"""Map OpenAI SDK, OpenAI-compatible server, and transport failures to the owned provider errors.

``retryable`` carries the transient-failure judgment the stream retry wrapper acts on; statuses and
codes are preserved so user-facing messages and compaction can still tell failures apart.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import httpx

from mindroom.llm.errors import ContextWindowExceeded, InvalidRequest, ProviderError, ProviderUnavailable, RateLimited

__all__ = [
    "missing_api_key_error",
    "provider_error",
    "responses_event_error",
    "sdk_failures",
]

# Statuses after which an unchanged request can succeed; 409 is OpenAI's transient conflict.
_TRANSIENT_STATUSES = frozenset({408, 409, 500, 502, 503, 504})
_RATE_LIMIT_STATUSES = frozenset({429, 529})
_INVALID_REQUEST_STATUSES = frozenset({400, 404, 413, 422})
# Message fragments providers use for an input that does not fit the context window.
_CONTEXT_WINDOW_MARKERS = (
    "context_length_exceeded",
    "context window",
    "maximum context length",
    "token limit",
    "too many tokens",
    "payload too large",
    "content_too_large",
    "request too large",
    "input too long",
    "prompt is too long",
    "prompt too long",
    "exceeds the model",
)
# Error types and codes OpenAI-compatible servers put in mid-stream SSE error events for transient failures.
_TRANSIENT_ERROR_TYPES = frozenset({"overloaded", "overloaded_error", "server_error", "internal_server_error"})
_RESPONSES_EVENT_STATUSES = {"server_error": 500, "rate_limit_exceeded": 429, "vector_store_timeout": 504}


def sdk_failures() -> tuple[type[Exception], ...]:
    """Return the exception types adapters map with ``provider_error``; resolved late to keep the SDK unloaded."""
    import openai  # noqa: PLC0415 - keep the SDK out of module import time

    return (openai.OpenAIError, httpx.TransportError)


def _error_fields(body: object) -> tuple[str | None, str | None, str | None]:
    """Return the message, code, and type of an OpenAI-style error body."""
    if not isinstance(body, Mapping):
        return (body if isinstance(body, str) and body else None), None, None
    fields = cast("Mapping[str, Any]", body)
    nested = fields.get("error")
    if isinstance(nested, Mapping):
        fields = cast("Mapping[str, Any]", nested)
    message, code, error_type = fields.get("message"), fields.get("code"), fields.get("type")
    return (
        message if isinstance(message, str) else None,
        str(code) if code is not None else None,
        error_type if isinstance(error_type, str) else None,
    )


def _classified(message: str, *, provider: str, status_code: int | None, code: str | None) -> ProviderError:
    lowered = message.lower()
    if code == "context_length_exceeded" or (
        status_code in _INVALID_REQUEST_STATUSES and any(marker in lowered for marker in _CONTEXT_WINDOW_MARKERS)
    ):
        return ContextWindowExceeded(message, provider=provider, status_code=status_code, code=code)
    if status_code in _RATE_LIMIT_STATUSES:
        return RateLimited(message, provider=provider, status_code=status_code, code=code)
    if status_code in _TRANSIENT_STATUSES:
        return ProviderUnavailable(message, provider=provider, status_code=status_code, code=code)
    if status_code in _INVALID_REQUEST_STATUSES:
        return InvalidRequest(message, provider=provider, status_code=status_code, code=code)
    return ProviderError(message, provider=provider, status_code=status_code, code=code)


def _stream_event_error(message: str, *, provider: str, code: str | None, error_type: str | None) -> ProviderError:
    """Classify an error event sent inside an already committed 200 stream."""
    transient = (
        (code is not None and code.isdecimal() and int(code) in _TRANSIENT_STATUSES | _RATE_LIMIT_STATUSES)
        or error_type in _TRANSIENT_ERROR_TYPES
        or (
            error_type == "api_error"
            and any(marker in message.casefold() for marker in ("overloaded", "internal server error"))
        )
    )
    if transient:
        return ProviderUnavailable(message, provider=provider, code=code)
    if code == "context_length_exceeded" or any(marker in message.lower() for marker in _CONTEXT_WINDOW_MARKERS):
        return ContextWindowExceeded(message, provider=provider, code=code)
    return ProviderError(message, provider=provider, code=code)


def provider_error(error: Exception, *, provider: str) -> ProviderError:
    """Return the owned error for one SDK or transport failure; ``provider`` is the metrics provider name."""
    import openai  # noqa: PLC0415 - keep the SDK out of module import time

    if isinstance(error, openai.APIStatusError):
        message, code, _ = _error_fields(error.body)
        if message is None:
            message = error.response.text or str(error)
        return _classified(message, provider=provider, status_code=error.status_code, code=code)
    if isinstance(error, openai.APIConnectionError | httpx.TransportError):
        return ProviderUnavailable(str(error) or type(error).__name__, provider=provider, code="connection_error")
    if isinstance(error, openai.APIError):
        message, code, error_type = _error_fields(error.body)
        return _stream_event_error(message or error.message, provider=provider, code=code, error_type=error_type)
    return ProviderError(str(error) or type(error).__name__, provider=provider)


def responses_event_error(code: str | None, message: str | None, *, provider: str) -> ProviderError:
    """Return the owned error for a Responses ``error`` or ``response.failed`` event."""
    status_code = _RESPONSES_EVENT_STATUSES.get(code, 400) if code is not None else 400
    return _classified(
        message or "OpenAI Responses stream failed",
        provider=provider,
        status_code=status_code,
        code=code,
    )


def missing_api_key_error(env_var: str, *, provider: str) -> ProviderError:
    """Return the error for a route that has no API key, worded as the user-facing message mapping expects."""
    message = f"{env_var} not set. Please set the {env_var} environment variable."
    return ProviderError(message, provider=provider, status_code=401, code="missing_api_key")
