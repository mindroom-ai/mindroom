"""Regression checks for lifecycle and compacted-stream chaos observations."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass
from io import BytesIO, StringIO
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock, patch

import nio
import pytest
import structlog
from agno.run.base import RunStatus

from mindroom.ai_run_metadata import build_ai_run_metadata_content
from mindroom.cli import main as cli_main
from mindroom.config.main import Config
from mindroom.constants import AI_RUN_METADATA_KEY
from mindroom.conversation_resolver import ConversationResolver, ConversationResolverDeps
from mindroom.conversation_state_writer import ConversationStateWriter
from mindroom.dispatch_handoff import PreparedIngress
from mindroom.dispatch_replay_guard import has_newer_unresponded_in_thread, has_newer_unresponded_journal_thread_event
from mindroom.event_journal import DeliveryStage, EventClass, EventJournalStore, EventKind, InboundEvent
from mindroom.handled_turns import HandledTurnLedger
from mindroom.journal_dispatch import JournalCallbacks, JournalDispatcher
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.message_builder import build_matrix_edit_content
from mindroom.tool_system.runtime_context import ToolRuntimeSupport
from mindroom.turn_record import RevisionReplay, TurnRecord
from mindroom.turn_store import TurnStore, TurnStoreDeps
from scripts.testing import fuzz_live_matrix as live_fuzz

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.event_journal import PrincipalStore


def _redaction_observer_store() -> TurnStore:
    """Expose typed owner dependencies while isolating the original mutation boundary."""
    resolver_deps = Mock(spec=ConversationResolverDeps)
    resolver_deps.matrix_id = MatrixID.parse("@general:example")
    deps = Mock(spec=TurnStoreDeps)
    deps.agent_name = "general"
    deps.resolver = ConversationResolver(resolver_deps)
    store = object.__new__(TurnStore)
    store.deps = deps
    store._ledger = Mock(spec=HandledTurnLedger)
    store._ledger.all_turn_records.return_value = ()
    return store


@dataclass
class _SupersessionCase:
    journal: EventJournalStore
    oracle: live_fuzz.ExactReplyOracle
    auditor: live_fuzz.FinalStateAuditor
    events: dict[str, dict[str, Any]]


async def _observe_guard_decision(
    principal: PrincipalStore,
    event: dict[str, Any],
    history: list[dict[str, Any]],
    *,
    output: StringIO,
    use_history: bool,
    handled: frozenset[str] = frozenset(),
) -> None:
    """Capture actual replay-guard decisions across initial admission and retries."""
    logger = cast(
        "structlog.stdlib.BoundLogger",
        structlog.wrap_logger(
            structlog.PrintLogger(file=output),
            processors=[structlog.dev.ConsoleRenderer(colors=True)],
        ).bind(agent="general", logger="mindroom.bot"),
    )
    prepared = PreparedIngress(
        event["sender"],
        event["event_id"],
        event["content"]["body"],
        event,
        server_timestamp=event["origin_server_ts"],
    )
    if use_history:
        skipped = has_newer_unresponded_in_thread(
            prepared,
            event["sender"],
            [
                ResolvedVisibleMessage(
                    newer["sender"],
                    newer["content"]["body"],
                    newer["origin_server_ts"],
                    newer["event_id"],
                    newer["content"],
                    "$root",
                    newer["event_id"],
                )
                for newer in history
            ],
            may_be_superseded_by_newer_requester_turn=True,
            requester_user_id_for_event=lambda sender, _: sender,
            is_visible_router_voice_echo=lambda *_: False,
            sender_is_trusted_for_ingress_metadata=lambda _: False,
            is_handled=lambda source: source in handled,
            logger=logger,
        )
    else:
        skipped = await has_newer_unresponded_journal_thread_event(
            room_id=event["room_id"],
            event=prepared,
            requester_user_id=event["sender"],
            thread_id="$root",
            may_be_superseded_by_newer_requester_turn=True,
            pending_turns=principal,
            requester_user_id_for_event=lambda sender, _: sender,
            is_visible_router_voice_echo=lambda *_: False,
            sender_is_trusted_for_ingress_metadata=lambda _: False,
            is_handled=lambda source: source in handled,
            logger=logger,
        )
    assert skipped


async def _supersession_case(
    tmp_path: Path,
    *,
    visible_old: bool,
    plain_reply: bool = False,
    plain_reply_parent: str = "$root",
    old_record: bool = True,
    interrupt_settlement: bool = False,
    history_guard: bool = False,
) -> _SupersessionCase:
    """Exercise real admission, guard decisions and ignored-source settlement; fake only Matrix transport."""
    ledger = tmp_path / "event_journal.db"
    journal = EventJournalStore.open_sqlite(ledger)
    principal = journal.principal("general@@agent:example")
    events: dict[str, dict[str, Any]] = {
        source: {
            "event_id": source,
            "room_id": "!room:example",
            "_audit_room_id": "!room:example",
            "sender": "@user:example",
            "type": "m.room.message",
            "origin_server_ts": timestamp,
            "content": {
                "msgtype": "m.text",
                "body": f"MRK[src={logical};rev=orig]",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$root", "m.in_reply_to": {"event_id": "$root"}},
            },
        }
        for source, logical, timestamp in (("$old", "op:1", 10), ("$new", "op:2", 20))
    }
    if plain_reply:
        events["$old"]["content"]["m.relates_to"] = {"m.in_reply_to": {"event_id": plain_reply_parent}}
        events["$new"]["content"]["m.relates_to"] = {"m.in_reply_to": {"event_id": "$old"}}
    for source, event in events.items():
        await principal.admit(
            InboundEvent(
                source,
                "!room:example",
                None if plain_reply else "$root",
                EventKind.MESSAGE,
                EventClass.ACTIONABLE,
                "@user:example",
                event["origin_server_ts"],
                event,
            ),
        )
    output = StringIO()
    await _observe_guard_decision(
        principal,
        events["$old"],
        [events["$new"]],
        output=output,
        use_history=plain_reply or history_guard,
    )
    dispatcher = JournalDispatcher(
        principal,
        Mock(spec=JournalCallbacks),
        lambda room: nio.MatrixRoom(room, "@agent:example"),
    )
    if interrupt_settlement:
        # Cancel at the commit boundary after the real guard and settlement owners ran.
        with (
            patch.object(journal.backend, "write", AsyncMock(side_effect=asyncio.CancelledError)),
            pytest.raises(asyncio.CancelledError),
        ):
            await dispatcher.settle_intentionally_ignored_turn_sources(("$old",))
        assert await principal.unsettled_event_ids() == {"$old", "$new"}
    else:
        await dispatcher.settle_intentionally_ignored_turn_sources(("$old",))
        await principal.settle_many(("$new",))
    old = TurnRecord.create(("$old",), response_event_id="$old-reply" if visible_old else None, completed=False)
    new = TurnRecord.create(("$new",), response_event_id="$new-reply", completed=True)
    records = ([old] if old_record else []) + ([] if interrupt_settlement else [new])
    for record in records:
        await journal.turn_records("general").upsert(
            index_event_ids=record.indexed_event_ids,
            anchor_event_id=record.anchor_event_id,
            record_json=json.dumps(live_fuzz.TurnRecordCodec._to_ledger_record(record)),
        )
    responses = [] if interrupt_settlement else [("$new", "$new-reply", 2, "completed")]
    if visible_old:
        responses.append(("$old", "$old-reply", 1, "error"))
    for source, response, call, status in responses:
        events[response] = {
            **events[source],
            "event_id": response,
            "sender": "@agent:example",
            "origin_server_ts": 30 + call,
            "content": {
                "msgtype": "m.text",
                "body": f"LIVE-FUZZ call={call} END call={call}"
                + ("\n\n" + live_fuzz.RESTART_INTERRUPTED_RESPONSE_NOTE if status == "error" else ""),
                live_fuzz.STREAM_STATUS_KEY: status,
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$root", "m.in_reply_to": {"event_id": source}},
            },
        }
    client = Mock(spec=live_fuzz.LiveMatrixClient)
    client.room_id = "!room:example"
    client.paginate_room = AsyncMock(return_value=list(events.values()))
    oracle = live_fuzz.ExactReplyOracle(
        client,
        "@agent:example",
        coalescing_threads=True,
        ledger_path=ledger,
        expected_body_for=lambda call: f"LIVE-FUZZ call={call} END call={call}",
    )
    oracle.log_path = tmp_path / "mindroom.log"
    oracle.log_path.write_text(output.getvalue())
    if plain_reply:
        events[plain_reply_parent] = {
            **events["$old"],
            "event_id": plain_reply_parent,
            "origin_server_ts": 1,
            "content": {"msgtype": "m.text", "body": "root"}
            | (
                {"m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}} if plain_reply_parent != "$root" else {}
            ),
        }
    for source, logical in (("$old", "op:1"), ("$new", "op:2")):
        oracle.expect(logical, source)
    for event in events.values():
        oracle._ingest_event(event)
    auditor = live_fuzz.FinalStateAuditor(
        client,
        oracle,
        agent_id=oracle.agent_id,
        ledger_path=ledger,
        expected_body_for=oracle.expected_body_for,
        observed_markers_for=lambda call: frozenset({f"MRK[src=op:{call};rev=orig]"}),
    )
    client.paginate_room = AsyncMock(side_effect=lambda _: list(events.values()))
    oracle.observed_markers_for = auditor.observed_markers_for
    oracle.source_current_markers = {"$old": "MRK[src=op:1;rev=orig]", "$new": "MRK[src=op:2;rev=orig]"}
    return _SupersessionCase(journal, oracle, auditor, events)


@pytest.mark.asyncio
@pytest.mark.parametrize(("visible_old", "old_record"), [(False, False), (False, True), (True, True)])
@pytest.mark.parametrize("plain_reply", [False, True])
async def test_supersession_uses_real_settled_journal_owner(
    tmp_path: Path,
    visible_old: bool,
    old_record: bool,
    plain_reply: bool,
) -> None:
    """Ignoring admitted replay must settle harness debt without completing its old generation."""
    case = await _supersession_case(tmp_path, visible_old=visible_old, old_record=old_record, plain_reply=plain_reply)
    try:
        case.oracle.refresh_ledger_attributions(min_interval=0)
        assert "$old" in case.oracle.supersession_proofs
        metrics = await case.auditor.audit(room_ids=("!room:example",), sent_records=(), redacted_targets={})
        assert metrics["ledger_superseded_sources"] == 1
        assert metrics["completed_final_bodies"] == 1
        assert case.oracle.unsettled_required_sources() == []
        records = live_fuzz.read_ledger_records(tmp_path / "event_journal.db", include_incomplete=True)
        assert ("$old" in records) is old_record
        if old_record:
            assert not records["$old"].completed
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect",
    [
        "missing_journal",
        "foreign_principal",
        "pending_journal",
        "missing_new_journal",
        "wrong_requester",
        "wrong_room",
        "wrong_thread",
        "wrong_timestamp",
        "missing_log",
        "wrong_agent",
        "wrong_log_source",
        "wrong_log_newer",
        "embedded_log",
        "malformed_log",
        "no_anchor",
        "unfinished_anchor",
        "streaming_anchor",
        "wrong_anchor_marker",
        "malformed_ledger",
        "conflicting_ledger",
        "partial_journal",
        "unacknowledged_final",
        "pending_edit",
        "streaming_old",
        "canonical_streaming_old",
        "missing_old_attribution",
        "duplicate_reply",
        "unrelated_recovery",
        "duplicate_relay",
        "wrong_old_marker",
        "pending_cleanup",
        "wrong_log_thread",
        "truncated_log",
        "unproved_owned_source",
        "misplaced_final",
    ],
)
async def test_supersession_rejects_missing_or_foreign_ownership(tmp_path: Path, defect: str) -> None:  # noqa: C901, PLR0912, PLR0915
    """Each missing proof fact must leave the old generation or its independent debt failing."""
    case = await _supersession_case(tmp_path, visible_old=True)
    ledger = tmp_path / "event_journal.db"
    assert case.oracle.log_path is not None
    log = case.oracle.log_path.read_text()
    if defect in {
        "missing_log",
        "wrong_agent",
        "wrong_log_source",
        "wrong_log_newer",
        "embedded_log",
        "malformed_log",
        "wrong_log_thread",
        "truncated_log",
    }:
        changed = {
            "missing_log": "",
            "wrong_agent": log.replace("general", "router"),
            "wrong_log_source": log.replace("$old", "$elsewhere"),
            "wrong_log_newer": log.replace("$new", "$elsewhere"),
            "embedded_log": "message_body='" + log + "'",
            "malformed_log": log.replace("skipped_event_id", "skipped_event"),
            "wrong_log_thread": log.replace("$root", "$elsewhere"),
            "truncated_log": log.rstrip("\n"),
        }[defect]
        case.oracle.log_path.write_text(changed)
    updates = {
        "missing_journal": "DELETE FROM journal_events WHERE event_id='$old'",
        "missing_new_journal": "DELETE FROM journal_events WHERE event_id='$new'",
        "foreign_principal": "UPDATE journal_events SET principal_id='general@@other:example' WHERE event_id='$old'",
        "pending_journal": "UPDATE journal_events SET state='pending' WHERE event_id='$old'",
        "wrong_requester": "UPDATE journal_events SET sender='@other:example' WHERE event_id='$new'",
        "wrong_room": "UPDATE journal_events SET room_id='!other:example' WHERE event_id='$new'",
        "wrong_timestamp": "UPDATE journal_events SET origin_server_ts=9 WHERE event_id='$new'",
        "no_anchor": "DELETE FROM turn_records WHERE index_event_id='$new'",
        "malformed_ledger": "UPDATE turn_records SET record_json='broken' WHERE index_event_id='$old'",
        "partial_journal": "DROP TABLE journal_events",
        "missing_old_attribution": "DELETE FROM turn_records WHERE index_event_id='$old'",
    }
    if defect in updates:
        with closing(sqlite3.connect(ledger)) as database:
            database.execute(updates[defect])
            database.commit()
    if defect in {"unfinished_anchor", "conflicting_ledger", "pending_cleanup", "unproved_owned_source"}:
        records = live_fuzz.read_ledger_records(ledger, include_incomplete=True)
        record = records["$old" if defect in {"pending_cleanup", "unproved_owned_source"} else "$new"]
        raw = live_fuzz.TurnRecordCodec._to_ledger_record(record)
        if defect == "unfinished_anchor":
            raw["completed"] = False
        elif defect == "pending_cleanup":
            raw["revision_replay"] = {
                "$edit": RevisionReplay("$old", 100, redacted=True, cleanup_pending=True).to_record(),
            }
        elif defect == "unproved_owned_source":
            raw["source_event_ids"] = ["$old", "$unproved"]
        else:
            raw["source_event_ids"] = ["$new", "$old"]
        with closing(sqlite3.connect(ledger)) as database:
            database.execute(
                "UPDATE turn_records SET record_json=? WHERE index_event_id=?",
                (json.dumps(raw), record.anchor_event_id),
            )
            database.commit()
    if defect in {"unacknowledged_final", "misplaced_final"}:
        await case.journal.principal("general@@agent:example").enqueue_matrix_delivery(
            delivery_id="$old",
            stage=DeliveryStage.FINAL,
            room_id="!room:example",
            thread_id="$elsewhere" if defect == "misplaced_final" else "$root",
            payload={"body": "unfinished final"},
            edits_event_id="$old-reply",
        )
    if defect == "wrong_thread":
        case.events["$new"]["content"]["m.relates_to"]["event_id"] = "$elsewhere"
        case.events["$new-reply"]["content"]["m.relates_to"]["event_id"] = "$elsewhere"
    if defect in {"streaming_old", "canonical_streaming_old", "streaming_anchor"}:
        content = case.events["$new-reply" if defect == "streaming_anchor" else "$old-reply"]["content"]
        content[live_fuzz.STREAM_STATUS_KEY] = "streaming"
        if defect == "canonical_streaming_old":
            content["body"] = "LIVE-FUZZ call=1 END call=1"
    if defect in {"wrong_anchor_marker", "wrong_old_marker"}:
        bad_call = 2 if defect == "wrong_anchor_marker" else 1
        case.auditor.observed_markers_for = lambda call: frozenset(
            {"wrong" if call == bad_call else f"MRK[src=op:{call};rev=orig]"},
        )
    if defect == "pending_edit":
        case.auditor.pending_edit_markers = {"$old": {"$edit": "new revision"}}
    if defect == "duplicate_reply":
        case.events["$duplicate"] = {**case.events["$old-reply"], "event_id": "$duplicate"}
    if defect in {"unrelated_recovery", "duplicate_relay"}:
        case.oracle.internal_relay_senders = frozenset({"@router:example"})
        for relay in ("$relay", "$duplicate-relay") if defect == "duplicate_relay" else ("$relay",):
            case.events[relay] = {
                **case.events["$old-reply"],
                "event_id": relay,
                "sender": "@router:example",
                "content": {
                    "msgtype": "m.text",
                    "body": live_fuzz.AUTO_RESUME_MESSAGE,
                    live_fuzz.SOURCE_KIND_KEY: live_fuzz.TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$root",
                        "m.in_reply_to": {"event_id": "$old-reply"},
                    },
                },
            }
        case.events["$unrelated"] = {
            **case.events["$new-reply"],
            "event_id": "$unrelated",
            "content": {
                **case.events["$new-reply"]["content"],
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$root",
                    "m.in_reply_to": {"event_id": "$other-relay"},
                },
            },
        }
    try:
        with pytest.raises(AssertionError):
            await case.auditor.audit(room_ids=("!room:example",), sent_records=(), redacted_targets={})
    finally:
        await case.journal.close()


@pytest.mark.asyncio
async def test_supersession_keeps_completed_old_generation(tmp_path: Path) -> None:
    """A completed old response remains completed generation despite an earlier skip observation."""
    case = await _supersession_case(tmp_path, visible_old=True)
    record = TurnRecord.create(("$old",), response_event_id="$old-reply", completed=True)
    await case.journal.turn_records("general").upsert(
        index_event_ids=record.indexed_event_ids,
        anchor_event_id=record.anchor_event_id,
        record_json=json.dumps(live_fuzz.TurnRecordCodec._to_ledger_record(record)),
    )
    case.events["$old-reply"]["content"]["body"] = "LIVE-FUZZ call=1 END call=1"
    case.events["$old-reply"]["content"][live_fuzz.STREAM_STATUS_KEY] = "completed"
    try:
        metrics = await case.auditor.audit(room_ids=("!room:example",), sent_records=(), redacted_targets={})
        assert metrics["ledger_superseded_sources"] == 0
        assert metrics["completed_final_bodies"] == 2
    finally:
        await case.journal.close()


def _blocking_supersession_final(case: _SupersessionCase) -> dict[str, Any]:
    """Use actual blocking AI metadata and Matrix edit builders after a pending INITIAL."""
    original = case.events["$new-reply"]
    final_content = {
        "msgtype": "m.text",
        "body": "LIVE-FUZZ call=2 END call=2",
        **build_ai_run_metadata_content(
            config=Config(models={}),
            model_name="default",
            run_id="run-new",
            session_id="!room:example_$root",
            status=RunStatus.completed,
            model=live_fuzz.MODEL_ID,
            model_provider="openai",
        ),
    }
    final = {
        **original,
        "event_id": "$blocking-final",
        "origin_server_ts": 50,
        "content": build_matrix_edit_content("$new-reply", final_content),
    }
    original["content"] = {
        **original["content"],
        "body": "Thinking...",
        live_fuzz.STREAM_STATUS_KEY: "pending",
    }
    case.oracle.canonical_events["$new-reply"] = dict(original)
    case.events["$blocking-final"] = final
    case.oracle._ingest_event(final)
    return final


@pytest.mark.asyncio
@pytest.mark.parametrize("live_poll", [False, True])
@pytest.mark.parametrize("visible_old", [False, True])
async def test_supersession_accepts_actual_blocking_final(
    tmp_path: Path,
    live_poll: bool,
    visible_old: bool,
) -> None:
    """Completed blocking metadata can anchor exact supersession without a streaming-specific key."""
    case = await _supersession_case(tmp_path, visible_old=visible_old, old_record=visible_old)
    final = _blocking_supersession_final(case)
    assert live_fuzz.STREAM_STATUS_KEY not in final["content"]["m.new_content"]
    assert final["content"]["m.new_content"][AI_RUN_METADATA_KEY]["status"] == "completed"
    try:
        if live_poll:
            case.oracle.refresh_ledger_attributions(min_interval=0)
            assert case.oracle.unsettled_required_sources() == []
        else:
            metrics = await case.auditor.audit(room_ids=("!room:example",), sent_records=(), redacted_targets={})
            assert metrics["completed_final_bodies"] == 1
            assert metrics["ledger_superseded_sources"] == 1
        assert case.oracle.supersession_proofs["$old"].anchor_response_event_id == "$new-reply"
    finally:
        await case.journal.close()


async def _assert_terminal_supersession(case: _SupersessionCase, *, live_poll: bool, accepted: bool) -> None:
    """Exercise proof consumers against the same authoritative transport view."""
    case.oracle.canonical_events = {event_id: dict(event) for event_id, event in case.events.items()}
    case.auditor.client.paginate_room = AsyncMock(
        side_effect=lambda room: [event for event in case.events.values() if event["_audit_room_id"] == room],
    )
    if live_poll:
        case.oracle.refresh_ledger_attributions(min_interval=0)
        assert ("$old" in case.oracle.supersession_proofs) is accepted
    elif accepted:
        result = await case.auditor.audit(
            room_ids=("!room:example", "!foreign:example"),
            sent_records=(),
            redacted_targets={},
        )
        assert result["ledger_superseded_sources"] == 1
    else:
        with pytest.raises(AssertionError):
            await case.auditor.audit(
                room_ids=("!room:example", "!foreign:example"),
                sent_records=(),
                redacted_targets={},
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("live_poll", [False, True])
@pytest.mark.parametrize(
    ("signals", "accepted"),
    [
        pytest.param({}, False, id="both-absent"),
        pytest.param({live_fuzz.STREAM_STATUS_KEY: "completed"}, True, id="stream-only"),
        pytest.param({AI_RUN_METADATA_KEY: {"status": "completed"}}, True, id="blocking"),
        pytest.param(
            {live_fuzz.STREAM_STATUS_KEY: "completed", AI_RUN_METADATA_KEY: {"status": "completed"}},
            True,
            id="both-completed",
        ),
        *[
            pytest.param(
                {live_fuzz.STREAM_STATUS_KEY: status, AI_RUN_METADATA_KEY: {"status": "completed"}},
                False,
                id=f"stream-{name}",
            )
            for name, status in (
                ("null", None),
                ("integer", 1),
                ("boolean", True),
                ("list", []),
                ("mapping", {}),
                ("unknown", "unknown"),
                ("pending", "pending"),
                ("streaming", "streaming"),
                ("approval", "approval_pending"),
                ("interrupted", "interrupted"),
                ("cancelled", "cancelled"),
                ("error", "error"),
            )
        ],
        *[
            pytest.param({**stream, AI_RUN_METADATA_KEY: metadata}, False, id=f"ai-{name}-{mode}")
            for mode, stream in (("blocking", {}), ("streaming", {live_fuzz.STREAM_STATUS_KEY: "completed"}))
            for name, metadata in (
                ("null", None),
                ("string", "completed"),
                ("list", []),
                ("missing-status", {}),
                ("null-status", {"status": None}),
                ("invalid-status", {"status": 1}),
                ("pending", {"status": "pending"}),
                ("error", {"status": "error"}),
                ("cancelled", {"status": "cancelled"}),
                ("unknown", {"status": "unknown"}),
            )
        ],
    ],
)
async def test_supersession_terminal_signals_require_consistent_completion(
    tmp_path: Path,
    live_poll: bool,
    signals: dict[str, Any],
    accepted: bool,
) -> None:
    """Terminal metadata presence and value must agree within the selected completed body payload."""
    case = await _supersession_case(tmp_path, visible_old=False, old_record=False)
    final = _blocking_supersession_final(case)
    final["content"] = build_matrix_edit_content(
        "$new-reply",
        {"body": "LIVE-FUZZ call=2 END call=2", "msgtype": "m.text", **signals},
    )
    try:
        await _assert_terminal_supersession(case, live_poll=live_poll, accepted=accepted)
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("live_poll", [False, True])
@pytest.mark.parametrize(
    "location",
    ["outer_wrapper", "original", "older_edit", "foreign_room", "bundled_outer_wrapper"],
)
async def test_supersession_cannot_borrow_blocking_terminal_metadata(
    tmp_path: Path,
    live_poll: bool,
    location: str,
) -> None:
    """Completed AI metadata elsewhere cannot terminalize a selected replacement that lacks it."""
    case = await _supersession_case(tmp_path, visible_old=False, old_record=False)
    final = _blocking_supersession_final(case)
    completed = final["content"]["m.new_content"].pop(AI_RUN_METADATA_KEY)
    if location == "original":
        case.events["$new-reply"]["content"] = {
            **case.events["$new-reply"]["content"],
            live_fuzz.STREAM_STATUS_KEY: "completed",
            AI_RUN_METADATA_KEY: completed,
        }
    elif location in {"older_edit", "foreign_room"}:
        other = {
            **final,
            "event_id": "$other-final",
            "origin_server_ts": 60 if location == "foreign_room" else 40,
            "_audit_room_id": "!foreign:example" if location == "foreign_room" else "!room:example",
            "content": build_matrix_edit_content(
                "$new-reply",
                {**final["content"]["m.new_content"], AI_RUN_METADATA_KEY: completed},
            ),
        }
        case.events["$other-final"] = other
        case.oracle._ingest_event(other)
    elif location == "bundled_outer_wrapper":
        case.events.pop("$blocking-final")
        case.events["$new-reply"]["unsigned"] = {"m.relations": {"m.replace": {"event": final}}}
    try:
        await _assert_terminal_supersession(case, live_poll=live_poll, accepted=False)
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("live_poll", [False, True])
@pytest.mark.parametrize(
    "defect",
    ["unacknowledged_final", "pending_edit", "wrong_marker", "unfinished_turn", "pending_cleanup", "wrong_source"],
)
async def test_supersession_blocking_terminal_keeps_independent_ownership_checks(
    tmp_path: Path,
    live_poll: bool,
    defect: str,
) -> None:
    """Successful blocking metadata cannot discharge independent generation, source or delivery debt."""
    case = await _supersession_case(tmp_path, visible_old=False, old_record=False)
    _blocking_supersession_final(case)
    if defect == "unacknowledged_final":
        await case.journal.principal("general@@agent:example").enqueue_matrix_delivery(
            delivery_id="$new",
            stage=DeliveryStage.FINAL,
            room_id="!room:example",
            thread_id="$root",
            payload={"body": "still owed"},
            edits_event_id="$new-reply",
        )
    elif defect == "pending_edit":
        case.oracle.pending_edit_markers = {"$new": {"$edit": "MRK[src=op:2;rev=edit:1]"}}
        case.auditor.pending_edit_markers = dict(case.oracle.pending_edit_markers)
    elif defect == "wrong_marker":
        case.oracle.observed_markers_for = case.auditor.observed_markers_for = lambda _: frozenset(
            {"MRK[src=wrong;rev=orig]"},
        )
    elif defect == "wrong_source":
        case.events["$new-reply"]["content"]["m.relates_to"]["m.in_reply_to"]["event_id"] = "$old"
    else:
        record = TurnRecord.create(
            ("$new",),
            response_event_id="$new-reply",
            completed=defect != "unfinished_turn",
            revision_replay={"$edit": RevisionReplay("$new", 100, redacted=True, cleanup_pending=True).to_record()}
            if defect == "pending_cleanup"
            else None,
        )
        await case.journal.turn_records("general").forget(index_event_ids=("$new",))
        await case.journal.turn_records("general").upsert(
            index_event_ids=record.indexed_event_ids,
            anchor_event_id=record.anchor_event_id,
            record_json=json.dumps(live_fuzz.TurnRecordCodec._to_ledger_record(record)),
        )
        stored = live_fuzz.read_ledger_records(tmp_path / "event_journal.db", include_incomplete=True)["$new"]
        assert stored.completed is (defect != "unfinished_turn")
        if defect == "pending_cleanup":
            assert stored.revision_replay is not None
            assert stored.revision_replay["$edit"].cleanup_pending
    try:
        await _assert_terminal_supersession(case, live_poll=live_poll, accepted=False)
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("live_poll", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("cleanup_kind", ["revision", "source"])
@pytest.mark.parametrize("pending", [False, True])
async def test_supersession_completed_anchor_requires_settled_cleanup(
    tmp_path: Path,
    live_poll: bool,
    streaming: bool,
    cleanup_kind: str,
    pending: bool,
) -> None:
    """Both terminal forms need an anchor free of authoritative revision and source cleanup debt."""
    case = await _supersession_case(tmp_path, visible_old=False, old_record=False)
    final = _blocking_supersession_final(case)
    if streaming:
        final["content"]["m.new_content"][live_fuzz.STREAM_STATUS_KEY] = "completed"
    record = TurnRecord.create(
        ("$new",),
        response_event_id="$new-reply",
        completed=True,
        discovery_event_ids=("$removed",) if cleanup_kind == "source" else (),
        redacted_source_event_ids=("$removed",) if cleanup_kind == "source" else (),
        pending_redaction_cleanup_event_ids=("$removed",) if pending and cleanup_kind == "source" else (),
        revision_replay={"$edit": RevisionReplay("$new", 100, redacted=True, cleanup_pending=pending).to_record()}
        if cleanup_kind == "revision"
        else None,
    )
    store = case.journal.turn_records("general")
    await store.forget(index_event_ids=("$new",))
    await store.upsert(
        index_event_ids=record.indexed_event_ids,
        anchor_event_id=record.anchor_event_id,
        record_json=json.dumps(live_fuzz.TurnRecordCodec._to_ledger_record(record)),
    )
    stored = live_fuzz.read_ledger_records(tmp_path / "event_journal.db", include_incomplete=True)["$new"]
    assert stored.completed
    if cleanup_kind == "revision":
        assert stored.revision_replay is not None
        assert stored.revision_replay["$edit"].cleanup_pending is pending
    else:
        assert stored.pending_redaction_cleanup_event_ids == (("$removed",) if pending else ())
    try:
        await _assert_terminal_supersession(case, live_poll=live_poll, accepted=not pending)
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("live_poll", [False, True])
async def test_supersession_accepts_bundled_blocking_final(tmp_path: Path, live_poll: bool) -> None:
    """Server-bundled FINAL replacement carries the same successful blocking contract."""
    case = await _supersession_case(tmp_path, visible_old=False, old_record=False)
    final = _blocking_supersession_final(case)
    case.events.pop("$blocking-final")
    case.events["$new-reply"]["unsigned"] = {"m.relations": {"m.replace": {"event": final}}}
    try:
        await _assert_terminal_supersession(case, live_poll=live_poll, accepted=True)
    finally:
        await case.journal.close()


@pytest.mark.asyncio
async def test_supersession_of_interrupted_placeholder_does_not_claim_generation(tmp_path: Path) -> None:
    """Cleanup before any visible model output needs no invented old model call."""
    case = await _supersession_case(tmp_path, visible_old=True)
    case.events["$old-reply"]["content"]["body"] = "Thinking...\n\n" + live_fuzz.RESTART_INTERRUPTED_RESPONSE_NOTE
    try:
        metrics = await case.auditor.audit(room_ids=("!room:example",), sent_records=(), redacted_targets={})
        assert metrics["ledger_superseded_sources"] == 1
        assert metrics["completed_final_bodies"] == 1
    finally:
        await case.journal.close()


@pytest.mark.asyncio
async def test_supersession_live_wait_keeps_unproved_old_response_pending(tmp_path: Path) -> None:
    """A visible restart note cannot substitute for exact durable semantic settlement."""
    case = await _supersession_case(tmp_path, visible_old=True)
    assert case.oracle.log_path is not None
    case.oracle.log_path.write_text("")
    try:
        case.oracle.refresh_ledger_attributions(min_interval=0)
        assert case.oracle.unsettled_required_sources() == ["$old"]
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_room", [False, True])
async def test_supersession_terminal_edit_must_belong_to_response_room(tmp_path: Path, foreign_room: bool) -> None:
    """A restart edit can terminalize only the response in its own Matrix room."""
    case = await _supersession_case(tmp_path, visible_old=True)
    original = case.events["$old-reply"]
    case.events["$terminal-edit"] = {
        **original,
        "event_id": "$terminal-edit",
        "origin_server_ts": 50,
        "_audit_room_id": "!foreign:example" if foreign_room else "!room:example",
        "content": {
            "msgtype": "m.text",
            "body": "* " + original["content"]["body"],
            "m.new_content": dict(original["content"]),
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$old-reply"},
        },
    }
    original["content"][live_fuzz.STREAM_STATUS_KEY] = "streaming"
    original["content"]["body"] = "LIVE-FUZZ call=1 END call=1"
    case.auditor.source_current_markers = dict(case.oracle.source_current_markers)
    try:
        case.auditor._observe_supersession(case.events)
        assert ("$old" in case.auditor.supersession_proofs) is not foreign_room
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["old", "new"])
@pytest.mark.parametrize("foreign_room", [False, True])
async def test_supersession_full_audit_uses_one_room_for_body_and_status(
    tmp_path: Path,
    target: str,
    foreign_room: bool,
) -> None:
    """Terminal status cannot borrow its required interruption or completed body from another room."""
    case = await _supersession_case(tmp_path, visible_old=True)
    response_id = f"${target}-reply"
    original = case.events[response_id]
    case.events["$terminal-edit"] = {
        **original,
        "event_id": "$terminal-edit",
        "origin_server_ts": 50,
        "_audit_room_id": "!foreign:example" if foreign_room else "!room:example",
        "content": {
            "msgtype": "m.text",
            "body": "* " + original["content"]["body"],
            "m.new_content": dict(original["content"]),
            "m.relates_to": {"rel_type": "m.replace", "event_id": response_id},
        },
    }
    # Keep terminal metadata in the real room, but remove its qualifying body.
    original["content"]["body"] = "LIVE-FUZZ call=1 END call=1" if target == "old" else "LIVE-FUZZ call=2 partial"
    case.oracle._ingest_event(case.events["$terminal-edit"])
    case.auditor.client.paginate_room = AsyncMock(
        side_effect=lambda room: [event for event in case.events.values() if event["_audit_room_id"] == room],
    )
    try:
        if foreign_room:
            with pytest.raises(AssertionError):
                await case.auditor.audit(
                    room_ids=("!room:example", "!foreign:example"),
                    sent_records=(),
                    redacted_targets={},
                )
        else:
            metrics = await case.auditor.audit(room_ids=("!room:example",), sent_records=(), redacted_targets={})
            assert metrics["ledger_superseded_sources"] == 1
            assert metrics["completed_final_bodies"] == 1
    finally:
        await case.journal.close()


async def _complete_supersession_source(case: _SupersessionCase, source: str, call: int) -> None:
    """Complete a fixture source through actual journal/turn owners and simulated Matrix delivery."""
    response_id = f"{source}-reply"
    record = TurnRecord.create((source,), response_event_id=response_id, completed=True)
    await case.journal.turn_records("general").upsert(
        index_event_ids=record.indexed_event_ids,
        anchor_event_id=record.anchor_event_id,
        record_json=json.dumps(live_fuzz.TurnRecordCodec._to_ledger_record(record)),
    )
    await case.journal.principal("general@@agent:example").settle_many((source,))
    case.events[response_id] = {
        **case.events[source],
        "event_id": response_id,
        "sender": "@agent:example",
        "origin_server_ts": 30 + call,
        "content": {
            "msgtype": "m.text",
            "body": f"LIVE-FUZZ call={call} END call={call}",
            live_fuzz.STREAM_STATUS_KEY: "completed",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$root", "m.in_reply_to": {"event_id": source}},
        },
    }


async def _add_redacted_completed_source(
    case: _SupersessionCase,
    ancestry: str,
) -> tuple[list[live_fuzz._SentRecord], dict[str, str]]:
    """Retain authored ancestry beside actual settled/tombstoned journal ownership and empty Matrix shells."""
    root = {**case.events["$new"], "event_id": "$ancestor", "content": {"body": "ancestor", "msgtype": "m.text"}}
    bridge = {
        **root,
        "event_id": "$bridge",
        "content": {
            "body": "bridge",
            "msgtype": "m.text",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$ancestor"},
        },
    }
    relation = (
        {"rel_type": "m.thread", "event_id": "$ancestor", "m.in_reply_to": {"event_id": "$ancestor"}}
        if ancestry == "thread"
        else {"m.in_reply_to": {"event_id": "$bridge" if ancestry == "multi_hop" else "$ancestor"}}
    )
    source = {
        **case.events["$new"],
        "event_id": "$redacted",
        "origin_server_ts": 25,
        "content": {"body": "MRK[src=op:3;rev=orig]", "msgtype": "m.text", "m.relates_to": relation},
    }
    authored = [root, bridge, source]
    sent_records = [
        live_fuzz._SentRecord(
            event["event_id"],
            "!room:example",
            "m.room.message",
            sender=event["sender"],
            content=event["content"],
        )
        for event in authored
    ]
    case.events.update((event["event_id"], event) for event in authored)
    await case.journal.principal("general@@agent:example").admit(
        InboundEvent(
            "$redacted",
            "!room:example",
            "$ancestor" if ancestry == "thread" else None,
            EventKind.MESSAGE,
            EventClass.ACTIONABLE,
            "@user:example",
            25,
            source,
        ),
    )
    case.oracle.expect("op:3", "$redacted", thread=1)
    await _complete_supersession_source(case, "$redacted", 3)
    case.events["$redacted-reply"]["content"]["m.relates_to"]["event_id"] = "$ancestor"
    tombstone = TurnRecord.create(
        ("$redacted",),
        redacted_source_event_ids=("$redacted",),
        response_event_id="$redacted-reply",
        completed=True,
    )
    await case.journal.turn_records("general").upsert(
        index_event_ids=tombstone.indexed_event_ids,
        anchor_event_id=tombstone.anchor_event_id,
        record_json=json.dumps(live_fuzz.TurnRecordCodec._to_ledger_record(tombstone)),
    )
    redacted = {"$redacted": "$redaction"}
    if ancestry == "multi_hop":
        redacted["$bridge"] = "$bridge-redaction"
    for event_id, redaction_id in redacted.items():
        case.events[event_id] = {
            **case.events[event_id],
            "content": {},
            "unsigned": {"redacted_because": {"event_id": redaction_id}},
        }
    for event in case.events.values():
        case.oracle._ingest_event(event)
    return sent_records, redacted


@pytest.mark.asyncio
@pytest.mark.parametrize("ancestry", ["thread", "plain", "multi_hop"])
@pytest.mark.parametrize("live_poll", [False, True])
async def test_supersession_preserves_redacted_authored_ancestry(
    tmp_path: Path,
    ancestry: str,
    live_poll: bool,
) -> None:
    """An unrelated redacted source must not invalidate completed replies or independently proven supersession."""
    case = await _supersession_case(tmp_path, visible_old=True)
    sent_records, redacted = await _add_redacted_completed_source(case, ancestry)
    try:
        if live_poll:
            case.oracle.sent_records = sent_records
            case.oracle.refresh_ledger_attributions(min_interval=0)
            assert case.oracle.ledger_response("$new") == "$new-reply"
            assert case.oracle.ledger_response("$redacted") == "$redacted-reply"
            assert "$old" in case.oracle.supersession_proofs
        else:
            metrics = await case.auditor.audit(
                room_ids=("!room:example",),
                sent_records=sent_records,
                redacted_targets=redacted,
            )
            assert metrics["completed_final_bodies"] == 2
            assert metrics["ledger_superseded_sources"] == 1
        assert all(case.events[event_id]["content"] == {} for event_id in redacted)
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("live_poll", [False, True])
async def test_supersession_source_pair_preserves_redacted_predecessor(tmp_path: Path, live_poll: bool) -> None:
    """Both admitted plain-reply sources resolve through retained ancestry of their redacted predecessor."""
    case = await _supersession_case(tmp_path, visible_old=True, plain_reply=True, plain_reply_parent="$predecessor")
    parent = case.events["$predecessor"]
    sent_records = [
        live_fuzz._SentRecord(
            "$predecessor",
            "!room:example",
            "m.room.message",
            sender=parent["sender"],
            content=parent["content"],
        ),
    ]
    parent["content"] = {}
    parent["unsigned"] = {"redacted_because": {"event_id": "$redaction"}}
    case.oracle.canonical_events["$predecessor"] = dict(parent)
    try:
        if live_poll:
            case.oracle.sent_records = sent_records
            case.oracle.refresh_ledger_attributions(min_interval=0)
        else:
            await case.auditor.audit(
                room_ids=("!room:example",),
                sent_records=sent_records,
                redacted_targets={"$predecessor": "$redaction"},
            )
        assert case.oracle.supersession_proofs["$old"].thread_id == "$root"
        assert case.oracle.supersession_proofs["$old"].anchor_source_event_id == "$new"
        assert parent["content"] == {}
    finally:
        await case.journal.close()


@pytest.mark.asyncio
async def test_supersession_live_runner_retains_records_added_after_construction(tmp_path: Path) -> None:
    """Runner's evolving authored-record collection must reach live ledger refresh after source redaction."""
    case = await _supersession_case(tmp_path, visible_old=True)
    stack = Mock(spec=live_fuzz.ManagedTuwunelStack)
    stack.agent_id = "@agent:example"
    stack.router_id = "@router:example"
    stack.storage_path = tmp_path
    stack.log_path = case.oracle.log_path
    stack.runtime_redaction_path = tmp_path / "runtime-redactions.jsonl"
    runner = live_fuzz.LiveFuzzRunner(
        stack,
        (case.oracle.client,),
        live_fuzz.LiveFuzzScenario(1, (), profile="chaos"),
        reply_timeout=1,
        settle_seconds=0,
    )
    sent_records, redacted = await _add_redacted_completed_source(case, "multi_hop")
    runner.sent_records.extend(sent_records)
    runner.oracle.ledger_path = case.oracle.ledger_path
    runner.oracle.expected_body_for = case.oracle.expected_body_for
    runner.oracle.observed_markers_for = case.oracle.observed_markers_for
    runner.source_current_markers.update(case.oracle.source_current_markers)
    for source, logical in case.oracle.expected_sources.items():
        runner.oracle.expect(logical, source, thread=case.oracle.source_threads[source])
    for event in case.events.values():
        runner.oracle._ingest_event(event)
    try:
        runner.oracle.refresh_ledger_attributions(min_interval=0)
        assert runner.oracle.ledger_response("$new") == "$new-reply"
        assert runner.oracle.ledger_response("$redacted") == "$redacted-reply"
        assert "$old" in runner.oracle.supersession_proofs
        assert all(runner.oracle.canonical_events[source]["content"] == {} for source in redacted)
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("live_poll", [False, True])
@pytest.mark.parametrize(
    "defect",
    ["reply_room", "reply_root", "missing_ancestry", "contradictory_ancestry", "missing_journal"],
)
async def test_supersession_retained_ancestry_keeps_provenance_checks(
    tmp_path: Path,
    live_poll: bool,
    defect: str,
) -> None:
    """Retained authored records cannot excuse foreign replies or absent/contradictory proof identity."""
    case = await _supersession_case(tmp_path, visible_old=True)
    sent_records, redacted = await _add_redacted_completed_source(case, "multi_hop")
    if defect == "reply_room":
        case.events["$redacted-reply"]["_audit_room_id"] = "!foreign:example"
    elif defect == "reply_root":
        case.events["$redacted-reply"]["content"]["m.relates_to"]["event_id"] = "$wrong"
    elif defect == "missing_ancestry":
        sent_records = [record for record in sent_records if record.event_id != "$bridge"]
    elif defect == "contradictory_ancestry":
        sent_records = [
            live_fuzz._SentRecord(
                record.event_id,
                record.room_id,
                record.event_type,
                sender=record.sender,
                content={"body": "wrong", "m.relates_to": {"rel_type": "m.thread", "event_id": "$wrong"}},
            )
            if record.event_id == "$bridge"
            else record
            for record in sent_records
        ]
    else:
        await case.journal.backend.write(
            lambda connection: connection.execute("DELETE FROM journal_events WHERE event_id = ?", ("$old",)),
        )
    case.oracle.canonical_events = {event_id: dict(event) for event_id, event in case.events.items()}
    case.auditor.client.paginate_room = AsyncMock(
        side_effect=lambda room: [event for event in case.events.values() if event["_audit_room_id"] == room],
    )
    try:
        if live_poll:
            case.oracle.sent_records = sent_records
            case.oracle.refresh_ledger_attributions(min_interval=0)
            assert "$old" not in case.oracle.supersession_proofs
        else:
            with pytest.raises(AssertionError):
                await case.auditor.audit(
                    room_ids=("!room:example", "!foreign:example"),
                    sent_records=sent_records,
                    redacted_targets=redacted,
                )
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_history", [False, True])
@pytest.mark.parametrize("retry_history", [False, True])
@pytest.mark.parametrize("changed_successor", [False, True])
async def test_supersession_retry_after_interrupted_settlement_validates_each_guard_candidate(
    tmp_path: Path,
    first_history: bool,
    retry_history: bool,
    changed_successor: bool,
) -> None:
    """A positive guard may be logged again with different successor or mode after its settlement is interrupted."""
    case = await _supersession_case(tmp_path, visible_old=True, interrupt_settlement=True, history_guard=first_history)
    principal = case.journal.principal("general@@agent:example")
    assert "$old" in await principal.unsettled_event_ids()
    successor = "$new"
    if changed_successor:
        await _complete_supersession_source(case, "$new", 2)
        successor = "$retry"
        case.events[successor] = {
            **case.events["$new"],
            "event_id": successor,
            "origin_server_ts": 25,
            "content": {**case.events["$new"]["content"], "body": "MRK[src=op:3;rev=orig]"},
        }
        await principal.admit(
            InboundEvent(
                successor,
                "!room:example",
                "$root",
                EventKind.MESSAGE,
                EventClass.ACTIONABLE,
                "@user:example",
                25,
                case.events[successor],
            ),
        )
        case.oracle.expect("op:3", successor)
    records = live_fuzz.read_ledger_records(tmp_path / "event_journal.db")
    output = StringIO()
    await _observe_guard_decision(
        principal,
        case.events["$old"],
        [event for source, event in case.events.items() if source in {"$new", "$retry"}],
        output=output,
        use_history=retry_history,
        handled=frozenset(source for source, record in records.items() if record.completed),
    )
    assert case.oracle.log_path is not None
    case.oracle.log_path.write_text(case.oracle.log_path.read_text() + output.getvalue())
    await JournalDispatcher(
        principal,
        Mock(spec=JournalCallbacks),
        lambda room: nio.MatrixRoom(room, "@agent:example"),
    ).settle_intentionally_ignored_turn_sources(("$old",))
    await _complete_supersession_source(case, successor, 3 if changed_successor else 2)
    assert "$old" not in await principal.unsettled_event_ids()
    for event in case.events.values():
        case.oracle._ingest_event(event)
    try:
        metrics = await case.auditor.audit(room_ids=("!room:example",), sent_records=(), redacted_targets={})
        assert metrics["ledger_superseded_sources"] == 1
        assert case.oracle.supersession_proofs["$old"].newer_event_id == successor
        assert not live_fuzz.read_ledger_records(tmp_path / "event_journal.db", include_incomplete=True)[
            "$old"
        ].completed
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_candidate", [False, True])
@pytest.mark.parametrize("invalid", ["unknown_successor", "wrong_thread", "malformed"])
async def test_supersession_retry_candidates_do_not_promote_invalid_observations(
    tmp_path: Path,
    valid_candidate: bool,
    invalid: str,
) -> None:
    """An invalid later observation cannot qualify alone or poison an independently valid earlier one."""
    case = await _supersession_case(tmp_path, visible_old=True)
    assert case.oracle.log_path is not None
    positive = case.oracle.log_path.read_text()
    invalid_record = {
        "unknown_successor": positive.replace("$new", "$foreign"),
        "wrong_thread": positive.replace("$root", "$foreign"),
        "malformed": positive.replace("skipped_event_id", "skipped_event"),
    }[invalid]
    case.oracle.log_path.write_text((positive if valid_candidate else "") + invalid_record)
    try:
        if valid_candidate:
            metrics = await case.auditor.audit(room_ids=("!room:example",), sent_records=(), redacted_targets={})
            assert metrics["ledger_superseded_sources"] == 1
            assert case.oracle.supersession_proofs["$old"].newer_event_id == "$new"
        else:
            with pytest.raises(AssertionError):
                await case.auditor.audit(room_ids=("!room:example",), sent_records=(), redacted_targets={})
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("broken_link", [False, True])
async def test_supersession_forward_chain_requires_every_exact_link(tmp_path: Path, broken_link: bool) -> None:
    """An incomplete named successor needs its own positive guard and terminal anchor."""
    case = await _supersession_case(tmp_path, visible_old=True)
    principal = case.journal.principal("general@@agent:example")
    case.events["$last"] = {
        **case.events["$new"],
        "event_id": "$last",
        "origin_server_ts": 25,
        "content": {**case.events["$new"]["content"], "body": "MRK[src=op:3;rev=orig]"},
    }
    await principal.admit(
        InboundEvent(
            "$last",
            "!room:example",
            "$root",
            EventKind.MESSAGE,
            EventClass.ACTIONABLE,
            "@user:example",
            25,
            case.events["$last"],
        ),
    )
    middle = TurnRecord.create(("$new",), response_event_id="$new-reply", completed=False)
    last = TurnRecord.create(("$last",), response_event_id="$last-reply", completed=True)
    # Replace the fixture's completed successor with the crash-time unfinished owner.
    # Production correctly refuses to downgrade completion through upsert.
    with closing(sqlite3.connect(tmp_path / "event_journal.db")) as database:
        database.execute("DELETE FROM turn_records WHERE index_event_id='$new'")
        database.commit()
    for record in (middle, last):
        await case.journal.turn_records("general").upsert(
            index_event_ids=record.indexed_event_ids,
            anchor_event_id=record.anchor_event_id,
            record_json=json.dumps(live_fuzz.TurnRecordCodec._to_ledger_record(record)),
        )
    output = StringIO()
    logger = structlog.wrap_logger(
        structlog.PrintLogger(file=output),
        processors=[structlog.dev.ConsoleRenderer(colors=True)],
    ).bind(agent="general", logger="mindroom.bot")
    assert has_newer_unresponded_in_thread(
        PreparedIngress("@user:example", "$new", "new", case.events["$new"], server_timestamp=20),
        "@user:example",
        [
            ResolvedVisibleMessage(
                "@user:example",
                "last",
                25,
                "$last",
                case.events["$last"]["content"],
                "$root",
                "$last",
            ),
        ],
        may_be_superseded_by_newer_requester_turn=True,
        requester_user_id_for_event=lambda sender, _: sender,
        is_visible_router_voice_echo=lambda *_: False,
        sender_is_trusted_for_ingress_metadata=lambda _: False,
        is_handled=lambda _: False,
        logger=cast("structlog.stdlib.BoundLogger", logger),
    )
    await JournalDispatcher(
        principal,
        Mock(spec=JournalCallbacks),
        lambda room: nio.MatrixRoom(room, "@agent:example"),
    ).settle_intentionally_ignored_turn_sources(("$new",))
    await principal.settle_many(("$last",))
    assert case.oracle.log_path is not None
    if not broken_link:
        case.oracle.log_path.write_text(case.oracle.log_path.read_text() + output.getvalue())
    case.events["$last-reply"] = {
        **case.events["$new-reply"],
        "event_id": "$last-reply",
        "origin_server_ts": 40,
        "content": {
            **case.events["$new-reply"]["content"],
            "body": "LIVE-FUZZ call=3 END call=3",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$root", "m.in_reply_to": {"event_id": "$last"}},
        },
    }
    case.events["$new-reply"]["content"]["body"] += "\n\n" + live_fuzz.RESTART_INTERRUPTED_RESPONSE_NOTE
    case.events["$new-reply"]["content"][live_fuzz.STREAM_STATUS_KEY] = "error"
    case.oracle.expect("op:3", "$last")
    for event in case.events.values():
        case.oracle._ingest_event(event)
    try:
        if broken_link:
            with pytest.raises(AssertionError):
                await case.auditor.audit(room_ids=("!room:example",), sent_records=(), redacted_targets={})
        else:
            metrics = await case.auditor.audit(room_ids=("!room:example",), sent_records=(), redacted_targets={})
            assert metrics["ledger_superseded_sources"] == 2
            assert metrics["completed_final_bodies"] == 1
            assert case.oracle.supersession_proofs["$old"].anchor_source_event_id == "$last"
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["return", "error", "cancel", "append_failure", "missing_file"])
async def test_runtime_redaction_observer_records_before_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """Real installed wrapper records synchronously and preserves the original boundary."""
    path = tmp_path / "entries.jsonl"
    path.touch()
    store = _redaction_observer_store()
    monkeypatch.setattr(TurnStore, "is_revision_redacted", lambda _self, _target: False)
    calls: list[str] = []
    error = RuntimeError("original mutation failed")
    cancelled = asyncio.CancelledError("original cancelled")
    result = TurnRecord.create(("$edit",), completed=False)

    async def original(self: TurnStore, target: str) -> TurnRecord:
        assert self is store
        assert json.loads(path.read_text())["target_event_id"] == target
        calls.append(target)
        if outcome == "error":
            raise error
        if outcome == "cancel":
            raise cancelled
        return result

    monkeypatch.setattr(TurnStore, "mark_source_redacted", original)
    monkeypatch.setattr(live_fuzz.time, "monotonic_ns", lambda: 123)
    live_fuzz._install_runtime_redaction_observer(path)
    if outcome in {"append_failure", "missing_file"}:
        path.unlink()
        if outcome == "append_failure":
            path.mkdir()
        with pytest.raises(SystemExit, match="redaction observation"):
            store.mark_source_redacted("$edit").close()
        assert not calls
        return
    operation = store.mark_source_redacted("$edit")
    assert not calls
    assert json.loads(path.read_text()) == {
        "agent_name": "general",
        "principal_id": "general@@general:example",
        "target_event_id": "$edit",
        "monotonic_ns": 123,
        "already_redacted": False,
    }
    if outcome in {"error", "cancel"}:
        with pytest.raises(type(error if outcome == "error" else cancelled)) as caught:
            await operation
        assert caught.value is (error if outcome == "error" else cancelled)
    else:
        assert await operation is result
    assert calls == ["$edit"]


