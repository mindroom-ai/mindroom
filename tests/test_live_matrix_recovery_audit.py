"""Exact restart continuation ownership must agree across live and final audits."""

from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractAsyncContextManager, closing
from copy import deepcopy
from dataclasses import dataclass, replace
from io import StringIO
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock, patch

import nio
import pytest
import structlog
from agno.run.base import RunStatus

from mindroom.ai_run_metadata import build_ai_run_metadata_content
from mindroom.authorization import get_effective_sender_id_for_reply_permissions
from mindroom.config.main import Config
from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.dispatch_handoff import PreparedIngress
from mindroom.dispatch_replay_guard import has_newer_unresponded_journal_thread_event
from mindroom.event_journal import DeliveryStage, EventClass, EventJournalStore, EventKind, InboundEvent
from mindroom.journal_dispatch import JournalCallbacks, JournalDispatcher
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.message_builder import build_matrix_edit_content
from mindroom.matrix.stale_stream_cleanup import (
    _auto_resume_interrupted_threads as auto_resume_interrupted_threads,
)
from mindroom.matrix.stale_stream_cleanup import (
    _cleanup_stale_streaming_room as cleanup_stale_streaming_room,
)
from mindroom.response_delivery_recovery import ResponseDeliveryRecovery
from mindroom.turn_record import RevisionReplay, TurnRecord
from scripts.testing import fuzz_live_matrix as fuzz
from tests.access_schema_support import with_current_room_member_access
from tests.conftest import bind_runtime_paths, make_matrix_client_mock
from tests.conftest import test_runtime_paths as runtime_paths_at
from tests.identity_helpers import persist_entity_accounts
from tests.test_response_delivery_gateway import _gateway
from tests.test_stale_stream_cleanup import _aiter, _room_messages_response
from tests.test_turn_store import _store

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import PrincipalStore

ROOM = "!room:example"
AGENT = "@general:localhost"
ROUTER = "@router:localhost"
REQUESTER = "@user:example"
MARKER = "MRK[src=op:1;rev=orig]"


@dataclass
class RecoveryCase:
    """Real ownership stores and observed Matrix transport for one restart continuation."""

    journal: EventJournalStore
    oracle: fuzz.ExactReplyOracle
    auditor: fuzz.FinalStateAuditor
    events: dict[str, dict[str, Any]]


def _message(event_id: str, sender: str, timestamp: int, content: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "sender": sender,
        "type": "m.room.message",
        "room_id": ROOM,
        "_audit_room_id": ROOM,
        "origin_server_ts": timestamp,
        "content": content,
    }


def _reply_content(target: str, body: str) -> dict[str, Any]:
    return {
        "body": body,
        "msgtype": "m.text",
        "m.relates_to": {"rel_type": "m.thread", "event_id": "$root", "m.in_reply_to": {"event_id": target}},
    }


async def _admit(principal: PrincipalStore, event: dict[str, Any]) -> None:
    await principal.admit(
        InboundEvent(
            event["event_id"],
            ROOM,
            "$root",
            EventKind.MESSAGE,
            EventClass.ACTIONABLE,
            event["sender"],
            event["origin_server_ts"],
            event,
        ),
    )


async def _deliver(
    principal: PrincipalStore,
    source: str,
    stage: DeliveryStage,
    event: dict[str, Any],
    *,
    target: str | None = None,
) -> None:
    await principal.enqueue_matrix_delivery(
        delivery_id=source,
        stage=stage,
        room_id=ROOM,
        thread_id="$root",
        payload=event["content"],
        edits_event_id=target,
    )
    delivery = await principal.claim_matrix_delivery(delivery_id=source, stage=stage)
    assert delivery is not None
    event["content"] = dict(delivery.payload)
    await principal.acknowledge_matrix_delivery(
        delivery_id=source,
        stage=stage,
        event_id=event["event_id"],
        delivered_projections=(),
    )


