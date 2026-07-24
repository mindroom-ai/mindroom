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
import fcntl
import hashlib
import itertools
import json
import os
import random
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from contextlib import suppress
from dataclasses import asdict, dataclass
from enum import StrEnum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast
from urllib.parse import quote

import httpx
import nio
import yaml

import mindroom
from mindroom.dispatch_source import AUTO_RESUME_MESSAGE
from mindroom.handled_turns import TurnRecord, TurnRecordCodec
from mindroom.streaming import INTERRUPTED_RESPONSE_NOTE, RESTART_INTERRUPTED_RESPONSE_NOTE

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping
    from io import TextIOWrapper

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTANCE_REGISTRY = PROJECT_ROOT / "local" / "instances" / "deploy" / "instances.json"
DEFAULT_LIVE_FUZZ_STATE_ROOT = Path.home() / ".mindroom" / "live-fuzz"
MODEL_ID = "mindroom-live-fuzz"
AGENT_NAME = "general"
ROOM_KEY = "lobby"
LIFECYCLE_COMMAND_TIMEOUT_SECONDS = 180.0


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
    unusable_responses: set[str]
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
        """Serialize the complete logical workload for replay on a fresh server."""
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
            unusable_responses=set(),
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
        self._validate_reply_uniqueness(batch)
        self._validate_edit_uniqueness(batch)
        self._validate_redaction_uniqueness(batch)
        self._validate_redaction_response_races(batch, state)
        for operation in batch:
            self._validate_mutation_operation(operation, state)
        self._register_batch_events(batch, state)

    def _validate_reply_uniqueness(self, batch: tuple[LiveOperation, ...]) -> None:
        """Reject reply races the exact oracle cannot attribute."""
        reply_keys = [(operation.thread, operation.client) for operation in batch if operation.kind in MESSAGE_KINDS]
        if len(reply_keys) != len(set(reply_keys)):
            msg = "same-thread messages requiring replies must use separate batches"
            raise ValueError(msg)
        if self.profile != "chaos":
            reply_threads = [key[0] for key in reply_keys]
            if len(reply_threads) != len(set(reply_threads)):
                msg = "same-thread messages requiring replies must use separate batches"
                raise ValueError(msg)

    def _validate_edit_uniqueness(self, batch: tuple[LiveOperation, ...]) -> None:
        """Reject two concurrent edits of one source the auditor cannot resolve.

        Same-batch edits of a shared target land in nondeterministic Matrix
        order, so the surviving revision is unknowable and the final-body audit
        would flap.
        """
        edited = [operation.target for operation in batch if operation.kind is LiveOperationKind.EDIT]
        if len(edited) != len(set(edited)):
            msg = "one source may be edited at most once per batch"
            raise ValueError(msg)

    def _validate_redaction_uniqueness(self, batch: tuple[LiveOperation, ...]) -> None:
        """Reject duplicate concurrent redactions with nondeterministic provenance."""
        redacted = [operation.target for operation in batch if operation.kind is LiveOperationKind.REDACTION]
        if len(redacted) != len(set(redacted)):
            msg = "one event may be redacted at most once per batch"
            raise ValueError(msg)

    def _register_batch_events(self, batch: tuple[LiveOperation, ...], state: _ValidationState) -> None:
        """Fold one validated batch into the cross-batch bookkeeping."""
        for operation in batch:
            if operation.kind is not LiveOperationKind.IDEMPOTENT_RETRY:
                state.known_events.add(operation.event_ref)
                state.authors[operation.event_ref] = operation.client
            if operation.kind in MESSAGE_KINDS:
                state.message_events.add(operation.event_ref)
                state.known_responses.add(f"response:{operation.event_ref}")
        for operation in batch:
            if operation.kind is LiveOperationKind.REDACTION:
                assert operation.target is not None
                redacted_response = f"response:{operation.target}"
                if redacted_response in state.known_responses and redacted_response not in state.settled_responses:
                    state.unusable_responses.add(redacted_response)
        if self.profile != "chaos":
            # The fuzz runner settles every reply after each batch, so all
            # responses are proven to exist before the next batch starts.
            state.settled_responses = {
                f"response:{message}" for message in state.message_events
            } - state.unusable_responses

    def _validate_redaction_response_races(
        self,
        batch: tuple[LiveOperation, ...],
        state: _ValidationState,
    ) -> None:
        """Reject batches racing a redaction against its own unsettled reply."""
        redacted_messages = {
            operation.target
            for operation in batch
            if operation.kind is LiveOperationKind.REDACTION and operation.target in state.message_events
        }
        unsettled_response_targets = {
            operation.target
            for operation in batch
            if operation.target is not None
            and operation.target.startswith("response:")
            and operation.target not in state.settled_responses
        }
        conflicts = {f"response:{message}" for message in redacted_messages} & unsettled_response_targets
        if conflicts:
            msg = f"cannot target unsettled responses of same-batch redacted sources: {sorted(conflicts)}"
            raise ValueError(msg)

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
        settled_when_quiet = {f"response:{message}" for message in state.message_events} - state.unusable_responses
        if kind is LiveOperationKind.CHECKPOINT:
            state.settled_responses = settled_when_quiet
            return
        if kind is LiveOperationKind.COLD_RESTART_MINDROOM and state.settled_responses != settled_when_quiet:
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
        if operation.target in state.unusable_responses:
            msg = f"{operation.target!r} may never settle after its source redaction and cannot be targeted"
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
    unusable_responses: set[str]


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
        unusable_responses=set(),
    )


def _choose_operation(
    randomizer: random.Random,
    state: _ScenarioGenerationState,
    *,
    operation_id: int,
    thread_count: int,
    batch_edited: set[str],
    batch_redacted: set[str],
) -> LiveOperation:
    thread = randomizer.randrange(thread_count)
    kind = randomizer.choice(_WEIGHTED_KINDS)
    # Two same-batch edits of one source race to a nondeterministic surviving
    # revision, so a target already edited this batch is off limits.
    available_edits = [
        target for target in state.editable[thread] if target not in state.redacted and target not in batch_edited
    ]
    available_redactions = [
        target for target in state.redactable[thread] if target not in state.redacted and target not in batch_redacted
    ]
    available_retries = [target for target in state.messages[thread] if target not in state.redacted]

    if kind is LiveOperationKind.THREAD_MESSAGE:
        target = randomizer.choice(state.messages[thread])
    elif kind is LiveOperationKind.PLAIN_REPLY:
        target = randomizer.choice(state.responses[thread])
    elif kind is LiveOperationKind.EDIT and available_edits:
        target = randomizer.choice(available_edits)
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
        batch_edited: set[str] = set()
        batch_redacted: set[str] = set()
        for offset in range(batch_size):
            operation = _choose_operation(
                randomizer,
                state,
                operation_id=operation_id + offset,
                thread_count=thread_count,
                batch_edited=batch_edited,
                batch_redacted=batch_redacted,
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
            elif operation.kind is LiveOperationKind.EDIT:
                assert operation.target is not None
                batch_edited.add(operation.target)
            elif operation.kind is LiveOperationKind.REDACTION:
                assert operation.target is not None
                batch_redacted.add(operation.target)
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
            } - self.state.unusable_responses


def _pick_chaos_thread(build: _ChaosBuild) -> int:
    """Pick a thread with the hot thread over-weighted."""
    tuning = build.tuning
    index = build.randomizer.randrange(tuning.thread_count + tuning.hot_thread_weight - 1)
    return 0 if index < tuning.hot_thread_weight else index - tuning.hot_thread_weight + 1


def _response_target_allowed(
    state: _ScenarioGenerationState,
    target: str,
    *,
    mindroom_running: bool,
    batch_redacted: set[str],
) -> bool:
    """Return whether one `response:` reference is safe to target right now."""
    if target in state.settled_responses:
        return True
    if target in state.unusable_responses:
        return False
    source = target.removeprefix("response:")
    if source in state.redacted or source in batch_redacted:
        return False
    return mindroom_running


def _choose_chaos_operation(
    build: _ChaosBuild,
    *,
    mindroom_running: bool,
    batch_redacted: set[str],
    batch_response_sources: set[str],
    batch_edited: set[str],
) -> LiveOperation:
    """Choose one realistic operation honoring downtime and authorship rules."""
    randomizer = build.randomizer
    state = build.state
    thread = _pick_chaos_thread(build)
    kind = randomizer.choice(_WEIGHTED_KINDS)
    random_client = randomizer.randrange(build.tuning.client_count)

    def response_available(target: str) -> bool:
        return _response_target_allowed(
            state,
            target,
            mindroom_running=mindroom_running,
            batch_redacted=batch_redacted,
        )

    available_responses = [target for target in state.responses[thread] if response_available(target)]
    available_reactions = [
        target
        for target in state.reaction_targets[thread]
        if not target.startswith("response:") or response_available(target)
    ]
    # Two concurrent edits of one source race to the last surviving Matrix
    # revision, so their final body is nondeterministic. Forbid a second
    # same-batch edit of a target already edited this batch.
    available_edits = [
        target for target in state.editable[thread] if target not in state.redacted and target not in batch_edited
    ]
    available_redactions = [
        target
        for target in state.redactable[thread]
        if target not in state.redacted
        and target not in batch_redacted
        # Never race a redaction against a same-batch target of its own
        # unsettled response, or the resolver could wait forever.
        and not (target in batch_response_sources and f"response:{target}" not in state.settled_responses)
    ]
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
    state = build.state
    batch_size = min(remaining, build.randomizer.randint(1, build.tuning.max_batch_size))
    operations: list[LiveOperation] = []
    reply_keys: set[tuple[int, int]] = set()
    batch_redacted: set[str] = set()
    batch_response_sources: set[str] = set()
    batch_edited: set[str] = set()
    for _ in range(batch_size):
        operation = _choose_chaos_operation(
            build,
            mindroom_running=mindroom_running,
            batch_redacted=batch_redacted,
            batch_response_sources=batch_response_sources,
            batch_edited=batch_edited,
        )
        if operation.kind in MESSAGE_KINDS and (operation.thread, operation.client) in reply_keys:
            operation = LiveOperation(
                operation_id=operation.operation_id,
                kind=LiveOperationKind.REACTION,
                thread=operation.thread,
                target=build.randomizer.choice(
                    [
                        target
                        for target in state.reaction_targets[operation.thread]
                        if not target.startswith("response:")
                        or _response_target_allowed(
                            state,
                            target,
                            mindroom_running=mindroom_running,
                            batch_redacted=batch_redacted,
                        )
                    ],
                ),
                client=operation.client,
            )
        if operation.kind in MESSAGE_KINDS:
            reply_keys.add((operation.thread, operation.client))
        assert operation.target is not None
        if operation.kind is LiveOperationKind.REDACTION:
            batch_redacted.add(operation.target)
        elif operation.kind is LiveOperationKind.EDIT:
            batch_edited.add(operation.target)
        elif operation.target.startswith("response:"):
            batch_response_sources.add(operation.target.removeprefix("response:"))
        operations.append(operation)
    build.batches.append(tuple(operations))
    _update_generation_state(state, operations)
    for operation in operations:
        if operation.kind is LiveOperationKind.REDACTION:
            assert operation.target is not None
            redacted_response = f"response:{operation.target}"
            if (
                redacted_response in state.responses[operation.thread]
                and redacted_response not in state.settled_responses
            ):
                state.unusable_responses.add(redacted_response)
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


ORIGINAL_REVISION = "orig"
_MARKER_PATTERN = re.compile(r"MRK\[src=[^;\]]+;rev=[^\]]+\]")


def _source_marker(source: str, revision: str) -> str:
    """Return a stable token binding one source body to a logical source and revision.

    The token is embedded verbatim in a fuzz USER body so it survives the round
    trip through Matrix and reaches the model stub. It intentionally does not
    start with ``LIVE-FUZZ call=`` so it can never be mistaken for a model
    response body by ``_body_call_id``.
    """
    return f"MRK[src={source};rev={revision}]"


def _parse_markers(text: str) -> frozenset[str]:
    """Extract every ``MRK[...]`` token from a string as full token strings."""
    return frozenset(_MARKER_PATTERN.findall(text))


