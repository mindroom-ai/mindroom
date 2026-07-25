"""Reuse fully hydrated thread resolution only for a provably append-only durable suffix."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.matrix.event_info import (
    EventInfo,
    event_source_is_timeline_in_room,
)
from mindroom.matrix.replacements import bundled_replacement_candidates
from mindroom.matrix.thread_membership import event_info_proves_thread_membership

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mindroom.matrix.cache import ThreadRevision
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage


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
        return deepcopy(self.messages)


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
    known_event_ids.update(related_event_id_by_event_id.values())
    return ThreadResolutionSnapshot(
        messages=deepcopy(list(messages)),
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
        return self._snapshot if (room_id, thread_id) == self._key else None

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
    room_id: str,
    thread_id: str,
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
    if not _suffix_is_safely_appendable(
        snapshot,
        resolved_suffix,
        room_id=room_id,
        thread_id=thread_id,
    ):
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
    *,
    room_id: str,
    thread_id: str,
) -> bool:
    """Return whether suffix rows can only introduce new messages or edits to new messages."""
    suffix_event_ids: set[str] = set()
    for event_source in suffix:
        if event_source.get("type") != "m.room.message" or not event_source_is_timeline_in_room(
            event_source,
            room_id,
        ):
            return False
        event_id = event_source.get("event_id")
        event_info = EventInfo.from_event(event_source)
        if (
            not isinstance(event_id, str)
            or not event_id
            or event_id in snapshot.known_event_ids
            or event_id in suffix_event_ids
            or not event_info_proves_thread_membership(event_info, event_id, thread_id)
        ):
            return False
        suffix_event_ids.add(event_id)
    for event_source in suffix:
        event_info = EventInfo.from_event(event_source)
        if event_info.is_edit and event_info.original_event_id not in suffix_event_ids:
            return False
        for candidate in bundled_replacement_candidates(event_source):
            target = EventInfo.from_event(candidate).original_event_id
            if target is not None and target not in suffix_event_ids:
                return False
    return True


__all__ = [
    "ThreadResolutionReuseCache",
    "ThreadResolutionSnapshot",
    "build_thread_resolution_snapshot",
    "reusable_event_source_suffix",
    "snapshot_matches_revision",
]
