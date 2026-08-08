"""Reading room history straight from the homeserver.

Everything here answers a question by paginating `/messages` or `/threads`
and reading what comes back. Nothing consults a local store, which is what
makes these the readers that outlive any particular cache: a caller that
needs the homeserver's own answer -- because it is checking whether a local
answer is still true, or because there is no local answer yet -- has to come
here.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import nio
from aiohttp import ClientError
from nio.responses import RoomThreadsResponse

from mindroom.logging_config import get_logger
from mindroom.matrix.client_visible_messages import (
    ResolvedVisibleMessage,
    ThreadEditCandidates,
    apply_latest_edits_to_messages,
    bundled_replacement_candidates,
    is_visible_room_message,
    room_message_fallback_body,
)
from mindroom.matrix.event_info import EventInfo, is_thread_affecting_relation
from mindroom.matrix.event_normalization import (
    is_opaque_encrypted_event_source,
    normalize_nio_event_for_cache,
)
from mindroom.matrix.media import (
    is_encrypted_media_event_source,
    parse_matrix_media_event_source,
)
from mindroom.matrix.message_content import (
    VisibleRoomMessage,
    extract_and_resolve_message,
    resolve_event_source_content,
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
    from collections.abc import Callable, Collection, Iterable

logger = get_logger(__name__)

_ROOM_HISTORY_MESSAGE_TYPES = ("m.room.message", "m.room.encrypted")
_APPROVAL_CARD_EVENT_TYPE = "io.mindroom.tool_approval"
# An approval card is `io.mindroom.tool_approval` in a plain room and arrives
# wrapped as `m.room.encrypted` in an encrypted one, where nio decrypts the
# chunk in place and the plaintext type reappears on the event source.
_APPROVAL_CARD_HISTORY_TYPES = (_APPROVAL_CARD_EVENT_TYPE, "m.room.encrypted")
_MAX_APPROVAL_CARD_SCAN_PAGES = 10
_MAX_ENUMERATED_THREAD_ROOTS = 2000
_MAX_THREAD_ENUMERATION_PAGES = 100


class OpaqueEncryptedThreadHistoryError(RuntimeError):
    """Raised when a thread reconstruction depends on still-undecryptable encrypted events."""


class UnresolvedOpaqueRoomHistoryError(OpaqueEncryptedThreadHistoryError):
    """Raised when opaque room history cannot be assigned to a specific thread."""


@dataclass(slots=True)
class _ThreadEventSourceScanResult:
    """Raw event sources plus scan metadata for one room-history thread fetch."""

    event_sources: list[dict[str, Any]]
    page_count: int
    scanned_event_count: int
    homeserver_scan_parse_cpu_ms: float = 0.0


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


def is_room_message_event(event: nio.Event) -> bool:
    """Return whether one nio event is a readable Matrix room message."""
    event_source = event.source if isinstance(event.source, dict) else {}
    return event_source.get("type") == "m.room.message"


def _event_source_for_cache(event: nio.Event) -> dict[str, Any]:
    """Normalize one nio event source for persistent cache storage."""
    return normalize_nio_event_for_cache(event)


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
    if not is_room_message_event(event):
        return None

    event_info = EventInfo.from_event(event.source)
    if is_visible_room_message(event) and edit_candidates.record(
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
    scan_result = await bulk_scan_thread_event_sources(client, room_id, thread_root_ids=(thread_id,))
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
        raise UnresolvedOpaqueRoomHistoryError(msg)
    return _ThreadEventSourceScanResult(
        event_sources=scan_result.thread_event_sources[thread_id],
        page_count=scan_result.page_count,
        scanned_event_count=scan_result.scanned_event_count,
        homeserver_scan_parse_cpu_ms=scan_result.homeserver_scan_parse_cpu_ms,
    )


async def find_response_event_ids_via_room_messages(
    client: nio.AsyncClient,
    room_id: str,
    *,
    response_sender: str,
    source_event_ids: Collection[str],
    response_source_filter: Callable[[Mapping[str, Any]], bool] | None = None,
) -> frozenset[str]:
    """Find original responses to exact source events in recent room history."""
    sources = set(source_event_ids)
    remaining_sources = set(sources)
    response_event_ids: set[str] = set()
    from_token: str | None = None
    seen_pagination_tokens: set[str] = set()

    while remaining_sources:
        response = await client.room_messages(
            room_id,
            start=from_token,
            limit=100,
            message_filter={"types": list(_ROOM_HISTORY_MESSAGE_TYPES)},
            direction=nio.MessageDirection.back,
        )
        if not isinstance(response, nio.RoomMessagesResponse):
            msg = f"response recovery room scan failed for {room_id}: {response}"
            raise RuntimeError(msg)  # noqa: TRY004
        if not response.chunk:
            break
        for event in response.chunk:
            if not isinstance(event, nio.Event):
                continue
            remaining_sources.discard(event.event_id)
            event_source = event.source if isinstance(event.source, dict) else {}
            event_info = EventInfo.from_event(event_source)
            if (
                event_source.get("sender") == response_sender
                and not event_info.is_edit
                and not event_info.is_thread_fallback
                and event_info.reply_to_event_id in sources
                and (response_source_filter is None or response_source_filter(event_source))
            ):
                response_event_ids.add(event.event_id)
        if not response.end:
            break
        if response.end in seen_pagination_tokens:
            msg = f"response recovery room scan repeated pagination token for {room_id}"
            raise RuntimeError(msg)
        seen_pagination_tokens.add(response.end)
        from_token = response.end

    return frozenset(response_event_ids)


def _is_approval_card_for(event_source: Mapping[str, Any], *, card_sender: str, approval_id: str) -> bool:
    """Return whether one scanned event is the original card for this approval."""
    if event_source.get("type") != _APPROVAL_CARD_EVENT_TYPE or event_source.get("sender") != card_sender:
        return False
    if EventInfo.from_event(dict(event_source)).is_edit:
        # A terminal edit carries the same approval id as the card it replaces.
        # Adopting the edit would bind every later write to an event the room
        # renders as part of another, so only the original counts.
        return False
    content = event_source.get("content")
    return isinstance(content, Mapping) and content.get("approval_id") == approval_id


async def find_approval_card_event_id_via_room_messages(
    client: nio.AsyncClient,
    room_id: str,
    *,
    card_sender: str,
    approval_id: str,
) -> str | None:
    """Find the Matrix event one approval card became, in recent room history.

    Asked when a card's row survived a crash unacknowledged and the frozen
    transaction ID has stopped being proof, which is a re-login between the
    send and the recovery. Presenting the transaction again from the new device
    would put a second prompt in the room rather than converge on the first, so
    the room itself is the only witness left.

    Located by ``approval_id`` rather than by the transaction: the transaction
    is device-scoped, which is the whole reason this lookup is being made. The
    approval id is a per-request ``uuid4`` frozen into the card body before it
    was sent, so at most one original card in the room carries it.

    Bounded, and the bound is a real limit rather than a guess dressed up as
    one. A card reachable from here was sent by a process that died moments
    afterwards, so it sits near the tip of the room; running out of pages
    before the end of history therefore means the answer was not established,
    and that is raised rather than reported as absence. Returning None only
    ever means the walk saw all the history there was and the card was not in
    it.
    """
    from_token: str | None = None
    seen_pagination_tokens: set[str] = set()

    for _page in range(_MAX_APPROVAL_CARD_SCAN_PAGES):
        response = await client.room_messages(
            room_id,
            start=from_token,
            limit=100,
            message_filter={"types": list(_APPROVAL_CARD_HISTORY_TYPES)},
            direction=nio.MessageDirection.back,
        )
        if not isinstance(response, nio.RoomMessagesResponse):
            msg = f"approval card room scan failed for {room_id}: {response}"
            raise RuntimeError(msg)  # noqa: TRY004
        for event in response.chunk:
            if not isinstance(event, nio.Event):
                continue
            event_source = event.source if isinstance(event.source, dict) else {}
            if _is_approval_card_for(event_source, card_sender=card_sender, approval_id=approval_id):
                return event.event_id
        if not response.chunk or not response.end:
            return None
        if response.end in seen_pagination_tokens:
            msg = f"approval card room scan repeated pagination token for {room_id}"
            raise RuntimeError(msg)
        seen_pagination_tokens.add(response.end)
        from_token = response.end

    # Raised rather than answered, and the same way every other failure here
    # is: the caller separates "the room says no" from "the room did not say",
    # and only the first retires anything.
    msg = (
        f"approval card room scan for {approval_id!r} in {room_id} reached its "
        f"{_MAX_APPROVAL_CARD_SCAN_PAGES}-page bound with history left, so the card's absence is unproven"
    )
    raise RuntimeError(msg)


@dataclass(frozen=True)
class _BulkThreadScanResult:
    """Per-thread event sources recovered by one backward room scan."""

    thread_event_sources: dict[str, list[dict[str, Any]]]
    missing_root_ids: frozenset[str]
    unresolved_opaque_event_ids: frozenset[str]
    page_count: int
    scanned_event_count: int
    homeserver_scan_parse_cpu_ms: float = 0.0


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
        edit_event = edit_candidates.winner_for(
            original_event_id,
            sender=_scanned_event_sender(scanned_message_sources.get(original_event_id)),
        )
        if edit_event is None:
            continue
        # A replacement lands in the thread its original was resolved into, and nowhere else.
        # The thread its own ``m.new_content`` names is not consulted: Matrix ignores every
        # relation written there, so it is a claim, and a claim about an event this scan never
        # read is the one input an outsider fully controls.
        target_roots = {
            root_id for root_id in (original_event_id, resolved_thread_ids.get(original_event_id)) if root_id in grouped
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


async def bulk_scan_thread_event_sources(
    client: nio.AsyncClient,
    room_id: str,
    *,
    thread_root_ids: Collection[str],
) -> _BulkThreadScanResult:
    """Walk room history backward once and recover every requested thread's event sources."""
    edit_candidates = ThreadEditCandidates()
    scanned_message_sources: dict[str, dict[str, Any]] = {}
    remaining_root_ids = set(thread_root_ids)
    from_token: str | None = None
    page_count = 0
    scanned_event_count = 0
    homeserver_scan_parse_cpu_ms = 0.0

    while remaining_root_ids:
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
        homeserver_scan_parse_cpu_ms=homeserver_scan_parse_cpu_ms,
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


