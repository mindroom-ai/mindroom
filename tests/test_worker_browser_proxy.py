"""Destination policy and bounded lifetime of the worker browser SOCKS proxy."""

import asyncio
import ipaddress
import json
import os
import shutil
import socket
import ssl
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from structlog.testing import capture_logs

from mindroom.constants import resolve_primary_runtime_paths
from mindroom.custom_tools.browser import BrowserTools
from mindroom.custom_tools.browser_mcp import BrowserMCPTools
from mindroom.worker_computer import browser_proxy, mcp_provider
from mindroom.worker_computer.browser_bundle import COMPUTER_BROWSER_MCP_CONFIG
from mindroom.worker_computer.browser_proxy import (
    PROXIED_WEBRTC_ONLY_ARG,
    BrowserDestinationProxy,
    BrowserEgress,
    _UpstreamProxy,
    browser_egress,
)
from tests.browser_egress_helpers import SQUID_DEFAULT_CONNECT_PORTS, SquidLikeUpstream, socks5_connect
from tests.browser_lifecycle_helpers import LifecycleBrowser

_WORKER = _UpstreamProxy(host="worker", port=3128, tls=False)
_OTHER = _UpstreamProxy(host="other", port=3128, tls=False)


@pytest.mark.parametrize(
    ("runtime_env", "worker_env", "expected"),
    [
        ({}, {}, None),
        ({"all_proxy": "", "ALL_PROXY": "http://worker:3128"}, {}, _WORKER),
        ({}, {"all_proxy": "", "ALL_PROXY": "http://worker:3128"}, _WORKER),
        (
            {},
            {
                "http_proxy": "",
                "HTTP_PROXY": "http://worker:3128",
                "https_proxy": "",
                "HTTPS_PROXY": "http://worker:3128",
            },
            _WORKER,
        ),
        ({"all_proxy": "http://old:3128"}, {"ALL_PROXY": "http://worker:3128"}, _WORKER),
        # One configured proxy carries every connection, whichever variable names it.
        ({}, {"HTTPS_PROXY": "http://worker:3128"}, _WORKER),
        ({}, {"http_proxy": "http://worker:3128"}, _WORKER),
        # Trivially different spellings of one proxy are the same proxy.
        ({}, {"http_proxy": "http://worker:3128", "https_proxy": "http://worker:3128/"}, _WORKER),
        ({}, {"http_proxy": "HTTP://Worker:3128", "https_proxy": "http://worker:3128"}, _WORKER),
        (
            {},
            {"http_proxy": "http://worker", "https_proxy": "http://worker:80"},
            _UpstreamProxy("worker", 80, tls=False),
        ),
        ({}, {"http_proxy": "worker:3128"}, _WORKER),
        ({}, {"https_proxy": "https://[fd00::1]:3128"}, _UpstreamProxy("fd00::1", 3128, tls=True)),
    ],
)
def test_runner_egress_sends_every_connection_to_its_one_proxy(
    runtime_env: dict[str, str],
    worker_env: dict[str, str],
    expected: _UpstreamProxy | None,
) -> None:
    """Case aliases cannot let primary settings shadow a runner's egress route, which then carries every port."""
    egress = browser_egress(runtime_env, worker_env, egress_control=True)
    assert egress == BrowserEgress(http=expected, https=expected, by_hostname=True)


_CLASH = _UpstreamProxy(host="127.0.0.1", port=7890, tls=False)


