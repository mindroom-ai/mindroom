"""Requester-plus-agent Desktop pairing over authenticated Matrix to-device events."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.desktop.protocol import desktop_pairing_verification

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths

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


@contextmanager
def _pairing_connection(runtime_paths: RuntimePaths) -> Iterator[sqlite3.Connection]:
    path = _pairing_db_path(runtime_paths)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    connection = sqlite3.connect(path)
    try:
        path.chmod(0o600)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS desktop_pairings (
                token_hash TEXT PRIMARY KEY,
                requester_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                device_user_id TEXT,
                device_id TEXT,
                device_ed25519 TEXT
            )
            """,
        )
        with connection:
            yield connection
    finally:
        connection.close()


def _purge_expired(connection: sqlite3.Connection, now: int) -> None:
    connection.execute("DELETE FROM desktop_pairings WHERE expires_at <= ?", (now,))


def create_desktop_pairing(
    runtime_paths: RuntimePaths,
    *,
    requester_id: str,
    agent_name: str,
    now: int | None = None,
) -> DesktopPairingStart:
    """Create one single-use pairing token bound to requester plus agent."""
    current_time = int(time.time()) if now is None else now
    token = secrets.token_urlsafe(_PAIRING_TOKEN_BYTES)
    expires_at = current_time + _PAIRING_TTL_SECONDS
    with _pairing_connection(runtime_paths) as connection:
        _purge_expired(connection, current_time)
        connection.execute(
            """
            INSERT INTO desktop_pairings (
                token_hash, requester_id, agent_name, expires_at
            ) VALUES (?, ?, ?, ?)
            """,
            (_token_hash(token), requester_id, agent_name, expires_at),
        )
    return DesktopPairingStart(token=token, expires_at=expires_at)


def _pending_pairing(
    row: tuple[str, str, int, str | None, str | None, str | None],
) -> PendingDesktopPairing:
    requester_id, agent_name, expires_at, device_user_id, device_id, device_ed25519 = row
    return PendingDesktopPairing(
        requester_id=requester_id,
        agent_name=agent_name,
        expires_at=expires_at,
        device_user_id=device_user_id,
        device_id=device_id,
        device_ed25519=device_ed25519,
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
        SELECT requester_id, agent_name, expires_at,
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
    with _pairing_connection(runtime_paths) as connection:
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
    """Return one claimed pairing only in its original requester-agent conversation."""
    current_time = int(time.time()) if now is None else now
    with _pairing_connection(runtime_paths) as connection:
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
    with _pairing_connection(runtime_paths) as connection:
        connection.execute("DELETE FROM desktop_pairings WHERE token_hash = ?", (_token_hash(token),))


__all__ = [
    "DesktopPairingError",
    "DesktopPairingStart",
    "PendingDesktopPairing",
    "claim_desktop_pairing",
    "complete_desktop_pairing",
    "confirm_desktop_pairing",
    "create_desktop_pairing",
]
