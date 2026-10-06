"""What a stopped main process left for a turn, and the reads a starting bot runs for it."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from mindroom.constants import STREAM_STATUS_APPROVAL_PENDING, STREAM_STATUS_KEY, STREAM_STATUS_PENDING
from mindroom.event_journal import DeliveryStage, EventClass, EventKind, InboundEvent, ProjectedEvent
from mindroom.history.types import HistoryScope
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.message_target import MessageTarget
from mindroom.response_sources import ResponseAttempt, ResponseSources
from mindroom.turn_record import TurnRecord
from tests.conftest import unwrap_extracted_collaborator

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.bot import AgentBot
    from mindroom.event_journal import ApprovalContinuation


async def main_left_reply(
    bot: AgentBot,
    *,
    room_id: str = "!room:localhost",
    thread_id: str = "$thread",
    source: str = "$source",
    owner: str = "general",
    scope: HistoryScope | None = None,
    requester_id: str = "@user:localhost",
    event_id: str = "$reply",
    payload: dict[str, object] | None = None,
) -> TurnRecord:
    """Leave what a stopped main process left for a turn, then start this one, which adopts its reply."""
    store = bot.journal_principal()
    await store.admit(
        InboundEvent(
            event_id=source,
            room_id=room_id,
            thread_id=thread_id,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender=requester_id,
            origin_server_ts=1,
            source={"event_id": source, "content": {"body": "run it"}},
        ),
        ProjectedEvent(
            event_id=source,
            room_id=room_id,
            thread_id=thread_id,
            sender=requester_id,
            origin_server_ts=1,
            content={"body": "run it"},
            replaces_event_id=None,
            redacts_event_id=None,
        ),
    )
    sources = ResponseSources((source,), (source,))
    await store.enqueue_matrix_delivery(
        delivery_id=source,
        stage=DeliveryStage.INITIAL,
        room_id=room_id,
        thread_id=thread_id,
        payload=payload or {"body": "Thinking...", STREAM_STATUS_KEY: STREAM_STATUS_PENDING},
        response_attempt=ResponseAttempt(owner, sources),
    )
    await store.claim_matrix_delivery(delivery_id=source, stage=DeliveryStage.INITIAL)
    await store.acknowledge_matrix_delivery(
        delivery_id=source,
        stage=DeliveryStage.INITIAL,
        event_id=event_id,
        delivered_projections=(),
    )
    record = TurnRecord.create(
        (source,),
        completed=False,
        response_owner=owner,
        response_event_id=event_id,
        requester_id=requester_id,
        conversation_target=MessageTarget.resolve(room_id, thread_id, source),
        history_scope=scope or HistoryScope(kind="agent", scope_id=owner),
    )
    await bot._turn_store.record_pending_turn(record)
    await bot._reply_runtime.start()
    return record


async def read_after_sync(bot: AgentBot, visible: ResolvedVisibleMessage | Exception | None) -> AsyncMock:
    """Run the legacy reads the outbox recovery passes run, until one lands or they give up."""
    fetch = AsyncMock(side_effect=visible) if isinstance(visible, Exception) else AsyncMock(return_value=visible)
    with patch("mindroom.legacy_reply_messages.fetch_latest_visible_message", new=fetch):
        for _attempt in range(3):
            await bot._legacy_reply_reads.run()
    return fetch


async def adopt_main_left_approval(bot: AgentBot, continuation: ApprovalContinuation) -> None:
    """Start the bot, which adopts the paused reply of a continuation stored without reply records, and read it."""
    await bot._reply_runtime.start()
    await read_after_sync(
        bot,
        ResolvedVisibleMessage.synthetic(
            event_id=continuation.response_event_id,
            sender=bot.matrix_id.full_id,
            body="Waiting for approval.",
            timestamp=1,
            thread_id=continuation.thread_id,
            content={"body": "Waiting for approval.", STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING},
        ),
    )


@asynccontextmanager
async def resumed_main_left_approval(
    bot: AgentBot,
    continuation: ApprovalContinuation,
) -> AsyncIterator[ApprovalContinuation]:
    """Adopt a ready continuation stored without reply records, then claim it with its reply's resume span.

    The claim is the one the journal's approval handoff makes, so the claimed
    continuation runs as the span's attempt for as long as the context is open.
    """
    runner = unwrap_extracted_collaborator(bot._response_runner)
    await adopt_main_left_approval(bot, continuation)
    async with runner._reply_span_scope() as slot:
        claimed = await runner._claim_owned_approval(continuation, slot=slot)
        assert claimed is not None
        assert slot.handle is not None
        yield claimed
