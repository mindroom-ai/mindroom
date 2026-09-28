"""Hosted instances follow their subscription: stop, restart, and tear down."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from backend.deps import verify_admin, verify_user
from backend.openrouter import OpenRouterKeyNotFoundError
from backend.services.instance_lifecycle import (
    LifecycleSummary,
    reconcile_all_subscriptions,
    reconcile_subscription_instances,
)
from backend.services.provisioner_service import provision_instance, set_instance_openrouter_key_disabled
from backend.tasks.cleanup import run_cleanup_job
from fastapi.testclient import TestClient
from main import app

from tests.fake_supabase import FakeSupabase

ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"
SUBSCRIPTION_ID = "11111111-1111-1111-1111-111111111111"
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "supabase/migrations"


@dataclass
class Platform:
    """Fake database plus the Kubernetes, provisioning, and OpenRouter side effects of the lifecycle."""

    db: FakeSupabase
    kubectl: AsyncMock
    start: AsyncMock
    provision: AsyncMock
    uninstall: AsyncMock
    set_key_disabled: AsyncMock
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
        resume_lifecycle_hold: bool,
    ) -> dict[str, Any]:
        assert resume_lifecycle_hold
        db.row("instances", instance_id=data["instance_id"]).update(
            {"status": "running", "openrouter_key_hash": "key_hash_new"}
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
        stripe=Mock(api_key=""),
    )
    with (
        patch(f"{lifecycle}.ensure_supabase", return_value=db),
        patch("backend.routes.webhooks.ensure_supabase", return_value=db),
        patch("backend.tasks.cleanup.ensure_supabase", return_value=db),
        patch(f"{lifecycle}.run_kubectl", platform.kubectl),
        patch(f"{lifecycle}.check_deployment_exists", AsyncMock(return_value=True)),
        patch(f"{lifecycle}.start_instance", platform.start),
        patch(f"{lifecycle}.provision_instance", platform.provision),
        patch(f"{lifecycle}.uninstall_instance", platform.uninstall),
        patch(f"{lifecycle}.set_instance_openrouter_key_disabled", platform.set_key_disabled),
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


def _stripe_subscription(status: str) -> dict[str, Any]:
    return {
        "id": "sub_stripe_1",
        "customer": "cus_1",
        "status": status,
        "items": {
            "data": [{"price": {"id": "price_hobby", "metadata": {"tier": "hobby", "billing_cycle": "monthly"}}}]
        },
        "trial_end": None,
        "canceled_at": None,
    }


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
        patch("backend.tasks.cleanup.cleanup_soft_deleted_accounts", return_value={"accounts_deleted": 0}),
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

    _send_webhook("invoice.payment_failed", {"id": "in_1", "customer": "cus_1", "subscription": "sub_stripe_1"})

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
