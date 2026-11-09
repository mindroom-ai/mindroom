"""A worker service's bridge to the agent's browser page, under the Computer's control lease."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import aiohttp
import pytest
import pytest_asyncio
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from mindroom.api import sandbox_runner_app
from mindroom.worker_computer.agent_browser import AgentBrowserBridge
from mindroom.worker_computer.protocol import BrowserSession
from mindroom.worker_computer.runtime import ComputerControlError, WorkerComputerRuntime
from tests.api.test_worker_services import _AUTH, _body, _call, _runner
from tests.test_worker_computer_runtime import FakeDisplay, unused

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from playwright.async_api import Page

_TIMEOUT = 10
_FORM = """<title>Form</title><input id=field><button id=go onclick="document.title = 'clicked'">Go</button>
<script>
window.released = [];
document.addEventListener("keyup", (event) => released.push(event.key));
document.addEventListener("mouseup", (event) => released.push("button " + event.button));
</script>"""


@dataclass
class _Agent:
    """The agent's browser as the Computer holds it: the page it shows, or none."""

    shown: Page | None
    runtime: WorkerComputerRuntime = field(default_factory=lambda: WorkerComputerRuntime(FakeDisplay()))

    def session(self) -> BrowserSession:
        return BrowserSession(execute=unused, close=unused, page=lambda: self.shown)

    async def browse(self) -> None:
        """Make one browser call, the way the agent's browser tool does."""
        await self.runtime.run_browser_call("binding", lambda _display: self.session(), [], {})


class _Service:
    """A worker service speaking CDP through one bridge."""

    def __init__(self, runtime: WorkerComputerRuntime) -> None:
        self.bridge = AgentBrowserBridge(runtime, self._arrive, poll_seconds=0.05)
        self.messages: list[dict[str, Any]] = []
        self._arrived = asyncio.Event()
        self._last_id = 0

    async def _arrive(self, text: str) -> None:
        self.messages.append(json.loads(text))
        self._arrived.set()

    async def _take(self, matches: Any) -> dict[str, Any]:  # noqa: ANN401 - a predicate over JSON messages
        async with asyncio.timeout(_TIMEOUT):
            while True:
                for index, message in enumerate(self.messages):
                    if matches(message):
                        return self.messages.pop(index)
                self._arrived.clear()
                await self._arrived.wait()

    def send(self, method: str, params: dict[str, Any] | None = None) -> int:
        self._last_id += 1
        self.bridge.receive(json.dumps({"id": self._last_id, "method": method, "params": params or {}}))
        return self._last_id

    async def answer(self, command_id: int) -> dict[str, Any]:
        return await self._take(lambda message: message.get("id") == command_id)

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one command and return its result, failing on an error answer."""
        answer = await self.answer(self.send(method, params))
        assert "result" in answer, answer
        return answer["result"]

    async def refused(self, method: str, params: dict[str, Any] | None = None) -> str:
        """Send one command and return the message of its error answer."""
        answer = await self.answer(self.send(method, params))
        assert "error" in answer, answer
        return answer["error"]["message"]

    async def event(self, method: str) -> dict[str, Any]:
        return (await self._take(lambda message: message.get("method") == method))["params"]

    def clear_events(self, method: str) -> None:
        self.messages = [message for message in self.messages if message.get("method") != method]


@asynccontextmanager
async def _connected(runtime: WorkerComputerRuntime) -> AsyncIterator[_Service]:
    service = _Service(runtime)
    tasks = [asyncio.create_task(service.bridge.follow()), asyncio.create_task(service.bridge.send())]
    try:
        yield service
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await service.bridge.close()


@pytest_asyncio.fixture
async def agent() -> AsyncIterator[_Agent]:
    """The agent's browser on a page of a real headless Chromium, in a running Computer."""
    executable = os.environ.get("MINDROOM_TEST_BROWSER_EXECUTABLE") or shutil.which("chromium")
    if executable is None:
        pytest.skip("Chromium required for the agent's browser bridge")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=executable, headless=True)
        try:
            context = await browser.new_context(viewport={"width": 800, "height": 600})
            page = await context.new_page()
            await page.set_content(_FORM)
            agent = _Agent(page)
            await agent.browse()
            yield agent
            await agent.runtime.close()
        finally:
            await browser.close()


