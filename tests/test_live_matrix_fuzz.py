"""Tests for replayable real-server Matrix fuzz traces and their oracle."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import replace
from typing import Any, cast

import pytest

from mindroom.matrix.cache.sqlite_event_cache import _initialize_event_cache_db
from scripts.testing.fuzz_live_matrix import (
    ExactReplyOracle,
    LiveFuzzRunner,
    LiveFuzzScenario,
    LiveMatrixClient,
    LiveOperation,
    LiveOperationKind,
    ManagedTuwunelStack,
    RestartRegressionObservation,
    _restart_prompt_observation,
    evaluate_restart_regression,
    live_scenario_from_seed,
    restart_regression_scenario,
    saturation_scenario,
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


def test_restart_regression_scenario_is_allowed_and_json_replayable() -> None:
    """The manual profile must survive the same trace parser used by replay."""
    scenario = restart_regression_scenario()

    assert LiveFuzzScenario.from_json(scenario.to_json()) == scenario


def test_restart_regression_evaluator_accepts_pass_and_rejects_bad_directions() -> None:
    """The profile's pure oracle must accept clean evidence and reject old output and prompt overlap."""
    passing = RestartRegressionObservation(
        historical_output_counts=(0, 0),
        replacement_boundary_reached=True,
        cached_event_pair_count=4,
        fresh_output_count=1,
        fresh_prompt_observed=True,
        historical_in_fresh_prompt=False,
        response_callbacks_quiescent=True,
    )

    assert evaluate_restart_regression(passing) == ()

    failures = evaluate_restart_regression(
        replace(
            passing,
            historical_output_counts=(1, 0),
            historical_in_fresh_prompt=True,
        ),
    )

    assert any("invariant=historical_output_suppressed" in failure for failure in failures)
    assert any("invariant=historical_events_absent_from_fresh_prompt" in failure for failure in failures)


def test_restart_regression_cache_evidence_uses_production_schema_and_exact_filters() -> None:
    """Principal, room, and event filters must reject plausible distractor rows."""
    stack = ManagedTuwunelStack()
    try:
        stack.agent_id, stack.router_id = "@agent:example", "@router:example"
        assert not stack.wait_for_log_count(("missing",), 1, timeout=0)
        stack.log_path.write_text("agent_setup_complete @agent:example\n", encoding="utf-8")
        assert stack.wait_for_log_count(("agent_setup_complete", "@agent:example"), 1, timeout=0)
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
        assert _restart_prompt_observation(
            "Preparing agent and prompt $fresh $old-text",
            "$fresh",
            event_ids,
        ) == (True, True)
        assert _restart_prompt_observation("Preparing agent and prompt $fresh", "$fresh", event_ids) == (True, False)
        assert _restart_prompt_observation("Preparing agent and prompt $old-text", "$fresh", event_ids) == (
            False,
            False,
        )
        assert (
            LiveFuzzRunner._combined_response_count(
                "$fresh",
                {"$fresh": {"$agent-response"}},
                {"$fresh": {"$router-response"}},
            )
            == 2
        )
    finally:
        stack.close()


def test_restart_regression_cache_probe_does_not_create_an_empty_database() -> None:
    """Missing runtime cache state must not be converted into an empty SQLite database."""
    stack = ManagedTuwunelStack()
    try:
        database_path = stack.storage_path / "event_cache.db"

        assert stack.cached_restart_event_pair_count("!target:example", ("$old-text", "$old-media")) == 0
        assert not database_path.exists()
    finally:
        stack.close()


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
async def test_restart_observation_rejects_historical_output_arriving_during_quiescence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A historical reply arriving after the fresh terminal reply must still fail."""

    class DormantClient:
        room_id = "!restart:example"

        def __init__(self) -> None:
            self.seen_events: dict[str, dict[str, Any]] = {}
            self.sync_count = 0

        async def sync_incremental(self, *, timeout_ms: int, allow_limited: bool = False) -> None:
            del timeout_ms, allow_limited
            self.sync_count += 1
            if self.sync_count == 1:
                self.seen_events["$fresh-response"] = response(
                    "$fresh-response",
                    "@agent:example",
                    "$fresh",
                )
            if self.sync_count == 3:
                self.seen_events["$late-historical-response"] = response(
                    "$late-historical-response",
                    "@agent:example",
                    "$old-text",
                )
            await asyncio.sleep(0.05)

    def response(event_id: str, sender: str, source: str) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "sender": sender,
            "type": "m.room.message",
            "content": {
                "body": "END call=1",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": source,
                    "m.in_reply_to": {"event_id": source},
                },
            },
        }

    stack = ManagedTuwunelStack()
    try:
        stack.agent_id, stack.router_id = "@agent:example", "@router:example"
        stack.log_path.write_text(
            "matrix_event_callback_started !restart:example\nPreparing agent and prompt $fresh\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(stack, "cached_restart_event_pair_count", lambda _room_id, _event_ids: 4)
        dormant = DormantClient()
        runner = LiveFuzzRunner(
            stack,
            (cast("LiveMatrixClient", dormant),),
            restart_regression_scenario(),
            reply_timeout=2,
            settle_seconds=0,
        )

        observation = await runner._wait_for_restart_observation(
            cast("LiveMatrixClient", dormant),
            historical_event_ids=("$old-text", "$old-media"),
            fresh_event_id="$fresh",
            replacement_boundary_reached=True,
        )

        assert observation.response_callbacks_quiescent
        assert observation.historical_output_counts == (1, 0)
        assert any(
            "invariant=historical_output_suppressed" in failure for failure in evaluate_restart_regression(observation)
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
