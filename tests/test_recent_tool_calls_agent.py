from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest
from pydantic import BaseModel

from braincode.agent import Agent, PermissionRequest, PermissionResponse, ToolResultEvent
from braincode.client import LLMClient
from braincode.conversation import ConversationManager
from braincode.permissions import Decision, PermissionMode
from braincode.tools import ToolRegistry
from braincode.tools.base import (
    RepeatPolicy,
    StreamEnd,
    StreamEvent,
    Tool,
    ToolCallComplete,
    ToolResult,
)


class _Params(BaseModel):
    value: str


class _CountingTool(Tool):
    name = "Count"
    description = "count calls"
    params_model = _Params
    is_concurrency_safe = True

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, params: _Params) -> ToolResult:
        self.calls += 1
        return ToolResult(output=f"result:{params.value}")


class _GuardedCountingTool(_CountingTool):
    repeat_policy = RepeatPolicy.GUARD


class _WarnCountingTool(_CountingTool):
    repeat_policy = RepeatPolicy.WARN


class _Client(LLMClient):
    def __init__(self) -> None:
        self.responses: list[list[StreamEvent]] = [
            [ToolCallComplete("one", "Count", {"value": "x"}), StreamEnd("end_turn")],
            [ToolCallComplete("two", "Count", {"value": "x"}), StreamEnd("end_turn")],
            [ToolCallComplete("three", "Count", {"value": "x"}), StreamEnd("end_turn")],
            [StreamEnd("end_turn")],
        ]

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        for event in self.responses.pop(0):
            yield event
            await asyncio.sleep(0)


class _EmptyClient(LLMClient):
    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEnd("end_turn")


class _BatchedGuardClient(LLMClient):
    def __init__(self) -> None:
        self.responses: list[list[StreamEvent]] = [
            [ToolCallComplete("one", "Count", {"value": "x"}), StreamEnd("end_turn")],
            [ToolCallComplete("two", "Count", {"value": "x"}), StreamEnd("end_turn")],
            [ToolCallComplete("three", "Count", {"value": "x"}), StreamEnd("end_turn")],
            [
                ToolCallComplete("four", "Count", {"value": "x"}),
                ToolCallComplete("five", "Count", {"value": "x"}),
                StreamEnd("end_turn"),
            ],
            [ToolCallComplete("six", "Count", {"value": "x"}), StreamEnd("end_turn")],
            [StreamEnd("end_turn")],
        ]

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        for event in self.responses.pop(0):
            yield event
            await asyncio.sleep(0)


class _CompletionGuardClient(LLMClient):
    def __init__(self) -> None:
        self.responses: list[list[StreamEvent]] = [
            [ToolCallComplete("one", "Count", {"value": "x"}), StreamEnd("end_turn")],
            [ToolCallComplete("two", "Count", {"value": "x"}), StreamEnd("end_turn")],
            [ToolCallComplete("three", "Count", {"value": "x"}), StreamEnd("end_turn")],
            [ToolCallComplete("four", "Count", {"value": "x"}), StreamEnd("end_turn")],
            [ToolCallComplete("five", "Count", {"value": "x"}), StreamEnd("end_turn")],
            [StreamEnd("end_turn")],
        ]

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        for event in self.responses.pop(0):
            yield event


@pytest.mark.asyncio
async def test_agent_executes_repeats_but_compacts_conversation_output():
    tool = _CountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    runtime_events: list[tuple[str, dict[str, Any]]] = []
    agent = Agent(
        _Client(),
        registry,
        "anthropic",
        runtime_event_sink=lambda event_type, payload: runtime_events.append(
            (event_type, payload)
        ),
    )
    conversation = ConversationManager()
    conversation.add_user_message("repeat")

    events = [event async for event in agent.run(conversation)]
    results = [event for event in events if isinstance(event, ToolResultEvent)]
    tool_messages = [message for message in conversation.history if message.tool_results]

    assert tool.calls == 3
    assert [result.output for result in results] == [
        "result:x",
        "result:x",
        "result:x",
    ]
    assert tool_messages[0].tool_results[0].content == "result:x"
    assert "Repeated tool call" in tool_messages[1].tool_results[0].content
    assert "Tool loop detected" in tool_messages[2].tool_results[0].content
    assert [event_type for event_type, _ in runtime_events] == [
        "tool_repeat_detected",
        "tool_loop_warning",
    ]
    assert runtime_events[-1][1]["policy"] == RepeatPolicy.OBSERVE.value


async def _execute_path(agent: Agent, path: str, call_number: int) -> ToolResult:
    tc = ToolCallComplete(str(call_number), "Count", {"value": "x"})
    if path == "direct":
        return (await agent._execute_single_tool_direct(tc)).result
    if path == "interactive":
        items = [item async for item in agent._execute_tool(tc)]
        assert len(items) == 1
        result, _elapsed, _is_unknown = items[0]
        return result
    return await agent._execute_tool_noninteractive(tc)


