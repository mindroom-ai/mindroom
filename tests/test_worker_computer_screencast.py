"""A viewer's screencast of the agent's browser page, and the input it may send while it holds control."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright
from starlette.websockets import WebSocketDisconnect

from mindroom.api.sandbox_runner import initialize_sandbox_runner_app
from mindroom.api.worker_computer import router
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.worker_computer.protocol import (
    MAX_SCREENCAST_MESSAGE_BYTES,
    MAX_WORKER_STREAM_MESSAGE_BYTES,
    SCREENCAST_PROTOCOL,
    BrowserSession,
)
from mindroom.worker_computer.runtime import WorkerComputerRuntime
from mindroom.worker_computer.screencast import ScreencastViewer
from tests.test_worker_computer_runtime import FakeDisplay

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from playwright.async_api import Page

RUNNER_TOKEN = "test-runner-token"  # noqa: S105 - isolated test credential
_TIMEOUT = 10
_FORM = """<title>Form</title><input id=field><button id=go onclick="document.title = 'clicked'">Go</button>
<textarea id=notes></textarea>
<script>
window.released = [];
document.addEventListener("wheel", (event) => window.wheeled = event.deltaY);
document.addEventListener("keyup", (event) => released.push(event.key));
document.addEventListener("mouseup", (event) => released.push("button " + event.button));
</script>"""


class _Viewer:
    """What a screencast sends one viewer."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.arrived = asyncio.Event()
        self.frames: asyncio.Queue[bytes] = asyncio.Queue()

    async def send_text(self, text: str) -> None:
        self.messages.append(json.loads(text))
        self.arrived.set()

    async def send_bytes(self, data: bytes) -> None:
        await self.frames.put(data)

    async def message(self, kind: str) -> dict[str, Any]:
        """Take the oldest message of one type, leaving messages of other types for later."""
        async with asyncio.timeout(_TIMEOUT):
            while True:
                for index, message in enumerate(self.messages):
                    if message["type"] == kind:
                        return self.messages.pop(index)
                self.arrived.clear()
                await self.arrived.wait()

    async def frame(self) -> bytes:
        async with asyncio.timeout(_TIMEOUT):
            return await self.frames.get()


@asynccontextmanager
async def _watching(viewer: ScreencastViewer) -> AsyncIterator[None]:
    tasks = [asyncio.create_task(viewer.follow()), asyncio.create_task(viewer.send())]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await viewer.close()


@pytest_asyncio.fixture
async def page() -> AsyncIterator[Page]:
    """A page of a real headless Chromium, the way the agent's browser tool holds one in this process."""
    executable = os.environ.get("MINDROOM_TEST_BROWSER_EXECUTABLE") or shutil.which("chromium")
    if executable is None:
        pytest.skip("Chromium required for the screencast")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=executable, headless=True)
        try:
            context = await browser.new_context(viewport={"width": 800, "height": 600})
            page = await context.new_page()
            await page.set_content(_FORM)
            yield page
        finally:
            await browser.close()


async def _click(viewer: ScreencastViewer, page: Page, selector: str) -> None:
    box = await page.locator(selector).bounding_box()
    assert box is not None
    point = {"type": "mouse", "x": box["x"] + 5, "y": box["y"] + 5, "button": "left"}
    await viewer.receive(json.dumps({**point, "event": "down", "buttons": 1}))
    await viewer.receive(json.dumps({**point, "event": "up"}))


async def _type(viewer: ScreencastViewer, text: str) -> None:
    for character in text:
        key = {"type": "key", "key": character, "code": "Key" + character.upper(), "keyCode": ord(character.upper())}
        await viewer.receive(json.dumps({**key, "event": "down"}))
        await viewer.receive(json.dumps({**key, "event": "up"}))


