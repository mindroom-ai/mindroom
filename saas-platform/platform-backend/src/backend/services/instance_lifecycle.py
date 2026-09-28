"""Keep hosted instances in line with their subscription.

This module is the single owner of the subscription-driven instance lifecycle.
Stripe webhooks and the nightly cleanup job both call it, so they behave identically:

- Not entitled: stop the instance, disable its platform-paid OpenRouter key, and schedule teardown.
- Entitled again: start the instance (or reprovision it when it was torn down), re-enable the key,
  and clear the schedule.
- Teardown due and still not entitled: uninstall everything and mark the instance deprovisioned.

`lifecycle_stopped_at` marks the instances this module holds.
Customer or admin stops never set it, so a manually stopped instance of an entitled subscription is left alone.
Every step is idempotent: the desired state is re-applied on each run, and a failure is stored in
`lifecycle_error` for the next run to retry.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from backend.config import INSTANCE_TEARDOWN_GRACE_DAYS, logger
from backend.deps import ensure_supabase
from backend.entitlements import is_expired_trial, is_subscription_service_active, parse_timestamp
from backend.k8s import check_deployment_exists, run_kubectl, tenant_stop_deployment_refs
from backend.services.instances_data import get_instance, update_instance
from backend.services.provisioner_service import (
    provision_instance,
    set_instance_openrouter_key_disabled,
    start_instance,
    uninstall_instance,
)

if TYPE_CHECKING:
    from supabase import Client

LIFECYCLE_INSTANCE_COLUMNS = (
    "instance_id,subscription_id,account_id,status,openrouter_key_hash,"
    "lifecycle_stopped_at,teardown_after,lifecycle_error,lifecycle_error_at"
)
_CLEARED_LIFECYCLE_ERROR = {"lifecycle_error": None, "lifecycle_error_at": None}
_PAGE_SIZE = 1000
_INSTANCES_NAMESPACE = "mindroom-instances"
# Serializes webhook-triggered and nightly runs for the same subscription within this process.
_subscription_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


@dataclass
class LifecycleSummary:
    """Counts and errors from one or more reconcile runs."""

    subscriptions_checked: int = 0
    instances_stopped: int = 0
    instances_resumed: int = 0
    instances_torn_down: int = 0
    subscriptions_paused: int = 0
    errors: list[str] = field(default_factory=list)


async def reconcile_subscription_instances(
    subscription: dict[str, Any],
    *,
    instances: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
    summary: LifecycleSummary | None = None,
) -> LifecycleSummary:
    """Bring every instance of one subscription to the state its entitlement requires."""
    summary = summary or LifecycleSummary()
    now = now or datetime.now(UTC)
    sb = ensure_supabase()
    async with _subscription_locks[str(subscription["id"])]:
        if instances is None:
            instances = (
                sb.table("instances")
                .select(LIFECYCLE_INSTANCE_COLUMNS)
                .eq("subscription_id", subscription["id"])
                .execute()
                .data
                or []
            )
        entitled = is_subscription_service_active(subscription, now=now)
        for instance in instances:
            try:
                if entitled:
                    await _resume(sb, instance, subscription, summary)
                else:
                    await _hold(sb, instance, subscription, now, summary)
            except Exception as exc:  # noqa: BLE001
                _record_error(sb, instance, exc, now, summary)

        if is_expired_trial(subscription, now=now):
            sb.table("subscriptions").update({"status": "paused", "updated_at": now.isoformat()}).eq(
                "id", subscription["id"]
            ).execute()
            summary.subscriptions_paused += 1
    summary.subscriptions_checked += 1
    return summary


async def reconcile_account_instances(account_id: str) -> None:
    """Reconcile the instances of one account's subscriptions; used after Stripe webhooks.

    Runs as a background task after the webhook response, so it never fails the webhook.
    Instance errors are stored on the instance and retried by the nightly job.
    """
    try:
        sb = ensure_supabase()
        subscriptions = sb.table("subscriptions").select("*").eq("account_id", account_id).execute().data or []
        for subscription in subscriptions:
            await reconcile_subscription_instances(subscription)
    except Exception:
        logger.exception("Instance lifecycle reconcile failed for account %s; the nightly job retries", account_id)


async def reconcile_all_subscriptions(*, now: datetime | None = None) -> LifecycleSummary:
    """Reconcile every subscription that owns an instance."""
    sb = ensure_supabase()
    summary = LifecycleSummary()
    for subscription, instances in _subscriptions_with_instances(sb):
        try:
            await reconcile_subscription_instances(subscription, instances=instances, now=now, summary=summary)
        except Exception as exc:
            logger.exception("Instance lifecycle reconcile failed for subscription %s", subscription["id"])
            summary.errors.append(f"subscription {subscription['id']}: {exc}")
    return summary


def lifecycle_overview(*, now: datetime | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return instances pending teardown and instances whose lifecycle looks stuck, for the admin portal.

    Each row carries the instance lifecycle columns, `account_email`, `subscription_status`, and `problem`.
    """
    now = now or datetime.now(UTC)
    rows = _instance_rows(
        ensure_supabase(), f"{LIFECYCLE_INSTANCE_COLUMNS},subscription:subscriptions(*),account:accounts(email)"
    )
    pending: list[dict[str, Any]] = []
    stuck: list[dict[str, Any]] = []
    for row in rows:
        subscription = row.pop("subscription", None) or {}
        account = row.pop("account", None) or {}
        item = {
            **row,
            "account_email": account.get("email"),
            "subscription_status": subscription.get("status"),
            "problem": _lifecycle_problem(row, subscription, now),
        }
        if row.get("lifecycle_stopped_at") and row.get("status") != "deprovisioned":
            pending.append(item)
        if item["problem"]:
            stuck.append(item)
    pending.sort(key=lambda item: item.get("teardown_after") or "")
    return pending, stuck


