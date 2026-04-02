"""Reply-chain resolution for non-thread Matrix clients.

Walks ``m.in_reply_to`` links back to the conversation root so that
plain replies from clients without thread support are mapped to the
correct thread.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.message_content import resolve_event_source_content, visible_body_from_event_source
from mindroom.matrix.thread_history_result import ThreadHistoryResult

if TYPE_CHECKING:
    import structlog

    from mindroom.matrix.client import ResolvedVisibleMessage

# Callback type for fetching thread history, injected by the caller to keep
# this module decoupled from the concrete import (and easy to mock in tests).
type _FetchThreadHistory = Callable[
    [nio.AsyncClient, str, str],
    Coroutine[Any, Any, list[ResolvedVisibleMessage]],
]

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


@dataclass
class _ReplyChainNode:
    """Cached reply-chain node metadata for context derivation."""

    sender: str
    event_id: str
    timestamp: int
    event_source: dict[str, Any]
    parent_event_id: str | None
    thread_root_id: str | None
    has_relations: bool


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


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


async def _node_to_history_message(
    node: _ReplyChainNode,
    client: nio.AsyncClient | None,
) -> ResolvedVisibleMessage:
    """Convert a Matrix event to normalized history message structure."""
    from mindroom.matrix.client import ResolvedVisibleMessage  # noqa: PLC0415

    resolved_source = await resolve_event_source_content(node.event_source, client)
    content = resolved_source.get("content", {})
    fallback_body = ""
    if isinstance(content, dict):
        raw_body = content.get("body")
        if isinstance(raw_body, str):
            fallback_body = raw_body
    return ResolvedVisibleMessage.synthetic(
        sender=node.sender,
        body=visible_body_from_event_source(resolved_source, fallback_body),
        timestamp=node.timestamp,
        event_id=node.event_id,
        content=content if isinstance(content, dict) else {},
    )


async def _materialize_chain_history(
    chain_nodes: Sequence[_ReplyChainNode],
    content_client: nio.AsyncClient | None,
) -> list[ResolvedVisibleMessage]:
    """Convert cached reply-chain nodes into visible history messages."""
    return [await _node_to_history_message(node, content_client) for node in chain_nodes]


def _history_message_event_id(message: ResolvedVisibleMessage) -> str | None:
    """Return the event ID for one history message."""
    event_id = message.event_id
    return event_id if isinstance(event_id, str) else None


def _next_reply_chain_event_id(event_info: EventInfo, current_event_id: str) -> str | None:
    """Resolve the next event in a reply chain."""
    return event_info.next_related_event_id(current_event_id)


def _thread_history_has_replies(thread_history: Sequence[ResolvedVisibleMessage], root_event_id: str) -> bool:
    """Return whether a root event already has thread replies."""
    return any(_history_message_event_id(msg) != root_event_id for msg in thread_history)


def _thread_history_is_full(thread_history: Sequence[ResolvedVisibleMessage], *, default: bool) -> bool:
    """Return whether *thread_history* is already fully hydrated."""
    if isinstance(thread_history, ThreadHistoryResult):
        return bool(thread_history.is_full_history)
    return default


def _unique_history_event_ids(messages: Sequence[ResolvedVisibleMessage]) -> list[str]:
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


def _history_messages_by_event_id(messages: Sequence[ResolvedVisibleMessage]) -> dict[str, ResolvedVisibleMessage]:
    """Index history messages by event ID."""
    return {
        event_id: message for message in messages if isinstance((event_id := _history_message_event_id(message)), str)
    }


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
    thread_history: Sequence[ResolvedVisibleMessage],
    chain_history: Sequence[ResolvedVisibleMessage],
) -> list[ResolvedVisibleMessage]:
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
    caches: ReplyChainCaches,
    logger: structlog.stdlib.BoundLogger,
    room_id: str,
    event_id: str,
) -> _ReplyChainNode | None:
    """Fetch reply-chain node metadata from cache or Matrix."""
    cached_node = caches.nodes.get(room_id, event_id)
    if cached_node:
        return cached_node

    response = await client.room_get_event(room_id, event_id)
    if not isinstance(response, nio.RoomGetEventResponse):
        logger.debug(
            "Failed to resolve reply event for context",
            room_id=room_id,
            event_id=event_id,
            error=str(response),
        )
        return None

    target_event = response.event
    target_info = EventInfo.from_event(target_event.source)
    event_source = target_event.source if isinstance(target_event.source, dict) else {}
    node = _ReplyChainNode(
        sender=target_event.sender,
        event_id=target_event.event_id,
        timestamp=target_event.server_timestamp if isinstance(target_event.server_timestamp, int) else 0,
        event_source=event_source,
        parent_event_id=_next_reply_chain_event_id(target_info, event_id),
        thread_root_id=target_info.thread_id,
        has_relations=target_info.has_relations,
    )
    caches.nodes.put(room_id, event_id, node)
    if node.thread_root_id:
        _cache_roots(caches, room_id, [event_id], node.thread_root_id, points_to_thread=True)
    return node


async def _resolve_direct_thread_root(
    client: nio.AsyncClient,
    caches: ReplyChainCaches,
    fetch_history: _FetchThreadHistory,
    room_id: str,
    event_id: str,
    node: _ReplyChainNode,
    visited_event_ids: list[str],
    chain_history_length: int,
    *,
    default_history_is_full: bool,
) -> tuple[str, Sequence[ResolvedVisibleMessage], bool, bool] | None:
    """Resolve clients that reply to an existing thread root without m.thread metadata."""
    if chain_history_length != 1 or node.parent_event_id or node.thread_root_id or node.has_relations:
        return None

    thread_history = await fetch_history(client, room_id, event_id)
    if not _thread_history_has_replies(thread_history, event_id):
        return None

    caches.nodes.put(
        room_id,
        event_id,
        _ReplyChainNode(
            sender=node.sender,
            event_id=node.event_id,
            timestamp=node.timestamp,
            event_source=node.event_source,
            parent_event_id=node.parent_event_id,
            thread_root_id=event_id,
            has_relations=node.has_relations,
        ),
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

        node = await _fetch_node(client, effective_caches, logger, room_id, current_event_id)
        if node is None:
            break

        next_event_id = node.thread_root_id or node.parent_event_id
        if next_event_id is None:
            if not node.has_relations:
                canonical_event_id = current_event_id
            break
        current_event_id = next_event_id

    return canonical_event_id


async def _build_context_result(
    caches: ReplyChainCaches,
    *,
    room_id: str,
    reply_to_event_id: str,
    chain_nodes: list[_ReplyChainNode],
    visited_event_ids: list[str],
    thread_root_id: str | None,
    content_client: nio.AsyncClient | None,
) -> tuple[str, list[ResolvedVisibleMessage], bool, bool]:
    """Build reply-chain context tuple after traversal is complete."""
    cached_root = _first_cached_root(caches, room_id, visited_event_ids)
    if not chain_nodes:
        if cached_root:
            return cached_root.root_event_id, [], cached_root.points_to_thread, False
        return reply_to_event_id, [], False, False

    # Fetches walk from newest->oldest, but consumers expect chronological history.
    chain_history = await _materialize_chain_history(list(reversed(chain_nodes)), content_client)

    if thread_root_id:
        _cache_roots(caches, room_id, visited_event_ids, thread_root_id, points_to_thread=True)
        return thread_root_id, chain_history, True, False

    root_event_id = _history_message_event_id(chain_history[0]) or reply_to_event_id
    _cache_roots(caches, room_id, visited_event_ids, root_event_id, points_to_thread=False)
    return root_event_id, chain_history, False, False


async def _resolve_reply_chain(
    client: nio.AsyncClient,
    caches: ReplyChainCaches,
    logger: structlog.stdlib.BoundLogger,
    fetch_history: _FetchThreadHistory,
    room_id: str,
    reply_to_event_id: str,
    *,
    default_history_is_full: bool,
    content_client: nio.AsyncClient | None,
) -> tuple[str, list[ResolvedVisibleMessage], bool, bool]:
    """Resolve reply-chain context for clients that don't send thread relations.

    Returns:
        Tuple of (conversation_root_id, context_history, points_to_thread, is_full_thread_history)

    """
    chain_nodes: list[_ReplyChainNode] = []
    thread_root_id: str | None = None
    current_event_id: str | None = reply_to_event_id
    seen_event_ids: set[str] = set()
    visited_event_ids: list[str] = []
    direct_thread_root_context: tuple[str, Sequence[ResolvedVisibleMessage], bool, bool] | None = None

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

        node = await _fetch_node(client, caches, logger, room_id, current_event_id)
        if node is None:
            break

        chain_nodes.append(node)
        if node.thread_root_id:
            thread_root_id = thread_root_id or node.thread_root_id

        direct_thread_root_context = await _resolve_direct_thread_root(
            client,
            caches,
            fetch_history,
            room_id=room_id,
            event_id=current_event_id,
            node=node,
            visited_event_ids=visited_event_ids,
            chain_history_length=len(chain_nodes),
            default_history_is_full=default_history_is_full,
        )
        if direct_thread_root_context is not None:
            break

        current_event_id = node.parent_event_id

    if direct_thread_root_context is not None:
        context_root_id, thread_history, points_to_thread, is_full_thread_history = direct_thread_root_context
        return context_root_id, list(thread_history), points_to_thread, is_full_thread_history

    return await _build_context_result(
        caches,
        room_id=room_id,
        reply_to_event_id=reply_to_event_id,
        chain_nodes=chain_nodes,
        visited_event_ids=visited_event_ids,
        thread_root_id=thread_root_id,
        content_client=content_client,
    )


async def _resolve_reply_chain_target(
    client: nio.AsyncClient,
    caches: ReplyChainCaches,
    logger: structlog.stdlib.BoundLogger,
    room_id: str,
    reply_to_event_id: str,
) -> tuple[str, bool]:
    """Resolve the canonical conversation root without building preview history."""
    current_event_id: str | None = reply_to_event_id
    seen_event_ids: set[str] = set()
    visited_event_ids: list[str] = []
    thread_root_id: str | None = None

    while current_event_id:
        if len(visited_event_ids) >= caches.traversal_limit:
            logger.warning(
                "Reply-chain traversal limit reached while resolving dispatch target",
                room_id=room_id,
                reply_to_event_id=reply_to_event_id,
                traversal_limit=caches.traversal_limit,
                traversed_events=len(visited_event_ids),
                last_event_id=visited_event_ids[-1] if visited_event_ids else None,
            )
            break
        if current_event_id in seen_event_ids:
            logger.debug(
                "Detected reply-chain cycle while resolving dispatch target",
                room_id=room_id,
                event_id=current_event_id,
            )
            break
        seen_event_ids.add(current_event_id)
        visited_event_ids.append(current_event_id)

        node = await _fetch_node(client, caches, logger, room_id, current_event_id)
        if node is None:
            break

        if node.thread_root_id:
            thread_root_id = node.thread_root_id
            break

        current_event_id = node.parent_event_id

    if thread_root_id is not None:
        _cache_roots(caches, room_id, visited_event_ids, thread_root_id, points_to_thread=True)
        return thread_root_id, True

    cached_root = _first_cached_root(caches, room_id, visited_event_ids)
    if cached_root is not None:
        return cached_root.root_event_id, cached_root.points_to_thread

    root_event_id = visited_event_ids[-1] if visited_event_ids else reply_to_event_id
    _cache_roots(caches, room_id, visited_event_ids, root_event_id, points_to_thread=False)
    return root_event_id, False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def derive_conversation_context(
    client: nio.AsyncClient,
    room_id: str,
    event_info: EventInfo,
    caches: ReplyChainCaches,
    logger: structlog.stdlib.BoundLogger,
    fetch_history: _FetchThreadHistory,
) -> tuple[bool, str | None, list[ResolvedVisibleMessage]]:
    """Derive conversation context from threads or reply chains."""
    thread_root_id = event_info.thread_id
    if thread_root_id is None and event_info.is_edit:
        # Edit events use top-level m.replace, but thread relation may still
        # exist in m.new_content for thread edits.
        thread_root_id = event_info.thread_id_from_edit
    if thread_root_id is not None:
        thread_history = await fetch_history(client, room_id, thread_root_id)
        return True, thread_root_id, list(thread_history)

    reply_chain_seed = event_info.original_event_id if event_info.is_edit else event_info.reply_to_event_id
    if not reply_chain_seed:
        return False, None, []

    (
        context_root_id,
        chain_history,
        points_to_thread,
        is_full_thread_history,
    ) = await _resolve_reply_chain(
        client,
        caches,
        logger,
        fetch_history,
        room_id,
        reply_chain_seed,
        default_history_is_full=True,
        content_client=client,
    )
    if points_to_thread:
        if is_full_thread_history:
            return True, context_root_id, list(chain_history)

        thread_history = await fetch_history(client, room_id, context_root_id)
        if chain_history:
            thread_history = _merge_thread_and_chain_history(thread_history, chain_history)
        return True, context_root_id, list(thread_history)

    # Policy choice: reply-only chains are still treated as one conversation
    # context so responder selection and memory use a stable root.
    return True, context_root_id, chain_history


async def derive_conversation_target(
    client: nio.AsyncClient,
    room_id: str,
    event_info: EventInfo,
    caches: ReplyChainCaches,
    logger: structlog.stdlib.BoundLogger,
) -> tuple[bool, str | None, list[ResolvedVisibleMessage], bool]:
    """Derive the conversation target without reconstructing preview history.

    Returns:
        Tuple of (is_thread, thread_id, thread_history, requires_full_thread_history)

    """
    thread_root_id = event_info.thread_id
    if thread_root_id is None and event_info.is_edit:
        thread_root_id = event_info.thread_id_from_edit
    if thread_root_id is not None:
        return True, thread_root_id, [], True

    reply_chain_seed = event_info.original_event_id if event_info.is_edit else event_info.reply_to_event_id
    if not reply_chain_seed:
        return False, None, [], False

    context_root_id, _ = await _resolve_reply_chain_target(
        client,
        caches,
        logger,
        room_id,
        reply_chain_seed,
    )
    return True, context_root_id, [], True
