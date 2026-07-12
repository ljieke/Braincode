# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from braincode.hooks.conditions import ConditionGroup


@dataclass
class Action:
    type: str
    command: str = ""
    message: str = ""
    url: str = ""
    method: str = "POST"
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    prompt: str = ""
    timeout: int = 30


@dataclass
class ActionResult:
    output: str = ""
    success: bool = True


@dataclass
class Hook:
    id: str
    event: str
    action: Action
    condition: ConditionGroup | None = None
    reject: bool = False
    once: bool = False
    async_exec: bool = False
    executed: bool = False
    configured_result: HookResult | None = None


    def should_run(self) -> bool:
        if self.once and self.executed:
            return False
        return True


    def mark_executed(self) -> None:
        self.executed = True


@dataclass
class HookContext:
    event_name: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    file_path: str = ""
    message: str = ""
    error: str = ""
    tool_output: str = ""

    def get_field(self, name: str) -> str:
        if name == "tool":
            return self.tool_name
        if name == "event":
            return self.event_name
        if name.startswith("args."):
            key = name[5:]
            value = self.tool_args.get(key, "")
            return str(value) if value else ""
        return ""

    def expand(self, template: str) -> str:
        result = template
        result = result.replace("$EVENT", self.event_name)
        result = result.replace("$TOOL_NAME", self.tool_name)
        result = result.replace("$FILE_PATH", self.file_path)
        result = result.replace("$MESSAGE", self.message)
        result = result.replace("$ERROR", self.error)
        result = result.replace("$TOOL_OUTPUT", self.tool_output)
        for key, value in self.tool_args.items():
            result = result.replace(f"$TOOL_ARGS.{key}", str(value))
        return result


@dataclass
class HookResult:
    outcome: str = "continue"
    message: str = ""
    reject_reason: str = ""
    prevent_continuation: bool = False
    updated_args: dict[str, Any] | None = None
    updated_output: str | None = None
    additional_context: str = ""

    @property
    def is_rejected(self) -> bool:
        return self.outcome in {"reject", "cancel"}

    def expanded(self, ctx: HookContext) -> HookResult:
        updated_args = None
        if self.updated_args is not None:
            updated_args = {
                key: ctx.expand(value) if isinstance(value, str) else value
                for key, value in self.updated_args.items()
            }
        return HookResult(
            outcome=self.outcome,
            message=ctx.expand(self.message),
            reject_reason=ctx.expand(self.reject_reason),
            prevent_continuation=self.prevent_continuation,
            updated_args=updated_args,
            updated_output=(
                ctx.expand(self.updated_output)
                if self.updated_output is not None
                else None
            ),
            additional_context=ctx.expand(self.additional_context),
        )


class ToolRejectedError(Exception):
    def __init__(self, tool: str, reason: str, hook_id: str) -> None:
        self.tool = tool
        self.reason = reason
        self.hook_id = hook_id
        super().__init__(f"Tool '{tool}' rejected by hook '{hook_id}': {reason}")
