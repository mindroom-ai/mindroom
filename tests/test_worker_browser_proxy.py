"""Destination policy and bounded lifetime of the worker browser SOCKS proxy."""

import asyncio
import ipaddress
import os
import shutil
import socket
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from mindroom.worker_computer import browser_proxy
from mindroom.worker_computer.browser_proxy import BrowserDestinationProxy


async def _connect(
    proxy: BrowserDestinationProxy,
    host: str,
    port: int,
    *,
    literal: bool = False,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, int]:
    endpoint = urlsplit(proxy.endpoint)
    reader, writer = await asyncio.open_connection(endpoint.hostname, endpoint.port)
    writer.write(b"\x05\x01\x00")
    await writer.drain()
    assert await reader.readexactly(2) == b"\x05\x00"
    if literal:
        address = ipaddress.ip_address(host)
        encoded = bytes([1 if address.version == 4 else 4]) + address.packed
    else:
        hostname = host.encode("ascii")
        encoded = b"\x03" + bytes([len(hostname)]) + hostname
    writer.write(b"\x05\x01\x00" + encoded + port.to_bytes(2, "big"))
    await writer.drain()
    reply = await reader.readexactly(10)
    return reader, writer, reply[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_destination_policy_and_owned_tunnel_cleanup(private: bool) -> None:
    """Private opt-in permits a real tunnel, never metadata or proxy recursion."""
    reached = asyncio.Event()
    disconnected = asyncio.Event()

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        reached.set()
        try:
            while data := await reader.read(1024):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            disconnected.set()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    proxy = BrowserDestinationProxy(allow_private_networks=private)
    await proxy.start()
    try:
        for host, destination_port in [
            ("169.254.169.254", 80),
            ("metadata.google.internal", 80),
            ("127.0.0.1", urlsplit(proxy.endpoint).port),
        ]:
            reader, writer, status = await _connect(proxy, host, destination_port)
            assert status != 0
            assert await reader.read() == b""
            writer.close()
            await writer.wait_closed()
        reader, writer, status = await _connect(proxy, "127.0.0.1", port)
        assert (status == 0) is private
        if private:
            writer.write(b"opaque TLS or HTTP bytes")
            await writer.drain()
            assert await reader.readexactly(24) == b"opaque TLS or HTTP bytes"
        else:
            assert not reached.is_set()
        await asyncio.wait_for(proxy.close(), 1)
        assert await reader.read() == b""
        if private:
            await asyncio.wait_for(disconnected.wait(), 1)
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_validated_numeric_address_is_dialed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The proxy never resolves the original hostname again at connection time."""
    resolved = []
    dialed = []
    original = asyncio.open_connection

    def resolve(host: str, *, port: int, allow_private_networks: bool) -> list[ipaddress.IPv4Address]:
        resolved.append((host, port, allow_private_networks))
        return [ipaddress.IPv4Address("8.8.8.8")]

    async def dial(host: str, port: int, **kwargs: object) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if host == "127.0.0.1":
            return await original(host, port)
        dialed.append((host, port, kwargs))
        msg = "fixture connection refused"
        raise OSError(msg)

    monkeypatch.setattr(browser_proxy, "validated_connect_addresses", resolve)
    monkeypatch.setattr(asyncio, "open_connection", dial)
    proxy = BrowserDestinationProxy()
    await proxy.start()
    try:
        _, writer, status = await _connect(proxy, "fixture.example", 443)
        assert status != 0
        assert resolved == [("fixture.example", 443, False)]
        assert dialed == [("8.8.8.8", 443, {"family": socket.AF_INET})]
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"\x04\x01\x00", b"\x05\x01\x02", b"\x05\x00", b"\x05"])
async def test_invalid_or_stalled_greeting_closes(payload: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unsupported authentication and incomplete handshakes fail within a deadline."""
    monkeypatch.setattr(browser_proxy, "_SETUP_DEADLINE", 0.05)
    proxy = BrowserDestinationProxy()
    await proxy.start()
    endpoint = urlsplit(proxy.endpoint)
    try:
        reader, writer = await asyncio.open_connection(endpoint.hostname, endpoint.port)
        writer.write(payload)
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 1) in (b"", b"\x05\xff")
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b"\x05\x02\x00\x01",
        b"\x05\x03\x00\x01",
        b"\x05\x01\x01\x01",
        b"\x05\x01\x00\x09",
        b"\x05\x01\x00\x03\x00",
        b"\x05\x01\x00\x03\x01\xff",
    ],
)
async def test_unsupported_connect_requests_close(payload: bytes) -> None:
    """BIND, UDP, malformed headers and invalid address encodings never connect."""
    proxy = BrowserDestinationProxy()
    await proxy.start()
    endpoint = urlsplit(proxy.endpoint)
    try:
        reader, writer = await asyncio.open_connection(endpoint.hostname, endpoint.port)
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        assert await reader.readexactly(2) == b"\x05\x00"
        writer.write(payload)
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 1) == b""
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.close()


