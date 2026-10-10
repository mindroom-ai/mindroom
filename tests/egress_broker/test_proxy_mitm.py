"""Tests for TLS interception: per-request credential injection inside CONNECT tunnels to hosts with rules."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import ssl
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import aiohttp
import certifi
import pytest
from cryptography.hazmat.primitives import serialization
from structlog.testing import capture_logs

from mindroom.config.egress_broker import EgressAuth, EgressBrokerConfig, EgressRule, EgressService
from mindroom.egress_broker.ca import materialize_ca_bundle
from mindroom.egress_broker.dial import DialPolicy
from mindroom.egress_broker.mitm import _verifying_context
from mindroom.egress_broker.secrets import Secret, SecretMissing, SecretNeedsReconnect, SecretUnavailable
from tests.egress_broker.conftest import (
    DEFAULT_CLAIMS,
    audit_records,
    connect_request,
    proxy_authorization,
    read_raw_response,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import httpx

    from mindroom.egress_broker.audit import AuditLog
    from mindroom.egress_broker.tokens import WorkerClaims
    from tests.egress_broker.conftest import BrokerFactory, Upstream, UpstreamCA

    ProxyClient = Callable[..., httpx.AsyncClient]

MANAGE_URL = "https://m.test/connections/egress"
CONNECT_URL = "https://m.test/api/oauth/github/authorize?connect_token=one-time-token"
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


def _websocket_handshake(port: int, *, connection: str = "Upgrade", upgrade: str = "websocket") -> bytes:
    return (
        f"GET /ws HTTP/1.1\r\nHost: localhost:{port}\r\n"
        f"Upgrade: {upgrade}\r\nConnection: {connection}\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode()


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
@pytest.mark.parametrize(
    "target",
    [
        "/private/../echo",
        "/private/%2e%2e/echo",
        "/private/%2E%2e/echo",
        "/private/%252e%252e/echo",
        "/private//echo",
        "/private\\..\\echo",
    ],
    ids=["dot-dot", "encoded", "mixed-case", "double-encoded", "empty-segment", "backslash"],
)
async def test_ambiguous_path_on_rule_host_is_refused(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    audit: AuditLog,
    target: str,
) -> None:
    """A path an upstream could resolve differently is refused before matching, so it cannot borrow a rule."""
    await broker(_config(path_prefix="/private"), secrets={"svc": SECRET})
    reader, writer = await _open_tunnel(broker, f"localhost:{tls_upstream.port}")
    try:
        writer.write(f"GET {target}?page=2 HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        response = await read_raw_response(reader)
    finally:
        writer.transport.abort()

    assert response.status == 400
    assert response.headers["connection"] == "close"
    assert response.json() == {"error": "bad_request"}
    assert tls_upstream.hits == []
    assert broker.resolved == []
    [record] = await audit_records(audit, 1)
    assert (record.kind, record.status, record.path, record.service) == ("denied", 400, target, None)


@pytest.mark.asyncio
async def test_restrict_to_rules_refuses_unlisted_paths_in_tunnel(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A restricted service's host serves only listed paths; others get 403 instead of an unauthenticated forward."""
    rule = EgressRule(host="localhost", path_prefix="/echo", auth=EgressAuth(type="bearer"))
    await broker(
        EgressBrokerConfig(services={"svc": EgressService(restrict_to_rules=True, rules=[rule])}),
        secrets={"svc": SECRET},
    )
    client = proxy_client(broker.token())

    allowed = await client.get(tls_upstream.url("/echo"))
    refused = await client.get(tls_upstream.url("/ok?page=2"))

    assert allowed.status_code == 200
    assert allowed.json()["headers"]["authorization"] == [f"Bearer {SECRET}"]
    assert refused.status_code == 403
    assert refused.headers["connection"] == "close"
    assert refused.json() == {"error": "path_not_allowed"}
    assert tls_upstream.hits == ["/echo"]
    assert broker.resolved == ["svc"]
    records = await audit_records(audit, 2)
    assert {record.path: (record.kind, record.status, record.service) for record in records} == {
        "/echo": ("request", 200, "svc"),
        "/ok": ("denied", 403, None),
    }


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
async def test_missing_secret_offers_oauth_connect_link(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A service whose OAuth provider can be connected names it and the link beside the key page."""
    await broker(
        _config(),
        resolve_secret=lambda _claims, _service: SecretMissing(provider="github", connect_url=CONNECT_URL),
        manage_url=lambda _claims: MANAGE_URL,
    )
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/echo"))

    assert response.status_code == 403
    assert response.json() == {
        "error": "credential_not_configured",
        "service": "svc",
        "manage_url": MANAGE_URL,
        "provider": "github",
        "connect_url": CONNECT_URL,
    }
    assert tls_upstream.hits == []
    [record] = await audit_records(audit, 1)
    assert (record.kind, record.status, record.service) == ("denied", 403, "svc")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            SecretNeedsReconnect(provider="github", connect_url=CONNECT_URL),
            {"error": "oauth_connection_required", "service": "svc", "provider": "github", "connect_url": CONNECT_URL},
        ),
        (
            SecretNeedsReconnect(provider="github", reset_required=True),
            {
                "error": "oauth_connection_required",
                "service": "svc",
                "provider": "github",
                "connect_url": None,
                "reset_required": True,
            },
        ),
    ],
)
async def test_oauth_reconnect_required_gets_403_without_reaching_upstream(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
    result: SecretNeedsReconnect,
    expected: dict[str, object],
) -> None:
    """A connection that cannot supply a token tells the agent to reconnect, or to reset first, and is audited."""
    await broker(_config(), resolve_secret=lambda _claims, _service: result, manage_url=lambda _claims: MANAGE_URL)
    client = proxy_client(broker.token())

    with capture_logs() as logs:
        response = await client.get(tls_upstream.url("/echo"))

    assert response.status_code == 403
    assert response.json() == expected
    assert tls_upstream.hits == []
    [record] = await audit_records(audit, 1)
    assert (record.kind, record.status, record.service, record.path) == ("denied", 403, "svc", "/echo")
    assert "one-time-token" not in repr(logs)


@pytest.mark.asyncio
async def test_oauth_refresh_outage_gets_retryable_503(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A provider outage is a retryable 503 that names the service and provider, with no reconnect prompt."""
    await broker(
        _config(),
        resolve_secret=lambda _claims, _service: SecretUnavailable(provider="github"),
        manage_url=lambda _claims: MANAGE_URL,
    )
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/echo"))

    assert response.status_code == 503
    assert response.json() == {"error": "oauth_refresh_failed", "service": "svc", "provider": "github"}
    assert tls_upstream.hits == []
    [record] = await audit_records(audit, 1)
    assert (record.kind, record.status, record.service, record.path) == ("request", 503, "svc", "/echo")


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
    writer.write(_websocket_handshake(tls_upstream.port))
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
    await broker(config_provider=lambda _claims: configs[-1], secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    first = await client.get(tls_upstream.url("/echo"))
    configs.append(EgressBrokerConfig(unmatched_hosts="deny", services=_config("api.github.com").services))
    second = await client.get(tls_upstream.url("/echo"))

    assert first.json()["headers"]["authorization"] == [f"Bearer {SECRET}"]
    assert second.status_code == 403
    assert second.json() == {"error": "host_not_allowed", "services": ["svc"]}
    assert tls_upstream.hits == ["/echo"]


@pytest.mark.asyncio
async def test_rules_come_from_the_requesters_own_config(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    upstream_ca: UpstreamCA,
) -> None:
    """The broker reads rules for each requester's verified claims: only Alice's config intercepts the host."""
    alice = DEFAULT_CLAIMS
    bob = replace(DEFAULT_CLAIMS, worker_key="worker-bob-code", requester_id="@bob:example.org")
    seen: list[str | None] = []

    def per_requester(claims: WorkerClaims) -> EgressBrokerConfig:
        seen.append(claims.requester_id)
        return _config() if claims == alice else EgressBrokerConfig()

    await broker(config_provider=per_requester, secrets={"svc": SECRET})
    alice_response = await proxy_client(broker.token(alice)).get(tls_upstream.url("/echo"))
    # Bob's CONNECT is a blind tunnel, so his client sees the upstream's own certificate.
    bob_client = proxy_client(broker.token(bob), verify=upstream_ca.client_context())
    bob_response = await bob_client.get(tls_upstream.url("/echo"))

    assert alice_response.json()["headers"]["authorization"] == [f"Bearer {SECRET}"]
    assert bob_response.status_code == 200
    assert "authorization" not in bob_response.json()["headers"]
    # Alice's CONNECT and her request each read her config; Bob's CONNECT reads his.
    assert seen == [alice.requester_id, alice.requester_id, bob.requester_id]


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


@pytest.mark.asyncio
async def test_bytes_sent_before_connect_answer_get_400(broker: BrokerFactory) -> None:
    """Bytes sent with the CONNECT, before the broker answered it, cannot start TLS and are refused."""
    started = await broker(_config(), secrets={"svc": SECRET})
    reader, writer = await asyncio.open_connection("127.0.0.1", started.port)
    try:
        writer.write(
            connect_request("localhost:443", authorization=proxy_authorization(broker.token()))
            + b"\x16\x03\x01\x00\x05early",
        )
        response = await read_raw_response(reader)
    finally:
        writer.transport.abort()

    assert response.status == 400
    assert response.json() == {"error": "bad_request"}
    assert broker.resolved == []


@pytest.mark.asyncio
async def test_client_hello_sent_before_connect_answer_still_reaches_tls(
    broker: BrokerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ClientHello that arrives while the broker is still preparing its 200 is handed to the TLS handshake."""
    started = await broker(_config(), secrets={"svc": SECRET})
    preparing, release = threading.Event(), threading.Event()
    server_context = broker.ca.server_context

    def slow_server_context(host: str) -> ssl.SSLContext:
        preparing.set()
        release.wait(5)
        return server_context(host)

    monkeypatch.setattr(broker.ca, "server_context", slow_server_context)
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = ssl.create_default_context(cadata=broker.ca.cert_pem).wrap_bio(
        incoming,
        outgoing,
        server_hostname="localhost",
    )
    reader, writer = await asyncio.open_connection("127.0.0.1", started.port)
    try:
        writer.write(connect_request("localhost:443", authorization=proxy_authorization(broker.token())))
        assert await asyncio.to_thread(preparing.wait, 5)
        with contextlib.suppress(ssl.SSLWantReadError):
            tls.do_handshake()
        writer.write(outgoing.read())
        # Give the broker's event loop the chance to read the early ClientHello before the leaf is ready.
        await asyncio.sleep(0.05)
        release.set()
        async with asyncio.timeout(5):
            assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
            while True:
                try:
                    tls.do_handshake()
                    break
                except ssl.SSLWantReadError:
                    writer.write(outgoing.read())
                    data = await reader.read(65536)
                    assert data
                    incoming.write(data)
        writer.write(outgoing.read())
    finally:
        release.set()
        writer.transport.abort()

    assert ("DNS", "localhost") in tls.getpeercert()["subjectAltName"]


@pytest.mark.asyncio
async def test_secret_with_line_break_gets_502_without_leaking(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A stored secret that is not a valid header value fails the request without reaching logs or the upstream."""
    await broker(_config(), resolve_secret=lambda _claims, _service: Secret(f"{SECRET}\r\nX-Injected: y"))
    client = proxy_client(broker.token())

    with capture_logs() as logs:
        response = await client.get(tls_upstream.url("/echo"))

    assert (response.status_code, response.json()) == (502, {"error": "broker_error"})
    assert tls_upstream.hits == []
    [record] = await audit_records(audit, 1)
    assert (record.kind, record.status, record.service) == ("request", 502, "svc")
    assert {"event": "egress_broker_secret_unusable", "log_level": "warning", "service": "svc"} in logs
    assert SECRET not in repr(logs)


@pytest.mark.parametrize("trust_env", [None, "SSL_CERT_FILE", "SSL_CERT_DIR"])
def test_default_upstream_context_adds_certifi_unless_trust_is_configured(
    monkeypatch: pytest.MonkeyPatch,
    upstream_ca: UpstreamCA,
    tmp_path: Path,
    trust_env: str | None,
) -> None:
    """Without a configured trust store certifi's roots are added; SSL_CERT_FILE or SSL_CERT_DIR is left to OpenSSL."""
    operator_file = certifi.where()
    stand_in = tmp_path / "certifi.pem"
    stand_in.write_text(upstream_ca.pem)
    monkeypatch.setattr(certifi, "where", lambda: str(stand_in))
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    if trust_env == "SSL_CERT_FILE":
        monkeypatch.setenv("SSL_CERT_FILE", operator_file)
    elif trust_env == "SSL_CERT_DIR":
        (tmp_path / "certs").mkdir()
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "certs"))

    loaded = _verifying_context().get_ca_certs(binary_form=True)

    assert (upstream_ca.cert.public_bytes(serialization.Encoding.DER) in loaded) is (trust_env is None)


@pytest.mark.asyncio
async def test_non_websocket_upgrade_is_not_spliced(broker: BrokerFactory, tls_upstream: Upstream) -> None:
    """Only websocket upgrades are spliced; any other Upgrade is dropped and served as a normal request."""
    await broker(_config(), secrets={"svc": SECRET})
    reader, writer = await _open_tunnel(broker, f"localhost:{tls_upstream.port}")
    try:
        writer.write(
            (
                f"GET /echo HTTP/1.1\r\nHost: localhost:{tls_upstream.port}\r\n"
                "Connection: Upgrade, HTTP2-Settings\r\nUpgrade: h2c\r\nHTTP2-Settings: AAMAAABkAAQAAP__\r\n\r\n"
            ).encode(),
        )
        upgraded = await read_raw_response(reader)
        writer.write(f"GET /echo HTTP/1.1\r\nHost: localhost:{tls_upstream.port}\r\n\r\n".encode())
        following = await read_raw_response(reader)
    finally:
        writer.transport.abort()

    assert (upgraded.status, following.status) == (200, 200)
    seen = upgraded.json()["headers"]
    assert not {"upgrade", "connection", "http2-settings"} & set(seen)
    assert seen["authorization"] == [f"Bearer {SECRET}"]


@pytest.mark.asyncio
async def test_websocket_handshake_forwards_only_connection_upgrade(
    broker: BrokerFactory,
    tls_upstream: Upstream,
) -> None:
    """A websocket handshake reaches the upstream with ``Connection: Upgrade`` instead of the client's value."""
    await broker(_config(), secrets={"svc": SECRET})
    reader, writer = await _open_tunnel(broker, f"localhost:{tls_upstream.port}")
    try:
        writer.write(_websocket_handshake(tls_upstream.port, connection="keep-alive, Upgrade", upgrade="WebSocket"))
        switched = await read_raw_response(reader)
    finally:
        writer.transport.abort()

    assert switched.status == 101
    [seen] = tls_upstream.requests
    assert seen.headers["connection"] == ["Upgrade"]
    assert seen.headers["upgrade"] == ["websocket"]


@pytest.mark.asyncio
async def test_expired_token_stops_open_tunnel(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """The token is checked on every intercepted request, so a busy tunnel stops working when the token expires."""
    clock = [time.time()]
    await broker(_config(), secrets={"svc": SECRET})

    with patch("mindroom.egress_broker.tokens.time", SimpleNamespace(time=lambda: clock[0])):
        client = proxy_client(broker.token())
        first = await client.get(tls_upstream.url("/echo"))
        clock[0] += 604800 + 1
        second = await client.get(tls_upstream.url("/echo"))

    assert first.status_code == 200
    assert second.status_code == 407
    assert second.headers["connection"] == "close"
    assert second.headers["proxy-authenticate"] == 'Basic realm="mindroom-egress-broker"'
    assert tls_upstream.hits == ["/echo"]
    assert broker.resolved == ["svc"]


@pytest.mark.asyncio
async def test_chunked_body_over_limit_in_tunnel_gets_413_and_closes_upstream(
    broker: BrokerFactory,
    upstream_ca: UpstreamCA,
    audit: AuditLog,
    tmp_path: Path,
) -> None:
    """A streamed body crossing the limit gets 413, and the half-sent upstream request's connection is closed."""
    upstream_closed = asyncio.Event()

    async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while await reader.read(65536):
                pass
        except OSError:
            pass
        finally:
            upstream_closed.set()
            writer.transport.abort()

    upstream = await asyncio.start_server(receive, "127.0.0.1", 0, ssl=upstream_ca.server_context(tmp_path))
    port = upstream.sockets[0].getsockname()[1]
    await broker(_config(), secrets={"svc": SECRET}, max_body_bytes=1024)
    reader, writer = await _open_tunnel(broker, f"localhost:{port}")
    try:
        writer.write(
            f"POST /upload HTTP/1.1\r\nHost: localhost:{port}\r\nTransfer-Encoding: chunked\r\n\r\n".encode()
            + b"".join(b"200\r\n" + b"x" * 512 + b"\r\n" for _ in range(4))
            + b"0\r\n\r\n",
        )
        response = await read_raw_response(reader)
        async with asyncio.timeout(5):
            await upstream_closed.wait()
    finally:
        writer.transport.abort()
        upstream.close()
        await upstream.wait_closed()

    assert response.status == 413
    assert response.json() == {"error": "request_body_too_large"}
    [record] = await audit_records(audit, 1)
    assert (record.kind, record.status, record.service) == ("request", 413, "svc")


@pytest.mark.asyncio
async def test_http10_request_without_host_gets_tunnel_authority(broker: BrokerFactory, tls_upstream: Upstream) -> None:
    """An HTTP/1.0 request without Host is forwarded with the CONNECT authority as its Host."""
    await broker(_config(), secrets={"svc": SECRET})
    reader, writer = await _open_tunnel(broker, f"localhost:{tls_upstream.port}")
    try:
        writer.write(b"GET /echo HTTP/1.0\r\n\r\n")
        response = await read_raw_response(reader)
    finally:
        writer.transport.abort()

    assert response.status == 200
    seen = response.json()["headers"]
    assert seen["host"] == [f"localhost:{tls_upstream.port}"]
    assert seen["authorization"] == [f"Bearer {SECRET}"]
