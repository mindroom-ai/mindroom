"""Logging configuration for mindroom using structlog."""

from __future__ import annotations

import logging
import logging.config
import os
import sys
from datetime import UTC, datetime
from io import StringIO
from typing import TYPE_CHECKING, cast

import structlog

from mindroom.redaction import redact_log_event, redact_sensitive_text

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from pathlib import Path
    from typing import TextIO

    from structlog.typing import ExceptionRenderer, ExcInfo, Processor

    from mindroom.constants import RuntimePaths

__all__ = [
    "bound_log_context",
    "configure_default_logging",
    "get_logger",
    "setup_logging",
    "setup_subprocess_logging",
    "subprocess_logging_env",
    "uses_default_logging",
]

_LOG_LEVEL_ENV = "LOG_LEVEL"
_LOG_FORMAT_ENV = "MINDROOM_LOG_FORMAT"
_LOGGER_LEVELS_ENV = "MINDROOM_LOGGER_LEVELS"


_DEFAULT_LOGGER_LEVELS = {
    # Reduce verbosity of nio (Matrix) library by default.
    "nio": "WARNING",
    "nio.client": "WARNING",
    # Matrix crypto decrypt warnings can arrive in bursts when sessions are
    # missing. Keep them available through MINDROOM_LOGGER_LEVELS when needed.
    "nio.crypto": "ERROR",
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


class _RedactingExceptionFormatter:
    """Render exceptions normally, then redact credential-bearing text."""

    def __init__(self, formatter: ExceptionRenderer) -> None:
        self._formatter = formatter

    def __call__(self, sio: TextIO, exc_info: ExcInfo) -> None:
        buffer = StringIO()
        self._formatter(buffer, exc_info)
        sio.write(redact_sensitive_text(buffer.getvalue()))


_MISSING = object()


def _redact_log_event_preserving_exc_info(
    logger: object,
    method_name: str,
    event_dict: dict[str, object],
) -> dict[str, object]:
    """Redact event fields without replacing raw exc_info before console rendering."""
    exc_info = event_dict.get("exc_info", _MISSING)
    if exc_info is _MISSING:
        return redact_log_event(logger, method_name, event_dict)

    redacted_input = dict(event_dict)
    redacted_input.pop("exc_info")
    redacted = redact_log_event(logger, method_name, redacted_input)
    redacted["exc_info"] = exc_info
    return redacted


# structlog's builtin console renderer prints Rich tracebacks with every frame's
# locals: resolved config, credentials, subprocess environments. Processes that
# never call `setup_logging`, such as subprocess entrypoints and the sandbox
# runner, log through these processors instead.
_DEFAULT_PROCESSORS: list[Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.StackInfoRenderer(),
    structlog.dev.set_exc_info,
    structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
    cast("Processor", _redact_log_event_preserving_exc_info),
    structlog.dev.ConsoleRenderer(
        colors=False,
        exception_formatter=_RedactingExceptionFormatter(structlog.dev.plain_traceback),
    ),
]


def configure_default_logging() -> None:
    """Restore the logging a process uses until it calls `setup_logging`."""
    structlog.reset_defaults()
    structlog.configure(processors=_DEFAULT_PROCESSORS)


def uses_default_logging() -> bool:
    """Return whether the processors are still the ones `configure_default_logging` installed."""
    return structlog.get_config()["processors"] is _DEFAULT_PROCESSORS


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
    handler_names: list[str],
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
            "handlers": handler_names,
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

    renderer_name = _configure_logging(level=level, log_file=log_file)

    # Log startup message
    logger = get_logger(__name__)
    logger.info("Logging initialized", log_file=str(log_file), level=level, log_format=renderer_name)


def subprocess_logging_env() -> dict[str, str]:
    """Return the environment that makes `setup_subprocess_logging` in a child log like this process."""
    # The root level also reflects `mindroom run --log-level`, which never reaches the environment.
    return {
        _LOG_LEVEL_ENV: logging.getLevelName(logging.getLogger().level),
        _LOG_FORMAT_ENV: os.getenv(_LOG_FORMAT_ENV, ""),
        _LOGGER_LEVELS_ENV: os.getenv(_LOGGER_LEVELS_ENV, ""),
    }


def setup_subprocess_logging() -> None:
    """Configure a child process to log to stderr like the parent that built its `subprocess_logging_env`.

    The child writes no log file of its own, so each run does not leave a new file behind.
    """
    _configure_logging(level=os.getenv(_LOG_LEVEL_ENV, "INFO"), log_file=None)


def _configure_logging(*, level: str, log_file: Path | None) -> str:
    """Route structlog, stdlib, and Agno records to stderr and an optional file; return the renderer name."""
    # Shared processors that don't affect output format
    timestamper = structlog.processors.TimeStamper(fmt="iso")
    pre_chain = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.ExtraAdder(),
        timestamper,
        _redact_log_event_preserving_exc_info,
    ]
    log_format = os.getenv(_LOG_FORMAT_ENV, "text").strip().lower()
    use_colors = sys.stderr.isatty() and not os.getenv("NO_COLOR")
    renderer_name = "json" if log_format == "json" else ("colored" if use_colors else "text")
    handler_level, loggers = _build_logger_levels(
        global_level=level,
        override_config=os.getenv(_LOGGER_LEVELS_ENV),
        handler_names=["console", "file"] if log_file is not None else ["console"],
    )

    text_processors = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        _redact_log_event_preserving_exc_info,
        structlog.dev.ConsoleRenderer(
            colors=False,
            exception_formatter=_RedactingExceptionFormatter(structlog.dev.plain_traceback),
        ),
    ]
    colored_processors = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        _redact_log_event_preserving_exc_info,
        structlog.dev.ConsoleRenderer(
            colors=True,
            exception_formatter=_RedactingExceptionFormatter(
                structlog.dev.RichTracebackFormatter(
                    # The locals can be very large, so we hide them by default
                    show_locals=False,
                ),
            ),
        ),
    ]
    json_processors = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        structlog.processors.ExceptionRenderer(),
        redact_log_event,
        structlog.processors.JSONRenderer(),
    ]

    handlers: dict[str, dict[str, object]] = {
        "console": {
            "level": handler_level,
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "formatter": renderer_name,
            "filters": ["nio_validation"],
        },
    }
    if log_file is not None:
        handlers["file"] = {
            "level": handler_level,
            "class": "logging.FileHandler",
            "filename": str(log_file),
            "mode": "a",
            "encoding": "utf-8",
            "formatter": "json" if renderer_name == "json" else "text",
            "filters": ["nio_validation"],
        }

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
            "handlers": handlers,
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
    _route_agno_loggers_to_root()
    return renderer_name


