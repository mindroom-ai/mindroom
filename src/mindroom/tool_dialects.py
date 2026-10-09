"""Translate MindRoom's canonical tools to and from the tool interface each model family was trained on.

Inside MindRoom every tool call is canonical: approvals, hooks, worker routing, and stored history use
canonical names and arguments.
A dialect exists only on the provider wire, so the same history renders correctly for whichever
model a thread runs next.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agno.tools.function import Function

from mindroom.logging_config import get_logger
from mindroom.model_loading import canonical_provider
from mindroom.tool_dialect_types import MINDROOM_WIRE_KEY, DialectArgumentError, DialectName, ToolDialect, WireFunction
from mindroom.tool_system.tool_access import ToolKey

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agno.models.message import Message

    from mindroom.config.models import ModelConfig

logger = get_logger(__name__)

_CLAUDE_PROVIDERS = frozenset({"anthropic", "vertexai_claude", "bedrock_claude"})
_CODEX_PROVIDERS = frozenset({"codex", "openai_codex"})
# These providers also front OpenAI-compatible servers, so only OpenAI model IDs select the Codex dialect.
_OPENAI_MODEL_PROVIDERS = frozenset({"openai", "azure"})
_OPENAI_MODEL_ID = re.compile(r"gpt-|o\d|codex")

_DIALECTS: dict[DialectName, ToolDialect] = {"mindroom": ToolDialect(name="mindroom")}


@dataclass(frozen=True)
class _ToolCallError:
    """A wire tool call that could not become a canonical call, answered with *message* instead of running."""

    call_id: str | None
    name: str
    message: str


def _resolve_tool_dialect_name(model_config: ModelConfig) -> DialectName:
    """Return the dialect for *model_config*, detecting the model family when ``tool_dialect`` is ``auto``."""
    if model_config.tool_dialect != "auto":
        return model_config.tool_dialect
    provider = canonical_provider(model_config.provider)
    model_id = model_config.id.strip().lower()
    if provider == "openrouter":
        vendor, _, model_id = model_id.partition("/")
        provider = {"anthropic": "anthropic", "openai": "openai"}.get(vendor, provider)
    if provider in _CLAUDE_PROVIDERS:
        return "claude"
    if provider in _CODEX_PROVIDERS or (provider in _OPENAI_MODEL_PROVIDERS and _OPENAI_MODEL_ID.match(model_id)):
        return "codex"
    return "mindroom"


def resolve_tool_dialect(model_config: ModelConfig | None) -> ToolDialect:
    """Return the dialect object for *model_config*; an unknown model keeps MindRoom's own tools."""
    name = _resolve_tool_dialect_name(model_config) if model_config is not None else "mindroom"
    return _DIALECTS.get(name, _DIALECTS["mindroom"])


def _tool_dict_name(tool: dict[str, Any]) -> str:
    function = tool.get("function")
    if isinstance(function, dict):
        return str(function.get("name", ""))
    return str(tool.get("name", ""))


def _wire_function_for(dialect: ToolDialect, function: Function) -> WireFunction | None:
    return next(
        (
            wire_function
            for wire_function in dialect.functions
            if wire_function.key == ToolKey(function.owning_toolkit or "", function.name)
        ),
        None,
    )


def _wire_tool_dict(function: Function, wire_function: WireFunction, *, custom_tools: bool) -> dict[str, Any]:
    if custom_tools and wire_function.custom_format is not None:
        return {
            "type": "custom",
            "name": wire_function.wire_name,
            "description": wire_function.description,
            "format": wire_function.custom_format,
        }
    definition = function.to_dict()
    # Canonical strictness describes the canonical schema, not the wire one.
    definition.pop("strict", None)
    definition.update(
        name=wire_function.wire_name,
        description=wire_function.description,
        parameters=wire_function.parameters,
    )
    return {"type": "function", "function": definition}


def wire_tools(
    dialect: ToolDialect,
    tools: Sequence[Function | dict[str, Any]],
    *,
    custom_tools: bool,
) -> list[Function | dict[str, Any]]:
    """Return *tools* with hidden functions dropped and owner-matched functions replaced by wire definitions.

    Other functions stay ``Function`` objects so the model's own tool formatting still applies to them.
    """
    taken = {tool.name if isinstance(tool, Function) else _tool_dict_name(tool) for tool in tools}
    presented: list[Function | dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Function):
            presented.append(tool)
            continue
        if ToolKey(tool.owning_toolkit or "", tool.name) in dialect.hidden:
            continue
        wire_function = _wire_function_for(dialect, tool)
        if wire_function is not None and wire_function.wire_name in taken:
            logger.warning(
                "Tool dialect name collides with another tool; keeping the canonical name",
                dialect=dialect.name,
                function=tool.name,
                wire_name=wire_function.wire_name,
            )
            wire_function = None
        presented.append(
            tool if wire_function is None else _wire_tool_dict(tool, wire_function, custom_tools=custom_tools),
        )
    return presented


