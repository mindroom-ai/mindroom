"""Logging configuration for mindroom using structlog."""

from __future__ import annotations

import atexit
import contextvars
import logging
import logging.config
import logging.handlers
import os
import queue
import sys
import traceback
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, cast

import structlog

from mindroom.redaction import redact_log_event, redact_sensitive_text

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from typing import TextIO

    from structlog.typing import ExceptionRenderer, ExcInfo, Processor

    from mindroom.constants import RuntimePaths

__all__ = [
    "bound_log_context",
    "configure_default_logging",
    "current_log_file",
    "exception_frames",
    "get_logger",
    "setup_logging",
    "setup_subprocess_logging",
    "subprocess_logging_env",
    "use_library_logging",
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


class _Mem0KeywordSearchFilter(logging.Filter):
    """Drop Mem0's advice to leave Chroma, which it logs for every memory MindRoom builds."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return False for Mem0's Chroma keyword-search warning and True for every other record.

        Chroma is the only vector store MindRoom configures, so the advice to switch to another one cannot be
        followed, and MindRoom builds a Mem0 memory for every memory operation.
        """
        return not (
            record.name == "mem0.memory.main"
            and record.getMessage().startswith("The 'chroma' vector store does not support keyword search.")
        )


def _scrub_request_record(record: logging.LogRecord) -> bool:
    """Keep the query, headers, and frame contents of uvicorn's requests out of its records, since they can carry secrets.

    Returns False for each raw WebSocket traffic record, whether or not it carries such data, and True for every other
    record, its query cut.
    """
    message = record.msg if isinstance(record.msg, str) else ""
    # The websockets library logs each raw handshake line, header, and frame on uvicorn's error logger at DEBUG.
    if message.startswith(("< ", "> ")):
        return False
    # Uvicorn logs each HTTP request's target, its path with query, as the third argument of an access line,
    # and each WebSocket handshake's as the second argument of a `WebSocket` line on its error logger.
    if record.name == "uvicorn.access":
        target = 2
    elif message.startswith('%s - "WebSocket %s"'):
        target = 1
    else:
        return True
    if isinstance(record.args, tuple) and len(record.args) > target:
        args = list(record.args)
        args[target] = str(args[target]).partition("?")[0]
        record.args = tuple(args)
    return True


class _AgnoLevelFilter(logging.Filter):
    """Drop records below the level MindRoom resolved for one Agno logger."""

    def __init__(self, level: str) -> None:
        super().__init__()
        self.levelno = logging.getLevelNamesMapping()[level]

    def filter(self, record: logging.LogRecord) -> bool:
        """Keep only records at or above the resolved level."""
        return record.levelno >= self.levelno


class _AgnoRecordsToRoot(logging.Handler):
    """Hand an Agno logger's records at or above the root logger's level to the root logger's handlers."""

    def __init__(self, agno_logger: logging.Logger) -> None:
        super().__init__()
        self._agno_logger = agno_logger

    def emit(self, record: logging.LogRecord) -> None:
        """Pass the record on while the program leaves the Agno logger as MindRoom set it up.

        The level is checked here since Agno's own runs reset its loggers' levels.
        """
        if self._agno_logger.propagate or self._agno_logger.handlers != [self]:
            return
        root = logging.getLogger()
        if record.levelno >= root.getEffectiveLevel():
            root.handle(record)


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


def _hand_rendered_line_to_logger(
    logger: object,
    _method_name: str,
    line: str,
) -> str | tuple[tuple[str], dict[str, None]]:
    """Hand a stdlib logger the rendered line without the exception, whose traceback the line already holds.

    Library logging hands every line to a stdlib logger, and a program can also set structlog's stdlib logger factory
    and keep these processors; `logging.Logger.exception` logs the current exception again unless told there is none.
    """
    if isinstance(logger, logging.Logger):
        return (line,), {"exc_info": None}
    return line


# structlog's builtin console renderer prints Rich tracebacks with every frame's
# locals: resolved config, credentials, subprocess environments. Processes that
# never call `setup_logging`, such as subprocess entrypoints and the sandbox
# runner, log through these processors instead, and so does library logging.
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
    cast("Processor", _hand_rendered_line_to_logger),
]


# MindRoom's own processors for library logging, which skip each record below its stdlib logger's level first.
_LIBRARY_PROCESSORS: list[Processor] = [
    structlog.stdlib.filter_by_level,
    structlog.stdlib.PositionalArgumentsFormatter(),
    *_DEFAULT_PROCESSORS,
]
_library_logging = False


def use_library_logging() -> None:
    """Hand MindRoom's and Agno's records to stdlib logging, where the program's own logging setup decides what shows.

    MindRoom's records keep `_DEFAULT_PROCESSORS`' rendering and redaction, whatever the program configures structlog
    to do. Until the program configures stdlib logging, Python prints only warnings and errors, on stderr.
    """
    global _library_logging
    _library_logging = True
    _route_agno_console_to_root()


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


