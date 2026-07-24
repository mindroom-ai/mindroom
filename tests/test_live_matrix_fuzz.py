"""Tests for replayable real-server Matrix fuzz traces and their oracle."""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import signal
import sys
from collections import defaultdict
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from mindroom.dispatch_source import AUTO_RESUME_MESSAGE
from mindroom.handled_turns import TurnRecord, TurnRecordCodec
from mindroom.streaming import RESTART_INTERRUPTED_RESPONSE_NOTE

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path

import pytest

import scripts.testing.fuzz_live_matrix as live_fuzz
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
    ManagedTuwunelStack,
    _body_call_id,
    _ModelHandler,
    _parse_markers,
    _persist_failure_bundle,
    _run_command,
    _sanitized_oracle_snapshot,
    _SentPayload,
    _SentRecord,
    _source_marker,
    _validated_child_provenance,
    chaos_scenario_from_seed,
    live_scenario_from_seed,
    saturation_scenario,
)


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
    ("hard_kill", "expected_signal"),
    [(False, signal.SIGINT), (True, signal.SIGKILL)],
)
def test_stop_mindroom_targets_exact_process_group(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hard_kill: bool,
    expected_signal: signal.Signals,
) -> None:
    """Graceful and hard stops signal the owned child group, then reap it."""

    class FakeProcess:
        pid = 4242

        def __init__(self) -> None:
            self.waited = False

        def poll(self) -> None:
            return None

        def wait(self, *, timeout: float) -> int:
            assert timeout in {10, 20}
            self.waited = True
            return 0

    process = FakeProcess()
    stack = object.__new__(ManagedTuwunelStack)
    stack._mindroom_process = process
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr("scripts.testing.fuzz_live_matrix.os.killpg", lambda pid, sig: signals.append((pid, sig)))

    stack._stop_mindroom(kill=hard_kill)

    assert signals == [(process.pid, expected_signal)]
    assert process.waited
    assert stack._mindroom_process is None


