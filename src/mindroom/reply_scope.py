"""The in-memory side of one span: its handle, its presentation, and its claim and exits.

A span is one executor's claim on a durable reply. The handle
lives in a context variable for the task that runs the span, so the attempt
task, the streamer's tasks, and the delivery gateway all see the same span
without threading it through every call. The reply record is the durable
owner; the handle only caches what the span last committed, so it can render
payloads for the revision it knows and learn when a Stop changed it.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from mindroom import reply_lifecycle as rl
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.event_journal.replies import (
    AppliedTransition,
    ApprovalEnded,
    Decide,
    ReplyCreation,
    ReplyDebtDue,
    TurnCompleted,
    WakeApproval,
)
from mindroom.logging_config import get_logger
from mindroom.reply_presentation import (
    AGENT_PLACEHOLDER,
    NoteKind,
    Presentation,
    Segment,
    after_restart,
    continued_by,
    decode_presentation,
    encode_presentation,
    format_error_note,
    note_segment,
    render_body,
    with_answer,
)
from mindroom.response_sources import ResponseSources
from mindroom.stop import SpanRegistry
from mindroom.streaming import ProgressPermission, UnfinishedStreamedReply
from mindroom.tool_system.call_record import recording_tool_calls
from mindroom.tool_system.events import (
    ToolTraceEntry,
    deserialize_tool_trace,
    format_tool_combined,
    remap_visible_tool_marker_indices,
    serialize_tool_trace,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping

    from mindroom.cancellation import CancelSource
    from mindroom.event_journal import ApprovalContinuation, PrincipalStore
    from mindroom.event_journal.replies import PostCommitEffect
    from mindroom.matrix_delivery import ReplyRowEnqueuer
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
    # The hold key of the background work the span's response boundary left outstanding, with nothing ready.
    leaves_work: str | None = None

    @property
    def exited(self) -> bool:
        """Return whether a committed transition ended the span, as this task knows it."""
        return self.span.ended

    @property
    def waits_for(self) -> str | None:
        """Return the hold key the span's answer waits for, or ``None`` when its answer ends the reply.

        A span that runs for an approval never waits: its approval's settlement ends the reply.
        """
        if self.reply.approval_id is not None or self.span.kind is rl.SpanKind.APPROVAL_RESUME:
            return None
        return self.leaves_work

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


logger = get_logger(__name__)

# A span that ended this way left its turn to a later span, which its tool calls may already have served.
_INTERRUPTED_OUTCOMES = frozenset(
    {rl.SpanOutcome.LOST, rl.SpanOutcome.RELEASED, rl.SpanOutcome.PAUSED, rl.SpanOutcome.SUPERSEDED},
)


def _entry_json(entry: ToolTraceEntry) -> str:
    """Return one tool call as the reply records store it."""
    (encoded,) = serialize_tool_trace((entry,))
    return json.dumps(encoded)


@dataclass(frozen=True)
class _SpanToolCalls:
    """Records each tool call on the span the calling task runs, before the tool runs."""

    runtime: ReplyRuntime

    async def started(self, tool_name: str, args: Mapping[str, object]) -> str | None:
        handle = current_span()
        if handle is None:
            return None
        call_id = _new_id()
        _, entry = format_tool_combined(tool_name, dict(args), None)
        if not await self.runtime.store.replies.start_tool_call(
            span_id=handle.span_id,
            call_id=call_id,
            entry_json=_entry_json(replace(entry, type="tool_call_started")),
            now_ns=time.time_ns(),
        ):
            # A Stop committed before this start, or the span ended: the tool must not run. A synchronous tool's
            # hooks run on a thread the Stop's cancellation never reaches, so this refusal is what stops it.
            raise asyncio.CancelledError
        return call_id

    async def finished(self, call_id: str, tool_name: str, args: Mapping[str, object], result: object) -> None:
        handle = current_span()
        if handle is None:
            return
        if isinstance(result, BaseException):
            result = f"{type(result).__name__}: {result}"
        _, entry = format_tool_combined(tool_name, dict(args), result)
        try:
            await self.runtime.store.replies.record_tool_call(
                span_id=handle.span_id,
                call_id=call_id,
                entry_json=_entry_json(entry),
                now_ns=time.time_ns(),
            )
        except Exception:
            # The tool already ran, so its outcome stands: the call stays recorded as started, which a replay is
            # told to check before repeating it.
            logger.warning("tool_call_finish_not_recorded", tool_name=tool_name, call_id=call_id, exc_info=True)


class ClaimRefused(Enum):
    """Why a claim opened no span."""

    # The reply cannot be claimed yet; what blocks it retries the sources once it resolves.
    DEFERRED = "deferred"
    # Nothing runs for these sources: the reply already answered the edit, or a
    # Stop, deletion, or departure ended the reply.
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
    # Keeps a pending approval's conversation busy, for the approvals a start finds.
    hold_conversation: Callable[[ApprovalContinuation], None]
    # Releases the conversation the ended approval held, and settles what its reply owes.
    approval_ended: Callable[[ApprovalEnded], None]
    # Delivers what a reply another reply's transition ended owes Matrix.
    settle_debt: Callable[[str], None]
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
        in the span's own task, whose next await the cancel interrupts. The
        in-memory effects run even when an awaited one fails, so a failed read
        cannot keep a stopped span running or a conversation held.
        """
        try:
            for effect in effects:
                if isinstance(effect, TurnCompleted):
                    await self.complete_turn(effect.record)
                elif isinstance(effect, WakeApproval):
                    await self._wake_fenced_approval(effect.approval_id)
        finally:
            for effect in effects:
                if isinstance(effect, ApprovalEnded):
                    self.claim_may_proceed(effect.reply_id)
                    self.approval_ended(effect)
                elif isinstance(effect, ReplyDebtDue):
                    self.settle_debt(effect.reply_id)
            for effect in effects:
                if isinstance(effect, rl.CancelSpan):
                    self.spans.cancel(effect.span_id, cancel_source="user_stop" if effect.by_stop else None)

    async def _span_tool_calls(self, span_ids: tuple[str, ...]) -> tuple[ToolTraceEntry, ...]:
        """Return the tool calls these spans recorded, in the order they started."""
        stored = await self.store.replies.tool_calls(span_ids)
        return tuple(deserialize_tool_trace([json.loads(entry) for entry in stored]))

    async def interrupted_tool_calls(self, handle: SpanHandle) -> tuple[ToolTraceEntry, ...]:
        """Return the tool calls of the attempts this span takes over: the latest spans that left its turn unanswered.

        A regeneration redoes its edit's turn, so it takes over only the earlier attempts of the same edit.
        """
        regeneration = handle.span.kind is rl.SpanKind.REGENERATION
        interrupted: list[str] = []
        for span in reversed(await self.store.replies.spans(handle.reply_id)):
            if span.span_id == handle.span_id:
                continue
            if span.outcome not in _INTERRUPTED_OUTCOMES or (
                regeneration and span.delivery_id != handle.span.delivery_id
            ):
                break
            interrupted.append(span.span_id)
        return await self._span_tool_calls(tuple(reversed(interrupted)))

    async def finish_approval(self, approval_id: str) -> bool:
        """Finish a paused run once its FINAL is terminal, settling its turn; return whether it finished."""
        return await run_coroutine_until_complete(self._ran(self.store.finish_approval_continuation(approval_id)))

    async def release_approval(self, approval_id: str, expected_generation: int) -> bool:
        """Release an interrupted run's approval as ``rl.approval_released`` decides; return whether it was released."""
        return await run_coroutine_until_complete(
            self._ran(self.store.release_approval_continuation(approval_id, expected_generation=expected_generation)),
        )

    async def _ran(self, ending: Awaitable[tuple[PostCommitEffect, ...] | None]) -> bool:
        """Run what an ended approval's commit left, returning whether it ended.

        Callers run it to completion even when they are cancelled, so a
        committed end always releases the conversation its approval held.
        """
        effects = await ending
        if effects is None:
            return False
        await self.run_effects(effects)
        return True

    async def _wake_fenced_approval(self, approval_id: str) -> None:
        """Run a fenced approval's failure settlement."""
        continuation = await self.store.approval_continuation(approval_id)
        if continuation is None:
            return
        # Its source worker settles it once whatever owns the source lets go of it.
        self.retry_sources(continuation.room_id, continuation.source_event_ids)

    async def _wait_to_claim(
        self,
        reply_id: str,
        room_id: str,
        sources: tuple[str, ...],
    ) -> None:
        """Retry sources once what blocked their claim on the reply is gone, instead of retrying at once.

        The wait registers first and then rechecks, so a resolution that landed
        in between still retries them. A retry claims again and may wait again.
        """
        waiting = self._waiting_claims.setdefault(reply_id, [])
        if (room_id, sources) not in waiting:
            waiting.append((room_id, sources))
        reply = await self.store.replies.load(reply_id)
        if reply is None or not rl.claim_blocked(
            reply,
            durable_write_debt=await self.store.replies.has_unresolved_rows(reply_id),
        ):
            # A note still owed and not yet enqueued wakes it when its row resolves.
            self.claim_may_proceed(reply_id)

    def waits_to_claim(self, sources: Iterable[str]) -> bool:
        """Return whether a deferred claim of any of these sources waits for what blocks it, which retries them."""
        wanted = frozenset(sources)
        return any(
            wanted.intersection(waiting) for claims in self._waiting_claims.values() for _room, waiting in claims
        )

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
        """Cancel the spans source deletions ended and wake the approvals they cancelled; return the replies they ended.

        Each tombstone's projection ended its replies, and fenced the approval
        that held one, in its own commit and recorded the span it cancelled.
        The debt of the replies it ended is now due.
        """
        endings = await self.store.replies.take_deletion_endings()
        # Every cancel lands before an awaited wake can fail.
        for ending in endings:
            if ending.span_id is not None:
                self.spans.cancel(ending.span_id, cancel_source=None)
        for ending in endings:
            reply = await self.store.replies.load(ending.reply_id)
            if reply is not None and reply.approval_id is not None:
                # Its settlement expires the cards and settles the sources the approval holds.
                await self._wake_fenced_approval(reply.approval_id)
        return tuple(ending.reply_id for ending in endings)

    async def forget_finished(self) -> None:
        """Drop the records of replies finished as long ago as the handled-turn ledger forgets their turns."""
        before_ns = time.time_ns() - _FINISHED_REPLY_RETENTION_NS
        while await self.store.replies.forget_finished(before_ns=before_ns, limit=_FORGET_BATCH) == _FORGET_BATCH:
            pass

    async def take_ownership(self) -> None:
        """Make this bot instance the owner of its principal's replies, before it writes any of them."""
        await self.store.replies.write_generation(self.generation)

    async def start(self) -> None:
        """Make this bot instance the owner of its principal's replies, then end what older instances left running.

        Runs before journal replay: replay claims continue the replies whose
        sources are still pending, and the notes this owes are delivered by the
        outbox recovery after each room syncs.
        """
        await self.take_ownership()
        # No span a deletion ended before this start survived it; recovery delivers what their replies owe.
        await self.store.replies.take_deletion_endings()
        for applied in await self.store.replies.owner_lost(self.generation, now_ns=time.time_ns()):
            await self.run_effects(applied.post_commit)
        # A message to a conversation an approval still holds waits for that approval, after a restart too.
        for continuation in await self.store.pending_approvals():
            self.hold_conversation(continuation)

    @asynccontextmanager
    async def span_scope(self) -> AsyncIterator[SpanSlot]:
        """Open the slot a claim inside fills, so child tasks share the span."""
        slot = SpanSlot()
        token = _current_slot.set(slot)
        try:
            with recording_tool_calls(_SpanToolCalls(self)):
                yield slot
        finally:
            _current_slot.reset(token)
            if slot.handle is not None:
                self.spans.forget(slot.handle.span_id)

    async def claim(
        self,
        *,
        delivery_id: str,
        sources: ResponseSources,
        room_id: str,
        thread_id: str | None,
        placeholder: str = AGENT_PLACEHOLDER,
        show_tool_calls: bool = True,
        driving_edit_id: str | None = None,
        existing_event_id: str | None = None,
        interactive_span_id: str | None = None,
        wake_reply_id: str | None = None,
    ) -> SpanHandle | ClaimRefused:
        """Claim the reply one span answers, or say why no span opened."""
        empty = Presentation(placeholder=placeholder, show_tool_calls=show_tool_calls)
        request = await self._new_request(
            delivery_id=delivery_id,
            sources=sources,
            room_id=room_id,
            thread_id=thread_id,
            empty=empty,
            driving_edit_id=driving_edit_id,
            interactive_span_id=interactive_span_id,
            wake_reply_id=wake_reply_id,
        )
        # The claim may continue the span a selection's acknowledgement created instead of opening its own.
        candidates = (request.span_id,) if interactive_span_id is None else (request.span_id, interactive_span_id)
        with self._expecting(candidates):
            applied = await self.committed(
                await self.store.replies.claim(request, existing_event_id=existing_event_id),
            )
        return await self._opened(
            applied.transition,
            candidates,
            room_id=room_id,
            pending=sources.pending_event_ids,
            empty=empty,
        )

    @contextmanager
    def _expecting(self, span_ids: tuple[str, ...]) -> Iterator[None]:
        """Expect these spans while their claim commits; a Stop can reach a span before its task registers."""
        for span_id in span_ids:
            self.spans.expect(span_id)
        try:
            yield
        except BaseException:
            for span_id in span_ids:
                self.spans.forget(span_id)
            raise

    async def _opened(
        self,
        transition: rl.Transition,
        candidates: tuple[str, ...],
        *,
        room_id: str,
        pending: tuple[str, ...],
        empty: Presentation,
    ) -> SpanHandle | ClaimRefused:
        """Return the span a committed claim opened, or say why it opened none, forgetting the spans it did not take."""
        claimed_span_id = None if transition.claimed is None else transition.claimed.span_id
        for span_id in candidates:
            if span_id != claimed_span_id:
                self.spans.forget(span_id)
        if transition.outcome is rl.Outcome.STALE:
            return ClaimRefused.RETIRED
        if transition.outcome is rl.Outcome.DUPLICATE or transition.unmodeled is not None:
            # An unmodeled claim already ended the reply and settled its sources, or failed the approval that holds them.
            return ClaimRefused.NOTHING_TO_RUN
        if transition.claimed is None or transition.reply is None:
            # Earlier writes of this reply are unresolved; their resolution
            # wakes these sources instead of waiting under the conversation lock.
            assert transition.reply is not None, "only a reply with earlier writes defers a claim"
            await self._wait_to_claim(transition.reply.reply_id, room_id, pending)
            return ClaimRefused.DEFERRED
        return _handle_for(self, transition.reply, transition.claimed, empty)

    async def _new_request(
        self,
        *,
        delivery_id: str,
        sources: ResponseSources,
        room_id: str,
        thread_id: str | None,
        empty: Presentation,
        driving_edit_id: str | None = None,
        interactive_span_id: str | None = None,
        wake_reply_id: str | None = None,
    ) -> rl.ClaimRequest:
        return rl.ClaimRequest(
            span_id=_new_id(),
            delivery_id=delivery_id,
            sources=sources,
            bot_generation=self.generation,
            now_ns=time.time_ns(),
            new_reply_id=_new_id(),
            entity_name=self.entity_name,
            room_id=room_id,
            thread_id=thread_id,
            membership_epoch=await self.store.membership_epoch(room_id),
            empty_presentation=encode_presentation(empty),
            driving_edit_id=driving_edit_id,
            interactive_span_id=interactive_span_id,
            wake_reply_id=wake_reply_id,
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
        claim = await self._new_request(
            delivery_id=continuation.source_event_ids[0],
            sources=continuation.sources,
            room_id=continuation.room_id,
            thread_id=continuation.thread_id,
            empty=empty,
        )
        with self._expecting((claim.span_id,)):
            claimed, applied = await self.store.claim_approval_resume(continuation.approval_id, claim=claim)
            if applied is not None:
                applied = await self.committed(applied)
        if applied is None:
            self.spans.forget(claim.span_id)
            return None, None
        # A retired claim leaves the resume to the instance that took the replies over, whose approval recovery runs
        # it; an unmodeled one ended the reply and failed the approval, whose settlement ends it.
        opened = await self._opened(
            applied.transition,
            (claim.span_id,),
            room_id=continuation.room_id,
            pending=continuation.source_event_ids,
            empty=empty,
        )
        if isinstance(opened, ClaimRefused):
            return None, None
        assert claimed is not None, "a resume span claims its continuation in the same transaction"
        return claimed, opened

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
        """Return an interactive selection's acknowledgement, the row that creates its reply.

        No task runs for its span until the selection's answer claims it, which a Stop or departure before then refuses.
        """
        claim = await self._new_request(
            delivery_id=delivery_id,
            sources=ResponseSources(pending, logical, discovery),
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
            now_ns=time.time_ns(),
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
    if (
        span.kind is rl.SpanKind.WAKE
        and shown.trailing_note is not None
        and shown.trailing_note.note is NoteKind.JOB_WAIT
    ):
        # The reply's latest write is its wait: the wake answers below what it showed, without the waiting note.
        base = replace(shown, trailing_note=None, placeholder=empty.placeholder, show_tool_calls=empty.show_tool_calls)
        body, trace = render_body(base)
        answered = UnfinishedStreamedReply(visible_text=body, tool_trace=trace, interrupted=False)
        return SpanHandle(runtime=runtime, span=span, reply=reply, base=base, resumed=answered)
    # A replay, or a wake an interruption cut short, continues below what the stopped attempt may have shown: its
    # work, then the restart note.
    restarted = after_restart(shown)
    work = restarted.segments[0] if restarted.segments else None
    base = restarted if work is not None else replace(empty, segments=())
    resumed = None if work is None else UnfinishedStreamedReply(visible_text=work.text, tool_trace=work.tool_trace)
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

    @property
    def reply_id(self) -> str:
        """Return the reply the row belongs to."""
        return self.span.reply_id


def _acknowledgement_write(claim: rl.ClaimRequest, shown: Presentation) -> ReplyWrite:
    """Return an interactive selection's acknowledgement, the row that creates its reply."""
    encoded = encode_presentation(shown)
    # Pure: names the reply and the not-yet-current span the row creates.
    created = rl.interactive_acknowledgement(claim, shown=encoded)
    return ReplyWrite(
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


def wait_decision(handle: SpanHandle, shown: Presentation, *, hold_key: str) -> Decide:
    """Return the rule that ends a span whose create already showed its wait, as the reply's first message."""
    write = rl.TerminalWrite(
        shown=encode_presentation(shown),
        prepared_revision=handle.reply.revision,
        state=rl.ReplyState.WAITING,
        confirms=handle.unconfirmed_progress,
    )
    return lambda reply, span: rl.wait(
        reply,
        span,
        write,
        hold_key=hold_key,
        shown_by_create=True,
        now_ns=time.time_ns(),
    )


def release_decision(
    handle: SpanHandle,
    *,
    outcome: Literal[rl.SpanOutcome.RELEASED, rl.SpanOutcome.SUPERSEDED] = rl.SpanOutcome.RELEASED,
) -> Decide:
    """Return the rule that ends a span with its sources pending for a retry or replay."""
    confirms = handle.unconfirmed_progress
    return lambda reply, span: rl.release(reply, span, now_ns=time.time_ns(), outcome=outcome, confirms=confirms)


def terminal_source_decision(*, source_deleted: bool) -> Decide:
    """Return the rule that ends a reply whose sources a deletion or another settlement made terminal before it ran."""
    if source_deleted:
        return lambda reply, span: rl.sources_deleted(reply, span, now_ns=time.time_ns())
    return lambda reply, span: rl.sources_settled_without_reply(reply, span, now_ns=time.time_ns())


def suppress_decision(handle: SpanHandle, *, reason: Literal["suppressed", "hook_failed"] = "suppressed") -> Decide:
    """Return the rule that ends a span whose answer must not be shown."""
    confirms = handle.unconfirmed_progress
    return lambda reply, span: rl.suppress(reply, span, reason=reason, confirms=confirms, now_ns=time.time_ns())


def pause_write(
    handle: SpanHandle,
    shown: Presentation,
    *,
    in_place: bool,
    enqueue: ReplyRowEnqueuer,
) -> ReplyWrite:
    """Return a reply's pause row, recorded with the continuation that holds the paused run."""
    return ReplyWrite(
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
    hold_key: str | None = None,
) -> ReplyWrite:
    """Return the span's terminal row for one reply state, as finish, stopped, wait, or a delivery failure decides it.

    ``ACTIVE`` is the note an interruption shows before delivery started: an
    edit that keeps the reply's sources pending. ``WAITING`` is the answer of a
    span that leaves background work outstanding, an edit that keeps the reply
    open for that work, whose ``hold_key`` it names.
    """
    write = rl.TerminalWrite(
        shown=encode_presentation(shown),
        prepared_revision=handle.reply.revision,
        state=state,
        frozen_display=None if frozen_display is None else encode_presentation(frozen_display),
        confirms=handle.unconfirmed_progress,
    )

    def decide(reply: rl.Reply, span: rl.Span) -> rl.Transition:
        now_ns = time.time_ns()
        if write.state is rl.ReplyState.COMPLETED:
            return rl.finish(reply, span, write, now_ns=now_ns)
        if write.state is rl.ReplyState.WAITING:
            assert hold_key is not None, "a waiting reply names the work it waits for"
            return rl.wait(reply, span, write, hold_key=hold_key, now_ns=now_ns)
        if write.state is rl.ReplyState.CANCELLED:
            return rl.stopped(reply, span, write, now_ns=now_ns)
        return rl.fail(reply, span, write, now_ns=now_ns)

    return ReplyWrite(
        span=handle.span,
        handle=handle,
        stage=rl.WriteStage.EDIT if state in {rl.ReplyState.ACTIVE, rl.ReplyState.WAITING} else rl.WriteStage.FINAL,
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
) -> ReplyWrite:
    """Return the terminal note a failed approval shows on the reply it paused, before its finish ends the reply."""
    encoded = encode_presentation(shown)
    revision = reply.revision
    return ReplyWrite(
        span=span,
        stage=rl.WriteStage.FINAL,
        shown=shown,
        decide=lambda current, owner: rl.approval_failure_note(
            current,
            owner,
            approval_id=approval_id,
            shown=encoded,
            state=state,
            prepared_revision=revision,
            now_ns=time.time_ns(),
        ),
    )


@dataclass(frozen=True, slots=True)
class _NotedEnd:
    """A span end that writes its terminal row: what the reply showed, plus one note."""

    state: rl.ReplyState
    note: Segment


def interruption_note(cancel_source: CancelSource) -> NoteKind:
    """Return the note a reply shows below what it showed when a restart or another cancellation cut it short."""
    return NoteKind.RESTART if cancel_source == "sync_restart" else NoteKind.INTERRUPTED


def interrupted_end(
    reply: rl.Reply,
    *,
    cancel_source: CancelSource | None,
    failure_reason: str | None,
    delivery_started: bool,
) -> _NotedEnd | None:
    """Return the noted end of a span whose response was cancelled or failed with a settled outcome.

    ``None`` means the span is released, so its sources retry. ``cancel_source``
    is ``None`` for a failure. A recorded Stop ends the reply cancelled, as the
    rules decide for every exit. A failure ends the reply failed with its error
    note, and its turn is not retried. An interruption of a reply with no event
    yet leaves Matrix untouched and its sources retry; before delivery starts,
    the reply shows the interruption's note while its sources retry, and once
    delivery started, it ends failed with that note.
    """
    if reply.unapplied_stop:
        return _NotedEnd(rl.ReplyState.CANCELLED, note_segment(NoteKind.CANCELLED))
    if cancel_source is None:
        return _NotedEnd(
            rl.ReplyState.FAILED,
            note_segment(NoteKind.ERROR, format_error_note(failure_reason or "interrupted")),
        )
    if reply.event_id is None:
        return None
    note = note_segment(interruption_note(cancel_source))
    return _NotedEnd(rl.ReplyState.FAILED if delivery_started else rl.ReplyState.ACTIVE, note)
