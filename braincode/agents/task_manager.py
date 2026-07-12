# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from braincode.agent import Agent
    from braincode.jobs.manager import JobManager

log = logging.getLogger(__name__)


@dataclass
class ProgressInfo:
    tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    last_activity: str = ""


@dataclass
class BackgroundTask:
    id: str
    name: str
    agent: Agent | None
    task: str
    status: str = "running"
    result: str = ""
    start_time: float = field(default_factory=time.monotonic)
    end_time: float | None = None
    cancel: Callable[[], None] | None = None
    progress: ProgressInfo = field(default_factory=ProgressInfo)


class TaskManager:

    def __init__(self, job_manager: JobManager | None = None) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._notify_queue: asyncio.Queue[str] = asyncio.Queue()
        self._async_tasks: dict[str, asyncio.Task[None]] = {}
        self._job_manager = job_manager
        if self._job_manager is not None:
            self._job_manager.recover_agent_jobs()


    def launch(
        self,
        agent: Agent,
        task: str,
        name: str = "",
        fork_conversation: Any = None,
    ) -> str:
        task_id = uuid.uuid4().hex[:8]
        bg = BackgroundTask(
            id=task_id,
            name=name or task_id,
            agent=agent,
            task=task,
        )
        if self._job_manager is not None:
            self._job_manager.create_agent(
                job_id=task_id,
                name=bg.name,
                task=task,
                team_name=getattr(agent, "team_name", "") or "",
                worktree_path=str(getattr(agent, "work_dir", "") or ""),
            )
        self._tasks[task_id] = bg

        async_task = asyncio.create_task(
            self._run_background(task_id, fork_conversation)
        )
        self._async_tasks[task_id] = async_task

        bg.cancel = async_task.cancel
        return task_id


    async def _run_background(
        self, task_id: str, fork_conversation: Any = None
    ) -> None:
        bg = self._tasks.get(task_id)
        if bg is None:
            return

        heartbeat_task: asyncio.Task[None] | None = None

        try:
            if self._job_manager is not None:
                claimed = self._job_manager.claim(task_id)
                if claimed is None:
                    job = self._job_manager.store.get(task_id)
                    bg.status = job.status.value if job is not None else "failed"
                    bg.result = (
                        job.error_text if job is not None and job.error_text
                        else "Task could not claim its durable Job"
                    )
                    return
                heartbeat_task = asyncio.create_task(self._heartbeat_job(task_id))

            event_callback = (
                self._make_event_callback(bg) if self._job_manager is not None else None
            )
            if fork_conversation is not None:
                if event_callback is None:
                    result = await bg.agent.run_to_completion("", fork_conversation)
                else:
                    result = await bg.agent.run_to_completion(
                        "", fork_conversation, event_callback=event_callback
                    )
            else:
                if event_callback is None:
                    result = await bg.agent.run_to_completion(bg.task)
                else:
                    result = await bg.agent.run_to_completion(
                        bg.task, event_callback=event_callback
                    )
            bg.result = result
            bg.status = "completed"

            if bg.agent.team_name and bg.agent._team_manager:
                mailbox = bg.agent._team_manager.get_mailbox(bg.agent.team_name)
                if mailbox:
                    from braincode.teams.mailbox import create_message
                    msg = create_message(
                        from_agent=bg.name,
                        to_agent="lead",
                        content=f"[idle] {bg.name}: completed initial task",
                        summary=f"{bg.name} idle",
                    )
                    mailbox.write("lead", msg)

                    for _ in range(60):
                        await asyncio.sleep(1)
                        msgs = mailbox.consume(bg.agent.agent_id)
                        if not msgs:
                            continue
                        prompt = "\n\n".join(
                            f"[Message from {m.from_agent}] {m.content}" for m in msgs
                        )
                        result = await bg.agent.run_to_completion(prompt)
                        bg.result = result
                        msg = create_message(
                            from_agent=bg.name,
                            to_agent="lead",
                            content=f"[idle] {bg.name}: completed follow-up",
                            summary=f"{bg.name} idle",
                        )
                        mailbox.write("lead", msg)

            if self._job_manager is not None:
                self._job_manager.complete(task_id, bg.result)

        except asyncio.CancelledError:
            bg.status = "cancelled"
            bg.result = "Task was cancelled"
            if self._job_manager is not None:
                self._job_manager.cancel(task_id)
        except Exception as e:
            log.error("Background task %s failed: %s", task_id, e)
            bg.status = "failed"
            bg.result = f"Error: {e}"
            if self._job_manager is not None:
                try:
                    self._job_manager.fail(task_id, str(e))
                except Exception:
                    log.exception("Failed to persist failure for Job %s", task_id)
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            bg.end_time = time.monotonic()
            if bg.agent is not None:
                bg.progress.input_tokens = bg.agent.total_input_tokens
                bg.progress.output_tokens = bg.agent.total_output_tokens
            self._persist_progress(bg)
            self._async_tasks.pop(task_id, None)
            if self._job_manager is None:
                await self._notify_queue.put(task_id)


    def adopt_running(
        self,
        agent: Agent,
        task_description: str,
        partial_result: str = "",
        name: str = "",
    ) -> str:
        task_id = uuid.uuid4().hex[:8]
        bg = BackgroundTask(
            id=task_id,
            name=name or task_id,
            agent=agent,
            task=task_description,
            result=partial_result,
        )
        if self._job_manager is not None:
            self._job_manager.create_agent(
                job_id=task_id,
                name=bg.name,
                task=task_description,
                team_name=getattr(agent, "team_name", "") or "",
                worktree_path=str(getattr(agent, "work_dir", "") or ""),
                partial_result=partial_result,
            )
        self._tasks[task_id] = bg

        async_task = asyncio.create_task(self._continue_background(task_id))
        self._async_tasks[task_id] = async_task
        bg.cancel = async_task.cancel
        return task_id


    async def _continue_background(self, task_id: str) -> None:
        bg = self._tasks.get(task_id)
        if bg is None:
            return

        heartbeat_task: asyncio.Task[None] | None = None

        try:
            if self._job_manager is not None:
                claimed = self._job_manager.claim(task_id)
                if claimed is None:
                    job = self._job_manager.store.get(task_id)
                    bg.status = job.status.value if job is not None else "failed"
                    bg.result = (
                        job.error_text if job is not None and job.error_text
                        else "Task could not claim its durable Job"
                    )
                    return
                heartbeat_task = asyncio.create_task(self._heartbeat_job(task_id))
            event_callback = (
                self._make_event_callback(bg) if self._job_manager is not None else None
            )
            if event_callback is None:
                result = await bg.agent.run_to_completion(bg.task)
            else:
                result = await bg.agent.run_to_completion(
                    bg.task, event_callback=event_callback
                )
            bg.result = (bg.result + "\n" + result).strip() if bg.result else result
            bg.status = "completed"
            if self._job_manager is not None:
                self._job_manager.complete(task_id, bg.result)
        except asyncio.CancelledError:
            bg.status = "cancelled"
            if self._job_manager is not None:
                self._job_manager.cancel(task_id)
        except Exception as e:
            log.error("Background task %s failed: %s", task_id, e)
            bg.status = "failed"
            bg.result = f"Error: {e}"
            if self._job_manager is not None:
                try:
                    self._job_manager.fail(task_id, str(e))
                except Exception:
                    log.exception("Failed to persist failure for Job %s", task_id)
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            bg.end_time = time.monotonic()
            if bg.agent is not None:
                bg.progress.input_tokens = bg.agent.total_input_tokens
                bg.progress.output_tokens = bg.agent.total_output_tokens
            self._persist_progress(bg)
            self._async_tasks.pop(task_id, None)
            if self._job_manager is None:
                await self._notify_queue.put(task_id)

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[BackgroundTask]:
        return list(self._tasks.values())

    def cancel(self, task_id: str) -> bool:
        bg = self._tasks.get(task_id)
        if bg is None or bg.status != "running":
            return False
        async_task = self._async_tasks.get(task_id)
        if async_task and not async_task.done():
            if self._job_manager is not None:
                self._job_manager.cancel(task_id)
            async_task.cancel()
            return True
        return False

    def poll_completed(self) -> list[BackgroundTask]:
        if self._job_manager is not None:
            self._job_manager.recover_agent_jobs()
            return self._poll_durable_completed()

        completed: list[BackgroundTask] = []
        while not self._notify_queue.empty():
            try:
                task_id = self._notify_queue.get_nowait()
                bg = self._tasks.get(task_id)
                if bg is not None:
                    completed.append(bg)
            except asyncio.QueueEmpty:
                break
        return completed

    async def _heartbeat_job(self, task_id: str) -> None:
        if self._job_manager is None:
            return
        interval = max(1.0, self._job_manager.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            if not self._job_manager.heartbeat(task_id):
                log.warning("Durable lease was lost for Job %s", task_id)
                return

    def _make_event_callback(self, bg: BackgroundTask) -> Callable[[dict[str, Any]], None]:
        def on_event(event: dict[str, Any]) -> None:
            event_type = str(event.get("type", "activity"))
            if event_type == "usage":
                usage = event.get("usage", {})
                bg.progress.input_tokens = int(usage.get("inputTokens", 0))
                bg.progress.output_tokens = int(usage.get("outputTokens", 0))
            elif event_type == "tool_use":
                bg.progress.tool_call_count += 1
                if self._job_manager is not None:
                    self._job_manager.append_event(
                        bg.id,
                        "tool_use",
                        {
                            "tool_name": event.get("toolName", ""),
                            "tool_call_count": bg.progress.tool_call_count,
                        },
                    )
            bg.progress.last_activity = datetime.now(UTC).isoformat()
            self._persist_progress(bg)

        return on_event

    def _persist_progress(self, bg: BackgroundTask) -> None:
        if self._job_manager is None:
            return
        self._job_manager.update_progress(
            bg.id,
            {
                "tool_call_count": bg.progress.tool_call_count,
                "input_tokens": bg.progress.input_tokens,
                "output_tokens": bg.progress.output_tokens,
                "last_activity": bg.progress.last_activity,
            },
        )

    def _poll_durable_completed(self) -> list[BackgroundTask]:
        if self._job_manager is None:
            return []
        completed: list[BackgroundTask] = []
        for event in self._job_manager.poll_terminal_events():
            job = self._job_manager.store.get(event.job_id)
            if job is None:
                continue
            bg = self._tasks.get(job.id)
            if bg is None:
                elapsed = 0.0
                if job.started_at is not None and job.finished_at is not None:
                    elapsed = max(0.0, (job.finished_at - job.started_at).total_seconds())
                bg = BackgroundTask(
                    id=job.id,
                    name=job.name,
                    agent=None,
                    task=job.description,
                    start_time=0.0,
                    end_time=elapsed,
                )
                self._tasks[job.id] = bg
            bg.status = job.status.value
            if job.status.value == "completed":
                bg.result = job.result_text
            elif job.status.value == "cancelled":
                bg.result = "Task was cancelled"
            else:
                bg.result = f"Error: {job.error_text}" if job.error_text else "Task failed"
            try:
                progress = json.loads(job.progress_json)
            except json.JSONDecodeError:
                progress = {}
            bg.progress.tool_call_count = int(progress.get("tool_call_count", 0))
            bg.progress.input_tokens = int(progress.get("input_tokens", 0))
            bg.progress.output_tokens = int(progress.get("output_tokens", 0))
            bg.progress.last_activity = str(progress.get("last_activity", ""))
            completed.append(bg)
        return completed