def test_stop_mindroom_kills_group_after_leader_already_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owned descendants must die even when the process-group leader exited."""

    class FakeProcess:
        pid = 4242

        def poll(self) -> int:
            return 0

    stack = object.__new__(ManagedTuwunelStack)
    stack._mindroom_process = FakeProcess()
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr("scripts.testing.fuzz_live_matrix.os.killpg", lambda pid, sig: signals.append((pid, sig)))

    stack._stop_mindroom()

    assert signals == [(4242, signal.SIGKILL)]
    assert stack._mindroom_process is None


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
    """Restart recovery may validly answer a structurally valid resume relay."""
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
            {
                "event_id": "$resume-relay",
                "sender": "@router:example",
                "type": "m.room.message",
                "content": {
                    "body": f"@agent {AUTO_RESUME_MESSAGE}",
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
            _threaded_reply_event(
                sender="@router:example",
                event_id="$false-relay",
                thread_root="$root",
                in_reply_to="$completed",
                body=f"@agent {AUTO_RESUME_MESSAGE}",
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
async def test_redacting_settled_coalesced_source_does_not_mark_optional(tmp_path: Path) -> None:
    """Durably settled coalesced work stays required after source redaction."""
    matrix_client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    ledger_path = tmp_path / "general_responded.json"
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
                "$first": TurnRecord(source_event_ids=("$first",), response_event_id=None, completed=True),
                "$second": TurnRecord(
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
        runner._resolve_target = lambda _logical_ref: asyncio.sleep(0, result="$first")  # type: ignore[method-assign]
        runner._client_for_operation = lambda _operation: RedactionClient()  # type: ignore[method-assign]
        runner._room_for_thread = lambda _thread: "!room:example"  # type: ignore[method-assign]

        operation = LiveOperation(3, LiveOperationKind.REDACTION, 0, "op:1")
        await runner._apply(operation)

        assert "$first" not in oracle.optional_sources
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

        foreign = TurnRecord(
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
async def test_visible_optional_reply_requires_durable_attribution(tmp_path: Path) -> None:
    """Optional means zero replies are allowed, not unattributed visible replies."""
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
        oracle.expect("op:1", "$optional", thread=0)
        oracle.mark_source_optional("$optional")
        replies = {"$optional": {"$reply"}}

        _write_ledger(ledger_path, {})
        with pytest.raises(AssertionError, match="optional-source reply"):
            auditor._assert_ledger_attribution(replies)

        record = TurnRecord(source_event_ids=("$optional",), response_event_id="$reply", completed=True)
        _write_ledger(ledger_path, {"$optional": record})
        assert auditor._assert_ledger_attribution(replies) == {
            "ledger_attributed_sources": 1,
            "ledger_superseded_sources": 0,
        }

        _write_ledger(
            ledger_path,
            {
                "$optional": TurnRecord(
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
            body=f"@agent {AUTO_RESUME_MESSAGE}",
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
                body=f"@agent {AUTO_RESUME_MESSAGE}",
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
                body=f"@agent {AUTO_RESUME_MESSAGE}",
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
                body=f"@agent {AUTO_RESUME_MESSAGE}",
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


def _revision_runner() -> LiveFuzzRunner:
    """Bare runner exposing only the source-revision maintenance state."""
    runner = object.__new__(LiveFuzzRunner)
    runner.source_current_markers = {}
    runner.source_revision_markers = defaultdict(dict)
    runner._source_revision_stack = {}
    runner._edit_event_source = {}
    return runner


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


def test_child_provenance_uses_loaded_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact provenance must describe the child overlay, not the parent install."""
    project_root = tmp_path / "mindroom"
    monkeypatch.setattr(live_fuzz, "PROJECT_ROOT", project_root)
    attestation = tmp_path / "runtime-attestation.json"
    attestation.write_text(
        json.dumps(
            {
                "mindroom_module_path": str(tmp_path / "mindroom" / "__init__.py"),
                "nio_module_path": str(tmp_path / "overlay" / "nio" / "__init__.py"),
                "nio_version": "1.2.3",
                "python": "3.13",
            },
        ),
        encoding="utf-8",
    )

    def git_state(path: Path, **_kwargs: object) -> tuple[str, bool]:
        return ("nio-head", False) if "overlay" in str(path) else ("mindroom-head", False)

    monkeypatch.setattr("scripts.testing.fuzz_live_matrix._git_state_for_file", git_state)
    monkeypatch.setattr(
        "scripts.testing.fuzz_live_matrix._git_root_for_path",
        lambda path: tmp_path / "overlay" if "overlay" in str(path) else project_root,
    )
    monkeypatch.setattr(
        "scripts.testing.fuzz_live_matrix._git_revision",
        lambda path: "nio-head" if "overlay" in str(path) else "mindroom-head",
    )

    provenance = _validated_child_provenance(
        attestation,
        overlay=str(tmp_path / "overlay"),
    )

    assert provenance["nio_module_path"] == str((tmp_path / "overlay" / "nio" / "__init__.py").resolve())
    assert provenance["nio_revision"] == "nio-head"
    assert provenance["nio_expected_revision"] == "nio-head"


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
        _validated_child_provenance(attestation, overlay=None)


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
        _validated_child_provenance(attestation, overlay=None)


def test_child_provenance_rejects_same_head_from_other_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commit equality cannot substitute for the requested editable checkout."""
    project_root = tmp_path / "mindroom"
    monkeypatch.setattr(live_fuzz, "PROJECT_ROOT", project_root)
    overlay = tmp_path / "requested-overlay"
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

    with pytest.raises(RuntimeError, match="outside requested editable overlay"):
        _validated_child_provenance(attestation, overlay=str(overlay))


def test_start_mindroom_uses_editable_overlay_and_persists_each_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attested nio path must remain inside its requested Git checkout."""
    stack = ManagedTuwunelStack(state_root=tmp_path / "state")
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
        monkeypatch.setenv("MINDROOM_LIVE_FUZZ_UV_WITH", "/persistent/mindroom-nio")
        monkeypatch.setattr("scripts.testing.fuzz_live_matrix.subprocess.Popen", popen)

        stack._start_mindroom()

        assert commands
        assert commands[0][2:4] == ["--with-editable", "/persistent/mindroom-nio"]
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

    class FakeClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.index = len(clients)
            clients.append(self)

        async def close(self) -> None:
            closed.append(self.index)
            if self.index == 0:
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


