# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com

"""
Remote Control 服务器：通过 WebSocket 桥接 Agent 事件和 Web UI。

使用 websockets 库提供 HTTP（静态 HTML）+ WebSocket 服务，
让用户在浏览器中与 Braincode Agent 交互。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import websockets
from websockets.asyncio.server import Server as WSServer, ServerConnection
from websockets.http11 import Request, Response

from braincode.agent import (
    Agent,
    CompactNotification,
    ErrorEvent,
    HookEvent,
    LoopComplete,
    PermissionRequest,
    PermissionResponse,
    RetryEvent,
    StreamText,
    ThinkingText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
    UsageEvent,
)
from braincode.client import create_client, resolve_context_window
from braincode.commands import CommandContext, CommandRegistry, CommandType
from braincode.commands.handlers import register_all_commands
from braincode.commands.parser import parse_command
from braincode.config import (
    MCPServerConfig,
    PluginConfig,
    ProviderConfig,
    RecoveryConfig,
    SandboxAppConfig,
    SchedulerConfig,
)
from braincode.conversation import ConversationManager
from braincode.hooks import HookEngine
from braincode.mcp import MCPManager
from braincode.memory import MemoryManager, load_instructions
from braincode.memory.session import Session, SessionManager
from braincode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from braincode.skills.loader import SkillLoader
from braincode.tools import ToolRegistry, create_default_registry
from braincode.tools.plugins import load_plugins
from braincode.tools.impl.tool_search import ToolSearchTool
from braincode.tools.load_skill import LoadSkill
from braincode.web_content import INDEX_HTML

log = logging.getLogger(__name__)


class RemoteServer:
    """Remote Control 核心：桥接 Agent 事件和 WebSocket 客户端。"""

    def __init__(
        self,
        providers: list[ProviderConfig],
        mcp_servers: list[MCPServerConfig] | None = None,
        hook_engine: HookEngine | None = None,
        addr: str = "0.0.0.0",
        port: int = 18888,
        recovery_config: RecoveryConfig | None = None,
        scheduler_config: SchedulerConfig | None = None,
        sandbox_config: SandboxAppConfig | None = None,
        plugin_config: PluginConfig | None = None,
    ) -> None:
        self.providers = providers
        self._mcp_server_configs = mcp_servers or []
        self.hook_engine = hook_engine
        self.addr = addr
        self.port = port
        self._recovery_config = recovery_config or RecoveryConfig()
        self._scheduler_config = scheduler_config or SchedulerConfig()
        self._sandbox_config = sandbox_config or SandboxAppConfig()
        self._plugin_config = plugin_config or PluginConfig()

        # WebSocket 连接池（支持多客户端广播）
        self._connections: set[ServerConnection] = set()

        # Agent 相关状态
        self.agent: Agent | None = None
        self.conversation: ConversationManager | None = None
        self.registry: ToolRegistry | None = None
        self.session_id: str = ""
        self._streaming = False
        self._cancel_event: asyncio.Event | None = None

        # 权限请求的 pending 队列：id -> Future
        self._pending_perms: dict[str, asyncio.Future[PermissionResponse]] = {}

        # 命令注册表
        self.command_registry = CommandRegistry()
        register_all_commands(self.command_registry)

        # MCP 相关
        self.mcp_manager: MCPManager | None = None
        self._mcp_instructions: str = ""

        # Skill 加载器
        self.skill_loader: SkillLoader | None = None

        # Memory / Session
        self.memory_manager: MemoryManager | None = None
        self.session_manager: SessionManager | None = None
        self.session: Session | None = None
        self.job_manager = None
        self.scheduler = None
        self.tool_job_runner = None
        self.prompt_job_runner = None
        self.runtime = None
        self.worktree_manager = None
        self.team_manager = None
        self.task_manager = None
        self.trace_manager = None
        self.agent_loader = None

    # ------------------------------------------------------------------
    # 启动入口
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """启动 HTTP + WebSocket 服务器。"""
        # 初始化 Agent
        self._init_agent()

        # 初始化 MCP（如果有配置）
        await self._init_mcp()

        print(f"\n  Remote UI: http://localhost:{self.port}\n")

        # websockets 的 serve 支持 process_request 回调来处理普通 HTTP
        try:
            async with websockets.serve(
                self._ws_handler,
                self.addr,
                self.port,
                process_request=self._process_http_request,
                max_size=4 * 1024 * 1024,
            ):
                await asyncio.Future()
        finally:
            if self.runtime is not None:
                await self.runtime.shutdown()

    # ------------------------------------------------------------------
    # HTTP 请求处理（为 / 路径提供前端 HTML）
    # ------------------------------------------------------------------

    def _process_http_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        """拦截 HTTP 请求，对 / 路径返回 HTML 页面。
        返回 None 表示继续走 WebSocket 升级流程。
        """
        if request.path == "/":
            return Response(
                200,
                "OK",
                websockets.Headers({"Content-Type": "text/html; charset=utf-8"}),
                INDEX_HTML.encode("utf-8"),
            )
        if request.path != "/ws":
            return Response(404, "Not Found", websockets.Headers(), b"404 Not Found")
        # /ws 路径 → 继续 WebSocket 升级
        return None

    # ------------------------------------------------------------------
    # WebSocket 连接处理
    # ------------------------------------------------------------------

    async def _ws_handler(self, websocket: ServerConnection) -> None:
        """处理单个 WebSocket 连接的全生命周期。"""
        self._connections.add(websocket)
        try:
            # 连接建立时推送会话信息
            await self._broadcast({
                "type": "connected",
                "data": {
                    "session": self.session_id,
                    "cwd": os.getcwd(),
                },
            })

            # 推送命令列表
            await self._broadcast({
                "type": "commands",
                "data": self._build_command_list(),
            })

            # 消息循环
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")
                data = msg.get("data", {})

                if msg_type == "user_message":
                    content = data.get("content", "").strip()
                    if content:
                        # 在后台任务中处理，不阻塞 WebSocket 读循环
                        asyncio.create_task(self._handle_user_message(content))

                elif msg_type == "permission_response":
                    self._handle_permission_response(data)

                elif msg_type == "cancel":
                    if self._cancel_event is not None:
                        self._cancel_event.set()

                elif msg_type == "ping":
                    # 应用层保活
                    await self._broadcast({"type": "pong", "data": None})

        except websockets.ConnectionClosed:
            pass
        finally:
            self._connections.discard(websocket)

    # ------------------------------------------------------------------
    # Agent 初始化（复刻 TUI 的 _select_provider 流程）
    # ------------------------------------------------------------------

    def _init_agent(self) -> None:
        """初始化 Agent 及相关子系统。"""
        provider = self.providers[0]
        work_dir = os.getcwd()
        home = Path.home()

        # 权限系统
        checker = PermissionChecker(
            detector=DangerousCommandDetector(),
            sandbox=PathSandbox(work_dir),
            rule_engine=RuleEngine(
                user_rules_path=home / ".braincode" / "permissions.yaml",
                project_rules_path=Path(work_dir) / ".braincode" / "permissions.yaml",
                local_rules_path=Path(work_dir) / ".braincode" / "permissions.local.yaml",
            ),
            mode=PermissionMode.DEFAULT,
            sandbox_enabled=(
                self._sandbox_config.enabled and self._sandbox_config.auto_allow
            ),
        )

        # 加载自定义指令和记忆
        instructions = load_instructions(work_dir)
        self.memory_manager = MemoryManager(work_dir)
        self.session_manager = SessionManager(work_dir)
        self.session = self.session_manager.create()
        self.session_id = self.session.session_id

        # 创建 LLM 客户端
        client = create_client(provider)
        from braincode.recovery import build_recovery_controller

        # 工具注册表
        self.registry = create_default_registry()
        self.registry.register(ToolSearchTool(self.registry))
        bash_tool = self.registry.get("Bash")
        if bash_tool is not None:
            bash_tool.work_dir = work_dir
        if self._sandbox_config.enabled and bash_tool is not None:
            from braincode.sandbox import SandboxConfig, create_sandbox
            os_sandbox = create_sandbox()
            if os_sandbox and os_sandbox.available():
                bash_tool.sandbox = os_sandbox
                bash_tool.sandbox_config = SandboxConfig(
                    allow_write=[work_dir, "/tmp"],
                    deny_write=[
                        f"{work_dir}/.braincode/config.yaml",
                        f"{work_dir}/.braincode/permissions.local.yaml",
                    ],
                    network_enabled=self._sandbox_config.network_enabled,
                )

        plugin_report = load_plugins(
            self.registry,
            work_dir=work_dir,
            plugin_config=self._plugin_config,
            permission_checker=checker,
            services={"remote": self},
        )
        if self._plugin_config.strict and plugin_report.errors:
            raise RuntimeError(
                "Tool plugin loading failed: " + "; ".join(plugin_report.errors)
            )

        # Skill 加载
        self.skill_loader = SkillLoader(work_dir)
        self.skill_loader.load_all()
        load_skill_tool = LoadSkill()
        self.registry.register(load_skill_tool)

        # 创建 Agent
        self.agent = Agent(
            client=client,
            registry=self.registry,
            protocol=provider.protocol,
            work_dir=work_dir,
            permission_checker=checker,
            context_window=provider.get_context_window(),
            instructions_content=instructions,
            memory_manager=self.memory_manager,
            hook_engine=self.hook_engine,
            recovery_controller=build_recovery_controller(
                client, self.providers, self._recovery_config
            ),
        )
        self.agent.session_id = self.session_id

        from braincode.jobs import (
            BackgroundToolRunner,
            JobKind,
            JobManager,
            MisfirePolicy,
            OverlapPolicy,
            PromptJobRunner,
            SchedulerService,
        )
        from braincode.tools.job_tools import (
            CronCreateTool,
            CronDeleteTool,
            CronListTool,
            JobCancelTool,
            JobCreateTool,
            JobGetTool,
            JobListTool,
        )

        self.job_manager = JobManager.for_project(work_dir)
        self.tool_job_runner = BackgroundToolRunner(
            self.job_manager, self.registry, checker, work_dir=work_dir
        )

        async def run_scheduled_prompt(job, prompt: str) -> str:
            scheduled_client = create_client(provider)
            scheduled_session = self.session_manager.create()
            scheduled_registry = ToolRegistry()
            for scheduled_tool in self.registry.list_tools():
                if scheduled_tool.name != "AskUserQuestion":
                    scheduled_registry.register_from(self.registry, scheduled_tool)
            scheduled_agent = Agent(
                client=scheduled_client,
                registry=scheduled_registry,
                protocol=provider.protocol,
                work_dir=job.worktree_path or work_dir,
                permission_checker=checker,
                context_window=provider.get_context_window(),
                instructions_content=instructions,
                memory_manager=self.memory_manager,
                hook_engine=self.hook_engine,
                recovery_controller=build_recovery_controller(
                    scheduled_client, self.providers, self._recovery_config
                ),
            )
            scheduled_agent.session_id = scheduled_session.session_id
            try:
                return await scheduled_agent.run_to_completion(prompt)
            finally:
                scheduled_session.close()

        self.prompt_job_runner = PromptJobRunner(
            self.job_manager,
            run_scheduled_prompt,
            output_dir=Path(work_dir) / ".braincode" / "job-output",
        )

        def dispatch_scheduled_job(job) -> None:
            if job.kind == JobKind.TOOL:
                self.tool_job_runner.submit_job(job)
            elif job.kind == JobKind.PROMPT:
                self.prompt_job_runner.submit_job(job)

        self.scheduler = SchedulerService(
            self.job_manager.store,
            poll_interval_seconds=self._scheduler_config.poll_interval_seconds,
            on_job_created=dispatch_scheduled_job,
            default_timezone=self._scheduler_config.timezone,
            default_misfire_policy=MisfirePolicy(
                self._scheduler_config.default_misfire_policy
            ),
            default_overlap_policy=OverlapPolicy(
                self._scheduler_config.default_overlap_policy
            ),
        )
        from braincode.prompt_state import (
            CronPromptStateProvider,
            JobPromptStateProvider,
            TeamPromptStateProvider,
            WorktreePromptStateProvider,
        )
        self.agent.register_prompt_state_provider(
            JobPromptStateProvider(self.job_manager)
        )
        self.agent.register_prompt_state_provider(
            CronPromptStateProvider(self.scheduler)
        )
        from braincode.agents.loader import AgentLoader
        from braincode.agents.task_manager import TaskManager
        from braincode.agents.trace import TraceManager
        from braincode.runtime import RuntimeContainer
        from braincode.teams.manager import TeamManager
        from braincode.tools.agent_tool import AgentTool
        from braincode.tools.team_create import TeamCreateTool
        from braincode.tools.team_delete import TeamDeleteTool
        from braincode.worktree import WorktreeManager

        self.worktree_manager = WorktreeManager(repo_root=work_dir)
        restored = self.worktree_manager.restore_session()
        if restored is not None:
            self.agent.work_dir = restored.worktree_path
        self.task_manager = TaskManager(self.job_manager)
        self.trace_manager = TraceManager()
        self.agent_loader = AgentLoader(work_dir)
        self.agent_loader.load_all()
        self.team_manager = TeamManager(
            worktree_manager=self.worktree_manager,
            trace_manager=self.trace_manager,
            job_manager=self.job_manager,
        )
        self.registry.register(
            AgentTool(
                agent_loader=self.agent_loader,
                task_manager=self.task_manager,
                trace_manager=self.trace_manager,
                parent_agent=self.agent,
                provider_config=provider,
                worktree_manager=self.worktree_manager,
                team_manager=self.team_manager,
            )
        )
        self.registry.register(
            TeamCreateTool(
                team_manager=self.team_manager,
                parent_agent=self.agent,
                teammate_mode="in-process",
                is_interactive=False,
            )
        )
        self.registry.register(
            TeamDeleteTool(
                team_manager=self.team_manager,
                parent_agent=self.agent,
            )
        )
        self.agent._team_manager = self.team_manager
        self.agent.notification_fn = self.team_manager.drain_lead_mailbox
        self.agent.register_prompt_state_provider(
            WorktreePromptStateProvider(self.worktree_manager)
        )
        self.agent.register_prompt_state_provider(
            TeamPromptStateProvider(self.team_manager)
        )
        self.runtime = RuntimeContainer.adopt(
            client=client,
            agent=self.agent,
            registry=self.registry,
            permission_checker=checker,
            job_manager=self.job_manager,
            scheduler=self.scheduler,
            team_manager=self.team_manager,
            worktree_manager=self.worktree_manager,
            hook_engine=self.hook_engine,
            tool_job_runner=self.tool_job_runner,
            prompt_job_runner=self.prompt_job_runner,
            task_manager=self.task_manager,
            trace_manager=self.trace_manager,
            memory_manager=self.memory_manager,
            session_manager=self.session_manager,
            session=self.session,
            skill_loader=self.skill_loader,
            agent_loader=self.agent_loader,
            load_skill_tool=load_skill_tool,
            mcp_configs=self._mcp_server_configs,
        )
        self.runtime.event_bus.subscribe(self._on_runtime_event)

        from braincode.commands.handlers.cron import create_cron_command
        from braincode.commands.handlers.jobs import create_jobs_command
        self.command_registry.register_sync(
            create_jobs_command(
                self.job_manager,
                self.tool_job_runner,
                self.prompt_job_runner,
                self.task_manager,
            )
        )
        self.command_registry.register_sync(create_cron_command(self.scheduler))
        if bash_tool is not None:
            bash_tool.background_runner = self.tool_job_runner
        for job_tool in (
            JobCreateTool(
                self.job_manager, self.tool_job_runner, self.prompt_job_runner
            ),
            JobGetTool(self.job_manager),
            JobListTool(self.job_manager),
            JobCancelTool(
                self.job_manager, self.tool_job_runner, self.prompt_job_runner
            ),
            CronCreateTool(self.scheduler, self.tool_job_runner),
            CronListTool(self.scheduler),
            CronDeleteTool(self.scheduler),
        ):
            self.registry.register(job_tool)
        self.tool_job_runner.start_pending()
        self.prompt_job_runner.start_pending()
        if self._scheduler_config.enabled:
            self.scheduler.start()

        # 连接 Skill 到 Agent
        load_skill_tool.set_loader(self.skill_loader)
        load_skill_tool.set_agent(self.agent)

        catalog = self.skill_loader.get_catalog()
        if catalog:
            lines = ["You can use the following Skills:", ""]
            for name, desc in catalog:
                lines.append(f"- {name}: {desc}")
            lines.append("")
            lines.append("If the user's request matches a Skill, call LoadSkill to activate it.")
            self.agent.set_skill_catalog("\n".join(lines))

        # 初始化对话管理器
        self.conversation = ConversationManager()

        log.info("Agent initialized: session=%s, model=%s", self.session_id, provider.model)

    # ------------------------------------------------------------------
    # MCP 初始化
    # ------------------------------------------------------------------

    async def _init_mcp(self) -> None:
        """连接所有配置的 MCP 服务器，注册工具。"""
        if not self._mcp_server_configs or self.registry is None:
            return

        if self.runtime is not None:
            connect_result = await self.runtime.initialize_mcp()
            manager = self.runtime.mcp_manager
        else:
            manager = MCPManager()
            manager.load_configs(self._mcp_server_configs)
            connect_result = await manager.register_all_tools(self.registry)
        self.mcp_manager = manager

        for err in connect_result.errors:
            log.warning("MCP error: %s", err)

        # 构建 MCP 指令（首次发送消息时注入 conversation）
        if connect_result.servers:
            parts = []
            for srv_info in connect_result.servers:
                section = f"## {srv_info.name}\n"
                if srv_info.instructions:
                    section += srv_info.instructions
                else:
                    tool_names = [
                        t.name for t in self.registry.list_tools()
                        if t.name.startswith(f"mcp__{srv_info.name}__")
                    ]
                    if tool_names:
                        section += "Available tools: " + ", ".join(tool_names)
                parts.append(section)
            self._mcp_instructions = (
                "# MCP Server Instructions\n\n"
                "The following MCP servers have provided instructions "
                "for how to use their tools and resources:\n\n"
                + "\n\n".join(parts)
            )
        if self.agent is not None:
            self.agent.set_mcp_prompt_state(self._mcp_instructions)

    # ------------------------------------------------------------------
    # 用户消息处理
    # ------------------------------------------------------------------

    async def _handle_user_message(self, content: str) -> None:
        """处理来自 Web UI 的用户消息或斜杠命令。"""
        if self._streaming:
            return

        # 斜杠命令
        if content.startswith("/"):
            await self._handle_slash_command(content)
            return

        # 普通消息 → 发给 Agent
        self._streaming = True
        assert self.conversation is not None
        assert self.agent is not None

        self.conversation.add_user_message(content)

        # 创建取消事件
        self._cancel_event = asyncio.Event()
        start_time = time.monotonic()
        stream_buf = ""

        try:
            async for event in self.agent.run(self.conversation):
                # 检查取消信号
                if self._cancel_event.is_set():
                    break

                if isinstance(event, StreamText):
                    stream_buf += event.text
                    await self._broadcast({
                        "type": "stream_text",
                        "data": {"text": event.text},
                    })

                elif isinstance(event, ThinkingText):
                    await self._broadcast({
                        "type": "thinking_text",
                        "data": {"text": event.text},
                    })

                elif isinstance(event, ToolUseEvent):
                    await self._broadcast({
                        "type": "tool_use",
                        "data": {
                            "toolId": event.tool_id,
                            "toolName": event.tool_name,
                            "args": event.arguments,
                        },
                    })

                elif isinstance(event, ToolResultEvent):
                    # 如果之前有累积的流式文本，先结束它
                    if stream_buf:
                        await self._broadcast({
                            "type": "stream_end",
                            "data": {"text": stream_buf},
                        })
                        stream_buf = ""
                    await self._broadcast({
                        "type": "tool_result",
                        "data": {
                            "toolId": event.tool_id,
                            "toolName": event.tool_name,
                            "output": event.output,
                            "isError": event.is_error,
                            "elapsed": event.elapsed,
                        },
                    })

                elif isinstance(event, PermissionRequest):
                    # 生成唯一 ID，等待 Web 端回复
                    perm_id = f"perm_{time.time_ns()}"
                    self._pending_perms[perm_id] = event.future
                    await self._broadcast({
                        "type": "permission_request",
                        "data": {
                            "id": perm_id,
                            "toolName": event.tool_name,
                            "description": event.description,
                        },
                    })

                elif isinstance(event, TurnComplete):
                    if stream_buf:
                        await self._broadcast({
                            "type": "stream_end",
                            "data": {"text": stream_buf},
                        })
                        stream_buf = ""
                    await self._broadcast({
                        "type": "turn_complete",
                        "data": {"turn": event.turn},
                    })

                elif isinstance(event, LoopComplete):
                    if stream_buf:
                        await self._broadcast({
                            "type": "stream_end",
                            "data": {"text": stream_buf},
                        })
                        stream_buf = ""
                    elapsed = time.monotonic() - start_time
                    await self._broadcast({
                        "type": "loop_complete",
                        "data": {
                            "totalTurns": event.total_turns,
                            "elapsed": elapsed,
                        },
                    })

                elif isinstance(event, UsageEvent):
                    await self._broadcast({
                        "type": "usage",
                        "data": {
                            "inputTokens": event.input_tokens,
                            "outputTokens": event.output_tokens,
                        },
                    })

                elif isinstance(event, ErrorEvent):
                    await self._broadcast({
                        "type": "error",
                        "data": {"message": event.message},
                    })

                elif isinstance(event, CompactNotification):
                    await self._broadcast({
                        "type": "compact",
                        "data": {"message": event.message},
                    })

                elif isinstance(event, RetryEvent):
                    await self._broadcast({
                        "type": "retry",
                        "data": {
                            "reason": event.reason,
                            "waitMs": int(event.wait * 1000),
                            "attempt": event.attempt,
                            "provider": event.provider_name,
                            "providerSwitched": event.provider_switched,
                        },
                    })

                elif isinstance(event, HookEvent):
                    status = "ok" if event.success else "error"
                    await self._broadcast({
                        "type": "system",
                        "data": {
                            "message": f"Hook [{event.hook_id}] {status}: {event.output}"
                        },
                    })

        except asyncio.CancelledError:
            await self._broadcast({
                "type": "error",
                "data": {"message": "Operation cancelled"},
            })
        except Exception as exc:
            log.exception("Agent run error")
            await self._broadcast({
                "type": "error",
                "data": {"message": str(exc)},
            })
        finally:
            self._streaming = False
            self._cancel_event = None

    # ------------------------------------------------------------------
    # 斜杠命令处理
    # ------------------------------------------------------------------

    async def _handle_slash_command(self, input_text: str) -> None:
        """分发斜杠命令。"""
        name, args, is_command = parse_command(input_text)
        if not is_command or not name:
            return

        cmd = self.command_registry.find(name)
        if cmd is None:
            await self._broadcast({
                "type": "error",
                "data": {"message": f"Unknown command: /{name} — type /help to see available commands"},
            })
            await self._broadcast({"type": "command_done", "data": None})
            return

        # 需要参数但没给
        if not args and cmd.arg_prompt:
            await self._broadcast({
                "type": "system",
                "data": {"message": cmd.arg_prompt},
            })
            await self._broadcast({"type": "command_done", "data": None})
            return

        if cmd.type == CommandType.LOCAL:
            # 本地命令直接执行
            ctx = self._build_command_context(args)
            try:
                await cmd.handler(ctx)
            except Exception as exc:
                await self._broadcast({
                    "type": "error",
                    "data": {"message": f"Command error: {exc}"},
                })
            await self._broadcast({"type": "command_done", "data": None})

        elif cmd.type == CommandType.LOCAL_UI:
            # UI 命令需要特殊处理
            if name == "clear":
                self.conversation = ConversationManager()
                if self.agent is not None:
                    self.agent.clear_active_skills()
                await self._broadcast({"type": "clear", "data": None})

            elif name == "compact":
                await self._handle_compact()
                return

            else:
                await self._broadcast({
                    "type": "system",
                    "data": {"message": f"/{name} is not fully supported in remote mode."},
                })

            await self._broadcast({"type": "command_done", "data": None})

        elif cmd.type == CommandType.PROMPT:
            # Prompt 类命令：handler 返回 prompt 文本，注入给 agent
            ctx = self._build_command_context(args)
            try:
                await cmd.handler(ctx)
            except Exception as exc:
                await self._broadcast({
                    "type": "error",
                    "data": {"message": f"Command error: {exc}"},
                })
                await self._broadcast({"type": "command_done", "data": None})

    def _build_command_context(self, args: str) -> CommandContext:
        """构建命令上下文。"""
        return CommandContext(
            args=args,
            agent=self.agent,
            conversation=self.conversation,
            session=self.session,
            session_manager=self.session_manager,
            memory_manager=self.memory_manager,
            ui=self,  # type: ignore[arg-type]
            config={
                "registry": self.command_registry,
            },
        )

    async def _handle_compact(self) -> None:
        """处理 /compact 命令。"""
        if self.agent is None or self.conversation is None:
            await self._broadcast({
                "type": "error",
                "data": {"message": "Compact requires an active agent."},
            })
            await self._broadcast({"type": "command_done", "data": None})
            return

        await self._broadcast({
            "type": "system",
            "data": {"message": "Compacting conversation..."},
        })

        result = await self.agent.manual_compact(self.conversation)
        if isinstance(result, CompactNotification):
            await self._broadcast({
                "type": "system",
                "data": {"message": result.message},
            })
        elif isinstance(result, ErrorEvent):
            await self._broadcast({
                "type": "error",
                "data": {"message": result.message},
            })

        await self._broadcast({"type": "command_done", "data": None})

    # ------------------------------------------------------------------
    # UIController 协议实现（供命令系统回调）
    # ------------------------------------------------------------------

    def add_system_message(self, text: str) -> None:
        """同步接口 — 在事件循环中调度广播。"""
        asyncio.ensure_future(self._broadcast({
            "type": "system",
            "data": {"message": text},
        }))

    def _on_runtime_event(self, event) -> None:
        asyncio.create_task(
            self._broadcast(
                {
                    "type": "runtime_event",
                    "data": event.to_dict(),
                }
            )
        )

    def send_user_message(self, text: str) -> None:
        """同步接口 — 注入用户消息并触发 agent。"""
        asyncio.create_task(self._handle_user_message(text))

    def set_plan_mode(self, enabled: bool) -> None:
        if self.agent is None:
            return
        if enabled:
            self.agent.set_permission_mode(PermissionMode.PLAN)
        else:
            self.agent.set_permission_mode(PermissionMode.DEFAULT)

    def get_token_count(self) -> tuple[int, int]:
        if self.agent:
            return self.agent.total_input_tokens, self.agent.total_output_tokens
        return 0, 0

    def refresh_status(self) -> None:
        pass  # Remote 模式不需要刷新 TUI 状态栏

    # ------------------------------------------------------------------
    # 权限响应处理
    # ------------------------------------------------------------------

    def _handle_permission_response(self, data: dict[str, Any]) -> None:
        """处理来自 Web UI 的权限回复。"""
        perm_id = data.get("id", "")
        response_str = data.get("response", "deny")

        future = self._pending_perms.pop(perm_id, None)
        if future is None or future.done():
            return

        # 映射字符串到枚举
        mapping = {
            "allow": PermissionResponse.ALLOW,
            "deny": PermissionResponse.DENY,
            "allowAlways": PermissionResponse.ALLOW_ALWAYS,
        }
        response = mapping.get(response_str, PermissionResponse.DENY)
        future.set_result(response)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _build_command_list(self) -> list[dict[str, str]]:
        """构建命令列表，推送给前端用于斜杠命令菜单。"""
        result = []
        for cmd in self.command_registry.list_commands():
            result.append({
                "name": cmd.name,
                "description": cmd.description,
            })
        return result

    async def _broadcast(self, msg: dict[str, Any]) -> None:
        """向所有已连接的 WebSocket 客户端广播消息。"""
        if not self._connections:
            return
        data = json.dumps(msg, ensure_ascii=False)
        # 复制集合避免迭代中修改
        closed = []
        for ws in list(self._connections):
            try:
                await ws.send(data)
            except websockets.ConnectionClosed:
                closed.append(ws)
            except Exception:
                closed.append(ws)
        for ws in closed:
            self._connections.discard(ws)
