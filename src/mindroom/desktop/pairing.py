"""Requester-plus-agent Desktop pairing over authenticated Matrix to-device events."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from mindroom.desktop.protocol import (
    DESKTOP_PAIRING_CLAIM_EVENT_TYPE,
    DesktopPairingClaim,
    DesktopProtocolError,
    desktop_pairing_verification,
    event_content,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.constants import RuntimePaths
    from mindroom.matrix.to_device import AuthenticatedToDeviceEvent

logger = get_logger(__name__)

_PAIRING_TOKEN_BYTES = 24
_PAIRING_TTL_SECONDS = 15 * 60
_PAIRING_DB_NAME = "desktop_pairing.sqlite"


class DesktopPairingError(ValueError):
    """One pairing operation is invalid, expired, or unauthorized."""


@dataclass(frozen=True, slots=True)
class DesktopPairingStart:
    """New raw token returned only to the initiating requester."""

    token: str
    expires_at: int


@dataclass(frozen=True, slots=True)
class PendingDesktopPairing:
    """One pending pairing claim bound to a requester and exact agent."""

    requester_id: str
    agent_name: str
    room_id: str
    thread_id: str | None
    expires_at: int
    device_user_id: str | None
    device_id: str | None
    device_ed25519: str | None

    @property
    def claimed(self) -> bool:
        """Return whether an authenticated local device has presented the token."""
        return all((self.device_user_id, self.device_id, self.device_ed25519))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _pairing_db_path(runtime_paths: RuntimePaths) -> Path:
    return runtime_paths.storage_root / "tracking" / _PAIRING_DB_NAME


def _connect(runtime_paths: RuntimePaths) -> sqlite3.Connection:
    path = _pairing_db_path(runtime_paths)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    connection = sqlite3.connect(path)
    path.chmod(0o600)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS desktop_pairings (
            token_hash TEXT PRIMARY KEY,
            requester_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            room_id TEXT NOT NULL,
            thread_id TEXT,
            expires_at INTEGER NOT NULL,
            device_user_id TEXT,
            device_id TEXT,
            device_ed25519 TEXT
        )
        """,
    )
    return connection


def _purge_expired(connection: sqlite3.Connection, now: int) -> None:
    connection.execute("DELETE FROM desktop_pairings WHERE expires_at <= ?", (now,))


