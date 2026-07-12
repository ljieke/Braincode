# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com

from __future__ import annotations

from braincode.hooks.conditions import ConditionParseError, parse_condition
from braincode.hooks.events import LifecycleEvent
from braincode.hooks.models import Action, Hook, HookResult

_VALID_EVENTS = {e.value for e in LifecycleEvent}
_VALID_ACTION_TYPES = {"command", "prompt", "http", "agent"}

_REQUIRED_FIELDS: dict[str, list[str]] = {
    "command": ["command"],
    "prompt": ["message"],
    "http": ["url"],
    "agent": ["prompt"],
}


class HookConfigError(Exception):
    pass


def _identify(entry: dict, index: int) -> str:
    hook_id = entry.get("id", "")
    return f"hook '{hook_id}'" if hook_id else f"hook #{index + 1}"


def load_hooks(raw_hooks: list[dict] | None) -> list[Hook]:
    if not raw_hooks:
        return []

    hooks: list[Hook] = []
    for i, entry in enumerate(raw_hooks):
        label = _identify(entry, i)

        if not isinstance(entry, dict):
            raise HookConfigError(f"{label}: must be a mapping")

        event = entry.get("event")
        if not event:
            raise HookConfigError(f"{label}: missing 'event' field")
        if event not in _VALID_EVENTS:
            raise HookConfigError(
                f"{label}: invalid event '{event}', "
                f"must be one of: {', '.join(sorted(_VALID_EVENTS))}"
            )

        raw_action = entry.get("action")
        if not isinstance(raw_action, dict):
            raise HookConfigError(f"{label}: missing or invalid 'action' field")

        action_type = raw_action.get("type")
        if action_type not in _VALID_ACTION_TYPES:
            raise HookConfigError(
                f"{label}: invalid action type '{action_type}', "
                f"must be one of: {', '.join(sorted(_VALID_ACTION_TYPES))}"
            )

        required = _REQUIRED_FIELDS[action_type]
        for field_name in required:
            if not raw_action.get(field_name):
                raise HookConfigError(
                    f"{label}: action type '{action_type}' requires "
                    f"'{field_name}' field"
                )

        reject = bool(entry.get("reject", False))
        if reject and event != "pre_tool_use":
            raise HookConfigError(
                f"{label}: 'reject' can only be used with 'pre_tool_use' event"
            )

        async_exec = bool(entry.get("async", False))
        if async_exec and reject:
            raise HookConfigError(
                f"{label}: async hooks cannot reject the current invocation"
            )

        condition = None
        raw_if = entry.get("if")
        if raw_if:
            try:
                condition = parse_condition(str(raw_if))
            except ConditionParseError as e:
                raise HookConfigError(f"{label}: condition error: {e}") from e

        hook_id = entry.get("id", f"{event}_{i}")

        timeout = raw_action.get("timeout", 30)
        if not isinstance(timeout, int) or timeout <= 0:
            raise HookConfigError(f"{label}: timeout must be a positive integer")

        action = Action(
            type=action_type,
            command=raw_action.get("command", ""),
            message=raw_action.get("message", ""),
            url=raw_action.get("url", ""),
            method=raw_action.get("method", "POST"),
            body=raw_action.get("body", ""),
            headers=raw_action.get("headers", {}),
            prompt=raw_action.get("prompt", ""),
            timeout=timeout,
        )

        configured_result = None
        raw_result = entry.get("result")
        if raw_result is not None:
            if not isinstance(raw_result, dict):
                raise HookConfigError(f"{label}: 'result' must be a mapping")
            outcome = str(raw_result.get("outcome", "continue"))
            if outcome not in {"continue", "reject", "cancel"}:
                raise HookConfigError(f"{label}: invalid result outcome '{outcome}'")
            updated_args = raw_result.get("updated_args")
            if updated_args is not None and not isinstance(updated_args, dict):
                raise HookConfigError(f"{label}: result.updated_args must be a mapping")
            configured_result = HookResult(
                outcome=outcome,
                message=str(raw_result.get("message", "")),
                reject_reason=str(raw_result.get("reject_reason", "")),
                prevent_continuation=bool(
                    raw_result.get("prevent_continuation", False)
                ),
                updated_args=updated_args,
                updated_output=(
                    str(raw_result["updated_output"])
                    if "updated_output" in raw_result
                    else None
                ),
                additional_context=str(raw_result.get("additional_context", "")),
            )
            if async_exec and (
                configured_result.is_rejected
                or configured_result.prevent_continuation
                or configured_result.updated_args is not None
                or configured_result.updated_output is not None
            ):
                raise HookConfigError(
                    f"{label}: async hooks may only return message/additional_context"
                )

        hooks.append(
            Hook(
                id=hook_id,
                event=event,
                action=action,
                condition=condition,
                reject=reject,
                once=bool(entry.get("once", False)),
                async_exec=async_exec,
                configured_result=configured_result,
            )
        )

    return hooks
