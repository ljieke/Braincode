# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from pydantic import ValidationError

from braincode.client import LLMClient
from braincode.context import (
    CompactBoundary,
    CompactCircuitBreaker,
    CompactEvent,
    ContentReplacementRecord,
    ContentReplacementState,
    RecoveryState,
    append_replacement_records,
    apply_tool_result_budget,
    auto_compact,
    create_replacement_state,
    ensure_session_dir,
    load_replacement_records,
    reconstruct_replacement_state,
)
from braincode.conversation import ConversationManager, ToolResultBlock, ToolUseBlock
from braincode.conversation import ThinkingBlock as ConvThinkingBlock
from braincode.memory.auto_memory import MemoryManager
from braincode.permissions import (
    Decision,
    PermissionChecker,
    PermissionMode,
)
from braincode.hooks import HookContext, HookEngine, HookResult
from braincode.prompts import build_environment_context, build_plan_mode_reminder, build_system_prompt
from braincode.prompt_state import (
    MemoryPromptStateProvider,
    MCPPromptStateProvider,
    PromptStateProvider,
    PromptStateRegistry,
    RecoveryPromptStateProvider,
    SkillsPromptStateProvider,
)
from braincode.recovery import RecoveryController, RecoveryNotice
from braincode.tools import ToolRegistry
from braincode.tools.base import (
    MAX_OUTPUT_CHARS,
    StreamEnd,
    StreamEvent,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
    ToolResult,
)
from braincode.tools.recent_calls import (
    CallContext,
    RecentToolCallTracker,
    RepeatPolicy,
    TrackedToolResult,
)

log = logging.getLogger(__name__)

MEMORY_EXTRACTION_INTERVAL = 1
MAX_TOKENS_CEILING = 64000


# ---------------------------------------------------------------------------
# AgentEvent 事件类型
# ---------------------------------------------------------------------------

@dataclass
class StreamText:
    text: str


@dataclass
class ThinkingText:
    text: str


@dataclass
class RetryEvent:
    reason: str
    wait: float = 0.0
    attempt: int = 0
    provider_name: str = ""
    provider_switched: bool = False


@dataclass
class ToolUseEvent:
    tool_name: str
    tool_id: str
    arguments: dict[str, Any]


@dataclass
class ToolResultEvent:
    tool_id: str
    tool_name: str
    output: str
    is_error: bool
    elapsed: float


@dataclass
class TurnComplete:
    turn: int


@dataclass
class LoopComplete:
    total_turns: int


@dataclass
class UsageEvent:
    input_tokens: int
    output_tokens: int


@dataclass
class ErrorEvent:
    message: str


@dataclass
class CompactNotification:
    before_tokens: int
    message: str
    # 结构化 boundary（摘要 + 原文保留尾部），UI/session 层用它持久化 compact_boundary 记录。
    # 失败路径下为 None。
    boundary: "CompactBoundary | None" = None


@dataclass
class HookEvent:
    hook_id: str
    event: str
    output: str
    success: bool


class PermissionResponse(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ALLOW_ALWAYS = "allow_always"


@dataclass
class PermissionRequest:
    tool_name: str
    description: str
    future: asyncio.Future[PermissionResponse]


AgentEvent = (
    StreamText
    | ThinkingText
    | RetryEvent
    | ToolUseEvent
    | ToolResultEvent
    | TurnComplete
    | LoopComplete
    | UsageEvent
    | ErrorEvent
    | PermissionRequest
    | CompactNotification
    | HookEvent
)


# ---------------------------------------------------------------------------
# LLM 响应收集器
# ---------------------------------------------------------------------------

@dataclass
class ThinkingBlock:
    thinking: str
    signature: str


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCallComplete] = field(default_factory=list)
    thinking_blocks: list[ThinkingBlock] = field(default_factory=list)
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0


class StreamCollector:
    def __init__(self) -> None:
        self.response = LLMResponse()

    async def consume(
        self, stream: AsyncIterator[StreamEvent | RecoveryNotice]
    ) -> AsyncIterator[AgentEvent]:
        async for event in stream:
            if isinstance(event, RecoveryNotice):
                yield RetryEvent(
                    reason=event.reason,
                    wait=event.wait,
                    attempt=event.attempt,
                    provider_name=event.provider_name,
                    provider_switched=event.event_type == "provider_switched",
                )
            elif isinstance(event, TextDelta):
                self.response.text += event.text
                yield StreamText(text=event.text)
            elif isinstance(event, ThinkingDelta):
                yield ThinkingText(text=event.text)
            elif isinstance(event, ThinkingComplete):
                self.response.thinking_blocks.append(
                    ThinkingBlock(thinking=event.thinking, signature=event.signature)
                )
            elif isinstance(event, ToolCallStart):
                pass
            elif isinstance(event, ToolCallDelta):
                pass
            elif isinstance(event, ToolCallComplete):
                self.response.tool_calls.append(event)
                yield ToolUseEvent(
                    tool_name=event.tool_name,
                    tool_id=event.tool_id,
                    arguments=event.arguments,
                )
            elif isinstance(event, StreamEnd):
                self.response.stop_reason = event.stop_reason
                self.response.input_tokens = event.input_tokens
                self.response.output_tokens = event.output_tokens
                self.response.cache_read = event.cache_read
                self.response.cache_creation = event.cache_creation


# ---------------------------------------------------------------------------
# tool 批量执行
# ---------------------------------------------------------------------------

@dataclass
class ToolBatch:
    concurrent: bool
    calls: list[ToolCallComplete]


def partition_tool_calls(
    tool_calls: list[ToolCallComplete],
    registry: ToolRegistry,
) -> list[ToolBatch]:
    batches: list[ToolBatch] = []
    for tc in tool_calls:
        tool = registry.get(tc.tool_name)
        safe = tool is not None and tool.is_concurrency_safe and registry.is_enabled(tc.tool_name)

        if safe and batches and batches[-1].concurrent:
            batches[-1].calls.append(tc)
        else:
            batches.append(ToolBatch(concurrent=safe, calls=[tc]))
    return batches


# ---------------------------------------------------------------------------
# streaming 执行器 — 在 LLM streaming 期间启动 tool 执行
# ---------------------------------------------------------------------------

@dataclass
class _ToolExecResult:
    tool_id: str
    tool_name: str
    result: ToolResult
    elapsed: float
    is_unknown: bool
    conversation_output: str | None = None


class StreamingExecutor:
    def __init__(self) -> None:
        self._tasks: list[tuple[int, asyncio.Task[_ToolExecResult]]] = []
        self._order = 0

    def submit(
        self,
        coro: Any,
    ) -> None:
        task = asyncio.create_task(coro)
        self._tasks.append((self._order, task))
        self._order += 1

    async def collect_results(self) -> list[_ToolExecResult]:
        if not self._tasks:
            return []
        tasks = [t for _, t in sorted(self._tasks, key=lambda x: x[0])]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[_ToolExecResult] = []
        for r in results:
            if isinstance(r, Exception):
                out.append(_ToolExecResult(
                    tool_id="",
                    tool_name="",
                    result=ToolResult(output=f"Tool execution error: {r}", is_error=True),
                    elapsed=0.0,
                    is_unknown=False,
                ))
            else:
                out.append(r)
        return out


