"""A dedicated worker's runner relays the primary's requests and WebSockets to the apps its worker runs, and nothing else."""

from __future__ import annotations

import asyncio
import gc
import os
import shlex
import signal
import socket
import sys
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from subprocess import DEVNULL, PIPE
from typing import TYPE_CHECKING

import aiohttp
import pytest
import uvicorn
from aiohttp import web
from yarl import URL

from mindroom.api import sandbox_runner_app
from mindroom.api.sandbox_runner import initialize_sandbox_runner_app
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.previews import listeners

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="The port list reads listening sockets from /proc/net."),
    # The runner serves WebSockets with uvicorn's default implementation, the deprecated websockets.legacy one.
    pytest.mark.filterwarnings(r"ignore:websockets\.legacy is deprecated.*:DeprecationWarning:websockets\.legacy"),
    pytest.mark.filterwarnings(
        r"ignore:websockets\.server\.WebSocketServerProtocol is deprecated:DeprecationWarning:uvicorn\.protocols\.websockets\.websockets_impl",
    ),
    pytest.mark.filterwarnings(
        r"ignore:remove second argument of ws_handler:DeprecationWarning:websockets\.legacy\.server",
    ),
]

# The runner's refusals carry this header, so a caller tells them from an app's answers.
_REFUSED = "x-mindroom-relay-refused"
_TOKEN = "runner-secret"  # noqa: S105 - isolated test credential
_AUTH = {"X-Mindroom-Sandbox-Token": _TOKEN}
_CLOSE_CODES = web.AppKey("close_codes", asyncio.Queue[int | None])


