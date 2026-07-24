"""Process-local reuse of resolved thread history across repeated durable-cache reads.

Long agent threads accumulate one raw ``m.replace`` event per streaming edit, so the durable
row count grows 10-50x faster than the visible message count. Re-parsing and re-resolving every
raw row on each turn is the measured post-lock hotspot. This module keeps one bounded per-bot
snapshot of the last complete resolution per thread and only re-resolves the raw-row suffix that
was appended since.

Safety model (any doubt falls back to full resolution by returning ``None``):

1. A snapshot is only comparable when the trusted internal sender set and the durable room
   membership epoch are identical to the ones the snapshot was resolved under.
2. The fresh durable rows must start with the snapshot's exact raw rows (dict equality), so any
   in-place row change (redaction pruning, snapshot replacement, reordering) forces full
   resolution.
3. Suffix rows must be plain ``m.room.message`` events with new, unique event IDs that were never
   seen by the snapshot (including edit targets, reply targets, and synthesized originals), and
   every suffix edit - explicit or bundled - must target a suffix-local event, so no suffix row
   can mutate or duplicate an already-resolved message.
4. Snapshots are only stored by the caller when sidecar hydration was fully served from the
   durable cache, so degraded preview bodies are never frozen into future turns.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.event_info import EventInfo

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

_MAX_SNAPSHOTS_PER_CACHE = 8


@dataclass(slots=True)
class ThreadResolutionSnapshot:
    """One complete thread resolution keyed by the exact durable rows that produced it."""

    event_sources: list[dict[str, Any]]
    messages: list[ResolvedVisibleMessage]
    input_order_by_event_id: dict[str, int]
    related_event_id_by_event_id: dict[str, str]
    known_event_ids: frozenset[str]
    trusted_sender_ids: frozenset[str]
    membership_epoch: int

    def cloned_messages(self) -> list[ResolvedVisibleMessage]:
        """Return caller-owned message copies so later turns never see caller mutations."""
        return [clone_resolved_visible_message(message) for message in self.messages]


def clone_resolved_visible_message(message: ResolvedVisibleMessage) -> ResolvedVisibleMessage:
    """Return one independent copy of a resolved visible message."""
    return ResolvedVisibleMessage(
        sender=message.sender,
        body=message.body,
        timestamp=message.timestamp,
        event_id=message.event_id,
        content=dict(message.content),
        thread_id=message.thread_id,
        latest_event_id=message.latest_event_id,
        stream_status=message.stream_status,
    )


def build_thread_resolution_snapshot(
    *,
    event_sources: Sequence[dict[str, Any]],
    messages: Sequence[ResolvedVisibleMessage],
    input_order_by_event_id: dict[str, int],
    related_event_id_by_event_id: dict[str, str],
    trusted_sender_ids: frozenset[str],
    membership_epoch: int,
) -> ThreadResolutionSnapshot:
    """Build one reusable snapshot with private message copies and the full known-ID closure."""
    known_event_ids: set[str] = set()
    for event_source in event_sources:
        event_id = event_source.get("event_id")
        if isinstance(event_id, str):
            known_event_ids.add(event_id)
    for message in messages:
        known_event_ids.add(message.event_id)
        known_event_ids.add(message.latest_event_id)
    # Relation targets cover edits whose original never resolved to a message: a suffix row
    # reusing such an ID could change how those prior edits apply, so it must force full
    # resolution.
    known_event_ids.update(related_event_id_by_event_id.values())
    return ThreadResolutionSnapshot(
        event_sources=list(event_sources),
        messages=[clone_resolved_visible_message(message) for message in messages],
        input_order_by_event_id=input_order_by_event_id,
        related_event_id_by_event_id=related_event_id_by_event_id,
        known_event_ids=frozenset(known_event_ids),
        trusted_sender_ids=trusted_sender_ids,
        membership_epoch=membership_epoch,
    )


class ThreadResolutionReuseCache:
    """Bounded per-bot LRU of reusable thread resolutions keyed by (room_id, thread_id)."""

    def __init__(self, max_entries: int = _MAX_SNAPSHOTS_PER_CACHE) -> None:
        self._max_entries = max_entries
        self._snapshots: OrderedDict[tuple[str, str], ThreadResolutionSnapshot] = OrderedDict()

    def get(self, room_id: str, thread_id: str) -> ThreadResolutionSnapshot | None:
        """Return the stored snapshot for one thread when present."""
        key = (room_id, thread_id)
        snapshot = self._snapshots.get(key)
        if snapshot is not None:
            self._snapshots.move_to_end(key)
        return snapshot

    def store(self, room_id: str, thread_id: str, snapshot: ThreadResolutionSnapshot) -> None:
        """Store one snapshot, evicting the least recently used entry beyond the cap."""
        key = (room_id, thread_id)
        self._snapshots[key] = snapshot
        self._snapshots.move_to_end(key)
        while len(self._snapshots) > self._max_entries:
            self._snapshots.popitem(last=False)

    def discard(self, room_id: str, thread_id: str) -> None:
        """Drop one snapshot after its durable counterpart was invalidated."""
        self._snapshots.pop((room_id, thread_id), None)


def reusable_event_source_suffix(
    snapshot: ThreadResolutionSnapshot,
    event_sources: Sequence[dict[str, Any]],
    *,
    trusted_sender_ids: frozenset[str],
    membership_epoch: int,
) -> list[dict[str, Any]] | None:
    """Return the appended raw rows when the snapshot is a safe exact prefix, else None."""
    if snapshot.trusted_sender_ids != trusted_sender_ids or snapshot.membership_epoch != membership_epoch:
        return None
    prefix_length = len(snapshot.event_sources)
    if len(event_sources) < prefix_length:
        return None
    if any(
        row != snapshot_row
        for row, snapshot_row in zip(event_sources[:prefix_length], snapshot.event_sources, strict=True)
    ):
        return None
    suffix = list(event_sources[prefix_length:])
    if not _suffix_is_safely_appendable(snapshot, suffix):
        return None
    return suffix


def _suffix_is_safely_appendable(
    snapshot: ThreadResolutionSnapshot,
    suffix: Sequence[dict[str, Any]],
) -> bool:
    """Return whether suffix rows can only introduce new messages or edits to new messages."""
    suffix_event_ids: set[str] = set()
    for event_source in suffix:
        if event_source.get("type") != "m.room.message":
            return False
        event_id = event_source.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            return False
        if event_id in snapshot.known_event_ids or event_id in suffix_event_ids:
            return False
        suffix_event_ids.add(event_id)
    for event_source in suffix:
        event_info = EventInfo.from_event(event_source)
        if event_info.is_edit and event_info.original_event_id not in suffix_event_ids:
            return False
        if any(target not in suffix_event_ids for target in _bundled_edit_target_ids(event_source)):
            return False
    return True


def _bundled_edit_target_ids(event_source: Mapping[str, Any]) -> Iterable[str]:
    """Yield original-event targets of any bundled ``m.replace`` aggregation on one row."""
    unsigned = event_source.get("unsigned")
    if not isinstance(unsigned, Mapping):
        return
    relations = unsigned.get("m.relations")
    if not isinstance(relations, Mapping):
        return
    replacement = relations.get("m.replace")
    if not isinstance(replacement, Mapping):
        return
    for candidate in (replacement.get("event"), replacement.get("latest_event"), replacement):
        if not isinstance(candidate, Mapping):
            continue
        normalized_candidate = {key: value for key, value in candidate.items() if isinstance(key, str)}
        target = EventInfo.from_event(normalized_candidate).original_event_id
        if target is not None:
            yield target


__all__ = [
    "ThreadResolutionReuseCache",
    "ThreadResolutionSnapshot",
    "build_thread_resolution_snapshot",
    "clone_resolved_visible_message",
    "reusable_event_source_suffix",
]
