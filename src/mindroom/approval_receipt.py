"""Trusted model context describing one tool-approval continuation."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from agno.models.message import Message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator

    from agno.models.base import Model
    from agno.models.response import ModelResponse

_MARKER_KEY = "mindroom_approval_receipt"
_HOOK_ATTR = "_mindroom_approval_receipt_hook_installed"


@dataclass
class _ApprovalReceiptContext:
    receipt_text: str
    fired_model_ids: set[int] = field(default_factory=set)


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


def _messages_with_approval_receipt(
    messages: list[Message],
    *,
    model_id: int,
) -> list[Message]:
    receipt_context = _context.get()
    if receipt_context is None or model_id in receipt_context.fired_model_ids:
        return messages
    receipt_context.fired_model_ids.add(model_id)
    outbound = list(messages)
    system_index = next(
        (
            index
            for index, message in enumerate(outbound)
            if message.role in {"system", "developer"} and isinstance(message.content, str)
        ),
        None,
    )
    if system_index is None:
        outbound.insert(
            0,
            Message(
                role="system",
                content=receipt_context.receipt_text,
                provider_data={_MARKER_KEY: True},
                add_to_agent_memory=False,
            ),
        )
    else:
        system_message = deepcopy(outbound[system_index])
        system_message.content = f"{system_message.content}\n\n{receipt_context.receipt_text}"
        system_message.provider_data = {
            **(system_message.provider_data if isinstance(system_message.provider_data, dict) else {}),
            _MARKER_KEY: True,
        }
        outbound[system_index] = system_message
    return outbound


def install_approval_receipt_hook(model: Model) -> None:
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
            outbound = _messages_with_approval_receipt(cast("list[Message]", messages), model_id=model_id)
            return await original_aresponse(*args, **{**kwargs, "messages": outbound})
        if args and isinstance(args[0], list):
            outbound = _messages_with_approval_receipt(cast("list[Message]", args[0]), model_id=model_id)
            return await original_aresponse(outbound, *args[1:], **kwargs)
        return await original_aresponse(*args, **kwargs)

    model_dict["aresponse"] = _aresponse_with_approval_receipt
