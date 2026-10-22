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
    from collections.abc import Callable, Mapping

    from agno.tools.function import Function, FunctionCall

_PATCH_LOCK = Lock()
_PATCHED = False
# Parameter schemas are a few nodes; a schema past these limits keeps Agno's conversion.
_MAX_SCHEMA_NODES = 256
_MAX_ALTERNATIVES = 64

# One way a schema admits a value: its allowed JSON types and its choices, each None when unrestricted.
type _Alternative = tuple[frozenset[str] | None, tuple[object, ...] | None]

# AGNO_COMPAT: Tool-call argument parsing turns the strings none, null, true, and false into None and booleans.
# Reason: Agno 3.0.9 get_function_call replaces every top-level string argument whose stripped, lower-cased value is
# none, null, true, or false with None, True, or False, whatever the parameter's schema, so a string parameter never
# receives those words: a choice such as Graph's "none" or a message that is exactly "None" or "true" reaches the tool
# as None or a boolean. The model's string is put back when it is exactly one of the parameter's choices, or when the
# parameter takes free text and either Agno made a boolean of it or the parameter is required and does not admit null.
# An optional string, and a choice parameter for words that are not its choices, keep Agno's None for none and null,
# because models send those to leave an optional parameter out. Every other parameter keeps Agno's conversion, because
# models also send "true" for booleans and "null" for optional numbers.
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
    optional: bool
    choices: tuple[str, ...]

    def keeps(self, converted: object, sent: str) -> bool:
        """Return whether the parameter gets the model's string back from Agno's None or boolean."""
        if sent in self.choices:
            return True
        # An optional string keeps Agno's reading of "none" and "null" as left out.
        return self.free_text and (converted is not None or not self.optional)


class _SchemaTooLargeError(Exception):
    pass


def _items(value: object) -> list[Any]:
    # Schemas from MCP servers arrive as written, so a keyword may hold the wrong JSON type.
    return value if isinstance(value, list) else []


def _both(first: _Alternative, second: _Alternative) -> _Alternative:
    """Return what two schemas that both apply admit together."""
    (first_types, first_choices), (second_types, second_choices) = first, second
    types = first_types if second_types is None else second_types if first_types is None else first_types & second_types
    if first_choices is None or second_choices is None:
        return types, first_choices if second_choices is None else second_choices
    return types, tuple(choice for choice in first_choices if choice in second_choices)


def _referenced(reference: str, root: Mapping[str, Any]) -> Any:  # noqa: ANN401 - a JSON Schema node of any shape.
    """Return the schema a local JSON Pointer reference names, or None."""
    target: Any = root
    for part in reference[2:].split("/"):
        target = target.get(part.replace("~1", "/").replace("~0", "~")) if isinstance(target, dict) else None
    return target


def _alternatives(schema: Mapping[str, Any] | None, root: Mapping[str, Any], budget: list[int]) -> list[_Alternative]:
    """Return the alternatives a JSON Schema admits, reading allOf and local references as further restrictions.

    ``budget`` holds the nodes the whole walk may still read, so self-references end.
    """
    budget[0] -= 1
    if budget[0] < 0:
        raise _SchemaTooLargeError
    if not isinstance(schema, dict):
        return [(None, None)]
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


def _string_parameter(name: str, parameters: Mapping[str, Any]) -> _StringParameter | None:
    """Describe a parameter whose schema admits a string, or return None when it admits none."""
    try:
        alternatives = _alternatives(parameters["properties"].get(name), parameters, [_MAX_SCHEMA_NODES])
    except _SchemaTooLargeError:
        return None
    free_text = any(types is not None and "string" in types and choices is None for types, choices in alternatives)
    # Agno leaves null out of the schema of Optional[X], and of X | None from Python 3.14, but X stays out of required.
    optional = name not in _items(parameters.get("required")) or any(
        None in choices if choices is not None else types is not None and "null" in types
        for types, choices in alternatives
    )
    choices = tuple(
        choice
        for types, options in alternatives
        if types is None or "string" in types
        for choice in options or ()
        if isinstance(choice, str)
    )
    if not free_text and not choices:
        return None
    return _StringParameter(free_text=free_text, optional=optional, choices=choices)


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
        if (value is None or isinstance(value, bool)) and (parameter := _string_parameter(name, parameters)) is not None
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
