"""Tests for the egress broker listener: proxy auth, blind tunnels, and plain HTTP forwarding."""

from __future__ import annotations

import asyncio
import socket
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom.config.egress_broker import EgressAuth, EgressBrokerConfig, EgressRule, EgressService
from mindroom.egress_broker import TokenSigner
from tests.egress_broker.conftest import (
    DEFAULT_CLAIMS,
    audit_records,
    connect_request,
    proxy_authorization,
    read_raw_response,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    import httpx

    from mindroom.egress_broker import AuditLog
    from mindroom.egress_broker.proxy import EgressBroker
    from tests.egress_broker.conftest import BrokerFactory, RawResponse, Upstream, UpstreamCA

    RawProxy = Callable[[int, bytes], Awaitable[RawResponse]]
    ProxyClient = Callable[..., httpx.AsyncClient]


def _config(host: str, *, path_prefix: str = "/") -> EgressBrokerConfig:
    rule = EgressRule(host=host, path_prefix=path_prefix, auth=EgressAuth(type="bearer"))
    return EgressBrokerConfig(services={"svc": EgressService(rules=[rule])})


@pytest.mark.asyncio
async def test_missing_token_gets_407(broker: BrokerFactory, raw_proxy: RawProxy) -> None:
    """A CONNECT without Proxy-Authorization is challenged with Basic auth."""
    started = await broker()

    response = await raw_proxy(started.port, connect_request("localhost:443"))

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
        "garbage": proxy_authorization("mrb1.not.valid"),
        "foreign-key": proxy_authorization(TokenSigner(b"x" * 32).mint(DEFAULT_CLAIMS)),
        "expired": proxy_authorization(broker.signer.mint(DEFAULT_CLAIMS, now=time.time() - 10 * 604800)),
        "bearer-garbage": "Bearer mrb1.not.valid",
        "not-base64": "Basic !!!",
    }[credential]

    response = await raw_proxy(started.port, connect_request("localhost:443", authorization=authorization))

    assert response.status == 407
    assert "proxy-authenticate" in response.headers


@pytest.mark.asyncio
async def test_bearer_token_opens_tunnel(broker: BrokerFactory, tls_upstream: Upstream, raw_proxy: RawProxy) -> None:
    """``Proxy-Authorization: Bearer <token>`` is accepted as well as Basic."""
    started = await broker()

    response = await raw_proxy(
        started.port,
        connect_request(f"localhost:{tls_upstream.port}", authorization=f"Bearer {broker.token()}"),
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

    response = await raw_proxy(
        started.port,
        connect_request("localhost:443", authorization=proxy_authorization(broker.token())),
    )

    assert response.status == 403
    assert response.headers["content-type"] == "application/json"
    assert response.headers["connection"] == "close"
    assert response.json() == {"error": "host_not_allowed", "services": ["github", "openai"]}
    [record] = await audit_records(audit, 1)
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
    response = await raw_proxy(
        broker_default_policy.port,
        connect_request(target, authorization=proxy_authorization(broker.token())),
    )

    assert response.status == 403
    assert response.json() == {"error": "destination_blocked"}


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["localhost", "localhost:", "localhost:0", "localhost:https", "::1:443"])
async def test_connect_without_valid_port_gets_400(broker: BrokerFactory, raw_proxy: RawProxy, target: str) -> None:
    """A CONNECT target must name an explicit numeric port, and IPv6 literals need brackets."""
    started = await broker()

    response = await raw_proxy(started.port, connect_request(target, authorization=proxy_authorization(broker.token())))

    assert response.status == 400


@pytest.mark.asyncio
async def test_unresolvable_connect_target_gets_502(broker: BrokerFactory, raw_proxy: RawProxy) -> None:
    """A name that does not resolve is an upstream failure, not a policy block."""
    started = await broker()

    def unresolvable(*_args: object, **_kwargs: object) -> list[object]:
        exc = ValueError("URL is not allowed for server-side fetching")
        exc.reason = "dns_resolution_failed"  # type: ignore[attr-defined]
        raise exc

    with patch("mindroom.egress_broker.dial.validated_connect_addresses", side_effect=unresolvable):
        response = await raw_proxy(
            started.port,
            connect_request("nonexistent.invalid:443", authorization=proxy_authorization(broker.token())),
        )

    assert response.status == 502
    assert response.json() == {"error": "upstream_unreachable"}


@pytest.mark.asyncio
async def test_slow_request_head_is_dropped(broker: BrokerFactory) -> None:
    """A client that never finishes its request head is disconnected after the head deadline."""
    started = await broker(head_timeout=0.2)
    reader, writer = await asyncio.open_connection("127.0.0.1", started.port)
    writer.write(b"CONNECT localhost:443 HTTP/1.1\r\nHost: localhost:443\r\n")
    await writer.drain()

    async with asyncio.timeout(5):
        remaining = await reader.read()

    writer.transport.abort()
    assert remaining == b""