@pytest.mark.parametrize(
    ("env", "expected_http", "expected_https"),
    [
        ({"all_proxy": "http://worker:3128"}, _WORKER, _WORKER),
        ({"http_proxy": "http://worker:3128"}, _WORKER, None),
        ({"https_proxy": "http://worker:3128"}, None, _WORKER),
        ({"all_proxy": "http://worker:3128", "https_proxy": "http://other:3128"}, _WORKER, _OTHER),
        ({"http_proxy": "http://worker:3128", "https_proxy": "http://other:3128"}, _WORKER, _OTHER),
        # A SOCKS all_proxy beside scheme proxies, as proxy clients such as Clash export, is never used.
        (
            {
                "http_proxy": "http://127.0.0.1:7890",
                "https_proxy": "http://127.0.0.1:7890",
                "all_proxy": "socks5://127.0.0.1:7891",
            },
            _CLASH,
            _CLASH,
        ),
        # An unusable proxy is ignored with a warning, so the relay dials those destinations itself.
        ({"all_proxy": "socks5://127.0.0.1:7891"}, None, None),
        ({"http_proxy": "http://worker:3128", "https_proxy": "http://user:pass@worker:3128"}, _WORKER, None),
        ({"auto_proxy": "http://wpad/proxy.pac", "all_proxy": "http://worker:3128"}, _WORKER, _WORKER),
    ],
)
def test_primary_egress_follows_curl_proxy_precedence(
    env: dict[str, str],
    expected_http: _UpstreamProxy | None,
    expected_https: _UpstreamProxy | None,
) -> None:
    """Scheme proxies win over all_proxy, which applies only to schemes without one."""
    egress = browser_egress({}, env, egress_control=False)
    assert (egress.http, egress.https, egress.by_hostname) == (expected_http, expected_https, False)
    assert egress._upstream_for(80) == expected_http
    assert egress._upstream_for(443) == egress._upstream_for(8443) == expected_https


@pytest.mark.parametrize(
    ("worker_env", "message"),
    [
        ({"ALL_PROXY": "socks5://worker:1080"}, "all_proxy names a SOCKS proxy"),
        ({"https_proxy": "socks5h://worker:1080"}, "https_proxy names a SOCKS proxy"),
        ({"ALL_PROXY": "http://user:pass@worker:3128"}, "proxy credentials inside the URL are not supported"),
        ({"ALL_PROXY": "http://worker:3128/path"}, "must not include a path"),
        ({"ALL_PROXY": "ftp://worker:21"}, "http:// or https:// proxy URL"),
        ({"ALL_PROXY": "http://worker:port"}, "not a valid proxy URL"),
        ({"SOCKS_SERVER": "socks5://worker:1080"}, "cannot follow socks_server"),
        ({"auto_proxy": ""}, "cannot follow auto_proxy"),
        ({"http_proxy": "http://one:3128", "https_proxy": "http://two:3128"}, "name different proxies"),
        ({"http_proxy": "http://proxy:3128", "https_proxy": "http://proxy:3129"}, "name different proxies"),
        ({"all_proxy": "http://worker:3128", "https_proxy": "http://other:3128"}, "name different proxies"),
        (
            {
                "http_proxy": "http://127.0.0.1:7890",
                "https_proxy": "http://127.0.0.1:7890",
                "all_proxy": "socks5://127.0.0.1:7891",
            },
            "all_proxy names a SOCKS proxy",
        ),
    ],
)
def test_runner_egress_fails_closed_on_unsupported_or_ambiguous_proxies(
    worker_env: dict[str, str],
    message: str,
) -> None:
    """Where a proxy may enforce approved egress, the browser never guesses or dials around it."""
    with pytest.raises(ValueError, match=message) as refused:
        browser_egress({}, worker_env, egress_control=True)
    assert "pass" not in str(refused.value)


@pytest.mark.parametrize(
    ("host", "address", "expected"),
    [
        ("api.corp.example", "8.8.8.8", True),
        ("corp.example", "8.8.8.8", True),
        ("evilcorp.example", "8.8.8.8", False),
        ("printer", "8.8.8.8", True),
        ("host.example", "192.168.1.5", True),
        ("service.example", "10.9.9.9", True),
        ("service.example", "::ffff:10.9.9.9", True),
        ("service.example", "11.0.0.1", False),
        ("v6.example", "fd00::5", True),
    ],
)
def test_no_proxy_matches_names_by_suffix_and_addresses_by_range(host: str, address: str, expected: bool) -> None:
    """NO_PROXY decides only whether an already validated destination skips the upstream proxy."""
    egress = BrowserEgress(no_proxy=(".corp.example", "printer", "10.0.0.0/8", "192.168.1.5", "[fd00::5]"))
    assert egress._bypasses(host, ipaddress.ip_address(address)) is expected
    assert BrowserEgress(no_proxy=("*",))._bypasses(host, ipaddress.ip_address(address)) is True


