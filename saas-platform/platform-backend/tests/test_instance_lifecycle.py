"""Hosted instances follow their subscription: stop, restart, and tear down."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import pytest
import stripe
from backend.deps import verify_admin, verify_user, verify_user_allow_deleted
from backend.openrouter import CreatedOpenRouterKey, OpenRouterKeyNotFoundError
from backend.pricing import get_plan_details
from backend.services.instance_lifecycle import (
    DELETION_BILLING_MARKER,
    LifecycleSummary,
    lifecycle_overview,
    reconcile_all_subscriptions,
    reconcile_subscription_instances,
)
from backend.services.provisioner_service import provision_instance, set_instance_openrouter_key_disabled
from backend.tasks.cleanup import run_cleanup_job
from fastapi import HTTPException
from fastapi.testclient import TestClient
from main import app
from supabase import PostgrestAPIError

from tests.fake_supabase import FakeSupabase

ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"
SUBSCRIPTION_ID = "11111111-1111-1111-1111-111111111111"
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "supabase/migrations"
LEGACY = "backend.services.legacy_instance_lifecycle"
PERIOD_END = 1_790_000_000


@dataclass
class Platform:
    """Fake database plus the Kubernetes, provisioning, and OpenRouter side effects of the lifecycle."""

    db: FakeSupabase
    kubectl: AsyncMock
    start: AsyncMock
    provision: AsyncMock
    uninstall: AsyncMock
    set_key_disabled: AsyncMock
    revoke_key: AsyncMock
    stripe: Mock

    def instance(self) -> dict[str, Any]:
        return self.db.row("instances", instance_id=7)

    def subscription(self) -> dict[str, Any]:
        return self.db.row("subscriptions", id=SUBSCRIPTION_ID)

    def scaled_down(self) -> bool:
        return call(["scale", "deployment/mindroom-7", "--replicas=0"], namespace="mindroom-instances") in (
            self.kubectl.await_args_list
        )


def _subscription(status: str, **fields: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "id": SUBSCRIPTION_ID,
        "account_id": ACCOUNT_ID,
        "stripe_subscription_id": "sub_stripe_1",
        "tier": "hobby",
        "status": status,
        "trial_ends_at": None,
        "updated_at": "2026-09-01T00:00:00+00:00",
        **fields,
    }


def _instance(status: str, **fields: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "id": "instance-row-7",
        "instance_id": 7,
        "subscription_id": SUBSCRIPTION_ID,
        "account_id": ACCOUNT_ID,
        "status": status,
        "tier": "hobby",
        "openrouter_key_hash": "key_hash_7",
        "openrouter_key_limit_usd": get_plan_details("hobby").included_ai_budget_usd,
        "openrouter_key_limit_reset": "monthly",
        "lifecycle_stopped_at": None,
        "teardown_after": None,
        "lifecycle_error": None,
        "lifecycle_error_at": None,
        **fields,
    }


@pytest.fixture
def platform() -> Iterator[Platform]:
    db = FakeSupabase(
        {
            "accounts": [{"id": ACCOUNT_ID, "email": "customer@example.com", "stripe_customer_id": "cus_1"}],
            "subscriptions": [],
            "instances": [],
            "webhook_events": [],
            "cleanup_runs": [],
        }
    )

    def mark(status: str) -> AsyncMock:
        async def side_effect(instance_id: Any, *_args: Any, **_kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
            db.row("instances", instance_id=instance_id)["status"] = status
            return {"success": True, "message": status}

        return AsyncMock(side_effect=side_effect)

    async def provision(
        _sb: Any,  # noqa: ANN401
        *,
        data: dict[str, Any],
        background_tasks: Any,  # noqa: ANN401, ARG001
        resume_lifecycle_hold: bool,  # noqa: ARG001
    ) -> dict[str, Any]:
        # Like provision_instance, a successful deploy records the tier it deployed.
        db.row("instances", instance_id=data["instance_id"]).update(
            {"status": "running", "tier": data["tier"], "openrouter_key_hash": "key_hash_new"}
        )
        return {"success": True}

    lifecycle = "backend.services.instance_lifecycle"
    platform = Platform(
        db=db,
        kubectl=AsyncMock(return_value=(0, "scaled", "")),
        start=mark("running"),
        provision=AsyncMock(side_effect=provision),
        uninstall=mark("deprovisioned"),
        set_key_disabled=AsyncMock(),
        revoke_key=AsyncMock(),
        stripe=Mock(api_key=""),
    )
    with (
        patch(f"{lifecycle}.ensure_supabase", return_value=db),
        patch("backend.routes.webhooks.ensure_supabase", return_value=db),
        patch("backend.tasks.cleanup.ensure_supabase", return_value=db),
        patch(f"{lifecycle}.run_kubectl", platform.kubectl),
        patch(f"{lifecycle}.check_deployment_exists", AsyncMock(return_value=True)),
        patch(f"{LEGACY}.check_deployment_exists", AsyncMock(return_value=True)),
        patch(f"{lifecycle}.start_instance", platform.start),
        patch(f"{lifecycle}.provision_instance", platform.provision),
        patch(f"{lifecycle}.uninstall_instance", platform.uninstall),
        patch(f"{lifecycle}.set_instance_openrouter_key_disabled", platform.set_key_disabled),
        patch(f"{lifecycle}.revoke_instance_openrouter_key", platform.revoke_key),
        patch(f"{lifecycle}.stripe", platform.stripe),
        patch("backend.routes.webhooks.STRIPE_WEBHOOK_SECRET", "whsec_test"),
    ):
        yield platform


def _send_webhook(event_type: str, obj: dict[str, Any]) -> dict[str, Any]:
    event = Mock(id=f"evt_{event_type}", type=event_type)
    event.data.object = obj
    with patch("backend.routes.webhooks.stripe.Webhook.construct_event", return_value=event):
        response = TestClient(app).post("/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "sig"})
    assert response.status_code == 200
    return response.json()


def _stripe_subscription(status: str, tier: str = "hobby") -> dict[str, Any]:
    return {
        "id": "sub_stripe_1",
        "customer": "cus_1",
        "status": status,
        "items": {"data": [{"price": {"id": f"price_{tier}", "metadata": {"tier": tier, "billing_cycle": "monthly"}}}]},
        "trial_end": None,
        "canceled_at": None,
    }


def _pro_key() -> dict[str, Any]:
    """Stored metadata of the platform-paid key a pro instance was provisioned with."""
    return {
        "openrouter_key_hash": "key_hash_pro",
        "openrouter_key_limit_usd": get_plan_details("pro").included_ai_budget_usd,
        "openrouter_key_limit_reset": "monthly",
    }


def _held(now: datetime) -> dict[str, Any]:
    return {
        "lifecycle_stopped_at": (now - timedelta(days=2)).isoformat(),
        "teardown_after": (now + timedelta(days=28)).isoformat(),
    }


def _pending_deletion(platform: Platform, *, days_ago: float = 1) -> None:
    deleted_at = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    platform.db.row("accounts", id=ACCOUNT_ID).update({"deleted_at": deleted_at, "status": "deleted"})


def _stripe_lists(platform: Platform, *subscriptions: Mock) -> None:
    platform.stripe.api_key = "sk_test"
    platform.stripe.Subscription.list.return_value.auto_paging_iter.return_value = list(subscriptions)


def _stripe_sub(
    stripe_id: str, status: str, *, ends: bool = False, ends_on: int | None = None, marked: bool = False
) -> MagicMock:
    """A Stripe subscription as the list API returns it, optionally already set to end with its period or on a date.

    Its paid period ends at PERIOD_END. Stripe sets `cancel_at` in both cases, and `cancel_at_period_end` only in the
    first.
    """
    metadata = {DELETION_BILLING_MARKER: "none"} if marked else {}
    cancel_at = PERIOD_END if ends else ends_on
    subscription = MagicMock(id=stripe_id, status=status, cancel_at_period_end=ends, cancel_at=cancel_at)
    subscription.metadata = metadata
    subscription.__getitem__.side_effect = {"items": {"data": [{"current_period_end": PERIOD_END}]}}.__getitem__
    return subscription


def _claimable(platform: Platform) -> None:
    """Let the nightly cleanup claim the pending account, as claim_account_hard_delete does after the grace period."""
    platform.db.rpc_results["claim_account_hard_delete"] = True


def test_webhook_cancel_stops_instance_disables_key_and_schedules_teardown(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].append(_instance("running"))

    _send_webhook("customer.subscription.deleted", {"id": "sub_stripe_1"})

    instance = platform.instance()
    assert platform.subscription()["status"] == "cancelled"
    assert instance["status"] == "stopped"
    assert platform.scaled_down()
    platform.set_key_disabled.assert_awaited_once()
    assert platform.set_key_disabled.await_args.kwargs == {"disabled": True}
    teardown_after = datetime.fromisoformat(instance["teardown_after"])
    assert teardown_after - datetime.fromisoformat(instance["lifecycle_stopped_at"]) == timedelta(days=30)
    platform.uninstall.assert_not_awaited()


def test_stripe_canceled_status_is_stored_as_cancelled_and_stops_instance(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].append(_instance("running"))

    _send_webhook("customer.subscription.updated", _stripe_subscription("canceled"))

    assert platform.subscription()["status"] == "cancelled"
    assert platform.instance()["status"] == "stopped"


def test_webhook_resubscribe_starts_instance_and_reenables_key(platform: Platform) -> None:
    now = datetime.now(UTC)
    platform.db.tables["subscriptions"].append(_subscription("cancelled"))
    platform.db.tables["instances"].append(
        _instance(
            "stopped",
            lifecycle_stopped_at=(now - timedelta(days=3)).isoformat(),
            teardown_after=(now + timedelta(days=27)).isoformat(),
        )
    )

    _send_webhook("customer.subscription.created", _stripe_subscription("active"))

    instance = platform.instance()
    platform.start.assert_awaited_once_with(7)
    assert instance["status"] == "running"
    assert platform.set_key_disabled.await_args.kwargs == {"disabled": False}
    assert instance["lifecycle_stopped_at"] is None
    assert instance["teardown_after"] is None


@pytest.mark.asyncio
async def test_resubscribe_after_teardown_reprovisions_instance(platform: Platform) -> None:
    now = datetime.now(UTC)
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].append(
        _instance(
            "deprovisioned",
            openrouter_key_hash=None,
            lifecycle_stopped_at=(now - timedelta(days=40)).isoformat(),
            teardown_after=(now - timedelta(days=10)).isoformat(),
        )
    )

    await reconcile_subscription_instances(SUBSCRIPTION_ID)

    platform.provision.assert_awaited_once()
    assert platform.provision.await_args.kwargs["data"] == {
        "subscription_id": SUBSCRIPTION_ID,
        "account_id": ACCOUNT_ID,
        "tier": "hobby",
        "instance_id": 7,
    }
    assert platform.instance()["status"] == "running"
    assert platform.instance()["lifecycle_stopped_at"] is None


def test_manually_stopped_instance_of_entitled_subscription_stays_stopped(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].append(_instance("stopped"))

    _send_webhook("customer.subscription.updated", _stripe_subscription("active"))

    platform.start.assert_not_awaited()
    assert platform.instance()["status"] == "stopped"


def test_webhook_succeeds_when_kubernetes_is_down_and_records_error(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].append(_instance("running"))
    platform.kubectl.return_value = (1, "", "Unable to connect to the server")

    body = _send_webhook("customer.subscription.deleted", {"id": "sub_stripe_1"})

    instance = platform.instance()
    assert body == {"received": True, "error": None}
    assert instance["status"] == "running"
    assert "Unable to connect to the server" in instance["lifecycle_error"]
    assert instance["teardown_after"] is not None


@pytest.mark.asyncio
async def test_nightly_job_catches_missed_cancellation_and_records_run(platform: Platform) -> None:
    # The DB says cancelled, but no webhook stopped the instance, and its status was left as error.
    platform.db.tables["subscriptions"].append(_subscription("cancelled"))
    platform.db.tables["instances"].append(_instance("error"))

    with (
        patch(
            "backend.tasks.cleanup.cleanup_soft_deleted_accounts", return_value={"accounts_deleted": 0, "errors": []}
        ),
        patch("backend.tasks.cleanup.cleanup_old_audit_logs", return_value={"audit_logs_deleted": 0}),
        patch("backend.tasks.cleanup.cleanup_old_usage_metrics", return_value={"usage_metrics_deleted": 0}),
    ):
        run = await run_cleanup_job()

    assert run["ok"] is True
    assert platform.scaled_down()
    assert platform.instance()["status"] == "stopped"
    assert platform.instance()["teardown_after"] is not None
    assert run["summary"]["instance_lifecycle"]["instances_stopped"] == 1
    assert platform.db.tables["cleanup_runs"] == [{**run, "id": platform.db.tables["cleanup_runs"][0]["id"]}]


@pytest.mark.asyncio
async def test_past_due_subscription_keeps_instance_running(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("past_due"))
    platform.db.tables["instances"].append(_instance("running"))

    summary = await reconcile_subscription_instances(SUBSCRIPTION_ID)

    platform.kubectl.assert_not_awaited()
    platform.set_key_disabled.assert_not_awaited()
    assert platform.instance()["status"] == "running"
    assert platform.instance()["teardown_after"] is None
    assert summary.errors == []


def test_payment_failure_does_not_make_incomplete_subscription_past_due(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("incomplete"))

    _send_webhook(
        "invoice.payment_failed",
        {"id": "in_1", "customer": "cus_1", "parent": {"subscription_details": {"subscription": "sub_stripe_1"}}},
    )

    assert platform.subscription()["status"] == "incomplete"


@pytest.mark.asyncio
async def test_teardown_waits_for_grace_period(platform: Platform) -> None:
    now = datetime.now(UTC)
    platform.db.tables["subscriptions"].append(_subscription("cancelled"))
    platform.db.tables["instances"].append(
        _instance(
            "stopped",
            lifecycle_stopped_at=(now - timedelta(days=29)).isoformat(),
            teardown_after=(now + timedelta(days=1)).isoformat(),
        )
    )

    await reconcile_subscription_instances(SUBSCRIPTION_ID, now=now)
    platform.uninstall.assert_not_awaited()

    summary = await reconcile_subscription_instances(SUBSCRIPTION_ID, now=now + timedelta(days=1, minutes=1))
    platform.uninstall.assert_awaited_once_with(7)
    assert platform.instance()["status"] == "deprovisioned"
    assert summary.instances_torn_down == 1


@pytest.mark.asyncio
async def test_teardown_skipped_when_subscription_became_entitled(platform: Platform) -> None:
    now = datetime.now(UTC)
    platform.db.tables["subscriptions"].append(_subscription("cancelled"))
    platform.db.tables["instances"].append(
        _instance(
            "stopped",
            lifecycle_stopped_at=(now - timedelta(days=31)).isoformat(),
            teardown_after=(now - timedelta(days=1)).isoformat(),
        )
    )

    async def resubscribe_while_stopping(*_args: Any, **_kwargs: Any) -> tuple[int, str, str]:  # noqa: ANN401
        platform.subscription()["status"] = "active"
        return 0, "scaled", ""

    platform.kubectl.side_effect = resubscribe_while_stopping

    await reconcile_subscription_instances(SUBSCRIPTION_ID, now=now)

    platform.uninstall.assert_not_awaited()
    assert platform.instance()["status"] == "stopped"


@pytest.mark.asyncio
async def test_one_failing_cleanup_task_does_not_skip_the_others(platform: Platform) -> None:
    audit_logs = Mock(return_value={"audit_logs_deleted": 3})
    usage_metrics = Mock(return_value={"usage_metrics_deleted": 4})
    reconcile = AsyncMock(return_value=LifecycleSummary())
    with (
        patch("backend.tasks.cleanup.cleanup_soft_deleted_accounts", side_effect=RuntimeError("rpc denied")),
        patch("backend.tasks.cleanup.cleanup_old_audit_logs", audit_logs),
        patch("backend.tasks.cleanup.cleanup_old_usage_metrics", usage_metrics),
        patch("backend.tasks.cleanup.reconcile_all_subscriptions", reconcile),
    ):
        run = await run_cleanup_job()

    audit_logs.assert_called_once()
    usage_metrics.assert_called_once()
    reconcile.assert_awaited_once()
    assert run["ok"] is False
    assert run["summary"]["accounts"] == {"error": "rpc denied"}
    assert platform.db.tables["cleanup_runs"][0]["ok"] is False


def test_provision_route_restarts_held_instance(platform: Platform) -> None:
    now = datetime.now(UTC)
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].append(
        _instance(
            "stopped",
            lifecycle_stopped_at=(now - timedelta(days=2)).isoformat(),
            teardown_after=(now + timedelta(days=28)).isoformat(),
        )
    )
    app.dependency_overrides[verify_user] = lambda: {"account_id": ACCOUNT_ID, "email": "customer@example.com"}
    try:
        with patch("backend.routes.instances.ensure_supabase", return_value=platform.db):
            response = TestClient(app).post("/my/instances/provision")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["message"] == "Instance restarted"
    platform.start.assert_awaited_once_with(7)


def test_admin_lifecycle_overview_reports_last_run_pending_and_stuck(platform: Platform) -> None:
    now = datetime.now(UTC)
    platform.db.tables["subscriptions"].extend(
        [_subscription("cancelled"), _subscription("cancelled", id="sub-2", account_id=ACCOUNT_ID)]
    )
    platform.db.tables["instances"].extend(
        [
            _instance(
                "stopped",
                lifecycle_stopped_at=(now - timedelta(days=5)).isoformat(),
                teardown_after=(now + timedelta(days=25)).isoformat(),
            ),
            _instance("running", id="instance-row-8", instance_id=8, subscription_id="sub-2"),
        ]
    )
    platform.db.tables["cleanup_runs"].append(
        {"id": "run-1", "started_at": now.isoformat(), "finished_at": now.isoformat(), "ok": True, "summary": {}}
    )
    app.dependency_overrides[verify_admin] = lambda: {"user_id": "admin", "email": "admin@example.com"}
    try:
        with patch("backend.routes.admin.ensure_supabase", return_value=platform.db):
            response = TestClient(app).get("/admin/instance-lifecycle")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["teardown_grace_days"] == 30
    assert body["last_run"]["ok"] is True
    assert [row["instance_id"] for row in body["pending_teardown"]] == [7]
    assert body["pending_teardown"][0]["account_email"] == "customer@example.com"
    assert [(row["instance_id"], row["problem"]) for row in body["stuck"]] == [
        (8, "Subscription is not entitled but the instance is not scheduled for teardown")
    ]


@pytest.mark.asyncio
async def test_disabling_a_missing_key_succeeds_but_reenabling_it_raises() -> None:
    missing = Mock(side_effect=OpenRouterKeyNotFoundError("status 404"))
    with patch("backend.services.provisioner_service.set_openrouter_key_disabled", missing):
        await set_instance_openrouter_key_disabled({"instance_id": 7, "openrouter_key_hash": "gone"}, disabled=True)
        with pytest.raises(OpenRouterKeyNotFoundError):
            await set_instance_openrouter_key_disabled(
                {"instance_id": 7, "openrouter_key_hash": "gone"}, disabled=False
            )
        await set_instance_openrouter_key_disabled({"instance_id": 7, "openrouter_key_hash": None}, disabled=False)

    assert missing.call_count == 2


@pytest.mark.asyncio
async def test_stale_stored_status_is_corrected_from_stripe_instead_of_stopping(platform: Platform) -> None:
    # A late subscription.created (incomplete) overwrote the row after the customer paid again.
    platform.db.tables["subscriptions"].append(_subscription("incomplete"))
    platform.db.tables["instances"].append(_instance("running"))
    platform.stripe.api_key = "sk_test"
    platform.stripe.Subscription.retrieve.return_value = {"status": "active", "trial_end": None}

    summary = await reconcile_subscription_instances(SUBSCRIPTION_ID)

    platform.stripe.Subscription.retrieve.assert_called_once_with("sub_stripe_1")
    assert platform.subscription()["status"] == "active"
    platform.kubectl.assert_not_awaited()
    assert platform.instance()["teardown_after"] is None
    assert summary.errors == []


@pytest.mark.asyncio
async def test_resubscription_during_stripe_refresh_is_not_overwritten(platform: Platform) -> None:
    now = datetime.now(UTC)
    # Stored as active, so the old subscription's "canceled" from Stripe would be written as a correction.
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].append(
        _instance(
            "stopped",
            lifecycle_stopped_at=(now - timedelta(days=31)).isoformat(),
            teardown_after=(now - timedelta(days=1)).isoformat(),
        )
    )
    platform.stripe.api_key = "sk_test"

    def retrieve(stripe_subscription_id: str) -> dict[str, Any]:
        if stripe_subscription_id == "sub_stripe_1":
            # The resubscription webhook rebinds the row while the old subscription is being fetched.
            platform.subscription().update({"stripe_subscription_id": "sub_stripe_2", "status": "active"})
            return {"status": "canceled", "trial_end": None}
        return {"status": "active", "trial_end": None}

    platform.stripe.Subscription.retrieve.side_effect = retrieve

    await reconcile_subscription_instances(SUBSCRIPTION_ID, now=now)

    assert platform.subscription()["status"] == "active"
    assert platform.subscription()["stripe_subscription_id"] == "sub_stripe_2"
    platform.uninstall.assert_not_awaited()
    platform.start.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_payment_recovery_during_stripe_refresh_is_not_overwritten(platform: Platform) -> None:
    now = datetime.now(UTC)
    platform.db.tables["subscriptions"].append(_subscription("cancelled"))
    platform.db.tables["instances"].append(
        _instance(
            "stopped",
            lifecycle_stopped_at=(now - timedelta(days=31)).isoformat(),
            teardown_after=(now - timedelta(days=1)).isoformat(),
        )
    )
    platform.stripe.api_key = "sk_test"
    responses = iter([{"status": "unpaid", "trial_end": None}, {"status": "active", "trial_end": None}])

    def retrieve(_stripe_subscription_id: str) -> dict[str, Any]:
        response = next(responses)
        if response["status"] == "unpaid":
            # A newer webhook for the same Stripe subscription lands while the older state is fetched.
            platform.subscription().update({"status": "active", "updated_at": "2026-09-02T00:00:00+00:00"})
        return response

    platform.stripe.Subscription.retrieve.side_effect = retrieve

    await reconcile_subscription_instances(SUBSCRIPTION_ID, now=now)

    assert platform.subscription()["status"] == "active"
    platform.uninstall.assert_not_awaited()
    platform.start.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_nightly_run_stops_instance_restarted_by_a_stale_active_event(platform: Platform) -> None:
    # A delayed "active" update after the cancellation resumed the instance and left the row active.
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].append(_instance("running"))
    platform.stripe.api_key = "sk_test"
    platform.stripe.Subscription.retrieve.return_value = {"status": "canceled", "trial_end": None}

    summary = await reconcile_all_subscriptions()

    assert platform.subscription()["status"] == "cancelled"
    assert platform.scaled_down()
    assert platform.instance()["status"] == "stopped"
    assert summary.instances_stopped == 1


@pytest.mark.asyncio
async def test_nightly_run_reprovisions_torn_down_instance_after_a_missed_payment_update(platform: Platform) -> None:
    now = datetime.now(UTC)
    platform.db.tables["subscriptions"].append(_subscription("unpaid"))
    platform.db.tables["instances"].append(
        _instance(
            "deprovisioned",
            openrouter_key_hash=None,
            lifecycle_stopped_at=(now - timedelta(days=40)).isoformat(),
            teardown_after=(now - timedelta(days=10)).isoformat(),
        )
    )
    platform.stripe.api_key = "sk_test"
    platform.stripe.Subscription.retrieve.return_value = {"status": "active", "trial_end": None}

    await reconcile_all_subscriptions()

    assert platform.subscription()["status"] == "active"
    platform.provision.assert_awaited_once()
    assert platform.instance()["status"] == "running"


@pytest.mark.asyncio
async def test_held_instance_is_not_resumed_on_a_stale_active_status(platform: Platform) -> None:
    now = datetime.now(UTC)
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].append(
        _instance(
            "stopped",
            lifecycle_stopped_at=(now - timedelta(days=2)).isoformat(),
            teardown_after=(now + timedelta(days=28)).isoformat(),
        )
    )
    platform.stripe.api_key = "sk_test"
    platform.stripe.Subscription.retrieve.return_value = {"status": "canceled", "trial_end": None}

    await reconcile_subscription_instances(SUBSCRIPTION_ID)

    platform.start.assert_not_awaited()
    assert platform.subscription()["status"] == "cancelled"
    assert platform.instance()["status"] == "stopped"


def test_failed_binding_lookup_asks_stripe_to_redeliver(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("cancelled", stripe_subscription_id="sub_stripe_old"))
    newer = {**_stripe_subscription("active"), "created": 1_750_000_000}
    event = Mock(id="evt_created", type="customer.subscription.created")
    event.data.object = newer
    client = TestClient(app)

    with (
        patch("backend.routes.webhooks.stripe.Webhook.construct_event", return_value=event),
        patch("backend.routes.webhooks.stripe.Subscription.retrieve", side_effect=RuntimeError("stripe 503")),
    ):
        failed = client.post("/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "sig"})

    assert failed.status_code == 500
    assert platform.subscription()["stripe_subscription_id"] == "sub_stripe_old"
    assert platform.db.tables["webhook_events"] == []

    with (
        patch("backend.routes.webhooks.stripe.Webhook.construct_event", return_value=event),
        patch("backend.routes.webhooks.stripe.Subscription.retrieve", return_value={"created": 1_700_000_000}),
    ):
        retried = client.post("/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "sig"})

    assert retried.status_code == 200
    assert platform.subscription()["stripe_subscription_id"] == "sub_stripe_1"
    assert platform.subscription()["status"] == "active"


def test_transient_db_failure_in_created_handler_is_redelivered(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("cancelled", stripe_subscription_id="sub_stripe_old"))
    newer = {**_stripe_subscription("active"), "created": 1_750_000_000}
    event = Mock(id="evt_created", type="customer.subscription.created")
    event.data.object = newer
    client = TestClient(app)
    real_table = platform.db.table

    def failing_subscription_writes(name: str) -> Any:  # noqa: ANN401
        query = real_table(name)
        if name == "subscriptions":
            query.update = Mock(side_effect=RuntimeError("connection reset"))
        return query

    with (
        patch("backend.routes.webhooks.stripe.Webhook.construct_event", return_value=event),
        patch("backend.routes.webhooks.stripe.Subscription.retrieve", return_value={"created": 1_700_000_000}),
    ):
        with patch.object(platform.db, "table", side_effect=failing_subscription_writes):
            failed = client.post("/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "sig"})
        retried = client.post("/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "sig"})

    assert failed.status_code == 500
    assert retried.status_code == 200
    assert platform.subscription()["stripe_subscription_id"] == "sub_stripe_1"
    assert platform.subscription()["status"] == "active"
    assert len(platform.db.tables["webhook_events"]) == 1


def test_deletion_of_a_superseded_subscription_is_a_no_op(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("active", stripe_subscription_id="sub_stripe_new"))
    platform.db.tables["instances"].append(_instance("running"))

    body = _send_webhook("customer.subscription.deleted", {"id": "sub_stripe_old"})

    assert body == {"received": True, "error": None}
    assert platform.subscription()["status"] == "active"
    assert platform.instance()["status"] == "running"


@pytest.mark.asyncio
async def test_resume_after_a_failed_secret_publication_reprovisions(platform: Platform) -> None:
    now = datetime.now(UTC)
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].append(
        _instance(
            "stopped",
            lifecycle_stopped_at=(now - timedelta(days=2)).isoformat(),
            teardown_after=(now + timedelta(days=28)).isoformat(),
        )
    )
    platform.set_key_disabled.side_effect = [OpenRouterKeyNotFoundError("status 404"), None]
    provision = platform.provision.side_effect
    attempts = 0

    async def secret_fails_once(*args: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            # The replacement key is created and its hash saved, then publishing the Secret fails.
            platform.instance()["openrouter_key_hash"] = "key_hash_unpublished"
            msg = "Failed to apply instance Secret mindroom-api-keys-7"
            raise RuntimeError(msg)
        return await provision(*args, **kwargs)

    platform.provision.side_effect = secret_fails_once

    first = await reconcile_subscription_instances(SUBSCRIPTION_ID)
    assert first.errors
    assert platform.instance()["lifecycle_stopped_at"] is not None

    second = await reconcile_subscription_instances(SUBSCRIPTION_ID)

    assert second.errors == []
    assert attempts == 2
    assert platform.start.await_count == 1  # only the first run started; the retry reprovisioned
    assert platform.instance()["openrouter_key_hash"] == "key_hash_new"
    assert platform.instance()["lifecycle_stopped_at"] is None
    assert platform.instance()["lifecycle_error"] is None


def test_delayed_creation_of_an_older_subscription_keeps_the_newer_binding(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("active", stripe_subscription_id="sub_stripe_new"))
    platform.db.tables["instances"].append(_instance("running"))
    old_created = {**_stripe_subscription("incomplete"), "created": 1_700_000_000}

    with patch(
        "backend.routes.webhooks.stripe.Subscription.retrieve", return_value={"created": 1_750_000_000}
    ) as retrieve:
        _send_webhook("customer.subscription.created", old_created)

    retrieve.assert_called_once_with("sub_stripe_new")
    assert platform.subscription()["stripe_subscription_id"] == "sub_stripe_new"
    assert platform.subscription()["status"] == "active"
    assert platform.instance()["status"] == "running"


def test_creation_of_a_newer_subscription_replaces_the_old_binding(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("cancelled", stripe_subscription_id="sub_stripe_old"))
    newer = {**_stripe_subscription("active"), "created": 1_750_000_000}

    with patch("backend.routes.webhooks.stripe.Subscription.retrieve", return_value={"created": 1_700_000_000}):
        _send_webhook("customer.subscription.created", newer)

    assert platform.subscription()["stripe_subscription_id"] == "sub_stripe_1"
    assert platform.subscription()["status"] == "active"


@pytest.mark.asyncio
async def test_expired_trial_stops_instance_and_pauses_subscription(platform: Platform) -> None:
    now = datetime.now(UTC)
    platform.db.tables["subscriptions"].append(
        _subscription(
            "trialing", tier="byok", stripe_subscription_id=None, trial_ends_at=(now - timedelta(days=1)).isoformat()
        )
    )
    platform.db.tables["instances"].append(_instance("running", tier="byok", openrouter_key_hash=None))

    summary = await reconcile_subscription_instances(SUBSCRIPTION_ID, now=now)

    platform.kubectl.assert_has_awaits(
        [
            call(["scale", "deployment/mindroom-7", "--replicas=0"], namespace="mindroom-instances"),
            call(["scale", "deployment/synapse-7", "--replicas=0"], namespace="mindroom-instances"),
        ]
    )
    assert platform.instance()["status"] == "stopped"
    assert platform.subscription()["status"] == "paused"
    assert summary.instances_stopped == 1
    assert summary.subscriptions_paused == 1


@pytest.mark.asyncio
async def test_unexpired_trial_keeps_running(platform: Platform) -> None:
    now = datetime.now(UTC)
    platform.db.tables["subscriptions"].append(
        _subscription(
            "trialing", tier="byok", stripe_subscription_id=None, trial_ends_at=(now + timedelta(days=2)).isoformat()
        )
    )
    platform.db.tables["instances"].append(_instance("running", tier="byok", openrouter_key_hash=None))

    summary = await reconcile_subscription_instances(SUBSCRIPTION_ID, now=now)

    platform.kubectl.assert_not_awaited()
    assert platform.instance()["status"] == "running"
    assert platform.subscription()["status"] == "trialing"
    assert summary.subscriptions_paused == 0


@pytest.mark.asyncio
async def test_failed_tenant_scale_down_records_error_without_marking_stopped(platform: Platform) -> None:
    now = datetime.now(UTC)
    platform.db.tables["subscriptions"].append(
        _subscription(
            "trialing", tier="byok", stripe_subscription_id=None, trial_ends_at=(now - timedelta(days=1)).isoformat()
        )
    )
    platform.db.tables["instances"].append(_instance("running", tier="byok", openrouter_key_hash=None))
    platform.kubectl.side_effect = [(0, "scaled", ""), (1, "", "synapse scale forbidden")]

    summary = await reconcile_subscription_instances(SUBSCRIPTION_ID, now=now)

    instance = platform.instance()
    assert instance["status"] == "running"
    assert "synapse scale forbidden" in instance["lifecycle_error"]
    assert instance["teardown_after"] is not None
    assert summary.errors == ["instance 7: kubectl scale failed for deployment/synapse-7: synapse scale forbidden"]


@pytest.mark.asyncio
async def test_stripe_outage_stops_nothing(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("cancelled"))
    platform.db.tables["instances"].append(_instance("running"))
    platform.stripe.api_key = "sk_test"
    platform.stripe.Subscription.retrieve.side_effect = RuntimeError("stripe unavailable")

    with pytest.raises(RuntimeError, match="stripe unavailable"):
        await reconcile_subscription_instances(SUBSCRIPTION_ID)

    platform.kubectl.assert_not_awaited()
    assert platform.instance()["status"] == "running"


@pytest.mark.asyncio
async def test_resume_replaces_a_key_that_openrouter_deleted(platform: Platform) -> None:
    now = datetime.now(UTC)
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].append(
        _instance(
            "stopped",
            lifecycle_stopped_at=(now - timedelta(days=2)).isoformat(),
            teardown_after=(now + timedelta(days=28)).isoformat(),
        )
    )
    platform.set_key_disabled.side_effect = [OpenRouterKeyNotFoundError("status 404"), None]

    summary = await reconcile_subscription_instances(SUBSCRIPTION_ID)

    platform.start.assert_awaited_once_with(7)
    platform.provision.assert_awaited_once()
    assert platform.instance()["openrouter_key_hash"] == "key_hash_new"
    assert platform.instance()["lifecycle_stopped_at"] is None
    assert summary.errors == []


@pytest.mark.asyncio
async def test_failed_key_replacement_is_retried_by_reprovisioning(platform: Platform) -> None:
    now = datetime.now(UTC)
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].append(
        _instance(
            "stopped",
            lifecycle_stopped_at=(now - timedelta(days=2)).isoformat(),
            teardown_after=(now + timedelta(days=28)).isoformat(),
        )
    )
    platform.set_key_disabled.side_effect = [OpenRouterKeyNotFoundError("status 404"), None]
    provision = platform.provision.side_effect
    attempts = 0

    async def flaky_provision(*args: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            msg = "OpenRouter key creation failed with status 502"
            raise RuntimeError(msg)
        return await provision(*args, **kwargs)

    platform.provision.side_effect = flaky_provision

    first = await reconcile_subscription_instances(SUBSCRIPTION_ID)

    assert first.errors
    assert platform.instance()["openrouter_key_hash"] is None
    assert platform.instance()["lifecycle_stopped_at"] is not None

    second = await reconcile_subscription_instances(SUBSCRIPTION_ID)

    assert second.errors == []
    assert attempts == 2
    assert platform.instance()["openrouter_key_hash"] == "key_hash_new"
    assert platform.instance()["lifecycle_stopped_at"] is None
    assert platform.instance()["lifecycle_error"] is None


def test_update_for_a_superseded_stripe_subscription_is_ignored(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("active", stripe_subscription_id="sub_stripe_new"))
    platform.db.tables["instances"].append(_instance("running"))

    _send_webhook("customer.subscription.updated", _stripe_subscription("canceled"))

    assert platform.subscription()["status"] == "active"
    assert platform.instance()["status"] == "running"


def test_customer_start_of_held_instance_resumes_through_lifecycle(platform: Platform) -> None:
    now = datetime.now(UTC)
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].append(
        _instance(
            "stopped",
            lifecycle_stopped_at=(now - timedelta(days=2)).isoformat(),
            teardown_after=(now + timedelta(days=28)).isoformat(),
        )
    )
    app.dependency_overrides[verify_user] = lambda: {"account_id": ACCOUNT_ID, "email": "customer@example.com"}
    try:
        with (
            patch("backend.routes.instances.ensure_supabase", return_value=platform.db),
            patch("backend.services.provisioner_service.start_instance", platform.start),
        ):
            response = TestClient(app).post("/my/instances/7/start")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert platform.set_key_disabled.await_args.kwargs == {"disabled": False}
    assert platform.instance()["lifecycle_stopped_at"] is None


@pytest.mark.asyncio
async def test_operator_reprovision_keeps_held_instance_stopped() -> None:
    db = FakeSupabase(
        {
            "instances": [
                _instance(
                    "stopped", tier="byok", openrouter_key_hash=None, lifecycle_stopped_at="2026-09-20T03:00:00+00:00"
                )
            ]
        }
    )
    kubectl = AsyncMock(return_value=(0, "", ""))
    wait_ready = AsyncMock(return_value=True)
    service = "backend.services.provisioner_service"
    with (
        patch(f"{service}.run_kubectl", kubectl),
        patch(f"{service}.run_helm", AsyncMock(return_value=(0, "deployed", ""))),
        patch(f"{service}._apply_instance_secret", AsyncMock(return_value="hash")),
        patch(f"{service}.wait_for_deployment_ready", wait_ready),
        patch(f"{service}.PROVISIONER_API_KEY", "test-root-secret"),
    ):
        result = await provision_instance(
            db,
            data={"subscription_id": SUBSCRIPTION_ID, "account_id": ACCOUNT_ID, "tier": "byok", "instance_id": 7},
            background_tasks=None,
        )

    assert "kept stopped" in result["message"]
    assert call(["scale", "deployment/mindroom-7", "--replicas=0"], namespace="mindroom-instances") in (
        kubectl.await_args_list
    )
    wait_ready.assert_not_awaited()
    assert db.row("instances", instance_id=7)["status"] == "stopped"


def test_customer_start_is_refused_when_stripe_contradicts_the_stored_active_status(platform: Platform) -> None:
    now = datetime.now(UTC)
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].append(
        _instance(
            "stopped",
            lifecycle_stopped_at=(now - timedelta(days=2)).isoformat(),
            teardown_after=(now + timedelta(days=28)).isoformat(),
        )
    )
    platform.stripe.api_key = "sk_test"
    platform.stripe.Subscription.retrieve.return_value = {"status": "canceled", "trial_end": None}
    app.dependency_overrides[verify_user] = lambda: {"account_id": ACCOUNT_ID, "email": "customer@example.com"}
    try:
        with (
            patch("backend.routes.instances.ensure_supabase", return_value=platform.db),
            patch("backend.services.provisioner_service.start_instance", platform.start),
        ):
            response = TestClient(app).post("/my/instances/7/start")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 402
    platform.start.assert_not_awaited()
    assert platform.subscription()["status"] == "cancelled"
    assert platform.instance()["status"] == "stopped"


@pytest.mark.asyncio
async def test_failed_secret_publication_never_leaves_stored_metadata_naming_an_unpublished_key() -> None:
    hobby_budget = get_plan_details("hobby").included_ai_budget_usd
    # The stored key is stale (old budget), so provisioning deletes it and mints a replacement.
    db = FakeSupabase(
        {
            "instances": [
                _instance(
                    "stopped",
                    openrouter_key_hash="hash_A",
                    openrouter_key_limit_usd=hobby_budget + 1,
                    openrouter_key_limit_reset="monthly",
                )
            ]
        }
    )
    published = {"openrouter_key": "key_A"}
    alive = {"hash_A"}
    minted = iter(["B", "C"])
    apply_attempts = 0

    def create_key(*, management_api_key: str, plan: Any) -> CreatedOpenRouterKey:  # noqa: ARG001
        suffix = next(minted)
        alive.add(f"hash_{suffix}")
        return CreatedOpenRouterKey(f"key_{suffix}", f"hash_{suffix}", plan.name, plan.monthly_limit_usd, "monthly")

    def delete_key(*, management_api_key: str, key_hash: str) -> None:  # noqa: ARG001
        alive.discard(key_hash)

    async def apply_secret(_instance_id: str, _namespace: str, secret_data: dict[str, str]) -> str:
        nonlocal apply_attempts
        apply_attempts += 1
        if apply_attempts == 1:
            msg = "Failed to apply instance Secret mindroom-api-keys-7"
            raise RuntimeError(msg)
        published.update(secret_data)
        return "hash"

    async def kubectl(args: list[str], namespace: str | None = None) -> tuple[int, str, str]:  # noqa: ARG001
        for key, value in published.items():
            if f"-o=jsonpath={{.data.{key}}}" in args:
                return 0, base64.b64encode(value.encode()).decode(), ""
        return 0, "", ""

    service = "backend.services.provisioner_service"
    data = {"subscription_id": SUBSCRIPTION_ID, "account_id": ACCOUNT_ID, "tier": "hobby", "instance_id": 7}
    with (
        patch(f"{service}.OPENROUTER_PROVISIONING_API_KEY", "sk-or-v1-management"),
        patch(f"{service}.PROVISIONER_API_KEY", "test-root-secret"),
        patch(f"{service}.create_openrouter_key", create_key),
        patch(f"{service}.delete_openrouter_key", delete_key),
        patch(f"{service}._apply_instance_secret", apply_secret),
        patch(f"{service}.run_kubectl", kubectl),
        patch(f"{service}.run_helm", AsyncMock(return_value=(0, "deployed", ""))),
        patch(f"{service}.wait_for_deployment_ready", AsyncMock(return_value=True)),
    ):
        with pytest.raises(HTTPException):
            await provision_instance(db, data=data, background_tasks=None, resume_lifecycle_hold=True)
        # The stale key was deleted before B existed, and B was discarded, so the row names no key at all.
        assert db.row("instances", instance_id=7)["openrouter_key_hash"] is None
        assert alive == set()
        await provision_instance(db, data=data, background_tasks=None, resume_lifecycle_hold=True)

    assert published["openrouter_key"] == "key_C"
    assert db.row("instances", instance_id=7)["openrouter_key_hash"] == "hash_C"
    assert alive == {"hash_C"}


def test_lifecycle_migration_is_idempotent_and_service_role_only() -> None:
    migration = (MIGRATIONS_DIR / "004_instance_lifecycle.sql").read_text(encoding="utf-8")
    baseline = (MIGRATIONS_DIR / "000_consolidated_complete_schema.sql").read_text(encoding="utf-8")

    assert migration.lstrip().startswith("--")
    assert "BEGIN;" in migration
    assert migration.rstrip().endswith("COMMIT;")
    assert "CREATE TABLE IF NOT EXISTS cleanup_runs" in migration
    for column in ("lifecycle_stopped_at", "teardown_after", "lifecycle_error", "lifecycle_error_at"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in migration
        assert column in baseline
    for sql in (migration, baseline):
        assert "ALTER TABLE cleanup_runs ENABLE ROW LEVEL SECURITY;" in sql
        assert "REVOKE ALL ON TABLE cleanup_runs FROM PUBLIC, anon, authenticated;" in sql
        assert "GRANT ALL ON TABLE cleanup_runs TO service_role;" in sql
        assert "'incomplete', 'incomplete_expired', 'unpaid'" in sql


def _request_deletion(platform: Platform) -> Any:  # noqa: ANN401
    # The soft_delete_account RPC is recorded, not run, so the account is marked pending deletion up front.
    _pending_deletion(platform, days_ago=0)
    app.dependency_overrides[verify_user] = lambda: {"account_id": ACCOUNT_ID, "email": "customer@example.com"}
    try:
        with patch("backend.routes.gdpr.ensure_supabase", return_value=platform.db):
            return TestClient(app).post("/my/gdpr/request-deletion", json={"confirmation": True})
    finally:
        app.dependency_overrides.clear()


def test_deletion_request_stops_instances_and_lets_paid_billing_end_with_its_period(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].append(_instance("running"))
    _stripe_lists(
        platform,
        _stripe_sub("sub_stripe_1", "active"),
        _stripe_sub("sub_customer_ends", "active", ends=True),
        _stripe_sub("sub_customer_ends_on_date", "active", ends_on=PERIOD_END - 86_400),
        _stripe_sub("sub_unpaid_checkout", "incomplete"),
        _stripe_sub("sub_stripe_old", "canceled"),
    )

    response = _request_deletion(platform)

    assert response.status_code == 200
    assert platform.db.rpc_calls[0][0] == "soft_delete_account"
    # Only the paid subscription that would renew is set to end, and marked so cancelling the deletion undoes it;
    # the customer's own end dates stay.
    platform.stripe.Subscription.modify.assert_called_once_with(
        "sub_stripe_1", cancel_at_period_end=True, metadata={DELETION_BILLING_MARKER: "none"}
    )
    # A first invoice left unpaid has no period to finish and must not be paid during the grace period.
    platform.stripe.Subscription.cancel.assert_called_once_with("sub_unpaid_checkout")
    assert platform.scaled_down()
    assert platform.set_key_disabled.await_args.kwargs == {"disabled": True}
    assert platform.instance()["status"] == "stopped"
    assert platform.instance()["lifecycle_stopped_at"] is not None


@pytest.mark.asyncio
async def test_account_pending_deletion_stays_stopped_while_stripe_still_reports_active(platform: Platform) -> None:
    _pending_deletion(platform)
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].append(_instance("stopped", **_held(datetime.now(UTC))))
    platform.stripe.api_key = "sk_test"
    platform.stripe.Subscription.retrieve.return_value = {"status": "active", "trial_end": None}

    summary = await reconcile_all_subscriptions()

    platform.stripe.Subscription.retrieve.assert_not_called()
    platform.start.assert_not_awaited()
    platform.provision.assert_not_awaited()
    assert platform.instance()["status"] == "stopped"
    assert platform.instance()["lifecycle_stopped_at"] is not None
    assert summary.instances_resumed == 0


@pytest.mark.asyncio
async def test_account_pending_deletion_stops_instances_during_a_stripe_outage(platform: Platform) -> None:
    _pending_deletion(platform)
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].append(_instance("running"))
    platform.stripe.api_key = "sk_test"
    platform.stripe.Subscription.retrieve.side_effect = RuntimeError("stripe unavailable")

    summary = await reconcile_all_subscriptions()

    assert summary.errors == []
    assert platform.scaled_down()
    assert platform.instance()["status"] == "stopped"


def _cleanup_with_other_tasks_stubbed() -> Any:  # noqa: ANN401
    return (
        patch("backend.tasks.cleanup.cleanup_old_audit_logs", return_value={"audit_logs_deleted": 0}),
        patch("backend.tasks.cleanup.cleanup_old_usage_metrics", return_value={"usage_metrics_deleted": 0}),
    )


@pytest.mark.asyncio
async def test_hard_delete_uninstalls_every_instance_and_cancels_billing_before_deleting_rows(
    platform: Platform,
) -> None:
    _pending_deletion(platform, days_ago=8)
    platform.db.tables["subscriptions"].append(_subscription("cancelled"))
    platform.db.tables["instances"].append(_instance("deprovisioned"))
    _stripe_lists(platform, _stripe_sub("sub_stripe_1", "past_due", ends=True, marked=True))
    # The fake RPCs delete no rows, so the nightly lifecycle run after the cleanup still sees the instance.
    platform.stripe.Subscription.retrieve.return_value = {"status": "canceled", "trial_end": None}
    order: list[str] = []
    platform.uninstall.side_effect = lambda instance_id: order.append(f"uninstall {instance_id}")
    platform.stripe.Subscription.cancel.side_effect = lambda stripe_id: order.append(f"cancel {stripe_id}")

    _claimable(platform)

    audit_logs, usage_metrics = _cleanup_with_other_tasks_stubbed()
    with audit_logs, usage_metrics:
        run = await run_cleanup_job()

    assert order == ["cancel sub_stripe_1", "uninstall 7"]
    assert [name for name, _params in platform.db.rpc_calls] == ["claim_account_hard_delete", "hard_delete_account"]
    # The login goes last and takes the account row with it.
    assert platform.db.auth.admin.deleted_users == [ACCOUNT_ID]
    assert platform.db.tables["accounts"] == []
    assert run["summary"]["accounts"]["accounts_deleted"] == 1
    assert run["ok"] is True


@pytest.mark.asyncio
async def test_failed_auth_user_deletion_is_retried_by_the_next_run(platform: Platform) -> None:
    _pending_deletion(platform, days_ago=8)
    platform.db.tables["subscriptions"].append(_subscription("cancelled"))
    _claimable(platform)
    platform.db.auth.admin.error = RuntimeError("auth unavailable")

    audit_logs, usage_metrics = _cleanup_with_other_tasks_stubbed()
    with audit_logs, usage_metrics:
        failed = await run_cleanup_job()
    # The account row, and with it the login, survives the failure, so the next run finds the account again.
    assert platform.db.row("accounts", id=ACCOUNT_ID)["deleted_at"] is not None
    assert failed["summary"]["accounts"]["errors"] == [f"account {ACCOUNT_ID}: auth unavailable"]

    platform.db.auth.admin.error = None
    audit_logs, usage_metrics = _cleanup_with_other_tasks_stubbed()
    with audit_logs, usage_metrics:
        retried = await run_cleanup_job()

    assert platform.db.auth.admin.deleted_users == [ACCOUNT_ID]
    assert platform.db.tables["accounts"] == []
    assert retried["summary"]["accounts"]["accounts_deleted"] == 1


@pytest.mark.asyncio
async def test_failed_teardown_keeps_the_account_rows_for_the_next_run(platform: Platform) -> None:
    _pending_deletion(platform, days_ago=8)
    platform.db.tables["subscriptions"].append(_subscription("cancelled"))
    platform.db.tables["instances"].append(_instance("stopped"))
    platform.uninstall.side_effect = HTTPException(status_code=500, detail="Failed to uninstall instance: timeout")
    _claimable(platform)

    audit_logs, usage_metrics = _cleanup_with_other_tasks_stubbed()
    with audit_logs, usage_metrics:
        run = await run_cleanup_job()

    assert [name for name, _params in platform.db.rpc_calls] == ["claim_account_hard_delete"]
    assert run["ok"] is False
    assert run["summary"]["accounts"]["accounts_deleted"] == 0
    assert "Failed to uninstall instance: timeout" in run["summary"]["accounts"]["errors"][0]


@pytest.mark.asyncio
async def test_account_the_database_does_not_let_cleanup_claim_is_not_torn_down(platform: Platform) -> None:
    # The backend clock says the grace period ended, but restore_account's clock, the database's, disagrees.
    _pending_deletion(platform, days_ago=7.01)
    platform.db.tables["subscriptions"].append(_subscription("cancelled"))
    platform.db.tables["instances"].append(_instance("stopped"))
    platform.db.rpc_results["claim_account_hard_delete"] = False

    audit_logs, usage_metrics = _cleanup_with_other_tasks_stubbed()
    with audit_logs, usage_metrics:
        run = await run_cleanup_job()

    platform.uninstall.assert_not_awaited()
    assert [name for name, _params in platform.db.rpc_calls] == ["claim_account_hard_delete"]
    assert run["summary"]["accounts"] == {**run["summary"]["accounts"], "accounts_deleted": 0, "errors": []}


@pytest.mark.asyncio
async def test_account_inside_its_grace_period_is_not_torn_down(platform: Platform) -> None:
    _pending_deletion(platform, days_ago=2)
    platform.db.tables["subscriptions"].append(_subscription("cancelled"))
    platform.db.tables["instances"].append(_instance("stopped"))

    audit_logs, usage_metrics = _cleanup_with_other_tasks_stubbed()
    with audit_logs, usage_metrics:
        await run_cleanup_job()

    platform.uninstall.assert_not_awaited()
    assert platform.db.rpc_calls == []


def _cancel_deletion(platform: Platform) -> Any:  # noqa: ANN401
    record_rpc = platform.db.rpc
    platform.db.rpc_results["restore_account"] = True

    def restore_account(name: str, params: dict[str, Any]) -> Any:  # noqa: ANN401
        platform.db.row("accounts", id=ACCOUNT_ID)["deleted_at"] = None
        return record_rpc(name, params)

    app.dependency_overrides[verify_user_allow_deleted] = lambda: {
        "account_id": ACCOUNT_ID,
        "email": "customer@example.com",
    }
    try:
        with (
            patch("backend.routes.gdpr.ensure_supabase", return_value=platform.db),
            patch.object(platform.db, "rpc", side_effect=restore_account),
        ):
            return TestClient(app).post("/my/gdpr/cancel-deletion")
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(("stripe_status", "restarted"), [("canceled", False), ("active", True)])
def test_cancelled_deletion_restarts_instances_only_for_a_subscription_stripe_still_bills(
    platform: Platform, stripe_status: str, *, restarted: bool
) -> None:
    now = datetime.now(UTC)
    _pending_deletion(platform)
    # Stored as active, as the old restore_account wrote it; Stripe decides.
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].append(_instance("stopped", **_held(now)))
    _stripe_lists(
        platform,
        _stripe_sub("sub_stripe_1", "active", ends=True, marked=True),
        _stripe_sub("sub_customer_ends", "active", ends=True),
    )
    platform.stripe.Subscription.retrieve.return_value = {"status": stripe_status, "trial_end": None}

    response = _cancel_deletion(platform)

    assert response.status_code == 200
    assert platform.db.rpc_calls == [("restore_account", {"target_account_id": ACCOUNT_ID})]
    # Only the billing the deletion set to end renews again; the customer's own cancellation stays.
    platform.stripe.Subscription.modify.assert_called_once_with(
        "sub_stripe_1", cancel_at_period_end=False, metadata={DELETION_BILLING_MARKER: ""}
    )
    assert platform.start.await_count == int(restarted)
    assert (platform.instance()["lifecycle_stopped_at"] is None) is restarted


def test_fresh_provision_confirms_the_stored_status_with_stripe(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("active", tier="pro"))
    platform.stripe.api_key = "sk_test"
    platform.stripe.Subscription.retrieve.return_value = {"status": "canceled", "trial_end": None}
    provision = AsyncMock(return_value={"success": True, "customer_id": "7"})
    app.dependency_overrides[verify_user] = lambda: {"account_id": ACCOUNT_ID, "email": "customer@example.com"}
    try:
        with (
            patch("backend.routes.instances.ensure_supabase", return_value=platform.db),
            patch("backend.services.provisioner_service.provision_instance", provision),
        ):
            response = TestClient(app).post("/my/instances/provision")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 402
    provision.assert_not_awaited()
    assert platform.subscription()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_resume_on_a_cheaper_tier_reprovisions_instead_of_reenabling_the_old_key(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("active", tier="byok"))
    platform.db.tables["instances"].append(_instance("stopped", tier="pro", **_pro_key(), **_held(datetime.now(UTC))))

    await reconcile_subscription_instances(SUBSCRIPTION_ID)

    platform.start.assert_not_awaited()
    platform.provision.assert_awaited_once()
    assert platform.provision.await_args.kwargs["data"]["tier"] == "byok"
    assert platform.provision.await_args.kwargs["resume_lifecycle_hold"] is True
    assert platform.instance()["lifecycle_stopped_at"] is None


def test_start_of_a_stopped_instance_redeploys_it_for_its_tier(platform: Platform) -> None:
    # The pro key was revoked while the customer had it stopped after a downgrade to hobby.
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].append(_instance("stopped", tier="pro", openrouter_key_hash=None))
    app.dependency_overrides[verify_user] = lambda: {"account_id": ACCOUNT_ID, "email": "customer@example.com"}
    try:
        with (
            patch("backend.routes.instances.ensure_supabase", return_value=platform.db),
            patch("backend.services.provisioner_service.start_instance", platform.start),
        ):
            response = TestClient(app).post("/my/instances/7/start")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    platform.start.assert_awaited_once_with(7)
    platform.provision.assert_awaited_once()
    assert platform.provision.await_args.kwargs["data"]["tier"] == "hobby"


@pytest.mark.asyncio
async def test_tier_change_with_the_same_budget_redeploys_a_running_instance(platform: Platform) -> None:
    # Enterprise and byok both include no AI budget, but run different resource profiles.
    platform.db.tables["subscriptions"].append(_subscription("active", tier="byok"))
    platform.db.tables["instances"].append(_instance("running", tier="enterprise", openrouter_key_hash=None))

    await reconcile_subscription_instances(SUBSCRIPTION_ID)

    platform.provision.assert_awaited_once()
    assert platform.provision.await_args.kwargs["data"]["tier"] == "byok"


def test_plan_change_redeploys_a_running_instance_with_the_new_budget(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("active", tier="pro"))
    platform.db.tables["instances"].append(_instance("running", tier="pro", **_pro_key()))

    _send_webhook("customer.subscription.updated", _stripe_subscription("active", tier="hobby"))

    assert platform.subscription()["tier"] == "hobby"
    platform.provision.assert_awaited_once()
    assert platform.provision.await_args.kwargs["data"]["tier"] == "hobby"
    # Not a resume, so a hold or an account deletion that lands during the redeploy keeps the instance stopped.
    assert platform.provision.await_args.kwargs["resume_lifecycle_hold"] is False
    platform.revoke_key.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_after_failure", ["error", "running"])
async def test_failed_plan_redeploy_is_recorded_and_retried(platform: Platform, status_after_failure: str) -> None:
    platform.db.tables["subscriptions"].append(_subscription("active", tier="hobby"))
    platform.db.tables["instances"].append(_instance("running", tier="pro", **_pro_key()))
    redeploy = platform.provision.side_effect

    async def helm_fails_once(*args: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        if platform.provision.await_count == 1:
            # Like provision_instance, the hobby key is published before Helm fails, which marks the instance
            # errored; a Kubernetes status sync may set it back to running before the next run.
            platform.instance().update(
                {
                    "status": status_after_failure,
                    "openrouter_key_limit_usd": get_plan_details("hobby").included_ai_budget_usd,
                }
            )
            msg = "Helm install failed: timed out"
            raise RuntimeError(msg)
        return await redeploy(*args, **kwargs)

    platform.provision.side_effect = helm_fails_once

    first = await reconcile_subscription_instances(SUBSCRIPTION_ID)
    assert "Helm install failed" in platform.instance()["lifecycle_error"]
    # The tier is recorded only after a successful deploy, so the instance still reads as pro.
    assert platform.instance()["tier"] == "pro"

    second = await reconcile_subscription_instances(SUBSCRIPTION_ID)
    third = await reconcile_subscription_instances(SUBSCRIPTION_ID)

    assert first.errors
    assert second.errors == third.errors == []
    assert platform.provision.await_count == 2
    assert platform.instance()["tier"] == "hobby"
    assert platform.instance()["lifecycle_error"] is None


@pytest.mark.asyncio
async def test_plan_change_revokes_a_larger_key_of_a_stopped_instance_without_starting_it(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("active", tier="byok"))
    platform.db.tables["instances"].append(_instance("stopped", tier="pro", **_pro_key()))

    await reconcile_subscription_instances(SUBSCRIPTION_ID)

    platform.revoke_key.assert_awaited_once()
    assert platform.revoke_key.await_args.args[1] == 7
    platform.start.assert_not_awaited()
    platform.provision.assert_not_awaited()
    assert platform.instance()["status"] == "stopped"


@pytest.mark.asyncio
async def test_plan_upgrade_keeps_the_smaller_key_of_a_stopped_instance(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("active", tier="pro"))
    platform.db.tables["instances"].append(_instance("stopped"))

    await reconcile_subscription_instances(SUBSCRIPTION_ID)

    platform.revoke_key.assert_not_awaited()
    platform.provision.assert_not_awaited()
    platform.start.assert_not_awaited()


_SERVICE = "backend.services.provisioner_service"
_REPROVISION_7 = {"subscription_id": SUBSCRIPTION_ID, "account_id": ACCOUNT_ID, "instance_id": 7}


def _pvc_listing(size: str) -> str:
    return json.dumps(
        {
            "items": [
                {"spec": {"storageClassName": "hcloud-volumes", "resources": {"requests": {"storage": size}}}},
                {"spec": {"storageClassName": "hcloud-volumes", "resources": {"requests": {"storage": size}}}},
            ]
        }
    )


@pytest.mark.asyncio
async def test_reprovisioning_on_a_tier_without_budget_deletes_the_stored_key() -> None:
    db = FakeSupabase({"instances": [_instance("stopped", tier="pro", **_pro_key())]})
    published: dict[str, str] = {}
    delete_key = Mock(return_value=None)

    async def apply_secret(_instance_id: str, _namespace: str, secret_data: dict[str, str]) -> str:
        published.update(secret_data)
        return "hash"

    with (
        patch(f"{_SERVICE}.OPENROUTER_PROVISIONING_API_KEY", "sk-or-v1-management"),
        patch(f"{_SERVICE}.PROVISIONER_API_KEY", "test-root-secret"),
        patch(f"{_SERVICE}.create_openrouter_key", Mock(side_effect=AssertionError("byok has no budget"))),
        patch(f"{_SERVICE}.delete_openrouter_key", delete_key),
        patch(f"{_SERVICE}._apply_instance_secret", apply_secret),
        patch(f"{_SERVICE}.run_kubectl", AsyncMock(return_value=(0, "", ""))),
        patch(f"{_SERVICE}.run_helm", AsyncMock(return_value=(0, "deployed", ""))),
        patch(f"{_SERVICE}.wait_for_deployment_ready", AsyncMock(return_value=True)),
    ):
        await provision_instance(
            db, data={**_REPROVISION_7, "tier": "byok"}, background_tasks=None, resume_lifecycle_hold=True
        )

    delete_key.assert_called_once_with(management_api_key="sk-or-v1-management", key_hash="key_hash_pro")
    assert published["openrouter_key"] == ""
    row = db.row("instances", instance_id=7)
    assert row["openrouter_key_hash"] is None
    assert row["openrouter_key_limit_usd"] is None
    assert row["tier"] == "byok"


@pytest.mark.asyncio
async def test_a_failed_redeploy_keeps_the_previously_deployed_tier() -> None:
    db = FakeSupabase({"instances": [_instance("running", tier="pro", openrouter_key_hash=None)]})
    with (
        patch(f"{_SERVICE}.PROVISIONER_API_KEY", "test-root-secret"),
        patch(f"{_SERVICE}._apply_instance_secret", AsyncMock(return_value="hash")),
        patch(f"{_SERVICE}.run_kubectl", AsyncMock(return_value=(0, "", ""))),
        patch(f"{_SERVICE}.run_helm", AsyncMock(return_value=(1, "", "timed out"))),
        pytest.raises(HTTPException),
    ):
        await provision_instance(db, data={**_REPROVISION_7, "tier": "byok"}, background_tasks=None)

    # The lifecycle compares this tier with the subscription's, so it keeps retrying until a deploy succeeds.
    assert db.row("instances", instance_id=7)["tier"] == "pro"
    assert db.row("instances", instance_id=7)["status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tier", "existing_size", "expected_size"),
    [("hobby", "25Gi", "25Gi"), ("pro", "10Gi", "25Gi"), ("pro", "50Gi", "50Gi")],
)
async def test_reprovisioning_never_shrinks_the_instance_volumes(
    tier: str, existing_size: str, expected_size: str
) -> None:
    db = FakeSupabase({"instances": [_instance("running", openrouter_key_hash=None)]})
    helm = AsyncMock(return_value=(0, "deployed", ""))

    async def kubectl(args: list[str], namespace: str | None = None) -> tuple[int, str, str]:  # noqa: ARG001
        return (0, _pvc_listing(existing_size), "") if args[:2] == ["get", "pvc"] else (0, "", "")

    created = CreatedOpenRouterKey("key", "hash_new", "label", get_plan_details(tier).included_ai_budget_usd, "monthly")
    with (
        patch(f"{_SERVICE}.OPENROUTER_PROVISIONING_API_KEY", "sk-or-v1-management"),
        patch(f"{_SERVICE}.PROVISIONER_API_KEY", "test-root-secret"),
        patch(f"{_SERVICE}.create_openrouter_key", Mock(return_value=created)),
        patch(f"{_SERVICE}._apply_instance_secret", AsyncMock(return_value="hash")),
        patch(f"{_SERVICE}.run_kubectl", kubectl),
        patch(f"{_SERVICE}.run_helm", helm),
        patch(f"{_SERVICE}.wait_for_deployment_ready", AsyncMock(return_value=True)),
    ):
        await provision_instance(db, data={**_REPROVISION_7, "tier": tier}, background_tasks=None)

    helm_args = helm.await_args.args[0]
    storage_sets = [
        value for flag, value in zip(helm_args, helm_args[1:]) if flag == "--set" and value.startswith("storage=")
    ]
    assert storage_sets[-1] == f"storage={expected_size}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("constraint", "status_code"), [("instances_subscription_id_key", 409), ("instances_subdomain_key", 500)]
)
async def test_a_second_instance_for_one_subscription_is_refused_by_the_database(
    constraint: str, status_code: int
) -> None:
    create_key = Mock()
    helm = AsyncMock()
    duplicate = PostgrestAPIError(
        {"code": "23505", "message": f'duplicate key value violates unique constraint "{constraint}"'}
    )
    with (
        patch(f"{_SERVICE}.create_instance", Mock(side_effect=duplicate)),
        patch(f"{_SERVICE}.create_openrouter_key", create_key),
        patch(f"{_SERVICE}.run_helm", helm),
        pytest.raises(HTTPException) as refused,
    ):
        await provision_instance(
            FakeSupabase({"instances": []}),
            data={"subscription_id": SUBSCRIPTION_ID, "account_id": ACCOUNT_ID, "tier": "pro"},
            background_tasks=None,
        )

    assert refused.value.status_code == status_code
    create_key.assert_not_called()
    helm.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_soft_deleted_instance_that_kept_running_is_held(platform: Platform) -> None:
    # An older soft_delete_account marked the instance deprovisioned without stopping its deployment.
    _pending_deletion(platform)
    platform.db.tables["subscriptions"].append(_subscription("cancelled"))
    platform.db.tables["instances"].append(_instance("deprovisioned"))

    summary = await reconcile_subscription_instances(SUBSCRIPTION_ID)

    assert summary.errors == []
    assert platform.scaled_down()
    assert platform.set_key_disabled.await_args.kwargs == {"disabled": True}
    assert platform.instance()["status"] == "stopped"
    assert platform.instance()["teardown_after"] is not None


@pytest.mark.asyncio
async def test_legacy_soft_deleted_instance_of_a_paying_restored_account_runs(platform: Platform) -> None:
    # An older soft delete stored the subscription as cancelled; the account was restored and Stripe still bills.
    platform.db.tables["subscriptions"].append(_subscription("cancelled"))
    platform.db.tables["instances"].append(_instance("deprovisioned"))
    platform.stripe.api_key = "sk_test"
    platform.stripe.Subscription.retrieve.return_value = {"status": "active", "trial_end": None}

    await reconcile_all_subscriptions()

    assert platform.subscription()["status"] == "active"
    platform.kubectl.assert_not_awaited()
    assert platform.instance()["status"] == "running"
    assert platform.instance()["lifecycle_stopped_at"] is None


@pytest.mark.asyncio
async def test_webhook_reconcile_does_not_look_for_legacy_deployments(platform: Platform) -> None:
    # Only the nightly run and accounts pending deletion pay a Kubernetes call per torn-down instance.
    platform.db.tables["subscriptions"].append(_subscription("cancelled"))
    platform.db.tables["instances"].append(_instance("deprovisioned", openrouter_key_hash=None))
    check = AsyncMock(return_value=True)

    with patch(f"{LEGACY}.check_deployment_exists", check):
        await reconcile_subscription_instances(SUBSCRIPTION_ID)

    check.assert_not_awaited()
    assert platform.instance()["status"] == "deprovisioned"


@pytest.mark.asyncio
async def test_nightly_run_sets_billing_of_accounts_inside_their_grace_period_to_end(platform: Platform) -> None:
    # Deletion was requested before a release that set billing to end at request time.
    _pending_deletion(platform, days_ago=2)
    platform.db.tables["subscriptions"].append(_subscription("active"))
    _stripe_lists(platform, _stripe_sub("sub_stripe_1", "active"), _stripe_sub("sub_unpaid", "incomplete"))

    audit_logs, usage_metrics = _cleanup_with_other_tasks_stubbed()
    with audit_logs, usage_metrics:
        run = await run_cleanup_job()

    platform.stripe.Subscription.modify.assert_called_once_with(
        "sub_stripe_1", cancel_at_period_end=True, metadata={DELETION_BILLING_MARKER: "none"}
    )
    # Also a retry for a cancellation the deletion request could not finish.
    platform.stripe.Subscription.cancel.assert_called_once_with("sub_unpaid")
    assert platform.db.rpc_calls == []
    assert run["summary"]["accounts"]["errors"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("restored", ["before the billing change", "during the billing change"])
async def test_nightly_billing_change_leaves_an_account_restored_meanwhile_billed(
    platform: Platform, restored: str
) -> None:
    _pending_deletion(platform, days_ago=2)
    platform.db.tables["subscriptions"].append(_subscription("active"))
    _stripe_lists(platform, _stripe_sub("sub_stripe_1", "active"), _stripe_sub("sub_unpaid", "incomplete"))
    account = platform.db.row("accounts", id=ACCOUNT_ID)
    real_table = platform.db.table
    listed = False

    def restore_after_listing(name: str) -> Any:  # noqa: ANN401
        # The customer cancels the deletion right after the nightly job listed the pending accounts.
        nonlocal listed
        query = real_table(name)
        if name == "accounts" and not listed and restored == "before the billing change":
            listed = True
            execute = query.execute

            def execute_then_restore() -> Any:  # noqa: ANN401
                result = execute()
                account["deleted_at"] = None
                return result

            query.execute = execute_then_restore
        return query

    def modify(_stripe_id: str, **params: Any) -> None:  # noqa: ANN401
        if params["cancel_at_period_end"] and restored == "during the billing change":
            account["deleted_at"] = None

    platform.stripe.Subscription.modify.side_effect = modify

    audit_logs, usage_metrics = _cleanup_with_other_tasks_stubbed()
    with audit_logs, usage_metrics, patch.object(platform.db, "table", side_effect=restore_after_listing):
        await run_cleanup_job()

    ends = call("sub_stripe_1", cancel_at_period_end=True, metadata={DELETION_BILLING_MARKER: "none"})
    resumes = call("sub_stripe_1", cancel_at_period_end=False, metadata={DELETION_BILLING_MARKER: ""})
    expected = [] if restored == "before the billing change" else [ends, resumes]
    assert platform.stripe.Subscription.modify.call_args_list == expected
    # A cancellation cannot be undone, so a restored account never loses a subscription to it.
    platform.stripe.Subscription.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_deprovisioned_instance_without_a_deployment_is_left_alone(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("cancelled"))
    platform.db.tables["instances"].append(_instance("deprovisioned", openrouter_key_hash=None))

    with patch(f"{LEGACY}.check_deployment_exists", AsyncMock(return_value=False)):
        await reconcile_all_subscriptions()

    platform.kubectl.assert_not_awaited()
    assert platform.instance()["status"] == "deprovisioned"
    assert platform.instance()["lifecycle_stopped_at"] is None


def test_failed_billing_schedule_undoes_the_subscriptions_it_already_set_to_end(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].append(_instance("running"))
    _stripe_lists(platform, _stripe_sub("sub_a", "active"), _stripe_sub("sub_b", "trialing"))
    platform.stripe.StripeError = stripe.StripeError

    def modify(stripe_id: str, **params: Any) -> None:  # noqa: ANN401
        if stripe_id == "sub_b" and params["cancel_at_period_end"]:
            msg = "stripe unavailable"
            raise stripe.APIConnectionError(msg)

    platform.stripe.Subscription.modify.side_effect = modify

    response = _request_deletion(platform)

    assert response.status_code == 502
    assert platform.db.rpc_calls == []
    assert platform.stripe.Subscription.modify.call_args_list == [
        call("sub_a", cancel_at_period_end=True, metadata={DELETION_BILLING_MARKER: "none"}),
        call("sub_b", cancel_at_period_end=True, metadata={DELETION_BILLING_MARKER: "none"}),
        call("sub_a", cancel_at_period_end=False, metadata={DELETION_BILLING_MARKER: ""}),
    ]
    assert platform.instance()["status"] == "running"


@pytest.mark.parametrize(("path", "instances"), [("/my/instances/provision", []), ("/my/instances/7/start", [7])])
def test_account_pending_deletion_cannot_provision_or_start_instances(
    platform: Platform, path: str, instances: list[int]
) -> None:
    _pending_deletion(platform)
    platform.db.tables["subscriptions"].append(_subscription("active"))
    platform.db.tables["instances"].extend(_instance("stopped", instance_id=instance_id) for instance_id in instances)
    provision = AsyncMock()
    app.dependency_overrides[verify_user] = lambda: {"account_id": ACCOUNT_ID, "email": "customer@example.com"}
    try:
        with (
            patch("backend.routes.instances.ensure_supabase", return_value=platform.db),
            patch("backend.services.provisioner_service.provision_instance", provision),
            patch("backend.services.provisioner_service.start_instance", platform.start),
        ):
            response = TestClient(app).post(path)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    provision.assert_not_awaited()
    platform.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_instance_provisioned_for_an_account_pending_deletion_stays_stopped() -> None:
    # The deletion request's hold ran before this instance row existed, so only the account says to hold it.
    db = FakeSupabase(
        {
            "accounts": [{"id": ACCOUNT_ID, "deleted_at": "2026-09-28T03:00:00+00:00"}],
            "instances": [_instance("running", tier="byok", openrouter_key_hash=None)],
        }
    )
    kubectl = AsyncMock(return_value=(0, "", ""))
    with (
        patch(f"{_SERVICE}.PROVISIONER_API_KEY", "test-root-secret"),
        patch(f"{_SERVICE}._apply_instance_secret", AsyncMock(return_value="hash")),
        patch(f"{_SERVICE}.run_kubectl", kubectl),
        patch(f"{_SERVICE}.run_helm", AsyncMock(return_value=(0, "deployed", ""))),
        patch(f"{_SERVICE}.wait_for_deployment_ready", AsyncMock(return_value=True)),
    ):
        result = await provision_instance(db, data={**_REPROVISION_7, "tier": "byok"}, background_tasks=None)

    assert "kept stopped" in result["message"]
    assert db.row("instances", instance_id=7)["status"] == "stopped"


@pytest.mark.asyncio
@pytest.mark.parametrize("during", ["helm", "readiness"])
async def test_a_hold_that_lands_while_provisioning_keeps_the_instance_stopped(during: str) -> None:
    db = FakeSupabase({"instances": [_instance("running", tier="byok", openrouter_key_hash=None)]})
    kubectl = AsyncMock(return_value=(0, "", ""))

    def hold_now(step: str) -> None:
        # A webhook reconcile holds the instance concurrently; the provisioning request does not take its lock.
        if step == during:
            db.row("instances", instance_id=7)["lifecycle_stopped_at"] = "2026-09-28T03:00:00+00:00"

    async def helm(_args: list[str]) -> tuple[int, str, str]:
        hold_now("helm")
        return 0, "deployed", ""

    async def wait_ready(*_args: Any, **_kwargs: Any) -> bool:  # noqa: ANN401
        hold_now("readiness")
        return True

    with (
        patch(f"{_SERVICE}.PROVISIONER_API_KEY", "test-root-secret"),
        patch(f"{_SERVICE}._apply_instance_secret", AsyncMock(return_value="hash")),
        patch(f"{_SERVICE}.run_kubectl", kubectl),
        patch(f"{_SERVICE}.run_helm", helm),
        patch(f"{_SERVICE}.wait_for_deployment_ready", wait_ready),
    ):
        result = await provision_instance(db, data={**_REPROVISION_7, "tier": "byok"}, background_tasks=None)

    assert "kept stopped" in result["message"]
    assert call(["scale", "deployment/mindroom-7", "--replicas=0"], namespace="mindroom-instances") in (
        kubectl.await_args_list
    )
    assert db.row("instances", instance_id=7)["status"] == "stopped"


def test_a_trial_after_an_ended_earlier_trial_ends_at_once(platform: Platform) -> None:
    # A checkout opened before the earlier trial was cancelled still carried a trial.
    platform.db.tables["subscriptions"].append(_subscription("cancelled", stripe_subscription_id="sub_stripe_old"))
    trialing = {**_stripe_subscription("trialing"), "created": 1_750_000_000, "trial_start": 1_750_000_000}
    history = [
        Mock(id="sub_stripe_1", status="trialing", created=1_750_000_000, trial_start=1_750_000_000),
        Mock(id="sub_stripe_old", status="canceled", created=1_700_000_000, trial_start=1_700_000_000),
    ]
    ended = {**_stripe_subscription("active"), "trial_start": 1_750_000_000}
    webhooks = "backend.routes.webhooks.stripe.Subscription"

    with (
        patch(f"{webhooks}.list") as list_subscriptions,
        patch(f"{webhooks}.retrieve", side_effect=[{"created": 1_700_000_000}, {"status": "trialing"}]),
        patch(f"{webhooks}.modify", return_value=ended) as modify,
    ):
        list_subscriptions.return_value.auto_paging_iter.return_value = history
        _send_webhook("customer.subscription.created", trialing)

    modify.assert_called_once_with("sub_stripe_1", trial_end="now")
    assert platform.subscription()["stripe_subscription_id"] == "sub_stripe_1"
    assert platform.subscription()["status"] == "active"


def test_a_redelivered_event_for_a_cancelled_duplicate_trial_keeps_the_binding(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("trialing", stripe_subscription_id="sub_stripe_first"))
    later = {**_stripe_subscription("trialing"), "created": 1_750_000_100, "trial_start": 1_750_000_100}
    history = [
        Mock(id="sub_stripe_first", status="trialing", created=1_750_000_000, trial_start=1_750_000_000),
        Mock(id="sub_stripe_1", status="trialing", created=1_750_000_100, trial_start=1_750_000_100),
    ]
    webhooks = "backend.routes.webhooks.stripe.Subscription"
    # Each delivery checks the account's binding, then the new subscription, which the first delivery cancelled.
    cancelled = {**later, "status": "canceled"}
    retrieved = [{"created": 1_750_000_000}, {"status": "trialing"}, {"created": 1_750_000_000}, cancelled]

    with (
        patch(f"{webhooks}.list") as list_subscriptions,
        patch(f"{webhooks}.retrieve", side_effect=retrieved),
        patch(f"{webhooks}.cancel") as cancel,
        patch(f"{webhooks}.modify") as modify,
    ):
        list_subscriptions.return_value.auto_paging_iter.return_value = history
        first = _send_webhook("customer.subscription.created", later)
        redelivered = _send_webhook("customer.subscription.created", later)

    assert first == redelivered == {"received": True, "error": None}
    cancel.assert_called_once_with("sub_stripe_1")
    modify.assert_not_called()
    assert platform.subscription()["stripe_subscription_id"] == "sub_stripe_first"
    assert platform.subscription()["status"] == "trialing"


def test_a_trial_duplicating_a_running_earlier_trial_is_cancelled_and_not_bound(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("trialing", stripe_subscription_id="sub_stripe_first"))
    later = {**_stripe_subscription("trialing"), "created": 1_750_000_100, "trial_start": 1_750_000_100}
    history = [
        Mock(id="sub_stripe_first", status="trialing", created=1_750_000_000, trial_start=1_750_000_000),
        Mock(id="sub_stripe_1", status="trialing", created=1_750_000_100, trial_start=1_750_000_100),
    ]
    webhooks = "backend.routes.webhooks.stripe.Subscription"

    with (
        patch(f"{webhooks}.list") as list_subscriptions,
        patch(f"{webhooks}.retrieve", side_effect=[{"created": 1_750_000_000}, {"status": "trialing"}]),
        patch(f"{webhooks}.cancel") as cancel,
        patch(f"{webhooks}.modify") as modify,
    ):
        list_subscriptions.return_value.auto_paging_iter.return_value = history
        _send_webhook("customer.subscription.created", later)

    cancel.assert_called_once_with("sub_stripe_1")
    modify.assert_not_called()
    assert platform.subscription()["stripe_subscription_id"] == "sub_stripe_first"


def test_the_earliest_of_two_parallel_trials_is_kept(platform: Platform) -> None:
    trialing = {**_stripe_subscription("trialing"), "created": 1_750_000_000, "trial_start": 1_750_000_000}
    webhooks = "backend.routes.webhooks.stripe.Subscription"
    history = [
        Mock(id="sub_stripe_1", status="trialing", created=1_750_000_000, trial_start=1_750_000_000),
        Mock(id="sub_stripe_later", status="trialing", created=1_750_000_100, trial_start=1_750_000_100),
    ]

    with patch(f"{webhooks}.list") as list_subscriptions, patch(f"{webhooks}.modify") as modify:
        list_subscriptions.return_value.auto_paging_iter.return_value = history
        _send_webhook("customer.subscription.created", trialing)

    modify.assert_not_called()
    assert [row["status"] for row in platform.db.tables["subscriptions"]] == ["trialing"]


@pytest.mark.asyncio
async def test_short_teardown_grace_never_uninstalls_inside_the_account_deletion_grace_period(
    platform: Platform,
) -> None:
    # With INSTANCE_TEARDOWN_GRACE_DAYS below 7 the held instance's teardown date comes first; the account's own
    # cleanup, which the customer can still cancel, decides.
    now = datetime.now(UTC)
    _pending_deletion(platform, days_ago=2)
    platform.db.tables["subscriptions"].append(_subscription("cancelled"))
    platform.db.tables["instances"].append(
        _instance(
            "stopped",
            lifecycle_stopped_at=(now - timedelta(days=2)).isoformat(),
            teardown_after=(now - timedelta(days=1)).isoformat(),
        )
    )

    await reconcile_subscription_instances(SUBSCRIPTION_ID, now=now)

    platform.uninstall.assert_not_awaited()
    assert platform.instance()["status"] == "stopped"
    # A restored account gets the instance's full teardown grace period again, not a date already past.
    assert datetime.fromisoformat(platform.instance()["teardown_after"]) == now + timedelta(days=30)


def test_admin_overview_does_not_flag_the_deferred_teardown_of_an_account_pending_deletion(
    platform: Platform,
) -> None:
    now = datetime.now(UTC)
    _pending_deletion(platform, days_ago=2)
    platform.db.tables["subscriptions"].append(_subscription("cancelled"))
    platform.db.tables["instances"].append(
        _instance(
            "stopped",
            lifecycle_stopped_at=(now - timedelta(days=9)).isoformat(),
            teardown_after=(now - timedelta(days=3)).isoformat(),
        )
    )

    _pending, stuck = lifecycle_overview(now=now)

    assert stuck == []


def test_deletion_moves_a_later_end_date_to_the_period_end_and_cancelling_it_restores_that_date(
    platform: Platform,
) -> None:
    later = PERIOD_END + 30 * 86_400
    platform.db.tables["subscriptions"].append(_subscription("active"))
    _stripe_lists(platform, _stripe_sub("sub_stripe_1", "active", ends_on=later))

    assert _request_deletion(platform).status_code == 200
    platform.stripe.Subscription.modify.assert_called_once_with(
        "sub_stripe_1", cancel_at_period_end=True, metadata={DELETION_BILLING_MARKER: str(later)}
    )

    platform.stripe.Subscription.modify.reset_mock()
    _stripe_lists(platform, _stripe_sub("sub_stripe_1", "active", ends=True, marked=True))
    platform.stripe.Subscription.list.return_value.auto_paging_iter.return_value[0].metadata = {
        DELETION_BILLING_MARKER: str(later)
    }
    assert _cancel_deletion(platform).status_code == 200

    platform.stripe.Subscription.modify.assert_called_once_with(
        "sub_stripe_1", cancel_at=later, metadata={DELETION_BILLING_MARKER: ""}
    )


def test_unpaid_subscriptions_are_cancelled_only_once_the_deletion_is_recorded(platform: Platform) -> None:
    platform.db.tables["subscriptions"].append(_subscription("incomplete"))
    _stripe_lists(platform, _stripe_sub("sub_unpaid", "incomplete"))
    recorded_first: list[bool] = []
    platform.stripe.Subscription.cancel.side_effect = lambda _stripe_id: recorded_first.append(
        [name for name, _params in platform.db.rpc_calls] == ["soft_delete_account"]
    )

    assert _request_deletion(platform).status_code == 200

    assert recorded_first == [True]


@pytest.mark.asyncio
async def test_nightly_cleanup_pages_through_every_pending_account(platform: Platform) -> None:
    second_account = "00000000-0000-0000-0000-000000000002"
    _pending_deletion(platform, days_ago=2)
    platform.db.tables["accounts"].append(
        {
            "id": second_account,
            "stripe_customer_id": "cus_2",
            "deleted_at": platform.db.row("accounts", id=ACCOUNT_ID)["deleted_at"],
        }
    )
    seen: list[str] = []

    async def record(account_id: str) -> list[Any]:
        seen.append(account_id)
        return []

    audit_logs, usage_metrics = _cleanup_with_other_tasks_stubbed()
    with (
        audit_logs,
        usage_metrics,
        patch("backend.tasks.cleanup._PAGE_SIZE", 1),
        patch("backend.tasks.cleanup.end_account_billing_at_period_end", side_effect=record),
        patch("backend.tasks.cleanup.cancel_unpaid_subscriptions", AsyncMock()),
    ):
        await run_cleanup_job()

    assert sorted(seen) == sorted([ACCOUNT_ID, second_account])
