"""Authenticated forward proxy that brokers worker HTTP(S) traffic."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import ipaddress
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

import h11

from mindroom.egress_broker.audit import AuditRecord
from mindroom.egress_broker.dial import DestinationBlockedError, DialPolicy, open_upstream
from mindroom.egress_broker.rules import (
    host_has_rules,
    inject_credentials,
    match_rule,
    strip_request_headers,
    strip_response_headers,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    import ssl

    from mindroom.config.egress_broker import EgressBrokerConfig
    from mindroom.egress_broker.audit import AuditLog
    from mindroom.egress_broker.ca import BrokerCA
    from mindroom.egress_broker.tokens import TokenSigner, WorkerClaims

__all__ = ["EgressBroker", "ManageUrl", "SecretResolver"]

type SecretResolver = Callable[[WorkerClaims, str], str | None]
type ManageUrl = Callable[[WorkerClaims], str | None]

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
class _AuditEntry:
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

    def to_record(self) -> AuditRecord:
        return AuditRecord(
            at=datetime.now(UTC),
            kind=self.kind,
            scope=self.claims.scope_label,
            agent_name=self.claims.agent_name,
            requester_id=self.claims.requester_id,
            method=self.method,
            host=self.host,
            path=self.path,
            service=self.service,
            status=self.status,
            bytes_up=self.bytes_up,
            bytes_down=self.bytes_down,
            duration_ms=int((time.monotonic() - self.started) * 1000),
        )


class _Peer:
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


async def _send_json(
    client: _Peer,
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


async def _close_stream(writer: asyncio.StreamWriter) -> None:
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


async def _read_response(upstream: _Peer) -> h11.Response:
    """Return the final response head, skipping informational responses."""
    while True:
        event = await upstream.next_event()
        if isinstance(event, h11.Response):
            return event
        if not isinstance(event, h11.InformationalResponse):
            msg = "Upstream closed before responding."
            raise ConnectionError(msg)


def _normalize_host(host: str) -> str:
    """Return a lowercase hostname or compressed IPv6 literal, rejecting anything else."""
    host = host.lower()
    if ":" in host:
        if "%" in host:
            msg = "Zone-scoped addresses are not supported."
            raise ValueError(msg)
        return ipaddress.IPv6Address(host).compressed
    if not _HOSTNAME.fullmatch(host):
        msg = "Invalid host."
        raise ValueError(msg)
    return host


def _parse_port(text: str) -> int:
    if not (text.isdigit() and len(text) <= 5 and 0 < int(text) < 65536):
        msg = "Invalid port."
        raise ValueError(msg)
    return int(text)


def _parse_connect_target(target: bytes) -> tuple[str, int]:
    """Split a CONNECT authority such as ``host:443`` or ``[::1]:443``; the port is required."""
    text = target.decode("ascii")
    if text.startswith("["):
        host, separator, port = text[1:].partition("]:")
        if not separator or ":" not in host:
            msg = "Invalid IPv6 CONNECT target."
            raise ValueError(msg)
    else:
        host, separator, port = text.rpartition(":")
        if not separator or ":" in host:
            msg = "CONNECT target needs host:port."
            raise ValueError(msg)
    return _normalize_host(host), _parse_port(port)


def _parse_absolute_http(target: bytes) -> tuple[str, int, bytes, bytes]:
    """Split an absolute-form ``http://`` target into host, port, Host authority, and origin-form target."""
    parts = urlsplit(target.decode("ascii"))
    if parts.scheme.lower() != "http" or not parts.hostname:
        msg = "Only absolute http:// targets are proxied without CONNECT."
        raise ValueError(msg)
    origin = parts.path or "/"
    if parts.query:
        origin = f"{origin}?{parts.query}"
    port = 80 if parts.port is None else parts.port
    if port == 0:
        msg = "Invalid port."
        raise ValueError(msg)
    authority = parts.netloc.rpartition("@")[2]
    return _normalize_host(parts.hostname), port, authority.encode("ascii"), origin.encode("ascii")


