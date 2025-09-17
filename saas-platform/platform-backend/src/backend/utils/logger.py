"""
Sanitized logger wrapper for production use.
KISS principle - wraps standard Python logger with automatic sanitization.
"""

import logging
import os
from typing import Any
from .log_sanitizer import sanitize_args

# Get the standard logger
_logger = logging.getLogger("backend")


class SanitizedLogger:
    """
    Logger wrapper that automatically sanitizes sensitive data in production.
    """

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def _log(self, level: int, msg: str, *args: Any, **kwargs: Any) -> None:
        """Internal log method with sanitization."""
        # Check environment at runtime
        is_production = os.getenv("ENVIRONMENT", "development").lower() in ["production", "prod"]

        # Sanitize the message and arguments in production
        if is_production:
            # Sanitize both the message and any args
            sanitized = sanitize_args(msg, *args) if args else (sanitize_args(msg)[0],)
            if len(sanitized) > 1:
                self._logger.log(level, sanitized[0], *sanitized[1:], **kwargs)
            else:
                self._logger.log(level, sanitized[0], **kwargs)
        else:
            self._logger.log(level, msg, *args, **kwargs)

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.ERROR, msg, *args, **kwargs)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log an exception with traceback."""
        kwargs["exc_info"] = True
        self._log(logging.ERROR, msg, *args, **kwargs)

    def critical(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.CRITICAL, msg, *args, **kwargs)


# Create the sanitized logger instance
logger = SanitizedLogger(_logger)

# For backward compatibility, also export the original logger methods
debug = logger.debug
info = logger.info
warning = logger.warning
error = logger.error
exception = logger.exception
critical = logger.critical
