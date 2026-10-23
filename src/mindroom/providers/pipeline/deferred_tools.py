"""Portable deferred tool search for routes without native tool search.

The request keeps one fixed tool list without the deferred schemas, and the model reaches a deferred
function through ``call_tool`` after ``tool_search`` returned its definition. Stored calls to deferred
functions are shown to the provider as ``call_tool`` calls, so the request prefix stays byte-stable and
discovery never invalidates the prompt cache. Incoming ``call_tool`` calls are resolved by the run loop
with ``mindroom.agent_loop.tool_search.resolve_call_tool_calls``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from mindroom.llm.call_tool import CALL_TOOL_NAME
from mindroom.providers.stored_tool_calls import call_arguments
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelProvider, ModelRequest, StreamEvent

__all__ = ["portable_tool_search_stream"]


def _wrapped_call(call: dict[str, Any], deferred: frozenset[str]) -> dict[str, Any]:
    function = call.get("function")
    # A persisted id-only placeholder has no function; ``providers/legacy_stored_tool_calls.py`` drops it later.
    if not isinstance(function, dict) or function.get("name") not in deferred:
        return call
    wrapped = json.dumps({"name": function["name"], "arguments": call_arguments(function)}, ensure_ascii=False)
    return {**call, "function": {**function, "name": CALL_TOOL_NAME, "arguments": wrapped}}


def _projected_message(message: Message, deferred: frozenset[str]) -> Message:
    if message.tool_calls:
        calls = [_wrapped_call(call, deferred) for call in message.tool_calls]
        if any(new is not old for new, old in zip(calls, message.tool_calls, strict=True)):
            return replace(message, tool_calls=calls)
        return message
    if message.tool_name in deferred:
        return replace(message, tool_name=CALL_TOOL_NAME)
    return message


def _projected_request(request: ModelRequest) -> ModelRequest:
    deferred = request.deferred_tool_names
    return replace(
        request,
        messages=[_projected_message(message, deferred) for message in request.messages],
        tools=[tool for tool in request.tools if tool.name not in deferred],
        deferred_tool_names=frozenset(),
    )


async def portable_tool_search_stream(inner: ModelProvider, request: ModelRequest) -> AsyncIterator[StreamEvent]:
    """Stream ``request`` with deferred tools hidden behind ``call_tool`` unless the route searches natively."""
    if request.deferred_tool_names and not inner.capabilities.native_tool_search:
        request = _projected_request(request)
    events = inner.stream(request)
    async with closing_async_stream(events):
        async for event in events:
            yield event
