"""Chat handling for the `!auto_approve` command, which skips tool approval in one thread for a while."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from mindroom.approval.auto_approve_windows import (
    AUTO_APPROVE_MAX_DURATION,
    auto_approve_expires_at,
    end_auto_approve,
    room_auto_approve_windows,
    start_auto_approve,
)
from mindroom.approval.inbound import handle_tool_approval_action
from mindroom.approval.manager import get_approval_store
from mindroom.approval.tool_approval import resolve_tool_approval_approver
from mindroom.matrix.event_types import AUTO_APPROVE_WINDOWS_EVENT_TYPE
from mindroom.steward_duties import moderator_rights_message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio
    import structlog

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_USAGE = "Usage: `/auto_approve <minutes>`, `/auto_approve <hours>h`, `/auto_approve off`, or `/auto_approve` to show the current window"
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


async def _publish_windows(
    put_room_state: Callable[..., Awaitable[bool | None]],
    runtime_paths: RuntimePaths,
    room_id: str,
    logger: structlog.stdlib.BoundLogger,
    opening: dict[tuple[str | None, str], datetime] | None = None,
) -> bool | None:
    """Publish the room's open windows, and the one ``opening``, as the room steward's own state, for clients.

    None when the room refuses the write, False when it fails for another reason.
    """
    windows = {(w.thread_id, w.requester_id): w.expires_at for w in room_auto_approve_windows(runtime_paths, room_id)}
    content = {
        "windows": [
            {"thread_id": thread_id, "requester_id": requester_id, "expires_at": expires_at.isoformat()}
            for (thread_id, requester_id), expires_at in (windows | (opening or {})).items()
        ],
    }
    try:
        return await put_room_state(room_id, AUTO_APPROVE_WINDOWS_EVENT_TYPE, content, state_key=None)
    except Exception:
        logger.warning("Publishing auto-approve windows failed", room_id=room_id, exc_info=True)
        return False


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


async def handle_auto_approve_command(  # noqa: PLR0911 - one answer per outcome
    args_text: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    put_room_state: Callable[..., Awaitable[bool | None]],
    room: nio.MatrixRoom,
    thread_id: str | None,
    requester_user_id: str,
    logger: structlog.stdlib.BoundLogger,
    membership_index: AgentReplyMembershipIndex,
) -> str:
    """Show, start, or end the requester's auto-approve window in this conversation, and publish the room's windows."""
    approver_id = resolve_tool_approval_approver(config, runtime_paths, requester_user_id)
    if approver_id is None:
        return "❌ Only a person can turn on auto-approve."
    requested = args_text.strip().lower()
    # A room without threads is one conversation, so its window covers the whole room.
    place = "this thread" if thread_id is not None else "this room"

    if not requested:
        expires_at = auto_approve_expires_at(
            runtime_paths,
            room_id=room.room_id,
            thread_id=thread_id,
            requester_id=approver_id,
        )
        return (
            f"Auto-approve for all your tool calls is off in {place}.\n\n{_USAGE}"
            if expires_at is None
            else f"⚠️ Auto-approve for all your tool calls is on in {place} until {_until(expires_at, config)}.\n\nUse `/auto_approve off` to end it."
        )

    if requested == "off":
        ended = end_auto_approve(runtime_paths, room_id=room.room_id, thread_id=thread_id, requester_id=approver_id)
        # A window always ends here; an `off` with none open still clears what the room shows of an earlier failure.
        published = await _publish_windows(put_room_state, runtime_paths, room.room_id, logger)
        if published is False:
            return (
                f"⚠️ Auto-approve for all your tool calls is off in {place}, but the room may still show it as on. "
                "Run `/auto_approve off` again to retry."
            )
        if not ended:
            return f"Auto-approve for all your tool calls is not on in {place}."
        if published is None:
            return moderator_rights_message("show that auto-approve is off")
        return (
            f"✅ Auto-approve for all your tool calls is off in {place}; they follow the usual approval rules again. "
            "A tool you auto-approved from an approval card stays approved until you stop it on that card or its time ends."
        )

    duration = _parse_duration(requested)
    if duration is None or not timedelta(minutes=1) <= duration <= AUTO_APPROVE_MAX_DURATION:
        return f"❌ Choose a duration from 1 minute to 24 hours, such as `30` or `2h`.\n\n{_USAGE}"
    # The room shows the window before it opens here, so no tool call runs unasked under a window the room refuses.
    now = datetime.now(UTC)
    opening = {(thread_id, approver_id): now + duration}
    published = await _publish_windows(put_room_state, runtime_paths, room.room_id, logger, opening)
    if published is None:
        return moderator_rights_message("turn on auto-approve")
    if not published:
        return "⚠️ Auto-approve was not turned on because publishing it failed. Run the command again to retry."
    expires_at = start_auto_approve(
        runtime_paths,
        room_id=room.room_id,
        thread_id=thread_id,
        requester_id=approver_id,
        duration=duration,
        now=now,
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
        f"⚠️ Auto-approve for all your tool calls is on in {place} until {_until(expires_at, config)}: "
        f"they run without asking for approval.{approved_text}\n\n"
        "Use `/auto_approve off` to end it."
    )
