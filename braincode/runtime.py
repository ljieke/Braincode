from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from braincode.agent import Agent
from braincode.agents.loader import AgentLoader
from braincode.agents.task_manager import TaskManager
from braincode.agents.trace import TraceManager
from braincode.client import LLMClient, create_client
from braincode.config import (
    MCPServerConfig,
    PluginConfig,
    ProviderConfig,
    RecoveryConfig,
    SandboxAppConfig,
    SchedulerConfig,
    WorktreeConfig,
)
from braincode.hooks import HookEngine
from braincode.jobs import (
    BackgroundToolRunner,
    Job,
    JobKind,
    JobManager,
    MisfirePolicy,
    OverlapPolicy,
    PromptJobRunner,
    SchedulerService,
)
from braincode.mcp import ConnectResult, MCPManager
from braincode.memory import MemoryManager, load_instructions
from braincode.memory.session import Session, SessionManager
from braincode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from braincode.prompt_state import (
    CronPromptStateProvider,
    JobPromptStateProvider,
    TeamPromptStateProvider,
    WorktreePromptStateProvider,
)
from braincode.recovery import RecoveryController, build_recovery_controller
from braincode.skills.loader import SkillLoader
from braincode.teams.manager import TeamManager
from braincode.tools import ToolRegistry, create_default_registry
from braincode.tools.plugins import load_plugins
from braincode.tools.agent_tool import AgentTool
from braincode.tools.ask_user import AskUserTool
from braincode.tools.exit_plan_mode import ExitPlanModeTool
from braincode.tools.impl.tool_search import ToolSearchTool
from braincode.tools.install_skill import InstallSkillTool
from braincode.tools.job_tools import (
    CronCreateTool,
    CronDeleteTool,
    CronListTool,
    JobCancelTool,
    JobCreateTool,
    JobGetTool,
    JobListTool,
)
from braincode.tools.load_skill import LoadSkill
from braincode.tools.synthetic_output import SyntheticOutputTool
from braincode.tools.team_create import TeamCreateTool
from braincode.tools.team_delete import TeamDeleteTool
from braincode.worktree import WorktreeManager

log = logging.getLogger(__name__)


class RuntimeEventType(StrEnum):
    JOB_CREATED = "job_created"
    JOB_STARTED = "job_started"
    JOB_PROGRESS = "job_progress"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    JOB_CANCELLED = "job_cancelled"
    SCHEDULE_FIRED = "schedule_fired"
    RETRY_STARTED = "retry_started"
    PROVIDER_SWITCHED = "provider_switched"
    HOOK_MODIFIED_INPUT = "hook_modified_input"
    HOOK_REJECTED = "hook_rejected"


@dataclass(frozen=True)
class RuntimeEvent:
    type: RuntimeEventType
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
        }


class RuntimeEventBus:
    def __init__(self, history_limit: int = 500) -> None:
        self._subscribers: list[Callable[[RuntimeEvent], None]] = []
        self._history: deque[RuntimeEvent] = deque(maxlen=history_limit)

    def subscribe(
        self, callback: Callable[[RuntimeEvent], None]
    ) -> Callable[[], None]:
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> RuntimeEvent:
        event = RuntimeEvent(RuntimeEventType(event_type), payload or {})
        self._history.append(event)
        for subscriber in list(self._subscribers):
            try:
                subscriber(event)
            except Exception:
                log.warning("Runtime event subscriber failed", exc_info=True)
        return event

    def history(self) -> list[RuntimeEvent]:
        return list(self._history)


