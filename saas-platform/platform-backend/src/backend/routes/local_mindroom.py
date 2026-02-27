"""Local MindRoom pairing and provisioning routes.

This is an initial in-memory implementation to validate API/UX flows.
Replace in-memory state with persistent storage for production use.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from backend.config import logger
from backend.deps import limiter, verify_user
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

PAIR_CODE_TTL_MINUTES = 10
REGISTRATION_TOKEN_TTL_MINUTES = 5
PAIR_POLL_INTERVAL_SECONDS = 3
PAIR_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

router = APIRouter(prefix="/v1/local-mindroom", tags=["local-mindroom"])


@dataclass
class PairSession:
    id: str
    user_id: str
    pair_code_hash: str
    status: Literal["pending", "connected", "expired"]
    created_at: datetime
    expires_at: datetime
    completed_at: datetime | None = None
    connection_id: str | None = None


@dataclass
class LocalConnection:
    id: str
    user_id: str
    client_name: str
    fingerprint: str
    client_secret_hash: str
    created_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None = None


@dataclass
class IssuedCredential:
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
    pair_code: str
    expires_at: datetime
    poll_interval_seconds: int = Field(default=PAIR_POLL_INTERVAL_SECONDS)


class LocalConnectionOut(BaseModel):
    id: str
    client_name: str
    fingerprint: str
    created_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None = None


class PairStatusResponse(BaseModel):
    status: Literal["pending", "connected", "expired"]
    expires_at: datetime | None = None
    connection: LocalConnectionOut | None = None


class PairCompleteRequest(BaseModel):
    pair_code: str = Field(min_length=9, max_length=9)
    client_name: str = Field(min_length=1, max_length=120)
    client_pubkey_or_fingerprint: str = Field(min_length=1, max_length=512)


class PairCompleteResponse(BaseModel):
    connection: LocalConnectionOut
    client_id: str
    client_secret: str


class ConnectionsResponse(BaseModel):
    connections: list[LocalConnectionOut]


class RevokeConnectionResponse(BaseModel):
    revoked: bool
    connection_id: str


class IssueTokenRequest(BaseModel):
    purpose: Literal["register_agent"] = "register_agent"
    agent_hint: str | None = Field(default=None, max_length=120)


class IssueTokenResponse(BaseModel):
    credential_id: str
    registration_token: str
    expires_at: datetime
    uses_remaining: int


_state_lock = asyncio.Lock()
_pair_sessions: dict[str, PairSession] = {}
_pair_session_by_hash: dict[str, str] = {}
_connections: dict[str, LocalConnection] = {}
_issued_credentials: dict[str, IssuedCredential] = {}


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _generate_pair_code() -> str:
    left = "".join(secrets.choice(PAIR_CODE_ALPHABET) for _ in range(4))
    right = "".join(secrets.choice(PAIR_CODE_ALPHABET) for _ in range(4))
    return f"{left}-{right}"


def _normalize_pair_code(pair_code: str) -> str:
    return pair_code.strip().upper()


def _serialize_connection(connection: LocalConnection) -> LocalConnectionOut:
    return LocalConnectionOut(
        id=connection.id,
        client_name=connection.client_name,
        fingerprint=connection.fingerprint,
        created_at=connection.created_at,
        last_seen_at=connection.last_seen_at,
        revoked_at=connection.revoked_at,
    )


def _expire_if_needed(session: PairSession, now: datetime) -> None:
    if session.status == "pending" and session.expires_at <= now:
        session.status = "expired"


def _find_pair_session_unlocked(pair_code: str) -> PairSession | None:
    pair_hash = _hash_token(_normalize_pair_code(pair_code))
    session_id = _pair_session_by_hash.get(pair_hash)
    if not session_id:
        return None
    return _pair_sessions.get(session_id)


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


@router.post("/pair/start", response_model=PairStartResponse)
@limiter.limit("10/minute")
async def start_pair(
    request: Request,  # noqa: ARG001
    user: dict = Depends(verify_user),
) -> PairStartResponse:
    """Create a short-lived one-time pair code for the authenticated user."""
    user_id = str(user["user_id"])
    now = _now_utc()
    expires_at = now + timedelta(minutes=PAIR_CODE_TTL_MINUTES)
    pair_code = _generate_pair_code()
    pair_hash = _hash_token(pair_code)
    session_id = secrets.token_urlsafe(18)

    async with _state_lock:
        for session in _pair_sessions.values():
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

    logger.info("Created local MindRoom pair session user_id=%s session_id=%s", user_id, session_id)
    return PairStartResponse(pair_code=pair_code, expires_at=expires_at)


@router.get("/pair/status", response_model=PairStatusResponse)
@limiter.limit("30/minute")
async def pair_status(
    request: Request,  # noqa: ARG001
    pair_code: str,
    user: dict = Depends(verify_user),
) -> PairStatusResponse:
    """Return pairing status for a code owned by the authenticated user."""
    user_id = str(user["user_id"])
    now = _now_utc()
    async with _state_lock:
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


@router.post("/pair/complete", response_model=PairCompleteResponse)
@limiter.limit("20/minute")
async def pair_complete(
    request: Request,  # noqa: ARG001
    payload: PairCompleteRequest,
) -> PairCompleteResponse:
    """Complete a pairing request from a local MindRoom client."""
    now = _now_utc()
    async with _state_lock:
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

    logger.info(
        "Completed local MindRoom pairing user_id=%s session_id=%s connection_id=%s",
        session.user_id,
        session.id,
        connection_id,
    )
    return PairCompleteResponse(
        connection=_serialize_connection(connection),
        client_id=connection.id,
        client_secret=client_secret,
    )


@router.get("/connections", response_model=ConnectionsResponse)
@limiter.limit("30/minute")
async def list_connections(
    request: Request,  # noqa: ARG001
    user: dict = Depends(verify_user),
) -> ConnectionsResponse:
    """List local MindRoom installations linked to the authenticated user."""
    user_id = str(user["user_id"])
    async with _state_lock:
        connections = [_serialize_connection(c) for c in _connections.values() if c.user_id == user_id]
    return ConnectionsResponse(connections=connections)


@router.delete("/connections/{connection_id}", response_model=RevokeConnectionResponse)
@limiter.limit("20/minute")
async def revoke_connection(
    request: Request,  # noqa: ARG001
    connection_id: str,
    user: dict = Depends(verify_user),
) -> RevokeConnectionResponse:
    """Revoke a linked local MindRoom installation."""
    user_id = str(user["user_id"])
    now = _now_utc()

    async with _state_lock:
        connection = _connections.get(connection_id)
        if not connection or connection.user_id != user_id:
            raise HTTPException(status_code=404, detail="Connection not found")
        connection.revoked_at = now

        for credential in _issued_credentials.values():
            if credential.connection_id == connection_id:
                credential.revoked_at = now
                credential.uses_remaining = 0

    logger.info("Revoked local MindRoom connection user_id=%s connection_id=%s", user_id, connection_id)
    return RevokeConnectionResponse(revoked=True, connection_id=connection_id)


@router.post("/tokens/issue", response_model=IssueTokenResponse)
@limiter.limit("60/minute")
async def issue_registration_token(
    request: Request,  # noqa: ARG001
    payload: IssueTokenRequest,
    x_local_mindroom_client_id: Annotated[str | None, Header(alias="X-Local-MindRoom-Client-Id")] = None,
    x_local_mindroom_client_secret: Annotated[str | None, Header(alias="X-Local-MindRoom-Client-Secret")] = None,
) -> IssueTokenResponse:
    """Issue a short-lived, one-use registration credential for a linked local client."""
    now = _now_utc()
    expires_at = now + timedelta(minutes=REGISTRATION_TOKEN_TTL_MINUTES)
    registration_token = secrets.token_urlsafe(32)
    credential_id = secrets.token_urlsafe(18)

    async with _state_lock:
        connection = _require_local_client(x_local_mindroom_client_id, x_local_mindroom_client_secret)
        connection.last_seen_at = now

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

    logger.info(
        "Issued local MindRoom registration credential connection_id=%s credential_id=%s purpose=%s",
        connection.id,
        credential_id,
        payload.purpose,
    )
    return IssueTokenResponse(
        credential_id=credential_id,
        registration_token=registration_token,
        expires_at=expires_at,
        uses_remaining=1,
    )