def test_pass_receipt_survives_bundle_discard(tmp_path: Path) -> None:
    """A successful exact-head run keeps compact provenance after bulky cleanup."""
    root = tmp_path / "artifacts"
    bundle = FailureBundle.create(root, "run-pass", scenario=_bundle_scenario(), provenance={"parent": True})
    provenance = {"mindroom_revision": "abc", "nio_revision": "def"}

    receipt = bundle.retain_pass_receipt({"status": "PASS"}, provenance)
    bundle.discard()

    assert receipt.exists()
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["cleanup"] == "PASS"
    assert payload["result"]["status"] == "PASS"
    assert payload["provenance"] == provenance
    assert len(payload["scenario_sha256"]) == 64


def test_stack_close_attempts_every_stage_after_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One teardown failure must not skip later cleanup stages."""
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

    def stop_mindroom() -> None:
        events.append("mindroom")
        message = "stop failed"
        raise KeyboardInterrupt(message)

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

    with pytest.raises(ExceptionGroup) as raised:
        stack.close()

    assert events == [
        "mindroom",
        "log",
        "shutdown",
        "server_close",
        "thread",
        "instance",
        "temp",
        "manifest",
        "lease",
    ]
    assert len(raised.value.exceptions) == 2


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
        stack._write_manifest(
            state="ready",
            matrix_port=18008,
            api_port=18765,
            mindroom_pid=1234,
        )
        payload = json.loads(stack.manifest_path.read_text(encoding="utf-8"))

        assert payload["instance_name"] == stack.instance_name
        assert payload["artifact_directory"] == str(artifact)
        assert payload["state"] == "ready"
        assert payload["matrix_port"] == 18008
        assert payload["api_port"] == 18765
        assert payload["mindroom_pid"] == 1234
        assert not stack.manifest_path.with_suffix(".tmp").exists()
    finally:
        stack.temp_dir.cleanup()


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
                "instance_name": "fuzz-old",
                "project_root": str(old_root),
                "state": "cleanup_failed",
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


@pytest.mark.asyncio
async def test_send_expected_message_defers_reply_checks_until_registration() -> None:
    """A fast reply cannot be rejected while its Matrix source ID is in flight."""
    matrix_client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(matrix_client, "@agent:example")
    runner = object.__new__(LiveFuzzRunner)
    runner.oracle = oracle
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
    runner = object.__new__(LiveFuzzRunner)
    runner.oracle = oracle

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
    monkeypatch.setattr(live_fuzz, "_run_provenance", dict)
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
    monkeypatch.setattr(live_fuzz, "_run_provenance", dict)
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
    monkeypatch.setattr(live_fuzz, "_run_provenance", dict)
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
    monkeypatch.setattr(live_fuzz, "_run_provenance", dict)
    monkeypatch.setattr(live_fuzz, "ManagedTuwunelStack", InterruptedStack)
    monkeypatch.setattr(live_fuzz, "_run_live", fail_run)

    with pytest.raises(AssertionError, match="primary invariant"):
        live_fuzz.main()

    assert stack_events == ["start", "stop", "close"]
    cleanup_files = list((tmp_path / "artifacts").glob("*/cleanup_error.txt"))
    assert len(cleanup_files) == 1
    assert "KeyboardInterrupt" in cleanup_files[0].read_text(encoding="utf-8")


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
    runner = object.__new__(LiveFuzzRunner)
    runner.oracle = oracle

    try:
        _persist_failure_bundle(bundle, stack, runner, AssertionError("boom"))
    finally:
        await oracle.client.close()

    snapshot_text = (bundle.directory / "oracle_snapshot.json").read_text(encoding="utf-8")
    assert "s_secret_sync_token" not in snapshot_text
    assert "next_batch" not in snapshot_text
