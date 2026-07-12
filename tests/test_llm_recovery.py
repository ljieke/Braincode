from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from braincode.agent import Agent, RetryEvent, StreamText
from braincode.client import (
    AuthenticationError,
    ContextLimitError,
    LLMClient,
    NetworkError,
    OverloadedError,
    RateLimitError,
    StreamInterruptedError,
)
from braincode.context import RecoveryState
from braincode.conversation import ConversationManager
from braincode.recovery import RecoveryController, RecoveryNotice, RetryPolicy
from braincode.tools import create_default_registry
from braincode.tools.base import StreamEnd, StreamEvent, TextDelta, ToolCallComplete
from braincode.validator import ConfigError, validate_config_structure


class ScriptedClient(LLMClient):
    def __init__(self, name: str, actions: list[object]) -> None:
        self.provider_name = name
        self.actions = list(actions)
        self.calls = 0
        self.max_output_tokens = 0

    def set_max_output_tokens(self, tokens: int) -> None:
        self.max_output_tokens = tokens

    async def stream(
        self, conversation, system="", tools=None
    ) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        for event in action:
            if isinstance(event, Exception):
                raise event
            yield event


def success(text: str = "ok", stop_reason: str = "end_turn") -> list[StreamEvent]:
    return [
        TextDelta(text=text),
        StreamEnd(stop_reason=stop_reason, input_tokens=3, output_tokens=2),
    ]


@pytest.mark.asyncio
async def test_rate_limit_honors_retry_after() -> None:
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = ScriptedClient(
        "primary", [RateLimitError("limited", retry_after=2.5), success()]
    )
    controller = RecoveryController(
        [client], sleep=record_sleep, jitter=lambda: 0.0
    )
    state = RecoveryState()

    events = [
        event
        async for event in controller.stream(
            ConversationManager(), state=state
        )
    ]

    notice = next(event for event in events if isinstance(event, RecoveryNotice))
    assert notice.wait == 2.5
    assert notice.attempt == 1
    assert sleeps == [2.5]
    assert state.rate_limit_attempts == 1
    assert client.calls == 2


@pytest.mark.asyncio
async def test_overload_switches_to_configured_fallback() -> None:
    primary = ScriptedClient("primary", [OverloadedError("overloaded")])
    fallback = ScriptedClient("fallback", [success("fallback result")])
    controller = RecoveryController([primary, fallback])

    events = [
        event
        async for event in controller.stream(ConversationManager())
    ]

    notice = next(event for event in events if isinstance(event, RecoveryNotice))
    assert notice.event_type == "provider_switched"
    assert notice.previous_provider_name == "primary"
    assert notice.provider_name == "fallback"
    assert controller.current_client is fallback
    assert primary.calls == 1
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_network_jitter_retries_then_succeeds() -> None:
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = ScriptedClient(
        "primary", [NetworkError("one"), NetworkError("two"), success()]
    )
    controller = RecoveryController(
        [client],
        policy=RetryPolicy(
            max_retries=3,
            base_delay_seconds=0.25,
            max_delay_seconds=2.0,
        ),
        sleep=record_sleep,
        jitter=lambda: 0.0,
    )
    state = RecoveryState()

    events = [
        event
        async for event in controller.stream(ConversationManager(), state=state)
    ]

    assert len([event for event in events if isinstance(event, RecoveryNotice)]) == 2
    assert sleeps == [0.25, 0.5]
    assert state.network_attempts == 2
    assert client.calls == 3


@pytest.mark.asyncio
async def test_authentication_failure_is_not_retried_or_fallbacked() -> None:
    primary = ScriptedClient("primary", [AuthenticationError("bad key")])
    fallback = ScriptedClient("fallback", [success()])
    controller = RecoveryController([primary, fallback])

    with pytest.raises(AuthenticationError):
        _ = [event async for event in controller.stream(ConversationManager())]

    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_context_limit_compacts_once_then_retries() -> None:
    client = ScriptedClient(
        "primary", [ContextLimitError("context length exceeded"), success()]
    )
    controller = RecoveryController([client])
    recovered = 0

    async def compact() -> bool:
        nonlocal recovered
        recovered += 1
        return True

    events = [
        event
        async for event in controller.stream(
            ConversationManager(), context_recover=compact
        )
    ]

    assert recovered == 1
    assert any(
        isinstance(event, RecoveryNotice)
        and event.event_type == "context_recovered"
        for event in events
    )
    assert client.calls == 2


