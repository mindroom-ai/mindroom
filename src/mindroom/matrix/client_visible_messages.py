"""Visible Matrix message projection helpers."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import nio

from mindroom.constants import STREAM_STATUS_KEY
from mindroom.entity_resolution import current_internal_sender_ids
from mindroom.matrix.event_info import (
    EventInfo,
    event_source_is_state_event,
    event_source_matches_room,
    origin_server_ts_from_event_source,
    replacement_content_for_original,
    replacement_content_is_renderable,
    reply_to_event_id_from_content,
    room_message_content_is_renderable,
)
from mindroom.matrix.message_content import extract_and_resolve_message, extract_edit_body, resolve_event_source_content
from mindroom.matrix.visible_body import bundled_visible_body_preview, visible_body_from_event_source

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.cache import ConversationEventCache
    from mindroom.matrix.message_content import SidecarHydrationBatch

_VISIBLE_ROOM_MESSAGE_EVENT_TYPES = (nio.RoomMessageText, nio.RoomMessageNotice)
type ThreadEditCandidatesByOriginalEventId = dict[str, list[nio.RoomMessage]]


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
            self.content = replacement_content_for_original(self.content, content)
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


def bundled_replacement_candidates(event_source: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return bundled replacement candidates in preference order."""
    candidates: list[dict[str, Any]] = []
    unsigned = event_source.get("unsigned")
    if not isinstance(unsigned, Mapping):
        return candidates
    relations = unsigned.get("m.relations")
    if not isinstance(relations, Mapping):
        return candidates
    replacement = relations.get("m.replace")
    if not isinstance(replacement, Mapping):
        return candidates
    for candidate in (
        replacement.get("latest_event"),
        replacement.get("event"),
        replacement,
    ):
        if isinstance(candidate, Mapping):
            candidates.extend(
                [{key: value for key, value in candidate.items() if isinstance(key, str)}],
            )
    return candidates


def is_valid_bundled_replacement(
    original_event_source: Mapping[str, Any],
    replacement_event_source: dict[str, Any],
    *,
    room_id: str | None = None,
) -> bool:
    """Return whether a bundled replacement satisfies Matrix edit validity."""
    original = {key: value for key, value in original_event_source.items() if isinstance(key, str)}
    original_event_id = original.get("event_id")
    replacement_event_id = replacement_event_source.get("event_id")
    original_sender = original.get("sender")
    original_type = original.get("type")
    original_timestamp = origin_server_ts_from_event_source(original)
    replacement_timestamp = origin_server_ts_from_event_source(replacement_event_source)
    original_content = original.get("content")
    identities_are_valid = (
        isinstance(original_event_id, str)
        and bool(original_event_id)
        and isinstance(replacement_event_id, str)
        and bool(replacement_event_id)
        and replacement_event_id != original_event_id
        and isinstance(original_sender, str)
        and bool(original_sender)
        and replacement_event_source.get("sender") == original_sender
        and isinstance(original_type, str)
        and bool(original_type)
        and replacement_event_source.get("type") == original_type
        and isinstance(original_timestamp, int)
        and not isinstance(original_timestamp, bool)
    )
    if not identities_are_valid or (
        not isinstance(replacement_timestamp, int)
        or isinstance(replacement_timestamp, bool)
        or event_source_is_state_event(original)
        or event_source_is_state_event(replacement_event_source)
        or EventInfo.from_event(original).is_edit
    ):
        return False
    if not isinstance(original_content, Mapping):
        return False

    original_room_id = original.get("room_id")
    replacement_room_id = replacement_event_source.get("room_id")
    rooms_are_valid = not (
        room_id is not None
        and (
            not event_source_matches_room(original, room_id)
            or not event_source_matches_room(replacement_event_source, room_id)
        )
    ) and not (
        isinstance(original_room_id, str)
        and isinstance(replacement_room_id, str)
        and original_room_id != replacement_room_id
    )
    if not rooms_are_valid:
        return False

    replacement_info = EventInfo.from_event(replacement_event_source)
    content = replacement_event_source.get("content")
    if (
        not replacement_info.is_edit
        or replacement_info.original_event_id != original_event_id
        or not isinstance(content, Mapping)
        or not isinstance(content.get("m.new_content"), Mapping)
    ):
        return False
    return original_type != "m.room.message" or (
        room_message_content_is_renderable(original_content)
        and replacement_content_is_renderable(original_type, content)
    )


