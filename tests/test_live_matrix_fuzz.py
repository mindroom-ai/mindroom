"""Tests for replayable real-server Matrix fuzz traces and their oracle."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mindroom.handled_turns import TurnRecord, TurnRecordCodec
from mindroom.streaming import RESTART_INTERRUPTED_RESPONSE_NOTE

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from scripts.testing.fuzz_live_matrix import (
    ORIGINAL_REVISION,
    ChaosTuning,
    ExactReplyOracle,
    FailureBundle,
    FinalStateAuditor,
    LiveFuzzRunner,
    LiveFuzzScenario,
    LiveMatrixClient,
    LiveOperation,
    LiveOperationKind,
    _body_call_id,
    _ModelHandler,
    _parse_markers,
    _persist_failure_bundle,
    _sanitized_oracle_snapshot,
    _SentRecord,
    _source_marker,
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


def _write_ledger(ledger_path: Path, records: dict[str, TurnRecord]) -> None:
    """Serialize handled-turn records into the versioned ledger file."""
    ledger_path.write_text(
        json.dumps(
            {
                "schema_version": TurnRecordCodec.schema_version(),
                "records": {event_id: TurnRecordCodec.to_ledger_record(record) for event_id, record in records.items()},
            },
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_coalescing_oracle_settles_via_ledger_attribution(tmp_path: Path) -> None:
    """Sources swallowed into a combined follow-up turn settle via the durable ledger.

    A newer source anchors on its own visible combined reply, but an older
    superseded source only settles once its own completed no-response record
    proves supersession. A direct visible reply is never enough on its own.
    """
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

        # The combined reply anchors the newest source, but the older source
        # has no durable terminal record of its own yet, so it stays unsettled.
        oracle._ingest_event(_agent_reply_event("$second", "$combined-reply", "LIVE-FUZZ call=2 END call=2"))
        assert oracle.unsettled_required_sources() == ["$first"]
        assert oracle.resolve_response_ref("response:op:2") == "$combined-reply"
        with pytest.raises(KeyError, match="response event not observed"):
            oracle.resolve_response_ref("response:op:1")

        # A completed no-response supersession record proves the older source was
        # legitimately skipped; it now settles and its cover is the combined reply.
        second_record = TurnRecord(
            source_event_ids=("$second",),
            response_event_id="$combined-reply",
            completed=True,
        )
        superseded_record = TurnRecord(source_event_ids=("$first",), response_event_id=None, completed=True)
        _write_ledger(ledger_path, {"$first": superseded_record, "$second": second_record})
        oracle.refresh_ledger_attributions(min_interval=0.0)
        assert oracle.unsettled_required_sources() == []
        assert oracle.resolve_response_ref("response:op:1") == "$combined-reply"

        # A dedicated response-backed record instead attributes the older source
        # directly to its own reply.
        dedicated_record = TurnRecord(
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
        oracle._ingest_event(_agent_reply_event("$second", "$combined-reply", "LIVE-FUZZ call=2 END call=2"))

        second_record = TurnRecord(
            source_event_ids=("$second",),
            response_event_id="$combined-reply",
            completed=True,
        )
        # An incomplete older record never proves supersession.
        incomplete = TurnRecord(source_event_ids=("$first",), response_event_id=None, completed=False)
        _write_ledger(ledger_path, {"$first": incomplete, "$second": second_record})
        oracle.refresh_ledger_attributions(min_interval=0.0)
        assert oracle.unsettled_required_sources() == ["$first"]
        with pytest.raises(KeyError, match="response event not observed"):
            oracle.resolve_response_ref("response:op:1")
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
    """Durable attribution must cover every required source and every visible reply.

    An older source with no completed record of its own can never be inferred
    superseded from chronology; only production's own completed no-response
    record settles it.
    """
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

        coalesced_record = TurnRecord(
            source_event_ids=("$first", "$second"),
            response_event_id="$combined-reply",
            completed=True,
        )
        # One coalesced record attributing both sources to one visible reply passes.
        _write_ledger(ledger_path, {"$first": coalesced_record, "$second": coalesced_record})
        assert auditor._assert_ledger_attribution(replies) == {
            "ledger_attributed_sources": 2,
            "ledger_superseded_sources": 0,
        }

        # The newest source alone with the older one absent fails: the older
        # source has no durable terminal record at all.
        _write_ledger(ledger_path, {"$second": coalesced_record})
        with pytest.raises(AssertionError, match="superseded chain source"):
            auditor._assert_ledger_attribution(replies)

        # An incomplete older record is dropped by the loader, so it also fails.
        incomplete_first = TurnRecord(source_event_ids=("$first",), response_event_id=None, completed=False)
        anchor_second = TurnRecord(
            source_event_ids=("$second",),
            response_event_id="$combined-reply",
            completed=True,
        )
        _write_ledger(ledger_path, {"$first": incomplete_first, "$second": anchor_second})
        with pytest.raises(AssertionError, match="superseded chain source"):
            auditor._assert_ledger_attribution(replies)

        # A completed no-response record for the older source proves supersession.
        superseded_first = TurnRecord(source_event_ids=("$first",), response_event_id=None, completed=True)
        _write_ledger(ledger_path, {"$first": superseded_first, "$second": anchor_second})
        assert auditor._assert_ledger_attribution(replies) == {
            "ledger_attributed_sources": 1,
            "ledger_superseded_sources": 1,
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
async def test_final_body_audit_accepts_exact_resume_relay_chain() -> None:
    """An interrupted note passes only with the exact ``I <- R <- A`` chain."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    auditor = _recovery_auditor(client)
    try:
        events: dict[str, Any] = {"$reply": _interrupted_reply()}
        replies = {"$source": {"$reply"}}
        # An interrupted note with no resume chain fails.
        with pytest.raises(AssertionError, match="non-canonical body"):
            auditor._assert_final_bodies_complete(events, replies)

        # The relay R replies to the interrupted response I in the same thread.
        events["$relay"] = _threaded_reply_event(
            sender="@router:example",
            event_id="$relay",
            thread_root="$root",
            in_reply_to="$reply",
            body="resume",
        )
        # The completed agent response A replies to the relay R in the thread.
        events["$resumed"] = _threaded_reply_event(
            sender="@agent:example",
            event_id="$resumed",
            thread_root="$root",
            in_reply_to="$relay",
            body="LIVE-FUZZ call=11 END call=11",
        )
        assert auditor._assert_final_bodies_complete(events, replies) == 1
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
            "$relay": _threaded_reply_event(
                sender="@router:example",
                event_id="$relay",
                thread_root="$root",
                in_reply_to="$other-interrupted",
                body="resume",
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
            "$relay": _threaded_reply_event(
                sender="@router:example",
                event_id="$relay",
                thread_root="$root",
                in_reply_to="$reply",
                body="resume",
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
            "$relay": _threaded_reply_event(
                sender="@router:example",
                event_id="$relay",
                thread_root="$root",
                in_reply_to="$reply",
                body="resume",
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
    ledger_path = tmp_path / "general_responded.json"
    marker_a = _source_marker("op:1", ORIGINAL_REVISION)
    marker_b = _source_marker("op:2", ORIGINAL_REVISION)
    auditor = _model_source_auditor(
        ledger_path=ledger_path,
        expected_sources={"$a": "op:1", "$b": "op:2"},
        source_current_markers={"$a": marker_a, "$b": marker_b},
        observed={7: frozenset({marker_b})},
    )
    try:
        record = TurnRecord(source_event_ids=("$a",), response_event_id="$reply-a", completed=True)
        _write_ledger(ledger_path, {"$a": record})
        events = {"$reply-a": _agent_reply_event("$a", "$reply-a", _short_body_for(7))}
        with pytest.raises(AssertionError, match="without current source markers"):
            auditor._assert_model_saw_current_sources(events)
    finally:
        await auditor.client.close()


@pytest.mark.asyncio
async def test_model_source_audit_rejects_pre_edit_revision(tmp_path: Path) -> None:
    """A response generated from a source's OLD revision fails after a later valid edit."""
    ledger_path = tmp_path / "general_responded.json"
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
        record = TurnRecord(source_event_ids=("$a",), response_event_id="$reply-a", completed=True)
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
    ledger_path = tmp_path / "general_responded.json"
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
        coalesced = TurnRecord(source_event_ids=("$a", "$b"), response_event_id="$combined", completed=True)
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
    """A completed no-response supersession record requires no marker."""
    ledger_path = tmp_path / "general_responded.json"
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
        superseded = TurnRecord(source_event_ids=("$a",), response_event_id=None, completed=True)
        _write_ledger(ledger_path, {"$a": superseded})
        auditor._assert_model_saw_current_sources({})
    finally:
        await auditor.client.close()


@pytest.mark.asyncio
async def test_model_source_audit_ignores_redacted_source_marker(tmp_path: Path) -> None:
    """A response-backed record whose only source was redacted requires no marker.

    Production tombstones a durably redacted source and refuses to regenerate an
    edit against it, so the still-visible response legitimately reflects the
    pre-redaction body. Requiring the source's post-redaction edit marker would
    demand behavior production correctly declines. This is the root:41 live-gate
    false positive.
    """
    ledger_path = tmp_path / "general_responded.json"
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
        redacted = TurnRecord(
            source_event_ids=("$a",),
            redacted_source_event_ids=("$a",),
            response_event_id="$reply-a",
            completed=True,
        )
        _write_ledger(ledger_path, {"$a": redacted})
        events = {"$reply-a": _agent_reply_event("$a", "$reply-a", _short_body_for(4))}
        # No marker is required for the tombstoned source, so the audit passes.
        auditor._assert_model_saw_current_sources(events)
    finally:
        await auditor.client.close()


@pytest.mark.asyncio
async def test_model_source_audit_requires_live_sibling_not_redacted_sibling(tmp_path: Path) -> None:
    """A coalesced record still requires the live sibling's marker but not the redacted one."""
    ledger_path = tmp_path / "general_responded.json"
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
        coalesced = TurnRecord(
            source_event_ids=("$a", "$b"),
            redacted_source_event_ids=("$b",),
            response_event_id="$combined",
            completed=True,
        )
        _write_ledger(ledger_path, {"$a": coalesced, "$b": coalesced})
        events = {"$combined": _agent_reply_event("$a", "$combined", _short_body_for(9))}
        # Live sibling $a satisfied, redacted sibling $b excluded -> passes.
        auditor._assert_model_saw_current_sources(events)

        # Dropping the live sibling's marker still fails: only the redacted one is excused.
        auditor.observed_markers_for = lambda _call_id: frozenset()
        with pytest.raises(AssertionError, match="without current source markers"):
            auditor._assert_model_saw_current_sources(events)
    finally:
        await auditor.client.close()


class _FakeStack:
    """Minimal stand-in exposing the fields the failure-bundle path reads.

    Records teardown ordering so tests can assert MindRoom stops before the
    Tuwunel log is captured and before evidence is copied.
    """

    def __init__(self, storage_path: Path, log_path: Path, *, tuwunel_log: str = "tuwunel line\n") -> None:
        self.storage_path = storage_path
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
    ledger = storage / "tracking" / "general_responded.json"
    ledger.write_text(json.dumps({"schema_version": TurnRecordCodec.schema_version(), "turns": {}}), encoding="utf-8")
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
    oracle = _snapshot_oracle()
    runner = object.__new__(LiveFuzzRunner)
    runner.oracle = oracle

    try:
        _persist_failure_bundle(bundle, stack, runner, AssertionError("reply invariant failed"))
    finally:
        await oracle.client.close()

    # Teardown deletes the stack's temp storage; the bundle must not point into it.
    import shutil  # noqa: PLC0415 - local to this teardown-simulation test

    shutil.rmtree(tmp_path / "mindroom_data")

    directory = bundle.directory
    assert directory.exists()
    assert (directory / "scenario.json").read_text(encoding="utf-8").strip() == scenario.to_json()
    assert (directory / "provenance.json").exists()
    journal_lines = (directory / "realized_journal.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(journal_lines[0])["event_id"] == "$sent"
    assert "mindroom line" in (directory / "mindroom.log").read_text(encoding="utf-8")
    assert (directory / "handled_turns.json").exists()
    observations = json.loads((directory / "model_observations.json").read_text(encoding="utf-8"))
    assert observations["3"] == [_source_marker("op:1", ORIGINAL_REVISION)]
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
    runner = object.__new__(LiveFuzzRunner)
    runner.oracle = oracle

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
    runner = object.__new__(LiveFuzzRunner)
    runner.oracle = oracle

    try:
        _persist_failure_bundle(bundle, stack, runner, AssertionError("boom"))
    finally:
        await oracle.client.close()

    snapshot_text = (bundle.directory / "oracle_snapshot.json").read_text(encoding="utf-8")
    assert "s_secret_sync_token" not in snapshot_text
    assert "next_batch" not in snapshot_text
