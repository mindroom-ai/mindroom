"""MatrixRTC wire formats for Element Call interop.

Implements the deployed (legacy MSC4143 session) flavor that Element Call and
Cinny ship today: ``org.matrix.msc3401.call.member`` state events for call
membership and ``io.element.call.encryption_keys`` to-device events for
per-sender media keys. Kept free of nio imports so the formats stay testable
as plain dict transforms.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mindroom.matrix import event_types as matrix_event_types

CALL_MEMBER_EVENT_TYPE = matrix_event_types.CALL_MEMBER_EVENT_TYPE
CALL_ENCRYPTION_KEYS_EVENT_TYPE = matrix_event_types.CALL_ENCRYPTION_KEYS_EVENT_TYPE
RTC_NOTIFICATION_EVENT_TYPE = matrix_event_types.RTC_NOTIFICATION_EVENT_TYPE

_CALL_APPLICATION = "m.call"
_ROOM_CALL_ID = ""
_ROOM_CALL_SCOPE = "m.room"

#: Fallback validity window for membership events, matching matrix-js-sdk's
#: ``DEFAULT_EXPIRE_DURATION`` (4 hours).
DEFAULT_MEMBERSHIP_EXPIRES_MS = 4 * 60 * 60 * 1000


def _preferred_livekit_service_url(content: dict[str, Any], *, uses_multi_sfu: bool) -> str | None:
    """Return the selected first preferred LiveKit focus URL."""
    foci = content.get("foci_preferred")
    if not isinstance(foci, list) or not foci:
        return None
    focus = foci[0]
    if not isinstance(focus, dict) or focus.get("type") != "livekit":
        return None
    # A direct WebSocket transport might publish elsewhere even with a trusted service URL.
    if uses_multi_sfu and "url" in focus:
        return None
    candidate = focus.get("livekit_service_url")
    return candidate if isinstance(candidate, str) and candidate else None


def _is_supported_room_call_session(content: dict[str, Any]) -> bool:
    """Return whether legacy session metadata describes our supported room call."""
    focus_active = content.get("focus_active")
    return (
        content.get("application") == _CALL_APPLICATION
        and content.get("call_id") in (_ROOM_CALL_ID, "ROOM")
        and content.get("scope") in (None, _ROOM_CALL_SCOPE)
        and isinstance(focus_active, dict)
        and focus_active.get("type") == "livekit"
        and focus_active.get("focus_selection") in ("oldest_membership", "multi_sfu")
    )


@dataclass(frozen=True)
class CallMember:
    """One active device in a room call, parsed from a membership state event."""

    user_id: str
    device_id: str
    created_ts: int
    expires_ms: int
    livekit_service_url: str | None = None
    uses_multi_sfu: bool = False

    def is_expired(self, now_ms: int) -> bool:
        """Whether the membership state event has outlived its validity window."""
        return now_ms >= self.created_ts + self.expires_ms


def membership_state_key(user_id: str, device_id: str) -> str:
    """State key Element Call expects for a room-call membership event."""
    # The leading underscore keeps the state key from starting with "@", which
    # servers reserve for state owned by that exact user.
    return f"_{user_id}_{device_id}_{_CALL_APPLICATION}"


def build_membership_content(
    *,
    user_id: str,
    device_id: str,
    livekit_service_url: str,
    expires_ms: int,
    created_ts: int | None = None,
    intent: str = "audio",
) -> dict[str, Any]:
    """Build ``org.matrix.msc3401.call.member`` content for a room-scoped call."""
    content: dict[str, Any] = {
        "application": _CALL_APPLICATION,
        "call_id": _ROOM_CALL_ID,
        "scope": _ROOM_CALL_SCOPE,
        "device_id": device_id,
        "membershipID": f"{user_id}:{device_id}",
        "expires": expires_ms,
        "m.call.intent": intent,
        "focus_active": {"type": "livekit", "focus_selection": "oldest_membership"},
        "foci_preferred": [{"type": "livekit", "livekit_service_url": livekit_service_url}],
    }
    if created_ts is not None:
        content["created_ts"] = created_ts
    return content


def parse_membership_event(event_source: dict[str, Any]) -> CallMember | None:
    """Parse a membership state event into a ``CallMember``.

    Returns ``None`` for leave events (empty content), non-room-call sessions,
    and content that does not match the legacy session format.
    """
    if event_source.get("type") != CALL_MEMBER_EVENT_TYPE or "state_key" not in event_source:
        return None
    sender = event_source.get("sender")
    content = event_source.get("content")
    if not isinstance(sender, str) or not isinstance(content, dict) or not content:
        return None
    if not _is_supported_room_call_session(content):
        return None
    device_id = content.get("device_id")
    if not isinstance(device_id, str) or not device_id:
        return None
    origin_ts = event_source.get("origin_server_ts", 0)
    created_ts = content.get("created_ts")
    if not isinstance(created_ts, int) or isinstance(created_ts, bool):
        created_ts = origin_ts if isinstance(origin_ts, int) and not isinstance(origin_ts, bool) else 0
    expires = content.get("expires")
    if not isinstance(expires, int) or isinstance(expires, bool):
        expires = DEFAULT_MEMBERSHIP_EXPIRES_MS
    uses_multi_sfu = content["focus_active"]["focus_selection"] == "multi_sfu"
    return CallMember(
        user_id=sender,
        device_id=device_id,
        created_ts=created_ts,
        expires_ms=expires,
        livekit_service_url=_preferred_livekit_service_url(content, uses_multi_sfu=uses_multi_sfu),
        uses_multi_sfu=uses_multi_sfu,
    )


@dataclass(frozen=True)
class ReceivedFrameKey:
    """A media frame key received from another call participant."""

    user_id: str
    claimed_device_id: str
    key_base64: str
    key_index: int
    received_at_ms: int


def build_key_to_device_content(
    *,
    key_base64: str,
    key_index: int,
    room_id: str,
    member_id: str,
    device_id: str,
    sent_ts: int,
) -> dict[str, Any]:
    """Build ``io.element.call.encryption_keys`` to-device content."""
    return {
        "keys": {"index": key_index, "key": key_base64},
        "member": {"id": member_id, "claimed_device_id": device_id},
        "room_id": room_id,
        "session": {"application": _CALL_APPLICATION, "call_id": _ROOM_CALL_ID, "scope": _ROOM_CALL_SCOPE},
        "sent_ts": sent_ts,
    }


def parse_key_to_device_content(
    sender: str,
    content: dict[str, Any],
    *,
    room_id: str,
    received_at_ms: int,
) -> ReceivedFrameKey | None:
    """Parse SDK 41 or 45 ``io.element.call.encryption_keys`` to-device content."""
    if content.get("room_id") != room_id:
        return None
    # The member ID is advisory; key ownership comes from the authenticated Matrix device.
    member_id = content.get("member_id")
    if member_id is not None and not isinstance(member_id, str):
        return None
    media_key = _parse_media_key(content.get("media_key")) or _parse_media_key(content.get("keys"))
    if media_key is None:
        return None
    key, index = media_key
    member = content.get("member")
    if not isinstance(member, dict):
        return None
    claimed_device_id = member.get("claimed_device_id")
    if not isinstance(claimed_device_id, str) or not claimed_device_id:
        return None
    return ReceivedFrameKey(
        user_id=sender,
        claimed_device_id=claimed_device_id,
        key_base64=key,
        key_index=index,
        received_at_ms=received_at_ms,
    )


def _parse_media_key(entry: object) -> tuple[str, int] | None:
    """Validate a modern or legacy media key entry."""
    if not isinstance(entry, dict):
        return None
    key = entry.get("key")
    index = entry.get("index")
    if (
        not isinstance(key, str)
        or not key
        or not isinstance(index, int)
        or isinstance(index, bool)
        or not 0 <= index < 256
    ):
        return None
    return key, index
