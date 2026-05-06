"""Matrix approval-card parsing and event helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.visible_body import visible_content_from_content

PendingApprovalStatus = Literal["pending", "approved", "denied", "expired"]


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """Typed projection of one Matrix `io.mindroom.tool_approval` card."""

    approval_id: str
    card_event_id: str
    room_id: str
    card_sender_id: str
    requester_id: str
    approver_user_id: str
    tool_name: str
    arguments_preview: dict[str, Any]
    arguments_preview_truncated: bool
    timeout_seconds: int
    created_at_ms: int
    thread_id: str | None = None
    agent_name: str | None = None
    requested_at: str | None = None
    expires_at: str | None = None

    @classmethod
    def from_card_event(cls, event: dict[str, Any], *, room_id: str) -> PendingApproval:
        """Parse one Matrix approval card event into a typed read-only view."""
        if event.get("type") != "io.mindroom.tool_approval":
            msg = "Approval card event has the wrong event type."
            raise ValueError(msg)
        content = event.get("content")
        if not isinstance(content, dict):
            msg = "Approval card event is missing content."
            raise TypeError(msg)
        if _is_replace_content(content):
            msg = "Approval card event is a replacement edit, not an original card."
            raise ValueError(msg)

        event_id = _required_str(event, "event_id")
        sender = _required_str(event, "sender")
        approval_id = _content_str(content, "approval_id") or _content_str(content, "tool_call_id")
        tool_name = _content_str(content, "tool_name")
        approver_user_id = _content_str(content, "approver_user_id")
        if approval_id is None or tool_name is None or approver_user_id is None:
            msg = "Approval card event is missing required approval fields."
            raise ValueError(msg)

        arguments = content.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {"value": arguments}

        requested_at = _content_str(content, "requested_at")
        expires_at = _content_str(content, "expires_at")
        created_at_ms = _created_at_ms(event, requested_at)
        timeout_seconds = _timeout_seconds(requested_at, expires_at)
        requester_id = _content_str(content, "requester_id") or ""
        thread_id = _content_str(content, "thread_id")
        agent_name = _content_str(content, "agent_name")

        return cls(
            approval_id=approval_id,
            card_event_id=event_id,
            room_id=room_id,
            card_sender_id=sender,
            requester_id=requester_id,
            approver_user_id=approver_user_id,
            tool_name=tool_name,
            arguments_preview=cast("dict[str, Any]", arguments),
            arguments_preview_truncated=bool(content.get("arguments_truncated")),
            timeout_seconds=timeout_seconds,
            created_at_ms=created_at_ms,
            thread_id=thread_id,
            agent_name=agent_name,
            requested_at=requested_at,
            expires_at=expires_at,
        )

    def latest_status(self, latest_edit: dict[str, Any] | None) -> PendingApprovalStatus:
        """Return the visible approval status after applying the latest cached edit."""
        if latest_edit is None:
            return "pending"
        content = latest_edit.get("content")
        if not isinstance(content, dict):
            return "pending"
        status = visible_content_from_content(cast("dict[str, object]", content)).get("status")
        if status in {"pending", "approved", "denied", "expired"}:
            return cast("PendingApprovalStatus", status)
        return "pending"


def is_original_approval_card(event: dict[str, Any]) -> bool:
    """Return whether an event is an original approval card, not a replacement edit."""
    content = event.get("content")
    return (
        event.get("type") == "io.mindroom.tool_approval"
        and isinstance(content, dict)
        and not _is_replace_content(content)
    )


def terminal_edit_matches_card_sender(edit: dict[str, Any] | None, card_sender_id: str) -> bool:
    """Return whether a cached terminal edit is trusted for one approval card."""
    if edit is None:
        return True
    return edit.get("sender") == card_sender_id


def _required_str(event: dict[str, Any], key: str) -> str:
    value = event.get(key)
    if isinstance(value, str) and value:
        return value
    msg = f"Approval card event is missing {key}."
    raise ValueError(msg)


def _content_str(content: dict[str, Any], key: str) -> str | None:
    value = content.get(key)
    return value if isinstance(value, str) and value else None


def _created_at_ms(event: dict[str, Any], requested_at: str | None) -> int:
    parsed = parse_approval_datetime(requested_at)
    if parsed is None:
        timestamp = event.get("origin_server_ts")
        return timestamp if isinstance(timestamp, int) and not isinstance(timestamp, bool) else 0
    return int(parsed.timestamp() * 1000)


def _timeout_seconds(requested_at: str | None, expires_at: str | None) -> int:
    requested = parse_approval_datetime(requested_at)
    expires = parse_approval_datetime(expires_at)
    if requested is None or expires is None:
        return 0
    return max(0, int((expires - requested).total_seconds()))


def parse_approval_datetime(value: str | None) -> datetime | None:
    """Parse an approval ISO timestamp, treating naive timestamps as UTC."""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _is_replace_content(content: dict[str, Any]) -> bool:
    return EventInfo.from_event({"content": content}).is_edit
