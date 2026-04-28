"""Thread-history reads and reconstruction helpers."""

from __future__ import annotations

import time
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import nio
from aiohttp import ClientError
from nio.responses import RoomThreadsResponse

from mindroom.logging_config import get_logger
from mindroom.matrix.cache import (
    THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC,
    THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
    THREAD_HISTORY_ERROR_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_CACHE,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_HOMESERVER,
    THREAD_HISTORY_SOURCE_STALE_CACHE,
    ThreadCacheState,
    ThreadHistoryResult,
    normalize_nio_event_for_cache,
    thread_cache_rejection_reason,
    thread_history_result,
)
from mindroom.matrix.client_visible_messages import (
    ResolvedVisibleMessage,
    _apply_latest_edits_to_messages,
    _record_latest_thread_edit,
)
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.message_content import (
    extract_and_resolve_message,
    resolve_event_source_content,
)
from mindroom.matrix.thread_membership import ThreadRoomScanRootNotFoundError
from mindroom.matrix.thread_projection import (
    ordered_event_ids_from_scanned_event_sources,
    resolve_thread_ids_for_event_infos,
    sort_thread_event_sources_root_first,
    sort_thread_messages_root_first,
)
from mindroom.matrix.visible_body import visible_body_from_event_source

if TYPE_CHECKING:
    from mindroom.matrix.cache import ConversationEventCache

logger = get_logger(__name__)
_VISIBLE_ROOM_MESSAGE_EVENT_TYPES = (nio.RoomMessageText, nio.RoomMessageNotice)


@dataclass(slots=True)
class _ThreadHistoryFetchResult:
    """Resolved thread history plus the raw sources and timing diagnostics used to build it."""

    history: list[ResolvedVisibleMessage]
    event_sources: list[dict[str, Any]]
    fetch_ms: float
    room_scan_pages: int
    scanned_event_count: int
    resolution_ms: float
    sidecar_hydration_ms: float


@dataclass(slots=True)
class _ThreadEventSourceScanResult:
    """Raw event sources plus scan metadata for one room-history thread fetch."""

    event_sources: list[dict[str, Any]]
    page_count: int
    scanned_event_count: int


def _thread_history_result(
    history: list[ResolvedVisibleMessage],
    *,
    is_full_history: bool,
    diagnostics: Mapping[str, str | int | float | bool] | None = None,
) -> ThreadHistoryResult:
    """Wrap history with hydration metadata used by dispatch fast paths."""
    return thread_history_result(history, is_full_history=is_full_history, diagnostics=diagnostics)


class RoomThreadsPageError(ValueError):
    """Raised when a single /threads page request fails."""

    def __init__(
        self,
        *,
        response: str,
        errcode: str | None = None,
        retry_after_ms: int | None = None,
    ) -> None:
        super().__init__(response)
        self.response = response
        self.errcode = errcode
        self.retry_after_ms = retry_after_ms


def _room_threads_page_error_from_response(response: object) -> RoomThreadsPageError:
    """Preserve nio response details for /threads pagination failures."""
    if isinstance(response, nio.ErrorResponse):
        return RoomThreadsPageError(
            response=str(response),
            errcode=response.status_code,
            retry_after_ms=response.retry_after_ms,
        )
    return RoomThreadsPageError(response=str(response))


def _room_threads_page_error_from_exception(exc: BaseException) -> RoomThreadsPageError:
    """Normalize transport failures into the same structured /threads error."""
    detail = str(exc)
    response = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    return RoomThreadsPageError(response=response)


def _is_room_message_event(event: nio.Event) -> bool:
    """Return whether one nio event is a readable Matrix room message."""
    event_source = event.source if isinstance(event.source, dict) else {}
    return event_source.get("type") == "m.room.message"


def _room_message_fallback_body(event: nio.Event) -> str:
    """Return one best-effort fallback body for a room message event."""
    if isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES):
        return event.body
    event_source = event.source if isinstance(event.source, dict) else {}
    content = event_source.get("content")
    if isinstance(content, dict):
        body = content.get("body")
        if isinstance(body, str):
            return body
    return ""