# ---------------------------------------------------------------------------
# Agent 主循环
# ---------------------------------------------------------------------------

class Agent:
    def __init__(
        self,
        client: LLMClient,
        registry: ToolRegistry,
        protocol: str,
        work_dir: str = ".",
        max_iterations: int = 0,
        permission_checker: PermissionChecker | None = None,
        context_window: int = 200_000,
        instructions_content: str = "",
        memory_manager: MemoryManager | None = None,
        hook_engine: HookEngine | None = None,
        recovery_controller: RecoveryController | None = None,
        prompt_state_registry: PromptStateRegistry | None = None,
        runtime_event_sink: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.client = client
        self.registry = registry
        self.protocol = protocol
        self.work_dir = work_dir
        self.max_iterations = max_iterations
        self.permission_checker = permission_checker
        self.permission_mode: PermissionMode = (
            permission_checker.mode if permission_checker else PermissionMode.DEFAULT
        )
        self.context_window = context_window
        self.session_dir = ensure_session_dir(work_dir)
        self.compact_breaker = CompactCircuitBreaker()
        self.replacement_state: ContentReplacementState = create_replacement_state()
        # 保存重建工作上下文所需的快照，在 Layer 2 压缩对话后使用：
        # 最近的文件读取和 skill 调用。每次 ReadFile / skill 调用时记录，
        # auto_compact 触发阈值时消费。
        self.recovery_state: RecoveryState = RecoveryState()
        self.recovery_controller = recovery_controller or RecoveryController([client])
        self.prompt_state_registry = prompt_state_registry or PromptStateRegistry()
        self._skills_prompt_provider = SkillsPromptStateProvider()
        self._mcp_prompt_provider = MCPPromptStateProvider()
        self.prompt_state_registry.register(self._skills_prompt_provider)
        self.prompt_state_registry.register(self._mcp_prompt_provider)
        if memory_manager is not None:
            self.prompt_state_registry.register(
                MemoryPromptStateProvider(memory_manager)
            )
        self.prompt_state_registry.register(
            RecoveryPromptStateProvider(self.recovery_controller)
        )
        self.runtime_event_sink = runtime_event_sink
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.instructions_content = instructions_content
        self.memory_manager = memory_manager
        self.hook_engine = hook_engine
        self._loop_count = 0
        # 记忆提取合并策略（对齐 Go 版 inProgress + pendingContext）：
        # _extracting: 标记是否有提取正在进行
        # _pending_extraction: 提取期间又触发了新请求，标记需要尾随提取
        self._extracting = False
        self._pending_extraction = False
        self._consolidator: MemoryConsolidator | None = None
        if memory_manager is not None:
            from braincode.memory.consolidation import MemoryConsolidator
            self._consolidator = MemoryConsolidator(work_dir)
        self.session_id: str = ""
        self.active_skills: dict[str, str] = {}
        self._skill_catalog: str = ""
        self._agent_catalog: str = ""
        self._agent_catalog_list: list[tuple[str, str]] = []
        self.agent_id: str = uuid.uuid4().hex[:12]
        self.parent_id: str | None = None
        self.trace_id: str | None = None
        self.coordinator_mode: bool = False
        self.team_name: str = ""
        self._team_manager: Any = None
        self.notification_fn: Callable[[], list[str]] | None = None
        self.file_history: Any = None
        self._hook_prevent_continuation = False
        self.recent_tool_calls = RecentToolCallTracker()
        # Keep the explicit class-name alias available for integrations and tests.
        self.recent_tool_call_tracker = self.recent_tool_calls

        # 非阻塞 memory recall：prefetch task 与主 LLM 调用并行，工具执行后注入
        self.memory_recall_task: Any | None = None
        self._memory_recall_consumed: bool = False

    @property
    def _transcript_path(self) -> str:
        if self.session_id:
            return str(Path(self.work_dir) / ".braincode" / "sessions" / f"{self.session_id}.jsonl")
        return ""

    @property
    def plan_mode(self) -> bool:
        return self.permission_mode == PermissionMode.PLAN

    _plan_path_cache: Path | None = None

    def _get_plan_path(self) -> Path:
        if self._plan_path_cache is not None:
            return self._plan_path_cache
        import random
        import datetime
        _ADJECTIVES = ["bold", "bright", "calm", "cool", "deep", "fair", "fast", "fine",
                       "glad", "keen", "kind", "lean", "mild", "neat", "pure", "safe",
                       "slim", "soft", "tall", "warm", "wise", "grand", "swift", "vivid"]
        _NOUNS = ["sketch", "draft", "spark", "bloom", "trail", "ridge", "creek", "grove",
                  "cliff", "cloud", "field", "forge", "frost", "haven", "pearl", "stone",
                  "storm", "river", "tower", "delta", "flame", "orbit", "pulse", "shore"]
        plans_dir = Path(self.work_dir) / ".braincode" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%m%d-%H%M")
        slug = f"{random.choice(_ADJECTIVES)}-{random.choice(_NOUNS)}-{ts}"
        self._plan_path_cache = plans_dir / f"{slug}.md"
        return self._plan_path_cache

    def set_permission_mode(self, mode: PermissionMode) -> None:
        self.permission_mode = mode
        if self.permission_checker:
            self.permission_checker.mode = mode

    def activate_skill(self, name: str, prompt_body: str) -> None:
        self.active_skills[name] = prompt_body

    def clear_active_skills(self) -> None:
        self.active_skills.clear()

    def set_skill_catalog(self, catalog: str) -> None:
        self._skill_catalog = catalog
        self._skills_prompt_provider.set_content(catalog)

    def set_mcp_prompt_state(self, content: str) -> None:
        self._mcp_prompt_provider.set_content(content)

    def register_prompt_state_provider(
        self, provider: PromptStateProvider
    ) -> None:
        self.prompt_state_registry.register(provider)

    def _emit_runtime_event(self, event_type: str, **payload: Any) -> None:
        if self.runtime_event_sink is not None:
            self.runtime_event_sink(event_type, payload)


    def set_agent_catalog(self, catalog: str, catalog_list: list[tuple[str, str]] | None = None) -> None:
        self._agent_catalog = catalog
        if catalog_list is not None:
            self._agent_catalog_list = catalog_list

    def _build_hook_context(self, event: str, **kwargs: str | dict) -> HookContext:
        return HookContext(
            event_name=event,
            tool_name=str(kwargs.get("tool_name", "")),
            tool_args=kwargs.get("tool_args", {}),
            file_path=str(kwargs.get("file_path", "")),
            message=str(kwargs.get("message", "")),
            error=str(kwargs.get("error", "")),
            tool_output=str(kwargs.get("tool_output", "")),
        )

    def _infer_file_path(self, args: dict) -> str:
        return str(args.get("file_path", args.get("path", "")))

    def _drain_hook_events(self) -> list[HookEvent]:
        if not self.hook_engine:
            return []
        return [
            HookEvent(
                hook_id=n.hook_id,
                event=n.event,
                output=n.output,
                success=n.success,
            )
            for n in self.hook_engine.drain_notifications()
        ]

    def _inject_hook_contexts(self, conversation: ConversationManager) -> None:
        if self.hook_engine is None:
            return
        for context in self.hook_engine.drain_additional_contexts():
            if context:
                conversation.add_system_reminder(context)

    def _latest_user_message(self, conversation: ConversationManager) -> str:
        for message in reversed(conversation.history):
            if message.role == "user" and not message.tool_results:
                return message.content
        return ""

    async def _run_hook_event(self, event: str, **kwargs: str | dict) -> HookResult:
        if self.hook_engine is None:
            return HookResult()
        result = await self.hook_engine.run_hooks(
            event, self._build_hook_context(event, **kwargs)
        )
        if result.prevent_continuation:
            self._hook_prevent_continuation = True
        return result

    async def _apply_pre_tool_hooks(
        self, tc: ToolCallComplete
    ) -> tuple[ToolCallComplete, ToolResult | None]:
        if self.hook_engine is None:
            return tc, None
        hook_result = await self._run_hook_event(
            "pre_tool_use",
            tool_name=tc.tool_name,
            tool_args=tc.arguments,
            file_path=self._infer_file_path(tc.arguments),
        )
        arguments = dict(tc.arguments)
        if hook_result.updated_args:
            original_arguments = dict(tc.arguments)
            arguments.update(hook_result.updated_args)
            tc = ToolCallComplete(
                tool_id=tc.tool_id,
                tool_name=tc.tool_name,
                arguments=arguments,
            )
            self._emit_runtime_event(
                "hook_modified_input",
                hook_event="pre_tool_use",
                tool_name=tc.tool_name,
                original_args=original_arguments,
                updated_args=hook_result.updated_args,
                final_args=arguments,
            )
        if hook_result.is_rejected:
            reason = (
                hook_result.reject_reason
                or hook_result.message
                or "Hook rejected tool call"
            )
            self._emit_runtime_event(
                "hook_rejected",
                hook_event="pre_tool_use",
                tool_name=tc.tool_name,
                reason=reason,
            )
            return tc, ToolResult(output=f"Hook rejected: {reason}", is_error=True)
        return tc, None

    async def _apply_post_tool_hooks(
        self, tc: ToolCallComplete, result: ToolResult
    ) -> ToolResult:
        if self.hook_engine is None:
            return result
        hook_result = await self._run_hook_event(
            "post_tool_use",
            tool_name=tc.tool_name,
            tool_args=tc.arguments,
            file_path=self._infer_file_path(tc.arguments),
            tool_output=result.output,
            error=result.output if result.is_error else "",
        )
        if hook_result.updated_output is not None:
            result = ToolResult(
                output=hook_result.updated_output,
                is_error=result.is_error,
            )
        if hook_result.is_rejected:
            result = ToolResult(
                output=(
                    hook_result.reject_reason
                    or hook_result.message
                    or "Hook cancelled after tool execution"
                ),
                is_error=True,
            )
        return result

    async def _apply_permission_request_hooks(
        self,
        tool: Any,
        tc: ToolCallComplete,
        decision: Decision,
    ) -> tuple[ToolCallComplete, Decision, ToolResult | None]:
        if decision.effect != "ask" or self.hook_engine is None:
            return tc, decision, None

        hook_result = await self._run_hook_event(
            "permission_request",
            tool_name=tc.tool_name,
            tool_args=tc.arguments,
            file_path=self._infer_file_path(tc.arguments),
        )
        if hook_result.updated_args:
            original_arguments = dict(tc.arguments)
            arguments = dict(tc.arguments)
            arguments.update(hook_result.updated_args)
            tc = ToolCallComplete(
                tool_id=tc.tool_id,
                tool_name=tc.tool_name,
                arguments=arguments,
            )
            decision = self.permission_checker.check(tool, tc.arguments)
            self._emit_runtime_event(
                "hook_modified_input",
                hook_event="permission_request",
                tool_name=tc.tool_name,
                original_args=original_arguments,
                updated_args=hook_result.updated_args,
                final_args=arguments,
            )
        if hook_result.is_rejected:
            reason = (
                hook_result.reject_reason
                or hook_result.message
                or "permission request cancelled"
            )
            self._emit_runtime_event(
                "hook_rejected",
                hook_event="permission_request",
                tool_name=tc.tool_name,
                reason=reason,
            )
            return tc, decision, ToolResult(
                output=f"Permission denied by Hook: {reason}",
                is_error=True,
            )
        return tc, decision, None

    async def run(self, conversation: ConversationManager) -> AsyncIterator[AgentEvent]:
        self.recent_tool_calls.begin_run()
        self._current_conversation = conversation
        self._hook_prevent_continuation = False
        user_message = self._latest_user_message(conversation)
        if self.hook_engine:
            await self._run_hook_event("user_prompt_submit", message=user_message)
            for he in self._drain_hook_events():
                yield he

        env_context = build_environment_context(
            self.work_dir, self.active_skills, "", self._agent_catalog
        )
        conversation.inject_environment(env_context)

        conversation.inject_long_term_memory(self.instructions_content, "")

        if self.hook_engine:
            await self._run_hook_event("session_start", message=user_message)
            for he in self._drain_hook_events():
                yield he

        iteration = 0
        consecutive_unknown = 0
        max_tokens_escalated = False
        output_recoveries = 0
        self.recovery_state.output_token_escalated = False
        self.recovery_state.continuation_count = 0

        while not self._hook_prevent_continuation:
            iteration += 1

            if self.max_iterations > 0 and iteration > self.max_iterations:
                yield ErrorEvent(
                    message=f"Agent reached maximum iterations ({self.max_iterations})"
                )
                break

            if self.hook_engine:
                await self._run_hook_event("turn_start")
                for he in self._drain_hook_events():
                    yield he
                if self._hook_prevent_continuation:
                    break

            self._consume_mailbox(conversation)
            if self.notification_fn:
                for note in self.notification_fn():
                    conversation.add_system_reminder(note)

            if self.hook_engine:
                await self._run_hook_event("pre_send")
                for he in self._drain_hook_events():
                    yield he
                self._inject_hook_contexts(conversation)
                if self._hook_prevent_continuation:
                    break

            hook_prompts = (
                self.hook_engine.get_prompt_messages() if self.hook_engine else None
            )
            system = build_system_prompt(
                hook_prompts=hook_prompts,
                coordinator_mode=self.coordinator_mode,
                agent_catalog=self._agent_catalog_list or None,
                prompt_state=self.prompt_state_registry.render(),
            )

            if self.plan_mode:
                plan_path = str(self._get_plan_path())
                if self.permission_checker:
                    self.permission_checker.plan_file_path = plan_path
                plan_exists = self._get_plan_path().exists()
                plan_reminder = build_plan_mode_reminder(
                    plan_path, plan_exists, iteration
                )
                conversation.add_system_reminder(plan_reminder)

            if self.hook_engine:
                for note in self.hook_engine.drain_notifications():
                    conversation.add_system_reminder(
                        f"Hook [{note.hook_id}] {note.event}: {note.output}"
                    )

            deferred_names = self.registry.get_deferred_tool_names()
            if deferred_names:
                conversation.add_system_reminder(
                    "The following deferred tools are available via ToolSearch. "
                    "Their schemas are NOT loaded - use ToolSearch with "
                    'query "select:<name>[,<name>...]" to load tool schemas before calling them:\n'
                    + "\n".join(deferred_names)
                )

            tools = self.registry.get_all_definitions()

            # Layer 1: apply tool-result budget（就地修改 conversation）
            new_records = apply_tool_result_budget(
                conversation, self.session_dir, self.replacement_state
            )
            if new_records:
                append_replacement_records(self.session_dir, new_records)

            # Layer 2: 接近 context window 上限时自动 compact
            # tool-result budget 已就地修改 conversation，直接用 conversation.history 估算
            compact_result = await auto_compact(
                conversation,
                self.client,
                self.context_window,
                self.session_dir,
                protocol=self.protocol,
                breaker=self.compact_breaker,
                recovery=self.recovery_state,
                tool_schemas=self.registry.get_all_definitions(),
                transcript_path=self._transcript_path,
            )
            if isinstance(compact_result, CompactEvent):
                yield CompactNotification(
                    before_tokens=compact_result.before_tokens,
                    message=f"上下文已压缩（压缩前 {compact_result.before_tokens:,} tokens）",
                    boundary=compact_result.boundary,
                )
                conversation.inject_environment(env_context)
                conversation.inject_long_term_memory(
                    self.instructions_content, ""
                )
                # 压缩后重新应用 budget（就地修改）
                apply_tool_result_budget(
                    conversation, self.session_dir, self.replacement_state
                )
            elif isinstance(compact_result, str):
                yield ErrorEvent(message=compact_result)

            collector = StreamCollector()
            executor = StreamingExecutor()
            deferred_tool_calls: list[ToolCallComplete] = []
            llm_stream = self.recovery_controller.stream(
                conversation,
                system=system,
                tools=tools,
                state=self.recovery_state,
                context_recover=lambda: self._recover_context_limit(conversation),
            )
            async for event in collector.consume(llm_stream):
                if isinstance(event, RetryEvent):
                    self._emit_runtime_event(
                        "provider_switched"
                        if event.provider_switched
                        else "retry_started",
                        reason=event.reason,
                        attempt=event.attempt,
                        wait=event.wait,
                        provider=event.provider_name,
                    )
                # 流式工具执行：收到完整 tool_use 就立刻提交执行，不等整个响应结束
                if isinstance(event, ToolUseEvent):
                    tc = collector.response.tool_calls[-1]
                    # Hook 可能修改参数并改变权限结论，因此必须先走完整的
                    # pre_tool_use -> permission lifecycle，再决定是否执行。
                    tool = self.registry.get(tc.tool_name)
                    needs_ask = self.hook_engine is not None
                    if tool and self.permission_checker:
                        decision = self.permission_checker.check(tool, tc.arguments)
                        needs_ask = needs_ask or decision.effect == "ask"
                    if needs_ask:
                        deferred_tool_calls.append(tc)
                    else:
                        self.recovery_state.tool_execution_started = True
                        executor.submit(self._execute_single_tool_direct(tc))
                yield event

            response = collector.response
            self.client = self.recovery_controller.current_client

            if self.hook_engine:
                post_receive = await self._run_hook_event(
                    "post_receive", message=response.text
                )
                if post_receive.updated_output is not None:
                    response.text = post_receive.updated_output
                for he in self._drain_hook_events():
                    yield he

            self.total_input_tokens += response.input_tokens
            self.total_output_tokens += response.output_tokens
            yield UsageEvent(
                input_tokens=self.total_input_tokens,
                output_tokens=self.total_output_tokens,
            )

            conv_thinking = [
                ConvThinkingBlock(thinking=tb.thinking, signature=tb.signature)
                for tb in response.thinking_blocks
            ]

            if self._hook_prevent_continuation:
                if response.text:
                    conversation.add_assistant_message(
                        response.text, thinking_blocks=conv_thinking
                    )
                if self.hook_engine:
                    await self._run_hook_event("turn_end")
                    for he in self._drain_hook_events():
                        yield he
                yield LoopComplete(total_turns=iteration)
                break

            if response.stop_reason == "max_tokens":
                if not max_tokens_escalated:
                    self.client.set_max_output_tokens(MAX_TOKENS_CEILING)
                    max_tokens_escalated = True
                    self.recovery_state.output_token_escalated = True
                    if response.text:
                        conversation.add_assistant_message(
                            response.text, thinking_blocks=conv_thinking
                        )
                        conversation.add_user_message(
                            "Output token limit hit. Resume directly from where you stopped. "
                            "Do not apologize or repeat previous content. Pick up mid-thought if needed."
                        )
                    retry_event = RetryEvent(reason="max_tokens escalation")
                    self._emit_runtime_event(
                        "retry_started", reason=retry_event.reason, attempt=0, wait=0.0
                    )
                    yield retry_event
                    continue
                elif output_recoveries < self.recovery_controller.policy.max_output_continuations:
                    output_recoveries += 1
                    self.recovery_state.continuation_count = output_recoveries
                    conversation.add_assistant_message(
                        response.text, thinking_blocks=conv_thinking
                    )
                    conversation.add_user_message(
                        "Output token limit hit. Resume directly from where you stopped. "
                        "Break remaining work into smaller pieces."
                    )
                    retry_event = RetryEvent(
                        reason=(
                            f"max_tokens recovery {output_recoveries}/"
                            f"{self.recovery_controller.policy.max_output_continuations}"
                        )
                    )
                    self._emit_runtime_event(
                        "retry_started", reason=retry_event.reason, attempt=0, wait=0.0
                    )
                    yield retry_event
                    continue
            else:
                output_recoveries = 0

            if not response.tool_calls:
                conversation.add_assistant_message(
                    response.text, thinking_blocks=conv_thinking
                )
                self._loop_count += 1
                if (
                    self._loop_count % MEMORY_EXTRACTION_INTERVAL == 0
                    and self.memory_manager
                ):
                    asyncio.ensure_future(self._extract_memories(conversation))
                if self._consolidator is not None:
                    asyncio.ensure_future(
                        self._consolidator.maybe_run(self.client, conversation, self.protocol)
                    )
                if self.hook_engine:
                    await self._run_hook_event("turn_end")
                    for he in self._drain_hook_events():
                        yield he
                if self.file_history is not None:
                    summary = response.text[:60] + "..." if len(response.text) > 60 else response.text
                    self.file_history.make_snapshot(len(conversation.history), summary)
                yield LoopComplete(total_turns=iteration)
                break

            tool_uses = [
                ToolUseBlock(
                    tool_use_id=tc.tool_id,
                    tool_name=tc.tool_name,
                    arguments=tc.arguments,
                )
                for tc in response.tool_calls
            ]
            conversation.add_assistant_message(
                response.text, tool_uses, thinking_blocks=conv_thinking
            )
            # 在 assistant 回复加入历史后锚定实际用量：基线（input + cache + output）
            # 覆盖到当前位置，因此下一轮迭代顶部的 auto-compact 检查只需对
            # 接下来追加的 tool results 做字符估算。
            conversation.record_usage_anchor(
                response.input_tokens,
                response.output_tokens,
                response.cache_read,
                response.cache_creation,
            )

            # 收集流式执行器中已提交的工具结果（工具在 LLM 流式输出期间已开始执行）
            tool_results: list[ToolResultBlock] = []
            streaming_results = await executor.collect_results()

            for br in streaming_results:
                if br.is_unknown:
                    consecutive_unknown += 1
                else:
                    consecutive_unknown = 0
                conversation_output = (
                    br.conversation_output
                    if br.conversation_output is not None
                    else br.result.output
                )
                content = self._maybe_persist_or_truncate(
                    br.tool_id, conversation_output
                )
                tool_results.append(
                    ToolResultBlock(
                        tool_use_id=br.tool_id,
                        content=content,
                        is_error=br.result.is_error,
                    )
                )
                yield ToolResultEvent(
                    tool_id=br.tool_id,
                    tool_name=br.tool_name,
                    output=br.result.output,
                    is_error=br.result.is_error,
                    elapsed=br.elapsed,
                )

            # 需要交互式权限确认的工具，在流结束后顺序执行
            for tc in deferred_tool_calls:
                self.recovery_state.tool_execution_started = True
                tracked: TrackedToolResult | None = None
                elapsed = 0.0
                is_unknown = False

                async for item in self._execute_tool_tracked(tc):
                    if isinstance(item, PermissionRequest):
                        yield item
                    else:
                        tracked, elapsed, is_unknown = item

                if tracked is None:
                    tracked = self._untracked_tool_result(
                        ToolResult(output="Error: no result from tool", is_error=True)
                    )
                result = tracked.result

                if is_unknown:
                    consecutive_unknown += 1
                else:
                    consecutive_unknown = 0

                content = self._maybe_persist_or_truncate(
                    tc.tool_id,
                    tracked.conversation_output,
                )
                tool_results.append(
                    ToolResultBlock(
                        tool_use_id=tc.tool_id,
                        content=content,
                        is_error=result.is_error,
                    )
                )
                yield ToolResultEvent(
                    tool_id=tc.tool_id,
                    tool_name=tc.tool_name,
                    output=result.output,
                    is_error=result.is_error,
                    elapsed=elapsed,
                )

            if consecutive_unknown >= 3:
                yield ErrorEvent(
                    message="Agent terminated: too many consecutive unknown tool calls"
                )
                break

            exit_plan_called = any(
                tc.tool_name == "ExitPlanMode" for tc in response.tool_calls
            )
            conversation.add_tool_results_message(tool_results)

            # 非阻塞 memory recall：工具执行完后检查 prefetch 是否就绪
            if self.memory_recall_task and not self._memory_recall_consumed:
                if self.memory_recall_task.done():
                    try:
                        recall = self.memory_recall_task.result()
                        if recall:
                            conversation.add_system_reminder(recall)
                    except Exception:
                        pass
                    self._memory_recall_consumed = True

            if self.hook_engine:
                await self._run_hook_event("turn_end")
                for he in self._drain_hook_events():
                    yield he
            yield TurnComplete(turn=iteration)

            if exit_plan_called or self._hook_prevent_continuation:
                yield LoopComplete(total_turns=iteration)
                break

        if self.hook_engine:
            await self._run_hook_event("stop")
            await self._run_hook_event("session_end")
            for he in self._drain_hook_events():
                yield he
        self._hook_prevent_continuation = False


    def _consume_mailbox(self, conversation: ConversationManager) -> None:
        if not self.team_name or not self._team_manager:
            return
        try:
            mailbox = self._team_manager.get_mailbox(self.team_name)
            if mailbox is None:
                return
            messages = mailbox.consume(self.agent_id)
            for msg in messages:
                prefix = f"[Message from {msg.from_agent}]"
                if msg.message_type != "text":
                    prefix = f"[{msg.message_type} from {msg.from_agent}]"
                content = f"{prefix} {msg.content}"
                conversation.add_user_message(content)
        except Exception as e:
            log.debug("Mailbox consumption failed: %s", e)

    def _build_permission_description(self, tc: ToolCallComplete) -> str:
        """为 HITL 权限确认生成人类可读的操作描述。"""
        return PermissionChecker.describe_tool_action(tc.tool_name, tc.arguments)

    def _begin_tracked_tool_call(
        self,
        tc: ToolCallComplete,
        policy: RepeatPolicy = RepeatPolicy.OBSERVE,
    ) -> CallContext:
        return self.recent_tool_calls.before_call(
            tc.tool_name,
            tc.arguments,
            policy,
        )

    def _finish_tracked_tool_call(
        self,
        context: CallContext,
        tc: ToolCallComplete,
        result: ToolResult,
    ) -> TrackedToolResult:
        decision = self.recent_tool_calls.after_call(
            context,
            tc.tool_id,
            result.output,
            result.is_error,
        )
        conversation_output = (
            decision.warning
            or decision.compact_output
            or result.output
        )
        if decision.warning:
            log.warning(
                "Tool loop detected: agent=%s tool=%s fingerprint=%s "
                "same_result_count=%d",
                self.agent_id,
                tc.tool_name,
                context.fingerprint,
                decision.same_result_count,
            )
            self._emit_runtime_event(
                "tool_loop_warning",
                agent_id=self.agent_id,
                tool_name=tc.tool_name,
                fingerprint=context.fingerprint,
                call_count=decision.call_count,
                same_result_count=decision.same_result_count,
                policy=RepeatPolicy.OBSERVE.value,
            )
        elif decision.repeated:
            self._emit_runtime_event(
                "tool_repeat_detected",
                agent_id=self.agent_id,
                tool_name=tc.tool_name,
                fingerprint=context.fingerprint,
                call_count=decision.call_count,
                same_result_count=decision.same_result_count,
                policy=RepeatPolicy.OBSERVE.value,
            )
        return TrackedToolResult(
            result=result,
            conversation_output=conversation_output,
            repeated=decision.repeated,
            warning=decision.warning,
        )

    @staticmethod
    def _untracked_tool_result(result: ToolResult) -> TrackedToolResult:
        return TrackedToolResult(
            result=result,
            conversation_output=result.output,
            repeated=False,
        )

    async def _execute_validated_tool(
        self,
        tool: Any,
        tc: ToolCallComplete,
    ) -> TrackedToolResult:
        context = self._begin_tracked_tool_call(tc)
        try:
            try:
                params = tool.params_model.model_validate(tc.arguments)
                result = await tool.execute(params)
            except ValidationError as e:
                result = ToolResult(
                    output=f"Parameter validation error: {e}",
                    is_error=True,
                )
            except Exception as e:
                result = ToolResult(
                    output=f"Tool execution error: {e}",
                    is_error=True,
                )

            self._snapshot_for_recovery(tc, result)
            result = await self._apply_post_tool_hooks(tc, result)
        except BaseException:
            self.recent_tool_calls.abandon_call(context)
            raise

        return self._finish_tracked_tool_call(context, tc, result)

    async def _execute_single_tool_direct(
        self, tc: ToolCallComplete
    ) -> _ToolExecResult:
        tool = self.registry.get(tc.tool_name)
        start = time.monotonic()

        if tool is None:
            return _ToolExecResult(
                tool_id=tc.tool_id,
                tool_name=tc.tool_name,
                result=ToolResult(output=f"Error: unknown tool '{tc.tool_name}'", is_error=True),
                elapsed=time.monotonic() - start,
                is_unknown=True,
            )

        if not self.registry.is_enabled(tc.tool_name):
            return _ToolExecResult(
                tool_id=tc.tool_id,
                tool_name=tc.tool_name,
                result=ToolResult(output=f"Error: tool '{tc.tool_name}' is disabled", is_error=True),
                elapsed=time.monotonic() - start,
                is_unknown=False,
            )

        tc, hook_error = await self._apply_pre_tool_hooks(tc)
        if hook_error is not None:
            return _ToolExecResult(
                tool_id=tc.tool_id,
                tool_name=tc.tool_name,
                result=hook_error,
                elapsed=time.monotonic() - start,
                is_unknown=False,
            )

        if self.permission_checker:
            decision = self.permission_checker.check(tool, tc.arguments)
            tc, decision, permission_error = await self._apply_permission_request_hooks(
                tool, tc, decision
            )
            if permission_error is not None:
                return _ToolExecResult(
                    tool_id=tc.tool_id,
                    tool_name=tc.tool_name,
                    result=permission_error,
                    elapsed=time.monotonic() - start,
                    is_unknown=False,
                )
            if decision.effect == "deny":
                return _ToolExecResult(
                    tool_id=tc.tool_id,
                    tool_name=tc.tool_name,
                    result=ToolResult(output=f"Permission denied: {decision.reason}", is_error=True),
                    elapsed=time.monotonic() - start,
                    is_unknown=False,
                )
            if decision.effect == "ask":
                return _ToolExecResult(
                    tool_id=tc.tool_id,
                    tool_name=tc.tool_name,
                    result=ToolResult(
                        output=(
                            "Permission denied: tool arguments require "
                            "interactive confirmation"
                        ),
                        is_error=True,
                    ),
                    elapsed=time.monotonic() - start,
                    is_unknown=False,
                )

        tracked = await self._execute_validated_tool(tool, tc)

        return _ToolExecResult(
            tool_id=tc.tool_id,
            tool_name=tc.tool_name,
            result=tracked.result,
            elapsed=time.monotonic() - start,
            is_unknown=False,
            conversation_output=tracked.conversation_output,
        )


    async def _execute_batch_parallel(
        self, calls: list[ToolCallComplete]
    ) -> list[_ToolExecResult]:
        tasks = [self._execute_single_tool_direct(tc) for tc in calls]
        return list(await asyncio.gather(*tasks))

    async def _execute_tool(
        self, tc: ToolCallComplete
    ) -> AsyncIterator[PermissionRequest | tuple[ToolResult, float, bool]]:
        async for item in self._execute_tool_tracked(tc):
            if isinstance(item, PermissionRequest):
                yield item
            else:
                tracked, elapsed, is_unknown = item
                yield tracked.result, elapsed, is_unknown

    async def _execute_tool_tracked(
        self, tc: ToolCallComplete
    ) -> AsyncIterator[
        PermissionRequest | tuple[TrackedToolResult, float, bool]
    ]:
        tool = self.registry.get(tc.tool_name)
        start = time.monotonic()
        is_unknown = False

        if tool is None:
            result = ToolResult(
                output=f"Error: unknown tool '{tc.tool_name}'", is_error=True
            )
            is_unknown = True
            elapsed = time.monotonic() - start
            yield self._untracked_tool_result(result), elapsed, is_unknown
            return

        if not self.registry.is_enabled(tc.tool_name):
            result = ToolResult(
                output=f"Error: tool '{tc.tool_name}' is disabled in current mode",
                is_error=True,
            )
            elapsed = time.monotonic() - start
            yield self._untracked_tool_result(result), elapsed, is_unknown
            return

        tc, hook_error = await self._apply_pre_tool_hooks(tc)
        if hook_error is not None:
            yield (
                self._untracked_tool_result(hook_error),
                time.monotonic() - start,
                is_unknown,
            )
            return

        if self.permission_checker:
            decision = self.permission_checker.check(tool, tc.arguments)
            tc, decision, permission_error = await self._apply_permission_request_hooks(
                tool, tc, decision
            )
            if permission_error is not None:
                yield (
                    self._untracked_tool_result(permission_error),
                    time.monotonic() - start,
                    is_unknown,
                )
                return
            if decision.effect == "deny":
                result = ToolResult(
                    output=f"Permission denied: {decision.reason}",
                    is_error=True,
                )
                yield (
                    self._untracked_tool_result(result),
                    time.monotonic() - start,
                    is_unknown,
                )
                return
            if decision.effect == "ask":
                loop = asyncio.get_running_loop()
                future: asyncio.Future[PermissionResponse] = loop.create_future()
                yield PermissionRequest(
                    tool_name=tc.tool_name,
                    description=self._build_permission_description(tc),
                    future=future,
                )
                response = await future
                if response == PermissionResponse.DENY:
                    result = ToolResult(
                        output="Permission denied: 用户拒绝了此操作",
                        is_error=True,
                    )
                    yield (
                        self._untracked_tool_result(result),
                        time.monotonic() - start,
                        is_unknown,
                    )
                    return
                if response == PermissionResponse.ALLOW_ALWAYS:
                    from braincode.permissions.rules import Rule, extract_content

                    content = extract_content(tc.tool_name, tc.arguments)
                    pattern = f"{content[:60]}*" if len(content) > 60 else f"{content}*"
                    rule = Rule(
                        tool_name=tc.tool_name,
                        pattern=pattern,
                        effect="allow",
                    )
                    self.permission_checker.rule_engine.append_local_rule(rule)
                    self.permission_checker.add_session_allow(tc.tool_name, content)

        tracked = await self._execute_validated_tool(tool, tc)

        elapsed = time.monotonic() - start
        yield tracked, elapsed, is_unknown

    def _snapshot_for_recovery(
        self, tc: ToolCallComplete, result: ToolResult
    ) -> None:
        """捕获 ReadFile 刚交给模型的内容，以便 Layer 2 压缩对话后
        auto_compact 能重新附加这些数据。每次 ReadFile 多一次磁盘读取，
        比从 tool 输出中反向解析行号要划算。
        """
        if result.is_error or tc.tool_name != "ReadFile":
            return
        path = tc.arguments.get("file_path") if isinstance(tc.arguments, dict) else None
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            return
        self.recovery_state.record_file_read(path, content)

    async def _extract_memories(
        self, conversation: ConversationManager
    ) -> None:
        """触发记忆提取，对齐 Go 版 inProgress + pendingContext 合并策略。

        当提取正在进行时，新的触发不会启动并发提取，而是标记 _pending_extraction。
        当前提取完成后检查该标志，如果有 pending 则立即执行一次尾随提取，
        防止多个触发器同时执行导致重复提取。
        """
        if not self.memory_manager:
            return

        # 合并策略：正在提取时暂存新请求，等当前提取完成后尾随执行
        if self._extracting:
            log.debug("[extractMemories] extraction in progress — stashing for trailing run")
            self._pending_extraction = True
            return

        self._extracting = True
        try:
            await self.memory_manager.extract(
                self.client, conversation, self.protocol
            )
        except Exception as e:
            log.debug("Memory extraction failed: %s", e)
        finally:
            self._extracting = False
            # 检查是否有尾随提取请求
            if self._pending_extraction:
                self._pending_extraction = False
                log.debug("[extractMemories] running trailing extraction for stashed context")
                # 递归调用自身处理尾随请求
                await self._extract_memories(conversation)

    async def manual_compact(
        self, conversation: ConversationManager
    ) -> CompactNotification | ErrorEvent:
        # auto_compact 会用摘要替换 conversation.history，所有 tool-result 内容
        # （原始或已替换的）都将被丢弃。这里跳过 apply_tool_result_budget —
        # 它在主循环中的唯一目的是为 LLM 调用生成 api_conv，而本路径不需要
        # 发起看到替换结果的 LLM 调用（auto_compact 内部的摘要调用操作的是原始对话）。
        result = await auto_compact(
            conversation,
            self.client,
            self.context_window,
            self.session_dir,
            protocol=self.protocol,
            manual=True,
            breaker=self.compact_breaker,
            recovery=self.recovery_state,
            tool_schemas=self.registry.get_all_definitions(),
            transcript_path=self._transcript_path,
        )
        if isinstance(result, CompactEvent):
            env_context = build_environment_context(
                self.work_dir, self.active_skills, "", self._agent_catalog
            )
            conversation.inject_environment(env_context)
            conversation.inject_long_term_memory(
                self.instructions_content, ""
            )
            return CompactNotification(
                before_tokens=result.before_tokens,
                message=f"上下文已压缩（压缩前 {result.before_tokens:,} tokens）",
                boundary=result.boundary,
            )
        return ErrorEvent(message=result or "压缩失败：对话历史为空或未达到压缩条件")

    async def run_to_completion(
        self, task: str, conversation: ConversationManager | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        self.recent_tool_calls.begin_run()
        env_context = build_environment_context(
            self.work_dir, self.active_skills, "", self._agent_catalog
        )
        if conversation is None:
            conversation = ConversationManager()
            conversation.inject_environment(env_context)

            if self.instructions_content:
                conversation.inject_long_term_memory(
                    self.instructions_content, ""
                )

        if task:
            conversation.add_user_message(task)

        self._hook_prevent_continuation = False
        user_message = task or self._latest_user_message(conversation)
        if self.hook_engine:
            await self._run_hook_event("user_prompt_submit", message=user_message)
            await self._run_hook_event("session_start", message=user_message)

        tools = self.registry.get_all_definitions()

        log.info(
            "[run_to_completion] agent=%s tools=%d names=%s coordinator=%s",
            self.agent_id,
            len(tools),
            [t["name"] for t in tools][:10],
            self.coordinator_mode,
        )

        last_text = ""
        max_tokens_escalated = False
        output_recoveries = 0
        self.recovery_state.output_token_escalated = False
        self.recovery_state.continuation_count = 0

        iteration = 0
        while not self._hook_prevent_continuation:
            iteration += 1
            if self.max_iterations > 0 and iteration > self.max_iterations:
                break
            if self.hook_engine:
                await self._run_hook_event("turn_start")
                if self._hook_prevent_continuation:
                    break

            self._consume_mailbox(conversation)
            if self.notification_fn:
                for note in self.notification_fn():
                    conversation.add_system_reminder(note)

            if self.hook_engine:
                await self._run_hook_event("pre_send")
                self._inject_hook_contexts(conversation)
                if self._hook_prevent_continuation:
                    break

            hook_prompts = (
                self.hook_engine.get_prompt_messages() if self.hook_engine else None
            )
            system = build_system_prompt(
                hook_prompts=hook_prompts,
                coordinator_mode=self.coordinator_mode,
                prompt_state=self.prompt_state_registry.render(),
            )

            # 先应用 tool-result budget（就地修改），再做 auto-compact，确保预算内的结果不会被误压缩
            pre_compact_records = apply_tool_result_budget(
                conversation, self.session_dir, self.replacement_state
            )
            if pre_compact_records:
                append_replacement_records(self.session_dir, pre_compact_records)

            compact_result = await auto_compact(
                conversation,
                self.client,
                self.context_window,
                self.session_dir,
                protocol=self.protocol,
                breaker=self.compact_breaker,
                recovery=self.recovery_state,
                tool_schemas=self.registry.get_all_definitions(),
                transcript_path=self._transcript_path,
            )
            if isinstance(compact_result, CompactEvent):
                conversation.inject_environment(env_context)

            deferred_names = self.registry.get_deferred_tool_names()
            if deferred_names:
                conversation.add_system_reminder(
                    "The following deferred tools are available via ToolSearch. "
                    "Their schemas are NOT loaded - use ToolSearch with "
                    'query "select:<name>[,<name>...]" to load tool schemas before calling them:\n'
                    + "\n".join(deferred_names)
                )

            # 压缩后或追加 deferred 提示后重新应用 budget（就地修改）
            _new_records = apply_tool_result_budget(
                conversation, self.session_dir, self.replacement_state
            )
            if _new_records:
                append_replacement_records(self.session_dir, _new_records)

            collector = StreamCollector()
            llm_stream = self.recovery_controller.stream(
                conversation,
                system=system,
                tools=tools,
                state=self.recovery_state,
                context_recover=lambda: self._recover_context_limit(conversation),
            )
            async for _event in collector.consume(llm_stream):
                if isinstance(_event, RetryEvent):
                    self._emit_runtime_event(
                        "provider_switched"
                        if _event.provider_switched
                        else "retry_started",
                        reason=_event.reason,
                        attempt=_event.attempt,
                        wait=_event.wait,
                        provider=_event.provider_name,
                    )
                    if event_callback:
                        event_callback(
                            {
                                "type": "retry",
                                "reason": _event.reason,
                                "attempt": _event.attempt,
                                "wait": _event.wait,
                                "provider": _event.provider_name,
                                "providerSwitched": _event.provider_switched,
                            }
                        )

            response = collector.response
            self.client = self.recovery_controller.current_client
            if self.hook_engine:
                post_receive = await self._run_hook_event(
                    "post_receive", message=response.text
                )
                if post_receive.updated_output is not None:
                    response.text = post_receive.updated_output
            self.total_input_tokens += response.input_tokens
            self.total_output_tokens += response.output_tokens

            if event_callback:
                event_callback({
                    "type": "usage",
                    "usage": {
                        "inputTokens": self.total_input_tokens,
                        "outputTokens": self.total_output_tokens,
                    },
                })

            if response.text:
                last_text = response.text
                if event_callback:
                    event_callback({
                        "type": "stream_text",
                        "text": response.text,
                    })

            if self._hook_prevent_continuation:
                if response.text:
                    conversation.add_assistant_message(response.text)
                if self.hook_engine:
                    await self._run_hook_event("turn_end")
                break

            if response.stop_reason == "max_tokens":
                if not max_tokens_escalated:
                    self.client.set_max_output_tokens(MAX_TOKENS_CEILING)
                    max_tokens_escalated = True
                    self.recovery_state.output_token_escalated = True
                    if response.text:
                        conversation.add_assistant_message(response.text)
                        conversation.add_user_message(
                            "Output token limit hit. Resume directly from where you stopped. "
                            "Do not apologize or repeat previous content."
                        )
                    self._emit_runtime_event(
                        "retry_started",
                        reason="max_tokens escalation",
                        attempt=0,
                        wait=0.0,
                    )
                    if event_callback:
                        event_callback(
                            {"type": "retry", "reason": "max_tokens escalation"}
                        )
                    continue
                if output_recoveries < self.recovery_controller.policy.max_output_continuations:
                    output_recoveries += 1
                    self.recovery_state.continuation_count = output_recoveries
                    conversation.add_assistant_message(response.text)
                    conversation.add_user_message(
                        "Output token limit hit. Resume directly from where you stopped. "
                        "Break remaining work into smaller pieces."
                    )
                    continuation_reason = (
                        "max_tokens recovery "
                        f"{output_recoveries}/"
                        f"{self.recovery_controller.policy.max_output_continuations}"
                    )
                    self._emit_runtime_event(
                        "retry_started",
                        reason=continuation_reason,
                        attempt=output_recoveries,
                        wait=0.0,
                    )
                    if event_callback:
                        event_callback(
                            {
                                "type": "retry",
                                "reason": continuation_reason,
                            }
                        )
                    continue
            else:
                output_recoveries = 0

            log.info(
                "[run_to_completion] agent=%s iter=%d tool_calls=%d text_len=%d stop=%s",
                self.agent_id, iteration, len(response.tool_calls),
                len(response.text), response.stop_reason,
            )

            if not response.tool_calls:
                conversation.add_assistant_message(response.text)
                if self.hook_engine:
                    await self._run_hook_event("turn_end")
                if self.file_history is not None:
                    summary = response.text[:60] + "..." if len(response.text) > 60 else response.text
                    self.file_history.make_snapshot(len(conversation.history), summary)
                break

            tool_uses = [
                ToolUseBlock(
                    tool_use_id=tc.tool_id,
                    tool_name=tc.tool_name,
                    arguments=tc.arguments,
                )
                for tc in response.tool_calls
            ]
            conversation.add_assistant_message(response.text, tool_uses)
            # assistant 回复已在历史中，锚定实际用量；下一轮迭代只需对
            # 下方追加的 tool results 做字符估算。
            conversation.record_usage_anchor(
                response.input_tokens,
                response.output_tokens,
                response.cache_read,
                response.cache_creation,
            )

            tool_results: list[ToolResultBlock] = []
            for tc in response.tool_calls:
                self.recovery_state.tool_execution_started = True
                if event_callback:
                    event_callback({
                        "type": "tool_use",
                        "toolName": tc.tool_name,
                        "args": tc.arguments,
                    })
                tracked = await self._execute_tool_noninteractive_tracked(tc)
                result = tracked.result
                content = self._maybe_persist_or_truncate(
                    tc.tool_id,
                    tracked.conversation_output,
                )
                tool_results.append(
                    ToolResultBlock(
                        tool_use_id=tc.tool_id,
                        content=content,
                        is_error=result.is_error,
                    )
                )

            conversation.add_tool_results_message(tool_results)

            if self.hook_engine:
                await self._run_hook_event("turn_end")

            if self._hook_prevent_continuation:
                break

        if self.hook_engine:
            await self._run_hook_event("stop")
            await self._run_hook_event("session_end")
        self._hook_prevent_continuation = False
        return last_text

    async def _recover_context_limit(
        self, conversation: ConversationManager
    ) -> bool:
        self.client = self.recovery_controller.current_client
        result = await self.manual_compact(conversation)
        return isinstance(result, CompactNotification)

    async def _execute_tool_noninteractive(
        self, tc: ToolCallComplete
    ) -> ToolResult:
        tracked = await self._execute_tool_noninteractive_tracked(tc)
        return tracked.result

    async def _execute_tool_noninteractive_tracked(
        self, tc: ToolCallComplete
    ) -> TrackedToolResult:
        tool = self.registry.get(tc.tool_name)

        if tool is None:
            return self._untracked_tool_result(
                ToolResult(
                    output=f"Error: unknown tool '{tc.tool_name}'",
                    is_error=True,
                )
            )

        if not self.registry.is_enabled(tc.tool_name):
            return self._untracked_tool_result(
                ToolResult(
                    output=f"Error: tool '{tc.tool_name}' is disabled",
                    is_error=True,
                )
            )

        tc, hook_error = await self._apply_pre_tool_hooks(tc)
        if hook_error is not None:
            return self._untracked_tool_result(hook_error)

        if self.permission_checker:
            decision = self.permission_checker.check(tool, tc.arguments)
            tc, decision, permission_error = await self._apply_permission_request_hooks(
                tool, tc, decision
            )
            if permission_error is not None:
                return self._untracked_tool_result(permission_error)
            if decision.effect == "deny":
                return self._untracked_tool_result(
                    ToolResult(
                        output=f"Permission denied: {decision.reason}",
                        is_error=True,
                    )
                )
            if decision.effect == "ask":
                if self.permission_mode == PermissionMode.BYPASS:
                    pass  # BYPASS 模式自动批准
                else:
                    return self._untracked_tool_result(
                        ToolResult(
                            output=(
                                "Permission denied: non-interactive agent cannot "
                                "prompt user"
                            ),
                            is_error=True,
                        )
                    )

        return await self._execute_validated_tool(tool, tc)

    def _maybe_persist_or_truncate(self, tool_use_id: str, text: str) -> str:
        from braincode.context.manager import (
            SINGLE_RESULT_CHAR_LIMIT,
            make_persisted_preview,
            persist_tool_result,
        )

        if len(text) > SINGLE_RESULT_CHAR_LIMIT:
            fp = persist_tool_result(tool_use_id, text, self.session_dir)
            return make_persisted_preview(text, fp)
        if len(text) > MAX_OUTPUT_CHARS:
            return text[:MAX_OUTPUT_CHARS] + "\n… (output truncated)"
        return text
