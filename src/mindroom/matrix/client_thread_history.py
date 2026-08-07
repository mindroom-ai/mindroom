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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.cache import (
    thread_cache_rejection_reason,
)
from mindroom.matrix.cache.thread_cache_gap import (
    mark_room_threads_gap_fail_closed,
    mark_thread_gap_fail_closed,
)
from mindroom.matrix.client_visible_messages import (
    VISIBLE_ROOM_MESSAGE_EVENT_TYPES,
    ResolvedVisibleMessage,
    ThreadEditCandidates,
    apply_latest_edits_to_messages,
)
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.event_normalization import is_opaque_encrypted_event_source
from mindroom.matrix.membership_fence import UNCERTIFIED_MEMBERSHIP_EPOCH
from mindroom.matrix.message_content import (
    SidecarHydrationBatch,
    extract_and_resolve_message,
    prepare_sidecar_hydration_batch,
    resolve_event_source_content,
)
from mindroom.matrix.room_history_reads import (
    OpaqueEncryptedThreadHistoryError,
    UnresolvedOpaqueRoomHistoryError,
    bulk_scan_thread_event_sources,
    bundled_replacement_source,
    fetch_thread_event_sources_via_room_messages,
    parse_room_message_event,
    room_message_fallback_body,
)
from mindroom.matrix.thread_diagnostics import (
    THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC,
    THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
    THREAD_HISTORY_ERROR_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_HOMESERVER,
    THREAD_HISTORY_SOURCE_STALE_CACHE,
)
from mindroom.matrix.thread_history_result import ThreadHistoryResult, thread_history_result
from mindroom.matrix.thread_membership import (
    ThreadRoomScanRootNotFoundError,
)
from mindroom.matrix.thread_projection import (
    sort_thread_messages_root_first,
)
from mindroom.matrix.visible_body import visible_body_from_event_source
from mindroom.timing import elapsed_ms_since

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from mindroom.matrix.cache import ConversationEventCache

logger = get_logger(__name__)
_OPAQUE_ENCRYPTED_THREAD_HISTORY_REASON = "thread_history_opaque_encrypted_event"
_OPAQUE_ENCRYPTED_EVENT_REJECTION = "opaque_encrypted_event"
_MISSING_THREAD_ROOT_REJECTION = "missing_thread_root"
type _ThreadHistoryDiagnosticValue = str | int | float | bool | None


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
            room_message_fallback_body(event),
            trusted_sender_ids=trusted_sender_ids,
        ),
        timestamp=event.server_timestamp if isinstance(event.server_timestamp, int) else 0,
        event_id=event.event_id,
        content=normalized_content,
        thread_id=event_info.thread_id,
    )
    message.refresh_stream_status()
    return message


def _event_id_from_source(event_source: Mapping[str, Any]) -> str | None:
    """Return one Matrix event ID from a raw event source when present."""
    event_id = event_source.get("event_id")
    return event_id if isinstance(event_id, str) else None


def _sidecar_hydration_sources(
    event_sources: Sequence[dict[str, Any]],
    *,
    hydrate_sidecars: bool,
) -> list[dict[str, Any]]:
    """Return sources whose sidecars this resolution pass may hydrate."""
    hydration_sources: list[dict[str, Any]] = []
    for event_source in event_sources:
        bundled_replacement = bundled_replacement_source(event_source)
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
        if (parsed_event := parse_room_message_event(event_source)) is not None
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
        replacement_source = bundled_replacement_source(event.source)
        if replacement_source is not None:
            bundled_replacement = nio.Event.parse_event(replacement_source)
            if isinstance(bundled_replacement, VISIBLE_ROOM_MESSAGE_EVENT_TYPES):
                edit_candidates.record(
                    bundled_replacement,
                    event_info=EventInfo.from_event(bundled_replacement.source),
                )
        if isinstance(event, VISIBLE_ROOM_MESSAGE_EVENT_TYPES) and edit_candidates.record(
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
    except UnresolvedOpaqueRoomHistoryError:
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
    if isinstance(event, VISIBLE_ROOM_MESSAGE_EVENT_TYPES):
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
            room_message_fallback_body(event),
            trusted_sender_ids=trusted_sender_ids,
        ),
        timestamp=event.server_timestamp if isinstance(event.server_timestamp, int) else 0,
        event_id=event.event_id,
        content=normalized_content,
        thread_id=event_info.thread_id,
    )
    message.refresh_stream_status()
    return message


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


@dataclass(frozen=True)
class BulkThreadRefreshStats:
    """Summary for one bulk thread-cache refresh pass over a room."""

    requested_threads: int
    usable_threads: int
    missing_root_ids: frozenset[str]
    room_scan_pages: int
    scanned_event_count: int
    scan_truncated: bool = False


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
    scan_result = await bulk_scan_thread_event_sources(
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


__all__ = [
    "BulkThreadRefreshStats",
    "ThreadRoomScanRootNotFoundError",
    "bulk_refresh_room_thread_histories",
    "log_thread_history_refresh",
    "refresh_thread_history_from_source",
    "thread_ids_needing_refill",
]
