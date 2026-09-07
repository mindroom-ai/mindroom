"""SDK authorization provider with one-use, requester-bound durable grants."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, override
from urllib.parse import urlencode, urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from mindroom.mcp_gateway.accounts import account_is_active
from mindroom.mcp_gateway.lifecycle import owned_grants, public_grant, touch_grant
from mindroom.mcp_gateway.store import GatewayOAuthCapacityError, GatewayOAuthStore

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from mindroom.constants import RuntimePaths

_SCOPES = ["mcp:tools"]
_CONSENT_TTL = 600
_CODE_TTL = 300
_ACCESS_TTL = 900


def _budget(runtime_paths: RuntimePaths, name: str, default: int) -> int:
    value = runtime_paths.env_value(name)
    try:
        result = int(value) if value is not None else default
    except ValueError as exc:
        msg = f"{name} must be a positive integer"
        raise ValueError(msg) from exc
    if result <= 0:
        msg = f"{name} must be a positive integer"
        raise ValueError(msg)
    return result


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _secret() -> str:
    return secrets.token_urlsafe(32)


def _consent_callback(callback: str, **params: str | None) -> str:
    """Append OAuth response fields without normalizing the validated callback."""
    _, query_marker, query = callback.partition("?")
    separator = "&" if query_marker else "?"
    if query_marker and not query:
        separator = ""
    return callback + separator + urlencode({key: value for key, value in params.items() if value is not None})


def _valid_url(value: str) -> bool:
    parsed = urlsplit(value)
    if not parsed.hostname or parsed.username or parsed.password or "#" in value or len(value) > 2048:
        return False
    if parsed.scheme == "https":
        return True
    if parsed.scheme != "http":
        return False
    if parsed.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


class _GatewayAuthorizationCode(AuthorizationCode):
    """Code bound to the user, agent and grant approved in the browser."""

    requester_id: str
    authenticated_user_id: str
    agent_name: str
    grant_id: str
    account_id: str | None = None


class GatewayAccessToken(AccessToken):
    """Bearer principal scoped to one personal agent and OAuth grant."""

    requester_id: str
    authenticated_user_id: str
    agent_name: str
    grant_id: str
    account_id: str | None = None


class _GatewayRefreshToken(RefreshToken):
    """Rotating capability that cannot extend its grant's maximum lifetime."""

    requester_id: str
    authenticated_user_id: str
    agent_name: str
    grant_id: str
    account_id: str | None = None
    resource: str


@dataclass(frozen=True)
class _GatewayConsent:
    """Only validated display metadata and the current browser CSRF nonce."""

    client_name: str
    redirect_uri: str
    csrf_token: str


