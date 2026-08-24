"""Vendored Agno roleful-input and historical-media patch.

Agno Agent preserves ``list[Message]`` input as roleful provider messages, while
Agno Team currently flattens that same shape through ``get_text_from_message``.
This throwaway monkey-patch mirrors the Agent message-builder path until Agno
Team has the same upstream behavior.
Both builders also remove inline payloads from persisted history while keeping
current-turn media available to the provider.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from typing import Any, cast

from agno.agent import _messages as agent_messages
from agno.models.message import Message
from agno.run.messages import RunMessages
from agno.team import _messages as team_messages
from agno.utils.log import log_warning

_PATCHED = False
_PATCH_LOCK = threading.Lock()
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


def _strip_history_inline_media(run_messages: RunMessages) -> RunMessages:
    """Keep inline payloads on the current turn, not persisted history."""
    for message in run_messages.messages:
        if not message.from_history:
            continue
        message.audio = None
        message.images = None
        message.files = None
        message.videos = None
    return run_messages


def apply_patch() -> None:
    """Patch Agno Team run-message builders once per interpreter."""
    global _PATCHED
    if _PATCHED:
        return
    with _PATCH_LOCK:
        if _PATCHED:
            return

        original_team_get_run_messages = cast("_RunMessagesBuilder", team_messages._get_run_messages)
        original_team_aget_run_messages = cast("_AsyncRunMessagesBuilder", team_messages._aget_run_messages)
        original_agent_get_run_messages = cast("_RunMessagesBuilder", agent_messages.get_run_messages)
        original_agent_aget_run_messages = cast("_AsyncRunMessagesBuilder", agent_messages.aget_run_messages)

        def _get_run_messages(*args: object, **kwargs: object) -> RunMessages:
            input_message = kwargs.get("input_message")
            if not _is_roleful_message_list(input_message):
                return _strip_history_inline_media(original_team_get_run_messages(*args, **kwargs))

            passthrough_kwargs = {**kwargs, "input_message": None}
            run_messages = original_team_get_run_messages(*args, **passthrough_kwargs)
            _append_input_messages(run_messages, cast("_RolefulInput", input_message))
            return _strip_history_inline_media(run_messages)

        async def _aget_run_messages(*args: object, **kwargs: object) -> RunMessages:
            input_message = kwargs.get("input_message")
            if not _is_roleful_message_list(input_message):
                return _strip_history_inline_media(await original_team_aget_run_messages(*args, **kwargs))

            passthrough_kwargs = {**kwargs, "input_message": None}
            run_messages = await original_team_aget_run_messages(*args, **passthrough_kwargs)
            _append_input_messages(run_messages, cast("_RolefulInput", input_message))
            return _strip_history_inline_media(run_messages)

        def _agent_get_run_messages(*args: object, **kwargs: object) -> RunMessages:
            return _strip_history_inline_media(original_agent_get_run_messages(*args, **kwargs))

        async def _agent_aget_run_messages(*args: object, **kwargs: object) -> RunMessages:
            return _strip_history_inline_media(await original_agent_aget_run_messages(*args, **kwargs))

        team_messages._get_run_messages = cast("Any", _get_run_messages)
        team_messages._aget_run_messages = cast("Any", _aget_run_messages)
        agent_messages.get_run_messages = cast("Any", _agent_get_run_messages)
        agent_messages.aget_run_messages = cast("Any", _agent_aget_run_messages)
        _PATCHED = True
