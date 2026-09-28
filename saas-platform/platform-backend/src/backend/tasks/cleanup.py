"""
Nightly cleanup job: data retention, GDPR hard deletes, and the hosted instance lifecycle.
Each task runs independently, and every run is recorded in `cleanup_runs` for the admin portal.
"""

from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from backend.deps import ensure_supabase
from backend.services.instance_lifecycle import reconcile_all_subscriptions

logger = logging.getLogger(__name__)


def cleanup_soft_deleted_accounts(grace_period_days: int = 7) -> dict:
    """
    Hard delete accounts that have been soft-deleted for longer than grace period.
    This ensures GDPR compliance while giving users time to recover accounts.
    """
    sb = ensure_supabase()
    cutoff_date = datetime.now(UTC) - timedelta(days=grace_period_days)

    # Find accounts ready for hard deletion
    result = (
        sb.table("accounts")
        .select("id")
        .not_.is_("deleted_at", "null")
        .lt("deleted_at", cutoff_date.isoformat())
        .execute()
    )

    accounts_deleted = 0

    for account in result.data or []:
        # Call hard delete function
        sb.rpc("hard_delete_account", {"target_account_id": account["id"]}).execute()
        accounts_deleted += 1
        logger.info(f"Hard deleted account {account['id']} after {grace_period_days} day grace period")

    return {"accounts_deleted": accounts_deleted, "timestamp": datetime.now(UTC).isoformat()}


def cleanup_old_audit_logs(retention_days: int = 90) -> dict:
    """
    Clean up old audit logs beyond retention period.
    Keep critical security events longer.
    """
    sb = ensure_supabase()
    cutoff_date = datetime.now(UTC) - timedelta(days=retention_days)

    # Delete non-critical audit logs
    # Keep security-related events for 7 years
    critical_actions = [
        "gdpr_deletion_requested",
        "gdpr_deletion_cancelled",
        "account_deleted",
        "admin_privilege_granted",
        "admin_privilege_revoked",
    ]

    result = (
        sb.table("audit_logs")
        .delete()
        .lt("created_at", cutoff_date.isoformat())
        .not_.in_("action", critical_actions)
        .execute()
    )

    logs_deleted = len(result.data or [])

    return {
        "audit_logs_deleted": logs_deleted,
        "cutoff_date": cutoff_date.isoformat(),
        "timestamp": datetime.now(UTC).isoformat(),
    }


def cleanup_old_usage_metrics(retention_days: int = 365) -> dict:
    """
    Clean up old usage metrics beyond retention period.
    Keep aggregated data for longer-term analytics.
    """
    sb = ensure_supabase()
    cutoff_date = datetime.now(UTC) - timedelta(days=retention_days)

    result = sb.table("usage_metrics").delete().lt("metric_date", cutoff_date.date().isoformat()).execute()

    metrics_deleted = len(result.data or [])

    return {
        "usage_metrics_deleted": metrics_deleted,
        "cutoff_date": cutoff_date.isoformat(),
        "timestamp": datetime.now(UTC).isoformat(),
    }


async def run_cleanup_job() -> dict[str, Any]:
    """Run every nightly task independently and record the run.

    One failing task never skips the others; its error is kept in the summary and marks the run failed.
    """
    started_at = datetime.now(UTC)
    summary: dict[str, Any] = {}
    ok = True
    retention_tasks: dict[str, Callable[[], dict]] = {
        "accounts": cleanup_soft_deleted_accounts,
        "audit_logs": cleanup_old_audit_logs,
        "usage_metrics": cleanup_old_usage_metrics,
    }
    for name, task in retention_tasks.items():
        try:
            summary[name] = task()
        except Exception as exc:
            logger.exception("Cleanup task %s failed", name)
            summary[name] = {"error": str(exc)}
            ok = False

    try:
        lifecycle = await reconcile_all_subscriptions()
        summary["instance_lifecycle"] = asdict(lifecycle)
        ok = ok and not lifecycle.errors
    except Exception as exc:
        logger.exception("Instance lifecycle reconcile failed")
        summary["instance_lifecycle"] = {"error": str(exc)}
        ok = False

    run = {
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "ok": ok,
        "summary": summary,
    }
    try:
        ensure_supabase().table("cleanup_runs").insert(run).execute()
    except Exception:
        logger.exception("Failed to record cleanup run")
    return run


if __name__ == "__main__":
    # Can be run directly for testing
    import asyncio
    import json

    print(json.dumps(asyncio.run(run_cleanup_job()), indent=2))