def _route_agno_loggers_to_root() -> None:
    """Send Agno records through the configured handlers, so they share their format, level, and stream."""
    # AGNO_COMPAT: Agno library loggers bypass the host's logging configuration.
    # Reason: Importing agno.utils.log gives the agno, agno-team, and agno-workflow loggers their own
    # stdout handler at INFO with propagation disabled, so their records ignore the configured level,
    # skip the JSON renderer and redaction, and leave the knowledge refresh child as plain stderr lines.
    # Upstream issue: https://github.com/agno-agi/agno/issues/4097, closed after Agno 2.0 added
    # `configure_agno_logging`, which swaps module globals but leaves the default handlers on the
    # loggers that modules imported directly.
    # Upstream PR: none identified.
    # Remove when: Agno's library loggers propagate to the host's handlers without installing their own.
    # Coverage: tests/test_logging_config.py::test_agno_records_follow_configured_format_and_level;
    # tests/test_logging_config.py::test_subprocess_logging_matches_parent_format_and_level.
    from agno.utils import log as agno_log  # noqa: PLC0415 - keeps agno out of the config layer's imports.

    for agno_logger in (agno_log.agent_logger, agno_log.team_logger, agno_log.workflow_logger):
        for handler in list(agno_logger.handlers):
            agno_logger.removeHandler(handler)
        agno_logger.propagate = True


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


if not structlog.is_configured():
    configure_default_logging()
