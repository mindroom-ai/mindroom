"""A worker service's CDP session on the agent's browser page, under the Computer's control lease.

The agent's ``browser`` tool runs Chromium in this worker process through Playwright, which speaks CDP to it over a
pipe, so a bridge reaches the page without any debugging port.
Each bridge holds its own CDP session on the page the tool shows and moves it when the agent shows another page.
A service may watch with a few commands, and act only while its bridge holds the Computer's control lease.
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

_POLL_SECONDS = 1.0
# A page its script froze never answers input or a detach, so input, releasing what a leaving controller holds, and
# detaching give up after this.
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


class AgentPage:
    """One CDP session on the agent's current page that moves with the agent, and the input a controller holds there."""

    def __init__(
        self,
        agent_page: Callable[[], Page | None],
        moved: Callable[[CDPSession | None], Awaitable[None]],
    ) -> None:
        self.cdp: CDPSession | None = None
        self._agent_page = agent_page
        self._moved = moved
        self._page: Page | None = None
        self._shown = False
        self._buttons: set[str] = set()
        self._keys: dict[str, dict[str, Any]] = {}
        self._pointer: tuple[object, object] = (0, 0)

    async def update(self) -> None:
        """Move this session to the agent's current page if the agent shows another, and report each move."""
        page = self._agent_page()
        if self._shown and page is self._page:
            return
        self._shown = True
        previous, self.cdp, self._page = self.cdp, None, None
        # Buttons and keys held on the page left behind belong to that page.
        self._buttons.clear()
        self._keys.clear()
        if previous is not None:
            await _detach(previous)
        if page is not None:
            # A page that closes meanwhile is no longer the agent's at the next poll, which moves on.
            with suppress(PlaywrightError):
                self.cdp = await page.context.new_cdp_session(page)
                self._page = page
        with suppress(PlaywrightError):
            await self._moved(self.cdp)

    async def input(self, cdp: CDPSession, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send one input event, remembering which buttons and keys it leaves held."""
        if method == "Input.dispatchMouseEvent":
            self._pointer = (params.get("x", 0), params.get("y", 0))
            button = params.get("button")
            if params.get("type") == "mousePressed" and isinstance(button, str) and button != "none":
                self._buttons.add(button)
            elif params.get("type") == "mouseReleased" and isinstance(button, str):
                self._buttons.discard(button)
        elif method == "Input.dispatchKeyEvent":
            key = params.get("code") or params.get("key")
            if params.get("type") in ("keyDown", "rawKeyDown") and isinstance(key, str):
                self._keys[key] = params
            elif params.get("type") == "keyUp" and isinstance(key, str):
                self._keys.pop(key, None)
        async with asyncio.timeout(_INPUT_SECONDS):
            return await cdp.send(method, params)

    async def release(self) -> None:
        """Release the buttons and keys still held, so the page does not see them stuck."""
        cdp, buttons, keys = self.cdp, list(self._buttons), list(self._keys.values())
        self._buttons.clear()
        self._keys.clear()
        if cdp is None:
            return
        with suppress(PlaywrightError, TimeoutError):
            async with asyncio.timeout(_INPUT_SECONDS):
                for button in buttons:
                    x, y = self._pointer
                    await cdp.send(
                        "Input.dispatchMouseEvent",
                        {"type": "mouseReleased", "x": x, "y": y, "button": button},
                    )
                for key in keys:
                    up = {name: value for name, value in key.items() if name not in ("text", "unmodifiedText")}
                    await cdp.send("Input.dispatchKeyEvent", {**up, "type": "keyUp"})

    async def close(self) -> None:
        """Release what is still held and detach from the page."""
        await self.release()
        cdp, self.cdp, self._page = self.cdp, None, None
        if cdp is not None:
            await _detach(cdp)


class AgentBrowserBridge:
    """Answer one worker service's CDP commands on the agent's page under the Computer's rules, and send its events."""

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
        self._agent = AgentPage(computer.agent_page, self._moved)
        self._outbox: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: set[asyncio.Task[None]] = set()
        self._control = asyncio.Lock()
        self._lease: asyncio.Event | None = None
        self._showing: bool | None = None

    def _post(self, message: dict[str, Any]) -> None:
        self._outbox.put_nowait(json.dumps(message))

    def _start(self, coroutine: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def follow(self) -> None:
        """Follow the agent's current page until cancelled."""
        while True:
            await self._agent.update()
            await asyncio.sleep(self._poll_seconds)

    async def _moved(self, cdp: CDPSession | None) -> None:
        if cdp is not None:
            for event in _EVENTS:
                cdp.on(event, partial(self._forward, cdp, event))
        elif self._showing is False:
            # A page that cannot be attached to is tried again at each poll, which the service need not hear about.
            return
        self._showing = cdp is not None
        # The service starts what it needs, such as a screencast, again on the new page's session.
        self._post({"method": "MindRoom.page", "params": {"page": self._showing}})

    def _forward(self, cdp: CDPSession, method: str, params: dict[str, Any]) -> None:
        # The page this bridge is leaving can still deliver an event while its session detaches.
        if cdp is self._agent.cdp:
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
        cdp = self._agent.cdp
        if cdp is None:
            msg = "The agent's browser shows no page."
            raise _RefusedError(msg)
        if method in _WATCH:
            return await cdp.send(method, params)
        if self._lease is None or not self._computer.allows_input(self._id, self._lease):
            msg = "Take control of the Computer first."
            raise _RefusedError(msg)
        if method.startswith("Input."):
            return await self._agent.input(cdp, method, params)
        if method == "Page.navigate":
            params = _navigation(params)
        return await cdp.send(method, params)

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
                # The agent resumes on a page that sees nothing still held.
                await self._agent.release()
                await self._computer.release_control(self._id)

    async def _lease_ended(self, lease: asyncio.Event) -> None:
        # A release, a Computer stop or restart, or a dead display ends the lease; all but a release close the browser.
        await lease.wait()
        if self._lease is lease:
            self._lease = None
            self._post({"method": "MindRoom.control", "params": {"held": False}})

    async def close(self) -> None:
        """Release what this bridge holds on the page and its control lease, then detach from the page."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        lease, self._lease = self._lease, None
        if lease is not None:
            await self._agent.release()
            await self._computer.detach_stream(self._id, lease)
        await self._agent.close()
