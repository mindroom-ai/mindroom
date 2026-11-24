"""TLS edge cases inside an intercepted tunnel: SNI, ALPN, and upstream certificate failures."""

from __future__ import annotations

import json
import ssl
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from tests.egress_broker.conftest import (
    LOCALHOST_UNREACHABLE,
    connect_request,
    proxy_authorization,
    read_raw_response,
    single_rule_config,
)
from tests.egress_broker.raw_http import (
    SECRET,
    assert_contained,
    assert_listener_serves,
    open_client,
    open_tunnel,
)

if TYPE_CHECKING:
    import asyncio
    from pathlib import Path

    from tests.egress_broker.conftest import BrokerFactory, ServeFactory, UpstreamCA
    from tests.egress_broker.raw_http import UpstreamFactory


# TLS edge cases inside the tunnel.


def _client_context(broker: BrokerFactory) -> ssl.SSLContext:
    """Return a context trusting the broker CA that, like a hostile worker, skips hostname checks."""
    context = ssl.create_default_context(cadata=broker.ca.cert_pem)
    context.check_hostname = False
    return context


@pytest.mark.asyncio
async def test_sni_differing_from_connect_host_is_refused(broker: BrokerFactory, upstreams: UpstreamFactory) -> None:
    """A client naming another host in SNI than in CONNECT gets no tunnel to send requests through, so no secret."""
    await broker(single_rule_config(), secrets={"svc": SECRET})
    upstream = await upstreams(tls=True)
    probe = await upstreams()
    client = await open_client(broker.latest.port)
    try:
        client.send(connect_request(f"localhost:{upstream.port}", authorization=proxy_authorization(broker.token())))
        assert (await read_raw_response(client.reader)).status == 200
        # The broker aborts with an unrecognized_name alert; the client may see the alert or just the reset after it.
        with capture_logs() as logs, pytest.raises((ssl.SSLError, ConnectionResetError)):
            await client.writer.start_tls(_client_context(broker), server_hostname="evil.example")
    finally:
        client.close()

    assert {"event": "egress_broker_client_tls_failed", "error_type": "SSLError", "log_level": "debug"} in logs
    assert broker.resolved == []
    assert upstream.raw == b""
    await assert_listener_serves(broker, probe)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "server_hostname",
    ["localhost", "localhost.", "LOCALHOST", None],
    ids=["sni", "sni-trailing-dot", "sni-uppercase", "no-sni"],
)
async def test_sni_matching_or_absent_routes_by_connect_host(
    broker: BrokerFactory,
    upstreams: UpstreamFactory,
    server_hostname: str | None,
) -> None:
    """A matching SNI (any case, trailing dot or not) or none gets the CONNECT host's leaf; Host still has to match."""
    await broker(single_rule_config(), secrets={"svc": SECRET})
    upstream = await upstreams(tls=True)
    probe = await upstreams()
    client = await open_tunnel(broker, upstream.port, context=_client_context(broker), server_hostname=server_hostname)
    try:
        peer = client.writer.get_extra_info("ssl_object").getpeercert()
        client.send(f"GET /a HTTP/1.1\r\nHost: localhost:{upstream.port}\r\n\r\n".encode())
        served = await client.reply()
        client.send(f"GET /b HTTP/1.1\r\nHost: evil.example:{upstream.port}\r\n\r\n".encode())
        fronted = await client.reply()
    finally:
        client.close()

    assert ("DNS", "localhost") in peer["subjectAltName"]
    assert served is not None
    assert fronted is not None
    assert (served.status, fronted.status) == (200, 403)
    assert [request.target for request in upstream.complete] == [b"/a"]
    assert_contained(
        intercepted=upstream,
        others=[],
        client_bytes=bytes(client.received),
        host=f"localhost:{upstream.port}",
    )
    await assert_listener_serves(broker, probe)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("offered", "selected"),
    [(["h2"], None), (["h2", "http/1.1"], "http/1.1"), (["http/1.1"], "http/1.1")],
    ids=["h2-only", "h2-and-http11", "http11"],
)
async def test_alpn_selects_only_http11(
    broker: BrokerFactory,
    upstreams: UpstreamFactory,
    offered: list[str],
    selected: str | None,
) -> None:
    """The broker only speaks HTTP/1.1: it selects it when offered, and an HTTP/2 preface is refused cleanly."""
    await broker(single_rule_config(), secrets={"svc": SECRET})
    upstream = await upstreams(tls=True)
    probe = await upstreams()
    context = ssl.create_default_context(cadata=broker.ca.cert_pem)
    context.set_alpn_protocols(offered)
    client = await open_tunnel(broker, upstream.port, context=context)
    try:
        negotiated = client.writer.get_extra_info("ssl_object").selected_alpn_protocol()
        if selected is None:
            # An h2-only client that goes ahead anyway sends the HTTP/2 connection preface.
            client.send(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n")
        else:
            client.send(f"GET /a HTTP/1.1\r\nHost: localhost:{upstream.port}\r\n\r\n".encode())
        reply = await client.reply()
        closed = await client.closed() if selected is None else None
    finally:
        client.close()

    assert negotiated == selected
    assert reply is not None
    if selected is None:
        assert (reply.status, closed) == (400, True)
        assert upstream.raw == b""
    else:
        assert reply.status == 200
        assert len(upstream.complete) == 1
    assert_contained(
        intercepted=upstream,
        others=[],
        client_bytes=bytes(client.received),
        host=f"localhost:{upstream.port}",
    )
    await assert_listener_serves(broker, probe)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["untrusted-ca", "wrong-name", "plaintext"])
async def test_upstream_tls_failure_gets_502_without_fallback(
    broker: BrokerFactory,
    upstreams: UpstreamFactory,
    upstream_ca: UpstreamCA,
    serve: ServeFactory,
    tmp_path: Path,
    failure: str,
) -> None:
    """An upstream the broker cannot verify gets a 502, and the broker never retries without TLS."""
    await broker(single_rule_config(), secrets={"svc": SECRET})
    probe = await upstreams()
    received: list[bytes] = []
    if failure == "plaintext":

        async def answer_in_plaintext(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            received.append(await reader.read(65536))
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            writer.close()

        port = await serve(answer_in_plaintext)
        upstream = None
    else:
        context = (
            broker.ca.server_context("localhost")
            if failure == "untrusted-ca"
            else upstream_ca.server_context(tmp_path, name="other.example", with_loopback_ip=False)
        )
        upstream = await upstreams(ssl_context=context)
        port = upstream.port
    client = await open_tunnel(broker, port)
    try:
        client.send(f"GET /a HTTP/1.1\r\nHost: localhost:{port}\r\n\r\n".encode())
        reply = await client.reply()
        closed = await client.closed()
    finally:
        client.close()

    assert reply is not None
    assert reply.status == 502
    assert json.loads(reply.body) == LOCALHOST_UNREACHABLE
    assert closed is True
    if upstream is not None:
        # The handshake failed before the upstream read anything.
        assert upstream.raw == b""
    else:
        # Only TLS records reached the plaintext server: a ClientHello, never the request or the secret.
        assert received
        assert all(data.startswith(b"\x16\x03") for data in received)
        assert not [data for data in received if b"HTTP/1.1" in data or SECRET.encode() in data]
    assert SECRET.encode() not in client.received
    await assert_listener_serves(broker, probe)
