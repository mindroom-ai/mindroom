"""Pure decision rules for automatically reopening resolved Matrix threads on new replies."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from mindroom.constants import (
    NONTERMINAL_STREAM_STATUSES,
    RESOLVED_IN_TURN_KEY,
    STREAM_STATUS_KEY,
    VISIBLE_ROUTER_VOICE_ECHO_KEY,
)
from mindroom.event_journal import replacement_target, thread_root
from mindroom.thread_snooze import THREAD_SNOOZE_CONTENT_KEY

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_TOOL_APPROVAL_EVENT_TYPE = "io.mindroom.tool_approval"
_QUALIFYING_MESSAGE_TYPES = frozenset({"m.text", "m.emote", "m.image", "m.audio", "m.video", "m.file", "m.location"})

__all__ = ["event_origin_server_ts", "extract_auto_reopen_reply"]


def event_origin_server_ts(source: Mapping[str, object]) -> int | None:
    """Return an event's integer ``origin_server_ts`` in ms, rejecting booleans and non-integers."""
    ts = source.get("origin_server_ts")
    return ts if type(ts) is int else None


def _qualifies_event_kind(event_type: object, content: Mapping[str, object], is_managed: Callable[[], bool]) -> bool:
    if event_type == "m.room.message":
        msgtype = content.get("msgtype")
        if isinstance(msgtype, str) and msgtype in _QUALIFYING_MESSAGE_TYPES:
            return True
        if msgtype == "m.notice":
            status = content.get(STREAM_STATUS_KEY)
            return isinstance(status, str) and status in NONTERMINAL_STREAM_STATUSES and is_managed()
        return False
    return event_type == "m.sticker" or (event_type == _TOOL_APPROVAL_EVENT_TYPE and is_managed())


def extract_auto_reopen_reply(
    source: Mapping[str, object],
    *,
    sender_is_managed: Callable[[str], bool],
) -> tuple[str, str, int] | None:
    """Return ``(thread_root_id, event_id, origin_server_ts)`` when cleartext ``source`` is a qualifying new reply."""
    event_id, sender, raw_content = source.get("event_id"), source.get("sender"), source.get("content")
    origin_server_ts = event_origin_server_ts(source)
    if (
        "state_key" in source
        or not isinstance(event_id, str)
        or not event_id
        or not isinstance(sender, str)
        or not sender
        or origin_server_ts is None
        or not isinstance(raw_content, dict)
    ):
        return None

    content = cast("Mapping[str, object]", raw_content)
    thread_root_id = thread_root(content) if replacement_target(content) is None else None
    if not thread_root_id or thread_root_id == event_id:
        return None

    managed: bool | None = None

    def is_managed() -> bool:
        nonlocal managed
        if managed is None:
            managed = sender_is_managed(sender)
        return managed

    if (
        THREAD_SNOOZE_CONTENT_KEY in content
        or content.get(RESOLVED_IN_TURN_KEY) is True
        or content.get(VISIBLE_ROUTER_VOICE_ECHO_KEY) is True
    ) and is_managed():
        return None
    if not _qualifies_event_kind(source.get("type"), content, is_managed):
        return None
    return thread_root_id, event_id, origin_server_ts
