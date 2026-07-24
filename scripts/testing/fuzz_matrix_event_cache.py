"""Replayable state-sequence fuzzing for the durable Matrix event cache.

This module drives realistic joined-timeline writes through ``ThreadSyncWritePolicy``
while interleaving snapshot replacement, invalidation, redaction, duplicate delivery,
opaque-event replay, and concurrent thread activity.

The pytest suite uses Hypothesis to generate and shrink traces.
For longer deterministic runs:

    uv run python scripts/testing/fuzz_matrix_event_cache.py --seed 42 --steps 500

On failure the backend-independent workload trace is printed and can be rerun with:

    uv run python scripts/testing/fuzz_matrix_event_cache.py --trace trace.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import tempfile
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.cache import (
    ConversationEventCache,
    EventCacheWriteCoordinator,
    ThreadRevision,
    thread_cache_rejection_reason,
)
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.cache.thread_write_cache_ops import ThreadMutationCacheOps
from mindroom.matrix.cache.thread_writes import ThreadSyncWritePolicy
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_bookkeeping import ThreadMutationResolver

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from mindroom.bot_runtime_view import BotRuntimeView

FUZZ_PRINCIPAL = "@mindroom_cache_fuzz:localhost"
OTHER_PRINCIPAL = "@mindroom_cache_fuzz_other:localhost"
_BASE_TIMESTAMP = 1_700_000_000_000


def _required_int(value: dict[str, object], key: str) -> int:
    field = value.get(key)
    if not isinstance(field, int) or isinstance(field, bool):
        msg = f"Matrix cache fuzz field {key!r} must be an integer"
        raise TypeError(msg)
    return field


def _required_bool(value: dict[str, object], key: str) -> bool:
    field = value.get(key)
    if not isinstance(field, bool):
        msg = f"Matrix cache fuzz field {key!r} must be a boolean"
        raise TypeError(msg)
    return field


class OperationKind(StrEnum):
    """One mutation family understood by the cache fuzzer."""

    THREADED_MESSAGE = "threaded_message"
    PLAIN_REPLY = "plain_reply"
    EDIT = "edit"
    REACTION = "reaction"
    REFERENCE = "reference"
    REDACTION = "redaction"
    CIPHERTEXT_REPLAY = "ciphertext_replay"
    REPLACE_THREAD = "replace_thread"
    INVALIDATE_THREAD = "invalidate_thread"
    MARK_THREAD_STALE = "mark_thread_stale"
    MARK_ROOM_STALE = "mark_room_stale"
    LIMITED_SYNC = "limited_sync"
    REOPEN_CACHE = "reopen_cache"
    REJOIN_ROOM = "rejoin_room"


@dataclass(frozen=True, slots=True)
class FuzzOperation:
    """Compact, JSON-serializable mutation description."""

    kind: OperationKind
    room: int
    thread: int
    slot: int
    target: int
    variant: int

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> FuzzOperation:
        """Parse one serialized operation."""
        raw_kind = value.get("kind")
        if not isinstance(raw_kind, str):
            msg = "Matrix cache fuzz operation kind must be a string"
            raise TypeError(msg)
        return cls(
            kind=OperationKind(raw_kind),
            room=_required_int(value, "room"),
            thread=_required_int(value, "thread"),
            slot=_required_int(value, "slot"),
            target=_required_int(value, "target"),
            variant=_required_int(value, "variant"),
        )


@dataclass(frozen=True, slots=True)
class FuzzScenario:
    """Self-describing cache workload made of ordered concurrent batches.

    Replay preserves the exact operations and batch boundaries. Operations within one batch run
    concurrently, but their event-loop interleaving is deliberately not recorded.
    """

    batches: tuple[tuple[FuzzOperation, ...], ...]
    room_count: int = 2
    thread_count: int = 4
    verify_reference_model: bool = False

    def to_json(self) -> str:
        """Serialize workload operations, dimensions, and batch boundaries."""
        payload = {
            "version": 2,
            "batches": [[asdict(operation) for operation in batch] for batch in self.batches],
            "room_count": self.room_count,
            "thread_count": self.thread_count,
            "verify_reference_model": self.verify_reference_model,
        }
        return json.dumps(payload, indent=2, sort_keys=True)

    def validate(self) -> None:
        """Reject lifecycle operations that cannot safely race storage users."""
        if self.room_count < 1 or self.thread_count < 1:
            msg = "Matrix cache fuzz room and thread counts must be positive"
            raise ValueError(msg)
        if self.verify_reference_model and any(len(batch) != 1 for batch in self.batches):
            msg = "Matrix cache reference-model workloads require singleton batches"
            raise ValueError(msg)
        for batch in self.batches:
            if not batch:
                msg = "Matrix cache fuzz batches must not be empty"
                raise ValueError(msg)
            for operation in batch:
                if not 0 <= operation.room < self.room_count:
                    msg = f"Matrix cache fuzz room index {operation.room} exceeds configured room count"
                    raise ValueError(msg)
                if not 0 <= operation.thread < self.thread_count:
                    msg = f"Matrix cache fuzz thread index {operation.thread} exceeds configured thread count"
                    raise ValueError(msg)
            if (
                any(operation.kind in {OperationKind.REOPEN_CACHE, OperationKind.REJOIN_ROOM} for operation in batch)
                and len(batch) != 1
            ):
                msg = "Matrix cache lifecycle operations must be singleton batches"
                raise ValueError(msg)
        _validate_event_id_reuse(self)

    @classmethod
    def from_json(cls, value: str) -> FuzzScenario:
        """Load a trace emitted by :meth:`to_json`."""
        payload = json.loads(value)
        if not isinstance(payload, dict) or payload.get("version") != 2:
            msg = "unsupported Matrix cache fuzz trace"
            raise ValueError(msg)
        raw_batches = payload.get("batches")
        if not isinstance(raw_batches, list):
            msg = "Matrix cache fuzz trace is missing batches"
            raise TypeError(msg)
        scenario = cls(
            batches=tuple(
                tuple(FuzzOperation.from_dict(cast("dict[str, object]", operation)) for operation in batch)
                for batch in raw_batches
            ),
            room_count=_required_int(payload, "room_count"),
            thread_count=_required_int(payload, "thread_count"),
            verify_reference_model=_required_bool(payload, "verify_reference_model"),
        )
        scenario.validate()
        return scenario


@dataclass(slots=True)
class _EventSection:
    events: list[object]


@dataclass(slots=True)
class _Timeline:
    events: list[nio.Event]
    limited: bool


@dataclass(slots=True)
class _RoomSync:
    timeline: _Timeline
    state: _EventSection
    ephemeral: _EventSection
    account_data: _EventSection


@dataclass(slots=True)
class _SyncRooms:
    join: dict[str, _RoomSync]
    invite: dict[str, _RoomSync]
    leave: dict[str, _RoomSync]


@dataclass(slots=True)
class _DeviceLists:
    changed: list[str]
    left: list[str]


@dataclass(slots=True)
class _SyncEnvelope:
    rooms: _SyncRooms
    presence: _EventSection
    account_data: _EventSection
    to_device: _EventSection
    device_lists: _DeviceLists


@dataclass(slots=True)
class _CacheRuntime:
    event_cache: ConversationEventCache
    event_cache_write_coordinator: EventCacheWriteCoordinator


@dataclass(frozen=True, slots=True)
class ObservableCacheState:
    """Stable public state used for restart and backend-parity checks."""

    events: tuple[tuple[str, str, str | None], ...]
    mappings: tuple[tuple[str, str, str | None], ...]
    threads: tuple[tuple[str, str, tuple[str, ...]], ...]
    revisions: tuple[tuple[str, str, ThreadRevision | None], ...]
    invalidation_reasons: tuple[tuple[str, str, str | None, str | None], ...]

    def backend_parity_projection(self) -> tuple[object, ...]:
        """Return behavior excluding backend-owned write-sequence values."""
        comparable_revisions = tuple(
            (
                current_room_id,
                current_thread_id,
                None if revision is None else (revision.event_count, revision.max_origin_server_ts),
            )
            for current_room_id, current_thread_id, revision in self.revisions
        )
        return (
            self.events,
            self.mappings,
            self.threads,
            comparable_revisions,
            self.invalidation_reasons,
        )


_REFERENCE_INCREMENTAL_THREAD_REASONS = frozenset(
    {
        "live_thread_mutation",
        "outbound_thread_mutation",
        "sync_thread_mutation",
    },
)


def _reduce_thread_invalidation_reason(current: str | None, incoming: str) -> str:
    """Apply the cache contract's sticky precedence without calling production reducers."""
    if current is None:
        return incoming
    current_is_incremental = current in _REFERENCE_INCREMENTAL_THREAD_REASONS
    incoming_is_incremental = incoming in _REFERENCE_INCREMENTAL_THREAD_REASONS
    if current_is_incremental != incoming_is_incremental:
        return current if incoming_is_incremental else incoming
    return incoming