def _marker_fingerprint(markers: frozenset[str]) -> int:
    """Return a stable non-negative fingerprint of one marker set.

    Uses a content hash of the sorted tokens so the slow/fast profile a source
    receives is a pure function of its markers, identical regardless of the
    order concurrent model requests arrive and stable across processes.
    """
    digest = hashlib.blake2b("\n".join(sorted(markers)).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


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

    # Class-level observation map guarded by a lock because the stub runs under a
    # ThreadingHTTPServer: concurrent MindRoom requests each land in their own
    # handler thread. Each entry records the source-revision markers seen on the
    # FINAL user message of one model call, keyed by that call's assigned id.
    _observation_lock = threading.Lock()
    _observed_markers: ClassVar[dict[int, frozenset[str]]] = {}

    @classmethod
    def reset_observations(cls) -> None:
        """Clear observed markers and restart call-id numbering for a fresh stack."""
        with cls._observation_lock:
            cls._observed_markers = {}
        cls.call_ids = itertools.count(1)

    @classmethod
    def _record_observation(cls, call_id: int, markers: frozenset[str]) -> None:
        with cls._observation_lock:
            cls._observed_markers[call_id] = markers

    @classmethod
    def observed_markers_for(cls, call_id: int) -> frozenset[str]:
        """Return the markers observed on one model call's final user message."""
        with cls._observation_lock:
            return cls._observed_markers.get(call_id, frozenset())

    @classmethod
    def observations_snapshot(cls) -> dict[int, list[str]]:
        """Return every recorded call's markers for durable failure evidence."""
        with cls._observation_lock:
            return {call_id: sorted(markers) for call_id, markers in cls._observed_markers.items()}

    @classmethod
    def _is_slow_call(cls, call_id: int) -> bool:
        """Decide slow vs fast purely from the call's observed marker fingerprint.

        Deriving the profile from a stable hash of the parsed marker set (not
        the HTTP arrival order) means reversing the order concurrent requests
        reach the stub never changes which source streams slowly. A call with no
        markers (an internal relay or system call) is always fast.
        """
        if cls.slow_call_modulus <= 0:
            return False
        markers = cls.observed_markers_for(call_id)
        if not markers:
            return False
        return _marker_fingerprint(markers) % cls.slow_call_modulus == 0

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

    @staticmethod
    def _final_user_markers(payload: Mapping[str, object]) -> frozenset[str]:
        """Return the markers on the final user message only.

        MindRoom sends conversation history as earlier messages and the current
        turn as the last ``role == "user"`` entry, so scanning only that entry
        prevents a stale history marker from masking a wrong current turn.
        """
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return frozenset()
        for raw_message in reversed(messages):
            if not isinstance(raw_message, dict):
                continue
            message = cast("dict[str, object]", raw_message)
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return _parse_markers(content)
            if isinstance(content, list):
                parts = [cast("dict[str, object]", part).get("text", "") for part in content if isinstance(part, dict)]
                return _parse_markers(" ".join(text for text in parts if isinstance(text, str)))
            return frozenset()
        return frozenset()

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length))
        call_id = next(self.call_ids)
        self._record_observation(call_id, self._final_user_markers(payload))
        content = self._response_text(call_id)
        if self._is_slow_call(call_id) and self.first_token_delay > 0:
            time.sleep(self.first_token_delay)
        if payload.get("stream") is True:
            try:
                self._send_stream(call_id, content)
            except OSError:
                # Chaos intentionally kills MindRoom with model streams in
                # flight. The abandoned HTTP connection is expected and must
                # not escape from the request-handler thread.
                self.close_connection = True
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


