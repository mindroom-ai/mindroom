"""Delivery recovery scans and thread enumeration straight from the homeserver.

Everything here answers a question by paginating `/messages` or `/threads`
and reading what comes back, without consulting a local store: whether a
delivery already reached a room, and which threads a room has. What a thread
contains is not answered here; the conversation projection reads that.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import TYPE_CHECKING, Any

import nio
from aiohttp import ClientError
from nio.responses import RoomThreadsResponse

from mindroom.logging_config import get_logger
from mindroom.matrix.event_info import EventInfo

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Mapping, Sequence

logger = get_logger(__name__)

_ROOM_HISTORY_MESSAGE_TYPES = ("m.room.message", "m.room.encrypted")
_MAX_EXACT_DELIVERY_SCAN_PAGES = 10
_MAX_ENUMERATED_THREAD_ROOTS = 2000
_MAX_THREAD_ENUMERATION_PAGES = 100


class _UnresolvedOpaqueRoomHistoryError(RuntimeError):
    """Raised when undecryptable events from a delivery's sender could hide the exact delivery a scan must prove."""


def _finish_exact_delivery_scan_at_bound(
    *,
    room_id: str,
    pages_fetched: int,
    delivery_found: bool,
) -> bool:
    """Finish with a bounded match, or raise when its absence remains unproven."""
    if pages_fetched < _MAX_EXACT_DELIVERY_SCAN_PAGES:
        return False
    if delivery_found:
        return True
    msg = (
        f"exact delivery room scan in {room_id} reached its "
        f"{_MAX_EXACT_DELIVERY_SCAN_PAGES}-page bound with history left, so the delivery's absence is unproven"
    )
    raise RuntimeError(msg)


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


async def find_response_event_ids_via_room_messages(
    client: nio.AsyncClient,
    room_id: str,
    *,
    response_sender: str,
    source_event_ids: Collection[str],
    response_source_filter: Callable[[Mapping[str, Any]], bool] | None = None,
    response_content: Mapping[str, Any] | None = None,
    event_types: Collection[str] = _ROOM_HISTORY_MESSAGE_TYPES,
) -> frozenset[str]:
    """Find responses by source relation or exact frozen content."""
    sources = set(source_event_ids)
    remaining_sources = set(sources)
    response_event_ids: set[str] = set()
    from_token: str | None = None
    seen_pagination_tokens: set[str] = set()
    pages_fetched = 0

    while remaining_sources or (not sources and response_content is not None and not response_event_ids):
        response = await client.room_messages(
            room_id,
            start=from_token,
            limit=100,
            message_filter={"types": list(event_types)},
            direction=nio.MessageDirection.back,
        )
        if not isinstance(response, nio.RoomMessagesResponse):
            msg = f"response recovery room scan failed for {room_id}: {response}"
            raise RuntimeError(msg)  # noqa: TRY004
        pages_fetched += 1
        for event in response.chunk:
            if not isinstance(event, nio.Event):
                continue
            remaining_sources.discard(event.event_id)
            event_source = event.source if isinstance(event.source, dict) else {}
            _refuse_opaque_exact_delivery_candidate(
                room_id=room_id,
                event_source=event_source,
                response_sender=response_sender,
                exact_content_required=response_content is not None,
            )
            event_info = EventInfo.from_event(event_source)
            exact_content = response_content is not None and event_source.get("content") == response_content
            replies_to_source = (
                not event_info.is_edit and not event_info.is_thread_fallback and event_info.reply_to_event_id in sources
            )
            if (
                event_source.get("sender") == response_sender
                and (exact_content if response_content is not None else replies_to_source)
                and (response_source_filter is None or response_source_filter(event_source))
            ):
                response_event_ids.add(event.event_id)
        # A standalone frozen delivery has no source event to bound the walk,
        # so its exact content ends the scan as soon as it is found.
        if (not sources and response_event_ids) or (sources and not remaining_sources):
            break
        if not response.end:
            break
        if response.end in seen_pagination_tokens:
            msg = f"response recovery room scan repeated pagination token for {room_id}"
            raise RuntimeError(msg)
        if response_content is not None and _finish_exact_delivery_scan_at_bound(
            room_id=room_id,
            pages_fetched=pages_fetched,
            delivery_found=bool(response_event_ids),
        ):
            break
        seen_pagination_tokens.add(response.end)
        from_token = response.end

    return frozenset(response_event_ids)


def _refuse_opaque_exact_delivery_candidate(
    *,
    room_id: str,
    event_source: Mapping[str, Any],
    response_sender: str,
    exact_content_required: bool,
) -> None:
    """Fail closed when ciphertext may hide the exact delivery marker."""
    if not exact_content_required or event_source.get("sender") != response_sender:
        return
    if event_source.get("type") == "m.room.encrypted":
        msg = f"exact delivery room scan for {room_id} contains undecryptable events from its sender"
        raise _UnresolvedOpaqueRoomHistoryError(msg)


async def find_outbox_delivery_event_id_via_room_messages(
    client: nio.AsyncClient,
    room_id: str,
    *,
    delivery_sender: str,
    source_event_ids: Collection[str],
    delivery_content: Mapping[str, Any],
    delivery_event_type: str,
) -> str | None:
    """Find the one event carrying a frozen outbox payload."""
    delivered = await find_response_event_ids_via_room_messages(
        client,
        room_id,
        response_sender=delivery_sender,
        source_event_ids=source_event_ids,
        response_source_filter=lambda source: source.get("type") == delivery_event_type,
        response_content=delivery_content,
        event_types=(delivery_event_type, "m.room.encrypted"),
    )
    if len(delivered) > 1:
        msg = f"Matrix delivery has {len(delivered)} exact copies in {room_id}"
        raise RuntimeError(msg)
    return next(iter(delivered), None)


