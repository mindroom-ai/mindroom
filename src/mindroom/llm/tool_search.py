"""Portable deferred tool search, for routes without native tool search (``DeferredTools.portable``).

The request keeps one fixed tool list without the deferred schemas, and the model reaches a deferred function through
``call_tool`` after ``tool_search`` returned its definition. Stored calls to deferred functions are shown to the
provider as ``call_tool`` calls (``portable_tool_search_stream``), so the request prefix stays byte-stable and
discovery never invalidates the prompt cache. The loop rewrites each resolvable ``call_tool`` call to the real function
before resolving it (``resolve_call_tool_calls``), so approval, the hook bridge, and stored history see the real name.
"""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Final, cast

from mindroom.llm.provider import DeferredTools

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Container

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelProvider, ModelRequest, StreamEvent

_CALL_TOOL_NAME: Final = "call_tool"


def arguments_object(value: object) -> dict[str, Any] | None:
    """Return call arguments parsed like the tool executor parses them when they are a JSON-serializable object.

    Model output can be malformed in many ways (unhashable literals, sets, deep nesting), so any failure returns None,
    "not an object", rather than raising an error that would fail the run.
    """
    try:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                # The tool executor reads Python-literal arguments too (``parse_tool_arguments``).
                value = ast.literal_eval(value)
        if value == []:
            # A call whose arguments are an empty list runs as a call without arguments.
            return {}
        if not isinstance(value, dict):
            return None
        json.dumps(value)
    except Exception:  # Any failure means undecodable, as the tool executor treats arguments.
        return None
    return cast("dict[str, Any]", value)


def portable_tool_search_stream(inner: ModelProvider, request: ModelRequest) -> AsyncIterator[StreamEvent]:
    """Stream ``request`` with its deferred tools hidden behind ``call_tool`` when its tool search is portable."""
    if (deferred := request.deferred_tools).portable:
        request = replace(
            request,
            messages=[_projected_message(message, deferred.names) for message in request.messages],
            tools=[tool for tool in request.tools if tool.name not in deferred.names],
            deferred_tools=DeferredTools(),
        )
    return inner.stream(request)


def _projected_message(message: Message, deferred: frozenset[str]) -> Message:
    if message.tool_calls:
        calls = [_wrapped_call(call, deferred) for call in message.tool_calls]
        if any(new is not old for new, old in zip(calls, message.tool_calls, strict=True)):
            return replace(message, tool_calls=calls)
        return message
    if message.tool_name in deferred:
        return replace(message, tool_name=_CALL_TOOL_NAME)
    return message


def _wrapped_call(call: dict[str, Any], deferred: frozenset[str]) -> dict[str, Any]:
    function = call["function"]
    if function["name"] not in deferred:
        return call
    # Malformed stored arguments wrap as empty, as the other wires read them (``providers/stored_tool_calls.py``).
    arguments = arguments_object(function.get("arguments")) or {}
    wrapped = json.dumps({"name": function["name"], "arguments": arguments}, ensure_ascii=False)
    return {**call, "function": {**function, "name": _CALL_TOOL_NAME, "arguments": wrapped}}


def resolve_call_tool_calls(tool_calls: list[dict[str, Any]], callable_names: Container[str]) -> list[dict[str, Any]]:
    """Rewrite each resolvable ``call_tool`` call to the function it names.

    A call naming an unknown function or carrying non-object arguments stays a ``call_tool`` call, whose fallback
    function tells the model how to retry.
    """
    return [_resolved_call(call, callable_names) for call in tool_calls]


def _resolved_call(call: dict[str, Any], callable_names: Container[str]) -> dict[str, Any]:
    function = call.get("function")
    if not isinstance(function, dict) or function.get("name") != _CALL_TOOL_NAME:
        return call
    payload = arguments_object(function.get("arguments") or "{}")
    if payload is None:
        return call
    name = payload.get("name")
    if not isinstance(name, str) or name == _CALL_TOOL_NAME or name not in callable_names:
        return call
    # Weaker models sometimes send the nested arguments as a JSON string.
    raw_arguments = payload.get("arguments")
    arguments = {} if raw_arguments in (None, "") else arguments_object(raw_arguments)
    if arguments is None:
        return call
    # Keys beside name and arguments, such as background_after or arguments flattened by a weaker model, belong to
    # the call too; nested arguments win a conflict, and a null extra means the key was omitted.
    extras = {key: value for key, value in payload.items() if key not in {"name", "arguments"} and value is not None}
    resolved_arguments = json.dumps({**extras, **arguments}, ensure_ascii=False)
    return {**call, "function": {**function, "name": name, "arguments": resolved_arguments}}


__all__ = ["arguments_object", "portable_tool_search_stream", "resolve_call_tool_calls"]