def _run_command(
    *command: str,
    timeout_seconds: float = LIFECYCLE_COMMAND_TIMEOUT_SECONDS,
    cwd: Path = PROJECT_ROOT,
) -> str:
    """Run one bounded lifecycle command in its own killable process group."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        msg = f"command timed out ({' '.join(command)}):\n{stdout}\n{stderr}"
        raise TimeoutError(msg) from exc
    except BaseException:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
        raise
    if process.returncode:
        msg = f"command failed ({' '.join(command)}):\n{stdout}\n{stderr}"
        raise RuntimeError(msg)
    return stdout


def _attempt_cleanup(
    errors: list[Exception],
    label: str,
    action: Callable[[], object],
) -> None:
    """Run one teardown stage while retaining its failure."""
    try:
        action()
    except BaseException as exc:
        errors.append(RuntimeError(f"{label}: {exc}"))


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
        provenance_sink: Callable[[Mapping[str, object]], None] | None = None,
        artifact_directory: Path | None = None,
        state_root: Path = DEFAULT_LIVE_FUZZ_STATE_ROOT,
    ) -> None:
        token = secrets.token_hex(4)
        self._stream_profile = stream_profile or StreamProfile()
        self.instance_name = f"fuzz{token}"
        self.namespace = self.instance_name
        self.state_root = state_root
        self.manifest_path = state_root / "runs" / f"{self.instance_name}.json"
        self.artifact_directory = artifact_directory
        self.temp_dir = tempfile.TemporaryDirectory(prefix="mindroom-live-matrix-fuzz-")
        self.root = Path(self.temp_dir.name)
        self.storage_path = self.root / "mindroom_data"
        self.config_path = self.root / "config.yaml"
        self.log_path = self.root / "mindroom.log"
        self.attestation_path = self.root / "runtime-attestation.json"
        self.runtime_provenance: dict[str, object] | None = None
        self.api_port = 0
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
        self._provenance_sink = provenance_sink
        self._host_lease: TextIOWrapper | None = None

    def _acquire_host_lease(self) -> None:
        """Serialize live stacks across worktrees and retain crash ownership."""
        self.state_root.mkdir(parents=True, exist_ok=True)
        lease = (self.state_root / "host.lock").open("a+", encoding="utf-8")
        try:
            fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lease.close()
            msg = "another host-wide MindRoom live fuzz stack owns the durable lease"
            raise RuntimeError(msg) from None
        self._host_lease = lease

    def _write_manifest(self, *, state: str, **fields: object) -> None:
        """Atomically persist exact resources for crash recovery."""
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {}
        if self.manifest_path.exists():
            existing = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                payload.update(existing)
        payload.update(
            {
                "artifact_directory": str(self.artifact_directory) if self.artifact_directory is not None else None,
                "harness_pid": os.getpid(),
                "instance_name": self.instance_name,
                "mindroom_command_marker": str(self.attestation_path),
                "project_root": str(PROJECT_ROOT),
                "state": state,
                **fields,
            },
        )
        temporary = self.manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.manifest_path)

    def _recover_abandoned_runs(self) -> None:
        """Remove exact resources left by a prior lease owner that crashed."""
        runs = self.state_root / "runs"
        if not runs.exists():
            return
        for manifest_path in sorted(runs.glob("fuzz*.json")):
            if manifest_path == self.manifest_path:
                continue
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("state") in {"closed", "recovered"}:
                continue
            instance_name = payload.get("instance_name")
            project_root = payload.get("project_root")
            if (
                not isinstance(instance_name, str)
                or not instance_name.startswith("fuzz")
                or not isinstance(project_root, str)
            ):
                msg = f"invalid abandoned live-fuzz manifest: {manifest_path}"
                raise RuntimeError(msg)
            old_root = Path(project_root)
            if not old_root.exists():
                msg = f"abandoned live-fuzz worktree is unavailable: {old_root}"
                raise RuntimeError(msg)
            self._terminate_recorded_mindroom(payload)
            registry_path = old_root / "local" / "instances" / "deploy" / "instances.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else {}
            instances = registry.get("instances", {}) if isinstance(registry, dict) else {}
            if isinstance(instances, dict) and instance_name in instances:
                _run_command(
                    "just",
                    "local-instances-remove",
                    instance_name,
                    cwd=old_root,
                )
            payload["state"] = "recovered"
            temporary = manifest_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temporary.replace(manifest_path)

    @staticmethod
    def _terminate_recorded_mindroom(payload: Mapping[str, object]) -> None:
        """Kill only the exact attested process group retained by one manifest."""
        pid = payload.get("mindroom_pid")
        marker = payload.get("mindroom_command_marker")
        if not isinstance(pid, int) or not isinstance(marker, str):
            return
        result = subprocess.run(
            ("ps", "-axo", "pid=,pgid=,command="),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        group_commands: list[str] = []
        for line in result.stdout.splitlines():
            fields = line.strip().split(maxsplit=2)
            if len(fields) != 3:
                continue
            _, pgid, command = fields
            if pgid.isdigit() and int(pgid) == pid:
                group_commands.append(command)
        if not group_commands:
            return
        if not any(marker in command and "__mindroom_runtime_child__" in command for command in group_commands):
            msg = f"refusing to kill unverified abandoned process group {pid}"
            raise RuntimeError(msg)
        with suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)

    def _release_host_lease(self) -> None:
        """Release the host lease after exact resource cleanup is attempted."""
        lease = self._host_lease
        if lease is None:
            return
        fcntl.flock(lease.fileno(), fcntl.LOCK_UN)
        lease.close()
        self._host_lease = None

    def start(self) -> None:
        """Create every live dependency and wait for the managed room."""
        self._acquire_host_lease()
        self._recover_abandoned_runs()
        self._write_manifest(state="creating")
        _run_command("just", "local-instances-create", self.instance_name, "tuwunel")
        self._created = True
        registry = json.loads(INSTANCE_REGISTRY.read_text(encoding="utf-8"))
        instance = registry["instances"][self.instance_name]
        matrix_port = int(instance["matrix_port"])
        self.api_port = int(instance["mindroom_port"])
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
        self._write_manifest(
            state="ready",
            matrix_port=matrix_port,
            api_port=self.api_port,
            mindroom_pid=self._mindroom_process.pid if self._mindroom_process is not None else None,
        )

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
        """Attempt every teardown stage and report all cleanup failures."""
        errors: list[Exception] = []

        _attempt_cleanup(errors, "stop MindRoom", self._stop_mindroom)
        if self._log_handle is not None:
            handle = self._log_handle
            _attempt_cleanup(errors, "close MindRoom log", handle.close)
            if handle.closed:
                self._log_handle = None
        if self._model_server is not None:
            server = self._model_server
            _attempt_cleanup(errors, "stop model server", server.shutdown)
            _attempt_cleanup(errors, "close model server", server.server_close)
            self._model_server = None
        if self._model_thread is not None:
            thread = self._model_thread
            _attempt_cleanup(errors, "join model server thread", lambda: thread.join(timeout=5))
            if thread.is_alive():
                errors.append(RuntimeError("join model server thread: thread remained alive"))
            else:
                self._model_thread = None
        if self._created:
            _attempt_cleanup(
                errors,
                "remove Tuwunel instance",
                lambda: _run_command("just", "local-instances-remove", self.instance_name),
            )
            if not any(str(error).startswith("remove Tuwunel instance:") for error in errors):
                self._created = False
        _attempt_cleanup(errors, "remove temporary stack storage", self.temp_dir.cleanup)
        cleanup_state = "cleanup_failed" if errors else "closed"
        _attempt_cleanup(
            errors,
            "write cleanup manifest",
            lambda: self._write_manifest(
                state=cleanup_state,
                cleanup_errors=[str(error) for error in errors],
            ),
        )
        _attempt_cleanup(errors, "release host lease", self._release_host_lease)
        if errors:
            message = "live Matrix fuzz cleanup failed"
            raise ExceptionGroup(message, errors)

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

    def tuwunel_log(self, *, tail: int = 4000) -> str:
        """Return the homeserver container log for durable failure evidence.

        Captured before instance removal so a race-producing schedule keeps its
        server-side view. Docker failures are folded into the returned text so a
        missing log never masks the primary fuzz assertion.
        """
        if not self._created:
            return ""
        try:
            completed = subprocess.run(
                ["docker", "logs", "--tail", str(tail), f"{self.instance_name}-tuwunel"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"<tuwunel log capture failed: {exc}>"
        return completed.stdout + completed.stderr

    def _start_model_server(self) -> int:
        profile = self._stream_profile
        _ModelHandler.reset_observations()
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
        overlay_args = ("--with-editable", overlay) if overlay else ()
        self.attestation_path.unlink(missing_ok=True)
        self._mindroom_process = subprocess.Popen(
            [
                "uv",
                "run",
                *overlay_args,
                "python",
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
            start_new_session=True,
        )
        self._write_manifest(
            state="starting_mindroom",
            mindroom_pid=self._mindroom_process.pid,
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
                    self._write_manifest(
                        state="ready",
                        mindroom_pid=self._mindroom_process.pid,
                    )
                    return
            time.sleep(0.2)
        msg = f"MindRoom did not create all of {self.room_keys!r}:\n{self.log_tail()}"
        raise TimeoutError(msg)

    def _wait_for_runtime_attestation(self) -> None:
        """Capture and validate the exact modules loaded by the spawned child."""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.attestation_path.exists():
                self.runtime_provenance = _validated_child_provenance(
                    self.attestation_path,
                    overlay=os.environ.get("MINDROOM_LIVE_FUZZ_UV_WITH"),
                )
                if self._provenance_sink is not None:
                    self._provenance_sink(self.runtime_provenance)
                return
            if self._mindroom_process is not None and self._mindroom_process.poll() is not None:
                msg = f"MindRoom exited before runtime attestation:\n{self.log_tail()}"
                raise RuntimeError(msg)
            time.sleep(0.05)
        msg = "MindRoom child did not attest loaded runtime paths"
        raise TimeoutError(msg)

    def _stop_mindroom(self, *, kill: bool = False) -> None:
        process = self._mindroom_process
        if process is None:
            return
        running = process.poll() is None
        if kill or not running:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            if running:
                process.wait(timeout=10)
        else:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
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
        self.user_id = ""
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
        self.user_id = user_id
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


def read_ledger_records(ledger_path: Path) -> dict[str, TurnRecord]:
    """Read every completed handled-turn record keyed by its source event.

    A completed record with a visible ``response_event_id`` proves that source
    was answered. A completed record with ``response_event_id`` set to ``None``
    is production's exact durable proof that the source was legitimately
    skipped as a superseded replay. Missing, malformed, or ``completed=False``
    records are omitted, so the oracle can require proof of a terminal outcome
    rather than inferring supersession from chronology alone.
    """
    if not ledger_path.exists():
        return {}
    try:
        payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema_version") != TurnRecordCodec.schema_version():
        return {}
    raw_records = payload.get("records")
    if not isinstance(raw_records, dict):
        return {}
    records: dict[str, TurnRecord] = {}
    for event_id, raw_record in raw_records.items():
        if not isinstance(event_id, str):
            continue
        record = TurnRecordCodec.from_ledger_record(event_id, raw_record)
        if record is not None and record.completed:
            records[event_id] = record
    return records


class ExactReplyOracle:
    """Track canonical agent replies from real incremental `/sync` responses.

    In strict mode (fuzz and saturation), every required source must collect
    exactly one direct canonical reply. Chaos mode models MindRoom's
    active-follow-up coalescing: messages arriving during an active response
    in the same thread are answered by one combined reply targeting the
    newest queued source, so settlement requires every source observed and
    every thread's newest required source directly replied, while exact
    per-source attribution is audited afterwards from the durable turn
    ledger. In both modes, duplicate direct replies and replies to unknown
    sources fail immediately.
    """

    def __init__(
        self,
        client: LiveMatrixClient,
        agent_id: str,
        *,
        internal_relay_senders: Collection[str] = (),
        coalescing_threads: bool = False,
        ledger_path: Path | None = None,
        expected_body_for: Callable[[int], str] = _ModelHandler.response_text_for,
    ) -> None:
        self.client = client
        self.agent_id = agent_id
        self.internal_relay_senders = frozenset(internal_relay_senders)
        self.coalescing_threads = coalescing_threads
        self.ledger_path = ledger_path
        self.expected_body_for = expected_body_for
        self._ledger_records: dict[str, TurnRecord] = {}
        self._ledger_read_at = 0.0
        self.internal_source_ids: set[str] = set()
        self.next_batch: str | None = None
        self.expected_sources: dict[str, str] = {}
        self.optional_sources: set[str] = set()
        self.source_threads: dict[str, int] = {}
        self.observed_sources: set[str] = set()
        self.chains: dict[tuple[int, int], list[str]] = defaultdict(list)
        self.response_ids: dict[str, set[str]] = defaultdict(set)
        self.response_event_by_ref: dict[str, str] = {}
        # Newest visible body per agent reply (keyed by the reply event id),
        # folding in `m.replace` edits so settlement can tell a still-streaming
        # placeholder apart from a completed canonical body.
        self.latest_reply_bodies: dict[str, tuple[tuple[int, int, str], str]] = {}
        self.seen_event_ids: set[str] = set()
        self.event_summaries: dict[str, dict[str, Any]] = {}
        self.sent_at: dict[str, float] = {}
        self.reply_latencies: dict[str, float] = {}
        self._last_response_activity_at = time.monotonic()
        self._sync_lock = asyncio.Lock()
        self._pending_expectation_registrations = 0

    async def initialize(self) -> None:
        """Establish a sync token before the fuzz traffic starts."""
        await self._sync_once(timeout_ms=0, allow_limited=True)

    def expect(
        self,
        logical_ref: str,
        event_id: str,
        *,
        thread: int = 0,
        client: int = 0,
        sent_at: float | None = None,
    ) -> None:
        """Require one canonical agent reply covering a source event."""
        self.expected_sources[event_id] = logical_ref
        self.source_threads[event_id] = thread
        self.chains[thread, client].append(event_id)
        if sent_at is not None:
            self.sent_at[event_id] = sent_at
        # A concurrent pump may have synced the source before this
        # registration ran; the dedup set would otherwise hide it forever.
        if event_id in self.seen_event_ids:
            self.observed_sources.add(event_id)

    def mark_source_optional(self, event_id: str) -> None:
        """Allow zero replies for a source redacted before its reply settled."""
        if event_id in self.expected_sources:
            self.optional_sources.add(event_id)

    def begin_expectation_registration(self) -> None:
        """Fence invariant checks while a sent source awaits its Matrix event ID."""
        self._pending_expectation_registrations += 1

    def finish_expectation_registration(self, *, validate: bool = True) -> None:
        """Release one send fence and validate replies once every source is known."""
        self._pending_expectation_registrations -= 1
        if validate and self._pending_expectation_registrations == 0:
            self._assert_no_wrong_replies()

    def refresh_ledger_attributions(self, *, min_interval: float = 0.5) -> None:
        """Re-read MindRoom's durable per-source terminal turn records."""
        if self.ledger_path is None:
            return
        now = time.monotonic()
        if now - self._ledger_read_at < min_interval:
            return
        self._ledger_read_at = now
        self._ledger_records = read_ledger_records(self.ledger_path)

    def ledger_response(self, event_id: str) -> str | None:
        """Return the durable response one source's completed record attributes."""
        record = self._ledger_records.get(event_id)
        return record.response_event_id if record is not None else None

    def _supersession_proven(self, event_id: str) -> bool:
        """Return whether a completed no-response record proves supersession.

        Production's replay guard records a skipped superseded turn as a
        completed record with ``response_event_id=None``. That exact durable
        record is the only acceptable supersession proof; chronology alone
        never counts.
        """
        record = self._ledger_records.get(event_id)
        return record is not None and record.response_event_id is None

    def directly_settled(self, event_id: str) -> bool:
        """Return whether one source has its own reply or response-backed record."""
        return len(self.response_ids.get(event_id, ())) == 1 or self.ledger_response(event_id) is not None

    def settled_sources(self) -> set[str]:
        """Return sources settled under per-(thread, sender) chain semantics.

        MindRoom may supersede an older unresponded message once the same
        requester sends a newer one in the same thread. A chain settles from
        its newest required member backwards: the newest must be directly
        replied or response-backed in the ledger, and every older member must
        then present its own durable terminal record -- either its own
        response-backed attribution, or the completed no-response record that
        proves it was legitimately superseded once a later member anchored.
        A missing, incomplete, or malformed record never settles.
        """
        settled: set[str] = set()
        for chain in self.chains.values():
            anchored = False
            for event_id in reversed(chain):
                if event_id in self.optional_sources:
                    if anchored:
                        settled.add(event_id)
                    continue
                if not anchored:
                    if self.directly_settled(event_id):
                        anchored = True
                        settled.add(event_id)
                    continue
                if self.directly_settled(event_id) or self._supersession_proven(event_id):
                    settled.add(event_id)
        return settled

    def unsettled_required_sources(self) -> list[str]:
        """Return sources blocking settlement under the active reply model."""
        if not self.coalescing_threads:
            return [
                event_id
                for event_id in self.expected_sources
                if event_id not in self.optional_sources and len(self.response_ids.get(event_id, ())) != 1
            ]
        settled = self.settled_sources()
        return [
            event_id
            for event_id in self.expected_sources
            if event_id not in self.optional_sources and not (event_id in self.observed_sources and event_id in settled)
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
            self.refresh_ledger_attributions()
            if not self.unsettled_required_sources() and not self.incomplete_streaming_sources():
                settled_after = max(settled_after, self._last_response_activity_at + settle_seconds)
                if time.monotonic() >= settled_after:
                    return
        streaming = set(self.incomplete_streaming_sources())
        missing = {
            f"{self.expected_sources[event_id]} ({event_id})": {
                "direct_replies": len(self.response_ids.get(event_id, ())),
                "ledger_attributed": self.ledger_response(event_id) is not None,
                "ledger_superseded": self._supersession_proven(event_id),
                "observed": event_id in self.observed_sources,
                "reply_streaming_incomplete": event_id in streaming,
            }
            for event_id in {*self.unsettled_required_sources(), *streaming}
        }
        msg = f"timed out waiting for exact agent replies: {missing}"
        raise AssertionError(msg)

    def resolve_response_ref(self, response_ref: str) -> str:
        """Resolve a logical agent-response reference to its real event ID.

        In chaos mode a coalesced source has no direct reply of its own; the
        agent's answer covering it is the combined reply that MindRoom's
        durable ledger attributes the source to.
        """
        event_id = self.response_event_by_ref.get(response_ref)
        if event_id is not None:
            return event_id
        if self.coalescing_threads:
            source_event_id = next(
                (
                    candidate_id
                    for candidate_id, ref in self.expected_sources.items()
                    if ref == response_ref.removeprefix("response:")
                ),
                None,
            )
            if source_event_id is not None:
                covering = self._covering_response(source_event_id)
                if covering is not None:
                    return covering
        msg = f"response event not observed for {response_ref!r}"
        raise KeyError(msg)

    def _covering_response(self, source_event_id: str) -> str | None:
        """Return the reply covering one coalesced or superseded source.

        A source is covered only through proven chain state: its own
        response-backed record, or -- when its completed record proves it was
        superseded -- the response-backed reply of a later chain member. An
        older source with no durable terminal record of its own is never
        treated as covered.
        """
        own_attribution = self.ledger_response(source_event_id)
        if own_attribution is not None:
            return own_attribution
        if not self._supersession_proven(source_event_id):
            return None
        chain = next((chain for chain in self.chains.values() if source_event_id in chain), None)
        if chain is None:
            return None
        for later_source in chain[chain.index(source_event_id) + 1 :]:
            attribution = self.ledger_response(later_source)
            if attribution is not None:
                return attribution
            replies = self.response_ids.get(later_source, set())
            if len(replies) == 1:
                return next(iter(replies))
        return None

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
            "origin_server_ts": event.get("origin_server_ts"),
        }
        if event_id in self.expected_sources:
            self.observed_sources.add(event_id)
        if event.get("sender") in self.internal_relay_senders:
            # Only a structurally valid auto-resume relay may exempt an agent
            # reply from the wrong-reply invariant. Blanket-trusting every
            # router-authored event (a greeting, topic chatter, a malformed
            # recovery) would mask agent/router reply loops, so require the
            # canonical relay shape production emits: a threaded resume message.
            relay_target = _auto_resume_relay_target(
                event,
                relay_senders=self.internal_relay_senders,
            )
            target = self.event_summaries.get(relay_target[0], {}) if relay_target is not None else {}
            target_relation = target.get("relates_to")
            target_root = target_relation.get("event_id") if isinstance(target_relation, dict) else None
            latest_target_body = self.latest_reply_bodies.get(relay_target[0]) if relay_target is not None else None
            if (
                relay_target is not None
                and target.get("sender") == self.agent_id
                and target.get("type") == "m.room.message"
                and target_root == relay_target[1]
                and latest_target_body is not None
                and latest_target_body[1].endswith(
                    (INTERRUPTED_RESPONSE_NOTE, RESTART_INTERRUPTED_RESPONSE_NOTE),
                )
            ):
                self.internal_source_ids.add(event_id)
            return
        if event.get("sender") != self.agent_id or event.get("type") != "m.room.message":
            return
        content = event.get("content")
        if isinstance(content, dict):
            self._ingest_agent_message(event_id, content)

    def _ingest_agent_message(self, event_id: str, content: Mapping[str, Any]) -> None:
        """Fold one agent `m.room.message` into reply bodies and thread attributions."""
        relation = content.get("m.relates_to")
        # A canonical original reply or an edit of a tracked reply is streaming
        # activity, so it extends the quiet window even when the original event
        # is already older than the settle interval.
        if self._track_reply_body(event_id, content, relation):
            self._last_response_activity_at = time.monotonic()
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

    def _track_reply_body(
        self,
        event_id: str,
        content: Mapping[str, Any],
        relation: Any,  # noqa: ANN401
    ) -> bool:
        """Fold one agent message (original reply or `m.replace` edit) into latest bodies.

        Return whether this observation was a canonical original reply or an edit
        of an already-tracked canonical reply. An edit of an unknown target is
        neither folded nor reported, so it never extends the quiet window.
        """
        is_edit = isinstance(relation, dict) and relation.get("rel_type") == "m.replace"
        reply_event_id = relation.get("event_id") if is_edit else event_id
        if not isinstance(reply_event_id, str):
            return False
        # An edit only counts when it targets a canonical reply we already track.
        if is_edit and reply_event_id not in self.latest_reply_bodies:
            return False
        new_content = content.get("m.new_content")
        body_source = new_content if isinstance(new_content, dict) else content
        body = body_source.get("body")
        if not isinstance(body, str):
            return False
        timestamp = self.event_summaries.get(event_id, {}).get("origin_server_ts")
        order = _replacement_order(event_id, timestamp, is_edit=is_edit)
        current = self.latest_reply_bodies.get(reply_event_id)
        if current is None or order >= current[0]:
            self.latest_reply_bodies[reply_event_id] = (order, body)
        return True

    def _reply_body_complete(self, body: str) -> bool:
        """Return whether one reply body is a settled terminal state.

        A body is terminal when it is the exact completed stream for its model
        call, or a by-design interrupted note (restart recovery and the final
        audit own the validity of those). Placeholders and partial streams are
        not terminal, so they must keep settlement open.
        """
        if body.endswith((INTERRUPTED_RESPONSE_NOTE, RESTART_INTERRUPTED_RESPONSE_NOTE)):
            return True
        call_id = _body_call_id(body)
        return call_id is not None and body == self.expected_body_for(call_id)

    def incomplete_streaming_sources(self) -> list[str]:
        """Return observed required sources whose covering reply is still streaming.

        Settlement otherwise depends only on a reply being *observed*, which a
        placeholder edit satisfies; a required reply that has not reached a
        terminal body must keep the window open so the final audit never reads a
        mid-stream ``Thinking...`` body. A genuinely frozen stream never reaches
        a terminal body either, so the checkpoint deadline still fails it.
        """
        blocking: list[str] = []
        for event_id in self.expected_sources:
            if event_id in self.optional_sources or event_id not in self.observed_sources:
                continue
            reply_event_id = self._settled_reply_event(event_id)
            if reply_event_id is None:
                continue
            latest = self.latest_reply_bodies.get(reply_event_id)
            if latest is None or not self._reply_body_complete(latest[1]):
                blocking.append(event_id)
        return blocking

    def _settled_reply_event(self, source_event_id: str) -> str | None:
        """Return the reply event covering one source, if one is known yet."""
        replies = self.response_ids.get(source_event_id)
        if replies and len(replies) == 1:
            return next(iter(replies))
        if self.coalescing_threads:
            return self._covering_response(source_event_id)
        return None

    def _assert_no_wrong_replies(self) -> None:
        if self._pending_expectation_registrations:
            return
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
    sender: str | None = None
    redacts: str | None = None
    reaction_key: str | None = None
    # The exact ``content`` dict the fuzzer sent. The final audit compares this
    # verbatim against the paginated homeserver copy, so a wrong body, dropped
    # marker, changed ``msgtype``, lost reply target, or retargeted relation is
    # caught. Matrix persists client ``content`` unchanged (server metadata
    # lives in ``unsigned``/top-level fields, outside ``content``), so no
    # normalization carve-out is required for these unencrypted rooms.
    content: Mapping[str, Any] | None = None


