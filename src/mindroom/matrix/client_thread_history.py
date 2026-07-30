"""Thread-history reads and reconstruction helpers.

Cache rules (each encodes a shipped regression fix; do not weaken them):

1. A cached thread snapshot is served only when no gap marker is recorded against it
   (``thread_cache_rejection_reason``). A stale or incomplete snapshot is detected and refetched,
   not prevented — see ``mindroom.matrix.cache.thread_cache_state`` for the two rules governing the
   marker.

2. Cached rows that do not include the thread-root event or that still contain opaque
   ``m.room.encrypted`` payloads are never served: both the read path and the stale-fallback
   path refuse such rows and invalidate the entry, and an incomplete fresh homeserver fetch is never
   stored (PR #741).

3. Cache repopulation passes the fetch start time plus the durable room-membership epoch to
   ``replace_thread``. The epoch stops a fetch crossing a leave/rejoin boundary in this or another
   process; the fetch start time stops a gap detected mid-fetch being cleared by that fetch.

4. Stale fallback exists only on the advisory path: ``fetch_thread_history`` may serve stale cached rows
   when a refetch fails, labelled ``stale_cache`` source with the degraded flag set.
   The dispatch fetchers (``fetch_dispatch_thread_history``, ``fetch_dispatch_thread_snapshot``) never
   serve stale rows; on refetch failure they raise.

5. Reconstruction is canonical: membership of scanned events is decided by
   ``resolve_thread_ids_for_event_infos`` over the page-local relation graph (same rules as live
   resolution), edits collapse into their originals and never appear as standalone messages, and
   ordering follows ``thread_projection`` (root first, then timestamp, with same-timestamp relation
   ancestors before descendants).

6. The room scan requests both ``m.room.message`` and ``m.room.encrypted`` timeline events so nio can
   decrypt threads in encrypted rooms (PR #878), pages backwards until the root event is seen, and
   raises ``ThreadRoomScanRootNotFoundError`` when the scan drains without finding it.

7. Still-opaque encrypted evidence fails closed: a reconstruction whose sources include an
   undecryptable relation-bearing event for the requested thread, or whose scan contains one with
   unresolved thread impact, gap-marks the thread and raises ``OpaqueEncryptedThreadHistoryError``
   instead of certifying incomplete history; the gap marker survives until a decryption-capable
   refresh replaces the snapshot.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import nio
from aiohttp import ClientError
from nio.responses import RoomThreadsResponse

from mindroom.logging_config import get_logger
from mindroom.matrix.cache import (
    ThreadCacheGap,
    ThreadHistoryResult,
    is_opaque_encrypted_event_source,
    normalize_nio_event_for_cache,
    thread_cache_gap_reason,
    thread_cache_rejection_reason,
    thread_history_result,
)
from mindroom.matrix.cache.thread_cache_gap import (
    mark_room_threads_gap_fail_closed,
    mark_thread_gap_fail_closed,
)
from mindroom.matrix.client_visible_messages import (
    ResolvedVisibleMessage,
    ThreadEditCandidates,
    apply_latest_edits_to_messages,
)
from mindroom.matrix.event_info import EventInfo, is_thread_affecting_relation
from mindroom.matrix.media import (
    is_encrypted_media_event_source,
    parse_matrix_media_event_source,
)
from mindroom.matrix.membership_fence import UNCERTIFIED_MEMBERSHIP_EPOCH
from mindroom.matrix.message_content import (
    SidecarHydrationBatch,
    extract_and_resolve_message,
    prepare_sidecar_hydration_batch,
    resolve_event_source_content,
)
from mindroom.matrix.thread_diagnostics import (
    THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC,
    THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
    THREAD_HISTORY_ERROR_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_CACHE,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_HOMESERVER,
    THREAD_HISTORY_SOURCE_STALE_CACHE,
)
from mindroom.matrix.thread_membership import (
    ThreadResolutionState,
    ThreadRoomScanRootNotFoundError,
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
from mindroom.timing import elapsed_ms_since

if TYPE_CHECKING:
    from mindroom.matrix.cache import ConversationEventCache

logger = get_logger(__name__)
_VISIBLE_ROOM_MESSAGE_EVENT_TYPES = (nio.RoomMessageText, nio.RoomMessageNotice)
_ROOM_HISTORY_MESSAGE_TYPES = ("m.room.message", "m.room.encrypted")
_MAX_ENUMERATED_THREAD_ROOTS = 2000
_MAX_THREAD_ENUMERATION_PAGES = 100
_OPAQUE_ENCRYPTED_THREAD_HISTORY_REASON = "thread_history_opaque_encrypted_event"
_OPAQUE_ENCRYPTED_EVENT_REJECTION = "opaque_encrypted_event"
_MISSING_THREAD_ROOT_REJECTION = "missing_thread_root"
type _ThreadHistoryDiagnosticValue = str | int | float | bool | None
type _ThreadHistoryRefill = Callable[
    [Mapping[str, str | int | float | bool] | None],
    Awaitable[ThreadHistoryResult],
]


class OpaqueEncryptedThreadHistoryError(RuntimeError):
    """Raised when a thread reconstruction depends on still-undecryptable encrypted events."""


class _UnresolvedOpaqueRoomHistoryError(OpaqueEncryptedThreadHistoryError):
    """Raised when opaque room history cannot be assigned to a specific thread."""


async def _capture_membership_epoch(event_cache: ConversationEventCache, room_id: str) -> int:
    """Return a durable refill generation or a value that rejects every cache write."""
    try:
        membership_epoch = await event_cache.room_membership_epoch(room_id)
    except Exception as exc:
        logger.warning(
            "Failed to certify Matrix cache refill generation; continuing without cache writes",
            room_id=room_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return UNCERTIFIED_MEMBERSHIP_EPOCH
    return UNCERTIFIED_MEMBERSHIP_EPOCH if membership_epoch is None else membership_epoch


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
    homeserver_scan_parse_cpu_ms: float = 0.0


@dataclass(slots=True)
class _ThreadEventSourceScanResult:
    """Raw event sources plus scan metadata for one room-history thread fetch."""

    event_sources: list[dict[str, Any]]
    page_count: int
    scanned_event_count: int
    homeserver_scan_parse_cpu_ms: float = 0.0


def _thread_history_result(
    history: list[ResolvedVisibleMessage],
    *,
    is_full_history: bool,
    diagnostics: Mapping[str, str | int | float | bool] | None = None,
) -> ThreadHistoryResult:
    """Wrap history with hydration metadata used by dispatch fast paths."""
    return thread_history_result(history, is_full_history=is_full_history, diagnostics=diagnostics)


def log_thread_history_refresh(
    *,
    room_id: str,
    thread_id: str,
    caller_label: str,
    mode: str,
    diagnostics: Mapping[str, _ThreadHistoryDiagnosticValue],
    coordinator_queue_wait_ms: float,
    post_coordinator_read_started: float,
) -> None:
    """Emit one structured INFO line for a completed thread read."""
    post_coordinator_read_ms = elapsed_ms_since(post_coordinator_read_started, clock=time.perf_counter)
    log_fields: dict[str, _ThreadHistoryDiagnosticValue] = {
        "room_id": room_id,
        "thread_id": thread_id,
        "caller_label": caller_label,
        "mode": mode,
        "cache_read_ms": diagnostics.get("cache_read_ms", 0.0),
        "homeserver_fetch_ms": diagnostics.get("homeserver_fetch_ms", 0.0),
        "homeserver_scan_pages": diagnostics.get("homeserver_scan_pages", 0),
        "homeserver_scanned_event_count": diagnostics.get("homeserver_scanned_event_count", 0),
        "homeserver_thread_event_count": diagnostics.get("homeserver_thread_event_count", 0),
        "resolution_ms": diagnostics.get("resolution_ms", 0.0),
        "sidecar_hydration_ms": diagnostics.get("sidecar_hydration_ms", 0.0),
        "coordinator_queue_wait_ms": coordinator_queue_wait_ms,
        "post_coordinator_read_ms": post_coordinator_read_ms,
        "thread_read_total_ms": coordinator_queue_wait_ms + post_coordinator_read_ms,
        "refill_singleflight_wait_ms": diagnostics.get("refill_singleflight_wait_ms", 0.0),
        "refill_singleflight_shared": diagnostics.get("refill_singleflight_shared", False),
        "homeserver_scan_parse_cpu_ms": diagnostics.get("homeserver_scan_parse_cpu_ms", 0.0),
        "cache_reject_reason": diagnostics.get(THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC),
        "thread_read_source": diagnostics.get(THREAD_HISTORY_SOURCE_DIAGNOSTIC),
        "thread_read_degraded": diagnostics.get(THREAD_HISTORY_DEGRADED_DIAGNOSTIC, False),
        "thread_read_error": diagnostics.get(THREAD_HISTORY_ERROR_DIAGNOSTIC),
    }
    for field_name in (
        "cache_store_written",
        "cache_store_failed",
    ):
        if field_name in diagnostics:
            log_fields[field_name] = diagnostics[field_name]
    logger.info("matrix_cache_thread_history_refreshed", **log_fields)


def _report_direct_source_refresh(
    result: ThreadHistoryResult,
    *,
    room_id: str,
    thread_id: str,
    caller_label: str | None,
    coordinator_queue_wait_ms: float,
    post_coordinator_read_started: float,
) -> ThreadHistoryResult:
    """Log one direct source refresh under its caller's label."""
    if caller_label is not None:
        log_thread_history_refresh(
            room_id=room_id,
            thread_id=thread_id,
            caller_label=caller_label,
            mode="full_scan",
            diagnostics=result.diagnostics,
            coordinator_queue_wait_ms=coordinator_queue_wait_ms,
            post_coordinator_read_started=post_coordinator_read_started,
        )
    return result


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
    if is_encrypted_media_event_source(event_source):
        parsed_event = parse_matrix_media_event_source(event_source)
    else:
        try:
            parsed_event = nio.Event.parse_event(event_source)
        except Exception:
            return None
    if parsed_event is None:
        return None
    # nio's parser returns BadEvent even though its public return type is Event.
    event = cast("nio.Event", parsed_event)
    return event if _is_room_message_event(event) else None


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