def _lifecycle_problem(instance: dict[str, Any], subscription: dict[str, Any], now: datetime) -> str | None:
    """Describe why an instance's lifecycle state disagrees with its subscription, or None when it agrees."""
    if instance.get("lifecycle_error"):
        return f"Last lifecycle step failed: {instance['lifecycle_error']}"
    status = instance.get("status")
    if status == "deprovisioned":
        return None
    held = instance.get("lifecycle_stopped_at") is not None
    entitled = bool(subscription) and is_subscription_service_active(subscription, now=now)
    if held and entitled:
        return "Subscription is entitled again but the instance is still held"
    if held and status != "stopped":
        return f"Held for an inactive subscription but status is {status}"
    teardown_after = parse_timestamp(instance.get("teardown_after"))
    if held and teardown_after is not None and teardown_after < now - timedelta(days=1):
        return "Teardown is overdue"
    if not held and not entitled:
        return "Subscription is not entitled but the instance is not scheduled for teardown"
    return None


def _instance_rows(sb: Client, columns: str) -> list[dict[str, Any]]:
    """Return every instance row, paged past PostgREST's row limit."""
    rows: list[dict[str, Any]] = []
    while True:
        page = (
            sb.table("instances")
            .select(columns)
            .order("instance_id")
            .range(len(rows), len(rows) + _PAGE_SIZE - 1)
            .execute()
            .data
            or []
        )
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            return rows