@pytest.fixture
def site() -> Iterator[str]:
    """A local web server, like one the agent starts from its shell."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<title>Site</title><p>served")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


async def _click(service: _Service, page: Page, selector: str) -> None:
    box = await page.locator(selector).bounding_box()
    assert box is not None
    point = {"x": box["x"] + 5, "y": box["y"] + 5, "button": "left", "clickCount": 1}
    await service.call("Input.dispatchMouseEvent", {"type": "mousePressed", **point})
    await service.call("Input.dispatchMouseEvent", {"type": "mouseReleased", **point})


@pytest.mark.asyncio
async def test_a_service_watches_the_agents_page_with_the_allowed_commands_only(agent: _Agent) -> None:
    """Frames and the page's address flow without control; acting answers an error and does nothing."""
    page = cast("Page", agent.shown)
    async with _connected(agent.runtime) as service:
        assert await service.event("MindRoom.page") == {"page": True}
        await service.call("Page.startScreencast", {"format": "jpeg", "quality": 80})
        frame = await service.event("Page.screencastFrame")
        assert base64.b64decode(frame["data"]).startswith(b"\xff\xd8")
        await service.call("Page.screencastFrameAck", {"sessionId": frame["sessionId"]})
        info = (await service.call("Target.getTargetInfo"))["targetInfo"]
        assert (info["url"], info["title"]) == ("about:blank", "Form")
        await service.call("Page.enable")
        assert "entries" in await service.call("Page.getNavigationHistory")
        assert "cssLayoutViewport" in await service.call("Page.getLayoutMetrics")

        box = await page.locator("#go").bounding_box()
        assert box is not None
        press = {"type": "mousePressed", "x": box["x"] + 5, "y": box["y"] + 5, "button": "left", "clickCount": 1}
        assert "control" in await service.refused("Input.dispatchMouseEvent", press)
        assert "control" in await service.refused("Input.insertText", {"text": "no"})
        assert "control" in await service.refused("Page.navigate", {"url": "about:blank"})
        assert "control" in await service.refused("Emulation.setDeviceMetricsOverride", {"width": 1})

        expression = {"expression": "document.title = 'evaluated'"}
        assert "not available" in await service.refused("Runtime.evaluate", expression)
        assert "not available" in await service.refused("Network.getCookies")
        assert "not available" in await service.refused("Target.createTarget", {"url": "about:blank"})
        assert "not available" in await service.refused("Browser.close")
        # Another target's address is not this page's to show.
        assert await service.refused("Target.getTargetInfo", {"targetId": "other"})
        assert await page.title() == "Form"
        assert await page.evaluate("released") == []


