"""Raw-socket harness for the smuggling, framing, upstream-anomaly, and TLS corpora.

Requests are written as raw bytes, as plain proxy requests and inside an intercepted tunnel, because a client
library would normalize the edge cases away. The fake upstream (`RawUpstream`, served by the `upstreams` fixture in
`conftest.py`) reads what it receives with its own strict parser, which refuses anything another HTTP/1.1 parser
could frame differently (both framing headers, repeated framing headers, bare CR or LF, folded lines, trailers). So
every case can assert exactly what was forwarded: one well-framed request per request the broker parsed, or nothing.
`assert_contained` checks that the injected secret reached only the CONNECT host, once per forwarded request and
never the client, and `assert_listener_serves` that the listener still serves a fresh request afterwards.
"""

from __future__ import annotations

import asyncio
import re
import ssl
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from tests.egress_broker.conftest import connect_request, parse_head, proxy_authorization, read_raw_response

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from tests.egress_broker.conftest import BrokerFactory

SECRET = "smuggle-s3cret"  # noqa: S105 - test credential injected for the intercepted host
INJECTED = b"Bearer " + SECRET.encode()
# A complete response an upstream sends where nothing asked for it; relayed, it would answer the next request.
STRAY_RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\n\r\nSMUGGLED"
OK = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
SECOND = b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\nsecond"
MAX_BODY = 1024

type Action = bytes | float | Literal["close"]
type Script = Callable[[int], Sequence[Action]]


# A strict upstream-side parser: the "different parser" every forwarded byte must read the same way.

_TOKEN = rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_REQUEST_LINE = re.compile(rb"(" + _TOKEN + rb") (\S+) HTTP/1\.1")
_HEADER_LINE = re.compile(rb"(" + _TOKEN + rb"):[ \t]*([^\r\n]*?)[ \t]*")
_CHUNK_SIZE = re.compile(rb"[0-9a-f]+")


@dataclass(frozen=True)
class Forwarded:
    """One request as the upstream read it; `body` is None when the body never fully arrived."""

    method: bytes
    target: bytes
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes | None

    def header(self, name: bytes) -> list[bytes]:
        """Return every value of the lowercase header `name`, in order."""
        return [value for key, value in self.headers if key == name]


def _strict_head(head: bytes) -> tuple[bytes, bytes, tuple[tuple[bytes, bytes], ...]]:
    lines = head.split(b"\r\n")
    assert not [line for line in lines if b"\r" in line or b"\n" in line], f"bare CR or LF in {head!r}"
    request_line = _REQUEST_LINE.fullmatch(lines[0])
    assert request_line, f"request line {lines[0]!r}"
    headers = []
    for line in lines[1:]:
        # A folded continuation line starts with whitespace, which is not a header name.
        header = _HEADER_LINE.fullmatch(line)
        assert header, f"header line {line!r}"
        headers.append((header[1].lower(), header[2]))
    return request_line[1], request_line[2], tuple(headers)


def _strict_chunked(rest: bytes) -> tuple[bytes | None, bytes]:
    body = bytearray()
    while True:
        line, separator, after = rest.partition(b"\r\n")
        if not separator:
            return None, b""
        assert _CHUNK_SIZE.fullmatch(line), f"chunk size line {line!r}"
        size = int(line, 16)
        if size == 0:
            if len(after) < 2:
                return None, b""
            assert after[:2] == b"\r\n", f"trailer fields forwarded: {after[:80]!r}"
            return bytes(body), after[2:]
        if len(after) < size + 2:
            return None, b""
        assert after[size : size + 2] == b"\r\n", "chunk data not followed by CRLF"
        body += after[:size]
        rest = after[size + 2 :]


def _strict_body(headers: tuple[tuple[bytes, bytes], ...], rest: bytes) -> tuple[bytes | None, bytes]:
    lengths = [value for name, value in headers if name == b"content-length"]
    codings = [value for name, value in headers if name == b"transfer-encoding"]
    assert not (lengths and codings), "Content-Length beside Transfer-Encoding"
    assert len(lengths) <= 1, f"repeated Content-Length {lengths!r}"
    assert len(codings) <= 1, f"repeated Transfer-Encoding {codings!r}"
    if codings:
        assert codings == [b"chunked"], f"Transfer-Encoding {codings!r}"
        return _strict_chunked(rest)
    assert not lengths or lengths[0].isdigit(), f"Content-Length {lengths!r}"
    size = int(lengths[0]) if lengths else 0
    if len(rest) < size:
        return None, b""
    return rest[:size], rest[size:]


def parse_forwarded(raw: bytes, *, final: bool = False) -> list[Forwarded]:
    """Read one connection's bytes as a strict server would; the last request may be partial.

    Bytes after a complete request must start another well-formed head, so the count is exact. Once
    everything has arrived (`final`), bytes that never complete a head are refused too.
    """
    requests: list[Forwarded] = []
    while raw:
        head, separator, rest = raw.partition(b"\r\n\r\n")
        if not separator:
            assert not final, f"bytes that start no complete head: {raw[:80]!r}"
            break
        method, target, headers = _strict_head(head)
        body, raw = _strict_body(headers, rest)
        requests.append(Forwarded(method, target, headers, body))
        if body is None:
            break
    return requests