async def _publish_resume(
    events: dict[str, dict[str, Any]],
    config: Config,
    paths: RuntimePaths,
    principal: PrincipalStore,
    journal: EventJournalStore,
    tmp_path: Path,
) -> None:
    """Publish the real startup cleanup note and trusted relay over fake Matrix transport."""
    client = make_matrix_client_mock(user_id=AGENT)
    client.rooms = {ROOM: nio.MatrixRoom(ROOM, AGENT)}
    client.room_messages.return_value = _room_messages_response(
        *(nio.RoomMessageText.from_dict(event) for event in events.values()),
    )
    client.room_get_event_relations = Mock(side_effect=lambda *_args, **_kwargs: _aiter())

    async def send(*_args: object, **kwargs: object) -> nio.RoomSendResponse:
        content = cast("dict[str, Any]", kwargs["content"])
        editing = content.get("m.relates_to", {}).get("rel_type") == "m.replace"
        event_id = "$restart-edit" if editing else "$relay"
        events[event_id] = _message(event_id, AGENT if editing else ROUTER, 400_000 if editing else 500_000, content)
        return nio.RoomSendResponse(event_id, ROOM)

    client.room_send.side_effect = send
    turn_store = await _store(journal, agent_name="general")
    gateway = _gateway(tmp_path, principal)
    gateway = replace(
        gateway,
        deps=replace(
            gateway.deps,
            response_recovery=ResponseDeliveryRecovery(
                principal,
                lambda: turn_store,
                gateway.deps.redact_message_event,
            ),
        ),
    )

    def recovery_scope(_agent: str, room_id: str, event_id: str) -> AbstractAsyncContextManager[bool]:
        return gateway.response_recovery_scope(room_id, event_id)

    with patch("mindroom.matrix.stale_stream_cleanup.time.time", return_value=1_000):
        cleaned, interrupted = await cleanup_stale_streaming_room(
            client,
            room_id=ROOM,
            actors={AGENT: client},
            bot_user_ids={AGENT, ROUTER},
            config=config,
            runtime_paths=paths,
            startup_cutoff_ms=900_000,
            response_recovery_scope=recovery_scope,
        )
    assert cleaned == 1
    assert len(interrupted) == 1
    assert interrupted[0].original_sender_id == REQUESTER
    client.user_id = ROUTER
    history = [
        ResolvedVisibleMessage.synthetic(sender=AGENT, body="interrupted", event_id="$interrupted", timestamp=400_000),
    ]

    with patch(
        "mindroom.matrix.stale_stream_cleanup.fetch_thread_messages_from_source",
        AsyncMock(return_value=history),
    ):
        resumed = await auto_resume_interrupted_threads(
            client,
            interrupted,
            response_recovery_scope=recovery_scope,
            config=config,
            runtime_paths=paths,
        )
    assert resumed == 1
    assert events["$relay"]["content"][ORIGINAL_SENDER_KEY] == REQUESTER


def _observe_model_requests(events: dict[str, dict[str, Any]], *, published: bool) -> None:
    """Extract actual request markers with an empty final relay user message."""
    request = {
        "messages": [
            {"role": "user", "content": MARKER},
            {"role": "assistant", "content": "interrupted"},
            {"role": "user", "content": events["$relay"]["content"]["body"]},
        ],
    }
    fuzz._ModelHandler.reset_observations()
    if published:
        fuzz._ModelHandler._record_observation(233, frozenset({MARKER}), full_request_markers=frozenset({MARKER}))
    fuzz._ModelHandler._record_observation(
        287,
        fuzz._ModelHandler._final_user_markers(request),
        full_request_markers=fuzz._parse_markers(json.dumps(request)),
    )
    assert fuzz._ModelHandler.observed_markers_for(287) == frozenset()
    assert fuzz._ModelHandler.full_request_markers_for(287) == {MARKER}


