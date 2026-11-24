"""Raw-socket request corpus: smuggling, framing, pipelining, and Expect cases on both listener paths.

Each case is written as raw bytes, as a plain proxy request and inside an intercepted tunnel; see `raw_http` for the
strict upstream parser and the invariants every case checks.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import h11
import pytest

from tests.egress_broker.conftest import MALFORMED, connect_request, proxy_authorization, single_rule_config
from tests.egress_broker.raw_http import (
    MAX_BODY,
    SECOND,
    SECRET,
    Forwarded,
    assert_contained,
    assert_listener_serves,
    client_for,
    open_client,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.egress_broker.conftest import BrokerFactory
    from tests.egress_broker.raw_http import UpstreamFactory


# The request corpus: each case runs as plain HTTP and inside an intercepted tunnel.


def _body(expected: bytes, *, chunked: bool) -> Callable[[Forwarded], None]:
    def check(request: Forwarded) -> None:
        assert request.body == expected
        assert request.header(b"transfer-encoding") == ([b"chunked"] if chunked else [])
        assert request.header(b"content-length") == ([] if chunked else [str(len(expected)).encode()])

    return check


def _single_line(name: bytes, value: bytes) -> Callable[[Forwarded], None]:
    def check(request: Forwarded) -> None:
        assert request.header(name) == [value]

    return check


def _no_upgrade(request: Forwarded) -> None:
    assert not {b"upgrade", b"connection", b"http2-settings"} & {name for name, _ in request.headers}


def _without_expect(check: Callable[[Forwarded], None]) -> Callable[[Forwarded], None]:
    def checked(request: Forwarded) -> None:
        check(request)
        assert request.header(b"expect") == []

    return checked


@dataclass(frozen=True)
class Outcome:
    """The statuses the client reads, in order, and what the upstream receives.

    `forwarded` counts whole requests; `partial` says whether one more head arrived whose body never did.
    `closes` defaults to closing after an error status.
    """

    statuses: tuple[int, ...]
    forwarded: int
    partial: bool = False
    closes: bool | None = None

    @property
    def expect_close(self) -> bool:
        """Return whether the broker must close the connection after the last reply."""
        return self.closes if self.closes is not None else self.statuses[-1] >= 400


@dataclass(frozen=True)
class Case:
    """One raw request (or pipelined requests) and the outcome on each path."""

    id: str
    template: str
    plain: Outcome
    tunnel: Outcome
    check: Callable[[Forwarded], None] | None = None
    rewrite: Callable[[bytes], bytes] | None = None


def _same(statuses: tuple[int, ...], forwarded: int, **kwargs: bool) -> dict[str, Outcome]:
    outcome = Outcome(statuses, forwarded, **kwargs)
    return {"plain": outcome, "tunnel": outcome}


_HEAD_LIMIT = 64 * 1024


def _padded_to(size: int) -> Callable[[bytes], bytes]:
    """Pad the empty `X-Pad` header ending the head so that the whole head is exactly `size` bytes."""

    def pad(raw: bytes) -> bytes:
        assert raw.endswith(b"X-Pad: \r\n\r\n")
        return raw[:-4] + b"a" * (size - len(raw)) + raw[-4:]

    return pad


_POST = "POST {t}/a HTTP/1.1\r\nHost: {h}\r\n{a}"
_GET = "GET {t}/a HTTP/1.1\r\nHost: {h}\r\n{a}"
# A request with no credentials and another Host: as plain HTTP it is unauthenticated, in a tunnel it is fronting.
_SMUGGLED = "GET /smuggled HTTP/1.1\r\nHost: evil.example\r\n\r\n"
# RFC 9112 section 6.1: after a request with both length headers the connection closes, so the rest is never read.
_CLOSES_AFTER_BOTH_LENGTHS = {"plain": Outcome((200,), 1, closes=True), "tunnel": Outcome((200,), 1, closes=True)}
_REJECTED_TE = "Content-Length: 5\r\nTransfer-Encoding: {value}\r\n\r\n0\r\n\r\n"
_ACCEPTED_TE = "Content-Length: 5\r\n{line}\r\n\r\n0\r\n\r\n"

REQUEST_CASES = [
    # Conflicting length headers: the broker frames by Transfer-Encoding, forwards chunked framing only, and closes.
    Case(
        "cl-te",
        _POST + "Content-Length: 4\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n" + _SMUGGLED,
        **_CLOSES_AFTER_BOTH_LENGTHS,
        check=_body(b"", chunked=True),
    ),
    Case(
        "te-cl",
        _POST + "Transfer-Encoding: chunked\r\nContent-Length: 3\r\n\r\n8\r\nSMUGGLED\r\n0\r\n\r\n" + _SMUGGLED,
        **_CLOSES_AFTER_BOTH_LENGTHS,
        check=_body(b"SMUGGLED", chunked=True),
    ),
    Case(
        "cl-te-with-body",
        _POST + "Content-Length: 3\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n",
        **_CLOSES_AFTER_BOTH_LENGTHS,
        check=_body(b"hello", chunked=True),
    ),
    *(
        Case(f"te-te-{name}", _POST + _REJECTED_TE.format(value=value), **_same((501,), 0))
        for name, value in [
            ("xchunked", "xchunked"),
            ("chunked-identity", "chunked, identity"),
            ("identity-chunked", "identity, chunked"),
            ("repeated", "chunked\r\nTransfer-Encoding: chunked"),
        ]
    ),
    Case(
        "te-te-space-before-colon",
        _POST + "Content-Length: 5\r\nTransfer-Encoding : chunked\r\n\r\n0\r\n\r\n",
        **_same((400,), 0),
    ),
    Case("te-te-vertical-tab", _POST + _REJECTED_TE.format(value="\x0bchunked"), **_same((400,), 0)),
    *(
        Case(
            f"te-te-{name}",
            _POST + _ACCEPTED_TE.format(line=line),
            **_same((200,), 1),
            check=_body(b"", chunked=True),
        )
        for name, line in [
            ("uppercase", "Transfer-Encoding: CHUNKED"),
            ("tab", "Transfer-Encoding:\tchunked"),
            ("obs-fold", "Transfer-Encoding:\r\n chunked"),
        ]
    ),
    Case(
        "cl-repeated-equal",
        _POST + "Content-Length: 3\r\nContent-Length: 3\r\n\r\nabc",
        **_same((200,), 1),
        check=_body(b"abc", chunked=False),
    ),
    Case(
        "cl-list-equal",
        _POST + "Content-Length: 3, 3\r\n\r\nabc",
        **_same((200,), 1),
        check=_body(b"abc", chunked=False),
    ),
    *(
        Case(f"cl-{name}", _POST + f"{lines}\r\n\r\nabc", **_same((400,), 0))
        for name, lines in [
            ("conflicting", "Content-Length: 3\r\nContent-Length: 4"),
            ("list-conflicting", "Content-Length: 3, 4"),
            ("plus-sign", "Content-Length: +3"),
            ("minus-sign", "Content-Length: -3"),
            ("hex", "Content-Length: 0x3"),
            # h11 refuses more than 20 digits.
            ("too-many-digits", "Content-Length: 100000000000000000003"),
        ]
    ),
    # 2**64 + 3 wraps to 3 in a 64-bit parser; the broker reads it whole and refuses it as too large.
    Case("cl-wraps-64-bit", _POST + "Content-Length: 18446744073709551619\r\n\r\nabc", **_same((413,), 0)),
    # Chunk sizes: the head is already upstream when a bad chunk arrives, so the upstream sees one partial request.
    Case(
        "chunk-size-too-long",
        _POST + "Transfer-Encoding: chunked\r\n\r\n" + "1" * 21 + "\r\nabc\r\n0\r\n\r\n",
        **_same((400,), 0, partial=True),
    ),
    Case(
        # 2**64 + 3 wraps to 3 in a 64-bit parser, which would read the rest as a second request.
        "chunk-size-wraps-64-bit",
        _POST + "Transfer-Encoding: chunked\r\n\r\n10000000000000003\r\nabc\r\n0\r\n\r\n" + _SMUGGLED + "x" * MAX_BODY,
        **_same((413,), 0, partial=True),
    ),
    Case(
        "chunk-size-negative",
        _POST + "Transfer-Encoding: chunked\r\n\r\n-3\r\nabc\r\n0\r\n\r\n",
        **_same((400,), 0, partial=True),
    ),
    Case(
        "chunk-bare-lf",
        _POST + "Transfer-Encoding: chunked\r\n\r\n3\nabc\n0\n\n\r\n",
        **_same((400,), 0, partial=True),
    ),
    Case(
        "chunk-bad-terminator",
        _POST + "Transfer-Encoding: chunked\r\n\r\n3\r\nabcXX0\r\n\r\n",
        **_same((400,), 0, partial=True),
    ),
    Case(
        "chunk-extension",
        _POST + "Transfer-Encoding: chunked\r\n\r\n3;ext=1\r\nabc\r\n0\r\n\r\n",
        **_same((200,), 1),
        check=_body(b"abc", chunked=True),
    ),
    Case(
        "chunk-trailer-dropped",
        _POST + "Transfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\nAuthorization: Bearer forged\r\n\r\n",
        **_same((200,), 1),
        check=_body(b"abc", chunked=True),
    ),
    # Line endings and folding: whatever h11 accepts is re-serialized with CRLF and one line per header.
    Case("bare-lf-head", _GET + "\r\n", **_same((200,), 1), rewrite=lambda raw: raw.replace(b"\r\n", b"\n")),
    Case("bare-cr-head", "GET {t}/a HTTP/1.1\rHost: {h}\r\n{a}\r\n", **_same((400,), 0)),
    Case("cr-in-header-value", _GET + "X-Note: a\rb\r\n\r\n", **_same((400,), 0)),
    Case("nul-in-header-value", _GET + "X-Note: a\x00b\r\n\r\n", **_same((400,), 0)),
    Case(
        "obs-fold-value",
        _GET + "X-Folded: one\r\n two\r\n\r\n",
        **_same((200,), 1),
        check=_single_line(b"x-folded", b"one two"),
    ),
    Case("obs-fold-first-line", "GET {t}/a HTTP/1.1\r\n X-Folded: one\r\nHost: {h}\r\n{a}\r\n", **_same((400,), 0)),
    # Request heads are limited to 64 KiB, request line through the blank line, however the reads split them.
    Case("oversized-head", _GET + "X-Big: " + "a" * 70_000, **_same((431,), 0)),
    Case("head-at-limit", _GET + "X-Pad: \r\n\r\n", **_same((200,), 1), rewrite=_padded_to(_HEAD_LIMIT)),
    Case("head-over-limit", _GET + "X-Pad: \r\n\r\n", **_same((431,), 0), rewrite=_padded_to(_HEAD_LIMIT + 1)),
    Case("repeated-host", _GET + "Host: evil.example\r\n\r\n", **_same((400,), 0)),
    # Pipelined requests that would reach another host.
    Case(
        "pipelined-other-host",
        _GET + "\r\n" + _SMUGGLED,
        plain=Outcome((200, 407), 1),
        tunnel=Outcome((200, 403), 1),
    ),
    Case(
        "pipelined-absolute-form",
        _GET + "\r\nGET http://localhost:1/smuggled HTTP/1.1\r\nHost: localhost:1\r\n{a}\r\n",
        # As plain HTTP it is a request to the rule host, which needs TLS; in a tunnel only origin-form routes.
        plain=Outcome((200, 403), 1),
        tunnel=Outcome((200, 400), 1),
    ),
    Case(
        "pipelined-after-connection-close",
        _GET + "Connection: close\r\n\r\n" + _SMUGGLED,
        **_same((200,), 1, closes=True),
    ),
    Case(
        "host-differs-from-target",
        "GET {t}/a HTTP/1.1\r\nHost: evil.example\r\n{a}\r\n",
        # Plain requests route by URL and get its authority as Host; in a tunnel another Host is fronting.
        plain=Outcome((200,), 1),
        tunnel=Outcome((403,), 0),
    ),
    # A Connection header listing framing headers must not change the framing that reaches the upstream.
    Case(
        "connection-lists-content-length",
        _POST + "Connection: Content-Length\r\nContent-Length: 3\r\n\r\nabc",
        **_same((200,), 1),
        check=_body(b"abc", chunked=False),
    ),
    Case(
        "connection-lists-content-length-over-limit",
        _POST + "Connection: Content-Length\r\nContent-Length: 5000\r\n\r\n",
        **_same((413,), 0),
    ),
    Case(
        "connection-lists-transfer-encoding",
        _POST + "Connection: Transfer-Encoding\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n",
        **_same((200,), 1),
        check=_body(b"abc", chunked=True),
    ),
    Case(
        "connection-lists-host",
        _GET + "Connection: Host\r\n\r\n",
        **_same((200,), 1),
    ),
    # Upgrades other than websocket are dropped and the request is served as a normal one.
    Case(
        "upgrade-h2c",
        _GET
        + "Connection: Upgrade, HTTP2-Settings\r\nUpgrade: h2c\r\nHTTP2-Settings: AAMAAABkAAQAAP__\r\n\r\n"
        + _GET
        + "\r\n",
        **_same((200, 200), 2),
        check=_no_upgrade,
    ),
    Case(
        # A websocket upgrade whose connection must close after the response (both lengths) is never spliced.
        "upgrade-websocket-with-both-lengths",
        _GET + "Connection: Upgrade\r\nUpgrade: websocket\r\nContent-Length: 4\r\n"
        "Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n" + _SMUGGLED,
        # Plain requests never upgrade, so it is served as a plain request that closes; in a tunnel it is refused.
        plain=Outcome((200,), 1, closes=True),
        tunnel=Outcome((400,), 0),
        check=_no_upgrade,
    ),
    Case(
        "upgrade-websocket-and-h2c",
        _GET + "Connection: Upgrade\r\nUpgrade: websocket, h2c\r\n\r\n",
        **_same((200,), 1),
        check=_no_upgrade,
    ),
    Case(
        "http10-chunked",
        "POST {t}/a HTTP/1.0\r\nHost: {h}\r\n{a}Transfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n",
        **_same((200,), 1, closes=True),
        check=_body(b"abc", chunked=True),
    ),
    # Expect: the broker answers 100 itself and never forwards the header; an oversized body is refused first.
    Case(
        "expect-continue-body-sent-early",
        _POST + "Content-Length: 5\r\nExpect: 100-continue\r\n\r\nhello",
        **_same((100, 200), 1),
        check=_without_expect(_body(b"hello", chunked=False)),
    ),
    Case(
        "expect-continue-over-limit",
        _POST + "Content-Length: 5000\r\nExpect: 100-continue\r\n\r\n",
        **_same((413,), 0),
    ),
    Case(
        # The HTTP/2 prior-knowledge preface: no credentials as plain HTTP, and not origin-form in a tunnel.
        "h2-preface",
        "PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n",
        plain=Outcome((407,), 0),
        tunnel=Outcome((400,), 0),
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("tunnel", [False, True], ids=["plain", "tunnel"])
@pytest.mark.parametrize("case", REQUEST_CASES, ids=[case.id for case in REQUEST_CASES])
async def test_request_corpus(broker: BrokerFactory, upstreams: UpstreamFactory, case: Case, tunnel: bool) -> None:
    """Each malformed or ambiguous request is refused or forwarded as exactly the requests the broker parsed."""
    await broker(single_rule_config(), secrets={"svc": SECRET}, max_body_bytes=MAX_BODY)
    upstream = await upstreams(tls=tunnel)
    probe = await upstreams()
    outcome = case.tunnel if tunnel else case.plain
    client, target = await client_for(broker, upstream.port, tunnel=tunnel)
    try:
        raw = target.render(case.template)
        client.send(case.rewrite(raw) if case.rewrite else raw)
        replies = [await client.reply() for _ in outcome.statuses]
        closed = await client.closed() if outcome.expect_close else None
    finally:
        client.close()

    assert [reply.status if reply else None for reply in replies] == list(outcome.statuses)
    assert all(reply.complete for reply in replies if reply)
    assert closed is (True if outcome.expect_close else None)
    assert len(upstream.complete) == outcome.forwarded
    assert len(upstream.requests) == outcome.forwarded + outcome.partial
    if case.check is not None:
        for request in upstream.complete:
            case.check(request)
    assert_contained(
        intercepted=upstream if tunnel else None,
        others=[] if tunnel else [upstream],
        client_bytes=bytes(client.received),
        host=target.host,
    )
    await assert_listener_serves(broker, probe)


# Expect: 100-continue and pipelining across hosts, which need more than one write or upstream.


@pytest.mark.asyncio
@pytest.mark.parametrize("tunnel", [False, True], ids=["plain", "tunnel"])
async def test_expect_continue_forwards_one_request_after_the_body(
    broker: BrokerFactory,
    upstreams: UpstreamFactory,
    tunnel: bool,
) -> None:
    """A client waiting for 100 Continue gets it, then its body travels in the same single forwarded request."""
    await broker(single_rule_config(), secrets={"svc": SECRET})
    upstream = await upstreams(tls=tunnel)
    probe = await upstreams()
    client, target = await client_for(broker, upstream.port, tunnel=tunnel)
    try:
        client.send(target.render(_POST + "Content-Length: 5\r\nExpect: 100-continue\r\n\r\n"))
        interim = await client.reply()
        client.send(b"hello")
        final = await client.reply()
    finally:
        client.close()

    assert interim is not None
    assert interim.status == 100
    assert final is not None
    assert (final.status, final.body) == (200, b"ok")
    [request] = upstream.complete
    assert request.body == b"hello"
    assert request.header(b"expect") == []
    assert_contained(
        intercepted=upstream if tunnel else None,
        others=[] if tunnel else [upstream],
        client_bytes=bytes(client.received),
        host=target.host,
    )
    await assert_listener_serves(broker, probe)


class _ServerReads:
    """Count the bytes each of the broker's client-side h11 connections receives, to wait for a part to be read.

    The broker feeds h11 and asks it for the next event in one step, so once the bytes are counted, h11 has already
    judged the incomplete head they leave.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._received: dict[int, int] = {}
        self._changed = asyncio.Event()
        receive_data = h11.Connection.receive_data

        def counting_receive_data(conn: h11.Connection, data: bytes) -> None:
            receive_data(conn, data)
            if conn.our_role is h11.SERVER:
                self._received[id(conn)] = self._received.get(id(conn), 0) + len(data)
                self._changed.set()

        monkeypatch.setattr(h11.Connection, "receive_data", counting_receive_data)

    async def wait_for(self, size: int) -> None:
        """Wait until one connection has received at least `size` bytes."""
        async with asyncio.timeout(5):
            while max(self._received.values(), default=0) < size:
                self._changed.clear()
                await self._changed.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("tunnel", [False, True], ids=["plain", "tunnel"])
