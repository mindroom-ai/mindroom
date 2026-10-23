# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Map provider HTTP statuses, error wording, and transport failures to the normalized provider errors.

Every adapter classifies its SDK's failures through ``classify_provider_error``, and an error event inside an
already committed stream through ``stream_event_error``, so the stream retry stage and the media fallback see one
rule: rate limits and the transient statuses are retryable, a request that does not fit the context window is
``ContextWindowExceeded``, and the invalid-request statuses are final.
"""

from __future__ import annotations

import httpx

from mindroom.llm.errors import (
    TRANSPORT_TIMEOUT,
    ContextWindowExceeded,
    InvalidRequest,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)

__all__ = [
    "classify_provider_error",
    "google_auth_error",
    "is_context_window_error",
    "stream_event_error",
    "transport_error",
]

_RATE_LIMIT_STATUS = 429
# Statuses after which an unchanged request can succeed: 409 is OpenAI's transient conflict, and 529 is
# Anthropic's overloaded model, which the error message reports as temporary unavailability, not as a rate limit.
_TRANSIENT_STATUSES = frozenset({408, 409, 500, 502, 503, 504, 529})
_INVALID_REQUEST_STATUSES = frozenset({400, 404, 413, 422})
# Anthropic reports an overloaded model with this error type, with or without a 529 status.
_OVERLOADED_ERROR_TYPE = "overloaded_error"
# Error types of a stream's error event that report throttling, and those that report a transient provider failure:
# Anthropic's, whose ``timeout_error`` is a gateway timeout, and those OpenAI-compatible servers send.
_RATE_LIMIT_ERROR_TYPES = frozenset({"rate_limit_error"})
_TRANSIENT_ERROR_TYPES = frozenset(
    {"overloaded", _OVERLOADED_ERROR_TYPE, "server_error", "internal_server_error", "api_error", "timeout_error"},
)
# Wording providers use when a request does not fit the model's context window (Agno's
# ``ModelProviderError.CONTEXT_WINDOW_PATTERNS`` without its over-broad ``max_tokens``, plus the
# payload-size wording OpenAI-compatible servers and Anthropic use).
_CONTEXT_WINDOW_MARKERS = (
    "context_length_exceeded",
    "context window",
    "context length",
    "maximum context length",
    "maximum number of tokens",
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
_HTTP_CLIENT_ERROR_RANGE = range(400, 500)
# A timeout as the HTTP client raises it, or as aiohttp and the socket layer raise it.
_TRANSPORT_TIMEOUTS = (httpx.TimeoutException, TimeoutError)


def is_context_window_error(message: str, *, code: str | None = None) -> bool:
    """Return whether an error's code or wording says the request does not fit the context window."""
    if code == "context_length_exceeded":
        return True
    lowered = message.casefold()
    return any(marker in lowered for marker in _CONTEXT_WINDOW_MARKERS)


def classify_provider_error(
    message: str,
    *,
    provider: str,
    status_code: int | None,
    code: str | None = None,
) -> ProviderError:
    """Return the normalized error for a provider response that failed with ``status_code``.

    A status outside the rate-limit, transient, and invalid-request sets, such as 401 or 403, is a final
    ``ProviderError``; context-window wording only counts on a client error, because a 5xx that mentions
    the context window is still the provider's failure to retry.
    """
    if status_code == _RATE_LIMIT_STATUS:
        return RateLimited(message, provider=provider, status_code=status_code, code=code)
    if _OVERLOADED_ERROR_TYPE in message.casefold():
        return ProviderUnavailable(message, provider=provider, status_code=status_code, code=code)
    if (status_code is None or status_code in _HTTP_CLIENT_ERROR_RANGE) and is_context_window_error(
        message,
        code=code,
    ):
        return ContextWindowExceeded(message, provider=provider, status_code=status_code, code=code)
    if status_code in _TRANSIENT_STATUSES:
        return ProviderUnavailable(message, provider=provider, status_code=status_code, code=code)
    if status_code in _INVALID_REQUEST_STATUSES:
        return InvalidRequest(message, provider=provider, status_code=status_code, code=code)
    return ProviderError(message, provider=provider, status_code=status_code, code=code)


def stream_event_error(
    message: str,
    *,
    provider: str,
    code: str | None = None,
    error_type: str | None = None,
) -> ProviderError:
    """Return the normalized error for an error event inside a stream whose HTTP response already succeeded.

    Such an event carries no HTTP status of its own: OpenAI-compatible servers put a status in ``code``, which
    counts as one, and Anthropic names the failure in ``error_type``. Throttling and transient failures, timeouts
    included, are retryable; any other event, such as an invalid request, is final, as the same request would fail
    again.
    """
    status = int(code) if code is not None and code.isdecimal() else None
    if status == _RATE_LIMIT_STATUS or error_type in _RATE_LIMIT_ERROR_TYPES:
        return RateLimited(message, provider=provider, code=code)
    if status in _TRANSIENT_STATUSES or error_type in _TRANSIENT_ERROR_TYPES:
        return ProviderUnavailable(message, provider=provider, code=code)
    if is_context_window_error(message, code=code):
        return ContextWindowExceeded(message, provider=provider, code=code)
    return ProviderError(message, provider=provider, code=code)


def transport_error(error: Exception, *, provider: str) -> ProviderUnavailable:
    """Return the retryable error for a network or transport failure before a response completed.

    A timeout carries ``TRANSPORT_TIMEOUT``, also when an SDK re-raised it as its own error, as the OpenAI and
    Anthropic SDKs raise ``APITimeoutError`` from the HTTP client's timeout.
    """
    timed_out = isinstance(error, _TRANSPORT_TIMEOUTS) or isinstance(error.__cause__, _TRANSPORT_TIMEOUTS)
    code = TRANSPORT_TIMEOUT if timed_out else "connection_error"
    return ProviderUnavailable(str(error) or type(error).__name__, provider=provider, code=code)


def google_auth_error(error: Exception, *, provider: str) -> ProviderError | None:
    """Return the normalized error for a failure to mint a Google access token, or None for any other error.

    Vertex clients refresh their token inside the request: a network failure or a retryable token-endpoint
    response can succeed later, while rejected credentials, such as a deleted service-account key, are final
    and report as an authentication failure. Credentials that cannot be found or loaded stay a configuration
    error.
    """
    from google.auth import exceptions  # noqa: PLC0415 - google-auth loads with the first Vertex client

    if isinstance(error, exceptions.TransportError) or (isinstance(error, exceptions.RefreshError) and error.retryable):
        return transport_error(error, provider=provider)
    if isinstance(error, exceptions.RefreshError):
        return ProviderError(f"Google credentials were rejected: {error}", provider=provider, status_code=401)
    return None