@dataclass(frozen=True)
class _Runner:
    port: int

    @property
    def root(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/sandbox-runner/ports"


@asynccontextmanager
async def _runner(
    tmp_path: Path,
    *,
    dedicated: bool = True,
    env: dict[str, str] | None = None,
) -> AsyncIterator[_Runner]:
    """Serve the sandbox runner app over real TCP with ``env``, as a dedicated worker's runner unless told otherwise."""
    env = {**({"MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": "v1:tenant:shared:coder"} if dedicated else {}), **(env or {})}
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env=env)
    initialize_sandbox_runner_app(sandbox_runner_app.app, paths, config=Config(), runner_token=_TOKEN)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(
        uvicorn.Config(sandbox_runner_app.app, lifespan="off", log_level="error"),
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
    """Answer with what the app received, two cookies, and a hop-by-hop header and a refusal marker the relay must drop.

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
    # Only the runner marks its refusals; an app's own marker never reaches the caller.
    response.headers[_REFUSED] = "1"
    if "location" in request.query:
        response.headers["Location"] = request.query["location"]
    if "gzip" in request.query:
        response.enable_compression(web.ContentCoding.gzip)
    return response


async def _socket(request: web.Request) -> web.WebSocketResponse:  # noqa: C901 - one branch per scripted command
    """Greet with the path and handshake headers the app received, and echo each message.

    "big" asks for a 5 MiB message, "flood" for messages until the socket closes, "bye" closes with 4001, "restart"
    with 1012, "quiet" without a code, "crash" drops the connection without closing, and the client's own close code
    is recorded.
    """
    websocket = web.WebSocketResponse(protocols=("chat",))
    await websocket.prepare(request)
    await websocket.send_json(
        {"path": request.raw_path, "headers": {name.lower(): value for name, value in request.headers.items()}},
    )
    flooding: list[asyncio.Task[None]] = []

    async def flood() -> None:
        with suppress(ConnectionError):
            while not websocket.closed:
                await websocket.send_str("x" * 1024)

    async for message in websocket:
        if message.type == aiohttp.WSMsgType.BINARY:
            await websocket.send_bytes(message.data)
            continue
        match message.data:
            case "bye":
                await websocket.close(code=4001)
            case "restart":
                await websocket.close(code=1012)
            case "quiet":
                # A close frame without a status code, as a browser's close() sends; aiohttp has no public call for it.
                await websocket._writer.send_frame(b"", aiohttp.WSMsgType.CLOSE)
                assert request.transport is not None
                request.transport.close()
            case "crash":
                assert request.transport is not None
                request.transport.close()
            case "big":
                await websocket.send_bytes(b"x" * 5 * 1024 * 1024)
            case "flood":
                flooding.append(asyncio.create_task(flood()))
            case _:
                await websocket.send_str(message.data)
    for task in flooding:
        await task
    request.app[_CLOSE_CODES].put_nowait(websocket.close_code)
    return websocket


@asynccontextmanager
async def _app(
    host: str = "127.0.0.1",
    *,
    port: int = 0,
    released: asyncio.Event | None = None,
    close_codes: asyncio.Queue[int | None] | None = None,
) -> AsyncIterator[int]:
    """Serve the test app on ``host`` at ``port``, or any free one, and yield its port."""

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
    listener = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host, port))
    except OSError:
        listener.close()
        raise
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.SockSite(runner, listener).start()
        yield listener.getsockname()[1]
    finally:
        await runner.cleanup()


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
            # Headers the caller leaves out stay out: the relay adds no Accept, User-Agent, Accept-Encoding or Content-Type.
            skip_auto_headers=("Accept", "User-Agent", "Accept-Encoding", "Content-Type"),
        ) as response,
    ):
        assert response.status == 200
        assert _REFUSED not in response.headers
        assert response.headers.getall("Set-Cookie") == ["a=1", "b=2"]
        assert len(response.headers.getall("Date")) == len(response.headers.getall("Server")) == 1
        assert "Proxy-Authenticate" not in response.headers
        seen = await response.json()
    headers = dict(seen.pop("headers"))
    assert seen == {"method": "POST", "path": "/a%3Fb/../c%2Fd?x=%20&y=1+2", "body": "payload"}
    assert headers["host"] == f"127.0.0.1:{port}"
    assert headers["cookie"] == "session=app"
    assert headers["content-length"] == "7"
    assert {
        "x-mindroom-sandbox-token",
        "x-private",
        "accept",
        "user-agent",
        "accept-encoding",
        "content-type",
    }.isdisjoint(
        headers,
    )


@pytest.mark.asyncio
async def test_relay_passes_a_chunked_upload(tmp_path: Path) -> None:
    """A body sent without a length, in chunks, reaches the app whole."""

    async def chunks() -> AsyncIterator[bytes]:
        for part in (b"first;", b"second"):
            yield part

    async with (
        _app() as port,
        _runner(tmp_path) as runner,
        aiohttp.ClientSession() as client,
        client.put(f"{runner.root}/{port}/upload", data=chunks(), headers=_AUTH) as response,
    ):
        seen = await response.json()
    assert dict(seen["headers"])["transfer-encoding"] == "chunked"
    assert seen["body"] == "first;second"


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
@pytest.mark.parametrize("host", ["::1", "::", "::ffff:127.0.0.1"])
async def test_relay_reaches_an_app_on_an_ipv6_socket_that_takes_loopback(tmp_path: Path, host: str) -> None:
    """An app on ``[::1]``, every IPv6 address, or ``::ffff:127.0.0.1`` (as Java binds localhost) is listed and reached.

    Requests and WebSockets both reach it.
    """
    async with AsyncExitStack() as stack:
        try:
            port = await stack.enter_async_context(_app(host))
        except OSError:
            pytest.skip("No IPv6 on this host.")
        runner = await stack.enter_async_context(_runner(tmp_path))
        client = await stack.enter_async_context(aiohttp.ClientSession())
        async with client.get(runner.root, headers=_AUTH) as response:
            assert port in await response.json()
        async with client.get(f"{runner.root}/{port}/hello", headers=_AUTH) as response:
            assert response.status == 200
            assert (await response.json())["path"] == "/hello"
        async with client.ws_connect(f"{runner.root}/{port}/socket", headers=_AUTH) as websocket:
            assert (await websocket.receive_json())["path"] == "/socket"


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
async def test_relay_refuses_a_port_no_app_listens_on_at_loopback(tmp_path: Path, unused_tcp_port: int) -> None:
    """A closed port, a server only on another address, and no port at all are marked refusals."""
    async with _app("127.0.0.2") as elsewhere, _runner(tmp_path) as runner, aiohttp.ClientSession() as client:
        for port in (unused_tcp_port, elsewhere, 0, 70000):
            async with client.get(f"{runner.root}/{port}/", headers=_AUTH) as response:
                assert response.status == 502
                assert response.headers[_REFUSED] == "1"
                assert await response.json() == {"detail": f"No app listens on port {port} at this worker's loopback."}


async def _assert_refused(client: aiohttp.ClientSession, runner: _Runner, port: int, status: int, detail: str) -> None:
    """Requests and WebSockets to ``port`` are marked refusals."""
    async with client.get(f"{runner.root}/{port}/", headers=_AUTH) as response:
        assert response.status == status
        assert response.headers[_REFUSED] == "1"
        assert await response.json() == {"detail": detail}
    with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
        await client.ws_connect(f"{runner.root}/{port}/socket", headers=_AUTH)
    assert refused.value.status == 403


async def _assert_unlisted(client: aiohttp.ClientSession, runner: _Runner, port: int) -> None:
    async with client.get(runner.root, headers=_AUTH) as response:
        assert response.status == 200
        assert port not in await response.json()


async def _wait_listening(port: int) -> None:
    async with asyncio.timeout(10):
        while True:
            with suppress(OSError):
                _, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                return
            await asyncio.sleep(0.05)


@asynccontextmanager
async def _python_server(root: Path, port: int, *, detached: bool) -> AsyncIterator[None]:
    """Run Python's HTTP server for ``root`` on ``port``, as the runner's child or left behind by the shell that started it."""
    command = [sys.executable, "-m", "http.server", "--bind", "127.0.0.1", "--directory", str(root), str(port)]
    if detached:
        shell = await asyncio.create_subprocess_shell(f"{shlex.join(command)} >/dev/null 2>&1 & echo $!", stdout=PIPE)
        pid = int((await shell.communicate())[0])
    else:
        server = await asyncio.create_subprocess_exec(*command, stdout=DEVNULL, stderr=DEVNULL)
        pid = server.pid
    try:
        await _wait_listening(port)
        yield
    finally:
        os.kill(pid, signal.SIGTERM)
        if not detached:
            await server.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("detached", [False, True], ids=["child", "left-behind"])
async def test_relay_reaches_a_server_the_worker_started(tmp_path: Path, unused_tcp_port: int, detached: bool) -> None:
    """A server a command of the runner started is listed and relayed, and so is one that outlived the command."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("hello from the worker")
    async with (
        _python_server(site, unused_tcp_port, detached=detached),
        _runner(tmp_path) as runner,
        aiohttp.ClientSession() as client,
    ):
        async with client.get(f"{runner.root}/{unused_tcp_port}/", headers=_AUTH) as response:
            assert response.status == 200
            assert await response.text() == "hello from the worker"
        async with client.get(runner.root, headers=_AUTH) as response:
            assert unused_tcp_port in await response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["MINDROOM_PORT", "MINDROOM_SCRIPT_GATEWAY_PORT", "MINDROOM_EGRESS_BROKER_PORT"])
