"""Exact redaction cleanup debt can outlive an interrupted response."""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import nio
import pytest
from agno.agent import Agent
from agno.db.base import BaseDb, SessionType
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession

from mindroom.agent_storage import create_state_storage, get_agent_session
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.event_journal import DeliveryStage, EventClass, EventKind, journal
from mindroom.handled_turns import TurnRecord, TurnRecordCodec, _reset_handled_turn_ledger_runtime
from mindroom.history.types import HistoryScope
from mindroom.matrix.journal_ingress import replayable_redaction_target
from mindroom.message_target import MessageTarget
from mindroom.response_runner import ResponseRunner
from mindroom.turn_store import TurnStore
from tests.journal_helpers import admit_dispatch_event
from tests.test_orderly_shutdown_recovery import _dispatcher
from tests.test_response_delivery_gateway import _response_recovery_bot
from tests.test_turn_store import _FakeAgentStorage, _ReplayCaptureModel, _store, _store_with_storage

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.event_journal import EventJournalStore, JournalEvent
    from mindroom.event_journal.backend import Transaction

ROOM = "!redaction:localhost"
SOURCE = "$deleted"
REDACTION = "$redaction"
pytestmark = pytest.mark.ledger_loads_from_disk


def _message(event_id: str = SOURCE, body: str = "deleted prompt") -> nio.RoomMessageText:
    event = nio.Event.parse_event(
        {
            "event_id": event_id,
            "type": "m.room.message",
            "sender": "@human:localhost",
            "origin_server_ts": 1000,
            "content": {"msgtype": "m.text", "body": body},
        },
    )
    assert isinstance(event, nio.RoomMessageText)
    return event


def _redaction(target: str = SOURCE) -> nio.RedactionEvent:
    event = nio.Event.parse_event(
        {
            "event_id": REDACTION,
            "type": "m.room.redaction",
            "sender": "@human:localhost",
            "origin_server_ts": 2000,
            "redacts": target,
            "content": {},
        },
    )
    assert isinstance(event, nio.RedactionEvent)
    return event


