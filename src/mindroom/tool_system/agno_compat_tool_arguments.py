"""Give string parameters the model's string through Agno's tool-call argument parsing."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from functools import wraps
from threading import Lock
from typing import TYPE_CHECKING, Any, cast

from agno.utils import tools as agno_tool_utils

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from agno.tools.function import Function, FunctionCall

_PATCH_LOCK = Lock()
_PATCHED = False
_MAX_SCHEMA_DEPTH = 32

# AGNO_COMPAT: Tool-call argument parsing turns the strings none, null, true, and false into None and booleans.
# Reason: Agno 3.0.9 get_function_call replaces every top-level string argument whose stripped, lower-cased value is
# none, null, true, or false with None, True, or False, whatever the parameter's schema, so a string parameter never
# receives those words: a choice such as Graph's "none" or a message that is exactly "None" or "true" reaches the tool
# as None or a boolean. The model's string is put back when it is exactly one of the parameter's choices, or when the
# parameter takes free text and either Agno made a boolean of it or the parameter does not admit null. An optional
# string, and a choice parameter for words that are not its choices, keep Agno's None for none and null, because models
# send those to leave an optional parameter out (Agno renders Literal[...] | None without null). Every other parameter
# keeps Agno's conversion, because models also send "true" for booleans and "null" for optional numbers.
# Agno parses a model's tool calls and a resumed paused call's stored arguments through agno.utils.tools, so the patch
# wraps that module's get_function_call.
# Upstream issue: https://github.com/agno-agi/agno/issues/10459, open (checked October 22, 2026).
# Upstream PR: None open; https://github.com/agno-agi/agno/pull/10432 dropped the conversion for every parameter and was
# closed without merge.
# Remove when: Agno's get_function_call gives a parameter whose schema admits a string the model's string unchanged
# under the same rule, for a model's tool calls and for a resumed paused call.
# Coverage: tests/test_agno_compat_tool_arguments.py and
# tests/test_tool_config_sync.py::test_registered_tool_contract, which sends every registered string parameter its
# choices and these words.


@dataclass(frozen=True)
class _StringParameter:
    free_text: bool
    nullable: bool
    choices: tuple[object, ...]

    def keeps(self, converted: object, sent: str) -> bool:
        """Return whether the parameter gets the model's string back from Agno's None or boolean."""
        if sent in self.choices:
            return True
        # An optional string keeps Agno's reading of "none" and "null" as left out.
        return self.free_text and (converted is not None or not self.nullable)


def _schemas(schema: Mapping[str, Any] | None, root: Mapping[str, Any], depth: int = 0) -> Iterator[Mapping[str, Any]]:
    """Yield a JSON Schema and the schemas its anyOf, oneOf, allOf, and references within the parameters name."""
    if not isinstance(schema, dict) or depth > _MAX_SCHEMA_DEPTH:
        return
    yield schema
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/"):
        target: Any = root
        for part in reference[2:].split("/"):
            target = target.get(part) if isinstance(target, dict) else None
        yield from _schemas(target, root, depth + 1)
    for key in ("anyOf", "oneOf", "allOf"):
        for option in _items(schema.get(key)):
            yield from _schemas(option, root, depth + 1)


def _items(value: object) -> list[Any]:
    # Schemas from MCP servers arrive as written, so a keyword may hold the wrong JSON type.
    return value if isinstance(value, list) else []


def _string_parameter(schema: Mapping[str, Any] | None, root: Mapping[str, Any]) -> _StringParameter | None:
    """Describe a parameter whose schema admits a string, or return None when it admits none."""
    free_text = nullable = False
    choices: list[object] = []
    for option in _schemas(schema, root):
        declared = option.get("type")
        types = {kind for kind in (_items(declared) or [declared]) if isinstance(kind, str)}
        values = [*_items(option.get("enum")), *((option["const"],) if "const" in option else ())]
        free_text = free_text or ("string" in types and not values)
        nullable = nullable or "null" in types or None in values
        choices.extend(values)
    if not free_text and not any(isinstance(choice, str) for choice in choices):
        return None
    return _StringParameter(free_text=free_text, nullable=nullable, choices=tuple(choices))


def _decoded_object(arguments: str) -> Mapping[str, object]:
    # Agno's own decoding, JSON first and then a Python literal, which gave this call its arguments.
    try:
        decoded = json.loads(arguments)
    except ValueError:
        decoded = ast.literal_eval(arguments)
    return decoded if isinstance(decoded, dict) else {}


def _restore_string_arguments(call: FunctionCall, arguments: str) -> None:
    parsed = call.arguments
    parameters = call.function.parameters
    properties = parameters.get("properties")
    if not parsed or not isinstance(properties, dict):
        return
    converted = {
        name: (value, parameter)
        for name, value in parsed.items()
        if (value is None or isinstance(value, bool))
        and (parameter := _string_parameter(properties.get(name), parameters)) is not None
    }
    if not converted:
        return
    original = _decoded_object(arguments)
    for name, (value, parameter) in converted.items():
        sent = original.get(name)
        if isinstance(sent, str) and parameter.keeps(value, sent):
            parsed[name] = sent


def _keeping_string_arguments(
    get_function_call: Callable[..., FunctionCall | None],
) -> Callable[..., FunctionCall | None]:
    @wraps(get_function_call)
    def parse(
        name: str,
        arguments: str | None = None,
        call_id: str | None = None,
        functions: dict[str, Function] | None = None,
    ) -> FunctionCall | None:
        call = get_function_call(name=name, arguments=arguments, call_id=call_id, functions=functions)
        if call is not None and call.error is None and arguments:
            _restore_string_arguments(call, arguments)
        return call

    return parse


def install_patch() -> None:
    """Wrap Agno's tool-call argument parsing once per interpreter."""
    global _PATCHED
    with _PATCH_LOCK:
        if _PATCHED:
            return
        agno_tool_utils.get_function_call = cast("Any", _keeping_string_arguments(agno_tool_utils.get_function_call))
        _PATCHED = True