def wire_function_name(dialect: ToolDialect, toolkit_name: str, function_name: str) -> str:
    """Return the wire name of a canonical function, or its canonical name when *dialect* does not map it."""
    key = ToolKey(toolkit_name, function_name)
    return next(
        (wire_function.wire_name for wire_function in dialect.functions if wire_function.key == key),
        function_name,
    )


def _translate_call(
    dialect: ToolDialect,
    call: dict[str, Any],
    wire_function: WireFunction,
) -> dict[str, Any] | _ToolCallError:
    name = wire_function.wire_name
    raw_arguments = call["function"].get("arguments") or "{}"
    try:
        arguments = json.loads(raw_arguments)
        if not isinstance(arguments, dict):
            msg = f"{name} arguments must be a JSON object"
            raise DialectArgumentError(msg)  # noqa: TRY301
        canonical_arguments = wire_function.to_canonical(arguments)
    except json.JSONDecodeError as exc:
        return _ToolCallError(
            call_id=call.get("id"),
            name=name,
            message=f"Error: Invalid JSON arguments for {name}: {exc}",
        )
    except DialectArgumentError as exc:
        return _ToolCallError(call_id=call.get("id"), name=name, message=f"Error: {exc}")
    previous_wire = call.get(MINDROOM_WIRE_KEY)
    custom = isinstance(previous_wire, dict) and bool(previous_wire.get("custom"))
    return {
        **call,
        "function": {
            **call["function"],
            "name": wire_function.key.function,
            "arguments": json.dumps(canonical_arguments),
        },
        MINDROOM_WIRE_KEY: {
            "dialect": dialect.name,
            "toolkit": wire_function.key.toolkit,
            "name": name,
            "arguments": raw_arguments,
            "custom": custom,
        },
    }


def canonical_tool_calls(
    dialect: ToolDialect,
    tool_calls: list[dict[str, Any]],
    functions: Mapping[str, Function],
) -> tuple[list[dict[str, Any]], list[_ToolCallError]]:
    """Return *tool_calls* with wire calls made canonical, plus errors for calls that cannot translate.

    A call naming a function that exists keeps its name, so a wire name demoted by a collision still
    reaches the colliding function.
    Untranslatable calls stay in the returned list unchanged so history keeps every call.
    """
    by_wire_name = {wire_function.wire_name: wire_function for wire_function in dialect.functions}
    translated: list[dict[str, Any]] = []
    errors: list[_ToolCallError] = []
    for call in tool_calls:
        name = call.get("function", {}).get("name")
        wire_function = None if name in functions else by_wire_name.get(name)
        function = functions.get(wire_function.key.function) if wire_function is not None else None
        if wire_function is None or function is None or function.owning_toolkit != wire_function.key.toolkit:
            translated.append(call)
            continue
        result = _translate_call(dialect, call, wire_function)
        if isinstance(result, _ToolCallError):
            errors.append(result)
            translated.append(call)
        else:
            translated.append(result)
    return translated, errors


def _wire_call(dialect: ToolDialect, by_canonical: Mapping[str, WireFunction], call: dict[str, Any]) -> dict[str, Any]:
    function = call.get("function")
    if not isinstance(function, dict):
        return call
    wire = call.get(MINDROOM_WIRE_KEY)
    if isinstance(wire, dict) and wire.get("dialect") == dialect.name:
        return {**call, "function": {**function, "name": wire["name"], "arguments": wire["arguments"]}}
    wire_function = by_canonical.get(function.get("name", ""))
    if wire_function is None:
        return call
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except json.JSONDecodeError:
        return call
    if not isinstance(arguments, dict):
        return call
    wire_arguments = json.dumps(wire_function.to_wire(arguments))
    return {**call, "function": {**function, "name": wire_function.wire_name, "arguments": wire_arguments}}


def _wire_result(message: Message, wire_function: WireFunction) -> Message:
    render = wire_function.render_result
    if render is None:
        return message
    update = {
        field: render(value)
        for field in ("content", "compressed_content")
        if isinstance(value := getattr(message, field), str)
    }
    if all(getattr(message, field) == value for field, value in update.items()):
        return message
    return message.model_copy(update=update)


def wire_messages(dialect: ToolDialect, messages: list[Message]) -> list[Message]:
    """Return *messages* rendered in *dialect* for one provider request, copying only messages that change.

    A call recorded in the active dialect replays its exact wire form; other calls translate by canonical name.
    """
    by_canonical = {wire_function.key.function: wire_function for wire_function in dialect.functions}
    rendered: list[Message] = []
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            calls = [_wire_call(dialect, by_canonical, call) for call in message.tool_calls]
            rendered.append(
                message if calls == message.tool_calls else message.model_copy(update={"tool_calls": calls}),
            )
        elif message.role == "tool" and (wire_function := by_canonical.get(message.tool_name or "")) is not None:
            rendered.append(_wire_result(message, wire_function))
        else:
            rendered.append(message)
    return rendered
