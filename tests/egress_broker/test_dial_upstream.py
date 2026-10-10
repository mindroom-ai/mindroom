"""Tests for dialing upstreams through an operator egress proxy."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import ssl
from typing import TYPE_CHECKING, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.config.egress_broker import EgressAuth, EgressBrokerConfig, EgressRule, EgressService
from mindroom.egress_broker import dial
from mindroom.egress_broker.dial import DestinationBlockedError, DialPolicy, open_upstream
from mindroom.worker_computer.browser_proxy import BrowserEgress, UpstreamTunnelRefusedError, _UpstreamProxy
from tests.egress_broker.conftest import connect_request, proxy_authorization

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractContextManager
    from pathlib import Path

    import httpx

    from tests.egress_broker.conftest import BrokerFactory, RawResponse, UpstreamCA

    RawProxy = Callable[[int, bytes], Awaitable[RawResponse]]

_PUBLIC = ipaddress.IPv4Address("93.184.216.34")
_RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"


async def _pump(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    with contextlib.suppress(OSError):
        while data := await source.read(64 * 1024):
            sink.write(data)
            await sink.drain()


class _OperatorProxy:
    """A fake operator HTTP proxy that records CONNECT heads and serves one response inside each tunnel it accepts.

    It stands in for the public destination behind the tunnel: with ``inner_tls`` the tunnel leads to a TLS server,
    and ``outer_tls`` makes the connection to the proxy itself TLS. With ``after_connect`` set to ``garbage`` it
    answers the client's first bytes with something that is not TLS, and with ``silent`` it never answers; in both
    it then waits for the client to close the tunnel and sets ``tunnel_closed``.
    """

    def __init__(
        self,
        *,
        refuse: bool = False,
        inner_tls: ssl.SSLContext | None = None,
        outer_tls: ssl.SSLContext | None = None,
        after_connect: Literal["serve", "garbage", "silent"] = "serve",
    ) -> None:
        self.heads: list[bytes] = []
        self.inner_requests: list[bytes] = []
        self.tunnel_closed = asyncio.Event()
        self._refuse = refuse
        self._inner_tls = inner_tls
        self._outer_tls = outer_tls
        self._after_connect = after_connect
        self._servers: list[asyncio.Server] = []
        self._inner_port = 0
        self._writers: list[asyncio.StreamWriter] = []

    @property
    def requests(self) -> list[bytes]:
        """Return the request line of every CONNECT received."""
        return [head.split(b"\r\n", 1)[0] for head in self.heads]

    async def __aenter__(self) -> _UpstreamProxy:
        if self._inner_tls is not None:
            inner = await asyncio.start_server(self._guarded(self._respond), "127.0.0.1", 0, ssl=self._inner_tls)
            self._servers.append(inner)
            self._inner_port = inner.sockets[0].getsockname()[1]
        server = await asyncio.start_server(self._guarded(self._serve), "127.0.0.1", 0, ssl=self._outer_tls)
        self._servers.append(server)
        return _UpstreamProxy(
            host="127.0.0.1",
            port=server.sockets[0].getsockname()[1],
            tls=self._outer_tls is not None,
        )

    async def __aexit__(self, *_args: object) -> None:
        for server in self._servers:
            server.close()
        # A client that leaked its tunnel must fail its test, not hang the server shutdown.
        for writer in self._writers:
            writer.transport.abort()
        for server in self._servers:
            await server.wait_closed()

    def _guarded(
        self,
        handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
    ) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            self._writers.append(writer)
            try:
                with contextlib.suppress(OSError, asyncio.IncompleteReadError):
                    await handler(reader, writer)
            finally:
                writer.transport.abort()

        return handle

    async def _respond(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.inner_requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(_RESPONSE)
        await writer.drain()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.heads.append(await reader.readuntil(b"\r\n\r\n"))
        if self._refuse:
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        if self._after_connect != "serve":
            try:
                if self._after_connect == "garbage":
                    await reader.readexactly(1)
                    writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                    await writer.drain()
                await reader.read()
            finally:
                self.tunnel_closed.set()
        elif self._inner_tls is None:
            await self._respond(reader, writer)
        else:
            inner_reader, inner_writer = await asyncio.open_connection("127.0.0.1", self._inner_port)
            self._writers.append(inner_writer)
            _done, pending = await asyncio.wait(
                [
                    asyncio.ensure_future(_pump(reader, inner_writer)),
                    asyncio.ensure_future(_pump(inner_reader, writer)),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()


async def _get(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, host: str) -> bytes:
    """Send one request on an open upstream stream and return the whole response."""
    try:
        writer.write(f"GET /ok HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        async with asyncio.timeout(5):
            return await reader.read()
    finally:
        writer.transport.abort()


def _resolve_as(*addresses: ipaddress.IPv4Address | ipaddress.IPv6Address) -> AbstractContextManager[object]:
    return patch("mindroom.egress_broker.dial.validated_connect_addresses", return_value=list(addresses))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("address", "authority"),
    [
        (_PUBLIC, b"93.184.216.34:443"),
        (ipaddress.IPv6Address("2606:2800:220:1:248:1893:25c8:1946"), b"[2606:2800:220:1:248:1893:25c8:1946]:443"),
    ],
)
async def test_tunnels_validated_ip_through_operator_proxy(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    authority: bytes,
) -> None:
    """The operator proxy is asked for the validated IP, never the name, and carries nothing else."""
    operator = _OperatorProxy()
    async with operator as proxy:
        policy = DialPolicy(egress=BrowserEgress(http=proxy, https=proxy))
        with _resolve_as(address):
            reader, writer = await open_upstream("example.com", 443, policy=policy, ssl_context=None)
        response = await _get(reader, writer, "example.com")
    assert response.endswith(b"ok")
    assert [head.split(b"\r\n") for head in operator.heads] == [
        [b"CONNECT " + authority + b" HTTP/1.1", b"Host: " + authority, b"", b""],
    ]


@pytest.mark.asyncio
async def test_tls_to_the_named_host_runs_inside_the_tunnel(upstream_ca: UpstreamCA, tmp_path: Path) -> None:
    """The handshake verifies the destination name, not the IP the tunnel was opened to."""
    async with _OperatorProxy(inner_tls=upstream_ca.server_context(tmp_path)) as proxy:
        policy = DialPolicy(egress=BrowserEgress(https=proxy))
        with _resolve_as(_PUBLIC):
            reader, writer = await open_upstream(
                "localhost",
                443,
                policy=policy,
                ssl_context=upstream_ca.client_context(),
            )
        response = await _get(reader, writer, "localhost")
    assert response.endswith(b"ok")


@pytest.mark.asyncio
async def test_certificate_for_another_name_fails_inside_the_tunnel(upstream_ca: UpstreamCA, tmp_path: Path) -> None:
    """The handshake verifies the destination name exactly as a direct dial does."""
    async with _OperatorProxy(inner_tls=upstream_ca.server_context(tmp_path)) as proxy:
        policy = DialPolicy(egress=BrowserEgress(https=proxy))
        with _resolve_as(_PUBLIC), pytest.raises(ssl.SSLCertVerificationError):
            await open_upstream("other.example", 443, policy=policy, ssl_context=upstream_ca.client_context())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("after_connect", "failure"),
    [("garbage", ssl.SSLError), ("silent", TimeoutError)],
)
async def test_tls_handshake_failure_inside_the_tunnel_closes_the_connection(
    after_connect: Literal["garbage", "silent"],
    failure: type[Exception],
) -> None:
    """A handshake that fails or stalls inside the tunnel leaves no open connection to the operator proxy."""
    operator = _OperatorProxy(after_connect=after_connect)
    async with operator as proxy:
        policy = DialPolicy(connect_timeout=0.3, egress=BrowserEgress(https=proxy))
        with _resolve_as(_PUBLIC), pytest.raises(failure):
            await open_upstream("localhost", 443, policy=policy, ssl_context=ssl.create_default_context())
        async with asyncio.timeout(5):
            await operator.tunnel_closed.wait()


@pytest.mark.asyncio
async def test_https_operator_proxy_carries_tls_inside_tls(
    upstream_ca: UpstreamCA,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An https:// operator proxy is verified with the default trust store, and the destination's TLS runs inside it."""
    monkeypatch.setattr(dial, "_operator_proxy_tls", upstream_ca.client_context)
    operator = _OperatorProxy(
        outer_tls=upstream_ca.server_context(tmp_path),
        inner_tls=upstream_ca.server_context(tmp_path),
    )
    async with operator as proxy:
        policy = DialPolicy(egress=BrowserEgress(https=proxy))
        with _resolve_as(_PUBLIC):
            reader, writer = await open_upstream(
                "localhost",
                443,
                policy=policy,
                ssl_context=upstream_ca.client_context(),
            )
        response = await _get(reader, writer, "localhost")
    assert response.endswith(b"ok")
    assert operator.requests == [b"CONNECT 93.184.216.34:443 HTTP/1.1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("egress", "port"),
    [
        (BrowserEgress(https=_UpstreamProxy("127.0.0.1", 1, tls=False), no_proxy=(".example.com",)), 443),
        (BrowserEgress(https=_UpstreamProxy("127.0.0.1", 1, tls=False), no_proxy=("93.184.216.0/24",)), 443),
        (BrowserEgress(https=_UpstreamProxy("127.0.0.1", 1, tls=False), no_proxy=("*",)), 443),
        # Only port 80 would use the http proxy, and none is configured.
        (BrowserEgress(https=_UpstreamProxy("127.0.0.1", 1, tls=False)), 80),
    ],
)
async def test_no_proxy_destination_dials_direct(egress: BrowserEgress, port: int) -> None:
    """NO_PROXY entries and ports without a configured proxy are dialed directly at the validated IP."""
    dialed: list[tuple[str, int]] = []

    async def open_connection(host: str, port: int, **_kwargs: object) -> tuple[AsyncMock, MagicMock]:
        dialed.append((host, port))
        return AsyncMock(spec=asyncio.StreamReader), MagicMock(spec=asyncio.StreamWriter)

    with (
        _resolve_as(_PUBLIC),
        patch("mindroom.egress_broker.dial.asyncio.open_connection", side_effect=open_connection),
    ):
        await open_upstream("www.example.com", port, policy=DialPolicy(egress=egress), ssl_context=None)

    assert dialed == [("93.184.216.34", port)]