class _RecordingUpstream:
    """An HTTP proxy that records CONNECT requests and echoes tunneled bytes when it accepts one."""

    def __init__(self, *, accept: bool = True) -> None:
        self.requests: list[bytes] = []
        self._accept = accept
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> _UpstreamProxy:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return _UpstreamProxy(host="127.0.0.1", port=self._server.sockets[0].getsockname()[1], tls=False)

    async def __aexit__(self, *_args: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            self.requests.append((await reader.readuntil(b"\r\n\r\n")).split(b"\r\n", 1)[0])
            if not self._accept:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            while data := await reader.read(1024):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()


async def _relay_reply(proxy: BrowserDestinationProxy, host: str, port: int) -> int:
    reader, writer, status = await socks5_connect(proxy.endpoint, host, port)
    if status == 0:
        writer.write(b"tunneled bytes")
        await writer.drain()
        assert await reader.readexactly(14) == b"tunneled bytes"
    writer.close()
    await writer.wait_closed()
    return status


def _resolve_as(monkeypatch: pytest.MonkeyPatch, answers: dict[str, str]) -> None:
    """Answer relay lookups from a table while keeping the real destination policy."""
    real = browser_proxy.validated_connect_addresses

    def validate(host: str, **kwargs: bool | int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        if host in answers:
            real(answers[host], **kwargs)
            return [ipaddress.ip_address(answers[host])]
        return real(host, **kwargs)

    monkeypatch.setattr(browser_proxy, "validated_connect_addresses", validate)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("by_hostname", "expected"),
    [(False, b"CONNECT 8.8.8.8:443"), (True, b"CONNECT public.example:443")],
)
async def test_relay_tunnels_validated_destinations_through_the_upstream_proxy(
    by_hostname: bool,
    expected: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The primary pins the validated address in CONNECT; a runner names the host for its approved-egress proxy."""
    _resolve_as(monkeypatch, {"public.example": "8.8.8.8"})
    recorder = _RecordingUpstream()
    async with recorder as upstream_proxy:
        proxy = BrowserDestinationProxy(
            egress=BrowserEgress(http=upstream_proxy, https=upstream_proxy, by_hostname=by_hostname),
        )
        await proxy.start()
        try:
            assert await _relay_reply(proxy, "public.example", 443) == 0
        finally:
            await proxy.close()
    assert recorder.requests == [expected + b" HTTP/1.1"]


@pytest.mark.asyncio
async def test_relay_refuses_what_policy_denies_before_any_upstream_or_direct_dial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NO_PROXY cannot make metadata, link-local, or rebound loopback destinations reachable."""
    _resolve_as(monkeypatch, {"rebind.example": "127.0.0.1"})
    recorder = _RecordingUpstream()
    async with recorder as upstream_proxy:
        proxy = BrowserDestinationProxy(
            allow_private_networks=True,
            egress=BrowserEgress(
                http=upstream_proxy,
                https=upstream_proxy,
                no_proxy=("169.254.0.0/16", "metadata.google.internal", "fd00:ec2::/32", "::ffff:169.254.0.0/112"),
            ),
        )
        denied = BrowserDestinationProxy(egress=BrowserEgress(http=upstream_proxy, https=upstream_proxy))
        await proxy.start()
        await denied.start()
        try:
            for host in ["169.254.169.254", "metadata.google.internal", "fd00:ec2::254", "::ffff:169.254.169.254"]:
                assert await _relay_reply(proxy, host, 80) != 0
            assert await _relay_reply(denied, "rebind.example", 80) != 0
        finally:
            await proxy.close()
            await denied.close()
    assert recorder.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allow_private_networks", "direct"),
    [(True, True), (False, False)],
)
async def test_no_proxy_hosts_go_direct_only_for_private_network_browsing(
    allow_private_networks: bool,
    direct: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NO_PROXY destination the policy allows is dialed directly only when private browsing is trusted."""
    _resolve_as(monkeypatch, {"docs.corp.example": "8.8.4.4"})
    dialed: list[str] = []
    original_open = asyncio.open_connection

    async def open_connection(
        host: str,
        port: int,
        **kwargs: object,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if host == "8.8.4.4":
            dialed.append(host)
            msg = "fixture refuses the direct dial"
            raise OSError(msg)
        return await original_open(host, port, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    recorder = _RecordingUpstream()
    async with recorder as upstream_proxy:
        proxy = BrowserDestinationProxy(
            allow_private_networks=allow_private_networks,
            egress=BrowserEgress(http=upstream_proxy, https=upstream_proxy, no_proxy=(".corp.example",)),
        )
        await proxy.start()
        try:
            status = await _relay_reply(proxy, "docs.corp.example", 443)
        finally:
            await proxy.close()
    assert (dialed == ["8.8.4.4"]) is direct
    assert (recorder.requests == [b"CONNECT 8.8.4.4:443 HTTP/1.1"]) is not direct
    assert (status == 0) is not direct


@pytest.mark.asyncio
async def test_one_page_cannot_hold_more_than_its_share_of_browser_dns_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slow lookups from one browser stay within its own few slots, so another browser still resolves promptly."""
    release = threading.Event()
    lock = threading.Lock()
    active = [0, 0]
    saturated = asyncio.Event()
    loop = asyncio.get_running_loop()

    def validate(host: str, **_kwargs: bool | int) -> list[ipaddress.IPv4Address]:
        if not host.endswith(".slow.example"):
            return [ipaddress.IPv4Address("127.0.0.1")]
        with lock:
            active[0] += 1
            active[1] = max(active)
            if active[0] == 4:
                loop.call_soon_threadsafe(saturated.set)
        release.wait(5)
        with lock:
            active[0] -= 1
        msg = "fixture nameserver never answered"
        raise OSError(msg)

    monkeypatch.setattr(browser_proxy, "validated_connect_addresses", validate)
    echo = await asyncio.start_server(lambda _reader, writer: writer.close(), "127.0.0.1", 0)
    slow = BrowserDestinationProxy()
    fast = BrowserDestinationProxy(allow_loopback=True)
    await slow.start()
    await fast.start()
    stalled = [asyncio.create_task(socks5_connect(slow.endpoint, f"{index}.slow.example", 80)) for index in range(40)]
    try:
        await asyncio.wait_for(saturated.wait(), 5)
        _reader, writer, status = await asyncio.wait_for(
            socks5_connect(fast.endpoint, "fast.example", echo.sockets[0].getsockname()[1]),
            2,
        )
        assert status == 0
        writer.close()
        await writer.wait_closed()
        assert active[1] == 4
    finally:
        release.set()
        for task in stalled:
            task.cancel()
        await asyncio.gather(*stalled, return_exceptions=True)
        await slow.close()
        await fast.close()
        echo.close()
        await echo.wait_closed()


@pytest.mark.asyncio
async def test_connections_to_one_slow_host_share_its_lookup_and_leave_slots_for_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Many connections to a host that resolves slowly cannot stall the same browser's other destinations."""
    release = threading.Event()
    lookups: list[str] = []
    entered: list[str] = []
    resolve = BrowserDestinationProxy._resolve

    def validate(host: str, **_kwargs: bool | int) -> list[ipaddress.IPv4Address]:
        lookups.append(host)
        if host == "slow.example":
            release.wait(5)
            msg = "fixture nameserver never answered"
            raise OSError(msg)
        return [ipaddress.IPv4Address("127.0.0.1")]

    async def observed_resolve(
        proxy: BrowserDestinationProxy,
        host: str,
        port: int,
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        entered.append(host)
        return await resolve(proxy, host, port)

    monkeypatch.setattr(browser_proxy, "validated_connect_addresses", validate)
    monkeypatch.setattr(BrowserDestinationProxy, "_resolve", observed_resolve)
    echo = await asyncio.start_server(lambda _reader, writer: writer.close(), "127.0.0.1", 0)
    proxy = BrowserDestinationProxy(allow_loopback=True)
    await proxy.start()
    stalled = [asyncio.create_task(socks5_connect(proxy.endpoint, "slow.example", 80)) for _ in range(6)]
    try:
        async with asyncio.timeout(5):
            while len(entered) < len(stalled):  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        _reader, writer, status = await asyncio.wait_for(
            socks5_connect(proxy.endpoint, "fast.example", echo.sockets[0].getsockname()[1]),
            2,
        )
        assert status == 0
        writer.close()
        await writer.wait_closed()
        assert lookups.count("slow.example") == 1
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*stalled), 5)
        assert [result[2] for result in results] == [2] * len(stalled)
        for _reader, stalled_writer, _status in results:
            stalled_writer.close()
            await stalled_writer.wait_closed()
    finally:
        release.set()
        for task in stalled:
            task.cancel()
        await asyncio.gather(*stalled, return_exceptions=True)
        await proxy.close()
        echo.close()
        await echo.wait_closed()


def _tls_contexts(tmp_path: Path) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """Return a TLS server context for 127.0.0.1 and a client context that trusts only its test CA."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "relay test proxy")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "proxy.pem", tmp_path / "proxy.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_path, key_path)
    client = ssl.create_default_context(cafile=str(cert_path))
    return server, client


@pytest.mark.asyncio
async def test_relay_tunnels_through_an_https_egress_proxy_and_survives_client_half_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A TLS connection to the proxy cannot half-close, so a client EOF must not end the tunnel early."""
    _resolve_as(monkeypatch, {"public.example": "8.8.8.8"})
    server_tls, client_tls = _tls_contexts(tmp_path)
    requests: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        requests.append((await reader.readuntil(b"\r\n\r\n")).split(b"\r\n", 1)[0])
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        payload = await reader.readexactly(4)
        writer.write(payload.upper())
        await writer.drain()
        writer.close()

    upstream_server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_tls)
    upstream = _UpstreamProxy(host="127.0.0.1", port=upstream_server.sockets[0].getsockname()[1], tls=True)
    proxy = BrowserDestinationProxy(egress=BrowserEgress(http=upstream, https=upstream))
    proxy._upstream_tls = client_tls
    await proxy.start()
    try:
        reader, writer, status = await socks5_connect(proxy.endpoint, "public.example", 443)
        assert status == 0
        writer.write(b"ping")
        writer.write_eof()
        assert await asyncio.wait_for(reader.readexactly(4), 5) == b"PING"
        assert await asyncio.wait_for(reader.read(), 5) == b""
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.close()
        upstream_server.close()
        await upstream_server.wait_closed()
    assert requests == [b"CONNECT 8.8.8.8:443 HTTP/1.1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("by_hostname", "requirement"),
    [
        (False, "must allow CONNECT to IP addresses on ports 80 and 443"),
        (True, "must allow CONNECT to the allowed hostnames on ports 80 and 443"),
    ],
)
async def test_proxy_refusal_logs_the_connect_requirement(
    by_hostname: bool,
    requirement: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Squid-style refusal of CONNECT to port 80 fails the connection and names what the proxy must allow."""
    _resolve_as(monkeypatch, {"public.example": "8.8.8.8"})
    squid = SquidLikeUpstream(connect_ports=SQUID_DEFAULT_CONNECT_PORTS)
    url = urlsplit(await squid.start())
    upstream = _UpstreamProxy(host=url.hostname, port=url.port, tls=False)
    proxy = BrowserDestinationProxy(egress=BrowserEgress(http=upstream, https=upstream, by_hostname=by_hostname))
    await proxy.start()
    try:
        with capture_logs() as logs:
            assert await _relay_reply(proxy, "public.example", 80) != 0
    finally:
        await proxy.close()
        await squid.close()
    target = b"public.example" if by_hostname else b"8.8.8.8"
    assert squid.requests == [b"CONNECT " + target + b":80 HTTP/1.1"]
    [refusal] = [entry for entry in logs if entry["event"] == "browser_egress_proxy_refused_tunnel"]
    assert refusal["status"] == "403"
    assert requirement in refusal["requirement"]


@pytest.mark.asyncio
async def test_upstream_refusal_fails_the_connection_without_a_direct_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused tunnel is a failed connection, never a reason to dial around the egress proxy."""
    _resolve_as(monkeypatch, {"public.example": "8.8.8.8"})
    dialed: list[str] = []
    original_open = asyncio.open_connection

    async def open_connection(
        host: str,
        port: int,
        **kwargs: object,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if host != "127.0.0.1":
            dialed.append(host)
        return await original_open(host, port, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    recorder = _RecordingUpstream(accept=False)
    async with recorder as upstream_proxy:
        proxy = BrowserDestinationProxy(egress=BrowserEgress(http=upstream_proxy, https=upstream_proxy))
        await proxy.start()
        try:
            assert await _relay_reply(proxy, "public.example", 443) != 0
        finally:
            await proxy.close()
    assert recorder.requests == [b"CONNECT 8.8.8.8:443 HTTP/1.1"]
    assert dialed == []


@pytest.mark.asyncio
async def test_computer_mcp_binding_allows_own_preview_and_blocks_proxy_recursion(tmp_path: Path) -> None:
    """Display binding supplies the same loopback policy to the verifier and TCP relay."""
    paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    toolkit = BrowserMCPTools(runtime_paths=paths)
    toolkit.bind_worker_display(":99", tmp_path / "workspace")
    provider = toolkit._provider
    assert provider is not None
    proxy, verifier = provider._proxy, provider._verifier
    assert proxy is not None
    await proxy.start()
    await verifier.start()
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            headers = {"Authorization": "Bearer " + verifier.token}
            result = await client.post(verifier.endpoint, json={"url": "http://localhost:5173"}, headers=headers)
            assert result.json() == {"allowed": True}
        verifier_url = urlsplit(verifier.endpoint)
        reader, writer, status = await socks5_connect(proxy.endpoint, "127.0.0.1", verifier_url.port)
        try:
            assert status == 0
            writer.write(b"POST /verify HTTP/1.1\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            assert (await reader.read()).startswith(b"HTTP/1.1 403")
        finally:
            writer.close()
            await writer.wait_closed()
        for host in ["127.0.0.1", "::1", "::ffff:127.0.0.1"]:
            reader, writer, status = await socks5_connect(
                proxy.endpoint,
                host,
                urlsplit(proxy.endpoint).port,
                literal=True,
            )
            assert status != 0
            assert await reader.read() == b""
            writer.close()
            await writer.wait_closed()
    finally:
        await toolkit.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["browser", "browser_mcp"])
async def test_computer_binding_chains_the_worker_proxy_behind_the_relay(
    provider: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both providers make the relay Chromium's only proxy, and the relay tunnels through the worker's proxy."""
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:3128")
    monkeypatch.delenv("all_proxy", raising=False)
    paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"all_proxy": "http://127.0.0.1:1"},
    )
    if provider == "browser":
        toolkit = BrowserTools(paths)
        adapter = LifecycleBrowser()
        launch_options: dict[str, object] = {}
        original_launch = adapter.launch_persistent_context

        async def launch(**kwargs: object) -> LifecycleBrowser:
            launch_options.update(kwargs)
            return await original_launch(**kwargs)

        monkeypatch.setattr(adapter, "launch_persistent_context", launch)
        monkeypatch.setattr("mindroom.custom_tools.browser.async_playwright", lambda: adapter)
        toolkit.bind_worker_display(":99", tmp_path / "workspace")
        state = await toolkit._ensure_profile("mindroom")
        relay = state.destination_proxy
        proxy = launch_options["proxy"]
        assert isinstance(proxy, dict)
        server, bypass = proxy["server"], proxy["bypass"]
    else:
        toolkit = BrowserMCPTools(runtime_paths=paths)
        toolkit.bind_worker_display(":99", tmp_path / "workspace")
        assert toolkit._provider is not None
        relay = toolkit._provider._proxy
        await relay.start()
        args = toolkit._provider._server_parameters().args
        server, bypass = args[args.index("--proxy-server") + 1], args[args.index("--proxy-bypass") + 1]
    try:
        assert relay is not None
        assert server == relay.endpoint
        assert bypass == "<-loopback>"
        worker_proxy = _UpstreamProxy(host="127.0.0.1", port=3128, tls=False)
        assert relay._egress == BrowserEgress(http=worker_proxy, https=worker_proxy, by_hostname=True)
    finally:
        await toolkit.aclose()


def test_computer_mcp_browser_launches_with_proxied_webrtc_only(tmp_path: Path) -> None:
    """The pinned Playwright MCP reads Chromium launch arguments from a bundled, valid JSON config."""
    config = json.loads(Path(mcp_provider.__file__).with_name("browser_mcp_config.json").read_text(encoding="utf-8"))
    assert config == {"browser": {"launchOptions": {"args": [PROXIED_WEBRTC_ONLY_ARG]}}}
    dockerfile = (Path(__file__).parents[1] / "local/instances/deploy/Dockerfile.mindroom").read_text(encoding="utf-8")
    assert f"COPY src/mindroom/worker_computer/browser_mcp_config.json ./{Path(COMPUTER_BROWSER_MCP_CONFIG).name}" in (
        dockerfile
    )
    browser = mcp_provider.WorkerBrowserMCP(display=":99", workspace=tmp_path / "workspace", storage_root=tmp_path)
    args = browser._server_parameters().args
    assert args[args.index("--config") + 1] == COMPUTER_BROWSER_MCP_CONFIG


@pytest.mark.asyncio
@pytest.mark.parametrize(("private", "loopback"), [(False, False), (True, False), (False, True)])
async def test_destination_policy_and_owned_tunnel_cleanup(private: bool, loopback: bool) -> None:
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
    proxy = BrowserDestinationProxy(allow_private_networks=private, allow_loopback=loopback)
    await proxy.start()
    try:
        for host, destination_port in [
            ("169.254.169.254", 80),
            ("metadata.google.internal", 80),
            ("127.0.0.1", urlsplit(proxy.endpoint).port),
        ]:
            reader, writer, status = await socks5_connect(proxy.endpoint, host, destination_port)
            assert status != 0
            assert await reader.read() == b""
            writer.close()
            await writer.wait_closed()
        reader, writer, status = await socks5_connect(proxy.endpoint, "127.0.0.1", port)
        assert (status == 0) is (private or loopback)
        if private or loopback:
            writer.write(b"opaque TLS or HTTP bytes")
            await writer.drain()
            assert await reader.readexactly(24) == b"opaque TLS or HTTP bytes"
        else:
            assert not reached.is_set()
        await asyncio.wait_for(proxy.close(), 1)
        assert await reader.read() == b""
        if private or loopback:
            await asyncio.wait_for(disconnected.wait(), 1)
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("loopback", [False, True])
async def test_ipv6_literal_domain_names_follow_destination_policy(loopback: bool) -> None:
    """Chromium sends IPv6 URL hosts unbracketed as SOCKS domain names, which the policy then decides."""
    try:
        server = await asyncio.start_server(lambda _reader, writer: writer.close(), "::1", 0)
    except OSError:
        pytest.skip("IPv6 loopback is unavailable")
    port = server.sockets[0].getsockname()[1]
    proxy = BrowserDestinationProxy(allow_loopback=loopback)
    await proxy.start()
    try:
        for host in ["::1", "0:0:0:0:0:0:0:1"]:
            _reader, writer, status = await socks5_connect(proxy.endpoint, host, port)
            assert (status == 0) is loopback
            writer.close()
            await writer.wait_closed()
        for host in ["fe80::1%eth0", "[::1]", "::1/128"]:
            with pytest.raises(asyncio.IncompleteReadError):
                await socks5_connect(proxy.endpoint, host, port)
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

    def resolve(
        host: str,
        *,
        port: int,
        allow_private_networks: bool,
        allow_loopback: bool,
    ) -> list[ipaddress.IPv4Address]:
        assert not allow_loopback
        assert threading.current_thread().name.startswith("mindroom-browser-dns")
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
        _, writer, status = await socks5_connect(proxy.endpoint, "fixture.example", 443)
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
        _, writer, status = await socks5_connect(proxy.endpoint, host, port, literal=True)
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

    def validate(host: str, *, port: int, allow_private_networks: bool, allow_loopback: bool) -> object:
        try:
            return original_validate(
                host,
                port=port,
                allow_private_networks=allow_private_networks,
                allow_loopback=allow_loopback,
            )
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
        result.args[result.args.index("--config") + 1] = str(
            Path(mcp_provider.__file__).with_name("browser_mcp_config.json"),
        )
        return result

    monkeypatch.setattr(asyncio, "open_connection", dial)
    monkeypatch.setattr(browser_proxy, "validated_connect_addresses", validate)
    monkeypatch.setattr(mcp_provider, "COMPUTER_BROWSER_MCP_SERVER", cli)
    monkeypatch.setattr(mcp_provider, "COMPUTER_BROWSER_EXECUTABLE", executable)
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
