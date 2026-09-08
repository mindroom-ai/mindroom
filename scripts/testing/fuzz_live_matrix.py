"""Replay concurrent Matrix mutations against disposable Tuwunel and MindRoom.

Unlike the in-process fuzzers, this runner crosses the real Matrix
transport and the complete MindRoom sync/dispatch/journal path. It starts an
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
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections import defaultdict
from collections.abc import Mapping
from contextlib import closing, suppress
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast
from urllib.parse import quote

import httpx
import nio
import yaml

import mindroom
from mindroom.constants import AI_RUN_METADATA_KEY, ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY, STREAM_STATUS_KEY
from mindroom.dispatch_source import AUTO_RESUME_MESSAGE, TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.handled_turns import TurnRecord, TurnRecordCodec
from mindroom.prompts import AGENT_IDENTITY_CONTEXT_TEMPLATE
from mindroom.streaming import INTERRUPTED_RESPONSE_NOTE, RESTART_INTERRUPTED_RESPONSE_NOTE

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Coroutine
    from io import TextIOWrapper
    from typing import Literal

    from mindroom.turn_store import TurnStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTANCE_REGISTRY = PROJECT_ROOT / "local" / "instances" / "deploy" / "instances.json"
DEFAULT_LIVE_FUZZ_STATE_ROOT = Path.home() / ".mindroom" / "live-fuzz"
MODEL_ID = "mindroom-live-fuzz"
RESTART_MODEL_ID = "mindroom-live-fuzz-replacement"
RECOVERED_MODEL_ID = "mindroom-live-fuzz-recovered"
ORIGINAL_RUNTIME_GENERATION_MARKER = "runtime-generation=original"
REPLACEMENT_RUNTIME_GENERATION_MARKER = "runtime-generation=replacement"
RECOVERED_RUNTIME_GENERATION_MARKER = "runtime-generation=recovered"
FRESH_RESTART_REQUEST = "Synthetic fresh startup request"
AGENT_NAME = "general"
ROUTER_NAME = "router"
ROOM_KEY = "lobby"
LIFECYCLE_COMMAND_TIMEOUT_SECONDS = 180.0
# API, background, ingestion, and response shutdown windows can total 40s;
# leave room for resource release while retaining a bounded outer watchdog.
MINDROOM_SHUTDOWN_TIMEOUT_SECONDS = 60.0
_PROCESS_GROUP_GRACE_SECONDS = 1.0
_PROCESS_GROUP_KILL_SECONDS = 10.0
_PROCESS_GROUP_POLL_SECONDS = 0.05
_STARTUP_MAINTENANCE_PHASES = frozenset(
    {
        "startup_maintenance.rooms_and_memberships",
        "startup_maintenance.runtime_support",
        "startup_maintenance.stale_stream_recovery.initial",
        "startup_maintenance.stale_stream_recovery.joined_room_delta",
    },
)
_STARTUP_PHASE_PATTERN = re.compile(r"\bphase=(startup_maintenance\.[^\s\]]+)")
_STARTUP_STATUS_PATTERN = re.compile(r"\bstatus=([a-z_]+)")
RESTART_SHUTDOWN_FAILURE_MARKER = "runtime_drain_incomplete_with_durable_dispatch_recovery"
ORDERLY_SHUTDOWN_MARKER = "All agent bots stopped"
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_MANAGED_STREAM_OBSERVER_PAGE_SIZE = 500

# What a run is allowed to tell us about itself, keyed by the exact production
# marker that says it. Every one of these has to exist in `src/`:
# a counter whose marker no longer exists prints a zero that reads like
# evidence of health, which is worse than printing nothing, and three of them
# survived here for months after the module that logged them was deleted.
# `test_diagnostic_counters_track_live_production_markers` fails if any entry
# stops being a real marker, so the next deletion cannot leave one behind.
DIAGNOSTIC_MARKERS: dict[str, str] = {
    # A conversation read that stopped short of the prompt window it was asked
    # for. This is the surviving form of the deleted cache's degraded-read
    # signal: the reader is the journal now, but "the turn was built from less
    # history than it wanted" is still a thing that can happen under load.
    "degraded_conversation_reads": "conversation_hydration_ceiling_reached",
    "event_loop_stalls": "event_loop_stall_detected",
    # A shutdown whose drain did not finish and handed its unfinished work to
    # durable recovery. Expected during a restart that lands mid-turn, and the
    # whole point of the journal -- but it must be visible, not silent.
    "restart_drain_incomplete": RESTART_SHUTDOWN_FAILURE_MARKER,
}

# Every event in one room is handled by a single sequential lane, so a wait for
# N outstanding replies is a wait for N agent turns end to end. The budget is
# therefore N times the turn latency this machine is actually showing us, times
# a factor that absorbs the ordinary spread between a median turn and a slow
# one. The factor is the only guess here; the latency it multiplies is measured.
_BUDGET_SAFETY_FACTOR = 3.0

# Silence is what separates a slow machine from a wedged one. A lane that is
# merely slow still finishes turns; a lane that is stuck finishes none. Tolerate
# a few consecutive turn latencies of quiet before calling it stuck, so one
# pathological turn cannot be mistaken for a wedge.
_STALL_TURN_MULTIPLE = 4.0

# Extending a deadline is only defensible while replies keep arriving. Cap the
# extensions anyway so a livelock that dribbles one reply per minute eventually
# fails instead of running forever.
_MAX_BUDGET_EXTENSIONS = 3

# Thread roots are setup, not the concurrency under test, and the room's single
# lane serialises them regardless of how many are in flight. Sending them in
# waves keeps genuine transport-level concurrency while bounding how much work
# any one deadline has to cover and how much a failure report has to explain.
DEFAULT_ROOT_FANOUT = 8

# Sustained capacity fixes every response at 4,800 characters so launch spread
# cannot consume the 45-second all-stream overlap. The profile streams at 80
# characters per second, and this lower bound still rejects a fast or
# non-streaming responder.
SUSTAINED_STREAM_MIN_ACTIVE_SECONDS = 45.0


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
    # Two different product guarantees, so two different operations. A signal
    # MindRoom can answer is a drain: it must come down in order and lose
    # nothing. A kill it cannot answer is a crash: nothing drains, and every
    # committed obligation has to come back from the journal on its own.
    RESTART_MINDROOM = "restart_mindroom"
    CRASH_MINDROOM = "crash_mindroom"
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
    cleanup_sources: tuple[str, ...] = ()

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
        cleanup_sources = value.get("cleanup_sources", [])
        if not isinstance(cleanup_sources, list) or not all(isinstance(source, str) for source in cleanup_sources):
            msg = "cleanup probe sources must be a list of logical source references"
            raise TypeError(msg)
        return cls(
            operation_id=_required_int(value, "operation_id"),
            kind=LiveOperationKind(_required_string(value, "kind")),
            thread=_required_int(value, "thread"),
            target=raw_target,
            client=_required_int(value, "client") if "client" in value else 0,
            cleanup_sources=tuple(cast("list[str]", cleanup_sources)),
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
        _reject_unknown_live_scenario_profile(self)
        self._validate_cleanup_probes()
        if self.profile in {"restart-regression", "sustained-stream-capacity"}:
            _validate_fixed_profile_trace(self)
            return
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
        if self.profile in {"saturation", "short-stream-correctness"}:
            self._validate_saturation_shape()
        if not state.mindroom_running:
            msg = "live Matrix fuzz traces must leave MindRoom running"
            raise ValueError(msg)

    def _validate_cleanup_probes(self) -> None:
        """Require explicit probes to name earlier source redactions in their own thread."""
        source_threads = {f"root:{thread}": thread for thread in range(self.thread_count)}
        redacted: set[str] = set()
        for batch in self.batches:
            for operation in batch:
                if operation.cleanup_sources and (
                    self.profile not in {"fuzz", "chaos"}
                    or operation.kind is not LiveOperationKind.THREAD_MESSAGE
                    or any(
                        source not in redacted or source_threads.get(source) != operation.thread
                        for source in operation.cleanup_sources
                    )
                ):
                    msg = f"cleanup probe {operation.event_ref} must name prior source redactions in its thread"
                    raise ValueError(msg)
            for operation in batch:
                if operation.kind in MESSAGE_KINDS:
                    source_threads[operation.event_ref] = operation.thread
                elif operation.kind is LiveOperationKind.EDIT and operation.target in source_threads:
                    source_threads[operation.event_ref] = source_threads[operation.target]
                if operation.kind is LiveOperationKind.REDACTION and operation.target in source_threads:
                    assert operation.target is not None
                    redacted.add(operation.target)

    def _validate_saturation_shape(self) -> None:
        """Require the exact hot-then-parallel shape executed by the saturation driver."""
        if self.thread_count < 2:
            msg = "saturation traces need one hot thread and at least one parallel thread"
            raise ValueError(msg)
        if self.client_count != 1:
            msg = "saturation traces use implicit per-thread clients and must keep client_count at one"
            raise ValueError(msg)

        parallel_start = next(
            (index for index, batch in enumerate(self.batches) if any(operation.thread != 0 for operation in batch)),
            len(self.batches),
        )
        expected_targets = {thread: f"response:root:{thread}" for thread in range(self.thread_count)}
        parallel_threads = set(range(1, self.thread_count))
        for index, batch in enumerate(self.batches):
            expected_threads = {0} if index < parallel_start else parallel_threads
            batch_threads = [operation.thread for operation in batch]
            if len(batch_threads) != len(expected_threads) or set(batch_threads) != expected_threads:
                msg = "saturation batches need exactly one operation for every expected phase thread"
                raise ValueError(msg)
            for operation in batch:
                if operation.kind is not LiveOperationKind.THREAD_MESSAGE:
                    msg = "saturation traces may contain only thread-message operations"
                    raise ValueError(msg)
                if operation.client != 0:
                    msg = "saturation operation clients are assigned implicitly from their thread"
                    raise ValueError(msg)
                expected_target = expected_targets[operation.thread]
                if operation.target != expected_target:
                    msg = (
                        f"saturation operation {operation.event_ref} must target "
                        f"{expected_target!r}, not {operation.target!r}"
                    )
                    raise ValueError(msg)
                expected_targets[operation.thread] = f"response:{operation.event_ref}"

    def _validate_batch(self, batch: tuple[LiveOperation, ...], state: _ValidationState) -> None:
        if not batch:
            msg = "live Matrix fuzz batches must not be empty"
            raise ValueError(msg)
        if self.profile == "fuzz":
            for operation in batch:
                if operation.kind in LIFECYCLE_KINDS - _INTERRUPTION_KINDS:
                    msg = f"{operation.kind} is not supported by the fuzz profile"
                    raise ValueError(msg)
            _validate_interruption_placement(batch)
            for operation in batch:
                _validate_live_operation(
                    operation,
                    thread_count=self.thread_count,
                    allowed_targets=state.known_events | state.known_responses,
                    message_events=state.message_events,
                    operation_ids=state.operation_ids,
                )
            self._register_batch_events(tuple(op for op in batch if op.kind not in _INTERRUPTION_KINDS), state)
            self._validate_reply_uniqueness(batch)
            return
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
        if self.profile in {"saturation", "short-stream-correctness"} or (
            self.profile == "fuzz" and kind is not LiveOperationKind.RESTART_MINDROOM
        ):
            msg = f"{kind} is not supported by the {self.profile} profile"
            raise ValueError(msg)
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


_INTERRUPTION_KINDS = frozenset({LiveOperationKind.RESTART_MINDROOM, LiveOperationKind.CRASH_MINDROOM})


def _owes_reply(operation: LiveOperation) -> bool:
    """Return whether this operation obliges the agent to answer exactly once."""
    return operation.kind in {
        LiveOperationKind.THREAD_MESSAGE,
        LiveOperationKind.PLAIN_REPLY,
    }


def _validate_interruption_placement(batch: tuple[LiveOperation, ...]) -> None:
    """Reject an interruption that could not possibly land inside a turn.

    The trace, not the runner, is where this has to hold: an interruption the
    generator put in a batch of its own, or after a batch that owes the agent
    nothing, hits an idle process however carefully the runner then times it.
    """
    positions = [index for index, operation in enumerate(batch) if operation.kind in _INTERRUPTION_KINDS]
    if not positions:
        return
    if len(positions) != 1 or positions[0] != len(batch) - 1:
        msg = "a MindRoom restart or crash must be the last operation of exactly one batch"
        raise ValueError(msg)
    if not any(_owes_reply(operation) for operation in batch):
        msg = "a MindRoom restart or crash must interrupt a batch that owes at least one reply"
        raise ValueError(msg)


def _normalized_log(log: str) -> str:
    """Remove renderer control codes before content-free log matching."""
    return _ANSI_ESCAPE_PATTERN.sub("", log)


def _log_count(log: str, *markers: str) -> int:
    """Count log lines containing every content-free marker."""
    return sum(all(marker in line for marker in markers) for line in _normalized_log(log).splitlines())


def _semantic_ingress_markers(
    *,
    agent: str,
    room_id: str,
    event_id: str,
) -> tuple[str, ...]:
    """Return exact structured fields identifying one semantic ingress log."""
    return (
        "Received message",
        f"agent={agent}",
        f"room_id={room_id}",
        f"event_id={event_id}",
    )


def _wait_until(predicate: Callable[[], bool], *, timeout: float) -> bool:
    """Poll one content-free live invariant until its bounded deadline."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.1, remaining))


def _reject_unknown_live_scenario_profile(scenario: LiveFuzzScenario) -> None:
    """Reject profiles without a runner implementation."""
    if scenario.profile not in {
        "fuzz",
        "chaos",
        "saturation",
        "restart-regression",
        "short-stream-correctness",
        "sustained-stream-capacity",
    }:
        msg = f"unsupported live Matrix fuzz profile {scenario.profile!r}"
        raise ValueError(msg)


def _validate_fixed_profile_trace(scenario: LiveFuzzScenario) -> None:
    """Require fixed profiles to own their operations outside replayable traces."""
    restart_count_is_invalid = scenario.profile == "restart-regression" and scenario.thread_count != 1
    if restart_count_is_invalid or scenario.batches:
        msg = f"{scenario.profile} profile requires its fixed empty trace"
        raise ValueError(msg)


def restart_regression_scenario() -> LiveFuzzScenario:
    """Return the fixed config-replacement regression trace."""
    scenario = LiveFuzzScenario(thread_count=1, batches=(), profile="restart-regression")
    scenario.validate()
    return scenario


def sustained_stream_capacity_scenario(*, root_count: int = 200) -> LiveFuzzScenario:
    """Return the fixed no-fault sustained-stream capacity trace."""
    scenario = LiveFuzzScenario(thread_count=root_count, batches=(), profile="sustained-stream-capacity")
    scenario.validate()
    return scenario


@dataclass(frozen=True, slots=True)
class ManagedStreamTerminalAudit:
    """Pure canonical-response and effective-terminal evidence."""

    expected_sources: tuple[str, ...]
    canonical_responses: tuple[tuple[str, str], ...]
    canonical_response_count: int
    missing_sources: tuple[str, ...]
    duplicate_sources: tuple[tuple[str, tuple[str, ...]], ...]
    unexpected_sources: tuple[str, ...]
    invalid_relations: tuple[tuple[str, str | None, str | None], ...]
    invalid_replacements: tuple[str, ...]
    invalid_terminal_transitions: tuple[tuple[str, int], ...]
    noncompleted_sources: tuple[tuple[str, str | None], ...]
    min_active_stream_seconds: float
    max_active_stream_seconds: float
    full_overlap_seconds: float
    peak_active_streams: int


@dataclass(frozen=True, slots=True)
class ManagedStreamDrainCounts:
    """Actionable durable work remaining after managed-stream responses."""

    pending_journal_rows: int
    unacknowledged_outbox_rows: int


@dataclass(frozen=True, slots=True)
class ManagedStreamHealthSample:
    """One parsed runtime and Matrix-sync health observation."""

    healthy: bool
    last_sync_time: datetime | None


@dataclass(frozen=True, slots=True)
class ManagedStreamLogCounts:
    """Managed-stream lifecycle counters at one observation boundary."""

    recovery_abandonment_markers: int


@dataclass(frozen=True, slots=True)
class ManagedStreamBaseline:
    """Observer and log state captured only after warm completion."""

    event_ids: frozenset[str]
    log_counts: ManagedStreamLogCounts


@dataclass(frozen=True, slots=True)
class SustainedStreamCapacitySourceAudit:
    """Exact root-source evidence for the no-fault capacity workload."""

    expected_source_ids: tuple[str, ...]
    observed_source_ids: tuple[str, ...]
    missing_source_ids: tuple[str, ...]
    duplicate_source_ids: tuple[str, ...]
    unexpected_source_ids: tuple[str, ...]
    invalid_source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SustainedStreamCapacityObservation:
    """Frozen evidence evaluated before a sustained-stream capacity PASS."""

    root_count: int
    source_audit: SustainedStreamCapacitySourceAudit
    terminal_audit: ManagedStreamTerminalAudit
    health_samples: tuple[ManagedStreamHealthSample, ...]
    health_samples_while_root_release: int
    durable_drain: ManagedStreamDrainCounts | None
    recovery_abandonment_markers: int
    watchdog_stalls: int
    durable_drain_failure_markers: int
    reaction_settled: bool
    pre_fence_last_sync: datetime | None
    post_fence_last_sync: datetime | None
    clean_shutdown: bool
    phase_durations: tuple[tuple[str, float], ...]


@dataclass(slots=True)
class _ManagedRootLaunchBarrier:
    """Hold every configured root send until an observer has begun."""

    expected_roots: int
    all_entered: asyncio.Event
    release_sends: asyncio.Event
    entered_roots: int = 0

    @classmethod
    def create(cls, expected_roots: int) -> _ManagedRootLaunchBarrier:
        """Create one event-loop-local two-way launch barrier."""
        return cls(
            expected_roots=expected_roots,
            all_entered=asyncio.Event(),
            release_sends=asyncio.Event(),
        )

    async def wait_for_release(self) -> None:
        """Record one entered root and wait for the health-side release."""
        self.entered_roots += 1
        if self.entered_roots == self.expected_roots:
            self.all_entered.set()
        await self.release_sends.wait()


def audit_sustained_stream_capacity_sources(
    events: Collection[Mapping[str, Any]],
    *,
    expected_source_ids: Collection[str],
    load_sender_id: str,
    responder_id: str,
    run_id: str,
) -> SustainedStreamCapacitySourceAudit:
    """Validate exact managed-sender roots from one no-fault workload interval."""
    expected_source_tuple = tuple(expected_source_ids)
    expected = frozenset(expected_source_tuple)
    events_by_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    unexpected: set[str] = set()
    run_marker = re.compile(rf"run={re.escape(run_id)} thread=(\d+)")

    for event in events:
        event_id = event.get("event_id")
        if not isinstance(event_id, str):
            continue
        if event_id in expected:
            events_by_id[event_id].append(event)
            continue
        content = event.get("content")
        body = content.get("body") if isinstance(content, dict) else None
        if (
            event.get("type") == "m.room.message"
            and event.get("sender") == load_sender_id
            and isinstance(body, str)
            and run_marker.search(body) is not None
        ):
            unexpected.add(event_id)

    missing = tuple(source_id for source_id in expected_source_tuple if source_id not in events_by_id)
    duplicates = tuple(source_id for source_id in expected_source_tuple if len(events_by_id[source_id]) > 1)
    invalid: list[str] = []
    for thread, source_id in enumerate(expected_source_tuple):
        expected_marker = f"run={run_id} thread={thread}"
        source_events = events_by_id.get(source_id, ())
        if any(
            event.get("type") != "m.room.message"
            or event.get("sender") != load_sender_id
            or not isinstance((content := event.get("content")), dict)
            or content.get("msgtype") != "m.text"
            or content.get("m.mentions") != {"user_ids": [responder_id]}
            or not isinstance((body := content.get("body")), str)
            or body.count(expected_marker) != 1
            or run_marker.findall(body) != [str(thread)]
            for event in source_events
        ):
            invalid.append(source_id)

    return SustainedStreamCapacitySourceAudit(
        expected_source_ids=expected_source_tuple,
        observed_source_ids=tuple(source_id for source_id in expected_source_tuple if source_id in events_by_id),
        missing_source_ids=missing,
        duplicate_source_ids=duplicates,
        unexpected_source_ids=tuple(sorted(unexpected)),
        invalid_source_ids=tuple(invalid),
    )


@dataclass(frozen=True, slots=True)
class _ManagedStreamEventIndex:
    """Responder originals, edits, and malformed canonical relations."""

    originals: Mapping[str, tuple[Mapping[str, Any], ...]]
    edits: Mapping[str, tuple[Mapping[str, Any], ...]]
    invalid_relations: tuple[tuple[str, str | None, str | None], ...]
    invalid_replacements: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ManagedStreamSourceFold:
    """One exact source's effective response state and active interval."""

    source_event_id: str
    response_event_id: str
    effective_status: str | None
    terminal_transition_count: int
    started_at_ms: int
    finished_at_ms: int


def _recovery_stream_status(event: Mapping[str, Any]) -> str | None:
    """Read the stream status from one canonical response original."""
    content = event.get("content")
    if not isinstance(content, dict):
        return None
    status = content.get("io.mindroom.stream_status")
    return status if isinstance(status, str) else None


def _recovery_replacement_status(event: Mapping[str, Any]) -> str | None:
    """Read a replacement status only from a structurally valid new content body."""
    content = event.get("content")
    if not isinstance(content, dict):
        return None
    new_content = content.get("m.new_content")
    if not isinstance(new_content, dict):
        return None
    body = new_content.get("body")
    msgtype = new_content.get("msgtype")
    status = new_content.get("io.mindroom.stream_status")
    if not isinstance(body, str) or not isinstance(msgtype, str) or not isinstance(status, str):
        return None
    if status == "completed" and msgtype != "m.text":
        return None
    return status


def _recovery_event_order(event: Mapping[str, Any]) -> tuple[int, str]:
    """Return the stable Matrix order used to fold response edits."""
    timestamp = event.get("origin_server_ts")
    event_id = event.get("event_id")
    return (
        timestamp if isinstance(timestamp, int) and not isinstance(timestamp, bool) else 0,
        event_id if isinstance(event_id, str) else "",
    )


def _validated_recovery_replacements(
    originals: Mapping[str, Collection[Mapping[str, Any]]],
    candidates: Mapping[str, Collection[Mapping[str, Any]]],
) -> tuple[dict[str, tuple[Mapping[str, Any], ...]], tuple[str, ...]]:
    """Separate canonical-response replacements from malformed and orphan edits."""
    response_event_ids = {
        cast("str", event["event_id"]) for source_events in originals.values() for event in source_events
    }
    edits: dict[str, tuple[Mapping[str, Any], ...]] = {}
    invalid_replacements: list[str] = []
    for target_event_id, target_edits in candidates.items():
        valid_edits = []
        for event in target_edits:
            event_id = cast("str", event["event_id"])
            if target_event_id not in response_event_ids or _recovery_replacement_status(event) is None:
                invalid_replacements.append(event_id)
            else:
                valid_edits.append(event)
        if valid_edits:
            edits[target_event_id] = tuple(valid_edits)
    return edits, tuple(sorted(invalid_replacements))


def _index_managed_stream_events(
    events: Collection[Mapping[str, Any]],
    *,
    responder_id: str,
) -> _ManagedStreamEventIndex:
    """Index canonical candidates and same-responder replacements."""
    originals: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    replacement_candidates: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    invalid_relations: list[tuple[str, str | None, str | None]] = []
    invalid_replacements: list[str] = []
    for event in events:
        if event.get("type") != "m.room.message" or event.get("sender") != responder_id:
            continue
        event_id = event.get("event_id")
        content = event.get("content")
        if not isinstance(content, dict):
            continue
        relation = content.get("m.relates_to")
        if not isinstance(relation, dict):
            continue
        if relation.get("rel_type") == "m.replace":
            target_event_id = relation.get("event_id")
            if not isinstance(event_id, str) or not isinstance(target_event_id, str):
                invalid_replacements.append(event_id if isinstance(event_id, str) else "<missing-event-id>")
            else:
                replacement_candidates[target_event_id].append(event)
            continue
        if not isinstance(event_id, str):
            continue
        if relation.get("rel_type") != "m.thread":
            continue
        thread_event_id = relation.get("event_id")
        reply = relation.get("m.in_reply_to")
        reply_event_id = reply.get("event_id") if isinstance(reply, dict) else None
        resolved_thread_id = thread_event_id if isinstance(thread_event_id, str) else None
        resolved_reply_id = reply_event_id if isinstance(reply_event_id, str) else None
        if resolved_thread_id is None or resolved_reply_id is None or resolved_thread_id != resolved_reply_id:
            invalid_relations.append((event_id, resolved_thread_id, resolved_reply_id))
            continue
        originals[resolved_reply_id].append(event)
    edits, invalid_candidate_replacements = _validated_recovery_replacements(
        originals,
        replacement_candidates,
    )
    invalid_replacements.extend(invalid_candidate_replacements)

    return _ManagedStreamEventIndex(
        originals={source: tuple(source_events) for source, source_events in originals.items()},
        edits=edits,
        invalid_relations=tuple(sorted(invalid_relations)),
        invalid_replacements=tuple(sorted(invalid_replacements)),
    )


def _fold_managed_stream_source(
    source_event_id: str,
    *,
    index: _ManagedStreamEventIndex,
) -> _ManagedStreamSourceFold | None:
    """Fold one uniquely canonical original with its same-responder edits."""
    originals = index.originals.get(source_event_id, ())
    if len(originals) != 1:
        return None
    original = originals[0]
    response_event_id = cast("str", original["event_id"])
    edits = index.edits.get(response_event_id, ())
    ordered_events = tuple(sorted((original, *edits), key=_recovery_event_order))
    statuses = tuple(
        _recovery_stream_status(event) if event is original else _recovery_replacement_status(event)
        for event in ordered_events
    )
    completed_events = tuple(
        event for event, status in zip(ordered_events, statuses, strict=True) if status == "completed"
    )
    latest = ordered_events[-1]
    terminal = completed_events[0] if len(completed_events) == 1 else latest
    return _ManagedStreamSourceFold(
        source_event_id=source_event_id,
        response_event_id=response_event_id,
        effective_status=statuses[-1],
        terminal_transition_count=len(completed_events),
        started_at_ms=_recovery_event_order(original)[0],
        finished_at_ms=_recovery_event_order(terminal)[0],
    )


def _peak_managed_streams(folds: Collection[_ManagedStreamSourceFold]) -> int:
    """Return peak overlap for half-open original-to-terminal intervals."""
    boundaries = sorted(
        boundary
        for fold in folds
        if fold.finished_at_ms > fold.started_at_ms
        for boundary in ((fold.started_at_ms, 1), (fold.finished_at_ms, -1))
    )
    active_streams = 0
    peak_active_streams = 0
    for _timestamp, delta in boundaries:
        active_streams += delta
        peak_active_streams = max(peak_active_streams, active_streams)
    return peak_active_streams


def _full_managed_stream_overlap_seconds(folds: Collection[_ManagedStreamSourceFold]) -> float:
    """Return the common intersection shared by every folded stream interval."""
    if not folds:
        return 0.0
    return max(
        0.0,
        (min(fold.finished_at_ms for fold in folds) - max(fold.started_at_ms for fold in folds)) / 1000,
    )


def audit_managed_stream_events(
    events: Collection[Mapping[str, Any]],
    *,
    responder_id: str,
    expected_source_ids: Collection[str],
) -> ManagedStreamTerminalAudit:
    """Fold raw Matrix originals and same-responder edits into terminal evidence."""
    expected_sources = tuple(sorted(expected_source_ids))
    expected = frozenset(expected_sources)
    index = _index_managed_stream_events(events, responder_id=responder_id)
    folds = tuple(
        fold for source in expected_sources if (fold := _fold_managed_stream_source(source, index=index)) is not None
    )
    missing_sources = tuple(source for source in expected_sources if not index.originals.get(source))
    duplicate_sources = tuple(
        (
            source,
            tuple(
                sorted(cast("str", event["event_id"]) for event in index.originals.get(source, ())),
            ),
        )
        for source in expected_sources
        if len(index.originals.get(source, ())) > 1
    )
    unexpected_sources = tuple(sorted(source for source in index.originals if source not in expected))
    durations = tuple(max(0.0, (fold.finished_at_ms - fold.started_at_ms) / 1000) for fold in folds)

    return ManagedStreamTerminalAudit(
        expected_sources=expected_sources,
        canonical_responses=tuple((fold.source_event_id, fold.response_event_id) for fold in folds),
        canonical_response_count=sum(len(index.originals.get(source, ())) for source in expected_sources),
        missing_sources=missing_sources,
        duplicate_sources=duplicate_sources,
        unexpected_sources=unexpected_sources,
        invalid_relations=index.invalid_relations,
        invalid_replacements=index.invalid_replacements,
        invalid_terminal_transitions=tuple(
            (fold.response_event_id, fold.terminal_transition_count)
            for fold in folds
            if fold.terminal_transition_count != 1
        ),
        noncompleted_sources=tuple(
            (fold.source_event_id, fold.effective_status) for fold in folds if fold.effective_status != "completed"
        ),
        min_active_stream_seconds=min(durations, default=0.0),
        max_active_stream_seconds=max(durations, default=0.0),
        full_overlap_seconds=_full_managed_stream_overlap_seconds(folds),
        peak_active_streams=_peak_managed_streams(folds),
    )


def evaluate_sustained_stream_capacity(observation: SustainedStreamCapacityObservation) -> tuple[str, ...]:
    """Return every ordinary-capacity acceptance failure in one settled observation."""
    source_audit = observation.source_audit
    terminal_audit = observation.terminal_audit
    expected_source_ids = source_audit.expected_source_ids
    observed_source_ids = source_audit.observed_source_ids
    canonical_sources = tuple(source_id for source_id, _response_id in terminal_audit.canonical_responses)
    canonical_response_ids = tuple(response_id for _source_id, response_id in terminal_audit.canonical_responses)
    before, after = observation.pre_fence_last_sync, observation.post_fence_last_sync
    failures = (
        (
            f"root_source_count expected={observation.root_count} observed={len(expected_source_ids)}"
            if observation.root_count != len(expected_source_ids)
            else ""
        ),
        (
            f"root_source_audit_duplicate_ids={expected_source_ids}"
            if len(expected_source_ids) != len(frozenset(expected_source_ids))
            else ""
        ),
        (
            f"root_source_audit_duplicate_ids={observed_source_ids}"
            if len(observed_source_ids) != len(frozenset(observed_source_ids))
            else ""
        ),
        (
            f"root_source_audit_incomplete expected={expected_source_ids} observed={observed_source_ids}"
            if frozenset(observed_source_ids) != frozenset(expected_source_ids)
            else ""
        ),
        f"missing_root_sources={source_audit.missing_source_ids}" if source_audit.missing_source_ids else "",
        f"duplicate_root_sources={source_audit.duplicate_source_ids}" if source_audit.duplicate_source_ids else "",
        f"unknown_root_sources={source_audit.unexpected_source_ids}" if source_audit.unexpected_source_ids else "",
        f"invalid_root_sources={source_audit.invalid_source_ids}" if source_audit.invalid_source_ids else "",
        (
            f"root_source_terminal_mismatch sources={expected_source_ids} terminals={terminal_audit.expected_sources}"
            if frozenset(expected_source_ids) != frozenset(terminal_audit.expected_sources)
            else ""
        ),
        (
            f"terminal_expected_sources expected={observation.root_count} observed={terminal_audit.expected_sources}"
            if len(terminal_audit.expected_sources) != observation.root_count
            or len(terminal_audit.expected_sources) != len(frozenset(terminal_audit.expected_sources))
            else ""
        ),
        (
            f"canonical_responses expected={observation.root_count} observed={len(terminal_audit.canonical_responses)}"
            if len(terminal_audit.canonical_responses) != observation.root_count
            else ""
        ),
        (
            f"canonical_response_count expected={observation.root_count} "
            f"observed={terminal_audit.canonical_response_count}"
            if terminal_audit.canonical_response_count != observation.root_count
            else ""
        ),
        (
            f"canonical_response_evidence_count responses={len(terminal_audit.canonical_responses)} "
            f"count={terminal_audit.canonical_response_count}"
            if len(terminal_audit.canonical_responses) != terminal_audit.canonical_response_count
            else ""
        ),
        (
            f"canonical_response_source_ids expected={expected_source_ids} observed={canonical_sources}"
            if len(canonical_sources) != len(frozenset(canonical_sources))
            or frozenset(canonical_sources) != frozenset(expected_source_ids)
            else ""
        ),
        (
            f"canonical_response_ids={canonical_response_ids}"
            if len(canonical_response_ids) != len(frozenset(canonical_response_ids))
            else ""
        ),
        f"missing_sources={terminal_audit.missing_sources}" if terminal_audit.missing_sources else "",
        f"duplicate_sources={terminal_audit.duplicate_sources}" if terminal_audit.duplicate_sources else "",
        f"unknown_sources={terminal_audit.unexpected_sources}" if terminal_audit.unexpected_sources else "",
        f"invalid_relations={terminal_audit.invalid_relations}" if terminal_audit.invalid_relations else "",
        (f"invalid_replacements={terminal_audit.invalid_replacements}" if terminal_audit.invalid_replacements else ""),
        (
            f"invalid_terminal_transitions={terminal_audit.invalid_terminal_transitions}"
            if terminal_audit.invalid_terminal_transitions
            else ""
        ),
        f"noncompleted_sources={terminal_audit.noncompleted_sources}" if terminal_audit.noncompleted_sources else "",
        (
            f"active_stream_duration_too_short={terminal_audit.min_active_stream_seconds:.3f} "
            f"minimum={SUSTAINED_STREAM_MIN_ACTIVE_SECONDS:.3f}"
            if terminal_audit.min_active_stream_seconds < SUSTAINED_STREAM_MIN_ACTIVE_SECONDS
            else ""
        ),
        (
            f"peak_active_streams={terminal_audit.peak_active_streams} expected={observation.root_count}"
            if terminal_audit.peak_active_streams != observation.root_count
            else ""
        ),
        (
            f"full_overlap_too_short={terminal_audit.full_overlap_seconds:.3f} "
            f"minimum={SUSTAINED_STREAM_MIN_ACTIVE_SECONDS:.3f}"
            if terminal_audit.full_overlap_seconds < SUSTAINED_STREAM_MIN_ACTIVE_SECONDS
            else ""
        ),
        (
            "health_samples_unhealthy"
            if not observation.health_samples
            or any(not sample.healthy or sample.last_sync_time is None for sample in observation.health_samples)
            else ""
        ),
        (
            f"health_samples_while_root_release={observation.health_samples_while_root_release}"
            if observation.health_samples_while_root_release < 1
            else ""
        ),
        (
            f"pending_journal_rows={observation.durable_drain.pending_journal_rows}"
            if observation.durable_drain is not None and observation.durable_drain.pending_journal_rows
            else ""
        ),
        (
            f"unacknowledged_outbox_rows={observation.durable_drain.unacknowledged_outbox_rows}"
            if observation.durable_drain is not None and observation.durable_drain.unacknowledged_outbox_rows
            else ""
        ),
        (
            f"recovery_abandonment_markers={observation.recovery_abandonment_markers}"
            if observation.recovery_abandonment_markers
            else ""
        ),
        f"watchdog_stalls={observation.watchdog_stalls}" if observation.watchdog_stalls else "",
        (
            f"durable_drain_failure_markers={observation.durable_drain_failure_markers}"
            if observation.durable_drain_failure_markers
            else ""
        ),
        "reaction_not_settled" if not observation.reaction_settled else "",
        "sync_progress_absent_after_fence" if before is None or after is None or after <= before else "",
        "shutdown_not_clean" if not observation.clean_shutdown else "",
    )
    return tuple(failure for failure in failures if failure)


