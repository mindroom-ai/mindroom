"""Decode a model's tool-call arguments and run one tool call to its outcome.

``parse_tool_arguments`` reads a provider's arguments string; a parameter whose schema admits a string may keep the
words none, null, true, and false, which otherwise read as values.
``execute_tool_call`` runs the entrypoint: framework parameters injected, synchronous bodies off the event loop under
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
import inspect
import json
from collections.abc import AsyncIterator, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from mindroom.tool_system.agno_toolkits import StreamCollector, framework_arguments, model_arguments, tool_value
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.tool_function import ToolValue
from mindroom.tool_system.tool_hooks import run_sync_tool_entrypoint

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from mindroom.history.types import HistoryScope
    from mindroom.tool_system.tool_function import ToolFunction

# What the loop sends the model for a call that names no function it offered, kept as main behaves.
MISSING_TOOL_ERROR_TEXT = "Error: The requested tool does not exist or is not available."
_DECODE_ERROR_TEXT = (
    "Error while decoding function arguments: {error}\n\nPlease make sure we can json.loads() the arguments and retry."
)
_NOT_AN_OBJECT_ERROR_TEXT = "Function arguments are not a valid JSON object.\n\n Please fix and retry."
_LITERAL_WORDS: dict[str, bool | None] = {"none": None, "null": None, "true": True, "false": False}
# Parameter schemas are a few nodes; a schema past these limits reads the literal words as values.
_MAX_SCHEMA_NODES = 256
_MAX_ALTERNATIVES = 64
# The session id of a call that belongs to no conversation, such as an MCP gateway or script-run call. Tools that key
# state by session, such as ``claude_agent``, share one session across those calls, kept as main behaves.
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
    ``history_scope`` is the run's stored conversation when it is a team's, as ``RunContext`` names it.
    """

    run_id: str
    session_id: str
    user_id: str | None = None
    call_id: str | None = None
    session_state: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    history_scope: HistoryScope | None = None


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


# One way a parameter schema admits a value: its allowed JSON types and its choices, each None when unrestricted.
type _Alternative = tuple[frozenset[str] | None, tuple[object, ...] | None]


class _SchemaTooLargeError(Exception):
    """A parameter schema past the read limits; its literal words read as values."""


def _argument_value(name: str, value: object, parameters: Mapping[str, Any]) -> object:
    """Return the value the top-level argument ``name`` reaches the tool as, given the function's ``parameters``.

    Models send the strings none, null, true, and false (any case, padded) to mean null or a boolean, so those read
    as values, except for a parameter whose schema admits a string: it keeps the model's string when the string is
    exactly one of its choices, or when it takes free text and the word is a boolean or the parameter is required
    and does not admit null. An optional string, and a choice parameter for words that are not its choices, read
    none and null as left out, because models send those to leave an optional parameter out. A parameter that is
    not required is optional even when its schema omits null: Agno leaves null out of the schema of ``Optional[X]``
    and of a plain default, and of ``X | None`` from Python 3.14.
    """
    if not isinstance(value, str) or (word := value.strip().lower()) not in _LITERAL_WORDS:
        return value
    converted = _LITERAL_WORDS[word]
    properties = parameters.get("properties")
    schema = properties.get(name) if isinstance(properties, dict) else None
    try:
        alternatives = _alternatives(schema, parameters, [_MAX_SCHEMA_NODES])
    except _SchemaTooLargeError:
        return converted
    free_text = any(types is not None and "string" in types and choices is None for types, choices in alternatives)
    optional = name not in _items(parameters.get("required")) or any(
        None in choices if choices is not None else types is not None and "null" in types
        for types, choices in alternatives
    )
    choices = [
        choice
        for types, options in alternatives
        if types is None or "string" in types
        for choice in options or ()
        if isinstance(choice, str)
    ]
    if value in choices or (free_text and (converted is not None or not optional)):
        return value
    return converted


