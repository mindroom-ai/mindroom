"""Cached schema preparation for prompt-only tool descriptions."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from functools import lru_cache
from inspect import isfunction, ismethod, signature
from types import MethodType
from typing import TYPE_CHECKING, Any

from agno.tools.function import Function, UserInputField

if TYPE_CHECKING:
    from collections.abc import Callable


_CACHE_POSTPROCESSOR_ATTR = "_mindroom_schema_cache_postprocessor"
_CACHE_SOURCE_ATTR = "_mindroom_schema_cache_source"
type _SchemaCachePostprocessor = Callable[[Function, bool], None]


@dataclass(frozen=True, slots=True)
class _ProcessedFunctionSchema:
    """Processed prompt schema snapshot for one Function."""

    parameters: dict[str, Any]
    description: str | None
    user_input_schema: tuple[UserInputField, ...] | None


@dataclass(frozen=True, slots=True)
class _SchemaCacheEntrypoint:
    """Hashable schema inputs while retaining the live entrypoint out of the cache key."""

    cache_source: Callable[..., object]
    schema_fingerprint: tuple[str, str, str | None]
    entrypoint: Callable[..., object] = field(compare=False, hash=False, repr=False)

    def __hash__(self) -> int:
        return hash((self.cache_source, self.schema_fingerprint))


def set_schema_cache_postprocessor(function: Function, postprocessor: _SchemaCachePostprocessor) -> None:
    """Allow a custom entrypoint processor to reuse cached schema preparation.

    ``postprocessor`` must be a module-level function that applies the custom
    processor's complete behavior to a cache-owned ``Function``.
    """
    if (
        not isfunction(postprocessor)
        or "<locals>" in postprocessor.__qualname__
        or postprocessor.__closure__ is not None
    ):
        msg = "Schema cache postprocessors must be module-level functions."
        raise TypeError(msg)
    object.__setattr__(function, _CACHE_POSTPROCESSOR_ATTR, postprocessor)


def set_schema_cache_source(function: Function, source: Callable[..., object]) -> None:
    """Set a private stable cache source without changing the live entrypoint."""
    cache_source = source.__func__ if ismethod(source) else source
    if not isfunction(cache_source):
        msg = "Schema cache sources must be functions."
        raise TypeError(msg)
    object.__setattr__(function, _CACHE_SOURCE_ATTR, cache_source)


def get_schema_cache_source(function: Function) -> Callable[..., object] | None:
    """Return the private stable cache source for a custom schema processor."""
    source = getattr(function, _CACHE_SOURCE_ATTR, None)
    return source if isfunction(source) else None


def _is_stable_cache_postprocessor(postprocessor: object) -> bool:
    return (
        isfunction(postprocessor) and "<locals>" not in postprocessor.__qualname__ and postprocessor.__closure__ is None
    )


def _uses_default_entrypoint_processor(function: Function) -> bool:
    processor = function.process_entrypoint
    return (
        isinstance(processor, MethodType)
        and processor.__self__ is function
        and processor.__func__ is Function.process_entrypoint
    )


def _schema_cache_entrypoint(function: Function) -> _SchemaCacheEntrypoint | None:
    if function.entrypoint is None or not callable(function.entrypoint):
        return None

    source_callable = get_schema_cache_source(function) or function.entrypoint
    if isinstance(source_callable, MethodType) or ismethod(source_callable):
        source_callable = source_callable.__func__
    elif not isfunction(source_callable):
        return None

    try:
        return _SchemaCacheEntrypoint(
            cache_source=source_callable,
            schema_fingerprint=(
                str(signature(function.entrypoint)),
                repr(getattr(function.entrypoint, "__annotations__", {})),
                getattr(function.entrypoint, "__doc__", None),
            ),
            entrypoint=function.entrypoint,
        )
    except (TypeError, ValueError):
        return None


def cached_processed_schema(function: Function, *, strict: bool) -> _ProcessedFunctionSchema | None:
    """Return a private copy of the cached processed prompt schema for one Function.

    Never mutates ``function``. Returns ``None`` when the entrypoint or
    parameters cannot form a stable cache key, in which case callers must fall
    back to full entrypoint processing on a private copy.
    """
    if function.entrypoint is None:
        return None

    cache_postprocessor = getattr(function, _CACHE_POSTPROCESSOR_ATTR, None)
    if not _uses_default_entrypoint_processor(function) and cache_postprocessor is None:
        return None
    if cache_postprocessor is not None and not _is_stable_cache_postprocessor(cache_postprocessor):
        return None

    entrypoint = _schema_cache_entrypoint(function)
    if entrypoint is None:
        return None

    try:
        parameters_json = json.dumps(function.parameters, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return None

    snapshot = _cached_processed_function_schema(
        entrypoint,
        function.name,
        function.description,
        parameters_json,
        function.skip_entrypoint_processing,
        function.requires_user_input,
        tuple(function.user_input_fields) if function.user_input_fields is not None else None,
        function.strict,
        strict,
        cache_postprocessor,
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
    entrypoint: _SchemaCacheEntrypoint,
    name: str,
    description: str | None,
    parameters_json: str,
    skip_entrypoint_processing: bool,
    requires_user_input: bool | None,
    user_input_fields: tuple[str, ...] | None,
    function_strict: bool | None,
    strict: bool,
    cache_postprocessor: _SchemaCachePostprocessor | None,
) -> _ProcessedFunctionSchema:
    function = Function(
        name=name,
        description=description,
        parameters=json.loads(parameters_json),
        entrypoint=entrypoint.entrypoint,
        skip_entrypoint_processing=skip_entrypoint_processing,
        requires_user_input=requires_user_input,
        user_input_fields=list(user_input_fields) if user_input_fields is not None else None,
        strict=function_strict,
    )
    if cache_postprocessor is None:
        function.process_entrypoint(strict=strict)
    else:
        cache_postprocessor(function, strict)
    return _ProcessedFunctionSchema(
        parameters=deepcopy(function.parameters),
        description=function.description,
        user_input_schema=tuple(deepcopy(function.user_input_schema))
        if function.user_input_schema is not None
        else None,
    )
