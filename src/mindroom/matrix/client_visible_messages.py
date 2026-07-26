"""Visible Matrix message projection helpers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import nio

from mindroom.constants import STREAM_STATUS_KEY
from mindroom.entity_resolution import current_internal_sender_ids
from mindroom.matrix import replacements
from mindroom.matrix.cache.event_normalization import normalize_nio_event_for_cache
from mindroom.matrix.event_info import (
    EventInfo,
    origin_server_ts_from_event_source,
    reply_to_event_id_from_content,
)
from mindroom.matrix.media import (
    parse_room_message_event_source,
    valid_room_message_event_source,
    valid_room_message_replacement,
)
from mindroom.matrix.message_content import extract_and_resolve_message, extract_edit_body, resolve_event_source_content
from mindroom.matrix.visible_body import visible_body_from_event_source

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.cache import ConversationEventCache
    from mindroom.matrix.message_content import SidecarHydrationBatch

_VISIBLE_ROOM_MESSAGE_EVENT_TYPES = (nio.RoomMessageText, nio.RoomMessageNotice)
type ThreadEditCandidatesByOriginalEventId = dict[str, list[dict[str, Any]]]


@dataclass(slots=True)
class ResolvedVisibleMessage:
    """Canonical visible message state used during history reconstruction."""

    sender: str
    body: str
    timestamp: int
    event_id: str
    content: dict[str, Any]
    thread_id: str | None
    latest_event_id: str
    stream_status: str | None = None
    latest_event_timestamp: int | None = None

    @classmethod
    def from_message_data(
        cls,
        message_data: dict[str, Any],
        *,
        thread_id: str | None,
        latest_event_id: str,
    ) -> ResolvedVisibleMessage:
        """Build a resolved visible message from extracted message data."""
        message = cls(
            sender=message_data["sender"],
            body=message_data["body"],
            timestamp=message_data["timestamp"],
            event_id=message_data["event_id"],
            content=message_data["content"],
            thread_id=thread_id,
            latest_event_id=latest_event_id,
        )
        message.refresh_stream_status()
        return message

    @classmethod
    def synthetic(
        cls,
        *,
        sender: str,
        body: str,
        event_id: str,
        timestamp: int = 0,
        content: dict[str, Any] | None = None,
        thread_id: str | None = None,
    ) -> ResolvedVisibleMessage:
        """Build a synthetic visible message for non-Matrix history inputs."""
        message = cls(
            sender=sender,
            body=body,
            timestamp=timestamp,
            event_id=event_id,
            content=content or {"body": body},
            thread_id=thread_id,
            latest_event_id=event_id,
        )
        message.refresh_stream_status()
        return message

    def refresh_stream_status(self) -> None:
        """Refresh normalized stream status from message content."""
        self.stream_status = _stream_status_from_content(self.content)

    def apply_edit(
        self,
        *,
        body: str,
        latest_event_id: str,
        latest_event_timestamp: int,
        content: dict[str, Any] | None,
    ) -> None:
        """Apply the newest visible edit state to this message."""
        self.body = body
        self.latest_event_id = latest_event_id
        self.latest_event_timestamp = latest_event_timestamp
        if content is not None:
            self.content = replacements.replacement_content(self.content, content)
        self.refresh_stream_status()

    @property
    def visible_event_id(self) -> str:
        """Return the event ID for the currently visible event state."""
        return self.latest_event_id

    @property
    def visible_timestamp(self) -> int:
        """Return the timestamp of the currently visible event state."""
        return (
            self.timestamp if self.latest_event_timestamp is None else max(self.timestamp, self.latest_event_timestamp)
        )

    @property
    def reply_to_event_id(self) -> str | None:
        """Return the explicit reply target encoded on the visible content."""
        return reply_to_event_id_from_content(self.content)

    def to_dict(self) -> dict[str, Any]:
        """Convert the resolved message back to the public dictionary shape."""
        message_data = {
            "sender": self.sender,
            "body": self.body,
            "timestamp": self.timestamp,
            "event_id": self.event_id,
            "content": self.content,
            "thread_id": self.thread_id,
            "latest_event_id": self.latest_event_id,
        }
        msgtype = self.content.get("msgtype")
        if isinstance(msgtype, str) and msgtype != "m.text":
            message_data["msgtype"] = msgtype
        if self.stream_status is not None:
            message_data["stream_status"] = self.stream_status
        return message_data


def trusted_visible_sender_ids(
    config: Config,
    runtime_paths: RuntimePaths,
) -> frozenset[str]:
    """Return the trusted internal senders for high-level Matrix read helpers."""
    return current_internal_sender_ids(config, runtime_paths)


def _resolved_trusted_sender_ids(
    config: Config,
    runtime_paths: RuntimePaths,
    trusted_sender_ids: Collection[str] | None,
) -> Collection[str]:
    """Reuse one caller-provided trust set or derive it from the current runtime."""
    if trusted_sender_ids is not None:
        return trusted_sender_ids
    return trusted_visible_sender_ids(config, runtime_paths)


async def extract_visible_message(
    event: nio.RoomMessageText | nio.RoomMessageNotice,
    client: nio.AsyncClient | None = None,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    event_cache: ConversationEventCache | None = None,
    room_id: str | None = None,
    trusted_sender_ids: Collection[str] | None = None,
) -> dict[str, Any]:
    """Extract one visible message using runtime-derived sender trust."""
    return await extract_and_resolve_message(
        event,
        client,
        event_cache=event_cache,
        room_id=room_id,
        trusted_sender_ids=_resolved_trusted_sender_ids(config, runtime_paths, trusted_sender_ids),
    )


async def extract_visible_edit_body(
    event_source: dict[str, Any],
    client: nio.AsyncClient | None = None,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    event_cache: ConversationEventCache | None = None,
    room_id: str | None = None,
    trusted_sender_ids: Collection[str] | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Extract one visible edit body using runtime-derived sender trust."""
    return await extract_edit_body(
        event_source,
        client,
        event_cache=event_cache,
        room_id=room_id,
        trusted_sender_ids=_resolved_trusted_sender_ids(config, runtime_paths, trusted_sender_ids),
        replacement_validator=valid_room_message_replacement,
    )


