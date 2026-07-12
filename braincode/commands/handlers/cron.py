from __future__ import annotations

from braincode.commands.registry import Command, CommandContext, CommandType
from braincode.jobs import SchedulerService


def create_cron_command(scheduler: SchedulerService) -> Command:
    async def handler(ctx: CommandContext) -> None:
        parts = ctx.args.strip().split(maxsplit=1)
        if parts and parts[0] == "delete":
            if len(parts) != 2:
                ctx.ui.add_system_message("用法: /cron delete <schedule-id>")
                return
            if scheduler.delete(parts[1]):
                ctx.ui.add_system_message(f"已删除 Cron: {parts[1]}")
            else:
                ctx.ui.add_system_message(f"未找到 Cron: {parts[1]}")
            return
        schedules = scheduler.list()
        ctx.ui.add_system_message(
            "\n".join(
                ["Cron schedules:"]
                + [
                    f"  [{item.id}] {item.cron_expression} {item.timezone} "
                    f"next={item.next_run_at.isoformat()} {item.name}"
                    for item in schedules
                ]
            )
            if schedules
            else "没有 Cron schedule"
        )

    return Command(
        name="cron",
        description="查看和删除 Cron schedule",
        type=CommandType.LOCAL,
        handler=handler,
        usage="/cron [delete <schedule-id>]",
    )
