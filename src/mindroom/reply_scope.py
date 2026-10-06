"""The in-memory side of one span: its handle, its presentation, and its claim and exits.

A span is one executor's claim on a durable reply (DESIGN.md §3). The handle
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
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom import reply_lifecycle as rl
from mindroom.cancellation import request_task_cancel
from mindroom.event_journal.replies import AppliedTransition, ClaimLookup, Decide, ReplyCreation
from mindroom.reply_presentation import (
    AGENT_PLACEHOLDER,
    Presentation,
    Segment,
    after_restart,
    continued_by,
    decode_presentation,
    encode_presentation,
    shown_work,
    visible_work,
    with_answer,
)
from mindroom.streaming import UnfinishedStreamedReply
from mindroom.tool_system.events import remap_visible_tool_marker_indices

if TYPE_CHECKING:
    import asyncio
    from collections.abc import AsyncIterator, Callable, Mapping

    from mindroom.cancellation import TaskCancelSource
    from mindroom.event_journal import ApprovalContinuation, PrincipalStore
    from mindroom.matrix_delivery import ReplyRowEnqueuer
    from mindroom.tool_system.events import ToolTraceEntry


@dataclass
class SpanHandle:
    """One running span, as the task that executes it knows it."""

    runtime: ReplyRuntime
    span: rl.Span
    # The reply as this span last committed or read it.
    reply: rl.Reply
    # What earlier spans left above this span's answer.
    base: Presentation
    # What a stopped attempt showed, for main's streamer to continue below.
    resumed: UnfinishedStreamedReply | None = None
    # Whether what the reply showed before this span is known (records, not a legacy read).
    shown_before_known: bool = True
    # The span's last direct progress edit that Matrix accepted and no durable
    # write has recorded yet.
    unconfirmed_progress: rl.ProgressConfirmation | None = None
    # Set by the span's terminal transition or exit.
    exited: bool = False

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

    def answer_segment(
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
        answer = self.answer_segment(text, tool_trace, team_state=team_state)
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
    """Describe what a reply showed in the shape main's streamer continues below."""
    work = shown_work(shown)
    if work is None:
        return None
    return UnfinishedStreamedReply(visible_text=work.text, tool_trace=work.tool_trace)


