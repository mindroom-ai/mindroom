# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Map provider HTTP statuses, error wording, and transport failures to the normalized provider errors.

Every adapter classifies its SDK's failures through ``classify_provider_error``, and an error event inside an
already committed stream through ``stream_event_error``, so the stream retry stage and the media fallback see one
rule: rate limits and the transient statuses are retryable, a request that does not fit the context window is
``ContextWindowExceeded``, and the invalid-request statuses are final.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from mindroom.llm.errors import (
    TRANSPORT_TIMEOUT,
    ContextWindowExceeded,
    InvalidRequest,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "classify_provider_error",
    "is_context_window_error",
    "provider_failure",
    "stream_event_error",
]

_RATE_LIMIT_STATUS = 429
# Statuses after which an unchanged request can succeed: 409 is OpenAI's transient conflict, and 529 is
# Anthropic's overloaded model, which the error message reports as temporary unavailability, not as a rate limit.
_TRANSIENT_STATUSES = frozenset({408, 409, 500, 502, 503, 504, 529})
_INVALID_REQUEST_STATUSES = frozenset({400, 404, 413, 422})
# Anthropic reports an overloaded model with this error type, with or without a 529 status.
_OVERLOADED_ERROR_TYPE = "overloaded_error"
# The error type of a stream's error event that reports throttling.
_RATE_LIMIT_ERROR_TYPES = frozenset({"rate_limit_error"})
# The error types of a stream's error event that report a transient provider failure: Anthropic's, where
# ``timeout_error`` is a gateway timeout, and the ones OpenAI-compatible servers send.
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
    again. Wording that names the context window outranks a transient error type, because a retry cannot make the
    request fit.
    """
    status = int(code) if code is not None and code.isdecimal() else None
    if status == _RATE_LIMIT_STATUS or error_type in _RATE_LIMIT_ERROR_TYPES:
        return RateLimited(message, provider=provider, code=code)
    if status in _TRANSIENT_STATUSES:
        return ProviderUnavailable(message, provider=provider, code=code)
    if is_context_window_error(message, code=code):
        return ContextWindowExceeded(message, provider=provider, code=code)
    if error_type in _TRANSIENT_ERROR_TYPES:
        return ProviderUnavailable(message, provider=provider, code=code)
    return ProviderError(message, provider=provider, code=code)


def provider_failure(error: BaseException, *, provider: str) -> ProviderError | None:
    """Return the normalized error a transport or Google sign-in failure is, or None for the adapter's own mapping.

    The error and every cause it shows count, so a network failure that an SDK, the Google auth library, or a CLI
    login refresh wrapped in an error of its own is still the retryable failure it is: a refused or reset
    connection, a timeout, an HTTP client or aiohttp transport error, or a Google token endpoint that could not be
    reached or answered with a retryable error. Google credentials the token endpoint rejected, such as a deleted
    service-account key, are an authentication failure; credentials that cannot be found or loaded stay a
    configuration error.
    """
    import aiohttp  # noqa: PLC0415 - google-genai's transport, loaded only once a request fails
    from google.auth import exceptions as google_auth  # noqa: PLC0415 - loaded only once a request fails

    chain = list(_visible_chain(error))
    transport = (httpx.TransportError, ConnectionError, TimeoutError, aiohttp.ClientError, google_auth.TransportError)
    if any(
        isinstance(cause, transport) or (isinstance(cause, google_auth.RefreshError) and cause.retryable)
        for cause in chain
    ):
        timed_out = any(isinstance(cause, _TRANSPORT_TIMEOUTS) for cause in chain)
        code = TRANSPORT_TIMEOUT if timed_out else "connection_error"
        return ProviderUnavailable(str(error) or type(error).__name__, provider=provider, code=code)
    if any(isinstance(cause, google_auth.RefreshError) for cause in chain):
        return ProviderError(f"Google credentials were rejected: {error}", provider=provider, status_code=401)
    return None


def _visible_chain(error: BaseException) -> Iterator[BaseException]:
    """Yield the error and each cause it shows, explicit or implicit, once; a suppressed context ends the chain."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
