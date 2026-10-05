"""Reply records persist exactly on both journal backends."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom import reply_lifecycle as rl
from mindroom.event_journal import reply_messages, reply_spans
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
from tests.test_event_journal_store import ROOM, admit

if TYPE_CHECKING:
    from mindroom.event_journal import EventJournalStore

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
    applied = await principal.replies.apply(transition)
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
    await principal.replies.apply(rl.Transition(outcome=rl.Outcome.APPLIED, reply=reply))

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
    await principal.replies.apply(transition)
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
    await principal.replies.apply(first)
    second = rl.claim(
        replace(_request("span-2", reply_id="reply-2"), now_ns=20),
        ClaimContext(None, None, None, None, durable_write_debt=False, active_generation="gen-1"),
    )
    await principal.replies.apply(second)

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
    principal = journal_store.principal(PRINCIPAL)
    transition = _first_claim()
    await principal.replies.apply(transition)
    assert transition.reply is not None
    active = await journal_store.backend.read(
        lambda tx: reply_messages.for_room(tx, PRINCIPAL, ROOM, states=(ReplyState.ACTIVE, ReplyState.PAUSED)),
    )
    assert [reply.reply_id for reply in active] == ["reply-1"]
    assert await journal_store.backend.read(lambda tx: reply_messages.with_pending_work(tx, PRINCIPAL)) == ()
    owed = replace(transition.reply, owed_write=OwedWrite("span-1", rl.NOTE_RESTART))
    await principal.replies.apply(rl.Transition(outcome=rl.Outcome.APPLIED, reply=owed))
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
    await principal.replies.apply(claim)
    assert claim.reply is not None
    assert claim.claimed is not None
    assert await principal.is_pending("$source")

    finished = rl.finish(
        claim.reply,
        claim.claimed,
        rl.TerminalWrite(shown="answer", prepared_revision=claim.reply.revision, state=ReplyState.COMPLETED),
        now_ns=30,
    )
    await principal.replies.apply(finished)

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
    await principal.replies.apply(claim)
    assert claim.reply is not None
    stop = rl.stop(claim.reply, claim.claimed, rl.StopFacts(3, newer_edit=False, span_live=True), now_ns=40)
    applied = await principal.replies.apply(stop)
    assert applied.post_commit == (rl.CancelSpan("span-1"),)
    stored = await principal.replies.load("reply-1")
    assert stored is not None
    assert stored.stop_receipt_order == 3