@pytest.mark.parametrize(
    ("first_part", "total", "status"),
    [(20 * 1024, _HEAD_LIMIT, 200), (60 * 1024, 70 * 1024, 431)],
    ids=["split-at-limit", "split-over-limit"],
)
async def test_head_limit_holds_however_the_head_is_split(
    broker: BrokerFactory,
    upstreams: UpstreamFactory,
    monkeypatch: pytest.MonkeyPatch,
    tunnel: bool,
    first_part: int,
    total: int,
    status: int,
) -> None:
    """A head of 64 KiB is read even when its first part alone exceeds h11's default; one over it never is."""
    await broker(single_rule_config(), secrets={"svc": SECRET}, max_body_bytes=MAX_BODY)
    upstream = await upstreams(tls=tunnel)
    probe = await upstreams()
    client, target = await client_for(broker, upstream.port, tunnel=tunnel)
    # Counted only from here on: the CONNECT head is far shorter than either first part anyway.
    reads = _ServerReads(monkeypatch)
    try:
        raw = _padded_to(total)(target.render(_GET + "X-Pad: \r\n\r\n"))
        client.send(raw[:first_part])
        await client.writer.drain()
        # The broker has read the whole first part, and h11 has judged it, before the rest is sent.
        await reads.wait_for(first_part)
        client.send(raw[first_part:])
        reply = await client.reply()
    finally:
        client.close()

    assert reply is not None
    assert reply.status == status
    assert len(upstream.requests) == (status == 200)
    assert_contained(
        intercepted=upstream if tunnel else None,
        others=[] if tunnel else [upstream],
        client_bytes=bytes(client.received),
        host=target.host,
    )
    await assert_listener_serves(broker, probe)


