# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from braincode.config import ConfigError, load_config
from braincode.hooks import HookConfigError, HookEngine, load_hooks
from braincode.permissions import PermissionMode


def main() -> None:
    # 先确保 .braincode/ 目录存在，否则下面写 debug.log 会因目录不存在而崩溃
    Path(".braincode").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        filename=".braincode/debug.log",
        filemode="w",
    )

    parser = argparse.ArgumentParser(prog="braincode", description="Braincode AI coding assistant")
    parser.add_argument(
        "--mode",
        choices=[m.value for m in PermissionMode],
        default=None,
        help="Permission mode (overrides config.yaml)",
    )
    parser.add_argument(
        "-p",
        metavar="PROMPT",
        default=None,
        help="Run non-interactively: execute the prompt and print the result to stdout",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "stream-json"],
        default="text",
        help="Output format for -p mode: 'text' (default) prints final text, 'stream-json' emits NDJSON events",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        default=False,
        help="Start in remote mode: WebSocket server on 0.0.0.0:18888 with browser UI",
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    mode_str = args.mode if args.mode else config.permission_mode
    permission_mode = PermissionMode(mode_str)

    try:
        hooks = load_hooks(config.raw_hooks)
    except HookConfigError as e:
        print(f"Hook config error: {e}", file=sys.stderr)
        sys.exit(1)

    hook_engine = HookEngine(hooks) if hooks else None

    if args.p is not None:
        output_format = getattr(args, "output_format", "text")
        asyncio.run(_run_prompt(config, permission_mode, hook_engine, args.p, output_format))
        return

    # Remote 模式：启动 WebSocket 服务器，浏览器访问 http://localhost:18888
    if args.remote:
        from braincode.remote import RemoteServer

        server = RemoteServer(
            providers=config.providers,
            mcp_servers=config.mcp_servers,
            hook_engine=hook_engine,
            recovery_config=config.recovery,
            scheduler_config=config.scheduler,
            sandbox_config=config.sandbox,
        )
        asyncio.run(server.run())
        return

    from braincode.app import BraincodeApp
    from braincode.driver import NoAltScreenDriver

    app = BraincodeApp(
        providers=config.providers,
        permission_mode=permission_mode,
        mcp_servers=config.mcp_servers,
        hook_engine=hook_engine,
        enable_fork=config.enable_fork,
        enable_verification_agent=config.enable_verification_agent,
        worktree_config=config.worktree,
        teammate_mode=config.teammate_mode,
        enable_coordinator_mode=config.enable_coordinator_mode,
        driver_class=NoAltScreenDriver,
        sandbox_config=config.sandbox,
        recovery_config=config.recovery,
        scheduler_config=config.scheduler,
    )
    app.run()


