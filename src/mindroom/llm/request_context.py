"""The per-request logging context callers bind around one model request, and the JSON view of arbitrary values.

Request telemetry (``LLM usage`` lines, the opt-in request log) attributes each provider request to the agent,
session, and correlation id bound here; the context travels with the task, so stages wrapped around the
adapter read it without being handed it.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, fields, is_dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "REQUEST_ATTRIBUTION_FIELDS",
    "JSONValue",
    "RequestAttribution",
    "bind_llm_request_log_context",
    "current_llm_request_log_context",
    "json_safe",
]

type _JSONScalar = str | int | float | bool | None
type JSONValue = _JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

_REQUEST_CONTEXT = ContextVar[dict[str, JSONValue] | None]("mindroom_llm_request_log_context", default=None)


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestAttribution:
    """The turn a provider request belongs to, as its request-log context names it; the turn's prompts stay out."""

    agent_id: str
    session_id: str
    room_id: str | None
    thread_id: str | None
    reply_to_event_id: str | None
    requester_id: str | None
    correlation_id: str | None


# The request-log context keys that attribute a request to its turn, which the usage line and response record repeat.
REQUEST_ATTRIBUTION_FIELDS = tuple(attribute.name for attribute in fields(RequestAttribution))


def json_safe(value: object) -> JSONValue:
    """Return ``value`` as JSON-serializable data; dataclasses become dicts and other unknown values their ``repr``."""
    normalized: object = value
    if not isinstance(normalized, type) and is_dataclass(normalized):
        normalized = asdict(normalized)
    if isinstance(normalized, dict):
        return {str(key): json_safe(item) for key, item in cast("dict[Any, Any]", normalized).items()}
    if isinstance(normalized, list | tuple | set | frozenset):
        return [json_safe(item) for item in cast("Any", normalized)]
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