async def _recover_case(tmp_path: Path, *, streaming: bool = False, published: bool = False) -> RecoveryCase:
    """Keep admission, startup cleanup, guard, settlement and delivery storage real."""
    paths = runtime_paths_at(tmp_path)
    config = bind_runtime_paths(
        with_current_room_member_access(Config(agents={"general": {"display_name": "General"}})),
        paths,
    )
    persist_entity_accounts(config, paths, usernames={"general": "general", "router": "router"})
    events = {
        "$root": _message("$root", REQUESTER, 100_000, {"body": "thread", "msgtype": "m.text"}),
        "$source": _message("$source", REQUESTER, 200_000, _reply_content("$root", MARKER)),
        "$interrupted": _message(
            "$interrupted",
            AGENT,
            300_000,
            _reply_content("$source", "Thinking...") | {fuzz.STREAM_STATUS_KEY: "pending"},
        ),
    }
    if published:
        events["$interrupted"]["content"]["body"] = "LIVE-FUZZ call=233 END"
        events["$interrupted"]["content"][fuzz.STREAM_STATUS_KEY] = "streaming"
    journal = EventJournalStore.open_sqlite(tmp_path / "event_journal.db")
    principal = journal.principal(f"general@{AGENT}")
    await _admit(principal, events["$source"])
    await _deliver(principal, "$source", DeliveryStage.INITIAL, events["$interrupted"])
    old = TurnRecord.create(("$source",), response_event_id="$interrupted", completed=False, requester_id=REQUESTER)
    await journal.turn_records("general").upsert(
        index_event_ids=old.indexed_event_ids,
        anchor_event_id=old.anchor_event_id,
        record_json=json.dumps(fuzz.TurnRecordCodec._to_ledger_record(old)),
    )

    # Synthetic continuation belongs only to an orphan; pending originals replay canonically.
    await principal.settle("$source")
    await _publish_resume(events, config, paths, principal, journal, tmp_path)
    await _admit(principal, events["$relay"])
    output = StringIO()
    logger = cast(
        "structlog.stdlib.BoundLogger",
        structlog.wrap_logger(
            structlog.PrintLogger(file=output),
            processors=[structlog.dev.ConsoleRenderer(colors=True)],
        ).bind(agent="general", logger="mindroom.bot"),
    )
    skipped = await has_newer_unresponded_journal_thread_event(
        room_id=ROOM,
        event=PreparedIngress(REQUESTER, "$source", MARKER, events["$source"], server_timestamp=200_000),
        requester_user_id=REQUESTER,
        thread_id="$root",
        may_be_superseded_by_newer_requester_turn=True,
        pending_turns=principal,
        requester_user_id_for_event=lambda sender, event: get_effective_sender_id_for_reply_permissions(
            sender,
            event,
            config,
            paths,
        ),
        is_visible_router_voice_echo=lambda *_: False,
        sender_is_trusted_for_ingress_metadata=lambda sender: sender in {AGENT, ROUTER},
        is_handled=lambda _: False,
        logger=logger,
    )
    assert skipped
    dispatcher = JournalDispatcher(principal, Mock(spec=JournalCallbacks), lambda room: nio.MatrixRoom(room, AGENT))
    await dispatcher.settle_intentionally_ignored_turn_sources(("$source",))
    await principal.settle_many(("$relay",))

    events["$answer"] = _message(
        "$answer",
        AGENT,
        600_000,
        _reply_content("$relay", "Thinking...") | {fuzz.STREAM_STATUS_KEY: "pending"},
    )
    await _deliver(principal, "$relay", DeliveryStage.INITIAL, events["$answer"])
    metadata = build_ai_run_metadata_content(
        config=config,
        model_name="default",
        model=None,
        model_provider=None,
        run_id="recovery-run",
        session_id="recovery-session",
        status=RunStatus.completed,
    )
    completed_content = _reply_content("$relay", "LIVE-FUZZ call=287 END call=287") | metadata
    if streaming:
        completed_content[fuzz.STREAM_STATUS_KEY] = "completed"
    events["$final"] = _message("$final", AGENT, 700_000, build_matrix_edit_content("$answer", completed_content))
    await _deliver(principal, "$relay", DeliveryStage.FINAL, events["$final"], target="$answer")
    turn = TurnRecord.create(("$relay",), response_event_id="$answer", completed=True, requester_id=REQUESTER)
    await journal.turn_records("general").upsert(
        index_event_ids=turn.indexed_event_ids,
        anchor_event_id=turn.anchor_event_id,
        record_json=json.dumps(fuzz.TurnRecordCodec._to_ledger_record(turn)),
    )

    _observe_model_requests(events, published=published)
    transport = Mock(spec=fuzz.LiveMatrixClient)
    transport.room_id = ROOM
    transport.paginate_room = AsyncMock(
        side_effect=lambda room: [event for event in events.values() if event["_audit_room_id"] == room],
    )
    oracle = fuzz.ExactReplyOracle(
        transport,
        AGENT,
        internal_relay_senders={ROUTER},
        coalescing_threads=True,
        ledger_path=tmp_path / "event_journal.db",
        expected_body_for=lambda call: f"LIVE-FUZZ call={call} END call={call}",
    )
    oracle.log_path = tmp_path / "mindroom.log"
    oracle.log_path.write_text(output.getvalue())
    oracle.expect("op:1", "$source")
    oracle.source_current_markers = {"$source": MARKER}
    for event in events.values():
        oracle._ingest_event(event)
    assert oracle.internal_source_ids == {"$relay"}
    auditor = fuzz.FinalStateAuditor(
        transport,
        oracle,
        agent_id=AGENT,
        ledger_path=oracle.ledger_path,
        expected_body_for=oracle.expected_body_for,
        source_current_markers=oracle.source_current_markers,
    )
    return RecoveryCase(journal, oracle, auditor, events)


