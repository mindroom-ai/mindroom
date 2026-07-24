"""Replay concurrent Matrix mutations against disposable Tuwunel and MindRoom.

Unlike ``fuzz_matrix_event_cache.py``, this runner crosses the real Matrix
transport and the complete MindRoom sync/dispatch/cache path. It starts an
isolated Tuwunel, a deterministic OpenAI-compatible stub, and the current
worktree's MindRoom process. Every run uses disposable Matrix accounts and
removes the isolated stack afterward.

Run with ``uv run python scripts/testing/fuzz_live_matrix.py --seed 42``.
Use ``--save-trace`` and ``--trace`` to replay the same logical event history
on a new disposable server.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import itertools
import json
import os
import random
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from enum import StrEnum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

import httpx
import nio
import yaml

import mindroom
from mindroom.matrix.sync_tokens import load_sync_checkpoint

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping
    from io import TextIOWrapper

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTANCE_REGISTRY = PROJECT_ROOT / "local" / "instances" / "deploy" / "instances.json"
MODEL_ID = "mindroom-live-fuzz"
AGENT_NAME = "general"
ROOM_KEY = "lobby"
RECOVERY_TIMELINE_LIMIT = 50
REGISTRY_READ_ATTEMPTS = 3
REGISTRY_READ_RETRY_SECONDS = 0.05
SOURCE_MARKER_PATTERN = re.compile(r"LIVE-SOURCE\[([A-Za-z0-9_.:-]+)\]")


def _history_fingerprint(source_markers: Collection[str]) -> str:
    """Return a stable short digest for one model-visible source history."""
    serialized = "\0".join(source_markers).encode()
    return hashlib.sha256(serialized).hexdigest()[:16]


def _source_identity(logical_ref: str, body: str) -> str:
    """Bind one logical source revision to its canonical visible body."""
    return f"{logical_ref}.{hashlib.sha256(body.encode()).hexdigest()[:12]}"


def _source_marker_from_content(content: Mapping[str, object]) -> str:
    """Return the one source identity embedded in sent message content."""
    body = content.get("body")
    markers = SOURCE_MARKER_PATTERN.findall(body) if isinstance(body, str) else []
    if len(markers) != 1:
        msg = f"sent live-fuzz content must contain one source identity, got {markers}"
        raise ValueError(msg)
    return markers[0]


@dataclass(frozen=True, slots=True)
class RuntimeProvenance:
    """Exact code loaded by one live campaign."""

    mindroom_module_path: str
    mindroom_revision: str
    mindroom_expected_revision: str
    nio_module_path: str
    nio_version: str
    nio_revision: str
    nio_expected_revision: str
    mindroom_dirty: bool = False
    nio_dirty: bool = False
    nio_source_hash: str = ""

    def as_dict(self) -> dict[str, str | bool]:
        """Return JSON-ready provenance fields."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _SyncCheckpointState:
    """One observable durable checkpoint file state."""

    token: str | None
    mtime_ns: int | None


def _required_int(value: Mapping[str, object], key: str) -> int:
    field = value.get(key)
    if not isinstance(field, int) or isinstance(field, bool):
        msg = f"Live Matrix fuzz operation field {key!r} must be an integer"
        raise TypeError(msg)
    return field


def _required_string(value: Mapping[str, object], key: str) -> str:
    field = value.get(key)
    if not isinstance(field, str):
        msg = f"Live Matrix fuzz operation field {key!r} must be a string"
        raise TypeError(msg)
    return field


def _optional_int(value: Mapping[str, object], key: str, default: int) -> int:
    field = value.get(key, default)
    if not isinstance(field, int) or isinstance(field, bool):
        msg = f"Live Matrix fuzz operation field {key!r} must be an integer"
        raise TypeError(msg)
    return field


class LiveOperationKind(StrEnum):
    """User-visible Matrix mutation families."""

    THREAD_MESSAGE = "thread_message"
    PLAIN_REPLY = "plain_reply"
    EDIT = "edit"
    REACTION = "reaction"
    REDACTION = "redaction"
    IDEMPOTENT_RETRY = "idempotent_retry"
    RESTART_MINDROOM = "restart_mindroom"


_RECOVERY_OPERATION_KINDS = {
    LiveOperationKind.THREAD_MESSAGE,
    LiveOperationKind.PLAIN_REPLY,
    LiveOperationKind.IDEMPOTENT_RETRY,
}
_SATURATION_OPERATION_KINDS = {LiveOperationKind.THREAD_MESSAGE}


@dataclass(frozen=True, slots=True)
class LiveOperation:
    """One replayable live Matrix action."""

    operation_id: int
    kind: LiveOperationKind
    thread: int
    target: str | None
    room: int = 0
    client: int = 0

    @property
    def event_ref(self) -> str:
        """Return the logical reference for this operation's event."""
        return f"op:{self.operation_id}"

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, object],
        *,
        require_ownership: bool = False,
    ) -> LiveOperation:
        """Parse one serialized operation."""
        raw_target = value.get("target")
        if raw_target is not None and not isinstance(raw_target, str):
            msg = "Live Matrix fuzz operation target must be a string or null"
            raise TypeError(msg)
        room = _required_int(value, "room") if require_ownership else _optional_int(value, "room", 0)
        client = _required_int(value, "client") if require_ownership else _optional_int(value, "client", 0)
        return cls(
            operation_id=_required_int(value, "operation_id"),
            kind=LiveOperationKind(_required_string(value, "kind")),
            thread=_required_int(value, "thread"),
            target=raw_target,
            room=room,
            client=client,
        )


