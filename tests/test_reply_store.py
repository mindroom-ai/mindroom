"""Reply records persist exactly on both journal backends."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom import reply_lifecycle as rl
from mindroom.event_journal import DeliveryStage, DepartureSource, replies, reply_messages, reply_spans
from mindroom.event_journal.replies import AppliedTransition, ClaimLookup, Decide, ReplyRowRequest
from mindroom.handled_turns import TurnRecordCodec
from mindroom.reply_lifecycle import (
    ClaimContext,
    ClaimRequest,
    LegacyPending,
    OwedWrite,
    ReplyState,
    Rollback,
    SpanKind,
    SpanOutcome,
    SpanSources,
)
from mindroom.turn_record import TurnRecord
from tests.journal_membership_helpers import admit_room_membership
from tests.test_event_journal_store import ROOM, admit

if TYPE_CHECKING:
    from mindroom.event_journal import EventJournalStore, PrincipalStore

pytestmark = pytest.mark.asyncio

PRINCIPAL = "agent@alice"


def _request(span_id: str = "span-1", *, reply_id: str = "reply-1", source: str = "$source") -> ClaimRequest:
    return ClaimRequest(
        span_id=span_id,
        delivery_id=source,
        sources=SpanSources(pending=(source,), logical=(source,), discovery=("$alias",)),
        bot_generation="gen-1",
        now_ns=10,
        new_reply_id=reply_id,
        entity_name="agent",
        room_id=ROOM,
        thread_id="$thread",
        membership_epoch=3,
        requester_id="@user:example.org",
        visibility_policy=rl.VisibilityPolicy.NORMAL,
        empty_presentation='{"version":1}',
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
    """Every reply column and span column restores as written."""
    principal = journal_store.principal(PRINCIPAL)
    transition = _first_claim()
    applied = await _apply(journal_store, transition)
    assert applied.post_commit == ()
    assert transition.reply is not None
    assert transition.claimed is not None
    reply = replace(
        transition.reply,
        event_id="$reply",
        continuation_event_ids=("$segment",),
        frozen_display='{"frozen":true}',
        possibly_shown='{"shown":true}',
        possibly_shown_seq=4,
        confirmed_seq=3,
        legacy_pending=LegacyPending.PRESENTATION_READ,
        placeholder_only=True,
        stop_receipt_order=9,
        stop_applied_receipt_order=8,
        stop_button_event_id="$button",
        redaction_pending=("$old",),
        owed_write=OwedWrite("span-1", rl.NOTE_ERROR, "boom"),
        reply_sequence=4,
        approval_id="approval-1",
        revision=7,
    )
    await _apply(journal_store, rl.Transition(outcome=rl.Outcome.APPLIED, reply=reply))

    assert await principal.replies.load("reply-1") == reply
    assert await principal.replies.for_event("$reply") == reply
    span = await principal.replies.span("span-1")
    assert span == transition.claimed
    assert span is not None
    assert span.sources == SpanSources(pending=("$source",), logical=("$source",), discovery=("$alias",))


async def test_span_outcome_is_write_once_and_rollback_round_trips(journal_store: EventJournalStore) -> None:
    """A span's outcome can be written once; a conflicting second outcome is refused."""
    principal = journal_store.principal(PRINCIPAL)
    transition = _first_claim()
    await _apply(journal_store, transition)
    assert transition.claimed is not None
    rollback = Rollback(presentation="old", frozen_display=None, state=ReplyState.COMPLETED, presentation_known=False)
    regen = replace(transition.claimed, span_id="span-2", kind=SpanKind.REGENERATION, rollback=rollback)
    await journal_store.backend.write(lambda tx: reply_spans.save(tx, PRINCIPAL, regen))
    assert await principal.replies.span("span-2") == regen

    ended = replace(transition.claimed, outcome=SpanOutcome.COMPLETED, ended_at_ns=20)
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


async def test_room_and_work_queries(journal_store: EventJournalStore) -> None:
    """Replies are found by room and state, and by owed work."""
    transition = _first_claim()
    await _apply(journal_store, transition)
    assert transition.reply is not None
    active = await journal_store.backend.read(
        lambda tx: reply_messages.for_room(tx, PRINCIPAL, ROOM, states=(ReplyState.ACTIVE, ReplyState.PAUSED)),
    )
    assert [reply.reply_id for reply in active] == ["reply-1"]
    assert await journal_store.backend.read(lambda tx: reply_messages.with_pending_work(tx, PRINCIPAL)) == ()
    owed = replace(transition.reply, owed_write=OwedWrite("span-1", rl.NOTE_RESTART))
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
    taken = await journal_store.backend.write(lambda tx: reply_messages.take_pending_stop(tx, PRINCIPAL, "$created"))
    assert taken == (6, ROOM)
    assert (
        await journal_store.backend.write(lambda tx: reply_messages.take_pending_stop(tx, PRINCIPAL, "$created"))
        is None
    )