@pytest.mark.asyncio
async def test_a_service_in_control_acts_on_the_page_while_the_agents_browser_calls_are_refused(
    agent: _Agent,
    site: str,
) -> None:
    """Input, navigation and viewport act under control; script and other schemes stay refused."""
    page = cast("Page", agent.shown)
    async with _connected(agent.runtime) as service:
        assert await service.event("MindRoom.page") == {"page": True}
        await service.call("MindRoom.takeControl")
        assert await service.event("MindRoom.control") == {"held": True}
        with pytest.raises(ComputerControlError, match="under user control"):
            await agent.browse()

        await _click(service, page, "#go")
        assert await page.title() == "clicked"
        await _click(service, page, "#field")
        await service.call("Input.insertText", {"text": "hi"})
        key = {"key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "text": "a"}
        await service.call("Input.dispatchKeyEvent", {"type": "keyDown", **key})
        await service.call("Input.dispatchKeyEvent", {"type": "keyUp", **key})
        assert await page.input_value("#field") == "hia"
        metrics = {"width": 400, "height": 300, "deviceScaleFactor": 1, "mobile": False}
        await service.call("Emulation.setDeviceMetricsOverride", metrics)
        await page.wait_for_function("innerWidth === 400", timeout=_TIMEOUT * 1000)
        await service.call("Emulation.clearDeviceMetricsOverride")
        await page.wait_for_function("innerWidth === 800", timeout=_TIMEOUT * 1000)

        script = {"ignoreCache": False, "scriptToEvaluateOnLoad": "document.title = 'evaluated'"}
        assert await service.refused("Page.reload", script)
        for url in (
            "javascript:document.title='evaluated'",
            "file:///etc/passwd",
            "data:text/html,x",
            "chrome://version",
        ):
            assert await service.refused("Page.navigate", {"url": url})
        assert await service.refused("Page.navigate", {"url": "http://169.254.169.254/"})
        assert await page.title() == "clicked"
        # Numbers JSON cannot carry would end the browser's driver connection, so they never reach it.
        for command_id, number in ((900, "NaN"), (901, "-Infinity"), (902, "1e999")):
            params = f'{{"type": "mouseMoved", "x": {number}, "y": 1}}'
            service.bridge.receive(f'{{"id": {command_id}, "method": "Input.dispatchMouseEvent", "params": {params}}}')
            assert (await service.answer(command_id))["error"]["message"] == "Numbers must be finite."

        await service.call("Page.navigate", {"url": site})
        await page.wait_for_function("document.title === 'Site'", timeout=_TIMEOUT * 1000)
        await service.call("Page.navigate", {"url": "about:blank"})
        await page.wait_for_url("about:blank", timeout=_TIMEOUT * 1000)

        await service.call("MindRoom.releaseControl")
        assert await service.event("MindRoom.control") == {"held": False}
        await agent.browse()
        assert "control" in await service.refused("Input.insertText", {"text": "no"})


@pytest.mark.asyncio
async def test_the_bridge_follows_the_agent_to_another_tab_and_to_none(agent: _Agent) -> None:
    """When the agent shows another page the service hears of it, and its new screencast shows that page."""
    first = cast("Page", agent.shown)
    second = await first.context.new_page()
    await second.set_content("<title>Second</title><p>second page")
    async with _connected(agent.runtime) as service:
        assert await service.event("MindRoom.page") == {"page": True}
        await service.call("Page.startScreencast", {"format": "jpeg"})
        await service.event("Page.screencastFrame")
        await second.bring_to_front()
        agent.shown = second
        assert await service.event("MindRoom.page") == {"page": True}
        service.clear_events("Page.screencastFrame")
        assert (await service.call("Target.getTargetInfo"))["targetInfo"]["title"] == "Second"
        await service.call("Page.startScreencast", {"format": "jpeg"})
        assert base64.b64decode((await service.event("Page.screencastFrame"))["data"]).startswith(b"\xff\xd8")
        agent.shown = None
        assert await service.event("MindRoom.page") == {"page": False}
        assert "no page" in await service.refused("Target.getTargetInfo")


@pytest.mark.asyncio
async def test_a_computer_stop_ends_the_bridges_lease_and_its_page(agent: _Agent) -> None:
    """A stop ends control as it does for the desktop's viewers; the bridge stays and reports no page."""
    async with _connected(agent.runtime) as service:
        assert await service.event("MindRoom.page") == {"page": True}
        await service.call("MindRoom.takeControl")
        assert await service.event("MindRoom.control") == {"held": True}
        await agent.runtime.stop()
        assert await service.event("MindRoom.control") == {"held": False}
        assert await service.event("MindRoom.page") == {"page": False}
        assert "stopped" in await service.refused("MindRoom.takeControl")
        await agent.browse()
        assert await service.event("MindRoom.page") == {"page": True}
        await service.call("MindRoom.takeControl")
        assert await service.event("MindRoom.control") == {"held": True}


@pytest.mark.asyncio
async def test_control_is_exclusive_with_the_desktops_viewers(agent: _Agent) -> None:
    """A viewer holding the Computer keeps the bridge out, and the bridge holding it keeps viewers out."""
    runtime = agent.runtime
    generation = runtime.status()["generation"]
    viewer = await runtime.attach_stream("viewer", generation)
    await runtime.take_control("viewer", generation=generation)
    async with _connected(runtime) as service:
        assert "another user session" in await service.refused("MindRoom.takeControl")
        await runtime.release_control("viewer")
        await service.call("MindRoom.takeControl")
        viewer = await runtime.attach_stream("viewer", generation)
        with pytest.raises(ComputerControlError, match="another user session"):
            await runtime.take_control("viewer", generation=generation)
    assert runtime.status()["controller_session_id"] is None
    await runtime.detach_stream("viewer", viewer)


@pytest.mark.asyncio
async def test_leaving_releases_the_keys_and_buttons_the_service_held_and_the_lease(agent: _Agent) -> None:
    """A service that leaves mid-press leaves no key or button stuck, and the agent can browse again."""
    page = cast("Page", agent.shown)
    async with _connected(agent.runtime) as service:
        assert await service.event("MindRoom.page") == {"page": True}
        await service.call("MindRoom.takeControl")
        await service.call("Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": "Shift", "code": "ShiftLeft"})
        await service.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": 1, "y": 1, "button": "right"})
    assert sorted(await page.evaluate("released")) == ["Shift", "button 2"]
    assert agent.runtime.status()["controller_session_id"] is None
    await agent.browse()


