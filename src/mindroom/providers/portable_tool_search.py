"""Portable deferred tool search for models without native tool search.

Deferred functions stay registered with Agno, so approval, hooks, worker
routing, and stored history all use their real names. Each provider request
instead carries one fixed tool list without the deferred schemas, and the model
reaches a deferred function through the ``call_tool`` wrapper after
``tool_search`` returned its definition. Incoming ``call_tool`` calls are
rewritten to the real function before Agno resolves them, and stored calls to
deferred functions are presented back to the provider as ``call_tool`` calls,
so the request prefix stays byte-stable and discovery never invalidates the
prompt cache.
"""

from __future__ import annotations

import ast
import json
from contextlib import aclosing
from functools import partial
from typing import TYPE_CHECKING, Any, Final, cast

from mindroom.agno_compat_model_hooks import install_async_invocation_hooks, install_tool_call_rewrite

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Container, Coroutine

    from agno.models.base import Model
    from agno.models.message import Message
    from agno.models.response import ModelResponse

__all__ = [
    "install_portable_deferred_tool_search",
    "portable_deferred_tool_names",
    "project_portable_request",
    "resolve_call_tool_calls",
]

_CALL_TOOL_NAME: Final = "call_tool"
_DEFERRED_TOOL_NAMES_ATTR = "_mindroom_portable_deferred_tool_names"
_INVOCATION_MARKER = "_mindroom_portable_tool_search_invocation"
_REWRITE_MARKER = "_mindroom_portable_tool_search_rewrite"


def install_portable_deferred_tool_search(model: Model, *, deferred_tool_names: frozenset[str]) -> None:
    """Hide the named functions from every provider request of one model behind ``call_tool``."""
    if not deferred_tool_names:
        return
    vars(model)[_DEFERRED_TOOL_NAMES_ATTR] = frozenset(deferred_tool_names)

    def projected(kwargs: dict[str, Any]) -> tuple[dict[str, Any], Callable[[], None]]:
        messages = kwargs.get("messages")
        if not isinstance(messages, list):
            return kwargs, lambda: None
        projected_messages, wire_tools = project_portable_request(
            messages,
            kwargs.get("tools"),
            portable_deferred_tool_names(model),
        )
        return {**kwargs, "messages": projected_messages, "tools": wire_tools}, partial(
            _publish_temporary_message_removal,
            messages,
            projected_messages,
        )

    def wrap_invoke(
        original: Callable[..., Coroutine[object, object, ModelResponse]],
    ) -> Callable[..., Coroutine[object, object, ModelResponse]]:
        async def invoke(*args: object, **kwargs: Any) -> ModelResponse:  # noqa: ANN401
            request, publish_temporary_message_removal = projected(kwargs)
            try:
                return await original(*args, **request)
            finally:
                publish_temporary_message_removal()

        return invoke

    def wrap_stream(
        original: Callable[..., AsyncIterator[ModelResponse]],
    ) -> Callable[..., AsyncIterator[ModelResponse]]:
        async def stream(*args: object, **kwargs: Any) -> AsyncIterator[ModelResponse]:  # noqa: ANN401
            request, publish_temporary_message_removal = projected(kwargs)
            try:
                async with aclosing(cast("AsyncGenerator[ModelResponse]", original(*args, **request))) as responses:
                    async for response in responses:
                        yield response
            finally:
                publish_temporary_message_removal()

        return stream

    install_async_invocation_hooks(model, marker=_INVOCATION_MARKER, wrap_invoke=wrap_invoke, wrap_stream=wrap_stream)
    install_tool_call_rewrite(model, marker=_REWRITE_MARKER, rewrite=resolve_call_tool_calls)


# AGNO_COMPAT: Retry guidance is removed from the request message list instead of run history.
# Reason: After a malformed function call, Agno's retry loop appends a temporary guidance message to the run's
# message list, and Gemini removes it after the retry succeeds by rewriting, in place, the list its invoke
# received. A projected request hands the provider a copy, so without mirroring the removal the guidance stays in
# run history and is replayed as an ordinary user message on every later turn.
# Upstream issue: Tracking gap; no Agno issue asks for the retry loop to own removal of its temporary messages.
# Upstream PR: None identified.
# Remove when: Agno removes temporary retry messages from the list its retry loop owns rather than inside a
# provider's invoke.
# Coverage: tests/test_portable_tool_search.py::test_retry_guidance_the_provider_drops_stays_out_of_history.
def _publish_temporary_message_removal(messages: list[Message], projected_messages: list[Message]) -> None:
    """Drop temporary messages from ``messages`` once the provider has dropped them from its projected request."""
    if any(message.temporary for message in messages) and not any(message.temporary for message in projected_messages):
        messages[:] = [message for message in messages if not message.temporary]


