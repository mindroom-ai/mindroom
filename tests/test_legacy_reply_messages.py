"""Replies main left in flight get reply records once, and learn what only Matrix shows after sync."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom import reply_lifecycle as rl
from mindroom.event_journal import (
    INTERRUPTED_FAILURE_REASON,
    ApprovalContinuation,
    DeliveryStage,
    DepartureSource,
    EventKind,
    turn_records,
)
from mindroom.event_journal.replies import ClaimLookup, ReplyCreation, ReplyRowRequest
from mindroom.handled_turns import TurnRecordCodec
from mindroom.history.types import HistoryScope
from mindroom.legacy_reply_messages import LEGACY_PRESENTATIONS, LegacyReplyReads
from mindroom.message_target import MessageTarget
from mindroom.reply_presentation import Presentation, Segment, decode_presentation, encode_presentation
from mindroom.reply_scope import ReplyRuntime
from mindroom.response_sources import ResponseSources
from mindroom.tool_system.events import ToolTraceEntry
from mindroom.turn_record import TurnRecord
from tests.journal_membership_helpers import admit_room_membership
from tests.legacy_reply_helpers import keep_main_paused_answer, store_main_continuation
from tests.test_event_journal_store import ROOM, admit

if TYPE_CHECKING:
    from mindroom.event_journal import EventJournalStore, PrincipalStore

pytestmark = pytest.mark.asyncio

PRINCIPAL = "agent@alice"
ENTITY = "agent"
NOW = 1_000_000


async def _turn(
    journal_store: EventJournalStore,
    source: str,
    *,
    coalesced: tuple[str, ...] = (),
    completed: bool = False,
    response_event_id: str | None = None,
    stop_order: int | None = None,
    stop_settled: bool = False,
    history: bool = True,
) -> TurnRecord:
    """Store a main-era turn record of this agent, as main's ledger wrote it."""
    record = TurnRecord.create(
        [source, *coalesced],
        requester_id="@user:example.org",
        completed=completed,
        response_event_id=response_event_id,
        conversation_target=MessageTarget.resolve(ROOM, None, source, room_mode=True),
        history_scope=HistoryScope(kind="agent", scope_id=ENTITY) if history else None,
    )
    stored = TurnRecordCodec._to_ledger_record(record)
    if stop_order is not None:
        # Main kept a turn's Stop on its record.
        stored["user_stop_receipt_order"] = stop_order
        if stop_settled:
            stored["user_stop_settled_receipt_order"] = stop_order
    anchor_event_id = record.anchor_event_id
    assert anchor_event_id is not None
    # Written as main's ledger stored it, past the current codec.
    await journal_store.backend.write(
        lambda transaction: turn_records.upsert(
            transaction,
            ENTITY,
            index_event_ids=record.indexed_event_ids,
            anchor_event_id=anchor_event_id,
            record_json=json.dumps(stored),
        ),
    )
    return record


async def _row(
    principal: PrincipalStore,
    source: str,
    stage: DeliveryStage,
    body: str,
    *,
    status: str,
    acknowledged: str | None = None,
    edits: str | None = None,
) -> None:
    """Record one of main's outbox rows for a turn, without reply identity."""
    content: dict[str, object] = {"msgtype": "m.text", "body": body, "io.mindroom.stream_status": status}
    payload = (
        {"m.new_content": content, "m.relates_to": {"rel_type": "m.replace", "event_id": edits}}
        if edits is not None
        else content
    )
    assert await principal.enqueue_matrix_delivery(
        delivery_id=source,
        stage=stage,
        room_id=ROOM,
        thread_id=None,
        payload=payload,
        edits_event_id=edits,
    )
    if acknowledged is not None:
        assert await principal.claim_matrix_delivery(delivery_id=source, stage=stage)
        await principal.acknowledge_matrix_delivery(
            delivery_id=source,
            stage=stage,
            event_id=acknowledged,
            delivered_projections=(),
        )


def _continuation(state: str, *, approval_id: str = "approval-1") -> ApprovalContinuation:
    return ApprovalContinuation(
        approval_id=approval_id,
        run_id=f"run-{approval_id}",
        session_id="session",
        entity_kind="agent",
        entity_name=ENTITY,
        room_id=ROOM,
        thread_id=None,
        requester_id="@user:example.org",
        response_event_id="$reply",
        sources=ResponseSources(("$source",), ("$source",)),
        calls=(),
        state=state,  # type: ignore[arg-type]
    )


