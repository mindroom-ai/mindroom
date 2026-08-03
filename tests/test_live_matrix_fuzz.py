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

from mindroom.dispatch_obligations import DispatchObligationStore
from mindroom.matrix.cache.sqlite_event_cache import _initialize_event_cache_db
from mindroom.matrix.sync_certification import SyncCheckpoint
from mindroom.matrix.sync_continuity import SyncContinuityStore
from scripts.testing.fuzz_live_matrix import (
    ORDERLY_SHUTDOWN_MARKER,
    PROJECT_ROOT,
    RESTART_SHUTDOWN_FAILURE_MARKER,
    ExactReplyOracle,
    LiveFuzzRunner,
    LiveFuzzScenario,
    LiveMatrixClient,
    LiveOperation,
    LiveOperationKind,
    ManagedTuwunelStack,
    RestartRegressionObservation,
    _log_count,
    _ModelHandler,
    _restart_prompt_observation,
    _semantic_ingress_markers,
    evaluate_restart_regression,
    live_scenario_from_seed,
    restart_regression_scenario,
    saturation_scenario,
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

    def wait_for_cached_restart_event_pairs(
        self,
        room_id: str,
        event_ids: tuple[str, str],
        *,
        minimum: int,
        timeout: float,
    ) -> bool:
        assert room_id == "!restart:example"
        assert event_ids == ("$restart-old-text", "$restart-old-media")
        assert minimum == 4
        assert timeout == 1
        return True

    def cached_restart_event_pair_count(self, room_id: str, event_ids: tuple[str, str]) -> int:
        assert room_id == "!restart:example"
        assert event_ids == ("$restart-old-text", "$restart-old-media")
        return 4

    def wait_for_restart_dispatch_obligation_state(
        self,
        event_id: str,
        *,
        expected: str | frozenset[str],
        timeout: float,
    ) -> bool:
        assert event_id == "$restart-fresh"
        assert expected == frozenset({"pending", "deferred"})
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
            cached_event_pair_count=4,
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
    monkeypatch.setattr(stack, "cached_restart_event_pair_count", lambda _room_id, _event_ids: 4)
    monkeypatch.setattr(stack, "restart_dispatch_obligation_state", lambda _event_id: "succeeded")

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
    assert LiveFuzzScenario.from_json(scenario.to_json()) == scenario
    assert (
        sum(
            operation.kind is not LiveOperationKind.RESTART_MINDROOM
            for batch in scenario.batches
            for operation in batch
        )
        == 250
    )
    assert any(
        operation.kind is LiveOperationKind.RESTART_MINDROOM for batch in scenario.batches for operation in batch
    )
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


def test_saturation_scenario_matches_original_two_phase_workload() -> None:
    """The regression profile must preserve the old hot-then-parallel ordering."""
    scenario = saturation_scenario()

    assert scenario.thread_count == 13
    assert len(scenario.batches) == 108
    assert all(len(batch) == 1 and batch[0].thread == 0 for batch in scenario.batches[:100])
    assert all([operation.thread for operation in batch] == list(range(1, 13)) for batch in scenario.batches[100:])


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
        cached_event_pair_count=4,
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
            cached_event_pair_count=0,
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
    assert any("invariant=historical_event_pairs_cached" in failure for failure in failures)
    assert any("invariant=fresh_agent_response_exactly_once" in failure for failure in failures)
    assert any("invariant=fresh_router_response_suppressed" in failure for failure in failures)
    assert any("invariant=fresh_response_complete" in failure for failure in failures)
    assert any("invariant=fresh_semantic_ingress_replayed_after_restart" in failure for failure in failures)
    assert any("invariant=recovered_generation_response_observed" in failure for failure in failures)
    assert any("invariant=fresh_dispatch_obligation_recovered" in failure for failure in failures)
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

        assert stack.order == [
            "durable-callback",
            "obligation-pending",
            "model-in-flight",
            "sync-checkpoint",
            "hard-restart",
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
async def test_restart_regression_does_not_send_fresh_event_before_historical_cache_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifecycle completion alone must not release the fresh event."""
    stack = ManagedTuwunelStack()
    try:
        stack.agent_id, stack.router_id = "@agent:example", "@router:example"
        monkeypatch.setattr(stack, "apply_replacement_config", lambda _room_id: None)
        monkeypatch.setattr(stack, "wait_for_log_count", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(stack, "cached_restart_event_pair_count", lambda *_args, **_kwargs: 3)
        dormant = _RecordingDormantClient()
        runner = LiveFuzzRunner(
            stack,
            (cast("LiveMatrixClient", dormant),),
            restart_regression_scenario(),
            reply_timeout=0,
            settle_seconds=0,
        )

        with pytest.raises(AssertionError, match="historical_event_pairs_cached"):
            await runner._run_restart_regression()

        assert dormant.sent_txn_ids == ["restart-old-text", "restart-old-media"]
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


def test_restart_regression_cache_evidence_uses_production_schema_and_exact_filters() -> None:
    """Principal, room, and event filters must reject plausible distractor rows."""
    stack = ManagedTuwunelStack()
    try:
        stack.agent_id, stack.router_id = "@agent:example", "@router:example"
        stack.storage_path.mkdir()
        database_path = stack.storage_path / "event_cache.db"
        database, _report, _generation = asyncio.run(_initialize_event_cache_db(database_path))
        asyncio.run(database.close())
        rows = (
            ("@agent:example", "$old-text", "!target:example"),
            ("@agent:example", "$old-media", "!target:example"),
            ("@router:example", "$old-text", "!target:example"),
            ("@router:example", "$old-media", "!target:example"),
            ("@wrong:example", "$old-text", "!target:example"),
            ("@wrong:example", "$old-media", "!target:example"),
            ("@agent:example", "$wrong-event", "!target:example"),
            ("@router:example", "$wrong-event", "!target:example"),
            ("@agent:example", "$old-text", "!wrong:example"),
        )
        with closing(sqlite3.connect(database_path)) as fixture_database:
            fixture_database.executemany(
                """
                INSERT INTO events(
                    principal_id,
                    event_id,
                    room_id,
                    origin_server_ts,
                    event_json,
                    sender,
                    cached_at,
                    write_seq
                ) VALUES (?, ?, ?, 1, '{}', '@sender:example', 1.0, ?)
                """,
                ((*row, write_seq) for write_seq, row in enumerate(rows, start=1)),
            )
            fixture_database.commit()

        event_ids = ("$old-text", "$old-media")
        assert stack.cached_restart_event_pair_count("!target:example", event_ids) == 4
    finally:
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


def test_restart_regression_cache_probe_does_not_create_an_empty_database() -> None:
    """Missing runtime cache state must not be converted into an empty SQLite database."""
    stack = ManagedTuwunelStack()
    try:
        database_path = stack.storage_path / "event_cache.db"

        assert stack.cached_restart_event_pair_count("!target:example", ("$old-text", "$old-media")) == 0
        assert not database_path.exists()
    finally:
        stack.close()


def test_restart_regression_waits_for_checkpoint_later_than_fresh_event() -> None:
    """The hard-restart boundary must be beyond the fresh event's cached response."""
    stack = ManagedTuwunelStack()
    writer: threading.Thread | None = None
    try:
        stack.agent_id = "@agent:example"
        stack.storage_path.mkdir()
        database_path = stack.storage_path / "event_cache.db"
        database, _report, _generation = asyncio.run(_initialize_event_cache_db(database_path))
        asyncio.run(database.close())
        with closing(sqlite3.connect(database_path)) as fixture_database:
            fixture_database.execute(
                """
                INSERT INTO events(
                    principal_id,
                    event_id,
                    room_id,
                    origin_server_ts,
                    event_json,
                    sender,
                    cached_at,
                    write_seq
                ) VALUES (?, ?, ?, 1, '{}', '@sender:example', 1.0, 1)
                """,
                (stack.agent_id, "$fresh", "!target:example"),
            )
            fixture_database.commit()
        continuity_store = SyncContinuityStore(stack.storage_path, "general")
        continuity_store.replace_checkpoint(
            SyncCheckpoint("s_before", cache_generation="generation"),
        )

        def advance_checkpoint() -> None:
            time.sleep(0.1)
            continuity_store.replace_checkpoint(
                SyncCheckpoint("s_after", cache_generation="generation"),
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


def test_restart_regression_reads_exact_durable_obligation_state() -> None:
    """The recovery oracle must follow the exact agent message obligation."""
    stack = ManagedTuwunelStack()
    try:
        stack.agent_id = "@agent:example"
        store = DispatchObligationStore(
            tracking_path=stack.storage_path / "tracking",
            principal_id=stack.agent_id,
            entity_name="general",
        )
        database_path = store._database_path
        with closing(sqlite3.connect(database_path)) as database:
            database.execute(
                """
                INSERT INTO dispatch_obligations(
                    principal_id,
                    entity_name,
                    source_event_id,
                    callback_kind,
                    room_id,
                    event_source_json,
                    state,
                    created_at_ns,
                    settled_at_ns
                ) VALUES (?, 'general', '$fresh', 'message', '!room:example', '{}', 'pending', 1, NULL)
                """,
                (stack.agent_id,),
            )
            database.commit()

        assert stack.restart_dispatch_obligation_state("$fresh") == "pending"
        assert stack.restart_dispatch_obligation_state("$other") is None
        assert stack.wait_for_restart_dispatch_obligation_state(
            "$fresh",
            expected=frozenset({"pending", "deferred"}),
            timeout=0.01,
        )

        with closing(sqlite3.connect(database_path)) as database:
            database.execute(
                "UPDATE dispatch_obligations SET state = 'deferred'",
            )
            database.commit()

        assert stack.wait_for_restart_dispatch_obligation_state(
            "$fresh",
            expected=frozenset({"pending", "deferred"}),
            timeout=0.01,
        )

        with closing(sqlite3.connect(database_path)) as database:
            database.execute(
                "UPDATE dispatch_obligations SET state = 'succeeded', settled_at_ns = 2",
            )
            database.commit()

        assert stack.restart_dispatch_obligation_state("$fresh") == "succeeded"
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


def test_diagnostic_counts_handle_colored_structlog_fields() -> None:
    """ANSI rendering must not turn timeout counters into structural zeroes."""
    stack = ManagedTuwunelStack()
    try:
        stack.log_path.write_text(
            "thread_read_error=\x1b[35mcache_coordinator_timeout\x1b[0m\n"
            "thread_read_error=\x1b[35mdispatch_read_timeout\x1b[0m\n",
            encoding="utf-8",
        )

        assert stack.diagnostic_counts()["cache_coordinator_timeouts"] == 1
        assert stack.diagnostic_counts()["dispatch_read_timeouts"] == 1
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
    assert observation.cached_event_pair_count == 4
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
        monkeypatch.setattr(stack, "cached_restart_event_pair_count", lambda _room_id, _event_ids: 4)
        monkeypatch.setattr(stack, "restart_dispatch_obligation_state", lambda _event_id: "succeeded")
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
