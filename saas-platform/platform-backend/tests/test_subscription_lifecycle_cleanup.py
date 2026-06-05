"""Subscription lifecycle cleanup tests."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import pytest

from backend.tasks.cleanup import cleanup_unentitled_instances


@pytest.mark.asyncio
async def test_cleanup_stops_instances_for_expired_trials() -> None:
    expired_trial = {
        "id": "sub_expired",
        "tier": "byok",
        "status": "trialing",
        "trial_ends_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
    }
    active_subscription = {"id": "sub_active", "tier": "byok", "status": "active", "trial_ends_at": None}

    subscription_query = MagicMock()
    subscription_query.select.return_value = subscription_query
    subscription_query.execute.return_value = Mock(data=[expired_trial, active_subscription])

    instance_query = MagicMock()
    instance_query.select.return_value = instance_query
    instance_query.eq.return_value = instance_query
    instance_query.in_.return_value = instance_query
    instance_query.execute.return_value = Mock(data=[{"instance_id": 123, "status": "running"}])

    instance_update = MagicMock()
    instance_update.update.return_value = instance_update
    instance_update.eq.return_value = instance_update
    instance_update.execute.return_value = Mock(data=[])

    subscription_update = MagicMock()
    subscription_update.update.return_value = subscription_update
    subscription_update.eq.return_value = subscription_update
    subscription_update.execute.return_value = Mock(data=[])

    table_calls = []

    def table_side_effect(table_name: str) -> MagicMock:
        table_calls.append(table_name)
        if table_name == "subscriptions" and table_calls.count("subscriptions") == 1:
            return subscription_query
        if table_name == "instances" and table_calls.count("instances") == 1:
            return instance_query
        if table_name == "instances":
            return instance_update
        if table_name == "subscriptions":
            return subscription_update
        return MagicMock()

    supabase = MagicMock()
    supabase.table.side_effect = table_side_effect

    with patch("backend.tasks.cleanup.ensure_supabase", return_value=supabase):
        with patch("backend.tasks.cleanup.run_kubectl", new=AsyncMock(return_value=(0, "", ""))) as kubectl:
            result = await cleanup_unentitled_instances()

    assert result["instances_stopped"] == 1
    assert result["subscriptions_paused"] == 1
    assert result["errors"] == 0
    kubectl.assert_has_awaits(
        [
            call(["scale", "deployment/mindroom-123", "--replicas=0"], namespace="mindroom-instances"),
            call(["scale", "deployment/synapse-123", "--replicas=0"], namespace="mindroom-instances"),
        ]
    )
    instance_update.update.assert_called_once()
    assert instance_update.update.call_args[0][0]["status"] == "stopped"
    subscription_update.update.assert_called_once()
    assert subscription_update.update.call_args[0][0]["status"] == "paused"


@pytest.mark.asyncio
async def test_cleanup_does_not_mark_instance_stopped_when_any_tenant_deployment_fails() -> None:
    expired_trial = {
        "id": "sub_expired",
        "tier": "byok",
        "status": "trialing",
        "trial_ends_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
    }

    subscription_query = MagicMock()
    subscription_query.select.return_value = subscription_query
    subscription_query.execute.return_value = Mock(data=[expired_trial])

    instance_query = MagicMock()
    instance_query.select.return_value = instance_query
    instance_query.eq.return_value = instance_query
    instance_query.in_.return_value = instance_query
    instance_query.execute.return_value = Mock(data=[{"instance_id": 123, "status": "running"}])

    instance_update = MagicMock()
    instance_update.update.return_value = instance_update
    instance_update.eq.return_value = instance_update
    instance_update.execute.return_value = Mock(data=[])

    subscription_update = MagicMock()
    subscription_update.update.return_value = subscription_update
    subscription_update.eq.return_value = subscription_update
    subscription_update.execute.return_value = Mock(data=[])

    table_calls = []

    def table_side_effect(table_name: str) -> MagicMock:
        table_calls.append(table_name)
        if table_name == "subscriptions" and table_calls.count("subscriptions") == 1:
            return subscription_query
        if table_name == "instances" and table_calls.count("instances") == 1:
            return instance_query
        if table_name == "instances":
            return instance_update
        if table_name == "subscriptions":
            return subscription_update
        return MagicMock()

    supabase = MagicMock()
    supabase.table.side_effect = table_side_effect

    with patch("backend.tasks.cleanup.ensure_supabase", return_value=supabase):
        with patch(
            "backend.tasks.cleanup.run_kubectl", new=AsyncMock(side_effect=[(0, "", ""), (1, "", "synapse error")])
        ) as kubectl:
            result = await cleanup_unentitled_instances()

    assert result["instances_stopped"] == 0
    assert result["subscriptions_paused"] == 1
    assert result["errors"] == 1
    kubectl.assert_has_awaits(
        [
            call(["scale", "deployment/mindroom-123", "--replicas=0"], namespace="mindroom-instances"),
            call(["scale", "deployment/synapse-123", "--replicas=0"], namespace="mindroom-instances"),
        ]
    )
    instance_update.update.assert_not_called()
    subscription_update.update.assert_called_once()
    assert subscription_update.update.call_args[0][0]["status"] == "paused"
