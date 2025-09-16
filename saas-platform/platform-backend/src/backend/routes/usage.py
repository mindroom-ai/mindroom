"""Usage metrics and monitoring routes."""

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
    sub_result = sb.table("subscriptions").select("id").eq("account_id", account_id).execute()
    if not sub_result.data:
        return UsageResponse(
            usage=[],
            aggregated=UsageAggregateOut(total_messages=0, total_agents=0, total_storage=0),
        ).model_dump(by_alias=True)

    subscription_id = sub_result.data[0]["id"]
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
    total_messages = sum(d["messages_sent"] or 0 for d in usage_data)
    total_agents = max(
        (d["agents_used"] for d in usage_data if d["agents_used"] is not None),
        default=0,
    )
    total_storage = max(
        (d["storage_used_gb"] for d in usage_data if d["storage_used_gb"] is not None),
        default=0,
    )

    # Clean up None values in usage_data
    cleaned_usage = []
    for d in usage_data:
        cleaned = d.copy()
        if cleaned.get("agents_used") is None:
            cleaned["agents_used"] = 0
        if cleaned.get("storage_used_gb") is None:
            cleaned["storage_used_gb"] = 0.0
        if cleaned.get("messages_sent") is None:
            cleaned["messages_sent"] = 0
        cleaned_usage.append(cleaned)

    return UsageResponse(
        usage=[UsageMetricOut(**d) for d in cleaned_usage],
        aggregated=UsageAggregateOut(
            total_messages=total_messages,
            total_agents=total_agents,
            total_storage=float(total_storage) if total_storage is not None else 0,
        ),
    ).model_dump(by_alias=True)