class GatewayOAuthProvider(
    OAuthAuthorizationServerProvider[_GatewayAuthorizationCode, _GatewayRefreshToken, GatewayAccessToken],
):
    """Implement the MCP SDK authorization-server provider protocol."""

    def __init__(
        self,
        runtime_paths: RuntimePaths,
        *,
        public_url: str,
        clock: Callable[[], float] | None = None,
    ) -> None:
        origin = public_url.rstrip("/")
        parsed = urlsplit(origin)
        if not _valid_url(origin) or parsed.path or parsed.query:
            msg = "MCP public URL must be an HTTPS origin or loopback HTTP origin"
            raise ValueError(msg)
        self.resource_url = origin + "/mcp"
        self.issuer_url = origin + "/mcp/oauth"
        self.consent_url = origin + "/connections/mcp/authorize"
        self._clock = clock or (lambda: time.time())
        self.scim_token = runtime_paths.env_value("MINDROOM_MCP_SCIM_TOKEN") or None
        if self.scim_token is not None and len(self.scim_token) < 32:
            msg = "MINDROOM_MCP_SCIM_TOKEN must contain at least 32 characters"
            raise ValueError(msg)
        self.accounts_required = self.scim_token is not None
        idle_days = _budget(runtime_paths, "MINDROOM_MCP_OAUTH_IDLE_TTL_DAYS", 30)
        grant_days = _budget(runtime_paths, "MINDROOM_MCP_OAUTH_GRANT_TTL_DAYS", 180 if self.accounts_required else 30)
        if idle_days > grant_days or grant_days > 365 or (grant_days > 30 and not self.accounts_required):
            msg = "MCP OAuth lifetimes require idle <= absolute <= 365 days and managed accounts above 30 days"
            raise ValueError(msg)
        self._idle_ttl = idle_days * 86_400
        self._grant_ttl = grant_days * 86_400
        self.store = GatewayOAuthStore(
            runtime_paths.storage_root,
            onboarding_max_bytes=_budget(runtime_paths, "MINDROOM_MCP_GATEWAY_ONBOARDING_MAX_BYTES", 64 * 1024 * 1024),
            max_bytes=_budget(runtime_paths, "MINDROOM_MCP_OAUTH_MAX_BYTES", 256 * 1024 * 1024),
            user_max_bytes=_budget(runtime_paths, "MINDROOM_MCP_OAUTH_USER_MAX_BYTES", 16 * 1024 * 1024),
            clock=self._clock,
        )

    @override
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Collect expired registrations before loading client metadata without network fetches."""

        def read(connection: sqlite3.Connection) -> OAuthClientInformationFull | None:
            row = connection.execute("SELECT metadata FROM clients WHERE client_id = ?", (client_id,)).fetchone()
            return OAuthClientInformationFull.model_validate_json(row["metadata"]) if row else None

        return await self.store.transact(read)

    @override
    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Accept bounded metadata for public authorization-code/refresh clients."""
        client_id = client_info.client_id
        if (
            not client_id
            or len(client_id) > 256
            or client_info.client_secret is not None
            or client_info.token_endpoint_auth_method != "none"  # noqa: S105
            or sorted(client_info.grant_types) != ["authorization_code", "refresh_token"]
            or client_info.response_types != ["code"]
            or client_info.scope not in (None, "mcp:tools")
            or len(client_info.client_name or "") > 256
            or len(client_info.model_dump_json().encode("utf-8")) > 16_384
        ):
            raise RegistrationError("invalid_client_metadata", "Unsupported public client metadata")  # noqa: EM101
        redirects = client_info.redirect_uris
        if not redirects or len(redirects) > 10 or any(not _valid_url(str(uri)) for uri in redirects):
            raise RegistrationError("invalid_redirect_uri", "Invalid callback URL")  # noqa: EM101
        normalized = client_info.model_copy(update={"scope": "mcp:tools"})

        def save(connection: sqlite3.Connection) -> None:
            existing = connection.execute(
                "SELECT metadata FROM clients WHERE client_id = ?",
                (normalized.client_id,),
            ).fetchone()
            if existing and existing["metadata"] != normalized.model_dump_json():
                raise RegistrationError("invalid_client_metadata", "Client is already registered")  # noqa: EM101
            if existing:
                return
            metadata = normalized.model_dump_json()
            connection.execute(
                "INSERT INTO clients (client_id, metadata, expires_at) VALUES (?, ?, ?)",
                (normalized.client_id, metadata, self.store.registration_expires_at()),
            )
            self.store.require_capacity(connection, onboarding=True)

        await self.store.transact(save)

    @override
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Create an opaque, short-lived browser consent request."""
        registered = await self.get_client(client.client_id or "")
        if registered is None:
            raise AuthorizeError("unauthorized_client", "Unknown client")  # noqa: EM101
        if params.resource != self.resource_url:
            raise AuthorizeError("invalid_request", "Exact MCP resource is required")  # noqa: EM101
        if params.scopes != _SCOPES:
            raise AuthorizeError("invalid_scope", "Only mcp:tools is supported")  # noqa: EM101
        if params.redirect_uri not in (registered.redirect_uris or []):
            raise AuthorizeError("invalid_request", "Unregistered callback")  # noqa: EM101
        if len(params.code_challenge) != 43 or len(params.state or "") > 2048:
            raise AuthorizeError("invalid_request", "Invalid authorization parameters")  # noqa: EM101
        state = _secret()
        payload = json.dumps(
            {
                "client_id": registered.client_id,
                "client_name": registered.client_name or "MCP client",
                "params": params.model_dump(mode="json"),
            },
        )

        def save(connection: sqlite3.Connection) -> None:
            if not connection.execute("SELECT 1 FROM clients WHERE client_id = ?", (registered.client_id,)).fetchone():
                raise AuthorizeError("unauthorized_client", "Client registration expired")  # noqa: EM101
            connection.execute(
                "INSERT INTO pending (state_hash, payload, expires_at) VALUES (?, ?, ?)",
                (_digest(state), payload, self._clock() + _CONSENT_TTL),
            )
            self.store.require_capacity(connection, onboarding=True)

        try:
            await self.store.transact(save)
        except GatewayOAuthCapacityError:
            return _consent_callback(str(params.redirect_uri), error="temporarily_unavailable", state=params.state)
        return self.consent_url + "?" + urlencode({"state": state})

    def _pending(self, connection: sqlite3.Connection, state: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM pending WHERE state_hash = ?", (_digest(state),)).fetchone()
        if row is None or row["expires_at"] <= self._clock():
            raise AuthorizeError("invalid_request", "Consent is invalid or expired")  # noqa: EM101
        payload = json.loads(row["payload"])
        if payload["params"]["resource"] != self.resource_url:
            raise AuthorizeError("invalid_request", "Consent resource changed")  # noqa: EM101
        return row

    async def begin_consent(
        self,
        state: str,
        *,
        requester_id: str,
        authenticated_user_id: str,
        agent_name: str,
        account_id: str | None = None,
    ) -> _GatewayConsent:
        """Bind the first authenticated visitor and issue a fresh browser nonce."""

        def bind(connection: sqlite3.Connection) -> _GatewayConsent:
            row = self._pending(connection, state)
            if (
                not self._account_allowed(connection, account_id)
                or not requester_id
                or not authenticated_user_id
                or not agent_name
                or (
                    row["requester_id"] is not None
                    and (
                        row["account_id"] != account_id
                        or row["requester_id"] != requester_id
                        or row["agent_name"] != agent_name
                        or row["authenticated_user_id"] != authenticated_user_id
                    )
                )
            ):
                raise AuthorizeError("access_denied", "Consent belongs to another principal")  # noqa: EM101
            csrf = _secret()
            connection.execute(
                """UPDATE pending SET requester_id = ?, authenticated_user_id = ?, agent_name = ?, csrf_hash = ?, account_id = ?
                   WHERE state_hash = ?""",
                (requester_id, authenticated_user_id, agent_name, _digest(csrf), account_id, _digest(state)),
            )
            self.store.require_capacity(connection, onboarding=True)
            payload = json.loads(row["payload"])
            return _GatewayConsent(payload["client_name"], payload["params"]["redirect_uri"], csrf)

        return await self.store.transact(bind)

    async def finish_consent(
        self,
        state: str,
        *,
        requester_id: str,
        authenticated_user_id: str,
        agent_name: str,
        csrf_token: str,
        allow: bool,
        account_id: str | None = None,
    ) -> str:
        """Consume browser consent once and return its authoritative callback."""

        def finish(connection: sqlite3.Connection) -> str:
            row = self._pending(connection, state)
            if (
                not self._account_allowed(connection, account_id)
                or row["account_id"] != account_id
                or row["requester_id"] != requester_id
                or row["authenticated_user_id"] != authenticated_user_id
                or row["agent_name"] != agent_name
                or not row["csrf_hash"]
                or not secrets.compare_digest(row["csrf_hash"], _digest(csrf_token))
            ):
                raise AuthorizeError("access_denied", "Invalid consent principal or nonce")  # noqa: EM101
            payload = json.loads(row["payload"])
            params = AuthorizationParams.model_validate(payload["params"])
            connection.execute("DELETE FROM pending WHERE state_hash = ?", (_digest(state),))
            if not allow:
                return _consent_callback(str(params.redirect_uri), error="access_denied", state=params.state)
            now = self._clock()
            grant_id = _secret()
            grant: dict[str, Any] = {
                "client_id": payload["client_id"],
                "requester_id": requester_id,
                "authenticated_user_id": authenticated_user_id,
                "agent_name": agent_name,
                "grant_id": grant_id,
                "account_id": account_id,
                "resource": self.resource_url,
                "scopes": _SCOPES,
                "redirect_uri": str(params.redirect_uri),
            }
            connection.execute(
                """INSERT INTO grants (grant_id, payload, expires_at, requester_id, created_at, last_activity_at, idle_expires_at, account_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    grant_id,
                    json.dumps(grant),
                    now + self._grant_ttl,
                    requester_id,
                    now,
                    now,
                    now + self._idle_ttl,
                    account_id,
                ),
            )
            code = _GatewayAuthorizationCode(
                **{key: value for key, value in grant.items() if key != "redirect_uri"},
                code=_secret(),
                expires_at=now + _CODE_TTL,
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            )
            self._save_capability(connection, "code", code.code, code)
            self.store.require_capacity(connection, requester_id=requester_id)
            return _consent_callback(str(params.redirect_uri), code=code.code, state=params.state)

        return await self.store.transact(finish)

    @staticmethod
    def _save_capability(
        connection: sqlite3.Connection,
        kind: str,
        raw: str,
        capability: _GatewayAuthorizationCode | GatewayAccessToken | _GatewayRefreshToken,
        *,
        issued_at: float | None = None,
    ) -> None:
        payload = capability.model_dump(mode="json", exclude={"code", "token"})
        connection.execute(
            "INSERT INTO capabilities (token_hash, kind, grant_id, payload, expires_at, issued_at) VALUES (?, ?, ?, ?, ?, ?)",
            (_digest(raw), kind, capability.grant_id, json.dumps(payload), capability.expires_at, issued_at),
        )

    def _load(
        self,
        connection: sqlite3.Connection,
        kind: str,
        raw: str,
        *,
        include_consumed: bool = False,
    ) -> sqlite3.Row | None:
        row = connection.execute(
            """SELECT c.*, g.payload AS grant_payload, g.expires_at AS grant_expires_at, g.idle_expires_at, g.revoked, g.account_id
               FROM capabilities c JOIN grants g ON g.grant_id = c.grant_id
               WHERE c.token_hash = ? AND c.kind = ?""",
            (_digest(raw), kind),
        ).fetchone()
        if row is None or row["revoked"] or (row["consumed"] and not include_consumed):
            return None
        if min(row["expires_at"], row["grant_expires_at"], row["idle_expires_at"]) <= self._clock():
            return None
        if not self._account_allowed(connection, row["account_id"]):
            return None
        if json.loads(row["grant_payload"])["resource"] != self.resource_url:
            return None
        return row

    def _account_allowed(self, connection: sqlite3.Connection, account_id: str | None) -> bool:
        if account_id is None:
            return not self.accounts_required
        return self.accounts_required and account_is_active(connection, account_id)

    @staticmethod
    def _capability_payload(row: sqlite3.Row) -> dict[str, Any]:
        if row["kind"] == "refresh" and row["payload"] == "{}":
            grant = json.loads(row["grant_payload"])
            return _GatewayRefreshToken(token="", expires_at=row["expires_at"], **grant).model_dump(
                mode="json",
                exclude={"token"},
            )
        payload = json.loads(row["payload"])
        payload.setdefault("account_id", None)
        return payload

    @override
    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> _GatewayAuthorizationCode | None:
        """Look up a code without consuming it before SDK PKCE validation."""

        def read(connection: sqlite3.Connection) -> _GatewayAuthorizationCode | None:
            row = self._load(connection, "code", authorization_code)
            if row is None:
                return None
            code = _GatewayAuthorizationCode(code=authorization_code, **json.loads(row["payload"]))
            return code if code.client_id == client.client_id else None

        return await self.store.read(read)

    @override
    async def load_access_token(self, token: str) -> GatewayAccessToken | None:
        """Resolve only an unexpired, unrevoked principal for this fixed resource."""

        def read(connection: sqlite3.Connection) -> GatewayAccessToken | None:
            row = self._load(connection, "access", token)
            return GatewayAccessToken(token=token, **json.loads(row["payload"])) if row else None

        return await self.store.read(read)

    @override
    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> _GatewayRefreshToken | None:
        """Look up a refresh token; exchange detects reuse and revokes its family."""

        def read(connection: sqlite3.Connection) -> _GatewayRefreshToken | None:
            row = self._load(connection, "refresh", refresh_token, include_consumed=True)
            if row is None:
                return None
            token = _GatewayRefreshToken(token=refresh_token, **self._capability_payload(row))
            return token if token.client_id == client.client_id else None

        return await self.store.read(read)

    def _issue_tokens(self, connection: sqlite3.Connection, row: sqlite3.Row) -> OAuthToken:
        issued_at = self._clock()
        recent = connection.execute(
            "SELECT COUNT(*) FROM capabilities WHERE kind = 'access' AND grant_id = ? AND issued_at > ?",
            (row["grant_id"], issued_at - 60),
        ).fetchone()[0]
        if recent >= 6:
            msg = "MCP OAuth token issuance is temporarily limited"
            raise GatewayOAuthCapacityError(msg)
        now = int(issued_at)
        expires_at = min(now + _ACCESS_TTL, int(row["grant_expires_at"]))
        grant = json.loads(row["grant_payload"])
        access = GatewayAccessToken(**grant, token=_secret(), expires_at=expires_at)
        refresh = _GatewayRefreshToken(**grant, token=_secret(), expires_at=int(row["grant_expires_at"]))
        self._save_capability(connection, "access", access.token, access, issued_at=issued_at)
        self._save_capability(connection, "refresh", refresh.token, refresh)
        self.store.require_capacity(connection, requester_id=grant["requester_id"])
        touch_grant(connection, row["grant_id"], issued_at, self._idle_ttl)
        return OAuthToken(
            access_token=access.token,
            refresh_token=refresh.token,
            expires_in=expires_at - now,
            scope="mcp:tools",
        )

    @override
    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: _GatewayAuthorizationCode,
    ) -> OAuthToken:
        """Consume a validated authorization code and issue its first token pair."""

        def exchange(connection: sqlite3.Connection) -> OAuthToken:
            row = self._load(connection, "code", authorization_code.code)
            if (
                row is None
                or authorization_code.client_id != client.client_id
                or self._capability_payload(row) != authorization_code.model_dump(mode="json", exclude={"code"})
            ):
                raise TokenError("invalid_grant", "Invalid authorization code")  # noqa: EM101
            connection.execute(
                "UPDATE capabilities SET consumed = 1 WHERE token_hash = ?",
                (_digest(authorization_code.code),),
            )
            return self._issue_tokens(connection, row)

        return await self.store.transact(exchange)

    @override
    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: _GatewayRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Rotate once; concurrent reuse revokes the whole grant, including the winner."""
        if scopes != _SCOPES:
            raise TokenError("invalid_scope", "Only mcp:tools is supported")  # noqa: EM101

        def exchange(connection: sqlite3.Connection) -> OAuthToken | None:
            row = self._load(connection, "refresh", refresh_token.token, include_consumed=True)
            if (
                row is None
                or refresh_token.client_id != client.client_id
                or self._capability_payload(row) != refresh_token.model_dump(mode="json", exclude={"token"})
            ):
                raise TokenError("invalid_grant", "Invalid refresh token")  # noqa: EM101
            if row["consumed"]:
                self.store.delete_family(connection, row["grant_id"])
                return None
            connection.execute(
                """UPDATE capabilities SET consumed = 1,
                   payload = CASE WHEN kind = 'refresh' THEN '{}' ELSE payload END
                   WHERE grant_id = ? AND consumed = 0 AND kind IN ('access', 'refresh')""",
                (row["grant_id"],),
            )
            return self._issue_tokens(connection, row)

        result = await self.store.transact(exchange)
        if result is None:
            raise TokenError("invalid_grant", "Refresh token was already used")  # noqa: EM101
        return result

    @override
    async def revoke_token(self, token: GatewayAccessToken | _GatewayRefreshToken) -> None:
        """Revoke the entire family using the stored capability's authoritative grant."""

        def revoke(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                """SELECT c.*, g.payload AS grant_payload FROM capabilities c JOIN grants g USING (grant_id)
                   WHERE token_hash = ? AND kind IN ('access', 'refresh')""",
                (_digest(token.token),),
            ).fetchone()
            if row and self._capability_payload(row) == token.model_dump(mode="json", exclude={"token"}):
                self.store.delete_family(connection, row["grant_id"])

        await self.store.transact(revoke)

    async def list_grants(
        self,
        *,
        requester_id: str,
        authenticated_user_id: str,
        agent_name: str,
        after: str | None = None,
        limit: int = 101,
    ) -> list[dict[str, Any]]:
        """List a bounded page of active connections without recording activity."""
        if not 1 <= limit <= 101:
            msg = "Connection page size must be between 1 and 101"
            raise ValueError(msg)

        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = owned_grants(
                connection,
                requester_id=requester_id,
                authenticated_user_id=authenticated_user_id,
                agent_name=agent_name,
                resource=self.resource_url,
                active_at=self._clock(),
                accounts_required=self.accounts_required,
                after=after,
                limit=limit,
            )
            return [public_grant(connection, row) for row in rows]

        return await self.store.read(read)

    async def record_use(self, token: GatewayAccessToken) -> bool:
        """Revalidate the exact bearer under the writer lock before recording successful use."""

        def record(connection: sqlite3.Connection) -> bool:
            row = self._load(connection, "access", token.token)
            if row is None or self._capability_payload(row) != token.model_dump(mode="json", exclude={"token"}):
                return False
            touch_grant(connection, row["grant_id"], self._clock(), self._idle_ttl, used=True)
            return True

        return await self.store.transact(record)

    async def revoke_grants(
        self,
        *,
        requester_id: str,
        authenticated_user_id: str,
        agent_name: str,
        grant_id: str | None = None,
    ) -> bool:
        """Atomically remove owned grants and, for revoke-all, bound pending approvals."""

        def revoke(connection: sqlite3.Connection) -> bool:
            rows = owned_grants(
                connection,
                requester_id=requester_id,
                authenticated_user_id=authenticated_user_id,
                agent_name=agent_name,
                resource=self.resource_url,
            )
            selected = [row for row in rows if grant_id is None or row["grant_id"] == grant_id]
            for row in selected:
                self.store.delete_family(connection, row["grant_id"])
            if grant_id is None:
                connection.execute(
                    """DELETE FROM pending WHERE requester_id = ? AND authenticated_user_id = ? AND agent_name = ?
                       AND json_extract(payload, '$.params.resource') = ?""",
                    (requester_id, authenticated_user_id, agent_name, self.resource_url),
                )
            return grant_id is None or bool(selected)

        return await self.store.transact(revoke)
