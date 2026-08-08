"""Tests for replayable real-server Matrix fuzz traces and their oracle."""

from __future__ import annotations

import asyncio
import gc
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

import httpx
import pytest
import yaml

from mindroom.config.main import Config
from mindroom.event_journal import DeliveryStage, EventClass, EventJournalStore, EventKind, InboundEvent
from mindroom.matrix.conversation_hydration import ConversationHydrator
from mindroom.matrix.sync_continuity import SyncContinuityStore
from mindroom.matrix.sync_token_values import SyncCheckpoint
from scripts.testing import fuzz_live_matrix
from scripts.testing.fuzz_live_matrix import (
    DEFAULT_ROOT_FANOUT,
    DIAGNOSTIC_MARKERS,
    ORDERLY_SHUTDOWN_MARKER,
    PROJECT_ROOT,
    RECOVERY_CLIFF_MIN_ACTIVE_STREAM_SECONDS,
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
    ManagedTuwunelStack,
    MissingReplyStage,
    OutboxRow,
    RecoveryCliffDrainCounts,
    RecoveryCliffFaultShape,
    RecoveryCliffHealthSample,
    RecoveryCliffObservation,
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
    audit_recovery_cliff_events,
    classify_missing_reply,
    collect_host_load_report,
    evaluate_recovery_cliff,
    evaluate_restart_regression,
    evaluate_sustained_stream_capacity,
    live_scenario_from_seed,
    recovery_cliff_fault_shape,
    recovery_cliff_scenario,
    restart_regression_scenario,
    short_stream_correctness_scenario,
    sustained_stream_capacity_scenario,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


class _RecordingDormantClient:
    room_id = "!restart:example"

    def __init__(self) -> None:
        self.sent_payloads: list[tuple[str, str, dict[str, Any]]] = []

    @property
    def sent_txn_ids(self) -> list[str]:
        return [txn_id for _event_type, txn_id, _content in self.sent_payloads]

    async def create_public_room(self) -> None:
        return

    async def send_event(self, event_type: str, txn_id: str, content: dict[str, Any]) -> str:
        self.sent_payloads.append((event_type, txn_id, content))
        return f"${txn_id}"


class _RecoveryCliffBoundaryClient:
    """Fail if recovery-cliff falls through to disposable registration."""

    room_id = "!recovery:example"
    _REGISTER_MESSAGE = "recovery-cliff must use persisted managed credentials"

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


class _RecoveryCliffLaunchBarrierClient:
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


class _RecoveryCliffHeldLoadClient:
    """Require every held context event before the simultaneous root barrier."""

    room_id = "!recovery:example"

    def __init__(self, *, context_events: int, fail_context: bool = False) -> None:
        self.context_events = context_events
        self.fail_context = fail_context
        self.context_payloads: list[tuple[str, dict[str, Any]]] = []
        self.root_payloads: list[tuple[str, dict[str, Any]]] = []
        self.all_roots_entered = asyncio.Event()

    async def send_event(self, event_type: str, txn_id: str, content: dict[str, Any]) -> str:
        assert event_type == "m.room.message"
        if content["msgtype"] == "m.notice":
            assert not self.root_payloads
            self.context_payloads.append((txn_id, content))
            if self.fail_context and len(self.context_payloads) == 1:
                msg = "held context send failed"
                raise RuntimeError(msg)
            return f"${txn_id}"
        assert len(self.context_payloads) == self.context_events
        self.root_payloads.append((txn_id, content))
        if len(self.root_payloads) == 100:
            self.all_roots_entered.set()
        await self.all_roots_entered.wait()
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
    """Build one raw event retained by the recovery-cliff observer."""
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
    omitted_original = _recovery_original("$omitted-original", "$source", 1_000, "streaming")
    compacted_edit = _recovery_edit(
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
        audit = audit_recovery_cliff_events(
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

    assert seen == set(LiveOperationKind)


def test_short_stream_correctness_scenario_matches_original_two_phase_workload() -> None:
    """Short-stream correctness preserves the old hot-then-parallel workload."""
    scenario = short_stream_correctness_scenario()

    assert scenario.profile == "short-stream-correctness"
    assert scenario.thread_count == 13
    assert len(scenario.batches) == 108
    assert all(len(batch) == 1 and batch[0].thread == 0 for batch in scenario.batches[:100])
    assert all([operation.thread for operation in batch] == list(range(1, 13)) for batch in scenario.batches[100:])


def test_recovery_cliff_scenario_has_fixed_empty_trace_for_one_hundred_roots() -> None:
    """The recovery profile owns its fixed 100-root workload outside the trace."""
    scenario = recovery_cliff_scenario()

    assert scenario == LiveFuzzScenario(thread_count=100, batches=(), profile="recovery-cliff")
    scenario.validate()


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


def test_recovery_cliff_cli_keeps_its_default_root_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recovery profile must default to 100 roots when --threads is omitted."""
    monkeypatch.setattr(sys, "argv", ["fuzz_live_matrix.py", "--profile", "recovery-cliff"])

    scenario = fuzz_live_matrix._scenario_from_args(fuzz_live_matrix._parse_args())

    assert scenario == LiveFuzzScenario(thread_count=100, batches=(), profile="recovery-cliff")


def test_recovery_cliff_cli_can_raise_the_capacity_root_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """A capacity run must reach 200 roots without weakening the fixed empty trace."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fuzz_live_matrix.py",
            "--profile",
            "recovery-cliff",
            "--threads",
            "200",
        ],
    )

    scenario = fuzz_live_matrix._scenario_from_args(fuzz_live_matrix._parse_args())

    assert scenario == LiveFuzzScenario(thread_count=200, batches=(), profile="recovery-cliff")
    scenario.validate()


def test_fuzz_cli_keeps_its_default_thread_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Making recovery capacity configurable must leave ordinary fuzz at 45 threads."""
    monkeypatch.setattr(sys, "argv", ["fuzz_live_matrix.py"])

    scenario = fuzz_live_matrix._scenario_from_args(fuzz_live_matrix._parse_args())

    assert scenario.profile == "fuzz"
    assert scenario.thread_count == 45


def test_recovery_cliff_scenario_rejects_an_altered_trace_shape() -> None:
    """The fixed recovery runner must not silently ignore declared trace operations."""
    scenario = LiveFuzzScenario(
        thread_count=100,
        batches=((LiveOperation(0, LiveOperationKind.REACTION, 0, "root:0"),),),
        profile="recovery-cliff",
    )

    with pytest.raises(ValueError, match="fixed empty trace"):
        scenario.validate()


def test_recovery_cliff_fault_shape_exceeds_one_live_window_and_one_recovery_pump() -> None:
    """The held burst must leave more history than one bounded recovery pump can close."""
    stack = ManagedTuwunelStack(profile="recovery-cliff")
    try:
        stack.config_path.write_text(
            "matrix_sync:\n  mode: sliding\n  sliding_timeline_limit: 100\n",
            encoding="utf-8",
        )

        shape = recovery_cliff_fault_shape(stack.config_path, root_count=100)

        assert shape == RecoveryCliffFaultShape(
            timeline_limit=100,
            recovery_max_pages=10,
            recovery_page_size=50,
            recovery_max_events=2_000,
            context_event_count=601,
            root_count=100,
        )
        assert shape.context_event_count + shape.root_count == 701
        recovered_event_count = shape.context_event_count + shape.root_count - shape.timeline_limit
        assert recovered_event_count <= shape.recovery_max_events
    finally:
        stack.close()


def test_recovery_cliff_fault_shape_accepts_the_recovery_cap_and_refuses_the_first_event_beyond_it() -> None:
    """A capacity search must not misreport an over-cap trace as runtime collapse."""
    stack = ManagedTuwunelStack(profile="recovery-cliff")
    try:
        stack.config_path.write_text(
            "matrix_sync:\n  mode: sliding\n  sliding_timeline_limit: 100\n",
            encoding="utf-8",
        )

        shape = recovery_cliff_fault_shape(stack.config_path, root_count=1_499)
        assert shape.context_event_count + shape.root_count - shape.timeline_limit == shape.recovery_max_events

        with pytest.raises(ValueError, match="exceeds nio's configured room recovery cap"):
            recovery_cliff_fault_shape(stack.config_path, root_count=1_500)
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_managed_load_authenticates_the_sender_without_registering() -> None:
    """Managed load must use the persisted sender instead of creating an account."""
    stack = ManagedTuwunelStack(profile="recovery-cliff")
    client = _RecoveryCliffBoundaryClient()
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", client),),
        recovery_cliff_scenario(),
        reply_timeout=1,
        settle_seconds=0,
    )
    try:
        stack.storage_path.mkdir()
        (stack.storage_path / "matrix_state.yaml").write_text(
            yaml.safe_dump(
                {
                    "accounts": {
                        "agent_load_sender": {
                            "access_token": "managed-sender-token",
                            "device_id": "MANAGED-SENDER-DEVICE",
                        },
                    },
                },
            ),
            encoding="utf-8",
        )

        await runner._authenticate_managed_sender()

        assert client.access_token == "managed-sender-token"  # noqa: S105 - fake live-test token
        assert client.calls == ["join_room"]
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_recovery_cliff_run_dispatches_before_disposable_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moving recovery dispatch below generic register makes this regression fail."""
    stack = ManagedTuwunelStack(profile="recovery-cliff")
    client = _RecoveryCliffBoundaryClient()
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", client),),
        recovery_cliff_scenario(),
        reply_timeout=1,
        settle_seconds=0,
    )
    calls: list[str] = []

    async def run_recovery() -> dict[str, float | int | str]:
        calls.append("recovery")
        return {"profile": "recovery-cliff", "status": "PASS"}

    monkeypatch.setattr(runner, "_run_recovery_cliff", run_recovery)
    try:
        assert await runner.run() == {"profile": "recovery-cliff", "status": "PASS"}
        assert calls == ["recovery"]
        assert client.calls == []
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_sustained_stream_capacity_run_dispatches_before_disposable_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capacity must select managed credentials before the disposable-user path."""
    stack = ManagedTuwunelStack(profile="sustained-stream-capacity")
    client = _RecoveryCliffBoundaryClient()
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", client),),
        sustained_stream_capacity_scenario(),
        reply_timeout=1,
        settle_seconds=0,
    )
    calls: list[str] = []

    async def run_capacity() -> dict[str, float | int | str]:
        calls.append("capacity")
        return {"profile": "sustained-stream-capacity", "status": "PASS"}

    monkeypatch.setattr(runner, "_run_sustained_stream_capacity", run_capacity, raising=False)
    try:
        assert await runner.run() == {"profile": "sustained-stream-capacity", "status": "PASS"}
        assert calls == ["capacity"]
        assert client.calls == []
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_sustained_stream_capacity_releases_two_hundred_roots_without_fault_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every no-fault root enters one gather and carries one exact marker and mention."""
    stack = ManagedTuwunelStack(profile="sustained-stream-capacity")
    stack.agent_id = "@mindroom_general:example"
    client = _RecoveryCliffLaunchBarrierClient(expected_sends=200)
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", client),),
        sustained_stream_capacity_scenario(),
        reply_timeout=1,
        settle_seconds=0,
    )
    fault_calls: list[str] = []
    stack.pause_mindroom = lambda **_kwargs: fault_calls.append("pause")  # type: ignore[method-assign]

    async def release_fault_load(**_kwargs: object) -> tuple[str, ...]:
        fault_calls.append("recovery-load")
        return ()

    monkeypatch.setattr(runner, "_release_recovery_cliff_load", release_fault_load)
    try:
        released = await asyncio.wait_for(
            runner._release_managed_roots(
                run_id="unit-run",
                deadline=time.monotonic() + 1,
                transaction_prefix="sustained-stream-capacity-root",
                body_prefix="Sustained stream capacity",
            ),
            timeout=1,
        )

        assert fault_calls == []
        assert len(client.sent_payloads) == 200
        assert len(released) == 200
        root_payloads = {
            txn_id: content
            for event_type, txn_id, content in client.sent_payloads
            if event_type == "m.room.message" and content["msgtype"] == "m.text"
        }
        assert len(root_payloads) == 200
        observed_markers: set[str] = set()
        for thread in range(200):
            content = root_payloads[f"sustained-stream-capacity-root-unit-run-{thread}"]
            marker = f"run=unit-run thread={thread}"
            assert content["body"].count(marker) == 1
            observed_markers.add(marker)
            assert content["m.mentions"] == {"user_ids": [stack.agent_id]}
        assert len(observed_markers) == 200
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_sustained_stream_capacity_samples_liveness_while_root_gather_is_outstanding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The root gather cannot finish before an in-flight health/process sample."""
    stack = ManagedTuwunelStack(profile="sustained-stream-capacity")
    stack.agent_id = "@mindroom_general:example"
    client = _RecoveryCliffLaunchBarrierClient(expected_sends=200, finish=False)
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", client),),
        sustained_stream_capacity_scenario(),
        reply_timeout=1,
        settle_seconds=0,
    )
    sampled: list[RecoveryCliffHealthSample] = []
    pause_calls: list[str] = []
    stack.pause_mindroom = lambda **_kwargs: pause_calls.append("pause")  # type: ignore[method-assign]

    async def observer_step(
        *,
        deadline: float,
        health_samples: list[RecoveryCliffHealthSample],
    ) -> RecoveryCliffHealthSample:
        assert deadline > time.monotonic()
        await client.all_entered.wait()
        assert client.all_entered.is_set()
        assert len(client.sent_payloads) == 200
        sample = RecoveryCliffHealthSample(True, datetime(2026, 8, 8, tzinfo=UTC))
        health_samples.append(sample)
        sampled.append(sample)
        client.never.set()
        await asyncio.sleep(0)
        return sample

    monkeypatch.setattr(runner, "_recovery_cliff_observer_step", observer_step)
    try:
        released = await runner._release_sustained_stream_capacity_roots(
            run_id="unit-run",
            deadline=time.monotonic() + 5,
            health_samples=[],
        )

        assert len(released) == 200
        assert sampled
        assert pause_calls == []
    finally:
        client.never.set()
        stack.close()


@pytest.mark.asyncio
async def test_sustained_stream_capacity_fast_root_gather_still_samples_concurrent_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every root must enter before health begins, and no send may finish before health starts."""
    stack = ManagedTuwunelStack(profile="sustained-stream-capacity")
    stack.agent_id = "@mindroom_general:example"
    order: list[str] = []

    class FastClient(_RecoveryCliffBoundaryClient):
        async def send_event(self, event_type: str, txn_id: str, content: dict[str, Any]) -> str:
            del event_type, content
            order.append(f"send:{txn_id}")
            return f"${txn_id}"

    client = FastClient()
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", client),),
        sustained_stream_capacity_scenario(root_count=2),
        reply_timeout=1,
        settle_seconds=0,
    )
    health_samples: list[RecoveryCliffHealthSample] = []

    async def observer_step(**kwargs: object) -> RecoveryCliffHealthSample:
        order.append("health:started")
        sample = RecoveryCliffHealthSample(True, datetime(2026, 8, 8, tzinfo=UTC))
        cast("list[RecoveryCliffHealthSample]", kwargs["health_samples"]).append(sample)
        await asyncio.sleep(0)
        return sample

    barrier_type = fuzz_live_matrix._ManagedRootLaunchBarrier
    wait_for_release = barrier_type.wait_for_release

    async def record_root_entry(barrier: object) -> None:
        order.append("root:entered")
        await wait_for_release(barrier)

    monkeypatch.setattr(barrier_type, "wait_for_release", record_root_entry)
    monkeypatch.setattr(runner, "_recovery_cliff_observer_step", observer_step)
    try:
        released = await runner._release_sustained_stream_capacity_roots(
            run_id="unit-run",
            deadline=time.monotonic() + 1,
            health_samples=health_samples,
        )

        assert released == (
            "$sustained-stream-capacity-root-unit-run-0",
            "$sustained-stream-capacity-root-unit-run-1",
        )
        assert health_samples
        assert order[:5] == [
            "root:entered",
            "root:entered",
            "health:started",
            "send:sustained-stream-capacity-root-unit-run-0",
            "send:sustained-stream-capacity-root-unit-run-1",
        ]
    finally:
        stack.close()