@pytest.mark.asyncio
async def test_loopback_destination_dials_direct_even_with_a_proxy() -> None:
    """An operator proxy's loopback is another host, so a permitted loopback destination never goes through it."""
    operator = _OperatorProxy()
    async with operator as proxy:
        server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            policy = DialPolicy(allow_loopback=True, egress=BrowserEgress(http=proxy, https=proxy))
            _reader, writer = await open_upstream("127.0.0.1", port, policy=policy, ssl_context=None)
            writer.transport.abort()
        finally:
            server.close()
            await server.wait_closed()
    assert operator.heads == []


@pytest.mark.asyncio
async def test_ipv4_mapped_loopback_dials_direct_even_with_a_proxy() -> None:
    """An IPv4-mapped loopback address is loopback too, so it is never sent to the operator proxy."""
    operator = _OperatorProxy()
    async with operator as proxy:
        server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            policy = DialPolicy(egress=BrowserEgress(http=proxy, https=proxy))
            with _resolve_as(ipaddress.IPv6Address("::ffff:127.0.0.1")):
                _reader, writer = await open_upstream("mapped.example", port, policy=policy, ssl_context=None)
            writer.transport.abort()
        finally:
            server.close()
            await server.wait_closed()
    assert operator.heads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["10.0.0.1", "169.254.169.254", "127.0.0.1", "::ffff:10.0.0.1"])