async def _run_prompt(config, permission_mode, hook_engine, prompt: str, output_format: str = "text") -> None:
    from braincode.agent import (
        Agent,
        CompactNotification,
        ErrorEvent,
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
    from braincode.conversation import ConversationManager
    from braincode.memory import MemoryManager
    from braincode.memory.instructions import load_instructions
    from braincode.permissions import (
        DangerousCommandDetector,
        PathSandbox,
        PermissionChecker,
        RuleEngine,
    )
    from braincode.tools import create_default_registry
    from braincode.agents.loader import AgentLoader
    from braincode.agents.task_manager import TaskManager
    from braincode.agents.trace import TraceManager
    from braincode.tools.agent_tool import AgentTool
    from braincode.tools.impl.tool_search import ToolSearchTool
    from braincode.teams.manager import TeamManager
    from braincode.teams.models import BackendType
    from braincode.tools.team_create import TeamCreateTool
    from braincode.tools.team_delete import TeamDeleteTool
    from braincode.worktree import WorktreeManager
    from braincode.config import WorktreeConfig
    from braincode.jobs import JobManager
    from braincode.jobs import (
        BackgroundToolRunner,
        JobKind,
        MisfirePolicy,
        OverlapPolicy,
        PromptJobRunner,
        SchedulerService,
    )
    from braincode.recovery import build_recovery_controller
    from braincode.memory.session import SessionManager
    from braincode.tools.job_tools import (
        CronCreateTool,
        CronDeleteTool,
        CronListTool,
        JobCancelTool,
        JobCreateTool,
        JobGetTool,
        JobListTool,
    )

    is_json = output_format == "stream-json"

    def emit_json(obj: dict) -> None:
        """输出一行 NDJSON 到 stdout"""
        print(json.dumps(obj, ensure_ascii=False), flush=True)

    provider = config.providers[0]
    client = create_client(provider)
    # 第 2 层：尽力从 provider 自动拉取模型的 context window（缓存在 provider 上）。
    # 不会抛异常或阻塞启动；失败则退化到映射表。
    await resolve_context_window(provider)
    work_dir = os.getcwd()
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
        sandbox_enabled=(config.sandbox.enabled and config.sandbox.auto_allow),
    )

    instructions = load_instructions(work_dir)
    memory_manager = MemoryManager(work_dir)
    registry = create_default_registry()
    registry.register(ToolSearchTool(registry, protocol=provider.protocol))
    bash_tool = registry.get("Bash")
    if bash_tool is not None:
        bash_tool.work_dir = work_dir
    if config.sandbox.enabled and bash_tool is not None:
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
                network_enabled=config.sandbox.network_enabled,
            )

    agent = Agent(
        client=client,
        registry=registry,
        protocol=provider.protocol,
        work_dir=work_dir,
        permission_checker=checker,
        context_window=provider.get_context_window(),
        instructions_content=instructions,
        memory_manager=memory_manager,
        hook_engine=hook_engine,
        recovery_controller=build_recovery_controller(
            client, config.providers, config.recovery
        ),
    )

    wt_cfg = config.worktree or WorktreeConfig()
    wt_manager = WorktreeManager(
        repo_root=work_dir,
        symlink_directories=wt_cfg.symlink_directories,
    )
    trace_manager = TraceManager()
    job_manager = JobManager.for_project(work_dir)
    task_manager = TaskManager(job_manager)
    tool_job_runner = BackgroundToolRunner(
        job_manager, registry, checker, work_dir=work_dir
    )

    async def run_scheduled_prompt(job, scheduled_prompt: str) -> str:
        scheduled_client = create_client(provider)
        scheduled_session = SessionManager(work_dir).create()
        scheduled_registry = type(registry)()
        for scheduled_tool in registry.list_tools():
            if scheduled_tool.name != "AskUserQuestion":
                scheduled_registry.register(scheduled_tool)
        scheduled_agent = Agent(
            client=scheduled_client,
            registry=scheduled_registry,
            protocol=provider.protocol,
            work_dir=job.worktree_path or work_dir,
            permission_checker=checker,
            context_window=provider.get_context_window(),
            instructions_content=instructions,
            memory_manager=memory_manager,
            hook_engine=hook_engine,
            recovery_controller=build_recovery_controller(
                scheduled_client, config.providers, config.recovery
            ),
        )
        scheduled_agent.session_id = scheduled_session.session_id
        try:
            return await scheduled_agent.run_to_completion(scheduled_prompt)
        finally:
            scheduled_session.close()

    prompt_job_runner = PromptJobRunner(
        job_manager,
        run_scheduled_prompt,
        output_dir=Path(work_dir) / ".braincode" / "job-output",
    )

    def dispatch_scheduled_job(job) -> None:
        if job.kind == JobKind.TOOL:
            tool_job_runner.submit_job(job)
        elif job.kind == JobKind.PROMPT:
            prompt_job_runner.submit_job(job)

    scheduler = SchedulerService(
        job_manager.store,
        poll_interval_seconds=config.scheduler.poll_interval_seconds,
        on_job_created=dispatch_scheduled_job,
        default_timezone=config.scheduler.timezone,
        default_misfire_policy=MisfirePolicy(
            config.scheduler.default_misfire_policy
        ),
        default_overlap_policy=OverlapPolicy(
            config.scheduler.default_overlap_policy
        ),
    )
    from braincode.prompt_state import (
        CronPromptStateProvider,
        JobPromptStateProvider,
        TeamPromptStateProvider,
        WorktreePromptStateProvider,
    )
    agent.register_prompt_state_provider(JobPromptStateProvider(job_manager))
    agent.register_prompt_state_provider(CronPromptStateProvider(scheduler))
    if bash_tool is not None:
        bash_tool.background_runner = tool_job_runner
    for job_tool in (
        JobCreateTool(job_manager, tool_job_runner, prompt_job_runner),
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
    tool_job_runner.start_pending()
    prompt_job_runner.start_pending()
    if config.scheduler.enabled:
        scheduler.start()
    agent_loader = AgentLoader(work_dir, enable_verification=config.enable_verification_agent)
    agent_loader.load_all()
    team_manager = TeamManager(
        worktree_manager=wt_manager,
        trace_manager=trace_manager,
        job_manager=job_manager,
    )
    agent.register_prompt_state_provider(WorktreePromptStateProvider(wt_manager))
    agent.register_prompt_state_provider(TeamPromptStateProvider(team_manager))

    agent_tool = AgentTool(
        agent_loader=agent_loader,
        task_manager=task_manager,
        trace_manager=trace_manager,
        parent_agent=agent,
        enable_fork=config.enable_fork,
        provider_config=provider,
        worktree_manager=wt_manager,
        team_manager=team_manager,
    )
    registry.register(agent_tool)
    registry.register(TeamCreateTool(
        team_manager=team_manager,
        parent_agent=agent,
        teammate_mode="in-process",
        is_interactive=False,
        enable_coordinator_mode=config.enable_coordinator_mode,
    ))
    registry.register(TeamDeleteTool(team_manager=team_manager, parent_agent=agent))

    def drain_notifications() -> list[str]:
        notes: list[str] = []
        for t in task_manager.poll_completed():
            notes.append(
                f"<task-notification>\n<task_id>{t.id}</task_id>\n"
                f"<status>{t.status}</status>\n<result>{t.result}</result>\n"
                f"</task-notification>"
            )
        notes.extend(team_manager.drain_lead_mailbox())
        return notes

    def drain_mailbox_only() -> list[str]:
        return team_manager.drain_lead_mailbox()

    agent.notification_fn = drain_mailbox_only

    from braincode.runtime import RuntimeContainer
    runtime = RuntimeContainer.adopt(
        client=client,
        agent=agent,
        registry=registry,
        permission_checker=checker,
        job_manager=job_manager,
        scheduler=scheduler,
        team_manager=team_manager,
        worktree_manager=wt_manager,
        hook_engine=hook_engine,
        tool_job_runner=tool_job_runner,
        prompt_job_runner=prompt_job_runner,
        task_manager=task_manager,
        trace_manager=trace_manager,
        memory_manager=memory_manager,
        agent_loader=agent_loader,
        mcp_configs=config.mcp_servers,
    )

    def on_runtime_event(event) -> None:
        if is_json:
            emit_json({"type": "runtime_event", "event": event.to_dict()})
        elif event.type.value in {"retry_started", "provider_switched"}:
            print(
                f"[{event.type.value}] {event.payload.get('reason', '')}",
                file=sys.stderr,
                flush=True,
            )

    runtime.event_bus.subscribe(on_runtime_event)
    mcp_result = await runtime.initialize_mcp()
    for error in mcp_result.errors:
        if is_json:
            emit_json({"type": "error", "message": error})
        else:
            print(f"MCP warning: {error}", file=sys.stderr, flush=True)

    # 使用事件驱动的 agent.run()，支持 text 和 stream-json 两种输出格式
    conv = ConversationManager()
    conv.add_user_message(prompt)

    start = time.monotonic()
    text_buf = ""
    total_input = 0
    total_output = 0
    tool_calls: list[dict] = []

    async for event in agent.run(conv):
        if isinstance(event, StreamText):
            text_buf += event.text
            if is_json:
                emit_json({"type": "assistant", "text": event.text})

        elif isinstance(event, ThinkingText):
            if is_json:
                emit_json({"type": "thinking", "text": event.text})

        elif isinstance(event, ToolUseEvent):
            tool_calls.append({"name": event.tool_name, "is_error": False})
            if is_json:
                emit_json({
                    "type": "tool_use",
                    "tool_name": event.tool_name,
                    "tool_id": event.tool_id,
                    "args": event.arguments,
                })

        elif isinstance(event, ToolResultEvent):
            # 回填最后一个同名 tool_call 的 is_error
            if tool_calls:
                tool_calls[-1]["is_error"] = event.is_error
            if is_json:
                emit_json({
                    "type": "tool_result",
                    "tool_name": event.tool_name,
                    "tool_id": event.tool_id,
                    "output": event.output,
                    "is_error": event.is_error,
                    "elapsed": round(event.elapsed, 3),
                })

        elif isinstance(event, UsageEvent):
            total_input = event.input_tokens
            total_output = event.output_tokens
            if is_json:
                emit_json({
                    "type": "usage",
                    "input_tokens": event.input_tokens,
                    "output_tokens": event.output_tokens,
                })

        elif isinstance(event, TurnComplete):
            if is_json:
                emit_json({"type": "turn_complete", "turn": event.turn})

        elif isinstance(event, LoopComplete):
            # 最终结果：stream-json 输出 result 行，text 模式直接打印文本
            elapsed_ms = int((time.monotonic() - start) * 1000)
            if is_json:
                emit_json({
                    "type": "result",
                    "result": text_buf,
                    "duration_ms": elapsed_ms,
                    "num_turns": event.total_turns,
                    "tool_calls": tool_calls,
                    "usage": {
                        "input_tokens": total_input,
                        "output_tokens": total_output,
                    },
                    "stop_reason": "end_turn",
                })
            else:
                print(text_buf, end="", flush=True)
            break

        elif isinstance(event, ErrorEvent):
            if is_json:
                emit_json({"type": "error", "message": event.message})
            else:
                print(f"Error: {event.message}", file=sys.stderr, flush=True)

        elif isinstance(event, CompactNotification):
            if is_json:
                emit_json({"type": "compact", "message": event.message})

        elif isinstance(event, RetryEvent):
            pass  # RuntimeEventBus owns retry/provider output.

        elif isinstance(event, PermissionRequest):
            # -p 非交互模式：自动批准所有权限请求
            event.future.set_result(PermissionResponse.ALLOW)

    # 如果有 team 在运行，轮询等待 teammate 完成
    if not team_manager._teams:
        await runtime.shutdown()
        return

    for i in range(90):
        await asyncio.sleep(2)
        running = {k: not t.done() for k, t in task_manager._async_tasks.items()}
        completed_ids = [t.id for t in task_manager._tasks.values() if t.status != "running"]
        print(f"[poll {i}] running={running} completed={completed_ids} teams={list(team_manager._teams.keys())} queue_size={task_manager._notify_queue.qsize()}", file=sys.stderr, flush=True)
        notes = drain_notifications()
        if not notes:
            has_running = any(v for v in running.values())
            if not has_running:
                print(f"[poll {i}] no running tasks, breaking", file=sys.stderr, flush=True)
                break
            continue
        for note in notes:
            conv.add_system_reminder(note)
        # 后续 team 轮询仍用 run_to_completion，避免重复事件循环
        last_result = await agent.run_to_completion(
            "Teammate notifications received. Process them and continue.", conv
        )
        if is_json:
            emit_json({"type": "assistant", "text": last_result})
        else:
            print(last_result, flush=True)

    await runtime.shutdown()


if __name__ == "__main__":
    main()
