"""Tests for replayable real-server Matrix fuzz traces and their oracle."""

from __future__ import annotations

import asyncio
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import httpx
import pytest
import yaml
from nio.durable.store import DurableStore

from mindroom.config.main import Config
from mindroom.event_journal import DeliveryStage, EventClass, EventJournalStore, EventKind, InboundEvent
from mindroom.event_journal.schema import SQLITE_DIALECT, schema_statements
from mindroom.matrix.conversation_hydration import ConversationHydrator
from scripts.testing import fuzz_live_matrix
from scripts.testing.fuzz_live_matrix import (
    DEFAULT_ROOT_FANOUT,
    DIAGNOSTIC_MARKERS,
    ORDERLY_SHUTDOWN_MARKER,
    PROJECT_ROOT,
    RESTART_SHUTDOWN_FAILURE_MARKER,
    ExactReplyOracle,
    ExactReplyTimeoutError,
    HostLoadReport,
    JournalRow,
    LiveFuzzRunner,
    LiveFuzzScenario,
    LiveMatrixClient,
    LiveOperation,
    LiveOperationKind,
    ManagedStreamDrainCounts,
    ManagedStreamHealthSample,
    ManagedTuwunelStack,
    MissingReplyStage,
    OutboxRow,
    RestartRegressionObservation,
    SlowWaitNotice,
    SustainedStreamCapacityObservation,
    SustainedStreamCapacitySourceAudit,
    TurnLatencyMonitor,
    WaitBudget,
    _log_count,
    _ModelHandler,
    _restart_prompt_observation,
    _semantic_ingress_markers,
    audit_managed_stream_events,
    classify_missing_reply,
    collect_host_load_report,
    evaluate_restart_regression,
    evaluate_sustained_stream_capacity,
    live_scenario_from_seed,
    restart_regression_scenario,
    short_stream_correctness_scenario,
    sustained_stream_capacity_scenario,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


import io
import json
import shutil
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import scripts.testing.fuzz_live_matrix as live_fuzz
from mindroom.constants import SOURCE_KIND_KEY
from mindroom.dispatch_source import AUTO_RESUME_MESSAGE, TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.handled_turns import TurnRecord, TurnRecordCodec
from mindroom.streaming import RESTART_INTERRUPTED_RESPONSE_NOTE
from mindroom.turn_record import RevisionReplay
from mindroom.turn_store import TurnStore, TurnStoreDeps
from scripts.testing.fuzz_live_matrix import (
    ORIGINAL_REVISION,
    ChaosTuning,
    FailureBundle,
    FinalStateAuditor,
    _body_call_id,
    _parse_markers,
    _persist_failure_bundle,
    _run_command,
    _sanitized_oracle_snapshot,
    _SentPayload,
    _SentRecord,
    _source_marker,
    _validated_child_provenance,
    chaos_scenario_from_seed,
    saturation_scenario,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterator


class _RecordingDormantClient:
    room_id = "!restart:example"
    user_id = "@sender:example"

    def __init__(self) -> None:
        self.sent_payloads: list[tuple[str, str, dict[str, Any]]] = []

    @property
    def sent_txn_ids(self) -> list[str]:
        return [txn_id for _event_type, txn_id, _content in self.sent_payloads]

    async def create_public_room(self) -> None:
        return

    async def send_event(
        self,
        event_type: str,
        txn_id: str,
        content: dict[str, Any],
        *,
        room_id: str | None = None,
    ) -> str:
        del room_id
        self.sent_payloads.append((event_type, txn_id, content))
        return f"${txn_id}"


class _ManagedStreamBoundaryClient:
    """Fail if managed load falls through to disposable registration."""

    room_id = "!recovery:example"
    _REGISTER_MESSAGE = "managed load must use persisted managed credentials"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.seen_events: dict[str, dict[str, Any]] = {}
        self.sync_calls = 0
        self.complete_sync_calls = 0

    async def register(self) -> None:
        self.calls.append("register")
        raise AssertionError(self._REGISTER_MESSAGE)

    async def join_room(self) -> None:
        self.calls.append("join_room")

    async def send_event(self, event_type: str, txn_id: str, content: dict[str, Any]) -> str:
        del event_type, txn_id, content
        self.calls.append("send_event")
        return "$sent"

    async def sync_incremental(self, *, timeout_ms: int, allow_limited: bool = False) -> None:
        del timeout_ms, allow_limited
        self.sync_calls += 1

    async def sync_incremental_complete(self, *, timeout_ms: int) -> None:
        del timeout_ms
        self.complete_sync_calls += 1


class _ManagedStreamLaunchBarrierClient:
    """Release sends only after the complete cliff burst has entered."""

    room_id = "!recovery:example"

    def __init__(self, expected_sends: int, *, finish: bool = True) -> None:
        self.expected_sends = expected_sends
        self.finish = finish
        self.all_entered = asyncio.Event()
        self.never = asyncio.Event()
        self.sent_payloads: list[tuple[str, str, dict[str, Any]]] = []

    async def send_event(self, event_type: str, txn_id: str, content: dict[str, Any]) -> str:
        self.sent_payloads.append((event_type, txn_id, content))
        if len(self.sent_payloads) == self.expected_sends:
            self.all_entered.set()
        await self.all_entered.wait()
        if not self.finish:
            await self.never.wait()
        return f"${txn_id}"


class _StaticObservationClient:
    room_id = "!restart:example"

    def __init__(self, events: tuple[dict[str, Any], ...]) -> None:
        self.seen_events = {event["event_id"]: event for event in events}

    async def sync_incremental(self, *, timeout_ms: int, allow_limited: bool = False) -> None:
        del timeout_ms, allow_limited
        await asyncio.sleep(0.001)


class _RestartBoundaryStack(ManagedTuwunelStack):
    """Deterministic stack seam for the hard-restart ordering contract."""

    def __init__(self) -> None:
        super().__init__()
        self.agent_id, self.router_id = "@agent:example", "@router:example"
        self.order: list[str] = []
        self.checkpoint_ready = True

    def apply_replacement_config(self, room_id: str) -> None:
        assert room_id == "!restart:example"

    def wait_for_log_count(self, markers: tuple[str, ...], minimum: int, timeout: float = 60) -> bool:
        assert minimum >= 1
        assert timeout == 1
        if markers == (
            "Received message",
            "agent=general",
            "room_id=!restart:example",
            "event_id=$restart-fresh",
        ):
            self.order.append("durable-callback")
        return True

    def projected_restart_event_pair_count(self, room_id: str, event_ids: tuple[str, str]) -> int:
        assert room_id == "!restart:example"
        assert event_ids == ("$restart-old-text", "$restart-old-media")
        return 4

    def wait_for_restart_journal_event_state(
        self,
        event_id: str,
        *,
        expected: str | frozenset[str],
        timeout: float,
    ) -> bool:
        assert event_id == "$restart-fresh"
        assert expected == frozenset({"pending"})
        assert timeout == 1
        self.order.append("obligation-pending")
        return True

    def wait_for_blocked_restart_request(self, *, timeout: float) -> bool:
        assert timeout == 1
        self.order.append("model-in-flight")
        return True

    def wait_for_restart_event_checkpoint(self, room_id: str, event_id: str, *, timeout: float) -> bool:
        assert (room_id, event_id, timeout) == ("!restart:example", "$restart-fresh", 1)
        self.order.append("sync-checkpoint")
        return self.checkpoint_ready

    def restart_mindroom_for_recovery(self, *, timeout: float) -> None:
        assert timeout == 1
        self.order.append("hard-restart")

    def log_count(self, *markers: str) -> int:
        assert markers
        return 1


class _RestartBoundaryRunner(LiveFuzzRunner):
    """Return settled evidence after exercising the real pre-restart sequence."""

    async def _wait_for_restart_observation(
        self,
        dormant: LiveMatrixClient,
        *,
        historical_event_ids: tuple[str, str],
        fresh_event_id: str,
        fresh_semantic_ingress_count_before_restart: int,
    ) -> RestartRegressionObservation:
        assert dormant.room_id == "!restart:example"
        assert historical_event_ids == ("$restart-old-text", "$restart-old-media")
        assert fresh_event_id == "$restart-fresh"
        assert fresh_semantic_ingress_count_before_restart == 1
        return RestartRegressionObservation(
            historical_output_counts=(0, 0),
            historical_callback_counts=(0, 0),
            projected_after_answer_count=0,
            historical_projected_on_room_read=0,
            fresh_agent_output_count=1,
            fresh_router_output_count=0,
            fresh_response_complete=True,
            fresh_semantic_ingress_count_before_restart=1,
            fresh_semantic_ingress_count=2,
            recovered_generation_response_observed=True,
            fresh_obligation_recovered=True,
            fresh_prompt_observed=True,
            historical_in_fresh_prompt=False,
            orderly_drain_completed=True,
        )

    async def _read_historical_room_projection(
        self,
        *,
        room_id: str,
        historical_event_ids: tuple[str, str],
    ) -> int:
        assert room_id == "!restart:example"
        assert historical_event_ids == ("$restart-old-text", "$restart-old-media")
        cast("_RestartBoundaryStack", self.stack).order.append("room-read")
        return 2


@pytest.mark.asyncio
async def test_restart_room_exposes_prejoin_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """The disposable room must expose old events to bots that join during replacement."""
    client = LiveMatrixClient("http://matrix.invalid", "")
    request: tuple[str, str, dict[str, Any]] | None = None

    async def record_request(
        method: str,
        path: str,
        *,
        json_body: dict[str, Any],
    ) -> dict[str, str]:
        nonlocal request
        request = method, path, json_body
        return {"room_id": "!restart:example"}

    monkeypatch.setattr(client, "_request", record_request)
    try:
        await client.create_public_room()
        assert client.room_id == "!restart:example"
        assert request == (
            "POST",
            "/_matrix/client/v3/createRoom",
            {
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
    finally:
        await client.close()


def _observer_event(event_id: str, status: str = "streaming") -> dict[str, Any]:
    """Build one raw event retained by the managed-stream observer."""
    return {
        "event_id": event_id,
        "origin_server_ts": 1,
        "sender": "@mindroom_general:example",
        "type": "m.room.message",
        "content": {"body": event_id, "io.mindroom.stream_status": status},
    }


@pytest.mark.asyncio
async def test_recovery_observer_enumerates_the_complete_positioned_sync_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forward messages, not a limited sync window, authoritatively retain every raw event."""
    client = LiveMatrixClient("http://matrix.invalid", "!recovery:example")
    client.next_batch = "s-before"
    client.seen_events = {"$known": _observer_event("$known", "completed")}
    omitted_original = _stream_original("$omitted-original", "$source", 1_000, "streaming")
    compacted_edit = _stream_edit(
        "$compacted-edit",
        "$omitted-original",
        48_000,
        "completed",
        outer_status="streaming",
    )
    requests: list[tuple[str, str, dict[str, str | int]]] = []
    pages = iter(
        (
            {
                "start": "s-before",
                "end": "p-one",
                "chunk": [omitted_original],
            },
            {
                "start": "p-one",
                "end": "p-two",
                "chunk": [],
            },
            {
                "start": "p-two",
                "end": "p-three",
                "chunk": [
                    compacted_edit,
                    _observer_event("$newest"),
                ],
            },
            {"start": "p-three", "chunk": []},
        ),
    )

    async def limited_sync(since: str | None, *, timeout_ms: int) -> dict[str, Any]:
        assert since == "s-before"
        assert timeout_ms == 250
        return {
            "next_batch": "s-after",
            "rooms": {
                "join": {
                    client.room_id: {
                        "timeline": {
                            "limited": True,
                            "prev_batch": "p-newest",
                            "events": [_observer_event("$newest")],
                        },
                    },
                },
            },
        }

    async def messages_request(
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str | int] | None = None,
    ) -> dict[str, Any]:
        assert json_body is None
        assert params is not None
        requests.append((method, path, dict(params)))
        return next(pages)

    monkeypatch.setattr(client, "sync", limited_sync)
    monkeypatch.setattr(client, "_request", messages_request)
    try:
        await client.sync_incremental_complete(timeout_ms=250)

        assert client.next_batch == "s-after"
        assert set(client.seen_events) == {
            "$known",
            "$compacted-edit",
            "$omitted-original",
            "$newest",
        }
        assert client.seen_events["$compacted-edit"] == compacted_edit
        audit = audit_managed_stream_events(
            (
                client.seen_events["$omitted-original"],
                client.seen_events["$compacted-edit"],
            ),
            responder_id="@mindroom_general:example",
            expected_source_ids=("$source",),
        )
        assert audit.canonical_responses == (("$source", "$omitted-original"),)
        assert audit.noncompleted_sources == ()
        assert requests == [
            (
                "GET",
                "/_matrix/client/v3/rooms/%21recovery%3Aexample/messages",
                {"dir": "f", "from": "s-before", "to": "s-after", "limit": 500},
            ),
            (
                "GET",
                "/_matrix/client/v3/rooms/%21recovery%3Aexample/messages",
                {"dir": "f", "from": "p-one", "to": "s-after", "limit": 500},
            ),
            (
                "GET",
                "/_matrix/client/v3/rooms/%21recovery%3Aexample/messages",
                {"dir": "f", "from": "p-two", "to": "s-after", "limit": 500},
            ),
            (
                "GET",
                "/_matrix/client/v3/rooms/%21recovery%3Aexample/messages",
                {"dir": "f", "from": "p-three", "to": "s-after", "limit": 500},
            ),
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pages", "failure"),
    [
        (
            ({"start": "s-before", "end": "s-before", "chunk": []},),
            "did not advance",
        ),
        (
            (
                {"start": "s-before", "end": "p-next", "chunk": [_observer_event("$new")]},
                {"start": "p-next", "end": "s-before", "chunk": []},
            ),
            "cycled",
        ),
        (
            ({"start": "s-before", "end": 7, "chunk": [_observer_event("$new")]},),
            "end cursor",
        ),
        (
            ({"start": "wrong-position", "chunk": []},),
            "start cursor",
        ),
        (
            ({"start": "s-before", "chunk": [_observer_event("$new")]},),
            "ended before proving",
        ),
        (
            (
                {"start": "s-before", "end": "s-after", "chunk": [_observer_event("$new")]},
                {"start": "s-after", "chunk": [_observer_event("$omitted")]},
            ),
            "ended before proving",
        ),
    ],
)
async def test_recovery_observer_rejects_stalled_or_cyclic_history_without_mutating_cursor(
    monkeypatch: pytest.MonkeyPatch,
    pages: tuple[dict[str, Any], ...],
    failure: str,
) -> None:
    """A bad interval cannot partially publish staged raw events or the sync cursor."""
    client = LiveMatrixClient("http://matrix.invalid", "!recovery:example")
    client.next_batch = "s-before"
    known = _observer_event("$known", "completed")
    client.seen_events = {"$known": known}
    scripted_pages = iter(pages)

    async def limited_sync(_since: str | None, *, timeout_ms: int) -> dict[str, Any]:
        assert timeout_ms == 250
        return {
            "next_batch": "s-after",
            "rooms": {
                "join": {
                    client.room_id: {
                        "timeline": {
                            "limited": True,
                            "prev_batch": "p-newest",
                            "events": [_observer_event("$newest")],
                        },
                    },
                },
            },
        }

    async def messages_request(
        _method: str,
        _path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str | int] | None = None,
    ) -> dict[str, Any]:
        assert json_body is None
        assert params is not None
        assert params["dir"] == "f"
        assert params["to"] == "s-after"
        return next(scripted_pages)

    monkeypatch.setattr(client, "sync", limited_sync)
    monkeypatch.setattr(client, "_request", messages_request)
    try:
        with pytest.raises(AssertionError, match=failure):
            await client.sync_incremental_complete(timeout_ms=250)

        assert client.next_batch == "s-before"
        assert client.seen_events == {"$known": known}
    finally:
        await client.close()


def _restart_response(
    event_id: str,
    sender: str,
    source: str,
    *,
    body: str = "LIVE-FUZZ runtime-generation=recovered END call=1",
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "sender": sender,
        "type": "m.room.message",
        "content": {
            "body": body,
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": source,
                "m.in_reply_to": {"event_id": source},
            },
        },
    }


_RESTART_OBSERVATION_LOG = (
    "Received message agent=general event_id=$fresh room_id=!restart:example\n"
    "Received message agent=general event_id=$fresh room_id=!restart:example\n"
    "Preparing agent and prompt agent=general $fresh\n"
)


@pytest.fixture
def seeded_restart_observation_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[ManagedTuwunelStack, list[float]]]:
    """Yield one fully seeded observation seam with exact shutdown calls."""
    stack = ManagedTuwunelStack()
    stop_calls: list[float] = []
    stack.agent_id, stack.router_id = "@agent:example", "@router:example"
    monkeypatch.setattr(stack, "projected_restart_event_pair_count", lambda _room_id, _event_ids: 4)
    monkeypatch.setattr(stack, "restart_journal_event_state", lambda _event_id: "settled")

    def record_stop(*, timeout: float = 20) -> bool:
        stop_calls.append(timeout)
        return True

    monkeypatch.setattr(stack, "stop_mindroom", record_stop)
    try:
        yield stack, stop_calls
    finally:
        stack.close()


async def _collect_seeded_restart_observation(
    stack: ManagedTuwunelStack,
    *,
    log: str,
    events: tuple[dict[str, Any], ...],
    reply_timeout: float = 0.05,
) -> RestartRegressionObservation:
    """Run the shared exact restart-observation seam."""
    stack.log_path.write_text(log, encoding="utf-8")
    dormant = _StaticObservationClient(events)
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", dormant),),
        restart_regression_scenario(),
        reply_timeout=reply_timeout,
        settle_seconds=0,
    )
    return await runner._wait_for_restart_observation(
        cast("LiveMatrixClient", dormant),
        historical_event_ids=("$old-text", "$old-media"),
        fresh_event_id="$fresh",
        fresh_semantic_ingress_count_before_restart=1,
    )


def test_live_scenario_is_deterministic_and_json_replayable() -> None:
    """A seed must produce a stable trace that survives JSON round-tripping."""
    scenario = live_scenario_from_seed(
        42,
        steps=250,
        thread_count=12,
        max_batch_size=10,
        restart_interval=75,
    )

    assert scenario == live_scenario_from_seed(
        42,
        steps=250,
        thread_count=12,
        max_batch_size=10,
        restart_interval=75,
    )
    interruption_kinds = {LiveOperationKind.RESTART_MINDROOM, LiveOperationKind.CRASH_MINDROOM}
    assert LiveFuzzScenario.from_json(scenario.to_json()) == scenario
    assert sum(operation.kind not in interruption_kinds for batch in scenario.batches for operation in batch) == 250
    assert {
        operation.kind for batch in scenario.batches for operation in batch if operation.kind in interruption_kinds
    } == interruption_kinds
    for batch in scenario.batches:
        reply_threads = [
            operation.thread
            for operation in batch
            if operation.kind
            in {
                LiveOperationKind.THREAD_MESSAGE,
                LiveOperationKind.PLAIN_REPLY,
            }
        ]
        assert len(reply_threads) == len(set(reply_threads))


def test_live_scenario_schedules_every_interruption_inside_unfinished_work() -> None:
    """An interruption in a batch of its own can only ever hit an idle process.

    The runner drains before every batch, so a singleton restart batch is
    taken after the previous batch's replies have all landed. Scheduling the
    interruption as the tail of a batch that owes a reply is what puts it
    where the journal's guarantee lives, and alternating graceful restarts
    with hard crashes is what stops a run from proving only that the drain
    works.
    """
    kinds = {LiveOperationKind.RESTART_MINDROOM, LiveOperationKind.CRASH_MINDROOM}
    scenario = live_scenario_from_seed(3, steps=200, thread_count=8, max_batch_size=6, restart_interval=25)
    interrupted = [batch for batch in scenario.batches if any(operation.kind in kinds for operation in batch)]

    assert len(interrupted) == 8
    for batch in interrupted:
        assert batch[-1].kind in kinds
        assert sum(operation.kind in kinds for operation in batch) == 1
        assert any(
            operation.kind in {LiveOperationKind.THREAD_MESSAGE, LiveOperationKind.PLAIN_REPLY} for operation in batch
        )
    assert [batch[-1].kind for batch in interrupted] == [
        LiveOperationKind.RESTART_MINDROOM,
        LiveOperationKind.CRASH_MINDROOM,
    ] * 4


@pytest.mark.parametrize(
    ("batch", "expected"),
    [
        pytest.param(
            (LiveOperation(0, LiveOperationKind.RESTART_MINDROOM, 0, None),),
            "must interrupt a batch that owes at least one reply",
            id="alone",
        ),
        pytest.param(
            (
                LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "root:0"),
                LiveOperation(1, LiveOperationKind.RESTART_MINDROOM, 0, None),
                LiveOperation(2, LiveOperationKind.REACTION, 0, "root:0"),
            ),
            "must be the last operation of exactly one batch",
            id="not-last",
        ),
        pytest.param(
            (
                LiveOperation(0, LiveOperationKind.REACTION, 0, "root:0"),
                LiveOperation(1, LiveOperationKind.RESTART_MINDROOM, 0, None),
            ),
            "must interrupt a batch that owes at least one reply",
            id="no-reply-owed",
        ),
    ],
)
def test_live_scenario_rejects_a_restart_that_interrupts_nothing(
    batch: tuple[LiveOperation, ...],
    expected: str,
) -> None:
    """The trace has to say the restart lands mid-turn; the runner cannot rescue it."""
    scenario = LiveFuzzScenario(thread_count=1, batches=(batch,))

    with pytest.raises(ValueError, match=expected):
        scenario.validate()


def test_live_scenario_generator_covers_every_matrix_mutation() -> None:
    """The weighted generator must reach every supported live operation."""
    seen = {
        operation.kind
        for seed in range(5)
        for batch in live_scenario_from_seed(
            seed,
            steps=200,
            thread_count=8,
            restart_interval=50,
        ).batches
        for operation in batch
    }

    assert seen == {
        LiveOperationKind.THREAD_MESSAGE,
        LiveOperationKind.PLAIN_REPLY,
        LiveOperationKind.EDIT,
        LiveOperationKind.REACTION,
        LiveOperationKind.REDACTION,
        LiveOperationKind.IDEMPOTENT_RETRY,
        LiveOperationKind.RESTART_MINDROOM,
        LiveOperationKind.CRASH_MINDROOM,
    }


def test_short_stream_correctness_scenario_matches_original_two_phase_workload() -> None:
    """Short-stream correctness preserves the old hot-then-parallel workload."""
    scenario = short_stream_correctness_scenario()

    assert scenario.profile == "short-stream-correctness"
    assert scenario.thread_count == 13
    assert len(scenario.batches) == 108
    assert all(len(batch) == 1 and batch[0].thread == 0 for batch in scenario.batches[:100])
    assert all([operation.thread for operation in batch] == list(range(1, 13)) for batch in scenario.batches[100:])


def test_sustained_stream_capacity_defaults_to_two_hundred_roots() -> None:
    """Ordinary capacity owns a fixed 200-root workload outside the trace."""
    assert sustained_stream_capacity_scenario() == LiveFuzzScenario(
        thread_count=200,
        batches=(),
        profile="sustained-stream-capacity",
    )


def test_cli_threads_override_sustained_stream_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operators can raise the fixed capacity workload without changing its trace."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["fuzz_live_matrix.py", "--profile", "sustained-stream-capacity", "--threads", "400"],
    )

    assert fuzz_live_matrix._scenario_from_args(fuzz_live_matrix._parse_args()).thread_count == 400


def test_fuzz_cli_keeps_its_default_thread_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Making recovery capacity configurable must leave ordinary fuzz at 45 threads."""
    monkeypatch.setattr(sys, "argv", ["fuzz_live_matrix.py"])

    scenario = fuzz_live_matrix._scenario_from_args(fuzz_live_matrix._parse_args())

    assert scenario.profile == "fuzz"
    assert scenario.thread_count == 45


def _stream_original(event_id: str, source_id: str, timestamp: int, status: str) -> dict[str, Any]:
    """Build one literal canonical Matrix response original."""
    return {
        "event_id": event_id,
        "origin_server_ts": timestamp,
        "sender": "@mindroom_general:example",
        "type": "m.room.message",
        "content": {
            "body": status,
            "io.mindroom.stream_status": status,
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": source_id,
                "m.in_reply_to": {"event_id": source_id},
            },
        },
    }


def _stream_edit(
    event_id: str,
    response_id: str,
    timestamp: int,
    status: str,
    *,
    sender: str = "@mindroom_general:example",
    outer_status: str | None = None,
    msgtype: str | None = None,
) -> dict[str, Any]:
    """Build one literal Matrix replacement with optional new-content precedence."""
    resolved_msgtype = msgtype or ("m.text" if status == "completed" else "m.notice")
    content: dict[str, Any] = {
        "body": status,
        "msgtype": resolved_msgtype,
        "io.mindroom.stream_status": outer_status or status,
        "m.relates_to": {"rel_type": "m.replace", "event_id": response_id},
        "m.new_content": {
            "body": status,
            "msgtype": resolved_msgtype,
            "io.mindroom.stream_status": status,
        },
    }
    return {
        "event_id": event_id,
        "origin_server_ts": timestamp,
        "sender": sender,
        "type": "m.room.message",
        "content": content,
    }


def _completed_managed_stream_events() -> tuple[dict[str, Any], ...]:
    """Return two overlapping production-shaped completed source streams."""
    return (
        _stream_original("$response-0", "$source-0", 1_000, "pending"),
        _stream_edit("$edit-0", "$response-0", 48_000, "completed", outer_status="streaming"),
        _stream_original("$response-1", "$source-1", 2_000, "streaming"),
        _stream_edit("$edit-1", "$response-1", 49_000, "completed"),
    )


@pytest.mark.asyncio
async def test_managed_stream_warm_completion_precedes_event_and_log_baselines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Warm output is excluded from workload audit and its markers are already baselined."""
    stack = ManagedTuwunelStack(profile="sustained-stream-capacity")
    stack.agent_id = "@mindroom_general:example"
    client = _ManagedStreamBoundaryClient()
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", client),),
        sustained_stream_capacity_scenario(root_count=1),
        reply_timeout=1,
        settle_seconds=0,
    )
    warm_completed = False
    log_queries: list[tuple[str, ...]] = []

    async def send_warm(_event_type: str, _txn_id: str, _content: dict[str, Any]) -> str:
        return "$warm"

    async def wait_for_warm(**_kwargs: object) -> object:
        nonlocal warm_completed
        client.seen_events["$warm-response"] = _stream_original(
            "$warm-response",
            "$warm",
            1_000,
            "completed",
        )
        warm_completed = True
        return object()

    def log_count(*markers: str) -> int:
        assert warm_completed
        log_queries.append(markers)
        return 5

    monkeypatch.setattr(client, "send_event", send_warm)
    monkeypatch.setattr(stack, "managed_room_baseline_ready", lambda: True)
    monkeypatch.setattr(runner, "_wait_for_managed_stream_terminals", wait_for_warm)
    monkeypatch.setattr(stack, "log_count", log_count)
    try:
        baseline = await runner._prepare_managed_stream_baseline(run_id="unit-run")
        client.seen_events.update(
            {
                event["event_id"]: event
                for event in (
                    _stream_original("$response", "$workload", 2_000, "streaming"),
                    _stream_edit("$terminal", "$response", 49_000, "completed"),
                )
            },
        )
        audit = runner._managed_stream_audit(
            baseline_event_ids=baseline.event_ids,
            expected_source_ids=("$workload",),
        )

        assert "$warm-response" in baseline.event_ids
        assert baseline.log_counts.recovery_abandonment_markers == 5
        assert log_queries == [("Abandoning", client.room_id)]
        assert audit.unexpected_sources == ()
        assert audit.canonical_responses == (("$workload", "$response"),)
    finally:
        stack.close()


def _valid_sustained_stream_capacity_observation() -> SustainedStreamCapacityObservation:
    """Build settled no-fault capacity evidence from hand-checked root sources."""
    source_ids = ("$source-0", "$source-1")
    before = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    after = datetime(2026, 8, 8, 12, 1, tzinfo=UTC)
    return SustainedStreamCapacityObservation(
        root_count=2,
        source_audit=SustainedStreamCapacitySourceAudit(
            expected_source_ids=source_ids,
            observed_source_ids=source_ids,
            missing_source_ids=(),
            duplicate_source_ids=(),
            unexpected_source_ids=(),
            invalid_source_ids=(),
        ),
        terminal_audit=audit_managed_stream_events(
            _completed_managed_stream_events(),
            responder_id="@mindroom_general:example",
            expected_source_ids=source_ids,
        ),
        health_samples=(
            ManagedStreamHealthSample(healthy=True, last_sync_time=before),
            ManagedStreamHealthSample(healthy=True, last_sync_time=after),
        ),
        health_samples_while_root_release=1,
        durable_drain=ManagedStreamDrainCounts(0, 0),
        recovery_abandonment_markers=0,
        watchdog_stalls=0,
        durable_drain_failure_markers=0,
        reaction_settled=True,
        pre_fence_last_sync=before,
        post_fence_last_sync=after,
        clean_shutdown=True,
        phase_durations=(("root_release", 1.0), ("terminal_settlement", 47.0), ("shutdown", 1.0)),
    )


def _capacity_root(
    event_id: str,
    thread: int,
    *,
    run_id: str = "unit-run",
    sender: str = "@mindroom_load_sender:example",
    body: str | None = None,
    mentions: tuple[str, ...] = ("@mindroom_general:example",),
) -> dict[str, Any]:
    """Build one production-shaped managed capacity root."""
    marker = f"run={run_id} thread={thread}"
    return {
        "event_id": event_id,
        "type": "m.room.message",
        "sender": sender,
        "content": {
            "msgtype": "m.text",
            "body": body or f"Sustained stream capacity {marker} @mindroom_general:example",
            "m.mentions": {"user_ids": list(mentions)},
        },
    }


def test_sustained_stream_capacity_source_audit_requires_exact_managed_roots() -> None:
    """Raw root proof rejects missing, forged, malformed, duplicated, and unknown sources."""
    valid = (_capacity_root("$source-0", 0), _capacity_root("$source-1", 1))
    audit = fuzz_live_matrix.audit_sustained_stream_capacity_sources(
        valid,
        expected_source_ids=("$source-0", "$source-1"),
        load_sender_id="@mindroom_load_sender:example",
        responder_id="@mindroom_general:example",
        run_id="unit-run",
    )

    assert audit == SustainedStreamCapacitySourceAudit(
        expected_source_ids=("$source-0", "$source-1"),
        observed_source_ids=("$source-0", "$source-1"),
        missing_source_ids=(),
        duplicate_source_ids=(),
        unexpected_source_ids=(),
        invalid_source_ids=(),
    )

    mutations = (
        (valid[:1], "missing_source_ids", ("$source-1",)),
        (
            (_capacity_root("$source-0", 0, sender="@foreign:example"), valid[1]),
            "invalid_source_ids",
            ("$source-0",),
        ),
        (
            (_capacity_root("$source-0", 0, mentions=("@wrong:example",)), valid[1]),
            "invalid_source_ids",
            ("$source-0",),
        ),
        (
            (
                _capacity_root(
                    "$source-0",
                    0,
                    body=(
                        "Sustained stream capacity run=unit-run thread=0 "
                        "run=unit-run thread=0 @mindroom_general:example"
                    ),
                ),
                valid[1],
            ),
            "invalid_source_ids",
            ("$source-0",),
        ),
        (
            (*valid, _capacity_root("$source-0", 0)),
            "duplicate_source_ids",
            ("$source-0",),
        ),
        (
            (*valid, _capacity_root("$unknown", 2)),
            "unexpected_source_ids",
            ("$unknown",),
        ),
    )
    for events, field, expected in mutations:
        mutated = fuzz_live_matrix.audit_sustained_stream_capacity_sources(
            events,
            expected_source_ids=("$source-0", "$source-1"),
            load_sender_id="@mindroom_load_sender:example",
            responder_id="@mindroom_general:example",
            run_id="unit-run",
        )
        observed = {
            "missing_source_ids": mutated.missing_source_ids,
            "invalid_source_ids": mutated.invalid_source_ids,
            "duplicate_source_ids": mutated.duplicate_source_ids,
            "unexpected_source_ids": mutated.unexpected_source_ids,
        }[field]
        assert observed == expected, field


@pytest.mark.asyncio
async def test_sustained_stream_capacity_runner_uses_one_deadline_and_emits_phase_evidence(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-fault lifecycle stays ordered and shutdown consumes only the fixed SLA remainder."""
    stack = ManagedTuwunelStack(profile="sustained-stream-capacity")
    stack.agent_id = "@mindroom_general:example"
    stack.load_sender_id = "@mindroom_load_sender:example"
    client = _ManagedStreamBoundaryClient()
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", client),),
        sustained_stream_capacity_scenario(root_count=2),
        reply_timeout=1,
        settle_seconds=0,
    )
    order: list[str] = []
    deadline_seen = 0.0
    shutdown_timeouts: list[float] = []
    marker_count = 0
    before = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    after = datetime(2026, 8, 8, 12, 1, tzinfo=UTC)

    async def authenticate() -> None:
        order.append("authenticate")

    async def baseline(*, run_id: str) -> fuzz_live_matrix.ManagedStreamBaseline:
        assert run_id
        order.append("warm-and-baseline")
        return fuzz_live_matrix.ManagedStreamBaseline(
            event_ids=frozenset(),
            log_counts=fuzz_live_matrix.ManagedStreamLogCounts(0),
        )

    async def release(
        *,
        run_id: str,
        deadline: float,
        health_samples: list[ManagedStreamHealthSample],
    ) -> tuple[str, ...]:
        nonlocal deadline_seen
        order.append("root-release")
        deadline_seen = deadline
        assert deadline > time.monotonic()
        health_samples.append(ManagedStreamHealthSample(True, before))
        roots = (
            _capacity_root("$source-0", 0, run_id=run_id),
            _capacity_root("$source-1", 1, run_id=run_id),
        )
        client.seen_events.update({event["event_id"]: event for event in roots})
        return "$source-0", "$source-1"

    async def terminals(**_kwargs: object) -> fuzz_live_matrix.ManagedStreamTerminalAudit:
        order.append("terminals")
        events = _completed_managed_stream_events()
        client.seen_events.update({event["event_id"]: event for event in events})
        return audit_managed_stream_events(
            events,
            responder_id=stack.agent_id,
            expected_source_ids=("$source-0", "$source-1"),
        )

    async def observe_raw(**kwargs: object) -> ManagedStreamHealthSample:
        order.append("raw-observe")
        sample = ManagedStreamHealthSample(True, before)
        cast("list[ManagedStreamHealthSample]", kwargs["health_samples"]).append(sample)
        return sample

    async def drain(**_kwargs: object) -> ManagedStreamDrainCounts:
        order.append("drain")
        return ManagedStreamDrainCounts(0, 0)

    async def fence(**_kwargs: object) -> tuple[bool, datetime, datetime]:
        order.append("fence")
        return True, before, after

    def stop_mindroom(*, timeout: float = 20) -> bool:
        order.append("shutdown")
        shutdown_timeouts.append(timeout)
        return True

    monkeypatch.setattr(runner, "_authenticate_managed_sender", authenticate)
    monkeypatch.setattr(runner, "_prepare_managed_stream_baseline", baseline)
    monkeypatch.setattr(runner, "_release_sustained_stream_capacity_roots", release)
    monkeypatch.setattr(runner, "_managed_stream_observer_step", observe_raw)
    monkeypatch.setattr(runner, "_wait_for_managed_stream_terminals", terminals)
    monkeypatch.setattr(runner, "_wait_for_managed_stream_drain", drain)
    monkeypatch.setattr(runner, "_wait_for_managed_stream_fence", fence)
    monkeypatch.setattr(stack, "stop_mindroom", stop_mindroom)
    monkeypatch.setattr(stack, "restart_shutdown_failure_count", lambda: marker_count)
    monkeypatch.setattr(stack, "log_count", lambda *_markers: 0)
    try:
        result = await runner._run_sustained_stream_capacity()

        assert order == [
            "authenticate",
            "warm-and-baseline",
            "root-release",
            "raw-observe",
            "terminals",
            "drain",
            "fence",
            "drain",
            "shutdown",
        ]
        assert deadline_seen > 0
        assert shutdown_timeouts
        assert 0 < shutdown_timeouts[0] <= min(20, deadline_seen - time.monotonic() + 0.1)
        assert result["profile"] == "sustained-stream-capacity"
        assert result["status"] == "PASS"
        assert result["roots"] == 2
        assert result["observed_root_sources"] == 2
        assert result["canonical_agent_replies"] == 2
        assert result["full_overlap_seconds"] == pytest.approx(46.0)
        assert result["health_samples_while_root_release"] == 1
        assert result["durable_drain_failure_markers"] == 0
        assert result["phase_root_release_seconds"] >= 0
        assert result["phase_terminal_settlement_seconds"] >= 0
        assert result["phase_shutdown_seconds"] >= 0
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_sustained_stream_capacity_rejects_shutdown_durable_recovery_marker_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean stop verdict cannot hide a new incomplete-drain recovery marker."""
    stack = ManagedTuwunelStack(profile="sustained-stream-capacity")
    stack.agent_id = "@mindroom_general:example"
    stack.load_sender_id = "@mindroom_load_sender:example"
    client = _ManagedStreamBoundaryClient()
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", client),),
        sustained_stream_capacity_scenario(root_count=2),
        reply_timeout=1,
        settle_seconds=0,
    )
    marker_count = 3
    before = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    after = datetime(2026, 8, 8, 12, 1, tzinfo=UTC)
    terminal = _valid_sustained_stream_capacity_observation().terminal_audit

    async def baseline(*, run_id: str) -> fuzz_live_matrix.ManagedStreamBaseline:
        del run_id
        return fuzz_live_matrix.ManagedStreamBaseline(
            event_ids=frozenset(),
            log_counts=fuzz_live_matrix.ManagedStreamLogCounts(0),
        )

    async def release(**kwargs: object) -> tuple[str, ...]:
        health_samples = cast("list[ManagedStreamHealthSample]", kwargs["health_samples"])
        run_id = cast("str", kwargs["run_id"])
        health_samples.append(ManagedStreamHealthSample(True, before))
        roots = (
            _capacity_root("$source-0", 0, run_id=run_id),
            _capacity_root("$source-1", 1, run_id=run_id),
        )
        client.seen_events.update({event["event_id"]: event for event in roots})
        client.seen_events.update({event["event_id"]: event for event in _completed_managed_stream_events()})
        return "$source-0", "$source-1"

    async def drain(**_kwargs: object) -> ManagedStreamDrainCounts:
        return ManagedStreamDrainCounts(0, 0)

    async def observe_raw(**kwargs: object) -> ManagedStreamHealthSample:
        sample = ManagedStreamHealthSample(True, before)
        cast("list[ManagedStreamHealthSample]", kwargs["health_samples"]).append(sample)
        return sample

    async def fence(**_kwargs: object) -> tuple[bool, datetime, datetime]:
        return True, before, after

    def stop_mindroom(*, timeout: float = 20) -> bool:
        nonlocal marker_count
        assert timeout > 0
        marker_count += 1
        return True

    monkeypatch.setattr(runner, "_authenticate_managed_sender", lambda: asyncio.sleep(0))
    monkeypatch.setattr(runner, "_prepare_managed_stream_baseline", baseline)
    monkeypatch.setattr(runner, "_release_sustained_stream_capacity_roots", release)
    monkeypatch.setattr(runner, "_managed_stream_observer_step", observe_raw)
    monkeypatch.setattr(runner, "_wait_for_managed_stream_terminals", lambda **_kwargs: asyncio.sleep(0, terminal))
    monkeypatch.setattr(runner, "_wait_for_managed_stream_drain", drain)
    monkeypatch.setattr(runner, "_wait_for_managed_stream_fence", fence)
    monkeypatch.setattr(stack, "stop_mindroom", stop_mindroom)
    monkeypatch.setattr(stack, "restart_shutdown_failure_count", lambda: marker_count)
    monkeypatch.setattr(stack, "log_count", lambda *_markers: 0)
    try:
        with pytest.raises(AssertionError, match="durable_drain_failure_markers=1"):
            await runner._run_sustained_stream_capacity()
    finally:
        stack.close()


def test_sustained_stream_capacity_evaluator_accepts_complete_no_fault_evidence() -> None:
    """A capacity PASS requires only ordinary completion and health evidence."""
    assert evaluate_sustained_stream_capacity(_valid_sustained_stream_capacity_observation()) == ()


def test_sustained_stream_capacity_evaluator_rejects_terminal_corruption() -> None:
    """One wrong canonical terminal direction cannot become a capacity PASS."""
    valid = _valid_sustained_stream_capacity_observation()
    terminal_audit = valid.terminal_audit
    cases = (
        (replace(terminal_audit, missing_sources=("$source-1",)), "missing_sources"),
        (replace(terminal_audit, duplicate_sources=(("$source-1", ("$one", "$two")),)), "duplicate_sources"),
        (replace(terminal_audit, unexpected_sources=("$unknown",)), "unknown_sources"),
        (replace(terminal_audit, invalid_relations=(("$reply", "$thread", "$source"),)), "invalid_relations"),
        (replace(terminal_audit, invalid_replacements=("$edit",)), "invalid_replacements"),
        (
            replace(terminal_audit, invalid_terminal_transitions=(("$response-1", 2),)),
            "invalid_terminal_transitions",
        ),
        (replace(terminal_audit, noncompleted_sources=(("$source-1", "streaming"),)), "noncompleted_sources"),
        (replace(terminal_audit, min_active_stream_seconds=44.999), "active_stream_duration_too_short"),
        (replace(terminal_audit, full_overlap_seconds=44.999), "full_overlap_too_short"),
        (replace(terminal_audit, peak_active_streams=1), "peak_active_streams"),
        (replace(terminal_audit, peak_active_streams=3), "peak_active_streams"),
        (replace(terminal_audit, canonical_responses=()), "canonical_responses"),
        (replace(terminal_audit, canonical_response_count=0), "canonical_response_count"),
        (
            replace(
                terminal_audit,
                expected_sources=("$source-0", "$source-1", "$source-1"),
            ),
            "terminal_expected_sources",
        ),
        (
            replace(
                terminal_audit,
                canonical_responses=(
                    ("$source-0", "$response-0"),
                    ("$source-0", "$response-1"),
                ),
            ),
            "canonical_response_source_ids",
        ),
    )

    for audit, marker in cases:
        failures = evaluate_sustained_stream_capacity(replace(valid, terminal_audit=audit))
        assert any(marker in failure for failure in failures), marker


def test_sustained_stream_capacity_evaluator_rejects_unsettled_or_incomplete_evidence() -> None:
    """No-fault capacity still fails closed on every required lifecycle observation."""
    valid = _valid_sustained_stream_capacity_observation()
    before = valid.pre_fence_last_sync
    assert before is not None
    cases = (
        (
            replace(
                valid,
                health_samples=(ManagedStreamHealthSample(healthy=False, last_sync_time=before),),
            ),
            "health_samples_unhealthy",
        ),
        (replace(valid, health_samples=()), "health_samples_unhealthy"),
        (replace(valid, health_samples_while_root_release=0), "health_samples_while_root_release"),
        (replace(valid, recovery_abandonment_markers=1), "recovery_abandonment_markers"),
        (replace(valid, watchdog_stalls=1), "watchdog_stalls"),
        (
            replace(
                valid,
                durable_drain=replace(valid.durable_drain, pending_journal_rows=1),
            ),
            "pending_journal_rows",
        ),
        (
            replace(
                valid,
                durable_drain=replace(valid.durable_drain, unacknowledged_outbox_rows=1),
            ),
            "unacknowledged_outbox_rows",
        ),
        (replace(valid, reaction_settled=False), "reaction_not_settled"),
        (replace(valid, post_fence_last_sync=before), "sync_progress_absent_after_fence"),
        (
            replace(
                valid,
                source_audit=replace(valid.source_audit, observed_source_ids=("$source-0",)),
            ),
            "root_source_audit_incomplete",
        ),
        (
            replace(
                valid,
                source_audit=replace(
                    valid.source_audit,
                    observed_source_ids=("$source-0", "$source-1", "$source-1"),
                ),
            ),
            "root_source_audit_duplicate_ids",
        ),
        (replace(valid, clean_shutdown=False), "shutdown_not_clean"),
        (replace(valid, durable_drain_failure_markers=1), "durable_drain_failure_markers"),
    )

    for observation, marker in cases:
        failures = evaluate_sustained_stream_capacity(observation)
        assert any(marker in failure for failure in failures), marker


def test_reply_timeout_help_distinguishes_adaptive_and_fixed_profiles(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Operators must not mistake capacity's whole-workload SLA for an adaptive floor."""
    monkeypatch.setenv("COLUMNS", "240")
    monkeypatch.setattr(sys, "argv", ["fuzz_live_matrix.py", "--help"])

    with pytest.raises(SystemExit) as raised:
        fuzz_live_matrix._parse_args()

    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    assert "adaptive per-turn floor for fuzz, restart-regression, and short-stream-correctness" in help_text
    assert "one fixed whole-workload non-extending SLA for sustained-stream-capacity" in help_text


def test_sustained_stream_capacity_readme_documents_parser_and_no_fault_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capacity invocation and its no-fault boundary stay documented."""
    readme = (PROJECT_ROOT / "scripts" / "README.md").read_text(encoding="utf-8")

    assert sustained_stream_capacity_scenario().thread_count == 200
    assert (
        "uv run python scripts/testing/fuzz_live_matrix.py --profile sustained-stream-capacity "
        "--threads 200 --reply-timeout 180"
    ) in readme
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fuzz_live_matrix.py",
            "--profile",
            "sustained-stream-capacity",
            "--threads",
            "200",
            "--reply-timeout",
            "180",
        ],
    )
    args = fuzz_live_matrix._parse_args()
    assert args.threads == sustained_stream_capacity_scenario().thread_count
    assert args.reply_timeout == 180
    assert fuzz_live_matrix._scenario_from_args(args) == sustained_stream_capacity_scenario()
    assert "N configured root source events" in readme
    assert "all N streams" in readme
    assert "does not send SIGSTOP" in readme
    assert "does not pause or restart MindRoom" in readme
    assert "does not require legacy recovery markers" in readme
    assert "does not require a recovery marker" in readme


def test_managed_stream_event_audit_folds_only_same_responder_edits_in_total_order() -> None:
    """A same-timestamp later edit wins, while another sender cannot finish it."""
    events = (
        _completed_managed_stream_events()[0],
        _stream_edit("$edit-a", "$response-0", 2_000, "completed"),
        _stream_edit("$edit-z", "$response-0", 2_000, "streaming"),
        _stream_edit(
            "$foreign-edit",
            "$response-0",
            9_000,
            "completed",
            sender="@other:example",
        ),
    )

    audit = audit_managed_stream_events(
        events,
        responder_id="@mindroom_general:example",
        expected_source_ids=frozenset({"$source-0"}),
    )

    assert audit.noncompleted_sources == (("$source-0", "streaming"),)


def test_managed_stream_event_audit_requires_matching_thread_and_reply_relations() -> None:
    """An expected reply target cannot compensate for the wrong thread root."""
    original = _completed_managed_stream_events()[0]
    wrong_thread = {
        **original,
        "content": {
            **original["content"],
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$different-thread",
                "m.in_reply_to": {"event_id": "$source-0"},
            },
        },
    }

    audit = audit_managed_stream_events(
        (wrong_thread,),
        responder_id="@mindroom_general:example",
        expected_source_ids=frozenset({"$source-0"}),
    )
    valid = _valid_sustained_stream_capacity_observation()
    failures = evaluate_sustained_stream_capacity(
        replace(
            valid,
            root_count=1,
            source_audit=SustainedStreamCapacitySourceAudit(
                expected_source_ids=("$source-0",),
                observed_source_ids=("$source-0",),
                missing_source_ids=(),
                duplicate_source_ids=(),
                unexpected_source_ids=(),
                invalid_source_ids=(),
            ),
            terminal_audit=audit,
        ),
    )

    assert audit.canonical_response_count == 0
    assert audit.missing_sources == ("$source-0",)
    assert audit.invalid_relations == (("$response-0", "$different-thread", "$source-0"),)
    assert any("invalid_relation" in failure for failure in failures)


@pytest.mark.asyncio
async def test_machine_readable_pass_result_labels_its_profile() -> None:
    """A passing short-stream result must state its profile instead of implying capacity."""
    stack = ManagedTuwunelStack()
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", _ManagedStreamBoundaryClient()),),
        LiveFuzzScenario(thread_count=13, batches=(), profile="short-stream-correctness"),
        reply_timeout=1,
        settle_seconds=0,
    )
    try:
        result = await runner._run_batches(())
        assert result["profile"] == "short-stream-correctness"
        assert result["status"] == "PASS"
    finally:
        stack.close()


def test_live_scenario_rejects_same_batch_dependency() -> None:
    """Concurrent operations may only target events from completed batches."""
    scenario = LiveFuzzScenario(
        thread_count=1,
        batches=(
            (
                LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "root:0"),
                LiveOperation(1, LiveOperationKind.REACTION, 0, "op:0"),
            ),
        ),
    )

    with pytest.raises(ValueError, match="unknown or same-batch target"):
        scenario.validate()


def test_live_scenario_rejects_ambiguous_same_thread_reply_batch() -> None:
    """The exact-reply oracle cannot distinguish a valid coalesced turn from loss."""
    scenario = LiveFuzzScenario(
        thread_count=1,
        batches=(
            (
                LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "root:0"),
                LiveOperation(1, LiveOperationKind.PLAIN_REPLY, 0, "response:root:0"),
            ),
        ),
    )

    with pytest.raises(ValueError, match="same-thread messages"):
        scenario.validate()


def test_restart_regression_scenario_has_fixed_empty_shape() -> None:
    """The manual profile owns its deterministic operations outside the fuzz trace."""
    scenario = restart_regression_scenario()

    assert scenario == LiveFuzzScenario(thread_count=1, batches=(), profile="restart-regression")
    scenario.validate()


def test_semantic_ingress_count_excludes_restart_relay_thread_reference() -> None:
    """A relay referring to the fresh thread must not count as fresh event ingress."""
    markers = _semantic_ingress_markers(
        agent="general",
        room_id="!restart:example",
        event_id="$fresh",
    )
    log = (
        "Received message agent=general event_id=$fresh room_id=!restart:example thread_id=None\n"
        "Received message agent=general event_id=$relay room_id=!restart:example thread_id=$fresh\n"
    )

    assert _log_count(log, *markers) == 1


def test_restart_regression_scenario_rejects_declared_batches_ignored_by_fixed_runner() -> None:
    """The fixed restart profile must reject operations its runner would ignore."""
    scenario = LiveFuzzScenario(
        thread_count=1,
        batches=((LiveOperation(0, LiveOperationKind.RESTART_MINDROOM, 0, None),),),
        profile="restart-regression",
    )

    with pytest.raises(ValueError, match="fixed empty trace"):
        scenario.validate()


def test_restart_regression_evaluator_accepts_pass_and_rejects_bad_directions() -> None:
    """The profile's pure oracle must accept clean evidence and reject old output and prompt overlap."""
    passing = RestartRegressionObservation(
        historical_output_counts=(0, 0),
        historical_callback_counts=(0, 0),
        projected_after_answer_count=0,
        historical_projected_on_room_read=2,
        fresh_agent_output_count=1,
        fresh_router_output_count=0,
        fresh_response_complete=True,
        fresh_semantic_ingress_count_before_restart=1,
        fresh_semantic_ingress_count=2,
        recovered_generation_response_observed=True,
        fresh_obligation_recovered=True,
        fresh_prompt_observed=True,
        historical_in_fresh_prompt=False,
        orderly_drain_completed=True,
    )

    assert evaluate_restart_regression(passing) == ()

    failures = evaluate_restart_regression(
        replace(
            passing,
            historical_output_counts=(1, 0),
            historical_callback_counts=(0, 1),
            projected_after_answer_count=0,
            historical_projected_on_room_read=0,
            fresh_agent_output_count=0,
            fresh_router_output_count=1,
            fresh_response_complete=False,
            fresh_semantic_ingress_count=1,
            recovered_generation_response_observed=False,
            fresh_obligation_recovered=False,
            historical_in_fresh_prompt=True,
            orderly_drain_completed=False,
        ),
    )

    assert any("invariant=historical_output_suppressed" in failure for failure in failures)
    assert any("invariant=historical_callback_suppressed" in failure for failure in failures)
    assert any("invariant=historical_events_projected_on_room_read" in failure for failure in failures)
    assert any("invariant=fresh_agent_response_exactly_once" in failure for failure in failures)
    assert any("invariant=fresh_router_response_suppressed" in failure for failure in failures)
    assert any("invariant=fresh_response_complete" in failure for failure in failures)
    assert any("invariant=fresh_semantic_ingress_replayed_after_restart" in failure for failure in failures)
    assert any("invariant=recovered_generation_response_observed" in failure for failure in failures)
    assert any("invariant=fresh_journal_event_recovered" in failure for failure in failures)
    assert any("invariant=historical_events_absent_from_fresh_prompt" in failure for failure in failures)
    assert any("invariant=orderly_drain_completed" in failure for failure in failures)

    unmeasured = evaluate_restart_regression(
        replace(passing, orderly_drain_completed=None),
    )
    assert not any("invariant=orderly_drain_completed" in failure for failure in unmeasured)


@pytest.mark.asyncio
async def test_restart_regression_does_not_send_fresh_event_before_replacement_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missed replacement boundary must abort before the fresh event is sent."""
    stack = ManagedTuwunelStack()
    try:
        stack.agent_id, stack.router_id = "@agent:example", "@router:example"
        monkeypatch.setattr(stack, "apply_replacement_config", lambda _room_id: None)
        monkeypatch.setattr(stack, "wait_for_log_count", lambda *_args, **_kwargs: False)
        dormant = _RecordingDormantClient()
        runner = LiveFuzzRunner(
            stack,
            (cast("LiveMatrixClient", dormant),),
            restart_regression_scenario(),
            reply_timeout=0,
            settle_seconds=0,
        )

        with pytest.raises(AssertionError, match="replacement_setup_boundary_reached"):
            await runner._run_restart_regression()

        assert dormant.sent_txn_ids == ["restart-old-text", "restart-old-media"]
        assert dormant.sent_payloads[0] == (
            "m.room.message",
            "restart-old-text",
            {
                "body": "Synthetic historical text @agent:example",
                "m.mentions": {"user_ids": ["@agent:example"]},
                "msgtype": "m.text",
            },
        )
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_restart_regression_boundary_requires_old_runtime_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacement setup is insufficient until both old bot generations report shutdown."""
    stack = ManagedTuwunelStack()
    observed_markers: list[tuple[str, ...]] = []
    try:
        stack.agent_id, stack.router_id = "@agent:example", "@router:example"
        monkeypatch.setattr(stack, "apply_replacement_config", lambda _room_id: None)

        def miss_every_boundary(markers: tuple[str, ...], *_args: object, **_kwargs: object) -> bool:
            observed_markers.append(markers)
            return False

        monkeypatch.setattr(stack, "wait_for_log_count", miss_every_boundary)
        dormant = _RecordingDormantClient()
        runner = LiveFuzzRunner(
            stack,
            (cast("LiveMatrixClient", dormant),),
            restart_regression_scenario(),
            reply_timeout=0,
            settle_seconds=0,
        )

        with pytest.raises(AssertionError, match="replacement_setup_boundary_reached"):
            await runner._run_restart_regression()

        assert (
            "matrix_agent_response_runtime_shutdown",
            "agent=general",
            "restart_reason_category=config_reload",
        ) in observed_markers
        assert (
            "matrix_agent_response_runtime_shutdown",
            "agent=router",
            "restart_reason_category=config_reload",
        ) in observed_markers
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_restart_regression_crosses_fresh_obligation_over_hard_restart() -> None:
    """The fresh callback must be durable and in flight before the process is killed."""
    stack = _RestartBoundaryStack()
    try:
        dormant = _RecordingDormantClient()
        runner = _RestartBoundaryRunner(
            stack,
            (cast("LiveMatrixClient", dormant),),
            restart_regression_scenario(),
            reply_timeout=1,
            settle_seconds=0,
        )

        await runner._run_restart_regression()

        # The room read is last on purpose: hydration writes to the projection,
        # so a read that ran any earlier would manufacture the evidence the
        # other invariants are supposed to find on their own.
        assert stack.order == [
            "durable-callback",
            "obligation-pending",
            "model-in-flight",
            "sync-checkpoint",
            "hard-restart",
            "room-read",
        ]
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_restart_regression_refuses_hard_kill_before_fresh_checkpoint() -> None:
    """A cached fresh event without later sync continuity cannot cross the kill boundary."""
    stack = _RestartBoundaryStack()
    stack.checkpoint_ready = False
    try:
        dormant = _RecordingDormantClient()
        runner = _RestartBoundaryRunner(
            stack,
            (cast("LiveMatrixClient", dormant),),
            restart_regression_scenario(),
            reply_timeout=1,
            settle_seconds=0,
        )

        with pytest.raises(AssertionError, match="fresh_sync_checkpoint_advanced_before_restart"):
            await runner._run_restart_regression()

        assert stack.order[-1] == "sync-checkpoint"
        assert "hard-restart" not in stack.order
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_restart_regression_releases_fresh_event_without_waiting_for_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fresh event follows the replacement boundary with no historical wait.

    Hydration is lazy, so nothing fetches this room's history until something
    reads it. A pre-condition wait for that history would never be satisfied,
    which is why the profile releases the fresh event straight after the
    lifecycle boundary and reads the room afterwards instead.
    """
    stack = ManagedTuwunelStack()
    history_reads: list[object] = []
    try:
        stack.agent_id, stack.router_id = "@agent:example", "@router:example"
        monkeypatch.setattr(stack, "apply_replacement_config", lambda _room_id: None)
        monkeypatch.setattr(stack, "wait_for_log_count", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(
            stack,
            "projected_restart_event_pair_count",
            lambda *args: history_reads.append(args) or 0,
        )
        dormant = _RecordingDormantClient()
        runner = LiveFuzzRunner(
            stack,
            (cast("LiveMatrixClient", dormant),),
            restart_regression_scenario(),
            reply_timeout=0,
            settle_seconds=0,
        )

        with pytest.raises(AssertionError, match="fresh_dispatch_obligation_unsettled_before_restart"):
            await runner._run_restart_regression()

        assert dormant.sent_txn_ids == [
            "restart-old-text",
            "restart-old-media",
            "restart-fresh",
        ]
        assert not history_reads
    finally:
        stack.close()


def test_restart_log_wait_handles_ansi_and_multiple_markers() -> None:
    """Rendered log fields must still support exact multi-marker waits."""
    stack = ManagedTuwunelStack()
    try:
        stack.agent_id, stack.router_id = "@agent:example", "@router:example"
        assert not stack.wait_for_log_count(("missing",), 1, timeout=0)
        stack.log_path.write_text(
            "agent_setup_complete @agent:example\n"
            "\x1b[1mmatrix_agent_response_runtime_shutdown\x1b[0m "
            "agent=\x1b[35mgeneral\x1b[0m restart_reason_category=\x1b[35mconfig_reload\x1b[0m\n",
            encoding="utf-8",
        )
        assert stack.wait_for_log_count(("agent_setup_complete", "@agent:example"), 1, timeout=0)
        assert stack.wait_for_log_count(
            (
                "matrix_agent_response_runtime_shutdown",
                "agent=general",
                "restart_reason_category=config_reload",
            ),
            1,
            timeout=0,
        )
    finally:
        stack.close()


def test_restart_regression_projection_evidence_uses_production_schema_and_exact_filters() -> None:
    """Principal, room, and event filters must reject plausible distractor rows."""
    stack = ManagedTuwunelStack()
    try:
        stack.agent_id, stack.router_id = "@agent:example", "@router:example"
        stack.storage_path.mkdir()
        database_path = stack.storage_path / "tracking" / "event_journal.db"
        database_path.parent.mkdir(parents=True, exist_ok=True)
        store = EventJournalStore.open_sqlite(database_path)
        asyncio.run(store.close())
        rows = (
            ("general@@agent:example", "!target:example", "$old-text"),
            ("general@@agent:example", "!target:example", "$old-media"),
            ("router@@router:example", "!target:example", "$old-text"),
            ("router@@router:example", "!target:example", "$old-media"),
            ("general@@wrong:example", "!target:example", "$old-text"),
            ("general@@wrong:example", "!target:example", "$old-media"),
            ("general@@agent:example", "!target:example", "$wrong-event"),
            ("router@@router:example", "!target:example", "$wrong-event"),
            ("general@@agent:example", "!wrong:example", "$old-text"),
        )
        with closing(sqlite3.connect(database_path)) as fixture_database:
            fixture_database.executemany(
                """
                INSERT INTO visible_messages(
                    principal_id,
                    room_id,
                    logical_event_id,
                    thread_id,
                    sender,
                    created_ts,
                    revision_event_id,
                    revision_ts,
                    content_json,
                    membership_epoch
                ) VALUES (?, ?, ?, '', '@sender:example', 1, ?, 1, '{}', 0)
                """,
                ((*row, row[2]) for row in rows),
            )
            fixture_database.commit()

        event_ids = ("$old-text", "$old-media")
        assert stack.projected_restart_event_pair_count("!target:example", event_ids) == 4
    finally:
        stack.close()


def _seed_visible_message(
    stack: ManagedTuwunelStack,
    *,
    principal: str,
    room_id: str,
    logical_event_id: str,
    thread_id: str = "",
) -> None:
    """Write one projection row through the production schema."""
    database_path = stack.storage_path / "tracking" / "event_journal.db"
    EventJournalStore.open_sqlite(database_path)
    with closing(sqlite3.connect(database_path)) as fixture_database:
        fixture_database.execute(
            """
            INSERT INTO visible_messages(
                principal_id,
                room_id,
                logical_event_id,
                thread_id,
                sender,
                created_ts,
                revision_event_id,
                revision_ts,
                content_json,
                membership_epoch
            ) VALUES (?, ?, ?, ?, '@sender:example', 1, ?, 1, '{}', 0)
            """,
            (principal, room_id, logical_event_id, thread_id, logical_event_id),
        )
        fixture_database.commit()


async def _no_network_hydration(
    _self: ConversationHydrator,
    *,
    room_id: str,
    thread_id: str | None,
) -> None:
    """Stand in for hydration so the read runs against exactly the seeded rows."""
    assert room_id
    del thread_id


@pytest.mark.asyncio
async def test_restart_room_read_finds_history_the_answer_never_projected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The room read must reach main-timeline history, not the fresh thread.

    This is the whole content of the assertion. Answering the fresh event
    hydrates the fresh *thread*, and the pre-gap history is not in it, so a
    read pointed at that thread finds nothing. Pointing the read at the room
    conversation is what separates "the history is gone" from "the history
    appears when something asks".
    """
    stack = ManagedTuwunelStack()
    room, thread = "!target:example", "$fresh-root"
    runner = None
    try:
        stack.agent_id, stack.router_id = "@agent:example", "@router:example"
        agent = f"general@{stack.agent_id}"
        for logical_event_id in ("$old-text", "$old-media"):
            _seed_visible_message(stack, principal=agent, room_id=room, logical_event_id=logical_event_id)
        _seed_visible_message(
            stack,
            principal=agent,
            room_id=room,
            logical_event_id="$fresh-reply",
            thread_id=thread,
        )
        (stack.storage_path / "matrix_state.yaml").write_text(
            "accounts:\n  agent_general:\n    username: general\n    access_token: token\n    device_id: DEVICE\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(ConversationHydrator, "ensure_hydrated", _no_network_hydration)
        runner = LiveFuzzRunner(
            stack,
            (LiveMatrixClient("http://matrix.invalid", room),),
            restart_regression_scenario(),
            reply_timeout=1,
            settle_seconds=0,
        )

        assert (
            await runner._read_historical_room_projection(
                room_id=room,
                historical_event_ids=("$old-text", "$old-media"),
            )
            == 2
        )
    finally:
        if runner is not None:
            await asyncio.gather(*(client.close() for client in runner.clients))
        stack.close()


@pytest.mark.asyncio
async def test_restart_room_read_without_persisted_credentials_fails_the_invariant() -> None:
    """A run that never persisted the agent account must not read as a quiet success."""
    stack = ManagedTuwunelStack()
    runner = None
    try:
        stack.agent_id, stack.router_id = "@agent:example", "@router:example"
        runner = LiveFuzzRunner(
            stack,
            (LiveMatrixClient("http://matrix.invalid", "!target:example"),),
            restart_regression_scenario(),
            reply_timeout=1,
            settle_seconds=0,
        )

        observed = await runner._read_historical_room_projection(
            room_id="!target:example",
            historical_event_ids=("$old-text", "$old-media"),
        )

        assert observed == 0
        assert any(
            "invariant=historical_events_projected_on_room_read" in failure
            for failure in evaluate_restart_regression(
                RestartRegressionObservation(
                    historical_output_counts=(0, 0),
                    historical_callback_counts=(0, 0),
                    projected_after_answer_count=0,
                    historical_projected_on_room_read=observed,
                    fresh_agent_output_count=1,
                    fresh_router_output_count=0,
                    fresh_response_complete=True,
                    fresh_semantic_ingress_count_before_restart=1,
                    fresh_semantic_ingress_count=2,
                    recovered_generation_response_observed=True,
                    fresh_obligation_recovered=True,
                    fresh_prompt_observed=True,
                    historical_in_fresh_prompt=False,
                    orderly_drain_completed=True,
                ),
            )
        )
    finally:
        if runner is not None:
            await asyncio.gather(*(client.close() for client in runner.clients))
        stack.close()


@pytest.mark.parametrize(
    ("log", "expected"),
    [
        ("Preparing agent and prompt agent=general $fresh $old-text", (True, True)),
        ("Preparing agent and prompt agent=general $fresh", (True, False)),
        ("Preparing agent and prompt agent=router $fresh", (False, False)),
        ("Preparing agent and prompt agent=general $old-text", (False, False)),
    ],
)
def test_restart_prompt_observation_filters_exact_fresh_agent_prompt(
    log: str,
    expected: tuple[bool, bool],
) -> None:
    """Prompt evidence must identify the fresh agent turn and historical overlap independently."""
    assert _restart_prompt_observation(log, "$fresh", ("$old-text", "$old-media")) == expected


def test_combined_response_count_includes_every_configured_sender() -> None:
    """The restart oracle must count agent and router responses to the same source."""
    assert (
        LiveFuzzRunner._combined_response_count(
            "$fresh",
            {"$fresh": {"$agent-response"}},
            {"$fresh": {"$router-response"}},
        )
        == 2
    )


def test_restart_regression_projection_probe_does_not_create_an_empty_database() -> None:
    """Missing runtime journal state must not be converted into an empty SQLite database."""
    stack = ManagedTuwunelStack()
    try:
        database_path = stack.storage_path / "tracking" / "event_journal.db"

        assert stack.projected_restart_event_pair_count("!target:example", ("$old-text", "$old-media")) == 0
        assert not database_path.exists()
    finally:
        stack.close()


@pytest.mark.parametrize("debt", ["input", "batch"])
def test_restart_regression_waits_for_projected_event_and_drained_source(debt: str) -> None:
    """Projection alone cannot cross the restart boundary before producer settlement."""
    stack = ManagedTuwunelStack()
    writer: threading.Thread | None = None
    try:
        stack.agent_id = "@agent:example"
        stack.storage_path.mkdir()
        database_path = stack.storage_path / "tracking" / "event_journal.db"
        database_path.parent.mkdir(parents=True, exist_ok=True)
        store = EventJournalStore.open_sqlite(database_path)
        asyncio.run(store.close())
        with closing(sqlite3.connect(database_path)) as fixture_database:
            fixture_database.execute(
                """
                INSERT INTO visible_messages(
                    principal_id,
                    room_id,
                    logical_event_id,
                    thread_id,
                    sender,
                    created_ts,
                    revision_event_id,
                    revision_ts,
                    content_json,
                    membership_epoch
                ) VALUES (?, ?, ?, '', '@sender:example', 1, ?, 1, '{}', 0)
                """,
                (f"general@{stack.agent_id}", "!target:example", "$fresh", "$fresh"),
            )
            fixture_database.commit()
        for name, user in (("router", "@router:example"), ("general", stack.agent_id)):
            producer = DurableStore(
                stack.storage_path / "encryption_keys" / name,
                user_id=user,
                device_id="DEVICE",
                consumer_id=uuid4(),
            )
            with producer.transaction():
                producer.set_cursor("opaque-unchanged-position")
                if name == "general":
                    if debt == "input":
                        producer.capture(b"{}")
                    else:
                        producer.publish((), completes_sync=True)
            producer_path = producer.path
            producer.close()
        assert not stack.wait_for_restart_event_checkpoint("!target:example", "$fresh", timeout=0.01)

        def advance_checkpoint() -> None:
            time.sleep(0.1)
            with closing(sqlite3.connect(producer_path)) as database:
                database.execute("DELETE FROM NioDurableInput")
                database.execute("DELETE FROM NioDurableBatch")
                database.commit()

        writer = threading.Thread(target=advance_checkpoint)
        writer.start()
        assert stack.wait_for_restart_event_checkpoint(
            "!target:example",
            "$fresh",
            timeout=1,
        )
        writer.join(timeout=1)
    finally:
        if writer is not None:
            writer.join(timeout=1)
        stack.close()


def test_restart_regression_reads_exact_durable_journal_state() -> None:
    """The recovery oracle must follow the exact agent message journal row."""
    stack = ManagedTuwunelStack()
    try:
        stack.agent_id = "@agent:example"
        store = EventJournalStore.open_sqlite(stack.storage_path / "tracking" / "event_journal.db")
        principal_id = f"general@{stack.agent_id}"
        database_path = stack.storage_path / "tracking" / "event_journal.db"

        async def admit() -> None:
            await store.principal(principal_id).admit(
                InboundEvent(
                    event_id="$fresh",
                    room_id="!room:example",
                    thread_id=None,
                    kind=EventKind.MESSAGE,
                    event_class=EventClass.ACTIONABLE,
                    sender="@user:example",
                    origin_server_ts=1,
                    source={"event_id": "$fresh"},
                ),
            )

        asyncio.run(admit())

        assert stack.restart_journal_event_state("$fresh") == "pending"
        assert stack.restart_journal_event_state("$other") is None
        assert stack.wait_for_restart_journal_event_state(
            "$fresh",
            expected="pending",
            timeout=0.01,
        )

        # Settling is the fact the oracle needs; the journal records no reason.
        with closing(sqlite3.connect(database_path)) as database:
            database.execute("UPDATE journal_events SET state = 'settled'")
            database.commit()

        assert stack.restart_journal_event_state("$fresh") == "settled"
    finally:
        stack.close()


def test_restart_config_update_atomically_replaces_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live watcher must never observe a truncated replacement config."""
    stack = ManagedTuwunelStack()
    try:
        stack.config_path.write_text(
            "models:\n  default:\n    id: mindroom-live-fuzz\nagents:\n  general:\n    rooms: [lobby]\n",
            encoding="utf-8",
        )
        replacements: list[tuple[Path, Path]] = []
        replace_path = Path.replace

        def record_replace(source: Path, destination: Path) -> Path:
            replacements.append((source, destination))
            return replace_path(source, destination)

        monkeypatch.setattr(Path, "replace", record_replace)

        stack.apply_replacement_config("!restart:example")

        assert replacements == [(stack.config_path.with_suffix(".yaml.tmp"), stack.config_path)]
        assert "!restart:example" in stack.config_path.read_text(encoding="utf-8")
        assert "mindroom-live-fuzz-replacement" in stack.config_path.read_text(encoding="utf-8")
    finally:
        stack.close()


def test_restart_config_uses_agent_specific_replacement_model() -> None:
    """Router traffic must never share the model ID that arms the agent restart latch."""
    stack = ManagedTuwunelStack()
    try:
        stack._write_config(9292)
        config = yaml.safe_load(stack.config_path.read_text(encoding="utf-8"))

        assert config["agents"]["general"]["model"] == "default"
        assert config["router"]["model"] == "router"
        assert config["models"]["default"]["id"] == "mindroom-live-fuzz"
        assert config["models"]["router"]["id"] == "mindroom-live-fuzz"
        assert config["room_defaults"]["join_policy"] == "public"
        assert "matrix_room_access" not in config
        assert "authorization" not in config
        Config.model_validate(config)

        stack.apply_replacement_config("!restart:example")
        replacement = yaml.safe_load(stack.config_path.read_text(encoding="utf-8"))

        assert replacement["models"]["default"]["id"] == "mindroom-live-fuzz-replacement"
        assert replacement["models"]["router"]["id"] == "mindroom-live-fuzz"
    finally:
        stack.close()


def test_sustained_stream_capacity_config_uses_managed_sender_and_synthetic_responder() -> None:
    """The no-fault profile must leave 45 seconds of overlap after a spread launch."""
    stack = ManagedTuwunelStack(profile="sustained-stream-capacity")
    try:
        stack._write_config(9292)
        config = yaml.safe_load(stack.config_path.read_text(encoding="utf-8"))

        assert config["matrix_sync"] == {
            "mode": "classic",
        }
        assert config["agents"]["general"]["model"] == "synthetic"
        assert config["agents"]["load_sender"]["rooms"] == ["lobby"]
        assert config["models"]["synthetic"]["extra_kwargs"] == {
            "seed": 1,
            "min_response_chars": 4800,
            "max_response_chars": 4800,
            "chunk_chars": 40,
            "chars_per_second": 80,
            "tool_call_probability": 0.2,
        }
    finally:
        stack.close()


@pytest.fixture
def managed_agent_credentials_stack() -> Iterator[ManagedTuwunelStack]:
    """Provide two distinct persisted managed-agent credential records."""
    stack = ManagedTuwunelStack()
    try:
        stack.storage_path.mkdir()
        (stack.storage_path / "matrix_state.yaml").write_text(
            yaml.safe_dump(
                {
                    "accounts": {
                        "agent_general": {
                            "access_token": "general-token",
                            "device_id": "general-device",
                        },
                        "agent_load_sender": {
                            "access_token": "sender-token",
                            "device_id": "sender-device",
                        },
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        yield stack
    finally:
        stack.close()


def test_managed_agent_credentials_selects_the_requested_account(
    managed_agent_credentials_stack: ManagedTuwunelStack,
) -> None:
    """The managed load sender must never reuse the responder's persisted credentials."""
    assert managed_agent_credentials_stack.agent_matrix_credentials() == ("general-token", "general-device")
    assert managed_agent_credentials_stack.agent_matrix_credentials("load_sender") == (
        "sender-token",
        "sender-device",
    )


def test_managed_stream_drain_counts_only_live_journal_and_outbox_rows() -> None:
    """Terminal journal and acknowledged outbox rows do not keep the drain open."""
    stack = ManagedTuwunelStack(profile="sustained-stream-capacity")
    try:
        database_path = stack.storage_path / "tracking" / "event_journal.db"
        database_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database_path)) as database:
            database.execute("CREATE TABLE journal_events(state TEXT NOT NULL)")
            database.execute("CREATE TABLE matrix_delivery_outbox(acknowledged_event_id TEXT)")
            database.executemany(
                "INSERT INTO journal_events(state) VALUES (?)",
                (("pending",), ("settled",)),
            )
            database.executemany(
                "INSERT INTO matrix_delivery_outbox(acknowledged_event_id) VALUES (?)",
                ((None,), ("$response",)),
            )
            database.commit()

        assert stack.managed_stream_drain_counts() == ManagedStreamDrainCounts(
            pending_journal_rows=1,
            unacknowledged_outbox_rows=1,
        )
    finally:
        stack.close()


def test_managed_stream_drain_fails_when_the_journal_database_is_missing() -> None:
    """Absent durable state must not be reported as a zero-row drain."""
    stack = ManagedTuwunelStack(profile="sustained-stream-capacity")
    try:
        with pytest.raises(FileNotFoundError, match="event journal database"):
            stack.managed_stream_drain_counts()
    finally:
        stack.close()


def test_recovery_debt_counts_only_exact_workload_final_rows() -> None:
    """Only attempted unacknowledged FINAL debt for general workload roots is evidence."""
    stack = ManagedTuwunelStack(profile="sustained-stream-capacity")
    stack.agent_id = "@mindroom_general:example"
    expected_principal = f"general@{stack.agent_id}"
    try:
        database_path = stack.storage_path / "tracking" / "event_journal.db"
        database_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database_path)) as database:
            database.execute(
                "CREATE TABLE matrix_delivery_outbox("
                "principal_id TEXT, delivery_id TEXT, stage TEXT, attempted INTEGER, acknowledged_event_id TEXT)",
            )
            database.executemany(
                "INSERT INTO matrix_delivery_outbox VALUES (?, ?, ?, ?, ?)",
                (
                    (expected_principal, "$source-0", "final", 1, None),
                    ("router@@mindroom_router:example", "$source-0", "final", 1, None),
                    (expected_principal, "$unknown", "final", 1, None),
                    (expected_principal, "$source-1", "initial", 1, None),
                    (expected_principal, "$source-1", "final", 0, None),
                    (expected_principal, "$source-1", "final", 1, "$acknowledged"),
                ),
            )
            database.commit()

        assert stack.recovery_outbox_debt(("$source-0", "$source-1")) == 1
    finally:
        stack.close()


def test_managed_stream_reaction_state_filters_exact_principal_event_and_kind() -> None:
    """A distractor principal cannot prove that the responder settled the fence."""
    stack = ManagedTuwunelStack(profile="sustained-stream-capacity")
    stack.agent_id = "@mindroom_general:example"
    try:
        database_path = stack.storage_path / "tracking" / "event_journal.db"
        database_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database_path)) as database:
            database.execute(
                "CREATE TABLE journal_events(principal_id TEXT, event_id TEXT, kind TEXT, state TEXT)",
            )
            database.execute(
                "INSERT INTO journal_events VALUES (?, '$reaction', 'reaction', 'pending')",
                ("router@@mindroom_router:example",),
            )
            database.commit()
            assert stack.managed_stream_reaction_state("$reaction") is None
            database.execute(
                "INSERT INTO journal_events VALUES (?, '$reaction', 'reaction', 'settled')",
                (f"general@{stack.agent_id}",),
            )
            database.commit()
        assert stack.managed_stream_reaction_state("$reaction") == "settled"
    finally:
        stack.close()


def test_restart_recovery_hard_kills_and_boots_new_model_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The crossed-boundary restart must preserve storage but change PID and model."""

    class Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def wait(*, timeout: float) -> int:
            assert 0 < timeout <= 7
            return -9

    stack = ManagedTuwunelStack()
    old_process = Process(10)
    new_process = Process(11)
    signals: list[tuple[int, int]] = []
    try:
        stack.config_path.write_text(
            "models:\n  default:\n    id: mindroom-live-fuzz-replacement\n",
            encoding="utf-8",
        )
        stack._mindroom_process = cast("Any", old_process)
        monkeypatch.setattr(os, "killpg", lambda pid, signum: signals.append((pid, signum)))
        startup_timeouts: list[float] = []

        def record_start(*, timeout: float) -> None:
            startup_timeouts.append(timeout)
            stack._mindroom_process = cast("Any", new_process)

        monkeypatch.setattr(stack, "_start_mindroom", record_start)

        assert stack.restart_mindroom_for_recovery(timeout=7) is None
        assert signals == [(10, signal.SIGKILL)]
        assert len(startup_timeouts) == 1
        assert 0 < startup_timeouts[0] <= 7
        assert "mindroom-live-fuzz-recovered" in stack.config_path.read_text(encoding="utf-8")
    finally:
        stack._mindroom_process = None
        stack.close()


def test_restart_model_latch_blocks_only_pre_restart_fresh_request() -> None:
    """The old request must remain in flight while the recovered generation stays runnable."""
    stack = ManagedTuwunelStack()
    response_body: list[str] = []
    try:
        model_port = stack._start_model_server()
        router_response = httpx.post(
            f"http://127.0.0.1:{model_port}/v1/chat/completions",
            json={
                "model": "mindroom-live-fuzz",
                "messages": [{"role": "user", "content": "Synthetic fresh startup request"}],
            },
            timeout=5,
        )
        assert "runtime-generation=original" in router_response.json()["choices"][0]["message"]["content"]
        assert not stack.wait_for_blocked_restart_request(timeout=0)

        def send_blocked_request() -> None:
            response = httpx.post(
                f"http://127.0.0.1:{model_port}/v1/chat/completions",
                json={
                    "model": "mindroom-live-fuzz-replacement",
                    "messages": [{"role": "user", "content": "Synthetic fresh startup request"}],
                },
                timeout=5,
            )
            response_body.append(response.json()["choices"][0]["message"]["content"])

        request_thread = threading.Thread(target=send_blocked_request)
        request_thread.start()
        assert stack.wait_for_blocked_restart_request(timeout=1)
        assert request_thread.is_alive()

        recovered = httpx.post(
            f"http://127.0.0.1:{model_port}/v1/chat/completions",
            json={
                "model": "mindroom-live-fuzz-recovered",
                "messages": [{"role": "user", "content": "Synthetic fresh startup request"}],
            },
            timeout=5,
        )
        assert "runtime-generation=recovered" in recovered.json()["choices"][0]["message"]["content"]

        _ModelHandler.blocked_request_release.set()
        request_thread.join(timeout=5)
        assert not request_thread.is_alive()
        assert "runtime-generation=replacement" in response_body[0]
    finally:
        _ModelHandler.blocked_request_release.set()
        stack.close()


def test_restart_model_latch_uses_configured_reply_bound() -> None:
    """The model hold must use the same bound configured for restart observations."""
    stack = ManagedTuwunelStack(model_latch_timeout=17.5)
    try:
        stack._start_model_server()

        assert _ModelHandler.blocked_request_timeout == 17.5
    finally:
        stack.close()


@pytest.mark.parametrize("disconnect", [BrokenPipeError, ConnectionResetError])
def test_model_handler_ignores_client_disconnect_after_latched_request(
    monkeypatch: pytest.MonkeyPatch,
    disconnect: type[OSError],
) -> None:
    """A killed runtime's closed model connection must not escape the request handler."""
    payload = b'{"model":"mindroom-live-fuzz","messages":[]}'
    handler = object.__new__(_ModelHandler)
    handler.path = "/v1/chat/completions"
    handler.headers = {"Content-Length": str(len(payload))}
    handler.rfile = BytesIO(payload)

    def fail_send(_payload: object) -> None:
        raise disconnect

    monkeypatch.setattr(handler, "_send_json", fail_send)

    handler.do_POST()

    assert handler.close_connection


def test_diagnostic_counters_track_live_production_markers() -> None:
    """A counted marker no production module logs is a zero pretending to be evidence.

    Three counters here outlived the module that emitted them and kept
    reporting `0` in every result JSON, which the harness's own test could not
    notice because it fed itself the marker text. Nothing but the real tree
    can answer whether a marker is still live.
    """
    sources = [path.read_text(encoding="utf-8") for path in (PROJECT_ROOT / "src").rglob("*.py")]
    dead = sorted(
        f"{name}={marker}"
        for name, marker in DIAGNOSTIC_MARKERS.items()
        if not any(marker in source for source in sources)
    )

    assert not dead, f"diagnostic counters whose production marker no longer exists: {dead}"


def test_diagnostic_counts_handle_colored_structlog_fields() -> None:
    """ANSI rendering must not turn live counters into structural zeroes."""
    stack = ManagedTuwunelStack()
    try:
        stack.log_path.write_text(
            "".join(f"event=\x1b[35m{marker}\x1b[0m\n" for marker in DIAGNOSTIC_MARKERS.values()),
            encoding="utf-8",
        )

        assert stack.diagnostic_counts() == dict.fromkeys(DIAGNOSTIC_MARKERS, 1)
    finally:
        stack.close()


def test_managed_runtime_overrides_inherited_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host logging settings must not change the restart oracle's renderer or visibility."""
    monkeypatch.setenv("MINDROOM_LOG_FORMAT", "json")
    monkeypatch.setenv("MINDROOM_LOGGER_LEVELS", "mindroom:ERROR")
    monkeypatch.setenv("UV_PYTHON", "3.12")
    stack = ManagedTuwunelStack()
    try:
        stack.homeserver = "http://matrix.invalid"
        stack.server_name = "matrix.invalid"

        environment = stack._mindroom_environment()

        assert environment["MINDROOM_LOG_FORMAT"] == "text"
        assert environment["MINDROOM_LOG_LEVEL"] == "INFO"
        assert environment["MINDROOM_LOGGER_LEVELS"] == ""
        assert "UV_PYTHON" not in environment
    finally:
        stack.close()


def test_managed_runtime_pins_child_to_python_313(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every managed MindRoom child must match the production Python runtime."""

    class Process:
        pid = 4242

        @staticmethod
        def poll() -> None:
            return None

    stack = ManagedTuwunelStack()
    commands: list[list[str]] = []
    try:
        stack.storage_path.mkdir()
        (stack.storage_path / "matrix_state.yaml").write_text(
            "rooms:\n  lobby:\n    room_id: '!room:example'\n",
            encoding="utf-8",
        )
        stack._log_handle = stack.log_path.open("a", encoding="utf-8")
        stack._env = stack._mindroom_environment()

        def record_popen(command: list[str], **_kwargs: object) -> Process:
            commands.append(command)
            return Process()

        def complete_url_wait(_url: str, *, timeout: float) -> None:
            assert 0 < timeout <= 7

        monkeypatch.setattr(subprocess, "Popen", record_popen)
        monkeypatch.setattr(stack, "_wait_for_url", complete_url_wait)

        monkeypatch.setattr(stack, "_wait_for_runtime_attestation", lambda: None)
        stack._start_mindroom(timeout=7)

        assert commands == [
            [
                "uv",
                "run",
                "--locked",
                "--python",
                "3.13",
                "python",
                str(Path(live_fuzz.__file__).resolve()),
                "__mindroom_runtime_child__",
                str(stack.attestation_path),
                str(stack.runtime_redaction_path),
                "run",
                "--api-port",
                str(stack.api_port),
                "--log-level",
                "INFO",
            ],
        ]
    finally:
        stack._mindroom_process = None
        stack.close()


def test_restart_shutdown_rejects_nonzero_process_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bounded process exit is graceful only when shutdown succeeds."""

    class FailedProcess:
        pid = 10
        returncode = 7

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def wait(*, timeout: float) -> int:
            del timeout
            return 7

    stack = ManagedTuwunelStack()
    signals: list[tuple[int, int]] = []
    try:
        process = FailedProcess()
        stack._mindroom_process = cast("Any", process)

        def killpg(pid: int, signum: int) -> None:
            signals.append((pid, signum))
            if signum == 0:
                raise ProcessLookupError

        monkeypatch.setattr(os, "killpg", killpg)

        assert not stack.stop_mindroom(timeout=1)
        assert signals == [(10, signal.SIGINT), (10, 0)]
        assert stack._mindroom_process is None
    finally:
        stack.close()


@pytest.mark.parametrize("returncode", [-signal.SIGINT, 128 + signal.SIGINT])
def test_restart_shutdown_accepts_uv_sigint_after_child_drain(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
) -> None:
    """The uv wrapper's SIGINT status is clean only after the child drain marker."""

    class WrapperProcess:
        pid = 10

        def __init__(self) -> None:
            self.returncode = returncode

        @staticmethod
        def poll() -> None:
            return None

        def wait(self, *, timeout: float) -> int:
            assert timeout == 1
            stack.log_path.write_text(f"{ORDERLY_SHUTDOWN_MARKER}\n", encoding="utf-8")
            return self.returncode

    stack = ManagedTuwunelStack()
    signals: list[tuple[int, int]] = []
    try:
        stack._mindroom_process = cast("Any", WrapperProcess())

        def killpg(pid: int, signum: int) -> None:
            signals.append((pid, signum))
            if signum == 0:
                raise ProcessLookupError

        monkeypatch.setattr(os, "killpg", killpg)

        assert stack.stop_mindroom(timeout=1)
        assert signals == [(10, signal.SIGINT), (10, 0)]
        assert stack._mindroom_process is None
    finally:
        stack.close()


def test_restart_shutdown_rejects_uv_sigint_without_child_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrapper signal alone must not prove that the managed child drained."""

    class WrapperProcess:
        pid = 10
        returncode = 128 + signal.SIGINT

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def wait(*, timeout: float) -> int:
            assert timeout == 1
            return 128 + signal.SIGINT

    stack = ManagedTuwunelStack()
    try:
        stack._mindroom_process = cast("Any", WrapperProcess())

        def killpg(_pid: int, signum: int) -> None:
            if signum == 0:
                raise ProcessLookupError

        monkeypatch.setattr(os, "killpg", killpg)

        assert not stack.stop_mindroom(timeout=1)
    finally:
        stack.close()


def test_restart_shutdown_rejects_forced_process_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """An orderly-shutdown timeout must kill the process and remain non-graceful."""

    class TimedOutProcess:
        returncode: int | None = None

        def __init__(self) -> None:
            self.pid = 10
            self.wait_timeouts: list[float] = []

        @staticmethod
        def poll() -> None:
            return None

        def wait(self, *, timeout: float) -> int:
            self.wait_timeouts.append(timeout)
            if len(self.wait_timeouts) == 1:
                command = "mindroom"
                raise subprocess.TimeoutExpired(command, timeout)
            return -9

    stack = ManagedTuwunelStack()
    process = TimedOutProcess()
    signals: list[tuple[int, int]] = []
    try:
        stack._mindroom_process = cast("Any", process)
        monkeypatch.setattr(os, "killpg", lambda pid, signum: signals.append((pid, signum)))

        assert not stack.stop_mindroom(timeout=1)
        assert signals == [(10, signal.SIGINT), (10, signal.SIGKILL)]
        assert process.wait_timeouts == [1, 10]
        assert stack._mindroom_process is None
    finally:
        stack.close()


class _GracefulShutdownProcess:
    """Faithful managed-process seam for lifecycle shutdown tests."""

    pid = 10

    def __init__(
        self,
        log_path: Path,
        *,
        emit_orderly_marker: bool,
        returncode: int = 128 + signal.SIGINT,
    ) -> None:
        self.log_path = log_path
        self.emit_orderly_marker = emit_orderly_marker
        self.returncode = returncode

    @staticmethod
    def poll() -> None:
        return None

    def wait(self, *, timeout: float) -> int:
        assert 0 < timeout <= live_fuzz.MINDROOM_SHUTDOWN_TIMEOUT_SECONDS
        if self.emit_orderly_marker:
            with self.log_path.open("a", encoding="utf-8") as log:
                log.write(f"{ORDERLY_SHUTDOWN_MARKER}\n")
        return self.returncode


def _install_graceful_shutdown_process(
    stack: ManagedTuwunelStack,
    monkeypatch: pytest.MonkeyPatch,
    *,
    emit_orderly_marker: bool,
    group_survives_sigint: bool = False,
) -> list[signal.Signals | int]:
    """Install a managed leader and a process group with realistic signal state."""
    stack._mindroom_process = cast(
        "Any",
        _GracefulShutdownProcess(
            stack.log_path,
            emit_orderly_marker=emit_orderly_marker,
        ),
    )
    group_alive = True
    signals: list[signal.Signals | int] = []

    def killpg(_pid: int, sent_signal: signal.Signals | int) -> None:
        nonlocal group_alive
        signals.append(sent_signal)
        if sent_signal == 0:
            if group_alive:
                return
            raise ProcessLookupError
        if (sent_signal == signal.SIGINT and not group_survives_sigint) or sent_signal == signal.SIGKILL:
            group_alive = False

    monkeypatch.setattr(live_fuzz.os, "killpg", killpg)
    monkeypatch.setattr(live_fuzz, "_PROCESS_GROUP_GRACE_SECONDS", 0.0)
    return signals


@pytest.mark.parametrize("stale_marker", [False, True])
def test_cold_restart_requires_fresh_orderly_marker_before_reset(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stale_marker: bool,
) -> None:
    """A cold restart cannot erase cursors after an incompletely proven stop."""
    stack = ManagedTuwunelStack()
    resets: list[Path] = []
    starts: list[int] = []
    try:
        if stale_marker:
            stack.log_path.write_text(f"{ORDERLY_SHUTDOWN_MARKER}\n", encoding="utf-8")
        _install_graceful_shutdown_process(stack, monkeypatch, emit_orderly_marker=False)
        monkeypatch.setattr(live_fuzz, "_reset_durable_sync_cursors", resets.append)
        monkeypatch.setattr(stack, "_start_mindroom", lambda: starts.append(1))

        with pytest.raises(RuntimeError, match="fresh orderly shutdown marker"):
            stack.cold_restart_mindroom()

        assert resets == []
        assert starts == []
    finally:
        stack.close()


def test_final_cleanup_rejects_stale_orderly_marker_and_finishes_other_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final cleanup retains a missing-marker failure after removing stack storage."""
    stack = ManagedTuwunelStack()
    stack.log_path.write_text(f"{ORDERLY_SHUTDOWN_MARKER}\n", encoding="utf-8")
    root = stack.root
    _install_graceful_shutdown_process(stack, monkeypatch, emit_orderly_marker=False)

    with pytest.raises(ExceptionGroup, match="live Matrix fuzz cleanup failed") as raised:
        stack.close()

    assert "fresh orderly shutdown marker" in str(raised.value.exceptions[0])
    assert not root.exists()


def test_restart_rejects_surviving_group_without_starting_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary restart cleans a surviving group and refuses replacement."""
    stack = ManagedTuwunelStack()
    starts: list[int] = []
    try:
        signals = _install_graceful_shutdown_process(
            stack,
            monkeypatch,
            emit_orderly_marker=True,
            group_survives_sigint=True,
        )
        monkeypatch.setattr(stack, "_start_mindroom", lambda: starts.append(1))

        with pytest.raises(AssertionError, match="did not shut down cleanly"):
            stack.restart_mindroom()

        assert signal.SIGKILL in signals
        assert starts == []
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_planned_outage_rejects_and_cleans_surviving_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A planned outage cannot accept a leader exit while its group survives."""
    stack = ManagedTuwunelStack()
    runner = object.__new__(LiveFuzzRunner)
    runner.stack = stack
    runner._mindroom_running = True
    runner.outage_count = 0
    runner._journal = None
    try:
        signals = _install_graceful_shutdown_process(
            stack,
            monkeypatch,
            emit_orderly_marker=True,
            group_survives_sigint=True,
        )

        with pytest.raises(AssertionError, match="planned outage"):
            await runner._apply_lifecycle(LiveOperationKind.STOP_MINDROOM, 0)

        assert signal.SIGKILL in signals
        assert runner._mindroom_running
        assert runner.outage_count == 0
    finally:
        stack.close()


def test_graceful_stop_accepts_fresh_marker_and_gone_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh marker and a drained process group prove graceful shutdown."""
    stack = ManagedTuwunelStack()
    try:
        signals = _install_graceful_shutdown_process(stack, monkeypatch, emit_orderly_marker=True)

        assert stack.stop_mindroom(timeout=1)
        assert signals == [signal.SIGINT, 0]
        assert stack._mindroom_process is None
    finally:
        stack.close()


def test_restart_refuses_to_continue_after_an_unclean_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A restart that discards the shutdown verdict cannot tell SIGKILL from clean.

    `stop_mindroom` already knows whether the child stopped on its own signal
    and logged an orderly bot shutdown. Ignoring that made a hung drain
    followed by a kill look exactly like a healthy restart, and the run went
    on to report PASS.
    """
    stack = ManagedTuwunelStack()
    started: list[int] = []
    try:
        monkeypatch.setattr(stack, "stop_mindroom", lambda: False)
        monkeypatch.setattr(stack, "_start_mindroom", lambda: started.append(1))

        with pytest.raises(AssertionError, match="did not shut down cleanly"):
            stack.restart_mindroom()

        assert started == []
    finally:
        stack.close()


class _RestartOrderClient(_RecordingDormantClient):
    """Record sends into the shared restart-boundary ordering."""

    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self.order = order

    async def send_event(
        self,
        event_type: str,
        txn_id: str,
        content: dict[str, Any],
        *,
        room_id: str | None = None,
    ) -> str:
        self.order.append("send")
        return await super().send_event(event_type, txn_id, content, room_id=room_id)


class _RestartOrderStack(ManagedTuwunelStack):
    """Answer the interruption boundary without a live runtime or journal."""

    def __init__(self, *, pending_work: bool) -> None:
        super().__init__()
        self.agent_id, self.router_id = "@agent:example", "@router:example"
        self.pending_work = pending_work
        self.order: list[str] = []

    def wait_for_pending_journal_work(self, *, timeout: float) -> bool:
        assert timeout == 1
        self.order.append("wait-pending")
        return self.pending_work

    def restart_mindroom(self) -> None:
        self.order.append("restart")

    def crash_mindroom(self, *, timeout: float = 20) -> None:
        del timeout
        self.order.append("crash")


class _RestartOrderRunner(LiveFuzzRunner):
    """Satisfy every outstanding reply so the batch loop can complete."""

    async def _await_replies(self) -> None:
        stack = cast("_RestartOrderStack", self.stack)
        outstanding = self.oracle.outstanding()
        stack.order.append(f"await:{len(outstanding)}")
        for event_id in outstanding:
            self.oracle.response_ids[event_id].add(f"{event_id}-reply")


def _restart_order_runner(
    *,
    pending_work: bool,
    kind: LiveOperationKind = LiveOperationKind.RESTART_MINDROOM,
) -> _RestartOrderRunner:
    """Build one batch whose interruption must land while a reply is still owed."""
    stack = _RestartOrderStack(pending_work=pending_work)
    scenario = LiveFuzzScenario(
        thread_count=1,
        batches=(
            (
                LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "root:0"),
                LiveOperation(1, kind, 0, None),
            ),
        ),
    )
    scenario.validate()
    runner = _RestartOrderRunner(
        stack,
        (cast("LiveMatrixClient", _RestartOrderClient(stack.order)),),
        scenario,
        reply_timeout=1,
        settle_seconds=0,
    )
    runner.event_ids["root:0"] = "$root0"
    return runner


@pytest.mark.parametrize(
    ("kind", "expected_call", "expected_counts"),
    [
        pytest.param(LiveOperationKind.RESTART_MINDROOM, "restart", (1, 0), id="graceful"),
        pytest.param(LiveOperationKind.CRASH_MINDROOM, "crash", (0, 1), id="hard"),
    ],
)
@pytest.mark.asyncio
async def test_interruption_lands_after_the_batch_is_sent_and_before_its_replies(
    kind: LiveOperationKind,
    expected_call: str,
    expected_counts: tuple[int, int],
) -> None:
    """The interruption must happen with the batch committed and unanswered."""
    runner = _restart_order_runner(pending_work=True, kind=kind)
    try:
        result = await runner._run_batches(runner.scenario.batches)

        assert cast("_RestartOrderStack", runner.stack).order == ["send", "wait-pending", expected_call, "await:1"]
        assert (result["restarts"], result["crashes"]) == expected_counts
        assert result["interruptions_with_work_outstanding"] == 1
    finally:
        runner.stack.close()


@pytest.mark.asyncio
async def test_run_fails_when_an_interruption_found_no_work_to_interrupt() -> None:
    """`restarts: 18` must not be reportable when every one hit an idle runtime."""
    runner = _restart_order_runner(pending_work=False)
    try:
        with pytest.raises(AssertionError, match="found no committed unfinished journal work"):
            await runner._run_batches(runner.scenario.batches)
    finally:
        runner.stack.close()


def test_crash_kills_the_runtime_without_giving_it_a_chance_to_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash must not be a restart with extra steps.

    SIGINT lets MindRoom finish the turn it was running, which tests the drain
    and leaves the journal nothing to recover. Only a signal it cannot answer
    puts committed, unfinished work in front of durable recovery.
    """

    class Process:
        pid = 10

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def wait(*, timeout: float) -> int:
            assert timeout == 7
            return -9

    stack = ManagedTuwunelStack()
    signals: list[tuple[int, int]] = []
    started: list[int] = []
    orderly_stops: list[int] = []
    try:
        stack._mindroom_process = cast("Any", Process())
        monkeypatch.setattr(os, "killpg", lambda pid, signum: signals.append((pid, signum)))
        monkeypatch.setattr(stack, "_start_mindroom", lambda: started.append(1))
        monkeypatch.setattr(stack, "stop_mindroom", lambda **_kwargs: bool(orderly_stops.append(1)))

        stack.crash_mindroom(timeout=7)

        assert signals == [(10, signal.SIGKILL)]
        assert started == [1]
        assert orderly_stops == []
        assert stack._mindroom_process is None
    finally:
        stack._mindroom_process = None
        stack.close()


def test_pending_journal_work_counts_only_unsettled_events() -> None:
    """The interruption probe must read the production journal, not a log line.

    A restart is worth taking only while the journal owes something, so what
    the probe counts has to be the durable state MindRoom actually writes --
    admitted and not yet settled -- and not a marker the harness invented.
    """
    stack = ManagedTuwunelStack()
    try:
        assert stack.pending_journal_event_count() == 0

        stack.agent_id = "@agent:example"
        store = EventJournalStore.open_sqlite(stack.storage_path / "tracking" / "event_journal.db")

        async def seed() -> None:
            principal = store.principal(f"general@{stack.agent_id}")
            for event_id in ("$settled", "$pending"):
                await principal.admit(
                    InboundEvent(
                        event_id=event_id,
                        room_id="!room:example",
                        thread_id=None,
                        kind=EventKind.MESSAGE,
                        event_class=EventClass.ACTIONABLE,
                        sender="@user:example",
                        origin_server_ts=1,
                        source={"event_id": event_id},
                    ),
                )
            await principal.settle("$settled")

        asyncio.run(seed())

        assert stack.pending_journal_event_count() == 1
        assert stack.wait_for_pending_journal_work(timeout=0.1)
    finally:
        stack.close()


def test_restart_shutdown_failure_count_tracks_emitted_durable_recovery_marker() -> None:
    """The harness must gate on the production marker emitted by its recovery path."""
    assert any(
        RESTART_SHUTDOWN_FAILURE_MARKER in path.read_text(encoding="utf-8")
        for path in (PROJECT_ROOT / "src").rglob("*.py")
    )
    stack = ManagedTuwunelStack()
    try:
        stack.log_path.write_text(
            f'{{"event": "{RESTART_SHUTDOWN_FAILURE_MARKER}"}}\n',
            encoding="utf-8",
        )

        assert stack.restart_shutdown_failure_count() == 1
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_restart_observation_rejects_incomplete_runtime_drain_from_replacement(
    seeded_restart_observation_stack: tuple[ManagedTuwunelStack, list[float]],
) -> None:
    """An incomplete runtime drain before final shutdown must not become the accepted baseline."""
    stack, stop_calls = seeded_restart_observation_stack
    observation = await _collect_seeded_restart_observation(
        stack,
        log=_RESTART_OBSERVATION_LOG + f'{{"event": "{RESTART_SHUTDOWN_FAILURE_MARKER}"}}\n',
        events=(_restart_response("$agent-response", stack.agent_id, "$fresh"),),
    )

    assert stop_calls == [0.05]
    assert not observation.orderly_drain_completed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_stop_calls", "expected_orderly_drain", "expected_failure"),
    [
        ("historical-callback", [0.05], True, "historical_callback_suppressed"),
        ("router-response", [], None, "fresh_agent_response_exactly_once"),
        ("old-generation", [], None, "recovered_generation_response_observed"),
    ],
)
async def test_restart_observation_rejects_nonqualifying_evidence(
    seeded_restart_observation_stack: tuple[ManagedTuwunelStack, list[float]],
    case: str,
    expected_stop_calls: list[float],
    expected_orderly_drain: bool | None,
    expected_failure: str,
) -> None:
    """Only exact recovered-agent evidence may complete final observation."""
    stack, stop_calls = seeded_restart_observation_stack
    log = _RESTART_OBSERVATION_LOG
    sender = stack.agent_id
    event_id = "$agent-response"
    body = "LIVE-FUZZ runtime-generation=recovered END call=1"
    if case == "historical-callback":
        log = "matrix_event_callback_started event_id=$old-media room_id=!restart:example\n" + log
    elif case == "router-response":
        sender = stack.router_id
        event_id = "$router-response"
    else:
        event_id = "$old-runtime-response"
        body = "LIVE-FUZZ runtime-generation=replacement END call=1"

    observation = await _collect_seeded_restart_observation(
        stack,
        log=log,
        events=(_restart_response(event_id, sender, "$fresh", body=body),),
    )

    assert stop_calls == expected_stop_calls
    assert observation.orderly_drain_completed is expected_orderly_drain
    assert any(f"invariant={expected_failure}" in failure for failure in evaluate_restart_regression(observation))


@pytest.mark.asyncio
async def test_restart_observation_rejects_mixed_runtime_generations(
    seeded_restart_observation_stack: tuple[ManagedTuwunelStack, list[float]],
) -> None:
    """Any duplicate response from the old runtime must invalidate recovered-generation evidence."""
    stack, _stop_calls = seeded_restart_observation_stack
    response_ids = ("$agent-response-a", "$agent-response-b")
    selected_first = response_ids[0]
    events = tuple(
        _restart_response(
            response_id,
            stack.agent_id,
            "$fresh",
            body=(
                "LIVE-FUZZ runtime-generation=recovered END call=1"
                if response_id == selected_first
                else "LIVE-FUZZ runtime-generation=replacement END call=1"
            ),
        )
        for response_id in response_ids
    )

    observation = await _collect_seeded_restart_observation(
        stack,
        log=_RESTART_OBSERVATION_LOG,
        events=events,
        reply_timeout=0,
    )

    assert not observation.recovered_generation_response_observed


@pytest.mark.asyncio
async def test_restart_observation_samples_real_evidence_when_deadline_already_expired(
    seeded_restart_observation_stack: tuple[ManagedTuwunelStack, list[float]],
) -> None:
    """A zero observation window must report durable state instead of fabricated zeros."""
    stack, stop_calls = seeded_restart_observation_stack
    observation = await _collect_seeded_restart_observation(
        stack,
        log=(
            "matrix_event_callback_started agent_name=general event_id=$fresh room_id=!restart:example\n"
            "matrix_event_callback_started agent_name=general event_id=$fresh room_id=!restart:example\n"
            "matrix_event_callback_started agent_name=general event_id=$fresh room_id=!restart:example\n"
            + _RESTART_OBSERVATION_LOG
        ),
        events=(_restart_response("$agent-response", stack.agent_id, "$fresh"),),
        reply_timeout=0,
    )

    assert stop_calls == [0]
    assert observation.projected_after_answer_count == 4
    assert observation.fresh_agent_output_count == 1
    assert observation.fresh_response_complete
    assert observation.fresh_semantic_ingress_count == 2
    assert observation.recovered_generation_response_observed
    assert observation.fresh_obligation_recovered
    assert observation.fresh_prompt_observed


@pytest.mark.asyncio
async def test_restart_observation_reports_incomplete_fresh_response(
    seeded_restart_observation_stack: tuple[ManagedTuwunelStack, list[float]],
) -> None:
    """A truncated recovered response must identify response completion as the failed invariant."""
    stack, stop_calls = seeded_restart_observation_stack
    observation = await _collect_seeded_restart_observation(
        stack,
        log=_RESTART_OBSERVATION_LOG,
        events=(
            _restart_response(
                "$agent-response",
                stack.agent_id,
                "$fresh",
                body="LIVE-FUZZ runtime-generation=recovered partial",
            ),
        ),
        reply_timeout=0.01,
    )

    assert stop_calls == []
    assert not observation.fresh_response_complete
    assert any("invariant=fresh_response_complete" in failure for failure in evaluate_restart_regression(observation))


@pytest.mark.asyncio
async def test_restart_response_index_honors_sender_override() -> None:
    """Agent and router observations must use their explicitly selected sender."""
    stack = ManagedTuwunelStack()
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    try:
        stack.agent_id, stack.router_id = "@agent:example", "@router:example"
        runner = LiveFuzzRunner(
            stack,
            (client,),
            restart_regression_scenario(),
            reply_timeout=1,
            settle_seconds=0,
        )

        def response(event_id: str, sender: str, source: str) -> dict[str, Any]:
            return {
                "event_id": event_id,
                "sender": sender,
                "type": "m.room.message",
                "content": {
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": source,
                        "m.in_reply_to": {"event_id": source},
                    },
                },
            }

        events = (
            response("$agent-response", stack.agent_id, "$agent-source"),
            response("$router-response", stack.router_id, "$router-source"),
        )

        assert runner._canonical_response_ids(events) == {"$agent-source": {"$agent-response"}}
        assert runner._canonical_response_ids(events, sender_id=stack.router_id) == {
            "$router-source": {"$router-response"},
        }
    finally:
        await client.close()
        stack.close()


@pytest.mark.asyncio
async def test_generic_response_index_preserves_nested_thread_root_and_direct_source() -> None:
    """A reply inside an existing thread may target a source below that thread's root."""
    stack = ManagedTuwunelStack()
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    try:
        stack.agent_id = "@agent:example"
        runner = LiveFuzzRunner(
            stack,
            (client,),
            LiveFuzzScenario(thread_count=1, batches=()),
            reply_timeout=1,
            settle_seconds=0,
        )
        nested_reply = {
            "event_id": "$response",
            "sender": stack.agent_id,
            "type": "m.room.message",
            "content": {
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread-root",
                    "m.in_reply_to": {"event_id": "$nested-source"},
                },
            },
        }

        assert runner._canonical_response_ids(
            (nested_reply,),
            root_event_id="$thread-root",
        ) == {"$nested-source": {"$response"}}
    finally:
        await client.close()
        stack.close()


@pytest.mark.asyncio
async def test_restart_observation_rejects_historical_output_arriving_during_callback_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A historical reply arriving while callbacks drain must still fail."""

    class DormantClient:
        room_id = "!restart:example"

        def __init__(self) -> None:
            self.seen_events: dict[str, dict[str, Any]] = {}
            self.sync_count = 0
            self.pending_historical_event: dict[str, Any] | None = None

        async def sync_incremental(self, *, timeout_ms: int, allow_limited: bool = False) -> None:
            del timeout_ms, allow_limited
            self.sync_count += 1
            if self.sync_count == 1:
                self.seen_events["$fresh-response"] = response(
                    "$fresh-response",
                    "@agent:example",
                    "$fresh",
                )
            if self.sync_count >= 2 and self.pending_historical_event is not None:
                self.seen_events["$late-historical-response"] = self.pending_historical_event
            await asyncio.sleep(0.05)

    def response(event_id: str, sender: str, source: str) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "sender": sender,
            "type": "m.room.message",
            "content": {
                "body": "LIVE-FUZZ runtime-generation=recovered END call=1",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": source,
                    "m.in_reply_to": {"event_id": source},
                },
            },
        }

    stack = ManagedTuwunelStack()
    stop_calls: list[float] = []
    try:
        stack.agent_id, stack.router_id = "@agent:example", "@router:example"
        stack.log_path.write_text(
            "Received message agent=general event_id=$fresh room_id=!restart:example\n"
            "Received message agent=general event_id=$fresh room_id=!restart:example\n"
            "Preparing agent and prompt agent=general $fresh\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(stack, "projected_restart_event_pair_count", lambda _room_id, _event_ids: 4)
        monkeypatch.setattr(stack, "restart_journal_event_state", lambda _event_id: "settled")
        dormant = DormantClient()

        def drain_callbacks(*, timeout: float = 20) -> bool:
            stop_calls.append(timeout)
            assert timeout == 2
            time.sleep(1.2)
            dormant.pending_historical_event = response(
                "$late-historical-response",
                "@agent:example",
                "$old-text",
            )
            with stack.log_path.open("a", encoding="utf-8") as log:
                log.write(f'{{"event": "{RESTART_SHUTDOWN_FAILURE_MARKER}"}}\n')
            return True

        original_stop_mindroom = stack.stop_mindroom
        monkeypatch.setattr(stack, "stop_mindroom", drain_callbacks)
        runner = LiveFuzzRunner(
            stack,
            (cast("LiveMatrixClient", dormant),),
            restart_regression_scenario(),
            reply_timeout=2,
            settle_seconds=0,
        )

        try:
            observation = await runner._wait_for_restart_observation(
                cast("LiveMatrixClient", dormant),
                historical_event_ids=("$old-text", "$old-media"),
                fresh_event_id="$fresh",
                fresh_semantic_ingress_count_before_restart=1,
            )
        finally:
            monkeypatch.setattr(stack, "stop_mindroom", original_stop_mindroom)

        assert dormant.sync_count == 2
        assert stop_calls == [2]
        assert not observation.orderly_drain_completed
        assert observation.historical_output_counts == (1, 0)
        assert any(
            "invariant=historical_output_suppressed" in failure for failure in evaluate_restart_regression(observation)
        )
        assert any(
            "invariant=orderly_drain_completed" in failure for failure in evaluate_restart_regression(observation)
        )
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_exact_reply_oracle_counts_only_canonical_agent_thread_replies() -> None:
    """Edits and duplicate sync delivery must not inflate canonical counts."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    oracle.expect("root:0", "$source")

    canonical: dict[str, Any] = {
        "event_id": "$response",
        "sender": "@agent:example",
        "type": "m.room.message",
        "content": {
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$source",
                "m.in_reply_to": {"event_id": "$source"},
            },
        },
    }
    oracle._ingest_event(canonical)
    oracle._ingest_event(canonical)
    oracle._ingest_event(
        {
            **canonical,
            "event_id": "$edit",
            "content": {
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$response",
                },
            },
        },
    )

    assert oracle.response_ids == {"$source": {"$response"}}
    assert oracle.resolve_response_ref("response:root:0") == "$response"
    oracle._assert_no_wrong_replies()
    await client.close()


@pytest.mark.asyncio
async def test_exact_reply_oracle_rejects_duplicate_canonical_replies() -> None:
    """Two distinct agent events replying to one input must fail immediately."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    oracle.expect("root:0", "$source")
    for event_id in ("$response-one", "$response-two"):
        oracle._ingest_event(
            {
                "event_id": event_id,
                "sender": "@agent:example",
                "type": "m.room.message",
                "content": {
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$source",
                        "m.in_reply_to": {"event_id": "$source"},
                    },
                },
            },
        )

    with pytest.raises(AssertionError, match="duplicates"):
        oracle._assert_no_wrong_replies()
    await client.close()


@pytest.mark.asyncio
async def test_exact_reply_oracle_allows_response_to_internal_restart_relay() -> None:
    """Restart recovery may validly answer a router-authored resume relay."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(
        client,
        "@agent:example",
        internal_relay_senders=("@router:example",),
    )
    try:
        oracle.expect("root:0", "$root")
        oracle._ingest_event(
            _threaded_reply_event(
                sender="@agent:example",
                event_id="$interrupted",
                thread_root="$root",
                in_reply_to="$root",
                body=f"partial {RESTART_INTERRUPTED_RESPONSE_NOTE}",
            ),
        )
        oracle._ingest_event(
            {
                "event_id": "$resume-relay",
                "sender": "@router:example",
                "type": "m.room.message",
                "content": {
                    "body": AUTO_RESUME_MESSAGE,
                    SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$root",
                        "m.in_reply_to": {"event_id": "$interrupted"},
                    },
                },
            },
        )
        oracle._ingest_event(
            {
                "event_id": "$response",
                "sender": "@agent:example",
                "type": "m.room.message",
                "content": {
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$root",
                        "m.in_reply_to": {"event_id": "$resume-relay"},
                    },
                },
            },
        )
        oracle._assert_no_wrong_replies()
    finally:
        await client.close()


class _FakeClock:
    """A monotonic clock the harness tests advance on purpose."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        """Return the current fake time."""
        return self.now


class _ScriptedSyncClient:
    """A Matrix client whose sync drives a fake clock and scripted replies."""

    room_id = "!room:example"
    room_ids = (room_id,)

    def __init__(
        self,
        clock: _FakeClock,
        *,
        tick: float,
        deliveries: tuple[tuple[float, str], ...] = (),
    ) -> None:
        self.clock = clock
        self.tick = tick
        self._deliveries = sorted(deliveries)
        self._delivered = 0

    async def sync(self, since: str | None, *, timeout_ms: int) -> dict[str, Any]:
        """Advance the clock one poll and hand back whatever is due."""
        del since, timeout_ms
        await asyncio.sleep(0)
        self.clock.now += self.tick
        events: list[dict[str, Any]] = []
        while self._delivered < len(self._deliveries) and self._deliveries[self._delivered][0] <= self.clock.now:
            _due_at, source_event_id = self._deliveries[self._delivered]
            self._delivered += 1
            events.append(
                {
                    "event_id": f"{source_event_id}-reply",
                    "sender": "@agent:example",
                    "type": "m.room.message",
                    "content": {
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": "$root",
                            "m.in_reply_to": {"event_id": source_event_id},
                        },
                    },
                },
            )
        return {
            "next_batch": f"s{self.clock.now}",
            "rooms": {"join": {self.room_id: {"timeline": {"limited": False, "events": events}}}},
        }


def _scripted_oracle(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tick: float,
    sources: tuple[str, ...],
    deliveries: tuple[tuple[float, str], ...] = (),
) -> tuple[ExactReplyOracle, _FakeClock]:
    """Build an oracle whose only clock and traffic come from the test."""
    clock = _FakeClock()
    monkeypatch.setattr(fuzz_live_matrix, "time", clock)
    client = _ScriptedSyncClient(clock, tick=tick, deliveries=deliveries)
    oracle = ExactReplyOracle(cast("LiveMatrixClient", client), "@agent:example")
    for index, source_event_id in enumerate(sources):
        oracle.expect(f"op:{index}", source_event_id)
    return oracle, clock


def test_wait_budget_scales_with_the_work_and_keeps_the_single_turn_floor() -> None:
    """A wait for many sequential turns must not share a one-turn deadline."""
    single = WaitBudget(turns=1, per_turn_seconds=2.0, settle_seconds=0.75, floor_seconds=60.0)
    many = WaitBudget(turns=45, per_turn_seconds=2.0, settle_seconds=0.75, floor_seconds=60.0)

    assert single.seconds == pytest.approx(60.75)
    assert many.seconds == pytest.approx(45 * 2.0 * 3.0 + 0.75)
    assert many.seconds > single.seconds
    # An unmeasured machine falls back to exactly the operator's deadline.
    assert WaitBudget(turns=45, per_turn_seconds=0.0, settle_seconds=0.75, floor_seconds=60.0).seconds == pytest.approx(
        60.75,
    )


def test_wait_budget_derives_the_stall_window_from_measured_latency() -> None:
    """Silence long enough to cover several turns is a wedge, not slowness."""
    fast = WaitBudget(turns=45, per_turn_seconds=2.0, settle_seconds=0.0, floor_seconds=1.0)
    slow = WaitBudget(turns=45, per_turn_seconds=30.0, settle_seconds=0.0, floor_seconds=1.0)

    assert fast.stall_seconds == pytest.approx(8.0)
    assert slow.stall_seconds == pytest.approx(120.0)
    # The wedge detector always fires long before the whole-batch deadline.
    assert fast.stall_seconds < fast.seconds
    assert slow.stall_seconds < slow.seconds


def test_turn_latency_monitor_keeps_the_slowest_observed_turn() -> None:
    """Budgets must follow the machine's worst turn, not its luckiest."""
    monitor = TurnLatencyMonitor()

    assert monitor.per_turn_seconds == 0.0

    monitor.observe(turns=8, elapsed_seconds=8.0)
    assert monitor.per_turn_seconds == pytest.approx(1.0)

    monitor.observe(turns=4, elapsed_seconds=12.0)
    assert monitor.per_turn_seconds == pytest.approx(3.0)

    monitor.observe(turns=10, elapsed_seconds=1.0)
    assert monitor.per_turn_seconds == pytest.approx(3.0)

    # Waits that drove no turn and impossible durations teach nothing.
    monitor.observe(turns=0, elapsed_seconds=99.0)
    monitor.observe(turns=5, elapsed_seconds=-1.0)
    assert monitor.per_turn_seconds == pytest.approx(3.0)


class _ChatteringSyncClient:
    """A client whose bots keep answering each other after the work is done."""

    room_id = "!room:example"
    room_ids = (room_id,)

    def __init__(self, clock: _FakeClock, *, tick: float) -> None:
        self.clock = clock
        self.tick = tick
        self._round = 0

    async def sync(self, since: str | None, *, timeout_ms: int) -> dict[str, Any]:
        """Emit one fresh router prompt and one fresh agent answer per poll."""
        del since, timeout_ms
        await asyncio.sleep(0)
        self.clock.now += self.tick
        self._round += 1
        relay = f"$relay{self._round}"
        prior_reply = "$a-reply" if self._round == 1 else f"$relay{self._round - 1}-answer"
        events: list[dict[str, Any]] = []
        if self._round == 1:
            events.append(
                _threaded_reply_event(
                    sender="@agent:example",
                    event_id="$a-reply",
                    thread_root="$root",
                    in_reply_to="$a",
                    body=f"partial {RESTART_INTERRUPTED_RESPONSE_NOTE}",
                ),
            )
        events.extend(
            [
                _resume_relay_event(event_id=relay, thread_root="$root", in_reply_to=prior_reply),
                _threaded_reply_event(
                    sender="@agent:example",
                    event_id=f"{relay}-answer",
                    thread_root="$root",
                    in_reply_to=relay,
                    body=f"partial {RESTART_INTERRUPTED_RESPONSE_NOTE}",
                ),
            ],
        )
        return {
            "next_batch": f"s{self.clock.now}",
            "rooms": {"join": {self.room_id: {"timeline": {"limited": False, "events": events}}}},
        }


@pytest.mark.asyncio
async def test_wait_fails_when_the_room_never_goes_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bots looping at each other must fail the wait, not extend it forever."""
    clock = _FakeClock()
    monkeypatch.setattr(fuzz_live_matrix, "time", clock)
    client = _ChatteringSyncClient(clock, tick=0.1)
    oracle = ExactReplyOracle(
        cast("LiveMatrixClient", client),
        "@agent:example",
        internal_relay_senders=("@router:example",),
    )
    oracle.expect("op:0", "$a")
    budget = WaitBudget(turns=1, per_turn_seconds=0.0, settle_seconds=0.5, floor_seconds=2.0)

    with pytest.raises(AssertionError, match="never went quiet"):
        await oracle.wait_until_exact(budget)

    assert clock.now == pytest.approx(budget.stall_seconds, abs=0.2)


@pytest.mark.asyncio
async def test_wait_reports_a_silent_runtime_as_wedged_long_before_its_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bot that answers nothing must fail fast, not run out its whole budget."""
    oracle, clock = _scripted_oracle(monkeypatch, tick=0.05, sources=("$a", "$b", "$c"))
    budget = WaitBudget(turns=3, per_turn_seconds=10.0, settle_seconds=0.0, floor_seconds=1.0)

    with pytest.raises(ExactReplyTimeoutError) as failure:
        await oracle.wait_until_exact(budget)

    assert failure.value.wedged is True
    assert failure.value.waited_seconds == pytest.approx(budget.stall_seconds, abs=0.1)
    assert failure.value.waited_seconds < budget.seconds
    assert set(failure.value.missing) == {"$a", "$b", "$c"}
    assert "wedged rather than slow" in str(failure.value)
    assert clock.now == pytest.approx(failure.value.waited_seconds, abs=0.1)


@pytest.mark.asyncio
async def test_wait_extends_its_deadline_while_replies_are_still_arriving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow machine that keeps finishing turns must be allowed to finish."""
    sources = tuple(f"$s{index}" for index in range(6))
    deliveries = tuple((1.5 * (index + 1), source) for index, source in enumerate(sources))
    oracle, clock = _scripted_oracle(monkeypatch, tick=0.1, sources=sources, deliveries=deliveries)
    budget = WaitBudget(turns=6, per_turn_seconds=0.3, settle_seconds=0.0, floor_seconds=2.0)
    notices: list[SlowWaitNotice] = []

    elapsed = await oracle.wait_until_exact(budget, on_slow=notices.append)

    assert budget.seconds == pytest.approx(5.4)
    assert elapsed > budget.seconds
    assert clock.now == pytest.approx(9.0, abs=0.2)
    assert [notice.extension for notice in notices] == [1]
    assert "slow machine" in notices[0].render()
    assert not oracle.outstanding()


@pytest.mark.asyncio
async def test_wait_stops_extending_for_a_reply_stream_that_never_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A livelock that dribbles one reply at a time must still fail."""
    sources = tuple(f"$s{index}" for index in range(200))
    deliveries = tuple((1.5 * (index + 1), source) for index, source in enumerate(sources))
    oracle, _clock = _scripted_oracle(monkeypatch, tick=0.1, sources=sources, deliveries=deliveries)
    budget = WaitBudget(turns=200, per_turn_seconds=0.009, settle_seconds=0.0, floor_seconds=2.0)
    notices: list[SlowWaitNotice] = []

    with pytest.raises(ExactReplyTimeoutError) as failure:
        await oracle.wait_until_exact(budget, on_slow=notices.append)

    assert [notice.extension for notice in notices] == [1, 2, 3]
    assert failure.value.wedged is False
    assert "deadline extensions were exhausted" in str(failure.value)


@pytest.mark.asyncio
async def test_wait_never_extends_a_window_that_produced_no_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget shorter than its own stall window must not buy a wedge more time."""
    oracle, _clock = _scripted_oracle(monkeypatch, tick=0.1, sources=("$a",))
    # A one-turn budget that expires before the silence detector would.
    budget = WaitBudget(turns=1, per_turn_seconds=1.0, settle_seconds=0.0, floor_seconds=3.0)
    notices: list[SlowWaitNotice] = []

    with pytest.raises(ExactReplyTimeoutError) as failure:
        await oracle.wait_until_exact(budget, on_slow=notices.append)

    assert budget.seconds == pytest.approx(3.0)
    assert budget.stall_seconds == pytest.approx(4.0)
    assert notices == []
    assert failure.value.wedged is True
    assert failure.value.waited_seconds == pytest.approx(3.0, abs=0.15)


@pytest.mark.asyncio
async def test_wait_fails_immediately_when_the_managed_runtime_has_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead MindRoom process must never be waited out as a slow one."""
    oracle, clock = _scripted_oracle(monkeypatch, tick=0.05, sources=("$a",))
    budget = WaitBudget(turns=1, per_turn_seconds=100.0, settle_seconds=0.0, floor_seconds=100.0)

    def died() -> None:
        msg = "MindRoom exited with code 1 while the harness was waiting for replies"
        raise AssertionError(msg)

    with pytest.raises(AssertionError, match="MindRoom exited with code 1"):
        await oracle.wait_until_exact(budget, liveness=died)

    assert clock.now == pytest.approx(0.05)


class _ExitedProcess:
    """A managed child that has already exited."""

    returncode = 3

    def poll(self) -> int:
        """Report the recorded exit status."""
        return self.returncode


def test_require_runtime_alive_reports_an_exited_child() -> None:
    """The liveness probe must read the managed child's real exit status."""
    stack = ManagedTuwunelStack()
    try:
        stack.require_runtime_alive()

        stack._mindroom_process = cast("subprocess.Popen[str]", _ExitedProcess())
        with pytest.raises(AssertionError, match="MindRoom exited with code 3"):
            stack.require_runtime_alive()
    finally:
        stack._mindroom_process = None
        stack.close()


def _journal_row(*, state: str) -> JournalRow:
    """Build one durable journal row for the classifier."""
    return JournalRow(
        principal_id="general@@agent:example",
        kind="message",
        state=state,
        semantic_consumer=None,
        receipt_order=12,
    )


def _outbox_row(*, acknowledged_event_id: str | None) -> OutboxRow:
    """Build one staged response row for the classifier."""
    return OutboxRow(
        principal_id="general@@agent:example",
        stage="initial",
        attempted=1,
        acknowledged_event_id=acknowledged_event_id,
    )


@pytest.mark.parametrize(
    ("journal_rows", "outbox_rows", "expected_stage"),
    [
        ((), (), MissingReplyStage.NOT_ADMITTED),
        (
            (_journal_row(state="pending"),),
            (),
            MissingReplyStage.ADMITTED_NEVER_DISPATCHED,
        ),
        (
            (_journal_row(state="settled"),),
            (),
            MissingReplyStage.SETTLED_WITHOUT_REPLY,
        ),
        (
            (_journal_row(state="pending"),),
            (_outbox_row(acknowledged_event_id=None),),
            MissingReplyStage.DISPATCHED_NEVER_SENT,
        ),
        (
            (_journal_row(state="settled"),),
            (_outbox_row(acknowledged_event_id="$reply"),),
            MissingReplyStage.SENT_BUT_UNOBSERVED,
        ),
    ],
    ids=[
        "never-admitted",
        "admitted-never-dispatched",
        "settled-without-reply",
        "dispatched-never-sent",
        "sent-but-unobserved",
    ],
)
def test_classify_missing_reply_names_the_durable_position(
    journal_rows: tuple[JournalRow, ...],
    outbox_rows: tuple[OutboxRow, ...],
    expected_stage: MissingReplyStage,
) -> None:
    """Each durable position is a different failure with a different owner."""
    stage, detail = classify_missing_reply(journal_rows, outbox_rows)

    assert stage is expected_stage
    assert detail


def test_missing_reply_diagnosis_reads_the_production_journal_schema() -> None:
    """The failure report must query the schema MindRoom actually writes."""
    stack = ManagedTuwunelStack()
    try:
        stack.agent_id = "@agent:example"
        principal_id = f"general@{stack.agent_id}"
        store = EventJournalStore.open_sqlite(stack.storage_path / "tracking" / "event_journal.db")

        async def seed() -> None:
            principal = store.principal(principal_id)
            for event_id in ("$stuck", "$staged"):
                await principal.admit(
                    InboundEvent(
                        event_id=event_id,
                        room_id="!room:example",
                        thread_id=None,
                        kind=EventKind.MESSAGE,
                        event_class=EventClass.ACTIONABLE,
                        sender="@user:example",
                        origin_server_ts=1,
                        source={"event_id": event_id},
                    ),
                )
            await principal.enqueue_matrix_delivery(
                delivery_id="$staged",
                stage=DeliveryStage.INITIAL,
                room_id="!room:example",
                thread_id=None,
                payload={"body": "hello"},
            )

        asyncio.run(seed())

        report = stack.diagnose_missing_replies({"$stuck": "op:1", "$staged": "op:2", "$never": "op:3"})

        assert "journal: pending per room !room:example=2" in report
        assert "oldest pending receipt_order=1 event_id=$stuck" in report
        assert f"op:1 ($stuck): {MissingReplyStage.ADMITTED_NEVER_DISPATCHED.value}" in report
        assert f"op:2 ($staged): {MissingReplyStage.DISPATCHED_NEVER_SENT.value}" in report
        assert f"op:3 ($never): {MissingReplyStage.NOT_ADMITTED.value}" in report
        assert principal_id in report
    finally:
        stack.close()


def test_missing_reply_diagnosis_survives_a_run_with_no_journal_yet() -> None:
    """A failure before the runtime writes anything must still report cleanly."""
    stack = ManagedTuwunelStack()
    try:
        report = stack.diagnose_missing_replies({"$one": "op:1"})

        assert "journal: no pending events" in report
        assert MissingReplyStage.NOT_ADMITTED.value in report
        assert not (stack.storage_path / "tracking" / "event_journal.db").exists()
    finally:
        stack.close()


def test_host_load_report_warns_only_about_a_contended_machine() -> None:
    """A run competing with other work must say so before it starts."""
    quiet = HostLoadReport(
        cpu_count=16,
        load_average=(1.0, 1.0, 1.0),
        docker_cpus=4,
        docker_memory_bytes=8 * 1024**3,
        competing_test_processes=0,
    )
    busy = replace(quiet, load_average=(24.0, 30.0, 40.0), competing_test_processes=4)

    assert quiet.contended is False
    assert "WARNING" not in quiet.render()
    assert "docker 4 cpus / 8 GiB" in quiet.render()
    assert busy.contended is True
    assert busy.load_per_cpu == pytest.approx(1.5)
    assert "WARNING" in busy.render()
    assert "4 competing test processes" in busy.render()
    assert busy.as_dict()["host_load_per_cpu"] == pytest.approx(1.5)
    # A machine with spare cores is still contended while tests share it.
    assert replace(quiet, competing_test_processes=1).contended is True


def test_collect_host_load_report_measures_the_real_machine() -> None:
    """The preflight report must read this host rather than guess."""
    report = collect_host_load_report()

    assert report.cpu_count >= 1
    assert len(report.load_average) == 3
    assert report.competing_test_processes >= 0
    assert report.as_dict()["host_cpu_count"] == report.cpu_count


class _WaveRecordingRunner(LiveFuzzRunner):
    """Record how many roots each wave leaves outstanding, then satisfy them."""

    waves: list[int]

    async def _await_replies(self) -> None:
        outstanding = self.oracle.outstanding()
        self.waves.append(len(outstanding))
        for event_id in outstanding:
            self.oracle.response_ids[event_id].add(f"{event_id}-reply")


def _wave_runner(*, root_fanout: int) -> _WaveRecordingRunner:
    """Build a root-fan-out runner with no live dependencies."""
    stack = ManagedTuwunelStack()
    stack.agent_id, stack.router_id = "@agent:example", "@router:example"
    runner = _WaveRecordingRunner(
        stack,
        (cast("LiveMatrixClient", _RecordingDormantClient()),),
        live_scenario_from_seed(1, steps=1, thread_count=25, max_batch_size=1, restart_interval=0),
        reply_timeout=1,
        settle_seconds=0,
        root_fanout=root_fanout,
    )
    runner.waves = []
    return runner


@pytest.mark.asyncio
async def test_send_roots_releases_waves_sized_to_the_single_room_lane() -> None:
    """Roots are setup, so no wait should have to explain the whole fan-out."""
    runner = _wave_runner(root_fanout=DEFAULT_ROOT_FANOUT)
    try:
        await runner._send_roots(range(25))

        assert runner.waves == [8, 8, 8, 1]
        assert len(runner.event_ids) == 25
    finally:
        runner.stack.close()


@pytest.mark.asyncio
async def test_send_roots_keeps_the_simultaneous_fan_out_reachable() -> None:
    """The old all-at-once behaviour stays available behind an explicit flag."""
    runner = _wave_runner(root_fanout=0)
    try:
        await runner._send_roots(range(25))

        assert runner.waves == [25]
    finally:
        runner.stack.close()


def test_model_stream_disconnect_does_not_escape_request_handler() -> None:
    """Chaos may kill the streaming client without failing the model stub."""
    handler = object.__new__(_ModelHandler)
    payload = json.dumps({"messages": [], "stream": True}).encode()
    handler.path = "/v1/chat/completions"
    handler.headers = {"Content-Length": str(len(payload))}
    handler.rfile = io.BytesIO(payload)
    handler.call_ids = iter((1,))
    handler.close_connection = False

    def disconnect(_call_id: int, _content: str) -> None:
        raise BrokenPipeError

    handler._send_stream = disconnect  # type: ignore[method-assign]
    handler.do_POST()

    assert handler.close_connection is True


def test_lifecycle_command_timeout_kills_bounded_process_group() -> None:
    """A hung lifecycle command must time out instead of hanging the campaign."""
    with pytest.raises(TimeoutError, match="command timed out"):
        _run_command(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            timeout_seconds=0.01,
        )


def test_lifecycle_timeout_kills_descendant_after_leader_exits() -> None:
    """An exited leader cannot leave a pipe-owning descendant hanging cleanup."""
    script = "import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])"
    with pytest.raises(TimeoutError, match="command timed out"):
        _run_command(
            sys.executable,
            "-c",
            script,
            timeout_seconds=0.05,
        )


def test_lifecycle_command_interrupt_kills_and_drains_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt must not leave the lifecycle command or descendants alive."""
    killpg_calls: list[tuple[int, signal.Signals]] = []

    class InterruptedProcess:
        pid = 4242
        returncode = None
        communicate_calls = 0

        def communicate(self, *, timeout: float) -> tuple[str, str]:
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise KeyboardInterrupt
            assert timeout == 10
            return "", ""

    process = InterruptedProcess()
    monkeypatch.setattr(live_fuzz.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(live_fuzz.os, "killpg", lambda pid, sig: killpg_calls.append((pid, sig)))

    with pytest.raises(KeyboardInterrupt):
        _run_command("lifecycle-command")

    assert killpg_calls == [(process.pid, signal.SIGKILL)]
    assert process.communicate_calls == 2


@pytest.mark.parametrize(
    ("hard_kill", "expected_signal", "return_code"),
    [
        (False, signal.SIGINT, 0),
        (True, signal.SIGKILL, -signal.SIGKILL),
    ],
)
def test_stop_mindroom_targets_exact_process_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    hard_kill: bool,
    expected_signal: signal.Signals,
    return_code: int,
) -> None:
    """Graceful and hard stops signal the owned child group, then reap it."""

    class FakeProcess:
        pid = 4242

        def __init__(self) -> None:
            self.waited = False

        def poll(self) -> None:
            return None

        def wait(self, *, timeout: float) -> int:
            assert timeout in {10, live_fuzz.MINDROOM_SHUTDOWN_TIMEOUT_SECONDS}
            self.waited = True
            if not hard_kill:
                stack.log_path.write_text(f"{ORDERLY_SHUTDOWN_MARKER}\n", encoding="utf-8")
            return return_code

    process = FakeProcess()
    stack = object.__new__(ManagedTuwunelStack)
    stack._mindroom_process = process
    stack.log_path = tmp_path / "mindroom.log"
    signals: list[tuple[int, signal.Signals | int]] = []

    def killpg(pid: int, sent_signal: signal.Signals | int) -> None:
        signals.append((pid, sent_signal))
        if sent_signal == 0:
            raise ProcessLookupError

    monkeypatch.setattr("scripts.testing.fuzz_live_matrix.os.killpg", killpg)

    stack._stop_mindroom(kill=hard_kill)

    expected_signals: list[tuple[int, signal.Signals | int]] = [(process.pid, expected_signal)]
    if not hard_kill:
        expected_signals.append((process.pid, 0))
    assert signals == expected_signals
    assert process.waited
    assert stack._mindroom_process is None


def test_stop_mindroom_kills_group_after_leader_already_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead leader fails the gate after its owned descendants are killed."""

    class FakeProcess:
        pid = 4242

        def poll(self) -> int:
            return 0

    stack = object.__new__(ManagedTuwunelStack)
    stack._mindroom_process = FakeProcess()
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr("scripts.testing.fuzz_live_matrix.os.killpg", lambda pid, sig: signals.append((pid, sig)))

    with pytest.raises(RuntimeError, match="exited before managed shutdown"):
        stack._stop_mindroom()

    assert signals == [(4242, signal.SIGKILL)]
    assert stack._mindroom_process is None


def test_stop_mindroom_reports_sigkill_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A hung graceful shutdown is cleanup, never passing health evidence."""

    class FakeProcess:
        pid = 4242
        waits = 0

        @staticmethod
        def poll() -> None:
            return None

        def wait(self, *, timeout: float) -> int:
            self.waits += 1
            if timeout == live_fuzz.MINDROOM_SHUTDOWN_TIMEOUT_SECONDS:
                command = "mindroom"
                raise live_fuzz.subprocess.TimeoutExpired(command, timeout)
            assert timeout == 10
            return -signal.SIGKILL

    process = FakeProcess()
    stack = object.__new__(ManagedTuwunelStack)
    stack._mindroom_process = process
    stack.log_path = tmp_path / "mindroom.log"
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(live_fuzz.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    with pytest.raises(TimeoutError, match="required SIGKILL"):
        stack._stop_mindroom()

    assert signals == [
        (process.pid, signal.SIGINT),
        (process.pid, signal.SIGKILL),
    ]
    assert process.waits == 2
    assert stack._mindroom_process is None


def test_stop_mindroom_rejects_nonzero_graceful_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An unexpected exit after SIGINT still fails managed shutdown."""

    class FakeProcess:
        pid = 4242

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def wait(*, timeout: float) -> int:
            assert timeout == live_fuzz.MINDROOM_SHUTDOWN_TIMEOUT_SECONDS
            return 3

    stack = object.__new__(ManagedTuwunelStack)
    stack._mindroom_process = FakeProcess()
    stack.log_path = tmp_path / "mindroom.log"

    def killpg(_pid: int, sent_signal: signal.Signals | int) -> None:
        if sent_signal == 0:
            raise ProcessLookupError

    monkeypatch.setattr(live_fuzz.os, "killpg", killpg)

    with pytest.raises(RuntimeError, match="graceful shutdown exited with status 3"):
        stack._stop_mindroom()

    assert stack._mindroom_process is None


@pytest.mark.parametrize("return_code", [-signal.SIGINT, 128 + signal.SIGINT])
def test_stop_mindroom_accepts_sigint_exit_status_with_fresh_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    return_code: int,
) -> None:
    """Accepted SIGINT status, a fresh marker, and a gone group prove shutdown."""

    class FakeProcess:
        pid = 4242

        @staticmethod
        def poll() -> None:
            return None

        def wait(self, *, timeout: float) -> int:
            assert timeout == live_fuzz.MINDROOM_SHUTDOWN_TIMEOUT_SECONDS
            stack.log_path.write_text(f"{ORDERLY_SHUTDOWN_MARKER}\n", encoding="utf-8")
            return return_code

    stack = object.__new__(ManagedTuwunelStack)
    stack._mindroom_process = FakeProcess()
    stack.log_path = tmp_path / "mindroom.log"

    def killpg(_pid: int, sent_signal: signal.Signals | int) -> None:
        if sent_signal == 0:
            raise ProcessLookupError

    monkeypatch.setattr(live_fuzz.os, "killpg", killpg)

    stack._stop_mindroom()

    assert stack._mindroom_process is None


def test_stop_mindroom_rejects_exit_before_sigint_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An expected-looking exit cannot pass when managed SIGINT never landed."""
    wait_calls: list[float] = []

    class FakeProcess:
        pid = 4242

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def wait(*, timeout: float) -> int:
            wait_calls.append(timeout)
            return 130

    stack = object.__new__(ManagedTuwunelStack)
    stack._mindroom_process = FakeProcess()
    stack.log_path = tmp_path / "mindroom.log"

    def missing_group(_pid: int, _sig: signal.Signals) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(live_fuzz.os, "killpg", missing_group)

    with pytest.raises(RuntimeError, match="exited before managed SIGINT delivery with status 130"):
        stack._stop_mindroom()

    assert wait_calls == [10]
    assert stack._mindroom_process is None


def test_stop_mindroom_rejects_exit_before_sigkill_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spontaneous exit cannot count as a successful hard-kill injection."""
    wait_calls: list[float] = []

    class FakeProcess:
        pid = 4242

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def wait(*, timeout: float) -> int:
            wait_calls.append(timeout)
            return 7

    stack = object.__new__(ManagedTuwunelStack)
    stack._mindroom_process = FakeProcess()

    def missing_group(_pid: int, _sig: signal.Signals) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(live_fuzz.os, "killpg", missing_group)

    with pytest.raises(RuntimeError, match="exited before managed SIGKILL delivery with status 7"):
        stack._stop_mindroom(kill=True)

    assert wait_calls == [10]
    assert stack._mindroom_process is None


def test_stop_mindroom_rejects_surviving_group_after_graceful_leader_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A leader's SIGINT status cannot hide a surviving same-group child."""
    group_alive = True
    signals: list[signal.Signals | int] = []

    class FakeProcess:
        pid = 4242

        @staticmethod
        def poll() -> None:
            return None

        def wait(self, *, timeout: float) -> int:
            assert timeout == live_fuzz.MINDROOM_SHUTDOWN_TIMEOUT_SECONDS
            stack.log_path.write_text(f"{ORDERLY_SHUTDOWN_MARKER}\n", encoding="utf-8")
            return 128 + signal.SIGINT

    def killpg(_pid: int, sent_signal: signal.Signals | int) -> None:
        nonlocal group_alive
        signals.append(sent_signal)
        if sent_signal == 0:
            if group_alive:
                return
            raise ProcessLookupError
        if sent_signal == signal.SIGKILL:
            group_alive = False

    stack = object.__new__(ManagedTuwunelStack)
    stack._mindroom_process = FakeProcess()
    stack.log_path = tmp_path / "mindroom.log"
    monkeypatch.setattr(live_fuzz.os, "killpg", killpg)
    monkeypatch.setattr(live_fuzz, "_PROCESS_GROUP_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(live_fuzz.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="process group survived graceful SIGINT and required SIGKILL"):
        stack._stop_mindroom()

    assert signals == [
        signal.SIGINT,
        0,
        signal.SIGKILL,
        0,
    ]
    assert stack._mindroom_process is None


@pytest.mark.parametrize("return_code", [7, 128 + signal.SIGKILL])
def test_stop_mindroom_rejects_non_sigkill_wait_status(
    monkeypatch: pytest.MonkeyPatch,
    *,
    return_code: int,
) -> None:
    """Only direct-Popen SIGKILL status proves the managed hard-kill."""

    class FakeProcess:
        pid = 4242

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def wait(*, timeout: float) -> int:
            assert timeout == 10
            return return_code

    stack = object.__new__(ManagedTuwunelStack)
    stack._mindroom_process = FakeProcess()
    monkeypatch.setattr(live_fuzz.os, "killpg", lambda _pid, _sig: None)

    with pytest.raises(RuntimeError, match=f"hard kill exited with status {return_code}"):
        stack._stop_mindroom(kill=True)

    assert stack._mindroom_process is None


def test_final_runtime_health_rejects_preexited_process(tmp_path: Path) -> None:
    """Canonical state cannot turn an already-dead runtime into a PASS."""

    class FakeProcess:
        @staticmethod
        def poll() -> int:
            return 7

    stack = object.__new__(ManagedTuwunelStack)
    stack._mindroom_process = FakeProcess()
    stack.log_path = tmp_path / "mindroom.log"
    stack.log_path.write_text("fatal runtime exit\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="exited before final audit with status 7"):
        stack.assert_mindroom_running()


def test_startup_maintenance_wait_uses_only_current_process_generation(tmp_path: Path) -> None:
    """A stale completion marker from the prior process cannot release restart audit."""
    phases = {
        "startup_maintenance.rooms_and_memberships",
        "startup_maintenance.runtime_support",
        "startup_maintenance.stale_stream_recovery.initial",
        "startup_maintenance.stale_stream_recovery.joined_room_delta",
    }
    assert phases == live_fuzz._STARTUP_MAINTENANCE_PHASES

    class FakeProcess:
        @staticmethod
        def poll() -> None:
            return None

    log_path = tmp_path / "mindroom.log"
    log_path.write_text(
        "startup_phase_finished phase=startup_maintenance.runtime_support status=failed\n",
        encoding="utf-8",
    )
    current_generation_offset = log_path.stat().st_size
    with log_path.open("a", encoding="utf-8") as log:
        for phase in sorted(phases):
            log.write(f"startup_phase_finished phase={phase} status=completed\n")

    stack = object.__new__(ManagedTuwunelStack)
    stack.log_path = log_path
    stack._mindroom_start_log_offset = current_generation_offset
    stack._mindroom_process = FakeProcess()

    stack.wait_for_startup_maintenance(timeout_seconds=0.1)


def test_startup_maintenance_wait_rejects_failed_current_phase(tmp_path: Path) -> None:
    """A terminal failed phase cannot be mistaken for completed maintenance."""

    class FakeProcess:
        @staticmethod
        def poll() -> None:
            return None

    log_path = tmp_path / "mindroom.log"
    log_path.write_text(
        "startup_phase_finished phase=startup_maintenance.stale_stream_recovery.initial status=failed\n",
        encoding="utf-8",
    )
    stack = object.__new__(ManagedTuwunelStack)
    stack.log_path = log_path
    stack._mindroom_start_log_offset = 0
    stack._mindroom_process = FakeProcess()

    with pytest.raises(AssertionError, match="did not complete cleanly"):
        stack.wait_for_startup_maintenance(timeout_seconds=0.1)


def test_startup_maintenance_wait_parses_colored_structlog_fields(tmp_path: Path) -> None:
    """ANSI styling around structlog keys and values cannot hide completion."""

    class FakeProcess:
        @staticmethod
        def poll() -> None:
            return None

    log_path = tmp_path / "mindroom.log"
    log_path.write_text(
        "".join(
            "\x1b[32mstartup_phase_finished\x1b[0m "
            f"\x1b[36mphase\x1b[0m=\x1b[35m{phase}\x1b[0m "
            "\x1b[36mstatus\x1b[0m=\x1b[35mcompleted\x1b[0m\n"
            for phase in sorted(live_fuzz._STARTUP_MAINTENANCE_PHASES)
        ),
        encoding="utf-8",
    )
    stack = object.__new__(ManagedTuwunelStack)
    stack.log_path = log_path
    stack._mindroom_start_log_offset = 0
    stack._mindroom_process = FakeProcess()

    stack.wait_for_startup_maintenance(timeout_seconds=0.1)


def test_startup_maintenance_wait_has_bounded_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing generation markers fail at the caller's deadline."""

    class FakeProcess:
        @staticmethod
        def poll() -> None:
            return None

    clock = 0.0

    def monotonic() -> float:
        nonlocal clock
        now = clock
        clock += 1.0
        return now

    log_path = tmp_path / "mindroom.log"
    log_path.write_text("", encoding="utf-8")
    stack = object.__new__(ManagedTuwunelStack)
    stack.log_path = log_path
    stack._mindroom_start_log_offset = 0
    stack._mindroom_process = FakeProcess()
    monkeypatch.setattr(live_fuzz.time, "monotonic", monotonic)
    monkeypatch.setattr(live_fuzz.time, "sleep", lambda _seconds: None)

    with pytest.raises(TimeoutError, match="startup maintenance phases"):
        stack.wait_for_startup_maintenance(timeout_seconds=2.0)


def test_saturation_scenario_matches_original_two_phase_workload() -> None:
    """The regression profile must preserve the old hot-then-parallel ordering."""
    scenario = saturation_scenario()

    assert scenario.thread_count == 13
    assert len(scenario.batches) == 108
    assert all(len(batch) == 1 and batch[0].thread == 0 for batch in scenario.batches[:100])
    assert all([operation.thread for operation in batch] == list(range(1, 13)) for batch in scenario.batches[100:])


@pytest.mark.parametrize(
    ("scenario", "error"),
    [
        (
            LiveFuzzScenario(thread_count=1, batches=(), profile="saturation"),
            "at least one parallel thread",
        ),
        (
            LiveFuzzScenario(
                thread_count=3,
                profile="saturation",
                batches=(
                    (
                        LiveOperation(
                            0,
                            LiveOperationKind.THREAD_MESSAGE,
                            1,
                            "response:root:1",
                        ),
                    ),
                ),
            ),
            "every expected phase thread",
        ),
        (
            LiveFuzzScenario(
                thread_count=2,
                profile="saturation",
                batches=((LiveOperation(0, LiveOperationKind.REACTION, 0, "root:0"),),),
            ),
            "only thread-message operations",
        ),
        (
            LiveFuzzScenario(
                thread_count=2,
                profile="saturation",
                batches=((LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "root:0"),),),
            ),
            "must target 'response:root:0'",
        ),
    ],
)
def test_saturation_validation_rejects_shapes_the_driver_cannot_replay(
    scenario: LiveFuzzScenario,
    error: str,
) -> None:
    """Validated saturation traces must match every operation the driver sends."""
    with pytest.raises(ValueError, match=error):
        scenario.validate()


def test_generators_never_edit_one_source_twice_per_batch() -> None:
    """Codex #6: no seed may place two edits of one source in a concurrent batch.

    The default fuzz generator previously did so at seed=1, batch=3, target op:8,
    leaving the surviving revision at the mercy of coroutine completion order.
    Both generators must now be collision-free across seeds.
    """
    scenarios = [live_scenario_from_seed(seed, steps=200, restart_interval=100) for seed in range(8)] + [
        chaos_scenario_from_seed(seed, steps=300) for seed in range(8)
    ]
    for scenario in scenarios:
        for batch in scenario.batches:
            edited = [operation.target for operation in batch if operation.kind is LiveOperationKind.EDIT]
            assert len(edited) == len(set(edited))


@pytest.mark.asyncio
async def test_restart_observation_attributes_auto_resumed_response_to_original_source(
    seeded_restart_observation_stack: tuple[ManagedTuwunelStack, list[float]],
) -> None:
    """A completed auto-resume chain is the final response for its interrupted source."""
    stack, stop_calls = seeded_restart_observation_stack
    interrupted = _restart_response(
        "$interrupted",
        stack.agent_id,
        "$fresh",
        body=f"partial\n\n{RESTART_INTERRUPTED_RESPONSE_NOTE}",
    )
    relay = {
        "event_id": "$relay",
        "sender": stack.router_id,
        "type": "m.room.message",
        "content": {
            SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
            "body": f"@agent {AUTO_RESUME_MESSAGE}",
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$fresh",
                "m.in_reply_to": {"event_id": "$interrupted"},
            },
        },
    }
    recovered = _restart_response("$recovered", stack.agent_id, "$relay")
    recovered["content"]["m.relates_to"]["event_id"] = "$fresh"

    observation = await _collect_seeded_restart_observation(
        stack,
        log=_RESTART_OBSERVATION_LOG,
        events=(interrupted, relay, recovered),
    )

    assert stop_calls == [0.05]
    assert observation.fresh_agent_output_count == 1
    assert observation.fresh_response_complete
    assert observation.recovered_generation_response_observed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "duplicate_original",
        "wrong_target",
        "wrong_sender",
        "wrong_root",
        "wrong_room",
        "uninterrupted",
        "duplicate_recovered",
    ],
)
async def test_restart_observation_rejects_broken_resume_chain(
    seeded_restart_observation_stack: tuple[ManagedTuwunelStack, list[float]],
    fault: str,
) -> None:
    """A relay answer cannot erase missing provenance or duplicate direct originals."""
    stack, _stop_calls = seeded_restart_observation_stack
    interrupted = _restart_response(
        "$interrupted",
        stack.agent_id,
        "$fresh",
        body=f"partial\n\n{RESTART_INTERRUPTED_RESPONSE_NOTE}",
    )
    relay = _restart_response("$relay", stack.router_id, "$interrupted", body=AUTO_RESUME_MESSAGE)
    relay["content"][SOURCE_KIND_KEY] = TRUSTED_INTERNAL_RELAY_SOURCE_KIND
    relay["content"]["m.relates_to"]["event_id"] = "$fresh"
    recovered = _restart_response("$recovered", stack.agent_id, "$relay")
    recovered["content"]["m.relates_to"]["event_id"] = "$fresh"
    events = [interrupted, relay, recovered]
    if fault == "missing":
        events.remove(interrupted)
    elif fault == "duplicate_original":
        events.append({**interrupted, "event_id": "$duplicate"})
    elif fault == "wrong_target":
        relay["content"]["m.relates_to"]["m.in_reply_to"]["event_id"] = "$unrelated"
    elif fault == "wrong_sender":
        interrupted["sender"] = "@outsider:example"
    elif fault == "wrong_root":
        recovered["content"]["m.relates_to"]["event_id"] = "$unrelated"
    elif fault == "wrong_room":
        interrupted["room_id"] = "!unrelated:example"
    elif fault == "uninterrupted":
        interrupted["content"]["body"] = "original completed END call=0"
    else:
        events.append({**recovered, "event_id": "$duplicate"})
    observation = await _collect_seeded_restart_observation(stack, log=_RESTART_OBSERVATION_LOG, events=tuple(events))
    assert not observation.fresh_response_complete


@pytest.mark.asyncio
async def test_exact_reply_oracle_flags_reply_to_unrelated_router_traffic() -> None:
    """An agent reply to ordinary router traffic is unexpected, not exempt."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(
        client,
        "@agent:example",
        internal_relay_senders=("@router:example",),
    )
    try:
        # A router greeting is not an auto-resume relay: no AUTO_RESUME_MESSAGE
        # body, no threaded reply relation.
        oracle._ingest_event(
            {
                "event_id": "$greeting",
                "sender": "@router:example",
                "type": "m.room.message",
                "content": {"body": "not a resume"},
            },
        )
        assert "$greeting" not in oracle.internal_source_ids
        oracle._ingest_event(
            {
                "event_id": "$response",
                "sender": "@agent:example",
                "type": "m.room.message",
                "content": {
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$root",
                        "m.in_reply_to": {"event_id": "$greeting"},
                    },
                },
            },
        )
        with pytest.raises(AssertionError, match="unexpected"):
            oracle._assert_no_wrong_replies()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_exact_reply_oracle_rejects_router_quote_of_resume_message() -> None:
    """Quoted resume text without the trusted source kind is ordinary traffic."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(
        client,
        "@agent:example",
        internal_relay_senders=("@router:example",),
    )
    try:
        oracle.expect("op:1", "$source")
        interrupted = _agent_reply_event(
            "$source",
            "$interrupted",
            f"LIVE-FUZZ call=1 {RESTART_INTERRUPTED_RESPONSE_NOTE}",
        )
        interrupted["content"]["m.relates_to"]["event_id"] = "$root"
        oracle._ingest_event(interrupted)
        oracle._ingest_event(
            _threaded_reply_event(
                sender="@router:example",
                event_id="$quoted-resume",
                thread_root="$root",
                in_reply_to="$interrupted",
                body=f"ordinary quote: {AUTO_RESUME_MESSAGE}",
            ),
        )
        oracle._ingest_event(
            _threaded_reply_event(
                sender="@agent:example",
                event_id="$extra",
                thread_root="$root",
                in_reply_to="$quoted-resume",
                body="LIVE-FUZZ call=2 END call=2",
            ),
        )

        assert "$quoted-resume" not in oracle.internal_source_ids
        with pytest.raises(AssertionError, match="unexpected"):
            oracle._assert_no_wrong_replies()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_exact_reply_oracle_rejects_resume_relay_after_completed_reply() -> None:
    """A resume-shaped relay is internal only when its target is interrupted."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(
        client,
        "@agent:example",
        internal_relay_senders=("@router:example",),
    )
    try:
        oracle.expect("op:1", "$source")
        completed = _agent_reply_event("$source", "$completed", "LIVE-FUZZ call=1 END call=1")
        completed["content"]["m.relates_to"]["event_id"] = "$root"
        oracle._ingest_event(completed)
        oracle._ingest_event(
            _resume_relay_event(
                event_id="$false-relay",
                thread_root="$root",
                in_reply_to="$completed",
            ),
        )
        oracle._ingest_event(
            _threaded_reply_event(
                sender="@agent:example",
                event_id="$extra",
                thread_root="$root",
                in_reply_to="$false-relay",
                body="LIVE-FUZZ call=2 END call=2",
            ),
        )

        assert "$false-relay" not in oracle.internal_source_ids
        with pytest.raises(AssertionError, match="unexpected"):
            oracle._assert_no_wrong_replies()
    finally:
        await client.close()


def test_chaos_scenario_is_deterministic_and_json_replayable() -> None:
    """Chaos traces must be seed-stable and survive JSON round-tripping."""
    tuning = ChaosTuning(thread_count=10, client_count=3, room_count=2, lifecycle_interval=30)
    scenario = chaos_scenario_from_seed(7, steps=150, tuning=tuning)

    assert scenario == chaos_scenario_from_seed(7, steps=150, tuning=tuning)
    assert LiveFuzzScenario.from_json(scenario.to_json()) == scenario
    assert scenario.profile == "chaos"
    assert scenario.client_count == 3
    assert scenario.room_count == 2
    assert {operation.client for batch in scenario.batches for operation in batch} == {0, 1, 2}


def test_chaos_allows_same_thread_races_only_across_distinct_clients() -> None:
    """Two senders may race one thread; one sender may not race itself."""
    same_client = LiveFuzzScenario(
        thread_count=1,
        client_count=2,
        profile="chaos",
        batches=(
            (
                LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "root:0", 1),
                LiveOperation(1, LiveOperationKind.THREAD_MESSAGE, 0, "root:0", 1),
            ),
        ),
    )
    with pytest.raises(ValueError, match="same-thread messages"):
        same_client.validate()

    distinct_clients = LiveFuzzScenario(
        thread_count=1,
        client_count=2,
        profile="chaos",
        batches=(
            (
                LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "root:0", 0),
                LiveOperation(1, LiveOperationKind.THREAD_MESSAGE, 0, "root:0", 1),
            ),
        ),
    )
    distinct_clients.validate()


def test_chaos_validation_rejects_lifecycle_inside_concurrent_batch() -> None:
    """Lifecycle disruptions cannot share a batch with Matrix mutations."""
    scenario = LiveFuzzScenario(
        thread_count=1,
        profile="chaos",
        batches=(
            (
                LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "root:0"),
                LiveOperation(1, LiveOperationKind.CHECKPOINT, 0, None),
            ),
        ),
    )
    with pytest.raises(ValueError, match="singleton"):
        scenario.validate()


@pytest.mark.parametrize(
    ("profile", "kind"),
    [
        ("fuzz", LiveOperationKind.STOP_MINDROOM),
        ("fuzz", LiveOperationKind.START_MINDROOM),
        ("saturation", LiveOperationKind.RESTART_MINDROOM),
    ],
)
def test_non_chaos_validation_rejects_unsupported_lifecycle(
    profile: str,
    kind: LiveOperationKind,
) -> None:
    """Validated traces cannot reach a profile driver that lacks the lifecycle."""
    scenario = LiveFuzzScenario(
        thread_count=1,
        profile=profile,
        batches=((LiveOperation(0, kind, 0, None),),),
    )

    with pytest.raises(ValueError, match=f"not supported by the {profile} profile"):
        scenario.validate()


def test_chaos_validation_tracks_mindroom_lifecycle_state() -> None:
    """Starting a running MindRoom or ending stopped must be rejected."""
    with pytest.raises(ValueError, match="already running"):
        LiveFuzzScenario(
            thread_count=1,
            profile="chaos",
            batches=((LiveOperation(0, LiveOperationKind.START_MINDROOM, 0, None),),),
        ).validate()
    with pytest.raises(ValueError, match="leave MindRoom running"):
        LiveFuzzScenario(
            thread_count=1,
            profile="chaos",
            batches=((LiveOperation(0, LiveOperationKind.STOP_MINDROOM, 0, None),),),
        ).validate()


def test_chaos_validation_rejects_unsettled_response_target_during_outage() -> None:
    """Outage traffic may only reference agent replies that settled before the stop."""
    unsettled = LiveFuzzScenario(
        thread_count=1,
        profile="chaos",
        batches=(
            (LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "root:0"),),
            (LiveOperation(1, LiveOperationKind.STOP_MINDROOM, 0, None),),
            (LiveOperation(2, LiveOperationKind.REACTION, 0, "response:op:0"),),
            (LiveOperation(3, LiveOperationKind.START_MINDROOM, 0, None),),
        ),
    )
    with pytest.raises(ValueError, match="while MindRoom is down"):
        unsettled.validate()

    settled = LiveFuzzScenario(
        thread_count=1,
        profile="chaos",
        batches=(
            (LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "root:0"),),
            (LiveOperation(1, LiveOperationKind.CHECKPOINT, 0, None),),
            (LiveOperation(2, LiveOperationKind.STOP_MINDROOM, 0, None),),
            (LiveOperation(3, LiveOperationKind.REACTION, 0, "response:op:0"),),
            (LiveOperation(4, LiveOperationKind.START_MINDROOM, 0, None),),
        ),
    )
    settled.validate()


def test_chaos_validation_pins_mutations_to_their_author() -> None:
    """Edits, redactions, and retries must come from the original sender."""
    foreign_edit = LiveFuzzScenario(
        thread_count=1,
        client_count=2,
        profile="chaos",
        batches=((LiveOperation(0, LiveOperationKind.EDIT, 0, "root:0", 1),),),
    )
    with pytest.raises(ValueError, match="must come from its author"):
        foreign_edit.validate()

    response_edit = LiveFuzzScenario(
        thread_count=1,
        profile="chaos",
        batches=((LiveOperation(0, LiveOperationKind.EDIT, 0, "response:root:0"),),),
    )
    with pytest.raises(ValueError, match="fuzz-authored"):
        response_edit.validate()


def test_chaos_validation_rejects_two_edits_of_one_source_in_a_batch() -> None:
    """Codex #6: concurrent edits of one source have no deterministic winner.

    Two same-batch ``m.replace`` events on the same target land in
    nondeterministic Matrix order, so the surviving revision is unknowable and
    the final-body audit would flap. Generation excludes such pairs; the
    validator rejects them defensively for hand-written and replayed traces.
    """
    scenario = LiveFuzzScenario(
        thread_count=1,
        profile="chaos",
        batches=(
            (LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "root:0"),),
            (
                LiveOperation(1, LiveOperationKind.EDIT, 0, "op:0"),
                LiveOperation(2, LiveOperationKind.EDIT, 0, "op:0"),
            ),
            (LiveOperation(3, LiveOperationKind.CHECKPOINT, 0, None),),
        ),
    )
    with pytest.raises(ValueError, match="edited at most once per batch"):
        scenario.validate()


def test_chaos_validation_rejects_duplicate_redactions_in_one_batch() -> None:
    """One target cannot have two nondeterministically winning redaction IDs."""
    scenario = LiveFuzzScenario(
        thread_count=1,
        profile="chaos",
        batches=(
            (
                LiveOperation(0, LiveOperationKind.REDACTION, 0, "root:0"),
                LiveOperation(1, LiveOperationKind.REDACTION, 0, "root:0"),
            ),
        ),
    )
    with pytest.raises(ValueError, match="redacted at most once"):
        scenario.validate()


def test_generators_never_redact_one_target_twice_per_batch() -> None:
    """Generated replay traces preserve exact redaction provenance."""
    scenarios = [live_scenario_from_seed(seed, steps=100, thread_count=6) for seed in range(8)]
    scenarios.extend(
        chaos_scenario_from_seed(
            seed,
            steps=100,
            tuning=ChaosTuning(thread_count=6, client_count=3, room_count=2),
        )
        for seed in range(8)
    )
    for scenario in scenarios:
        for batch in scenario.batches:
            redacted = [operation.target for operation in batch if operation.kind is LiveOperationKind.REDACTION]
            assert len(redacted) == len(set(redacted))


def test_chaos_validation_requires_checkpoint_before_cold_restart() -> None:
    """Cold restarts drop the sync token, so every prior reply must be settled."""
    scenario = LiveFuzzScenario(
        thread_count=1,
        profile="chaos",
        batches=(
            (LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "root:0"),),
            (LiveOperation(1, LiveOperationKind.COLD_RESTART_MINDROOM, 0, None),),
        ),
    )
    with pytest.raises(ValueError, match="follow a checkpoint"):
        scenario.validate()


def test_legacy_trace_without_client_fields_still_loads() -> None:
    """Traces recorded before the chaos profile keep replaying unchanged."""
    legacy = live_scenario_from_seed(3, steps=40, thread_count=4, restart_interval=0)
    payload = json.loads(legacy.to_json())
    payload.pop("client_count")
    for batch in payload["batches"]:
        for operation in batch:
            operation.pop("client")
    stripped = json.dumps(payload)
    assert '"client"' not in stripped
    assert '"client_count"' not in stripped
    scenario = LiveFuzzScenario.from_json(stripped)

    assert scenario == legacy
    assert scenario.client_count == 1
    assert scenario.room_count == 1


@pytest.mark.asyncio
async def test_exact_reply_oracle_allows_missing_reply_only_for_optional_sources() -> None:
    """A source redacted before settling may have zero replies, never two."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    try:
        oracle.expect("root:0", "$kept")
        oracle.expect("op:1", "$redacted")
        oracle.mark_source_optional("$redacted")
        assert oracle.unsettled_required_sources() == ["$kept"]

        for event_id in ("$reply-one", "$reply-two"):
            oracle._ingest_event(
                {
                    "event_id": event_id,
                    "sender": "@agent:example",
                    "type": "m.room.message",
                    "content": {
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": "$redacted",
                            "m.in_reply_to": {"event_id": "$redacted"},
                        },
                    },
                },
            )
        with pytest.raises(AssertionError, match="duplicates"):
            oracle._assert_no_wrong_replies()
    finally:
        await client.close()


def _agent_reply_event(source: str, event_id: str, body: str) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "sender": "@agent:example",
        "type": "m.room.message",
        "origin_server_ts": 100,
        "content": {
            "body": body,
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": source,
                "m.in_reply_to": {"event_id": source},
            },
        },
    }


def _threaded_reply_event(
    *,
    sender: str,
    event_id: str,
    thread_root: str,
    in_reply_to: str,
    body: str,
) -> dict[str, Any]:
    """Build a threaded reply with explicit thread root and reply target."""
    return {
        "event_id": event_id,
        "sender": sender,
        "type": "m.room.message",
        "origin_server_ts": 100,
        "content": {
            "body": body,
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": thread_root,
                "m.in_reply_to": {"event_id": in_reply_to},
            },
        },
    }


def _resume_relay_event(
    *,
    event_id: str,
    thread_root: str,
    in_reply_to: str,
) -> dict[str, Any]:
    """Build the exact structured router relay production emits."""
    event = _threaded_reply_event(
        sender="@router:example",
        event_id=event_id,
        thread_root=thread_root,
        in_reply_to=in_reply_to,
        body=f"@agent {AUTO_RESUME_MESSAGE}",
    )
    event["content"][SOURCE_KIND_KEY] = TRUSTED_INTERNAL_RELAY_SOURCE_KIND
    return event


@pytest.mark.asyncio
async def test_final_state_auditor_flags_incomplete_final_bodies() -> None:
    """An interrupted terminal note must fail the completed-stream audit."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=lambda call_id: f"LIVE-FUZZ call={call_id} END call={call_id}",
    )
    try:
        oracle.expect("root:0", "$source")
        events = {"$reply": _agent_reply_event("$source", "$reply", "[Response interrupted by service restart]")}
        replies = auditor._canonical_agent_replies(events)
        with pytest.raises(AssertionError, match="non-canonical body"):
            auditor._assert_final_bodies_complete(events, replies)

        events["$edit"] = {
            "event_id": "$edit",
            "sender": "@agent:example",
            "type": "m.room.message",
            "origin_server_ts": 200,
            "content": {
                "body": "* LIVE-FUZZ call=4 END call=4",
                "m.new_content": {"body": "LIVE-FUZZ call=4 END call=4"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
            },
        }
        assert auditor._assert_final_bodies_complete(events, replies) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_final_state_auditor_validates_visible_optional_source_replies() -> None:
    """Optional sources allow zero replies but must still validate any visible reply."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=lambda call_id: f"LIVE-FUZZ call={call_id} END call={call_id}",
    )
    try:
        oracle.expect("root:0", "$optional")
        oracle.mark_source_optional("$optional")

        # Zero replies is valid for an optional source (redaction race).
        assert auditor._assert_final_bodies_complete({}, {}) == 0

        # A still-visible non-terminal reply (frozen placeholder) must fail even
        # though the source is optional.
        partial = {"$reply": _agent_reply_event("$optional", "$reply", "Thinking...")}
        replies = auditor._canonical_agent_replies(partial)
        with pytest.raises(AssertionError, match="non-canonical body"):
            auditor._assert_final_bodies_complete(partial, replies)

        # A completed reply to the optional source passes.
        done = {"$reply": _agent_reply_event("$optional", "$reply", "LIVE-FUZZ call=7 END call=7")}
        done_replies = auditor._canonical_agent_replies(done)
        assert auditor._assert_final_bodies_complete(done, done_replies) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_final_state_auditor_enforces_redaction_and_reaction_semantics() -> None:
    """Redacted events must be pruned and live reactions must keep their key."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=lambda call_id: f"LIVE-FUZZ call={call_id} END call={call_id}",
    )
    msg_content = {"body": "hello", "msgtype": "m.text"}
    react_content = {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$msg", "key": "fuzz-9"}}
    records = [
        _SentRecord("$msg", "!room:example", "m.room.message", content=msg_content),
        _SentRecord("$gone", "!room:example", "m.room.message", content={"body": "bye", "msgtype": "m.text"}),
        _SentRecord("$react", "!room:example", "m.reaction", reaction_key="fuzz-9", content=react_content),
    ]
    try:
        events = {
            "$msg": {
                "event_id": "$msg",
                "type": "m.room.message",
                "content": dict(msg_content),
                "_audit_room_id": "!room:example",
            },
            "$gone": {
                "event_id": "$gone",
                "type": "m.room.message",
                "content": {},
                "_audit_room_id": "!room:example",
            },
            "$react": {
                "event_id": "$react",
                "type": "m.reaction",
                "content": dict(react_content),
                "_audit_room_id": "!room:example",
            },
        }
        auditor._assert_sent_events_canonical(events, records, {"$gone"})

        with pytest.raises(AssertionError, match="kept visible content"):
            auditor._assert_sent_events_canonical(events, records, {"$gone", "$msg"})

        retained_msgtype = {
            **events,
            "$gone": {**events["$gone"], "content": {"msgtype": "m.text"}},
        }
        with pytest.raises(AssertionError, match="kept visible content"):
            auditor._assert_sent_events_canonical(retained_msgtype, records, {"$gone"})

        with pytest.raises(AssertionError, match="missing from /messages"):
            auditor._assert_sent_events_canonical({}, records, set())
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_final_state_auditor_requires_exact_redaction_provenance() -> None:
    """A redacted shell and its redaction event must point to each other exactly."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=lambda call_id: f"LIVE-FUZZ call={call_id} END call={call_id}",
    )
    records = [
        _SentRecord("$target", "!room:example", "m.room.message", content={"body": "gone"}),
        _SentRecord(
            "$redaction",
            "!room:example",
            "m.room.redaction",
            redacts="$target",
            content={"reason": "live cache fuzz"},
        ),
    ]
    events = {
        "$target": {
            "event_id": "$target",
            "type": "m.room.message",
            "content": {},
            "unsigned": {"redacted_because": {"event_id": "$redaction"}},
            "_audit_room_id": "!room:example",
        },
        "$redaction": {
            "event_id": "$redaction",
            "type": "m.room.redaction",
            "content": {"reason": "live cache fuzz", "redacts": "$target"},
            "_audit_room_id": "!room:example",
        },
    }
    try:
        auditor._assert_sent_events_canonical(events, records, {"$target": "$redaction"})

        without_shell = {event_id: event for event_id, event in events.items() if event_id != "$target"}
        with pytest.raises(AssertionError, match="missing from /messages"):
            auditor._assert_sent_events_canonical(without_shell, records, {"$target": "$redaction"})

        wrong_envelope = {**events, "$target": {**events["$target"], "unsigned": {}}}
        with pytest.raises(AssertionError, match="points to"):
            auditor._assert_sent_events_canonical(wrong_envelope, records, {"$target": "$redaction"})

        wrong_redacts = {
            **events,
            "$redaction": {
                **events["$redaction"],
                "content": {**events["$redaction"]["content"], "redacts": "$other"},
            },
        }
        with pytest.raises(AssertionError, match="redacts"):
            auditor._assert_sent_events_canonical(wrong_redacts, records, {"$target": "$redaction"})
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_final_state_auditor_rejects_diverged_content() -> None:
    """The verbatim audit catches any content that diverges from the sent payload."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=lambda call_id: f"LIVE-FUZZ call={call_id} END call={call_id}",
    )
    msg_content = {"body": "LIVE-FUZZ call=1 END call=1", "msgtype": "m.text"}
    react_content = {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$msg", "key": "fuzz-9"}}
    records = [
        _SentRecord("$msg", "!room:example", "m.room.message", content=msg_content),
        _SentRecord("$react", "!room:example", "m.reaction", reaction_key="fuzz-9", content=react_content),
    ]
    try:
        # A corrupted paginated body must fail even though it is still a string.
        with pytest.raises(AssertionError, match="content diverged"):
            auditor._assert_sent_events_canonical(
                {"$msg": {"event_id": "$msg", "type": "m.room.message", "content": {"body": "CORRUPTED"}}},
                [records[0]],
                set(),
            )

        # A dropped marker / changed msgtype must fail.
        with pytest.raises(AssertionError, match="content diverged"):
            auditor._assert_sent_events_canonical(
                {"$msg": {"event_id": "$msg", "type": "m.room.message", "content": {"body": msg_content["body"]}}},
                [records[0]],
                set(),
            )

        # A retargeted reaction relation must fail.
        wrong_react = {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$other", "key": "fuzz-9"}}
        with pytest.raises(AssertionError, match="content diverged"):
            auditor._assert_sent_events_canonical(
                {"$react": {"event_id": "$react", "type": "m.reaction", "content": wrong_react}},
                [records[1]],
                set(),
            )

        # The exact payload round-trips cleanly.
        auditor._assert_sent_events_canonical(
            {
                "$msg": {
                    "event_id": "$msg",
                    "type": "m.room.message",
                    "content": dict(msg_content),
                    "_audit_room_id": "!room:example",
                },
                "$react": {
                    "event_id": "$react",
                    "type": "m.reaction",
                    "content": dict(react_content),
                    "_audit_room_id": "!room:example",
                },
            },
            records,
            set(),
        )
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "wrong_value", "message"),
    [
        ("_audit_room_id", "!wrong:example", "appeared in room"),
        ("sender", "@mallory:example", "has sender"),
        ("type", "m.reaction", "has type"),
    ],
)
async def test_final_state_auditor_rejects_wrong_event_provenance(
    field: str,
    wrong_value: str,
    message: str,
) -> None:
    """Final state must preserve the exact room, author, and event type."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=lambda call_id: f"LIVE-FUZZ call={call_id} END call={call_id}",
    )
    record = _SentRecord(
        "$msg",
        "!room:example",
        "m.room.message",
        sender="@alice:example",
        content={"body": "hello", "msgtype": "m.text"},
    )
    event = {
        "event_id": "$msg",
        "type": "m.room.message",
        "sender": "@alice:example",
        "content": {"body": "hello", "msgtype": "m.text"},
        "_audit_room_id": "!room:example",
        field: wrong_value,
    }
    try:
        with pytest.raises(AssertionError, match=message):
            auditor._assert_sent_events_canonical({"$msg": event}, [record], set())
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_field", ["room", "root"])
async def test_final_state_auditor_rejects_reply_outside_source_thread(
    wrong_field: str,
) -> None:
    """A direct reply must share both room and canonical root with its source."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=lambda call_id: f"LIVE-FUZZ call={call_id} END call={call_id}",
    )
    oracle.expect("root:0", "$source")
    source_record = _SentRecord(
        "$source",
        "!room:example",
        "m.room.message",
        content={"body": "source"},
    )
    reply = _agent_reply_event("$source", "$reply", "LIVE-FUZZ call=1 END call=1")
    reply["_audit_room_id"] = "!other:example" if wrong_field == "room" else "!room:example"
    if wrong_field == "root":
        reply["content"]["m.relates_to"]["event_id"] = "$wrong-root"
    events = {
        "$source": {
            "event_id": "$source",
            "type": "m.room.message",
            "content": {"body": "source"},
            "_audit_room_id": "!room:example",
        },
        "$reply": reply,
    }
    try:
        with pytest.raises(AssertionError, match="reply provenance"):
            auditor._canonical_agent_replies(events, sent_records=[source_record])
    finally:
        await client.close()


def test_body_call_id_parses_only_canonical_prefixes() -> None:
    """Call IDs come only from exact stub-format bodies."""
    assert _body_call_id("LIVE-FUZZ call=17 segment-000 END call=17") == 17
    assert _body_call_id("[Response interrupted by service restart]") is None
    assert _body_call_id("LIVE-FUZZ call=x END") is None


@pytest.mark.asyncio
async def test_all_reply_body_oracles_use_same_total_replacement_order() -> None:
    """Edits beat originals, then timestamp and event ID break edit ties."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=lambda call_id: f"LIVE-FUZZ call={call_id} END call={call_id}",
    )
    original = _agent_reply_event("$source", "$reply", "original")
    original["origin_server_ts"] = 999

    def edit(event_id: str, body: str) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "sender": "@agent:example",
            "type": "m.room.message",
            "origin_server_ts": 100,
            "content": {
                "body": f"* {body}",
                "m.new_content": {"body": body},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
            },
        }

    edit_z = edit("$edit-z", "winner")
    edit_a = edit("$edit-a", "loser")
    try:
        for event in (original, edit_z, edit_a):
            oracle._ingest_event(event)
        assert oracle.latest_reply_bodies["$reply"][1] == "winner"

        events = {event["event_id"]: event for event in (original, edit_z, edit_a)}
        assert auditor._latest_agent_body(events, "$reply") == "winner"
        assert LiveFuzzRunner._latest_event_body(events.values(), "$reply") == "winner"
    finally:
        await client.close()


def test_response_view_diagnostic_exposes_order_status_and_body_tail() -> None:
    """Timeout evidence identifies which Matrix replacement actually wins."""
    original = _agent_reply_event("$source", "$reply", "original")
    original["origin_server_ts"] = 99
    original["content"]["io.mindroom.stream_status"] = "pending"
    partial = _agent_edit_event("$reply", "$partial", "PARTIAL", ts=100)
    partial["content"]["m.new_content"]["io.mindroom.stream_status"] = "streaming"
    complete = _agent_edit_event("$reply", "$complete", "FINAL END call=1", ts=101)
    complete["content"]["m.new_content"]["io.mindroom.stream_status"] = "completed"

    diagnostic = live_fuzz._response_view_diagnostic(
        (complete, original, partial),
        "$reply",
    )

    assert diagnostic == {
        "original_present": True,
        "ordered_candidates": [
            {
                "event_id": "$reply",
                "origin_server_ts": 99,
                "source": "original",
                "stream_status": "pending",
                "body_length": 8,
                "body_tail": "original",
            },
            {
                "event_id": "$partial",
                "origin_server_ts": 100,
                "source": "standalone",
                "stream_status": "streaming",
                "body_length": 7,
                "body_tail": "PARTIAL",
            },
            {
                "event_id": "$complete",
                "origin_server_ts": 101,
                "source": "standalone",
                "stream_status": "completed",
                "body_length": 16,
                "body_tail": "FINAL END call=1",
            },
        ],
        "latest_body_length": 16,
        "latest_body_tail": "FINAL END call=1",
    }


@pytest.mark.asyncio
async def test_all_reply_body_oracles_read_server_bundled_replacement() -> None:
    """Compacted history exposes its latest edit through bundled relations."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=_short_body_for,
    )
    original = _agent_reply_event("$source", "$reply", "partial")
    replacement = _agent_edit_event("$reply", "$edit", _short_body_for(7), ts=200)
    original["unsigned"] = {"m.relations": {"m.replace": {"event": replacement}}}
    try:
        oracle._ingest_event(original)
        assert oracle.latest_reply_bodies["$reply"][1] == _short_body_for(7)

        events = {"$reply": original}
        assert auditor._latest_agent_body(events, "$reply") == _short_body_for(7)
        assert LiveFuzzRunner._latest_event_body(events.values(), "$reply") == _short_body_for(7)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement_fields",
    [
        {},
        {"m.new_content": "not-an-object"},
        {"m.new_content": {"body": 123}},
    ],
)
async def test_all_reply_body_oracles_ignore_invalid_replacement_new_content(
    replacement_fields: dict[str, object],
) -> None:
    """An invalid edit cannot fall back to its forged outer fallback body."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=_short_body_for,
    )
    original = _agent_reply_event("$source", "$reply", "original")
    invalid_edit = {
        "event_id": "$edit",
        "sender": "@agent:example",
        "type": "m.room.message",
        "origin_server_ts": 999,
        "content": {
            "body": "forged outer fallback",
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
            **replacement_fields,
        },
    }
    try:
        oracle._ingest_event(original)
        oracle._ingest_event(invalid_edit)
        assert oracle.latest_reply_bodies["$reply"][1] == "original"

        events = {"$reply": original, "$edit": invalid_edit}
        assert auditor._latest_agent_body(events, "$reply") == "original"
        assert LiveFuzzRunner._latest_event_body(events.values(), "$reply") == "original"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_exact_reply_oracle_ignores_empty_defaultdict_entries() -> None:
    """Stale empty reply sets from bookkeeping reads must not count as replies."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    try:
        oracle.expect("root:0", "$source")
        assert oracle.unsettled_required_sources() == ["$source"]
        oracle.response_ids["$untracked-redaction-target"]
        oracle._assert_no_wrong_replies()
    finally:
        await client.close()


def test_chaos_validation_blocks_targets_of_redacted_unsettled_responses() -> None:
    """A reply suppressed by source redaction may never be awaited as a target."""
    cross_batch = LiveFuzzScenario(
        thread_count=1,
        profile="chaos",
        batches=(
            (LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "root:0"),),
            (LiveOperation(1, LiveOperationKind.REDACTION, 0, "op:0"),),
            (LiveOperation(2, LiveOperationKind.REACTION, 0, "response:op:0"),),
        ),
    )
    with pytest.raises(ValueError, match="may never settle"):
        cross_batch.validate()

    same_batch = LiveFuzzScenario(
        thread_count=1,
        profile="chaos",
        batches=(
            (LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "root:0"),),
            (
                LiveOperation(1, LiveOperationKind.REDACTION, 0, "op:0"),
                LiveOperation(2, LiveOperationKind.REACTION, 0, "response:op:0"),
            ),
        ),
    )
    with pytest.raises(ValueError, match="same-batch redacted sources"):
        same_batch.validate()

    settled_first = LiveFuzzScenario(
        thread_count=1,
        profile="chaos",
        batches=(
            (LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "root:0"),),
            (LiveOperation(1, LiveOperationKind.CHECKPOINT, 0, None),),
            (
                LiveOperation(2, LiveOperationKind.REDACTION, 0, "op:0"),
                LiveOperation(3, LiveOperationKind.REACTION, 0, "response:op:0"),
            ),
        ),
    )
    settled_first.validate()


def _write_ledger(ledger_path: Path, records: dict[str, TurnRecord]) -> None:
    """Store exact handled-turn projections in the production journal schema."""
    with closing(sqlite3.connect(ledger_path)) as database:
        for statement in schema_statements(SQLITE_DIALECT):
            database.execute(statement)
        database.execute("DELETE FROM turn_records WHERE agent_name = 'general'")
        database.executemany(
            "INSERT INTO turn_records (agent_name, index_event_id, anchor_event_id, record_json) VALUES (?, ?, ?, ?)",
            [
                ("general", event_id, record.anchor_event_id, json.dumps(TurnRecordCodec._to_ledger_record(record)))
                for event_id, record in records.items()
            ],
        )
        database.commit()


def _cleanup_qualification_runner(tmp_path: Path) -> LiveFuzzRunner:
    """Keep real runner, oracle, codec, and SQLite reads; replace only Matrix transport."""
    client = Mock(spec=LiveMatrixClient)
    client.user_id = "@user:example"
    client.room_id = "!room:example"
    client.send_event = AsyncMock(side_effect=["$ordinary", "$anchor"])
    stack = Mock(spec=ManagedTuwunelStack)
    stack.log_path = tmp_path / "mindroom.log"
    stack.runtime_redaction_path = tmp_path / "runtime-redactions.jsonl"
    stack.agent_id = "@agent:example"
    stack.router_id = "@router:example"
    stack.storage_path = tmp_path
    stack.room_keys = ("room",)
    stack.room_ids = {"room": "!room:example"}
    stack.room_id = "!room:example"
    runner = LiveFuzzRunner(
        stack,
        (client,),
        LiveFuzzScenario(1, (), profile="chaos"),
        reply_timeout=0.01,
        settle_seconds=0,
    )
    (tmp_path / "tracking").mkdir()
    runner.event_ids = {"root:0": "$root", "op:0": "$edit"}
    runner.oracle.expect("root:0", "$root", thread=0)
    runner.source_current_markers["$root"] = _source_marker("root:0", ORIGINAL_REVISION)
    runner.source_revision_markers["$root"]["$edit"] = _source_marker("root:0", "edit:0")
    runner.redacted_targets["$edit"] = "$delete-edit"
    return runner


def _cleanup_sent_events(runner: LiveFuzzRunner) -> dict[str, dict[str, Any]]:
    """Build transport-visible originals or exact redaction shells for the final audit fixture."""
    events: dict[str, dict[str, Any]] = {}
    for sent in runner.sent_records:
        events[sent.event_id] = {
            "event_id": sent.event_id,
            "type": sent.event_type,
            "sender": sent.sender,
            "content": {} if sent.event_id in runner.redacted_targets else dict(sent.content or {}),
        }
        if sent.event_id in runner.redacted_targets:
            events[sent.event_id]["unsigned"] = {
                "redacted_because": {"event_id": runner.redacted_targets[sent.event_id]},
            }
    return events


@pytest.mark.asyncio
async def test_unconsumed_edit_physical_tombstone_settles_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deletion before revision registration settles the exact edit after an answered source."""
    runner = _cleanup_qualification_runner(tmp_path)
    ledger = tmp_path / "tracking/event_journal.db"
    journal = EventJournalStore.open_sqlite(ledger)
    store = TurnStore(
        TurnStoreDeps(
            agent_name="general",
            turn_records=journal.turn_records("general"),
            redacted_event_ids=journal.principal("agent@alice").redacted_event_ids,
            legacy_responses_file=None,
            state_writer=Mock(),
            resolver=Mock(),
            tool_runtime=Mock(),
        ),
    )
    try:
        await store.warm()
        await store.record_responded_turn(
            TurnRecord.create(source_event_ids=("$root",), response_event_id="$root-reply"),
        )
        await store.mark_source_redacted("$edit")
        # Runtime exact-event invalidation establishes the expectation independently of the harness.
        assert store.is_revision_redacted("$edit")
    finally:
        await journal.close()

    records = live_fuzz.read_ledger_records(ledger, strict=True)
    assert records["$root"].completed
    assert records["$root"].response_event_id == "$root-reply"
    assert "$edit" not in (records["$root"].revision_replay or {})
    assert not records["$edit"].completed
    assert records["$edit"].redacted_source_event_ids == ("$edit",)
    assert not records["$edit"].pending_redaction_cleanup_event_ids
    runner._pending_source_tombstones.add("$edit")
    monkeypatch.setattr(runner.oracle, "pump", AsyncMock())
    await runner._wait_for_pending_mutation_effects(deadline_seconds=0.01, batch_index=29)
    assert not runner._pending_source_tombstones
    assert runner._qualified_cleanup_targets(LiveOperation(1, LiveOperationKind.THREAD_MESSAGE, 0, "root:0")) == (
        "$edit",
    )
    assert live_fuzz._redaction_target_state("$edit", records, runner.source_revision_markers) == (True, False)


@pytest.mark.parametrize(
    ("physical", "root_revision", "context_revision", "expected"),
    [
        (None, None, None, (False, False)),
        (None, None, "wrong_edit", (False, False)),
        (None, None, "wrong_source", (False, False)),
        (None, None, "registered", (False, False)),
        (None, None, "clean", (True, False)),
        (None, "clean", "pending", (True, True)),
        (None, "clean", "registered", (True, True)),
        ("clean", None, "pending", (True, True)),
        ("clean", None, "registered", (True, True)),
        ("clean", "clean", "pending", (True, True)),
        ("clean", "clean", "clean", (True, False)),
        ("clean", None, "wrong_source", (True, False)),
        ("pending", "clean", "clean", (True, True)),
        ("registered", None, None, (False, False)),
    ],
)
def test_edit_tombstone_checks_every_matching_cleanup_owner(
    tmp_path: Path,
    physical: str | None,
    root_revision: str | None,
    context_revision: str | None,
    expected: tuple[bool, bool],
) -> None:
    """Exact deletion evidence cannot hide another response owner's unreconciled cleanup."""
    records = {}
    for owner, state in (("$root", root_revision), ("$context", context_revision)):
        replay = (
            {
                "$wrong" if state == "wrong_edit" else "$edit": RevisionReplay(
                    "$wrong" if state == "wrong_source" else "$root",
                    100,
                    redacted=state != "registered",
                    cleanup_pending=state == "pending",
                ),
            }
            if state is not None
            else None
        )
        records[owner] = TurnRecord.create(
            source_event_ids=(owner,),
            response_event_id=f"{owner}-reply",
            revision_replay=replay,
            # An original source's deletion proves nothing about its physical edit.
            redacted_source_event_ids=(owner,) if owner == "$root" else (),
        )
    if physical is not None:
        records["$edit"] = TurnRecord.create(
            source_event_ids=("$edit",),
            completed=physical == "registered",
            redacted_source_event_ids=() if physical == "registered" else ("$edit",),
            pending_redaction_cleanup_event_ids=("$edit",) if physical == "pending" else (),
        )
    ledger = tmp_path / "event_journal.db"
    _write_ledger(ledger, records)
    persisted = live_fuzz.read_ledger_records(ledger, strict=True)
    assert live_fuzz._redaction_target_state("$edit", persisted, {"$root": {"$edit": "marker"}}) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["no_response", "redacted", "missing", "incomplete"])
@pytest.mark.parametrize("attempt", ["none", "clean", "contaminated", "pending"])
@pytest.mark.parametrize("dedicated", [False, True])
async def test_ordinary_cleanup_qualification_preserves_terminal_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    attempt: str,
    dedicated: bool,
) -> None:
    """Real ordinary qualification must audit attempts without inventing a response obligation."""
    runner = _cleanup_qualification_runner(tmp_path)
    ledger = tmp_path / "tracking/event_journal.db"
    root_record = TurnRecord.create(
        source_event_ids=("$root",),
        response_event_id="$root-reply",
        revision_replay={"$edit": RevisionReplay("$root", 100, redacted=True, cleanup_pending=True)},
    )
    _write_ledger(ledger, {"$root": root_record})
    operation = LiveOperation(
        1,
        LiveOperationKind.THREAD_MESSAGE,
        0,
        "root:0",
        cleanup_sources=("op:0",) if dedicated else (),
    )
    runner.scenario = replace(
        runner.scenario,
        batches=(
            (LiveOperation(0, LiveOperationKind.EDIT, 0, "root:0"),),
            (LiveOperation(10, LiveOperationKind.REDACTION, 0, "op:0"),),
            (operation,),
        ),
    )
    runner.scenario.validate()
    runner._record_batch_results([await runner._apply(operation)])
    assert runner._cleanup_probe_targets == {"$ordinary": ("$edit",)}
    events = {
        "$root": {
            "event_id": "$root",
            "type": "m.room.message",
            "sender": runner.client.user_id,
            "content": {"body": "root"},
        },
        "$root-reply": _agent_reply_event("$root", "$root-reply", _short_body_for(1)),
    }
    records = {
        "$root": replace(
            root_record,
            revision_replay={
                "$edit": RevisionReplay(
                    "$root",
                    100,
                    redacted=True,
                    cleanup_pending=attempt == "pending" or (outcome == "redacted" and attempt == "none"),
                ),
            },
        ),
    }
    observed = {1: frozenset({_source_marker("root:0", ORIGINAL_REVISION)})}
    if outcome != "redacted":
        await runner._apply(LiveOperation(2, LiveOperationKind.THREAD_MESSAGE, 0, "root:0"))
        events["$anchor-reply"] = _agent_reply_event("$anchor", "$anchor-reply", _short_body_for(3))
        events["$anchor-reply"]["content"]["m.relates_to"]["event_id"] = "$root"
        records["$anchor"] = TurnRecord.create(source_event_ids=("$anchor",), response_event_id="$anchor-reply")
        observed[3] = frozenset({_source_marker("op:2", ORIGINAL_REVISION)})
    if outcome != "missing":
        records["$ordinary"] = TurnRecord.create(
            source_event_ids=("$ordinary",),
            completed=outcome == "no_response",
            redacted_source_event_ids=("$ordinary",) if outcome == "redacted" else (),
        )
    if outcome == "redacted":
        runner.redacted_targets["$ordinary"] = "$delete-ordinary"
        runner.oracle.mark_source_optional("$ordinary")
    events.update(_cleanup_sent_events(runner))
    _write_ledger(ledger, records)
    runner.client.paginate_room = AsyncMock(return_value=list(events.values()))
    for event in events.values():
        runner.oracle._ingest_event(event)
    if attempt != "none":
        observed[2] = frozenset({_source_marker("op:1", ORIGINAL_REVISION)})
    full_requests = dict(observed)
    if attempt == "contaminated":
        full_requests[2] |= {_source_marker("root:0", "edit:0")}
    monkeypatch.setattr(_ModelHandler, "_observed_markers", observed)
    monkeypatch.setattr(_ModelHandler, "_full_request_markers", full_requests)
    monkeypatch.setattr(_ModelHandler, "response_text_for", _short_body_for)
    failure = {"missing": "supersession proof", "incomplete": "incomplete"}.get(outcome)
    if dedicated and failure is None:
        failure = "has no completed response"
    failure = failure or {"pending": "pending or missing tombstone cleanup", "contaminated": "redacted history"}.get(
        attempt,
    )
    if failure is not None:
        with pytest.raises(AssertionError, match=failure):
            await runner._audit_final_state()
    else:
        result = await runner._audit_final_state()
        assert result["redaction_cleanup_uncovered_sources"] == int(attempt == "none")
        assert result["redaction_cleanup_checked_calls"] == int(outcome != "redacted") + int(attempt != "none")


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("provenance", ["wrong_source", "wrong_edit", "not_redacted"])
async def test_cleanup_admission_rejects_inexact_incomplete_owner_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
    provenance: str,
) -> None:
    """Keeping unfinished observation records must not relax exact edit provenance."""
    runner = _cleanup_qualification_runner(tmp_path)
    ledger = tmp_path / "tracking/event_journal.db"
    record = TurnRecord.create(
        source_event_ids=("$root",),
        completed=False,
        revision_replay={
            "$wrong" if provenance == "wrong_edit" else "$edit": RevisionReplay(
                "$wrong" if provenance == "wrong_source" else "$root",
                100,
                redacted=provenance != "not_redacted",
                cleanup_pending=True,
            ),
        },
    )
    _write_ledger(ledger, {"$root": record})
    monkeypatch.setattr(runner.oracle, "pump", AsyncMock())
    operation = LiveOperation(
        1,
        LiveOperationKind.THREAD_MESSAGE,
        0,
        "root:0",
        cleanup_sources=("op:0",) if explicit else (),
    )
    if explicit:
        with pytest.raises(AssertionError, match="cleanup probe tombstones"):
            await runner._apply(operation)
    else:
        await runner._apply(operation)
    assert runner._cleanup_probe_targets == {}
    assert not runner.oracle.source_completed_without_response("$root")


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_cleanup_admission_reads_incomplete_owner_edit_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
) -> None:
    """Serialized edit tombstones admit later input while their live owner remains unfinished."""
    runner = _cleanup_qualification_runner(tmp_path)
    ledger = tmp_path / "tracking/event_journal.db"
    incomplete = TurnRecord.create(
        source_event_ids=("$root",),
        completed=False,
        response_event_id="$inflight",
        revision_replay={"$edit": RevisionReplay("$root", 100, redacted=True, cleanup_pending=True)},
    )
    _write_ledger(ledger, {"$root": incomplete})
    monkeypatch.setattr(
        runner.oracle,
        "pump",
        AsyncMock(side_effect=AssertionError("waited despite committed edit tombstone")),
    )
    await runner._apply(
        LiveOperation(1, LiveOperationKind.THREAD_MESSAGE, 0, "root:0", cleanup_sources=("op:0",) if explicit else ()),
    )
    assert runner._cleanup_probe_targets == {"$ordinary": ("$edit",)}
    assert runner.oracle.ledger_response("$root") is None
    assert not runner.oracle.source_completed_without_response("$root")
    assert "$root" in runner.oracle.unsettled_required_sources()
    assert live_fuzz.read_ledger_records(ledger) == {}
    with pytest.raises(AssertionError, match="incomplete"):
        live_fuzz.read_ledger_records(ledger, strict=True)
    with pytest.raises(AssertionError, match="incomplete"):
        live_fuzz.read_ledger_records(ledger, strict=True, include_incomplete=True)


@pytest.mark.asyncio
async def test_coalescing_oracle_settles_via_ledger_attribution(tmp_path: Path) -> None:
    """Sources swallowed into a combined follow-up turn settle via the durable ledger.

    A newer source anchors on its own visible combined reply, but an
    older source can settle through its own completed no-response outcome,
    which does not prove supersession or attribute the newer reply.
    """
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    ledger_path = tmp_path / "event_journal.db"
    oracle = ExactReplyOracle(
        client,
        "@agent:example",
        coalescing_threads=True,
        ledger_path=ledger_path,
    )
    try:
        oracle.expect("op:1", "$first", thread=3, client=1)
        oracle.expect("op:2", "$second", thread=3, client=1)
        for source in ("$first", "$second"):
            oracle._ingest_event({"event_id": source, "sender": "@user:example", "type": "m.room.message"})
        assert set(oracle.unsettled_required_sources()) == {"$first", "$second"}

        # The combined reply anchors the newest source, but the older source
        # has no durable terminal record of its own yet, so it stays unsettled.
        oracle._ingest_event(_agent_reply_event("$second", "$combined-reply", "LIVE-FUZZ call=2 END call=2"))
        assert oracle.unsettled_required_sources() == ["$first"]
        assert oracle.resolve_response_ref("response:op:2") == "$combined-reply"
        with pytest.raises(KeyError, match="response event not observed"):
            oracle.resolve_response_ref("response:op:1")

        # A separately completed no-response turn settles without attributing the
        # combined reply or pretending a replay-guard decision occurred.
        second_record = TurnRecord.create(
            source_event_ids=("$second",),
            response_event_id="$combined-reply",
            completed=True,
        )
        superseded_record = TurnRecord.create(source_event_ids=("$first",), response_event_id=None, completed=True)
        _write_ledger(ledger_path, {"$first": superseded_record, "$second": second_record})
        oracle.refresh_ledger_attributions(min_interval=0.0)
        assert oracle.unsettled_required_sources() == []
        assert not oracle._supersession_proven("$first")
        with pytest.raises(KeyError, match="response event not observed"):
            oracle.resolve_response_ref("response:op:1")

        # A dedicated response-backed record instead attributes the older source
        # directly to its own reply.
        dedicated_record = TurnRecord.create(
            source_event_ids=("$first",),
            response_event_id="$dedicated-reply",
            completed=True,
        )
        _write_ledger(ledger_path, {"$first": dedicated_record, "$second": second_record})
        oracle.refresh_ledger_attributions(min_interval=0.0)
        assert oracle.resolve_response_ref("response:op:1") == "$dedicated-reply"
        oracle._assert_no_wrong_replies()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_coalescing_oracle_requires_own_record_for_incomplete_supersession(tmp_path: Path) -> None:
    """A missing or incomplete older record blocks settlement even once anchored."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    ledger_path = tmp_path / "event_journal.db"
    oracle = ExactReplyOracle(
        client,
        "@agent:example",
        coalescing_threads=True,
        ledger_path=ledger_path,
    )
    try:
        oracle.expect("op:1", "$first", thread=3, client=1)
        oracle.expect("op:2", "$second", thread=3, client=1)
        for source in ("$first", "$second"):
            oracle._ingest_event({"event_id": source, "sender": "@user:example", "type": "m.room.message"})
        oracle._ingest_event(_agent_reply_event("$second", "$combined-reply", "LIVE-FUZZ call=2 END call=2"))

        second_record = TurnRecord.create(
            source_event_ids=("$second",),
            response_event_id="$combined-reply",
            completed=True,
        )
        # An incomplete older record never proves supersession.
        incomplete = TurnRecord.create(source_event_ids=("$first",), response_event_id=None, completed=False)
        _write_ledger(ledger_path, {"$first": incomplete, "$second": second_record})
        oracle.refresh_ledger_attributions(min_interval=0.0)
        assert oracle.unsettled_required_sources() == ["$first"]
        with pytest.raises(KeyError, match="response event not observed"):
            oracle.resolve_response_ref("response:op:1")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_redacting_settled_coalesced_source_does_not_mark_optional(tmp_path: Path) -> None:
    """Durably settled coalesced work stays required after source redaction."""
    matrix_client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    ledger_path = tmp_path / "event_journal.db"
    oracle = ExactReplyOracle(
        matrix_client,
        "@agent:example",
        coalescing_threads=True,
        ledger_path=ledger_path,
    )
    try:
        oracle.expect("op:1", "$first", thread=0, client=0)
        oracle.expect("op:2", "$second", thread=0, client=0)
        for source in ("$first", "$second"):
            oracle._ingest_event({"event_id": source, "sender": "@user:example", "type": "m.room.message"})
        oracle._ingest_event(_agent_reply_event("$second", "$combined", "LIVE-FUZZ call=2 END call=2"))
        _write_ledger(
            ledger_path,
            {
                "$first": TurnRecord.create(source_event_ids=("$first",), response_event_id=None, completed=True),
                "$second": TurnRecord.create(
                    source_event_ids=("$second",),
                    response_event_id="$combined",
                    completed=True,
                ),
            },
        )
        oracle.refresh_ledger_attributions(min_interval=0.0)
        assert "$first" in oracle.settled_sources()
        assert oracle.response_ids["$first"] == set()

        class RedactionClient:
            user_id = "@user:example"

            @staticmethod
            async def redact(
                _target_event_id: str,
                _txn_id: str,
                *,
                room_id: str,
            ) -> str:
                assert room_id == "!room:example"
                return "$redaction"

        runner = object.__new__(LiveFuzzRunner)
        runner.oracle = oracle
        runner.redacted_targets = {}
        runner.sent_records = []
        runner._edit_event_source = {}
        runner.redacted_edit_evidence = {}
        runner.source_revision_markers = defaultdict(dict)
        runner._pending_edit_markers = {}
        runner._pending_source_tombstones = set()
        runner._resolve_target = lambda _logical_ref: asyncio.sleep(0, result="$first")  # type: ignore[method-assign]
        runner._client_for_operation = lambda _operation: RedactionClient()  # type: ignore[method-assign]
        runner._room_for_thread = lambda _thread: "!room:example"  # type: ignore[method-assign]
        pump_calls: list[int] = []

        async def pump(*, timeout_ms: int) -> None:
            pump_calls.append(timeout_ms)

        oracle.pump = pump  # type: ignore[method-assign]

        operation = LiveOperation(3, LiveOperationKind.REDACTION, 0, "op:1")
        await runner._apply(operation)

        assert pump_calls == [0]
        assert "$first" not in oracle.optional_sources
    finally:
        await matrix_client.close()


@pytest.mark.asyncio
async def test_source_redaction_pumps_visible_reply_before_optional_classification() -> None:
    """A server-visible reply cannot be hidden by the oracle's stale sync view."""
    matrix_client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(matrix_client, "@agent:example", coalescing_threads=True)
    try:
        oracle.expect("op:1", "$source", thread=0, client=0)
        oracle._ingest_event({"event_id": "$source", "sender": "@user:example", "type": "m.room.message"})

        async def pump(*, timeout_ms: int) -> None:
            assert timeout_ms == 0
            oracle._ingest_event(_agent_reply_event("$source", "$reply", "LIVE-FUZZ call=1 END call=1"))

        oracle.pump = pump  # type: ignore[method-assign]

        class RedactionClient:
            user_id = "@user:example"

            @staticmethod
            async def redact(
                _target_event_id: str,
                _txn_id: str,
                *,
                room_id: str,
            ) -> str:
                assert room_id == "!room:example"
                return "$redaction"

        runner = object.__new__(LiveFuzzRunner)
        runner.oracle = oracle
        runner.redacted_targets = {}
        runner.sent_records = []
        runner._edit_event_source = {}
        runner.redacted_edit_evidence = {}
        runner.source_revision_markers = defaultdict(dict)
        runner._pending_edit_markers = {}
        runner._pending_source_tombstones = set()
        runner._resolve_target = lambda _logical_ref: asyncio.sleep(0, result="$source")  # type: ignore[method-assign]
        runner._client_for_operation = lambda _operation: RedactionClient()  # type: ignore[method-assign]
        runner._room_for_thread = lambda _thread: "!room:example"  # type: ignore[method-assign]

        await runner._apply(LiveOperation(3, LiveOperationKind.REDACTION, 0, "op:1"))

        assert oracle.response_ids["$source"] == {"$reply"}
        assert "$source" not in oracle.optional_sources
    finally:
        await matrix_client.close()


@pytest.mark.asyncio
async def test_coalescing_oracle_requires_every_source_observed() -> None:
    """A source lost by the homeserver or sync stream blocks settlement."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example", coalescing_threads=True)
    try:
        oracle.expect("op:1", "$observed", thread=0)
        oracle.expect("op:2", "$lost", thread=0)
        oracle._ingest_event({"event_id": "$observed", "sender": "@user:example", "type": "m.room.message"})
        oracle._ingest_event(_agent_reply_event("$observed", "$reply", "LIVE-FUZZ call=1 END call=1"))

        assert oracle.unsettled_required_sources() == ["$lost"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ledger_attribution_flags_missing_and_orphaned_turns(tmp_path: Path) -> None:
    """Durable attribution must cover every required source and every visible reply.

    An older source with no completed record of its own can never be inferred
    completed from chronology; only production's own completed no-response
    record settles it.
    """
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example", coalescing_threads=True)
    ledger_path = tmp_path / "event_journal.db"
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=lambda call_id: f"LIVE-FUZZ call={call_id} END call={call_id}",
        ledger_path=ledger_path,
    )
    try:
        oracle.expect("op:1", "$first", thread=0)
        oracle.expect("op:2", "$second", thread=0)
        replies = {"$second": {"$combined-reply"}}

        coalesced_record = TurnRecord.create(
            source_event_ids=("$first", "$second"),
            response_event_id="$combined-reply",
            completed=True,
        )
        # One coalesced record attributing both sources to one visible reply passes.
        _write_ledger(ledger_path, {"$first": coalesced_record, "$second": coalesced_record})
        assert auditor._assert_ledger_attribution(replies) == {
            "ledger_attributed_sources": 2,
            "ledger_superseded_sources": 0,
            "ledger_recovered_sources": 0,
        }

        # The newest source alone with the older one absent fails: the older
        # source has no durable terminal record at all.
        _write_ledger(ledger_path, {"$second": coalesced_record})
        with pytest.raises(AssertionError, match="superseded chain source"):
            auditor._assert_ledger_attribution(replies)

        # Final audit rejects an incomplete record directly.
        incomplete_first = TurnRecord.create(source_event_ids=("$first",), response_event_id=None, completed=False)
        anchor_second = TurnRecord.create(
            source_event_ids=("$second",),
            response_event_id="$combined-reply",
            completed=True,
        )
        _write_ledger(ledger_path, {"$first": incomplete_first, "$second": anchor_second})
        with pytest.raises(AssertionError, match=r"\$first.*incomplete"):
            auditor._assert_ledger_attribution(replies)

        # A completed no-response turn remains completed generation, not supersession.
        superseded_first = TurnRecord.create(source_event_ids=("$first",), response_event_id=None, completed=True)
        _write_ledger(ledger_path, {"$first": superseded_first, "$second": anchor_second})
        assert auditor._assert_ledger_attribution(replies) == {
            "ledger_attributed_sources": 2,
            "ledger_superseded_sources": 0,
            "ledger_recovered_sources": 0,
        }

        # A visible reply with no durable record attributing it is an orphan.
        orphan = {"$second": {"$combined-reply"}, "$first": {"$rogue-reply"}}
        _write_ledger(ledger_path, {"$first": coalesced_record, "$second": coalesced_record})
        with pytest.raises(AssertionError, match="not attributed by any durable turn record"):
            auditor._assert_ledger_attribution(orphan)

        # The newest chain source itself missing a record fails distinctly.
        _write_ledger(ledger_path, {"$first": superseded_first})
        with pytest.raises(AssertionError, match="newest chain source"):
            auditor._assert_ledger_attribution(replies)

        foreign = TurnRecord.create(
            source_event_ids=("$other",),
            discovery_event_ids=("$first",),
            response_event_id="$combined-reply",
            completed=True,
        )
        _write_ledger(ledger_path, {"$first": foreign, "$second": anchor_second})
        with pytest.raises(AssertionError, match="does not own"):
            auditor._assert_ledger_attribution(replies)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ledger_attribution_accepts_cross_requester_coalesced_record(
    tmp_path: Path,
) -> None:
    """One completed record may attach its shared reply to either owned source."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example", coalescing_threads=True)
    ledger_path = tmp_path / "event_journal.db"
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=_short_body_for,
        ledger_path=ledger_path,
    )
    try:
        oracle.expect("op:1", "$chain-a", thread=0, client=0)
        oracle.expect("op:2", "$chain-b", thread=0, client=1)
        coalesced = TurnRecord.create(
            source_event_ids=("$chain-a", "$chain-b"),
            response_event_id="$one-reply",
            completed=True,
        )
        _write_ledger(ledger_path, {"$chain-a": coalesced, "$chain-b": coalesced})

        assert auditor._assert_ledger_attribution({"$chain-b": {"$one-reply"}}) == {
            "ledger_attributed_sources": 2,
            "ledger_superseded_sources": 0,
            "ledger_recovered_sources": 0,
        }

        stale = replace(coalesced, response_event_id="$stale-reply")
        _write_ledger(ledger_path, {"$chain-a": stale, "$chain-b": stale})
        with pytest.raises(AssertionError, match=r"\$stale-reply.*not a visible canonical reply"):
            auditor._assert_ledger_attribution({"$chain-b": {"$one-reply"}})

        chain_a = TurnRecord.create(
            source_event_ids=("$chain-a",),
            response_event_id="$one-reply",
            completed=True,
        )
        chain_b = TurnRecord.create(
            source_event_ids=("$chain-b",),
            response_event_id="$one-reply",
            completed=True,
        )
        _write_ledger(ledger_path, {"$chain-a": chain_a, "$chain-b": chain_b})
        with pytest.raises(AssertionError, match=r"\$chain-a.*not a visible canonical reply"):
            auditor._assert_ledger_attribution({"$chain-b": {"$one-reply"}})
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ledger_attribution_rejects_coalesced_record_across_threads(
    tmp_path: Path,
) -> None:
    """One record cannot claim a visible reply from another logical thread."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example", coalescing_threads=True)
    ledger_path = tmp_path / "event_journal.db"
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=_short_body_for,
        ledger_path=ledger_path,
    )
    try:
        oracle.expect("op:1", "$chain-a", thread=0, client=0)
        oracle.expect("op:2", "$chain-b", thread=1, client=1)
        forged = TurnRecord.create(
            source_event_ids=("$chain-a", "$chain-b"),
            response_event_id="$one-reply",
            completed=True,
        )
        _write_ledger(ledger_path, {"$chain-a": forged, "$chain-b": forged})

        with pytest.raises(AssertionError, match="coalesces sources across logical Matrix threads"):
            auditor._assert_ledger_attribution({"$chain-b": {"$one-reply"}})
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_final_ledger_audit_rejects_one_malformed_projection(tmp_path: Path) -> None:
    """One corrupt row cannot disappear and let an otherwise valid audit pass."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example", coalescing_threads=True)
    ledger_path = tmp_path / "event_journal.db"
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=lambda call_id: f"LIVE-FUZZ call={call_id} END call={call_id}",
        ledger_path=ledger_path,
    )
    try:
        oracle.expect("op:1", "$source", thread=0)
        record = TurnRecord.create(source_event_ids=("$source",), response_event_id="$reply", completed=True)
        _write_ledger(ledger_path, {"$source": record})
        with closing(sqlite3.connect(ledger_path)) as database:
            database.execute(
                "INSERT INTO turn_records (agent_name, index_event_id, anchor_event_id, record_json) VALUES (?, ?, ?, ?)",
                ("general", "$corrupt", "$corrupt", json.dumps({"completed": True})),
            )
            database.commit()

        # Live settlement remains tolerant while a write may be in flight.
        assert live_fuzz.read_ledger_records(ledger_path) == {"$source": record}
        with pytest.raises(AssertionError, match=r"ledger invalid.*\$corrupt.*invalid projection"):
            auditor._assert_ledger_attribution({"$source": {"$reply"}})
    finally:
        await client.close()


def test_strict_ledger_read_rejects_incomplete_record(tmp_path: Path) -> None:
    """A final audit cannot silently ignore a durable non-terminal turn."""
    ledger_path = tmp_path / "event_journal.db"
    _write_ledger(
        ledger_path,
        {
            "$pending": TurnRecord.create(
                source_event_ids=("$pending",),
                response_event_id=None,
                completed=False,
            ),
        },
    )

    assert live_fuzz.read_ledger_records(ledger_path) == {}
    with pytest.raises(AssertionError, match=r"\$pending.*incomplete"):
        live_fuzz.read_ledger_records(ledger_path, strict=True)


@pytest.mark.asyncio
async def test_final_audit_reuses_one_ledger_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One audit uses one durable snapshot for every ledger assertion."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    client.paginate_room = AsyncMock(return_value=[])
    oracle = ExactReplyOracle(client, "@agent:example")
    ledger_path = tmp_path / "event_journal.db"
    _write_ledger(ledger_path, {})
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=_short_body_for,
        ledger_path=ledger_path,
    )
    reads = 0
    original_read = live_fuzz._read_supersession_snapshot

    def count_reads(path: Path, principal_id: str) -> live_fuzz._SupersessionSnapshot:
        nonlocal reads
        reads += 1
        return original_read(path, principal_id)

    monkeypatch.setattr(live_fuzz, "_read_supersession_snapshot", count_reads)
    try:
        await auditor.audit(
            room_ids={"!room:example"},
            sent_records=(),
            redacted_targets={},
        )
    finally:
        await client.close()

    assert reads == 1


def test_strict_ledger_read_accepts_durable_redaction_tombstone(tmp_path: Path) -> None:
    """A durable tombstone settles replay while session cleanup awaits a response."""
    ledger_path = tmp_path / "event_journal.db"
    tombstone = TurnRecord.create(
        source_event_ids=("$stop-reaction",),
        redacted_source_event_ids=("$stop-reaction",),
        response_event_id=None,
        completed=False,
    )
    _write_ledger(ledger_path, {"$stop-reaction": tombstone})

    assert live_fuzz.read_ledger_records(ledger_path, strict=True) == {
        "$stop-reaction": tombstone,
    }
    oracle = ExactReplyOracle(
        AsyncMock(),
        "@agent:example",
        ledger_path=ledger_path,
    )
    oracle.refresh_ledger_attributions(min_interval=0)
    assert oracle.source_tombstoned("$stop-reaction")

    pending_cleanup = replace(
        tombstone,
        pending_redaction_cleanup_event_ids=("$stop-reaction",),
    )
    _write_ledger(ledger_path, {"$stop-reaction": pending_cleanup})
    assert live_fuzz.read_ledger_records(ledger_path, strict=True) == {
        "$stop-reaction": pending_cleanup,
    }
    oracle.refresh_ledger_attributions(min_interval=0)
    assert oracle.source_tombstoned("$stop-reaction")


@pytest.mark.asyncio
async def test_visible_optional_reply_requires_durable_attribution(tmp_path: Path) -> None:
    """Optional means zero replies are allowed, not unattributed visible replies."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example", coalescing_threads=True)
    ledger_path = tmp_path / "event_journal.db"
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=lambda call_id: f"LIVE-FUZZ call={call_id} END call={call_id}",
        ledger_path=ledger_path,
    )
    try:
        oracle.expect("op:1", "$optional", thread=0)
        oracle.mark_source_optional("$optional")
        replies = {"$optional": {"$reply"}}

        _write_ledger(ledger_path, {})
        with pytest.raises(AssertionError, match="optional-source reply"):
            auditor._assert_ledger_attribution(replies)

        record = TurnRecord.create(source_event_ids=("$optional",), response_event_id="$reply", completed=True)
        _write_ledger(ledger_path, {"$optional": record})
        assert auditor._assert_ledger_attribution(replies) == {
            "ledger_attributed_sources": 1,
            "ledger_superseded_sources": 0,
            "ledger_recovered_sources": 0,
        }

        _write_ledger(
            ledger_path,
            {
                "$optional": TurnRecord.create(
                    source_event_ids=("$optional",),
                    response_event_id="$phantom",
                    completed=True,
                ),
            },
        )
        with pytest.raises(AssertionError, match="not a visible canonical reply"):
            auditor._assert_ledger_attribution({})
    finally:
        await client.close()


def _recovery_auditor(client: LiveMatrixClient) -> FinalStateAuditor:
    oracle = ExactReplyOracle(
        client,
        "@agent:example",
        coalescing_threads=True,
        internal_relay_senders=("@router:example",),
    )
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=lambda call_id: f"LIVE-FUZZ call={call_id} END call={call_id}",
    )
    oracle.expect("op:1", "$source", thread=0)
    return auditor


def _interrupted_reply(thread_root: str = "$root") -> dict[str, Any]:
    interrupted = _agent_reply_event(
        "$source",
        "$reply",
        f"LIVE-FUZZ call=9 {RESTART_INTERRUPTED_RESPONSE_NOTE}",
    )
    interrupted["content"]["m.relates_to"]["event_id"] = thread_root
    return interrupted


@pytest.mark.asyncio
async def test_final_body_audit_rejects_resume_chain_without_durable_proof() -> None:
    """Visible recovery relations cannot replace joined durable continuation ownership."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    auditor = _recovery_auditor(client)
    try:
        events: dict[str, Any] = {"$reply": _interrupted_reply()}
        replies = {"$source": {"$reply"}}
        # An interrupted note with no resume chain fails.
        with pytest.raises(AssertionError, match="non-canonical body"):
            auditor._assert_final_bodies_complete(events, replies)

        # The relay R replies to the interrupted response I in the same thread.
        events["$relay"] = _resume_relay_event(
            event_id="$relay",
            thread_root="$root",
            in_reply_to="$reply",
        )
        # The completed agent response A replies to the relay R in the thread.
        events["$resumed"] = _threaded_reply_event(
            sender="@agent:example",
            event_id="$resumed",
            thread_root="$root",
            in_reply_to="$relay",
            body="LIVE-FUZZ call=11 END call=11",
        )
        with pytest.raises(AssertionError, match="non-canonical body"):
            auditor._assert_final_bodies_complete(events, replies)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_final_body_audit_rejects_relay_for_another_interruption() -> None:
    """A relay replying to a different interrupted response never recovers this one."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    auditor = _recovery_auditor(client)
    try:
        events: dict[str, Any] = {
            "$reply": _interrupted_reply(),
            # Relay points at some other interrupted response, not $reply.
            "$relay": _resume_relay_event(
                event_id="$relay",
                thread_root="$root",
                in_reply_to="$other-interrupted",
            ),
            "$resumed": _threaded_reply_event(
                sender="@agent:example",
                event_id="$resumed",
                thread_root="$root",
                in_reply_to="$relay",
                body="LIVE-FUZZ call=11 END call=11",
            ),
        }
        replies = {"$source": {"$reply"}}
        with pytest.raises(AssertionError, match="non-canonical body"):
            auditor._assert_final_bodies_complete(events, replies)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_final_body_audit_rejects_agent_reply_to_other_event() -> None:
    """A completed agent reply to some non-relay event never recovers the note."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    auditor = _recovery_auditor(client)
    try:
        events: dict[str, Any] = {
            "$reply": _interrupted_reply(),
            "$relay": _resume_relay_event(
                event_id="$relay",
                thread_root="$root",
                in_reply_to="$reply",
            ),
            # Agent reply targets a bystander event, not the relay.
            "$resumed": _threaded_reply_event(
                sender="@agent:example",
                event_id="$resumed",
                thread_root="$root",
                in_reply_to="$bystander",
                body="LIVE-FUZZ call=11 END call=11",
            ),
        }
        replies = {"$source": {"$reply"}}
        with pytest.raises(AssertionError, match="non-canonical body"):
            auditor._assert_final_bodies_complete(events, replies)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_final_body_audit_rejects_resume_in_wrong_thread() -> None:
    """A completed agent reply to the relay in another thread never recovers."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    auditor = _recovery_auditor(client)
    try:
        events: dict[str, Any] = {
            "$reply": _interrupted_reply(),
            "$relay": _resume_relay_event(
                event_id="$relay",
                thread_root="$root",
                in_reply_to="$reply",
            ),
            # Correct reply target but a different thread root.
            "$resumed": _threaded_reply_event(
                sender="@agent:example",
                event_id="$resumed",
                thread_root="$other-root",
                in_reply_to="$relay",
                body="LIVE-FUZZ call=11 END call=11",
            ),
        }
        replies = {"$source": {"$reply"}}
        with pytest.raises(AssertionError, match="non-canonical body"):
            auditor._assert_final_bodies_complete(events, replies)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_final_body_audit_rejects_missing_relay_event() -> None:
    """A resume answer whose relay event is absent from the map never recovers."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    auditor = _recovery_auditor(client)
    try:
        events: dict[str, Any] = {
            "$reply": _interrupted_reply(),
            # No $relay event in the map, even though the agent reply names it.
            "$resumed": _threaded_reply_event(
                sender="@agent:example",
                event_id="$resumed",
                thread_root="$root",
                in_reply_to="$relay",
                body="LIVE-FUZZ call=11 END call=11",
            ),
        }
        replies = {"$source": {"$reply"}}
        with pytest.raises(AssertionError, match="non-canonical body"):
            auditor._assert_final_bodies_complete(events, replies)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_latest_agent_body_breaks_timestamp_ties_by_event_id() -> None:
    """Equal-timestamp replacements select the lexicographically larger event ID.

    Regression guard for the refuted O2 finding: Matrix v1.19 selects the
    largest event ID on a replacement timestamp tie, so arrival order must not
    override event-ID ordering.
    """
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    auditor = _recovery_auditor(client)
    try:
        events: dict[str, Any] = {"$reply": _agent_reply_event("$source", "$reply", "partial")}
        # Insert the smaller-ID edit last, in the opposite order from event-ID
        # ordering, so arrival order and ID ordering disagree.
        events["$zzz"] = _agent_edit_event("$reply", "$zzz", "PARTIAL EDIT", ts=200)
        events["$aaa"] = _agent_edit_event("$reply", "$aaa", "FINAL EDIT", ts=200)
        assert auditor._latest_agent_body(events, "$reply") == "PARTIAL EDIT"
    finally:
        await client.close()


def _short_body_for(call_id: int) -> str:
    return f"LIVE-FUZZ call={call_id} END call={call_id}"


def _agent_edit_event(reply_event_id: str, event_id: str, body: str, *, ts: int) -> dict[str, Any]:
    """Build an `m.replace` edit whose real streamed body lives in `m.new_content`."""
    return {
        "event_id": event_id,
        "sender": "@agent:example",
        "type": "m.room.message",
        "origin_server_ts": ts,
        "content": {
            "body": f" * {body}",
            "m.new_content": {"body": body, "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": reply_event_id},
        },
    }


def _streaming_oracle() -> ExactReplyOracle:
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(
        client,
        "@agent:example",
        coalescing_threads=True,
        expected_body_for=_short_body_for,
    )
    oracle.expect("op:1", "$source", thread=0)
    oracle._ingest_event({"event_id": "$source", "sender": "@user:example", "type": "m.room.message"})
    return oracle


@pytest.mark.asyncio
async def test_incomplete_streaming_reply_blocks_settlement() -> None:
    """A placeholder body on an observed reply keeps the source unsettled."""
    oracle = _streaming_oracle()
    try:
        placeholder = _agent_reply_event("$source", "$reply", "Thinking...")
        oracle._ingest_event(placeholder)

        # The reply is observed, so the reply-count model alone treats it settled.
        assert oracle.unsettled_required_sources() == []
        # The body gate keeps it open until the stream reaches a terminal body.
        assert oracle.incomplete_streaming_sources() == ["$source"]
    finally:
        await oracle.client.close()


@pytest.mark.asyncio
async def test_completed_streaming_reply_settles_after_edit() -> None:
    """Once the final edit carries the canonical body the source settles."""
    oracle = _streaming_oracle()
    try:
        oracle._ingest_event(_agent_reply_event("$source", "$reply", "Thinking..."))
        assert oracle.incomplete_streaming_sources() == ["$source"]

        oracle._ingest_event(
            _agent_edit_event("$reply", "$edit", _short_body_for(1), ts=200),
        )

        assert oracle.incomplete_streaming_sources() == []
        assert oracle.unsettled_required_sources() == []
    finally:
        await oracle.client.close()


@pytest.mark.asyncio
async def test_interrupted_note_reply_does_not_block_settlement() -> None:
    """A by-design interrupted note is terminal; restart recovery owns its validity."""
    oracle = _streaming_oracle()
    try:
        note_body = f"partial stream {RESTART_INTERRUPTED_RESPONSE_NOTE}"
        oracle._ingest_event(_agent_reply_event("$source", "$reply", note_body))

        assert oracle.incomplete_streaming_sources() == []
    finally:
        await oracle.client.close()


@pytest.mark.asyncio
async def test_late_final_edit_resets_quiet_window() -> None:
    """A late final `m.replace` advances the tracked-response activity clock."""
    oracle = _streaming_oracle()
    try:
        oracle._ingest_event(_agent_reply_event("$source", "$reply", "Thinking..."))
        before = oracle._last_response_activity_at
        oracle._ingest_event(_agent_edit_event("$reply", "$edit", _short_body_for(1), ts=200))

        assert oracle._last_response_activity_at > before
        assert oracle.incomplete_streaming_sources() == []
    finally:
        await oracle.client.close()


@pytest.mark.asyncio
async def test_late_partial_edit_resets_quiet_window_and_keeps_streaming() -> None:
    """A late partial `m.replace` advances the clock but keeps the stream open."""
    oracle = _streaming_oracle()
    try:
        oracle._ingest_event(_agent_reply_event("$source", "$reply", "Thinking..."))
        before = oracle._last_response_activity_at
        oracle._ingest_event(_agent_edit_event("$reply", "$edit", "still streaming", ts=200))

        assert oracle._last_response_activity_at > before
        assert oracle.incomplete_streaming_sources() == ["$source"]
    finally:
        await oracle.client.close()


@pytest.mark.asyncio
async def test_unrelated_agent_message_does_not_reset_quiet_window() -> None:
    """An edit of an untracked target must not extend the tracked-response window."""
    oracle = _streaming_oracle()
    try:
        oracle._ingest_event(_agent_reply_event("$source", "$reply", _short_body_for(1)))
        before = oracle._last_response_activity_at
        # An edit whose target was never tracked as a canonical reply.
        oracle._ingest_event(_agent_edit_event("$unknown", "$stray-edit", "noise", ts=300))

        assert oracle._last_response_activity_at == before
    finally:
        await oracle.client.close()


@pytest.mark.asyncio
async def test_duplicate_edit_delivery_does_not_reset_clock_twice() -> None:
    """A re-delivered, already-seen edit event must not advance the clock again."""
    oracle = _streaming_oracle()
    try:
        oracle._ingest_event(_agent_reply_event("$source", "$reply", "Thinking..."))
        edit = _agent_edit_event("$reply", "$edit", _short_body_for(1), ts=200)
        oracle._ingest_event(edit)
        after_first = oracle._last_response_activity_at
        # The same edit event id arrives a second time via a duplicate sync.
        oracle._ingest_event(edit)

        assert oracle._last_response_activity_at == after_first
    finally:
        await oracle.client.close()


@pytest.mark.asyncio
async def test_duplicate_canonical_reply_inside_edit_window_is_detected() -> None:
    """A second distinct canonical reply is caught even after an edit extends the window."""
    oracle = _streaming_oracle()
    try:
        oracle._ingest_event(_agent_reply_event("$source", "$reply", "Thinking..."))
        oracle._ingest_event(_agent_edit_event("$reply", "$edit", _short_body_for(1), ts=200))
        # A second, distinct canonical reply to the same source is a wrong reply.
        oracle._ingest_event(_agent_reply_event("$source", "$reply-two", _short_body_for(2)))

        with pytest.raises(AssertionError, match="duplicates"):
            oracle._assert_no_wrong_replies()
    finally:
        await oracle.client.close()


@pytest.mark.asyncio
async def test_matrix_quiet_window_repairs_limited_sync_before_advancing_cursor() -> None:
    """A truncated saturation window is backfilled before its cursor advances."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")

    async def limited_sync(_since: str | None, *, timeout_ms: int) -> dict[str, Any]:
        assert timeout_ms > 0
        return {
            "next_batch": "truncated",
            "rooms": {
                "join": {
                    "!room:example": {
                        "timeline": {
                            "limited": True,
                            "events": [
                                {"event_id": "$live-suffix"},
                                {"event_id": "$shared", "view": "sync"},
                            ],
                        },
                    },
                },
            },
        }

    async def paginate_room(room_id: str) -> list[dict[str, Any]]:
        assert room_id == "!room:example"
        return [
            {"event_id": "$recovered"},
            {"event_id": "$shared", "view": "backfill"},
        ]

    client.sync = limited_sync  # type: ignore[method-assign]
    client.paginate_room = paginate_room  # type: ignore[method-assign]
    try:
        await client.wait_until_quiet(deadline_seconds=1.0, quiet_seconds=0.0)
        assert client.next_batch == "truncated"
        assert client.seen_events == {
            "$recovered": {"event_id": "$recovered", "_audit_room_id": "!room:example"},
            "$live-suffix": {"event_id": "$live-suffix", "_audit_room_id": "!room:example"},
            "$shared": {"event_id": "$shared", "view": "backfill", "_audit_room_id": "!room:example"},
        }
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_matrix_limited_sync_does_not_advance_cursor_when_repair_fails() -> None:
    """A failed exact backfill leaves the sync cursor at its last complete token."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")

    async def limited_sync(_since: str | None, *, timeout_ms: int) -> dict[str, Any]:
        assert timeout_ms > 0
        return {
            "next_batch": "truncated",
            "rooms": {
                "join": {
                    "!room:example": {
                        "timeline": {
                            "limited": True,
                            "events": [],
                        },
                    },
                },
            },
        }

    async def paginate_room(_room_id: str) -> list[dict[str, Any]]:
        msg = "backfill failed"
        raise RuntimeError(msg)

    client.sync = limited_sync  # type: ignore[method-assign]
    client.paginate_room = paginate_room  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="backfill failed"):
            await client.sync_incremental(timeout_ms=1)
        assert client.next_batch is None
        assert client.seen_events == {}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_matrix_quiet_window_restarts_after_late_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An event after an initially quiet poll requires a fresh full window."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    clock = 0.0
    calls = 0
    original_monotonic = live_fuzz.time.monotonic

    def monotonic() -> float:
        return clock

    async def sync_incremental(*, timeout_ms: int, allow_limited: bool) -> int:
        nonlocal calls, clock
        assert timeout_ms > 0
        assert allow_limited is False
        calls += 1
        advances = (0.6, 0.2, 0.6, 0.5)
        clock += advances[calls - 1]
        if calls == 2:
            client.seen_events["$late-duplicate"] = {"event_id": "$late-duplicate"}
            return 1
        return 0

    client.sync_incremental = sync_incremental  # type: ignore[method-assign]
    monkeypatch.setattr(live_fuzz.time, "monotonic", monotonic)
    try:
        await client.wait_until_quiet(deadline_seconds=10.0, quiet_seconds=1.0)
    finally:
        monkeypatch.setattr(live_fuzz.time, "monotonic", original_monotonic)
        await client.close()

    assert calls == 4


@pytest.mark.asyncio
async def test_matrix_quiet_window_has_bounded_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A noisy or short-polling server cannot keep the saturation audit alive forever."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    clock = 0.0
    calls = 0
    original_monotonic = live_fuzz.time.monotonic

    def monotonic() -> float:
        return clock

    async def sync_incremental(*, timeout_ms: int, allow_limited: bool) -> int:
        nonlocal calls, clock
        assert 0 < timeout_ms <= 250
        assert allow_limited is False
        calls += 1
        clock += 0.6
        return 0

    client.sync_incremental = sync_incremental  # type: ignore[method-assign]
    monkeypatch.setattr(live_fuzz.time, "monotonic", monotonic)
    try:
        with pytest.raises(TimeoutError, match="did not stay quiet"):
            await client.wait_until_quiet(deadline_seconds=2.0, quiet_seconds=5.0)
    finally:
        monkeypatch.setattr(live_fuzz.time, "monotonic", original_monotonic)
        await client.close()

    assert calls == 4


def _marker_payload(*bodies: str) -> dict[str, Any]:
    """Build a chat-completions payload whose messages carry the given bodies in order."""
    return {"messages": [{"role": "user", "content": body} for body in bodies]}


def test_do_post_records_only_final_user_message_markers() -> None:
    """The observation map reflects only the final user message, never prior history."""
    correct = _source_marker("op:1", ORIGINAL_REVISION)
    stale = _source_marker("op:0", ORIGINAL_REVISION)
    # The correct marker sits in an earlier history turn; the final user turn
    # carries an unrelated marker, so only the final turn's markers are recorded.
    assert _ModelHandler._final_user_markers(_marker_payload(f"history {correct}", f"current {stale}")) == frozenset(
        {stale},
    )
    # A final user turn with no marker records nothing even if history had one.
    assert _ModelHandler._final_user_markers(_marker_payload(f"history {correct}", "current turn")) == frozenset()
    # A single final user turn with the correct marker records it.
    assert _ModelHandler._final_user_markers(_marker_payload(f"only {correct}")) == frozenset({correct})


def test_reversed_model_arrival_preserves_slow_fast_profile() -> None:
    """Slow/fast selection follows the marker fingerprint, not HTTP arrival order."""
    _ModelHandler.reset_observations()
    marker_a = _source_marker("op:1", ORIGINAL_REVISION)
    marker_b = _source_marker("op:2", ORIGINAL_REVISION)
    try:
        _ModelHandler.slow_call_modulus = 3
        # Forward arrival: A is call 1, B is call 2.
        _ModelHandler._record_observation(1, frozenset({marker_a}))
        _ModelHandler._record_observation(2, frozenset({marker_b}))
        forward = {marker_a: _ModelHandler._is_slow_call(1), marker_b: _ModelHandler._is_slow_call(2)}

        # Reversed arrival: the same two markers land under swapped call ids.
        _ModelHandler.reset_observations()
        _ModelHandler._record_observation(1, frozenset({marker_b}))
        _ModelHandler._record_observation(2, frozenset({marker_a}))
        reversed_ = {marker_b: _ModelHandler._is_slow_call(1), marker_a: _ModelHandler._is_slow_call(2)}

        assert forward == reversed_
        assert _parse_markers(f"prefix {marker_a} suffix {marker_b}") == frozenset({marker_a, marker_b})
    finally:
        _ModelHandler.slow_call_modulus = 0
        _ModelHandler.reset_observations()


@pytest.mark.asyncio
async def test_ledger_free_final_audit_binds_direct_reply_to_exact_source_marker() -> None:
    """Wrong, missing, and exact current-turn markers fail, fail, and pass."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    marker_a = _source_marker("op:1", ORIGINAL_REVISION)
    marker_b = _source_marker("op:2", ORIGINAL_REVISION)
    source_content = {"body": f"source {marker_a}", "msgtype": "m.text"}
    source = {
        "event_id": "$source",
        "sender": "@user:example",
        "type": "m.room.message",
        "origin_server_ts": 50,
        "content": source_content,
    }
    reply = _agent_reply_event("$source", "$reply", _short_body_for(7))

    async def paginate_room(_room_id: str) -> list[dict[str, Any]]:
        return [source, reply]

    client.paginate_room = paginate_room  # type: ignore[method-assign]
    oracle.expect("op:1", "$source")
    oracle._ingest_event(source)
    oracle._ingest_event(reply)
    observed = frozenset({marker_b})
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=_short_body_for,
        source_current_markers={"$source": marker_a},
        observed_markers_for=lambda _call_id: observed,
    )
    sent_records = [
        _SentRecord(
            "$source",
            "!room:example",
            "m.room.message",
            sender="@user:example",
            content=source_content,
        ),
    ]
    try:
        for observed_markers in (frozenset({marker_b}), frozenset()):
            observed = observed_markers
            with pytest.raises(AssertionError, match="direct-reply model source audit failed"):
                await auditor.audit(
                    room_ids=("!room:example",),
                    sent_records=sent_records,
                    redacted_targets={},
                )

        observed = frozenset({marker_a})
        result = await auditor.audit(
            room_ids=("!room:example",),
            sent_records=sent_records,
            redacted_targets={},
        )
        assert result["completed_final_bodies"] == 1
    finally:
        await client.close()


def _model_source_auditor(
    *,
    ledger_path: Path,
    expected_sources: dict[str, str],
    source_current_markers: dict[str, str],
    observed: dict[int, frozenset[str]],
) -> FinalStateAuditor:
    """Build an auditor wired to explicit markers and an in-memory observation map."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example", coalescing_threads=True, ledger_path=ledger_path)
    oracle.expected_sources.update(expected_sources)
    return FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=_short_body_for,
        ledger_path=ledger_path,
        source_current_markers=source_current_markers,
        observed_markers_for=lambda call_id: observed.get(call_id, frozenset()),
    )


@pytest.mark.asyncio
async def test_model_source_audit_rejects_response_from_wrong_source(tmp_path: Path) -> None:
    """A reply attached to source A but generated from source B's marker fails."""
    ledger_path = tmp_path / "event_journal.db"
    marker_a = _source_marker("op:1", ORIGINAL_REVISION)
    marker_b = _source_marker("op:2", ORIGINAL_REVISION)
    auditor = _model_source_auditor(
        ledger_path=ledger_path,
        expected_sources={"$a": "op:1", "$b": "op:2"},
        source_current_markers={"$a": marker_a, "$b": marker_b},
        observed={7: frozenset({marker_b})},
    )
    try:
        record = TurnRecord.create(source_event_ids=("$a",), response_event_id="$reply-a", completed=True)
        _write_ledger(ledger_path, {"$a": record})
        events = {"$reply-a": _agent_reply_event("$a", "$reply-a", _short_body_for(7))}
        with pytest.raises(AssertionError, match="without current source markers"):
            auditor._assert_model_saw_current_sources(events)
    finally:
        await auditor.client.close()


@pytest.mark.asyncio
async def test_model_source_audit_rejects_pre_edit_revision(tmp_path: Path) -> None:
    """A response generated from a source's OLD revision fails after a later valid edit."""
    ledger_path = tmp_path / "event_journal.db"
    orig = _source_marker("op:1", ORIGINAL_REVISION)
    edited = _source_marker("op:1", "edit:5")
    auditor = _model_source_auditor(
        ledger_path=ledger_path,
        expected_sources={"$a": "op:1"},
        # The edit revised the source, so its current marker is the edit revision.
        source_current_markers={"$a": edited},
        # The model call only ever observed the pre-edit body.
        observed={4: frozenset({orig})},
    )
    try:
        record = TurnRecord.create(source_event_ids=("$a",), response_event_id="$reply-a", completed=True)
        _write_ledger(ledger_path, {"$a": record})
        events = {"$reply-a": _agent_reply_event("$a", "$reply-a", _short_body_for(4))}
        with pytest.raises(AssertionError, match="without current source markers"):
            auditor._assert_model_saw_current_sources(events)

        # Observing the edited revision instead passes.
        auditor.observed_markers_for = lambda call_id: {4: frozenset({edited})}.get(call_id, frozenset())
        auditor._assert_model_saw_current_sources(events)
    finally:
        await auditor.client.close()


@pytest.mark.asyncio
async def test_model_source_audit_rejects_coalesced_missing_one_source(tmp_path: Path) -> None:
    """A coalesced response missing ONE current source marker fails."""
    ledger_path = tmp_path / "event_journal.db"
    marker_a = _source_marker("op:1", ORIGINAL_REVISION)
    marker_b = _source_marker("op:2", ORIGINAL_REVISION)
    auditor = _model_source_auditor(
        ledger_path=ledger_path,
        expected_sources={"$a": "op:1", "$b": "op:2"},
        source_current_markers={"$a": marker_a, "$b": marker_b},
        # The coalesced call only observed source A's marker.
        observed={9: frozenset({marker_a})},
    )
    try:
        coalesced = TurnRecord.create(source_event_ids=("$a", "$b"), response_event_id="$combined", completed=True)
        _write_ledger(ledger_path, {"$a": coalesced, "$b": coalesced})
        events = {"$combined": _agent_reply_event("$b", "$combined", _short_body_for(9))}
        with pytest.raises(AssertionError, match="without current source markers"):
            auditor._assert_model_saw_current_sources(events)

        # Observing both current markers satisfies the coalesced turn.
        auditor.observed_markers_for = lambda call_id: {9: frozenset({marker_a, marker_b})}.get(call_id, frozenset())
        auditor._assert_model_saw_current_sources(events)
    finally:
        await auditor.client.close()


@pytest.mark.asyncio
async def test_model_source_audit_ignores_no_response_supersession(tmp_path: Path) -> None:
    """A completed no-response turn requires no marker."""
    ledger_path = tmp_path / "event_journal.db"
    marker_a = _source_marker("op:1", ORIGINAL_REVISION)
    auditor = _model_source_auditor(
        ledger_path=ledger_path,
        expected_sources={"$a": "op:1"},
        source_current_markers={"$a": marker_a},
        # No call ever observed this source; the empty map would fail a
        # response-backed record, but a no-response record must not require one.
        observed={},
    )
    try:
        superseded = TurnRecord.create(source_event_ids=("$a",), response_event_id=None, completed=True)
        _write_ledger(ledger_path, {"$a": superseded})
        auditor._assert_model_saw_current_sources({})
    finally:
        await auditor.client.close()


@pytest.mark.asyncio
async def test_model_source_audit_uses_harness_redaction_truth(tmp_path: Path) -> None:
    """Only a harness-observed redaction may waive one source marker.

    Production tombstones a durably redacted source and refuses to regenerate an
    edit against it, so the still-visible response legitimately reflects the
    pre-redaction body. Requiring the source's post-redaction edit marker would
    demand behavior production correctly declines. This is the root:41 live-gate
    false positive.
    """
    ledger_path = tmp_path / "event_journal.db"
    orig = _source_marker("op:1", ORIGINAL_REVISION)
    edited = _source_marker("op:1", "edit:5")
    auditor = _model_source_auditor(
        ledger_path=ledger_path,
        expected_sources={"$a": "op:1"},
        # An edit landed after the redaction, so the current marker is the edit,
        # yet the source is tombstoned and no longer feeds model replay.
        source_current_markers={"$a": edited},
        # The model only ever saw the original body before the redaction.
        observed={4: frozenset({orig})},
    )
    try:
        redacted = TurnRecord.create(
            source_event_ids=("$a",),
            redacted_source_event_ids=("$a",),
            response_event_id="$reply-a",
            completed=True,
        )
        _write_ledger(ledger_path, {"$a": redacted})
        events = {"$reply-a": _agent_reply_event("$a", "$reply-a", _short_body_for(4))}
        with pytest.raises(AssertionError, match="claims unobserved source redactions"):
            auditor._assert_model_saw_current_sources(events)

        auditor._assert_model_saw_current_sources(
            events,
            redacted_source_event_ids={"$a"},
        )

        unrelated = _source_marker("op:unrelated", ORIGINAL_REVISION)
        auditor.observed_markers_for = lambda _call_id: frozenset({orig, unrelated})
        with pytest.raises(AssertionError, match="unexpected source markers"):
            auditor._assert_model_saw_current_sources(
                events,
                redacted_source_event_ids={"$a"},
            )
    finally:
        await auditor.client.close()


def test_ledger_redaction_audit_requires_harness_expected_tombstone() -> None:
    """Harness-observed source redaction must exist in the durable source row."""
    live = TurnRecord.create(
        source_event_ids=("$source",),
        response_event_id="$reply",
        completed=True,
    )
    assert FinalStateAuditor._ledger_redaction_problems(
        {"$source": live},
        {"$source"},
        {"$source"},
    ) == ["harness-redacted source $source has no durable tombstone"]

    tombstoned = TurnRecord.create(
        source_event_ids=("$source",),
        redacted_source_event_ids=("$source",),
        response_event_id="$reply",
        completed=True,
    )
    assert (
        FinalStateAuditor._ledger_redaction_problems(
            {"$source": tombstoned},
            {"$source"},
            {"$source"},
        )
        == []
    )


def test_ledger_redaction_audit_ignores_non_harness_runtime_records() -> None:
    """Runtime-owned stop-reaction tombstones are outside source audit authority."""
    stop_reaction = TurnRecord.create(
        source_event_ids=("$stop-reaction",),
        redacted_source_event_ids=("$stop-reaction",),
        completed=False,
    )
    assert (
        FinalStateAuditor._ledger_redaction_problems(
            {"$stop-reaction": stop_reaction},
            set(),
            {"$source"},
        )
        == []
    )


@pytest.mark.asyncio
async def test_model_source_audit_requires_live_sibling_not_redacted_sibling(tmp_path: Path) -> None:
    """A coalesced record still requires the live sibling's marker but not the redacted one."""
    ledger_path = tmp_path / "event_journal.db"
    marker_a = _source_marker("op:1", ORIGINAL_REVISION)
    marker_b_edit = _source_marker("op:2", "edit:9")
    auditor = _model_source_auditor(
        ledger_path=ledger_path,
        expected_sources={"$a": "op:1", "$b": "op:2"},
        # $b was edited after redaction; its marker must NOT be required, while
        # $a stays a live source whose current marker is mandatory.
        source_current_markers={"$a": marker_a, "$b": marker_b_edit},
        observed={9: frozenset({marker_a})},
    )
    try:
        coalesced = TurnRecord.create(
            source_event_ids=("$a", "$b"),
            redacted_source_event_ids=("$b",),
            response_event_id="$combined",
            completed=True,
        )
        _write_ledger(ledger_path, {"$a": coalesced, "$b": coalesced})
        events = {"$combined": _agent_reply_event("$a", "$combined", _short_body_for(9))}
        # Live sibling $a satisfied, redacted sibling $b excluded -> passes.
        auditor._assert_model_saw_current_sources(
            events,
            redacted_source_event_ids={"$b"},
        )

        # Dropping the live sibling's marker still fails: only the redacted one is excused.
        auditor.observed_markers_for = lambda _call_id: frozenset()
        with pytest.raises(AssertionError, match="without current source markers"):
            auditor._assert_model_saw_current_sources(
                events,
                redacted_source_event_ids={"$b"},
            )
    finally:
        await auditor.client.close()


def _revision_runner() -> LiveFuzzRunner:
    """Bare runner exposing only the source-revision maintenance state."""
    runner = object.__new__(LiveFuzzRunner)
    runner.source_current_markers = {}
    runner.source_revision_markers = defaultdict(dict)
    runner._source_revision_stack = {}
    runner._edit_event_source = {}
    runner.redacted_edit_evidence = {}
    runner.runtime_redaction_path = None
    return runner


def _temporal_revision_runner() -> LiveFuzzRunner:
    """Use real oracle state with only Matrix HTTP replaced by a typed boundary mock."""
    runner = _revision_runner()
    runner.client = Mock(spec=LiveMatrixClient)
    runner.client.user_id = "@user:example"
    runner.client.redact = AsyncMock(return_value="$redaction")
    runner.oracle = ExactReplyOracle(runner.client, "@agent:example", coalescing_threads=True)
    runner.oracle._ledger_observations = runner.oracle._ledger_records
    runner.oracle.expect("root:0", "$root", thread=0)
    runner.source_current_markers["$root"] = _source_marker("root:0", ORIGINAL_REVISION)
    runner._pending_edit_markers = {}
    runner._pending_source_tombstones = set()
    runner.redacted_edit_evidence = {}
    runner.redacted_targets = {}
    runner.sent_records = []
    runner.event_ids = {"root:0": "$root", "op:1": "$a", "op:2": "$b"}
    runner._client_for_operation = lambda _: runner.client  # type: ignore[method-assign]
    runner._room_for_thread = lambda _: "!room:example"  # type: ignore[method-assign]
    return runner


def _add_runtime_historical_evidence(
    auditor: FinalStateAuditor,
    tmp_path: Path,
    failure: str,
    observed: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Install exact runtime-entry and model observations for historical boundary controls."""
    auditor.redacted_edit_evidence["$a"] = replace(
        auditor.redacted_edit_evidence["$a"],
        observed_call_ids=frozenset(),
    )
    auditor.runtime_redaction_path = tmp_path / "runtime-redactions.jsonl"
    entry = {
        "agent_name": "general",
        "principal_id": "general@@agent:example",
        "target_event_id": "$a",
        "monotonic_ns": 200,
        "already_redacted": failure == "runtime_preexisting",
    }
    if failure == "runtime_wrong_owner":
        entry["agent_name"] = "router"
        entry["principal_id"] = "router@@router:example"
    if failure == "runtime_wrong_target":
        entry["target_event_id"] = "$other"
    entries = [entry]
    if failure in {"runtime_duplicate", "runtime_restart"}:
        entries.append({**entry, "monotonic_ns": 400, "already_redacted": True})
    if failure != "runtime_missing":
        auditor.runtime_redaction_path.write_text(
            "".join(json.dumps(item) + "\n" for item in entries),
            encoding="utf-8",
        )
    observed_at = 100 if failure in {"runtime_before", "runtime_duplicate"} else 300
    if failure == "runtime_tie":
        observed_at = 200
    monkeypatch.setattr(live_fuzz.time, "monotonic_ns", lambda: observed_at)
    _ModelHandler.reset_observations()
    _ModelHandler._record_observation(7, frozenset(observed))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        None,
        "source",
        "edit",
        "call",
        "response",
        "missing",
        "registered",
        "selected_only",
        "physical_tombstone",
        "uncompleted",
        "later_live",
        "newer_live",
        "pending",
        "missing_sibling",
        "ledger_source",
        "unredacted",
        "both_markers",
        "runtime_before",
        "runtime_after",
        "runtime_tie",
        "runtime_duplicate",
        "runtime_restart",
        "runtime_preexisting",
        "runtime_wrong_owner",
        "runtime_wrong_target",
        "runtime_missing",
    ],
)
async def test_redacted_edit_preserves_completed_historical_answer(
    tmp_path: Path,
    failure: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the exact terminal consumed edit and frozen visible generation may survive rollback."""
    marker = _source_marker("root:0", "edit:1")
    sibling = _source_marker("op:9", ORIGINAL_REVISION)
    observed = {marker} if failure == "missing_sibling" else {marker, sibling}
    if failure == "both_markers":
        observed.add(_source_marker("root:0", ORIGINAL_REVISION))
    auditor = _model_source_auditor(
        ledger_path=tmp_path / "event_journal.db",
        expected_sources={"$root": "root:0", "$sibling": "op:9"},
        source_current_markers={"$root": _source_marker("root:0", ORIGINAL_REVISION), "$sibling": sibling},
        observed={7: frozenset(observed)},
    )
    auditor.source_revision_markers = {"$root": {"$a": marker}}
    auditor.redacted_edit_evidence = {
        "$a": live_fuzz.RedactedEditEvidence(
            "$wrong" if failure == "source" else "$root",
            "$wrong" if failure == "edit" else "$a",
            marker,
            frozenset({2} if failure == "call" else {7}),
            frozenset({"$a", "$b"} if failure == "newer_live" else {"$a"}),
        ),
    }
    if failure is not None and failure.startswith("runtime_"):
        _add_runtime_historical_evidence(auditor, tmp_path, failure, observed, monkeypatch)
    revision = RevisionReplay(
        "$wrong" if failure == "ledger_source" else "$root",
        100,
        redacted=True,
        response_event_id="$wrong" if failure == "response" else "$reply",
    )
    if failure == "registered":
        revision = replace(revision, response_event_id=None)
    record = TurnRecord.create(
        source_event_ids=("$root", "$sibling"),
        response_event_id="$reply",
        completed=failure != "uncompleted",
        revision_replay={} if failure in {"missing", "selected_only", "physical_tombstone"} else {"$a": revision},
        source_event_revisions={"$root": (100, "$a")} if failure == "selected_only" else {"$root": (0, "$root")},
    )
    events = {
        "$reply": _agent_reply_event("$root", "$reply", _short_body_for(7)),
        "$a": {"event_id": "$a", "origin_server_ts": 100},
    }
    if failure in {"later_live", "newer_live"}:
        auditor.source_revision_markers["$root"]["$b"] = _source_marker("root:0", "edit:2")
        events["$b"] = {"event_id": "$b", "origin_server_ts": 50 if failure == "later_live" else 200}
    if failure == "pending":
        auditor.pending_edit_markers = {"$root": {"$b": _source_marker("root:0", "edit:2")}}
    records = {"$root": record}
    if failure == "physical_tombstone":
        records["$a"] = TurnRecord.create(
            source_event_ids=("$a",),
            completed=False,
            redacted_source_event_ids=("$a",),
        )
    try:
        if failure in {None, "runtime_before", "runtime_duplicate"}:
            auditor._assert_model_saw_current_sources(
                events,
                records=records,
                redacted_targets={"$a": "$redaction"},
            )
        else:
            with pytest.raises(AssertionError, match="model source-revision audit"):
                auditor._assert_model_saw_current_sources(
                    events,
                    records=records,
                    redacted_targets={} if failure == "unredacted" else {"$a": "$redaction"},
                )
    finally:
        await auditor.client.close()
        _ModelHandler.reset_observations()


@pytest.mark.asyncio
async def test_redacted_edit_creates_no_original_regeneration_debt() -> None:
    """Removing an edit restores content without ordering another historical answer."""
    runner = _temporal_revision_runner()
    marker = _source_marker("root:0", "edit:1")
    runner._push_source_revision("$root", "$a", marker)
    runner._edit_event_source["$a"] = "$root"
    await runner._apply_redaction(LiveOperation(3, LiveOperationKind.REDACTION, 0, "op:1"))
    assert not runner._pending_edit_markers
    assert runner._pending_source_tombstones == {"$a"}


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", ["$a", "$b"])
async def test_redaction_preserves_unresolved_surviving_edit_debts(deleted: str) -> None:
    """A newer unconsumed edit and its deletion cannot erase an older live obligation."""
    runner = _temporal_revision_runner()
    markers = {"$a": _source_marker("root:0", "edit:1"), "$b": _source_marker("root:0", "edit:2")}
    for edit, marker in markers.items():
        runner._push_source_revision("$root", edit, marker)
        runner._edit_event_source[edit] = "$root"
    runner._pending_edit_markers["$root"] = dict(markers)
    await runner._apply_redaction(
        LiveOperation(3, LiveOperationKind.REDACTION, 0, "op:1" if deleted == "$a" else "op:2"),
    )
    surviving = "$b" if deleted == "$a" else "$a"
    assert runner._pending_edit_markers == {"$root": {surviving: markers[surviving]}}


@pytest.mark.asyncio
async def test_redacted_edit_snapshot_does_not_expand(monkeypatch: pytest.MonkeyPatch) -> None:
    """Duplicate deletion must retain exact observed IDs, even when later IDs are numerically lower."""
    runner = _temporal_revision_runner()
    marker = _source_marker("root:0", "edit:1")
    runner._push_source_revision("$root", "$a", marker)
    runner._edit_event_source["$a"] = "$root"
    observations = {9: [marker], 6: [_source_marker("root:9", ORIGINAL_REVISION)]}
    monkeypatch.setattr(_ModelHandler, "observations_snapshot", lambda: dict(observations))
    operation = LiveOperation(3, LiveOperationKind.REDACTION, 0, "op:1")
    await runner._apply_redaction(operation)
    evidence = runner.redacted_edit_evidence["$a"]
    observations[2] = [marker]
    runner.source_revision_markers["$root"]["$b"] = _source_marker("root:0", "edit:2")
    await runner._apply_redaction(operation)
    assert runner.redacted_edit_evidence["$a"] is evidence
    assert evidence.observed_call_ids == {9}
    assert evidence.known_edit_event_ids == {"$a"}


@pytest.mark.asyncio
@pytest.mark.parametrize("attributed", [False, True])
async def test_redacted_edit_request_records_terminal_response_after_deletion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    attributed: bool,
) -> None:
    """Pre-deletion observation can finish later, but registration and selected replay cannot prove it."""
    runner = _temporal_revision_runner()
    marker = _source_marker("root:0", "edit:1")
    runner._push_source_revision("$root", "$a", marker)
    runner._edit_event_source["$a"] = "$root"
    registered = TurnRecord.create(
        source_event_ids=("$root",),
        completed=False,
        revision_replay={"$a": RevisionReplay("$root", 100)},
    )
    runner.oracle._ledger_records["$root"] = registered
    monkeypatch.setattr(_ModelHandler, "observations_snapshot", lambda: {7: [marker]})
    await runner._apply_redaction(LiveOperation(3, LiveOperationKind.REDACTION, 0, "op:1"))
    completed = replace(
        registered,
        completed=True,
        response_event_id="$reply",
        source_event_revisions={"$root": (0, "$root")},
        revision_replay={
            "$a": RevisionReplay("$root", 100, redacted=True, response_event_id="$reply" if attributed else None),
        },
    )
    auditor = _model_source_auditor(
        ledger_path=tmp_path / "event_journal.db",
        expected_sources={"$root": "root:0"},
        source_current_markers=runner.source_current_markers,
        observed={7: frozenset({marker})},
    )
    auditor.source_revision_markers = runner.source_revision_markers
    auditor.redacted_edit_evidence = runner.redacted_edit_evidence
    events = {
        "$a": {"event_id": "$a", "origin_server_ts": 100},
        "$reply": _agent_reply_event("$root", "$reply", _short_body_for(7)),
    }
    try:
        if attributed:
            auditor._assert_model_saw_current_sources(
                events,
                records={"$root": completed},
                redacted_targets=runner.redacted_targets,
            )
        else:
            with pytest.raises(AssertionError, match="model source-revision audit"):
                auditor._assert_model_saw_current_sources(
                    events,
                    records={"$root": completed},
                    redacted_targets=runner.redacted_targets,
                )
    finally:
        await auditor.client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["completed", "registered", "older", "unobserved_order"])
async def test_completed_newer_edit_supersedes_only_proven_older_debts(
    monkeypatch: pytest.MonkeyPatch,
    proof: str,
) -> None:
    """Canonical completed consumption supersedes older edits; send order and registration do not."""
    runner = _temporal_revision_runner()
    markers = {"$a": _source_marker("root:0", "edit:1"), "$b": _source_marker("root:0", "edit:2")}
    for edit, marker in reversed(tuple(markers.items())):
        runner._push_source_revision("$root", edit, marker)
        runner._edit_event_source[edit] = "$root"
    runner.oracle.event_summaries = {
        "$a": {"origin_server_ts": 100},
        "$b": {"origin_server_ts": 50 if proof == "older" else 200},
    }
    if proof == "unobserved_order":
        del runner.oracle.event_summaries["$a"]
    runner._pending_edit_markers = {"$root": dict(markers)}
    assert runner._current_pending_edit_marker("$root") == markers["$a" if proof == "older" else "$b"]
    runner.oracle._ledger_records["$root"] = TurnRecord.create(
        source_event_ids=("$root",),
        response_event_id="$reply",
        revision_replay={
            "$b": RevisionReplay("$root", 200, response_event_id=None if proof == "registered" else "$reply"),
        },
    )
    runner.oracle.latest_reply_bodies["$reply"] = ((0, 0, "$reply"), _short_body_for(7))
    monkeypatch.setattr(_ModelHandler, "observed_markers_for", lambda _: frozenset({markers["$b"]}))
    await runner._apply_redaction(LiveOperation(3, LiveOperationKind.REDACTION, 0, "op:2"))
    assert runner._pending_edit_markers == ({} if proof == "completed" else {"$root": {"$a": markers["$a"]}})
    assert runner._pending_source_tombstones == {"$b"}


@pytest.mark.asyncio
async def test_final_source_revision_uses_matrix_order_not_completion_order() -> None:
    """Concurrent edit completion order cannot change the canonical source body."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    oracle.expect("root:0", "$root")
    first = _source_marker("root:0", "edit:1")
    second = _source_marker("root:0", "edit:2")
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=_short_body_for,
        source_revision_markers={"$root": {"$edit-a": first, "$edit-z": second}},
    )
    events = {
        "$edit-a": {"event_id": "$edit-a", "origin_server_ts": 100},
        "$edit-z": {"event_id": "$edit-z", "origin_server_ts": 100},
    }
    try:
        auditor._resolve_source_revision_markers(events, {})
        assert auditor.source_current_markers["$root"] == second

        auditor._resolve_source_revision_markers(events, {"$edit-z": "$redaction"})
        assert auditor.source_current_markers["$root"] == first
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("later_authored", [False, True])
@pytest.mark.parametrize("proof", ["none", "http", "runtime"])
async def test_redacted_edit_late_completion_supersedes_only_preexisting_debt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    later_authored: bool,
    proof: str,
) -> None:
    """A frozen request finishing after deletion can cover older input, never an unseen later edit."""
    runner = _temporal_revision_runner()
    first = _source_marker("root:0", "edit:1")
    second = _source_marker("root:0", "edit:2")
    if not later_authored:
        runner._push_source_revision("$root", "$a", first)
    runner._push_source_revision("$root", "$b", second)
    runner._edit_event_source["$b"] = "$root"
    monkeypatch.setattr(_ModelHandler, "observations_snapshot", lambda: {7: [second]} if proof == "http" else {})
    monkeypatch.setattr(_ModelHandler, "observed_markers_for", lambda _: frozenset({second}))
    await runner._apply_redaction(LiveOperation(3, LiveOperationKind.REDACTION, 0, "op:2"))
    if proof == "runtime":
        runner.runtime_redaction_path = tmp_path / "runtime-redactions.jsonl"
        runner.runtime_redaction_path.write_text(
            json.dumps(
                {
                    "agent_name": "general",
                    "principal_id": "general@@agent:example",
                    "target_event_id": "$b",
                    "monotonic_ns": 200,
                    "already_redacted": False,
                },
            )
            + "\n",
        )
        monkeypatch.setattr(live_fuzz.time, "monotonic_ns", lambda: 100)
        _ModelHandler._record_observation(7, frozenset({second}))
    if later_authored:
        runner._push_source_revision("$root", "$a", first)
    runner._pending_edit_markers = {"$root": {"$a": first}}
    runner.oracle.event_summaries = {"$a": {"origin_server_ts": 100}, "$b": {"origin_server_ts": 200}}
    runner.oracle._ledger_records["$root"] = TurnRecord.create(
        source_event_ids=("$root",),
        response_event_id="$reply",
        revision_replay={"$b": RevisionReplay("$root", 200, redacted=True, response_event_id="$reply")},
    )
    runner.oracle.latest_reply_bodies["$reply"] = ((0, 0, "$reply"), _short_body_for(7))
    runner._reconcile_edit_debts()
    assert runner._pending_edit_markers == ({"$root": {"$a": first}} if later_authored or proof == "none" else {})
    assert runner.redacted_edit_evidence["$b"].known_edit_event_ids == (
        frozenset({"$b"}) if later_authored else frozenset({"$a", "$b"})
    )
    _ModelHandler.reset_observations()


@pytest.mark.asyncio
async def test_redacted_edit_failure_never_creates_historical_allowance() -> None:
    """Rejected HTTP redaction leaves no frozen proof, tombstone debt, or authored redaction."""
    runner = _temporal_revision_runner()
    runner._push_source_revision("$root", "$a", _source_marker("root:0", "edit:1"))
    runner.client.redact = AsyncMock(side_effect=RuntimeError("redaction rejected"))
    with pytest.raises(RuntimeError, match="redaction rejected"):
        await runner._apply_redaction(LiveOperation(3, LiveOperationKind.REDACTION, 0, "op:1"))
    assert runner.redacted_edit_evidence == {}
    assert runner.redacted_targets == {}
    assert runner._pending_source_tombstones == set()


@pytest.mark.asyncio
async def test_redacted_edit_records_snapshot_before_landed_callback_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation after landed accounting cannot erase the exact successful redaction snapshot."""
    runner = _temporal_revision_runner()
    marker = _source_marker("root:0", "edit:1")
    runner._push_source_revision("$root", "$a", marker)
    runner._edit_event_source["$a"] = "$root"
    monkeypatch.setattr(_ModelHandler, "observations_snapshot", lambda: {7: [marker]})

    def landed(_result: tuple[LiveOperation, str | None, _SentPayload | None]) -> None:
        assert runner.redacted_edit_evidence["$a"].observed_call_ids == {7}
        assert runner._pending_source_tombstones == {"$a"}
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await runner._apply_redaction(LiveOperation(3, LiveOperationKind.REDACTION, 0, "op:1"), on_landed=landed)
    assert runner.redacted_targets == {"$a": "$redaction"}
    assert len(runner.sent_records) == 1


def test_redacting_edit_event_reverts_source_marker_to_original() -> None:
    """Redacting an ``m.replace`` restores the source's pre-edit current marker.

    This is the root:44 live-gate false positive: the edit event itself, not the
    source, was redacted, so Matrix reverts the source body to ``orig`` and the
    model correctly ends there. The oracle's expected marker must follow.
    """
    runner = _revision_runner()
    orig = _source_marker("root:44", ORIGINAL_REVISION)
    edited = _source_marker("root:44", "edit:13")
    runner.source_current_markers["$root"] = orig

    runner._push_source_revision("$root", "$edit", edited)
    runner._edit_event_source["$edit"] = "$root"
    assert runner.source_current_markers["$root"] == edited

    runner._pop_source_revision("$root", "$edit")
    assert runner.source_current_markers["$root"] == orig
    # A duplicate redaction of the same edit must not pop an unrelated revision.
    assert "$edit" not in runner._edit_event_source
    runner._pop_source_revision("$root", "$edit")
    assert runner.source_current_markers["$root"] == orig


def test_redacting_latest_edit_reverts_to_prior_surviving_edit() -> None:
    """With chained edits, redacting the newest reverts to the previous edit body."""
    runner = _revision_runner()
    orig = _source_marker("root:5", ORIGINAL_REVISION)
    first = _source_marker("root:5", "edit:3")
    second = _source_marker("root:5", "edit:8")
    runner.source_current_markers["$root"] = orig

    runner._push_source_revision("$root", "$e1", first)
    runner._edit_event_source["$e1"] = "$root"
    runner._push_source_revision("$root", "$e2", second)
    runner._edit_event_source["$e2"] = "$root"
    assert runner.source_current_markers["$root"] == second

    # Redacting the newest edit falls back to the earlier surviving edit body.
    runner._pop_source_revision("$root", "$e2")
    assert runner.source_current_markers["$root"] == first
    # Redacting the remaining edit falls back to the original body.
    runner._pop_source_revision("$root", "$e1")
    assert runner.source_current_markers["$root"] == orig


def test_redacting_non_newest_edit_keeps_newer_surviving_revision() -> None:
    """Redacting a middle edit removes only its revision, not the surviving top.

    Codex #6: the revision stack popped its top unconditionally, so redacting an
    older edit while a newer one still survived reverted the source to the wrong
    (older) body. The redacted edit's entry must be removed by identity, leaving
    the newest surviving revision current.
    """
    runner = _revision_runner()
    orig = _source_marker("root:9", ORIGINAL_REVISION)
    first = _source_marker("root:9", "edit:1")
    second = _source_marker("root:9", "edit:2")
    runner.source_current_markers["$root"] = orig

    runner._push_source_revision("$root", "$e1", first)
    runner._edit_event_source["$e1"] = "$root"
    runner._push_source_revision("$root", "$e2", second)
    runner._edit_event_source["$e2"] = "$root"
    assert runner.source_current_markers["$root"] == second

    # Redacting the older edit leaves the newer edit as the surviving body.
    runner._pop_source_revision("$root", "$e1")
    assert runner.source_current_markers["$root"] == second
    # Redacting the newer edit now falls all the way back to the original.
    runner._pop_source_revision("$root", "$e2")
    assert runner.source_current_markers["$root"] == orig


@pytest.mark.asyncio
async def test_edit_registers_latest_marker_as_pending_checkpoint_effect() -> None:
    """A landed edit remains owed until its response regeneration is visible."""
    runner = _revision_runner()
    runner.source_current_markers["$source"] = _source_marker("op:1", ORIGINAL_REVISION)
    runner._pending_edit_markers = {}
    runner._pending_source_tombstones = set()
    runner.sent_records = []
    runner.stack = SimpleNamespace(agent_id="@agent:example")
    runner._resolve_target = lambda _logical_ref: asyncio.sleep(0, result="$source")  # type: ignore[method-assign]
    runner._room_for_thread = lambda _thread: "!room:example"  # type: ignore[method-assign]

    class EditClient:
        user_id = "@user:example"

        @staticmethod
        async def send_event(
            event_type: str,
            _txn_id: str,
            content: dict[str, Any],
            *,
            room_id: str,
        ) -> str:
            assert event_type == "m.room.message"
            assert content["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$source"}
            assert room_id == "!room:example"
            return "$edit"

    runner._client_for_operation = lambda _operation: EditClient()  # type: ignore[method-assign]
    operation = LiveOperation(7, LiveOperationKind.EDIT, 0, "op:1")

    await runner._apply(operation)

    marker = _source_marker("op:1", "edit:7")
    assert runner._pending_edit_markers == {"$source": {"$edit": marker}}
    assert runner.source_current_markers["$source"] == marker


class _FakeStack:
    """Minimal stand-in exposing the fields the failure-bundle path reads.

    Records teardown ordering so tests can assert MindRoom stops before the
    Tuwunel log is captured and before evidence is copied.
    """

    def __init__(self, storage_path: Path, log_path: Path, *, tuwunel_log: str = "tuwunel line\n") -> None:
        self.storage_path = storage_path
        self.runtime_redaction_path = storage_path.parent / "runtime-redactions.jsonl"
        self.log_path = log_path
        self._tuwunel_log = tuwunel_log
        self.events: list[str] = []

    def log_tail(self, lines: int = 80) -> str:  # noqa: ARG002 - mirror the real stack signature
        return "tail\n"

    def stop_mindroom(self) -> None:
        self.events.append("stop_mindroom")

    def diagnostic_counts(self) -> dict[str, int]:
        self.events.append("diagnostics")
        return {"event_loop_stalls": 2, "sync_restart_retries": 1}

    def tuwunel_log(self, *, tail: int = 4000) -> str:  # noqa: ARG002 - mirror the real stack signature
        self.events.append("tuwunel_log")
        return self._tuwunel_log


def _bundle_scenario() -> LiveFuzzScenario:
    """A tiny valid scenario used as durable logical evidence."""
    return LiveFuzzScenario(
        thread_count=1,
        batches=((LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, None),),),
    )


def _prepared_stack(tmp_path: Path, *, log_text: str = "mindroom line\n") -> _FakeStack:
    """Create a fake stack whose log and ledger already hold copyable evidence."""
    storage = tmp_path / "mindroom_data"
    (storage / "tracking").mkdir(parents=True)
    ledger = storage / "tracking" / "event_journal.db"
    _write_ledger(ledger, {})
    log_path = tmp_path / "mindroom.log"
    log_path.write_text(log_text, encoding="utf-8")
    return _FakeStack(storage, log_path)


def _snapshot_oracle() -> ExactReplyOracle:
    """A settled-then-replied oracle whose snapshot carries diagnosable state."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    oracle.next_batch = "s_secret_sync_token"
    oracle.expect("op:1", "$source")
    oracle._ingest_event(
        {
            "event_id": "$response",
            "sender": "@agent:example",
            "type": "m.room.message",
            "content": {
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$source",
                    "m.in_reply_to": {"event_id": "$source"},
                },
            },
        },
    )
    return oracle


def _bundle_runner(oracle: ExactReplyOracle) -> LiveFuzzRunner:
    """Initialize the exact source-matching state captured with failure evidence."""
    runner = _revision_runner()
    runner.oracle = oracle
    runner._pending_edit_markers = {}
    runner.redacted_targets = {}
    return runner


@pytest.mark.asyncio
async def test_failure_bundle_persists_evidence_and_survives_teardown(tmp_path: Path) -> None:
    """A run failure leaves a complete bundle after the stack is destroyed."""
    _ModelHandler.reset_observations()
    _ModelHandler._record_observation(3, frozenset({_source_marker("op:1", ORIGINAL_REVISION)}))
    artifact_root = tmp_path / "artifacts"
    scenario = _bundle_scenario()
    bundle = FailureBundle.create(artifact_root, "run-1", scenario=scenario, provenance={"mindroom_head": "abc123"})
    bundle.record_realized({"sequence": 1, "event_ref": "op:0", "event_id": "$sent"})

    stack = _prepared_stack(tmp_path)
    stack.runtime_redaction_path.write_text('{"entry": "generation one"}\n{"entry": "generation two"}\n')
    timing = _ModelHandler.timed_observations_snapshot()[3]
    oracle = _snapshot_oracle()
    runner = _bundle_runner(oracle)

    try:
        _persist_failure_bundle(bundle, stack, runner, AssertionError("reply invariant failed"))
    finally:
        await oracle.client.close()
        _ModelHandler.reset_observations()

    # Teardown deletes the stack's temp storage; the bundle must not point into it.
    assert _ModelHandler.observations_snapshot() == {}
    shutil.rmtree(tmp_path / "mindroom_data")

    directory = bundle.directory
    assert directory.exists()
    assert (directory / "scenario.json").read_text(encoding="utf-8").strip() == scenario.to_json()
    assert (directory / "provenance.json").exists()
    journal_lines = (directory / "realized_journal.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(journal_lines[0])["event_id"] == "$sent"
    assert "mindroom line" in (directory / "mindroom.log").read_text(encoding="utf-8")
    assert (directory / "handled_turns.json").exists()
    assert json.loads((directory / "nio_recovery.json").read_text(encoding="utf-8")) == {"stores": {}}
    observations = json.loads((directory / "model_observations.json").read_text(encoding="utf-8"))
    assert observations["3"] == [_source_marker("op:1", ORIGINAL_REVISION)]
    saved_timing = json.loads((directory / "model_timing_observations.json").read_text())
    assert saved_timing["3"] == {"markers": sorted(timing.markers), "monotonic_ns": timing.monotonic_ns}
    assert (directory / "runtime-redactions.jsonl").read_text() == stack.runtime_redaction_path.read_text()
    assert json.loads((directory / "source_matching.json").read_text())["redacted_edit_evidence"] == {}
    diagnostics = json.loads((directory / "diagnostics.json").read_text(encoding="utf-8"))
    assert diagnostics["event_loop_stalls"] == 2
    assert "tuwunel line" in (directory / "tuwunel.log").read_text(encoding="utf-8")
    exception_text = (directory / "exception.txt").read_text(encoding="utf-8")
    assert "reply invariant failed" in exception_text
    assert stack.events.index("stop_mindroom") < stack.events.index("tuwunel_log")


def test_failure_bundle_records_realized_completion_order(tmp_path: Path) -> None:
    """Out-of-order concurrent completions appear in true completion order."""
    bundle = FailureBundle.create(
        tmp_path / "artifacts",
        "run-2",
        scenario=_bundle_scenario(),
        provenance={},
    )
    runner = object.__new__(LiveFuzzRunner)
    runner.operation_count = 0
    runner._realized_sequence = 0
    runner.event_ids = {}
    runner.sent_payloads = {}
    runner._mindroom_running = True
    runner._journal = bundle.record_realized

    # A concurrent batch resolves with thread 2 finishing before thread 0.
    results = [
        (LiveOperation(7, LiveOperationKind.THREAD_MESSAGE, 2, None, client=1), "$late-thread", None),
        (LiveOperation(3, LiveOperationKind.THREAD_MESSAGE, 0, None, client=0), "$early-thread", None),
    ]
    runner._record_batch_results(results)

    journal = [
        json.loads(line)
        for line in (bundle.directory / "realized_journal.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [entry["event_id"] for entry in journal] == ["$late-thread", "$early-thread"]
    assert [entry["thread"] for entry in journal] == [2, 0]
    assert [entry["sequence"] for entry in journal] == [1, 2]


def test_successful_run_discards_its_failure_bundle(tmp_path: Path) -> None:
    """Codex #7: a passing run leaves no failure bundle behind.

    The bundle directory is created before the run so a mid-startup kill still
    leaves a manifest, but a run that passes must remove it rather than
    accumulate a stale scenario/provenance/journal per successful run.
    """
    root = tmp_path / "artifacts"
    bundle = FailureBundle.create(root, "run-ok", scenario=_bundle_scenario(), provenance={})
    assert bundle.directory.exists()

    bundle.discard()

    assert not bundle.directory.exists()
    # A second discard is a no-op, not an error, so success cleanup is idempotent.
    bundle.discard()


def test_child_provenance_rejects_nested_mindroom_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path below the runner root must still belong to the runner's Git checkout."""
    project_root = tmp_path / "mindroom"
    nested_root = project_root / "nested"
    monkeypatch.setattr(live_fuzz, "PROJECT_ROOT", project_root)
    attestation = tmp_path / "runtime-attestation.json"
    attestation.write_text(
        json.dumps(
            {
                "mindroom_module_path": str(nested_root / "src" / "mindroom" / "__init__.py"),
                "nio_module_path": str(tmp_path / "nio" / "__init__.py"),
            },
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("scripts.testing.fuzz_live_matrix._git_root_for_path", lambda _path: nested_root)

    with pytest.raises(RuntimeError, match="nested or different Git checkout"):
        _validated_child_provenance(
            attestation,
            expected_mindroom_revision="mindroom-head",
        )


def test_child_provenance_rejects_same_head_from_other_mindroom_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commit equality cannot substitute for the live runner's MindRoom checkout."""
    requested = tmp_path / "requested-mindroom"
    other = tmp_path / "other-mindroom"
    monkeypatch.setattr(live_fuzz, "PROJECT_ROOT", requested)
    attestation = tmp_path / "runtime-attestation.json"
    attestation.write_text(
        json.dumps(
            {
                "mindroom_module_path": str(other / "src" / "mindroom" / "__init__.py"),
                "nio_module_path": str(tmp_path / "nio" / "__init__.py"),
            },
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="outside the live runner checkout"):
        _validated_child_provenance(
            attestation,
            expected_mindroom_revision="same-head",
        )


def test_child_provenance_rejects_pythonpath_checkout_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PYTHONPATH cannot substitute another checkout even at the same commit."""
    _test_runtime_attestation(tmp_path, monkeypatch)
    project_root = tmp_path / "mindroom"
    monkeypatch.setattr(live_fuzz, "PROJECT_ROOT", project_root)
    other = tmp_path / "other-checkout"
    attestation = tmp_path / "runtime-attestation.json"
    attestation.write_text(
        json.dumps(
            {
                "mindroom_module_path": str(project_root / "__init__.py"),
                "nio_module_path": str(other / "src" / "nio" / "__init__.py"),
            },
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "scripts.testing.fuzz_live_matrix._git_state_for_file",
        lambda *_args, **_kwargs: ("same-head", False),
    )
    monkeypatch.setattr(
        "scripts.testing.fuzz_live_matrix._git_revision",
        lambda _path: "same-head",
    )
    monkeypatch.setattr(
        "scripts.testing.fuzz_live_matrix._git_root_for_path",
        lambda path: project_root if "mindroom" in str(path) else other,
    )
    monkeypatch.setenv("PYTHONPATH", str(other / "src"))

    with pytest.raises(RuntimeError, match="outside the locked installed distribution"):
        _validated_child_provenance(
            attestation,
            expected_mindroom_revision="same-head",
        )


def test_start_mindroom_uses_locked_installation_and_persists_each_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child uses its locked environment and persists each process identity."""
    stack = ManagedTuwunelStack(
        state_root=tmp_path / "state",
    )
    commands: list[list[str]] = []
    manifests: list[dict[str, object]] = []

    class FakeProcess:
        pid = 4242

        def poll(self) -> None:
            return None

    def popen(command: list[str], **_kwargs: object) -> FakeProcess:
        commands.append(command)
        return FakeProcess()

    try:
        stack._log_handle = io.StringIO()
        stack.api_port = 18765
        stack._env = {}
        stack.storage_path.mkdir(parents=True)
        (stack.storage_path / "matrix_state.yaml").write_text(
            json.dumps({"rooms": {"lobby": {"room_id": "!room:example"}}}),
            encoding="utf-8",
        )
        stack._wait_for_runtime_attestation = lambda: None  # type: ignore[method-assign]
        stack._wait_for_url = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        stack._write_manifest = lambda **kwargs: manifests.append(kwargs)  # type: ignore[method-assign]
        monkeypatch.setattr("scripts.testing.fuzz_live_matrix.subprocess.Popen", popen)

        stack._start_mindroom()

        assert commands
        assert commands[0][:5] == ["uv", "run", "--locked", "--python", "3.13"]
        assert "__mindroom_runtime_child__" in commands[0]
        assert manifests == [
            {"state": "starting_mindroom", "mindroom_pid": 4242},
            {"state": "ready", "mindroom_pid": 4242},
        ]
    finally:
        stack.temp_dir.cleanup()


@pytest.mark.asyncio
async def test_run_live_closes_every_client_without_masking_primary_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Client-close failures annotate, but never replace, the fuzz failure."""
    closed: list[int] = []
    clients: list[object] = []
    primary = ValueError("primary fuzz failure")
    interruption = KeyboardInterrupt("close interrupted")

    class FakeClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.index = len(clients)
            clients.append(self)

        async def close(self) -> None:
            closed.append(self.index)
            if self.index == 0:
                raise interruption
            if self.index == 1:
                message = "close failed"
                raise RuntimeError(message)

    class FakeRunner:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def run(self) -> dict[str, object]:
            raise primary

    monkeypatch.setattr(live_fuzz, "LiveMatrixClient", FakeClient)
    monkeypatch.setattr(live_fuzz, "LiveFuzzRunner", FakeRunner)
    stack: Any = SimpleNamespace(
        homeserver="http://matrix.invalid",
        room_ids={"lobby": "!room:example"},
        room_keys=("lobby",),
        room_id="!room:example",
    )
    scenario = LiveFuzzScenario(thread_count=1, client_count=3, batches=())

    with pytest.raises(ValueError, match="primary fuzz failure") as raised:
        await live_fuzz._run_live(
            stack,
            scenario,
            reply_timeout=1,
            settle_seconds=0,
        )

    assert raised.value is primary
    assert closed == [0, 1, 2]
    assert any("Matrix client cleanup failures" in note for note in primary.__notes__)
    assert any("KeyboardInterrupt: close interrupted" in note for note in primary.__notes__)
    assert any("RuntimeError: close failed" in note for note in primary.__notes__)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interruption",
    [KeyboardInterrupt("close interrupted"), SystemExit("close exited")],
)
async def test_run_live_finishes_client_cleanup_before_rethrowing_first_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    interruption: BaseException,
) -> None:
    """A passing workload rethrows its exact cleanup interrupt after every close."""
    closed: list[int] = []
    clients: list[object] = []

    class FakeClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.index = len(clients)
            clients.append(self)

        async def close(self) -> None:
            closed.append(self.index)
            if self.index == 0:
                raise interruption
            if self.index == 1:
                message = "secondary close failed"
                raise RuntimeError(message)

    class FakeRunner:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def run(self) -> dict[str, object]:
            return {"status": "PASS"}

    monkeypatch.setattr(live_fuzz, "LiveMatrixClient", FakeClient)
    monkeypatch.setattr(live_fuzz, "LiveFuzzRunner", FakeRunner)
    stack: Any = SimpleNamespace(
        homeserver="http://matrix.invalid",
        room_ids={"lobby": "!room:example"},
        room_keys=("lobby",),
        room_id="!room:example",
    )
    scenario = LiveFuzzScenario(thread_count=1, client_count=3, batches=())

    with pytest.raises(type(interruption)) as raised:
        await live_fuzz._run_live(
            stack,
            scenario,
            reply_timeout=1,
            settle_seconds=0,
        )

    assert raised.value is interruption
    assert closed == [0, 1, 2]
    assert any("RuntimeError: secondary close failed" in note for note in interruption.__notes__)


@pytest.mark.asyncio
async def test_run_live_groups_ordinary_client_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A passing workload reports every ordinary close failure as one group."""
    closed: list[int] = []
    clients: list[object] = []

    class FakeClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.index = len(clients)
            clients.append(self)

        async def close(self) -> None:
            closed.append(self.index)
            if self.index < 2:
                message = f"close {self.index} failed"
                raise RuntimeError(message)

    class FakeRunner:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def run(self) -> dict[str, object]:
            return {"status": "PASS"}

    monkeypatch.setattr(live_fuzz, "LiveMatrixClient", FakeClient)
    monkeypatch.setattr(live_fuzz, "LiveFuzzRunner", FakeRunner)
    stack: Any = SimpleNamespace(
        homeserver="http://matrix.invalid",
        room_ids={"lobby": "!room:example"},
        room_keys=("lobby",),
        room_id="!room:example",
    )
    scenario = LiveFuzzScenario(thread_count=1, client_count=3, batches=())

    with pytest.raises(ExceptionGroup, match="Matrix client cleanup failed") as raised:
        await live_fuzz._run_live(
            stack,
            scenario,
            reply_timeout=1,
            settle_seconds=0,
        )

    assert closed == [0, 1, 2]
    assert len(raised.value.exceptions) == 2
    assert "close Matrix client 0: close 0 failed" in str(raised.value.exceptions[0])
    assert "close Matrix client 1: close 1 failed" in str(raised.value.exceptions[1])


def test_pass_receipt_survives_bundle_discard(tmp_path: Path) -> None:
    """A successful exact-head run keeps compact provenance after bulky cleanup."""
    root = tmp_path / "artifacts"
    bundle = FailureBundle.create(root, "run-pass", scenario=_bundle_scenario(), provenance={"parent": True})
    provenance = {
        "matrix_homeserver": "http://127.0.0.1:18008",
        "matrix_server_implementation": "tuwunel",
        "matrix_server_name": "m-fuzz.localhost",
        "mindroom_dirty": False,
        "mindroom_source_sha256": "a" * 64,
        "mindroom_expected_revision": "abc",
        "mindroom_frozen_revision": "abc",
        "mindroom_revision": "abc",
        "nio_module_sha256": "b" * 64,
        "nio_expected_version": "def",
        "nio_version": "def",
        "runtime_generations": [
            {
                "mindroom_dirty": False,
                "mindroom_source_sha256": "a" * 64,
                "mindroom_expected_revision": "abc",
                "mindroom_revision": "abc",
                "runtime_generation": 1,
                "nio_expected_version": "def",
                "nio_version": "def",
                "nio_module_sha256": "b" * 64,
            },
        ],
        "final_source_validation": {
            "mindroom_dirty": False,
            "mindroom_source_sha256": "a" * 64,
            "mindroom_expected_revision": "abc",
            "mindroom_revision": "abc",
            "nio_module_sha256": "b" * 64,
            "nio_expected_version": "def",
            "nio_version": "def",
        },
        "tuwunel_container": "fuzz-tuwunel",
        "tuwunel_image_id": "sha256:1234",
        "tuwunel_image_reference": "ghcr.io/mindroom-ai/tuwunel:latest",
    }

    receipt = bundle.retain_pass_receipt({"status": "PASS"}, provenance)
    bundle.discard()

    assert receipt.exists()
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["cleanup"] == "PASS"
    assert payload["result"]["status"] == "PASS"
    assert payload["provenance"] == provenance
    assert len(payload["scenario_sha256"]) == 64


@pytest.mark.parametrize(
    ("nio_fields", "message"),
    [
        ({"nio_version": "actual", "nio_expected_version": ""}, "locked installed"),
        ({"nio_version": "actual", "nio_expected_version": "expected"}, "locked installed"),
        (
            {"nio_version": "actual", "nio_expected_version": "actual", "nio_module_sha256": "short"},
            "content digest",
        ),
    ],
)
def test_pass_receipt_rejects_unproven_nio(
    nio_fields: dict[str, object],
    message: str,
    tmp_path: Path,
) -> None:
    """Receipt creation independently enforces exact clean nio provenance."""
    bundle = FailureBundle.create(
        tmp_path / "artifacts",
        "run-rejected",
        scenario=_bundle_scenario(),
        provenance={},
    )
    provenance = {
        "final_source_validation": {
            "mindroom_dirty": False,
            "mindroom_source_sha256": "a" * 64,
            "mindroom_expected_revision": "mindroom-head",
            "mindroom_revision": "mindroom-head",
            "nio_module_sha256": "b" * 64,
            "nio_expected_version": nio_fields.get("nio_expected_version"),
            "nio_version": nio_fields.get("nio_version"),
        },
        "mindroom_frozen_revision": "mindroom-head",
        "runtime_generations": [
            {
                "mindroom_dirty": False,
                "mindroom_source_sha256": "a" * 64,
                "mindroom_expected_revision": "mindroom-head",
                "mindroom_revision": "mindroom-head",
            },
        ],
        "nio_module_sha256": "b" * 64,
        **nio_fields,
    }

    with pytest.raises(RuntimeError, match=message):
        bundle.retain_pass_receipt({"status": "PASS"}, provenance)

    assert not (tmp_path / "artifacts" / "receipts").exists()


def test_runtime_provenance_identifies_tuwunel_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Child attestation retains exact homeserver container and image identity."""
    sink: list[dict[str, object]] = []
    stack = ManagedTuwunelStack(
        state_root=tmp_path / "state",
        provenance_sink=lambda provenance: sink.append(dict(provenance)),
        mindroom_revision="abc",
    )
    stack._mindroom_source_digest = "a" * 64
    stack._nio_source_digest = "b" * 64
    try:
        stack.attestation_path.write_text("{}", encoding="utf-8")
        stack.homeserver = "http://127.0.0.1:18008"
        stack.server_name = "m-fuzz.localhost"
        monkeypatch.setattr(
            "scripts.testing.fuzz_live_matrix._validated_child_provenance",
            lambda *_args, **_kwargs: {
                "mindroom_dirty": False,
                "mindroom_source_sha256": "a" * 64,
                "mindroom_expected_revision": "abc",
                "mindroom_revision": "abc",
                "nio_module_sha256": "b" * 64,
                "nio_expected_version": "nio-head",
                "nio_version": "nio-head",
            },
        )
        monkeypatch.setattr(
            "scripts.testing.fuzz_live_matrix._run_command",
            lambda *_args, **_kwargs: json.dumps(
                [
                    {
                        "Config": {"Image": "ghcr.io/mindroom-ai/tuwunel:latest"},
                        "Image": "sha256:1234",
                    },
                ],
            ),
        )

        stack._wait_for_runtime_attestation()
        stack._wait_for_runtime_attestation()

        assert stack.runtime_provenance is not None
        generations = stack.runtime_provenance["runtime_generations"]
        assert isinstance(generations, list)
        assert [generation["runtime_generation"] for generation in generations] == [1, 2]
        assert all(generation["mindroom_revision"] == "abc" for generation in generations)
        assert stack.runtime_provenance["mindroom_frozen_revision"] == "abc"
        assert stack.runtime_provenance["runtime_generation"] == 2
        assert len(sink) == 2
        assert len(sink[0]["runtime_generations"]) == 1
        assert sink[1] == stack.runtime_provenance
    finally:
        stack.temp_dir.cleanup()


@pytest.mark.parametrize(
    "interruption",
    [KeyboardInterrupt("stop interrupted"), SystemExit("stop exited")],
)
def test_stack_close_attempts_every_stage_before_rethrowing_first_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    interruption: BaseException,
    tmp_path: Path,
) -> None:
    """One teardown interrupt must not skip later cleanup stages."""
    events: list[str] = []

    class FakeLog:
        closed = False

        def close(self) -> None:
            events.append("log")
            self.closed = True

    class FakeServer:
        def shutdown(self) -> None:
            events.append("shutdown")
            message = "shutdown failed"
            raise RuntimeError(message)

        def server_close(self) -> None:
            events.append("server_close")

    class FakeThread:
        def join(self, *, timeout: float) -> None:
            assert timeout == 5
            events.append("thread")

        def is_alive(self) -> bool:
            return False

    class FakeTempDir:
        def cleanup(self) -> None:
            events.append("temp")

    stack = object.__new__(ManagedTuwunelStack)
    stack.runtime_redaction_path = tmp_path / "runtime-redactions.jsonl"

    def stop_mindroom() -> None:
        events.append("mindroom")
        raise interruption

    stack._stop_mindroom = stop_mindroom  # type: ignore[method-assign]
    stack._log_handle = FakeLog()
    stack._model_server = FakeServer()
    stack._model_thread = FakeThread()
    stack._created = True
    stack.instance_name = "fuzz-test"
    stack.temp_dir = FakeTempDir()
    stack._write_manifest = lambda **_kwargs: events.append("manifest")  # type: ignore[method-assign]
    stack._release_host_lease = lambda: events.append("lease")  # type: ignore[method-assign]
    monkeypatch.setattr(
        "scripts.testing.fuzz_live_matrix._run_command",
        lambda *_args, **_kwargs: events.append("instance"),
    )

    def snapshot() -> None:
        events.append("snapshot")
        message = "snapshot failed"
        raise ValueError(message)

    with pytest.raises(type(interruption)) as raised:
        stack.close(before_destructive_cleanup=snapshot)

    assert raised.value is interruption
    assert events == [
        "mindroom",
        "log",
        "shutdown",
        "server_close",
        "thread",
        "snapshot",
        "instance",
        "temp",
        "manifest",
        "lease",
    ]
    assert any("stop model server: RuntimeError: shutdown failed" in note for note in interruption.__notes__)
    assert any("snapshot runtime evidence: ValueError: snapshot failed" in note for note in interruption.__notes__)


def test_stack_close_groups_ordinary_cleanup_failures(tmp_path: Path) -> None:
    """Ordinary teardown failures remain grouped after all stages finish."""
    events: list[str] = []
    stack = object.__new__(ManagedTuwunelStack)

    stack.runtime_redaction_path = tmp_path / "runtime-redactions.jsonl"

    def stop_mindroom() -> None:
        events.append("mindroom")
        message = "stop failed"
        raise RuntimeError(message)

    class FakeTempDir:
        def cleanup(self) -> None:
            events.append("temp")

    def release_lease() -> None:
        events.append("lease")
        message = "lease failed"
        raise OSError(message)

    stack._stop_mindroom = stop_mindroom  # type: ignore[method-assign]
    stack._log_handle = None
    stack._model_server = None
    stack._model_thread = None
    stack._created = False
    stack.temp_dir = FakeTempDir()
    stack._write_manifest = lambda **_kwargs: events.append("manifest")  # type: ignore[method-assign]
    stack._release_host_lease = release_lease  # type: ignore[method-assign]

    with pytest.raises(ExceptionGroup, match="live Matrix fuzz cleanup failed") as raised:
        stack.close()

    assert events == ["mindroom", "temp", "manifest", "lease"]
    assert len(raised.value.exceptions) == 2
    assert "stop MindRoom: stop failed" in str(raised.value.exceptions[0])
    assert "release host lease: lease failed" in str(raised.value.exceptions[1])


def test_host_lease_excludes_second_live_stack(tmp_path: Path) -> None:
    """Separate worktrees cannot allocate colliding Matrix ports concurrently."""
    first = ManagedTuwunelStack(state_root=tmp_path / "state")
    second = ManagedTuwunelStack(state_root=tmp_path / "state")
    try:
        first._acquire_host_lease()
        with pytest.raises(RuntimeError, match="host-wide"):
            second._acquire_host_lease()
        first._release_host_lease()
        second._acquire_host_lease()
    finally:
        first._release_host_lease()
        second._release_host_lease()
        first.temp_dir.cleanup()
        second.temp_dir.cleanup()


def test_live_stack_manifest_is_atomic_and_recoverable(tmp_path: Path) -> None:
    """Durable manifest names the exact instance and evidence directory."""
    artifact = tmp_path / "artifacts" / "run"
    stack = ManagedTuwunelStack(
        state_root=tmp_path / "state",
        artifact_directory=artifact,
    )
    try:
        stack._created = True
        stack._write_manifest(
            state="ready",
            matrix_port=18008,
            api_port=18765,
            mindroom_pid=1234,
        )
        payload = json.loads(stack.manifest_path.read_text(encoding="utf-8"))

        assert payload["instance_name"] == stack.instance_name
        assert payload["docker_compose_project"] == stack.instance_name
        assert payload["artifact_directory"] == str(artifact)
        assert payload["state"] == "ready"
        assert payload["matrix_port"] == 18008
        assert payload["api_port"] == 18765
        assert payload["mindroom_pid"] == 1234
        assert payload["instance_cleanup_required"] is True
        assert not stack.manifest_path.with_suffix(".tmp").exists()
    finally:
        stack.temp_dir.cleanup()


@pytest.mark.parametrize("remove_fails", [False, True])
def test_create_failure_retains_exact_cleanup_obligation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    remove_fails: bool,
) -> None:
    """A partially successful create stays recoverable until exact removal."""
    stack = ManagedTuwunelStack(
        state_root=tmp_path / "state",
    )
    commands: list[tuple[str, ...]] = []

    def run_command(*command: str, **_kwargs: object) -> str:
        commands.append(command)
        if command[1] == "local-instances-create":
            message = "create failed after side effect"
            raise RuntimeError(message)
        if remove_fails:
            message = "remove failed"
            raise RuntimeError(message)
        return ""

    monkeypatch.setattr(live_fuzz, "_run_command", run_command)

    with pytest.raises(RuntimeError, match="create failed after side effect"):
        stack.start()

    assert stack._created is True
    if remove_fails:
        with pytest.raises(ExceptionGroup, match="live Matrix fuzz cleanup failed"):
            stack.close()
    else:
        stack.close()

    payload = json.loads(stack.manifest_path.read_text(encoding="utf-8"))
    assert payload["state"] == ("cleanup_failed" if remove_fails else "closed")
    assert payload["instance_cleanup_required"] is remove_fails
    assert stack._created is remove_fails
    assert commands == [
        ("just", "local-instances-create", stack.instance_name, "tuwunel"),
        ("just", "local-instances-remove", stack.instance_name),
    ]


def test_abandoned_manifest_recovery_is_registry_aware_and_kills_exact_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale cleanup error cannot make an already-removed instance block forever."""
    state_root = tmp_path / "state"
    old_root = tmp_path / "old-worktree"
    registry_path = old_root / "local" / "instances" / "deploy" / "instances.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(json.dumps({"instances": {}}), encoding="utf-8")
    manifest_path = state_root / "runs" / "fuzz-old.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "docker_compose_project": "fuzz-old",
                "instance_name": "fuzz-old",
                "project_root": str(old_root),
                "state": "cleanup_failed",
                "instance_cleanup_required": False,
                "mindroom_pid": 4242,
                "mindroom_command_marker": "/persistent/attestation.json",
            },
        ),
        encoding="utf-8",
    )
    stack = ManagedTuwunelStack(state_root=state_root)
    events: list[str] = []
    try:
        monkeypatch.setattr(
            stack,
            "_terminate_recorded_mindroom",
            lambda _payload: events.append("process"),
        )
        monkeypatch.setattr(
            "scripts.testing.fuzz_live_matrix._run_command",
            lambda *_args, **_kwargs: events.append("instance"),
        )

        stack._recover_abandoned_runs()

        assert events == ["process"]
        assert json.loads(manifest_path.read_text(encoding="utf-8"))["state"] == "recovered"
    finally:
        stack.temp_dir.cleanup()


def test_abandoned_creating_manifest_cleans_unregistered_compose_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash between create side effects and registration still gets removed."""
    state_root = tmp_path / "state"
    old_root = tmp_path / "old-worktree"
    registry_path = old_root / "local" / "instances" / "deploy" / "instances.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(json.dumps({"instances": {}}), encoding="utf-8")
    manifest_path = state_root / "runs" / "fuzz-old.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "docker_compose_project": "fuzz-old",
                "instance_name": "fuzz-old",
                "project_root": str(old_root),
                "state": "creating",
            },
        ),
        encoding="utf-8",
    )
    stack = ManagedTuwunelStack(state_root=state_root)
    commands: list[tuple[tuple[str, ...], dict[str, object]]] = []
    try:
        monkeypatch.setattr(
            live_fuzz,
            "_run_command",
            lambda *command, **kwargs: commands.append((command, kwargs)),
        )

        stack._recover_abandoned_runs()

        assert commands == [
            (
                ("docker", "compose", "-p", "fuzz-old", "down", "-v"),
                {"cwd": old_root / "local" / "instances" / "deploy"},
            ),
        ]
        assert json.loads(manifest_path.read_text(encoding="utf-8"))["state"] == "recovered"
    finally:
        stack.temp_dir.cleanup()


def test_abandoned_manifest_recovery_survives_deleted_worktree_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deleted worktree cannot wedge every later fuzz run on stale cleanup."""
    state_root = tmp_path / "state"
    manifest_path = state_root / "runs" / "fuzz-old.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "docker_compose_project": "fuzz-old",
                "instance_name": "fuzz-old",
                "project_root": str(tmp_path / "deleted-worktree"),
                "state": "cleanup_failed",
                "mindroom_pid": 4242,
                "mindroom_command_marker": "/persistent/attestation.json",
            },
        ),
        encoding="utf-8",
    )
    stack = ManagedTuwunelStack(state_root=state_root)
    events: list[object] = []
    try:
        monkeypatch.setattr(
            stack,
            "_terminate_recorded_mindroom",
            lambda _payload: events.append("process"),
        )
        monkeypatch.setattr(
            "scripts.testing.fuzz_live_matrix._run_command",
            lambda *command, **kwargs: events.append((command, kwargs)),
        )

        stack._recover_abandoned_runs()
        stack._recover_abandoned_runs()

        assert events == [
            "process",
            (
                ("docker", "compose", "-p", "fuzz-old", "down", "-v"),
                {"cwd": live_fuzz.PROJECT_ROOT / "local" / "instances" / "deploy"},
            ),
        ]
        assert json.loads(manifest_path.read_text(encoding="utf-8"))["state"] == "recovered"
    finally:
        stack.temp_dir.cleanup()


def test_abandoned_failed_close_tears_down_exact_project_when_registry_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed close remains cleanup debt even after registry loss."""
    state_root = tmp_path / "state"
    old_root = tmp_path / "old-worktree"
    registry_path = old_root / "local" / "instances" / "deploy" / "instances.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(json.dumps({"instances": {}}), encoding="utf-8")
    manifest_path = state_root / "runs" / "fuzz-old.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "docker_compose_project": "fuzz-old",
                "instance_name": "fuzz-old",
                "instance_cleanup_required": True,
                "project_root": str(old_root),
                "state": "cleanup_failed",
            },
        ),
        encoding="utf-8",
    )
    stack = ManagedTuwunelStack(state_root=state_root)
    commands: list[tuple[tuple[str, ...], dict[str, object]]] = []
    try:
        monkeypatch.setattr(
            live_fuzz,
            "_run_command",
            lambda *command, **kwargs: commands.append((command, kwargs)),
        )

        stack._recover_abandoned_runs()

        assert commands == [
            (
                ("docker", "compose", "-p", "fuzz-old", "down", "-v"),
                {"cwd": old_root / "local" / "instances" / "deploy"},
            ),
        ]
        recovered = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert recovered["state"] == "recovered"
        assert recovered["instance_cleanup_required"] is False
    finally:
        stack.temp_dir.cleanup()


def test_abandoned_cleanup_failure_keeps_manifest_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed exact teardown cannot falsely mark its manifest recovered."""
    state_root = tmp_path / "state"
    old_root = tmp_path / "old-worktree"
    registry_path = old_root / "local" / "instances" / "deploy" / "instances.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(json.dumps({"instances": {}}), encoding="utf-8")
    manifest_path = state_root / "runs" / "fuzz-old.json"
    manifest_path.parent.mkdir(parents=True)
    manifest = {
        "docker_compose_project": "fuzz-old",
        "instance_name": "fuzz-old",
        "instance_cleanup_required": True,
        "project_root": str(old_root),
        "state": "cleanup_failed",
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    stack = ManagedTuwunelStack(state_root=state_root)
    try:

        def fail_teardown(*_command: str, **_kwargs: object) -> str:
            msg = "compose down failed"
            raise RuntimeError(msg)

        monkeypatch.setattr(live_fuzz, "_run_command", fail_teardown)

        with pytest.raises(RuntimeError, match="compose down failed"):
            stack._recover_abandoned_runs()

        assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest
    finally:
        stack.temp_dir.cleanup()


@pytest.mark.parametrize(
    "registry_text",
    [
        '{"instances":',
        json.dumps({"instances": []}),
    ],
)
def test_abandoned_recovery_uses_each_manifest_when_registry_is_corrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registry_text: str,
) -> None:
    """A corrupt shared registry falls back to each exact manifest project."""
    state_root = tmp_path / "state"
    old_root = tmp_path / "old-worktree"
    registry_path = old_root / "local" / "instances" / "deploy" / "instances.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(registry_text, encoding="utf-8")
    manifest_paths: list[Path] = []
    for instance_name in ("fuzz-old-a", "fuzz-old-b"):
        manifest_path = state_root / "runs" / f"{instance_name}.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "docker_compose_project": instance_name,
                    "instance_name": instance_name,
                    "instance_cleanup_required": True,
                    "project_root": str(old_root),
                    "state": "cleanup_failed",
                },
            ),
            encoding="utf-8",
        )
        manifest_paths.append(manifest_path)
    stack = ManagedTuwunelStack(state_root=state_root)
    commands: list[tuple[tuple[str, ...], dict[str, object]]] = []
    try:
        monkeypatch.setattr(
            live_fuzz,
            "_run_command",
            lambda *command, **kwargs: commands.append((command, kwargs)),
        )

        stack._recover_abandoned_runs()

        assert commands == [
            (
                ("docker", "compose", "-p", instance_name, "down", "-v"),
                {"cwd": old_root / "local" / "instances" / "deploy"},
            )
            for instance_name in ("fuzz-old-a", "fuzz-old-b")
        ]
        for manifest_path in manifest_paths:
            recovered = json.loads(manifest_path.read_text(encoding="utf-8"))
            assert recovered["state"] == "recovered"
            assert recovered["instance_cleanup_required"] is False
    finally:
        stack.temp_dir.cleanup()


def test_abandoned_process_group_requires_exact_command_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crash recovery kills only the process group named by its durable manifest."""
    ps_result = SimpleNamespace(
        stdout=(
            " 4242 4242 uv run python /repo/fuzz_live_matrix.py "
            "__mindroom_runtime_child__ /persistent/attestation.json run\n"
            " 4243 4242 python mindroom-worker\n"
        ),
    )
    monkeypatch.setattr(
        "scripts.testing.fuzz_live_matrix.subprocess.run",
        lambda *_args, **_kwargs: ps_result,
    )
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr("scripts.testing.fuzz_live_matrix.os.killpg", lambda pid, sig: signals.append((pid, sig)))

    ManagedTuwunelStack._terminate_recorded_mindroom(
        {
            "mindroom_pid": 4242,
            "mindroom_command_marker": "/persistent/attestation.json",
        },
    )

    assert signals == [(4242, signal.SIGKILL)]


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["fuzz", "chaos", "saturation"])
async def test_every_live_profile_runs_the_shared_final_audit(profile: str) -> None:
    """Every workload settles current startup before the independent final audit."""
    calls: list[str] = []

    class FakeClient:
        async def register(self) -> None:
            calls.append("register")

        async def join_room(self) -> None:
            calls.append("join")

        async def sync_incremental(self, *, timeout_ms: int, allow_limited: bool) -> None:
            assert (timeout_ms, allow_limited) == (0, True)
            calls.append("client-sync")

    class FakeOracle:
        async def initialize(self) -> None:
            calls.append("oracle-init")

        async def wait_until_exact(self, *, deadline_seconds: float, settle_seconds: float) -> None:
            assert (deadline_seconds, settle_seconds) == (12.0, 0.75)
            calls.append("oracle-quiet")

    class FakeStack:
        @staticmethod
        def wait_for_startup_maintenance(*, timeout_seconds: float) -> None:
            assert timeout_seconds == 12.0
            calls.append("maintenance")

    async def send_roots(_threads: Collection[int]) -> None:
        calls.append("roots")

    async def run_profile(*_args: object) -> dict[str, object]:
        calls.append(profile)
        return {"status": "PASS"}

    async def audit_final_state() -> dict[str, int]:
        calls.append("audit")
        return {"audited_events": 7}

    runner = object.__new__(LiveFuzzRunner)
    runner.clients = (FakeClient(),)
    stack = FakeStack()
    stack.assert_mindroom_running = lambda: calls.append("runtime-alive")  # type: ignore[attr-defined]
    runner.stack = stack
    runner.oracle = FakeOracle()
    runner.scenario = LiveFuzzScenario(thread_count=1, batches=(), profile=profile)
    runner.reply_timeout = 12.0
    runner.settle_seconds = 0.75
    runner._startup_maintenance_pending = True
    runner._send_roots = send_roots  # type: ignore[method-assign]
    runner._await_first_baseline_response = AsyncMock()
    runner._await_room_baselines = AsyncMock()
    runner._wait_for_pending_mutation_effects = AsyncMock()
    runner._run_batches = run_profile  # type: ignore[method-assign]
    runner._run_chaos = run_profile  # type: ignore[method-assign]
    runner._run_short_stream_correctness = run_profile  # type: ignore[method-assign]
    runner._audit_final_state = audit_final_state  # type: ignore[method-assign]

    result = await runner.run()

    assert calls[-6:] == [
        profile,
        "maintenance",
        "oracle-quiet",
        "runtime-alive",
        "audit",
        "runtime-alive",
    ]
    assert result == {"status": "PASS", "audited_events": 7}


@pytest.mark.asyncio
async def test_new_runner_owes_initial_startup_maintenance(tmp_path: Path) -> None:
    """The process started before runner construction still needs the final fence."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    stack = SimpleNamespace(
        agent_id="@agent:example",
        router_id="@router:example",
        storage_path=tmp_path,
        runtime_redaction_path=tmp_path / "runtime-redactions.jsonl",
        log_path=tmp_path / "mindroom.log",
    )
    try:
        runner = LiveFuzzRunner(
            stack,  # type: ignore[arg-type]
            (client,),
            LiveFuzzScenario(thread_count=1, batches=()),
            reply_timeout=12.0,
            settle_seconds=0.75,
        )
        assert runner._startup_maintenance_pending is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_chaos_restart_registers_current_generation_maintenance() -> None:
    """Chaos restarts must register current-generation maintenance debt."""
    calls: list[str] = []

    class FakeStack:
        @staticmethod
        def restart_mindroom() -> None:
            calls.append("restart")

    class FakeOracle:
        def __init__(self) -> None:
            self.expected_sources: dict[str, str] = {}

        @staticmethod
        async def wait_until_exact(*, deadline_seconds: float, settle_seconds: float) -> None:
            assert (deadline_seconds, settle_seconds) == (12.0, 0.75)
            calls.append("batch-quiet")

    runner = object.__new__(LiveFuzzRunner)
    runner.stack = FakeStack()
    runner.oracle = FakeOracle()
    runner.scenario = LiveFuzzScenario(thread_count=1, batches=())
    runner.reply_timeout = 12.0
    runner.settle_seconds = 0.75
    runner.restart_count = 0
    runner.executed_batches = 0
    runner.operation_count = 0
    runner._mindroom_running = True
    runner._startup_maintenance_pending = False
    runner._journal = None
    restart = LiveOperation(
        0,
        LiveOperationKind.RESTART_MINDROOM,
        0,
        None,
    )

    await runner._apply_lifecycle(restart.kind, 0)

    assert calls == ["restart"]
    assert runner.restart_count == 1
    assert runner._startup_maintenance_pending is True


@pytest.mark.asyncio
async def test_chaos_checkpoint_waits_for_latest_marker_and_tombstone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reply settlement alone cannot release delayed mutation effects."""
    runner = _temporal_revision_runner()
    marker = _source_marker("root:0", "edit:7")
    runner._pending_edit_markers = {"$root": {"$edit": marker}}
    runner.source_revision_markers = {"$root": {"$edit": marker}}
    runner._pending_source_tombstones = {"$redacted"}
    runner.oracle.event_summaries = {"$edit": {"origin_server_ts": 100}}
    runner.oracle._ledger_records["$root"] = TurnRecord.create(
        source_event_ids=("$root",),
        response_event_id="$reply",
        revision_replay={"$edit": RevisionReplay("$root", 100, response_event_id="$reply")},
    )
    runner.oracle.latest_reply_bodies["$reply"] = ((0, 0, "$reply"), _short_body_for(1))
    runner.reply_timeout = 12.0
    runner.settle_seconds = 0.75
    runner.pending_grace = 1.0
    calls: list[str] = []

    async def wait_until_exact(*, deadline_seconds: float, settle_seconds: float) -> None:
        assert (deadline_seconds, settle_seconds) == (12.0, 0.75)
        calls.append("replies")

    async def pump(*, timeout_ms: int) -> None:
        assert timeout_ms == 250
        calls.append(f"pump:{len(calls)}")
        runner.oracle._ledger_records["$redacted"] = TurnRecord.create(
            source_event_ids=("$redacted",),
            redacted_source_event_ids=("$redacted",),
        )
        if len(calls) == 3:
            runner.oracle.latest_reply_bodies["$reply"] = ((1, 1, "$edit"), _short_body_for(2))

    monkeypatch.setattr(runner.oracle, "unsettled_required_sources", list)
    monkeypatch.setattr(runner.oracle, "wait_until_exact", wait_until_exact)
    monkeypatch.setattr(runner.oracle, "pump", pump)
    monkeypatch.setattr(
        _ModelHandler,
        "observed_markers_for",
        lambda call: frozenset({marker}) if call == 2 else frozenset(),
    )
    await runner._checkpoint(batch_index=9)
    assert calls == ["replies", "pump:1", "pump:2"]
    assert runner._pending_edit_markers == {}
    assert runner._pending_source_tombstones == set()


@pytest.mark.asyncio
async def test_chaos_checkpoint_releases_marker_for_no_response_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """A durable no-response source has no visible reply mutation to await."""
    runner = _temporal_revision_runner()
    runner._pending_edit_markers = {"$root": {"$edit": _source_marker("root:0", "edit:4")}}
    runner.oracle._ledger_records["$root"] = TurnRecord.create(source_event_ids=("$root",))
    monkeypatch.setattr(runner.oracle, "pump", AsyncMock())
    await runner._wait_for_pending_mutation_effects(deadline_seconds=1.0, batch_index=3)
    assert runner._pending_edit_markers == {}


@pytest.mark.asyncio
async def test_chaos_checkpoint_tombstone_releases_same_source_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A source tombstone supersedes a concurrently landed edit obligation."""
    runner = _temporal_revision_runner()
    runner._pending_edit_markers = {"$root": {"$edit": _source_marker("root:0", "edit:183")}}
    runner._pending_source_tombstones = {"$root"}
    pumps = 0

    async def pump(*, timeout_ms: int) -> None:
        nonlocal pumps
        assert timeout_ms == 250
        pumps += 1
        runner.oracle._ledger_records["$root"] = TurnRecord.create(
            source_event_ids=("$root",),
            response_event_id="$reply",
            redacted_source_event_ids=("$root",),
        )

    monkeypatch.setattr(runner.oracle, "pump", pump)
    await runner._wait_for_pending_mutation_effects(deadline_seconds=1.0, batch_index=26)
    assert pumps == 1
    assert runner._pending_edit_markers == {}
    assert runner._pending_source_tombstones == set()


@pytest.mark.asyncio
async def test_startup_phase_failure_prevents_shared_final_audit() -> None:
    """A failed current-generation phase blocks audit for every profile."""
    calls: list[str] = []

    class FakeClient:
        @staticmethod
        async def register() -> None:
            return

        @staticmethod
        async def join_room() -> None:
            return

    class FakeOracle:
        @staticmethod
        async def initialize() -> None:
            return

        @staticmethod
        async def wait_until_exact(*, deadline_seconds: float, settle_seconds: float) -> None:
            assert (deadline_seconds, settle_seconds) == (12.0, 0.75)

    class FakeStack:
        @staticmethod
        def wait_for_startup_maintenance(*, timeout_seconds: float) -> None:
            assert timeout_seconds == 12.0
            calls.append("maintenance-failed")
            failure = "startup phase failed"
            raise AssertionError(failure)

    async def send_roots(_threads: Collection[int]) -> None:
        return

    async def run_batches(*_args: object) -> dict[str, object]:
        return {"status": "PASS"}

    async def audit_final_state() -> dict[str, int]:
        calls.append("audit")
        return {}

    runner = object.__new__(LiveFuzzRunner)
    runner.clients = (FakeClient(),)
    runner.stack = FakeStack()
    runner.oracle = FakeOracle()
    runner.scenario = LiveFuzzScenario(thread_count=1, batches=())
    runner.reply_timeout = 12.0
    runner.settle_seconds = 0.75
    runner._startup_maintenance_pending = True
    runner._send_roots = send_roots  # type: ignore[method-assign]
    runner._await_first_baseline_response = AsyncMock()
    runner._await_room_baselines = AsyncMock()
    runner._wait_for_pending_mutation_effects = AsyncMock()
    runner._run_batches = run_batches  # type: ignore[method-assign]
    runner._audit_final_state = audit_final_state  # type: ignore[method-assign]

    with pytest.raises(AssertionError, match="startup phase failed"):
        await runner.run()

    assert calls == ["maintenance-failed"]


@pytest.mark.asyncio
async def test_dead_runtime_prevents_shared_final_audit() -> None:
    """A dead SUT cannot pass from already-persisted Matrix and ledger state."""
    calls: list[str] = []

    class FakeClient:
        @staticmethod
        async def register() -> None:
            return

        @staticmethod
        async def join_room() -> None:
            return

    class FakeOracle:
        @staticmethod
        async def initialize() -> None:
            return

        @staticmethod
        async def wait_until_exact(*, deadline_seconds: float, settle_seconds: float) -> None:
            assert (deadline_seconds, settle_seconds) == (12.0, 0.75)

    class FakeStack:
        @staticmethod
        def wait_for_startup_maintenance(*, timeout_seconds: float) -> None:
            assert timeout_seconds == 12.0

        @staticmethod
        def assert_mindroom_running() -> None:
            calls.append("runtime-dead")
            message = "runtime exited"
            raise RuntimeError(message)

    async def send_roots(_threads: Collection[int]) -> None:
        return

    async def run_batches(*_args: object) -> dict[str, object]:
        return {"status": "PASS"}

    async def audit_final_state() -> dict[str, int]:
        calls.append("audit")
        return {}

    runner = object.__new__(LiveFuzzRunner)
    runner.clients = (FakeClient(),)
    runner.stack = FakeStack()
    runner.oracle = FakeOracle()
    runner.scenario = LiveFuzzScenario(thread_count=1, batches=())
    runner.reply_timeout = 12.0
    runner.settle_seconds = 0.75
    runner._startup_maintenance_pending = True
    runner._send_roots = send_roots  # type: ignore[method-assign]
    runner._await_first_baseline_response = AsyncMock()
    runner._await_room_baselines = AsyncMock()
    runner._wait_for_pending_mutation_effects = AsyncMock()
    runner._run_batches = run_batches  # type: ignore[method-assign]
    runner._audit_final_state = audit_final_state  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="runtime exited"):
        await runner.run()

    assert calls == ["runtime-dead"]


@pytest.mark.asyncio
async def test_runtime_exit_during_final_audit_prevents_pass() -> None:  # noqa: C901
    """SUT must remain alive through completion of canonical final reads."""
    calls: list[str] = []

    class FakeClient:
        @staticmethod
        async def register() -> None:
            return

        @staticmethod
        async def join_room() -> None:
            return

    class FakeOracle:
        @staticmethod
        async def initialize() -> None:
            return

        @staticmethod
        async def wait_until_exact(*, deadline_seconds: float, settle_seconds: float) -> None:
            assert (deadline_seconds, settle_seconds) == (12.0, 0.75)

    class FakeStack:
        health_checks = 0

        @staticmethod
        def wait_for_startup_maintenance(*, timeout_seconds: float) -> None:
            assert timeout_seconds == 12.0

        def assert_mindroom_running(self) -> None:
            self.health_checks += 1
            calls.append(f"health:{self.health_checks}")
            if self.health_checks == 2:
                message = "runtime exited during audit"
                raise RuntimeError(message)

    async def send_roots(_threads: Collection[int]) -> None:
        return

    async def run_batches(*_args: object) -> dict[str, object]:
        return {"status": "PASS"}

    async def audit_final_state() -> dict[str, int]:
        calls.append("audit")
        return {}

    runner = object.__new__(LiveFuzzRunner)
    runner.clients = (FakeClient(),)
    runner.stack = FakeStack()
    runner.oracle = FakeOracle()
    runner.scenario = LiveFuzzScenario(thread_count=1, batches=())
    runner.reply_timeout = 12.0
    runner.settle_seconds = 0.75
    runner._startup_maintenance_pending = True
    runner._send_roots = send_roots  # type: ignore[method-assign]
    runner._await_first_baseline_response = AsyncMock()
    runner._await_room_baselines = AsyncMock()
    runner._wait_for_pending_mutation_effects = AsyncMock()
    runner._run_batches = run_batches  # type: ignore[method-assign]
    runner._audit_final_state = audit_final_state  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="exited during audit"):
        await runner.run()

    assert calls == ["health:1", "audit", "health:2"]


@pytest.mark.asyncio
async def test_restart_recovery_waits_for_maintenance_before_oracle_quiet() -> None:
    """The final restart audit is driven by runtime completion, then Matrix sync."""
    calls: list[str] = []

    class FakeStack:
        @staticmethod
        def wait_for_startup_maintenance(*, timeout_seconds: float) -> None:
            assert timeout_seconds == 12.0
            calls.append("maintenance")

    class FakeOracle:
        @staticmethod
        async def wait_until_exact(*, deadline_seconds: float, settle_seconds: float) -> None:
            assert (deadline_seconds, settle_seconds) == (12.0, 0.75)
            calls.append("oracle-quiet")

    runner = object.__new__(LiveFuzzRunner)
    runner.stack = FakeStack()
    runner.oracle = FakeOracle()
    runner.reply_timeout = 12.0
    runner.settle_seconds = 0.75
    runner._startup_maintenance_pending = True

    await runner._wait_for_restart_recovery_window()

    assert calls == ["maintenance", "oracle-quiet"]
    assert runner._startup_maintenance_pending is False


@pytest.mark.asyncio
async def test_final_messages_cardinality_rejects_delayed_duplicate() -> None:
    """The canonical `/messages` view remains the final saturation authority."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    oracle.expect("root:0", "$source")
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=_short_body_for,
    )
    try:
        with pytest.raises(AssertionError, match="2 direct replies in /messages"):
            auditor._assert_reply_cardinality(
                {"$source": {"$first", "$delayed-duplicate"}},
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_chaos_cardinality_allows_zero_direct_reply_only_within_same_thread() -> None:
    """Coalescing may cover different requesters in one thread, never another thread."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example", coalescing_threads=True)
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=_short_body_for,
    )
    try:
        oracle.expect("op:1", "$first", thread=0, client=0)
        oracle.expect("op:2", "$second", thread=0, client=1)
        auditor._assert_reply_cardinality({"$second": {"$combined"}})

        oracle.expect("op:3", "$other-chain", thread=1, client=1)
        with pytest.raises(AssertionError, match="op:3 has no canonical reply in its Matrix thread"):
            auditor._assert_reply_cardinality({"$second": {"$combined"}})
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_saturation_requires_oracle_and_sender_quiet_windows() -> None:
    """Saturation cannot check cardinality before both sync views are quiet."""
    calls: list[str] = []

    class FakeClient:
        def __init__(self) -> None:
            self.seen_events: dict[str, dict[str, Any]] = {}

        @staticmethod
        async def wait_until_quiet(*, deadline_seconds: float, quiet_seconds: float) -> None:
            assert (deadline_seconds, quiet_seconds) == (12.0, 0.75)
            calls.append("sender-quiet")

    class FakeOracle:
        @staticmethod
        async def wait_until_exact(*, deadline_seconds: float, settle_seconds: float) -> None:
            assert (deadline_seconds, settle_seconds) == (12.0, 0.75)
            calls.append("oracle-quiet")

    async def saturation_turn(
        _client: FakeClient,
        **kwargs: object,
    ) -> tuple[str, str]:
        expected_sources = kwargs["expected_sources"]
        assert isinstance(expected_sources, set)
        expected_sources.add("$source")
        calls.append("turn")
        return "$root", "$reply"

    runner = object.__new__(LiveFuzzRunner)
    runner.clients = (FakeClient(),)
    runner.scenario = LiveFuzzScenario(thread_count=1, batches=(), profile="saturation")
    runner.oracle = FakeOracle()
    runner.reply_timeout = 12.0
    runner.settle_seconds = 0.75
    runner.operation_count = 0
    runner.executed_batches = 0
    runner._short_stream_turn = saturation_turn  # type: ignore[method-assign]
    runner._canonical_response_ids = lambda _events: {"$source": {"$reply"}}  # type: ignore[method-assign]

    result = await runner._run_short_stream_correctness()

    assert calls == ["turn", "oracle-quiet", "sender-quiet"]
    assert result["status"] == "PASS"


@pytest.mark.asyncio
async def test_short_stream_turn_registers_exact_sent_source_for_final_audit() -> None:
    """Saturation audit inputs come from the real Matrix send result."""
    expected: list[tuple[str, str, int, int]] = []

    class FakeOracle:
        def begin_expectation_registration(self) -> None:
            expected.append(("begin", "", -1, -1))

        def expect(
            self,
            logical_ref: str,
            event_id: str,
            *,
            thread: int,
            client: int,
            sent_at: float,
        ) -> None:
            assert sent_at > 0
            expected.append((logical_ref, event_id, thread, client))

        def finish_expectation_registration(self, *, validate: bool) -> None:
            assert validate is True
            expected.append(("finish", "", -1, -1))

    class FakeClient:
        user_id = "@sender:example"

        @staticmethod
        async def send_event(
            event_type: str,
            _txn_id: str,
            content: dict[str, Any],
            *,
            room_id: str,
        ) -> str:
            assert event_type == "m.room.message"
            assert content["body"].startswith("Live short-stream correctness op:9")
            assert room_id == "!room:example"
            return "$source-from-server"

    async def completed_response(
        _client: FakeClient,
        *,
        root_event_id: str,
        source_event_id: str,
    ) -> str:
        assert root_event_id == "$root"
        assert source_event_id == "$source-from-server"
        return "$response-from-server"

    runner = object.__new__(LiveFuzzRunner)
    runner.stack = SimpleNamespace(
        agent_id="@agent:example",
        room_id="!room:example",
        room_ids={"lobby": "!room:example"},
        room_keys=("lobby",),
    )
    runner.scenario = LiveFuzzScenario(thread_count=4, batches=(), profile="saturation")
    runner.oracle = FakeOracle()
    runner.sent_records = []
    runner.source_current_markers = {}
    runner._wait_for_completed_response = completed_response  # type: ignore[method-assign]
    expected_sources: set[str] = set()
    marker = _source_marker("op:9", ORIGINAL_REVISION)

    result = await runner._short_stream_turn(
        FakeClient(),  # type: ignore[arg-type]
        label="op:9",
        thread=3,
        client_index=2,
        thread_root="$root",
        reply_to="$prior",
        expected_sources=expected_sources,
    )

    assert result == ("$root", "$response-from-server")
    assert expected_sources == {"$source-from-server"}
    assert runner.source_current_markers == {"$source-from-server": marker}
    assert expected == [
        ("begin", "", -1, -1),
        ("op:9", "$source-from-server", 3, 2),
        ("finish", "", -1, -1),
    ]
    assert runner.sent_records == [
        _SentRecord(
            "$source-from-server",
            "!room:example",
            "m.room.message",
            sender="@sender:example",
            content=runner.sent_records[0].content,
        ),
    ]
    assert runner.sent_records[0].content is not None
    assert runner.sent_records[0].content["body"].endswith(marker)


def test_failure_bundle_interleaves_lifecycle_boundaries(tmp_path: Path) -> None:
    """Codex #5: restarts and outages appear in the realized sequence.

    A restart between two mutations reorders which of them the running MindRoom
    ever observed, so the journal must record the boundary with a monotonic
    sequence spanning mutations and lifecycle alike, without inflating the
    mutation-only operation count.
    """
    bundle = FailureBundle.create(tmp_path / "artifacts", "run-4", scenario=_bundle_scenario(), provenance={})
    runner = object.__new__(LiveFuzzRunner)
    runner.operation_count = 0
    runner._realized_sequence = 0
    runner.event_ids = {}
    runner.sent_payloads = {}
    runner._mindroom_running = True
    runner._journal = bundle.record_realized

    runner._record_batch_results([(LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, None), "$first", None)])
    runner._mindroom_running = False
    runner._record_lifecycle(LiveOperationKind.STOP_MINDROOM)
    runner._mindroom_running = True
    runner._record_lifecycle(LiveOperationKind.START_MINDROOM)
    runner._record_batch_results([(LiveOperation(1, LiveOperationKind.THREAD_MESSAGE, 0, None), "$second", None)])

    journal = [
        json.loads(line)
        for line in (bundle.directory / "realized_journal.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [entry["kind"] for entry in journal] == [
        "thread_message",
        "stop_mindroom",
        "start_mindroom",
        "thread_message",
    ]
    assert [entry["sequence"] for entry in journal] == [1, 2, 3, 4]
    assert [entry["mindroom_running"] for entry in journal] == [True, False, True, True]
    # Lifecycle boundaries never inflate the mutation-only operation count.
    assert runner.operation_count == 2


@pytest.mark.asyncio
async def test_apply_batch_returns_results_in_true_completion_order() -> None:
    """Codex #5: a concurrent batch is journaled by completion, not input order.

    ``asyncio.gather`` preserves input order, so the durable journal would
    misrepresent which send actually landed first. The batch driver drains each
    apply as it resolves, so a later-listed op that finishes first is recorded
    first.
    """
    runner = object.__new__(LiveFuzzRunner)

    async def fake_apply(operation: LiveOperation) -> tuple[LiveOperation, str, None]:
        # The first-listed op sleeps longest, so completion order reverses input.
        await asyncio.sleep(0.03 if operation.thread == 0 else 0.0)
        return operation, f"$done-{operation.thread}", None

    runner._apply = fake_apply  # type: ignore[method-assign]
    batch = (
        LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, None, client=0),
        LiveOperation(1, LiveOperationKind.THREAD_MESSAGE, 2, None, client=1),
    )

    results = await runner._apply_batch_in_completion_order(batch)

    # Thread 2 (listed second) resolves first, so it leads the completion order.
    assert [operation.thread for operation, _event_id, _payload in results] == [2, 0]


@pytest.mark.asyncio
async def test_apply_batch_journals_landed_sibling_before_failure() -> None:
    """A failed concurrent sibling must not erase an already-landed mutation."""
    runner = object.__new__(LiveFuzzRunner)
    runner.operation_count = 0
    runner._realized_sequence = 0
    runner.event_ids = {}
    runner.sent_payloads = {}
    runner._mindroom_running = True
    journal: list[dict[str, object]] = []
    runner._journal = journal.append
    landed = asyncio.Event()
    blocker_started = asyncio.Event()
    blocker_cancelled = asyncio.Event()
    never = asyncio.Event()

    async def fake_apply(
        operation: LiveOperation,
    ) -> tuple[LiveOperation, str | None, None]:
        if operation.thread == 0:
            landed.set()
            return operation, "$landed", None
        if operation.thread == 1:
            await landed.wait()
            await blocker_started.wait()
            message = "sibling failed"
            raise RuntimeError(message)
        blocker_started.set()
        try:
            await never.wait()
        finally:
            blocker_cancelled.set()

    runner._apply = fake_apply  # type: ignore[method-assign]
    batch = (
        LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, None),
        LiveOperation(1, LiveOperationKind.THREAD_MESSAGE, 1, None),
        LiveOperation(2, LiveOperationKind.THREAD_MESSAGE, 2, None),
    )

    with pytest.raises(RuntimeError, match="sibling failed"):
        await runner._apply_batch_in_completion_order(
            batch,
            on_complete=runner._record_batch_results,
        )

    assert runner.operation_count == 1
    assert runner.event_ids == {"op:0": "$landed"}
    assert [entry["event_id"] for entry in journal] == ["$landed"]
    assert blocker_cancelled.is_set()


@pytest.mark.asyncio
async def test_apply_batch_records_success_when_failure_is_observed_first() -> None:
    """A failed task cannot hide another task that already returned success."""
    runner = object.__new__(LiveFuzzRunner)
    recorded: list[str] = []

    async def fake_apply(
        operation: LiveOperation,
    ) -> tuple[LiveOperation, str | None, None]:
        if operation.thread == 0:
            message = "first task failed"
            raise RuntimeError(message)
        return operation, "$landed", None

    def record(
        results: Collection[tuple[LiveOperation, str | None, object]],
    ) -> None:
        recorded.extend(event_id for _operation, event_id, _payload in results if event_id is not None)

    runner._apply = fake_apply  # type: ignore[method-assign]
    batch = (
        LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, None),
        LiveOperation(1, LiveOperationKind.THREAD_MESSAGE, 1, None),
    )

    with pytest.raises(RuntimeError, match="first task failed"):
        await runner._apply_batch_in_completion_order(batch, on_complete=record)

    assert recorded == ["$landed"]


def _redaction_accounting_runner(
    *,
    pump: Callable[..., Any],
) -> tuple[LiveFuzzRunner, SimpleNamespace, list[dict[str, object]]]:
    """Build a bare redaction runner with durable in-memory accounting."""

    class RedactionClient:
        user_id = "@user:example"

        @staticmethod
        async def redact(
            target_event_id: str,
            _txn_id: str,
            *,
            room_id: str,
        ) -> str:
            assert target_event_id == "$source"
            assert room_id == "!room:example"
            return "$redaction"

    oracle = SimpleNamespace(
        expected_sources={"$source": "op:1"},
        pump=pump,
        refresh_ledger_attributions=lambda **_kwargs: None,
        settled_sources=lambda: set(),
        optional_sources=set(),
    )
    oracle.mark_source_optional = oracle.optional_sources.add
    runner = _bundle_runner(oracle)
    runner.redacted_targets = {}
    runner.sent_records = []
    runner._edit_event_source = {}
    runner.redacted_edit_evidence = {}
    runner.source_revision_markers = defaultdict(dict)
    runner._pending_edit_markers = {}
    runner._pending_source_tombstones = set()
    runner.operation_count = 0
    runner._realized_sequence = 0
    runner.event_ids = {}
    runner.sent_payloads = {}
    runner._mindroom_running = True
    journal: list[dict[str, object]] = []
    runner._journal = journal.append
    runner._resolve_target = lambda _logical_ref: asyncio.sleep(0, result="$source")  # type: ignore[method-assign]
    runner._client_for_operation = lambda _operation: RedactionClient()  # type: ignore[method-assign]
    runner._room_for_thread = lambda _thread: "!room:example"  # type: ignore[method-assign]
    return runner, oracle, journal


@pytest.mark.asyncio
async def test_redaction_success_journals_once_before_optional_classification() -> None:
    """A normally completed source redaction is accounted and classified once."""

    async def pump(*, timeout_ms: int) -> None:
        assert timeout_ms == 0

    runner, oracle, journal = _redaction_accounting_runner(pump=pump)
    operation = LiveOperation(3, LiveOperationKind.REDACTION, 0, "op:1")

    await runner._apply_batch_in_completion_order(
        (operation,),
        on_complete=runner._record_batch_results,
    )

    assert runner.operation_count == 1
    assert runner.event_ids == {"op:3": "$redaction"}
    assert runner.redacted_targets == {"$source": "$redaction"}
    assert runner._pending_source_tombstones == {"$source"}
    assert oracle.optional_sources == {"$source"}
    assert [entry["event_id"] for entry in journal] == ["$redaction"]


@pytest.mark.asyncio
async def test_redaction_cancelled_in_pump_keeps_one_landed_journal_record() -> None:
    """Cancellation after Matrix landing cannot erase or duplicate accounting."""
    pump_started = asyncio.Event()
    never = asyncio.Event()

    async def pump(*, timeout_ms: int) -> None:
        assert timeout_ms == 0
        pump_started.set()
        await never.wait()

    runner, oracle, journal = _redaction_accounting_runner(pump=pump)
    operation = LiveOperation(3, LiveOperationKind.REDACTION, 0, "op:1")
    task = asyncio.create_task(
        runner._apply_batch_in_completion_order(
            (operation,),
            on_complete=runner._record_batch_results,
        ),
    )
    await pump_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runner.operation_count == 1
    assert runner.event_ids == {"op:3": "$redaction"}
    assert runner.redacted_targets == {"$source": "$redaction"}
    assert oracle.optional_sources == set()
    assert [entry["event_id"] for entry in journal] == ["$redaction"]


@pytest.mark.asyncio
async def test_send_expected_message_defers_reply_checks_until_registration() -> None:
    """A fast reply cannot be rejected while its Matrix source ID is in flight."""
    matrix_client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(matrix_client, "@agent:example")
    runner = _bundle_runner(oracle)
    runner.sent_records = []

    class FastReplyClient:
        user_id = "@user:example"

        @staticmethod
        async def send_event(
            _event_type: str,
            _txn_id: str,
            _content: object,
            *,
            room_id: str,
        ) -> str:
            assert room_id == "!room:example"
            oracle._ingest_event(_agent_reply_event("$source", "$reply", "LIVE-FUZZ call=1 END call=1"))
            oracle._assert_no_wrong_replies()
            return "$source"

    operation = LiveOperation(1, LiveOperationKind.THREAD_MESSAGE, 0, "root:0")
    payload = _SentPayload(
        event_type="m.room.message",
        txn_id="txn",
        content={"msgtype": "m.text", "body": "source"},
    )
    try:
        event_id = await runner._send_expected_message(
            operation,
            FastReplyClient(),  # type: ignore[arg-type]
            payload,
            "!room:example",
        )
    finally:
        await matrix_client.close()

    assert event_id == "$source"
    assert oracle.expected_sources == {"$source": "op:1"}
    assert oracle.response_ids["$source"] == {"$reply"}


@pytest.mark.asyncio
async def test_failure_bundle_artifact_error_preserves_primary_failure(tmp_path: Path) -> None:
    """A broken artifact writer must not raise over the primary fuzz error."""
    bundle = FailureBundle.create(
        tmp_path / "artifacts",
        "run-3",
        scenario=_bundle_scenario(),
        provenance={},
    )
    # A directory where a log file is expected forces the copy to fail.
    (bundle.directory / "mindroom.log").mkdir()
    stack = _prepared_stack(tmp_path)
    oracle = _snapshot_oracle()
    runner = _bundle_runner(oracle)

    try:
        # Must not raise: the primary AssertionError is re-raised by main(), not here.
        _persist_failure_bundle(bundle, stack, runner, AssertionError("primary invariant"))
    finally:
        await oracle.client.close()

    errors = (bundle.directory / "artifact_errors.txt").read_text(encoding="utf-8")
    assert "mindroom.log" in errors
    # Other artifacts were still written despite the one failure.
    assert (bundle.directory / "diagnostics.json").exists()
    assert (bundle.directory / "tuwunel.log").exists()


@pytest.mark.asyncio
async def test_direct_bundle_persistence_reports_writer_failure_after_other_artifacts(
    tmp_path: Path,
) -> None:
    """A broken writer blocks success only after every independent write runs."""
    bundle = FailureBundle.create(
        tmp_path / "artifacts",
        "run-direct-writer-failure",
        scenario=_bundle_scenario(),
        provenance={},
    )
    (bundle.directory / "mindroom.log").mkdir()
    stack = _prepared_stack(tmp_path)
    oracle = _snapshot_oracle()
    runner = _bundle_runner(oracle)

    try:
        with pytest.raises(ExceptionGroup, match="failure bundle artifact write failed"):
            live_fuzz._persist_run_bundle(
                bundle,
                stack,
                runner,
                exception=AssertionError("primary invariant"),
            )
    finally:
        await oracle.client.close()

    assert (bundle.directory / "diagnostics.json").exists()
    assert (bundle.directory / "tuwunel.log").exists()
    assert "mindroom.log" in (bundle.directory / "artifact_errors.txt").read_text(encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_collector", ["diagnostics.json", "tuwunel.log"])
async def test_failure_bundle_finalizes_every_artifact_after_collector_failure(
    tmp_path: Path,
    failed_collector: str,
) -> None:
    """One collector failure leaves a sentinel without blocking finalization."""
    bundle = FailureBundle.create(
        tmp_path / "artifacts",
        f"run-{failed_collector}",
        scenario=_bundle_scenario(),
        provenance={},
    )
    stack = _prepared_stack(tmp_path)
    oracle = _snapshot_oracle()
    runner = _bundle_runner(oracle)
    collector_error = RuntimeError(f"{failed_collector} exploded")

    if failed_collector == "diagnostics.json":

        def fail_diagnostics() -> dict[str, int]:
            stack.events.append("diagnostics")
            raise collector_error

        stack.diagnostic_counts = fail_diagnostics  # type: ignore[method-assign]
    else:

        def fail_tuwunel_log() -> str:
            stack.events.append("tuwunel_log")
            raise collector_error

        stack.tuwunel_log = fail_tuwunel_log  # type: ignore[method-assign]

    try:
        with pytest.raises(ExceptionGroup, match="failure bundle collector failed"):
            live_fuzz._persist_run_bundle(
                bundle,
                stack,
                runner,
                exception=AssertionError("primary invariant"),
            )
    finally:
        await oracle.client.close()

    expected = {
        "artifact_errors.txt",
        "diagnostics.json",
        "exception.txt",
        "handled_turns.json",
        "mindroom.log",
        "model_observations.json",
        "oracle_snapshot.json",
        "tuwunel.log",
    }
    assert expected <= {path.name for path in bundle.directory.iterdir()}
    assert stack.events == ["diagnostics", "tuwunel_log"]
    assert "primary invariant" in (bundle.directory / "exception.txt").read_text(encoding="utf-8")
    assert json.loads((bundle.directory / "oracle_snapshot.json").read_text(encoding="utf-8"))["expected_sources"] == {
        "$source": "op:1",
    }
    assert (bundle.directory / "model_observations.json").exists()
    capture_errors = (bundle.directory / "artifact_errors.txt").read_text(encoding="utf-8")
    assert f"{failed_collector}: RuntimeError: {failed_collector} exploded" in capture_errors
    if failed_collector == "diagnostics.json":
        sentinel = json.loads((bundle.directory / "diagnostics.json").read_text(encoding="utf-8"))["_capture_error"]
    else:
        sentinel = (bundle.directory / "tuwunel.log").read_text(encoding="utf-8")
    assert f"<{failed_collector} capture failed: RuntimeError: {failed_collector} exploded>" in sentinel


def test_failure_bundle_records_artifact_error_after_finalize(tmp_path: Path) -> None:
    """Late evidence failures append after the main bundle was finalized."""
    bundle = FailureBundle.create(
        tmp_path / "artifacts",
        "run-late-artifact-error",
        scenario=_bundle_scenario(),
        provenance={},
    )

    def fail_writer(_destination: Path) -> None:
        message = "late artifact failed"
        raise OSError(message)

    bundle._write_isolated("late.txt", fail_writer)

    errors = (bundle.directory / "artifact_errors.txt").read_text(encoding="utf-8")
    assert "late.txt: late artifact failed" in errors


@pytest.mark.asyncio
async def test_failure_bundle_finalizes_when_stop_mindroom_fails(tmp_path: Path) -> None:
    """Failure evidence survives a MindRoom stop error during capture."""
    bundle = FailureBundle.create(
        tmp_path / "artifacts",
        "run-stop-failure",
        scenario=_bundle_scenario(),
        provenance={},
    )
    stack = _prepared_stack(tmp_path)
    oracle = _snapshot_oracle()
    runner = _bundle_runner(oracle)

    def fail_stop() -> None:
        message = "stop failed"
        raise RuntimeError(message)

    stack.stop_mindroom = fail_stop  # type: ignore[method-assign]
    try:
        _persist_failure_bundle(bundle, stack, runner, AssertionError("primary invariant"))
    finally:
        await oracle.client.close()

    expected = {
        "cleanup_error.txt",
        "diagnostics.json",
        "exception.txt",
        "handled_turns.json",
        "mindroom.log",
        "model_observations.json",
        "oracle_snapshot.json",
        "tuwunel.log",
    }
    assert expected <= {path.name for path in bundle.directory.iterdir()}
    assert "stop failed" in (bundle.directory / "cleanup_error.txt").read_text(encoding="utf-8")


def test_main_preserves_base_exception_evidence_and_closes_stack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An interrupted campaign preserves evidence and tears down its stack."""
    args = SimpleNamespace(
        sync_mode="classic",
        root_fanout=DEFAULT_ROOT_FANOUT,
        artifact_root=tmp_path / "artifacts",
        failure_log=None,
        pending_grace=0.0,
        reply_timeout=1.0,
        save_trace=None,
        seed=1,
        settle_seconds=0.0,
        trace=None,
    )
    stack_events: list[str] = []
    captured: list[BaseException] = []

    class InterruptedStack:
        log_path = tmp_path / "mindroom.log"

        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            stack_events.append("start")

        def close(self) -> None:
            stack_events.append("close")

    async def interrupt_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise KeyboardInterrupt

    monkeypatch.setattr(live_fuzz, "_parse_args", lambda: args)
    monkeypatch.setattr(live_fuzz, "_scenario_from_args", lambda _args: _bundle_scenario())
    monkeypatch.setattr(live_fuzz, "_run_provenance", lambda _revision: {})
    monkeypatch.setattr(live_fuzz, "ManagedTuwunelStack", InterruptedStack)
    monkeypatch.setattr(live_fuzz, "_run_live", interrupt_run)
    monkeypatch.setattr(
        live_fuzz,
        "_persist_failure_bundle",
        lambda _bundle, _stack, _runner, exc: captured.append(exc),
    )

    with pytest.raises(KeyboardInterrupt):
        live_fuzz.main()

    assert stack_events == ["start", "close"]
    assert len(captured) == 1
    assert isinstance(captured[0], KeyboardInterrupt)


def test_main_bad_failure_log_preserves_primary_and_closes_stack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failure-log copy error cannot replace the run failure or skip teardown."""
    failure_log = tmp_path / "failure-log"
    failure_log.mkdir()
    mindroom_log = tmp_path / "mindroom.log"
    mindroom_log.write_text("mindroom output\n", encoding="utf-8")
    args = SimpleNamespace(
        sync_mode="classic",
        root_fanout=DEFAULT_ROOT_FANOUT,
        artifact_root=tmp_path / "artifacts",
        failure_log=failure_log,
        pending_grace=0.0,
        reply_timeout=1.0,
        save_trace=None,
        seed=1,
        settle_seconds=0.0,
        trace=None,
    )
    stack_events: list[str] = []

    class InterruptedStack:
        log_path = mindroom_log

        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            stack_events.append("start")

        def close(self) -> None:
            stack_events.append("close")

    async def interrupt_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise KeyboardInterrupt

    monkeypatch.setattr(live_fuzz, "_parse_args", lambda: args)
    monkeypatch.setattr(live_fuzz, "_scenario_from_args", lambda _args: _bundle_scenario())
    monkeypatch.setattr(live_fuzz, "_run_provenance", lambda _revision: {})
    monkeypatch.setattr(live_fuzz, "ManagedTuwunelStack", InterruptedStack)
    monkeypatch.setattr(live_fuzz, "_run_live", interrupt_run)
    monkeypatch.setattr(live_fuzz, "_persist_failure_bundle", lambda *_args, **_kwargs: None)

    with pytest.raises(KeyboardInterrupt):
        live_fuzz.main()

    assert stack_events == ["start", "close"]


def test_main_bundle_capture_failure_preserves_primary_and_closes_stack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Any evidence-capture failure still preserves the run error and teardown."""
    args = SimpleNamespace(
        sync_mode="classic",
        root_fanout=DEFAULT_ROOT_FANOUT,
        artifact_root=tmp_path / "artifacts",
        failure_log=None,
        pending_grace=0.0,
        reply_timeout=1.0,
        save_trace=None,
        seed=1,
        settle_seconds=0.0,
        trace=None,
    )
    stack_events: list[str] = []

    class InterruptedStack:
        log_path = tmp_path / "mindroom.log"

        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            stack_events.append("start")

        def close(self) -> None:
            stack_events.append("close")

    async def fail_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        message = "primary invariant"
        raise AssertionError(message)

    def fail_capture(*_args: object, **_kwargs: object) -> None:
        message = "diagnostic read failed"
        raise OSError(message)

    monkeypatch.setattr(live_fuzz, "_parse_args", lambda: args)
    monkeypatch.setattr(live_fuzz, "_scenario_from_args", lambda _args: _bundle_scenario())
    monkeypatch.setattr(live_fuzz, "_run_provenance", lambda _revision: {})
    monkeypatch.setattr(live_fuzz, "ManagedTuwunelStack", InterruptedStack)
    monkeypatch.setattr(live_fuzz, "_run_live", fail_run)
    monkeypatch.setattr(live_fuzz, "_persist_failure_bundle", fail_capture)

    with pytest.raises(AssertionError, match="primary invariant"):
        live_fuzz.main()

    assert stack_events == ["start", "close"]


def test_main_stop_interrupt_preserves_primary_and_closes_stack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An interrupted evidence stage cannot mask the run error or skip close."""
    args = SimpleNamespace(
        sync_mode="classic",
        root_fanout=DEFAULT_ROOT_FANOUT,
        artifact_root=tmp_path / "artifacts",
        failure_log=None,
        pending_grace=0.0,
        reply_timeout=1.0,
        save_trace=None,
        seed=1,
        settle_seconds=0.0,
        trace=None,
    )
    storage_path = tmp_path / "mindroom_data"
    (storage_path / "tracking").mkdir(parents=True)
    log_path = tmp_path / "mindroom.log"
    log_path.write_text("mindroom output\n", encoding="utf-8")
    stack_events: list[str] = []

    class InterruptedStack:
        runtime_provenance = None

        def __init__(self, **_kwargs: object) -> None:
            self.log_path = log_path
            self.storage_path = storage_path

        def start(self) -> None:
            stack_events.append("start")

        def log_tail(self) -> str:
            return "tail"

        def stop_mindroom(self) -> None:
            stack_events.append("stop")
            raise KeyboardInterrupt

        def diagnostic_counts(self) -> dict[str, int]:
            return {}

        def tuwunel_log(self) -> str:
            return "tuwunel"

        def close(self) -> None:
            stack_events.append("close")

    async def fail_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        message = "primary invariant"
        raise AssertionError(message)

    monkeypatch.setattr(live_fuzz, "_parse_args", lambda: args)
    monkeypatch.setattr(live_fuzz, "_scenario_from_args", lambda _args: _bundle_scenario())
    monkeypatch.setattr(live_fuzz, "_run_provenance", lambda _revision: {})
    monkeypatch.setattr(live_fuzz, "ManagedTuwunelStack", InterruptedStack)
    monkeypatch.setattr(live_fuzz, "_run_live", fail_run)

    with pytest.raises(AssertionError, match="primary invariant"):
        live_fuzz.main()

    assert stack_events == ["start", "stop", "close"]
    cleanup_files = list((tmp_path / "artifacts").glob("*/cleanup_error.txt"))
    assert len(cleanup_files) == 1
    assert "KeyboardInterrupt" in cleanup_files[0].read_text(encoding="utf-8")


def test_main_rechecks_sources_after_teardown_before_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PASS revalidates final sources after teardown and before its receipt."""
    args = SimpleNamespace(
        sync_mode="classic",
        root_fanout=DEFAULT_ROOT_FANOUT,
        artifact_root=tmp_path / "artifacts",
        failure_log=None,
        pending_grace=0.0,
        reply_timeout=1.0,
        save_trace=None,
        seed=1,
        settle_seconds=0.0,
        trace=None,
    )
    events: list[str] = []

    class PassingStack:
        sync_mode = "classic"

        def __init__(self, **_kwargs: object) -> None:
            self.runtime_provenance = {
                "mindroom_source_sha256": "a" * 64,
                "mindroom_frozen_revision": "mindroom-head",
                "nio_module_sha256": "b" * 64,
                "nio_expected_version": "nio-head",
                "nio_version": "nio-head",
                "runtime_generations": [
                    {
                        "nio_module_sha256": "b" * 64,
                        "nio_expected_version": "nio-head",
                        "nio_version": "nio-head",
                        "mindroom_dirty": False,
                        "mindroom_source_sha256": "a" * 64,
                        "mindroom_expected_revision": "mindroom-head",
                        "mindroom_revision": "mindroom-head",
                    },
                ],
            }

        def start(self) -> None:
            events.append("start")

        def diagnostic_counts(self) -> dict[str, int]:
            events.append("diagnostics")
            return {}

        def revalidate_runtime_provenance(self) -> dict[str, object]:
            events.append("revalidate")
            return self.runtime_provenance

        def close(self, *, before_destructive_cleanup: Callable[[], None]) -> None:
            events.append("stop")
            before_destructive_cleanup()
            events.append("teardown")

    async def pass_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"status": "PASS"}

    def retain_receipt(
        _bundle: FailureBundle,
        _result: dict[str, object],
        _provenance: dict[str, object],
    ) -> Path:
        events.append("receipt")
        return tmp_path / "receipt.json"

    monkeypatch.setattr(live_fuzz, "_parse_args", lambda: args)
    monkeypatch.setattr(live_fuzz, "_scenario_from_args", lambda _args: _bundle_scenario())
    monkeypatch.setattr(live_fuzz, "_run_provenance", lambda _revision: {})
    monkeypatch.setattr(live_fuzz, "_required_mindroom_revision", lambda: "mindroom-head")
    monkeypatch.setattr(live_fuzz, "ManagedTuwunelStack", PassingStack)
    monkeypatch.setattr(live_fuzz, "_run_live", pass_run)
    monkeypatch.setattr(
        live_fuzz,
        "_persist_run_bundle",
        lambda *_args, **_kwargs: events.append("snapshot"),
    )
    monkeypatch.setattr(FailureBundle, "retain_pass_receipt", retain_receipt)

    live_fuzz.main()

    assert events == [
        "start",
        "diagnostics",
        "stop",
        "snapshot",
        "teardown",
        "revalidate",
        "receipt",
    ]


def test_main_cleanup_failure_retains_pre_teardown_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A passing workload snapshots disposable evidence before failed cleanup."""
    artifact_root = tmp_path / "artifacts"
    args = SimpleNamespace(
        sync_mode="classic",
        root_fanout=DEFAULT_ROOT_FANOUT,
        artifact_root=artifact_root,
        failure_log=None,
        pending_grace=0.0,
        reply_timeout=1.0,
        save_trace=None,
        seed=1,
        settle_seconds=0.0,
        trace=None,
    )
    disposable = tmp_path / "disposable"
    storage_path = disposable / "mindroom_data"
    ledger_path = storage_path / "tracking" / "event_journal.db"
    log_path = disposable / "mindroom.log"
    events: list[str] = []

    class CleanupFailingStack:
        sync_mode = "classic"
        runtime_redaction_path = tmp_path / "runtime-redactions.jsonl"

        def __init__(self, **_kwargs: object) -> None:
            self.runtime_provenance = {
                "mindroom_dirty": False,
                "mindroom_source_sha256": "a" * 64,
                "mindroom_expected_revision": "abc",
                "mindroom_frozen_revision": "abc",
                "mindroom_revision": "abc",
                "nio_module_sha256": "b" * 64,
                "nio_expected_version": "nio-head",
                "nio_version": "nio-head",
                "runtime_generations": [
                    {
                        "nio_module_sha256": "b" * 64,
                        "nio_expected_version": "nio-head",
                        "nio_version": "nio-head",
                        "mindroom_dirty": False,
                        "mindroom_source_sha256": "a" * 64,
                        "mindroom_expected_revision": "abc",
                        "mindroom_revision": "abc",
                    },
                ],
            }
            ledger_path.parent.mkdir(parents=True)
            _write_ledger(ledger_path, {})
            log_path.write_text("complete MindRoom log\n", encoding="utf-8")
            self.log_path = log_path
            self.storage_path = storage_path

        def start(self) -> None:
            events.append("start")

        def diagnostic_counts(self) -> dict[str, int]:
            events.append("diagnostics")
            return {"event_loop_stalls": 0}

        def tuwunel_log(self) -> str:
            events.append("tuwunel_log")
            return "complete Tuwunel log\n"

        def revalidate_runtime_provenance(self) -> dict[str, object]:
            self.runtime_provenance["final_source_validation"] = {
                "mindroom_dirty": False,
                "mindroom_source_sha256": "a" * 64,
                "mindroom_expected_revision": "abc",
                "mindroom_revision": "abc",
                "nio_module_sha256": "b" * 64,
                "nio_expected_version": "nio-head",
                "nio_version": "nio-head",
            }
            return self.runtime_provenance

        def close(self, *, before_destructive_cleanup: Callable[[], None]) -> None:
            events.append("stop")
            before_destructive_cleanup()
            events.append("remove")
            shutil.rmtree(disposable)
            cleanup_message = "cleanup failed"
            remove_message = "remove Tuwunel instance: failed"
            lease_message = "release host lease: failed"
            nested_message = "nested"
            raise ExceptionGroup(
                cleanup_message,
                [
                    RuntimeError(remove_message),
                    ExceptionGroup(nested_message, [OSError(lease_message)]),
                ],
            )

    async def pass_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"status": "PASS"}

    monkeypatch.setattr(live_fuzz, "_parse_args", lambda: args)
    monkeypatch.setattr(live_fuzz, "_scenario_from_args", lambda _args: _bundle_scenario())
    monkeypatch.setattr(live_fuzz, "_run_provenance", lambda _revision: {})
    monkeypatch.setattr(live_fuzz, "ManagedTuwunelStack", CleanupFailingStack)
    monkeypatch.setattr(live_fuzz, "_run_live", pass_run)

    with pytest.raises(ExceptionGroup, match="cleanup failed"):
        live_fuzz.main()

    assert not disposable.exists()
    run_directories = [path for path in artifact_root.iterdir() if path.name != "receipts"]
    assert len(run_directories) == 1
    directory = run_directories[0]
    assert {
        "cleanup_error.txt",
        "diagnostics.json",
        "handled_turns.json",
        "mindroom.log",
        "model_observations.json",
        "oracle_snapshot.json",
        "provenance.json",
        "realized_journal.jsonl",
        "scenario.json",
        "tuwunel.log",
    } <= {path.name for path in directory.iterdir()}
    assert "complete MindRoom log" in (directory / "mindroom.log").read_text(encoding="utf-8")
    assert "complete Tuwunel log" in (directory / "tuwunel.log").read_text(encoding="utf-8")
    cleanup_errors = (directory / "cleanup_error.txt").read_text(encoding="utf-8")
    assert "remove Tuwunel instance: failed" in cleanup_errors
    assert "release host lease: failed" in cleanup_errors
    assert not (artifact_root / "receipts").exists()
    assert events == ["start", "diagnostics", "stop", "diagnostics", "tuwunel_log", "remove"]


def test_main_missing_runtime_provenance_captures_bundle_before_teardown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing child attestation becomes a captured failure before destruction."""
    artifact_root = tmp_path / "artifacts"
    disposable = tmp_path / "disposable"
    storage_path = disposable / "mindroom_data"
    ledger_path = storage_path / "tracking" / "event_journal.db"
    log_path = disposable / "mindroom.log"
    ledger_path.parent.mkdir(parents=True)
    _write_ledger(ledger_path, {})
    log_path.write_text("runtime without attestation\n", encoding="utf-8")
    args = SimpleNamespace(
        sync_mode="classic",
        root_fanout=DEFAULT_ROOT_FANOUT,
        artifact_root=artifact_root,
        failure_log=None,
        pending_grace=0.0,
        reply_timeout=1.0,
        save_trace=None,
        seed=1,
        settle_seconds=0.0,
        trace=None,
    )
    events: list[str] = []

    class MissingProvenanceStack:
        runtime_provenance = None
        runtime_redaction_path = tmp_path / "runtime-redactions.jsonl"

        sync_mode = "classic"

        def __init__(self, **_kwargs: object) -> None:
            self.log_path = log_path
            self.storage_path = storage_path

        def start(self) -> None:
            events.append("start")

        def log_tail(self) -> str:
            return "runtime without attestation"

        def stop_mindroom(self) -> None:
            events.append("stop")

        def diagnostic_counts(self) -> dict[str, int]:
            events.append("diagnostics")
            return {}

        def tuwunel_log(self) -> str:
            events.append("tuwunel")
            return "tuwunel evidence\n"

        def close(self) -> None:
            events.append("close")
            shutil.rmtree(disposable)

    async def pass_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"status": "PASS"}

    monkeypatch.setattr(live_fuzz, "_parse_args", lambda: args)
    monkeypatch.setattr(live_fuzz, "_scenario_from_args", lambda _args: _bundle_scenario())
    monkeypatch.setattr(live_fuzz, "_run_provenance", lambda _revision: {})
    monkeypatch.setattr(live_fuzz, "ManagedTuwunelStack", MissingProvenanceStack)
    monkeypatch.setattr(live_fuzz, "_run_live", pass_run)

    with pytest.raises(RuntimeError, match="omitted child runtime provenance"):
        live_fuzz.main()

    directory = next(path for path in artifact_root.iterdir() if path.is_dir())
    assert not disposable.exists()
    assert "runtime without attestation" in (directory / "mindroom.log").read_text(encoding="utf-8")
    assert "omitted child runtime provenance" in (directory / "exception.txt").read_text(encoding="utf-8")
    assert f"Live Matrix fuzz failure bundle: {directory}" in capsys.readouterr().err
    assert events == ["start", "diagnostics", "stop", "diagnostics", "tuwunel", "close"]


def test_failure_bundle_appends_every_cleanup_error(tmp_path: Path) -> None:
    """Multiple teardown failures remain visible in occurrence order."""
    bundle = FailureBundle.create(
        tmp_path / "artifacts",
        "run-cleanup-errors",
        scenario=_bundle_scenario(),
        provenance={},
    )

    bundle.record_cleanup_error(OSError("failure-log failed"))
    bundle.record_cleanup_error(
        ExceptionGroup(
            "close failed",
            [
                RuntimeError("remove instance: docker failed"),
                RuntimeError("release lease: lock failed"),
            ],
        ),
    )

    cleanup_errors = (bundle.directory / "cleanup_error.txt").read_text(encoding="utf-8")
    assert "OSError: failure-log failed" in cleanup_errors
    assert "ExceptionGroup: close failed" in cleanup_errors
    assert "RuntimeError: remove instance: docker failed" in cleanup_errors
    assert "RuntimeError: release lease: lock failed" in cleanup_errors
    assert cleanup_errors.index("failure-log failed") < cleanup_errors.index("close failed")


@pytest.mark.asyncio
async def test_sanitized_oracle_snapshot_excludes_tokens_and_sync_state() -> None:
    """The snapshot keeps opaque IDs but never sync tokens or access tokens."""
    oracle = _snapshot_oracle()
    try:
        snapshot = _sanitized_oracle_snapshot(oracle)
    finally:
        await oracle.client.close()

    serialized = json.dumps(snapshot)
    assert "s_secret_sync_token" not in serialized
    assert "next_batch" not in snapshot
    assert "access_token" not in serialized
    assert snapshot["expected_sources"] == {"$source": "op:1"}
    assert snapshot["response_ids"] == {"$source": ["$response"]}


@pytest.mark.asyncio
async def test_failure_bundle_snapshot_omits_sync_state_end_to_end(tmp_path: Path) -> None:
    """The persisted oracle snapshot carries no raw Matrix sync state."""
    bundle = FailureBundle.create(
        tmp_path / "artifacts",
        "run-4",
        scenario=_bundle_scenario(),
        provenance={},
    )
    stack = _prepared_stack(tmp_path)
    oracle = _snapshot_oracle()
    runner = _bundle_runner(oracle)

    try:
        _persist_failure_bundle(bundle, stack, runner, AssertionError("boom"))
    finally:
        await oracle.client.close()

    snapshot_text = (bundle.directory / "oracle_snapshot.json").read_text(encoding="utf-8")
    assert "s_secret_sync_token" not in snapshot_text
    assert "next_batch" not in snapshot_text


def _test_runtime_attestation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide real package files at controlled runtime and distribution paths."""
    project = tmp_path / "mindroom"
    module = project / "src" / "mindroom" / "__init__.py"
    module.parent.mkdir(parents=True)
    module.write_text("# runtime\n", encoding="utf-8")
    nio_module = tmp_path / "installed" / "nio" / "__init__.py"
    nio_module.parent.mkdir(parents=True)
    nio_module.write_text("# installed package\n", encoding="utf-8")
    project.joinpath("uv.lock").write_text('[[package]]\nname = "mindroom-nio"\nversion = "1.0.0"\n', encoding="utf-8")
    monkeypatch.setattr(live_fuzz.nio, "__file__", str(nio_module))
    monkeypatch.setattr(live_fuzz, "PROJECT_ROOT", project)
    monkeypatch.setattr(live_fuzz, "_git_root_for_path", lambda _path: project)
    monkeypatch.setattr(live_fuzz, "_git_state_for_file", lambda *_args, **_kwargs: ("mindroom-head", False))
    monkeypatch.setattr(live_fuzz, "_git_revision", lambda _path: "mindroom-head")
    monkeypatch.setattr(live_fuzz, "_mindroom_source_sha256", lambda: "a" * 64)
    monkeypatch.setattr(live_fuzz, "version", lambda _name: "1.0.0")
    monkeypatch.setattr(live_fuzz, "distribution", lambda _name: SimpleNamespace(locate_file=lambda _name: nio_module))
    attestation = tmp_path / "runtime-attestation.json"
    attestation.write_text(
        json.dumps(
            {
                "mindroom_module_path": str(module),
                "nio_module_path": str(nio_module),
                "nio_version": "1.0.0",
                "python": "3.13",
            },
        ),
        encoding="utf-8",
    )
    return attestation


def test_child_provenance_uses_locked_installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The receipt binds the loaded package to the lockfile and its actual bytes."""
    attestation = _test_runtime_attestation(tmp_path, monkeypatch)
    provenance = _validated_child_provenance(attestation, expected_mindroom_revision="mindroom-head")
    assert provenance["nio_version"] == provenance["nio_expected_version"] == "1.0.0"
    assert len(provenance["nio_module_sha256"]) == 64
    assert provenance["mindroom_source_sha256"] == "a" * 64


@pytest.mark.parametrize("changed", ["loaded", "installed", "lockfile"])
def test_child_provenance_rejects_version_changed_after_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    """Neither a different loaded version nor a changed installation proves the lock pin."""
    attestation = _test_runtime_attestation(tmp_path, monkeypatch)
    if changed == "loaded":
        payload = json.loads(attestation.read_text(encoding="utf-8"))
        payload["nio_version"] = "2.0.0"
        attestation.write_text(json.dumps(payload), encoding="utf-8")
    elif changed == "installed":
        monkeypatch.setattr(live_fuzz, "version", lambda _name: "2.0.0")
    else:
        (live_fuzz.PROJECT_ROOT / "uv.lock").write_text(
            '[[package]]\nname = "mindroom-nio"\nversion = "2.0.0"\n',
            encoding="utf-8",
        )
    with pytest.raises(RuntimeError, match=r"version does not match uv\.lock"):
        _validated_child_provenance(attestation, expected_mindroom_revision="mindroom-head")


def test_restart_rejects_mindroom_head_move_and_keeps_first_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replacement process cannot silently change the tested runtime revision."""
    attestation = _test_runtime_attestation(tmp_path, monkeypatch)
    stack = ManagedTuwunelStack(state_root=tmp_path / "state", mindroom_revision="mindroom-head")
    stack.attestation_path = attestation
    monkeypatch.setattr(stack, "_tuwunel_provenance", dict)
    try:
        stack._wait_for_runtime_attestation()
        first = dict(stack.runtime_provenance or {})
        monkeypatch.setattr(live_fuzz, "_git_state_for_file", lambda *_args, **_kwargs: ("moved-head", False))
        with pytest.raises(RuntimeError, match="expected mindroom-head, loaded moved-head"):
            stack._wait_for_runtime_attestation()
        assert stack.runtime_provenance == first
        assert len(first["runtime_generations"]) == 1
    finally:
        stack.temp_dir.cleanup()


def test_child_provenance_records_worktree_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Uncommitted inputs are explicit in the receipt and retain a content fingerprint."""
    attestation = _test_runtime_attestation(tmp_path, monkeypatch)
    monkeypatch.setattr(live_fuzz, "_git_state_for_file", lambda *_args, **_kwargs: ("mindroom-head", True))
    result = _validated_child_provenance(attestation, expected_mindroom_revision="mindroom-head")
    assert result["mindroom_dirty"] is True
    assert result["mindroom_source_sha256"] == "a" * 64


@pytest.mark.parametrize("changed", ["mindroom", "nio"])
def test_final_source_recheck_rejects_post_attestation_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    """Changes after startup invalidate the final receipt even if versions stay equal."""
    attestation = _test_runtime_attestation(tmp_path, monkeypatch)
    stack = ManagedTuwunelStack(state_root=tmp_path / "state", mindroom_revision="mindroom-head")
    stack.attestation_path = attestation
    monkeypatch.setattr(stack, "_tuwunel_provenance", dict)
    try:
        stack._wait_for_runtime_attestation()
        if changed == "mindroom":
            monkeypatch.setattr(live_fuzz, "_mindroom_source_sha256", lambda: "b" * 64)
        else:
            module = Path(json.loads(attestation.read_text(encoding="utf-8"))["nio_module_path"])
            module.write_text("# changed after startup\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match=r"changed|match"):
            stack.revalidate_runtime_provenance()
        assert "final_source_validation" not in (stack.runtime_provenance or {})
    finally:
        stack.temp_dir.cleanup()
