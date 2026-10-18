"""HTTP/1.1 plumbing shared by the proxy listener and TLS interception: streams, errors, dialing, audit."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qs, urlsplit

import h11

from mindroom.egress_broker.audit import AuditRecord
from mindroom.egress_broker.dial import DestinationBlockedError, DialPolicy, open_upstream
from mindroom.egress_broker.messages import git_error_text, refusal_body
from mindroom.egress_broker.rules import route_request, strip_hop_by_hop
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    import ssl
    from collections.abc import Awaitable, Callable

    from mindroom.egress_broker.audit import AuditLog
    from mindroom.egress_broker.messages import ScopeInfo
    from mindroom.egress_broker.rules import EgressRules, Route
    from mindroom.egress_broker.tokens import WorkerClaims

__all__ = [
    "AuditEntry",
    "Peer",
    "Relay",
    "close_stream",
    "host_not_allowed",
    "normalize_host",
    "send_json",
    "send_proxy_challenge",
    "serve_peer",
    "split_authority",
    "upstream_request_headers",
]

_CHUNK = 64 * 1024
# A client's request head, request line through the blank line, whether it arrives in one read or many.
_MAX_REQUEST_HEAD = 64 * 1024
# How long a closing connection gets: to flush what was written, or for an upstream that failed an upload to answer.
_CLOSE_TIMEOUT = 5.0
# How long a peer may send nothing, while the broker waits on it or splices, before the connection ends.
_IDLE_TIMEOUT = 1800.0
_PROXY_AUTHENTICATE = (b"proxy-authenticate", b'Basic realm="mindroom-egress-broker"')
_HOSTNAME = re.compile(r"[a-z0-9_][a-z0-9_.-]*")
# A refusal's reason phrase repeats its message for clients that print only the status line.
_REASON_LIMIT = 200
# The git ERR line has room for the whole message; this only bounds a message that quotes a huge path.
_GIT_ERROR_LIMIT = 4096
_GIT_DISCOVERY_SERVICES = frozenset({"git-upload-pack", "git-receive-pack"})
# Methods that ask a server to echo the request back, which would return an injected credential.
_ECHO_METHODS = frozenset({"TRACE", "TRACK"})
# Names a broker refusal's code; h11 lowercases header names, so upstream copies compare equal.
_ERROR_HEADER = b"x-mindroom-error"

logger = get_logger(__name__)


class _RequestFailedError(Exception):
    """A brokered request failed with a status and error code the client should receive."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(status)
        self.status = status
        self.code = code


@dataclass
class AuditEntry:
    """Metadata for one audited tunnel or request; never holds header values or queries."""

    claims: WorkerClaims
    kind: Literal["request", "tunnel", "denied"]
    method: str
    host: str
    path: str
    service: str | None = None
    status: int = 0
    bytes_up: int = 0
    bytes_down: int = 0
    code: str | None = None
    started: float = field(default_factory=time.monotonic)


