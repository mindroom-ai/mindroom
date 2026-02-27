#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "fastapi>=0.116.1",
#   "httpx>=0.27",
#   "uvicorn>=0.35",
# ]
# ///
"""Standalone local MindRoom provisioning service.

This service is designed for hosted Matrix + chat deployments where users run
MindRoom locally. Browser users authenticate with their Matrix access token.
Paired local MindRoom installs receive client credentials that can request
registration tokens for agent account creation.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


PAIR_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
DEFAULT_PAIR_CODE_TTL_SECONDS = 10 * 60
DEFAULT_REGISTRATION_TOKEN_TTL_SECONDS = 5 * 60
DEFAULT_PAIR_POLL_INTERVAL_SECONDS = 3
DEFAULT_STATE_PATH = "/var/lib/mindroom-local-provisioning/state.json"
DEFAULT_CORS_ORIGINS = "https://chat.mindroom.chat"
DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 8776


@dataclass(slots=True)
class ServiceConfig:
    """Runtime configuration for the provisioning service."""

    matrix_homeserver: str
    matrix_ssl_verify: bool
    matrix_registration_token: str
    state_path: Path
    pair_code_ttl_seconds: int
    registration_token_ttl_seconds: int
    pair_poll_interval_seconds: int
    cors_origins: list[str]
    listen_host: str
    listen_port: int


@dataclass(slots=True)
class PairSession:
    """Pair code lifecycle state."""

    id: str
    user_id: str
    pair_code_hash: str
    status: Literal["pending", "connected", "expired"]
    created_at: datetime
    expires_at: datetime
    completed_at: datetime | None = None
    connection_id: str | None = None


@dataclass(slots=True)
class LocalConnection:
    """A linked local MindRoom installation."""

    id: str
    user_id: str
    client_name: str
    fingerprint: str
    client_secret_hash: str
    created_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None = None


@dataclass(slots=True)
class IssuedCredential:
    """A registration token issuance event."""

    id: str
    connection_id: str
    purpose: Literal["register_agent"]
    token_hash: str
    agent_hint: str | None
    uses_remaining: int
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


class PairStartResponse(BaseModel):
    """Response for starting pairing."""

    pair_code: str
    expires_at: datetime
    poll_interval_seconds: int


class LocalConnectionOut(BaseModel):
    """Public shape for linked local installations."""

    id: str
    client_name: str
    fingerprint: str
    created_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None = None


class PairStatusResponse(BaseModel):
    """Response for pair status polling."""

    status: Literal["pending", "connected", "expired"]
    expires_at: datetime | None = None
    connection: LocalConnectionOut | None = None


class PairCompleteRequest(BaseModel):
    """Request payload for local pairing completion."""

    pair_code: str = Field(min_length=9, max_length=9)
    client_name: str = Field(min_length=1, max_length=120)
    client_pubkey_or_fingerprint: str = Field(min_length=1, max_length=512)


class PairCompleteResponse(BaseModel):
    """Response payload for completed pairing."""

    connection: LocalConnectionOut
    client_id: str
    client_secret: str


class ConnectionsResponse(BaseModel):
    """List of user-owned local connections."""

    connections: list[LocalConnectionOut]


class RevokeConnectionResponse(BaseModel):
    """Response after revoking a local connection."""

    revoked: bool
    connection_id: str


class IssueTokenRequest(BaseModel):
    """Request payload for registration token issuance."""

    purpose: Literal["register_agent"] = "register_agent"
    agent_hint: str | None = Field(default=None, max_length=120)


class IssueTokenResponse(BaseModel):
    """Issued registration token payload."""

    credential_id: str
    registration_token: str
    expires_at: datetime
    uses_remaining: int


_state_lock = asyncio.Lock()
_pair_sessions: dict[str, PairSession] = {}
_pair_session_by_hash: dict[str, str] = {}
_connections: dict[str, LocalConnection] = {}
_issued_credentials: dict[str, IssuedCredential] = {}
_rate_limit_buckets: dict[str, list[float]] = {}


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat()


def _from_utc_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no"}


def _env_int(name: str, *, default: int, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = int(raw)
    if value < minimum:
        msg = f"{name} must be >= {minimum}, got {value}"
        raise ValueError(msg)
    return value


def _read_secret(*, env_name: str, file_env_name: str) -> str | None:
    direct = os.getenv(env_name, "").strip()
    if direct:
        return direct

    file_path = os.getenv(file_env_name, "").strip()
    if not file_path:
        return None

    value = Path(file_path).read_text(encoding="utf-8").strip()
    return value or None


def _load_service_config_from_env() -> ServiceConfig:
    matrix_homeserver = os.getenv("MATRIX_HOMESERVER", "https://mindroom.chat").strip().rstrip("/")
    if not matrix_homeserver:
        msg = "MATRIX_HOMESERVER must be set."
        raise ValueError(msg)

    registration_token = _read_secret(
        env_name="MATRIX_REGISTRATION_TOKEN",
        file_env_name="MATRIX_REGISTRATION_TOKEN_FILE",
    )
    if not registration_token:
        msg = "Set MATRIX_REGISTRATION_TOKEN (or MATRIX_REGISTRATION_TOKEN_FILE)."
        raise ValueError(msg)

    state_path = Path(os.getenv("MINDROOM_PROVISIONING_STATE_PATH", DEFAULT_STATE_PATH)).expanduser()
    pair_ttl = _env_int(
        "MINDROOM_PROVISIONING_PAIR_TTL_SECONDS",
        default=DEFAULT_PAIR_CODE_TTL_SECONDS,
        minimum=30,
    )
    token_ttl = _env_int(
        "MINDROOM_PROVISIONING_TOKEN_TTL_SECONDS",
        default=DEFAULT_REGISTRATION_TOKEN_TTL_SECONDS,
        minimum=30,
    )
    poll_interval = _env_int(
        "MINDROOM_PROVISIONING_POLL_INTERVAL_SECONDS",
        default=DEFAULT_PAIR_POLL_INTERVAL_SECONDS,
        minimum=1,
    )

    raw_origins = os.getenv("MINDROOM_PROVISIONING_CORS_ORIGINS", DEFAULT_CORS_ORIGINS)
    cors_origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    if not cors_origins:
        cors_origins = [DEFAULT_CORS_ORIGINS]

    return ServiceConfig(
        matrix_homeserver=matrix_homeserver,
        matrix_ssl_verify=_env_bool("MATRIX_SSL_VERIFY", default=True),
        matrix_registration_token=registration_token,
        state_path=state_path,
        pair_code_ttl_seconds=pair_ttl,
        registration_token_ttl_seconds=token_ttl,
        pair_poll_interval_seconds=poll_interval,
        cors_origins=cors_origins,
        listen_host=os.getenv("MINDROOM_PROVISIONING_HOST", DEFAULT_LISTEN_HOST).strip(),
        listen_port=_env_int("MINDROOM_PROVISIONING_PORT", default=DEFAULT_LISTEN_PORT, minimum=1),
    )


def _serialize_connection(connection: LocalConnection) -> LocalConnectionOut:
    return LocalConnectionOut(
        id=connection.id,
        client_name=connection.client_name,
        fingerprint=connection.fingerprint,
        created_at=connection.created_at,
        last_seen_at=connection.last_seen_at,
        revoked_at=connection.revoked_at,
    )


def _pair_sessions_payload() -> list[dict[str, str | None]]:
    return [
        {
            "id": session.id,
            "user_id": session.user_id,
            "pair_code_hash": session.pair_code_hash,
            "status": session.status,
            "created_at": _as_utc_iso(session.created_at),
            "expires_at": _as_utc_iso(session.expires_at),
            "completed_at": _as_utc_iso(session.completed_at),
            "connection_id": session.connection_id,
        }
        for session in _pair_sessions.values()
    ]


def _connections_payload() -> list[dict[str, str | None]]:
    return [
        {
            "id": connection.id,
            "user_id": connection.user_id,
            "client_name": connection.client_name,
            "fingerprint": connection.fingerprint,
            "client_secret_hash": connection.client_secret_hash,
            "created_at": _as_utc_iso(connection.created_at),
            "last_seen_at": _as_utc_iso(connection.last_seen_at),
            "revoked_at": _as_utc_iso(connection.revoked_at),
        }
        for connection in _connections.values()
    ]


def _issued_credentials_payload() -> list[dict[str, str | int | None]]:
    return [
        {
            "id": credential.id,
            "connection_id": credential.connection_id,
            "purpose": credential.purpose,
            "token_hash": credential.token_hash,
            "agent_hint": credential.agent_hint,
            "uses_remaining": credential.uses_remaining,
            "created_at": _as_utc_iso(credential.created_at),
            "expires_at": _as_utc_iso(credential.expires_at),
            "revoked_at": _as_utc_iso(credential.revoked_at),
        }
        for credential in _issued_credentials.values()
    ]


def _persist_state_unlocked(state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "pair_sessions": _pair_sessions_payload(),
        "connections": _connections_payload(),
        "issued_credentials": _issued_credentials_payload(),
    }
    tmp_path = state_path.with_suffix(f"{state_path.suffix}.tmp")
    tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(state_path)


def _clear_state_unlocked() -> None:
    _pair_sessions.clear()
    _pair_session_by_hash.clear()
    _connections.clear()
    _issued_credentials.clear()


def _load_state_from_disk_unlocked(state_path: Path) -> None:
    _clear_state_unlocked()
    if not state_path.exists():
        return

    payload = json.loads(state_path.read_text(encoding="utf-8"))

    for item in payload.get("pair_sessions", []):
        session = PairSession(
            id=item["id"],
            user_id=item["user_id"],
            pair_code_hash=item["pair_code_hash"],
            status=item["status"],
            created_at=_from_utc_iso(item["created_at"]) or _now_utc(),
            expires_at=_from_utc_iso(item["expires_at"]) or _now_utc(),
            completed_at=_from_utc_iso(item.get("completed_at")),
            connection_id=item.get("connection_id"),
        )
        _pair_sessions[session.id] = session
        _pair_session_by_hash[session.pair_code_hash] = session.id

    for item in payload.get("connections", []):
        connection = LocalConnection(
            id=item["id"],
            user_id=item["user_id"],
            client_name=item["client_name"],
            fingerprint=item["fingerprint"],
            client_secret_hash=item["client_secret_hash"],
            created_at=_from_utc_iso(item["created_at"]) or _now_utc(),
            last_seen_at=_from_utc_iso(item["last_seen_at"]) or _now_utc(),
            revoked_at=_from_utc_iso(item.get("revoked_at")),
        )
        _connections[connection.id] = connection

    for item in payload.get("issued_credentials", []):
        credential = IssuedCredential(
            id=item["id"],
            connection_id=item["connection_id"],
            purpose=item["purpose"],
            token_hash=item["token_hash"],
            agent_hint=item.get("agent_hint"),
            uses_remaining=int(item["uses_remaining"]),
            created_at=_from_utc_iso(item["created_at"]) or _now_utc(),
            expires_at=_from_utc_iso(item["expires_at"]) or _now_utc(),
            revoked_at=_from_utc_iso(item.get("revoked_at")),
        )
        _issued_credentials[credential.id] = credential


def _normalize_pair_code(pair_code: str) -> str:
    return pair_code.strip().upper()


def _generate_pair_code() -> str:
    left = "".join(secrets.choice(PAIR_CODE_ALPHABET) for _ in range(4))
    right = "".join(secrets.choice(PAIR_CODE_ALPHABET) for _ in range(4))
    return f"{left}-{right}"


def _find_pair_session_unlocked(pair_code: str) -> PairSession | None:
    pair_hash = _hash_token(_normalize_pair_code(pair_code))
    session_id = _pair_session_by_hash.get(pair_hash)
    if not session_id:
        return None
    return _pair_sessions.get(session_id)


def _expire_if_needed(session: PairSession, now: datetime) -> None:
    if session.status == "pending" and session.expires_at <= now:
        session.status = "expired"


def _enforce_rate_limit_unlocked(*, key: str, limit: int, window_seconds: int) -> None:
    now = time.monotonic()
    window_start = now - window_seconds
    entries = [value for value in _rate_limit_buckets.get(key, []) if value >= window_start]
    if len(entries) >= limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    entries.append(now)
    _rate_limit_buckets[key] = entries


def _require_local_client(client_id: str | None, client_secret: str | None) -> LocalConnection:
    if not client_id or not client_secret:
        raise HTTPException(status_code=401, detail="Missing local client credentials")
    connection = _connections.get(client_id)
    if not connection:
        raise HTTPException(status_code=401, detail="Invalid local client credentials")
    expected_hash = connection.client_secret_hash
    provided_hash = _hash_token(client_secret)
    if not hmac.compare_digest(expected_hash, provided_hash):
        raise HTTPException(status_code=401, detail="Invalid local client credentials")
    if connection.revoked_at:
        raise HTTPException(status_code=403, detail="Connection revoked")
    return connection


async def _matrix_whoami(config: ServiceConfig, access_token: str) -> str:
    url = f"{config.matrix_homeserver}/_matrix/client/v3/account/whoami"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=8, verify=config.matrix_ssl_verify) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Matrix homeserver: {exc}") from exc

    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="Invalid Matrix access token")
    if not response.is_success:
        raise HTTPException(status_code=502, detail="Matrix homeserver whoami failed")

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Matrix homeserver returned invalid whoami response") from exc

    user_id = payload.get("user_id") if isinstance(payload, dict) else None
    if not isinstance(user_id, str) or not user_id.startswith("@"):
        raise HTTPException(status_code=502, detail="Matrix whoami response missing user_id")
    return user_id


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None


async def _verify_browser_user(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_matrix_access_token: Annotated[str | None, Header(alias="X-Matrix-Access-Token")] = None,
) -> str:
    token = _extract_bearer_token(authorization)
    if not token and x_matrix_access_token:
        token = x_matrix_access_token.strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing Matrix access token")

    config: ServiceConfig = request.app.state.service_config
    return await _matrix_whoami(config, token)


def create_app(config: ServiceConfig | None = None) -> FastAPI:  # noqa: C901, PLR0915
    """Create the standalone provisioning FastAPI app."""
    service_config = config or _load_service_config_from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.service_config = service_config
        async with _state_lock:
            _load_state_from_disk_unlocked(service_config.state_path)
        yield

    app = FastAPI(title="MindRoom Local Provisioning Service", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=service_config.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Matrix-Access-Token"],
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/local-mindroom/pair/start", response_model=PairStartResponse)
    async def start_pair(
        user_id: Annotated[str, Depends(_verify_browser_user)],
    ) -> PairStartResponse:
        now = _now_utc()
        expires_at = now + timedelta(seconds=service_config.pair_code_ttl_seconds)
        pair_code = _generate_pair_code()
        pair_hash = _hash_token(pair_code)
        session_id = secrets.token_urlsafe(18)

        async with _state_lock:
            _enforce_rate_limit_unlocked(key=f"pair:start:{user_id}", limit=10, window_seconds=60)
            for session in _pair_sessions.values():
                _expire_if_needed(session, now)
                if session.user_id == user_id and session.status == "pending":
                    session.status = "expired"

            session = PairSession(
                id=session_id,
                user_id=user_id,
                pair_code_hash=pair_hash,
                status="pending",
                created_at=now,
                expires_at=expires_at,
            )
            _pair_sessions[session_id] = session
            _pair_session_by_hash[pair_hash] = session_id
            _persist_state_unlocked(service_config.state_path)

        return PairStartResponse(
            pair_code=pair_code,
            expires_at=expires_at,
            poll_interval_seconds=service_config.pair_poll_interval_seconds,
        )

    @app.get("/v1/local-mindroom/pair/status", response_model=PairStatusResponse)
    async def pair_status(
        pair_code: str,
        user_id: Annotated[str, Depends(_verify_browser_user)],
    ) -> PairStatusResponse:
        now = _now_utc()
        async with _state_lock:
            _enforce_rate_limit_unlocked(key=f"pair:status:{user_id}", limit=60, window_seconds=60)
            session = _find_pair_session_unlocked(pair_code)
            if not session or session.user_id != user_id:
                raise HTTPException(status_code=404, detail="Pair code not found")

            _expire_if_needed(session, now)
            if session.status == "connected" and session.connection_id:
                connection = _connections.get(session.connection_id)
                if connection:
                    return PairStatusResponse(status="connected", connection=_serialize_connection(connection))
            if session.status == "expired":
                return PairStatusResponse(status="expired")
            return PairStatusResponse(status="pending", expires_at=session.expires_at)

    @app.post("/v1/local-mindroom/pair/complete", response_model=PairCompleteResponse)
    async def pair_complete(
        request: Request,
        payload: PairCompleteRequest,
    ) -> PairCompleteResponse:
        now = _now_utc()
        remote = request.client.host if request.client else "unknown"
        async with _state_lock:
            _enforce_rate_limit_unlocked(key=f"pair:complete:{remote}", limit=20, window_seconds=60)
            session = _find_pair_session_unlocked(payload.pair_code)
            if not session:
                raise HTTPException(status_code=404, detail="Pair code not found")

            _expire_if_needed(session, now)
            if session.status == "expired":
                raise HTTPException(status_code=410, detail="Pair code expired")
            if session.status == "connected":
                raise HTTPException(status_code=409, detail="Pair code already used")

            client_secret = secrets.token_urlsafe(32)
            connection_id = secrets.token_urlsafe(18)
            connection = LocalConnection(
                id=connection_id,
                user_id=session.user_id,
                client_name=payload.client_name.strip(),
                fingerprint=payload.client_pubkey_or_fingerprint.strip(),
                client_secret_hash=_hash_token(client_secret),
                created_at=now,
                last_seen_at=now,
            )
            _connections[connection_id] = connection

            session.status = "connected"
            session.completed_at = now
            session.connection_id = connection_id
            _persist_state_unlocked(service_config.state_path)

        return PairCompleteResponse(
            connection=_serialize_connection(connection),
            client_id=connection.id,
            client_secret=client_secret,
        )

    @app.get("/v1/local-mindroom/connections", response_model=ConnectionsResponse)
    async def list_connections(
        user_id: Annotated[str, Depends(_verify_browser_user)],
    ) -> ConnectionsResponse:
        async with _state_lock:
            _enforce_rate_limit_unlocked(key=f"connections:list:{user_id}", limit=60, window_seconds=60)
            connections = [_serialize_connection(c) for c in _connections.values() if c.user_id == user_id]
        return ConnectionsResponse(connections=connections)

    @app.delete("/v1/local-mindroom/connections/{connection_id}", response_model=RevokeConnectionResponse)
    async def revoke_connection(
        connection_id: str,
        user_id: Annotated[str, Depends(_verify_browser_user)],
    ) -> RevokeConnectionResponse:
        now = _now_utc()
        async with _state_lock:
            _enforce_rate_limit_unlocked(key=f"connections:revoke:{user_id}", limit=20, window_seconds=60)
            connection = _connections.get(connection_id)
            if not connection or connection.user_id != user_id:
                raise HTTPException(status_code=404, detail="Connection not found")
            connection.revoked_at = now
            connection.last_seen_at = now

            for credential in _issued_credentials.values():
                if credential.connection_id == connection_id:
                    credential.revoked_at = now
                    credential.uses_remaining = 0

            _persist_state_unlocked(service_config.state_path)

        return RevokeConnectionResponse(revoked=True, connection_id=connection_id)

    @app.post("/v1/local-mindroom/tokens/issue", response_model=IssueTokenResponse)
    async def issue_registration_token(
        payload: IssueTokenRequest,
        x_local_mindroom_client_id: Annotated[str | None, Header(alias="X-Local-MindRoom-Client-Id")] = None,
        x_local_mindroom_client_secret: Annotated[str | None, Header(alias="X-Local-MindRoom-Client-Secret")] = None,
    ) -> IssueTokenResponse:
        now = _now_utc()
        expires_at = now + timedelta(seconds=service_config.registration_token_ttl_seconds)

        async with _state_lock:
            connection = _require_local_client(x_local_mindroom_client_id, x_local_mindroom_client_secret)
            _enforce_rate_limit_unlocked(key=f"tokens:issue:{connection.id}", limit=60, window_seconds=60)
            connection.last_seen_at = now

            # Tuwunel currently expects the configured registration token.
            # We gate access here (paired client + revocation + rate limits).
            registration_token = service_config.matrix_registration_token
            credential_id = secrets.token_urlsafe(18)
            credential = IssuedCredential(
                id=credential_id,
                connection_id=connection.id,
                purpose=payload.purpose,
                token_hash=_hash_token(registration_token),
                agent_hint=payload.agent_hint,
                uses_remaining=1,
                created_at=now,
                expires_at=expires_at,
            )
            _issued_credentials[credential_id] = credential
            _persist_state_unlocked(service_config.state_path)

        return IssueTokenResponse(
            credential_id=credential_id,
            registration_token=registration_token,
            expires_at=expires_at,
            uses_remaining=1,
        )

    return app


def main() -> None:
    """Run the provisioning API with uvicorn."""
    config = _load_service_config_from_env()
    app = create_app(config)
    uvicorn.run(app, host=config.listen_host, port=config.listen_port)


if __name__ == "__main__":
    main()
