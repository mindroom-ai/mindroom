"""Centralized error handling for MindRoom agents.

This module provides unified error handling and user notification for various failure scenarios.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import TYPE_CHECKING, Self

from .logging_config import get_logger

if TYPE_CHECKING:
    from nio import AsyncClient


logger = get_logger(__name__)


class ErrorCategory(Enum):
    """Categories of errors for appropriate user messaging."""

    API_KEY = "api_key"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    TIMEOUT = "timeout"
    PERMISSION = "permission"
    CONFIGURATION = "configuration"
    TOOL_FAILURE = "tool_failure"
    MEMORY = "memory"
    UNKNOWN = "unknown"


def categorize_error(error: Exception) -> ErrorCategory:  # noqa: PLR0911
    """Categorize an exception for appropriate user messaging.

    Args:
        error: The exception to categorize

    Returns:
        The error category

    """
    error_str = str(error).lower()
    error_type = type(error).__name__.lower()

    # API key related errors
    if any(
        keyword in error_str
        for keyword in ["api key", "api_key", "unauthorized", "401", "invalid key", "authentication"]
    ):
        return ErrorCategory.API_KEY

    # Rate limiting
    if any(keyword in error_str for keyword in ["rate limit", "429", "too many requests", "quota"]):
        return ErrorCategory.RATE_LIMIT

    # Network errors
    if any(keyword in error_str for keyword in ["connection", "network", "unreachable", "dns", "ssl", "certificate"]):
        return ErrorCategory.NETWORK

    # Timeout errors
    if "timeout" in error_str or "timeout" in error_type:
        return ErrorCategory.TIMEOUT

    # Permission errors
    if any(keyword in error_str for keyword in ["permission", "403", "forbidden", "access denied"]):
        return ErrorCategory.PERMISSION

    # Configuration errors (be more specific to avoid false positives)
    if any(keyword in error_str for keyword in ["config", "configuration", "missing configuration", "not found"]):
        return ErrorCategory.CONFIGURATION

    # Tool execution failures
    if any(keyword in error_str for keyword in ["tool", "function", "execution"]):
        return ErrorCategory.TOOL_FAILURE

    # Memory errors
    if any(keyword in error_str for keyword in ["memory", "out of memory", "oom"]):
        return ErrorCategory.MEMORY

    return ErrorCategory.UNKNOWN


def get_user_friendly_error_message(error: Exception, agent_name: str | None = None) -> str:  # noqa: PLR0911
    """Generate a user-friendly error message based on the error category.

    Args:
        error: The exception that occurred
        agent_name: Optional name of the agent that encountered the error

    Returns:
        A user-friendly error message

    """
    category = categorize_error(error)
    agent_prefix = f"[{agent_name}] " if agent_name else ""

    if category == ErrorCategory.API_KEY:
        provider = _extract_provider_from_error(error)
        provider_msg = f" for {provider}" if provider else ""
        return (
            f"{agent_prefix}❌ **Authentication Error**: I couldn't authenticate with the AI service{provider_msg}. "
            f"Please check that the API key is correctly configured. "
            f"You can update it through the widget or configuration file."
        )

    if category == ErrorCategory.RATE_LIMIT:
        return (
            f"{agent_prefix}⏱️ **Rate Limit**: The AI service has rate-limited our requests. "
            f"Please wait a moment before trying again, or consider upgrading your API plan."
        )

    if category == ErrorCategory.NETWORK:
        return (
            f"{agent_prefix}🌐 **Network Error**: I couldn't connect to the AI service. "
            f"This might be a temporary network issue. Please try again in a moment."
        )

    if category == ErrorCategory.TIMEOUT:
        return (
            f"{agent_prefix}⏰ **Timeout**: The request took too long to complete. "
            f"The AI service might be experiencing high load. Please try again."
        )

    if category == ErrorCategory.PERMISSION:
        return (
            f"{agent_prefix}🔒 **Permission Denied**: I don't have permission to access this resource. "
            f"Please check your account permissions and try again."
        )

    if category == ErrorCategory.CONFIGURATION:
        return (
            f"{agent_prefix}⚙️ **Configuration Error**: There's an issue with my configuration. "
            f"Please check the settings and ensure all required fields are properly configured."
        )

    if category == ErrorCategory.TOOL_FAILURE:
        tool_name = _extract_tool_name_from_error(error)
        tool_msg = f" ({tool_name})" if tool_name else ""
        return (
            f"{agent_prefix}🔧 **Tool Error**: A tool{tool_msg} encountered an error during execution. "
            f"I'll try to continue without it, but some functionality might be limited."
        )

    if category == ErrorCategory.MEMORY:
        return (
            f"{agent_prefix}💾 **Memory Error**: I'm running low on memory. "
            f"Try simplifying your request or breaking it into smaller parts."
        )

    # For unknown errors, provide a generic message but log the details
    logger.error(f"Uncategorized error: {error}")
    return (
        f"{agent_prefix}⚠️ **Unexpected Error**: I encountered an unexpected issue. "
        f"The error has been logged and will be investigated. Please try again or rephrase your request."
    )


def _extract_provider_from_error(error: Exception) -> str | None:
    """Try to extract the AI provider name from the error message."""
    error_str = str(error).lower()

    providers = {
        "openai": "OpenAI",
        "anthropic": "Anthropic",
        "claude": "Anthropic",
        "gemini": "Google Gemini",
        "google": "Google",
        "ollama": "Ollama",
        "cerebras": "Cerebras",
        "openrouter": "OpenRouter",
    }

    for key, name in providers.items():
        if key in error_str:
            return name

    return None


def _extract_tool_name_from_error(error: Exception) -> str | None:
    """Try to extract the tool name from the error message."""
    error_str = str(error)

    # Look for common patterns like "Tool 'name'" or "tool_name"

    patterns = [
        r"Tool ['\"]([^'\"]+)['\"]",
        r"tool[_\s]+([a-zA-Z0-9_]+)",
        r"function ['\"]([^'\"]+)['\"]",
    ]

    for pattern in patterns:
        match = re.search(pattern, error_str, re.IGNORECASE)
        if match:
            return match.group(1)

    return None


async def send_error_message(
    client: AsyncClient,
    room_id: str,
    error: Exception,
    agent_name: str | None = None,
    reply_to_event_id: str | None = None,
    thread_id: str | None = None,
    sender_domain: str | None = None,
) -> str | None:
    """Send a user-friendly error message to a Matrix room.

    Args:
        client: Matrix client for sending messages
        room_id: Room to send the message to
        error: The exception that occurred
        agent_name: Optional agent name for context
        reply_to_event_id: Optional event to reply to
        thread_id: Optional thread ID if in a thread
        sender_domain: Optional sender domain for mentions

    Returns:
        Event ID if message was sent successfully, None otherwise

    """
    from .matrix.client import get_latest_thread_event_id_if_needed, send_message  # noqa: PLC0415
    from .matrix.mentions import format_message_with_mentions  # noqa: PLC0415

    # Generate user-friendly error message
    error_message = get_user_friendly_error_message(error, agent_name)

    # Log the full error for debugging
    logger.error(
        f"Error occurred in {agent_name or 'agent'}: {error}",
        extra={
            "agent": agent_name,
            "room_id": room_id,
            "thread_id": thread_id,
            "error_type": type(error).__name__,
            "error_category": categorize_error(error).value,
        },
    )

    # Ensure we have a thread_id if replying
    effective_thread_id = thread_id or reply_to_event_id

    # Get the latest message in thread for MSC3440 fallback compatibility
    latest_thread_event_id = None
    if effective_thread_id:
        latest_thread_event_id = await get_latest_thread_event_id_if_needed(
            client,
            room_id,
            effective_thread_id,
            reply_to_event_id,
        )

    # Format the message with proper thread structure
    from .config import Config  # noqa: PLC0415

    config = Config()  # Load default config for formatting

    content = format_message_with_mentions(
        config,
        error_message,
        sender_domain=sender_domain or "",
        thread_event_id=effective_thread_id,
        reply_to_event_id=reply_to_event_id,
        latest_thread_event_id=latest_thread_event_id,
    )

    # Mark as an error message for potential special handling
    content["com.mindroom.error_message"] = True
    content["com.mindroom.error_category"] = categorize_error(error).value

    # Send the error message
    event_id = await send_message(client, room_id, content)

    if event_id:
        logger.info(f"Sent error message to room {room_id}: {event_id}")
    else:
        logger.error(f"Failed to send error message to room {room_id}")

    return event_id


class ErrorHandlingContext:
    """Context manager for handling errors in agent operations."""

    def __init__(
        self,
        client: AsyncClient | None = None,
        room_id: str | None = None,
        agent_name: str | None = None,
        reply_to_event_id: str | None = None,
        thread_id: str | None = None,
        sender_domain: str | None = None,
        fallback_message: str | None = None,
    ) -> None:
        """Initialize error handling context.

        Args:
            client: Matrix client for sending error messages
            room_id: Room to send error messages to
            agent_name: Name of the agent for context
            reply_to_event_id: Event to reply to with errors
            thread_id: Thread ID if in a thread
            sender_domain: Sender domain for mentions
            fallback_message: Optional fallback message if error notification fails

        """
        self.client = client
        self.room_id = room_id
        self.agent_name = agent_name
        self.reply_to_event_id = reply_to_event_id
        self.thread_id = thread_id
        self.sender_domain = sender_domain
        self.fallback_message = fallback_message

    async def __aenter__(self) -> Self:
        """Enter the error handling context."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> bool:
        """Exit the error handling context and handle any exceptions."""
        if exc_val is not None and self.client and self.room_id and isinstance(exc_val, Exception):
            # Send error message to user
            await send_error_message(
                self.client,
                self.room_id,
                exc_val,
                self.agent_name,
                self.reply_to_event_id,
                self.thread_id,
                self.sender_domain,
            )
            # Suppress the exception since we've handled it
            return True
        return False