@pytest.mark.asyncio
async def test_pipelined_plain_requests_reach_only_their_own_hosts(
    broker: BrokerFactory,
    upstreams: UpstreamFactory,
) -> None:
    """Pipelined plain requests to different upstreams each reach only the host their own URL names."""
    await broker(single_rule_config(), secrets={"svc": SECRET})
    first = await upstreams()
    second = await upstreams(lambda _index: [SECOND])
    probe = await upstreams()
    client, _target = await client_for(broker, first.port, tunnel=False)
    credential = proxy_authorization(broker.token())
    try:
        client.send(
            b"".join(
                (
                    f"GET http://127.0.0.1:{upstream.port}/{name} HTTP/1.1\r\nHost: 127.0.0.1:{upstream.port}\r\n"
                    f"Proxy-Authorization: {credential}\r\n\r\n"
                ).encode()
                for name, upstream in [("first", first), ("second", second)]
            ),
        )
        replies = [await client.reply(), await client.reply()]
    finally:
        client.close()

    assert [(reply.status, reply.body) for reply in replies if reply] == [(200, b"ok"), (200, b"second")]
    assert [(request.target, request.header(b"host")) for request in first.complete] == [
        (b"/first", [f"127.0.0.1:{first.port}".encode()]),
    ]
    assert [(request.target, request.header(b"host")) for request in second.complete] == [
        (b"/second", [f"127.0.0.1:{second.port}".encode()]),
    ]
    assert first.errors == second.errors == []
    assert SECRET.encode() not in first.raw + second.raw
    await assert_listener_serves(broker, probe)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "after_head",
    [
        "Content-Length: 5\r\n\r\nhello",
        "\r\nGET /a HTTP/1.1\r\nHost: localhost\r\n\r\n",
        # The start of a TLS record sent before the broker answered the CONNECT, which TLS can no longer read.
        "\r\n\x16\x03\x01\x00\x05early",
    ],
    ids=["connect-with-body", "request-pipelined-after-connect", "tls-record-before-connect-answer"],
)
async def test_connect_carrying_more_than_its_head_is_refused(
    broker: BrokerFactory,
    upstreams: UpstreamFactory,
    after_head: str,
) -> None:
    """A CONNECT to an intercepted host with a body, or bytes behind it, is refused as malformed before any TLS."""
    await broker(single_rule_config(), secrets={"svc": SECRET})
    upstream = await upstreams(tls=True)
    probe = await upstreams()
    client = await open_client(broker.latest.port)
    try:
        head = connect_request(f"localhost:{upstream.port}", authorization=proxy_authorization(broker.token()))
        client.send(head.removesuffix(b"\r\n") + after_head.encode())
        reply = await client.reply()
        closed = await client.closed()
    finally:
        client.close()

    assert reply is not None
    assert (reply.status, closed) == (400, True)
    assert json.loads(reply.body) == MALFORMED
    assert upstream.raw == b""
    assert broker.resolved == []
    await assert_listener_serves(broker, probe)
