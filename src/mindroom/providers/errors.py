# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Map provider HTTP statuses, error wording, and transport failures to the normalized provider errors.

Every adapter classifies its SDK's failures through ``classify_provider_error``, so the stream retry stage
and the media fallback see one rule: rate limits and the transient statuses are retryable, a request that
does not fit the context window is ``ContextWindowExceeded``, and the invalid-request statuses are final.
"""

from __future__ import annotations

from mindroom.llm.errors import (
    ContextWindowExceeded,
    InvalidRequest,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)

__all__ = [
    "CONTEXT_WINDOW_MARKERS",
    "OVERLOADED_ERROR_TYPE",
    "RATE_LIMIT_STATUSES",
    "TRANSIENT_STATUSES",
    "classify_provider_error",
    "is_context_window_error",
    "transport_error",
]

RATE_LIMIT_STATUSES = frozenset({429, 529})
# Statuses after which an unchanged request can succeed; 409 is OpenAI's transient conflict.
TRANSIENT_STATUSES = frozenset({408, 409, 500, 502, 503, 504})
_INVALID_REQUEST_STATUSES = frozenset({400, 404, 413, 422})
# Anthropic reports an overloaded model with this error type, with or without a 529 status.
OVERLOADED_ERROR_TYPE = "overloaded_error"
# Wording providers use when a request does not fit the model's context window (Agno's
# ``ModelProviderError.CONTEXT_WINDOW_PATTERNS`` without its over-broad ``max_tokens``, plus the
# payload-size wording OpenAI-compatible servers and Anthropic use).
CONTEXT_WINDOW_MARKERS = (
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


def is_context_window_error(message: str, *, code: str | None = None) -> bool:
    """Return whether an error's code or wording says the request does not fit the context window."""
    if code == "context_length_exceeded":
        return True
    lowered = message.casefold()
    return any(marker in lowered for marker in CONTEXT_WINDOW_MARKERS)


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
    if status_code in RATE_LIMIT_STATUSES or OVERLOADED_ERROR_TYPE in message.casefold():
        return RateLimited(message, provider=provider, status_code=status_code, code=code)
    if (status_code is None or status_code in _HTTP_CLIENT_ERROR_RANGE) and is_context_window_error(
        message,
        code=code,
    ):
        return ContextWindowExceeded(message, provider=provider, status_code=status_code, code=code)
    if status_code in TRANSIENT_STATUSES:
        return ProviderUnavailable(message, provider=provider, status_code=status_code, code=code)
    if status_code in _INVALID_REQUEST_STATUSES:
        return InvalidRequest(message, provider=provider, status_code=status_code, code=code)
    return ProviderError(message, provider=provider, status_code=status_code, code=code)


def transport_error(error: Exception, *, provider: str) -> ProviderUnavailable:
    """Return the retryable error for a network or transport failure before a response completed."""
    return ProviderUnavailable(str(error) or type(error).__name__, provider=provider, code="connection_error")
