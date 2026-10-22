"""Chat handling for the `!yolo` command, which skips tool approval in one thread for a while."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from mindroom.approval.inbound import handle_tool_approval_action
from mindroom.approval.manager import get_approval_store
from mindroom.approval.tool_approval import resolve_tool_approval_approver
from mindroom.approval.yolo import YOLO_MAX_DURATION, end_yolo, start_yolo, yolo_expires_at

if TYPE_CHECKING:
    from datetime import datetime

    import nio
    import structlog

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_USAGE = "Usage: `!yolo <minutes>`, `!yolo <hours>h`, `!yolo off`, or `!yolo` to show the current window"
# A bare number counts minutes; five digits already exceed the longest window.
_DURATION = re.compile(r"^(\d{1,5})\s*(m|min|mins|minutes?|h|hr|hrs|hours?)?$")


def _parse_duration(text: str) -> timedelta | None:
    match = _DURATION.match(text)
    if match is None:
        return None
    amount = int(match.group(1))
    return timedelta(hours=amount) if (match.group(2) or "").startswith("h") else timedelta(minutes=amount)


def _until(expires_at: datetime, config: Config) -> str:
    return expires_at.astimezone(ZoneInfo(config.timezone)).strftime("%Y-%m-%d %H:%M %Z")


async def _approve_pending_cards(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    room: nio.MatrixRoom,
    thread_id: str | None,
    approver_id: str,
    logger: structlog.stdlib.BoundLogger,
    membership_index: AgentReplyMembershipIndex,
) -> int:
    """Approve the requester's cards already waiting in this conversation; return how many it approved."""
    manager = get_approval_store()
    if manager is None:
        return 0
    approved = 0
    for card_event_id in await manager.pending_card_event_ids(
        room_id=room.room_id,
        thread_id=thread_id,
        approver_user_id=approver_id,
    ):
        approved += await handle_tool_approval_action(
            room=room,
            sender_id=approver_id,
            config=config,
            runtime_paths=runtime_paths,
            orchestrator=None,
            logger=logger,
            approval_event_id=card_event_id,
            status="approved",
            reason=None,
            membership_index=membership_index,
        )
    return approved


async def handle_yolo_command(
    args_text: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    room: nio.MatrixRoom,
    thread_id: str | None,
    requester_user_id: str,
    logger: structlog.stdlib.BoundLogger,
    membership_index: AgentReplyMembershipIndex,
) -> str:
    """Show, start, or end the requester's YOLO window in this conversation."""
    approver_id = resolve_tool_approval_approver(config, runtime_paths, requester_user_id)
    if approver_id is None:
        return "❌ Only a person can turn on YOLO mode."
    requested = args_text.strip().lower()
    # A room without threads is one conversation, so its window covers the whole room.
    place = "this thread" if thread_id is not None else "this room"

    if not requested:
        expires_at = yolo_expires_at(runtime_paths, room_id=room.room_id, thread_id=thread_id, requester_id=approver_id)
        return (
            f"YOLO mode is off in {place}.\n\n{_USAGE}"
            if expires_at is None
            else f"⚠️ YOLO mode is on in {place} until {_until(expires_at, config)}.\n\nUse `!yolo off` to end it."
        )

    if requested == "off":
        return (
            f"✅ YOLO mode is off. Tool calls in {place} ask for approval again."
            if end_yolo(runtime_paths, room_id=room.room_id, thread_id=thread_id, requester_id=approver_id)
            else f"YOLO mode is not on in {place}."
        )

    duration = _parse_duration(requested)
    if duration is None or not timedelta(minutes=1) <= duration <= YOLO_MAX_DURATION:
        return f"❌ Choose a duration from 1 minute to 24 hours, such as `30` or `2h`.\n\n{_USAGE}"
    expires_at = start_yolo(
        runtime_paths,
        room_id=room.room_id,
        thread_id=thread_id,
        requester_id=approver_id,
        duration=duration,
    )
    approved = await _approve_pending_cards(
        config=config,
        runtime_paths=runtime_paths,
        room=room,
        thread_id=thread_id,
        approver_id=approver_id,
        logger=logger,
        membership_index=membership_index,
    )
    approved_text = f" Approved {approved} waiting tool call{'s' if approved != 1 else ''}." if approved else ""
    return (
        f"⚠️ YOLO mode is on in {place} until {_until(expires_at, config)}: "
        f"tool calls you request here run without asking for approval.{approved_text}\n\n"
        "Use `!yolo off` to end it."
    )
