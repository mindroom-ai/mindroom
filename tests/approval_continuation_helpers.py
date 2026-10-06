"""Drive an approval continuation's claim and advance through its reply, as an approval resume does."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom import reply_lifecycle as rl
from mindroom.event_journal.approval_continuations import ApprovalAdvance
from mindroom.event_journal.replies import ReplyRowRequest
from mindroom.reply_presentation import Presentation, encode_presentation

if TYPE_CHECKING:
    from mindroom.event_journal import PrincipalStore
    from mindroom.event_journal.approval_continuations import ApprovalCall, ApprovalContinuation


async def claim_continuation(
    principal: PrincipalStore,
    approval_id: str,
    *,
    runtime_generation: str,
) -> ApprovalContinuation | None:
    """Claim one ready paused run with its reply's resume span, as the bot instance ``runtime_generation`` does.

    Any other instance that owned the principal's replies owns them again afterwards, as after a takeover.
    """
    continuation = await principal.approval_continuation(approval_id)
    if continuation is None:
        return None
    owner = await principal.replies.active_generation()
    await principal.replies.write_generation(runtime_generation, now_ns=time.time_ns())
    sources = continuation.sources
    claimed, _applied = await principal.claim_approval_resume(
        approval_id,
        claim=rl.ClaimRequest(
            span_id=uuid4().hex,
            delivery_id=sources.pending_event_ids[0],
            sources=rl.SpanSources(
                pending=sources.pending_event_ids,
                logical=sources.logical_source_event_ids,
                discovery=sources.discovery_event_ids,
            ),
            bot_generation=runtime_generation,
            now_ns=time.time_ns(),
            new_reply_id=uuid4().hex,
            entity_name=continuation.entity_name,
            room_id=continuation.room_id,
            thread_id=continuation.thread_id,
            membership_epoch=await principal.membership_epoch(continuation.room_id),
            empty_presentation=encode_presentation(Presentation(show_tool_calls=continuation.show_tool_calls)),
        ),
    )
    if owner is not None:
        await principal.replies.write_generation(owner, now_ns=time.time_ns())
    return claimed


async def advance_continuation(
    principal: PrincipalStore,
    approval_id: str,
    *,
    claimant_generation: int,
    run_id: str,
    session_id: str,
    calls: tuple[ApprovalCall, ...],
    runtime_model_name: str | None = None,
    delegation_storage_bindings: dict[str, dict[str, object]] | None = None,
    cli_call: dict[str, object] | None = None,
    continuation_count: int | None = None,
) -> ApprovalContinuation | None:
    """Replace one claimed generation with the next exact Agno pause, pausing the resume that ran it."""
    current = await principal.approval_continuation(approval_id)
    assert current is not None
    assert current.span_id is not None
    paused = await principal.replies.span(current.span_id)
    assert paused is not None
    reply = await principal.replies.load(paused.reply_id)
    assert reply is not None
    assert reply.current_span_id is not None
    enqueued = await principal.pause_for_approval(
        ApprovalAdvance(
            approval_id=approval_id,
            claimant_generation=claimant_generation,
            run_id=run_id,
            session_id=session_id,
            calls=calls,
            runtime_model_name=runtime_model_name,
            delegation_storage_bindings=delegation_storage_bindings,
            cli_call=cli_call,
            continuation_count=continuation_count,
        ),
        request=ReplyRowRequest(
            reply_id=reply.reply_id,
            span_id=reply.current_span_id,
            decide=lambda reply, held: rl.pause(
                reply,
                held,
                rl.PauseWrite(shown=reply.presentation, prepared_revision=reply.revision, stage=None),
                approval_id=approval_id,
                in_place=False,
                now_ns=time.time_ns(),
            ),
        ),
        room_id=current.room_id,
        thread_id=current.thread_id,
        payload={},
    )
    if enqueued is None or not enqueued.transition.applied:
        return None
    return await principal.approval_continuation(approval_id)