@pytest.mark.parametrize("primary_exception", [RuntimeError, asyncio.CancelledError])
@pytest.mark.asyncio
async def test_sustained_stream_capacity_consumes_concurrent_release_failure(
    monkeypatch: pytest.MonkeyPatch,
    primary_exception: type[BaseException],
) -> None:
    """Health failure or cancellation must not leak a simultaneous root-task exception."""
    stack = ManagedTuwunelStack(profile="sustained-stream-capacity")
    stack.agent_id = "@mindroom_general:example"
    all_sends_entered = asyncio.Event()
    fail_sends = asyncio.Event()
    all_sends_failed = asyncio.Event()
    send_count = 0
    failed_send_count = 0

    class ConcurrentFailureClient(_RecoveryCliffBoundaryClient):
        async def send_event(self, event_type: str, txn_id: str, content: dict[str, Any]) -> str:
            nonlocal send_count, failed_send_count
            del event_type, txn_id, content
            send_count += 1
            if send_count == 2:
                all_sends_entered.set()
            await fail_sends.wait()
            failed_send_count += 1
            if failed_send_count == 2:
                all_sends_failed.set()
            msg = "root release failed"
            raise ValueError(msg)

    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", ConcurrentFailureClient()),),
        sustained_stream_capacity_scenario(root_count=2),
        reply_timeout=1,
        settle_seconds=0,
    )

    async def fail_health(**_kwargs: object) -> RecoveryCliffHealthSample:
        await all_sends_entered.wait()
        fail_sends.set()
        await all_sends_failed.wait()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        msg = "initial health failed"
        raise primary_exception(msg)

    monkeypatch.setattr(runner, "_recovery_cliff_observer_step", fail_health)
    loop = asyncio.get_running_loop()
    loop_exceptions: list[dict[str, object]] = []
    previous_exception_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_exceptions.append(context))

    async def run_and_assert_primary_failure() -> None:
        with pytest.raises(primary_exception, match="initial health failed") as raised:
            await runner._release_sustained_stream_capacity_roots(
                run_id="unit-run",
                deadline=time.monotonic() + 1,
                health_samples=[],
            )
        raised.value.__traceback__ = None

    try:
        await run_and_assert_primary_failure()
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_exception_handler)
        stack.close()

    assert loop_exceptions == []


