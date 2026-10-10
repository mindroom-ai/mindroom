"""Reply records persist exactly on both journal backends."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from mindroom import reply_lifecycle as rl
from mindroom import reply_scope
from mindroom.event_journal import (
    DeliveryStage,
    DepartureSource,
    EventKind,
    PrincipalStore,
    replies,
    reply_messages,
    reply_spans,
    turn_records,
)
from mindroom.event_journal.replies import (
    AppliedTransition,
    ApprovalEnded,
    Decide,
    PreparedReplyRow,
    ReplyRowRequest,
    WakeApproval,
)
from mindroom.handled_turns import HandledTurnLedger, TurnRecordCodec
from mindroom.reply_lifecycle import (
    ClaimContext,
    ClaimRequest,
    OwedWrite,
    ReplyState,
    Rollback,
    SpanKind,
    SpanOutcome,
)
from mindroom.reply_presentation import Presentation, encode_presentation
from mindroom.response_sources import ResponseSources
from mindroom.stop import SpanRegistry
from mindroom.tool_system.events import ToolTraceEntry
from mindroom.turn_record import TurnRecord
from tests import test_event_journal_store as journal_tests
from tests.journal_membership_helpers import admit_room_membership
from tests.reply_span_helpers import paused_for_approval, reply_shown_for_approval
from tests.test_event_journal_store import ROOM, admit, text

if TYPE_CHECKING:
    from mindroom.cancellation import TaskCancelSource
    from mindroom.event_journal import EventJournalStore

pytestmark = pytest.mark.asyncio

PRINCIPAL = "agent@alice"


def _request(span_id: str = "span-1", *, reply_id: str = "reply-1", source: str = "$source") -> ClaimRequest:
    return ClaimRequest(
        span_id=span_id,
        delivery_id=source,
        sources=ResponseSources(
            pending_event_ids=(source,),
            logical_source_event_ids=(source,),
            discovery_event_ids=("$alias",),
        ),
        bot_generation="gen-1",
        now_ns=10,
        new_reply_id=reply_id,
        entity_name="agent",
        room_id=ROOM,
        thread_id="$thread",
        membership_epoch=3,
        empty_presentation=encode_presentation(Presentation()),
    )


async def _apply(journal_store: EventJournalStore, transition: rl.Transition) -> AppliedTransition:
    """Write a transition decided in the test, the way the journal layer does inside its transactions."""
    return await journal_store.backend.write(lambda tx: replies.apply(tx, PRINCIPAL, transition))


def _first_claim(**changes: object) -> rl.Transition:
    return rl.claim(
        _request(**changes),  # type: ignore[arg-type]
        ClaimContext(
            reply=None,
            last_span=None,
            current_span=None,
            interactive_span=None,
            durable_write_debt=False,
            active_generation="gen-1",
        ),
    )


async def test_reply_and_span_round_trip_every_field(journal_store: EventJournalStore) -> None:
    """Every reply column and span column restores as written; the approval hold is read, never written."""
    principal = journal_store.principal(PRINCIPAL)
    transition = _first_claim()
    applied = await _apply(journal_store, transition)
    assert applied.post_commit == ()
    assert transition.reply is not None
    assert transition.claimed is not None
    reply = replace(
        transition.reply,
        event_id="$reply",
        possibly_shown='{"shown":true}',
        confirmed_seq=3,
        placeholder_only=True,
        stop_receipt_order=9,
        stop_applied_receipt_order=8,
        stop_button_event_id="$button",
        redaction_pending=("$old",),
        owed_write=OwedWrite("span-1", rl.NoteKind.ERROR, "boom"),
        reply_sequence=4,
        revision=7,
        hold_key='{"recipient":"agent"}',
    )
    await _apply(journal_store, rl.Transition(outcome=rl.Outcome.APPLIED, reply=reply))

    assert await principal.replies.load("reply-1") == reply
    assert await principal.replies.for_event("$reply") == reply
    span = await principal.replies.span("span-1")
    assert span == transition.claimed
    assert span is not None
    assert span.sources == ResponseSources(
        pending_event_ids=("$source",),
        logical_source_event_ids=("$source",),
        discovery_event_ids=("$alias",),
    )


async def test_span_outcome_is_write_once_and_rollback_round_trips(journal_store: EventJournalStore) -> None:
    """A span's outcome can be written once; a conflicting second outcome is refused."""
    principal = journal_store.principal(PRINCIPAL)
    transition = _first_claim()
    await _apply(journal_store, transition)
    assert transition.claimed is not None
    rollback = Rollback(presentation="old", state=ReplyState.COMPLETED)
    regen = replace(transition.claimed, span_id="span-2", kind=SpanKind.REGENERATION, rollback=rollback)
    await journal_store.backend.write(lambda tx: reply_spans.save(tx, PRINCIPAL, regen))
    assert await principal.replies.span("span-2") == regen

    ended = replace(transition.claimed, outcome=SpanOutcome.COMPLETED)
    await journal_store.backend.write(lambda tx: reply_spans.save(tx, PRINCIPAL, ended))
    await journal_store.backend.write(lambda tx: reply_spans.save(tx, PRINCIPAL, ended))
    assert await principal.replies.span("span-1") == ended
    with pytest.raises(RuntimeError, match="already ended"):
        await journal_store.backend.write(
            lambda tx: reply_spans.save(tx, PRINCIPAL, replace(ended, outcome=SpanOutcome.FAILED)),
        )


async def test_lookups_by_sources_and_delivery_prefer_the_newest(journal_store: EventJournalStore) -> None:
    """A source finds its newest reply; a delivery id names its latest span."""
    principal = journal_store.principal(PRINCIPAL)
    first = _first_claim()
    await _apply(journal_store, first)
    second = rl.claim(
        replace(_request("span-2", reply_id="reply-2"), now_ns=20),
        ClaimContext(None, None, None, None, durable_write_debt=False, active_generation="gen-1"),
    )
    await _apply(journal_store, second)

    found = await principal.replies.for_sources(("$other", "$source"))
    assert found is not None
    assert found.reply_id == "reply-2"
    assert await principal.replies.for_sources(("$alias",)) is None
    latest = await journal_store.backend.read(lambda tx: reply_spans.latest_for_delivery(tx, PRINCIPAL, "$source"))
    assert latest is not None
    assert latest.span_id == "span-2"
    assert [span.span_id for span in await principal.replies.spans("reply-1")] == ["span-1"]


async def test_work_and_open_reply_queries(journal_store: EventJournalStore) -> None:
    """Replies are found by owed work, and open ones across principals."""
    transition = _first_claim()
    await _apply(journal_store, transition)
    assert transition.reply is not None
    opened = await journal_store.backend.read(reply_messages.open_replies)
    assert [(principal, reply.reply_id) for principal, reply in opened] == [(PRINCIPAL, "reply-1")]
    assert await journal_store.backend.read(lambda tx: reply_messages.with_pending_work(tx, PRINCIPAL)) == ()
    owed = replace(transition.reply, owed_write=OwedWrite("span-1", rl.NoteKind.RESTART))
    await _apply(journal_store, rl.Transition(outcome=rl.Outcome.APPLIED, reply=owed))
    pending = await journal_store.backend.read(lambda tx: reply_messages.with_pending_work(tx, PRINCIPAL))
    assert [reply.reply_id for reply in pending] == ["reply-1"]
    other = journal_store.principal("agent@bob")
    assert await other.replies.load("reply-1") is None


async def test_pending_stops_keep_the_newest_and_are_taken_once(journal_store: EventJournalStore) -> None:
    """A Stop on an unbound event is stored, newer Stops replace older ones, and binding takes it."""

    async def record(order: int) -> bool:
        return await journal_store.backend.write(
            lambda tx: reply_messages.record_pending_stop(
                tx,
                PRINCIPAL,
                target_event_id="$created",
                receipt_order=order,
                room_id=ROOM,
                now_ns=1,
            ),
        )

    assert await record(5)
    assert not await record(4)
    assert await record(6)
    assert (
        await journal_store.backend.write(
            lambda tx: reply_messages.take_pending_stop(tx, PRINCIPAL, "$created", "!elsewhere:localhost"),
        )
        is None
    )
    taken = await journal_store.backend.write(
        lambda tx: reply_messages.take_pending_stop(tx, PRINCIPAL, "$created", ROOM),
    )
    assert taken == 6
    assert (
        await journal_store.backend.write(lambda tx: reply_messages.take_pending_stop(tx, PRINCIPAL, "$created", ROOM))
        is None
    )


async def test_generation_is_replaced_by_each_bot_instance(journal_store: EventJournalStore) -> None:
    """Each bot instance becomes the owner of its principal's replies."""
    replies = journal_store.principal(PRINCIPAL).replies
    assert await replies.active_generation() is None
    await replies.write_generation("gen-1")
    await replies.write_generation("gen-2")
    assert await replies.active_generation() == "gen-2"
    assert await journal_store.principal("agent@bob").replies.active_generation() is None


