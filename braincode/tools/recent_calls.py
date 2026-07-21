from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from os import PathLike
from typing import Any

from braincode.tools.base import RepeatPolicy, ToolResult


log = logging.getLogger(__name__)

RECENT_TOOL_CALL_MAX_ENTRIES = 64
RECENT_TOOL_CALL_COMPACT_AFTER = 2
RECENT_TOOL_CALL_WARN_AFTER = 3
RECENT_TOOL_CALL_GUARD_AFTER = 4


@dataclass
class GuardSnapshot:
    """Per-response guard eligibility, consumed once per fingerprint."""

    eligible: frozenset[str]
    consumed: set[str] = field(default_factory=set)

    def consume(self, fingerprint: str) -> bool:
        if fingerprint not in self.eligible or fingerprint in self.consumed:
            return False
        self.consumed.add(fingerprint)
        return True


@dataclass
class RecentToolCall:
    fingerprint: str
    tool_name: str
    arguments_json: str
    call_count: int = 0
    same_result_count: int = 0
    last_result_hash: str | None = None
    last_tool_use_id: str | None = None
    in_flight: int = 0
    policy: RepeatPolicy = RepeatPolicy.OBSERVE
    guard_armed: bool = False


@dataclass(frozen=True)
class CallContext:
    fingerprint: str
    run_id: int
    call_number: int
    blocked: bool = False
    same_result_count: int = 0
    last_tool_use_id: str | None = None
    policy: RepeatPolicy = RepeatPolicy.OBSERVE


@dataclass(frozen=True)
class RepeatDecision:
    repeated: bool
    same_result_count: int
    compact_output: str | None = None
    warning: str | None = None
    block_next: bool = False
    call_count: int = 0


@dataclass(frozen=True)
class TrackedToolResult:
    result: ToolResult
    conversation_output: str
    repeated: bool
    warning: str | None = None
    guarded: bool = False