def _canonical_content_key(content: Mapping[str, Any]) -> str:
    """Return the canonical form exact-content matching groups payloads by."""
    return json.dumps(dict(content), sort_keys=True, separators=(",", ":"))


def _matched_delivery_copy_key(
    event: object,
    *,
    room_id: str,
    delivery_sender: str,
    delivery_event_type: str,
    expected: Mapping[str, int],
) -> str | None:
    """Return the expected key one scanned event is an exact copy of, if any."""
    if not isinstance(event, nio.Event):
        return None
    event_source = event.source if isinstance(event.source, dict) else {}
    _refuse_opaque_exact_delivery_candidate(
        room_id=room_id,
        event_source=event_source,
        response_sender=delivery_sender,
        exact_content_required=True,
    )
    if event_source.get("sender") != delivery_sender or event_source.get("type") != delivery_event_type:
        return None
    content = event_source.get("content")
    if not isinstance(content, dict):
        return None
    key = _canonical_content_key(content)
    return key if key in expected else None


async def _count_delivery_copies_via_room_messages(
    client: nio.AsyncClient,
    room_id: str,
    *,
    delivery_sender: str,
    expected: Mapping[str, int],
    delivery_event_type: str,
) -> Counter[str]:
    """Count exact-content copies per canonical key, stopping when all are found."""
    found: Counter[str] = Counter()
    from_token: str | None = None
    seen_pagination_tokens: set[str] = set()
    pages_fetched = 0

    def all_found() -> bool:
        return all(found[key] >= count for key, count in expected.items())

    while not all_found():
        response = await client.room_messages(
            room_id,
            start=from_token,
            limit=100,
            message_filter={"types": [delivery_event_type, "m.room.encrypted"]},
            direction=nio.MessageDirection.back,
        )
        if not isinstance(response, nio.RoomMessagesResponse):
            msg = f"delivery copy count room scan failed for {room_id}: {response}"
            raise RuntimeError(msg)  # noqa: TRY004
        pages_fetched += 1
        for event in response.chunk:
            key = _matched_delivery_copy_key(
                event,
                room_id=room_id,
                delivery_sender=delivery_sender,
                delivery_event_type=delivery_event_type,
                expected=expected,
            )
            if key is not None:
                found[key] += 1
        if all_found() or not response.end:
            break
        if response.end in seen_pagination_tokens:
            msg = f"delivery copy count room scan repeated pagination token for {room_id}"
            raise RuntimeError(msg)
        # A copy is still missing, so the bound can only raise.
        _finish_exact_delivery_scan_at_bound(room_id=room_id, pages_fetched=pages_fetched, delivery_found=False)
        seen_pagination_tokens.add(response.end)
        from_token = response.end

    return found


async def missing_outbox_delivery_copy_indices_via_room_messages(
    client: nio.AsyncClient,
    room_id: str,
    *,
    delivery_sender: str,
    delivery_contents: Sequence[Mapping[str, Any]],
    delivery_event_type: str,
) -> list[int]:
    """Return which expected payloads the room does not hold enough copies of.

    One frozen payload can legitimately appear several times -- segmented
    responses repeat byte-identical continuation events -- so reconciliation
    needs multiplicities, not existence. Each room copy satisfies exactly one
    expected position, consumed in order; the result lists the indices left
    unsatisfied. The scan stops as soon as every position is accounted for,
    and fails closed at the page bound while an absence stays unproven.
    """
    keys = [_canonical_content_key(content) for content in delivery_contents]
    found = await _count_delivery_copies_via_room_messages(
        client,
        room_id,
        delivery_sender=delivery_sender,
        expected=Counter(keys),
        delivery_event_type=delivery_event_type,
    )
    missing: list[int] = []
    for index, key in enumerate(keys):
        if found[key] > 0:
            found[key] -= 1
        else:
            missing.append(index)
    return missing


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


async def enumerate_room_thread_root_ids(
    client: nio.AsyncClient,
    room_id: str,
    *,
    max_thread_roots: int = _MAX_ENUMERATED_THREAD_ROOTS,
    page_size: int = 100,
) -> tuple[list[str], bool]:
    """Return unique room thread-root IDs in /threads order."""
    if max_thread_roots <= 0:
        return [], True

    thread_root_ids: list[str] = []
    seen_thread_root_ids: set[str] = set()
    seen_next_tokens: set[str] = set()
    page_token: str | None = None
    pages_fetched = 0

    while True:
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
            if discarded_due_to_cap or (
                next_token is not None and (new_root_count == 0 or len(thread_root_ids) >= max_thread_roots)
            ):
                return thread_root_ids, True
        elif next_token is not None:
            logger.warning(
                "Room thread enumeration stopped on empty page with pagination token",
                room_id=room_id,
                page_count=pages_fetched,
                thread_root_count=len(thread_root_ids),
            )
            return thread_root_ids, True
        if next_token is None:
            return thread_root_ids, False
        if next_token in seen_next_tokens:
            logger.warning(
                "Room thread enumeration stopped on repeated pagination token",
                room_id=room_id,
                page_count=pages_fetched,
                thread_root_count=len(thread_root_ids),
            )
            return thread_root_ids, True
        if pages_fetched >= _MAX_THREAD_ENUMERATION_PAGES:
            logger.warning(
                "Room thread enumeration stopped at page cap",
                room_id=room_id,
                page_count=pages_fetched,
                thread_root_count=len(thread_root_ids),
                max_pages=_MAX_THREAD_ENUMERATION_PAGES,
            )
            return thread_root_ids, True

        seen_next_tokens.add(next_token)
        page_token = next_token
