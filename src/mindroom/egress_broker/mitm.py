"""TLS interception for CONNECT targets with rules: credentials are injected per request, never seen by the worker."""

from __future__ import annotations

import asyncio
import functools
import ssl
from dataclasses import dataclass
from typing import TYPE_CHECKING

import certifi
import h11

from mindroom.egress_broker._relay import (
    AuditEntry,
    Peer,
    normalize_host,
    send_json,
    serve_peer,
    upstream_request_headers,
)
from mindroom.egress_broker.rules import host_has_rules, inject_credentials, match_rule
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.egress_broker._relay import Relay
    from mindroom.egress_broker.ca import BrokerCA
    from mindroom.egress_broker.proxy import ManageUrl, SecretResolver
    from mindroom.egress_broker.tokens import WorkerClaims

__all__ = ["TlsInterceptor"]

logger = get_logger(__name__)


def _verifying_context() -> ssl.SSLContext:
    """Trust the system store plus certifi's roots, so upstream verification works where the system store is empty."""
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    context.set_alpn_protocols(["http/1.1"])
    return context


def _host_header_name(value: bytes) -> str | None:
    """Return the normalized hostname a Host header names, ignoring a numeric port; None when malformed."""
    text = value.decode("latin-1")
    if text.startswith("["):
        name, bracket, port = text[1:].partition("]")
        if not bracket or ":" not in name or (port and not port.startswith(":")):
            return None
        port = port.removeprefix(":")
    else:
        name, _, port = text.partition(":")
    if port and not (port.isdigit() and len(port) <= 5):
        return None
    try:
        return normalize_host(name)
    except ValueError:
        return None


@dataclass
class _Tunnel:
    """One intercepted CONNECT: the verified identity, the target, and the upstream connection it reuses."""

    claims: WorkerClaims
    host: str
    port: int
    upstream: Peer | None = None

    @property
    def authority(self) -> bytes:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return (host if self.port == 443 else f"{host}:{self.port}").encode("ascii")

    def drop_upstream(self) -> None:
        if self.upstream is not None:
            self.upstream.writer.transport.abort()
            self.upstream = None