def _type_name(value: Any) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _stable_fallback(value: Any, seen: set[int]) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    if isinstance(value, PathLike):
        return {"__path__": str(value)}
    if isinstance(value, Enum):
        return {
            "__enum__": _type_name(value),
            "value": _stable_fallback(value.value, seen),
        }

    object_id = id(value)
    if object_id in seen:
        return {"__cycle__": _type_name(value)}

    if isinstance(value, Mapping):
        seen.add(object_id)
        try:
            pairs = [
                [
                    _stable_fallback(key, seen),
                    _stable_fallback(item, seen),
                ]
                for key, item in value.items()
            ]
            pairs.sort(
                key=lambda pair: json.dumps(
                    pair[0],
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            return {"__mapping__": pairs}
        finally:
            seen.remove(object_id)

    if isinstance(value, (list, tuple)):
        seen.add(object_id)
        try:
            return {
                "__sequence__": _type_name(value),
                "items": [_stable_fallback(item, seen) for item in value],
            }
        finally:
            seen.remove(object_id)

    if isinstance(value, (set, frozenset)):
        seen.add(object_id)
        try:
            items = [_stable_fallback(item, seen) for item in value]
            items.sort(
                key=lambda item: json.dumps(
                    item,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            return {"__set__": _type_name(value), "items": items}
        finally:
            seen.remove(object_id)

    seen.add(object_id)
    try:
        try:
            state = vars(value)
        except (TypeError, AttributeError):
            state = None
        normalized: dict[str, Any] = {"__type__": _type_name(value)}
        if state:
            normalized["state"] = _stable_fallback(state, seen)
        return normalized
    finally:
        seen.remove(object_id)


def canonical_arguments_json(arguments: dict[str, Any]) -> str:
    try:
        return json.dumps(
            arguments,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        log.debug(
            "Tool arguments were not JSON serializable; using stable fallback",
            exc_info=True,
        )

    try:
        normalized = _stable_fallback(arguments, set())
    except Exception:
        log.debug("Failed to normalize tool arguments", exc_info=True)
        normalized = {"__type__": _type_name(arguments)}
    return json.dumps(
        normalized,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def tool_call_fingerprint(tool_name: str, arguments_json: str) -> str:
    return hashlib.sha256(
        f"{tool_name}\0{arguments_json}".encode("utf-8")
    ).hexdigest()


def tool_result_hash(output: str, is_error: bool) -> str:
    return hashlib.sha256(
        f"{int(is_error)}\0{output}".encode("utf-8")
    ).hexdigest()


class RecentToolCallTracker:
    def __init__(
        self,
        max_entries: int = RECENT_TOOL_CALL_MAX_ENTRIES,
        compact_after: int = RECENT_TOOL_CALL_COMPACT_AFTER,
        warn_after: int = RECENT_TOOL_CALL_WARN_AFTER,
        guard_after: int | None = RECENT_TOOL_CALL_GUARD_AFTER,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        if compact_after < 2:
            raise ValueError("compact_after must be at least 2")
        if warn_after < compact_after:
            raise ValueError("warn_after must be at least compact_after")
        if guard_after is not None and guard_after < 1:
            raise ValueError("guard_after must be at least 1 or None")
        self.max_entries = max_entries
        self.compact_after = compact_after
        self.warn_after = warn_after
        self.guard_after = guard_after
        self._run_id = 0
        self._calls: OrderedDict[str, RecentToolCall] = OrderedDict()
        self._active_calls: set[tuple[int, str, int]] = set()

    @property
    def run_id(self) -> int:
        return self._run_id

    @property
    def in_flight(self) -> int:
        return sum(call.in_flight for call in self._calls.values())

    def __len__(self) -> int:
        return len(self._calls)

    def begin_run(self) -> None:
        self._run_id += 1
        self._calls.clear()
        self._active_calls.clear()

    def get(self, fingerprint: str) -> RecentToolCall | None:
        entry = self._calls.get(fingerprint)
        if entry is not None:
            self._calls.move_to_end(fingerprint)
        return entry

    def guard_snapshot(self) -> GuardSnapshot:
        """Capture guards armed before a batch starts.

        A completion inside one model response must not newly block a sibling
        call from that same response.
        """
        return GuardSnapshot(
            frozenset(
                fingerprint
                for fingerprint, entry in self._calls.items()
                if entry.guard_armed
            )
        )

    def before_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        policy: RepeatPolicy,
        guard_snapshot: GuardSnapshot | None = None,
    ) -> CallContext:
        arguments_json = canonical_arguments_json(arguments)
        fingerprint = tool_call_fingerprint(tool_name, arguments_json)
        policy = RepeatPolicy(policy)
        entry = self._calls.get(fingerprint)
        if entry is None:
            self._make_room()
            entry = RecentToolCall(
                fingerprint=fingerprint,
                tool_name=tool_name,
                arguments_json=arguments_json,
                policy=policy,
            )
            self._calls[fingerprint] = entry
        else:
            entry.policy = policy
            self._calls.move_to_end(fingerprint)

        entry.call_count += 1
        if policy != RepeatPolicy.GUARD or not entry.guard_armed:
            guard_visible = False
        elif guard_snapshot is None:
            guard_visible = True
        elif isinstance(guard_snapshot, GuardSnapshot):
            guard_visible = guard_snapshot.consume(fingerprint)
        else:
            # Accept set-like snapshots from older integrations.
            guard_visible = fingerprint in guard_snapshot
        blocked = (
            policy == RepeatPolicy.GUARD
            and entry.guard_armed
            and guard_visible
        )
        entry.in_flight += 1
        context = CallContext(
            fingerprint=fingerprint,
            run_id=self._run_id,
            call_number=entry.call_count,
            blocked=blocked,
            same_result_count=entry.same_result_count,
            last_tool_use_id=entry.last_tool_use_id,
            policy=policy,
        )
        if blocked:
            entry.in_flight -= 1
            entry.guard_armed = False
            self._trim_idle()
            return context
        self._active_calls.add(
            (context.run_id, context.fingerprint, context.call_number)
        )
        return context

    def after_call(
        self,
        context: CallContext,
        tool_use_id: str,
        output: str,
        is_error: bool,
    ) -> RepeatDecision:
        if context.blocked:
            return RepeatDecision(
                repeated=context.same_result_count > 1,
                same_result_count=context.same_result_count,
                call_count=context.call_number,
            )
        active_key = (context.run_id, context.fingerprint, context.call_number)
        if context.run_id != self._run_id or active_key not in self._active_calls:
            return RepeatDecision(repeated=False, same_result_count=0)

        self._active_calls.remove(active_key)
        entry = self._calls.get(context.fingerprint)
        if entry is None:
            return RepeatDecision(repeated=False, same_result_count=0)

        entry.in_flight = max(0, entry.in_flight - 1)
        previous_tool_use_id = entry.last_tool_use_id
        result_hash = tool_result_hash(output, is_error)
        if entry.last_result_hash == result_hash:
            entry.same_result_count += 1
        else:
            entry.same_result_count = 1

        repeated = entry.same_result_count > 1
        compact_output: str | None = None
        if (
            entry.same_result_count >= self.compact_after
            and entry.same_result_count < self.warn_after
        ):
            reference = previous_tool_use_id or "unknown"
            compact_output = (
                "Repeated tool call: result unchanged from tool use "
                f"`{reference}`.\n"
                "Do not repeat this call unless the underlying state has changed."
            )

        warning: str | None = None
        if entry.same_result_count >= self.warn_after:
            warning = (
                "Tool loop detected: this exact call has produced the same result "
                f"{entry.same_result_count} times.\n"
                "Change strategy, inspect a different signal, or explain the blocker "
                "instead of repeating it."
            )

        entry.last_result_hash = result_hash
        entry.last_tool_use_id = tool_use_id
        entry.guard_armed = (
            context.policy == RepeatPolicy.GUARD
            and self.guard_after is not None
            and entry.same_result_count >= self.guard_after
        )
        self._calls.move_to_end(context.fingerprint)
        self._trim_idle()
        return RepeatDecision(
            repeated=repeated,
            same_result_count=entry.same_result_count,
            call_count=entry.call_count,
            compact_output=compact_output,
            warning=warning,
            block_next=entry.guard_armed,
        )

    def abandon_call(self, context: CallContext) -> None:
        active_key = (context.run_id, context.fingerprint, context.call_number)
        if active_key not in self._active_calls:
            return
        self._active_calls.remove(active_key)
        if context.run_id != self._run_id:
            return
        entry = self._calls.get(context.fingerprint)
        if entry is not None:
            entry.in_flight = max(0, entry.in_flight - 1)
        self._trim_idle()

    def _make_room(self) -> None:
        if len(self._calls) < self.max_entries:
            return
        fingerprint = next(
            (
                candidate
                for candidate, entry in self._calls.items()
                if entry.in_flight == 0
            ),
            None,
        )
        if fingerprint is not None:
            self._calls.pop(fingerprint)

    def _trim_idle(self) -> None:
        while len(self._calls) > self.max_entries:
            fingerprint = next(
                (
                    candidate
                    for candidate, entry in self._calls.items()
                    if entry.in_flight == 0
                ),
                None,
            )
            if fingerprint is None:
                return
            self._calls.pop(fingerprint)
