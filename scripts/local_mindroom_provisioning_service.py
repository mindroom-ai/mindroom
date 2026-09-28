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
MindRoom locally. Paired local MindRoom installs receive client credentials
that can request registration tokens for agent account creation.

Authentication: browser users prove their identity with a Matrix OpenID token
in the ``X-Matrix-OpenID-Token`` header. The chat client requests it from the
user's homeserver (``/_matrix/client/v3/user/{userId}/openid/request_token``),
and the service resolves it through its configured homeserver's
``/_matrix/federation/v1/openid/userinfo`` endpoint, accepting only users on
``MATRIX_SERVER_NAME``. An OpenID token grants no account access, so the
service never needs Matrix access tokens. It is still a short-lived bearer
credential: until it expires, any OpenID relying party that validates tokens
with the same homeserver (for example the MatrixRTC lk-jwt-service or an
identity server) accepts it as proof of the user's identity.
Browser-initiated pairing and connection management still accept access
tokens for the currently deployed chat client; device pairing does not.

Pairing flows: the service supports device-initiated pairing (CLI starts at
``/v1/local-mindroom/pair/device/start``, browser user approves at
``/v1/local-mindroom/pair/device/approve``, CLI polls at
``/v1/local-mindroom/pair/device/poll`` for credentials). Before approving,
the browser reads ``/v1/local-mindroom/pair/device/inspect``, which names the
client and the address that started the session. The legacy
browser-initiated flow (``/v1/local-mindroom/pair/start``,
``/v1/local-mindroom/pair/status``, ``/v1/local-mindroom/pair/complete``) is
kept for one release to allow existing chat clients to migrate.

Rate limits: every request that the service must resolve through the
homeserver (OpenID userinfo, or whoami for legacy access tokens) first counts
against a per-client-address limit of 300 per minute, so invalid tokens cannot
make the service flood the homeserver while many users behind one NAT still
reach their per-user limits, which apply after verification. Polls for a
known device secret are limited to 30 per minute per secret, so one client
cannot starve the others; unknown secrets get no per-secret state. All device
polls then count against 300 per minute per client address, leaving room for
many CLIs behind one NAT. The service refuses to start with a
MINDROOM_PROVISIONING_POLL_INTERVAL_SECONDS short enough for the per-secret
limit to throttle clients that follow it.

Retention: pair sessions that expired or were claimed stay one more code
lifetime after expiry or completion, so old codes and replayed polls still
report expired or already claimed, and are then pruned. Connections are never
pruned.

Last seen: paired installs authenticate with their client credentials. Agent
registration, Google OAuth client fetches, and
``/v1/local-mindroom/heartbeat`` all refresh the connection's ``last_seen_at``,
which the chat client shows for each install. Running installs send a heartbeat
at startup and every few hours; the service records heartbeats at most once per
connection every ten minutes so it does not rewrite the state file on every call.

Namespace exemption: pairing always assigns each new connection a random
namespace, and register-agent only accepts usernames shaped like
``mindroom_<entity>_<namespace>``. The operator's own installs are the
deliberate exception: they keep the plain ``mindroom_<entity>`` names. To mark
such a trusted connection namespace-exempt, the operator stops the service,
sets ``"namespace": ""`` on that connection in the persisted state file
(``MINDROOM_PROVISIONING_STATE_PATH``, default
``/var/lib/mindroom-local-provisioning/state.json``), and starts the service
again. The paired install must also unset ``MINDROOM_NAMESPACE`` in its local
``.env`` (``mindroom connect`` writes one during pairing) so the client builds
plain usernames, and the exemption is per-connection, so re-pairing requires
editing the state file again. The value must be exactly ``""``: ``null``, a removed key, or a
whitespace-only string fails closed to a derived namespace that will not match
the connection's original pairing namespace. An exempt connection skips only
the namespace suffix check — usernames must still be valid Matrix localparts
starting with ``mindroom_``. Exemption trusts that connection with the entire
``mindroom_*`` username space, including usernames shaped like other
connections' namespaced agents, so exempt only installs you fully control.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal
from urllib.parse import quote, urlencode

import httpx
import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


