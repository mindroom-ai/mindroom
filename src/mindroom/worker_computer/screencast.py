"""One viewer's CDP screencast of the agent's browser page, and the input that viewer sends while it holds control.

The agent's ``browser`` tool runs Chromium in this worker process through Playwright, which speaks CDP to it over a
pipe, so a viewer reaches the page without any debugging port.
Each viewer opens its own CDP session on the page the tool shows and moves to another page when the agent does.
A frame is acknowledged once it is sent, skipped, or replaced by a newer one, so a slow viewer gets the newest frame
instead of a queue.
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

from mindroom.worker_computer.protocol import MAX_WORKER_STREAM_MESSAGE_BYTES

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from playwright.async_api import CDPSession, Page

_POLL_SECONDS = 1.0
# A page its script froze never answers input, so input, and releasing what a leaving viewer holds, give up after this.
_INPUT_SECONDS = 2.0
_JPEG_QUALITY = 80
# Chromium scales frames down to fit, which keeps them well below MAX_WORKER_STREAM_MESSAGE_BYTES.
_MAX_FRAME_WIDTH = 1920
_MAX_FRAME_HEIGHT = 1200
_CTRL_OR_META = 2 | 4
_MOUSE_EVENTS = {"down": "mousePressed", "up": "mouseReleased", "move": "mouseMoved"}
# A pointer may lie outside the page, as when a drag leaves the viewer's panel.
_Coordinate = Annotated[float, Field(ge=-16384, le=16384)]
_Modifiers = Annotated[int, Field(ge=0, le=15)]


class _Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Mouse(_Message):
    type: Literal["mouse"]
    event: Literal["down", "up", "move"]
    x: _Coordinate
    y: _Coordinate
    button: Literal["none", "left", "middle", "right", "back", "forward"] = "none"
    buttons: int = Field(default=0, ge=0, le=31)
    clicks: int = Field(default=1, ge=0, le=3)
    modifiers: _Modifiers = 0


class _Wheel(_Message):
    type: Literal["wheel"]
    x: _Coordinate
    y: _Coordinate
    dx: float = Field(default=0, ge=-10000, le=10000)
    dy: float = Field(default=0, ge=-10000, le=10000)
    modifiers: _Modifiers = 0


class _Key(_Message):
    type: Literal["key"]
    event: Literal["down", "up"]
    key: str = Field(min_length=1, max_length=32)
    code: str = Field(default="", max_length=32)
    key_code: int = Field(default=0, ge=0, le=255, alias="keyCode")
    text: str | None = Field(default=None, max_length=4)
    modifiers: _Modifiers = 0
    repeat: bool = False
    location: int = Field(default=0, ge=0, le=3)


class _Text(_Message):
    type: Literal["text"]
    text: str = Field(min_length=1)


_ViewerInput = _Mouse | _Wheel | _Key | _Text
_ViewerMessage = TypeAdapter(Annotated[_ViewerInput, Field(discriminator="type")])


def _key_event(message: _Key) -> dict[str, Any]:
    """Map a DOM keyboard event onto CDP's, producing text only for keys that type it."""
    text = message.text
    if text is None and not message.modifiers & _CTRL_OR_META:
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
    """Show one viewer the agent's current page and apply that viewer's input while it holds control."""

    def __init__(
        self,
        agent_page: Callable[[], Page | None],
        controls: Callable[[], bool],
        send_text: Callable[[str], Awaitable[None]],
        send_bytes: Callable[[bytes], Awaitable[None]],
        *,
        poll_seconds: float = _POLL_SECONDS,
    ) -> None:
        self._agent_page = agent_page
        self._controls = controls
        self._send_text = send_text
        self._send_bytes = send_bytes
        self._poll_seconds = poll_seconds
        self._page: Page | None = None
        self._cdp: CDPSession | None = None
        # What the viewer was last sent, and what it should see next; send() closes the gap.
        self._sent_location: dict[str, str] | None = None
        self._location: dict[str, str] | None = None
        self._viewport: tuple[int, int] | None = None
        self._invalid = False
        # The newest frame not yet sent, and frames it replaced, which Chromium still waits to have acknowledged.
        self._frame: tuple[CDPSession, dict[str, Any]] | None = None
        self._replaced: list[tuple[CDPSession, int]] = []
        self._wake = asyncio.Event()
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
            await asyncio.sleep(self._poll_seconds)

    async def _show(self, page: Page | None) -> None:
        previous, self._cdp, self._page, self._frame, self._viewport = self._cdp, None, None, None, None
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

    async def _refresh_location(self) -> None:
        location = {"url": "", "title": ""}
        if self._cdp is not None:
            try:
                target = (await self._cdp.send("Target.getTargetInfo"))["targetInfo"]
            except PlaywrightError:
                return
            location = {"url": target["url"], "title": target["title"]}
        if location != self._location:
            self._location = location
            self._wake.set()

    def _on_frame(self, cdp: CDPSession, params: dict[str, Any]) -> None:
        if cdp is not self._cdp:
            # The page this viewer is leaving can still deliver a frame while its session detaches.
            return
        if self._frame is not None:
            self._replaced.append((self._frame[0], self._frame[1]["sessionId"]))
        self._frame = (cdp, params)
        self._wake.set()

    async def send(self) -> None:
        """Send what changed and the newest frame until cancelled."""
        while True:
            await self._wake.wait()
            self._wake.clear()
            replaced, self._replaced = self._replaced, []
            for cdp, frame_id in replaced:
                await self._acknowledge(cdp, frame_id)
            if self._invalid:
                self._invalid = False
                await self._send_text(json.dumps({"type": "error", "detail": "Invalid screencast message."}))
            location = self._location
            if location is not None and location != self._sent_location:
                self._sent_location = location
                await self._send_text(json.dumps({"type": "url-changed", **location}))
            frame, self._frame = self._frame, None
            if frame is None:
                continue
            cdp, params = frame
            jpeg = base64.b64decode(params["data"])
            viewport = (round(params["metadata"]["deviceWidth"]), round(params["metadata"]["deviceHeight"]))
            if len(jpeg) < MAX_WORKER_STREAM_MESSAGE_BYTES:
                if viewport != self._viewport:
                    self._viewport = viewport
                    await self._send_text(json.dumps({"type": "viewport", "width": viewport[0], "height": viewport[1]}))
                await self._send_bytes(jpeg)
            await self._acknowledge(cdp, params["sessionId"])

    @staticmethod
    async def _acknowledge(cdp: CDPSession, frame_id: int) -> None:
        with suppress(PlaywrightError):
            await cdp.send("Page.screencastFrameAck", {"sessionId": frame_id})

    async def receive(self, raw: str) -> None:
        """Apply one viewer message; its input reaches the page only while the viewer holds control."""
        try:
            message = _ViewerMessage.validate_json(raw)
        except ValidationError:
            self._invalid = True
            self._wake.set()
            return
        cdp = self._cdp
        if cdp is None or not self._controls():
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
            await cdp.send(
                "Input.dispatchMouseEvent",
                {
                    "type": _MOUSE_EVENTS[message.event],
                    "x": message.x,
                    "y": message.y,
                    "button": message.button,
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
        else:
            await cdp.send("Input.insertText", {"text": message.text})

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