def test_real_runtime_child_installs_observer_across_generations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Child attests installation only after installing; restart retains prior entries and model calls."""
    path = tmp_path / "entries.jsonl"
    path.touch()
    attestation = tmp_path / "attestation.json"
    store = _redaction_observer_store()
    generation = 0

    async def original(self: TurnStore, target: str) -> None:
        assert self is store
        assert target == "$edit"

    def app() -> None:
        assert TurnStore.mark_source_redacted is not original
        assert json.loads(attestation.read_text())["runtime_redaction_observer"]["path"] == str(path)
        asyncio.run(store.mark_source_redacted("$edit"))

    monkeypatch.setattr(cli_main, "app", app)
    monkeypatch.setattr(live_fuzz.sys, "argv", ["harness"])
    monkeypatch.setattr(TurnStore, "is_revision_redacted", lambda _self, _target: generation == 2)
    live_fuzz._ModelHandler.reset_observations()
    monkeypatch.setattr(live_fuzz.time, "monotonic_ns", lambda: 100)
    live_fuzz._ModelHandler._record_observation(90, frozenset({"marker"}))
    for generation in (1, 2):
        monkeypatch.setattr(TurnStore, "mark_source_redacted", original)
        monkeypatch.setattr(live_fuzz.time, "monotonic_ns", lambda generation=generation: generation * 200)
        live_fuzz._run_mindroom_runtime_child(attestation, path, ["run"])
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    assert [entry["monotonic_ns"] for entry in entries] == [200, 400]
    assert [entry["already_redacted"] for entry in entries] == [False, True]
    assert live_fuzz._runtime_redaction_cutoff(path, "general@@general:example", "$edit") == 200
    assert live_fuzz._ModelHandler.timed_observations_snapshot()[90].monotonic_ns == 100
    live_fuzz._ModelHandler.reset_observations()


@pytest.mark.asyncio
async def test_runtime_redaction_observer_rejects_recovered_revision_without_physical_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal_store: EventJournalStore,
) -> None:
    """A durably invalidated owner revision cannot gain a later cutoff from its first callback."""
    store = TurnStore(
        TurnStoreDeps(
            agent_name="general",
            turn_records=journal_store.turn_records("general"),
            redacted_event_ids=journal_store.principal("agent@alice").redacted_event_ids,
            legacy_responses_file=None,
            state_writer=Mock(spec=ConversationStateWriter),
            resolver=_redaction_observer_store().deps.resolver,
            tool_runtime=Mock(spec=ToolRuntimeSupport),
        ),
    )
    await store.warm()
    await store.record_turn(
        TurnRecord.create(
            ("$source",),
            completed=True,
            revision_replay={"$edit": RevisionReplay("$source", 100, redacted=True)},
        ),
    )
    assert not store.is_revision_redacted("$edit")
    path = tmp_path / "entries.jsonl"
    path.touch()
    monkeypatch.setattr(TurnStore, "mark_source_redacted", TurnStore.mark_source_redacted)
    live_fuzz._install_runtime_redaction_observer(path)
    await store.mark_source_redacted("$edit")
    assert json.loads(path.read_text())["already_redacted"] is True
    assert live_fuzz._runtime_redaction_cutoff(path, "general@@general:example", "$edit") is None


@pytest.mark.parametrize("invalid", ["truncated", "malformed", "backwards", "recovered", "missing"])
def test_runtime_redaction_evidence_never_moves_cutoff(
    tmp_path: Path,
    invalid: str,
) -> None:
    """Damaged evidence or a recovered first tombstone cannot date invalidation later."""
    path = tmp_path / "entries.jsonl"
    entry = {
        "agent_name": "general",
        "principal_id": "general@@general:example",
        "target_event_id": "$edit",
        "monotonic_ns": 200,
        "already_redacted": invalid == "recovered",
    }
    content = json.dumps(entry) + "\n"
    if invalid == "truncated":
        content += '{"agent_name":'
    if invalid == "malformed":
        content += json.dumps({**entry, "monotonic_ns": "300"}) + "\n"
    if invalid == "backwards":
        content += json.dumps({**entry, "monotonic_ns": 100}) + "\n"
    if invalid != "missing":
        path.write_text(content)
    if invalid in {"recovered", "missing"}:
        assert live_fuzz._runtime_redaction_cutoff(path, "general@@general:example", "$edit") is None
    else:
        with pytest.raises(AssertionError, match="runtime redaction evidence"):
            live_fuzz._runtime_redaction_cutoff(path, "general@@general:example", "$edit")


def test_model_observation_timing_is_atomic_and_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Call identifiers are opaque; each exact marker snapshot retains its own host clock."""
    handler = live_fuzz._ModelHandler
    handler.reset_observations()
    monkeypatch.setattr(live_fuzz.time, "monotonic_ns", lambda: 100)
    handler._record_observation(90, frozenset({"first"}))
    monkeypatch.setattr(live_fuzz.time, "monotonic_ns", lambda: 300)
    handler._record_observation(2, frozenset({"second"}))
    observations = handler.timed_observations_snapshot()
    assert observations[90].monotonic_ns == 100
    assert observations[90].markers == frozenset({"first"})
    assert observations[2].monotonic_ns == 300
    handler.reset_observations()
    assert handler.timed_observations_snapshot() == {}