@pytest.mark.asyncio
async def test_releasing_control_releases_what_the_service_held(agent: _Agent) -> None:
    """The agent never resumes on a page that still sees a key down from the service."""
    page = cast("Page", agent.shown)
    async with _connected(agent.runtime) as service:
        assert await service.event("MindRoom.page") == {"page": True}
        await service.call("MindRoom.takeControl")
        await service.call("Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": "Control", "code": "ControlLeft"})
        await service.call("MindRoom.releaseControl")
        assert await page.evaluate("released") == ["Control"]


@pytest.mark.asyncio
async def test_a_pending_navigation_holds_neither_other_commands_nor_a_leaving_service(agent: _Agent) -> None:
    """A server that never answers keeps only its own navigation waiting; release and leaving still complete."""
    stalled = await asyncio.start_server(lambda _reader, _writer: None, "127.0.0.1", 0)
    port = stalled.sockets[0].getsockname()[1]
    try:
        async with _connected(agent.runtime) as service:
            assert await service.event("MindRoom.page") == {"page": True}
            await service.call("MindRoom.takeControl")
            assert await service.event("MindRoom.control") == {"held": True}
            navigation = service.send("Page.navigate", {"url": f"http://127.0.0.1:{port}/"})
            assert await service.call("Target.getTargetInfo")
            await service.call("MindRoom.releaseControl")
            assert await service.event("MindRoom.control") == {"held": False}
            async with asyncio.timeout(_TIMEOUT):
                await service.bridge.close()
            assert navigation not in {message.get("id") for message in service.messages}
        await agent.browse()
    finally:
        stalled.close()


class _ClosingPage:
    """A page that closes as the bridge attaches to it, while the agent's browser still names it."""

    def __init__(self) -> None:
        self.context = self
        self.attempts = 0
        self.retried = asyncio.Event()

    async def new_cdp_session(self, _page: object) -> None:
        self.attempts += 1
        if self.attempts >= 3:
            self.retried.set()
        msg = "Target page, context or browser has been closed"
        raise PlaywrightError(msg)


@pytest.mark.asyncio
async def test_a_page_the_bridge_cannot_attach_to_reads_as_no_page_once() -> None:
    """The bridge tries again at each poll, and the service hears that there is no page once."""
    closing = _ClosingPage()
    agent = _Agent(cast("Page", closing))
    await agent.browse()
    async with _connected(agent.runtime) as service:
        assert await service.event("MindRoom.page") == {"page": False}
        await asyncio.wait_for(closing.retried.wait(), _TIMEOUT)
        assert [message for message in service.messages if message.get("method") == "MindRoom.page"] == []
    await agent.runtime.close()


