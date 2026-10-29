"""A redaction admitted for an agent survives an interrupted response and its recovery."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.event_journal import DeliveryStage, EventClass, EventKind
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord
from mindroom.message_target import MessageTarget
from mindroom.reply_scope import ReplyRuntime
from mindroom.response_runner import ResponseRunner
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.turn_record import TurnRecord
from mindroom.turn_store import TurnClaim, TurnStore
from tests.conftest import seed_session
from tests.journal_helpers import admit_dispatch_event
from tests.redaction_helpers import remove_redacted_history_like_next_response
from tests.test_orderly_shutdown_recovery import _dispatcher
from tests.test_turn_store import _in_room, _next_request, _seeded_storage, _store, _store_with_storage

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.event_journal import EventJournalStore
    from mindroom.session_storage.store import ConversationStore

ROOM = "!redaction:localhost"
SOURCE = "$deleted"
REDACTION = "$redaction"


def _redaction_callback_bot(journal_store: EventJournalStore) -> AgentBot:
    """Return the minimal bot whose redaction callback these tests hand the journal dispatcher."""
    bot = object.__new__(AgentBot)
    # No reply runs here, so a deletion ends none and nothing owes debt.
    bot._reply_runtime = ReplyRuntime(
        store=journal_store.principal("agent@alice"),
        entity_name="agent",
        generation="gen-1",
        retry_sources=lambda _room_id, _sources: None,
        approval_ended=lambda _ended: None,
    )
    return bot


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
async def test_a_redaction_admitted_during_an_interrupted_reply_releases_its_source(
    journal_store: EventJournalStore,
) -> None:
    """Admission deletes the source and retires its generation; its callback only ends what the deletion ended."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store)
    bot = _redaction_callback_bot(journal_store)
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
    claim = await store.claim(turn)
    assert isinstance(claim, TurnClaim)

    async def generate() -> None:
        started.set()
        await asyncio.Event().wait()

    runner = ResponseRunner(deps=MagicMock())
    response = runner.inbox.track_inbox_response(
        generate(),
        name="inbox_response:deleted",
        recovery_proof_ready=lambda: store.response_recovery_ready(turn),
        claim=claim,
        source_event_ids=turn.source_event_ids,
        room_id=ROOM,
    )
    await started.wait()
    try:
        await admit_dispatch_event(dispatcher, room, _redaction(), EventKind.REDACTION, EventClass.ACTIONABLE)
        assert not await principal.is_pending(SOURCE)
        assert await principal.is_pending(REDACTION)
        proof = runner.inbox._inbox_response_tasks[response].recovery_proof_ready
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
        redacted = await store.get_turn_record(SOURCE)
        assert redacted is not None
        assert not redacted.completed
        assert await store.deleted_sources(redacted) == {SOURCE}
        assert not redacted.source_event_prompts
        assert await dispatcher.drain_once() == 1
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
    """Reopened history is sanitized before the survivor's reply, whichever callback runs first."""
    principal = journal_store.principal("agent@alice")
    target = MessageTarget.resolve(ROOM, "$thread", "$survivor")
    scope = HistoryScope(kind="agent", scope_id="agent")
    store = await _store(journal_store)

    def storage_factory(*_args: object, **_kwargs: object) -> ConversationStore:
        return create_state_storage("agent", tmp_path)

    store.deps.state_writer.create_storage.side_effect = storage_factory
    store.deps.state_writer.history_scope.return_value = scope
    original_run = RunRecord(
        run_id="interrupted",
        agent_id="agent",
        session_id=target.session_id,
        messages=[Message(role="user", content="deleted prompt")],
        metadata={"matrix_event_id": SOURCE},
    )
    session = ConversationSession(kind="agent", session_id=target.session_id, owner_id="agent", runs=[original_run])
    storage = storage_factory()
    seed_session(storage, session)
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
    pending = await store.record_pending_turn(turn)
    assert pending is not None
    stored = pending.record
    assert stored.turn_key is not None
    await admit_dispatch_event(initial_dispatcher, room, _redaction(), EventKind.REDACTION, EventClass.ACTIONABLE)
    assert await store.response_recovery_ready(turn)
    reopened = TurnStore(store.deps)
    await reopened.warm()
    bot = _redaction_callback_bot(journal_store)
    requests: list[str] = []
    survivor_delivered = asyncio.Event()

    async def survivor(_room: nio.MatrixRoom, event: nio.RoomMessageFormatted) -> TurnDispatchOutcome:
        assert event.event_id == "$survivor"
        preparation = await reopened.prepare_pending_response_source(
            target=target,
            source_event_ids=("$survivor",),
        )
        assert preparation.invalid == ()
        recovered_storage = storage_factory()
        await remove_redacted_history_like_next_response(reopened, target, recovered_storage, scope=scope)
        requests.append(_next_request(recovered_storage, target.session_id, scope, event.body))
        await principal.enqueue_final_answer(
            delivery_id="$survivor",
            room_id=ROOM,
            thread_id="$thread",
            payload={"msgtype": "m.text", "body": "captured"},
            turn_key=stored.turn_key,
        )
        survivor_delivered.set()
        return TurnDispatchOutcome.DEFERRED

    dispatcher = _dispatcher(principal, survivor)
    dispatcher.callbacks = replace(dispatcher.callbacks, on_redaction=bot._on_redaction)
    if callback_first:
        await dispatcher._worker.drain_once()
        assert not await principal.is_pending(REDACTION)
    await dispatcher.drain_once()
    dispatcher.start()
    try:
        await asyncio.wait_for(survivor_delivered.wait(), timeout=5)
    finally:
        await dispatcher.stop()
    assert len(requests) == 1
    assert "keep prompt" in requests[0]
    assert "deleted prompt" not in requests[0]
    assert not await principal.is_pending("$survivor")
    assert not await principal.is_pending(REDACTION)
    assert await principal.load_matrix_delivery(delivery_id="$survivor", stage=DeliveryStage.FINAL) is not None
    redacted = await reopened.get_turn_record(SOURCE)
    assert redacted is not None
    assert SOURCE in await reopened.deleted_sources(redacted)
    assert redacted.source_event_prompts == {"$survivor": "keep prompt"}
    preparation = await reopened.prepare_pending_response_source(
        target=target,
        source_event_ids=("$next",),
    )
    assert preparation.invalid == ()
    storage = storage_factory()
    persisted = storage.load_session(target.session_id, "agent")
    assert persisted is not None
    assert "deleted prompt" not in str([run.messages for run in persisted.runs or []])


