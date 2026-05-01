"""Visible Matrix message projection helpers."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import nio

from mindroom.constants import STREAM_STATUS_KEY
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.identity import active_internal_sender_ids
from mindroom.matrix.message_content import extract_and_resolve_message, extract_edit_body, resolve_event_source_content
from mindroom.matrix.visible_body import bundled_visible_body_preview, visible_body_from_event_source

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.cache import ConversationEventCache

_VISIBLE_ROOM_MESSAGE_EVENT_TYPES = (nio.RoomMessageText, nio.RoomMessageNotice)


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
        timestamp: int,
        latest_event_id: str,
        thread_id: str | None,
        content: dict[str, Any] | None,
    ) -> None:
        """Apply the newest visible edit state to this message."""
        self.body = body
        self.timestamp = timestamp
        self.latest_event_id = latest_event_id
        if thread_id is not None:
            self.thread_id = thread_id
        if content is not None:
            self.content = content
        self.refresh_stream_status()

    @property
    def visible_event_id(self) -> str:
        """Return the event ID for the currently visible event state."""
        return self.latest_event_id

    @property
    def reply_to_event_id(self) -> str | None:
        """Return the explicit reply target encoded on the visible content."""
        return _reply_to_event_id_from_content(self.content)

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
    return active_internal_sender_ids(config, runtime_paths)


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


def _bundled_replacement_candidates(event_source: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return bundled replacement candidates in preference order."""
    candidates: list[dict[str, Any]] = []
    unsigned = event_source.get("unsigned")
    for container in (unsigned, event_source):
        if not isinstance(container, Mapping):
            continue
        relations = container.get("m.relations")
        if not isinstance(relations, Mapping):
            continue
        replacement = relations.get("m.replace")
        if not isinstance(replacement, Mapping):
            continue
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
    for candidate in _bundled_replacement_candidates(event_source):
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


def _reply_to_event_id_from_content(content: Mapping[str, Any] | None) -> str | None:
    """Return the explicit reply target encoded on one visible content payload."""
    if content is None:
        return None
    relates_to = content.get("m.relates_to")
    if not isinstance(relates_to, Mapping):
        return None
    in_reply_to = relates_to.get("m.in_reply_to")
    if not isinstance(in_reply_to, Mapping):
        return None
    reply_to_event_id = in_reply_to.get("event_id")
    return reply_to_event_id if isinstance(reply_to_event_id, str) else None


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


def _record_latest_thread_edit(
    event: nio.RoomMessageText | nio.RoomMessageNotice,
    *,
    event_info: EventInfo,
    latest_edits_by_original_event_id: dict[str, tuple[nio.RoomMessageText | nio.RoomMessageNotice, str | None]],
) -> bool:
    """Track latest edit candidate, returning True if event is an edit."""
    if not (event_info.is_edit and event_info.original_event_id):
        return False

    original_event_id = event_info.original_event_id
    current_latest_edit_data = latest_edits_by_original_event_id.get(original_event_id)
    current_latest_edit = current_latest_edit_data[0] if current_latest_edit_data else None
    if current_latest_edit is None or (event.server_timestamp, event.event_id) > (
        current_latest_edit.server_timestamp,
        current_latest_edit.event_id,
    ):
        latest_edits_by_original_event_id[original_event_id] = (event, event_info.thread_id_from_edit)
    return True


async def _apply_latest_edits_to_messages(
    client: nio.AsyncClient,
    *,
    messages_by_event_id: dict[str, ResolvedVisibleMessage],
    latest_edits_by_original_event_id: dict[str, tuple[nio.RoomMessageText | nio.RoomMessageNotice, str | None]],
    required_thread_id: str | None = None,
    event_cache: ConversationEventCache | None = None,
    room_id: str | None = None,
    trusted_sender_ids: Collection[str] = (),
) -> None:
    """Apply latest edits to message records and synthesize missing originals when allowed."""
    for original_event_id, (edit_event, edit_thread_id) in latest_edits_by_original_event_id.items():
        existing_message = messages_by_event_id.get(original_event_id)

        # Ignore missing originals unrelated to this thread before resolving
        # potentially large edit payloads from sidecar storage.
        if existing_message is None and required_thread_id is not None and edit_thread_id != required_thread_id:
            continue

        edited_body, edited_content = await extract_edit_body(
            edit_event.source,
            client,
            event_cache=event_cache,
            room_id=room_id,
            trusted_sender_ids=trusted_sender_ids,
        )
        if edited_body is None:
            continue

        if existing_message is not None:
            existing_message.apply_edit(
                body=edited_body,
                timestamp=edit_event.server_timestamp,
                latest_event_id=edit_event.event_id,
                thread_id=edit_thread_id,
                content=edited_content,
            )
            continue

        synthesized_message = ResolvedVisibleMessage(
            sender=edit_event.sender,
            body=edited_body,
            timestamp=edit_event.server_timestamp,
            event_id=original_event_id,
            content=edited_content if edited_content is not None else {},
            thread_id=edit_thread_id,
            latest_event_id=edit_event.event_id,
        )
        synthesized_message.refresh_stream_status()
        messages_by_event_id[original_event_id] = synthesized_message


async def resolve_latest_visible_messages(
    events: Sequence[nio.RoomMessageText | nio.RoomMessageNotice],
    client: nio.AsyncClient,
    *,
    sender: str | None = None,
    trusted_sender_ids: Collection[str] = (),
) -> dict[str, ResolvedVisibleMessage]:
    """Resolve the latest visible message state by original event ID for a set of message events."""
    messages_by_event_id: dict[str, ResolvedVisibleMessage] = {}
    latest_edits_by_original_event_id: dict[str, tuple[nio.RoomMessageText | nio.RoomMessageNotice, str | None]] = {}

    for event in events:
        if sender is not None and event.sender != sender:
            continue

        event_info = EventInfo.from_event(event.source)
        if _record_latest_thread_edit(
            event,
            event_info=event_info,
            latest_edits_by_original_event_id=latest_edits_by_original_event_id,
        ):
            continue

        if event.event_id in messages_by_event_id:
            continue

        message_data = await extract_and_resolve_message(
            event,
            client,
            trusted_sender_ids=trusted_sender_ids,
        )
        messages_by_event_id[event.event_id] = ResolvedVisibleMessage.from_message_data(
            message_data,
            thread_id=event_info.thread_id,
            latest_event_id=event.event_id,
        )

    await _apply_latest_edits_to_messages(
        client,
        messages_by_event_id=messages_by_event_id,
        latest_edits_by_original_event_id=latest_edits_by_original_event_id,
        trusted_sender_ids=trusted_sender_ids,
    )
    return messages_by_event_id


__all__ = [
    "ResolvedVisibleMessage",
    "bundled_replacement_body",
    "extract_visible_edit_body",
    "extract_visible_message",
    "message_preview",
    "replace_visible_message",
    "resolve_latest_visible_messages",
    "resolve_visible_event_source",
    "thread_root_body_preview",
    "trusted_visible_sender_ids",
]