@pytest.mark.asyncio
async def test_screencast_shows_the_page_and_applies_input_only_while_the_viewer_controls(page: Page) -> None:
    """Frames, the page's address, and input that reaches the page only under control, released when it leaves."""
    controls = False
    sent = _Viewer()
    viewer = ScreencastViewer(lambda: page, lambda: controls, sent.send_text, sent.send_bytes, poll_seconds=0.05)
    async with _watching(viewer):
        assert await sent.message("url-changed") == {"type": "url-changed", "url": "about:blank", "title": "Form"}
        assert await sent.message("viewport") == {"type": "viewport", "width": 800, "height": 600}
        assert (await sent.frame()).startswith(b"\xff\xd8")

        await _click(viewer, page, "#go")
        await _click(viewer, page, "#field")
        await _type(viewer, "no")
        assert await page.title() == "Form"
        assert await page.input_value("#field") == ""

        controls = True
        await _click(viewer, page, "#go")
        assert (await sent.message("url-changed"))["title"] == "clicked"
        await _click(viewer, page, "#field")
        await _type(viewer, "hi")
        await viewer.receive(json.dumps({"type": "text", "text": " ünï"}))
        backspace = {"type": "key", "key": "Backspace", "code": "Backspace", "keyCode": 8}
        await viewer.receive(json.dumps({**backspace, "event": "down"}))
        await viewer.receive(json.dumps({**backspace, "event": "up"}))
        assert await page.input_value("#field") == "hi ün"
        # Chromium would insert the text of a key held with Meta, which is a shortcut, not typing.
        meta_q = {"type": "key", "key": "q", "code": "KeyQ", "keyCode": 81, "modifiers": 4}
        await viewer.receive(json.dumps({**meta_q, "event": "down"}))
        await viewer.receive(json.dumps({**meta_q, "event": "up"}))
        assert await page.input_value("#field") == "hi ün"
        await _click(viewer, page, "#notes")
        enter = {"type": "key", "key": "Enter", "code": "Enter", "keyCode": 13}
        await viewer.receive(json.dumps({**enter, "event": "down"}))
        await viewer.receive(json.dumps({**enter, "event": "up"}))
        assert await page.input_value("#notes") == "\n"
        await viewer.receive(json.dumps({"type": "wheel", "x": 10, "y": 10, "dy": 120}))
        await page.wait_for_function("window.wheeled === 120", timeout=_TIMEOUT * 1000)

        await viewer.receive("{}")
        assert await sent.message("error") == {"type": "error", "detail": "Invalid screencast message."}

        await page.evaluate("released.length = 0")
        await viewer.receive(json.dumps({"type": "key", "event": "down", "key": "Shift", "code": "ShiftLeft"}))
        await viewer.receive(json.dumps({"type": "mouse", "event": "down", "x": 1, "y": 1, "button": "right"}))
    assert sorted(await page.evaluate("released")) == ["Shift", "button 2"]


@pytest.mark.asyncio
async def test_screencast_follows_the_agent_to_another_page_and_to_none(page: Page) -> None:
    """When the agent shows another page, or none, the viewer's stream moves with it without reconnecting."""
    second = await page.context.new_page()
    await second.set_viewport_size({"width": 640, "height": 480})
    await second.set_content("<title>Second</title><p>second page</p>")
    shown: Page | None = page
    sent = _Viewer()
    viewer = ScreencastViewer(lambda: shown, lambda: True, sent.send_text, sent.send_bytes, poll_seconds=0.05)
    async with _watching(viewer):
        assert (await sent.message("url-changed"))["title"] == "Form"
        assert await sent.message("viewport") == {"type": "viewport", "width": 800, "height": 600}
        # The agent's browser brings the tab it makes active to the front, where Chromium renders it.
        await second.bring_to_front()
        shown = second
        assert (await sent.message("url-changed"))["title"] == "Second"
        assert await sent.message("viewport") == {"type": "viewport", "width": 640, "height": 480}
        shown = None
        assert await sent.message("url-changed") == {"type": "url-changed", "url": "", "title": ""}
        await _click(viewer, second, "p")
        while not sent.frames.empty():
            sent.frames.get_nowait()
        await second.evaluate("document.body.style.background = 'red'")
        await asyncio.sleep(0.3)
        assert sent.frames.empty()


