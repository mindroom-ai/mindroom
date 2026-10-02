"""Supabase data access for the `instances` table.

Centralizes the query patterns that repeat across route modules and the
provisioner service (execute + extract ``.data`` + handle empty), so routes
mock a narrow function surface instead of Supabase client chains.
Rows stay plain dicts; this module adds no model layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from backend.deps import ensure_supabase

if TYPE_CHECKING:
    from supabase import Client

_OWNED_INSTANCE_COLUMNS = "id,instance_id,subscription_id,account_id,lifecycle_stopped_at"


def get_instance(sb: Client, instance_id: int | str, columns: str = "*") -> dict[str, Any] | None:
    """Return one instance row by instance_id, or None when absent."""
    result = sb.table("instances").select(columns).eq("instance_id", str(instance_id)).execute()
    return result.data[0] if result.data else None


def get_owned_instance(sb: Client, instance_id: int | str, account_id: str) -> dict[str, Any] | None:
    """Return the instance row when it belongs to the account, or None."""
    result = (
        sb.table("instances")
        .select(_OWNED_INSTANCE_COLUMNS)
        .eq("instance_id", str(instance_id))
        .eq("account_id", account_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def get_instances_for_account(sb: Client, account_id: str, *, newest_first: bool = False) -> list[dict[str, Any]]:
    """Return all instance rows for an account."""
    query = sb.table("instances").select("*").eq("account_id", account_id)
    if newest_first:
        query = query.order("created_at", desc=True)
    return query.execute().data or []


def list_instances(sb: Client, columns: str = "*") -> list[dict[str, Any]]:
    """Return all instance rows, optionally restricted to a column subset."""
    return sb.table("instances").select(columns).execute().data or []


def create_instance(sb: Client, fields: dict[str, Any]) -> dict[str, Any] | None:
    """Insert one instance row and return it, or None when the insert returned no rows."""
    result = sb.table("instances").insert(fields).execute()
    return result.data[0] if result.data else None


def update_instance(
    sb: Client,
    instance_id: int | str,
    fields: dict[str, Any],
    *,
    expected_status: str | None = None,
    without_openrouter_key: bool = False,
    expected_openrouter_key_hash: str | None = None,
) -> list[dict[str, Any]]:
    """Update an instance row by instance_id and return the updated rows.

    With ``expected_status``, only a row still in that status is updated, so one of several concurrent callers wins.
    With ``without_openrouter_key``, only a row that records no OpenRouter key is updated, so one of several
    concurrent provisions records the key it created.
    With ``expected_openrouter_key_hash``, only a row that still records that OpenRouter key is updated.
    Stamps ``updated_at`` automatically unless the caller provides its own value.
    """
    payload = {"updated_at": datetime.now(UTC).isoformat(), **fields}
    query = sb.table("instances").update(payload).eq("instance_id", str(instance_id))
    if expected_status is not None:
        query = query.eq("status", expected_status)
    if without_openrouter_key:
        query = query.is_("openrouter_key_hash", "null")
    if expected_openrouter_key_hash is not None:
        query = query.eq("openrouter_key_hash", expected_openrouter_key_hash)
    return query.execute().data or []


def update_instance_status(instance_id: int | str, status: str) -> bool:
    """Best-effort instance status update; returns False instead of raising."""
    try:
        update_instance(ensure_supabase(), instance_id, {"status": status})
    except Exception:
        return False
    return True
