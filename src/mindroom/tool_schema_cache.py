"""Cached schema preparation for prompt-only tool descriptions."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from inspect import isfunction, ismethod
from types import MethodType
from typing import TYPE_CHECKING, Any

from agno.tools.function import Function, UserInputField

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True, slots=True)
class _ProcessedFunctionSchema:
    parameters: dict[str, Any]
    description: str | None
    user_input_schema: tuple[UserInputField, ...] | None


def process_function_schema_for_prompt(function: Function, *, strict: bool) -> None:
    """Process one Function schema using a cache for stable entrypoints."""
    if function.entrypoint is None:
        function.process_entrypoint(strict=strict)
        return

    processor = function.process_entrypoint
    if isinstance(processor, MethodType) and processor.__func__ is not Function.process_entrypoint:
        function.process_entrypoint(strict=strict)
        return

    source_callable = getattr(function.entrypoint, "__wrapped__", function.entrypoint)
    if isinstance(source_callable, MethodType) or ismethod(source_callable):
        source_callable = source_callable.__func__
    elif not isfunction(source_callable):
        function.process_entrypoint(strict=strict)
        return

    try:
        parameters_json = json.dumps(function.parameters, sort_keys=True, separators=(",", ":"))
    except TypeError:
        function.process_entrypoint(strict=strict)
        return

    snapshot = _cached_processed_function_schema(
        source_callable,
        function.name,
        function.description,
        parameters_json,
        function.skip_entrypoint_processing,
        function.requires_user_input,
        tuple(function.user_input_fields) if function.user_input_fields is not None else None,
        function.strict,
        strict,
    )
    function.parameters = deepcopy(snapshot.parameters)
    function.description = snapshot.description
    function.user_input_schema = (
        list(deepcopy(snapshot.user_input_schema)) if snapshot.user_input_schema is not None else None
    )


def clear_tool_schema_cache() -> None:
    """Clear cached schemas after plugin or tool code changes."""
    _cached_processed_function_schema.cache_clear()


@lru_cache(maxsize=4096)
def _cached_processed_function_schema(
    source_callable: Callable[..., object],
    name: str,
    description: str | None,
    parameters_json: str,
    skip_entrypoint_processing: bool,
    requires_user_input: bool | None,
    user_input_fields: tuple[str, ...] | None,
    function_strict: bool | None,
    strict: bool,
) -> _ProcessedFunctionSchema:
    function = Function(
        name=name,
        description=description,
        parameters=json.loads(parameters_json),
        entrypoint=source_callable,
        skip_entrypoint_processing=skip_entrypoint_processing,
        requires_user_input=requires_user_input,
        user_input_fields=list(user_input_fields) if user_input_fields is not None else None,
        strict=function_strict,
    )
    function.process_entrypoint(strict=strict)
    return _ProcessedFunctionSchema(
        parameters=deepcopy(function.parameters),
        description=function.description,
        user_input_schema=tuple(deepcopy(function.user_input_schema))
        if function.user_input_schema is not None
        else None,
    )