@dataclass(slots=True)
class ReferenceCacheModel:
    """Independent semantic model for deterministic cache state-machine traces."""

    events: dict[tuple[str, str], dict[str, Any]]
    mappings: dict[tuple[str, str], str]
    threads: dict[tuple[str, str], dict[str, tuple[int, int]]]
    tombstones: set[tuple[str, str]]
    thread_reasons: dict[tuple[str, str], str | None]
    thread_validated_at: dict[tuple[str, str], int]
    room_reasons: dict[str, str]
    room_invalidated_at: dict[str, int]
    clock: int = 0
    thread_write_sequence: int = 0

    @classmethod
    def empty(cls) -> ReferenceCacheModel:
        """Create an empty reference state without consulting a cache backend."""
        return cls(
            events={},
            mappings={},
            threads={},
            tombstones=set(),
            thread_reasons={},
            thread_validated_at={},
            room_reasons={},
            room_invalidated_at={},
        )

    def seed_room(self, room: int, thread_count: int) -> None:
        """Model one authoritative root snapshot per thread."""
        for thread in range(thread_count):
            self.replace_thread(
                room,
                thread,
                [root_source(room, thread)],
            )

    def apply_operation(self, operation: FuzzOperation, *, thread_count: int) -> None:
        """Apply one deterministic operation using Matrix cache contract semantics."""
        self.clock += 1
        if operation.kind in {
            OperationKind.THREADED_MESSAGE,
            OperationKind.PLAIN_REPLY,
            OperationKind.EDIT,
            OperationKind.REACTION,
            OperationKind.REFERENCE,
            OperationKind.CIPHERTEXT_REPLAY,
        }:
            self._apply_source(_operation_sources(operation)[0])
        elif operation.kind is OperationKind.REDACTION:
            self._apply_redaction(operation)
        elif operation.kind is OperationKind.REPLACE_THREAD:
            self.replace_thread(
                operation.room,
                operation.thread,
                _operation_sources(operation),
            )
        elif operation.kind is OperationKind.INVALIDATE_THREAD:
            self._invalidate_thread(operation.room, operation.thread)
        elif operation.kind is OperationKind.MARK_THREAD_STALE:
            reason = "sync_thread_mutation" if operation.variant % 3 else "sync_opaque_encrypted_event"
            self._mark_thread_stale(operation.room, operation.thread, reason)
            if operation.variant % 2:
                self._revalidate_thread(operation.room, operation.thread)
        elif operation.kind is OperationKind.MARK_ROOM_STALE:
            self._mark_room_stale(operation.room, "sync_thread_lookup_unavailable")
        elif operation.kind is OperationKind.LIMITED_SYNC:
            self._mark_room_stale(operation.room, "limited_sync_timeline")
        elif operation.kind is OperationKind.REJOIN_ROOM:
            current_room_id = room_id(operation.room)
            self._purge_room(current_room_id)
            self._mark_room_stale_by_id(current_room_id, "room_rejoined")
            self.clock += 1
            self.seed_room(operation.room, thread_count)

    def _apply_source(self, source: dict[str, Any]) -> None:
        current_room_id = cast("str", source["room_id"])
        event_id = cast("str", source["event_id"])
        event_type = source.get("type")
        accepted = self._store_point_event(source)
        if event_type == "m.reaction":
            return
        current_thread_id = self._resolve_source_thread(source)
        if current_thread_id is None:
            if self._source_can_affect_thread(source):
                self._mark_room_stale_by_id(current_room_id, "sync_thread_lookup_unavailable")
            return
        self.mappings[(current_room_id, event_id)] = current_thread_id
        self.mappings.setdefault((current_room_id, current_thread_id), current_thread_id)
        if event_type == "m.room.encrypted":
            self._mark_thread_stale_by_id(
                current_room_id,
                current_thread_id,
                "sync_opaque_encrypted_event",
            )
            return
        if not accepted:
            return
        self._mark_thread_stale_by_id(
            current_room_id,
            current_thread_id,
            "sync_thread_mutation",
        )
        key = (current_room_id, current_thread_id)
        members = self.threads.get(key)
        if members is None:
            self.thread_reasons[key] = "sync_append_failed"
            return
        self.thread_write_sequence += 1
        members[event_id] = (
            cast("int", source["origin_server_ts"]),
            self.thread_write_sequence,
        )
        self._revalidate_thread_by_id(current_room_id, current_thread_id)

    def _store_point_event(self, source: dict[str, Any]) -> bool:
        current_room_id = cast("str", source["room_id"])
        event_id = cast("str", source["event_id"])
        key = (current_room_id, event_id)
        relation = cast("dict[str, Any]", source.get("content", {})).get("m.relates_to")
        original_event_id = (
            relation.get("event_id") if isinstance(relation, dict) and relation.get("rel_type") == "m.replace" else None
        )
        if key in self.tombstones or (
            isinstance(original_event_id, str) and (current_room_id, original_event_id) in self.tombstones
        ):
            return False
        previous = self.events.get(key)
        if (
            previous is not None
            and previous.get("type") != "m.room.encrypted"
            and source.get("type") == "m.room.encrypted"
        ):
            return False
        self.events[key] = source
        return True

    def _resolve_source_thread(self, source: dict[str, Any]) -> str | None:
        current_room_id = cast("str", source["room_id"])
        content = source.get("content")
        if not isinstance(content, dict):
            return None
        relation = content.get("m.relates_to")
        if isinstance(relation, dict) and relation.get("rel_type") == "m.thread":
            target = relation.get("event_id")
            return target if isinstance(target, str) else None
        new_content = content.get("m.new_content")
        if isinstance(new_content, dict):
            new_relation = new_content.get("m.relates_to")
            if isinstance(new_relation, dict) and new_relation.get("rel_type") == "m.thread":
                target = new_relation.get("event_id")
                if isinstance(target, str):
                    return target
        target = None
        if isinstance(relation, dict):
            reply = relation.get("m.in_reply_to")
            if isinstance(reply, dict):
                target = reply.get("event_id")
            if target is None:
                target = relation.get("event_id")
        return self.mappings.get((current_room_id, target)) if isinstance(target, str) else None

    @staticmethod
    def _source_can_affect_thread(source: dict[str, Any]) -> bool:
        return source.get("type") in {"m.room.message", "m.room.encrypted"}

    def _apply_redaction(self, operation: FuzzOperation) -> None:
        current_room_id = room_id(operation.room)
        target_id = _redaction_target(operation)
        target_key = (current_room_id, target_id)
        target_thread_id = self.mappings.get(target_key)
        target = self.events.get(target_key)
        dependent_edit_ids = [
            event_id
            for (event_room_id, event_id), event in self.events.items()
            if event_room_id == current_room_id and self._edit_target(event) == target_id
        ]
        self.tombstones.add(target_key)
        for event_id in [target_id, *dependent_edit_ids]:
            self._delete_event(current_room_id, event_id)
        if target is not None and target.get("type") != "m.reaction" and target_thread_id is not None:
            self._mark_thread_stale_by_id(
                current_room_id,
                target_thread_id,
                "sync_redaction",
            )
        elif target is not None and target.get("type") != "m.reaction":
            self._mark_room_stale_by_id(
                current_room_id,
                "sync_redaction_lookup_unavailable",
            )

    @staticmethod
    def _edit_target(event: dict[str, Any]) -> str | None:
        content = event.get("content")
        relation = content.get("m.relates_to") if isinstance(content, dict) else None
        target = relation.get("event_id") if isinstance(relation, dict) else None
        return target if relation and relation.get("rel_type") == "m.replace" and isinstance(target, str) else None

    def _delete_event(self, current_room_id: str, event_id: str) -> None:
        self.events.pop((current_room_id, event_id), None)
        mapped_thread_id = self.mappings.pop((current_room_id, event_id), None)
        for (event_room_id, _thread_id), members in self.threads.items():
            if event_room_id == current_room_id:
                members.pop(event_id, None)
        if mapped_thread_id == event_id and self.threads.get((current_room_id, event_id)):
            self.mappings[(current_room_id, event_id)] = event_id

    def replace_thread(
        self,
        room: int,
        thread: int,
        sources: list[dict[str, Any]],
    ) -> None:
        """Replace one modeled snapshot and its owned point/index rows."""
        current_room_id = room_id(room)
        current_thread_id = thread_id(room, thread)
        key = (current_room_id, current_thread_id)
        existing_ids = set(self.threads.get(key, {}))
        accepted_sources = [source for source in sources if self._store_point_event(source)]
        replacement_ids = {cast("str", source["event_id"]) for source in accepted_sources}
        for removed_event_id in existing_ids - replacement_ids:
            self._delete_event(current_room_id, removed_event_id)
        members: dict[str, tuple[int, int]] = {}
        for source in accepted_sources:
            event_id = cast("str", source["event_id"])
            self.thread_write_sequence += 1
            members[event_id] = (
                cast("int", source["origin_server_ts"]),
                self.thread_write_sequence,
            )
            self.mappings[(current_room_id, event_id)] = current_thread_id
        if members:
            self.mappings[(current_room_id, current_thread_id)] = current_thread_id
        self.threads[key] = members
        self.thread_reasons[key] = None
        self.thread_validated_at[key] = self.clock

    def _invalidate_thread(self, room: int, thread: int) -> None:
        current_room_id = room_id(room)
        current_thread_id = thread_id(room, thread)
        key = (current_room_id, current_thread_id)
        for event_id in tuple(self.threads.get(key, {})):
            self._delete_event(current_room_id, event_id)
        self.threads.pop(key, None)
        self.thread_reasons.pop(key, None)
        self.thread_validated_at.pop(key, None)

    def _mark_thread_stale(self, room: int, thread: int, reason: str) -> None:
        self._mark_thread_stale_by_id(room_id(room), thread_id(room, thread), reason)

    def _mark_thread_stale_by_id(
        self,
        current_room_id: str,
        current_thread_id: str,
        reason: str,
    ) -> None:
        key = (current_room_id, current_thread_id)
        self.thread_reasons[key] = _reduce_thread_invalidation_reason(
            self.thread_reasons.get(key),
            reason,
        )

    def _revalidate_thread(self, room: int, thread: int) -> None:
        self._revalidate_thread_by_id(room_id(room), thread_id(room, thread))

    def _revalidate_thread_by_id(
        self,
        current_room_id: str,
        current_thread_id: str,
    ) -> None:
        key = (current_room_id, current_thread_id)
        validated_at = self.thread_validated_at.get(key, -1)
        room_invalidated_at = self.room_invalidated_at.get(current_room_id, -1)
        reason = self.thread_reasons.get(key)
        if reason == "sync_thread_mutation" and validated_at > room_invalidated_at:
            self.thread_reasons[key] = None
            self.thread_validated_at[key] = self.clock

    def _mark_room_stale(self, room: int, reason: str) -> None:
        self._mark_room_stale_by_id(room_id(room), reason)

    def _mark_room_stale_by_id(self, current_room_id: str, reason: str) -> None:
        self.room_reasons[current_room_id] = reason
        self.room_invalidated_at[current_room_id] = self.clock

    def _purge_room(self, current_room_id: str) -> None:
        for key in tuple(self.events):
            if key[0] == current_room_id:
                self.events.pop(key)
        for key in tuple(self.mappings):
            if key[0] == current_room_id:
                self.mappings.pop(key)
        for key in tuple(self.threads):
            if key[0] == current_room_id:
                self.threads.pop(key)
                self.thread_reasons.pop(key, None)
                self.thread_validated_at.pop(key, None)
        self.tombstones = {key for key in self.tombstones if key[0] != current_room_id}
        self.room_reasons.pop(current_room_id, None)
        self.room_invalidated_at.pop(current_room_id, None)

    def latest_edit(self, current_room_id: str, original_event_id: str) -> dict[str, Any] | None:
        """Select a valid surviving edit by the Matrix timestamp/event-ID order."""
        candidates = [
            event
            for (event_room_id, _event_id), event in self.events.items()
            if event_room_id == current_room_id and self._edit_target(event) == original_event_id
        ]
        return max(
            candidates,
            key=lambda event: (
                cast("int", event["origin_server_ts"]),
                cast("str", event["event_id"]),
            ),
            default=None,
        )

    async def assert_matches(
        self,
        cache: ConversationEventCache,
        *,
        known_ids: set[tuple[str, str]],
        room_count: int,
        thread_count: int,
    ) -> None:
        """Compare all public cache projections with independent expected state."""
        for key in sorted(known_ids | set(self.events) | self.tombstones):
            current_room_id, event_id = key
            assert await cache.get_event(current_room_id, event_id) == self.events.get(key), (
                f"reference point payload mismatch: {current_room_id} {event_id}"
            )
            assert await cache.get_thread_id_for_event(current_room_id, event_id) == self.mappings.get(key), (
                f"reference thread mapping mismatch: {current_room_id} {event_id}"
            )
            assert await cache.get_latest_edit(current_room_id, event_id) == self.latest_edit(
                current_room_id,
                event_id,
            ), f"reference latest-edit mismatch: {current_room_id} {event_id}"
        for room in range(room_count):
            current_room_id = room_id(room)
            for thread in range(thread_count):
                current_thread_id = thread_id(room, thread)
                key = (current_room_id, current_thread_id)
                expected_members = self.threads.get(key)
                events = await cache.get_thread_events(current_room_id, current_thread_id)
                expected_ids = (
                    None
                    if expected_members is None
                    else tuple(
                        event_id
                        for event_id, _order in sorted(
                            expected_members.items(),
                            key=lambda item: item[1],
                        )
                    )
                )
                actual_ids = None if events is None else tuple(cast("str", event["event_id"]) for event in events)
                assert actual_ids == expected_ids, (
                    f"reference thread membership/order mismatch: {current_room_id} {current_thread_id}"
                )
                revision = await cache.get_thread_revision(current_room_id, current_thread_id)
                if expected_members is None:
                    assert revision is None
                else:
                    assert revision is not None
                    assert revision.event_count == len(expected_members)
                    assert revision.max_origin_server_ts == max(
                        timestamp for timestamp, _sequence in expected_members.values()
                    )
                state = await cache.get_thread_cache_state(current_room_id, current_thread_id)
                expected_thread_reason = self.thread_reasons.get(key)
                expected_room_reason = self.room_reasons.get(current_room_id)
                if state is not None:
                    assert state.invalidation_reason == expected_thread_reason, (
                        f"reference thread invalidation mismatch: {current_room_id} "
                        f"{current_thread_id} expected={expected_thread_reason!r} "
                        f"actual={state.invalidation_reason!r}"
                    )
                    assert state.room_invalidation_reason == expected_room_reason, (
                        f"reference room invalidation mismatch: {current_room_id} "
                        f"{current_thread_id} expected={expected_room_reason!r} "
                        f"actual={state.room_invalidation_reason!r}"
                    )