def test_runtime_membership_uses_exact_time_not_call_number(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh retry IDs and missing timestamps cannot inherit earlier identical marker membership."""
    path = tmp_path / "entries.jsonl"
    path.write_text(
        json.dumps(
            {
                "agent_name": "general",
                "principal_id": "general@@general:example",
                "target_event_id": "$edit",
                "monotonic_ns": 200,
                "already_redacted": False,
            },
        )
        + "\n",
    )
    evidence = live_fuzz.RedactedEditEvidence("$source", "$edit", "marker", frozenset({4}), frozenset({"$edit"}))
    handler = live_fuzz._ModelHandler
    handler.reset_observations()
    monkeypatch.setattr(live_fuzz.time, "monotonic_ns", lambda: 100)
    handler._record_observation(90, frozenset({"marker"}))
    monkeypatch.setattr(live_fuzz.time, "monotonic_ns", lambda: 300)
    handler._record_observation(2, frozenset({"marker"}))
    for call, expected in ((90, True), (2, False), (4, True), (1, False)):
        assert (
            live_fuzz._historical_call_observed(
                evidence,
                call,
                agent_id="@general:example",
                runtime_redaction_path=path,
            )
            is expected
        )
    handler.reset_observations()


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


def test_generated_cleanup_probes_include_exact_redacted_edit() -> None:
    """Deleting one edit needs a later serialized probe naming that physical revision."""
    scenario = live_fuzz.LiveFuzzScenario(
        1,
        (
            (live_fuzz.LiveOperation(10, live_fuzz.LiveOperationKind.EDIT, 0, "root:0"),),
            (live_fuzz.LiveOperation(11, live_fuzz.LiveOperationKind.REDACTION, 0, "op:10"),),
        ),
    )
    saved = scenario.to_json()
    qualified = live_fuzz._with_redaction_cleanup_probes(scenario)
    assert qualified.batches[-1][0].cleanup_sources == ("op:10",)
    assert qualified.batches[:2] == scenario.batches
    assert live_fuzz.LiveFuzzScenario.from_json(qualified.to_json()) == qualified
    assert live_fuzz.LiveFuzzScenario.from_json(saved).to_json() == saved


def test_saved_trace_loading_never_adds_cleanup_operations(tmp_path: Path) -> None:
    """Both saved-trace entry points preserve exact operations and leave source bytes untouched."""
    operations = (
        (live_fuzz.LiveOperation(10, live_fuzz.LiveOperationKind.EDIT, 0, "root:0"),),
        (live_fuzz.LiveOperation(11, live_fuzz.LiveOperationKind.REDACTION, 0, "op:10"),),
    )
    original = live_fuzz.LiveFuzzScenario(1, operations)
    saved = json.dumps(json.loads(original.to_json()), indent=4).encode()
    trace = tmp_path / "saved.json"
    trace.write_bytes(saved)
    assert live_fuzz.LiveFuzzScenario.from_json(saved.decode()).batches == operations
    assert live_fuzz._scenario_from_args(argparse.Namespace(trace=trace)).batches == operations
    assert trace.read_bytes() == saved


def test_cleanup_probe_rejects_contaminated_attempt_before_clean_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed first request leaking removed history cannot hide behind a clean retry."""
    old = live_fuzz._source_marker("root:0", live_fuzz.ORIGINAL_REVISION)
    later = live_fuzz._source_marker("op:1", live_fuzz.ORIGINAL_REVISION)
    oracle = Mock(spec=live_fuzz.ExactReplyOracle)
    oracle.expected_sources = {"$old": "root:0", "$probe": "op:1"}
    monkeypatch.setattr(live_fuzz._ModelHandler, "observations_snapshot", lambda: {9: [later], 2: [later]})
    auditor = live_fuzz.FinalStateAuditor(
        Mock(spec=live_fuzz.LiveMatrixClient),
        oracle,
        agent_id="@agent:test",
        expected_body_for=lambda _: "unused",
        cleanup_probes={"$probe": ("$old",)},
        full_request_markers_for=lambda call: frozenset({later, old} if call == 9 else {later}),
    )
    records = {
        "$old": live_fuzz.TurnRecord.create(source_event_ids=("$old",), redacted_source_event_ids=("$old",)),
        "$probe": live_fuzz.TurnRecord.create(source_event_ids=("$probe",), response_event_id="$reply"),
    }
    events = {
        "$reply": {
            "event_id": "$reply",
            "sender": "@agent:test",
            "type": "m.room.message",
            "content": {"body": "LIVE-FUZZ call=2 END call=2"},
        },
    }
    with pytest.raises(AssertionError, match="redaction cleanup probe"):
        auditor._assert_redaction_cleanup_probes(events, records)


@pytest.mark.parametrize("evidence", ["current", "visible", "none"])
@pytest.mark.parametrize("pending", [False, True])
def test_ordinary_edit_cleanup_requires_acknowledgement_for_any_call(
    monkeypatch: pytest.MonkeyPatch,
    evidence: str,
    pending: bool,
) -> None:
    """Actual edit-cleanup requests require monotonic acknowledgement even without a terminal reply."""
    removed = live_fuzz._source_marker("root:0", "edit:0")
    later = live_fuzz._source_marker("op:1", live_fuzz.ORIGINAL_REVISION)
    oracle = Mock(spec=live_fuzz.ExactReplyOracle)
    oracle.expected_sources = {"$root": "root:0", "$probe": "op:1"}
    monkeypatch.setattr(
        live_fuzz._ModelHandler,
        "observations_snapshot",
        lambda: {7: [later]} if evidence == "current" else {},
    )
    auditor = live_fuzz.FinalStateAuditor(
        Mock(spec=live_fuzz.LiveMatrixClient),
        oracle,
        agent_id="@agent:test",
        expected_body_for=lambda _: "unused",
        observed_cleanup_probes={"$probe": ("$edit",)},
        source_revision_markers={"$root": {"$edit": removed}},
        full_request_markers_for=lambda _: frozenset({later}),
    )
    records = {
        "$root": live_fuzz.TurnRecord.create(
            source_event_ids=("$root",),
            revision_replay={"$edit": RevisionReplay("$root", 100, redacted=True, cleanup_pending=pending)},
        ),
        "$probe": live_fuzz.TurnRecord.create(
            source_event_ids=("$probe",),
            completed=False,
            redacted_source_event_ids=("$probe",),
            response_event_id="$reply" if evidence == "visible" else None,
        ),
    }
    events = {
        "$reply": {
            "event_id": "$reply",
            "sender": "@agent:test",
            "type": "m.room.message",
            "content": {"body": "LIVE-FUZZ call=7 END call=7"},
        },
    }
    if pending and evidence != "none":
        with pytest.raises(AssertionError, match="pending or missing tombstone cleanup"):
            auditor._assert_redaction_cleanup_probes(events, records)
    else:
        result = auditor._assert_redaction_cleanup_probes(events, records)
        assert result["redaction_cleanup_uncovered_sources"] == int(evidence == "none")
        assert result["redaction_cleanup_checked_calls"] == int(evidence != "none")


@pytest.mark.parametrize("dedicated", [False, True])
@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("contaminated", [False, True])
def test_original_source_cleanup_distinguishes_ordinary_calls_from_dedicated_probes(
    monkeypatch: pytest.MonkeyPatch,
    dedicated: bool,
    pending: bool,
    contaminated: bool,
) -> None:
    """Repeated source callbacks may re-arm final debt; actual inputs and dedicated probes stay strict."""
    removed = live_fuzz._source_marker("root:0", live_fuzz.ORIGINAL_REVISION)
    later = live_fuzz._source_marker("op:1", live_fuzz.ORIGINAL_REVISION)
    oracle = Mock(spec=live_fuzz.ExactReplyOracle)
    oracle.expected_sources = {"$root": "root:0", "$probe": "op:1"}
    monkeypatch.setattr(live_fuzz._ModelHandler, "observations_snapshot", lambda: {7: [later]})
    targets = {"$probe": ("$root",)}
    auditor = live_fuzz.FinalStateAuditor(
        Mock(spec=live_fuzz.LiveMatrixClient),
        oracle,
        agent_id="@agent:test",
        expected_body_for=lambda _: "unused",
        cleanup_probes=targets if dedicated else {},
        observed_cleanup_probes=targets,
        full_request_markers_for=lambda _: frozenset({later, removed} if contaminated else {later}),
    )
    records = {
        "$root": live_fuzz.TurnRecord.create(
            source_event_ids=("$root",),
            redacted_source_event_ids=("$root",),
            pending_redaction_cleanup_event_ids=("$root",) if pending else (),
        ),
        "$probe": live_fuzz.TurnRecord.create(source_event_ids=("$probe",), response_event_id="$reply"),
    }
    events = {
        "$reply": {
            "event_id": "$reply",
            "sender": "@agent:test",
            "type": "m.room.message",
            "content": {"body": "LIVE-FUZZ call=7 END call=7"},
        },
    }
    if dedicated and pending:
        with pytest.raises(AssertionError, match="pending or missing tombstone cleanup"):
            auditor._assert_redaction_cleanup_probes(events, records)
    elif contaminated:
        with pytest.raises(AssertionError, match="redacted history"):
            auditor._assert_redaction_cleanup_probes(events, records)
    else:
        assert auditor._assert_redaction_cleanup_probes(events, records)["redaction_cleanup_checked_calls"] == 1


def test_ordinary_cleanup_checks_visible_call_even_when_redacted_owner_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A visible generation cannot evade full-request checks through a nonterminal redaction tombstone."""
    old = live_fuzz._source_marker("root:0", live_fuzz.ORIGINAL_REVISION)
    later = live_fuzz._source_marker("op:1", live_fuzz.ORIGINAL_REVISION)
    oracle = Mock(spec=live_fuzz.ExactReplyOracle)
    oracle.expected_sources = {"$old": "root:0", "$probe": "op:1"}
    monkeypatch.setattr(live_fuzz._ModelHandler, "observations_snapshot", dict)
    auditor = live_fuzz.FinalStateAuditor(
        Mock(spec=live_fuzz.LiveMatrixClient),
        oracle,
        agent_id="@agent:test",
        expected_body_for=lambda _: "unused",
        observed_cleanup_probes={"$probe": ("$old",)},
        full_request_markers_for=lambda _: frozenset({later, old}),
    )
    records = {
        "$old": live_fuzz.TurnRecord.create(source_event_ids=("$old",), redacted_source_event_ids=("$old",)),
        "$probe": live_fuzz.TurnRecord.create(
            source_event_ids=("$probe",),
            response_event_id="$reply",
            completed=False,
            redacted_source_event_ids=("$probe",),
        ),
    }
    events = {
        "$reply": {
            "event_id": "$reply",
            "sender": "@agent:test",
            "type": "m.room.message",
            "content": {"body": "LIVE-FUZZ call=7 END call=7"},
        },
    }
    with pytest.raises(AssertionError, match="redacted history"):
        auditor._assert_redaction_cleanup_probes(events, records)


@pytest.mark.parametrize("failure", [None, "history", "pending", "missing", "source", "original_only"])
def test_edit_cleanup_probe_forbids_only_removed_revision(failure: str | None) -> None:
    """Edit cleanup must be exact; original and surviving revision history stay legal."""
    oracle = Mock(spec=live_fuzz.ExactReplyOracle)
    oracle.expected_sources = {"$root": "root:0", "$probe": "op:3"}
    removed = live_fuzz._source_marker("root:0", "edit:1")
    surviving = live_fuzz._source_marker("root:0", "edit:2")
    later = live_fuzz._source_marker("op:3", live_fuzz.ORIGINAL_REVISION)
    observed = {later, surviving, live_fuzz._source_marker("root:0", live_fuzz.ORIGINAL_REVISION)}
    if failure == "history":
        observed.add(removed)
    auditor = live_fuzz.FinalStateAuditor(
        Mock(spec=live_fuzz.LiveMatrixClient),
        oracle,
        agent_id="@agent:test",
        expected_body_for=lambda _: "unused",
        cleanup_probes={"$probe": ("$a",)},
        source_revision_markers={"$root": {"$a": removed, "$b": surviving}},
        full_request_markers_for=lambda _: frozenset(observed),
    )
    records = {
        "$root": live_fuzz.TurnRecord.create(
            source_event_ids=("$root",),
            redacted_source_event_ids=("$root",) if failure == "original_only" else (),
            revision_replay={}
            if failure in {"missing", "original_only"}
            else {
                "$a": RevisionReplay(
                    "$wrong" if failure == "source" else "$root",
                    100,
                    redacted=True,
                    cleanup_pending=failure == "pending",
                ),
                "$unrelated": RevisionReplay("$root", 200, redacted=True, cleanup_pending=True),
            },
        ),
        "$probe": live_fuzz.TurnRecord.create(source_event_ids=("$probe",), response_event_id="$reply"),
    }
    events = {
        "$reply": {
            "event_id": "$reply",
            "sender": "@agent:test",
            "type": "m.room.message",
            "content": {"body": "LIVE-FUZZ call=1 END call=1"},
        },
    }
    if failure is None:
        auditor._assert_redaction_cleanup_probes(events, records)
    else:
        with pytest.raises(AssertionError, match="redaction cleanup probe"):
            auditor._assert_redaction_cleanup_probes(events, records)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [live_fuzz.LiveOperationKind.THREAD_MESSAGE, live_fuzz.LiveOperationKind.PLAIN_REPLY])
async def test_saved_later_message_qualifies_only_after_observed_tombstone(
    monkeypatch: pytest.MonkeyPatch,
    kind: live_fuzz.LiveOperationKind,
) -> None:
    """A tombstone appearing during send cannot retrospectively qualify that source."""
    stack = live_fuzz.ManagedTuwunelStack()
    client = live_fuzz.LiveMatrixClient("http://matrix.invalid", "!room:test")
    runner = live_fuzz.LiveFuzzRunner(
        stack,
        (client,),
        live_fuzz.LiveFuzzScenario(1, ()),
        reply_timeout=1,
        settle_seconds=0,
    )
    runner.event_ids["root:0"] = "$root"
    runner.oracle._ledger_observations = runner.oracle._ledger_records
    runner.oracle.expect("root:0", "$root", thread=0)
    runner.source_revision_markers["$root"]["$a"] = live_fuzz._source_marker("root:0", "edit:0")
    runner.redacted_targets["$a"] = "$redaction"
    reads = 0

    def refresh(**_kwargs: object) -> None:
        nonlocal reads
        reads += 1

    async def send(operation: live_fuzz.LiveOperation, *_args: object) -> str:
        runner.oracle._ledger_records["$root"] = live_fuzz.TurnRecord.create(
            source_event_ids=("$root",),
            revision_replay={"$a": RevisionReplay("$root", 100, redacted=True, cleanup_pending=True)},
        )
        return f"$later-{operation.operation_id}"

    monkeypatch.setattr(runner.oracle, "refresh_ledger_attributions", refresh)
    monkeypatch.setattr(runner.oracle, "pump", AsyncMock(side_effect=AssertionError("ordinary send added a wait")))
    monkeypatch.setattr(runner, "_send_expected_message", send)
    monkeypatch.setattr(runner, "_room_for_thread", lambda _: "!room:test")
    try:
        await runner._apply(live_fuzz.LiveOperation(1, kind, 0, "root:0"))
        assert runner._cleanup_probe_targets == {}
        await runner._apply(live_fuzz.LiveOperation(2, kind, 0, "root:0"))
        assert runner._cleanup_probe_targets == {"$later-2": ("$a",)}
        assert reads == 2
    finally:
        await client.close()
        stack.close()


@pytest.mark.parametrize("target", ["op:10", "op:9", "op:99"])
def test_cleanup_probe_rejects_live_edit_reaction_and_unknown_targets(target: str) -> None:
    """Only an earlier-batch redaction of the exact source or edit can qualify a probe."""
    operations = (
        (live_fuzz.LiveOperation(9, live_fuzz.LiveOperationKind.REACTION, 0, "root:0"),),
        (live_fuzz.LiveOperation(10, live_fuzz.LiveOperationKind.EDIT, 0, "root:0"),),
        (live_fuzz.LiveOperation(11, live_fuzz.LiveOperationKind.REDACTION, 0, "op:9"),),
        (
            live_fuzz.LiveOperation(
                12,
                live_fuzz.LiveOperationKind.THREAD_MESSAGE,
                0,
                "root:0",
                cleanup_sources=(target,),
            ),
        ),
    )
    with pytest.raises(ValueError, match="cleanup probe"):
        live_fuzz.LiveFuzzScenario(1, operations).validate()


def test_cleanup_probe_rejects_same_batch_edit_redaction() -> None:
    """Concurrent redaction and probe send provide no causal cleanup boundary."""
    operations = (
        (live_fuzz.LiveOperation(10, live_fuzz.LiveOperationKind.EDIT, 0, "root:0"),),
        (
            live_fuzz.LiveOperation(11, live_fuzz.LiveOperationKind.REDACTION, 0, "op:10"),
            live_fuzz.LiveOperation(
                12,
                live_fuzz.LiveOperationKind.THREAD_MESSAGE,
                0,
                "root:0",
                cleanup_sources=("op:10",),
            ),
        ),
    )
    with pytest.raises(ValueError, match="cleanup probe"):
        live_fuzz.LiveFuzzScenario(1, operations).validate()


def test_edit_cleanup_probe_uses_originating_source_thread() -> None:
    """An edit's routing thread cannot relabel the original source's cleanup session."""
    scenario = live_fuzz.LiveFuzzScenario(
        2,
        (
            (live_fuzz.LiveOperation(10, live_fuzz.LiveOperationKind.EDIT, 0, "root:1"),),
            (live_fuzz.LiveOperation(11, live_fuzz.LiveOperationKind.REDACTION, 0, "op:10"),),
        ),
    )
    qualified = live_fuzz._with_redaction_cleanup_probes(scenario)
    assert qualified.batches[-1][0].thread == 1
    wrong = live_fuzz.LiveOperation(
        12,
        live_fuzz.LiveOperationKind.THREAD_MESSAGE,
        0,
        "root:0",
        cleanup_sources=("op:10",),
    )
    with pytest.raises(ValueError, match="cleanup probe"):
        live_fuzz.LiveFuzzScenario(2, (*scenario.batches, (wrong,))).validate()


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
@pytest.mark.parametrize("edit_target", [False, True])
async def test_fuzz_cleanup_probe_waits_for_durable_tombstone_before_send(
    monkeypatch: pytest.MonkeyPatch,
    edit_target: bool,
) -> None:
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
    runner.oracle._ledger_observations = runner.oracle._ledger_records
    runner.event_ids["op:0"] = "$edit"
    target = "$edit" if edit_target else "$old"
    runner._pending_source_tombstones.add(target)
    if edit_target:
        runner.source_revision_markers["$old"]["$edit"] = live_fuzz._source_marker("root:0", "edit:0")
    tombstone = live_fuzz.TurnRecord.create(
        source_event_ids=("$old",),
        completed=True,
        redacted_source_event_ids=("$old",),
        pending_redaction_cleanup_event_ids=("$old",),
        revision_replay={"$edit": RevisionReplay("$old", 100, redacted=True, cleanup_pending=True)}
        if edit_target
        else {},
    )
    pumps = 0

    async def pump(**_kwargs: object) -> None:
        nonlocal pumps
        pumps += 1
        runner.oracle._ledger_records["$old"] = (
            live_fuzz.TurnRecord.create(source_event_ids=("$old",), redacted_source_event_ids=("$old",))
            if edit_target and pumps == 1
            else tombstone
        )

    async def send(*_args: object) -> str:
        assert runner.oracle.source_tombstoned("$old")
        assert not runner._pending_source_tombstones
        assert pumps == (2 if edit_target else 1)
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
            cleanup_sources=("op:0" if edit_target else "root:0",),
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