class _FakeSession:
    """A CDP session whose frames the test delivers by hand."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any] | None]] = []
        self.on_frame: Any = None
        self.screencasting = asyncio.Event()
        self.acknowledged: asyncio.Queue[int] = asyncio.Queue()
        # A frame Chromium had in flight when the viewer detached, delivered during the detach.
        self.frame_on_detach: bytes | None = None
        # A page its script froze, whose input calls never return.
        self.frozen = False

    def on(self, event: str, handler: object) -> None:
        assert event == "Page.screencastFrame"
        self.on_frame = handler

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sent.append((method, params))
        if self.frozen and method.startswith("Input."):
            await asyncio.Event().wait()
        if method == "Page.startScreencast":
            self.screencasting.set()
        if method == "Page.screencastFrameAck":
            assert params is not None
            await self.acknowledged.put(params["sessionId"])
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"url": "https://example.org/", "title": "Example"}}
        return {}

    async def detach(self) -> None:
        self.sent.append(("detach", None))
        if self.frame_on_detach is not None:
            self.frame(0, self.frame_on_detach)

    def frame(self, frame_id: int, data: bytes) -> None:
        metadata = {"deviceWidth": 1280, "deviceHeight": 800}
        self.on_frame({"data": base64.b64encode(data).decode(), "metadata": metadata, "sessionId": frame_id})

    def calls(self, method: str) -> list[dict[str, Any] | None]:
        return [params for name, params in self.sent if name == method]


class _FakePage:
    """The page side of the fake: its context opens the one fake session."""

    def __init__(self) -> None:
        self.session = _FakeSession()
        self.context = self

    async def new_cdp_session(self, _page: object) -> _FakeSession:
        return self.session


@pytest.mark.asyncio
async def test_slow_viewer_gets_the_newest_frame_and_chromium_every_acknowledgement() -> None:
    """A newer frame replaces one not yet sent, an oversized frame is skipped, and Chromium hears about each."""
    fake = _FakePage()
    sent = _Viewer()
    viewer = ScreencastViewer(lambda: cast("Page", fake), lambda: True, sent.send_text, sent.send_bytes)

    async def acknowledged() -> int:
        return await asyncio.wait_for(fake.session.acknowledged.get(), _TIMEOUT)

    async with _watching(viewer):
        await asyncio.wait_for(fake.session.screencasting.wait(), _TIMEOUT)
        fake.session.frame(1, b"first")
        fake.session.frame(2, b"second")
        assert await sent.frame() == b"second"
        assert [await acknowledged(), await acknowledged()] == [1, 2]
        # A frame at the limit is one message too big for the backend hop.
        fake.session.frame(3, b"x" * MAX_WORKER_STREAM_MESSAGE_BYTES)
        assert await acknowledged() == 3
        fake.session.frame(4, b"fourth")
        assert await sent.frame() == b"fourth"
        assert await acknowledged() == 4
        assert sent.frames.empty()


@pytest.mark.asyncio
async def test_leaving_a_page_detaches_from_it_and_never_shows_its_late_frame() -> None:
    """A frame the left page delivers while the viewer detaches never reaches the viewer after it moved on."""
    old, new = _FakePage(), _FakePage()
    old.session.frame_on_detach = b"old"
    shown = old
    sent = _Viewer()
    viewer = ScreencastViewer(
        lambda: cast("Page", shown),
        lambda: True,
        sent.send_text,
        sent.send_bytes,
        poll_seconds=0.01,
    )
    async with _watching(viewer):
        await asyncio.wait_for(old.session.screencasting.wait(), _TIMEOUT)
        shown = new
        await asyncio.wait_for(new.session.screencasting.wait(), _TIMEOUT)
        assert ("detach", None) in old.session.sent
        new.session.frame(1, b"new")
        assert await sent.frame() == b"new"


@pytest.mark.asyncio
async def test_a_page_frozen_by_its_script_holds_neither_input_nor_a_leaving_viewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Input to a page that never answers gives up, so the viewer keeps reading, and still detaches when it leaves."""
    monkeypatch.setattr("mindroom.worker_computer.screencast._INPUT_SECONDS", 0.05)
    fake = _FakePage()
    sent = _Viewer()
    viewer = ScreencastViewer(lambda: cast("Page", fake), lambda: True, sent.send_text, sent.send_bytes)
    follow = asyncio.create_task(viewer.follow())
    await asyncio.wait_for(fake.session.screencasting.wait(), _TIMEOUT)
    fake.session.frozen = True
    down = json.dumps({"type": "mouse", "event": "down", "x": 1, "y": 1, "button": "left"})
    await asyncio.wait_for(viewer.receive(down), _TIMEOUT)
    follow.cancel()
    await asyncio.gather(follow, return_exceptions=True)
    await asyncio.wait_for(viewer.close(), _TIMEOUT)
    assert fake.session.sent[-2][0] == "Input.dispatchMouseEvent"
    assert fake.session.sent[-1] == ("detach", None)


