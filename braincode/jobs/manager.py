from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from braincode.jobs.models import (
    Job,
    JobEvent,
    JobKind,
    JobQuery,
    JobSpec,
    JobStatus,
    Schedule,
    ScheduleSpec,
)
from braincode.jobs.store import JobStateError, SQLiteJobStore
from braincode.jobs.cron import CronExpression


DEFAULT_LEASE_SECONDS = 30


class JobManager:
    """Small runtime service around the durable JobStore.

    It deliberately owns no scheduler or background event loop. Callers retain
    asyncio task ownership while this service provides durable state, leases,
    progress and notification events.
    """

    def __init__(
        self,
        store: SQLiteJobStore,
        *,
        owner: str | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        event_sink: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.store = store
        self.owner = owner or f"runtime-{uuid.uuid4().hex}"
        self.lease_seconds = lease_seconds
        self.event_sink = event_sink

    @classmethod
    def for_project(
        cls,
        project_dir: str | Path,
        *,
        event_sink: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> JobManager:
        database = Path(project_dir) / ".braincode" / "runtime.db"
        return cls(SQLiteJobStore(database), event_sink=event_sink)

    def _emit(self, event_type: str, job: Job, **extra: Any) -> None:
        if self.event_sink is None:
            return
        payload = {
            "job_id": job.id,
            "kind": job.kind.value,
            "name": job.name,
            "status": job.status.value,
            **extra,
        }
        self.event_sink(event_type, payload)

    def create(self, spec: JobSpec) -> Job:
        job = self.store.create(spec)
        self._emit("job_created", job)
        return job

    def create_schedule(self, spec: ScheduleSpec) -> Schedule:
        next_run = CronExpression.parse(spec.cron_expression).next_after(
            self.store._now(), spec.timezone
        )
        return self.store.create_schedule(spec, next_run)

    def list_schedules(self) -> list[Schedule]:
        return self.store.list_schedules()

    def delete_schedule(self, schedule_id: str) -> bool:
        return self.store.delete_schedule(schedule_id)

    def create_agent(
        self,
        *,
        job_id: str,
        name: str,
        task: str,
        team_name: str = "",
        worktree_path: str = "",
        partial_result: str = "",
    ) -> Job:
        payload = {
            "task": task,
            "partial_result": partial_result,
            "runtime_id": self.owner,
        }
        return self.create(
            JobSpec(
                id=job_id,
                kind=JobKind.AGENT,
                name=name,
                description=task,
                payload_json=json.dumps(payload, ensure_ascii=False),
                team_name=team_name,
                worktree_path=worktree_path,
            )
        )

    def claim(self, job_id: str) -> Job | None:
        job = self.store.claim(job_id, self.owner, self.lease_seconds)
        if job is not None:
            self._emit("job_started", job)
        return job

    def heartbeat(self, job_id: str) -> bool:
        return self.store.heartbeat(job_id, self.owner, self.lease_seconds)

    def complete(self, job_id: str, result: str) -> Job:
        job = self.store.complete(job_id, self.owner, result)
        self._emit("job_completed", job, result=result)
        return job

    def fail(self, job_id: str, error: str) -> Job:
        job = self.store.fail(job_id, self.owner, error)
        self._emit("job_failed", job, error=error)
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.store.cancel(job_id)
        self._emit("job_cancelled", job)
        return job

    def update_progress(self, job_id: str, progress: dict[str, Any]) -> Job:
        job = self.store.update_progress(
            job_id, json.dumps(progress, ensure_ascii=False, sort_keys=True)
        )
        self._emit("job_progress", job, progress=progress)
        return job

    def mark_failed(self, job_id: str, error: str) -> Job:
        job = self.store.mark_failed(job_id, error)
        self._emit("job_failed", job, error=error)
        return job

    def append_event(
        self, job_id: str, event_type: str, payload: dict[str, Any] | None = None
    ) -> JobEvent:
        return self.store.append_event(
            job_id,
            event_type,
            json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
        )

    def poll_terminal_events(self, *, kind: JobKind = JobKind.AGENT) -> list[JobEvent]:
        return self.store.consume_events(
            ("completed", "failed", "cancelled"), (kind,)
        )

    def recover_agent_jobs(self) -> list[Job]:
        """Turn orphaned Agent jobs into explicit terminal failures.

        Phase 2 persists Agent metadata but not a serializable live Agent. A job
        created by another runtime therefore cannot safely resume. Worktree
        existence is checked first so recovery never executes in a wrong cwd.
        """
        expired_ids = {job.id for job in self.store.recover_expired()}
        recovered: list[Job] = []
        jobs = self.store.list(JobQuery(kinds=(JobKind.AGENT,), limit=10_000))
        now = datetime.now(UTC)
        for job in jobs:
            if job.is_terminal:
                continue
            try:
                payload = json.loads(job.payload_json)
            except json.JSONDecodeError:
                payload = {}
            runtime_id = payload.get("runtime_id")
            if not runtime_id or runtime_id == self.owner:
                continue
            if (
                job.status == JobStatus.RUNNING
                and job.lease_until is not None
                and job.lease_until > now
            ):
                # Another live Braincode process may share this project DB.
                continue
            if (
                job.status in {JobStatus.PENDING, JobStatus.BLOCKED}
                and job.id not in expired_ids
                and (
                    not job.worktree_path or Path(job.worktree_path).is_dir()
                )
                and (now - job.updated_at).total_seconds() < self.lease_seconds
            ):
                # Avoid racing another runtime between create() and claim().
                continue
            if job.worktree_path and not Path(job.worktree_path).is_dir():
                reason = f"Agent worktree no longer exists: {job.worktree_path}"
            else:
                reason = (
                    "Agent runtime state is unavailable after restart; "
                    "the job cannot be resumed safely"
                )
            recovered.append(self.mark_failed(job.id, reason))
        return recovered

    def ensure_team_task_running(self, job_id: str, owner: str) -> Job | None:
        job = self.store.get(job_id)
        if job is None:
            return None
        if job.status == JobStatus.RUNNING:
            return job
        if job.status != JobStatus.PENDING:
            return job
        return self.store.claim(job_id, owner, self.lease_seconds)

    def complete_team_task(self, job_id: str, owner: str, result: str = "") -> Job:
        job = self.store.get(job_id)
        if job is None:
            raise JobStateError(f"Job '{job_id}' not found")
        if job.status == JobStatus.COMPLETED:
            return job
        if job.status == JobStatus.PENDING:
            claimed = self.store.claim(job_id, owner, self.lease_seconds)
            if claimed is None:
                raise JobStateError(f"Team job '{job_id}' cannot be claimed")
            job = claimed
        if job.status != JobStatus.RUNNING or job.owner is None:
            raise JobStateError(f"Team job '{job_id}' is {job.status.value}")
        return self.store.complete(job_id, job.owner, result)
