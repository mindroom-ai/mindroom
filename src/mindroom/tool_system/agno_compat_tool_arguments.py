"""Give string parameters the model's string through Agno's tool-call argument parsing."""

from __future__ import annotations

import ast
import json
from functools import wraps
from threading import Lock
from typing import TYPE_CHECKING, Any, cast

from agno.utils import tools as agno_tool_utils

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agno.tools.function import Function, FunctionCall

_PATCH_LOCK = Lock()
_PATCHED = False
_MAX_SCHEMA_DEPTH = 32

# AGNO_COMPAT: Tool-call argument parsing turns the strings none, null, true, and false into None and booleans.
# Reason: Agno 3.0.9 get_function_call replaces every top-level string argument whose stripped, lower-cased value is
# none, null, true, or false with None, True, or False, whatever the parameter's schema, so a string parameter never
# receives those words: a choice such as Graph's "none" or a message that is exactly "None" or "true" reaches the tool
# as None or a boolean. The model's string is put back for each parameter whose schema accepts a string; every other
# parameter keeps Agno's conversion, because models do send "true" for booleans and "null" for optional numbers.
# Agno parses a model's tool calls and a resumed paused call's stored arguments through agno.utils.tools, so the patch
# wraps that module's get_function_call.
# Upstream issue: https://github.com/agno-agi/agno/issues/10459, open (checked October 22, 2026).
# Upstream PR: None open; https://github.com/agno-agi/agno/pull/10432 dropped the conversion for every parameter and was
# closed without merge.
# Remove when: Agno's get_function_call gives a string argument of a parameter whose schema accepts a string to the tool
# unchanged, for a model's tool calls and for a resumed paused call.
# Coverage: tests/test_agno_compat_tool_arguments.py and
# tests/test_tool_config_sync.py::test_registered_tool_contract, which sends every registered string parameter its
# choices and these words.


def _accepts_string(schema: Mapping[str, Any] | None, root: Mapping[str, Any], depth: int = 0) -> bool:
    """Return whether a JSON Schema admits a string, following references within the function's parameters."""
    if not isinstance(schema, dict) or depth > _MAX_SCHEMA_DEPTH:
        return False
    kind = schema.get("type")
    if kind == "string" or (isinstance(kind, list) and "string" in kind):
        return True
    if any(isinstance(value, str) for value in schema.get("enum") or ()) or isinstance(schema.get("const"), str):
        return True
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/"):
        target: Any = root
        for part in reference[2:].split("/"):
            target = target.get(part) if isinstance(target, dict) else None
        if _accepts_string(target, root, depth + 1):
            return True
    return any(
        _accepts_string(option, root, depth + 1)
        for key in ("anyOf", "oneOf", "allOf")
        for option in schema.get(key) or ()
    )


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
    converted = [
        name
        for name, value in parsed.items()
        if (value is None or isinstance(value, bool)) and _accepts_string(properties.get(name), parameters)
    ]
    if not converted:
        return
    original = _decoded_object(arguments)
    for name in converted:
        value = original.get(name)
        if isinstance(value, str):
            parsed[name] = value


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
