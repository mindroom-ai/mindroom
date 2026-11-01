"""Simple error handling for MindRoom agents."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.failure_text import failure_text
from mindroom.llm.errors import (
    MISSING_API_KEY,
    TOKENS_PER_MINUTE,
    AuthenticationFailed,
    ContextWindowExceeded,
    ModelSafeguardRefusal,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from mindroom.logging_config import get_logger
from mindroom.origins import configured_public_url
from mindroom.redaction import redact_sensitive_text

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

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


def _missing_provider_key_message(runtime_paths: RuntimePaths | None) -> str:
    dashboard_url = configured_public_url(runtime_paths) if runtime_paths else None
    dashboard = f"the MindRoom dashboard ({dashboard_url})" if dashboard_url else "the MindRoom dashboard"
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

    An error's type alone decides its message: a provider error was classified from the provider's status and wording
    where its adapter raised it, and any other error shows its own text, except that a timeout says so.
    ``runtime_paths`` points a missing-key reply at the dashboard URL.
    """
    safe_error = redact_sensitive_text(failure_text(error))
    agent_prefix = f"[{agent_name}] " if agent_name else ""

    logger.error(
        "agent_error",
        agent=agent_name or "agent",
        error_type=type(error).__name__,
        error=repr(error),
    )

    if isinstance(error, ModelSafeguardRefusal):
        return (
            f"{agent_prefix}⚠️ This model's safeguards blocked the request. "
            "Choose a different model (`!model list`) or revise the prompt, then try again."
        )
    if needs_provider_key(error):
        return f"{agent_prefix}{_missing_provider_key_message(runtime_paths)}"
    if isinstance(error, ProviderError):
        return f"{agent_prefix}{_provider_error_message(error, safe_error)}"
    if isinstance(error, TimeoutError):
        return f"{agent_prefix}⏰ Request timed out. Please try again."
    return f"{agent_prefix}⚠️ Error: {safe_error}"


def needs_provider_key(error: Exception) -> bool:
    """Return whether ``error`` means no AI provider key is set up, which its reply tells the user how to fix.

    That reply is setup guidance the user reads, unlike the other failure replies, which report an error. Only the
    typed missing-key error an adapter raises at its boundary means it.
    """
    return isinstance(error, AuthenticationFailed) and error.code == MISSING_API_KEY


def _provider_error_message(error: ProviderError, safe_error: str) -> str:
    if isinstance(error, AuthenticationFailed):
        provider_hint = f" ({error.provider})" if error.provider else ""
        return f"❌ Authentication failed{provider_hint}: {safe_error}"
    if isinstance(error, ContextWindowExceeded) and error.code == TOKENS_PER_MINUTE:
        return (
            "⚠️ This request is larger than the account's tokens-per-minute limit. "
            "Start a new thread or compact the conversation, then try again."
        )
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