@pytest.mark.asyncio
async def test_partial_text_is_preserved_and_request_is_not_replayed() -> None:
    client = ScriptedClient(
        "primary", [[TextDelta(text="partial"), NetworkError("disconnect")], success()]
    )
    controller = RecoveryController([client])
    state = RecoveryState()

    with pytest.raises(StreamInterruptedError) as raised:
        _ = [
            event
            async for event in controller.stream(
                ConversationManager(), state=state
            )
        ]

    assert raised.value.any_output_emitted is True
    assert client.calls == 1


@pytest.mark.asyncio
async def test_completed_tool_call_is_never_replayed() -> None:
    client = ScriptedClient(
        "primary",
        [[
            ToolCallComplete(
                tool_id="tool-1", tool_name="ReadFile", arguments={}
            ),
            NetworkError("disconnect"),
        ], success()],
    )
    controller = RecoveryController([client])
    state = RecoveryState()

    with pytest.raises(StreamInterruptedError) as raised:
        _ = [
            event
            async for event in controller.stream(
                ConversationManager(), state=state
            )
        ]

    assert raised.value.tool_call_completed is True
    assert client.calls == 1


@pytest.mark.asyncio
async def test_run_to_completion_has_bounded_max_token_continuations() -> None:
    client = ScriptedClient(
        "primary",
        [
            success("part-1", "max_tokens"),
            success("part-2", "max_tokens"),
            success("part-3", "max_tokens"),
            success("done"),
        ],
    )
    controller = RecoveryController(
        [client],
        policy=RetryPolicy(max_output_continuations=2),
    )
    agent = Agent(
        client=client,
        registry=create_default_registry(),
        protocol="anthropic",
        recovery_controller=controller,
    )

    result = await agent.run_to_completion("continue")

    assert result == "done"
    assert client.calls == 4
    assert client.max_output_tokens == 64_000
    assert agent.recovery_state.output_token_escalated is True
    assert agent.recovery_state.continuation_count == 2


def test_recovery_config_is_validated_at_config_boundary() -> None:
    validated = validate_config_structure(
        {
            "providers": [
                {
                    "name": "primary",
                    "protocol": "anthropic",
                    "base_url": "https://primary.invalid",
                    "model": "model-a",
                },
                {
                    "name": "backup",
                    "protocol": "openai-compat",
                    "base_url": "https://backup.invalid",
                    "model": "model-b",
                },
            ],
            "recovery": {
                "max_retries": 2,
                "base_delay_seconds": 0.1,
                "max_delay_seconds": 1,
                "max_output_continuations": 1,
                "fallback_providers": ["backup"],
            },
        }
    )
    assert validated["recovery"]["fallback_providers"] == ["backup"]
    assert validated["recovery"]["max_retries"] == 2

    with pytest.raises(ConfigError, match="Unknown recovery fallback"):
        validate_config_structure(
            {
                "providers": [
                    {
                        "name": "primary",
                        "protocol": "anthropic",
                        "base_url": "https://primary.invalid",
                        "model": "model-a",
                    }
                ],
                "recovery": {"fallback_providers": ["missing"]},
            }
        )


@pytest.mark.asyncio
async def test_agent_run_emits_observable_retry_event() -> None:
    client = ScriptedClient("primary", [NetworkError("temporary"), success("ok")])
    controller = RecoveryController(
        [client],
        policy=RetryPolicy(base_delay_seconds=0, max_delay_seconds=0),
    )
    agent = Agent(
        client=client,
        registry=create_default_registry(),
        protocol="anthropic",
        recovery_controller=controller,
    )

    events = [event async for event in agent.run(ConversationManager())]

    retry = next(event for event in events if isinstance(event, RetryEvent))
    assert retry.reason == "temporary"
    assert retry.attempt == 1
    assert retry.provider_name == "primary"
    assert any(isinstance(event, StreamText) and event.text == "ok" for event in events)