@pytest.mark.parametrize("path", ["direct", "interactive", "noninteractive"])
@pytest.mark.asyncio
async def test_guard_preserves_all_three_agent_execution_contracts(path: str):
    tool = _GuardedCountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    runtime_events: list[tuple[str, dict[str, Any]]] = []
    agent = Agent(
        _EmptyClient(),
        registry,
        "anthropic",
        runtime_event_sink=lambda event_type, payload: runtime_events.append(
            (event_type, payload)
        ),
    )
    agent.recent_tool_calls.begin_run()

    results = [await _execute_path(agent, path, index) for index in range(1, 6)]

    assert tool.calls == 4
    assert all(result.output == "result:x" for result in results[:4])
    assert results[-1].is_error is True
    assert "was not executed" in results[-1].output
    event_type, payload = runtime_events[-1]
    assert event_type == "tool_loop_guarded"
    assert payload["agent_id"] == agent.agent_id
    assert payload["tool_name"] == "Count"
    assert len(payload["fingerprint"]) == 64
    assert payload["call_count"] == 5
    assert payload["same_result_count"] == 4
    assert payload["policy"] == RepeatPolicy.GUARD.value


@pytest.mark.asyncio
async def test_warn_policy_executes_and_reports_its_actual_policy():
    tool = _WarnCountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    runtime_events: list[tuple[str, dict[str, Any]]] = []
    agent = Agent(
        _EmptyClient(),
        registry,
        "anthropic",
        runtime_event_sink=lambda event_type, payload: runtime_events.append(
            (event_type, payload)
        ),
    )
    agent.recent_tool_calls.begin_run()

    for index in range(6):
        result = await agent._execute_tool_noninteractive(
            ToolCallComplete(str(index), "Count", {"value": "x"})
        )
        assert "was not executed" not in result.output

    assert tool.calls == 6
    assert runtime_events[-1][0] == "tool_loop_warning"
    assert runtime_events[-1][1]["policy"] == RepeatPolicy.WARN.value


@pytest.mark.asyncio
async def test_guarded_call_skips_post_tool_hooks(monkeypatch: pytest.MonkeyPatch):
    tool = _GuardedCountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    agent = Agent(_EmptyClient(), registry, "anthropic")
    agent.recent_tool_calls.begin_run()
    post_tool_calls = 0

    async def record_post_tool(
        tc: ToolCallComplete, result: ToolResult
    ) -> ToolResult:
        nonlocal post_tool_calls
        post_tool_calls += 1
        return result

    monkeypatch.setattr(agent, "_apply_post_tool_hooks", record_post_tool)

    for index in range(5):
        await agent._execute_tool_noninteractive(
            ToolCallComplete(str(index), "Count", {"value": "x"})
        )

    assert tool.calls == 4
    assert post_tool_calls == 4


@pytest.mark.asyncio
async def test_same_response_calls_do_not_arm_guard_for_each_other():
    tool = _GuardedCountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    agent = Agent(_BatchedGuardClient(), registry, "anthropic")
    conversation = ConversationManager()
    conversation.add_user_message("repeat")

    events = [event async for event in agent.run(conversation)]
    results = [event for event in events if isinstance(event, ToolResultEvent)]

    assert tool.calls == 5
    assert next(result for result in results if result.tool_id == "five").is_error is False
    guarded = next(result for result in results if result.tool_id == "six")
    assert guarded.is_error is True
    assert "was not executed" in guarded.output


@pytest.mark.asyncio
async def test_run_to_completion_applies_guard_and_keeps_tool_result_pairing(tmp_path):
    tool = _GuardedCountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    agent = Agent(
        _CompletionGuardClient(),
        registry,
        "anthropic",
        work_dir=str(tmp_path),
    )
    conversation = ConversationManager()

    await agent.run_to_completion("repeat", conversation)

    tool_messages = [
        message for message in conversation.history if message.tool_results
    ]
    assert tool.calls == 4
    assert len(tool_messages) == 5
    assert "Tool loop guarded" in tool_messages[-1].tool_results[0].content


@pytest.mark.asyncio
async def test_interactive_permission_allow_still_runs_guard_after_confirmation():
    class AskChecker:
        mode = PermissionMode.DEFAULT

        def check(self, tool, arguments):
            return Decision(effect="ask", reason="confirm")

    tool = _GuardedCountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    agent = Agent(
        _EmptyClient(),
        registry,
        "anthropic",
        permission_checker=AskChecker(),
    )
    agent.recent_tool_calls.begin_run()

    permission_requests = 0
    results: list[ToolResult] = []
    for index in range(5):
        stream = agent._execute_tool(
            ToolCallComplete(str(index), "Count", {"value": "x"})
        )
        item = await stream.__anext__()
        assert isinstance(item, PermissionRequest)
        permission_requests += 1
        item.future.set_result(PermissionResponse.ALLOW)
        result, _elapsed, _is_unknown = await stream.__anext__()
        results.append(result)
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

    assert permission_requests == 5
    assert tool.calls == 4
    assert results[-1].is_error is True
    assert "was not executed" in results[-1].output