async def _main_continuation(
    principal: PrincipalStore,
    continuation: ApprovalContinuation,
    *,
    text: str = "Reading document",
    tool_trace: tuple[ToolTraceEntry, ...] = (),
    team_state: dict[str, object] | None = None,
) -> None:
    """Store a continuation as main left it, with the paused answer kept in its context."""
    await store_main_continuation(principal, continuation)
    await keep_main_paused_answer(
        principal,
        continuation.approval_id,
        text=text,
        tool_trace=tool_trace,
        team_state=team_state,
    )


async def _adopt(principal: PrincipalStore) -> tuple[rl.Reply, ...]:
    await principal.replies.write_generation("gen-new", now_ns=NOW)
    await principal.adopt_legacy_replies(
        entity_name=ENTITY,
        presentations=LEGACY_PRESENTATIONS,
        now_ns=NOW,
    )
    replies = []
    for reply, _last in await principal.legacy_reply_reads():
        replies.append(reply)
    return tuple(replies)


async def _only_reply(principal: PrincipalStore, source: str = "$source") -> rl.Reply:
    reply = await principal.replies.for_sources((source,))
    assert reply is not None
    return reply


async def _spans(principal: PrincipalStore, reply: rl.Reply) -> list[tuple[rl.SpanKind, rl.SpanOutcome | None]]:
    return [(span.kind, span.outcome) for span in await principal.replies.spans(reply.reply_id)]


def _text(presentation: str) -> str:
    return "".join(segment.text for segment in decode_presentation(presentation).segments)


@pytest.mark.parametrize("state", ["waiting", "ready"])
@pytest.mark.parametrize("entity_kind", ["agent", "team"])
async def test_a_waiting_approval_pauses_its_reply_with_what_it_showed(
    journal_store: EventJournalStore,
    entity_kind: Literal["agent", "team"],
    state: Literal["waiting", "ready"],
) -> None:
    """The continuation's kept presentation becomes the paused reply's; the approval runtime stays its owner.

    Nothing has resumed past the pause yet, so no read replaces it: a team's
    resume restores the document kept here.
    """
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")
    trace = ToolTraceEntry(type="tool_call_started", tool_name="read_document", tool_call_id="call-1")
    # Main kept a team's structured document beside its text; an agent's state was empty.
    team_state: dict[str, object] | None = (
        {"kind": "team_stream", "version": 2, "members": [], "consensus": "Reading document"}
        if entity_kind == "team"
        else None
    )
    continuation = replace(_continuation(state), entity_kind=entity_kind)
    await _main_continuation(principal, continuation, tool_trace=(trace,), team_state=team_state)

    assert await _adopt(principal) == ()
    reply = await _only_reply(principal)
    assert reply.legacy_pending is None
    assert reply.state is rl.ReplyState.PAUSED
    assert reply.event_id == "$reply"
    assert reply.approval_id == "approval-1"
    (answer,) = decode_presentation(reply.presentation).segments
    assert answer.text == "Reading document"
    assert answer.tool_trace == (trace,)
    assert answer.team_state == team_state
    assert await _spans(principal, reply) == [(rl.SpanKind.TURN, rl.SpanOutcome.PAUSED)]
    # The continuation now names the span that paused its reply, which holds its reply's identity.
    adopted = await principal.approval_continuation("approval-1")
    assert adopted is not None
    assert adopted.span_id == reply.last_span_id
    assert (adopted.response_event_id, adopted.room_id, adopted.source_event_ids) == ("$reply", ROOM, ("$source",))
    # owner_lost leaves a paused reply to its approval.
    assert await principal.replies.owner_lost("gen-new", now_ns=NOW) == ()