_CALL_ID_PREFIX = "LIVE-FUZZ call="


def _replacement_order(event_id: str, timestamp: object, *, is_edit: bool) -> tuple[int, int, str]:
    """Return one canonical total order for an original and its replacements."""
    return (int(is_edit), timestamp if isinstance(timestamp, int) else 0, event_id)


def _auto_resume_relay_target(
    event: Mapping[str, Any],
    *,
    relay_senders: Collection[str],
) -> tuple[str, str] | None:
    """Return the interrupted target and thread root for one exact resume relay."""
    if event.get("sender") not in relay_senders or event.get("type") != "m.room.message":
        return None
    content = event.get("content")
    if not isinstance(content, dict):
        return None
    body = content.get("body")
    if not isinstance(body, str) or AUTO_RESUME_MESSAGE not in body:
        return None
    relation = content.get("m.relates_to")
    if not isinstance(relation, dict) or relation.get("rel_type") != "m.thread":
        return None
    root = relation.get("event_id")
    in_reply_to = relation.get("m.in_reply_to")
    target = in_reply_to.get("event_id") if isinstance(in_reply_to, dict) else None
    if not isinstance(root, str) or not isinstance(target, str):
        return None
    return target, root


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
        ledger_path: Path | None = None,
        source_current_markers: Mapping[str, str] | None = None,
        source_revision_markers: Mapping[str, Mapping[str, str]] | None = None,
        observed_markers_for: Callable[[int], frozenset[str]] = _ModelHandler.observed_markers_for,
    ) -> None:
        self.client = client
        self.oracle = oracle
        self.agent_id = agent_id
        self.expected_body_for = expected_body_for
        self.ledger_path = ledger_path
        # Per source event id, the marker of the latest valid revision the runner
        # sent to Matrix. Empty when the run does not track revisions.
        self.source_current_markers = dict(source_current_markers or {})
        self.source_revision_markers = {
            source_event_id: dict(revisions) for source_event_id, revisions in (source_revision_markers or {}).items()
        }
        self.observed_markers_for = observed_markers_for

    async def audit(
        self,
        *,
        room_ids: Collection[str],
        sent_records: Collection[_SentRecord],
        redacted_targets: Mapping[str, str] | Collection[str],
    ) -> dict[str, int]:
        """Run every final-state assertion and return audit metrics."""
        events: dict[str, dict[str, Any]] = {}
        for room_id in room_ids:
            for event in await self.client.paginate_room(room_id):
                event_id = event.get("event_id")
                if isinstance(event_id, str) and event_id not in events:
                    events[event_id] = {**event, "_audit_room_id": room_id}
        redacted = dict(redacted_targets) if isinstance(redacted_targets, dict) else dict.fromkeys(redacted_targets, "")
        self._resolve_source_revision_markers(events, redacted)
        self._assert_sent_events_canonical(events, sent_records, redacted)
        replies = self._canonical_agent_replies(events, sent_records=sent_records)
        self._assert_reply_cardinality(replies)
        completed = self._assert_final_bodies_complete(events, replies)
        self._assert_sync_view_parity(events, sent_records, replies)
        ledger_metrics: dict[str, int] = {}
        if self.ledger_path is not None:
            ledger_metrics = self._assert_ledger_attribution(replies)
            self._assert_model_saw_current_sources(events)
        return {
            "audited_events": len(events),
            "audited_rooms": len(set(room_ids)),
            "completed_final_bodies": completed,
            **ledger_metrics,
        }

    def _resolve_source_revision_markers(
        self,
        events: Mapping[str, Mapping[str, Any]],
        redacted: Mapping[str, str],
    ) -> None:
        """Resolve each source's latest surviving edit from canonical Matrix order."""
        for source_event_id, logical_ref in self.oracle.expected_sources.items():
            self.source_current_markers[source_event_id] = _source_marker(logical_ref, ORIGINAL_REVISION)
            revisions = self.source_revision_markers.get(source_event_id, {})
            surviving = [
                (
                    _replacement_order(
                        edit_event_id,
                        events.get(edit_event_id, {}).get("origin_server_ts"),
                        is_edit=True,
                    ),
                    marker,
                )
                for edit_event_id, marker in revisions.items()
                if edit_event_id in events and edit_event_id not in redacted
            ]
            if surviving:
                self.source_current_markers[source_event_id] = max(surviving)[1]

    def _assert_sent_events_canonical(
        self,
        events: Mapping[str, Mapping[str, Any]],
        sent_records: Collection[_SentRecord],
        redacted: Mapping[str, str] | Collection[str],
    ) -> None:
        """Every sent event survives verbatim, redactions prune, reactions stay visible."""
        redaction_ids = dict(redacted) if isinstance(redacted, dict) else dict.fromkeys(redacted, "")
        problems: list[str] = []
        for record in sent_records:
            event = events.get(record.event_id)
            if event is None:
                problems.append(f"missing from /messages: {record.event_id} ({record.event_type})")
                continue
            problems.extend(self._sent_event_problems(record, event, redaction_ids))
        if problems:
            msg = f"final Matrix state audit failed: {problems}"
            raise AssertionError(msg)

    @staticmethod
    def _sent_event_problems(
        record: _SentRecord,
        event: Mapping[str, Any],
        redaction_ids: Mapping[str, str],
    ) -> list[str]:
        """Return canonical-state mismatches for one present event."""
        problems: list[str] = []
        content = event.get("content")
        content = content if isinstance(content, dict) else {}
        if event.get("_audit_room_id") != record.room_id:
            problems.append(
                f"{record.event_id} appeared in room {event.get('_audit_room_id')}, expected {record.room_id}",
            )
        if event.get("type") != record.event_type:
            problems.append(
                f"{record.event_id} has type {event.get('type')}, expected {record.event_type}",
            )
        if record.sender is not None and event.get("sender") != record.sender:
            problems.append(
                f"{record.event_id} has sender {event.get('sender')}, expected {record.sender}",
            )
        if record.redacts is not None:
            problems.extend(FinalStateAuditor._redaction_event_problems(record, event, content))
        if record.event_id in redaction_ids:
            problems.extend(FinalStateAuditor._redaction_problems(record, event, content, redaction_ids))
        elif record.redacts is None and record.content is not None and content != dict(record.content):
            problems.append(
                f"{record.event_type} {record.event_id} content diverged from sent payload: "
                f"expected {dict(record.content)!r}, got {content!r}",
            )
        return problems

    @staticmethod
    def _redaction_event_problems(
        record: _SentRecord,
        event: Mapping[str, Any],
        content: Mapping[str, Any],
    ) -> list[str]:
        """Validate pre-v11 and v11+ redaction event target placement."""
        assert record.redacts is not None
        top_level_target = event.get("redacts")
        content_target = content.get("redacts")
        targets = {target for target in (top_level_target, content_target) if isinstance(target, str)}
        problems: list[str] = []
        if targets != {record.redacts}:
            problems.append(
                f"{record.event_id} redacts {sorted(targets)}, expected {record.redacts}",
            )
        expected_reason = record.content.get("reason") if record.content is not None else None
        if content.get("reason") != expected_reason:
            problems.append(
                f"{record.event_id} redaction reason is {content.get('reason')!r}, expected {expected_reason!r}",
            )
        return problems

    @staticmethod
    def _redaction_problems(
        record: _SentRecord,
        event: Mapping[str, Any],
        content: Mapping[str, Any],
        redaction_ids: Mapping[str, str],
    ) -> list[str]:
        """Return mismatches for one redacted event shell."""
        problems: list[str] = []
        if content:
            problems.append(f"redacted event kept visible content: {record.event_id}: {dict(content)!r}")
        redaction_event_id = redaction_ids[record.event_id]
        unsigned = event.get("unsigned")
        redacted_because = unsigned.get("redacted_because") if isinstance(unsigned, dict) else None
        actual_redaction_id = redacted_because.get("event_id") if isinstance(redacted_because, dict) else None
        if redaction_event_id and actual_redaction_id != redaction_event_id:
            problems.append(
                f"redacted event {record.event_id} points to {actual_redaction_id}, expected {redaction_event_id}",
            )
        return problems

    def _canonical_agent_replies(
        self,
        events: Mapping[str, Mapping[str, Any]],
        *,
        sent_records: Collection[_SentRecord] = (),
    ) -> dict[str, set[str]]:
        """Index canonical agent originals by source from the paginated view."""
        records = {record.event_id: record for record in sent_records}
        replies: dict[str, set[str]] = defaultdict(set)
        problems: list[str] = []
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
                source_record = records.get(source_event_id)
                source_event = events.get(source_event_id, {})
                source_room = source_record.room_id if source_record is not None else source_event.get("_audit_room_id")
                if isinstance(source_room, str) and event.get("_audit_room_id") != source_room:
                    problems.append(
                        f"agent reply {event_id} is in room {event.get('_audit_room_id')}, "
                        f"but source {source_event_id} is in {source_room}",
                    )
                    continue
                expected_root = self._source_thread_root(
                    source_event_id,
                    events,
                    records,
                    seen=set(),
                )
                if expected_root is not None and relation.get("event_id") != expected_root:
                    problems.append(
                        f"agent reply {event_id} uses thread root {relation.get('event_id')}, "
                        f"expected {expected_root} for source {source_event_id}",
                    )
                    continue
                replies[source_event_id].add(event_id)
        if problems:
            msg = f"final agent reply provenance audit failed: {problems}"
            raise AssertionError(msg)
        return replies

    @classmethod
    def _source_thread_root(
        cls,
        event_id: str,
        events: Mapping[str, Mapping[str, Any]],
        records: Mapping[str, _SentRecord],
        *,
        seen: set[str],
    ) -> str | None:
        """Resolve one source's canonical thread root through reply ancestry."""
        if event_id in seen:
            return None
        seen.add(event_id)
        record = records.get(event_id)
        event = events.get(event_id, {})
        content: object = record.content if record is not None else event.get("content")
        if not isinstance(content, dict):
            return None
        relation = content.get("m.relates_to")
        if not isinstance(relation, dict):
            return event_id
        root = relation.get("event_id")
        if relation.get("rel_type") == "m.thread" and isinstance(root, str):
            return root
        reply = relation.get("m.in_reply_to")
        target = reply.get("event_id") if isinstance(reply, dict) else None
        if isinstance(target, str):
            return cls._source_thread_root(target, events, records, seen=seen)
        return event_id

    def _assert_reply_cardinality(self, replies: Mapping[str, set[str]]) -> None:
        """Server-canonical replies must match the active reply model."""
        oracle = self.oracle
        problems: list[str] = []
        for source_event_id, logical_ref in oracle.expected_sources.items():
            count = len(replies.get(source_event_id, ()))
            if count > 1:
                problems.append(f"source {logical_ref} has {count} direct replies in /messages")
            elif not oracle.coalescing_threads and source_event_id not in oracle.optional_sources and count != 1:
                problems.append(f"source {logical_ref} has {count} canonical replies in /messages")
        for source_event_id, reply_ids in replies.items():
            if source_event_id in oracle.expected_sources or source_event_id in oracle.internal_source_ids:
                continue
            problems.append(f"unexpected agent replies to {source_event_id}: {sorted(reply_ids)}")
        if problems:
            msg = f"final reply cardinality audit failed: {problems}"
            raise AssertionError(msg)

    def _assert_ledger_attribution(self, replies: Mapping[str, set[str]]) -> dict[str, int]:
        """Every required source must present its own durable terminal record.

        Matrix relations cannot expose which sources one coalesced reply
        covered, so exact per-source attribution comes from MindRoom's
        handled-turn ledger, walked per (thread, sender) chain to honor the
        supersede policy, and cross-checked against the `/messages` view in
        both directions. The same terminal-proof loader backs both live
        settlement and this final audit so the two can never drift: an older
        chain source counts as superseded only when its own completed
        no-response record exists, never from chronology alone.
        """
        assert self.ledger_path is not None
        oracle = self.oracle
        if not self.ledger_path.exists():
            msg = f"handled-turn ledger missing at {self.ledger_path}"
            raise AssertionError(msg)
        records = read_ledger_records(self.ledger_path)

        problems: list[str] = []
        ledger_response_ids, attributed, optional_problems = self._attribute_optional_replies(replies, records)
        problems.extend(optional_problems)
        superseded = 0
        for chain in oracle.chains.values():
            anchored = False
            for source_event_id in reversed(chain):
                if source_event_id in oracle.optional_sources:
                    continue
                logical_ref = oracle.expected_sources[source_event_id]
                record = records.get(source_event_id)
                if record is not None and source_event_id not in record.source_event_ids:
                    problems.append(
                        f"turn record keyed by {source_event_id} does not own that source: {record.source_event_ids}",
                    )
                    record = None
                if record is not None and record.response_event_id is not None:
                    ledger_response_ids.add(record.response_event_id)
                    attributed += 1
                    anchored = True
                elif anchored and record is not None and record.response_event_id is None:
                    # An older message legitimately superseded by a newer settled
                    # message from the same sender, proven by production's own
                    # completed no-response record for this exact source.
                    superseded += 1
                elif anchored:
                    problems.append(
                        f"superseded chain source {logical_ref} ({source_event_id}) "
                        "has no completed no-response supersession record",
                    )
                else:
                    problems.append(
                        f"newest chain source {logical_ref} ({source_event_id}) has no durable attribution",
                    )

        all_expected_reply_ids = {
            reply_id
            for source_event_id, reply_ids in replies.items()
            if source_event_id in oracle.expected_sources
            for reply_id in reply_ids
        }
        required_reply_ids = all_expected_reply_ids
        problems.extend(
            f"ledger response {response_id} is not a visible canonical reply"
            for response_id in sorted(ledger_response_ids - all_expected_reply_ids)
        )
        problems.extend(
            f"visible reply {reply_id} is not attributed by any durable turn record"
            for reply_id in sorted(required_reply_ids - ledger_response_ids)
        )
        if problems:
            msg = f"durable turn attribution audit failed: {problems}"
            raise AssertionError(msg)
        return {"ledger_attributed_sources": attributed, "ledger_superseded_sources": superseded}

    def _attribute_optional_replies(
        self,
        replies: Mapping[str, set[str]],
        records: Mapping[str, TurnRecord],
    ) -> tuple[set[str], int, list[str]]:
        """Require attribution only when an optional source kept a visible reply."""
        response_ids: set[str] = set()
        problems: list[str] = []
        for source_event_id in self.oracle.optional_sources:
            visible_reply_ids = replies.get(source_event_id, set())
            record = records.get(source_event_id)
            if record is not None and source_event_id not in record.source_event_ids:
                problems.append(
                    f"turn record keyed by {source_event_id} does not own that source: {record.source_event_ids}",
                )
                record = None
            if not visible_reply_ids:
                if record is not None and record.response_event_id is not None:
                    response_ids.add(record.response_event_id)
                continue
            logical_ref = self.oracle.expected_sources[source_event_id]
            if record is None or record.response_event_id not in visible_reply_ids:
                problems.append(
                    f"visible optional-source reply for {logical_ref} ({source_event_id}) "
                    "has no matching durable attribution",
                )
            else:
                response_ids.add(record.response_event_id)
        return response_ids, len(response_ids), problems

    def _assert_model_saw_current_sources(self, events: Mapping[str, Mapping[str, Any]]) -> None:
        """Every response-backed turn must be generated from its sources' current bodies.

        A right-shaped body proves the model was called, but not that it was
        called with the correct sources at their latest revision. Each
        response-backed ledger record names the sources it covers; the model
        call that produced its visible reply must have observed the current
        marker of every one of those sources. A wrong-source body, a pre-edit
        body, or a coalesced body missing one source's current marker fails
        here. A completed no-response supersession record requires no marker.

        Only *replayable* sources carry a required marker. A source that was
        durably redacted is tombstoned: production deliberately refuses to
        regenerate an edit against it (``edit_regenerator.py`` ignores edits to
        redacted sources), so a record may keep its already-visible response
        while one covered source no longer feeds model replay. Requiring the
        redacted source's post-redaction edit marker would demand behavior
        production correctly declines, so the required set is derived from
        ``record.replay_source_event_ids`` (``source_event_ids`` minus
        ``redacted_source_event_ids``) — the same contract production uses.
        """
        assert self.ledger_path is not None
        records = read_ledger_records(self.ledger_path)
        expected_sources = self.oracle.expected_sources
        problems: list[str] = []
        for source_event_id, record in records.items():
            if record.response_event_id is None:
                continue
            required = {
                self.source_current_markers[covered]
                for covered in record.replay_source_event_ids
                if covered in expected_sources and covered in self.source_current_markers
            }
            if not required:
                continue
            body = self._latest_agent_body(events, record.response_event_id)
            call_id = _body_call_id(body)
            observed = self.observed_markers_for(call_id) if call_id is not None else frozenset()
            missing = required - observed
            if missing:
                problems.append(
                    f"turn for {expected_sources.get(source_event_id, source_event_id)} "
                    f"({source_event_id}) generated without current source markers "
                    f"{sorted(missing)}; model saw {sorted(observed)}",
                )
        if problems:
            msg = f"model source-revision audit failed: {problems}"
            raise AssertionError(msg)

    def _assert_final_bodies_complete(
        self,
        events: Mapping[str, Mapping[str, Any]],
        replies: Mapping[str, set[str]],
    ) -> int:
        """Every required reply ends as one exact completed stream or a recovered interruption.

        A restart may terminate a stream into a visible interrupted note by
        design, but only when a completed auto-resume answer exists in the
        same thread; an interrupted or partial final body without recovery is
        a failure.
        """
        problems: list[str] = []
        checked = 0
        # Optional sources permit *zero* replies after a redaction race, but any
        # reply that still exists must pass the same canonical/recovered-terminal
        # checks — a frozen ``Thinking...`` placeholder, a partial stream, or an
        # unrecovered interruption left visible is still a failure. The inner
        # ``replies.get(source_event_id, ())`` loop naturally tolerates the
        # zero-reply case, so every expected source is audited here.
        audited_sources = list(self.oracle.expected_sources.items())
        audited_sources.extend((relay_id, f"relay:{relay_id}") for relay_id in self.oracle.internal_source_ids)
        for source_event_id, logical_ref in audited_sources:
            for reply_event_id in replies.get(source_event_id, ()):
                body = self._latest_agent_body(events, reply_event_id)
                call_id = _body_call_id(body)
                if call_id is not None and body == self.expected_body_for(call_id):
                    checked += 1
                    continue
                if self._is_recovered_interruption(events, reply_event_id, body):
                    checked += 1
                    continue
                problems.append(
                    f"reply to {logical_ref} ended with a non-canonical body: {body[:120]!r}",
                )
        if problems:
            msg = f"final response body audit failed: {problems}"
            raise AssertionError(msg)
        return checked

    def _is_recovered_interruption(
        self,
        events: Mapping[str, Mapping[str, Any]],
        reply_event_id: str,
        body: str,
    ) -> bool:
        """Return whether an interrupted terminal note was covered by auto-resume.

        Recovery is proven only by the exact causal chain ``I <- R <- A``: the
        interrupted response ``I`` (``reply_event_id``) must be answered by an
        internal relay ``R`` authored by a configured relay sender in the same
        thread, and the completed canonical agent response ``A`` must reply to
        that relay in the same thread. A completed reply to any unrelated relay
        in the thread never counts.
        """
        if not body.endswith((INTERRUPTED_RESPONSE_NOTE, RESTART_INTERRUPTED_RESPONSE_NOTE)):
            return False
        thread_root = self._thread_root(events.get(reply_event_id, {}))
        if thread_root is None:
            return False
        for relay_id, relay in events.items():
            if not self._relay_replies_to(relay, thread_root, reply_event_id):
                continue
            if self._agent_response_completes_relay(events, thread_root, relay_id):
                return True
        return False

    def _relay_replies_to(
        self,
        relay: Mapping[str, Any],
        thread_root: str,
        interrupted_event_id: str,
    ) -> bool:
        """A relay proves recovery only if it replies to ``interrupted_event_id``."""
        target = _auto_resume_relay_target(
            relay,
            relay_senders=self.oracle.internal_relay_senders,
        )
        return target == (interrupted_event_id, thread_root)

    def _agent_response_completes_relay(
        self,
        events: Mapping[str, Mapping[str, Any]],
        thread_root: str,
        relay_id: str,
    ) -> bool:
        """A completed canonical agent reply must reply to ``relay_id`` in-thread."""
        for event_id, event in events.items():
            if event.get("sender") != self.agent_id or event.get("type") != "m.room.message":
                continue
            content = event.get("content")
            if not isinstance(content, dict):
                continue
            relation = content.get("m.relates_to")
            if not isinstance(relation, dict) or relation.get("event_id") != thread_root:
                continue
            if relation.get("rel_type") != "m.thread":
                continue
            in_reply_to = relation.get("m.in_reply_to")
            resumed_source = in_reply_to.get("event_id") if isinstance(in_reply_to, dict) else None
            if resumed_source != relay_id:
                continue
            resumed_body = self._latest_agent_body(events, event_id)
            call_id = _body_call_id(resumed_body)
            if call_id is not None and resumed_body == self.expected_body_for(call_id):
                return True
        return False

    @staticmethod
    def _thread_root(event: Mapping[str, Any]) -> str | None:
        """Return the thread root of one event, if any."""
        content = event.get("content")
        if not isinstance(content, dict):
            return None
        relation = content.get("m.relates_to")
        if not isinstance(relation, dict) or relation.get("rel_type") != "m.thread":
            return None
        root = relation.get("event_id")
        return root if isinstance(root, str) else None

    def _latest_agent_body(self, events: Mapping[str, Mapping[str, Any]], reply_event_id: str) -> str:
        """Return the newest visible body for one agent reply."""
        candidates: list[tuple[tuple[int, int, str], str]] = []
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
                candidates.append((_replacement_order(event_id, timestamp, is_edit=is_edit), body))
        return max(candidates, default=((0, 0, ""), ""))[1]

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
        journal: Callable[[Mapping[str, object]], None] | None = None,
    ) -> None:
        self.stack = stack
        self.clients = clients
        self.client = clients[0]
        self.scenario = scenario
        self.reply_timeout = reply_timeout
        self.settle_seconds = settle_seconds
        self.pending_grace = pending_grace
        self._journal = journal
        self.oracle = ExactReplyOracle(
            self.client,
            stack.agent_id,
            internal_relay_senders=(stack.router_id,),
            coalescing_threads=scenario.profile == "chaos",
            ledger_path=(
                stack.storage_path / "tracking" / f"{AGENT_NAME}_responded.json"
                if scenario.profile == "chaos"
                else None
            ),
            expected_body_for=_ModelHandler.response_text_for,
        )
        self.event_ids: dict[str, str] = {}
        self.sent_payloads: dict[str, _SentPayload] = {}
        self.sent_records: list[_SentRecord] = []
        # Maps every redacted event to the redaction event that removed it.
        # The final audit requires both the redacted shell and its exact
        # ``unsigned.redacted_because`` provenance.
        self.redacted_targets: dict[str, str] = {}
        # Per source event id, the marker of the latest valid revision that
        # reached Matrix (``orig`` on send, the edit marker after an edit
        # revises it). The final audit binds each turn's model call to these.
        self.source_current_markers: dict[str, str] = {}
        self.source_revision_markers: dict[str, dict[str, str]] = defaultdict(dict)
        # Per source event id, the ordered stack of surviving revisions as
        # ``(edit_event_id, marker)`` entries (bottom is ``(None, orig)``, each
        # applied edit pushes ``(its event id, its marker)``). Redacting an
        # ``m.replace`` reverts the source to its latest *surviving* revision, so
        # a redaction removes that edit's entry by identity — not blindly the
        # top, which would be wrong when a non-newest edit is redacted — and the
        # current marker becomes whichever entry now sits on top.
        self._source_revision_stack: dict[str, list[tuple[str | None, str]]] = {}
        # Maps an edit event id to the source event id it revised so a later
        # redaction targeting that edit knows which source's stack to revert.
        self._edit_event_source: dict[str, str] = {}
        self.operation_count = 0
        # Monotonic sequence for the realized journal, spanning both mutations
        # and lifecycle boundaries so the durable trace preserves their true
        # interleaving without inflating the mutation-only ``operation_count``.
        self._realized_sequence = 0
        self.restart_count = 0
        self.tuwunel_restart_count = 0
        self.outage_count = 0
        self.executed_batches = 0
        self.max_unsettled = 0
        self._mindroom_running = True
        self._last_mindroom_start_at: float | None = None

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
        candidates: list[tuple[tuple[int, int, str], str]] = []
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
                candidates.append((_replacement_order(event_id, timestamp, is_edit=is_edit), body))
        return max(candidates, default=((0, 0, ""), ""))[1]

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
                self._record_lifecycle(LiveOperationKind.RESTART_MINDROOM)
            else:
                await self._apply_batch_in_completion_order(
                    batch,
                    on_complete=self._record_batch_results,
                )
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

    async def _apply_batch_in_completion_order(
        self,
        batch: tuple[LiveOperation, ...],
        *,
        on_complete: Callable[
            [Collection[tuple[LiveOperation, str | None, _SentPayload | None]]],
            None,
        ]
        | None = None,
    ) -> list[tuple[LiveOperation, str | None, _SentPayload | None]]:
        """Apply a batch concurrently, returning results in true completion order.

        ``asyncio.gather`` yields results in input order, which would make the
        durable journal misrepresent a nondeterministic race. Draining the
        applies as they finish lets the caller record each op the instant its
        send resolves. If one sibling fails, already-landed results remain
        journaled while every unfinished task is cancelled and joined.
        """
        results: list[tuple[LiveOperation, str | None, _SentPayload | None]] = []

        async def apply_and_record(
            operation: LiveOperation,
        ) -> tuple[LiveOperation, str | None, _SentPayload | None]:
            result = await self._apply(operation)
            results.append(result)
            if on_complete is not None:
                on_complete((result,))
            return result

        tasks = [asyncio.create_task(apply_and_record(operation)) for operation in batch]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return results

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
            self._record_realized(operation, event_id)

    def _record_realized(self, operation: LiveOperation, event_id: str | None) -> None:
        """Append one realized operation to the failure bundle's journal.

        Called in true completion order after a concurrent batch resolves, so a
        nondeterministic race can be reconstructed from the durable trace even
        though the logical scenario only records batches.
        """
        if self._journal is None:
            return
        self._realized_sequence += 1
        self._journal(
            {
                "sequence": self._realized_sequence,
                "kind": str(operation.kind),
                "event_ref": operation.event_ref,
                "thread": operation.thread,
                "client": operation.client,
                "event_id": event_id,
                "mindroom_running": self._mindroom_running,
            },
        )

    def _record_lifecycle(self, kind: LiveOperationKind) -> None:
        """Append one realized lifecycle boundary to the journal.

        Restarts and outages reorder which mutations the running MindRoom ever
        observed, so the realized sequence must interleave them with mutations to
        stay reconstructable.
        """
        if self._journal is None:
            return
        self._realized_sequence += 1
        self._journal(
            {
                "sequence": self._realized_sequence,
                "kind": str(kind),
                "event_ref": None,
                "thread": None,
                "client": None,
                "event_id": None,
                "mindroom_running": self._mindroom_running,
            },
        )

    async def _run_chaos(self) -> dict[str, object]:
        """Run sustained overlapping load, settling only at explicit checkpoints."""
        for batch_index, batch in enumerate(self.scenario.batches):
            first = batch[0]
            if first.kind in LIFECYCLE_KINDS:
                await self._apply_lifecycle(first.kind, batch_index)
            else:
                await self._apply_batch_in_completion_order(
                    batch,
                    on_complete=self._record_batch_results,
                )
                try:
                    await self.oracle.pump()
                except AssertionError as exc:
                    msg = f"{exc} after chaos batch {batch_index}"
                    raise AssertionError(msg) from exc
            self.executed_batches += 1
            self.max_unsettled = max(self.max_unsettled, len(self.oracle.unsettled_required_sources()))

        await self._checkpoint(len(self.scenario.batches))
        await self._wait_for_restart_recovery_window()
        auditor = FinalStateAuditor(
            self.client,
            self.oracle,
            agent_id=self.stack.agent_id,
            expected_body_for=_ModelHandler.response_text_for,
            ledger_path=self.stack.storage_path / "tracking" / f"{AGENT_NAME}_responded.json",
            source_current_markers=self.source_current_markers,
            source_revision_markers=self.source_revision_markers,
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
            # A checkpoint only settles pending replies; it reorders nothing, so
            # it stays out of the realized sequence.
            await self._checkpoint(batch_index)
            return
        if kind is LiveOperationKind.RESTART_MINDROOM:
            self.stack.restart_mindroom()
            self.restart_count += 1
            self._last_mindroom_start_at = time.monotonic()
        elif kind is LiveOperationKind.KILL_RESTART_MINDROOM:
            self.stack.kill_restart_mindroom()
            self.restart_count += 1
            self._last_mindroom_start_at = time.monotonic()
        elif kind is LiveOperationKind.COLD_RESTART_MINDROOM:
            self.stack.cold_restart_mindroom()
            self.restart_count += 1
            self._last_mindroom_start_at = time.monotonic()
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
            self._last_mindroom_start_at = time.monotonic()
        else:  # pragma: no cover - validation rejects unknown lifecycle kinds
            msg = f"unsupported lifecycle operation {kind}"
            raise AssertionError(msg)
        # Journal after the state transition so the recorded running flag matches
        # the world the next mutations observe.
        self._record_lifecycle(kind)

    async def _wait_for_restart_recovery_window(self) -> None:
        """Give delayed startup recovery time to settle after a late restart.

        Streams interrupted by a restart are cleaned and auto-resumed by a
        delayed startup pass, so the final audit must not run before that
        designed recovery latency has elapsed.
        """
        if self._last_mindroom_start_at is None:
            return
        recovery_wait = 16.0 - (time.monotonic() - self._last_mindroom_start_at)
        if recovery_wait <= 0:
            return
        await asyncio.sleep(recovery_wait)
        await self.oracle.wait_until_exact(
            deadline_seconds=self.reply_timeout,
            settle_seconds=self.settle_seconds,
        )

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
            self.oracle.refresh_ledger_attributions()
            try:
                return self.oracle.resolve_response_ref(logical_ref)
            except KeyError:
                continue
        msg = f"agent response never observed for {logical_ref!r}"
        raise TimeoutError(msg)

    async def _send_roots(self, threads: Collection[int]) -> None:
        async def send_root(thread: int) -> tuple[int, str, _SentPayload, float]:
            logical_ref = f"root:{thread}"
            content = self._message_content(
                f"Live fuzz root {thread}",
                marker=_source_marker(logical_ref, ORIGINAL_REVISION),
            )
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
            self.sent_records.append(
                _SentRecord(
                    event_id,
                    room_id,
                    payload.event_type,
                    sender=root_client.user_id,
                    content=payload.content,
                ),
            )
            return thread, event_id, payload, time.monotonic()

        roots = await asyncio.gather(*(send_root(thread) for thread in threads))
        for thread, event_id, payload, sent_at in roots:
            logical_ref = f"root:{thread}"
            self.event_ids[logical_ref] = event_id
            self.sent_payloads[logical_ref] = payload
            self.source_current_markers[event_id] = _source_marker(logical_ref, ORIGINAL_REVISION)
            self.oracle.expect(
                logical_ref,
                event_id,
                thread=thread,
                client=self.scenario.root_client(thread),
                sent_at=sent_at,
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
                marker=_source_marker(operation.event_ref, ORIGINAL_REVISION),
            )
            payload = _SentPayload("m.room.message", txn_id, content)
            event_id = await self._send_expected_message(operation, client, payload, room_id)
            self.source_current_markers[event_id] = _source_marker(operation.event_ref, ORIGINAL_REVISION)
            return operation, event_id, payload

        if operation.kind is LiveOperationKind.PLAIN_REPLY:
            content = self._message_content(
                f"Live fuzz plain reply {operation.operation_id}",
                relation={"m.in_reply_to": {"event_id": target_event_id}},
                marker=_source_marker(operation.event_ref, ORIGINAL_REVISION),
            )
            payload = _SentPayload("m.room.message", txn_id, content)
            event_id = await self._send_expected_message(operation, client, payload, room_id)
            self.source_current_markers[event_id] = _source_marker(operation.event_ref, ORIGINAL_REVISION)
            return operation, event_id, payload

        if operation.kind is LiveOperationKind.EDIT:
            edit_marker = _source_marker(operation.target, f"edit:{operation.operation_id}")
            new_content = self._message_content(
                f"Live fuzz edited message {operation.operation_id}",
                marker=edit_marker,
            )
            content = {
                **new_content,
                "m.new_content": new_content,
                "m.relates_to": {"rel_type": "m.replace", "event_id": target_event_id},
            }
            event_id = await client.send_event("m.room.message", txn_id, content, room_id=room_id)
            self.sent_records.append(
                _SentRecord(
                    event_id,
                    room_id,
                    "m.room.message",
                    sender=client.user_id,
                    content=content,
                ),
            )
            # The edit revises the target source in place, so its current marker
            # becomes the edit revision the model must now observe. Push the
            # revision keyed by this edit's event id so a later redaction of this
            # edit can revert the source to whatever revision was current beneath
            # it, even if a newer edit has since landed on top.
            self._push_source_revision(target_event_id, event_id, edit_marker)
            self._edit_event_source[event_id] = target_event_id
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
            self.sent_records.append(
                _SentRecord(
                    event_id,
                    room_id,
                    "m.reaction",
                    sender=client.user_id,
                    reaction_key=reaction_key,
                    content=content,
                ),
            )
            return operation, event_id, None

        if operation.kind is LiveOperationKind.REDACTION:
            event_id = await client.redact(target_event_id, txn_id, room_id=room_id)
            self.redacted_targets[target_event_id] = event_id
            self.sent_records.append(
                _SentRecord(
                    event_id,
                    room_id,
                    "m.room.redaction",
                    sender=client.user_id,
                    redacts=target_event_id,
                    content={"reason": "live cache fuzz"},
                ),
            )
            reverted_source = self._edit_event_source.get(target_event_id)
            if reverted_source is not None:
                # Redacting an ``m.replace`` reverts its target source to the
                # latest surviving revision, so the model correctly ends at that
                # earlier body. Revert the expected marker rather than treating
                # this as a source redaction so the audit does not demand a
                # revision Matrix itself rolled back.
                self._pop_source_revision(reverted_source, target_event_id)
            else:
                self.oracle.refresh_ledger_attributions(min_interval=0.0)
            if reverted_source is None and target_event_id not in self.oracle.settled_sources():
                # A source redacted before its reply settles legitimately races
                # the in-flight response, so its exact cardinality is zero-or-one.
                self.oracle.mark_source_optional(target_event_id)
            return operation, event_id, None

        payload = self.sent_payloads[operation.target]
        event_id = await client.send_event(payload.event_type, payload.txn_id, payload.content, room_id=room_id)
        if event_id != target_event_id:
            msg = f"idempotent retry changed event ID for {operation.target}: {target_event_id} -> {event_id}"
            raise AssertionError(msg)
        return operation, event_id, None

    def _push_source_revision(self, source_event_id: str, edit_event_id: str, marker: str) -> None:
        """Record a new current revision for a source and mirror it as the marker.

        The stack is seeded lazily from the source's already-registered ``orig``
        marker (as a base entry with no edit id) so a redaction of the first edit
        can restore it.
        """
        stack = self._source_revision_stack.get(source_event_id)
        if stack is None:
            base = self.source_current_markers.get(source_event_id)
            stack = [(None, base)] if base is not None else []
            self._source_revision_stack[source_event_id] = stack
        stack.append((edit_event_id, marker))
        self.source_revision_markers[source_event_id][edit_event_id] = marker
        self.source_current_markers[source_event_id] = marker

    def _pop_source_revision(self, source_event_id: str, edit_event_id: str) -> None:
        """Revert a source past one redacted edit, restoring the surviving top.

        Matrix reverts an ``m.replace`` target to its latest *surviving*
        revision, so the redacted edit's entry is removed by identity from
        wherever it sits in the stack — not blindly the top, which would corrupt
        the current marker when a non-newest edit is redacted while a newer one
        still survives. The edit is de-registered first, and a redaction of an
        already-reverted (or never-registered) edit is a no-op so it can never
        drop an unrelated revision.
        """
        if self._edit_event_source.pop(edit_event_id, None) is None:
            return
        stack = self._source_revision_stack.get(source_event_id)
        if not stack:
            return
        for index in range(len(stack) - 1, -1, -1):
            if stack[index][0] == edit_event_id:
                del stack[index]
                break
        else:
            return
        if stack:
            self.source_current_markers[source_event_id] = stack[-1][1]
        else:
            self.source_current_markers.pop(source_event_id, None)

    async def _send_expected_message(
        self,
        operation: LiveOperation,
        client: LiveMatrixClient,
        payload: _SentPayload,
        room_id: str,
    ) -> str:
        """Send one reply-expecting message behind an oracle assertion fence.

        Concurrent target-resolution waiters pump the oracle mid-batch, so a
        fast agent reply must not be classified before its expectation exists.
        """
        self.oracle.begin_expectation_registration()
        registered = False
        try:
            event_id = await client.send_event(payload.event_type, payload.txn_id, payload.content, room_id=room_id)
            self.sent_records.append(
                _SentRecord(
                    event_id,
                    room_id,
                    payload.event_type,
                    sender=client.user_id,
                    content=payload.content,
                ),
            )
            self.oracle.expect(
                operation.event_ref,
                event_id,
                thread=operation.thread,
                client=operation.client,
                sent_at=time.monotonic(),
            )
            registered = True
            return event_id
        finally:
            self.oracle.finish_expectation_registration(validate=registered)

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
        marker: str | None = None,
    ) -> dict[str, Any]:
        # The source-revision marker is appended after the mention so it reaches
        # the model unchanged; the mention and body prefix other code depends on
        # stay untouched.
        marked_body = f"{body} {self.stack.agent_id}" + (f" {marker}" if marker is not None else "")
        content: dict[str, Any] = {
            "msgtype": "m.text",
            "body": marked_body,
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
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=DEFAULT_ARTIFACT_ROOT,
        help="stable ignored directory holding one durable failure bundle per run",
    )
    return parser.parse_args()