async def test_generation_is_replaced_by_each_bot_instance(journal_store: EventJournalStore) -> None:
    """Each bot instance becomes the owner of its principal's replies."""
    replies = journal_store.principal(PRINCIPAL).replies
    assert await replies.active_generation() is None
    await replies.write_generation("gen-1", now_ns=1)
    await replies.write_generation("gen-2", now_ns=2)
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


async def test_post_commit_effects_are_returned(journal_store: EventJournalStore) -> None:
    """Cancellation of a live span is left for after the commit."""
    principal = journal_store.principal(PRINCIPAL)
    claim = _first_claim()
    await _apply(journal_store, claim)
    assert claim.reply is not None
    stop = rl.stop(claim.reply, claim.claimed, rl.StopFacts(3, newer_edit=False, span_live=True), now_ns=40)
    applied = await _apply(journal_store, stop)
    assert applied.post_commit == (rl.CancelSpan("span-1", by_stop=True),)
    stored = await principal.replies.load("reply-1")
    assert stored is not None
    assert stored.stop_receipt_order == 3


# --- reply rows -------------------------------------------------------------


async def _claimed(principal: PrincipalStore) -> tuple[rl.Reply, rl.Span]:
    await admit(principal, "$source")
    await principal.replies.write_generation("gen-1", now_ns=1)
    claim = (await principal.replies.claim(_request(), ClaimLookup())).transition
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
        request=ReplyRowRequest(reply_id=reply.reply_id, span_id=span.span_id, decide=_finish()),
        room_id=ROOM,
        thread_id=None,
        payload={"body": "answer"},
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
    assert acknowledged.bound
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
    await _apply(journal_store, rl.stop(reply, span, rl.StopFacts(4, newer_edit=False, span_live=True), now_ns=40))
    enqueued = await principal.enqueue_reply_row(
        request=ReplyRowRequest(reply_id=reply.reply_id, span_id=span.span_id, decide=_finish(revision=0)),
        room_id=ROOM,
        thread_id=None,
        payload={"body": "answer"},
    )
    assert enqueued is not None
    assert enqueued.transition.outcome is rl.Outcome.RECOMPUTE
    assert enqueued.transaction_id is None
    assert await principal.is_pending("$source")
    assert await principal.load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL) is None


