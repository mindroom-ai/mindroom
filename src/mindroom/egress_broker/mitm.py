"""TLS interception for CONNECT targets with rules: credentials are injected per request, never seen by the worker."""

from __future__ import annotations

import asyncio
import functools
import os
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
    send_proxy_challenge,
    serve_peer,
    upstream_request_headers,
)
from mindroom.egress_broker.rules import RuleMatch, host_has_rules, inject_credentials, match_rule
from mindroom.egress_broker.secrets import Secret, SecretMissing, SecretNeedsReconnect
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.config.egress_broker import EgressBrokerConfig
    from mindroom.egress_broker._relay import Relay
    from mindroom.egress_broker.ca import BrokerCA
    from mindroom.egress_broker.proxy import ManageUrl, SecretResolver
    from mindroom.egress_broker.secrets import SecretResult
    from mindroom.egress_broker.tokens import TokenSigner, WorkerClaims

__all__ = ["TlsInterceptor"]

logger = get_logger(__name__)


def _verifying_context() -> ssl.SSLContext:
    """Trust the system store plus certifi's roots, so upstream verification works where the system store is empty.

    An explicit SSL_CERT_FILE or SSL_CERT_DIR is the operator's trust choice and is left to OpenSSL unchanged,
    as for Matrix connections; OpenSSL reads both from the process environment.
    """
    context = ssl.create_default_context()
    if not any(name in os.environ for name in ("SSL_CERT_FILE", "SSL_CERT_DIR")):
        context.load_verify_locations(cafile=certifi.where())
    context.set_alpn_protocols(["http/1.1"])
    return context


def _is_websocket_upgrade(headers: list[tuple[bytes, bytes]]) -> bool:
    """Return whether the request asks to switch to websocket, the only upgrade the broker splices."""
    offered = [token.strip().lower() for name, value in headers if name == b"upgrade" for token in value.split(b",")]
    return offered == [b"websocket"]