async def test_applying_a_terminal_transition_settles_the_span_sources(journal_store: EventJournalStore) -> None:
    """A transition's settlement commits in the same transaction as the reply change."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    claim = _first_claim()
    await _apply(journal_store, claim)
    assert claim.reply is not None
    assert claim.claimed is not None
    assert await principal.is_pending("$source")

    finished = rl.finish(
        claim.reply,
        claim.claimed,
        rl.TerminalWrite(shown="answer", prepared_revision=claim.reply.revision, state=ReplyState.COMPLETED),
        now_ns=30,
    )
    await _apply(journal_store, finished)

    assert not await principal.is_pending("$source")
    stored = await principal.replies.load("reply-1")
    assert stored is not None
    assert stored.state is ReplyState.COMPLETED
    span = await principal.replies.span("span-1")
    assert span is not None
    assert span.outcome is SpanOutcome.COMPLETED


async def _pending_turn(journal_store: EventJournalStore, *source_ids: str) -> None:
    pending = TurnRecord.create(list(source_ids), completed=False)
    await journal_store.backend.write(
        lambda tx: turn_records.write_record(
            tx,
            "agent",
            index_event_ids=pending.indexed_event_ids,
            anchor_event_id=pending.anchor_event_id,
            record_json=json.dumps(TurnRecordCodec._to_ledger_record(pending)),
        ),
    )


async def _answered(journal_store: EventJournalStore, source_id: str) -> bool:
    record = await journal_store.backend.read(lambda tx: turn_records.load_record(tx, "agent", source_id))
    assert record is not None
    return record.completed


async def test_settling_a_spans_sources_records_its_turn_answered(journal_store: EventJournalStore) -> None:
    """The reply rule that settles a turn's sources is what records the turn answered, in the same transaction."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    pending = TurnRecord.create(["$source"], completed=False)
    await journal_store.backend.write(
        lambda tx: turn_records.write_record(
            tx,
            "agent",
            index_event_ids=pending.indexed_event_ids,
            anchor_event_id="$source",
            record_json=json.dumps(TurnRecordCodec._to_ledger_record(pending)),
        ),
    )
    claim = _first_claim()
    await _apply(journal_store, claim)
    assert claim.reply is not None
    assert claim.claimed is not None

    applied = await _apply(
        journal_store,
        rl.finish(
            claim.reply,
            claim.claimed,
            rl.TerminalWrite(shown="answer", prepared_revision=claim.reply.revision, state=ReplyState.COMPLETED),
            now_ns=30,
        ),
    )

    record = await journal_store.backend.read(lambda tx: turn_records.load_record(tx, "agent", "$source"))
    assert record is not None
    assert record.completed
    assert record.response_event_id is None
    assert [effect.record for effect in applied.post_commit if isinstance(effect, replies.TurnCompleted)] == [record]

    # A ledger write derived before the settlement arrives late; the turn stays answered.
    await journal_store.backend.write(
        lambda tx: turn_records.write_record(
            tx,
            "agent",
            index_event_ids=pending.indexed_event_ids,
            anchor_event_id="$source",
            record_json=json.dumps(TurnRecordCodec._to_ledger_record(pending)),
        ),
    )
    late = await journal_store.backend.read(lambda tx: turn_records.load_record(tx, "agent", "$source"))
    assert late is not None
    assert late.completed


async def test_a_late_ledger_write_keeps_the_edit_a_regeneration_answered(journal_store: EventJournalStore) -> None:
    """A cached write derived before a regeneration answered cannot put the older prompt and revision back."""

    async def write(record: TurnRecord) -> None:
        assert record.anchor_event_id is not None
        anchor = record.anchor_event_id
        await journal_store.backend.write(
            lambda tx: turn_records.write_record(
                tx,
                "agent",
                index_event_ids=record.indexed_event_ids,
                anchor_event_id=anchor,
                record_json=json.dumps(TurnRecordCodec._to_ledger_record(record)),
            ),
        )

    answered = TurnRecord.create(
        ["$source"],
        completed=True,
        source_event_prompts={"$source": "edited"},
        source_event_revisions={"$source": (20, "$edit")},
        timestamp=1.0,
    )
    await write(answered)
    # Derived from the turn as the first answer left it, and stamped later.
    stale = TurnRecord.create(
        ["$source"],
        completed=True,
        source_event_prompts={"$source": "original"},
        timestamp=2.0,
    )
    await write(stale)

    record = await journal_store.backend.read(lambda tx: turn_records.load_record(tx, "agent", "$source"))
    assert record is not None
    assert record.completed
    assert record.source_event_revisions == {"$source": (20, "$edit")}
    assert record.source_event_prompts == {"$source": "edited"}


async def test_post_commit_effects_are_returned(journal_store: EventJournalStore) -> None:
    """Cancellation of a live span is left for after the commit."""
    principal = journal_store.principal(PRINCIPAL)
    claim = _first_claim()
    await _apply(journal_store, claim)
    assert claim.reply is not None
    stop = rl.stop(claim.reply, claim.claimed, rl.StopFacts(3, span_live=True), now_ns=40)
    applied = await _apply(journal_store, stop)
    assert applied.post_commit == (rl.CancelSpan("span-1", by_stop=True),)
    stored = await principal.replies.load("reply-1")
    assert stored is not None
    assert stored.stop_receipt_order == 3


# --- reply rows -------------------------------------------------------------


async def _claimed(principal: PrincipalStore) -> tuple[rl.Reply, rl.Span]:
    await admit(principal, "$source")
    await principal.replies.write_generation("gen-1")
    claim = (await principal.replies.claim(_request())).transition
    assert claim.reply is not None
    assert claim.claimed is not None
    assert claim.reply.membership_epoch == await principal.membership_epoch(ROOM)
    return claim.reply, claim.claimed


def _finish(write_state: ReplyState = ReplyState.COMPLETED, *, revision: int = 0) -> Decide:
    def decide(reply: rl.Reply, span: rl.Span) -> rl.Transition:
        return rl.finish(
            reply,
            span,
            rl.TerminalWrite(shown="answer", prepared_revision=revision, state=write_state),
            now_ns=50,
        )

    return decide


async def test_terminal_row_settles_sources_and_its_ack_binds_the_reply(journal_store: EventJournalStore) -> None:
    """A finished span's FINAL is a sequenced reply row; its acknowledgement binds the event."""
    principal = journal_store.principal(PRINCIPAL)
    reply, span = await _claimed(principal)
    enqueued = await principal.enqueue_reply_row(
        ReplyRowRequest(
            reply_id=reply.reply_id,
            span_id=span.span_id,
            decide=_finish(),
        ),
        PreparedReplyRow(payload={"body": "answer"}),
    )
    assert enqueued is not None
    assert enqueued.delivery_id == "$source"
    assert enqueued.stage is rl.WriteStage.FINAL
    assert enqueued.settled_event_ids == ("$source",)
    assert not await principal.is_pending("$source")
    delivery = await principal.load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    assert delivery is not None
    assert (delivery.reply_id, delivery.span_id, delivery.reply_sequence) == ("reply-1", "span-1", 1)
    assert await principal.replies.has_unresolved_rows("reply-1")

    assert await principal.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    acknowledged = await principal.acknowledge_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        event_id="$answer",
        delivered_projections=(),
    )
    assert acknowledged.settled_event_id == "$answer"
    stored = await principal.replies.load("reply-1")
    assert stored is not None
    assert stored.state is ReplyState.COMPLETED
    assert stored.event_id == "$answer"
    assert stored.confirmed_seq == 1
    assert not await principal.replies.has_unresolved_rows("reply-1")


async def test_a_stop_committed_after_rendering_writes_nothing(journal_store: EventJournalStore) -> None:
    """A payload rendered for an older revision is refused with Recompute and settles nothing."""
    principal = journal_store.principal(PRINCIPAL)
    reply, span = await _claimed(principal)
    await _apply(journal_store, rl.stop(reply, span, rl.StopFacts(4, span_live=True), now_ns=40))
    enqueued = await principal.enqueue_reply_row(
        ReplyRowRequest(
            reply_id=reply.reply_id,
            span_id=span.span_id,
            decide=_finish(revision=0),
        ),
        PreparedReplyRow(payload={"body": "answer"}),
    )
    assert enqueued is not None
    assert enqueued.transition.outcome is rl.Outcome.RECOMPUTE
    assert enqueued.delivery_id is None
    assert await principal.is_pending("$source")
    assert await principal.load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL) is None


async def test_edit_row_waits_for_the_create_and_targets_its_event(journal_store: EventJournalStore) -> None:
    """A non-terminal row enqueued before the create is acknowledged edits the event that create binds."""
    principal = journal_store.principal(PRINCIPAL)
    reply, span = await _claimed(principal)
    initial = await principal.enqueue_reply_row(
        ReplyRowRequest(
            reply_id=reply.reply_id,
            span_id=span.span_id,
            decide=lambda reply, span: rl.enqueue_initial(
                reply,
                span,
                shown="ph",
                placeholder_only=True,
                prepared_revision=reply.revision,
                now_ns=60,
            ),
            placeholder_only=True,
        ),
        PreparedReplyRow(payload={"body": "Thinking..."}),
    )
    assert initial is not None
    assert initial.stage is rl.WriteStage.INITIAL
    pause = await principal.enqueue_reply_row(
        ReplyRowRequest(
            reply_id=reply.reply_id,
            span_id=span.span_id,
            decide=lambda reply, span: rl.fail(
                reply,
                span,
                rl.TerminalWrite(shown="note", prepared_revision=reply.revision, state=ReplyState.ACTIVE),
                now_ns=70,
            ),
        ),
        PreparedReplyRow(payload={"body": "note"}),
    )
    assert pause is not None
    assert pause.stage is rl.WriteStage.EDIT
    assert pause.delivery_id == "$source:edit:2"
    assert await principal.is_pending("$source")
    assert await principal.unresolved_reply_rows("reply-1") == (
        ("$source", DeliveryStage.INITIAL),
        ("$source:edit:2", DeliveryStage.EDIT),
    )
    assert await principal.claim_matrix_delivery(delivery_id="$source:edit:2", stage=DeliveryStage.EDIT) is None

    assert await principal.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.INITIAL)
    await principal.acknowledge_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.INITIAL,
        event_id="$reply",
        delivered_projections=(),
    )
    claimed_edit = await principal.claim_matrix_delivery(delivery_id="$source:edit:2", stage=DeliveryStage.EDIT)
    assert claimed_edit is not None
    assert claimed_edit.edits_event_id == "$reply"
    stored = await principal.replies.load("reply-1")
    assert stored is not None
    assert stored.event_id == "$reply"
    assert stored.placeholder_only