def room_id(room: int) -> str:
    """Return one deterministic Matrix room ID."""
    return f"!fuzz-room-{room}:localhost"


def thread_id(room: int, thread: int) -> str:
    """Return one deterministic thread root ID."""
    return f"$fuzz-r{room}-t{thread}-root"


def message_id(
    room: int,
    thread: int,
    slot: int,
    target: int = 0,
    variant: int = 0,
) -> str:
    """Return one deterministic explicit thread-message ID."""
    return f"$fuzz-r{room}-t{thread}-message-{slot}-target-{target}-variant-{variant}"


def reply_id(
    room: int,
    thread: int,
    slot: int,
    target: int = 0,
    variant: int = 0,
) -> str:
    """Return one deterministic reply-only message ID."""
    return f"$fuzz-r{room}-t{thread}-reply-{slot}-target-{target}-variant-{variant}"


def edit_id(room: int, thread: int, target: int, slot: int, variant: int) -> str:
    """Return one deterministic edit ID."""
    return f"$fuzz-r{room}-t{thread}-edit-{slot}-target-{target}-variant-{variant}"


def reaction_id(room: int, thread: int, target: int, slot: int, variant: int = 0) -> str:
    """Return one deterministic reaction ID."""
    return f"$fuzz-r{room}-t{thread}-reaction-{slot}-target-{target}-variant-{variant}"