@dataclass(frozen=True, slots=True)
class LiveFuzzScenario:
    """Concurrent live batches with logical references instead of event IDs."""

    thread_count: int
    batches: tuple[tuple[LiveOperation, ...], ...]
    profile: str = "fuzz"
    room_count: int = 1
    client_count: int = 1

    def to_json(self) -> str:
        """Serialize the complete trace for exact replay on a fresh server."""
        return json.dumps(
            {
                "version": 1,
                "profile": self.profile,
                "room_count": self.room_count,
                "client_count": self.client_count,
                "thread_count": self.thread_count,
                "batches": [[asdict(operation) for operation in batch] for batch in self.batches],
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, value: str) -> LiveFuzzScenario:
        """Load a trace emitted by :meth:`to_json`."""
        payload = json.loads(value)
        if isinstance(payload, dict) and "scenario" in payload:
            payload = payload["scenario"]
        if not isinstance(payload, dict) or payload.get("version") != 1:
            msg = "unsupported live Matrix fuzz trace"
            raise ValueError(msg)
        profile = _required_string(payload, "profile")
        raw_batches = payload.get("batches")
        if not isinstance(raw_batches, list):
            msg = "live Matrix fuzz trace is missing batches"
            raise TypeError(msg)
        scenario = cls(
            thread_count=_required_int(payload, "thread_count"),
            batches=tuple(
                tuple(
                    LiveOperation.from_dict(
                        cast("dict[str, object]", operation),
                        require_ownership=profile == "recovery",
                    )
                    for operation in batch
                )
                for batch in raw_batches
            ),
            profile=profile,
            room_count=_optional_int(payload, "room_count", 1),
            client_count=_optional_int(payload, "client_count", 1),
        )
        scenario.validate()
        return scenario

    def validate(self) -> None:
        """Reject traces with impossible same-batch or forward dependencies."""
        _validate_scenario_dimensions(self)
        state = _initial_validation_state(self)
        for batch in self.batches:
            _validate_scenario_batch(self, batch, state)
        _validate_recovery_size(self, state.recovery_sources_by_room)


@dataclass(slots=True)
class _LiveValidationState:
    known_events: set[str]
    known_responses: set[str]
    event_rooms: dict[str, int]
    event_lanes: dict[str, tuple[int, int, int]]
    effective_sources: dict[str, str]
    redacted_events: set[str]
    message_events: set[str]
    operation_ids: set[int]
    recovery_message_lanes: set[tuple[int, int, int]]
    recovery_sources_by_room: dict[int, int]


def _validate_scenario_dimensions(scenario: LiveFuzzScenario) -> None:
    """Validate profile dimensions before inspecting operations."""
    if scenario.thread_count < 1 or scenario.room_count < 1 or scenario.client_count < 1:
        msg = "live Matrix fuzz trace must contain at least one room, client, and thread"
        raise ValueError(msg)
    if scenario.profile not in {"fuzz", "recovery", "saturation"}:
        msg = f"unsupported live Matrix fuzz profile {scenario.profile!r}"
        raise ValueError(msg)
    if scenario.profile != "recovery" and (scenario.room_count != 1 or scenario.client_count != 1):
        msg = f"{scenario.profile} profile requires exactly one declared room and client"
        raise ValueError(msg)
    if scenario.profile == "saturation":
        if scenario.thread_count < 2:
            msg = "saturation profile requires at least one hot and one parallel thread"
            raise ValueError(msg)
        _validate_saturation_shape(scenario.batches, thread_count=scenario.thread_count)


def _initial_validation_state(scenario: LiveFuzzScenario) -> _LiveValidationState:
    """Build logical roots and their ownership metadata."""
    if scenario.profile == "recovery":
        known_events = {
            f"root:{room}:{thread}" for room in range(scenario.room_count) for thread in range(scenario.thread_count)
        }
        event_lanes = {
            f"root:{room}:{thread}": (room, 0, thread)
            for room in range(scenario.room_count)
            for thread in range(scenario.thread_count)
        }
    else:
        known_events = {f"root:{thread}" for thread in range(scenario.thread_count)}
        event_lanes = {f"root:{thread}": (0, 0, thread) for thread in range(scenario.thread_count)}
    known_responses = {f"response:{event_ref}" for event_ref in known_events}
    event_lanes.update({f"response:{event_ref}": event_lanes[event_ref] for event_ref in known_events})
    effective_sources = {event_ref: event_ref for event_ref in known_events}
    effective_sources.update({f"response:{event_ref}": event_ref for event_ref in known_events})
    return _LiveValidationState(
        known_events=known_events,
        known_responses=known_responses,
        event_rooms={event_ref: (_logical_ref_room(event_ref) or 0) for event_ref in known_events | known_responses},
        event_lanes=event_lanes,
        effective_sources=effective_sources,
        redacted_events=set(),
        message_events=set(known_events),
        operation_ids=set(),
        recovery_message_lanes=set(),
        recovery_sources_by_room=defaultdict(int),
    )


def _validate_scenario_batch(
    scenario: LiveFuzzScenario,
    batch: tuple[LiveOperation, ...],
    state: _LiveValidationState,
) -> None:
    """Validate one concurrent batch and publish its completed references."""
    _validate_live_batch_shape(batch)
    new_events: set[str] = set()
    new_responses: set[str] = set()
    new_messages: set[str] = set()
    new_redactions: set[str] = set()
    for operation in batch:
        _validate_profile_operation(scenario.profile, operation)
        _validate_live_operation(
            operation,
            thread_count=scenario.thread_count,
            operation_ids=state.operation_ids,
            allowed_targets=state.known_events | state.known_responses,
            event_rooms=state.event_rooms,
            event_lanes=state.event_lanes,
            effective_sources=state.effective_sources,
            redacted_events=state.redacted_events,
            message_events=state.message_events,
            room_count=scenario.room_count,
            client_count=scenario.client_count,
            profile=scenario.profile,
        )
        if operation.kind not in {
            LiveOperationKind.IDEMPOTENT_RETRY,
            LiveOperationKind.RESTART_MINDROOM,
        }:
            new_events.add(operation.event_ref)
            state.event_rooms[operation.event_ref] = operation.room
            state.event_lanes[operation.event_ref] = (operation.room, operation.client, operation.thread)
            assert operation.target is not None
            state.effective_sources[operation.event_ref] = state.effective_sources[operation.target]
        if operation.kind is LiveOperationKind.REDACTION:
            assert operation.target is not None
            new_redactions.add(operation.target)
        if operation.kind not in {LiveOperationKind.THREAD_MESSAGE, LiveOperationKind.PLAIN_REPLY}:
            continue
        state.effective_sources[operation.event_ref] = operation.event_ref
        if scenario.profile == "recovery":
            lane = (operation.room, operation.client, operation.thread)
            if lane in state.recovery_message_lanes:
                msg = (
                    "recovery messages must use unique room, client, and thread lanes "
                    "so intentional coalescing does not weaken the exact-reply oracle"
                )
                raise ValueError(msg)
            state.recovery_message_lanes.add(lane)
            state.recovery_sources_by_room[operation.room] += 1
        new_messages.add(operation.event_ref)
        response_ref = f"response:{operation.event_ref}"
        new_responses.add(response_ref)
        state.event_rooms[response_ref] = operation.room
        state.event_lanes[response_ref] = (operation.room, operation.client, operation.thread)
        state.effective_sources[response_ref] = operation.event_ref

    state.known_events.update(new_events)
    if scenario.profile != "recovery":
        state.known_responses.update(new_responses)
    state.message_events.update(new_messages)
    state.redacted_events.update(new_redactions)


def _validate_recovery_size(
    scenario: LiveFuzzScenario,
    sources_by_room: Mapping[int, int],
) -> None:
    """Preserve the limited-sync precondition in loaded recovery traces."""
    if scenario.profile != "recovery":
        return
    undersized_rooms = {
        room: sources_by_room[room]
        for room in range(scenario.room_count)
        if sources_by_room[room] <= RECOVERY_TIMELINE_LIMIT
    }
    if undersized_rooms:
        msg = (
            f"recovery profile requires more than {RECOVERY_TIMELINE_LIMIT} "
            f"non-retry sources per room: {undersized_rooms}"
        )
        raise ValueError(msg)


def _validate_live_batch_shape(batch: tuple[LiveOperation, ...]) -> None:
    if not batch:
        msg = "live Matrix fuzz batches must not be empty"
        raise ValueError(msg)
    restart_operations = [operation for operation in batch if operation.kind is LiveOperationKind.RESTART_MINDROOM]
    if restart_operations and len(batch) != 1:
        msg = "MindRoom restart must be a singleton batch"
        raise ValueError(msg)
    reply_threads = [
        (operation.room, operation.thread)
        for operation in batch
        if operation.kind
        in {
            LiveOperationKind.THREAD_MESSAGE,
            LiveOperationKind.PLAIN_REPLY,
        }
    ]
    if len(reply_threads) != len(set(reply_threads)):
        msg = "same-thread messages requiring replies must use separate batches"
        raise ValueError(msg)
    history_mutation_threads = [
        (operation.room, operation.thread)
        for operation in batch
        if operation.kind in {LiveOperationKind.EDIT, LiveOperationKind.REDACTION}
    ]
    history_mutation_lanes = set(history_mutation_threads)
    if len(history_mutation_threads) != len(history_mutation_lanes) or history_mutation_lanes.intersection(
        reply_threads,
    ):
        msg = "same-thread message, edit, and redaction history mutations must use separate batches"
        raise ValueError(msg)


def _validate_profile_operation(profile: str, operation: LiveOperation) -> None:
    if profile == "recovery" and operation.kind not in _RECOVERY_OPERATION_KINDS:
        msg = f"recovery profile does not support {operation.kind}"
        raise ValueError(msg)
    if profile == "saturation" and operation.kind not in _SATURATION_OPERATION_KINDS:
        msg = f"saturation profile does not support {operation.kind}"
        raise ValueError(msg)


def _validate_saturation_shape(
    batches: tuple[tuple[LiveOperation, ...], ...],
    *,
    thread_count: int,
) -> None:
    """Require the hot-then-complete-parallel schedule the runner executes."""
    parallel_started = False
    hot_batch_count = 0
    parallel_batch_count = 0
    parallel_threads = set(range(1, thread_count))
    expected_targets = {thread: f"response:root:{thread}" for thread in range(thread_count)}
    for batch in batches:
        batch_threads = [operation.thread for operation in batch]
        if not parallel_started and batch_threads == [0]:
            hot_batch_count += 1
        else:
            parallel_started = True
            parallel_batch_count += 1
            if len(batch_threads) != len(parallel_threads) or set(batch_threads) != parallel_threads:
                msg = (
                    "saturation parallel batches require exactly one operation for "
                    f"every nonzero thread; got {batch_threads}"
                )
                raise ValueError(msg)
        for operation in batch:
            if operation.kind is not LiveOperationKind.THREAD_MESSAGE:
                continue
            expected_target = expected_targets[operation.thread]
            if operation.target != expected_target:
                msg = f"saturation thread {operation.thread} must target {expected_target!r}, not {operation.target!r}"
                raise ValueError(msg)
            expected_targets[operation.thread] = f"response:{operation.event_ref}"
    if hot_batch_count == 0 or parallel_batch_count == 0:
        msg = "saturation profile requires at least one hot batch and one complete parallel batch"
        raise ValueError(msg)


def _validate_live_operation(
    operation: LiveOperation,
    *,
    thread_count: int,
    operation_ids: set[int],
    allowed_targets: set[str],
    event_rooms: Mapping[str, int],
    event_lanes: Mapping[str, tuple[int, int, int]],
    effective_sources: Mapping[str, str],
    redacted_events: set[str],
    message_events: set[str],
    room_count: int,
    client_count: int,
    profile: str,
) -> None:
    if operation.operation_id in operation_ids:
        msg = f"duplicate live Matrix fuzz operation ID {operation.operation_id}"
        raise ValueError(msg)
    operation_ids.add(operation.operation_id)
    _validate_operation_location(
        operation,
        thread_count=thread_count,
        room_count=room_count,
        client_count=client_count,
    )
    if operation.kind is LiveOperationKind.RESTART_MINDROOM:
        if operation.target is not None:
            msg = "MindRoom restart must not have a target"
            raise ValueError(msg)
        return
    target_room, target_client, target_thread = _validate_live_target(
        operation,
        allowed_targets=allowed_targets,
        event_rooms=event_rooms,
        event_lanes=event_lanes,
        effective_sources=effective_sources,
        redacted_events=redacted_events,
        profile=profile,
    )
    assert operation.target is not None
    if operation.kind is LiveOperationKind.IDEMPOTENT_RETRY and operation.target not in message_events:
        msg = "idempotent retries may only target messages"
        raise ValueError(msg)
    if operation.kind is LiveOperationKind.IDEMPOTENT_RETRY and (
        (target_room, target_client, target_thread) != (operation.room, operation.client, operation.thread)
    ):
        msg = "idempotent retries must preserve the original room, client, and thread ownership"
        raise ValueError(msg)


def _validate_live_target(
    operation: LiveOperation,
    *,
    allowed_targets: set[str],
    event_rooms: Mapping[str, int],
    event_lanes: Mapping[str, tuple[int, int, int]],
    effective_sources: Mapping[str, str],
    redacted_events: set[str],
    profile: str,
) -> tuple[int, int, int]:
    """Validate one relation target against the completed logical state."""
    target = operation.target
    if target is None:
        msg = f"{operation.kind} requires a target"
        raise ValueError(msg)
    if target not in allowed_targets:
        msg = f"unknown or same-batch target {target!r}"
        raise ValueError(msg)
    if target in redacted_events or effective_sources[target] in redacted_events:
        msg = f"{operation.kind} targets redacted event or source {target!r}"
        raise ValueError(msg)
    if operation.kind is LiveOperationKind.REDACTION and target.startswith("response:"):
        msg = "live fuzz clients may not redact agent responses"
        raise ValueError(msg)
    if event_rooms[target] != operation.room:
        msg = f"cross-room target {target!r} from room {operation.room}"
        raise ValueError(msg)
    target_lane = event_lanes[target]
    if profile == "recovery" and target_lane[2] != operation.thread:
        msg = f"recovery target {target!r} belongs to thread {target_lane[2]}, not thread {operation.thread}"
        raise ValueError(msg)
    return target_lane


def _validate_operation_location(
    operation: LiveOperation,
    *,
    thread_count: int,
    room_count: int,
    client_count: int,
) -> None:
    if not 0 <= operation.thread < thread_count:
        msg = f"invalid thread {operation.thread}"
        raise ValueError(msg)
    if not 0 <= operation.room < room_count:
        msg = f"invalid room {operation.room}"
        raise ValueError(msg)
    if not 0 <= operation.client < client_count:
        msg = f"invalid client {operation.client}"
        raise ValueError(msg)


def _logical_ref_room(logical_ref: str) -> int | None:
    """Extract the room slot encoded by recovery root references."""
    root_ref = logical_ref.removeprefix("response:")
    parts = root_ref.split(":")
    if len(parts) == 3 and parts[0] == "root":
        return int(parts[1])
    return None


_WEIGHTED_KINDS = (
    LiveOperationKind.THREAD_MESSAGE,
    LiveOperationKind.THREAD_MESSAGE,
    LiveOperationKind.THREAD_MESSAGE,
    LiveOperationKind.PLAIN_REPLY,
    LiveOperationKind.PLAIN_REPLY,
    LiveOperationKind.EDIT,
    LiveOperationKind.EDIT,
    LiveOperationKind.REACTION,
    LiveOperationKind.REACTION,
    LiveOperationKind.REACTION,
    LiveOperationKind.REDACTION,
    LiveOperationKind.IDEMPOTENT_RETRY,
)


@dataclass(slots=True)
class _ScenarioGenerationState:
    messages: dict[int, list[str]]
    responses: dict[int, list[str]]
    editable: dict[int, list[str]]
    reaction_targets: dict[int, list[str]]
    redactable: dict[int, list[str]]
    effective_sources: dict[str, str]
    redacted: set[str]


def _initial_generation_state(thread_count: int) -> _ScenarioGenerationState:
    roots = {f"root:{thread}" for thread in range(thread_count)}
    return _ScenarioGenerationState(
        messages={thread: [f"root:{thread}"] for thread in range(thread_count)},
        responses={thread: [f"response:root:{thread}"] for thread in range(thread_count)},
        editable={thread: [f"root:{thread}", f"response:root:{thread}"] for thread in range(thread_count)},
        reaction_targets={thread: [f"root:{thread}", f"response:root:{thread}"] for thread in range(thread_count)},
        redactable={thread: [] for thread in range(thread_count)},
        effective_sources={
            **{root: root for root in roots},
            **{f"response:{root}": root for root in roots},
        },
        redacted=set(),
    )


def _generation_target_is_active(state: _ScenarioGenerationState, target: str) -> bool:
    """Return whether a generated relation can still target this event."""
    return target not in state.redacted and state.effective_sources[target] not in state.redacted


def _choose_operation(
    randomizer: random.Random,
    state: _ScenarioGenerationState,
    *,
    operation_id: int,
    thread_count: int,
) -> LiveOperation:
    thread = randomizer.randrange(thread_count)
    kind = randomizer.choice(_WEIGHTED_KINDS)
    available_messages = [target for target in state.messages[thread] if _generation_target_is_active(state, target)]
    available_responses = [target for target in state.responses[thread] if _generation_target_is_active(state, target)]
    available_edits = [target for target in state.editable[thread] if _generation_target_is_active(state, target)]
    available_reactions = [
        target for target in state.reaction_targets[thread] if _generation_target_is_active(state, target)
    ]
    available_redactions = [
        target for target in state.redactable[thread] if _generation_target_is_active(state, target)
    ]
    available_retries = [target for target in state.messages[thread] if _generation_target_is_active(state, target)]

    if kind is LiveOperationKind.THREAD_MESSAGE:
        target = randomizer.choice(available_messages)
    elif kind is LiveOperationKind.PLAIN_REPLY:
        target = randomizer.choice(available_responses)
    elif kind is LiveOperationKind.EDIT:
        target = randomizer.choice(available_edits)
    elif kind is LiveOperationKind.REACTION:
        target = randomizer.choice(available_reactions)
    elif kind is LiveOperationKind.REDACTION and available_redactions:
        target = randomizer.choice(available_redactions)
    elif kind is LiveOperationKind.IDEMPOTENT_RETRY and available_retries:
        target = randomizer.choice(available_retries)
    else:
        kind = LiveOperationKind.REACTION
        target = randomizer.choice(available_reactions)
    return LiveOperation(operation_id=operation_id, kind=kind, thread=thread, target=target)


def _update_generation_state(
    state: _ScenarioGenerationState,
    operations: Collection[LiveOperation],
) -> None:
    for operation in operations:
        if operation.kind in {
            LiveOperationKind.THREAD_MESSAGE,
            LiveOperationKind.PLAIN_REPLY,
        }:
            state.messages[operation.thread].append(operation.event_ref)
            state.responses[operation.thread].append(f"response:{operation.event_ref}")
            state.editable[operation.thread].extend((operation.event_ref, f"response:{operation.event_ref}"))
            state.reaction_targets[operation.thread].extend(
                (operation.event_ref, f"response:{operation.event_ref}"),
            )
            state.redactable[operation.thread].append(operation.event_ref)
            state.effective_sources[operation.event_ref] = operation.event_ref
            state.effective_sources[f"response:{operation.event_ref}"] = operation.event_ref
        elif operation.kind in {LiveOperationKind.EDIT, LiveOperationKind.REACTION}:
            state.reaction_targets[operation.thread].append(operation.event_ref)
            state.redactable[operation.thread].append(operation.event_ref)
            assert operation.target is not None
            state.effective_sources[operation.event_ref] = state.effective_sources[operation.target]
        elif operation.kind is LiveOperationKind.REDACTION:
            assert operation.target is not None
            state.redacted.add(operation.target)


def live_scenario_from_seed(
    seed: int,
    *,
    steps: int,
    thread_count: int = 45,
    max_batch_size: int = 16,
    restart_interval: int = 100,
) -> LiveFuzzScenario:
    """Generate realistic concurrent batches with only prior-batch dependencies."""
    if steps < 1 or thread_count < 1 or max_batch_size < 1 or restart_interval < 0:
        msg = "steps, threads, and batch size must be positive; restart interval must be non-negative"
        raise ValueError(msg)

    randomizer = random.Random(seed)  # noqa: S311 - deterministic test trace generation
    state = _initial_generation_state(thread_count)
    batches: list[tuple[LiveOperation, ...]] = []
    operation_id = 0
    generated = 0
    next_restart = restart_interval

    while generated < steps:
        if restart_interval and generated >= next_restart:
            batches.append(
                (
                    LiveOperation(
                        operation_id=operation_id,
                        kind=LiveOperationKind.RESTART_MINDROOM,
                        thread=0,
                        target=None,
                    ),
                ),
            )
            operation_id += 1
            next_restart += restart_interval

        batch_size = min(steps - generated, randomizer.randint(1, max_batch_size))
        operations: list[LiveOperation] = []
        reply_threads: set[int] = set()
        history_mutation_threads: set[int] = set()
        for offset in range(batch_size):
            operation = _choose_operation(
                randomizer,
                state,
                operation_id=operation_id + offset,
                thread_count=thread_count,
            )
            needs_reply = operation.kind in {
                LiveOperationKind.THREAD_MESSAGE,
                LiveOperationKind.PLAIN_REPLY,
            }
            mutates_history = operation.kind in {
                LiveOperationKind.EDIT,
                LiveOperationKind.REDACTION,
            }
            if (needs_reply or mutates_history) and operation.thread in (reply_threads | history_mutation_threads):
                operation = LiveOperation(
                    operation_id=operation.operation_id,
                    kind=LiveOperationKind.REACTION,
                    thread=operation.thread,
                    target=randomizer.choice(
                        [
                            target
                            for target in state.reaction_targets[operation.thread]
                            if _generation_target_is_active(state, target)
                        ],
                    ),
                )
                needs_reply = False
                mutates_history = False
            operations.append(operation)
            if needs_reply:
                reply_threads.add(operation.thread)
            elif mutates_history:
                history_mutation_threads.add(operation.thread)
        operation_id += batch_size

        batches.append(tuple(operations))
        generated += len(operations)
        _update_generation_state(state, operations)

    scenario = LiveFuzzScenario(thread_count=thread_count, batches=tuple(batches))
    scenario.validate()
    return scenario


def recovery_scenario_from_seed(
    seed: int,
    *,
    messages_per_room: int = 64,
    room_count: int = 3,
    thread_count: int = 12,
    client_count: int = 6,
    max_batch_size: int = 12,
) -> LiveFuzzScenario:
    """Build a deterministic multi-room outage and limited-sync recovery schedule."""
    if min(messages_per_room, room_count, thread_count, client_count, max_batch_size) < 1:
        msg = "recovery profile dimensions must be positive"
        raise ValueError(msg)
    if messages_per_room <= RECOVERY_TIMELINE_LIMIT:
        msg = (
            f"recovery profile requires more than {RECOVERY_TIMELINE_LIMIT} messages per room to exceed the sync window"
        )
        raise ValueError(msg)
    if messages_per_room > client_count * thread_count:
        msg = (
            "recovery profile needs at least one unique client/thread lane per message "
            "to distinguish transport loss from intentional coalescing"
        )
        raise ValueError(msg)

    randomizer = random.Random(seed)  # noqa: S311 - deterministic test trace generation
    pending = dict.fromkeys(range(room_count), messages_per_room)
    lanes = {
        room: [(client, thread) for client in range(client_count) for thread in range(thread_count)]
        for room in range(room_count)
    }
    for room_lanes in lanes.values():
        randomizer.shuffle(room_lanes)
    batches: list[tuple[LiveOperation, ...]] = []
    sent_messages: list[LiveOperation] = []
    operation_id = 0
    round_index = 0
    while any(pending.values()):
        batch: list[LiveOperation] = []
        used_threads: set[tuple[int, int]] = set()
        candidates = [room for room in range(room_count) for _ in range(min(pending[room], max_batch_size))]
        randomizer.shuffle(candidates)
        for room in candidates:
            if len(batch) >= max_batch_size or pending[room] == 0:
                continue
            available_lanes = [lane for lane in lanes[room] if (room, lane[1]) not in used_threads]
            if not available_lanes:
                continue
            client, thread = randomizer.choice(available_lanes)
            lanes[room].remove((client, thread))
            used_threads.add((room, thread))
            root_ref = f"root:{room}:{thread}"
            kind = (
                LiveOperationKind.PLAIN_REPLY
                if (operation_id + round_index) % 3 == 0
                else LiveOperationKind.THREAD_MESSAGE
            )
            target = f"response:{root_ref}" if kind is LiveOperationKind.PLAIN_REPLY else root_ref
            operation = LiveOperation(
                operation_id=operation_id,
                kind=kind,
                thread=thread,
                target=target,
                room=room,
                client=client,
            )
            batch.append(operation)
            sent_messages.append(operation)
            pending[room] -= 1
            operation_id += 1
        batches.append(tuple(batch))

        retry_candidates = [operation for operation in sent_messages[-len(batch) :] if operation.operation_id % 11 == 0]
        if retry_candidates:
            batches.append(
                tuple(
                    LiveOperation(
                        operation_id=operation_id + offset,
                        kind=LiveOperationKind.IDEMPOTENT_RETRY,
                        thread=source.thread,
                        target=source.event_ref,
                        room=source.room,
                        client=source.client,
                    )
                    for offset, source in enumerate(retry_candidates)
                ),
            )
            operation_id += len(retry_candidates)
        round_index += 1

    scenario = LiveFuzzScenario(
        thread_count=thread_count,
        batches=tuple(batches),
        profile="recovery",
        room_count=room_count,
        client_count=client_count,
    )
    scenario.validate()
    return scenario


def saturation_scenario(
    *,
    hot_turns: int = 100,
    parallel_threads: int = 12,
    parallel_turns: int = 8,
) -> LiveFuzzScenario:
    """Reproduce the long-thread plus 12-way saturation workload."""
    if hot_turns < 1 or parallel_threads < 1 or parallel_turns < 1:
        msg = "saturation workload dimensions must all be positive"
        raise ValueError(msg)
    thread_count = parallel_threads + 1
    batches: list[tuple[LiveOperation, ...]] = []
    operation_id = 0
    hot_parent = "response:root:0"
    for _ in range(hot_turns):
        operation = LiveOperation(
            operation_id=operation_id,
            kind=LiveOperationKind.THREAD_MESSAGE,
            thread=0,
            target=hot_parent,
        )
        batches.append((operation,))
        hot_parent = f"response:{operation.event_ref}"
        operation_id += 1

    parallel_parents = {thread: f"response:root:{thread}" for thread in range(1, thread_count)}
    for _ in range(parallel_turns):
        batch: list[LiveOperation] = []
        for thread in range(1, thread_count):
            operation = LiveOperation(
                operation_id=operation_id,
                kind=LiveOperationKind.THREAD_MESSAGE,
                thread=thread,
                target=parallel_parents[thread],
            )
            batch.append(operation)
            parallel_parents[thread] = f"response:{operation.event_ref}"
            operation_id += 1
        batches.append(tuple(batch))

    scenario = LiveFuzzScenario(
        thread_count=thread_count,
        batches=tuple(batches),
        profile="saturation",
    )
    scenario.validate()
    return scenario


class _ModelHandler(BaseHTTPRequestHandler):
    """Small deterministic OpenAI-compatible endpoint for live transport tests."""

    protocol_version = "HTTP/1.1"
    call_ids = itertools.count(1)
    stream_segments = 4
    stream_delay = 0.001

    def _send_json(self, payload: Mapping[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/v1/models":
            self._send_json(
                {
                    "object": "list",
                    "data": [{"id": MODEL_ID, "object": "model", "owned_by": "mindroom-fuzz"}],
                },
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length))
        call_id = next(self.call_ids)
        source_marker, history_fingerprint = self._request_identity(payload)
        content = self.response_text_for(
            call_id,
            source_marker=source_marker,
            history_fingerprint=history_fingerprint,
        )
        if payload.get("stream") is True:
            self._send_stream(call_id, content)
            return
        self._send_json(
            {
                "id": f"live-fuzz-response-{call_id}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    },
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    @classmethod
    def response_text_for(
        cls,
        call_id: int,
        *,
        source_marker: str = "unbound",
        history_fingerprint: str | None = None,
    ) -> str:
        """Return the only complete body accepted for one model call."""
        fingerprint = history_fingerprint or _history_fingerprint(())
        segments = " ".join(f"segment-{index:03d}" for index in range(cls.stream_segments))
        return f"LIVE-FUZZ call={call_id} source={source_marker} history={fingerprint} {segments} END call={call_id}"

    @staticmethod
    def _request_identity(payload: Mapping[str, object]) -> tuple[str, str]:
        """Bind deterministic output to the latest source and ordered source history."""
        markers: list[str] = []

        def collect(value: object, message_markers: list[str]) -> None:
            if isinstance(value, str):
                message_markers.extend(SOURCE_MARKER_PATTERN.findall(value))
            elif isinstance(value, list):
                for item in value:
                    collect(item, message_markers)
            elif isinstance(value, dict):
                for item in value.values():
                    collect(item, message_markers)

        messages = payload.get("messages")
        if isinstance(messages, list):
            for message in messages:
                message_markers: list[str] = []
                collect(message, message_markers)
                markers.extend(dict.fromkeys(message_markers))
        else:
            collect(messages, markers)
        source_marker = markers[-1] if markers else "unbound"
        return source_marker, _history_fingerprint(markers)

    def _send_stream(self, call_id: int, content: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        base = {
            "id": f"live-fuzz-response-{call_id}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": MODEL_ID,
        }
        self._write_sse(
            {
                **base,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            },
        )
        words = content.split()
        for index in range(0, len(words), 2):
            chunk_text = " ".join(words[index : index + 2])
            if index + 2 < len(words):
                chunk_text += " "
            self._write_sse(
                {
                    **base,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": chunk_text},
                            "finish_reason": None,
                        },
                    ],
                },
            )
            time.sleep(self.stream_delay)
        self._write_sse(
            {
                **base,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        )
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def _write_sse(self, payload: Mapping[str, object]) -> None:
        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        self.wfile.flush()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ANN401
        """Keep hundreds of deterministic model calls out of test output."""


def _available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return cast("int", sock.getsockname()[1])


def _run_command(*command: str) -> str:
    result = subprocess.run(
        command,
        check=False,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        msg = f"command failed ({' '.join(command)}):\n{result.stdout}\n{result.stderr}"
        raise RuntimeError(msg)
    return result.stdout


def _git_state_for_file(
    path: Path,
    *,
    scopes: Collection[Path] = (),
) -> tuple[str | None, bool]:
    """Return the containing revision and whether relevant loaded source is dirty."""
    root_result = subprocess.run(
        ("git", "-C", str(path.parent), "rev-parse", "--show-toplevel"),
        check=False,
        capture_output=True,
        text=True,
    )
    if root_result.returncode:
        return None, False
    root = Path(root_result.stdout.strip()).resolve()
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(root):
        return None, False
    relative_path = resolved_path.relative_to(root)
    tracked_result = subprocess.run(
        ("git", "-C", str(root), "ls-files", "--error-unmatch", str(relative_path)),
        check=False,
        capture_output=True,
        text=True,
    )
    if tracked_result.returncode:
        return None, True
    relative_scopes = [
        str(scope.resolve().relative_to(root)) for scope in scopes or (path,) if scope.resolve().is_relative_to(root)
    ]
    status_result = subprocess.run(
        ("git", "-C", str(root), "status", "--short", "--untracked-files=all", "--", *relative_scopes),
        check=False,
        capture_output=True,
        text=True,
    )
    dirty = status_result.returncode != 0 or bool(status_result.stdout.strip())
    revision_result = subprocess.run(
        ("git", "-C", str(root), "rev-parse", "HEAD"),
        check=False,
        capture_output=True,
        text=True,
    )
    revision = revision_result.stdout.strip() if revision_result.returncode == 0 else None
    return revision, dirty


def _git_revision_for_file(path: Path) -> str | None:
    """Return HEAD only when the loaded file is tracked and clean."""
    revision, dirty = _git_state_for_file(path)
    return revision if not dirty else None


def _source_hash(path: Path) -> str:
    """Hash one loaded source file for wheel and checkout provenance."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_provenance(
    *,
    mindroom_module_path: Path | None = None,
    nio_module_path: Path | None = None,
) -> RuntimeProvenance:
    """Resolve exact loaded package paths and compare MindRoom to this runner."""
    loaded_mindroom_file = mindroom.__file__
    loaded_nio_file = nio.__file__
    if loaded_mindroom_file is None or loaded_nio_file is None:
        msg = "imported MindRoom and nio modules must have filesystem paths"
        raise RuntimeError(msg)
    mindroom_path = (mindroom_module_path or Path(loaded_mindroom_file)).resolve()
    nio_path = (nio_module_path or Path(loaded_nio_file)).resolve()
    nio_revision, nio_dirty = _git_state_for_file(
        nio_path,
        scopes=(nio_path.parent,),
    )
    mindroom_revision, mindroom_dirty = _git_state_for_file(
        mindroom_path,
        scopes=(mindroom_path.parent, Path(__file__)),
    )
    expected_mindroom_revision, runner_dirty = _git_state_for_file(
        Path(__file__),
        scopes=(PROJECT_ROOT / "src" / "mindroom", Path(__file__)),
    )
    if expected_mindroom_revision is None:
        msg = "could not resolve the MindRoom revision containing the live runner"
        raise RuntimeError(msg)
    return RuntimeProvenance(
        mindroom_module_path=str(mindroom_path),
        mindroom_revision=mindroom_revision or "unverified",
        mindroom_expected_revision=expected_mindroom_revision,
        nio_module_path=str(nio_path),
        nio_version=importlib.metadata.version("mindroom-nio"),
        nio_revision=nio_revision or "unverified",
        nio_expected_revision=os.getenv("MINDROOM_NIO_FUZZ_COMMIT", ""),
        mindroom_dirty=mindroom_dirty or runner_dirty,
        nio_dirty=nio_dirty,
        nio_source_hash=_source_hash(
            nio_path.parent / "client" / "async_client.py"
            if (nio_path.parent / "client" / "async_client.py").exists()
            else nio_path,
        ),
    )


def _validate_nio_provenance(provenance: RuntimeProvenance) -> None:
    """Fail closed unless child-attested MindRoom and nio match exact revisions."""
    if provenance.mindroom_dirty:
        msg = f"live fuzz exact provenance requires clean loaded source: mindroom_dirty={provenance.mindroom_dirty}"
        raise RuntimeError(msg)
    if provenance.mindroom_revision == "unverified":
        msg = f"live fuzz exact provenance could not verify loaded MindRoom at {provenance.mindroom_module_path}"
        raise RuntimeError(msg)
    if provenance.mindroom_revision != provenance.mindroom_expected_revision:
        msg = (
            "loaded MindRoom revision does not match live runner: "
            f"expected {provenance.mindroom_expected_revision}, loaded {provenance.mindroom_revision} "
            f"from {provenance.mindroom_module_path}"
        )
        raise RuntimeError(msg)
    expected = provenance.nio_expected_revision
    if not expected:
        return
    if provenance.nio_dirty:
        msg = "live fuzz exact provenance requires clean loaded nio source: nio_dirty=True"
        raise RuntimeError(msg)
    if provenance.nio_revision == "unverified":
        msg = f"live fuzz exact provenance could not verify loaded mindroom-nio at {provenance.nio_module_path}"
        raise RuntimeError(msg)
    if expected != provenance.nio_revision:
        msg = (
            "loaded mindroom-nio revision does not match campaign requirement: "
            f"expected {expected}, loaded {provenance.nio_revision} from {provenance.nio_module_path}"
        )
        raise RuntimeError(msg)


def _read_instance_registry() -> dict[str, Any]:
    """Read the non-atomic deploy registry without trusting a partial write."""
    for attempt in range(REGISTRY_READ_ATTEMPTS):
        if not INSTANCE_REGISTRY.exists():
            return {}
        try:
            registry = json.loads(INSTANCE_REGISTRY.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            if attempt == REGISTRY_READ_ATTEMPTS - 1:
                msg = "instance registry remained unreadable; refusing to start a fuzz stack"
                raise RuntimeError(msg) from error
            time.sleep(REGISTRY_READ_RETRY_SECONDS)
            continue
        if not isinstance(registry, dict):
            msg = "instance registry must contain a JSON object"
            raise TypeError(msg)
        return registry
    msg = "unreachable registry retry state"
    raise AssertionError(msg)


def _active_fuzz_instances() -> tuple[str, ...]:
    """Probe the registry and return fuzz stacks whose real homeserver is alive."""
    registry = _read_instance_registry()
    instances = registry.get("instances", {})
    if not isinstance(instances, dict):
        return ()
    active: list[str] = []
    for name, value in instances.items():
        if not isinstance(name, str) or not name.startswith("fuzz") or not isinstance(value, dict):
            continue
        matrix_port = value.get("matrix_port")
        if not isinstance(matrix_port, int):
            continue
        try:
            response = httpx.get(
                f"http://127.0.0.1:{matrix_port}/_matrix/client/versions",
                timeout=0.5,
            )
        except httpx.HTTPError:
            continue
        if response.is_success:
            active.append(name)
    return tuple(sorted(active))


class ManagedTuwunelStack:
    """Disposable Tuwunel plus the current worktree's MindRoom runtime."""

    def __init__(
        self,
        *,
        room_count: int = 1,
        stream_segments: int = 4,
        stream_delay: float = 0.001,
    ) -> None:
        token = secrets.token_hex(4)
        self.instance_name = f"fuzz{token}"
        self.namespace = self.instance_name
        self.temp_dir = tempfile.TemporaryDirectory(prefix="mindroom-live-matrix-fuzz-")
        self.root = Path(self.temp_dir.name)
        self.storage_path = self.root / "mindroom_data"
        self.config_path = self.root / "config.yaml"
        self.log_path = self.root / "mindroom.log"
        self.attestation_path = self.root / "runtime-attestation.json"
        self.runtime_provenance: RuntimeProvenance | None = None
        self.api_port = _available_port()
        self.homeserver = ""
        self.server_name = ""
        self.room_id = ""
        self.room_ids: tuple[str, ...] = ()
        self.room_keys = tuple(ROOM_KEY if room == 0 else f"fuzz_room_{room}" for room in range(room_count))
        self.agent_id = ""
        self.router_id = ""
        self.preexisting_fuzz_servers = 0
        self._created = False
        self._model_server: ThreadingHTTPServer | None = None
        self._model_thread: threading.Thread | None = None
        self._mindroom_process: subprocess.Popen[str] | None = None
        self._log_handle: TextIOWrapper | None = None
        self._env: dict[str, str] = {}
        self._stream_segments = stream_segments
        self._stream_delay = stream_delay

    def start(self) -> None:
        """Create every live dependency and wait for the managed room."""
        active_instances = _active_fuzz_instances()
        self.preexisting_fuzz_servers = len(active_instances)
        if active_instances:
            msg = f"live fuzz server already active: {', '.join(active_instances)}"
            raise RuntimeError(msg)
        _run_command("just", "local-instances-create", self.instance_name, "tuwunel")
        self._created = True
        registry = _read_instance_registry()
        instance = registry["instances"][self.instance_name]
        matrix_port = int(instance["matrix_port"])
        domain = str(instance["domain"])
        self.homeserver = f"http://127.0.0.1:{matrix_port}"
        self.server_name = f"m-{domain}"
        self.agent_id = f"@mindroom_{AGENT_NAME}_{self.namespace}:{self.server_name}"
        self.router_id = f"@mindroom_router_{self.namespace}:{self.server_name}"

        _run_command("just", "local-instances-start-matrix", self.instance_name)
        self._wait_for_url(f"{self.homeserver}/_matrix/client/versions", timeout=30)
        model_port = self._start_model_server()
        self._write_config(model_port)
        self._env = {
            **os.environ,
            "MATRIX_HOMESERVER": self.homeserver,
            "MATRIX_SERVER_NAME": self.server_name,
            "MATRIX_SSL_VERIFY": "false",
            "MINDROOM_CONFIG_PATH": str(self.config_path),
            "MINDROOM_NAMESPACE": self.namespace,
            "MINDROOM_STORAGE_PATH": str(self.storage_path),
            "MINDROOM_LOG_LEVEL": "INFO",
            "OPENAI_API_KEY": "sk-live-fuzz",
            "UV_PYTHON": "3.13",
        }
        self._log_handle = self.log_path.open("a", encoding="utf-8")
        self._start_mindroom()

    def restart_mindroom(self) -> None:
        """Restart only MindRoom while preserving its cache and Matrix account."""
        self._stop_mindroom()
        self._start_mindroom()

    def stop_mindroom(self) -> None:
        """Stop MindRoom while leaving the isolated homeserver writable."""
        self._stop_mindroom()

    def resume_mindroom(self) -> None:
        """Resume MindRoom against its durable cache and saved sync token."""
        self._start_mindroom()

    def close(self) -> None:
        """Stop child processes and delete the exact disposable instance."""
        self._stop_mindroom()
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        if self._model_server is not None:
            self._model_server.shutdown()
            self._model_server.server_close()
            self._model_server = None
        if self._model_thread is not None:
            self._model_thread.join(timeout=5)
            self._model_thread = None
        if self._created:
            _run_command("just", "local-instances-remove", self.instance_name)
            self._created = False
        self.temp_dir.cleanup()

    def log_tail(self, lines: int = 80) -> str:
        """Return recent MindRoom output when a live invariant fails."""
        if not self.log_path.exists():
            return ""
        return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])

    def diagnostic_counts(self) -> dict[str, int]:
        """Count saturation signals in the complete runtime output."""
        if not self.log_path.exists():
            return {}
        log = self.log_path.read_text(encoding="utf-8", errors="replace")
        lines = log.splitlines()
        return {
            "cache_coordinator_timeouts": log.count("thread_read_error=cache_coordinator_timeout"),
            "degraded_thread_reads": log.count("matrix_cache_thread_read_degraded"),
            "dispatch_read_timeouts": log.count("thread_read_error=dispatch_read_timeout"),
            "event_loop_stalls": log.count("event_loop_stall_detected"),
            "limited_sync_backfill_warnings": log.count("limited-timeline backfill"),
            "limited_sync_certification_events": sum(
                "matrix_sync_certification_uncertain" in line and "limited_sync_timeline" in line for line in lines
            ),
        }

    def sync_checkpoint_state(self, agent_name: str) -> _SyncCheckpointState:
        """Return one bot's durable token and checkpoint-file version."""
        token_path = self.storage_path / "sync_tokens" / f"{agent_name}.token"
        checkpoint = load_sync_checkpoint(self.storage_path, agent_name)
        try:
            mtime_ns = token_path.stat().st_mtime_ns
        except FileNotFoundError:
            mtime_ns = None
        return _SyncCheckpointState(
            token=checkpoint.token if checkpoint is not None else None,
            mtime_ns=mtime_ns,
        )

    async def wait_for_sync_checkpoint_advance(
        self,
        agent_name: str,
        previous_state: _SyncCheckpointState,
        *,
        deadline_seconds: float,
    ) -> _SyncCheckpointState:
        """Wait for a durable checkpoint write after a completed barrier."""
        deadline = time.monotonic() + deadline_seconds
        while time.monotonic() < deadline:
            process = self._mindroom_process
            if process is not None and process.poll() is not None:
                msg = f"MindRoom exited while waiting for {agent_name} sync checkpoint:\n{self.log_tail()}"
                raise RuntimeError(msg)
            state = self.sync_checkpoint_state(agent_name)
            if state.token is not None and state != previous_state:
                return state
            await asyncio.sleep(0.05)
        msg = f"timed out waiting for {agent_name} durable sync checkpoint to advance"
        raise TimeoutError(msg)

    def _start_model_server(self) -> int:
        _ModelHandler.stream_segments = self._stream_segments
        _ModelHandler.stream_delay = self._stream_delay
        self._model_server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelHandler)
        port = self._model_server.server_address[1]
        self._model_thread = threading.Thread(
            target=self._model_server.serve_forever,
            name="mindroom-live-fuzz-model",
            daemon=True,
        )
        self._model_thread.start()
        return port

    def _write_config(self, model_port: int) -> None:
        config = {
            "models": {
                "default": {
                    "provider": "openai",
                    "id": MODEL_ID,
                    "extra_kwargs": {"base_url": f"http://127.0.0.1:{model_port}/v1"},
                },
            },
            "agents": {
                AGENT_NAME: {
                    "display_name": "Live Fuzz Agent",
                    "role": "Return a deterministic acknowledgement.",
                    "model": "default",
                    "tools": [],
                    "rooms": list(self.room_keys),
                    "learning": False,
                },
            },
            "defaults": {"tools": [], "enable_streaming": True, "markdown": False},
            "memory": {"backend": "file"},
            "router": {"model": "default"},
            "mindroom_user": {"username": "livefuzzowner", "display_name": "Live Fuzz Owner"},
            "matrix_room_access": {
                "mode": "multi_user",
                "multi_user_join_rule": "public",
                "publish_to_room_directory": False,
                "invite_only_rooms": [],
                "reconcile_existing_rooms": False,
            },
            "authorization": {
                "default_room_access": True,
                "global_users": [],
                "agent_reply_permissions": {},
            },
        }
        self.config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    def _start_mindroom(self) -> None:
        assert self._log_handle is not None
        self.attestation_path.unlink(missing_ok=True)
        self._mindroom_process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "__mindroom_runtime_child__",
                str(self.attestation_path),
                "run",
                "--api-port",
                str(self.api_port),
                "--log-level",
                "INFO",
            ],
            cwd=PROJECT_ROOT,
            env=self._env,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._wait_for_runtime_attestation()
        self._wait_for_url(f"http://127.0.0.1:{self.api_port}/api/health", timeout=60)
        state_path = self.storage_path / "matrix_state.yaml"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self._mindroom_process.poll() is not None:
                msg = f"MindRoom exited during startup:\n{self.log_tail()}"
                raise RuntimeError(msg)
            if state_path.exists():
                state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
                state_rooms = state.get("rooms", {}) if isinstance(state, dict) else {}
                room_ids = [
                    room.get("room_id")
                    for room_key in self.room_keys
                    if isinstance(state_rooms, dict)
                    and isinstance((room := state_rooms.get(room_key)), dict)
                    and isinstance(room.get("room_id"), str)
                ]
                if len(room_ids) == len(self.room_keys):
                    self.room_ids = tuple(cast("list[str]", room_ids))
                    self.room_id = self.room_ids[0]
                    return
            time.sleep(0.2)
        msg = f"MindRoom did not create rooms {self.room_keys!r}:\n{self.log_tail()}"
        raise TimeoutError(msg)

    def runtime_module_paths(self) -> tuple[Path, Path]:
        """Return child-attested package paths."""
        payload = json.loads(self.attestation_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            msg = "MindRoom runtime attestation must be a JSON object"
            raise TypeError(msg)
        mindroom_path = payload.get("mindroom_module_path")
        nio_path = payload.get("nio_module_path")
        if not isinstance(mindroom_path, str) or not isinstance(nio_path, str):
            msg = "MindRoom runtime attestation omitted package paths"
            raise TypeError(msg)
        return Path(mindroom_path), Path(nio_path)

    def _wait_for_runtime_attestation(self) -> None:
        """Wait for child import attestation before accepting health."""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.attestation_path.exists():
                mindroom_path, nio_path = self.runtime_module_paths()
                self.runtime_provenance = _runtime_provenance(
                    mindroom_module_path=mindroom_path,
                    nio_module_path=nio_path,
                )
                return
            if self._mindroom_process is not None and self._mindroom_process.poll() is not None:
                msg = f"MindRoom exited before runtime attestation:\n{self.log_tail()}"
                raise RuntimeError(msg)
            time.sleep(0.05)
        msg = "MindRoom child did not attest loaded runtime paths"
        raise TimeoutError(msg)

    def _stop_mindroom(self) -> None:
        process = self._mindroom_process
        if process is None:
            return
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        self._mindroom_process = None

    @staticmethod
    def _wait_for_url(url: str, *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = httpx.get(url, timeout=1)
                if response.is_success:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        msg = f"timed out waiting for {url}"
        raise TimeoutError(msg)


@dataclass(frozen=True, slots=True)
class _SentPayload:
    event_type: str
    txn_id: str
    content: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _ExpectedSaturationReply:
    root_event_id: str
    source_marker: str
    history_fingerprint: str


class LiveMatrixClient:
    """Minimal real Matrix client used by the live fuzzer."""

    def __init__(
        self,
        homeserver: str,
        room_id: str,
        *,
        room_slot: int = 0,
        client_slot: int = 0,
    ) -> None:
        self.homeserver = homeserver.rstrip("/")
        self.room_id = room_id
        self.room_slot = room_slot
        self.client_slot = client_slot
        self.http = httpx.AsyncClient(timeout=30)
        self.access_token = ""
        self.next_batch: str | None = None
        self.seen_events: dict[str, dict[str, Any]] = {}
        self.pagination_page_count = 0

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self.http.aclose()

    async def register(self) -> str:
        """Register one disposable account without exposing its token."""
        username = f"livefuzz{secrets.token_hex(6)}"
        password = secrets.token_urlsafe(24)
        payload: dict[str, Any] = {
            "auth": {"type": "m.login.dummy"},
            "username": username,
            "password": password,
        }
        response = await self.http.post(f"{self.homeserver}/_matrix/client/v3/register", json=payload)
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            session = response.json().get("session")
            if isinstance(session, str):
                payload["auth"]["session"] = session
                response = await self.http.post(
                    f"{self.homeserver}/_matrix/client/v3/register",
                    json=payload,
                )
        response.raise_for_status()
        data = response.json()
        token = data.get("access_token")
        user_id = data.get("user_id")
        if not isinstance(token, str) or not isinstance(user_id, str):
            msg = "Matrix registration omitted access_token or user_id"
            raise TypeError(msg)
        self.access_token = token
        return user_id

    async def join_room(self) -> None:
        """Join the managed public room."""
        room_id = quote(self.room_id, safe="")
        await self._request("POST", f"/_matrix/client/v3/join/{room_id}", json_body={})

    async def send_event(
        self,
        event_type: str,
        txn_id: str,
        content: Mapping[str, Any],
    ) -> str:
        """Send one event with a caller-stable transaction ID."""
        room_id = quote(self.room_id, safe="")
        encoded_type = quote(event_type, safe="")
        encoded_txn = quote(txn_id, safe="")
        data = await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{room_id}/send/{encoded_type}/{encoded_txn}",
            json_body=content,
        )
        event_id = data.get("event_id")
        if not isinstance(event_id, str):
            msg = f"Matrix send omitted event_id: {data}"
            raise TypeError(msg)
        return event_id

    async def redact(self, target_event_id: str, txn_id: str) -> str:
        """Redact one event authored by the disposable account."""
        room_id = quote(self.room_id, safe="")
        event_id = quote(target_event_id, safe="")
        encoded_txn = quote(txn_id, safe="")
        data = await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{room_id}/redact/{event_id}/{encoded_txn}",
            json_body={"reason": "live cache fuzz"},
        )
        redaction_id = data.get("event_id")
        if not isinstance(redaction_id, str):
            msg = f"Matrix redaction omitted event_id: {data}"
            raise TypeError(msg)
        return redaction_id

    async def sync(
        self,
        since: str | None,
        *,
        timeout_ms: int,
        timeline_limit: int = 2000,
    ) -> dict[str, Any]:
        """Read one incremental sync window from the real homeserver."""
        params: dict[str, str | int] = {
            "timeout": timeout_ms,
            "filter": json.dumps({"room": {"timeline": {"limit": timeline_limit}}}),
        }
        if since is not None:
            params["since"] = since
        return await self._request("GET", "/_matrix/client/v3/sync", params=params)

    async def sync_incremental(
        self,
        *,
        timeout_ms: int,
        allow_limited: bool = False,
    ) -> None:
        """Advance this client's private sync cursor and retain room events."""
        since = self.next_batch
        data = await self.sync(since, timeout_ms=timeout_ms)
        next_batch = data.get("next_batch")
        if not isinstance(next_batch, str):
            msg = "Matrix sync omitted next_batch"
            raise TypeError(msg)
        joined = data.get("rooms", {}).get("join", {})
        room = joined.get(self.room_id, {}) if isinstance(joined, dict) else {}
        timeline = room.get("timeline", {}) if isinstance(room, dict) else {}
        if timeline.get("limited") is True and not allow_limited:
            msg = "incremental Matrix fuzz sync unexpectedly returned a limited timeline"
            raise AssertionError(msg)
        if timeline.get("limited") is True and since is not None:
            await self._hydrate_limited_gap(timeline, since)
        events = timeline.get("events", [])
        if not isinstance(events, list):
            msg = "Matrix sync room timeline events must be a list"
            raise TypeError(msg)
        self._ingest_events(events)
        self.next_batch = next_batch

    def _ingest_events(self, events: Collection[object]) -> None:
        """Retain event-id-addressable timeline events."""
        for raw_event in events:
            if not isinstance(raw_event, dict):
                continue
            event = cast("dict[str, Any]", raw_event)
            event_id = event.get("event_id")
            if isinstance(event_id, str):
                self.seen_events[event_id] = event

    async def _hydrate_limited_gap(
        self,
        timeline: Mapping[str, Any],
        since: str,
    ) -> None:
        """Hydrate every page hidden between an incremental cursor and limited tail."""
        token = timeline.get("prev_batch")
        if not isinstance(token, str):
            msg = "limited Matrix timeline omitted prev_batch"
            raise TypeError(msg)
        seen_tokens: set[str] = set()
        for _ in range(20):
            if token in seen_tokens:
                msg = f"bounded Matrix pagination repeated token {token!r}"
                raise AssertionError(msg)
            seen_tokens.add(token)
            events, next_token = await self.messages_before(token, to_token=since)
            self.pagination_page_count += 1
            self._ingest_events(events)
            if next_token is None:
                return
            token = next_token
        msg = "bounded Matrix client pagination exceeded 20 pages"
        raise AssertionError(msg)

    async def messages_before(
        self,
        from_token: str,
        *,
        to_token: str | None = None,
        limit: int = 1000,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Read one real backward pagination page for an independent oracle."""
        room_id = quote(self.room_id, safe="")
        params: dict[str, str | int] = {
            "dir": "b",
            "from": from_token,
            "limit": limit,
        }
        if to_token is not None:
            params["to"] = to_token
        data = await self._request(
            "GET",
            f"/_matrix/client/v3/rooms/{room_id}/messages",
            params=params,
        )
        raw_chunk = data.get("chunk", [])
        if not isinstance(raw_chunk, list):
            msg = "Matrix room messages chunk must be a list"
            raise TypeError(msg)
        chunk = [cast("dict[str, Any]", event) for event in raw_chunk if isinstance(event, dict)]
        end = data.get("end")
        if end is not None and not isinstance(end, str):
            msg = "Matrix room messages end token must be a string or null"
            raise TypeError(msg)
        return chunk, end

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str | int] | None = None,
    ) -> dict[str, Any]:
        response = await self.http.request(
            method,
            f"{self.homeserver}{path}",
            headers={"Authorization": f"Bearer {self.access_token}"},
            json=json_body,
            params=params,
        )
        if response.is_error:
            msg = f"Matrix {method} {path} failed with HTTP {response.status_code}: {response.text}"
            raise RuntimeError(msg)
        data = response.json()
        if not isinstance(data, dict):
            msg = f"Matrix {method} {path} returned non-object JSON"
            raise TypeError(msg)
        return data


class ExactReplyOracle:
    """Track canonical agent replies from real incremental `/sync` responses."""

    def __init__(
        self,
        client: LiveMatrixClient,
        agent_id: str,
    ) -> None:
        self.client = client
        self.agent_id = agent_id
        self.next_batch: str | None = None
        self.expected_sources: dict[str, str] = {}
        self.expected_thread_roots: dict[str, str] = {}
        self.expected_model_identity: dict[str, tuple[str, str]] = {}
        self.response_ids: dict[str, set[str]] = defaultdict(set)
        self.wrong_thread_roots: dict[str, set[tuple[str, str | None]]] = defaultdict(set)
        self.malformed_response_ids: set[str] = set()
        self.response_event_by_ref: dict[str, str] = {}
        self.latest_reply_bodies: dict[str, tuple[int, str, str]] = {}
        self.reply_events_with_edits: set[str] = set()
        self.response_edit_targets: dict[str, str] = {}
        self.seen_event_ids: set[str] = set()
        self.limited_timeline_count = 0
        self.pagination_page_count = 0
        self.gap_audit_page_count = 0
        self.gap_audit_missing_sources: set[str] = set()
        self._gap_audit_sources: set[str] | None = None
        self._gap_audit_completed = False
        self._last_response_at = time.monotonic()

    async def initialize(self) -> None:
        """Establish a sync token before the fuzz traffic starts."""
        await self._sync_once(timeout_ms=0, allow_limited=True)

    def expect(
        self,
        logical_ref: str,
        event_id: str,
        *,
        root_event_id: str | None = None,
        source_marker: str | None = None,
        history_markers: Collection[str] = (),
    ) -> None:
        """Require exactly one canonical agent reply to a source event."""
        self.expected_sources[event_id] = logical_ref
        self.expected_thread_roots[event_id] = root_event_id or event_id
        if source_marker is not None:
            self.expected_model_identity[event_id] = (
                source_marker,
                _history_fingerprint(history_markers),
            )
        if self._gap_audit_sources is not None and not self._gap_audit_completed:
            self._gap_audit_sources.add(event_id)

    def arm_gap_audit(self) -> None:
        """Scope the next limited-gap audit to subsequently expected sources."""
        if self.next_batch is None:
            msg = "limited-gap audit requires an established sync token"
            raise RuntimeError(msg)
        self._gap_audit_sources = set()
        self.gap_audit_missing_sources.clear()
        self._gap_audit_completed = False

    async def audit_armed_limited_gap(self) -> None:
        """Sync the offline burst and require the armed limited-gap audit to run."""
        if self._gap_audit_sources is None:
            msg = "limited-gap audit is not armed"
            raise RuntimeError(msg)
        await self._sync_once(
            timeout_ms=0,
            allow_limited=True,
            timeline_limit=RECOVERY_TIMELINE_LIMIT,
        )
        if not self._gap_audit_completed:
            msg = "recovery source burst did not produce a limited observer timeline"
            raise AssertionError(msg)

    async def wait_until_exact(
        self,
        *,
        deadline_seconds: float,
        settle_seconds: float,
        allow_limited: bool = False,
    ) -> None:
        """Wait until all sources have one reply and the room stays quiet."""
        deadline = time.monotonic() + deadline_seconds
        settled_after = time.monotonic() + settle_seconds
        while time.monotonic() < deadline:
            await self._sync_once(timeout_ms=250, allow_limited=allow_limited)
            self._assert_no_wrong_replies()
            incomplete_streams = self._incomplete_streaming_sources()
            gap_complete = self._gap_audit_sources is None or (
                self._gap_audit_completed and not self.gap_audit_missing_sources
            )
            if (
                all(len(self.response_ids[source]) == 1 for source in self.expected_sources)
                and not incomplete_streams
                and gap_complete
            ):
                settled_after = max(settled_after, self._last_response_at + settle_seconds)
                if time.monotonic() >= settled_after:
                    return
        missing = {
            logical_ref: {
                "event_id": event_id,
                "reply_count": len(self.response_ids[event_id]),
            }
            for event_id, logical_ref in self.expected_sources.items()
            if len(self.response_ids[event_id]) != 1 or event_id in incomplete_streams
        }
        missing_gap_refs = sorted(self.expected_sources[event_id] for event_id in self.gap_audit_missing_sources)
        msg = (
            f"timed out waiting for exact agent replies in room {self.client.room_slot}: "
            f"{missing}; bounded_gap_missing={missing_gap_refs}"
        )
        raise AssertionError(msg)

    def resolve_response_ref(self, response_ref: str) -> str:
        """Resolve a logical agent-response reference to its real event ID."""
        event_id = self.response_event_by_ref.get(response_ref)
        if event_id is None:
            msg = f"response event not observed for {response_ref!r}"
            raise KeyError(msg)
        return event_id

    async def _sync_once(
        self,
        *,
        timeout_ms: int,
        allow_limited: bool = False,
        timeline_limit: int = 2000,
    ) -> None:
        since = self.next_batch
        data = await self.client.sync(
            since,
            timeout_ms=timeout_ms,
            timeline_limit=timeline_limit,
        )
        next_batch = data.get("next_batch")
        if not isinstance(next_batch, str):
            msg = "Matrix sync omitted next_batch"
            raise TypeError(msg)
        self.next_batch = next_batch
        joined = data.get("rooms", {}).get("join", {})
        room = joined.get(self.client.room_id, {}) if isinstance(joined, dict) else {}
        timeline = room.get("timeline", {}) if isinstance(room, dict) else {}
        if timeline.get("limited") is True and not allow_limited:
            msg = "live fuzz oracle received a limited timeline; reduce batch size"
            raise AssertionError(msg)
        if timeline.get("limited") is True:
            self.limited_timeline_count += 1
            if since is not None:
                await self._ingest_limited_gap(timeline, since)
        events = timeline.get("events", [])
        if not isinstance(events, list):
            return
        for raw_event in events:
            if isinstance(raw_event, dict):
                self._ingest_event(raw_event)

    async def _ingest_limited_gap(
        self,
        timeline: Mapping[str, Any],
        since: str,
    ) -> None:
        """Ingest only the bounded incremental gap hidden by a limited sync."""
        prev_batch = timeline.get("prev_batch")
        if not isinstance(prev_batch, str):
            msg = "limited Matrix timeline omitted prev_batch"
            raise TypeError(msg)
        timeline_events = timeline.get("events", [])
        observed_ids = {
            event_id
            for event in timeline_events
            if isinstance(event, dict) and isinstance((event_id := event.get("event_id")), str)
        }
        token = prev_batch
        seen_tokens: set[str] = set()
        for _ in range(20):
            if token in seen_tokens:
                msg = f"bounded Matrix pagination repeated token {token!r}"
                raise AssertionError(msg)
            seen_tokens.add(token)
            events, next_token = await self.client.messages_before(
                token,
                to_token=since,
            )
            self.pagination_page_count += 1
            if self._gap_audit_sources is not None and not self._gap_audit_completed:
                self.gap_audit_page_count += 1
            observed_ids.update(event_id for event in events if isinstance((event_id := event.get("event_id")), str))
            for event in reversed(events):
                self._ingest_event(event)
            if next_token is None:
                break
            token = next_token
        else:
            msg = "bounded Matrix oracle pagination exceeded 20 pages"
            raise AssertionError(msg)
        if self._gap_audit_sources is not None and not self._gap_audit_completed:
            self.gap_audit_missing_sources.update(self._gap_audit_sources - observed_ids)
            self._gap_audit_completed = True

    def _ingest_event(self, event: Mapping[str, Any]) -> None:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in self.seen_event_ids:
            return
        self.seen_event_ids.add(event_id)
        if event.get("sender") != self.agent_id or event.get("type") != "m.room.message":
            return
        content = event.get("content")
        if not isinstance(content, dict):
            self.malformed_response_ids.add(event_id)
            return
        relation = content.get("m.relates_to")
        if isinstance(relation, dict) and relation.get("rel_type") == "m.replace":
            self._ingest_agent_edit(event_id, event, content, relation)
            return
        self._track_reply_body(event_id, event, content, relation)
        if not isinstance(relation, dict) or relation.get("rel_type") != "m.thread":
            self.malformed_response_ids.add(event_id)
            return
        reply = relation.get("m.in_reply_to")
        source_event_id = reply.get("event_id") if isinstance(reply, dict) else None
        if not isinstance(source_event_id, str):
            self.malformed_response_ids.add(event_id)
            return
        expected_root = self.expected_thread_roots.get(source_event_id)
        observed_root = relation.get("event_id")
        if expected_root is not None and observed_root != expected_root:
            self.wrong_thread_roots[source_event_id].add(
                (event_id, observed_root if isinstance(observed_root, str) else None),
            )
        self.response_ids[source_event_id].add(event_id)
        logical_ref = self.expected_sources.get(source_event_id)
        if logical_ref is not None:
            self.response_event_by_ref[f"response:{logical_ref}"] = event_id
        self._last_response_at = time.monotonic()

    def _ingest_agent_edit(
        self,
        event_id: str,
        event: Mapping[str, Any],
        content: Mapping[str, Any],
        relation: Mapping[str, Any],
    ) -> None:
        """Track one well-formed replacement for later target validation."""
        target_event_id = relation.get("event_id")
        new_content = content.get("m.new_content")
        if (
            not isinstance(target_event_id, str)
            or not target_event_id
            or not isinstance(new_content, dict)
            or not isinstance(new_content.get("body"), str)
            or not isinstance(new_content.get("msgtype"), str)
        ):
            self.malformed_response_ids.add(event_id)
            return
        self.response_edit_targets[event_id] = target_event_id
        self._track_reply_body(event_id, event, content, relation)

    def _track_reply_body(
        self,
        event_id: str,
        event: Mapping[str, Any],
        content: Mapping[str, Any],
        relation: object,
    ) -> None:
        """Fold originals and `m.replace` edits into one visible reply body."""
        edit_relation = cast("dict[str, object]", relation) if isinstance(relation, dict) else None
        is_edit = edit_relation is not None and edit_relation.get("rel_type") == "m.replace"
        response_event_id = edit_relation.get("event_id") if is_edit and edit_relation is not None else event_id
        if not isinstance(response_event_id, str):
            return
        new_content = content.get("m.new_content")
        body_source = new_content if isinstance(new_content, dict) else content
        body = body_source.get("body")
        if not isinstance(body, str):
            return
        raw_timestamp = event.get("origin_server_ts")
        timestamp = raw_timestamp if isinstance(raw_timestamp, int) else 0
        candidate = (timestamp, event_id, body)
        current = self.latest_reply_bodies.get(response_event_id)
        if is_edit:
            if response_event_id not in self.reply_events_with_edits or current is None or candidate[:2] >= current[:2]:
                self.latest_reply_bodies[response_event_id] = candidate
            self.reply_events_with_edits.add(response_event_id)
        elif response_event_id not in self.reply_events_with_edits and (
            current is None or candidate[:2] >= current[:2]
        ):
            self.latest_reply_bodies[response_event_id] = candidate
        self._last_response_at = time.monotonic()

    def _incomplete_streaming_sources(self) -> set[str]:
        """Return sources whose one reply is still a placeholder or partial edit."""
        incomplete: set[str] = set()
        for source_event_id in self.expected_sources:
            response_event_ids = self.response_ids[source_event_id]
            if len(response_event_ids) != 1:
                continue
            response_event_id = next(iter(response_event_ids))
            latest = self.latest_reply_bodies.get(response_event_id)
            expected_identity = self.expected_model_identity.get(source_event_id)
            if latest is None or not self._is_complete_model_body(
                latest[2],
                expected_source_marker=expected_identity[0] if expected_identity is not None else None,
                expected_history_fingerprint=expected_identity[1] if expected_identity is not None else None,
            ):
                incomplete.add(source_event_id)
        return incomplete

    @staticmethod
    def _is_complete_model_body(
        body: str,
        *,
        expected_source_marker: str | None = None,
        expected_history_fingerprint: str | None = None,
    ) -> bool:
        match = re.fullmatch(
            r"LIVE-FUZZ call=(\d+) source=([A-Za-z0-9_.:-]+) history=([0-9a-f]{16}) .* END call=(\d+)",
            body,
        )
        if match is None or match.group(1) != match.group(4):
            return False
        source_marker = match.group(2)
        history_fingerprint = match.group(3)
        if expected_source_marker is not None and source_marker != expected_source_marker:
            return False
        if expected_history_fingerprint is not None and history_fingerprint != expected_history_fingerprint:
            return False
        return body == _ModelHandler.response_text_for(
            int(match.group(1)),
            source_marker=source_marker,
            history_fingerprint=history_fingerprint,
        )

    def _assert_no_wrong_replies(self) -> None:
        duplicates = {
            self.expected_sources.get(source, source): sorted(event_ids)
            for source, event_ids in self.response_ids.items()
            if len(event_ids) > 1
        }
        unexpected = {
            source: sorted(event_ids)
            for source, event_ids in self.response_ids.items()
            if source not in self.expected_sources
        }
        wrong_roots = {
            self.expected_sources.get(source, source): sorted(values)
            for source, values in self.wrong_thread_roots.items()
        }
        canonical_response_ids = set(self.response_event_by_ref.values())
        orphan_edits = {
            event_id: target_event_id
            for event_id, target_event_id in self.response_edit_targets.items()
            if target_event_id not in canonical_response_ids
        }
        if duplicates or unexpected or wrong_roots or orphan_edits or self.malformed_response_ids:
            msg = (
                "agent reply invariant failed: "
                f"duplicates={duplicates}, unexpected={unexpected}, wrong_thread_roots={wrong_roots}, "
                f"orphan_edits={orphan_edits}, malformed={sorted(self.malformed_response_ids)}"
            )
            raise AssertionError(msg)


class LiveFuzzRunner:
    """Translate logical operations into concurrent real Matrix writes."""

    def __init__(
        self,
        stack: ManagedTuwunelStack,
        clients: tuple[LiveMatrixClient, ...],
        scenario: LiveFuzzScenario,
        *,
        reply_timeout: float,
        settle_seconds: float,
    ) -> None:
        self.stack = stack
        self.clients = clients
        self.client = clients[0]
        self.scenario = scenario
        self.reply_timeout = reply_timeout
        self.settle_seconds = settle_seconds
        self.oracle = ExactReplyOracle(self.client, stack.agent_id)
        self.event_ids: dict[str, str] = {}
        self.response_event_ids: dict[str, str] = {}
        self.sent_payloads: dict[str, _SentPayload] = {}
        self.operation_count = 0
        self.restart_count = 0
        self.executed_batches = 0
        self.redaction_history_audits = 0
        self._history_markers: dict[tuple[int, int], list[str]] = defaultdict(list)
        self._latest_source_ref: dict[tuple[int, int, int], str] = {}
        self._source_revisions: dict[str, list[tuple[str, str]]] = {}
        self._source_lanes: dict[str, tuple[int, int]] = {}
        self._edit_sources: dict[str, str] = {}
        self._redacted_source_refs: set[str] = set()

    def _expect_source(
        self,
        oracle: ExactReplyOracle,
        logical_ref: str,
        event_id: str,
        *,
        root_event_id: str,
        room: int,
        thread: int,
        client: int = 0,
        source_content: Mapping[str, object],
    ) -> None:
        """Register exact reply, thread, source, and history expectations together."""
        lane = (room, thread)
        self._history_markers[lane].append(logical_ref)
        source_identity = _source_marker_from_content(source_content)
        self._source_revisions[logical_ref] = [(logical_ref, source_identity)]
        self._source_lanes[logical_ref] = lane
        self._latest_source_ref[(room, client, thread)] = logical_ref
        oracle.expect(
            logical_ref,
            event_id,
            root_event_id=root_event_id,
            source_marker=source_identity,
            history_markers=self._history_identities(lane),
        )

    def _history_identities(self, lane: tuple[int, int]) -> tuple[str, ...]:
        """Return ordered canonical identities still visible in one lane."""
        return tuple(
            self._source_revisions[source_ref][-1][1]
            for source_ref in self._history_markers[lane]
            if source_ref not in self._redacted_source_refs
        )

    def _refresh_source_expectation(self, source_ref: str) -> None:
        """Require regeneration against the source's current visible revision."""
        source_event_id = self.event_ids[source_ref]
        lane = self._source_lanes[source_ref]
        self.oracle.expected_model_identity[source_event_id] = (
            self._source_revisions[source_ref][-1][1],
            _history_fingerprint(self._history_identities(lane)),
        )

    def _record_edit_revision(
        self,
        operation: LiveOperation,
        source_content: Mapping[str, object],
    ) -> None:
        """Advance one source's canonical revision and response expectation."""
        assert operation.target is not None
        source_ref = operation.target
        revisions = self._source_revisions.get(source_ref)
        if revisions is None:
            return
        revisions.append((operation.event_ref, _source_marker_from_content(source_content)))
        self._edit_sources[operation.event_ref] = source_ref
        self._refresh_source_expectation(source_ref)

    def _record_redaction(self, operation: LiveOperation) -> tuple[int, int] | None:
        """Apply a redaction and return the source lane needing a fresh audit."""
        assert operation.target is not None
        target_ref = operation.target
        if target_ref in self._source_revisions:
            self._redacted_source_refs.add(target_ref)
            return self._source_lanes[target_ref]
        source_ref = self._edit_sources.get(target_ref)
        if source_ref is None:
            return None
        revisions = self._source_revisions[source_ref]
        revisions[:] = [revision for revision in revisions if revision[0] != target_ref]
        self._refresh_source_expectation(source_ref)
        return None

    async def _send_redaction_history_audit(
        self,
        operation: LiveOperation,
        lane: tuple[int, int],
    ) -> None:
        """Send a new turn proving a source redaction changed visible history."""
        room, thread = lane
        logical_ref = f"redaction-audit:{operation.event_ref}"
        root_event_id = self.event_ids[f"root:{thread}"]
        latest_source_ref = self._latest_source_ref[(room, 0, thread)]
        reply_event_id = self.oracle.resolve_response_ref(f"response:{latest_source_ref}")
        content = self._message_content(
            f"Live fuzz redaction audit {operation.operation_id}",
            relation={
                "rel_type": "m.thread",
                "event_id": root_event_id,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": reply_event_id},
            },
            source_marker=logical_ref,
        )
        event_id = await self._client_for_thread(thread).send_event(
            "m.room.message",
            f"live-fuzz-{logical_ref}",
            content,
        )
        self.event_ids[logical_ref] = event_id
        self._expect_source(
            self.oracle,
            logical_ref,
            event_id,
            root_event_id=root_event_id,
            room=room,
            thread=thread,
            source_content=content,
        )
        self.redaction_history_audits += 1

    async def _record_redaction_and_audit(self, operation: LiveOperation) -> None:
        """Apply one redaction transition and send its required history audit."""
        if lane := self._record_redaction(operation):
            await self._send_redaction_history_audit(operation, lane)

    async def run(self) -> dict[str, int | str]:
        """Execute every batch and enforce the reply invariant after each."""
        await asyncio.gather(*(client.register() for client in self.clients))
        await asyncio.gather(*(client.join_room() for client in self.clients))
        if self.scenario.profile == "recovery":
            return await self._run_recovery()
        if self.scenario.profile == "saturation":
            await asyncio.gather(
                *(client.sync_incremental(timeout_ms=0, allow_limited=True) for client in self.clients),
            )
            return await self._run_saturation()

        await self.oracle.initialize()
        await self._send_roots(range(self.scenario.thread_count))
        return await self._run_batches(
            self.scenario.batches,
        )

    async def _run_recovery(self) -> dict[str, int | str]:
        """Replay a bounded outage, limited sync, reconnect, and duplicate audit."""
        observers = tuple(self._recovery_client(room, 0) for room in range(self.scenario.room_count))
        oracles = tuple(ExactReplyOracle(client, self.stack.agent_id) for client in observers)
        await asyncio.gather(*(oracle.initialize() for oracle in oracles))
        await self._send_recovery_roots(oracles)
        await self._send_recovery_checkpoint_barrier(oracles)

        for oracle in oracles:
            oracle.arm_gap_audit()
        self.stack.stop_mindroom()
        offline_started = time.monotonic()
        message_count = 0
        retry_count = 0
        for batch in self.scenario.batches:
            results = await asyncio.gather(*(self._apply_recovery_operation(operation) for operation in batch))
            for operation, event_id, payload in results:
                self.operation_count += 1
                if operation.kind is LiveOperationKind.IDEMPOTENT_RETRY:
                    retry_count += 1
                    continue
                message_count += 1
                self.event_ids[operation.event_ref] = event_id
                self.sent_payloads[operation.event_ref] = payload
                root_event_id = self.event_ids[f"root:{operation.room}:{operation.thread}"]
                self._expect_source(
                    oracles[operation.room],
                    operation.event_ref,
                    event_id,
                    root_event_id=root_event_id,
                    room=operation.room,
                    thread=operation.thread,
                    client=operation.client,
                    source_content=payload.content,
                )
            self.executed_batches += 1

        await asyncio.gather(*(oracle.audit_armed_limited_gap() for oracle in oracles))
        self.stack.resume_mindroom()
        self.restart_count += 1
        await asyncio.gather(
            *(
                oracle.wait_until_exact(
                    deadline_seconds=self.reply_timeout,
                    settle_seconds=self.settle_seconds,
                    allow_limited=True,
                )
                for oracle in oracles
            ),
        )
        for oracle in oracles:
            self.response_event_ids.update(oracle.response_event_by_ref)
        recovery_seconds = time.monotonic() - offline_started

        self.stack.restart_mindroom()
        self.restart_count += 1
        restart_barrier_count = await self._send_recovery_restart_barriers(oracles)
        for oracle in oracles:
            oracle._assert_no_wrong_replies()

        return {
            "batches": self.executed_batches,
            "canonical_agent_replies": sum(len(oracle.expected_sources) for oracle in oracles),
            "clients": self.scenario.client_count * self.scenario.room_count,
            "duplicates": 0,
            "messages": message_count,
            "missing": 0,
            "operations": self.operation_count,
            "oracle_limited_timelines": sum(oracle.limited_timeline_count for oracle in oracles),
            "oracle_gap_audit_pages": sum(oracle.gap_audit_page_count for oracle in oracles),
            "oracle_pagination_pages": sum(oracle.pagination_page_count for oracle in oracles),
            "pre_outage_checkpoint_barriers": 1,
            "recovery_runtime_ms": round(recovery_seconds * 1000),
            "restart_barriers": restart_barrier_count,
            "restarts": self.restart_count,
            "rooms": self.scenario.room_count,
            "roots": self.scenario.thread_count * self.scenario.room_count,
            "status": "PASS",
            "threads": self.scenario.thread_count * self.scenario.room_count,
            "transaction_retries": retry_count,
        }

    async def _send_recovery_checkpoint_barrier(
        self,
        oracles: tuple[ExactReplyOracle, ...],
    ) -> None:
        """Attach the pre-outage durable checkpoint wait to a concrete source."""
        room = 0
        thread = 0
        logical_ref = "pre-outage-checkpoint-barrier"
        root_ref = f"root:{room}:{thread}"
        root_event_id = self.event_ids[root_ref]
        reply_event_id = self.response_event_ids[f"response:{root_ref}"]
        content = self._message_content(
            "Live recovery pre-outage checkpoint barrier",
            relation={
                "rel_type": "m.thread",
                "event_id": root_event_id,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": reply_event_id},
            },
            source_marker=logical_ref,
        )
        event_id = await self._recovery_client(room, 0).send_event(
            "m.room.message",
            f"live-recovery-{logical_ref}",
            content,
        )
        self.event_ids[logical_ref] = event_id
        self._expect_source(
            oracles[room],
            logical_ref,
            event_id,
            root_event_id=root_event_id,
            room=room,
            thread=thread,
            source_content=content,
        )
        await oracles[room].wait_until_exact(
            deadline_seconds=self.reply_timeout,
            settle_seconds=self.settle_seconds,
        )
        self.response_event_ids.update(oracles[room].response_event_by_ref)
        checkpoint_after_reply = self.stack.sync_checkpoint_state(AGENT_NAME)
        await self.stack.wait_for_sync_checkpoint_advance(
            AGENT_NAME,
            checkpoint_after_reply,
            deadline_seconds=self.reply_timeout,
        )

    async def _send_recovery_restart_barriers(
        self,
        oracles: tuple[ExactReplyOracle, ...],
    ) -> int:
        """Fence every sender/thread lane before the final duplicate audit."""
        lanes = sorted(
            {
                (operation.room, operation.client, operation.thread)
                for batch in self.scenario.batches
                for operation in batch
                if operation.kind is not LiveOperationKind.IDEMPOTENT_RETRY
            },
        )

        async def send_barrier(
            room: int,
            client: int,
            thread: int,
        ) -> tuple[int, int, str, str, dict[str, Any]]:
            logical_ref = f"restart-barrier:{room}:{client}:{thread}"
            root_ref = f"root:{room}:{thread}"
            root_event_id = self.event_ids[root_ref]
            latest_source_ref = self._latest_source_ref[(room, client, thread)]
            reply_event_id = self.response_event_ids[f"response:{latest_source_ref}"]
            content = self._message_content(
                f"Live recovery restart barrier {room}:{client}:{thread}",
                relation={
                    "rel_type": "m.thread",
                    "event_id": root_event_id,
                    "is_falling_back": True,
                    "m.in_reply_to": {"event_id": reply_event_id},
                },
                source_marker=logical_ref,
            )
            event_id = await self._recovery_client(room, client).send_event(
                "m.room.message",
                f"live-recovery-{logical_ref}",
                content,
            )
            return room, thread, logical_ref, event_id, content

        barriers: list[tuple[int, int, str, str, dict[str, Any]]] = []
        pending_lanes = list(lanes)
        while pending_lanes:
            used_conversations: set[tuple[int, int]] = set()
            wave: list[tuple[int, int, int]] = []
            remaining: list[tuple[int, int, int]] = []
            for lane in pending_lanes:
                conversation = (lane[0], lane[2])
                if conversation in used_conversations:
                    remaining.append(lane)
                else:
                    used_conversations.add(conversation)
                    wave.append(lane)
            wave_barriers = await asyncio.gather(*(send_barrier(*lane) for lane in wave))
            barriers.extend(wave_barriers)
            for (room, client, thread), (_, _, logical_ref, event_id, content) in zip(
                wave,
                wave_barriers,
                strict=True,
            ):
                self.event_ids[logical_ref] = event_id
                root_event_id = self.event_ids[f"root:{room}:{thread}"]
                self._expect_source(
                    oracles[room],
                    logical_ref,
                    event_id,
                    root_event_id=root_event_id,
                    room=room,
                    thread=thread,
                    client=client,
                    source_content=content,
                )
            await asyncio.gather(
                *(
                    oracle.wait_until_exact(
                        deadline_seconds=self.reply_timeout,
                        settle_seconds=0,
                        allow_limited=True,
                    )
                    for oracle in oracles
                ),
            )
            for oracle in oracles:
                self.response_event_ids.update(oracle.response_event_by_ref)
            pending_lanes = remaining
        await asyncio.gather(
            *(
                oracle.wait_until_exact(
                    deadline_seconds=self.reply_timeout,
                    settle_seconds=max(self.settle_seconds, 1),
                    allow_limited=True,
                )
                for oracle in oracles
            ),
        )
        for oracle in oracles:
            self.response_event_ids.update(oracle.response_event_by_ref)
        checkpoint_after_replies = self.stack.sync_checkpoint_state(AGENT_NAME)
        await self.stack.wait_for_sync_checkpoint_advance(
            AGENT_NAME,
            checkpoint_after_replies,
            deadline_seconds=self.reply_timeout,
        )
        await asyncio.gather(
            *(
                oracle.wait_until_exact(
                    deadline_seconds=self.reply_timeout,
                    settle_seconds=max(self.settle_seconds, 1),
                    allow_limited=True,
                )
                for oracle in oracles
            ),
        )
        for oracle in oracles:
            self.response_event_ids.update(oracle.response_event_by_ref)
        return len(barriers)

    async def _send_recovery_roots(self, oracles: tuple[ExactReplyOracle, ...]) -> None:
        """Create hot and cold thread roots in every room before the outage."""

        async def send_root(room: int, thread: int) -> tuple[int, str, str, _SentPayload]:
            logical_ref = f"root:{room}:{thread}"
            content = self._message_content(
                f"Live recovery root {room}:{thread}",
                source_marker=logical_ref,
            )
            payload = _SentPayload("m.room.message", f"live-recovery-{logical_ref}", content)
            event_id = await self._recovery_client(room, 0).send_event(
                payload.event_type,
                payload.txn_id,
                payload.content,
            )
            return room, logical_ref, event_id, payload

        roots = await asyncio.gather(
            *(
                send_root(room, thread)
                for room in range(self.scenario.room_count)
                for thread in range(self.scenario.thread_count)
            ),
        )
        for room, logical_ref, event_id, payload in roots:
            self.event_ids[logical_ref] = event_id
            self.sent_payloads[logical_ref] = payload
            self._expect_source(
                oracles[room],
                logical_ref,
                event_id,
                root_event_id=event_id,
                room=room,
                thread=int(logical_ref.rsplit(":", 1)[1]),
                source_content=payload.content,
            )
        await asyncio.gather(
            *(
                oracle.wait_until_exact(
                    deadline_seconds=self.reply_timeout,
                    settle_seconds=self.settle_seconds,
                )
                for oracle in oracles
            ),
        )
        for oracle in oracles:
            self.response_event_ids.update(oracle.response_event_by_ref)

    async def _apply_recovery_operation(
        self,
        operation: LiveOperation,
    ) -> tuple[LiveOperation, str, _SentPayload]:
        """Send one scheduled offline mutation or exact transaction retry."""
        assert operation.target is not None
        client = self._recovery_client(operation.room, operation.client)
        if operation.kind is LiveOperationKind.IDEMPOTENT_RETRY:
            payload = self.sent_payloads[operation.target]
            event_id = await client.send_event(payload.event_type, payload.txn_id, payload.content)
            expected_event_id = self.event_ids[operation.target]
            if event_id != expected_event_id:
                msg = f"recovery retry changed event ID for {operation.target}: {expected_event_id} -> {event_id}"
                raise AssertionError(msg)
            return operation, event_id, payload

        target_event_id = self._resolve_event_ref(operation.target)
        if operation.kind is LiveOperationKind.THREAD_MESSAGE:
            root_event_id = self.event_ids[f"root:{operation.room}:{operation.thread}"]
            relation = {
                "rel_type": "m.thread",
                "event_id": root_event_id,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": target_event_id},
            }
        elif operation.kind is LiveOperationKind.PLAIN_REPLY:
            relation = {"m.in_reply_to": {"event_id": target_event_id}}
        else:
            msg = f"unsupported recovery operation: {operation.kind}"
            raise AssertionError(msg)
        content = self._message_content(
            f"Live recovery message {operation.operation_id}",
            relation=relation,
            source_marker=operation.event_ref,
        )
        payload = _SentPayload(
            "m.room.message",
            f"live-recovery-op-{operation.operation_id}",
            content,
        )
        event_id = await client.send_event(payload.event_type, payload.txn_id, payload.content)
        return operation, event_id, payload

    def _recovery_client(self, room: int, client: int) -> LiveMatrixClient:
        """Return the uniquely scheduled sender in one recovery room."""
        return next(item for item in self.clients if item.room_slot == room and item.client_slot == client)

    async def _run_saturation(self) -> dict[str, int | str]:
        """Run hot and parallel turns without cross-thread barriers."""
        parallel_start = self._saturation_parallel_start()
        expected_sources: dict[str, _ExpectedSaturationReply] = {}

        hot_root, hot_response = await self._saturation_turn(
            self.clients[0],
            thread=0,
            label="hot-root",
            thread_root=None,
            reply_to=None,
            expected_sources=expected_sources,
        )
        for batch in self.scenario.batches[:parallel_start]:
            operation = batch[0]
            _, hot_response = await self._saturation_turn(
                self.clients[0],
                thread=0,
                label=operation.event_ref,
                thread_root=hot_root,
                reply_to=hot_response,
                expected_sources=expected_sources,
            )
            self.operation_count += 1
            self.executed_batches += 1

        parallel_batches = self.scenario.batches[parallel_start:]

        async def run_parallel_thread(
            thread: int,
        ) -> tuple[int, LiveMatrixClient, str, str]:
            client = self._client_for_thread(thread)
            root, response = await self._saturation_turn(
                client,
                thread=thread,
                label=f"root:{thread}",
                thread_root=None,
                reply_to=None,
                expected_sources=expected_sources,
            )
            for batch in parallel_batches:
                operation = next(item for item in batch if item.thread == thread)
                _, response = await self._saturation_turn(
                    client,
                    thread=thread,
                    label=operation.event_ref,
                    thread_root=root,
                    reply_to=response,
                    expected_sources=expected_sources,
                )
                self.operation_count += 1
            return thread, client, root, response

        parallel_lanes = await asyncio.gather(
            *(run_parallel_thread(thread) for thread in range(1, self.scenario.thread_count)),
        )
        self.executed_batches += len(parallel_batches)

        lane_states = ((0, self.clients[0], hot_root, hot_response), *parallel_lanes)
        # Hot thread 0 and parallel thread 1 intentionally share client 0.
        # Serialize fences so they cannot race that client's private sync cursor.
        for thread, client, root, response in lane_states:
            await self._saturation_turn(
                client,
                thread=thread,
                label=f"saturation-barrier:{thread}",
                thread_root=root,
                reply_to=response,
                expected_sources=expected_sources,
            )
        checkpoint_after_replies = self.stack.sync_checkpoint_state(AGENT_NAME)
        await self.stack.wait_for_sync_checkpoint_advance(
            AGENT_NAME,
            checkpoint_after_replies,
            deadline_seconds=self.reply_timeout,
        )
        await self._wait_for_saturation_quiescence(expected_sources)

        return {
            "batches": self.executed_batches,
            "canonical_agent_replies": len(expected_sources),
            "operations": self.operation_count,
            "restarts": 0,
            "roots": self.scenario.thread_count,
            "saturation_barriers": len(lane_states),
            "status": "PASS",
        }

    async def _wait_for_saturation_quiescence(
        self,
        expected_sources: Mapping[str, _ExpectedSaturationReply],
    ) -> None:
        """Keep every observer open after all event-attached lane barriers."""
        quiet_deadline = time.monotonic() + max(self.settle_seconds, 1.0)
        while time.monotonic() < quiet_deadline:
            await asyncio.gather(
                *(client.sync_incremental(timeout_ms=250, allow_limited=True) for client in self.clients),
            )
            self._assert_saturation_replies(expected_sources)
        self._assert_saturation_replies(expected_sources)

    def _assert_saturation_replies(
        self,
        expected_sources: Mapping[str, _ExpectedSaturationReply],
    ) -> None:
        """Audit the union of complete and paginated observer histories."""
        all_events = {event_id: event for client in self.clients for event_id, event in client.seen_events.items()}
        response_ids = self._canonical_response_ids(all_events.values())
        response_roots = self._canonical_response_roots(all_events.values())
        duplicates = {
            source_event_id: sorted(event_ids)
            for source_event_id, event_ids in response_ids.items()
            if source_event_id in expected_sources and len(event_ids) != 1
        }
        missing = sorted(expected_sources.keys() - response_ids.keys())
        unexpected = {
            source_event_id: sorted(event_ids)
            for source_event_id, event_ids in response_ids.items()
            if source_event_id not in expected_sources
        }
        malformed = sorted(self._malformed_agent_original_ids(all_events.values()))
        wrong_roots: dict[str, str | None] = {}
        corrupt_bodies: list[str] = []
        for source_event_id, expectation in expected_sources.items():
            event_ids = response_ids.get(source_event_id, set())
            if len(event_ids) != 1:
                continue
            response_event_id = next(iter(event_ids))
            observed_root = response_roots.get((source_event_id, response_event_id))
            if observed_root != expectation.root_event_id:
                wrong_roots[source_event_id] = observed_root
            body = self._latest_event_body(all_events.values(), response_event_id)
            if not ExactReplyOracle._is_complete_model_body(
                body,
                expected_source_marker=expectation.source_marker,
                expected_history_fingerprint=expectation.history_fingerprint,
            ):
                corrupt_bodies.append(source_event_id)
        if duplicates or missing or unexpected or malformed or wrong_roots or corrupt_bodies:
            msg = (
                "saturation reply invariant failed: "
                f"duplicates={duplicates}, missing={missing}, unexpected={unexpected}, "
                f"malformed={malformed}, wrong_roots={wrong_roots}, corrupt_bodies={sorted(corrupt_bodies)}"
            )
            raise AssertionError(msg)

    async def _saturation_turn(
        self,
        client: LiveMatrixClient,
        *,
        thread: int,
        label: str,
        thread_root: str | None,
        reply_to: str | None,
        expected_sources: dict[str, _ExpectedSaturationReply],
    ) -> tuple[str, str]:
        """Send one old-harness turn and wait for its completed stream."""
        content = self._message_content(
            f"Live saturation {label}",
            relation=(
                {
                    "rel_type": "m.thread",
                    "event_id": thread_root,
                    "is_falling_back": True,
                    "m.in_reply_to": {"event_id": reply_to},
                }
                if thread_root is not None and reply_to is not None
                else None
            ),
            source_marker=label,
        )
        txn_id = f"live-saturation-{label}-{secrets.token_hex(4)}"
        source_event_id = await client.send_event("m.room.message", txn_id, content)
        root_event_id = thread_root or source_event_id
        history_markers = self._history_markers[(0, thread)]
        source_marker = _source_marker_from_content(content)
        history_markers.append(source_marker)
        history_fingerprint = _history_fingerprint(history_markers)
        expected_sources[source_event_id] = _ExpectedSaturationReply(
            root_event_id=root_event_id,
            source_marker=source_marker,
            history_fingerprint=history_fingerprint,
        )
        response_event_id = await self._wait_for_completed_response(
            client,
            root_event_id=root_event_id,
            source_event_id=source_event_id,
            source_marker=source_marker,
            history_markers=history_markers,
        )
        return root_event_id, response_event_id

    async def _wait_for_completed_response(
        self,
        client: LiveMatrixClient,
        *,
        root_event_id: str,
        source_event_id: str,
        source_marker: str,
        history_markers: Collection[str],
    ) -> str:
        """Wait until one source has exactly one fully streamed response."""
        deadline = time.monotonic() + self.reply_timeout
        while time.monotonic() < deadline:
            malformed = self._malformed_agent_original_ids(client.seen_events.values())
            if malformed:
                msg = f"malformed visible agent replies: {sorted(malformed)}"
                raise AssertionError(msg)
            response_ids = self._canonical_response_ids(
                client.seen_events.values(),
                root_event_id=root_event_id,
            ).get(source_event_id, set())
            if len(response_ids) > 1:
                msg = f"duplicate agent replies for {source_event_id}: {sorted(response_ids)}"
                raise AssertionError(msg)
            if len(response_ids) == 1:
                response_event_id = next(iter(response_ids))
                body = self._latest_event_body(client.seen_events.values(), response_event_id)
                if ExactReplyOracle._is_complete_model_body(
                    body,
                    expected_source_marker=source_marker,
                    expected_history_fingerprint=_history_fingerprint(history_markers),
                ):
                    return response_event_id
            await client.sync_incremental(timeout_ms=1000, allow_limited=True)
        msg = f"agent response timeout for {source_event_id}"
        raise TimeoutError(msg)

    def _canonical_response_ids(
        self,
        events: Collection[Mapping[str, Any]],
        *,
        root_event_id: str | None = None,
    ) -> dict[str, set[str]]:
        """Index canonical agent originals by their direct source event."""
        response_ids: dict[str, set[str]] = defaultdict(set)
        for event in events:
            if event.get("type") != "m.room.message" or event.get("sender") != self.stack.agent_id:
                continue
            event_id = event.get("event_id")
            content = event.get("content")
            if not isinstance(event_id, str) or not isinstance(content, dict):
                continue
            relation = content.get("m.relates_to")
            if isinstance(relation, dict) and relation.get("rel_type") == "m.replace":
                continue
            if not isinstance(relation, dict) or relation.get("rel_type") != "m.thread":
                continue
            if root_event_id is not None and relation.get("event_id") != root_event_id:
                continue
            in_reply_to = relation.get("m.in_reply_to")
            source_event_id = in_reply_to.get("event_id") if isinstance(in_reply_to, dict) else None
            if isinstance(source_event_id, str):
                response_ids[source_event_id].add(event_id)
        return response_ids

    def _malformed_agent_original_ids(
        self,
        events: Collection[Mapping[str, Any]],
    ) -> set[str]:
        """Return visible agent originals lacking one canonical thread/source relation."""
        malformed: set[str] = set()
        for event in events:
            if event.get("type") != "m.room.message" or event.get("sender") != self.stack.agent_id:
                continue
            event_id = event.get("event_id")
            if not isinstance(event_id, str):
                continue
            content = event.get("content")
            relation = content.get("m.relates_to") if isinstance(content, dict) else None
            if isinstance(relation, dict) and relation.get("rel_type") == "m.replace":
                continue
            in_reply_to = relation.get("m.in_reply_to") if isinstance(relation, dict) else None
            if (
                not isinstance(relation, dict)
                or relation.get("rel_type") != "m.thread"
                or not isinstance(relation.get("event_id"), str)
                or not isinstance(in_reply_to, dict)
                or not isinstance(in_reply_to.get("event_id"), str)
            ):
                malformed.add(event_id)
        return malformed

    def _canonical_response_roots(
        self,
        events: Collection[Mapping[str, Any]],
    ) -> dict[tuple[str, str], str]:
        """Index each canonical response's observed thread root."""
        roots: dict[tuple[str, str], str] = {}
        for event in events:
            if event.get("type") != "m.room.message" or event.get("sender") != self.stack.agent_id:
                continue
            event_id = event.get("event_id")
            content = event.get("content")
            if not isinstance(event_id, str) or not isinstance(content, dict):
                continue
            relation = content.get("m.relates_to")
            if not isinstance(relation, dict) or relation.get("rel_type") != "m.thread":
                continue
            in_reply_to = relation.get("m.in_reply_to")
            source_event_id = in_reply_to.get("event_id") if isinstance(in_reply_to, dict) else None
            root_event_id = relation.get("event_id")
            if isinstance(source_event_id, str) and isinstance(root_event_id, str):
                roots[(source_event_id, event_id)] = root_event_id
        return roots

    @staticmethod
    def _latest_event_body(
        events: Collection[Mapping[str, Any]],
        response_event_id: str,
    ) -> str:
        """Return the newest original or edit body for one response."""
        original_body = ""
        edit_candidates: list[tuple[int, str, str]] = []
        for event in events:
            event_id = event.get("event_id")
            content = event.get("content")
            if not isinstance(event_id, str) or not isinstance(content, dict):
                continue
            relation = content.get("m.relates_to")
            is_original = event_id == response_event_id
            is_edit = (
                isinstance(relation, dict)
                and relation.get("rel_type") == "m.replace"
                and relation.get("event_id") == response_event_id
            )
            if not is_original and not is_edit:
                continue
            new_content = content.get("m.new_content")
            body_source = new_content if isinstance(new_content, dict) else content
            body = body_source.get("body")
            if isinstance(body, str):
                timestamp = event.get("origin_server_ts")
                if is_edit:
                    edit_candidates.append((timestamp if isinstance(timestamp, int) else 0, event_id, body))
                else:
                    original_body = body
        return max(edit_candidates, default=(0, "", original_body))[2]

    async def _run_batches(
        self,
        batches: tuple[tuple[LiveOperation, ...], ...],
        *,
        batch_index_offset: int = 0,
    ) -> dict[str, int | str]:
        """Run one contiguous scenario segment against already-created roots."""
        for relative_batch_index, batch in enumerate(batches):
            batch_index = batch_index_offset + relative_batch_index
            if batch[0].kind is LiveOperationKind.RESTART_MINDROOM:
                self.stack.restart_mindroom()
                self.restart_count += 1
                await self._send_generic_restart_barrier()
            else:
                results = await asyncio.gather(*(self._apply(operation) for operation in batch))
                for operation, event_id, payload in results:
                    self.operation_count += 1
                    if event_id is not None and operation.kind is not LiveOperationKind.IDEMPOTENT_RETRY:
                        self.event_ids[operation.event_ref] = event_id
                    if payload is not None:
                        self.sent_payloads[operation.event_ref] = payload
                    if operation.kind in {
                        LiveOperationKind.THREAD_MESSAGE,
                        LiveOperationKind.PLAIN_REPLY,
                    }:
                        assert event_id is not None
                        assert payload is not None
                        self._expect_source(
                            self.oracle,
                            operation.event_ref,
                            event_id,
                            root_event_id=self.event_ids[f"root:{operation.thread}"],
                            room=0,
                            thread=operation.thread,
                            source_content=payload.content,
                        )
                    elif operation.kind is LiveOperationKind.EDIT:
                        assert payload is not None
                        self._record_edit_revision(operation, payload.content)
                    elif operation.kind is LiveOperationKind.REDACTION:
                        await self._record_redaction_and_audit(operation)
            try:
                await self.oracle.wait_until_exact(
                    deadline_seconds=self.reply_timeout,
                    settle_seconds=self.settle_seconds,
                )
            except AssertionError as exc:
                msg = f"{exc} after live batch {batch_index}"
                raise AssertionError(msg) from exc
            self.executed_batches += 1

        return {
            "batches": self.executed_batches,
            "canonical_agent_replies": len(self.oracle.expected_sources),
            "operations": self.operation_count,
            "redaction_history_audits": self.redaction_history_audits,
            "restarts": self.restart_count,
            "roots": self.scenario.thread_count,
            "status": "PASS",
        }

    async def _send_generic_restart_barrier(self) -> None:
        """Require a concrete exact reply and durable sync after every restart."""
        thread = 0
        logical_ref = f"restart-barrier:{self.restart_count}"
        root_event_id = self.event_ids[f"root:{thread}"]
        latest_source_ref = self._latest_source_ref[(0, 0, thread)]
        reply_event_id = self.oracle.resolve_response_ref(f"response:{latest_source_ref}")
        content = self._message_content(
            f"Live fuzz restart barrier {self.restart_count}",
            relation={
                "rel_type": "m.thread",
                "event_id": root_event_id,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": reply_event_id},
            },
            source_marker=logical_ref,
        )
        event_id = await self.client.send_event(
            "m.room.message",
            f"live-fuzz-{logical_ref}",
            content,
        )
        self.event_ids[logical_ref] = event_id
        self._expect_source(
            self.oracle,
            logical_ref,
            event_id,
            root_event_id=root_event_id,
            room=0,
            thread=thread,
            source_content=content,
        )
        await self.oracle.wait_until_exact(
            deadline_seconds=self.reply_timeout,
            settle_seconds=self.settle_seconds,
        )
        self.response_event_ids.update(self.oracle.response_event_by_ref)
        checkpoint_after_reply = self.stack.sync_checkpoint_state(AGENT_NAME)
        await self.stack.wait_for_sync_checkpoint_advance(
            AGENT_NAME,
            checkpoint_after_reply,
            deadline_seconds=self.reply_timeout,
        )

    def _saturation_parallel_start(self) -> int:
        """Return the first batch belonging to the parallel saturation phase."""
        return next(
            (
                index
                for index, batch in enumerate(self.scenario.batches)
                if any(operation.thread != 0 for operation in batch)
            ),
            len(self.scenario.batches),
        )

    def _client_for_thread(self, thread: int) -> LiveMatrixClient:
        """Use the original multi-sender mapping for saturation traces."""
        if self.scenario.profile != "saturation":
            return self.client
        client_index = max(thread - 1, 0)
        return self.clients[client_index]

    async def _send_roots(self, threads: Collection[int]) -> None:
        async def send_root(thread: int) -> tuple[int, str, _SentPayload]:
            logical_ref = f"root:{thread}"
            content = self._message_content(
                f"Live fuzz root {thread}",
                source_marker=logical_ref,
            )
            payload = _SentPayload("m.room.message", f"live-fuzz-{logical_ref}", content)
            event_id = await self._client_for_thread(thread).send_event(
                payload.event_type,
                payload.txn_id,
                payload.content,
            )
            return thread, event_id, payload

        roots = await asyncio.gather(*(send_root(thread) for thread in threads))
        for thread, event_id, payload in roots:
            logical_ref = f"root:{thread}"
            self.event_ids[logical_ref] = event_id
            self.sent_payloads[logical_ref] = payload
            self._expect_source(
                self.oracle,
                logical_ref,
                event_id,
                root_event_id=event_id,
                room=0,
                thread=thread,
                source_content=payload.content,
            )
        await self.oracle.wait_until_exact(
            deadline_seconds=self.reply_timeout,
            settle_seconds=self.settle_seconds,
        )

    async def _apply(
        self,
        operation: LiveOperation,
    ) -> tuple[LiveOperation, str | None, _SentPayload | None]:
        assert operation.target is not None
        target_event_id = self._resolve_event_ref(operation.target)
        txn_id = f"live-fuzz-op-{operation.operation_id}"
        client = self._client_for_thread(operation.thread)

        if operation.kind is LiveOperationKind.THREAD_MESSAGE:
            root_event_id = self.event_ids[f"root:{operation.thread}"]
            content = self._message_content(
                f"Live fuzz thread message {operation.operation_id}",
                relation={
                    "rel_type": "m.thread",
                    "event_id": root_event_id,
                    "is_falling_back": True,
                    "m.in_reply_to": {"event_id": target_event_id},
                },
                source_marker=operation.event_ref,
            )
            payload = _SentPayload("m.room.message", txn_id, content)
            event_id = await client.send_event(payload.event_type, txn_id, content)
            return operation, event_id, payload

        if operation.kind is LiveOperationKind.PLAIN_REPLY:
            content = self._message_content(
                f"Live fuzz plain reply {operation.operation_id}",
                relation={"m.in_reply_to": {"event_id": target_event_id}},
                source_marker=operation.event_ref,
            )
            payload = _SentPayload("m.room.message", txn_id, content)
            event_id = await client.send_event(payload.event_type, txn_id, content)
            return operation, event_id, payload

        if operation.kind is LiveOperationKind.EDIT:
            source_marker = operation.target.removeprefix("response:")
            new_content = self._message_content(
                f"Live fuzz edited message {operation.operation_id}",
                source_marker=source_marker,
            )
            content = {
                **new_content,
                "m.new_content": new_content,
                "m.relates_to": {"rel_type": "m.replace", "event_id": target_event_id},
            }
            event_id = await client.send_event("m.room.message", txn_id, content)
            return operation, event_id, _SentPayload("m.room.message", txn_id, content)

        if operation.kind is LiveOperationKind.REACTION:
            content = {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": target_event_id,
                    "key": f"fuzz-{operation.operation_id}",
                },
            }
            event_id = await client.send_event("m.reaction", txn_id, content)
            return operation, event_id, None

        if operation.kind is LiveOperationKind.REDACTION:
            event_id = await client.redact(target_event_id, txn_id)
            return operation, event_id, None

        payload = self.sent_payloads[operation.target]
        event_id = await client.send_event(payload.event_type, payload.txn_id, payload.content)
        if event_id != target_event_id:
            msg = f"idempotent retry changed event ID for {operation.target}: {target_event_id} -> {event_id}"
            raise AssertionError(msg)
        return operation, event_id, None

    def _resolve_event_ref(self, logical_ref: str) -> str:
        if logical_ref.startswith("response:"):
            recovery_event_id = self.response_event_ids.get(logical_ref)
            if recovery_event_id is not None:
                return recovery_event_id
            return self.oracle.resolve_response_ref(logical_ref)
        event_id = self.event_ids.get(logical_ref)
        if event_id is None:
            msg = f"event not observed for {logical_ref!r}"
            raise KeyError(msg)
        return event_id

    def _message_content(
        self,
        body: str,
        *,
        relation: Mapping[str, Any] | None = None,
        source_marker: str | None = None,
    ) -> dict[str, Any]:
        source_identity = _source_identity(source_marker, body) if source_marker is not None else None
        marker_suffix = f" LIVE-SOURCE[{source_identity}]" if source_identity is not None else ""
        content: dict[str, Any] = {
            "msgtype": "m.text",
            "body": f"{body}{marker_suffix} {self.stack.agent_id}",
            "m.mentions": {"user_ids": [self.stack.agent_id]},
        }
        if relation is not None:
            content["m.relates_to"] = dict(relation)
        return content


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        msg = "must be at least 1"
        raise argparse.ArgumentTypeError(msg)
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        msg = "must be non-negative"
        raise argparse.ArgumentTypeError(msg)
    return parsed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("fuzz", "recovery", "saturation"), default="fuzz")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--steps", type=_positive_int, default=200)
    parser.add_argument("--rooms", type=_positive_int, default=3)
    parser.add_argument("--clients", type=_positive_int, default=4)
    parser.add_argument("--messages-per-room", type=_positive_int, default=64)
    parser.add_argument("--threads", type=_positive_int, default=45)
    parser.add_argument("--max-batch-size", type=_positive_int, default=16)
    parser.add_argument("--restart-interval", type=_non_negative_int, default=100)
    parser.add_argument(
        "--reply-timeout",
        type=float,
        help="per-reply deadline (default: 60s fuzz, 180s saturation)",
    )
    parser.add_argument("--settle-seconds", type=float, default=0.75)
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--save-trace", type=Path)
    parser.add_argument("--failure-log", type=Path)
    return parser.parse_args()