def ordered_valid_bundled_replacements(
    original_event_source: Mapping[str, Any],
    *,
    room_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return valid bundled replacements in canonical Matrix latest-first order."""
    return sorted(
        (
            candidate
            for candidate in bundled_replacement_candidates(original_event_source)
            if is_valid_bundled_replacement(original_event_source, candidate, room_id=room_id)
        ),
        key=lambda candidate: (
            origin_server_ts_from_event_source(candidate),
            candidate["event_id"],
        ),
        reverse=True,
    )


async def bundled_replacement_body(
    event_source: Mapping[str, Any],
    *,
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    event_cache: ConversationEventCache | None = None,
    room_id: str | None = None,
    trusted_sender_ids: Collection[str] | None = None,
) -> str | None:
    """Return one canonical bundled replacement body using runtime-derived sender trust."""
    trusted_sender_ids = _resolved_trusted_sender_ids(config, runtime_paths, trusted_sender_ids)
    for candidate in ordered_valid_bundled_replacements(event_source, room_id=room_id):
        resolved_candidate = await resolve_event_source_content(
            candidate,
            client,
            event_cache=event_cache,
            room_id=room_id,
        )
        body = bundled_visible_body_preview(
            resolved_candidate,
            trusted_sender_ids=trusted_sender_ids,
        )
        if body is not None:
            return body
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
    event_cache: ConversationEventCache | None = None,
    room_id: str | None = None,
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
    event: nio.RoomMessage,
    *,
    event_info: EventInfo,
    edit_candidates_by_original_event_id: ThreadEditCandidatesByOriginalEventId,
) -> bool:
    """Track one edit candidate, returning True if the event is an edit."""
    if not (event_info.is_edit and event_info.original_event_id):
        return False
    if not event.event_id:
        return True

    edit_candidates_by_original_event_id.setdefault(event_info.original_event_id, []).append(event)
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

        ordered_candidates = sorted(
            edit_candidates,
            key=lambda edit_event: (edit_event.server_timestamp, edit_event.event_id),
            reverse=True,
        )
        for edit_event in ordered_candidates:
            if (
                edit_event.sender != existing_message.sender
                or event_source_is_state_event(edit_event.source)
                or (room_id is not None and not event_source_matches_room(edit_event.source, room_id))
                or not room_message_content_is_renderable(existing_message.content)
                or not replacement_content_is_renderable("m.room.message", edit_event.source.get("content", {}))
            ):
                continue
            edited_body, edited_content = await extract_edit_body(
                edit_event.source,
                client,
                event_cache=event_cache,
                room_id=room_id,
                expected_membership_epoch=expected_membership_epoch,
                hydration_batch=hydration_batch,
                trusted_sender_ids=trusted_sender_ids,
            )
            if edited_body is None:
                continue
            existing_message.apply_edit(
                body=edited_body,
                latest_event_id=edit_event.event_id,
                latest_event_timestamp=edit_event.server_timestamp,
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

    for event in events:
        if (
            (sender is not None and event.sender != sender)
            or event_source_is_state_event(event.source)
            or (room_id is not None and not event_source_matches_room(event.source, room_id))
        ):
            continue

        event_info = EventInfo.from_event(event.source)
        if record_thread_edit_candidate(
            event,
            event_info=event_info,
            edit_candidates_by_original_event_id=edit_candidates_by_original_event_id,
        ):
            continue

        if event.event_id in messages_by_event_id:
            continue

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
    "bundled_replacement_candidates",
    "extract_visible_edit_body",
    "extract_visible_message",
    "is_valid_bundled_replacement",
    "message_preview",
    "ordered_valid_bundled_replacements",
    "record_thread_edit_candidate",
    "replace_visible_message",
    "resolve_latest_visible_messages",
    "resolve_visible_event_source",
    "thread_root_body_preview",
    "trusted_visible_sender_ids",
]