async def test_an_interrupted_stream_with_pending_sources_waits_for_its_read_then_replays(
    journal_store: EventJournalStore,
) -> None:
    """Its event is bound and read after sync; until then a replay claim waits, then continues below it."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _turn(journal_store, "$source")
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")

    (reply,) = await _adopt(principal)
    assert reply.state is rl.ReplyState.ACTIVE
    assert reply.event_id == "$reply"
    assert reply.legacy_pending is rl.LegacyPending.PRESENTATION_READ
    assert await _spans(principal, reply) == [(rl.SpanKind.TURN, rl.SpanOutcome.LOST)]
    assert await principal.replies.owner_lost("gen-new", now_ns=NOW) == ()

    shown = encode_presentation(Presentation(segments=(Segment(kind="answer", text="Partial"),)))
    await principal.finish_legacy_reply_read(reply.reply_id, rl.LegacyRead(shown=shown, event_id="$reply"), now_ns=NOW)
    read = await _only_reply(principal)
    assert read.legacy_pending is None
    assert read.state is rl.ReplyState.ACTIVE
    assert _text(read.possibly_shown or "") == "Partial"


async def test_a_stop_the_earlier_release_finished_gets_no_reply(journal_store: EventJournalStore) -> None:
    """A Stop shown just before a crash stands, though the source it left pending replays: that turn ignores it."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _turn(journal_store, "$source", stop_order=5, stop_settled=True)
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")

    assert await _adopt(principal) == ()
    assert await principal.replies.for_sources(("$source",)) is None


async def test_a_stream_main_stopped_after_its_sources_settled_gets_the_restart_note(
    journal_store: EventJournalStore,
) -> None:
    """Still streaming inside main's cleanup window, it fails with the restart note main's cleanup gave it."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await principal.settle_many(("$source",))
    await _turn(journal_store, "$source")
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")

    (reply,) = await _adopt(principal)
    shown = encode_presentation(Presentation(segments=(Segment(kind="answer", text="Partial"),)))
    await principal.finish_legacy_reply_read(
        reply.reply_id,
        rl.LegacyRead(shown=shown, event_id="$reply", recent=True),
        now_ns=NOW,
    )
    ended = await _only_reply(principal)
    assert ended.state is rl.ReplyState.FAILED
    assert ended.owed_write is not None
    assert ended.owed_write.note == rl._NOTE_RESTART


async def test_a_settled_stream_from_days_ago_is_history(journal_store: EventJournalStore) -> None:
    """No cleanup an earlier release ran could still reach it, so it gets no reply and no read."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await principal.settle_many(("$source",))
    await _turn(journal_store, "$source")
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")
    initial = await principal.load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.INITIAL)
    assert initial is not None
    two_days_later = initial.created_at_ns + 2 * 24 * 60 * 60 * 1_000_000_000

    await principal.replies.write_generation("gen-new", now_ns=two_days_later)
    await principal.adopt_legacy_replies(
        entity_name=ENTITY,
        presentations=LEGACY_PRESENTATIONS,
        now_ns=two_days_later,
    )

    assert await principal.legacy_reply_reads() == ()
    assert await principal.replies.for_sources(("$source",)) is None


async def test_a_placeholder_whose_source_was_deleted_becomes_a_gone_reply_that_removes_it(
    journal_store: EventJournalStore,
) -> None:
    """Main still owed the cleanup of a deleted request's placeholder; the gone reply owes its redaction."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _turn(journal_store, "$source")
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")
    await admit(principal, "$redaction", redacts="$source", kind=EventKind.REDACTION, content={})

    await _adopt(principal)

    gone = await _only_reply(principal)
    assert gone.state is rl.ReplyState.GONE
    assert gone.event_id == "$reply"
    assert gone.redaction_pending == ("$reply",)
    assert await _spans(principal, gone) == [(rl.SpanKind.TURN, rl.SpanOutcome.CANCELLED)]
    initial = await principal.load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.INITIAL)
    assert initial is not None
    assert initial.reply_id == gone.reply_id


async def test_a_stream_that_shows_it_completed_keeps_its_answer(journal_store: EventJournalStore) -> None:
    """An event whose status says it ended is history, not a stream to annotate."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await principal.settle_many(("$source",))
    await _turn(journal_store, "$source")
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")

    (reply,) = await _adopt(principal)
    await principal.finish_legacy_reply_read(
        reply.reply_id,
        rl.LegacyRead(event_id="$reply", ended_as=rl.ReplyState.COMPLETED, recent=True),
        now_ns=NOW,
    )
    ended = await _only_reply(principal)
    assert ended.state is rl.ReplyState.COMPLETED
    assert ended.owed_write is None


