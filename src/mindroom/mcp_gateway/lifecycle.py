"""Durable grant metadata and exact personal connection ownership."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3


def migrate_lifecycle(connection: sqlite3.Connection) -> None:
    """Preserve existing absolute deadlines and leave unknown creation/activity dates null."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(grants)")}
    if "idle_expires_at" in columns:
        return
    for declaration in (
        "created_at REAL",
        "last_used_at REAL",
        "last_activity_at REAL",
        "idle_expires_at REAL",
        "account_id TEXT",
    ):
        connection.execute("ALTER TABLE grants ADD COLUMN " + declaration)
    connection.execute("UPDATE grants SET idle_expires_at = expires_at")
    connection.execute("ALTER TABLE pending ADD COLUMN account_id TEXT")
    for statement in (
        "CREATE INDEX grants_idle_expiry ON grants(idle_expires_at)",
        "CREATE INDEX grants_account ON grants(account_id)",
        "CREATE INDEX pending_account ON pending(account_id)",
        "CREATE INDEX capabilities_access_expiry ON capabilities(expires_at) WHERE kind = 'access'",
        """CREATE INDEX grants_owner ON grants(requester_id,
            json_extract(payload, '$.authenticated_user_id'), json_extract(payload, '$.agent_name'),
            json_extract(payload, '$.resource'))""",
        "CREATE INDEX pending_owner ON pending(requester_id, authenticated_user_id, agent_name)",
    ):
        connection.execute(statement)


def touch_grant(
    connection: sqlite3.Connection,
    grant_id: str,
    now: float,
    idle_ttl: int,
    *,
    used: bool = False,
) -> None:
    """Advance validated activity monotonically, bounded by the original absolute deadline."""
    connection.execute(
        """UPDATE grants SET
            last_activity_at = MAX(COALESCE(last_activity_at, ?), ?),
            idle_expires_at = MIN(expires_at, MAX(idle_expires_at, ?)),
            last_used_at = CASE WHEN ? THEN MAX(COALESCE(last_used_at, ?), ?) ELSE last_used_at END
            WHERE grant_id = ?""",
        (now, now, now + idle_ttl, used, now, now, grant_id),
    )


def owned_grants(
    connection: sqlite3.Connection,
    *,
    requester_id: str,
    authenticated_user_id: str,
    agent_name: str,
    resource: str,
    active_at: float | None = None,
    accounts_required: bool = False,
    after: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Match canonical and original identities plus the exact agent and resource context."""
    query = """SELECT * FROM grants WHERE requester_id = ?
        AND json_extract(payload, '$.authenticated_user_id') = ?
        AND json_extract(payload, '$.agent_name') = ? AND json_extract(payload, '$.resource') = ?"""
    values: list[Any] = [requester_id, authenticated_user_id, agent_name, resource]
    if active_at is not None:
        query += """ AND revoked = 0 AND expires_at > ? AND idle_expires_at > ?
            AND ((? = 0 AND account_id IS NULL) OR (? = 1 AND EXISTS (
                SELECT 1 FROM gateway_accounts a WHERE a.account_id = grants.account_id AND a.active = 1)))"""
        values.extend([active_at, active_at, accounts_required, accounts_required])
    if after is not None:
        query += " AND grant_id > ?"
        values.append(after)
    query += " ORDER BY grant_id"
    if limit is not None:
        query += " LIMIT ?"
        values.append(limit)
    return connection.execute(query, values).fetchall()


def public_grant(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    """Expose only connection display metadata, never capability material."""
    payload = json.loads(row["payload"])
    client = connection.execute("SELECT metadata FROM clients WHERE client_id = ?", (payload["client_id"],)).fetchone()
    metadata = json.loads(client["metadata"]) if client else {}
    return {
        "id": row["grant_id"],
        "client_name": metadata.get("client_name") or "MCP client",
        "redirect_uri": payload.get("redirect_uri"),
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "idle_expires_at": row["idle_expires_at"],
        "expires_at": row["expires_at"],
    }