PAIR_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
DEFAULT_PAIR_CODE_TTL_SECONDS = 10 * 60
DEFAULT_PAIR_POLL_INTERVAL_SECONDS = 3
APPROVED_CLAIM_GRACE_SECONDS = 60
DEFAULT_STATE_PATH = "/var/lib/mindroom-local-provisioning/state.json"
DEFAULT_CORS_ORIGINS = "https://chat.mindroom.chat"
DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 8776
DEFAULT_APPROVE_URL = "https://chat.mindroom.chat/connect"
OPENID_TOKEN_HEADER = "X-Matrix-OpenID-Token"  # noqa: S105
RATE_LIMIT_CLEANUP_INTERVAL_SECONDS = 300
RATE_LIMIT_STALE_SECONDS = 3600
NAMESPACE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
NAMESPACE_LENGTH = 8
MANAGED_AGENT_USERNAME_PREFIX = "mindroom_"
MATRIX_LOCALPART_RE = re.compile(r"\A[-a-z0-9._=/+]+\Z")
# The local MindRoom client (src/mindroom/matrix/provisioning.py) classifies
# register-agent and heartbeat errors by these exact strings; a contract test keeps the two sides in sync.
CONNECTION_REVOKED_DETAIL = "Connection revoked"
NAMESPACE_MISMATCH_DETAIL = "Requested username is outside this local connection namespace"
# `mindroom connect` and `run` (src/mindroom/cli/connect.py) recognize a lost approval by this exact 410 detail.
PAIR_SESSION_ALREADY_CLAIMED_DETAIL = "Pair session already claimed"
PAIR_STATUS_SESSION_HEADER = "X-Local-MindRoom-Pair-Session-Id"
HEARTBEAT_LAST_SEEN_RESOLUTION = timedelta(minutes=10)
# Browser tokens are resolved by the homeserver, so this per-address limit runs before that lookup.
# It leaves room for several users behind one NAT to reach their per-user limits of 60 per minute.
HOMESERVER_TOKEN_LOOKUP_LIMIT_PER_MINUTE = 300
# Many CLIs polling every few seconds may share one NAT address; each device secret is limited separately.
DEVICE_POLL_LIMIT_PER_ADDRESS_PER_MINUTE = 300
DEVICE_POLL_LIMIT_PER_SECRET_PER_MINUTE = 30
# The rate-limit window includes both ends, so a client polling exactly every 60 / limit seconds sends one poll
# too many per window; the advertised interval must be strictly longer to stay within the per-device limit.
MIN_PAIR_POLL_INTERVAL_SECONDS = 60 // DEVICE_POLL_LIMIT_PER_SECRET_PER_MINUTE + 1


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
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    approve_url: str = DEFAULT_APPROVE_URL


@dataclass(slots=True)
class PairSession:
    """Pair code lifecycle state.

    Browser-initiated sessions know their user at start; device sessions learn it on approval.
    """

    id: str
    user_id: str | None
    pair_code_hash: str
    status: Literal["pending", "approved", "connected", "expired"]
    created_at: datetime
    expires_at: datetime
    completed_at: datetime | None = None
    connection_id: str | None = None
    device_secret_hash: str | None = None
    client_name: str | None = None
    fingerprint: str | None = None
    approved_at: datetime | None = None
    client_ip: str | None = None


@dataclass(slots=True)
class LocalConnection:
    """A linked local MindRoom installation."""

    id: str
    user_id: str
    client_name: str
    fingerprint: str
    namespace: str
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
    pair_session_by_device_secret_hash: dict[str, str]
    connections: dict[str, LocalConnection]
    rate_limit_buckets: dict[str, list[float]]
    last_rate_limit_cleanup: float


class PairStartResponse(BaseModel):
    """Response for starting pairing."""

    pair_code: str
    pair_session_id: str
    expires_at: datetime
    poll_interval_seconds: int


class LocalConnectionOut(BaseModel):
    """Public shape for linked local installations."""

    id: str
    client_name: str
    fingerprint: str
    namespace: str
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
    namespace: str
    owner_user_id: str


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


class HeartbeatResponse(BaseModel):
    """Acknowledgement of a local install heartbeat."""

    status: Literal["ok"]


class GoogleOAuthClientResponse(BaseModel):
    """Installed-app OAuth client returned only to an authenticated local install."""

    client_id: str
    client_secret: str


class DevicePairStartRequest(BaseModel):
    """Local client request to begin device pairing."""

    client_name: str = Field(min_length=1, max_length=120)
    client_pubkey_or_fingerprint: str = Field(min_length=1, max_length=512)


class DevicePairStartResponse(BaseModel):
    """Pair code for the user and polling secret for the local client."""

    pair_code: str
    device_secret: str
    approve_url: str
    expires_at: datetime
    poll_interval_seconds: int


class DevicePairCodeRequest(BaseModel):
    """Browser request naming a device pair code."""

    pair_code: str = Field(min_length=9, max_length=9)


class DevicePairSessionOut(BaseModel):
    """What the approving browser user sees about the waiting machine."""

    client_name: str
    client_ip: str | None
    created_at: datetime
    expires_at: datetime
    status: Literal["pending", "approved"]


class DevicePairPollRequest(BaseModel):
    """Local client poll using its device secret."""

    device_secret: str = Field(min_length=1, max_length=256)


