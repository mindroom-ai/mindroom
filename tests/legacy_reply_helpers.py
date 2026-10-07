"""What an earlier release left paused for approval, as a starting bot adopts it."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from mindroom.event_journal import approval_continuations
from mindroom.handled_turns import TurnRecordCodec
from mindroom.tool_system.events import serialize_tool_trace
from tests.conftest import unwrap_extracted_collaborator

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

    from mindroom.bot import AgentBot
    from mindroom.event_journal import ApprovalContinuation, PrincipalStore
    from mindroom.event_journal.backend import Transaction
    from mindroom.tool_system.events import ToolTraceEntry


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


async def adopt_main_left_approval(bot: AgentBot) -> None:
    """Start the bot, which adopts the paused reply of a continuation stored without reply records."""
    await bot._reply_runtime.start()


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
    await adopt_main_left_approval(bot)
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
        # The identity the upgrade copied into the context, read until reply classification names the paused span.
        context = {
            **approval_continuations._context(continuation),
            "show_tool_calls": continuation.show_tool_calls,
            "prepared_edit_record": (
                None
                if continuation.prepared_edit_record is None
                else TurnRecordCodec._to_ledger_record(continuation.prepared_edit_record)
            ),
            "legacy_identity": {
                "entity_name": continuation.entity_name,
                "room_id": continuation.room_id,
                "thread_id": continuation.thread_id,
                "response_event_id": continuation.response_event_id,
                "pending_event_ids": list(continuation.source_event_ids),
                "logical_source_event_ids": list(continuation.sources.logical_source_event_ids),
                "discovery_event_ids": list(continuation.sources.discovery_event_ids),
            },
        }
        transaction.execute(
            """
            INSERT INTO approval_continuations (
                principal_id, approval_id, span_id, state,
                generation, runtime_generation, failure_reason, context_json, created_at_ns
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                store._principal_id,
                continuation.approval_id,
                continuation.state,
                continuation.generation,
                continuation.runtime_generation,
                continuation.failure_reason,
                approval_continuations._json(context),
                len(continuation.approval_id),
            ),
        )
        approval_continuations._insert_calls(
            transaction,
            store._principal_id,
            continuation.approval_id,
            continuation.generation,
            continuation.calls,
        )

    await store._backend.write(write)
