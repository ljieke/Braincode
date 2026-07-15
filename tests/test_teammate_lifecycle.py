from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from braincode.teams.lifecycle import (
    LifecycleTransitionError,
    TeammateState,
)
from braincode.teams.mailbox import Mailbox, create_message
from braincode.teams.manager import TeamManager
from braincode.teams.models import BackendType, TeammateInfo
from braincode.teams.spawn_inprocess import spawn_inprocess_teammate


def make_member(
    *,
    agent_id: str = "agent-1",
    lifecycle_state: str | None = TeammateState.CREATED.value,
    is_active: bool | None = None,
) -> TeammateInfo:
    return TeammateInfo(
        name="worker",
        agent_id=agent_id,
        agent_type="worker",
        model="test-model",
        worktree_path="/tmp/worktree",
        backend_type=BackendType.IN_PROCESS.value,
        is_active=is_active,
        lifecycle_state=lifecycle_state,
    )


async def wait_until(predicate, timeout: float = 1.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout=timeout)


class RecordingAgent:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def run_to_completion(
        self,
        prompt: str,
        conversation,
        event_callback=None,
    ) -> str:
        self.prompts.append(prompt)
        if event_callback is not None:
            event_callback({
                "type": "usage",
                "usage": {"inputTokens": 10, "outputTokens": 2},
            })
        await asyncio.sleep(0)
        return f"result-{len(self.prompts)}"


class FailingAgent:
    async def run_to_completion(
        self,
        prompt: str,
        conversation,
        event_callback=None,
    ) -> str:
        raise RuntimeError("agent failed")


def test_lifecycle_accepts_only_defined_transitions() -> None:
    member = make_member()

    assert member.state == TeammateState.CREATED
    assert member.transition_to(TeammateState.RUNNING) is True
    assert member.transition_to(TeammateState.IDLE) is True
    assert member.transition_to(TeammateState.RUNNING) is True
    assert member.transition_to(TeammateState.STOPPING) is True
    assert member.transition_to(TeammateState.STOPPED) is True
    assert member.transition_to(TeammateState.STOPPED) is False

    with pytest.raises(
        LifecycleTransitionError,
        match="stopped -> running",
    ):
        member.transition_to(TeammateState.RUNNING)


def test_legacy_is_active_values_migrate_to_lifecycle_states() -> None:
    running = make_member(lifecycle_state=None, is_active=True)
    idle = make_member(lifecycle_state=None, is_active=False)
    created = make_member(lifecycle_state=None, is_active=None)

    assert running.state == TeammateState.RUNNING
    assert idle.state == TeammateState.IDLE
    assert created.state == TeammateState.CREATED


def test_team_manager_persists_each_transition(tmp_path: Path) -> None:
    with patch("braincode.teams.models.Path.home", return_value=tmp_path):
        manager = TeamManager()
        team = manager.create_team("lifecycle", "lead-1")
        manager.register_member(team.name, make_member())

        manager.transition_member(
            team.name,
            "agent-1",
            TeammateState.RUNNING,
            "test started",
        )
        manager.transition_member(
            team.name,
            "agent-1",
            TeammateState.IDLE,
            "test completed",
        )

        restored = type(team).load(team.config_path)
        assert restored.get_member("agent-1").state == TeammateState.IDLE
        assert restored.get_member("agent-1").is_active is False


