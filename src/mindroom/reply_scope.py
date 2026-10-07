"""The in-memory side of one span: its handle, its presentation, and its claim and exits.

A span is one executor's claim on a durable reply. The handle
lives in a context variable for the task that runs the span, so the attempt
task, the streamer's tasks, and the delivery gateway all see the same span
without threading it through every call. The reply record is the durable
owner; the handle only caches what the span last committed, so it can render
payloads for the revision it knows and learn when a Stop changed it.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom import reply_lifecycle as rl
from mindroom.event_journal.approval_continuations import SUPERSEDED_FAILURE_REASON
from mindroom.event_journal.replies import AppliedTransition, ClaimLookup, Decide, ReplyCreation, TurnCompleted
from mindroom.event_journal.turn_records import encode_prepared_edit
from mindroom.legacy_reply_messages import LEGACY_PRESENTATIONS
from mindroom.reply_presentation import (
    AGENT_PLACEHOLDER,
    Presentation,
    Segment,
    after_restart,
    continued_by,
    decode_presentation,
    encode_presentation,
    render_body,
    shown_work,
    with_answer,
)
from mindroom.stop import SpanRegistry
from mindroom.streaming import ProgressPermission, UnfinishedStreamedReply
from mindroom.tool_system.events import remap_visible_tool_marker_indices

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping

    from mindroom.event_journal import ApprovalContinuation, PrincipalStore
    from mindroom.event_journal.replies import PostCommitEffect
    from mindroom.matrix_delivery import ReplyRowEnqueuer
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.turn_record import TurnRecord


@dataclass
class SpanHandle:
    """One running span, as the task that executes it knows it."""

    runtime: ReplyRuntime
    span: rl.Span
    # The reply as this span last committed or read it.
    reply: rl.Reply
    # What earlier spans left above this span's answer.
    base: Presentation
    # What a stopped attempt showed, for the streamer to continue below.
    resumed: UnfinishedStreamedReply | None = None
    # The span's last direct progress edit that Matrix accepted and no durable
    # write has recorded yet.
    unconfirmed_progress: rl.ProgressConfirmation | None = None

    @property
    def exited(self) -> bool:
        """Return whether a committed transition ended the span, as this task knows it."""
        return self.span.ended

    @property
    def span_id(self) -> str:
        """Return the span's identity."""
        return self.span.span_id

    @property
    def reply_id(self) -> str:
        """Return the reply this span writes."""
        return self.reply.reply_id

    def note(self, applied: AppliedTransition) -> None:
        """Remember what a committed transition left, for later renders."""
        transition = applied.transition
        if transition.reply is not None and transition.reply.reply_id == self.reply.reply_id:
            self.reply = transition.reply
        for span in transition.spans:
            if span.span_id == self.span.span_id:
                self.span = span
        if transition.applied and (transition.row is not None or self.span.ended):
            # A durable write recorded the last progress edit's confirmation.
            self.unconfirmed_progress = None

    def _answer_segment(
        self,
        text: str,
        tool_trace: tuple[ToolTraceEntry, ...],
        *,
        team_state: Mapping[str, object] | None = None,
    ) -> Segment:
        """Return this span's answer as one segment, its markers counted from one."""
        resumed = self.resumed
        own_text = text
        own_trace = tool_trace
        if resumed is not None:
            own_text = text.removeprefix(resumed.resumed_text)
            resumed_count = len(resumed.tool_trace)
            own_trace = tool_trace[resumed_count:]
            if resumed_count and own_trace:
                own_text = remap_visible_tool_marker_indices(
                    own_text,
                    {index + resumed_count: index for index in range(1, len(own_trace) + 1)},
                )
        return Segment(
            kind="answer",
            text=own_text,
            span_id=self.span.span_id,
            tool_trace=own_trace,
            team_state=team_state,
        )

    def presentation(
        self,
        text: str,
        tool_trace: tuple[ToolTraceEntry, ...],
        *,
        team_state: Mapping[str, object] | None = None,
        trailing_note: Segment | None = None,
    ) -> Presentation:
        """Return the whole reply as it would show with this span's answer so far."""
        answer = self._answer_segment(text, tool_trace, team_state=team_state)
        presentation = with_answer(self.base, answer) if answer.text.strip() or answer.tool_trace else self.base
        return replace(presentation, trailing_note=trailing_note)


