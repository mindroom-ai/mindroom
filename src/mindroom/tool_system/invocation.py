"""Decode a model's tool-call arguments and run one tool call to its outcome.

``parse_tool_arguments`` reproduces how Agno 3.0.9 read a provider's arguments string, and ``execute_tool_call`` runs
the entrypoint the way Agno's loop did: framework parameters injected, synchronous bodies off the event loop under
the completion-ownership helpers, generators drained, and a raised exception turned into the error text the model
reads.
Middleware runs on the event loop around the leaf, outermost first, and may rewrite the arguments or answer the call
itself.
Whatever arguments reach the leaf, a value sent for a framework-injected parameter never replaces the injected object.
``current_tool_call()`` names the call being run to the middleware and the entrypoint below it, so a tool that needs
its own call id reads it there instead of taking it as a parameter.
``run_tool_call`` is the same execution for runners outside the loop that need the tool's own return value.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

from mindroom.tool_system.agno_toolkits import StreamCollector, framework_arguments, model_arguments, tool_value
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.tool_function import ToolValue
from mindroom.tool_system.tool_hooks import run_sync_tool_entrypoint

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from mindroom.llm.media import Audio, File, Image, Video
    from mindroom.tool_system.tool_function import ToolFunction

# What the loop sends the model for a call that names no function it offered; the same text Agno used.
MISSING_TOOL_ERROR_TEXT = "Error: The requested tool does not exist or is not available."
_DECODE_ERROR_TEXT = (
    "Error while decoding function arguments: {error}\n\nPlease make sure we can json.loads() the arguments and retry."
)
_NOT_AN_OBJECT_ERROR_TEXT = "Function arguments are not a valid JSON object.\n\n Please fix and retry."
# The session id of a call that belongs to no conversation, such as an MCP gateway or script-run call. Tools that key
# state by session, such as ``claude_agent``, share one session across those calls, the one they used when Agno's
# runners called them without a run context.
DETACHED_SESSION_ID = "default"


@dataclass(frozen=True, slots=True)
class ToolArgumentError:
    """A tool call the model sent with arguments that cannot be decoded; ``error_text`` is what the model reads."""

    error_text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCallContext:
    """The run a tool call belongs to, in owned terms.

    ``session_state`` is shared with the tool, which may mutate it in place; the caller persists it afterwards.
    ``call_id`` is the provider's id of the tool call being run, when the call came from a model.
    The media are the run's input media, offered to entrypoints that declare the reserved media parameters.
    """

    run_id: str
    session_id: str
    user_id: str | None = None
    call_id: str | None = None
    session_state: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    images: Sequence[Image] = ()
    audios: Sequence[Audio] = ()
    videos: Sequence[Video] = ()
    files: Sequence[File] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCall:
    """One tool call as middleware sees it: the function, the current arguments, and the run context."""

    function: ToolFunction
    arguments: dict[str, Any]
    context: ToolCallContext


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolOutcome(ToolValue):
    """What one tool call produced: the text the model reads (the error text on failure) and the tool's media."""

    stop_after_call: bool = False
    is_error: bool = False


type ToolNext = Callable[[dict[str, Any]], Awaitable[object]]
type ToolMiddleware = Callable[[ToolCall, ToolNext], Awaitable[object]]

_CURRENT_CALL: ContextVar[ToolCall | None] = ContextVar("mindroom_current_tool_call", default=None)


def current_tool_call() -> ToolCall | None:
    """Return the tool call ``run_tool_call`` is running in this task, with the arguments it was entered with."""
    return _CURRENT_CALL.get()


