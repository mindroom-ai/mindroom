"""Cached JSON schemas for prompt-only tool descriptions."""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass
from inspect import isfunction
from threading import Lock
from types import FunctionType, MethodType
from typing import Any
from weakref import ref

from agno.tools.function import Function

from mindroom.tool_system.declarations import tool_schema_source

type _SchemaCacheKey = tuple[ref[FunctionType], bool, bool, str]

_CACHE_SIZE = 4096
_SCHEMA_CACHE: OrderedDict[_SchemaCacheKey, str] = OrderedDict()
_SCHEMA_CACHE_LOCK = Lock()


@dataclass(frozen=True, slots=True)
class _ProcessedFunctionSchema:
    """Detached model-facing fields; no execution state or annotation objects."""

    parameters: dict[str, Any]
    description: str | None


def cached_processed_schema(function: Function, *, strict: bool) -> _ProcessedFunctionSchema | None:
    """Return a private prompt schema without extending the callable's lifetime.

    Unsupported processors, callables, or non-JSON schemas use the caller's
    existing uncached preparation path.
    """
    from mindroom.tool_system.output_files import (  # noqa: PLC0415 - Preserve the slim startup import boundary.
        uses_output_file_schema,
    )

    if function.entrypoint is None:
        return None

    output_file_schema = uses_output_file_schema(function)
    processor = function.process_entrypoint
    if not isinstance(processor, MethodType) or (
        processor.__func__ is not Function.process_entrypoint and not output_file_schema
    ):
        return None

    source = tool_schema_source(function.entrypoint)
    bound_method = isinstance(source, MethodType)
    if isinstance(source, MethodType):
        source = source.__func__
    if not isfunction(source):
        return None

    try:
        inputs = json.dumps(
            (
                function.name,
                function.description,
                function.parameters,
                function.skip_entrypoint_processing,
                function.requires_user_input,
                function.user_input_fields,
                function.strict,
                strict,
            ),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        return None
    key = (ref(source), bound_method, output_file_schema, inputs)
    with _SCHEMA_CACHE_LOCK:
        payload = _SCHEMA_CACHE.get(key)
        if payload is not None:
            _SCHEMA_CACHE.move_to_end(key)

    if payload is None:
        prepared = function.model_copy(deep=True)
        prepared.process_entrypoint(strict=strict)
        try:
            # JSON values cannot keep owners alive through annotations or container attributes.
            payload = json.dumps({"parameters": prepared.parameters, "description": prepared.description})
        except (TypeError, ValueError):
            return None
        with _SCHEMA_CACHE_LOCK:
            _SCHEMA_CACHE[key] = payload
            _SCHEMA_CACHE.move_to_end(key)
            if len(_SCHEMA_CACHE) > _CACHE_SIZE:
                _SCHEMA_CACHE.popitem(last=False)

    return _ProcessedFunctionSchema(**json.loads(payload))


def clear_tool_schema_cache() -> None:
    """Clear cached schemas after plugin or tool code changes."""
    with _SCHEMA_CACHE_LOCK:
        _SCHEMA_CACHE.clear()
