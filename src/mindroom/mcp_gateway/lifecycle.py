"""Durable grant metadata and exact user connection ownership."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from mindroom.origins import normalized_origin

if TYPE_CHECKING:
    import sqlite3


# LEGACY_COMPAT: Gateway grants and consent stored with the configured public-URL spelling.
# Legacy format: grant, capability, and pending-consent payloads whose `resource` keeps the operator's raw
# MINDROOM_PUBLIC_URL spelling (host case preserved, an explicit default port kept), written when OAuth URLs
# were derived from the configured value verbatim instead of the normalized browser origin.
# Last legacy release: no tagged cutoff; every build before the origin normalization wrote the raw spelling.
# Handling: readers match a stored resource against the current one after normalizing its origin, and new
# grants and tokens are written with the normalized resource, so stored families converge on first use.
# Coverage: tests/test_mcp_gateway_oauth.py::test_grants_stored_with_the_configured_spelling_still_load,
# tests/test_mcp_gateway_oauth.py::test_pending_consent_stored_with_the_configured_spelling_still_completes,
# tests/test_mcp_gateway_oauth.py::test_revoke_all_covers_grants_and_pending_stored_with_the_configured_spelling.


def resources_match(stored: object, current: str) -> bool:
    """Return whether a stored resource names the current resource under any equivalent origin spelling."""
    if stored == current:
        return True
    if not isinstance(stored, str):
        return False
    origin = normalized_origin(stored)
    return origin is not None and origin + urlsplit(stored).path == current


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
    account_id: str | None,
    resource: str,
    active_at: float | None = None,
    accounts_required: bool = False,
    after: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Match canonical and original identities plus the exact account and resource context.

    The resource context matches under equivalent origin spellings (see the LEGACY_COMPAT note on
    `resources_match`), which SQL cannot normalize, so that one comparison runs in Python after the
    identity and account filters have bounded the rows to one owner; paging still applies afterwards.
    """
    query = """SELECT * FROM grants WHERE requester_id = ?
        AND json_extract(payload, '$.authenticated_user_id') = ?
        AND account_id IS ?"""
    values: list[Any] = [requester_id, authenticated_user_id, account_id]
    if active_at is not None:
        query += """ AND revoked = 0 AND expires_at > ? AND idle_expires_at > ?
            AND ((? = 0 AND account_id IS NULL) OR (? = 1 AND EXISTS (
                SELECT 1 FROM gateway_accounts a WHERE a.account_id = grants.account_id AND a.active = 1)))"""
        values.extend([active_at, active_at, accounts_required, accounts_required])
    query += " ORDER BY grant_id"
    rows = [
        row
        for row in connection.execute(query, values).fetchall()
        if resources_match(json.loads(row["payload"]).get("resource"), resource)
    ]
    if after is not None:
        rows = [row for row in rows if row["grant_id"] > after]
    if limit is not None:
        rows = rows[:limit]
    return rows


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
