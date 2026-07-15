# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable

from braincode.teams.lifecycle import TeammateState
from braincode.teams.mailbox import Mailbox, MailboxMessage, create_message
from braincode.teams.progress import TeammateProgress, random_verb

if TYPE_CHECKING:
    from braincode.agent import Agent
    from braincode.conversation import ConversationManager
    from braincode.teams.models import TeammateInfo

log = logging.getLogger(__name__)

IDLE_POLL_INTERVAL = 0.5
SHUTDOWN_PREFIX = "[shutdown]"
LEAD_NAME = "lead"


def _is_shutdown_request(msg: MailboxMessage) -> bool:
    return (
        msg.message_type == "shutdown_request"
        or msg.content.strip().startswith(SHUTDOWN_PREFIX)
    )


def _create_idle_notification(
    member_name: str,
    lead_agent_id: str,
    reason: str,
) -> MailboxMessage:
    return create_message(
        from_agent=member_name,
        to_agent=lead_agent_id,
        content=f"[idle] {member_name} (reason: {reason})",
        summary="idle",
    )


def _drain_pending_messages(
    mailbox: Mailbox,
    mailbox_key: str,
) -> tuple[str, bool]:
    msgs = mailbox.consume(mailbox_key)
    if not msgs:
        return "", False
    parts = ["You have new messages:\n"]
    shutdown_requested = False
    for msg in msgs:
        if _is_shutdown_request(msg):
            shutdown_requested = True
        else:
            parts.append(f"From {msg.from_agent}: {msg.content}\n")
    reminder = "\n".join(parts) if len(parts) > 1 else ""
    return reminder, shutdown_requested


async def _wait_for_next_prompt_or_shutdown(
    mailbox: Mailbox,
    mailbox_key: str,
) -> tuple[str, bool]:
    """Wait until a follow-up message or shutdown request is available."""

    while True:
        await asyncio.sleep(IDLE_POLL_INTERVAL)
        msgs = mailbox.consume(mailbox_key)
        if not msgs:
            continue

        has_shutdown = False
        follow_ups: list[MailboxMessage] = []
        for msg in msgs:
            if _is_shutdown_request(msg):
                has_shutdown = True
            else:
                follow_ups.append(msg)

        if has_shutdown:
            return "", True
        if not follow_ups:
            continue

        parts = ["You have new messages from your team:\n"]
        for msg in follow_ups:
            parts.append(f"From {msg.from_agent}: {msg.content}\n")
        return "\n".join(parts), False


class InProcessTeammateHandle:
    def __init__(
        self,
        agent: Agent,
        task: asyncio.Task[str],
        name: str,
        progress: TeammateProgress | None = None,
    ) -> None:
        self.agent = agent
        self.task = task
        self.name = name
        self.progress = progress

    @property
    def done(self) -> bool:
        return self.task.done()

    @property
    def result(self) -> str | None:
        if self.task.done():
            try:
                return self.task.result()
            except (asyncio.CancelledError, Exception):
                return None
        return None

    def cancel(self) -> None:
        if not self.task.done():
            self.task.cancel()


def spawn_inprocess_teammate(
    agent: Agent,
    prompt: str,
    name: str,
    conversation: ConversationManager | None = None,
    member: TeammateInfo | None = None,
    team_name: str = "",
    mailbox: Mailbox | None = None,
    mailbox_key: str = "",
    lead_agent_id: str = "",
    on_state_change: Callable[[TeammateState, str], None] | None = None,
) -> InProcessTeammateHandle:
    """Start an in-process teammate governed by the lifecycle state machine.

    With a mailbox the teammate remains alive after each turn and alternates
    between RUNNING and IDLE until shutdown. Without a mailbox it performs one
    turn and moves through STOPPING to STOPPED for backward compatibility.
    """

    progress = TeammateProgress(
        name=name,
        team_name=team_name,
        spinner_verb=random_verb(),
    )
    if member is not None:
        member.progress = progress
        progress.status = member.state.value

    resolved_mailbox_key = mailbox_key or (
        member.agent_id if member is not None else name
    )
    resolved_lead_id = lead_agent_id or LEAD_NAME

    def transition(state: TeammateState, reason: str) -> None:
        progress.status = state.value
        if on_state_change is not None:
            on_state_change(state, reason)
        elif member is not None:
            member.transition_to(state)

    def notify_lead(content: str, summary: str) -> None:
        if mailbox is None:
            return
        mailbox.write(
            resolved_lead_id,
            create_message(
                from_agent=name,
                to_agent=resolved_lead_id,
                content=content,
                summary=summary,
                message_type="text",
            ),
        )

    def on_event(event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "tool_use":
            progress.record_tool_use(
                event.get("toolName", ""), event.get("args", {})
            )
        elif event_type == "usage":
            usage = event.get("usage", {})
            progress.record_tokens(
                usage.get("inputTokens", 0),
                usage.get("outputTokens", 0),
            )
        elif event_type == "stream_text":
            text = event.get("text")
            if text:
                with progress._lock:
                    progress.last_message = text

    async def run() -> str:
        try:
            if conversation is not None:
                conv = conversation
            else:
                from braincode.conversation import ConversationManager

                conv = ConversationManager()

            next_prompt = prompt
            result = ""
            turn_number = 0

            while True:
                if mailbox is not None:
                    reminder, shutdown_requested = _drain_pending_messages(
                        mailbox, resolved_mailbox_key
                    )
                    if shutdown_requested:
                        transition(
                            TeammateState.STOPPING,
                            "shutdown requested before task start",
                        )
                        transition(
                            TeammateState.STOPPED,
                            "shutdown completed",
                        )
                        return result
                    if reminder:
                        conv.add_system_reminder(reminder)

                turn_number += 1
                transition(
                    TeammateState.RUNNING,
                    "initial task started"
                    if turn_number == 1
                    else "follow-up task started",
                )
                result = await agent.run_to_completion(
                    next_prompt, conv, event_callback=on_event
                )
                next_prompt = ""

                if mailbox is None:
                    transition(TeammateState.STOPPING, "one-shot task completed")
                    transition(TeammateState.STOPPED, "one-shot teammate stopped")
                    return result

                transition(
                    TeammateState.IDLE,
                    "task completed; awaiting work",
                )
                mailbox.write(
                    resolved_lead_id,
                    _create_idle_notification(
                        name, resolved_lead_id, "available"
                    ),
                )

                next_prompt, shutdown = await _wait_for_next_prompt_or_shutdown(
                    mailbox, resolved_mailbox_key
                )
                if shutdown:
                    transition(TeammateState.STOPPING, "shutdown requested")
                    transition(TeammateState.STOPPED, "shutdown completed")
                    return result

        except asyncio.CancelledError:
            transition(
                TeammateState.STOPPING,
                "runtime cancellation requested",
            )
            transition(
                TeammateState.STOPPED,
                "runtime cancellation completed",
            )
            raise
        except Exception as exc:
            transition(TeammateState.FAILED, str(exc))
            notify_lead(f"[failed] {name}: {exc}", f"{name} failed")
            raise

    task = asyncio.create_task(run(), name=f"teammate-{name}")
    log.info("Spawned in-process teammate %s (verb=%s)", name, progress.spinner_verb)
    return InProcessTeammateHandle(
        agent=agent,
        task=task,
        name=name,
        progress=progress,
    )
