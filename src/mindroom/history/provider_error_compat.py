"""Legacy provider error interpretation for compaction.

Agno sometimes wraps SDK failures as generic ModelProviderError values or text.
Keep cause-chain inspection and wording compatibility outside core retry policy.
"""

from __future__ import annotations

import httpx
from agno.exceptions import ModelProviderError

from mindroom.error_handling import TRANSIENT_PROVIDER_STATUS_CODES

# Status 502 is excluded because ``ModelProviderError`` uses it for unclassified
# errors. Default-502 errors retry only when their cause chain proves a typed
# network failure.
_TRANSIENT_SUMMARY_STATUS_CODES = TRANSIENT_PROVIDER_STATUS_CODES - {502}

_SHRINKABLE_PROVIDER_ERROR_FRAGMENTS = (
    "context length",
    "context_length_exceeded",
    "too many tokens",
    "max tokens",
    "too large",
    "too long",
    "input size",
    "input too large",
    "maximum length",
    "max length",
    "request too large",
    "reduce the length",
)
_TIMEOUT_PROVIDER_ERROR_FRAGMENT = "timed out"


def _has_typed_network_cause(error: ModelProviderError) -> bool:
    """Return whether visible explicit or implicit causes contain a typed network failure."""
    from anthropic import APIConnectionError as AnthropicAPIConnectionError  # noqa: PLC0415
    from openai import APIConnectionError as OpenAIAPIConnectionError  # noqa: PLC0415

    cause = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(
            cause,
            ConnectionError
            | TimeoutError
            | httpx.TransportError
            | AnthropicAPIConnectionError
            | OpenAIAPIConnectionError,
        ):
            return True
        cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
    return False


def is_transient_summary_error(error: Exception) -> bool:
    """Return whether a provider failure warrants one unchanged retry."""
    if not isinstance(error, ModelProviderError):
        return False
    if error.status_code in _TRANSIENT_SUMMARY_STATUS_CODES:
        return True
    return error.status_code == 502 and _has_typed_network_cause(error)


def is_provider_timeout(error: BaseException) -> bool:
    """Recognize timeout text from providers that do not preserve typed errors."""
    return _TIMEOUT_PROVIDER_ERROR_FRAGMENT in str(error).casefold()


def is_legacy_summary_size_error(error: Exception) -> bool:
    """Isolate old provider size/error wording from the compaction retry policy."""
    message = str(error).casefold()
    return any(fragment in message for fragment in _SHRINKABLE_PROVIDER_ERROR_FRAGMENTS) or (
        is_provider_timeout(error) and not is_transient_summary_error(error)
    )