class DevicePairPollResponse(BaseModel):
    """Poll result; credentials appear exactly once, with status connected."""

    status: Literal["pending", "expired", "connected"]
    expires_at: datetime | None = None
    client_id: str | None = None
    client_secret: str | None = None
    namespace: str | None = None
    owner_user_id: str | None = None
    connection: LocalConnectionOut | None = None


def _new_runtime_state() -> ProvisioningState:
    return ProvisioningState(
        lock=asyncio.Lock(),
        pair_sessions={},
        pair_session_by_hash={},
        pair_session_by_device_secret_hash={},
        connections={},
        rate_limit_buckets={},
        last_rate_limit_cleanup=0.0,
    )


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _derive_namespace(seed: str) -> str:
    """Derive a compact namespace from an identifier."""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:NAMESPACE_LENGTH]


def _generate_connection_namespace(state: ProvisioningState) -> str:
    """Generate a namespace that is unique within persisted provisioning state."""
    existing = {connection.namespace for connection in state.connections.values()}
    while True:
        candidate = "".join(secrets.choice(NAMESPACE_ALPHABET) for _ in range(NAMESPACE_LENGTH))
        if candidate not in existing:
            return candidate


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
        minimum=MIN_PAIR_POLL_INTERVAL_SECONDS,
    )

    raw_origins = os.getenv("MINDROOM_PROVISIONING_CORS_ORIGINS", DEFAULT_CORS_ORIGINS)
    cors_origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    if not cors_origins:
        cors_origins = [DEFAULT_CORS_ORIGINS]

    google_oauth_client_id = os.getenv("MINDROOM_GOOGLE_OAUTH_CLIENT_ID", "").strip() or None
    google_oauth_client_secret = _read_secret(
        env_name="MINDROOM_GOOGLE_OAUTH_CLIENT_SECRET",
        file_env_name="MINDROOM_GOOGLE_OAUTH_CLIENT_SECRET_FILE",
    )
    if (google_oauth_client_id is None) != (google_oauth_client_secret is None):
        msg = "MINDROOM_GOOGLE_OAUTH_CLIENT_ID and its client secret must be configured together."
        raise ValueError(msg)

    approve_url = (
        os.getenv("MINDROOM_PROVISIONING_APPROVE_URL", DEFAULT_APPROVE_URL).strip().rstrip("/") or DEFAULT_APPROVE_URL
    )

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
        google_oauth_client_id=google_oauth_client_id,
        google_oauth_client_secret=google_oauth_client_secret,
        approve_url=approve_url,
    )