class TlsInterceptor:
    """Terminates worker TLS for hosts with rules and relays each request with the scope's secret injected.

    Routing uses the CONNECT target only: a request whose Host names another host or whose target is not
    origin-form is refused, so a secret can never be fronted to a different origin behind the same address.
    """

    def __init__(
        self,
        relay: Relay,
        *,
        ca: BrokerCA,
        upstream_ssl_context: ssl.SSLContext | None,
        resolve_secret: SecretResolver,
        manage_url: ManageUrl,
    ) -> None:
        self._relay = relay
        self._ca = ca
        self._upstream_ssl_context = upstream_ssl_context or _verifying_context()
        self._resolve_secret = resolve_secret
        self._manage_url = manage_url

    async def intercept(self, client: Peer, claims: WorkerClaims, host: str, port: int) -> None:
        """Accept the CONNECT, complete TLS with a leaf for `host`, and serve requests until the tunnel closes."""
        # Prepare the leaf before answering: once the 200 is out the client starts its handshake.
        context = await asyncio.to_thread(self._ca.server_context, host)
        if client.conn.trailing_data[0]:
            # Bytes sent before the 200 cannot be handed to the TLS handshake.
            await send_json(client, 400, {"error": "bad_request"})
            return
        await client.send(h11.Response(status_code=200, reason=b"Connection Established", headers=[]))
        try:
            await client.writer.start_tls(context)
        except OSError as exc:
            # Usually a client that does not trust the broker CA; nothing was requested, so nothing is audited.
            logger.debug("egress_broker_client_tls_failed", error_type=type(exc).__name__)
            return
        tunnel = _Tunnel(claims=claims, host=host, port=port)
        tls_client = Peer(
            h11.Connection(h11.SERVER),
            client.reader,
            client.writer,
            idle_timeout=self._relay.idle_timeout,
        )
        try:
            await serve_peer(tls_client, functools.partial(self._serve_request, tunnel=tunnel))
        finally:
            tunnel.drop_upstream()

    async def _serve_request(self, client: Peer, *, tunnel: _Tunnel) -> bool:
        """Serve one request inside the tunnel; return whether the tunnel stays open for another."""
        request = await client.next_event()
        if not isinstance(request, h11.Request):
            return False
        origin_form = request.target.startswith(b"/")
        entry = AuditEntry(
            claims=tunnel.claims,
            kind="request",
            method=request.method.decode("ascii"),
            host=tunnel.host,
            path=request.target.split(b"?", 1)[0].decode("ascii") if origin_form else "",
        )
        host_header = next((value for name, value in request.headers if name == b"host"), None)
        if not await self._addresses_tunnel(client, entry, tunnel, origin_form=origin_form, host_header=host_header):
            return False
        config = await self._relay.read_config(client, entry)
        if config is None:
            return False
        has_rules = host_has_rules(config, tunnel.host, tunnel.port)
        if not has_rules and config.unmatched_hosts == "deny":
            await self._relay.deny(client, entry, {"error": "host_not_allowed", "services": list(config.services)})
            return False
        headers = upstream_request_headers(
            list(request.headers),
            host=host_header or tunnel.authority,
            keep_upgrade=any(name == b"upgrade" for name, _ in request.headers),
        )
        target = request.target
        if (match := match_rule(config, tunnel.host, tunnel.port, entry.path)) is not None:
            entry.service = match.service
            secret = await self._secret(client, entry, match.service)
            if secret is None:
                return False
            headers, target = inject_credentials(headers, target, match.rule.auth, secret)
        upstream_request = h11.Request(method=request.method, target=target, headers=headers)
        return await self._forward(client, entry, tunnel, upstream_request, strip_cookies=has_rules)

    async def _addresses_tunnel(
        self,
        client: Peer,
        entry: AuditEntry,
        tunnel: _Tunnel,
        *,
        origin_form: bool,
        host_header: bytes | None,
    ) -> bool:
        """Return whether a request can only reach the CONNECT host; otherwise answer the client, audit, and return False."""
        if not origin_form:
            # Absolute, authority, and asterisk forms could name another destination; only the CONNECT target routes.
            await self._relay.deny(client, entry, {"error": "bad_request"}, status=400)
            return False
        if host_header is not None and _host_header_name(host_header) != tunnel.host:
            # Forwarding another Host to the same address is domain fronting: it could carry the secret elsewhere.
            await self._relay.deny(client, entry, {"error": "host_mismatch"})
            return False
        return True

    async def _secret(self, client: Peer, entry: AuditEntry, service: str) -> str | None:
        """Return the worker scope's secret for `service`; otherwise answer the client, audit, and return None."""
        try:
            secret = await asyncio.to_thread(self._resolve_secret, entry.claims, service)
            manage_url = None if secret else self._manage_url(entry.claims)
        except Exception as exc:
            logger.warning("egress_broker_secret_lookup_failed", error_type=type(exc).__name__)
            await self._relay.reject(client, entry, 502, {"error": "broker_error"})
            return None
        if not secret:
            body: dict[str, object] = {
                "error": "credential_not_configured",
                "service": service,
                "manage_url": manage_url,
            }
            await self._relay.deny(client, entry, body)
            return None
        return secret

    async def _forward(
        self,
        client: Peer,
        entry: AuditEntry,
        tunnel: _Tunnel,
        request: h11.Request,
        *,
        strip_cookies: bool,
    ) -> bool:
        """Send one request over the tunnel's upstream connection; return whether the tunnel stays open."""
        if await self._relay.refuse_large_body(client, entry, request):
            return False
        if tunnel.upstream is not None and tunnel.upstream.reader.at_eof():
            # The upstream closed its idle connection since the last response.
            tunnel.drop_upstream()
        if tunnel.upstream is None:
            tunnel.upstream = await self._relay.open_upstream(
                client,
                entry,
                tunnel.port,
                ssl_context=self._upstream_ssl_context,
            )
            if tunnel.upstream is None:
                return False
        upstream = tunnel.upstream
        keep_open = await self._relay.forward(client, upstream, request, entry, strip_cookies=strip_cookies)
        if upstream.conn.our_state is h11.DONE and upstream.conn.their_state is h11.DONE:
            upstream.conn.start_next_cycle()
        else:
            # Connection: close from the upstream, a failed exchange, or a switched protocol.
            tunnel.drop_upstream()
        return keep_open