async def test_an_unacknowledged_answer_becomes_the_reply_its_row_finishes(journal_store: EventJournalStore) -> None:
    """A frozen FINAL main still owed ends the reply as its payload says; its acknowledgement binds it."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _turn(journal_store, "$source")
    await _row(principal, "$source", DeliveryStage.FINAL, "The answer.", status="completed")

    assert await _adopt(principal) == ()
    reply = await _only_reply(principal)
    assert reply.state is rl.ReplyState.COMPLETED
    assert reply.event_id is None
    row = await principal.load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    assert row is not None
    assert row.reply_id == reply.reply_id
    assert row.reply_sequence == 1
    # The answer is enqueued, so its source settles and its turn is answered.
    assert not await principal.is_pending("$source")
    turn = await journal_store.turn_records(ENTITY).load("$source")
    assert turn is not None
    assert turn.completed

    assert await principal.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    await principal.acknowledge_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        event_id="$answer",
        delivered_projections=(),
    )
    bound = await _only_reply(principal)
    assert bound.event_id == "$answer"
    assert bound.confirmed_seq == 1


async def test_a_pending_turn_whose_stream_created_its_reply_scans_for_it(journal_store: EventJournalStore) -> None:
    """With no placeholder row, the adoption scan finds the event before the replay claims it."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _turn(journal_store, "$source")

    (reply,) = await _adopt(principal)
    assert reply.legacy_pending is rl.LegacyPending.ADOPTION_SCAN
    assert reply.event_id is None
    await principal.finish_legacy_reply_read(reply.reply_id, rl.LegacyRead(event_id="$streamed"), now_ns=NOW)
    found = await _only_reply(principal)
    assert found.event_id == "$streamed"
    assert found.legacy_pending is None


@pytest.mark.parametrize("ended_by", ["deletion", "departure"])
async def test_a_scanned_reply_that_ended_before_its_event_was_found_removes_it_unless_the_room_was_left(
    journal_store: EventJournalStore,
    ended_by: str,
) -> None:
    """A request deleted while the scan looks for its streamed reply has that event removed once the scan finds it.

    Leaving the room drops everything owed to it, as for a late event a row creates.
    """
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _turn(journal_store, "$source")
    (reply,) = await _adopt(principal)
    assert reply.legacy_pending is rl.LegacyPending.ADOPTION_SCAN
    if ended_by == "deletion":
        await admit(principal, "$redaction", redacts="$source", kind=EventKind.REDACTION, content={})
    else:
        await admit_room_membership(principal, ROOM, "leave", source=DepartureSource.LOCAL)
    gone = await _only_reply(principal)
    assert gone.state is rl.ReplyState.GONE
    assert gone.redaction_pending == ()

    await principal.finish_legacy_reply_read(reply.reply_id, rl.LegacyRead(event_id="$streamed"), now_ns=NOW)
    found = await _only_reply(principal)
    assert found.event_id == "$streamed"
    assert found.legacy_pending is None
    assert found.redaction_pending == (("$streamed",) if ended_by == "deletion" else ())


async def test_an_unsettled_stop_reaches_the_reply_it_named(journal_store: EventJournalStore) -> None:
    """A Stop main recorded but never finalized cancels the adopted reply with its note owed."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _turn(journal_store, "$source", response_event_id="$reply", stop_order=5)
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")

    # Cancelled, it still reads what it showed, so the cancel note goes below it.
    (reply,) = await _adopt(principal)
    assert reply.state is rl.ReplyState.CANCELLED
    assert reply.stop_receipt_order == 5
    assert reply.owed_write is not None
    assert reply.owed_write.note == rl._NOTE_CANCELLED
    assert not await principal.is_pending("$source")


async def test_an_unsettled_stop_on_a_finished_answer_is_kept_on_its_reply(journal_store: EventJournalStore) -> None:
    """The answer finished before main settled its Stop: the answer is adopted finished, and the Stop is kept."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await principal.settle_many(("$source",))
    await _turn(journal_store, "$source", completed=True, response_event_id="$reply", stop_order=5)

    await _adopt(principal)
    reply = await _only_reply(principal)
    assert reply.event_id == "$reply"
    assert reply.state is rl.ReplyState.COMPLETED
    assert reply.stop_receipt_order == 5
    assert not reply.unapplied_stop
    assert reply.owed_write is None


