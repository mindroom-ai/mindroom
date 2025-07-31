"""Logging configuration for mindroom using loguru."""

from __future__ import annotations

import hashlib
import logging
import sys
from types import FrameType
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from loguru import Logger

__all__ = ["setup_logging", "colorize"]


def colorize(agent_name: str) -> str:
    """Get a colorized agent name string with consistent color based on the name.

    Args:
        agent_name: The agent name to colorize

    Returns:
        The agent name wrapped in color tags, e.g. "<cyan>[agent_name]</cyan>"
    """
    # List of available colors that work well in terminals
    colors = [
        "cyan",
        "magenta",
        "green",
        "yellow",
        "blue",
        "red",
        "light-cyan",
        "light-magenta",
        "light-green",
        "light-yellow",
        "light-blue",
        "light-red",
    ]

    # Use hash to get consistent color for each agent
    hash_value = int(hashlib.md5(agent_name.encode()).hexdigest(), 16)
    color_index = hash_value % len(colors)
    color = colors[color_index]

    return f"<{color}>[{agent_name}]</{color}>"


class InterceptHandler(logging.Handler):
    """Handler to intercept standard logging and redirect to loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        """Emit a log record to loguru.

        Args:
            record: The LogRecord instance containing log information
        """
        # Get corresponding Loguru level if it exists
        level: str | int
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message
        frame: FrameType | None = sys._getframe(6)
        depth: int = 6
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging(level: str = "INFO", colorize: bool = True) -> Logger:
    """
    Configure loguru for mindroom and intercept standard logging.

    Args:
        level: Minimum logging level (e.g., "DEBUG", "INFO", "WARNING", "ERROR")
        colorize: Whether to use colors in output

    Returns:
        The configured loguru logger instance
    """
    # Remove default loguru handler
    logger.remove()

    # Add new handler with custom format
    logger.add(
        sys.stderr,
        colorize=colorize,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - {message}",
        level=level,
    )

    # Intercept standard logging
    logging.root.handlers = []
    logging.root.setLevel(level)
    logging.root.addHandler(InterceptHandler())

    # Propagate all loggers to root
    for name in logging.root.manager.loggerDict:
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True

    # Optional: Set specific levels for noisy libraries
    # logging.getLogger("urllib3").setLevel(logging.WARNING)
    # logging.getLogger("asyncio").setLevel(logging.WARNING)

    return logger
