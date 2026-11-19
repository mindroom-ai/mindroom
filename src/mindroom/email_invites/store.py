"""SQLite store for email invites: requests, hashed codes, claims and caps."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths

ASKER_DAILY_LIMIT = 5
ROOM_OPEN_LIMIT = 20
ADDRESS_DAILY_LIMIT = 3
DEPLOYMENT_DAILY_LIMIT = 50

_DAY_SECONDS = 86400
_CODE_BYTES = 24
_DB_NAME = "email_invites.sqlite"
_COLUMNS = (
    "id, room_id, thread_id, email, requested_by, agent_name, created_at, expires_at,"
    " inviter_id, sent_at, revoked, claimed_by, claimed_at, invited_at"
)
_OPEN = "revoked = 0 AND expires_at > ? AND invited_at IS NULL"

LimitName = Literal["asker", "room", "address", "deployment"]


class EmailInviteLimitError(ValueError):
    """One of the daily or per-room caps is reached."""

    def __init__(self, limit: LimitName) -> None:
        super().__init__(f"email invite limit reached: {limit}")
        self.limit: LimitName = limit


@dataclass(frozen=True, slots=True)
class EmailInvite:
    """One stored invite request."""

    id: int
    room_id: str
    thread_id: str | None
    email: str
    requested_by: str
    agent_name: str
    created_at: int
    expires_at: int
    inviter_id: str | None
    sent_at: int | None
    revoked: bool
    claimed_by: str | None
    claimed_at: int | None
    invited_at: int | None


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else now


def _db_path(paths: RuntimePaths) -> Path:
    return paths.storage_root / "tracking" / _DB_NAME


@contextmanager
def _connection(paths: RuntimePaths) -> Iterator[sqlite3.Connection]:
    path = _db_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    connection = sqlite3.connect(path, isolation_level=None, timeout=10)
    try:
        path.chmod(0o600)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS email_invites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT NOT NULL,
                thread_id TEXT,
                email TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                inviter_id TEXT,
                code_hash TEXT UNIQUE,
                sent_at INTEGER,
                revoked INTEGER NOT NULL DEFAULT 0,
                claimed_by TEXT,
                claimed_at INTEGER,
                invited_at INTEGER
            )
            """,
        )
        yield connection
    finally:
        connection.close()


@contextmanager
def _write(paths: RuntimePaths, now: int) -> Iterator[sqlite3.Connection]:
    """Run one write transaction, purging rows a day past expiry first."""
    with _connection(paths) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("DELETE FROM email_invites WHERE expires_at < ?", (now - _DAY_SECONDS,))
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")


def _invite(row: tuple) -> EmailInvite:
    values = list(row)
    values[10] = bool(values[10])
    return EmailInvite(*values)


def _select(connection: sqlite3.Connection, where: str, params: tuple) -> list[EmailInvite]:
    rows = connection.execute(f"SELECT {_COLUMNS} FROM email_invites WHERE {where} ORDER BY id", params)  # noqa: S608
    return [_invite(row) for row in rows]


def _count(connection: sqlite3.Connection, where: str, params: tuple) -> int:
    return connection.execute(f"SELECT COUNT(*) FROM email_invites WHERE {where}", params).fetchone()[0]  # noqa: S608


def _enforce(connection: sqlite3.Connection, limit: LimitName, maximum: int, where: str, params: tuple) -> None:
    if _count(connection, where, params) >= maximum:
        raise EmailInviteLimitError(limit)


