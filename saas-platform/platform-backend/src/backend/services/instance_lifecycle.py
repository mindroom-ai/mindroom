"""Keep hosted instances in line with their subscription.

This module is the single owner of the subscription-driven instance lifecycle.
Stripe webhooks and the nightly cleanup job both call it, so they behave identically:

- Not entitled (including every subscription of an account pending deletion): stop the instance,
  disable its platform-paid OpenRouter key, and schedule teardown.
- Entitled again: start the instance (or reprovision it when it was torn down or its key does not match
  the tier), re-enable the key, and clear the schedule.
- Entitled on a different tier: redeploy a running instance with the tier's key and resources, and revoke
  a larger key from an instance that is not running.
- Teardown due and still not entitled: uninstall everything and mark the instance deprovisioned.

Account deletion also runs through this module: a deletion request cancels Stripe billing and holds the
instances, and the GDPR hard delete uninstalls every instance before the account's rows are deleted.

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
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

import anyio
from backend.config import INSTANCE_TEARDOWN_GRACE_DAYS, logger, stripe
from backend.deps import ensure_supabase
from backend.entitlements import (
    db_subscription_status,
    is_expired_trial,
    is_subscription_service_active,
    parse_timestamp,
)
from backend.k8s import check_deployment_exists, run_kubectl, tenant_stop_deployment_refs
from backend.openrouter import OpenRouterKeyNotFoundError
from backend.services.instances_data import get_instance, update_instance
from backend.services.provisioner_service import (
    CLEARED_OPENROUTER_KEY_METADATA,
    account_may_run_instances,
    openrouter_key_exceeds_plan,
    openrouter_key_matches_plan,
    provision_instance,
    revoke_instance_openrouter_key,
    set_instance_openrouter_key_disabled,
    start_instance,
    uninstall_instance,
)

if TYPE_CHECKING:
    from supabase import Client

LIFECYCLE_INSTANCE_COLUMNS = (
    "instance_id,subscription_id,account_id,status,tier,openrouter_key_hash,openrouter_key_limit_usd,"
    "openrouter_key_limit_reset,lifecycle_stopped_at,teardown_after,lifecycle_error,lifecycle_error_at"
)
_CLEARED_LIFECYCLE_ERROR = {"lifecycle_error": None, "lifecycle_error_at": None}
_PAGE_SIZE = 1000
_STRIPE_REFRESH_ATTEMPTS = 3
_INSTANCES_NAMESPACE = "mindroom-instances"
# Stripe subscriptions in these states no longer bill and cannot be cancelled again.
ENDED_STRIPE_STATUSES = frozenset({"canceled", "incomplete_expired"})
# Stripe subscriptions in these states have no paid period to finish, so an account deletion cancels them at once;
# otherwise a customer could still pay an open first invoice or resume a paused trial during the grace period.
_UNBILLED_STRIPE_STATUSES = frozenset({"incomplete", "paused"})
# Metadata on the Stripe subscriptions an account deletion set to end, so cancelling it resumes only those.
DELETION_BILLING_MARKER = "mindroom_ends_for_account_deletion"
# Teardown cancels every subscription without a refund, so billing changes wait until a deletion is cancelled.
PENDING_DELETION_BILLING_DETAIL = "Cancel the account deletion before changing billing"
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
    subscription_id: str,
    *,
    now: datetime | None = None,
    summary: LifecycleSummary | None = None,
    refresh_from_stripe: bool = False,
) -> LifecycleSummary:
    """Bring every instance of one subscription to the state its entitlement requires.

    The subscription and its instances are read inside the per-subscription lock, so a run never acts on
    a snapshot that a concurrent webhook or nightly run already changed.
    The stored status is refreshed from Stripe before any instance changes, and always when
    `refresh_from_stripe` is set (the nightly run), so a lost or out-of-order webhook cannot win.
    A subscription of an account pending deletion is never entitled, so its instances stop without asking Stripe.
    """
    summary = summary or LifecycleSummary()
    now = now or datetime.now(UTC)
    sb = ensure_supabase()
    async with _subscription_locks[str(subscription_id)]:
        rows = sb.table("subscriptions").select("*").eq("id", subscription_id).limit(1).execute().data
        if not rows:
            return summary
        subscription = rows[0]
        instances = (
            sb.table("instances")
            .select(LIFECYCLE_INSTANCE_COLUMNS)
            .eq("subscription_id", subscription_id)
            .execute()
            .data
            or []
        )
        account_may_run = account_may_run_instances(sb, subscription["account_id"])
        entitled = account_may_run and is_subscription_service_active(subscription, now=now)
        # Looking for legacy soft-deleted deployments costs a Kubernetes call per torn-down instance, so only the
        # nightly run and accounts pending deletion, which a legacy soft delete left running, look for them.
        find_legacy = refresh_from_stripe or not account_may_run
        if account_may_run and (
            refresh_from_stripe
            or any(_needs_change(instance, subscription, entitled=entitled) for instance in instances)
        ):
            refreshed = await _refresh_status_from_stripe(sb, subscription_id)
            if refreshed is None:
                return summary
            subscription = refreshed
            entitled = is_subscription_service_active(subscription, now=now)
        for instance in instances:
            try:
                if find_legacy and await _legacy_soft_deleted_deployment_exists(instance):
                    instance = _adopt_legacy_soft_deleted_instance(sb, instance)  # noqa: PLW2901
                if not entitled:
                    await _hold(sb, instance, subscription, now, summary)
                elif instance.get("lifecycle_stopped_at"):
                    await _resume(sb, instance, subscription, summary)
                else:
                    await _align_plan(sb, instance, subscription)
            except Exception as exc:  # noqa: BLE001
                _record_error(sb, instance, exc, now, summary)

        if is_expired_trial(subscription, now=now):
            sb.table("subscriptions").update({"status": "paused", "updated_at": now.isoformat()}).eq(
                "id", subscription["id"]
            ).execute()
            summary.subscriptions_paused += 1
    summary.subscriptions_checked += 1
    return summary


async def reconcile_account_instances(account_id: str) -> list[str]:
    """Reconcile the instances of one account's subscriptions and return the errors of the steps that failed.

    Used after Stripe webhooks, deletion changes, and customer starts; it never raises, so webhooks can run it as a
    background task after their response. Instance errors are also stored on the instance and retried nightly.
    """
    summary = LifecycleSummary()
    try:
        sb = ensure_supabase()
        subscriptions = sb.table("subscriptions").select("id").eq("account_id", account_id).execute().data or []
        for subscription in subscriptions:
            await reconcile_subscription_instances(subscription["id"], summary=summary)
    except Exception as exc:
        logger.exception("Instance lifecycle reconcile failed for account %s; the nightly job retries", account_id)
        summary.errors.append(f"account {account_id}: {exc}")
    return summary.errors


async def end_account_billing_at_period_end(account_id: str) -> list[str]:
    """Let each Stripe subscription of the account's customer end with its paid period, for an account deletion.

    Subscriptions the customer already set to end keep their schedule, and ones without a paid period (`incomplete`,
    `paused`) are cancelled at once; the ones this sets to end are marked so `resume_account_billing` undoes only
    those, and their ids are returned. Repeating it changes nothing. A Stripe error propagates after this call's
    changes are undone where Stripe allows, so the request can simply be retried. A no-op without a Stripe customer
    or without Stripe.
    """
    if customer_id := _stripe_customer_id(ensure_supabase(), account_id):
        return await anyio.to_thread.run_sync(partial(_end_customer_billing_at_period_end, customer_id))
    return []


async def resume_account_billing(account_id: str) -> None:
    """Undo `end_account_billing_at_period_end` after an account deletion is cancelled; a Stripe error propagates."""
    if customer_id := _stripe_customer_id(ensure_supabase(), account_id):
        await anyio.to_thread.run_sync(partial(_resume_customer_billing, customer_id))


async def resume_subscriptions(subscription_ids: list[str]) -> None:
    """Undo the end that `end_account_billing_at_period_end` set on exactly these subscriptions."""
    for subscription_id in subscription_ids:
        await anyio.to_thread.run_sync(_resume_subscription, subscription_id)


async def tear_down_account(account_id: str) -> None:
    """Cancel billing at once and uninstall every hosted instance of an account being deleted.

    Used by the GDPR cleanup once the grace period ended and by the admin complete deletion.
    Instance rows are the only record tying Helm releases, volumes, Secrets, and OpenRouter keys to their owner,
    so delete the account's rows only after this returns. A failure propagates for the next run to retry, and every
    step tolerates resources that are already gone, including instances a legacy soft delete marked deprovisioned
    without uninstalling them.
    """
    sb = ensure_supabase()
    await _cancel_stripe_subscriptions(sb, account_id)
    for instance in _account_instances(sb, account_id):
        async with _subscription_locks[str(instance["subscription_id"])]:
            await uninstall_instance(instance["instance_id"])


async def delete_auth_user(account_id: str) -> None:
    """Delete the account's Supabase auth user through the admin API, the last step of an account deletion.

    The `accounts` row references its auth user with ON DELETE CASCADE, so that row, and anything still pointing at
    it, goes too. Run it only after teardown and the other application rows: a failure propagates and keeps the
    account row, which is what the next attempt retries from.
    """
    sb = ensure_supabase()
    await anyio.to_thread.run_sync(sb.auth.admin.delete_user, account_id)


async def verified_subscription(sb: Client, subscription_id: str) -> dict[str, Any] | None:
    """Return a subscription row whose stored status was just confirmed with Stripe, or None when it is gone.

    Callers that mint platform-paid resources use this, so a stale stored status never grants them.
    """
    async with _subscription_locks[str(subscription_id)]:
        return await _refresh_status_from_stripe(sb, subscription_id)


async def reconcile_all_subscriptions(*, now: datetime | None = None) -> LifecycleSummary:
    """Reconcile every subscription that owns an instance."""
    summary = LifecycleSummary()
    subscription_ids = dict.fromkeys(
        row["subscription_id"] for row in _instance_rows(ensure_supabase(), "subscription_id")
    )
    for subscription_id in subscription_ids:
        try:
            await reconcile_subscription_instances(subscription_id, now=now, summary=summary, refresh_from_stripe=True)
        except Exception as exc:
            logger.exception("Instance lifecycle reconcile failed for subscription %s", subscription_id)
            summary.errors.append(f"subscription {subscription_id}: {exc}")
    return summary


def _account_may_run(account: dict[str, Any] | None) -> bool:
    """Return whether an account may run instances at all: it exists and is not pending deletion."""
    return account is not None and account.get("deleted_at") is None


def _may_run_instances(
    subscription: dict[str, Any], account: dict[str, Any] | None, *, now: datetime | None = None
) -> bool:
    """Return whether a subscription may run its instances: it is entitled and its account is not pending deletion."""
    return _account_may_run(account) and is_subscription_service_active(subscription, now=now)


def _account_instances(sb: Client, account_id: str) -> list[dict[str, Any]]:
    """Return every instance the account's rows own, by account or by subscription."""
    columns = "instance_id,subscription_id"
    rows = sb.table("instances").select(columns).eq("account_id", account_id).execute().data or []
    subscriptions = sb.table("subscriptions").select("id").eq("account_id", account_id).execute().data or []
    if subscriptions:
        subscription_ids = [subscription["id"] for subscription in subscriptions]
        rows += sb.table("instances").select(columns).in_("subscription_id", subscription_ids).execute().data or []
    return list({str(row["instance_id"]): row for row in rows}.values())