@pytest.mark.asyncio
async def test_plain_http_matched_rule_requires_tls(
    broker: BrokerFactory,
    http_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A plain request a rule matches is refused, so a secret never crosses an unencrypted connection."""
    await broker(_config("localhost"), secrets={"svc": "s3cret"})
    client = proxy_client(broker.token())

    response = await client.get(http_upstream.url("/echo?page=2"))

    assert response.status_code == 403
    assert response.headers["content-type"] == "application/json"
    assert response.headers["connection"] == "close"
    assert response.json() == {"error": "tls_required", "service": "svc"}
    assert http_upstream.hits == []
    [record] = await audit_records(audit, 1)
    assert (record.kind, record.service, record.status, record.path) == ("denied", "svc", 403, "/echo")


@pytest.mark.asyncio
async def test_plain_http_unmatched_path_on_rule_host_forwards_unmodified(
    broker: BrokerFactory,
    http_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A path no rule matches is forwarded by URL without credentials, and the Host header cannot reroute it."""
    await broker(_config("localhost", path_prefix="/private"), secrets={"svc": "s3cret"})
    client = proxy_client(broker.token())

    response = await client.get(http_upstream.url("/echo?page=2"), headers={"Host": "evil.example"})

    assert response.status_code == 200
    echoed = response.json()
    assert "authorization" not in echoed["headers"]
    assert "proxy-authorization" not in echoed["headers"]
    assert echoed["headers"]["host"] == [f"localhost:{http_upstream.port}"]
    assert echoed["query"] == {"page": "2"}
    [record] = await audit_records(audit, 1)
    assert (record.kind, record.service, record.status, record.path) == ("request", None, 200, "/echo")
    assert (record.scope, record.agent_name, record.requester_id) == ("user_agent", "code", "@alice:example.org")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "cookie_kept"),
    [(None, True), (_config("localhost", path_prefix="/private"), False)],
    ids=["unmatched_host", "rule_host"],
)
async def test_plain_http_set_cookie_stripped_only_for_rule_hosts(
    broker: BrokerFactory,
    http_upstream: Upstream,
    proxy_client: ProxyClient,
    config: EgressBrokerConfig | None,
    cookie_kept: bool,
) -> None:
    """Hosts with rules lose Set-Cookie so sessions stay brokered; other hosts keep their cookies."""
    await broker(config)
    client = proxy_client(broker.token())

    response = await client.get(http_upstream.url("/cookie"))

    assert response.status_code == 200
    assert ("set-cookie" in response.headers) is cookie_kept


