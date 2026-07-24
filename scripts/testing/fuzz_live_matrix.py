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
import itertools
import json
import os
import random
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
import yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping
    from io import TextIOWrapper

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTANCE_REGISTRY = PROJECT_ROOT / "local" / "instances" / "deploy" / "instances.json"
MODEL_ID = "mindroom-live-fuzz"
AGENT_NAME = "general"
ROOM_KEY = "lobby"


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


class LiveOperationKind(StrEnum):
    """User-visible Matrix mutation families."""

    THREAD_MESSAGE = "thread_message"
    PLAIN_REPLY = "plain_reply"
    EDIT = "edit"
    REACTION = "reaction"
    REDACTION = "redaction"
    IDEMPOTENT_RETRY = "idempotent_retry"
    RESTART_MINDROOM = "restart_mindroom"
    KILL_RESTART_MINDROOM = "kill_restart_mindroom"
    COLD_RESTART_MINDROOM = "cold_restart_mindroom"
    RESTART_TUWUNEL = "restart_tuwunel"
    STOP_MINDROOM = "stop_mindroom"
    START_MINDROOM = "start_mindroom"
    CHECKPOINT = "checkpoint"


MESSAGE_KINDS = frozenset(
    {LiveOperationKind.THREAD_MESSAGE, LiveOperationKind.PLAIN_REPLY},
)
AUTHORED_TARGET_KINDS = frozenset(
    {LiveOperationKind.EDIT, LiveOperationKind.REDACTION, LiveOperationKind.IDEMPOTENT_RETRY},
)
LIFECYCLE_KINDS = frozenset(
    {
        LiveOperationKind.RESTART_MINDROOM,
        LiveOperationKind.KILL_RESTART_MINDROOM,
        LiveOperationKind.COLD_RESTART_MINDROOM,
        LiveOperationKind.RESTART_TUWUNEL,
        LiveOperationKind.STOP_MINDROOM,
        LiveOperationKind.START_MINDROOM,
        LiveOperationKind.CHECKPOINT,
    },
)


@dataclass(frozen=True, slots=True)
class LiveOperation:
    """One replayable live Matrix action."""

    operation_id: int
    kind: LiveOperationKind
    thread: int
    target: str | None
    client: int = 0

    @property
    def event_ref(self) -> str:
        """Return the logical reference for this operation's event."""
        return f"op:{self.operation_id}"

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> LiveOperation:
        """Parse one serialized operation."""
        raw_target = value.get("target")
        if raw_target is not None and not isinstance(raw_target, str):
            msg = "Live Matrix fuzz operation target must be a string or null"
            raise TypeError(msg)
        return cls(
            operation_id=_required_int(value, "operation_id"),
            kind=LiveOperationKind(_required_string(value, "kind")),
            thread=_required_int(value, "thread"),
            target=raw_target,
            client=_required_int(value, "client") if "client" in value else 0,
        )


@dataclass(slots=True)
class _ValidationState:
    """Cross-batch bookkeeping shared by trace validation."""

    known_events: set[str]
    known_responses: set[str]
    message_events: set[str]
    settled_responses: set[str]
    authors: dict[str, int]
    operation_ids: set[int]
    mindroom_running: bool = True


