"""Observable Playwright boundary adapters for persistent browser lifecycle tests."""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class LifecyclePage:
    """Page adapter whose native events reach the real BrowserTools callbacks."""

    def __init__(self, url: str = "about:blank") -> None:
        self.url = url
        self.closed = False
        self.foreground = False
        self.listeners: dict[str, list[Callable[..., object]]] = {}

    async def bring_to_front(self) -> None:
        """Expose the page selected by the browser's native tab strip."""
        self.foreground = True

    def on(self, event: str, callback: Callable[..., object]) -> None:
        """Register a native page event listener."""
        self.listeners.setdefault(event, []).append(callback)

    async def emit(self, event: str, value: object) -> None:
        """Deliver one native event and await asynchronous side effects."""
        for callback in self.listeners.get(event, []):
            result = callback(value)
            if inspect.isawaitable(result):
                await result

    def is_closed(self) -> bool:
        """Return observable native page liveness."""
        return self.closed

    async def title(self) -> str:
        """Return the page label used by the real tab listing."""
        return self.url

    async def goto(self, url: str, **_kwargs: object) -> None:
        """Navigate without performing external network I/O."""
        self.url = url


class LifecycleBrowser:
    """Driver/context boundary with deferred phases and observable live resources."""

    def __init__(self, *, pause_at: str | None = None, initial_pages: bool = False, fail_start: bool = False) -> None:
        self.pause_at = pause_at
        self.reached = asyncio.Event()
        self.proceed = asyncio.Event()
        self.fail_start = fail_start
        self.start_cancelled = False
        self.live_resources: set[str] = set()
        self.pages = [LifecyclePage()] if initial_pages else []
        self.page_listeners: list[Callable[[LifecyclePage], object]] = []
        self.chromium = self

    async def checkpoint(self, phase: str) -> None:
        """Suspend a selected external operation until the test cancels it."""
        if phase == self.pause_at:
            self.reached.set()
            await self.proceed.wait()

    async def start(self) -> LifecycleBrowser:
        """Acquire the driver resource."""
        self.live_resources.add("driver")
        try:
            await self.checkpoint("driver_start")
        except asyncio.CancelledError:
            self.start_cancelled = True
            raise
        if self.fail_start:
            msg = "Driver startup failed."
            raise RuntimeError(msg)
        return self

    async def launch_persistent_context(self, **_kwargs: object) -> LifecycleBrowser:
        """Acquire a context only after launch completes."""
        await self.checkpoint("launch")
        self.live_resources.add("context")
        return self

    async def route(self, _pattern: str, _callback: object) -> None:
        """Install routing after the selected setup checkpoint."""
        await self.checkpoint("route")

    def on(self, event: str, callback: Callable[[LifecyclePage], object]) -> None:
        """Subscribe to native page creation."""
        assert event == "page"
        self.page_listeners.append(callback)

    def add_native_page(self, url: str = "about:blank") -> LifecyclePage:
        """Open a native page and notify any registered context listeners."""
        page = LifecyclePage(url)
        self.pages.append(page)
        for callback in self.page_listeners:
            callback(page)
        return page

    async def new_page(self) -> LifecyclePage:
        """Tool-created pages produce the same native page event as Chromium."""
        await self.checkpoint("new_page")
        return self.add_native_page()

    async def close(self) -> None:
        """Release the context resource."""
        self.live_resources.discard("context")
        for page in self.pages:
            page.closed = True

    async def stop(self) -> None:
        """Release the Playwright driver resource."""
        self.live_resources.discard("driver")

    async def __aexit__(self, *_args: object) -> None:
        """Close manager-owned resources when acquisition did not return a driver."""
        await self.stop()