def _snapshot_message_dict(
    event: nio.Event,
    *,
    trusted_sender_ids: Collection[str] = (),
) -> ResolvedVisibleMessage:
    """Build one lightweight visible message without hydrating sidecars."""
    event_source = event.source if isinstance(event.source, dict) else {}
    content = event_source.get("content", {})
    normalized_content = content if isinstance(content, dict) else {}
    event_info = EventInfo.from_event(event_source)
    message = ResolvedVisibleMessage.synthetic(
        sender=event.sender,
        body=visible_body_from_event_source(
            event_source,
            _room_message_fallback_body(event),
            trusted_sender_ids=trusted_sender_ids,
        ),
        timestamp=event.server_timestamp if isinstance(event.server_timestamp, int) else 0,
        event_id=event.event_id,
        content=normalized_content,
        thread_id=event_info.thread_id,
    )
    message.refresh_stream_status()
    return message


def _parse_room_message_event(event_source: dict[str, Any]) -> nio.Event | None:
    """Parse one event dict into a room-message event when possible."""
    try:
        parsed_event = nio.Event.parse_event(event_source)
    except Exception:
        return None
    return parsed_event if _is_room_message_event(parsed_event) else None


def _parse_visible_text_message_event(
    event_source: dict[str, Any],
) -> nio.RoomMessageText | nio.RoomMessageNotice | None:
    """Parse one event dict into a visible text or notice message when possible."""
    parsed_event = _parse_room_message_event(event_source)
    return parsed_event if isinstance(parsed_event, (nio.RoomMessageText, nio.RoomMessageNotice)) else None


def _event_source_for_cache(event: nio.Event) -> dict[str, Any]:
    """Normalize one nio event source for persistent cache storage."""
    return normalize_nio_event_for_cache(event)


def _event_id_from_source(event_source: Mapping[str, Any]) -> str | None:
    """Return one Matrix event ID from a raw event source when present."""
    event_id = event_source.get("event_id")
    return event_id if isinstance(event_id, str) else None