def _sidecar_hydration_sources(
    event_sources: Sequence[dict[str, Any]],
    *,
    hydrate_sidecars: bool,
) -> list[dict[str, Any]]:
    """Return sources whose sidecars this resolution pass may hydrate."""
    hydration_sources: list[dict[str, Any]] = []
    for event_source in event_sources:
        bundled_replacement = _bundled_replacement_source(event_source)
        if bundled_replacement is not None:
            hydration_sources.append(bundled_replacement)
        if hydrate_sidecars or EventInfo.from_event(event_source).is_edit:
            hydration_sources.append(event_source)
    return hydration_sources


@dataclass(slots=True)
class _ResolvedThreadEventSources:
    """One resolution pass over raw thread rows."""

    messages: list[ResolvedVisibleMessage]
    sidecar_hydration_ms: float
    input_order_by_event_id: dict[str, int]
    related_event_id_by_event_id: dict[str, str]


async def _resolve_thread_history_from_event_sources_timed(
    client: nio.AsyncClient,
    *,
    room_id: str,
    thread_id: str,
    event_sources: Sequence[dict[str, Any]],
    hydrate_sidecars: bool = True,
    event_cache: ConversationEventCache,
    expected_membership_epoch: int | None = None,
    trusted_sender_ids: Collection[str] = (),
    register_sidecar_owners: bool = False,
) -> _ResolvedThreadEventSources:
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
    edit_candidates = ThreadEditCandidates()
    sidecar_hydration_started = time.perf_counter()
    hydration_sources = _sidecar_hydration_sources(event_sources, hydrate_sidecars=hydrate_sidecars)
    hydration_batch = await prepare_sidecar_hydration_batch(
        hydration_sources,
        event_cache=event_cache,
        room_id=room_id,
        expected_membership_epoch=expected_membership_epoch,
        register_owners=register_sidecar_owners,
    )
    for event in parsed_events:
        event_info = EventInfo.from_event(event.source)
        bundled_replacement_source = _bundled_replacement_source(event.source)
        if bundled_replacement_source is not None:
            bundled_replacement = nio.Event.parse_event(bundled_replacement_source)
            if isinstance(bundled_replacement, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES):
                edit_candidates.record(
                    bundled_replacement,
                    event_info=EventInfo.from_event(bundled_replacement.source),
                )
        if isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES) and edit_candidates.record(
            event,
            event_info=event_info,
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
                expected_membership_epoch=expected_membership_epoch,
                hydration_batch=hydration_batch,
                trusted_sender_ids=trusted_sender_ids,
            )
            if hydrate_sidecars
            else _snapshot_message_dict(event, trusted_sender_ids=trusted_sender_ids)
        )

    await apply_latest_edits_to_messages(
        client,
        messages_by_event_id=messages_by_event_id,
        edit_candidates=edit_candidates,
        required_thread_id=thread_id,
        event_cache=event_cache,
        room_id=room_id,
        expected_membership_epoch=expected_membership_epoch,
        hydration_batch=hydration_batch,
        trusted_sender_ids=trusted_sender_ids,
    )
    messages = list(messages_by_event_id.values())
    sort_thread_messages_root_first(
        messages,
        thread_id=thread_id,
        input_order_by_event_id=input_order_by_event_id,
        related_event_id_by_event_id=related_event_id_by_event_id,
    )
    return _ResolvedThreadEventSources(
        messages=messages,
        sidecar_hydration_ms=elapsed_ms_since(sidecar_hydration_started, clock=time.perf_counter),
        input_order_by_event_id=input_order_by_event_id,
        related_event_id_by_event_id=related_event_id_by_event_id,
    )


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
    cached_membership_epoch = await _capture_membership_epoch(event_cache, room_id)
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
    cached_rejection_reason = _thread_history_cache_rejection_reason(cached_event_sources, thread_id=thread_id)
    if cached_rejection_reason is not None:
        logger.warning(
            "Stale thread cache is incomplete; refusing degraded history",
            room_id=room_id,
            thread_id=thread_id,
            error=str(fetch_error),
            cache_rejection_reason=cached_rejection_reason,
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
        expected_membership_epoch=cached_membership_epoch,
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
        "cache_read_ms": elapsed_ms_since(cache_read_started, clock=time.perf_counter),
        "resolution_ms": elapsed_ms_since(resolution_started, clock=time.perf_counter),
        "sidecar_hydration_ms": sidecar_hydration_ms,
        THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_STALE_CACHE,
        THREAD_HISTORY_ERROR_DIAGNOSTIC: str(fetch_error),
        THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
    }
    if cache_reject_diagnostics is not None:
        diagnostics.update(cache_reject_diagnostics)
    # Same rule as the trusted-cache path: a cached read cannot drop messages, so completeness
    # turns only on whether sidecars were hydrated. This result is already flagged degraded, but
    # is_full_history is a separate signal gating planning completeness and the model refresh.
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
    expected_membership_epoch: int,
    trusted_sender_ids: Collection[str] = (),
) -> tuple[list[ResolvedVisibleMessage] | None, float]:
    """Resolve cached thread history or invalidate the cache entry on corruption."""
    try:
        resolved = await _resolve_thread_history_from_event_sources_timed(
            client,
            room_id=room_id,
            thread_id=thread_id,
            event_sources=cached_event_sources,
            hydrate_sidecars=hydrate_sidecars,
            event_cache=event_cache,
            expected_membership_epoch=expected_membership_epoch,
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
    return resolved.messages, resolved.sidecar_hydration_ms


def _cache_reject_diagnostics(gap: ThreadCacheGap) -> dict[str, str | int | float | bool]:
    """Describe one gap-marked snapshot for the read that refused it.

    The rejection reason comes from ``thread_cache_gap_reason`` rather than an argument, so
    the label a read logs and the gate that produced it cannot drift apart.
    """
    diagnostics: dict[str, str | int | float | bool] = {
        THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC: thread_cache_gap_reason(gap),
        "cache_gap_marked_at": gap.gap_marked_at,
        "cache_gap_age_ms": elapsed_ms_since(gap.gap_marked_at, clock=time.time),
    }
    if gap.gap_reason is not None:
        diagnostics["cache_gap_reason"] = gap.gap_reason
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
    cached_membership_epoch = await _capture_membership_epoch(event_cache, room_id)
    gap = await event_cache.get_thread_cache_gap(room_id, thread_id)
    if gap is not None:
        cache_reject_diagnostics = _cache_reject_diagnostics(gap)
        logger.info(
            "Thread cache rejected for read",
            room_id=room_id,
            thread_id=thread_id,
            **cache_reject_diagnostics,
        )
        return None, cache_reject_diagnostics

    resolution_started = time.perf_counter()
    cache_read_started = time.perf_counter()
    cached_event_sources = await event_cache.get_thread_events(room_id, thread_id)
    cache_read_ms = elapsed_ms_since(cache_read_started, clock=time.perf_counter)
    if cached_event_sources is None:
        return None, {THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC: "cache_rows_missing"}
    cached_rejection_reason = _thread_history_cache_rejection_reason(cached_event_sources, thread_id=thread_id)
    if cached_rejection_reason is not None:
        await _invalidate_thread_cache_entry(event_cache, room_id=room_id, thread_id=thread_id)
        payload_reject_diagnostics: dict[str, str | int | float | bool] = {
            THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC: f"cache_{cached_rejection_reason}",
        }
        logger.info(
            "Thread cache rejected for read",
            room_id=room_id,
            thread_id=thread_id,
            **payload_reject_diagnostics,
        )
        return None, payload_reject_diagnostics

    resolved_history, sidecar_hydration_ms = await _resolve_cached_thread_history(
        client,
        room_id=room_id,
        thread_id=thread_id,
        event_cache=event_cache,
        cached_event_sources=cached_event_sources,
        hydrate_sidecars=hydrate_sidecars,
        expected_membership_epoch=cached_membership_epoch,
        trusted_sender_ids=trusted_sender_ids,
    )
    if resolved_history is None:
        return None, {THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC: "cache_payload_unresolvable"}

    # A window that left messages out is not full history, whatever its sidecars did. Callers gate
    # completeness-dependent planning and the model-history refresh on this flag, so claiming a
    # truncated tail is complete silently drops older participants and mentions from the context.
    return _thread_history_result(
        resolved_history,
        is_full_history=hydrate_sidecars,
        diagnostics={
            "cache_read_ms": cache_read_ms,
            "resolution_ms": elapsed_ms_since(resolution_started, clock=time.perf_counter),
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
    expected_membership_epoch: int,
    trusted_sender_ids: Collection[str] = (),
) -> _ThreadHistoryFetchResult:
    """Fetch thread history and raw event sources from the homeserver."""
    return await _fetch_thread_history_via_room_messages_with_events(
        client,
        room_id,
        thread_id,
        hydrate_sidecars=hydrate_sidecars,
        event_cache=event_cache,
        expected_membership_epoch=expected_membership_epoch,
        trusted_sender_ids=trusted_sender_ids,
    )


async def _reject_opaque_thread_snapshot(
    event_cache: ConversationEventCache,
    *,
    room_id: str,
    thread_id: str,
    fetch_result: _ThreadHistoryFetchResult,
) -> None:
    """Reject an opaque-poisoned reconstruction before its guarded store."""
    if not any(is_opaque_encrypted_event_source(source) for source in fetch_result.event_sources):
        return
    await _mark_thread_gap_for_opaque_history(event_cache, room_id=room_id, thread_id=thread_id)
    msg = f"thread history for {thread_id} contains still-undecryptable encrypted events"
    raise OpaqueEncryptedThreadHistoryError(msg)


@dataclass(frozen=True, slots=True)
class _ThreadCacheStoreResult:
    """What one snapshot store attempt did.

    ``written`` and ``failed`` are independent, not two views of one flag: a cache whose writes are
    unavailable stores nothing without failing, and operators reading the two diagnostics keys need
    to tell that case apart from a genuine write fault.
    """

    written: bool
    failed: bool


async def _store_reconstructed_thread_snapshot(
    *,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
    fetch_result: _ThreadHistoryFetchResult,
    membership_epoch: int,
    fetch_started_at: float,
) -> _ThreadCacheStoreResult:
    """Install one reconstructed snapshot and report what the store attempt did."""
    await _reject_opaque_thread_snapshot(
        event_cache,
        room_id=room_id,
        thread_id=thread_id,
        fetch_result=fetch_result,
    )
    store_result = await _store_thread_history_cache(
        event_cache,
        room_id=room_id,
        thread_id=thread_id,
        event_sources=fetch_result.event_sources,
        expected_membership_epoch=membership_epoch,
        fetch_started_at=fetch_started_at,
    )
    logger.info(
        "Thread history cache store completed",
        room_id=room_id,
        thread_id=thread_id,
        cache_store_written=store_result.written,
        cache_store_failed=store_result.failed,
        event_count=len(fetch_result.event_sources),
        homeserver_scan_pages=fetch_result.room_scan_pages,
        homeserver_scanned_event_count=fetch_result.scanned_event_count,
        homeserver_thread_event_count=len(fetch_result.event_sources),
        homeserver_scan_parse_cpu_ms=fetch_result.homeserver_scan_parse_cpu_ms,
    )
    return store_result


def _homeserver_thread_history_result(
    fetch_result: _ThreadHistoryFetchResult,
    *,
    hydrate_sidecars: bool,
    store_result: _ThreadCacheStoreResult,
    cache_reject_diagnostics: Mapping[str, str | int | float | bool] | None,
) -> ThreadHistoryResult:
    """Build the fail-open homeserver result after one reconstruct-and-store."""
    diagnostics: dict[str, str | int | float | bool] = {
        "cache_read_ms": 0.0,
        "homeserver_fetch_ms": fetch_result.fetch_ms,
        "homeserver_scan_pages": fetch_result.room_scan_pages,
        "homeserver_scanned_event_count": fetch_result.scanned_event_count,
        "homeserver_thread_event_count": len(fetch_result.event_sources),
        "resolution_ms": fetch_result.resolution_ms,
        "sidecar_hydration_ms": fetch_result.sidecar_hydration_ms,
        "homeserver_scan_parse_cpu_ms": fetch_result.homeserver_scan_parse_cpu_ms,
        "cache_store_written": store_result.written,
        "cache_store_failed": store_result.failed,
        THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_HOMESERVER,
    }
    if cache_reject_diagnostics is not None:
        diagnostics.update(cache_reject_diagnostics)
    return _thread_history_result(
        fetch_result.history,
        is_full_history=hydrate_sidecars,
        diagnostics=diagnostics,
    )


async def refresh_thread_history_from_source(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
    *,
    hydrate_sidecars: bool = True,
    allow_stale_fallback: bool = True,
    cache_reject_diagnostics: Mapping[str, str | int | float | bool] | None = None,
    trusted_sender_ids: Collection[str] = (),
    caller_label: str | None = None,
    coordinator_queue_wait_ms: float = 0.0,
    post_coordinator_read_started: float | None = None,
) -> ThreadHistoryResult:
    """Fetch fresh thread history from Matrix and repopulate the advisory cache.

    One fetch, one store. There is no retry loop: a replacement cannot lose a race any more, and a
    gap that lands mid-fetch survives the store so the next read refetches it.
    """
    resolved_post_coordinator_read_started = (
        time.perf_counter() if post_coordinator_read_started is None else post_coordinator_read_started
    )
    fetch_started_at = time.time()
    fetch_membership_epoch = await _capture_membership_epoch(event_cache, room_id)
    try:
        fetch_result = await _fetch_thread_history_with_events(
            client,
            room_id,
            thread_id,
            hydrate_sidecars=hydrate_sidecars,
            event_cache=event_cache,
            expected_membership_epoch=fetch_membership_epoch,
            trusted_sender_ids=trusted_sender_ids,
        )
    except _UnresolvedOpaqueRoomHistoryError:
        await _mark_room_gap_for_opaque_history(event_cache, room_id=room_id)
        raise
    except Exception as exc:
        stale_history = (
            await _load_stale_cached_thread_history(
                client,
                room_id=room_id,
                thread_id=thread_id,
                event_cache=event_cache,
                hydrate_sidecars=hydrate_sidecars,
                fetch_error=exc,
                cache_reject_diagnostics=cache_reject_diagnostics,
                trusted_sender_ids=trusted_sender_ids,
            )
            if allow_stale_fallback
            else None
        )
        if stale_history is not None:
            return _report_direct_source_refresh(
                stale_history,
                room_id=room_id,
                thread_id=thread_id,
                caller_label=caller_label,
                coordinator_queue_wait_ms=coordinator_queue_wait_ms,
                post_coordinator_read_started=resolved_post_coordinator_read_started,
            )
        raise
    store_result = await _store_reconstructed_thread_snapshot(
        room_id=room_id,
        thread_id=thread_id,
        event_cache=event_cache,
        fetch_result=fetch_result,
        membership_epoch=fetch_membership_epoch,
        fetch_started_at=fetch_started_at,
    )
    if not store_result.written:
        # A cache that cannot accept writes is the condition operators most need to see, and it is
        # otherwise only logged at INFO.
        logger.warning(
            "Thread cache refill did not install a snapshot",
            room_id=room_id,
            thread_id=thread_id,
        )
    return _report_direct_source_refresh(
        _homeserver_thread_history_result(
            fetch_result,
            hydrate_sidecars=hydrate_sidecars,
            store_result=store_result,
            cache_reject_diagnostics=cache_reject_diagnostics,
        ),
        room_id=room_id,
        thread_id=thread_id,
        caller_label=caller_label,
        coordinator_queue_wait_ms=coordinator_queue_wait_ms,
        post_coordinator_read_started=resolved_post_coordinator_read_started,
    )


async def _store_thread_history_cache(
    event_cache: ConversationEventCache,
    *,
    room_id: str,
    thread_id: str,
    event_sources: Sequence[dict[str, Any]],
    expected_membership_epoch: int,
    fetch_started_at: float,
) -> _ThreadCacheStoreResult:
    """Best-effort replacement of one cached thread snapshot."""
    try:
        written = await event_cache.replace_thread(
            room_id,
            thread_id,
            list(event_sources),
            expected_membership_epoch=expected_membership_epoch,
            fetch_started_at=fetch_started_at,
        )
    except Exception as exc:
        logger.warning(
            "Event cache write failed; continuing without cache",
            room_id=room_id,
            thread_id=thread_id,
            event_count=len(event_sources),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return _ThreadCacheStoreResult(written=False, failed=True)
    return _ThreadCacheStoreResult(written=written, failed=False)


def _thread_history_cache_rejection_reason(
    event_sources: Sequence[dict[str, Any]],
    *,
    thread_id: str,
) -> str | None:
    """Return why one thread event payload cannot become an authoritative snapshot."""
    if any(is_opaque_encrypted_event_source(event_source) for event_source in event_sources):
        return _OPAQUE_ENCRYPTED_EVENT_REJECTION
    if not any(_event_id_from_source(event_source) == thread_id for event_source in event_sources):
        return _MISSING_THREAD_ROOT_REJECTION
    return None


async def _mark_thread_gap_for_opaque_history(
    event_cache: ConversationEventCache,
    *,
    room_id: str,
    thread_id: str,
) -> None:
    """Keep one opaque-poisoned thread durably gapped, deleting the snapshot only when the marker fails."""
    await mark_thread_gap_fail_closed(
        event_cache,
        room_id=room_id,
        thread_id=thread_id,
        reason=_OPAQUE_ENCRYPTED_THREAD_HISTORY_REASON,
        logger=logger,
    )


async def _mark_room_gap_for_opaque_history(
    event_cache: ConversationEventCache,
    *,
    room_id: str,
) -> None:
    """Keep every thread gapped when opaque relation impact cannot be scoped within the room."""
    await mark_room_threads_gap_fail_closed(
        event_cache,
        room_id=room_id,
        reason=_OPAQUE_ENCRYPTED_THREAD_HISTORY_REASON,
        logger=logger,
    )


async def _resolve_thread_history_message(
    event: nio.Event,
    client: nio.AsyncClient,
    *,
    event_cache: ConversationEventCache,
    room_id: str,
    expected_membership_epoch: int | None = None,
    hydration_batch: SidecarHydrationBatch | None = None,
    trusted_sender_ids: Collection[str] = (),
) -> ResolvedVisibleMessage:
    """Resolve one room-message event into the normalized thread-history shape."""
    if isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES):
        message_data = await extract_and_resolve_message(
            event,
            client,
            event_cache=event_cache,
            room_id=room_id,
            expected_membership_epoch=expected_membership_epoch,
            hydration_batch=hydration_batch,
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
        expected_membership_epoch=expected_membership_epoch,
        hydration_batch=hydration_batch,
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


async def _fetch_thread_history_with_cache_policy(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
    *,
    hydrate_sidecars: bool,
    allow_stale_fallback: bool,
    cache_read_failure_message: str,
    trusted_sender_ids: Collection[str] = (),
    caller_label: str = "unknown",
    coordinator_queue_wait_ms: float = 0.0,
    post_coordinator_read_started: float | None,
    refill: _ThreadHistoryRefill | None = None,
) -> ThreadHistoryResult:
    """Serve one trusted cache hit or delegate only the required refill."""
    resolved_post_coordinator_read_started = (
        time.perf_counter() if post_coordinator_read_started is None else post_coordinator_read_started
    )
    cache_reject_diagnostics: dict[str, str | int | float | bool] | None = None
    cached_history: ThreadHistoryResult | None = None
    try:
        cached_history, cache_reject_diagnostics = await _load_cached_thread_history_if_usable(
            client,
            room_id=room_id,
            thread_id=thread_id,
            event_cache=event_cache,
            hydrate_sidecars=hydrate_sidecars,
            trusted_sender_ids=trusted_sender_ids,
        )
    except Exception as exc:
        logger.warning(
            cache_read_failure_message,
            room_id=room_id,
            thread_id=thread_id,
            error=str(exc),
        )
    if cached_history is not None:
        result = cached_history
    elif refill is not None:
        result = await refill(cache_reject_diagnostics)
    else:
        result = await refresh_thread_history_from_source(
            client,
            room_id,
            thread_id,
            event_cache,
            hydrate_sidecars=hydrate_sidecars,
            allow_stale_fallback=allow_stale_fallback,
            cache_reject_diagnostics=cache_reject_diagnostics,
            trusted_sender_ids=trusted_sender_ids,
        )
    log_thread_history_refresh(
        room_id=room_id,
        thread_id=thread_id,
        caller_label=caller_label,
        mode="cache_hit" if cached_history is not None else "full_scan",
        diagnostics=result.diagnostics,
        coordinator_queue_wait_ms=coordinator_queue_wait_ms,
        post_coordinator_read_started=resolved_post_coordinator_read_started,
    )
    return result


async def fetch_thread_history(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
    *,
    trusted_sender_ids: Collection[str] = (),
    caller_label: str = "unknown",
    coordinator_queue_wait_ms: float = 0.0,
    post_coordinator_read_started: float | None = None,
    refill: _ThreadHistoryRefill | None = None,
) -> ThreadHistoryResult:
    """Fetch all messages in a thread, allowing advisory stale fallback.

    Cached reads are collapsed but never truncated, so a caller whose output is the history
    itself - export, and anything else that must be complete rather than recent - gets every
    message. ``is_full_history`` still reports false when sidecar hydration was skipped.
    """
    return await _fetch_thread_history_with_cache_policy(
        client,
        room_id,
        thread_id,
        event_cache,
        hydrate_sidecars=True,
        allow_stale_fallback=True,
        cache_read_failure_message="Durable thread cache read failed; refetching from homeserver",
        trusted_sender_ids=trusted_sender_ids,
        caller_label=caller_label,
        coordinator_queue_wait_ms=coordinator_queue_wait_ms,
        post_coordinator_read_started=post_coordinator_read_started,
        refill=refill,
    )


async def fetch_dispatch_thread_history(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
    *,
    trusted_sender_ids: Collection[str] = (),
    caller_label: str = "unknown",
    coordinator_queue_wait_ms: float = 0.0,
    post_coordinator_read_started: float | None = None,
    refill: _ThreadHistoryRefill | None = None,
) -> ThreadHistoryResult:
    """Fetch strict full thread history from trusted cache or a fresh refill."""
    return await _fetch_thread_history_with_cache_policy(
        client,
        room_id,
        thread_id,
        event_cache,
        hydrate_sidecars=True,
        allow_stale_fallback=False,
        cache_read_failure_message="Durable dispatch thread cache read failed; refetching from homeserver",
        trusted_sender_ids=trusted_sender_ids,
        caller_label=caller_label,
        coordinator_queue_wait_ms=coordinator_queue_wait_ms,
        post_coordinator_read_started=post_coordinator_read_started,
        refill=refill,
    )


async def fetch_dispatch_thread_snapshot(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
    *,
    trusted_sender_ids: Collection[str] = (),
    caller_label: str = "unknown",
    coordinator_queue_wait_ms: float = 0.0,
    post_coordinator_read_started: float | None = None,
    refill: _ThreadHistoryRefill | None = None,
) -> ThreadHistoryResult:
    """Fetch strict lightweight dispatch context from trusted cache or a fresh refill."""
    return await _fetch_thread_history_with_cache_policy(
        client,
        room_id,
        thread_id,
        event_cache,
        hydrate_sidecars=False,
        allow_stale_fallback=False,
        cache_read_failure_message="Durable dispatch thread cache read failed; refetching snapshot from homeserver",
        trusted_sender_ids=trusted_sender_ids,
        caller_label=caller_label,
        coordinator_queue_wait_ms=coordinator_queue_wait_ms,
        post_coordinator_read_started=post_coordinator_read_started,
        refill=refill,
    )


async def _fetch_thread_history_via_room_messages_with_events(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    *,
    hydrate_sidecars: bool,
    event_cache: ConversationEventCache,
    expected_membership_epoch: int,
    trusted_sender_ids: Collection[str] = (),
) -> _ThreadHistoryFetchResult:
    """Fetch all thread messages by scanning room history pages."""
    fetch_started = time.perf_counter()
    scan_result = await fetch_thread_event_sources_via_room_messages(client, room_id, thread_id)
    resolution_started = time.perf_counter()
    resolution = await _resolve_thread_history_from_event_sources_timed(
        client,
        room_id=room_id,
        thread_id=thread_id,
        event_sources=scan_result.event_sources,
        hydrate_sidecars=hydrate_sidecars,
        event_cache=event_cache,
        expected_membership_epoch=expected_membership_epoch,
        trusted_sender_ids=trusted_sender_ids,
        register_sidecar_owners=True,
    )
    return _ThreadHistoryFetchResult(
        history=resolution.messages,
        event_sources=scan_result.event_sources,
        fetch_ms=elapsed_ms_since(fetch_started, clock=time.perf_counter),
        room_scan_pages=scan_result.page_count,
        scanned_event_count=scan_result.scanned_event_count,
        resolution_ms=elapsed_ms_since(resolution_started, clock=time.perf_counter),
        sidecar_hydration_ms=resolution.sidecar_hydration_ms,
        homeserver_scan_parse_cpu_ms=scan_result.homeserver_scan_parse_cpu_ms,
    )


def _is_opaque_thread_affecting_event_source(event_source: Mapping[str, Any]) -> bool:
    """Return whether one scanned payload is undecrypted ciphertext with exposed thread-affecting relations."""
    if not is_opaque_encrypted_event_source(event_source):
        return False
    event_info = EventInfo.from_event(dict(event_source))
    return is_thread_affecting_relation(event_info, event_type=event_info.event_type)


def _record_scanned_room_message_source(
    event: nio.Event,
    *,
    edit_candidates: ThreadEditCandidates,
    scanned_message_sources: dict[str, dict[str, Any]],
) -> str | None:
    """Record one scanned room-message source and return the recorded event ID."""
    event_source = event.source if isinstance(event.source, dict) else {}
    if _is_opaque_thread_affecting_event_source(event_source):
        # Undecryptable relation-bearing ciphertext is recorded as fail-closed evidence: it resolves
        # thread membership through its exposed relation and poisons only that reconstruction.
        scanned_message_sources[event.event_id] = _event_source_for_cache(event)
        return event.event_id
    if not _is_room_message_event(event):
        return None

    event_info = EventInfo.from_event(event.source)
    if isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES) and edit_candidates.record(
        event,
        event_info=event_info,
    ):
        return None
    if event_info.is_edit:
        return None

    scanned_message_sources[event.event_id] = _event_source_for_cache(event)
    return event.event_id


async def fetch_thread_event_sources_via_room_messages(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
) -> _ThreadEventSourceScanResult:
    """Fetch one thread's event sources by scanning room history pages."""
    scan_result = await _bulk_scan_thread_event_sources(client, room_id, thread_root_ids=(thread_id,))
    if thread_id in scan_result.missing_root_ids:
        msg = f"thread root {thread_id} not found during room scan"
        logger.warning(
            "Thread room scan ended without finding root",
            room_id=room_id,
            thread_id=thread_id,
            user_id=client.user_id,
            room_scan_pages=scan_result.page_count,
            scanned_event_count=scan_result.scanned_event_count,
        )
        raise ThreadRoomScanRootNotFoundError(msg)
    if scan_result.unresolved_opaque_event_ids:
        logger.warning(
            "Thread room scan contains opaque encrypted relations with unresolved impact",
            room_id=room_id,
            thread_id=thread_id,
            user_id=client.user_id,
            unresolved_opaque_event_ids=sorted(scan_result.unresolved_opaque_event_ids),
        )
        msg = f"thread history scan for {thread_id} contains undecryptable events with unresolved thread impact"
        raise _UnresolvedOpaqueRoomHistoryError(msg)
    return _ThreadEventSourceScanResult(
        event_sources=scan_result.thread_event_sources[thread_id],
        page_count=scan_result.page_count,
        scanned_event_count=scan_result.scanned_event_count,
        homeserver_scan_parse_cpu_ms=scan_result.homeserver_scan_parse_cpu_ms,
    )


@dataclass(frozen=True)
class _BulkThreadScanResult:
    """Per-thread event sources recovered by one backward room scan."""

    thread_event_sources: dict[str, list[dict[str, Any]]]
    missing_root_ids: frozenset[str]
    unresolved_opaque_event_ids: frozenset[str]
    page_count: int
    scanned_event_count: int
    scan_truncated: bool
    homeserver_scan_parse_cpu_ms: float = 0.0


@dataclass(frozen=True)
class BulkThreadRefreshStats:
    """Summary for one bulk thread-cache refresh pass over a room."""

    requested_threads: int
    usable_threads: int
    missing_root_ids: frozenset[str]
    room_scan_pages: int
    scanned_event_count: int
    scan_truncated: bool = False


async def _unresolved_opaque_relation_event_ids(
    room_id: str,
    *,
    event_infos: dict[str, EventInfo],
    scanned_message_sources: dict[str, dict[str, Any]],
    resolved_thread_ids: dict[str, str],
) -> frozenset[str]:
    """Return scanned opaque relation-bearing events whose thread impact stays unknown."""
    access = map_backed_thread_membership_access(
        event_infos=event_infos,
        resolved_thread_ids=resolved_thread_ids,
    )
    unresolved_event_ids: set[str] = set()
    for event_id, event_source in scanned_message_sources.items():
        if event_id in resolved_thread_ids or not is_opaque_encrypted_event_source(event_source):
            continue
        resolution = await resolve_event_thread_membership(
            room_id,
            event_infos[event_id],
            access=access,
        )
        if resolution.state is ThreadResolutionState.INDETERMINATE:
            unresolved_event_ids.add(event_id)
    return frozenset(unresolved_event_ids)


def _scanned_event_sender(event_source: dict[str, Any] | None) -> str | None:
    """Return one scanned event's sender, or None when the event was never scanned."""
    if event_source is None:
        return None
    sender = event_source.get("sender")
    return sender if isinstance(sender, str) else None


async def _group_scanned_sources_by_thread(
    *,
    room_id: str,
    thread_root_ids: Collection[str],
    scanned_message_sources: dict[str, dict[str, Any]],
    edit_candidates: ThreadEditCandidates,
) -> tuple[dict[str, list[dict[str, Any]]], frozenset[str]]:
    """Bucket room-scan sources per requested thread and report unresolved opaque relations."""
    grouped: dict[str, dict[str, dict[str, Any]]] = {
        root_id: {root_id: scanned_message_sources[root_id]}
        for root_id in thread_root_ids
        if root_id in scanned_message_sources
    }
    if not grouped:
        return {}, frozenset()
    event_infos = {
        event_id: EventInfo.from_event(event_source) for event_id, event_source in scanned_message_sources.items()
    }
    ordered_event_ids = ordered_event_ids_from_scanned_event_sources(scanned_message_sources.values())
    resolved_thread_ids = await resolve_thread_ids_for_event_infos(
        room_id,
        event_infos=event_infos,
        ordered_event_ids=ordered_event_ids,
    )
    for event_id in ordered_event_ids:
        root_id = resolved_thread_ids.get(event_id)
        if root_id is None or root_id == event_id:
            continue
        bucket = grouped.get(root_id)
        if bucket is None or event_id in bucket:
            continue
        bucket[event_id] = scanned_message_sources[event_id]

    unresolved_opaque_event_ids = await _unresolved_opaque_relation_event_ids(
        room_id,
        event_infos=event_infos,
        scanned_message_sources=scanned_message_sources,
        resolved_thread_ids=resolved_thread_ids,
    )

    edits_by_root: dict[str, list[dict[str, Any]]] = {}
    for original_event_id in edit_candidates.original_event_ids():
        winner = edit_candidates.winner_for(
            original_event_id,
            sender=_scanned_event_sender(scanned_message_sources.get(original_event_id)),
        )
        if winner is None:
            continue
        edit_event, edit_thread_id = winner
        target_roots = {
            root_id
            for root_id in (original_event_id, resolved_thread_ids.get(original_event_id), edit_thread_id)
            if root_id in grouped
        }
        for root_id in target_roots:
            edits_by_root.setdefault(root_id, []).append(_event_source_for_cache(edit_event))

    grouped_sources = {
        root_id: sort_thread_event_sources_root_first(
            [*bucket.values(), *edits_by_root.get(root_id, [])],
            thread_id=root_id,
        )
        for root_id, bucket in grouped.items()
    }
    return grouped_sources, unresolved_opaque_event_ids


async def _bulk_scan_thread_event_sources(
    client: nio.AsyncClient,
    room_id: str,
    *,
    thread_root_ids: Collection[str],
    max_scan_pages: int | None = None,
) -> _BulkThreadScanResult:
    """Walk room history backward once and recover every requested thread's event sources."""
    if max_scan_pages is not None and max_scan_pages < 1:
        msg = "max_scan_pages must be at least 1"
        raise ValueError(msg)
    edit_candidates = ThreadEditCandidates()
    scanned_message_sources: dict[str, dict[str, Any]] = {}
    remaining_root_ids = set(thread_root_ids)
    from_token: str | None = None
    page_count = 0
    scanned_event_count = 0
    scan_truncated = False
    homeserver_scan_parse_cpu_ms = 0.0

    while remaining_root_ids:
        if max_scan_pages is not None and page_count >= max_scan_pages:
            scan_truncated = True
            break
        response = await client.room_messages(
            room_id,
            start=from_token,
            limit=100,
            message_filter={"types": list(_ROOM_HISTORY_MESSAGE_TYPES)},
            direction=nio.MessageDirection.back,
        )
        if not isinstance(response, nio.RoomMessagesResponse):
            msg = f"bulk room scan failed for {room_id}: {response}"
            logger.error(
                "Failed bulk thread history scan",
                room_id=room_id,
                user_id=client.user_id,
                error=str(response),
            )
            raise RuntimeError(msg)  # noqa: TRY004
        if not response.chunk:
            break
        page_count += 1
        parse_cpu_started = time.thread_time()
        for event in response.chunk:
            if not isinstance(event, nio.Event):
                continue
            scanned_event_count += 1
            recorded_event_id = _record_scanned_room_message_source(
                event,
                edit_candidates=edit_candidates,
                scanned_message_sources=scanned_message_sources,
            )
            if recorded_event_id is not None:
                remaining_root_ids.discard(recorded_event_id)
        homeserver_scan_parse_cpu_ms += elapsed_ms_since(parse_cpu_started, clock=time.thread_time)
        if not response.end:
            break
        from_token = response.end

    thread_event_sources, unresolved_opaque_event_ids = await _group_scanned_sources_by_thread(
        room_id=room_id,
        thread_root_ids=thread_root_ids,
        scanned_message_sources=scanned_message_sources,
        edit_candidates=edit_candidates,
    )
    return _BulkThreadScanResult(
        thread_event_sources=thread_event_sources,
        missing_root_ids=frozenset(remaining_root_ids),
        unresolved_opaque_event_ids=unresolved_opaque_event_ids,
        page_count=page_count,
        scanned_event_count=scanned_event_count,
        scan_truncated=scan_truncated,
        homeserver_scan_parse_cpu_ms=homeserver_scan_parse_cpu_ms,
    )


async def bulk_refresh_room_thread_histories(
    client: nio.AsyncClient,
    room_id: str,
    event_cache: ConversationEventCache,
    *,
    thread_root_ids: Collection[str],
    caller_label: str = "unknown",
    max_scan_pages: int | None = None,
) -> BulkThreadRefreshStats:
    """Warm the durable thread cache for many threads with one backward room scan.

    The per-thread refresh walks room history until it sees that one thread's root, so bulk
    backfills of dormant rooms degrade to O(threads x history) homeserver work. This performs one
    O(history) walk, buckets every scanned event with the same canonical resolution rules as the
    per-thread path, and stores each requested thread through the same
    ``replace_thread`` path. Threads whose root never appeared in the scan are
    reported in ``missing_root_ids`` and never stored. A caller-provided page budget stops the scan
    with remaining roots reported as missing and ``scan_truncated`` set. Threads whose reconstruction
    contains still-opaque encrypted evidence are gap-marked instead of stored, and a scan holding
    opaque relations with unresolved impact gap-marks every requested thread.
    """
    fetch_started_at = time.time()
    fetch_membership_epoch = await _capture_membership_epoch(event_cache, room_id)
    scan_result = await _bulk_scan_thread_event_sources(
        client,
        room_id,
        thread_root_ids=thread_root_ids,
        max_scan_pages=max_scan_pages,
    )
    usable_threads = 0
    opaque_gap_threads = 0
    if scan_result.unresolved_opaque_event_ids:
        logger.warning(
            "Bulk thread refresh scan contains opaque encrypted relations with unresolved impact",
            room_id=room_id,
            caller_label=caller_label,
            user_id=client.user_id,
            unresolved_opaque_event_ids=sorted(scan_result.unresolved_opaque_event_ids),
        )
        await _mark_room_gap_for_opaque_history(event_cache, room_id=room_id)
        opaque_gap_threads = len(set(thread_root_ids))
    else:
        for thread_id, event_sources in scan_result.thread_event_sources.items():
            rejection_reason = _thread_history_cache_rejection_reason(event_sources, thread_id=thread_id)
            if rejection_reason == _OPAQUE_ENCRYPTED_EVENT_REJECTION:
                await _mark_thread_gap_for_opaque_history(event_cache, room_id=room_id, thread_id=thread_id)
                opaque_gap_threads += 1
                continue
            if rejection_reason is not None:
                continue
            store_result = await _store_thread_history_cache(
                event_cache,
                room_id=room_id,
                thread_id=thread_id,
                event_sources=event_sources,
                expected_membership_epoch=fetch_membership_epoch,
                fetch_started_at=fetch_started_at,
            )
            if store_result.written:
                usable_threads += 1
    stats = BulkThreadRefreshStats(
        requested_threads=len(set(thread_root_ids)),
        usable_threads=usable_threads,
        missing_root_ids=scan_result.missing_root_ids,
        room_scan_pages=scan_result.page_count,
        scanned_event_count=scan_result.scanned_event_count,
        scan_truncated=scan_result.scan_truncated,
    )
    logger.info(
        "Bulk thread cache refresh completed",
        room_id=room_id,
        caller_label=caller_label,
        requested_threads=stats.requested_threads,
        usable_threads=stats.usable_threads,
        opaque_gap_threads=opaque_gap_threads,
        missing_roots=len(stats.missing_root_ids),
        room_scan_pages=stats.room_scan_pages,
        scanned_event_count=stats.scanned_event_count,
        scan_truncated=stats.scan_truncated,
    )
    return stats


async def thread_ids_needing_refill(
    event_cache: ConversationEventCache,
    room_id: str,
    thread_ids: Collection[str],
) -> tuple[str, ...]:
    """Return the given threads whose durable snapshots would not be served from cache.

    Two ways a thread fails to serve, and both have to be asked about: it carries a gap marker, or
    it has no snapshot at all. Checking only the marker silently reports every never-cached thread
    as a cache hit, which turns startup prewarm into a no-op.
    """
    reads = await asyncio.gather(
        *(
            asyncio.gather(
                event_cache.get_thread_cache_gap(room_id, thread_id),
                event_cache.has_thread_snapshot(room_id, thread_id),
            )
            for thread_id in thread_ids
        ),
    )
    return tuple(
        thread_id
        for thread_id, (gap, has_snapshot) in zip(thread_ids, reads, strict=True)
        if thread_cache_rejection_reason(gap) is not None or not has_snapshot
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


def _append_unique_thread_root_ids(
    thread_roots: Iterable[nio.Event],
    thread_root_ids: list[str],
    seen_thread_root_ids: set[str],
    *,
    max_thread_roots: int,
) -> tuple[int, bool]:
    """Append unseen thread roots up to the cap and report discarded roots."""
    new_root_count = 0
    for thread_root in thread_roots:
        thread_root_id = thread_root.event_id
        if not thread_root_id:
            continue
        if thread_root_id in seen_thread_root_ids:
            continue
        if len(thread_root_ids) >= max_thread_roots:
            return new_root_count, True
        seen_thread_root_ids.add(thread_root_id)
        thread_root_ids.append(thread_root_id)
        new_root_count += 1

    return new_root_count, False


def _non_empty_thread_root_page_truncated(
    *,
    discarded_due_to_cap: bool,
    next_token: str | None,
    new_root_count: int,
    thread_root_count: int,
    max_thread_roots: int,
) -> bool:
    """Report whether a non-empty /threads page exhausted enumeration safety guards."""
    return (
        discarded_due_to_cap
        or (next_token is not None and new_root_count == 0)
        or (next_token is not None and thread_root_count >= max_thread_roots)
    )


async def enumerate_room_thread_root_ids(
    client: nio.AsyncClient,
    room_id: str,
    *,
    max_thread_roots: int = _MAX_ENUMERATED_THREAD_ROOTS,
    page_size: int = 100,
) -> tuple[list[str], bool]:
    """Return unique room thread-root IDs in /threads order."""
    thread_root_ids: list[str] = []
    truncated = max_thread_roots <= 0
    if truncated:
        return thread_root_ids, truncated

    seen_thread_root_ids: set[str] = set()
    seen_next_tokens: set[str] = set()
    page_token: str | None = None
    pages_fetched = 0

    while not truncated:
        thread_roots, next_token = await get_room_threads_page(
            client,
            room_id,
            limit=page_size,
            page_token=page_token,
        )
        pages_fetched += 1
        if thread_roots:
            new_root_count, discarded_due_to_cap = _append_unique_thread_root_ids(
                thread_roots,
                thread_root_ids,
                seen_thread_root_ids,
                max_thread_roots=max_thread_roots,
            )
            if _non_empty_thread_root_page_truncated(
                discarded_due_to_cap=discarded_due_to_cap,
                next_token=next_token,
                new_root_count=new_root_count,
                thread_root_count=len(thread_root_ids),
                max_thread_roots=max_thread_roots,
            ):
                truncated = True
                break
        elif next_token is not None:
            logger.warning(
                "Room thread enumeration stopped on empty page with pagination token",
                room_id=room_id,
                page_count=pages_fetched,
                thread_root_count=len(thread_root_ids),
            )
            truncated = True
            break
        if next_token is None:
            break
        if next_token in seen_next_tokens:
            logger.warning(
                "Room thread enumeration stopped on repeated pagination token",
                room_id=room_id,
                page_count=pages_fetched,
                thread_root_count=len(thread_root_ids),
            )
            truncated = True
            break
        if pages_fetched >= _MAX_THREAD_ENUMERATION_PAGES:
            logger.warning(
                "Room thread enumeration stopped at page cap",
                room_id=room_id,
                page_count=pages_fetched,
                thread_root_count=len(thread_root_ids),
                max_pages=_MAX_THREAD_ENUMERATION_PAGES,
            )
            truncated = True
            break

        seen_next_tokens.add(next_token)
        page_token = next_token

    return thread_root_ids, truncated


__all__ = [
    "BulkThreadRefreshStats",
    "OpaqueEncryptedThreadHistoryError",
    "RoomThreadsPageError",
    "ThreadRoomScanRootNotFoundError",
    "bulk_refresh_room_thread_histories",
    "enumerate_room_thread_root_ids",
    "fetch_dispatch_thread_history",
    "fetch_dispatch_thread_snapshot",
    "fetch_thread_event_sources_via_room_messages",
    "fetch_thread_history",
    "get_room_threads_page",
    "log_thread_history_refresh",
    "refresh_thread_history_from_source",
    "thread_ids_needing_refill",
]
