from __future__ import annotations

from dataclasses import dataclass

import pytest

from braincode.agent import Agent
from braincode.client import LLMClient
from braincode.conversation import ConversationManager
from braincode.prompt_state import (
    JobPromptStateProvider,
    PromptStateRegistry,
    TextPromptStateProvider,
)
from braincode.prompts import build_system_prompt
from braincode.tools import create_default_registry
from braincode.tools.base import StreamEnd, TextDelta, ToolCallComplete


@dataclass
class MutableProvider:
    name: str
    priority: int
    value: str
    volatile: bool = False
    render_count: int = 0

    def signature(self):
        return self.value

    def render(self) -> str:
        self.render_count += 1
        return self.value


def test_same_signature_reuses_rendered_section() -> None:
    registry = PromptStateRegistry()
    provider = MutableProvider("jobs", 10, "jobs-v1")
    registry.register(provider)

    assert registry.render() == "jobs-v1"
    assert registry.render() == "jobs-v1"
    assert provider.render_count == 1
    assert registry.stats().cache_hits == 1
    assert registry.stats().cache_misses == 1


def test_only_changed_provider_is_rebuilt() -> None:
    registry = PromptStateRegistry()
    jobs = MutableProvider("jobs", 10, "jobs-v1")
    teams = MutableProvider("teams", 20, "teams-v1")
    registry.register(jobs)
    registry.register(teams)

    registry.render()
    jobs.value = "jobs-v2"
    assert registry.render() == "jobs-v2\n\nteams-v1"

    assert jobs.render_count == 2
    assert teams.render_count == 1
    assert registry.stats().rebuild_reasons == {
        "new_provider": 2,
        "signature_changed": 1,
    }


def test_sections_sort_by_priority_then_name() -> None:
    registry = PromptStateRegistry()
    registry.register(MutableProvider("zeta", 20, "zeta"))
    registry.register(MutableProvider("beta", 10, "beta"))
    registry.register(MutableProvider("alpha", 10, "alpha"))

    assert registry.render() == "alpha\n\nbeta\n\nzeta"


def test_empty_state_is_not_injected() -> None:
    registry = PromptStateRegistry()
    registry.register(MutableProvider("empty", 10, ""))
    registry.register(MutableProvider("present", 20, "present"))

    assert registry.render() == "present"


def test_volatile_provider_does_not_change_stable_prompt() -> None:
    registry = PromptStateRegistry()
    stable = MutableProvider("stable", 10, "stable")
    volatile = MutableProvider("clock", 5, "12:00", volatile=True)
    registry.register(stable)
    registry.register(volatile)

    assert registry.render() == "stable"
    volatile.value = "12:01"
    assert registry.render() == "stable"
    assert stable.render_count == 1
    assert volatile.render_count == 0
    assert registry.render_volatile() == "12:01"


def test_job_progress_does_not_rebuild_stable_section(tmp_path) -> None:
    from braincode.jobs import JobKind, JobManager, JobSpec

    manager = JobManager.for_project(tmp_path)
    job = manager.create(JobSpec(kind=JobKind.TOOL, name="build"))
    registry = PromptStateRegistry()
    registry.register(JobPromptStateProvider(manager))

    first = registry.render()
    manager.update_progress(job.id, {"percent": 50, "message": "halfway"})
    second = registry.render()

    assert second == first
    assert registry.stats().cache_hits == 1
    assert registry.stats().cache_misses == 1


def test_text_provider_updates_by_signature() -> None:
    registry = PromptStateRegistry()
    provider = TextPromptStateProvider("skills", 10, "Skills Directory")
    registry.register(provider)

    assert registry.render() == ""
    provider.set_content("- pdf: PDF support")
    assert registry.render() == "# Skills Directory\n\n- pdf: PDF support"


def test_coordinator_prompt_keeps_core_and_appends_state() -> None:
    prompt = build_system_prompt(
        coordinator_mode=True,
        prompt_state="# Runtime State\n\nactive",
    )

    assert "orchestrates software engineering tasks" in prompt
    assert prompt.endswith("# Runtime State\n\nactive")


@pytest.mark.asyncio
async def test_agent_uses_registry_without_rebuilding_unchanged_state() -> None:
    captured_systems: list[str] = []

    class MockClient(LLMClient):
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, conversation, system="", tools=None):
            self.calls += 1
            captured_systems.append(system)
            if self.calls == 1:
                yield ToolCallComplete("t1", "Bash", {"command": "echo ok"})
                yield StreamEnd("tool_use", input_tokens=1, output_tokens=1)
                return
            yield TextDelta("done")
            yield StreamEnd("end_turn", input_tokens=1, output_tokens=1)

    registry = PromptStateRegistry()
    provider = MutableProvider("runtime", 105, "# Runtime\n\nstable")
    registry.register(provider)
    agent = Agent(
        client=MockClient(),
        registry=create_default_registry(),
        protocol="anthropic",
        prompt_state_registry=registry,
    )
    conversation = ConversationManager()
    conversation.add_user_message("hello")

    async for _ in agent.run(conversation):
        pass

    assert len(captured_systems) == 2
    assert captured_systems[0] == captured_systems[1]
    assert "# Runtime\n\nstable" in captured_systems[0]
    assert provider.render_count == 1
