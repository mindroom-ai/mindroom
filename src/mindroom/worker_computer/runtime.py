"""Single-loop owner for the worker display, browser, and control lease."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from uuid import uuid4

from mindroom.worker_computer.protocol import BrowserSession, ComputerDisplay, ComputerStatus


class ComputerControlError(RuntimeError):
    """A control transition or stale call cannot be applied."""


class WorkerComputerRuntime:
    """Serialize browser operations while retaining one worker-owned browser."""

    def __init__(self, display: ComputerDisplay) -> None:
        self.display = display
        self._lock = asyncio.Lock()
        self._generation = uuid4().hex
        self._epoch = 0
        self._state: Literal["starting", "ready", "stopped"] = "stopped"
        self._controller: str | None = None
        self._pending_controller: str | None = None
        self._streams: dict[str, asyncio.Event] = {}
        self._browser: BrowserSession | None = None
        self._browser_key: str | None = None
        self._monitor: asyncio.Task[None] | None = None

    def status(self) -> ComputerStatus:
        """Inspect state without starting a stopped display."""
        healthy = self._state != "ready" or self.display.healthy()
        return ComputerStatus(
            state=self._state if healthy else "stopped",
            generation=self._generation,
            controller_session_id=self._controller if healthy else None,
        )

    async def _start_locked(self) -> None:
        if self._state == "ready" and self.display.healthy():
            return
        if self._state != "stopped":
            await self._stop_locked()
        self._state = "starting"
        try:
            await self.display.start()
        except BaseException:
            await self._stop_locked()
            raise
        self._state = "ready"
        self._monitor = asyncio.create_task(self._watch_display())

    async def _watch_display(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            async with self._lock:
                if not self.display.healthy():
                    await self._stop_locked()
                    return

    async def ensure_started(self) -> ComputerStatus:
        """Lazily start the private display on the owning event loop."""
        async with self._lock:
            await self._start_locked()
            return self.status()

    async def _run_browser_action(self, action: Callable[[], Awaitable[object]]) -> object:
        """Run an agent action unless control or lifecycle changed while queued."""
        epoch = self._epoch
        async with self._lock:
            if self._controller is not None or self._pending_controller is not None:
                msg = "Computer is under user control; resume the agent first."
                raise ComputerControlError(msg)
            if epoch != self._epoch:
                msg = "Computer changed while this browser action was queued; retry."
                raise ComputerControlError(msg)
            await self._start_locked()
            try:
                return await action()
            except (TimeoutError, asyncio.CancelledError):
                await self._stop_locked()
                raise

    async def run_browser_call(
        self,
        binding_key: str,
        factory: Callable[[str], BrowserSession],
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> object:
        """Reuse the prepared browser binding until its scope or config changes."""

        async def action() -> object:
            if self._browser_key != binding_key:
                if self._browser is not None:
                    await self._browser.close()
                    self._invalidate()
                self._browser = None
                self._browser_key = None
                self._browser = factory(self.display.display)
                self._browser_key = binding_key
            assert self._browser is not None
            return await self._browser.execute(*args, **kwargs)

        return await self._run_browser_action(action)

    async def take_control(self, session_id: str, *, generation: str | None = None) -> ComputerStatus:
        """Wait for an active action, then grant exclusive control to a live viewer."""
        self._check_generation(generation)
        if self._controller not in (None, session_id) or self._pending_controller not in (None, session_id):
            msg = "Computer is controlled by another user session."
            raise ComputerControlError(msg)
        stream = self._streams.get(session_id)
        if stream is None or stream.is_set():
            msg = "Connect a live computer stream before taking control."
            raise ComputerControlError(msg)
        self._pending_controller = session_id
        self._epoch += 1
        try:
            async with self._lock:
                self._check_generation(generation)
                if self._streams.get(session_id) is not stream or stream.is_set() or self.status()["state"] != "ready":
                    msg = "Computer stream is no longer connected."
                    raise ComputerControlError(msg)
                self._controller = session_id
                return self.status()
        finally:
            if self._pending_controller == session_id:
                self._pending_controller = None

    async def release_control(self, session_id: str, *, generation: str | None = None) -> ComputerStatus:
        """Release only this viewer's control lease."""
        async with self._lock:
            self._check_generation(generation)
            if self._controller == session_id:
                self._controller = None
                self._epoch += 1
                # RFB disconnect resets keys/buttons held by this controller.
                # The same viewer can reconnect in watch mode immediately.
                stream = self._streams.pop(session_id, None)
                if stream is not None:
                    stream.set()
            return self.status()

    async def attach_stream(self, session_id: str, generation: str) -> asyncio.Event:
        """Replace an old stream; its disconnect cannot revoke the replacement."""
        async with self._lock:
            if generation != self._generation or self.status()["state"] != "ready":
                msg = "Computer generation is stale or stopped."
                raise ComputerControlError(msg)
            previous = self._streams.get(session_id)
            if previous is not None:
                previous.set()
            stream = asyncio.Event()
            self._streams[session_id] = stream
            return stream

    def allows_input(self, session_id: str, stream: asyncio.Event) -> bool:
        """Check current ownership for each parsed client message."""
        return (
            self._controller == session_id
            and self._streams.get(session_id) is stream
            and not stream.is_set()
            and self.status()["state"] == "ready"
        )

    async def detach_stream(self, session_id: str, stream: asyncio.Event) -> None:
        """Release a disconnected viewer without affecting a newer connection."""
        async with self._lock:
            if self._streams.get(session_id) is stream:
                del self._streams[session_id]
                stream.set()
                if self._controller == session_id:
                    self._controller = None
                    self._epoch += 1

    def _invalidate(self) -> None:
        self._epoch += 1
        self._generation = uuid4().hex
        self._controller = None
        for stream in self._streams.values():
            stream.set()
        self._streams.clear()

    async def _stop_locked(self) -> None:
        monitor, self._monitor = self._monitor, None
        if monitor is not None and monitor is not asyncio.current_task():
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        self._invalidate()
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            self._browser = None
            self._browser_key = None
            self._state = "stopped"
            await self.display.close()

    def _check_generation(self, generation: str | None) -> None:
        if generation is not None and generation != self._generation:
            msg = "Computer generation changed; create a new session."
            raise ComputerControlError(msg)

    async def stop(self, *, generation: str | None = None) -> ComputerStatus:
        """Stop owned processes and invalidate queued calls, retaining disk state."""
        self._check_generation(generation)
        self._epoch += 1
        async with self._lock:
            self._check_generation(generation)
            await self._stop_locked()
            return self.status()

    async def close(self) -> None:
        """Release all runtime resources at ASGI shutdown."""
        await self.stop()
