"""A viewer's screencast of the agent's browser page, and the input and navigation it may send while it holds control."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import time
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast

import pytest
import pytest_asyncio
from aiohttp import web
from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from starlette.websockets import WebSocketDisconnect

from mindroom.api.computer_browser_binding import select_browser_provider
from mindroom.api.sandbox_runner import initialize_sandbox_runner_app
from mindroom.api.worker_computer import router
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.browser import BrowserTools
from mindroom.tools.browser import browser_tools
from mindroom.worker_computer.protocol import (
    MAX_SCREENCAST_MESSAGE_BYTES,
    MAX_WORKER_STREAM_MESSAGE_BYTES,
    SCREENCAST_PROTOCOL,
    BrowserSession,
)
from mindroom.worker_computer.runtime import WorkerComputerRuntime
from mindroom.worker_computer.screencast import ScreencastViewer
from tests.test_worker_computer_runtime import FakeDisplay, unused

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator
    from pathlib import Path

    from playwright.async_api import Page
    from starlette.testclient import WebSocketTestSession

RUNNER_TOKEN = "test-runner-token"  # noqa: S105 - isolated test credential
_TIMEOUT = 10
_FORM = """<title>Form</title><input id=field><button id=go onclick="document.title = 'clicked'">Go</button>
<textarea id=notes></textarea><input id=slider type=range>
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

    async def reaches(self, expected: dict[str, Any]) -> None:
        """Take messages of the expected type until one equals it, as a state the viewer passes through settles."""
        while await self.message(expected["type"]) != expected:
            pass

    async def frame(self) -> bytes:
        async with asyncio.timeout(_TIMEOUT):
            return await self.frames.get()

    def screencast(
        self,
        page: Callable[[], Page | None],
        controls: Callable[[], bool],
        **options: float,
    ) -> ScreencastViewer:
        """A screencast of the agent's page to this viewer, from a browser that may not reach private networks."""
        return ScreencastViewer(page, lambda: False, controls, self.send_text, self.send_bytes, **options)


def _location(url: str, title: str, *, back: bool = False, forward: bool = False) -> dict[str, Any]:
    return {"type": "url-changed", "url": url, "title": title, "canGoBack": back, "canGoForward": forward}


_LOADING = {"type": "loading", "loading": True}
_LOADED = {"type": "loading", "loading": False}
_INVALID = {"type": "error", "detail": "Invalid screencast message."}
_REFUSED = {"type": "error", "detail": "This address cannot be opened."}


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


@dataclass
class _Site:
    """A local web server whose held responses wait until the test releases them."""

    origin: str = ""
    hits: Counter[str] = field(default_factory=Counter)
    held: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    def url(self, path: str) -> str:
        return self.origin + path


@pytest_asyncio.fixture
async def site() -> AsyncIterator[_Site]:
    """Pages named by their path, a page held until released, and a page whose image is held until released."""
    served = _Site()

    async def named(request: web.Request) -> web.Response:
        served.hits[request.path] += 1
        return web.Response(text=f"<title>{request.match_info['name']}</title>", content_type="text/html")

    async def held(_request: web.Request) -> web.Response:
        served.held.set()
        await served.release.wait()
        return web.Response(text="<title>Held</title>", content_type="text/html", headers={"Cache-Control": "no-store"})

    async def with_held_image(_request: web.Request) -> web.Response:
        return web.Response(text="<title>Image</title><img src=/held>", content_type="text/html")

    app = web.Application()
    app.router.add_get("/page/{name}", named)
    app.router.add_get("/held", held)
    app.router.add_get("/image", with_held_image)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=0)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    served.origin = f"http://127.0.0.1:{runner.addresses[0][1]}"
    try:
        yield served
    finally:
        served.release.set()
        await runner.cleanup()


async def _click(viewer: ScreencastViewer, page: Page, selector: str) -> None:
    box = await page.locator(selector).bounding_box()
    assert box is not None
    point = {"type": "mouse", "x": box["x"] + 5, "y": box["y"] + 5, "button": "left"}
    await viewer.receive(json.dumps({**point, "event": "down", "buttons": 1}))
    await viewer.receive(json.dumps({**point, "event": "up"}))