def _serialize_connection(connection: LocalConnection) -> LocalConnectionOut:
    return LocalConnectionOut(
        id=connection.id,
        client_name=connection.client_name,
        fingerprint=connection.fingerprint,
        namespace=connection.namespace,
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
            "device_secret_hash": session.device_secret_hash,
            "client_name": session.client_name,
            "fingerprint": session.fingerprint,
            "approved_at": _as_utc_iso(session.approved_at),
            "client_ip": session.client_ip,
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
            "namespace": connection.namespace,
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
    state.pair_session_by_device_secret_hash.clear()
    state.connections.clear()
    state.rate_limit_buckets.clear()


def _load_state_from_disk_unlocked(state: ProvisioningState, state_path: Path) -> None:
    _clear_state_unlocked(state)
    if not state_path.exists():
        return

    payload = json.loads(state_path.read_text(encoding="utf-8"))

    for item in payload.get("pair_sessions", []):
        # LEGACY_COMPAT: pair sessions persisted before device pairing lack device fields
        # Legacy format: state written by the provisioning service before this change, which only supported browser-initiated pairing; device_secret_hash, client_name, fingerprint, and approved_at are missing.
        # Last legacy release: unversioned service state; replaced by this change.
        # Handling: missing fields load as None, i.e. a browser-initiated session.
        # Coverage: tests/test_local_mindroom_provisioning_service.py::test_legacy_state_loads_browser_sessions.
        session = PairSession(
            id=item["id"],
            user_id=item["user_id"],
            pair_code_hash=item["pair_code_hash"],
            status=item["status"],
            created_at=_from_utc_iso(item["created_at"]) or _now_utc(),
            expires_at=_from_utc_iso(item["expires_at"]) or _now_utc(),
            completed_at=_from_utc_iso(item.get("completed_at")),
            connection_id=item.get("connection_id"),
            device_secret_hash=item.get("device_secret_hash"),
            client_name=item.get("client_name"),
            fingerprint=item.get("fingerprint"),
            approved_at=_from_utc_iso(item.get("approved_at")),
            # LEGACY_COMPAT: pair sessions persisted before requester addresses were recorded lack client_ip
            # Legacy format: state written by the provisioning service before this change; client_ip is missing.
            # Last legacy release: unversioned service state; replaced by this change.
            # Handling: a missing client_ip loads as None, which the approval page shows as an unknown address.
            # Coverage: tests/test_local_mindroom_provisioning_service.py::test_legacy_state_loads_device_sessions_without_client_ip.
            client_ip=item.get("client_ip"),
        )
        state.pair_sessions[session.id] = session
        state.pair_session_by_hash[session.pair_code_hash] = session.id
        if session.device_secret_hash is not None:
            state.pair_session_by_device_secret_hash[session.device_secret_hash] = session.id

    for item in payload.get("connections", []):
        connection_id = item["id"]
        raw_namespace = item.get("namespace")
        # Only a literal "" (the operator-set exemption sentinel, see module
        # docstring) may stay empty. Whitespace-only, null, and missing values
        # fail closed to a derived namespace so a connection can never become
        # namespace-exempt by accident.
        if raw_namespace == "":
            namespace = ""
        elif isinstance(raw_namespace, str) and raw_namespace.strip():
            namespace = raw_namespace.strip().lower()
        else:
            namespace = _derive_namespace(connection_id)
        connection = LocalConnection(
            id=connection_id,
            user_id=item["user_id"],
            client_name=item["client_name"],
            fingerprint=item["fingerprint"],
            namespace=namespace,
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


def _new_pair_code_unlocked(state: ProvisioningState) -> str:
    """Return a pair code no stored session already uses."""
    while True:
        pair_code = _generate_pair_code()
        if _hash_token(pair_code) not in state.pair_session_by_hash:
            return pair_code


def _find_pair_session_unlocked(state: ProvisioningState, pair_code: str) -> PairSession | None:
    pair_hash = _hash_token(_normalize_pair_code(pair_code))
    session_id = state.pair_session_by_hash.get(pair_hash)
    if not session_id:
        return None
    return state.pair_sessions.get(session_id)


def _is_managed_agent_username_for_namespace(username: str, namespace: str) -> bool:
    """Return whether username matches mindroom_<entity>_<namespace>."""
    suffix = f"_{namespace}"
    return (
        username.startswith(MANAGED_AGENT_USERNAME_PREFIX)
        and username.endswith(suffix)
        and len(username) > len(MANAGED_AGENT_USERNAME_PREFIX) + len(suffix)
    )


def _is_username_permitted_for_connection(username: str, namespace: str) -> bool:
    """Return whether a connection may register the requested agent username.

    An empty namespace marks a namespace-exempt connection (operator-set, see
    module docstring): the namespace suffix check is skipped, but the managed
    agent prefix is still required. Localpart syntax is validated separately
    in register_agent so it surfaces as 400, not 403.
    """
    if not namespace:
        return username.startswith(MANAGED_AGENT_USERNAME_PREFIX) and len(username) > len(
            MANAGED_AGENT_USERNAME_PREFIX,
        )
    return _is_managed_agent_username_for_namespace(username, namespace)


def _expire_if_needed(session: PairSession, now: datetime) -> None:
    if session.status in ("pending", "approved") and session.expires_at <= now:
        session.status = "expired"


def _prune_pair_sessions_unlocked(state: ProvisioningState, now: datetime, pair_code_ttl_seconds: int) -> None:
    """Remove finished sessions after one more code lifetime to prevent unbounded growth.

    Expired sessions stay one code lifetime past expiry so that an old code or
    poll still reports "expired" (410 / ``status="expired"``) instead of "not
    found" after the CLI renews its code. Connected sessions stay one code
    lifetime past completion so a replayed poll still reports 410. Connections
    themselves are never pruned.
    """
    retain_after = now - timedelta(seconds=pair_code_ttl_seconds)
    finished_ids = []
    for session_id, session in state.pair_sessions.items():
        _expire_if_needed(session, now)
        if session.status == "expired":
            finished_at = session.expires_at
        elif session.status == "connected":
            finished_at = session.completed_at or session.expires_at
        else:
            continue
        if finished_at <= retain_after:
            finished_ids.append(session_id)

    for session_id in finished_ids:
        session = state.pair_sessions.pop(session_id)
        state.pair_session_by_hash.pop(session.pair_code_hash, None)
        if session.device_secret_hash is not None:
            state.pair_session_by_device_secret_hash.pop(session.device_secret_hash, None)


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
        raise HTTPException(status_code=403, detail=CONNECTION_REVOKED_DETAIL)
    return connection


async def _matrix_openid_userinfo(config: ServiceConfig, openid_token: str) -> str:
    url = f"{config.matrix_homeserver}/_matrix/federation/v1/openid/userinfo"
    try:
        async with httpx.AsyncClient(timeout=8, verify=config.matrix_ssl_verify) as client:
            response = await client.get(url, params={"access_token": openid_token})
    except httpx.HTTPError as exc:
        # The token is in the request URL, which httpx errors may include, so report only the error type.
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach Matrix homeserver: {type(exc).__name__}",
        ) from None

    try:
        payload = response.json()
    except ValueError:
        payload = None
    errcode = payload.get("errcode") if isinstance(payload, dict) else None
    if response.status_code in {401, 403} or errcode == "M_UNKNOWN_TOKEN":
        raise HTTPException(status_code=401, detail="Invalid Matrix OpenID token")
    if not response.is_success:
        raise HTTPException(status_code=502, detail="Matrix homeserver OpenID userinfo failed")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Matrix homeserver returned invalid OpenID userinfo response")

    user_id = payload.get("sub")
    if not isinstance(user_id, str) or not user_id.startswith("@"):
        raise HTTPException(status_code=401, detail="Matrix OpenID token has no valid user")
    localpart, _, server_name = user_id[1:].partition(":")
    if not localpart or server_name != config.matrix_server_name:
        raise HTTPException(status_code=401, detail="Matrix OpenID token belongs to another server")
    return user_id


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


async def _limit_homeserver_token_lookup(request: Request) -> None:
    """Bound homeserver lookups per client address so invalid tokens cannot amplify traffic."""
    state = _runtime_state_from_request(request)
    remote = request.client.host if request.client else "unknown"
    async with state.lock:
        _enforce_rate_limit_unlocked(
            state,
            key=f"auth:homeserver-lookup:{remote}",
            limit=HOMESERVER_TOKEN_LOOKUP_LIMIT_PER_MINUTE,
            window_seconds=60,
        )


async def _verify_openid_user(
    request: Request,
    x_matrix_openid_token: Annotated[str | None, Header(alias=OPENID_TOKEN_HEADER)] = None,
) -> str:
    token = x_matrix_openid_token.strip() if x_matrix_openid_token else ""
    if not token:
        raise HTTPException(status_code=401, detail="Missing Matrix OpenID token")
    await _limit_homeserver_token_lookup(request)
    return await _matrix_openid_userinfo(_service_config_from_request(request), token)


async def _verify_browser_user(
    request: Request,
    x_matrix_openid_token: Annotated[str | None, Header(alias=OPENID_TOKEN_HEADER)] = None,
    authorization: Annotated[str | None, Header()] = None,
    x_matrix_access_token: Annotated[str | None, Header(alias="X-Matrix-Access-Token")] = None,
) -> str:
    if x_matrix_openid_token and x_matrix_openid_token.strip():
        return await _verify_openid_user(request, x_matrix_openid_token)

    # LEGACY_COMPAT: browser endpoints authenticated with the user's Matrix access token
    # Legacy format: the deployed MindRoom Chat client sends its full Matrix access token as `Authorization: Bearer` or `X-Matrix-Access-Token` to pair/start, pair/status, GET /connections, and DELETE /connections/{id}.
    # Last legacy release: unversioned external input; the deployed chat client is not a MindRoom release, and its replacement sends `X-Matrix-OpenID-Token` instead.
    # Handling: when no OpenID token is sent, these endpoints still resolve the access token through the homeserver's whoami endpoint; the OpenID header wins when both are sent, and device inspect/approve never accept access tokens.
    # Coverage: tests/test_local_mindroom_provisioning_service.py::test_browser_endpoints_still_accept_legacy_access_token_headers, tests/test_local_mindroom_provisioning_service.py::test_openid_token_wins_over_legacy_access_token, tests/test_local_mindroom_provisioning_service.py::test_device_endpoints_reject_access_tokens.
    token = _extract_bearer_token(authorization)
    if not token and x_matrix_access_token:
        token = x_matrix_access_token.strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing Matrix OpenID token")
    await _limit_homeserver_token_lookup(request)
    return await _matrix_whoami(_service_config_from_request(request), token)


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
        _prune_pair_sessions_unlocked(state, now, config.pair_code_ttl_seconds)
        _enforce_rate_limit_unlocked(state, key=f"pair:start:{user_id}", limit=10, window_seconds=60)
        for session in state.pair_sessions.values():
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
        pair_session_id=session_id,
        expires_at=expires_at,
        poll_interval_seconds=config.pair_poll_interval_seconds,
    )