async def test_a_terminal_row_waiting_for_the_create_takes_its_target_from_the_reply(
    journal_store: EventJournalStore,
) -> None:
    """The create's acknowledgement binds the reply's event; the terminal row reads it from the reply when claimed."""
    principal = journal_store.principal(PRINCIPAL)
    reply, span = await _claimed(principal)
    await principal.enqueue_reply_row(
        ReplyRowRequest(
            reply_id=reply.reply_id,
            span_id=span.span_id,
            decide=lambda reply, span: rl.enqueue_initial(
                reply,
                span,
                shown="ph",
                placeholder_only=True,
                prepared_revision=reply.revision,
                now_ns=60,
            ),
            placeholder_only=True,
        ),
        PreparedReplyRow(payload={"body": "Thinking..."}),
    )
    assert await principal.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.INITIAL)
    await principal.enqueue_reply_row(
        ReplyRowRequest(
            reply_id=reply.reply_id,
            span_id=span.span_id,
            decide=_finish(),
        ),
        PreparedReplyRow(payload={"body": "answer"}),
    )
    await principal.acknowledge_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.INITIAL,
        event_id="$reply",
        delivered_projections=(),
    )

    waiting = await principal.load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    assert waiting is not None
    assert waiting.edits_event_id is None
    claimed = await principal.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    assert claimed is not None
    assert claimed.edits_event_id == "$reply"


async def test_permanent_failure_of_a_terminal_row_applies_its_rule(journal_store: EventJournalStore) -> None:
    """A terminal row Matrix refuses for good leaves the reply failed, with the span's outcome kept."""
    principal = journal_store.principal(PRINCIPAL)
    reply, span = await _claimed(principal)
    await _apply(journal_store, rl.Transition(outcome=rl.Outcome.APPLIED, reply=replace(reply, event_id="$reply")))
    enqueued = await principal.enqueue_reply_row(
        ReplyRowRequest(
            reply_id=reply.reply_id,
            span_id=span.span_id,
            decide=_finish(),
        ),
        PreparedReplyRow(payload={"body": "answer"}),
    )
    assert enqueued is not None
    assert await principal.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    await principal.record_permanent_matrix_delivery_failure(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        reason="too large",
    )
    await principal.record_permanent_matrix_delivery_failure(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        reason="too large",
    )
    stored = await principal.replies.load("reply-1")
    assert stored is not None
    assert stored.state is ReplyState.FAILED
    span_after = await principal.replies.span("span-1")
    assert span_after is not None
    assert span_after.outcome is SpanOutcome.COMPLETED


async def test_the_note_of_a_refused_only_create_is_sent_as_the_replys_message(
    journal_store: EventJournalStore,
) -> None:
    """A reply whose only create Matrix refused for good shows its delivery-failed note as a message of its own."""
    principal = journal_store.principal(PRINCIPAL)
    reply, span = await _claimed(principal)
    enqueued = await principal.enqueue_reply_row(
        ReplyRowRequest(
            reply_id=reply.reply_id,
            span_id=span.span_id,
            decide=_finish(),
        ),
        PreparedReplyRow(payload={"body": "answer"}),
    )
    assert enqueued is not None
    assert await principal.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    await principal.record_permanent_matrix_delivery_failure(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        reason="too large",
    )
    failed = await principal.replies.load("reply-1")
    assert failed is not None
    assert failed.event_id is None
    assert failed.owed_write is not None

    note = await principal.enqueue_reply_row(
        ReplyRowRequest(
            reply_id="reply-1",
            span_id=failed.owed_write.span_id,
            decide=lambda current, owner: rl.flush_owed_write(
                current,
                owner,
                shown="note",
                prepared_revision=failed.revision,
                span_has_final=True,
                now_ns=60,
            ),
        ),
        PreparedReplyRow(payload={"body": "note"}),
    )
    assert note is not None
    assert note.stage is rl.WriteStage.EDIT
    claimed = await principal.claim_matrix_delivery(delivery_id=note.delivery_id, stage=DeliveryStage.EDIT)
    assert claimed is not None, "the note waits for an event no row will ever create"
    assert claimed.edits_event_id is None
    await principal.acknowledge_matrix_delivery(
        delivery_id=note.delivery_id,
        stage=DeliveryStage.EDIT,
        event_id="$note",
        delivered_projections=(),
    )
    stored = await principal.replies.load("reply-1")
    assert stored is not None
    assert stored.state is ReplyState.FAILED
    assert stored.event_id == "$note"
    assert stored.owed_write is None
    assert not await principal.replies.has_unresolved_rows("reply-1")


async def test_a_tool_starts_only_while_its_span_runs_without_a_recorded_stop(journal_store: EventJournalStore) -> None:
    """The start record admits a call only for a running span; a Stop committed first refuses it, unrecorded."""
    principal = journal_store.principal(PRINCIPAL)
    reply, span = await _claimed(principal)
    assert await principal.replies.start_tool_call(span_id=span.span_id, call_id="before", entry_json="{}", now_ns=20)

    await _apply(journal_store, rl.Transition(outcome=rl.Outcome.APPLIED, reply=replace(reply, stop_receipt_order=1)))
    assert not await principal.replies.start_tool_call(
        span_id=span.span_id,
        call_id="after-stop",
        entry_json="{}",
        now_ns=30,
    )
    assert not await principal.replies.start_tool_call(span_id="no-such-span", call_id="x", entry_json="{}", now_ns=30)
    assert await principal.replies.tool_calls((span.span_id,)) == ("{}",)


async def test_a_replay_lists_the_tool_calls_of_an_attempt_a_superseded_one_took_over(
    journal_store: EventJournalStore,
) -> None:
    """A superseded attempt ran no model, so the next replay still lists what the attempt before it ran."""
    principal = journal_store.principal(PRINCIPAL)
    reply, first = await _claimed(principal)
    runtime = reply_scope.ReplyRuntime(
        store=principal,
        entity_name="agent",
        generation="gen-1",
        retry_sources=lambda _room_id, _sources: None,
        complete_turn=AsyncMock(),
        hold_conversation=lambda _continuation: None,
        approval_ended=lambda _ended: None,
        settle_debt=lambda _reply_id: None,
    )
    await principal.replies.record_tool_call(
        span_id=first.span_id,
        call_id="call-1",
        entry_json=reply_scope._entry_json(
            ToolTraceEntry(type="tool_call_completed", tool_name="counter", args_preview="{}", result_preview="1"),
        ),
        now_ns=20,
    )
    lost = replace(first, outcome=SpanOutcome.LOST)
    superseded = replace(
        first,
        span_id="span-2",
        kind=SpanKind.REPLAY,
        claimed_at_ns=40,
        outcome=SpanOutcome.SUPERSEDED,
    )
    current = replace(first, span_id="span-3", kind=SpanKind.REPLAY, claimed_at_ns=60)
    await _apply(
        journal_store,
        rl.Transition(outcome=rl.Outcome.APPLIED, reply=reply, spans=(lost, superseded, current)),
    )

    handle = reply_scope.SpanHandle(runtime=runtime, span=current, reply=reply, base=Presentation())
    calls = await runtime.interrupted_tool_calls(handle)

    assert [call.tool_name for call in calls] == ["counter"]


async def test_a_regeneration_lists_only_the_tool_calls_of_its_own_edits_earlier_attempts(
    journal_store: EventJournalStore,
) -> None:
    """An edit redoes the turn, so its regeneration is told only about the attempts of that edit a restart cut short."""
    principal = journal_store.principal(PRINCIPAL)
    reply, first = await _claimed(principal)
    runtime = reply_scope.ReplyRuntime(
        store=principal,
        entity_name="agent",
        generation="gen-1",
        retry_sources=lambda _room_id, _sources: None,
        complete_turn=AsyncMock(),
        hold_conversation=lambda _continuation: None,
        approval_ended=lambda _ended: None,
        settle_debt=lambda _reply_id: None,
    )
    lost_regeneration = replace(
        first,
        span_id="span-2",
        kind=SpanKind.REGENERATION,
        delivery_id="$edit",
        claimed_at_ns=40,
        outcome=SpanOutcome.LOST,
    )
    for span_id, tool_name in ((first.span_id, "before_edit"), (lost_regeneration.span_id, "edit_attempt")):
        await principal.replies.record_tool_call(
            span_id=span_id,
            call_id=tool_name,
            entry_json=reply_scope._entry_json(
                ToolTraceEntry(type="tool_call_completed", tool_name=tool_name, args_preview="{}", result_preview="1"),
            ),
            now_ns=20,
        )
    paused = replace(first, outcome=SpanOutcome.PAUSED)
    current = replace(lost_regeneration, span_id="span-3", claimed_at_ns=60, outcome=None)
    await _apply(
        journal_store,
        rl.Transition(outcome=rl.Outcome.APPLIED, reply=reply, spans=(paused, lost_regeneration, current)),
    )

    handle = reply_scope.SpanHandle(runtime=runtime, span=current, reply=reply, base=Presentation())
    calls = await runtime.interrupted_tool_calls(handle)

    assert [call.tool_name for call in calls] == ["edit_attempt"]


