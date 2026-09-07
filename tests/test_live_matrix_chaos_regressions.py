"""Regression checks for lifecycle and compacted-stream chaos observations."""

from __future__ import annotations

import json
import signal
import subprocess
from io import BytesIO
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from scripts.testing import fuzz_live_matrix as live_fuzz

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("method", ["stop_mindroom", "_stop_mindroom"])
@pytest.mark.parametrize("cleanup_seconds", [41.0, 90.0])
def test_graceful_shutdown_allows_staged_cleanup_but_still_kills_a_hang(
    method: str,
    cleanup_seconds: float,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime phase budgets can exceed twenty seconds without permitting an unbounded stop."""
    stack = live_fuzz.ManagedTuwunelStack()
    process = Mock(spec=subprocess.Popen)
    process.pid = 4242
    process.poll.return_value = None
    process.returncode = None
    stack._mindroom_process = process
    signals: list[int] = []

    def send_signal(_pid: int, sent_signal: int) -> None:
        signals.append(sent_signal)

    def wait(*, timeout: float) -> int:
        if signal.SIGKILL in signals:
            process.returncode = -signal.SIGKILL
        elif timeout >= cleanup_seconds:
            process.returncode = 0
        else:
            command = "managed runtime"
            raise subprocess.TimeoutExpired(command, timeout)
        return process.returncode

    process.wait.side_effect = wait
    monkeypatch.setattr(live_fuzz.os, "killpg", send_signal)
    monkeypatch.setattr(live_fuzz, "_cleanup_surviving_process_group", lambda _: False)
    monkeypatch.setattr(stack, "log_count", lambda *_: int(process.returncode == 0))
    try:
        if method == "stop_mindroom":
            assert stack.stop_mindroom() is (cleanup_seconds == 41.0)
        elif cleanup_seconds == 41.0:
            stack._stop_mindroom()
        else:
            with pytest.raises(TimeoutError, match="required SIGKILL"):
                stack._stop_mindroom()
        assert signals == ([signal.SIGINT] if cleanup_seconds == 41.0 else [signal.SIGINT, signal.SIGKILL])
        assert stack._mindroom_process is None
    finally:
        stack._mindroom_process = None
        stack.close()


@pytest.mark.asyncio
async def test_graceful_chaos_outage_rejects_failed_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A forced kill, bad exit, or missing shutdown marker cannot count as a clean outage."""
    stack = live_fuzz.ManagedTuwunelStack()
    client = live_fuzz.LiveMatrixClient("http://matrix.invalid", "!room:test")
    runner = live_fuzz.LiveFuzzRunner(
        stack,
        (client,),
        live_fuzz.LiveFuzzScenario(1, (), profile="chaos"),
        reply_timeout=1,
        settle_seconds=0,
    )
    monkeypatch.setattr(stack, "stop_mindroom", lambda: False)
    try:
        with pytest.raises(AssertionError, match=r"did not shut down cleanly.*outage"):
            await runner._apply_lifecycle(live_fuzz.LiveOperationKind.STOP_MINDROOM, 0)
        assert runner.outage_count == 0
    finally:
        await client.close()
        stack.close()


@pytest.mark.asyncio
async def test_bundled_final_edit_retains_timestamp_against_older_standalone_edit() -> None:
    """An older edit arriving after a bundled final must not restore an incomplete body."""
    client = live_fuzz.LiveMatrixClient("http://matrix.invalid", "!room:test")
    oracle = live_fuzz.ExactReplyOracle(
        client,
        "@agent:test",
        expected_body_for=lambda _call_id: "LIVE-FUZZ call=1 END call=1",
    )
    oracle.expect("op:0", "$source")
    final = {
        "event_id": "$final",
        "type": "m.room.message",
        "sender": "@agent:test",
        "origin_server_ts": 300,
        "content": {
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
            "m.new_content": {"body": "LIVE-FUZZ call=1 END call=1"},
        },
    }
    original = {
        "event_id": "$reply",
        "type": "m.room.message",
        "sender": "@agent:test",
        "origin_server_ts": 100,
        "content": {
            "body": "Thinking...",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$source", "m.in_reply_to": {"event_id": "$source"}},
        },
        "unsigned": {"m.relations": {"m.replace": {"event": final}}},
    }
    partial = {
        "event_id": "$partial",
        "type": "m.room.message",
        "sender": "@agent:test",
        "origin_server_ts": 200,
        "content": {
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
            "m.new_content": {"body": "LIVE-FUZZ call=1 partial"},
        },
    }
    try:
        oracle._ingest_event(original)
        oracle._ingest_event(partial)
        assert oracle.latest_reply_bodies["$reply"][1] == "LIVE-FUZZ call=1 END call=1"
    finally:
        await client.close()


@pytest.mark.parametrize("sources", [("$source",), ("$source", "$live")])
def test_completed_turn_preserves_lazy_redaction_cleanup(
    tmp_path: Path,
    sources: tuple[str, ...],
) -> None:
    """A durable tombstone may defer session cleanup until the next response."""
    record = live_fuzz.TurnRecord.create(
        source_event_ids=sources,
        response_event_id="$reply",
        completed=True,
        redacted_source_event_ids=("$source",),
        pending_redaction_cleanup_event_ids=("$source",),
    )
    rows = {"$source": live_fuzz.TurnRecordCodec._to_ledger_record(record)}
    assert live_fuzz._decode_ledger_rows(tmp_path / "event_journal.db", rows, strict=True) == {"$source": record}


def test_generated_cleanup_probes_are_serialized_and_replay_verbatim() -> None:
    """Qualification adds visible operations while replay preserves the saved workload."""
    scenario = live_fuzz.LiveFuzzScenario(
        2,
        ((live_fuzz.LiveOperation(0, live_fuzz.LiveOperationKind.REDACTION, 1, "root:1"),),),
        profile="chaos",
    )
    qualified = live_fuzz._with_redaction_cleanup_probes(scenario)
    assert qualified.batches[:1] == scenario.batches
    assert [batch[0].kind for batch in qualified.batches[1:]] == [
        live_fuzz.LiveOperationKind.CHECKPOINT,
        live_fuzz.LiveOperationKind.THREAD_MESSAGE,
        live_fuzz.LiveOperationKind.CHECKPOINT,
    ]
    probe = qualified.batches[-2][0]
    assert probe.cleanup_sources == ("root:1",)
    assert probe.thread == 1
    assert live_fuzz.LiveFuzzScenario.from_json(qualified.to_json()) == qualified
    assert live_fuzz.LiveFuzzScenario.from_json(scenario.to_json()) == scenario


@pytest.mark.parametrize("bad_source", ["root:0", "op:99"])
def test_cleanup_probe_rejects_unredacted_or_unknown_sources(bad_source: str) -> None:
    """A trace cannot claim cleanup coverage for an unredacted or absent source."""
    operation = live_fuzz.LiveOperation(
        0,
        live_fuzz.LiveOperationKind.THREAD_MESSAGE,
        0,
        "root:0",
        cleanup_sources=(bad_source,),
    )
    with pytest.raises(ValueError, match="cleanup probe"):
        live_fuzz.LiveFuzzScenario(1, ((operation,),)).validate()


@pytest.mark.parametrize(
    "failure",
    ["pending", "history", "edit_history", "missing_observation", "unrelated_pending", "edited_probe", None],
)
def test_cleanup_probe_requires_session_cleanup_and_absent_full_request_markers(failure: str | None) -> None:
    """A clean current turn alone cannot hide stale historical input or deferred cleanup."""
    oracle = Mock(spec=live_fuzz.ExactReplyOracle)
    oracle.expected_sources = {"$source": "root:0", "$probe": "op:1"}
    old_marker = live_fuzz._source_marker("root:0", live_fuzz.ORIGINAL_REVISION)
    edit_marker = live_fuzz._source_marker("root:0", "edit:0")
    new_marker = live_fuzz._source_marker("op:1", live_fuzz.ORIGINAL_REVISION)
    if failure == "edited_probe":
        new_marker = live_fuzz._source_marker("op:1", "edit:2")
    observed = {new_marker}
    if failure == "history":
        observed.add(old_marker)
    if failure == "edit_history":
        observed.add(edit_marker)
    if failure == "missing_observation":
        observed.clear()
    auditor = live_fuzz.FinalStateAuditor(
        Mock(spec=live_fuzz.LiveMatrixClient),
        oracle,
        agent_id="@agent:test",
        expected_body_for=lambda _: "unused",
        cleanup_probes={"$probe": ("$source",)},
        source_revision_markers={"$source": {"$edit": edit_marker}, "$probe": {"$probe_edit": new_marker}},
        source_current_markers={"$probe": new_marker},
        full_request_markers_for=lambda _: frozenset(observed),
    )
    records = {
        "$source": live_fuzz.TurnRecord.create(
            source_event_ids=("$source",),
            completed=True,
            redacted_source_event_ids=("$source",),
            pending_redaction_cleanup_event_ids=("$source",) if failure == "pending" else (),
        ),
        "$probe": live_fuzz.TurnRecord.create(source_event_ids=("$probe",), response_event_id="$reply", completed=True),
    }
    if failure == "unrelated_pending":
        records["$source"] = live_fuzz.TurnRecord.create(
            source_event_ids=("$source", "$later"),
            completed=True,
            redacted_source_event_ids=("$source", "$later"),
            pending_redaction_cleanup_event_ids=("$later",),
        )
    events = {
        "$reply": {
            "event_id": "$reply",
            "sender": "@agent:test",
            "type": "m.room.message",
            "content": {"body": "LIVE-FUZZ call=1 END call=1"},
        },
    }
    if failure in {None, "unrelated_pending", "edited_probe"}:
        auditor._assert_redaction_cleanup_probes(events, records)
    else:
        with pytest.raises(AssertionError, match="redaction cleanup probe"):
            auditor._assert_redaction_cleanup_probes(events, records)


@pytest.mark.parametrize(
    ("kind", "thread"),
    [(live_fuzz.LiveOperationKind.THREAD_MESSAGE, 1), (live_fuzz.LiveOperationKind.EDIT, 0)],
)
def test_cleanup_probe_rejects_wrong_thread_or_nonmessage(kind: live_fuzz.LiveOperationKind, thread: int) -> None:
    """Probe metadata cannot certify a different session or a mutation without a next turn."""
    redaction = live_fuzz.LiveOperation(0, live_fuzz.LiveOperationKind.REDACTION, 0, "root:0")
    probe = live_fuzz.LiveOperation(1, kind, thread, f"root:{thread}", cleanup_sources=("root:0",))
    with pytest.raises(ValueError, match="cleanup probe"):
        live_fuzz.LiveFuzzScenario(2, ((redaction,), (probe,))).validate()


def test_model_capture_keeps_historical_markers_separate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full request evidence sees history that the exact current-source oracle excludes."""
    old = live_fuzz._source_marker("root:0", live_fuzz.ORIGINAL_REVISION)
    current = live_fuzz._source_marker("op:1", live_fuzz.ORIGINAL_REVISION)
    payload = json.dumps(
        {"messages": [{"role": "user", "content": old}, {"role": "user", "content": current}]},
    ).encode()
    handler = object.__new__(live_fuzz._ModelHandler)
    handler.path = "/v1/chat/completions"
    handler.headers = {"Content-Length": str(len(payload))}
    handler.rfile = BytesIO(payload)
    monkeypatch.setattr(handler, "_send_json", lambda _: None)
    live_fuzz._ModelHandler.reset_observations()
    try:
        handler.do_POST()
        assert live_fuzz._ModelHandler.observed_markers_for(1) == {current}
        assert live_fuzz._ModelHandler.full_request_markers_for(1) == {old, current}
        assert live_fuzz._ModelHandler.full_request_observations_snapshot() == {1: sorted((old, current))}
    finally:
        live_fuzz._ModelHandler.reset_observations()


@pytest.mark.asyncio
async def test_fuzz_cleanup_probe_waits_for_durable_tombstone_before_send(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit fuzz probe must not race the source redaction callback."""
    stack = live_fuzz.ManagedTuwunelStack()
    client = live_fuzz.LiveMatrixClient("http://matrix.invalid", "!room:test")
    runner = live_fuzz.LiveFuzzRunner(
        stack,
        (client,),
        live_fuzz.LiveFuzzScenario(1, ()),
        reply_timeout=1,
        settle_seconds=0,
    )
    runner.event_ids["root:0"] = "$old"
    runner._pending_source_tombstones.add("$old")
    tombstone = live_fuzz.TurnRecord.create(
        source_event_ids=("$old",),
        completed=True,
        redacted_source_event_ids=("$old",),
        pending_redaction_cleanup_event_ids=("$old",),
    )

    async def pump(**_kwargs: object) -> None:
        runner.oracle._ledger_records["$old"] = tombstone

    async def send(*_args: object) -> str:
        assert runner.oracle.source_tombstoned("$old")
        assert not runner._pending_source_tombstones
        return "$probe"

    monkeypatch.setattr(runner.oracle, "pump", pump)
    monkeypatch.setattr(runner.oracle, "refresh_ledger_attributions", lambda **_: None)
    monkeypatch.setattr(runner, "_send_expected_message", send)
    monkeypatch.setattr(runner, "_resolve_target", AsyncMock(return_value="$old"))
    monkeypatch.setattr(runner, "_room_for_thread", lambda _: "!room:test")
    try:
        probe = live_fuzz.LiveOperation(
            1,
            live_fuzz.LiveOperationKind.THREAD_MESSAGE,
            0,
            "root:0",
            cleanup_sources=("root:0",),
        )
        assert (await runner._apply(probe))[1] == "$probe"
    finally:
        await client.close()
        stack.close()


def test_malformed_abandoned_manifest_reports_its_path_without_cleanup(tmp_path: Path) -> None:
    """Corrupt recovery state must identify its file and remain untouched for inspection."""
    manifest = tmp_path / "runs" / "fuzzbroken.json"
    manifest.parent.mkdir()
    manifest.write_text('{"instance_name":')
    stack = live_fuzz.ManagedTuwunelStack(state_root=tmp_path)
    try:
        with pytest.raises(RuntimeError, match="invalid abandoned live-fuzz manifest") as error:
            stack._recover_abandoned_runs()
        assert str(manifest) in str(error.value)
        assert manifest.read_text() == '{"instance_name":'
    finally:
        stack.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["fuzz", "chaos", "short-stream-correctness", "saturation"])