async def _run_live(
    stack: ManagedTuwunelStack,
    scenario: LiveFuzzScenario,
    *,
    reply_timeout: float,
    settle_seconds: float,
) -> dict[str, int | str]:
    if scenario.profile == "recovery":
        clients = tuple(
            LiveMatrixClient(
                stack.homeserver,
                stack.room_ids[room],
                room_slot=room,
                client_slot=client,
            )
            for room in range(scenario.room_count)
            for client in range(scenario.client_count)
        )
    else:
        client_count = scenario.thread_count - 1 if scenario.profile == "saturation" else 1
        clients = tuple(LiveMatrixClient(stack.homeserver, stack.room_id) for _ in range(client_count))
    try:
        return await LiveFuzzRunner(
            stack,
            clients,
            scenario,
            reply_timeout=reply_timeout,
            settle_seconds=settle_seconds,
        ).run()
    finally:
        await asyncio.gather(*(client.close() for client in clients))


def _failure_artifact(
    *,
    error: Exception,
    scenario: LiveFuzzScenario,
    seed: int | str,
    provenance: RuntimeProvenance,
    stack: ManagedTuwunelStack,
    runtime_ms: int,
) -> dict[str, Any]:
    """Build one replayable failure record with loaded-code provenance."""
    return {
        "diagnostics": stack.diagnostic_counts(),
        "error": f"{type(error).__name__}: {error}",
        "mindroom_log": (
            stack.log_path.read_text(encoding="utf-8", errors="replace") if stack.log_path.exists() else ""
        ),
        "profile": scenario.profile,
        "runtime_ms": runtime_ms,
        "scenario": json.loads(scenario.to_json()),
        "seed": seed,
        **provenance.as_dict(),
    }