async def _press(viewer: ScreencastViewer, key: dict[str, Any]) -> None:
    await viewer.receive(json.dumps({"type": "key", **key, "event": "down"}))
    await viewer.receive(json.dumps({"type": "key", **key, "event": "up"}))


async def _type(viewer: ScreencastViewer, text: str) -> None:
    for character in text:
        await _press(viewer, {"key": character, "code": "Key" + character.upper(), "keyCode": ord(character.upper())})


@pytest.mark.asyncio
async def test_screencast_shows_the_page_and_applies_input_only_while_the_viewer_controls(page: Page) -> None:
    """Frames, the page's address, and input that reaches the page only under control, released when it leaves."""
    controls = False
    sent = _Viewer()
    viewer = sent.screencast(lambda: page, lambda: controls, poll_seconds=0.05)
    async with _watching(viewer):
        assert await sent.message("url-changed") == _location("about:blank", "Form")
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
        await _press(viewer, {"key": "Backspace", "code": "Backspace", "keyCode": 8})
        assert await page.input_value("#field") == "hi ün"
        # Chromium would insert the text of a key held with Meta, which is a shortcut, not typing.
        await _press(viewer, {"key": "q", "code": "KeyQ", "keyCode": 81, "modifiers": 4})
        assert await page.input_value("#field") == "hi ün"
        await _click(viewer, page, "#notes")
        await _press(viewer, {"key": "Enter", "code": "Enter", "keyCode": 13})
        assert await page.input_value("#notes") == "\n"
        await viewer.receive(json.dumps({"type": "wheel", "x": 10, "y": 10, "dy": 120}))
        await page.wait_for_function("window.wheeled === 120", timeout=_TIMEOUT * 1000)
        # A drag that leaves the viewer's panel ends outside the page, which still sees the button go up.
        await page.evaluate("released.length = 0")
        await viewer.receive(json.dumps({"type": "mouse", "event": "down", "x": 5, "y": 5, "button": "left"}))
        await viewer.receive(json.dumps({"type": "mouse", "event": "up", "x": -20, "y": 5, "button": "left"}))
        # A fourth quick click counts on, as macOS browsers report it.
        fourth = {"type": "mouse", "x": 5, "y": 5, "button": "left", "clicks": 4}
        await viewer.receive(json.dumps({**fourth, "event": "down"}))
        await viewer.receive(json.dumps({**fourth, "event": "up"}))
        assert await page.evaluate("released") == ["button 0", "button 0"]
        # A drag's moves report the held button in buttons alone, as DOM pointer moves do.
        box = await page.locator("#slider").bounding_box()
        assert box is not None
        left, right, middle = box["x"] + 2, box["x"] + box["width"] - 2, box["y"] + box["height"] / 2
        await viewer.receive(json.dumps({"type": "mouse", "event": "down", "x": left, "y": middle, "button": "left"}))
        await viewer.receive(json.dumps({"type": "mouse", "event": "move", "x": right, "y": middle, "buttons": 1}))
        await viewer.receive(json.dumps({"type": "mouse", "event": "up", "x": right, "y": middle, "button": "left"}))
        assert int(await page.input_value("#slider")) > 90

        await viewer.receive("{}")
        assert await sent.message("error") == _INVALID
        await viewer.receive('{"type": "mouse", "event": "move", "x": NaN, "y": 1}')
        assert await sent.message("error") == _INVALID

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
    viewer = sent.screencast(lambda: shown, lambda: True, poll_seconds=0.05)
    async with _watching(viewer):
        assert (await sent.message("url-changed"))["title"] == "Form"
        assert await sent.message("viewport") == {"type": "viewport", "width": 800, "height": 600}
        # The agent's browser brings the tab it makes active to the front, where Chromium renders it.
        await second.bring_to_front()
        shown = second
        assert (await sent.message("url-changed"))["title"] == "Second"
        assert await sent.message("viewport") == {"type": "viewport", "width": 640, "height": 480}
        shown = None
        assert await sent.message("url-changed") == _location("", "")
        await _click(viewer, second, "p")
        while not sent.frames.empty():
            sent.frames.get_nowait()
        await second.evaluate("document.body.style.background = 'red'")
        await asyncio.sleep(0.3)
        assert sent.frames.empty()