@dataclass(frozen=True, slots=True)
class LiveFuzzScenario:
    """Concurrent live batches with logical references instead of event IDs."""

    thread_count: int
    batches: tuple[tuple[LiveOperation, ...], ...]
    profile: str = "fuzz"
    client_count: int = 1
    room_count: int = 1

    def to_json(self) -> str:
        """Serialize the complete trace for exact replay on a fresh server."""
        return json.dumps(
            {
                "version": 1,
                "profile": self.profile,
                "thread_count": self.thread_count,
                "client_count": self.client_count,
                "room_count": self.room_count,
                "batches": [[asdict(operation) for operation in batch] for batch in self.batches],
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, value: str) -> LiveFuzzScenario:
        """Load a trace emitted by :meth:`to_json`."""
        payload = json.loads(value)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            msg = "unsupported live Matrix fuzz trace"
            raise ValueError(msg)
        raw_batches = payload.get("batches")
        if not isinstance(raw_batches, list):
            msg = "live Matrix fuzz trace is missing batches"
            raise TypeError(msg)
        scenario = cls(
            thread_count=_required_int(payload, "thread_count"),
            batches=tuple(
                tuple(LiveOperation.from_dict(cast("dict[str, object]", operation)) for operation in batch)
                for batch in raw_batches
            ),
            profile=_required_string(payload, "profile"),
            client_count=_required_int(payload, "client_count") if "client_count" in payload else 1,
            room_count=_required_int(payload, "room_count") if "room_count" in payload else 1,
        )
        scenario.validate()
        return scenario

    def root_client(self, thread: int) -> int:
        """Return the deterministic author client for one thread root."""
        return thread % self.client_count

    def room_index(self, thread: int) -> int:
        """Return the room hosting one thread."""
        return thread % self.room_count

    def validate(self) -> None:
        """Reject traces with impossible same-batch or forward dependencies."""
        if self.thread_count < 1:
            msg = "live Matrix fuzz trace must contain at least one thread"
            raise ValueError(msg)
        if self.client_count < 1 or self.room_count < 1:
            msg = "live Matrix fuzz traces need at least one client and one room"
            raise ValueError(msg)
        if self.profile not in {"fuzz", "saturation", "chaos"}:
            msg = f"unsupported live Matrix fuzz profile {self.profile!r}"
            raise ValueError(msg)
        state = _ValidationState(
            known_events={f"root:{thread}" for thread in range(self.thread_count)},
            known_responses={f"response:root:{thread}" for thread in range(self.thread_count)},
            message_events={f"root:{thread}" for thread in range(self.thread_count)},
            settled_responses={f"response:root:{thread}" for thread in range(self.thread_count)},
            authors={f"root:{thread}": self.root_client(thread) for thread in range(self.thread_count)},
            operation_ids=set(),
        )
        for batch in self.batches:
            self._validate_batch(batch, state)
        if not state.mindroom_running:
            msg = "live Matrix fuzz traces must leave MindRoom running"
            raise ValueError(msg)

    def _validate_batch(self, batch: tuple[LiveOperation, ...], state: _ValidationState) -> None:
        if not batch:
            msg = "live Matrix fuzz batches must not be empty"
            raise ValueError(msg)
        if any(operation.kind in LIFECYCLE_KINDS for operation in batch):
            if len(batch) != 1:
                msg = "lifecycle operations must be singleton batches"
                raise ValueError(msg)
            self._validate_lifecycle_operation(batch[0], state)
            return
        reply_keys = [(operation.thread, operation.client) for operation in batch if operation.kind in MESSAGE_KINDS]
        if len(reply_keys) != len(set(reply_keys)):
            msg = "same-thread messages requiring replies must use separate batches"
            raise ValueError(msg)
        if self.profile != "chaos":
            reply_threads = [key[0] for key in reply_keys]
            if len(reply_threads) != len(set(reply_threads)):
                msg = "same-thread messages requiring replies must use separate batches"
                raise ValueError(msg)

        new_events: set[str] = set()
        new_responses: set[str] = set()
        new_messages: set[str] = set()
        new_authors: dict[str, int] = {}
        for operation in batch:
            self._validate_mutation_operation(operation, state)
            if operation.kind is not LiveOperationKind.IDEMPOTENT_RETRY:
                new_events.add(operation.event_ref)
                new_authors[operation.event_ref] = operation.client
            if operation.kind in MESSAGE_KINDS:
                new_messages.add(operation.event_ref)
                new_responses.add(f"response:{operation.event_ref}")

        state.known_events.update(new_events)
        state.known_responses.update(new_responses)
        state.message_events.update(new_messages)
        state.authors.update(new_authors)

    def _validate_lifecycle_operation(self, operation: LiveOperation, state: _ValidationState) -> None:
        self._register_operation_id(operation, state)
        if operation.target is not None:
            msg = f"{operation.kind} must not have a target"
            raise ValueError(msg)
        kind = operation.kind
        if kind is LiveOperationKind.START_MINDROOM:
            if state.mindroom_running:
                msg = "cannot start MindRoom while it is already running"
                raise ValueError(msg)
            state.mindroom_running = True
            return
        if not state.mindroom_running:
            msg = f"{kind} requires a running MindRoom"
            raise ValueError(msg)
        if kind is LiveOperationKind.STOP_MINDROOM:
            state.mindroom_running = False
            return
        if kind is LiveOperationKind.CHECKPOINT:
            state.settled_responses = {f"response:{message}" for message in state.message_events}
            return
        if kind is LiveOperationKind.COLD_RESTART_MINDROOM and state.settled_responses != {
            f"response:{message}" for message in state.message_events
        }:
            msg = "cold restarts must directly follow a checkpoint"
            raise ValueError(msg)
        # Warm restart variants keep MindRoom running and settle at the next checkpoint.

    def _validate_mutation_operation(self, operation: LiveOperation, state: _ValidationState) -> None:
        self._register_operation_id(operation, state)
        if operation.target is None:
            msg = f"{operation.kind} requires a target"
            raise ValueError(msg)
        if operation.target not in state.known_events and operation.target not in state.known_responses:
            msg = f"unknown or same-batch target {operation.target!r}"
            raise ValueError(msg)
        if operation.kind is LiveOperationKind.IDEMPOTENT_RETRY and operation.target not in state.message_events:
            msg = "idempotent retries may only target messages"
            raise ValueError(msg)
        if not state.mindroom_running and operation.target in state.known_responses - state.settled_responses:
            msg = f"{operation.target!r} cannot be targeted while MindRoom is down before its reply settled"
            raise ValueError(msg)
        if self.profile != "chaos":
            return
        if operation.kind in AUTHORED_TARGET_KINDS:
            author = state.authors.get(operation.target)
            if author is None:
                msg = f"{operation.kind} may only target fuzz-authored events, not {operation.target!r}"
                raise ValueError(msg)
            if author != operation.client:
                msg = (
                    f"{operation.kind} on {operation.target!r} must come from its author "
                    f"client {author}, not client {operation.client}"
                )
                raise ValueError(msg)

    def _register_operation_id(self, operation: LiveOperation, state: _ValidationState) -> None:
        if operation.operation_id in state.operation_ids:
            msg = f"duplicate live Matrix fuzz operation ID {operation.operation_id}"
            raise ValueError(msg)
        state.operation_ids.add(operation.operation_id)
        if not 0 <= operation.thread < self.thread_count:
            msg = f"invalid thread {operation.thread}"
            raise ValueError(msg)
        if not 0 <= operation.client < self.client_count:
            msg = f"invalid client {operation.client}"
            raise ValueError(msg)


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
    redacted: set[str]
    authors: dict[str, int]
    settled_responses: set[str]


def _initial_generation_state(thread_count: int, *, client_count: int = 1) -> _ScenarioGenerationState:
    return _ScenarioGenerationState(
        messages={thread: [f"root:{thread}"] for thread in range(thread_count)},
        responses={thread: [f"response:root:{thread}"] for thread in range(thread_count)},
        editable={thread: [f"root:{thread}"] for thread in range(thread_count)},
        reaction_targets={thread: [f"root:{thread}", f"response:root:{thread}"] for thread in range(thread_count)},
        redactable={thread: [f"root:{thread}"] for thread in range(thread_count)},
        redacted=set(),
        authors={f"root:{thread}": thread % client_count for thread in range(thread_count)},
        settled_responses={f"response:root:{thread}" for thread in range(thread_count)},
    )


def _choose_operation(
    randomizer: random.Random,
    state: _ScenarioGenerationState,
    *,
    operation_id: int,
    thread_count: int,
) -> LiveOperation:
    thread = randomizer.randrange(thread_count)
    kind = randomizer.choice(_WEIGHTED_KINDS)
    available_edits = [target for target in state.editable[thread] if target not in state.redacted]
    available_redactions = [target for target in state.redactable[thread] if target not in state.redacted]
    available_retries = [target for target in state.messages[thread] if target not in state.redacted]

    if kind is LiveOperationKind.THREAD_MESSAGE:
        target = randomizer.choice(state.messages[thread])
    elif kind is LiveOperationKind.PLAIN_REPLY:
        target = randomizer.choice(state.responses[thread])
    elif kind is LiveOperationKind.EDIT:
        target = randomizer.choice(available_edits or state.messages[thread])
    elif kind is LiveOperationKind.REACTION:
        target = randomizer.choice(state.reaction_targets[thread])
    elif kind is LiveOperationKind.REDACTION and available_redactions:
        target = randomizer.choice(available_redactions)
    elif kind is LiveOperationKind.IDEMPOTENT_RETRY and available_retries:
        target = randomizer.choice(available_retries)
    else:
        kind = LiveOperationKind.REACTION
        target = randomizer.choice(state.reaction_targets[thread])
    return LiveOperation(operation_id=operation_id, kind=kind, thread=thread, target=target)


def _update_generation_state(
    state: _ScenarioGenerationState,
    operations: Collection[LiveOperation],
) -> None:
    for operation in operations:
        if operation.kind is not LiveOperationKind.IDEMPOTENT_RETRY:
            state.authors[operation.event_ref] = operation.client
        if operation.kind in {
            LiveOperationKind.THREAD_MESSAGE,
            LiveOperationKind.PLAIN_REPLY,
        }:
            state.messages[operation.thread].append(operation.event_ref)
            state.responses[operation.thread].append(f"response:{operation.event_ref}")
            state.editable[operation.thread].append(operation.event_ref)
            state.reaction_targets[operation.thread].extend(
                (operation.event_ref, f"response:{operation.event_ref}"),
            )
            state.redactable[operation.thread].append(operation.event_ref)
        elif operation.kind in {LiveOperationKind.EDIT, LiveOperationKind.REACTION}:
            state.reaction_targets[operation.thread].append(operation.event_ref)
            state.redactable[operation.thread].append(operation.event_ref)
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
            if needs_reply and operation.thread in reply_threads:
                operation = LiveOperation(
                    operation_id=operation.operation_id,
                    kind=LiveOperationKind.REACTION,
                    thread=operation.thread,
                    target=randomizer.choice(state.reaction_targets[operation.thread]),
                )
                needs_reply = False
            operations.append(operation)
            if needs_reply:
                reply_threads.add(operation.thread)
        operation_id += batch_size

        batches.append(tuple(operations))
        generated += len(operations)
        _update_generation_state(state, operations)

    scenario = LiveFuzzScenario(thread_count=thread_count, batches=tuple(batches))
    scenario.validate()
    return scenario


@dataclass(frozen=True, slots=True)
class ChaosTuning:
    """Composable knobs for the adversarial chaos profile."""

    thread_count: int = 24
    client_count: int = 4
    room_count: int = 2
    max_batch_size: int = 12
    hot_thread_weight: int = 6
    checkpoint_interval: int = 40
    lifecycle_interval: int = 70
    downtime_batches: int = 2

    def validate(self) -> None:
        """Reject impossible tuning combinations before generation."""
        if min(self.thread_count, self.client_count, self.room_count, self.max_batch_size) < 1:
            msg = "chaos tuning requires positive thread, client, room, and batch sizes"
            raise ValueError(msg)
        if self.hot_thread_weight < 1 or self.downtime_batches < 0:
            msg = "chaos tuning requires a positive hot-thread weight and non-negative downtime batches"
            raise ValueError(msg)
        if self.checkpoint_interval < 0 or self.lifecycle_interval < 0:
            msg = "chaos tuning intervals must be non-negative"
            raise ValueError(msg)


_LIFECYCLE_CHOICES = (
    LiveOperationKind.RESTART_MINDROOM,
    LiveOperationKind.RESTART_MINDROOM,
    LiveOperationKind.KILL_RESTART_MINDROOM,
    LiveOperationKind.COLD_RESTART_MINDROOM,
    LiveOperationKind.RESTART_TUWUNEL,
    LiveOperationKind.STOP_MINDROOM,
    LiveOperationKind.STOP_MINDROOM,
)


@dataclass(slots=True)
class _ChaosBuild:
    """Mutable context threaded through chaos-scenario generation."""

    randomizer: random.Random
    state: _ScenarioGenerationState
    tuning: ChaosTuning
    batches: list[tuple[LiveOperation, ...]]
    operation_id: int = 0
    generated: int = 0

    def next_operation_id(self) -> int:
        operation_id = self.operation_id
        self.operation_id += 1
        return operation_id

    def singleton(self, kind: LiveOperationKind) -> None:
        self.batches.append(
            (LiveOperation(operation_id=self.next_operation_id(), kind=kind, thread=0, target=None),),
        )
        if kind is LiveOperationKind.CHECKPOINT:
            self.state.settled_responses = {
                f"response:{message}" for messages in self.state.messages.values() for message in messages
            }


def _pick_chaos_thread(build: _ChaosBuild) -> int:
    """Pick a thread with the hot thread over-weighted."""
    tuning = build.tuning
    index = build.randomizer.randrange(tuning.thread_count + tuning.hot_thread_weight - 1)
    return 0 if index < tuning.hot_thread_weight else index - tuning.hot_thread_weight + 1


def _choose_chaos_operation(build: _ChaosBuild, *, mindroom_running: bool) -> LiveOperation:
    """Choose one realistic operation honoring downtime and authorship rules."""
    randomizer = build.randomizer
    state = build.state
    thread = _pick_chaos_thread(build)
    kind = randomizer.choice(_WEIGHTED_KINDS)
    random_client = randomizer.randrange(build.tuning.client_count)

    def response_available(target: str) -> bool:
        return mindroom_running or target in state.settled_responses

    available_responses = [target for target in state.responses[thread] if response_available(target)]
    available_reactions = [
        target
        for target in state.reaction_targets[thread]
        if not target.startswith("response:") or response_available(target)
    ]
    available_edits = [target for target in state.editable[thread] if target not in state.redacted]
    available_redactions = [target for target in state.redactable[thread] if target not in state.redacted]
    available_retries = [target for target in state.messages[thread] if target not in state.redacted]

    target: str | None = None
    client = random_client
    if kind is LiveOperationKind.THREAD_MESSAGE:
        target = randomizer.choice(state.messages[thread])
    elif kind is LiveOperationKind.PLAIN_REPLY and available_responses:
        target = randomizer.choice(available_responses)
    elif kind is LiveOperationKind.EDIT and available_edits:
        target = randomizer.choice(available_edits)
        client = state.authors[target]
    elif kind is LiveOperationKind.REDACTION and available_redactions:
        target = randomizer.choice(available_redactions)
        client = state.authors[target]
    elif kind is LiveOperationKind.IDEMPOTENT_RETRY and available_retries:
        target = randomizer.choice(available_retries)
        client = state.authors[target]
    if target is None:
        kind = LiveOperationKind.REACTION
    if kind is LiveOperationKind.REACTION:
        target = randomizer.choice(available_reactions)
        client = random_client
    assert target is not None
    return LiveOperation(
        operation_id=build.next_operation_id(),
        kind=kind,
        thread=thread,
        target=target,
        client=client,
    )


def _append_chaos_batch(build: _ChaosBuild, *, remaining: int, mindroom_running: bool) -> int:
    """Append one concurrent mutation batch and return its operation count."""
    batch_size = min(remaining, build.randomizer.randint(1, build.tuning.max_batch_size))
    operations: list[LiveOperation] = []
    reply_keys: set[tuple[int, int]] = set()
    for _ in range(batch_size):
        operation = _choose_chaos_operation(build, mindroom_running=mindroom_running)
        if operation.kind in MESSAGE_KINDS and (operation.thread, operation.client) in reply_keys:
            operation = LiveOperation(
                operation_id=operation.operation_id,
                kind=LiveOperationKind.REACTION,
                thread=operation.thread,
                target=build.randomizer.choice(
                    [
                        target
                        for target in build.state.reaction_targets[operation.thread]
                        if not target.startswith("response:")
                        or mindroom_running
                        or target in build.state.settled_responses
                    ],
                ),
                client=operation.client,
            )
        if operation.kind in MESSAGE_KINDS:
            reply_keys.add((operation.thread, operation.client))
        operations.append(operation)
    build.batches.append(tuple(operations))
    _update_generation_state(build.state, operations)
    build.generated += len(operations)
    return len(operations)


def _append_chaos_lifecycle(build: _ChaosBuild, *, steps: int) -> bool:
    """Append one lifecycle disruption; return whether it ended fully settled."""
    kind = build.randomizer.choice(_LIFECYCLE_CHOICES)
    if kind is LiveOperationKind.COLD_RESTART_MINDROOM:
        build.singleton(LiveOperationKind.CHECKPOINT)
        build.singleton(LiveOperationKind.COLD_RESTART_MINDROOM)
        return True
    if kind is not LiveOperationKind.STOP_MINDROOM:
        build.singleton(kind)
        return False
    build.singleton(LiveOperationKind.STOP_MINDROOM)
    for _ in range(build.tuning.downtime_batches):
        remaining = steps - build.generated
        if remaining < 1:
            break
        _append_chaos_batch(build, remaining=remaining, mindroom_running=False)
    build.singleton(LiveOperationKind.START_MINDROOM)
    build.singleton(LiveOperationKind.CHECKPOINT)
    return True


def chaos_scenario_from_seed(
    seed: int,
    *,
    steps: int,
    tuning: ChaosTuning | None = None,
) -> LiveFuzzScenario:
    """Generate one replayable adversarial chaos trace from a seed."""
    if steps < 1:
        msg = "steps must be positive"
        raise ValueError(msg)
    tuning = tuning or ChaosTuning()
    tuning.validate()
    build = _ChaosBuild(
        randomizer=random.Random(seed),  # noqa: S311 - deterministic test trace generation
        state=_initial_generation_state(tuning.thread_count, client_count=tuning.client_count),
        tuning=tuning,
        batches=[],
    )
    ops_since_checkpoint = 0
    ops_since_lifecycle = 0
    while build.generated < steps:
        if tuning.checkpoint_interval and ops_since_checkpoint >= tuning.checkpoint_interval:
            build.singleton(LiveOperationKind.CHECKPOINT)
            ops_since_checkpoint = 0
        if tuning.lifecycle_interval and ops_since_lifecycle >= tuning.lifecycle_interval:
            ended_settled = _append_chaos_lifecycle(build, steps=steps)
            ops_since_lifecycle = 0
            if ended_settled:
                ops_since_checkpoint = 0
            continue
        appended = _append_chaos_batch(
            build,
            remaining=steps - build.generated,
            mindroom_running=True,
        )
        ops_since_checkpoint += appended
        ops_since_lifecycle += appended

    scenario = LiveFuzzScenario(
        thread_count=tuning.thread_count,
        batches=tuple(build.batches),
        profile="chaos",
        client_count=tuning.client_count,
        room_count=tuning.room_count,
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
    """Small deterministic OpenAI-compatible endpoint for live transport tests.

    When ``slow_call_modulus`` is positive, every ``slow_call_modulus``-th call
    streams ``slow_stream_segments`` segments with ``slow_stream_delay`` between
    chunks after an initial ``first_token_delay`` — a deterministic mix of fast
    turns and long 100+-replacement streams that mutations can race against.
    """

    protocol_version = "HTTP/1.1"
    call_ids = itertools.count(1)
    stream_segments = 4
    stream_delay = 0.001
    slow_call_modulus = 0
    slow_stream_segments = 120
    slow_stream_delay = 0.02
    first_token_delay = 0.0

    @classmethod
    def _is_slow_call(cls, call_id: int) -> bool:
        return cls.slow_call_modulus > 0 and call_id % cls.slow_call_modulus == 0

    @classmethod
    def segments_for(cls, call_id: int) -> int:
        """Return the deterministic segment count for one model call."""
        return cls.slow_stream_segments if cls._is_slow_call(call_id) else cls.stream_segments

    @classmethod
    def delay_for(cls, call_id: int) -> float:
        """Return the deterministic inter-chunk delay for one model call."""
        return cls.slow_stream_delay if cls._is_slow_call(call_id) else cls.stream_delay

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
        content = self._response_text(call_id)
        if self._is_slow_call(call_id) and self.first_token_delay > 0:
            time.sleep(self.first_token_delay)
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
    def _response_text(cls, call_id: int) -> str:
        segments = " ".join(f"segment-{index:03d}" for index in range(cls.segments_for(call_id)))
        return f"LIVE-FUZZ call={call_id} {segments} END call={call_id}"

    @classmethod
    def response_text_for(cls, call_id: int) -> str:
        """Return the exact completed body one model call must produce."""
        return cls._response_text(call_id)

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
        chunk_delay = self.delay_for(call_id)
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
            time.sleep(chunk_delay)
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


@dataclass(frozen=True, slots=True)
class StreamProfile:
    """Deterministic model-stub stream shape for one live run."""

    stream_segments: int = 4
    stream_delay: float = 0.001
    slow_call_modulus: int = 0
    slow_stream_segments: int = 120
    slow_stream_delay: float = 0.02
    first_token_delay: float = 0.0


class ManagedTuwunelStack:
    """Disposable Tuwunel plus the current worktree's MindRoom runtime."""

    def __init__(
        self,
        *,
        stream_profile: StreamProfile | None = None,
        room_keys: tuple[str, ...] = (ROOM_KEY,),
    ) -> None:
        token = secrets.token_hex(4)
        self._stream_profile = stream_profile or StreamProfile()
        self.instance_name = f"fuzz{token}"
        self.namespace = self.instance_name
        self.temp_dir = tempfile.TemporaryDirectory(prefix="mindroom-live-matrix-fuzz-")
        self.root = Path(self.temp_dir.name)
        self.storage_path = self.root / "mindroom_data"
        self.config_path = self.root / "config.yaml"
        self.log_path = self.root / "mindroom.log"
        self.api_port = _available_port()
        self.homeserver = ""
        self.server_name = ""
        self.room_keys = room_keys
        self.room_ids: dict[str, str] = {}
        self.room_id = ""
        self.agent_id = ""
        self.router_id = ""
        self._created = False
        self._model_server: ThreadingHTTPServer | None = None
        self._model_thread: threading.Thread | None = None
        self._mindroom_process: subprocess.Popen[str] | None = None
        self._log_handle: TextIOWrapper | None = None
        self._env: dict[str, str] = {}

    def start(self) -> None:
        """Create every live dependency and wait for the managed room."""
        _run_command("just", "local-instances-create", self.instance_name, "tuwunel")
        self._created = True
        registry = json.loads(INSTANCE_REGISTRY.read_text(encoding="utf-8"))
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

    def kill_restart_mindroom(self) -> None:
        """Hard-kill MindRoom without a drain, then restart it."""
        self._stop_mindroom(kill=True)
        self._start_mindroom()

    def cold_restart_mindroom(self) -> None:
        """Restart MindRoom with cleared sync checkpoints, forcing a full resync."""
        self._stop_mindroom()
        sync_tokens_dir = self.storage_path / "sync_tokens"
        if sync_tokens_dir.exists():
            for token_path in sync_tokens_dir.glob("*.token"):
                token_path.unlink()
        self._start_mindroom()

    def stop_mindroom(self) -> None:
        """Stop MindRoom while keeping Tuwunel accepting writes."""
        self._stop_mindroom()

    def start_mindroom(self) -> None:
        """Start MindRoom again after an explicit stop."""
        self._start_mindroom()

    def restart_tuwunel(self) -> None:
        """Restart the homeserver container, forcing every sync loop to reconnect."""
        _run_command("docker", "restart", f"{self.instance_name}-tuwunel")
        self._wait_for_url(f"{self.homeserver}/_matrix/client/versions", timeout=60)

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
        return {
            "cache_coordinator_timeouts": log.count("thread_read_error=cache_coordinator_timeout"),
            "degraded_thread_reads": log.count("matrix_cache_thread_read_degraded"),
            "dispatch_read_timeouts": log.count("thread_read_error=dispatch_read_timeout"),
            "event_loop_stalls": log.count("event_loop_stall_detected"),
            "redacted_source_suppressions": log.count("response_suppressed_for_redacted_source"),
            "sync_certification_uncertain": log.count("matrix_sync_certification_uncertain"),
            "sync_restart_retries": log.count("sync_restart_retry_started"),
        }

    def _start_model_server(self) -> int:
        profile = self._stream_profile
        _ModelHandler.stream_segments = profile.stream_segments
        _ModelHandler.stream_delay = profile.stream_delay
        _ModelHandler.slow_call_modulus = profile.slow_call_modulus
        _ModelHandler.slow_stream_segments = profile.slow_stream_segments
        _ModelHandler.slow_stream_delay = profile.slow_stream_delay
        _ModelHandler.first_token_delay = profile.first_token_delay
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
        # MINDROOM_LIVE_FUZZ_UV_WITH overlays one dependency (for example a
        # pinned mindroom-nio checkout) without touching pyproject or uv.lock.
        overlay = os.environ.get("MINDROOM_LIVE_FUZZ_UV_WITH")
        overlay_args = ("--with", overlay) if overlay else ()
        self._mindroom_process = subprocess.Popen(
            [
                "uv",
                "run",
                *overlay_args,
                "mindroom",
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
        self._wait_for_url(f"http://127.0.0.1:{self.api_port}/api/health", timeout=60)
        state_path = self.storage_path / "matrix_state.yaml"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self._mindroom_process.poll() is not None:
                msg = f"MindRoom exited during startup:\n{self.log_tail()}"
                raise RuntimeError(msg)
            if state_path.exists():
                state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
                rooms = state.get("rooms", {}) if isinstance(state, dict) else {}
                room_ids: dict[str, str] = {}
                for room_key in self.room_keys:
                    room = rooms.get(room_key, {}) if isinstance(rooms, dict) else {}
                    room_id = room.get("room_id") if isinstance(room, dict) else None
                    if isinstance(room_id, str):
                        room_ids[room_key] = room_id
                if len(room_ids) == len(self.room_keys):
                    self.room_ids = room_ids
                    self.room_id = room_ids[self.room_keys[0]]
                    return
            time.sleep(0.2)
        msg = f"MindRoom did not create all of {self.room_keys!r}:\n{self.log_tail()}"
        raise TimeoutError(msg)

    def _stop_mindroom(self, *, kill: bool = False) -> None:
        process = self._mindroom_process
        if process is None:
            return
        if process.poll() is None:
            if kill:
                process.kill()
                process.wait(timeout=10)
            else:
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


class LiveMatrixClient:
    """Minimal real Matrix client used by the live fuzzer."""

    def __init__(self, homeserver: str, room_id: str, *, room_ids: tuple[str, ...] | None = None) -> None:
        self.homeserver = homeserver.rstrip("/")
        self.room_id = room_id
        self.room_ids = room_ids or (room_id,)
        self.http = httpx.AsyncClient(timeout=30)
        self.access_token = ""
        self.next_batch: str | None = None
        self.seen_events: dict[str, dict[str, Any]] = {}
        self.transport_retry_seconds = 0.0

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
        """Join every managed public room."""
        for room_id in self.room_ids:
            encoded_room = quote(room_id, safe="")
            await self._request("POST", f"/_matrix/client/v3/join/{encoded_room}", json_body={})

    async def send_event(
        self,
        event_type: str,
        txn_id: str,
        content: Mapping[str, Any],
        *,
        room_id: str | None = None,
    ) -> str:
        """Send one event with a caller-stable transaction ID."""
        encoded_room = quote(room_id or self.room_id, safe="")
        encoded_type = quote(event_type, safe="")
        encoded_txn = quote(txn_id, safe="")
        data = await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{encoded_room}/send/{encoded_type}/{encoded_txn}",
            json_body=content,
        )
        event_id = data.get("event_id")
        if not isinstance(event_id, str):
            msg = f"Matrix send omitted event_id: {data}"
            raise TypeError(msg)
        return event_id

    async def redact(self, target_event_id: str, txn_id: str, *, room_id: str | None = None) -> str:
        """Redact one event authored by the disposable account."""
        encoded_room = quote(room_id or self.room_id, safe="")
        event_id = quote(target_event_id, safe="")
        encoded_txn = quote(txn_id, safe="")
        data = await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{encoded_room}/redact/{event_id}/{encoded_txn}",
            json_body={"reason": "live cache fuzz"},
        )
        redaction_id = data.get("event_id")
        if not isinstance(redaction_id, str):
            msg = f"Matrix redaction omitted event_id: {data}"
            raise TypeError(msg)
        return redaction_id

    async def paginate_room(self, room_id: str, *, page_limit: int = 500) -> list[dict[str, Any]]:
        """Return the full visible room history through `/messages`."""
        events: list[dict[str, Any]] = []
        from_token: str | None = None
        for _ in range(page_limit):
            params: dict[str, str | int] = {"dir": "b", "limit": 100}
            if from_token is not None:
                params["from"] = from_token
            encoded_room = quote(room_id, safe="")
            data = await self._request(
                "GET",
                f"/_matrix/client/v3/rooms/{encoded_room}/messages",
                params=params,
            )
            chunk = data.get("chunk")
            if not isinstance(chunk, list) or not chunk:
                return events
            events.extend(event for event in chunk if isinstance(event, dict))
            end = data.get("end")
            if not isinstance(end, str) or end == from_token:
                return events
            from_token = end
        msg = f"room {room_id} history exceeded {page_limit} pagination pages"
        raise AssertionError(msg)

    async def sync(self, since: str | None, *, timeout_ms: int) -> dict[str, Any]:
        """Read one incremental sync window from the real homeserver."""
        params: dict[str, str | int] = {
            "timeout": timeout_ms,
            "filter": json.dumps({"room": {"timeline": {"limit": 2000}}}),
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
        data = await self.sync(self.next_batch, timeout_ms=timeout_ms)
        next_batch = data.get("next_batch")
        if not isinstance(next_batch, str):
            msg = "Matrix sync omitted next_batch"
            raise TypeError(msg)
        joined = data.get("rooms", {}).get("join", {})
        for room_id in self.room_ids:
            room = joined.get(room_id, {}) if isinstance(joined, dict) else {}
            timeline = room.get("timeline", {}) if isinstance(room, dict) else {}
            if timeline.get("limited") is True and not allow_limited:
                msg = "incremental Matrix fuzz sync unexpectedly returned a limited timeline"
                raise AssertionError(msg)
            events = timeline.get("events", [])
            if not isinstance(events, list):
                msg = "Matrix sync room timeline events must be a list"
                raise TypeError(msg)
            for raw_event in events:
                if not isinstance(raw_event, dict):
                    continue
                event = cast("dict[str, Any]", raw_event)
                event_id = event.get("event_id")
                if isinstance(event_id, str):
                    self.seen_events[event_id] = event
        self.next_batch = next_batch

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str | int] | None = None,
    ) -> dict[str, Any]:
        # Transaction-keyed PUTs and reads are idempotent, so a bounded retry
        # window lets chaos runs survive an in-flight homeserver restart.
        retry_deadline = time.monotonic() + self.transport_retry_seconds
        while True:
            try:
                response = await self.http.request(
                    method,
                    f"{self.homeserver}{path}",
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    json=json_body,
                    params=params,
                )
            except httpx.TransportError:
                if time.monotonic() >= retry_deadline:
                    raise
                await asyncio.sleep(0.5)
                continue
            if response.status_code in {502, 503, 504} and time.monotonic() < retry_deadline:
                await asyncio.sleep(0.5)
                continue
            break
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
        *,
        internal_relay_senders: Collection[str] = (),
    ) -> None:
        self.client = client
        self.agent_id = agent_id
        self.internal_relay_senders = frozenset(internal_relay_senders)
        self.internal_source_ids: set[str] = set()
        self.next_batch: str | None = None
        self.expected_sources: dict[str, str] = {}
        self.optional_sources: set[str] = set()
        self.response_ids: dict[str, set[str]] = defaultdict(set)
        self.response_event_by_ref: dict[str, str] = {}
        self.seen_event_ids: set[str] = set()
        self.event_summaries: dict[str, dict[str, Any]] = {}
        self.sent_at: dict[str, float] = {}
        self.reply_latencies: dict[str, float] = {}
        self._last_response_at = time.monotonic()
        self._sync_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Establish a sync token before the fuzz traffic starts."""
        await self._sync_once(timeout_ms=0, allow_limited=True)

    def expect(self, logical_ref: str, event_id: str, *, sent_at: float | None = None) -> None:
        """Require exactly one canonical agent reply to a source event."""
        self.expected_sources[event_id] = logical_ref
        if sent_at is not None:
            self.sent_at[event_id] = sent_at

    def mark_source_optional(self, event_id: str) -> None:
        """Allow zero replies for a source redacted before its reply settled."""
        if event_id in self.expected_sources:
            self.optional_sources.add(event_id)

    def unsettled_required_sources(self) -> list[str]:
        """Return required sources still missing their single canonical reply."""
        return [
            event_id
            for event_id in self.expected_sources
            if event_id not in self.optional_sources and len(self.response_ids.get(event_id, ())) != 1
        ]

    async def pump(self, *, timeout_ms: int = 0) -> None:
        """Ingest one sync window and enforce duplicate/unexpected invariants."""
        await self._sync_once(timeout_ms=timeout_ms)
        self._assert_no_wrong_replies()

    async def wait_until_exact(
        self,
        *,
        deadline_seconds: float,
        settle_seconds: float,
    ) -> None:
        """Wait until all sources have one reply and the room stays quiet."""
        deadline = time.monotonic() + deadline_seconds
        settled_after = time.monotonic() + settle_seconds
        while time.monotonic() < deadline:
            await self._sync_once(timeout_ms=250)
            self._assert_no_wrong_replies()
            if not self.unsettled_required_sources():
                settled_after = max(settled_after, self._last_response_at + settle_seconds)
                if time.monotonic() >= settled_after:
                    return
        missing = {
            self.expected_sources[event_id]: len(self.response_ids.get(event_id, ()))
            for event_id in self.unsettled_required_sources()
        }
        msg = f"timed out waiting for exact agent replies: {missing}"
        raise AssertionError(msg)

    def resolve_response_ref(self, response_ref: str) -> str:
        """Resolve a logical agent-response reference to its real event ID."""
        event_id = self.response_event_by_ref.get(response_ref)
        if event_id is None:
            msg = f"response event not observed for {response_ref!r}"
            raise KeyError(msg)
        return event_id

    async def _sync_once(self, *, timeout_ms: int, allow_limited: bool = False) -> None:
        async with self._sync_lock:
            data = await self.client.sync(self.next_batch, timeout_ms=timeout_ms)
            next_batch = data.get("next_batch")
            if not isinstance(next_batch, str):
                msg = "Matrix sync omitted next_batch"
                raise TypeError(msg)
            self.next_batch = next_batch
            joined = data.get("rooms", {}).get("join", {})
            for room_id in self.client.room_ids:
                room = joined.get(room_id, {}) if isinstance(joined, dict) else {}
                timeline = room.get("timeline", {}) if isinstance(room, dict) else {}
                if timeline.get("limited") is True and not allow_limited:
                    msg = "live fuzz oracle received a limited timeline; reduce batch size"
                    raise AssertionError(msg)
                events = timeline.get("events", [])
                if not isinstance(events, list):
                    continue
                for raw_event in events:
                    if isinstance(raw_event, dict):
                        self._ingest_event(raw_event)

    def _ingest_event(self, event: Mapping[str, Any]) -> None:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in self.seen_event_ids:
            return
        self.seen_event_ids.add(event_id)
        self.event_summaries[event_id] = {
            "sender": event.get("sender"),
            "type": event.get("type"),
            "body": (event.get("content") or {}).get("body") if isinstance(event.get("content"), dict) else None,
            "relates_to": (event.get("content") or {}).get("m.relates_to")
            if isinstance(event.get("content"), dict)
            else None,
        }
        if event.get("sender") in self.internal_relay_senders:
            self.internal_source_ids.add(event_id)
            return
        if event.get("sender") != self.agent_id or event.get("type") != "m.room.message":
            return
        content = event.get("content")
        if not isinstance(content, dict):
            return
        relation = content.get("m.relates_to")
        if not isinstance(relation, dict) or relation.get("rel_type") != "m.thread":
            return
        reply = relation.get("m.in_reply_to")
        source_event_id = reply.get("event_id") if isinstance(reply, dict) else None
        if not isinstance(source_event_id, str):
            return
        self.response_ids[source_event_id].add(event_id)
        logical_ref = self.expected_sources.get(source_event_id)
        if logical_ref is not None:
            self.response_event_by_ref[f"response:{logical_ref}"] = event_id
        sent_at = self.sent_at.get(source_event_id)
        if sent_at is not None and source_event_id not in self.reply_latencies:
            self.reply_latencies[source_event_id] = time.monotonic() - sent_at
        self._last_response_at = time.monotonic()

    def _assert_no_wrong_replies(self) -> None:
        duplicates = {
            self.expected_sources.get(source, source): sorted(event_ids)
            for source, event_ids in self.response_ids.items()
            if len(event_ids) > 1
        }
        unexpected = {
            source: sorted(event_ids)
            for source, event_ids in self.response_ids.items()
            if event_ids and source not in self.expected_sources and source not in self.internal_source_ids
        }
        if duplicates or unexpected:
            details = {
                event_id: self.event_summaries.get(event_id)
                for event_id in (
                    *unexpected,
                    *(reply for replies in (*duplicates.values(), *unexpected.values()) for reply in replies),
                )
            }
            msg = f"agent reply invariant failed: duplicates={duplicates}, unexpected={unexpected}, details={details}"
            raise AssertionError(msg)


def _latency_summary(latencies: Collection[float]) -> dict[str, float]:
    """Summarize reply latencies without asserting on timing."""
    ordered = sorted(latencies)
    if not ordered:
        return {}

    def percentile(fraction: float) -> float:
        return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]

    return {
        "reply_latency_p50_s": round(percentile(0.50), 3),
        "reply_latency_p95_s": round(percentile(0.95), 3),
        "reply_latency_max_s": round(ordered[-1], 3),
    }


@dataclass(frozen=True, slots=True)
class _SentRecord:
    """One event the fuzzer wrote, for final canonical-state auditing."""

    event_id: str
    room_id: str
    event_type: str
    reaction_key: str | None = None


_CALL_ID_PREFIX = "LIVE-FUZZ call="


def _body_call_id(body: str) -> int | None:
    """Parse the model call ID a completed response body must embed."""
    if not body.startswith(_CALL_ID_PREFIX):
        return None
    digits = body[len(_CALL_ID_PREFIX) :].split(" ", 1)[0]
    return int(digits) if digits.isdigit() else None


class FinalStateAuditor:
    """Audit canonical end-state through fresh `/messages` pagination.

    `/messages` walks the resolved room DAG independently of the incremental
    `/sync` stream the oracle consumed, so this catches divergent
    interleavings, lost events, wrong redaction semantics, missing reactions,
    and incomplete final edits that a sync-only view could miss.
    """

    def __init__(
        self,
        client: LiveMatrixClient,
        oracle: ExactReplyOracle,
        *,
        agent_id: str,
        expected_body_for: Callable[[int], str],
    ) -> None:
        self.client = client
        self.oracle = oracle
        self.agent_id = agent_id
        self.expected_body_for = expected_body_for

    async def audit(
        self,
        *,
        room_ids: Collection[str],
        sent_records: Collection[_SentRecord],
        redacted_targets: Collection[str],
    ) -> dict[str, int]:
        """Run every final-state assertion and return audit metrics."""
        events: dict[str, dict[str, Any]] = {}
        for room_id in room_ids:
            for event in await self.client.paginate_room(room_id):
                event_id = event.get("event_id")
                if isinstance(event_id, str) and event_id not in events:
                    events[event_id] = event
        redacted = set(redacted_targets)
        self._assert_sent_events_canonical(events, sent_records, redacted)
        replies = self._canonical_agent_replies(events)
        self._assert_reply_cardinality(replies)
        completed = self._assert_final_bodies_complete(events, replies)
        self._assert_sync_view_parity(events, sent_records, replies)
        return {
            "audited_events": len(events),
            "audited_rooms": len(set(room_ids)),
            "completed_final_bodies": completed,
        }

    def _assert_sent_events_canonical(
        self,
        events: Mapping[str, Mapping[str, Any]],
        sent_records: Collection[_SentRecord],
        redacted: set[str],
    ) -> None:
        """Every sent event survives verbatim, redactions prune, reactions stay visible."""
        problems: list[str] = []
        for record in sent_records:
            event = events.get(record.event_id)
            if event is None:
                if record.event_id not in redacted:
                    problems.append(f"missing from /messages: {record.event_id} ({record.event_type})")
                continue
            content = event.get("content")
            content = content if isinstance(content, dict) else {}
            if record.event_id in redacted:
                if content.get("body") is not None or content.get("m.relates_to") is not None:
                    problems.append(f"redacted event kept visible content: {record.event_id}")
                continue
            if record.event_type == "m.reaction":
                relation = content.get("m.relates_to")
                key = relation.get("key") if isinstance(relation, dict) else None
                if key != record.reaction_key:
                    problems.append(
                        f"reaction {record.event_id} lost its key: expected {record.reaction_key!r}, got {key!r}",
                    )
            elif record.event_type == "m.room.message" and not isinstance(content.get("body"), str):
                problems.append(f"message {record.event_id} lost its body")
        if problems:
            msg = f"final Matrix state audit failed: {problems}"
            raise AssertionError(msg)

    def _canonical_agent_replies(self, events: Mapping[str, Mapping[str, Any]]) -> dict[str, set[str]]:
        """Index canonical agent originals by source from the paginated view."""
        replies: dict[str, set[str]] = defaultdict(set)
        for event_id, event in events.items():
            if event.get("sender") != self.agent_id or event.get("type") != "m.room.message":
                continue
            content = event.get("content")
            if not isinstance(content, dict):
                continue
            relation = content.get("m.relates_to")
            if not isinstance(relation, dict) or relation.get("rel_type") != "m.thread":
                continue
            reply = relation.get("m.in_reply_to")
            source_event_id = reply.get("event_id") if isinstance(reply, dict) else None
            if isinstance(source_event_id, str):
                replies[source_event_id].add(event_id)
        return replies

    def _assert_reply_cardinality(self, replies: Mapping[str, set[str]]) -> None:
        """Server-canonical replies must match the exact-cardinality contract."""
        oracle = self.oracle
        problems: list[str] = []
        for source_event_id, logical_ref in oracle.expected_sources.items():
            count = len(replies.get(source_event_id, ()))
            if source_event_id in oracle.optional_sources:
                if count > 1:
                    problems.append(f"redacted source {logical_ref} has {count} replies")
            elif count != 1:
                problems.append(f"source {logical_ref} has {count} canonical replies in /messages")
        for source_event_id, reply_ids in replies.items():
            if source_event_id in oracle.expected_sources or source_event_id in oracle.internal_source_ids:
                continue
            problems.append(f"unexpected agent replies to {source_event_id}: {sorted(reply_ids)}")
        if problems:
            msg = f"final reply cardinality audit failed: {problems}"
            raise AssertionError(msg)

    def _assert_final_bodies_complete(
        self,
        events: Mapping[str, Mapping[str, Any]],
        replies: Mapping[str, set[str]],
    ) -> int:
        """The latest edit of every required reply is one exact completed stream."""
        problems: list[str] = []
        checked = 0
        for source_event_id, logical_ref in self.oracle.expected_sources.items():
            if source_event_id in self.oracle.optional_sources:
                continue
            for reply_event_id in replies.get(source_event_id, ()):
                body = self._latest_agent_body(events, reply_event_id)
                call_id = _body_call_id(body)
                if call_id is None or body != self.expected_body_for(call_id):
                    problems.append(
                        f"reply to {logical_ref} ended with a non-canonical body: {body[:120]!r}",
                    )
                else:
                    checked += 1
        if problems:
            msg = f"final response body audit failed: {problems}"
            raise AssertionError(msg)
        return checked

    def _latest_agent_body(self, events: Mapping[str, Mapping[str, Any]], reply_event_id: str) -> str:
        """Return the newest visible body for one agent reply."""
        candidates: list[tuple[int, str, str]] = []
        for event_id, event in events.items():
            if event.get("sender") != self.agent_id:
                continue
            content = event.get("content")
            if not isinstance(content, dict):
                continue
            relation = content.get("m.relates_to")
            is_original = event_id == reply_event_id
            is_edit = (
                isinstance(relation, dict)
                and relation.get("rel_type") == "m.replace"
                and relation.get("event_id") == reply_event_id
            )
            if not is_original and not is_edit:
                continue
            new_content = content.get("m.new_content")
            body_source = new_content if isinstance(new_content, dict) else content
            body = body_source.get("body")
            if isinstance(body, str):
                timestamp = event.get("origin_server_ts")
                candidates.append((timestamp if isinstance(timestamp, int) else 0, event_id, body))
        return max(candidates, default=(0, "", ""))[2]

    def _assert_sync_view_parity(
        self,
        events: Mapping[str, Mapping[str, Any]],
        sent_records: Collection[_SentRecord],
        replies: Mapping[str, set[str]],
    ) -> None:
        """Everything `/messages` proves must also have crossed the oracle's `/sync`."""
        seen = self.oracle.seen_event_ids
        missing = [
            record.event_id for record in sent_records if record.event_id in events and record.event_id not in seen
        ]
        missing.extend(
            reply_event_id
            for reply_ids in replies.values()
            for reply_event_id in reply_ids
            if reply_event_id not in seen
        )
        if missing:
            msg = f"events visible in /messages never crossed incremental /sync: {sorted(missing)}"
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
        pending_grace: float = 1.0,
    ) -> None:
        self.stack = stack
        self.clients = clients
        self.client = clients[0]
        self.scenario = scenario
        self.reply_timeout = reply_timeout
        self.settle_seconds = settle_seconds
        self.pending_grace = pending_grace
        self.oracle = ExactReplyOracle(
            self.client,
            stack.agent_id,
            internal_relay_senders=(stack.router_id,),
        )
        self.event_ids: dict[str, str] = {}
        self.sent_payloads: dict[str, _SentPayload] = {}
        self.sent_records: list[_SentRecord] = []
        self.redacted_targets: set[str] = set()
        self.operation_count = 0
        self.restart_count = 0
        self.tuwunel_restart_count = 0
        self.outage_count = 0
        self.executed_batches = 0
        self.max_unsettled = 0
        self._mindroom_running = True

    async def run(self) -> dict[str, object]:
        """Execute every batch and enforce the reply invariant after each."""
        await asyncio.gather(*(client.register() for client in self.clients))
        await asyncio.gather(*(client.join_room() for client in self.clients))
        if self.scenario.profile == "saturation":
            await asyncio.gather(
                *(client.sync_incremental(timeout_ms=0, allow_limited=True) for client in self.clients),
            )
            return await self._run_saturation()

        await self.oracle.initialize()
        await self._send_roots(range(self.scenario.thread_count))
        if self.scenario.profile == "chaos":
            return await self._run_chaos()
        return await self._run_batches(
            self.scenario.batches,
        )

    async def _run_saturation(self) -> dict[str, object]:
        """Run hot and parallel turns without cross-thread barriers."""
        parallel_start = self._saturation_parallel_start()
        expected_sources: set[str] = set()

        hot_root, hot_response = await self._saturation_turn(
            self.clients[0],
            label="hot-root",
            thread_root=None,
            reply_to=None,
            expected_sources=expected_sources,
        )
        for batch in self.scenario.batches[:parallel_start]:
            operation = batch[0]
            _, hot_response = await self._saturation_turn(
                self.clients[0],
                label=operation.event_ref,
                thread_root=hot_root,
                reply_to=hot_response,
                expected_sources=expected_sources,
            )
            self.operation_count += 1
            self.executed_batches += 1

        parallel_batches = self.scenario.batches[parallel_start:]

        async def run_parallel_thread(thread: int) -> None:
            client = self._client_for_thread(thread)
            root, response = await self._saturation_turn(
                client,
                label=f"root:{thread}",
                thread_root=None,
                reply_to=None,
                expected_sources=expected_sources,
            )
            for batch in parallel_batches:
                operation = next(item for item in batch if item.thread == thread)
                _, response = await self._saturation_turn(
                    client,
                    label=operation.event_ref,
                    thread_root=root,
                    reply_to=response,
                    expected_sources=expected_sources,
                )
                self.operation_count += 1

        await asyncio.gather(
            *(run_parallel_thread(thread) for thread in range(1, self.scenario.thread_count)),
        )
        self.executed_batches += len(parallel_batches)

        # A duplicate response may finish just after its twin. Let all model
        # streams settle, then audit the union of every sender's sync history.
        await asyncio.sleep(max(self.settle_seconds, 1.0))
        await asyncio.gather(
            *(client.sync_incremental(timeout_ms=0, allow_limited=True) for client in self.clients),
        )
        all_events = {event_id: event for client in self.clients for event_id, event in client.seen_events.items()}
        response_ids = self._canonical_response_ids(all_events.values())
        duplicates = {
            source_event_id: sorted(event_ids)
            for source_event_id, event_ids in response_ids.items()
            if source_event_id in expected_sources and len(event_ids) != 1
        }
        missing = sorted(expected_sources - response_ids.keys())
        unexpected = {
            source_event_id: sorted(event_ids)
            for source_event_id, event_ids in response_ids.items()
            if source_event_id not in expected_sources
        }
        if duplicates or missing or unexpected:
            msg = (
                "saturation reply invariant failed: "
                f"duplicates={duplicates}, missing={missing}, unexpected={unexpected}"
            )
            raise AssertionError(msg)

        return {
            "batches": self.executed_batches,
            "canonical_agent_replies": len(expected_sources),
            "operations": self.operation_count,
            "restarts": 0,
            "roots": self.scenario.thread_count,
            "status": "PASS",
        }

    async def _saturation_turn(
        self,
        client: LiveMatrixClient,
        *,
        label: str,
        thread_root: str | None,
        reply_to: str | None,
        expected_sources: set[str],
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
        )
        txn_id = f"live-saturation-{label}-{secrets.token_hex(4)}"
        source_event_id = await client.send_event("m.room.message", txn_id, content)
        expected_sources.add(source_event_id)
        root_event_id = thread_root or source_event_id
        response_event_id = await self._wait_for_completed_response(
            client,
            root_event_id=root_event_id,
            source_event_id=source_event_id,
        )
        return root_event_id, response_event_id

    async def _wait_for_completed_response(
        self,
        client: LiveMatrixClient,
        *,
        root_event_id: str,
        source_event_id: str,
    ) -> str:
        """Wait until one source has exactly one fully streamed response."""
        deadline = time.monotonic() + self.reply_timeout
        while time.monotonic() < deadline:
            response_ids = self._canonical_response_ids(
                client.seen_events.values(),
                root_event_id=root_event_id,
            ).get(source_event_id, set())
            if len(response_ids) > 1:
                msg = f"duplicate agent replies for {source_event_id}: {sorted(response_ids)}"
                raise AssertionError(msg)
            if len(response_ids) == 1:
                response_event_id = next(iter(response_ids))
                if "END call=" in self._latest_event_body(client.seen_events.values(), response_event_id):
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
            if not isinstance(relation, dict) or relation.get("rel_type") != "m.thread":
                continue
            if root_event_id is not None and relation.get("event_id") != root_event_id:
                continue
            in_reply_to = relation.get("m.in_reply_to")
            source_event_id = in_reply_to.get("event_id") if isinstance(in_reply_to, dict) else None
            if isinstance(source_event_id, str):
                response_ids[source_event_id].add(event_id)
        return response_ids

    @staticmethod
    def _latest_event_body(
        events: Collection[Mapping[str, Any]],
        response_event_id: str,
    ) -> str:
        """Return the newest original or edit body for one response."""
        candidates: list[tuple[int, str]] = []
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
                candidates.append((timestamp if isinstance(timestamp, int) else 0, body))
        return max(candidates, default=(0, ""))[1]

    async def _run_batches(
        self,
        batches: tuple[tuple[LiveOperation, ...], ...],
        *,
        batch_index_offset: int = 0,
    ) -> dict[str, object]:
        """Run one contiguous scenario segment against already-created roots."""
        for relative_batch_index, batch in enumerate(batches):
            batch_index = batch_index_offset + relative_batch_index
            if batch[0].kind is LiveOperationKind.RESTART_MINDROOM:
                self.stack.restart_mindroom()
                self.restart_count += 1
            else:
                results = await asyncio.gather(*(self._apply(operation) for operation in batch))
                self._record_batch_results(results)
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
            "restarts": self.restart_count,
            "roots": self.scenario.thread_count,
            "status": "PASS",
        }

    def _record_batch_results(
        self,
        results: Collection[tuple[LiveOperation, str | None, _SentPayload | None]],
    ) -> None:
        """Register sent events and payloads for one completed batch."""
        for operation, event_id, payload in results:
            self.operation_count += 1
            if event_id is not None and operation.kind is not LiveOperationKind.IDEMPOTENT_RETRY:
                self.event_ids[operation.event_ref] = event_id
            if payload is not None:
                self.sent_payloads[operation.event_ref] = payload

    async def _run_chaos(self) -> dict[str, object]:
        """Run sustained overlapping load, settling only at explicit checkpoints."""
        for batch_index, batch in enumerate(self.scenario.batches):
            first = batch[0]
            if first.kind in LIFECYCLE_KINDS:
                await self._apply_lifecycle(first.kind, batch_index)
            else:
                results = await asyncio.gather(*(self._apply(operation) for operation in batch))
                self._record_batch_results(results)
                try:
                    await self.oracle.pump()
                except AssertionError as exc:
                    msg = f"{exc} after chaos batch {batch_index}"
                    raise AssertionError(msg) from exc
            self.executed_batches += 1
            self.max_unsettled = max(self.max_unsettled, len(self.oracle.unsettled_required_sources()))

        await self._checkpoint(len(self.scenario.batches))
        auditor = FinalStateAuditor(
            self.client,
            self.oracle,
            agent_id=self.stack.agent_id,
            expected_body_for=_ModelHandler.response_text_for,
        )
        audit = await auditor.audit(
            room_ids=tuple(self.stack.room_ids.values()),
            sent_records=self.sent_records,
            redacted_targets=self.redacted_targets,
        )
        return {
            "batches": self.executed_batches,
            "canonical_agent_replies": len(self.oracle.expected_sources),
            "clients": self.scenario.client_count,
            "max_unsettled_sources": self.max_unsettled,
            "operations": self.operation_count,
            "optional_redacted_sources": len(self.oracle.optional_sources),
            "outages": self.outage_count,
            "restarts": self.restart_count,
            "rooms": self.scenario.room_count,
            "roots": self.scenario.thread_count,
            "status": "PASS",
            "tuwunel_restarts": self.tuwunel_restart_count,
            **audit,
            **_latency_summary(self.oracle.reply_latencies.values()),
        }

    async def _apply_lifecycle(self, kind: LiveOperationKind, batch_index: int) -> None:
        """Run one singleton lifecycle disruption."""
        if kind is LiveOperationKind.CHECKPOINT:
            await self._checkpoint(batch_index)
        elif kind is LiveOperationKind.RESTART_MINDROOM:
            self.stack.restart_mindroom()
            self.restart_count += 1
        elif kind is LiveOperationKind.KILL_RESTART_MINDROOM:
            self.stack.kill_restart_mindroom()
            self.restart_count += 1
        elif kind is LiveOperationKind.COLD_RESTART_MINDROOM:
            self.stack.cold_restart_mindroom()
            self.restart_count += 1
        elif kind is LiveOperationKind.RESTART_TUWUNEL:
            self.stack.restart_tuwunel()
            self.tuwunel_restart_count += 1
        elif kind is LiveOperationKind.STOP_MINDROOM:
            self.stack.stop_mindroom()
            self._mindroom_running = False
            self.outage_count += 1
        elif kind is LiveOperationKind.START_MINDROOM:
            self.stack.start_mindroom()
            self._mindroom_running = True
        else:  # pragma: no cover - validation rejects unknown lifecycle kinds
            msg = f"unsupported lifecycle operation {kind}"
            raise AssertionError(msg)

    async def _checkpoint(self, batch_index: int) -> None:
        """Require full exact settlement, scaling the deadline with backlog."""
        unsettled = len(self.oracle.unsettled_required_sources())
        deadline_seconds = self.reply_timeout + self.pending_grace * unsettled
        try:
            await self.oracle.wait_until_exact(
                deadline_seconds=deadline_seconds,
                settle_seconds=self.settle_seconds,
            )
        except AssertionError as exc:
            msg = f"{exc} at chaos checkpoint (batch {batch_index}, backlog {unsettled})"
            raise AssertionError(msg) from exc

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

    def _client_for_operation(self, operation: LiveOperation) -> LiveMatrixClient:
        """Route one operation through its authored sender."""
        if self.scenario.profile == "saturation":
            return self._client_for_thread(operation.thread)
        return self.clients[operation.client]

    def _room_for_thread(self, thread: int) -> str:
        """Return the real room ID hosting one logical thread."""
        room_key = self.stack.room_keys[self.scenario.room_index(thread)]
        return self.stack.room_ids.get(room_key, self.stack.room_id) or self.stack.room_id

    async def _resolve_target(self, logical_ref: str) -> str:
        """Resolve a target, waiting for a live response when chaos allows it."""
        if not logical_ref.startswith("response:"):
            return self._resolve_event_ref(logical_ref)
        try:
            return self.oracle.resolve_response_ref(logical_ref)
        except KeyError:
            if self.scenario.profile != "chaos" or not self._mindroom_running:
                raise
        deadline = time.monotonic() + self.reply_timeout
        while time.monotonic() < deadline:
            await self.oracle.pump(timeout_ms=300)
            try:
                return self.oracle.resolve_response_ref(logical_ref)
            except KeyError:
                continue
        msg = f"agent response never observed for {logical_ref!r}"
        raise TimeoutError(msg)

    async def _send_roots(self, threads: Collection[int]) -> None:
        async def send_root(thread: int) -> tuple[int, str, _SentPayload, float]:
            logical_ref = f"root:{thread}"
            content = self._message_content(f"Live fuzz root {thread}")
            payload = _SentPayload("m.room.message", f"live-fuzz-{logical_ref}", content)
            root_client = (
                self._client_for_thread(thread)
                if self.scenario.profile == "saturation"
                else self.clients[self.scenario.root_client(thread)]
            )
            room_id = self._room_for_thread(thread)
            event_id = await root_client.send_event(
                payload.event_type,
                payload.txn_id,
                payload.content,
                room_id=room_id,
            )
            self.sent_records.append(_SentRecord(event_id, room_id, payload.event_type))
            return thread, event_id, payload, time.monotonic()

        roots = await asyncio.gather(*(send_root(thread) for thread in threads))
        for thread, event_id, payload, sent_at in roots:
            logical_ref = f"root:{thread}"
            self.event_ids[logical_ref] = event_id
            self.sent_payloads[logical_ref] = payload
            self.oracle.expect(logical_ref, event_id, sent_at=sent_at)
        await self.oracle.wait_until_exact(
            deadline_seconds=self.reply_timeout,
            settle_seconds=self.settle_seconds,
        )

    async def _apply(
        self,
        operation: LiveOperation,
    ) -> tuple[LiveOperation, str | None, _SentPayload | None]:
        assert operation.target is not None
        target_event_id = await self._resolve_target(operation.target)
        txn_id = f"live-fuzz-op-{operation.operation_id}"
        client = self._client_for_operation(operation)
        room_id = self._room_for_thread(operation.thread)

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
            )
            payload = _SentPayload("m.room.message", txn_id, content)
            event_id = await self._send_expected_message(operation, client, payload, room_id)
            return operation, event_id, payload

        if operation.kind is LiveOperationKind.PLAIN_REPLY:
            content = self._message_content(
                f"Live fuzz plain reply {operation.operation_id}",
                relation={"m.in_reply_to": {"event_id": target_event_id}},
            )
            payload = _SentPayload("m.room.message", txn_id, content)
            event_id = await self._send_expected_message(operation, client, payload, room_id)
            return operation, event_id, payload

        if operation.kind is LiveOperationKind.EDIT:
            new_content = self._message_content(f"Live fuzz edited message {operation.operation_id}")
            content = {
                **new_content,
                "m.new_content": new_content,
                "m.relates_to": {"rel_type": "m.replace", "event_id": target_event_id},
            }
            event_id = await client.send_event("m.room.message", txn_id, content, room_id=room_id)
            self.sent_records.append(_SentRecord(event_id, room_id, "m.room.message"))
            return operation, event_id, None

        if operation.kind is LiveOperationKind.REACTION:
            reaction_key = f"fuzz-{operation.operation_id}"
            content = {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": target_event_id,
                    "key": reaction_key,
                },
            }
            event_id = await client.send_event("m.reaction", txn_id, content, room_id=room_id)
            self.sent_records.append(_SentRecord(event_id, room_id, "m.reaction", reaction_key=reaction_key))
            return operation, event_id, None

        if operation.kind is LiveOperationKind.REDACTION:
            # A source redacted before its reply settles legitimately races the
            # in-flight response, so its exact cardinality becomes zero-or-one.
            if len(self.oracle.response_ids.get(target_event_id, ())) != 1:
                self.oracle.mark_source_optional(target_event_id)
            self.redacted_targets.add(target_event_id)
            event_id = await client.redact(target_event_id, txn_id, room_id=room_id)
            return operation, event_id, None

        payload = self.sent_payloads[operation.target]
        event_id = await client.send_event(payload.event_type, payload.txn_id, payload.content, room_id=room_id)
        if event_id != target_event_id:
            msg = f"idempotent retry changed event ID for {operation.target}: {target_event_id} -> {event_id}"
            raise AssertionError(msg)
        return operation, event_id, None

    async def _send_expected_message(
        self,
        operation: LiveOperation,
        client: LiveMatrixClient,
        payload: _SentPayload,
        room_id: str,
    ) -> str:
        """Send one reply-expecting message and register it before any sync pump.

        Concurrent target-resolution waiters pump the oracle mid-batch, so a
        fast agent reply must never be observable before its expectation exists.
        """
        event_id = await client.send_event(payload.event_type, payload.txn_id, payload.content, room_id=room_id)
        self.sent_records.append(_SentRecord(event_id, room_id, payload.event_type))
        self.oracle.expect(operation.event_ref, event_id, sent_at=time.monotonic())
        return event_id

    def _resolve_event_ref(self, logical_ref: str) -> str:
        if logical_ref.startswith("response:"):
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
    ) -> dict[str, Any]:
        content: dict[str, Any] = {
            "msgtype": "m.text",
            "body": f"{body} {self.stack.agent_id}",
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
    parser.add_argument("--profile", choices=("fuzz", "saturation", "chaos"), default="fuzz")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--steps", type=_positive_int, default=200)
    parser.add_argument("--threads", type=_positive_int, default=45)
    parser.add_argument("--max-batch-size", type=_positive_int, default=16)
    parser.add_argument("--restart-interval", type=_non_negative_int, default=100)
    parser.add_argument("--clients", type=_positive_int, default=4, help="chaos senders racing concurrently")
    parser.add_argument("--rooms", type=_positive_int, default=2, help="chaos rooms hosting threads")
    parser.add_argument("--hot-thread-weight", type=_positive_int, default=6)
    parser.add_argument("--checkpoint-interval", type=_non_negative_int, default=40)
    parser.add_argument("--lifecycle-interval", type=_non_negative_int, default=70)
    parser.add_argument("--downtime-batches", type=_non_negative_int, default=2)
    parser.add_argument("--pending-grace", type=float, default=1.0)
    parser.add_argument(
        "--reply-timeout",
        type=float,
        help="per-reply deadline (default: 60s fuzz, 180s saturation, 90s chaos)",
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
    pending_grace: float = 1.0,
) -> dict[str, object]:
    client_count = scenario.thread_count - 1 if scenario.profile == "saturation" else scenario.client_count
    room_ids = tuple(stack.room_ids.get(room_key, stack.room_id) for room_key in stack.room_keys)
    clients = tuple(LiveMatrixClient(stack.homeserver, stack.room_id, room_ids=room_ids) for _ in range(client_count))
    if scenario.profile == "chaos":
        for client in clients:
            client.transport_retry_seconds = 45.0
    try:
        return await LiveFuzzRunner(
            stack,
            clients,
            scenario,
            reply_timeout=reply_timeout,
            settle_seconds=settle_seconds,
            pending_grace=pending_grace,
        ).run()
    finally:
        await asyncio.gather(*(client.close() for client in clients))


def _scenario_from_args(args: argparse.Namespace) -> LiveFuzzScenario:
    """Build or load the requested trace."""
    if args.trace is not None:
        return LiveFuzzScenario.from_json(args.trace.read_text(encoding="utf-8"))
    if args.profile == "saturation":
        return saturation_scenario()
    if args.profile == "chaos":
        return chaos_scenario_from_seed(
            args.seed,
            steps=args.steps,
            tuning=ChaosTuning(
                thread_count=args.threads,
                client_count=args.clients,
                room_count=args.rooms,
                max_batch_size=args.max_batch_size,
                hot_thread_weight=args.hot_thread_weight,
                checkpoint_interval=args.checkpoint_interval,
                lifecycle_interval=args.lifecycle_interval,
                downtime_batches=args.downtime_batches,
            ),
        )
    return live_scenario_from_seed(
        args.seed,
        steps=args.steps,
        thread_count=args.threads,
        max_batch_size=args.max_batch_size,
        restart_interval=args.restart_interval,
    )


_PROFILE_STREAMS = {
    "fuzz": StreamProfile(),
    "saturation": StreamProfile(stream_segments=96, stream_delay=0.012),
    "chaos": StreamProfile(
        stream_segments=8,
        stream_delay=0.002,
        slow_call_modulus=7,
        slow_stream_segments=120,
        slow_stream_delay=0.05,
        first_token_delay=0.3,
    ),
}

_PROFILE_REPLY_TIMEOUTS = {"fuzz": 60.0, "saturation": 180.0, "chaos": 90.0}


def _room_keys_for(scenario: LiveFuzzScenario) -> tuple[str, ...]:
    """Return config room keys covering every scenario room."""
    return (ROOM_KEY, *(f"chaos{index}" for index in range(1, scenario.room_count)))


def main() -> None:
    """Run one trace against a fresh disposable real-server stack."""
    args = _parse_args()
    scenario = _scenario_from_args(args)
    if args.save_trace is not None:
        args.save_trace.write_text(scenario.to_json() + "\n", encoding="utf-8")
    reply_timeout = args.reply_timeout
    if reply_timeout is None:
        reply_timeout = _PROFILE_REPLY_TIMEOUTS[scenario.profile]

    stack = ManagedTuwunelStack(
        stream_profile=_PROFILE_STREAMS[scenario.profile],
        room_keys=_room_keys_for(scenario),
    )
    try:
        stack.start()
        started_at = time.monotonic()
        result = asyncio.run(
            _run_live(
                stack,
                scenario,
                reply_timeout=reply_timeout,
                settle_seconds=args.settle_seconds,
                pending_grace=args.pending_grace,
            ),
        )
        result["seed"] = args.seed if args.trace is None else "trace"
        result["wall_seconds"] = round(time.monotonic() - started_at, 1)
        result.update(stack.diagnostic_counts())
        print(json.dumps(result, sort_keys=True))
    except Exception:
        print("Live Matrix fuzz trace:", file=sys.stderr)
        print(args.trace or scenario.to_json(), file=sys.stderr)
        print(json.dumps(stack.diagnostic_counts(), sort_keys=True), file=sys.stderr)
        if args.failure_log is not None and stack.log_path.exists():
            args.failure_log.write_text(
                stack.log_path.read_text(encoding="utf-8", errors="replace"),
                encoding="utf-8",
            )
        log_tail = stack.log_tail()
        if log_tail:
            print("MindRoom log tail:", file=sys.stderr)
            print(log_tail, file=sys.stderr)
        raise
    finally:
        stack.close()


if __name__ == "__main__":
    main()
