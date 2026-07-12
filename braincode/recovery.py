from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from braincode.client import (
    ContextLimitError,
    LLMClient,
    LLMError,
    NetworkError,
    OverloadedError,
    RateLimitError,
    StreamInterruptedError,
)
from braincode.tools.base import (
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 6
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 32.0
    max_output_continuations: int = 3

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds must not be negative")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")
        if self.max_output_continuations < 0:
            raise ValueError("max_output_continuations must not be negative")


@dataclass(frozen=True)
class RecoveryNotice:
    event_type: str
    reason: str
    attempt: int
    wait: float
    provider_name: str
    previous_provider_name: str = ""


@dataclass
class RecoveryController:
    clients: list[LLMClient]
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    jitter: Callable[[], float] = random.random

    def __post_init__(self) -> None:
        if not self.clients:
            raise ValueError("RecoveryController requires at least one client")
        self._current_provider_index = 0

    @property
    def current_client(self) -> LLMClient:
        return self.clients[self._current_provider_index]

    @property
    def current_provider_name(self) -> str:
        client = self.current_client
        return str(
            getattr(client, "provider_name", "")
            or getattr(client, "model", "")
            or type(client).__name__
        )

    def clone_for(self, primary: LLMClient) -> RecoveryController:
        clients = [primary, *(client for client in self.clients if client is not primary)]
        return RecoveryController(
            clients=clients,
            policy=self.policy,
            sleep=self.sleep,
            jitter=self.jitter,
        )

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return retry_after
        exponential = min(
            self.policy.base_delay_seconds * (2 ** max(0, attempt - 1)),
            self.policy.max_delay_seconds,
        )
        if exponential == 0:
            return 0.0
        return min(
            self.policy.max_delay_seconds,
            exponential + self.jitter() * self.policy.base_delay_seconds,
        )

    async def stream(
        self,
        conversation: Any,
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        state: Any = None,
        context_recover: Callable[[], Awaitable[bool]] | None = None,
    ) -> AsyncIterator[StreamEvent | RecoveryNotice]:
        if state is not None and hasattr(state, "reset_llm_request"):
            state.reset_llm_request(self._current_provider_index)

        attempt = 0
        while True:
            client = self.current_client
            provider_name = self.current_provider_name
            try:
                async for event in client.stream(
                    conversation, system=system, tools=tools
                ):
                    if state is not None:
                        state.stream_started = True
                        state.current_provider_index = self._current_provider_index
                        if isinstance(
                            event,
                            (TextDelta, ThinkingDelta, ToolCallStart, ToolCallDelta),
                        ):
                            state.any_output_emitted = True
                        if isinstance(event, ToolCallComplete):
                            state.any_output_emitted = True
                            state.tool_call_completed = True
                    yield event
                return
            except LLMError as error:
                if not error.provider_name:
                    error.provider_name = provider_name

                any_output = bool(
                    state is not None and state.any_output_emitted
                )
                tool_completed = bool(
                    state is not None and state.tool_call_completed
                )
                tool_started = bool(
                    state is not None and state.tool_execution_started
                )
                if any_output or tool_completed or tool_started:
                    raise StreamInterruptedError(
                        f"Model stream interrupted after output began: {error}",
                        provider_name=provider_name,
                        any_output_emitted=any_output,
                        tool_call_completed=tool_completed,
                    ) from error

                if (
                    isinstance(error, ContextLimitError)
                    and context_recover is not None
                    and not bool(
                        state is not None and state.context_recovery_attempted
                    )
                ):
                    if state is not None:
                        state.context_recovery_attempted = True
                    if await context_recover():
                        attempt += 1
                        if state is not None:
                            state.attempt = attempt
                        notice = RecoveryNotice(
                            event_type="context_recovered",
                            reason="context limit: compacted conversation and retrying",
                            attempt=attempt,
                            wait=0.0,
                            provider_name=provider_name,
                        )
                        log.info("LLM recovery: %s", notice.reason)
                        yield notice
                        continue
                    raise

                if not error.retryable or attempt >= self.policy.max_retries:
                    raise

                attempt += 1
                if state is not None:
                    state.attempt = attempt
                    if isinstance(error, RateLimitError):
                        state.rate_limit_attempts += 1
                    elif isinstance(error, OverloadedError):
                        state.overload_attempts += 1
                    elif isinstance(error, NetworkError):
                        state.network_attempts += 1

                if (
                    isinstance(error, OverloadedError)
                    and self._current_provider_index + 1 < len(self.clients)
                ):
                    previous = provider_name
                    self._current_provider_index += 1
                    if state is not None:
                        state.current_provider_index = self._current_provider_index
                    notice = RecoveryNotice(
                        event_type="provider_switched",
                        reason=f"provider overloaded; switched from {previous}",
                        attempt=attempt,
                        wait=0.0,
                        provider_name=self.current_provider_name,
                        previous_provider_name=previous,
                    )
                    log.warning(
                        "LLM provider switched from %s to %s",
                        previous,
                        notice.provider_name,
                    )
                    yield notice
                    continue

                delay = self._delay(attempt, error.retry_after)
                notice = RecoveryNotice(
                    event_type="retry_started",
                    reason=str(error),
                    attempt=attempt,
                    wait=delay,
                    provider_name=provider_name,
                )
                log.warning(
                    "LLM retry %s/%s on %s in %.3fs: %s",
                    attempt,
                    self.policy.max_retries,
                    provider_name,
                    delay,
                    error,
                )
                yield notice
                if delay > 0:
                    await self.sleep(delay)


async def stream_with_recovery(
    client: LLMClient,
    conversation: Any,
    *,
    system: str = "",
    tools: list[dict[str, Any]] | None = None,
) -> AsyncIterator[StreamEvent]:
    controller = RecoveryController([client])
    async for event in controller.stream(
        conversation, system=system, tools=tools
    ):
        if isinstance(event, RecoveryNotice):
            continue
        yield event


def build_recovery_controller(
    primary_client: LLMClient,
    providers: list[Any],
    config: Any,
) -> RecoveryController:
    from braincode.client import create_client

    clients = [primary_client]
    by_name = {provider.name: provider for provider in providers}
    primary_name = getattr(primary_client, "provider_name", "")
    for name in config.fallback_providers:
        provider = by_name.get(name)
        if provider is None or name == primary_name:
            continue
        try:
            clients.append(create_client(provider))
        except LLMError as error:
            log.warning("Skipping fallback provider %s: %s", name, error)
    return RecoveryController(
        clients,
        policy=RetryPolicy(
            max_retries=config.max_retries,
            base_delay_seconds=config.base_delay_seconds,
            max_delay_seconds=config.max_delay_seconds,
            max_output_continuations=config.max_output_continuations,
        ),
    )