@router.get("/v1/local-mindroom/pair/status", response_model=PairStatusResponse)
async def pair_status(
    user_id: Annotated[str, Depends(_verify_browser_user)],
    state: Annotated[ProvisioningState, Depends(_runtime_state_from_request)],
    x_local_mindroom_pair_session_id: Annotated[
        str | None,
        Header(alias=PAIR_STATUS_SESSION_HEADER),
    ] = None,
) -> PairStatusResponse:
    """Poll a pairing session by opaque session ID header."""
    now = _now_utc()
    async with state.lock:
        _enforce_rate_limit_unlocked(state, key=f"pair:status:{user_id}", limit=60, window_seconds=60)
        session_id = x_local_mindroom_pair_session_id.strip() if x_local_mindroom_pair_session_id else ""
        if not session_id:
            raise HTTPException(status_code=400, detail="Missing pair session id")
        session = state.pair_sessions.get(session_id)
        if not session or session.user_id != user_id:
            raise HTTPException(status_code=404, detail="Pair session not found")

        _expire_if_needed(session, now)
        if session.status == "connected" and session.connection_id:
            connection = state.connections.get(session.connection_id)
            if connection:
                return PairStatusResponse(status="connected", connection=_serialize_connection(connection))
        if session.status == "expired":
            return PairStatusResponse(status="expired")
        return PairStatusResponse(status="pending", expires_at=session.expires_at)