def _alternatives(schema: object, root: Mapping[str, Any], budget: list[int]) -> list[_Alternative]:
    """Return the alternatives a JSON Schema admits, reading allOf and local references as further restrictions.

    ``budget`` holds the nodes the whole walk may still read, so self-references end.
    """
    budget[0] -= 1
    if budget[0] < 0:
        raise _SchemaTooLargeError
    if not isinstance(schema, dict):
        return [(None, None)]
    schema = cast("dict[str, Any]", schema)
    declared = schema.get("type")
    types = None if declared is None else frozenset(k for k in (_items(declared) or [declared]) if isinstance(k, str))
    choices = tuple(_items(schema.get("enum"))) if "enum" in schema else None
    if "const" in schema:
        choices = _both((None, choices), (None, (schema["const"],)))[1]
    restrictions = [_alternatives(member, root, budget) for member in _items(schema.get("allOf"))]
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/"):
        restrictions.append(_alternatives(_referenced(reference, root), root, budget))
    for key in ("anyOf", "oneOf"):
        if members := _items(schema.get(key)):
            options: list[_Alternative] = []
            for member in members:
                options.extend(_alternatives(member, root, budget))
            restrictions.append(options)
    alternatives: list[_Alternative] = [(types, choices)]
    for restriction in restrictions:
        alternatives = [_both(first, second) for first in alternatives for second in restriction]
        if len(alternatives) > _MAX_ALTERNATIVES:
            raise _SchemaTooLargeError
    return alternatives


def _both(first: _Alternative, second: _Alternative) -> _Alternative:
    """Return what two schemas that both apply admit together."""
    (first_types, first_choices), (second_types, second_choices) = first, second
    types = first_types if second_types is None else second_types if first_types is None else first_types & second_types
    if first_choices is None or second_choices is None:
        return types, first_choices if second_choices is None else second_choices
    return types, tuple(choice for choice in first_choices if choice in second_choices)


def _referenced(reference: str, root: Mapping[str, Any]) -> object:
    """Return the schema a local JSON Pointer reference names, or None."""
    target: Any = root
    for part in reference[2:].split("/"):
        target = target.get(part.replace("~1", "/").replace("~0", "~")) if isinstance(target, dict) else None
    return target


def _items(value: object) -> list[Any]:
    # Schemas from MCP servers arrive as written, so a keyword may hold the wrong JSON type.
    return value if isinstance(value, list) else []


def parse_tool_arguments(
    raw_arguments: str | None,
    function: ToolFunction,
) -> dict[str, Any] | None | ToolArgumentError:
    """Decode a provider's arguments string into the arguments a call stores and runs with.

    JSON first, then a Python literal; ``[]`` means the call has no arguments and decodes to None; any other
    non-object is an error the model reads.
    Top-level strings none, null, true, and false read as ``_argument_value`` decides; a paused call stores the
    result and resumes with it, so a resumed call receives the same values.
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
    # TODO(simplify): ``[]`` decodes to None while an empty string decodes to ``{}``, two forms of "no arguments".
    # Both could decode to ``{}``. Stored data would notice: such a call's ``tool_args`` is stored as ``{}``, not null.
    if isinstance(decoded, list) and not decoded:
        return None
    if not isinstance(decoded, dict):
        return ToolArgumentError(_NOT_AN_OBJECT_ERROR_TEXT)
    cleaned = {name: _argument_value(name, value, function.parameters) for name, value in decoded.items()}
    return model_arguments(cleaned, function)


async def run_tool_call(
    function: ToolFunction,
    arguments: Mapping[str, Any] | None,
    context: ToolCallContext,
    *,
    middleware: Sequence[ToolMiddleware] = (),
) -> object:
    """Run one tool call through ``middleware``, then the function's own, to its entrypoint; return what it returned.

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
    for step in reversed((*middleware, *call.function.middleware)):
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
    # rewrites them, so values for injected names are dropped here as well.
    # A media name the schema declares belongs to the model, so that value replaces the framework's.
    kwargs = {**framework_arguments(function, context), **model_arguments(arguments, function)}
    if inspect.iscoroutinefunction(entrypoint) or inspect.isasyncgenfunction(entrypoint):
        result = entrypoint(**kwargs)
    else:
        result = await run_sync_tool_entrypoint(partial(_invoke_and_drain_sync, entrypoint, kwargs))
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