async def test_a_departure_ends_the_rooms_replies_and_refuses_their_rows(journal_store: EventJournalStore) -> None:
    """Leaving a room ends its running replies gone with their spans released; a later row changes nothing."""
    principal = journal_store.principal(PRINCIPAL)
    reply, span = await _claimed(principal)
    await admit_room_membership(principal, ROOM, "leave", source=DepartureSource.LOCAL)
    departed = await principal.replies.load("reply-1")
    assert departed is not None
    assert departed.state is ReplyState.GONE
    assert departed.current_span_id is None
    released = await principal.replies.span(span.span_id)
    assert released is not None
    assert released.outcome is SpanOutcome.RELEASED
    enqueued = await principal.enqueue_reply_row(
        ReplyRowRequest(
            reply_id=reply.reply_id,
            span_id=span.span_id,
            decide=_finish(),
        ),
        PreparedReplyRow(payload={"body": "answer"}),
    )
    # The released span's write is stale, so no row is recorded.
    assert enqueued is not None
    assert enqueued.applied.transition.outcome is rl.Outcome.STALE
    assert enqueued.delivery_id is None
    assert await principal.replies.load("reply-1") == departed


async def _delete(principal: PrincipalStore, event_id: str) -> None:
    await admit(principal, f"$redaction-{event_id}", redacts=event_id, kind=EventKind.REDACTION, content={})


async def test_deleting_every_source_ends_the_reply_and_its_span(journal_store: EventJournalStore) -> None:
    """The tombstone of a reply's last remaining source ends it gone in the same commit, its span cancelled."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$first")
    await admit(principal, "$second")
    pending = TurnRecord.create(["$first", "$second"], completed=False)
    await journal_store.backend.write(
        lambda tx: turn_records.write_record(
            tx,
            "agent",
            index_event_ids=pending.indexed_event_ids,
            anchor_event_id=pending.anchor_event_id,
            record_json=json.dumps(TurnRecordCodec._to_ledger_record(pending)),
        ),
    )
    await principal.replies.write_generation("gen-1")
    request = replace(
        _request(source="$first"),
        sources=ResponseSources(
            pending_event_ids=("$first", "$second"),
            logical_source_event_ids=("$first", "$second"),
        ),
    )
    span = (await principal.replies.claim(request)).transition.claimed
    assert span is not None

    await _delete(principal, "$first")
    running = await principal.replies.load("reply-1")
    assert running is not None
    assert running.state is ReplyState.ACTIVE
    assert running.current_span_id == span.span_id
    assert await principal.replies.take_deletion_endings() == ()

    await _delete(principal, "$second")
    gone = await principal.replies.load("reply-1")
    assert gone is not None
    assert gone.state is ReplyState.GONE
    assert gone.current_span_id is None
    cancelled = await principal.replies.span(span.span_id)
    assert cancelled is not None
    assert cancelled.outcome is SpanOutcome.CANCELLED
    assert not await principal.is_pending("$first")
    assert not await principal.is_pending("$second")
    # The deleted sources settle through the reply's settlement, and nothing answered their turn.
    record = await journal_store.backend.read(lambda tx: turn_records.load_record(tx, "agent", "$first"))
    assert record is not None
    assert not record.completed
    # The bot cancels exactly the span the deletion ended, once.
    assert await principal.replies.take_deletion_endings() == (reply_messages.DeletionEnding("reply-1", span.span_id),)
    assert await principal.replies.take_deletion_endings() == ()


async def _answered_and_regenerating(principal: PrincipalStore) -> rl.Span:
    """Answer ``$source``, then claim an edit's regeneration of it that has written nothing yet."""
    reply, span = await _claimed(principal)
    await principal.enqueue_reply_row(
        ReplyRowRequest(
            reply_id=reply.reply_id,
            span_id=span.span_id,
            decide=_finish(),
        ),
        PreparedReplyRow(payload={"body": "answer"}),
    )
    assert await principal.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    await principal.acknowledge_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        event_id="$answer",
        delivered_projections=(),
    )
    await admit(principal, "$edit")
    regeneration = replace(
        _request("span-2", source="$edit"),
        sources=ResponseSources(pending_event_ids=("$edit",), logical_source_event_ids=("$source",)),
        driving_edit_id="$edit",
    )
    claimed = (await principal.replies.claim(regeneration, existing_event_id="$answer")).transition
    assert claimed.claimed is not None
    return claimed.claimed


async def test_a_regeneration_keeps_the_membership_its_edit_was_admitted_in(journal_store: EventJournalStore) -> None:
    """After the bot left and rejoined, the regenerated reply's records name the membership its rows now belong to."""
    principal = journal_store.principal(PRINCIPAL)
    reply, span = await _claimed(principal)
    await principal.enqueue_reply_row(
        ReplyRowRequest(
            reply_id=reply.reply_id,
            span_id=span.span_id,
            decide=_finish(),
        ),
        PreparedReplyRow(payload={"body": "answer"}),
    )
    assert await principal.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    await principal.acknowledge_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        event_id="$answer",
        delivered_projections=(),
    )
    await admit_room_membership(principal, ROOM, "leave", source=DepartureSource.LOCAL)
    rejoined = await admit_room_membership(principal, ROOM, "join")
    answered = await principal.replies.load("reply-1")
    assert answered is not None
    assert answered.state is ReplyState.COMPLETED
    assert answered.membership_epoch != rejoined
    await admit(principal, "$edit")
    regeneration = replace(
        _request("span-2", reply_id="reply-new", source="$edit"),
        sources=ResponseSources(pending_event_ids=("$edit",), logical_source_event_ids=("$source",)),
        driving_edit_id="$edit",
    )
    claimed = (await principal.replies.claim(regeneration, existing_event_id="$answer")).transition
    assert claimed.claimed is not None
    assert claimed.claimed.reply_id == "reply-1"
    stored = await principal.replies.load("reply-1")
    assert stored is not None
    assert stored.membership_epoch == rejoined


async def test_deleting_the_source_of_a_regeneration_a_restart_left_keeps_the_answer(
    journal_store: EventJournalStore,
) -> None:
    """An edit regeneration a restart stopped before it wrote anything still leaves the answer it would replace."""
    principal = journal_store.principal(PRINCIPAL)
    await _answered_and_regenerating(principal)
    answered = await principal.replies.load("reply-1")
    assert answered is not None
    await principal.replies.write_generation("gen-2")
    assert len(await principal.replies.owner_lost("gen-2", now_ns=70)) == 1

    await _delete(principal, "$source")

    kept = await principal.replies.load("reply-1")
    assert kept is not None
    assert kept.state is ReplyState.COMPLETED
    assert kept.presentation == answered.presentation
    assert kept.redaction_pending == ()
    assert not await principal.is_pending("$edit")
    # No span runs for it any more.
    assert await principal.replies.take_deletion_endings() == (reply_messages.DeletionEnding("reply-1", None),)


async def test_deleting_a_running_regenerations_source_cancels_exactly_its_span(
    journal_store: EventJournalStore,
) -> None:
    """The restored answer stands, and the regeneration still running for it is the span the bot cancels."""
    principal = journal_store.principal(PRINCIPAL)
    regeneration = await _answered_and_regenerating(principal)
    await _delete(principal, "$source")
    kept = await principal.replies.load("reply-1")
    assert kept is not None
    assert kept.state is ReplyState.COMPLETED
    assert await principal.replies.take_deletion_endings() == (
        reply_messages.DeletionEnding("reply-1", regeneration.span_id),
    )


async def test_a_deletion_that_ends_nothing_cancels_nothing(journal_store: EventJournalStore) -> None:
    """A reply an earlier rule already restored keeps whatever it still runs, such as the delivery of its note."""
    principal = journal_store.principal(PRINCIPAL)
    regeneration = await _answered_and_regenerating(principal)
    restored = await principal.replies.decide(
        reply_id="reply-1",
        span_id=regeneration.span_id,
        decide=lambda reply, span: rl.dispatch_failed(reply, span, error_text="setup failed", now_ns=60),
    )
    assert _span_outcome(restored, regeneration.span_id) is SpanOutcome.RESTORED
    await _delete(principal, "$source")
    assert await principal.replies.take_deletion_endings() == ()