@dataclass
class ReplyRuntime:
    """One bot instance's owner of its replies' claims and span exits."""

    store: PrincipalStore
    entity_name: str
    generation: str
    retry_sources: Callable[[str, tuple[str, ...]], None]
    clock: Callable[[], int] = field(default=time.time_ns)
    # The task executing each live span of this bot instance.
    _tasks: dict[str, asyncio.Task[object]] = field(default_factory=dict, init=False, repr=False)
    # Cancellations of live spans whose task had not started yet (DESIGN.md §8 registration recheck).
    _cancel_on_start: dict[str, TaskCancelSource] = field(default_factory=dict, init=False, repr=False)
    # Sources whose claim waited for a reply's earlier writes, by reply.
    _waiting_for_rows: dict[str, list[tuple[str, tuple[str, ...]]]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    async def _wait_for_rows(self, reply_id: str, room_id: str, sources: tuple[str, ...]) -> None:
        """Retry sources once the reply's earlier writes resolve, instead of retrying at once."""
        self._waiting_for_rows.setdefault(reply_id, []).append((room_id, sources))
        reply = await self.store.replies.load(reply_id)
        if reply is None or (reply.owed_write is None and not await self.store.replies.has_unresolved_rows(reply_id)):
            # They resolved before this claim registered its wait. A note still
            # owed and not yet enqueued wakes it when its row resolves.
            self.rows_resolved(reply_id)

    def rows_resolved(self, reply_id: str) -> None:
        """Retry the claims that waited for this reply's earlier writes."""
        for room_id, sources in self._waiting_for_rows.pop(reply_id, ()):
            self.retry_sources(room_id, sources)

    def register_task(self, handle: SpanHandle, task: asyncio.Task[object]) -> None:
        """Remember the task that executes one span, cancelling it at once if a Stop already reached the span."""
        self._tasks[handle.span_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(handle.span_id, None))
        cancel_source = self._cancel_on_start.pop(handle.span_id, None)
        if cancel_source is not None:
            request_task_cancel(task, cancel_source=cancel_source)

    def cancel_span(self, span_id: str, *, cancel_source: TaskCancelSource) -> bool:
        """Cancel exactly the named span's task; a span whose task has not started is cancelled when it does."""
        task = self._tasks.get(span_id)
        if task is None:
            self._cancel_on_start[span_id] = cancel_source
            return False
        if task.done():
            return False
        request_task_cancel(task, cancel_source=cancel_source)
        return True

    def forget_span(self, span_id: str) -> None:
        """Drop a cancellation that the span ended before starting its task."""
        self._cancel_on_start.pop(span_id, None)

    async def start(self) -> None:
        """Make this bot instance the owner of its principal's replies."""
        await self.store.replies.write_generation(self.generation, now_ns=self.clock())

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
                self.forget_span(slot.handle.span_id)

    async def claim(
        self,
        *,
        delivery_id: str,
        sources: rl.SpanSources,
        room_id: str,
        thread_id: str | None,
        requester_id: str,
        visibility_policy: rl.VisibilityPolicy,
        placeholder: str = AGENT_PLACEHOLDER,
        show_tool_calls: bool = True,
        driving_edit_id: str | None = None,
        edit_receipt_order: int | None = None,
        historical_event_id: str | None = None,
        existing_event_id: str | None = None,
        approval_id: str | None = None,
        approval_generation: int | None = None,
        interactive_span_id: str | None = None,
    ) -> SpanHandle | None:
        """Claim the reply one span answers; ``None`` when the claim must wait for earlier writes."""
        empty = Presentation(placeholder=placeholder, show_tool_calls=show_tool_calls)
        request = replace(
            await self.claim_request(
                delivery_id=delivery_id,
                sources=sources,
                room_id=room_id,
                thread_id=thread_id,
                requester_id=requester_id,
                visibility_policy=visibility_policy,
                empty=empty,
            ),
            driving_edit_id=driving_edit_id,
            approval_id=approval_id,
            approval_generation=approval_generation,
            interactive_span_id=interactive_span_id,
            historical_event_id=historical_event_id,
        )
        applied = await self.store.replies.claim(
            request,
            ClaimLookup(
                interactive_span_id=interactive_span_id,
                existing_event_id=existing_event_id,
                edit_receipt_order=edit_receipt_order,
            ),
        )
        transition = applied.transition
        if transition.claimed is None or transition.reply is None:
            # Earlier writes of this reply are unresolved; their resolution
            # wakes these sources instead of waiting under the conversation lock.
            assert transition.reply is not None, "only a reply with earlier writes defers a claim"
            await self._wait_for_rows(transition.reply.reply_id, room_id, sources.pending)
            return None
        return _handle_for(self, transition.reply, transition.claimed, empty)

    async def claim_request(
        self,
        *,
        delivery_id: str,
        sources: rl.SpanSources,
        room_id: str,
        thread_id: str | None,
        requester_id: str,
        visibility_policy: rl.VisibilityPolicy,
        empty: Presentation,
    ) -> rl.ClaimRequest:
        """Return a claim by this bot instance, with fresh identities for the span and any reply it creates."""
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
            requester_id=requester_id,
            visibility_policy=visibility_policy,
            empty_presentation=encode_presentation(empty),
        )

    async def claim_approval_resume(
        self,
        continuation: ApprovalContinuation,
        *,
        runtime_generation: str,
        legacy_show_tool_calls: bool | None,
        placeholder: str,
    ) -> tuple[ApprovalContinuation | None, SpanHandle | None]:
        """Claim a ready continuation and its paused reply's resume span together.

        Returns no handle for a continuation no reply records own; returns
        neither when the reply's earlier writes are unresolved, which retry the
        sources once they resolve.
        """
        empty = Presentation(placeholder=placeholder, show_tool_calls=continuation.show_tool_calls)
        sources = continuation.sources
        claim = await self.claim_request(
            delivery_id=continuation.source_event_ids[0],
            sources=rl.SpanSources(
                pending=sources.pending_event_ids,
                logical=sources.logical_source_event_ids,
                discovery=sources.discovery_event_ids,
            ),
            room_id=continuation.room_id,
            thread_id=continuation.thread_id,
            requester_id=continuation.requester_id,
            visibility_policy=rl.VisibilityPolicy.NORMAL,
            empty=empty,
        )
        claimed, applied = await self.store.claim_approval_resume(
            continuation.approval_id,
            runtime_generation=runtime_generation,
            claim=claim,
            legacy_show_tool_calls=legacy_show_tool_calls,
        )
        if applied is None:
            return claimed, None
        transition = applied.transition
        if claimed is None or transition.claimed is None or transition.reply is None:
            assert transition.reply is not None, "only a reply with earlier writes defers a resume"
            await self._wait_for_rows(transition.reply.reply_id, continuation.room_id, sources.pending_event_ids)
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
        requester_id: str,
        text: str,
    ) -> ReplyWrite:
        """Return an interactive selection's acknowledgement, the row that creates its reply (PR-1.md §6.1)."""
        claim = await self.claim_request(
            delivery_id=delivery_id,
            sources=rl.SpanSources(pending=pending, logical=logical, discovery=discovery),
            room_id=room_id,
            thread_id=thread_id,
            requester_id=requester_id,
            visibility_policy=rl.VisibilityPolicy.NORMAL,
            empty=Presentation(),
        )
        return _acknowledgement_write(claim, Presentation(placeholder=text))

    async def decide(self, handle: SpanHandle, decide: Decide) -> AppliedTransition:
        """Apply one span exit in its own transaction and remember what it left."""
        applied = await self.store.replies.decide(reply_id=handle.reply_id, span_id=handle.span_id, decide=decide)
        handle.note(applied)
        if handle.span.ended:
            handle.exited = True
        return applied

    async def write_ahead(self, handle: SpanHandle, presentation: Presentation) -> bool:
        """Record the presentation of the next direct progress edit; ``False`` refuses the edit."""
        previous = handle.unconfirmed_progress
        applied = await self.store.replies.decide(
            reply_id=handle.reply_id,
            span_id=handle.span_id,
            decide=lambda reply, span: rl.write_ahead(
                reply,
                span,
                shown=encode_presentation(presentation),
                previous=previous,
                active_generation=self.generation,
                now_ns=self.clock(),
            ),
        )
        if not applied.transition.applied:
            return False
        handle.note(applied)
        handle.unconfirmed_progress = None
        return True


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

    @property
    def shown_json(self) -> str:
        """Return the encoded presentation this write may show."""
        return encode_presentation(self.shown)


