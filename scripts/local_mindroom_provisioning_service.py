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
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


PAIR_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
DEFAULT_PAIR_CODE_TTL_SECONDS = 10 * 60
DEFAULT_PAIR_POLL_INTERVAL_SECONDS = 3
DEFAULT_STATE_PATH = "/var/lib/mindroom-local-provisioning/state.json"
DEFAULT_CORS_ORIGINS = "https://chat.mindroom.chat"
DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 8776
RATE_LIMIT_CLEANUP_INTERVAL_SECONDS = 300
RATE_LIMIT_STALE_SECONDS = 3600


@dataclass(slots=True)
class ServiceConfig:
    """Runtime configuration for the provisioning service."""

    matrix_homeserver: str
    matrix_server_name: str
    matrix_ssl_verify: bool
    matrix_registration_token: str
    state_path: Path
    pair_code_ttl_seconds: int
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
class ProvisioningState:
    """In-memory mutable runtime state for one app instance."""

    lock: asyncio.Lock
    pair_sessions: dict[str, PairSession]
    pair_session_by_hash: dict[str, str]
    connections: dict[str, LocalConnection]
    rate_limit_buckets: dict[str, list[float]]
    last_rate_limit_cleanup: float


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


class RegisterAgentRequest(BaseModel):
    """Request payload for registering an agent account via provisioning service."""

    homeserver: str = Field(min_length=1, max_length=512)
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)
    display_name: str = Field(min_length=1, max_length=255)


class RegisterAgentResponse(BaseModel):
    """Result of a server-side agent registration attempt."""

    status: Literal["created", "user_in_use"]
    user_id: str


def _new_runtime_state() -> ProvisioningState:
    return ProvisioningState(
        lock=asyncio.Lock(),
        pair_sessions={},
        pair_session_by_hash={},
        connections={},
        rate_limit_buckets={},
        last_rate_limit_cleanup=0.0,
    )


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
    matrix_server_name = os.getenv("MATRIX_SERVER_NAME", "").strip()
    if not matrix_server_name:
        parsed = httpx.URL(matrix_homeserver)
        if not parsed.host:
            msg = f"Could not infer MATRIX_SERVER_NAME from MATRIX_HOMESERVER: {matrix_homeserver}"
            raise ValueError(msg)
        matrix_server_name = parsed.host

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
        matrix_server_name=matrix_server_name,
        matrix_ssl_verify=_env_bool("MATRIX_SSL_VERIFY", default=True),
        matrix_registration_token=registration_token,
        state_path=state_path,
        pair_code_ttl_seconds=pair_ttl,
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


def _pair_sessions_payload(state: ProvisioningState) -> list[dict[str, str | None]]:
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
        for session in state.pair_sessions.values()
    ]


def _connections_payload(state: ProvisioningState) -> list[dict[str, str | None]]:
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
        for connection in state.connections.values()
    ]


