"""Logging configuration for mindroom using structlog."""

from __future__ import annotations

import hashlib
import logging
import sys

import structlog

__all__ = ["setup_logging", "emoji", "get_logger"]


def emoji(agent_name: str) -> str:
    """Get an emoji-prefixed agent name string with consistent emoji based on the name.

    Args:
        agent_name: The agent name to add emoji to

    Returns:
        The agent name with a unique emoji prefix
    """
    # Emojis for different agents
    emojis = [
        "🤖",  # robot
        "🧮",  # abacus
        "💡",  # light bulb
        "🔧",  # wrench
        "📊",  # chart
        "🎯",  # target
        "🚀",  # rocket
        "⚡",  # lightning
        "🔍",  # magnifying glass
        "📝",  # memo
        "🎨",  # artist palette
        "🧪",  # test tube
        "🎪",  # circus tent
        "🌟",  # star
        "🔮",  # crystal ball
        "🛠️",  # hammer and wrench
    ]

    # Use hash to get consistent emoji for each agent
    hash_value = int(hashlib.md5(agent_name.encode()).hexdigest(), 16)
    emoji_index = hash_value % len(emojis)
    emoji = emojis[emoji_index]

    return f"{emoji} {agent_name}"


def setup_logging(level: str = "INFO") -> None:
    """
    Configure structlog for mindroom.

    Args:
        level: Minimum logging level (e.g., "DEBUG", "INFO", "WARNING", "ERROR")
    """
    # Configure structlog with built-in console renderer
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.dev.ConsoleRenderer(colors=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper(), logging.INFO)),
        logger_factory=structlog.WriteLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure standard logging
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
        stream=sys.stderr,
    )


def get_logger(name: str = __name__) -> structlog.BoundLogger:
    """Get a structlog logger instance.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Configured structlog logger
    """
    return structlog.get_logger(name)
