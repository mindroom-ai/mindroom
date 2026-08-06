"""Direct Matrix pagination for one exported thread's complete history.

Export is the only reader that wants a whole thread rather than a recent window, so it cannot be
served by the bounded conversation projection: every projection read takes a limit precisely so
that no call exists which could materialize an unbounded room. Export therefore walks Matrix
``/messages`` itself and owns its own reconstruction.

The walk is unbounded by intent, and bounded in exactly one respect: a homeserver that keeps
handing back a pagination token it has already given us has stopped making progress, so the scan
raises instead of looping. That check is reachable only while the thread root is still missing --
the scan stops the moment it sees the root -- so it can never fail a thread whose history was
already recovered. An empty page is read as exhaustion before the token is examined at all,
because a homeserver at the start of its history legitimately answers that way.

This module keeps no durable state. Sidecar plaintext is fetched from the homeserver on every
pass rather than reused from a cache, which is what exporting without one costs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.client_visible_messages import (
    ResolvedVisibleMessage,
    ThreadEditCandidates,
    apply_latest_edits_to_messages,
)
from mindroom.matrix.event_info import EventInfo, is_thread_affecting_relation
from mindroom.matrix.media import is_encrypted_media_event_source, parse_matrix_media_event_source
from mindroom.matrix.message_content import extract_and_resolve_message, resolve_event_source_content
from mindroom.matrix.thread_membership import (
    ThreadResolutionState,
    map_backed_thread_membership_access,
    resolve_event_thread_membership,
)
from mindroom.matrix.thread_projection import (
    ordered_event_ids_from_scanned_event_sources,
    resolve_thread_ids_for_event_infos,
    sort_thread_event_sources_root_first,
    sort_thread_messages_root_first,
)
from mindroom.matrix.visible_body import visible_body_from_event_source

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

logger = get_logger(__name__)

_ROOM_HISTORY_MESSAGE_TYPES = ("m.room.message", "m.room.encrypted")
_ROOM_HISTORY_PAGE_SIZE = 100
_ROOM_MESSAGE_EVENT_TYPE = "m.room.message"
_OPAQUE_ENCRYPTED_EVENT_TYPE = "m.room.encrypted"
_VISIBLE_ROOM_MESSAGE_EVENT_TYPES = (nio.RoomMessageText, nio.RoomMessageNotice)


class ThreadExportHistoryError(RuntimeError):
    """Raised when one thread's history cannot be exported from Matrix."""


def _is_opaque_encrypted_event_source(event_source: Mapping[str, Any]) -> bool:
    """Return whether one payload is still an undecrypted Matrix ciphertext envelope."""
    return event_source.get("type") == _OPAQUE_ENCRYPTED_EVENT_TYPE


def _is_opaque_thread_affecting_event_source(event_source: Mapping[str, Any]) -> bool:
    """Return whether one payload is ciphertext whose exposed relation affects a thread."""
    if not _is_opaque_encrypted_event_source(event_source):
        return False
    event_info = EventInfo.from_event(dict(event_source))
    return is_thread_affecting_relation(event_info, event_type=event_info.event_type)


def _event_source_of(event: nio.Event) -> dict[str, Any]:
    """Return one nio event's raw source payload as a plain dict."""
    return dict(event.source) if isinstance(event.source, dict) else {}


def _parse_room_message_event(event_source: dict[str, Any]) -> nio.Event | None:
    """Parse one event dict into a room-message event when possible."""
    if is_encrypted_media_event_source(event_source):
        parsed_event = parse_matrix_media_event_source(event_source)
    else:
        try:
            # nio raises on payloads it cannot shape, and one malformed event in a room must cost
            # that event rather than the whole thread export.
            parsed_event = nio.Event.parse_event(event_source)
        except Exception:
            return None
    if parsed_event is None:
        return None
    # nio's parser returns BadEvent even though its public return type is Event.
    event = cast("nio.Event", parsed_event)
    return event if _event_source_of(event).get("type") == _ROOM_MESSAGE_EVENT_TYPE else None


def _parse_visible_text_message_event(
    event_source: dict[str, Any],
) -> nio.RoomMessageText | nio.RoomMessageNotice | None:
    """Parse one event dict into a visible text or notice message when possible."""
    parsed_event = _parse_room_message_event(event_source)
    return parsed_event if isinstance(parsed_event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES) else None


