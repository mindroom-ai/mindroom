"""Simple error handling for MindRoom agents."""

from __future__ import annotations

import ast
import json

from agno.exceptions import ContextWindowExceededError, ModelProviderError, ModelRateLimitError

from mindroom.logging_config import get_logger
from mindroom.redaction import redact_sensitive_text

logger = get_logger(__name__)

# Shared by provider retry policy and final user-message routing. Status 200 is
# the Claude mid-stream SSE error case: the HTTP response was already committed
# before the provider emitted an error event.
TRANSIENT_PROVIDER_STATUS_CODES = frozenset({200, 408, 409, 429, 500, 502, 503, 504, 529})

_CONTEXT_OVERFLOW_MARKERS = (
    "context_length_exceeded",
    "maximum context length",
    "input is too long for requested model",
    "input is too long for this model",
    "input too long",
    "prompt is too long",
)
_RATE_LIMIT_MARKERS = ("rate limit", "token rate", "tokens per minute", "requests per minute", "quota")
_OUTPUT_LIMIT_MARKERS = (
    "output token",
    "output exceeds",
    "output length",
    "max output",
    "maximum output",
    "completion token",
    "response truncated",
)
_INPUT_MARKERS = ("input", "prompt", "request")


class AvatarGenerationError(RuntimeError):
    """Raised when managed avatar generation cannot produce required assets."""


class AvatarSyncError(RuntimeError):
    """Raised when managed avatar sync cannot complete."""


def _extract_provider_from_error(error: Exception) -> str | None:
    """Try to extract the provider name from the exception's module."""
    module = type(error).__module__ or ""
    # e.g. "openai" from "openai._exceptions", "anthropic" from "anthropic._exceptions"
    top_module = module.split(".")[0] if module else ""
    known_providers = {"openai", "anthropic", "google", "groq", "cerebras", "httpx"}
    if top_module in known_providers:
        return top_module
    return None


def _structured_provider_error(error_str: str) -> tuple[str, str] | None:
    """Extract ``(type, message)`` from one JSON or Python-repr provider payload."""
    start = error_str.find("{")
    end = error_str.rfind("}")
    if start < 0 or end <= start:
        return None
    candidate = error_str[start : end + 1]
    try:
        payload = json.loads(candidate)
    except (TypeError, ValueError):
        try:
            payload = ast.literal_eval(candidate)
        except (SyntaxError, ValueError):
            return None
    if not isinstance(payload, dict) or payload.get("type") != "error":
        return None
    provider_error = payload.get("error")
    if not isinstance(provider_error, dict):
        return None
    error_type = provider_error.get("type")
    message = provider_error.get("message")
    if not isinstance(error_type, str) or not isinstance(message, str):
        return None
    return error_type, message


def _has_provider_status(error: Exception, status_code: int) -> bool:
    """Return whether a typed provider exception has the given status."""
    if getattr(error, "status_code", None) != status_code:
        return False
    return isinstance(error, ModelProviderError) or _extract_provider_from_error(error) is not None


def is_context_window_overflow_error(error: Exception | str) -> bool:
    """Recognize deterministic input-context overflows without using status codes."""
    if isinstance(error, ContextWindowExceededError):
        return True
    if not isinstance(error, (ModelProviderError, str)):
        return False

    message = error.message if isinstance(error, ModelProviderError) else error
    normalized = str(message).casefold()
    is_rate_limit = isinstance(error, ModelRateLimitError) or any(
        marker in normalized for marker in _RATE_LIMIT_MARKERS
    )
    is_output_only = any(marker in normalized for marker in _OUTPUT_LIMIT_MARKERS) and not any(
        marker in normalized for marker in _INPUT_MARKERS
    )
    if is_rate_limit or is_output_only:
        return False
    if any(marker in normalized for marker in _CONTEXT_OVERFLOW_MARKERS):
        return True
    if "context window" in normalized and any(marker in normalized for marker in ("exceed", "too long", "too large")):
        return True
    return "input token count" in normalized and "exceed" in normalized and "maximum" in normalized


def _is_transient_provider_error(error: Exception) -> bool:
    """Recognize provider failures that already exhausted automatic retries."""
    status_code = getattr(error, "status_code", None)
    if isinstance(error, ModelProviderError) and status_code in TRANSIENT_PROVIDER_STATUS_CODES:
        return True
    if _extract_provider_from_error(error) is not None and status_code in TRANSIENT_PROVIDER_STATUS_CODES:
        return True

    structured_error = _structured_provider_error(str(error))
    if structured_error is None:
        return False
    error_type, message = structured_error
    error_type = error_type.casefold()
    message = message.casefold()
    return error_type in {"overloaded", "overloaded_error"} or (
        error_type == "api_error" and "internal server error" in message
    )


def get_user_friendly_error_message(error: Exception, agent_name: str | None = None) -> str:
    """Return a user-friendly error message.

    Args:
        error: The exception that occurred
        agent_name: Optional name of the agent that encountered the error

    Returns:
        A user-friendly error message

    """
    error_str = str(error).lower()
    safe_error = redact_sensitive_text(str(error))
    agent_prefix = f"[{agent_name}] " if agent_name else ""

    # Log the full error for debugging
    logger.error(
        "agent_error",
        agent=agent_name or "agent",
        error_type=type(error).__name__,
        error=repr(error),
    )

    # Only distinguish the most important error types
    if any(x in error_str for x in ["401", "auth", "unauthorized", "api key", "api_key", "apikey"]):
        provider = _extract_provider_from_error(error)
        provider_hint = f" ({provider})" if provider else ""
        return f"{agent_prefix}❌ Authentication failed{provider_hint}: {safe_error}"
    if is_context_window_overflow_error(error):
        structured_error = _structured_provider_error(str(error))
        diagnostic = redact_sensitive_text(structured_error[1]) if structured_error is not None else safe_error
        return f"{agent_prefix}⚠️ Context window exceeded: {diagnostic}"
    if any(x in error_str for x in ["rate", "429", "quota"]) or _has_provider_status(error, 429):
        return f"{agent_prefix}⏱️ Rate limited. Please wait a moment and try again."
    if _is_transient_provider_error(error):
        return (
            f"{agent_prefix}⚠️ Model provider temporarily unavailable after automatic retries. Please try again shortly."
        )
    if "timeout" in error_str:
        return f"{agent_prefix}⏰ Request timed out. Please try again."
    # Generic error with the actual error message for transparency
    return f"{agent_prefix}⚠️ Error: {safe_error}"
