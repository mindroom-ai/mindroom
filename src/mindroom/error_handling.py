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
from mindroom.providers.errors import classify_provider_error, is_credential_error, whole_words
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
# Wording of text without a provider payload, read as whole words. A lowercased error name is one word, so only a name
# that starts with ``ratelimit``, ``quotaexceeded``, or ``quotasexceeded`` reads as throttling (not ``RateError`` or
# ``OpenAIRateLimitError``), and only an auth family name such as ``AuthenticationError`` as credentials (not
# ``AuthError`` or ``InvalidApiKeyError``); splitting CamelCase would also read a property ``authToken`` as credentials.
_UNAUTHORIZED_WORDING = whole_words("401")
_RATE_LIMIT_WORDING = whole_words(r"rate(?:limit[a-z]*)?", "429", r"quotas?(?:exceeded[a-z]*)?")
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
    """Return the SDK that raised an untyped error, such as ``openai`` for an error from ``openai._exceptions``."""
    top_module = (type(error).__module__ or "").split(".")[0]
    # TODO(simplify): An untyped httpx error names `httpx` as the provider in the authentication reply. Name no
    # provider for it, since httpx is the HTTP client and not a provider. Users would notice.
    return top_module if top_module in {"openai", "anthropic", "google", "groq", "httpx"} else None


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
    """Return the reply that reports ``error``, prefixed with ``[agent_name]`` when one is given.

    A provider error's type alone decides its message, since the provider's status and wording were read when it
    was classified. An error of another kind that holds a provider's error payload reads as that payload's typed
    error would; only one without a payload is matched by the whole words in its text. ``runtime_paths`` points a
    missing-key reply at the dashboard URL.
    """
    safe_error = redact_sensitive_text(str(error))
    agent_prefix = f"[{agent_name}] " if agent_name else ""

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


def _authentication_failed(provider: str | None, safe_error: str) -> str:
    provider_hint = f" ({provider})" if provider else ""
    return f"❌ Authentication failed{provider_hint}: {safe_error}"


def _provider_error_message(error: ProviderError, safe_error: str, runtime_paths: RuntimePaths | None) -> str:
    if isinstance(error, AuthenticationFailed):
        if error.code == MISSING_API_KEY:
            return _missing_provider_key_message(runtime_paths)
        return _authentication_failed(error.provider, safe_error)
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
    provider = _extract_provider_from_error(error)
    if (payload := _structured_provider_error(error_str)) is not None:
        error_type, message = payload
        typed = classify_provider_error(message, provider=provider or "", error_type=error_type)
        return _provider_error_message(typed, safe_error, runtime_paths)
    if _UNAUTHORIZED_WORDING.search(error_str) or is_credential_error(error_str):
        return _authentication_failed(provider, safe_error)
    if _RATE_LIMIT_WORDING.search(error_str):
        return _RATE_LIMITED
    # No word holds "timeout" by accident, while a name such as ConnectTimeoutError holds it on purpose.
    if "timeout" in error_str:
        return "⏰ Request timed out. Please try again."
    return f"⚠️ Error: {safe_error}"
