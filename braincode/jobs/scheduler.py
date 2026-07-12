from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Callable

from braincode.jobs.cron import CronExpression, CronExpressionError
from braincode.jobs.models import (
    Job,
    JobKind,
    JobSpec,
    MisfirePolicy,
    OverlapPolicy,
    Schedule,
    ScheduleSpec,
)
from braincode.jobs.store import SQLiteJobStore


class SchedulerService:
    def __init__(
        self,
        store: SQLiteJobStore,
        *,
        poll_interval_seconds: float = 1.0,
        clock: Callable[[], datetime] | None = None,
        on_job_created: Callable[[Job], None] | None = None,
        default_timezone: str = "UTC",
        default_misfire_policy: MisfirePolicy = MisfirePolicy.SKIP,
        default_overlap_policy: OverlapPolicy = OverlapPolicy.COALESCE,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.store = store
        self.poll_interval_seconds = poll_interval_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._on_job_created = on_job_created
        self.default_timezone = default_timezone
        self.default_misfire_policy = default_misfire_policy
        self.default_overlap_policy = default_overlap_policy
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def create(self, spec: ScheduleSpec) -> Schedule:
        cron = CronExpression.parse(spec.cron_expression)
        if spec.job_kind not in {JobKind.PROMPT, JobKind.TOOL}:
            raise ValueError("Cron schedules currently support prompt and tool Jobs")
        try:
            payload = json.loads(spec.payload_json)
        except json.JSONDecodeError as exc:
            raise ValueError("payload_json must contain valid JSON") from exc
        if spec.job_kind == JobKind.PROMPT and not str(payload.get("prompt", "")).strip():
            raise ValueError("Prompt schedules require payload.prompt")
        if spec.job_kind == JobKind.TOOL:
            if not str(payload.get("tool_name", "")).strip():
                raise ValueError("Tool schedules require payload.tool_name")
            if not isinstance(payload.get("arguments", {}), dict):
                raise ValueError("Tool schedule arguments must be an object")
        next_run = cron.next_after(self._clock(), spec.timezone)
        return self.store.create_schedule(spec, next_run)

    def list(self, *, enabled_only: bool = False) -> list[Schedule]:
        return self.store.list_schedules(enabled_only=enabled_only)

    def delete(self, schedule_id: str) -> bool:
        return self.store.delete_schedule(schedule_id)

    def process_due(self, now: datetime | None = None) -> list[Job]:
        now = (now or self._clock()).astimezone(UTC)
        created: list[Job] = []
        for schedule in self.store.list_schedules(enabled_only=True):
            if schedule.next_run_at > now:
                continue
            cron = CronExpression.parse(schedule.cron_expression)
            scheduled_for = schedule.next_run_at
            late_by = (now - scheduled_for).total_seconds()
            missed = late_by > self.poll_interval_seconds
            if missed and schedule.misfire_policy == MisfirePolicy.SKIP:
                next_run = cron.next_after(now, schedule.timezone)
                self.store.record_schedule_fire(
                    schedule.id,
                    scheduled_for,
                    next_run,
                    job_spec=None,
                    disposition="misfire_skipped",
                )
                continue

            next_run = cron.next_after(
                now if missed else scheduled_for, schedule.timezone
            )
            active = self.store.has_active_schedule_job(schedule.id)
            if active and schedule.overlap_policy != OverlapPolicy.PARALLEL:
                disposition = (
                    "overlap_skipped"
                    if schedule.overlap_policy == OverlapPolicy.SKIP
                    else "overlap_coalesced"
                )
                self.store.record_schedule_fire(
                    schedule.id,
                    scheduled_for,
                    next_run,
                    job_spec=None,
                    disposition=disposition,
                )
                continue

            payload = json.loads(schedule.payload_json)
            job_spec = JobSpec(
                kind=schedule.job_kind,
                name=schedule.name,
                description=str(payload.get("prompt", payload.get("tool_name", ""))),
                payload_json=schedule.payload_json,
                worktree_path=str(payload.get("cwd", "")),
                schedule_id=schedule.id,
            )
            job = self.store.record_schedule_fire(
                schedule.id,
                scheduled_for,
                next_run,
                job_spec=job_spec,
                disposition="fired",
            )
            if job is not None:
                created.append(job)
                if self._on_job_created is not None:
                    self._on_job_created(job)
        return created

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run())
        return self._task

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            self.process_due()
            await asyncio.sleep(self.poll_interval_seconds)
