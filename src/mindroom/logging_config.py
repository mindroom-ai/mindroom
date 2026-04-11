"""Logging configuration for mindroom using structlog."""

from __future__ import annotations

import logging
import logging.config
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from mindroom.constants import RuntimePaths

__all__ = ["bound_log_context", "get_logger", "setup_logging"]


class _NioValidationFilter(logging.Filter):
    """Filter out harmless nio validation warnings that confuse AI agents."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter out specific nio validation warnings.

        Returns:
            False to suppress the log record, True to keep it

        """
        # Filter out only the specific user_id and room_id validation warnings from nio
        if record.name == "nio.responses":
            msg = record.getMessage()
            if "Error validating response: 'user_id' is a required property" in msg:
                # This warning occurs when Matrix server responses don't include user_id
                # which happens during registration checks. It's harmless.
                return False
            if "Error validating response: 'room_id' is a required property" in msg:
                # Similar harmless warning for room_id
                return False
        return True


def setup_logging(
    *,
    level: str = "INFO",
    runtime_paths: RuntimePaths,
) -> None:
    """Configure structlog for mindroom with file and console output.

    Args:
        level: Minimum logging level (e.g., "DEBUG", "INFO", "WARNING", "ERROR")
        runtime_paths: Explicit runtime context that determines the log directory

    """
    # Create logs directory if it doesn't exist
    logs_dir = runtime_paths.storage_root / "logs"
    logs_dir.mkdir(exist_ok=True, parents=True)

    # Create timestamped log file
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    log_file = logs_dir / f"mindroom_{timestamp}.log"

    # Shared processors that don't affect output format
    timestamper = structlog.processors.TimeStamper(fmt="iso")
    pre_chain = [
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        timestamper,
    ]
    log_format = os.getenv("MINDROOM_LOG_FORMAT", "text").strip().lower()
    renderer_name = "json" if log_format == "json" else "text"

    text_processors = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        structlog.dev.ConsoleRenderer(colors=False),
    ]
    colored_processors = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        structlog.dev.ConsoleRenderer(
            colors=True,
            exception_formatter=structlog.dev.RichTracebackFormatter(
                # The locals can be very large, so we hide them by default
                show_locals=False,
            ),
        ),
    ]
    json_processors = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        structlog.processors.ExceptionRenderer(),
        structlog.processors.JSONRenderer(),
    ]

    # Configure logging with both console and file handlers
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "text": {
                    "()": structlog.stdlib.ProcessorFormatter,
                    "processors": text_processors,
                    "foreign_pre_chain": pre_chain,
                },
                "colored": {
                    "()": structlog.stdlib.ProcessorFormatter,
                    "processors": colored_processors,
                    "foreign_pre_chain": pre_chain,
                },
                "json": {
                    "()": structlog.stdlib.ProcessorFormatter,
                    "processors": json_processors,
                    "foreign_pre_chain": pre_chain,
                },
            },
            "filters": {
                "nio_validation": {  # ty: ignore[missing-typed-dict-key]
                    "()": _NioValidationFilter,  # ty: ignore[invalid-key]
                },
            },
            "handlers": {
                "console": {
                    "level": level.upper(),
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stderr",
                    "formatter": "json" if renderer_name == "json" else "colored",
                    "filters": ["nio_validation"],
                },
                "file": {
                    "level": level.upper(),
                    "class": "logging.FileHandler",
                    "filename": str(log_file),
                    "mode": "a",
                    "encoding": "utf-8",
                    "formatter": "json" if renderer_name == "json" else "text",
                    "filters": ["nio_validation"],
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
    logger.info("Logging initialized", log_file=str(log_file), level=level, log_format=renderer_name)


def get_logger(name: str = __name__) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger instance.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Configured structlog logger

    """
    return structlog.get_logger(name)


def bound_log_context(**context: object) -> AbstractContextManager[None]:
    """Temporarily bind structured log fields for the current async/task scope."""
    return structlog.contextvars.bound_contextvars(**context)
