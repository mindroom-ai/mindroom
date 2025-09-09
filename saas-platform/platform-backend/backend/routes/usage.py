from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from backend.deps import ensure_supabase, verify_user
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter()


@router.get("/api/v1/usage")
async def get_user_usage(
    user=Depends(verify_user),  # noqa: B008
    days: int = 30,
) -> dict[str, Any]:
    """Get usage metrics for current user."""
    sb = ensure_supabase()

    try:
        account_id = user["account_id"]
        sub_result = sb.table("subscriptions").select("id").eq("account_id", account_id).single().execute()
        if not sub_result.data:
            return {"usage": [], "aggregated": {"totalMessages": 0, "totalAgents": 0, "totalStorage": 0}}

        subscription_id = sub_result.data["id"]
        start_date = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()

        usage_result = (
            sb.table("usage_metrics")
            .select("*")
            .eq("subscription_id", subscription_id)
            .gte("metric_date", start_date)
            .order("metric_date", desc=False)
            .execute()
        )

        usage_data = usage_result.data or []
        total_messages = sum(d["messages_sent"] for d in usage_data)
        total_agents = max((d["agents_used"] for d in usage_data), default=0)
        total_storage = max((d["storage_used_gb"] for d in usage_data), default=0)

        return {
            "usage": usage_data,
            "aggregated": {
                "totalMessages": total_messages,
                "totalAgents": total_agents,
                "totalStorage": total_storage,
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to fetch usage") from e