def _create_connection_unlocked(
    state: ProvisioningState,
    *,
    user_id: str,
    client_name: str,
    fingerprint: str,
    now: datetime,
) -> tuple[LocalConnection, str]:
    """Create a local connection and return it with its one-time plaintext secret."""
    client_secret = secrets.token_urlsafe(32)
    connection = LocalConnection(
        id=secrets.token_urlsafe(18),
        user_id=user_id,
        client_name=client_name.strip(),
        fingerprint=fingerprint.strip(),
        namespace=_generate_connection_namespace(state),
        client_secret_hash=_hash_token(client_secret),
        created_at=now,
        last_seen_at=now,
    )
    state.connections[connection.id] = connection
    return connection, client_secret


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
        if session.device_secret_hash is not None or session.user_id is None:
            raise HTTPException(status_code=404, detail="Pair code not found")

        _expire_if_needed(session, now)
        if session.status == "expired":
            raise HTTPException(status_code=410, detail="Pair code expired")
        if session.status == "connected":
            raise HTTPException(status_code=409, detail="Pair code already used")

        connection, client_secret = _create_connection_unlocked(
            state,
            user_id=session.user_id,
            client_name=payload.client_name,
            fingerprint=payload.client_pubkey_or_fingerprint,
            now=now,
        )

        session.status = "connected"
        session.completed_at = now
        session.connection_id = connection.id
        _persist_state_unlocked(state, config.state_path)

    return PairCompleteResponse(
        connection=_serialize_connection(connection),
        client_id=connection.id,
        client_secret=client_secret,
        namespace=connection.namespace,
        owner_user_id=session.user_id,
    )


def _find_device_session_unlocked(state: ProvisioningState, pair_code: str, now: datetime) -> PairSession:
    session = _find_pair_session_unlocked(state, pair_code)
    if session is None or session.device_secret_hash is None:
        raise HTTPException(status_code=404, detail="Pair code not found")
    _expire_if_needed(session, now)
    if session.status == "expired":
        raise HTTPException(status_code=410, detail="Pair code expired")
    if session.status == "connected":
        raise HTTPException(status_code=409, detail="Pair code already used")
    return session


def _device_session_out(session: PairSession) -> DevicePairSessionOut:
    if session.client_name is None:
        raise HTTPException(status_code=500, detail="Corrupt pair session")
    if session.status not in ("pending", "approved"):
        raise HTTPException(status_code=500, detail="Corrupt pair session")
    return DevicePairSessionOut(
        client_name=session.client_name,
        client_ip=session.client_ip,
        created_at=session.created_at,
        expires_at=session.expires_at,
        status=session.status,
    )


@router.post("/v1/local-mindroom/pair/device/start", response_model=DevicePairStartResponse)
async def start_device_pair(
    request: Request,
    payload: DevicePairStartRequest,
    config: Annotated[ServiceConfig, Depends(_service_config_from_request)],
    state: Annotated[ProvisioningState, Depends(_runtime_state_from_request)],
) -> DevicePairStartResponse:
    """Start pairing from a local client; a signed-in browser user approves it later."""
    now = _now_utc()
    remote = request.client.host if request.client else "unknown"
    device_secret = secrets.token_urlsafe(32)
    async with state.lock:
        _prune_pair_sessions_unlocked(state, now, config.pair_code_ttl_seconds)
        _enforce_rate_limit_unlocked(state, key=f"pair:device:start:{remote}", limit=10, window_seconds=60)
        pair_code = _new_pair_code_unlocked(state)
        session = PairSession(
            id=secrets.token_urlsafe(18),
            user_id=None,
            pair_code_hash=_hash_token(pair_code),
            status="pending",
            created_at=now,
            expires_at=now + timedelta(seconds=config.pair_code_ttl_seconds),
            device_secret_hash=_hash_token(device_secret),
            client_name=payload.client_name.strip(),
            fingerprint=payload.client_pubkey_or_fingerprint.strip(),
            client_ip=request.client.host if request.client else None,
        )
        state.pair_sessions[session.id] = session
        state.pair_session_by_hash[session.pair_code_hash] = session.id
        state.pair_session_by_device_secret_hash[session.device_secret_hash] = session.id
        _persist_state_unlocked(state, config.state_path)
    return DevicePairStartResponse(
        pair_code=pair_code,
        device_secret=device_secret,
        approve_url=f"{config.approve_url}?{urlencode({'code': pair_code})}",
        expires_at=session.expires_at,
        poll_interval_seconds=config.pair_poll_interval_seconds,
    )


