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

from mindroom.config.main import Config
from mindroom.logging_config import get_logger
from mindroom.model_loading import canonical_provider
from mindroom.tool_dialects.claude import CLAUDE_DIALECT
from mindroom.tool_dialects.codex import CODEX_DIALECT
from mindroom.tool_dialects.types import (
    APPLY_PATCH,
    FILE_EDITS,
    MINDROOM_WIRE_KEY,
    DialectArgumentError,
    DialectName,
    ToolDialect,
    WireFunction,
    without_wire_record,
)
from mindroom.tool_system.output_files import OUTPUT_PATH_ARGUMENT

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agno.models.message import Message

    from mindroom.config.models import ModelConfig
    from mindroom.tool_system.tool_access import ToolKey

logger = get_logger(__name__)

_CLAUDE_PROVIDERS = frozenset({"anthropic", "vertexai_claude", "bedrock_claude"})
_CODEX_PROVIDERS = frozenset({"codex", "openai_codex"})
# These providers also front OpenAI-compatible servers, so only OpenAI model IDs select the Codex dialect.
_OPENAI_MODEL_PROVIDERS = frozenset({"openai", "azure"})
_OPENAI_MODEL_ID = re.compile(r"gpt-|o\d|codex")

# apply_patch exists for Codex models; every other dialect edits with edit_file and write_file.
_MINDROOM_DIALECT = ToolDialect(name="mindroom", replaced={APPLY_PATCH: FILE_EDITS})
_DIALECTS: dict[DialectName, ToolDialect] = {
    "mindroom": _MINDROOM_DIALECT,
    "claude": CLAUDE_DIALECT,
    "codex": CODEX_DIALECT,
}


@dataclass(frozen=True)
class _ToolCallError:
    """A wire tool *call* that could not become a canonical call, answered with *message* instead of running."""

    call: dict[str, Any]
    name: str
    message: str

    @property
    def call_id(self) -> str | None:
        """Return the provider ID the error answers."""
        return self.call.get("id")


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
    return _DIALECTS[_resolve_tool_dialect_name(model_config) if model_config is not None else "mindroom"]


def _tool_dict_name(tool: dict[str, Any]) -> str:
    """Return the name of one Agno-formatted or provider-built tool definition."""
    function = tool.get("function")
    if isinstance(function, dict):
        return str(function.get("name", ""))
    return str(tool.get("name", ""))


def _is_canonical(function: Function, key: ToolKey) -> bool:
    """Return whether *function* is canonical *key*, including when an authored preset brought its toolkit in."""
    return function.name == key.function and key.toolkit in Config.expand_tool_names([function.owning_toolkit or ""])


def _wire_function_for(dialect: ToolDialect, function: Function) -> WireFunction | None:
    return next(
        (wire_function for wire_function in dialect.functions if _is_canonical(function, wire_function.key)),
        None,
    )


def _wire_description(function: Function, wire_function: WireFunction) -> str:
    canonical_description = function.description or ""
    notes = [note for note in wire_function.carried_notes if note in canonical_description]
    return "\n\n".join((wire_function.description, *notes))


def _wire_parameters(function: Function, wire_function: WireFunction) -> dict[str, Any]:
    """Return the wire schema plus the MindRoom-managed output-path argument the canonical schema offers."""
    output_path = (function.parameters or {}).get("properties", {}).get(OUTPUT_PATH_ARGUMENT)
    if output_path is None:
        return wire_function.parameters
    properties = {**wire_function.parameters.get("properties", {}), OUTPUT_PATH_ARGUMENT: output_path}
    return {**wire_function.parameters, "properties": properties}


def _wire_tool_dict(function: Function, wire_function: WireFunction, *, custom_tools: bool) -> dict[str, Any]:
    if custom_tools and wire_function.custom_format is not None:
        return {
            "type": "custom",
            "name": wire_function.wire_name,
            "description": _wire_description(function, wire_function),
            "format": wire_function.custom_format,
        }
    definition = function.to_dict()
    # Canonical strictness describes the canonical schema, not the wire one.
    definition.pop("strict", None)
    definition.update(
        name=wire_function.wire_name,
        description=_wire_description(function, wire_function),
        parameters=_wire_parameters(function, wire_function),
    )
    return {"type": "function", "function": definition}


def presents(dialect: ToolDialect, function: Function, tools: Sequence[Function | dict[str, Any]]) -> bool:
    """Return whether *dialect* shows *function* among *tools*: not beside a function that replaces it."""
    replacements = next((keys for key, keys in dialect.replaced.items() if _is_canonical(function, key)), ())
    return not any(
        isinstance(tool, Function) and _is_canonical(tool, replacement)
        for tool in tools
        for replacement in replacements
    )


def wire_tools(
    dialect: ToolDialect,
    tools: Sequence[Function | dict[str, Any]],
    *,
    custom_tools: bool,
) -> list[Function | dict[str, Any]]:
    """Return *tools* with hidden functions dropped and canonical functions replaced by wire definitions.

    Other functions stay ``Function`` objects so the model's own tool formatting still applies to them.
    """
    taken = {tool.name if isinstance(tool, Function) else _tool_dict_name(tool) for tool in tools}
    presented: list[Function | dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Function):
            presented.append(tool)
            continue
        if not presents(dialect, tool, tools):
            continue
        wire_function = _wire_function_for(dialect, tool)
        if wire_function is not None and wire_function.wire_name != tool.name and wire_function.wire_name in taken:
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
    registered = Config.expand_tool_names([toolkit_name])
    return next(
        (
            wire_function.wire_name
            for wire_function in dialect.functions
            if wire_function.key.function == function_name and wire_function.key.toolkit in registered
        ),
        function_name,
    )


