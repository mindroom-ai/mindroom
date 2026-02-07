"""Simple error handling for MindRoom agents."""

from __future__ import annotations

from .logging_config import get_logger

logger = get_logger(__name__)


def _extract_provider_from_error(error: Exception) -> str | None:
    """Try to extract the provider name from the exception's module."""
    module = type(error).__module__ or ""
    # e.g. "openai" from "openai._exceptions", "anthropic" from "anthropic._exceptions"
    top_module = module.split(".")[0] if module else ""
    known_providers = {"openai", "anthropic", "google", "groq", "cerebras", "httpx"}
    if top_module in known_providers:
        return top_module
    return None


def get_user_friendly_error_message(error: Exception, agent_name: str | None = None) -> str:
    """Return a user-friendly error message.

    Args:
        error: The exception that occurred
        agent_name: Optional name of the agent that encountered the error

    Returns:
        A user-friendly error message

    """
    error_str = str(error).lower()
    agent_prefix = f"[{agent_name}] " if agent_name else ""

    # Log the full error for debugging
    logger.error(f"Error in {agent_name or 'agent'}: {error!r}")

    # Only distinguish the most important error types
    if any(x in error_str for x in ["401", "auth", "unauthorized", "api key", "api_key", "apikey"]):
        provider = _extract_provider_from_error(error)
        provider_hint = f" ({provider})" if provider else ""
        return f"{agent_prefix}❌ Authentication failed{provider_hint}: {error}"
    if any(x in error_str for x in ["rate", "429", "quota"]):
        return f"{agent_prefix}⏱️ Rate limited. Please wait a moment and try again."
    if "timeout" in error_str:
        return f"{agent_prefix}⏰ Request timed out. Please try again."
    # Generic error with the actual error message for transparency
    return f"{agent_prefix}⚠️ Error: {error!s}"
