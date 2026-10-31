"""Trusted model context describing one tool-approval continuation."""

from __future__ import annotations

import re
from functools import partial
from typing import TYPE_CHECKING

from mindroom.agent_loop.model_loop import SendRequest
from mindroom.approval.receipt_readers import take_unsent_receipt
from mindroom.event_journal import DEFAULT_ROUTER_MANAGED_ROOM_REASON, ApprovalCall, ApprovalDecision
from mindroom.llm.messages import Message

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.agent_loop.model_loop import LoopState, RequestDecision

_SAFE_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}")
# Provider and card call ids, such as ``toolu_01…``, ``functions.save_file:0``, or ``member_id:call_id``.
_SAFE_TOOL_CALL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}")
_APPROVAL_RECEIPT_HEADER = (
    "[SYSTEM NOTICE — TOOL APPROVAL RECEIPT] This trusted MindRoom runtime receipt records how "
    "paused tool calls were authorized. Do not infer approval policy from tool success alone."
)


def _receipt_call_label(call: ApprovalCall) -> str:
    """Name the call by its tool and its id, which stays the same across every approval in the response."""
    name = f"`{call.tool_name}`" if _SAFE_TOOL_NAME.fullmatch(call.tool_name) else "invalid tool name"
    call_id = (
        f"tool call id `{call.tool_call_id}`"
        if _SAFE_TOOL_CALL_ID.fullmatch(call.tool_call_id)
        else "invalid tool call id"
    )
    return f"{name} ({call_id})"


def build_approval_receipt(calls: tuple[ApprovalCall, ...]) -> str:
    """Render trusted model context for decided ``calls``, as one reader sees what ``approval_receipt_context`` binds."""
    lines = [_APPROVAL_RECEIPT_HEADER]
    for call in calls:
        call_label = _receipt_call_label(call)
        if call.decision is ApprovalDecision.APPROVED:
            if call.human_approval_required is True:
                outcome = (
                    "human approval was required and granted before execution, "
                    "directly or through a matching timed approval window."
                )
            elif call.human_approval_required is False:
                outcome = "human approval was not required; policy approved execution."
            else:
                outcome = "approval was granted, but its approval provenance is unavailable."
        elif call.decision is ApprovalDecision.EXPIRED:
            outcome = "human approval expired; the tool was not executed."
        elif call.reason == DEFAULT_ROUTER_MANAGED_ROOM_REASON:
            # Nobody was asked, so the runtime's own reason says what to do next, such as calling `invite_router`.
            outcome = f"the tool was not executed: {call.reason}"
        else:
            outcome = "approval was denied; the tool was not executed."
        lines.append(f"- {call_label}: {outcome}")
    return "\n".join(lines)


def _sent_call_id(messages: Sequence[Message], call_id: str) -> str:
    """Return the id a request for ``messages`` sends for their call ``call_id``.

    That is a Responses call's ``call_id``, which its result references and a replay keeps when it drops the call's
    ``fc_`` item id, and any other call's own id.
    """
    return next(
        (
            tool_call["call_id"]
            for message in messages
            for tool_call in message.tool_calls or ()
            if tool_call.get("id") == call_id and tool_call.get("call_id")
        ),
        call_id,
    )


async def approval_receipt_hook(state: LoopState) -> RequestDecision:
    """Add the bound receipt to a reader run's messages before its first request as a reader, once per run.

    The hook goes on every run; ``approval_receipt_context`` decides which runs read the receipt, and under which
    ids it names their calls, each as the request sends it. The receipt follows what the run's messages end with:
    a resumed run's answers to its decided calls, or a fresh run's input. It stays there for the run's later
    requests, so the system prompt and every message before it are what earlier requests sent. Like the turn's
    memory results, it is turn context: the run stores it there, so its resume and later turns send it unchanged.
    """
    calls = take_unsent_receipt(state.run.run_id, partial(_sent_call_id, state.messages))
    if calls is not None:
        state.messages.append(Message(role="user", content=build_approval_receipt(calls), add_to_agent_memory=False))
    return SendRequest()


__all__ = [
    "approval_receipt_hook",
    "build_approval_receipt",
]