# Several minutes of busy logging at tens of records a second, in a few tens of megabytes at most.
_MAX_QUEUED_LOG_FILE_RECORDS = 10_000


class _LogFile(logging.FileHandler):
    """A log file whose failed reopen is reported like a failed write, so it cannot end the writer thread."""

    def emit(self, record: logging.LogRecord) -> None:
        """Append the record, reopening the file first if it was closed."""
        try:
            super().emit(record)
        except Exception:
            self.handleError(record)


class _QueuedFileHandler(logging.handlers.QueueHandler):
    """Format records on the logging thread and append them to the log file from a writer thread.

    A log directory on a network file system can stall writes for minutes, and a write on the event loop would stall
    every agent with it. Formatting stays on the logging thread, so records keep their bound context and timestamps.
    The queue is capped so a long stall cannot grow memory without limit: from the cap until the writer has caught up,
    records skip the file only, stderr still gets them, and a note in the file says how many it skipped.
    """

    def __init__(self, filename: str, mode: str, encoding: str) -> None:
        # Unlike `queue.Queue`, `SimpleQueue` is reentrant, so a finalizer that logs during a put cannot deadlock it.
        super().__init__(queue.SimpleQueue())
        self._file = _LogFile(filename, mode=mode, encoding=encoding)
        self._dropped_records = 0
        # A later `dictConfig`, such as uvicorn's, drops this handler from the list `logging.shutdown` closes at exit.
        atexit.register(self.close)

    def enqueue(self, record: logging.LogRecord) -> None:
        """Queue the formatted record, or count it as dropped from the cap until the writer has caught up."""
        queued_records = cast("queue.SimpleQueue[logging.LogRecord]", self.queue).qsize()
        if queued_records >= _MAX_QUEUED_LOG_FILE_RECORDS or (self._dropped_records and queued_records):
            self._dropped_records += 1
            return
        if self.listener is None:
            # Any `dictConfig` closes every handler, even one it leaves on the root logger, and a closed `FileHandler`
            # reopens on its next record; this writer starts again the same way.
            listener = logging.handlers.QueueListener(self.queue, self._file)
            listener.start()
            self.listener = listener
        self._queue_dropped_records_note()
        self.queue.put_nowait(record)

    def _queue_dropped_records_note(self) -> None:
        """Queue a note where the file skipped records, if it skipped any since the last note."""
        if not self._dropped_records:
            return
        note = logging.makeLogRecord(
            {
                "name": __name__,
                "levelno": logging.WARNING,
                "levelname": "WARNING",
                "msg": "Log file dropped records while its writes were stalled",
                "dropped_records": self._dropped_records,
            },
        )
        self._dropped_records = 0
        # An empty context keeps the bound fields of whichever task logged next off the note.
        self.queue.put_nowait(contextvars.Context().run(self.prepare, note))

    def close(self) -> None:
        """Write every queued record, then close the file."""
        self.acquire()
        try:
            if self.listener is not None:
                self._queue_dropped_records_note()
                self.listener.stop()
                self.listener = None
            self._file.close()
        finally:
            self.release()
        super().close()


_current_log_file: Path | None = None


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
    global _current_log_file
    _current_log_file = log_file

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


def current_log_file() -> Path | None:
    """Return the log file `setup_logging` opened in this process, if any."""
    return _current_log_file


