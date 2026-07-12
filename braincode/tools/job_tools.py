from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from braincode.jobs import (
    JobKind,
    JobManager,
    JobQuery,
    JobSpec,
    MisfirePolicy,
    OverlapPolicy,
    ScheduleSpec,
    SchedulerService,
)
from braincode.jobs.runners import BackgroundToolRunner, PromptJobRunner
from braincode.tools.base import Tool, ToolResult


class JobCreateParams(BaseModel):
    kind: JobKind
    name: str
    payload: dict[str, Any] = Field(default_factory=dict)
    cwd: str = ""


class JobCreateTool(Tool):
    name = "JobCreate"
    description = "Create a durable prompt or tool Job."
    params_model = JobCreateParams
    category = "command"

    def __init__(
        self,
        manager: JobManager,
        tool_runner: BackgroundToolRunner | None = None,
        prompt_runner: PromptJobRunner | None = None,
        task_manager: Any = None,
    ) -> None:
        self.manager = manager
        self.tool_runner = tool_runner
        self.prompt_runner = prompt_runner
        self.task_manager = task_manager

    async def execute(self, params: BaseModel) -> ToolResult:
        p: JobCreateParams = params  # type: ignore[assignment]
        if p.kind == JobKind.TOOL:
            if self.tool_runner is None:
                return ToolResult("Background tool runner is not configured", True)
            tool_name = str(p.payload.get("tool_name", ""))
            arguments = p.payload.get("arguments", {})
            if not tool_name or not isinstance(arguments, dict):
                return ToolResult("Tool Jobs require tool_name and arguments", True)
            job = self.tool_runner.enqueue(tool_name, arguments, cwd=p.cwd or None)
        elif p.kind == JobKind.PROMPT:
            prompt = str(p.payload.get("prompt", ""))
            if not prompt:
                return ToolResult("Prompt Jobs require payload.prompt", True)
            job = self.manager.create(
                JobSpec(
                    kind=JobKind.PROMPT,
                    name=p.name,
                    description=prompt,
                    payload_json=json.dumps(p.payload, ensure_ascii=False),
                    worktree_path=p.cwd,
                )
            )
            if self.prompt_runner is not None:
                self.prompt_runner.submit_job(job)
        else:
            return ToolResult("JobCreate currently supports prompt and tool Jobs", True)
        return ToolResult(f"Created Job {job.id} ({job.status.value})")


class JobGetParams(BaseModel):
    job_id: str