@pytest.mark.asyncio
async def test_close_owns_stalled_handshakes() -> None:
    """Closing a proxy never waits for an incomplete handshake deadline."""
    proxy = BrowserDestinationProxy()
    await proxy.start()
    endpoint = urlsplit(proxy.endpoint)
    reader, writer = await asyncio.open_connection(endpoint.hostname, endpoint.port)
    try:
        await asyncio.wait_for(proxy.close(), 1)
        assert await reader.read() == b""
        with pytest.raises(ConnectionRefusedError):
            await asyncio.open_connection(endpoint.hostname, endpoint.port)
    finally:
        writer.close()
        await writer.wait_closed()
        await proxy.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
async def test_literal_proxy_recursion_never_dials(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """All supported literal encodings, including mapped IPv4, reject self-connect."""
    proxy = BrowserDestinationProxy(allow_private_networks=True)
    await proxy.start()
    port = urlsplit(proxy.endpoint).port
    assert port is not None
    original = asyncio.open_connection
    calls = []

    async def dial(
        destination: str,
        destination_port: int,
        **kwargs: object,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if kwargs:
            calls.append(destination)
            raise ConnectionRefusedError
        return await original(destination, destination_port)

    monkeypatch.setattr(asyncio, "open_connection", dial)
    try:
        _, writer, status = await _connect(proxy, host, port, literal=True)
        assert status != 0
        assert calls == []
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["deadline", "close"])
async def test_stalled_destination_connect_is_cancelled(stop: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Setup deadlines and explicit close cancel a pending destination connection."""
    original = asyncio.open_connection
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def dial(host: str, port: int, **_kwargs: object) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if host == "127.0.0.1":
            return await original(host, port)
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
        raise AssertionError

    monkeypatch.setattr(asyncio, "open_connection", dial)
    monkeypatch.setattr(browser_proxy, "_SETUP_DEADLINE", 0.1)
    proxy = BrowserDestinationProxy()
    await proxy.start()
    endpoint = urlsplit(proxy.endpoint)
    reader, writer = await asyncio.open_connection(endpoint.hostname, endpoint.port)
    try:
        writer.write(b"\x05\x01\x00\x05\x01\x00\x01\x08\x08\x08\x08\x01\xbb")
        await writer.drain()
        assert await reader.readexactly(2) == b"\x05\x00"
        await asyncio.wait_for(entered.wait(), 1)
        if stop == "close":
            await asyncio.wait_for(proxy.close(), 1)
        assert await asyncio.wait_for(reader.read(), 1) == b""
        assert cancelled.is_set()
    finally:
        writer.close()
        await writer.wait_closed()
        await proxy.close()


@pytest.mark.asyncio
async def test_pinned_browser_redirect_destinations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: C901, PLR0915 - one real browser fixture lifecycle
    """Real pinned MCP/Chromium cannot send redirected traffic to a denied host."""
    from aiohttp import web  # noqa: PLC0415

    from mindroom.worker_computer import mcp_provider  # noqa: PLC0415

    cli = os.environ.get("MINDROOM_TEST_BROWSER_MCP_CLI")
    executable = os.environ.get("MINDROOM_TEST_BROWSER_EXECUTABLE") or shutil.which("chromium")
    if not cli or not executable:
        pytest.skip("Set MINDROOM_TEST_BROWSER_MCP_CLI and supply Chromium for the pinned integration probe")
    hits: list[str] = []
    denied_connection = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_validate = browser_proxy.validated_connect_addresses
    original_open = asyncio.open_connection
    original_parameters = mcp_provider.WorkerBrowserMCP._server_parameters

    def validate(host: str, *, port: int, allow_private_networks: bool) -> object:
        try:
            return original_validate(host, port=port, allow_private_networks=allow_private_networks)
        except ValueError:
            if host == "127.0.0.1":
                loop.call_soon_threadsafe(denied_connection.set)
            raise

    async def fixture(request: web.Request) -> web.Response:
        hits.append(request.path)
        locations = {
            "/denied": f"http://127.0.0.1:{port}/blocked",
            "/metadata": "http://169.254.169.254/blocked",
            "/allowed": f"http://9.9.9.9:{port}/destination",
            "/post": f"http://9.9.9.9:{port}/post-destination",
        }
        if request.path in locations:
            return web.Response(status=307, headers={"Location": locations[request.path]})
        if request.path == "/post-destination":
            assert await request.text() == "exactly once"
        return web.Response(text="<title>Proxy fixture</title><h1>Destination</h1>", content_type="text/html")

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", fixture)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]

    async def dial(
        host: str,
        destination_port: int,
        **kwargs: object,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        # Only validated public fixture addresses map to the local fixture.
        # Denied loopback/metadata destinations use the actual unchanged policy.
        if host in {"8.8.8.8", "9.9.9.9"} and destination_port == port:
            host = "127.0.0.1"
        return await original_open(host, destination_port, **kwargs)

    def parameters(self: mcp_provider.WorkerBrowserMCP) -> object:
        result = original_parameters(self)
        result.args += ["--headless"]
        result.args[result.args.index("--init-page") + 1] = str(
            Path(mcp_provider.__file__).with_name("browser_guard.cjs"),
        )
        return result

    monkeypatch.setattr(asyncio, "open_connection", dial)
    monkeypatch.setattr(browser_proxy, "validated_connect_addresses", validate)
    monkeypatch.setattr(mcp_provider, "_SERVER", cli)
    monkeypatch.setattr(mcp_provider, "_BROWSER", executable)
    monkeypatch.setattr(mcp_provider.WorkerBrowserMCP, "_server_parameters", parameters)
    browser = mcp_provider.WorkerBrowserMCP(
        display=":99",
        workspace=tmp_path / "workspace",
        storage_root=tmp_path / "profile",
    )
    base = f"http://8.8.8.8:{port}"
    try:
        with pytest.raises(RuntimeError, match="ERR_SOCKS_CONNECTION_FAILED"):
            await browser.execute("browser_navigate", {"url": base + "/denied"})
        assert "/denied" in hits
        assert "/blocked" not in hits
        await browser.close()
        browser = mcp_provider.WorkerBrowserMCP(
            display=":99",
            workspace=tmp_path / "workspace",
            storage_root=tmp_path / "profile",
        )
        await browser.execute("browser_navigate", {"url": base + "/allowed"})
        assert "/destination" in hits
        # Fetch, image and popup redirects must use the same destination proxy.
        await browser.execute("browser_navigate", {"url": base + "/page"})
        await browser.execute(
            "browser_evaluate",
            {
                "function": "async () => { await fetch('/post', {method:'POST', body:'exactly once'}).catch(() => {}); await Promise.all([fetch('/denied').catch(() => {}), new Promise(resolve => { const img = new Image(); img.onload = img.onerror = resolve; img.src = '/denied'; })]); }",
            },
        )
        denied_connection.clear()
        await browser.execute("browser_evaluate", {"function": "() => { window.open('/denied'); }"})
        await asyncio.wait_for(denied_connection.wait(), 5)
        await browser.execute("browser_snapshot", {})
        assert hits.count("/post") == hits.count("/post-destination") == 1
        assert "/blocked" not in hits
        await browser.close()
        browser = mcp_provider.WorkerBrowserMCP(
            display=":99",
            workspace=tmp_path / "workspace",
            storage_root=tmp_path / "private",
            allow_private_networks=True,
        )
        await browser.execute("browser_navigate", {"url": base + "/denied"})
        assert hits.count("/blocked") == 1
        with pytest.raises(RuntimeError, match="ERR_SOCKS_CONNECTION_FAILED"):
            await browser.execute("browser_navigate", {"url": base + "/metadata"})
        assert hits.count("/blocked") == 1
    finally:
        await browser.close()
        await runner.cleanup()
