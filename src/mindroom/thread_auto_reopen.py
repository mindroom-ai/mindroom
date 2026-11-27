"""Pure decision rules for automatically reopening resolved Matrix threads on new replies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from mindroom.constants import (
    NONTERMINAL_STREAM_STATUSES,
    RESOLVED_IN_TURN_KEY,
    STREAM_STATUS_KEY,
)
from mindroom.event_journal import replacement_target, thread_root
from mindroom.thread_snooze import THREAD_SNOOZE_CONTENT_KEY
from mindroom.thread_tags import (
    RESOLVED_THREAD_TAG,
    THREAD_TAGS_EVENT_TYPE,
    ThreadTagRecord,
    parse_thread_tag_record,
    parse_thread_tag_state_key,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_TOOL_APPROVAL_EVENT_TYPE = "io.mindroom.tool_approval"
_QUALIFYING_MESSAGE_TYPES = frozenset({"m.text", "m.emote", "m.image", "m.audio", "m.video", "m.file"})

__all__ = [
    "ResolvedThread",
    "event_origin_server_ts",
    "extract_auto_reopen_reply",
    "resolved_from_state_event",
]


@dataclass(frozen=True, slots=True)
class ResolvedThread:
    """One resolved thread tracked in memory for automatic reopen on newer replies."""

    room_id: str
    thread_root_id: str
    resolved_ts: int
    record: ThreadTagRecord
    state_event_id: str | None = None


def event_origin_server_ts(source: Mapping[str, object]) -> int | None:
    """Return an event's integer ``origin_server_ts`` in ms, rejecting booleans and non-integers."""
    ts = source.get("origin_server_ts")
    return ts if type(ts) is int else None


def resolved_from_state_event(
    room_id: str,
    source: Mapping[str, object],
) -> tuple[str, ResolvedThread | None] | None:
    """Parse one raw ``com.mindroom.thread.tags`` state event for the ``resolved`` tag."""
    if source.get("type") != THREAD_TAGS_EVENT_TYPE:
        return None
    parsed_state_key = parse_thread_tag_state_key(source.get("state_key"))
    if parsed_state_key is None or parsed_state_key[1] != RESOLVED_THREAD_TAG:
        return None
    thread_root_id = parsed_state_key[0]

    record = parse_thread_tag_record(RESOLVED_THREAD_TAG, source.get("content"))
    if record is None:
        return thread_root_id, None

    ts = event_origin_server_ts(source)
    resolved_ts = ts if ts is not None else int(record.set_at.timestamp() * 1000)
    raw_event_id = source.get("event_id")
    state_event_id = raw_event_id if isinstance(raw_event_id, str) and raw_event_id else None
    return thread_root_id, ResolvedThread(
        room_id=room_id,
        thread_root_id=thread_root_id,
        resolved_ts=resolved_ts,
        record=record,
        state_event_id=state_event_id,
    )


def _qualifies_event_kind(
    event_type: object,
    content: Mapping[str, object],
    is_managed: Callable[[], bool],
) -> bool:
    if event_type == "m.room.message":
        msgtype = content.get("msgtype")
        if isinstance(msgtype, str) and msgtype in _QUALIFYING_MESSAGE_TYPES:
            return True
        if msgtype == "m.notice":
            status = content.get(STREAM_STATUS_KEY)
            return isinstance(status, str) and status in NONTERMINAL_STREAM_STATUSES and is_managed()
        return False
    if event_type == "m.sticker":
        return True
    return event_type == _TOOL_APPROVAL_EVENT_TYPE and is_managed()


def extract_auto_reopen_reply(
    source: Mapping[str, object],
    *,
    sender_is_managed: Callable[[str], bool],
) -> tuple[str, str, int] | None:
    """Return ``(thread_root_id, event_id, origin_server_ts)`` when cleartext ``source`` is a qualifying new reply."""
    if "state_key" in source:
        return None
    event_id = source.get("event_id")
    sender = source.get("sender")
    origin_server_ts = event_origin_server_ts(source)
    raw_content = source.get("content")
    if not (
        isinstance(event_id, str)
        and event_id
        and isinstance(sender, str)
        and sender
        and origin_server_ts is not None
        and isinstance(raw_content, dict)
    ):
        return None

    content = cast("Mapping[str, object]", raw_content)
    thread_root_id = thread_root(content) if replacement_target(content) is None else None
    if not thread_root_id or thread_root_id == event_id or THREAD_SNOOZE_CONTENT_KEY in content:
        return None

    managed: bool | None = None

    def is_managed() -> bool:
        nonlocal managed
        if managed is None:
            managed = sender_is_managed(sender)
        return managed

    if content.get(RESOLVED_IN_TURN_KEY) is True and is_managed():
        return None
    if not _qualifies_event_kind(source.get("type"), content, is_managed):
        return None
    return thread_root_id, event_id, origin_server_ts
