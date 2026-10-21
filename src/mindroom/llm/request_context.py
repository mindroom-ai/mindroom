"""The per-request logging context callers bind around one model request, and the JSON view of arbitrary values.

Request telemetry (``LLM usage`` lines, the opt-in request log) attributes each provider request to the agent,
session, and correlation id bound here; the context travels with the task, so stages wrapped around the
adapter read it without being handed it.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["JSONValue", "bind_llm_request_log_context", "current_llm_request_log_context", "json_safe"]

type _JSONScalar = str | int | float | bool | None
type JSONValue = _JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

_REQUEST_CONTEXT = ContextVar[dict[str, JSONValue] | None]("mindroom_llm_request_log_context", default=None)


def json_safe(value: object) -> JSONValue:
    """Return ``value`` as JSON-serializable data; pydantic models and dataclasses become dicts, bytes base64."""
    normalized: object = value
    if isinstance(normalized, BaseModel):
        normalized = normalized.model_dump(mode="python", exclude_none=True)
    elif not isinstance(normalized, type) and is_dataclass(normalized):
        normalized = asdict(normalized)
    if isinstance(normalized, dict):
        return {str(key): json_safe(item) for key, item in cast("dict[Any, Any]", normalized).items()}
    if isinstance(normalized, list | tuple | set | frozenset):
        return [json_safe(item) for item in cast("Any", normalized)]
    if isinstance(normalized, bytes):
        return {"__type__": "bytes", "base64": base64.b64encode(normalized).decode("ascii")}
    if isinstance(normalized, Path):
        return str(normalized)
    if isinstance(normalized, str | int | float | bool) or normalized is None:
        return normalized
    return repr(normalized)


def current_llm_request_log_context() -> dict[str, JSONValue]:
    """Return a detached copy of the request-log context bound to the current task."""
    return cast("dict[str, JSONValue]", json_safe(_REQUEST_CONTEXT.get() or {}))


@contextmanager
def bind_llm_request_log_context(**context: object) -> Iterator[None]:
    """Bind per-run request metadata so log entries can be attributed later; None values are left out."""
    bound_context = dict(_REQUEST_CONTEXT.get() or {})
    for key, value in context.items():
        if value is None:
            continue
        bound_context[str(key)] = json_safe(value)
    token = _REQUEST_CONTEXT.set(bound_context or None)
    try:
        yield
    finally:
        _REQUEST_CONTEXT.reset(token)