def _subscriptions_with_instances(sb: Client) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Return each subscription that has instances, together with those instances."""
    grouped: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    for row in _instance_rows(sb, f"{LIFECYCLE_INSTANCE_COLUMNS},subscription:subscriptions(*)"):
        subscription = row.pop("subscription", None)
        if subscription:
            grouped.setdefault(str(subscription["id"]), (subscription, []))[1].append(row)
    return list(grouped.values())


async def _resume(
    sb: Client, instance: dict[str, Any], subscription: dict[str, Any], summary: LifecycleSummary
) -> None:
    """Undo a lifecycle hold for an entitled subscription."""
    if instance.get("lifecycle_stopped_at") is None:
        return
    instance_id = instance["instance_id"]
    if instance.get("status") == "deprovisioned" or not await check_deployment_exists(str(instance_id)):
        await provision_instance(
            sb,
            data={
                "subscription_id": subscription["id"],
                "account_id": subscription["account_id"],
                "tier": subscription["tier"],
                "instance_id": instance_id,
            },
            background_tasks=None,
        )
    else:
        await start_instance(instance_id)
    # Reprovisioning may have minted a new key, so re-read the hash before enabling it.
    current = get_instance(sb, instance_id, columns="instance_id,openrouter_key_hash") or {}
    await set_instance_openrouter_key_disabled(current, disabled=False)
    update_instance(sb, instance_id, {"lifecycle_stopped_at": None, "teardown_after": None, **_CLEARED_LIFECYCLE_ERROR})
    summary.instances_resumed += 1
    logger.info("Resumed instance %s for entitled subscription %s", instance_id, subscription["id"])


async def _hold(
    sb: Client,
    instance: dict[str, Any],
    subscription: dict[str, Any],
    now: datetime,
    summary: LifecycleSummary,
) -> None:
    """Keep an instance of an unentitled subscription stopped, and tear it down once its grace period ends."""
    if instance.get("status") == "deprovisioned":
        return
    instance_id = instance["instance_id"]
    if instance.get("lifecycle_stopped_at") is None:
        hold = {
            "lifecycle_stopped_at": now.isoformat(),
            "teardown_after": (now + timedelta(days=INSTANCE_TEARDOWN_GRACE_DAYS)).isoformat(),
        }
        update_instance(sb, instance_id, hold)
        instance = {**instance, **hold}
        summary.instances_stopped += 1
        logger.info("Stopping instance %s: subscription %s is not entitled", instance_id, subscription["id"])

    await _scale_down(instance_id)
    await set_instance_openrouter_key_disabled(instance, disabled=True)
    if instance.get("status") != "stopped" or instance.get("lifecycle_error"):
        update_instance(sb, instance_id, {"status": "stopped", **_CLEARED_LIFECYCLE_ERROR})

    teardown_after = parse_timestamp(instance.get("teardown_after"))
    if teardown_after is not None and teardown_after <= now:
        await _teardown(sb, instance_id, subscription, summary)


async def _teardown(sb: Client, instance_id: Any, subscription: dict[str, Any], summary: LifecycleSummary) -> None:
    """Uninstall an instance whose grace period ended, unless its subscription became entitled meanwhile."""
    fresh = sb.table("subscriptions").select("*").eq("id", subscription["id"]).limit(1).execute().data
    if fresh and is_subscription_service_active(fresh[0]):
        logger.warning(
            "Skipping teardown of instance %s: subscription %s is entitled again", instance_id, fresh[0]["id"]
        )
        return
    await uninstall_instance(instance_id)
    summary.instances_torn_down += 1
    logger.info("Tore down instance %s after its grace period", instance_id)


async def _scale_down(instance_id: Any) -> None:
    """Scale every tenant deployment to zero; a deployment that does not exist is already stopped."""
    for deployment_ref in tenant_stop_deployment_refs(instance_id):
        code, out, err = await run_kubectl(["scale", deployment_ref, "--replicas=0"], namespace=_INSTANCES_NAMESPACE)
        message = (err or out).strip()
        if code != 0 and "notfound" not in message.lower().replace(" ", ""):
            msg = f"kubectl scale failed for {deployment_ref}: {message}"
            raise RuntimeError(msg)


def _record_error(
    sb: Client, instance: dict[str, Any], exc: Exception, now: datetime, summary: LifecycleSummary
) -> None:
    instance_id = instance.get("instance_id")
    message = str(exc) or type(exc).__name__
    logger.error("Instance lifecycle step failed for instance %s: %s", instance_id, message, exc_info=exc)
    summary.errors.append(f"instance {instance_id}: {message}")
    try:
        update_instance(sb, instance_id, {"lifecycle_error": message, "lifecycle_error_at": now.isoformat()})
    except Exception:
        logger.exception("Failed to record lifecycle error for instance %s", instance_id)