def test_inprocess_teammate_runs_follow_up_and_stops_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(
            "braincode.teams.spawn_inprocess.IDLE_POLL_INTERVAL", 0.01
        )
        mailbox = Mailbox(tmp_path / "mailbox")
        member = make_member()
        agent = RecordingAgent()
        observed: list[TeammateState] = []

        def record_transition(state: TeammateState, reason: str) -> None:
            member.transition_to(state)
            observed.append(state)

        handle = spawn_inprocess_teammate(
            agent=agent,
            prompt="initial task",
            name=member.name,
            member=member,
            team_name="lifecycle",
            mailbox=mailbox,
            mailbox_key=member.agent_id,
            lead_agent_id="lead-1",
            on_state_change=record_transition,
        )

        await wait_until(lambda: member.state == TeammateState.IDLE)
        mailbox.write(
            member.agent_id,
            create_message(
                "lead-1",
                member.agent_id,
                "follow-up task",
                summary="follow up",
            ),
        )
        await wait_until(
            lambda: len(agent.prompts) == 2
            and member.state == TeammateState.IDLE
        )

        mailbox.write(
            member.agent_id,
            create_message(
                "lead-1",
                member.agent_id,
                "stop now",
                summary="shutdown",
                message_type="shutdown_request",
            ),
        )
        result = await asyncio.wait_for(handle.task, timeout=1.0)

        assert result == "result-2"
        assert member.state == TeammateState.STOPPED
        assert agent.prompts[0] == "initial task"
        assert "follow-up task" in agent.prompts[1]
        assert observed == [
            TeammateState.RUNNING,
            TeammateState.IDLE,
            TeammateState.RUNNING,
            TeammateState.IDLE,
            TeammateState.STOPPING,
            TeammateState.STOPPED,
        ]
        assert len(mailbox.consume("lead-1")) == 2

    asyncio.run(scenario())


def test_runtime_shutdown_persists_stopped_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(
            "braincode.teams.spawn_inprocess.IDLE_POLL_INTERVAL", 0.01
        )
        with patch("braincode.teams.models.Path.home", return_value=tmp_path):
            manager = TeamManager()
            team = manager.create_team("shutdown", "lead-1")
            member = make_member()
            manager.register_member(team.name, member)
            mailbox = manager.get_mailbox(team.name)

            handle = spawn_inprocess_teammate(
                agent=RecordingAgent(),
                prompt="initial task",
                name=member.name,
                member=member,
                team_name=team.name,
                mailbox=mailbox,
                mailbox_key=member.agent_id,
                lead_agent_id=team.lead_agent_id,
                on_state_change=lambda state, reason: manager.transition_member(
                    team.name, member.agent_id, state, reason
                ),
            )
            manager.register_inprocess_handle(member.agent_id, handle)

            await wait_until(lambda: member.state == TeammateState.IDLE)
            await manager.shutdown()

            restored = type(team).load(team.config_path)
            assert restored.get_member(member.agent_id).state == TeammateState.STOPPED
            assert handle.done is True

    asyncio.run(scenario())


def test_shutdown_queued_before_start_skips_initial_task(tmp_path: Path) -> None:
    async def scenario() -> None:
        mailbox = Mailbox(tmp_path / "mailbox")
        member = make_member()
        agent = RecordingAgent()
        mailbox.write(
            member.agent_id,
            create_message(
                "lead-1",
                member.agent_id,
                "stop before start",
                summary="shutdown",
                message_type="shutdown_request",
            ),
        )

        handle = spawn_inprocess_teammate(
            agent=agent,
            prompt="must not run",
            name=member.name,
            member=member,
            team_name="lifecycle",
            mailbox=mailbox,
            mailbox_key=member.agent_id,
            lead_agent_id="lead-1",
        )
        result = await asyncio.wait_for(handle.task, timeout=1.0)

        assert result == ""
        assert agent.prompts == []
        assert member.state == TeammateState.STOPPED

    asyncio.run(scenario())


def test_agent_failure_moves_to_failed_and_notifies_lead(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        mailbox = Mailbox(tmp_path / "mailbox")
        member = make_member()

        handle = spawn_inprocess_teammate(
            agent=FailingAgent(),
            prompt="fail",
            name=member.name,
            member=member,
            team_name="lifecycle",
            mailbox=mailbox,
            mailbox_key=member.agent_id,
            lead_agent_id="lead-1",
            on_state_change=lambda state, reason: member.transition_to(state),
        )

        with pytest.raises(RuntimeError, match="agent failed"):
            await handle.task

        assert member.state == TeammateState.FAILED
        messages = mailbox.consume("lead-1")
        assert len(messages) == 1
        assert "agent failed" in messages[0].content

    asyncio.run(scenario())