@pytest.mark.asyncio
@pytest.mark.parametrize("live_poll", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
async def test_exact_restart_continuation_settles_original_source(
    tmp_path: Path,
    live_poll: bool,
    streaming: bool,
) -> None:
    """A completed trusted continuation closes source debt without completing its old generation."""
    case = await _recover_case(tmp_path, streaming=streaming)
    try:
        if live_poll:
            case.oracle.refresh_ledger_attributions(min_interval=0)
            assert case.oracle.unsettled_required_sources() == []
        else:
            metrics = await case.auditor.audit(room_ids=(ROOM,), sent_records=(), redacted_targets={})
            assert metrics["ledger_recovered_sources"] == 1
            assert metrics["completed_final_bodies"] == 1
        records = fuzz.read_ledger_records(case.oracle.ledger_path, include_incomplete=True)
        assert not records["$source"].completed
        assert case.oracle.recovery_proofs["$source"].call_id == 287
    finally:
        await case.journal.close()


async def _assert_recovery(case: RecoveryCase, *, live_poll: bool, accepted: bool) -> None:
    """Exercise the real public audit boundary with independently changed durable or wire facts."""
    case.oracle.canonical_events = dict(case.events)
    if live_poll:
        case.oracle.refresh_ledger_attributions(min_interval=0)
        assert (case.oracle.unsettled_required_sources() == []) is accepted
        assert ("$source" in case.oracle.recovery_proofs) is accepted
    elif accepted:
        metrics = await case.auditor.audit(room_ids=(ROOM,), sent_records=(), redacted_targets={})
        assert metrics["ledger_recovered_sources"] == 1
    else:
        with pytest.raises(AssertionError):
            await case.auditor.audit(room_ids=(ROOM,), sent_records=(), redacted_targets={})


def _sql(case: RecoveryCase, statement: str, values: tuple[object, ...]) -> None:
    assert case.oracle.ledger_path is not None
    with closing(sqlite3.connect(case.oracle.ledger_path)) as database, database:
        database.execute(statement, values)


@pytest.mark.asyncio
@pytest.mark.parametrize("live_poll", [False, True])
@pytest.mark.parametrize("source", ["$source", "$relay"])
@pytest.mark.parametrize(
    ("query", "value"),
    [
        ("UPDATE journal_events SET state = ? WHERE event_id = ?", "pending"),
        ("UPDATE journal_events SET principal_id = ? WHERE event_id = ?", "foreign@principal"),
        ("UPDATE journal_events SET room_id = ? WHERE event_id = ?", "!other:example"),
        ("UPDATE journal_events SET thread_id = ? WHERE event_id = ?", "$other"),
        ("UPDATE journal_events SET sender = ? WHERE event_id = ?", "@other:example"),
        ("UPDATE journal_events SET origin_server_ts = ? WHERE event_id = ?", 999),
        ("UPDATE journal_events SET kind = ? WHERE event_id = ?", "reaction"),
        ("missing", None),
    ],
)
async def test_recovery_requires_exact_settled_journal_identity(
    tmp_path: Path,
    live_poll: bool,
    source: str,
    query: str,
    value: object,
) -> None:
    """Absent, unsettled or mismatched principal-scoped admission cannot prove continuation."""
    case = await _recover_case(tmp_path)
    try:
        if query == "missing":
            _sql(case, "DELETE FROM journal_events WHERE event_id = ?", (source,))
        else:
            _sql(case, query, (value, source))
        await _assert_recovery(case, live_poll=live_poll, accepted=False)
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("live_poll", [False, True])
@pytest.mark.parametrize(
    ("event_id", "path", "value"),
    [
        ("$relay", ("sender",), "@untrusted:example"),
        ("$relay", ("content", fuzz.SOURCE_KIND_KEY), "message"),
        ("$relay", ("content", ORIGINAL_SENDER_KEY), "@other:example"),
        ("$relay", ("content", ORIGINAL_SENDER_KEY), None),
        ("$relay", ("content", "body"), "unrelated router message"),
        ("$relay", ("content", "m.relates_to", "m.in_reply_to", "event_id"), "$other"),
        ("$relay", ("content", "m.relates_to", "event_id"), "$other"),
        ("$relay", ("_audit_room_id",), "!other:example"),
        ("$relay", ("origin_server_ts",), 250_000),
        ("$answer", ("content", "m.relates_to", "m.in_reply_to", "event_id"), "$other"),
        ("$answer", ("content", "m.relates_to", "event_id"), "$other"),
        ("$answer", ("_audit_room_id",), "!other:example"),
        ("$answer", ("origin_server_ts",), 450_000),
        ("$restart-edit", ("content", "m.new_content", fuzz.STREAM_STATUS_KEY), "streaming"),
        ("$restart-edit", ("_audit_room_id",), "!other:example"),
    ],
)
async def test_recovery_requires_exact_trusted_wire_chain(
    tmp_path: Path,
    live_poll: bool,
    event_id: str,
    path: tuple[str, ...],
    value: object,
) -> None:
    """Matching source text cannot substitute for trusted requester and causal Matrix identity."""
    case = await _recover_case(tmp_path)
    try:
        target = case.events[event_id]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        await _assert_recovery(case, live_poll=live_poll, accepted=False)
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("live_poll", [False, True])
@pytest.mark.parametrize("event_id", ["$interrupted", "$relay", "$answer"])
async def test_recovery_rejects_duplicate_chain_events(tmp_path: Path, live_poll: bool, event_id: str) -> None:
    """A second original, relay or answer prevents exact unique continuation attribution."""
    case = await _recover_case(tmp_path)
    try:
        duplicate = deepcopy(case.events[event_id])
        duplicate["event_id"] = "$duplicate"
        case.events["$duplicate"] = duplicate
        await _assert_recovery(case, live_poll=live_poll, accepted=False)
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("live_poll", [False, True])
@pytest.mark.parametrize("source", ["$source", "$relay"])
@pytest.mark.parametrize(
    "defect",
    ["missing", "requester", "response", "redacted", "source_cleanup", "revision_cleanup", "pending_edit"],
)
async def test_recovery_preserves_exact_turn_and_cleanup_ownership(
    tmp_path: Path,
    live_poll: bool,
    source: str,
    defect: str,
) -> None:
    """Every owner retains independent source, requester, visible response and mutation debt."""
    case = await _recover_case(tmp_path)
    try:
        assert case.oracle.ledger_path is not None
        record = fuzz.read_ledger_records(case.oracle.ledger_path, include_incomplete=True)[source]
        changes = {
            "requester": {"requester_id": "@other:example"},
            "response": {"response_event_id": "$other"},
            "redacted": {"redacted_source_event_ids": (source,)},
            "source_cleanup": {
                "discovery_event_ids": ("$edit",),
                "redacted_source_event_ids": ("$edit",),
                "pending_redaction_cleanup_event_ids": ("$edit",),
            },
            "revision_cleanup": {"revision_replay": {"$edit": RevisionReplay(source, 100, cleanup_pending=True)}},
        }
        if defect == "pending_edit":
            case.oracle.pending_edit_markers = {source: {"$edit": "MRK[src=op:1;rev=edit:1]"}}
            case.auditor.pending_edit_markers = dict(case.oracle.pending_edit_markers)
        else:
            await case.journal.turn_records("general").forget(index_event_ids=record.indexed_event_ids)
            if defect != "missing":
                record = replace(record, **changes[defect])
                await case.journal.turn_records("general").upsert(
                    index_event_ids=record.indexed_event_ids,
                    anchor_event_id=record.anchor_event_id,
                    record_json=json.dumps(fuzz.TurnRecordCodec._to_ledger_record(record)),
                )
                stored = fuzz.read_ledger_records(case.oracle.ledger_path, include_incomplete=True)[source]
                assert stored == record
        if live_poll and source == "$source" and defect == "redacted":
            case.oracle.refresh_ledger_attributions(min_interval=0)
            assert case.oracle.recovery_proofs == {}
        else:
            await _assert_recovery(case, live_poll=live_poll, accepted=False)
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("live_poll", [False, True])
@pytest.mark.parametrize(
    ("query", "value"),
    [
        ("UPDATE matrix_delivery_outbox SET acknowledged_event_id = ? WHERE delivery_id = ? AND stage = ?", None),
        ("UPDATE matrix_delivery_outbox SET acknowledged_event_id = ? WHERE delivery_id = ? AND stage = ?", "$other"),
        ("UPDATE matrix_delivery_outbox SET edits_event_id = ? WHERE delivery_id = ? AND stage = ?", "$other"),
        ("UPDATE matrix_delivery_outbox SET room_id = ? WHERE delivery_id = ? AND stage = ?", "!other:example"),
        ("UPDATE matrix_delivery_outbox SET thread_id = ? WHERE delivery_id = ? AND stage = ?", "$other"),
        ("UPDATE matrix_delivery_outbox SET attempted = ? WHERE delivery_id = ? AND stage = ?", 0),
        ("UPDATE matrix_delivery_outbox SET retired = ? WHERE delivery_id = ? AND stage = ?", 1),
        ("UPDATE matrix_delivery_outbox SET edit_target_pending = ? WHERE delivery_id = ? AND stage = ?", 1),
        (
            "UPDATE matrix_delivery_outbox SET permanent_failure_reason = ? WHERE delivery_id = ? AND stage = ?",
            "refused",
        ),
        ("UPDATE matrix_delivery_outbox SET payload_json = ? WHERE delivery_id = ? AND stage = ?", "null"),
        ("UPDATE matrix_delivery_outbox SET principal_id = ? WHERE delivery_id = ? AND stage = ?", "foreign@principal"),
    ],
)
async def test_recovery_requires_exact_acknowledged_final(
    tmp_path: Path,
    live_poll: bool,
    query: str,
    value: object,
) -> None:
    """Canonical visible bytes alone cannot discharge a different or unfinished FINAL owner."""
    case = await _recover_case(tmp_path)
    try:
        _sql(
            case,
            query,
            (value, "$relay", "final"),
        )
        await _assert_recovery(case, live_poll=live_poll, accepted=False)
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("live_poll", [False, True])
@pytest.mark.parametrize(
    "signals",
    [
        {},
        {fuzz.STREAM_STATUS_KEY: "pending"},
        {fuzz.AI_RUN_METADATA_KEY: {"status": "error"}},
        {fuzz.STREAM_STATUS_KEY: None, fuzz.AI_RUN_METADATA_KEY: {"status": "completed"}},
        {fuzz.STREAM_STATUS_KEY: "completed", fuzz.AI_RUN_METADATA_KEY: None},
        {fuzz.STREAM_STATUS_KEY: "completed", fuzz.AI_RUN_METADATA_KEY: {"status": "cancelled"}},
    ],
)
async def test_recovery_requires_same_payload_terminal_completion(
    tmp_path: Path,
    live_poll: bool,
    signals: dict[str, Any],
) -> None:
    """Both durable and visible FINAL payloads must carry consistent successful completion."""
    case = await _recover_case(tmp_path)
    try:
        content = case.events["$final"]["content"]
        payload = content["m.new_content"]
        payload.pop(fuzz.STREAM_STATUS_KEY, None)
        payload.pop(fuzz.AI_RUN_METADATA_KEY, None)
        payload.update(signals)
        _sql(
            case,
            "UPDATE matrix_delivery_outbox SET payload_json = ? WHERE delivery_id = ? AND stage = ?",
            (json.dumps(content), "$relay", "final"),
        )
        await _assert_recovery(case, live_poll=live_poll, accepted=False)
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("live_poll", [False, True])
@pytest.mark.parametrize(
    "defect",
    ["missing", "later_summary", "stale_revision", "old_published_marker", "posted_only"],
)
async def test_recovery_cannot_borrow_other_generation_or_marker(tmp_path: Path, live_poll: bool, defect: str) -> None:
    """Only the exact completed continuation call can consume current history for its original source."""
    case = await _recover_case(tmp_path, published=defect == "old_published_marker")
    try:
        if defect in {"missing", "later_summary"}:
            fuzz._ModelHandler._record_observation(287, frozenset(), full_request_markers=frozenset())
            if defect == "later_summary":
                fuzz._ModelHandler._record_observation(
                    288,
                    frozenset({MARKER}),
                    full_request_markers=frozenset({MARKER}),
                )
        elif defect == "old_published_marker":
            fuzz._ModelHandler._record_observation(233, frozenset(), full_request_markers=frozenset({MARKER}))
        elif defect == "stale_revision":
            current = "MRK[src=op:1;rev=edit:1]"
            case.events["$source-edit"] = _message(
                "$source-edit",
                REQUESTER,
                250_000,
                build_matrix_edit_content("$source", _reply_content("$root", current)),
            )
            case.auditor.source_revision_markers = {"$source": {"$source-edit": current}}
            case.oracle.source_current_markers = {"$source": current}
        else:
            case.events.pop("$answer")
            case.events.pop("$final")
            await case.journal.turn_records("general").forget(index_event_ids=("$relay",))
            _sql(case, "DELETE FROM matrix_delivery_outbox WHERE delivery_id = ?", ("$relay",))
        await _assert_recovery(case, live_poll=live_poll, accepted=False)
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("live_poll", [False, True])
async def test_recovery_preserves_valid_published_original_prefix(tmp_path: Path, live_poll: bool) -> None:
    """A published original prefix retains its own active-marker evidence beside continuation history."""
    case = await _recover_case(tmp_path, published=True)
    try:
        await _assert_recovery(case, live_poll=live_poll, accepted=True)
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("live_poll", [False, True])
@pytest.mark.parametrize(
    "location",
    ["original", "outer_wrapper", "older_edit", "foreign_room", "bundled", "bundled_outer_wrapper"],
)
async def test_recovery_selects_one_canonical_final_payload(tmp_path: Path, live_poll: bool, location: str) -> None:
    """Completion cannot come from a different payload; a real bundled acknowledged FINAL qualifies."""
    case = await _recover_case(tmp_path)
    try:
        final = case.events["$final"]
        completed = deepcopy(final["content"]["m.new_content"][fuzz.AI_RUN_METADATA_KEY])
        if location != "bundled":
            final["content"]["m.new_content"].pop(fuzz.AI_RUN_METADATA_KEY)
        if location == "original":
            case.events["$answer"]["content"][fuzz.AI_RUN_METADATA_KEY] = completed
        elif location in {"outer_wrapper", "bundled_outer_wrapper"}:
            final["content"][fuzz.AI_RUN_METADATA_KEY] = completed
        elif location in {"older_edit", "foreign_room"}:
            other = deepcopy(final)
            other["event_id"] = "$other-final"
            other["origin_server_ts"] = 650_000 if location == "older_edit" else 800_000
            other["_audit_room_id"] = ROOM if location == "older_edit" else "!foreign:example"
            other["content"]["m.new_content"][fuzz.AI_RUN_METADATA_KEY] = completed
            case.events["$other-final"] = other
        _sql(
            case,
            "UPDATE matrix_delivery_outbox SET payload_json = ? WHERE delivery_id = ? AND stage = ?",
            (json.dumps(final["content"]), "$relay", "final"),
        )
        if location in {"bundled", "bundled_outer_wrapper"}:
            case.events.pop("$final")
            case.events["$answer"]["unsigned"] = {"m.relations": {"m.replace": {"event": final}}}
        await _assert_recovery(case, live_poll=live_poll, accepted=location == "bundled")
    finally:
        await case.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("live_poll", [False, True])
@pytest.mark.parametrize("defect", ["incomplete_turn", "pending_old_delivery", "malformed_ledger", "partial_journal"])
async def test_recovery_keeps_independent_durable_debt(tmp_path: Path, live_poll: bool, defect: str) -> None:
    """Successful visible continuation cannot hide unfinished or unreadable durable ownership."""
    case = await _recover_case(tmp_path)
    try:
        if defect == "incomplete_turn":
            assert case.oracle.ledger_path is not None
            record = fuzz.read_ledger_records(case.oracle.ledger_path, include_incomplete=True)["$relay"]
            await case.journal.turn_records("general").forget(index_event_ids=("$relay",))
            await case.journal.turn_records("general").upsert(
                index_event_ids=("$relay",),
                anchor_event_id="$relay",
                record_json=json.dumps(fuzz.TurnRecordCodec._to_ledger_record(replace(record, completed=False))),
            )
        elif defect == "pending_old_delivery":
            await case.journal.principal(f"general@{AGENT}").enqueue_matrix_delivery(
                delivery_id="$source",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id="$root",
                payload={"body": "owed"},
                edits_event_id="$interrupted",
            )
            _sql(
                case,
                "UPDATE matrix_delivery_outbox SET room_id = ?, thread_id = ? WHERE delivery_id = ? AND stage = ?",
                ("!other:example", "$other", "$source", "final"),
            )
            assert case.oracle.ledger_path is not None
            snapshot = fuzz._read_supersession_snapshot(case.oracle.ledger_path, f"general@{AGENT}")
            assert any(delivery.delivery_id == "$source" for delivery in snapshot.pending_deliveries)
        elif defect == "malformed_ledger":
            _sql(case, "UPDATE turn_records SET record_json = ? WHERE index_event_id = ?", ("null", "$source"))
        else:
            _sql(case, "DROP TABLE journal_events", ())
        await _assert_recovery(case, live_poll=live_poll, accepted=False)
    finally:
        await case.journal.close()
