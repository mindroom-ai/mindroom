"""Tests for the egress broker listener: proxy auth, blind tunnels, and plain HTTP forwarding."""

from __future__ import annotations

import asyncio
import base64
import socket
import time
from typing import TYPE_CHECKING

import pytest

from mindroom.config.egress_broker import EgressAuth, EgressBrokerConfig, EgressRule, EgressService
from mindroom.egress_broker import TokenSigner
from tests.egress_broker.conftest import DEFAULT_CLAIMS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    import httpx

    from mindroom.egress_broker import AuditLog, AuditRecord
    from mindroom.egress_broker.proxy import EgressBroker
    from tests.egress_broker.conftest import BrokerFactory, RawResponse, Upstream, UpstreamCA

    RawProxy = Callable[[int, bytes], Awaitable[RawResponse]]
    ProxyClient = Callable[..., httpx.AsyncClient]


def _config(host: str) -> EgressBrokerConfig:
    return EgressBrokerConfig(
        services={"svc": EgressService(rules=[EgressRule(host=host, auth=EgressAuth(type="bearer"))])},
    )


def _connect(target: str, *, authorization: str | None = None) -> bytes:
    lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
    if authorization is not None:
        lines.append(f"Proxy-Authorization: {authorization}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def _basic(token: str) -> str:
    return "Basic " + base64.b64encode(f"{token}:".encode()).decode()


async def _audit_records(audit: AuditLog, count: int) -> list[AuditRecord]:
    """Wait for the broker to write `count` records; tunnels are audited only after they close."""
    async with asyncio.timeout(5):
        # The broker writes from its own thread, so the database is the only thing to watch.
        while len(records := audit.query()) < count:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    return records


@pytest.mark.asyncio
async def test_missing_token_gets_407(broker: BrokerFactory, raw_proxy: RawProxy) -> None:
    """A CONNECT without Proxy-Authorization is challenged with Basic auth."""
    started = await broker()

    response = await raw_proxy(started.port, _connect("localhost:443"))

    assert response.status == 407
    assert response.headers["proxy-authenticate"] == 'Basic realm="mindroom-egress-broker"'
    assert response.headers["connection"] == "close"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential",
    ["garbage", "foreign-key", "expired", "bearer-garbage", "not-base64"],
)
async def test_bad_token_gets_407(broker: BrokerFactory, raw_proxy: RawProxy, credential: str) -> None:
    """Unparseable, foreign-signed, and expired tokens are all challenged like a missing one."""
    started = await broker()
    authorization = {
        "garbage": _basic("mrb1.not.valid"),
        "foreign-key": _basic(TokenSigner(b"x" * 32).mint(DEFAULT_CLAIMS)),
        "expired": _basic(broker.signer.mint(DEFAULT_CLAIMS, now=time.time() - 10 * 604800)),
        "bearer-garbage": "Bearer mrb1.not.valid",
        "not-base64": "Basic !!!",
    }[credential]

    response = await raw_proxy(started.port, _connect("localhost:443", authorization=authorization))

    assert response.status == 407
    assert "proxy-authenticate" in response.headers


@pytest.mark.asyncio
async def test_bearer_token_opens_tunnel(broker: BrokerFactory, tls_upstream: Upstream, raw_proxy: RawProxy) -> None:
    """``Proxy-Authorization: Bearer <token>`` is accepted as well as Basic."""
    started = await broker()

    response = await raw_proxy(
        started.port,
        _connect(f"localhost:{tls_upstream.port}", authorization=f"Bearer {broker.token()}"),
    )

    assert response.status == 200


@pytest.mark.asyncio
async def test_unmatched_host_is_blind_tunnel(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    upstream_ca: UpstreamCA,
    proxy_client: ProxyClient,
) -> None:
    """A host without rules is tunnelled untouched: the client verifies the upstream's real certificate."""
    await broker()
    client = proxy_client(broker.token(), verify=upstream_ca.client_context())

    response = await client.get(tls_upstream.url("/echo"))

    assert response.status_code == 200
    echoed = response.json()
    assert echoed["path"] == "/echo"
    assert "proxy-authorization" not in echoed["headers"]
    assert "authorization" not in echoed["headers"]


@pytest.mark.asyncio
async def test_unmatched_host_denied_under_deny_policy(
    broker: BrokerFactory,
    raw_proxy: RawProxy,
    audit: AuditLog,
) -> None:
    """Under ``unmatched_hosts: deny`` a host without rules gets 403 with the configured service names."""
    config = EgressBrokerConfig(
        unmatched_hosts="deny",
        services={
            "github": EgressService(rules=[EgressRule(host="api.github.com", auth=EgressAuth(type="bearer"))]),
            "openai": EgressService(rules=[EgressRule(host="api.openai.com", auth=EgressAuth(type="bearer"))]),
        },
    )
    started = await broker(config)

    response = await raw_proxy(started.port, _connect("localhost:443", authorization=_basic(broker.token())))

    assert response.status == 403
    assert response.headers["content-type"] == "application/json"
    assert response.headers["connection"] == "close"
    assert response.json() == {"error": "host_not_allowed", "services": ["github", "openai"]}
    [record] = await _audit_records(audit, 1)
    assert (record.kind, record.status, record.host, record.method) == ("denied", 403, "localhost", "CONNECT")


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["127.0.0.1:8766", "localhost:8766", "[::1]:8766"])
async def test_default_policy_blocks_loopback(
    broker_default_policy: EgressBroker,
    broker: BrokerFactory,
    raw_proxy: RawProxy,
    target: str,
) -> None:
    """The dial guard refuses loopback tunnels, including bracketed IPv6 CONNECT targets."""
    response = await raw_proxy(broker_default_policy.port, _connect(target, authorization=_basic(broker.token())))

    assert response.status == 403
    assert response.json() == {"error": "destination_blocked"}


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["localhost", "localhost:", "localhost:0", "localhost:https", "::1:443"])
async def test_connect_without_valid_port_gets_400(broker: BrokerFactory, raw_proxy: RawProxy, target: str) -> None:
    """A CONNECT target must name an explicit numeric port, and IPv6 literals need brackets."""
    started = await broker()

    response = await raw_proxy(started.port, _connect(target, authorization=_basic(broker.token())))

    assert response.status == 400


