"""A worker service's own CDP session on the agent's browser page, gated by the Computer's control lease.

The agent's ``browser`` tool drives Chromium from this runner process through Playwright, so the bridge opens its CDP
sessions through Playwright too, and the browser needs no debugging port.
A service may always send the watching commands, and the acting ones only while its bridge holds the Computer's
control; every other command is refused before it reaches the page.
The wire protocol is described in docs/plugins.md#the-agents-browser.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, ConfigDict, StrictInt, ValidationError

from mindroom.server_fetch_url import ServerFetchUrlError, validate_server_fetch_url
from mindroom.worker_computer.runtime import ComputerControlError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from playwright.async_api import CDPSession, Page

    from mindroom.worker_computer.runtime import WorkerComputerRuntime

# How often the bridge looks at which page the agent's browser shows.
_POLL_SECONDS = 1.0
# A page whose script hangs answers neither input nor a detach, so the bridge stops waiting on those after this.
_INPUT_SECONDS = 2.0
_WATCH = frozenset(
    {
        "Page.startScreencast",
        "Page.stopScreencast",
        "Page.screencastFrameAck",
        "Target.getTargetInfo",
        "Page.getNavigationHistory",
        "Page.getLayoutMetrics",
        "Page.enable",
    },
)
_ACT = frozenset(
    {
        "Input.dispatchMouseEvent",
        "Input.dispatchKeyEvent",
        "Input.insertText",
        "Page.navigate",
        "Page.navigateToHistoryEntry",
        "Page.reload",
        "Page.stopLoading",
        "Emulation.setDeviceMetricsOverride",
        "Emulation.clearDeviceMetricsOverride",
    },
)
# Parameters that would run script in the page or describe another target.
_REFUSED_PARAMS = {"Page.reload": "scriptToEvaluateOnLoad", "Target.getTargetInfo": "targetId"}
_EVENTS = (
    "Page.screencastFrame",
    "Page.screencastVisibilityChanged",
    "Page.frameNavigated",
    "Page.navigatedWithinDocument",
    "Page.frameStartedLoading",
    "Page.frameStoppedLoading",
)


class _RefusedError(Exception):
    """A command the bridge answers with an error without sending it to the page."""


class _Command(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: StrictInt
    method: str
    params: dict[str, Any] = {}


def _navigation(params: dict[str, Any]) -> dict[str, Any]:
    """Return navigation parameters whose address the agent's browser may open: http, https, or a blank page."""
    url = params.get("url")
    if url == "about:blank":
        return params
    if not isinstance(url, str):
        msg = "Page.navigate needs a url."
        raise _RefusedError(msg)
    try:
        # The browser's request guard and destination relay check every request against the agent's own network
        # settings, so this first check only keeps out other schemes and addresses no browser may reach.
        url = validate_server_fetch_url(url, allow_private_networks=True, allow_loopback=True, resolve_hostnames=False)
    except ServerFetchUrlError as error:
        msg = f"The agent's browser cannot open this address ({error.reason})."
        raise _RefusedError(msg) from error
    return {**params, "url": url}


def _check(method: str, params: dict[str, Any]) -> None:
    """Refuse a method outside the allowlists, a parameter outside the bridge's reach, and numbers JSON cannot carry."""
    if method not in _WATCH | _ACT:
        msg = f"{method} is not available through the agent's browser bridge."
        raise _RefusedError(msg)
    if _REFUSED_PARAMS.get(method) in params:
        msg = f"{method} is not available with {_REFUSED_PARAMS[method]}."
        raise _RefusedError(msg)
    try:
        json.dumps(params, allow_nan=False)
    except ValueError:
        # Playwright's driver cannot encode such a number and drops its connection, and with it the agent's browser.
        msg = "Numbers must be finite."
        raise _RefusedError(msg) from None


async def _detach(cdp: CDPSession) -> None:
    with suppress(PlaywrightError, TimeoutError):
        async with asyncio.timeout(_INPUT_SECONDS):
            await cdp.detach()