@router.post("/v1/local-mindroom/pair/device/inspect", response_model=DevicePairSessionOut)
async def inspect_device_pair(
    payload: DevicePairCodeRequest,
    user_id: Annotated[str, Depends(_verify_openid_user)],
    state: Annotated[ProvisioningState, Depends(_runtime_state_from_request)],
) -> DevicePairSessionOut:
    """Describe the machine waiting behind a device code before the user approves it."""
    async with state.lock:
        _enforce_rate_limit_unlocked(state, key=f"pair:device:inspect:{user_id}", limit=20, window_seconds=60)
        return _device_session_out(_find_device_session_unlocked(state, payload.pair_code, _now_utc()))


@router.post("/v1/local-mindroom/pair/device/approve", response_model=DevicePairSessionOut)
async def approve_device_pair(
    payload: DevicePairCodeRequest,
    user_id: Annotated[str, Depends(_verify_openid_user)],
    config: Annotated[ServiceConfig, Depends(_service_config_from_request)],
    state: Annotated[ProvisioningState, Depends(_runtime_state_from_request)],
) -> DevicePairSessionOut:
    """Bind a waiting device session to the approving Matrix user."""
    now = _now_utc()
    async with state.lock:
        _enforce_rate_limit_unlocked(state, key=f"pair:device:approve:{user_id}", limit=20, window_seconds=60)
        session = _find_device_session_unlocked(state, payload.pair_code, now)
        if session.status == "approved" and session.user_id != user_id:
            raise HTTPException(status_code=409, detail="Pair code already approved")
        if session.status == "pending":
            session.status = "approved"
            session.user_id = user_id
            session.approved_at = now
            session.expires_at = max(session.expires_at, now + timedelta(seconds=APPROVED_CLAIM_GRACE_SECONDS))
            _persist_state_unlocked(state, config.state_path)
        return _device_session_out(session)