def _upstream_request_headers(received: list[tuple[bytes, bytes]], authority: bytes) -> list[tuple[bytes, bytes]]:
    """Return the headers to send upstream: Host set to the routed authority, hop-by-hop removed, framing rebuilt.

    The broker answers ``Expect: 100-continue`` itself. A chunked body is re-chunked, so a client
    Content-Length beside it is dropped rather than forwarded next to the new Transfer-Encoding.
    """
    chunked = any(name == b"transfer-encoding" for name, _ in received)
    dropped = {b"host", b"expect", b"content-length"} if chunked else {b"host", b"expect"}
    headers = [(b"host", authority)]
    headers += [
        (name, value) for name, value in strip_request_headers(received, keep_upgrade=False) if name not in dropped
    ]
    if chunked:
        headers.append((b"transfer-encoding", b"chunked"))
    return headers


def _token_from(headers: list[tuple[bytes, bytes]]) -> str | None:
    """Extract the proxy token from ``Basic b64(token:<anything>)`` or ``Bearer <token>``."""
    value = next((value for name, value in headers if name == b"proxy-authorization"), None)
    if value is None:
        return None
    scheme, _, credential = value.strip().partition(b" ")
    credential = credential.strip()
    if scheme.lower() == b"bearer":
        return credential.decode("latin-1")
    if scheme.lower() != b"basic":
        return None
    try:
        decoded = base64.b64decode(credential, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return None
    return decoded.partition(":")[0]


class EgressBroker:
    """Forward proxy that authenticates workers and injects configured credentials.

    Hosts without rules are tunnelled blind or denied by policy; absolute-form ``http://``
    requests are forwarded with injection when a rule matches.
    """

    def __init__(
        self,
        *,
        ca: BrokerCA,
        signer: TokenSigner,
        config_provider: Callable[[], EgressBrokerConfig],
        resolve_secret: SecretResolver,
        audit: AuditLog,
        dial_policy: DialPolicy = DialPolicy(),  # noqa: B008 - frozen dataclass
        upstream_ssl_context: ssl.SSLContext | None = None,
        manage_url: ManageUrl = lambda _claims: None,
        max_body_bytes: int = 1 << 30,
        idle_timeout: float = 1800.0,
    ) -> None:
        self._ca = ca
        self._signer = signer
        self._config_provider = config_provider
        self._resolve_secret = resolve_secret
        self._audit = audit
        self._dial_policy = dial_policy
        self._upstream_ssl_context = upstream_ssl_context
        self._manage_url = manage_url
        self._max_body_bytes = max_body_bytes
        self._idle_timeout = idle_timeout
        self._server: asyncio.Server | None = None
        self._connections: dict[asyncio.Task[None], asyncio.StreamWriter] = {}

    @property
    def port(self) -> int:
        """Return the bound port, so callers may start on port 0."""
        if self._server is None:
            msg = "Egress broker is not listening."
            raise RuntimeError(msg)
        return self._server.sockets[0].getsockname()[1]

    async def start(self, host: str, port: int) -> None:
        """Start listening; raises the bind error when the address is unavailable."""
        if self._server is not None:
            msg = "Egress broker is already listening."
            raise RuntimeError(msg)
        self._server = await asyncio.start_server(self._accept, host, port)

    async def close(self) -> None:
        """Stop listening and drop every open connection without draining peers."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
        tasks = tuple(self._connections)
        for task in tasks:
            self._connections[task].transport.abort()
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            await server.wait_closed()

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Register synchronously so close also owns clients accepted this tick.
        if self._server is None:
            writer.transport.abort()
            return
        task = asyncio.create_task(self._serve(reader, writer))
        self._connections[task] = writer
        task.add_done_callback(self._connections.pop)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = _Peer(h11.Connection(h11.SERVER), reader, writer, idle_timeout=self._idle_timeout)
        try:
            try:
                while await self._serve_request(client):
                    client.conn.start_next_cycle()
            except h11.RemoteProtocolError as exc:
                await _send_json(client, exc.error_status_hint, {"error": "bad_request"})
        except Exception as exc:
            # One broken client must never reach the listener; exception text can echo header bytes.
            logger.debug("egress_broker_connection_failed", error_type=type(exc).__name__)
        finally:
            await _close_stream(writer)

    async def _serve_request(self, client: _Peer) -> bool:
        """Serve one client request; return whether the connection stays open for another."""
        request = await client.next_event()
        if not isinstance(request, h11.Request):
            return False
        token = _token_from(list(request.headers))
        claims = self._signer.verify(token) if token is not None else None
        if claims is None:
            await _send_json(
                client,
                407,
                {"error": "proxy_authentication_required"},
                headers=(_PROXY_AUTHENTICATE,),
            )
            return False
        if request.method == b"CONNECT":
            await self._connect(client, request, claims)
            return False
        return await self._forward_plain(client, request, claims)

    async def _connect(self, client: _Peer, request: h11.Request, claims: WorkerClaims) -> None:
        try:
            host, port = _parse_connect_target(request.target)
        except ValueError:
            await _send_json(client, 400, {"error": "bad_request"})
            return
        if not isinstance(await client.next_event(), h11.EndOfMessage):
            await _send_json(client, 400, {"error": "bad_request"})
            return
        config = self._config_provider()
        if host_has_rules(config, host, port):
            # Hosts with rules need TLS interception, which this listener does not offer yet.
            await _send_json(client, 501, {"error": "not_implemented"})
            return
        entry = _AuditEntry(claims=claims, kind="tunnel", method="CONNECT", host=host, path="")
        if config.unmatched_hosts == "deny":
            await self._deny(client, entry, {"error": "host_not_allowed", "services": list(config.services)})
            return
        await self._tunnel(client, entry, port)

    async def _open_upstream(
        self,
        client: _Peer,
        entry: _AuditEntry,
        port: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        """Dial the upstream through the guard; on failure answer the client, audit, and return None."""
        try:
            return await _from_upstream(open_upstream(entry.host, port, policy=self._dial_policy, ssl_context=None))
        except DestinationBlockedError:
            await self._deny(client, entry, {"error": "destination_blocked"})
        except _RequestFailedError as exc:
            await self._reject(client, entry, exc.status, {"error": exc.code})
        return None

    async def _deny(self, client: _Peer, entry: _AuditEntry, body: dict[str, object]) -> None:
        entry.kind = "denied"
        await self._reject(client, entry, 403, body)

    async def _reject(self, client: _Peer, entry: _AuditEntry, status: int, body: dict[str, object]) -> None:
        entry.status = status
        await _send_json(client, status, body)
        await self._record(entry)

    async def _record(self, entry: _AuditEntry) -> None:
        try:
            await asyncio.to_thread(self._audit.record, entry.to_record())
        except Exception as exc:
            logger.warning("egress_broker_audit_write_failed", error_type=type(exc).__name__)

    async def _tunnel(self, client: _Peer, entry: _AuditEntry, port: int) -> None:
        """Relay raw bytes to an unmatched host without looking inside them."""
        upstream = await self._open_upstream(client, entry, port)
        if upstream is None:
            return
        upstream_reader, upstream_writer = upstream
        try:
            await client.send(h11.Response(status_code=200, reason=b"Connection Established", headers=[]))
            entry.status = 200
            buffered, _ = client.conn.trailing_data
            if buffered:
                upstream_writer.write(buffered)
                entry.bytes_up += len(buffered)
            await self._splice(client, upstream_reader, upstream_writer, entry)
        finally:
            upstream_writer.transport.abort()
            await self._record(entry)

    async def _splice(
        self,
        client: _Peer,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        entry: _AuditEntry,
    ) -> None:
        """Copy bytes both ways until both sides finish or neither sends for the idle timeout."""
        loop = asyncio.get_running_loop()

        async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, upload: bool) -> None:
            try:
                while data := await reader.read(_CHUNK):
                    idle.reschedule(loop.time() + self._idle_timeout)
                    if upload:
                        entry.bytes_up += len(data)
                    else:
                        entry.bytes_down += len(data)
                    writer.write(data)
                    await writer.drain()
                if writer.can_write_eof():
                    writer.write_eof()
            except OSError:
                writer.transport.abort()

        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(self._idle_timeout) as idle, asyncio.TaskGroup() as group:
                group.create_task(pump(client.reader, upstream_writer, upload=True))
                group.create_task(pump(upstream_reader, client.writer, upload=False))

    async def _forward_plain(self, client: _Peer, request: h11.Request, claims: WorkerClaims) -> bool:
        """Forward one absolute-form ``http://`` request, routed by its URL and never its Host header."""
        try:
            host, port, authority, target = _parse_absolute_http(request.target)
        except ValueError:
            await _send_json(client, 400, {"error": "bad_request"})
            return False
        path = target.split(b"?", 1)[0].decode("ascii")
        entry = _AuditEntry(claims=claims, kind="request", method=request.method.decode("ascii"), host=host, path=path)
        config = self._config_provider()
        if config.unmatched_hosts == "deny" and not host_has_rules(config, host, port):
            await self._deny(client, entry, {"error": "host_not_allowed", "services": list(config.services)})
            return False
        headers = _upstream_request_headers(list(request.headers), authority)
        upstream_request = await self._inject(
            client,
            entry,
            config,
            port,
            h11.Request(method=request.method, target=target, headers=headers),
        )
        if upstream_request is None:
            return False
        return await self._forward(client, entry, port, upstream_request)

    async def _inject(
        self,
        client: _Peer,
        entry: _AuditEntry,
        config: EgressBrokerConfig,
        port: int,
        request: h11.Request,
    ) -> h11.Request | None:
        """Return the upstream request with the matching rule's secret injected.

        A request no rule matches passes unchanged. A matched service without a secret in the
        worker's scope is answered with 403 and returns None.
        """
        match = match_rule(config, entry.host, port, entry.path)
        if match is None:
            return request
        entry.service = match.service
        secret = await asyncio.to_thread(self._resolve_secret, entry.claims, match.service)
        if secret is None:
            await self._deny(
                client,
                entry,
                {
                    "error": "credential_not_configured",
                    "service": match.service,
                    "manage_url": self._manage_url(entry.claims),
                },
            )
            return None
        headers, target = inject_credentials(list(request.headers), request.target, match.rule.auth, secret)
        return h11.Request(method=request.method, target=target, headers=headers)

    async def _forward(self, client: _Peer, entry: _AuditEntry, port: int, request: h11.Request) -> bool:
        """Send one request over a fresh upstream connection; return whether the client connection stays open."""
        declared = next((int(value) for name, value in request.headers if name == b"content-length"), 0)
        if declared > self._max_body_bytes:
            await self._reject(client, entry, 413, {"error": "request_body_too_large"})
            return False
        upstream = await self._open_upstream(client, entry, port)
        if upstream is None:
            return False
        upstream_reader, upstream_writer = upstream
        try:
            await self._exchange(
                client,
                _Peer(h11.Connection(h11.CLIENT), upstream_reader, upstream_writer, idle_timeout=self._idle_timeout),
                request,
                entry,
            )
        except _RequestFailedError as exc:
            # A failure after the response head went out keeps that status; the client just sees the cut.
            entry.status = entry.status or exc.status
            await _send_json(client, exc.status, {"error": exc.code})
            return False
        finally:
            upstream_writer.transport.abort()
            await self._record(entry)
        return client.conn.our_state is h11.DONE and client.conn.their_state is h11.DONE

    async def _exchange(self, client: _Peer, upstream: _Peer, request: h11.Request, entry: _AuditEntry) -> None:
        """Send one request upstream and stream both bodies, never following redirects."""
        await _from_upstream(upstream.send(request))
        if client.conn.they_are_waiting_for_100_continue:
            await client.send(h11.InformationalResponse(status_code=100, headers=[]))
        while isinstance(event := await client.next_event(), h11.Data):
            entry.bytes_up += len(event.data)
            if entry.bytes_up > self._max_body_bytes:
                raise _RequestFailedError(413, "request_body_too_large")
            await _from_upstream(upstream.send(event))
        await _from_upstream(upstream.send(h11.EndOfMessage()))
        response = await _from_upstream(_read_response(upstream))
        entry.status = response.status_code
        await client.send(
            h11.Response(
                status_code=response.status_code,
                reason=response.reason,
                headers=strip_response_headers(list(response.headers), keep_upgrade=False),
            ),
        )
        while isinstance(event := await _from_upstream(upstream.next_event()), h11.Data):
            entry.bytes_down += len(event.data)
            await client.send(event)
        await client.send(h11.EndOfMessage())