def _stripe_customer_id(sb: Client, account_id: str) -> str | None:
    """Return the account's Stripe customer, or None when it has none or Stripe is not configured."""
    rows = sb.table("accounts").select("stripe_customer_id").eq("id", account_id).limit(1).execute().data
    customer_id = rows[0].get("stripe_customer_id") if rows else None
    return customer_id if customer_id and stripe.api_key else None


async def _cancel_stripe_subscriptions(sb: Client, account_id: str) -> None:
    """Cancel every Stripe subscription of the account's customer that still bills; a no-op without Stripe."""
    if customer_id := _stripe_customer_id(sb, account_id):
        await anyio.to_thread.run_sync(partial(_cancel_customer_subscriptions, customer_id))


def _customer_subscriptions(customer_id: str) -> list[Any]:
    """Return every Stripe subscription of a customer that has not ended."""
    subscriptions = stripe.Subscription.list(customer=customer_id, status="all", limit=100).auto_paging_iter()
    return [subscription for subscription in subscriptions if subscription.status not in ENDED_STRIPE_STATUSES]


def _cancel_customer_subscriptions(customer_id: str) -> None:
    for subscription in _customer_subscriptions(customer_id):
        stripe.Subscription.cancel(subscription.id)
        logger.info("Cancelled Stripe subscription %s of customer %s", subscription.id, customer_id)