async def test_initial_traffic_waits_for_durable_room_baselines(profile: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """No startup request may enter the first cold timeline as ignored history."""
    stack = live_fuzz.ManagedTuwunelStack()
    client = live_fuzz.LiveMatrixClient("http://matrix.invalid", "!room:test")
    runner = live_fuzz.LiveFuzzRunner(
        stack,
        (client,),
        live_fuzz.LiveFuzzScenario(1, (), profile=profile),
        reply_timeout=1,
        settle_seconds=0,
    )
    checks = 0

    def baseline_ready() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 2

    async def first_traffic() -> None:
        assert checks >= 2
        msg = "traffic started after baseline"
        raise RuntimeError(msg)

    monkeypatch.setattr(stack, "managed_room_baseline_ready", baseline_ready)
    monkeypatch.setattr(client, "register", AsyncMock())
    monkeypatch.setattr(client, "join_room", AsyncMock())
    monkeypatch.setattr(client, "sync_incremental", AsyncMock())
    monkeypatch.setattr(runner.oracle, "initialize", AsyncMock())
    monkeypatch.setattr(runner, "_await_first_baseline_response", first_traffic)
    monkeypatch.setattr(runner, "_run_short_stream_correctness", first_traffic)
    try:
        with pytest.raises(RuntimeError, match="traffic started after baseline"):
            await runner.run()
    finally:
        await client.close()
        stack.close()
