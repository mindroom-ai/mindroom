"""Reply-chain resolution for non-thread Matrix clients.

Walks ``m.in_reply_to`` links back to the conversation root so that
plain replies from clients without thread support are mapped to the
correct thread.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.cache.thread_history_result import ThreadHistoryResult
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.message_content import resolve_event_source_content, visible_body_from_event_source

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    import structlog

    from mindroom.matrix.client import ResolvedVisibleMessage
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol

type _HistoryMessage = ResolvedVisibleMessage


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class _LRUCache[T]:
    """Bounded LRU cache keyed by (room_id, event_id)."""

    __slots__ = ("_data", "maxsize")

    def __init__(self, maxsize: int) -> None:
        self._data: OrderedDict[tuple[str, str], T] = OrderedDict()
        self.maxsize = maxsize

    def get(self, room_id: str, event_id: str) -> T | None:
        key = (room_id, event_id)
        value = self._data.get(key)
        if value is not None:
            self._data.move_to_end(key)
        return value

    def put(self, room_id: str, event_id: str, value: T) -> None:
        key = (room_id, event_id)
        self._data[key] = value
        self._data.move_to_end(key)
        if len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)

    def clear(self) -> None:
        self._data.clear()


@dataclass
class _ReplyChainNode:
    """Cached reply-chain node metadata for context derivation."""

    message: ResolvedVisibleMessage
    parent_event_id: str | None
    thread_root_id: str | None
    has_relations: bool
    event_source: dict[str, Any] | None = None
    content_hydrated: bool = True


@dataclass
class _ReplyChainRoot:
    """Canonical root metadata for a reply-chain event."""

    root_event_id: str
    points_to_thread: bool


@dataclass
class ReplyChainCaches:
    """Per-bot caches for reply-chain traversal."""

    nodes: _LRUCache[_ReplyChainNode] = field(default_factory=lambda: _LRUCache(4096))
    roots: _LRUCache[_ReplyChainRoot] = field(default_factory=lambda: _LRUCache(4096))
    traversal_limit: int = 500

    def _invalidate_nodes(self, room_id: str, pending_event_ids: set[str]) -> bool:
        added_event_ids = False
        for (cached_room_id, event_id), node in list(self.nodes._data.items()):
            if cached_room_id != room_id:
                continue
            if (
                event_id not in pending_event_ids
                and node.parent_event_id not in pending_event_ids
                and node.thread_root_id not in pending_event_ids
            ):
                continue
            self.nodes._data.pop((cached_room_id, event_id), None)
            if event_id in pending_event_ids:
                continue
            pending_event_ids.add(event_id)
            added_event_ids = True
        return added_event_ids

    def _invalidate_roots(self, room_id: str, pending_event_ids: set[str]) -> bool:
        added_event_ids = False
        for (cached_room_id, event_id), root in list(self.roots._data.items()):
            if cached_room_id != room_id:
                continue
            if event_id not in pending_event_ids and root.root_event_id not in pending_event_ids:
                continue
            self.roots._data.pop((cached_room_id, event_id), None)
            if event_id in pending_event_ids:
                continue
            pending_event_ids.add(event_id)
            added_event_ids = True
        return added_event_ids

    def invalidate(self, room_id: str, event_ids: Iterable[str | None]) -> None:
        """Drop cached reply-chain entries affected by one or more event changes."""
        pending_event_ids = {event_id for event_id in event_ids if isinstance(event_id, str) and event_id}
        if not pending_event_ids:
            return
        while self._invalidate_nodes(room_id, pending_event_ids) or self._invalidate_roots(room_id, pending_event_ids):
            pass

    def event_source(self, room_id: str, event_id: str) -> dict[str, Any] | None:
        """Return cached event source for one node when it is currently materialized."""
        node = self.nodes.get(room_id, event_id)
        if node is None or not isinstance(node.event_source, dict):
            return None
        return node.event_source

    def clear(self) -> None:
        """Drop all process-local reply-chain caches for one runtime lifetime."""
        self.nodes.clear()
        self.roots.clear()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


async def _history_message_from_event_source(
    event_source: dict[str, Any],
    *,
    sender: str,
    fallback_body: str,
    timestamp: int,
    event_id: str,
    client: nio.AsyncClient,
    hydrate_sidecars: bool,
) -> ResolvedVisibleMessage:
    """Build one normalized history message from raw event source content."""
    from mindroom.matrix.client import ResolvedVisibleMessage  # noqa: PLC0415

    resolved_source = await resolve_event_source_content(event_source, client) if hydrate_sidecars else event_source
    content = resolved_source.get("content")
    content_dict = content if isinstance(content, dict) else {}
    event_info = EventInfo.from_event(resolved_source)
    visible_fallback_body = fallback_body
    raw_body = content_dict.get("body")
    if isinstance(raw_body, str):
        visible_fallback_body = raw_body
    return ResolvedVisibleMessage.synthetic(
        sender=sender,
        body=visible_body_from_event_source(resolved_source, visible_fallback_body),
        timestamp=timestamp,
        event_id=event_id,
        content=content_dict,
        thread_id=event_info.thread_id,
    )


async def _event_to_history_message(
    event: nio.Event,
    client: nio.AsyncClient,
    *,
    hydrate_sidecars: bool,
) -> ResolvedVisibleMessage:
    """Convert a Matrix event to normalized history message structure."""
    event_source = event.source if isinstance(event.source, dict) else {}
    fallback_body = event.body if isinstance(event, (nio.RoomMessageText, nio.RoomMessageNotice)) else ""
    return await _history_message_from_event_source(
        event_source,
        sender=event.sender,
        fallback_body=fallback_body,
        timestamp=event.server_timestamp if isinstance(event.server_timestamp, int) else 0,
        event_id=event.event_id,
        client=client,
        hydrate_sidecars=hydrate_sidecars,
    )


async def _hydrate_cached_node_message(
    node: _ReplyChainNode,
    client: nio.AsyncClient,
) -> None:
    """Upgrade one cached preview node to hydrated sidecar content in place."""
    if node.content_hydrated or node.event_source is None:
        return
    node.message = await _history_message_from_event_source(
        node.event_source,
        sender=node.message.sender,
        fallback_body=node.message.body,
        timestamp=node.message.timestamp,
        event_id=node.message.event_id,
        client=client,
        hydrate_sidecars=True,
    )
    node.content_hydrated = True


def _history_message_event_id(message: _HistoryMessage) -> str | None:
    """Return the event ID for one history message."""
    return message.event_id


def _next_reply_chain_event_id(event_info: EventInfo, current_event_id: str) -> str | None:
    """Resolve the next event in a reply chain."""
    return event_info.next_related_event_id(current_event_id)


def _thread_history_has_replies(thread_history: Sequence[_HistoryMessage], root_event_id: str) -> bool:
    """Return whether a root event already has thread replies."""
    return any(_history_message_event_id(msg) != root_event_id for msg in thread_history)


def _thread_history_is_full(thread_history: Sequence[_HistoryMessage], *, default: bool) -> bool:
    """Return whether *thread_history* is already fully hydrated."""
    if isinstance(thread_history, ThreadHistoryResult):
        return thread_history.is_full_history
    return default


def _unique_history_event_ids(messages: Sequence[_HistoryMessage]) -> list[str]:
    """Return unique string event IDs while preserving input order."""
    ids: list[str] = []
    seen: set[str] = set()
    for message in messages:
        event_id = _history_message_event_id(message)
        if not isinstance(event_id, str) or event_id in seen:
            continue
        seen.add(event_id)
        ids.append(event_id)
    return ids


def _history_messages_by_event_id(messages: Sequence[_HistoryMessage]) -> dict[str, _HistoryMessage]:
    """Index history messages by event ID."""
    messages_by_event_id: dict[str, _HistoryMessage] = {}
    for message in messages:
        event_id = _history_message_event_id(message)
        if isinstance(event_id, str):
            messages_by_event_id[event_id] = message
    return messages_by_event_id


def _shortest_common_supersequence_ids(thread_ids: list[str], chain_ids: list[str]) -> list[str]:
    """Build a shortest common supersequence over event IDs.

    A simple seen-set merge would lose relative ordering when the two
    sequences interleave (e.g. thread: [A, B, C] and chain: [A, X, B, Y, C]).
    SCS preserves both orderings and inserts chain-only events at the correct
    chronological positions.  Bounded by the 500-event traversal limit.
    """
    m = len(thread_ids)
    n = len(chain_ids)

    lcs: list[list[int]] = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m - 1, -1, -1):
        for j in range(n - 1, -1, -1):
            if thread_ids[i] == chain_ids[j]:
                lcs[i][j] = 1 + lcs[i + 1][j + 1]
            else:
                lcs[i][j] = max(lcs[i + 1][j], lcs[i][j + 1])

    merged_ids: list[str] = []
    i = 0
    j = 0
    while i < m and j < n:
        if thread_ids[i] == chain_ids[j]:
            merged_ids.append(thread_ids[i])
            i += 1
            j += 1
        elif lcs[i + 1][j] > lcs[i][j + 1]:
            merged_ids.append(thread_ids[i])
            i += 1
        else:
            merged_ids.append(chain_ids[j])
            j += 1

    merged_ids.extend(thread_ids[i:])
    merged_ids.extend(chain_ids[j:])
    return merged_ids


def _merge_thread_and_chain_history(
    thread_history: Sequence[_HistoryMessage],
    chain_history: Sequence[_HistoryMessage],
) -> list[_HistoryMessage]:
    """Merge thread history with plain-reply chain history without duplicates."""
    thread_ids = _unique_history_event_ids(thread_history)
    chain_ids = _unique_history_event_ids(chain_history)

    if not thread_ids:
        return list(chain_history)
    if not chain_ids:
        return list(thread_history)

    thread_messages = _history_messages_by_event_id(thread_history)
    chain_messages = _history_messages_by_event_id(chain_history)
    merged_ids = _shortest_common_supersequence_ids(thread_ids, chain_ids)
    return [thread_messages.get(event_id) or chain_messages[event_id] for event_id in merged_ids]


def _merged_thread_history(
    thread_history: Sequence[_HistoryMessage],
    chain_history: Sequence[_HistoryMessage],
) -> list[_HistoryMessage]:
    if not chain_history:
        return list(thread_history)
    return _merge_thread_and_chain_history(thread_history, chain_history)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _cache_roots(
    caches: ReplyChainCaches,
    room_id: str,
    event_ids: list[str],
    root_event_id: str,
    *,
    points_to_thread: bool,
) -> None:
    """Path-compress canonical root metadata across visited events."""
    root = _ReplyChainRoot(root_event_id=root_event_id, points_to_thread=points_to_thread)
    for event_id in event_ids:
        caches.roots.put(room_id, event_id, root)


def _first_cached_root(caches: ReplyChainCaches, room_id: str, event_ids: list[str]) -> _ReplyChainRoot | None:
    """Return the first cached root metadata found in a traversal path."""
    for event_id in event_ids:
        cached_root = caches.roots.get(room_id, event_id)
        if cached_root:
            return cached_root
    return None


# ---------------------------------------------------------------------------
# Async resolution logic
# ---------------------------------------------------------------------------


async def _fetch_node(
    client: nio.AsyncClient,
    access: ConversationCacheProtocol,
    caches: ReplyChainCaches,
    logger: structlog.stdlib.BoundLogger,
    room_id: str,
    event_id: str,
    *,
    hydrate_sidecars: bool = True,
) -> _ReplyChainNode | None:
    """Fetch reply-chain node metadata from cache or Matrix."""
    cached_node = caches.nodes.get(room_id, event_id)
    if cached_node:
        if hydrate_sidecars:
            await _hydrate_cached_node_message(cached_node, client)
        return cached_node

    response = await access.get_event(room_id, event_id)
    if not isinstance(response, nio.RoomGetEventResponse):
        logger.debug(
            "Failed to resolve reply event for context",
            room_id=room_id,
            event_id=event_id,
            error=str(response),
        )
        return None

    target_event = response.event
    event_source = target_event.source if isinstance(target_event.source, dict) else {}
    target_info = EventInfo.from_event(target_event.source)
    node = _ReplyChainNode(
        message=await _event_to_history_message(target_event, client, hydrate_sidecars=hydrate_sidecars),
        parent_event_id=_next_reply_chain_event_id(target_info, event_id),
        thread_root_id=target_info.thread_id,
        has_relations=target_info.has_relations,
        event_source={key: value for key, value in event_source.items() if isinstance(key, str)},
        content_hydrated=hydrate_sidecars,
    )
    caches.nodes.put(room_id, event_id, node)
    if node.thread_root_id:
        _cache_roots(caches, room_id, [event_id], node.thread_root_id, points_to_thread=True)
    return node


async def _resolve_direct_thread_root(
    access: ConversationCacheProtocol,
    caches: ReplyChainCaches,
    room_id: str,
    event_id: str,
    node: _ReplyChainNode,
    visited_event_ids: list[str],
    chain_history_length: int,
    *,
    default_history_is_full: bool,
) -> tuple[str, Sequence[_HistoryMessage], bool, bool] | None:
    """Resolve clients that reply to an existing thread root without m.thread metadata."""
    if chain_history_length != 1 or node.parent_event_id or node.thread_root_id or node.has_relations:
        return None

    try:
        thread_history = await access.get_thread_snapshot(room_id, event_id)
    except Exception as exc:
        logger.warning(
            "Failed to probe direct thread root from reply chain; continuing without thread inference",
            room_id=room_id,
            event_id=event_id,
            error=str(exc),
        )
        return None
    if not _thread_history_has_replies(thread_history, event_id):
        return None

    caches.nodes.put(
        room_id,
        event_id,
        replace(node, thread_root_id=event_id),
    )
    _cache_roots(caches, room_id, visited_event_ids, event_id, points_to_thread=True)
    return (
        event_id,
        thread_history,
        True,
        _thread_history_is_full(thread_history, default=default_history_is_full),
    )


async def canonicalize_related_event_id(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
    *,
    access: ConversationCacheProtocol,
    caches: ReplyChainCaches | None = None,
    traversal_limit: int | None = None,
) -> str | None:
    """Resolve one event or relation chain into its canonical conversation root."""
    current_event_id = event_id.strip()
    if not current_event_id:
        return None

    effective_caches = caches or ReplyChainCaches()
    effective_traversal_limit = traversal_limit or effective_caches.traversal_limit
    canonical_event_id: str | None = None
    seen_event_ids: set[str] = set()

    while current_event_id:
        if len(seen_event_ids) >= effective_traversal_limit or current_event_id in seen_event_ids:
            break
        seen_event_ids.add(current_event_id)

        node = await _fetch_node(
            client,
            access,
            effective_caches,
            logger,
            room_id,
            current_event_id,
        )
        if node is None:
            break

        next_event_id = node.thread_root_id or node.parent_event_id
        if next_event_id is None:
            if not node.has_relations:
                canonical_event_id = current_event_id
            break
        current_event_id = next_event_id

    return canonical_event_id


def _build_context_result(
    caches: ReplyChainCaches,
    *,
    room_id: str,
    reply_to_event_id: str,
    chain_history: list[_HistoryMessage],
    visited_event_ids: list[str],
    thread_root_id: str | None,
) -> tuple[str, list[_HistoryMessage], bool, bool]:
    """Build reply-chain context tuple after traversal is complete."""
    cached_root = _first_cached_root(caches, room_id, visited_event_ids)
    if not chain_history:
        if cached_root:
            return cached_root.root_event_id, [], cached_root.points_to_thread, False
        return reply_to_event_id, [], False, False

    # Fetches walk from newest->oldest, but consumers expect chronological history.
    chain_history.reverse()

    if thread_root_id:
        _cache_roots(caches, room_id, visited_event_ids, thread_root_id, points_to_thread=True)
        return thread_root_id, chain_history, True, False

    root_event_id = _history_message_event_id(chain_history[0]) or reply_to_event_id
    _cache_roots(caches, room_id, visited_event_ids, root_event_id, points_to_thread=False)
    return root_event_id, chain_history, False, False


async def _resolve_reply_chain(
    client: nio.AsyncClient,
    access: ConversationCacheProtocol,
    caches: ReplyChainCaches,
    logger: structlog.stdlib.BoundLogger,
    room_id: str,
    reply_to_event_id: str,
    *,
    default_history_is_full: bool,
    hydrate_sidecars: bool,
) -> tuple[str, list[_HistoryMessage], bool, bool]:
    """Resolve reply-chain context for clients that don't send thread relations.

    Returns:
        Tuple of (conversation_root_id, context_history, points_to_thread, is_full_thread_history)

    """
    chain_history: list[_HistoryMessage] = []
    thread_root_id: str | None = None
    current_event_id: str | None = reply_to_event_id
    seen_event_ids: set[str] = set()
    visited_event_ids: list[str] = []
    direct_thread_root_context: tuple[str, Sequence[_HistoryMessage], bool, bool] | None = None

    while current_event_id:
        if len(visited_event_ids) >= caches.traversal_limit:
            logger.warning(
                "Reply-chain traversal limit reached while resolving context",
                room_id=room_id,
                reply_to_event_id=reply_to_event_id,
                traversal_limit=caches.traversal_limit,
                traversed_events=len(visited_event_ids),
                last_event_id=visited_event_ids[-1] if visited_event_ids else None,
            )
            break
        if current_event_id in seen_event_ids:
            logger.debug(
                "Detected reply-chain cycle while resolving context",
                room_id=room_id,
                event_id=current_event_id,
            )
            break
        seen_event_ids.add(current_event_id)
        visited_event_ids.append(current_event_id)

        node = await _fetch_node(
            client,
            access,
            caches,
            logger,
            room_id,
            current_event_id,
            hydrate_sidecars=hydrate_sidecars,
        )
        if node is None:
            break

        chain_history.append(node.message)
        if node.thread_root_id:
            thread_root_id = thread_root_id or node.thread_root_id

        direct_thread_root_context = await _resolve_direct_thread_root(
            access,
            caches,
            room_id=room_id,
            event_id=current_event_id,
            node=node,
            visited_event_ids=visited_event_ids,
            chain_history_length=len(chain_history),
            default_history_is_full=default_history_is_full,
        )
        if direct_thread_root_context is not None:
            break

        current_event_id = node.parent_event_id

    if direct_thread_root_context is not None:
        context_root_id, thread_history, points_to_thread, is_full_thread_history = direct_thread_root_context
        return context_root_id, cast("list[_HistoryMessage]", thread_history), points_to_thread, is_full_thread_history

    return _build_context_result(
        caches,
        room_id=room_id,
        reply_to_event_id=reply_to_event_id,
        chain_history=chain_history,
        visited_event_ids=visited_event_ids,
        thread_root_id=thread_root_id,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _thread_root_id_from_event_info(event_info: EventInfo) -> str | None:
    if event_info.thread_id is not None:
        return event_info.thread_id
    if event_info.is_edit:
        # Edit events use top-level m.replace, but thread relation may still
        # exist in m.new_content for thread edits.
        return event_info.thread_id_from_edit
    return None


def _reply_chain_seed(event_info: EventInfo) -> str | None:
    return event_info.original_event_id if event_info.is_edit else event_info.reply_to_event_id


async def _thread_history_or_fallback(
    *,
    access: ConversationCacheProtocol,
    logger: structlog.stdlib.BoundLogger,
    room_id: str,
    thread_id: str,
    fallback_history: Sequence[_HistoryMessage],
    log_message: str,
) -> list[_HistoryMessage]:
    try:
        return list(await access.get_thread_history(room_id, thread_id))
    except Exception as exc:
        logger.warning(
            log_message,
            room_id=room_id,
            thread_id=thread_id,
            fallback_message_count=len(fallback_history),
            error=str(exc),
        )
        return list(fallback_history)


async def _thread_snapshot_or_fallback(
    *,
    access: ConversationCacheProtocol,
    logger: structlog.stdlib.BoundLogger,
    room_id: str,
    thread_id: str,
    fallback_history: Sequence[_HistoryMessage],
    log_message: str,
) -> tuple[list[_HistoryMessage], bool]:
    try:
        snapshot = await access.get_thread_snapshot(room_id, thread_id)
    except Exception as exc:
        logger.warning(
            log_message,
            room_id=room_id,
            thread_id=thread_id,
            fallback_message_count=len(fallback_history),
            error=str(exc),
        )
        return list(fallback_history), False
    return list(snapshot), not _thread_history_is_full(snapshot, default=False)


async def derive_conversation_context(
    client: nio.AsyncClient,
    room_id: str,
    event_info: EventInfo,
    caches: ReplyChainCaches,
    logger: structlog.stdlib.BoundLogger,
    access: ConversationCacheProtocol,
) -> tuple[bool, str | None, list[_HistoryMessage]]:
    """Derive conversation context from threads or reply chains."""
    thread_root_id = _thread_root_id_from_event_info(event_info)
    if thread_root_id is not None:
        thread_history = await _thread_history_or_fallback(
            access=access,
            logger=logger,
            room_id=room_id,
            thread_id=thread_root_id,
            fallback_history=[],
            log_message="Failed to fetch thread history for direct thread context; continuing without history",
        )
        return True, thread_root_id, thread_history

    reply_chain_seed = _reply_chain_seed(event_info)
    if not reply_chain_seed:
        return False, None, []

    (
        context_root_id,
        chain_history,
        points_to_thread,
        is_full_thread_history,
    ) = await _resolve_reply_chain(
        client,
        access,
        caches,
        logger,
        room_id,
        reply_chain_seed,
        default_history_is_full=True,
        hydrate_sidecars=True,
    )
    if points_to_thread:
        if is_full_thread_history:
            return True, context_root_id, list(chain_history)

        thread_history = await _thread_history_or_fallback(
            access=access,
            logger=logger,
            room_id=room_id,
            thread_id=context_root_id,
            fallback_history=chain_history,
            log_message="Failed to fetch thread history for reply-chain context; continuing with chain history",
        )
        return True, context_root_id, _merged_thread_history(thread_history, chain_history)

    # Policy choice: reply-only chains are still treated as one conversation
    # context so responder selection and memory use a stable root.
    return True, context_root_id, chain_history


async def derive_conversation_target(
    client: nio.AsyncClient,
    room_id: str,
    event_info: EventInfo,
    caches: ReplyChainCaches,
    logger: structlog.stdlib.BoundLogger,
    access: ConversationCacheProtocol,
) -> tuple[bool, str | None, list[_HistoryMessage], bool]:
    """Derive the conversation target with lightweight history for dispatch.

    Returns:
        Tuple of (is_thread, thread_id, thread_history, requires_full_thread_history)

    """
    thread_root_id = _thread_root_id_from_event_info(event_info)
    if thread_root_id is not None:
        thread_history, requires_full_thread_history = await _thread_snapshot_or_fallback(
            access=access,
            logger=logger,
            room_id=room_id,
            thread_id=thread_root_id,
            fallback_history=[],
            log_message="Failed to fetch thread snapshot for direct thread target; continuing without history",
        )
        return (
            True,
            thread_root_id,
            thread_history,
            requires_full_thread_history,
        )

    reply_chain_seed = _reply_chain_seed(event_info)
    if not reply_chain_seed:
        return False, None, [], False

    (
        context_root_id,
        chain_history,
        points_to_thread,
        is_full_thread_history,
    ) = await _resolve_reply_chain(
        client,
        access,
        caches,
        logger,
        room_id,
        reply_chain_seed,
        default_history_is_full=False,
        hydrate_sidecars=False,
    )
    if points_to_thread:
        if is_full_thread_history:
            return True, context_root_id, list(chain_history), False

        thread_history, requires_full_thread_history = await _thread_snapshot_or_fallback(
            access=access,
            logger=logger,
            room_id=room_id,
            thread_id=context_root_id,
            fallback_history=chain_history,
            log_message="Failed to fetch thread snapshot for reply-chain target; continuing with chain history",
        )
        return (
            True,
            context_root_id,
            _merged_thread_history(thread_history, chain_history),
            requires_full_thread_history,
        )

    return True, context_root_id, list(chain_history), False
