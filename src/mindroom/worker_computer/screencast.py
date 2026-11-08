"""One viewer's CDP screencast of the agent's browser page, and the input that viewer sends while it holds control.

Each viewer opens its own CDP session on the page the agent's browser shows, as a worker service's bridge does, and
moves to another page when the agent does.
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

from mindroom.worker_computer.agent_browser import AgentPage
from mindroom.worker_computer.protocol import MAX_WORKER_STREAM_MESSAGE_BYTES

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from playwright.async_api import CDPSession, Page

_POLL_SECONDS = 1.0
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


_ViewerInput = _Mouse | _Wheel | _Key | _Text
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
        self._agent = AgentPage(agent_page, self._moved)
        self._controls = controls
        self._send_text = send_text
        self._send_bytes = send_bytes
        self._poll_seconds = poll_seconds
        # What the viewer was last sent, and what it should see next; send() closes the gap.
        self._sent_location: dict[str, str] | None = None
        self._location: dict[str, str] | None = None
        self._viewport: tuple[int, int] | None = None
        self._invalid = False
        # The newest frame not yet sent.
        self._frame: dict[str, Any] | None = None
        self._wake = asyncio.Event()

    async def follow(self) -> None:
        """Show the agent's current page until cancelled, moving to another page when the agent does."""
        while True:
            await self._agent.update()
            await self._refresh_location()
            await asyncio.sleep(self._poll_seconds)

    async def _moved(self, cdp: CDPSession | None) -> None:
        self._frame, self._viewport = None, None
        if cdp is None:
            return
        # Frames can arrive before startScreencast answers, so the session is current first.
        cdp.on("Page.screencastFrame", partial(self._on_frame, cdp))
        # The viewer learns which page it sees before that page's first frame.
        await self._refresh_location()
        await cdp.send(
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
        if (cdp := self._agent.cdp) is not None:
            try:
                target = (await cdp.send("Target.getTargetInfo"))["targetInfo"]
            except PlaywrightError:
                return
            location = {"url": target["url"], "title": target["title"]}
        if location != self._location:
            self._location = location
            self._wake.set()

    async def _on_frame(self, cdp: CDPSession, params: dict[str, Any]) -> None:
        if cdp is not self._agent.cdp:
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
            jpeg = base64.b64decode(frame["data"])
            viewport = (round(frame["metadata"]["deviceWidth"]), round(frame["metadata"]["deviceHeight"]))
            if len(jpeg) < MAX_WORKER_STREAM_MESSAGE_BYTES:
                if viewport != self._viewport:
                    self._viewport = viewport
                    await self._send_text(json.dumps({"type": "viewport", "width": viewport[0], "height": viewport[1]}))
                await self._send_bytes(jpeg)

    async def receive(self, raw: str) -> None:
        """Apply one viewer message; its input reaches the page only while the viewer holds control."""
        try:
            message = _ViewerMessage.validate_json(raw)
        except ValidationError:
            self._invalid = True
            self._wake.set()
            return
        cdp = self._agent.cdp
        if cdp is None or not self._controls():
            return
        # The page may close under this input, and the next poll moves to the agent's next page.
        with suppress(PlaywrightError, TimeoutError):
            await self._dispatch(cdp, message)

    async def _dispatch(self, cdp: CDPSession, message: _ViewerInput) -> None:
        if isinstance(message, _Mouse):
            button = message.button
            if message.event == "move" and button == "none":
                # Chromium drags only when a move names a held button, which DOM pointer moves report in buttons alone.
                button = next((name for name, bit in _BUTTON_BITS if message.buttons & bit), "none")
            await self._agent.input(
                cdp,
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
            await self._agent.input(
                cdp,
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
            await self._agent.input(cdp, "Input.dispatchKeyEvent", _key_event(message))
        else:
            await self._agent.input(cdp, "Input.insertText", {"text": message.text})

    async def close(self) -> None:
        """Release the buttons and keys this viewer still holds, so the page does not see them stuck, and detach."""
        await self._agent.close()
