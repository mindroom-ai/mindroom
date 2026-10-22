"""Map OpenAI SDK, OpenAI-compatible server, and transport failures to the owned provider errors.

``retryable`` carries the transient-failure judgment the stream retry wrapper acts on; statuses and
codes are preserved so user-facing messages and compaction can still tell failures apart.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import httpx

from mindroom.llm.errors import ContextWindowExceeded, ProviderError, ProviderUnavailable
from mindroom.providers.errors import (
    OVERLOADED_ERROR_TYPE,
    RATE_LIMIT_STATUSES,
    TRANSIENT_STATUSES,
    classify_provider_error,
    is_context_window_error,
    transport_error,
)

__all__ = [
    "missing_api_key_error",
    "provider_error",
    "responses_event_error",
    "sdk_failures",
]

# Error codes and types OpenAI-compatible servers put in mid-stream SSE error events for transient failures.
_TRANSIENT_EVENT_STATUSES = RATE_LIMIT_STATUSES | TRANSIENT_STATUSES
_TRANSIENT_ERROR_TYPES = frozenset({"overloaded", OVERLOADED_ERROR_TYPE, "server_error", "internal_server_error"})
_RESPONSES_EVENT_STATUSES = {"server_error": 500, "rate_limit_exceeded": 429, "vector_store_timeout": 504}


def sdk_failures() -> tuple[type[Exception], ...]:
    """Return the exception types adapters map with ``provider_error``; resolved late to keep the SDK unloaded."""
    import openai  # noqa: PLC0415 - keep the SDK out of module import time

    return (openai.OpenAIError, httpx.TransportError)


def _error_fields(body: object) -> tuple[str | None, str | None, str | None]:
    """Return the message, code, and type of an error body the SDK already unwrapped from its ``error`` envelope."""
    if not isinstance(body, Mapping):
        return (body if isinstance(body, str) and body else None), None, None
    fields = cast("Mapping[str, Any]", body)
    message, code, error_type = fields.get("message"), fields.get("code"), fields.get("type")
    return (
        message if isinstance(message, str) else None,
        str(code) if code is not None else None,
        error_type if isinstance(error_type, str) else None,
    )


def _stream_event_error(message: str, *, provider: str, code: str | None, error_type: str | None) -> ProviderError:
    """Classify an error event sent inside an already committed 200 stream."""
    transient = (
        (code is not None and code.isdecimal() and int(code) in _TRANSIENT_EVENT_STATUSES)
        or error_type in _TRANSIENT_ERROR_TYPES
        or (
            error_type == "api_error"
            and any(marker in message.casefold() for marker in ("overloaded", "internal server error"))
        )
    )
    if transient:
        return ProviderUnavailable(message, provider=provider, code=code)
    if is_context_window_error(message, code=code):
        return ContextWindowExceeded(message, provider=provider, code=code)
    return ProviderError(message, provider=provider, code=code)


def provider_error(error: Exception, *, provider: str) -> ProviderError:
    """Return the owned error for one SDK or transport failure; ``provider`` is the metrics provider name."""
    import openai  # noqa: PLC0415 - keep the SDK out of module import time

    if isinstance(error, openai.APIStatusError):
        message, code, _ = _error_fields(error.body)
        if message is None:
            message = error.response.text or str(error)
        return classify_provider_error(message, provider=provider, status_code=error.status_code, code=code)
    if isinstance(error, openai.APIConnectionError | httpx.TransportError):
        return transport_error(error, provider=provider)
    if isinstance(error, openai.APIError):
        message, code, error_type = _error_fields(error.body)
        return _stream_event_error(message or error.message, provider=provider, code=code, error_type=error_type)
    return ProviderError(str(error) or type(error).__name__, provider=provider)


def responses_event_error(code: str | None, message: str | None, *, provider: str) -> ProviderError:
    """Return the owned error for a Responses ``error`` or ``response.failed`` event."""
    status_code = _RESPONSES_EVENT_STATUSES.get(code, 400) if code is not None else 400
    return classify_provider_error(
        message or "OpenAI Responses stream failed",
        provider=provider,
        status_code=status_code,
        code=code,
    )


def missing_api_key_error(env_var: str, *, provider: str) -> ProviderError:
    """Return the error for a route that has no API key, worded as the user-facing message mapping expects."""
    message = f"{env_var} not set. Please set the {env_var} environment variable."
    return ProviderError(message, provider=provider, status_code=401, code="missing_api_key")
