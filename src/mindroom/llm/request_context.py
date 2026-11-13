"""The context callers bind around model requests, the logging context and the request headers, and a JSON view of values.

Request telemetry (``LLM usage`` lines, the opt-in request log) attributes each provider request to the agent,
session, and correlation id bound here; the context travels with the task, so stages wrapped around the
adapter read it without being handed it. The request headers travel the same way to the adapters that send them.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, fields, is_dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

__all__ = [
    "REQUEST_ATTRIBUTION_FIELDS",
    "JSONValue",
    "RequestAttribution",
    "bind_llm_request_log_context",
    "bind_request_headers",
    "current_llm_request_log_context",
    "json_safe",
    "with_request_headers",
]

type _JSONScalar = str | int | float | bool | None
type JSONValue = _JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

_REQUEST_CONTEXT = ContextVar[dict[str, JSONValue] | None]("mindroom_llm_request_log_context", default=None)
_REQUEST_HEADERS: ContextVar[Callable[[], Mapping[str, str]] | None] = ContextVar(
    "mindroom_llm_request_headers",
    default=None,
)


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
    """Return ``value`` as JSON-serializable data.

    Dataclasses become dicts, bytes become base64 text as the provider SDKs send media, and other unknown values
    become their ``repr``.
    """
    normalized: object = value
    if not isinstance(normalized, type) and is_dataclass(normalized):
        normalized = asdict(normalized)
    if isinstance(normalized, bytes):
        return base64.b64encode(normalized).decode("ascii")
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
    bound = {key: json_safe(value) for key, value in context.items() if value is not None}
    token = _REQUEST_CONTEXT.set({**(_REQUEST_CONTEXT.get() or {}), **bound} or None)
    try:
        yield
    finally:
        _REQUEST_CONTEXT.reset(token)


@contextmanager
def bind_request_headers(headers: Callable[[], Mapping[str, str]]) -> Iterator[None]:
    """Bind the headers every model request sent in this context carries, which ``headers`` returns as each is sent."""
    token = _REQUEST_HEADERS.set(headers)
    try:
        yield
    finally:
        _REQUEST_HEADERS.reset(token)


def with_request_headers(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return one model request's SDK arguments with the bound headers over its own ``extra_headers``.

    The SDKs send a request's ``extra_headers`` over the client's default headers, so the bound headers win over
    every header the model's config authors. Without bound headers the arguments are returned as they are.
    """
    headers = _REQUEST_HEADERS.get()
    if headers is None:
        return kwargs
    return {**kwargs, "extra_headers": {**(kwargs.get("extra_headers") or {}), **headers()}}
