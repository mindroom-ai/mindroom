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
    """Processed prompt schema snapshot for one Function."""

    parameters: dict[str, Any]
    description: str | None
    user_input_schema: tuple[UserInputField, ...] | None


def cached_processed_schema(function: Function, *, strict: bool) -> _ProcessedFunctionSchema | None:
    """Return a private copy of the cached processed prompt schema for one Function.

    Never mutates ``function``. Returns ``None`` when the entrypoint or
    parameters cannot form a stable cache key, in which case callers must fall
    back to full entrypoint processing on a private copy.
    """
    from mindroom.tool_system.output_files import (  # noqa: PLC0415 - Keep workspace imports out of this leaf module's startup.
        output_file_schema_source,
    )

    if function.entrypoint is None:
        return None

    output_source = output_file_schema_source(function)
    processor = function.process_entrypoint
    if (
        isinstance(processor, MethodType)
        and processor.__func__ is not Function.process_entrypoint
        and output_source is None
    ):
        return None

    source_callable = output_source or getattr(function.entrypoint, "__wrapped__", function.entrypoint)
    bound_method = False
    if isinstance(source_callable, MethodType) or ismethod(source_callable):
        source_callable = source_callable.__func__
        bound_method = True
    if not isfunction(source_callable):
        return None

    if output_source is not None:
        # Factory functions can retain request state in defaults as well as closures.
        if (
            source_callable.__closure__
            or "<locals>" in source_callable.__qualname__
            or getattr(source_callable, "__wrapped__", None) is not None
        ):
            return None
        strict = False if function.strict is False else strict

    try:
        parameters_json = json.dumps(function.parameters, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return None

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
        output_source is not None,
        bound_method if output_source is not None else False,
    )
    # Copy at the boundary so callers can never corrupt the shared LRU entry.
    return _ProcessedFunctionSchema(
        parameters=deepcopy(snapshot.parameters),
        description=snapshot.description,
        user_input_schema=deepcopy(snapshot.user_input_schema) if snapshot.user_input_schema is not None else None,
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
    output_file: bool = False,
    output_file_bound_method: bool = False,
) -> _ProcessedFunctionSchema:
    if output_file:
        from mindroom.tool_system.output_files import output_file_schema_entrypoint  # noqa: PLC0415

        source_callable = output_file_schema_entrypoint(source_callable, bound_method=output_file_bound_method)
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
    if output_file:
        from mindroom.tool_system.output_files import ensure_output_path_schema_optional  # noqa: PLC0415

        ensure_output_path_schema_optional(function)
    return _ProcessedFunctionSchema(
        parameters=deepcopy(function.parameters),
        description=function.description,
        user_input_schema=tuple(deepcopy(function.user_input_schema))
        if function.user_input_schema is not None
        else None,
    )