@pytest.mark.asyncio
async def test_controlling_viewer_navigates_and_sees_the_history_and_loading(page: Page, site: _Site) -> None:
    """Navigation, history, reload and stop act only under control, and the viewer sees where the page is."""
    controls = False
    sent = _Viewer()
    # Each navigation updates the viewer at once, long before the next poll.
    viewer = sent.screencast(lambda: page, lambda: controls, poll_seconds=60)
    one, two = site.url("/page/One"), site.url("/page/Two")
    async with _watching(viewer):
        assert await sent.message("url-changed") == _location("about:blank", "Form")
        assert await sent.message("loading") == _LOADED
        # A frame inside the page loading is not the page loading, as a browser's own loading indicator shows.
        await page.evaluate(
            f"document.body.append(Object.assign(document.createElement('iframe'), {{src: '{site.url('/held')}'}}))",
        )
        await asyncio.wait_for(site.held.wait(), _TIMEOUT)
        # Chromium reports a frame loading before it requests the frame, and this answer comes after that report.
        await page.evaluate("1")
        await viewer.receive("{}")
        await sent.message("error")
        assert not [message for message in sent.messages if message["type"] == "loading"]
        for message in ({"type": "navigate", "url": one}, {"type": "reload"}, {"type": "navigate", "url": "file:///"}):
            await viewer.receive(json.dumps(message))

        controls = True
        await viewer.receive(json.dumps({"type": "navigate", "url": one}))
        await sent.reaches(_location(one, "One", back=True))
        await viewer.receive(json.dumps({"type": "navigate", "url": two}))
        await sent.reaches(_location(two, "Two", back=True))
        # The navigation and reload sent without control never reached the page.
        assert site.hits == {"/page/One": 1, "/page/Two": 1}
        assert await page.evaluate("history.length") == 3
        assert not [message for message in sent.messages if message["type"] == "error"]
        await viewer.receive(json.dumps({"type": "back"}))
        await sent.reaches(_location(one, "One", back=True, forward=True))
        await viewer.receive(json.dumps({"type": "forward"}))
        await sent.reaches(_location(two, "Two", back=True))
        await viewer.receive(json.dumps({"type": "forward"}))
        await page.evaluate("window.before = true")
        await viewer.receive(json.dumps({"type": "reload"}))
        await page.wait_for_function("window.before === undefined", timeout=_TIMEOUT * 1000)
        assert site.hits["/page/Two"] == 2

        # A server that has not answered holds the navigation only up to the input bound, and stop abandons it.
        await asyncio.wait_for(viewer.receive(json.dumps({"type": "navigate", "url": site.url("/held")})), _TIMEOUT)
        await sent.reaches(_LOADING)
        await viewer.receive(json.dumps({"type": "stop"}))
        await sent.reaches(_LOADED)
        assert page.url == two
        assert await page.evaluate("history.length") == 3
        # An in-page navigation reaches the viewer at once as well.
        pushed = two + "#pushed"
        await page.evaluate("history.pushState(null, '', '#pushed')")
        await sent.reaches(_location(pushed, "Two", back=True))

        for refused in (
            "file:///etc/passwd",
            "chrome://version",
            "view-source:" + two,
            "javascript:document.title = 'ran'",
            "data:text/html,<title>data</title>",
            "http://10.0.0.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://metadata.google.internal/",
        ):
            await viewer.receive(json.dumps({"type": "navigate", "url": refused}))
            assert await sent.message("error") == _REFUSED
        assert page.url == pushed
        assert await page.title() == "Two"
        await viewer.receive(json.dumps({"type": "navigate", "url": "about:blank"}))
        await page.wait_for_url("about:blank", timeout=_TIMEOUT * 1000)