@pytest.mark.asyncio
async def test_recovery_cliff_releases_configured_two_hundred_roots_in_one_gather() -> None:
    """A sequential root sender deadlocks before all 200 root sends are entered."""
    stack = ManagedTuwunelStack(profile="recovery-cliff")
    stack.agent_id = "@mindroom_general:example"
    client = _RecoveryCliffLaunchBarrierClient(expected_sends=200)
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", client),),
        recovery_cliff_scenario(root_count=200),
        reply_timeout=1,
        settle_seconds=0,
    )
    try:
        released = await asyncio.wait_for(
            runner._release_managed_roots(
                run_id="unit-run",
                deadline=time.monotonic() + 1,
                transaction_prefix="recovery-cliff-root",
                body_prefix="Recovery cliff",
            ),
            timeout=1,
        )

        assert len(client.sent_payloads) == 200
        assert len(released) == 200
        root_payloads = {
            txn_id: content
            for event_type, txn_id, content in client.sent_payloads
            if event_type == "m.room.message" and content["msgtype"] == "m.text"
        }
        assert len(root_payloads) == 200
        for thread in range(200):
            content = root_payloads[f"recovery-cliff-root-unit-run-{thread}"]
            assert f"run=unit-run thread={thread}" in content["body"]
            assert content["m.mentions"] == {"user_ids": [stack.agent_id]}
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_recovery_cliff_holds_context_before_releasing_roots_and_always_resumes() -> None:
    """The stopped runtime sees 601 notices before the exact 100-root barrier."""
    stack = ManagedTuwunelStack(profile="recovery-cliff")
    stack.agent_id = "@mindroom_general:example"
    client = _RecoveryCliffHeldLoadClient(context_events=601)
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", client),),
        recovery_cliff_scenario(),
        reply_timeout=1,
        settle_seconds=0,
    )
    lifecycle: list[str] = []
    stack.pause_mindroom = lambda **_kwargs: lifecycle.append("pause")  # type: ignore[method-assign]
    stack.resume_mindroom = lambda: lifecycle.append("resume")  # type: ignore[method-assign]
    shape = RecoveryCliffFaultShape(100, 10, 50, 2_000, 601, 100)
    try:
        released = await asyncio.wait_for(
            runner._release_recovery_cliff_load(
                run_id="unit-run",
                deadline=time.monotonic() + 1,
                shape=shape,
            ),
            timeout=2,
        )

        assert lifecycle == ["pause", "resume"]
        assert len(client.context_payloads) == 601
        assert len(client.root_payloads) == 100
        assert len(released) == 100
        assert all(content["m.mentions"] == {"user_ids": []} for _txn, content in client.context_payloads)
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_recovery_cliff_resumes_the_process_group_when_a_held_send_fails() -> None:
    """No Matrix send failure may strand the managed runtime under SIGSTOP."""
    stack = ManagedTuwunelStack(profile="recovery-cliff")
    client = _RecoveryCliffHeldLoadClient(context_events=601, fail_context=True)
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", client),),
        recovery_cliff_scenario(),
        reply_timeout=1,
        settle_seconds=0,
    )
    lifecycle: list[str] = []
    stack.pause_mindroom = lambda **_kwargs: lifecycle.append("pause")  # type: ignore[method-assign]
    stack.resume_mindroom = lambda: lifecycle.append("resume")  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="held context send failed"):
            await runner._release_recovery_cliff_load(
                run_id="unit-run",
                deadline=time.monotonic() + 1,
                shape=RecoveryCliffFaultShape(100, 10, 50, 2_000, 601, 100),
            )

        assert lifecycle == ["pause", "resume"]
        assert client.root_payloads == []
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_recovery_cliff_root_gather_is_bounded_by_the_fixed_sla() -> None:
    """Entered but unfinished homeserver sends cannot outlive the one SLA."""
    stack = ManagedTuwunelStack(profile="recovery-cliff")
    stack.agent_id = "@mindroom_general:example"
    client = _RecoveryCliffLaunchBarrierClient(expected_sends=100, finish=False)
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", client),),
        recovery_cliff_scenario(),
        reply_timeout=1,
        settle_seconds=0,
    )
    asyncio.get_running_loop().call_later(0.2, client.never.set)
    try:
        with pytest.raises(TimeoutError):
            await runner._release_managed_roots(
                run_id="unit-run",
                deadline=time.monotonic() + 0.075,
                transaction_prefix="recovery-cliff-root",
                body_prefix="Recovery cliff",
            )
        assert len(client.sent_payloads) == 100
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_recovery_cliff_health_and_sync_poll_cannot_overrun_the_fixed_sla(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow health request is governed by the load SLA, not its own timeout."""
    stack = ManagedTuwunelStack(profile="recovery-cliff")
    client = _RecoveryCliffBoundaryClient()
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", client),),
        recovery_cliff_scenario(),
        reply_timeout=1,
        settle_seconds=0,
    )
    health_release = threading.Event()

    def blocked_health() -> RecoveryCliffHealthSample:
        health_release.wait(timeout=1)
        return RecoveryCliffHealthSample(
            healthy=True,
            last_sync_time=datetime(2026, 8, 7, tzinfo=UTC),
        )

    monkeypatch.setattr(stack, "recovery_health_sample", blocked_health)
    asyncio.get_running_loop().call_later(0.2, health_release.set)
    try:
        with pytest.raises(TimeoutError):
            await runner._recovery_cliff_observer_step(
                deadline=time.monotonic() + 0.075,
                health_samples=[],
            )
        assert client.sync_calls == 0
    finally:
        health_release.set()
        stack.close()


@pytest.mark.asyncio
async def test_recovery_cliff_observer_step_uses_the_complete_incremental_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moving the observer back to strict no-backfill sync must reintroduce this failure."""
    stack = ManagedTuwunelStack(profile="recovery-cliff")
    client = _RecoveryCliffBoundaryClient()
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", client),),
        recovery_cliff_scenario(),
        reply_timeout=1,
        settle_seconds=0,
    )
    monkeypatch.setattr(stack, "require_runtime_alive", lambda: None)
    monkeypatch.setattr(
        stack,
        "recovery_health_sample",
        lambda: RecoveryCliffHealthSample(True, datetime(2026, 8, 7, tzinfo=UTC)),
    )
    try:
        await runner._recovery_cliff_observer_step(
            deadline=time.monotonic() + 1,
            health_samples=[],
        )

        assert client.complete_sync_calls == 1
        assert client.sync_calls == 0
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_recovery_cliff_limited_backfill_cannot_overrun_the_fixed_sla(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every `/messages` page remains causally bounded by the workload deadline."""
    stack = ManagedTuwunelStack(profile="recovery-cliff")
    client = LiveMatrixClient("http://matrix.invalid", "!recovery:example")
    client.next_batch = "s-before"
    client.seen_events = {"$known": _observer_event("$known", "completed")}
    runner = LiveFuzzRunner(
        stack,
        (client,),
        recovery_cliff_scenario(),
        reply_timeout=1,
        settle_seconds=0,
    )
    page_release = asyncio.Event()

    async def limited_sync(_since: str | None, *, timeout_ms: int) -> dict[str, Any]:
        assert timeout_ms <= 250
        return {
            "next_batch": "s-after",
            "rooms": {
                "join": {
                    client.room_id: {
                        "timeline": {
                            "limited": True,
                            "prev_batch": "p-start",
                            "events": [_observer_event("$newest")],
                        },
                    },
                },
            },
        }

    async def blocked_messages(
        _method: str,
        _path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str | int] | None = None,
    ) -> dict[str, Any]:
        assert json_body is None
        assert params == {"dir": "f", "from": "s-before", "to": "s-after", "limit": 500}
        await page_release.wait()
        return {"start": "p-start", "end": "p-next", "chunk": []}

    monkeypatch.setattr(stack, "require_runtime_alive", lambda: None)
    monkeypatch.setattr(
        stack,
        "recovery_health_sample",
        lambda: RecoveryCliffHealthSample(True, datetime(2026, 8, 7, tzinfo=UTC)),
    )
    monkeypatch.setattr(client, "sync", limited_sync)
    monkeypatch.setattr(client, "_request", blocked_messages)
    asyncio.get_running_loop().call_later(0.2, page_release.set)
    try:
        with pytest.raises(TimeoutError):
            await runner._recovery_cliff_observer_step(
                deadline=time.monotonic() + 0.075,
                health_samples=[],
            )

        assert client.next_batch == "s-before"
        assert set(client.seen_events) == {"$known"}
    finally:
        page_release.set()
        await client.close()
        stack.close()


@pytest.mark.asyncio
async def test_recovery_cliff_samples_post_fence_health_after_exact_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The advancing health timestamp must be observed after fence settlement."""
    stack = ManagedTuwunelStack(profile="recovery-cliff")
    client = _RecoveryCliffBoundaryClient()
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", client),),
        recovery_cliff_scenario(),
        reply_timeout=1,
        settle_seconds=0,
    )
    order: list[str] = []
    before = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    after = datetime(2026, 8, 7, 12, 1, tzinfo=UTC)
    health_times = iter((before, after))

    def sample_health() -> RecoveryCliffHealthSample:
        order.append("health")
        return RecoveryCliffHealthSample(healthy=True, last_sync_time=next(health_times))

    def reaction_state(event_id: str) -> str:
        assert event_id == "$reaction"
        order.append("settled-query")
        return "settled"

    async def send_reaction(event_type: str, txn_id: str, content: dict[str, Any]) -> str:
        del event_type, txn_id, content
        order.append("send-reaction")
        return "$reaction"

    monkeypatch.setattr(stack, "require_runtime_alive", lambda: None)
    monkeypatch.setattr(stack, "recovery_health_sample", sample_health)
    monkeypatch.setattr(stack, "recovery_reaction_state", reaction_state)
    monkeypatch.setattr(client, "send_event", send_reaction)
    try:
        settled, pre_fence, post_fence = await runner._wait_for_recovery_cliff_fence(
            target_event_id="$response",
            run_id="unit-run",
            deadline=time.monotonic() + 1,
            health_samples=[],
        )

        assert settled is True
        assert (pre_fence, post_fence) == (before, after)
        assert order == ["health", "send-reaction", "settled-query", "health"]
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_recovery_cliff_terminal_wait_samples_exact_workload_outbox_debt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient attempted FINAL row must be retained before the terminal audit passes."""
    stack = ManagedTuwunelStack(profile="recovery-cliff")
    client = _RecoveryCliffBoundaryClient()
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", client),),
        recovery_cliff_scenario(),
        reply_timeout=1,
        settle_seconds=0,
    )
    complete = _valid_recovery_cliff_observation().terminal_audit
    audits = iter((replace(complete, noncompleted_sources=(("$source-0", "streaming"),)), complete))
    debt = iter((0, 3))
    debt_samples: list[int] = []

    async def observer_step(**_kwargs: object) -> RecoveryCliffHealthSample:
        return RecoveryCliffHealthSample(True, datetime(2026, 8, 7, tzinfo=UTC))

    monkeypatch.setattr(runner, "_recovery_cliff_audit", lambda **_kwargs: next(audits))
    monkeypatch.setattr(runner, "_recovery_cliff_observer_step", observer_step)
    monkeypatch.setattr(stack, "recovery_outbox_debt", lambda _source_ids: next(debt))
    try:
        terminal = await runner._wait_for_recovery_cliff_terminals(
            baseline_event_ids=frozenset(),
            expected_source_ids=("$source-0", "$source-1"),
            deadline=time.monotonic() + 1,
            health_samples=[],
            debt_samples=debt_samples,
        )

        assert terminal is complete
        assert debt_samples == [0, 3]
    finally:
        stack.close()


def _recovery_original(event_id: str, source_id: str, timestamp: int, status: str) -> dict[str, Any]:
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


def _recovery_edit(
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


def _completed_recovery_cliff_events() -> tuple[dict[str, Any], ...]:
    """Return two overlapping production-shaped completed source streams."""
    return (
        _recovery_original("$response-0", "$source-0", 1_000, "pending"),
        _recovery_edit("$edit-0", "$response-0", 48_000, "completed", outer_status="streaming"),
        _recovery_original("$response-1", "$source-1", 2_000, "streaming"),
        _recovery_edit("$edit-1", "$response-1", 49_000, "completed"),
    )


@pytest.mark.asyncio
async def test_recovery_cliff_warm_completion_precedes_event_and_log_baselines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Warm output is excluded from workload audit and its markers are already baselined."""
    stack = ManagedTuwunelStack(profile="recovery-cliff")
    stack.agent_id = "@mindroom_general:example"
    client = _RecoveryCliffBoundaryClient()
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", client),),
        recovery_cliff_scenario(),
        reply_timeout=1,
        settle_seconds=0,
    )
    warm_completed = False
    log_queries: list[tuple[str, ...]] = []

    async def send_warm(_event_type: str, _txn_id: str, _content: dict[str, Any]) -> str:
        return "$warm"

    async def wait_for_warm(**_kwargs: object) -> object:
        nonlocal warm_completed
        client.seen_events["$warm-response"] = _recovery_original(
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
        if "Waiting to retry Matrix delivery after sync recovery" in markers:
            return 3
        if "Resent unacknowledged deliveries" in markers:
            return 4
        return 0

    monkeypatch.setattr(client, "send_event", send_warm)
    monkeypatch.setattr(runner, "_wait_for_recovery_cliff_terminals", wait_for_warm)
    monkeypatch.setattr(stack, "log_count", log_count)
    try:
        baseline = await runner._prepare_recovery_cliff_baseline(run_id="unit-run")
        client.seen_events.update(
            {
                event["event_id"]: event
                for event in (
                    _recovery_original("$response", "$workload", 2_000, "streaming"),
                    _recovery_edit("$terminal", "$response", 49_000, "completed"),
                )
            },
        )
        audit = runner._recovery_cliff_audit(
            baseline_event_ids=baseline.event_ids,
            expected_source_ids=("$workload",),
        )

        assert "$warm-response" in baseline.event_ids
        assert baseline.log_counts.delivery_retry_markers == 3
        assert baseline.log_counts.delivery_worker_markers == 4
        assert log_queries == [
            (
                "Waiting to retry Matrix delivery after sync recovery",
                f"room_id={client.room_id}",
            ),
            ("Resent unacknowledged deliveries", "agent=general"),
            ("Abandoning", client.room_id),
        ]
        assert audit.unexpected_sources == ()
        assert audit.canonical_responses == (("$workload", "$response"),)
    finally:
        stack.close()


def _valid_recovery_cliff_observation() -> RecoveryCliffObservation:
    """Build a settled observation whose expected values are hand-checked."""
    before = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    after = datetime(2026, 8, 7, 12, 1, tzinfo=UTC)
    audit = audit_recovery_cliff_events(
        _completed_recovery_cliff_events(),
        responder_id="@mindroom_general:example",
        expected_source_ids=frozenset({"$source-0", "$source-1"}),
    )
    return RecoveryCliffObservation(
        root_count=2,
        terminal_audit=audit,
        delivery_retry_markers=1,
        peak_unacknowledged_final_outbox_rows=1,
        delivery_worker_markers=1,
        recovery_abandonment_markers=0,
        drain=RecoveryCliffDrainCounts(
            pending_journal_rows=0,
            unacknowledged_outbox_rows=0,
        ),
        health_samples=(
            RecoveryCliffHealthSample(healthy=True, last_sync_time=before),
            RecoveryCliffHealthSample(healthy=True, last_sync_time=after),
        ),
        watchdog_stalls=0,
        reaction_settled=True,
        pre_fence_last_sync=before,
        post_fence_last_sync=after,
        clean_shutdown=True,
    )


def test_recovery_cliff_event_audit_accepts_exact_completed_streams() -> None:
    """One exact original with an effective completed status satisfies each source."""
    observation = _valid_recovery_cliff_observation()

    assert observation.terminal_audit.canonical_responses == (
        ("$source-0", "$response-0"),
        ("$source-1", "$response-1"),
    )
    assert observation.terminal_audit.canonical_response_count == 2
    assert observation.terminal_audit.max_active_stream_seconds == pytest.approx(47.0)
    assert observation.terminal_audit.full_overlap_seconds == pytest.approx(46.0)
    assert observation.terminal_audit.peak_active_streams == 2
    assert evaluate_recovery_cliff(observation) == ()


def test_recovery_cliff_event_audit_rejects_every_invalid_terminal_replacement() -> None:
    """Orphan, malformed, and repeated terminal edits cannot be omitted from evidence."""
    valid = _completed_recovery_cliff_events()
    malformed = {
        **valid[1],
        "content": {
            **valid[1]["content"],
            "io.mindroom.stream_status": "completed",
            "m.new_content": "malformed",
        },
    }
    missing_new_content = {
        **valid[1],
        "content": {key: value for key, value in valid[1]["content"].items() if key != "m.new_content"},
    }
    status_only_new_content = {
        **valid[1],
        "content": {
            **valid[1]["content"],
            "m.new_content": {"io.mindroom.stream_status": "completed"},
        },
    }
    malformed_message_envelope = {
        **valid[1],
        "content": {
            **valid[1]["content"],
            "m.new_content": {
                "body": 7,
                "msgtype": ["m.text"],
                "io.mindroom.stream_status": "completed",
            },
        },
    }
    completed_notice = {
        **valid[1],
        "content": {
            **valid[1]["content"],
            "m.new_content": {
                **valid[1]["content"]["m.new_content"],
                "msgtype": "m.notice",
            },
        },
    }
    mutations = (
        (
            (*valid, _recovery_edit("$orphan", "$missing-response", 50_000, "streaming", outer_status="pending")),
            "invalid_replacements",
        ),
        ((valid[0], missing_new_content, valid[2], valid[3]), "invalid_replacements"),
        ((valid[0], malformed, valid[2], valid[3]), "invalid_replacements"),
        ((valid[0], status_only_new_content, valid[2], valid[3]), "invalid_replacements"),
        ((valid[0], malformed_message_envelope, valid[2], valid[3]), "invalid_replacements"),
        ((valid[0], completed_notice, valid[2], valid[3]), "invalid_replacements"),
        (
            (
                *valid,
                _recovery_edit(
                    "$second-terminal",
                    "$response-0",
                    100_000,
                    "completed",
                    outer_status="streaming",
                ),
            ),
            "terminal_transitions",
        ),
    )

    for events, marker in mutations:
        audit = audit_recovery_cliff_events(
            events,
            responder_id="@mindroom_general:example",
            expected_source_ids=("$source-0", "$source-1"),
        )
        failures = evaluate_recovery_cliff(replace(_valid_recovery_cliff_observation(), terminal_audit=audit))
        assert any(marker in failure for failure in failures), marker

    progressive = audit_recovery_cliff_events(
        (
            valid[0],
            _recovery_edit("$progress-0", "$response-0", 20_000, "streaming", outer_status="pending"),
            valid[1],
            valid[2],
            _recovery_edit("$progress-1", "$response-1", 21_000, "streaming", outer_status="pending"),
            valid[3],
        ),
        responder_id="@mindroom_general:example",
        expected_source_ids=("$source-0", "$source-1"),
    )
    assert (
        evaluate_recovery_cliff(
            replace(_valid_recovery_cliff_observation(), terminal_audit=progressive),
        )
        == ()
    )


def _valid_sustained_stream_capacity_observation() -> SustainedStreamCapacityObservation:
    """Build settled no-fault capacity evidence from hand-checked root sources."""
    recovery_observation = _valid_recovery_cliff_observation()
    source_ids = ("$source-0", "$source-1")
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
        terminal_audit=recovery_observation.terminal_audit,
        health_samples=recovery_observation.health_samples,
        health_samples_while_root_release=1,
        durable_drain=recovery_observation.drain,
        recovery_abandonment_markers=0,
        watchdog_stalls=0,
        durable_drain_failure_markers=0,
        reaction_settled=True,
        pre_fence_last_sync=recovery_observation.pre_fence_last_sync,
        post_fence_last_sync=recovery_observation.post_fence_last_sync,
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
    client = _RecoveryCliffBoundaryClient()
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

    async def baseline(*, run_id: str) -> fuzz_live_matrix.RecoveryCliffBaseline:
        assert run_id
        order.append("warm-and-baseline")
        return fuzz_live_matrix.RecoveryCliffBaseline(
            event_ids=frozenset(),
            log_counts=fuzz_live_matrix.RecoveryCliffLogCounts(0, 0, 0),
        )

    async def release(
        *,
        run_id: str,
        deadline: float,
        health_samples: list[RecoveryCliffHealthSample],
    ) -> tuple[str, ...]:
        nonlocal deadline_seen
        order.append("root-release")
        deadline_seen = deadline
        assert deadline > time.monotonic()
        health_samples.append(RecoveryCliffHealthSample(True, before))
        roots = (
            _capacity_root("$source-0", 0, run_id=run_id),
            _capacity_root("$source-1", 1, run_id=run_id),
        )
        client.seen_events.update({event["event_id"]: event for event in roots})
        return "$source-0", "$source-1"

    async def terminals(**_kwargs: object) -> fuzz_live_matrix.RecoveryCliffTerminalAudit:
        order.append("terminals")
        events = _completed_recovery_cliff_events()
        client.seen_events.update({event["event_id"]: event for event in events})
        return audit_recovery_cliff_events(
            events,
            responder_id=stack.agent_id,
            expected_source_ids=("$source-0", "$source-1"),
        )

    async def observe_raw(**kwargs: object) -> RecoveryCliffHealthSample:
        order.append("raw-observe")
        sample = RecoveryCliffHealthSample(True, before)
        cast("list[RecoveryCliffHealthSample]", kwargs["health_samples"]).append(sample)
        return sample

    async def forbidden_fault_release(**_kwargs: object) -> tuple[str, ...]:
        raise AssertionError

    async def drain(**_kwargs: object) -> RecoveryCliffDrainCounts:
        order.append("drain")
        return RecoveryCliffDrainCounts(0, 0)

    async def fence(**_kwargs: object) -> tuple[bool, datetime, datetime]:
        order.append("fence")
        return True, before, after

    def stop_mindroom(*, timeout: float = 20) -> bool:
        order.append("shutdown")
        shutdown_timeouts.append(timeout)
        return True

    monkeypatch.setattr(runner, "_authenticate_managed_sender", authenticate)
    monkeypatch.setattr(runner, "_prepare_recovery_cliff_baseline", baseline)
    monkeypatch.setattr(runner, "_release_sustained_stream_capacity_roots", release)
    monkeypatch.setattr(runner, "_release_recovery_cliff_load", forbidden_fault_release)
    monkeypatch.setattr(runner, "_recovery_cliff_observer_step", observe_raw)
    monkeypatch.setattr(runner, "_wait_for_recovery_cliff_terminals", terminals)
    monkeypatch.setattr(runner, "_wait_for_recovery_cliff_drain", drain)
    monkeypatch.setattr(runner, "_wait_for_recovery_cliff_fence", fence)
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
    client = _RecoveryCliffBoundaryClient()
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
    terminal = _valid_recovery_cliff_observation().terminal_audit

    async def baseline(*, run_id: str) -> fuzz_live_matrix.RecoveryCliffBaseline:
        del run_id
        return fuzz_live_matrix.RecoveryCliffBaseline(
            event_ids=frozenset(),
            log_counts=fuzz_live_matrix.RecoveryCliffLogCounts(0, 0, 0),
        )

    async def release(**kwargs: object) -> tuple[str, ...]:
        health_samples = cast("list[RecoveryCliffHealthSample]", kwargs["health_samples"])
        run_id = cast("str", kwargs["run_id"])
        health_samples.append(RecoveryCliffHealthSample(True, before))
        roots = (
            _capacity_root("$source-0", 0, run_id=run_id),
            _capacity_root("$source-1", 1, run_id=run_id),
        )
        client.seen_events.update({event["event_id"]: event for event in roots})
        client.seen_events.update({event["event_id"]: event for event in _completed_recovery_cliff_events()})
        return "$source-0", "$source-1"

    async def drain(**_kwargs: object) -> RecoveryCliffDrainCounts:
        return RecoveryCliffDrainCounts(0, 0)

    async def observe_raw(**kwargs: object) -> RecoveryCliffHealthSample:
        sample = RecoveryCliffHealthSample(True, before)
        cast("list[RecoveryCliffHealthSample]", kwargs["health_samples"]).append(sample)
        return sample

    async def fence(**_kwargs: object) -> tuple[bool, datetime, datetime]:
        return True, before, after

    def stop_mindroom(*, timeout: float = 20) -> bool:
        nonlocal marker_count
        assert timeout > 0
        marker_count += 1
        return True

    monkeypatch.setattr(runner, "_authenticate_managed_sender", lambda: asyncio.sleep(0))
    monkeypatch.setattr(runner, "_prepare_recovery_cliff_baseline", baseline)
    monkeypatch.setattr(runner, "_release_sustained_stream_capacity_roots", release)
    monkeypatch.setattr(runner, "_recovery_cliff_observer_step", observe_raw)
    monkeypatch.setattr(runner, "_wait_for_recovery_cliff_terminals", lambda **_kwargs: asyncio.sleep(0, terminal))
    monkeypatch.setattr(runner, "_wait_for_recovery_cliff_drain", drain)
    monkeypatch.setattr(runner, "_wait_for_recovery_cliff_fence", fence)
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
                health_samples=(RecoveryCliffHealthSample(healthy=False, last_sync_time=before),),
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


def test_recovery_cliff_pass_payload_surfaces_fault_and_worker_debt_evidence() -> None:
    """Machine-readable PASS must retain the causal fault and detached-worker proof."""
    stack = ManagedTuwunelStack(profile="recovery-cliff")
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", _RecoveryCliffBoundaryClient()),),
        recovery_cliff_scenario(),
        reply_timeout=1,
        settle_seconds=0,
    )
    observation = _valid_recovery_cliff_observation()
    try:
        result = runner._recovery_cliff_pass_result(
            observation,
            shape=RecoveryCliffFaultShape(100, 10, 50, 2_000, 601, 100),
        )

        assert result["context_events"] == 601
        assert result["held_events"] == 701
        assert result["peak_unacknowledged_final_outbox_rows"] == 1
        assert result["delivery_worker_markers"] == 1
        assert result["recovery_abandonment_markers"] == 0
        assert result["full_overlap_seconds"] == pytest.approx(46.0)
        assert "recovery_incomplete_markers" not in result
    finally:
        stack.close()