def parse_room_message_event(event_source: dict[str, Any]) -> nio.Event | None:
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
    return event if is_room_message_event(event) else None


def bundled_replacement_source(event_source: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return one bundled replacement event source when Matrix already included it.

    Candidate order is not this module's to choose. It used to be, and it chose
    differently from the preview reader -- ``event`` before ``latest_event``,
    and only under ``unsigned`` -- so one source carrying both keys produced one
    body in a thread preview and another in the history rebuilt beside it.

    What stays here is the part that is this reader's own: a candidate is usable
    only if it parses as a visible room message, which is stricter than a
    preview needs and is why the two cannot simply share a single function.
    """
    for candidate in bundled_replacement_candidates(event_source):
        if _parse_visible_room_message_event(candidate) is not None:
            return candidate
    return None


async def fetch_thread_messages_from_source(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    *,
    trusted_sender_ids: Collection[str] = (),
) -> list[ResolvedVisibleMessage]:
    """Return one thread's visible messages as the homeserver reports them right now.

    This is the read for a caller that must observe a write another runtime
    just made. The projection is strict about staleness but still answers from
    local state, so it cannot see an event that has not reached this process
    yet -- and "has it reached anyone else" is the only question worth paying a
    homeserver round trip for.

    No local store is consulted or written, deliberately. Sidecar bodies are
    fetched from their media URL rather than from a text cache: the caller is
    already paying for a room scan, and a cache read here would reintroduce the
    staleness the scan exists to avoid.
    """
    scan_result = await fetch_thread_event_sources_via_room_messages(client, room_id, thread_id)
    parsed_events = [
        parsed
        for event_source in scan_result.event_sources
        if (parsed := parse_room_message_event(event_source)) is not None
    ]
    messages_by_event_id: dict[str, ResolvedVisibleMessage] = {}
    edit_candidates = ThreadEditCandidates()
    for event in parsed_events:
        event_info = EventInfo.from_event(event.source)
        replacement_source = bundled_replacement_source(event.source)
        if replacement_source is not None:
            bundled_replacement = parse_room_message_event(replacement_source)
            if is_visible_room_message(bundled_replacement):
                edit_candidates.record(
                    bundled_replacement,
                    event_info=EventInfo.from_event(bundled_replacement.source),
                )
        if is_visible_room_message(event) and edit_candidates.record(
            event,
            event_info=event_info,
        ):
            continue
        if event_info.is_edit or event.event_id in messages_by_event_id:
            continue
        messages_by_event_id[event.event_id] = await _resolve_message_from_source(
            event,
            client,
            trusted_sender_ids=trusted_sender_ids,
        )
    await apply_latest_edits_to_messages(
        client,
        messages_by_event_id=messages_by_event_id,
        edit_candidates=edit_candidates,
        synthesize_unseen_originals=False,
        trusted_sender_ids=trusted_sender_ids,
    )
    messages = list(messages_by_event_id.values())
    sort_thread_messages_root_first(messages, thread_id=thread_id)
    return messages


async def _resolve_message_from_source(
    event: nio.Event,
    client: nio.AsyncClient,
    *,
    trusted_sender_ids: Collection[str],
) -> ResolvedVisibleMessage:
    """Resolve one scanned event into the normalized thread-history shape."""
    if is_visible_room_message(event):
        message_data = await extract_and_resolve_message(event, client, trusted_sender_ids=trusted_sender_ids)
        return ResolvedVisibleMessage.from_message_data(
            message_data,
            thread_id=EventInfo.from_event(event.source).thread_id,
            latest_event_id=event.event_id,
        )

    resolved_event_source = await resolve_event_source_content(
        event.source if isinstance(event.source, dict) else {},
        client,
    )
    content = resolved_event_source.get("content", {})
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
        content=content if isinstance(content, dict) else {},
        thread_id=event_info.thread_id,
    )
    message.refresh_stream_status()
    return message


def _parse_visible_room_message_event(
    event_source: dict[str, Any],
) -> VisibleRoomMessage | None:
    """Parse one event dict into a visible room message when possible."""
    parsed_event = parse_room_message_event(event_source)
    return parsed_event if is_visible_room_message(parsed_event) else None