@pytest.mark.asyncio
async def test_viewer_of_no_page_is_told_there_is_nothing_to_show() -> None:
    """Before the agent's browser starts, the viewer gets an empty location and no loading."""
    sent = _Viewer()
    viewer = sent.screencast(lambda: None, lambda: True)
    async with _watching(viewer):
        assert await sent.message("url-changed") == _location("", "")
        assert await sent.message("loading") == _LOADED


@pytest.mark.asyncio
async def test_viewer_connecting_while_the_page_loads_is_told_so(page: Page, site: _Site) -> None:
    """A page still loading when the viewer arrives shows as loading until it finishes."""
    await page.goto(site.url("/image"), wait_until="domcontentloaded")
    sent = _Viewer()
    viewer = sent.screencast(lambda: page, lambda: False)
    async with _watching(viewer):
        assert await sent.message("loading") == _LOADING
        site.release.set()
        assert await sent.message("loading") == _LOADED


@pytest.mark.asyncio
async def test_viewer_connecting_while_a_navigation_waits_for_its_server_is_told_the_page_loads(
    page: Page,
    site: _Site,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page cannot answer before the navigation commits, and the navigation Chromium shows pending still counts."""
    monkeypatch.setattr("mindroom.worker_computer.screencast._INPUT_SECONDS", 0.2)
    navigation = asyncio.create_task(page.goto(site.url("/held"), timeout=_TIMEOUT * 1000))
    await asyncio.wait_for(site.held.wait(), _TIMEOUT)
    sent = _Viewer()
    viewer = sent.screencast(lambda: page, lambda: False)
    async with _watching(viewer):
        assert await sent.message("loading") == _LOADING
        site.release.set()
        await navigation
        assert await sent.message("loading") == _LOADED


class _FakeSession:
    """A CDP session whose frames the test delivers by hand."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any] | None]] = []
        self.handlers: dict[str, Any] = {}
        self.screencasting = asyncio.Event()
        self.acknowledged: asyncio.Queue[int] = asyncio.Queue()
        # A frame Chromium had in flight when the viewer detached, delivered during the detach.
        self.frame_on_detach: bytes | None = None
        # A page its script froze, whose input, Page.enable and evaluation never return.
        self.frozen = False
        self.ready_state = "complete"
        # Whether the page reports that it stopped loading before it answers what its readyState was.
        self.stops_loading_before_answering = False

    def on(self, event: str, handler: object) -> None:
        self.handlers[event] = handler

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sent.append((method, params))
        if self.frozen and (method.startswith("Input.") or method in {"Page.enable", "Runtime.evaluate"}):
            await asyncio.Event().wait()
        if method == "Page.startScreencast":
            self.screencasting.set()
        if method == "Page.screencastFrameAck":
            assert params is not None
            await self.acknowledged.put(params["sessionId"])
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": "page", "url": "https://example.org/", "title": "Example"}}
        if method == "Page.getNavigationHistory":
            return {"currentIndex": 0, "entries": [{"id": 1, "url": "https://example.org/", "title": "Example"}]}
        if method == "Runtime.evaluate":
            if self.stops_loading_before_answering:
                self.handlers["Page.frameStoppedLoading"]({"frameId": "page"})
            return {"result": {"type": "string", "value": self.ready_state}}
        return {}

    async def detach(self) -> None:
        self.sent.append(("detach", None))
        if self.frame_on_detach is not None:
            await self.frame(0, self.frame_on_detach)

    async def frame(self, frame_id: int, data: bytes) -> None:
        metadata = {"deviceWidth": 1280, "deviceHeight": 800}
        # Playwright runs an async event handler as a task; awaiting it here delivers the frame at a known point.
        frame = {"data": base64.b64encode(data).decode(), "metadata": metadata, "sessionId": frame_id}
        await self.handlers["Page.screencastFrame"](frame)


class _FakePage:
    """The page side of the fake: its context opens the one fake session."""

    def __init__(self) -> None:
        self.session = _FakeSession()
        self.context = self
        # A page that closed before the viewer could attach to it.
        self.closed = False

    async def new_cdp_session(self, _page: object) -> _FakeSession:
        if self.closed:
            msg = "Target page, context or browser has been closed"
            raise PlaywrightError(msg)
        return self.session


@pytest.mark.asyncio
async def test_slow_viewer_gets_the_newest_frame_and_chromium_every_acknowledgement_at_once() -> None:
    """While a slow viewer still sends a frame, newer ones replace each other and Chromium hears about each at once.

    Chromium stops making frames while it waits on acknowledgements, so a page that settled during a slow send would
    otherwise never show the viewer its final look.
    """
    fake = _FakePage()
    link = asyncio.Semaphore(0)
    started: asyncio.Queue[bytes] = asyncio.Queue()
    delivered: asyncio.Queue[bytes] = asyncio.Queue()

    async def over_a_slow_link(jpeg: bytes) -> None:
        await started.put(jpeg)
        await link.acquire()
        await delivered.put(jpeg)

    viewer = ScreencastViewer(lambda: cast("Page", fake), lambda: False, lambda: True, unused, over_a_slow_link)

    async def acknowledged() -> int:
        return await asyncio.wait_for(fake.session.acknowledged.get(), _TIMEOUT)

    async def next_frame(frames: asyncio.Queue[bytes]) -> bytes:
        return await asyncio.wait_for(frames.get(), _TIMEOUT)

    async with _watching(viewer):
        await asyncio.wait_for(fake.session.screencasting.wait(), _TIMEOUT)
        await fake.session.frame(1, b"first")
        assert await next_frame(started) == b"first"
        await fake.session.frame(2, b"second")
        await fake.session.frame(3, b"third")
        assert [await acknowledged() for _ in range(3)] == [1, 2, 3]
        link.release()
        assert await next_frame(delivered) == b"first"
        assert await next_frame(started) == b"third"
        # A frame at the limit is one message too big for the backend hop.
        await fake.session.frame(4, b"x" * MAX_WORKER_STREAM_MESSAGE_BYTES)
        link.release()
        # The viewer takes the waiting frame before this returns.
        assert await next_frame(delivered) == b"third"
        await fake.session.frame(5, b"fifth")
        assert await next_frame(started) == b"fifth"
        assert [await acknowledged() for _ in range(2)] == [4, 5]


@pytest.mark.asyncio
async def test_leaving_a_page_detaches_from_it_and_never_shows_its_late_frame() -> None:
    """A frame the left page delivers while the viewer detaches never reaches the viewer after it moved on."""
    old, new = _FakePage(), _FakePage()
    old.session.frame_on_detach = b"old"
    shown = old
    sent = _Viewer()
    viewer = sent.screencast(lambda: cast("Page", shown), lambda: True, poll_seconds=0.01)
    async with _watching(viewer):
        await asyncio.wait_for(old.session.screencasting.wait(), _TIMEOUT)
        shown = new
        await asyncio.wait_for(new.session.screencasting.wait(), _TIMEOUT)
        assert ("detach", None) in old.session.sent
        await new.session.frame(1, b"new")
        assert await sent.frame() == b"new"


@pytest.mark.asyncio
async def test_viewer_is_told_the_loading_state_of_the_page_it_shows_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The viewer is told the loading state of the page it shows, and of no other.

    A frozen page shows as not loading, a report newer than a page's answer wins, a left page's report is ignored, and
    neither a page that closed before the viewer attached nor no page loads.
    """
    monkeypatch.setattr("mindroom.worker_computer.screencast._INPUT_SECONDS", 0.05)
    frozen, finishing, loading, closed, loading_again = (_FakePage() for _ in range(5))
    frozen.session.frozen = True
    finishing.session.ready_state = "interactive"
    finishing.session.stops_loading_before_answering = True
    loading.session.ready_state = loading_again.session.ready_state = "loading"
    closed.closed = True
    shown: _FakePage | None = frozen
    sent = _Viewer()
    viewer = sent.screencast(lambda: cast("Page | None", shown), lambda: True, poll_seconds=0.01)

    async def framed(fake: _FakePage) -> list[dict[str, Any]]:
        """Wait for a frame of the shown page, which follows what the viewer was told about it, and return loading."""
        await asyncio.wait_for(fake.session.screencasting.wait(), _TIMEOUT)
        await fake.session.frame(1, b"frame")
        assert await sent.frame() == b"frame"
        return [message for message in sent.messages if message["type"] == "loading"]

    async with _watching(viewer):
        assert await sent.message("loading") == _LOADED
        shown = finishing
        assert await framed(finishing) == []
        shown = loading
        assert await framed(loading) == [_LOADING]
        await sent.message("loading")
        frozen.session.handlers["Page.frameStoppedLoading"]({"frameId": "page"})
        assert await framed(loading) == []
        shown = closed
        assert await sent.message("loading") == _LOADED
        shown = loading_again
        assert await framed(loading_again) == [_LOADING]
        await sent.message("loading")
        shown = None
        assert await sent.message("loading") == _LOADED


@pytest.mark.asyncio
async def test_navigate_waits_longer_for_an_address_lookup_than_for_input_but_not_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lookup slower than the input bound still navigates; one slower than its own bound answers an error."""
    monkeypatch.setattr("mindroom.worker_computer.screencast._INPUT_SECONDS", 0.05)
    monkeypatch.setattr("mindroom.worker_computer.screencast._LOOKUP_SECONDS", 1.0)
    lookup_seconds = {"https://slow.example/": 0.3, "https://stalled.example/": 3.0}

    def slow_lookup(url: str, **_policy: bool) -> str:
        time.sleep(lookup_seconds[url])
        return url

    monkeypatch.setattr("mindroom.worker_computer.screencast.validate_server_fetch_url", slow_lookup)
    fake = _FakePage()
    sent = _Viewer()
    viewer = sent.screencast(lambda: cast("Page", fake), lambda: True)
    async with _watching(viewer):
        await asyncio.wait_for(fake.session.screencasting.wait(), _TIMEOUT)
        await viewer.receive(json.dumps({"type": "navigate", "url": "https://slow.example/"}))
        await asyncio.wait_for(viewer.receive(json.dumps({"type": "navigate", "url": "https://stalled.example/"})), 2)
        assert await sent.message("error") == _REFUSED
    navigated = [params for name, params in fake.session.sent if name == "Page.navigate"]
    assert navigated == [{"url": "https://slow.example/"}]


@pytest.mark.asyncio
async def test_a_page_frozen_by_its_script_holds_neither_input_nor_a_leaving_viewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Input to a page that never answers gives up, so the viewer keeps reading, and still detaches when it leaves."""
    monkeypatch.setattr("mindroom.worker_computer.screencast._INPUT_SECONDS", 0.05)
    fake = _FakePage()
    sent = _Viewer()
    viewer = sent.screencast(lambda: cast("Page", fake), lambda: True)
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


_HEADERS = {"X-Mindroom-Sandbox-Token": RUNNER_TOKEN}
_EXAMPLE = _location("https://example.org/", "Example")


@contextmanager
def _worker(tmp_path: Path, session: BrowserSession) -> Iterator[tuple[TestClient, WorkerComputerRuntime, str]]:
    """A worker app whose Computer runs the agent's browser session, its client, and the viewer's screencast URL."""
    runtime = WorkerComputerRuntime(FakeDisplay())

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await runtime.run_browser_call("binding", lambda _display: session, [], {})
        yield
        await runtime.close()

    app = FastAPI(lifespan=lifespan)
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    initialize_sandbox_runner_app(app, paths, config=Config(), runner_token=RUNNER_TOKEN)
    app.state.worker_computer = runtime
    app.include_router(router)
    with TestClient(app) as client:
        generation = client.get("/computer", headers=_HEADERS).json()["generation"]
        yield client, runtime, f"/computer/screencast?session_id=viewer&generation={generation}"


def _connected(websocket: WebSocketTestSession) -> None:
    """Take the page's location and loading state, which the viewer is sent on connect."""
    assert json.loads(websocket.receive_text()) == _EXAMPLE
    assert json.loads(websocket.receive_text()) == _LOADED


def test_worker_route_requires_the_runner_and_takes_input_only_under_the_control_lease(tmp_path: Path) -> None:
    """The worker serves the screencast like the desktop stream: runner token, stream lease, and control lease."""
    fake = _FakePage()
    session = BrowserSession(execute=unused, close=unused, page=lambda: cast("Page", fake))
    key = json.dumps({"type": "key", "event": "down", "key": "a", "code": "KeyA", "keyCode": 65})
    with _worker(tmp_path, session) as (client, runtime, url):
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(url, subprotocols=[SCREENCAST_PROTOCOL]):
            pass
        with client.websocket_connect(url, headers=dict(_HEADERS), subprotocols=[SCREENCAST_PROTOCOL]) as websocket:
            assert websocket.accepted_subprotocol == SCREENCAST_PROTOCOL
            _connected(websocket)
            websocket.send_text(key)
            control = {"session_id": "viewer", "action": "take"}
            assert client.post("/computer/control", headers=_HEADERS, json=control).status_code == 200
            websocket.send_text(key)
            websocket.send_text("{}")
            assert json.loads(websocket.receive_text())["type"] == "error"
            release = {"session_id": "viewer", "action": "release"}
            assert client.post("/computer/control", headers=_HEADERS, json=release).status_code == 200
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()
        keys = [
            (params["type"], params["key"])
            for name, params in fake.session.sent
            if name == "Input.dispatchKeyEvent" and params
        ]
        assert keys == [("keyDown", "a"), ("keyUp", "a")]
        assert fake.session.sent[-1] == ("detach", None)
        assert runtime.status()["controller_session_id"] is None
        with client.websocket_connect(url, headers=dict(_HEADERS), subprotocols=[SCREENCAST_PROTOCOL]) as websocket:
            _connected(websocket)
            websocket.send_bytes(b"binary input")
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()
        with client.websocket_connect(url, headers=dict(_HEADERS), subprotocols=[SCREENCAST_PROTOCOL]) as websocket:
            _connected(websocket)
            websocket.send_text(" " * (MAX_SCREENCAST_MESSAGE_BYTES + 1))
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()


@pytest.mark.parametrize("allowed", [False, True])
def test_viewer_opens_a_private_address_only_when_the_agents_browser_may(tmp_path: Path, allowed: bool) -> None:
    """A viewer's navigate follows the agent's browser tool setting, which reaches it through the worker's binding."""
    provider = select_browser_provider("browser", "browser_control", browser_tools, json.dumps)
    toolkit = BrowserTools(resolve_runtime_paths(storage_path=tmp_path), allow_private_networks=allowed)
    _key, bound = provider.bind(toolkit, ":99", tmp_path / "workspace", unused)
    fake = _FakePage()
    session = replace(bound, execute=unused, page=lambda: cast("Page", fake))
    private = {"type": "navigate", "url": "http://10.0.0.1/"}
    with (
        _worker(tmp_path, session) as (client, _runtime, url),
        client.websocket_connect(url, headers=dict(_HEADERS), subprotocols=[SCREENCAST_PROTOCOL]) as websocket,
    ):
        _connected(websocket)
        control = {"session_id": "viewer", "action": "take"}
        assert client.post("/computer/control", headers=_HEADERS, json=control).status_code == 200
        websocket.send_text(json.dumps(private))
        websocket.send_text("{}")
        if not allowed:
            assert json.loads(websocket.receive_text()) == _REFUSED
        assert json.loads(websocket.receive_text()) == _INVALID
    assert (("Page.navigate", {"url": "http://10.0.0.1/"}) in fake.session.sent) is allowed
