"""Comprehensive event relation analysis for Matrix events.

This module provides a unified API for analyzing all Matrix event relations
including threads (MSC3440), edits, replies, reactions, and more.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

_THREAD_RELATION_EVENT_TYPES = frozenset({"m.room.encrypted", "m.room.message"})
_MATRIX_MEDIA_MESSAGE_TYPES = frozenset({"m.audio", "m.file", "m.image", "m.video"})
_APPROVAL_STATUSES = frozenset({"approved", "denied", "expired", "pending"})


def event_type_supports_thread_relations(event_type: object) -> bool:
    """Return whether this Matrix event family can affect conversation thread state."""
    return isinstance(event_type, str) and event_type in _THREAD_RELATION_EVENT_TYPES


def event_source_is_state_event(event_source: Mapping[str, object]) -> bool:
    """Return whether one raw Matrix event source carries a state key."""
    return "state_key" in event_source


def event_source_matches_room(event_source: Mapping[str, object], room_id: str) -> bool:
    """Return whether explicit room evidence agrees with the authoritative room."""
    return "room_id" not in event_source or event_source.get("room_id") == room_id


def event_source_matches_index(
    event_source: Mapping[str, object],
    indexed_event_id: str,
    indexed_origin_server_ts: int,
    room_id: str,
) -> bool:
    """Return whether one cached payload matches its authoritative index scope."""
    timestamp = event_source.get("origin_server_ts")
    return (
        bool(indexed_event_id)
        and event_source.get("event_id") == indexed_event_id
        and isinstance(timestamp, int)
        and not isinstance(timestamp, bool)
        and timestamp == indexed_origin_server_ts
        and event_source_matches_room(
            event_source,
            room_id,
        )
    )


def approval_status_from_content(content: Mapping[str, object]) -> str | None:
    """Return one valid approval-card status."""
    status = content.get("status")
    return status if isinstance(status, str) and status in _APPROVAL_STATUSES else None


def _encrypted_media_file_is_valid(file_info: object) -> bool:
    """Return whether one Matrix encrypted-file transport is usable v2 data."""
    if not isinstance(file_info, Mapping):
        return False
    normalized_file_info = cast("Mapping[str, object]", file_info)
    key = normalized_file_info.get("key")
    hashes = normalized_file_info.get("hashes")
    if not isinstance(key, Mapping):
        return False
    if not isinstance(hashes, Mapping):
        return False
    normalized_key = cast("Mapping[str, object]", key)
    normalized_hashes = cast("Mapping[str, object]", hashes)
    url = normalized_file_info.get("url")
    key_ops = normalized_key.get("key_ops")
    return (
        isinstance(url, str)
        and url.startswith("mxc://")
        and all(url[len("mxc://") :].partition("/"))
        and normalized_file_info.get("v") == "v2"
        and normalized_key.get("kty") == "oct"
        and normalized_key.get("alg") == "A256CTR"
        and normalized_key.get("ext") is True
        and isinstance(key_ops, list)
        and all(isinstance(operation, str) for operation in key_ops)
        and {"encrypt", "decrypt"}.issubset(key_ops)
        and _unpadded_base64_has_decoded_size(normalized_key.get("k"), 32, urlsafe=True)
        and _unpadded_base64_has_decoded_size(normalized_file_info.get("iv"), 16)
        and _unpadded_base64_has_decoded_size(normalized_hashes.get("sha256"), 32)
    )


def _unpadded_base64_has_decoded_size(
    value: object,
    expected_size: int,
    *,
    urlsafe: bool = False,
) -> bool:
    """Return whether one unpadded base64 value decodes to the expected byte size."""
    if not isinstance(value, str) or not value or "=" in value:
        return False
    padded_value = value + "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(
            padded_value,
            altchars=b"-_" if urlsafe else None,
            validate=True,
        )
    except (binascii.Error, ValueError):
        return False
    return len(decoded) == expected_size


def room_message_content_is_renderable(content: Mapping[str, object]) -> bool:
    """Return whether nio can render one Matrix room-message content payload."""
    msgtype = content.get("msgtype")
    if not isinstance(content.get("body"), str) or not isinstance(msgtype, str):
        return False
    if msgtype not in _MATRIX_MEDIA_MESSAGE_TYPES:
        return True
    if "file" in content:
        return _encrypted_media_file_is_valid(content.get("file"))
    return isinstance(content.get("url"), str)


def replacement_content_is_renderable(
    event_type: object,
    content: object,
) -> bool:
    """Return whether one supported replacement has valid visible content."""
    if not isinstance(content, Mapping):
        return False
    normalized_content = cast("Mapping[str, object]", content)
    new_content = normalized_content.get("m.new_content")
    if not isinstance(new_content, Mapping):
        return False
    normalized_new_content = cast("Mapping[str, object]", new_content)
    if event_type == "m.room.message":
        return room_message_content_is_renderable(normalized_content) and room_message_content_is_renderable(
            normalized_new_content,
        )
    if event_type == "io.mindroom.tool_approval":
        return approval_status_from_content(normalized_new_content) is not None
    return False


def origin_server_ts_from_event_source(event_source: object) -> int | float | None:
    """Return a Matrix origin timestamp from one raw event source if present."""
    if not isinstance(event_source, Mapping):
        return None
    raw_timestamp = cast("Mapping[str, object]", event_source).get("origin_server_ts")
    if isinstance(raw_timestamp, int | float) and not isinstance(raw_timestamp, bool):
        return raw_timestamp
    return None


def replacement_content_for_original(
    original_content: Mapping[str, object],
    new_content: Mapping[str, object],
) -> dict[str, object]:
    """Apply Matrix replacement content while preserving the original relation."""
    replacement_content = {
        key: value for key, value in new_content.items() if isinstance(key, str) and key != "m.relates_to"
    }
    if "m.relates_to" in original_content:
        replacement_content["m.relates_to"] = original_content["m.relates_to"]
    return replacement_content


def reply_to_event_id_from_content(content: Mapping[str, object] | None) -> str | None:
    """Return the explicit reply target encoded on one Matrix content payload."""
    if content is None:
        return None
    relates_to = content.get("m.relates_to")
    if not isinstance(relates_to, Mapping):
        return None
    relates_to = cast("Mapping[str, object]", relates_to)
    in_reply_to = relates_to.get("m.in_reply_to")
    if not isinstance(in_reply_to, Mapping):
        return None
    in_reply_to = cast("Mapping[str, object]", in_reply_to)
    reply_to_event_id = in_reply_to.get("event_id")
    return reply_to_event_id if isinstance(reply_to_event_id, str) else None


@dataclass
class EventInfo:
    """Comprehensive analysis of Matrix event relations."""

    # Thread information (MSC3440)
    is_thread: bool
    """Whether this event is part of a thread."""

    thread_id: str | None
    """The thread root event ID if this is a thread message."""

    can_be_thread_root: bool
    """Whether this event can be used as a thread root per MSC3440."""

    # Edit information
    is_edit: bool
    """Whether this event is an edit (m.replace)."""

    original_event_id: str | None
    """The event ID being edited if this is an edit."""

    # Reply information
    is_reply: bool
    """Whether this event is a reply to another event."""

    reply_to_event_id: str | None
    """The event ID being replied to if this is a reply."""

    # Reaction information
    is_reaction: bool
    """Whether this event is a reaction (m.annotation)."""

    reaction_key: str | None
    """The reaction key/emoji if this is a reaction."""

    reaction_target_event_id: str | None
    """The event ID being reacted to if this is a reaction."""

    # General relation information
    has_relations: bool
    """Whether this event has any relations."""

    relation_type: str | None
    """The relation type if any (m.replace, m.annotation, m.thread, etc)."""

    relates_to_event_id: str | None
    """The primary event ID this event relates to (if any)."""

    thread_id_from_edit: str | None = None
    """For edit events: the thread root event ID found in ``m.new_content``."""

    event_type: str | None = None
    """The Matrix event type carrying these relations, when known."""

    @staticmethod
    def from_event(event_source: dict | None) -> EventInfo:
        """Create EventInfo from a raw event source dictionary."""
        return _analyze_event_relations(event_source)

    def next_related_event_id(self, current_event_id: str) -> str | None:
        """Return the next relation target to inspect outside native thread hops."""
        for related_event_id in (
            self.original_event_id if self.is_edit else None,
            self.reaction_target_event_id if self.is_reaction else None,
            self.relates_to_event_id if self.relation_type == "m.reference" else None,
            self.reply_to_event_id,
        ):
            if not isinstance(related_event_id, str):
                continue
            normalized_related_event_id = related_event_id.strip()
            if not normalized_related_event_id or normalized_related_event_id == current_event_id:
                continue
            return normalized_related_event_id
        return None


def is_thread_affecting_relation(
    event_info: EventInfo,
    *,
    event_type: str | None,
) -> bool:
    """Return whether one event relation can affect visible thread-scoped cache state.

    Relation names are reused by non-message event families.
    Only relations carried by plaintext or encrypted room messages can add visible
    conversation history, so every non-message relation stays room-level.
    """
    return event_type_supports_thread_relations(event_type) and (
        event_info.is_thread or event_info.is_edit or event_info.is_reply or event_info.relation_type == "m.reference"
    )


def _analyze_event_relations(event_source: dict | None) -> EventInfo:
    """Analyze complete relation information for a Matrix event.

    This unified function provides all relation-related information in one place,
    replacing manual extraction of m.relates_to throughout the codebase.

    Per MSC3440:
    - A thread can only be created from events that don't have any rel_type
    - Thread messages use rel_type: m.thread
    - Edits use rel_type: m.replace
    - Reactions use rel_type: m.annotation
    - Replies can be within threads or standalone

    Args:
        event_source: The event source dictionary (e.g., event.source for nio events)

    Returns:
        EventInfo object with complete relation analysis

    """
    if not event_source:
        return EventInfo(
            is_thread=False,
            thread_id=None,
            can_be_thread_root=True,
            is_edit=False,
            original_event_id=None,
            is_reply=False,
            reply_to_event_id=None,
            is_reaction=False,
            reaction_key=None,
            reaction_target_event_id=None,
            has_relations=False,
            relation_type=None,
            relates_to_event_id=None,
            thread_id_from_edit=None,
        )

    raw_event_type = event_source.get("type")
    event_type = raw_event_type if isinstance(raw_event_type, str) else None
    content = event_source.get("content", {})
    if not isinstance(content, dict):
        content = {}
    relates_to = content.get("m.relates_to", {})
    if not isinstance(relates_to, dict):
        relates_to = {}

    # Extract basic relation information
    relation_type = relates_to.get("rel_type")
    has_relations = bool(relates_to)
    relates_to_event_id = relates_to.get("event_id")

    # Thread analysis
    is_thread = relation_type == "m.thread"
    thread_id = relates_to_event_id if is_thread else None

    # Edit analysis
    is_edit = relation_type == "m.replace"
    original_event_id = relates_to_event_id if is_edit else None
    thread_id_from_edit = _extract_thread_id_from_new_content(content) if is_edit else None

    # Reaction analysis
    is_reaction = relation_type == "m.annotation"
    reaction_key = relates_to.get("key") if is_reaction else None
    reaction_target_event_id = relates_to_event_id if is_reaction else None

    # Reply analysis: replies can exist within threads or as standalone.
    reply_to_event_id = reply_to_event_id_from_content(content)
    is_reply = reply_to_event_id is not None

    # Determine if this event can be a thread root (per MSC3440)
    # An event can only be a thread root if it has NO relations
    can_be_thread_root = not has_relations

    return EventInfo(
        event_type=event_type,
        # Thread info
        is_thread=is_thread,
        thread_id=thread_id,
        can_be_thread_root=can_be_thread_root,
        # Edit info
        is_edit=is_edit,
        original_event_id=original_event_id,
        # Reply info
        is_reply=is_reply,
        reply_to_event_id=reply_to_event_id,
        # Reaction info
        is_reaction=is_reaction,
        reaction_key=reaction_key,
        reaction_target_event_id=reaction_target_event_id,
        # General info
        has_relations=has_relations,
        relation_type=relation_type,
        relates_to_event_id=relates_to_event_id,
        thread_id_from_edit=thread_id_from_edit,
    )


def _extract_thread_id_from_new_content(content: dict) -> str | None:
    """Extract thread root event ID from edit ``m.new_content`` relation data."""
    new_content = content.get("m.new_content", {})
    if not isinstance(new_content, dict):
        return None

    new_relates_to = new_content.get("m.relates_to", {})
    if not isinstance(new_relates_to, dict):
        return None

    if new_relates_to.get("rel_type") != "m.thread":
        return None

    event_id = new_relates_to.get("event_id")
    return event_id if isinstance(event_id, str) else None