@dataclass
class RuntimeContainer:
    client: LLMClient
    agent: Agent
    registry: ToolRegistry
    permission_checker: PermissionChecker
    job_manager: JobManager
    scheduler: SchedulerService
    team_manager: TeamManager
    worktree_manager: WorktreeManager
    hook_engine: HookEngine | None
    recovery_controller: RecoveryController
    event_bus: RuntimeEventBus
    tool_job_runner: BackgroundToolRunner
    prompt_job_runner: PromptJobRunner
    task_manager: TaskManager
    trace_manager: TraceManager
    memory_manager: MemoryManager
    session_manager: SessionManager | None
    session: Session | None
    skill_loader: SkillLoader | None
    agent_loader: AgentLoader | None
    load_skill_tool: LoadSkill | None
    install_skill_tool: InstallSkillTool | None
    mcp_configs: list[MCPServerConfig] = field(default_factory=list)
    mcp_manager: MCPManager | None = None
    mcp_result: ConnectResult | None = None
    mcp_init_task: asyncio.Task[None] | None = None
    stale_cleanup_task: asyncio.Task[None] | None = None

    @property
    def prompt_state_registry(self):
        return self.agent.prompt_state_registry

    @classmethod
    def adopt(
        cls,
        *,
        client: LLMClient,
        agent: Agent,
        registry: ToolRegistry,
        permission_checker: PermissionChecker,
        job_manager: JobManager,
        scheduler: SchedulerService,
        team_manager: TeamManager,
        worktree_manager: WorktreeManager,
        hook_engine: HookEngine | None,
        tool_job_runner: BackgroundToolRunner,
        prompt_job_runner: PromptJobRunner,
        task_manager: TaskManager,
        trace_manager: TraceManager,
        memory_manager: MemoryManager,
        session_manager: SessionManager | None = None,
        session: Session | None = None,
        skill_loader: SkillLoader | None = None,
        agent_loader: AgentLoader | None = None,
        load_skill_tool: LoadSkill | None = None,
        install_skill_tool: InstallSkillTool | None = None,
        mcp_configs: list[MCPServerConfig] | None = None,
        mcp_manager: MCPManager | None = None,
    ) -> RuntimeContainer:
        event_bus = RuntimeEventBus()
        agent.runtime_event_sink = event_bus.emit
        job_manager.event_sink = event_bus.emit
        original_dispatch = scheduler._on_job_created

        def dispatch(job: Job) -> None:
            event_bus.emit(
                RuntimeEventType.SCHEDULE_FIRED.value,
                {
                    "schedule_id": job.schedule_id,
                    "job_id": job.id,
                    "kind": job.kind.value,
                    "name": job.name,
                },
            )
            event_bus.emit(
                RuntimeEventType.JOB_CREATED.value,
                {
                    "job_id": job.id,
                    "kind": job.kind.value,
                    "name": job.name,
                    "status": job.status.value,
                },
            )
            if original_dispatch is not None:
                original_dispatch(job)

        scheduler._on_job_created = dispatch
        return cls(
            client=client,
            agent=agent,
            registry=registry,
            permission_checker=permission_checker,
            job_manager=job_manager,
            scheduler=scheduler,
            team_manager=team_manager,
            worktree_manager=worktree_manager,
            hook_engine=hook_engine,
            recovery_controller=agent.recovery_controller,
            event_bus=event_bus,
            tool_job_runner=tool_job_runner,
            prompt_job_runner=prompt_job_runner,
            task_manager=task_manager,
            trace_manager=trace_manager,
            memory_manager=memory_manager,
            session_manager=session_manager,
            session=session,
            skill_loader=skill_loader,
            agent_loader=agent_loader,
            load_skill_tool=load_skill_tool,
            install_skill_tool=install_skill_tool,
            mcp_configs=list(mcp_configs or []),
            mcp_manager=mcp_manager,
        )

    async def initialize_mcp(self) -> ConnectResult:
        if self.mcp_result is not None:
            return self.mcp_result
        if not self.mcp_configs:
            self.mcp_result = ConnectResult()
            return self.mcp_result
        manager = MCPManager()
        manager.load_configs(self.mcp_configs)
        result = await manager.register_all_tools(self.registry)
        self.mcp_manager = manager
        self.mcp_result = result
        parts: list[str] = []
        for server in result.servers:
            section = f"## {server.name}\n"
            if server.instructions:
                section += server.instructions
            else:
                tool_names = [
                    tool.name
                    for tool in self.registry.list_tools()
                    if tool.name.startswith(f"mcp__{server.name}__")
                ]
                if tool_names:
                    section += "Available tools: " + ", ".join(tool_names)
            parts.append(section)
        content = ""
        if parts:
            content = (
                "The following MCP servers have provided instructions "
                "for how to use their tools and resources:\n\n"
                + "\n\n".join(parts)
            )
        self.agent.set_mcp_prompt_state(content)
        return self.mcp_result

    async def shutdown(self) -> None:
        if self.mcp_init_task is not None and not self.mcp_init_task.done():
            self.mcp_init_task.cancel()
            await asyncio.gather(self.mcp_init_task, return_exceptions=True)
        await self.scheduler.stop()
        await self.tool_job_runner.stop()
        await self.prompt_job_runner.stop()
        await self.task_manager.shutdown()
        await self.team_manager.shutdown()
        if self.mcp_manager is not None:
            await self.mcp_manager.shutdown()
        if self.stale_cleanup_task is not None and not self.stale_cleanup_task.done():
            self.stale_cleanup_task.cancel()
            await asyncio.gather(self.stale_cleanup_task, return_exceptions=True)
        if self.hook_engine is not None:
            from braincode.hooks import HookContext

            await self.hook_engine.run_hooks(
                "shutdown", HookContext(event_name="shutdown")
            )
        if self.session is not None:
            self.session.close()