class AgentBrowserBridge:
    """Answer one worker service's CDP commands on the agent's current page under the Computer's rules."""

    def __init__(
        self,
        computer: WorkerComputerRuntime,
        send_text: Callable[[str], Awaitable[None]],
        *,
        poll_seconds: float = _POLL_SECONDS,
    ) -> None:
        self._computer = computer
        self._send_text = send_text
        self._poll_seconds = poll_seconds
        # The Computer leases control to this bridge under an ID no viewer session uses.
        self._id = f"agent-browser-{uuid4().hex}"
        self._outbox: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: set[asyncio.Task[None]] = set()
        self._control = asyncio.Lock()
        self._lease: asyncio.Event | None = None
        # Set while a release runs, so no press slips in behind the releases it sends.
        self._releasing = False
        self._page: Page | None = None
        self._cdp: CDPSession | None = None
        # Whether the service was last told that there is a page, and None before it was told anything.
        self._reported: bool | None = None
        # The buttons and keys this bridge pressed and has not released yet, and where it last moved the pointer.
        self._buttons: set[str] = set()
        self._keys: dict[str, dict[str, Any]] = {}
        self._pointer: tuple[object, object] = (0, 0)

    def _post(self, message: dict[str, Any]) -> None:
        self._outbox.put_nowait(json.dumps(message))

    def _start(self, coroutine: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def follow(self) -> None:
        """Keep this bridge's session on the page the agent's browser shows, until cancelled."""
        while True:
            await self._attach(self._computer.agent_page())
            await asyncio.sleep(self._poll_seconds)

    async def _attach(self, page: Page | None) -> None:
        if page is self._page and self._reported is not None:
            return
        await self._leave_page()
        if page is not None:
            # A page that closes meanwhile stays unattached, and the next poll tries the page the agent shows then.
            with suppress(PlaywrightError):
                cdp = await page.context.new_cdp_session(page)
                for event in _EVENTS:
                    cdp.on(event, partial(self._forward, cdp, event))
                self._cdp, self._page = cdp, page
        showing = self._cdp is not None
        # Every new page is reported, because its session needs a screencast started again; a missing page once.
        if showing or self._reported is not False:
            self._reported = showing
            self._post({"method": "MindRoom.page", "params": {"page": showing}})

    async def _leave_page(self) -> None:
        # What this bridge still presses is released on the page it leaves, which alone saw it go down.
        await self._release_input()
        previous, self._cdp, self._page = self._cdp, None, None
        if previous is not None:
            await _detach(previous)

    def _forward(self, cdp: CDPSession, method: str, params: dict[str, Any]) -> None:
        # A session the bridge is detaching from can still deliver an event, which belongs to the page it left.
        if cdp is self._cdp:
            self._post({"method": method, "params": params})

    async def send(self) -> None:
        """Send answers and events in order until cancelled."""
        while True:
            await self._send_text(await self._outbox.get())

    def receive(self, raw: str) -> None:
        """Start one command; its answer follows when it completes, so a slow navigation holds up no other command."""
        try:
            command = _Command.model_validate_json(raw)
        except ValidationError:
            self._post({"id": None, "error": {"code": -32600, "message": "Invalid command."}})
            return
        self._start(self._answer(command))

    async def _answer(self, command: _Command) -> None:
        try:
            result = await self._run(command.method, command.params)
        except (_RefusedError, ComputerControlError, PlaywrightError) as error:
            self._post({"id": command.id, "error": {"code": -32000, "message": str(error)}})
        except TimeoutError:
            self._post({"id": command.id, "error": {"code": -32000, "message": "The page did not answer in time."}})
        else:
            self._post({"id": command.id, "result": result})

    async def _run(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "MindRoom.takeControl":
            await self._take_control()
            return {}
        if method == "MindRoom.releaseControl":
            await self._release_control()
            return {}
        _check(method, params)
        cdp = self._cdp
        if cdp is None:
            msg = "The agent's browser shows no page."
            raise _RefusedError(msg)
        if method in _WATCH:
            return await cdp.send(method, params)
        if self._lease is None or self._releasing or not self._computer.allows_input(self._id, self._lease):
            msg = "Take control of the Computer first."
            raise _RefusedError(msg)
        if method == "Page.navigate":
            params = _navigation(params)
        if not method.startswith("Input."):
            return await cdp.send(method, params)
        self._press(method, params)
        async with asyncio.timeout(_INPUT_SECONDS):
            return await cdp.send(method, params)

    def _press(self, method: str, params: dict[str, Any]) -> None:
        """Note which buttons and keys an input event leaves pressed."""
        kind = params.get("type")
        if method == "Input.dispatchMouseEvent":
            self._pointer = (params.get("x", 0), params.get("y", 0))
            button = params.get("button")
            if kind == "mousePressed" and isinstance(button, str) and button != "none":
                self._buttons.add(button)
            elif kind == "mouseReleased" and isinstance(button, str):
                self._buttons.discard(button)
        elif method == "Input.dispatchKeyEvent":
            key = params.get("code") or params.get("key")
            if kind in ("keyDown", "rawKeyDown") and isinstance(key, str):
                self._keys[key] = params
            elif kind == "keyUp" and isinstance(key, str):
                self._keys.pop(key, None)

    async def _release_input(self) -> None:
        """Release the buttons and keys this bridge still presses, so the agent never resumes with one held down."""
        # They are forgotten only once all their releases are sent, so a release that leaving cancels runs again then.
        if (cdp := self._cdp) is not None:
            x, y = self._pointer
            with suppress(PlaywrightError, TimeoutError):
                async with asyncio.timeout(_INPUT_SECONDS):
                    for button in list(self._buttons):
                        await cdp.send(
                            "Input.dispatchMouseEvent",
                            {"type": "mouseReleased", "x": x, "y": y, "button": button},
                        )
                    for key in list(self._keys.values()):
                        await cdp.send("Input.dispatchKeyEvent", {**key, "type": "keyUp"})
        self._buttons.clear()
        self._keys.clear()

    async def _take_control(self) -> None:
        async with self._control:
            if self._lease is not None and not self._lease.is_set():
                return
            generation = self._computer.status()["generation"]
            lease = await self._computer.attach_stream(self._id, generation)
            try:
                await self._computer.take_control(self._id, generation=generation)
            except BaseException:
                await self._computer.detach_stream(self._id, lease)
                raise
            self._lease = lease
            self._post({"method": "MindRoom.control", "params": {"held": True}})
            self._start(self._lease_ended(lease))

    async def _release_control(self) -> None:
        async with self._control:
            if self._lease is not None:
                self._releasing = True
                try:
                    await self._release_input()
                    await self._computer.release_control(self._id)
                finally:
                    self._releasing = False

    async def _lease_ended(self, lease: asyncio.Event) -> None:
        # A release, a Computer stop or restart, or a dead display ends the lease; all but a release close the browser.
        await lease.wait()
        if self._lease is lease:
            self._lease = None
            self._post({"method": "MindRoom.control", "params": {"held": False}})

    async def close(self) -> None:
        """Release what this bridge presses and its control lease, then detach from the page."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._release_input()
        lease, self._lease = self._lease, None
        if lease is not None:
            await self._computer.detach_stream(self._id, lease)
        await self._leave_page()
