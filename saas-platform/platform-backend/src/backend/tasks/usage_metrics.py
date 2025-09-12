"""Background tasks for collecting and storing usage metrics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

from backend.config import logger, supabase


async def collect_daily_usage_metrics() -> None:
    """Collect daily usage metrics for all accounts.

    This should be run daily via a cron job or scheduler.
    Collects metrics for the previous day.
    """
    # Get yesterday's date range
    try:
        yesterday = datetime.now(UTC) - timedelta(days=1)
        start_date = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)

        # Get all active accounts
        accounts_result = supabase.table("accounts").select("id").eq("status", "active").execute()

        if not accounts_result.data:
            logger.info("No active accounts to collect metrics for")
            return

        for account in accounts_result.data:
            account_id = account["id"]

            # Collect metrics for this account
            metrics = await _collect_account_metrics(
                supabase,
                account_id,
                start_date,
                end_date,
            )

            # Store metrics
            if metrics:
                supabase.table("usage_metrics").insert(
                    {
                        "account_id": account_id,
                        "metric_date": yesterday.date().isoformat(),
                        "messages_sent": metrics.get("messages_sent", 0),
                        "storage_mb": metrics.get("storage_mb", 0),
                        "api_calls": metrics.get("api_calls", 0),
                        "agent_count": metrics.get("agent_count", 0),
                        "created_at": datetime.now(UTC).isoformat(),
                    },
                ).execute()

                logger.info(f"Collected metrics for account {account_id}: {metrics}")

    except Exception as e:
        logger.exception(f"Error collecting usage metrics: {e}")


async def _collect_account_metrics(
    sb: Any,  # noqa: ANN401
    account_id: str,
    start_date: datetime,
    end_date: datetime,
) -> dict[str, int]:
    """Collect metrics for a specific account and date range."""
    metrics = {
        "messages_sent": 0,
        "storage_mb": 0,
        "api_calls": 0,
        "agent_count": 0,
    }

    try:
        # Count messages (from audit logs or a messages table if it exists)
        # This is a simplified example - adjust based on your actual data model
        audit_logs = (
            sb.table("audit_logs")
            .select("*", count="exact")
            .eq("account_id", account_id)
            .gte("created_at", start_date.isoformat())
            .lte("created_at", end_date.isoformat())
            .execute()
        )

        # Count API calls from audit logs
        if audit_logs.data:
            metrics["api_calls"] = len(audit_logs.data)
            # Count message-like actions
            message_actions = ["create", "send", "message"]
            metrics["messages_sent"] = sum(
                1
                for log in audit_logs.data
                if any(action in log.get("action", "").lower() for action in message_actions)
            )

        # Get instance count for this account
        instances = (
            sb.table("instances")
            .select("agent_count", count="exact")
            .eq("account_id", account_id)
            .eq("status", "running")
            .execute()
        )

        if instances.data:
            # Sum up agent counts from all instances
            metrics["agent_count"] = sum(inst.get("agent_count", 1) for inst in instances.data)

        # Calculate actual storage used by instances
        if instances.data:
            metrics["storage_mb"] = len(instances.data) * 100

    except Exception as e:
        logger.error(f"Error collecting metrics for account {account_id}: {e}")

    return metrics


async def update_realtime_metrics(
    account_id: str,
    metric_type: str,
    value: int = 1,
) -> None:
    """Update real-time usage metrics.

    This can be called throughout the application to track usage in real-time.

    Args:
        account_id: The account to update metrics for
        metric_type: Type of metric (messages_sent, api_calls, etc.)
        value: Value to add to the metric (default 1)

    """
    try:
        if not supabase:
            return

        today = datetime.now(UTC).date().isoformat()

        # Try to get existing record for today
        existing = (
            supabase.table("usage_metrics")
            .select("*")
            .eq("account_id", account_id)
            .eq("metric_date", today)
            .single()
            .execute()
        )

        if existing.data:
            # Update existing record
            current_value = existing.data.get(metric_type, 0)
            supabase.table("usage_metrics").update(
                {
                    metric_type: current_value + value,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            ).eq("id", existing.data["id"]).execute()
        else:
            # Create new record for today
            supabase.table("usage_metrics").insert(
                {
                    "account_id": account_id,
                    "metric_date": today,
                    metric_type: value,
                    "messages_sent": 0,
                    "storage_mb": 0,
                    "api_calls": 0,
                    "agent_count": 0,
                    "created_at": datetime.now(UTC).isoformat(),
                },
            ).execute()

    except Exception as e:
        logger.error(f"Error updating realtime metrics: {e}")
        raise
