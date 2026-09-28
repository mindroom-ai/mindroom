"""Matrix thread ordering and projection helpers.

Ordering and projection invariants:

1. Thread order is root first, then ``(origin_server_ts, stable input order, event_id)``.

2. Within one equal-timestamp group, relation ancestors precede their descendants (topological order
   seeded by input order); events outside any relation chain keep their plain sort position.

3. Batch membership for a local event graph comes from ``thread_membership.resolve_local_event_graph_thread_ids``,
   which applies the canonical rules with a memoized walk, so transitive implied membership (reply to a
   reply to a threaded event) resolves regardless of input order, in time linear in the number of events.
"""

from __future__ import annotations

import heapq
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from mindroom.matrix.event_info import EventInfo

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

_TThreadItem = TypeVar("_TThreadItem")


class _SupportsThreadMessageOrdering(Protocol):
    """Minimal protocol for timestamped thread messages ordered by event id."""

    event_id: str
    timestamp: int


def _thread_history_input_order(
    event_id: str,
    input_order_by_event_id: Mapping[str, int] | None,
) -> int:
    """Return the stable secondary ordering index for one event."""
    if input_order_by_event_id is None:
        return 0
    return input_order_by_event_id.get(event_id, 0)


def _build_same_timestamp_relation_graph(
    event_ids: set[str],
    *,
    related_event_id_by_event_id: Mapping[str, str],
) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Return parent and child relationships for one same-timestamp group."""
    in_degree = dict.fromkeys(event_ids, 0)
    children_by_parent: dict[str, list[str]] = {}
    for event_id in in_degree:
        parent_event_id = related_event_id_by_event_id.get(event_id)
        if parent_event_id not in event_ids or parent_event_id == event_id:
            continue
        in_degree[event_id] += 1
        children_by_parent.setdefault(parent_event_id, []).append(event_id)
    return in_degree, children_by_parent


def _topologically_sort_same_timestamp_event_ids(
    event_ids: set[str],
    *,
    related_event_id_by_event_id: Mapping[str, str],
    input_order_by_event_id: Mapping[str, int] | None,
) -> list[str] | None:
    """Return one ancestry-aware order for equal-timestamp events when relations exist."""
    if len(event_ids) < 2:
        return None

    in_degree, children_by_parent = _build_same_timestamp_relation_graph(
        event_ids,
        related_event_id_by_event_id=related_event_id_by_event_id,
    )
    if not children_by_parent:
        return None

    ready: list[tuple[int, str]] = []
    for event_id, degree in in_degree.items():
        if degree == 0:
            heapq.heappush(
                ready,
                (
                    _thread_history_input_order(event_id, input_order_by_event_id),
                    event_id,
                ),
            )

    ordered_event_ids: list[str] = []
    while ready:
        _input_order, event_id = heapq.heappop(ready)
        ordered_event_ids.append(event_id)
        for child_event_id in children_by_parent.get(event_id, ()):
            in_degree[child_event_id] -= 1
            if in_degree[child_event_id] == 0:
                heapq.heappush(
                    ready,
                    (
                        _thread_history_input_order(child_event_id, input_order_by_event_id),
                        child_event_id,
                    ),
                )

    if len(ordered_event_ids) != len(event_ids):
        remaining_event_ids = event_ids - set(ordered_event_ids)
        ordered_event_ids.extend(
            sorted(
                remaining_event_ids,
                key=lambda event_id: (
                    _thread_history_input_order(event_id, input_order_by_event_id),
                    event_id,
                ),
            ),
        )

    return ordered_event_ids


def _sort_same_timestamp_group(
    items: list[_TThreadItem],
    *,
    event_id_getter: Callable[[_TThreadItem], str],
    related_event_id_by_event_id: Mapping[str, str],
    input_order_by_event_id: Mapping[str, int] | None,
) -> list[_TThreadItem]:
    """Keep same-timestamp parents ahead of descendants when relation ancestry is known."""
    if len(items) < 2:
        return items

    items_by_event_id = {event_id_getter(item): item for item in items}
    ordered_event_ids = _topologically_sort_same_timestamp_event_ids(
        set(items_by_event_id),
        related_event_id_by_event_id=related_event_id_by_event_id,
        input_order_by_event_id=input_order_by_event_id,
    )
    if ordered_event_ids is None:
        return items

    return [items_by_event_id[event_id] for event_id in ordered_event_ids]


def _sort_thread_items_root_first(
    items: list[_TThreadItem],
    *,
    thread_id: str,
    event_id_getter: Callable[[_TThreadItem], str],
    timestamp_getter: Callable[[_TThreadItem], int],
    input_order_by_event_id: Mapping[str, int] | None = None,
    related_event_id_by_event_id: Mapping[str, str] | None = None,
) -> None:
    """Keep the thread root first, then order the remaining items chronologically."""
    items.sort(
        key=lambda item: (
            timestamp_getter(item),
            _thread_history_input_order(event_id_getter(item), input_order_by_event_id),
            event_id_getter(item),
        ),
    )
    if related_event_id_by_event_id is not None:
        grouped_items: list[_TThreadItem] = []
        index = 0
        while index < len(items):
            group_end = index + 1
            while group_end < len(items) and timestamp_getter(items[group_end]) == timestamp_getter(items[index]):
                group_end += 1
            grouped_items.extend(
                _sort_same_timestamp_group(
                    items[index:group_end],
                    event_id_getter=event_id_getter,
                    related_event_id_by_event_id=related_event_id_by_event_id,
                    input_order_by_event_id=input_order_by_event_id,
                ),
            )
            index = group_end
        items[:] = grouped_items

    root_index = next(
        (index for index, item in enumerate(items) if event_id_getter(item) == thread_id),
        None,
    )
    if root_index not in (None, 0):
        items.insert(0, items.pop(root_index))


def sort_thread_messages_root_first[TThreadMessageOrdering: _SupportsThreadMessageOrdering](
    messages: list[TThreadMessageOrdering],
    *,
    thread_id: str,
    input_order_by_event_id: Mapping[str, int] | None = None,
    related_event_id_by_event_id: Mapping[str, str] | None = None,
) -> None:
    """Keep the thread root first, then order the remaining messages chronologically."""
    _sort_thread_items_root_first(
        messages,
        thread_id=thread_id,
        event_id_getter=lambda message: message.event_id,
        timestamp_getter=lambda message: message.timestamp,
        input_order_by_event_id=input_order_by_event_id,
        related_event_id_by_event_id=related_event_id_by_event_id,
    )


def _event_id_from_source(event_source: Mapping[str, Any]) -> str | None:
    """Return one Matrix event ID from a raw event source when present."""
    event_id = event_source.get("event_id")
    return event_id if isinstance(event_id, str) else None


def sort_thread_event_sources_root_first(
    event_sources: Sequence[dict[str, Any]],
    *,
    thread_id: str,
) -> list[dict[str, Any]]:
    """Return one stable raw-source order with same-timestamp ancestors before descendants."""
    input_order_by_event_id: dict[str, int] = {}
    related_event_id_by_event_id: dict[str, str] = {}
    for index, event_source in enumerate(event_sources):
        event_id = _event_id_from_source(event_source)
        if event_id is None:
            continue
        input_order_by_event_id[event_id] = index
        related_event_id = EventInfo.from_event(event_source).next_related_event_id(event_id)
        if related_event_id is not None:
            related_event_id_by_event_id[event_id] = related_event_id

    sorted_sources = list(event_sources)
    _sort_thread_items_root_first(
        sorted_sources,
        thread_id=thread_id,
        event_id_getter=lambda source: _event_id_from_source(source) or "",
        timestamp_getter=lambda source: int(source.get("origin_server_ts", 0)),
        input_order_by_event_id=input_order_by_event_id,
        related_event_id_by_event_id=related_event_id_by_event_id,
    )
    return sorted_sources


def ordered_event_ids_from_scanned_event_sources(
    event_sources: Iterable[Mapping[str, Any]],
) -> list[str]:
    """Return scanned event IDs in stable chronological discovery order."""
    event_source_list = list(event_sources)
    input_order_by_event_id = {
        event_id: index
        for index, event_source in enumerate(event_source_list)
        if isinstance((event_id := event_source.get("event_id")), str)
    }
    return [
        event_id
        for event_source in sorted(
            event_source_list,
            key=lambda source: (
                int(source.get("origin_server_ts", 0)),
                input_order_by_event_id.get(str(source.get("event_id", "")), 0),
                str(source.get("event_id", "")),
            ),
        )
        if isinstance((event_id := event_source.get("event_id")), str)
    ]


__all__ = [
    "ordered_event_ids_from_scanned_event_sources",
    "sort_thread_event_sources_root_first",
    "sort_thread_messages_root_first",
]