async def test_relay_reaches_the_workers_app_on_a_port_the_primarys_servers_use(tmp_path: Path, setting: str) -> None:
    """The API, script gateway, and egress broker listen in the primary, so a worker's app on their port is relayed."""
    async with (
        _app() as port,
        _runner(tmp_path, env={setting: str(port)}) as runner,
        aiohttp.ClientSession() as client,
    ):
        async with client.get(f"{runner.root}/{port}/hello", headers=_AUTH) as response:
            assert response.status == 200
            assert (await response.json())["path"] == "/hello"
        async with client.get(runner.root, headers=_AUTH) as response:
            assert port in await response.json()


@pytest.mark.asyncio
async def test_relay_refuses_the_runners_own_port(tmp_path: Path) -> None:
    """The runner's own port is a marked refusal and is not listed."""
    async with _runner(tmp_path) as runner, aiohttp.ClientSession() as client:
        await _assert_refused(
            client,
            runner,
            runner.port,
            403,
            f"Port {runner.port} is this runner's own, which the relay never reaches.",
        )
        await _assert_unlisted(client, runner, runner.port)


@pytest.mark.asyncio
async def test_relay_refuses_a_server_no_process_of_the_worker_holds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An app the runner sees no process of its own holding, as a pod sidecar in its own PID namespace, is refused."""
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self" / "cgroup").write_text("0::/\n")
    monkeypatch.setattr(listeners, "_PROC", proc)
    async with _app() as port, _runner(tmp_path) as runner, aiohttp.ClientSession() as client:
        await _assert_refused(client, runner, port, 403, f"The server on port {port} is not one this worker runs.")
        await _assert_unlisted(client, runner, port)


@pytest.mark.asyncio
async def test_relay_refuses_every_port_when_proc_cannot_say(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``/proc`` to tell whose server listens on a port, the relay and the port list refuse and say why."""
    monkeypatch.setattr(listeners, "_PROC", tmp_path / "missing")
    cannot_tell = "This runner can't tell which processes hold its ports, so it relays to none."
    async with _app() as port, _runner(tmp_path) as runner, aiohttp.ClientSession() as client:
        await _assert_refused(client, runner, port, 403, cannot_tell)
        async with client.get(runner.root, headers=_AUTH) as response:
            assert response.status == 403
            assert response.headers[_REFUSED] == "1"
            assert await response.json() == {"detail": cannot_tell}