class JobGetTool(Tool):
    name = "JobGet"
    description = "Get durable Job status, progress and result."
    params_model = JobGetParams

    def __init__(self, manager: JobManager) -> None:
        self.manager = manager

    async def execute(self, params: BaseModel) -> ToolResult:
        p: JobGetParams = params  # type: ignore[assignment]
        job = self.manager.store.get(p.job_id)
        if job is None:
            return ToolResult(f"Job '{p.job_id}' not found", True)
        return ToolResult(
            json.dumps(
                {
                    "id": job.id,
                    "kind": job.kind.value,
                    "name": job.name,
                    "status": job.status.value,
                    "progress": json.loads(job.progress_json),
                    "result": job.result_text,
                    "error": job.error_text,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


class JobListParams(BaseModel):
    kind: JobKind | None = None
    limit: int = Field(default=20, ge=1, le=200)


class JobListTool(Tool):
    name = "JobList"
    description = "List durable Jobs."
    params_model = JobListParams

    def __init__(self, manager: JobManager) -> None:
        self.manager = manager

    async def execute(self, params: BaseModel) -> ToolResult:
        p: JobListParams = params  # type: ignore[assignment]
        kinds = (p.kind,) if p.kind is not None else ()
        jobs = self.manager.store.list(JobQuery(kinds=kinds, limit=p.limit))
        return ToolResult(
            "\n".join(
                f"[{job.id}] {job.kind.value:<6} {job.status.value:<10} {job.name}"
                for job in jobs
            )
            or "No Jobs"
        )


class JobCancelParams(BaseModel):
    job_id: str


class JobCancelTool(Tool):
    name = "JobCancel"
    description = "Cancel a pending or running durable Job."
    params_model = JobCancelParams
    category = "command"

    def __init__(
        self,
        manager: JobManager,
        tool_runner: BackgroundToolRunner | None = None,
        prompt_runner: PromptJobRunner | None = None,
    ) -> None:
        self.manager = manager
        self.tool_runner = tool_runner
        self.prompt_runner = prompt_runner

    async def execute(self, params: BaseModel) -> ToolResult:
        p: JobCancelParams = params  # type: ignore[assignment]
        job = self.manager.store.get(p.job_id)
        if job is None or job.is_terminal:
            return ToolResult(f"Job '{p.job_id}' cannot be cancelled", True)
        if self.tool_runner is not None and job.kind == JobKind.TOOL:
            self.tool_runner.cancel(job.id)
        elif self.prompt_runner is not None and job.kind == JobKind.PROMPT:
            self.prompt_runner.cancel(job.id)
        elif self.task_manager is not None and job.kind == JobKind.AGENT:
            if not self.task_manager.cancel(job.id):
                self.manager.cancel(job.id)
        else:
            self.manager.cancel(job.id)
        return ToolResult(f"Cancelled Job {job.id}")


class CronCreateParams(BaseModel):
    name: str
    cron_expression: str
    timezone: str = ""
    job_kind: JobKind
    payload: dict[str, Any]
    misfire_policy: MisfirePolicy | None = None
    overlap_policy: OverlapPolicy | None = None


class CronCreateTool(Tool):
    name = "CronCreate"
    description = "Create a persistent Cron schedule."
    params_model = CronCreateParams
    category = "command"

    def __init__(
        self,
        scheduler: SchedulerService,
        tool_runner: BackgroundToolRunner | None = None,
    ) -> None:
        self.scheduler = scheduler
        self.tool_runner = tool_runner

    async def execute(self, params: BaseModel) -> ToolResult:
        p: CronCreateParams = params  # type: ignore[assignment]
        payload = p.payload
        if p.job_kind == JobKind.TOOL:
            if self.tool_runner is None:
                return ToolResult("Background tool runner is not configured", True)
            tool_name = str(payload.get("tool_name", ""))
            arguments = payload.get("arguments", {})
            if not tool_name or not isinstance(arguments, dict):
                return ToolResult("Tool schedules require tool_name and arguments", True)
            try:
                payload, _, decision = self.tool_runner.prepare_payload(
                    tool_name,
                    arguments,
                    cwd=str(payload.get("cwd", "")) or None,
                    creator="cron",
                )
                if decision.effect != "allow":
                    return ToolResult(
                        "Background tool permission "
                        f"{decision.effect}: {decision.reason}",
                        True,
                    )
            except ValueError as exc:
                return ToolResult(str(exc), True)
        schedule = self.scheduler.create(
            ScheduleSpec(
                name=p.name,
                cron_expression=p.cron_expression,
                timezone=p.timezone or self.scheduler.default_timezone,
                job_kind=p.job_kind,
                payload_json=json.dumps(payload, ensure_ascii=False),
                misfire_policy=(
                    p.misfire_policy or self.scheduler.default_misfire_policy
                ),
                overlap_policy=(
                    p.overlap_policy or self.scheduler.default_overlap_policy
                ),
            )
        )
        return ToolResult(
            f"Created schedule {schedule.id}; next run {schedule.next_run_at.isoformat()}"
        )


class CronListParams(BaseModel):
    enabled_only: bool = False


class CronListTool(Tool):
    name = "CronList"
    description = "List persistent Cron schedules."
    params_model = CronListParams

    def __init__(self, scheduler: SchedulerService) -> None:
        self.scheduler = scheduler

    async def execute(self, params: BaseModel) -> ToolResult:
        p: CronListParams = params  # type: ignore[assignment]
        schedules = self.scheduler.list(enabled_only=p.enabled_only)
        return ToolResult(
            "\n".join(
                f"[{item.id}] {item.cron_expression} {item.timezone} "
                f"next={item.next_run_at.isoformat()} {item.name}"
                for item in schedules
            )
            or "No schedules"
        )


class CronDeleteParams(BaseModel):
    schedule_id: str


class CronDeleteTool(Tool):
    name = "CronDelete"
    description = "Delete a persistent Cron schedule without deleting its Jobs."
    params_model = CronDeleteParams
    category = "command"

    def __init__(self, scheduler: SchedulerService) -> None:
        self.scheduler = scheduler

    async def execute(self, params: BaseModel) -> ToolResult:
        p: CronDeleteParams = params  # type: ignore[assignment]
        if not self.scheduler.delete(p.schedule_id):
            return ToolResult(f"Schedule '{p.schedule_id}' not found", True)
        return ToolResult(f"Deleted schedule {p.schedule_id}")
