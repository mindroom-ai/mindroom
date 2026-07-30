"""Visible Matrix message projection helpers."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import nio

from mindroom.constants import STREAM_STATUS_KEY
from mindroom.entity_resolution import current_internal_sender_ids
from mindroom.matrix.event_info import EventInfo, reply_to_event_id_from_content
from mindroom.matrix.message_content import extract_and_resolve_message, extract_edit_body, resolve_event_source_content
from mindroom.matrix.visible_body import bundled_visible_body_preview, visible_body_from_event_source

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.cache import ConversationEventCache
    from mindroom.matrix.message_content import SidecarHydrationBatch

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
    edited_timestamp: int | None = None

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
        """Apply the newest visible edit state to this message.

        ``timestamp`` deliberately stays the original event's. It is the thread's ordering key, and
        an edit is a correction to a message rather than a new position in the conversation - a
        reply from an hour ago does not become the newest thing in the room because its author
        fixed a typo. It also has to stay immutable for the collapsed read to agree with the raw
        read: the query orders by the original timestamp, so a fold that moved edited messages to
        the end would disagree with it about the order of the thread itself. The edit's own time
        is kept separately for callers that want it.
        """
        self.body = body
        self.edited_timestamp = timestamp
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
        # Position and edit time are separate facts now that an edit no longer moves the message.
        # Emitting the edit time keeps it available to consumers that used to read it off
        # "timestamp" before that field became the immutable ordering key.
        if self.edited_timestamp is not None:
            message_data["edited_timestamp"] = self.edited_timestamp
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


type _EditCandidate = tuple[nio.RoomMessageText | nio.RoomMessageNotice, str | None]


def _edit_candidate_is_newer(
    candidate: nio.RoomMessageText | nio.RoomMessageNotice,
    current: nio.RoomMessageText | nio.RoomMessageNotice,
) -> bool:
    """Return whether one replacement candidate outranks another from the same sender.

    Timestamp then event ID is the Matrix rule and decides every real comparison, so it is checked
    first and alone. The payload terms exist only for the case those two tie, which means two
    payloads claiming the same event ID - one event observed both bundled and standalone, with the
    copies disagreeing. Ordering on the payload rather than on arrival keeps a reconstruction from
    depending on which copy the scan saw first, and prefers the copy carrying replacement content
    over an abridged one.

    Serialization is deferred into that tie because streaming emits tens to hundreds of
    replacements per response and each is compared against its bucket's running winner; doing it
    eagerly would put a JSON dump of every edit payload on the event loop.
    """
    if (candidate.server_timestamp, candidate.event_id) != (current.server_timestamp, current.event_id):
        return (candidate.server_timestamp, candidate.event_id) > (current.server_timestamp, current.event_id)
    return _edit_payload_rank(candidate) > _edit_payload_rank(current)


def _edit_payload_rank(event: nio.RoomMessageText | nio.RoomMessageNotice) -> tuple[bool, int, str]:
    """Return the content-derived tiebreak for two payloads claiming one event ID."""
    content = event.source.get("content") if isinstance(event.source, dict) else None
    normalized_content = content if isinstance(content, dict) else {}
    serialized_content = json.dumps(normalized_content, sort_keys=True, separators=(",", ":"))
    return (
        isinstance(normalized_content.get("m.new_content"), dict),
        len(serialized_content),
        serialized_content,
    )


@dataclass(slots=True)
class ThreadEditCandidates:
    """Replacement candidates for one reconstruction, kept per original and per sender.

    Matrix replacement events are only legitimate from the sender of the event they replace, but a
    thread is reconstructed from raw timeline events rather than from the homeserver's bundled
    relations, so nothing upstream has applied that rule. Candidates are therefore kept per sender
    and the sender check is applied when an edit is matched to its original, which is the first
    point where the original's sender is known. Keeping only a single global newest candidate
    would let one foreign edit hide the newest legitimate one.
    """

    _by_original_and_sender: dict[str, dict[str, _EditCandidate]] = field(default_factory=dict)

    def record(
        self,
        event: nio.RoomMessageText | nio.RoomMessageNotice,
        *,
        event_info: EventInfo,
    ) -> bool:
        """Track one replacement candidate, returning whether the event was an edit at all."""
        if not (event_info.is_edit and event_info.original_event_id):
            return False

        by_sender = self._by_original_and_sender.setdefault(event_info.original_event_id, {})
        current = by_sender.get(event.sender)
        if current is None or _edit_candidate_is_newer(event, current[0]):
            by_sender[event.sender] = (event, event_info.thread_id_from_edit)
        return True

    def original_event_ids(self) -> list[str]:
        """Return every original event ID some candidate claims to replace."""
        return list(self._by_original_and_sender)

    def winner_for(self, original_event_id: str, *, sender: str | None) -> _EditCandidate | None:
        """Return the newest legitimate replacement for one original.

        ``sender`` is the original's sender, or ``None`` when the original was never seen. An
        unseen original cannot be impersonated - the synthesized message carries the edit's own
        sender - so the newest candidate across senders wins in that case only.
        """
        by_sender = self._by_original_and_sender.get(original_event_id)
        if not by_sender:
            return None
        if sender is not None:
            return by_sender.get(sender)
        newest = next(iter(by_sender.values()))
        for candidate in by_sender.values():
            if _edit_candidate_is_newer(candidate[0], newest[0]):
                newest = candidate
        return newest


async def apply_latest_edits_to_messages(
    client: nio.AsyncClient,
    *,
    messages_by_event_id: dict[str, ResolvedVisibleMessage],
    edit_candidates: ThreadEditCandidates,
    required_thread_id: str | None = None,
    event_cache: ConversationEventCache | None = None,
    room_id: str | None = None,
    expected_membership_epoch: int | None = None,
    hydration_batch: SidecarHydrationBatch | None = None,
    trusted_sender_ids: Collection[str] = (),
) -> None:
    """Apply latest edits to message records and synthesize missing originals when allowed."""
    for original_event_id in edit_candidates.original_event_ids():
        existing_message = messages_by_event_id.get(original_event_id)
        winner = edit_candidates.winner_for(
            original_event_id,
            sender=None if existing_message is None else existing_message.sender,
        )
        # A replacement from anyone but the original's sender is not an edit of that message.
        if winner is None:
            continue
        edit_event, edit_thread_id = winner

        # Ignore missing originals unrelated to this thread before resolving
        # potentially large edit payloads from sidecar storage.
        if existing_message is None and required_thread_id is not None and edit_thread_id != required_thread_id:
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
    edit_candidates = ThreadEditCandidates()

    for event in events:
        if sender is not None and event.sender != sender:
            continue

        event_info = EventInfo.from_event(event.source)
        if edit_candidates.record(event, event_info=event_info):
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

    await apply_latest_edits_to_messages(
        client,
        messages_by_event_id=messages_by_event_id,
        edit_candidates=edit_candidates,
        trusted_sender_ids=trusted_sender_ids,
    )
    return messages_by_event_id


__all__ = [
    "ResolvedVisibleMessage",
    "ThreadEditCandidates",
    "apply_latest_edits_to_messages",
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
