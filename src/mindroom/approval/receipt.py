"""Trusted model context describing one tool-approval continuation."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import TYPE_CHECKING

from mindroom.agent_loop.model_loop import SendRequest
from mindroom.approval.receipt_readers import take_unsent_receipt
from mindroom.event_journal import ApprovalCall
from mindroom.event_journal import ApprovalDecision as ContinuationDecision
from mindroom.llm.messages import Message

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.agent_loop.model_loop import BeforeRequestHook, LoopState, RequestDecision
    from mindroom.llm.provider import Capabilities

_MARKER_KEY = "mindroom_approval_receipt"
_SAFE_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}")
_APPROVAL_RECEIPT_HEADER = (
    "[SYSTEM NOTICE — TOOL APPROVAL RECEIPT] This trusted MindRoom runtime receipt records how "
    "paused tool calls were authorized. Do not infer approval policy from tool success alone."
)


def _receipt_call_label(tool_name: str, call_ordinal: int) -> str:
    if _SAFE_TOOL_NAME.fullmatch(tool_name) is None:
        return f"invalid tool name (call #{call_ordinal})"
    return f"`{tool_name}` (call #{call_ordinal})"


def build_approval_receipt(calls: tuple[ApprovalCall, ...]) -> str:
    """Render trusted model context for one exact approval generation."""
    lines = [_APPROVAL_RECEIPT_HEADER]
    for call_ordinal, call in enumerate(calls, start=1):
        if call.decision is None:
            msg = f"Cannot build trusted receipt for pending approval call #{call_ordinal}"
            raise ValueError(msg)
        call_label = _receipt_call_label(call.tool_name, call_ordinal)
        if call.decision is ContinuationDecision.APPROVED:
            if call.human_approval_required is True:
                outcome = (
                    "human approval was required and granted before execution, "
                    "directly or through a matching timed approval window."
                )
            elif call.human_approval_required is False:
                outcome = "human approval was not required; policy approved execution."
            else:
                outcome = "approval was granted, but its approval provenance is unavailable."
        elif call.decision is ContinuationDecision.EXPIRED:
            outcome = "human approval expired; the tool was not executed."
        else:
            outcome = "approval was denied; the tool was not executed."
        lines.append(f"- {call_label}: {outcome}")
    return "\n".join(lines)


def _is_response_chain_boundary(message: Message) -> bool:
    provider_data = message.provider_data
    return (
        message.role == "assistant"
        and isinstance(provider_data, dict)
        and isinstance(provider_data.get("response_id"), str)
    )


def messages_with_approval_receipt(
    messages: Sequence[Message],
    receipt_text: str,
    *,
    after_response_id: bool = False,
) -> list[Message]:
    """Return the request's messages with the trusted receipt projected in, leaving ``messages`` as they are.

    The receipt is appended to the first prompt message as a copy; a request without one gets a system message of
    its own, placed after the latest response-chain boundary when the provider continues stored responses, so the
    provider still sees it.
    """
    outbound = list(messages)
    response_chain_index = (
        next(
            (index for index in range(len(outbound) - 1, -1, -1) if _is_response_chain_boundary(outbound[index])),
            None,
        )
        if after_response_id
        else None
    )
    system_index = (
        None
        if response_chain_index is not None
        else next(
            (
                index
                for index, message in enumerate(outbound)
                if message.role in {"system", "developer"} and isinstance(message.content, str)
            ),
            None,
        )
    )
    if system_index is None:
        receipt_message = Message(
            role="system",
            content=receipt_text,
            provider_data={_MARKER_KEY: True},
            add_to_agent_memory=False,
        )
        outbound.insert(0 if response_chain_index is None else response_chain_index + 1, receipt_message)
        return outbound
    receipt_message = deepcopy(outbound[system_index])
    receipt_message.content = f"{receipt_message.content}\n\n{receipt_text}"
    receipt_message.provider_data = {**(receipt_message.provider_data or {}), _MARKER_KEY: True}
    outbound[system_index] = receipt_message
    return outbound


def approval_receipt_hook(capabilities: Capabilities) -> BeforeRequestHook:
    """Return the pre-request hook that sends the bound receipt with a reader run's first request, once per run.

    The hook goes on every run; ``approval_receipt_context`` decides which runs read the receipt. It is projected
    into the request only, so no run's history holds it.
    """

    async def hook(state: LoopState) -> RequestDecision:
        receipt_text = take_unsent_receipt(state.run.run_id)
        if receipt_text is None:
            return SendRequest()
        return SendRequest(
            messages=messages_with_approval_receipt(
                state.messages,
                receipt_text,
                after_response_id=capabilities.continues_stored_responses,
            ),
        )

    return hook


__all__ = [
    "approval_receipt_hook",
    "build_approval_receipt",
    "messages_with_approval_receipt",
]
