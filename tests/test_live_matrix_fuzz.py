"""Tests for replayable real-server Matrix fuzz traces and their oracle."""

from __future__ import annotations

import asyncio
import os
import signal
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
import yaml

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
    RestartRegressionObservation,
    SlowWaitNotice,
    TurnLatencyMonitor,
    WaitBudget,
    _log_count,
    _ModelHandler,
    _restart_prompt_observation,
    _semantic_ingress_markers,
    classify_missing_reply,
    collect_host_load_report,
    evaluate_restart_regression,
    live_scenario_from_seed,
    recovery_cliff_scenario,
    restart_regression_scenario,
    short_stream_correctness_scenario,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


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


def test_recovery_cliff_scenario_rejects_an_altered_trace_shape() -> None:
    """The fixed recovery runner must not silently ignore declared trace operations."""
    scenario = LiveFuzzScenario(
        thread_count=100,
        batches=((LiveOperation(0, LiveOperationKind.REACTION, 0, "root:0"),),),
        profile="recovery-cliff",
    )

    with pytest.raises(ValueError, match="fixed empty trace"):
        scenario.validate()


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
        assert config["agents"]["load_sender"]["rooms"] == ["lobby"]
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