def _end_customer_billing_at_period_end(customer_id: str) -> list[str]:
    subscriptions = _customer_subscriptions(customer_id)
    scheduled: list[str] = []
    try:
        for subscription in subscriptions:
            # Stripe sets cancel_at for a subscription already ending, whether at its period end or a chosen date.
            if subscription.status in _UNBILLED_STRIPE_STATUSES or subscription.cancel_at is not None:
                continue
            stripe.Subscription.modify(
                subscription.id, cancel_at_period_end=True, metadata={DELETION_BILLING_MARKER: "true"}
            )
            scheduled.append(subscription.id)
            logger.info("Stripe subscription %s ends at its period end for an account deletion", subscription.id)
        # Cancelling cannot be undone, so it comes after every change that can.
        for subscription in subscriptions:
            if subscription.status in _UNBILLED_STRIPE_STATUSES:
                stripe.Subscription.cancel(subscription.id)
                logger.info("Cancelled unpaid Stripe subscription %s for an account deletion", subscription.id)
    except stripe.StripeError:
        for subscription_id in scheduled:
            try:
                _resume_subscription(subscription_id)
            except stripe.StripeError:
                logger.exception("Could not undo the scheduled end of Stripe subscription %s", subscription_id)
        raise
    return scheduled


def _resume_customer_billing(customer_id: str) -> None:
    for subscription in _customer_subscriptions(customer_id):
        if subscription.metadata.get(DELETION_BILLING_MARKER):
            _resume_subscription(subscription.id)
            logger.info("Resumed Stripe subscription %s after a cancelled account deletion", subscription.id)