async def test_a_settled_stop_on_a_finished_answer_still_covers_edits_admitted_before_it(
    journal_store: EventJournalStore,
) -> None:
    """Main settled the Stop, but an edit it covers may still wait in the journal: the adopted answer keeps it."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await principal.settle_many(("$source",))
    await _turn(journal_store, "$source", completed=True, response_event_id="$reply", stop_order=5, stop_settled=True)

    await _adopt(principal)
    reply = await _only_reply(principal)
    assert reply.event_id == "$reply"
    assert reply.state is rl.ReplyState.COMPLETED
    assert reply.stop_receipt_order == 5
    assert reply.owed_write is None


@pytest.mark.parametrize("in_flight", ["cancelled_final", "stopped_approval"])
async def test_a_settled_stop_on_a_reply_still_in_flight_covers_edits_admitted_before_it(
    journal_store: EventJournalStore,
    in_flight: str,
) -> None:
    """Main settled the Stop before the reply it ended was done: the adopted reply keeps it for an older edit.

    The cancelled answer's FINAL was still owed, or the approval the Stop
    failed had not written its note yet.
    """
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await admit(principal, "$edit")
    await _turn(journal_store, "$source", response_event_id="$reply", stop_order=5, stop_settled=True)
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")
    if in_flight == "cancelled_final":
        await _row(principal, "$source", DeliveryStage.FINAL, "Partial", status="cancelled", edits="$reply")
    else:
        stopped = replace(_continuation("failing"), failure_reason="cancelled_by_user")
        await _main_continuation(principal, stopped)

    await _adopt(principal)
    reply = await _only_reply(principal)
    assert reply.stop_receipt_order == 5
    if in_flight == "cancelled_final":
        assert reply.state is rl.ReplyState.CANCELLED
        assert await principal.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
        await principal.acknowledge_matrix_delivery(
            delivery_id="$source",
            stage=DeliveryStage.FINAL,
            event_id="$final",
            delivered_projections=(),
        )
    else:
        assert reply.state is rl.ReplyState.PAUSED
        assert reply.approval_id == "approval-1"
    edit = rl.ClaimRequest(
        span_id="span-edit",
        delivery_id="$edit",
        sources=rl.SpanSources(pending=("$edit",), logical=("$source",)),
        bot_generation="gen-new",
        now_ns=NOW,
        new_reply_id="reply-edit",
        entity_name=ENTITY,
        room_id=ROOM,
        thread_id=None,
        membership_epoch=await principal.membership_epoch(ROOM),
        empty_presentation=encode_presentation(Presentation()),
        driving_edit_id="$edit",
    )

    # Received before the Stop, the edit regenerates nothing and leaves the stopped approval to its settlement.
    applied = await principal.replies.claim(edit, ClaimLookup(existing_event_id="$reply", edit_receipt_order=3))
    assert applied.transition.outcome is rl.Outcome.DUPLICATE
    assert applied.transition.effects == ()
    if in_flight == "stopped_approval":
        continuation = await principal.approval_continuation("approval-1")
        assert continuation is not None
        assert continuation.failure_reason == "cancelled_by_user"


async def test_a_stop_is_read_before_the_ledger_rewrites_its_turn(journal_store: EventJournalStore) -> None:
    """Adopted before the ledger loads, a Stop main kept on a turn record reaches its reply though the load drops it."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _turn(journal_store, "$source", response_event_id="$reply", stop_order=5)
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")
    runtime = ReplyRuntime(
        store=principal,
        entity_name=ENTITY,
        generation="gen-new",
        retry_sources=lambda _room_id, _sources: None,
        complete_turn=AsyncMock(),
        clean_up_superseded=lambda _continuation: None,
    )

    adopted = await runtime.adopt_legacy()
    # Loading the ledger rewrites the record through the current codec, which keeps no Stop.
    await _turn(journal_store, "$source", response_event_id="$reply")
    await runtime.start(adopted)

    reply = await _only_reply(principal)
    assert reply.state is rl.ReplyState.CANCELLED
    assert reply.stop_receipt_order == 5