async def resolve_visible_event_source(
    event_source: Mapping[str, Any],
    client: nio.AsyncClient | None = None,
    *,
    fallback_body: str,
    config: Config,
    runtime_paths: RuntimePaths,
    event_cache: ConversationEventCache | None = None,
    room_id: str | None = None,
    trusted_sender_ids: Collection[str] | None = None,
) -> tuple[dict[str, Any], str]:
    """Resolve one event source plus its canonical visible body from runtime config."""
    normalized_event_source = {key: value for key, value in event_source.items() if isinstance(key, str)}
    resolved_event_source = await resolve_event_source_content(
        normalized_event_source,
        client,
        event_cache=event_cache,
        room_id=room_id,
    )
    return resolved_event_source, visible_body_from_event_source(
        resolved_event_source,
        fallback_body,
        trusted_sender_ids=_resolved_trusted_sender_ids(config, runtime_paths, trusted_sender_ids),
    )


def message_preview(body: object, max_length: int = 120) -> str:
    """Return one compact visible-body preview."""
    if not isinstance(body, str):
        return ""
    compact = " ".join(body.split())
    if len(compact) <= max_length:
        return compact
    return f"{compact[: max_length - 3].rstrip()}..."


async def bundled_replacement_body(
    event_source: Mapping[str, Any],
    *,
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    event_cache: ConversationEventCache,
    room_id: str,
    trusted_sender_ids: Collection[str] | None = None,
) -> str | None:
    """Return one canonical bundled replacement body using runtime-derived sender trust."""
    if event_source.get("type") == "m.room.message" and not valid_room_message_event_source(event_source):
        return None
    trusted_sender_ids = _resolved_trusted_sender_ids(config, runtime_paths, trusted_sender_ids)
    bundled_event_ids = {
        event_id
        for candidate in replacements.bundled_replacement_candidates(event_source)
        if isinstance((event_id := candidate.get("event_id")), str)
    }
    if not bundled_event_ids:
        return None
    excluded_event_ids = set(await event_cache.redacted_event_ids(room_id, bundled_event_ids))
    while (
        candidate := await event_cache.get_latest_edit(
            room_id,
            dict(event_source),
            validator=valid_room_message_replacement,
            excluded_event_ids=excluded_event_ids,
        )
    ) is not None:
        body, _content = await extract_edit_body(
            candidate,
            client,
            event_cache=event_cache,
            room_id=room_id,
            trusted_sender_ids=trusted_sender_ids,
            replacement_validator=valid_room_message_replacement,
        )
        if body is not None:
            return body
        excluded_event_ids.add(candidate["event_id"])
    return None