class Peer:
    """One HTTP/1.1 side of a brokered connection, driven by an h11 state machine."""

    def __init__(self, conn: h11.Connection, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.conn = conn
        self.reader = reader
        self.writer = writer
        # The request being answered, for the shape of a refusal; None until one is read in this cycle.
        self.request: h11.Request | None = None
        # Whether the connection must close after the response (RFC 9112 section 6.1).
        self.close_after_response = False

    @classmethod
    def serving(cls, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> Peer:
        """Return the broker's side of a client connection, which reads request heads of up to 64 KiB."""
        conn = h11.Connection(h11.SERVER, max_incomplete_event_size=_MAX_REQUEST_HEAD)
        return cls(conn, reader, writer)

    async def next_event(self) -> h11.Event | type[h11.NEED_DATA | h11.PAUSED]:
        """Return the next event, reading from the stream until h11 has one (never NEED_DATA).

        A request head over 64 KiB is refused with 431 however it arrives: h11 limits only an incomplete head, so a
        complete one that came in a single read is measured here. A request with both Content-Length and
        Transfer-Encoding is served, framed by the latter, but its connection closes after the response, so nothing
        a parser that trusted the length would read as a further request is ever read.
        """
        awaiting_head = self.conn.their_role is h11.CLIENT and self.conn.their_state is h11.IDLE
        # Bytes h11 holds or receives while it parses the head, minus what it holds afterwards, are the head.
        consumed = len(self.conn.trailing_data[0]) if awaiting_head else 0
        while (event := self.conn.next_event()) is h11.NEED_DATA:
            async with asyncio.timeout(_IDLE_TIMEOUT):
                data = await self.reader.read(_CHUNK)
            consumed += len(data)
            self.conn.receive_data(data)
        if isinstance(event, h11.Request):
            self.request = event
            if consumed - len(self.conn.trailing_data[0]) > _MAX_REQUEST_HEAD:
                msg = "Request head too large."
                raise h11.RemoteProtocolError(msg, error_status_hint=431)
            names = {name for name, _ in event.headers}
            self.close_after_response = {b"content-length", b"transfer-encoding"} <= names
        return event

    def start_next_cycle(self) -> None:
        """Get ready for the next request on this connection, forgetting the one just answered."""
        self.conn.start_next_cycle()
        self.request = None
        self.close_after_response = False

    async def send(self, *events: h11.Event) -> None:
        for event in events:
            if data := self.conn.send(event):
                self.writer.write(data)
        await self.writer.drain()

    async def sent_unprompted(self) -> bool:
        """Return, almost without waiting, whether the peer closed or sent bytes after the last message h11 read.

        An idle upstream answers nothing yet, so such bytes would be taken as the next request's response. They are
        found in h11's buffer or the stream's. Bytes still in the socket reach the stream only in the loop iteration
        in which the first read's zero timeout cancels it, so a second read takes them from the stream. Bytes that
        reach the socket after that iteration polled it are not seen, the same window as urllib3's
        `is_connection_dropped`. They can only come from this tunnel's own upstream, and the next response read on
        this tunnel would take them; no other tunnel, host, or requester is affected.
        """
        if self.conn.trailing_data[0]:
            return True
        for _attempt in range(2):
            try:
                # A read that cannot complete at once is cancelled on the next loop iteration.
                async with asyncio.timeout(0):
                    await self.reader.read(1)
            except TimeoutError:
                continue
            except OSError:
                return True
            return True
        return False


def _printable(text: str, *, limit: int) -> str:
    """Return `text` as printable ASCII of at most `limit` characters: CR and LF removed, other characters as `?`."""
    text = text.replace("\r", "").replace("\n", "")
    return "".join(char if " " <= char <= "~" else "?" for char in text[:limit])


def _git_discovery_service(request: h11.Request) -> str | None:
    """Return the service a git smart-HTTP discovery request names (`GET .../info/refs?service=git-upload-pack`).

    Only a git client's request (`User-Agent: git/...` or a `Git-Protocol` header) counts. Any target that does not
    parse as a URL is not one, since refusing a malformed request must never fail.
    """
    from_git = any(
        name == b"git-protocol" or (name == b"user-agent" and value.startswith(b"git/"))
        for name, value in request.headers
    )
    if request.method != b"GET" or not from_git:
        return None
    try:
        parts = urlsplit(request.target.decode("latin-1"))
    except ValueError:
        return None
    services = parse_qs(parts.query).get("service", [])
    if not parts.path.endswith("/info/refs") or len(services) != 1 or services[0] not in _GIT_DISCOVERY_SERVICES:
        return None
    return services[0]


def _pkt_line(data: bytes) -> bytes:
    """Frame `data` as one git pkt-line: four hex digits giving the length, the prefix included, then the data."""
    return b"%04x" % (len(data) + 4) + data


async def send_json(
    client: Peer,
    status: int,
    body: dict[str, object],
    *,
    headers: tuple[tuple[bytes, bytes], ...] = (),
) -> None:
    """Answer the client with a refusal body from `refusal_body` and close, unless a response is already under way.

    Every refusal carries `X-MindRoom-Error: <code>`, and its reason phrase is the body's message (printable ASCII, at
    most 200 characters), so clients that print the status line's reason, such as Python's urllib, still show it.
    A git client's (`User-Agent: git/...` or a `Git-Protocol` header) refused smart-HTTP discovery request
    (`GET .../info/refs?service=git-upload-pack` or `git-receive-pack`) gets a 200 advertisement whose pkt-lines end
    in `ERR <message>`, the message naming the manage link itself, so git prints `remote error: <message>` instead of
    a bare status; the caller's audit row keeps the refusal's own status and code. A 407 stays a 407 for git too:
    git's curl answers the challenge with the proxy credentials.
    """
    if client.conn.our_state not in {h11.IDLE, h11.SEND_RESPONSE}:
        return
    code, message = str(body["error"]), str(body["message"])
    request = client.request
    git_service = _git_discovery_service(request) if status != 407 and request is not None else None
    if git_service is None:
        content_type = b"application/json"
        payload = json.dumps(body).encode()
    else:
        status = 200
        content_type = f"application/x-{git_service}-advertisement".encode()
        error = _printable(git_error_text(body), limit=_GIT_ERROR_LIMIT)
        payload = _pkt_line(f"# service={git_service}\n".encode()) + b"0000" + _pkt_line(f"ERR {error}\n".encode())
    response = h11.Response(
        status_code=status,
        reason=_printable(message, limit=_REASON_LIMIT).encode(),
        headers=[
            (b"content-type", content_type),
            (b"content-length", str(len(payload)).encode()),
            (b"connection", b"close"),
            (_ERROR_HEADER, code.encode()),
            *headers,
        ],
    )
    events: list[h11.Event] = [response]
    if request is None or request.method != b"HEAD":
        events.append(h11.Data(data=payload))
    await client.send(*events, h11.EndOfMessage())


def host_not_allowed(rules: EgressRules, host: str) -> dict[str, object]:
    """Return the 403 body for a host `deny` refuses; it lists the operator's services, whose hosts are reachable."""
    return refusal_body("host_not_allowed", host=host, services=list(rules.operator.services))


async def send_proxy_challenge(client: Peer) -> None:
    """Answer 407 with a Basic challenge: the proxy token is missing, invalid, or expired."""
    await send_json(client, 407, refusal_body("proxy_authentication_required"), headers=(_PROXY_AUTHENTICATE,))


async def serve_peer(peer: Peer, serve_one: Callable[[Peer], Awaitable[bool]]) -> None:
    """Serve requests on `peer` until `serve_one` returns False, containing every failure to this connection."""
    try:
        try:
            while await serve_one(peer):
                peer.start_next_cycle()
        except h11.RemoteProtocolError as exc:
            await send_json(peer, exc.error_status_hint, refusal_body("bad_request"))
    except OSError as exc:
        # Peers disconnect and time out (TimeoutError is an OSError); exception text can echo header bytes.
        logger.debug("egress_broker_connection_failed", error_type=type(exc).__name__)
    except Exception as exc:
        # Last-resort guard of one connection: anything else is a broker fault that fails it, never the listener.
        logger.warning("egress_broker_connection_error", error_type=type(exc).__name__)
        # The fault left the connection in an unknown state, so this answer is best effort whatever stops it.
        with contextlib.suppress(Exception):
            await send_json(peer, 502, refusal_body("broker_error"))


async def close_stream(writer: asyncio.StreamWriter) -> None:
    """Close a stream after flushing what was written, aborting a peer that stops reading."""
    writer.close()
    try:
        # wait_closed re-raises whatever closed the transport, which asyncio does not limit to OSError; abort follows.
        with contextlib.suppress(Exception):
            async with asyncio.timeout(_CLOSE_TIMEOUT):
                await writer.wait_closed()
    finally:
        writer.transport.abort()


async def _from_upstream[T](operation: Awaitable[T]) -> T:
    """Await one upstream operation, mapping its failures to the status the client receives."""
    try:
        return await operation
    except TimeoutError as exc:
        raise _RequestFailedError(504, "upstream_timeout") from exc
    except (OSError, h11.ProtocolError) as exc:
        raise _RequestFailedError(502, "upstream_unreachable") from exc


async def _read_response(upstream: Peer) -> h11.Response | h11.InformationalResponse:
    """Return the final response head, or a 101 that switches protocols, skipping other informational responses."""
    while True:
        event = await upstream.next_event()
        if isinstance(event, h11.Response):
            return event
        if not isinstance(event, h11.InformationalResponse):
            msg = "Upstream closed before responding."
            raise ConnectionError(msg)
        if event.status_code == 101:
            return event


async def _keep_first_read(upstream: Peer, read: asyncio.Task[bytes]) -> None:
    """Hand h11 what `read` took from the upstream, or cancel it and leave any unread bytes in the stream.

    A failed read is not raised here: the stream keeps the error, and the next read raises it.
    """
    if not read.done():
        read.cancel()
        # Awaited, so no read is still waiting on the stream when the next one starts.
        await asyncio.wait([read])
    if not read.cancelled() and read.exception() is None:
        upstream.conn.receive_data(read.result())


def normalize_host(host: str) -> str:
    """Return a lowercase hostname without its trailing dot or a compressed IPv6 literal, rejecting anything else."""
    host = host.lower().removesuffix(".")
    if ":" in host:
        if "%" in host:
            msg = "Zone-scoped addresses are not supported."
            raise ValueError(msg)
        return ipaddress.IPv6Address(host).compressed
    if not _HOSTNAME.fullmatch(host):
        msg = "Invalid host."
        raise ValueError(msg)
    return host


def split_authority(text: str) -> tuple[str, str]:
    """Split ``host[:port]`` or ``[v6][:port]`` into the normalized host and the port text, which may be empty.

    Raises ValueError for a malformed host, bracket, or port; a port is at most five ASCII digits.
    """
    if text.startswith("["):
        host, bracket, port = text[1:].partition("]")
        if not bracket or ":" not in host or (port and not port.startswith(":")):
            msg = "Invalid IPv6 authority."
            raise ValueError(msg)
        port = port.removeprefix(":")
    else:
        host, _, port = text.partition(":")
    # str.isdigit alone would accept Latin-1 digits such as a superscript two.
    if port and not (port.isascii() and port.isdigit() and len(port) <= 5):
        msg = "Invalid port."
        raise ValueError(msg)
    return normalize_host(host), port


def upstream_request_headers(received: list[tuple[bytes, bytes]], *, host: bytes) -> list[tuple[bytes, bytes]]:
    """Return the headers to send upstream: Host set to `host`, hop-by-hop removed, framing rebuilt.

    The broker answers ``Expect: 100-continue`` itself. Framing follows what h11 read, whatever the client's
    Connection header lists: a chunked body is re-chunked, so a client Content-Length beside it is dropped
    rather than forwarded next to the new Transfer-Encoding; otherwise the Content-Length is kept.
    """
    chunked = any(name == b"transfer-encoding" for name, _ in received)
    length = next((value for name, value in received if name == b"content-length"), None)
    headers = [(b"host", host)]
    headers += [
        (name, value)
        for name, value in strip_hop_by_hop(received, keep_upgrade=False)
        if name not in {b"host", b"expect", b"content-length"}
    ]
    if chunked:
        headers.append((b"transfer-encoding", b"chunked"))
    elif length is not None:
        headers.append((b"content-length", length))
    return headers


def _downstream_response_headers(
    received: list[tuple[bytes, bytes]],
    *,
    strip_cookies: bool,
    keep_upgrade: bool,
) -> list[tuple[bytes, bytes]]:
    """Return the upstream response headers to relay: hop-by-hop removed, Set-Cookie only for hosts with rules.

    A chunked upstream body is re-framed for the client, so a Content-Length beside it is dropped
    rather than kept as the only (wrong) framing header. `X-MindRoom-Error` is dropped too, so only the broker's own
    refusals carry it and an upstream cannot pass its response off as one.
    """
    chunked = any(name == b"transfer-encoding" for name, _ in received)
    dropped = {_ERROR_HEADER, b"set-cookie"} if strip_cookies else {_ERROR_HEADER}
    return [
        (name, value)
        for name, value in strip_hop_by_hop(received, keep_upgrade=keep_upgrade)
        if not (chunked and name == b"content-length") and name not in dropped
    ]


@dataclass(frozen=True)
class Relay:
    """Per-request work both listener paths share: reading rules, dialing, streaming, answering, and auditing.

    `manage_url` is where the verified requester changes their egress keys and services, and `scope_info` returns who
    else can change them on a shared agent, and whether a personal scope's page is served at all, for refusal bodies.
    """

    audit: AuditLog
    rules_provider: Callable[[WorkerClaims], EgressRules]
    manage_url: str | None
    scope_info: Callable[[WorkerClaims], ScopeInfo]
    dial_policy: DialPolicy
    max_body_bytes: int

    async def read_rules(self, client: Peer, entry: AuditEntry) -> EgressRules | None:
        """Return the rules for the entry's requester; when the provider fails, answer 502, audit, and return None.

        The provider runs in a thread: it may read the requester scope's own services from the credential store.
        """
        try:
            return await asyncio.to_thread(self.rules_provider, entry.claims)
        except Exception as exc:
            # The provider runs the owner's config loader and reads the credential store; either can raise anything.
            logger.warning("egress_broker_config_unavailable", error_type=type(exc).__name__)
            await self.reject(client, entry, 502, refusal_body("broker_error"))
            return None

    async def route(self, client: Peer, entry: AuditEntry, port: int) -> tuple[EgressRules, Route] | None:
        """Return the rules and the route for the entry's host, `port`, and path; otherwise answer, audit, return None.

        A host without rules is denied under `unmatched_hosts: deny`. On a host with rules, `TRACE` and `TRACK` (in any
        letter case) get 405 `method_not_allowed`, since the upstream would echo the injected credential back. A path
        the route refuses (ambiguous, or unlisted on a restricted host) is refused, and the audit row names the
        restricting service of a `path_not_allowed` refusal, as a matched rule's row does. A failing rules provider or
        scope callback is a 502.
        """
        rules = await self.read_rules(client, entry)
        if rules is None:
            return None
        route = route_request(rules, entry.host, port, entry.path)
        if not route.host_has_rules and rules.unmatched_hosts == "deny":
            await self.deny(client, entry, host_not_allowed(rules, entry.host))
            return None
        if route.host_has_rules and entry.method.upper() in _ECHO_METHODS:
            await self.deny(client, entry, refusal_body("method_not_allowed"), status=405)
            return None
        if route.refusal is None:
            return rules, route
        entry.service = route.restricted_by
        try:
            scope = self.scope_info(entry.claims)
        except Exception as exc:
            # A callback the broker's owner supplies, which can raise anything.
            logger.warning("egress_broker_refusal_context_failed", error_type=type(exc).__name__)
            await self.reject(client, entry, 502, refusal_body("broker_error"))
            return None
        if route.refusal == "bad_request":
            await self.deny(client, entry, refusal_body("bad_request", path=entry.path), status=400)
        else:
            body = refusal_body(
                "path_not_allowed",
                host=entry.host,
                path=entry.path,
                service=route.restricted_by,
                service_label=route.restricted_by_label,
                shared=scope.shared,
                managers=scope.managers,
                personal_page=scope.personal_page,
                by_operator=route.restricted_by_operator,
                manage_url=self.manage_url,
            )
            await self.deny(client, entry, body)
        return None

    async def open_upstream(
        self,
        client: Peer,
        entry: AuditEntry,
        port: int,
        rules: EgressRules,
        *,
        ssl_context: ssl.SSLContext | None,
    ) -> Peer | None:
        """Dial `entry.host` through the guard and the rules' private networks; on failure answer, audit, return None."""
        policy = replace(self.dial_policy, private_networks=rules.private_networks)
        try:
            reader, writer = await _from_upstream(
                open_upstream(entry.host, port, policy=policy, ssl_context=ssl_context),
            )
        except DestinationBlockedError:
            await self.deny(client, entry, refusal_body("destination_blocked", host=entry.host))
        except _RequestFailedError as exc:
            await self.reject(client, entry, exc.status, refusal_body(exc.code, host=entry.host))
        else:
            return Peer(h11.Connection(h11.CLIENT), reader, writer)
        return None

    async def refuse_large_body(self, client: Peer, entry: AuditEntry, request: h11.Request) -> bool:
        """Answer 413 and return True when `request` declares a body over the limit, before anything is dialed."""
        declared = next((int(value) for name, value in request.headers if name == b"content-length"), 0)
        if declared <= self.max_body_bytes:
            return False
        await self.reject(client, entry, 413, refusal_body("request_body_too_large"))
        return True

    async def deny(self, client: Peer, entry: AuditEntry, body: dict[str, object], *, status: int = 403) -> None:
        entry.kind = "denied"
        await self.reject(client, entry, status, body)

    async def reject(self, client: Peer, entry: AuditEntry, status: int, body: dict[str, object]) -> None:
        entry.status = status
        error = body.get("error")
        entry.code = error if isinstance(error, str) else None
        await send_json(client, status, body)
        await self.record(entry)

    async def record(self, entry: AuditEntry) -> None:
        record = AuditRecord(
            at=datetime.now(UTC),
            kind=entry.kind,
            scope=entry.claims.scope_label,
            agent_name=entry.claims.agent_name,
            requester_id=entry.claims.requester_id,
            method=entry.method,
            host=entry.host,
            path=entry.path,
            service=entry.service,
            status=entry.status,
            bytes_up=entry.bytes_up,
            bytes_down=entry.bytes_down,
            duration_ms=int((time.monotonic() - entry.started) * 1000),
            code=entry.code,
        )
        try:
            await asyncio.to_thread(self.audit.record, record)
        except sqlite3.Error as exc:
            logger.warning("egress_broker_audit_write_failed", error_type=type(exc).__name__)

    async def splice(self, client: Peer, upstream: Peer, entry: AuditEntry) -> None:
        """Copy bytes both ways, starting with any each h11 side already buffered, until the streams end.

        A side that ends is half-closed where the transport allows it (plain TCP) and closed where it
        does not (TLS), so the other side ends too. Neither side sending for the idle timeout also ends it.
        """
        loop = asyncio.get_running_loop()
        buffered_up, _ = client.conn.trailing_data
        buffered_down, _ = upstream.conn.trailing_data
        entry.bytes_up += len(buffered_up)
        entry.bytes_down += len(buffered_down)
        upstream.writer.write(buffered_up)
        client.writer.write(buffered_down)

        async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, upload: bool) -> None:
            try:
                while data := await reader.read(_CHUNK):
                    idle.reschedule(loop.time() + _IDLE_TIMEOUT)
                    if upload:
                        entry.bytes_up += len(data)
                    else:
                        entry.bytes_down += len(data)
                    writer.write(data)
                    await writer.drain()
                if writer.can_write_eof():
                    writer.write_eof()
                else:
                    writer.close()
            except OSError:
                writer.transport.abort()

        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(_IDLE_TIMEOUT) as idle, asyncio.TaskGroup() as group:
                group.create_task(pump(client.reader, upstream.writer, upload=True))
                group.create_task(pump(upstream.reader, client.writer, upload=False))

    async def _upload(
        self,
        client: Peer,
        upstream: Peer,
        entry: AuditEntry,
    ) -> h11.Response | h11.InformationalResponse:
        """Stream the request body upstream, counting it against the body limit, and return the response head.

        When a body write fails, a final response the upstream sent within `_CLOSE_TIMEOUT` is returned, and the client
        connection, whose remaining body goes unread, closes after it; otherwise the write's failure is raised.
        """
        # The response's first read runs during the upload: once a write fails, asyncio raises that error on every
        # read, even of bytes it already received.
        first_read = asyncio.create_task(upstream.reader.read(_CHUNK))
        upload_error: _RequestFailedError | None = None
        try:
            while isinstance(event := await client.next_event(), h11.Data):
                entry.bytes_up += len(event.data)
                if entry.bytes_up > self.max_body_bytes:
                    raise _RequestFailedError(413, "request_body_too_large")  # noqa: TRY301 - forward answers it
                await _from_upstream(upstream.send(event))
            await _from_upstream(upstream.send(h11.EndOfMessage()))
        except _RequestFailedError as exc:
            if exc.code == "request_body_too_large":
                raise
            upload_error = exc
        finally:
            await _keep_first_read(upstream, first_read)
        if upload_error is None:
            return await _from_upstream(_read_response(upstream))
        try:
            response = await _from_upstream(asyncio.wait_for(_read_response(upstream), _CLOSE_TIMEOUT))
        except _RequestFailedError:
            response = None
        if not isinstance(response, h11.Response):
            raise upload_error
        client.close_after_response = True
        return response

    async def forward(
        self,
        client: Peer,
        upstream: Peer,
        request: h11.Request,
        entry: AuditEntry,
        *,
        strip_cookies: bool,
    ) -> bool:
        """Relay one request and its response, then audit it; return whether the client connection stays open.

        Redirects are never followed. After a protocol switch (101) the connection becomes a byte splice until either
        side closes. An upstream may answer from the request head and close before taking the whole body, as with an
        early 413: when a body write fails, a final response it sent is relayed, and the client connection closes.
        """
        try:
            await _from_upstream(upstream.send(request))
            if client.conn.they_are_waiting_for_100_continue:
                await client.send(h11.InformationalResponse(status_code=100, headers=[]))
            response = await self._upload(client, upstream, entry)
            entry.status = response.status_code
            switched = isinstance(response, h11.InformationalResponse)
            headers = _downstream_response_headers(
                list(response.headers),
                strip_cookies=strip_cookies,
                keep_upgrade=switched,
            )
            if switched:
                await client.send(h11.InformationalResponse(status_code=101, reason=response.reason, headers=headers))
                await self.splice(client, upstream, entry)
                return False
            if client.close_after_response:
                # h11 then ends the client connection after this response.
                headers.append((b"connection", b"close"))
            await client.send(h11.Response(status_code=response.status_code, reason=response.reason, headers=headers))
            while isinstance(event := await _from_upstream(upstream.next_event()), h11.Data):
                entry.bytes_down += len(event.data)
                await client.send(event)
            await client.send(h11.EndOfMessage())
        except _RequestFailedError as exc:
            # A failure after the response head went out keeps that status; the client just sees the cut.
            entry.status = entry.status or exc.status
            entry.code = exc.code
            await send_json(client, exc.status, refusal_body(exc.code, host=entry.host))
            return False
        finally:
            await self.record(entry)
        return client.conn.our_state is h11.DONE and client.conn.their_state is h11.DONE
