"""Usage metrics and monitoring routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from backend.deps import ensure_supabase, verify_user
from backend.models import UsageAggregateOut, UsageMetricOut, UsageResponse
from fastapi import APIRouter, Depends

router = APIRouter()


@router.get("/my/usage", response_model=UsageResponse)
async def get_user_usage(
    user: Annotated[dict, Depends(verify_user)],
    days: int = 30,
) -> dict[str, Any]:
    """Get usage metrics for current user."""
    sb = ensure_supabase()

    account_id = user["account_id"]
    sub_result = sb.table("subscriptions").select("id").eq("account_id", account_id).single().execute()
    if not sub_result.data:
        return UsageResponse(
            usage=[],
            aggregated=UsageAggregateOut(total_messages=0, total_agents=0, total_storage=0),
        ).model_dump(by_alias=True)

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

    return UsageResponse(
        usage=[UsageMetricOut(**d) for d in usage_data],
        aggregated=UsageAggregateOut(
            total_messages=total_messages,
            total_agents=total_agents,
            total_storage=float(total_storage) if total_storage is not None else 0,
        ),
    ).model_dump(by_alias=True)