async def test_command_turns_and_finished_answers_get_no_reply(journal_store: EventJournalStore) -> None:
    """Only agent and team turns with work left are adopted, and only at the first start."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$command")
    await _turn(journal_store, "$command", history=False)
    await _row(principal, "$command", DeliveryStage.INITIAL, "Working...", status="pending", acknowledged="$c")
    await admit(principal, "$done")
    await principal.settle_many(("$done",))
    await _turn(journal_store, "$done", completed=True, response_event_id="$d")
    await _row(principal, "$done", DeliveryStage.FINAL, "Done.", status="completed", acknowledged="$d")

    assert await _adopt(principal) == ()
    assert await principal.replies.for_sources(("$command",)) is None
    assert await principal.replies.for_sources(("$done",)) is None

    # A later start adopts nothing, even beside main-era rows.
    await admit(principal, "$late")
    await _turn(journal_store, "$late")
    await _row(principal, "$late", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$l")
    assert (
        await principal.adopt_legacy_replies(
            entity_name=ENTITY,
            presentations=LEGACY_PRESENTATIONS,
            now_ns=NOW,
        )
        == ()
    )
    assert await principal.replies.for_sources(("$late",)) is None


async def test_older_approvals_of_one_reply_are_superseded(journal_store: EventJournalStore) -> None:
    """Only the newest continuation pauses the reply; the older one is fenced, as an edit supersedes it.

    The original turn's rows created the same event, which stays one reply.
    """
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await admit(principal, "$edit")
    await _turn(journal_store, "$source")
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")
    await _main_continuation(principal, _continuation("waiting"))
    newer = replace(
        _continuation("waiting", approval_id="approval-2"),
        sources=ResponseSources(("$edit",), ("$source",)),
    )
    await _main_continuation(principal, newer, text="Rereading")

    await _adopt(principal)
    reply = await principal.replies.for_event("$reply")
    assert reply is not None
    assert reply.approval_id == "approval-2"
    assert _text(reply.presentation) == "Rereading"
    older = await principal.approval_continuation("approval-1")
    assert older is not None
    assert older.state == "failing"
    assert older.failure_reason == "superseded"
    # Its pause stays on the reply, superseded, and still holds its source for its cleanup.
    assert await _spans(principal, reply) == [
        (rl.SpanKind.TURN, rl.SpanOutcome.SUPERSEDED),
        (rl.SpanKind.TURN, rl.SpanOutcome.PAUSED),
    ]
    assert older.span_id is not None
    assert await principal.approval_continuation_for_source("$source") == older
    assert await principal.approval_continuation_for_source("$edit") == await principal.approval_continuation(
        "approval-2",
    )


async def test_an_adopted_regeneration_keeps_the_edit_it_selected(journal_store: EventJournalStore) -> None:
    """A paused regeneration main left still answers the edit it selected once its reply is adopted."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")
    selected = TurnRecord.create(
        ["$source"],
        source_event_revisions={"$source": (20, "$edit")},
        response_event_id="$reply",
        response_owner=ENTITY,
        conversation_target=MessageTarget.resolve(ROOM, None, "$source"),
    )
    paused = replace(
        _continuation("waiting"),
        sources=ResponseSources(("$source",), ("$source",), edit_receipt_order=20),
        prepared_edit_record=selected,
    )
    await _main_continuation(principal, paused)

    await _adopt(principal)
    adopted = await principal.approval_continuation("approval-1")
    assert adopted is not None
    assert adopted.span_id is not None
    assert adopted.prepared_edit_record == selected
    assert adopted.sources.edit_receipt_order == 20
    # A Stop received before the edit misses its regeneration and leaves the approval alone.
    stop = await principal.replies.record_stop("$reply", 10)
    assert stop is not None
    assert stop.transition.outcome is rl.Outcome.DUPLICATE
    assert stop.post_commit == ()
    assert (await principal.approval_continuation("approval-1")) == adopted