def _format_skill_catalog(catalog: list[tuple[str, str]]) -> str:
    if not catalog:
        return ""
    lines = ["You can use the following Skills:", ""]
    lines.extend(f"- {name}: {description}" for name, description in catalog)
    lines.extend(
        [
            "",
            "If the user's request matches a Skill, call LoadSkill to activate it.",
        ]
    )
    return "\n".join(lines)


def _format_agent_catalog(
    catalog: list[tuple[str, str]], enable_fork: bool
) -> str:
    if not catalog:
        return ""
    lines = [
        "## Available Sub-Agent Types",
        "",
        "Use the Agent tool with subagent_type parameter to delegate tasks:",
        "",
    ]
    lines.extend(f"- **{name}**: {description}" for name, description in catalog)
    if enable_fork:
        lines.extend(
            [
                "",
                "Leave subagent_type empty to fork the current conversation "
                "(inherits full dialog history).",
            ]
        )
    lines.extend(
        [
            "",
            "IMPORTANT: Sub-agents run in the background. After calling the Agent "
            "tool, report the task ID and end your turn; completion is delivered "
            "automatically.",
        ]
    )
    return "\n".join(lines)


def build_runtime(
    *,
    providers: list[ProviderConfig],
    provider: ProviderConfig | None = None,
    permission_mode: PermissionMode = PermissionMode.DEFAULT,
    hook_engine: HookEngine | None = None,
    work_dir: str | None = None,
    worktree_config: WorktreeConfig | None = None,
    sandbox_config: SandboxAppConfig | None = None,
    recovery_config: RecoveryConfig | None = None,
    scheduler_config: SchedulerConfig | None = None,
    plugin_config: PluginConfig | None = None,
    mcp_servers: list[MCPServerConfig] | None = None,
    enable_fork: bool = False,
    enable_verification_agent: bool = False,
    teammate_mode: str = "",
    enable_coordinator_mode: bool = False,
    is_interactive: bool = True,
    support_user_questions: bool = False,
    client: LLMClient | None = None,
    registry: ToolRegistry | None = None,
    file_cache: Any = None,
) -> RuntimeContainer:
    provider = provider or providers[0]
    work_dir = os.path.abspath(work_dir or os.getcwd())
    worktree_config = worktree_config or WorktreeConfig()
    sandbox_config = sandbox_config or SandboxAppConfig()
    recovery_config = recovery_config or RecoveryConfig()
    scheduler_config = scheduler_config or SchedulerConfig()
    plugin_config = plugin_config or PluginConfig()
    event_bus = RuntimeEventBus()
    client = client or create_client(provider)
    registry = registry or create_default_registry(file_cache=file_cache)

    home = Path.home()
    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(work_dir),
        rule_engine=RuleEngine(
            user_rules_path=home / ".braincode" / "permissions.yaml",
            project_rules_path=Path(work_dir) / ".braincode" / "permissions.yaml",
            local_rules_path=Path(work_dir) / ".braincode" / "permissions.local.yaml",
        ),
        mode=permission_mode,
        sandbox_enabled=(sandbox_config.enabled and sandbox_config.auto_allow),
    )
    plugin_report = load_plugins(
        registry,
        work_dir=work_dir,
        plugin_config=plugin_config,
        permission_checker=checker,
        services={"runtime": None},
    )
    if plugin_config.strict and plugin_report.errors:
        raise RuntimeError("Tool plugin loading failed: " + "; ".join(plugin_report.errors))
    if sandbox_config.enabled:
        from braincode.sandbox import SandboxConfig, create_sandbox

        os_sandbox = create_sandbox()
        bash_tool = registry.get("Bash")
        if os_sandbox and os_sandbox.available() and bash_tool is not None:
            bash_tool.sandbox = os_sandbox
            bash_tool.sandbox_config = SandboxConfig(
                allow_write=[work_dir, "/tmp"],
                deny_write=[
                    f"{work_dir}/.braincode/config.yaml",
                    f"{work_dir}/.braincode/permissions.local.yaml",
                ],
                network_enabled=sandbox_config.network_enabled,
            )

    memory_manager = MemoryManager(work_dir)
    session_manager = SessionManager(work_dir)
    session_manager.cleanup()
    session = session_manager.create()
    recovery_controller = build_recovery_controller(
        client, providers, recovery_config
    )
    agent = Agent(
        client=client,
        registry=registry,
        protocol=provider.protocol,
        work_dir=work_dir,
        permission_checker=checker,
        context_window=provider.get_context_window(),
        instructions_content=load_instructions(work_dir),
        memory_manager=memory_manager,
        hook_engine=hook_engine,
        recovery_controller=recovery_controller,
        runtime_event_sink=event_bus.emit,
    )
    agent.session_id = session.session_id

    skill_loader = SkillLoader(work_dir)
    skill_loader.load_all()
    load_skill_tool = LoadSkill()
    load_skill_tool.set_loader(skill_loader)
    load_skill_tool.set_agent(agent)
    registry.register(load_skill_tool)
    install_skill_tool = InstallSkillTool()
    install_skill_tool.set_loader(skill_loader)
    registry.register(install_skill_tool)
    registry.register(ToolSearchTool(registry))
    if support_user_questions:
        registry.register(AskUserTool())

    exit_plan_tool = ExitPlanModeTool(
        is_plan_mode=lambda: agent.plan_mode,
        plan_exists=lambda: agent._get_plan_path().exists(),
    )
    registry.register(exit_plan_tool)

    worktree_manager = WorktreeManager(
        repo_root=work_dir,
        symlink_directories=worktree_config.symlink_directories,
    )
    restored = worktree_manager.restore_session()
    if restored is not None:
        agent.work_dir = restored.worktree_path

    job_manager = JobManager.for_project(work_dir, event_sink=event_bus.emit)
    task_manager = TaskManager(job_manager)
    trace_manager = TraceManager()
    agent_loader = AgentLoader(
        work_dir, enable_verification=enable_verification_agent
    )
    agent_loader.load_all()
    team_manager = TeamManager(
        worktree_manager=worktree_manager,
        trace_manager=trace_manager,
        job_manager=job_manager,
    )

    tool_job_runner = BackgroundToolRunner(
        job_manager, registry, checker, work_dir=work_dir
    )

    async def run_scheduled_prompt(job: Job, prompt: str) -> str:
        scheduled_client = create_client(provider)
        scheduled_session = session_manager.create()
        scheduled_registry = ToolRegistry()
        for tool in registry.list_tools():
            if tool.name != "AskUserQuestion":
                scheduled_registry.register_from(registry, tool)
        scheduled_agent = Agent(
            client=scheduled_client,
            registry=scheduled_registry,
            protocol=provider.protocol,
            work_dir=job.worktree_path or work_dir,
            permission_checker=checker,
            context_window=provider.get_context_window(),
            instructions_content=load_instructions(work_dir),
            memory_manager=memory_manager,
            hook_engine=hook_engine,
            recovery_controller=build_recovery_controller(
                scheduled_client, providers, recovery_config
            ),
            runtime_event_sink=event_bus.emit,
        )
        scheduled_agent.session_id = scheduled_session.session_id
        try:
            return await scheduled_agent.run_to_completion(prompt)
        finally:
            scheduled_session.close()

    prompt_job_runner = PromptJobRunner(
        job_manager,
        run_scheduled_prompt,
        output_dir=Path(work_dir) / ".braincode" / "job-output",
    )

    def dispatch_scheduled_job(job: Job) -> None:
        event_bus.emit(
            RuntimeEventType.SCHEDULE_FIRED.value,
            {
                "schedule_id": job.schedule_id,
                "job_id": job.id,
                "kind": job.kind.value,
                "name": job.name,
            },
        )
        event_bus.emit(
            RuntimeEventType.JOB_CREATED.value,
            {
                "job_id": job.id,
                "kind": job.kind.value,
                "name": job.name,
                "status": job.status.value,
            },
        )
        if job.kind == JobKind.TOOL:
            tool_job_runner.submit_job(job)
        elif job.kind == JobKind.PROMPT:
            prompt_job_runner.submit_job(job)

    scheduler = SchedulerService(
        job_manager.store,
        poll_interval_seconds=scheduler_config.poll_interval_seconds,
        on_job_created=dispatch_scheduled_job,
        default_timezone=scheduler_config.timezone,
        default_misfire_policy=MisfirePolicy(
            scheduler_config.default_misfire_policy
        ),
        default_overlap_policy=OverlapPolicy(
            scheduler_config.default_overlap_policy
        ),
    )
    bash_tool = registry.get("Bash")
    if bash_tool is not None:
        bash_tool.work_dir = work_dir
        bash_tool.background_runner = tool_job_runner

    agent_tool = AgentTool(
        agent_loader=agent_loader,
        task_manager=task_manager,
        trace_manager=trace_manager,
        parent_agent=agent,
        enable_fork=enable_fork,
        provider_config=provider,
        worktree_manager=worktree_manager,
        team_manager=team_manager,
    )
    registry.register(agent_tool)
    registry.register(
        TeamCreateTool(
            team_manager=team_manager,
            parent_agent=agent,
            teammate_mode=(teammate_mode or ("in-process" if not is_interactive else "")),
            is_interactive=is_interactive,
            enable_coordinator_mode=enable_coordinator_mode,
        )
    )
    registry.register(TeamDeleteTool(team_manager=team_manager, parent_agent=agent))
    registry.register(SyntheticOutputTool())
    agent._team_manager = team_manager
    agent.notification_fn = team_manager.drain_lead_mailbox

    for job_tool in (
        JobCreateTool(
            job_manager, tool_job_runner, prompt_job_runner, task_manager
        ),
        JobGetTool(job_manager),
        JobListTool(job_manager),
        JobCancelTool(
            job_manager, tool_job_runner, prompt_job_runner, task_manager
        ),
        CronCreateTool(scheduler, tool_job_runner),
        CronListTool(scheduler),
        CronDeleteTool(scheduler),
    ):
        registry.register(job_tool)

    agent.register_prompt_state_provider(JobPromptStateProvider(job_manager))
    agent.register_prompt_state_provider(CronPromptStateProvider(scheduler))
    agent.register_prompt_state_provider(TeamPromptStateProvider(team_manager))
    agent.register_prompt_state_provider(
        WorktreePromptStateProvider(worktree_manager)
    )
    agent.set_skill_catalog(_format_skill_catalog(skill_loader.get_catalog()))
    agent_catalog = agent_loader.list_agents()
    agent.set_agent_catalog(
        _format_agent_catalog(agent_catalog, enable_fork),
        catalog_list=agent_catalog,
    )

    tool_job_runner.start_pending()
    prompt_job_runner.start_pending()
    if scheduler_config.enabled:
        scheduler.start()

    return RuntimeContainer(
        client=client,
        agent=agent,
        registry=registry,
        permission_checker=checker,
        job_manager=job_manager,
        scheduler=scheduler,
        team_manager=team_manager,
        worktree_manager=worktree_manager,
        hook_engine=hook_engine,
        recovery_controller=recovery_controller,
        event_bus=event_bus,
        tool_job_runner=tool_job_runner,
        prompt_job_runner=prompt_job_runner,
        task_manager=task_manager,
        trace_manager=trace_manager,
        memory_manager=memory_manager,
        session_manager=session_manager,
        session=session,
        skill_loader=skill_loader,
        agent_loader=agent_loader,
        load_skill_tool=load_skill_tool,
        install_skill_tool=install_skill_tool,
        mcp_configs=list(mcp_servers or []),
    )