class _CancelSpy(SpanRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled: list[str] = []

    def cancel(self, span_id: str, *, cancel_source: TaskCancelSource | None) -> bool:
        assert cancel_source is None
        self.cancelled.append(span_id)
        return False


async def test_a_stop_still_cancels_its_span_when_waking_its_approval_fails(journal_store: EventJournalStore) -> None:
    """A failed read while waking the fenced approval cannot leave the stopped span running or the hold in place."""
    principal = journal_store.principal("agent@alice")
    cancelled: list[tuple[str, str | None]] = []
    ended: list[ApprovalEnded] = []

    class _Spans(SpanRegistry):
        def cancel(self, span_id: str, *, cancel_source: TaskCancelSource | None) -> bool:
            cancelled.append((span_id, cancel_source))
            return True

    runtime = reply_scope.ReplyRuntime(
        store=principal,
        entity_name="agent",
        generation="gen-1",
        retry_sources=lambda _room_id, _sources: None,
        complete_turn=AsyncMock(),
        hold_conversation=lambda _continuation: None,
        approval_ended=ended.append,
        settle_debt=lambda _reply_id: None,
        spans=_Spans(),
    )
    with (
        patch.object(
            PrincipalStore,
            "approval_continuation",
            AsyncMock(side_effect=RuntimeError("journal unavailable")),
        ),
        pytest.raises(RuntimeError, match="journal unavailable"),
    ):
        await runtime.run_effects(
            (WakeApproval("approval-1"), ApprovalEnded("approval-2", "reply-2"), rl.CancelSpan("span-1", by_stop=True)),
        )
    assert cancelled == [("span-1", "user_stop")]
    assert [end.approval_id for end in ended] == ["approval-2"]


@pytest.mark.parametrize("ended_by", ["departure", "deletion"])
async def test_a_stop_button_landing_after_the_reply_ended_is_removed_unless_the_room_was_left(
    journal_store: EventJournalStore,
    ended_by: str,
) -> None:
    """A late button is redacted with the reply's other debt; a room the bot left owes nothing, so it stays."""
    principal = journal_store.principal(PRINCIPAL)
    reply, _span = await _claimed(principal)
    if ended_by == "departure":
        await admit_room_membership(principal, ROOM, "leave", source=DepartureSource.LOCAL)
    else:
        await _delete(principal, "$source")
    ended = await principal.replies.load(reply.reply_id)
    assert ended is not None
    assert ended.state is ReplyState.GONE

    await principal.replies.record_stop_button(reply.reply_id, "$button", now_ns=100)

    late = await principal.replies.load(reply.reply_id)
    assert late is not None
    assert ("$button" in late.redaction_pending) is (ended_by == "deletion")


async def test_a_departure_cancels_a_span_claimed_before_its_task_registers(journal_store: EventJournalStore) -> None:
    """Leaving the room between a span's claim and its task's registration cancels the task as it registers."""
    principal = journal_store.principal(PRINCIPAL)
    _reply, span = await _claimed(principal)
    runtime = reply_scope.ReplyRuntime(
        store=principal,
        entity_name="agent",
        generation="gen-1",
        retry_sources=lambda _room_id, _sources: None,
        complete_turn=AsyncMock(),
        hold_conversation=lambda _continuation: None,
        approval_ended=lambda _ended: None,
        settle_debt=lambda _reply_id: None,
    )
    runtime.spans.expect(span.span_id)
    await admit_room_membership(principal, ROOM, "leave", source=DepartureSource.LOCAL)

    await runtime.departed(ROOM)
    task = asyncio.create_task(asyncio.Event().wait())
    runtime.spans.register(span.span_id, task)

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_bot_cancels_the_spans_deletions_ended_and_a_restart_drops_them(
    journal_store: EventJournalStore,
) -> None:
    """Each recorded ending is taken once; a new bot instance runs none of the spans an older one recorded."""
    principal = journal_store.principal(PRINCIPAL)
    regeneration = await _answered_and_regenerating(principal)
    spans = _CancelSpy()
    runtime = reply_scope.ReplyRuntime(
        store=principal,
        entity_name="agent",
        generation="gen-1",
        retry_sources=lambda _room_id, _sources: None,
        complete_turn=AsyncMock(),
        hold_conversation=lambda _continuation: None,
        approval_ended=lambda _ended: None,
        settle_debt=lambda _reply_id: None,
        spans=spans,
    )
    await _delete(principal, "$source")
    assert await runtime.deletions_ended() == ("reply-1",)
    assert spans.cancelled == [regeneration.span_id]
    assert await runtime.deletions_ended() == ()
    ending = reply_messages.DeletionEnding("reply-1", regeneration.span_id)
    await journal_store.backend.write(lambda tx: reply_messages.record_deletion_ending(tx, PRINCIPAL, ending))
    await runtime.start()
    assert await principal.replies.take_deletion_endings() == ()


def _span_outcome(applied: AppliedTransition, span_id: str) -> SpanOutcome | None:
    return next(span.outcome for span in applied.transition.spans if span.span_id == span_id)


async def test_progress_waits_for_the_replys_earlier_durable_writes(journal_store: EventJournalStore) -> None:
    """An unresolved row, such as a pause whose send failed once, holds progress back: sent later, it would win."""
    principal = journal_store.principal(PRINCIPAL)
    reply, span = await _claimed(principal)
    await principal.enqueue_reply_row(
        ReplyRowRequest(
            reply_id=reply.reply_id,
            span_id=span.span_id,
            decide=lambda reply, span: rl.enqueue_initial(
                reply,
                span,
                shown="ph",
                placeholder_only=True,
                prepared_revision=reply.revision,
                now_ns=60,
            ),
            placeholder_only=True,
        ),
        PreparedReplyRow(payload={"body": "Thinking..."}),
    )
    assert await principal.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.INITIAL)

    async def progress() -> rl.Outcome:
        applied = await principal.replies.write_ahead(
            reply_id="reply-1",
            span_id="span-1",
            shown="more",
            previous=None,
            active_generation="gen-1",
            now_ns=80,
        )
        return applied.transition.outcome

    assert await progress() is rl.Outcome.DEFERRED
    await _acknowledge_the_create(principal)
    assert await progress() is rl.Outcome.APPLIED


async def test_a_retired_instance_neither_claims_nor_writes(journal_store: EventJournalStore) -> None:
    """After another instance takes the replies over, the old one's claims and running spans change nothing."""
    principal = journal_store.principal(PRINCIPAL)
    reply, span = await _claimed(principal)
    await principal.replies.write_generation("gen-2")

    await admit(principal, "$other")
    refused = await principal.replies.claim(_request("span-old", reply_id="reply-old", source="$other"))
    assert refused.transition.outcome is rl.Outcome.STALE
    assert await principal.replies.load("reply-old") is None
    enqueued = await principal.enqueue_reply_row(
        ReplyRowRequest(
            reply_id=reply.reply_id,
            span_id=span.span_id,
            decide=_finish(),
        ),
        PreparedReplyRow(payload={"body": "answer"}),
    )
    assert enqueued is not None
    assert enqueued.applied.transition.outcome is rl.Outcome.STALE
    assert enqueued.delivery_id is None
    progress = await principal.replies.decide(
        reply_id=reply.reply_id,
        span_id=span.span_id,
        decide=lambda current, live: rl.write_ahead(
            current,
            live,
            shown="more",
            previous=None,
            active_generation="gen-1",
            durable_write_debt=False,
            now_ns=80,
        ),
    )
    assert progress.transition.outcome is rl.Outcome.STALE
    assert await principal.replies.load(reply.reply_id) == reply


