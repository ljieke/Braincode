from __future__ import annotations

from pathlib import Path

import pytest

from braincode.client import LLMClient
from braincode.commands.handlers.jobs import create_jobs_command
from braincode.commands.registry import CommandContext
from braincode.config import ProviderConfig, SchedulerConfig
from braincode.hooks import Action, Hook, HookEngine, HookResult
from braincode.jobs import JobKind, JobManager, JobSpec
from braincode.permissions import PermissionMode
from braincode.runtime import (
    RuntimeEventBus,
    RuntimeEventType,
    build_runtime,
)
from braincode.tools.base import ToolCallComplete


class MockClient(LLMClient):
    provider_name = "mock"
    model = "mock-model"

    async def stream(self, conversation, system="", tools=None):
        if False:
            yield None


def _provider() -> ProviderConfig:
    return ProviderConfig(
        name="mock",
        protocol="anthropic",
        base_url="https://example.invalid",
        model="mock-model",
        api_key="test",
    )


def test_runtime_event_bus_delivers_same_event_to_all_surfaces() -> None:
    bus = RuntimeEventBus()
    tui: list[dict] = []
    cli: list[dict] = []
    remote: list[dict] = []
    bus.subscribe(lambda event: tui.append(event.to_dict()))
    bus.subscribe(lambda event: cli.append(event.to_dict()))
    bus.subscribe(lambda event: remote.append(event.to_dict()))

    bus.emit("provider_switched", {"provider": "fallback", "attempt": 2})

    assert tui == cli == remote
    assert tui[0]["type"] == "provider_switched"


def test_runtime_event_bus_accepts_tool_loop_guarded() -> None:
    bus = RuntimeEventBus()

    bus.emit(
        "tool_loop_guarded",
        {
            "tool_name": "ReadFile",
            "call_count": 5,
            "same_result_count": 4,
            "policy": "guard",
        },
    )

    assert bus.history()[0].type == RuntimeEventType.TOOL_LOOP_GUARDED


def test_job_manager_publishes_lifecycle_events(tmp_path: Path) -> None:
    bus = RuntimeEventBus()
    manager = JobManager.for_project(tmp_path, event_sink=bus.emit)

    job = manager.create(JobSpec(kind=JobKind.TOOL, name="build"))
    manager.claim(job.id)
    manager.update_progress(job.id, {"percent": 50})
    manager.complete(job.id, "done")

    assert [event.type for event in bus.history()] == [
        RuntimeEventType.JOB_CREATED,
        RuntimeEventType.JOB_STARTED,
        RuntimeEventType.JOB_PROGRESS,
        RuntimeEventType.JOB_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_agent_hook_changes_publish_runtime_events(tmp_path: Path) -> None:
    bus = RuntimeEventBus()
    modifying = Hook(
        id="modify",
        event="pre_tool_use",
        action=Action(type="prompt", message="modify"),
        configured_result=HookResult(updated_args={"command": "echo changed"}),
    )
    runtime = build_runtime(
        providers=[_provider()],
        client=MockClient(),
        work_dir=str(tmp_path),
        hook_engine=HookEngine([modifying]),
        scheduler_config=SchedulerConfig(enabled=False),
    )
    runtime.agent.runtime_event_sink = bus.emit

    result = await runtime.agent._execute_tool_noninteractive(
        ToolCallComplete("t1", "Bash", {"command": "echo original"})
    )

    assert result.is_error is False
    assert any(
        event.type == RuntimeEventType.HOOK_MODIFIED_INPUT
        for event in bus.history()
    )
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_build_runtime_initializes_and_stops_services_once(tmp_path: Path) -> None:
    runtime = build_runtime(
        providers=[_provider()],
        client=MockClient(),
        work_dir=str(tmp_path),
        permission_mode=PermissionMode.DEFAULT,
        scheduler_config=SchedulerConfig(enabled=False),
        is_interactive=False,
    )

    assert runtime.agent.registry is runtime.registry
    assert runtime.job_manager.store is runtime.scheduler.store
    assert runtime.agent.prompt_state_registry is runtime.prompt_state_registry
    assert runtime.registry.get("JobCreate") is not None
    assert runtime.registry.get("CronList") is not None
    assert runtime.registry.get("Agent") is not None
    assert runtime.scheduler._task is None

    await runtime.shutdown()
    assert runtime.session._file.closed is True


class MockUI:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def add_system_message(self, text: str) -> None:
        self.messages.append(text)

    def send_user_message(self, text: str) -> None:
        pass

    def set_plan_mode(self, enabled: bool) -> None:
        pass

    def get_token_count(self) -> tuple[int, int]:
        return 0, 0

    def refresh_status(self) -> None:
        pass


@pytest.mark.asyncio
async def test_jobs_command_reads_persisted_output(tmp_path: Path) -> None:
    manager = JobManager.for_project(tmp_path)
    job = manager.create(JobSpec(kind=JobKind.TOOL, name="large"))
    output_dir = tmp_path / ".braincode" / "job-output"
    output_dir.mkdir(parents=True)
    (output_dir / f"{job.id}.txt").write_text("full output", encoding="utf-8")
    ui = MockUI()
    command = create_jobs_command(manager)
    context = CommandContext(
        args=f"output {job.id}",
        agent=None,
        conversation=None,
        session=None,
        session_manager=None,
        memory_manager=None,
        ui=ui,
        config={},
    )

    await command.handler(context)

    assert ui.messages == ["full output"]
