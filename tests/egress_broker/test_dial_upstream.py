"""Tests for dialing upstreams through an operator egress proxy."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import ssl
from typing import TYPE_CHECKING
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


class _OperatorProxy:
    """A fake operator HTTP proxy that records CONNECT heads and serves one response inside each tunnel it accepts.

    It stands in for the public destination behind the tunnel: with ``inner_tls`` it speaks TLS there, and
    ``outer_tls`` makes the connection to the proxy itself TLS.
    """

    def __init__(
        self,
        *,
        refuse: bool = False,
        inner_tls: ssl.SSLContext | None = None,
        outer_tls: ssl.SSLContext | None = None,
    ) -> None:
        self.heads: list[bytes] = []
        self.inner_requests: list[bytes] = []
        self._refuse = refuse
        self._inner_tls = inner_tls
        self._outer_tls = outer_tls
        self._server: asyncio.Server | None = None

    @property
    def requests(self) -> list[bytes]:
        """Return the request line of every CONNECT received."""
        return [head.split(b"\r\n", 1)[0] for head in self.heads]

    async def __aenter__(self) -> _UpstreamProxy:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=self._outer_tls)
        port = self._server.sockets[0].getsockname()[1]
        return _UpstreamProxy(host="127.0.0.1", port=port, tls=self._outer_tls is not None)

    async def __aexit__(self, *_args: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(OSError, asyncio.IncompleteReadError):
            self.heads.append(await reader.readuntil(b"\r\n\r\n"))
            if self._refuse:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            if self._inner_tls is not None:
                await writer.start_tls(self._inner_tls)
            self.inner_requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(_RESPONSE)
            await writer.drain()
        writer.transport.abort()


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
async def test_tls_handshake_failure_inside_the_tunnel_closes_the_connection(
    upstream_ca: UpstreamCA,
    tmp_path: Path,
) -> None:
    """A destination whose certificate does not match the name fails like a direct dial, without leaking the tunnel."""
    async with _OperatorProxy(inner_tls=upstream_ca.server_context(tmp_path)) as proxy:
        policy = DialPolicy(egress=BrowserEgress(https=proxy))
        with _resolve_as(_PUBLIC), pytest.raises(ssl.SSLCertVerificationError):
            await open_upstream("other.example", 443, policy=policy, ssl_context=upstream_ca.client_context())


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
