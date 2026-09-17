"""Provider error interpretation for compaction and streaming.

Agno sometimes wraps SDK failures as generic ModelProviderError values or text.
Keep cause-chain inspection and wording compatibility outside core retry policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from agno.exceptions import ContextWindowExceededError, ModelProviderError

from mindroom.error_handling import (
    TRANSIENT_PROVIDER_STATUS_CODES,
    IncompleteResponsesStreamError,
    ModelSafeguardRefusalError,
)
from mindroom.model_instance_checks import isinstance_of_loaded

if TYPE_CHECKING:
    from collections.abc import Iterator

    from google.genai.errors import APIError as GoogleAPIError

# AGNO_COMPAT: Unclassified ModelProviderError failures default to HTTP 502.
# Reason: A generic 502 does not prove a transient provider failure; inspect its
# typed network cause chain before allowing an unchanged compaction retry.
# Streaming additionally accepts typed SDK statuses and structured SSE overloads.
# Upstream issue: https://github.com/agno-agi/agno/issues/8869
# Upstream PR: https://github.com/agno-agi/agno/pull/8870 (partial: preserves OpenAI
# codes and classifies Responses failures; generic and non-OpenAI errors remain).
# Remove when: Agno distinguishes unclassified failures from typed transient
# errors across supported providers; retain the owner's retry budget and policy.
# Coverage: tests/test_compaction_invariants.py::test_retry_policy_classifies_default_status_by_typed_network_chain;
# tests/test_compaction_invariants.py::test_retry_policy_does_not_retry_default_provider_error_status;
# tests/test_provider_stream_retry.py; tests/test_claude_stream_retry.py.
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


def _visible_causes(error: BaseException) -> Iterator[BaseException]:
    """Traverse visible causes once, respecting explicit context suppression."""
    cause = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        yield cause
        cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)


def _has_typed_network_cause(error: ModelProviderError) -> bool:
    """Return whether visible explicit or implicit causes contain a typed network failure."""
    import httpx  # noqa: PLC0415
    from anthropic import APIConnectionError as AnthropicAPIConnectionError  # noqa: PLC0415
    from openai import APIConnectionError as OpenAIAPIConnectionError  # noqa: PLC0415

    return any(
        isinstance(
            cause,
            ConnectionError
            | TimeoutError
            | httpx.TransportError
            | AnthropicAPIConnectionError
            | OpenAIAPIConnectionError,
        )
        for cause in _visible_causes(error)
    )


def _is_transient_sdk_error(error: BaseException) -> bool:
    """Recover status or structured SSE evidence that Agno replaced with default 502."""
    from anthropic import APIStatusError as AnthropicAPIStatusError  # noqa: PLC0415
    from openai import APIError as OpenAIAPIError  # noqa: PLC0415
    from openai import APIStatusError as OpenAIAPIStatusError  # noqa: PLC0415

    if isinstance(error, (AnthropicAPIStatusError, OpenAIAPIStatusError)):
        return error.status_code in TRANSIENT_PROVIDER_STATUS_CODES
    if isinstance_of_loaded(error, ("google.genai.errors", "APIError")):
        google_error: GoogleAPIError = cast("GoogleAPIError", error)
        return google_error.code in TRANSIENT_PROVIDER_STATUS_CODES
    if not isinstance(error, OpenAIAPIError) or not isinstance(error.body, dict):
        return False
    body = cast("dict[str, object]", error.body)
    nested = body.get("error")
    if isinstance(nested, dict):
        body = cast("dict[str, object]", nested)
    code = str(body.get("code", ""))
    if code.isdecimal():
        return code in {str(status) for status in TRANSIENT_PROVIDER_STATUS_CODES}
    error_type = body.get("type")
    if isinstance(error_type, str) and error_type in {
        "overloaded",
        "overloaded_error",
        "server_error",
        "internal_server_error",
    }:
        return True
    message = body.get("message")
    return (
        error_type == "api_error"
        and isinstance(message, str)
        and any(marker in message.casefold() for marker in ("overloaded", "internal server error"))
    )


def is_transient_stream_error(error: BaseException) -> bool:
    """Classify only proven transient failures before any meaningful stream output."""
    if not isinstance(error, ModelProviderError) or isinstance(
        error,
        (ContextWindowExceededError, ModelSafeguardRefusalError, IncompleteResponsesStreamError),
    ):
        return False
    if error.status_code != 502:
        return error.status_code in TRANSIENT_PROVIDER_STATUS_CODES
    return _has_typed_network_cause(error) or any(_is_transient_sdk_error(cause) for cause in _visible_causes(error))


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
