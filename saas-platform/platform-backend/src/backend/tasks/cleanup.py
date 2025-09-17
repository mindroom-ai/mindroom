"""
Cleanup tasks for GDPR compliance and data retention.
KISS principle - simple scheduled cleanup jobs.
"""

from datetime import UTC, datetime, timedelta
import logging

from backend.deps import ensure_supabase

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

    result = sb.table("usage_metrics").delete().lt("date", cutoff_date.isoformat()).execute()

    metrics_deleted = len(result.data or [])

    return {
        "usage_metrics_deleted": metrics_deleted,
        "cutoff_date": cutoff_date.isoformat(),
        "timestamp": datetime.now(UTC).isoformat(),
    }


def run_all_cleanup_tasks() -> dict:
    """
    Run all cleanup tasks.
    This should be scheduled to run daily via cron/scheduler.
    """
    return {
        "accounts": cleanup_soft_deleted_accounts(),
        "audit_logs": cleanup_old_audit_logs(),
        "usage_metrics": cleanup_old_usage_metrics(),
    }


if __name__ == "__main__":
    # Can be run directly for testing
    import json

    results = run_all_cleanup_tasks()
    print(json.dumps(results, indent=2))
