"""Lightweight timing instrumentation controlled by MINDROOM_TIMING env var."""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import os
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, ParamSpec, TypeVar, cast

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

# When set, log lines include the scope for grouping related timers.
timing_scope: ContextVar[str | None] = ContextVar("timing_scope", default=None)


def _is_enabled() -> bool:
    return os.environ.get("MINDROOM_TIMING", "") == "1"


def timed(label: str) -> Callable[[Callable[P, R]], Callable[P, R]]:  # noqa: C901
    """Decorator that logs elapsed time for sync/async functions.

    When MINDROOM_TIMING != "1", returns the original function unchanged (zero overhead).
    Log format: TIMING [<scope>] <label>: <elapsed>s  (scope omitted if not set)
    """

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        if not _is_enabled():
            return fn

        def emit_timing(start: float, kwargs: P.kwargs) -> None:
            scope = kwargs.get("timing_scope")
            if not isinstance(scope, str) or not scope:
                scope = timing_scope.get()
            prefix = f"[{scope}] " if scope else ""
            logger.info("TIMING %s%s: %.3fs", prefix, label, time.monotonic() - start)

        if inspect.isasyncgenfunction(fn):

            @functools.wraps(fn)
            async def async_generator_wrapper(*args: P.args, **kwargs: P.kwargs) -> AsyncIterator[object]:
                start = time.monotonic()
                try:
                    async_generator_fn = cast("Callable[P, AsyncIterator[object]]", fn)
                    async for item in async_generator_fn(*args, **kwargs):
                        yield item
                finally:
                    emit_timing(start, kwargs)

            return cast("Callable[P, R]", async_generator_wrapper)

        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                start = time.monotonic()
                try:
                    async_fn = cast("Callable[P, Awaitable[R]]", fn)
                    return await async_fn(*args, **kwargs)
                finally:
                    emit_timing(start, kwargs)

            return cast("Callable[P, R]", async_wrapper)

        @functools.wraps(fn)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            start = time.monotonic()
            try:
                return fn(*args, **kwargs)
            finally:
                emit_timing(start, kwargs)

        return sync_wrapper

    return decorator