@pytest.mark.asyncio
async def test_plain_http_absolute_form_injects(
    broker: BrokerFactory,
    http_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A plain request is routed by its URL, gets the secret injected, and loses proxy credentials."""
    await broker(_config("localhost"), secrets={"svc": "s3cret"})
    client = proxy_client(broker.token())

    response = await client.get(http_upstream.url("/echo?page=2"), headers={"Host": "evil.example"})

    assert response.status_code == 200
    echoed = response.json()
    assert echoed["headers"]["authorization"] == ["Bearer s3cret"]
    assert echoed["headers"]["host"] == [f"localhost:{http_upstream.port}"]
    assert "proxy-authorization" not in echoed["headers"]
    assert echoed["query"] == {"page": "2"}
    [record] = await _audit_records(audit, 1)
    assert (record.kind, record.service, record.status, record.path) == ("request", "svc", 200, "/echo")
    assert (record.scope, record.agent_name, record.requester_id) == ("user_agent", "code", "@alice:example.org")


@pytest.mark.asyncio
async def test_plain_http_missing_secret_gets_403_with_manage_url(
    broker: BrokerFactory,
    http_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """A matched service without a secret in scope is refused before anything reaches the upstream."""
    await broker(_config("localhost"), manage_url=lambda _claims: "https://m.test/connections/egress")
    client = proxy_client(broker.token())

    response = await client.get(http_upstream.url("/echo"))

    assert response.status_code == 403
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "error": "credential_not_configured",
        "service": "svc",
        "manage_url": "https://m.test/connections/egress",
    }
    assert http_upstream.hits == []


@pytest.mark.asyncio
async def test_plain_http_chunked_upload_is_streamed(
    broker: BrokerFactory,
    http_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """A chunked request body is re-framed for the upstream and arrives whole."""
    await broker()
    client = proxy_client(broker.token())

    async def body() -> AsyncIterator[bytes]:
        for _ in range(4):
            yield b"x" * 100_000

    response = await client.post(http_upstream.url("/upload"), content=body())

    assert response.status_code == 200
    assert response.json() == {"length": 400_000}


@pytest.mark.asyncio
async def test_plain_http_body_over_limit_gets_413(
    broker: BrokerFactory,
    http_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """A declared body over the broker limit is refused before the upstream is dialed."""
    await broker(max_body_bytes=1024)
    client = proxy_client(broker.token())

    response = await client.post(http_upstream.url("/upload"), content=b"x" * 2048)

    assert response.status_code == 413
    assert response.json() == {"error": "request_body_too_large"}
    assert http_upstream.hits == []


@pytest.mark.asyncio
async def test_plain_http_unreachable_upstream_gets_502(
    broker: BrokerFactory,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A refused upstream connection is reported as 502 and audited."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    await broker()
    client = proxy_client(broker.token())

    response = await client.get(f"http://localhost:{closed_port}/echo")

    assert response.status_code == 502
    assert response.json() == {"error": "upstream_unreachable"}
    [record] = await _audit_records(audit, 1)
    assert (record.kind, record.status, record.path) == ("request", 502, "/echo")


@pytest.mark.asyncio
async def test_tunnel_audited_without_query(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    upstream_ca: UpstreamCA,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A closed blind tunnel leaves one metadata record that never contains the request path or query."""
    await broker()
    client = proxy_client(broker.token(), verify=upstream_ca.client_context())
    assert (await client.get(tls_upstream.url("/echo?token=abc"))).status_code == 200
    await client.aclose()

    [record] = await _audit_records(audit, 1)

    assert (record.kind, record.host, record.path, record.method, record.status) == (
        "tunnel",
        "localhost",
        "",
        "CONNECT",
        200,
    )
    assert record.service is None
    assert record.bytes_up > 0
    assert record.bytes_down > 0
    assert record.scope == "user_agent"


@pytest.mark.asyncio
async def test_malformed_request_does_not_break_listener(broker: BrokerFactory, raw_proxy: RawProxy) -> None:
    """A client sending garbage gets 400 and the listener keeps serving others."""
    started = await broker()

    malformed = await raw_proxy(started.port, b"\x16\x03\x01 not http\r\n\r\n")
    following = await raw_proxy(started.port, _connect("localhost:443"))

    assert malformed.status == 400
    assert following.status == 407


@pytest.mark.asyncio
async def test_close_stops_listener(broker: BrokerFactory) -> None:
    """Close drops in-flight connections, stops accepting, and is idempotent."""
    started = await broker()
    port = started.port
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"CONNECT localhost:443 HTTP/1.1\r\n")
    await writer.drain()

    await started.close()

    async with asyncio.timeout(5):
        try:
            remaining = await reader.read()
        except ConnectionResetError:
            remaining = b""
    assert remaining == b""
    writer.transport.abort()
    with pytest.raises(ConnectionRefusedError):
        await asyncio.open_connection("127.0.0.1", port)
    await started.close()