async def test_a_claimed_team_resume_keeps_its_document_instead_of_a_read(journal_store: EventJournalStore) -> None:
    """A team's resume restores the document its continuation kept, which a read of rendered text would lose."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")
    team_state = {"kind": "team_stream", "version": 2, "members": [], "consensus": "Reading document"}
    continuation = replace(_continuation("claimed"), entity_kind="team")
    await _main_continuation(principal, continuation, team_state=team_state)

    assert await _adopt(principal) == ()
    reply = await _only_reply(principal)
    assert reply.legacy_pending is None
    (answer,) = decode_presentation(reply.presentation).segments
    assert answer.team_state == team_state


@pytest.mark.parametrize("state", ["claimed", "interrupted"])
async def test_a_claimed_resume_is_left_running_for_approval_recovery(
    journal_store: EventJournalStore,
    state: str,
) -> None:
    """A resume the old instance was running stays current; approval recovery decides it, as after a crash.

    A shutdown marked the resume it cut short interrupted, for the next
    instance to hand back to replay: it was running too.
    """
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")
    continuation = (
        _continuation("claimed")
        if state == "claimed"
        else replace(_continuation("failing"), failure_reason=INTERRUPTED_FAILURE_REASON)
    )
    await _main_continuation(principal, continuation)

    (reply,) = await _adopt(principal)
    assert reply.state is rl.ReplyState.ACTIVE
    assert reply.legacy_pending is rl.LegacyPending.PRESENTATION_READ
    assert await _spans(principal, reply) == [
        (rl.SpanKind.TURN, rl.SpanOutcome.PAUSED),
        (rl.SpanKind.APPROVAL_RESUME, None),
    ]
    resume = await principal.replies.span(reply.current_span_id or "")
    assert resume is not None
    assert resume.approval_id == "approval-1"
    adopted = await principal.approval_continuation("approval-1")
    assert adopted is not None
    if state == "claimed":
        # The resume is the claim; the instance that stopped ran it.
        assert adopted.claim_span_id == resume.span_id
        assert (adopted.state, adopted.runtime_generation) == ("claimed", resume.bot_generation)
    else:
        assert adopted.claim_span_id is None
    assert await principal.replies.owner_lost("gen-new", now_ns=NOW) == ()


async def test_a_read_that_raises_counts_as_a_failed_pass_and_gives_up(journal_store: EventJournalStore) -> None:
    """A room history the bot cannot read neither escapes the pass nor keeps the reply waiting forever."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _turn(journal_store, "$source")
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")
    (reply,) = await _adopt(principal)
    resolved: list[str] = []
    reads = LegacyReplyReads(
        store=principal,
        client=MagicMock,
        response_sender=lambda: "@agent:example.org",
        trusted_sender_ids=tuple,
        logger=MagicMock(),
        resolved=resolved.append,
    )
    fetch = AsyncMock(side_effect=RuntimeError("M_FORBIDDEN"))
    with patch("mindroom.legacy_reply_messages.fetch_latest_visible_message", new=fetch):
        for _ in range(3):
            await reads.run()
    assert resolved == [reply.reply_id]
    assert (await _only_reply(principal)).legacy_pending is None


async def test_reads_after_sync_record_what_the_event_showed(journal_store: EventJournalStore) -> None:
    """A read that fails is retried on later passes; one that lands releases the reply."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _turn(journal_store, "$source")
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")
    (reply,) = await _adopt(principal)
    resolved: list[str] = []
    reads = LegacyReplyReads(
        store=principal,
        client=MagicMock,
        response_sender=lambda: "@agent:example.org",
        trusted_sender_ids=tuple,
        logger=MagicMock(),
        resolved=resolved.append,
    )
    message = MagicMock(
        body="Partial answer",
        content={"body": "Partial answer", "io.mindroom.stream_status": "streaming"},
        stream_status="streaming",
        timestamp=0,
        edited_timestamp=None,
    )
    fetch = AsyncMock(side_effect=[None, message])
    with patch("mindroom.legacy_reply_messages.fetch_latest_visible_message", new=fetch):
        await reads.run()
        assert resolved == []
        await reads.run()
    assert resolved == [reply.reply_id]
    read = await _only_reply(principal)
    assert read.legacy_pending is None
    assert _text(read.presentation) == "Partial answer"


@pytest.mark.parametrize(
    ("body", "status", "placeholder_only"),
    [
        ("Thinking...", "pending", True),
        ("Partial\n\n**[Response interrupted]**", "error", False),
    ],
)
async def test_a_superseded_replay_removes_an_adopted_event_only_when_it_showed_the_placeholder(
    journal_store: EventJournalStore,
    body: str,
    status: str,
    placeholder_only: bool,
) -> None:
    """An event an earlier release ended with a note keeps that content when a newer message supersedes its replay."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _turn(journal_store, "$source")
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")
    await _adopt(principal)
    reads = LegacyReplyReads(
        store=principal,
        client=MagicMock,
        response_sender=lambda: "@agent:example.org",
        trusted_sender_ids=tuple,
        logger=MagicMock(),
        resolved=lambda _reply_id: None,
    )
    message = MagicMock(
        body=body,
        content={"body": body, "io.mindroom.stream_status": status},
        stream_status=status,
        timestamp=0,
        edited_timestamp=None,
    )
    with patch("mindroom.legacy_reply_messages.fetch_latest_visible_message", new=AsyncMock(return_value=message)):
        await reads.run()
    read = await _only_reply(principal)
    assert read.legacy_pending is None
    assert read.placeholder_only is placeholder_only
    assert read.last_span_id is not None
    last = await principal.replies.span(read.last_span_id)
    assert last is not None

    superseded = rl.replay_superseded(read, last, durable_write_debt=False, now_ns=NOW)

    assert superseded.reply is not None
    if placeholder_only:
        assert superseded.reply.state is rl.ReplyState.GONE
        assert superseded.reply.redaction_pending == ("$reply",)
    else:
        assert superseded.reply.state is rl.ReplyState.FAILED
        assert superseded.reply.redaction_pending == ()


