"""Build approval continuations and drive their claim and advance through their reply, as an approval resume does."""

from __future__ import annotations

import re
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom import reply_lifecycle as rl
from mindroom.approval.receipt import build_approval_receipt
from mindroom.event_journal.approval_continuations import (
    ApprovalAdvance,
    ApprovalCall,
    ApprovalContinuation,
    ApprovalDecision,
)
from mindroom.event_journal.replies import PreparedReplyRow, ReplyRowRequest
from mindroom.reply_presentation import Presentation, Segment, encode_presentation
from mindroom.response_sources import ResponseSources
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import message_origin, unwrap_extracted_collaborator

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from mindroom.bot import AgentBot
    from mindroom.event_journal import PrincipalStore
    from mindroom.llm.provider import ModelRequest

# The decided calls of a receipt that a test binds only to see which requests carry it, and its text.
RECEIPT_CALLS = (
    ApprovalCall(
        tool_call_id="call-receipt",
        tool_name="publish_report",
        invoking_agent="agent",
        expires_at_ns=1,
        decision=ApprovalDecision.APPROVED,
        human_approval_required=True,
    ),
)
RECEIPT_TEXT = build_approval_receipt(RECEIPT_CALLS)
# Every receipt opens with this line, which the model sees in the system prompt the receipt is appended to.
_RECEIPT_HEADER = RECEIPT_TEXT.splitlines()[0]


def _receipts(request: ModelRequest) -> list[str]:
    """Return each approval receipt the system prompt of ``request`` carries, from its header to its message's end."""
    return [
        content[content.index(_RECEIPT_HEADER) :]
        for message in request.messages
        if message.role == "system" and _RECEIPT_HEADER in (content := str(message.content))
    ]


def carries_approval_receipt(request: ModelRequest) -> bool:
    """Return whether the system prompt of ``request`` carries an approval receipt."""
    return bool(_receipts(request))


def receipt_call_ids(request: ModelRequest) -> list[str]:
    """Return the tool call ids the approval receipt projected into ``request`` names, in its order."""
    [receipt] = _receipts(request)
    return re.findall(r"tool call id `([^`]+)`", receipt)


def request_call_ids(request: ModelRequest) -> list[str]:
    """Return the ids of the tool calls the assistant messages of ``request`` hold, in order."""
    return [call["id"] for message in request.messages for call in message.tool_calls or ()]


def approval_continuation(**changes: Any) -> ApprovalContinuation:  # noqa: ANN401
    """Return a ready continuation of one user's message in ``!room:localhost``, with ``changes`` applied.

    Its origin is the requester's own message, and its run executed as the requester's Matrix turn with ``general``,
    unless ``changes`` names others.
    """
    requester_id = changes.get("requester_id", "@user:localhost")
    defaults: dict[str, Any] = {
        "execution_identity": ToolExecutionIdentity(
            channel="matrix",
            agent_name=changes.get("entity_name", "general"),
            requester_id=requester_id,
            room_id=changes.get("room_id", "!room:localhost"),
            thread_id=changes.get("thread_id", "$thread"),
            resolved_thread_id=changes.get("thread_id", "$thread"),
            session_id=changes.get("session_id", "session-1"),
        ),
        "approval_id": "approval",
        "run_id": "run-1",
        "session_id": "session-1",
        "entity_kind": "agent",
        "entity_name": "general",
        "room_id": "!room:localhost",
        "thread_id": "$thread",
        "requester_id": requester_id,
        "origin": message_origin(sender_id=requester_id),
        "response_event_id": "$waiting",
        "sources": ResponseSources(("$source",), ("$source",)),
        "calls": (),
        "state": "ready",
    }
    return ApprovalContinuation(**(defaults | changes))


async def continuation_claim(
    principal: PrincipalStore,
    continuation: ApprovalContinuation,
    *,
    generation: str,
) -> rl.ClaimRequest:
    """Return the claim of a span for a continuation's sources and reply, as the bot instance ``generation`` makes it."""
    return rl.ClaimRequest(
        span_id=uuid4().hex,
        delivery_id=continuation.source_event_ids[0],
        sources=continuation.sources,
        bot_generation=generation,
        now_ns=time.time_ns(),
        new_reply_id=uuid4().hex,
        entity_name=continuation.entity_name,
        room_id=continuation.room_id,
        thread_id=continuation.thread_id,
        membership_epoch=await principal.membership_epoch(continuation.room_id),
        empty_presentation=encode_presentation(Presentation(show_tool_calls=continuation.show_tool_calls)),
    )


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
    await principal.replies.write_generation(runtime_generation)
    claimed, _applied = await principal.claim_approval_resume(
        approval_id,
        claim=await continuation_claim(principal, continuation, generation=runtime_generation),
    )
    if owner is not None:
        await principal.replies.write_generation(owner)
    return claimed


@asynccontextmanager
async def resumed_approval(bot: AgentBot, continuation: ApprovalContinuation) -> AsyncIterator[ApprovalContinuation]:
    """Start the bot, then claim its paused ``continuation`` with its reply's resume span.

    The claim is the one the journal's approval handoff makes, so the claimed
    continuation runs as the span's attempt for as long as the context is open.
    """
    runner = unwrap_extracted_collaborator(bot._response_runner)
    await bot._reply_runtime.start()
    async with runner.deps.replies.span_scope() as slot:
        claimed = await runner._claim_owned_approval(continuation, slot=slot)
        assert claimed is not None
        assert slot.handle is not None
        yield claimed


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
        ReplyRowRequest(
            reply_id=reply.reply_id,
            span_id=reply.current_span_id,
            decide=lambda reply, held: rl.pause(
                reply,
                held,
                rl.PauseWrite(shown=reply.presentation, prepared_revision=reply.revision, stage=None),
                in_place=False,
                now_ns=time.time_ns(),
            ),
        ),
        PreparedReplyRow(payload={}),
    )
    if enqueued is None or not enqueued.transition.applied:
        return None
    return await principal.approval_continuation(approval_id)


async def freeze_resume_final(
    principal: PrincipalStore,
    claimed: ApprovalContinuation,
    *,
    text: str,
    payload: Mapping[str, object],
    result: Mapping[str, object] | None = None,
) -> None:
    """Freeze the completed answer a claimed resume wrote, as its span's FINAL reply row, without sending it."""
    assert claimed.claim_span_id is not None
    span = await principal.replies.span(claimed.claim_span_id)
    assert span is not None
    shown = encode_presentation(Presentation(segments=(Segment(kind="answer", text=text, span_id=span.span_id),)))

    def decide(reply: rl.Reply, current: rl.Span) -> rl.Transition:
        write = rl.TerminalWrite(shown=shown, prepared_revision=reply.revision, state=rl.ReplyState.COMPLETED)
        return rl.finish(reply, current, write, now_ns=time.time_ns())

    enqueued = await principal.enqueue_reply_row(
        ReplyRowRequest(
            reply_id=span.reply_id,
            span_id=span.span_id,
            decide=decide,
            stage=rl.WriteStage.FINAL,
        ),
        PreparedReplyRow(
            payload=payload,
            result=result,
        ),
    )
    assert enqueued is not None