@pytest.mark.asyncio
async def test_relay_reads_a_ports_holders_again_only_once_another_socket_listens_there(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A port found to be the worker's is relayed without reading its holders again, until its listening socket changes."""
    async with _runner(tmp_path) as runner, aiohttp.ClientSession() as client:
        async with _app() as port:
            async with client.get(f"{runner.root}/{port}/", headers=_AUTH) as response:
                assert response.status == 200
            monkeypatch.setattr(listeners, "_PROC", tmp_path / "missing")
            async with client.get(f"{runner.root}/{port}/", headers=_AUTH) as response:
                assert response.status == 200
        async with _app(port=port), client.get(f"{runner.root}/{port}/", headers=_AUTH) as response:
            assert response.status == 403


@pytest.mark.asyncio
async def test_relay_follows_no_websocket_redirect_to_a_server_the_worker_does_not_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WebSocket handshake an app redirects to a server no process of the worker holds is refused."""
    async with (
        _app() as port,
        _app() as other,
        _runner(tmp_path) as runner,
        aiohttp.ClientSession() as client,
    ):
        # Found to be the worker's while /proc shows its holder, so the app on ``port`` stays reachable below.
        async with client.get(f"{runner.root}/{port}/", headers=_AUTH) as response:
            assert response.status == 200
        proc = tmp_path / "proc"
        (proc / "self").mkdir(parents=True)
        (proc / "self" / "cgroup").write_text("0::/\n")
        monkeypatch.setattr(listeners, "_PROC", proc)
        there = f"http://127.0.0.1:{other}/socket"
        with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
            await client.ws_connect(f"{runner.root}/{port}/hop?status=302&location={there}", headers=_AUTH)
        assert refused.value.status == 403
        async with client.get(f"{runner.root}/{port}/", headers=_AUTH) as response:
            assert response.status == 200


@pytest.mark.asyncio
async def test_relay_refuses_when_the_app_drops_the_request(tmp_path: Path) -> None:
    """An app that closes the connection without answering is a marked refusal."""

    async def hang_up(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(hang_up, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with (
        server,
        _runner(tmp_path) as runner,
        aiohttp.ClientSession() as client,
        client.get(f"{runner.root}/{port}/", headers=_AUTH) as response,
    ):
        assert response.status == 502
        assert response.headers[_REFUSED] == "1"
        assert await response.json() == {"detail": f"The app on port {port} did not answer."}


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
    """The path and query, messages up to 16 MiB, the subprotocol, and the app's close code pass; the token does not."""
    async with (
        _app() as port,
        _runner(tmp_path) as runner,
        # Headers the caller leaves out stay out: the relay adds no User-Agent, Accept or Accept-Encoding.
        aiohttp.ClientSession(skip_auto_headers=("User-Agent", "Accept", "Accept-Encoding")) as client,
        client.ws_connect(
            f"{runner.root}/{port}/socket?room=1",
            headers={**_AUTH, "Cookie": "session=app"},
            protocols=("chat",),
            max_msg_size=16 * 1024 * 1024,
        ) as websocket,
    ):
        assert websocket.protocol == "chat"
        greeting = await websocket.receive_json()
        assert greeting["path"] == "/socket?room=1"
        handshake = greeting["headers"]
        assert handshake["cookie"] == "session=app"
        assert handshake["host"] == f"127.0.0.1:{port}"
        assert {"x-mindroom-sandbox-token", "user-agent", "accept", "accept-encoding"}.isdisjoint(handshake)
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
@pytest.mark.parametrize(("message", "close_code"), [("restart", 1012), ("quiet", 1000), ("crash", 1011)])
async def test_relay_tells_the_caller_how_the_app_ended_its_websocket(
    tmp_path: Path,
    message: str,
    close_code: int,
) -> None:
    """An app's own code reaches the caller, a close without one is normal, and a dropped connection is an error."""
    async with (
        _app() as port,
        _runner(tmp_path) as runner,
        aiohttp.ClientSession() as client,
        client.ws_connect(f"{runner.root}/{port}/socket", headers=_AUTH) as websocket,
    ):
        await websocket.receive_json()
        await websocket.send_str(message)
        assert (await websocket.receive()).type == aiohttp.WSMsgType.CLOSE
        assert websocket.close_code == close_code


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
async def test_relay_closes_the_app_normally_when_the_caller_closes_without_a_code(tmp_path: Path) -> None:
    """A caller's close frame without a status code, as a browser's close() sends, closes the app's socket with 1000."""
    close_codes: asyncio.Queue[int | None] = asyncio.Queue()
    async with _app(close_codes=close_codes) as port, _runner(tmp_path) as runner:
        reader, writer = await asyncio.open_connection("127.0.0.1", runner.port)
        writer.write(
            (
                f"GET /api/sandbox-runner/ports/{port}/socket HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
                f"X-Mindroom-Sandbox-Token: {_TOKEN}\r\n\r\n"
            ).encode(),
        )
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 101")
        # A masked close frame with an empty payload; like a browser, hang up once the runner has closed its side.
        writer.write(b"\x88\x80\x00\x00\x00\x00")
        async with asyncio.timeout(5):
            await reader.read()
        writer.close()
        await writer.wait_closed()
        async with asyncio.timeout(5):
            assert await close_codes.get() == 1000


@pytest.mark.asyncio
async def test_relay_ends_the_apps_websocket_in_an_error_when_the_caller_drops_mid_stream(tmp_path: Path) -> None:
    """A caller that drops its connection while the app streams to it ends the app's socket with 1011, not a normal 1000."""
    close_codes: asyncio.Queue[int | None] = asyncio.Queue()
    async with _app(close_codes=close_codes) as port, _runner(tmp_path) as runner:
        async with aiohttp.ClientSession() as client:
            websocket = await client.ws_connect(f"{runner.root}/{port}/socket", headers=_AUTH)
            await websocket.receive_json()
            await websocket.send_str("flood")
            assert await websocket.receive_str() == "x" * 1024
            # Drop the connection without a close frame.
            assert websocket._response.connection is not None
            websocket._response.connection.transport.abort()
        async with asyncio.timeout(5):
            assert await close_codes.get() == 1011


@pytest.mark.asyncio
async def test_relay_follows_a_websocket_redirect_only_on_loopback(tmp_path: Path) -> None:
    """A WebSocket handshake the app redirects within loopback connects; one it redirects elsewhere is refused."""
    async with (
        _app() as port,
        _app("127.0.0.2") as elsewhere,
        _runner(tmp_path) as runner,
        aiohttp.ClientSession() as client,
    ):
        here = f"http://127.0.0.1:{port}/socket"
        async with client.ws_connect(
            f"{runner.root}/{port}/hop?status=302&location={here}",
            headers=_AUTH,
        ) as websocket:
            assert (await websocket.receive_json())["path"] == "/socket"
        there = f"http://127.0.0.2:{elsewhere}/socket"
        with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
            await client.ws_connect(f"{runner.root}/{port}/hop?status=302&location={there}", headers=_AUTH)
        assert refused.value.status == 403


@pytest.mark.asyncio
async def test_relay_refuses_a_websocket_nothing_answers(tmp_path: Path, unused_tcp_port: int) -> None:
    """A WebSocket to a closed port, or to an app path that takes none, is refused at the handshake."""
    async with _app() as port, _runner(tmp_path) as runner, aiohttp.ClientSession() as client:
        for url in (f"{runner.root}/{unused_tcp_port}/socket", f"{runner.root}/{port}/plain"):
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