async def test_a_retired_instance_resume_writes_nothing_while_the_owner_may_end_it(
    journal_store: EventJournalStore,
) -> None:
    """A retired instance's own approval resume is refused; the owner's recovery still ends the resume it left."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await principal.replies.write_generation("gen-1")
    claim = _first_claim()
    assert claim.reply is not None
    assert claim.claimed is not None
    resume = replace(claim.claimed, kind=rl.SpanKind.APPROVAL_RESUME, approval_id="approval-1")
    await _apply(journal_store, replace(claim, spans=(resume,), claimed=resume))
    reply = await principal.replies.load(claim.reply.reply_id)
    assert reply is not None
    assert reply.current_span_id == resume.span_id
    await principal.replies.write_generation("gen-2")

    progress = await principal.replies.write_ahead(
        reply_id=reply.reply_id,
        span_id=resume.span_id,
        shown="more",
        previous=None,
        active_generation="gen-1",
        now_ns=80,
    )
    assert progress.transition.outcome is rl.Outcome.STALE
    enqueued = await principal.enqueue_reply_row(
        ReplyRowRequest(
            reply_id=reply.reply_id,
            span_id=resume.span_id,
            decide=_finish(),
            author_generation="gen-1",
        ),
        PreparedReplyRow(payload={"body": "answer"}),
    )
    assert enqueued is not None
    assert enqueued.applied.transition.outcome is rl.Outcome.STALE
    exited = await principal.replies.decide(
        reply_id=reply.reply_id,
        span_id=resume.span_id,
        decide=lambda current, live: rl.release(current, live, now_ns=85),
        author_generation="gen-1",
    )
    assert exited.transition.outcome is rl.Outcome.STALE
    assert await principal.replies.load(reply.reply_id) == reply

    ended = await principal.replies.decide(
        reply_id=reply.reply_id,
        span_id=resume.span_id,
        decide=lambda current, left: rl.span_left_behind(current, left, active_generation="gen-2", now_ns=90),
    )
    assert ended.transition.applied
    lost = await principal.replies.span(resume.span_id)
    assert lost is not None
    assert lost.outcome is rl.SpanOutcome.LOST


async def test_discarding_an_unavailable_owners_approval_ends_the_reply_it_paused(
    journal_store: EventJournalStore,
) -> None:
    """When the owner can never settle its approval, the cleanup that releases its sources ends its paused reply too."""
    alice = journal_store.principal("agent@alice")
    await journal_tests.TestApprovalContinuations.admit_sources(alice)
    continuation = await paused_for_approval(
        alice,
        journal_tests.TestApprovalContinuations.continuation(state="waiting"),
    )
    assert continuation is not None
    assert continuation.span_id is not None
    paused = await alice.replies.span(continuation.span_id)
    assert paused is not None
    assert await alice.request_approval_failure("approval-1", "agent removed", expected_state="waiting") is not None
    router = journal_store.principal("router@alice")
    delivery_id = await router.enqueue_unavailable_approval_notice(
        approval_id="approval-1",
        room_id=ROOM,
        thread_id="$thread",
        payload=text("agent removed"),
    )
    assert delivery_id is not None
    await router.claim_matrix_delivery(delivery_id=delivery_id, stage=DeliveryStage.FINAL)
    await router.acknowledge_matrix_delivery(
        delivery_id=delivery_id,
        stage=DeliveryStage.FINAL,
        event_id="$unavailable",
        delivered_projections=(),
    )

    assert await alice.discard_unavailable_approval_continuation("approval-1", notice_principal_id="router@alice")

    ended = await alice.replies.load(paused.reply_id)
    assert ended is not None
    assert ended.state is ReplyState.FAILED
    assert ended.approval_id is None
    assert not await alice.is_pending("$source-1")


async def test_a_resume_behind_an_unresolved_row_of_its_reply_waits_for_it(journal_store: EventJournalStore) -> None:
    """The resume opens no span, the approval stays ready, and its sources retry once the row resolves."""
    alice = journal_store.principal("agent@alice")
    await journal_tests.TestApprovalContinuations.admit_sources(alice)
    continuation = journal_tests.TestApprovalContinuations.continuation(state="ready")
    span = await reply_shown_for_approval(alice, continuation)
    assert span is not None
    paused = await alice.pause_for_approval(
        continuation,
        ReplyRowRequest(
            reply_id=span.reply_id,
            span_id=span.span_id,
            decide=lambda reply, held: rl.pause(
                reply,
                held,
                rl.PauseWrite(shown=reply.presentation, prepared_revision=reply.revision, stage=rl.WriteStage.EDIT),
                in_place=False,
                now_ns=40,
            ),
        ),
        PreparedReplyRow(payload=text("Waiting for approval")),
    )
    assert paused is not None
    assert paused.delivery_id is not None
    retried: list[tuple[str, tuple[str, ...]]] = []
    runtime = reply_scope.ReplyRuntime(
        store=alice,
        entity_name="agent",
        generation="gen-2",
        retry_sources=lambda room_id, sources: retried.append((room_id, sources)),
        complete_turn=AsyncMock(),
        hold_conversation=lambda _continuation: None,
        approval_ended=lambda _ended: None,
        settle_debt=lambda _reply_id: None,
    )
    await runtime.take_ownership()
    stored = await alice.approval_continuation("approval-1")
    assert stored is not None

    assert await runtime.claim_approval_resume(stored, placeholder="Thinking...") == (None, None)
    waiting = await alice.approval_continuation("approval-1")
    assert waiting is not None
    assert waiting.state == "ready"
    assert retried == []

    assert await alice.claim_matrix_delivery(delivery_id=paused.delivery_id, stage=DeliveryStage.EDIT)
    await alice.acknowledge_matrix_delivery(
        delivery_id=paused.delivery_id,
        stage=DeliveryStage.EDIT,
        event_id="$paused",
        delivered_projections=(),
    )
    runtime.claim_may_proceed(span.reply_id)
    assert retried == [(continuation.room_id, continuation.source_event_ids)]


async def test_a_reply_is_held_by_the_continuation_that_names_its_span(journal_store: EventJournalStore) -> None:
    """The hold is read from the continuation row, so no copy on the reply can drift from it."""
    alice = journal_store.principal("agent@alice")
    await journal_tests.TestApprovalContinuations.admit_sources(alice)
    continuation = await paused_for_approval(
        alice,
        journal_tests.TestApprovalContinuations.continuation(state="waiting"),
    )
    assert continuation is not None
    assert continuation.span_id is not None
    paused = await alice.replies.span(continuation.span_id)
    assert paused is not None
    held = await alice.replies.load(paused.reply_id)
    assert held is not None
    assert held.approval_id == "approval-1"


async def test_an_approval_whose_sources_were_deleted_leaves_its_turn_unanswered(
    journal_store: EventJournalStore,
) -> None:
    """Deleting the held sources cancels the approval; it finishes without a FINAL and nothing answered them."""
    alice = journal_store.principal("agent@alice")
    await journal_tests.TestApprovalContinuations.admit_sources(alice)
    await _pending_turn(journal_store, "$source-1", "$source-2")
    paused = await paused_for_approval(alice, journal_tests.TestApprovalContinuations.continuation(state="waiting"))
    assert paused is not None
    await _delete(alice, "$source-1")
    assert (await alice.approval_continuation("approval-1")).state == "waiting"
    await _delete(alice, "$source-2")
    held = await alice.replies.for_sources(("$source-1",))
    assert held is not None
    # The reply goes with its message; the approval stays until its settlement expires its cards.
    assert held.state is ReplyState.GONE
    failing = await alice.approval_continuation("approval-1")
    assert failing is not None
    assert failing.state == "failing"
    assert failing.failure_reason == "cancelled_by_user"
    assert await alice.finish_approval_continuation("approval-1") is not None
    assert not await alice.is_pending("$source-1")
    assert not await _answered(journal_store, "$source-1")


async def test_an_approval_end_reaches_its_conversation_even_when_its_caller_is_cancelled(
    journal_store: EventJournalStore,
) -> None:
    """A committed finish still releases the conversation it held: the commit and its effects finish together."""
    alice = journal_store.principal("agent@alice")
    await journal_tests.TestApprovalContinuations.admit_sources(alice)
    paused = await paused_for_approval(alice, journal_tests.TestApprovalContinuations.continuation(state="waiting"))
    assert paused is not None
    await _delete(alice, "$source-1")
    await _delete(alice, "$source-2")
    ended: list[ApprovalEnded] = []
    runtime = reply_scope.ReplyRuntime(
        store=alice,
        entity_name="agent",
        generation="gen-1",
        retry_sources=lambda _room_id, _sources: None,
        complete_turn=AsyncMock(),
        hold_conversation=lambda _continuation: None,
        approval_ended=ended.append,
        settle_debt=lambda _reply_id: None,
    )
    committing = asyncio.Event()
    finish = PrincipalStore.finish_approval_continuation

    async def slow_finish(store: PrincipalStore, approval_id: str) -> object:
        committing.set()
        await asyncio.sleep(0)
        return await finish(store, approval_id)

    with patch.object(PrincipalStore, "finish_approval_continuation", slow_finish):
        finishing = asyncio.create_task(runtime.finish_approval("approval-1"))
        await committing.wait()
        finishing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await finishing
    assert await alice.approval_continuation("approval-1") is None
    assert [end.approval_id for end in ended] == ["approval-1"]


async def test_retention_keeps_a_reply_a_continuation_still_names(journal_store: EventJournalStore) -> None:
    """A finished reply whose continuation waits for its cleanup is not forgotten, so that continuation still reads."""
    alice = journal_store.principal("agent@alice")
    await journal_tests.TestApprovalContinuations.admit_sources(alice)
    continuation = await paused_for_approval(
        alice,
        journal_tests.TestApprovalContinuations.continuation(state="waiting"),
    )
    assert continuation is not None
    # The entity left the configuration: its reply ends while the unavailable-owner cleanup still has to discard it.
    assert await journal_store.end_entity_replies(lambda _name: True, now_ns=10) == 1

    assert await alice.replies.forget_finished(before_ns=10**18, limit=10) == 0
    assert await alice.approval_continuation("approval-1") is not None


@pytest.mark.parametrize("kind", [rl.SpanKind.TURN, rl.SpanKind.APPROVAL_RESUME])
@pytest.mark.parametrize("taken_over", [False, True])
async def test_an_in_place_approval_claim_needs_a_span_this_instance_owns(
    journal_store: EventJournalStore,
    kind: rl.SpanKind,
    taken_over: bool,
) -> None:
    """A waiter a newer instance took over neither claims the approval nor resumes the reply; the owner's waiter does."""
    alice = journal_store.principal("agent@alice")
    await journal_tests.TestApprovalContinuations.admit_sources(alice)
    await alice.replies.write_generation("gen-1")
    continuation = journal_tests.TestApprovalContinuations.continuation(state="ready")
    claim = rl.claim(
        replace(
            _request(source="$source-1"),
            sources=ResponseSources(
                pending_event_ids=continuation.source_event_ids,
                logical_source_event_ids=continuation.source_event_ids,
            ),
        ),
        ClaimContext(
            reply=None,
            last_span=None,
            current_span=None,
            interactive_span=None,
            durable_write_debt=False,
            active_generation="gen-1",
        ),
    )
    assert claim.reply is not None
    assert claim.claimed is not None
    waiting = replace(
        claim.claimed,
        kind=kind,
        approval_id="approval-1" if kind is rl.SpanKind.APPROVAL_RESUME else None,
    )
    shown = replace(claim.reply, event_id="$waiting")
    await journal_store.backend.write(
        lambda tx: replies.apply(
            tx,
            "agent@alice",
            rl.Transition(outcome=rl.Outcome.APPLIED, reply=shown, spans=(waiting,)),
        ),
    )
    # The waiter pauses its reply in place: its span keeps running while the approval is decided.
    enqueued = await alice.pause_for_approval(
        continuation,
        ReplyRowRequest(
            reply_id=shown.reply_id,
            span_id=waiting.span_id,
            decide=lambda reply, span: rl.pause(
                reply,
                span,
                rl.PauseWrite(shown=reply.presentation, prepared_revision=reply.revision, stage=None),
                in_place=True,
                now_ns=60,
            ),
        ),
        PreparedReplyRow(payload={}),
    )
    assert enqueued is not None
    paused = await alice.replies.load(shown.reply_id)
    assert paused is not None
    assert paused.state is ReplyState.PAUSED
    if taken_over:
        await alice.replies.write_generation("gen-2")

    claimed, applied = await alice.claim_approval_in_place(
        "approval-1",
        runtime_generation="gen-1",
        reply_id=paused.reply_id,
        span_id=waiting.span_id,
    )

    continuation = await alice.approval_continuation("approval-1")
    reply = await alice.replies.load(paused.reply_id)
    assert continuation is not None
    assert reply is not None
    assert applied is not None
    if taken_over:
        assert claimed is None
        assert applied.transition.outcome is rl.Outcome.STALE
        assert continuation.state == "ready"
        assert reply == paused
    else:
        assert claimed is not None
        assert applied.transition.applied
        assert continuation.state == "claimed"
        assert reply.state is ReplyState.ACTIVE