def _bundled_replacement_source(event_source: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return one bundled replacement event source when Matrix already included it.

    Load-bearing rather than redundant with the standalone edit events in the same scan: the
    MindRoom homeserver forks collapse and purge superseded ``m.replace`` events, so a thread's
    current revision can exist only as this bundled aggregation.
    """
    unsigned = event_source.get("unsigned")
    if not isinstance(unsigned, Mapping):
        return None
    relations = unsigned.get("m.relations")
    if not isinstance(relations, Mapping):
        return None
    replacement = relations.get("m.replace")
    if not isinstance(replacement, Mapping):
        return None
    candidates: tuple[object, ...] = (
        replacement.get("event"),
        replacement.get("latest_event"),
    )
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        normalized_candidate = {key: value for key, value in candidate.items() if isinstance(key, str)}
        if _parse_visible_text_message_event(normalized_candidate) is not None:
            return normalized_candidate
    replacement_candidate = {key: value for key, value in replacement.items() if isinstance(key, str)}
    if {
        "event_id",
        "sender",
        "type",
        "origin_server_ts",
    }.issubset(replacement_candidate) and _parse_visible_text_message_event(replacement_candidate) is not None:
        return replacement_candidate
    return None


@dataclass(frozen=True, slots=True)
class _RoomHistoryScan:
    """One backward room-history walk, stopped at a thread root or at exhaustion."""

    event_sources_by_event_id: dict[str, dict[str, Any]]
    edit_candidates: ThreadEditCandidates
    root_found: bool
    page_count: int
    scanned_event_count: int


def _record_scanned_event(
    event: nio.Event,
    *,
    edit_candidates: ThreadEditCandidates,
    event_sources_by_event_id: dict[str, dict[str, Any]],
) -> str | None:
    """Record one scanned room-message source and return the recorded event ID."""
    event_source = _event_source_of(event)
    if _is_opaque_thread_affecting_event_source(event_source):
        # Undecryptable relation-bearing ciphertext is kept as fail-closed evidence: it resolves
        # thread membership through its exposed relation and poisons only that reconstruction.
        event_sources_by_event_id[event.event_id] = event_source
        return event.event_id
    if event_source.get("type") != _ROOM_MESSAGE_EVENT_TYPE:
        return None

    event_info = EventInfo.from_event(event_source)
    if isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES) and edit_candidates.record(
        event,
        event_info=event_info,
    ):
        return None
    if event_info.is_edit:
        return None

    event_sources_by_event_id[event.event_id] = event_source
    return event.event_id


async def _scan_room_history_for_thread(
    client: nio.AsyncClient,
    *,
    room_id: str,
    thread_id: str,
) -> _RoomHistoryScan:
    """Walk room history backwards until the thread root is seen or history runs out."""
    edit_candidates = ThreadEditCandidates()
    event_sources_by_event_id: dict[str, dict[str, Any]] = {}
    seen_pagination_tokens: set[str] = set()
    from_token: str | None = None
    root_found = False
    page_count = 0
    scanned_event_count = 0

    while True:
        response = await client.room_messages(
            room_id,
            start=from_token,
            limit=_ROOM_HISTORY_PAGE_SIZE,
            message_filter={"types": list(_ROOM_HISTORY_MESSAGE_TYPES)},
            direction=nio.MessageDirection.back,
        )
        if not isinstance(response, nio.RoomMessagesResponse):
            msg = f"thread export room scan failed for {room_id}: {response}"
            raise ThreadExportHistoryError(msg)
        if not response.chunk:
            break
        page_count += 1
        for event in response.chunk:
            if not isinstance(event, nio.Event):
                continue
            scanned_event_count += 1
            recorded_event_id = _record_scanned_event(
                event,
                edit_candidates=edit_candidates,
                event_sources_by_event_id=event_sources_by_event_id,
            )
            if recorded_event_id == thread_id:
                root_found = True
        if root_found or not response.end:
            break
        if response.end in seen_pagination_tokens:
            logger.warning(
                "Thread export room scan repeated a pagination token",
                room_id=room_id,
                thread_id=thread_id,
                page_count=page_count,
                scanned_event_count=scanned_event_count,
            )
            msg = (
                f"thread export room scan for {room_id} repeated pagination token before reaching "
                f"thread root {thread_id}"
            )
            raise ThreadExportHistoryError(msg)
        seen_pagination_tokens.add(response.end)
        from_token = response.end

    return _RoomHistoryScan(
        event_sources_by_event_id=event_sources_by_event_id,
        edit_candidates=edit_candidates,
        root_found=root_found,
        page_count=page_count,
        scanned_event_count=scanned_event_count,
    )


async def _reject_unresolved_opaque_relations(
    room_id: str,
    *,
    thread_id: str,
    event_infos: dict[str, EventInfo],
    scanned_event_sources: dict[str, dict[str, Any]],
    resolved_thread_ids: dict[str, str],
) -> None:
    """Fail closed when scanned ciphertext carries relations of unknown thread impact."""
    access = map_backed_thread_membership_access(
        event_infos=event_infos,
        resolved_thread_ids=resolved_thread_ids,
    )
    unresolved_event_ids: set[str] = set()
    for event_id, event_source in scanned_event_sources.items():
        if event_id in resolved_thread_ids or not _is_opaque_encrypted_event_source(event_source):
            continue
        resolution = await resolve_event_thread_membership(room_id, event_infos[event_id], access=access)
        if resolution.state is ThreadResolutionState.INDETERMINATE:
            unresolved_event_ids.add(event_id)
    if not unresolved_event_ids:
        return
    logger.warning(
        "Thread export room scan contains opaque encrypted relations with unresolved impact",
        room_id=room_id,
        thread_id=thread_id,
        unresolved_opaque_event_ids=sorted(unresolved_event_ids),
    )
    msg = f"thread history scan for {thread_id} contains undecryptable events with unresolved thread impact"
    raise ThreadExportHistoryError(msg)


def _scanned_event_sender(event_source: dict[str, Any] | None) -> str | None:
    """Return one scanned event's sender, or None when the event was never scanned."""
    if event_source is None:
        return None
    sender = event_source.get("sender")
    return sender if isinstance(sender, str) else None


def _winning_edit_sources(
    *,
    thread_id: str,
    scanned_event_sources: dict[str, dict[str, Any]],
    resolved_thread_ids: dict[str, str],
    edit_candidates: ThreadEditCandidates,
) -> list[dict[str, Any]]:
    """Return the newest legitimate replacement source for each original in this thread."""
    edit_sources: list[dict[str, Any]] = []
    for original_event_id in edit_candidates.original_event_ids():
        winner = edit_candidates.winner_for(
            original_event_id,
            sender=_scanned_event_sender(scanned_event_sources.get(original_event_id)),
        )
        if winner is None:
            continue
        edit_event, edit_thread_id = winner
        if thread_id not in {original_event_id, resolved_thread_ids.get(original_event_id), edit_thread_id}:
            continue
        edit_sources.append(_event_source_of(edit_event))
    return edit_sources


async def _thread_event_sources(
    *,
    room_id: str,
    thread_id: str,
    scan: _RoomHistoryScan,
) -> list[dict[str, Any]]:
    """Select and order the scanned sources that belong to one thread."""
    scanned_event_sources = scan.event_sources_by_event_id
    event_infos = {
        event_id: EventInfo.from_event(event_source) for event_id, event_source in scanned_event_sources.items()
    }
    ordered_event_ids = ordered_event_ids_from_scanned_event_sources(scanned_event_sources.values())
    resolved_thread_ids = await resolve_thread_ids_for_event_infos(
        room_id,
        event_infos=event_infos,
        ordered_event_ids=ordered_event_ids,
    )
    await _reject_unresolved_opaque_relations(
        room_id,
        thread_id=thread_id,
        event_infos=event_infos,
        scanned_event_sources=scanned_event_sources,
        resolved_thread_ids=resolved_thread_ids,
    )

    thread_sources: dict[str, dict[str, Any]] = {thread_id: scanned_event_sources[thread_id]}
    for event_id in ordered_event_ids:
        if event_id == thread_id or resolved_thread_ids.get(event_id) != thread_id:
            continue
        thread_sources.setdefault(event_id, scanned_event_sources[event_id])

    return sort_thread_event_sources_root_first(
        [
            *thread_sources.values(),
            *_winning_edit_sources(
                thread_id=thread_id,
                scanned_event_sources=scanned_event_sources,
                resolved_thread_ids=resolved_thread_ids,
                edit_candidates=scan.edit_candidates,
            ),
        ],
        thread_id=thread_id,
    )


async def _resolve_exported_message(
    event: nio.Event,
    client: nio.AsyncClient,
    *,
    room_id: str,
    trusted_sender_ids: Collection[str],
) -> ResolvedVisibleMessage:
    """Resolve one room-message event into the normalized export shape."""
    if isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES):
        message_data = await extract_and_resolve_message(
            event,
            client,
            room_id=room_id,
            trusted_sender_ids=trusted_sender_ids,
        )
        return ResolvedVisibleMessage.from_message_data(
            message_data,
            thread_id=EventInfo.from_event(_event_source_of(event)).thread_id,
            latest_event_id=event.event_id,
        )

    resolved_event_source = await resolve_event_source_content(
        _event_source_of(event),
        client,
        room_id=room_id,
    )
    content = resolved_event_source.get("content", {})
    normalized_content = content if isinstance(content, dict) else {}
    message = ResolvedVisibleMessage.synthetic(
        sender=event.sender,
        body=visible_body_from_event_source(
            resolved_event_source,
            _fallback_body(event),
            trusted_sender_ids=trusted_sender_ids,
        ),
        timestamp=event.server_timestamp if isinstance(event.server_timestamp, int) else 0,
        event_id=event.event_id,
        content=normalized_content,
        thread_id=EventInfo.from_event(resolved_event_source).thread_id,
    )
    message.refresh_stream_status()
    return message


