"""Simple error handling for MindRoom agents."""

from __future__ import annotations

import ast
import json

from agno.exceptions import ModelProviderError

from mindroom.logging_config import get_logger
from mindroom.redaction import redact_sensitive_text

logger = get_logger(__name__)

# Shared by provider retry policy and final user-message routing. Status 200 is
# the Claude mid-stream SSE error case: the HTTP response was already committed
# before the provider emitted an error event.
TRANSIENT_PROVIDER_STATUS_CODES = frozenset({200, 408, 409, 429, 500, 502, 503, 504, 529})
MODEL_SAFEGUARD_REFUSAL_MESSAGE = "Vertex Claude returned stop_reason=refusal"


class AvatarGenerationError(RuntimeError):
    """Raised when managed avatar generation cannot produce required assets."""


class AvatarSyncError(RuntimeError):
    """Raised when managed avatar sync cannot complete."""


class ModelSafeguardRefusalError(ModelProviderError):
    """Raised when a provider explicitly stops generation for safeguards."""


def is_model_safeguard_refusal(error: Exception | str) -> bool:
    """Recognize typed refusals and the exact text Agno preserves in errored runs."""
    return isinstance(error, ModelSafeguardRefusalError) or str(error).strip() == MODEL_SAFEGUARD_REFUSAL_MESSAGE


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
    return error_type.casefold(), message.casefold()


def _has_provider_status(error: Exception, status_code: int) -> bool:
    """Return whether a typed provider exception has the given status."""
    if getattr(error, "status_code", None) != status_code:
        return False
    return isinstance(error, ModelProviderError) or _extract_provider_from_error(error) is not None


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

    if is_model_safeguard_refusal(error):
        return (
            f"{agent_prefix}⚠️ This model's safeguards blocked the request. "
            "Choose a different model (`!model list`) or revise the prompt, then try again."
        )

    # Only distinguish the most important error types
    if any(x in error_str for x in ["401", "auth", "unauthorized", "api key", "api_key", "apikey"]):
        provider = _extract_provider_from_error(error)
        provider_hint = f" ({provider})" if provider else ""
        return f"{agent_prefix}❌ Authentication failed{provider_hint}: {safe_error}"
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
