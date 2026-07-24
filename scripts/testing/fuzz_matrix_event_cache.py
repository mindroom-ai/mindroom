"""Replayable state-sequence fuzzing for the durable Matrix event cache.

This module drives realistic joined-timeline writes through ``ThreadSyncWritePolicy``
while interleaving snapshot replacement, invalidation, redaction, duplicate delivery,
opaque-event replay, and concurrent thread activity.

The pytest suite uses Hypothesis to generate and shrink traces.
For longer deterministic runs:

    uv run python scripts/testing/fuzz_matrix_event_cache.py --seed 42 --steps 500

On failure the complete JSON trace is printed and can be replayed with:

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
        msg = f"Matrix cache fuzz operation field {key!r} must be an integer"
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
    """Ordered concurrent batches forming one replayable cache history."""

    batches: tuple[tuple[FuzzOperation, ...], ...]

    def to_json(self) -> str:
        """Serialize the complete trace for exact replay."""
        payload = {
            "version": 1,
            "batches": [[asdict(operation) for operation in batch] for batch in self.batches],
        }
        return json.dumps(payload, indent=2, sort_keys=True)

    def validate(self) -> None:
        """Reject lifecycle operations that cannot safely race storage users."""
        for batch in self.batches:
            if not batch:
                msg = "Matrix cache fuzz batches must not be empty"
                raise ValueError(msg)
            if (
                any(operation.kind in {OperationKind.REOPEN_CACHE, OperationKind.REJOIN_ROOM} for operation in batch)
                and len(batch) != 1
            ):
                msg = "Matrix cache lifecycle operations must be singleton batches"
                raise ValueError(msg)

    @classmethod
    def from_json(cls, value: str) -> FuzzScenario:
        """Load a trace emitted by :meth:`to_json`."""
        payload = json.loads(value)
        if not isinstance(payload, dict) or payload.get("version") != 1:
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


def room_id(room: int) -> str:
    """Return one deterministic Matrix room ID."""
    return f"!fuzz-room-{room}:localhost"


def thread_id(room: int, thread: int) -> str:
    """Return one deterministic thread root ID."""
    return f"$fuzz-r{room}-t{thread}-root"


def message_id(room: int, thread: int, slot: int) -> str:
    """Return one deterministic explicit thread-message ID."""
    return f"$fuzz-r{room}-t{thread}-message-{slot}"


def reply_id(room: int, thread: int, slot: int) -> str:
    """Return one deterministic reply-only message ID."""
    return f"$fuzz-r{room}-t{thread}-reply-{slot}"


def edit_id(room: int, thread: int, target: int, slot: int, variant: int) -> str:
    """Return one deterministic edit ID."""
    return f"$fuzz-r{room}-t{thread}-message-{target}-edit-{slot}-{variant % 2}"


def reaction_id(room: int, thread: int, target: int, slot: int) -> str:
    """Return one deterministic reaction ID."""
    return f"$fuzz-r{room}-t{thread}-message-{target}-reaction-{slot}"


def reference_id(room: int, thread: int, target: int, slot: int) -> str:
    """Return one deterministic reference-message ID."""
    return f"$fuzz-r{room}-t{thread}-message-{target}-reference-{slot}"


def _sender(slot: int) -> str:
    return f"@fuzz-user-{slot % 4}:localhost"


def _timestamp(room: int, thread: int, slot: int, offset: int = 0) -> int:
    return _BASE_TIMESTAMP + room * 1_000_000 + thread * 100_000 + slot * 100 + offset


def _operation_timestamp(operation: FuzzOperation, offset: int) -> int:
    """Return normal or deliberately tied timestamps from one compact variant."""
    timestamp_slot = operation.target if operation.variant >= 4 else operation.slot
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
    event_id = message_id(operation.room, operation.thread, operation.slot)
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
    event_id = reply_id(operation.room, operation.thread, operation.slot)
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
    if operation.variant == 7:
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
        event_id=reaction_id(operation.room, operation.thread, operation.target, operation.slot),
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
    event_id = reference_id(operation.room, operation.thread, operation.target, operation.slot)
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
    if operation.variant % 2:
        content["m.relates_to"] = {
            "event_id": thread_id(operation.room, operation.thread),
            "rel_type": "m.thread",
        }
    return _event_source(
        event_id=message_id(operation.room, operation.thread, operation.slot),
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
            operation.variant,
        )
    if target_kind == 3:
        return reaction_id(operation.room, operation.thread, operation.target, operation.slot)
    if target_kind == 4:
        return reply_id(operation.room, operation.thread, operation.target)
    return reference_id(operation.room, operation.thread, operation.target, operation.slot)


def _redaction_event(operation: FuzzOperation) -> nio.RedactionEvent:
    target_id = _redaction_target(operation)
    source = _event_source(
        event_id=f"$redact-{target_id.removeprefix('$')}-{operation.slot}",
        event_type="m.room.redaction",
        room=operation.room,
        sender=_sender(operation.slot),
        timestamp=_operation_timestamp(operation, 60),
        content={"reason": "cache fuzz"},
    )
    return nio.RedactionEvent(source, target_id)


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
    ) -> None:
        self.root_cache = cache
        self.cache = cache.for_principal(FUZZ_PRINCIPAL)
        self.other_cache = cache.for_principal(OTHER_PRINCIPAL)
        self.policy = _build_sync_policy(self.cache)
        self.scenario = scenario
        self.room_count = room_count
        self.thread_count = thread_count
        self.max_batch_seconds = max_batch_seconds
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
        for batch in self.scenario.batches:
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
        return await self.observe()

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
    room_count: int = 2,
    thread_count: int = 4,
    verify_restart: bool = True,
    max_batch_seconds: float | None = None,
) -> ObservableCacheState:
    """Run one scenario, emitting its exact trace on failure."""
    root_cache = cache_factory()
    await root_cache.initialize()
    runner = CacheFuzzRunner(
        root_cache,
        scenario,
        room_count=room_count,
        thread_count=thread_count,
        max_batch_seconds=max_batch_seconds,
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
        room_count=room_count,
        thread_count=thread_count,
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
                        variant=randomizer.randrange(8),
                    ),
                ),
            )
            remaining -= 1
            continue
        batch_size = min(remaining, randomizer.randint(1, max_batch_size))
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
                    variant=randomizer.randrange(8),
                ),
            )
        batches.append(tuple(batch_operations))
        remaining -= batch_size
    scenario = FuzzScenario(batches=tuple(batches))
    scenario.validate()
    return scenario


def model_based_scenario() -> FuzzScenario:
    """Exercise one explicit cache state machine across relation and lifecycle states."""
    operation = FuzzOperation
    scenario = FuzzScenario(
        batches=(
            (operation(OperationKind.PLAIN_REPLY, 0, 0, 7, 6, 0),),
            (operation(OperationKind.THREADED_MESSAGE, 0, 0, 6, 5, 4),),
            (operation(OperationKind.THREADED_MESSAGE, 0, 0, 5, 5, 4),),
            (operation(OperationKind.THREADED_MESSAGE, 0, 0, 3, 0, 0),),
            (operation(OperationKind.PLAIN_REPLY, 0, 0, 7, 6, 0),),
            (operation(OperationKind.EDIT, 0, 0, 8, 6, 0),),
            (operation(OperationKind.EDIT, 0, 0, 9, 0, 1),),
            (operation(OperationKind.REACTION, 0, 0, 10, 6, 0),),
            (operation(OperationKind.REDACTION, 0, 0, 10, 6, 3),),
            (operation(OperationKind.THREADED_MESSAGE, 0, 0, 4, 0, 0),),
            (operation(OperationKind.CIPHERTEXT_REPLAY, 0, 0, 4, 0, 0),),
            (operation(OperationKind.REOPEN_CACHE, 0, 0, 0, 0, 0),),
            (
                operation(OperationKind.THREADED_MESSAGE, 0, 1, 1, 0, 0),
                operation(OperationKind.THREADED_MESSAGE, 1, 1, 1, 0, 0),
                operation(OperationKind.REFERENCE, 1, 2, 2, 0, 1),
            ),
            (operation(OperationKind.LIMITED_SYNC, 1, 0, 0, 0, 0),),
            (operation(OperationKind.REJOIN_ROOM, 1, 0, 0, 0, 0),),
            (operation(OperationKind.THREADED_MESSAGE, 1, 2, 3, 0, 0),),
        ),
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
    return FuzzScenario(
        batches=(initial_messages, mixed_mutations, disruptive_mutations),
    )


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
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--save-trace", type=Path)
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
                room_count=args.rooms,
                thread_count=args.threads,
            ),
        )
    print(
        json.dumps(
            {
                "batches": len(scenario.batches),
                "operations": sum(len(batch) for batch in scenario.batches),
                "seed": args.seed if args.trace is None else None,
                "status": "PASS",
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()
