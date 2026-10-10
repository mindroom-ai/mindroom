"""HTTP/1.1 plumbing shared by the proxy listener and TLS interception: streams, errors, dialing, audit."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Literal

import h11

from mindroom.egress_broker.audit import AuditRecord
from mindroom.egress_broker.dial import DestinationBlockedError, DialPolicy, open_upstream
from mindroom.egress_broker.rules import strip_request_headers, strip_response_headers
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    import ssl
    from collections.abc import Awaitable, Callable

    from mindroom.egress_broker.audit import AuditLog
    from mindroom.egress_broker.rules import EgressRules
    from mindroom.egress_broker.tokens import WorkerClaims

__all__ = [
    "AuditEntry",
    "Peer",
    "Relay",
    "close_stream",
    "normalize_host",
    "send_json",
    "send_proxy_challenge",
    "serve_peer",
    "upstream_request_headers",
]

_CHUNK = 64 * 1024
_CLOSE_TIMEOUT = 5.0
_PROXY_AUTHENTICATE = (b"proxy-authenticate", b'Basic realm="mindroom-egress-broker"')
_HOSTNAME = re.compile(r"[a-z0-9_][a-z0-9_.-]*")

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
    started: float = field(default_factory=time.monotonic)


class Peer:
    """One HTTP/1.1 side of a brokered connection, driven by an h11 state machine."""

    def __init__(
        self,
        conn: h11.Connection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        idle_timeout: float,
    ) -> None:
        self.conn = conn
        self.reader = reader
        self.writer = writer
        self.request_method = b""
        self._idle_timeout = idle_timeout

    async def next_event(self) -> h11.Event | type[h11.NEED_DATA | h11.PAUSED]:
        """Return the next event, reading from the stream until h11 has one (never NEED_DATA)."""
        while (event := self.conn.next_event()) is h11.NEED_DATA:
            async with asyncio.timeout(self._idle_timeout):
                data = await self.reader.read(_CHUNK)
            self.conn.receive_data(data)
        if isinstance(event, h11.Request):
            self.request_method = event.method
        return event

    async def send(self, *events: h11.Event) -> None:
        for event in events:
            if data := self.conn.send(event):
                self.writer.write(data)
        await self.writer.drain()


async def send_json(
    client: Peer,
    status: int,
    body: dict[str, object],
    *,
    headers: tuple[tuple[bytes, bytes], ...] = (),
) -> None:
    """Answer the client with a JSON error and close, unless a response is already under way."""
    if client.conn.our_state not in {h11.IDLE, h11.SEND_RESPONSE}:
        return
    payload = json.dumps(body).encode()
    response = h11.Response(
        status_code=status,
        reason=HTTPStatus(status).phrase.encode(),
        headers=[
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
            (b"connection", b"close"),
            *headers,
        ],
    )
    events: list[h11.Event] = [response]
    if client.request_method != b"HEAD":
        events.append(h11.Data(data=payload))
    await client.send(*events, h11.EndOfMessage())


async def send_proxy_challenge(client: Peer) -> None:
    """Answer 407 with a Basic challenge: the proxy token is missing, invalid, or expired."""
    await send_json(client, 407, {"error": "proxy_authentication_required"}, headers=(_PROXY_AUTHENTICATE,))


async def serve_peer(peer: Peer, serve_one: Callable[[Peer], Awaitable[bool]]) -> None:
    """Serve requests on `peer` until `serve_one` returns False, containing every failure to this connection."""
    try:
        try:
            while await serve_one(peer):
                peer.conn.start_next_cycle()
        except h11.RemoteProtocolError as exc:
            await send_json(peer, exc.error_status_hint, {"error": "bad_request"})
    except OSError as exc:
        # Peers disconnect and time out (TimeoutError is an OSError); exception text can echo header bytes.
        logger.debug("egress_broker_connection_failed", error_type=type(exc).__name__)
    except Exception as exc:
        # Anything else is a broker fault: it fails this connection only, never the listener.
        logger.warning("egress_broker_connection_error", error_type=type(exc).__name__)
        with contextlib.suppress(Exception):
            await send_json(peer, 502, {"error": "broker_error"})


async def close_stream(writer: asyncio.StreamWriter) -> None:
    """Close a stream after flushing what was written, aborting a peer that stops reading."""
    writer.close()
    try:
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


def upstream_request_headers(received: list[tuple[bytes, bytes]], *, host: bytes) -> list[tuple[bytes, bytes]]:
    """Return the headers to send upstream: Host set to `host`, hop-by-hop removed, framing rebuilt.

    The broker answers ``Expect: 100-continue`` itself. A chunked body is re-chunked, so a client
    Content-Length beside it is dropped rather than forwarded next to the new Transfer-Encoding.
    """
    chunked = any(name == b"transfer-encoding" for name, _ in received)
    dropped = {b"host", b"expect", b"content-length"} if chunked else {b"host", b"expect"}
    headers = [(b"host", host)]
    headers += [
        (name, value) for name, value in strip_request_headers(received, keep_upgrade=False) if name not in dropped
    ]
    if chunked:
        headers.append((b"transfer-encoding", b"chunked"))
    return headers


def _downstream_response_headers(
    received: list[tuple[bytes, bytes]],
    *,
    strip_cookies: bool,
    keep_upgrade: bool,
) -> list[tuple[bytes, bytes]]:
    """Return the upstream response headers to relay: hop-by-hop removed, Set-Cookie only for hosts with rules.

    A chunked upstream body is re-framed for the client, so a Content-Length beside it is dropped
    rather than kept as the only (wrong) framing header.
    """
    # Hop-by-hop removal is the same in both directions; only the response variant also drops Set-Cookie.
    strip = strip_response_headers if strip_cookies else strip_request_headers
    chunked = any(name == b"transfer-encoding" for name, _ in received)
    return [
        (name, value)
        for name, value in strip(received, keep_upgrade=keep_upgrade)
        if not (chunked and name == b"content-length")
    ]


@dataclass(frozen=True)
class Relay:
    """Per-request work both listener paths share: reading rules, dialing, streaming, answering, and auditing."""

    audit: AuditLog
    rules_provider: Callable[[WorkerClaims], EgressRules]
    dial_policy: DialPolicy
    max_body_bytes: int
    idle_timeout: float

    async def read_rules(self, client: Peer, entry: AuditEntry) -> EgressRules | None:
        """Return the rules for the entry's requester; when the provider fails, answer 502, audit, and return None.

        The provider runs in a thread: it may read the requester scope's own services from the credential store.
        """
        try:
            return await asyncio.to_thread(self.rules_provider, entry.claims)
        except Exception as exc:
            logger.warning("egress_broker_config_unavailable", error_type=type(exc).__name__)
            await self.reject(client, entry, 502, {"error": "broker_error"})
            return None

    async def open_upstream(
        self,
        client: Peer,
        entry: AuditEntry,
        port: int,
        *,
        ssl_context: ssl.SSLContext | None,
    ) -> Peer | None:
        """Dial `entry.host` through the guard; on failure answer the client, audit, and return None."""
        try:
            reader, writer = await _from_upstream(
                open_upstream(entry.host, port, policy=self.dial_policy, ssl_context=ssl_context),
            )
        except DestinationBlockedError:
            await self.deny(client, entry, {"error": "destination_blocked"})
        except _RequestFailedError as exc:
            await self.reject(client, entry, exc.status, {"error": exc.code})
        else:
            return Peer(h11.Connection(h11.CLIENT), reader, writer, idle_timeout=self.idle_timeout)
        return None

    async def refuse_large_body(self, client: Peer, entry: AuditEntry, request: h11.Request) -> bool:
        """Answer 413 and return True when `request` declares a body over the limit, before anything is dialed."""
        declared = next((int(value) for name, value in request.headers if name == b"content-length"), 0)
        if declared <= self.max_body_bytes:
            return False
        await self.reject(client, entry, 413, {"error": "request_body_too_large"})
        return True

    async def deny(self, client: Peer, entry: AuditEntry, body: dict[str, object], *, status: int = 403) -> None:
        entry.kind = "denied"
        await self.reject(client, entry, status, body)

    async def reject(self, client: Peer, entry: AuditEntry, status: int, body: dict[str, object]) -> None:
        entry.status = status
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
        )
        try:
            await asyncio.to_thread(self.audit.record, record)
        except Exception as exc:
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
                    idle.reschedule(loop.time() + self.idle_timeout)
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
            async with asyncio.timeout(self.idle_timeout) as idle, asyncio.TaskGroup() as group:
                group.create_task(pump(client.reader, upstream.writer, upload=True))
                group.create_task(pump(upstream.reader, client.writer, upload=False))

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

        After a protocol switch (101) the connection becomes a byte splice until either side closes.
        """
        try:
            if await self._exchange(client, upstream, request, entry, strip_cookies=strip_cookies):
                await self.splice(client, upstream, entry)
                return False
        except _RequestFailedError as exc:
            # A failure after the response head went out keeps that status; the client just sees the cut.
            entry.status = entry.status or exc.status
            await send_json(client, exc.status, {"error": exc.code})
            return False
        finally:
            await self.record(entry)
        return client.conn.our_state is h11.DONE and client.conn.their_state is h11.DONE

    async def _exchange(
        self,
        client: Peer,
        upstream: Peer,
        request: h11.Request,
        entry: AuditEntry,
        *,
        strip_cookies: bool,
    ) -> bool:
        """Send one request upstream and stream both bodies, never following redirects; return whether a 101 came back."""
        await _from_upstream(upstream.send(request))
        if client.conn.they_are_waiting_for_100_continue:
            await client.send(h11.InformationalResponse(status_code=100, headers=[]))
        while isinstance(event := await client.next_event(), h11.Data):
            entry.bytes_up += len(event.data)
            if entry.bytes_up > self.max_body_bytes:
                raise _RequestFailedError(413, "request_body_too_large")
            await _from_upstream(upstream.send(event))
        await _from_upstream(upstream.send(h11.EndOfMessage()))
        response = await _from_upstream(_read_response(upstream))
        entry.status = response.status_code
        switched = isinstance(response, h11.InformationalResponse)
        headers = _downstream_response_headers(
            list(response.headers),
            strip_cookies=strip_cookies,
            keep_upgrade=switched,
        )
        if switched:
            await client.send(h11.InformationalResponse(status_code=101, reason=response.reason, headers=headers))
            return True
        await client.send(h11.Response(status_code=response.status_code, reason=response.reason, headers=headers))
        while isinstance(event := await _from_upstream(upstream.next_event()), h11.Data):
            entry.bytes_down += len(event.data)
            await client.send(event)
        await client.send(h11.EndOfMessage())
        return False