class _FakeSession:
    """A CDP session whose events the test delivers by hand; one Chromium had in flight arrives during the detach."""

    def __init__(self, late_frame: str | None = None) -> None:
        self.handlers: dict[str, Any] = {}
        self.late_frame = late_frame

    def on(self, event: str, handler: object) -> None:
        self.handlers[event] = handler

    async def send(self, _method: str, _params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {}

    async def detach(self) -> None:
        if self.late_frame is not None:
            self.frame(self.late_frame)

    def frame(self, data: str) -> None:
        self.handlers["Page.screencastFrame"]({"data": data})


class _FakePage:
    def __init__(self, session: _FakeSession) -> None:
        self.context = self
        self.session = session

    async def new_cdp_session(self, _page: object) -> _FakeSession:
        return self.session


@pytest.mark.asyncio
async def test_an_event_the_left_page_delivers_never_reaches_the_service() -> None:
    """Once the bridge moves to the agent's next page, a late frame of the page it left is dropped."""
    old, new = _FakePage(_FakeSession(late_frame="old")), _FakePage(_FakeSession())
    agent = _Agent(cast("Page", old))
    await agent.browse()
    async with _connected(agent.runtime) as service:
        assert await service.event("MindRoom.page") == {"page": True}
        agent.shown = cast("Page", new)
        assert await service.event("MindRoom.page") == {"page": True}
        new.session.frame("new")
        assert await service.event("Page.screencastFrame") == {"data": "new"}
    await agent.runtime.close()


_SECRET_HEADER = "X-MindRoom-Service-Secret"  # noqa: S105 - a header name
# A service that records its secret, then exits at once.
_BRIEF = """
import json, sys
with open("service.json", "w") as record:
    json.dump({"secret": sys.stdin.readline().strip()}, record)
"""


def _bridge_url(pid: object) -> str:
    """The bridge's address, as the runner hands it to the service process ``pid`` in its environment."""
    environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    (url,) = [entry.split(b"=", 1)[1].decode() for entry in environ if entry.startswith(b"MINDROOM_AGENT_BROWSER_URL=")]
    return url


async def _refused(url: str, headers: dict[str, str]) -> bool:
    async with aiohttp.ClientSession() as client:
        try:
            async with client.ws_connect(url, headers=headers):
                return False
        except aiohttp.WSServerHandshakeError:
            return True


@pytest.mark.asyncio
async def test_only_the_service_s_own_secret_opens_its_bridge_and_opening_it_starts_the_computer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither the runner token, another service's secret, nor a stopped or exited instance's secret opens it."""
    runtime = WorkerComputerRuntime(FakeDisplay())
    monkeypatch.setattr(sandbox_runner_app.app.state, "worker_computer", runtime, raising=False)
    async with _runner(tmp_path) as runner:
        (runner.plugin_root / "brief.py").write_text(_BRIEF)
        await _call("POST", runner.url("other"), _body(), _AUTH)
        other_secret = str(runner.record()["secret"])
        await _call("POST", runner.url(), _body(), _AUTH)
        record = runner.record()
        url = _bridge_url(record["pid"])
        assert url == f"ws://127.0.0.1:{runner.port}/api/sandbox-runner/plugin-services/demo/hello/agent-browser"
        for headers in ({}, _AUTH, {_SECRET_HEADER: other_secret}, {_SECRET_HEADER: f"{record['secret']}x"}):
            assert await _refused(url, headers)
        assert runtime.status()["state"] == "stopped"

        headers = {_SECRET_HEADER: str(record["secret"])}
        async with aiohttp.ClientSession() as client, client.ws_connect(url, headers=headers) as bridge:
            assert await bridge.receive_json() == {"method": "MindRoom.page", "params": {"page": False}}
            assert runtime.status()["state"] == "ready"
            # The agent's browser has no page yet, and a service that shows a browser of its own still takes control.
            await bridge.send_json({"id": 1, "method": "MindRoom.takeControl"})
            assert await bridge.receive_json() == {"method": "MindRoom.control", "params": {"held": True}}
            assert await bridge.receive_json() == {"id": 1, "result": {}}
            assert runtime.status()["controller_session_id"] is not None
            await bridge.send_bytes(b"binary")
            assert (await bridge.receive()).type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}
        assert runtime.status()["controller_session_id"] is None

        await _call("DELETE", runner.url(), None, _AUTH)
        assert await _refused(url, headers)
        _, brief = await _call("POST", runner.url("brief"), _body("brief.py"), _AUTH)
        assert brief["state"] == "exited"
        brief_secret = {_SECRET_HEADER: str(runner.record()["secret"])}
        assert await _refused(url.replace("/hello/", "/brief/"), brief_secret)


@pytest.mark.asyncio
async def test_a_worker_without_a_computer_refuses_the_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A service of a worker that runs no Computer has no agent's browser to reach."""
    monkeypatch.setattr(sandbox_runner_app.app.state, "worker_computer", None, raising=False)
    async with _runner(tmp_path) as runner:
        await _call("POST", runner.url(), _body(), _AUTH)
        record = runner.record()
        assert await _refused(_bridge_url(record["pid"]), {_SECRET_HEADER: str(record["secret"])})