async def test_finished_replies_that_owe_nothing_are_forgotten_with_age(journal_store: EventJournalStore) -> None:
    """Retention drops an old finished reply with its spans and their tool calls; one still owing Matrix a note is kept."""
    principal = journal_store.principal(PRINCIPAL)
    reply, span = await _claimed(principal)
    await principal.replies.record_tool_call(span_id=span.span_id, call_id="call", entry_json="{}", now_ns=60)
    ended = rl.sources_settled_without_reply(replace(reply, event_id="$reply"), span, now_ns=100)
    await _apply(journal_store, ended)
    assert ended.reply is not None
    assert ended.reply.owed_write is not None

    assert await principal.replies.forget_finished(before_ns=1_000, limit=10) == 0
    await _apply(journal_store, rl.Transition(outcome=rl.Outcome.APPLIED, reply=replace(ended.reply, owed_write=None)))
    assert await principal.replies.forget_finished(before_ns=50, limit=10) == 0
    assert await principal.replies.forget_finished(before_ns=1_000, limit=10) == 1

    assert await principal.replies.load(reply.reply_id) is None
    assert await principal.replies.span(span.span_id) is None
    assert await principal.replies.tool_calls((span.span_id,)) == ()
    assert await principal.replies.for_sources(("$source",)) is None


async def _stop_waiting_for_the_create(journal_store: EventJournalStore, stop_room: str = ROOM) -> PrincipalStore:
    """Record a Stop on ``$reply`` while the reply's create, which Matrix gives that event, is still unacknowledged.

    The Stop is a reaction in ``stop_room``.
    """
    principal = journal_store.principal(PRINCIPAL)
    reply, span = await _claimed(principal)
    turns = journal_store.turn_records("agent")
    pending_turn = TurnRecord.create(["$source"], requester_id="@user:localhost")
    assert pending_turn.anchor_event_id is not None
    await turns.upsert(
        index_event_ids=pending_turn.indexed_event_ids,
        anchor_event_id=pending_turn.anchor_event_id,
        record_json=json.dumps(TurnRecordCodec._to_ledger_record(pending_turn)),
    )
    await principal.enqueue_reply_row(
        ReplyRowRequest(
            reply_id=reply.reply_id,
            span_id=span.span_id,
            decide=lambda reply, span: rl.enqueue_initial(
                reply,
                span,
                shown="ph",
                placeholder_only=True,
                prepared_revision=reply.revision,
                now_ns=60,
            ),
            placeholder_only=True,
        ),
        PreparedReplyRow(payload={"body": "Thinking..."}),
    )
    await journal_store.backend.write(
        lambda tx: reply_messages.record_pending_stop(
            tx,
            PRINCIPAL,
            target_event_id="$reply",
            receipt_order=9,
            room_id=stop_room,
            now_ns=1,
        ),
    )
    assert await principal.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.INITIAL)
    return principal


async def _acknowledge_the_create(principal: PrincipalStore) -> tuple[object, ...]:
    acknowledged = await principal.acknowledge_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.INITIAL,
        event_id="$reply",
        delivered_projections=(),
    )
    return acknowledged.reply_effects


async def test_pending_stop_is_applied_when_the_create_binds_its_target(journal_store: EventJournalStore) -> None:
    """A Stop on an event not yet bound reaches the running span once the create is acknowledged."""
    principal = await _stop_waiting_for_the_create(journal_store)
    assert await _acknowledge_the_create(principal) == (rl.CancelSpan("span-1", by_stop=True),)
    stored = await principal.replies.load("reply-1")
    assert stored is not None
    assert stored.stop_receipt_order == 9


@pytest.mark.parametrize("own_room_stop", [False, True])
async def test_a_pending_stop_from_another_room_never_reaches_the_reply(
    journal_store: EventJournalStore,
    own_room_stop: bool,
) -> None:
    """A Stop reaction in another room naming the reply's event neither stops it nor displaces its own room's Stop."""
    elsewhere = "!elsewhere:example.org"
    principal = await _stop_waiting_for_the_create(journal_store, stop_room=ROOM if own_room_stop else elsewhere)
    if own_room_stop:
        await journal_store.backend.write(
            lambda tx: reply_messages.record_pending_stop(
                tx,
                PRINCIPAL,
                target_event_id="$reply",
                receipt_order=20,
                room_id=elsewhere,
                now_ns=1,
            ),
        )

    effects = await _acknowledge_the_create(principal)

    stored = await principal.replies.load("reply-1")
    assert stored is not None
    if own_room_stop:
        assert effects == (rl.CancelSpan("span-1", by_stop=True),)
        assert stored.stop_receipt_order == 9
    else:
        assert effects == ()
        assert stored.stop_receipt_order is None


async def test_a_pending_stop_no_create_binds_is_forgotten(journal_store: EventJournalStore) -> None:
    """A Stop on another event, taken while the create was unresolved, goes once no create in the room can bind it."""
    principal = await _stop_waiting_for_the_create(journal_store)
    await journal_store.backend.write(
        lambda tx: reply_messages.record_pending_stop(
            tx,
            PRINCIPAL,
            target_event_id="$someone-else",
            receipt_order=10,
            room_id=ROOM,
            now_ns=1,
        ),
    )
    await _acknowledge_the_create(principal)

    remaining = await journal_store.backend.read(
        lambda tx: tx.fetchall("SELECT target_event_id FROM pending_reply_stops WHERE principal_id = ?", (PRINCIPAL,)),
    )
    assert not remaining


async def test_pending_stop_ends_a_span_an_older_instance_ran(journal_store: EventJournalStore) -> None:
    """A create acknowledged after a restart ends its span with the Stop: no task here would see a cancel."""
    principal = await _stop_waiting_for_the_create(journal_store)
    await principal.replies.write_generation("gen-2")
    effects = await _acknowledge_the_create(principal)
    assert not any(isinstance(effect, rl.CancelSpan) for effect in effects)
    stored = await principal.replies.load("reply-1")
    assert stored is not None
    assert stored.state is ReplyState.CANCELLED
    assert stored.owed_write == OwedWrite("span-1", rl.NoteKind.CANCELLED)
    ended = await principal.replies.span("span-1")
    assert ended is not None
    assert ended.outcome is SpanOutcome.CANCELLED


async def test_settling_a_replay_without_a_turn_ends_the_reply_it_would_continue(
    journal_store: EventJournalStore,
) -> None:
    """Ingress settling the source a restart left for replay ends the reply in that commit; nothing replays it."""
    principal = journal_store.principal(PRINCIPAL)
    await _claimed(principal)
    pending = TurnRecord.create(["$source"], completed=False)
    await journal_store.backend.write(
        lambda tx: turn_records.write_record(
            tx,
            "agent",
            index_event_ids=pending.indexed_event_ids,
            anchor_event_id="$source",
            record_json=json.dumps(TurnRecordCodec._to_ledger_record(pending)),
        ),
    )
    await principal.replies.write_generation("gen-2")
    assert len(await principal.replies.owner_lost("gen-2", now_ns=80)) == 1
    waiting = await principal.replies.load("reply-1")
    assert waiting is not None
    assert waiting.state is ReplyState.ACTIVE

    assert await principal.settle("$source") == ("reply-1",)
    ended = await principal.replies.load("reply-1")
    assert ended is not None
    # It never showed an event, so it leaves nothing behind.
    assert ended.state is ReplyState.GONE
    assert await principal.settle("$source") == ()
    # Nothing answered the turn.
    record = await journal_store.backend.read(lambda tx: turn_records.load_record(tx, "agent", "$source"))
    assert record is not None
    assert not record.completed


async def test_a_replay_is_superseded_only_once_its_reply_owes_no_row(journal_store: EventJournalStore) -> None:
    """While the placeholder's send is unresolved the replay keeps its sources; once sent, both settle together."""
    principal = journal_store.principal(PRINCIPAL)
    reply, span = await _claimed(principal)
    await principal.enqueue_reply_row(
        ReplyRowRequest(
            reply_id=reply.reply_id,
            span_id=span.span_id,
            decide=lambda reply, span: rl.enqueue_initial(
                reply,
                span,
                shown="ph",
                placeholder_only=True,
                prepared_revision=reply.revision,
                now_ns=60,
            ),
            placeholder_only=True,
        ),
        PreparedReplyRow(payload={"body": "Thinking..."}),
    )
    # A restart lost the span; its source waits for the replay.
    await principal.replies.write_generation("gen-2")
    assert len(await principal.replies.owner_lost("gen-2", now_ns=80)) == 1

    kept = await principal.replies.supersede_replay(("$source",), now_ns=90)
    assert kept is not None
    assert kept.transition.outcome is rl.Outcome.DEFERRED
    assert await principal.is_pending("$source")

    assert await principal.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.INITIAL)
    await principal.acknowledge_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.INITIAL,
        event_id="$reply",
        delivered_projections=(),
    )
    superseded = await principal.replies.supersede_replay(("$source",), now_ns=100)
    assert superseded is not None
    assert superseded.transition.outcome is rl.Outcome.APPLIED
    gone = await principal.replies.load("reply-1")
    assert gone is not None
    assert gone.state is ReplyState.GONE
    assert gone.redaction_pending == ("$reply",)
    assert not await principal.is_pending("$source")
    assert await principal.replies.supersede_replay(("$elsewhere",), now_ns=110) is None


