"""Run delivery-layer code inside a claimed reply span, as every locked response turn does."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import uuid4

from mindroom import reply_lifecycle as rl
from mindroom.event_journal import ApprovalContinuation, DeliveryStage, EventClass, EventKind, InboundEvent, replies
from mindroom.event_journal.replies import ClaimLookup, ReplyRowRequest
from mindroom.reply_presentation import AGENT_PLACEHOLDER, Presentation, encode_presentation
from mindroom.reply_scope import ReplyRuntime, SpanHandle, initial_write
from mindroom.response_sources import ResponseSources
from tests.approval_continuation_helpers import claim_continuation

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from contextlib import AbstractAsyncContextManager

    from mindroom.delivery_gateway import DeliveryGateway, FinalDeliveryRequest
    from mindroom.event_journal import PrincipalStore
    from mindroom.final_delivery import FinalDeliveryOutcome
    from mindroom.response_runner import ResponseRequest, ResponseRunner
    from mindroom.turn_record import TurnRecord


def _runtime(
    principal: PrincipalStore,
    *,
    entity_name: str = "agent",
    complete_turn: Callable[[TurnRecord], Awaitable[object]] | None = None,
) -> ReplyRuntime:
    return ReplyRuntime(
        store=principal,
        entity_name=entity_name,
        generation="gen-test",
        retry_sources=lambda _room_id, _sources: None,
        complete_turn=complete_turn or AsyncMock(),
    )


async def seed_finished_reply(
    principal: PrincipalStore,
    event_id: str,
    *,
    sources: rl.SpanSources,
    room_id: str,
    thread_id: str | None,
    entity_name: str = "agent",
) -> rl.Reply:
    """Record a finished answer shown as ``event_id``, as a completed reply leaves it, for an edit to regenerate."""
    now_ns = time.time_ns()
    request = rl.ClaimRequest(
        span_id=uuid4().hex,
        delivery_id=event_id,
        sources=sources,
        bot_generation="gen-seed",
        now_ns=now_ns,
        new_reply_id=uuid4().hex,
        entity_name=entity_name,
        room_id=room_id,
        thread_id=thread_id,
        membership_epoch=await principal.membership_epoch(room_id),
        empty_presentation=encode_presentation(Presentation()),
    )
    reply = rl._new_reply(request, state=rl.ReplyState.COMPLETED, event_id=event_id)
    span = rl._end(rl._new_span(request, reply, rl.SpanKind.TURN), rl.SpanOutcome.COMPLETED, now_ns)
    transition = rl.Transition(outcome=rl.Outcome.APPLIED, reply=reply, spans=(span,))
    await principal._backend.write(lambda transaction: replies.apply(transaction, principal._principal_id, transition))
    return reply


@asynccontextmanager
async def reply_span(
    principal: PrincipalStore,
    *,
    source_event_id: str,
    room_id: str,
    thread_id: str | None = None,
    logical_source_event_ids: tuple[str, ...] | None = None,
    entity_name: str = "agent",
    placeholder: str = AGENT_PLACEHOLDER,
    show_tool_calls: bool = True,
    placeholder_event_id: str | None = None,
    regenerated_event_id: str | None = None,
    prepared_edit: TurnRecord | None = None,
    runtime: ReplyRuntime | None = None,
) -> AsyncIterator[SpanHandle]:
    """Claim a reply span for one source, admitted as ingress admits it, and run the block in it.

    With ``placeholder_event_id``, the reply's placeholder is already in the
    room as that event, the way a turn shows it before its answer. With
    ``regenerated_event_id``, the source is an edit regenerating that answer,
    and ``prepared_edit`` is the edit it selected.
    ``runtime`` claims as an existing bot instance instead of a fresh one.
    """
    if not await principal.is_pending(source_event_id):
        await principal.admit(
            InboundEvent(
                event_id=source_event_id,
                room_id=room_id,
                thread_id=thread_id,
                kind=EventKind.MESSAGE,
                event_class=EventClass.ACTIONABLE,
                sender="@user:localhost",
                origin_server_ts=1,
                source={},
            ),
        )
    if runtime is None:
        runtime = _runtime(principal, entity_name=entity_name)
    await runtime.take_ownership()
    if regenerated_event_id is not None and await principal.replies.for_event(regenerated_event_id) is None:
        await seed_finished_reply(
            principal,
            regenerated_event_id,
            sources=rl.SpanSources(pending=(), logical=logical_source_event_ids or (source_event_id,)),
            room_id=room_id,
            thread_id=thread_id,
            entity_name=entity_name,
        )
    async with runtime.span_scope() as slot:
        handle = await runtime.claim(
            delivery_id=source_event_id,
            sources=rl.SpanSources(
                pending=(source_event_id,),
                logical=logical_source_event_ids or (source_event_id,),
            ),
            room_id=room_id,
            thread_id=thread_id,
            placeholder=placeholder,
            show_tool_calls=show_tool_calls,
            driving_edit_id=None if regenerated_event_id is None else source_event_id,
            existing_event_id=regenerated_event_id,
            prepared_edit=prepared_edit,
        )
        assert isinstance(handle, SpanHandle)
        slot.handle = handle
        if placeholder_event_id is not None:
            write = initial_write(handle, Presentation(placeholder=placeholder), placeholder_only=True)
            enqueued = await principal.enqueue_reply_row(
                request=ReplyRowRequest(
                    reply_id=handle.reply_id,
                    span_id=handle.span_id,
                    decide=write.decide,
                    placeholder_only=True,
                    stage=rl.WriteStage.INITIAL,
                ),
                room_id=room_id,
                thread_id=thread_id,
                payload={"msgtype": "m.text", "body": placeholder},
            )
            assert enqueued is not None
            handle.note(enqueued.applied)
            assert await principal.claim_matrix_delivery(delivery_id=source_event_id, stage=DeliveryStage.INITIAL)
            await principal.acknowledge_matrix_delivery(
                delivery_id=source_event_id,
                stage=DeliveryStage.INITIAL,
                event_id=placeholder_event_id,
                delivered_projections=(),
            )
            reply = await principal.replies.load(handle.reply_id)
            assert reply is not None
            handle.reply = reply
        yield handle


async def final_in_span(
    gateway: DeliveryGateway,
    principal: PrincipalStore,
    request: FinalDeliveryRequest,
    *,
    prepared_edit: TurnRecord | None = None,
    complete_turn: Callable[[TurnRecord], Awaitable[object]] | None = None,
) -> FinalDeliveryOutcome:
    """Deliver one final answer from inside the reply span its request names, as a locked response turn does.

    A regeneration of the edit ``prepared_edit`` selected regenerates the
    answer the request names, and its completed answer consumes that edit; any
    other request with an existing event shows that event as its placeholder.
    ``complete_turn`` sees each turn the reply records answered, as the bot's
    gateway runs its reply runtime's effects.
    """
    runtime = _runtime(principal, complete_turn=complete_turn)
    gateway = replace(gateway, deps=replace(gateway.deps, reply_effects=runtime.run_effects))
    sources = request.identity.sources
    regenerated = request.existing_event_id if prepared_edit is not None else None
    async with reply_span(
        principal,
        source_event_id=request.identity.response_envelope.source_event_id,
        room_id=request.target.room_id,
        thread_id=request.target.resolved_thread_id,
        logical_source_event_ids=sources.logical_source_event_ids,
        placeholder_event_id=None if regenerated is not None else request.existing_event_id,
        regenerated_event_id=regenerated,
        prepared_edit=prepared_edit,
        runtime=runtime,
    ):
        return await gateway.deliver_final(replace(request, consumes_edit=prepared_edit is not None))


async def final_in_resume_span(
    gateway: DeliveryGateway,
    principal: PrincipalStore,
    request: FinalDeliveryRequest,
) -> FinalDeliveryOutcome:
    """Deliver one final answer as an approved run's resume, below the pause its request's event shows."""
    runtime = _runtime(principal)
    gateway = replace(gateway, deps=replace(gateway.deps, reply_effects=runtime.run_effects))
    source = request.identity.response_envelope.source_event_id
    if not await principal.is_pending(source):
        await principal.admit(
            InboundEvent(
                event_id=source,
                room_id=request.target.room_id,
                thread_id=request.target.resolved_thread_id,
                kind=EventKind.MESSAGE,
                event_class=EventClass.ACTIONABLE,
                sender="@user:localhost",
                origin_server_ts=1,
                source={},
            ),
        )
    assert request.existing_event_id is not None
    paused = await paused_for_approval(
        principal,
        ApprovalContinuation(
            approval_id=f"approval-{source}",
            run_id="run",
            session_id="session",
            entity_kind="agent",
            entity_name="agent",
            room_id=request.target.room_id,
            thread_id=request.target.resolved_thread_id,
            requester_id="@user:localhost",
            response_event_id=request.existing_event_id,
            sources=ResponseSources((source,), request.identity.sources.logical_source_event_ids or (source,)),
            calls=(),
            state="ready",
        ),
    )
    assert paused is not None
    await runtime.take_ownership()
    async with runtime.span_scope() as slot:
        claimed, handle = await runtime.claim_approval_resume(paused, placeholder=AGENT_PLACEHOLDER)
        assert claimed is not None
        assert handle is not None
        slot.handle = handle
        return await gateway.deliver_final(request)


def response_span(
    runner: ResponseRunner,
    request: ResponseRequest,
    *,
    placeholder_event_id: str | None = None,
    show_tool_calls: bool = True,
) -> AbstractAsyncContextManager[SpanHandle]:
    """Run the block in the reply span the runner's bot claims for the request's source.

    A regeneration's span regenerates the answer the request names, as the
    runner claims it.
    """
    regenerated = request.existing_event_id if request.prepared_edit_record is not None else None
    return reply_span(
        runner.deps.replies.store,
        runtime=runner.deps.replies,
        source_event_id=request.response_envelope.source_event_id,
        room_id=request.room_id,
        thread_id=request.response_envelope.target.resolved_thread_id,
        logical_source_event_ids=request.sources.logical_source_event_ids,
        placeholder_event_id=None if regenerated is not None else placeholder_event_id,
        regenerated_event_id=regenerated,
        prepared_edit=request.prepared_edit_record,
        show_tool_calls=show_tool_calls,
    )


async def reply_shown_for_approval(principal: PrincipalStore, continuation: ApprovalContinuation) -> rl.Span | None:
    """Claim a reply span for the continuation's sources and show its response event, as a response does before it pauses.

    ``None`` means the claim could not take the sources.
    """
    replies = principal.replies
    generation = await replies.active_generation()
    if generation is None:
        generation = "gen-test"
        await replies.write_generation(generation, now_ns=time.time_ns())
    sources = continuation.sources
    claimed = await replies.claim(
        rl.ClaimRequest(
            span_id=uuid4().hex,
            delivery_id=sources.pending_event_ids[0],
            sources=rl.SpanSources(
                pending=sources.pending_event_ids,
                logical=sources.logical_source_event_ids,
                discovery=sources.discovery_event_ids,
            ),
            bot_generation=generation,
            now_ns=time.time_ns(),
            new_reply_id=uuid4().hex,
            entity_name=continuation.entity_name,
            room_id=continuation.room_id,
            thread_id=continuation.thread_id,
            membership_epoch=await principal.membership_epoch(continuation.room_id),
            empty_presentation=encode_presentation(Presentation(show_tool_calls=continuation.show_tool_calls)),
        ),
        ClaimLookup(existing_event_id=continuation.response_event_id),
    )
    span = claimed.transition.claimed
    if span is None:
        return None
    reply = claimed.transition.reply
    assert reply is not None
    if reply.event_id is None:
        created = await principal.enqueue_reply_row(
            request=ReplyRowRequest(
                reply_id=span.reply_id,
                span_id=span.span_id,
                decide=lambda reply, created: rl.enqueue_initial(
                    reply,
                    created,
                    shown=reply.presentation,
                    placeholder_only=True,
                    prepared_revision=reply.revision,
                    now_ns=time.time_ns(),
                ),
                placeholder_only=True,
                stage=rl.WriteStage.INITIAL,
            ),
            room_id=continuation.room_id,
            thread_id=continuation.thread_id,
            payload={"msgtype": "m.text", "body": AGENT_PLACEHOLDER},
        )
        if created is None or created.transaction_id is None:
            return None
        await principal.claim_matrix_delivery(delivery_id=span.delivery_id, stage=DeliveryStage.INITIAL)
        await principal.acknowledge_matrix_delivery(
            delivery_id=span.delivery_id,
            stage=DeliveryStage.INITIAL,
            event_id=continuation.response_event_id,
            delivered_projections=(),
        )
    return span


async def pause_shown_reply(
    principal: PrincipalStore,
    continuation: ApprovalContinuation,
    span: rl.Span,
) -> ApprovalContinuation | None:
    """Pause the reply ``span`` shows for ``continuation``, in the transaction that creates the continuation."""
    enqueued = await principal.pause_for_approval(
        continuation,
        request=ReplyRowRequest(
            reply_id=span.reply_id,
            span_id=span.span_id,
            decide=lambda reply, held: rl.pause(
                reply,
                held,
                rl.PauseWrite(shown=reply.presentation, prepared_revision=reply.revision, stage=None),
                in_place=False,
                now_ns=time.time_ns(),
            ),
        ),
        room_id=continuation.room_id,
        thread_id=continuation.thread_id,
        payload={},
    )
    if enqueued is None or not enqueued.transition.applied:
        return None
    return await principal.approval_continuation(continuation.approval_id)


async def paused_for_approval(
    principal: PrincipalStore,
    continuation: ApprovalContinuation,
) -> ApprovalContinuation | None:
    """Pause a reply for ``continuation`` the way a response does, and return the continuation the pause created.

    ``None`` means the pause could not take the sources, as
    ``PrincipalStore.pause_for_approval`` reports it. A claimed continuation
    is paused ready and then claimed by its ``runtime_generation``'s resume.
    """
    claimed, claimant = continuation.state == "claimed", continuation.runtime_generation
    if claimed:
        continuation = replace(continuation, state="ready", runtime_generation=None)
    span = await reply_shown_for_approval(principal, continuation)
    paused = None if span is None else await pause_shown_reply(principal, continuation, span)
    if paused is None or not claimed:
        return paused
    return await claim_continuation(
        principal,
        continuation.approval_id,
        runtime_generation=claimant or "gen-test",
    )