@pytest.mark.asyncio
async def test_plain_http_denied_under_deny_policy(
    broker: BrokerFactory,
    http_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """``unmatched_hosts: deny`` applies to plain requests too."""
    await broker(EgressBrokerConfig(unmatched_hosts="deny", services=_config("api.github.com").services))
    client = proxy_client(broker.token())

    response = await client.get(http_upstream.url("/echo"))

    assert response.status_code == 403
    assert response.json() == {"error": "host_not_allowed", "services": ["svc"]}
    assert http_upstream.hits == []


@pytest.mark.asyncio
async def test_plain_http_blocked_destination(
    broker_default_policy: EgressBroker,
    broker: BrokerFactory,
    http_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """The dial guard covers plain requests: the default policy refuses loopback."""
    assert broker.latest is broker_default_policy
    client = proxy_client(broker.token())

    response = await client.get(http_upstream.url("/echo"))

    assert response.status_code == 403
    assert response.json() == {"error": "destination_blocked"}
    assert http_upstream.hits == []


@pytest.mark.asyncio
async def test_plain_http_drops_request_content_length_beside_chunked(
    broker: BrokerFactory,
    http_upstream: Upstream,
    raw_proxy: RawProxy,
) -> None:
    """A client sending both Content-Length and chunked framing never gets both forwarded upstream."""
    started = await broker()
    request = (
        f"POST {http_upstream.url('/echo')} HTTP/1.1\r\n"
        f"Host: localhost:{http_upstream.port}\r\n"
        f"Proxy-Authorization: {proxy_authorization(broker.token())}\r\n"
        "Content-Length: 3\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Connection: close\r\n\r\n"
        "5\r\nhello\r\n0\r\n\r\n"
    ).encode()

    response = await raw_proxy(started.port, request)

    # The upstream refuses a request carrying both headers, so a 200 alone proves they were not both sent.
    assert response.status == 200
    headers = response.json()["headers"]
    assert "content-length" not in headers
    assert headers["transfer-encoding"] == ["chunked"]


@pytest.mark.asyncio
async def test_plain_http_response_content_length_beside_chunked_is_reframed(
    broker: BrokerFactory,
    proxy_client: ProxyClient,
) -> None:
    """An upstream response with both framings reaches the client whole, not cut at the Content-Length."""

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n",
        )
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(respond, "127.0.0.1", 0)
    try:
        await broker()
        client = proxy_client(broker.token())

        response = await client.get(f"http://localhost:{upstream.sockets[0].getsockname()[1]}/")
    finally:
        upstream.close()
        await upstream.wait_closed()

    assert response.status_code == 200
    assert response.text == "hello"
    assert "content-length" not in response.headers


@pytest.mark.asyncio
async def test_plain_http_keepalive_reauthenticates_each_request(
    broker: BrokerFactory,
    http_upstream: Upstream,
) -> None:
    """Every request on a kept-alive connection must carry a valid token, not just the first."""
    started = await broker()
    reader, writer = await asyncio.open_connection("127.0.0.1", started.port)
    try:

        def request(authorization: str) -> bytes:
            return (
                f"GET {http_upstream.url('/echo')} HTTP/1.1\r\n"
                f"Host: localhost:{http_upstream.port}\r\n"
                f"Proxy-Authorization: {authorization}\r\n\r\n"
            ).encode()

        writer.write(request(proxy_authorization(broker.token())))
        first = await read_raw_response(reader)
        writer.write(request(proxy_authorization("mrb1.not.valid")))
        second = await read_raw_response(reader)
    finally:
        writer.transport.abort()

    assert first.status == 200
    assert "connection" not in first.headers
    assert second.status == 407
    assert http_upstream.hits == ["/echo"]


@pytest.mark.asyncio
async def test_plain_http_streamed_body_over_limit_gets_413(
    broker: BrokerFactory,
    http_upstream: Upstream,
    raw_proxy: RawProxy,
    audit: AuditLog,
) -> None:
    """A chunked body that crosses the limit mid-stream is cut off with 413."""
    started = await broker(max_body_bytes=1024)
    chunk = b"x" * 512
    request = (
        (
            f"POST {http_upstream.url('/upload')} HTTP/1.1\r\n"
            f"Host: localhost:{http_upstream.port}\r\n"
            f"Proxy-Authorization: {proxy_authorization(broker.token())}\r\n"
            "Transfer-Encoding: chunked\r\n\r\n"
        ).encode()
        + b"".join(b"200\r\n" + chunk + b"\r\n" for _ in range(4))
        + b"0\r\n\r\n"
    )

    response = await raw_proxy(started.port, request)

    assert response.status == 413
    assert response.json() == {"error": "request_body_too_large"}
    [record] = await audit_records(audit, 1)
    assert (record.kind, record.status) == ("request", 413)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport",
    ["connect", "plain"],
)
async def test_config_provider_failure_gets_502_and_listener_survives(
    broker: BrokerFactory,
    raw_proxy: RawProxy,
    audit: AuditLog,
    transport: str,
) -> None:
    """A failing rule provider fails the request with 502 broker_error and an audit row, not the listener."""

    def failing_provider() -> EgressBrokerConfig:
        msg = "config unavailable"
        raise RuntimeError(msg)

    started = await broker(config_provider=failing_provider)
    authorization = proxy_authorization(broker.token())
    request = (
        connect_request("localhost:443", authorization=authorization)
        if transport == "connect"
        else f"GET http://localhost:1/echo HTTP/1.1\r\nHost: localhost:1\r\nProxy-Authorization: {authorization}\r\n\r\n".encode()
    )

    first = await raw_proxy(started.port, request)
    second = await raw_proxy(started.port, request)

    assert (first.status, first.json()) == (502, {"error": "broker_error"})
    assert (second.status, second.json()) == (502, {"error": "broker_error"})
    records = await audit_records(audit, 2)
    assert {(record.kind, record.status) for record in records} == {
        ("tunnel" if transport == "connect" else "request", 502),
    }


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
    [record] = await audit_records(audit, 1)
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

    [record] = await audit_records(audit, 1)

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
    following = await raw_proxy(started.port, connect_request("localhost:443"))

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


@pytest.mark.asyncio
async def test_close_drops_established_tunnel(broker: BrokerFactory, tls_upstream: Upstream) -> None:
    """Close also ends tunnels that are already relaying."""
    started = await broker()
    reader, writer = await asyncio.open_connection("127.0.0.1", started.port)
    writer.write(connect_request(f"localhost:{tls_upstream.port}", authorization=proxy_authorization(broker.token())))
    assert (await read_raw_response(reader)).status == 200

    await started.close()

    async with asyncio.timeout(5):
        try:
            remaining = await reader.read()
        except ConnectionResetError:
            remaining = b""
    writer.transport.abort()
    assert remaining == b""
