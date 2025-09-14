"""Common database utilities and query helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from backend.deps import ensure_supabase


def update_instance_status(instance_id: int | str, status: str) -> bool:
    """Update instance status in database."""
    try:
        sb = ensure_supabase()
        sb.table("instances").update({"status": status, "updated_at": datetime.now(UTC).isoformat()}).eq(
            "instance_id", str(instance_id)
        ).execute()
    except Exception:
        return False
    else:
        return True
