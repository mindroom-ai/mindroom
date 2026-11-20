"""SQLite store for email invites: requests, hashed codes, claims and caps."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING, Literal, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.constants import RuntimePaths

# Caps: 5 requests per asker and 3 sends per address per 24 hours, 20 open requests per room, 50 sends per day.
_DAY = 86400
_OPEN = "revoked = 0 AND expires_at > ? AND invited_at IS NULL"
_SCHEMA = """CREATE TABLE IF NOT EXISTS email_invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT NOT NULL, thread_id TEXT, email TEXT NOT NULL,
    requested_by TEXT NOT NULL, agent_name TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
    inviter_id TEXT, code_hash TEXT UNIQUE, sent_at INTEGER, revoked INTEGER NOT NULL DEFAULT 0,
    claimed_by TEXT, claimed_at INTEGER, invited_at INTEGER, dispatched_at INTEGER)"""

_LimitName = Literal["asker", "room", "address", "deployment"]


class EmailInviteLimitError(ValueError):
    """One of the daily or per-room caps is reached."""

    def __init__(self, limit: _LimitName) -> None:
        super().__init__(f"email invite limit reached: {limit}")
        self.limit: _LimitName = limit


@dataclass(frozen=True, slots=True)
class EmailInvite:
    """One stored invite request; `inviter_id`, `sent_at` are None while it waits for approval."""

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
    dispatched_at: int | None


_COLUMNS = ", ".join(field.name for field in fields(EmailInvite))


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else now


@contextmanager
def _connection(paths: RuntimePaths) -> Iterator[sqlite3.Connection]:
    path = paths.storage_root / "tracking" / "email_invites.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    connection = sqlite3.connect(path, isolation_level=None, timeout=2)
    try:
        path.chmod(0o600)
        connection.execute(_SCHEMA)
        yield connection
    finally:
        connection.close()


@contextmanager
def _write(paths: RuntimePaths, now: int) -> Iterator[sqlite3.Connection]:
    """Run one `BEGIN IMMEDIATE` transaction, purging rows a day past expiry first."""
    with _connection(paths) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("DELETE FROM email_invites WHERE expires_at < ?", (now - _DAY,))
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")


def _select(connection: sqlite3.Connection, where: str, params: tuple) -> list[EmailInvite]:
    rows = connection.execute(f"SELECT {_COLUMNS} FROM email_invites WHERE {where} ORDER BY id", params)  # noqa: S608
    return [replace(EmailInvite(*row), revoked=bool(row[10])) for row in rows]


def _enforce(connection: sqlite3.Connection, limit: _LimitName, maximum: int, where: str, params: tuple) -> None:
    if connection.execute(f"SELECT COUNT(*) FROM email_invites WHERE {where}", params).fetchone()[0] >= maximum:  # noqa: S608
        raise EmailInviteLimitError(limit)


class Request(NamedTuple):
    """Where a request is made and by whom: the room, its thread, the address, the person, and the agent."""

    room_id: str
    thread_id: str | None
    email: str
    person: str
    agent_name: str


def _insert(connection: sqlite3.Connection, request: Request, now: int, expiry_days: int) -> EmailInvite:
    """Insert a waiting request within the asker and room caps; ValueError when one is already open."""
    _enforce(connection, "asker", 5, "requested_by = ? AND created_at > ?", (request.person, now - _DAY))
    _enforce(connection, "room", 20, f"room_id = ? AND {_OPEN}", (request.room_id, now))
    if _select(connection, f"room_id = ? AND email = ? AND {_OPEN}", (request.room_id, request.email, now)):
        msg = "an open invite for this address already exists in this room"
        raise ValueError(msg)
    cursor = connection.execute(
        "INSERT INTO email_invites (room_id, thread_id, email, requested_by, agent_name, created_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (*request, now, now + expiry_days * _DAY),
    )
    return _select(connection, "id = ?", (cursor.lastrowid,))[0]


def create_request(paths: RuntimePaths, request: Request, *, expiry_days: int, now: int | None = None) -> EmailInvite:
    """Store a waiting request asked by `request.person`, within the asker and room caps."""
    current = _now(now)
    with _write(paths, current) as connection:
        return _insert(connection, request, current, expiry_days)


def issue_code(
    paths: RuntimePaths,
    request: Request,
    *,
    expiry_days: int,
    now: int | None = None,
) -> tuple[EmailInvite, str]:
    """Send, as `request.person`, the open request for the room and address, or a new one, in one transaction.

    A waiting request is used; a sent one is replaced, so its link stops working. A refused cap changes nothing.
    """
    current = _now(now)
    code = secrets.token_urlsafe(24)
    with _write(paths, current) as connection:
        rows = _select(connection, f"room_id = ? AND email = ? AND {_OPEN}", (request.room_id, request.email, current))
        invite = rows[0] if rows and rows[0].sent_at is None else None
        if rows and invite is None:
            if rows[0].dispatched_at is not None:
                msg = "the open invite is being used"
                raise ValueError(msg)
            connection.execute("UPDATE email_invites SET revoked = 1 WHERE id = ?", (rows[0].id,))
        invite = invite or _insert(connection, request, current, expiry_days)
        _enforce(connection, "address", 3, "email = ? AND sent_at > ?", (request.email, current - _DAY))
        _enforce(connection, "deployment", 50, "sent_at > ?", (current - _DAY,))
        expires_at = current + expiry_days * _DAY
        connection.execute(
            "UPDATE email_invites SET inviter_id = ?, code_hash = ?, sent_at = ?, expires_at = ? WHERE id = ?",
            (request.person, _hash(code), current, expires_at, invite.id),
        )
    return replace(invite, inviter_id=request.person, sent_at=current, expires_at=expires_at), code


def get(paths: RuntimePaths, invite_id: int) -> EmailInvite | None:
    """Return one request whatever its state."""
    with _connection(paths) as connection:
        rows = _select(connection, "id = ?", (invite_id,))
    return rows[0] if rows else None


def open_invites(paths: RuntimePaths, room_id: str, now: int | None = None) -> list[EmailInvite]:
    """Return the open requests of one room, oldest first."""
    with _connection(paths) as connection:
        return _select(connection, f"room_id = ? AND {_OPEN}", (room_id, _now(now)))


def open_invite(paths: RuntimePaths, *, room_id: str, email: str, now: int | None = None) -> EmailInvite | None:
    """Return the open request for this room and address, if any."""
    return next((invite for invite in open_invites(paths, room_id, now) if invite.email == email), None)


def used_invite(paths: RuntimePaths, *, room_id: str, email: str) -> EmailInvite | None:
    """Return the newest request for this room and address that was used."""
    with _connection(paths) as connection:
        rows = _select(connection, "room_id = ? AND email = ? AND invited_at IS NOT NULL", (room_id, email))
    return rows[-1] if rows else None


def find_by_code(paths: RuntimePaths, code: str, now: int | None = None) -> EmailInvite | None:
    """Return the request for this code when it is not revoked and either still valid or already used."""
    with _connection(paths) as connection:
        where = "code_hash = ? AND revoked = 0 AND (expires_at > ? OR invited_at IS NOT NULL)"
        rows = _select(connection, where, (_hash(code), _now(now)))
    return rows[0] if rows else None


def _update(paths: RuntimePaths, now: int | None, sql: str, params: tuple) -> bool:
    with _write(paths, _now(now)) as connection:
        return connection.execute(sql, params).rowcount == 1


def revoke(paths: RuntimePaths, invite_id: int, now: int | None = None) -> bool:
    """Revoke a request no claim is inviting with; true means no Matrix invite will follow."""
    sql = "UPDATE email_invites SET revoked = 1 WHERE id = ? AND revoked = 0 AND invited_at IS NULL AND dispatched_at IS NULL"
    return _update(paths, now, sql, (invite_id,))


def bind_claim(paths: RuntimePaths, invite_id: int, claimant: str, now: int | None = None) -> bool:
    """Bind the code to one account in one conditional update; true when it is bound to that account."""
    current = _now(now)
    sql = (
        "UPDATE email_invites SET claimed_by = ?, claimed_at = COALESCE(claimed_at, ?) WHERE id = ? AND revoked = 0"
        " AND expires_at > ? AND invited_at IS NULL AND (claimed_by IS NULL OR claimed_by = ?)"
    )
    return _update(paths, current, sql, (claimant, current, invite_id, current, claimant))


def start_dispatch(paths: RuntimePaths, invite_id: int, claimant: str, now: int | None = None) -> bool:
    """Win the one Matrix invite of a row still open and bound to `claimant`; revoke and expiry lose after this."""
    current = _now(now)
    sql = (
        "UPDATE email_invites SET dispatched_at = ? WHERE id = ? AND claimed_by = ? AND revoked = 0"
        " AND expires_at > ? AND invited_at IS NULL AND dispatched_at IS NULL"
    )
    return _update(paths, current, sql, (current, invite_id, claimant, current))


def end_dispatch(paths: RuntimePaths, invite_id: int) -> None:
    """Release a dispatch the homeserver answered without inviting, so a later claim may try again."""
    _update(
        paths,
        None,
        "UPDATE email_invites SET dispatched_at = NULL WHERE id = ? AND invited_at IS NULL",
        (invite_id,),
    )


def mark_invited(paths: RuntimePaths, invite_id: int, now: int | None = None) -> bool:
    """Record that the Matrix invite for this request succeeded; true only the first time."""
    current = _now(now)
    sql = "UPDATE email_invites SET invited_at = ? WHERE id = ? AND invited_at IS NULL"
    return _update(paths, current, sql, (current, invite_id))
