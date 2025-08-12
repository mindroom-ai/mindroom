"""Logging configuration for mindroom using structlog."""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

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

    # Open file for writing logs
    file_handle = log_file.open("a", encoding="utf-8")

    # Simple solution: Create a custom writer that strips ANSI codes for file output
    class DualWriter:
        def __init__(self, file: TextIO) -> None:
            self.file = file
            self.stderr = sys.stderr
            # Regex to strip ANSI color codes
            self.ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

        def write(self, message: str) -> None:
            # Write to console with colors
            self.stderr.write(message)
            # Write to file without colors
            clean_message = self.ansi_escape.sub("", message)
            self.file.write(clean_message)
            self.file.flush()  # Ensure logs are written immediately

        def flush(self) -> None:
            self.stderr.flush()
            self.file.flush()

    dual_writer = DualWriter(file_handle)

    # Configure structlog with our dual writer
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.dev.ConsoleRenderer(colors=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper(), logging.INFO)),
        logger_factory=structlog.WriteLoggerFactory(file=dual_writer),  # type: ignore[arg-type]
        cache_logger_on_first_use=True,
    )

    # Configure standard logging
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
        stream=sys.stderr,
    )

    # Reduce verbosity of nio (Matrix) library
    logging.getLogger("nio").setLevel(logging.WARNING)
    logging.getLogger("nio.client").setLevel(logging.WARNING)
    logging.getLogger("nio.responses").setLevel(logging.WARNING)

    # Log startup message
    logger = get_logger(__name__)
    logger.info("Logging initialized", log_file=str(log_file), level=level)


def get_logger(name: str = __name__) -> structlog.BoundLogger:
    """Get a structlog logger instance.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Configured structlog logger

    """
    return structlog.get_logger(name)  # type: ignore[no-any-return]
