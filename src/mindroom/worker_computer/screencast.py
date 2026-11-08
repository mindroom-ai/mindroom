"""One viewer's CDP screencast of the agent's browser page, and the input and navigation it sends while it holds control.

The agent's ``browser`` tool runs Chromium in this worker process through Playwright, which speaks CDP to it over a
pipe, so a viewer reaches the page without any debugging port.
Each viewer opens its own CDP session on the page the tool shows and moves to another page when the agent does.
Each frame is acknowledged as it arrives, so Chromium never waits on a slow viewer, which gets the newest frame instead
of a queue.
The wire protocol is described in docs/tools/worker-computer.md#page-stream.
"""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import suppress
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any, Literal

from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from mindroom.browser.fetch_guard import run_browser_tool_url_check
from mindroom.server_fetch_url import ServerFetchUrlError, validate_server_fetch_url
from mindroom.worker_computer.protocol import MAX_WORKER_STREAM_MESSAGE_BYTES

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from playwright.async_api import CDPSession, Page

_POLL_SECONDS = 1.0
# A page its script froze never answers input, so input, and releasing what a leaving viewer holds, give up after this.
# So does a navigation whose server has not answered yet, which keeps loading after that.
_INPUT_SECONDS = 2.0
# Checking a navigate address can wait out a host name lookup's retry, so it gives up later than input, but it still
# gives up: the viewer's later messages, and the stream noticing that the viewer left, wait for it.
_LOOKUP_SECONDS = 10.0
_JPEG_QUALITY = 80
# Chromium scales frames down to fit, which keeps them well below MAX_WORKER_STREAM_MESSAGE_BYTES.
_MAX_FRAME_WIDTH = 1920
_MAX_FRAME_HEIGHT = 1200
_CTRL_OR_META = 2 | 4
_MOUSE_EVENTS = {"down": "mousePressed", "up": "mouseReleased", "move": "mouseMoved"}
# The DOM buttons bitmask, in the order a move names the held button.
_BUTTON_BITS = (("left", 1), ("right", 2), ("middle", 4), ("back", 8), ("forward", 16))


# Fields take any value of their type: the viewer holds control of this page, and Chromium refuses what it cannot use.
class _Message(BaseModel):
    # NaN and infinity would reach Playwright's driver, which cannot encode them.
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class _Mouse(_Message):
    type: Literal["mouse"]
    event: Literal["down", "up", "move"]
    x: float
    y: float
    button: Literal["none", "left", "middle", "right", "back", "forward"] = "none"
    buttons: int = 0
    clicks: int = 1
    modifiers: int = 0


class _Wheel(_Message):
    type: Literal["wheel"]
    x: float
    y: float
    dx: float = 0
    dy: float = 0
    modifiers: int = 0


class _Key(_Message):
    type: Literal["key"]
    event: Literal["down", "up"]
    key: str
    code: str = ""
    key_code: int = Field(default=0, alias="keyCode")
    modifiers: int = 0
    repeat: bool = False
    location: int = 0


class _Text(_Message):
    type: Literal["text"]
    text: str


class _Navigate(_Message):
    type: Literal["navigate"]
    url: str


class _Command(_Message):
    type: Literal["back", "forward", "reload", "stop"]


_ViewerInput = _Mouse | _Wheel | _Key | _Text | _Navigate | _Command
_ViewerMessage = TypeAdapter(Annotated[_ViewerInput, Field(discriminator="type")])


def _key_event(message: _Key) -> dict[str, Any]:
    """Map a DOM keyboard event onto CDP's; printable keys and Enter type their text unless Control or Meta is held."""
    text = None
    if not message.modifiers & _CTRL_OR_META:
        text = "\r" if message.key == "Enter" else message.key if len(message.key) == 1 else None
    params: dict[str, Any] = {
        "type": "keyUp" if message.event == "up" else "keyDown" if text else "rawKeyDown",
        "key": message.key,
        "code": message.code,
        "windowsVirtualKeyCode": message.key_code,
        "nativeVirtualKeyCode": message.key_code,
        "modifiers": message.modifiers,
        "autoRepeat": message.repeat,
        "location": message.location,
    }
    if text and message.event == "down":
        params["text"] = text
        params["unmodifiedText"] = text
    return params


