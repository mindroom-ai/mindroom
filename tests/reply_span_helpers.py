"""Run delivery-layer code inside a claimed reply span, as every locked response turn does."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from mindroom import reply_lifecycle as rl
from mindroom.event_journal import DeliveryStage, EventClass, EventKind, InboundEvent
from mindroom.event_journal.replies import ReplyRowRequest
from mindroom.reply_presentation import AGENT_PLACEHOLDER, Presentation
from mindroom.reply_scope import ReplyRuntime, SpanHandle, initial_write

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from contextlib import AbstractAsyncContextManager

    from mindroom.delivery_gateway import DeliveryGateway, FinalDeliveryRequest
    from mindroom.event_journal import PrincipalStore
    from mindroom.final_delivery import FinalDeliveryOutcome
    from mindroom.response_runner import ResponseRequest, ResponseRunner


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
    edit_receipt_order: int | None = None,
    runtime: ReplyRuntime | None = None,
) -> AsyncIterator[SpanHandle]:
    """Claim a reply span for one source, admitted as ingress admits it, and run the block in it.

    With ``placeholder_event_id``, the reply's placeholder is already in the
    room as that event, the way a turn shows it before its answer. With
    ``regenerated_event_id``, the source is an edit regenerating that answer.
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
        runtime = ReplyRuntime(
            store=principal,
            entity_name=entity_name,
            generation="gen-test",
            retry_sources=lambda _room_id, _sources: None,
            complete_turn=AsyncMock(),
            clean_up_superseded=lambda _continuation: None,
        )
    await runtime.take_ownership()
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
            edit_receipt_order=edit_receipt_order,
            historical_event_id=regenerated_event_id,
            existing_event_id=regenerated_event_id,
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
) -> FinalDeliveryOutcome:
    """Deliver one final answer from inside the reply span its request names, as a locked response turn does.

    An edit regeneration regenerates the answer it names; any other request
    with an existing event shows that event as its placeholder.
    """
    sources = request.identity.sources
    regenerated = request.existing_event_id if request.prepared_edit_record is not None else None
    async with reply_span(
        principal,
        source_event_id=request.identity.response_envelope.source_event_id,
        room_id=request.target.room_id,
        thread_id=request.target.resolved_thread_id,
        logical_source_event_ids=sources.logical_source_event_ids,
        placeholder_event_id=None if regenerated is not None else request.existing_event_id,
        regenerated_event_id=regenerated,
        edit_receipt_order=sources.edit_receipt_order,
    ):
        return await gateway.deliver_final(request)


def response_span(
    runner: ResponseRunner,
    request: ResponseRequest,
    *,
    placeholder_event_id: str | None = None,
) -> AbstractAsyncContextManager[SpanHandle]:
    """Run the block in the reply span the runner's bot claims for the request's source."""
    return reply_span(
        runner.deps.replies.store,
        runtime=runner.deps.replies,
        source_event_id=request.response_envelope.source_event_id,
        room_id=request.room_id,
        thread_id=request.response_envelope.target.resolved_thread_id,
        logical_source_event_ids=request.sources.logical_source_event_ids,
        placeholder_event_id=placeholder_event_id,
    )