def _event_fallback_body(event: nio.Event) -> str:
    """Return one best-effort Matrix body for preview fallback."""
    if isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES):
        return event.body
    event_source = event.source if isinstance(event.source, dict) else {}
    content = event_source.get("content")
    if isinstance(content, dict):
        body = content.get("body")
        if isinstance(body, str):
            return body
    return ""


async def thread_root_body_preview(
    event: nio.Event,
    *,
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    event_cache: ConversationEventCache,
    room_id: str,
    trusted_sender_ids: Collection[str] | None = None,
) -> str:
    """Return the canonical preview body for one thread root event."""
    if isinstance(event, nio.MegolmEvent):
        return "[encrypted]"
    event_source = event.source if isinstance(event.source, dict) else {}
    trusted_sender_ids = _resolved_trusted_sender_ids(config, runtime_paths, trusted_sender_ids)
    replacement_body = await bundled_replacement_body(
        event_source,
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        event_cache=event_cache,
        room_id=room_id,
        trusted_sender_ids=trusted_sender_ids,
    )
    if replacement_body is not None:
        return message_preview(replacement_body)
    _resolved_event_source, visible_body = await resolve_visible_event_source(
        event_source,
        client,
        fallback_body=_event_fallback_body(event),
        config=config,
        runtime_paths=runtime_paths,
        event_cache=event_cache,
        room_id=room_id,
        trusted_sender_ids=trusted_sender_ids,
    )
    return message_preview(visible_body)


def replace_visible_message(
    message: ResolvedVisibleMessage,
    *,
    sender: str | None = None,
    body: str | None = None,
) -> ResolvedVisibleMessage:
    """Return one visible-message copy while keeping body/content coherent."""
    updated_content: dict[str, Any] | None = None
    if body is not None:
        content = message.content
        updated_content = dict(content)
        updated_content["body"] = body

    updates: dict[str, str | dict[str, Any]] = {}
    if sender is not None:
        updates["sender"] = sender
    if body is not None:
        updates["body"] = body
    if updated_content is not None:
        updates["content"] = updated_content
    return replace(message, **updates)


def _stream_status_from_content(content: dict[str, Any] | None) -> str | None:
    """Extract persisted stream status from message content when present."""
    if content is None:
        return None
    status = content.get(STREAM_STATUS_KEY)
    return status if isinstance(status, str) else None


def record_thread_edit_candidate(
    event_source: dict[str, Any],
    *,
    edit_candidates_by_original_event_id: ThreadEditCandidatesByOriginalEventId,
) -> bool:
    """Track one edit candidate, returning True if the event is an edit."""
    event_info = EventInfo.from_event(event_source)
    if not event_info.is_edit:
        return False
    event_id = event_source.get("event_id")
    if event_info.original_event_id is not None and isinstance(event_id, str) and event_id:
        edit_candidates_by_original_event_id.setdefault(event_info.original_event_id, []).append(event_source)
    return True