def reference_id(room: int, thread: int, target: int, slot: int, variant: int = 0) -> str:
    """Return one deterministic reference-message ID."""
    return f"$fuzz-r{room}-t{thread}-reference-{slot}-target-{target}-variant-{variant}"


def _sender(slot: int) -> str:
    return f"@fuzz-user-{slot % 4}:localhost"


def _timestamp(room: int, thread: int, slot: int, offset: int = 0) -> int:
    return _BASE_TIMESTAMP + room * 1_000_000 + thread * 100_000 + slot * 100 + offset


def _operation_timestamp(operation: FuzzOperation, offset: int) -> int:
    """Return normal or deliberately tied timestamps from one compact variant."""
    timestamp_slot = operation.target if operation.variant >= 8 else operation.slot
    return _timestamp(operation.room, operation.thread, timestamp_slot, offset)


def _related_target_id(operation: FuzzOperation) -> str:
    """Select roots, replies, messages, or prior edits with one variant field."""
    target_kind = operation.variant % 4
    if target_kind == 0:
        return message_id(operation.room, operation.thread, operation.target)
    if target_kind == 1:
        return thread_id(operation.room, operation.thread)
    if target_kind == 2:
        return reply_id(operation.room, operation.thread, operation.target)
    return edit_id(
        operation.room,
        operation.thread,
        operation.target,
        operation.target,
        0,
    )


def _related_target_sender_slot(operation: FuzzOperation) -> int:
    target_kind = operation.variant % 4
    return operation.thread if target_kind == 1 else operation.target


def _event_source(
    *,
    event_id: str,
    event_type: str,
    room: int,
    sender: str,
    timestamp: int,
    content: dict[str, Any],
) -> dict[str, Any]:
    return {
        "content": content,
        "event_id": event_id,
        "origin_server_ts": timestamp,
        "room_id": room_id(room),
        "sender": sender,
        "type": event_type,
    }


def root_source(room: int, thread: int) -> dict[str, Any]:
    """Build one thread root."""
    event_id = thread_id(room, thread)
    return _event_source(
        event_id=event_id,
        event_type="m.room.message",
        room=room,
        sender=_sender(thread),
        timestamp=_timestamp(room, thread, 0),
        content={"body": event_id, "msgtype": "m.text"},
    )


def threaded_message_source(operation: FuzzOperation) -> dict[str, Any]:
    """Build one explicit threaded message."""
    event_id = message_id(
        operation.room,
        operation.thread,
        operation.slot,
        operation.target,
        operation.variant,
    )
    return _event_source(
        event_id=event_id,
        event_type="m.room.message",
        room=operation.room,
        sender=_sender(operation.slot),
        timestamp=_operation_timestamp(operation, 10),
        content={
            "body": event_id,
            "msgtype": "m.text",
            "m.relates_to": {
                "event_id": thread_id(operation.room, operation.thread),
                "rel_type": "m.thread",
            },
        },
    )


def plain_reply_source(operation: FuzzOperation) -> dict[str, Any]:
    """Build a reply whose thread must be inferred through its target."""
    event_id = reply_id(
        operation.room,
        operation.thread,
        operation.slot,
        operation.target,
        operation.variant,
    )
    target_id = _related_target_id(operation)
    return _event_source(
        event_id=event_id,
        event_type="m.room.message",
        room=operation.room,
        sender=_sender(operation.slot),
        timestamp=_operation_timestamp(operation, 20),
        content={
            "body": event_id,
            "msgtype": "m.text",
            "m.relates_to": {"m.in_reply_to": {"event_id": target_id}},
        },
    )


