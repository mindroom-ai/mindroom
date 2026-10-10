"""Authenticated forward proxy that brokers worker HTTP(S) traffic."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import h11

from mindroom.egress_broker._relay import (
    AuditEntry,
    Peer,
    Relay,
    close_stream,
    normalize_host,
    send_json,
    send_proxy_challenge,
    serve_peer,
    upstream_request_headers,
)
from mindroom.egress_broker.dial import DialPolicy
from mindroom.egress_broker.mitm import TlsInterceptor
from mindroom.egress_broker.rules import host_not_allowed, route_request

if TYPE_CHECKING:
    import ssl

    from mindroom.egress_broker.audit import AuditLog
    from mindroom.egress_broker.ca import BrokerCA
    from mindroom.egress_broker.rules import EgressRules
    from mindroom.egress_broker.secrets import SecretResult
    from mindroom.egress_broker.tokens import TokenSigner, WorkerClaims

__all__ = ["EgressBroker", "ManageUrl", "RulesProvider", "SecretResolver"]

# Called in a thread with the verified requester's claims; returns the rules that requester's traffic matches.
type RulesProvider = Callable[[WorkerClaims], EgressRules]
# Awaited on the broker's loop: the resolver chooses which executor each blocking lookup runs on.
type SecretResolver = Callable[[WorkerClaims, str], Awaitable[SecretResult]]
type ManageUrl = Callable[[WorkerClaims], str | None]


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
    return normalize_host(host), _parse_port(port)


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
    return normalize_host(parts.hostname), port, authority.encode("ascii"), origin.encode("ascii")


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

    CONNECT to a host with rules is intercepted and each request inside gets the worker scope's secret.
    Other hosts are tunnelled blind or denied by policy. Absolute-form ``http://`` requests are forwarded,
    except that a request a rule matches is refused: secrets only travel over TLS. Rules come from
    `rules_provider` for each requester's verified claims, read again for every CONNECT and request.
    """

    def __init__(
        self,
        *,
        ca: BrokerCA,
        signer: TokenSigner,
        rules_provider: RulesProvider,
        resolve_secret: SecretResolver,
        audit: AuditLog,
        dial_policy: DialPolicy = DialPolicy(),  # noqa: B008 - frozen dataclass
        upstream_ssl_context: ssl.SSLContext | None = None,
        manage_url: ManageUrl = lambda _claims: None,
        max_body_bytes: int = 1 << 30,
        idle_timeout: float = 1800.0,
        head_timeout: float = 30.0,
    ) -> None:
        self._signer = signer
        self._head_timeout = head_timeout
        self._relay = Relay(
            audit=audit,
            rules_provider=rules_provider,
            dial_policy=dial_policy,
            max_body_bytes=max_body_bytes,
            idle_timeout=idle_timeout,
        )
        self._interceptor = TlsInterceptor(
            self._relay,
            signer=signer,
            ca=ca,
            upstream_ssl_context=upstream_ssl_context,
            resolve_secret=resolve_secret,
            manage_url=manage_url,
        )
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
        client = Peer(h11.Connection(h11.SERVER), reader, writer, idle_timeout=self._relay.idle_timeout)
        try:
            await serve_peer(client, self._serve_request)
        finally:
            await close_stream(writer)

    async def _serve_request(self, client: Peer) -> bool:
        """Serve one client request; return whether the connection stays open for another."""
        # Unauthenticated clients may not hold a connection open by trickling a request head.
        async with asyncio.timeout(self._head_timeout):
            request = await client.next_event()
        if not isinstance(request, h11.Request):
            return False
        token = _token_from(list(request.headers))
        claims = self._signer.verify(token) if token is not None else None
        if token is None or claims is None:
            await send_proxy_challenge(client)
            return False
        if request.method == b"CONNECT":
            await self._connect(client, request, token, claims)
            return False
        return await self._forward_plain(client, request, claims)

    async def _connect(self, client: Peer, request: h11.Request, token: str, claims: WorkerClaims) -> None:
        try:
            host, port = _parse_connect_target(request.target)
        except ValueError:
            await send_json(client, 400, {"error": "bad_request"})
            return
        if not isinstance(await client.next_event(), h11.EndOfMessage):
            await send_json(client, 400, {"error": "bad_request"})
            return
        entry = AuditEntry(claims=claims, kind="tunnel", method="CONNECT", host=host, path="")
        rules = await self._relay.read_rules(client, entry)
        if rules is None:
            return
        if rules.intercepts(host, port):
            await self._interceptor.intercept(client, token, claims, host, port)
            return
        if rules.unmatched_hosts == "deny":
            await self._relay.deny(client, entry, host_not_allowed(rules))
            return
        await self._tunnel(client, entry, port)

    async def _tunnel(self, client: Peer, entry: AuditEntry, port: int) -> None:
        """Relay raw bytes to an unmatched host without looking inside them."""
        upstream = await self._relay.open_upstream(client, entry, port, ssl_context=None)
        if upstream is None:
            return
        try:
            await client.send(h11.Response(status_code=200, reason=b"Connection Established", headers=[]))
            entry.status = 200
            await self._relay.splice(client, upstream, entry)
        finally:
            upstream.writer.transport.abort()
            await self._relay.record(entry)

    async def _forward_plain(self, client: Peer, request: h11.Request, claims: WorkerClaims) -> bool:
        """Forward one absolute-form ``http://`` request, routed by its URL and never its Host header.

        Requests a rule matches are refused with ``tls_required`` instead: the broker never puts a
        secret on an unencrypted connection. Paths the route decision refuses (ambiguous, or unlisted on a
        restricted host) are refused as in a tunnel. Other requests to hosts with rules pass unmodified.
        """
        try:
            host, port, authority, target = _parse_absolute_http(request.target)
        except ValueError:
            await send_json(client, 400, {"error": "bad_request"})
            return False
        path = target.split(b"?", 1)[0].decode("ascii")
        entry = AuditEntry(claims=claims, kind="request", method=request.method.decode("ascii"), host=host, path=path)
        rules = await self._relay.read_rules(client, entry)
        if rules is None:
            return False
        route = route_request(rules, host, port, path)
        if rules.unmatched_hosts == "deny" and not route.host_has_rules:
            await self._relay.deny(client, entry, host_not_allowed(rules))
            return False
        if route.refusal is not None:
            await self._relay.deny(client, entry, {"error": route.refusal}, status=route.refusal_status)
            return False
        if route.match is not None:
            entry.service = route.match.service
            await self._relay.deny(client, entry, {"error": "tls_required", "service": route.match.service})
            return False
        headers = upstream_request_headers(list(request.headers), host=authority)
        upstream_request = h11.Request(method=request.method, target=target, headers=headers)
        return await self._forward(client, entry, port, upstream_request, strip_cookies=route.host_has_rules)

    async def _forward(
        self,
        client: Peer,
        entry: AuditEntry,
        port: int,
        request: h11.Request,
        *,
        strip_cookies: bool,
    ) -> bool:
        """Send one request over a fresh upstream connection; return whether the client connection stays open."""
        if await self._relay.refuse_large_body(client, entry, request):
            return False
        upstream = await self._relay.open_upstream(client, entry, port, ssl_context=None)
        if upstream is None:
            return False
        try:
            return await self._relay.forward(client, upstream, request, entry, strip_cookies=strip_cookies)
        finally:
            upstream.writer.transport.abort()