def _restart_failure(
    invariant: str,
    *,
    event_category: str,
    phase: str,
    observed: int | bool,
    step: int,
) -> str:
    """Format content-free restart failure coordinates."""
    return f"invariant={invariant} step={step} event_category={event_category} phase={phase} observed={observed}"


def _raise_restart_failures(failures: Collection[str]) -> None:
    """Raise one consistently headed restart-regression report."""
    raise AssertionError("restart regression invariant failures:\n" + "\n".join(failures))


def _require_restart_invariant(
    passed: bool,
    invariant: str,
    *,
    event_category: str,
    phase: str,
    observed: int | bool,
    step: int,
) -> None:
    """Raise one consistently formatted restart-boundary failure."""
    if passed:
        return
    failure = _restart_failure(
        invariant,
        event_category=event_category,
        phase=phase,
        observed=observed,
        step=step,
    )
    _raise_restart_failures((failure,))


@dataclass(frozen=True, slots=True)
class RestartRegressionObservation:
    """Content-free evidence collected after replacement activity settles."""

    historical_output_counts: tuple[int, int]
    historical_callback_counts: tuple[int, int]
    projected_after_answer_count: int
    historical_projected_on_room_read: int
    fresh_agent_output_count: int
    fresh_router_output_count: int
    fresh_response_complete: bool
    fresh_semantic_ingress_count_before_restart: int
    fresh_semantic_ingress_count: int
    recovered_generation_response_observed: bool
    fresh_obligation_recovered: bool
    fresh_prompt_observed: bool
    historical_in_fresh_prompt: bool
    orderly_drain_completed: bool | None


@dataclass(frozen=True, slots=True)
class _RestartInvariantCheck:
    """One typed content-free restart invariant."""

    invariant: str
    observed: int | bool | None
    expected: int | bool
    event_category: str
    phase: str
    step: int
    wait_until_passes: bool = False

    @property
    def passed(self) -> bool:
        """Return whether the exact expected value has been observed."""
        return self.observed == self.expected


def _restart_invariant_checks(
    observation: RestartRegressionObservation,
) -> tuple[_RestartInvariantCheck, ...]:
    """Return the single typed restart-invariant definition."""
    return (
        _RestartInvariantCheck(
            invariant="historical_output_suppressed",
            observed=observation.historical_output_counts[0],
            expected=0,
            event_category="historical_text",
            phase="replacement_sync",
            step=1,
        ),
        _RestartInvariantCheck(
            invariant="historical_output_suppressed",
            observed=observation.historical_output_counts[1],
            expected=0,
            event_category="historical_media",
            phase="replacement_sync",
            step=2,
        ),
        _RestartInvariantCheck(
            invariant="historical_callback_suppressed",
            observed=observation.historical_callback_counts[0],
            expected=0,
            event_category="historical_text",
            phase="replacement_sync",
            step=1,
        ),
        _RestartInvariantCheck(
            invariant="historical_callback_suppressed",
            observed=observation.historical_callback_counts[1],
            expected=0,
            event_category="historical_media",
            phase="replacement_sync",
            step=2,
        ),
        _RestartInvariantCheck(
            invariant="historical_events_projected_on_room_read",
            observed=observation.historical_projected_on_room_read,
            expected=2,
            event_category="historical_events",
            phase="room_read",
            step=3,
        ),
        _RestartInvariantCheck(
            invariant="fresh_agent_response_exactly_once",
            observed=observation.fresh_agent_output_count,
            expected=1,
            event_category="fresh_user",
            phase="recovery_startup",
            step=4,
            wait_until_passes=True,
        ),
        _RestartInvariantCheck(
            invariant="fresh_router_response_suppressed",
            observed=observation.fresh_router_output_count,
            expected=0,
            event_category="fresh_user",
            phase="recovery_startup",
            step=4,
        ),
        _RestartInvariantCheck(
            invariant="fresh_response_complete",
            observed=observation.fresh_response_complete,
            expected=True,
            event_category="fresh_user",
            phase="recovery_startup",
            step=4,
            wait_until_passes=True,
        ),
        _RestartInvariantCheck(
            invariant="fresh_semantic_ingress_replayed_after_restart",
            observed=(
                observation.fresh_semantic_ingress_count - observation.fresh_semantic_ingress_count_before_restart
            ),
            expected=1,
            event_category="fresh_user",
            phase="recovery_startup",
            step=4,
            wait_until_passes=True,
        ),
        _RestartInvariantCheck(
            invariant="recovered_generation_response_observed",
            observed=observation.recovered_generation_response_observed,
            expected=True,
            event_category="fresh_user",
            phase="recovery_startup",
            step=4,
            wait_until_passes=True,
        ),
        _RestartInvariantCheck(
            invariant="fresh_journal_event_recovered",
            observed=observation.fresh_obligation_recovered,
            expected=True,
            event_category="fresh_user",
            phase="recovery_startup",
            step=4,
            wait_until_passes=True,
        ),
        _RestartInvariantCheck(
            invariant="fresh_prompt_observed",
            observed=observation.fresh_prompt_observed,
            expected=True,
            event_category="fresh_user",
            phase="execution",
            step=4,
            wait_until_passes=True,
        ),
        _RestartInvariantCheck(
            invariant="historical_events_absent_from_fresh_prompt",
            observed=observation.historical_in_fresh_prompt,
            expected=False,
            event_category="historical_events",
            phase="execution",
            step=4,
        ),
        _RestartInvariantCheck(
            invariant="orderly_drain_completed",
            observed=observation.orderly_drain_completed,
            expected=True,
            event_category="lifecycle",
            phase="observation",
            step=4,
        ),
    )


def _positive_restart_evidence_ready(observation: RestartRegressionObservation) -> bool:
    """Return whether every positive invariant has reached its exact value."""
    return all(check.passed for check in _restart_invariant_checks(observation) if check.wait_until_passes)


def evaluate_restart_regression(observation: RestartRegressionObservation) -> tuple[str, ...]:
    """Return every violated restart invariant from one settled observation."""
    return tuple(
        _restart_failure(
            check.invariant,
            event_category=check.event_category,
            phase=check.phase,
            observed=check.observed,
            step=check.step,
        )
        for check in _restart_invariant_checks(observation)
        if check.observed is not None and not check.passed
    )


def _restart_prompt_observation(log: str, fresh_event_id: str, old_event_ids: tuple[str, str]) -> tuple[bool, bool]:
    """Report fresh prompt presence and any historical-event overlap."""
    fresh_lines = [
        line
        for line in _normalized_log(log).splitlines()
        if "Preparing agent and prompt" in line and f"agent={AGENT_NAME}" in line and fresh_event_id in line
    ]
    return bool(fresh_lines), any(event_id in line for line in fresh_lines for event_id in old_event_ids)


def _validate_live_operation(
    operation: LiveOperation,
    *,
    thread_count: int,
    operation_ids: set[int],
    allowed_targets: set[str],
    message_events: set[str],
) -> None:
    if operation.operation_id in operation_ids:
        msg = f"duplicate live Matrix fuzz operation ID {operation.operation_id}"
        raise ValueError(msg)
    operation_ids.add(operation.operation_id)
    if not 0 <= operation.thread < thread_count:
        msg = f"invalid thread {operation.thread}"
        raise ValueError(msg)
    if operation.kind in _INTERRUPTION_KINDS:
        if operation.target is not None:
            msg = "a MindRoom restart or crash must not have a target"
            raise ValueError(msg)
        return
    if operation.target is None:
        msg = f"{operation.kind} requires a target"
        raise ValueError(msg)
    if operation.target not in allowed_targets:
        msg = f"unknown or same-batch target {operation.target!r}"
        raise ValueError(msg)
    if operation.kind is LiveOperationKind.IDEMPOTENT_RETRY and operation.target not in message_events:
        msg = "idempotent retries may only target messages"
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


def _generate_batch(
    randomizer: random.Random,
    state: _ScenarioGenerationState,
    *,
    first_operation_id: int,
    batch_size: int,
    thread_count: int,
) -> list[LiveOperation]:
    """Choose one batch of operations with at most one reply owed per thread."""
    operations: list[LiveOperation] = []
    reply_threads: set[int] = set()
    batch_edited: set[str] = set()
    batch_redacted: set[str] = set()
    for offset in range(batch_size):
        operation = _choose_operation(
            randomizer,
            state,
            operation_id=first_operation_id + offset,
            thread_count=thread_count,
            batch_edited=batch_edited,
            batch_redacted=batch_redacted,
        )
        if _owes_reply(operation) and operation.thread in reply_threads:
            operation = LiveOperation(
                operation_id=operation.operation_id,
                kind=LiveOperationKind.REACTION,
                thread=operation.thread,
                target=randomizer.choice(state.reaction_targets[operation.thread]),
            )
        operations.append(operation)
        if _owes_reply(operation):
            reply_threads.add(operation.thread)
        elif operation.kind is LiveOperationKind.EDIT:
            assert operation.target is not None
            batch_edited.add(operation.target)
        elif operation.kind is LiveOperationKind.REDACTION:
            assert operation.target is not None
            batch_redacted.add(operation.target)
    return operations


def _batch_interrupted_by(
    randomizer: random.Random,
    state: _ScenarioGenerationState,
    operations: list[LiveOperation],
    *,
    kind: LiveOperationKind,
    interruption_operation_id: int,
) -> tuple[LiveOperation, ...]:
    """Return this batch with an interruption appended, guaranteed to land mid-turn.

    An interruption is only worth taking while a turn is owed, so the batch it
    ends must contain at least one message the agent still has to answer. A
    batch of nothing but reactions and edits is promoted by turning its first
    operation into a thread message; no reply is owed yet on that thread,
    because the batch had none at all.
    """
    if not any(_owes_reply(operation) for operation in operations):
        head = operations[0]
        operations[0] = LiveOperation(
            operation_id=head.operation_id,
            kind=LiveOperationKind.THREAD_MESSAGE,
            thread=head.thread,
            target=randomizer.choice(state.messages[head.thread]),
        )
    return (
        *operations,
        LiveOperation(
            operation_id=interruption_operation_id,
            kind=kind,
            thread=0,
            target=None,
        ),
    )


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
    interruptions = 0

    while generated < steps:
        batch_size = min(steps - generated, randomizer.randint(1, max_batch_size))
        operations = _generate_batch(
            randomizer,
            state,
            first_operation_id=operation_id,
            batch_size=batch_size,
            thread_count=thread_count,
        )
        operation_id += batch_size
        generated += len(operations)

        # The interruption rides along with the work rather than following it.
        # One in a batch of its own is only ever taken after the previous
        # batch's replies have all landed, which interrupts an idle process: it
        # can never exercise the recovery the journal is for. Graceful restarts
        # and hard crashes alternate because they prove different things, and a
        # run that only ever drained cleanly has not tested the journal at all.
        batch = tuple(operations)
        if restart_interval and generated >= next_restart:
            batch = _batch_interrupted_by(
                randomizer,
                state,
                operations,
                kind=(
                    LiveOperationKind.RESTART_MINDROOM if interruptions % 2 == 0 else LiveOperationKind.CRASH_MINDROOM
                ),
                interruption_operation_id=operation_id,
            )
            interruptions += 1
            operation_id += 1
            next_restart += restart_interval

        batches.append(batch)
        _update_generation_state(state, batch)

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


def short_stream_correctness_scenario(
    *,
    hot_turns: int = 100,
    parallel_threads: int = 12,
    parallel_turns: int = 8,
) -> LiveFuzzScenario:
    """Reproduce the existing long-thread plus 12-way short-stream workload."""
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
        profile="short-stream-correctness",
    )
    scenario.validate()
    return scenario


def saturation_scenario(
    *,
    hot_turns: int = 100,
    parallel_threads: int = 12,
    parallel_turns: int = 8,
) -> LiveFuzzScenario:
    """Return the short-stream workload under its original replay profile name."""
    return replace(
        short_stream_correctness_scenario(
            hot_turns=hot_turns,
            parallel_threads=parallel_threads,
            parallel_turns=parallel_turns,
        ),
        profile="saturation",
    )


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


@dataclass(frozen=True, slots=True)
class ModelCallObservation:
    """One exact request observed under the model server's lock."""

    markers: frozenset[str]
    monotonic_ns: int