def _resume_subscription(subscription_id: str) -> None:
    # An empty metadata value removes the key.
    stripe.Subscription.modify(subscription_id, cancel_at_period_end=False, metadata={DELETION_BILLING_MARKER: ""})


async def _refresh_status_from_stripe(sb: Client, subscription_id: str) -> dict[str, Any] | None:
    """Return the subscription row with its status refreshed from Stripe, storing any correction.

    Subscriptions without a Stripe id (platform trials, free tier) and deployments without Stripe keep the
    stored status. Any Stripe API error propagates so no instance is stopped on an unverified status.
    A webhook can change the row while Stripe is queried, so the correction is only written while the row is
    unchanged since it was read (same Stripe binding, status, and updated_at); otherwise the refresh restarts.
    """
    for _ in range(_STRIPE_REFRESH_ATTEMPTS):
        rows = sb.table("subscriptions").select("*").eq("id", subscription_id).limit(1).execute().data
        if not rows:
            return None
        subscription = rows[0]
        stripe_subscription_id = subscription.get("stripe_subscription_id")
        if not stripe_subscription_id or not stripe.api_key:
            return subscription
        remote = await anyio.to_thread.run_sync(stripe.Subscription.retrieve, stripe_subscription_id)
        trial_end = remote.get("trial_end")
        fields = {
            "status": db_subscription_status(str(remote["status"])),
            "trial_ends_at": datetime.fromtimestamp(trial_end, tz=UTC).isoformat() if trial_end else None,
        }
        unchanged = subscription.get("status") == fields["status"] and parse_timestamp(
            subscription.get("trial_ends_at")
        ) == parse_timestamp(fields["trial_ends_at"])
        query = sb.table("subscriptions")
        query = (
            query.select("*") if unchanged else query.update({**fields, "updated_at": datetime.now(UTC).isoformat()})
        )
        current = (
            query.eq("id", subscription_id)
            .eq("stripe_subscription_id", stripe_subscription_id)
            .eq("status", subscription.get("status"))
            .eq("updated_at", subscription.get("updated_at"))
            .execute()
            .data
        )
        if current:
            if not unchanged:
                logger.warning(
                    "Subscription %s was stored as %s but Stripe reports %s; corrected it",
                    subscription_id,
                    subscription.get("status"),
                    fields["status"],
                )
            return current[0]
        logger.info("Subscription %s changed while Stripe was queried; refreshing again", subscription_id)
    msg = f"Subscription {subscription_id} kept changing while its Stripe status was refreshed"
    raise RuntimeError(msg)