def _persist_state_unlocked(state: ProvisioningState, state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pair_sessions": _pair_sessions_payload(state),
        "connections": _connections_payload(state),
    }
    tmp_path = state_path.with_suffix(f"{state_path.suffix}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(state_path)


def _clear_state_unlocked(state: ProvisioningState) -> None:
    state.pair_sessions.clear()
    state.pair_session_by_hash.clear()
    state.connections.clear()
    state.rate_limit_buckets.clear()


def _load_state_from_disk_unlocked(state: ProvisioningState, state_path: Path) -> None:
    _clear_state_unlocked(state)
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
        state.pair_sessions[session.id] = session
        state.pair_session_by_hash[session.pair_code_hash] = session.id

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
        state.connections[connection.id] = connection


def _normalize_pair_code(pair_code: str) -> str:
    return pair_code.strip().upper()


def _normalize_homeserver_url(homeserver: str) -> str:
    return homeserver.strip().rstrip("/")


def _expected_user_id(server_name: str, username: str) -> str:
    return f"@{username}:{server_name}"


def _generate_pair_code() -> str:
    left = "".join(secrets.choice(PAIR_CODE_ALPHABET) for _ in range(4))
    right = "".join(secrets.choice(PAIR_CODE_ALPHABET) for _ in range(4))
    return f"{left}-{right}"


def _find_pair_session_unlocked(state: ProvisioningState, pair_code: str) -> PairSession | None:
    pair_hash = _hash_token(_normalize_pair_code(pair_code))
    session_id = state.pair_session_by_hash.get(pair_hash)
    if not session_id:
        return None
    return state.pair_sessions.get(session_id)


def _expire_if_needed(session: PairSession, now: datetime) -> None:
    if session.status == "pending" and session.expires_at <= now:
        session.status = "expired"


def _cleanup_rate_limit_buckets_unlocked(
    state: ProvisioningState,
    *,
    now: float,
    stale_seconds: int,
) -> None:
    stale_cutoff = now - stale_seconds
    stale_keys = [key for key, entries in state.rate_limit_buckets.items() if not entries or entries[-1] < stale_cutoff]
    for key in stale_keys:
        state.rate_limit_buckets.pop(key, None)


def _enforce_rate_limit_unlocked(
    state: ProvisioningState,
    *,
    key: str,
    limit: int,
    window_seconds: int,
) -> None:
    now = time.monotonic()
    if now - state.last_rate_limit_cleanup >= RATE_LIMIT_CLEANUP_INTERVAL_SECONDS:
        _cleanup_rate_limit_buckets_unlocked(state, now=now, stale_seconds=RATE_LIMIT_STALE_SECONDS)
        state.last_rate_limit_cleanup = now

    window_start = now - window_seconds
    entries = [value for value in state.rate_limit_buckets.get(key, []) if value >= window_start]
    if len(entries) >= limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    entries.append(now)
    state.rate_limit_buckets[key] = entries


def _require_local_client(
    state: ProvisioningState,
    client_id: str | None,
    client_secret: str | None,
) -> LocalConnection:
    if not client_id or not client_secret:
        raise HTTPException(status_code=401, detail="Missing local client credentials")
    connection = state.connections.get(client_id)
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


async def _register_agent_with_matrix(config: ServiceConfig, payload: RegisterAgentRequest) -> RegisterAgentResponse:
    register_url = f"{config.matrix_homeserver}/_matrix/client/v3/register"
    request_payload = {
        "username": payload.username,
        "password": payload.password,
        "device_name": "mindroom_agent",
        "auth": {
            "type": "m.login.registration_token",
            "token": config.matrix_registration_token,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=10, verify=config.matrix_ssl_verify) as client:
            response = await client.post(register_url, json=request_payload)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Matrix homeserver: {exc}") from exc

    expected_user_id = _expected_user_id(config.matrix_server_name, payload.username)
    if response.is_success:
        try:
            body = response.json()
        except ValueError:
            body = {}
        user_id = body.get("user_id", expected_user_id) if isinstance(body, dict) else expected_user_id
        if not isinstance(user_id, str) or not user_id.startswith("@"):
            user_id = expected_user_id

        access_token = body.get("access_token") if isinstance(body, dict) else None
        if isinstance(access_token, str) and access_token:
            profile_url = f"{config.matrix_homeserver}/_matrix/client/v3/profile/{quote(user_id, safe='')}/displayname"
            headers = {"Authorization": f"Bearer {access_token}"}
            profile_payload = {"displayname": payload.display_name}
            try:
                async with httpx.AsyncClient(timeout=10, verify=config.matrix_ssl_verify) as client:
                    await client.put(profile_url, json=profile_payload, headers=headers)
            except httpx.HTTPError:
                pass

        return RegisterAgentResponse(status="created", user_id=user_id)

    detail = response.text.strip() or "unknown error"
    errcode = None
    try:
        body = response.json()
        if isinstance(body, dict):
            errcode = body.get("errcode")
            detail = str(body.get("error", detail))
    except ValueError:
        pass

    if errcode == "M_USER_IN_USE":
        return RegisterAgentResponse(status="user_in_use", user_id=expected_user_id)

    raise HTTPException(status_code=502, detail=f"Matrix registration failed: {detail}")


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

    config = _service_config_from_request(request)
    return await _matrix_whoami(config, token)


def _service_config_from_request(request: Request) -> ServiceConfig:
    return request.app.state.service_config


def _runtime_state_from_request(request: Request) -> ProvisioningState:
    return request.app.state.runtime_state


router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe endpoint."""
    return {"status": "ok"}


@router.post("/v1/local-mindroom/pair/start", response_model=PairStartResponse)
async def start_pair(
    user_id: Annotated[str, Depends(_verify_browser_user)],
    config: Annotated[ServiceConfig, Depends(_service_config_from_request)],
    state: Annotated[ProvisioningState, Depends(_runtime_state_from_request)],
) -> PairStartResponse:
    """Start a browser-authenticated pairing flow for a local client."""
    now = _now_utc()
    expires_at = now + timedelta(seconds=config.pair_code_ttl_seconds)
    pair_code = _generate_pair_code()
    pair_hash = _hash_token(pair_code)
    session_id = secrets.token_urlsafe(18)

    async with state.lock:
        _enforce_rate_limit_unlocked(state, key=f"pair:start:{user_id}", limit=10, window_seconds=60)
        for session in state.pair_sessions.values():
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
        state.pair_sessions[session_id] = session
        state.pair_session_by_hash[pair_hash] = session_id
        _persist_state_unlocked(state, config.state_path)

    return PairStartResponse(
        pair_code=pair_code,
        expires_at=expires_at,
        poll_interval_seconds=config.pair_poll_interval_seconds,
    )


@router.get("/v1/local-mindroom/pair/status", response_model=PairStatusResponse)
async def pair_status(
    pair_code: str,
    user_id: Annotated[str, Depends(_verify_browser_user)],
    state: Annotated[ProvisioningState, Depends(_runtime_state_from_request)],
) -> PairStatusResponse:
    """Poll the status of a previously issued pair code."""
    now = _now_utc()
    async with state.lock:
        _enforce_rate_limit_unlocked(state, key=f"pair:status:{user_id}", limit=60, window_seconds=60)
        session = _find_pair_session_unlocked(state, pair_code)
        if not session or session.user_id != user_id:
            raise HTTPException(status_code=404, detail="Pair code not found")

        _expire_if_needed(session, now)
        if session.status == "connected" and session.connection_id:
            connection = state.connections.get(session.connection_id)
            if connection:
                return PairStatusResponse(status="connected", connection=_serialize_connection(connection))
        if session.status == "expired":
            return PairStatusResponse(status="expired")
        return PairStatusResponse(status="pending", expires_at=session.expires_at)


@router.post("/v1/local-mindroom/pair/complete", response_model=PairCompleteResponse)
async def pair_complete(
    request: Request,
    payload: PairCompleteRequest,
    config: Annotated[ServiceConfig, Depends(_service_config_from_request)],
    state: Annotated[ProvisioningState, Depends(_runtime_state_from_request)],
) -> PairCompleteResponse:
    """Complete pairing from the local client using the short pair code."""
    now = _now_utc()
    remote = request.client.host if request.client else "unknown"
    async with state.lock:
        _enforce_rate_limit_unlocked(state, key=f"pair:complete:{remote}", limit=20, window_seconds=60)
        session = _find_pair_session_unlocked(state, payload.pair_code)
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
        state.connections[connection_id] = connection

        session.status = "connected"
        session.completed_at = now
        session.connection_id = connection_id
        _persist_state_unlocked(state, config.state_path)

    return PairCompleteResponse(
        connection=_serialize_connection(connection),
        client_id=connection.id,
        client_secret=client_secret,
    )


@router.get("/v1/local-mindroom/connections", response_model=ConnectionsResponse)
async def list_connections(
    user_id: Annotated[str, Depends(_verify_browser_user)],
    state: Annotated[ProvisioningState, Depends(_runtime_state_from_request)],
) -> ConnectionsResponse:
    """List local client connections owned by the authenticated Matrix user."""
    async with state.lock:
        _enforce_rate_limit_unlocked(state, key=f"connections:list:{user_id}", limit=60, window_seconds=60)
        connections = [_serialize_connection(c) for c in state.connections.values() if c.user_id == user_id]
    return ConnectionsResponse(connections=connections)


@router.delete("/v1/local-mindroom/connections/{connection_id}", response_model=RevokeConnectionResponse)
async def revoke_connection(
    connection_id: str,
    user_id: Annotated[str, Depends(_verify_browser_user)],
    config: Annotated[ServiceConfig, Depends(_service_config_from_request)],
    state: Annotated[ProvisioningState, Depends(_runtime_state_from_request)],
) -> RevokeConnectionResponse:
    """Revoke a previously paired local client connection."""
    now = _now_utc()
    async with state.lock:
        _enforce_rate_limit_unlocked(state, key=f"connections:revoke:{user_id}", limit=20, window_seconds=60)
        connection = state.connections.get(connection_id)
        if not connection or connection.user_id != user_id:
            raise HTTPException(status_code=404, detail="Connection not found")
        connection.revoked_at = now
        connection.last_seen_at = now
        _persist_state_unlocked(state, config.state_path)

    return RevokeConnectionResponse(revoked=True, connection_id=connection_id)


@router.post("/v1/local-mindroom/register-agent", response_model=RegisterAgentResponse)
async def register_agent(
    payload: RegisterAgentRequest,
    config: Annotated[ServiceConfig, Depends(_service_config_from_request)],
    state: Annotated[ProvisioningState, Depends(_runtime_state_from_request)],
    x_local_mindroom_client_id: Annotated[str | None, Header(alias="X-Local-MindRoom-Client-Id")] = None,
    x_local_mindroom_client_secret: Annotated[str | None, Header(alias="X-Local-MindRoom-Client-Secret")] = None,
) -> RegisterAgentResponse:
    """Register an agent account server-side for an authenticated local client."""
    now = _now_utc()
    configured_homeserver = _normalize_homeserver_url(config.matrix_homeserver)
    requested_homeserver = _normalize_homeserver_url(payload.homeserver)
    if requested_homeserver != configured_homeserver:
        msg = (
            "Invalid homeserver for this provisioning service. "
            f"Expected {configured_homeserver}, got {requested_homeserver}."
        )
        raise HTTPException(status_code=400, detail=msg)

    async with state.lock:
        connection = _require_local_client(state, x_local_mindroom_client_id, x_local_mindroom_client_secret)
        _enforce_rate_limit_unlocked(state, key=f"register:agent:{connection.id}", limit=60, window_seconds=60)
        connection.last_seen_at = now
        _persist_state_unlocked(state, config.state_path)

    return await _register_agent_with_matrix(config, payload)


def create_app(config: ServiceConfig | None = None) -> FastAPI:
    """Create the standalone provisioning FastAPI app."""
    service_config = config or _load_service_config_from_env()
    runtime_state = _new_runtime_state()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.service_config = service_config
        app.state.runtime_state = runtime_state
        async with runtime_state.lock:
            _load_state_from_disk_unlocked(runtime_state, service_config.state_path)
        yield

    app = FastAPI(title="MindRoom Local Provisioning Service", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=service_config.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Matrix-Access-Token"],
    )
    app.include_router(router)
    return app


def main() -> None:
    """Run the provisioning API with uvicorn."""
    config = _load_service_config_from_env()
    app = create_app(config)
    uvicorn.run(app, host=config.listen_host, port=config.listen_port)


if __name__ == "__main__":
    main()
