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
from typing import TYPE_CHECKING, Any, Final, cast

from mindroom.agno_compat_model_hooks import install_async_invocation_hooks, install_tool_call_rewrite

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Container, Coroutine

    from agno.models.base import Model
    from agno.models.message import Message
    from agno.models.response import ModelResponse

__all__ = [
    "CALL_TOOL_NAME",
    "install_portable_deferred_tool_search",
    "portable_deferred_tool_names",
    "project_portable_request",
    "resolve_call_tool_calls",
]

CALL_TOOL_NAME: Final = "call_tool"
_DEFERRED_TOOL_NAMES_ATTR = "_mindroom_portable_deferred_tool_names"
_INVOCATION_MARKER = "_mindroom_portable_tool_search_invocation"
_REWRITE_MARKER = "_mindroom_portable_tool_search_rewrite"


def install_portable_deferred_tool_search(model: Model, *, deferred_tool_names: frozenset[str]) -> None:
    """Hide the named functions from every provider request of one model behind ``call_tool``."""
    if not deferred_tool_names:
        return
    vars(model)[_DEFERRED_TOOL_NAMES_ATTR] = frozenset(deferred_tool_names)

    def projected(kwargs: dict[str, Any]) -> dict[str, Any]:
        messages = kwargs.get("messages")
        if not isinstance(messages, list):
            return kwargs
        projected_messages, wire_tools = project_portable_request(
            messages,
            kwargs.get("tools"),
            portable_deferred_tool_names(model),
        )
        return {**kwargs, "messages": projected_messages, "tools": wire_tools}

    def wrap_invoke(
        original: Callable[..., Coroutine[object, object, ModelResponse]],
    ) -> Callable[..., Coroutine[object, object, ModelResponse]]:
        async def invoke(*args: object, **kwargs: Any) -> ModelResponse:  # noqa: ANN401
            return await original(*args, **projected(kwargs))

        return invoke

    def wrap_stream(
        original: Callable[..., AsyncIterator[ModelResponse]],
    ) -> Callable[..., AsyncIterator[ModelResponse]]:
        def stream(*args: object, **kwargs: Any) -> AsyncIterator[ModelResponse]:  # noqa: ANN401
            return original(*args, **projected(kwargs))

        return stream

    install_async_invocation_hooks(model, marker=_INVOCATION_MARKER, wrap_invoke=wrap_invoke, wrap_stream=wrap_stream)
    install_tool_call_rewrite(model, marker=_REWRITE_MARKER, rewrite=resolve_call_tool_calls)


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
        return message.model_copy(update={"tool_name": CALL_TOOL_NAME})
    return message


def _wrapped_call(call: dict[str, Any], deferred: frozenset[str]) -> dict[str, Any]:
    function = call.get("function")
    if not isinstance(function, dict) or function.get("name") not in deferred:
        return call
    arguments = _json_object(function.get("arguments") or "{}") or {}
    wrapped = json.dumps({"name": function["name"], "arguments": arguments}, ensure_ascii=False)
    return {**call, "function": {**function, "name": CALL_TOOL_NAME, "arguments": wrapped}}


def _resolved_call(call: dict[str, Any], callable_names: Container[str]) -> dict[str, Any]:
    function = call.get("function")
    if not isinstance(function, dict) or function.get("name") != CALL_TOOL_NAME:
        return call
    payload = _json_object(function.get("arguments") or "{}")
    if payload is None:
        return call
    name = payload.get("name")
    if not isinstance(name, str) or name == CALL_TOOL_NAME or name not in callable_names:
        return call
    # Weaker models sometimes send the nested arguments as a JSON string.
    raw_arguments = payload.get("arguments")
    arguments = {} if raw_arguments in (None, "") else _json_object(raw_arguments)
    if arguments is None:
        return call
    # Keys beside name and arguments, such as background_after or arguments flattened by a weaker model, belong to
    # the call too; nested arguments win a conflict.
    extras = {key: value for key, value in payload.items() if key not in {"name", "arguments"}}
    resolved_arguments = json.dumps({**extras, **arguments}, ensure_ascii=False)
    return {**call, "function": {**function, "name": name, "arguments": resolved_arguments}}


def _json_object(value: object) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            # Agno accepts Python-literal arguments on direct calls, so the wrapper does too.
            try:
                value = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                return None
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _tool_name(tool: dict[str, Any]) -> object:
    function = tool.get("function")
    return function.get("name") if isinstance(function, dict) else tool.get("name")
