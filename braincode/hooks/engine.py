from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from braincode.hooks.executors import execute_action
from braincode.hooks.models import Hook, HookContext, HookResult, ToolRejectedError

log = logging.getLogger(__name__)


@dataclass
class HookNotification:
    hook_id: str
    event: str
    output: str
    success: bool


def merge_hook_results(base: HookResult, incoming: HookResult) -> HookResult:
    updated_args = dict(base.updated_args or {})
    if incoming.updated_args:
        updated_args.update(incoming.updated_args)
    messages = [value for value in (base.message, incoming.message) if value]
    contexts = [
        value
        for value in (base.additional_context, incoming.additional_context)
        if value
    ]
    outcome = incoming.outcome if incoming.is_rejected else base.outcome
    reject_reason = incoming.reject_reason or base.reject_reason
    return HookResult(
        outcome=outcome,
        message="\n".join(messages),
        reject_reason=reject_reason,
        prevent_continuation=(
            base.prevent_continuation or incoming.prevent_continuation
        ),
        updated_args=updated_args or None,
        updated_output=(
            incoming.updated_output
            if incoming.updated_output is not None
            else base.updated_output
        ),
        additional_context="\n".join(contexts),
    )


def parse_hook_result(output: str) -> HookResult | None:
    text = output.strip()
    if not text:
        return None
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1])
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    known = {
        "outcome",
        "message",
        "reject_reason",
        "prevent_continuation",
        "updated_args",
        "updated_output",
        "additional_context",
    }
    if not known.intersection(value):
        return None
    outcome = str(value.get("outcome", "continue"))
    if outcome not in {"continue", "reject", "cancel"}:
        outcome = "continue"
    updated_args = value.get("updated_args")
    if not isinstance(updated_args, dict):
        updated_args = None
    return HookResult(
        outcome=outcome,
        message=str(value.get("message", "")),
        reject_reason=str(value.get("reject_reason", "")),
        prevent_continuation=bool(value.get("prevent_continuation", False)),
        updated_args=updated_args,
        updated_output=(
            str(value["updated_output"]) if "updated_output" in value else None
        ),
        additional_context=str(value.get("additional_context", "")),
    )


class HookEngine:
    def __init__(self, hooks: list[Hook] | None = None) -> None:
        self.hooks: list[Hook] = hooks or []
        self._prompt_messages: list[str] = []
        self._notifications: list[HookNotification] = []
        self._additional_contexts: list[str] = []

    def find_matching_hooks(self, event: str, ctx: HookContext) -> list[Hook]:
        matched: list[Hook] = []
        for hook in self.hooks:
            if hook.event != event:
                continue
            if not hook.should_run():
                continue
            if hook.condition is not None and not hook.condition.evaluate(ctx):
                continue
            matched.append(hook)
        return matched

    async def run_hooks(self, event: str, ctx: HookContext) -> HookResult:
        merged = HookResult()
        for hook in self.find_matching_hooks(event, ctx):
            hook.mark_executed()
            if hook.async_exec:
                asyncio.ensure_future(self._run_async(hook, ctx))
                continue
            result = await self._run_single(hook, ctx)
            merged = merge_hook_results(merged, result)
            if result.additional_context:
                self._additional_contexts.append(result.additional_context)
            if result.is_rejected:
                break
        return merged

    async def _run_single(self, hook: Hook, ctx: HookContext) -> HookResult:
        try:
            action_result = await execute_action(hook.action, ctx)
            self._notifications.append(
                HookNotification(
                    hook_id=hook.id,
                    event=hook.event,
                    output=action_result.output,
                    success=action_result.success,
                )
            )
            if not action_result.success:
                log.warning("Hook '%s' action failed: %s", hook.id, action_result.output)

            parsed_result = parse_hook_result(action_result.output)
            result = parsed_result or HookResult()
            if (
                hook.action.type == "prompt"
                and action_result.success
                and parsed_result is None
            ):
                self._prompt_messages.append(action_result.output)
            if hook.configured_result is not None:
                result = merge_hook_results(
                    result, hook.configured_result.expanded(ctx)
                )
            if hook.reject:
                result = merge_hook_results(
                    result,
                    HookResult(
                        outcome="reject",
                        reject_reason=(
                            result.reject_reason or action_result.output or hook.id
                        ),
                    ),
                )
            return result
        except Exception as exc:
            log.warning("Hook '%s' execution error: %s", hook.id, exc)
            self._notifications.append(
                HookNotification(
                    hook_id=hook.id,
                    event=hook.event,
                    output=str(exc),
                    success=False,
                )
            )
            return HookResult()

    async def _run_async(self, hook: Hook, ctx: HookContext) -> None:
        result = await self._run_single(hook, ctx)
        if (
            result.is_rejected
            or result.prevent_continuation
            or result.updated_args is not None
            or result.updated_output is not None
        ):
            log.warning(
                "Async hook '%s' returned synchronous mutations; ignoring them",
                hook.id,
            )
        if result.additional_context:
            self._additional_contexts.append(result.additional_context)

    async def run_pre_tool_hooks(
        self, ctx: HookContext
    ) -> ToolRejectedError | None:
        result = await self.run_hooks("pre_tool_use", ctx)
        if result.is_rejected:
            return ToolRejectedError(
                tool=ctx.tool_name,
                reason=result.reject_reason or result.message or "Hook rejected tool",
                hook_id="structured-hook",
            )
        return None

    def get_prompt_messages(self) -> list[str]:
        messages = list(self._prompt_messages)
        self._prompt_messages.clear()
        return messages

    def drain_additional_contexts(self) -> list[str]:
        contexts = list(self._additional_contexts)
        self._additional_contexts.clear()
        return contexts

    def drain_notifications(self) -> list[HookNotification]:
        notifications = list(self._notifications)
        self._notifications.clear()
        return notifications
