# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com

"""Hook 系统的测试 —— 涵盖事件、条件、执行器、引擎、加载器以及与 agent 的集成。"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, AsyncIterator
from unittest.mock import patch

import pytest

from braincode.hooks import (
    Action,
    ActionResult,
    Condition,
    ConditionGroup,
    ConditionParseError,
    Hook,
    HookConfigError,
    HookContext,
    HookEngine,
    HookResult,
    LifecycleEvent,
    ToolRejectedError,
    load_hooks,
    parse_condition,
)

# ---------------------------------------------------------------------------
# LifecycleEvent
# ---------------------------------------------------------------------------

class TestLifecycleEvent:
    def test_has_17_events(self):
        assert len(LifecycleEvent) == 17

    def test_string_comparison(self):
        assert LifecycleEvent.SESSION_START == "session_start"
        assert LifecycleEvent.PRE_TOOL_USE == "pre_tool_use"
        assert LifecycleEvent.SHUTDOWN == "shutdown"

    def test_all_values(self):
        expected = {
            "session_start", "session_end",
            "turn_start", "turn_end",
            "pre_tool_use", "post_tool_use",
            "pre_send", "post_receive",
            "startup", "shutdown", "error", "compact",
            "permission_request", "file_change", "command_execute",
            "user_prompt_submit", "stop",
        }
        assert {e.value for e in LifecycleEvent} == expected

# ---------------------------------------------------------------------------
# HookContext
# ---------------------------------------------------------------------------

class TestHookContext:

    def test_get_field_tool(self):
        ctx = HookContext(tool_name="Bash")
        assert ctx.get_field("tool") == "Bash"

    def test_get_field_event(self):
        ctx = HookContext(event_name="pre_tool_use")
        assert ctx.get_field("event") == "pre_tool_use"

    def test_get_field_args(self):
        ctx = HookContext(tool_args={"command": "ls -la", "path": "/tmp"})
        assert ctx.get_field("args.command") == "ls -la"
        assert ctx.get_field("args.path") == "/tmp"

    def test_get_field_unknown(self):
        ctx = HookContext()
        assert ctx.get_field("nonexistent") == ""
        assert ctx.get_field("args.missing") == ""

    def test_expand_all_variables(self):
        ctx = HookContext(
            event_name="post_tool_use",
            tool_name="WriteFile",
            tool_args={"file_path": "src/main.py"},
            file_path="src/main.py",
            message="done",
            error="",
            tool_output="written",
        )
        template = "Event=$EVENT Tool=$TOOL_NAME File=$FILE_PATH Msg=$MESSAGE Err=$ERROR Out=$TOOL_OUTPUT Arg=$TOOL_ARGS.file_path"
        result = ctx.expand(template)
        assert "Event=post_tool_use" in result
        assert "Tool=WriteFile" in result
        assert "File=src/main.py" in result
        assert "Msg=done" in result
        assert "Err=" in result
        assert "Out=written" in result
        assert "Arg=src/main.py" in result

    def test_expand_undefined_variable(self):
        ctx = HookContext()
        assert ctx.expand("hello $UNKNOWN world") == "hello $UNKNOWN world"
        assert ctx.expand("$FILE_PATH") == ""

# ---------------------------------------------------------------------------
# 条件解析
# ---------------------------------------------------------------------------

class TestParseCondition:
    def test_single_condition(self):
        group = parse_condition('tool == "Bash"')
        assert group is not None
        assert len(group.conditions) == 1
        assert group.conditions[0].field == "tool"
        assert group.conditions[0].operator == "=="
        assert group.conditions[0].value == "Bash"
        assert group.logic == "and"

    def test_and_combination(self):
        group = parse_condition('tool == "Bash" && args.command =~ /rm/')
        assert group is not None
        assert len(group.conditions) == 2
        assert group.logic == "and"

    def test_or_combination(self):
        group = parse_condition('tool == "Bash" || tool == "WriteFile"')
        assert group is not None
        assert len(group.conditions) == 2
        assert group.logic == "or"

    def test_mixed_operators_error(self):
        with pytest.raises(ConditionParseError, match="Cannot mix"):
            parse_condition('tool == "Bash" && args.x == "1" || args.y == "2"')

    def test_empty_condition(self):
        assert parse_condition("") is None
        assert parse_condition("   ") is None

    def test_regex_format(self):
        group = parse_condition('args.command =~ /rm\\s+-rf/')
        assert group is not None
        c = group.conditions[0]
        assert c.operator == "=~"
        assert c.value == "/rm\\s+-rf/"

    def test_no_valid_operator(self):
        with pytest.raises(ConditionParseError, match="No valid operator"):
            parse_condition("tool Bash")

# ---------------------------------------------------------------------------
# 条件求值
# ---------------------------------------------------------------------------

class TestConditionEvaluate:
    def test_eq(self):
        ctx = HookContext(tool_name="Bash")
        c = Condition(field="tool", operator="==", value="Bash")
        assert c.evaluate(ctx) is True
        c2 = Condition(field="tool", operator="==", value="WriteFile")
        assert c2.evaluate(ctx) is False

    def test_neq(self):
        ctx = HookContext(tool_name="Bash")
        c = Condition(field="tool", operator="!=", value="ReadFile")
        assert c.evaluate(ctx) is True
        c2 = Condition(field="tool", operator="!=", value="Bash")
        assert c2.evaluate(ctx) is False

    def test_regex(self):
        ctx = HookContext(tool_args={"command": "rm  -rf /"})
        c = Condition(field="args.command", operator="=~", value="/rm\\s+-rf/")
        assert c.evaluate(ctx) is True

    def test_glob(self):
        ctx = HookContext(tool_args={"path": "src/main.py"})
        c = Condition(field="args.path", operator="~=", value="*.py")
        assert c.evaluate(ctx) is True
        c2 = Condition(field="args.path", operator="~=", value="*.go")
        assert c2.evaluate(ctx) is False

class TestConditionGroupEvaluate:
    def test_and_all_pass(self):
        ctx = HookContext(tool_name="WriteFile", tool_args={"path": "src/app.py"})
        group = ConditionGroup(
            conditions=[
                Condition("tool", "==", "WriteFile"),
                Condition("args.path", "~=", "*.py"),
            ],
            logic="and",
        )
        assert group.evaluate(ctx) is True

    def test_and_partial_fail(self):
        ctx = HookContext(tool_name="WriteFile", tool_args={"path": "src/app.go"})
        group = ConditionGroup(
            conditions=[
                Condition("tool", "==", "WriteFile"),
                Condition("args.path", "~=", "*.py"),
            ],
            logic="and",
        )
        assert group.evaluate(ctx) is False

    def test_or_any_pass(self):
        ctx = HookContext(tool_name="Bash")
        group = ConditionGroup(
            conditions=[
                Condition("tool", "==", "Bash"),
                Condition("tool", "==", "WriteFile"),
            ],
            logic="or",
        )
        assert group.evaluate(ctx) is True

    def test_or_all_fail(self):
        ctx = HookContext(tool_name="ReadFile")
        group = ConditionGroup(
            conditions=[
                Condition("tool", "==", "Bash"),
                Condition("tool", "==", "WriteFile"),
            ],
            logic="or",
        )
        assert group.evaluate(ctx) is False

    def test_empty_group(self):
        ctx = HookContext()
        group = ConditionGroup(conditions=[], logic="and")
        assert group.evaluate(ctx) is True

# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------

class TestCommandExecutor:
    @pytest.mark.asyncio
    async def test_normal_execution(self):
        from braincode.hooks.executors import execute_command

        action = Action(type="command", command="echo hello")
        ctx = HookContext()
        result = await execute_command(action, ctx)
        assert result.success is True
        assert "hello" in result.output

    @pytest.mark.asyncio
    async def test_variable_substitution(self):
        from braincode.hooks.executors import execute_command

        action = Action(type="command", command="echo $FILE_PATH")
        ctx = HookContext(file_path="src/main.py")
        result = await execute_command(action, ctx)
        assert "src/main.py" in result.output

    @pytest.mark.asyncio
    async def test_timeout(self, python_command: Callable[[str], str]):
        from braincode.hooks.executors import execute_command

        action = Action(
            type="command",
            command=python_command("import time; time.sleep(10)"),
            timeout=1,
        )
        ctx = HookContext()
        result = await execute_command(action, ctx)
        assert result.success is False
        assert "timed out" in result.output

class TestPromptExecutor:
    @pytest.mark.asyncio
    async def test_returns_message(self):
        from braincode.hooks.executors import execute_prompt

        action = Action(type="prompt", message="Hello $TOOL_NAME")
        ctx = HookContext(tool_name="WriteFile")
        result = await execute_prompt(action, ctx)
        assert result.success is True
        assert result.output == "Hello WriteFile"

class TestHttpExecutor:
    @pytest.mark.asyncio
    async def test_mock_request(self):
        from braincode.hooks.executors import execute_http

        action = Action(type="http", url="https://httpbin.org/post", body='{"test": true}')
        ctx = HookContext()
        # 用 mock 避免发起真实的网络请求
        with patch("braincode.hooks.executors.urlopen") as mock_urlopen:
            mock_resp = mock_urlopen.return_value.__enter__.return_value
            mock_resp.status = 200
            mock_resp.read.return_value = b'{"ok": true}'
            result = await execute_http(action, ctx)
            assert result.success is True
            assert "200" in result.output

class TestAgentExecutor:
    @pytest.mark.asyncio
    async def test_stub(self):
        from braincode.hooks.executors import execute_agent

        action = Action(type="agent", prompt="Check $FILE_PATH")
        ctx = HookContext(file_path="test.py")
        result = await execute_agent(action, ctx)
        assert result.success is True
        assert "not yet implemented" in result.output

class TestExecuteAction:
    @pytest.mark.asyncio
    async def test_dispatch(self):
        from braincode.hooks.executors import execute_action

        action = Action(type="command", command="echo dispatch_test")
        ctx = HookContext()
        result = await execute_action(action, ctx)
        assert "dispatch_test" in result.output

    @pytest.mark.asyncio
    async def test_unknown_type(self):
        from braincode.hooks.executors import execute_action

        action = Action(type="unknown")
        ctx = HookContext()
        result = await execute_action(action, ctx)
        assert result.success is False

# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

class TestLoadHooks:
    def test_full_config(self):
        raw = [
            {
                "id": "auto-format",
                "event": "post_tool_use",
                "if": 'tool == "WriteFile"',
                "action": {"type": "command", "command": "echo formatted"},
            }
        ]
        hooks = load_hooks(raw)
        assert len(hooks) == 1
        assert hooks[0].id == "auto-format"
        assert hooks[0].event == "post_tool_use"
        assert hooks[0].condition is not None

    def test_auto_id(self):
        raw = [
            {"event": "session_start", "action": {"type": "prompt", "message": "hello"}}
        ]
        hooks = load_hooks(raw)
        assert hooks[0].id == "session_start_0"

    def test_empty(self):
        assert load_hooks(None) == []
        assert load_hooks([]) == []

    def test_invalid_event(self):
        with pytest.raises(HookConfigError, match="invalid event"):
            load_hooks([{"event": "bad_event", "action": {"type": "command", "command": "x"}}])

    def test_invalid_action_type(self):
        with pytest.raises(HookConfigError, match="invalid action type"):
            load_hooks([{"event": "startup", "action": {"type": "bad"}}])

    def test_reject_on_non_pre_tool_use(self):
        with pytest.raises(HookConfigError, match="reject.*pre_tool_use"):
            load_hooks([{
                "event": "post_tool_use",
                "action": {"type": "command", "command": "x"},
                "reject": True,
            }])

    def test_async_pre_tool_notification_is_allowed(self):
        hooks = load_hooks([{
            "event": "pre_tool_use",
            "action": {"type": "command", "command": "echo observed"},
            "async": True,
        }])
        assert hooks[0].async_exec is True

    def test_async_hook_cannot_modify_current_invocation(self):
        with pytest.raises(HookConfigError, match="async hooks may only"):
            load_hooks([{
                "event": "pre_tool_use",
                "action": {"type": "command", "command": "echo observed"},
                "async": True,
                "result": {"updated_args": {"command": "changed"}},
            }])

    def test_async_hook_cannot_use_legacy_reject(self):
        with pytest.raises(HookConfigError, match="cannot reject"):
            load_hooks([{
                "event": "pre_tool_use",
                "action": {"type": "command", "command": "echo observed"},
                "async": True,
                "reject": True,
            }])

    def test_structured_result_config(self):
        hooks = load_hooks([{
            "event": "post_tool_use",
            "action": {"type": "prompt", "message": "observed"},
            "result": {
                "updated_output": "rewritten $TOOL_OUTPUT",
                "additional_context": "remember $TOOL_NAME",
            },
        }])
        assert hooks[0].configured_result is not None
        assert hooks[0].configured_result.updated_output == "rewritten $TOOL_OUTPUT"

    def test_missing_required_field(self):
        with pytest.raises(HookConfigError, match="requires.*command"):
            load_hooks([{"event": "startup", "action": {"type": "command"}}])

        with pytest.raises(HookConfigError, match="requires.*url"):
            load_hooks([{"event": "startup", "action": {"type": "http"}}])

        with pytest.raises(HookConfigError, match="requires.*message"):
            load_hooks([{"event": "startup", "action": {"type": "prompt"}}])

        with pytest.raises(HookConfigError, match="requires.*prompt"):
            load_hooks([{"event": "startup", "action": {"type": "agent"}}])

# ---------------------------------------------------------------------------
# HookEngine
# ---------------------------------------------------------------------------

class TestHookEngine:

    def _make_hook(self, **kwargs) -> Hook:
        defaults = {
            "id": "test",
            "event": "post_tool_use",
            "action": Action(type="command", command="echo test"),
        }
        defaults.update(kwargs)
        return Hook(**defaults)

    def test_find_matching_hooks(self):
        h1 = self._make_hook(id="h1", event="post_tool_use")
        h2 = self._make_hook(id="h2", event="pre_tool_use")
        engine = HookEngine([h1, h2])
        ctx = HookContext(event_name="post_tool_use")
        matched = engine.find_matching_hooks("post_tool_use", ctx)
        assert len(matched) == 1
        assert matched[0].id == "h1"

    def test_find_with_condition_filter(self):
        h = self._make_hook(
            id="h1",
            event="post_tool_use",
            condition=ConditionGroup(
                conditions=[Condition("tool", "==", "WriteFile")],
                logic="and",
            ),
        )
        engine = HookEngine([h])

        ctx_match = HookContext(event_name="post_tool_use", tool_name="WriteFile")
        assert len(engine.find_matching_hooks("post_tool_use", ctx_match)) == 1

        ctx_no_match = HookContext(event_name="post_tool_use", tool_name="Bash")
        assert len(engine.find_matching_hooks("post_tool_use", ctx_no_match)) == 0

    def test_once_filter(self):
        h = self._make_hook(id="h1", once=True)
        engine = HookEngine([h])
        ctx = HookContext(event_name="post_tool_use")

        assert len(engine.find_matching_hooks("post_tool_use", ctx)) == 1
        h.mark_executed()
        assert len(engine.find_matching_hooks("post_tool_use", ctx)) == 0

    @pytest.mark.asyncio
    async def test_run_pre_tool_hooks_reject(self):
        h = self._make_hook(
            id="block-vendor",
            event="pre_tool_use",
            action=Action(type="command", command="echo rejected"),
            reject=True,
        )
        engine = HookEngine([h])
        ctx = HookContext(event_name="pre_tool_use", tool_name="WriteFile")
        result = await engine.run_pre_tool_hooks(ctx)
        assert result is not None
        assert isinstance(result, ToolRejectedError)
        assert "rejected" in result.reason

    @pytest.mark.asyncio
    async def test_run_pre_tool_hooks_no_reject(self):
        h = self._make_hook(
            id="log-only",
            event="pre_tool_use",
            action=Action(type="command", command="echo ok"),
            reject=False,
        )
        engine = HookEngine([h])
        ctx = HookContext(event_name="pre_tool_use", tool_name="WriteFile")
        result = await engine.run_pre_tool_hooks(ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_prompt_message_collection(self):
        h = self._make_hook(
            id="inject",
            event="session_start",
            action=Action(type="prompt", message="Project info here"),
        )
        engine = HookEngine([h])
        ctx = HookContext(event_name="session_start")
        await engine.run_hooks("session_start", ctx)
        messages = engine.get_prompt_messages()
        assert len(messages) == 1
        assert "Project info" in messages[0]
        assert engine.get_prompt_messages() == []

    @pytest.mark.asyncio
    async def test_error_does_not_raise(self):
        h = self._make_hook(
            id="bad",
            event="post_tool_use",
            action=Action(type="command", command="exit 1"),
        )
        engine = HookEngine([h])
        ctx = HookContext(event_name="post_tool_use")
        await engine.run_hooks("post_tool_use", ctx)

    @pytest.mark.asyncio
    async def test_async_hook_does_not_block(self):
        # 使用短命令替代 sleep 5，避免孤立子进程导致 pytest 退出时挂起
        h = self._make_hook(
            id="slow",
            event="post_tool_use",
            action=Action(type="command", command="echo async_done"),
            async_exec=True,
        )
        engine = HookEngine([h])
        ctx = HookContext(event_name="post_tool_use")
        await engine.run_hooks("post_tool_use", ctx)
        # 给异步任务一点时间完成
        await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_structured_results_merge_in_hook_order(self):
        first = self._make_hook(
            id="first",
            event="pre_tool_use",
            action=Action(type="prompt", message="first"),
            configured_result=HookResult(
                updated_args={"command": "first", "keep": True},
                additional_context="context one",
            ),
        )
        second = self._make_hook(
            id="second",
            event="pre_tool_use",
            action=Action(type="prompt", message="second"),
            configured_result=HookResult(
                updated_args={"command": "second"},
                additional_context="context two",
            ),
        )
        engine = HookEngine([first, second])

        result = await engine.run_hooks(
            "pre_tool_use",
            HookContext(event_name="pre_tool_use", tool_name="Bash"),
        )

        assert result.updated_args == {"command": "second", "keep": True}
        assert result.additional_context == "context one\ncontext two"
        assert engine.drain_additional_contexts() == ["context one", "context two"]

    @pytest.mark.asyncio
    async def test_reject_stops_later_side_effect_hooks(self):
        rejecting = self._make_hook(
            id="rejecting",
            event="pre_tool_use",
            action=Action(type="prompt", message="blocked"),
            configured_result=HookResult(
                outcome="reject", reject_reason="policy"
            ),
        )
        later = self._make_hook(
            id="later",
            event="pre_tool_use",
            action=Action(type="prompt", message="must not run"),
        )
        engine = HookEngine([rejecting, later])

        result = await engine.run_hooks(
            "pre_tool_use", HookContext(event_name="pre_tool_use")
        )

        assert result.is_rejected
        assert result.reject_reason == "policy"
        assert rejecting.executed is True
        assert later.executed is False

    @pytest.mark.asyncio
    async def test_command_output_can_return_structured_result(self):
        hook = self._make_hook(
            event="post_tool_use",
            action=Action(
                type="prompt",
                message='{"updated_output":"sanitized","additional_context":"next"}',
            ),
        )
        engine = HookEngine([hook])

        result = await engine.run_hooks(
            "post_tool_use", HookContext(event_name="post_tool_use")
        )

        assert result.updated_output == "sanitized"
        assert result.additional_context == "next"

# ---------------------------------------------------------------------------
# Agent 循环集成
# ---------------------------------------------------------------------------

class TestAgentHookIntegration:
    """验证 pre_tool_use 拒绝会导致工具调用被跳过。"""

    @pytest.mark.asyncio
    async def test_pre_tool_use_reject_skips_tool(self):
        from braincode.agent import Agent, ToolResultEvent
        from braincode.client import LLMClient
        from braincode.conversation import ConversationManager
        from pydantic import BaseModel
        from braincode.tools import create_default_registry
        from braincode.tools.base import (
            StreamEnd,
            TextDelta,
            Tool,
            ToolCallComplete,
            ToolResult,
        )

        executed = {"value": False}

        class TestToolParams(BaseModel):
            command: str

        class TestTool(Tool):
            name = "HookTestTool"
            description = "A harmless tool used to verify hook rejection."
            params_model = TestToolParams
            category = "command"

            async def execute(self, params: TestToolParams) -> ToolResult:
                executed["value"] = True
                return ToolResult(output=f"executed: {params.command}")

        class MockClient(LLMClient):
            def __init__(self):
                self._call = 0

            async def stream(self, conversation, system="", tools=None):
                self._call += 1
                if self._call == 1:
                    yield ToolCallComplete(
                        tool_id="t1",
                        tool_name="HookTestTool",
                        arguments={"command": "blocked operation"},
                    )
                    yield StreamEnd(stop_reason="tool_use", input_tokens=10, output_tokens=5)
                else:
                    yield TextDelta(text="I understand, I won't do that.")
                    yield StreamEnd(stop_reason="end_turn", input_tokens=10, output_tokens=5)

        hook = Hook(
            id="block-rm",
            event="pre_tool_use",
            action=Action(type="command", command="echo dangerous command blocked"),
            condition=parse_condition(
                'tool == "HookTestTool" && args.command =~ /blocked/'
            ),
            reject=True,
        )
        engine = HookEngine([hook])

        client = MockClient()
        registry = create_default_registry()
        registry.register(TestTool())
        conv = ConversationManager()
        conv.add_user_message("delete everything")

        agent = Agent(
            client=client,
            registry=registry,
            protocol="anthropic",
            hook_engine=engine,
        )

        events = []
        async for event in agent.run(conv):
            events.append(event)

        tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
        assert len(tool_results) >= 1
        rejected = tool_results[0]
        assert rejected.is_error is True
        assert "Hook rejected" in rejected.output
        assert executed["value"] is False

    @pytest.mark.asyncio
    async def test_post_hook_rewrites_output_and_injects_next_context(self):
        from braincode.agent import Agent
        from braincode.client import LLMClient
        from braincode.conversation import ConversationManager
        from braincode.tools import create_default_registry
        from braincode.tools.base import StreamEnd, TextDelta, ToolCallComplete

        observed = {"output": False, "context": False}

        class MockClient(LLMClient):
            def __init__(self):
                self._call = 0

            async def stream(self, conversation, system="", tools=None):
                self._call += 1
                if self._call == 1:
                    yield ToolCallComplete(
                        tool_id="t1",
                        tool_name="Bash",
                        arguments={"command": "echo raw"},
                    )
                    yield StreamEnd("tool_use", input_tokens=1, output_tokens=1)
                    return
                messages = conversation.get_messages()
                observed["output"] = any(
                    result.content == "sanitized"
                    for message in messages
                    for result in message.tool_results
                )
                observed["context"] = any(
                    "post hook context" in message.content for message in messages
                )
                yield TextDelta("done")
                yield StreamEnd("end_turn", input_tokens=1, output_tokens=1)

        hook = Hook(
            id="sanitize",
            event="post_tool_use",
            action=Action(type="prompt", message="observed"),
            configured_result=HookResult(
                updated_output="sanitized",
                additional_context="post hook context",
            ),
        )
        conversation = ConversationManager()
        conversation.add_user_message("run it")
        agent = Agent(
            client=MockClient(),
            registry=create_default_registry(),
            protocol="anthropic",
            hook_engine=HookEngine([hook]),
        )

        async for _ in agent.run(conversation):
            pass

        assert observed == {"output": True, "context": True}

    @pytest.mark.asyncio
    async def test_modified_args_are_rechecked_by_permission_checker(self):
        from braincode.agent import Agent
        from braincode.client import LLMClient
        from braincode.permissions import Decision, PermissionMode
        from braincode.tools import create_default_registry
        from braincode.tools.base import ToolCallComplete

        class Checker:
            mode = PermissionMode.DEFAULT

            def __init__(self):
                self.seen: list[str] = []

            def check(self, tool, arguments):
                command = str(arguments.get("command", ""))
                self.seen.append(command)
                if command == "blocked-final-command":
                    return Decision(effect="deny", reason="final args denied")
                return Decision(effect="allow", reason="safe")

        class MockClient(LLMClient):
            async def stream(self, conversation, system="", tools=None):
                if False:
                    yield None

        checker = Checker()
        hook = Hook(
            id="mutate",
            event="pre_tool_use",
            action=Action(type="prompt", message="mutating"),
            configured_result=HookResult(
                updated_args={"command": "blocked-final-command"}
            ),
        )
        agent = Agent(
            client=MockClient(),
            registry=create_default_registry(),
            protocol="anthropic",
            permission_checker=checker,
            hook_engine=HookEngine([hook]),
        )

        result = await agent._execute_tool_noninteractive(
            ToolCallComplete("t1", "Bash", {"command": "echo safe"})
        )

        assert result.is_error is True
        assert "final args denied" in result.output
        assert checker.seen[-1] == "blocked-final-command"

    @pytest.mark.asyncio
    async def test_permission_request_hook_can_modify_then_revalidate(self):
        from braincode.agent import Agent
        from braincode.client import LLMClient
        from braincode.permissions import Decision, PermissionMode
        from braincode.tools import create_default_registry
        from braincode.tools.base import ToolCallComplete

        class Checker:
            mode = PermissionMode.DEFAULT

            def __init__(self):
                self.seen: list[str] = []

            def check(self, tool, arguments):
                command = str(arguments.get("command", ""))
                self.seen.append(command)
                if command == "echo approved":
                    return Decision(effect="allow", reason="rewritten safely")
                return Decision(effect="ask", reason="confirmation required")

        class MockClient(LLMClient):
            async def stream(self, conversation, system="", tools=None):
                if False:
                    yield None

        checker = Checker()
        hook = Hook(
            id="approve-safe-form",
            event="permission_request",
            action=Action(type="prompt", message="rewriting"),
            configured_result=HookResult(
                updated_args={"command": "echo approved"}
            ),
        )
        agent = Agent(
            client=MockClient(),
            registry=create_default_registry(),
            protocol="anthropic",
            permission_checker=checker,
            hook_engine=HookEngine([hook]),
        )

        result = await agent._execute_tool_noninteractive(
            ToolCallComplete("t1", "Bash", {"command": "npm install package"})
        )

        assert result.is_error is False
        assert "approved" in result.output
        assert checker.seen == ["npm install package", "echo approved"]

    @pytest.mark.asyncio
    async def test_run_to_completion_has_full_lifecycle_parity(self):
        from braincode.agent import Agent
        from braincode.client import LLMClient
        from braincode.tools import create_default_registry
        from braincode.tools.base import StreamEnd, TextDelta

        class MockClient(LLMClient):
            async def stream(self, conversation, system="", tools=None):
                yield TextDelta("done")
                yield StreamEnd("end_turn", input_tokens=1, output_tokens=1)

        class RecordingHookEngine(HookEngine):
            def __init__(self):
                super().__init__([])
                self.events: list[str] = []

            async def run_hooks(self, event, ctx):
                self.events.append(event)
                if event == "post_receive":
                    return HookResult(updated_output="rewritten")
                return HookResult()

        engine = RecordingHookEngine()
        agent = Agent(
            client=MockClient(),
            registry=create_default_registry(),
            protocol="anthropic",
            hook_engine=engine,
        )

        result = await agent.run_to_completion("finish")

        assert result == "rewritten"
        assert engine.events == [
            "user_prompt_submit",
            "session_start",
            "turn_start",
            "pre_send",
            "post_receive",
            "turn_end",
            "stop",
            "session_end",
        ]
