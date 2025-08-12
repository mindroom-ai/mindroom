"""Logging configuration for mindroom using structlog."""

from __future__ import annotations

import hashlib
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

__all__ = ["emoji", "get_logger", "setup_logging"]


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
    hash_value = int(hashlib.sha256(agent_name.encode()).hexdigest(), 16)
    emoji_index = hash_value % len(emojis)
    emoji = emojis[emoji_index]

    return f"{emoji} {agent_name}"


def setup_logging(level: str = "INFO") -> None:
    """Configure structlog for mindroom with file and console output.

    Args:
        level: Minimum logging level (e.g., "DEBUG", "INFO", "WARNING", "ERROR")

    """
    # Create logs directory if it doesn't exist
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    # Create timestamped log file
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    log_file = logs_dir / f"mindroom_{timestamp}.log"

    # Create file handler for standard logging
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Configure standard logging for both console and file
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=[
            logging.StreamHandler(sys.stderr),
            file_handler,
        ],
        force=True,  # Force reconfiguration if already configured
    )

    # Configure structlog processors
    timestamper = structlog.processors.TimeStamper(fmt="iso")

    # Shared processors
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
    ]

    # Configure structlog to use stdlib logging
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Set up formatters for the handlers
    # Console formatter with colors
    console_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=True),
        ],
    )

    # File formatter without colors
    file_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
    )

    # Apply formatters to handlers
    for handler in logging.root.handlers:
        if isinstance(handler, logging.StreamHandler) and handler.stream == sys.stderr:
            handler.setFormatter(console_formatter)
        elif isinstance(handler, logging.FileHandler):
            handler.setFormatter(file_formatter)

    # Reduce verbosity of nio (Matrix) library
    logging.getLogger("nio").setLevel(logging.WARNING)
    logging.getLogger("nio.client").setLevel(logging.WARNING)
    logging.getLogger("nio.responses").setLevel(logging.WARNING)

    # Log startup message
    logger = get_logger(__name__)
    logger.info("Logging initialized", log_file=str(log_file), level=level)


def get_logger(name: str = __name__) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger instance.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Configured structlog logger

    """
    return structlog.get_logger(name)  # type: ignore[no-any-return]