def create_request(
    paths: RuntimePaths,
    *,
    room_id: str,
    thread_id: str | None,
    email: str,
    requested_by: str,
    agent_name: str,
    expiry_days: int,
    now: int | None = None,
) -> EmailInvite:
    """Store a waiting request, within the asker and room caps."""
    current = _now(now)
    with _write(paths, current) as connection:
        _enforce(
            connection,
            "asker",
            ASKER_DAILY_LIMIT,
            "requested_by = ? AND created_at > ?",
            (requested_by, current - _DAY_SECONDS),
        )
        _enforce(connection, "room", ROOM_OPEN_LIMIT, f"room_id = ? AND {_OPEN}", (room_id, current))
        if _count(connection, f"room_id = ? AND email = ? AND {_OPEN}", (room_id, email, current)):
            msg = "an open invite for this address already exists in this room"
            raise ValueError(msg)
        cursor = connection.execute(
            """
            INSERT INTO email_invites (
                room_id, thread_id, email, requested_by, agent_name, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (room_id, thread_id, email, requested_by, agent_name, current, current + expiry_days * _DAY_SECONDS),
        )
        return _select(connection, "id = ?", (cursor.lastrowid,))[0]


def issue_code(
    paths: RuntimePaths,
    invite_id: int,
    *,
    inviter_id: str,
    expiry_days: int,
    now: int | None = None,
) -> str:
    """Issue the single-use code of an open waiting request, within the send caps."""
    current = _now(now)
    code = secrets.token_urlsafe(_CODE_BYTES)
    with _write(paths, current) as connection:
        rows = _select(connection, f"id = ? AND code_hash IS NULL AND {_OPEN}", (invite_id, current))
        if not rows:
            msg = "no open waiting invite with this id"
            raise ValueError(msg)
        since = current - _DAY_SECONDS
        _enforce(connection, "address", ADDRESS_DAILY_LIMIT, "email = ? AND sent_at > ?", (rows[0].email, since))
        _enforce(connection, "deployment", DEPLOYMENT_DAILY_LIMIT, "sent_at > ?", (since,))
        connection.execute(
            "UPDATE email_invites SET inviter_id = ?, code_hash = ?, sent_at = ?, expires_at = ? WHERE id = ?",
            (inviter_id, _code_hash(code), current, current + expiry_days * _DAY_SECONDS, invite_id),
        )
    return code


def open_invite(paths: RuntimePaths, *, room_id: str, email: str, now: int | None = None) -> EmailInvite | None:
    """Return the open request for this room and address, if any."""
    with _connection(paths) as connection:
        rows = _select(connection, f"room_id = ? AND email = ? AND {_OPEN}", (room_id, email, _now(now)))
    return rows[-1] if rows else None


def open_invites(paths: RuntimePaths, room_id: str, now: int | None = None) -> list[EmailInvite]:
    """Return the open requests of one room, oldest first."""
    with _connection(paths) as connection:
        return _select(connection, f"room_id = ? AND {_OPEN}", (room_id, _now(now)))


def used_invite(paths: RuntimePaths, *, room_id: str, email: str) -> EmailInvite | None:
    """Return the newest request for this room and address that was used."""
    with _connection(paths) as connection:
        rows = _select(connection, "room_id = ? AND email = ? AND invited_at IS NOT NULL", (room_id, email))
    return rows[-1] if rows else None


def revoke(paths: RuntimePaths, invite_id: int, now: int | None = None) -> bool:
    """Revoke a request that was not used yet; return whether a row changed."""
    with _write(paths, _now(now)) as connection:
        cursor = connection.execute(
            "UPDATE email_invites SET revoked = 1 WHERE id = ? AND revoked = 0 AND invited_at IS NULL",
            (invite_id,),
        )
        return cursor.rowcount == 1


def find_by_code(paths: RuntimePaths, code: str, now: int | None = None) -> EmailInvite | None:
    """Return the request for this code when it is not revoked and still valid or already used."""
    with _connection(paths) as connection:
        rows = _select(
            connection,
            "code_hash = ? AND revoked = 0 AND (expires_at > ? OR invited_at IS NOT NULL)",
            (_code_hash(code), _now(now)),
        )
    return rows[0] if rows else None


def bind_claim(paths: RuntimePaths, invite_id: int, claimant: str, now: int | None = None) -> bool:
    """Bind the code to one account; true when it is now bound to that account."""
    current = _now(now)
    with _write(paths, current) as connection:
        cursor = connection.execute(
            """
            UPDATE email_invites SET claimed_by = ?, claimed_at = COALESCE(claimed_at, ?)
            WHERE id = ? AND revoked = 0 AND expires_at > ? AND invited_at IS NULL
                AND (claimed_by IS NULL OR claimed_by = ?)
            """,
            (claimant, current, invite_id, current, claimant),
        )
        return cursor.rowcount == 1


def mark_invited(paths: RuntimePaths, invite_id: int, now: int | None = None) -> None:
    """Record that the Matrix invite for this request succeeded."""
    current = _now(now)
    with _write(paths, current) as connection:
        connection.execute(
            "UPDATE email_invites SET invited_at = ? WHERE id = ? AND invited_at IS NULL",
            (current, invite_id),
        )