@pytest.mark.asyncio
async def test_pending_redaction_owns_interrupted_initial_response(  # noqa: PLR0915
    journal_store: EventJournalStore,
) -> None:
    """Admission retires generation while the replayable callback still owns cleanup."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store)
    bot = _response_recovery_bot(journal_store, store)
    dispatcher = _dispatcher(principal, MagicMock())
    dispatcher.callbacks = replace(dispatcher.callbacks, on_redaction=bot._on_redaction)
    room = nio.MatrixRoom(ROOM, "@bot:localhost")
    await admit_dispatch_event(dispatcher, room, _message(), EventKind.MESSAGE, EventClass.ACTIONABLE)
    turn = TurnRecord.create(
        [SOURCE],
        completed=False,
        response_event_id="$initial",
        requester_id="@human:localhost",
        response_owner="agent",
        conversation_target=MessageTarget.resolve(ROOM, "$thread", SOURCE),
        history_scope=HistoryScope(kind="agent", scope_id="agent"),
        source_event_prompts={SOURCE: "deleted prompt"},
    )
    await store.record_pending_turn(turn)
    await principal.enqueue_matrix_delivery(
        delivery_id=SOURCE,
        stage=DeliveryStage.INITIAL,
        room_id=ROOM,
        thread_id="$thread",
        payload={"msgtype": "m.text", "body": "Thinking..."},
    )
    await principal.acknowledge_matrix_delivery(
        delivery_id=SOURCE,
        stage=DeliveryStage.INITIAL,
        event_id="$initial",
        delivered_projections=(),
    )
    started = asyncio.Event()

    async def generate() -> None:
        assert store.try_claim_turn(turn)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            store.release_pending_turn_claim(turn)

    runner = ResponseRunner(deps=MagicMock())
    response = runner.track_inbox_response(
        generate(),
        name="inbox_response:deleted",
        recovery_proof_ready=lambda: bot._response_recovery_ready(turn),
        source_event_ids=turn.source_event_ids,
    )
    await started.wait()
    try:
        await admit_dispatch_event(dispatcher, room, _redaction(), EventKind.REDACTION, EventClass.ACTIONABLE)
        assert not await principal.is_pending(SOURCE)
        assert await principal.is_pending(REDACTION)
        proof = runner._inbox_response_tasks[response].recovery_proof_ready
        pending_proof = proof()
        assert inspect.isawaitable(pending_proof)
        assert not await pending_proof
        response.cancel()
        await asyncio.gather(response, return_exceptions=True)
        assert not store.has_live_turn_claim(SOURCE)
        terminal_proof = proof()
        assert inspect.isawaitable(terminal_proof)
        assert await terminal_proof, "exact pending redaction cleanup must release the interrupted source"
        assert await principal.load_matrix_delivery(delivery_id=SOURCE, stage=DeliveryStage.FINAL) is None
        incomplete = store.get_turn_record(SOURCE)
        assert incomplete is not None
        assert not incomplete.completed
        assert incomplete.redacted_source_event_ids == ()
        assert await dispatcher.drain_once() == 1
        redacted = store.get_turn_record(SOURCE)
        assert redacted is not None
        assert not redacted.completed
        assert redacted.redacted_source_event_ids == (SOURCE,)
        assert redacted.pending_redaction_cleanup_event_ids == (SOURCE,)
        assert not redacted.source_event_prompts
        assert not await principal.is_pending(REDACTION)
    finally:
        response.cancel()
        await asyncio.gather(response, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("callback_first", [False, True])
async def test_restart_survivor_cleans_pending_redaction_before_generation(  # noqa: PLR0915
    journal_store: EventJournalStore,
    tmp_path: Path,
    callback_first: bool,
) -> None:
    """Reopened Agno history is sanitized whichever callback runs first."""
    principal = journal_store.principal("agent@alice")
    target = MessageTarget.resolve(ROOM, "$thread", "$survivor")
    scope = HistoryScope(kind="agent", scope_id="agent")
    store = await _store(journal_store)

    def storage_factory(*_args: object, **_kwargs: object) -> BaseDb:
        return create_state_storage("agent", tmp_path, subdir="sessions", session_table="agent_sessions")

    store.deps.state_writer.create_storage.side_effect = storage_factory
    store.deps.state_writer.history_scope.return_value = scope
    store.deps.state_writer.session_type_for_scope.return_value = SessionType.AGENT
    original_run = RunOutput(
        run_id="interrupted",
        agent_id="agent",
        session_id=target.session_id,
        messages=[Message(role="user", content="deleted prompt")],
        metadata={"matrix_event_id": SOURCE},
    )
    session = AgentSession(session_id=target.session_id, agent_id="agent", runs=[original_run])
    storage = storage_factory()
    storage.upsert_session(session)
    storage.upsert_run(original_run, session_id=target.session_id)
    storage.close()
    initial_dispatcher = _dispatcher(principal, MagicMock())
    room = nio.MatrixRoom(ROOM, "@bot:localhost")
    for event in (_message(), _message("$survivor", "keep prompt")):
        await admit_dispatch_event(initial_dispatcher, room, event, EventKind.MESSAGE, EventClass.ACTIONABLE)
    turn = TurnRecord.create(
        [SOURCE, "$survivor"],
        completed=False,
        requester_id="@human:localhost",
        response_owner="agent",
        conversation_target=target,
        history_scope=scope,
        source_event_prompts={SOURCE: "deleted prompt", "$survivor": "keep prompt"},
    )
    await store.record_pending_turn(turn)
    await admit_dispatch_event(initial_dispatcher, room, _redaction(), EventKind.REDACTION, EventClass.ACTIONABLE)
    assert await _response_recovery_bot(journal_store, store)._response_recovery_ready(turn)
    _reset_handled_turn_ledger_runtime()
    reopened = TurnStore(store.deps)
    await reopened.warm()
    bot = _response_recovery_bot(journal_store, reopened)
    model = _ReplayCaptureModel(id="test", name="test", provider="test")

    async def survivor(_room: nio.MatrixRoom, event: nio.RoomMessageFormatted) -> TurnDispatchOutcome:
        assert event.event_id == "$survivor"
        assert not await reopened.prepare_pending_response_source(
            target=target,
            source_event_ids=("$survivor",),
            terminal_source_event_ids=(),
        )
        recovered_storage = storage_factory()
        try:
            answer = await Agent(
                id="agent",
                db=recovered_storage,
                model=model,
                add_history_to_context=True,
            ).arun(event.body, session_id=target.session_id)
        finally:
            recovered_storage.close()
        assert answer.content == "captured"
        await principal.enqueue_matrix_delivery(
            delivery_id="$survivor",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id="$thread",
            payload={"msgtype": "m.text", "body": answer.content},
            settle_source_event_ids=("$survivor",),
        )
        return TurnDispatchOutcome.DEFERRED

    dispatcher = _dispatcher(principal, survivor)
    dispatcher.callbacks = replace(dispatcher.callbacks, on_redaction=bot._on_redaction)
    if callback_first:
        await dispatcher._worker.drain_once()
        assert not await principal.is_pending(REDACTION)
    await dispatcher.drain_once()
    assert len(model.requests) == 1
    assert "keep prompt" in model.requests[0]
    assert "deleted prompt" not in model.requests[0]
    assert not await principal.is_pending("$survivor")
    assert not await principal.is_pending(REDACTION)
    assert await principal.load_matrix_delivery(delivery_id="$survivor", stage=DeliveryStage.FINAL) is not None
    redacted = reopened.get_turn_record(SOURCE)
    assert redacted is not None
    assert SOURCE in redacted.redacted_source_event_ids
    assert redacted.source_event_prompts == {"$survivor": "keep prompt"}
    # A later callback may conservatively rearm cleanup; it remains durable and safe to repeat.
    assert not await reopened.prepare_pending_response_source(
        target=target,
        source_event_ids=("$next",),
        terminal_source_event_ids=(),
    )
    storage = storage_factory()
    persisted = get_agent_session(storage, target.session_id)
    assert persisted is not None
    assert "deleted prompt" not in str([run.messages for run in persisted.runs or []])
    storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("late_registration", [False, True])
@pytest.mark.parametrize(
    "fault",
    ["none", "unredacted_late", "missing_ledger", "corrupt_ledger", "wrong_room", "missing_context"],
)
async def test_completed_redaction_requires_current_cleanup_authority(
    journal_store: EventJournalStore,
    late_registration: bool,
    fault: str,
) -> None:
    """The monotonic ledger retains authority before and after late response registration."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store)
    bot = _response_recovery_bot(journal_store, store)
    dispatcher = _dispatcher(principal, MagicMock())
    dispatcher.callbacks = replace(dispatcher.callbacks, on_redaction=bot._on_redaction)
    room = nio.MatrixRoom(ROOM, "@bot:localhost")
    await admit_dispatch_event(dispatcher, room, _message(), EventKind.MESSAGE, EventClass.ACTIONABLE)
    turn = TurnRecord.create(
        [SOURCE],
        completed=False,
        requester_id="@human:localhost",
        history_scope=HistoryScope(kind="agent", scope_id="agent"),
        conversation_target=MessageTarget.resolve(ROOM, "$thread", SOURCE),
        source_event_prompts={SOURCE: "deleted prompt"},
    )
    if not late_registration:
        await store.record_pending_turn(turn)
    await admit_dispatch_event(dispatcher, room, _redaction(), EventKind.REDACTION, EventClass.ACTIONABLE)
    assert await dispatcher.drain_once() == 1
    if late_registration:
        await store.record_pending_turn(turn)
    current = store.get_turn_record(SOURCE)
    assert current is not None
    assert not current.completed
    assert current.redacted_source_event_ids == (SOURCE,)
    assert current.pending_redaction_cleanup_event_ids == (SOURCE,)
    assert not current.source_event_prompts
    if fault == "missing_ledger":
        await journal_store.backend.write(
            lambda transaction: transaction.execute("DELETE FROM turn_records WHERE index_event_id = ?", (SOURCE,)),
        )
    elif fault != "none":
        raw = TurnRecordCodec._to_ledger_record(turn if fault == "unredacted_late" else current)
        if fault == "wrong_room":
            raw = TurnRecordCodec._to_ledger_record(
                replace(current, conversation_target=MessageTarget.resolve("!other:localhost", "$thread", SOURCE)),
            )
        elif fault == "missing_context":
            raw = TurnRecordCodec._to_ledger_record(replace(current, conversation_target=None))
        encoded = "{" if fault == "corrupt_ledger" else json.dumps(raw)
        await journal_store.backend.write(
            lambda transaction: transaction.execute(
                "UPDATE turn_records SET record_json = ? WHERE index_event_id = ?",
                (encoded, SOURCE),
            ),
        )
    assert await bot._response_recovery_ready(turn) is (fault == "none")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "missing_source",
        "bare_tombstone",
        "wrong_principal",
        "wrong_room",
        "wrong_kind",
        "wrong_target",
        "wrong_event_id",
        "malformed",
        "unreplayable",
        "bad_security",
        "context_only",
    ],
)
async def test_redaction_proof_rejects_inexact_callback_debt(journal_store: EventJournalStore, case: str) -> None:
    """Pending cleanup counts only when the exact retained callback can perform it."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store)
    bot = _response_recovery_bot(journal_store, store)
    dispatcher = _dispatcher(principal, MagicMock())
    room = nio.MatrixRoom(ROOM, "@bot:localhost")
    await admit_dispatch_event(dispatcher, room, _message(), EventKind.MESSAGE, EventClass.ACTIONABLE)
    turn = TurnRecord.create([SOURCE], completed=False)
    await store.record_pending_turn(turn)
    await admit_dispatch_event(
        dispatcher,
        room,
        _redaction(),
        EventKind.REDACTION,
        EventClass.CONTEXT_ONLY if case == "context_only" else EventClass.ACTIONABLE,
    )
    if case == "missing_source":
        await journal_store.backend.write(
            lambda transaction: transaction.execute("DELETE FROM journal_events WHERE event_id = ?", (SOURCE,)),
        )
    elif case == "bare_tombstone":
        await principal.settle(REDACTION)
    elif case in {"wrong_principal", "wrong_room", "wrong_kind"}:
        column, value = {
            "wrong_principal": ("principal_id", "agent@other"),
            "wrong_room": ("room_id", "!other:localhost"),
            "wrong_kind": ("kind", "message"),
        }[case]
        await journal_store.backend.write(
            lambda transaction: transaction.execute(
                f"UPDATE journal_events SET {column} = ? WHERE event_id = ?",  # noqa: S608 - fixed column allowlist
                (value, REDACTION),
            ),
        )
    elif case != "context_only":
        payload = dict(_redaction().source)
        if case == "wrong_target":
            payload["redacts"] = "$other"
        elif case == "wrong_event_id":
            payload["event_id"] = "$other"
        elif case == "unreplayable":
            payload.pop("sender")
        elif case == "bad_security":
            payload["io.mindroom.dispatch_recovery_security"] = "invalid"
        source_json = "{" if case == "malformed" else json.dumps(payload)
        await journal_store.backend.write(
            lambda transaction: transaction.execute(
                "UPDATE journal_events SET source_json = ? WHERE event_id = ?",
                (source_json, REDACTION),
            ),
        )
    assert not await bot._response_recovery_ready(turn)


@pytest.mark.asyncio
@pytest.mark.parametrize("uncovered_sibling", [False, True])
async def test_mixed_response_needs_independent_ownership_for_every_source(
    journal_store: EventJournalStore,
    uncovered_sibling: bool,
) -> None:
    """A pending survivor cannot excuse an unrelated settled sibling."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store)
    bot = _response_recovery_bot(journal_store, store)
    dispatcher = _dispatcher(principal, MagicMock())
    room = nio.MatrixRoom(ROOM, "@bot:localhost")
    sources = (SOURCE, "$survivor", "$uncovered") if uncovered_sibling else (SOURCE, "$survivor")
    for source in sources:
        await admit_dispatch_event(dispatcher, room, _message(source), EventKind.MESSAGE, EventClass.ACTIONABLE)
    turn = TurnRecord.create(sources, completed=False)
    await store.record_pending_turn(turn)
    await admit_dispatch_event(dispatcher, room, _redaction(), EventKind.REDACTION, EventClass.ACTIONABLE)
    if uncovered_sibling:
        await principal.settle("$uncovered")
    assert await bot._response_recovery_ready(turn) is not uncovered_sibling
    assert await principal.is_pending("$survivor")
    assert await principal.is_pending(REDACTION)