def lifecycle_overview(*, now: datetime | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return instances pending teardown and instances whose lifecycle looks stuck, for the admin portal.

    Each row carries the instance lifecycle columns, `account_email`, `subscription_status`, and `problem`.
    """
    now = now or datetime.now(UTC)
    rows = _instance_rows(
        ensure_supabase(),
        f"{LIFECYCLE_INSTANCE_COLUMNS},subscription:subscriptions(*),account:accounts(email,deleted_at)",
    )
    pending: list[dict[str, Any]] = []
    stuck: list[dict[str, Any]] = []
    for row in rows:
        subscription = row.pop("subscription", None) or {}
        account = row.pop("account", None)
        item = {
            **row,
            "account_email": (account or {}).get("email"),
            "subscription_status": subscription.get("status"),
            "problem": _lifecycle_problem(row, subscription, account, now),
        }
        if row.get("lifecycle_stopped_at") and row.get("status") != "deprovisioned":
            pending.append(item)
        if item["problem"]:
            stuck.append(item)
    pending.sort(key=lambda item: item.get("teardown_after") or "")
    return pending, stuck


def _lifecycle_problem(
    instance: dict[str, Any], subscription: dict[str, Any], account: dict[str, Any] | None, now: datetime
) -> str | None:
    """Describe why an instance's lifecycle state disagrees with its subscription, or None when it agrees."""
    if instance.get("lifecycle_error"):
        return f"Last lifecycle step failed: {instance['lifecycle_error']}"
    status = instance.get("status")
    if status == "deprovisioned":
        return None
    held = instance.get("lifecycle_stopped_at") is not None
    entitled = bool(subscription) and _may_run_instances(subscription, account, now=now)
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
        if not page:
            return rows
        rows.extend(page)


async def _resume(
    sb: Client, instance: dict[str, Any], subscription: dict[str, Any], summary: LifecycleSummary
) -> None:
    """Undo a lifecycle hold for an entitled subscription."""
    instance_id = instance["instance_id"]
    # Only provisioning applies another tier's resources and mints its key or drops one it does not include (for
    # example after a downgrade), and a key lost in an earlier failed attempt is missing too; re-enabling the
    # stored key would hand it back.
    plan_mismatch = not _deployed_plan_matches(instance, subscription["tier"])
    # After any failed resume or provision, only a full reprovision republishes the key and deployment.
    failed_before = bool(instance.get("lifecycle_error")) or instance.get("status") == "error"
    if (
        instance.get("status") == "deprovisioned"
        or plan_mismatch
        or failed_before
        or not await check_deployment_exists(str(instance_id))
    ):
        await _reprovision(sb, instance_id, subscription)
    else:
        await start_instance(instance_id)
    # Reprovisioning may have minted a new key, so re-read the hash before enabling it.
    current = get_instance(sb, instance_id, columns="instance_id,openrouter_key_hash") or {}
    try:
        await set_instance_openrouter_key_disabled(current, disabled=False)
    except OpenRouterKeyNotFoundError:
        # The key is gone on OpenRouter; forget it so reprovisioning mints and mounts a new one.
        update_instance(sb, instance_id, CLEARED_OPENROUTER_KEY_METADATA)
        await _reprovision(sb, instance_id, subscription)
    update_instance(sb, instance_id, {"lifecycle_stopped_at": None, "teardown_after": None, **_CLEARED_LIFECYCLE_ERROR})
    summary.instances_resumed += 1
    logger.info("Resumed instance %s for entitled subscription %s", instance_id, subscription["id"])


def _deployed_plan_matches(instance: dict[str, Any], tier: str) -> bool:
    """Return whether an instance was last deployed for the tier, with exactly the platform-paid key it includes."""
    return instance.get("tier") == tier and openrouter_key_matches_plan(instance, tier)


def _plan_alignment(instance: dict[str, Any], tier: str) -> Literal["redeploy", "revoke"] | None:
    """Return how an instance the lifecycle does not hold must change to run only what its tier pays for."""
    status = instance.get("status")
    if status == "error" and instance.get("lifecycle_error"):
        # A redeploy by this function failed, and provisioning marked the instance errored; retry it.
        return "redeploy"
    if _deployed_plan_matches(instance, tier):
        return None
    if status == "running":
        # Provisioning applies the tier's resources and replaces or deletes the key.
        return "redeploy"
    # Any other instance is not redeployed, which would start a customer-stopped one or race a provision; it only
    # loses a key its tier does not pay for, and the rest follows once it runs again.
    return "revoke" if openrouter_key_exceeds_plan(instance, tier) else None


def _needs_change(instance: dict[str, Any], subscription: dict[str, Any], *, entitled: bool) -> bool:
    if not entitled:
        return instance.get("status") != "deprovisioned"
    if instance.get("lifecycle_stopped_at"):
        return True
    return _plan_alignment(instance, subscription["tier"]) is not None


async def _align_plan(sb: Client, instance: dict[str, Any], subscription: dict[str, Any]) -> None:
    """Keep an entitled instance the lifecycle does not hold on what its subscription tier pays for."""
    instance_id = instance["instance_id"]
    alignment = _plan_alignment(instance, subscription["tier"])
    if alignment == "redeploy":
        logger.info("Redeploying instance %s for the %s tier of its subscription", instance_id, subscription["tier"])
        await _reprovision(sb, instance_id, subscription)
    elif alignment == "revoke":
        logger.info("Revoking the OpenRouter key of instance %s, which its tier does not include", instance_id)
        await revoke_instance_openrouter_key(sb, instance_id)
    # The instance now carries only what its tier pays for, so an earlier failed step is resolved.
    if instance.get("lifecycle_error"):
        update_instance(sb, instance_id, _CLEARED_LIFECYCLE_ERROR)


async def _reprovision(sb: Client, instance_id: Any, subscription: dict[str, Any]) -> None:
    await provision_instance(
        sb,
        data={
            "subscription_id": subscription["id"],
            "account_id": subscription["account_id"],
            "tier": subscription["tier"],
            "instance_id": instance_id,
        },
        background_tasks=None,
        resume_lifecycle_hold=True,
    )


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


# LEGACY_COMPAT: Instances a soft delete marked deprovisioned while their deployment kept running.
# Legacy format: an `instances` row with status `deprovisioned`, no `lifecycle_stopped_at`, and a live deployment,
#   written by `soft_delete_account` for every running instance of an account whose deletion was requested.
# Last legacy release: every release before the one that ships migration 005, whose soft delete changes only the
#   account while this module holds the instances.
# Handling: the nightly run, and any run for an account pending deletion, marks such a row running again, since
#   its deployment still runs; the lifecycle then holds it, or keeps it running for an entitled subscription.
#   A deprovisioned row without a deployment is already torn down and is left alone.
# Coverage: saas-platform/platform-backend/tests/test_instance_lifecycle.py::test_legacy_soft_deleted_instance_that_kept_running_is_held,
#   saas-platform/platform-backend/tests/test_instance_lifecycle.py::test_legacy_soft_deleted_instance_of_a_paying_restored_account_runs
async def _legacy_soft_deleted_deployment_exists(instance: dict[str, Any]) -> bool:
    if instance.get("status") != "deprovisioned" or instance.get("lifecycle_stopped_at") is not None:
        return False  # A lifecycle teardown sets the hold first.
    return await check_deployment_exists(str(instance["instance_id"]))


def _adopt_legacy_soft_deleted_instance(sb: Client, instance: dict[str, Any]) -> dict[str, Any]:
    logger.warning("Instance %s was marked deprovisioned but still runs; marking it running", instance["instance_id"])
    update_instance(sb, instance["instance_id"], {"status": "running"})
    return {**instance, "status": "running"}


async def _teardown(sb: Client, instance_id: Any, subscription: dict[str, Any], summary: LifecycleSummary) -> None:
    """Uninstall an instance whose grace period ended, unless its subscription became entitled meanwhile."""
    fresh = sb.table("subscriptions").select("*").eq("id", subscription["id"]).limit(1).execute().data
    if fresh and account_may_run_instances(sb, fresh[0]["account_id"]) and is_subscription_service_active(fresh[0]):
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
