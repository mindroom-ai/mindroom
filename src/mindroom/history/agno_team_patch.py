"""Vendored Agno Team roleful-input patch.

Agno Agent preserves ``list[Message]`` input as roleful provider messages, while
Agno Team currently flattens that same shape through ``get_text_from_message``.
This throwaway monkey-patch mirrors the Agent message-builder path until Agno
Team has the same upstream behavior.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from agno.models.message import Message
from agno.run.messages import RunMessages
from agno.team import _messages
from agno.utils.log import log_warning

_PATCHED = False
type _RolefulInput = list[Message]
type _RunMessagesBuilder = Callable[..., RunMessages]
type _AsyncRunMessagesBuilder = Callable[..., Awaitable[RunMessages]]


def _is_roleful_message_list(input_message: object) -> bool:
    return isinstance(input_message, list) and bool(input_message) and isinstance(input_message[0], Message)


def _append_input_messages(run_messages: RunMessages, input_messages: list[Any]) -> None:
    roleful_messages: list[Message] = []
    for input_message in input_messages:
        if isinstance(input_message, Message):
            message = input_message
        else:
            try:
                message = Message.model_validate(input_message)
            except Exception as exc:
                log_warning(f"Failed to validate message: {exc}")
                continue
        roleful_messages.append(message)
    if not roleful_messages:
        return

    additional_input = list(run_messages.extra_messages or [])
    run_messages.messages.extend(roleful_messages)
    if roleful_messages[-1].role == "user":
        run_messages.user_message = roleful_messages[-1]
        roleful_history = roleful_messages[:-1]
    else:
        roleful_history = roleful_messages
    run_messages.extra_messages = [*roleful_history, *additional_input]


def apply_patch() -> None:
    """Patch Agno Team run-message builders once per interpreter."""
    global _PATCHED
    if _PATCHED:
        return

    original_get_run_messages = cast("_RunMessagesBuilder", _messages._get_run_messages)
    original_aget_run_messages = cast("_AsyncRunMessagesBuilder", _messages._aget_run_messages)

    def _get_run_messages(*args: object, **kwargs: object) -> RunMessages:
        input_message = kwargs.get("input_message")
        if not _is_roleful_message_list(input_message):
            return original_get_run_messages(*args, **kwargs)

        passthrough_kwargs = {**kwargs, "input_message": None}
        run_messages = original_get_run_messages(*args, **passthrough_kwargs)
        _append_input_messages(run_messages, cast("_RolefulInput", input_message))
        return run_messages

    async def _aget_run_messages(*args: object, **kwargs: object) -> RunMessages:
        input_message = kwargs.get("input_message")
        if not _is_roleful_message_list(input_message):
            return await original_aget_run_messages(*args, **kwargs)

        passthrough_kwargs = {**kwargs, "input_message": None}
        run_messages = await original_aget_run_messages(*args, **passthrough_kwargs)
        _append_input_messages(run_messages, cast("_RolefulInput", input_message))
        return run_messages

    _messages._get_run_messages = cast("Any", _get_run_messages)
    _messages._aget_run_messages = cast("Any", _aget_run_messages)
    _PATCHED = True
