from __future__ import annotations

import asyncio
import json
from pathlib import Path
import pytest

from braincode.agents.task_manager import TaskManager
from braincode.jobs import JobKind, JobManager, JobSpec, JobStatus, SQLiteJobStore
from braincode.teams.manager import TeamManager
from braincode.teams.models import BackendType
from braincode.teams.shared_task import SharedTaskStore


class EventAgent:
    def __init__(self, work_dir: Path, *, fail: bool = False) -> None:
        self.work_dir = work_dir
        self.team_name = ""
        self._team_manager = None
        self.agent_id = "agent-1"
        self.total_input_tokens = 12
        self.total_output_tokens = 7
        self._fail = fail

    async def run_to_completion(self, task, conversation=None, event_callback=None):
        if event_callback:
            event_callback(
                {
                    "type": "usage",
                    "usage": {"inputTokens": 12, "outputTokens": 7},
                }
            )
            event_callback(
                {"type": "tool_use", "toolName": "Read", "args": {"path": "x"}}
            )
        if self._fail:
            raise RuntimeError("durable boom")
        return "durable result"


def make_manager(tmp_path: Path, owner: str = "runtime-one") -> JobManager:
    return JobManager(SQLiteJobStore(tmp_path / "runtime.db"), owner=owner)


def test_progress_and_events_are_durable(tmp_path: Path) -> None:
    store = SQLiteJobStore(tmp_path / "runtime.db")
    job = store.create(JobSpec(kind=JobKind.AGENT, name="progress"))

    store.update_progress(job.id, '{"tool_call_count": 2}')
    current = store.get(job.id)
    assert current is not None
    assert json.loads(current.progress_json) == {"tool_call_count": 2}

    events = store.consume_events(("progress",), (JobKind.AGENT,))
    assert [event.event_type for event in events] == ["progress"]
    assert store.consume_events(("progress",), (JobKind.AGENT,)) == []


@pytest.mark.asyncio
async def test_task_manager_persists_completion_progress_and_notification(
    tmp_path: Path,
) -> None:
    manager = make_manager(tmp_path)
    task_manager = TaskManager(manager)
    agent = EventAgent(tmp_path)

    task_id = task_manager.launch(agent, "do durable work", name="durable-agent")
    await asyncio.sleep(0.1)

    job = manager.store.get(task_id)
    assert job is not None
    assert job.status == JobStatus.COMPLETED
    assert job.result_text == "durable result"
    progress = json.loads(job.progress_json)
    assert progress["tool_call_count"] == 1
    assert progress["input_tokens"] == 12
    assert progress["output_tokens"] == 7
    assert progress["last_activity"]

    completed = task_manager.poll_completed()
    assert [task.id for task in completed] == [task_id]
    assert completed[0].result == "durable result"
    assert task_manager.poll_completed() == []


@pytest.mark.asyncio
async def test_task_manager_persists_failure(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    task_manager = TaskManager(manager)
    task_id = task_manager.launch(EventAgent(tmp_path, fail=True), "fail")
    await asyncio.sleep(0.1)

    job = manager.store.get(task_id)
    assert job is not None
    assert job.status == JobStatus.FAILED
    assert "durable boom" in job.error_text
    assert task_manager.poll_completed()[0].status == "failed"


@pytest.mark.asyncio
async def test_adopt_running_creates_and_completes_agent_job(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    task_manager = TaskManager(manager)
    task_id = task_manager.adopt_running(
        EventAgent(tmp_path),
        "continue foreground work",
        partial_result="partial",
        name="adopted",
    )
    await asyncio.sleep(0.1)

    job = manager.store.get(task_id)
    assert job is not None
    assert job.status == JobStatus.COMPLETED
    assert job.result_text == "partial\ndurable result"
    assert json.loads(job.payload_json)["partial_result"] == "partial"


@pytest.mark.asyncio
async def test_task_manager_cancels_runtime_and_job(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    task_manager = TaskManager(manager)
    agent = EventAgent(tmp_path)

    async def wait_forever(*args, **kwargs):
        await asyncio.sleep(60)
        return "late"

    agent.run_to_completion = wait_forever  # type: ignore[method-assign]
    task_id = task_manager.launch(agent, "wait")
    await asyncio.sleep(0.05)
    assert task_manager.cancel(task_id)
    await asyncio.sleep(0.05)

    job = manager.store.get(task_id)
    assert job is not None
    assert job.status == JobStatus.CANCELLED
    assert task_manager.poll_completed()[0].status == "cancelled"


def test_restart_marks_agent_with_missing_worktree_failed(tmp_path: Path) -> None:
    store = SQLiteJobStore(tmp_path / "runtime.db")
    old_manager = JobManager(store, owner="old-runtime")
    old_manager.create_agent(
        job_id="orphan",
        name="orphan",
        task="resume me",
        worktree_path=str(tmp_path / "missing-worktree"),
    )

    new_manager = JobManager(store, owner="new-runtime")
    restarted_tasks = TaskManager(new_manager)
    job = store.get("orphan")
    assert job is not None
    assert job.status == JobStatus.FAILED
    assert "worktree no longer exists" in job.error_text

    notification = restarted_tasks.poll_completed()
    assert len(notification) == 1
    assert notification[0].agent is None
    assert notification[0].status == "failed"


def test_recovery_does_not_take_over_another_live_runtime(tmp_path: Path) -> None:
    store = SQLiteJobStore(tmp_path / "runtime.db")
    first = JobManager(store, owner="live-runtime")
    first.create_agent(
        job_id="live",
        name="live",
        task="still running",
        worktree_path=str(tmp_path),
    )
    assert first.claim("live") is not None

    TaskManager(JobManager(store, owner="other-runtime"))
    job = store.get("live")
    assert job is not None
    assert job.status == JobStatus.RUNNING
    assert job.owner == "live-runtime"

    first.cancel("live")


def test_team_tasks_use_durable_dependencies(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    shared = SharedTaskStore(
        tmp_path / "team" / "tasks.json", manager, team_name="alpha"
    )
    first = shared.create("first", assignee="worker", blocks=["2"])
    second = shared.create("second")

    first_job = manager.store.get("team:alpha:task:1")
    second_job = manager.store.get("team:alpha:task:2")
    assert first_job is not None and first_job.kind == JobKind.TEAM
    assert second_job is not None and second_job.status == JobStatus.BLOCKED
    assert second_job.dependencies == ("team:alpha:task:1",)

    shared.update(first.id, status="in_progress")
    shared.update(first.id, status="completed")
    assert manager.store.get("team:alpha:task:1").status == JobStatus.COMPLETED
    assert manager.store.get("team:alpha:task:2").status == JobStatus.PENDING
    assert shared.get(second.id).status == "pending"


def test_team_store_does_not_delete_jobs_when_json_is_reset(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    shared = SharedTaskStore(tmp_path / "tasks.json", manager, team_name="safe")
    shared.create("keep durable")
    shared.init_empty()

    assert manager.store.get("team:safe:task:1") is not None


def test_deleting_team_preserves_its_durable_job(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    teams = TeamManager(job_manager=manager)
    teams._detected_backend = BackendType.IN_PROCESS
    team = teams.create_team("phase-two-delete", "lead")
    task_store = teams.get_task_store(team.name)
    assert task_store is not None
    task_store.create("preserve after team deletion")

    teams.delete_team(team.name)

    assert manager.store.get(f"team:{team.name}:task:1") is not None
