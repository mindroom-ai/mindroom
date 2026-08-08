"""Replay concurrent Matrix mutations against disposable Tuwunel and MindRoom.

Unlike the in-process fuzzers, this runner crosses the real Matrix
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
import re
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import datetime
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
RESTART_MODEL_ID = "mindroom-live-fuzz-replacement"
RECOVERED_MODEL_ID = "mindroom-live-fuzz-recovered"
ORIGINAL_RUNTIME_GENERATION_MARKER = "runtime-generation=original"
REPLACEMENT_RUNTIME_GENERATION_MARKER = "runtime-generation=replacement"
RECOVERED_RUNTIME_GENERATION_MARKER = "runtime-generation=recovered"
FRESH_RESTART_REQUEST = "Synthetic fresh startup request"
AGENT_NAME = "general"
ROUTER_NAME = "router"
ROOM_KEY = "lobby"
RESTART_SHUTDOWN_FAILURE_MARKER = "runtime_drain_incomplete_with_durable_dispatch_recovery"
ORDERLY_SHUTDOWN_MARKER = "All agent bots stopped"
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_RECOVERY_CLIFF_OBSERVER_PAGE_SIZE = 500

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

# Recovery-cliff emits 4,000 to 4,800 characters, while sustained capacity
# fixes every response at 4,800 characters so launch spread cannot consume the
# 45-second all-stream overlap. Both profiles stream at 80 characters per
# second, and this lower bound still rejects a fast or non-streaming responder.
RECOVERY_CLIFF_MIN_ACTIVE_STREAM_SECONDS = 45.0


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


@dataclass(frozen=True, slots=True)
class LiveOperation:
    """One replayable live Matrix action."""

    operation_id: int
    kind: LiveOperationKind
    thread: int
    target: str | None

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
        )


@dataclass(frozen=True, slots=True)
class LiveFuzzScenario:
    """Concurrent live batches with logical references instead of event IDs."""

    thread_count: int
    batches: tuple[tuple[LiveOperation, ...], ...]
    profile: str = "fuzz"

    def to_json(self) -> str:
        """Serialize the complete trace for exact replay on a fresh server."""
        return json.dumps(
            {
                "version": 1,
                "profile": self.profile,
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
        )
        scenario.validate()
        return scenario

    def validate(self) -> None:
        """Reject traces with impossible same-batch or forward dependencies."""
        if self.thread_count < 1:
            msg = "live Matrix fuzz trace must contain at least one thread"
            raise ValueError(msg)
        _reject_unknown_live_scenario_profile(self)
        if self.profile in {"restart-regression", "recovery-cliff", "sustained-stream-capacity"}:
            _validate_fixed_profile_trace(self)
            return
        known_events = {f"root:{thread}" for thread in range(self.thread_count)}
        known_responses = {f"response:root:{thread}" for thread in range(self.thread_count)}
        message_events = set(known_events)
        operation_ids: set[int] = set()

        for batch in self.batches:
            if not batch:
                msg = "live Matrix fuzz batches must not be empty"
                raise ValueError(msg)
            _validate_interruption_placement(batch)
            reply_threads = [operation.thread for operation in batch if _owes_reply(operation)]
            if len(reply_threads) != len(set(reply_threads)):
                msg = "same-thread messages requiring replies must use separate batches"
                raise ValueError(msg)

            new_events: set[str] = set()
            new_responses: set[str] = set()
            new_messages: set[str] = set()
            for operation in batch:
                _validate_live_operation(
                    operation,
                    thread_count=self.thread_count,
                    operation_ids=operation_ids,
                    allowed_targets=known_events | known_responses,
                    message_events=message_events,
                )
                if operation.kind is not LiveOperationKind.IDEMPOTENT_RETRY:
                    new_events.add(operation.event_ref)
                if operation.kind in {
                    LiveOperationKind.THREAD_MESSAGE,
                    LiveOperationKind.PLAIN_REPLY,
                }:
                    new_messages.add(operation.event_ref)
                    new_responses.add(f"response:{operation.event_ref}")

            known_events.update(new_events)
            known_responses.update(new_responses)
            message_events.update(new_messages)


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


def _process_group_states(process_group_id: int) -> dict[int, str]:
    """Return Linux process states for every current member of one group."""
    states: dict[int, str] = {}
    for stat_path in Path("/proc").glob("[0-9]*/stat"):
        try:
            raw_stat = stat_path.read_text(encoding="utf-8")
        except OSError:
            continue
        command_end = raw_stat.rfind(")")
        if command_end < 0:
            continue
        fields = raw_stat[command_end + 2 :].split()
        if len(fields) < 3:
            continue
        try:
            observed_group_id = int(fields[2])
        except ValueError:
            continue
        if observed_group_id == process_group_id:
            states[int(stat_path.parent.name)] = fields[0]
    return states


def _reject_unknown_live_scenario_profile(scenario: LiveFuzzScenario) -> None:
    """Reject profiles without a runner implementation."""
    if scenario.profile not in {
        "fuzz",
        "restart-regression",
        "short-stream-correctness",
        "recovery-cliff",
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


def recovery_cliff_scenario(*, root_count: int = 100) -> LiveFuzzScenario:
    """Return the fixed recovery-cliff trace for one root count."""
    scenario = LiveFuzzScenario(thread_count=root_count, batches=(), profile="recovery-cliff")
    scenario.validate()
    return scenario


def sustained_stream_capacity_scenario(*, root_count: int = 200) -> LiveFuzzScenario:
    """Return the fixed no-fault sustained-stream capacity trace."""
    scenario = LiveFuzzScenario(thread_count=root_count, batches=(), profile="sustained-stream-capacity")
    scenario.validate()
    return scenario


@dataclass(frozen=True, slots=True)
class RecoveryCliffFaultShape:
    """Configured limits and the deterministic held-event fault shape."""

    timeline_limit: int
    recovery_max_pages: int
    recovery_page_size: int
    recovery_max_events: int
    context_event_count: int
    root_count: int


def recovery_cliff_fault_shape(config_path: Path, *, root_count: int) -> RecoveryCliffFaultShape:
    """Derive a recoverable gap larger than one bounded nio history pump."""
    from mindroom.matrix.client_session import matrix_client_config  # noqa: PLC0415

    raw_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    matrix_sync = raw_config.get("matrix_sync") if isinstance(raw_config, dict) else None
    timeline_limit = matrix_sync.get("sliding_timeline_limit") if isinstance(matrix_sync, dict) else None
    if not isinstance(timeline_limit, int) or isinstance(timeline_limit, bool):
        msg = "recovery-cliff config omitted an integer Sliding Sync timeline limit"
        raise TypeError(msg)
    client_config = matrix_client_config()
    context_event_count = client_config.backfill_max_pages * client_config.backfill_page_size + timeline_limit + 1
    recovered_event_count = context_event_count + root_count - timeline_limit
    if recovered_event_count > client_config.backfill_max_events:
        msg = "recovery-cliff held event shape exceeds nio's configured room recovery cap"
        raise ValueError(msg)
    return RecoveryCliffFaultShape(
        timeline_limit=timeline_limit,
        recovery_max_pages=client_config.backfill_max_pages,
        recovery_page_size=client_config.backfill_page_size,
        recovery_max_events=client_config.backfill_max_events,
        context_event_count=context_event_count,
        root_count=root_count,
    )


@dataclass(frozen=True, slots=True)
class RecoveryCliffTerminalAudit:
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
class RecoveryCliffDrainCounts:
    """Actionable durable work remaining after recovery-cliff responses."""

    pending_journal_rows: int
    unacknowledged_outbox_rows: int


@dataclass(frozen=True, slots=True)
class RecoveryCliffHealthSample:
    """One parsed runtime and Matrix-sync health observation."""

    healthy: bool
    last_sync_time: datetime | None


@dataclass(frozen=True, slots=True)
class RecoveryCliffLogCounts:
    """Recovery-cliff lifecycle counters at one observation boundary."""

    delivery_retry_markers: int
    delivery_worker_markers: int
    recovery_abandonment_markers: int


@dataclass(frozen=True, slots=True)
class RecoveryCliffBaseline:
    """Observer and log state captured only after warm completion."""

    event_ids: frozenset[str]
    log_counts: RecoveryCliffLogCounts


@dataclass(frozen=True, slots=True)
class RecoveryCliffObservation:
    """Frozen evidence evaluated before a recovery-cliff PASS."""

    root_count: int
    terminal_audit: RecoveryCliffTerminalAudit
    delivery_retry_markers: int
    peak_unacknowledged_final_outbox_rows: int
    delivery_worker_markers: int
    recovery_abandonment_markers: int
    drain: RecoveryCliffDrainCounts
    health_samples: tuple[RecoveryCliffHealthSample, ...]
    watchdog_stalls: int
    reaction_settled: bool
    pre_fence_last_sync: datetime | None
    post_fence_last_sync: datetime | None
    clean_shutdown: bool


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
    terminal_audit: RecoveryCliffTerminalAudit
    health_samples: tuple[RecoveryCliffHealthSample, ...]
    health_samples_while_root_release: int
    durable_drain: RecoveryCliffDrainCounts | None
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
class _RecoveryCliffEventIndex:
    """Responder originals, edits, and malformed canonical relations."""

    originals: Mapping[str, tuple[Mapping[str, Any], ...]]
    edits: Mapping[str, tuple[Mapping[str, Any], ...]]
    invalid_relations: tuple[tuple[str, str | None, str | None], ...]
    invalid_replacements: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _RecoveryCliffSourceFold:
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


def _index_recovery_cliff_events(
    events: Collection[Mapping[str, Any]],
    *,
    responder_id: str,
) -> _RecoveryCliffEventIndex:
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

    return _RecoveryCliffEventIndex(
        originals={source: tuple(source_events) for source, source_events in originals.items()},
        edits=edits,
        invalid_relations=tuple(sorted(invalid_relations)),
        invalid_replacements=tuple(sorted(invalid_replacements)),
    )


def _fold_recovery_cliff_source(
    source_event_id: str,
    *,
    index: _RecoveryCliffEventIndex,
) -> _RecoveryCliffSourceFold | None:
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
    return _RecoveryCliffSourceFold(
        source_event_id=source_event_id,
        response_event_id=response_event_id,
        effective_status=statuses[-1],
        terminal_transition_count=len(completed_events),
        started_at_ms=_recovery_event_order(original)[0],
        finished_at_ms=_recovery_event_order(terminal)[0],
    )


def _peak_recovery_cliff_streams(folds: Collection[_RecoveryCliffSourceFold]) -> int:
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


def _full_recovery_cliff_overlap_seconds(folds: Collection[_RecoveryCliffSourceFold]) -> float:
    """Return the common intersection shared by every folded stream interval."""
    if not folds:
        return 0.0
    return max(
        0.0,
        (min(fold.finished_at_ms for fold in folds) - max(fold.started_at_ms for fold in folds)) / 1000,
    )


def audit_recovery_cliff_events(
    events: Collection[Mapping[str, Any]],
    *,
    responder_id: str,
    expected_source_ids: Collection[str],
) -> RecoveryCliffTerminalAudit:
    """Fold raw Matrix originals and same-responder edits into terminal evidence."""
    expected_sources = tuple(sorted(expected_source_ids))
    expected = frozenset(expected_sources)
    index = _index_recovery_cliff_events(events, responder_id=responder_id)
    folds = tuple(
        fold for source in expected_sources if (fold := _fold_recovery_cliff_source(source, index=index)) is not None
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

    return RecoveryCliffTerminalAudit(
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
        full_overlap_seconds=_full_recovery_cliff_overlap_seconds(folds),
        peak_active_streams=_peak_recovery_cliff_streams(folds),
    )


def evaluate_recovery_cliff(observation: RecoveryCliffObservation) -> tuple[str, ...]:
    """Return every acceptance failure in one settled recovery-cliff observation."""
    audit = observation.terminal_audit
    before, after = observation.pre_fence_last_sync, observation.post_fence_last_sync
    failures = (
        f"root_count expected={len(audit.expected_sources)} observed={observation.root_count}"
        if observation.root_count != len(audit.expected_sources)
        else "",
        f"missing_sources={audit.missing_sources}" if audit.missing_sources else "",
        f"duplicate_sources={audit.duplicate_sources}" if audit.duplicate_sources else "",
        f"unknown_sources={audit.unexpected_sources}" if audit.unexpected_sources else "",
        f"invalid_relations={audit.invalid_relations}" if audit.invalid_relations else "",
        f"invalid_replacements={audit.invalid_replacements}" if audit.invalid_replacements else "",
        (
            f"invalid_terminal_transitions={audit.invalid_terminal_transitions}"
            if audit.invalid_terminal_transitions
            else ""
        ),
        f"noncompleted_sources={audit.noncompleted_sources}" if audit.noncompleted_sources else "",
        (
            f"active_stream_duration_too_short={audit.min_active_stream_seconds:.3f} "
            f"minimum={RECOVERY_CLIFF_MIN_ACTIVE_STREAM_SECONDS:.3f}"
            if audit.min_active_stream_seconds < RECOVERY_CLIFF_MIN_ACTIVE_STREAM_SECONDS
            else ""
        ),
        (
            f"peak_active_streams={audit.peak_active_streams} expected={observation.root_count}"
            if audit.peak_active_streams < observation.root_count
            else ""
        ),
        (
            f"full_overlap_too_short={audit.full_overlap_seconds:.3f} "
            f"minimum={RECOVERY_CLIFF_MIN_ACTIVE_STREAM_SECONDS:.3f}"
            if audit.full_overlap_seconds < RECOVERY_CLIFF_MIN_ACTIVE_STREAM_SECONDS
            else ""
        ),
        "delivery_retry_markers=0" if observation.delivery_retry_markers < 1 else "",
        ("peak_unacknowledged_final_outbox_rows=0" if observation.peak_unacknowledged_final_outbox_rows < 1 else ""),
        "delivery_worker_markers=0" if observation.delivery_worker_markers < 1 else "",
        (
            f"recovery_abandonment_markers={observation.recovery_abandonment_markers}"
            if observation.recovery_abandonment_markers
            else ""
        ),
        (
            f"pending_journal_rows={observation.drain.pending_journal_rows}"
            if observation.drain.pending_journal_rows
            else ""
        ),
        (
            f"unacknowledged_outbox_rows={observation.drain.unacknowledged_outbox_rows}"
            if observation.drain.unacknowledged_outbox_rows
            else ""
        ),
        (
            "health_samples_unhealthy"
            if not observation.health_samples
            or any(not sample.healthy or sample.last_sync_time is None for sample in observation.health_samples)
            else ""
        ),
        f"watchdog_stalls={observation.watchdog_stalls}" if observation.watchdog_stalls else "",
        "reaction_not_settled" if not observation.reaction_settled else "",
        "sync_progress_absent_after_fence" if before is None or after is None or after <= before else "",
        "shutdown_not_clean" if not observation.clean_shutdown else "",
    )
    return tuple(failure for failure in failures if failure)


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
            f"minimum={RECOVERY_CLIFF_MIN_ACTIVE_STREAM_SECONDS:.3f}"
            if terminal_audit.min_active_stream_seconds < RECOVERY_CLIFF_MIN_ACTIVE_STREAM_SECONDS
            else ""
        ),
        (
            f"peak_active_streams={terminal_audit.peak_active_streams} expected={observation.root_count}"
            if terminal_audit.peak_active_streams != observation.root_count
            else ""
        ),
        (
            f"full_overlap_too_short={terminal_audit.full_overlap_seconds:.3f} "
            f"minimum={RECOVERY_CLIFF_MIN_ACTIVE_STREAM_SECONDS:.3f}"
            if terminal_audit.full_overlap_seconds < RECOVERY_CLIFF_MIN_ACTIVE_STREAM_SECONDS
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


def _initial_generation_state(thread_count: int) -> _ScenarioGenerationState:
    return _ScenarioGenerationState(
        messages={thread: [f"root:{thread}"] for thread in range(thread_count)},
        responses={thread: [f"response:root:{thread}"] for thread in range(thread_count)},
        editable={thread: [f"root:{thread}"] for thread in range(thread_count)},
        reaction_targets={thread: [f"root:{thread}", f"response:root:{thread}"] for thread in range(thread_count)},
        redactable={thread: [f"root:{thread}"] for thread in range(thread_count)},
        redacted=set(),
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
    for offset in range(batch_size):
        operation = _choose_operation(
            randomizer,
            state,
            operation_id=first_operation_id + offset,
            thread_count=thread_count,
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


class _ModelHandler(BaseHTTPRequestHandler):
    """Small deterministic OpenAI-compatible endpoint for live transport tests."""

    protocol_version = "HTTP/1.1"
    call_ids = itertools.count(1)
    stream_segments = 4
    stream_delay = 0.001
    blocked_request_timeout: float
    blocked_request_started = threading.Event()
    blocked_request_release = threading.Event()

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
                        {"id": MODEL_ID, "object": "model", "owned_by": "mindroom-fuzz"},
                        {
                            "id": RESTART_MODEL_ID,
                            "object": "model",
                            "owned_by": "mindroom-fuzz",
                        },
                        {
                            "id": RECOVERED_MODEL_ID,
                            "object": "model",
                            "owned_by": "mindroom-fuzz",
                        },
                    ],
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
        model_id = payload.get("model")
        if model_id == RESTART_MODEL_ID and FRESH_RESTART_REQUEST in json.dumps(payload):
            self.blocked_request_started.set()
            self.blocked_request_release.wait(timeout=self.blocked_request_timeout)
        generation_marker = {
            RESTART_MODEL_ID: REPLACEMENT_RUNTIME_GENERATION_MARKER,
            RECOVERED_MODEL_ID: RECOVERED_RUNTIME_GENERATION_MARKER,
        }.get(
            model_id,
            ORIGINAL_RUNTIME_GENERATION_MARKER,
        )
        content = self._response_text(call_id, generation_marker)
        try:
            if payload.get("stream") is True:
                self._send_stream(call_id, content, str(model_id))
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
            self.close_connection = True

    @classmethod
    def _response_text(cls, call_id: int, generation_marker: str) -> str:
        segments = " ".join(f"segment-{index:03d}" for index in range(cls.stream_segments))
        return f"LIVE-FUZZ call={call_id} {generation_marker} {segments} END call={call_id}"

    def _send_stream(self, call_id: int, content: str, model_id: str) -> None:
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
        load_average=cast("tuple[float, float, float]", os.getloadavg()),
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
        profile: str = "fuzz",
        stream_segments: int = 4,
        stream_delay: float = 0.001,
        model_latch_timeout: float = 60.0,
    ) -> None:
        if profile not in {
            "fuzz",
            "restart-regression",
            "short-stream-correctness",
            "recovery-cliff",
            "sustained-stream-capacity",
        }:
            msg = f"unsupported live Matrix fuzz profile {profile!r}"
            raise ValueError(msg)
        token = secrets.token_hex(4)
        self.profile = profile
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
        self._stream_segments = stream_segments
        self._stream_delay = stream_delay
        self._model_latch_timeout = model_latch_timeout

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
        self.load_sender_id = f"@mindroom_load_sender_{self.namespace}:{self.server_name}"
        self.router_id = f"@mindroom_router_{self.namespace}:{self.server_name}"

        _run_command("just", "local-instances-start-matrix", self.instance_name)
        self._wait_for_url(f"{self.homeserver}/_matrix/client/versions", timeout=30)
        model_port = self._start_model_server()
        self._write_config(model_port)
        self._env = self._mindroom_environment()
        self._log_handle = self.log_path.open("a", encoding="utf-8")
        self._start_mindroom()

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

    def pause_mindroom(self, *, timeout: float) -> None:
        """Stop the managed process group and non-destructively confirm its state."""
        process = self._mindroom_process
        if process is None or process.poll() is not None:
            msg = "MindRoom is not running"
            raise RuntimeError(msg)
        deadline = time.monotonic() + timeout
        os.killpg(process.pid, signal.SIGSTOP)
        confirmed = False
        try:
            while True:
                stopped = os.waitid(
                    os.P_PID,
                    process.pid,
                    os.WSTOPPED | os.WEXITED | os.WNOHANG | os.WNOWAIT,
                )
                if stopped is not None:
                    leader_stopped = stopped.si_code == os.CLD_STOPPED and stopped.si_status == signal.SIGSTOP
                    group_states = _process_group_states(process.pid)
                    if (
                        leader_stopped
                        and group_states.get(process.pid) in {"T", "t"}
                        and all(state in {"T", "t"} for state in group_states.values())
                    ):
                        confirmed = True
                        return
                    if not leader_stopped:
                        msg = "MindRoom exited before the recovery-cliff stop boundary"
                        raise RuntimeError(msg)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    msg = "MindRoom did not enter the stopped state before the recovery-cliff deadline"
                    raise TimeoutError(msg)
                time.sleep(min(0.01, remaining))
        finally:
            if not confirmed:
                os.killpg(process.pid, signal.SIGCONT)

    def resume_mindroom(self) -> None:
        """Resume the managed process group after a recovery-cliff fault boundary."""
        process = self._mindroom_process
        if process is None:
            msg = "MindRoom is not running"
            raise RuntimeError(msg)
        os.killpg(process.pid, signal.SIGCONT)

    def close(self) -> None:
        """Stop child processes and delete the exact disposable instance."""
        _ModelHandler.blocked_request_release.set()
        self.stop_mindroom()
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
        """Count every live diagnostic marker in the complete runtime output."""
        log = self.read_log()
        if not log:
            return {}
        return {name: _log_count(log, marker) for name, marker in DIAGNOSTIC_MARKERS.items()}

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
        """Add the dormant room and switch only the managed agent to its latch model."""
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        config["agents"][AGENT_NAME]["rooms"].append(room_id)
        config["models"]["default"]["id"] = RESTART_MODEL_ID
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

    def recovery_health_sample(self) -> RecoveryCliffHealthSample:
        """Read and parse one recovery-cliff API health sample."""
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
        return RecoveryCliffHealthSample(
            healthy=response.is_success and payload.get("status") == "healthy",
            last_sync_time=last_sync_time,
        )

    def recovery_drain_counts(self) -> RecoveryCliffDrainCounts:
        """Count only actionable pending journal and unacknowledged outbox rows."""
        database_path = self.storage_path / "tracking" / "event_journal.db"
        if not database_path.is_file():
            msg = f"recovery-cliff event journal database is missing: {database_path}"
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
                    "SELECT COUNT(*) FROM response_outbox WHERE acknowledged_event_id IS NULL",
                ).fetchone()[0],
            )
        return RecoveryCliffDrainCounts(
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
                SELECT COUNT(*) FROM response_outbox
                WHERE principal_id = ? AND stage = 'final'
                  AND attempted = 1 AND acknowledged_event_id IS NULL
                  AND turn_id IN ({placeholders})
                """,  # noqa: S608 - placeholders are generated, values remain bound
                (self._journal_principal_id(self.agent_id), *source_ids),
            ).fetchone()
        return int(cast("int", row[0]))

    def recovery_reaction_state(self, event_id: str) -> str | None:
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

    def _restart_sync_checkpoint_token(self) -> str | None:
        """Read the managed agent's exact durable Classic sync token."""
        continuity_path = self.storage_path / "sync_continuity" / f"{AGENT_NAME}.json"
        if not continuity_path.is_file():
            return None
        payload = json.loads(continuity_path.read_text(encoding="utf-8"))
        checkpoint = payload.get("checkpoint") if isinstance(payload, dict) else None
        token = checkpoint.get("token") if isinstance(checkpoint, dict) else None
        return token if isinstance(token, str) and token else None

    def wait_for_restart_event_checkpoint(self, room_id: str, event_id: str, *, timeout: float) -> bool:
        """Wait for a checkpoint strictly later than durable projection of one event."""
        deadline = time.monotonic() + timeout
        event_projected = _wait_until(
            lambda: self._restart_event_projected_for_agent(room_id, event_id),
            timeout=max(deadline - time.monotonic(), 0),
        )
        if not event_projected:
            return False
        checkpoint_at_projection_observation = self._restart_sync_checkpoint_token()
        return _wait_until(
            lambda: (
                (current := self._restart_sync_checkpoint_token()) is not None
                and current != checkpoint_at_projection_observation
            ),
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
                "SELECT principal_id, stage, attempted, acknowledged_event_id FROM response_outbox WHERE turn_id = ?",
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

    def _start_model_server(self) -> int:
        _ModelHandler.stream_segments = self._stream_segments
        _ModelHandler.stream_delay = self._stream_delay
        _ModelHandler.blocked_request_timeout = self._model_latch_timeout
        _ModelHandler.blocked_request_started.clear()
        _ModelHandler.blocked_request_release.clear()
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
                    "rooms": [ROOM_KEY],
                    "learning": False,
                },
            },
            "defaults": {"tools": [], "enable_streaming": True, "markdown": False},
            "memory": {"backend": "file"},
            "router": {"model": "router"},
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
        if self.profile in {"recovery-cliff", "sustained-stream-capacity"}:
            config["matrix_sync"] = {"mode": "sliding", "sliding_timeline_limit": 100}
            config["models"]["synthetic"] = {
                "provider": "synthetic",
                "id": "lorem-ipsum",
                "extra_kwargs": {
                    "seed": 1,
                    "min_response_chars": 4800 if self.profile == "sustained-stream-capacity" else 4000,
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
        """Start one production-version child within a single health-and-room deadline."""
        assert self._log_handle is not None
        deadline = time.monotonic() + timeout
        self._mindroom_process = subprocess.Popen(
            [
                "uv",
                "run",
                "--python",
                "3.13",
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
            start_new_session=True,
        )
        self._wait_for_url(
            f"http://127.0.0.1:{self.api_port}/api/health",
            timeout=max(0, deadline - time.monotonic()),
        )
        state_path = self.storage_path / "matrix_state.yaml"
        while time.monotonic() < deadline:
            if self._mindroom_process.poll() is not None:
                msg = "MindRoom exited during startup"
                raise RuntimeError(msg)
            if state_path.exists():
                state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
                room = state.get("rooms", {}).get(ROOM_KEY, {}) if isinstance(state, dict) else {}
                room_id = room.get("room_id") if isinstance(room, dict) else None
                if isinstance(room_id, str):
                    self.room_id = room_id
                    return
            time.sleep(0.2)
        msg = f"MindRoom did not create {ROOM_KEY!r}"
        raise TimeoutError(msg)

    def stop_mindroom(self, *, timeout: float = 20) -> bool:
        """Stop MindRoom and report whether its shutdown stayed bounded and clean."""
        process = self._mindroom_process
        if process is None:
            return True
        shutdown_marker_count = self.log_count(ORDERLY_SHUTDOWN_MARKER)
        stopped_gracefully = process.poll() is None
        if stopped_gracefully:
            os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                stopped_gracefully = False
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
        self._mindroom_process = None
        clean_exit = process.returncode in {
            0,
            -signal.SIGINT,
            128 + signal.SIGINT,
        }
        child_shutdown_completed = self.log_count(ORDERLY_SHUTDOWN_MARKER) > shutdown_marker_count
        return stopped_gracefully and clean_exit and child_shutdown_completed

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

    def __init__(self, homeserver: str, room_id: str) -> None:
        self.homeserver = homeserver.rstrip("/")
        self.room_id = room_id
        self.http = httpx.AsyncClient(timeout=30)
        self.access_token = ""
        self.next_batch: str | None = None
        self.seen_events: dict[str, dict[str, Any]] = {}

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

    async def create_public_room(self) -> None:
        """Create and select one disposable public room."""
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
        room = joined.get(self.room_id, {}) if isinstance(joined, dict) else {}
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
                    "limit": _RECOVERY_CLIFF_OBSERVER_PAGE_SIZE,
                },
            )
            if page.get("start") != cursor:
                msg = "recovery-cliff observer history page returned an unexpected start cursor"
                raise AssertionError(msg)
            page_events = self._raw_event_map(page.get("chunk"), source="room messages")
            recovered.update(page_events)
            next_cursor = page.get("end")
            if next_cursor is None:
                if page_events:
                    msg = "recovery-cliff observer history ended before proving interval exhaustion"
                    raise AssertionError(msg)
                return recovered
            if not isinstance(next_cursor, str) or not next_cursor:
                msg = "recovery-cliff observer history page returned an invalid end cursor"
                raise AssertionError(msg)
            if next_cursor == cursor:
                msg = "recovery-cliff observer history cursor did not advance"
                raise AssertionError(msg)
            if next_cursor in visited_cursors:
                msg = "recovery-cliff observer history cursor cycled"
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
        *,
        internal_relay_senders: Collection[str] = (),
    ) -> None:
        self.client = client
        self.agent_id = agent_id
        self.internal_relay_senders = frozenset(internal_relay_senders)
        self.internal_source_ids: set[str] = set()
        self.next_batch: str | None = None
        self.expected_sources: dict[str, str] = {}
        self.response_ids: dict[str, set[str]] = defaultdict(set)
        self.response_event_by_ref: dict[str, str] = {}
        self.seen_event_ids: set[str] = set()
        self._last_response_at = time.monotonic()
        self._last_progress_at = time.monotonic()

    async def initialize(self) -> None:
        """Establish a sync token before the fuzz traffic starts."""
        await self._sync_once(timeout_ms=0, allow_limited=True)

    def expect(self, logical_ref: str, event_id: str) -> None:
        """Require exactly one canonical agent reply to a source event."""
        self.expected_sources[event_id] = logical_ref

    def outstanding(self) -> dict[str, str]:
        """Return the expected sources that still owe exactly one reply."""
        return {
            event_id: logical_ref
            for event_id, logical_ref in self.expected_sources.items()
            if len(self.response_ids[event_id]) != 1
        }

    async def wait_until_exact(
        self,
        budget: WaitBudget,
        *,
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
        """Resolve a logical agent-response reference to its real event ID."""
        event_id = self.response_event_by_ref.get(response_ref)
        if event_id is None:
            msg = f"response event not observed for {response_ref!r}"
            raise KeyError(msg)
        return event_id

    async def _sync_once(self, *, timeout_ms: int, allow_limited: bool = False) -> None:
        data = await self.client.sync(self.next_batch, timeout_ms=timeout_ms)
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
        events = timeline.get("events", [])
        if not isinstance(events, list):
            return
        for raw_event in events:
            if isinstance(raw_event, dict):
                self._ingest_event(raw_event)

    def _ingest_event(self, event: Mapping[str, Any]) -> None:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in self.seen_event_ids:
            return
        self.seen_event_ids.add(event_id)
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
        first_reply_to_source = not self.response_ids[source_event_id]
        self.response_ids[source_event_id].add(event_id)
        logical_ref = self.expected_sources.get(source_event_id)
        if logical_ref is not None:
            self.response_event_by_ref[f"response:{logical_ref}"] = event_id
        self._last_response_at = time.monotonic()
        if first_reply_to_source and logical_ref is not None:
            # Progress is the outstanding set shrinking, not merely traffic.
            # A duplicate or a stray reply must never look like a lane that is
            # still working through its queue.
            self._last_progress_at = self._last_response_at

    def _assert_no_wrong_replies(self) -> None:
        duplicates = {
            self.expected_sources.get(source, source): sorted(event_ids)
            for source, event_ids in self.response_ids.items()
            if len(event_ids) > 1
        }
        unexpected = {
            source: sorted(event_ids)
            for source, event_ids in self.response_ids.items()
            if source not in self.expected_sources and source not in self.internal_source_ids
        }
        if duplicates or unexpected:
            msg = f"agent reply invariant failed: duplicates={duplicates}, unexpected={unexpected}"
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
        root_fanout: int = DEFAULT_ROOT_FANOUT,
    ) -> None:
        self.stack = stack
        self.clients = clients
        self.client = clients[0]
        self.scenario = scenario
        self.reply_timeout = reply_timeout
        self.settle_seconds = settle_seconds
        self.root_fanout = root_fanout
        self.latency = TurnLatencyMonitor()
        self.oracle = ExactReplyOracle(
            self.client,
            stack.agent_id,
            internal_relay_senders=(stack.router_id,),
        )
        self.event_ids: dict[str, str] = {}
        self.sent_payloads: dict[str, _SentPayload] = {}
        self.operation_count = 0
        self.restart_count = 0
        self.crash_count = 0
        self.interruptions_with_work_outstanding = 0
        self.executed_batches = 0
        self.slow_wait_extensions = 0

    async def run(self) -> dict[str, float | int | str]:
        """Execute every batch and enforce the reply invariant after each."""
        if self.scenario.profile == "recovery-cliff":
            return await self._run_recovery_cliff()
        if self.scenario.profile == "sustained-stream-capacity":
            return await self._run_sustained_stream_capacity()
        await asyncio.gather(*(client.register() for client in self.clients))
        await asyncio.gather(*(client.join_room() for client in self.clients))
        if self.scenario.profile == "restart-regression":
            return await self._run_restart_regression()
        if self.scenario.profile == "short-stream-correctness":
            await asyncio.gather(
                *(client.sync_incremental(timeout_ms=0, allow_limited=True) for client in self.clients),
            )
            return await self._run_short_stream_correctness()

        await self.oracle.initialize()
        await self._await_first_baseline_response()
        await self._send_roots(range(self.scenario.thread_count))
        return await self._run_batches(
            self.scenario.batches,
        )

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

        async with asyncio.timeout(self._recovery_cliff_remaining(deadline)):
            released = await asyncio.gather(
                *(send_root(thread) for thread in range(self.scenario.thread_count)),
            )
        roots = sorted(released)
        return tuple(event_id for _thread, event_id in roots)

    async def _release_recovery_cliff_load(
        self,
        *,
        run_id: str,
        deadline: float,
        shape: RecoveryCliffFaultShape,
    ) -> tuple[str, ...]:
        """Hold one causal history gap, release the root barrier, and resume."""

        async def send_context(index: int) -> str:
            return await self.client.send_event(
                "m.room.message",
                f"recovery-cliff-context-{run_id}-{index}",
                {
                    "msgtype": "m.notice",
                    "body": f"Recovery cliff context run={run_id} event={index}",
                    "m.mentions": {"user_ids": []},
                },
            )

        await asyncio.to_thread(
            self.stack.pause_mindroom,
            timeout=self._recovery_cliff_remaining(deadline),
        )
        try:
            async with asyncio.timeout(self._recovery_cliff_remaining(deadline)):
                await asyncio.gather(*(send_context(index) for index in range(shape.context_event_count)))
            return await self._release_managed_roots(
                run_id=run_id,
                deadline=deadline,
                transaction_prefix="recovery-cliff-root",
                body_prefix="Recovery cliff",
            )
        finally:
            self.stack.resume_mindroom()

    async def _release_sustained_stream_capacity_roots(
        self,
        *,
        run_id: str,
        deadline: float,
        health_samples: list[RecoveryCliffHealthSample],
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

        async def observe_initial_health() -> RecoveryCliffHealthSample:
            health_observation_started.set()
            return await self._recovery_cliff_observer_step(
                deadline=deadline,
                health_samples=health_samples,
            )

        initial_health_task: asyncio.Task[RecoveryCliffHealthSample] | None = None
        try:
            async with asyncio.timeout(self._recovery_cliff_remaining(deadline)):
                await launch_barrier.all_entered.wait()
            initial_health_task = asyncio.create_task(observe_initial_health())
            await health_observation_started.wait()
            launch_barrier.release_sends.set()
            await initial_health_task
            while not release_task.done():
                await self._recovery_cliff_observer_step(
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

    def _recovery_cliff_audit(
        self,
        *,
        baseline_event_ids: frozenset[str],
        expected_source_ids: Collection[str],
    ) -> RecoveryCliffTerminalAudit:
        """Audit the single observer cursor against exact workload sources."""
        return audit_recovery_cliff_events(
            tuple(event for event_id, event in self.client.seen_events.items() if event_id not in baseline_event_ids),
            responder_id=self.stack.agent_id,
            expected_source_ids=expected_source_ids,
        )

    def _recovery_cliff_log_counts(self) -> RecoveryCliffLogCounts:
        """Read exact recovery lifecycle counters for the managed responder room."""
        return RecoveryCliffLogCounts(
            delivery_retry_markers=self.stack.log_count(
                "Waiting to retry Matrix delivery after sync recovery",
                f"room_id={self.client.room_id}",
            ),
            delivery_worker_markers=self.stack.log_count(
                "Resent unacknowledged deliveries",
                f"agent={AGENT_NAME}",
            ),
            recovery_abandonment_markers=self.stack.log_count(
                "Abandoning",
                self.client.room_id,
            ),
        )

    async def _prepare_recovery_cliff_baseline(self, *, run_id: str) -> RecoveryCliffBaseline:
        """Complete one warm turn before snapshotting observer and log state."""
        await self.client.sync_incremental(timeout_ms=0, allow_limited=True)
        warm_baseline = frozenset(self.client.seen_events)
        warm_event_id = await self.client.send_event(
            "m.room.message",
            f"recovery-cliff-warm-up-{run_id}",
            self._message_content(f"Recovery cliff warm up run={run_id}"),
        )
        await self._wait_for_recovery_cliff_terminals(
            baseline_event_ids=warm_baseline,
            expected_source_ids=(warm_event_id,),
            deadline=time.monotonic() + self.reply_timeout,
            health_samples=[],
        )
        return RecoveryCliffBaseline(
            event_ids=frozenset(self.client.seen_events),
            log_counts=self._recovery_cliff_log_counts(),
        )

    @staticmethod
    def _recovery_cliff_terminal_ready(audit: RecoveryCliffTerminalAudit) -> bool:
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
    def _recovery_cliff_assert_no_terminal_corruption(audit: RecoveryCliffTerminalAudit) -> None:
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
            raise AssertionError("recovery-cliff terminal corruption: " + "; ".join(failures))

    @staticmethod
    def _recovery_cliff_remaining(deadline: float) -> float:
        """Return the remaining fixed SLA without extending it."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            msg = "recovery-cliff fixed service-level deadline expired"
            raise TimeoutError(msg)
        return remaining

    async def _recovery_cliff_observer_step(
        self,
        *,
        deadline: float,
        health_samples: list[RecoveryCliffHealthSample],
    ) -> RecoveryCliffHealthSample:
        """Sample runtime health and advance the one strict raw-event cursor."""
        remaining = self._recovery_cliff_remaining(deadline)
        async with asyncio.timeout(remaining):
            self.stack.require_runtime_alive()
            sample = await asyncio.to_thread(self.stack.recovery_health_sample)
            health_samples.append(sample)
            await self.client.sync_incremental_complete(
                timeout_ms=min(max(round(remaining * 1000), 0), 250),
            )
            self.stack.require_runtime_alive()
        return sample

    async def _wait_for_recovery_cliff_terminals(
        self,
        *,
        baseline_event_ids: frozenset[str],
        expected_source_ids: Collection[str],
        deadline: float,
        health_samples: list[RecoveryCliffHealthSample],
        debt_samples: list[int] | None = None,
    ) -> RecoveryCliffTerminalAudit:
        """Wait under one absolute deadline for exact completed responses."""
        while True:
            if debt_samples is not None:
                async with asyncio.timeout(self._recovery_cliff_remaining(deadline)):
                    debt_samples.append(
                        await asyncio.to_thread(
                            self.stack.recovery_outbox_debt,
                            expected_source_ids,
                        ),
                    )
            audit = self._recovery_cliff_audit(
                baseline_event_ids=baseline_event_ids,
                expected_source_ids=expected_source_ids,
            )
            self._recovery_cliff_assert_no_terminal_corruption(audit)
            if self._recovery_cliff_terminal_ready(audit):
                return audit
            await self._recovery_cliff_observer_step(
                deadline=deadline,
                health_samples=health_samples,
            )

    async def _wait_for_recovery_cliff_drain(
        self,
        *,
        baseline_event_ids: frozenset[str],
        expected_source_ids: Collection[str],
        deadline: float,
        health_samples: list[RecoveryCliffHealthSample],
    ) -> RecoveryCliffDrainCounts:
        """Wait for exact durable drain while continuing strict observation."""
        while True:
            async with asyncio.timeout(self._recovery_cliff_remaining(deadline)):
                counts = await asyncio.to_thread(self.stack.recovery_drain_counts)
            audit = self._recovery_cliff_audit(
                baseline_event_ids=baseline_event_ids,
                expected_source_ids=expected_source_ids,
            )
            self._recovery_cliff_assert_no_terminal_corruption(audit)
            if counts == RecoveryCliffDrainCounts(0, 0):
                return counts
            await self._recovery_cliff_observer_step(
                deadline=deadline,
                health_samples=health_samples,
            )

    async def _wait_for_recovery_cliff_fence(
        self,
        *,
        target_event_id: str,
        run_id: str,
        deadline: float,
        health_samples: list[RecoveryCliffHealthSample],
    ) -> tuple[bool, datetime, datetime]:
        """Require one exact settled reaction and later aggregated sync time."""
        pre_fence_sample = await self._recovery_cliff_observer_step(
            deadline=deadline,
            health_samples=health_samples,
        )
        if pre_fence_sample.last_sync_time is None:
            msg = "recovery-cliff pre-fence health omitted last_sync_time"
            raise AssertionError(msg)
        async with asyncio.timeout(self._recovery_cliff_remaining(deadline)):
            reaction_event_id = await self.client.send_event(
                "m.reaction",
                f"recovery-cliff-fence-{run_id}",
                {
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": target_event_id,
                        "key": "recovery-cliff-fence",
                    },
                },
            )
        while True:
            async with asyncio.timeout(self._recovery_cliff_remaining(deadline)):
                reaction_settled = (
                    await asyncio.to_thread(
                        self.stack.recovery_reaction_state,
                        reaction_event_id,
                    )
                    == "settled"
                )
            sample = await self._recovery_cliff_observer_step(
                deadline=deadline,
                health_samples=health_samples,
            )
            sync_advanced = (
                sample.last_sync_time is not None and sample.last_sync_time > pre_fence_sample.last_sync_time
            )
            if reaction_settled and sync_advanced:
                return True, pre_fence_sample.last_sync_time, sample.last_sync_time

    def _recovery_cliff_pass_result(
        self,
        observation: RecoveryCliffObservation,
        *,
        shape: RecoveryCliffFaultShape,
    ) -> dict[str, float | int | str]:
        """Render a self-evidencing machine-readable recovery-cliff PASS."""
        audit = observation.terminal_audit
        drain = observation.drain
        return {
            "profile": self.scenario.profile,
            "status": "PASS",
            "roots": observation.root_count,
            "context_events": shape.context_event_count,
            "held_events": shape.context_event_count + shape.root_count,
            "burst_events": shape.context_event_count + shape.root_count,
            "canonical_agent_replies": audit.canonical_response_count,
            "delivery_retry_markers": observation.delivery_retry_markers,
            "peak_unacknowledged_final_outbox_rows": observation.peak_unacknowledged_final_outbox_rows,
            "delivery_worker_markers": observation.delivery_worker_markers,
            "recovery_abandonment_markers": observation.recovery_abandonment_markers,
            "max_active_stream_seconds": round(audit.max_active_stream_seconds, 3),
            "full_overlap_seconds": round(audit.full_overlap_seconds, 3),
            "peak_active_streams": audit.peak_active_streams,
            "pending_journal_rows": drain.pending_journal_rows,
            "unacknowledged_outbox_rows": drain.unacknowledged_outbox_rows,
            "health_checks": len(observation.health_samples),
            "watchdog_stalls": observation.watchdog_stalls,
            "post_load_sync_advanced": (
                observation.pre_fence_last_sync is not None
                and observation.post_fence_last_sync is not None
                and observation.post_fence_last_sync > observation.pre_fence_last_sync
            ),
            "clean_shutdown": observation.clean_shutdown,
        }

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
        baseline = await self._prepare_recovery_cliff_baseline(run_id=run_id)
        watchdog_stalls_before = self.stack.log_count("matrix_sync_watchdog_stalled")
        durable_drain_failure_markers_before = self.stack.restart_shutdown_failure_count()

        deadline = time.monotonic() + self.reply_timeout
        phase_started = time.monotonic()
        health_samples: list[RecoveryCliffHealthSample] = []
        root_release_health_sample_baseline = len(health_samples)
        source_event_ids = await self._release_sustained_stream_capacity_roots(
            run_id=run_id,
            deadline=deadline,
            health_samples=health_samples,
        )
        health_samples_while_root_release = len(health_samples) - root_release_health_sample_baseline
        await self._recovery_cliff_observer_step(
            deadline=deadline,
            health_samples=health_samples,
        )
        phase_durations = [("root_release", time.monotonic() - phase_started)]

        phase_started = time.monotonic()
        terminal_audit = await self._wait_for_recovery_cliff_terminals(
            baseline_event_ids=baseline.event_ids,
            expected_source_ids=source_event_ids,
            deadline=deadline,
            health_samples=health_samples,
        )
        phase_durations.append(("terminal_settlement", time.monotonic() - phase_started))

        phase_started = time.monotonic()
        durable_drain = await self._wait_for_recovery_cliff_drain(
            baseline_event_ids=baseline.event_ids,
            expected_source_ids=source_event_ids,
            deadline=deadline,
            health_samples=health_samples,
        )
        phase_durations.append(("durable_drain", time.monotonic() - phase_started))

        phase_started = time.monotonic()
        reaction_settled, pre_fence_last_sync, post_fence_last_sync = await self._wait_for_recovery_cliff_fence(
            target_event_id=terminal_audit.canonical_responses[0][1],
            run_id=run_id,
            deadline=deadline,
            health_samples=health_samples,
        )
        phase_durations.append(("reaction_fence", time.monotonic() - phase_started))

        phase_started = time.monotonic()
        durable_drain = await self._wait_for_recovery_cliff_drain(
            baseline_event_ids=baseline.event_ids,
            expected_source_ids=source_event_ids,
            deadline=deadline,
            health_samples=health_samples,
        )
        terminal_audit = self._recovery_cliff_audit(
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
        shutdown_remaining = self._recovery_cliff_remaining(deadline)
        async with asyncio.timeout(shutdown_remaining):
            clean_shutdown = await asyncio.to_thread(
                self.stack.stop_mindroom,
                timeout=min(20.0, shutdown_remaining),
            )
        phase_durations.append(("shutdown", time.monotonic() - shutdown_started))

        final_logs = self._recovery_cliff_log_counts()
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

    async def _run_recovery_cliff(self) -> dict[str, float | int | str]:
        """Exercise and evaluate the configured delivery recovery cliff."""
        await self._authenticate_managed_sender()
        run_id = secrets.token_hex(6)
        baseline = await self._prepare_recovery_cliff_baseline(run_id=run_id)
        shape = recovery_cliff_fault_shape(
            self.stack.config_path,
            root_count=self.scenario.thread_count,
        )
        deadline = time.monotonic() + self.reply_timeout
        source_event_ids = await self._release_recovery_cliff_load(
            run_id=run_id,
            deadline=deadline,
            shape=shape,
        )
        expected_source_ids = frozenset(source_event_ids)
        health_samples: list[RecoveryCliffHealthSample] = []
        debt_samples: list[int] = []
        terminal_audit = await self._wait_for_recovery_cliff_terminals(
            baseline_event_ids=baseline.event_ids,
            expected_source_ids=expected_source_ids,
            deadline=deadline,
            health_samples=health_samples,
            debt_samples=debt_samples,
        )
        drain = await self._wait_for_recovery_cliff_drain(
            baseline_event_ids=baseline.event_ids,
            expected_source_ids=expected_source_ids,
            deadline=deadline,
            health_samples=health_samples,
        )

        reaction_settled, pre_fence_last_sync, post_fence_last_sync = await self._wait_for_recovery_cliff_fence(
            target_event_id=terminal_audit.canonical_responses[0][1],
            run_id=run_id,
            deadline=deadline,
            health_samples=health_samples,
        )
        drain = await self._wait_for_recovery_cliff_drain(
            baseline_event_ids=baseline.event_ids,
            expected_source_ids=expected_source_ids,
            deadline=deadline,
            health_samples=health_samples,
        )
        terminal_audit = self._recovery_cliff_audit(
            baseline_event_ids=baseline.event_ids,
            expected_source_ids=expected_source_ids,
        )

        clean_shutdown = await asyncio.to_thread(self.stack.stop_mindroom)
        final_logs = self._recovery_cliff_log_counts()
        observation = RecoveryCliffObservation(
            root_count=self.scenario.thread_count,
            terminal_audit=terminal_audit,
            delivery_retry_markers=(final_logs.delivery_retry_markers - baseline.log_counts.delivery_retry_markers),
            peak_unacknowledged_final_outbox_rows=max(debt_samples, default=0),
            delivery_worker_markers=(final_logs.delivery_worker_markers - baseline.log_counts.delivery_worker_markers),
            recovery_abandonment_markers=(
                final_logs.recovery_abandonment_markers - baseline.log_counts.recovery_abandonment_markers
            ),
            drain=drain,
            health_samples=tuple(health_samples),
            watchdog_stalls=self.stack.log_count("matrix_sync_watchdog_stalled"),
            reaction_settled=reaction_settled,
            pre_fence_last_sync=pre_fence_last_sync,
            post_fence_last_sync=post_fence_last_sync,
            clean_shutdown=clean_shutdown,
        )
        failures = evaluate_recovery_cliff(observation)
        if failures:
            raise AssertionError("recovery-cliff acceptance failures:\n" + "\n".join(failures))
        return self._recovery_cliff_pass_result(observation, shape=shape)

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
        fresh_agent_response_ids = agent.get(fresh_event_id, set())
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
            thread_root=None,
            reply_to=None,
            expected_sources=expected_sources,
        )
        for batch in self.scenario.batches[:parallel_start]:
            operation = batch[0]
            _, hot_response = await self._short_stream_turn(
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
            root, response = await self._short_stream_turn(
                client,
                label=f"root:{thread}",
                thread_root=None,
                reply_to=None,
                expected_sources=expected_sources,
            )
            for batch in parallel_batches:
                operation = next(item for item in batch if item.thread == thread)
                _, response = await self._short_stream_turn(
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
        thread_root: str | None,
        reply_to: str | None,
        expected_sources: set[str],
    ) -> tuple[str, str]:
        """Send one old-harness turn and wait for its completed stream."""
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
        )
        txn_id = f"live-short-stream-{label}-{secrets.token_hex(4)}"
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
    ) -> dict[str, int | str]:
        """Run one contiguous scenario segment against already-created roots."""
        for relative_batch_index, batch in enumerate(batches):
            batch_index = batch_index_offset + relative_batch_index
            work = tuple(operation for operation in batch if operation.kind not in _INTERRUPTION_KINDS)
            results = await asyncio.gather(*(self._apply(operation) for operation in work))
            for operation, event_id, payload in results:
                self.operation_count += 1
                if event_id is not None and operation.kind is not LiveOperationKind.IDEMPOTENT_RETRY:
                    self.event_ids[operation.event_ref] = event_id
                if payload is not None:
                    self.sent_payloads[operation.event_ref] = payload
                if _owes_reply(operation):
                    assert event_id is not None
                    self.oracle.expect(operation.event_ref, event_id)
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
        if self.scenario.profile != "short-stream-correctness":
            return self.client
        client_index = max(thread - 1, 0)
        return self.clients[client_index]

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

        MindRoom's no-loss guarantee begins after its first baseline response,
        not when its API reports healthy. Until then the agent is still doing
        its initial Matrix sync, and everything in that first timeline is
        classified as room history the agent must not answer — correctly, since
        a bot joining a room may not reply to the backlog it finds there.

        Traffic sent before that boundary is therefore outside the contract,
        and a scenario that starts there measures the race rather than the
        behaviour under test. One warm-up exchange establishes that the agent
        is answering, which is the only observable that actually means it.
        """
        content = self._message_content("Live fuzz warm up")
        event_id = await self.client.send_event("m.room.message", "live-fuzz-warm-up", content)
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

        async def send_root(thread: int) -> tuple[int, str, _SentPayload]:
            logical_ref = f"root:{thread}"
            content = self._message_content(f"Live fuzz root {thread}")
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
            self.oracle.expect(logical_ref, event_id)
        await self._await_replies()

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
            )
            payload = _SentPayload("m.room.message", txn_id, content)
            event_id = await client.send_event(payload.event_type, txn_id, content)
            return operation, event_id, payload

        if operation.kind is LiveOperationKind.PLAIN_REPLY:
            content = self._message_content(
                f"Live fuzz plain reply {operation.operation_id}",
                relation={"m.in_reply_to": {"event_id": target_event_id}},
            )
            payload = _SentPayload("m.room.message", txn_id, content)
            event_id = await client.send_event(payload.event_type, txn_id, content)
            return operation, event_id, payload

        if operation.kind is LiveOperationKind.EDIT:
            new_content = self._message_content(f"Live fuzz edited message {operation.operation_id}")
            content = {
                **new_content,
                "m.new_content": new_content,
                "m.relates_to": {"rel_type": "m.replace", "event_id": target_event_id},
            }
            event_id = await client.send_event("m.room.message", txn_id, content)
            return operation, event_id, None

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
    parser.add_argument(
        "--profile",
        choices=(
            "fuzz",
            "restart-regression",
            "short-stream-correctness",
            "recovery-cliff",
            "sustained-stream-capacity",
        ),
        default="fuzz",
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--steps", type=_positive_int, default=200)
    parser.add_argument(
        "--threads",
        type=_positive_int,
        help="thread count (default: 45 for fuzz, 100 for recovery-cliff, 200 for sustained-stream-capacity)",
    )
    parser.add_argument("--max-batch-size", type=_positive_int, default=16)
    parser.add_argument("--restart-interval", type=_non_negative_int, default=100)
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
            "one fixed whole-workload non-extending SLA for recovery-cliff and sustained-stream-capacity "
            "(default: 60s fuzz and restart-regression; 180s short-stream-correctness, recovery-cliff, "
            "and sustained-stream-capacity)"
        ),
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
    root_fanout: int,
) -> dict[str, float | int | str]:
    client_count = scenario.thread_count - 1 if scenario.profile == "short-stream-correctness" else 1
    clients = tuple(LiveMatrixClient(stack.homeserver, stack.room_id) for _ in range(client_count))
    try:
        return await LiveFuzzRunner(
            stack,
            clients,
            scenario,
            reply_timeout=reply_timeout,
            settle_seconds=settle_seconds,
            root_fanout=root_fanout,
        ).run()
    finally:
        await asyncio.gather(*(client.close() for client in clients))


