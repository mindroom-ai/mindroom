"""
Cleanup tasks for GDPR compliance and data retention.
KISS principle - simple scheduled cleanup jobs.
"""

from datetime import UTC, datetime, timedelta
import logging

from backend.deps import ensure_supabase
from backend.entitlements import is_expired_trial, is_subscription_service_active
from backend.k8s import instance_deployment_ref, run_kubectl

logger = logging.getLogger(__name__)
RUNNING_INSTANCE_STATUSES = ["running", "provisioning", "restarting"]


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


async def cleanup_unentitled_instances() -> dict:
    """
    Stop hosted instances whose subscription no longer allows infrastructure.
    Expired trials are marked paused so they do not get processed repeatedly.
    """
    sb = ensure_supabase()
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    sub_result = sb.table("subscriptions").select("id,tier,status,trial_ends_at").execute()

    instances_stopped = 0
    subscriptions_paused = 0
    errors = 0

    for subscription in sub_result.data or []:
        if is_subscription_service_active(subscription, now=now):
            continue

        instance_result = (
            sb.table("instances")
            .select("instance_id,status")
            .eq("subscription_id", subscription["id"])
            .in_("status", RUNNING_INSTANCE_STATUSES)
            .execute()
        )

        for instance in instance_result.data or []:
            instance_id = instance["instance_id"]
            code, out, err = await run_kubectl(
                ["scale", instance_deployment_ref(instance_id), "--replicas=0"], namespace="mindroom-instances"
            )
            if code == 0:
                sb.table("instances").update({"status": "stopped", "updated_at": now_iso}).eq(
                    "instance_id", instance_id
                ).execute()
                instances_stopped += 1
                logger.info("Stopped instance %s because subscription %s is inactive", instance_id, subscription["id"])
            else:
                errors += 1
                logger.warning(
                    "Failed to stop instance %s for inactive subscription %s: %s",
                    instance_id,
                    subscription["id"],
                    err.strip() if err else out.strip(),
                )

        if is_expired_trial(subscription, now=now):
            sb.table("subscriptions").update({"status": "paused", "updated_at": now_iso}).eq(
                "id", subscription["id"]
            ).execute()
            subscriptions_paused += 1

    return {
        "instances_stopped": instances_stopped,
        "subscriptions_paused": subscriptions_paused,
        "errors": errors,
        "timestamp": now_iso,
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
    import asyncio
    import json

    results = run_all_cleanup_tasks()
    results["subscription_lifecycle"] = asyncio.run(cleanup_unentitled_instances())
    print(json.dumps(results, indent=2))