def _literal_strings_to_values(value: object) -> object:
    # Agno reads the strings "none", "null", "true", and "false" (any case, padded) at the top level as values.
    if not isinstance(value, str):
        return value
    lowered = value.strip().lower()
    if lowered in {"none", "null"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return value


def parse_tool_arguments(
    raw_arguments: str | None,
    function: ToolFunction,
) -> dict[str, Any] | None | ToolArgumentError:
    """Decode a provider's arguments string into the arguments a call stores and runs with.

    JSON first, then a Python literal; ``[]`` means the call has no arguments and decodes to None; any other
    non-object is an error the model reads.
    Values the model sent for parameters the framework supplies are dropped.
    """
    if not raw_arguments:
        return {}
    try:
        try:
            decoded = json.loads(raw_arguments)
        except Exception:
            decoded = ast.literal_eval(raw_arguments)
    except Exception as error:
        return ToolArgumentError(_DECODE_ERROR_TEXT.format(error=error))
    if isinstance(decoded, list) and not decoded:
        return None
    if not isinstance(decoded, dict):
        return ToolArgumentError(_NOT_AN_OBJECT_ERROR_TEXT)
    cleaned = {name: _literal_strings_to_values(value) for name, value in decoded.items()}
    return model_arguments(cleaned, function)


async def run_tool_call(
    function: ToolFunction,
    arguments: Mapping[str, Any] | None,
    context: ToolCallContext,
    *,
    middleware: Sequence[ToolMiddleware] = (),
) -> object:
    """Run one tool call through ``middleware`` to its entrypoint and return what it returned, drained if it streamed.

    Runners outside the model loop that hand the tool's own value onward (background scripts, the MCP gateway,
    scheduled calls) use this; an exception the call raises propagates.
    Values for framework-injected parameters are dropped before middleware sees the arguments, so a resumed call
    running from its stored arguments is as clean as a freshly parsed one.
    """
    call = ToolCall(function=function, arguments=model_arguments(arguments or {}, function), context=context)
    token = _CURRENT_CALL.set(call)
    try:
        return await _run_chain(call, middleware)
    finally:
        _CURRENT_CALL.reset(token)


async def execute_tool_call(
    function: ToolFunction,
    arguments: Mapping[str, Any] | None,
    context: ToolCallContext,
    *,
    middleware: Sequence[ToolMiddleware] = (),
) -> ToolOutcome:
    """Run one tool call as ``run_tool_call`` does and return what the model should read.

    Any exception other than cancellation becomes a failed outcome whose content is the exception's text. The run
    stops after the call when the function always stops it, or when this call returned an outcome that does, as
    minimal mode's Bash does after a control tool it ran.
    """
    try:
        value = tool_value(await run_tool_call(function, arguments, context, middleware=middleware))
    except asyncio.CancelledError:
        raise
    except Exception as error:
        return ToolOutcome(content=str(error), stop_after_call=function.stop_after_tool_call, is_error=True)
    return ToolOutcome(
        content=value.content,
        images=value.images,
        audios=value.audios,
        videos=value.videos,
        files=value.files,
        stop_after_call=function.stop_after_tool_call or (isinstance(value, ToolOutcome) and value.stop_after_call),
    )


async def _run_chain(call: ToolCall, middleware: Sequence[ToolMiddleware]) -> object:
    async def leaf(arguments: dict[str, Any]) -> object:
        return await _run_entrypoint(call.function, arguments, call.context)

    call_next: ToolNext = leaf
    for step in reversed(middleware):
        call_next = _link(step, call, call_next)
    return await call_next(call.arguments)


def _link(step: ToolMiddleware, call: ToolCall, call_next: ToolNext) -> ToolNext:
    async def run(arguments: dict[str, Any]) -> object:
        return await step(ToolCall(function=call.function, arguments=arguments, context=call.context), call_next)

    return run


async def _run_entrypoint(function: ToolFunction, arguments: dict[str, Any], context: ToolCallContext) -> object:
    """Call the entrypoint with the framework's and the model's arguments; drain a generator it returns."""
    entrypoint = function.entrypoint
    # Arguments skip ``parse_tool_arguments`` when a paused call resumes from its stored arguments or middleware
    # rewrites them, so values for injected names are dropped here as well, as Agno dropped them at execution.
    # A media name the schema declares belongs to the model, so that value replaces the framework's.
    kwargs = {**framework_arguments(function, context), **model_arguments(arguments, function)}
    if inspect.iscoroutinefunction(entrypoint) or inspect.isasyncgenfunction(entrypoint):
        result = entrypoint(**kwargs)
    else:
        result = await run_sync_tool_entrypoint(partial(_invoke_and_drain_sync, entrypoint, kwargs), {})
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, AsyncIterator):
        return await _drain_async(result)
    return result


def _invoke_and_drain_sync(entrypoint: Callable[..., Any], kwargs: dict[str, Any]) -> object:
    """Run a synchronous entrypoint and, when it returns an iterator, consume it on the same worker thread."""
    result = entrypoint(**kwargs)
    if not isinstance(result, Iterator):
        return result
    collector = StreamCollector()
    try:
        for item in result:
            collector.add(item)
    finally:
        if inspect.isgenerator(result):
            result.close()
    return collector.value()


async def _drain_async(stream: AsyncIterator[object]) -> object:
    collector = StreamCollector()
    async with closing_async_stream(stream):
        async for item in stream:
            collector.add(item)
    return collector.value()


__all__ = [
    "DETACHED_SESSION_ID",
    "MISSING_TOOL_ERROR_TEXT",
    "ToolArgumentError",
    "ToolCall",
    "ToolCallContext",
    "ToolMiddleware",
    "ToolNext",
    "ToolOutcome",
    "current_tool_call",
    "execute_tool_call",
    "parse_tool_arguments",
    "run_tool_call",
]
