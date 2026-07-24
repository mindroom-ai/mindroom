"""Tests for replayable real-server Matrix fuzz traces and their oracle."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mindroom.handled_turns import TurnRecord, TurnRecordCodec

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from scripts.testing.fuzz_live_matrix import (
    ChaosTuning,
    ExactReplyOracle,
    FinalStateAuditor,
    LiveFuzzScenario,
    LiveMatrixClient,
    LiveOperation,
    LiveOperationKind,
    _body_call_id,
    _SentRecord,
    chaos_scenario_from_seed,
    live_scenario_from_seed,
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
    """The fuzz and chaos generators together must reach every operation."""
    fuzz_seen = {
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
    chaos_seen = {
        operation.kind
        for seed in range(8)
        for batch in chaos_scenario_from_seed(seed, steps=400).batches
        for operation in batch
    }

    assert LiveOperationKind.RESTART_MINDROOM in fuzz_seen
    assert fuzz_seen >= set(LiveOperationKind) - {
        LiveOperationKind.KILL_RESTART_MINDROOM,
        LiveOperationKind.COLD_RESTART_MINDROOM,
        LiveOperationKind.RESTART_TUWUNEL,
        LiveOperationKind.STOP_MINDROOM,
        LiveOperationKind.START_MINDROOM,
        LiveOperationKind.CHECKPOINT,
    }
    assert chaos_seen == set(LiveOperationKind)


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
    payload = legacy.to_json()
    stripped = payload.replace('"client": 0,\n          ', "").replace('"client_count": 1,\n  ', "")
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
    records = [
        _SentRecord("$msg", "!room:example", "m.room.message"),
        _SentRecord("$gone", "!room:example", "m.room.message"),
        _SentRecord("$react", "!room:example", "m.reaction", reaction_key="fuzz-9"),
    ]
    try:
        events = {
            "$msg": {"event_id": "$msg", "type": "m.room.message", "content": {"body": "hello"}},
            "$gone": {"event_id": "$gone", "type": "m.room.message", "content": {}},
            "$react": {
                "event_id": "$react",
                "type": "m.reaction",
                "content": {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$msg", "key": "fuzz-9"}},
            },
        }
        auditor._assert_sent_events_canonical(events, records, {"$gone"})

        with pytest.raises(AssertionError, match="kept visible content"):
            auditor._assert_sent_events_canonical(events, records, {"$gone", "$msg"})

        events["$react"]["content"]["m.relates_to"]["key"] = "wrong"
        with pytest.raises(AssertionError, match="lost its key"):
            auditor._assert_sent_events_canonical(events, records, {"$gone"})

        with pytest.raises(AssertionError, match="missing from /messages"):
            auditor._assert_sent_events_canonical({}, records, set())
    finally:
        await client.close()


def test_body_call_id_parses_only_canonical_prefixes() -> None:
    """Call IDs come only from exact stub-format bodies."""
    assert _body_call_id("LIVE-FUZZ call=17 segment-000 END call=17") == 17
    assert _body_call_id("[Response interrupted by service restart]") is None
    assert _body_call_id("LIVE-FUZZ call=x END") is None


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


@pytest.mark.asyncio
async def test_coalescing_oracle_settles_via_ledger_attribution(tmp_path: Path) -> None:
    """Sources swallowed into a combined follow-up turn settle via the durable ledger."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    ledger_path = tmp_path / "general_responded.json"
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

        oracle._ingest_event(_agent_reply_event("$second", "$combined-reply", "LIVE-FUZZ call=2 END call=2"))
        assert oracle.unsettled_required_sources() == []
        assert oracle.resolve_response_ref("response:op:2") == "$combined-reply"
        assert oracle.resolve_response_ref("response:op:1") == "$combined-reply"

        record = TurnRecord(
            source_event_ids=("$first",),
            response_event_id="$dedicated-reply",
            completed=True,
        )
        ledger_path.write_text(
            json.dumps(
                {
                    "schema_version": TurnRecordCodec.schema_version(),
                    "records": {"$first": TurnRecordCodec.to_ledger_record(record)},
                },
            ),
            encoding="utf-8",
        )
        oracle.refresh_ledger_attributions(min_interval=0.0)
        assert oracle.resolve_response_ref("response:op:1") == "$dedicated-reply"
        oracle._assert_no_wrong_replies()
    finally:
        await client.close()


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
    """Durable attribution must cover every required source and every visible reply."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example", coalescing_threads=True)
    ledger_path = tmp_path / "general_responded.json"
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

        record = TurnRecord(
            source_event_ids=("$first", "$second"),
            response_event_id="$combined-reply",
            completed=True,
        )
        full_ledger = json.dumps(
            {
                "schema_version": TurnRecordCodec.schema_version(),
                "records": {
                    "$first": TurnRecordCodec.to_ledger_record(record),
                    "$second": TurnRecordCodec.to_ledger_record(record),
                },
            },
        )
        ledger_path.write_text(full_ledger, encoding="utf-8")
        assert auditor._assert_ledger_attribution(replies) == {
            "ledger_attributed_sources": 2,
            "ledger_superseded_sources": 0,
        }

        ledger_path.write_text(
            json.dumps(
                {
                    "schema_version": TurnRecordCodec.schema_version(),
                    "records": {"$first": TurnRecordCodec.to_ledger_record(record)},
                },
            ),
            encoding="utf-8",
        )
        with pytest.raises(AssertionError, match="newest chain source"):
            auditor._assert_ledger_attribution(replies)

        superseded_ledger = json.dumps(
            {
                "schema_version": TurnRecordCodec.schema_version(),
                "records": {"$second": TurnRecordCodec.to_ledger_record(record)},
            },
        )
        ledger_path.write_text(superseded_ledger, encoding="utf-8")
        assert auditor._assert_ledger_attribution(replies) == {
            "ledger_attributed_sources": 1,
            "ledger_superseded_sources": 1,
        }

        orphan = {"$second": {"$combined-reply"}, "$first": {"$rogue-reply"}}
        ledger_path.write_text(full_ledger, encoding="utf-8")
        with pytest.raises(AssertionError, match="not attributed by any durable turn record"):
            auditor._assert_ledger_attribution(orphan)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_final_body_audit_accepts_only_recovered_interruptions() -> None:
    """An interrupted note passes only with a completed same-thread resume answer."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example", coalescing_threads=True)
    auditor = FinalStateAuditor(
        client,
        oracle,
        agent_id="@agent:example",
        expected_body_for=lambda call_id: f"LIVE-FUZZ call={call_id} END call={call_id}",
    )
    try:
        oracle.expect("op:1", "$source", thread=0)
        oracle.internal_source_ids.add("$resume-relay")
        interrupted = _agent_reply_event(
            "$source",
            "$reply",
            "LIVE-FUZZ call=9 **[Response interrupted by service restart]**",
        )
        interrupted["content"]["m.relates_to"]["event_id"] = "$root"
        events: dict[str, Any] = {"$reply": interrupted}
        replies = {"$source": {"$reply"}}
        with pytest.raises(AssertionError, match="non-canonical body"):
            auditor._assert_final_bodies_complete(events, replies)

        resumed = _agent_reply_event("$resume-relay", "$resumed", "LIVE-FUZZ call=11 END call=11")
        resumed["content"]["m.relates_to"]["event_id"] = "$root"
        events["$resumed"] = resumed
        assert auditor._assert_final_bodies_complete(events, replies) == 1
    finally:
        await client.close()