def _run_mindroom_runtime_child(attestation_path: Path, arguments: list[str]) -> None:
    """Attest imported packages, then enter the real MindRoom CLI."""
    from mindroom.cli.main import app  # noqa: PLC0415

    mindroom_file = mindroom.__file__
    nio_file = nio.__file__
    if mindroom_file is None or nio_file is None:
        msg = "runtime child imported packages without filesystem paths"
        raise RuntimeError(msg)
    payload = {
        "mindroom_module_path": str(Path(mindroom_file).resolve()),
        "nio_module_path": str(Path(nio_file).resolve()),
    }
    temporary_path = attestation_path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temporary_path.replace(attestation_path)
    sys.argv = ["mindroom", *arguments]
    app()


def main() -> None:
    """Run one trace against a fresh disposable real-server stack."""
    args = _parse_args()
    scenario = (
        LiveFuzzScenario.from_json(args.trace.read_text(encoding="utf-8"))
        if args.trace is not None
        else (
            saturation_scenario()
            if args.profile == "saturation"
            else (
                recovery_scenario_from_seed(
                    args.seed,
                    messages_per_room=args.messages_per_room,
                    room_count=args.rooms,
                    thread_count=args.threads,
                    client_count=args.clients,
                    max_batch_size=args.max_batch_size,
                )
                if args.profile == "recovery"
                else live_scenario_from_seed(
                    args.seed,
                    steps=args.steps,
                    thread_count=args.threads,
                    max_batch_size=args.max_batch_size,
                    restart_interval=args.restart_interval,
                )
            )
        )
    )
    if args.save_trace is not None:
        args.save_trace.write_text(scenario.to_json() + "\n", encoding="utf-8")
    reply_timeout = args.reply_timeout
    if reply_timeout is None:
        reply_timeout = 180 if scenario.profile == "saturation" else 60

    stack = ManagedTuwunelStack(
        room_count=scenario.room_count,
        stream_segments=96 if scenario.profile == "saturation" else 4,
        stream_delay=0.012 if scenario.profile == "saturation" else 0.001,
    )
    provenance = _runtime_provenance()
    seed: int | str = args.seed if args.trace is None else "trace"
    started = time.monotonic()
    try:
        stack.start()
        assert stack.runtime_provenance is not None
        provenance = stack.runtime_provenance
        _validate_nio_provenance(provenance)
        result = asyncio.run(
            _run_live(
                stack,
                scenario,
                reply_timeout=reply_timeout,
                settle_seconds=args.settle_seconds,
            ),
        )
        result["profile"] = scenario.profile
        result["seed"] = seed
        result["preexisting_fuzz_servers"] = stack.preexisting_fuzz_servers
        result["runtime_ms"] = round((time.monotonic() - started) * 1000)
        result.update(provenance.as_dict())
        result.update(stack.diagnostic_counts())
        print(json.dumps(result, sort_keys=True))
    except Exception as error:
        provenance = stack.runtime_provenance or provenance
        artifact = _failure_artifact(
            error=error,
            scenario=scenario,
            seed=seed,
            provenance=provenance,
            stack=stack,
            runtime_ms=round((time.monotonic() - started) * 1000),
        )
        print("Live Matrix fuzz failure:", file=sys.stderr)
        print(
            json.dumps({key: value for key, value in artifact.items() if key != "mindroom_log"}, sort_keys=True),
            file=sys.stderr,
        )
        if args.failure_log is not None:
            args.failure_log.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        log_tail = stack.log_tail()
        if log_tail:
            print("MindRoom log tail:", file=sys.stderr)
            print(log_tail, file=sys.stderr)
        raise
    finally:
        stack.close()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "__mindroom_runtime_child__":
        _run_mindroom_runtime_child(Path(sys.argv[2]), sys.argv[3:])
    else:
        main()
