"""Simple error handling for MindRoom agents."""

from __future__ import annotations

import ast
import json
import re
from typing import TYPE_CHECKING

from mindroom.llm.errors import (
    MISSING_API_KEY,
    MODEL_SAFEGUARD_REFUSAL_MESSAGE,
    AuthenticationFailed,
    ContextWindowExceeded,
    ModelSafeguardRefusal,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from mindroom.logging_config import get_logger
from mindroom.providers.errors import classify_provider_error
from mindroom.redaction import redact_sensitive_text

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

# The SDKs' own errors for a client built without any API key, which reach the reply untyped.
_MISSING_PROVIDER_KEY_PATTERN = re.compile(
    r"the api_key client option must be set"  # OpenAI SDK
    r"|missing credentials\. please pass an `api_key`"  # Newer OpenAI SDK
    r"|could not resolve authentication method",  # Anthropic SDK
)
_PROVIDER_UNAVAILABLE = "⚠️ Model provider temporarily unavailable after automatic retries. Please try again shortly."
_RATE_LIMITED = "⏱️ Rate limited. Please wait a moment and try again."


class AvatarGenerationError(RuntimeError):
    """Raised when managed avatar generation cannot produce required assets."""


class AvatarSyncError(RuntimeError):
    """Raised when managed avatar sync cannot complete."""


class MinimalModeUnavailableError(RuntimeError):
    """Minimal mode cannot serve a turn; the message already names its recovery command."""


def minimal_mode_failure_message(reason: str, agent_name: str, *, subagent: bool) -> str:
    """Include how to recover from unavailable minimal mode: the mode command, or a standard subagent."""
    if subagent:
        return f"{reason.rstrip('.')}. Start a new subagent without minimal."
    return f"{reason.rstrip('.')}. Return to standard mode with `!mode {agent_name} standard`."


def is_model_safeguard_refusal(error: Exception | str) -> bool:
    """Recognize the typed refusal and the exact text errored runs preserve."""
    return isinstance(error, ModelSafeguardRefusal) or str(error).strip() == MODEL_SAFEGUARD_REFUSAL_MESSAGE


def _extract_provider_from_error(error: Exception) -> str | None:
    """Try to extract the provider name from the module of an exception that is not an owned provider error."""
    module = type(error).__module__ or ""
    # e.g. "openai" from "openai._exceptions", "anthropic" from "anthropic._exceptions"
    top_module = module.split(".")[0] if module else ""
    known_providers = {"openai", "anthropic", "google", "groq", "httpx"}
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


def _names_transient_failure(error_str: str) -> bool:
    """Return whether an untyped error's text holds a provider payload the provider classifier calls transient."""
    structured_error = _structured_provider_error(error_str)
    if structured_error is None:
        return False
    error_type, message = structured_error
    return isinstance(classify_provider_error(message, provider="unknown", error_type=error_type), ProviderUnavailable)


def _missing_provider_key_message(runtime_paths: RuntimePaths | None) -> str:
    dashboard_url = (runtime_paths.env_value("MINDROOM_PUBLIC_URL") or "").strip() if runtime_paths else ""
    dashboard = f"the MindRoom dashboard ({dashboard_url.rstrip('/')})" if dashboard_url else "the MindRoom dashboard"
    return (
        "🔑 No AI provider key is set up yet, so I can't reply. "
        f"Open {dashboard}, choose **Connect your AI provider**, paste your key, then send your message again."
    )


def get_user_friendly_error_message(
    error: Exception,
    agent_name: str | None = None,
    *,
    runtime_paths: RuntimePaths | None = None,
) -> str:
    """Return a user-friendly error message.

    A provider error's type alone decides its message, since the provider's status and wording were read when it
    was classified; only an error of another kind is matched by the keywords in its text.

    Args:
        error: The exception that occurred
        agent_name: Optional name of the agent that encountered the error
        runtime_paths: Runtime context used to point missing-key errors at the dashboard URL

    Returns:
        A user-friendly error message

    """
    safe_error = redact_sensitive_text(str(error))
    agent_prefix = f"[{agent_name}] " if agent_name else ""

    # Log the full error for debugging
    logger.error(
        "agent_error",
        agent=agent_name or "agent",
        error_type=type(error).__name__,
        error=repr(error),
    )

    if isinstance(error, MinimalModeUnavailableError):
        # Keyword classification would mask the standard-mode recovery command.
        return f"{agent_prefix}⚠️ Error: {safe_error}"
    if is_model_safeguard_refusal(error):
        return (
            f"{agent_prefix}⚠️ This model's safeguards blocked the request. "
            "Choose a different model (`!model list`) or revise the prompt, then try again."
        )
    if isinstance(error, ProviderError):
        return f"{agent_prefix}{_provider_error_message(error, safe_error, runtime_paths)}"
    return f"{agent_prefix}{_untyped_error_message(error, safe_error, runtime_paths)}"


def _provider_error_message(error: ProviderError, safe_error: str, runtime_paths: RuntimePaths | None) -> str:
    if isinstance(error, AuthenticationFailed):
        if error.code == MISSING_API_KEY:
            return _missing_provider_key_message(runtime_paths)
        return f"❌ Authentication failed ({error.provider}): {safe_error}"
    if isinstance(error, ContextWindowExceeded):
        return (
            "⚠️ The conversation no longer fits the model's context window. "
            "Start a new thread or compact the conversation, then try again."
        )
    if isinstance(error, RateLimited):
        return _RATE_LIMITED
    if isinstance(error, ProviderUnavailable):
        return _PROVIDER_UNAVAILABLE
    return f"⚠️ Error: {safe_error}"


def _untyped_error_message(error: Exception, safe_error: str, runtime_paths: RuntimePaths | None) -> str:
    error_str = str(error).lower()
    if _MISSING_PROVIDER_KEY_PATTERN.search(error_str):
        return _missing_provider_key_message(runtime_paths)
    # Only distinguish the most important error types
    if any(x in error_str for x in ["401", "auth", "unauthorized", "api key", "api_key", "apikey"]):
        provider = _extract_provider_from_error(error)
        provider_hint = f" ({provider})" if provider else ""
        return f"❌ Authentication failed{provider_hint}: {safe_error}"
    if any(x in error_str for x in ["rate", "429", "quota"]):
        return _RATE_LIMITED
    if _names_transient_failure(error_str):
        return _PROVIDER_UNAVAILABLE
    if "timeout" in error_str:
        return "⏰ Request timed out. Please try again."
    # Generic error with the actual error message for transparency
    return f"⚠️ Error: {safe_error}"