@pytest.mark.asyncio
async def test_redaction_callback_settlement_cannot_split_recovery_snapshot(
    journal_store: EventJournalStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One read sees pending cleanup or its committed marker across the callback transition."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store)
    bot = _response_recovery_bot(journal_store, store)
    dispatcher = _dispatcher(principal, MagicMock())
    dispatcher.callbacks = replace(dispatcher.callbacks, on_redaction=bot._on_redaction)
    room = nio.MatrixRoom(ROOM, "@bot:localhost")
    await admit_dispatch_event(dispatcher, room, _message(), EventKind.MESSAGE, EventClass.ACTIONABLE)
    turn = TurnRecord.create([SOURCE], completed=False)
    await store.record_pending_turn(turn)
    await admit_dispatch_event(dispatcher, room, _redaction(), EventKind.REDACTION, EventClass.ACTIONABLE)
    records_read = threading.Event()
    callback_settled = threading.Event()
    original = journal.source_has_redaction_handoff

    def interleave(
        transaction: Transaction,
        principal_id: str,
        event_id: str,
        captured: TurnRecord,
        current: TurnRecord | None,
        redaction_target: Callable[[JournalEvent], str | None],
    ) -> bool:
        records_read.set()
        assert callback_settled.wait(5), "callback did not settle while recovery retained its snapshot"
        return original(transaction, principal_id, event_id, captured, current, redaction_target)

    monkeypatch.setattr(journal, "source_has_redaction_handoff", interleave)
    reading = asyncio.create_task(
        principal.response_recovery_state(
            turn_record=turn,
            agent_name="agent",
            redaction_target=replayable_redaction_target,
        ),
    )
    try:
        assert await asyncio.to_thread(records_read.wait, 5)
        assert await dispatcher.drain_once() == 1
        assert not await principal.is_pending(REDACTION)
    finally:
        callback_settled.set()
    snapshot = await reading
    assert snapshot.redacted_sources == (True,)
    assert snapshot.turn_records[0] is not None
    assert snapshot.turn_records[0].redacted_source_event_ids == ()
    assert await bot._response_recovery_ready(turn)


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted_id", [SOURCE, "$alias", "$revision"])
@pytest.mark.parametrize("scope", ["exact", "other_room", "other_principal"])
async def test_cleanup_gate_reconciles_only_its_recorded_physical_tombstones(
    journal_store: EventJournalStore,
    deleted_id: str,
    scope: str,
) -> None:
    """The batched lookup covers physical source aliases and revisions within exact scope."""
    principal = journal_store.principal("agent@alice")
    store = await _store_with_storage(journal_store, _FakeAgentStorage(None))
    target = MessageTarget.resolve(ROOM, "$thread", SOURCE)
    await store.record_pending_turn(
        TurnRecord.create(
            [SOURCE],
            discovery_event_ids=["$alias"],
            source_event_revisions={SOURCE: (1, "$revision")},
            completed=False,
            requester_id="@human:localhost",
            history_scope=HistoryScope(kind="agent", scope_id="agent"),
            conversation_target=target,
        ),
    )
    callback_owner = journal_store.principal("agent@other") if scope == "other_principal" else principal
    room_id = "!other:localhost" if scope == "other_room" else ROOM
    await admit_dispatch_event(
        _dispatcher(callback_owner, MagicMock()),
        nio.MatrixRoom(room_id, "@bot:localhost"),
        _redaction(deleted_id),
        EventKind.REDACTION,
        EventClass.CONTEXT_ONLY,
    )
    requested = (*tuple(f"$absent-{index}" for index in range(300)), deleted_id)
    assert await principal.redacted_event_ids(ROOM, requested) == (
        frozenset({deleted_id}) if scope == "exact" else frozenset()
    )
    assert not await store.prepare_pending_response_source(
        target=target,
        source_event_ids=("$later",),
        terminal_source_event_ids=(),
    )
    assert store.is_revision_redacted(deleted_id) is (scope == "exact")
