"""Shared schema constants and Function registration for schema-driven custom toolkits."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools.function import Function

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from agno.tools import Toolkit

_JSON_VALUE_SCHEMA: dict[str, object] = {
    "anyOf": [
        {"type": "object", "additionalProperties": True},
        {"type": "array", "items": {}},
        {"type": "string"},
        {"type": "number"},
        {"type": "integer"},
        {"type": "boolean"},
        {"type": "null"},
    ],
}
JSON_OBJECT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": _JSON_VALUE_SCHEMA,
}


def register_toolkit_functions(
    toolkit: Toolkit,
    *,
    entrypoints: Mapping[str, Callable[..., Awaitable[object]]],
    descriptions: Mapping[str, str],
    parameters: Mapping[str, dict[str, object]],
) -> None:
    """Register one async toolkit function per name from explicit schema mappings."""
    for function_name, entrypoint in entrypoints.items():
        toolkit.async_functions[function_name] = _toolkit_function(function_name, entrypoint, descriptions, parameters)


def _toolkit_function(
    function_name: str,
    entrypoint: Callable[..., object],
    descriptions: Mapping[str, str],
    parameters: Mapping[str, dict[str, object]],
) -> Function:
    return Function(
        name=function_name,
        description=descriptions[function_name],
        parameters=parameters[function_name],
        entrypoint=entrypoint,
        skip_entrypoint_processing=True,
    )