def test_reply_timeout_help_distinguishes_adaptive_and_fixed_profiles(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Operators must not mistake recovery-cliff's whole-workload SLA for an adaptive floor."""
    monkeypatch.setenv("COLUMNS", "240")
    monkeypatch.setattr(sys, "argv", ["fuzz_live_matrix.py", "--help"])

    with pytest.raises(SystemExit) as raised:
        fuzz_live_matrix._parse_args()

    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    assert "adaptive per-turn floor for fuzz, restart-regression, and short-stream-correctness" in help_text
    assert "one fixed whole-workload non-extending SLA for recovery-cliff" in help_text


def test_sustained_stream_capacity_readme_documents_parser_and_no_fault_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capacity invocation and its absence of recovery-cliff fault gates stay documented."""
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
    assert "It does not send SIGSTOP or inject a recovery-cliff context gap" in readme
    assert "does not fault-inject a context gap or require recovery-cliff-only delivery-retry" in readme
    assert "does not require a recovery marker" in readme


def test_recovery_cliff_event_audit_folds_only_same_responder_edits_in_total_order() -> None:
    """A same-timestamp later edit wins, while another sender cannot finish it."""
    events = (
        _completed_recovery_cliff_events()[0],
        _recovery_edit("$edit-a", "$response-0", 2_000, "completed"),
        _recovery_edit("$edit-z", "$response-0", 2_000, "streaming"),
        _recovery_edit(
            "$foreign-edit",
            "$response-0",
            9_000,
            "completed",
            sender="@other:example",
        ),
    )

    audit = audit_recovery_cliff_events(
        events,
        responder_id="@mindroom_general:example",
        expected_source_ids=frozenset({"$source-0"}),
    )

    assert audit.noncompleted_sources == (("$source-0", "streaming"),)


def test_recovery_cliff_event_audit_requires_matching_thread_and_reply_relations() -> None:
    """An expected reply target cannot compensate for the wrong thread root."""
    original = _completed_recovery_cliff_events()[0]
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

    audit = audit_recovery_cliff_events(
        (wrong_thread,),
        responder_id="@mindroom_general:example",
        expected_source_ids=frozenset({"$source-0"}),
    )
    failures = evaluate_recovery_cliff(
        replace(
            _valid_recovery_cliff_observation(),
            root_count=1,
            terminal_audit=audit,
        ),
    )

    assert audit.canonical_response_count == 0
    assert audit.missing_sources == ("$source-0",)
    assert audit.invalid_relations == (("$response-0", "$different-thread", "$source-0"),)
    assert any("invalid_relation" in failure for failure in failures)


def test_recovery_cliff_evaluator_rejects_bad_terminal_directions() -> None:
    """Missing, duplicate, nonterminal, and unknown originals each fail closed."""
    valid = _completed_recovery_cliff_events()
    expected = frozenset({"$source-0", "$source-1"})

    missing = audit_recovery_cliff_events(
        valid[:2],
        responder_id="@mindroom_general:example",
        expected_source_ids=expected,
    )
    duplicate = audit_recovery_cliff_events(
        (
            *valid,
            _recovery_original(
                "$response-1-duplicate",
                "$source-1",
                2_000,
                "streaming",
            ),
        ),
        responder_id="@mindroom_general:example",
        expected_source_ids=expected,
    )
    nonterminal_event = _recovery_edit(
        "$edit-0",
        "$response-0",
        48_000,
        "streaming",
        outer_status="completed",
    )
    nonterminal = audit_recovery_cliff_events(
        (valid[0], nonterminal_event, valid[2], valid[3]),
        responder_id="@mindroom_general:example",
        expected_source_ids=expected,
    )
    unknown = audit_recovery_cliff_events(
        (
            *valid,
            _recovery_original(
                "$unknown-response",
                "$boundary-source",
                4_000,
                "completed",
            ),
        ),
        responder_id="@mindroom_general:example",
        expected_source_ids=expected,
    )

    for audit, marker in (
        (missing, "missing"),
        (duplicate, "duplicate"),
        (nonterminal, "noncompleted"),
        (unknown, "unknown"),
    ):
        observation = replace(_valid_recovery_cliff_observation(), terminal_audit=audit)
        assert any(marker in failure for failure in evaluate_recovery_cliff(observation)), marker


def test_recovery_cliff_evaluator_rejects_unexercised_or_unsettled_completion() -> None:
    """Recovery, drain, health, fence, watchdog, and shutdown are all PASS gates."""
    valid = _valid_recovery_cliff_observation()
    before = valid.pre_fence_last_sync
    assert before is not None
    bad_observations = (
        (replace(valid, delivery_retry_markers=0), "delivery_retry"),
        (replace(valid, peak_unacknowledged_final_outbox_rows=0), "peak_unacknowledged_final_outbox_rows"),
        (replace(valid, delivery_worker_markers=0), "delivery_worker"),
        (replace(valid, recovery_abandonment_markers=1), "recovery_abandonment"),
        (
            replace(valid, drain=replace(valid.drain, pending_journal_rows=1)),
            "pending_journal_rows",
        ),
        (
            replace(valid, drain=replace(valid.drain, unacknowledged_outbox_rows=1)),
            "unacknowledged_outbox_rows",
        ),
        (
            replace(
                valid,
                health_samples=(RecoveryCliffHealthSample(healthy=False, last_sync_time=before),),
            ),
            "health",
        ),
        (replace(valid, watchdog_stalls=1), "watchdog"),
        (replace(valid, reaction_settled=False), "reaction"),
        (replace(valid, post_fence_last_sync=before), "sync_progress"),
        (replace(valid, clean_shutdown=False), "shutdown"),
    )

    for observation, marker in bad_observations:
        assert any(marker in failure for failure in evaluate_recovery_cliff(observation)), marker


def test_recovery_cliff_evaluator_requires_sustained_overlapping_streams() -> None:
    """Fast or serialized replies cannot masquerade as the configured cliff."""
    valid_events = _completed_recovery_cliff_events()
    expected = frozenset({"$source-0", "$source-1"})
    short_events = (
        valid_events[0],
        {**valid_events[1], "origin_server_ts": 2_000},
        valid_events[2],
        {**valid_events[3], "origin_server_ts": 3_000},
    )
    serialized_events = (
        valid_events[0],
        valid_events[1],
        {**valid_events[2], "origin_server_ts": 50_000},
        {**valid_events[3], "origin_server_ts": 97_000},
    )
    asymmetric_events = (
        valid_events[0],
        valid_events[1],
        valid_events[2],
        {**valid_events[3], "origin_server_ts": 3_000},
    )
    insufficient_common_overlap_events = (
        {**valid_events[0], "origin_server_ts": 0},
        {**valid_events[1], "origin_server_ts": 100_000},
        {**valid_events[2], "origin_server_ts": 56_000},
        {**valid_events[3], "origin_server_ts": 101_000},
    )
    short_audit = audit_recovery_cliff_events(
        short_events,
        responder_id="@mindroom_general:example",
        expected_source_ids=expected,
    )
    serialized_audit = audit_recovery_cliff_events(
        serialized_events,
        responder_id="@mindroom_general:example",
        expected_source_ids=expected,
    )
    asymmetric_audit = audit_recovery_cliff_events(
        asymmetric_events,
        responder_id="@mindroom_general:example",
        expected_source_ids=expected,
    )
    insufficient_common_overlap_audit = audit_recovery_cliff_events(
        insufficient_common_overlap_events,
        responder_id="@mindroom_general:example",
        expected_source_ids=expected,
    )

    assert RECOVERY_CLIFF_MIN_ACTIVE_STREAM_SECONDS == 45.0
    assert any(
        "active_stream_duration" in failure
        for failure in evaluate_recovery_cliff(
            replace(_valid_recovery_cliff_observation(), terminal_audit=short_audit),
        )
    )
    assert serialized_audit.max_active_stream_seconds == pytest.approx(47.0)
    assert serialized_audit.peak_active_streams == 1
    assert any(
        "peak_active_streams" in failure
        for failure in evaluate_recovery_cliff(
            replace(_valid_recovery_cliff_observation(), terminal_audit=serialized_audit),
        )
    )
    assert asymmetric_audit.max_active_stream_seconds == pytest.approx(47.0)
    assert asymmetric_audit.peak_active_streams == 2
    assert any(
        "active_stream_duration" in failure
        for failure in evaluate_recovery_cliff(
            replace(_valid_recovery_cliff_observation(), terminal_audit=asymmetric_audit),
        )
    )
    assert insufficient_common_overlap_audit.full_overlap_seconds == pytest.approx(44.0)
    assert any(
        "full_overlap" in failure
        for failure in evaluate_recovery_cliff(
            replace(
                _valid_recovery_cliff_observation(),
                terminal_audit=insufficient_common_overlap_audit,
            ),
        )
    )


@pytest.mark.asyncio
async def test_machine_readable_pass_result_labels_its_profile() -> None:
    """A passing short-stream result must state its profile instead of implying capacity."""
    stack = ManagedTuwunelStack()
    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", _RecoveryCliffBoundaryClient()),),
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


def test_restart_regression_waits_for_checkpoint_later_than_fresh_event() -> None:
    """The hard-restart boundary must be beyond the fresh event's projected response."""
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
        continuity_store = SyncContinuityStore(stack.storage_path, "general")
        continuity_store.replace_checkpoint(
            SyncCheckpoint("s_before", store_generation="generation"),
        )

        def advance_checkpoint() -> None:
            time.sleep(0.1)
            continuity_store.replace_checkpoint(
                SyncCheckpoint("s_after", store_generation="generation"),
            )

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

        stack.apply_replacement_config("!restart:example")
        replacement = yaml.safe_load(stack.config_path.read_text(encoding="utf-8"))

        assert replacement["models"]["default"]["id"] == "mindroom-live-fuzz-replacement"
        assert replacement["models"]["router"]["id"] == "mindroom-live-fuzz"
    finally:
        stack.close()


def test_recovery_cliff_managed_config_uses_synthetic_responder_and_sliding_sync() -> None:
    """Recovery-cliff config must use the fixed production-shaped sender and responder setup."""
    stack = ManagedTuwunelStack(profile="recovery-cliff")
    try:
        stack._write_config(9292)
        config = yaml.safe_load(stack.config_path.read_text(encoding="utf-8"))

        assert config["matrix_sync"] == {
            "mode": "sliding",
            "sliding_timeline_limit": 100,
        }
        assert config["models"]["synthetic"]["provider"] == "synthetic"
        assert config["models"]["synthetic"]["extra_kwargs"] == {
            "seed": 1,
            "min_response_chars": 4000,
            "max_response_chars": 4800,
            "chunk_chars": 40,
            "chars_per_second": 80,
            "tool_call_probability": 0.2,
        }
        assert config["agents"]["general"]["model"] == "synthetic"
        assert config["agents"]["general"]["tools"] == ["shell"]
        assert config["agents"]["general"]["worker_tools"] == []
        assert config["agents"]["load_sender"]["rooms"] == ["lobby"]
        parsed = Config.model_validate(config)
        assert parsed.models["synthetic"].id == "lorem-ipsum"
    finally:
        stack.close()


def test_sustained_stream_capacity_config_uses_managed_sender_and_synthetic_responder() -> None:
    """The no-fault profile must leave 45 seconds of overlap after a spread launch."""
    stack = ManagedTuwunelStack(profile="sustained-stream-capacity")
    try:
        stack._write_config(9292)
        config = yaml.safe_load(stack.config_path.read_text(encoding="utf-8"))

        assert config["matrix_sync"] == {
            "mode": "sliding",
            "sliding_timeline_limit": 100,
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


def test_recovery_cliff_drain_counts_only_live_journal_and_outbox_rows() -> None:
    """Terminal journal and acknowledged outbox rows do not keep the drain open."""
    stack = ManagedTuwunelStack(profile="recovery-cliff")
    try:
        database_path = stack.storage_path / "tracking" / "event_journal.db"
        database_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database_path)) as database:
            database.execute("CREATE TABLE journal_events(state TEXT NOT NULL)")
            database.execute("CREATE TABLE response_outbox(acknowledged_event_id TEXT)")
            database.executemany(
                "INSERT INTO journal_events(state) VALUES (?)",
                (("pending",), ("settled",)),
            )
            database.executemany(
                "INSERT INTO response_outbox(acknowledged_event_id) VALUES (?)",
                ((None,), ("$response",)),
            )
            database.commit()

        assert stack.recovery_drain_counts() == RecoveryCliffDrainCounts(
            pending_journal_rows=1,
            unacknowledged_outbox_rows=1,
        )
    finally:
        stack.close()


