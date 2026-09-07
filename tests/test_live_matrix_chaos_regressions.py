"""Regression checks for lifecycle and compacted-stream chaos observations."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from scripts.testing import fuzz_live_matrix as live_fuzz

if TYPE_CHECKING:
    from pathlib import Path


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
def test_completed_turn_with_pending_redaction_cleanup_is_not_terminal(
    tmp_path: Path,
    sources: tuple[str, ...],
) -> None:
    """An answered turn stays unsettled until its later redaction cleanup finishes."""
    record = live_fuzz.TurnRecord.create(
        source_event_ids=sources,
        response_event_id="$reply",
        completed=True,
        redacted_source_event_ids=("$source",),
        pending_redaction_cleanup_event_ids=("$source",),
    )
    rows = {"$source": live_fuzz.TurnRecordCodec._to_ledger_record(record)}
    assert live_fuzz._decode_ledger_rows(tmp_path / "event_journal.db", rows, strict=False) == {}
    with pytest.raises(AssertionError, match="incomplete"):
        live_fuzz._decode_ledger_rows(tmp_path / "event_journal.db", rows, strict=True)


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