class _ModelHandler(BaseHTTPRequestHandler):
    """Small deterministic OpenAI-compatible endpoint for live transport tests.

    When ``slow_call_modulus`` is positive, marker fingerprints divisible by it
    stream ``slow_stream_segments`` segments with ``slow_stream_delay`` between
    chunks after an initial ``first_token_delay``.
    Marker-free calls are always fast.
    """

    protocol_version = "HTTP/1.1"
    call_ids = itertools.count(1)
    stream_segments = 4
    stream_delay = 0.001
    slow_call_modulus = 0
    slow_stream_segments = 120
    slow_stream_delay = 0.02
    first_token_delay = 0.0
    blocked_request_timeout: float = 180.0
    blocked_request_started = threading.Event()
    blocked_request_release = threading.Event()

    # Class-level observation map guarded by a lock because the stub runs under a
    # ThreadingHTTPServer: concurrent MindRoom requests each land in their own
    # handler thread. Each entry records the source-revision markers seen on the
    # FINAL user message of one model call, keyed by that call's assigned id.
    _observation_lock = threading.Lock()
    _observed_markers: ClassVar[dict[int, frozenset[str]]] = {}
    _full_request_markers: ClassVar[dict[int, frozenset[str]]] = {}
    _timed_observations: ClassVar[dict[int, ModelCallObservation]] = {}

    @classmethod
    def reset_observations(cls) -> None:
        """Clear observed markers and restart call-id numbering for a fresh stack."""
        with cls._observation_lock:
            cls._observed_markers = {}
            cls._full_request_markers = {}
            cls._timed_observations = {}
        cls.call_ids = itertools.count(1)
        cls.blocked_request_started.clear()
        cls.blocked_request_release.clear()

    @classmethod
    def _record_observation(
        cls,
        call_id: int,
        markers: frozenset[str],
        *,
        full_request_markers: frozenset[str] = frozenset(),
    ) -> None:
        with cls._observation_lock:
            cls._timed_observations[call_id] = ModelCallObservation(markers, time.monotonic_ns())
            cls._observed_markers[call_id] = markers
            cls._full_request_markers[call_id] = full_request_markers

    @classmethod
    def timed_observations_snapshot(cls) -> dict[int, ModelCallObservation]:
        """Retain exact host-clock facts across runtime process generations."""
        with cls._observation_lock:
            return dict(cls._timed_observations)

    @classmethod
    def full_request_markers_for(cls, call_id: int) -> frozenset[str]:
        """Return markers anywhere in the model input, including historical turns."""
        with cls._observation_lock:
            return cls._full_request_markers.get(call_id, frozenset())

    @classmethod
    def full_request_observations_snapshot(cls) -> dict[int, list[str]]:
        """Retain full-request marker evidence separately from current-turn observations."""
        with cls._observation_lock:
            return {call_id: sorted(markers) for call_id, markers in cls._full_request_markers.items()}

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
                    "data": [
                        {"id": model_id, "object": "model", "owned_by": "mindroom-fuzz"}
                        for model_id in (MODEL_ID, RESTART_MODEL_ID, RECOVERED_MODEL_ID)
                    ],
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
        self._record_observation(
            call_id,
            self._final_user_markers(payload),
            full_request_markers=_parse_markers(json.dumps(payload)),
        )
        model_id = payload.get("model")
        if model_id == RESTART_MODEL_ID and FRESH_RESTART_REQUEST in json.dumps(payload):
            self.blocked_request_started.set()
            self.blocked_request_release.wait(timeout=self.blocked_request_timeout)
        generation_marker = {
            RESTART_MODEL_ID: REPLACEMENT_RUNTIME_GENERATION_MARKER,
            RECOVERED_MODEL_ID: RECOVERED_RUNTIME_GENERATION_MARKER,
        }.get(model_id, ORIGINAL_RUNTIME_GENERATION_MARKER)
        content = self._response_text(call_id, generation_marker)
        if self._is_slow_call(call_id) and self.first_token_delay > 0:
            time.sleep(self.first_token_delay)
        try:
            if payload.get("stream") is True:
                self._send_stream(call_id, content)
                return
            self._send_json(
                {
                    "id": f"live-fuzz-response-{call_id}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model_id,
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
        except (BrokenPipeError, ConnectionResetError):
            # Chaos and restart-regression intentionally kill MindRoom with a
            # model request in flight, abandoning the HTTP connection.
            self.close_connection = True

    @classmethod
    def _response_text(
        cls,
        call_id: int,
        generation_marker: str = ORIGINAL_RUNTIME_GENERATION_MARKER,
    ) -> str:
        segments = " ".join(f"segment-{index:03d}" for index in range(cls.segments_for(call_id)))
        return f"LIVE-FUZZ call={call_id} {generation_marker} {segments} END call={call_id}"

    @classmethod
    def response_text_for(cls, call_id: int) -> str:
        """Return the exact completed body one model call must produce."""
        return cls._response_text(call_id)

    def _send_stream(self, call_id: int, content: str, model_id: str = MODEL_ID) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        base = {
            "id": f"live-fuzz-response-{call_id}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model_id,
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


def _hard_kill_mindroom_process(process: subprocess.Popen[str]) -> None:
    """Require proof that SIGKILL reached the managed process group."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError as exc:
        return_code = process.wait(timeout=10)
        msg = f"MindRoom exited before managed SIGKILL delivery with status {return_code}"
        raise RuntimeError(msg) from exc
    return_code = process.wait(timeout=10)
    if return_code != -int(signal.SIGKILL):
        msg = f"MindRoom hard kill exited with status {return_code}"
        raise RuntimeError(msg)


def _wait_for_process_group_exit(
    process_group_id: int,
    *,
    timeout_seconds: float,
) -> bool:
    """Wait bounded time until no process remains in the managed group."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_PROCESS_GROUP_POLL_SECONDS)


def _cleanup_surviving_process_group(process_group_id: int) -> bool:
    """Kill a group surviving graceful leader exit and report that cleanup was required."""
    if _wait_for_process_group_exit(
        process_group_id,
        timeout_seconds=_PROCESS_GROUP_GRACE_SECONDS,
    ):
        return False
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return False
    if not _wait_for_process_group_exit(
        process_group_id,
        timeout_seconds=_PROCESS_GROUP_KILL_SECONDS,
    ):
        msg = "MindRoom process group survived SIGKILL cleanup"
        raise TimeoutError(msg)
    return True


def _graceful_shutdown_failure(
    *,
    return_code: int,
    required_group_kill: bool,
    child_shutdown_completed: bool,
) -> str | None:
    """Return why a managed graceful stop lacks one of its required proofs."""
    if return_code not in {0, -int(signal.SIGINT), 128 + int(signal.SIGINT)}:
        return f"MindRoom graceful shutdown exited with status {return_code}"
    if required_group_kill:
        return "MindRoom process group survived graceful SIGINT and required SIGKILL"
    if not child_shutdown_completed:
        return "MindRoom graceful shutdown omitted a fresh orderly shutdown marker"
    return None


def _attempt_cleanup(
    errors: list[tuple[str, BaseException]],
    label: str,
    action: Callable[[], object],
) -> bool:
    """Run one teardown stage while retaining its failure."""
    try:
        action()
    except BaseException as exc:
        errors.append((label, exc))
        return False
    return True


def _join_model_server_thread(thread: threading.Thread) -> None:
    """Join the model-server thread or report that it survived teardown."""
    thread.join(timeout=5)
    if thread.is_alive():
        msg = "thread remained alive"
        raise RuntimeError(msg)


def _format_cleanup_failure(label: str, error: BaseException) -> str:
    """Describe one retained teardown failure with its owning stage."""
    return f"{label}: {type(error).__name__}: {error}"


def _raise_cleanup_failures(
    errors: list[tuple[str, BaseException]],
    *,
    message: str,
) -> None:
    """Raise retained teardown failures without replacing control-flow exits."""
    if not errors:
        return
    first_interrupt = next(
        (
            (index, error)
            for index, (_label, error) in enumerate(errors)
            if isinstance(error, (KeyboardInterrupt, SystemExit))
        ),
        None,
    )
    if first_interrupt is not None:
        interrupt_index, interrupt = first_interrupt
        for index, (label, error) in enumerate(errors):
            if index != interrupt_index:
                interrupt.add_note(_format_cleanup_failure(label, error))
        raise interrupt
    wrapped = [RuntimeError(f"{label}: {error}") for label, error in errors]
    raise ExceptionGroup(message, wrapped)


@dataclass(frozen=True, slots=True)
class StreamProfile:
    """Deterministic model-stub stream shape for one live run."""

    stream_segments: int = 4
    stream_delay: float = 0.001
    slow_call_modulus: int = 0
    slow_stream_segments: int = 120
    slow_stream_delay: float = 0.02
    first_token_delay: float = 0.0


def _command_output(*command: str) -> str:
    """Return one advisory command's output, or empty when it is unavailable."""
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


@dataclass(frozen=True, slots=True)
class HostLoadReport:
    """What else the machine is doing while this proof runs.

    A live run competes for the same cores as everything else on the host, and
    a contended host is the single most common reason a passing proof turns
    red. Reporting the contention up front means a slow run can be recognised
    as a slow run instead of being investigated as a defect.
    """

    cpu_count: int
    load_average: tuple[float, float, float]
    docker_cpus: int | None
    docker_memory_bytes: int | None
    competing_test_processes: int

    @property
    def load_per_cpu(self) -> float:
        """Return the one-minute load carried by each host core."""
        return self.load_average[0] / self.cpu_count if self.cpu_count else 0.0

    @property
    def contended(self) -> bool:
        """Return whether the host already has a runnable process per core."""
        return self.load_per_cpu >= 1.0 or self.competing_test_processes > 0

    def as_dict(self) -> dict[str, float | int | None]:
        """Return the report in the run's machine-readable result shape."""
        return {
            "host_cpu_count": self.cpu_count,
            "host_load_1m": round(self.load_average[0], 2),
            "host_load_per_cpu": round(self.load_per_cpu, 2),
            "docker_cpus": self.docker_cpus,
            "docker_memory_bytes": self.docker_memory_bytes,
            "competing_test_processes": self.competing_test_processes,
        }

    def render(self) -> str:
        """Return one human-readable preflight line."""
        one, five, fifteen = self.load_average
        docker = (
            f"docker {self.docker_cpus} cpus / {self.docker_memory_bytes // (1024**3)} GiB"
            if self.docker_cpus is not None and self.docker_memory_bytes is not None
            else "docker limits unavailable"
        )
        headline = (
            f"host {self.cpu_count} cpus, load {one:.2f}/{five:.2f}/{fifteen:.2f} "
            f"({self.load_per_cpu:.2f} per cpu), {docker}, "
            f"{self.competing_test_processes} competing test processes"
        )
        if not self.contended:
            return headline
        return f"{headline}\nWARNING: this machine is already busy; agent turns will be slower than a quiet host"


def collect_host_load_report() -> HostLoadReport:
    """Measure the contention this run starts under."""
    docker_info = _command_output("docker", "info", "--format", "{{.NCPU}} {{.MemTotal}}").split()
    docker_cpus, docker_memory_bytes = (
        (int(docker_info[0]), int(docker_info[1]))
        if len(docker_info) == 2 and docker_info[0].isdigit()
        else (None, None)
    )
    own_pid = str(os.getpid())
    competing = [
        line
        for line in _command_output("pgrep", "-f", "pytest").splitlines()
        if line.strip() and line.strip() != own_pid
    ]
    return HostLoadReport(
        cpu_count=os.cpu_count() or 1,
        load_average=os.getloadavg(),
        docker_cpus=docker_cpus,
        docker_memory_bytes=docker_memory_bytes,
        competing_test_processes=len(competing),
    )


@dataclass(frozen=True, slots=True)
class WaitBudget:
    """How long a wait for `turns` sequential agent turns may take.

    The deadline is the work multiplied by the measured cost of that work,
    never a flat constant: a batch that demands forty-five turns of a
    single-threaded lane cannot be held to the same clock as a batch that
    demands one. ``floor_seconds`` is the operator's single-turn deadline and
    keeps small waits exactly as strict as they were before measurement.
    """

    turns: int
    per_turn_seconds: float
    settle_seconds: float
    floor_seconds: float

    @property
    def seconds(self) -> float:
        """Return the deadline for completing every outstanding turn."""
        return max(self.floor_seconds, self.turns * self.per_turn_seconds * _BUDGET_SAFETY_FACTOR) + self.settle_seconds

    @property
    def stall_seconds(self) -> float:
        """Return how long total silence means wedged rather than slow."""
        return max(self.floor_seconds, self.per_turn_seconds * _STALL_TURN_MULTIPLE)

    def describe(self) -> str:
        """Describe the budget in the terms that produced it."""
        measured = f"{self.per_turn_seconds:.2f}s/turn measured" if self.per_turn_seconds else "no turn measured yet"
        return (
            f"{self.seconds:.1f}s for {self.turns} sequential turns ({measured}, stall after {self.stall_seconds:.1f}s)"
        )


class TurnLatencyMonitor:
    """The cost of one sequential agent turn, as observed on this machine.

    Every completed wait is a measurement: it produced a known number of
    sequential replies in a known time. The slowest such observation is kept,
    because a budget derived from the fastest turn a machine ever managed is
    the same mistake as a flat constant.
    """

    def __init__(self) -> None:
        self._per_turn_seconds = 0.0

    def observe(self, *, turns: int, elapsed_seconds: float) -> None:
        """Record one wait that drove `turns` replies to completion."""
        if turns < 1 or elapsed_seconds <= 0:
            return
        self._per_turn_seconds = max(self._per_turn_seconds, elapsed_seconds / turns)

    @property
    def per_turn_seconds(self) -> float:
        """Return the slowest per-turn cost seen so far, or 0 before any."""
        return self._per_turn_seconds


@dataclass(frozen=True, slots=True)
class SlowWaitNotice:
    """One deadline extension granted because replies were still arriving."""

    turns_outstanding: int
    waited_seconds: float
    extension: int

    def render(self) -> str:
        """Describe the extension in a single operator-facing line."""
        return (
            f"slow machine: {self.turns_outstanding} replies still outstanding after "
            f"{self.waited_seconds:.1f}s but progress is ongoing; extending "
            f"(extension {self.extension} of {_MAX_BUDGET_EXTENSIONS})"
        )


class ExactReplyTimeoutError(AssertionError):
    """Some expected agent reply never arrived inside its measured budget."""

    def __init__(
        self,
        missing: Mapping[str, str],
        *,
        budget: WaitBudget,
        waited_seconds: float,
        silent_seconds: float,
        wedged: bool,
    ) -> None:
        self.missing = dict(missing)
        self.budget = budget
        self.waited_seconds = waited_seconds
        self.silent_seconds = silent_seconds
        self.wedged = wedged
        cause = (
            f"no reply arrived for {silent_seconds:.1f}s, so the runtime is wedged rather than slow"
            if wedged
            else f"replies kept arriving but {_MAX_BUDGET_EXTENSIONS} deadline extensions were exhausted"
        )
        listed = ", ".join(f"{logical_ref} ({event_id})" for event_id, logical_ref in sorted(missing.items()))
        super().__init__(
            f"timed out waiting for exact agent replies: {cause}; "
            f"waited {waited_seconds:.1f}s against a budget of {budget.describe()}; "
            f"{len(missing)} missing: {listed}",
        )


class MissingReplyStage(StrEnum):
    """How far a source event travelled before its reply stopped existing."""

    NOT_ADMITTED = "not_admitted"
    ADMITTED_NEVER_DISPATCHED = "admitted_never_dispatched"
    DISPATCHED_NEVER_SENT = "dispatched_never_sent"
    SENT_BUT_UNOBSERVED = "sent_but_unobserved"
    SETTLED_WITHOUT_REPLY = "settled_without_reply"


@dataclass(frozen=True, slots=True)
class JournalRow:
    """One principal's durable record of one inbound event."""

    principal_id: str
    kind: str
    state: str
    semantic_consumer: str | None
    receipt_order: int


@dataclass(frozen=True, slots=True)
class OutboxRow:
    """One principal's durable record of a response it owes a turn."""

    principal_id: str
    stage: str
    attempted: int
    acknowledged_event_id: str | None


@dataclass(frozen=True, slots=True)
class MissingReplyDiagnosis:
    """Where one missing reply's source event actually stopped."""

    logical_ref: str
    event_id: str
    stage: MissingReplyStage
    detail: str

    def render(self) -> str:
        """Return one indented diagnosis line."""
        return f"  {self.logical_ref} ({self.event_id}): {self.stage.value} - {self.detail}"


def classify_missing_reply(
    journal_rows: Collection[JournalRow],
    outbox_rows: Collection[OutboxRow],
) -> tuple[MissingReplyStage, str]:
    """Say where a source event stopped, from its own durable records.

    The four durable positions are distinct failures with distinct owners: the
    transport never delivered the event, the lane never picked it up, the turn
    ran but never reached Matrix, or Matrix accepted a reply the harness never
    saw. Reporting the position is the difference between a bug report and a
    re-investigation.
    """
    if not journal_rows:
        return (
            MissingReplyStage.NOT_ADMITTED,
            "no principal admitted the event: Matrix sync never delivered it, or ingress dropped it before the journal",
        )
    states = ", ".join(
        f"{row.principal_id}={row.state}"
        + (f" consumer={row.semantic_consumer}" if row.semantic_consumer else "")
        + f" receipt_order={row.receipt_order}"
        for row in sorted(journal_rows, key=lambda row: row.principal_id)
    )
    if not outbox_rows:
        if any(row.state == "pending" for row in journal_rows):
            return (
                MissingReplyStage.ADMITTED_NEVER_DISPATCHED,
                f"admitted but still pending, and no response was ever staged: {states}",
            )
        return (
            MissingReplyStage.SETTLED_WITHOUT_REPLY,
            f"the turn settled without staging any response, so the bot decided not to answer: {states}",
        )
    unacknowledged = [row for row in outbox_rows if row.acknowledged_event_id is None]
    deliveries = ", ".join(
        f"{row.principal_id}/{row.stage} attempted={row.attempted} acknowledged={row.acknowledged_event_id or 'no'}"
        for row in sorted(outbox_rows, key=lambda row: (row.principal_id, row.stage))
    )
    if unacknowledged:
        return (
            MissingReplyStage.DISPATCHED_NEVER_SENT,
            f"a response was staged but never acknowledged by Matrix: {deliveries}; journal {states}",
        )
    return (
        MissingReplyStage.SENT_BUT_UNOBSERVED,
        f"Matrix acknowledged the reply, so the harness never observed a delivered event: {deliveries}; journal {states}",
    )


@dataclass(frozen=True, slots=True)
class PendingLaneReport:
    """The depth of every room lane still holding unfinished work."""

    depths: tuple[tuple[str, int], ...]
    head_event_id: str | None
    head_receipt_order: int | None

    def render(self) -> str:
        """Summarise the backlog blocking the rooms under test."""
        if not self.depths:
            return "journal: no pending events"
        lanes = ", ".join(f"{room_id}={depth}" for room_id, depth in self.depths)
        head = (
            f"; oldest pending receipt_order={self.head_receipt_order} event_id={self.head_event_id}"
            if self.head_event_id is not None
            else ""
        )
        return f"journal: pending per room {lanes}{head}"


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
        mindroom_revision: str | None = None,
        profile: str = "fuzz",
        sync_mode: Literal["classic", "sliding"] = "classic",
        stream_segments: int = 4,
        stream_delay: float = 0.001,
        model_latch_timeout: float = 60.0,
    ) -> None:
        if profile not in {
            "fuzz",
            "chaos",
            "saturation",
            "restart-regression",
            "short-stream-correctness",
            "sustained-stream-capacity",
        }:
            msg = f"unsupported live Matrix fuzz profile {profile!r}"
            raise ValueError(msg)
        token = secrets.token_hex(4)
        self._stream_profile = stream_profile or StreamProfile(
            stream_segments=stream_segments,
            stream_delay=stream_delay,
        )
        self.profile = profile
        self.sync_mode = sync_mode
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
        self.runtime_redaction_path = state_root / "runs" / f"{self.instance_name}-runtime-redactions.jsonl"
        self.runtime_provenance: dict[str, object] | None = None
        self._runtime_generations: list[dict[str, object]] = []
        self.api_port = 0
        self.homeserver = ""
        self.server_name = ""
        self.room_keys = room_keys
        self.room_ids: dict[str, str] = {}
        self.room_id = ""
        self.agent_id = ""
        self.load_sender_id = ""
        self.router_id = ""
        self._created = False
        self._model_server: ThreadingHTTPServer | None = None
        self._model_thread: threading.Thread | None = None
        self._mindroom_process: subprocess.Popen[str] | None = None
        self._log_handle: TextIOWrapper | None = None
        self._env: dict[str, str] = {}
        self._provenance_sink = provenance_sink
        self._mindroom_revision = mindroom_revision
        self._mindroom_source_digest: str | None = None
        self._nio_source_digest: str | None = None
        self._host_lease: TextIOWrapper | None = None
        self._mindroom_start_log_offset = 0
        self._model_latch_timeout = model_latch_timeout

    def _frozen_mindroom_revision(self) -> str:
        """Freeze and return the one MindRoom revision allowed for this run."""
        if self._mindroom_revision is None:
            self._mindroom_revision = _required_mindroom_revision()
        if self._mindroom_source_digest is None:
            self._mindroom_source_digest = _mindroom_source_sha256()
            self._nio_source_digest = _nio_source_sha256(Path(nio.__file__).resolve())
        return self._mindroom_revision

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
                "docker_compose_project": self.instance_name,
                "harness_pid": os.getpid(),
                "instance_name": self.instance_name,
                "instance_cleanup_required": self._created,
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
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                msg = f"invalid abandoned live-fuzz manifest {manifest_path}: {exc.msg}"
                raise RuntimeError(msg) from exc
            if not isinstance(payload, dict) or payload.get("state") in {"closed", "recovered"}:
                continue
            instance_name = payload.get("instance_name")
            docker_compose_project = payload.get("docker_compose_project")
            project_root = payload.get("project_root")
            if (
                not isinstance(instance_name, str)
                or not instance_name.startswith("fuzz")
                or not isinstance(docker_compose_project, str)
                or docker_compose_project != instance_name
                or not isinstance(project_root, str)
            ):
                msg = f"invalid abandoned live-fuzz manifest: {manifest_path}"
                raise RuntimeError(msg)
            old_root = Path(project_root)
            self._terminate_recorded_mindroom(payload)
            cleanup_required = payload.get("instance_cleanup_required")
            if not isinstance(cleanup_required, bool):
                cleanup_required = True
            if cleanup_required:
                instances = self._registry_instances(old_root)
                if instances is not None and instance_name in instances:
                    _run_command(
                        "just",
                        "local-instances-remove",
                        instance_name,
                        cwd=old_root,
                    )
                else:
                    self._teardown_compose_project(
                        old_root,
                        docker_compose_project,
                    )
            payload["instance_cleanup_required"] = False
            payload["state"] = "recovered"
            temporary = manifest_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temporary.replace(manifest_path)

    @staticmethod
    def _registry_instances(project_root: Path) -> Mapping[str, object] | None:
        """Read one worktree's registry, returning unknown when it is unusable."""
        registry_path = project_root / "local" / "instances" / "deploy" / "instances.json"
        if not project_root.exists() or not registry_path.exists():
            return None
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(registry, dict):
            return None
        instances = registry.get("instances")
        return cast("dict[str, object]", instances) if isinstance(instances, dict) else None

    @staticmethod
    def _teardown_compose_project(project_root: Path, docker_compose_project: str) -> None:
        """Tear down only the exact project named by one durable manifest."""
        compose_root = project_root if project_root.exists() else PROJECT_ROOT
        _run_command(
            "docker",
            "compose",
            "-p",
            docker_compose_project,
            "down",
            "-v",
            cwd=compose_root / "local" / "instances" / "deploy",
        )

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
        self._frozen_mindroom_revision()
        self._acquire_host_lease()
        self._recover_abandoned_runs()
        self._write_manifest(
            state="creating",
            instance_cleanup_required=True,
        )
        # Instance creation can fail after registering or starting resources.
        # From this point onward cleanup owns the exact instance name, even
        # when the create command itself never returns successfully.
        self._created = True
        _run_command("just", "local-instances-create", self.instance_name, "tuwunel")
        registry = json.loads(INSTANCE_REGISTRY.read_text(encoding="utf-8"))
        instance = registry["instances"][self.instance_name]
        matrix_port = int(instance["matrix_port"])
        self.api_port = int(instance["mindroom_port"])
        domain = str(instance["domain"])
        self.homeserver = f"http://127.0.0.1:{matrix_port}"
        self.server_name = f"m-{domain}"
        self.agent_id = f"@mindroom_{AGENT_NAME}_{self.namespace}:{self.server_name}"
        self.load_sender_id = f"@mindroom_load_sender_{self.namespace}:{self.server_name}"
        self.router_id = f"@mindroom_router_{self.namespace}:{self.server_name}"

        _run_command("just", "local-instances-start-matrix", self.instance_name)
        self._wait_for_url(f"{self.homeserver}/_matrix/client/versions", timeout=30)
        model_port = self._start_model_server()
        self._write_config(model_port)
        self._env = self._mindroom_environment()
        self._log_handle = self.log_path.open("a", encoding="utf-8")
        self._start_mindroom()
        self._write_manifest(
            state="ready",
            matrix_port=matrix_port,
            api_port=self.api_port,
            mindroom_pid=self._mindroom_process.pid if self._mindroom_process is not None else None,
        )

    def _mindroom_environment(self) -> dict[str, str]:
        """Build the deterministic managed-child environment."""
        environment = {
            **os.environ,
            "MATRIX_HOMESERVER": self.homeserver,
            "MATRIX_SERVER_NAME": self.server_name,
            "MATRIX_SSL_VERIFY": "false",
            "MINDROOM_CONFIG_PATH": str(self.config_path),
            "MINDROOM_NAMESPACE": self.namespace,
            "MINDROOM_STORAGE_PATH": str(self.storage_path),
            "MINDROOM_LOG_FORMAT": "text",
            "MINDROOM_LOG_LEVEL": "INFO",
            "MINDROOM_LOGGER_LEVELS": "",
            "OPENAI_API_KEY": "sk-live-fuzz",
        }
        environment.pop("UV_PYTHON", None)
        return environment

    def restart_mindroom(self) -> None:
        """Restart only MindRoom, refusing to hide a shutdown that went wrong.

        `stop_mindroom` already decides whether the child stopped on its own
        signal, exited cleanly, and logged that its bots came down in order.
        Throwing that verdict away made a twenty-second hung drain followed by
        a SIGKILL indistinguishable from a clean restart, so the run continued
        and reported PASS on a stack that had just been shot in the head.
        """
        if not self.stop_mindroom():
            msg = (
                "MindRoom did not shut down cleanly before its restart: it either ignored SIGINT until the "
                "harness had to kill it, exited with an unexpected status, or never logged an orderly bot "
                f"shutdown ({ORDERLY_SHUTDOWN_MARKER!r})"
            )
            raise AssertionError(msg)
        self._start_mindroom()

    def wait_for_blocked_restart_request(self, *, timeout: float) -> bool:
        """Wait until the pre-restart generation has an exact fresh request in flight."""
        return _ModelHandler.blocked_request_started.wait(timeout=timeout)

    def crash_mindroom(self, *, timeout: float = 20) -> None:
        """Kill MindRoom outright and boot a replacement over the same state.

        No signal the runtime can answer, so no drain, no orderly shutdown, and
        no chance to finish the turn that was running. Everything the journal
        had committed and not settled is now owed to durable recovery, which is
        the guarantee this whole subsystem exists to make and the one a
        graceful restart never puts under any pressure.
        """
        self._hard_kill(timeout=timeout)
        self._start_mindroom()

    def _hard_kill(self, *, timeout: float) -> None:
        """Stop the managed child the way a crash would, with nothing drained."""
        process = self._mindroom_process
        if process is None:
            msg = "MindRoom is not running"
            raise RuntimeError(msg)
        if process.poll() is not None:
            msg = "MindRoom exited before the hard-restart boundary"
            raise RuntimeError(msg)
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=timeout)
        self._mindroom_process = None

    def restart_mindroom_for_recovery(self, *, timeout: float) -> None:
        """Hard-stop an in-flight turn and boot a distinguishable recovery generation."""
        deadline = time.monotonic() + timeout
        self._hard_kill(timeout=timeout)
        self._set_model_id(RECOVERED_MODEL_ID)
        _ModelHandler.blocked_request_release.set()
        self._start_mindroom(timeout=max(0, deadline - time.monotonic()))

    def log_count(self, *markers: str) -> int:
        """Count lines containing every content-free lifecycle marker."""
        return _log_count(self.read_log(), *markers)

    def read_log(self) -> str:
        """Read the complete MindRoom log once."""
        return self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""

    def restart_shutdown_failure_count(self) -> int:
        """Count the production marker proving durable-recovery drain failure."""
        return _log_count(self.read_log(), RESTART_SHUTDOWN_FAILURE_MARKER)

    def wait_for_log_count(self, markers: tuple[str, ...], minimum: int, timeout: float = 60) -> bool:
        """Wait for a bounded lifecycle milestone."""
        return _wait_until(lambda: self.log_count(*markers) >= minimum, timeout=timeout)

    def apply_replacement_config(self, room_id: str) -> None:
        """Replace both bots, add the dormant room and arm only the agent's latch."""
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        config["agents"][AGENT_NAME]["rooms"].append(room_id)
        config["models"]["default"]["id"] = RESTART_MODEL_ID
        # Room/model changes alone now apply in place; change a construction input.
        prompts = config.setdefault("prompts", {})
        prompts["AGENT_IDENTITY_CONTEXT_TEMPLATE"] = (
            prompts.get("AGENT_IDENTITY_CONTEXT_TEMPLATE", AGENT_IDENTITY_CONTEXT_TEMPLATE)
            + "\nThis is the replacement generation for the restart regression."
        )
        self._replace_config(config)

    def _set_model_id(self, model_id: str) -> None:
        """Atomically select the deterministic model used by the next runtime."""
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        config["models"]["default"]["id"] = model_id
        self._replace_config(config)

    def _replace_config(self, config: dict[str, Any]) -> None:
        """Atomically replace the managed configuration."""
        staged_path = self.config_path.with_suffix(".yaml.tmp")
        staged_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        staged_path.replace(self.config_path)

    def projected_restart_event_pair_count(self, room_id: str, event_ids: tuple[str, str]) -> int:
        """Count exact principal/event pairs the journal projected for the restart room."""
        rows = self._journal_query(
            """
            SELECT COUNT(*) FROM visible_messages
            WHERE principal_id IN (?, ?) AND room_id = ? AND logical_event_id IN (?, ?)
            """,
            (
                self._journal_principal_id(self.agent_id),
                self._journal_principal_id(self.router_id, agent_name=ROUTER_NAME),
                room_id,
                *event_ids,
            ),
        )
        return cast("int", rows[0][0]) if rows else 0

    def agent_matrix_credentials(self, agent_name: str = AGENT_NAME) -> tuple[str, str] | None:
        """Return one managed agent's persisted access token and device ID."""
        state_path = self.storage_path / "matrix_state.yaml"
        if not state_path.is_file():
            return None
        state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
        accounts = state.get("accounts", {}) if isinstance(state, dict) else {}
        account = accounts.get(f"agent_{agent_name}") if isinstance(accounts, dict) else None
        if not isinstance(account, dict):
            return None
        access_token, device_id = account.get("access_token"), account.get("device_id")
        if not isinstance(access_token, str) or not isinstance(device_id, str):
            return None
        return access_token, device_id

    def managed_room_baseline_ready(self) -> bool:
        """Require the agent's durable joined baseline in every configured room."""
        if not all(room_key in self.room_ids for room_key in self.room_keys):
            return False
        required_rooms = {self.room_ids[room_key] for room_key in self.room_keys}
        ready_rooms: set[str] = set()
        for path in (self.storage_path / "encryption_keys").glob("*/*.db"):
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as database:
                if not database.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='NioDurableRoom'",
                ).fetchone():
                    continue
                rows = database.execute("SELECT room_id, metadata FROM NioDurableRoom").fetchall()
            for room_id, raw_metadata in rows:
                if room_id not in required_rooms:
                    continue
                metadata = json.loads(raw_metadata)
                if (
                    metadata.get("own_user_id") == self.agent_id
                    and metadata.get("baseline") is True
                    and metadata.get("membership") == "join"
                ):
                    ready_rooms.add(room_id)
        return bool(required_rooms) and ready_rooms >= required_rooms

    def managed_stream_health_sample(self) -> ManagedStreamHealthSample:
        """Read and parse one managed-stream API health sample."""
        response = httpx.get(f"http://127.0.0.1:{self.api_port}/api/health", timeout=2)
        payload = response.json()
        if not isinstance(payload, dict):
            msg = "MindRoom health endpoint returned non-object JSON"
            raise TypeError(msg)
        raw_last_sync_time = payload.get("last_sync_time")
        if raw_last_sync_time is None:
            last_sync_time = None
        elif isinstance(raw_last_sync_time, str):
            last_sync_time = datetime.fromisoformat(raw_last_sync_time)
            if last_sync_time.tzinfo is None:
                msg = "MindRoom health last_sync_time must include a timezone"
                raise ValueError(msg)
        else:
            msg = "MindRoom health last_sync_time must be an ISO datetime or null"
            raise TypeError(msg)
        return ManagedStreamHealthSample(
            healthy=response.is_success and payload.get("status") == "healthy",
            last_sync_time=last_sync_time,
        )

    def managed_stream_drain_counts(self) -> ManagedStreamDrainCounts:
        """Count only actionable pending journal and unacknowledged outbox rows."""
        database_path = self.storage_path / "tracking" / "event_journal.db"
        if not database_path.is_file():
            msg = f"managed-stream event journal database is missing: {database_path}"
            raise FileNotFoundError(msg)
        with closing(sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)) as database:
            pending_journal_rows = cast(
                "int",
                database.execute(
                    "SELECT COUNT(*) FROM journal_events WHERE state = 'pending'",
                ).fetchone()[0],
            )
            unacknowledged_outbox_rows = cast(
                "int",
                database.execute(
                    "SELECT COUNT(*) FROM matrix_delivery_outbox WHERE acknowledged_event_id IS NULL",
                ).fetchone()[0],
            )
        return ManagedStreamDrainCounts(
            pending_journal_rows=pending_journal_rows,
            unacknowledged_outbox_rows=unacknowledged_outbox_rows,
        )

    def recovery_outbox_debt(self, source_event_ids: Collection[str]) -> int:
        """Count exact workload FINAL rows observed after attempt but before acknowledgement."""
        database_path = self.storage_path / "tracking" / "event_journal.db"
        if not database_path.is_file():
            msg = f"recovery-cliff event journal database is missing: {database_path}"
            raise FileNotFoundError(msg)
        source_ids = tuple(sorted(set(source_event_ids)))
        if not source_ids:
            return 0
        placeholders = ", ".join("?" for _source_id in source_ids)
        with closing(sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)) as database:
            row = database.execute(
                f"""
                SELECT COUNT(*) FROM matrix_delivery_outbox
                WHERE principal_id = ? AND stage = 'final'
                  AND attempted = 1 AND acknowledged_event_id IS NULL
                  AND delivery_id IN ({placeholders})
                """,  # noqa: S608 - placeholders are generated, values remain bound
                (self._journal_principal_id(self.agent_id), *source_ids),
            ).fetchone()
        return int(cast("int", row[0]))

    def managed_stream_reaction_state(self, event_id: str) -> str | None:
        """Return the responder's exact durable state for one reaction fence."""
        rows = self._journal_query(
            """
            SELECT state FROM journal_events
            WHERE principal_id = ? AND event_id = ? AND kind = 'reaction'
            """,
            (self._journal_principal_id(self.agent_id), event_id),
        )
        return str(rows[0][0]) if rows else None

    def _restart_event_projected_for_agent(self, room_id: str, event_id: str) -> bool:
        """Return whether the managed agent durably projected one exact event."""
        rows = self._journal_query(
            "SELECT 1 FROM visible_messages WHERE principal_id = ? AND room_id = ? AND logical_event_id = ?",
            (self._journal_principal_id(self.agent_id), room_id, event_id),
        )
        return bool(rows)

    @staticmethod
    def _journal_principal_id(matrix_id: str, *, agent_name: str = AGENT_NAME) -> str:
        """Return the journal's composite principal identity for one managed bot."""
        return f"{agent_name}@{matrix_id}"

    def _restart_source_settled(self) -> bool:
        """Read committed producer progress without interpreting opaque sync tokens."""
        found = False
        for path in (self.storage_path / "encryption_keys").glob("*/*.db"):
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as database:
                if not database.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='NioDurableMeta'",
                ).fetchone():
                    continue
                row = database.execute(
                    """
                    SELECT cursor IS NOT NULL
                        AND NOT EXISTS (SELECT 1 FROM NioDurableInput)
                        AND NOT EXISTS (SELECT 1 FROM NioDurableBatch)
                    FROM NioDurableMeta WHERE user_id=?
                    """,
                    (self.agent_id,),
                ).fetchone()
            if row is not None:
                if not row[0]:
                    return False
                found = True
        return found

    def wait_for_restart_event_checkpoint(self, room_id: str, event_id: str, *, timeout: float) -> bool:
        """Wait for exact event projection and producer settlement before hard restart."""
        deadline = time.monotonic() + timeout
        event_projected = _wait_until(
            lambda: self._restart_event_projected_for_agent(room_id, event_id),
            timeout=max(deadline - time.monotonic(), 0),
        )
        if not event_projected:
            return False
        return _wait_until(
            self._restart_source_settled,
            timeout=max(deadline - time.monotonic(), 0),
        )

    def restart_journal_event_state(self, event_id: str) -> str | None:
        """Return the durable state of one agent message without creating storage.

        Settled or pending, which is the whole of what the journal records.
        An event whose turn is still running is simply pending: there is no
        separate `deferred` state, since a process that dies mid-turn must
        leave the event eligible for retry either way.
        """
        database_path = self.storage_path / "tracking" / "event_journal.db"
        if not database_path.exists():
            return None
        with closing(sqlite3.connect(database_path)) as database:
            row = database.execute(
                """
                SELECT state
                FROM journal_events
                WHERE principal_id = ? AND event_id = ? AND kind = 'message'
                """,
                (f"{AGENT_NAME}@{self.agent_id}", event_id),
            ).fetchone()
        if row is None:
            return None
        return str(row[0])

    def _journal_query(self, query: str, parameters: tuple[object, ...]) -> list[tuple[object, ...]]:
        """Read the durable journal without creating it when it is absent."""
        database_path = self.storage_path / "tracking" / "event_journal.db"
        if not database_path.is_file():
            return []
        with closing(sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)) as database:
            return cast("list[tuple[object, ...]]", database.execute(query, parameters).fetchall())

    def journal_rows(self, event_id: str) -> tuple[JournalRow, ...]:
        """Return every principal's durable record of one inbound event."""
        return tuple(
            JournalRow(
                principal_id=str(row[0]),
                kind=str(row[1]),
                state=str(row[2]),
                semantic_consumer=None if row[3] is None else str(row[3]),
                receipt_order=int(cast("int", row[4])),
            )
            for row in self._journal_query(
                """
                SELECT principal_id, kind, state, semantic_consumer, receipt_order
                FROM journal_events
                WHERE event_id = ?
                """,
                (event_id,),
            )
        )

    def outbox_rows(self, turn_id: str) -> tuple[OutboxRow, ...]:
        """Return the responses staged for one turn.

        A turn is identified by the event that started it, so the source event
        ID is the outbox key as well as the journal key.
        """
        return tuple(
            OutboxRow(
                principal_id=str(row[0]),
                stage=str(row[1]),
                attempted=int(cast("int", row[2])),
                acknowledged_event_id=None if row[3] is None else str(row[3]),
            )
            for row in self._journal_query(
                "SELECT principal_id, stage, attempted, acknowledged_event_id "
                "FROM matrix_delivery_outbox WHERE delivery_id = ?",
                (turn_id,),
            )
        )

    def pending_lane_report(self) -> PendingLaneReport:
        """Report the per-room backlog and the event at the head of it."""
        depths = tuple(
            (str(row[0]), int(cast("int", row[1])))
            for row in self._journal_query(
                "SELECT room_id, COUNT(*) FROM journal_events WHERE state = 'pending' GROUP BY room_id ORDER BY room_id",
                (),
            )
        )
        head = self._journal_query(
            "SELECT event_id, receipt_order FROM journal_events WHERE state = 'pending' ORDER BY receipt_order LIMIT 1",
            (),
        )
        return PendingLaneReport(
            depths=depths,
            head_event_id=str(head[0][0]) if head else None,
            head_receipt_order=int(cast("int", head[0][1])) if head else None,
        )

    def pending_journal_event_count(self) -> int:
        """Count the events the journal owns and has not finished."""
        return sum(depth for _room_id, depth in self.pending_lane_report().depths)

    def wait_for_pending_journal_work(self, *, timeout: float) -> bool:
        """Wait until the journal holds committed work that is not settled yet.

        This is the precondition for a restart that means something. An event
        that is durably admitted and still pending is exactly the state the
        journal exists to survive, so a crash taken here tests recovery; a
        crash taken with an empty journal tests only that MindRoom can boot.
        """
        return _wait_until(lambda: self.pending_journal_event_count() > 0, timeout=timeout)

    def diagnose_missing_replies(self, missing: Mapping[str, str]) -> str:
        """Explain, per missing reply, how far its source event actually got."""
        diagnoses = tuple(
            MissingReplyDiagnosis(
                logical_ref=logical_ref,
                event_id=event_id,
                stage=stage,
                detail=detail,
            )
            for event_id, logical_ref in sorted(missing.items())
            for stage, detail in (classify_missing_reply(self.journal_rows(event_id), self.outbox_rows(event_id)),)
        )
        lines = [self.pending_lane_report().render(), *(diagnosis.render() for diagnosis in diagnoses)]
        return "\n".join(lines)

    def require_runtime_alive(self) -> None:
        """Fail immediately when the managed MindRoom process has exited."""
        process = self._mindroom_process
        if process is not None and process.poll() is not None:
            msg = f"MindRoom exited with code {process.returncode} while the harness was waiting for replies"
            raise AssertionError(msg)

    def wait_for_restart_journal_event_state(
        self,
        event_id: str,
        *,
        expected: str | frozenset[str],
        timeout: float,
    ) -> bool:
        """Wait until the exact fresh callback reaches one accepted durable state."""
        expected_states = frozenset({expected}) if isinstance(expected, str) else expected
        return _wait_until(
            lambda: self.restart_journal_event_state(event_id) in expected_states,
            timeout=timeout,
        )

    def kill_restart_mindroom(self) -> None:
        """Hard-kill MindRoom without a drain, then restart it."""
        self._stop_mindroom(kill=True)
        self._start_mindroom()

    def cold_restart_mindroom(self) -> None:
        """Restart MindRoom with cleared sync checkpoints, forcing a full resync."""
        self._stop_mindroom()
        _reset_durable_sync_cursors(self.storage_path)
        self._start_mindroom()

    def stop_mindroom(self, *, timeout: float = MINDROOM_SHUTDOWN_TIMEOUT_SECONDS) -> bool:
        """Stop MindRoom and report whether its shutdown stayed bounded and clean."""
        try:
            self._stop_mindroom(timeout=timeout)
        except (RuntimeError, TimeoutError):
            return False
        return True

    def start_mindroom(self) -> None:
        """Start MindRoom again after an explicit stop."""
        self._start_mindroom()

    def restart_tuwunel(self) -> None:
        """Restart the homeserver container, forcing every sync loop to reconnect."""
        _run_command("docker", "restart", f"{self.instance_name}-tuwunel")
        self._wait_for_url(f"{self.homeserver}/_matrix/client/versions", timeout=60)

    def wait_for_startup_maintenance(self, *, timeout_seconds: float) -> None:
        """Wait for every phase of the current MindRoom generation to complete."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            process = self._mindroom_process
            if process is None or process.poll() is not None:
                msg = f"MindRoom exited before startup maintenance completed:\n{self.log_tail()}"
                raise RuntimeError(msg)
            statuses = self._startup_maintenance_statuses()
            failed = {phase: status for phase, status in statuses.items() if status != "completed"}
            if failed:
                msg = f"MindRoom startup maintenance did not complete cleanly: {failed}"
                raise AssertionError(msg)
            if statuses.keys() >= _STARTUP_MAINTENANCE_PHASES:
                return
            time.sleep(0.1)
        missing = sorted(_STARTUP_MAINTENANCE_PHASES - self._startup_maintenance_statuses().keys())
        msg = f"timed out waiting for MindRoom startup maintenance phases: {missing}"
        raise TimeoutError(msg)

    def _startup_maintenance_statuses(self) -> dict[str, str]:
        """Read terminal startup phases emitted after the current process start."""
        if not self.log_path.exists():
            return {}
        with self.log_path.open("rb") as log:
            log.seek(self._mindroom_start_log_offset)
            generation_log = log.read().decode("utf-8", errors="replace")
        generation_log = _ANSI_ESCAPE_PATTERN.sub("", generation_log)
        statuses: dict[str, str] = {}
        for line in generation_log.splitlines():
            if "startup_phase_finished" not in line:
                continue
            phase_match = _STARTUP_PHASE_PATTERN.search(line)
            status_match = _STARTUP_STATUS_PATTERN.search(line)
            if phase_match is not None and status_match is not None:
                statuses[phase_match.group(1)] = status_match.group(1)
        return statuses

    def close(
        self,
        *,
        before_destructive_cleanup: Callable[[], None] | None = None,
    ) -> None:
        """Attempt every teardown stage and report all cleanup failures."""
        _ModelHandler.blocked_request_release.set()
        errors: list[tuple[str, BaseException]] = []

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
            if _attempt_cleanup(
                errors,
                "join model server thread",
                lambda: _join_model_server_thread(thread),
            ):
                self._model_thread = None
        if before_destructive_cleanup is not None:
            _attempt_cleanup(errors, "snapshot runtime evidence", before_destructive_cleanup)
        if self._created and _attempt_cleanup(
            errors,
            "remove Tuwunel instance",
            lambda: _run_command("just", "local-instances-remove", self.instance_name),
        ):
            self._created = False
        _attempt_cleanup(errors, "remove temporary stack storage", self.temp_dir.cleanup)
        _attempt_cleanup(
            errors,
            "remove runtime redaction observations",
            lambda: self.runtime_redaction_path.unlink(missing_ok=True),
        )
        cleanup_state = "cleanup_failed" if errors else "closed"
        _attempt_cleanup(
            errors,
            "write cleanup manifest",
            lambda: self._write_manifest(
                state=cleanup_state,
                cleanup_errors=[_format_cleanup_failure(label, error) for label, error in errors],
            ),
        )
        _attempt_cleanup(errors, "release host lease", self._release_host_lease)
        _raise_cleanup_failures(errors, message="live Matrix fuzz cleanup failed")

    def log_tail(self, lines: int = 80) -> str:
        """Return recent MindRoom output when a live invariant fails."""
        if not self.log_path.exists():
            return ""
        return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])

    def diagnostic_counts(self) -> dict[str, int]:
        """Count every live diagnostic marker in the complete runtime output."""
        log = self.read_log()
        if not log:
            return {}
        return {name: _log_count(log, marker) for name, marker in DIAGNOSTIC_MARKERS.items()}

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
        self.runtime_redaction_path.parent.mkdir(parents=True, exist_ok=True)
        self.runtime_redaction_path.touch(exist_ok=False)
        _ModelHandler.reset_observations()
        _ModelHandler.stream_segments = profile.stream_segments
        _ModelHandler.stream_delay = profile.stream_delay
        _ModelHandler.slow_call_modulus = profile.slow_call_modulus
        _ModelHandler.slow_stream_segments = profile.slow_stream_segments
        _ModelHandler.slow_stream_delay = profile.slow_stream_delay
        _ModelHandler.first_token_delay = profile.first_token_delay
        _ModelHandler.blocked_request_timeout = self._model_latch_timeout
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
        config: dict[str, Any] = {
            "matrix_sync": {"mode": self.sync_mode},
            "models": {
                "default": {
                    "provider": "openai",
                    "id": MODEL_ID,
                    "extra_kwargs": {"base_url": f"http://127.0.0.1:{model_port}/v1"},
                },
                "router": {
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
            "router": {"model": "router"},
            "mindroom_user": {"username": "livefuzzowner", "display_name": "Live Fuzz Owner"},
            "room_defaults": {"join_policy": "public"},
        }
        if self.profile == "sustained-stream-capacity":
            models = cast("dict[str, Any]", config["models"])
            models["synthetic"] = {
                "provider": "synthetic",
                "id": "lorem-ipsum",
                "extra_kwargs": {
                    "seed": 1,
                    "min_response_chars": 4800,
                    "max_response_chars": 4800,
                    "chunk_chars": 40,
                    "chars_per_second": 80,
                    "tool_call_probability": 0.2,
                },
            }
            config["agents"][AGENT_NAME]["model"] = "synthetic"
            config["agents"][AGENT_NAME]["tools"] = ["shell"]
            config["agents"][AGENT_NAME]["worker_tools"] = []
            config["agents"]["load_sender"] = {
                "display_name": "Live Fuzz Load Sender",
                "role": "Author deterministic managed-load workload roots.",
                "model": "synthetic",
                "tools": [],
                "rooms": [ROOM_KEY],
                "learning": False,
            }
        self.config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    def _start_mindroom(self, *, timeout: float = 60) -> None:
        assert self._log_handle is not None
        deadline = time.monotonic() + timeout
        self.attestation_path.unlink(missing_ok=True)
        self._mindroom_start_log_offset = self.log_path.stat().st_size if self.log_path.exists() else 0
        command = [
            "uv",
            "run",
            "--locked",
            "--python",
            "3.13",
            "python",
            str(Path(__file__).resolve()),
            "__mindroom_runtime_child__",
            str(self.attestation_path),
            str(self.runtime_redaction_path),
        ]
        command.extend(("run", "--api-port", str(self.api_port), "--log-level", "INFO"))
        self._mindroom_process = subprocess.Popen(
            command,
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
        self._wait_for_url(
            f"http://127.0.0.1:{self.api_port}/api/health",
            timeout=max(0, deadline - time.monotonic()),
        )
        state_path = self.storage_path / "matrix_state.yaml"
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
        expected_mindroom_revision = self._frozen_mindroom_revision()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.attestation_path.exists():
                generation = {
                    **_validated_child_provenance(
                        self.attestation_path,
                        expected_mindroom_revision=expected_mindroom_revision,
                    ),
                    **self._tuwunel_provenance(),
                    "runtime_generation": len(self._runtime_generations) + 1,
                }
                if generation["nio_module_sha256"] != self._nio_source_digest:
                    msg = "Nio source contents changed during the live run"
                    raise RuntimeError(msg)
                if generation["mindroom_source_sha256"] != self._mindroom_source_digest:
                    msg = "MindRoom source contents changed during the live run"
                    raise RuntimeError(msg)
                self._runtime_generations.append(generation)
                self.runtime_provenance = {
                    **generation,
                    "mindroom_frozen_revision": expected_mindroom_revision,
                    "runtime_generations": [dict(item) for item in self._runtime_generations],
                }
                if self._provenance_sink is not None:
                    self._provenance_sink(self.runtime_provenance)
                return
            if self._mindroom_process is not None and self._mindroom_process.poll() is not None:
                msg = f"MindRoom exited before runtime attestation:\n{self.log_tail()}"
                raise RuntimeError(msg)
            time.sleep(0.05)
        msg = "MindRoom child did not attest loaded runtime paths"
        raise TimeoutError(msg)

    def revalidate_runtime_provenance(self) -> Mapping[str, object]:
        """Recheck source contents at a destructive or PASS boundary."""
        provenance = self.runtime_provenance
        if provenance is None:
            msg = "passing live run omitted child runtime provenance"
            raise RuntimeError(msg)
        validated = _validated_import_provenance(
            provenance,
            expected_mindroom_revision=self._frozen_mindroom_revision(),
        )
        if validated["nio_module_sha256"] != self._nio_source_digest:
            msg = "Nio source contents changed before final validation"
            raise RuntimeError(msg)
        if validated["mindroom_source_sha256"] != self._mindroom_source_digest:
            msg = "MindRoom source contents changed before final validation"
            raise RuntimeError(msg)
        final_source_validation = {
            key: validated[key]
            for key in (
                "mindroom_dirty",
                "mindroom_expected_revision",
                "mindroom_revision",
                "nio_version",
                "nio_expected_version",
                "nio_module_sha256",
                "mindroom_source_sha256",
            )
        }
        self.runtime_provenance = {
            **provenance,
            **final_source_validation,
            "final_source_validation": final_source_validation,
        }
        if self._provenance_sink is not None:
            self._provenance_sink(self.runtime_provenance)
        return self.runtime_provenance

    def _tuwunel_provenance(self) -> dict[str, str]:
        """Return exact live homeserver and immutable container-image identity."""
        container_name = f"{self.instance_name}-tuwunel"
        raw = json.loads(
            _run_command(
                "docker",
                "container",
                "inspect",
                container_name,
            ),
        )
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
            msg = f"invalid Docker inspection for {container_name}"
            raise RuntimeError(msg)
        container = raw[0]
        config = container.get("Config")
        image_id = container.get("Image")
        image_reference = config.get("Image") if isinstance(config, dict) else None
        if not isinstance(image_id, str) or not isinstance(image_reference, str):
            msg = f"Docker inspection omitted Tuwunel image identity for {container_name}"
            raise TypeError(msg)
        return {
            "matrix_homeserver": self.homeserver,
            "matrix_server_implementation": "tuwunel",
            "matrix_server_name": self.server_name,
            "tuwunel_container": container_name,
            "tuwunel_image_id": image_id,
            "tuwunel_image_reference": image_reference,
        }

    def _stop_mindroom(
        self,
        *,
        kill: bool = False,
        timeout: float = MINDROOM_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        process = self._mindroom_process
        if process is None:
            return
        return_code = process.poll()
        if return_code is not None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            self._mindroom_process = None
            msg = f"MindRoom exited before managed shutdown with status {return_code}"
            raise RuntimeError(msg)
        if kill:
            try:
                _hard_kill_mindroom_process(process)
            finally:
                self._mindroom_process = None
            return
        shutdown_marker_count = self.log_count(ORDERLY_SHUTDOWN_MARKER)
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError as exc:
            try:
                return_code = process.wait(timeout=10)
            finally:
                self._mindroom_process = None
            msg = f"MindRoom exited before managed SIGINT delivery with status {return_code}"
            raise RuntimeError(msg) from exc
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
            finally:
                self._mindroom_process = None
            msg = "MindRoom ignored SIGINT and required SIGKILL"
            raise TimeoutError(msg) from exc
        try:
            required_group_kill = _cleanup_surviving_process_group(process.pid)
            failure_message = _graceful_shutdown_failure(
                return_code=return_code,
                required_group_kill=required_group_kill,
                child_shutdown_completed=self.log_count(ORDERLY_SHUTDOWN_MARKER) > shutdown_marker_count,
            )
            if failure_message is not None:
                raise RuntimeError(failure_message)
        finally:
            self._mindroom_process = None

    def assert_mindroom_running(self) -> None:
        """Require the managed runtime to remain alive through final audit."""
        process = self._mindroom_process
        if process is None:
            msg = "MindRoom is not running before final audit"
            raise RuntimeError(msg)
        return_code = process.poll()
        if return_code is not None:
            msg = f"MindRoom exited before final audit with status {return_code}:\n{self.log_tail()}"
            raise RuntimeError(msg)

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

    async def create_public_room(self) -> None:
        """Create and select one disposable world-readable public room."""
        data = await self._request(
            "POST",
            "/_matrix/client/v3/createRoom",
            json_body={
                "preset": "public_chat",
                "visibility": "public",
                "initial_state": [
                    {
                        "type": "m.room.history_visibility",
                        "state_key": "",
                        "content": {"history_visibility": "world_readable"},
                    },
                ],
            },
        )
        room_id = data.get("room_id")
        if not isinstance(room_id, str):
            msg = "Matrix createRoom omitted room_id"
            raise TypeError(msg)
        self.room_id = room_id
        self.room_ids = (room_id,)

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
            json_body={"reason": "live journal fuzz"},
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
    ) -> int:
        """Advance this client's private sync cursor and retain room events.

        Strict consumers repair a limited timeline from canonical room history
        before advancing the cursor.  Callers that explicitly allow limited
        timelines accept only the returned suffix because they do not use this
        client as an exact audit source.
        """
        data = await self.sync(self.next_batch, timeout_ms=timeout_ms)
        next_batch = data.get("next_batch")
        if not isinstance(next_batch, str):
            msg = "Matrix sync omitted next_batch"
            raise TypeError(msg)
        new_event_count = 0
        joined = data.get("rooms", {}).get("join", {})
        for room_id in self.room_ids:
            room = joined.get(room_id, {}) if isinstance(joined, dict) else {}
            timeline = room.get("timeline", {}) if isinstance(room, dict) else {}
            events = timeline.get("events", [])
            if not isinstance(events, list):
                msg = "Matrix sync room timeline events must be a list"
                raise TypeError(msg)
            if timeline.get("limited") is True and not allow_limited:
                # A canonical backfill closes the missing prefix, while the
                # live sync suffix can contain events newer than the history
                # snapshot. Retain both before advancing past next_batch. The
                # later backfill wins duplicate event IDs because its derived
                # relation bundles were queried after the sync snapshot.
                events = [*events, *await self.paginate_room(room_id)]
            for raw_event in events:
                if not isinstance(raw_event, dict):
                    continue
                event = cast("dict[str, Any]", raw_event)
                event_id = event.get("event_id")
                if isinstance(event_id, str):
                    new_event_count += event_id not in self.seen_events
                    self.seen_events[event_id] = {**event, "_audit_room_id": room_id}
        self.next_batch = next_batch
        return new_event_count

    async def wait_until_quiet(
        self,
        *,
        deadline_seconds: float,
        quiet_seconds: float,
    ) -> None:
        """Require strict incremental syncs to observe one exact quiet window."""
        if deadline_seconds <= 0 or quiet_seconds < 0:
            msg = "Matrix quiet-window deadline must be positive and duration non-negative"
            raise ValueError(msg)
        deadline = time.monotonic() + deadline_seconds
        quiet_since = time.monotonic()
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            timeout_ms = max(1, min(250, int(remaining * 1000)))
            new_event_count = await self.sync_incremental(
                timeout_ms=timeout_ms,
                allow_limited=False,
            )
            now = time.monotonic()
            if new_event_count:
                quiet_since = now
            if now - quiet_since >= quiet_seconds:
                return
        msg = f"Matrix room did not stay quiet for {quiet_seconds:.3f}s"
        raise TimeoutError(msg)

    @staticmethod
    def _raw_event_map(raw_events: object, *, source: str) -> dict[str, dict[str, Any]]:
        """Validate and index one raw Matrix timeline or messages chunk."""
        if not isinstance(raw_events, list):
            msg = f"Matrix {source} events must be a list"
            raise TypeError(msg)
        events: dict[str, dict[str, Any]] = {}
        for raw_event in raw_events:
            if not isinstance(raw_event, dict):
                msg = f"Matrix {source} included a non-object event"
                raise TypeError(msg)
            event = cast("dict[str, Any]", raw_event)
            event_id = event.get("event_id")
            if not isinstance(event_id, str):
                msg = f"Matrix {source} event omitted event_id"
                raise TypeError(msg)
            events[event_id] = event
        return events

    async def _enumerate_sync_interval(
        self,
        *,
        from_token: str,
        to_token: str,
    ) -> dict[str, dict[str, Any]]:
        """Enumerate one positioned sync interval through raw room history."""
        room_id = quote(self.room_id, safe="")
        path = f"/_matrix/client/v3/rooms/{room_id}/messages"
        cursor = from_token
        visited_cursors = {cursor}
        recovered: dict[str, dict[str, Any]] = {}
        while True:
            page = await self._request(
                "GET",
                path,
                params={
                    "dir": "f",
                    "from": cursor,
                    "to": to_token,
                    "limit": _MANAGED_STREAM_OBSERVER_PAGE_SIZE,
                },
            )
            if page.get("start") != cursor:
                msg = "managed-stream observer history page returned an unexpected start cursor"
                raise AssertionError(msg)
            page_events = self._raw_event_map(page.get("chunk"), source="room messages")
            recovered.update(page_events)
            next_cursor = page.get("end")
            if next_cursor is None:
                if page_events:
                    msg = "managed-stream observer history ended before proving interval exhaustion"
                    raise AssertionError(msg)
                return recovered
            if not isinstance(next_cursor, str) or not next_cursor:
                msg = "managed-stream observer history page returned an invalid end cursor"
                raise AssertionError(msg)
            if next_cursor == cursor:
                msg = "managed-stream observer history cursor did not advance"
                raise AssertionError(msg)
            if next_cursor in visited_cursors:
                msg = "managed-stream observer history cursor cycled"
                raise AssertionError(msg)
            visited_cursors.add(next_cursor)
            cursor = next_cursor

    async def sync_incremental_complete(self, *, timeout_ms: int) -> None:
        """Advance one cursor only after enumerating its complete raw interval."""
        from_token = self.next_batch
        data = await self.sync(from_token, timeout_ms=timeout_ms)
        next_batch = data.get("next_batch")
        if not isinstance(next_batch, str):
            msg = "Matrix sync omitted next_batch"
            raise TypeError(msg)
        if from_token is None:
            joined = data.get("rooms", {}).get("join", {})
            room = joined.get(self.room_id, {}) if isinstance(joined, dict) else {}
            timeline = room.get("timeline", {}) if isinstance(room, dict) else {}
            recovered = self._raw_event_map(timeline.get("events", []), source="sync timeline")
        elif from_token == next_batch:
            recovered = {}
        else:
            recovered = await self._enumerate_sync_interval(
                from_token=from_token,
                to_token=next_batch,
            )
        self.seen_events.update(recovered)
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


def read_ledger_records(
    ledger_path: Path,
    *,
    strict: bool = False,
    include_incomplete: bool = False,
) -> dict[str, TurnRecord]:
    """Read every terminal handled-turn record keyed by its source event.

    A completed record with a visible ``response_event_id`` proves that source
    was answered. Completed no-response records retain their own terminal
    meaning; deliberate replay supersession requires separate journal proof.
    Missing, malformed, or non-terminal records
    are omitted during live polling, so the oracle can wait for a terminal
    outcome rather than inferring supersession from chronology alone. Final
    audits use strict mode and reject every unreadable, malformed, or
    non-terminal entry instead of letting corruption look like an empty ledger.
    A fully redacted record is a durable tombstone even when ``completed``
    remains false. Session cleanup may remain pending until the next response;
    explicit cleanup probes audit that separate obligation.
    Live tombstone observation may include unfinished owners; strict final
    audits still reject their unfinished live sources.
    """
    raw_records = _load_ledger_rows(ledger_path, strict=strict)
    if raw_records is None:
        return {}
    return _decode_ledger_rows(ledger_path, raw_records, strict=strict, include_incomplete=include_incomplete)


def _invalid_ledger(ledger_path: Path, reason: str, *, strict: bool) -> None:
    """Raise for a final audit, or let live polling retry a transient file."""
    if strict:
        msg = f"handled-turn ledger invalid at {ledger_path}: {reason}"
        raise AssertionError(msg)


def _load_ledger_rows(
    ledger_path: Path,
    *,
    strict: bool,
    database: sqlite3.Connection | None = None,
) -> dict[str, object] | None:
    """Read this agent's durable turn projections from the current event journal."""
    if not ledger_path.exists():
        _invalid_ledger(ledger_path, "file is missing", strict=strict)
        return None
    try:
        if database is None:
            with closing(sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True)) as connection:
                return _load_ledger_rows(ledger_path, strict=strict, database=connection)
        rows = database.execute(
            "SELECT index_event_id, anchor_event_id, record_json FROM turn_records WHERE agent_name = ?",
            (AGENT_NAME,),
        ).fetchall()
        records: dict[str, object] = {}
        for index_event_id, anchor_event_id, record_json in rows:
            raw = json.loads(record_json)
            record = TurnRecordCodec._from_ledger_record(index_event_id, raw)
            if (
                record is None
                or record.anchor_event_id != anchor_event_id
                or index_event_id not in record.indexed_event_ids
            ):
                _invalid_ledger(ledger_path, f"record {index_event_id!r} has invalid projection", strict=strict)
                continue
            records[index_event_id] = raw
    except (sqlite3.Error, json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        _invalid_ledger(ledger_path, str(exc), strict=strict)
        return None
    else:
        return records


def _decode_ledger_rows(
    ledger_path: Path,
    raw_records: Mapping[str, object],
    *,
    strict: bool,
    include_incomplete: bool = False,
) -> dict[str, TurnRecord]:
    """Retain completed turns and durable tombstones, including deferred session cleanup."""
    records: dict[str, TurnRecord] = {}
    decoded_records: dict[str, TurnRecord] = {}
    for event_id, raw_record in raw_records.items():
        record = TurnRecordCodec._from_ledger_record(event_id, raw_record)
        if record is None:
            _invalid_ledger(ledger_path, f"record {event_id!r} is malformed", strict=strict)
            continue
        decoded_records[event_id] = record
        fully_redacted = not record.replay_source_event_ids
        # Redaction callbacks commit tombstones; the next response in this
        # session removes the saved run and clears pending cleanup. An idle
        # tombstoned session is therefore settled without eager cleanup.
        if not record.completed and not fully_redacted and (strict or not include_incomplete):
            _invalid_ledger(ledger_path, f"record {event_id!r} is incomplete", strict=strict)
            continue
        records[event_id] = record
    if strict:
        conflict = _ledger_projection_conflict(decoded_records)
        if conflict is not None:
            _invalid_ledger(ledger_path, conflict, strict=True)
    return records


def _ledger_projection_conflict(records: Mapping[str, TurnRecord]) -> str | None:
    """Describe the first pair of conflicting physical projections."""
    for event_id, record in records.items():
        for indexed_event_id in record.indexed_event_ids:
            projected = records.get(indexed_event_id)
            if projected is not None and projected != record:
                return f"record {event_id!r} conflicts with projection {indexed_event_id!r}"
    return None


@dataclass(frozen=True)
class SupersessionProof:
    """Exact settled replay decision with an independently completed forward anchor."""

    source_event_id: str
    newer_event_id: str
    anchor_source_event_id: str
    anchor_response_event_id: str
    interrupted_response_event_id: str | None
    principal_id: str
    room_id: str
    thread_id: str
    requester_user_id: str


@dataclass(frozen=True)
class RecoveryProof:
    """One original source continued by its exact durably completed trusted relay."""

    source_event_id: str
    interrupted_response_event_id: str
    relay_event_id: str
    response_event_id: str
    final_event_id: str
    call_id: int
    principal_id: str
    room_id: str
    thread_id: str
    requester_user_id: str


@dataclass(frozen=True)
class _SettledJournalSource:
    event_id: str
    room_id: str
    thread_id: str
    sender: str
    timestamp: int
    state: str
    kind: str


@dataclass(frozen=True)
class _SupersessionDecision:
    newer_event_id: str
    thread_id: str | None


@dataclass(frozen=True)
class _PendingSupersessionDelivery:
    room_id: str
    thread_id: str
    delivery_id: str
    edits_event_id: str | None


@dataclass(frozen=True)
class _SupersessionSnapshot:
    records: Mapping[str, TurnRecord]
    sources: Mapping[str, _SettledJournalSource]
    pending_deliveries: tuple[_PendingSupersessionDelivery, ...]
    acknowledged_finals: Mapping[str, _AcknowledgedRecoveryFinal]


@dataclass(frozen=True)
class _AcknowledgedRecoveryFinal:
    """The exact successful FINAL facts consumed by restart recovery proof."""

    room_id: str
    thread_id: str
    response_event_id: str | None
    event_id: str
    payload: Mapping[str, Any]


def _read_recovery_finals(database: sqlite3.Connection, principal_id: str) -> dict[str, _AcknowledgedRecoveryFinal]:
    """Read acknowledged FINAL ownership within the caller's consistent transaction."""
    rows = database.execute(
        "SELECT delivery_id, room_id, thread_id, edits_event_id, acknowledged_event_id, payload_json "
        "FROM matrix_delivery_outbox WHERE principal_id = ? AND stage = 'final' "
        "AND acknowledged_event_id IS NOT NULL AND event_type = 'm.room.message' "
        "AND attempted = 1 AND retired = 0 AND edit_target_pending = 0 AND permanent_failure_reason IS NULL",
        (principal_id,),
    ).fetchall()
    finals = {}
    for delivery_id, room, thread, response, event_id, raw in rows:
        if any(not isinstance(value, str) for value in (delivery_id, room, thread, event_id, raw)) or (
            response is not None and not isinstance(response, str)
        ):
            msg = "malformed recovery FINAL identity"
            raise AssertionError(msg)
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            msg = "malformed recovery FINAL payload"
            raise AssertionError(msg) from exc
        assert isinstance(payload, dict), "malformed recovery FINAL payload"
        finals[delivery_id] = _AcknowledgedRecoveryFinal(room, thread, response, event_id, payload)
    return finals


def _read_supersession_snapshot(path: Path, principal_id: str) -> _SupersessionSnapshot:
    """Read full principal debt and strict turn projections in one read-only transaction."""
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as database:
            database.execute("BEGIN")
            raw = _load_ledger_rows(path, strict=True, database=database)
            assert raw is not None
            records = _decode_ledger_rows(path, raw, strict=False, include_incomplete=True)
            conflict = _ledger_projection_conflict(records)
            if conflict is not None:
                _invalid_ledger(path, conflict, strict=True)
            rows = database.execute(
                "SELECT event_id, room_id, thread_id, sender, origin_server_ts, state, kind "
                "FROM journal_events WHERE principal_id = ?",
                (principal_id,),
            ).fetchall()
            sources = {}
            for row in rows:
                if any(not isinstance(value, str) for value in (*row[:4], *row[5:])) or not isinstance(row[4], int):
                    msg = "malformed supersession journal row"
                    raise AssertionError(msg)
                source = _SettledJournalSource(*row)
                sources[source.event_id] = source
            pending = database.execute(
                "SELECT room_id, thread_id, delivery_id, edits_event_id FROM matrix_delivery_outbox "
                "WHERE principal_id = ? AND acknowledged_event_id IS NULL",
                (principal_id,),
            ).fetchall()
            if any(
                any(not isinstance(value, str) for value in row[:3])
                or (row[3] is not None and not isinstance(row[3], str))
                for row in pending
            ):
                msg = "malformed supersession outbox row"
                raise AssertionError(msg)
            return _SupersessionSnapshot(
                records,
                sources,
                tuple(_PendingSupersessionDelivery(*row) for row in pending),
                _read_recovery_finals(database, principal_id),
            )
    except sqlite3.Error as exc:
        msg = f"supersession journal snapshot failed: {exc}"
        raise AssertionError(msg) from exc


_SUPERSESSION_GUARD_MESSAGES = (
    "Skipping older message — newer unresponded message from same sender in thread",
    "Skipping older message — newer pending journal event from same sender in degraded thread replay guard",
)


def _supersession_decisions(log_path: Path | None) -> dict[str, tuple[_SupersessionDecision, ...]]:
    """Retain distinct positive guard candidates across retries, rejecting malformed log records."""
    if log_path is None or not log_path.exists():
        return {}
    decisions: dict[str, list[_SupersessionDecision]] = defaultdict(list)
    for line in _ANSI_ESCAPE_PATTERN.sub("", log_path.read_text()).splitlines(keepends=True):
        if not line.endswith("\n"):
            continue
        match = re.fullmatch(
            r"(?:\d{4}-\d{2}-\d{2}T[\d:.]+Z\s+)?(?:\[info\s*\]\s+)?("
            + "|".join(map(re.escape, _SUPERSESSION_GUARD_MESSAGES))
            + r")\s+\[mindroom\.bot\]\s+(.*)",
            line.rstrip("\r\n"),
        )
        if match is None:
            continue
        fields = match[2].split()
        pairs = [field.split("=", 1) for field in fields]
        if any(len(pair) != 2 for pair in pairs) or len({pair[0] for pair in pairs}) != len(pairs):
            continue
        values = dict(pairs)
        old, newer = values.get("skipped_event_id", ""), values.get("newer_event_id", "")
        if values.get("agent") != AGENT_NAME or not old.startswith("$") or not newer.startswith("$"):
            continue
        thread_id = values.get("thread_id")
        if (thread_id is not None and not thread_id.startswith("$")) or (
            match[1] == _SUPERSESSION_GUARD_MESSAGES[1] and thread_id is None
        ):
            continue
        decision = _SupersessionDecision(newer, thread_id)
        if decision not in decisions[old]:
            decisions[old].append(decision)
    return {source: tuple(candidates) for source, candidates in decisions.items()}


@dataclass(frozen=True)
class _CanonicalResponseView:
    """Body and terminal metadata from the same canonical Matrix replacement."""

    body: str
    stream_status: str | None
    completed: bool
    event_id: str | None
    timestamp: int | None
    payload: Mapping[str, Any]


def _response_payload_completed(payload: object) -> bool:
    """Recognize successful streaming or blocking delivery without discarding key presence."""
    if not isinstance(payload, Mapping):
        return False
    if STREAM_STATUS_KEY in payload and payload[STREAM_STATUS_KEY] != "completed":
        return False
    if AI_RUN_METADATA_KEY in payload:
        metadata = payload[AI_RUN_METADATA_KEY]
        return isinstance(metadata, Mapping) and metadata.get("status") == "completed"
    return payload.get(STREAM_STATUS_KEY) == "completed"


def _canonical_response_view(
    events: Mapping[str, Mapping[str, Any]],
    response_id: str,
    agent_id: str,
) -> _CanonicalResponseView:
    """Resolve body and metadata together from one authoritative same-room replacement."""
    candidates: list[tuple[tuple[int, int, str], _CanonicalResponseView]] = []
    for event in events.values():
        if (
            event.get("sender") != agent_id
            or event.get("type") != "m.room.message"
            or event.get("_audit_room_id") != events.get(response_id, {}).get("_audit_room_id")
        ):
            continue
        for candidate in (event, *_bundled_replacement_events(event)):
            content = candidate.get("content")
            if not isinstance(content, Mapping):
                continue
            relation = content.get("m.relates_to")
            edit = (
                isinstance(relation, Mapping)
                and relation.get("rel_type") == "m.replace"
                and relation.get("event_id") == response_id
            )
            event_id = candidate.get("event_id")
            if not isinstance(event_id, str) or (event_id != response_id and not edit):
                continue
            payload = content.get("m.new_content") if edit else content
            status = payload.get(STREAM_STATUS_KEY) if isinstance(payload, Mapping) else None
            body = _canonical_message_body(content, is_edit=edit)
            if body is None:
                continue
            view = _CanonicalResponseView(
                body,
                status if isinstance(status, str) else None,
                _response_payload_completed(payload),
                event_id,
                candidate.get("origin_server_ts") if isinstance(candidate.get("origin_server_ts"), int) else None,
                payload if isinstance(payload, Mapping) else {},
            )
            candidates.append((_replacement_order(event_id, candidate.get("origin_server_ts"), is_edit=edit), view))
    return (
        max(candidates, key=lambda candidate: candidate[0])[1]
        if candidates
        else _CanonicalResponseView("", None, False, None, None, {})
    )


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
        self._ledger_observations: dict[str, TurnRecord] = {}
        self._ledger_read_at = 0.0
        self.log_path: Path | None = None
        self.sent_records: Collection[_SentRecord] = ()
        self.source_current_markers: Mapping[str, str] = {}
        self.observed_markers_for = _ModelHandler.observed_markers_for
        self.full_request_markers_for = _ModelHandler.full_request_markers_for
        self.pending_edit_markers: Mapping[str, Mapping[str, str]] = {}
        self.supersession_proofs: dict[str, SupersessionProof] = {}
        self.recovery_proofs: dict[str, RecoveryProof] = {}
        self.canonical_events: dict[str, Mapping[str, Any]] = {}
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
        self._last_response_at = time.monotonic()
        self._last_progress_at = time.monotonic()

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
        """Observe all typed facts once, keeping only terminal owners for reply settlement."""
        if self.ledger_path is None:
            return
        now = time.monotonic()
        if now - self._ledger_read_at < min_interval:
            return
        self._ledger_read_at = now
        self.supersession_proofs = {}
        self.recovery_proofs = {}
        if self.log_path is None:
            self._ledger_observations = read_ledger_records(self.ledger_path, include_incomplete=True)
        else:
            auditor = FinalStateAuditor(
                self.client,
                self,
                agent_id=self.agent_id,
                ledger_path=self.ledger_path,
                expected_body_for=self.expected_body_for,
                source_current_markers=self.source_current_markers,
                observed_markers_for=self.observed_markers_for,
                full_request_markers_for=self.full_request_markers_for,
                pending_edit_markers=self.pending_edit_markers,
            )
            try:
                self._ledger_observations = dict(
                    auditor._observe_supersession(self.canonical_events, sent_records=self.sent_records),
                )
            except (AssertionError, OSError):
                # A concurrent write may invalidate this poll, never final qualification.
                self.supersession_proofs = {}
                self.recovery_proofs = {}
                self._ledger_observations = {}
        self._ledger_records = {
            event_id: record
            for event_id, record in self._ledger_observations.items()
            if record.completed or not record.replay_source_event_ids
        }

    def ledger_response(self, event_id: str) -> str | None:
        """Return the durable response one source's completed record attributes."""
        record = self._ledger_records.get(event_id)
        return record.response_event_id if record is not None else None

    def source_tombstoned(self, event_id: str) -> bool:
        """Return whether one source has its exact durable redaction tombstone."""
        record = self._ledger_records.get(event_id)
        return record is not None and event_id in record.redacted_source_event_ids

    def source_completed_without_response(self, event_id: str) -> bool:
        """Return whether one source durably settled without a response."""
        record = self._ledger_records.get(event_id)
        return record is not None and record.completed and record.response_event_id is None

    def _supersession_proven(self, event_id: str) -> bool:
        """Return whether exact settled journal ownership proves deliberate supersession."""
        return event_id in self.supersession_proofs

    def directly_settled(self, event_id: str) -> bool:
        """Return whether one source has its own reply or response-backed record."""
        if self.coalescing_threads and self.log_path is not None:
            return (
                self.ledger_response(event_id) is not None
                or self._supersession_proven(event_id)
                or event_id in self.recovery_proofs
            )
        return len(self.response_ids.get(event_id, ())) == 1 or self.ledger_response(event_id) is not None

    def settled_sources(self) -> set[str]:
        """Return sources settled under per-(thread, sender) chain semantics.

        MindRoom may supersede an older unresponded message once the same
        requester sends a newer one in the same thread. A chain settles from
        its newest required member backwards: the newest must be directly
        replied or response-backed in the ledger, and every older member must
        then present completed generation or exact settled-journal supersession
        proof anchored to its named newer source. Missing or malformed debt
        evidence never settles; an old generation may remain incomplete.
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
                if (
                    self.directly_settled(event_id)
                    or self._supersession_proven(event_id)
                    or self.source_completed_without_response(event_id)
                ):
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

    async def wait_until_settled(
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

    def outstanding(self) -> dict[str, str]:
        """Return the expected sources that still owe exactly one reply."""
        return {
            event_id: logical_ref
            for event_id, logical_ref in self.expected_sources.items()
            if len(self.response_ids[event_id]) != 1
        }

    async def wait_until_exact(
        self,
        budget: WaitBudget | None = None,
        *,
        deadline_seconds: float | None = None,
        settle_seconds: float = 0.0,
        on_slow: Callable[[SlowWaitNotice], None] | None = None,
        liveness: Callable[[], None] | None = None,
    ) -> float:
        """Wait until all sources have one reply and the room stays quiet.

        Returns the seconds spent waiting so the caller can turn a completed
        wait into a latency measurement. The wait ends early and loudly when
        the runtime stops making progress, and is extended when the deadline
        arrives while replies are still landing: a machine that is merely slow
        must not be reported as a broken product.
        """
        if budget is None:
            if deadline_seconds is None:
                msg = "a wait budget or deadline_seconds is required"
                raise TypeError(msg)
            started = time.monotonic()
            await self.wait_until_settled(deadline_seconds=deadline_seconds, settle_seconds=settle_seconds)
            return time.monotonic() - started
        return await self._wait_with_budget(budget, on_slow=on_slow, liveness=liveness)

    async def _wait_with_budget(
        self,
        budget: WaitBudget,
        *,
        on_slow: Callable[[SlowWaitNotice], None] | None = None,
        liveness: Callable[[], None] | None = None,
    ) -> float:
        """Apply the adaptive progress and stall budget to incremental sync."""
        started = time.monotonic()
        window_started = started
        deadline = started + budget.seconds
        self._last_progress_at = started
        extensions = 0
        complete_since: float | None = None
        while True:
            await self._sync_once(timeout_ms=250)
            self._assert_no_wrong_replies()
            if liveness is not None:
                liveness()
            now = time.monotonic()
            outstanding = self.outstanding()
            if not outstanding:
                # Every expected reply is in, so the only open question is
                # whether a duplicate follows it. That window closes on quiet.
                if now - self._last_response_at >= budget.settle_seconds:
                    return now - started
                complete_since = now if complete_since is None else complete_since
                if now - complete_since < budget.stall_seconds:
                    continue
                msg = (
                    f"every expected reply arrived but the room never went quiet for {budget.settle_seconds:.2f}s "
                    f"within {budget.stall_seconds:.1f}s: the agent is still emitting traffic nobody asked for"
                )
                raise AssertionError(msg)
            complete_since = None
            silent_seconds = now - self._last_progress_at
            if silent_seconds >= budget.stall_seconds:
                raise ExactReplyTimeoutError(
                    outstanding,
                    budget=budget,
                    waited_seconds=now - started,
                    silent_seconds=silent_seconds,
                    wedged=True,
                )
            if now < deadline:
                continue
            # An extension is only defensible while the lane is still draining.
            # A window that produced no reply at all is a wedge whatever the
            # arithmetic says, so it must never buy itself more time.
            drained_this_window = self._last_progress_at > window_started
            if drained_this_window and extensions < _MAX_BUDGET_EXTENSIONS:
                extensions += 1
                window_started = now
                deadline = now + budget.seconds
                if on_slow is not None:
                    on_slow(
                        SlowWaitNotice(
                            turns_outstanding=len(outstanding),
                            waited_seconds=now - started,
                            extension=extensions,
                        ),
                    )
                continue
            raise ExactReplyTimeoutError(
                outstanding,
                budget=budget,
                waited_seconds=now - started,
                silent_seconds=silent_seconds,
                wedged=not drained_this_window,
            )

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
        response-backed record, or its exact proven supersession anchor.
        A completed no-response record does not attribute another reply.
        """
        own_attribution = self.ledger_response(source_event_id)
        if own_attribution is not None:
            return own_attribution
        if not self._supersession_proven(source_event_id):
            return None
        return self.supersession_proofs[source_event_id].anchor_response_event_id

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
                        self._ingest_event({**raw_event, "_audit_room_id": room_id})

    def _ingest_event(self, event: Mapping[str, Any]) -> None:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in self.seen_event_ids:
            return
        self.seen_event_ids.add(event_id)
        self.canonical_events[event_id] = dict(event)
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
            for replacement in _bundled_replacement_events(event):
                self._ingest_event(replacement)

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
        first_reply_to_source = not self.response_ids[source_event_id]
        self.response_ids[source_event_id].add(event_id)
        logical_ref = self.expected_sources.get(source_event_id)
        if logical_ref is not None:
            self.response_event_by_ref[f"response:{logical_ref}"] = event_id
        sent_at = self.sent_at.get(source_event_id)
        if sent_at is not None and source_event_id not in self.reply_latencies:
            self.reply_latencies[source_event_id] = time.monotonic() - sent_at

        self._last_response_at = time.monotonic()
        if first_reply_to_source and logical_ref is not None:
            # Progress is the outstanding set shrinking, not merely traffic.
            # A duplicate or a stray reply must never look like a lane that is
            # still working through its queue.
            self._last_progress_at = self._last_response_at

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
        body = _canonical_message_body(content, is_edit=is_edit)
        if body is None:
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
    """One authored event, retaining ancestry for live proofs and final canonical-state auditing."""

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


def _canonical_message_body(content: Mapping[str, Any], *, is_edit: bool) -> str | None:
    """Parse one original or replacement body without outer-body edit fallback."""
    body_source = content.get("m.new_content") if is_edit else content
    if not isinstance(body_source, dict):
        return None
    body = body_source.get("body")
    return body if isinstance(body, str) else None


def _bundled_replacement_events(event: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Return valid server-bundled replacements of one original event."""
    original_event_id = event.get("event_id")
    original_sender = event.get("sender")
    if not isinstance(original_event_id, str) or not isinstance(original_sender, str):
        return ()
    candidates: list[dict[str, Any]] = []
    unsigned = event.get("unsigned")
    for container in (unsigned, event):
        if not isinstance(container, Mapping):
            continue
        relations = container.get("m.relations")
        if not isinstance(relations, Mapping):
            continue
        replacement = relations.get("m.replace")
        if not isinstance(replacement, Mapping):
            continue
        for candidate in (replacement.get("event"), replacement.get("latest_event"), replacement):
            if not isinstance(candidate, Mapping):
                continue
            normalized = {key: value for key, value in candidate.items() if isinstance(key, str)}
            content = normalized.get("content")
            relation = content.get("m.relates_to") if isinstance(content, Mapping) else None
            if (
                normalized.get("sender") == original_sender
                and normalized.get("type") == "m.room.message"
                and isinstance(normalized.get("event_id"), str)
                and isinstance(relation, Mapping)
                and relation.get("rel_type") == "m.replace"
                and relation.get("event_id") == original_event_id
            ):
                candidates.append(normalized)
    return tuple(candidates)


def _latest_response_body(
    events: Collection[Mapping[str, Any]],
    response_event_id: str,
    *,
    sender_id: str | None = None,
) -> str:
    """Return the newest standalone or server-bundled response body."""
    candidates: list[tuple[tuple[int, int, str], str]] = []
    for event in events:
        event_id = event.get("event_id")
        if sender_id is not None and event.get("sender") != sender_id:
            continue
        content = event.get("content")
        if not isinstance(event_id, str) or not isinstance(content, Mapping):
            continue
        relation = content.get("m.relates_to")
        is_original = event_id == response_event_id
        is_edit = (
            isinstance(relation, Mapping)
            and relation.get("rel_type") == "m.replace"
            and relation.get("event_id") == response_event_id
        )
        if is_original:
            response_events = (event, *_bundled_replacement_events(event))
        elif is_edit:
            response_events = (event,)
        else:
            continue
        for response_event in response_events:
            response_id = response_event.get("event_id")
            response_content = response_event.get("content")
            if not isinstance(response_id, str) or not isinstance(response_content, Mapping):
                continue
            response_relation = response_content.get("m.relates_to")
            response_is_edit = (
                isinstance(response_relation, Mapping)
                and response_relation.get("rel_type") == "m.replace"
                and response_relation.get("event_id") == response_event_id
            )
            body = _canonical_message_body(response_content, is_edit=response_is_edit)
            if body is not None:
                timestamp = response_event.get("origin_server_ts")
                candidates.append(
                    (_replacement_order(response_id, timestamp, is_edit=response_is_edit), body),
                )
    return max(candidates, default=((0, 0, ""), ""))[1]


def _response_view_diagnostic(
    events: Collection[Mapping[str, Any]],
    response_event_id: str,
) -> dict[str, object]:
    """Summarize one response view without dumping progressive body contents."""
    candidates: dict[str, tuple[Mapping[str, Any], bool, str]] = {}
    original_present = False
    for event in events:
        event_id = event.get("event_id")
        content = event.get("content")
        if not isinstance(event_id, str) or not isinstance(content, Mapping):
            continue
        if event_id == response_event_id:
            original_present = True
            candidates[event_id] = (event, False, "original")
            for replacement in _bundled_replacement_events(event):
                replacement_id = replacement["event_id"]
                if isinstance(replacement_id, str):
                    candidates[replacement_id] = (replacement, True, "bundled")
        relation = content.get("m.relates_to")
        if (
            isinstance(relation, Mapping)
            and relation.get("rel_type") == "m.replace"
            and relation.get("event_id") == response_event_id
        ):
            candidates[event_id] = (event, True, "standalone")

    ordered_candidates: list[dict[str, object]] = []
    for event_id, (event, is_edit, source) in sorted(
        candidates.items(),
        key=lambda item: _replacement_order(
            item[0],
            item[1][0].get("origin_server_ts"),
            is_edit=item[1][1],
        ),
    ):
        content = event.get("content")
        assert isinstance(content, Mapping)
        body_source = content.get("m.new_content") if is_edit else content
        normalized_body_source = body_source if isinstance(body_source, Mapping) else {}
        body = _canonical_message_body(content, is_edit=is_edit) or ""
        ordered_candidates.append(
            {
                "event_id": event_id,
                "origin_server_ts": event.get("origin_server_ts"),
                "source": source,
                "stream_status": normalized_body_source.get(STREAM_STATUS_KEY),
                "body_length": len(body),
                "body_tail": body[-160:],
            },
        )
    body = _latest_response_body(events, response_event_id)
    return {
        "original_present": original_present,
        "ordered_candidates": ordered_candidates,
        "latest_body_length": len(body),
        "latest_body_tail": body[-160:],
    }


def _auto_resume_relay_target(
    event: Mapping[str, Any],
    *,
    relay_senders: Collection[str],
) -> tuple[str, str] | None:
    """Return the interrupted target and thread root for one exact resume relay."""
    if event.get("sender") not in relay_senders or event.get("type") != "m.room.message":
        return None
    content = event.get("content")
    if not isinstance(content, dict) or content.get(SOURCE_KIND_KEY) != TRUSTED_INTERNAL_RELAY_SOURCE_KIND:
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


@dataclass(frozen=True, slots=True)
class RedactedEditEvidence:
    """Exact model observations frozen when one physical edit's redaction returns."""

    source_event_id: str
    edit_event_id: str
    marker: str
    observed_call_ids: frozenset[int]
    known_edit_event_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class RuntimeRedactionEntry:
    """Synchronous entry to one owner's physical-source invalidation."""

    agent_name: str
    principal_id: str
    target_event_id: str
    monotonic_ns: int
    already_redacted: bool


def _parse_runtime_redaction_entry(line: str) -> RuntimeRedactionEntry:
    """Reject incomplete or ill-typed serialized entry facts."""
    try:
        raw = json.loads(line)
    except ValueError as exc:
        msg = "invalid runtime redaction evidence: malformed entry"
        raise AssertionError(msg) from exc
    if (
        not isinstance(raw, dict)
        or set(raw) != {"agent_name", "principal_id", "target_event_id", "monotonic_ns", "already_redacted"}
        or any(
            not isinstance(raw[key], str) or not raw[key] for key in ("agent_name", "principal_id", "target_event_id")
        )
        or type(raw["monotonic_ns"]) is not int
        or raw["monotonic_ns"] <= 0
        or type(raw["already_redacted"]) is not bool
        or not raw["principal_id"].startswith(raw["agent_name"] + "@@")
    ):
        msg = "invalid runtime redaction evidence: invalid entry fields"
        raise AssertionError(msg)
    return RuntimeRedactionEntry(**raw)


def _runtime_redaction_cutoff(path: Path | None, principal_id: str, target_id: str) -> int | None:
    """Use only the first complete exact-owner entry; damaged evidence fails closed."""
    if path is None or not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        content = handle.read()
    first: RuntimeRedactionEntry | None = None
    previous: dict[tuple[str, str], RuntimeRedactionEntry] = {}
    if content and not content.endswith("\n"):
        msg = "invalid runtime redaction evidence: truncated entry"
        raise AssertionError(msg)
    for line in content.splitlines():
        entry = _parse_runtime_redaction_entry(line)
        key = (entry.principal_id, entry.target_event_id)
        earlier = previous.get(key)
        if earlier is not None and (
            entry.monotonic_ns < earlier.monotonic_ns or (earlier.already_redacted and not entry.already_redacted)
        ):
            msg = "invalid runtime redaction evidence: contradictory entries"
            raise AssertionError(msg)
        previous[key] = entry
        if first is None and key == (principal_id, target_id):
            first = entry
    return first.monotonic_ns if first is not None and not first.already_redacted else None


def _historical_call_observed(
    evidence: RedactedEditEvidence,
    call_id: int,
    *,
    agent_id: str,
    runtime_redaction_path: Path | None,
) -> bool:
    """Join immutable HTTP membership with exact requests strictly before runtime entry."""
    if call_id in evidence.observed_call_ids:
        return True
    cutoff = _runtime_redaction_cutoff(
        runtime_redaction_path,
        ManagedTuwunelStack._journal_principal_id(agent_id),
        evidence.edit_event_id,
    )
    observation = _ModelHandler.timed_observations_snapshot().get(call_id)
    return (
        cutoff is not None
        and observation is not None
        and observation.monotonic_ns < cutoff
        and evidence.marker in observation.markers
    )


def _redaction_target_state(
    target_id: str,
    records: Mapping[str, TurnRecord],
    source_revision_markers: Mapping[str, Mapping[str, str]],
) -> tuple[bool, bool]:
    """Join exact event invalidation and cleanup debt across every response owner."""
    source_id = next((source for source, edits in source_revision_markers.items() if target_id in edits), None)
    physical = records.get(target_id)
    revisions = [
        revision
        for record in records.values()
        if (revision := (record.revision_replay or {}).get(target_id)) is not None
        and (source_id is None or revision.source_event_id == source_id)
    ]
    tombstoned = (physical is not None and target_id in physical.redacted_source_event_ids) or any(
        revision.redacted for revision in revisions
    )
    # Reconciliation joins physical invalidation into every registered owner
    # before locked cleanup acknowledges that owner's debt independently.
    pending = (physical is not None and target_id in physical.pending_redaction_cleanup_event_ids) or any(
        revision.cleanup_pending or (tombstoned and not revision.redacted) for revision in revisions
    )
    return tombstoned, pending


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
        cleanup_probes: Mapping[str, tuple[str, ...]] | None = None,
        full_request_markers_for: Callable[[int], frozenset[str]] = _ModelHandler.full_request_markers_for,
        redacted_edit_evidence: Mapping[str, RedactedEditEvidence] | None = None,
        pending_edit_markers: Mapping[str, Mapping[str, str]] | None = None,
        observed_cleanup_probes: Mapping[str, tuple[str, ...]] | None = None,
        runtime_redaction_path: Path | None = None,
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
        self.cleanup_probes = dict(cleanup_probes or {})
        self.observed_cleanup_probes = dict(observed_cleanup_probes or {})
        self.full_request_markers_for = full_request_markers_for
        self.redacted_edit_evidence = dict(redacted_edit_evidence or {})
        self.runtime_redaction_path = runtime_redaction_path
        self.pending_edit_markers = {source: dict(edits) for source, edits in (pending_edit_markers or {}).items()}
        self.supersession_proofs: dict[str, SupersessionProof] = {}
        self.recovery_proofs: dict[str, RecoveryProof] = {}

    def _observe_supersession(
        self,
        events: Mapping[str, Mapping[str, Any]],
        *,
        sent_records: Collection[_SentRecord] = (),
        replies: Mapping[str, set[str]] | None = None,
    ) -> Mapping[str, TurnRecord]:
        """Join current ownership and positive decisions without rewriting generation outcomes."""
        assert self.ledger_path is not None
        snapshot = _read_supersession_snapshot(self.ledger_path, f"{AGENT_NAME}@{self.agent_id}")
        decisions = _supersession_decisions(self.oracle.log_path)
        # Final audit already validated this view; live polling must validate
        # with the same retained ancestry before joining durable ownership.
        replies = self._canonical_agent_replies(events, sent_records=sent_records) if replies is None else replies
        authored = {record.event_id: record for record in sent_records}
        self.recovery_proofs = self._observe_recovery(snapshot, events, replies, authored)
        self.oracle.recovery_proofs = self.recovery_proofs
        proofs: dict[str, SupersessionProof] = {}
        for chain in self.oracle.chains.values():
            for index in range(len(chain) - 1, -1, -1):
                source = chain[index]
                # A process may stop after logging its guard but before settlement.
                # Prefer the latest observation that independently proves a link.
                for decision in reversed(decisions.get(source, ())):
                    if decision.newer_event_id not in chain[index + 1 :]:
                        continue
                    proof = self._prove_supersession(
                        source,
                        decision.newer_event_id,
                        snapshot,
                        events,
                        replies,
                        proofs,
                        sent_records=authored,
                    )
                    if proof is None or decision.thread_id not in {None, proof.thread_id}:
                        continue
                    proofs[source] = proof
                    break
        # A shared unfinished owner is indivisible: one proved source cannot
        # discharge another live source's generation or mutation debt.
        for source, proof in tuple(proofs.items()):
            record = snapshot.records.get(source)
            if record is not None and (
                any(owned not in proofs for owned in record.replay_source_event_ids)
                or record.pending_redaction_cleanup_event_ids
                or any(revision.cleanup_pending for revision in (record.revision_replay or {}).values())
            ):
                proofs.pop(proof.source_event_id, None)
        # Removing one incomplete owner also invalidates every chain depending on it.
        for source in tuple(proofs):
            proof = proofs[source]
            if proof.newer_event_id != proof.anchor_source_event_id and proof.newer_event_id not in proofs:
                del proofs[source]
        self.supersession_proofs = proofs
        self.oracle.supersession_proofs = proofs
        return snapshot.records

    def _observe_recovery(
        self,
        snapshot: _SupersessionSnapshot,
        events: Mapping[str, Mapping[str, Any]],
        replies: Mapping[str, set[str]],
        sent_records: Mapping[str, _SentRecord],
    ) -> dict[str, RecoveryProof]:
        """Join complete recovery chains once for every consumer of interrupted work."""
        interrupted_targets = {
            target[0]
            for event in events.values()
            if (target := _auto_resume_relay_target(event, relay_senders=self.oracle.internal_relay_senders))
            is not None
        }
        proofs = {
            source: proof
            for source in self.oracle.expected_sources
            if (record := snapshot.records.get(source)) is not None
            and record.response_event_id in interrupted_targets
            and (proof := self._prove_recovery(source, snapshot, events, replies, sent_records)) is not None
        }
        # Coalesced original ownership remains indivisible even after interruption.
        return {
            source: proof
            for source, proof in proofs.items()
            if all(owned in proofs for owned in snapshot.records[source].replay_source_event_ids)
        }

    def _prove_recovery(
        self,
        source: str,
        snapshot: _SupersessionSnapshot,
        events: Mapping[str, Mapping[str, Any]],
        replies: Mapping[str, set[str]],
        sent_records: Mapping[str, _SentRecord],
    ) -> RecoveryProof | None:
        """Bind one admitted original interruption to its unique trusted continuation."""
        old = snapshot.records.get(source)
        row = snapshot.sources.get(source)
        root = self._source_thread_root(source, events, sent_records, seen=set())
        if (
            old is None
            or row is None
            or root is None
            or row.state != "settled"
            or source not in old.replay_source_event_ids
            or old.response_event_id is None
            or old.requester_id_for_source(source) != row.sender
            or not self._journal_source_matches(row, events, root)
            or not self._record_sources_share_thread(old)
        ):
            return None
        interrupted = old.response_event_id
        old_view = _canonical_response_view(events, interrupted, self.agent_id)
        relays = [relay_id for relay_id, event in events.items() if self._relay_replies_to(event, root, interrupted)]
        if len(relays) != 1 or not self._recovery_interruption_valid(source, old, old_view, replies):
            return None
        relay = relays[0]
        continuation = self._completed_recovery_relay(relay, row, root, snapshot, events, replies)
        if continuation is None:
            return None
        answer, view = continuation
        relay_row = snapshot.sources[relay]
        if (
            old_view.timestamp is None
            or old_view.timestamp < row.timestamp
            or relay_row.timestamp < old_view.timestamp
            or self._recovery_has_debt((old, snapshot.records[relay]), snapshot, row.room_id, root)
        ):
            return None
        call = _body_call_id(view.body)
        marker = self.source_current_markers.get(source)
        if call is None or marker is None or marker not in self.full_request_markers_for(call):
            return None
        assert view.event_id is not None
        return RecoveryProof(
            source,
            interrupted,
            relay,
            answer,
            view.event_id,
            call,
            f"{AGENT_NAME}@{self.agent_id}",
            row.room_id,
            root,
            row.sender,
        )

    def _recovery_interruption_valid(
        self,
        source: str,
        record: TurnRecord,
        view: _CanonicalResponseView,
        replies: Mapping[str, set[str]],
    ) -> bool:
        """Retain exact original reply and any already-published model obligations."""
        if (
            self._visible_record_reply_ids(record, replies) != {record.response_event_id}
            or view.stream_status != "error"
            or not view.body.endswith(RESTART_INTERRUPTED_RESPONSE_NOTE)
        ):
            return False
        published = view.body.removesuffix(RESTART_INTERRUPTED_RESPONSE_NOTE).rstrip()
        if not published:
            return True
        call = _body_call_id(published)
        return (
            call is not None
            and self.expected_body_for(call).startswith(published)
            and self.source_current_markers.get(source) in self.observed_markers_for(call)
        )

    def _completed_recovery_relay(
        self,
        relay: str,
        original: _SettledJournalSource,
        root: str,
        snapshot: _SupersessionSnapshot,
        events: Mapping[str, Mapping[str, Any]],
        replies: Mapping[str, set[str]],
    ) -> tuple[str, _CanonicalResponseView] | None:
        """Require the relay's own settled identity, requester and completed response."""
        row = snapshot.sources.get(relay)
        turn = snapshot.records.get(relay)
        content = events[relay].get("content", {})
        if (
            row is None
            or turn is None
            or row.state != "settled"
            or row.room_id != original.room_id
            or row.timestamp <= original.timestamp
            or not self._journal_source_matches(row, events, root)
            or content.get(ORIGINAL_SENDER_KEY) != original.sender
            or turn.requester_id_for_source(relay) != original.sender
            or turn.source_event_ids != (relay,)
            or turn.replay_source_event_ids != (relay,)
            or not turn.completed
            or turn.response_event_id is None
        ):
            return None
        answer = turn.response_event_id
        view = _canonical_response_view(events, answer, self.agent_id)
        call = _body_call_id(view.body)
        answer_timestamp = events.get(answer, {}).get("origin_server_ts")
        if (
            replies.get(relay) != {answer}
            or events.get(answer, {}).get("_audit_room_id") != original.room_id
            or self._thread_root(events.get(answer, {})) != root
            or not isinstance(answer_timestamp, int)
            or answer_timestamp < row.timestamp
            or view.timestamp is None
            or view.timestamp < answer_timestamp
            or not view.completed
            or call is None
            or view.body != self.expected_body_for(call)
            or not self._recovery_final_matches(turn, view, snapshot, original.room_id, root)
        ):
            return None
        return answer, view

    @staticmethod
    def _recovery_final_matches(
        turn: TurnRecord,
        view: _CanonicalResponseView,
        snapshot: _SupersessionSnapshot,
        room_id: str,
        root: str,
    ) -> bool:
        """The acknowledged FINAL must be the selected canonical completed payload."""
        final = snapshot.acknowledged_finals.get(turn.anchor_event_id or "")
        if final is None:
            return False
        payload = final.payload.get("m.new_content") if final.response_event_id is not None else final.payload
        relation = final.payload.get("m.relates_to")
        target_matches = (
            (
                isinstance(relation, Mapping)
                and relation.get("rel_type") == "m.replace"
                and relation.get("event_id") == turn.response_event_id
            )
            if final.response_event_id is not None
            else final.event_id == turn.response_event_id
        )
        return (
            final.room_id == room_id
            and final.thread_id == root
            and final.response_event_id in {None, turn.response_event_id}
            and final.event_id == view.event_id
            and target_matches
            and payload == view.payload
        )

    def _recovery_has_debt(
        self,
        owners: Collection[TurnRecord],
        snapshot: _SupersessionSnapshot,
        room_id: str,
        root: str,
    ) -> bool:
        """Recovery cannot discharge source mutation, cleanup or visible delivery debt."""
        source_ids = {source for record in owners for source in record.indexed_event_ids}
        response_ids = {record.response_event_id for record in owners}
        return (
            any(self.pending_edit_markers.get(source) for source in source_ids)
            or any(record.pending_redaction_cleanup_event_ids for record in owners)
            or any(
                revision.cleanup_pending for record in owners for revision in (record.revision_replay or {}).values()
            )
            or any(
                (delivery.room_id == room_id and delivery.thread_id in {root, ""})
                or delivery.delivery_id in source_ids
                or delivery.edits_event_id in response_ids
                for delivery in snapshot.pending_deliveries
            )
        )

    def _prove_supersession(
        self,
        source: str,
        newer: str,
        snapshot: _SupersessionSnapshot,
        events: Mapping[str, Mapping[str, Any]],
        replies: Mapping[str, set[str]],
        proofs: Mapping[str, SupersessionProof],
        *,
        sent_records: Mapping[str, _SentRecord],
    ) -> SupersessionProof | None:
        """Validate one forward link against identity, debt, visible interruption and completed anchor."""
        root = self._supersession_source_pair(source, newer, snapshot, events, sent_records)
        if root is None:
            return None
        old_record = snapshot.records.get(source)
        if old_record is not None and (old_record.completed or source not in old_record.replay_source_event_ids):
            return None
        old_response = old_record.response_event_id if old_record is not None else None
        old_view = _canonical_response_view(events, old_response, self.agent_id) if old_response is not None else None
        if old_response is None:
            if replies.get(source):
                return None
        elif (
            old_record is None
            or old_response not in self._visible_record_reply_ids(old_record, replies)
            or old_view is None
            or old_view.stream_status != "error"
            or not old_view.body.endswith(RESTART_INTERRUPTED_RESPONSE_NOTE)
            or any(self._relay_replies_to(event, root, old_response) for event in events.values())
        ):
            return None
        anchor = self._completed_supersession_anchor(newer, snapshot.records, events, replies)
        if anchor is None:
            forward = proofs.get(newer)
            if forward is None:
                return None
            anchor = (forward.anchor_source_event_id, forward.anchor_response_event_id)
        row = snapshot.sources[source]
        return SupersessionProof(
            source,
            newer,
            *anchor,
            old_response,
            f"{AGENT_NAME}@{self.agent_id}",
            row.room_id,
            root,
            row.sender,
        )

    def _supersession_source_pair(
        self,
        source: str,
        newer: str,
        snapshot: _SupersessionSnapshot,
        events: Mapping[str, Mapping[str, Any]],
        sent_records: Mapping[str, _SentRecord],
    ) -> str | None:
        """Require matching admitted requester identities in the same resolved Matrix thread."""
        row, next_row = snapshot.sources.get(source), snapshot.sources.get(newer)
        if row is None or next_row is None or row.state != "settled" or next_row.state != "settled":
            return None
        root = self._source_thread_root(source, events, sent_records, seen=set())
        owners = [snapshot.records[event_id] for event_id in (source, newer) if event_id in snapshot.records]
        delivery_ids = {source, newer, *(record.anchor_event_id for record in owners)}
        response_ids = {record.response_event_id for record in owners} - {None}
        if (
            root is None
            or root != self._source_thread_root(newer, events, sent_records, seen=set())
            or row.room_id != next_row.room_id
            or row.sender != next_row.sender
            or row.timestamp >= next_row.timestamp
            or any(
                (delivery.room_id == row.room_id and delivery.thread_id in {root, ""})
                or delivery.delivery_id in delivery_ids
                or delivery.edits_event_id in response_ids
                for delivery in snapshot.pending_deliveries
            )
            or not all(self._journal_source_matches(item, events, root) for item in (row, next_row))
            or self.pending_edit_markers.get(source)
            or self.pending_edit_markers.get(newer)
        ):
            return None
        return root

    @staticmethod
    def _journal_source_matches(row: _SettledJournalSource, events: Mapping[str, Mapping[str, Any]], root: str) -> bool:
        """Bind an admitted identity to canonical wire facts; plain replies resolve by ancestry."""
        event = events.get(row.event_id, {})
        return (
            event.get("event_id") == row.event_id
            and event.get("_audit_room_id") == row.room_id
            and event.get("sender") == row.sender
            and event.get("origin_server_ts") == row.timestamp
            and event.get("type") == "m.room.message"
            and row.kind == "message"
            and row.thread_id in {"", root}
        )

    def _completed_supersession_anchor(
        self,
        source: str,
        records: Mapping[str, TurnRecord],
        events: Mapping[str, Mapping[str, Any]],
        replies: Mapping[str, set[str]],
    ) -> tuple[str, str] | None:
        """Require exact durable completion and visible current-source model consumption."""
        record = records.get(source)
        if (
            record is None
            or not record.completed
            or source not in record.replay_source_event_ids
            or record.pending_redaction_cleanup_event_ids
            or any(revision.cleanup_pending for revision in (record.revision_replay or {}).values())
        ):
            return None
        response = record.response_event_id
        if response is None or response not in self._visible_record_reply_ids(record, replies):
            return None
        view = _canonical_response_view(events, response, self.agent_id)
        if not self._record_sources_share_thread(record) or not view.completed:
            return None
        body = view.body
        call = _body_call_id(body)
        marker = self.source_current_markers.get(source)
        if (
            call is None
            or body != self.expected_body_for(call)
            or marker is None
            or marker not in self.observed_markers_for(call)
        ):
            return None
        return source, response

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
        records = (
            self._observe_supersession(events, sent_records=sent_records, replies=replies)
            if self.ledger_path is not None
            else None
        )
        completed = self._assert_final_bodies_complete(events, replies)
        self._assert_sync_view_parity(events, sent_records, replies)
        ledger_metrics: dict[str, int] = {}
        if self.ledger_path is not None:
            if not self.ledger_path.exists():
                msg = f"handled-turn ledger missing at {self.ledger_path}"
                raise AssertionError(msg)
            assert records is not None
            for event_id, record in records.items():
                if not record.completed and any(
                    source not in self.supersession_proofs and source not in self.recovery_proofs
                    for source in record.replay_source_event_ids
                ):
                    _invalid_ledger(
                        self.ledger_path,
                        f"record {event_id!r} is incomplete without exact supersession proof or recovery proof",
                        strict=True,
                    )
            redacted_sources = set(redacted) & set(self.oracle.expected_sources)
            ledger_metrics = self._assert_ledger_attribution(
                replies,
                records=records,
                redacted_source_event_ids=redacted_sources,
            )
            self._assert_model_saw_current_sources(
                events,
                records=records,
                redacted_source_event_ids=redacted_sources,
                redacted_targets=redacted,
            )
            ledger_metrics.update(self._assert_redaction_cleanup_probes(events, records))
        else:
            self._assert_direct_reply_model_sources(events, replies)
        return {
            "audited_events": len(events),
            "audited_rooms": len(set(room_ids)),
            "completed_final_bodies": completed,
            "superseded_interrupted_bodies": len(
                {proof.interrupted_response_event_id for proof in self.supersession_proofs.values()} - {None},
            ),
            "recovered_interrupted_bodies": len(
                {proof.interrupted_response_event_id for proof in self.recovery_proofs.values()},
            ),
            **ledger_metrics,
        }

    def _assert_redaction_cleanup_probes(
        self,
        events: Mapping[str, Mapping[str, Any]],
        records: Mapping[str, TurnRecord],
    ) -> dict[str, int]:
        """Dedicated probes owe responses; ordinary sources retain their own terminal contract."""
        uncovered = 0
        checked_calls = 0
        for probe_id, source_ids in {**self.cleanup_probes, **self.observed_cleanup_probes}.items():
            record = records.get(probe_id)
            response_id = record.response_event_id if record is not None else None
            completed_response = record is not None and record.completed and response_id is not None
            if probe_id in self.cleanup_probes and not completed_response:
                msg = f"redaction cleanup probe {probe_id} has no completed response"
                raise AssertionError(msg)
            call_id = _body_call_id(self._latest_agent_body(events, response_id)) if response_id is not None else None
            forbidden = {marker for source_id in source_ids for marker in self._cleanup_target_markers(source_id)}
            # A replay may edit or redact the probe itself later. Any authored
            # probe revision proves capture; the current-source audit above
            # independently enforces the latest revision for live sources.
            probe_markers = {
                _source_marker(self.oracle.expected_sources[probe_id], ORIGINAL_REVISION),
                *self.source_revision_markers.get(probe_id, {}).values(),
            }
            calls = {
                observed_call
                for observed_call, markers in _ModelHandler.observations_snapshot().items()
                if probe_markers.intersection(markers)
            }
            if call_id is not None:
                calls.add(call_id)
            for source_id in source_ids:
                tombstoned, pending = _redaction_target_state(source_id, records, self.source_revision_markers)
                is_edit = any(source_id in revisions for revisions in self.source_revision_markers.values())
                # Edit acknowledgement is monotonic before model admission.
                # Original-source callbacks can re-arm debt after an ordinary call.
                requires_cleanup = probe_id in self.cleanup_probes or (bool(calls) and is_edit)
                if not tombstoned or (pending and requires_cleanup):
                    msg = (
                        f"redaction cleanup probe {probe_id} left pending or missing tombstone cleanup for {source_id}"
                    )
                    raise AssertionError(msg)
            if not calls:
                if probe_id in self.cleanup_probes:
                    msg = f"redaction cleanup probe {probe_id} has no model-call evidence"
                    raise AssertionError(msg)
                uncovered += 1
            for observed_call in calls:
                observed = self.full_request_markers_for(observed_call)
                if not probe_markers & observed or forbidden & observed:
                    msg = (
                        f"redaction cleanup probe {probe_id} call {observed_call} has missing full-request evidence "
                        f"or redacted history: {sorted(forbidden & observed)}"
                    )
                    raise AssertionError(msg)
                checked_calls += 1
        return {"redaction_cleanup_uncovered_sources": uncovered, "redaction_cleanup_checked_calls": checked_calls}

    def _cleanup_target_markers(self, target_id: str) -> set[str]:
        """An edit forbids only its removed marker; a source forbids all its revisions."""
        for edits in self.source_revision_markers.values():
            if target_id in edits:
                return {edits[target_id]}
        return {
            _source_marker(self.oracle.expected_sources[target_id], ORIGINAL_REVISION),
            *self.source_revision_markers.get(target_id, {}).values(),
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
            elif source_event_id in oracle.optional_sources or count == 1:
                continue
            elif not oracle.coalescing_threads:
                problems.append(f"source {logical_ref} has {count} canonical replies in /messages")
            elif not self._visible_thread_reply_ids(source_event_id, replies):
                problems.append(
                    f"source {logical_ref} has no canonical reply in its Matrix thread",
                )
        for source_event_id, reply_ids in replies.items():
            if source_event_id in oracle.expected_sources or source_event_id in oracle.internal_source_ids:
                continue
            problems.append(f"unexpected agent replies to {source_event_id}: {sorted(reply_ids)}")
        if problems:
            msg = f"final reply cardinality audit failed: {problems}"
            raise AssertionError(msg)

    def _assert_ledger_attribution(
        self,
        replies: Mapping[str, set[str]],
        *,
        records: Mapping[str, TurnRecord] | None = None,
        redacted_source_event_ids: Collection[str] = (),
    ) -> dict[str, int]:
        """Every required source must present its own durable terminal record.

        Matrix relations cannot expose which sources one coalesced reply
        covered, so exact per-source attribution comes from MindRoom's
        handled-turn ledger, walked per (thread, sender) chain to honor the
        supersede policy, and cross-checked against the `/messages` view in
        both directions. The same typed supersession proof backs live settlement,
        final body classification and durable attribution. Completed no-response
        turns remain completed outcomes; chronology alone proves nothing.
        """
        assert self.ledger_path is not None
        oracle = self.oracle
        if records is None:
            if not self.ledger_path.exists():
                msg = f"handled-turn ledger missing at {self.ledger_path}"
                raise AssertionError(msg)
            records = read_ledger_records(self.ledger_path, strict=True)

        problems: list[str] = []
        harness_redacted = set(redacted_source_event_ids)
        problems.extend(
            self._ledger_redaction_problems(
                records,
                harness_redacted,
                set(oracle.expected_sources),
            ),
        )
        ledger_response_ids, attributed, optional_problems = self._attribute_optional_replies(
            replies,
            records,
        )
        problems.extend(optional_problems)
        required_response_ids, required_attributed, superseded, required_problems = self._attribute_required_replies(
            replies,
            records,
        )
        ledger_response_ids.update(required_response_ids)
        attributed += required_attributed
        problems.extend(required_problems)

        all_expected_reply_ids = {
            reply_id
            for source_event_id, reply_ids in replies.items()
            if source_event_id in oracle.expected_sources
            for reply_id in reply_ids
        }
        problems.extend(
            f"ledger response {response_id} is not a visible canonical reply"
            for response_id in sorted(ledger_response_ids - all_expected_reply_ids)
        )
        problems.extend(
            f"visible reply {reply_id} is not attributed by any durable turn record"
            for reply_id in sorted(all_expected_reply_ids - ledger_response_ids)
        )
        if problems:
            msg = f"durable turn attribution audit failed: {problems}"
            raise AssertionError(msg)
        return {
            "ledger_attributed_sources": attributed,
            "ledger_superseded_sources": superseded,
            "ledger_recovered_sources": len(self.recovery_proofs),
        }

    def _attribute_required_replies(
        self,
        replies: Mapping[str, set[str]],
        records: Mapping[str, TurnRecord],
    ) -> tuple[set[str], int, int, list[str]]:
        """Audit response attribution for every non-optional requester chain."""
        response_ids: set[str] = set()
        attributed = 0
        superseded = 0
        problems: list[str] = []
        for chain in self.oracle.chains.values():
            anchored = False
            for source_event_id in reversed(chain):
                if source_event_id in self.oracle.optional_sources:
                    continue
                logical_ref = self.oracle.expected_sources[source_event_id]
                record = records.get(source_event_id)
                if record is not None and source_event_id not in record.source_event_ids:
                    problems.append(
                        f"turn record keyed by {source_event_id} does not own that source: {record.source_event_ids}",
                    )
                    record = None
                if record is not None and not self._record_sources_share_thread(record):
                    problems.append(
                        f"turn record keyed by {source_event_id} coalesces sources across logical Matrix threads: "
                        f"{record.source_event_ids}",
                    )
                    record = None
                proof = self.supersession_proofs.get(source_event_id)
                recovery = self.recovery_proofs.get(source_event_id)
                if (
                    record is not None
                    and (record.completed or proof is not None or recovery is not None)
                    and record.response_event_id is not None
                ):
                    visible_record_reply_ids = self._visible_record_reply_ids(record, replies)
                    if record.response_event_id not in visible_record_reply_ids:
                        problems.append(
                            f"ledger response {record.response_event_id} for {logical_ref} "
                            f"({source_event_id}) is not a visible canonical reply for its owned sources",
                        )
                    else:
                        response_ids.add(record.response_event_id)
                        attributed += int(proof is None and recovery is None)
                        superseded += int(proof is not None)
                        anchored = True
                elif proof is not None or (
                    anchored and record is not None and record.completed and record.response_event_id is None
                ):
                    attributed += int(proof is None)
                    superseded += int(proof is not None)
                elif anchored:
                    problems.append(
                        f"superseded chain source {logical_ref} ({source_event_id}) "
                        "has no completed record or exact journal supersession proof",
                    )
                else:
                    problems.append(
                        f"newest chain source {logical_ref} ({source_event_id}) has no durable attribution",
                    )
        return response_ids, attributed, superseded, problems

    @staticmethod
    def _ledger_redaction_problems(
        records: Mapping[str, TurnRecord],
        harness_redacted: set[str],
        audited_source_event_ids: set[str],
    ) -> list[str]:
        """Require exact durable tombstones for harness-authored source redactions."""
        problems: list[str] = []
        for event_id, record in records.items():
            forged = (set(record.redacted_source_event_ids) & audited_source_event_ids) - harness_redacted
            if forged:
                problems.append(
                    f"turn record {event_id} claims unobserved source redactions: {sorted(forged)}",
                )
        for source_event_id in sorted(harness_redacted):
            record = records.get(source_event_id)
            if record is None or source_event_id not in record.redacted_source_event_ids:
                problems.append(
                    f"harness-redacted source {source_event_id} has no durable tombstone",
                )
        return problems

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
            if record is not None and not self._record_sources_share_thread(record):
                problems.append(
                    f"turn record keyed by {source_event_id} coalesces sources across logical Matrix threads: "
                    f"{record.source_event_ids}",
                )
                record = None
            if not visible_reply_ids:
                if record is not None and record.response_event_id is not None:
                    visible_record_reply_ids = self._visible_record_reply_ids(record, replies)
                    if record.response_event_id not in visible_record_reply_ids:
                        problems.append(
                            f"ledger response {record.response_event_id} for optional source "
                            f"{source_event_id} is not a visible canonical reply for its owned sources",
                        )
                    else:
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

    def _visible_thread_reply_ids(
        self,
        source_event_id: str,
        replies: Mapping[str, set[str]],
    ) -> set[str]:
        """Return canonical replies attached anywhere in one logical Matrix thread."""
        source_thread = self.oracle.source_threads.get(source_event_id)
        return {
            reply_id
            for candidate_event_id, candidate_thread in self.oracle.source_threads.items()
            if candidate_thread == source_thread
            for reply_id in replies.get(candidate_event_id, ())
        }

    @staticmethod
    def _visible_record_reply_ids(
        record: TurnRecord,
        replies: Mapping[str, set[str]],
    ) -> set[str]:
        """Return canonical replies attached to sources owned by one durable turn."""
        return {
            reply_id for source_event_id in record.source_event_ids for reply_id in replies.get(source_event_id, ())
        }

    def _record_sources_share_thread(self, record: TurnRecord) -> bool:
        """Return whether every owned source belongs to one known Matrix thread."""
        source_threads = {
            self.oracle.source_threads.get(source_event_id) for source_event_id in record.source_event_ids
        }
        return None not in source_threads and len(source_threads) == 1

    def _assert_model_saw_current_sources(
        self,
        events: Mapping[str, Mapping[str, Any]],
        *,
        records: Mapping[str, TurnRecord] | None = None,
        redacted_source_event_ids: Collection[str] = (),
        redacted_targets: Mapping[str, str] | None = None,
    ) -> None:
        """Every response-backed turn must be generated from its sources' current bodies.

        A right-shaped body proves the model was called, but not that it was
        called with the correct sources at their latest revision. Each
        response-backed ledger record names the sources it covers; the model
        call that produced its visible reply must have observed the current
        marker of every one of those sources. A wrong-source body, a pre-edit
        body, or a coalesced body missing one source's current marker fails
        here. A completed no-response turn requires no marker.

        Only *replayable* sources carry a required marker. A source that was
        durably redacted is tombstoned: production deliberately refuses to
        regenerate an edit against it (``edit_regenerator.py`` ignores edits to
        redacted sources), so a record may keep its already-visible response
        while one covered source no longer feeds model replay. Requiring the
        redacted source's post-redaction edit marker would demand behavior
        production correctly declines. The harness-authored redaction set is
        the independent authority here: production's own tombstone fields are
        checked against it and cannot waive a marker by themselves.
        """
        assert self.ledger_path is not None
        if records is None:
            records = read_ledger_records(self.ledger_path, strict=True)
        expected_sources = self.oracle.expected_sources
        problems: list[str] = []
        harness_redacted = set(redacted_source_event_ids)
        problems.extend(
            self._ledger_redaction_problems(
                records,
                harness_redacted,
                set(expected_sources),
            ),
        )
        for source_event_id, record in records.items():
            if record.response_event_id is None:
                continue
            covered_sources = set(record.source_event_ids) & set(expected_sources)
            live_sources = covered_sources - harness_redacted
            body = self._latest_agent_body(events, record.response_event_id)
            call_id = _body_call_id(body)
            if call_id is None and (
                source_event_id in self.supersession_proofs or source_event_id in self.recovery_proofs
            ):
                # A terminal interrupted placeholder never published model output.
                # Its exact proof closes semantic debt without claiming generation.
                continue
            observed = self.observed_markers_for(call_id) if call_id is not None else frozenset()
            required_live = {
                (
                    self._historical_source_marker(covered, record, call_id, events, redacted_targets or {})
                    if self.source_current_markers[covered] not in observed and call_id is not None
                    else None
                )
                or self.source_current_markers[covered]
                for covered in live_sources
                if covered in self.source_current_markers
            }
            redacted_markers = {
                marker
                for covered in covered_sources & harness_redacted
                for marker in (
                    _source_marker(expected_sources[covered], ORIGINAL_REVISION),
                    *self.source_revision_markers.get(covered, {}).values(),
                )
            }
            missing = required_live - observed
            unexpected = observed - required_live - redacted_markers
            if missing or unexpected:
                problems.append(
                    f"turn for {expected_sources.get(source_event_id, source_event_id)} "
                    f"({source_event_id}) generated without current source markers "
                    f"{sorted(missing)} or with unexpected source markers "
                    f"{sorted(unexpected)}; model saw {sorted(observed)}",
                )
        if problems:
            msg = f"model source-revision audit failed: {problems}"
            raise AssertionError(msg)

    def _historical_source_marker(
        self,
        source_id: str,
        record: TurnRecord,
        call_id: int,
        events: Mapping[str, Mapping[str, Any]],
        redacted_targets: Mapping[str, str],
    ) -> str | None:
        """Allow one frozen generation only with exact terminal removed-revision ownership."""
        if (
            source_id in redacted_targets
            or source_id not in record.source_event_ids
            or not record.completed
            or record.response_event_id not in events
            or self.pending_edit_markers.get(source_id)
        ):
            return None
        revisions = self.source_revision_markers.get(source_id, {})
        surviving = set(revisions) - set(redacted_targets)
        for edit_id, evidence in self.redacted_edit_evidence.items():
            revision = (record.revision_replay or {}).get(edit_id)
            if (
                edit_id not in redacted_targets
                or edit_id not in events
                or evidence.edit_event_id != edit_id
                or evidence.source_event_id != source_id
                or evidence.marker != revisions.get(edit_id)
                or not _historical_call_observed(
                    evidence,
                    call_id,
                    agent_id=self.agent_id,
                    runtime_redaction_path=self.runtime_redaction_path,
                )
                or evidence.marker not in self.observed_markers_for(call_id)
                or revision is None
                or revision.source_event_id != source_id
                or revision.response_event_id != record.response_event_id
                or surviving - evidence.known_edit_event_ids
            ):
                continue
            removed_order = _replacement_order(edit_id, events[edit_id].get("origin_server_ts"), is_edit=True)
            if any(
                live_id not in events
                or _replacement_order(live_id, events[live_id].get("origin_server_ts"), is_edit=True) > removed_order
                for live_id in surviving
            ):
                continue
            return evidence.marker
        return None

    def _assert_direct_reply_model_sources(
        self,
        events: Mapping[str, Mapping[str, Any]],
        replies: Mapping[str, set[str]],
    ) -> None:
        """Bind every ledger-free direct reply to its one exact source marker."""
        problems: list[str] = []
        for source_event_id, logical_ref in self.oracle.expected_sources.items():
            expected_marker = self.source_current_markers.get(source_event_id)
            if expected_marker is None:
                problems.append(f"{logical_ref} ({source_event_id}) has no retained source marker")
                continue
            for reply_event_id in replies.get(source_event_id, ()):
                body = self._latest_agent_body(events, reply_event_id)
                call_id = _body_call_id(body)
                observed = self.observed_markers_for(call_id) if call_id is not None else frozenset()
                if observed != {expected_marker}:
                    problems.append(
                        f"{logical_ref} ({source_event_id}) reply {reply_event_id} "
                        f"expected exactly {expected_marker!r}, model saw {sorted(observed)}",
                    )
        if problems:
            msg = f"direct-reply model source audit failed: {problems}"
            raise AssertionError(msg)

    def _assert_final_bodies_complete(
        self,
        events: Mapping[str, Mapping[str, Any]],
        replies: Mapping[str, set[str]],
    ) -> int:
        """Every required reply ends as one exact completed stream or a recovered interruption.

        A restart may terminate a stream into a visible interrupted note by
        design, with exact auto-resume recovery or independently proven deliberate
        supersession. Superseded interruptions are counted separately from
        completed generation; unfinished partial streams still fail.
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
                proof = self.supersession_proofs.get(source_event_id)
                if proof is not None and proof.interrupted_response_event_id == reply_event_id:
                    continue
                recovery = self.recovery_proofs.get(source_event_id)
                if recovery is not None and recovery.interrupted_response_event_id == reply_event_id:
                    continue
                body = self._latest_agent_body(events, reply_event_id)
                call_id = _body_call_id(body)
                if call_id is not None and body == self.expected_body_for(call_id):
                    checked += 1
                    continue
                problems.append(
                    f"reply to {logical_ref} ended with a non-canonical body: {body[:120]!r}",
                )
        if problems:
            msg = f"final response body audit failed: {problems}"
            raise AssertionError(msg)
        return checked

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
        return _canonical_response_view(events, reply_event_id, self.agent_id).body

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
        root_fanout: int = DEFAULT_ROOT_FANOUT,
    ) -> None:
        self.stack = stack
        self.clients = clients
        self.client = clients[0]
        self.scenario = scenario
        self.reply_timeout = reply_timeout
        self.settle_seconds = settle_seconds
        self.pending_grace = pending_grace
        self._journal = journal
        self.root_fanout = root_fanout
        self.latency = TurnLatencyMonitor()
        self.oracle = ExactReplyOracle(
            self.client,
            stack.agent_id,
            internal_relay_senders=(stack.router_id,),
            coalescing_threads=scenario.profile == "chaos",
            ledger_path=stack.storage_path / "tracking" / "event_journal.db",
            expected_body_for=_ModelHandler.response_text_for,
        )
        self.event_ids: dict[str, str] = {}
        self.sent_payloads: dict[str, _SentPayload] = {}
        self.sent_records: list[_SentRecord] = []
        # Share the growing authored history without restoring redacted wire content.
        self.oracle.sent_records = self.sent_records
        # Maps every redacted event to the redaction event that removed it.
        # The final audit requires both the redacted shell and its exact
        # ``unsigned.redacted_because`` provenance.
        self.redacted_targets: dict[str, str] = {}
        self.redacted_edit_evidence: dict[str, RedactedEditEvidence] = {}
        self.runtime_redaction_path = stack.runtime_redaction_path
        self._cleanup_probe_targets: dict[str, tuple[str, ...]] = {}
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
        # Mutation sends complete before MindRoom's asynchronous regeneration
        # and redaction cleanup. Keep every unresolved physical edit until
        # exact terminal consumption supersedes it, or its own redaction lands.
        self._pending_edit_markers: dict[str, dict[str, str]] = {}
        self.oracle.log_path = stack.log_path
        self.oracle.source_current_markers = self.source_current_markers
        self.oracle.pending_edit_markers = self._pending_edit_markers
        self._pending_source_tombstones: set[str] = set()
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
        # The stack starts MindRoom before the runner exists, so every profile
        # begins with current-generation startup maintenance still owed.
        self._startup_maintenance_pending = True
        self.crash_count = 0
        self.interruptions_with_work_outstanding = 0
        self.executed_batches = 0
        self.slow_wait_extensions = 0

    async def run(self) -> dict[str, object]:
        """Execute one workload and audit its final visible state."""
        if self.scenario.profile == "sustained-stream-capacity":
            return {**await self._run_sustained_stream_capacity()}
        await asyncio.gather(*(client.register() for client in self.clients))
        await asyncio.gather(*(client.join_room() for client in self.clients))
        if self.scenario.profile == "restart-regression":
            return {**await self._run_restart_regression()}
        await self.oracle.initialize()
        await self._await_room_baselines()
        if self.scenario.profile in {"short-stream-correctness", "saturation"}:
            await asyncio.gather(
                *(client.sync_incremental(timeout_ms=0, allow_limited=True) for client in self.clients),
            )
            result = await self._run_short_stream_correctness()
        else:
            await self._await_first_baseline_response()
            await self._send_roots(range(self.scenario.thread_count))
            result = (
                await self._run_chaos()
                if self.scenario.profile == "chaos"
                else await self._run_batches(self.scenario.batches)
            )
        await self._wait_for_restart_recovery_window()
        await self._wait_for_pending_mutation_effects(
            deadline_seconds=self.reply_timeout,
            batch_index=len(self.scenario.batches),
        )
        self.stack.assert_mindroom_running()
        audit_result = await self._audit_final_state()
        self.stack.assert_mindroom_running()
        return {**result, **audit_result}

    async def _authenticate_managed_sender(self) -> None:
        """Select the already-managed load sender without registering a user."""
        credentials = self.stack.agent_matrix_credentials("load_sender")
        if credentials is None:
            msg = f"{self.scenario.profile} managed load_sender credentials are missing"
            raise RuntimeError(msg)
        access_token, _device_id = credentials
        self.client.access_token = access_token
        await self.client.join_room()

    async def _release_managed_roots(
        self,
        *,
        run_id: str,
        deadline: float,
        transaction_prefix: str,
        body_prefix: str,
        launch_barrier: _ManagedRootLaunchBarrier | None = None,
    ) -> tuple[str, ...]:
        """Release every configured mentioned root in one gather."""

        async def send_root(thread: int) -> tuple[int, str]:
            if launch_barrier is not None:
                await launch_barrier.wait_for_release()
            content = {
                "msgtype": "m.text",
                "body": f"{body_prefix} run={run_id} thread={thread} {self.stack.agent_id}",
                "m.mentions": {"user_ids": [self.stack.agent_id]},
            }
            event_id = await self.client.send_event(
                "m.room.message",
                f"{transaction_prefix}-{run_id}-{thread}",
                content,
            )
            return thread, event_id

        async with asyncio.timeout(self._managed_stream_remaining(deadline)):
            released = await asyncio.gather(
                *(send_root(thread) for thread in range(self.scenario.thread_count)),
            )
        roots = sorted(released)
        return tuple(event_id for _thread, event_id in roots)

    async def _release_sustained_stream_capacity_roots(
        self,
        *,
        run_id: str,
        deadline: float,
        health_samples: list[ManagedStreamHealthSample],
    ) -> tuple[str, ...]:
        """Release no-fault roots while proving the managed runtime stays live."""
        launch_barrier = _ManagedRootLaunchBarrier.create(self.scenario.thread_count)
        release_task = asyncio.create_task(
            self._release_managed_roots(
                run_id=run_id,
                deadline=deadline,
                transaction_prefix="sustained-stream-capacity-root",
                body_prefix="Sustained stream capacity",
                launch_barrier=launch_barrier,
            ),
        )
        health_observation_started = asyncio.Event()

        async def observe_initial_health() -> ManagedStreamHealthSample:
            health_observation_started.set()
            return await self._managed_stream_observer_step(
                deadline=deadline,
                health_samples=health_samples,
            )

        initial_health_task: asyncio.Task[ManagedStreamHealthSample] | None = None
        try:
            async with asyncio.timeout(self._managed_stream_remaining(deadline)):
                await launch_barrier.all_entered.wait()
            initial_health_task = asyncio.create_task(observe_initial_health())
            await health_observation_started.wait()
            launch_barrier.release_sends.set()
            await initial_health_task
            while not release_task.done():
                await self._managed_stream_observer_step(
                    deadline=deadline,
                    health_samples=health_samples,
                )
            return await release_task
        finally:
            launch_barrier.release_sends.set()
            cleanup_tasks = (release_task,) if initial_health_task is None else (release_task, initial_health_task)
            for task in cleanup_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    def _managed_stream_audit(
        self,
        *,
        baseline_event_ids: frozenset[str],
        expected_source_ids: Collection[str],
    ) -> ManagedStreamTerminalAudit:
        """Audit the single observer cursor against exact workload sources."""
        return audit_managed_stream_events(
            tuple(event for event_id, event in self.client.seen_events.items() if event_id not in baseline_event_ids),
            responder_id=self.stack.agent_id,
            expected_source_ids=expected_source_ids,
        )

    def _managed_stream_log_counts(self) -> ManagedStreamLogCounts:
        """Read the surviving managed-stream lifecycle counter."""
        return ManagedStreamLogCounts(
            recovery_abandonment_markers=self.stack.log_count(
                "Abandoning",
                self.client.room_id,
            ),
        )

    async def _await_room_baselines(self) -> None:
        """Fence initial traffic after Nio has committed each room's cold history."""
        async with asyncio.timeout(self.reply_timeout):
            while not await asyncio.to_thread(self.stack.managed_room_baseline_ready):
                self.stack.require_runtime_alive()
                await asyncio.sleep(0.1)

    async def _prepare_managed_stream_baseline(self, *, run_id: str) -> ManagedStreamBaseline:
        """Complete one warm turn before snapshotting observer and log state."""
        await self._await_room_baselines()
        await self.client.sync_incremental(timeout_ms=0, allow_limited=True)
        warm_baseline = frozenset(self.client.seen_events)
        warm_event_id = await self.client.send_event(
            "m.room.message",
            f"managed-stream-warm-up-{run_id}",
            self._message_content(f"Managed stream warm up run={run_id}"),
        )
        await self._wait_for_managed_stream_terminals(
            baseline_event_ids=warm_baseline,
            expected_source_ids=(warm_event_id,),
            deadline=time.monotonic() + self.reply_timeout,
            health_samples=[],
        )
        return ManagedStreamBaseline(
            event_ids=frozenset(self.client.seen_events),
            log_counts=self._managed_stream_log_counts(),
        )

    @staticmethod
    def _managed_stream_terminal_ready(audit: ManagedStreamTerminalAudit) -> bool:
        """Return whether every source has one completed canonical response."""
        return (
            audit.canonical_response_count == len(audit.expected_sources)
            and not audit.missing_sources
            and not audit.duplicate_sources
            and not audit.unexpected_sources
            and not audit.invalid_relations
            and not audit.invalid_replacements
            and not audit.invalid_terminal_transitions
            and not audit.noncompleted_sources
        )

    @staticmethod
    def _managed_stream_assert_no_terminal_corruption(audit: ManagedStreamTerminalAudit) -> None:
        """Fail immediately on evidence that cannot become valid with more sync."""
        failures = []
        if audit.duplicate_sources:
            failures.append(f"duplicate_sources={audit.duplicate_sources}")
        if audit.unexpected_sources:
            failures.append(f"unknown_sources={audit.unexpected_sources}")
        if audit.invalid_relations:
            failures.append(f"invalid_relations={audit.invalid_relations}")
        if audit.invalid_replacements:
            failures.append(f"invalid_replacements={audit.invalid_replacements}")
        repeated_terminal_transitions = tuple(
            transition for transition in audit.invalid_terminal_transitions if transition[1] > 1
        )
        if repeated_terminal_transitions:
            failures.append(f"invalid_terminal_transitions={repeated_terminal_transitions}")
        if failures:
            raise AssertionError("managed-stream terminal corruption: " + "; ".join(failures))

    @staticmethod
    def _managed_stream_remaining(deadline: float) -> float:
        """Return the remaining fixed SLA without extending it."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            msg = "managed-stream fixed service-level deadline expired"
            raise TimeoutError(msg)
        return remaining

    async def _managed_stream_observer_step(
        self,
        *,
        deadline: float,
        health_samples: list[ManagedStreamHealthSample],
    ) -> ManagedStreamHealthSample:
        """Sample runtime health and advance the one strict raw-event cursor."""
        remaining = self._managed_stream_remaining(deadline)
        async with asyncio.timeout(remaining):
            self.stack.require_runtime_alive()
            sample = await asyncio.to_thread(self.stack.managed_stream_health_sample)
            health_samples.append(sample)
            await self.client.sync_incremental_complete(
                timeout_ms=min(max(round(remaining * 1000), 0), 250),
            )
            self.stack.require_runtime_alive()
        return sample

    async def _wait_for_managed_stream_terminals(
        self,
        *,
        baseline_event_ids: frozenset[str],
        expected_source_ids: Collection[str],
        deadline: float,
        health_samples: list[ManagedStreamHealthSample],
    ) -> ManagedStreamTerminalAudit:
        """Wait under one absolute deadline for exact completed responses."""
        while True:
            audit = self._managed_stream_audit(
                baseline_event_ids=baseline_event_ids,
                expected_source_ids=expected_source_ids,
            )
            self._managed_stream_assert_no_terminal_corruption(audit)
            if self._managed_stream_terminal_ready(audit):
                return audit
            await self._managed_stream_observer_step(
                deadline=deadline,
                health_samples=health_samples,
            )

    async def _wait_for_managed_stream_drain(
        self,
        *,
        baseline_event_ids: frozenset[str],
        expected_source_ids: Collection[str],
        deadline: float,
        health_samples: list[ManagedStreamHealthSample],
    ) -> ManagedStreamDrainCounts:
        """Wait for exact durable drain while continuing strict observation."""
        while True:
            async with asyncio.timeout(self._managed_stream_remaining(deadline)):
                counts = await asyncio.to_thread(self.stack.managed_stream_drain_counts)
            audit = self._managed_stream_audit(
                baseline_event_ids=baseline_event_ids,
                expected_source_ids=expected_source_ids,
            )
            self._managed_stream_assert_no_terminal_corruption(audit)
            if counts == ManagedStreamDrainCounts(0, 0):
                return counts
            await self._managed_stream_observer_step(
                deadline=deadline,
                health_samples=health_samples,
            )

    async def _wait_for_managed_stream_fence(
        self,
        *,
        target_event_id: str,
        run_id: str,
        deadline: float,
        health_samples: list[ManagedStreamHealthSample],
    ) -> tuple[bool, datetime, datetime]:
        """Require one exact settled reaction and later aggregated sync time."""
        pre_fence_sample = await self._managed_stream_observer_step(
            deadline=deadline,
            health_samples=health_samples,
        )
        if pre_fence_sample.last_sync_time is None:
            msg = "managed-stream pre-fence health omitted last_sync_time"
            raise AssertionError(msg)
        async with asyncio.timeout(self._managed_stream_remaining(deadline)):
            reaction_event_id = await self.client.send_event(
                "m.reaction",
                f"managed-stream-fence-{run_id}",
                {
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": target_event_id,
                        "key": "managed-stream-fence",
                    },
                },
            )
        while True:
            async with asyncio.timeout(self._managed_stream_remaining(deadline)):
                reaction_settled = (
                    await asyncio.to_thread(
                        self.stack.managed_stream_reaction_state,
                        reaction_event_id,
                    )
                    == "settled"
                )
            sample = await self._managed_stream_observer_step(
                deadline=deadline,
                health_samples=health_samples,
            )
            sync_advanced = (
                sample.last_sync_time is not None and sample.last_sync_time > pre_fence_sample.last_sync_time
            )
            if reaction_settled and sync_advanced:
                assert sample.last_sync_time is not None
                return True, pre_fence_sample.last_sync_time, sample.last_sync_time

    def _sustained_stream_capacity_source_audit(
        self,
        *,
        baseline_event_ids: frozenset[str],
        expected_source_ids: Collection[str],
        run_id: str,
    ) -> SustainedStreamCapacitySourceAudit:
        """Audit raw workload roots against the exact managed sender and run markers."""
        return audit_sustained_stream_capacity_sources(
            tuple(event for event_id, event in self.client.seen_events.items() if event_id not in baseline_event_ids),
            expected_source_ids=expected_source_ids,
            load_sender_id=self.stack.load_sender_id,
            responder_id=self.stack.agent_id,
            run_id=run_id,
        )

    def _sustained_stream_capacity_pass_result(
        self,
        observation: SustainedStreamCapacityObservation,
    ) -> dict[str, float | int | str]:
        """Render self-evidencing no-fault capacity evidence as JSON scalars."""
        audit = observation.terminal_audit
        source_audit = observation.source_audit
        drain = observation.durable_drain
        result: dict[str, float | int | str] = {
            "profile": self.scenario.profile,
            "status": "PASS",
            "roots": observation.root_count,
            "observed_root_sources": len(source_audit.observed_source_ids),
            "canonical_agent_replies": audit.canonical_response_count,
            "min_active_stream_seconds": round(audit.min_active_stream_seconds, 3),
            "max_active_stream_seconds": round(audit.max_active_stream_seconds, 3),
            "full_overlap_seconds": round(audit.full_overlap_seconds, 3),
            "peak_active_streams": audit.peak_active_streams,
            "pending_journal_rows": drain.pending_journal_rows if drain is not None else 0,
            "unacknowledged_outbox_rows": drain.unacknowledged_outbox_rows if drain is not None else 0,
            "health_checks": len(observation.health_samples),
            "health_samples_while_root_release": observation.health_samples_while_root_release,
            "recovery_abandonment_markers": observation.recovery_abandonment_markers,
            "watchdog_stalls": observation.watchdog_stalls,
            "durable_drain_failure_markers": observation.durable_drain_failure_markers,
            "reaction_settled": observation.reaction_settled,
            "post_load_sync_advanced": (
                observation.pre_fence_last_sync is not None
                and observation.post_fence_last_sync is not None
                and observation.post_fence_last_sync > observation.pre_fence_last_sync
            ),
            "clean_shutdown": observation.clean_shutdown,
        }
        result.update(
            {f"phase_{phase}_seconds": round(duration, 3) for phase, duration in observation.phase_durations},
        )
        return result

    async def _run_sustained_stream_capacity(self) -> dict[str, float | int | str]:
        """Exercise 200 ordinary overlapping streams under one fixed no-fault SLA."""
        await self._authenticate_managed_sender()
        run_id = secrets.token_hex(6)
        baseline = await self._prepare_managed_stream_baseline(run_id=run_id)
        watchdog_stalls_before = self.stack.log_count("matrix_sync_watchdog_stalled")
        durable_drain_failure_markers_before = self.stack.restart_shutdown_failure_count()

        deadline = time.monotonic() + self.reply_timeout
        phase_started = time.monotonic()
        health_samples: list[ManagedStreamHealthSample] = []
        root_release_health_sample_baseline = len(health_samples)
        source_event_ids = await self._release_sustained_stream_capacity_roots(
            run_id=run_id,
            deadline=deadline,
            health_samples=health_samples,
        )
        health_samples_while_root_release = len(health_samples) - root_release_health_sample_baseline
        await self._managed_stream_observer_step(
            deadline=deadline,
            health_samples=health_samples,
        )
        phase_durations = [("root_release", time.monotonic() - phase_started)]

        phase_started = time.monotonic()
        terminal_audit = await self._wait_for_managed_stream_terminals(
            baseline_event_ids=baseline.event_ids,
            expected_source_ids=source_event_ids,
            deadline=deadline,
            health_samples=health_samples,
        )
        phase_durations.append(("terminal_settlement", time.monotonic() - phase_started))

        phase_started = time.monotonic()
        durable_drain = await self._wait_for_managed_stream_drain(
            baseline_event_ids=baseline.event_ids,
            expected_source_ids=source_event_ids,
            deadline=deadline,
            health_samples=health_samples,
        )
        phase_durations.append(("durable_drain", time.monotonic() - phase_started))

        phase_started = time.monotonic()
        reaction_settled, pre_fence_last_sync, post_fence_last_sync = await self._wait_for_managed_stream_fence(
            target_event_id=terminal_audit.canonical_responses[0][1],
            run_id=run_id,
            deadline=deadline,
            health_samples=health_samples,
        )
        phase_durations.append(("reaction_fence", time.monotonic() - phase_started))

        phase_started = time.monotonic()
        durable_drain = await self._wait_for_managed_stream_drain(
            baseline_event_ids=baseline.event_ids,
            expected_source_ids=source_event_ids,
            deadline=deadline,
            health_samples=health_samples,
        )
        terminal_audit = self._managed_stream_audit(
            baseline_event_ids=baseline.event_ids,
            expected_source_ids=source_event_ids,
        )
        source_audit = self._sustained_stream_capacity_source_audit(
            baseline_event_ids=baseline.event_ids,
            expected_source_ids=source_event_ids,
            run_id=run_id,
        )
        phase_durations.append(("final_audit", time.monotonic() - phase_started))

        shutdown_started = time.monotonic()
        shutdown_remaining = self._managed_stream_remaining(deadline)
        async with asyncio.timeout(shutdown_remaining):
            clean_shutdown = await asyncio.to_thread(
                self.stack.stop_mindroom,
                timeout=min(20.0, shutdown_remaining),
            )
        phase_durations.append(("shutdown", time.monotonic() - shutdown_started))

        final_logs = self._managed_stream_log_counts()
        observation = SustainedStreamCapacityObservation(
            root_count=self.scenario.thread_count,
            source_audit=source_audit,
            terminal_audit=terminal_audit,
            health_samples=tuple(health_samples),
            health_samples_while_root_release=health_samples_while_root_release,
            durable_drain=durable_drain,
            recovery_abandonment_markers=(
                final_logs.recovery_abandonment_markers - baseline.log_counts.recovery_abandonment_markers
            ),
            watchdog_stalls=self.stack.log_count("matrix_sync_watchdog_stalled") - watchdog_stalls_before,
            durable_drain_failure_markers=(
                self.stack.restart_shutdown_failure_count() - durable_drain_failure_markers_before
            ),
            reaction_settled=reaction_settled,
            pre_fence_last_sync=pre_fence_last_sync,
            post_fence_last_sync=post_fence_last_sync,
            clean_shutdown=clean_shutdown,
            phase_durations=tuple(phase_durations),
        )
        failures = evaluate_sustained_stream_capacity(observation)
        if failures:
            raise AssertionError("sustained-stream-capacity acceptance failures:\n" + "\n".join(failures))
        return self._sustained_stream_capacity_pass_result(observation)

    async def _run_restart_regression(self) -> dict[str, int | str]:
        """Exercise real replacement recovery while observing only Matrix output."""
        dormant = self.client
        await dormant.create_public_room()
        historical_text = await dormant.send_event(
            "m.room.message",
            "restart-old-text",
            self._message_content("Synthetic historical text"),
        )
        historical_media = await dormant.send_event(
            "m.room.message",
            "restart-old-media",
            {
                "body": "Synthetic historical audio",
                "info": {"mimetype": "audio/ogg", "size": 1},
                "m.mentions": {"user_ids": [self.stack.agent_id]},
                "msgtype": "m.audio",
                "url": "mxc://localhost/synthetic",
            },
        )
        lifecycle_markers = (
            (
                "matrix_agent_response_runtime_shutdown",
                f"agent={AGENT_NAME}",
                "restart_reason_category=config_reload",
            ),
            (
                "matrix_agent_response_runtime_shutdown",
                f"agent={ROUTER_NAME}",
                "restart_reason_category=config_reload",
            ),
            ("agent_setup_complete", self.stack.agent_id),
            ("agent_setup_complete", self.stack.router_id),
            ("configuration_update_complete",),
        )
        lifecycle_counts = tuple(self.stack.log_count(*markers) for markers in lifecycle_markers)
        self.stack.apply_replacement_config(dormant.room_id)
        lifecycle_results = await asyncio.gather(
            *(
                asyncio.to_thread(
                    self.stack.wait_for_log_count,
                    markers,
                    count + 1,
                    timeout=self.reply_timeout,
                )
                for markers, count in zip(lifecycle_markers, lifecycle_counts, strict=True)
            ),
        )
        replacement_boundary_reached = all(lifecycle_results)
        _require_restart_invariant(
            replacement_boundary_reached,
            "replacement_setup_boundary_reached",
            event_category="lifecycle",
            phase="reload",
            observed=replacement_boundary_reached,
            step=3,
        )
        # The fresh event is released with no wait for the historical ones.
        # Hydration is lazy and per-conversation: nothing fetches this room's
        # history until something reads it, so there is no moment where the
        # history is durably present and the fresh event has not yet been
        # released, and waiting for one hangs until the deadline. The same
        # ground is covered after the answer by an explicit room read, which is
        # also the stronger claim: the history is not lost, and it appears when
        # something asks.
        historical_event_ids = (historical_text, historical_media)
        fresh = await dormant.send_event(
            "m.room.message",
            "restart-fresh",
            self._message_content(FRESH_RESTART_REQUEST),
        )
        callback_markers = _semantic_ingress_markers(
            agent=AGENT_NAME,
            room_id=dormant.room_id,
            event_id=fresh,
        )
        callback_accepted = await asyncio.to_thread(
            self.stack.wait_for_log_count,
            callback_markers,
            1,
            timeout=self.reply_timeout,
        )
        _require_restart_invariant(
            callback_accepted,
            "fresh_callback_accepted_before_restart",
            event_category="fresh_user",
            phase="pre_restart",
            observed=callback_accepted,
            step=4,
        )
        obligation_unsettled = await asyncio.to_thread(
            self.stack.wait_for_restart_journal_event_state,
            fresh,
            expected=frozenset({"pending"}),
            timeout=self.reply_timeout,
        )
        _require_restart_invariant(
            obligation_unsettled,
            "fresh_dispatch_obligation_unsettled_before_restart",
            event_category="fresh_user",
            phase="pre_restart",
            observed=obligation_unsettled,
            step=4,
        )
        request_in_flight = await asyncio.to_thread(
            self.stack.wait_for_blocked_restart_request,
            timeout=self.reply_timeout,
        )
        _require_restart_invariant(
            request_in_flight,
            "fresh_model_request_in_flight_before_restart",
            event_category="fresh_user",
            phase="pre_restart",
            observed=request_in_flight,
            step=4,
        )
        fresh_semantic_ingress_count_before_restart = self.stack.log_count(*callback_markers)
        _require_restart_invariant(
            fresh_semantic_ingress_count_before_restart == 1,
            "fresh_semantic_ingress_before_restart_exactly_once",
            event_category="fresh_user",
            phase="pre_restart",
            observed=fresh_semantic_ingress_count_before_restart,
            step=4,
        )
        fresh_response_checkpointed = await asyncio.to_thread(
            self.stack.wait_for_restart_event_checkpoint,
            dormant.room_id,
            fresh,
            timeout=self.reply_timeout,
        )
        _require_restart_invariant(
            fresh_response_checkpointed,
            "fresh_sync_checkpoint_advanced_before_restart",
            event_category="fresh_user",
            phase="pre_restart",
            observed=fresh_response_checkpointed,
            step=4,
        )

        recovery_markers = (
            ("agent_setup_complete", self.stack.agent_id),
            ("agent_setup_complete", self.stack.router_id),
        )
        recovery_counts = tuple(self.stack.log_count(*markers) for markers in recovery_markers)
        await asyncio.to_thread(
            self.stack.restart_mindroom_for_recovery,
            timeout=self.reply_timeout,
        )
        recovery_results = await asyncio.gather(
            *(
                asyncio.to_thread(
                    self.stack.wait_for_log_count,
                    markers,
                    count + 1,
                    timeout=self.reply_timeout,
                )
                for markers, count in zip(recovery_markers, recovery_counts, strict=True)
            ),
        )
        recovery_boundary_reached = all(recovery_results)
        _require_restart_invariant(
            recovery_boundary_reached,
            "recovery_setup_boundary_reached",
            event_category="lifecycle",
            phase="hard_restart",
            observed=recovery_boundary_reached,
            step=4,
        )

        observation = await self._wait_for_restart_observation(
            dormant,
            historical_event_ids=historical_event_ids,
            fresh_event_id=fresh,
            fresh_semantic_ingress_count_before_restart=fresh_semantic_ingress_count_before_restart,
        )
        observation = replace(
            observation,
            historical_projected_on_room_read=await self._read_historical_room_projection(
                room_id=dormant.room_id,
                historical_event_ids=historical_event_ids,
            ),
        )
        failures = evaluate_restart_regression(observation)
        if failures:
            _raise_restart_failures(failures)
        return {
            "historical_events_projected_after_answer": observation.projected_after_answer_count,
            "historical_events_projected_on_room_read": observation.historical_projected_on_room_read,
            "historical_outputs": sum(observation.historical_output_counts),
            "profile": self.scenario.profile,
            "status": "PASS",
        }

    def _collect_restart_observation(
        self,
        dormant: LiveMatrixClient,
        *,
        historical_event_ids: tuple[str, str],
        fresh_event_id: str,
        fresh_semantic_ingress_count_before_restart: int,
        orderly_drain_completed: bool | None,
    ) -> RestartRegressionObservation:
        """Collect one definitionally consistent restart evidence snapshot."""
        events = tuple(dormant.seen_events.values())
        log = self.stack.read_log()
        agent = self._canonical_response_ids(events)
        router = self._canonical_response_ids(events, sender_id=self.stack.router_id)
        historical_text_id, historical_media_id = historical_event_ids
        historical_output_counts = (
            self._combined_response_count(historical_text_id, agent, router),
            self._combined_response_count(historical_media_id, agent, router),
        )
        historical_callback_counts = (
            _log_count(
                log,
                "matrix_event_callback_started",
                f"room_id={dormant.room_id}",
                f"event_id={historical_text_id}",
            ),
            _log_count(
                log,
                "matrix_event_callback_started",
                f"room_id={dormant.room_id}",
                f"event_id={historical_media_id}",
            ),
        )
        projected_after_answer_count = self.stack.projected_restart_event_pair_count(
            dormant.room_id,
            historical_event_ids,
        )
        fresh_prompt_observed, historical_in_fresh_prompt = _restart_prompt_observation(
            log,
            fresh_event_id,
            historical_event_ids,
        )
        fresh_agent_response_ids = self._restart_response_ids(
            events,
            agent,
            fresh_event_id,
            room_id=dormant.room_id,
        )
        fresh_router_response_ids = router.get(fresh_event_id, set())
        fresh_response_bodies = tuple(
            self._latest_event_body(events, response_id) for response_id in sorted(fresh_agent_response_ids)
        )
        fresh_response_body = fresh_response_bodies[0] if len(fresh_response_bodies) == 1 else ""
        fresh_response_complete = (
            len(fresh_agent_response_ids) == 1 and not fresh_router_response_ids and "END call=" in fresh_response_body
        )
        return RestartRegressionObservation(
            historical_output_counts=historical_output_counts,
            historical_callback_counts=historical_callback_counts,
            fresh_agent_output_count=len(fresh_agent_response_ids),
            fresh_router_output_count=len(fresh_router_response_ids),
            fresh_response_complete=fresh_response_complete,
            fresh_semantic_ingress_count_before_restart=fresh_semantic_ingress_count_before_restart,
            fresh_semantic_ingress_count=_log_count(
                log,
                *_semantic_ingress_markers(
                    agent=AGENT_NAME,
                    room_id=dormant.room_id,
                    event_id=fresh_event_id,
                ),
            ),
            recovered_generation_response_observed=(
                bool(fresh_response_bodies)
                and all(RECOVERED_RUNTIME_GENERATION_MARKER in body for body in fresh_response_bodies)
            ),
            # Settled, which after recovery means the obligation was picked up
            # and finished. The journal no longer records *why* it finished --
            # nothing production reads that -- so whether the turn answered is
            # asserted by `recovered_generation_response_observed` and the
            # fresh-output count instead, which measure the visible reply
            # rather than a claim about it.
            fresh_obligation_recovered=(self.stack.restart_journal_event_state(fresh_event_id) == "settled"),
            projected_after_answer_count=projected_after_answer_count,
            # Filled in by the runner once every other observation is safely
            # made, because reading a conversation hydrates it.
            historical_projected_on_room_read=0,
            fresh_prompt_observed=fresh_prompt_observed,
            historical_in_fresh_prompt=historical_in_fresh_prompt,
            orderly_drain_completed=orderly_drain_completed,
        )

    async def _read_historical_room_projection(
        self,
        *,
        room_id: str,
        historical_event_ids: tuple[str, str],
    ) -> int:
        """Read the room conversation as the agent and count the historical messages.

        The point of this assertion is that it is a read. Hydration is lazy and
        per-conversation, so answering a turn in a thread does not project the
        room's main timeline, and demanding that it did would be demanding the
        eager back-fill this design removed. What must hold is weaker and more
        useful: the history is not lost, and it appears when something asks.

        Asking has to happen after every other observation, because hydration
        writes to the projection, and a read that ran earlier would manufacture
        the very evidence the earlier invariants are meant to find on their own.
        """
        # Imported here so the harness's module import stays free of nio and the
        # MindRoom runtime, which it otherwise never needs.
        from types import SimpleNamespace  # noqa: PLC0415

        import nio  # noqa: PLC0415

        from mindroom.event_journal import EventJournalStore  # noqa: PLC0415
        from mindroom.matrix.conversation_hydration import ConversationHydrator  # noqa: PLC0415

        credentials = self.stack.agent_matrix_credentials()
        if credentials is None:
            return 0
        access_token, device_id = credentials
        client = nio.AsyncClient(self.stack.homeserver, self.stack.agent_id)
        client.access_token = access_token
        client.user_id = self.stack.agent_id
        client.device_id = device_id
        try:
            store = EventJournalStore.open_sqlite(
                self.stack.storage_path / "tracking" / "event_journal.db",
            ).principal(f"{AGENT_NAME}@{self.stack.agent_id}")
            hydrator = ConversationHydrator(
                store=store,
                runtime=cast("Any", SimpleNamespace(client=client)),
                self_sender=self.stack.agent_id,
            )
            await hydrator.ensure_hydrated(room_id=room_id, thread_id=None)
            page = await store.read_conversation(room_id=room_id, thread_id=None, limit=100)
        finally:
            await client.close()
        projected = {message.logical_event_id for message in page.messages}
        return sum(event_id in projected for event_id in historical_event_ids)

    async def _wait_for_restart_observation(
        self,
        dormant: LiveMatrixClient,
        *,
        historical_event_ids: tuple[str, str],
        fresh_event_id: str,
        fresh_semantic_ingress_count_before_restart: int,
    ) -> RestartRegressionObservation:
        """Observe replacement output until the fresh response and callback stream settle."""
        deadline = time.monotonic() + self.reply_timeout
        observation = self._collect_restart_observation(
            dormant,
            historical_event_ids=historical_event_ids,
            fresh_event_id=fresh_event_id,
            fresh_semantic_ingress_count_before_restart=fresh_semantic_ingress_count_before_restart,
            orderly_drain_completed=None,
        )

        while not _positive_restart_evidence_ready(observation) and time.monotonic() < deadline:
            await dormant.sync_incremental(timeout_ms=250, allow_limited=True)
            observation = self._collect_restart_observation(
                dormant,
                historical_event_ids=historical_event_ids,
                fresh_event_id=fresh_event_id,
                fresh_semantic_ingress_count_before_restart=fresh_semantic_ingress_count_before_restart,
                orderly_drain_completed=None,
            )

        if _positive_restart_evidence_ready(observation):
            shutdown_failure_count_before = self.stack.restart_shutdown_failure_count()
            stopped_gracefully = await asyncio.to_thread(
                self.stack.stop_mindroom,
                timeout=self.reply_timeout,
            )
            orderly_drain_completed = (
                stopped_gracefully
                and shutdown_failure_count_before == 0
                and self.stack.restart_shutdown_failure_count() == 0
            )
            await dormant.sync_incremental(
                timeout_ms=max(round(self.settle_seconds * 1000), 0),
                allow_limited=True,
            )
            observation = self._collect_restart_observation(
                dormant,
                historical_event_ids=historical_event_ids,
                fresh_event_id=fresh_event_id,
                fresh_semantic_ingress_count_before_restart=fresh_semantic_ingress_count_before_restart,
                orderly_drain_completed=orderly_drain_completed,
            )

        return observation

    async def _run_short_stream_correctness(self) -> dict[str, int | str]:
        """Run hot and parallel turns without cross-thread barriers."""
        parallel_start = self._short_stream_parallel_start()
        expected_sources: set[str] = set()

        hot_root, hot_response = await self._short_stream_turn(
            self.clients[0],
            label="hot-root",
            thread=0,
            client_index=0,
            thread_root=None,
            reply_to=None,
            expected_sources=expected_sources,
        )
        for batch in self.scenario.batches[:parallel_start]:
            operation = batch[0]
            _, hot_response = await self._short_stream_turn(
                self.clients[0],
                label=operation.event_ref,
                thread=0,
                client_index=0,
                thread_root=hot_root,
                reply_to=hot_response,
                expected_sources=expected_sources,
            )
            self.operation_count += 1
            self.executed_batches += 1

        parallel_batches = self.scenario.batches[parallel_start:]

        async def run_parallel_thread(thread: int) -> None:
            client = self._client_for_thread(thread)
            root, response = await self._short_stream_turn(
                client,
                label=f"root:{thread}",
                thread=thread,
                client_index=thread - 1,
                thread_root=None,
                reply_to=None,
                expected_sources=expected_sources,
            )
            for batch in parallel_batches:
                operation = next(item for item in batch if item.thread == thread)
                _, response = await self._short_stream_turn(
                    client,
                    label=operation.event_ref,
                    thread=thread,
                    client_index=thread - 1,
                    thread_root=root,
                    reply_to=response,
                    expected_sources=expected_sources,
                )
                self.operation_count += 1

        await asyncio.gather(
            *(run_parallel_thread(thread) for thread in range(1, self.scenario.thread_count)),
        )
        self.executed_batches += len(parallel_batches)

        await self.oracle.wait_until_exact(
            deadline_seconds=self.reply_timeout,
            settle_seconds=self.settle_seconds,
        )
        # Every sender must observe a complete sync stream through one full
        # quiet window before the canonical `/messages` audit runs. A limited
        # window is repaired from room history before its cursor advances.
        await asyncio.gather(
            *(
                client.wait_until_quiet(
                    deadline_seconds=self.reply_timeout,
                    quiet_seconds=self.settle_seconds,
                )
                for client in self.clients
            ),
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
                "short-stream correctness reply invariant failed: "
                f"duplicates={duplicates}, missing={missing}, unexpected={unexpected}"
            )
            raise AssertionError(msg)

        return {
            "batches": self.executed_batches,
            "canonical_agent_replies": len(expected_sources),
            "operations": self.operation_count,
            "restarts": 0,
            "roots": self.scenario.thread_count,
            "profile": self.scenario.profile,
            "status": "PASS",
        }

    async def _short_stream_turn(
        self,
        client: LiveMatrixClient,
        *,
        label: str,
        thread: int,
        client_index: int,
        thread_root: str | None,
        reply_to: str | None,
        expected_sources: set[str],
    ) -> tuple[str, str]:
        """Send one old-harness turn and wait for its completed stream."""
        source_marker = _source_marker(label, ORIGINAL_REVISION)
        content = self._message_content(
            f"Live short-stream correctness {label}",
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
            marker=source_marker,
        )
        txn_id = f"live-saturation-{label}-{secrets.token_hex(4)}"
        room_id = self._room_for_thread(thread)
        self.oracle.begin_expectation_registration()
        registered = False
        try:
            source_event_id = await client.send_event("m.room.message", txn_id, content, room_id=room_id)
            self.sent_records.append(
                _SentRecord(
                    source_event_id,
                    room_id,
                    "m.room.message",
                    sender=client.user_id,
                    content=content,
                ),
            )
            self.source_current_markers[source_event_id] = source_marker
            self.oracle.expect(
                label,
                source_event_id,
                thread=thread,
                client=client_index,
                sent_at=time.monotonic(),
            )
            registered = True
        finally:
            self.oracle.finish_expectation_registration(validate=registered)
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
        response_ids: set[str] = set()
        while time.monotonic() < deadline:
            # Keep the independent oracle cursor current during saturation
            # instead of risking one limited final sync after the entire burst.
            await self.oracle.pump(timeout_ms=0)
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
            await client.sync_incremental(timeout_ms=1000)
        incremental_events = tuple(client.seen_events.values())
        diagnostic: dict[str, object] = {
            "incremental_response_ids": sorted(response_ids),
            "incremental_views": {
                response_id: _response_view_diagnostic(incremental_events, response_id)
                for response_id in sorted(response_ids)
            },
        }
        try:
            canonical_events = tuple(await client.paginate_room(client.room_id))
            canonical_response_ids = self._canonical_response_ids(
                canonical_events,
                root_event_id=root_event_id,
            ).get(source_event_id, set())
            diagnostic["canonical_response_ids"] = sorted(canonical_response_ids)
            diagnostic["canonical_views"] = {
                response_id: _response_view_diagnostic(canonical_events, response_id)
                for response_id in sorted(canonical_response_ids)
            }
        except Exception as exc:  # A diagnostic read must not replace the timeout.
            diagnostic["canonical_read_error"] = f"{type(exc).__name__}: {exc}"
        msg = f"agent response timeout for {source_event_id}: {json.dumps(diagnostic, sort_keys=True)}"
        raise TimeoutError(msg)

    def _canonical_response_ids(
        self,
        events: Collection[Mapping[str, Any]],
        *,
        root_event_id: str | None = None,
        sender_id: str | None = None,
    ) -> dict[str, set[str]]:
        """Index canonical agent originals by their direct source event."""
        response_ids: dict[str, set[str]] = defaultdict(set)
        expected_sender = self.stack.agent_id if sender_id is None else sender_id
        for event in events:
            if event.get("type") != "m.room.message" or event.get("sender") != expected_sender:
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
    def _combined_response_count(source_event_id: str, *response_indexes: Mapping[str, set[str]]) -> int:
        """Count canonical bot responses across every configured sender."""
        return sum(len(response_ids.get(source_event_id, set())) for response_ids in response_indexes)

    def _restart_response_ids(
        self,
        events: Collection[Mapping[str, Any]],
        agent_responses: Mapping[str, set[str]],
        source_event_id: str,
        *,
        room_id: str,
    ) -> set[str]:
        """Require one direct answer or one exact interrupted-response resume chain."""
        direct_response_ids = set(agent_responses.get(source_event_id, set()))
        # Cardinality is evidence: one relay must never hide several originals.
        if len(direct_response_ids) > 1:
            return direct_response_ids
        # /sync omits room_id; ingestion annotates its enclosing room timeline.
        room_events = tuple(
            event
            for event in events
            if event.get("room_id", room_id) == room_id and event.get("_audit_room_id", room_id) == room_id
        )
        proven_responses = self._canonical_response_ids(room_events, root_event_id=source_event_id)
        if direct_response_ids != proven_responses.get(source_event_id, set()):
            return set()
        relay_response_ids: set[str] = set()
        for event in events:
            event_id = event.get("event_id")
            if not isinstance(event_id, str):
                continue
            relay_target = _auto_resume_relay_target(
                event,
                relay_senders=(self.stack.router_id,),
            )
            if relay_target is not None and relay_target[1] == source_event_id:
                answers = agent_responses.get(event_id, set())
                if not answers:
                    continue
                interrupted_id = relay_target[0]
                if (
                    direct_response_ids != {interrupted_id}
                    or event not in room_events
                    or answers != proven_responses.get(event_id, set())
                    or not _latest_response_body(room_events, interrupted_id, sender_id=self.stack.agent_id).endswith(
                        (INTERRUPTED_RESPONSE_NOTE, RESTART_INTERRUPTED_RESPONSE_NOTE),
                    )
                ):
                    return set()
                relay_response_ids.update(answers)
        if not relay_response_ids:
            return direct_response_ids
        return relay_response_ids

    @staticmethod
    def _latest_event_body(
        events: Collection[Mapping[str, Any]],
        response_event_id: str,
    ) -> str:
        """Return the newest original or edit body for one response."""
        return _latest_response_body(events, response_event_id)

    async def _run_batches(
        self,
        batches: tuple[tuple[LiveOperation, ...], ...],
        *,
        batch_index_offset: int = 0,
    ) -> dict[str, object]:
        """Run one contiguous scenario segment against already-created roots."""
        for relative_batch_index, batch in enumerate(batches):
            batch_index = batch_index_offset + relative_batch_index
            work = tuple(operation for operation in batch if operation.kind not in _INTERRUPTION_KINDS)
            await self._apply_batch_in_completion_order(work, on_complete=self._record_batch_results)
            if len(work) != len(batch):
                self._interrupt_outstanding_work(batch[-1].kind, batch_index)
            try:
                await self._await_replies()
            except AssertionError as exc:
                msg = f"{exc}\nfailure occurred after live batch {batch_index}"
                raise AssertionError(msg) from exc
            self.executed_batches += 1

        self._require_interruptions_landed_mid_turn()
        return {
            "batches": self.executed_batches,
            "canonical_agent_replies": len(self.oracle.expected_sources),
            "operations": self.operation_count,
            "restarts": self.restart_count,
            "crashes": self.crash_count,
            "interruptions_with_work_outstanding": self.interruptions_with_work_outstanding,
            "roots": self.scenario.thread_count,
            "measured_turn_seconds": round(self.latency.per_turn_seconds, 3),
            "slow_wait_extensions": self.slow_wait_extensions,
            "profile": self.scenario.profile,
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
            if operation.kind is LiveOperationKind.REDACTION:
                return await self._apply_redaction(
                    operation,
                    on_landed=record_result,
                )
            result = await self._apply(operation)
            record_result(result)
            return result

        def record_result(
            result: tuple[LiveOperation, str | None, _SentPayload | None],
        ) -> None:
            results.append(result)
            if on_complete is not None:
                on_complete((result,))

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
            **_latency_summary(self.oracle.reply_latencies.values()),
        }

    async def _audit_final_state(self) -> dict[str, int]:
        """Audit every profile through an independent canonical Matrix view."""
        auditor = FinalStateAuditor(
            self.client,
            self.oracle,
            agent_id=self.stack.agent_id,
            expected_body_for=_ModelHandler.response_text_for,
            ledger_path=(
                None
                if self.scenario.profile in {"saturation", "short-stream-correctness"}
                else self.stack.storage_path / "tracking" / "event_journal.db"
            ),
            source_current_markers=self.source_current_markers,
            source_revision_markers=self.source_revision_markers,
            cleanup_probes={
                self.event_ids[operation.event_ref]: tuple(
                    self.event_ids[source] for source in operation.cleanup_sources
                )
                for batch in self.scenario.batches
                for operation in batch
                if operation.cleanup_sources
            },
            observed_cleanup_probes=self._cleanup_probe_targets,
            redacted_edit_evidence=self.redacted_edit_evidence,
            runtime_redaction_path=self.runtime_redaction_path,
            pending_edit_markers=self._pending_edit_markers,
        )
        return await auditor.audit(
            room_ids=tuple(self.stack.room_ids.values()),
            sent_records=self.sent_records,
            redacted_targets=self.redacted_targets,
        )

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
            self._startup_maintenance_pending = True
        elif kind is LiveOperationKind.KILL_RESTART_MINDROOM:
            self.stack.kill_restart_mindroom()
            self.restart_count += 1
            self._startup_maintenance_pending = True
        elif kind is LiveOperationKind.COLD_RESTART_MINDROOM:
            self.stack.cold_restart_mindroom()
            self.restart_count += 1
            self._startup_maintenance_pending = True
        elif kind is LiveOperationKind.RESTART_TUWUNEL:
            self.stack.restart_tuwunel()
            self.tuwunel_restart_count += 1
        elif kind is LiveOperationKind.STOP_MINDROOM:
            if not self.stack.stop_mindroom():
                msg = "MindRoom did not shut down cleanly before its planned outage"
                raise AssertionError(msg)
            self._mindroom_running = False
            self.outage_count += 1
        elif kind is LiveOperationKind.START_MINDROOM:
            self.stack.start_mindroom()
            self._mindroom_running = True
            self._startup_maintenance_pending = True
        else:  # pragma: no cover - validation rejects unknown lifecycle kinds
            msg = f"unsupported lifecycle operation {kind}"
            raise AssertionError(msg)
        # Journal after the state transition so the recorded running flag matches
        # the world the next mutations observe.
        self._record_lifecycle(kind)

    async def _wait_for_restart_recovery_window(self) -> None:
        """Wait for current-generation maintenance and observed Matrix quiet."""
        if not self._startup_maintenance_pending:
            return
        await asyncio.to_thread(
            self.stack.wait_for_startup_maintenance,
            timeout_seconds=self.reply_timeout,
        )
        await self.oracle.wait_until_exact(
            deadline_seconds=self.reply_timeout,
            settle_seconds=self.settle_seconds,
        )
        self._startup_maintenance_pending = False

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
        await self._wait_for_pending_mutation_effects(
            deadline_seconds=deadline_seconds,
            batch_index=batch_index,
        )

    async def _wait_for_pending_mutation_effects(
        self,
        *,
        deadline_seconds: float,
        batch_index: int,
    ) -> None:
        """Wait for owed regeneration markers and durable source tombstones."""
        deadline = time.monotonic() + deadline_seconds
        while self._pending_edit_markers or self._pending_source_tombstones:
            if time.monotonic() >= deadline:
                msg = (
                    f"timed out waiting for mutation effects at chaos checkpoint "
                    f"(batch {batch_index}): markers={self._pending_edit_markers}, "
                    f"current={[self._current_pending_edit_marker(source) for source in self._pending_edit_markers]}, "
                    f"tombstones={sorted(self._pending_source_tombstones)}"
                )
                raise AssertionError(msg)
            await self.oracle.pump(timeout_ms=250)
            self.oracle.refresh_ledger_attributions(min_interval=0.0)
            self._reconcile_edit_debts()
            self._pending_source_tombstones.difference_update(
                source_event_id
                for source_event_id in tuple(self._pending_source_tombstones)
                if _redaction_target_state(
                    source_event_id,
                    self.oracle._ledger_observations,
                    self.source_revision_markers,
                )[0]
            )

    def _current_pending_edit_marker(self, source_id: str) -> str | None:
        """Choose the newest observed live debt by canonical Matrix replacement order."""
        pending = self._pending_edit_markers.get(source_id, {})
        ordered = [
            (_replacement_order(edit, self.oracle.event_summaries[edit].get("origin_server_ts"), is_edit=True), marker)
            for edit, marker in pending.items()
            if edit not in self.redacted_targets and edit in self.oracle.event_summaries
        ]
        return max(ordered)[1] if ordered else None

    def _reconcile_edit_debts(self) -> None:
        """Only exact visible terminal consumption can discharge or supersede live edit debt."""
        for source_id, pending in tuple(self._pending_edit_markers.items()):
            record = self.oracle._ledger_records.get(source_id)
            if record is None or not record.completed or source_id not in record.source_event_ids:
                continue
            if self.oracle.source_tombstoned(source_id) or self.oracle.source_completed_without_response(source_id):
                del self._pending_edit_markers[source_id]
                continue
            latest = self.oracle.latest_reply_bodies.get(record.response_event_id or "")
            call_id = _body_call_id(latest[1]) if latest is not None else None
            if call_id is None:
                continue
            consumed_orders = self._completed_edit_orders(source_id, record, call_id)
            for edit in tuple(pending):
                event = self.oracle.event_summaries.get(edit)
                if event is not None and any(
                    _replacement_order(edit, event.get("origin_server_ts"), is_edit=True) <= consumed
                    and (evidence is None or edit in evidence.known_edit_event_ids)
                    for consumed, evidence in consumed_orders
                ):
                    del pending[edit]
            if not pending:
                del self._pending_edit_markers[source_id]

    def source_matching_snapshot(self) -> dict[str, object]:
        """Keep every historical-proof conjunct beside exact call and entry observations."""
        return {
            "source_current_markers": dict(self.source_current_markers),
            "source_revision_markers": {source: dict(edits) for source, edits in self.source_revision_markers.items()},
            "pending_edit_markers": {source: dict(edits) for source, edits in self._pending_edit_markers.items()},
            "redacted_targets": dict(self.redacted_targets),
            "event_summaries": dict(self.oracle.event_summaries),
            "latest_reply_bodies": dict(self.oracle.latest_reply_bodies),
            "redacted_edit_evidence": {
                edit: {
                    **asdict(evidence),
                    "observed_call_ids": sorted(evidence.observed_call_ids),
                    "known_edit_event_ids": sorted(evidence.known_edit_event_ids),
                }
                for edit, evidence in self.redacted_edit_evidence.items()
            },
        }

    def _completed_edit_orders(
        self,
        source_id: str,
        record: TurnRecord,
        call_id: int,
    ) -> list[tuple[tuple[int, int, str], RedactedEditEvidence | None]]:
        """Deleted consumption additionally needs its frozen call; later authored debt stays live."""
        observed = _ModelHandler.observed_markers_for(call_id)
        consumed: list[tuple[tuple[int, int, str], RedactedEditEvidence | None]] = []
        for edit, revision in (record.revision_replay or {}).items():
            marker = self.source_revision_markers.get(source_id, {}).get(edit)
            if (
                revision.source_event_id != source_id
                or revision.response_event_id != record.response_event_id
                or edit not in self.oracle.event_summaries
                or marker not in observed
            ):
                continue
            evidence = self.redacted_edit_evidence.get(edit) if edit in self.redacted_targets else None
            if edit in self.redacted_targets and (
                evidence is None
                or evidence.source_event_id != source_id
                or evidence.edit_event_id != edit
                or evidence.marker != marker
                or not _historical_call_observed(
                    evidence,
                    call_id,
                    agent_id=self.oracle.agent_id,
                    runtime_redaction_path=self.runtime_redaction_path,
                )
            ):
                continue
            order = _replacement_order(edit, self.oracle.event_summaries[edit].get("origin_server_ts"), is_edit=True)
            consumed.append((order, evidence))
        return consumed

    def _interrupt_outstanding_work(self, kind: LiveOperationKind, batch_index: int) -> None:
        """Take the process down while the journal still owes the batch a turn.

        The batch's writes have left the harness, so the only question is
        whether MindRoom has committed them yet. Waiting for a pending journal
        row before pulling the process out answers it: what dies is a runtime
        holding durable, unfinished obligations, which is the single case the
        journal was built for and the case the harness never used to reach.
        """
        interrupted = self.stack.wait_for_pending_journal_work(timeout=self.reply_timeout)
        if kind is LiveOperationKind.CRASH_MINDROOM:
            self.stack.crash_mindroom()
            self.crash_count += 1
        else:
            self.stack.restart_mindroom()
            self.restart_count += 1
        self._startup_maintenance_pending = True
        self._record_lifecycle(kind)
        self.interruptions_with_work_outstanding += int(interrupted)
        if not interrupted:
            print(
                f"{kind.value} at live batch {batch_index} found no pending journal work within "
                f"{self.reply_timeout:.0f}s and interrupted an idle runtime",
                file=sys.stderr,
                flush=True,
            )

    def _require_interruptions_landed_mid_turn(self) -> None:
        """Fail a run whose restarts and crashes never actually interrupted anything.

        Reporting `restarts: 18` while every one of them hit an idle process is
        the failure this whole apparatus exists to stop: a number that reads
        like crash coverage and is not.
        """
        interruptions = self.restart_count + self.crash_count
        if interruptions and self.interruptions_with_work_outstanding != interruptions:
            missed = interruptions - self.interruptions_with_work_outstanding
            msg = (
                f"{missed} of {interruptions} restarts and crashes found no committed unfinished journal work to "
                "interrupt, so the run did not exercise the recovery it reports as covered"
            )
            raise AssertionError(msg)

    def _short_stream_parallel_start(self) -> int:
        """Return the first batch belonging to the parallel short-stream phase."""
        return next(
            (
                index
                for index, batch in enumerate(self.scenario.batches)
                if any(operation.thread != 0 for operation in batch)
            ),
            len(self.scenario.batches),
        )

    def _client_for_thread(self, thread: int) -> LiveMatrixClient:
        """Use the original multi-sender mapping for short-stream traces."""
        if self.scenario.profile not in {"saturation", "short-stream-correctness"}:
            return self.client
        client_index = max(thread - 1, 0)
        return self.clients[client_index]

    def _client_for_operation(self, operation: LiveOperation) -> LiveMatrixClient:
        """Route one operation through its authored sender."""
        if self.scenario.profile in {"saturation", "short-stream-correctness"}:
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

    def _report_slow_wait(self, notice: SlowWaitNotice) -> None:
        """Say out loud that the machine, not the product, is the bottleneck."""
        self.slow_wait_extensions += 1
        print(notice.render(), file=sys.stderr, flush=True)

    async def _await_replies(self) -> None:
        """Wait out every outstanding reply under a work-derived budget.

        A failure here is reported with the durable position of each missing
        reply, because "one of forty-five is missing" is a count and the next
        reader needs a cause.
        """
        budget = WaitBudget(
            turns=len(self.oracle.outstanding()),
            per_turn_seconds=self.latency.per_turn_seconds,
            settle_seconds=self.settle_seconds,
            floor_seconds=self.reply_timeout,
        )
        try:
            elapsed = await self.oracle.wait_until_exact(
                budget,
                on_slow=self._report_slow_wait,
                liveness=self.stack.require_runtime_alive,
            )
        except ExactReplyTimeoutError as exc:
            msg = f"{exc}\n{self.stack.diagnose_missing_replies(exc.missing)}"
            raise AssertionError(msg) from exc
        self.latency.observe(turns=budget.turns, elapsed_seconds=elapsed - self.settle_seconds)

    async def _await_first_baseline_response(self) -> None:
        """Send one message and wait for its reply before the scenario starts.

        The runner first waits for the agent's durable joined-room baselines,
        because first timelines are history and cannot start new responses.
        This single exchange then verifies semantic readiness without retrying
        or discarding a missed request after that admission boundary.
        """
        marker = _source_marker("warm-up", ORIGINAL_REVISION)
        content = self._message_content("Live fuzz warm up", marker=marker)
        event_id = await self.client.send_event("m.room.message", "live-fuzz-warm-up", content)
        self.source_current_markers[event_id] = marker
        self.sent_records.append(
            _SentRecord(event_id, self.client.room_id, "m.room.message", sender=self.client.user_id, content=content),
        )
        self.oracle.expect("warm-up", event_id)
        await self._await_replies()

    async def _send_roots(self, threads: Collection[int]) -> None:
        """Create every thread root, in waves sized to the room's one lane.

        A room's events are handled by a single sequential lane, so releasing
        all forty-five roots at once buys no parallelism inside MindRoom. It
        only puts the entire fan-out behind one deadline and turns a failure
        report into "forty-four replies are missing" instead of naming the turn
        that stopped. Each wave is still sent simultaneously, so the transport
        concurrency the proof cares about is unchanged.
        """
        ordered = sorted(threads)
        wave_size = self.root_fanout or len(ordered) or 1
        for start in range(0, len(ordered), wave_size):
            await self._send_root_wave(ordered[start : start + wave_size])

    async def _send_root_wave(self, threads: Collection[int]) -> None:
        """Send one simultaneous wave of roots and wait out its replies."""

        async def send_root(thread: int) -> tuple[int, str, _SentPayload, float]:
            logical_ref = f"root:{thread}"
            content = self._message_content(
                f"Live fuzz root {thread}",
                marker=_source_marker(logical_ref, ORIGINAL_REVISION),
            )
            payload = _SentPayload("m.room.message", f"live-fuzz-{logical_ref}", content)
            root_client = (
                self._client_for_thread(thread)
                if self.scenario.profile in {"saturation", "short-stream-correctness"}
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
        await self._await_replies()

    async def _apply(
        self,
        operation: LiveOperation,
    ) -> tuple[LiveOperation, str | None, _SentPayload | None]:
        if operation.cleanup_sources:
            # The serialized probe may start only after the preceding source
            # redactions crossed the runtime's durable tombstone boundary.
            await self._wait_for_cleanup_tombstones(operation)
        if operation.kind is LiveOperationKind.REDACTION:
            return await self._apply_redaction(operation)

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
            cleanup_targets = self._qualified_cleanup_targets(operation)
            event_id = await self._send_expected_message(operation, client, payload, room_id)
            if cleanup_targets:
                self._cleanup_probe_targets[event_id] = cleanup_targets
            self.source_current_markers[event_id] = _source_marker(operation.event_ref, ORIGINAL_REVISION)
            return operation, event_id, payload

        if operation.kind is LiveOperationKind.PLAIN_REPLY:
            content = self._message_content(
                f"Live fuzz plain reply {operation.operation_id}",
                relation={"m.in_reply_to": {"event_id": target_event_id}},
                marker=_source_marker(operation.event_ref, ORIGINAL_REVISION),
            )
            payload = _SentPayload("m.room.message", txn_id, content)
            cleanup_targets = self._qualified_cleanup_targets(operation)
            event_id = await self._send_expected_message(operation, client, payload, room_id)
            if cleanup_targets:
                self._cleanup_probe_targets[event_id] = cleanup_targets
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
            self._reconcile_edit_debts()
            self._push_source_revision(target_event_id, event_id, edit_marker)
            self._edit_event_source[event_id] = target_event_id
            self._pending_edit_markers.setdefault(target_event_id, {})[event_id] = edit_marker
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

        payload = self.sent_payloads[operation.target]
        event_id = await client.send_event(payload.event_type, payload.txn_id, payload.content, room_id=room_id)
        if event_id != target_event_id:
            msg = f"idempotent retry changed event ID for {operation.target}: {target_event_id} -> {event_id}"
            raise AssertionError(msg)
        return operation, event_id, None

    async def _apply_redaction(
        self,
        operation: LiveOperation,
        *,
        on_landed: Callable[
            [tuple[LiveOperation, str | None, _SentPayload | None]],
            None,
        ]
        | None = None,
    ) -> tuple[LiveOperation, str | None, _SentPayload | None]:
        """Land and record one redaction before cancellable oracle follow-up."""
        assert operation.target is not None
        target_event_id = await self._resolve_target(operation.target)
        txn_id = f"live-fuzz-op-{operation.operation_id}"
        client = self._client_for_operation(operation)
        room_id = self._room_for_thread(operation.thread)
        event_id = await client.redact(target_event_id, txn_id, room_id=room_id)
        observations = _ModelHandler.observations_snapshot()
        reverted_source = next(
            (source for source, edits in self.source_revision_markers.items() if target_event_id in edits),
            None,
        )
        if reverted_source is not None and target_event_id not in self.redacted_edit_evidence:
            marker = self.source_revision_markers[reverted_source][target_event_id]
            self.redacted_edit_evidence[target_event_id] = RedactedEditEvidence(
                reverted_source,
                target_event_id,
                marker,
                frozenset(call for call, markers in observations.items() if marker in markers),
                frozenset(self.source_revision_markers[reverted_source]),
            )
        self._reconcile_edit_debts()
        self.redacted_targets[target_event_id] = event_id
        self.sent_records.append(
            _SentRecord(
                event_id,
                room_id,
                "m.room.redaction",
                sender=client.user_id,
                redacts=target_event_id,
                content={"reason": "live journal fuzz"},
            ),
        )
        if reverted_source is not None:
            # Canonical content rolls back independently of completed output.
            # Remove only this edit's debt; deletion orders no regeneration.
            self._pop_source_revision(reverted_source, target_event_id)
            pending = self._pending_edit_markers.get(reverted_source, {})
            pending.pop(target_event_id, None)
            if not pending:
                self._pending_edit_markers.pop(reverted_source, None)
            self._pending_source_tombstones.add(target_event_id)
        elif target_event_id in self.oracle.expected_sources:
            self._pending_edit_markers.pop(target_event_id, None)
            self._pending_source_tombstones.add(target_event_id)

        result = (operation, event_id, None)
        if on_landed is not None:
            on_landed(result)

        if reverted_source is None:
            await self.oracle.pump(timeout_ms=0)
            self.oracle.refresh_ledger_attributions(min_interval=0.0)
            if target_event_id not in self.oracle.settled_sources():
                # A source redacted before its reply settles legitimately races
                # the in-flight response, so its exact cardinality is zero-or-one.
                self.oracle.mark_source_optional(target_event_id)
        return result

    async def _wait_for_cleanup_tombstones(self, operation: LiveOperation) -> None:
        """Wait only for exact requested tombstones; the probe itself performs lazy cleanup."""
        deadline = time.monotonic() + self.reply_timeout
        targets = tuple(self.event_ids[source] for source in operation.cleanup_sources)
        while True:
            self.oracle.refresh_ledger_attributions(min_interval=0.0)
            observed = {
                target
                for target in targets
                if _redaction_target_state(target, self.oracle._ledger_observations, self.source_revision_markers)[0]
            }
            self._pending_source_tombstones.difference_update(observed)
            if len(observed) == len(targets):
                break
            if time.monotonic() >= deadline:
                msg = f"timed out waiting for cleanup probe tombstones: {sorted(set(targets) - observed)}"
                raise AssertionError(msg)
            await self.oracle.pump(timeout_ms=250)

    def _qualified_cleanup_targets(self, operation: LiveOperation) -> tuple[str, ...]:
        """Observe exact tombstones synchronously before a later source is sent."""
        self.oracle.refresh_ledger_attributions(min_interval=0.0)
        targets = {self.event_ids[source] for source in operation.cleanup_sources}
        for target_id in self.redacted_targets:
            source_id = next(
                (source for source, edits in self.source_revision_markers.items() if target_id in edits),
                target_id,
            )
            if (
                self.oracle.source_threads.get(source_id) == operation.thread
                and _redaction_target_state(target_id, self.oracle._ledger_observations, self.source_revision_markers)[
                    0
                ]
            ):
                targets.add(target_id)
        return tuple(sorted(targets))

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
    parser.add_argument(
        "--sync-mode",
        choices=("classic", "sliding"),
        default="classic",
        help="Matrix sync transport used by the managed runtime (default: classic)",
    )
    parser.add_argument(
        "--profile",
        choices=(
            "fuzz",
            "chaos",
            "saturation",
            "restart-regression",
            "short-stream-correctness",
            "sustained-stream-capacity",
        ),
        default="fuzz",
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--steps", type=_positive_int, default=200)
    parser.add_argument(
        "--threads",
        type=_positive_int,
        help="thread count (default: 45 for fuzz, 200 for sustained-stream-capacity)",
    )
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
        "--root-fanout",
        type=_non_negative_int,
        default=DEFAULT_ROOT_FANOUT,
        help="thread roots released simultaneously per wave (0 releases every root at once)",
    )
    parser.add_argument(
        "--reply-timeout",
        type=float,
        help=(
            "adaptive per-turn floor for fuzz, restart-regression, and short-stream-correctness; "
            "one fixed whole-workload non-extending SLA for sustained-stream-capacity "
            "(default: 60s fuzz and restart-regression; 180s short-stream-correctness "
            "and sustained-stream-capacity)"
        ),
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


async def _close_live_matrix_client(client: LiveMatrixClient) -> BaseException | None:
    """Close one client while retaining even process-control exceptions."""
    try:
        await client.close()
    except BaseException as exc:
        return exc
    return None


async def _run_live(
    stack: ManagedTuwunelStack,
    scenario: LiveFuzzScenario,
    *,
    reply_timeout: float,
    settle_seconds: float,
    root_fanout: int = DEFAULT_ROOT_FANOUT,
    pending_grace: float = 1.0,
    runner_sink: Callable[[LiveFuzzRunner], None] | None = None,
    journal: Callable[[Mapping[str, object]], None] | None = None,
) -> dict[str, object]:
    client_count = (
        scenario.thread_count - 1
        if scenario.profile in {"saturation", "short-stream-correctness"}
        else scenario.client_count
    )
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
        root_fanout=root_fanout,
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
            *(_close_live_matrix_client(client) for client in clients),
        )
        close_errors = [
            (f"close Matrix client {index}", result) for index, result in enumerate(close_results) if result is not None
        ]
        if close_errors:
            if primary_error is not None:
                details = "; ".join(_format_cleanup_failure(label, error) for label, error in close_errors)
                primary_error.add_note(f"Matrix client cleanup failures: {details}")
            else:
                _raise_cleanup_failures(close_errors, message="Matrix client cleanup failed")


def _with_redaction_cleanup_probes(scenario: LiveFuzzScenario) -> LiveFuzzScenario:
    """Append explicit next-turn qualification; loading a saved trace never calls this."""
    sources = {f"root:{thread}": thread for thread in range(scenario.thread_count)}
    redacted_by_thread: dict[int, set[str]] = defaultdict(set)
    operations = [operation for batch in scenario.batches for operation in batch]
    for operation in operations:
        if operation.kind in MESSAGE_KINDS:
            sources[operation.event_ref] = operation.thread
        elif operation.kind is LiveOperationKind.EDIT and operation.target in sources:
            sources[operation.event_ref] = sources[operation.target]
        if operation.kind is LiveOperationKind.REDACTION and operation.target in sources:
            assert operation.target is not None
            redacted_by_thread[sources[operation.target]].add(operation.target)
    if not redacted_by_thread:
        return scenario
    next_id = max((operation.operation_id for operation in operations), default=-1) + 1
    batches = list(scenario.batches)
    if scenario.profile == "chaos":
        batches.append((LiveOperation(next_id, LiveOperationKind.CHECKPOINT, 0, None),))
        next_id += 1
    probes = tuple(
        LiveOperation(
            next_id + index,
            LiveOperationKind.THREAD_MESSAGE,
            thread,
            f"root:{thread}",
            client=scenario.root_client(thread),
            cleanup_sources=tuple(sorted(redacted_sources)),
        )
        for index, (thread, redacted_sources) in enumerate(sorted(redacted_by_thread.items()))
    )
    batches.append(probes)
    if scenario.profile == "chaos":
        batches.append((LiveOperation(next_id + len(probes), LiveOperationKind.CHECKPOINT, 0, None),))
    qualified = replace(scenario, batches=tuple(batches))
    qualified.validate()
    return qualified


def _scenario_from_args(args: argparse.Namespace) -> LiveFuzzScenario:
    """Build or load the requested trace."""
    if args.trace is not None:
        return LiveFuzzScenario.from_json(args.trace.read_text(encoding="utf-8"))
    if args.profile in {"saturation", "short-stream-correctness"}:
        return replace(short_stream_correctness_scenario(), profile=args.profile)
    if args.profile == "restart-regression":
        return restart_regression_scenario()
    if args.profile == "sustained-stream-capacity":
        return sustained_stream_capacity_scenario(root_count=args.threads or 200)
    if args.profile == "chaos":
        return _with_redaction_cleanup_probes(
            chaos_scenario_from_seed(
                args.seed,
                steps=args.steps,
                tuning=ChaosTuning(
                    thread_count=args.threads or 24,
                    client_count=args.clients,
                    room_count=args.rooms,
                    max_batch_size=args.max_batch_size,
                    hot_thread_weight=args.hot_thread_weight,
                    checkpoint_interval=args.checkpoint_interval,
                    lifecycle_interval=args.lifecycle_interval,
                    downtime_batches=args.downtime_batches,
                ),
            ),
        )
    return _with_redaction_cleanup_probes(
        live_scenario_from_seed(
            args.seed,
            steps=args.steps,
            thread_count=args.threads or 45,
            max_batch_size=args.max_batch_size,
            restart_interval=args.restart_interval,
        ),
    )


_PROFILE_STREAMS = {
    "fuzz": StreamProfile(),
    "restart-regression": StreamProfile(),
    "short-stream-correctness": StreamProfile(stream_segments=96, stream_delay=0.012),
    "sustained-stream-capacity": StreamProfile(),
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

_PROFILE_REPLY_TIMEOUTS = {"fuzz": 60.0, "restart-regression": 60.0, "saturation": 180.0, "chaos": 90.0}


def _room_keys_for(scenario: LiveFuzzScenario) -> tuple[str, ...]:
    """Return config room keys covering every scenario room."""
    return (ROOM_KEY, *(f"chaos{index}" for index in range(1, scenario.room_count)))


DEFAULT_ARTIFACT_ROOT = DEFAULT_LIVE_FUZZ_STATE_ROOT / "artifacts"


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


def _required_mindroom_revision() -> str:
    """Return the exact runner revision or fail before live setup."""
    revision = _git_revision(PROJECT_ROOT)
    if revision is None:
        msg = "could not freeze the live runner MindRoom revision"
        raise RuntimeError(msg)
    return revision


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
    expected_mindroom_revision: str,
) -> dict[str, object]:
    """Validate actual child imports against the runner and locked installation."""
    raw = json.loads(attestation_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = "MindRoom runtime attestation must be a JSON object"
        raise TypeError(msg)
    return _validated_import_provenance(
        raw,
        expected_mindroom_revision=expected_mindroom_revision,
    )


def _validated_import_provenance(
    raw: Mapping[str, object],
    *,
    expected_mindroom_revision: str,
) -> dict[str, object]:
    """Validate imported module paths against the current checkout and locked installation."""
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
            PROJECT_ROOT / "justfile",
            PROJECT_ROOT / "local" / "instances" / "deploy",
            PROJECT_ROOT / "pyproject.toml",
            PROJECT_ROOT / "uv.lock",
        ),
    )
    if mindroom_revision is None:
        msg = "could not verify the loaded MindRoom revision"
        raise RuntimeError(msg)
    if mindroom_revision != expected_mindroom_revision:
        msg = (
            "loaded MindRoom revision does not match the live runner: "
            f"expected {expected_mindroom_revision}, loaded {mindroom_revision}"
        )
        raise RuntimeError(msg)
    expected_nio_path = Path(str(distribution("mindroom-nio").locate_file("nio/__init__.py"))).resolve()
    expected_version = _locked_nio_version()
    if nio_path != expected_nio_path:
        msg = f"loaded mindroom-nio path is outside the locked installed distribution: {nio_path}"
        raise RuntimeError(msg)
    if raw.get("nio_version") != expected_version or version("mindroom-nio") != expected_version:
        msg = f"loaded mindroom-nio version does not match uv.lock: expected {expected_version}"
        raise RuntimeError(msg)
    return {
        **raw,
        "mindroom_revision": mindroom_revision,
        "mindroom_expected_revision": expected_mindroom_revision,
        "mindroom_dirty": mindroom_dirty,
        "mindroom_source_sha256": _mindroom_source_sha256(),
        "nio_expected_version": expected_version,
        "nio_module_sha256": _nio_source_sha256(nio_path),
    }


def _nio_source_sha256(module_path: Path) -> str:
    """Bind all installed Nio source modules, including the durable sync implementation."""
    digest = hashlib.sha256()
    for path in sorted(module_path.parent.rglob("*.py")):
        digest.update(str(path.relative_to(module_path.parent)).encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _locked_nio_version() -> str:
    """Return the dependency version pinned by this worktree's lockfile."""
    lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    return next(package["version"] for package in lock["package"] if package["name"] == "mindroom-nio")


def _mindroom_source_sha256() -> str:
    """Bind tracked runtime and harness contents, including uncommitted changes."""
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            "src/mindroom",
            "scripts/testing/fuzz_live_matrix.py",
            "justfile",
            "local/instances/deploy",
            "pyproject.toml",
            "uv.lock",
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    )
    digest = hashlib.sha256()
    for relative in sorted(set(result.stdout.decode().split("\0")) - {""}):
        path = PROJECT_ROOT / relative
        digest.update(relative.encode() + b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
    return digest.hexdigest()


def _run_provenance(mindroom_revision: str | None = None) -> dict[str, object]:
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
    provenance["mindroom_head"] = mindroom_revision or _required_mindroom_revision()
    try:
        provenance["nio_version"] = version("mindroom-nio")
    except PackageNotFoundError:
        provenance["nio_version"] = "<not installed>"
    return provenance


def _install_runtime_redaction_observer(path: Path) -> None:
    """Install only in the real child; failed observation must stop before mutation."""
    from mindroom.turn_store import TurnStore  # noqa: PLC0415

    original = TurnStore.mark_source_redacted

    def observed(self: TurnStore, source_event_id: str) -> Coroutine[Any, Any, TurnRecord | None]:
        try:
            entry = RuntimeRedactionEntry(
                self.deps.agent_name,
                f"{self.deps.agent_name}@{self.deps.resolver.deps.matrix_id.full_id}",
                source_event_id,
                time.monotonic_ns(),
                self.is_revision_redacted(source_event_id)
                or any(
                    revision.redacted
                    for record in self._ledger.all_turn_records()
                    if (revision := (record.revision_replay or {}).get(source_event_id)) is not None
                ),
            )
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND), "w", encoding="utf-8") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                handle.write(json.dumps(asdict(entry), sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception as exc:
            msg = f"runtime redaction observation failed before mutation: {exc}"
            raise SystemExit(msg) from exc
        return original(self, source_event_id)

    TurnStore.mark_source_redacted = observed


def _run_mindroom_runtime_child(attestation_path: Path, observation_path: Path, arguments: list[str]) -> None:
    """Attest actual imported packages, then enter the real MindRoom CLI."""
    from mindroom.cli.main import app  # noqa: PLC0415

    _install_runtime_redaction_observer(observation_path)

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
        "runtime_redaction_observer": {
            "path": str(observation_path),
            "boundary": "TurnStore.mark_source_redacted.entry",
        },
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
        "principal_id": f"{AGENT_NAME}@{oracle.agent_id}",
        "expected_sources": dict(oracle.expected_sources),
        "optional_sources": sorted(oracle.optional_sources),
        "observed_sources": sorted(oracle.observed_sources),
        "unsettled_required_sources": sorted(oracle.unsettled_required_sources()),
        "response_ids": {source: sorted(ids) for source, ids in oracle.response_ids.items()},
        "internal_source_ids": sorted(oracle.internal_source_ids),
        "reply_latencies": {source: round(latency, 3) for source, latency in oracle.reply_latencies.items()},
        "supersession_proofs": {source: asdict(proof) for source, proof in oracle.supersession_proofs.items()},
        "recovery_proofs": {source: asdict(proof) for source, proof in oracle.recovery_proofs.items()},
        "response_views": {
            response: _response_view_diagnostic(tuple(oracle.canonical_events.values()), response)
            for replies in oracle.response_ids.values()
            for response in replies
        },
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
        self._artifact_write_errors: list[tuple[str, BaseException]] = []

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
        _require_exact_nio_provenance(provenance)
        _require_exact_mindroom_provenance(provenance, require_final=True)
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

    def record_capture_error(self, artifact: str, error: BaseException) -> None:
        """Retain one collector failure beside its sentinel artifact."""

        def append_error(destination: Path) -> None:
            with destination.open("a", encoding="utf-8") as handle:
                handle.write(f"{artifact}: {type(error).__name__}: {error}\n")

        self._write_isolated("artifact_errors.txt", append_error)

    def _write_isolated(self, name: str, writer: Callable[[Path], None]) -> None:
        """Run one artifact writer, folding any failure into the transcript."""
        try:
            writer(self.directory / name)
        except BaseException as exc:
            detail = f"{name}: {exc}"
            self._artifact_write_errors.append((f"write {name}", exc))
            if name != "artifact_errors.txt":
                with (
                    suppress(OSError),
                    (self.directory / "artifact_errors.txt").open("a", encoding="utf-8") as handle,
                ):
                    handle.write(detail + "\n")

    def finalize(
        self,
        *,
        exception: BaseException | None,
        log_path: Path,
        ledger_path: Path,
        nio_recovery_snapshot: Mapping[str, object],
        oracle_snapshot: Mapping[str, object],
        model_observations: Mapping[object, object],
        diagnostics: Mapping[str, object],
        tuwunel_log: str,
        full_request_observations: Mapping[object, object] | None = None,
        model_timing_observations: Mapping[object, object] | None = None,
        runtime_redaction_path: Path | None = None,
        source_matching: Mapping[str, object] | None = None,
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

        if exception is not None:
            self._write_isolated(
                "exception.txt",
                lambda destination: destination.write_text(
                    f"{type(exception).__name__}: {exception}\n",
                    encoding="utf-8",
                ),
            )
        self._write_isolated("mindroom.log", copy_text(log_path))
        self._write_isolated(
            "handled_turns.json",
            lambda destination: write_json(
                _ledger_evidence_snapshot(ledger_path, principal_id=oracle_snapshot.get("principal_id")),
            )(destination),
        )
        self._write_isolated("nio_recovery.json", write_json(dict(nio_recovery_snapshot)))
        self._write_isolated("oracle_snapshot.json", write_json(dict(oracle_snapshot)))
        self._write_isolated(
            "model_observations.json",
            write_json({str(call_id): markers for call_id, markers in model_observations.items()}),
        )
        self._write_isolated("diagnostics.json", write_json(dict(diagnostics)))
        self._write_isolated("model_timing_observations.json", write_json(dict(model_timing_observations or {})))
        self._write_isolated("source_matching.json", write_json(dict(source_matching or {})))
        if runtime_redaction_path is not None:
            self._write_isolated("runtime-redactions.jsonl", copy_text(runtime_redaction_path))
        self._write_isolated(
            "full_request_observations.json",
            write_json({str(call_id): markers for call_id, markers in (full_request_observations or {}).items()}),
        )
        self._write_isolated(
            "tuwunel.log",
            lambda destination: destination.write_text(tuwunel_log, encoding="utf-8"),
        )
        _raise_cleanup_failures(
            self._artifact_write_errors,
            message="failure bundle artifact write failed",
        )
        return self.directory


def _ledger_evidence_snapshot(ledger_path: Path, *, principal_id: object = None) -> dict[str, object]:
    """Preserve every stored projection verbatim, including unfinished or malformed rows."""
    if not ledger_path.is_file():
        return {"exists": False, "agent_name": AGENT_NAME, "rows": []}
    with closing(sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True)) as database:
        database.execute("BEGIN")
        database.row_factory = sqlite3.Row
        rows = database.execute(
            "SELECT index_event_id, anchor_event_id, record_json FROM turn_records WHERE agent_name = ? ORDER BY index_event_id",
            (AGENT_NAME,),
        ).fetchall()
        snapshot: dict[str, object] = {"exists": True, "agent_name": AGENT_NAME, "rows": [dict(row) for row in rows]}
        if isinstance(principal_id, str):
            snapshot["principal_id"] = principal_id
            snapshot["journal_sources"] = [
                dict(row)
                for row in database.execute(
                    "SELECT event_id, room_id, thread_id, kind, sender, origin_server_ts, state "
                    "FROM journal_events WHERE principal_id = ? ORDER BY receipt_order",
                    (principal_id,),
                ).fetchall()
            ]
            snapshot["delivery_ownership"] = [
                dict(row)
                for row in database.execute(
                    "SELECT delivery_id, stage, room_id, thread_id, event_type, edits_event_id, "
                    "acknowledged_event_id, edit_target_pending, attempted, retired, permanent_failure_reason, "
                    "payload_json, result_json FROM matrix_delivery_outbox WHERE principal_id = ? "
                    "ORDER BY created_at_ns, delivery_id, stage",
                    (principal_id,),
                ).fetchall()
            ]
    return snapshot


def _capture_bundle_collector(
    bundle: FailureBundle,
    artifact: str,
    collector: Callable[[], object],
    sentinel: Callable[[BaseException], object],
    errors: list[tuple[str, BaseException]],
) -> object:
    """Run one evidence collector without blocking independent artifacts."""
    try:
        return collector()
    except BaseException as exc:
        bundle.record_capture_error(artifact, exc)
        errors.append((f"capture {artifact}", exc))
        return sentinel(exc)


def _capture_error_text(artifact: str, error: BaseException) -> str:
    """Build a durable sentinel for a failed evidence collector."""
    return f"<{artifact} capture failed: {type(error).__name__}: {error}>"


def _durable_store_paths(storage_path: Path) -> tuple[Path, ...]:
    """Find the managed accounts' durable Nio SQLite stores."""
    return tuple(sorted((storage_path / "encryption_keys").rglob("*.db")))


def _reset_durable_sync_cursors(storage_path: Path) -> None:
    """Force a fresh sync only after Nio has durably drained captured input."""
    paths = _durable_store_paths(storage_path)
    if not paths:
        msg = "cold restart found no durable Nio stores"
        raise AssertionError(msg)
    # Validate every account before changing any cursor; the managed runtime
    # is stopped, so no input can arrive between this check and the reset.
    for path in paths:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as database:
            if database.execute("SELECT COUNT(*) FROM NioDurableInput").fetchone()[0]:
                msg = f"cold restart cannot bypass unfinished durable input in {path.name}"
                raise AssertionError(msg)
            if database.execute("SELECT COUNT(*) FROM NioDurableBatch").fetchone()[0]:
                msg = f"cold restart cannot bypass unacknowledged durable batches in {path.name}"
                raise AssertionError(msg)
    for path in paths:
        with closing(sqlite3.connect(path)) as database:
            database.execute("UPDATE NioDurableMeta SET cursor = NULL WHERE id = 1")
            database.commit()


def _nio_recovery_snapshot(storage_path: Path) -> dict[str, object]:
    """Read current durable positions and queue depths without event or crypto bodies."""
    stores: dict[str, object] = {}
    for path in _durable_store_paths(storage_path):
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as database:
            tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "NioDurableMeta" not in tables:
                continue
            version_number, stream_id, acknowledged, cursor_present = database.execute(
                "SELECT version, stream_id, acked_sequence, cursor IS NOT NULL FROM NioDurableMeta WHERE id=1",
            ).fetchone()
            stores[str(path.relative_to(storage_path))] = {
                "version": version_number,
                "stream_id": stream_id,
                "acked_sequence": acknowledged,
                "cursor_present": bool(cursor_present),
                "pending_input_count": database.execute("SELECT COUNT(*) FROM NioDurableInput").fetchone()[0],
                "pending_batch_count": database.execute("SELECT COUNT(*) FROM NioDurableBatch").fetchone()[0],
                "pending_sequences": [
                    row[0] for row in database.execute("SELECT sequence FROM NioDurableBatch ORDER BY sequence")
                ],
                "room_count": database.execute("SELECT COUNT(*) FROM NioDurableRoom").fetchone()[0],
            }
    return {"stores": stores}


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


def _require_exact_nio_provenance(provenance: Mapping[str, object]) -> None:
    """Reject PASS evidence unless imported Nio matches the installed lock pin."""
    expected = provenance.get("nio_expected_version")
    if not isinstance(expected, str) or not expected or provenance.get("nio_version") != expected:
        msg = "passing live run did not prove the locked installed mindroom-nio version"
        raise RuntimeError(msg)
    digest = provenance.get("nio_module_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        msg = "passing live run omitted the imported mindroom-nio content digest"
        raise RuntimeError(msg)


def _require_exact_mindroom_provenance(
    provenance: Mapping[str, object],
    *,
    require_final: bool = False,
) -> None:
    """Reject mixed or stale MindRoom generation evidence."""
    frozen_revision = provenance.get("mindroom_frozen_revision")
    generations = provenance.get("runtime_generations")
    source_digest = provenance.get("mindroom_source_sha256")
    if not isinstance(source_digest, str) or len(source_digest) != 64:
        msg = "passing live run omitted the MindRoom source content digest"
        raise RuntimeError(msg)
    if not isinstance(frozen_revision, str) or not frozen_revision:
        msg = "passing live run omitted the frozen MindRoom revision"
        raise RuntimeError(msg)
    if not isinstance(generations, list):
        msg = "passing live run has invalid per-generation runtime attestations"
        raise TypeError(msg)
    if not generations:
        msg = "passing live run omitted per-generation runtime attestations"
        raise RuntimeError(msg)
    for index, generation in enumerate(generations, start=1):
        if not isinstance(generation, dict):
            msg = f"passing live run has invalid runtime generation {index}"
            raise TypeError(msg)
        generation = cast("dict[str, object]", generation)
        if (
            generation.get("mindroom_revision") != frozen_revision
            or generation.get("mindroom_expected_revision") != frozen_revision
            or generation.get("mindroom_source_sha256") != provenance.get("mindroom_source_sha256")
            or generation.get("nio_module_sha256") != provenance.get("nio_module_sha256")
            or generation.get("nio_version") != provenance.get("nio_version")
            or generation.get("nio_expected_version") != provenance.get("nio_expected_version")
        ):
            msg = f"passing live run generation {index} did not use unchanged MindRoom {frozen_revision}"
            raise RuntimeError(msg)
    if not require_final:
        return
    _require_final_source_validation(provenance, frozen_revision)


def _require_final_source_validation(provenance: Mapping[str, object], frozen_revision: str) -> None:
    """Require final file validation to describe the same run as its receipt."""
    final_validation = provenance.get("final_source_validation")
    if not isinstance(final_validation, dict):
        msg = "passing live run omitted final source validation"
        raise TypeError(msg)
    final_validation = cast("dict[str, object]", final_validation)
    exact_fields = (
        "mindroom_dirty",
        "mindroom_expected_revision",
        "mindroom_revision",
        "nio_version",
        "nio_expected_version",
        "nio_module_sha256",
        "mindroom_source_sha256",
    )
    if (
        final_validation.get("mindroom_revision") != frozen_revision
        or final_validation.get("mindroom_expected_revision") != frozen_revision
        or final_validation.get("mindroom_source_sha256") != provenance.get("mindroom_source_sha256")
        or any(final_validation.get(key) != provenance.get(key) for key in exact_fields)
    ):
        msg = "passing live run final source validation does not match its receipt provenance"
        raise RuntimeError(msg)


def _require_runtime_provenance(
    stack: ManagedTuwunelStack,
) -> Mapping[str, object]:
    """Return child-attested provenance or fail before destructive cleanup."""
    provenance = stack.runtime_provenance
    if provenance is None:
        msg = "passing live run omitted child runtime provenance"
        raise RuntimeError(msg)
    _require_exact_mindroom_provenance(provenance)
    _require_exact_nio_provenance(provenance)
    return provenance


def main() -> None:
    """Run one trace against a fresh disposable real-server stack."""
    if len(sys.argv) >= 5 and sys.argv[1] == "__mindroom_runtime_child__":
        _run_mindroom_runtime_child(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4:])
        return
    args = _parse_args()
    scenario = _scenario_from_args(args)
    if args.save_trace is not None:
        args.save_trace.write_text(scenario.to_json() + "\n", encoding="utf-8")
    reply_timeout = args.reply_timeout
    if reply_timeout is None:
        reply_timeout = _PROFILE_REPLY_TIMEOUTS.get(scenario.profile, 180.0)
    host_load = collect_host_load_report()
    print(host_load.render(), file=sys.stderr, flush=True)

    mindroom_revision = _required_mindroom_revision()
    run_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}"
    bundle = FailureBundle.create(
        args.artifact_root,
        run_id,
        scenario=scenario,
        provenance=_run_provenance(mindroom_revision),
    )
    stack = ManagedTuwunelStack(
        stream_profile=_PROFILE_STREAMS[scenario.profile],
        room_keys=_room_keys_for(scenario),
        provenance_sink=bundle.update_provenance,
        artifact_directory=bundle.directory,
        mindroom_revision=mindroom_revision,
        profile=scenario.profile,
        sync_mode=args.sync_mode,
        stream_segments=96 if scenario.profile == "short-stream-correctness" else 4,
        stream_delay=0.012 if scenario.profile == "short-stream-correctness" else 0.001,
        # The hard-restart latch spans the later checkpoint wait plus process scheduling.
        model_latch_timeout=reply_timeout * 3,
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
                root_fanout=args.root_fanout,
                pending_grace=args.pending_grace,
                runner_sink=lambda runner: runner_holder.__setitem__("runner", runner),
                journal=bundle.record_realized,
            ),
        )
        result["seed"] = args.seed if args.trace is None else "trace"
        result["wall_seconds"] = round(time.monotonic() - started_at, 1)
        result.update(stack.diagnostic_counts())
        result.update(host_load.as_dict())
        result["sync_mode"] = stack.sync_mode
        provenance = _require_runtime_provenance(stack)
    except BaseException as exc:
        _capture_failed_run(args, bundle, stack, runner_holder.get("runner"), exc)
        raise

    def snapshot_runtime_evidence() -> None:
        _persist_run_bundle(bundle, stack, runner_holder.get("runner"))

    try:
        stack.close(
            before_destructive_cleanup=snapshot_runtime_evidence,
        )
    except BaseException as cleanup_error:
        _record_secondary_failure(bundle, cleanup_error, label="Live Matrix fuzz cleanup error")
        print(f"Live Matrix fuzz cleanup failure bundle: {bundle.directory}", file=sys.stderr)
        raise
    try:
        provenance = stack.revalidate_runtime_provenance()
        receipt = bundle.retain_pass_receipt(result, provenance)
    except BaseException as provenance_error:
        _record_secondary_failure(bundle, provenance_error, label="Final source validation error")
        print(f"Live Matrix fuzz provenance failure bundle: {bundle.directory}", file=sys.stderr)
        raise
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
        path = _persist_run_bundle(bundle, stack, runner, exception=exc)
        print(f"Live Matrix fuzz failure bundle: {path}", file=sys.stderr)
    except BaseException as bundle_exc:
        # Evidence-capture errors must never replace the primary fuzz failure.
        print(f"Failure bundle capture error (ignored): {bundle_exc}", file=sys.stderr)


def _persist_run_bundle(
    bundle: FailureBundle,
    stack: ManagedTuwunelStack,
    runner: LiveFuzzRunner | None,
    *,
    exception: BaseException | None = None,
) -> Path:
    """Copy disposable stack evidence before cleanup can remove its sources."""
    capture_errors: list[tuple[str, BaseException]] = []
    nio_recovery_snapshot = cast(
        "Mapping[str, object]",
        _capture_bundle_collector(
            bundle,
            "nio_recovery.json",
            lambda: _nio_recovery_snapshot(stack.storage_path),
            lambda error: {"_capture_error": _capture_error_text("nio_recovery.json", error)},
            capture_errors,
        ),
    )
    oracle_snapshot = cast(
        "Mapping[str, object]",
        _capture_bundle_collector(
            bundle,
            "oracle_snapshot.json",
            lambda: _sanitized_oracle_snapshot(runner.oracle) if runner is not None else {},
            lambda error: {"_capture_error": _capture_error_text("oracle_snapshot.json", error)},
            capture_errors,
        ),
    )
    model_observations = cast(
        "Mapping[object, object]",
        _capture_bundle_collector(
            bundle,
            "model_observations.json",
            _ModelHandler.observations_snapshot,
            lambda error: {"_capture_error": _capture_error_text("model_observations.json", error)},
            capture_errors,
        ),
    )
    full_request_observations = cast(
        "Mapping[object, object]",
        _capture_bundle_collector(
            bundle,
            "full_request_observations.json",
            _ModelHandler.full_request_observations_snapshot,
            lambda error: {"_capture_error": _capture_error_text("full_request_observations.json", error)},
            capture_errors,
        ),
    )
    diagnostics = cast(
        "Mapping[str, object]",
        _capture_bundle_collector(
            bundle,
            "diagnostics.json",
            stack.diagnostic_counts,
            lambda error: {"_capture_error": _capture_error_text("diagnostics.json", error)},
            capture_errors,
        ),
    )
    tuwunel_log = cast(
        "str",
        _capture_bundle_collector(
            bundle,
            "tuwunel.log",
            stack.tuwunel_log,
            lambda error: _capture_error_text("tuwunel.log", error) + "\n",
            capture_errors,
        ),
    )
    ledger_path = stack.storage_path / "tracking" / "event_journal.db"
    model_timing_observations = _capture_bundle_collector(
        bundle,
        "model_timing_observations.json",
        lambda: {
            str(call): {"markers": sorted(observation.markers), "monotonic_ns": observation.monotonic_ns}
            for call, observation in _ModelHandler.timed_observations_snapshot().items()
        },
        lambda error: {"_capture_error": _capture_error_text("model_timing_observations.json", error)},
        capture_errors,
    )
    source_matching = _capture_bundle_collector(
        bundle,
        "source_matching.json",
        lambda: runner.source_matching_snapshot() if runner is not None else {},
        lambda error: {"_capture_error": _capture_error_text("source_matching.json", error)},
        capture_errors,
    )
    path = bundle.finalize(
        exception=exception,
        log_path=stack.log_path,
        ledger_path=ledger_path,
        nio_recovery_snapshot=nio_recovery_snapshot,
        oracle_snapshot=oracle_snapshot,
        model_observations=model_observations,
        full_request_observations=full_request_observations,
        model_timing_observations=cast("Mapping[object, object]", model_timing_observations),
        runtime_redaction_path=stack.runtime_redaction_path,
        source_matching=cast("Mapping[str, object]", source_matching),
        diagnostics=diagnostics,
        tuwunel_log=tuwunel_log,
    )
    _raise_cleanup_failures(capture_errors, message="failure bundle collector failed")
    return path


if __name__ == "__main__":
    main()