@dataclass
class SpanSlot:
    """The scope's holder for the span its claim opens; shared with child tasks."""

    handle: SpanHandle | None = None


_current_slot: ContextVar[SpanSlot | None] = ContextVar("mindroom_reply_span", default=None)


def current_span() -> SpanHandle | None:
    """Return the span the current task executes, if any."""
    slot = _current_slot.get()
    return None if slot is None else slot.handle


def current_slot() -> SpanSlot | None:
    """Return the slot of the span scope the current task runs in, if any."""
    return _current_slot.get()


def _unfinished_from(shown: Presentation) -> UnfinishedStreamedReply | None:
    """Describe what a reply showed in the shape the streamer continues below."""
    work = shown_work(shown)
    if work is None:
        return None
    return UnfinishedStreamedReply(visible_text=work.text, tool_trace=work.tool_trace)


class ClaimRefused(Enum):
    """Why a claim opened no span."""

    # The reply cannot be claimed yet; what blocks it retries the sources once it resolves.
    DEFERRED = "deferred"
    # Nothing runs for these sources: the reply already answered the edit, a
    # Stop ended the reply or covers the edit, or a Stop ended a selection.
    NOTHING_TO_RUN = "nothing_to_run"
    # Another bot instance took this principal's replies over; it replays the sources.
    RETIRED = "retired"


# As long as the handled-turn ledger keeps a turn: nothing reaches a finished
# reply after its turn is forgotten.
_FINISHED_REPLY_RETENTION_NS = 30 * 24 * 60 * 60 * 1_000_000_000
_FORGET_BATCH = 500


