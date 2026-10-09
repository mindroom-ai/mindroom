"""Tests for TLS interception: per-request credential injection inside CONNECT tunnels to hosts with rules."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import ssl
import time
from typing import TYPE_CHECKING

import aiohttp
import pytest

from mindroom.config.egress_broker import EgressAuth, EgressBrokerConfig, EgressRule, EgressService
from mindroom.egress_broker import DialPolicy, materialize_ca_bundle
from tests.egress_broker.conftest import audit_records, connect_request, proxy_authorization, read_raw_response

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import httpx

    from mindroom.egress_broker import AuditLog
    from tests.egress_broker.conftest import BrokerFactory, Upstream, UpstreamCA

    ProxyClient = Callable[..., httpx.AsyncClient]

MANAGE_URL = "https://m.test/connections/egress"
SECRET = "s3cret"  # noqa: S105 - test credential the fake upstream expects
_PROXY_ENV = {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "git_askpass", "ssh_askpass"}


def _config(
    host: str = "localhost",
    *,
    path_prefix: str = "/",
    auth: EgressAuth | None = None,
) -> EgressBrokerConfig:
    rule = EgressRule(host=host, path_prefix=path_prefix, auth=auth or EgressAuth(type="bearer"))
    return EgressBrokerConfig(services={"svc": EgressService(rules=[rule])})


async def _open_tunnel(
    broker: BrokerFactory,
    target: str,
    *,
    verify: ssl.SSLContext | None = None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """CONNECT through the latest broker and start TLS for ``localhost`` inside the tunnel, as a worker would."""
    reader, writer = await asyncio.open_connection("127.0.0.1", broker.latest.port)
    writer.write(connect_request(target, authorization=proxy_authorization(broker.token())))
    assert (await read_raw_response(reader)).status == 200
    await writer.start_tls(
        verify or ssl.create_default_context(cadata=broker.ca.cert_pem),
        server_hostname="localhost",
    )
    return reader, writer


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def _run(*command: str, env: dict[str, str]) -> tuple[int, str, str]:
    """Run a command line tool while the broker keeps serving on this event loop."""
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    async with asyncio.timeout(30):
        stdout, stderr = await process.communicate()
    assert process.returncode is not None
    return process.returncode, stdout.decode(), stderr.decode()


def _env_without_proxies(**extra: str) -> dict[str, str]:
    """Return this process's environment without proxy or askpass settings that would bypass the broker."""
    return {name: value for name, value in os.environ.items() if name.lower() not in _PROXY_ENV} | extra


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["localhost", "LOCALHOST.", "localhost."])
async def test_connect_to_rule_host_is_intercepted(broker: BrokerFactory, host: str) -> None:
    """A host with rules (case and one trailing dot normalized) gets the broker's certificate, not a blind tunnel."""
    await broker(_config(), secrets={"svc": SECRET})

    # The client trusts only the broker CA, so the handshake alone proves the broker answered.
    _reader, writer = await _open_tunnel(broker, f"{host}:443")
    try:
        assert ("DNS", "localhost") in writer.get_extra_info("peercert")["subjectAltName"]
    finally:
        writer.transport.abort()
    assert broker.resolved == []


