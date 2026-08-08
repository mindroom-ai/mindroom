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
        if self.profile == "restart-regression":
            _validate_restart_regression_trace(self)
            return
        known_events = {f"root:{thread}" for thread in range(self.thread_count)}
        known_responses = {f"response:root:{thread}" for thread in range(self.thread_count)}
        message_events = set(known_events)
        operation_ids: set[int] = set()

        for batch in self.batches:
            if not batch:
                msg = "live Matrix fuzz batches must not be empty"
                raise ValueError(msg)
            restart_operations = [
                operation for operation in batch if operation.kind is LiveOperationKind.RESTART_MINDROOM
            ]
            if restart_operations and len(batch) != 1:
                msg = "MindRoom restart must be a singleton batch"
                raise ValueError(msg)
            reply_threads = [
                operation.thread
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
    if scenario.profile not in {"fuzz", "restart-regression", "saturation"}:
        msg = f"unsupported live Matrix fuzz profile {scenario.profile!r}"
        raise ValueError(msg)


def _validate_restart_regression_trace(scenario: LiveFuzzScenario) -> None:
    """Require the fixed restart profile to own its operations outside the trace."""
    if scenario.thread_count != 1 or scenario.batches:
        msg = "restart-regression profile requires its fixed empty trace"
        raise ValueError(msg)


def restart_regression_scenario() -> LiveFuzzScenario:
    """Return the fixed config-replacement regression trace."""
    scenario = LiveFuzzScenario(thread_count=1, batches=(), profile="restart-regression")
    scenario.validate()
    return scenario


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
    if operation.kind is LiveOperationKind.RESTART_MINDROOM:
        if operation.target is not None:
            msg = "MindRoom restart must not have a target"
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
        stream_segments: int = 4,
        stream_delay: float = 0.001,
        model_latch_timeout: float = 60.0,
    ) -> None:
        token = secrets.token_hex(4)
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
        """Restart only MindRoom while preserving its cache and Matrix account."""
        self.stop_mindroom()
        self._start_mindroom()

    def wait_for_blocked_restart_request(self, *, timeout: float) -> bool:
        """Wait until the pre-restart generation has an exact fresh request in flight."""
        return _ModelHandler.blocked_request_started.wait(timeout=timeout)

    def restart_mindroom_for_recovery(self, *, timeout: float) -> None:
        """Hard-stop an in-flight turn and boot a distinguishable recovery generation."""
        deadline = time.monotonic() + timeout
        process = self._mindroom_process
        if process is None:
            msg = "MindRoom is not running"
            raise RuntimeError(msg)
        if process.poll() is not None:
            msg = "MindRoom exited before the hard-restart boundary"
            raise RuntimeError(msg)
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=max(0, deadline - time.monotonic()))
        self._mindroom_process = None
        self._set_model_id(RECOVERED_MODEL_ID)
        _ModelHandler.blocked_request_release.set()
        self._start_mindroom(timeout=max(0, deadline - time.monotonic()))

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
        """Count saturation signals in the complete runtime output."""
        log = self.read_log()
        if not log:
            return {}
        return {
            "cache_coordinator_timeouts": _log_count(log, "thread_read_error=cache_coordinator_timeout"),
            "degraded_thread_reads": _log_count(log, "matrix_cache_thread_read_degraded"),
            "dispatch_read_timeouts": _log_count(log, "thread_read_error=dispatch_read_timeout"),
            "event_loop_stalls": _log_count(log, "event_loop_stall_detected"),
        }

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

    def agent_matrix_credentials(self) -> tuple[str, str] | None:
        """Return the managed agent's persisted access token and device ID."""
        state_path = self.storage_path / "matrix_state.yaml"
        if not state_path.is_file():
            return None
        state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
        accounts = state.get("accounts", {}) if isinstance(state, dict) else {}
        account = accounts.get(f"agent_{AGENT_NAME}") if isinstance(accounts, dict) else None
        if not isinstance(account, dict):
            return None
        access_token, device_id = account.get("access_token"), account.get("device_id")
        if not isinstance(access_token, str) or not isinstance(device_id, str):
            return None
        return access_token, device_id

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
        self.executed_batches = 0
        self.slow_wait_extensions = 0

    async def run(self) -> dict[str, int | str]:
        """Execute every batch and enforce the reply invariant after each."""
        await asyncio.gather(*(client.register() for client in self.clients))
        await asyncio.gather(*(client.join_room() for client in self.clients))
        if self.scenario.profile == "restart-regression":
            return await self._run_restart_regression()
        if self.scenario.profile == "saturation":
            await asyncio.gather(
                *(client.sync_incremental(timeout_ms=0, allow_limited=True) for client in self.clients),
            )
            return await self._run_saturation()

        await self.oracle.initialize()
        await self._await_first_baseline_response()
        await self._send_roots(range(self.scenario.thread_count))
        return await self._run_batches(
            self.scenario.batches,
        )

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

    async def _run_saturation(self) -> dict[str, int | str]:
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
            if batch[0].kind is LiveOperationKind.RESTART_MINDROOM:
                self.stack.restart_mindroom()
                self.restart_count += 1
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
                        self.oracle.expect(operation.event_ref, event_id)
            try:
                await self._await_replies()
            except AssertionError as exc:
                msg = f"{exc}\nfailure occurred after live batch {batch_index}"
                raise AssertionError(msg) from exc
            self.executed_batches += 1

        return {
            "batches": self.executed_batches,
            "canonical_agent_replies": len(self.oracle.expected_sources),
            "operations": self.operation_count,
            "restarts": self.restart_count,
            "roots": self.scenario.thread_count,
            "measured_turn_seconds": round(self.latency.per_turn_seconds, 3),
            "slow_wait_extensions": self.slow_wait_extensions,
            "status": "PASS",
        }

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
    parser.add_argument("--profile", choices=("fuzz", "restart-regression", "saturation"), default="fuzz")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--steps", type=_positive_int, default=200)
    parser.add_argument("--threads", type=_positive_int, default=45)
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
            "deadline for a single agent turn, and the floor under every multi-turn deadline "
            "(default: 60s fuzz and restart-regression, 180s saturation). Waits for N outstanding "
            "turns scale this from measured per-turn latency instead of using it flat."
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
) -> dict[str, int | str]:
    client_count = scenario.thread_count - 1 if scenario.profile == "saturation" else 1
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
    if args.profile == "saturation":
        return saturation_scenario()
    if args.profile == "restart-regression":
        return restart_regression_scenario()
    return live_scenario_from_seed(
        args.seed,
        steps=args.steps,
        thread_count=args.threads,
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
        reply_timeout = 180 if scenario.profile == "saturation" else 60
    host_load = collect_host_load_report()
    print(host_load.render(), file=sys.stderr, flush=True)

    stack = ManagedTuwunelStack(
        stream_segments=96 if scenario.profile == "saturation" else 4,
        stream_delay=0.012 if scenario.profile == "saturation" else 0.001,
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