def _with_output_path(source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    if OUTPUT_PATH_ARGUMENT not in source:
        return target
    return {**target, OUTPUT_PATH_ARGUMENT: source[OUTPUT_PATH_ARGUMENT]}


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
        canonical_arguments = _with_output_path(arguments, wire_function.to_canonical(arguments))
    except json.JSONDecodeError as exc:
        return _ToolCallError(call=call, name=name, message=f"Error: Invalid JSON arguments for {name}: {exc}")
    except DialectArgumentError as exc:
        return _ToolCallError(call=call, name=name, message=f"Error: {exc}")
    translated = {
        **call,
        "function": {
            **call["function"],
            "name": wire_function.key.function,
            "arguments": json.dumps(canonical_arguments, ensure_ascii=False),
        },
    }
    if _with_output_path(canonical_arguments, wire_function.to_wire(canonical_arguments)) != arguments:
        # The translation dropped or reshaped something, so keep what the model sent for same-dialect replay.
        translated[MINDROOM_WIRE_KEY] = {"dialect": dialect.name, "name": name, "arguments": raw_arguments}
    return translated


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
        wire_function = by_wire_name.get(name)
        # A name that is a real function belongs to it, unless that function is the dialect's own same-named one.
        if wire_function is not None and name in functions and name != wire_function.key.function:
            wire_function = None
        function = functions.get(wire_function.key.function) if wire_function is not None else None
        if wire_function is None or function is None or not _is_canonical(function, wire_function.key):
            translated.append(call)
            continue
        result = _translate_call(dialect, call, wire_function)
        if isinstance(result, _ToolCallError):
            errors.append(result)
            translated.append(call)
        else:
            translated.append(result)
    return translated, errors


def _wire_call(dialect: ToolDialect, mapped: Mapping[str, WireFunction], call: dict[str, Any]) -> dict[str, Any]:
    """Return *call* as this request presents it, never carrying the wire record to the provider."""
    stripped = without_wire_record(call)
    function = call.get("function")
    if not isinstance(function, dict):
        return stripped
    wire_function = mapped.get(function.get("name", ""))
    if wire_function is None:
        return stripped
    wire = call.get(MINDROOM_WIRE_KEY)
    if isinstance(wire, dict) and wire.get("dialect") == dialect.name and wire.get("name") == wire_function.wire_name:
        return {**stripped, "function": {**function, "name": wire["name"], "arguments": wire["arguments"]}}
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except json.JSONDecodeError:
        return stripped
    if not isinstance(arguments, dict):
        return stripped
    wire_arguments = json.dumps(_with_output_path(arguments, wire_function.to_wire(arguments)), ensure_ascii=False)
    return {**stripped, "function": {**function, "name": wire_function.wire_name, "arguments": wire_arguments}}


def _wire_result(message: Message, wire_function: WireFunction) -> Message:
    """Return a tool result under the wire name, with fixed MindRoom templates reworded for the dialect."""
    render = wire_function.render_result
    update: dict[str, Any] = {"tool_name": wire_function.wire_name}
    if render is not None:
        update.update(
            {
                field: render(value)
                for field in ("content", "compressed_content")
                if isinstance(value := getattr(message, field), str)
            },
        )
    if all(getattr(message, field) == value for field, value in update.items()):
        return message
    return message.model_copy(update=update)


def _is_wire_definition(tool: dict[str, Any], wire_function: WireFunction) -> bool:
    """Return whether *tool* is this dialect's own definition, which a same-named foreign tool is not."""
    if tool.get("type") == "custom":
        return tool.get("format") == wire_function.custom_format
    function = tool.get("function")
    parameters = function.get("parameters") if isinstance(function, dict) else None
    properties = set(parameters.get("properties", {})) if isinstance(parameters, dict) else set()
    return properties - {OUTPUT_PATH_ARGUMENT} == set(wire_function.parameters.get("properties", {}))


def _presented_wire_functions(dialect: ToolDialect, tools: Sequence[dict[str, Any]]) -> dict[str, WireFunction]:
    """Return the dialect functions this request presents in wire form, by canonical name.

    A wire name that differs from the canonical one counts unless the canonical name is presented too, which
    means a collision kept the canonical function; a same-named one counts only with the dialect's own schema.
    """
    by_name = {_tool_dict_name(tool): tool for tool in tools}
    mapped: dict[str, WireFunction] = {}
    for wire_function in dialect.functions:
        tool = by_name.get(wire_function.wire_name)
        if tool is None:
            continue
        if wire_function.wire_name == wire_function.key.function:
            presented = _is_wire_definition(tool, wire_function)
        else:
            presented = wire_function.key.function not in by_name
        if presented:
            mapped[wire_function.key.function] = wire_function
    return mapped


def wire_messages(dialect: ToolDialect, messages: list[Message], tools: Sequence[dict[str, Any]]) -> list[Message]:
    """Return *messages* rendered for one provider request that presents the formatted *tools*.

    Only functions the request presents in wire form render; a call recorded in the active dialect under
    the same wire name replays its exact wire form, and other calls translate by canonical name.
    Messages that change are copied, so stored history stays canonical.
    """
    mapped = _presented_wire_functions(dialect, tools)
    rendered: list[Message] = []
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            calls = [_wire_call(dialect, mapped, call) for call in message.tool_calls]
            rendered.append(
                message if calls == message.tool_calls else message.model_copy(update={"tool_calls": calls}),
            )
        elif message.role == "tool" and (wire_function := mapped.get(message.tool_name or "")) is not None:
            rendered.append(_wire_result(message, wire_function))
        else:
            rendered.append(message)
    return rendered
