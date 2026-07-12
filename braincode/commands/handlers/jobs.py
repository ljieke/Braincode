from __future__ import annotations

from braincode.commands.registry import Command, CommandContext, CommandType
from braincode.jobs import (
    BackgroundToolRunner,
    JobKind,
    JobManager,
    JobQuery,
    PromptJobRunner,
)


def create_jobs_command(
    manager: JobManager,
    tool_runner: BackgroundToolRunner | None = None,
    prompt_runner: PromptJobRunner | None = None,
    task_manager=None,
) -> Command:
    async def handler(ctx: CommandContext) -> None:
        parts = ctx.args.strip().split(maxsplit=1)
        if parts and parts[0] == "cancel":
            if len(parts) != 2:
                ctx.ui.add_system_message("用法: /jobs cancel <job-id>")
                return
            job = manager.store.get(parts[1])
            if job is None or job.is_terminal:
                ctx.ui.add_system_message(f"无法取消 Job: {parts[1]}")
                return
            if job.kind == JobKind.TOOL and tool_runner is not None:
                tool_runner.cancel(job.id)
            elif job.kind == JobKind.PROMPT and prompt_runner is not None:
                prompt_runner.cancel(job.id)
            elif job.kind == JobKind.AGENT and task_manager is not None:
                if not task_manager.cancel(job.id):
                    manager.cancel(job.id)
            else:
                manager.cancel(job.id)
            ctx.ui.add_system_message(f"已取消 Job: {job.id}")
            return
        jobs = manager.store.list(JobQuery(limit=30))
        ctx.ui.add_system_message(
            "\n".join(
                ["Jobs:"]
                + [
                    f"  [{job.id}] {job.kind.value:<6} {job.status.value:<10} {job.name}"
                    for job in jobs
                ]
            )
            if jobs
            else "没有 Job"
        )

    return Command(
        name="jobs",
        description="查看和取消持久 Job",
        type=CommandType.LOCAL,
        handler=handler,
        usage="/jobs [cancel <job-id>]",
    )