async def test_blocked_destination_never_contacts_proxy(host: str) -> None:
    """The SSRF guard runs before any tunnel, so a blocked destination never reaches the operator proxy."""
    connections = 0

    def accept(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal connections
        connections += 1
        writer.close()

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        proxy = _UpstreamProxy("127.0.0.1", port, tls=False)
        policy = DialPolicy(egress=BrowserEgress(http=proxy, https=proxy))
        with pytest.raises(DestinationBlockedError):
            await open_upstream(host, 443, policy=policy, ssl_context=None)
    finally:
        server.close()
        await server.wait_closed()
    assert connections == 0


@pytest.mark.asyncio
async def test_no_proxy_cannot_reach_blocked_addresses() -> None:
    """NO_PROXY only picks the route of a destination the guard allowed; it never lets a blocked one through."""
    operator = _OperatorProxy()
    async with operator as proxy:
        egress = BrowserEgress(https=proxy, no_proxy=("*", "10.0.0.0/8"))
        with (
            patch("mindroom.egress_broker.dial.asyncio.open_connection", new_callable=AsyncMock) as direct,
            pytest.raises(DestinationBlockedError),
        ):
            await open_upstream("10.0.0.1", 443, policy=DialPolicy(egress=egress), ssl_context=None)
    direct.assert_not_called()
    assert operator.heads == []


@pytest.mark.asyncio
async def test_refused_tunnel_is_not_retried_on_other_addresses() -> None:
    """A proxy that refuses one validated address ends the dial, as in the browser relay."""
    operator = _OperatorProxy(refuse=True)
    async with operator as proxy:
        policy = DialPolicy(egress=BrowserEgress(https=proxy))
        with _resolve_as(_PUBLIC, ipaddress.IPv4Address("93.184.216.35")), pytest.raises(UpstreamTunnelRefusedError):
            await open_upstream("example.com", 443, policy=policy, ssl_context=None)
    assert operator.requests == [b"CONNECT 93.184.216.34:443 HTTP/1.1"]


@pytest.mark.asyncio
async def test_proxy_refusal_maps_to_502(broker: BrokerFactory, raw_proxy: RawProxy) -> None:
    """A worker whose tunnel the operator proxy refuses gets the same 502 as any unreachable upstream."""
    operator = _OperatorProxy(refuse=True)
    async with operator as proxy:
        started = await broker(dial_policy=DialPolicy(egress=BrowserEgress(https=proxy)))
        with _resolve_as(_PUBLIC):
            response = await raw_proxy(
                started.port,
                connect_request("example.com:443", authorization=proxy_authorization(broker.token())),
            )
    assert response.status == 502
    assert response.json() == {"error": "upstream_unreachable"}
    assert operator.requests == [b"CONNECT 93.184.216.34:443 HTTP/1.1"]


@pytest.mark.asyncio
async def test_intercepted_request_reaches_destination_through_operator_proxy(
    broker: BrokerFactory,
    proxy_client: Callable[..., httpx.AsyncClient],
    upstream_ca: UpstreamCA,
    tmp_path: Path,
) -> None:
    """The secret is injected on the broker's own TLS session inside the tunnel; the operator proxy only sees CONNECT."""
    config = EgressBrokerConfig(
        services={"svc": EgressService(rules=[EgressRule(host="localhost", auth=EgressAuth(type="bearer"))])},
    )
    operator = _OperatorProxy(inner_tls=upstream_ca.server_context(tmp_path))
    async with operator as proxy:
        await broker(config, secrets={"svc": "s3cret"}, dial_policy=DialPolicy(egress=BrowserEgress(https=proxy)))
        token = broker.token()
        client = proxy_client(token)
        with _resolve_as(_PUBLIC):
            response = await client.get("https://localhost/ok")
    assert response.status_code == 200
    assert operator.requests == [b"CONNECT 93.184.216.34:443 HTTP/1.1"]
    assert b"s3cret" not in operator.heads[0]
    assert token.encode() not in operator.heads[0]
    assert b"proxy-authorization" not in operator.heads[0].lower()
    [inner] = operator.inner_requests
    assert b"authorization: bearer s3cret" in inner.lower()