@dataclass
class ReplyRuntime:
    """One bot instance's owner of its replies' claims and span exits."""

    store: PrincipalStore
    entity_name: str
    generation: str
    retry_sources: Callable[[str, tuple[str, ...]], None]
    # Tells the turn ledger about a turn a reply's settlement already recorded answered.
    complete_turn: Callable[[TurnRecord], Awaitable[object]]
    # Starts the cleanup of an approval an edit superseded, outside any conversation.
    clean_up_superseded: Callable[[ApprovalContinuation], None]
    clock: Callable[[], int] = field(default=time.time_ns)
    # The task of each span this bot instance executes, which a Stop cancels.
    spans: SpanRegistry = field(default_factory=SpanRegistry)
    # Sources whose claim waited until the reply could be claimed, by reply.
    _waiting_claims: dict[str, list[tuple[str, tuple[str, ...]]]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    async def run_effects(self, effects: tuple[PostCommitEffect, ...]) -> None:
        """Run what committed reply transitions left for after their commit.

        Span cancels run last: the acknowledgement that applies a Stop can run
        in the span's own task, whose next await the cancel interrupts.
        """
        for effect in effects:
            if isinstance(effect, TurnCompleted):
                await self.complete_turn(effect.record)
            elif isinstance(effect, rl.WakeApproval):
                await self._wake_fenced_approval(effect.approval_id)
        for effect in effects:
            if isinstance(effect, rl.CancelSpan):
                self.spans.cancel(effect.span_id, cancel_source="user_stop" if effect.by_stop else None)

    async def finish_approval(self, approval_id: str) -> bool:
        """Finish a paused run once its FINAL is terminal, settling its turn; return whether it finished."""
        finished = await self.store.finish_approval_continuation(approval_id)
        if finished is None:
            return False
        await self.run_effects(finished.post_commit)
        self._retry_waiting_claims()
        return True

    async def release_approval(self, approval_id: str, expected_generation: int) -> bool:
        """Release an interrupted run's approval as ``rl.approval_released`` decides; return whether it was released."""
        released = await self.store.release_approval_continuation(approval_id, expected_generation=expected_generation)
        if released is None:
            return False
        await self.run_effects(released.post_commit)
        self._retry_waiting_claims()
        return True

    def _retry_waiting_claims(self) -> None:
        """A claim an approval held back may run now that the run is gone."""
        for reply_id in tuple(self._waiting_claims):
            self.claim_may_proceed(reply_id)

    async def _wake_fenced_approval(self, approval_id: str) -> None:
        """Run a fenced approval's failure settlement."""
        continuation = await self.store.approval_continuation(approval_id)
        if continuation is None:
            return
        if continuation.failure_reason == SUPERSEDED_FAILURE_REASON:
            # An edit superseded it: nothing of it is shown any more, so its
            # cleanup runs now, outside the conversation the regeneration holds.
            self.clean_up_superseded(continuation)
            return
        # Its source worker settles it once whatever owns the source lets go of it.
        self.retry_sources(continuation.room_id, continuation.source_event_ids)

    async def _wait_to_claim(
        self,
        reply_id: str,
        room_id: str,
        sources: tuple[str, ...],
        *,
        driving_edit: bool,
    ) -> None:
        """Retry sources once what blocked their claim on the reply is gone, instead of retrying at once.

        The wait registers first and then rechecks, so a resolution that landed
        in between still retries them. A retry claims again and may wait again.
        """
        self._waiting_claims.setdefault(reply_id, []).append((room_id, sources))
        reply = await self.store.replies.load(reply_id)
        if reply is None or not rl.claim_blocked(
            reply,
            durable_write_debt=await self.store.replies.has_unresolved_rows(reply_id),
            driving_edit=driving_edit,
        ):
            # A note still owed and not yet enqueued wakes it when its row
            # resolves; an approval's finish or release wakes it when the run is gone.
            self.claim_may_proceed(reply_id)

    def claim_may_proceed(self, reply_id: str) -> None:
        """Retry the claims that waited on this reply."""
        for room_id, sources in self._waiting_claims.pop(reply_id, ()):
            self.retry_sources(room_id, sources)

    async def committed(self, applied: AppliedTransition, handle: SpanHandle | None = None) -> AppliedTransition:
        """Remember what a committed transition left on the span, then run its post-commit effects."""
        if handle is not None:
            handle.note(applied)
        await self.run_effects(applied.post_commit)
        return applied

    async def live_event_ids(self, room_id: str) -> frozenset[str]:
        """Return the events of a room's replies whose span runs in this bot instance."""
        return await self.store.replies.event_ids_of_spans(room_id, self.spans.live_span_ids())

    async def departed(self, room_id: str) -> None:
        """Cancel the spans this instance runs or starts in a room the bot left; the departure ended their replies."""
        for span_id in await self.store.replies.spans_in_room(room_id, self.spans.claimed_span_ids()):
            self.spans.cancel(span_id, cancel_source=None)

    async def deletions_ended(self) -> tuple[str, ...]:
        """Cancel the spans source deletions ended; return the replies they ended, whose debt is now due.

        Each tombstone's projection ended its replies in its own commit and recorded the span it cancelled.
        """
        endings = await self.store.replies.take_deletion_endings()
        for ending in endings:
            if ending.span_id is not None:
                self.spans.cancel(ending.span_id, cancel_source=None)
        return tuple(ending.reply_id for ending in endings)

    async def forget_finished(self) -> None:
        """Drop the records of replies finished as long ago as the handled-turn ledger forgets their turns."""
        before_ns = self.clock() - _FINISHED_REPLY_RETENTION_NS
        while await self.store.replies.forget_finished(before_ns=before_ns, limit=_FORGET_BATCH) == _FORGET_BATCH:
            pass

    async def take_ownership(self, adopted: tuple[AppliedTransition, ...] = ()) -> None:
        """Make this bot instance the owner of its principal's replies, before it writes any of them.

        Then it runs what an earlier :meth:`adopt_legacy` left to run.
        """
        await self.store.replies.write_generation(self.generation, now_ns=self.clock())
        for applied in adopted:
            await self.run_effects(applied.post_commit)

    async def adopt_legacy(self) -> tuple[AppliedTransition, ...]:
        """Give the replies an earlier release left in flight records, once per principal; return what to run after.

        It reads the Stop keys that release kept on turn records, so the bot
        runs it before the turn ledger loads and rewrites them, and hands what
        it returns to :meth:`start`.
        """
        return await self.store.adopt_legacy_replies(
            entity_name=self.entity_name,
            presentations=LEGACY_PRESENTATIONS,
            now_ns=self.clock(),
        )

    async def start(self, adopted: tuple[AppliedTransition, ...] = ()) -> None:
        """Make this bot instance the owner of its principal's replies, then end what older instances left running.

        Runs before journal replay: replies an earlier release left in flight
        get records first (``adopted`` is what an earlier :meth:`adopt_legacy`
        left to run), replay claims continue the replies whose sources are
        still pending, and the notes this owes are delivered by the outbox
        recovery after each room syncs.
        """
        await self.take_ownership()
        # No span a deletion ended before this start survived it; recovery delivers what their replies owe.
        await self.store.replies.take_deletion_endings()
        adopted = (*adopted, *await self.adopt_legacy())
        for applied in (*adopted, *await self.store.replies.owner_lost(self.generation, now_ns=self.clock())):
            await self.run_effects(applied.post_commit)

    @asynccontextmanager
    async def span_scope(self) -> AsyncIterator[SpanSlot]:
        """Open the slot a claim inside fills, so child tasks share the span."""
        slot = SpanSlot()
        token = _current_slot.set(slot)
        try:
            yield slot
        finally:
            _current_slot.reset(token)
            if slot.handle is not None:
                self.spans.forget(slot.handle.span_id)

    async def claim(
        self,
        *,
        delivery_id: str,
        sources: rl.SpanSources,
        room_id: str,
        thread_id: str | None,
        placeholder: str = AGENT_PLACEHOLDER,
        show_tool_calls: bool = True,
        driving_edit_id: str | None = None,
        edit_receipt_order: int | None = None,
        existing_event_id: str | None = None,
        approval_id: str | None = None,
        interactive_span_id: str | None = None,
        prepared_edit: TurnRecord | None = None,
    ) -> SpanHandle | ClaimRefused:
        """Claim the reply one span answers, or say why no span opened."""
        empty = Presentation(placeholder=placeholder, show_tool_calls=show_tool_calls)
        request = replace(
            await self._claim_request(
                delivery_id=delivery_id,
                sources=sources,
                room_id=room_id,
                thread_id=thread_id,
                empty=empty,
            ),
            driving_edit_id=driving_edit_id,
            approval_id=approval_id,
            interactive_span_id=interactive_span_id,
            prepared_edit=None if prepared_edit is None else encode_prepared_edit(prepared_edit),
        )
        try:
            applied = await self.committed(
                await self.store.replies.claim(
                    request,
                    ClaimLookup(
                        interactive_span_id=interactive_span_id,
                        existing_event_id=existing_event_id,
                        edit_receipt_order=edit_receipt_order,
                    ),
                ),
            )
        except BaseException:
            self.spans.forget(request.span_id)
            raise
        transition = applied.transition
        if transition.claimed is None or transition.claimed.span_id != request.span_id:
            # Refused, or continuing the span an interactive selection acknowledged.
            self.spans.forget(request.span_id)
        if transition.outcome is rl.Outcome.STALE:
            return ClaimRefused.RETIRED
        if transition.outcome is rl.Outcome.DUPLICATE:
            return ClaimRefused.NOTHING_TO_RUN
        if transition.claimed is None or transition.reply is None:
            # Earlier writes of this reply are unresolved; their resolution
            # wakes these sources instead of waiting under the conversation lock.
            assert transition.reply is not None, "only a reply with earlier writes defers a claim"
            await self._wait_to_claim(
                transition.reply.reply_id,
                room_id,
                sources.pending,
                driving_edit=driving_edit_id is not None,
            )
            return ClaimRefused.DEFERRED
        return _handle_for(self, transition.reply, transition.claimed, empty)

    async def adopt_historical_answer(
        self,
        event_id: str,
        *,
        sources: rl.SpanSources,
        room_id: str,
        thread_id: str | None,
    ) -> None:
        """Give an answer older than the reply records its reply, before an edit prunes the history naming it.

        Its span is keyed by the answer's event, which no edit driving a later span shares.
        """
        request = await self._new_request(
            delivery_id=event_id,
            sources=sources,
            room_id=room_id,
            thread_id=thread_id,
            empty=Presentation(),
        )
        await self.store.replies.adopt_historical_answer(request, event_id)

    async def _claim_request(
        self,
        *,
        delivery_id: str,
        sources: rl.SpanSources,
        room_id: str,
        thread_id: str | None,
        empty: Presentation,
    ) -> rl.ClaimRequest:
        """Return a claim by this bot instance, with fresh identities for the span and any reply it creates.

        A Stop can reach the span as soon as its claim commits, before its
        task registers, so the span registry expects it from here.
        """
        request = await self._new_request(
            delivery_id=delivery_id,
            sources=sources,
            room_id=room_id,
            thread_id=thread_id,
            empty=empty,
        )
        self.spans.expect(request.span_id)
        return request

    async def _new_request(
        self,
        *,
        delivery_id: str,
        sources: rl.SpanSources,
        room_id: str,
        thread_id: str | None,
        empty: Presentation,
    ) -> rl.ClaimRequest:
        return rl.ClaimRequest(
            span_id=_new_id(),
            delivery_id=delivery_id,
            sources=sources,
            bot_generation=self.generation,
            now_ns=self.clock(),
            new_reply_id=_new_id(),
            entity_name=self.entity_name,
            room_id=room_id,
            thread_id=thread_id,
            membership_epoch=await self.store.membership_epoch(room_id),
            empty_presentation=encode_presentation(empty),
        )

    async def claim_approval_resume(
        self,
        continuation: ApprovalContinuation,
        *,
        placeholder: str,
    ) -> tuple[ApprovalContinuation | None, SpanHandle | None]:
        """Claim a ready continuation with its paused reply's resume span.

        Returns neither when the continuation is not ready, or when the reply's
        earlier writes are unresolved, which retry the sources once they resolve.
        """
        empty = Presentation(placeholder=placeholder, show_tool_calls=continuation.show_tool_calls)
        sources = continuation.sources
        claim = await self._claim_request(
            delivery_id=continuation.source_event_ids[0],
            sources=rl.SpanSources(
                pending=sources.pending_event_ids,
                logical=sources.logical_source_event_ids,
                discovery=sources.discovery_event_ids,
            ),
            room_id=continuation.room_id,
            thread_id=continuation.thread_id,
            empty=empty,
        )
        try:
            claimed, applied = await self.store.claim_approval_resume(
                continuation.approval_id,
                claim=claim,
            )
            if applied is None:
                self.spans.forget(claim.span_id)
                return None, None
            transition = (await self.committed(applied)).transition
        except BaseException:
            self.spans.forget(claim.span_id)
            raise
        if claimed is None or transition.claimed is None or transition.reply is None:
            self.spans.forget(claim.span_id)
            if transition.outcome is rl.Outcome.STALE:
                # Another instance took the replies over; its approval recovery resumes this.
                return None, None
            assert transition.reply is not None, "only a reply with earlier writes defers a resume"
            await self._wait_to_claim(
                transition.reply.reply_id,
                continuation.room_id,
                sources.pending_event_ids,
                driving_edit=False,
            )
            return None, None
        return claimed, _handle_for(self, transition.reply, transition.claimed, empty)

    async def acknowledgement(
        self,
        *,
        delivery_id: str,
        pending: tuple[str, ...],
        logical: tuple[str, ...],
        discovery: tuple[str, ...],
        room_id: str,
        thread_id: str | None,
        text: str,
    ) -> ReplyWrite:
        """Return an interactive selection's acknowledgement, the row that creates its reply."""
        claim = await self._claim_request(
            delivery_id=delivery_id,
            sources=rl.SpanSources(pending=pending, logical=logical, discovery=discovery),
            room_id=room_id,
            thread_id=thread_id,
            empty=Presentation(),
        )
        return _acknowledgement_write(claim, Presentation(placeholder=text))

    async def decide(self, handle: SpanHandle, decide: Decide) -> AppliedTransition:
        """Apply one span exit in its own transaction and remember what it left."""
        return await self.committed(
            await self.store.replies.decide(
                reply_id=handle.reply_id,
                span_id=handle.span_id,
                decide=decide,
                author_generation=self.generation,
            ),
            handle,
        )

    async def write_ahead(self, handle: SpanHandle, presentation: Presentation) -> ProgressPermission:
        """Record the presentation of the next direct progress edit, and say whether to send it."""
        applied = await self.store.replies.write_ahead(
            reply_id=handle.reply_id,
            span_id=handle.span_id,
            shown=encode_presentation(presentation),
            previous=handle.unconfirmed_progress,
            active_generation=self.generation,
            now_ns=self.clock(),
        )
        if applied.transition.outcome is rl.Outcome.DEFERRED:
            return ProgressPermission.DEFER
        if not applied.transition.applied:
            await self.run_effects(applied.post_commit)
            return ProgressPermission.REFUSE
        await self.committed(applied, handle)
        handle.unconfirmed_progress = None
        return ProgressPermission.SEND


def _new_id() -> str:
    return uuid4().hex


def _handle_for(runtime: ReplyRuntime, reply: rl.Reply, span: rl.Span, empty: Presentation) -> SpanHandle:
    """Build the handle a claimed span runs with, from the records alone."""
    canonical = decode_presentation(reply.presentation)
    shown = decode_presentation(reply.possibly_shown) if reply.possibly_shown is not None else canonical
    if span.kind in {rl.SpanKind.TURN, rl.SpanKind.REGENERATION}:
        # A first answer starts empty; a regeneration replaces the answer with a fresh body.
        return SpanHandle(runtime=runtime, span=span, reply=reply, base=replace(empty, segments=()))
    if span.kind is rl.SpanKind.APPROVAL_RESUME:
        return SpanHandle(runtime=runtime, span=span, reply=reply, base=continued_by(canonical, span.span_id))
    # A replay continues below what the stopped attempt may have shown.
    resumed = _unfinished_from(shown)
    base = after_restart(shown) if resumed is not None else replace(empty, segments=())
    return SpanHandle(
        runtime=runtime,
        span=span,
        reply=reply,
        base=replace(base, placeholder=empty.placeholder, show_tool_calls=empty.show_tool_calls),
        resumed=resumed,
    )


# ---------------------------------------------------------------------------
# Durable writes


class ReplyWriteRefusedError(Exception):
    """A reply write's rule refused the prepared payload; nothing was written.

    ``Recompute`` means a Stop, deletion, or departure committed after the
    payload was rendered; ``Stale`` means the span no longer owns the reply.
    """

    def __init__(self, transition: rl.Transition) -> None:
        super().__init__(transition.outcome.value)
        self.transition = transition


@dataclass(frozen=True)
class ReplyWrite:
    """One durable write of a reply, rendered for the revision it names."""

    reply_id: str
    # The span the row is written for; its delivery id keys the row.
    span: rl.Span
    stage: rl.WriteStage
    shown: Presentation
    # Applied inside the enqueue transaction against the reply as it is then;
    # ``None`` for a row that creates its reply.
    decide: Callable[[rl.Reply, rl.Span], rl.Transition] | None
    placeholder_only: bool = False
    # The running span that renders this write, which learns what it committed.
    handle: SpanHandle | None = None
    create: ReplyCreation | None = None
    # Records the row coupled to another durable step instead of on its own.
    enqueue: ReplyRowEnqueuer | None = None


def _acknowledgement_write(claim: rl.ClaimRequest, shown: Presentation) -> ReplyWrite:
    """Return an interactive selection's acknowledgement, the row that creates its reply."""
    encoded = encode_presentation(shown)
    # Pure: names the reply and the not-yet-current span the row creates.
    created = rl.interactive_acknowledgement(claim, shown=encoded)
    return ReplyWrite(
        reply_id=claim.new_reply_id,
        span=created.spans[0],
        stage=rl.WriteStage.INITIAL,
        shown=shown,
        decide=None,
        placeholder_only=True,
        create=ReplyCreation(claim=claim, shown=encoded),
    )


def pause_decision(
    handle: SpanHandle,
    shown: Presentation,
    *,
    in_place: bool,
    stage: rl.WriteStage | None,
) -> Decide:
    """Return the rule that pauses a span's reply for approval."""
    write = rl.PauseWrite(
        shown=encode_presentation(shown),
        prepared_revision=handle.reply.revision,
        stage=stage,
        confirms=handle.unconfirmed_progress,
    )
    return lambda reply, span: rl.pause(
        reply,
        span,
        write,
        in_place=in_place,
        now_ns=time.time_ns(),
    )


def pause_write(
    handle: SpanHandle,
    shown: Presentation,
    *,
    in_place: bool,
    enqueue: ReplyRowEnqueuer,
) -> ReplyWrite:
    """Return a reply's pause row, recorded with the continuation that holds the paused run."""
    return ReplyWrite(
        reply_id=handle.reply_id,
        span=handle.span,
        handle=handle,
        stage=rl.WriteStage.EDIT,
        shown=shown,
        decide=pause_decision(handle, shown, in_place=in_place, stage=rl.WriteStage.EDIT),
        enqueue=enqueue,
    )


def initial_write(handle: SpanHandle, shown: Presentation, *, placeholder_only: bool) -> ReplyWrite:
    """Return the reply's first visible create as the span's ``INITIAL`` row."""
    encoded = encode_presentation(shown)
    revision = handle.reply.revision
    return ReplyWrite(
        reply_id=handle.reply_id,
        span=handle.span,
        handle=handle,
        stage=rl.WriteStage.INITIAL,
        shown=shown,
        placeholder_only=placeholder_only,
        decide=lambda reply, span: rl.enqueue_initial(
            reply,
            span,
            shown=encoded,
            placeholder_only=placeholder_only,
            prepared_revision=revision,
            now_ns=time.time_ns(),
        ),
    )


def terminal_write(
    handle: SpanHandle,
    shown: Presentation,
    *,
    state: rl.ReplyState,
    frozen_display: Presentation | None = None,
    consumes_edit: bool = False,
) -> ReplyWrite:
    """Return the span's terminal row for one reply state, as finish, stopped, or a delivery failure decides it."""
    write = rl.TerminalWrite(
        shown=encode_presentation(shown),
        prepared_revision=handle.reply.revision,
        state=state,
        frozen_display=None if frozen_display is None else encode_presentation(frozen_display),
        confirms=handle.unconfirmed_progress,
        consumes_edit=consumes_edit,
    )

    def decide(reply: rl.Reply, span: rl.Span) -> rl.Transition:
        now_ns = time.time_ns()
        if write.state is rl.ReplyState.COMPLETED:
            return rl.finish(reply, span, write, now_ns=now_ns)
        if write.state is rl.ReplyState.CANCELLED:
            return rl.stopped(reply, span, write, now_ns=now_ns)
        return rl.fail(reply, span, write, phase="delivery", now_ns=now_ns)

    return ReplyWrite(
        reply_id=handle.reply_id,
        span=handle.span,
        handle=handle,
        stage=rl.WriteStage.FINAL,
        shown=shown,
        # A note is not a placeholder: a later suppression must not redact it.
        placeholder_only=render_body(shown)[0] == shown.placeholder,
        decide=decide,
    )


def owed_note_write(reply: rl.Reply, span: rl.Span, shown: Presentation, *, span_has_final: bool) -> ReplyWrite:
    """Return the row that delivers a note a reply-authored transition owed."""
    encoded = encode_presentation(shown)
    revision = reply.revision
    return ReplyWrite(
        reply_id=reply.reply_id,
        span=span,
        stage=rl.WriteStage.EDIT if span_has_final else rl.WriteStage.FINAL,
        shown=shown,
        decide=lambda current, owner: rl.flush_owed_write(
            current,
            owner,
            shown=encoded,
            prepared_revision=revision,
            span_has_final=span_has_final,
            now_ns=time.time_ns(),
        ),
    )


def approval_note_write(
    reply: rl.Reply,
    span: rl.Span,
    shown: Presentation,
    *,
    approval_id: str,
    state: rl.ReplyState,
    span_has_final: bool,
) -> ReplyWrite:
    """Return the note a failed approval shows on the reply it paused, before its finish ends the reply."""
    encoded = encode_presentation(shown)
    revision = reply.revision
    return ReplyWrite(
        reply_id=reply.reply_id,
        span=span,
        stage=rl.WriteStage.EDIT if span_has_final else rl.WriteStage.FINAL,
        shown=shown,
        decide=lambda current, owner: rl.approval_failure_note(
            current,
            owner,
            approval_id=approval_id,
            shown=encoded,
            state=state,
            prepared_revision=revision,
            span_has_final=span_has_final,
            now_ns=time.time_ns(),
        ),
    )


def resumed_note_write(handle: SpanHandle, shown: Presentation) -> ReplyWrite:
    """Return the note a resumed reply shows when its continuation failed before delivering, keeping sources pending."""
    write = rl.TerminalWrite(
        shown=encode_presentation(shown),
        prepared_revision=handle.reply.revision,
        state=rl.ReplyState.ACTIVE,
        confirms=handle.unconfirmed_progress,
    )
    return ReplyWrite(
        reply_id=handle.reply_id,
        span=handle.span,
        handle=handle,
        stage=rl.WriteStage.EDIT,
        shown=shown,
        decide=lambda reply, span: rl.fail(reply, span, write, phase="pre_delivery", now_ns=time.time_ns()),
    )
