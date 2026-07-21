from __future__ import annotations

from typing import Any, AsyncIterator

import pytest
from pydantic import BaseModel

from braincode.agent import Agent, ToolResultEvent
from braincode.client import LLMClient
from braincode.conversation import ConversationManager
from braincode.tools import ToolRegistry
from braincode.tools.base import StreamEnd, StreamEvent, Tool, ToolCallComplete, ToolResult


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
