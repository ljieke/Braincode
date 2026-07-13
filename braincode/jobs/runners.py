from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from braincode.jobs.manager import JobManager
from braincode.jobs.models import Job, JobKind, JobQuery, JobSpec, JobStatus
from braincode.permissions import PermissionChecker
from braincode.tools import ToolRegistry
from braincode.tools.base import Tool, ToolResult


MAX_INLINE_JOB_OUTPUT = 10_000


def _output_summary(job_id: str, output: str, output_dir: Path) -> str:
    if len(output) <= MAX_INLINE_JOB_OUTPUT:
        return output
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{job_id}.txt"
    path.write_text(output, encoding="utf-8")
    return (
        output[:2_000]
        + f"\n... (full output persisted at {path}, {len(output)} characters)"
    )


class BackgroundToolRunner:
    def __init__(
        self,
        manager: JobManager,
        registry: ToolRegistry,
        permission_checker: PermissionChecker,
        *,
        work_dir: str,
        output_dir: str | Path | None = None,
    ) -> None:
        self.manager = manager
        self.registry = registry
        self.permission_checker = permission_checker
        self.work_dir = str(Path(work_dir).resolve())
        self.output_dir = Path(output_dir or Path(work_dir) / ".braincode" / "job-output")
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def enqueue(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        cwd: str | None = None,
        creator: str = "agent",
        parent_job_id: str | None = None,
    ) -> Job:
        payload, tool, decision = self.prepare_payload(
            tool_name,
            arguments,
            cwd=cwd,
            creator=creator,
            parent_job_id=parent_job_id,
        )
        validated = payload["arguments"]
        cwd_value = payload["cwd"]
        job = self.manager.create(
            JobSpec(
                kind=JobKind.TOOL,
                name=f"{tool_name}: {str(validated)[:80]}",
                description=f"Background {tool_name}",
                payload_json=json.dumps(payload, ensure_ascii=False),
                worktree_path=cwd_value,
                parent_job_id=parent_job_id,
            )
        )
        if decision.effect != "allow":
            return self.manager.mark_failed(
                job.id,
                f"Background tool permission {decision.effect}: {decision.reason}",
            )
        self.submit_job(job)
        return job

    def prepare_payload(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        cwd: str | None = None,
        creator: str = "agent",
        parent_job_id: str | None = None,
    ) -> tuple[dict[str, Any], Tool, Any]:
        tool = self.registry.get(tool_name)
        if tool is None or not self.registry.is_enabled(tool_name):
            raise ValueError(f"Tool '{tool_name}' is not available")
        validated = tool.params_model.model_validate(arguments).model_dump()
        if "run_in_background" in validated:
            validated["run_in_background"] = False
        decision = self.permission_checker.check(tool, validated)
        cwd_value = str(Path(cwd or self.work_dir).resolve())
        payload = {
            "tool_name": tool_name,
            "arguments": validated,
            "cwd": cwd_value,
            "permission_mode": self.permission_checker.mode.value,
            "sandbox": {
                "enabled": bool(getattr(tool, "sandbox", None)),
                "config": repr(getattr(tool, "sandbox_config", None)),
            },
            "creator": creator,
            "parent_job_id": parent_job_id,
        }
        return payload, tool, decision

    def start_pending(self) -> None:
        self.manager.store.recover_expired()
        for job in self.manager.store.list(
            JobQuery(statuses=(JobStatus.PENDING,), kinds=(JobKind.TOOL,), limit=10_000)
        ):
            self.submit_job(job)

    def submit_job(self, job: Job) -> None:
        if job.kind != JobKind.TOOL or job.id in self._tasks:
            return
        task = asyncio.create_task(self._run(job.id))
        self._tasks[job.id] = task
        task.add_done_callback(lambda _task, job_id=job.id: self._tasks.pop(job_id, None))

    def cancel(self, job_id: str) -> bool:
        job = self.manager.store.get(job_id)
        if job is None or job.kind != JobKind.TOOL or job.is_terminal:
            return False
        self.manager.cancel(job_id)
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
        return True

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run(self, job_id: str) -> None:
        heartbeat: asyncio.Task[None] | None = None
        try:
            job = self.manager.store.get(job_id)
            if job is None or job.status != JobStatus.PENDING:
                return
            payload = json.loads(job.payload_json)
            cwd = Path(payload.get("cwd") or self.work_dir)
            if not cwd.is_dir():
                self.manager.mark_failed(
                    job_id, f"Background tool cwd no longer exists: {cwd}"
                )
                return
            tool_name = str(payload.get("tool_name", ""))
            tool = self.registry.get(tool_name)
            if tool is None or not self.registry.is_enabled(tool_name):
                self.manager.mark_failed(job_id, f"Tool '{tool_name}' is unavailable")
                return
            arguments = payload.get("arguments", {})
            if "run_in_background" in arguments:
                arguments["run_in_background"] = False
            decision = self.permission_checker.check(tool, arguments)
            if decision.effect != "allow":
                self.manager.mark_failed(
                    job_id,
                    f"Background tool permission {decision.effect}: {decision.reason}",
                )
                return
            claimed = self.manager.claim(job_id)
            if claimed is None:
                return
            heartbeat = asyncio.create_task(self._heartbeat(job_id))
            execution_tool = self._clone_tool_for_job(tool, str(cwd))
            params = execution_tool.params_model.model_validate(arguments)
            result: ToolResult = await execution_tool.execute(params)
            output = _output_summary(job_id, result.output, self.output_dir)
            if result.is_error:
                self.manager.fail(job_id, output)
            else:
                self.manager.complete(job_id, output)
        except asyncio.CancelledError:
            job = self.manager.store.get(job_id)
            if job is not None and not job.is_terminal:
                self.manager.cancel(job_id)
            raise
        except Exception as exc:
            job = self.manager.store.get(job_id)
            if job is not None and not job.is_terminal:
                self.manager.mark_failed(job_id, str(exc))
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    @staticmethod
    def _clone_tool_for_job(tool: Tool, cwd: str) -> Tool:
        if tool.name != "Bash":
            return tool
        cloned = type(tool)()
        cloned.work_dir = cwd
        cloned.sandbox = getattr(tool, "sandbox", None)
        cloned.sandbox_config = getattr(tool, "sandbox_config", None)
        return cloned

    async def _heartbeat(self, job_id: str) -> None:
        interval = max(1.0, self.manager.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            if not self.manager.heartbeat(job_id):
                return


class PromptJobRunner:
    def __init__(
        self,
        manager: JobManager,
        handler: Callable[[Job, str], Awaitable[str]],
        *,
        output_dir: str | Path,
    ) -> None:
        self.manager = manager
        self.handler = handler
        self.output_dir = Path(output_dir)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def start_pending(self) -> None:
        self.manager.store.recover_expired()
        for job in self.manager.store.list(
            JobQuery(statuses=(JobStatus.PENDING,), kinds=(JobKind.PROMPT,), limit=10_000)
        ):
            self.submit_job(job)

    def submit_job(self, job: Job) -> None:
        if job.kind != JobKind.PROMPT or job.id in self._tasks:
            return
        task = asyncio.create_task(self._run(job.id))
        self._tasks[job.id] = task
        task.add_done_callback(lambda _task, job_id=job.id: self._tasks.pop(job_id, None))

    def cancel(self, job_id: str) -> bool:
        job = self.manager.store.get(job_id)
        if job is None or job.kind != JobKind.PROMPT or job.is_terminal:
            return False
        self.manager.cancel(job_id)
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
        return True

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run(self, job_id: str) -> None:
        heartbeat: asyncio.Task[None] | None = None
        try:
            job = self.manager.claim(job_id)
            if job is None:
                return
            heartbeat = asyncio.create_task(self._heartbeat(job_id))
            payload = json.loads(job.payload_json)
            prompt = str(payload.get("prompt", ""))
            result = await self.handler(job, prompt)
            self.manager.complete(
                job_id, _output_summary(job_id, result, self.output_dir)
            )
        except asyncio.CancelledError:
            job = self.manager.store.get(job_id)
            if job is not None and not job.is_terminal:
                self.manager.cancel(job_id)
            raise
        except Exception as exc:
            job = self.manager.store.get(job_id)
            if job is not None and not job.is_terminal:
                self.manager.mark_failed(job_id, str(exc))
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    async def _heartbeat(self, job_id: str) -> None:
        interval = max(1.0, self.manager.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            if not self.manager.heartbeat(job_id):
                return
