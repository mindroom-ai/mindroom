"""A dedicated worker's runner relays the primary's requests and WebSockets to apps on its loopback, and nothing else."""

from __future__ import annotations

import asyncio
import gc
import socket
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING

import aiohttp
import pytest
import uvicorn
from aiohttp import web
from aiohttp.test_utils import TestServer
from yarl import URL

from mindroom.api import sandbox_runner_app
from mindroom.api.sandbox_runner import initialize_sandbox_runner_app
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

# The runner's refusals carry this header, so a caller tells them from an app's answers.
_REFUSED = "x-mindroom-relay-refused"
_TOKEN = "runner-secret"  # noqa: S105 - isolated test credential
_AUTH = {"X-Mindroom-Sandbox-Token": _TOKEN}
_CLOSE_CODES = web.AppKey("close_codes", asyncio.Queue[int | None])


class _Runner:
    def __init__(self, port: int) -> None:
        self.port = port
        self.root = f"http://127.0.0.1:{port}/api/sandbox-runner/ports"


@asynccontextmanager
async def _runner(tmp_path: Path, *, dedicated: bool = True) -> AsyncIterator[_Runner]:
    """Serve the sandbox runner app over real TCP, as a dedicated worker's runner unless told otherwise."""
    env = {"MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": "v1:tenant:shared:coder"} if dedicated else {}
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env=env)
    initialize_sandbox_runner_app(sandbox_runner_app.app, paths, config=Config(), runner_token=_TOKEN)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(
        uvicorn.Config(sandbox_runner_app.app, lifespan="off", ws="websockets-sansio", log_level="error"),
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
        yield _Runner(listener.getsockname()[1])
    finally:
        server.should_exit = True
        await serving
        listener.close()


async def _echo(request: web.Request) -> web.Response:
    """Answer with what the app received, two cookies, and a hop-by-hop header the relay must drop.

    ``status`` sets the answer's status, ``location`` its Location header, and ``gzip`` compresses it.
    """
    response = web.json_response(
        {
            "method": request.method,
            "path": request.raw_path,
            "headers": [[name.lower(), value] for name, value in request.headers.items()],
            "body": (await request.read()).decode(),
        },
        status=int(request.query.get("status", "200")),
    )
    response.headers.add("Set-Cookie", "a=1")
    response.headers.add("Set-Cookie", "b=2")
    response.headers["Proxy-Authenticate"] = "Basic"
    if "location" in request.query:
        response.headers["Location"] = request.query["location"]
    if "gzip" in request.query:
        response.enable_compression(web.ContentCoding.gzip)
    return response


async def _socket(request: web.Request) -> web.WebSocketResponse:
    """Greet with the handshake headers the app received and echo each message.

    "big" asks for a 5 MiB message, "bye" closes with 4001, and the client's own close code is recorded.
    """
    websocket = web.WebSocketResponse(protocols=("chat",))
    await websocket.prepare(request)
    await websocket.send_json({name.lower(): value for name, value in request.headers.items()})
    async for message in websocket:
        if message.type == aiohttp.WSMsgType.TEXT and message.data == "bye":
            await websocket.close(code=4001)
        elif message.type == aiohttp.WSMsgType.TEXT and message.data == "big":
            await websocket.send_bytes(b"x" * 5 * 1024 * 1024)
        elif message.type == aiohttp.WSMsgType.TEXT:
            await websocket.send_str(message.data)
        elif message.type == aiohttp.WSMsgType.BINARY:
            await websocket.send_bytes(message.data)
    request.app[_CLOSE_CODES].put_nowait(websocket.close_code)
    return websocket


@asynccontextmanager
async def _app(
    host: str = "127.0.0.1",
    *,
    released: asyncio.Event | None = None,
    close_codes: asyncio.Queue[int | None] | None = None,
) -> AsyncIterator[int]:
    """Serve the test app on ``host`` and yield its port."""

    async def stream(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b"first;")
        assert released is not None
        await released.wait()
        await response.write(b"second")
        return response

    app = web.Application()
    app[_CLOSE_CODES] = close_codes if close_codes is not None else asyncio.Queue()
    app.router.add_get("/socket", _socket)
    app.router.add_get("/stream", stream)
    app.router.add_route("*", "/{tail:.*}", _echo)
    async with TestServer(app, host=host) as server:
        assert server.port is not None
        yield server.port


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.asyncio
async def test_relay_passes_a_request_and_its_answer_without_the_runner_token(tmp_path: Path) -> None:
    """Method, encoded path, query, body, and duplicate answer headers pass; the token and hop-by-hop headers do not."""
    async with (
        _app() as port,
        _runner(tmp_path) as runner,
        aiohttp.ClientSession() as client,
        client.post(
            URL(f"{runner.root}/{port}/a%3Fb/../c%2Fd?x=%20&y=1+2", encoded=True),
            data=b"payload",
            headers={**_AUTH, "Cookie": "session=app", "Connection": "keep-alive, X-Private", "X-Private": "hop"},
            # Headers the caller leaves out stay out: the relay adds none of its own.
            skip_auto_headers=("User-Agent", "Accept-Encoding", "Content-Type"),
        ) as response,
    ):
        assert response.status == 200
        assert _REFUSED not in response.headers
        assert response.headers.getall("Set-Cookie") == ["a=1", "b=2"]
        assert "Proxy-Authenticate" not in response.headers
        seen = await response.json()
    headers = dict(seen.pop("headers"))
    assert seen == {"method": "POST", "path": "/a%3Fb/../c%2Fd?x=%20&y=1+2", "body": "payload"}
    assert headers["host"] == f"127.0.0.1:{port}"
    assert headers["cookie"] == "session=app"
    assert headers["content-length"] == "7"
    assert {"x-mindroom-sandbox-token", "x-private", "user-agent", "accept-encoding", "content-type"}.isdisjoint(
        headers,
    )


@pytest.mark.asyncio
async def test_relay_streams_an_answer_as_the_app_writes_it(tmp_path: Path) -> None:
    """The first part of an answer arrives before the app has written the rest."""
    released = asyncio.Event()
    async with (
        _app(released=released) as port,
        _runner(tmp_path) as runner,
        aiohttp.ClientSession() as client,
        client.get(f"{runner.root}/{port}/stream", headers=_AUTH) as response,
    ):
        async with asyncio.timeout(5):
            assert await response.content.readexactly(6) == b"first;"
        released.set()
        assert await response.content.read() == b"second"


@pytest.mark.asyncio
async def test_relay_passes_an_apps_answer_as_the_app_sent_it(tmp_path: Path) -> None:
    """An app's own 502 stays unmarked, a redirect is not followed, a compressed answer stays compressed, and HEAD gets no body."""
    async with _app() as port, _runner(tmp_path) as runner, aiohttp.ClientSession() as client:
        async with client.get(f"{runner.root}/{port}/?status=502", headers=_AUTH) as response:
            assert response.status == 502
            assert _REFUSED not in response.headers
            headers = dict((await response.json())["headers"])
            # A request without a body goes out without one.
            assert {"content-length", "transfer-encoding"}.isdisjoint(headers)
        async with client.get(
            f"{runner.root}/{port}/?status=302&location=/elsewhere",
            headers=_AUTH,
            allow_redirects=False,
        ) as response:
            assert response.status == 302
            assert response.headers["Location"] == "/elsewhere"
        async with client.get(f"{runner.root}/{port}/zipped?gzip=1", headers=_AUTH) as response:
            assert response.headers["Content-Encoding"] == "gzip"
            assert (await response.json())["path"] == "/zipped?gzip=1"
        async with client.head(f"{runner.root}/{port}/", headers=_AUTH) as response:
            assert response.status == 200
            assert int(response.headers["Content-Length"]) > 0
            assert await response.read() == b""


@pytest.mark.asyncio
async def test_relay_reaches_an_app_listening_only_on_ipv6_loopback(tmp_path: Path) -> None:
    """An app on ``[::1]`` alone is listed and reached there."""
    async with AsyncExitStack() as stack:
        try:
            port = await stack.enter_async_context(_app("::1"))
        except OSError:
            pytest.skip("No IPv6 loopback on this host.")
        runner = await stack.enter_async_context(_runner(tmp_path))
        client = await stack.enter_async_context(aiohttp.ClientSession())
        async with client.get(runner.root, headers=_AUTH) as response:
            assert port in await response.json()
        async with client.get(f"{runner.root}/{port}/hello", headers=_AUTH) as response:
            assert response.status == 200
            assert (await response.json())["path"] == "/hello"


@pytest.mark.asyncio
async def test_port_list_shows_loopback_apps_and_leaves_out_the_runner_and_other_addresses(tmp_path: Path) -> None:
    """The list holds apps on loopback or every address, never the runner, a client socket, or another address."""
    async with (
        _app() as port,
        _app("0.0.0.0") as everywhere,  # noqa: S104 - a server on every address is reached at loopback too
        _app("127.0.0.2") as elsewhere,
        _runner(tmp_path) as runner,
    ):
        _, connected = await asyncio.open_connection("127.0.0.1", port)
        try:
            async with aiohttp.ClientSession() as client, client.get(runner.root, headers=_AUTH) as response:
                assert response.status == 200
                ports = await response.json()
        finally:
            connected.close()
            await connected.wait_closed()
    assert {port, everywhere} <= set(ports)
    assert {runner.port, elsewhere, connected.get_extra_info("sockname")[1]}.isdisjoint(ports)
    assert ports == sorted(ports)


@pytest.mark.asyncio
async def test_relay_refuses_a_port_no_app_listens_on_at_loopback(tmp_path: Path) -> None:
    """A closed port, the runner's own, and a server only on another address are marked refusals."""
    async with _app("127.0.0.2") as elsewhere, _runner(tmp_path) as runner, aiohttp.ClientSession() as client:
        for port in (_free_port(), runner.port, elsewhere):
            async with client.get(f"{runner.root}/{port}/", headers=_AUTH) as response:
                assert response.status == 502
                assert response.headers[_REFUSED] == "1"
                assert await response.json() == {"detail": f"No app listens on port {port} at this worker's loopback."}


@pytest.mark.asyncio
async def test_shared_runner_relays_nothing(tmp_path: Path) -> None:
    """The shared runner refuses to list or relay, since its loopback is not one worker's."""
    async with _app() as port, _runner(tmp_path, dedicated=False) as runner, aiohttp.ClientSession() as client:
        for url in (runner.root, f"{runner.root}/{port}/"):
            async with client.get(url, headers=_AUTH) as response:
                assert response.status == 403
                assert response.headers[_REFUSED] == "1"
        with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
            await client.ws_connect(f"{runner.root}/{port}/socket", headers=_AUTH)
        assert refused.value.status == 403


@pytest.mark.asyncio
async def test_relay_requires_the_runner_token(tmp_path: Path) -> None:
    """Without the runner's token, nothing is listed or relayed."""
    async with _app() as port, _runner(tmp_path) as runner, aiohttp.ClientSession() as client:
        for url in (runner.root, f"{runner.root}/{port}/"):
            async with client.get(url, headers={"X-Mindroom-Sandbox-Token": "wrong"}) as response:
                assert response.status == 401
        with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
            await client.ws_connect(f"{runner.root}/{port}/socket", headers={"X-Mindroom-Sandbox-Token": "wrong"})
        assert refused.value.status == 403


@pytest.mark.asyncio
async def test_relay_passes_a_websocket_both_ways_without_the_runner_token(tmp_path: Path) -> None:
    """Messages up to 16 MiB, the subprotocol, and the app's close code pass; the token does not."""
    async with (
        _app() as port,
        _runner(tmp_path) as runner,
        aiohttp.ClientSession() as client,
        client.ws_connect(
            f"{runner.root}/{port}/socket?room=1",
            headers={**_AUTH, "Cookie": "session=app"},
            protocols=("chat",),
            max_msg_size=16 * 1024 * 1024,
        ) as websocket,
    ):
        assert websocket.protocol == "chat"
        handshake = await websocket.receive_json()
        assert handshake["cookie"] == "session=app"
        assert handshake["host"] == f"127.0.0.1:{port}"
        assert "x-mindroom-sandbox-token" not in handshake
        await websocket.send_str("hello")
        assert await websocket.receive_str() == "hello"
        await websocket.send_bytes(b"\x00\x01")
        assert await websocket.receive_bytes() == b"\x00\x01"
        await websocket.send_str("big")
        assert len(await websocket.receive_bytes()) == 5 * 1024 * 1024
        await websocket.send_str("bye")
        assert (await websocket.receive()).type == aiohttp.WSMsgType.CLOSE
        assert websocket.close_code == 4001


@pytest.mark.asyncio
async def test_relay_passes_the_callers_close_code_to_the_app(tmp_path: Path) -> None:
    """A caller that closes with an application code closes the app's socket with the same code."""
    close_codes: asyncio.Queue[int | None] = asyncio.Queue()
    async with _app(close_codes=close_codes) as port, _runner(tmp_path) as runner, aiohttp.ClientSession() as client:
        async with client.ws_connect(f"{runner.root}/{port}/socket", headers=_AUTH) as websocket:
            await websocket.receive_json()
            await websocket.close(code=4002)
        async with asyncio.timeout(5):
            assert await close_codes.get() == 4002


@pytest.mark.asyncio
async def test_relay_refuses_a_websocket_nothing_answers(tmp_path: Path) -> None:
    """A WebSocket to a closed port, or to an app path that takes none, is refused at the handshake."""
    async with _app() as port, _runner(tmp_path) as runner, aiohttp.ClientSession() as client:
        for url in (f"{runner.root}/{_free_port()}/socket", f"{runner.root}/{port}/plain"):
            with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                await client.ws_connect(url, headers=_AUTH)
            assert refused.value.status == 403


@pytest.mark.asyncio
async def test_relay_closes_its_app_connection_after_each_answer(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each relayed answer closes its connection to the app, so none is left for garbage collection to report."""
    async with _app() as port, _runner(tmp_path) as runner, aiohttp.ClientSession() as client:
        for _ in range(3):
            async with client.get(f"{runner.root}/{port}/", headers=_AUTH) as response:
                await response.read()
    gc.collect()
    assert "Unclosed" not in caplog.text