def create_desktop_pairing(
    runtime_paths: RuntimePaths,
    *,
    requester_id: str,
    agent_name: str,
    room_id: str,
    thread_id: str | None,
    now: int | None = None,
) -> DesktopPairingStart:
    """Create one single-use pairing token bound to requester plus agent."""
    current_time = int(time.time()) if now is None else now
    token = secrets.token_urlsafe(_PAIRING_TOKEN_BYTES)
    expires_at = current_time + _PAIRING_TTL_SECONDS
    with _connect(runtime_paths) as connection:
        _purge_expired(connection, current_time)
        connection.execute(
            """
            INSERT INTO desktop_pairings (
                token_hash, requester_id, agent_name, room_id, thread_id, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (_token_hash(token), requester_id, agent_name, room_id, thread_id, expires_at),
        )
    return DesktopPairingStart(token=token, expires_at=expires_at)


def _pending_pairing(row: tuple[object, ...]) -> PendingDesktopPairing:
    requester_id, agent_name, room_id, thread_id, expires_at, device_user_id, device_id, device_ed25519 = row
    required_text = (requester_id, agent_name, room_id)
    optional_text = (thread_id, device_user_id, device_id, device_ed25519)
    if (
        any(not isinstance(value, str) for value in required_text)
        or any(value is not None and not isinstance(value, str) for value in optional_text)
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
    ):
        msg = "Desktop pairing database contains an invalid record."
        raise DesktopPairingError(msg)
    return PendingDesktopPairing(
        requester_id=cast("str", requester_id),
        agent_name=cast("str", agent_name),
        room_id=cast("str", room_id),
        thread_id=cast("str | None", thread_id),
        expires_at=expires_at,
        device_user_id=cast("str | None", device_user_id),
        device_id=cast("str | None", device_id),
        device_ed25519=cast("str | None", device_ed25519),
    )


def _load_pairing(
    connection: sqlite3.Connection,
    token: str,
    *,
    now: int,
) -> PendingDesktopPairing:
    _purge_expired(connection, now)
    row = connection.execute(
        """
        SELECT requester_id, agent_name, room_id, thread_id, expires_at,
               device_user_id, device_id, device_ed25519
        FROM desktop_pairings WHERE token_hash = ?
        """,
        (_token_hash(token),),
    ).fetchone()
    if row is None:
        msg = "Desktop pairing code is invalid or expired."
        raise DesktopPairingError(msg)
    return _pending_pairing(row)


def claim_desktop_pairing(
    runtime_paths: RuntimePaths,
    *,
    token: str,
    agent_name: str,
    device_user_id: str,
    device_id: str,
    device_ed25519: str,
    now: int | None = None,
) -> PendingDesktopPairing:
    """Attach one authenticated Matrix device to a pending pairing token."""
    current_time = int(time.time()) if now is None else now
    with _connect(runtime_paths) as connection:
        pending = _load_pairing(connection, token, now=current_time)
        if pending.agent_name != agent_name:
            msg = "Desktop pairing code belongs to another agent."
            raise DesktopPairingError(msg)
        claimed_identity = (pending.device_user_id, pending.device_id, pending.device_ed25519)
        new_identity = (device_user_id, device_id, device_ed25519)
        if pending.claimed and claimed_identity != new_identity:
            msg = "Desktop pairing code was already claimed by another device."
            raise DesktopPairingError(msg)
        connection.execute(
            """
            UPDATE desktop_pairings
            SET device_user_id = ?, device_id = ?, device_ed25519 = ?
            WHERE token_hash = ?
            """,
            (*new_identity, _token_hash(token)),
        )
        return PendingDesktopPairing(
            requester_id=pending.requester_id,
            agent_name=pending.agent_name,
            room_id=pending.room_id,
            thread_id=pending.thread_id,
            expires_at=pending.expires_at,
            device_user_id=device_user_id,
            device_id=device_id,
            device_ed25519=device_ed25519,
        )


def confirm_desktop_pairing(
    runtime_paths: RuntimePaths,
    *,
    token: str,
    requester_id: str,
    agent_name: str,
    verification: str,
    now: int | None = None,
) -> PendingDesktopPairing:
    """Return one claimed pairing only to its original requester and agent."""
    current_time = int(time.time()) if now is None else now
    with _connect(runtime_paths) as connection:
        pending = _load_pairing(connection, token, now=current_time)
    if pending.requester_id != requester_id or pending.agent_name != agent_name:
        msg = "Desktop pairing code does not belong to this requester and agent."
        raise DesktopPairingError(msg)
    if not pending.claimed:
        msg = "Desktop device has not claimed this pairing code yet."
        raise DesktopPairingError(msg)
    assert pending.device_ed25519 is not None
    expected_verification = desktop_pairing_verification(token, pending.device_ed25519)
    if not secrets.compare_digest(verification.upper(), expected_verification):
        msg = "Desktop pairing verification does not match the claimed local device."
        raise DesktopPairingError(msg)
    return pending


def complete_desktop_pairing(runtime_paths: RuntimePaths, *, token: str) -> None:
    """Consume a token after its scoped Desktop configuration was saved."""
    with _connect(runtime_paths) as connection:
        connection.execute("DELETE FROM desktop_pairings WHERE token_hash = ?", (_token_hash(token),))


async def handle_desktop_pairing_claim(
    event: AuthenticatedToDeviceEvent,
    *,
    client: nio.AsyncClient,
    agent_name: str,
    runtime_paths: RuntimePaths,
) -> None:
    """Record a claim only from its Matrix-authenticated local device identity."""
    if event.type != DESKTOP_PAIRING_CLAIM_EVENT_TYPE:
        return
    try:
        claim = DesktopPairingClaim.from_content(event_content(event.source))
    except DesktopProtocolError as exc:
        logger.warning("desktop_pairing_claim_malformed", agent=agent_name, reason=str(exc))
        return
    olm = client.olm
    if olm is None:
        logger.warning("desktop_pairing_claim_rejected", agent=agent_name, reason="missing_olm")
        return
    device = olm.device_store[event.sender].get(event.authenticated_device_id)
    if device is None or device.blacklisted:
        logger.warning("desktop_pairing_claim_rejected", agent=agent_name, reason="untrusted_device")
        return
    try:
        claim_desktop_pairing(
            runtime_paths,
            token=claim.token,
            agent_name=agent_name,
            device_user_id=event.sender,
            device_id=event.authenticated_device_id,
            device_ed25519=device.ed25519,
        )
    except DesktopPairingError as exc:
        logger.warning("desktop_pairing_claim_rejected", agent=agent_name, reason=str(exc))
        return
    except sqlite3.Error:
        logger.exception("desktop_pairing_claim_db_error", agent=agent_name)
        return
    logger.info("desktop_pairing_claimed", agent=agent_name)


__all__ = [
    "DesktopPairingError",
    "DesktopPairingStart",
    "PendingDesktopPairing",
    "claim_desktop_pairing",
    "complete_desktop_pairing",
    "confirm_desktop_pairing",
    "create_desktop_pairing",
    "handle_desktop_pairing_claim",
]