def edit_source(operation: FuzzOperation) -> dict[str, Any]:
    """Build a valid or wrong-sender edit of a deterministic message slot."""
    target_id = _related_target_id(operation)
    event_id = edit_id(
        operation.room,
        operation.thread,
        operation.target,
        operation.slot,
        operation.variant,
    )
    new_content: dict[str, Any] = {"body": f"edited {target_id}", "msgtype": "m.text"}
    if operation.variant % 2 == 0:
        new_content["m.relates_to"] = {
            "event_id": thread_id(operation.room, operation.thread),
            "rel_type": "m.thread",
        }
    sender_slot = _related_target_sender_slot(operation)
    if (operation.variant // 4) % 2:
        sender_slot += 1
    return _event_source(
        event_id=event_id,
        event_type="m.room.message",
        room=operation.room,
        sender=_sender(sender_slot),
        timestamp=_operation_timestamp(operation, 30 + operation.variant % 2),
        content={
            "body": f"* edited {target_id}",
            "msgtype": "m.text",
            "m.new_content": new_content,
            "m.relates_to": {"event_id": target_id, "rel_type": "m.replace"},
        },
    )


def reaction_source(operation: FuzzOperation) -> dict[str, Any]:
    """Build one annotation that must remain point-only."""
    target_id = _related_target_id(operation)
    return _event_source(
        event_id=reaction_id(
            operation.room,
            operation.thread,
            operation.target,
            operation.slot,
            operation.variant,
        ),
        event_type="m.reaction",
        room=operation.room,
        sender=_sender(operation.slot),
        timestamp=_operation_timestamp(operation, 40),
        content={
            "m.relates_to": {
                "event_id": target_id,
                "key": ("👍", "👎", "🚀")[operation.variant % 3],
                "rel_type": "m.annotation",
            },
        },
    )


def reference_source(operation: FuzzOperation) -> dict[str, Any]:
    """Build one visible message related by reference."""
    target_id = _related_target_id(operation)
    event_id = reference_id(
        operation.room,
        operation.thread,
        operation.target,
        operation.slot,
        operation.variant,
    )
    return _event_source(
        event_id=event_id,
        event_type="m.room.message",
        room=operation.room,
        sender=_sender(operation.slot),
        timestamp=_operation_timestamp(operation, 50),
        content={
            "body": event_id,
            "msgtype": "m.text",
            "m.relates_to": {"event_id": target_id, "rel_type": "m.reference"},
        },
    )


def ciphertext_source(operation: FuzzOperation) -> dict[str, Any]:
    """Build an opaque replay sharing an explicit message event ID."""
    content: dict[str, Any] = {
        "algorithm": "m.megolm.v1.aes-sha2",
        "ciphertext": "opaque",
        "device_id": "FUZZ",
        "sender_key": "opaque",
        "session_id": "opaque",
    }
    content["m.relates_to"] = {
        "event_id": thread_id(operation.room, operation.thread),
        "rel_type": "m.thread",
    }
    return _event_source(
        event_id=message_id(
            operation.room,
            operation.thread,
            operation.slot,
            operation.target,
            operation.variant,
        ),
        event_type="m.room.encrypted",
        room=operation.room,
        sender=_sender(operation.slot),
        timestamp=_operation_timestamp(operation, 10),
        content=content,
    )


def _raw_event(source: dict[str, Any]) -> nio.Event:
    event_type = source["type"]
    assert isinstance(event_type, str)
    return nio.UnknownEvent(source, event_type)


def _redaction_target(operation: FuzzOperation) -> str:
    target_kind = operation.variant % 6
    if target_kind == 0:
        return thread_id(operation.room, operation.thread)
    if target_kind == 1:
        return message_id(operation.room, operation.thread, operation.target)
    if target_kind == 2:
        return edit_id(
            operation.room,
            operation.thread,
            operation.target,
            operation.slot,
            0,
        )
    if target_kind == 3:
        return reaction_id(operation.room, operation.thread, operation.target, operation.slot)
    if target_kind == 4:
        return reply_id(operation.room, operation.thread, operation.target)
    return reference_id(operation.room, operation.thread, operation.target, operation.slot)


def _redaction_event(operation: FuzzOperation) -> nio.RedactionEvent:
    target_id = _redaction_target(operation)
    source = _event_source(
        event_id=(
            f"$fuzz-r{operation.room}-t{operation.thread}-redaction-{operation.slot}"
            f"-target-{operation.target}-variant-{operation.variant}"
        ),
        event_type="m.room.redaction",
        room=operation.room,
        sender=_sender(operation.slot),
        timestamp=_operation_timestamp(operation, 60),
        content={"reason": "cache fuzz"},
    )
    return nio.RedactionEvent(source, target_id)


def _operation_sources(operation: FuzzOperation) -> list[dict[str, Any]]:
    builders: dict[OperationKind, Callable[[FuzzOperation], dict[str, Any]]] = {
        OperationKind.THREADED_MESSAGE: threaded_message_source,
        OperationKind.PLAIN_REPLY: plain_reply_source,
        OperationKind.EDIT: edit_source,
        OperationKind.REACTION: reaction_source,
        OperationKind.REFERENCE: reference_source,
        OperationKind.CIPHERTEXT_REPLAY: ciphertext_source,
    }
    builder = builders.get(operation.kind)
    if builder is not None:
        return [builder(operation)]
    if operation.kind is OperationKind.REDACTION:
        return [_redaction_event(operation).source]
    if operation.kind is OperationKind.REPLACE_THREAD:
        return [
            root_source(operation.room, operation.thread),
            *[
                threaded_message_source(
                    FuzzOperation(
                        OperationKind.THREADED_MESSAGE,
                        operation.room,
                        operation.thread,
                        slot,
                        0,
                        0,
                    ),
                )
                for slot in range(operation.variant % 6)
            ],
        ]
    return []


def _is_ciphertext_upgrade(first: dict[str, Any], second: dict[str, Any]) -> bool:
    if {first.get("type"), second.get("type")} != {"m.room.encrypted", "m.room.message"}:
        return False
    immutable_keys = ("event_id", "origin_server_ts", "room_id", "sender")
    if any(first.get(key) != second.get(key) for key in immutable_keys):
        return False
    first_content = first.get("content")
    second_content = second.get("content")
    return (
        isinstance(first_content, dict)
        and isinstance(second_content, dict)
        and first_content.get("m.relates_to") == second_content.get("m.relates_to")
    )


def _validate_event_id_reuse(scenario: FuzzScenario) -> None:
    """Reject one event ID describing multiple immutable Matrix events."""
    sources_by_id: dict[tuple[str, str], dict[str, Any]] = {}
    for batch in scenario.batches:
        for operation in batch:
            for source in _operation_sources(operation):
                key = (cast("str", source["room_id"]), cast("str", source["event_id"]))
                previous = sources_by_id.get(key)
                if previous is not None and previous != source and not _is_ciphertext_upgrade(previous, source):
                    msg = f"Matrix cache fuzz event ID changes immutable payload: {key[0]} {key[1]}"
                    raise ValueError(msg)
                if previous is None or previous.get("type") == "m.room.encrypted":
                    sources_by_id[key] = source


def _sync_response(
    events: Sequence[nio.Event],
    *,
    room: int,
    limited: bool = False,
) -> _SyncEnvelope:
    empty = _EventSection(events=[])
    return _SyncEnvelope(
        rooms=_SyncRooms(
            join={
                room_id(room): _RoomSync(
                    timeline=_Timeline(events=list(events), limited=limited),
                    state=empty,
                    ephemeral=_EventSection(events=[]),
                    account_data=_EventSection(events=[]),
                ),
            },
            invite={},
            leave={},
        ),
        presence=_EventSection(events=[]),
        account_data=_EventSection(events=[]),
        to_device=_EventSection(events=[]),
        device_lists=_DeviceLists(changed=[], left=[]),
    )


def _build_sync_policy(cache: ConversationEventCache) -> ThreadSyncWritePolicy:
    logger = get_logger("scripts.testing.fuzz_matrix_event_cache")
    coordinator = EventCacheWriteCoordinator(logger=logger)
    runtime = cast(
        "BotRuntimeView",
        _CacheRuntime(
            event_cache=cache,
            event_cache_write_coordinator=coordinator,
        ),
    )

    async def fetch_event_info(fetched_room_id: str, event_id: str) -> EventInfo | None:
        event = await cache.get_event(fetched_room_id, event_id)
        return None if event is None else EventInfo.from_event(event)

    resolver = ThreadMutationResolver(
        logger_getter=lambda: logger,
        runtime=runtime,
        fetch_event_info_for_thread_resolution=fetch_event_info,
    )
    cache_ops = ThreadMutationCacheOps(
        logger_getter=lambda: logger,
        runtime=runtime,
    )
    return ThreadSyncWritePolicy(
        resolver=resolver,
        cache_ops=cache_ops,
    )


class CacheFuzzRunner:
    """Execute one scenario and continuously assert storage invariants."""

    def __init__(
        self,
        cache: ConversationEventCache,
        scenario: FuzzScenario,
        *,
        room_count: int,
        thread_count: int,
        max_batch_seconds: float | None = None,
        verify_reference_model: bool = False,
    ) -> None:
        self.root_cache = cache
        self.cache = cache.for_principal(FUZZ_PRINCIPAL)
        self.other_cache = cache.for_principal(OTHER_PRINCIPAL)
        self.policy = _build_sync_policy(self.cache)
        self.scenario = scenario
        self.room_count = room_count
        self.thread_count = thread_count
        self.max_batch_seconds = max_batch_seconds
        self.reference_model = ReferenceCacheModel.empty() if verify_reference_model else None
        self.known_ids: set[tuple[str, str]] = set()
        self.redacted_ids: set[tuple[str, str]] = set()
        self.reaction_ids: set[tuple[str, str]] = set()
        self.clear_event_ids: set[tuple[str, str]] = set()
        self.cache_generation: str | None = None
        self.reopen_count = 0
        self.rejoin_count = 0
        self.max_batch_latency_ms = 0.0

    async def seed(self) -> None:
        """Create authoritative roots so later operations can race realistically."""
        for room in range(self.room_count):
            await self._seed_room(room)
            if self.reference_model is not None:
                self.reference_model.seed_room(room, self.thread_count)

    async def _seed_room(self, room: int) -> None:
        """Seed one room after initial startup or a membership-generation reset."""
        current_room_id = room_id(room)
        membership_epoch = await self.cache.room_membership_epoch(current_room_id)
        assert membership_epoch is not None
        for thread in range(self.thread_count):
            root = root_source(room, thread)
            replaced = await self.cache.replace_thread_if_not_newer(
                current_room_id,
                thread_id(room, thread),
                [root],
                expected_membership_epoch=membership_epoch,
                fetch_started_at=float("inf"),
                validated_at=time.time(),
            )
            assert replaced
            self._remember_source(root)

    async def run(self) -> ObservableCacheState:
        """Execute all batches and return stable observable state."""
        self.scenario.validate()
        self.cache_generation = self.root_cache.cache_generation
        assert self.cache_generation is not None
        await self.seed()
        await self.assert_invariants()
        await self._assert_reference_model()
        for batch in self.scenario.batches:
            if self.reference_model is not None:
                for operation in batch:
                    self.reference_model.apply_operation(
                        operation,
                        thread_count=self.thread_count,
                    )
            started = time.perf_counter()
            await asyncio.gather(
                *(self._apply_operation(operation) for operation in batch),
            )
            batch_seconds = time.perf_counter() - started
            self.max_batch_latency_ms = max(self.max_batch_latency_ms, batch_seconds * 1000)
            if self.max_batch_seconds is not None:
                assert batch_seconds <= self.max_batch_seconds, (
                    f"cache fuzz batch exceeded latency bound: {batch_seconds:.3f}s > {self.max_batch_seconds:.3f}s"
                )
            await self.assert_invariants()
            await self._assert_reference_model()
        return await self.observe()

    async def _assert_reference_model(self) -> None:
        if self.reference_model is None:
            return
        await self.reference_model.assert_matches(
            self.cache,
            known_ids=self.known_ids,
            room_count=self.room_count,
            thread_count=self.thread_count,
        )

    async def _apply_sync(self, room: int, events: Sequence[nio.Event], *, limited: bool = False) -> None:
        result = await self.policy.cache_sync_timeline_for_certification(
            cast("nio.SyncResponse", _sync_response(events, room=room, limited=limited)),
        )
        assert result.complete is not limited
        assert result.limited_room_ids == ((room_id(room),) if limited else ())
        assert result.errors == ()

    def _remember_source(self, source: dict[str, Any]) -> None:
        source_room_id = cast("str", source["room_id"])
        source_event_id = cast("str", source["event_id"])
        self.known_ids.add((source_room_id, source_event_id))
        if source["type"] == "m.reaction":
            self.reaction_ids.add((source_room_id, source_event_id))
        if source["type"] != "m.room.encrypted":
            self.clear_event_ids.add((source_room_id, source_event_id))

    async def _apply_operation(self, operation: FuzzOperation) -> None:
        handlers: dict[OperationKind, Callable[[FuzzOperation], Awaitable[None]]] = {
            OperationKind.THREADED_MESSAGE: self._apply_source_operation,
            OperationKind.PLAIN_REPLY: self._apply_source_operation,
            OperationKind.EDIT: self._apply_source_operation,
            OperationKind.REACTION: self._apply_source_operation,
            OperationKind.REFERENCE: self._apply_source_operation,
            OperationKind.REDACTION: self._apply_redaction,
            OperationKind.CIPHERTEXT_REPLAY: self._apply_source_operation,
            OperationKind.REPLACE_THREAD: self._apply_thread_replacement,
            OperationKind.INVALIDATE_THREAD: self._apply_thread_invalidation,
            OperationKind.MARK_THREAD_STALE: self._apply_thread_stale_marker,
            OperationKind.MARK_ROOM_STALE: self._apply_room_stale_marker,
            OperationKind.LIMITED_SYNC: self._apply_limited_sync,
            OperationKind.REOPEN_CACHE: self._apply_cache_reopen,
            OperationKind.REJOIN_ROOM: self._apply_room_rejoin,
        }
        handler = handlers.get(operation.kind)
        if handler is None:
            msg = f"unsupported fuzz operation: {operation.kind}"
            raise AssertionError(msg)
        await handler(operation)

    async def _apply_source_operation(self, operation: FuzzOperation) -> None:
        builders: dict[OperationKind, Callable[[FuzzOperation], dict[str, Any]]] = {
            OperationKind.THREADED_MESSAGE: threaded_message_source,
            OperationKind.PLAIN_REPLY: plain_reply_source,
            OperationKind.EDIT: edit_source,
            OperationKind.REACTION: reaction_source,
            OperationKind.REFERENCE: reference_source,
            OperationKind.CIPHERTEXT_REPLAY: ciphertext_source,
        }
        source_builder = builders.get(operation.kind)
        if source_builder is None:
            msg = f"fuzz operation has no event builder: {operation.kind}"
            raise AssertionError(msg)
        source = source_builder(operation)
        self._remember_source(source)
        await self._apply_sync(operation.room, [_raw_event(source)])

    async def _apply_redaction(self, operation: FuzzOperation) -> None:
        current_room_id = room_id(operation.room)
        target_id = _redaction_target(operation)
        self.known_ids.add((current_room_id, target_id))
        self.redacted_ids.add((current_room_id, target_id))
        await self._apply_sync(operation.room, [_redaction_event(operation)])

    async def _apply_thread_replacement(self, operation: FuzzOperation) -> None:
        current_room_id = room_id(operation.room)
        current_thread_id = thread_id(operation.room, operation.thread)
        upper_slot = operation.variant % 6
        sources = [
            root_source(operation.room, operation.thread),
            *[
                threaded_message_source(
                    FuzzOperation(
                        kind=OperationKind.THREADED_MESSAGE,
                        room=operation.room,
                        thread=operation.thread,
                        slot=slot,
                        target=0,
                        variant=0,
                    ),
                )
                for slot in range(upper_slot)
            ],
        ]
        membership_epoch = await self.cache.room_membership_epoch(current_room_id)
        if membership_epoch is None:
            return
        replaced = await self.cache.replace_thread_if_not_newer(
            current_room_id,
            current_thread_id,
            sources,
            expected_membership_epoch=membership_epoch,
            fetch_started_at=time.time(),
            validated_at=time.time(),
        )
        if replaced:
            for source in sources:
                self._remember_source(source)

    async def _apply_thread_invalidation(self, operation: FuzzOperation) -> None:
        await self.cache.invalidate_thread(
            room_id(operation.room),
            thread_id(operation.room, operation.thread),
        )

    async def _apply_thread_stale_marker(self, operation: FuzzOperation) -> None:
        current_room_id = room_id(operation.room)
        current_thread_id = thread_id(operation.room, operation.thread)
        reason = "sync_thread_mutation" if operation.variant % 3 else "sync_opaque_encrypted_event"
        await self.cache.mark_thread_stale(
            current_room_id,
            current_thread_id,
            reason=reason,
        )
        if operation.variant % 2:
            await self.cache.revalidate_thread_after_incremental_update(
                current_room_id,
                current_thread_id,
            )

    async def _apply_room_stale_marker(self, operation: FuzzOperation) -> None:
        await self.cache.mark_room_threads_stale(
            room_id(operation.room),
            reason="sync_thread_lookup_unavailable",
        )

    async def _apply_limited_sync(self, operation: FuzzOperation) -> None:
        await self._apply_sync(operation.room, [], limited=True)

    async def _apply_cache_reopen(self, _operation: FuzzOperation) -> None:
        """Close and reopen storage while preserving durable state and generation."""
        await self.root_cache.close()
        await self.root_cache.initialize()
        self.cache = self.root_cache.for_principal(FUZZ_PRINCIPAL)
        self.other_cache = self.root_cache.for_principal(OTHER_PRINCIPAL)
        self.policy = _build_sync_policy(self.cache)
        self.reopen_count += 1

    async def _apply_room_rejoin(self, operation: FuzzOperation) -> None:
        """Cross a durable departure epoch, purge, rejoin, and incrementally refill."""
        current_room_id = room_id(operation.room)
        departure_epoch = self.cache.mark_room_departed(current_room_id)
        await self.cache.purge_room(current_room_id)
        await self.cache.mark_room_joined(
            current_room_id,
            expected_departure_epoch=departure_epoch,
        )
        for known_room_id, event_id in tuple(self.redacted_ids):
            if known_room_id == current_room_id:
                self.redacted_ids.discard((known_room_id, event_id))
        for known_room_id, event_id in tuple(self.reaction_ids):
            if known_room_id == current_room_id:
                self.reaction_ids.discard((known_room_id, event_id))
        for known_room_id, event_id in tuple(self.clear_event_ids):
            if known_room_id == current_room_id:
                self.clear_event_ids.discard((known_room_id, event_id))
        await self._seed_room(operation.room)
        self.rejoin_count += 1

    async def _assert_tombstone_invariants(self) -> None:
        for current_room_id, event_id in sorted(self.redacted_ids):
            assert await self.cache.get_event(current_room_id, event_id) is None, (
                f"redacted event resurrected: {current_room_id} {event_id}"
            )
            mapping = await self.cache.get_thread_id_for_event(current_room_id, event_id)
            is_thread_root = any(
                event_id == thread_id(room, thread)
                for room in range(self.room_count)
                for thread in range(self.thread_count)
            )
            assert mapping is None or (is_thread_root and mapping == event_id), (
                f"redacted non-root event retained a thread mapping: {current_room_id} {event_id} -> {mapping}"
            )

    async def _assert_isolation_and_reaction_invariants(self) -> None:
        for current_room_id, event_id in sorted(self.known_ids):
            assert await self.other_cache.get_event(current_room_id, event_id) is None
            assert await self.other_cache.get_thread_id_for_event(current_room_id, event_id) is None

        for current_room_id, event_id in sorted(self.reaction_ids):
            assert await self.cache.get_thread_id_for_event(current_room_id, event_id) is None

        for current_room_id, event_id in sorted(self.clear_event_ids):
            event = await self.cache.get_event(current_room_id, event_id)
            if event is not None:
                assert event.get("type") != "m.room.encrypted", (
                    f"opaque replay downgraded clear payload: {current_room_id} {event_id}"
                )

    async def _assert_thread_invariants(self) -> None:
        for room in range(self.room_count):
            current_room_id = room_id(room)
            for thread in range(self.thread_count):
                current_thread_id = thread_id(room, thread)
                events = await self.cache.get_thread_events(current_room_id, current_thread_id)
                revision = await self.cache.get_thread_revision(current_room_id, current_thread_id)
                if events is None:
                    assert revision is None
                    continue
                cache_state = await self.cache.get_thread_cache_state(
                    current_room_id,
                    current_thread_id,
                )
                assert cache_state is not None
                snapshot_is_reusable = thread_cache_rejection_reason(cache_state) is None
                event_ids = [cast("str", event["event_id"]) for event in events]
                timestamps = [cast("int", event["origin_server_ts"]) for event in events]
                assert len(event_ids) == len(set(event_ids))
                assert timestamps == sorted(timestamps)
                assert revision is not None
                assert revision.event_count == len(events)
                assert revision.max_origin_server_ts == max(timestamps)
                for event, event_id in zip(events, event_ids, strict=True):
                    assert await self.cache.get_event(current_room_id, event_id) == event
                    mapping = await self.cache.get_thread_id_for_event(current_room_id, event_id)
                    if snapshot_is_reusable:
                        assert mapping == current_thread_id, (
                            f"reusable thread snapshot/index mismatch: {current_room_id} "
                            f"{current_thread_id} contains {event_id}, mapped to {mapping}"
                        )
                    assert (current_room_id, event_id) not in self.redacted_ids
                    assert (current_room_id, event_id) not in self.reaction_ids

    async def _assert_latest_edit_invariants(self) -> None:
        for current_room_id, event_id in sorted(self.known_ids):
            latest_edit = await self.cache.get_latest_edit(current_room_id, event_id)
            if latest_edit is None:
                continue
            latest_edit_id = cast("str", latest_edit["event_id"])
            relation = cast("dict[str, Any]", latest_edit["content"]).get("m.relates_to")
            assert isinstance(relation, dict)
            assert relation.get("rel_type") == "m.replace"
            assert relation.get("event_id") == event_id
            assert await self.cache.get_event(current_room_id, latest_edit_id) == latest_edit
            assert (current_room_id, latest_edit_id) not in self.redacted_ids

    async def assert_invariants(self) -> None:
        """Assert cache consistency through only the public storage contract."""
        assert self.root_cache.cache_generation == self.cache_generation
        await self._assert_tombstone_invariants()
        await self._assert_isolation_and_reaction_invariants()
        await self._assert_thread_invariants()
        await self._assert_latest_edit_invariants()

    async def observe(self) -> ObservableCacheState:
        """Read stable state for restart comparisons."""
        events: list[tuple[str, str, str | None]] = []
        mappings: list[tuple[str, str, str | None]] = []
        for current_room_id, event_id in sorted(self.known_ids):
            event = await self.cache.get_event(current_room_id, event_id)
            events.append(
                (
                    current_room_id,
                    event_id,
                    None if event is None else json.dumps(event, sort_keys=True),
                ),
            )
            mappings.append(
                (
                    current_room_id,
                    event_id,
                    await self.cache.get_thread_id_for_event(current_room_id, event_id),
                ),
            )
        threads: list[tuple[str, str, tuple[str, ...]]] = []
        revisions: list[tuple[str, str, ThreadRevision | None]] = []
        invalidation_reasons: list[tuple[str, str, str | None, str | None]] = []
        for room in range(self.room_count):
            current_room_id = room_id(room)
            for thread in range(self.thread_count):
                current_thread_id = thread_id(room, thread)
                thread_events = await self.cache.get_thread_events(current_room_id, current_thread_id)
                threads.append(
                    (
                        current_room_id,
                        current_thread_id,
                        ()
                        if thread_events is None
                        else tuple(cast("str", event["event_id"]) for event in thread_events),
                    ),
                )
                revisions.append(
                    (
                        current_room_id,
                        current_thread_id,
                        await self.cache.get_thread_revision(current_room_id, current_thread_id),
                    ),
                )
                state = await self.cache.get_thread_cache_state(current_room_id, current_thread_id)
                invalidation_reasons.append(
                    (
                        current_room_id,
                        current_thread_id,
                        None if state is None else state.invalidation_reason,
                        None if state is None else state.room_invalidation_reason,
                    ),
                )
        return ObservableCacheState(
            events=tuple(events),
            mappings=tuple(mappings),
            threads=tuple(threads),
            revisions=tuple(revisions),
            invalidation_reasons=tuple(invalidation_reasons),
        )


async def run_scenario(
    cache_factory: Callable[[], ConversationEventCache],
    scenario: FuzzScenario,
    *,
    verify_restart: bool = True,
    max_batch_seconds: float | None = None,
) -> ObservableCacheState:
    """Run one scenario, emitting its backend-independent workload on failure."""
    root_cache = cache_factory()
    await root_cache.initialize()
    runner = CacheFuzzRunner(
        root_cache,
        scenario,
        room_count=scenario.room_count,
        thread_count=scenario.thread_count,
        max_batch_seconds=max_batch_seconds,
        verify_reference_model=scenario.verify_reference_model,
    )
    try:
        result = await runner.run()
        known_ids = set(runner.known_ids)
        redacted_ids = set(runner.redacted_ids)
        reaction_ids = set(runner.reaction_ids)
    except Exception as exc:
        msg = f"{exc}\nMatrix cache fuzz trace:\n{scenario.to_json()}"
        raise AssertionError(msg) from exc
    finally:
        await root_cache.close()

    if not verify_restart:
        return result

    reopened_root = cache_factory()
    await reopened_root.initialize()
    reopened = CacheFuzzRunner(
        reopened_root,
        FuzzScenario(batches=()),
        room_count=scenario.room_count,
        thread_count=scenario.thread_count,
    )
    reopened.known_ids = known_ids
    reopened.redacted_ids = redacted_ids
    reopened.reaction_ids = reaction_ids
    reopened.clear_event_ids = set(runner.clear_event_ids)
    reopened.cache_generation = reopened_root.cache_generation
    try:
        await reopened.assert_invariants()
        restarted_result = await reopened.observe()
        assert restarted_result == result
    except Exception as exc:
        msg = f"{exc}\nMatrix cache fuzz trace:\n{scenario.to_json()}"
        raise AssertionError(msg) from exc
    finally:
        await reopened_root.close()
    return result


_WEIGHTED_KINDS = (
    OperationKind.THREADED_MESSAGE,
    OperationKind.THREADED_MESSAGE,
    OperationKind.THREADED_MESSAGE,
    OperationKind.PLAIN_REPLY,
    OperationKind.PLAIN_REPLY,
    OperationKind.EDIT,
    OperationKind.EDIT,
    OperationKind.REACTION,
    OperationKind.REACTION,
    OperationKind.REFERENCE,
    OperationKind.REDACTION,
    OperationKind.CIPHERTEXT_REPLAY,
    OperationKind.REPLACE_THREAD,
    OperationKind.INVALIDATE_THREAD,
    OperationKind.MARK_THREAD_STALE,
    OperationKind.MARK_ROOM_STALE,
    OperationKind.LIMITED_SYNC,
    OperationKind.REOPEN_CACHE,
    OperationKind.REJOIN_ROOM,
)


def scenario_from_seed(
    seed: int,
    *,
    steps: int,
    room_count: int = 2,
    thread_count: int = 4,
    max_batch_size: int = 8,
    verify_reference_model: bool = False,
) -> FuzzScenario:
    """Generate a deterministic long-running scenario."""
    randomizer = random.Random(seed)  # noqa: S311 - deterministic test trace generation
    batches: list[tuple[FuzzOperation, ...]] = []
    remaining = steps
    while remaining:
        kind = randomizer.choice(_WEIGHTED_KINDS)
        if kind in {OperationKind.REOPEN_CACHE, OperationKind.REJOIN_ROOM}:
            batches.append(
                (
                    FuzzOperation(
                        kind=kind,
                        room=randomizer.randrange(room_count),
                        thread=randomizer.randrange(thread_count),
                        slot=randomizer.randrange(16),
                        target=randomizer.randrange(16),
                        variant=randomizer.randrange(16),
                    ),
                ),
            )
            remaining -= 1
            continue
        batch_size = 1 if verify_reference_model else min(remaining, randomizer.randint(1, max_batch_size))
        batch_operations: list[FuzzOperation] = []
        for _ in range(batch_size):
            operation_kind = randomizer.choice(_WEIGHTED_KINDS)
            while operation_kind in {OperationKind.REOPEN_CACHE, OperationKind.REJOIN_ROOM}:
                operation_kind = randomizer.choice(_WEIGHTED_KINDS)
            batch_operations.append(
                FuzzOperation(
                    kind=operation_kind,
                    room=randomizer.randrange(room_count),
                    thread=randomizer.randrange(thread_count),
                    slot=randomizer.randrange(16),
                    target=randomizer.randrange(16),
                    variant=randomizer.randrange(16),
                ),
            )
        batches.append(tuple(batch_operations))
        remaining -= batch_size
    scenario = FuzzScenario(
        batches=tuple(batches),
        room_count=room_count,
        thread_count=thread_count,
        verify_reference_model=verify_reference_model,
    )
    scenario.validate()
    return scenario


def model_based_scenario() -> FuzzScenario:
    """Exercise one explicit cache state machine across relation and lifecycle states."""
    operation = FuzzOperation
    scenario = FuzzScenario(
        batches=(
            (operation(OperationKind.PLAIN_REPLY, 0, 0, 7, 6, 0),),
            (operation(OperationKind.THREADED_MESSAGE, 0, 0, 6, 0, 0),),
            (operation(OperationKind.PLAIN_REPLY, 0, 0, 7, 6, 0),),
            (operation(OperationKind.THREADED_MESSAGE, 0, 0, 6, 5, 8),),
            (operation(OperationKind.THREADED_MESSAGE, 0, 0, 5, 5, 8),),
            (operation(OperationKind.THREADED_MESSAGE, 0, 0, 3, 0, 0),),
            (operation(OperationKind.EDIT, 0, 0, 8, 6, 0),),
            (operation(OperationKind.EDIT, 0, 0, 9, 0, 1),),
            (operation(OperationKind.REACTION, 0, 0, 10, 6, 0),),
            (operation(OperationKind.REDACTION, 0, 0, 10, 6, 3),),
            (operation(OperationKind.THREADED_MESSAGE, 0, 0, 4, 0, 0),),
            (operation(OperationKind.CIPHERTEXT_REPLAY, 0, 0, 4, 0, 0),),
            (operation(OperationKind.THREADED_MESSAGE, 0, 0, 4, 0, 0),),
            (operation(OperationKind.REOPEN_CACHE, 0, 0, 0, 0, 0),),
            (operation(OperationKind.THREADED_MESSAGE, 0, 1, 1, 0, 0),),
            (operation(OperationKind.THREADED_MESSAGE, 1, 1, 1, 0, 0),),
            (operation(OperationKind.REFERENCE, 1, 2, 2, 0, 1),),
            (operation(OperationKind.LIMITED_SYNC, 1, 0, 0, 0, 0),),
            (operation(OperationKind.REJOIN_ROOM, 1, 0, 0, 0, 0),),
            (operation(OperationKind.THREADED_MESSAGE, 1, 2, 3, 0, 0),),
        ),
        room_count=2,
        thread_count=3,
        verify_reference_model=True,
    )
    scenario.validate()
    return scenario


def concurrent_fanout_scenario(thread_count: int = 45) -> FuzzScenario:
    """Build the 45-way thread burst plus mixed follow-up mutations."""
    initial_messages = tuple(
        FuzzOperation(
            kind=OperationKind.THREADED_MESSAGE,
            room=0,
            thread=thread,
            slot=0,
            target=0,
            variant=0,
        )
        for thread in range(thread_count)
    )
    mixed_mutations = tuple(
        operation
        for thread in range(thread_count)
        for operation in (
            FuzzOperation(OperationKind.EDIT, 0, thread, 1, 0, 0),
            FuzzOperation(OperationKind.REACTION, 0, thread, 2, 0, thread),
            FuzzOperation(OperationKind.PLAIN_REPLY, 0, thread, 3, 0, 1),
        )
    )
    disruptive_mutations = tuple(
        FuzzOperation(
            kind=(OperationKind.REDACTION if thread % 2 else OperationKind.CIPHERTEXT_REPLAY),
            room=0,
            thread=thread,
            slot=0,
            target=0,
            variant=1,
        )
        for thread in range(0, thread_count, 5)
    )
    scenario = FuzzScenario(
        batches=(initial_messages, mixed_mutations, disruptive_mutations),
        room_count=1,
        thread_count=thread_count,
    )
    scenario.validate()
    return scenario


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        msg = "must be at least 1"
        raise argparse.ArgumentTypeError(msg)
    return parsed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--steps", type=_positive_int, default=500)
    parser.add_argument("--rooms", type=_positive_int, default=2)
    parser.add_argument("--threads", type=_positive_int, default=4)
    parser.add_argument("--max-batch-size", type=_positive_int, default=8)
    parser.add_argument(
        "--verify-reference-model",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Verify a sequential workload against the independent model; replay uses the saved setting.",
    )
    parser.add_argument(
        "--trace",
        type=Path,
        help="Rerun a saved backend-independent workload on SQLite.",
    )
    parser.add_argument(
        "--save-trace",
        type=Path,
        help="Save workload operations and batches; backend and scheduler interleaving are not recorded.",
    )
    return parser.parse_args()


def main() -> None:
    """Run a replayable SQLite fuzz scenario."""
    args = _parse_args()
    scenario = (
        FuzzScenario.from_json(args.trace.read_text(encoding="utf-8"))
        if args.trace is not None
        else scenario_from_seed(
            args.seed,
            steps=args.steps,
            room_count=args.rooms,
            thread_count=args.threads,
            max_batch_size=args.max_batch_size,
            verify_reference_model=args.verify_reference_model,
        )
    )
    if args.save_trace is not None:
        args.save_trace.write_text(scenario.to_json() + "\n", encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="mindroom-matrix-cache-fuzz-") as temp_dir:
        db_path = Path(temp_dir) / "event_cache.db"
        asyncio.run(
            run_scenario(
                lambda: SqliteEventCache(db_path),
                scenario,
            ),
        )
    print(
        json.dumps(
            {
                "batches": len(scenario.batches),
                "operations": sum(len(batch) for batch in scenario.batches),
                "rooms": scenario.room_count,
                "seed": args.seed if args.trace is None else None,
                "status": "PASS",
                "threads": scenario.thread_count,
                "verify_reference_model": scenario.verify_reference_model,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()
