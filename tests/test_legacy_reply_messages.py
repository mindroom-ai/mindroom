"""Replies main left in flight get reply records once, and learn what only Matrix shows after sync."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom import reply_lifecycle as rl
from mindroom.event_journal import ApprovalContinuation, DeliveryStage
from mindroom.handled_turns import TurnRecordCodec
from mindroom.history.types import HistoryScope
from mindroom.legacy_reply_messages import LEGACY_PRESENTATIONS, LegacyReplyReads
from mindroom.message_target import MessageTarget
from mindroom.reply_presentation import Presentation, Segment, decode_presentation, encode_presentation
from mindroom.response_sources import ResponseSources
from mindroom.turn_record import TurnRecord, canonicalize_turn_record
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
    completed: bool = False,
    response_event_id: str | None = None,
    stop_order: int | None = None,
    history: bool = True,
) -> TurnRecord:
    """Store a main-era turn record of this agent, as main's ledger wrote it."""
    record = TurnRecord.create(
        [source],
        requester_id="@user:example.org",
        completed=completed,
        response_event_id=response_event_id,
        conversation_target=MessageTarget.resolve(ROOM, None, source, room_mode=True),
        history_scope=HistoryScope(kind="agent", scope_id=ENTITY) if history else None,
    )
    if stop_order is not None:
        record = canonicalize_turn_record(record, user_stop_receipt_order=stop_order)
    assert record.anchor_event_id is not None
    await journal_store.turn_records(ENTITY).upsert(
        index_event_ids=record.indexed_event_ids,
        anchor_event_id=record.anchor_event_id,
        record_json=json.dumps(TurnRecordCodec._to_ledger_record(record)),
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


def _continuation(
    state: str,
    *,
    approval_id: str = "approval-1",
    text: str = "Reading document",
) -> ApprovalContinuation:
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
        response_text=text,
    )


async def _adopt(principal: PrincipalStore) -> tuple[rl.Reply, ...]:
    await principal.replies.write_generation("gen-new", now_ns=NOW)
    await principal.adopt_legacy_replies(entity_name=ENTITY, presentations=LEGACY_PRESENTATIONS, now_ns=NOW)
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


async def test_a_waiting_approval_pauses_its_reply_with_what_it_showed(journal_store: EventJournalStore) -> None:
    """The continuation's kept presentation becomes the paused reply's; the approval runtime stays its owner."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")
    assert await principal.create_approval_continuation(_continuation("waiting")) is not None

    assert await _adopt(principal) == ()
    reply = await _only_reply(principal)
    assert reply.state is rl.ReplyState.PAUSED
    assert reply.event_id == "$reply"
    assert reply.approval_id == "approval-1"
    assert _text(reply.presentation) == "Reading document"
    assert await _spans(principal, reply) == [(rl.SpanKind.TURN, rl.SpanOutcome.PAUSED)]
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
    assert ended.owed_write.note == rl.NOTE_RESTART


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
    assert reply.owed_write.note == rl.NOTE_CANCELLED
    assert not await principal.is_pending("$source")


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
    """Only the newest continuation pauses the reply; the older one is fenced, as an edit supersedes it."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await admit(principal, "$edit")
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")
    assert await principal.create_approval_continuation(_continuation("waiting")) is not None
    newer = replace(
        _continuation("waiting", approval_id="approval-2", text="Rereading"),
        sources=ResponseSources(("$edit",), ("$source",)),
    )
    assert await principal.create_approval_continuation(newer) is not None

    await _adopt(principal)
    reply = await principal.replies.for_event("$reply")
    assert reply is not None
    assert reply.approval_id == "approval-2"
    assert _text(reply.presentation) == "Rereading"
    older = await principal.approval_continuation("approval-1")
    assert older is not None
    assert older.state == "failing"
    assert older.failure_reason == "superseded"


async def test_a_claimed_resume_is_left_running_for_approval_recovery(journal_store: EventJournalStore) -> None:
    """A resume the old instance was running stays current; main's approval recovery decides it, as after a crash."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")
    assert await principal.create_approval_continuation(_continuation("claimed")) is not None

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
    assert await principal.replies.owner_lost("gen-new", now_ns=NOW) == ()


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