# A fake upstream that records raw bytes per connection and answers each complete request with a script.


@dataclass
class RawUpstream:
    """A loopback upstream whose `script(n)` gives the actions answering the n-th complete request it reads.

    An action writes bytes, sleeps for a float of seconds, or closes the connection ("close").
    `n` counts across connections, so a redialed connection continues the script. Bytes are recorded
    as they arrive, so a reset that follows them cannot hide them.
    """

    script: Script
    port: int = 0
    connections: list[bytearray] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    requested: int = 0
    answered: int = 0
    transports: list[asyncio.BaseTransport] = field(default_factory=list)

    @property
    def raw(self) -> bytes:
        """Return every byte received, across connections."""
        return b"".join(self.connections)

    @property
    def requests(self) -> list[Forwarded]:
        """Return every request received, across connections, including a partial last one per connection."""
        return [request for received in self.connections for request in parse_forwarded(bytes(received), final=True)]

    @property
    def complete(self) -> list[Forwarded]:
        """Return the requests that arrived whole."""
        return [request for request in self.requests if request.body is not None]

    async def wait_answered(self, count: int) -> None:
        """Wait until the script ran for `count` requests."""
        async with asyncio.timeout(5):
            while self.answered < count:  # noqa: ASYNC110
                await asyncio.sleep(0.01)


class UpstreamConnection(asyncio.Protocol):
    """One connection to a `RawUpstream`: records each byte as it arrives and runs the script per whole request."""

    def __init__(self, upstream: RawUpstream) -> None:
        self._upstream = upstream
        self._received = bytearray()
        self._handled = 0
        self._pending: asyncio.Queue[int] = asyncio.Queue()
        self._transport: asyncio.Transport | None = None
        self._responder: asyncio.Task[None] | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Register the connection with its upstream and start answering its requests."""
        assert isinstance(transport, asyncio.Transport)
        self._transport = transport
        self._upstream.connections.append(self._received)
        self._upstream.transports.append(transport)
        self._responder = asyncio.get_running_loop().create_task(self._respond(transport))

    def data_received(self, data: bytes) -> None:
        """Record the bytes and queue an answer for each request they complete; abort on a non-strict request."""
        self._received += data
        try:
            complete = sum(request.body is not None for request in parse_forwarded(bytes(self._received)))
        except AssertionError as exc:
            self._upstream.errors.append(str(exc))
            assert self._transport is not None
            self._transport.abort()
            return
        for _ in range(complete - self._handled):
            self._pending.put_nowait(self._upstream.requested)
            self._upstream.requested += 1
        self._handled = complete

    def connection_lost(self, exc: Exception | None) -> None:  # noqa: ARG002 - the Protocol signature
        """Stop answering."""
        if self._responder is not None:
            self._responder.cancel()

    async def _respond(self, transport: asyncio.Transport) -> None:
        while True:
            index = await self._pending.get()
            try:
                for action in self._upstream.script(index):
                    if action == "close":
                        transport.close()
                        return
                    if isinstance(action, float):
                        await asyncio.sleep(action)
                    else:
                        assert isinstance(action, bytes)
                        transport.write(action)
            finally:
                self._upstream.answered += 1


type UpstreamFactory = Callable[..., Awaitable[RawUpstream]]


# The worker side: a raw client connection that keeps every byte the broker sent.


@dataclass(frozen=True)
class Reply:
    """One response read off the client socket; `complete` is False when the broker cut it off mid-body."""

    status: int
    headers: dict[str, str]
    body: bytes
    complete: bool = True


@dataclass
class Client:
    """A raw connection to the broker: plain, or switched to TLS inside a CONNECT tunnel."""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    received: bytearray = field(default_factory=bytearray)

    def send(self, data: bytes) -> None:
        """Write raw bytes to the broker."""
        self.writer.write(data)

    async def _read(self, operation: Awaitable[bytes]) -> bytes:
        data = await operation
        self.received += data
        return data

    async def _body(self, headers: dict[str, str], *, head: bool, status: int) -> bytes:
        if head or status < 200 or status in {204, 304}:
            return b""
        if "content-length" in headers:
            return await self._read(self.reader.readexactly(int(headers["content-length"])))
        if headers.get("transfer-encoding") == "chunked":
            body = bytearray()
            while size := int((await self._read(self.reader.readuntil(b"\r\n"))).strip(), 16):
                body += (await self._read(self.reader.readexactly(size + 2)))[:-2]
            await self._read(self.reader.readuntil(b"\r\n"))
            return bytes(body)
        return await self._read(self.reader.read())

    async def reply(self, *, head: bool = False) -> Reply | None:
        """Read one response; None when the broker closed the connection before sending one."""
        async with asyncio.timeout(10):
            try:
                raw_head = await self._read(self.reader.readuntil(b"\r\n\r\n"))
            except (asyncio.IncompleteReadError, ConnectionResetError, ssl.SSLError) as exc:
                if isinstance(exc, asyncio.IncompleteReadError):
                    self.received += exc.partial
                return None
            status, _reason, headers = parse_head(raw_head)
            try:
                body = await self._body(headers, head=head, status=status)
            except (asyncio.IncompleteReadError, ConnectionResetError, ssl.SSLError) as exc:
                partial = exc.partial if isinstance(exc, asyncio.IncompleteReadError) else b""
                self.received += partial
                return Reply(status, headers, partial, complete=False)
        return Reply(status, headers, body)

    async def closed(self) -> bool:
        """Return whether the broker closes the connection without sending anything more."""
        try:
            async with asyncio.timeout(10):
                rest = await self._read(self.reader.read())
        except (ConnectionResetError, ssl.SSLError):
            rest = b""
        return rest == b""

    def close(self) -> None:
        """Drop the connection without a clean shutdown."""
        self.writer.transport.abort()


async def open_client(port: int) -> Client:
    """Open a raw connection to the broker listening on `port`."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    return Client(reader, writer)


