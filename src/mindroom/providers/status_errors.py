"""Map a provider's HTTP status failures and transport failures to the normalized provider errors."""

from __future__ import annotations

from mindroom.llm.errors import (
    ContextWindowExceeded,
    InvalidRequest,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)

_RATE_LIMIT_STATUSES = frozenset({429, 529})
_UNAVAILABLE_STATUSES = frozenset({408, 409, 500, 502, 503, 504})
_INVALID_REQUEST_STATUSES = frozenset({400, 404, 413, 422})
# Wording providers use when a request does not fit the model's context window.
_CONTEXT_WINDOW_FRAGMENTS = (
    "context window",
    "context length",
    "context_length_exceeded",
    "maximum context length",
    "maximum number of tokens",
    "too many tokens",
    "token limit",
    "prompt is too long",
    "prompt too long",
    "input too long",
)


def provider_error_for_status(message: str, *, provider: str, status_code: int | None) -> ProviderError:
    """Return the normalized error for a provider response that failed with ``status_code``."""
    if status_code in _RATE_LIMIT_STATUSES:
        return RateLimited(message, provider=provider, status_code=status_code)
    if status_code in _UNAVAILABLE_STATUSES:
        return ProviderUnavailable(message, provider=provider, status_code=status_code)
    lowered = message.lower()
    if any(fragment in lowered for fragment in _CONTEXT_WINDOW_FRAGMENTS):
        return ContextWindowExceeded(message, provider=provider, status_code=status_code)
    if status_code in _INVALID_REQUEST_STATUSES:
        return InvalidRequest(message, provider=provider, status_code=status_code)
    return ProviderError(message, provider=provider, status_code=status_code)


def provider_unavailable(error: Exception, *, provider: str) -> ProviderUnavailable:
    """Return the retryable error for a network or transport failure before a response completed."""
    return ProviderUnavailable(str(error) or type(error).__name__, provider=provider)


__all__ = ["provider_error_for_status", "provider_unavailable"]