@router.post("/v1/local-mindroom/pair/device/poll", response_model=DevicePairPollResponse)
async def poll_device_pair(
    request: Request,
    payload: DevicePairPollRequest,
    config: Annotated[ServiceConfig, Depends(_service_config_from_request)],
    state: Annotated[ProvisioningState, Depends(_runtime_state_from_request)],
) -> DevicePairPollResponse:
    """Report device pairing progress and hand out credentials once after approval."""
    now = _now_utc()
    remote = request.client.host if request.client else "unknown"
    device_secret_hash = _hash_token(payload.device_secret)
    async with state.lock:
        session_id = state.pair_session_by_device_secret_hash.get(device_secret_hash)
        session = state.pair_sessions.get(session_id) if session_id else None
        # Known devices are limited first so polls this rejects do not use up the address budget shared behind a NAT.
        # Unknown secrets get no bucket of their own, so random secrets cannot grow the rate-limit state.
        if session is not None:
            _enforce_rate_limit_unlocked(
                state,
                key=f"pair:device:poll:secret:{device_secret_hash}",
                limit=DEVICE_POLL_LIMIT_PER_SECRET_PER_MINUTE,
                window_seconds=60,
            )
        _enforce_rate_limit_unlocked(
            state,
            key=f"pair:device:poll:{remote}",
            limit=DEVICE_POLL_LIMIT_PER_ADDRESS_PER_MINUTE,
            window_seconds=60,
        )
        if session is None:
            raise HTTPException(status_code=404, detail="Pair session not found")
        _expire_if_needed(session, now)
        if session.status == "connected":
            raise HTTPException(status_code=410, detail=PAIR_SESSION_ALREADY_CLAIMED_DETAIL)
        if session.status in ("pending", "expired"):
            return DevicePairPollResponse(status=session.status, expires_at=session.expires_at)
        if session.user_id is None:
            raise HTTPException(status_code=500, detail="Corrupt pair session")
        if session.client_name is None:
            raise HTTPException(status_code=500, detail="Corrupt pair session")
        if session.fingerprint is None:
            raise HTTPException(status_code=500, detail="Corrupt pair session")
        connection, client_secret = _create_connection_unlocked(
            state,
            user_id=session.user_id,
            client_name=session.client_name,
            fingerprint=session.fingerprint,
            now=now,
        )
        session.status = "connected"
        session.completed_at = now
        session.connection_id = connection.id
        _persist_state_unlocked(state, config.state_path)
    return DevicePairPollResponse(
        status="connected",
        client_id=connection.id,
        client_secret=client_secret,
        namespace=connection.namespace,
        owner_user_id=session.user_id,
        connection=_serialize_connection(connection),
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

    # 400, not 403: clients treat 403 as an authorization problem, but a
    # malformed localpart can only be fixed by changing the agent name.
    if MATRIX_LOCALPART_RE.match(payload.username) is None:
        raise HTTPException(
            status_code=400,
            detail="Requested username is not a valid Matrix localpart (allowed: a-z 0-9 . _ = / + -)",
        )

    async with state.lock:
        connection = _require_local_client(state, x_local_mindroom_client_id, x_local_mindroom_client_secret)
        _enforce_rate_limit_unlocked(state, key=f"register:agent:{connection.id}", limit=60, window_seconds=60)
        if not _is_username_permitted_for_connection(payload.username, connection.namespace):
            raise HTTPException(
                status_code=403,
                detail=NAMESPACE_MISMATCH_DETAIL,
            )
        connection.last_seen_at = now
        _persist_state_unlocked(state, config.state_path)

    return await _register_agent_with_matrix(config, payload)


@router.post("/v1/local-mindroom/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    config: Annotated[ServiceConfig, Depends(_service_config_from_request)],
    state: Annotated[ProvisioningState, Depends(_runtime_state_from_request)],
    x_local_mindroom_client_id: Annotated[str | None, Header(alias="X-Local-MindRoom-Client-Id")] = None,
    x_local_mindroom_client_secret: Annotated[str | None, Header(alias="X-Local-MindRoom-Client-Secret")] = None,
) -> HeartbeatResponse:
    """Record that an authenticated local client is still running."""
    now = _now_utc()
    async with state.lock:
        connection = _require_local_client(state, x_local_mindroom_client_id, x_local_mindroom_client_secret)
        _enforce_rate_limit_unlocked(state, key=f"heartbeat:{connection.id}", limit=10, window_seconds=60)
        if now - connection.last_seen_at >= HEARTBEAT_LAST_SEEN_RESOLUTION:
            connection.last_seen_at = now
            _persist_state_unlocked(state, config.state_path)
    return HeartbeatResponse(status="ok")


@router.get("/v1/local-mindroom/oauth/google-client", response_model=GoogleOAuthClientResponse)
async def google_oauth_client(
    response: Response,
    config: Annotated[ServiceConfig, Depends(_service_config_from_request)],
    state: Annotated[ProvisioningState, Depends(_runtime_state_from_request)],
    x_local_mindroom_client_id: Annotated[str | None, Header(alias="X-Local-MindRoom-Client-Id")] = None,
    x_local_mindroom_client_secret: Annotated[str | None, Header(alias="X-Local-MindRoom-Client-Secret")] = None,
) -> GoogleOAuthClientResponse:
    """Return the Google installed-app client to one paired local runtime."""
    now = _now_utc()
    async with state.lock:
        connection = _require_local_client(state, x_local_mindroom_client_id, x_local_mindroom_client_secret)
        _enforce_rate_limit_unlocked(state, key=f"oauth:google-client:{connection.id}", limit=60, window_seconds=60)
        connection.last_seen_at = now
        _persist_state_unlocked(state, config.state_path)

    if not config.google_oauth_client_id or not config.google_oauth_client_secret:
        raise HTTPException(status_code=503, detail="Google OAuth client is not configured")
    response.headers["Cache-Control"] = "no-store"
    return GoogleOAuthClientResponse(
        client_id=config.google_oauth_client_id,
        client_secret=config.google_oauth_client_secret,
    )


def create_app(config: ServiceConfig | None = None) -> FastAPI:
    """Create the standalone provisioning FastAPI app."""
    service_config = config or _load_service_config_from_env()
    runtime_state = _new_runtime_state()
    # httpx logs full request URLs at INFO, and the OpenID userinfo URL carries the token in its query string.
    logging.getLogger("httpx").setLevel(logging.WARNING)

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
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Matrix-Access-Token",
            OPENID_TOKEN_HEADER,
            PAIR_STATUS_SESSION_HEADER,
        ],
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