async def test_a_selection_an_earlier_release_acknowledged_resolves_its_retried_acknowledgement(
    journal_store: EventJournalStore,
) -> None:
    """The replayed selection finds the acknowledgement that release sent, instead of failing to write another."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _turn(journal_store, "$source")
    ack = "You selected: 1 Yes\n\nProcessing your response..."
    await _row(principal, "$source", DeliveryStage.INITIAL, ack, status="pending", acknowledged="$ack")
    (adopted,) = await _adopt(principal)
    claim = rl.ClaimRequest(
        span_id="span-retry",
        delivery_id="$source",
        sources=rl.SpanSources(pending=("$source",), logical=("$source",)),
        bot_generation="gen-new",
        now_ns=NOW,
        new_reply_id="reply-retry",
        entity_name=ENTITY,
        room_id=ROOM,
        thread_id=None,
        membership_epoch=await principal.membership_epoch(ROOM),
        empty_presentation=encode_presentation(Presentation()),
    )

    resolved = await principal.enqueue_reply_row(
        request=ReplyRowRequest(
            reply_id="reply-retry",
            span_id="span-retry",
            decide=None,
            create=ReplyCreation(claim=claim, shown=encode_presentation(Presentation(placeholder=ack))),
        ),
        room_id=ROOM,
        thread_id=None,
        payload={"msgtype": "m.text", "body": ack},
    )

    assert resolved is not None
    assert resolved.applied.transition.outcome is rl.Outcome.DUPLICATE
    assert resolved.applied.transition.reply is not None
    assert resolved.applied.transition.reply.reply_id == adopted.reply_id
    assert resolved.sequence == 1
    reply = await _only_reply(principal)
    assert reply.event_id == "$ack"
    assert reply.confirmed


async def test_a_coalesced_turn_in_flight_is_adopted_once(journal_store: EventJournalStore) -> None:
    """A batch of two messages, whose rows that release keyed by the later one, becomes one reply."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$first")
    await admit(principal, "$second")
    await _turn(journal_store, "$first", coalesced=("$second",))
    await _row(principal, "$second", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")

    adopted = await _adopt(principal)

    assert len(adopted) == 1
    assert adopted[0].event_id == "$reply"
    assert adopted[0].legacy_pending is rl.LegacyPending.PRESENTATION_READ


async def test_a_read_that_gave_up_never_marks_the_event_as_only_a_placeholder(
    journal_store: EventJournalStore,
) -> None:
    """Nothing known about what the event shows, a later removal keeps it instead of redacting it."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _turn(journal_store, "$source")
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")
    (adopted,) = await _adopt(principal)
    assert adopted.placeholder_only

    await principal.finish_legacy_reply_read(adopted.reply_id, rl.LegacyRead(), now_ns=NOW)

    read = await _only_reply(principal)
    assert read.legacy_pending is None
    assert read.event_id == "$reply"
    assert not read.placeholder_only


@pytest.mark.parametrize("recent", [True, False])
async def test_a_settled_stream_gets_the_restart_note_only_within_the_stale_stream_window(
    journal_store: EventJournalStore,
    recent: bool,
) -> None:
    """That release's cleanup noted a stream it stopped within six hours; an older one keeps what it shows."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await principal.settle_many(("$source",))
    await _turn(journal_store, "$source")
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")
    (adopted,) = await _adopt(principal)
    reads = LegacyReplyReads(
        store=principal,
        client=MagicMock,
        response_sender=lambda: "@agent:example.org",
        trusted_sender_ids=tuple,
        logger=MagicMock(),
        resolved=lambda _reply_id: None,
    )
    edited_ms = int(time.time() * 1000) - (60_000 if recent else 7 * 60 * 60 * 1000)
    message = MagicMock(
        body="Partial answer",
        content={"body": "Partial answer", "io.mindroom.stream_status": "streaming"},
        stream_status="streaming",
        timestamp=edited_ms,
        edited_timestamp=None,
    )
    with patch("mindroom.legacy_reply_messages.fetch_latest_visible_message", new=AsyncMock(return_value=message)):
        await reads.run()

    ended = await _only_reply(principal)
    assert ended.reply_id == adopted.reply_id
    assert ended.state is rl.ReplyState.FAILED
    assert (ended.owed_write is not None) is recent