async def test_a_removed_entitys_open_replies_end_without_writing(journal_store: EventJournalStore) -> None:
    """The removed entity's running reply fails with its span lost, owing nothing; other entities are untouched."""
    principal = journal_store.principal(PRINCIPAL)
    await _pending_turn(journal_store, "$source")
    await _claimed(principal)
    other = journal_store.principal("other@alice")
    others = rl.claim(
        replace(_request("span-other", reply_id="reply-other"), entity_name="other"),
        ClaimContext(
            reply=None,
            last_span=None,
            current_span=None,
            interactive_span=None,
            durable_write_debt=False,
            active_generation="gen-1",
        ),
    )
    await journal_store.backend.write(lambda tx: replies.apply(tx, "other@alice", others))

    assert await journal_store.end_entity_replies(lambda name: name == "agent", now_ns=50) == 1

    ended = await principal.replies.load("reply-1")
    assert ended is not None
    assert ended.state is ReplyState.FAILED
    assert ended.current_span_id is None
    assert ended.owed_write is None
    assert ended.redaction_pending == ()
    lost = await principal.replies.span("span-1")
    assert lost is not None
    assert lost.outcome is SpanOutcome.LOST
    untouched = await other.replies.load("reply-other")
    assert untouched is not None
    assert untouched.state is ReplyState.ACTIVE
    assert await journal_store.end_entity_replies(lambda name: name == "agent", now_ns=60) == 0
    # No bot answers the source any more; it settles unanswered instead of waiting for a replay forever.
    assert not await principal.is_pending("$source")
    assert not await _answered(journal_store, "$source")


async def test_lock_and_state_queries(journal_store: EventJournalStore) -> None:
    """Locking returns the stored reply; state queries return only the asked states."""
    transition = _first_claim()
    await _apply(journal_store, transition)
    locked = await journal_store.backend.write(lambda tx: reply_messages.lock(tx, PRINCIPAL, "reply-1"))
    assert locked == transition.reply
    assert await journal_store.backend.write(lambda tx: reply_messages.lock(tx, PRINCIPAL, "missing")) is None
    active = await journal_store.backend.read(lambda tx: reply_messages.in_states(tx, PRINCIPAL, (ReplyState.ACTIVE,)))
    assert [reply.reply_id for reply in active] == ["reply-1"]
    assert (
        await journal_store.backend.read(lambda tx: reply_messages.in_states(tx, PRINCIPAL, (ReplyState.GONE,))) == ()
    )


async def test_owner_lost_ends_what_an_older_instance_left_running(journal_store: EventJournalStore) -> None:
    """At start, an orphaned span whose sources settled fails with a restart note owed; one with pending sources waits for replay."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$pending")
    orphan = _first_claim()
    assert orphan.reply is not None
    # It showed an event, which the restart note ends.
    orphan = replace(orphan, reply=replace(orphan.reply, event_id="$reply-1"))
    waiting = _first_claim(span_id="span-2", reply_id="reply-2", source="$pending")
    for transition in (orphan, waiting):
        await _apply(journal_store, transition)

    applied = await principal.replies.owner_lost("gen-2", now_ns=50)

    assert len(applied) == 2
    failed = await principal.replies.load("reply-1")
    assert failed is not None
    assert failed.state is rl.ReplyState.FAILED
    assert failed.owed_write == OwedWrite("span-1", rl.NoteKind.RESTART)
    lost = await principal.replies.span("span-1")
    assert lost is not None
    assert lost.outcome is rl.SpanOutcome.LOST
    replayable = await principal.replies.load("reply-2")
    assert replayable is not None
    assert replayable.state is rl.ReplyState.ACTIVE
    assert replayable.current_span_id is None
    assert (await principal.replies.span("span-2")).outcome is rl.SpanOutcome.LOST  # type: ignore[union-attr]
    # Run again by the same instance, it finds nothing left to end.
    assert await principal.replies.owner_lost("gen-2", now_ns=60) == ()


async def test_finished_replies_are_kept_as_long_as_the_ledger_keeps_their_turns() -> None:
    """Nothing reaches a finished reply after its turn is forgotten, so both retentions agree."""
    ledger_default = inspect.signature(HandledTurnLedger._cleanup_old_events).parameters["max_age_days"].default
    assert ledger_default * 24 * 60 * 60 * 1_000_000_000 == reply_scope._FINISHED_REPLY_RETENTION_NS


_KEY = '{"recipient":"agent","room_id":"!room"}'


def _wait(*, hold_key: str = _KEY) -> Decide:
    def decide(reply: rl.Reply, span: rl.Span) -> rl.Transition:
        write = rl.TerminalWrite(shown="answer", prepared_revision=reply.revision, state=ReplyState.WAITING)
        return rl.wait(reply, span, write, hold_key=hold_key, now_ns=50)

    return decide


async def _waiting(principal: PrincipalStore, *, source: str = "$source", reply_id: str = "reply-1") -> rl.Reply:
    """Return a reply that answered ``source`` and waits for its key's background work."""
    await admit(principal, source)
    await principal.replies.write_generation("gen-1")
    claim = (await principal.replies.claim(_request(f"span-{reply_id}", reply_id=reply_id, source=source))).transition
    assert claim.reply is not None
    assert claim.claimed is not None
    waited = await principal.replies.decide(reply_id=reply_id, span_id=claim.claimed.span_id, decide=_wait())
    assert waited.transition.reply is not None
    assert waited.transition.reply.state is ReplyState.WAITING
    return waited.transition.reply


async def test_an_owed_write_without_a_note_round_trips(journal_store: EventJournalStore) -> None:
    """The write a reply owes once it stops waiting carries no note."""
    principal = journal_store.principal(PRINCIPAL)
    transition = _first_claim()
    assert transition.reply is not None
    owed = replace(transition.reply, owed_write=OwedWrite("span-1", None))
    await _apply(journal_store, replace(transition, reply=owed))
    loaded = await principal.replies.load("reply-1")
    assert loaded is not None
    assert loaded.owed_write == OwedWrite("span-1", None)


async def test_a_newer_waiting_reply_takes_the_work_over(journal_store: EventJournalStore) -> None:
    """At most one reply waits for a key's work: the newer one ends the older one, which keeps its answer."""
    principal = journal_store.principal(PRINCIPAL)
    await _waiting(principal)
    other_key = await _waiting(principal, source="$elsewhere", reply_id="reply-3")
    assert other_key.hold_key == _KEY
    # The second wait above took reply-1 over too; a reply on another key is left alone.
    first = await principal.replies.load("reply-1")
    assert first is not None
    assert first.state is ReplyState.COMPLETED
    assert first.owed_write == OwedWrite("span-reply-1", None)

    await admit(principal, "$later")
    claim = (await principal.replies.claim(_request("span-reply-2", reply_id="reply-2", source="$later"))).transition
    assert claim.claimed is not None
    elsewhere = await principal.replies.decide(
        reply_id="reply-2",
        span_id=claim.claimed.span_id,
        decide=_wait(hold_key='{"recipient":"other"}'),
    )
    assert elsewhere.post_commit == ()
    taken = await principal.replies.load("reply-3")
    assert taken is not None
    assert taken.state is ReplyState.WAITING
    waiting = await journal_store.waiting_replies()
    assert [(principal_id, reply.reply_id) for principal_id, reply in waiting] == [
        (PRINCIPAL, "reply-2"),
        (PRINCIPAL, "reply-3"),
    ]


async def test_taking_work_over_owes_the_older_replys_end(journal_store: EventJournalStore) -> None:
    """The older reply's final write is due after the takeover commits."""
    principal = journal_store.principal(PRINCIPAL)
    await _waiting(principal)
    await admit(principal, "$later")
    claim = (await principal.replies.claim(_request("span-reply-2", reply_id="reply-2", source="$later"))).transition
    assert claim.claimed is not None
    taken = await principal.replies.decide(reply_id="reply-2", span_id=claim.claimed.span_id, decide=_wait())
    assert taken.post_commit == (replies.ReplyDebtDue("reply-1"),)


async def test_a_wake_claims_the_waiting_reply_it_names(journal_store: EventJournalStore) -> None:
    """A wake's sources are its own; the claim finds the reply by the wake's name for it."""
    principal = journal_store.principal(PRINCIPAL)
    await _waiting(principal)
    wake = replace(
        _request("span-wake", reply_id="unused", source="job-wake:reply-1:1"),
        wake_reply_id="reply-1",
    )
    claim = (await principal.replies.claim(wake)).transition
    assert claim.claimed is not None
    assert claim.claimed.kind is SpanKind.WAKE
    assert claim.claimed.reply_id == "reply-1"
    woken = await principal.replies.load("reply-1")
    assert woken is not None
    assert woken.state is ReplyState.ACTIVE
    assert woken.current_span_id == "span-wake"


async def test_a_stop_records_its_job_cancellation_until_a_runtime_applies_it(journal_store: EventJournalStore) -> None:
    """A Stop of an unfinished reply records that its work must be cancelled; retention drops what was never applied."""
    principal = journal_store.principal(PRINCIPAL)
    reply = await _waiting(principal)
    stopped = await _apply(
        journal_store,
        rl.stop(reply, None, rl.StopFacts(receipt_order=5, span_live=False), now_ns=60),
    )
    assert stopped.transition.reply is not None
    assert stopped.transition.reply.state is ReplyState.CANCELLED
    assert await journal_store.reply_job_stops() == ((PRINCIPAL, "reply-1"),)
    await principal.replies.forget_job_stop("reply-1")
    assert await journal_store.reply_job_stops() == ()

    await _apply(journal_store, rl.Transition(outcome=rl.Outcome.APPLIED, reply=stopped.transition.reply))
    await journal_store.backend.write(
        lambda tx: reply_messages.record_job_stop(tx, PRINCIPAL, "reply-1", now_ns=60),
    )
    flushed = replace(stopped.transition.reply, owed_write=None)
    await _apply(journal_store, rl.Transition(outcome=rl.Outcome.APPLIED, reply=flushed))
    assert await principal.replies.forget_finished(before_ns=10**18, limit=10) == 1
    assert await journal_store.reply_job_stops() == ()