def _acknowledgement_write(claim: rl.ClaimRequest, shown: Presentation) -> ReplyWrite:
    """Return an interactive selection's acknowledgement, the row that creates its reply (PR-1.md §6.1)."""
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
    approval_id: str,
    in_place: bool,
    stage: rl.WriteStage | None,
) -> Decide:
    """Return the rule that pauses a span's reply for approval (DESIGN.md §6.4 ``pause``)."""
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
        approval_id=approval_id,
        in_place=in_place,
        now_ns=time.time_ns(),
    )


def pause_write(
    handle: SpanHandle,
    shown: Presentation,
    *,
    approval_id: str,
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
        decide=pause_decision(handle, shown, approval_id=approval_id, in_place=in_place, stage=rl.WriteStage.EDIT),
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
) -> ReplyWrite:
    """Return the span's terminal row for one reply state, as finish, stopped, or a delivery failure decides it."""
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
        if write.state is rl.ReplyState.CANCELLED:
            return rl.stopped(reply, span, write, now_ns=now_ns)
        return rl.fail(reply, span, write, phase="delivery", now_ns=now_ns)

    return ReplyWrite(
        reply_id=handle.reply_id,
        span=handle.span,
        handle=handle,
        stage=rl.WriteStage.FINAL,
        shown=shown,
        placeholder_only=not visible_work(shown),
        decide=decide,
    )


def owed_note_write(reply: rl.Reply, span: rl.Span, shown: Presentation, *, span_has_final: bool) -> ReplyWrite:
    """Return the row that delivers a note a reply-authored transition owed (DESIGN.md §7.2 staging)."""
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
