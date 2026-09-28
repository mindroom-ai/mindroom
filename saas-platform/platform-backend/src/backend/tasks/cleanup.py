"""
Nightly cleanup job: data retention, GDPR hard deletes, and the hosted instance lifecycle.
Each task runs independently, and every run is recorded in `cleanup_runs` for the admin portal.
"""

from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from backend.config import ACCOUNT_DELETION_GRACE_DAYS
from backend.deps import ensure_supabase
from backend.entitlements import parse_timestamp
from backend.services.instance_lifecycle import (
    account_may_run_instances,
    delete_auth_user,
    end_account_billing_at_period_end,
    reconcile_all_subscriptions,
    resume_subscriptions,
    tear_down_account,
)

logger = logging.getLogger(__name__)


async def cleanup_soft_deleted_accounts(grace_period_days: int = ACCOUNT_DELETION_GRACE_DAYS) -> dict:
    """
    Hard delete accounts that have been soft-deleted for longer than grace period.
    This ensures GDPR compliance while giving users time to recover accounts.

    Each account is first claimed, which ends its restore window, then its Stripe billing is cancelled and its
    hosted instances are uninstalled before its rows go, because those rows are the only record of what to tear
    down; its auth user goes last and takes the account row with it. An account whose teardown or delete fails keeps
    its account row, is reported in `errors`, and is retried by the next run. An account still inside its grace
    period has its renewing Stripe subscriptions set to end with their period again, which covers accounts whose
    deletion was requested before a release that did this at request time.
    """
    sb = ensure_supabase()
    cutoff_date = datetime.now(UTC) - timedelta(days=grace_period_days)

    pending = sb.table("accounts").select("id,deleted_at").not_.is_("deleted_at", "null").execute().data or []

    accounts_deleted = 0
    errors: list[str] = []

    for account in pending:
        account_id = account["id"]
        try:
            deleted_at = parse_timestamp(account["deleted_at"])
            if deleted_at is not None and deleted_at >= cutoff_date:
                await _end_billing_unless_restored(sb, account_id)
                continue
            # The claim uses the database clock, like restore_account, so a restore can never land mid-teardown.
            if not sb.rpc("claim_account_hard_delete", {"target_account_id": account_id}).execute().data:
                logger.info("Skipping account %s: it was restored or its grace period has not ended", account_id)
                continue
            await tear_down_account(account_id)
            sb.rpc("hard_delete_account", {"target_account_id": account_id}).execute()
            await delete_auth_user(account_id)
        except Exception as exc:
            logger.exception("Deletion step failed for account %s; the next run retries", account_id)
            errors.append(f"account {account_id}: {exc}")
            continue
        accounts_deleted += 1
        logger.info(f"Hard deleted account {account_id} after {grace_period_days} day grace period")

    return {"accounts_deleted": accounts_deleted, "errors": errors, "timestamp": datetime.now(UTC).isoformat()}


async def _end_billing_unless_restored(sb: Any, account_id: str) -> None:  # noqa: ANN401
    """Set the account's renewing billing to end, unless the customer restored the account since it was listed."""
    if account_may_run_instances(sb, account_id):
        return
    scheduled = await end_account_billing_at_period_end(account_id)
    # A restore that landed during the Stripe calls resumed only what it saw marked, so undo this run's own marks.
    if scheduled and account_may_run_instances(sb, account_id):
        await resume_subscriptions(scheduled)


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
    try:
        summary["accounts"] = await cleanup_soft_deleted_accounts()
        ok = not summary["accounts"]["errors"]
    except Exception as exc:
        logger.exception("Cleanup task accounts failed")
        summary["accounts"] = {"error": str(exc)}
        ok = False

    retention_tasks: dict[str, Callable[[], dict]] = {
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