def _fallback_body(event: nio.Event) -> str:
    """Return one best-effort fallback body for a room-message event."""
    if isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES):
        return event.body
    content = _event_source_of(event).get("content")
    if isinstance(content, dict):
        body = content.get("body")
        if isinstance(body, str):
            return body
    return ""


async def _resolve_thread_messages(
    client: nio.AsyncClient,
    *,
    room_id: str,
    thread_id: str,
    event_sources: Sequence[dict[str, Any]],
    trusted_sender_ids: Collection[str],
) -> list[ResolvedVisibleMessage]:
    """Fold one thread's raw sources into its current visible messages."""
    input_order_by_event_id: dict[str, int] = {}
    related_event_id_by_event_id: dict[str, str] = {}
    for index, event_source in enumerate(event_sources):
        event_id = event_source.get("event_id")
        if not isinstance(event_id, str):
            continue
        input_order_by_event_id[event_id] = index
        related_event_id = EventInfo.from_event(event_source).next_related_event_id(event_id)
        if isinstance(related_event_id, str):
            related_event_id_by_event_id[event_id] = related_event_id

    messages_by_event_id: dict[str, ResolvedVisibleMessage] = {}
    edit_candidates = ThreadEditCandidates()
    for event_source in event_sources:
        event = _parse_room_message_event(event_source)
        if event is None:
            continue
        event_info = EventInfo.from_event(_event_source_of(event))
        bundled_replacement_source = _bundled_replacement_source(_event_source_of(event))
        if bundled_replacement_source is not None:
            bundled_replacement = nio.Event.parse_event(bundled_replacement_source)
            if isinstance(bundled_replacement, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES):
                edit_candidates.record(
                    bundled_replacement,
                    event_info=EventInfo.from_event(_event_source_of(bundled_replacement)),
                )
        if isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES) and edit_candidates.record(
            event,
            event_info=event_info,
        ):
            continue
        if event_info.is_edit or event.event_id in messages_by_event_id:
            continue
        messages_by_event_id[event.event_id] = await _resolve_exported_message(
            event,
            client,
            room_id=room_id,
            trusted_sender_ids=trusted_sender_ids,
        )

    await apply_latest_edits_to_messages(
        client,
        messages_by_event_id=messages_by_event_id,
        edit_candidates=edit_candidates,
        required_thread_id=thread_id,
        room_id=room_id,
        trusted_sender_ids=trusted_sender_ids,
    )
    messages = list(messages_by_event_id.values())
    sort_thread_messages_root_first(
        messages,
        thread_id=thread_id,
        input_order_by_event_id=input_order_by_event_id,
        related_event_id_by_event_id=related_event_id_by_event_id,
    )
    return messages


