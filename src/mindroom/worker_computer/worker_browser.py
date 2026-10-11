"""Single-loop resource owner for a dedicated worker's headless browser."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mindroom.background_tasks import run_coroutine_until_complete

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.custom_tools.browser import BrowserTools


class WorkerBrowserRuntime:
    """Retain browser resources while every call uses its freshly prepared toolkit."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._toolkit: BrowserTools | None = None
        self._binding: tuple[str, tuple[tuple[str, str], ...]] | None = None
        self._cleanup_required = False
        self._closed = False

    async def run(
        self,
        toolkit: BrowserTools,
        binding: tuple[str, tuple[tuple[str, str], ...]],
        execute: Callable[[], Awaitable[object]],
    ) -> object:
        """Serialize calls and transfer only browser resources to the current request."""
        async with self._lock:
            if self._closed:
                msg = "Worker browser is shutting down."
                raise RuntimeError(msg)
            if self._cleanup_required or self._binding != binding:
                await run_coroutine_until_complete(self._close_browser())
            if self._toolkit is not None:
                toolkit.take_worker_session(self._toolkit)
            self._toolkit = toolkit
            self._binding = binding
            try:
                return await execute()
            except (TimeoutError, asyncio.CancelledError):
                await run_coroutine_until_complete(self._close_browser())
                raise

    async def _close_browser(self) -> None:
        if self._toolkit is not None:
            self._cleanup_required = True
            await self._toolkit.aclose()
            self._toolkit = None
        self._binding = None
        self._cleanup_required = False

    async def close(self) -> None:
        """Reject queued calls and drain owned resources before ASGI shutdown."""
        self._closed = True
        await run_coroutine_until_complete(self._close())

    async def _close(self) -> None:
        async with self._lock:
            await self._close_browser()
