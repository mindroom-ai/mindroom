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
    send_json,
    send_proxy_challenge,
    serve_peer,
    split_authority,
    upstream_request_headers,
)
from mindroom.egress_broker.messages import refusal_body
from mindroom.egress_broker.rules import EgressRules, RuleMatch, inject_credentials
from mindroom.egress_broker.secrets import (
    Secret,
    SecretMissing,
    SecretNeedsReconnect,
    SecretServiceChanged,
    SecretUnavailable,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.egress_broker._relay import Relay
    from mindroom.egress_broker.ca import BrokerCA
    from mindroom.egress_broker.proxy import SecretResolver
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
        injected_headers, target = inject_credentials(headers, request.target, match.auth, secret)
        return h11.Request(method=request.method, target=target, headers=injected_headers)
    except h11.LocalProtocolError:
        # The error message can quote the offending header value, which may be the secret, so it is dropped here.
        return None


def _host_header_name(value: bytes) -> str | None:
    """Return the normalized hostname a Host header names, ignoring an ASCII-digit port; None when malformed."""
    try:
        return split_authority(value.decode("latin-1"))[0]
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
    ) -> None:
        self._relay = relay
        self._signer = signer
        self._ca = ca
        self._upstream_ssl_context = upstream_ssl_context or _verifying_context()
        self._resolve_secret = resolve_secret

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
            await send_json(client, 400, refusal_body("bad_request"))
            return
        await client.send(h11.Response(status_code=200, reason=b"Connection Established", headers=[]))
        try:
            await client.writer.start_tls(context)
        except OSError as exc:
            # Usually a client that does not trust the broker CA; nothing was requested, so nothing is audited.
            logger.debug("egress_broker_client_tls_failed", error_type=type(exc).__name__)
            return
        tunnel = _Tunnel(token=token, claims=claims, host=host, port=port)
        tls_client = Peer.serving(client.reader, client.writer)
        try:
            await serve_peer(tls_client, functools.partial(self._serve_request, tunnel=tunnel))
        finally:
            tunnel.drop_upstream()

    async def _serve_request(self, client: Peer, *, tunnel: _Tunnel) -> bool:  # noqa: C901, PLR0911 - checks in order
        """Serve one request inside the tunnel; return whether the tunnel stays open for another.

        The token is checked again on every request, so a busy tunnel does not outlive the token's expiry.
        A request must also be unable to reach anything but the CONNECT host.
        """
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
        if self._signer.verify(tunnel.token) is None:
            await send_proxy_challenge(client)
            return False
        if not origin_form:
            # Absolute, authority, and asterisk forms could name another destination; only the CONNECT target routes.
            await self._relay.deny(client, entry, refusal_body("bad_request"), status=400)
            return False
        host_header = next((value for name, value in request.headers if name == b"host"), None)
        if host_header is not None and _host_header_name(host_header) != tunnel.host:
            # Forwarding another Host to the same address is domain fronting: it could carry the secret elsewhere.
            await self._relay.deny(client, entry, refusal_body("host_mismatch"))
            return False
        routed = await self._relay.route(client, entry, tunnel.port)
        if routed is None:
            return False
        rules, route = routed
        headers = upstream_request_headers(list(request.headers), host=host_header or tunnel.authority)
        if _is_websocket_upgrade(list(request.headers)):
            if client.close_after_response:
                # Conflicting lengths mean the connection closes after this response, so it never becomes a splice.
                await self._relay.deny(client, entry, refusal_body("bad_request"), status=400)
                return False
            headers += [(b"connection", b"Upgrade"), (b"upgrade", b"websocket")]
        if (match := route.match) is None:
            upstream_request = h11.Request(method=request.method, target=request.target, headers=headers)
        else:
            entry.service = match.service
            if (secret := await self._secret(client, entry, match)) is None:
                return False
            if (upstream_request := _injected_request(request, headers, match, secret)) is None:
                logger.warning("egress_broker_secret_unusable", service=match.service)
                await self._relay.reject(client, entry, 502, refusal_body("broker_error"))
                return False
        return await self._forward(client, entry, tunnel, upstream_request, rules, strip_cookies=route.host_has_rules)

    async def _secret(self, client: Peer, entry: AuditEntry, match: RuleMatch) -> str | None:
        """Return the worker scope's secret for the matched service; otherwise answer the client, audit, and return None.

        The resolver also gets the definition the match came from, and resolves nothing once the service has changed.
        """
        try:
            result = await self._resolve_secret(entry.claims, match.service, match.definition)
            if isinstance(result, Secret) and result.value:
                return result.value
            status, body = self._refusal(entry.claims, match.service, result)
        except Exception as exc:
            # The resolver runs store and OAuth provider code, and `_refusal` the scope callback.
            logger.warning("egress_broker_secret_lookup_failed", error_type=type(exc).__name__)
            await self._relay.reject(client, entry, 502, refusal_body("broker_error"))
            return None
        if status == 403:
            await self._relay.deny(client, entry, body)
        else:
            await self._relay.reject(client, entry, status, body)
        return None

    def _refusal(self, claims: WorkerClaims, service: str, result: SecretResult) -> tuple[int, dict[str, object]]:
        """Return the status and body for a lookup without a secret: retry, retry later, reconnect, or set one.

        Each body but the `broker_error` for a changed service carries `manage_url`, which its message names, unless
        no page sets the scope's keys (a personal scope without the personal page), and a connect link only in
        `connect_url`. An account nobody allowed Agent Keys to use gets `oauth_reuse: "off"` and no connect link.
        """
        if isinstance(result, SecretServiceChanged):
            # The service changed after routing, so its credential may be for other hosts; a retry routes afresh.
            logger.info("egress_broker_service_changed", service=service)
            return 502, refusal_body("broker_error")
        manage_url = self._relay.manage_url
        scope = self._relay.scope_info(claims)
        if isinstance(result, SecretUnavailable):
            # The provider may recover, so this is a retryable failure, not a prompt to reconnect.
            return 503, refusal_body(
                "oauth_refresh_failed",
                service=service,
                provider=result.provider,
                shared=scope.shared,
                personal_page=scope.personal_page,
                manage_url=manage_url,
            )
        if isinstance(result, SecretNeedsReconnect):
            reconnect = refusal_body(
                "oauth_connection_required",
                service=service,
                provider=result.provider,
                shared=scope.shared,
                personal_page=scope.personal_page,
                connectable=result.connect_url is not None,
                connect_url=result.connect_url,
                manage_url=manage_url,
            )
            if result.reset_required:
                reconnect["reset_required"] = True
            return 403, reconnect
        # No secret, or an empty one: the body also names the account the user can connect or allow instead, if any.
        lookup = result if isinstance(result, SecretMissing) else SecretMissing()
        missing = refusal_body(
            "credential_not_configured",
            service=service,
            provider=lookup.provider,
            managers=scope.managers,
            shared=scope.shared,
            personal_page=scope.personal_page,
            connectable=lookup.provider is not None and lookup.connect_url is not None,
            reuse_off=lookup.reuse_off,
            requester_owned=lookup.requester_owned,
            manage_url=manage_url,
        )
        if lookup.provider is not None and not lookup.reuse_off:
            missing["connect_url"] = lookup.connect_url
        return 403, missing

    async def _forward(
        self,
        client: Peer,
        entry: AuditEntry,
        tunnel: _Tunnel,
        request: h11.Request,
        rules: EgressRules,
        *,
        strip_cookies: bool,
    ) -> bool:
        """Send one request over the tunnel's upstream connection; return whether the tunnel stays open."""
        if await self._relay.refuse_large_body(client, entry, request):
            return False
        if tunnel.upstream is not None and await tunnel.upstream.sent_unprompted():
            # The upstream closed its idle connection, or sent bytes after its last response that this request's
            # response must not be read from.
            tunnel.drop_upstream()
        if tunnel.upstream is None:
            tunnel.upstream = await self._relay.open_upstream(
                client,
                entry,
                tunnel.port,
                rules,
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