async def fetch_exported_thread_history(
    client: nio.AsyncClient,
    *,
    room_id: str,
    thread_id: str,
    trusted_sender_ids: Collection[str] = (),
) -> list[ResolvedVisibleMessage]:
    """Return one thread's complete current history straight from Matrix."""
    scan = await _scan_room_history_for_thread(client, room_id=room_id, thread_id=thread_id)
    if not scan.root_found:
        logger.warning(
            "Thread export room scan ended without finding root",
            room_id=room_id,
            thread_id=thread_id,
            page_count=scan.page_count,
            scanned_event_count=scan.scanned_event_count,
        )
        msg = f"thread root {thread_id} not found during room scan"
        raise ThreadExportHistoryError(msg)

    event_sources = await _thread_event_sources(room_id=room_id, thread_id=thread_id, scan=scan)
    if any(_is_opaque_encrypted_event_source(event_source) for event_source in event_sources):
        msg = f"thread history for {thread_id} contains still-undecryptable encrypted events"
        raise ThreadExportHistoryError(msg)

    messages = await _resolve_thread_messages(
        client,
        room_id=room_id,
        thread_id=thread_id,
        event_sources=event_sources,
        trusted_sender_ids=trusted_sender_ids,
    )
    logger.info(
        "thread_export_history_fetched",
        room_id=room_id,
        thread_id=thread_id,
        page_count=scan.page_count,
        scanned_event_count=scan.scanned_event_count,
        thread_event_count=len(event_sources),
        message_count=len(messages),
    )
    return messages


__all__ = ["ThreadExportHistoryError", "fetch_exported_thread_history"]