def portable_deferred_tool_names(model: object) -> frozenset[str]:
    """Return the function names one model hides behind ``call_tool``."""
    deferred_tool_names = vars(model).get(_DEFERRED_TOOL_NAMES_ATTR)
    return deferred_tool_names if isinstance(deferred_tool_names, frozenset) else frozenset()


def project_portable_request(
    messages: list[Message],
    tools: list[dict[str, Any]] | None,
    deferred: frozenset[str],
) -> tuple[list[Message], list[dict[str, Any]] | None]:
    """Return the provider view of one request: deferred schemas removed, deferred calls wrapped.

    Inputs are never mutated, and messages without deferred calls are passed through as the same objects.
    """
    wire_tools = None if tools is None else [tool for tool in tools if _tool_name(tool) not in deferred]
    return [_projected_message(message, deferred) for message in messages], wire_tools


def resolve_call_tool_calls(tool_calls: list[dict[str, Any]], callable_names: Container[str]) -> list[dict[str, Any]]:
    """Rewrite each resolvable ``call_tool`` call to the function it names.

    A call naming an unknown function or carrying non-object arguments stays a ``call_tool`` call, whose
    fallback function tells the model how to retry.
    """
    return [_resolved_call(call, callable_names) for call in tool_calls]


def _projected_message(message: Message, deferred: frozenset[str]) -> Message:
    if message.tool_calls:
        calls = [_wrapped_call(call, deferred) for call in message.tool_calls]
        if any(new is not old for new, old in zip(calls, message.tool_calls, strict=True)):
            return message.model_copy(update={"tool_calls": calls})
        return message
    if message.tool_name in deferred:
        return message.model_copy(update={"tool_name": _CALL_TOOL_NAME})
    return message


def _wrapped_call(call: dict[str, Any], deferred: frozenset[str]) -> dict[str, Any]:
    function = call.get("function")
    if not isinstance(function, dict):
        # LEGACY_COMPAT: Combined Gemini tool-result messages.
        # Legacy format: one role="tool" message with tool_call_id=None whose tool_calls entries each carry
        # tool_call_id, tool_name, and content; selected here when an entry has no "function" key.
        # Last legacy release: v2026.4.35 stored them through Agno 2.4.7's Gemini adapter; v2026.4.36 (Agno 2.5.13)
        # writes one canonical tool message per result.
        # Handling: entries naming a deferred function are renamed to call_tool, so the function responses Gemini
        # receives after normalize_tool_messages splits the message match the wrapped calls; stored history is unchanged.
        # Coverage: tests/test_portable_tool_search.py::test_projection_renames_results_in_combined_tool_messages.
        return {**call, "tool_name": _CALL_TOOL_NAME} if call.get("tool_name") in deferred else call
    if function.get("name") not in deferred:
        return call
    arguments = _json_object(function.get("arguments") or "{}") or {}
    wrapped = json.dumps({"name": function["name"], "arguments": arguments}, ensure_ascii=False)
    return {**call, "function": {**function, "name": _CALL_TOOL_NAME, "arguments": wrapped}}


def _resolved_call(call: dict[str, Any], callable_names: Container[str]) -> dict[str, Any]:
    function = call.get("function")
    if not isinstance(function, dict) or function.get("name") != _CALL_TOOL_NAME:
        return call
    payload = _json_object(function.get("arguments") or "{}")
    if payload is None:
        return call
    name = payload.get("name")
    if not isinstance(name, str) or name == _CALL_TOOL_NAME or name not in callable_names:
        return call
    # Weaker models sometimes send the nested arguments as a JSON string.
    raw_arguments = payload.get("arguments")
    arguments = {} if raw_arguments in (None, "") else _json_object(raw_arguments)
    if arguments is None:
        return call
    # Keys beside name and arguments, such as background_after or arguments flattened by a weaker model, belong to
    # the call too; nested arguments win a conflict, and a null extra means the key was omitted.
    extras = {key: value for key, value in payload.items() if key not in {"name", "arguments"} and value is not None}
    resolved_arguments = json.dumps({**extras, **arguments}, ensure_ascii=False)
    return {**call, "function": {**function, "name": name, "arguments": resolved_arguments}}


def _json_object(value: object) -> dict[str, Any] | None:
    """Return a JSON-serializable object parsed like Agno parses tool arguments, or None.

    Model output can be malformed in many ways (unhashable literals, sets, deep nesting), so any failure means
    "not an object" rather than an error that would fail the run.
    """
    try:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                # Agno accepts Python-literal arguments on direct calls, so the wrapper does too.
                value = ast.literal_eval(value)
        if not isinstance(value, dict):
            return None
        json.dumps(value)
    except Exception:  # Agno's argument parsing likewise treats any failure as undecodable.
        return None
    return cast("dict[str, Any]", value)


def _tool_name(tool: dict[str, Any]) -> object:
    function = tool.get("function")
    return function.get("name") if isinstance(function, dict) else tool.get("name")