def _injected_request(
    request: h11.Request,
    headers: list[tuple[bytes, bytes]],
    match: RuleMatch,
    secret: str,
) -> h11.Request | None:
    """Build the upstream request with the secret injected; None when h11 refuses it, such as a secret with CR/LF."""
    try:
        injected_headers, target = inject_credentials(headers, request.target, match.rule.auth, secret)
        return h11.Request(method=request.method, target=target, headers=injected_headers)
    except h11.LocalProtocolError:
        # The error message can quote the offending header value, which may be the secret, so it is dropped here.
        return None


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

    token: str
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
        signer: TokenSigner,
        ca: BrokerCA,
        upstream_ssl_context: ssl.SSLContext | None,
        resolve_secret: SecretResolver,
        manage_url: ManageUrl,
    ) -> None:
        self._relay = relay
        self._signer = signer
        self._ca = ca
        self._upstream_ssl_context = upstream_ssl_context or _verifying_context()
        self._resolve_secret = resolve_secret
        self._manage_url = manage_url

    async def intercept(self, client: Peer, token: str, claims: WorkerClaims, host: str, port: int) -> None:
        """Accept the CONNECT, complete TLS with a leaf for `host`, and serve requests until the tunnel closes."""
        # Leave anything the client sends next in the socket, where start_tls (which resumes reading) hands it
        # to the TLS layer; read now, it would sit in the stream buffer that the handshake never sees.
        transport = client.writer.transport
        assert isinstance(transport, asyncio.ReadTransport)  # a socket transport from start_server
        transport.pause_reading()
        # Prepare the leaf before answering: once the 200 is out the client starts its handshake.
        context = await asyncio.to_thread(self._ca.server_context, host)
        if client.conn.trailing_data[0]:
            # Bytes h11 already read past the CONNECT head cannot be handed to the TLS handshake.
            await send_json(client, 400, {"error": "bad_request"})
            return
        await client.send(h11.Response(status_code=200, reason=b"Connection Established", headers=[]))
        try:
            await client.writer.start_tls(context)
        except OSError as exc:
            # Usually a client that does not trust the broker CA; nothing was requested, so nothing is audited.
            logger.debug("egress_broker_client_tls_failed", error_type=type(exc).__name__)
            return
        tunnel = _Tunnel(token=token, claims=claims, host=host, port=port)
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
        if not await self._admit(client, entry, tunnel, origin_form=origin_form, host_header=host_header):
            return False
        config = await self._relay.read_config(client, entry)
        if config is None:
            return False
        has_rules = host_has_rules(config, tunnel.host, tunnel.port)
        upstream_request = await self._upstream_request(
            client,
            entry,
            tunnel,
            request,
            config,
            has_rules=has_rules,
            host_header=host_header,
        )
        if upstream_request is None:
            return False
        return await self._forward(client, entry, tunnel, upstream_request, strip_cookies=has_rules)

    async def _admit(
        self,
        client: Peer,
        entry: AuditEntry,
        tunnel: _Tunnel,
        *,
        origin_form: bool,
        host_header: bytes | None,
    ) -> bool:
        """Return whether the request may proceed; otherwise answer the client, audit a refusal, and return False.

        The token is checked again on every request, so a busy tunnel does not outlive the token's expiry.
        A request must also be unable to reach anything but the CONNECT host.
        """
        if self._signer.verify(tunnel.token) is None:
            await send_proxy_challenge(client)
            return False
        if not origin_form:
            # Absolute, authority, and asterisk forms could name another destination; only the CONNECT target routes.
            await self._relay.deny(client, entry, {"error": "bad_request"}, status=400)
            return False
        if host_header is not None and _host_header_name(host_header) != tunnel.host:
            # Forwarding another Host to the same address is domain fronting: it could carry the secret elsewhere.
            await self._relay.deny(client, entry, {"error": "host_mismatch"})
            return False
        return True

    async def _upstream_request(
        self,
        client: Peer,
        entry: AuditEntry,
        tunnel: _Tunnel,
        request: h11.Request,
        config: EgressBrokerConfig,
        *,
        has_rules: bool,
        host_header: bytes | None,
    ) -> h11.Request | None:
        """Return the request to send upstream, injected when a rule matches; otherwise answer, audit, and return None."""
        if not has_rules and config.unmatched_hosts == "deny":
            await self._relay.deny(client, entry, {"error": "host_not_allowed", "services": list(config.services)})
            return None
        headers = upstream_request_headers(list(request.headers), host=host_header or tunnel.authority)
        if _is_websocket_upgrade(list(request.headers)):
            headers += [(b"connection", b"Upgrade"), (b"upgrade", b"websocket")]
        match = match_rule(config, tunnel.host, tunnel.port, entry.path)
        if match is None:
            return h11.Request(method=request.method, target=request.target, headers=headers)
        entry.service = match.service
        secret = await self._secret(client, entry, match.service)
        if secret is None:
            return None
        injected = _injected_request(request, headers, match, secret)
        if injected is None:
            logger.warning("egress_broker_secret_unusable", service=match.service)
            await self._relay.reject(client, entry, 502, {"error": "broker_error"})
        return injected

    async def _secret(self, client: Peer, entry: AuditEntry, service: str) -> str | None:
        """Return the worker scope's secret for `service`; otherwise answer the client, audit, and return None."""
        try:
            result = await asyncio.to_thread(self._resolve_secret, entry.claims, service)
            if isinstance(result, Secret) and result.value:
                return result.value
            body = self._refusal(entry.claims, service, result)
        except Exception as exc:
            logger.warning("egress_broker_secret_lookup_failed", error_type=type(exc).__name__)
            await self._relay.reject(client, entry, 502, {"error": "broker_error"})
            return None
        await self._relay.deny(client, entry, body)
        return None

    def _refusal(self, claims: WorkerClaims, service: str, result: SecretResult) -> dict[str, object]:
        """Return the 403 body for a lookup without a secret: where to reconnect, or where to set one."""
        if isinstance(result, SecretNeedsReconnect):
            reconnect: dict[str, object] = {
                "error": "oauth_connection_required",
                "service": service,
                "provider": result.provider,
                "connect_url": result.connect_url,
            }
            if result.reset_required:
                reconnect["reset_required"] = True
            return reconnect
        missing: dict[str, object] = {
            "error": "credential_not_configured",
            "service": service,
            "manage_url": self._manage_url(claims),
        }
        if isinstance(result, SecretMissing) and result.provider is not None:
            missing["provider"] = result.provider
            missing["connect_url"] = result.connect_url
        return missing

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
