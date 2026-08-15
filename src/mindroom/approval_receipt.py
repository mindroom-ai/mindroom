"""Trusted model context describing one tool-approval continuation."""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from agno.models.message import Message

from mindroom.event_journal import ApprovalCall
from mindroom.event_journal import ApprovalDecision as ContinuationDecision

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator

    from agno.models.base import Model
    from agno.models.fallback import FallbackConfig
    from agno.models.response import ModelResponse

_MARKER_KEY = "mindroom_approval_receipt"
_HOOK_ATTR = "_mindroom_approval_receipt_hook_installed"
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
                outcome = "an approval card was shown and approved before execution."
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


@dataclass
class _ApprovalReceiptContext:
    receipt_text: str
    fired_model_ids: set[int] = field(default_factory=set)


@dataclass(frozen=True)
class _ApprovalReceiptProjection:
    """Transient receipt prompt plus a path for publishing model history mutations."""

    caller_messages: list[Message]
    outbound_messages: list[Message]
    receipt_message: Message
    original_system_message: Message | None

    def publish_model_mutations(self) -> None:
        """Copy model-added history back while removing the transient receipt."""
        published: list[Message] = []
        for message in self.outbound_messages:
            if message is self.receipt_message:
                if self.original_system_message is not None:
                    published.append(self.original_system_message)
            else:
                published.append(message)
        self.caller_messages[:] = published


_context: ContextVar[_ApprovalReceiptContext | None] = ContextVar(
    "approval_receipt_context",
    default=None,
)


@contextmanager
def approval_receipt_context(receipt_text: str) -> Generator[None, None, None]:
    """Bind one trusted approval receipt to the exact resumed model call."""
    token = _context.set(_ApprovalReceiptContext(receipt_text=receipt_text))
    try:
        yield
    finally:
        _context.reset(token)


def _is_response_chain_boundary(message: Message) -> bool:
    provider_data = message.provider_data
    return (
        message.role == "assistant"
        and isinstance(provider_data, dict)
        and isinstance(provider_data.get("response_id"), str)
    )


def _messages_with_approval_receipt(
    messages: list[Message],
    *,
    model_id: int,
) -> _ApprovalReceiptProjection | None:
    receipt_context = _context.get()
    if receipt_context is None or model_id in receipt_context.fired_model_ids:
        return None
    receipt_context.fired_model_ids.add(model_id)
    outbound = list(messages)
    response_chain_index = next(
        (index for index in range(len(outbound) - 1, -1, -1) if _is_response_chain_boundary(outbound[index])),
        None,
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
            content=receipt_context.receipt_text,
            provider_data={_MARKER_KEY: True},
            add_to_agent_memory=False,
        )
        original_system_message = None
        outbound.insert(0 if response_chain_index is None else response_chain_index + 1, receipt_message)
    else:
        original_system_message = outbound[system_index]
        receipt_message = deepcopy(original_system_message)
        receipt_message.content = f"{receipt_message.content}\n\n{receipt_context.receipt_text}"
        receipt_message.provider_data = {
            **(receipt_message.provider_data if isinstance(receipt_message.provider_data, dict) else {}),
            _MARKER_KEY: True,
        }
        outbound[system_index] = receipt_message
    return _ApprovalReceiptProjection(
        caller_messages=messages,
        outbound_messages=outbound,
        receipt_message=receipt_message,
        original_system_message=original_system_message,
    )


def _install_approval_receipt_hook(model: Model) -> None:
    """Append a trusted approval receipt immediately before a resumed model call."""
    try:
        original_aresponse = cast("Callable[..., Awaitable[ModelResponse]]", model.aresponse)
        model_dict = vars(model)
    except (AttributeError, TypeError):
        return
    if model_dict.get(_HOOK_ATTR) is True:
        return
    setattr(model, _HOOK_ATTR, True)
    model_id = id(model)

    async def _aresponse_with_approval_receipt(*args: object, **kwargs: object) -> ModelResponse:
        messages: object = kwargs.get("messages")
        if isinstance(messages, list):
            projection = _messages_with_approval_receipt(cast("list[Message]", messages), model_id=model_id)
            if projection is None:
                return await original_aresponse(*args, **kwargs)
            try:
                return await original_aresponse(*args, **{**kwargs, "messages": projection.outbound_messages})
            finally:
                projection.publish_model_mutations()
        if args and isinstance(args[0], list):
            projection = _messages_with_approval_receipt(cast("list[Message]", args[0]), model_id=model_id)
            if projection is None:
                return await original_aresponse(*args, **kwargs)
            try:
                return await original_aresponse(projection.outbound_messages, *args[1:], **kwargs)
            finally:
                projection.publish_model_mutations()
        return await original_aresponse(*args, **kwargs)

    model_dict["aresponse"] = _aresponse_with_approval_receipt


def install_approval_receipt_hooks(model: Model, fallback_config: FallbackConfig | None) -> None:
    """Install the receipt hook on the primary model and every resolved fallback."""
    _install_approval_receipt_hook(model)
    if fallback_config is None:
        return
    fallbacks = (
        *fallback_config.on_error,
        *fallback_config.on_rate_limit,
        *fallback_config.on_context_overflow,
    )
    for fallback in fallbacks:
        if not isinstance(fallback, str):
            _install_approval_receipt_hook(fallback)