async def open_tunnel(
    broker: BrokerFactory,
    target_port: int,
    *,
    host: str = "localhost",
    context: ssl.SSLContext | None = None,
    server_hostname: str | None = "localhost",
) -> Client:
    """CONNECT to ``<host>:<target_port>`` and start TLS inside the tunnel, trusting the broker CA."""
    client = await open_client(broker.latest.port)
    client.send(connect_request(f"{host}:{target_port}", authorization=proxy_authorization(broker.token())))
    assert (await read_raw_response(client.reader)).status == 200
    await client.writer.start_tls(
        context or ssl.create_default_context(cadata=broker.ca.cert_pem),
        server_hostname=server_hostname,
    )
    return client


@dataclass(frozen=True)
class Target:
    """Where a corpus request goes: absolute-form with proxy credentials, or origin-form inside a tunnel."""

    prefix: str
    host: str
    auth: str

    def render(self, template: str) -> bytes:
        """Fill ``{t}`` (the target before the path), ``{h}`` (the Host value), and ``{a}`` (the credential line)."""
        return template.format(t=self.prefix, h=self.host, a=self.auth).encode("latin-1")


async def client_for(broker: BrokerFactory, port: int, *, tunnel: bool) -> tuple[Client, Target]:
    """Open a connection whose requests reach the upstream on `port`: through an intercepted tunnel, or as plain HTTP.

    The plain upstream is 127.0.0.1, a host without rules, because a plain request a rule matches is refused.
    """
    if tunnel:
        client = await open_tunnel(broker, port)
        return client, Target(prefix="", host=f"localhost:{port}", auth="")
    client = await open_client(broker.latest.port)
    authority = f"127.0.0.1:{port}"
    credential = f"Proxy-Authorization: {proxy_authorization(broker.token())}\r\n"
    return client, Target(prefix=f"http://{authority}", host=authority, auth=credential)


def assert_contained(
    *,
    intercepted: RawUpstream | None,
    others: Sequence[RawUpstream],
    client_bytes: bytes,
    host: str,
) -> None:
    """Check what reached each upstream: strictly framed, the secret only on the CONNECT host's own requests.

    Every request the intercepted upstream received, whole or partial, carries the injected value exactly once,
    and the secret appears nowhere else: not at another upstream, not in anything relayed back to the client.
    """
    for upstream in [*others, *([intercepted] if intercepted else [])]:
        assert upstream.errors == []
        for request in upstream.requests:
            assert request.header(b"host") == [host.encode()]
            assert b"smuggled" not in request.target.lower()
            assert not [value for _, value in request.headers if b"evil.example" in value]
    for other in others:
        assert SECRET.encode() not in other.raw
    if intercepted is not None:
        for request in intercepted.requests:
            assert request.header(b"authorization") == [INJECTED]
        assert intercepted.raw.count(SECRET.encode()) == len(intercepted.requests)
    assert SECRET.encode() not in client_bytes


async def assert_listener_serves(broker: BrokerFactory, probe: RawUpstream) -> None:
    """A fresh connection still gets a valid plain request through, and the probe receives exactly it."""
    before = len(probe.complete)
    client = await open_client(broker.latest.port)
    try:
        client.send(
            (
                f"GET http://127.0.0.1:{probe.port}/probe HTTP/1.1\r\nHost: 127.0.0.1:{probe.port}\r\n"
                f"Proxy-Authorization: {proxy_authorization(broker.token())}\r\nConnection: close\r\n\r\n"
            ).encode(),
        )
        reply = await client.reply()
    finally:
        client.close()
    assert reply is not None
    assert (reply.status, reply.body) == (200, b"ok")
    assert [request.target for request in probe.complete[before:]] == [b"/probe"]
