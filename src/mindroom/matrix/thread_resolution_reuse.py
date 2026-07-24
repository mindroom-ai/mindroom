"""Process-local reuse of resolved thread history across repeated durable-cache reads.

Long agent threads accumulate one raw ``m.replace`` event per streaming edit, so the durable
row count grows 10-50x faster than the visible message count. Re-parsing and re-resolving every
raw row on each turn is the measured post-lock hotspot. This module keeps the last complete
resolution for one thread per bot and only re-resolves newly written durable rows.

Safety model (any doubt falls back to full resolution by returning ``None``):

1. A snapshot is only comparable when the trusted internal sender set and the durable room
   membership epoch are identical to the ones the snapshot was resolved under.
2. The durable event count plus monotonic payload and thread-index write sequences prove whether
   the thread is unchanged or whether every changed row is present in a bounded delta read. Any
   deletion, replacement, or in-place update forces full resolution.
3. Suffix rows must be plain ``m.room.message`` events with new, unique event IDs that were never
   seen by the snapshot (including edit targets, reply targets, and synthesized originals), and
   every suffix edit - explicit or bundled - must target a suffix-local event, so no suffix row
   can mutate or duplicate an already-resolved message.
4. Snapshots are only stored by the caller when sidecar hydration was fully served from the
   durable cache, so degraded preview bodies are never frozen into future turns.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.event_info import EventInfo

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from mindroom.matrix.cache import ThreadRevision


@dataclass(slots=True)
class ThreadResolutionSnapshot:
    """One complete thread resolution keyed by its durable row revision."""

    messages: list[ResolvedVisibleMessage]
    input_order_by_event_id: dict[str, int]
    related_event_id_by_event_id: dict[str, str]
    known_event_ids: frozenset[str]
    trusted_sender_ids: frozenset[str]
    membership_epoch: int
    revision: ThreadRevision
    sidecar_texts: dict[tuple[str, str], str]

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
        content=deepcopy(message.content),
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
    revision: ThreadRevision,
    sidecar_texts: Mapping[tuple[str, str], str],
    prior_known_event_ids: frozenset[str] = frozenset(),
) -> ThreadResolutionSnapshot:
    """Build one reusable snapshot with private message copies and the full known-ID closure."""
    known_event_ids = set(prior_known_event_ids)
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
        messages=[clone_resolved_visible_message(message) for message in messages],
        input_order_by_event_id=input_order_by_event_id,
        related_event_id_by_event_id=related_event_id_by_event_id,
        known_event_ids=frozenset(known_event_ids),
        trusted_sender_ids=trusted_sender_ids,
        membership_epoch=membership_epoch,
        revision=revision,
        sidecar_texts=dict(sidecar_texts),
    )


class ThreadResolutionReuseCache:
    """Keep the latest reusable thread resolution for one bot."""

    def __init__(self) -> None:
        self._key: tuple[str, str] | None = None
        self._snapshot: ThreadResolutionSnapshot | None = None

    def get(self, room_id: str, thread_id: str) -> ThreadResolutionSnapshot | None:
        """Return the stored snapshot for one thread when present."""
        key = (room_id, thread_id)
        return self._snapshot if key == self._key else None

    def store(self, room_id: str, thread_id: str, snapshot: ThreadResolutionSnapshot) -> None:
        """Replace the prior snapshot with the bot's latest resolved thread."""
        self._key = (room_id, thread_id)
        self._snapshot = snapshot

    def discard(self, room_id: str, thread_id: str) -> None:
        """Drop one snapshot after its durable counterpart was invalidated."""
        if self._key == (room_id, thread_id):
            self._key = None
            self._snapshot = None


def reusable_event_source_suffix(
    snapshot: ThreadResolutionSnapshot,
    suffix: Sequence[dict[str, Any]],
    *,
    trusted_sender_ids: frozenset[str],
    membership_epoch: int,
    revision: ThreadRevision,
) -> list[dict[str, Any]] | None:
    """Return a complete append-only delta when it is safe to merge, else None."""
    unsafe_timestamp = any(
        not isinstance(origin_server_ts := event_source.get("origin_server_ts"), int)
        or isinstance(origin_server_ts, bool)
        or origin_server_ts < snapshot.revision.max_origin_server_ts
        for event_source in suffix
    )
    if (
        snapshot.trusted_sender_ids != trusted_sender_ids
        or snapshot.membership_epoch != membership_epoch
        or revision.event_count <= snapshot.revision.event_count
        or (
            revision.max_write_seq <= snapshot.revision.max_write_seq
            and revision.max_thread_write_seq <= snapshot.revision.max_thread_write_seq
        )
        or len(suffix) != revision.event_count - snapshot.revision.event_count
        or revision.max_origin_server_ts < snapshot.revision.max_origin_server_ts
        or unsafe_timestamp
    ):
        return None
    resolved_suffix = list(suffix)
    if not _suffix_is_safely_appendable(snapshot, resolved_suffix):
        return None
    return resolved_suffix


def snapshot_matches_revision(
    snapshot: ThreadResolutionSnapshot,
    *,
    trusted_sender_ids: frozenset[str],
    membership_epoch: int,
    revision: ThreadRevision,
) -> bool:
    """Return whether durable state still names the snapshot's exact raw rows."""
    return (
        snapshot.trusted_sender_ids == trusted_sender_ids
        and snapshot.membership_epoch == membership_epoch
        and snapshot.revision == revision
    )


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
    "snapshot_matches_revision",
]