def _scenario_from_args(args: argparse.Namespace) -> LiveFuzzScenario:
    """Select one replay, fixed profile, or generated fuzz scenario."""
    if args.trace is not None:
        return LiveFuzzScenario.from_json(args.trace.read_text(encoding="utf-8"))
    if args.profile == "short-stream-correctness":
        return short_stream_correctness_scenario()
    if args.profile == "restart-regression":
        return restart_regression_scenario()
    if args.profile == "recovery-cliff":
        return recovery_cliff_scenario(root_count=args.threads or 100)
    if args.profile == "sustained-stream-capacity":
        return sustained_stream_capacity_scenario(root_count=args.threads or 200)
    return live_scenario_from_seed(
        args.seed,
        steps=args.steps,
        thread_count=args.threads or 45,
        max_batch_size=args.max_batch_size,
        restart_interval=args.restart_interval,
    )


def main() -> None:
    """Run one trace against a fresh disposable real-server stack."""
    args = _parse_args()
    scenario = _scenario_from_args(args)
    if args.save_trace is not None:
        args.save_trace.write_text(scenario.to_json() + "\n", encoding="utf-8")
    reply_timeout = args.reply_timeout
    if reply_timeout is None:
        reply_timeout = (
            180
            if scenario.profile in {"short-stream-correctness", "recovery-cliff", "sustained-stream-capacity"}
            else 60
        )
    host_load = collect_host_load_report()
    print(host_load.render(), file=sys.stderr, flush=True)

    stack = ManagedTuwunelStack(
        profile=scenario.profile,
        stream_segments=96 if scenario.profile == "short-stream-correctness" else 4,
        stream_delay=0.012 if scenario.profile == "short-stream-correctness" else 0.001,
        # The hard-restart latch spans the later checkpoint wait plus process scheduling.
        model_latch_timeout=reply_timeout * 3,
    )
    try:
        stack.start()
        result = asyncio.run(
            _run_live(
                stack,
                scenario,
                reply_timeout=reply_timeout,
                settle_seconds=args.settle_seconds,
                root_fanout=args.root_fanout,
            ),
        )
        if scenario.profile != "restart-regression":
            result["seed"] = args.seed if args.trace is None else "trace"
        payload: dict[str, object] = {**result, **stack.diagnostic_counts(), **host_load.as_dict()}
        print(json.dumps(payload, sort_keys=True))
    except Exception:
        print("Live Matrix fuzz trace:", file=sys.stderr)
        print(args.trace or scenario.to_json(), file=sys.stderr)
        print(f"host load at start: {host_load.render()}", file=sys.stderr)
        print(f"host load at failure: {collect_host_load_report().render()}", file=sys.stderr)
        print(json.dumps(stack.diagnostic_counts(), sort_keys=True), file=sys.stderr)
        if args.failure_log is not None and stack.log_path.exists():
            args.failure_log.write_text(
                stack.log_path.read_text(encoding="utf-8", errors="replace"),
                encoding="utf-8",
            )
        if scenario.profile != "restart-regression":
            log_tail = stack.log_tail()
            if log_tail:
                print("MindRoom log tail:", file=sys.stderr)
                print(log_tail, file=sys.stderr)
        raise
    finally:
        stack.close()


if __name__ == "__main__":
    main()
