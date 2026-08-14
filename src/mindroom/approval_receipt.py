"""Trusted model context describing one tool-approval continuation."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
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
    receipt_fired: bool = False


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


def _append_approval_receipt_if_needed(messages: list[Message]) -> None:
    receipt_context = _context.get()
    if receipt_context is None or receipt_context.receipt_fired:
        return
    system_message = next(
        (
            message
            for message in messages
            if message.role in {"system", "developer"} and isinstance(message.content, str)
        ),
        None,
    )
    if system_message is None:
        messages.insert(
            0,
            Message(
                role="system",
                content=receipt_context.receipt_text,
                provider_data={_MARKER_KEY: True},
                add_to_agent_memory=False,
            ),
        )
    else:
        system_message.content = f"{system_message.content}\n\n{receipt_context.receipt_text}"
        system_message.provider_data = {
            **(system_message.provider_data if isinstance(system_message.provider_data, dict) else {}),
            _MARKER_KEY: True,
        }
    receipt_context.receipt_fired = True


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

    async def _aresponse_with_approval_receipt(*args: object, **kwargs: object) -> ModelResponse:
        messages: object = kwargs.get("messages")
        if not isinstance(messages, list) and args:
            messages = args[0]
        if isinstance(messages, list):
            _append_approval_receipt_if_needed(cast("list[Message]", messages))
        return await original_aresponse(*args, **kwargs)

    model_dict["aresponse"] = _aresponse_with_approval_receipt