def _bundled_replacement_source(event_source: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return one bundled replacement event source when Matrix already included it."""
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


async def _resolve_thread_history_from_event_sources_timed(
    client: nio.AsyncClient,
    *,
    room_id: str,
    thread_id: str,
    event_sources: Sequence[dict[str, Any]],
    hydrate_sidecars: bool = True,
    event_cache: ConversationEventCache,
    trusted_sender_ids: Collection[str] = (),
) -> tuple[list[ResolvedVisibleMessage], float]:
    """Resolve visible thread history and return approximate sidecar hydration time."""
    input_order_by_event_id: dict[str, int] = {}
    related_event_id_by_event_id: dict[str, str] = {}
    for index, event_source in enumerate(event_sources):
        event_id = event_source.get("event_id")
        if isinstance(event_id, str):
            input_order_by_event_id[event_id] = index
            related_event_id = EventInfo.from_event(event_source).next_related_event_id(event_id)
            if isinstance(related_event_id, str):
                related_event_id_by_event_id[event_id] = related_event_id
    parsed_events = [
        parsed_event
        for event_source in event_sources
        if (parsed_event := _parse_room_message_event(event_source)) is not None
    ]
    messages_by_event_id: dict[str, ResolvedVisibleMessage] = {}
    latest_edits_by_original_event_id: dict[str, tuple[nio.RoomMessageText | nio.RoomMessageNotice, str | None]] = {}
    sidecar_hydration_started = time.perf_counter()
    for event in parsed_events:
        event_info = EventInfo.from_event(event.source)
        bundled_replacement_source = _bundled_replacement_source(event.source)
        if bundled_replacement_source is not None:
            bundled_replacement = nio.Event.parse_event(bundled_replacement_source)
            if isinstance(bundled_replacement, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES):
                _record_latest_thread_edit(
                    bundled_replacement,
                    event_info=EventInfo.from_event(bundled_replacement.source),
                    latest_edits_by_original_event_id=latest_edits_by_original_event_id,
                )
        if isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES) and _record_latest_thread_edit(
            event,
            event_info=event_info,
            latest_edits_by_original_event_id=latest_edits_by_original_event_id,
        ):
            continue
        if event_info.is_edit or event.event_id in messages_by_event_id:
            continue
        messages_by_event_id[event.event_id] = (
            await _resolve_thread_history_message(
                event,
                client,
                event_cache=event_cache,
                room_id=room_id,
                trusted_sender_ids=trusted_sender_ids,
            )
            if hydrate_sidecars
            else _snapshot_message_dict(event, trusted_sender_ids=trusted_sender_ids)
        )

    await _apply_latest_edits_to_messages(
        client,
        messages_by_event_id=messages_by_event_id,
        latest_edits_by_original_event_id=latest_edits_by_original_event_id,
        required_thread_id=thread_id,
        event_cache=event_cache,
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
    return messages, round((time.perf_counter() - sidecar_hydration_started) * 1000, 1)


async def _load_stale_cached_thread_history(
    client: nio.AsyncClient,
    *,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
    hydrate_sidecars: bool = True,
    fetch_error: Exception,
    cache_reject_diagnostics: Mapping[str, str | int | float | bool] | None = None,
    trusted_sender_ids: Collection[str] = (),
) -> ThreadHistoryResult | None:
    """Return stale cached thread history when a refetch fails but durable rows still exist."""
    cache_read_started = time.perf_counter()
    try:
        cached_event_sources = await event_cache.get_thread_events(room_id, thread_id)
    except Exception as exc:
        logger.warning(
            "Failed to read stale thread cache after refetch failure",
            room_id=room_id,
            thread_id=thread_id,
            fetch_error=str(fetch_error),
            cache_error=str(exc),
        )
        return None
    if cached_event_sources is None:
        return None
    if not _thread_history_fetch_is_cacheable(cached_event_sources, thread_id=thread_id):
        logger.warning(
            "Stale thread cache missing root; refusing degraded history",
            room_id=room_id,
            thread_id=thread_id,
            error=str(fetch_error),
        )
        await _invalidate_thread_cache_entry(event_cache, room_id=room_id, thread_id=thread_id)
        return None

    resolution_started = time.perf_counter()
    resolved_history, sidecar_hydration_ms = await _resolve_cached_thread_history(
        client,
        room_id=room_id,
        thread_id=thread_id,
        event_cache=event_cache,
        cached_event_sources=cached_event_sources,
        hydrate_sidecars=hydrate_sidecars,
        trusted_sender_ids=trusted_sender_ids,
    )
    if resolved_history is None:
        return None

    logger.warning(
        "Thread refetch failed; returning stale cached history",
        room_id=room_id,
        thread_id=thread_id,
        error=str(fetch_error),
    )
    diagnostics: dict[str, str | int | float | bool] = {
        "cache_read_ms": round((time.perf_counter() - cache_read_started) * 1000, 1),
        "resolution_ms": round((time.perf_counter() - resolution_started) * 1000, 1),
        "sidecar_hydration_ms": sidecar_hydration_ms,
        THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_STALE_CACHE,
        THREAD_HISTORY_ERROR_DIAGNOSTIC: str(fetch_error),
        THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
    }
    if cache_reject_diagnostics is not None:
        diagnostics.update(cache_reject_diagnostics)
    return _thread_history_result(
        resolved_history,
        is_full_history=hydrate_sidecars,
        diagnostics=diagnostics,
    )


async def _resolve_cached_thread_history(
    client: nio.AsyncClient,
    *,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
    cached_event_sources: Sequence[dict[str, Any]],
    hydrate_sidecars: bool = True,
    trusted_sender_ids: Collection[str] = (),
) -> tuple[list[ResolvedVisibleMessage] | None, float]:
    """Resolve cached thread history or invalidate the cache entry on corruption."""
    try:
        return await _resolve_thread_history_from_event_sources_timed(
            client,
            room_id=room_id,
            thread_id=thread_id,
            event_sources=cached_event_sources,
            hydrate_sidecars=hydrate_sidecars,
            event_cache=event_cache,
            trusted_sender_ids=trusted_sender_ids,
        )
    except Exception as exc:
        logger.warning(
            "Cached thread payload could not be resolved; refetching from homeserver",
            room_id=room_id,
            thread_id=thread_id,
            error=str(exc),
        )
        await _invalidate_thread_cache_entry(event_cache, room_id=room_id, thread_id=thread_id)
        return None, 0.0


def _cache_reject_diagnostics(
    *,
    cache_state: object,
    rejection_reason: str,
) -> dict[str, str | int | float | bool]:
    diagnostics: dict[str, str | int | float | bool] = {
        THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC: rejection_reason,
    }
    if not isinstance(cache_state, ThreadCacheState):
        return diagnostics
    if cache_state.validated_at is not None:
        diagnostics["cache_validated_at"] = cache_state.validated_at
        diagnostics["cache_age_ms"] = round((time.time() - cache_state.validated_at) * 1000, 1)
    if cache_state.invalidated_at is not None:
        diagnostics["cache_invalidated_at"] = cache_state.invalidated_at
    if cache_state.invalidation_reason is not None:
        diagnostics["cache_invalidation_reason"] = cache_state.invalidation_reason
    if cache_state.room_invalidated_at is not None:
        diagnostics["room_cache_invalidated_at"] = cache_state.room_invalidated_at
    if cache_state.room_invalidation_reason is not None:
        diagnostics["room_cache_invalidation_reason"] = cache_state.room_invalidation_reason
    return diagnostics


async def _load_cached_thread_history_if_usable(
    client: nio.AsyncClient,
    *,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
    hydrate_sidecars: bool,
    trusted_sender_ids: Collection[str] = (),
) -> tuple[ThreadHistoryResult | None, dict[str, str | int | float | bool] | None]:
    """Return a durable thread snapshot when the current runtime may safely trust it."""
    cache_state = await event_cache.get_thread_cache_state(room_id, thread_id)
    rejection_reason = thread_cache_rejection_reason(cache_state)
    if rejection_reason is not None:
        cache_reject_diagnostics = _cache_reject_diagnostics(
            cache_state=cache_state,
            rejection_reason=rejection_reason,
        )
        logger.info(
            "Thread cache rejected for read",
            room_id=room_id,
            thread_id=thread_id,
            **cache_reject_diagnostics,
        )
        return None, cache_reject_diagnostics

    cache_read_started = time.perf_counter()
    cached_event_sources = await event_cache.get_thread_events(room_id, thread_id)
    if cached_event_sources is None:
        cache_reject_diagnostics: dict[str, str | int | float | bool] = {
            THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC: "cache_rows_missing",
        }
        return None, cache_reject_diagnostics
    if not _thread_history_fetch_is_cacheable(cached_event_sources, thread_id=thread_id):
        await _invalidate_thread_cache_entry(event_cache, room_id=room_id, thread_id=thread_id)
        cache_reject_diagnostics: dict[str, str | int | float | bool] = {
            THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC: "cache_missing_thread_root",
        }
        logger.info(
            "Thread cache rejected for read",
            room_id=room_id,
            thread_id=thread_id,
            **cache_reject_diagnostics,
        )
        return None, cache_reject_diagnostics

    resolution_started = time.perf_counter()
    resolved_history, sidecar_hydration_ms = await _resolve_cached_thread_history(
        client,
        room_id=room_id,
        thread_id=thread_id,
        event_cache=event_cache,
        cached_event_sources=cached_event_sources,
        hydrate_sidecars=hydrate_sidecars,
        trusted_sender_ids=trusted_sender_ids,
    )
    if resolved_history is None:
        return None, {
            THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC: "cache_payload_unresolvable",
        }

    return _thread_history_result(
        resolved_history,
        is_full_history=hydrate_sidecars,
        diagnostics={
            "cache_read_ms": round((time.perf_counter() - cache_read_started) * 1000, 1),
            "resolution_ms": round((time.perf_counter() - resolution_started) * 1000, 1),
            "sidecar_hydration_ms": sidecar_hydration_ms,
            THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_CACHE,
        },
    ), None


async def _invalidate_thread_cache_entry(
    event_cache: ConversationEventCache,
    *,
    room_id: str,
    thread_id: str,
) -> None:
    """Best-effort invalidation for one broken cached thread entry."""
    try:
        await event_cache.invalidate_thread(room_id, thread_id)
    except Exception:
        logger.warning(
            "Failed to invalidate broken event cache entry",
            room_id=room_id,
            thread_id=thread_id,
        )


async def _fetch_thread_history_with_events(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    *,
    hydrate_sidecars: bool,
    event_cache: ConversationEventCache,
    trusted_sender_ids: Collection[str] = (),
) -> _ThreadHistoryFetchResult:
    """Fetch thread history and raw event sources from the homeserver."""
    return await _fetch_thread_history_via_room_messages_with_events(
        client,
        room_id,
        thread_id,
        hydrate_sidecars=hydrate_sidecars,
        event_cache=event_cache,
        trusted_sender_ids=trusted_sender_ids,
    )


async def refresh_thread_history_from_source(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
    *,
    hydrate_sidecars: bool = True,
    allow_stale_fallback: bool = True,
    cache_write_guard_started_at: float | None = None,
    cache_reject_diagnostics: Mapping[str, str | int | float | bool] | None = None,
    trusted_sender_ids: Collection[str] = (),
) -> ThreadHistoryResult:
    """Fetch fresh thread history from Matrix and repopulate the advisory cache."""
    fetch_started_at = time.time() if cache_write_guard_started_at is None else cache_write_guard_started_at
    try:
        fetch_result = await _fetch_thread_history_with_events(
            client,
            room_id,
            thread_id,
            hydrate_sidecars=hydrate_sidecars,
            event_cache=event_cache,
            trusted_sender_ids=trusted_sender_ids,
        )
    except Exception as exc:
        if allow_stale_fallback:
            stale_history = await _load_stale_cached_thread_history(
                client,
                room_id=room_id,
                thread_id=thread_id,
                event_cache=event_cache,
                hydrate_sidecars=hydrate_sidecars,
                fetch_error=exc,
                cache_reject_diagnostics=cache_reject_diagnostics,
                trusted_sender_ids=trusted_sender_ids,
            )
            if stale_history is not None:
                return stale_history
        raise
    if _thread_history_fetch_is_cacheable(fetch_result.event_sources, thread_id=thread_id):
        await _store_thread_history_cache(
            event_cache,
            room_id=room_id,
            thread_id=thread_id,
            event_sources=fetch_result.event_sources,
            fetch_started_at=fetch_started_at,
        )
    diagnostics: dict[str, str | int | float | bool] = {
        "cache_read_ms": 0.0,
        "homeserver_fetch_ms": fetch_result.fetch_ms,
        "homeserver_scan_pages": fetch_result.room_scan_pages,
        "homeserver_scanned_event_count": fetch_result.scanned_event_count,
        "homeserver_thread_event_count": len(fetch_result.event_sources),
        "resolution_ms": fetch_result.resolution_ms,
        "sidecar_hydration_ms": fetch_result.sidecar_hydration_ms,
        THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_HOMESERVER,
    }
    if cache_reject_diagnostics is not None:
        diagnostics.update(cache_reject_diagnostics)
    return _thread_history_result(
        fetch_result.history,
        is_full_history=hydrate_sidecars,
        diagnostics=diagnostics,
    )


async def _store_thread_history_cache(
    event_cache: ConversationEventCache,
    *,
    room_id: str,
    thread_id: str,
    event_sources: Sequence[dict[str, Any]],
    fetch_started_at: float | None = None,
) -> bool:
    """Best-effort replacement of one cached thread snapshot."""
    try:
        write_guard_started_at = time.time() if fetch_started_at is None else fetch_started_at
        return await event_cache.replace_thread_if_not_newer(
            room_id,
            thread_id,
            list(event_sources),
            fetch_started_at=write_guard_started_at,
        )
    except Exception as exc:
        logger.warning(
            "Event cache write failed; continuing without cache",
            room_id=room_id,
            thread_id=thread_id,
            error=str(exc),
        )
        return False


def _thread_history_fetch_is_cacheable(
    event_sources: Sequence[dict[str, Any]],
    *,
    thread_id: str,
) -> bool:
    """Return whether one homeserver fetch contains the root event and is safe to cache."""
    return any(_event_id_from_source(event_source) == thread_id for event_source in event_sources)


async def _resolve_thread_history_message(
    event: nio.Event,
    client: nio.AsyncClient,
    *,
    event_cache: ConversationEventCache,
    room_id: str,
    trusted_sender_ids: Collection[str] = (),
) -> ResolvedVisibleMessage:
    """Resolve one room-message event into the normalized thread-history shape."""
    if isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES):
        message_data = await extract_and_resolve_message(
            event,
            client,
            event_cache=event_cache,
            room_id=room_id,
            trusted_sender_ids=trusted_sender_ids,
        )
        return ResolvedVisibleMessage.from_message_data(
            message_data,
            thread_id=EventInfo.from_event(event.source).thread_id,
            latest_event_id=event.event_id,
        )

    resolved_event_source = await resolve_event_source_content(
        event.source if isinstance(event.source, dict) else {},
        client,
        event_cache=event_cache,
        room_id=room_id,
    )
    content = resolved_event_source.get("content", {})
    normalized_content = content if isinstance(content, dict) else {}
    event_info = EventInfo.from_event(resolved_event_source)
    message = ResolvedVisibleMessage.synthetic(
        sender=event.sender,
        body=visible_body_from_event_source(
            resolved_event_source,
            _room_message_fallback_body(event),
            trusted_sender_ids=trusted_sender_ids,
        ),
        timestamp=event.server_timestamp if isinstance(event.server_timestamp, int) else 0,
        event_id=event.event_id,
        content=normalized_content,
        thread_id=event_info.thread_id,
    )
    message.refresh_stream_status()
    return message


async def fetch_thread_history(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
    *,
    cache_write_guard_started_at: float | None = None,
    trusted_sender_ids: Collection[str] = (),
) -> ThreadHistoryResult:
    """Fetch all messages in a thread."""
    cache_reject_diagnostics: dict[str, str | int | float | bool] | None = None
    try:
        cached_history, cache_reject_diagnostics = await _load_cached_thread_history_if_usable(
            client,
            room_id=room_id,
            thread_id=thread_id,
            event_cache=event_cache,
            hydrate_sidecars=True,
            trusted_sender_ids=trusted_sender_ids,
        )
    except Exception as exc:
        logger.warning(
            "Durable thread cache read failed; refetching from homeserver",
            room_id=room_id,
            thread_id=thread_id,
            error=str(exc),
        )
    else:
        if cached_history is not None:
            return cached_history
    return await refresh_thread_history_from_source(
        client,
        room_id,
        thread_id,
        event_cache,
        allow_stale_fallback=True,
        cache_write_guard_started_at=cache_write_guard_started_at,
        cache_reject_diagnostics=cache_reject_diagnostics,
        trusted_sender_ids=trusted_sender_ids,
    )


async def fetch_thread_snapshot(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
    *,
    cache_write_guard_started_at: float | None = None,
    trusted_sender_ids: Collection[str] = (),
) -> ThreadHistoryResult:
    """Fetch lightweight thread context without hydrating sidecars when a fresh cache hit is unavailable."""
    cache_reject_diagnostics: dict[str, str | int | float | bool] | None = None
    try:
        cached_history, cache_reject_diagnostics = await _load_cached_thread_history_if_usable(
            client,
            room_id=room_id,
            thread_id=thread_id,
            event_cache=event_cache,
            hydrate_sidecars=False,
            trusted_sender_ids=trusted_sender_ids,
        )
    except Exception as exc:
        logger.warning(
            "Durable thread cache read failed; refetching snapshot from homeserver",
            room_id=room_id,
            thread_id=thread_id,
            error=str(exc),
        )
    else:
        if cached_history is not None:
            return cached_history
    return await refresh_thread_history_from_source(
        client,
        room_id,
        thread_id,
        event_cache,
        hydrate_sidecars=False,
        allow_stale_fallback=True,
        cache_write_guard_started_at=cache_write_guard_started_at,
        cache_reject_diagnostics=cache_reject_diagnostics,
        trusted_sender_ids=trusted_sender_ids,
    )


async def fetch_dispatch_thread_history(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
    *,
    cache_write_guard_started_at: float | None = None,
    trusted_sender_ids: Collection[str] = (),
) -> ThreadHistoryResult:
    """Fetch strict full thread history for dispatch using only fresh cache data or a homeserver refill."""
    cache_reject_diagnostics: dict[str, str | int | float | bool] | None = None
    try:
        cached_history, cache_reject_diagnostics = await _load_cached_thread_history_if_usable(
            client,
            room_id=room_id,
            thread_id=thread_id,
            event_cache=event_cache,
            hydrate_sidecars=True,
            trusted_sender_ids=trusted_sender_ids,
        )
    except Exception as exc:
        logger.warning(
            "Durable dispatch thread cache read failed; refetching from homeserver",
            room_id=room_id,
            thread_id=thread_id,
            error=str(exc),
        )
    else:
        if cached_history is not None:
            return cached_history
    return await refresh_thread_history_from_source(
        client,
        room_id,
        thread_id,
        event_cache,
        hydrate_sidecars=True,
        allow_stale_fallback=False,
        cache_write_guard_started_at=cache_write_guard_started_at,
        cache_reject_diagnostics=cache_reject_diagnostics,
        trusted_sender_ids=trusted_sender_ids,
    )


async def fetch_dispatch_thread_snapshot(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
    *,
    cache_write_guard_started_at: float | None = None,
    trusted_sender_ids: Collection[str] = (),
) -> ThreadHistoryResult:
    """Fetch strict lightweight dispatch context using only fresh cache data or a homeserver refill."""
    cache_reject_diagnostics: dict[str, str | int | float | bool] | None = None
    try:
        cached_history, cache_reject_diagnostics = await _load_cached_thread_history_if_usable(
            client,
            room_id=room_id,
            thread_id=thread_id,
            event_cache=event_cache,
            hydrate_sidecars=False,
            trusted_sender_ids=trusted_sender_ids,
        )
    except Exception as exc:
        logger.warning(
            "Durable dispatch thread cache read failed; refetching snapshot from homeserver",
            room_id=room_id,
            thread_id=thread_id,
            error=str(exc),
        )
    else:
        if cached_history is not None:
            return cached_history
    return await refresh_thread_history_from_source(
        client,
        room_id,
        thread_id,
        event_cache,
        hydrate_sidecars=False,
        allow_stale_fallback=False,
        cache_write_guard_started_at=cache_write_guard_started_at,
        cache_reject_diagnostics=cache_reject_diagnostics,
        trusted_sender_ids=trusted_sender_ids,
    )


async def _fetch_thread_history_via_room_messages_with_events(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    *,
    hydrate_sidecars: bool,
    event_cache: ConversationEventCache,
    trusted_sender_ids: Collection[str] = (),
) -> _ThreadHistoryFetchResult:
    """Fetch all thread messages by scanning room history pages."""
    fetch_started = time.perf_counter()
    scan_result = await _fetch_thread_event_sources_via_room_messages(client, room_id, thread_id)
    resolution_started = time.perf_counter()
    history, sidecar_hydration_ms = await _resolve_thread_history_from_event_sources_timed(
        client,
        room_id=room_id,
        thread_id=thread_id,
        event_sources=scan_result.event_sources,
        hydrate_sidecars=hydrate_sidecars,
        event_cache=event_cache,
        trusted_sender_ids=trusted_sender_ids,
    )
    return _ThreadHistoryFetchResult(
        history=history,
        event_sources=scan_result.event_sources,
        fetch_ms=round((time.perf_counter() - fetch_started) * 1000, 1),
        room_scan_pages=scan_result.page_count,
        scanned_event_count=scan_result.scanned_event_count,
        resolution_ms=round((time.perf_counter() - resolution_started) * 1000, 1),
        sidecar_hydration_ms=sidecar_hydration_ms,
    )


def _record_scanned_room_message_source(
    event: nio.Event,
    *,
    thread_id: str,
    latest_edits_by_original_event_id: dict[str, tuple[nio.RoomMessageText | nio.RoomMessageNotice, str | None]],
    scanned_message_sources: dict[str, dict[str, Any]],
) -> bool:
    """Record one scanned room-message source and return whether the thread root was found."""
    if not _is_room_message_event(event):
        return False

    event_info = EventInfo.from_event(event.source)
    if isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES) and _record_latest_thread_edit(
        event,
        event_info=event_info,
        latest_edits_by_original_event_id=latest_edits_by_original_event_id,
    ):
        return False
    if event_info.is_edit:
        return False

    scanned_message_sources[event.event_id] = _event_source_for_cache(event)
    return event.event_id == thread_id


async def _resolve_scanned_thread_message_sources(
    *,
    room_id: str,
    thread_id: str,
    scanned_message_sources: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Filter scanned room messages down to events that belong to one thread."""
    event_infos = {
        event_id: EventInfo.from_event(event_source) for event_id, event_source in scanned_message_sources.items()
    }
    relevant_message_sources = {
        thread_id: scanned_message_sources[thread_id],
    }
    ordered_event_ids = ordered_event_ids_from_scanned_event_sources(scanned_message_sources.values())
    resolved_thread_ids = await resolve_thread_ids_for_event_infos(
        room_id,
        event_infos=event_infos,
        ordered_event_ids=ordered_event_ids,
    )

    for event_id in ordered_event_ids:
        if event_id == thread_id or event_id in relevant_message_sources:
            continue
        if resolved_thread_ids.get(event_id) != thread_id:
            continue
        relevant_message_sources[event_id] = scanned_message_sources[event_id]

    ordered_relevant_sources = sort_thread_event_sources_root_first(
        list(relevant_message_sources.values()),
        thread_id=thread_id,
    )
    return {
        event_id: event_source
        for event_source in ordered_relevant_sources
        if isinstance((event_id := _event_id_from_source(event_source)), str)
    }


async def _fetch_thread_event_sources_via_room_messages(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
) -> _ThreadEventSourceScanResult:
    """Fetch thread event sources by scanning room history pages."""
    latest_edits_by_original_event_id: dict[str, tuple[nio.RoomMessageText | nio.RoomMessageNotice, str | None]] = {}
    scanned_message_sources: dict[str, dict[str, Any]] = {}
    from_token = None
    root_message_found = False
    page_count = 0
    scanned_event_count = 0

    while True:
        response = await client.room_messages(
            room_id,
            start=from_token,
            limit=100,
            message_filter={"types": ["m.room.message"]},
            direction=nio.MessageDirection.back,
        )

        if not isinstance(response, nio.RoomMessagesResponse):
            msg = f"room scan failed for {thread_id}: {response}"
            logger.error("Failed to fetch thread history", room_id=room_id, thread_id=thread_id, error=str(response))
            raise RuntimeError(msg)  # noqa: TRY004

        if not response.chunk:
            break
        page_count += 1

        for event in response.chunk:
            if not isinstance(event, nio.Event):
                continue
            scanned_event_count += 1
            if _record_scanned_room_message_source(
                event,
                thread_id=thread_id,
                latest_edits_by_original_event_id=latest_edits_by_original_event_id,
                scanned_message_sources=scanned_message_sources,
            ):
                root_message_found = True

        if root_message_found or not response.end:
            break
        from_token = response.end

    if not root_message_found:
        msg = f"thread root {thread_id} not found during room scan"
        logger.warning(
            "Thread room scan ended without finding root",
            room_id=room_id,
            thread_id=thread_id,
            room_scan_pages=page_count,
            scanned_event_count=len(scanned_message_sources),
        )
        raise ThreadRoomScanRootNotFoundError(msg)

    relevant_message_sources = await _resolve_scanned_thread_message_sources(
        room_id=room_id,
        thread_id=thread_id,
        scanned_message_sources=scanned_message_sources,
    )
    relevant_event_ids = set(relevant_message_sources)
    event_sources = list(relevant_message_sources.values())
    event_sources.extend(
        _event_source_for_cache(edit_event)
        for original_event_id, (edit_event, edit_thread_id) in latest_edits_by_original_event_id.items()
        if original_event_id in relevant_event_ids or edit_thread_id == thread_id
    )
    return _ThreadEventSourceScanResult(
        event_sources=sort_thread_event_sources_root_first(event_sources, thread_id=thread_id),
        page_count=page_count,
        scanned_event_count=scanned_event_count,
    )


async def get_room_threads_page(
    client: nio.AsyncClient,
    room_id: str,
    *,
    limit: int,
    page_token: str | None = None,
) -> tuple[list[nio.Event], str | None]:
    """Fetch a single page of thread roots for a room."""
    if not client.access_token:
        raise RoomThreadsPageError(
            response="Matrix client access token is required for room thread pagination.",
        )

    method, path = nio.Api.room_get_threads(
        client.access_token,
        room_id,
        paginate_from=page_token,
        limit=limit,
    )
    try:
        response = await client._send(
            RoomThreadsResponse,
            method,
            path,
            response_data=(room_id,),
        )
    except (ClientError, TimeoutError) as exc:
        raise _room_threads_page_error_from_exception(exc) from exc
    if not isinstance(response, RoomThreadsResponse):
        raise _room_threads_page_error_from_response(response)

    return response.thread_roots, response.next_batch


__all__ = [
    "RoomThreadsPageError",
    "ThreadRoomScanRootNotFoundError",
    "fetch_dispatch_thread_history",
    "fetch_dispatch_thread_snapshot",
    "fetch_thread_history",
    "fetch_thread_snapshot",
    "get_room_threads_page",
    "refresh_thread_history_from_source",
]
