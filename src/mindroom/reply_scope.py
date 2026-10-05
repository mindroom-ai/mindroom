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
from mindroom.event_journal.replies import AppliedTransition, ClaimLookup, Decide
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
    from collections.abc import AsyncIterator, Callable, Mapping

    from mindroom.event_journal import PrincipalStore
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
class _SpanSlot:
    """The scope's holder for the span its claim opens; shared with child tasks."""

    handle: SpanHandle | None = None


_current_slot: ContextVar[_SpanSlot | None] = ContextVar("mindroom_reply_span", default=None)


def current_span() -> SpanHandle | None:
    """Return the span the current task executes, if any."""
    slot = _current_slot.get()
    return None if slot is None else slot.handle


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

    async def start(self) -> None:
        """Make this bot instance the owner of its principal's replies."""
        await self.store.replies.write_generation(self.generation, now_ns=self.clock())

    @asynccontextmanager
    async def span_scope(self) -> AsyncIterator[_SpanSlot]:
        """Open the slot a claim inside fills, so child tasks share the span."""
        slot = _SpanSlot()
        token = _current_slot.set(slot)
        try:
            yield slot
        finally:
            _current_slot.reset(token)

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
        now_ns = self.clock()
        empty = Presentation(placeholder=placeholder, show_tool_calls=show_tool_calls)
        request = rl.ClaimRequest(
            span_id=_new_id(),
            delivery_id=delivery_id,
            sources=sources,
            bot_generation=self.generation,
            now_ns=now_ns,
            new_reply_id=_new_id(),
            entity_name=self.entity_name,
            room_id=room_id,
            thread_id=thread_id,
            membership_epoch=await self.store.membership_epoch(room_id),
            requester_id=requester_id,
            visibility_policy=visibility_policy,
            empty_presentation=encode_presentation(empty),
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
            self.retry_sources(room_id, sources.pending)
            return None
        return _handle_for(self, transition.reply, transition.claimed, empty)

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
    """One durable write of a span's reply, rendered for the revision it names."""

    handle: SpanHandle
    stage: rl.WriteStage
    shown: Presentation
    # Applied inside the enqueue transaction against the reply as it is then.
    decide: Callable[[rl.Reply, rl.Span], rl.Transition]
    placeholder_only: bool = False

    @property
    def shown_json(self) -> str:
        """Return the encoded presentation this write may show."""
        return encode_presentation(self.shown)


def initial_write(handle: SpanHandle, shown: Presentation, *, placeholder_only: bool) -> ReplyWrite:
    """Return the reply's first visible create as the span's ``INITIAL`` row."""
    encoded = encode_presentation(shown)
    revision = handle.reply.revision
    return ReplyWrite(
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
        handle=handle,
        stage=rl.WriteStage.FINAL,
        shown=shown,
        placeholder_only=not visible_work(shown),
        decide=decide,
    )
