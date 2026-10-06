"""What a stopped main process left for a turn, and the reads a starting bot runs for it."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from mindroom.constants import STREAM_STATUS_APPROVAL_PENDING, STREAM_STATUS_KEY, STREAM_STATUS_PENDING
from mindroom.event_journal import (
    DeliveryStage,
    EventClass,
    EventKind,
    InboundEvent,
    ProjectedEvent,
    approval_continuations,
)
from mindroom.history.types import HistoryScope
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.message_target import MessageTarget
from mindroom.tool_system.events import serialize_tool_trace
from mindroom.turn_record import TurnRecord
from tests.conftest import unwrap_extracted_collaborator

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

    from mindroom.bot import AgentBot
    from mindroom.event_journal import ApprovalContinuation, PrincipalStore
    from mindroom.event_journal.backend import Transaction
    from mindroom.tool_system.events import ToolTraceEntry


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
    await store.enqueue_matrix_delivery(
        delivery_id=source,
        stage=DeliveryStage.INITIAL,
        room_id=room_id,
        thread_id=thread_id,
        payload=payload or {"body": "Thinking...", STREAM_STATUS_KEY: STREAM_STATUS_PENDING},
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


async def keep_main_paused_answer(
    store: PrincipalStore,
    approval_id: str,
    *,
    text: str = "",
    tool_trace: Sequence[ToolTraceEntry] = (),
    team_state: Mapping[str, object] | None = None,
) -> None:
    """Write the paused answer main kept in a stored continuation's context, as releases before reply records did."""

    def write(transaction: Transaction) -> None:
        key = (store._principal_id, approval_id)
        row = transaction.fetchone(
            "SELECT context_json FROM approval_continuations WHERE principal_id = ? AND approval_id = ?",
            key,
        )
        assert row is not None
        context = json.loads(str(row["context_json"]))
        context.update(
            response_text=text,
            response_tool_trace=list(serialize_tool_trace(tool_trace, include_internal=True)),
            response_presentation_state=dict(team_state or {}),
        )
        transaction.execute(
            "UPDATE approval_continuations SET context_json = ? WHERE principal_id = ? AND approval_id = ?",
            (json.dumps(context), *key),
        )

    await store._backend.write(write)


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


async def store_main_continuation(store: PrincipalStore, continuation: ApprovalContinuation) -> None:
    """Store a continuation as an earlier release left it, after the upgrade adopted its reply identity.

    It names no paused span: reply classification gives it one.
    """

    def write(transaction: Transaction) -> None:
        context = approval_continuations._context(continuation)
        transaction.execute(
            """
            INSERT INTO approval_continuations (
                principal_id, approval_id, entity_name, span_id, state,
                generation, runtime_generation, failure_reason, context_json, created_at_ns
            ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                store._principal_id,
                continuation.approval_id,
                continuation.entity_name,
                continuation.state,
                continuation.generation,
                continuation.runtime_generation,
                continuation.failure_reason,
                approval_continuations._json(context),
                len(continuation.approval_id),
            ),
        )
        for ordinal, event_id in enumerate(continuation.source_event_ids):
            transaction.execute(
                "INSERT INTO approval_continuation_sources (principal_id, approval_id, event_id, source_ordinal) "
                "VALUES (?, ?, ?, ?)",
                (store._principal_id, continuation.approval_id, event_id, ordinal),
            )
        approval_continuations._insert_calls(
            transaction,
            store._principal_id,
            continuation.approval_id,
            continuation.generation,
            continuation.calls,
        )

    await store._backend.write(write)