async def apply_latest_edits_to_messages(
    client: nio.AsyncClient,
    *,
    messages_by_event_id: dict[str, ResolvedVisibleMessage],
    edit_candidates_by_original_event_id: ThreadEditCandidatesByOriginalEventId,
    event_cache: ConversationEventCache | None = None,
    room_id: str | None = None,
    expected_membership_epoch: int | None = None,
    hydration_batch: SidecarHydrationBatch | None = None,
    trusted_sender_ids: Collection[str] = (),
) -> None:
    """Apply each original's newest valid same-sender replacement."""
    for original_event_id, edit_candidates in edit_candidates_by_original_event_id.items():
        existing_message = messages_by_event_id.get(original_event_id)
        if existing_message is None:
            continue

        original_source = {
            "event_id": existing_message.event_id,
            "sender": existing_message.sender,
            "origin_server_ts": existing_message.timestamp,
            "type": "m.room.message",
            "content": existing_message.content,
        }
        for edit_source in replacements.ordered_replacements(
            original_source,
            edit_candidates,
            room_id=room_id,
            validator=valid_room_message_replacement,
        ):
            edited_body, edited_content = await extract_edit_body(
                edit_source,
                client,
                event_cache=event_cache,
                room_id=room_id,
                expected_membership_epoch=expected_membership_epoch,
                hydration_batch=hydration_batch,
                trusted_sender_ids=trusted_sender_ids,
                replacement_validator=valid_room_message_replacement,
            )
            if edited_body is None:
                continue
            edit_event_id = edit_source["event_id"]
            edit_timestamp = origin_server_ts_from_event_source(edit_source)
            assert isinstance(edit_event_id, str)
            assert isinstance(edit_timestamp, int)
            existing_message.apply_edit(
                body=edited_body,
                latest_event_id=edit_event_id,
                latest_event_timestamp=edit_timestamp,
                content=edited_content,
            )
            break


async def resolve_latest_visible_messages(
    events: Sequence[nio.RoomMessageText | nio.RoomMessageNotice],
    client: nio.AsyncClient,
    *,
    sender: str | None = None,
    room_id: str | None = None,
    trusted_sender_ids: Collection[str] = (),
) -> dict[str, ResolvedVisibleMessage]:
    """Resolve the latest visible message state by original event ID for a set of message events."""
    messages_by_event_id: dict[str, ResolvedVisibleMessage] = {}
    edit_candidates_by_original_event_id: ThreadEditCandidatesByOriginalEventId = {}
    canonical_sources, _conflicting_event_ids = replacements.canonical_event_sources(
        (normalize_nio_event_for_cache(event) for event in events),
        room_id=room_id,
        replacement_validator=valid_room_message_replacement,
    )
    for event_source in canonical_sources:
        event = parse_room_message_event_source(event_source)
        if not isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES):
            continue
        event_info = EventInfo.from_event(event_source)
        if record_thread_edit_candidate(
            event_source,
            edit_candidates_by_original_event_id=edit_candidates_by_original_event_id,
        ):
            continue

        if (sender is not None and event.sender != sender) or event.event_id in messages_by_event_id:
            continue

        bundled_candidates = replacements.bundled_replacement_candidates(event_source)
        if bundled_candidates:
            edit_candidates_by_original_event_id.setdefault(event.event_id, []).extend(bundled_candidates)

        message_data = await extract_and_resolve_message(
            event,
            client,
            room_id=room_id,
            trusted_sender_ids=trusted_sender_ids,
        )
        messages_by_event_id[event.event_id] = ResolvedVisibleMessage.from_message_data(
            message_data,
            thread_id=event_info.thread_id,
            latest_event_id=event.event_id,
        )

    await apply_latest_edits_to_messages(
        client,
        messages_by_event_id=messages_by_event_id,
        edit_candidates_by_original_event_id=edit_candidates_by_original_event_id,
        room_id=room_id,
        trusted_sender_ids=trusted_sender_ids,
    )
    return messages_by_event_id


__all__ = [
    "ResolvedVisibleMessage",
    "ThreadEditCandidatesByOriginalEventId",
    "apply_latest_edits_to_messages",
    "bundled_replacement_body",
    "extract_visible_edit_body",
    "extract_visible_message",
    "message_preview",
    "record_thread_edit_candidate",
    "replace_visible_message",
    "resolve_latest_visible_messages",
    "resolve_visible_event_source",
    "thread_root_body_preview",
    "trusted_visible_sender_ids",
]
