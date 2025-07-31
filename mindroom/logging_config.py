"""Logging configuration for mindroom using loguru."""

import logging
import sys

from loguru import logger


class InterceptHandler(logging.Handler):
    """Handler to intercept standard logging and redirect to loguru."""

    def emit(self, record):
        # Get corresponding Loguru level if it exists
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message
        frame, depth = sys._getframe(6), 6
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging(level="INFO", colorize=True):
    """
    Configure loguru for mindroom and intercept standard logging.

    Args:
        level: Minimum logging level
        colorize: Whether to use colors in output
    """
    # Remove default loguru handler
    logger.remove()

    # Add new handler with custom format
    logger.add(
        sys.stderr,
        colorize=colorize,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
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