@pytest.mark.asyncio
async def test_injects_bearer_and_client_never_sees_secret(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """The upstream receives the injected secret; nothing the broker relays back to the client contains it."""
    await broker(_config(), secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/ok?page=2"))

    assert response.status_code == 200
    [seen] = tls_upstream.requests
    assert seen.headers["authorization"] == [f"Bearer {SECRET}"]
    assert "proxy-authorization" not in seen.headers
    assert SECRET not in response.text
    assert not [value for value in response.headers.values() if SECRET in value]
    [record] = await audit_records(audit, 1)
    assert (record.kind, record.method, record.host, record.path, record.service, record.status) == (
        "request",
        "GET",
        "localhost",
        "/ok",
        "svc",
        200,
    )


@pytest.mark.asyncio
async def test_placeholder_authorization_is_replaced(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """A client's placeholder Authorization is removed, so the upstream sees exactly one injected value."""
    await broker(_config(), secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/echo"), headers={"Authorization": "token mindroom-brokered"})

    assert response.json()["headers"]["authorization"] == [f"Bearer {SECRET}"]


@pytest.mark.asyncio
async def test_keepalive_injects_per_request(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """Each request on one tunnel is matched on its own path, over a single upstream connection."""
    await broker(_config(path_prefix="/echo"), secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    assert (await client.get(tls_upstream.url("/echo"))).status_code == 200
    assert (await client.get(tls_upstream.url("/cookie"))).status_code == 200

    first, second = tls_upstream.requests
    assert first.headers["authorization"] == [f"Bearer {SECRET}"]
    assert "authorization" not in second.headers
    assert first.peer_port == second.peer_port
    records = await audit_records(audit, 2)
    assert {record.path: (record.kind, record.service) for record in records} == {
        "/echo": ("request", "svc"),
        "/cookie": ("request", None),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host_header",
    ["evil.example", "localhost.evil.example", "localhost:evil.example", "user@localhost", "[::1]"],
)
async def test_host_mismatch_is_refused(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
    host_header: str,
) -> None:
    """A Host naming anything but the CONNECT host is refused before any secret lookup (no domain fronting)."""
    await broker(_config(), secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/echo"), headers={"Host": host_header})

    assert response.status_code == 403
    assert response.headers["connection"] == "close"
    assert response.json() == {"error": "host_mismatch"}
    assert tls_upstream.hits == []
    assert broker.resolved == []
    [record] = await audit_records(audit, 1)
    assert (record.kind, record.status, record.host, record.path) == ("denied", 403, "localhost", "/echo")


@pytest.mark.asyncio
@pytest.mark.parametrize("host_header", ["LOCALHOST.:{port}", "localhost"])
async def test_matching_host_header_is_forwarded(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    host_header: str,
) -> None:
    """A Host naming the CONNECT host (any case, port, or trailing dot) reaches the upstream unchanged."""
    await broker(_config(), secrets={"svc": SECRET})
    client = proxy_client(broker.token())
    sent = host_header.format(port=tls_upstream.port)

    response = await client.get(tls_upstream.url("/echo"), headers={"Host": sent})

    assert response.status_code == 200
    echoed = response.json()["headers"]
    assert echoed["host"] == [sent]
    assert echoed["authorization"] == [f"Bearer {SECRET}"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "target"),
    [("GET", "https://localhost/echo"), ("CONNECT", "evil.example:443"), ("OPTIONS", "*")],
    ids=["absolute-form", "authority-form", "asterisk-form"],
)
async def test_non_origin_form_target_is_refused(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    audit: AuditLog,
    method: str,
    target: str,
) -> None:
    """Inside a tunnel only origin-form targets are served; the CONNECT target alone decides the destination."""
    await broker(_config(), secrets={"svc": SECRET})
    reader, writer = await _open_tunnel(broker, f"localhost:{tls_upstream.port}")
    try:
        writer.write(f"{method} {target} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        response = await read_raw_response(reader)
    finally:
        writer.transport.abort()

    assert response.status == 400
    assert response.json() == {"error": "bad_request"}
    assert tls_upstream.hits == []
    assert broker.resolved == []
    [record] = await audit_records(audit, 1)
    assert (record.kind, record.status, record.path) == ("denied", 400, "")


@pytest.mark.asyncio
async def test_redirect_is_returned_not_followed(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """A redirect reaches the client as-is; following it would be a new CONNECT and a new match."""
    await broker(_config(), secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/redirect"))

    assert response.status_code == 302
    assert response.headers["location"] == "https://other.invalid/"
    assert tls_upstream.hits == ["/redirect"]


@pytest.mark.asyncio
async def test_streamed_response_is_incremental(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """Response chunks are relayed as they arrive instead of after the whole body."""
    await broker(_config(), secrets={"svc": SECRET})
    client = proxy_client(broker.token())
    chunks: list[bytes] = []
    arrivals: list[float] = []

    async with client.stream("GET", tls_upstream.url("/stream")) as response:
        async for chunk in response.aiter_raw():
            chunks.append(chunk)
            arrivals.append(time.monotonic())

    assert b"".join(chunks) == b"chunk-0\nchunk-1\nchunk-2\n"
    assert arrivals[-1] - arrivals[0] >= 0.15


@pytest.mark.asyncio
async def test_set_cookie_stripped(broker: BrokerFactory, tls_upstream: Upstream, proxy_client: ProxyClient) -> None:
    """Brokered hosts never set cookies in the worker, so sessions cannot outlive the injected credential."""
    await broker(_config(), secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/cookie"))

    assert response.status_code == 200
    assert response.text == "ok"
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_missing_secret_returns_403_with_manage_url(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A matched service without a secret in the worker's scope tells the agent where to set one."""
    await broker(_config(), manage_url=lambda _claims: MANAGE_URL)
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/echo"))

    assert response.status_code == 403
    assert response.headers["connection"] == "close"
    assert response.json() == {"error": "credential_not_configured", "service": "svc", "manage_url": MANAGE_URL}
    assert tls_upstream.hits == []
    [record] = await audit_records(audit, 1)
    assert (record.kind, record.status, record.service, record.path) == ("denied", 403, "svc", "/echo")


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["resolve_secret", "manage_url"])
async def test_resolve_secret_failure_gets_502_and_audit_row(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
    failing: str,
) -> None:
    """A failing secret store or manage-link callback fails the request with 502, never the tunnel listener."""

    def fail(*_args: object) -> str:
        msg = "store unavailable"
        raise RuntimeError(msg)

    if failing == "resolve_secret":
        await broker(_config(), resolve_secret=fail)
    else:
        await broker(_config(), manage_url=fail)
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/echo?page=2"))
    following = await client.get(tls_upstream.url("/echo"))

    assert (response.status_code, response.json()) == (502, {"error": "broker_error"})
    assert following.status_code == 502
    assert tls_upstream.hits == []
    records = await audit_records(audit, 2)
    assert {(record.kind, record.status, record.service, record.path) for record in records} == {
        ("request", 502, "svc", "/echo"),
    }


@pytest.mark.asyncio
async def test_expect_continue_upload(broker: BrokerFactory, tls_upstream: Upstream) -> None:
    """The broker answers ``Expect: 100-continue`` itself and then streams the whole body upstream."""
    await broker(_config(), secrets={"svc": SECRET})
    size = 2 * 1024 * 1024
    reader, writer = await _open_tunnel(broker, f"localhost:{tls_upstream.port}")
    try:
        writer.write(
            (
                f"POST /upload HTTP/1.1\r\nHost: localhost:{tls_upstream.port}\r\n"
                f"Content-Length: {size}\r\nExpect: 100-continue\r\n\r\n"
            ).encode(),
        )
        interim = await read_raw_response(reader)
        writer.write(b"x" * size)
        final = await read_raw_response(reader)
    finally:
        writer.transport.abort()

    assert interim.status == 100
    assert final.status == 200
    assert final.json() == {"length": size}
    [seen] = tls_upstream.requests
    assert seen.headers["authorization"] == [f"Bearer {SECRET}"]
    assert "expect" not in seen.headers


@pytest.mark.asyncio
async def test_body_over_limit_gets_413(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """A declared body over the broker limit is refused before the upstream sees anything."""
    await broker(_config(), secrets={"svc": SECRET}, max_body_bytes=1024)
    client = proxy_client(broker.token())

    response = await client.post(tls_upstream.url("/upload"), content=b"x" * 2048)

    assert response.status_code == 413
    assert response.json() == {"error": "request_body_too_large"}
    assert tls_upstream.hits == []


@pytest.mark.asyncio
async def test_upstream_unreachable_gets_502(
    broker: BrokerFactory,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A refused upstream connection inside an intercepted tunnel is a 502 for that request."""
    await broker(_config(), secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    response = await client.get(f"https://localhost:{_closed_port()}/echo")

    assert response.status_code == 502
    assert response.json() == {"error": "upstream_unreachable"}
    [record] = await audit_records(audit, 1)
    assert (record.kind, record.status, record.path, record.service) == ("request", 502, "/echo", "svc")


@pytest.mark.asyncio
async def test_untrusted_upstream_certificate_gets_502(broker: BrokerFactory, proxy_client: ProxyClient) -> None:
    """The broker verifies the real upstream; one it cannot verify never receives the secret."""
    await broker(_config(), secrets={"svc": SECRET})
    received: list[bytes] = []

    async def record(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.read())
        writer.close()

    # Signed by the broker CA, which the broker's upstream context does not trust.
    upstream = await asyncio.start_server(record, "127.0.0.1", 0, ssl=broker.ca.server_context("localhost"))
    try:
        client = proxy_client(broker.token())
        response = await client.get(f"https://localhost:{upstream.sockets[0].getsockname()[1]}/echo")
    finally:
        upstream.close()
        await upstream.wait_closed()

    assert response.status_code == 502
    assert response.json() == {"error": "upstream_unreachable"}
    # The handshake failed before the upstream's handler ran, so not even a request head reached it.
    assert received == []


@pytest.mark.asyncio
async def test_blocked_upstream_gets_403(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """The dial guard covers intercepted upstreams: the default policy refuses loopback."""
    await broker(_config(), secrets={"svc": SECRET}, dial_policy=DialPolicy())
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/echo"))

    assert response.status_code == 403
    assert response.json() == {"error": "destination_blocked"}
    assert tls_upstream.hits == []
    [record] = await audit_records(audit, 1)
    assert (record.kind, record.status) == ("denied", 403)


@pytest.mark.asyncio
async def test_websocket_upgrade_spliced_with_injection(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    audit: AuditLog,
) -> None:
    """The websocket handshake is injected, then frames pass through untouched in both directions."""
    started = await broker(_config(), secrets={"svc": SECRET})
    url = tls_upstream.url("/ws").replace("https://", "wss://")

    async with (
        aiohttp.ClientSession() as session,
        session.ws_connect(
            url,
            proxy=f"http://127.0.0.1:{started.port}",
            proxy_auth=aiohttp.BasicAuth(broker.token(), ""),
            ssl=ssl.create_default_context(cadata=broker.ca.cert_pem),
        ) as socket_,
    ):
        await socket_.send_str("hello")
        assert await socket_.receive_str() == "hello"
        await socket_.send_bytes(b"\x00\x01")
        assert await socket_.receive_bytes() == b"\x00\x01"

    [seen] = tls_upstream.requests
    assert seen.headers["authorization"] == [f"Bearer {SECRET}"]
    [record] = await audit_records(audit, 1)
    assert (record.kind, record.status, record.path, record.service) == ("request", 101, "/ws", "svc")
    assert record.bytes_up > 0
    assert record.bytes_down > 0


@pytest.mark.asyncio
async def test_websocket_splice_ends_when_client_closes(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    audit: AuditLog,
) -> None:
    """Once the client goes away the upstream is closed too, even though the upstream would keep waiting."""
    await broker(_config(), secrets={"svc": SECRET})
    reader, writer = await _open_tunnel(broker, f"localhost:{tls_upstream.port}")
    writer.write(
        (
            f"GET /ws HTTP/1.1\r\nHost: localhost:{tls_upstream.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode(),
    )
    switched = await read_raw_response(reader)

    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()

    assert switched.status == 101
    assert switched.headers["upgrade"].lower() == "websocket"
    # The tunnel is audited only after the splice ends, so the record proves the upstream side was closed.
    [record] = await audit_records(audit, 1)
    assert (record.kind, record.status) == ("request", 101)


@pytest.mark.asyncio
async def test_query_auth_type(broker: BrokerFactory, tls_upstream: Upstream, proxy_client: ProxyClient) -> None:
    """Query auth replaces the client's parameter value and keeps the others."""
    await broker(_config(auth=EgressAuth(type="query", name="key")), secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/echo?key=client-value&page=2"))

    assert response.json()["query"] == {"key": SECRET, "page": "2"}


@pytest.mark.asyncio
async def test_upstream_connection_is_reused_until_upstream_closes(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """A tunnel keeps one upstream connection and dials a new one after the upstream answers ``Connection: close``."""
    await broker(_config(), secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    for path in ("/echo", "/echo", "/close", "/echo"):
        response = await client.get(tls_upstream.url(path))
        assert response.status_code == 200
        assert "connection" not in response.headers

    ports = [seen.peer_port for seen in tls_upstream.requests]
    assert ports[0] == ports[1] == ports[2] != ports[3]
    assert {seen.headers["authorization"][0] for seen in tls_upstream.requests} == {f"Bearer {SECRET}"}


@pytest.mark.asyncio
async def test_upstream_closed_between_requests_is_redialed(
    broker: BrokerFactory,
    upstream_ca: UpstreamCA,
    tmp_path: Path,
) -> None:
    """An upstream that closes an idle connection without saying so gets a fresh connection for the next request."""
    connections = 0
    closed = asyncio.Event()

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal connections
        connections += 1
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        closed.set()

    upstream = await asyncio.start_server(respond, "127.0.0.1", 0, ssl=upstream_ca.server_context(tmp_path))
    port = upstream.sockets[0].getsockname()[1]
    await broker(_config(), secrets={"svc": SECRET})
    reader, writer = await _open_tunnel(broker, f"localhost:{port}")
    request = f"GET /a HTTP/1.1\r\nHost: localhost:{port}\r\n\r\n".encode()
    try:
        writer.write(request)
        first = await read_raw_response(reader)
        async with asyncio.timeout(5):
            await closed.wait()
        writer.write(request)
        second = await read_raw_response(reader)
    finally:
        writer.transport.abort()
        upstream.close()
        await upstream.wait_closed()

    assert (first.status, second.status) == (200, 200)
    assert second.body == b"ok"
    assert connections == 2


@pytest.mark.asyncio
async def test_client_connection_close_ends_tunnel(broker: BrokerFactory, tls_upstream: Upstream) -> None:
    """A client's ``Connection: close`` is honored: the broker answers and then closes the tunnel."""
    await broker(_config(), secrets={"svc": SECRET})
    reader, writer = await _open_tunnel(broker, f"localhost:{tls_upstream.port}")
    try:
        writer.write(f"GET /echo HTTP/1.1\r\nHost: localhost:{tls_upstream.port}\r\nConnection: close\r\n\r\n".encode())
        response = await read_raw_response(reader)
        async with asyncio.timeout(5):
            remaining = await reader.read()
    finally:
        writer.transport.abort()

    assert response.status == 200
    assert response.headers["connection"] == "close"
    assert remaining == b""


@pytest.mark.asyncio
async def test_open_tunnel_follows_config_changes(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """Rules are read per request, so a config change applies to tunnels that are already open."""
    configs = [_config()]
    await broker(config_provider=lambda: configs[-1], secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    first = await client.get(tls_upstream.url("/echo"))
    configs.append(EgressBrokerConfig(unmatched_hosts="deny", services=_config("api.github.com").services))
    second = await client.get(tls_upstream.url("/echo"))

    assert first.json()["headers"]["authorization"] == [f"Bearer {SECRET}"]
    assert second.status_code == 403
    assert second.json() == {"error": "host_not_allowed", "services": ["svc"]}
    assert tls_upstream.hits == ["/echo"]


@pytest.mark.asyncio
async def test_client_rejecting_broker_ca_closes_without_secret_or_denial(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    upstream_ca: UpstreamCA,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A client that does not trust the broker CA just fails its handshake: no secret lookup, no denied row."""
    started = await broker(_config(), secrets={"svc": SECRET})
    reader, writer = await asyncio.open_connection("127.0.0.1", started.port)
    writer.write(connect_request("localhost:443", authorization=proxy_authorization(broker.token())))
    assert (await read_raw_response(reader)).status == 200

    with pytest.raises(ssl.SSLCertVerificationError):
        await writer.start_tls(upstream_ca.client_context(), server_hostname="localhost")
    writer.transport.abort()

    assert broker.resolved == []
    # The listener keeps serving, and the request below is the only audited event.
    response = await proxy_client(broker.token()).get(tls_upstream.url("/echo"))
    assert response.status_code == 200
    records = await audit_records(audit, 1)
    assert [(record.kind, record.path) for record in records] == [("request", "/echo")]


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("curl") is None, reason="curl not installed")
async def test_curl_through_broker(broker: BrokerFactory, tls_upstream: Upstream, tmp_path: Path) -> None:
    """Curl with only the proxy URL and the trust bundle reaches the upstream with the secret injected."""
    started = await broker(_config(), secrets={"svc": SECRET})
    bundle, _ca_only = materialize_ca_bundle(broker.ca.cert_pem, tmp_path / "trust")

    returncode, stdout, stderr = await _run(
        "curl",
        "-q",
        "-sS",
        "--fail",
        "--proxy",
        f"http://{broker.token()}:@127.0.0.1:{started.port}",
        "--cacert",
        str(bundle),
        tls_upstream.url("/echo"),
        env=_env_without_proxies(),
    )

    assert returncode == 0, stderr
    assert json.loads(stdout)["headers"]["authorization"] == [f"Bearer {SECRET}"]


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_git_ls_remote_basic_auth(broker: BrokerFactory, tls_upstream: Upstream, tmp_path: Path) -> None:
    """git, with no credentials configured anywhere, authenticates through the injected Basic header."""
    started = await broker(
        _config(auth=EgressAuth(type="basic", username="x-access-token")),
        secrets={"svc": SECRET},
    )
    bundle, _ca_only = materialize_ca_bundle(broker.ca.cert_pem, tmp_path / "trust")

    returncode, stdout, stderr = await _run(
        "git",
        "-c",
        f"http.proxy=http://{broker.token()}:@127.0.0.1:{started.port}",
        "-c",
        f"http.sslCAInfo={bundle}",
        "-c",
        "credential.helper=",
        "ls-remote",
        tls_upstream.url("/repo.git"),
        env=_env_without_proxies(
            HOME=str(tmp_path),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
        ),
    )

    assert returncode == 0, stderr
    assert f"{'1' * 40}\trefs/heads/main" in stdout
