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


_DEFAULT_LOGGER_LEVELS = {
    # Reduce verbosity of nio (Matrix) library by default.
    "nio": "WARNING",
    "nio.client": "WARNING",
    "nio.responses": "WARNING",
}


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


def _normalize_log_level(level: str) -> str:
    normalized = level.strip().upper()
    if normalized not in logging.getLevelNamesMapping():
        msg = f"Unsupported log level: {level!r}"
        raise ValueError(msg)
    return normalized


def _parse_logger_level_overrides(value: str | None) -> dict[str, str]:
    """Parse `logger:LEVEL` entries from MINDROOM_LOGGER_LEVELS."""
    if value is None or not value.strip():
        return {}

    overrides: dict[str, str] = {}
    for raw_entry in value.replace(";", ",").split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            msg = f"Invalid MINDROOM_LOGGER_LEVELS entry {entry!r}; expected logger:LEVEL"
            raise ValueError(msg)
        logger_name, level = (part.strip() for part in entry.split(":", maxsplit=1))
        if not logger_name:
            msg = f"Invalid MINDROOM_LOGGER_LEVELS entry {entry!r}; logger name is empty"
            raise ValueError(msg)
        overrides[logger_name] = _normalize_log_level(level)
    return overrides


def _build_logger_levels(
    *,
    global_level: str,
    override_config: str | None,
) -> tuple[str, dict[str, dict[str, object]]]:
    """Build logger config and the handler threshold needed to emit it."""
    level_numbers = logging.getLevelNamesMapping()
    root_level = _normalize_log_level(global_level)
    logger_levels = {
        **_DEFAULT_LOGGER_LEVELS,
        **_parse_logger_level_overrides(override_config),
    }
    handler_level = min((root_level, *logger_levels.values()), key=level_numbers.__getitem__)
    loggers: dict[str, dict[str, object]] = {
        "": {  # Root logger
            "handlers": ["console", "file"],
            "level": root_level,
            "propagate": False,
        },
    }
    loggers.update({logger_name: {"level": logger_level} for logger_name, logger_level in logger_levels.items()})
    return handler_level, loggers


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
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        timestamper,
    ]
    log_format = os.getenv("MINDROOM_LOG_FORMAT", "text").strip().lower()
    renderer_name = "json" if log_format == "json" else "text"
    handler_level, loggers = _build_logger_levels(
        global_level=level,
        override_config=os.getenv("MINDROOM_LOGGER_LEVELS"),
    )

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
                "nio_validation": {
                    "()": _NioValidationFilter,
                },
            },
            "handlers": {
                "console": {
                    "level": handler_level,
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stderr",
                    "formatter": "json" if renderer_name == "json" else "colored",
                    "filters": ["nio_validation"],
                },
                "file": {
                    "level": handler_level,
                    "class": "logging.FileHandler",
                    "filename": str(log_file),
                    "mode": "a",
                    "encoding": "utf-8",
                    "formatter": "json" if renderer_name == "json" else "text",
                    "filters": ["nio_validation"],
                },
            },
            "loggers": loggers,
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