@pytest.mark.asyncio
@pytest.mark.parametrize("late_registration", [False, True])
@pytest.mark.parametrize("fault", ["none", "missing_source", "missing_redaction", "wrong_room", "missing_context"])
async def test_a_redaction_proves_recovery_only_for_a_settled_source_in_its_own_room(
    journal_store: EventJournalStore,
    late_registration: bool,
    fault: str,
) -> None:
    """The agent's redaction of a source, in the room the turn names, is what hands a settled source off."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store)
    bot = _redaction_callback_bot(journal_store)
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
    current = await store.get_turn_record(SOURCE)
    assert current is not None
    assert not current.completed
    assert await store.deleted_sources(current) == {SOURCE}
    assert not current.source_event_prompts
    captured = turn
    if fault == "missing_source":
        await journal_store.backend.write(
            lambda transaction: transaction.execute("DELETE FROM journal_events WHERE event_id = ?", (SOURCE,)),
        )
    elif fault == "missing_redaction":
        await journal_store.backend.write(
            lambda transaction: transaction.execute("DELETE FROM event_redactions WHERE event_id = ?", (SOURCE,)),
        )
    elif fault == "wrong_room":
        captured = replace(turn, conversation_target=MessageTarget.resolve("!other:localhost", "$thread", SOURCE))
    elif fault == "missing_context":
        captured = replace(turn, conversation_target=None)
    # History cleanup does not depend on the record's conversation context, so losing it changes nothing.
    assert await store.response_recovery_ready(captured) is (fault in {"none", "missing_context"})


@pytest.mark.asyncio
@pytest.mark.parametrize("uncovered_sibling", [False, True])
async def test_mixed_response_needs_independent_ownership_for_every_source(
    journal_store: EventJournalStore,
    uncovered_sibling: bool,
) -> None:
    """A pending survivor cannot excuse an unrelated settled sibling."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store)
    dispatcher = _dispatcher(principal, MagicMock())
    room = nio.MatrixRoom(ROOM, "@bot:localhost")
    sources = (SOURCE, "$survivor", "$uncovered") if uncovered_sibling else (SOURCE, "$survivor")
    for source in sources:
        await admit_dispatch_event(dispatcher, room, _message(source), EventKind.MESSAGE, EventClass.ACTIONABLE)
    turn = _in_room(TurnRecord.create(sources, completed=False), ROOM)
    await store.record_pending_turn(turn)
    await admit_dispatch_event(dispatcher, room, _redaction(), EventKind.REDACTION, EventClass.ACTIONABLE)
    if uncovered_sibling:
        await principal.settle("$uncovered")
    assert await store.response_recovery_ready(turn) is not uncovered_sibling
    assert await principal.is_pending("$survivor")
    assert await principal.is_pending(REDACTION)


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted_id", [SOURCE, "$alias", "$revision"])
@pytest.mark.parametrize("scope", ["exact", "other_room", "other_login"])
async def test_a_redaction_is_read_for_its_room_by_every_login_of_the_agent(
    journal_store: EventJournalStore,
    tmp_path: Path,
    deleted_id: str,
    scope: str,
) -> None:
    """The batched lookup covers a source, its alias, and its revision in their room, whichever login saw it."""
    principal = journal_store.principal("agent@alice")
    store = await _store_with_storage(journal_store, _seeded_storage(tmp_path))
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
    callback_owner = journal_store.principal("agent@other") if scope == "other_login" else principal
    room_id = "!other:localhost" if scope == "other_room" else ROOM
    await admit_dispatch_event(
        _dispatcher(callback_owner, MagicMock()),
        nio.MatrixRoom(room_id, "@bot:localhost"),
        _redaction(deleted_id),
        EventKind.REDACTION,
        EventClass.CONTEXT_ONLY,
    )
    requested = (*tuple(f"$absent-{index}" for index in range(300)), deleted_id)
    expected = frozenset() if scope == "other_room" else frozenset({deleted_id})
    assert await store.redacted_event_ids(ROOM, requested) == expected
    preparation = await store.prepare_pending_response_source(
        target=target,
        source_event_ids=("$later",),
    )
    assert preparation.invalid == ()
    assert await store.redacted_event_ids(ROOM, (deleted_id,)) == expected