async def test_edit_row_waits_for_the_create_and_targets_its_event(journal_store: EventJournalStore) -> None:
    """A non-terminal row enqueued before the create is acknowledged edits the event that create binds."""
    principal = journal_store.principal(PRINCIPAL)
    reply, span = await _claimed(principal)
    initial = await principal.enqueue_reply_row(
        request=ReplyRowRequest(
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
        room_id=ROOM,
        thread_id=None,
        payload={"body": "Thinking..."},
    )
    assert initial is not None
    assert initial.stage is rl.WriteStage.INITIAL
    pause = await principal.enqueue_reply_row(
        request=ReplyRowRequest(
            reply_id=reply.reply_id,
            span_id=span.span_id,
            decide=lambda reply, span: rl.fail(
                reply,
                span,
                rl.TerminalWrite(shown="note", prepared_revision=reply.revision, state=ReplyState.ACTIVE),
                phase="pre_delivery",
                now_ns=70,
            ),
        ),
        room_id=ROOM,
        thread_id=None,
        payload={"body": "note"},
    )
    assert pause is not None
    assert pause.stage is rl.WriteStage.EDIT
    assert pause.delivery_id == "$source:edit:2"
    assert await principal.is_pending("$source")
    assert [row[2] for row in await principal.unresolved_reply_rows("reply-1")] == [1, 2]
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


async def test_permanent_failure_of_a_terminal_row_applies_its_rule(journal_store: EventJournalStore) -> None:
    """A terminal row Matrix refuses for good leaves the reply failed, with the span's outcome kept."""
    principal = journal_store.principal(PRINCIPAL)
    reply, span = await _claimed(principal)
    await _apply(journal_store, rl.Transition(outcome=rl.Outcome.APPLIED, reply=replace(reply, event_id="$reply")))
    enqueued = await principal.enqueue_reply_row(
        request=ReplyRowRequest(reply_id=reply.reply_id, span_id=span.span_id, decide=_finish()),
        room_id=ROOM,
        thread_id=None,
        payload={"body": "answer"},
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
        request=ReplyRowRequest(reply_id=reply.reply_id, span_id=span.span_id, decide=_finish()),
        room_id=ROOM,
        thread_id=None,
        payload={"body": "answer"},
    )
    # The released span's write is stale, so no row is recorded.
    assert enqueued is not None
    assert enqueued.applied.transition.outcome is rl.Outcome.STALE
    assert enqueued.delivery_id is None
    assert await principal.replies.load("reply-1") == departed


async def _stop_waiting_for_the_create(journal_store: EventJournalStore) -> PrincipalStore:
    """Record a Stop on ``$reply`` while the reply's create, which Matrix gives that event, is still unacknowledged."""
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
        request=ReplyRowRequest(
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
        room_id=ROOM,
        thread_id=None,
        payload={"body": "Thinking..."},
    )
    await journal_store.backend.write(
        lambda tx: reply_messages.record_pending_stop(
            tx,
            PRINCIPAL,
            target_event_id="$reply",
            receipt_order=9,
            room_id=ROOM,
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


async def _assert_the_turn_learned_the_stop(journal_store: EventJournalStore) -> None:
    # The turn learned the Stop and the reply's event in the same transaction.
    ((_index, _anchor, record_json),) = await journal_store.turn_records("agent").load_all()
    stopped = TurnRecordCodec._from_ledger_record("$source", json.loads(record_json))
    assert stopped is not None
    assert stopped.response_event_id == "$reply"
    assert stopped.user_stop_receipt_order == 9
    assert stopped.user_stop_settled_receipt_order == 9


async def test_pending_stop_is_applied_when_the_create_binds_its_target(journal_store: EventJournalStore) -> None:
    """A Stop on an event not yet bound reaches the running span once the create is acknowledged."""
    principal = await _stop_waiting_for_the_create(journal_store)
    assert await _acknowledge_the_create(principal) == (
        rl.CancelSpan("span-1", by_stop=True),
        rl.TransferStop(9, "$reply", turn_id="$source"),
    )
    stored = await principal.replies.load("reply-1")
    assert stored is not None
    assert stored.stop_receipt_order == 9
    await _assert_the_turn_learned_the_stop(journal_store)


async def test_pending_stop_ends_a_span_an_older_instance_ran(journal_store: EventJournalStore) -> None:
    """A create acknowledged after a restart ends its span with the Stop: no task here would see a cancel."""
    principal = await _stop_waiting_for_the_create(journal_store)
    await principal.replies.write_generation("gen-2", now_ns=70)
    assert await _acknowledge_the_create(principal) == (rl.TransferStop(9, "$reply", turn_id="$source"),)
    stored = await principal.replies.load("reply-1")
    assert stored is not None
    assert stored.state is ReplyState.CANCELLED
    assert stored.owed_write == OwedWrite("span-1", rl.NOTE_CANCELLED)
    ended = await principal.replies.span("span-1")
    assert ended is not None
    assert ended.outcome is SpanOutcome.CANCELLED
    await _assert_the_turn_learned_the_stop(journal_store)


async def test_a_replay_is_superseded_only_once_its_reply_owes_no_row(journal_store: EventJournalStore) -> None:
    """While the placeholder's send is unresolved the replay keeps its sources; once sent, both settle together."""
    principal = journal_store.principal(PRINCIPAL)
    reply, span = await _claimed(principal)
    await principal.enqueue_reply_row(
        request=ReplyRowRequest(
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
        room_id=ROOM,
        thread_id=None,
        payload={"body": "Thinking..."},
    )
    # A restart lost the span; its source waits for the replay.
    await principal.replies.write_generation("gen-2", now_ns=70)
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
    waiting = _first_claim(span_id="span-2", reply_id="reply-2", source="$pending")
    for transition in (orphan, waiting):
        await _apply(journal_store, transition)

    applied = await principal.replies.owner_lost("gen-2", now_ns=50)

    assert len(applied) == 2
    failed = await principal.replies.load("reply-1")
    assert failed is not None
    assert failed.state is rl.ReplyState.FAILED
    assert failed.owed_write == OwedWrite("span-1", rl.NOTE_RESTART)
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


async def test_owner_lost_leaves_a_reply_waiting_for_its_legacy_read(journal_store: EventJournalStore) -> None:
    """A main-era reply decides what it showed from its legacy read before any restart note."""
    principal = journal_store.principal(PRINCIPAL)
    transition = _first_claim()
    assert transition.reply is not None
    await _apply(
        journal_store,
        replace(transition, reply=replace(transition.reply, legacy_pending=LegacyPending.PRESENTATION_READ)),
    )
    assert await principal.replies.owner_lost("gen-2", now_ns=50) == ()
    span = await principal.replies.span("span-1")
    assert span is not None
    assert span.outcome is None