class ScreencastViewer:
    """Show one viewer the agent's current page and apply that viewer's input and navigation while it holds control."""

    def __init__(
        self,
        agent_page: Callable[[], Page | None],
        allows_private_networks: Callable[[], bool],
        controls: Callable[[], bool],
        send_text: Callable[[str], Awaitable[None]],
        send_bytes: Callable[[bytes], Awaitable[None]],
        *,
        poll_seconds: float = _POLL_SECONDS,
    ) -> None:
        self._agent_page = agent_page
        self._allows_private_networks = allows_private_networks
        self._controls = controls
        self._send_text = send_text
        self._send_bytes = send_bytes
        self._poll_seconds = poll_seconds
        self._page: Page | None = None
        self._cdp: CDPSession | None = None
        self._main_frame: str | None = None
        # What the viewer was last sent, and what it should see next; send() closes the gap.
        self._sent_location: dict[str, str | bool] | None = None
        self._location: dict[str, str | bool] | None = None
        self._sent_loading: bool | None = None
        self._loading: bool | None = False
        self._viewport: tuple[int, int] | None = None
        self._error: str | None = None
        # The newest frame not yet sent.
        self._frame: dict[str, Any] | None = None
        self._wake = asyncio.Event()
        # Set when the shown page starts or stops loading, which every navigation does, in-document ones included.
        self._navigated = asyncio.Event()
        self._buttons: set[str] = set()
        self._keys: dict[str, _Key] = {}
        self._pointer = (0.0, 0.0)

    async def follow(self) -> None:
        """Show the agent's current page until cancelled, moving to another page when the agent does."""
        while True:
            page = self._agent_page()
            if page is not self._page:
                await self._show(page)
            await self._refresh_location()
            # A navigation updates the viewer at once; the poll catches title changes and the agent moving on.
            with suppress(TimeoutError):
                async with asyncio.timeout(self._poll_seconds):
                    await self._navigated.wait()
            self._navigated.clear()

    async def _show(self, page: Page | None) -> None:
        previous, self._cdp, self._page, self._frame, self._viewport = self._cdp, None, None, None, None
        # Nothing loads without a page; whether a page loads is unknown until it answers or reports a change.
        self._loading = None if page is not None else False
        # Buttons and keys held on the page this viewer leaves belong to that page.
        self._buttons.clear()
        self._keys.clear()
        if previous is not None:
            with suppress(PlaywrightError):
                await previous.detach()
        if page is None:
            return
        with suppress(PlaywrightError):
            # A page that closes meanwhile is no longer the agent's at the next poll, which moves on.
            self._cdp = await page.context.new_cdp_session(page)
            self._page = page
            # Frames can arrive before startScreencast answers, so the session is current first.
            self._cdp.on("Page.screencastFrame", partial(self._on_frame, self._cdp))
            self._cdp.on("Page.frameStartedLoading", partial(self._on_loading, self._cdp, loading=True))
            self._cdp.on("Page.frameStoppedLoading", partial(self._on_loading, self._cdp, loading=False))
            # A page target's ID is its main frame's.
            self._main_frame = (await self._cdp.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
            # The viewer learns which page it sees before that page's first frame.
            await self._refresh_location()
            await self._cdp.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": _JPEG_QUALITY,
                    "maxWidth": _MAX_FRAME_WIDTH,
                    "maxHeight": _MAX_FRAME_HEIGHT,
                },
            )
            await self._read_loading(self._cdp)
        if self._loading is None:
            self._loading = False
        self._wake.set()

    async def _read_loading(self, cdp: CDPSession) -> None:
        """Start the page's loading events and read whether it loads now, unless an event has said so meanwhile."""
        try:
            # The page answers these only between navigations and while its script is not frozen, so they give up like
            # input.
            async with asyncio.timeout(_INPUT_SECONDS):
                await cdp.send("Page.enable")
                state = await cdp.send("Runtime.evaluate", {"expression": "document.readyState", "returnByValue": True})
            loading = state["result"].get("value") != "complete"
        except TimeoutError:
            # Until a navigation it was asked to make commits, as the agent's browser asks, Chromium shows its address
            # instead of the page's; a frozen page shows its own.
            target = (await cdp.send("Target.getTargetInfo"))["targetInfo"]
            history = await cdp.send("Page.getNavigationHistory")
            loading = target["url"] != history["entries"][history["currentIndex"]]["url"]
        if self._loading is None:
            self._loading = loading

    def _on_loading(self, cdp: CDPSession, params: dict[str, Any], *, loading: bool) -> None:
        if cdp is self._cdp and params["frameId"] == self._main_frame:
            self._loading = loading
            self._wake.set()
            self._navigated.set()

    async def _refresh_location(self) -> None:
        location: dict[str, str | bool] = {"url": "", "title": "", "canGoBack": False, "canGoForward": False}
        if self._cdp is not None:
            try:
                target = (await self._cdp.send("Target.getTargetInfo"))["targetInfo"]
                history = await self._cdp.send("Page.getNavigationHistory")
            except PlaywrightError:
                return
            index, entries = history["currentIndex"], history["entries"]
            location = {
                "url": target["url"],
                "title": target["title"],
                "canGoBack": index > 0,
                "canGoForward": index < len(entries) - 1,
            }
        if location != self._location:
            self._location = location
            self._wake.set()

    async def _on_frame(self, cdp: CDPSession, params: dict[str, Any]) -> None:
        if cdp is not self._cdp:
            # The page this viewer is leaving can still deliver a frame while its session detaches.
            return
        self._frame = params
        self._wake.set()
        # Chromium stops after a few frames it waits on, and the look a page settles on would then never be sent.
        with suppress(PlaywrightError):
            await cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})

    async def send(self) -> None:
        """Send what changed and the newest frame until cancelled."""
        while True:
            await self._wake.wait()
            self._wake.clear()
            error, self._error = self._error, None
            if error is not None:
                await self._send_text(json.dumps({"type": "error", "detail": error}))
            location = self._location
            if location is not None and location != self._sent_location:
                self._sent_location = location
                await self._send_text(json.dumps({"type": "url-changed", **location}))
            loading = self._loading
            if loading is not None and loading != self._sent_loading:
                self._sent_loading = loading
                await self._send_text(json.dumps({"type": "loading", "loading": loading}))
            frame, self._frame = self._frame, None
            if frame is None:
                continue
            jpeg = base64.b64decode(frame["data"])
            viewport = (round(frame["metadata"]["deviceWidth"]), round(frame["metadata"]["deviceHeight"]))
            if len(jpeg) < MAX_WORKER_STREAM_MESSAGE_BYTES:
                if viewport != self._viewport:
                    self._viewport = viewport
                    await self._send_text(json.dumps({"type": "viewport", "width": viewport[0], "height": viewport[1]}))
                await self._send_bytes(jpeg)

    def _report(self, detail: str) -> None:
        self._error = detail
        self._wake.set()

    async def receive(self, raw: str) -> None:
        """Apply one viewer message; its input and navigation reach the page only while the viewer holds control."""
        try:
            message = _ViewerMessage.validate_json(raw)
        except ValidationError:
            self._report("Invalid screencast message.")
            return
        cdp = self._cdp
        if cdp is None or not self._controls():
            return
        if isinstance(message, _Navigate) and not await self._allows_address(message.url):
            return
        # The page may close under this input, and the next poll moves to the agent's next page.
        with suppress(PlaywrightError, TimeoutError):
            async with asyncio.timeout(_INPUT_SECONDS):
                await self._dispatch(cdp, message)

    async def _dispatch(self, cdp: CDPSession, message: _ViewerInput) -> None:
        if isinstance(message, _Mouse):
            if message.event == "down" and message.button != "none":
                self._buttons.add(message.button)
            elif message.event == "up":
                self._buttons.discard(message.button)
            self._pointer = (message.x, message.y)
            button = message.button
            if message.event == "move" and button == "none":
                # Chromium drags only when a move names a held button, which DOM pointer moves report in buttons alone.
                button = next((name for name, bit in _BUTTON_BITS if message.buttons & bit), "none")
            await cdp.send(
                "Input.dispatchMouseEvent",
                {
                    "type": _MOUSE_EVENTS[message.event],
                    "x": message.x,
                    "y": message.y,
                    "button": button,
                    "buttons": message.buttons,
                    "clickCount": message.clicks,
                    "modifiers": message.modifiers,
                },
            )
        elif isinstance(message, _Wheel):
            await cdp.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": message.x,
                    "y": message.y,
                    "deltaX": message.dx,
                    "deltaY": message.dy,
                    "modifiers": message.modifiers,
                },
            )
        elif isinstance(message, _Key):
            if message.event == "down":
                self._keys[message.code or message.key] = message
            else:
                self._keys.pop(message.code or message.key, None)
            await cdp.send("Input.dispatchKeyEvent", _key_event(message))
        elif isinstance(message, _Text):
            await cdp.send("Input.insertText", {"text": message.text})
        else:
            await self._command(cdp, message)

    async def _command(self, cdp: CDPSession, message: _Navigate | _Command) -> None:
        if isinstance(message, _Navigate):
            await cdp.send("Page.navigate", {"url": message.url})
        elif message.type == "reload":
            await cdp.send("Page.reload")
        elif message.type == "stop":
            await cdp.send("Page.stopLoading")
        else:
            history = await cdp.send("Page.getNavigationHistory")
            index = history["currentIndex"] + (1 if message.type == "forward" else -1)
            if 0 <= index < len(history["entries"]):
                await cdp.send("Page.navigateToHistoryEntry", {"entryId": history["entries"][index]["id"]})

    async def _allows_address(self, url: str) -> bool:
        """Return whether the agent's browser tool would open this address, or it is about:blank; report it if not."""
        if url == "about:blank":
            return True
        try:
            async with asyncio.timeout(_LOOKUP_SECONDS):
                # The agent's browser in a Computer reaches the worker's own loopback; its page's request guard and
                # destination relay still check every request and redirect.
                await run_browser_tool_url_check(
                    validate_server_fetch_url,
                    url,
                    allow_private_networks=self._allows_private_networks(),
                    allow_loopback=True,
                )
        except (ServerFetchUrlError, TimeoutError):
            self._report("This address cannot be opened.")
            return False
        return True

    async def close(self) -> None:
        """Release the buttons and keys this viewer still holds, so the page does not see them stuck, and detach."""
        cdp = self._cdp
        if cdp is not None:
            with suppress(PlaywrightError, TimeoutError):
                async with asyncio.timeout(_INPUT_SECONDS):
                    for button in self._buttons:
                        await cdp.send(
                            "Input.dispatchMouseEvent",
                            {"type": "mouseReleased", "x": self._pointer[0], "y": self._pointer[1], "button": button},
                        )
                    for key in self._keys.values():
                        await cdp.send("Input.dispatchKeyEvent", _key_event(key.model_copy(update={"event": "up"})))
        await self._show(None)
