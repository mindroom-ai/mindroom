"""Replies an earlier release left paused for approval get reply records once; nothing else was in flight."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Literal

import pytest

from mindroom import reply_lifecycle as rl
from mindroom.event_journal import ApprovalContinuation, DeliveryStage, turn_records
from mindroom.handled_turns import TurnRecordCodec
from mindroom.history.types import HistoryScope
from mindroom.legacy_reply_messages import LEGACY_PRESENTATIONS
from mindroom.message_target import MessageTarget
from mindroom.reply_presentation import decode_presentation
from mindroom.response_sources import ResponseSources
from mindroom.tool_system.events import ToolTraceEntry
from mindroom.turn_record import TurnRecord
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
    completed: bool = False,
    response_event_id: str | None = None,
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
    anchor_event_id = record.anchor_event_id
    assert anchor_event_id is not None
    # Written as main's ledger stored it, past the current codec.
    await journal_store.backend.write(
        lambda transaction: turn_records.upsert(
            transaction,
            ENTITY,
            index_event_ids=record.indexed_event_ids,
            anchor_event_id=anchor_event_id,
            record_json=json.dumps(TurnRecordCodec._to_ledger_record(record)),
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


async def _adopt(principal: PrincipalStore) -> None:
    await principal.replies.write_generation("gen-new", now_ns=NOW)
    await principal.adopt_legacy_replies(entity_name=ENTITY, presentations=LEGACY_PRESENTATIONS, now_ns=NOW)


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

    A team's resume restores the document kept here.
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

    await _adopt(principal)
    reply = await _only_reply(principal)
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


async def test_only_approvals_are_adopted_and_only_at_the_first_start(journal_store: EventJournalStore) -> None:
    """An upgrade runs while no reply is in flight: turns and rows an earlier release left get no reply."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$done")
    await principal.settle_many(("$done",))
    await _turn(journal_store, "$done", completed=True, response_event_id="$d")
    await _row(principal, "$done", DeliveryStage.FINAL, "Done.", status="completed", acknowledged="$d")
    await admit(principal, "$command")
    await _turn(journal_store, "$command", history=False)

    await _adopt(principal)
    assert await principal.replies.for_sources(("$done",)) is None
    assert await principal.replies.for_sources(("$command",)) is None

    # A later start adopts nothing, even an approval an earlier release left.
    await admit(principal, "$source")
    await _main_continuation(principal, _continuation("waiting"))
    assert (
        await principal.adopt_legacy_replies(entity_name=ENTITY, presentations=LEGACY_PRESENTATIONS, now_ns=NOW) == ()
    )
    assert await principal.replies.for_sources(("$source",)) is None


async def test_older_approvals_of_one_reply_are_discarded_with_their_sources_settled(
    journal_store: EventJournalStore,
) -> None:
    """Only the newest continuation pauses the reply; an older one is dropped, its source settled, its cards gone."""
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
    assert (reply.state, reply.approval_id) == (rl.ReplyState.PAUSED, "approval-2")
    assert _text(reply.presentation) == "Rereading"
    assert await _spans(principal, reply) == [(rl.SpanKind.TURN, rl.SpanOutcome.PAUSED)]
    assert await principal.approval_continuation("approval-1") is None
    assert not await principal.is_pending("$source")
    assert await principal.approval_continuation_for_source("$edit") == await principal.approval_continuation(
        "approval-2",
    )


@pytest.mark.parametrize(
    ("reason", "state"),
    [("expired", rl.ReplyState.FAILED), ("cancelled_by_user", rl.ReplyState.CANCELLED)],
)
async def test_a_failing_approval_whose_note_was_delivered_is_adopted_paused_and_its_cleanup_ends_it(
    journal_store: EventJournalStore,
    reason: str,
    state: rl.ReplyState,
) -> None:
    """The room already shows the failure note: the owed cleanup ends the reply without writing it again."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")
    await _main_continuation(principal, replace(_continuation("failing"), failure_reason=reason))
    await _row(
        principal,
        "$source",
        DeliveryStage.FINAL,
        "Approval failed.",
        status="error",
        edits="$reply",
        acknowledged="$note",
    )

    await _adopt(principal)
    assert (await _only_reply(principal)).state is rl.ReplyState.PAUSED
    assert await principal.finish_approval_continuation("approval-1") is not None
    ended = await _only_reply(principal)
    assert (ended.state, ended.approval_id, ended.owed_write) == (state, None, None)
    assert not await principal.is_pending("$source")


async def test_a_stored_superseded_approval_is_discarded_at_adoption(journal_store: EventJournalStore) -> None:
    """An approval an earlier release superseded and never cleaned up is dropped, its source settled."""
    principal = journal_store.principal(PRINCIPAL)
    await admit(principal, "$source")
    await _row(principal, "$source", DeliveryStage.INITIAL, "Thinking...", status="pending", acknowledged="$reply")
    await _main_continuation(principal, replace(_continuation("failing"), failure_reason="superseded"))

    assert (
        await principal.adopt_legacy_replies(entity_name=ENTITY, presentations=LEGACY_PRESENTATIONS, now_ns=NOW) == ()
    )
    assert await principal.approval_continuation("approval-1") is None
    assert not await principal.is_pending("$source")
    assert await principal.replies.for_event("$reply") is None


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