def setup_subprocess_logging(log_file: Path | None = None) -> None:
    """Configure a child process to log to stderr like the parent that built its `subprocess_logging_env`.

    The child writes no log file of its own, so each run does not leave a new file behind.
    A long-lived child can pass the parent's `current_log_file()` to append its records there too.
    """
    _configure_logging(level=os.getenv(_LOG_LEVEL_ENV, "INFO"), log_file=log_file)


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
            "filters": ["nio_validation", "mem0_keyword_search"],
        },
    }
    if log_file is not None:
        handlers["file"] = {
            "level": handler_level,
            "()": _QueuedFileHandler,
            "filename": str(log_file),
            "mode": "a",
            "encoding": "utf-8",
            "formatter": "json" if renderer_name == "json" else "text",
            "filters": ["nio_validation", "mem0_keyword_search"],
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
                "mem0_keyword_search": {
                    "()": _Mem0KeywordSearchFilter,
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
    _route_agno_loggers_to_root(loggers)
    return renderer_name


def _route_agno_loggers_to_root(loggers: dict[str, dict[str, object]]) -> None:
    """Send Agno records through the configured handlers, so they share their format, level, and stream."""
    # AGNO_COMPAT: Agno library loggers bypass the host's logging configuration.
    # Reason: Importing agno.utils.log gives the agno, agno-team, and agno-workflow loggers their own
    # stdout handler at INFO with propagation disabled, so their records ignore the configured level,
    # skip the JSON renderer and redaction, leave the knowledge refresh child as plain stderr lines, and
    # print to the stdout of a program that runs headless turns.
    # The loggers keep the INFO level Agno sets on them, and propagated records skip the root level, so a
    # logger filter enforces the resolved level instead.
    # Upstream issue: https://github.com/agno-agi/agno/issues/4097, closed after Agno 2.0 added
    # `configure_agno_logging`, which swaps module globals but leaves the default handlers on the
    # loggers that modules imported directly.
    # Upstream PR: none identified.
    # Remove when: Agno's library loggers propagate to the host's handlers without installing their own
    # handlers or setting their own levels.
    # Coverage: tests/test_logging_config.py::test_agno_records_follow_configured_format_and_level;
    # tests/test_logging_config.py::test_agno_records_keep_the_resolved_level_after_agno_resets_it;
    # tests/test_logging_config.py::test_subprocess_logging_matches_parent_format_and_level;
    # tests/test_logging_config.py::test_library_logging_routes_agno_records_at_the_root_level;
    # tests/test_logging_config.py::test_library_logging_reroutes_only_an_agno_logger_as_agno_set_it_up;
    # tests/test_logging_config.py::test_library_logging_steps_aside_when_the_program_sets_up_agno_after_a_call.
    from agno.utils import log as agno_log  # noqa: PLC0415 - keeps agno out of the config layer's imports.

    for agno_logger in (agno_log.agent_logger, agno_log.team_logger, agno_log.workflow_logger):
        for handler in list(agno_logger.handlers):
            agno_logger.removeHandler(handler)
        agno_logger.propagate = True
        for log_filter in list(agno_logger.filters):
            if isinstance(log_filter, _AgnoLevelFilter):
                agno_logger.removeFilter(log_filter)
        # No Agno logger name has a parent below the root, so only an exact override or the root level applies.
        level = cast("str", loggers.get(agno_logger.name, loggers[""])["level"])
        agno_logger.addFilter(_AgnoLevelFilter(level))


def _route_agno_console_to_root() -> None:
    """Swap the console handler of each Agno logger still as Agno set it up for one that hands records to the root.

    This is library logging's form of the AGNO_COMPAT workaround in `_route_agno_loggers_to_root`. A logger the program
    set up or replaced in any way keeps its setup, as does one the program sets up after this swap.
    """
    from agno.utils import log as agno_log  # noqa: PLC0415 - keeps agno out of the config layer's imports.

    for agno_logger in (agno_log.agent_logger, agno_log.team_logger, agno_log.workflow_logger):
        if not isinstance(agno_logger, logging.Logger) or agno_logger.propagate or len(agno_logger.handlers) != 1:
            continue
        (handler,) = agno_logger.handlers
        # Agno's `build_logger` gives a terminal its Rich handler and anything else this stdout one, neither with a
        # filter or a level of its own.
        agnos = isinstance(handler, agno_log.ColoredRichHandler) or (
            type(handler) is logging.StreamHandler
            and handler.stream in (sys.stdout, sys.__stdout__)
            and handler.formatter is not None
            and handler.formatter._fmt == "%(levelname)-7s %(message)s"
        )
        if agnos and not handler.filters and handler.level == logging.NOTSET:
            agno_logger.removeHandler(handler)
            agno_logger.addHandler(_AgnoRecordsToRoot(agno_logger))


def exception_frames(error: BaseException) -> list[str]:
    """Return an exception's stack locations without its message, source lines, or locals."""
    return [
        f"{Path(frame.filename).name}:{frame.lineno} ({frame.name})"
        for frame in traceback.extract_tb(error.__traceback__)
    ]


class _Logger:
    """A logger that follows structlog's configuration, or after `use_library_logging` MindRoom's own processors."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._configured_logger = structlog.get_logger(name)

    def __reduce__(self) -> tuple[type[_Logger], tuple[str]]:
        """Pickle and copy by name, since `__getattr__` cannot serve a copy whose attributes are not set yet."""
        return _Logger, (self._name,)

    def __getattr__(self, attribute: str) -> object:
        if _library_logging:
            # Built here rather than from structlog's configuration, which the program owns and may set to anything.
            logger = structlog.stdlib.BoundLogger(logging.getLogger(self._name), _LIBRARY_PROCESSORS, {})
            return getattr(logger, attribute)
        return getattr(self._configured_logger, attribute)


def get_logger(name: str = __name__) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger instance.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Configured structlog logger

    """
    return cast("structlog.stdlib.BoundLogger", _Logger(name))


def bound_log_context(**context: object) -> AbstractContextManager[None]:
    """Temporarily bind structured log fields for the current async/task scope."""
    return structlog.contextvars.bound_contextvars(**context)


if not structlog.is_configured():
    configure_default_logging()
# A logger's filters outlast every later `dictConfig`, such as the one each uvicorn server runs as it starts,
# so every server in a process that imports this module keeps these.
logging.getLogger("uvicorn.access").addFilter(_scrub_request_record)
logging.getLogger("uvicorn.error").addFilter(_scrub_request_record)
