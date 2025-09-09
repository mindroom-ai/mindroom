"""Logging configuration for mindroom using structlog."""

from __future__ import annotations

import hashlib
import logging
import logging.config
from datetime import UTC, datetime
from pathlib import Path

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

    # Shared processors that don't affect output format
    timestamper = structlog.processors.TimeStamper(fmt="iso")
    pre_chain = [
        structlog.stdlib.add_log_level,
        timestamper,
    ]

    # Configure logging with both console and file handlers
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "plain": {
                    "()": structlog.stdlib.ProcessorFormatter,
                    "processors": [
                        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                        structlog.dev.ConsoleRenderer(colors=False),
                    ],
                    "foreign_pre_chain": pre_chain,
                },
                "colored": {
                    "()": structlog.stdlib.ProcessorFormatter,
                    "processors": [
                        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                        structlog.dev.ConsoleRenderer(
                            colors=True,
                            exception_formatter=structlog.dev.RichTracebackFormatter(
                                # The locals can be very large, so we hide them by default
                                show_locals=False,
                            ),
                        ),
                    ],
                    "foreign_pre_chain": pre_chain,
                },
            },
            "handlers": {
                "console": {
                    "level": level.upper(),
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stderr",
                    "formatter": "colored",
                },
                "file": {
                    "level": level.upper(),
                    "class": "logging.FileHandler",
                    "filename": str(log_file),
                    "mode": "a",
                    "encoding": "utf-8",
                    "formatter": "plain",
                },
            },
            "loggers": {
                "": {  # Root logger
                    "handlers": ["console", "file"],
                    "level": level.upper(),
                    "propagate": False,
                },
                # Reduce verbosity of nio (Matrix) library
                "nio": {
                    "level": "WARNING",
                },
                "nio.client": {
                    "level": "WARNING",
                },
                "nio.responses": {
                    "level": "WARNING",
                },
            },
        },
    )

    # Configure structlog to use stdlib logging
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            timestamper,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.UnicodeDecoder(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

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