def test_worker_route_requires_the_runner_and_takes_input_only_under_the_control_lease(tmp_path: Path) -> None:
    """The worker serves the screencast like the desktop stream: runner token, stream lease, and control lease."""
    fake = _FakePage()
    runtime = WorkerComputerRuntime(FakeDisplay())

    async def unused(*_args: object, **_kwargs: object) -> None:
        return None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        session = BrowserSession(execute=unused, close=unused, page=lambda: cast("Page", fake))
        await runtime.run_browser_call("binding", lambda _display: session, [], {})
        yield
        await runtime.close()

    app = FastAPI(lifespan=lifespan)
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    initialize_sandbox_runner_app(app, paths, config=Config(), runner_token=RUNNER_TOKEN)
    app.state.worker_computer = runtime
    app.include_router(router)
    headers = {"X-Mindroom-Sandbox-Token": RUNNER_TOKEN}
    key = json.dumps({"type": "key", "event": "down", "key": "a", "code": "KeyA", "keyCode": 65})
    with TestClient(app) as client:
        generation = client.get("/computer", headers=headers).json()["generation"]
        url = f"/computer/screencast?session_id=viewer&generation={generation}"
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(url, subprotocols=[SCREENCAST_PROTOCOL]):
            pass
        with client.websocket_connect(url, headers=dict(headers), subprotocols=[SCREENCAST_PROTOCOL]) as websocket:
            assert websocket.accepted_subprotocol == SCREENCAST_PROTOCOL
            message = {"type": "url-changed", "url": "https://example.org/", "title": "Example"}
            assert json.loads(websocket.receive_text()) == message
            websocket.send_text(key)
            control = {"session_id": "viewer", "action": "take"}
            assert client.post("/computer/control", headers=headers, json=control).status_code == 200
            websocket.send_text(key)
            websocket.send_text("{}")
            assert json.loads(websocket.receive_text())["type"] == "error"
            release = {"session_id": "viewer", "action": "release"}
            assert client.post("/computer/control", headers=headers, json=release).status_code == 200
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()
        keys = fake.session.calls("Input.dispatchKeyEvent")
        assert [(params["type"], params["key"]) for params in keys if params] == [("keyDown", "a"), ("keyUp", "a")]
        assert fake.session.sent[-1] == ("detach", None)
        assert runtime.status()["controller_session_id"] is None
        with client.websocket_connect(url, headers=dict(headers), subprotocols=[SCREENCAST_PROTOCOL]) as websocket:
            websocket.receive_text()
            websocket.send_bytes(b"binary input")
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()
        with client.websocket_connect(url, headers=dict(headers), subprotocols=[SCREENCAST_PROTOCOL]) as websocket:
            websocket.receive_text()
            websocket.send_text(" " * (MAX_SCREENCAST_MESSAGE_BYTES + 1))
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()