def test_recovery_cliff_drain_fails_when_the_journal_database_is_missing() -> None:
    """Absent durable state must not be reported as a zero-row drain."""
    stack = ManagedTuwunelStack(profile="recovery-cliff")
    try:
        with pytest.raises(FileNotFoundError, match="event journal database"):
            stack.recovery_drain_counts()
    finally:
        stack.close()


def test_recovery_cliff_debt_counts_only_exact_workload_final_rows() -> None:
    """Only attempted unacknowledged FINAL debt for general workload roots is evidence."""
    stack = ManagedTuwunelStack(profile="recovery-cliff")
    stack.agent_id = "@mindroom_general:example"
    expected_principal = f"general@{stack.agent_id}"
    try:
        database_path = stack.storage_path / "tracking" / "event_journal.db"
        database_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database_path)) as database:
            database.execute(
                "CREATE TABLE response_outbox("
                "principal_id TEXT, turn_id TEXT, stage TEXT, attempted INTEGER, acknowledged_event_id TEXT)",
            )
            database.executemany(
                "INSERT INTO response_outbox VALUES (?, ?, ?, ?, ?)",
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


def test_recovery_cliff_reaction_state_filters_exact_principal_event_and_kind() -> None:
    """A distractor principal cannot prove that the responder settled the fence."""
    stack = ManagedTuwunelStack(profile="recovery-cliff")
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
            assert stack.recovery_reaction_state("$reaction") is None
            database.execute(
                "INSERT INTO journal_events VALUES (?, '$reaction', 'reaction', 'settled')",
                (f"general@{stack.agent_id}",),
            )
            database.commit()
        assert stack.recovery_reaction_state("$reaction") == "settled"
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

        stack._start_mindroom(timeout=7)

        assert commands == [
            [
                "uv",
                "run",
                "--python",
                "3.13",
                "mindroom",
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
        monkeypatch.setattr(os, "killpg", lambda pid, signum: signals.append((pid, signum)))

        assert not stack.stop_mindroom(timeout=1)
        assert signals == [(10, signal.SIGINT)]
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
        monkeypatch.setattr(os, "killpg", lambda pid, signum: signals.append((pid, signum)))

        assert stack.stop_mindroom(timeout=1)
        assert signals == [(10, signal.SIGINT)]
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
        monkeypatch.setattr(os, "killpg", lambda _pid, _signum: None)

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

    async def send_event(self, event_type: str, txn_id: str, content: dict[str, Any]) -> str:
        self.order.append("send")
        return await super().send_event(event_type, txn_id, content)


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


def test_recovery_cliff_pause_confirms_stopped_state_without_consuming_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGSTOP is complete only after every managed process-group member stops."""

    class Process:
        pid = 10

        @staticmethod
        def poll() -> None:
            return None

    class Stopped:
        si_code = os.CLD_STOPPED
        si_status = signal.SIGSTOP

    stack = ManagedTuwunelStack(profile="recovery-cliff")
    signals: list[tuple[int, int]] = []
    wait_calls: list[tuple[int, int, int]] = []
    state_calls: list[int] = []
    group_states = iter(({10: "T", 11: "R"}, {10: "T", 11: "T"}))
    try:
        stack._mindroom_process = cast("Any", Process())
        monkeypatch.setattr(os, "killpg", lambda pid, signum: signals.append((pid, signum)))

        def waitid(idtype: int, pid: int, options: int) -> Stopped:
            wait_calls.append((idtype, pid, options))
            return Stopped()

        monkeypatch.setattr(os, "waitid", waitid)

        def process_group_states(process_group_id: int) -> dict[int, str]:
            state_calls.append(process_group_id)
            return next(group_states)

        monkeypatch.setattr(
            fuzz_live_matrix,
            "_process_group_states",
            process_group_states,
            raising=False,
        )

        stack.pause_mindroom(timeout=0.1)
        stack.resume_mindroom()

        assert signals == [(10, signal.SIGSTOP), (10, signal.SIGCONT)]
        expected_wait = (
            os.P_PID,
            10,
            os.WSTOPPED | os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
        assert wait_calls == [expected_wait, expected_wait]
        assert state_calls == [10, 10]
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
        oracle._ingest_event(
            {
                "event_id": "$resume-relay",
                "sender": "@router:example",
                "type": "m.room.message",
                "content": {"body": "resume"},
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
        events: list[dict[str, Any]] = [
            {"event_id": relay, "sender": "@router:example", "type": "m.room.message", "content": {"body": "again"}},
            {
                "event_id": f"{relay}-answer",
                "sender": "@agent:example",
                "type": "m.room.message",
                "content": {
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$root",
                        "m.in_reply_to": {"event_id": relay},
                    },
                },
            },
        ]
        if self._round == 1:
            events.append(
                {
                    "event_id": "$a-reply",
                    "sender": "@agent:example",
                    "type": "m.room.message",
                    "content": {
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": "$root",
                            "m.in_reply_to": {"event_id": "$a"},
                        },
                    },
                },
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
            await principal.enqueue_delivery(
                turn_id="$staged",
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