async def _run_live(
    stack: ManagedTuwunelStack,
    scenario: LiveFuzzScenario,
    *,
    reply_timeout: float,
    settle_seconds: float,
    pending_grace: float = 1.0,
    runner_sink: Callable[[LiveFuzzRunner], None] | None = None,
    journal: Callable[[Mapping[str, object]], None] | None = None,
) -> dict[str, object]:
    client_count = scenario.thread_count - 1 if scenario.profile == "saturation" else scenario.client_count
    room_ids = tuple(stack.room_ids.get(room_key, stack.room_id) for room_key in stack.room_keys)
    clients = tuple(LiveMatrixClient(stack.homeserver, stack.room_id, room_ids=room_ids) for _ in range(client_count))
    if scenario.profile == "chaos":
        for client in clients:
            client.transport_retry_seconds = 45.0
    runner = LiveFuzzRunner(
        stack,
        clients,
        scenario,
        reply_timeout=reply_timeout,
        settle_seconds=settle_seconds,
        pending_grace=pending_grace,
        journal=journal,
    )
    if runner_sink is not None:
        runner_sink(runner)
    primary_error: BaseException | None = None
    try:
        return await runner.run()
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        close_results = await asyncio.gather(
            *(client.close() for client in clients),
            return_exceptions=True,
        )
        close_errors = [result for result in close_results if isinstance(result, BaseException)]
        if close_errors:
            details = "; ".join(f"{type(error).__name__}: {error}" for error in close_errors)
            if primary_error is not None:
                primary_error.add_note(f"Matrix client cleanup failures: {details}")
            else:
                wrapped = [
                    error if isinstance(error, Exception) else RuntimeError(f"{type(error).__name__}: {error}")
                    for error in close_errors
                ]
                message = "Matrix client cleanup failed"
                raise ExceptionGroup(message, wrapped)


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


DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / "tmp" / "live-fuzz-artifacts"


def _git_root_for_path(path: Path) -> Path | None:
    """Return the exact Git checkout containing one loaded module."""
    result = subprocess.run(
        ("git", "-C", str(path.parent), "rev-parse", "--show-toplevel"),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else None


def _git_state_for_file(
    path: Path,
    *,
    scopes: Collection[Path] = (),
) -> tuple[str | None, bool]:
    """Return the containing Git revision and whether its checkout is dirty."""
    root = _git_root_for_path(path)
    if root is None:
        return None, False
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        return None, False
    tracked = subprocess.run(
        ("git", "-C", str(root), "ls-files", "--error-unmatch", str(resolved.relative_to(root))),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if tracked.returncode:
        return None, True
    relative_scopes = [
        str(scope.resolve().relative_to(root)) for scope in scopes or (path,) if scope.resolve().is_relative_to(root)
    ]
    status = subprocess.run(
        ("git", "-C", str(root), "status", "--short", "--untracked-files=all", "--", *relative_scopes),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    revision = subprocess.run(
        ("git", "-C", str(root), "rev-parse", "HEAD"),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return (
        revision.stdout.strip() if revision.returncode == 0 else None,
        status.returncode != 0 or bool(status.stdout.strip()),
    )


def _git_revision(path: Path) -> str | None:
    """Return one checkout's HEAD, if the path belongs to a Git checkout."""
    result = subprocess.run(
        ("git", "-C", str(path), "rev-parse", "HEAD"),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _require_path_within(path: Path, root: Path, message: str) -> None:
    """Fail closed unless an attested module belongs to its required checkout."""
    if not path.is_relative_to(root):
        error = f"{message}: {path}"
        raise RuntimeError(error)


def _require_git_root(path: Path, expected_root: Path, module_name: str) -> None:
    """Fail closed when a contained path belongs to a nested Git checkout."""
    if _git_root_for_path(path) != expected_root.resolve():
        msg = f"loaded {module_name} module belongs to a nested or different Git checkout"
        raise RuntimeError(msg)


def _validated_child_provenance(
    attestation_path: Path,
    *,
    overlay: str | None,
) -> dict[str, object]:
    """Validate actual child imports against the runner and requested overlay."""
    raw = json.loads(attestation_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = "MindRoom runtime attestation must be a JSON object"
        raise TypeError(msg)
    mindroom_value = raw.get("mindroom_module_path")
    nio_value = raw.get("nio_module_path")
    if not isinstance(mindroom_value, str) or not isinstance(nio_value, str):
        msg = "MindRoom runtime attestation omitted module paths"
        raise TypeError(msg)
    mindroom_path = Path(mindroom_value).resolve()
    nio_path = Path(nio_value).resolve()
    _require_path_within(
        mindroom_path,
        PROJECT_ROOT,
        "loaded MindRoom path is outside the live runner checkout",
    )
    _require_git_root(mindroom_path, PROJECT_ROOT, "MindRoom")
    mindroom_revision, mindroom_dirty = _git_state_for_file(
        mindroom_path,
        scopes=(
            PROJECT_ROOT / "src" / "mindroom",
            Path(__file__),
            PROJECT_ROOT / "pyproject.toml",
            PROJECT_ROOT / "uv.lock",
        ),
    )
    nio_revision, nio_dirty = _git_state_for_file(nio_path, scopes=(nio_path.parent,))
    expected_mindroom_revision = _git_revision(PROJECT_ROOT)
    if mindroom_revision is None or expected_mindroom_revision is None:
        msg = "could not verify the loaded MindRoom revision"
        raise RuntimeError(msg)
    if mindroom_dirty:
        msg = "live fuzz requires a clean loaded MindRoom checkout"
        raise RuntimeError(msg)
    if mindroom_revision != expected_mindroom_revision:
        msg = (
            "loaded MindRoom revision does not match the live runner: "
            f"expected {expected_mindroom_revision}, loaded {mindroom_revision}"
        )
        raise RuntimeError(msg)
    expected_nio_revision: str | None = None
    if overlay is not None:
        overlay_path = Path(overlay).resolve()
        if not nio_path.is_relative_to(overlay_path):
            msg = f"loaded mindroom-nio path is outside requested editable overlay: {nio_path}"
            raise RuntimeError(msg)
        _require_git_root(nio_path, overlay_path, "mindroom-nio")
        nio_revision, nio_dirty = _git_state_for_file(
            nio_path,
            scopes=(
                overlay_path / "src",
                overlay_path / "pyproject.toml",
                overlay_path / "uv.lock",
            ),
        )
        expected_nio_revision = _git_revision(overlay_path)
        if expected_nio_revision is None or nio_revision != expected_nio_revision:
            msg = (
                "loaded mindroom-nio revision does not match the requested overlay: "
                f"expected {expected_nio_revision}, loaded {nio_revision} from {nio_path}"
            )
            raise RuntimeError(msg)
        if nio_dirty:
            msg = "live fuzz requires a clean loaded mindroom-nio overlay"
            raise RuntimeError(msg)
    return {
        **raw,
        "mindroom_revision": mindroom_revision,
        "mindroom_expected_revision": expected_mindroom_revision,
        "mindroom_dirty": mindroom_dirty,
        "nio_revision": nio_revision or "unverified",
        "nio_expected_revision": expected_nio_revision or "",
        "nio_dirty": nio_dirty,
    }


def _run_provenance() -> dict[str, object]:
    """Capture parent identity until child-attested provenance replaces it.

    Only inert build/version identity is recorded; no credentials, tokens, or
    environment secrets are captured.
    """
    provenance: dict[str, object] = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "mindroom_module_path": str(Path(mindroom.__file__ or "").resolve()),
        "nio_module_path": str(Path(nio.__file__ or "").resolve()),
    }
    try:
        head = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        provenance["mindroom_head"] = f"<unavailable: {exc}>"
    else:
        provenance["mindroom_head"] = head.stdout.strip() or f"<git error: {head.stderr.strip()}>"
    try:
        provenance["nio_version"] = version("mindroom-nio")
    except PackageNotFoundError:
        provenance["nio_version"] = "<not installed>"
    return provenance


def _run_mindroom_runtime_child(attestation_path: Path, arguments: list[str]) -> None:
    """Attest actual imported packages, then enter the real MindRoom CLI."""
    from mindroom.cli.main import app  # noqa: PLC0415

    mindroom_file = mindroom.__file__
    nio_file = nio.__file__
    if mindroom_file is None or nio_file is None:
        msg = "runtime child imported packages without filesystem paths"
        raise RuntimeError(msg)
    payload = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "mindroom_module_path": str(Path(mindroom_file).resolve()),
        "nio_module_path": str(Path(nio_file).resolve()),
        "nio_version": version("mindroom-nio"),
    }
    temporary = attestation_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temporary.replace(attestation_path)
    sys.argv = ["mindroom", *arguments]
    app()


def _sanitized_oracle_snapshot(oracle: ExactReplyOracle) -> dict[str, object]:
    """Summarize oracle settlement state without any Matrix credentials.

    Access tokens and raw ``/sync`` state (``next_batch`` and any sync-window
    payload) are deliberately excluded; only opaque event IDs, logical
    references, and settlement counters are retained for diagnosis.
    """
    return {
        "expected_sources": dict(oracle.expected_sources),
        "optional_sources": sorted(oracle.optional_sources),
        "observed_sources": sorted(oracle.observed_sources),
        "unsettled_required_sources": sorted(oracle.unsettled_required_sources()),
        "response_ids": {source: sorted(ids) for source, ids in oracle.response_ids.items()},
        "internal_source_ids": sorted(oracle.internal_source_ids),
        "reply_latencies": {source: round(latency, 3) for source, latency in oracle.reply_latencies.items()},
    }


class FailureBundle:
    """Durable, self-contained evidence for one live fuzz run.

    Created before the disposable stack exists so a run killed mid-startup still
    leaves a manifest. Realized concurrent activity is appended as it happens,
    and on failure the full MindRoom log, ledger, sanitized oracle snapshot,
    model observations, diagnostics, and Tuwunel log are copied into the same
    stable directory before stack teardown removes their sources. Every artifact
    write is isolated so a copy error can never replace the primary fuzz
    assertion.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.journal_path = directory / "realized_journal.jsonl"
        self._cleanup_errors: list[str] = []

    @classmethod
    def create(
        cls,
        root: Path,
        run_id: str,
        *,
        scenario: LiveFuzzScenario,
        provenance: Mapping[str, object],
    ) -> FailureBundle:
        """Make the stable artifact directory and persist immutable run inputs."""
        directory = root / run_id
        directory.mkdir(parents=True, exist_ok=True)
        bundle = cls(directory)
        (directory / "scenario.json").write_text(scenario.to_json() + "\n", encoding="utf-8")
        bundle.update_provenance(provenance)
        bundle.journal_path.touch()
        return bundle

    def record_realized(self, entry: Mapping[str, object]) -> None:
        """Append one realized activity record in true completion order."""
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(entry), sort_keys=True) + "\n")

    def update_provenance(self, provenance: Mapping[str, object]) -> None:
        """Atomically replace parent identity with child-attested runtime identity."""
        destination = self.directory / "provenance.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(dict(provenance), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)

    def retain_pass_receipt(
        self,
        result: Mapping[str, object],
        provenance: Mapping[str, object],
    ) -> Path:
        """Keep compact exact-head PASS evidence after deleting bulky run inputs."""
        receipts = self.directory.parent / "receipts"
        receipts.mkdir(parents=True, exist_ok=True)
        destination = receipts / f"{self.directory.name}.json"
        scenario_bytes = (self.directory / "scenario.json").read_bytes()
        payload = {
            "cleanup": "PASS",
            "result": dict(result),
            "provenance": dict(provenance),
            "scenario_sha256": hashlib.sha256(scenario_bytes).hexdigest(),
        }
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(destination)
        return destination

    def discard(self) -> None:
        """Remove the pre-created bundle after a successful run.

        The directory is created before the run starts so a mid-startup kill
        still leaves a manifest, but a run that passes has no failure to
        preserve. Removing it here keeps ``artifact_root`` from accumulating a
        stale ``scenario.json``/``provenance.json``/journal per successful run.
        """
        shutil.rmtree(self.directory, ignore_errors=True)

    def record_cleanup_error(self, error: BaseException) -> None:
        """Retain teardown failure details without replacing a primary failure."""

        def error_lines(current: BaseException) -> list[str]:
            lines = [f"{type(current).__name__}: {current}"]
            if isinstance(current, BaseExceptionGroup):
                for child in current.exceptions:
                    lines.extend(error_lines(child))
            return lines

        def append_error(destination: Path) -> None:
            with destination.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(error_lines(error)) + "\n")

        self._write_isolated(
            "cleanup_error.txt",
            append_error,
        )

    def _write_isolated(self, name: str, writer: Callable[[Path], None]) -> None:
        """Run one artifact writer, folding any failure into the transcript."""
        try:
            writer(self.directory / name)
        except (OSError, ValueError, TypeError) as exc:
            detail = f"{name}: {exc}"
            self._cleanup_errors.append(detail)
            if name != "artifact_errors.txt":
                with (
                    suppress(OSError),
                    (self.directory / "artifact_errors.txt").open("a", encoding="utf-8") as handle,
                ):
                    handle.write(detail + "\n")

    def finalize(
        self,
        *,
        exception: BaseException,
        log_path: Path,
        ledger_path: Path,
        oracle_snapshot: Mapping[str, object],
        model_observations: Mapping[int, list[str]],
        diagnostics: Mapping[str, int],
        tuwunel_log: str,
    ) -> Path:
        """Copy every durable artifact before the stack is torn down."""

        def copy_text(source: Path) -> Callable[[Path], None]:
            def _copy(destination: Path) -> None:
                if source.exists():
                    destination.write_text(source.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
                else:
                    destination.write_text(f"<missing source: {source}>\n", encoding="utf-8")

            return _copy

        def write_json(payload: object) -> Callable[[Path], None]:
            def _write(destination: Path) -> None:
                destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            return _write

        self._write_isolated(
            "exception.txt",
            lambda destination: destination.write_text(
                f"{type(exception).__name__}: {exception}\n",
                encoding="utf-8",
            ),
        )
        self._write_isolated("mindroom.log", copy_text(log_path))
        self._write_isolated("handled_turns.json", copy_text(ledger_path))
        self._write_isolated("oracle_snapshot.json", write_json(dict(oracle_snapshot)))
        self._write_isolated(
            "model_observations.json",
            write_json({str(call_id): markers for call_id, markers in model_observations.items()}),
        )
        self._write_isolated("diagnostics.json", write_json(dict(diagnostics)))
        self._write_isolated(
            "tuwunel.log",
            lambda destination: destination.write_text(tuwunel_log, encoding="utf-8"),
        )
        return self.directory


def _record_secondary_failure(
    bundle: FailureBundle,
    error: BaseException,
    *,
    label: str,
) -> None:
    """Persist and report one secondary failure without replacing the primary."""
    with suppress(BaseException):
        bundle.record_cleanup_error(error)
    with suppress(BaseException):
        print(f"{label} (ignored): {error}", file=sys.stderr)


def _capture_failed_run(
    args: argparse.Namespace,
    bundle: FailureBundle,
    stack: ManagedTuwunelStack,
    runner: LiveFuzzRunner | None,
    error: BaseException,
) -> None:
    """Capture best-effort evidence and always close the failed stack."""
    try:
        try:
            _persist_failure_bundle(bundle, stack, runner, error)
        except BaseException as bundle_error:
            _record_secondary_failure(bundle, bundle_error, label="Failure bundle capture error")
        try:
            if args.failure_log is not None and stack.log_path.exists():
                args.failure_log.write_text(
                    stack.log_path.read_text(encoding="utf-8", errors="replace"),
                    encoding="utf-8",
                )
        except BaseException as failure_log_error:
            _record_secondary_failure(bundle, failure_log_error, label="Failure-log copy error")
    finally:
        try:
            stack.close()
        except BaseException as cleanup_error:
            _record_secondary_failure(bundle, cleanup_error, label="Live Matrix fuzz cleanup error")


def main() -> None:
    """Run one trace against a fresh disposable real-server stack."""
    if len(sys.argv) >= 4 and sys.argv[1] == "__mindroom_runtime_child__":
        _run_mindroom_runtime_child(Path(sys.argv[2]), sys.argv[3:])
        return
    args = _parse_args()
    scenario = _scenario_from_args(args)
    if args.save_trace is not None:
        args.save_trace.write_text(scenario.to_json() + "\n", encoding="utf-8")
    reply_timeout = args.reply_timeout
    if reply_timeout is None:
        reply_timeout = _PROFILE_REPLY_TIMEOUTS[scenario.profile]

    run_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}"
    bundle = FailureBundle.create(
        args.artifact_root,
        run_id,
        scenario=scenario,
        provenance=_run_provenance(),
    )
    stack = ManagedTuwunelStack(
        stream_profile=_PROFILE_STREAMS[scenario.profile],
        room_keys=_room_keys_for(scenario),
        provenance_sink=bundle.update_provenance,
        artifact_directory=bundle.directory,
    )
    runner_holder: dict[str, LiveFuzzRunner] = {}
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
                runner_sink=lambda runner: runner_holder.__setitem__("runner", runner),
                journal=bundle.record_realized,
            ),
        )
        result["seed"] = args.seed if args.trace is None else "trace"
        result["wall_seconds"] = round(time.monotonic() - started_at, 1)
        result.update(stack.diagnostic_counts())
    except BaseException as exc:
        _capture_failed_run(args, bundle, stack, runner_holder.get("runner"), exc)
        raise
    stack.close()
    provenance = stack.runtime_provenance
    if provenance is None:
        msg = "passing live run omitted child runtime provenance"
        raise RuntimeError(msg)
    receipt = bundle.retain_pass_receipt(result, provenance)
    result["pass_receipt"] = str(receipt)
    print(json.dumps(result, sort_keys=True))
    # A passing run has no failure to preserve. Delete its bundle only after
    # teardown also succeeds, so a cleanup failure retains recovery evidence.
    bundle.discard()


def _persist_failure_bundle(
    bundle: FailureBundle,
    stack: ManagedTuwunelStack,
    runner: LiveFuzzRunner | None,
    exc: BaseException,
) -> None:
    """Copy durable evidence before teardown; never mask the primary failure.

    MindRoom is stopped first so its log is complete, and the Tuwunel log is
    captured while the container still exists. Any error assembling the bundle
    is reported but does not replace the fuzz assertion, which ``main`` re-raises.
    """
    with suppress(BaseException):
        print("MindRoom log tail:", file=sys.stderr)
        print(stack.log_tail(), file=sys.stderr)
    # The exact logical-workload JSON is too large to dump to stderr; it is
    # persisted as scenario.json inside the failure bundle below, and its path
    # is printed so the same operation batches and inputs can be replayed.
    print(f"Replay trace: {bundle.directory / 'scenario.json'}", file=sys.stderr)
    try:
        stack.stop_mindroom()
    except BaseException as stop_exc:
        bundle.record_cleanup_error(stop_exc)
        print(f"MindRoom stop before failure capture failed: {stop_exc}", file=sys.stderr)
    try:
        oracle_snapshot = _sanitized_oracle_snapshot(runner.oracle) if runner is not None else {}
        ledger_path = stack.storage_path / "tracking" / f"{AGENT_NAME}_responded.json"
        path = bundle.finalize(
            exception=exc,
            log_path=stack.log_path,
            ledger_path=ledger_path,
            oracle_snapshot=oracle_snapshot,
            model_observations=_ModelHandler.observations_snapshot(),
            diagnostics=stack.diagnostic_counts(),
            tuwunel_log=stack.tuwunel_log(),
        )
        print(f"Live Matrix fuzz failure bundle: {path}", file=sys.stderr)
    except BaseException as bundle_exc:
        # Evidence-capture errors must never replace the primary fuzz failure.
        print(f"Failure bundle capture error (ignored): {bundle_exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
