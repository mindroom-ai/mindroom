"""Instances the soft delete of older releases marked deprovisioned while their deployment kept running.

The subscription lifecycle in `instance_lifecycle.py` owns what happens to such an instance once it is recognized;
this module only recognizes it and records that it still runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from backend.config import logger
from backend.k8s import check_deployment_exists
from backend.services.instances_data import update_instance

if TYPE_CHECKING:
    from supabase import Client


# LEGACY_COMPAT: Instances a soft delete marked deprovisioned while their deployment kept running.
# Legacy format: an `instances` row with status `deprovisioned`, no `lifecycle_stopped_at`, and a live deployment,
#   written by `soft_delete_account` for every running instance of an account whose deletion was requested.
# Last legacy release: v2026.9.373; replacement: v2026.9.374, whose migration 005 makes the soft delete change only
#   the account while the subscription lifecycle holds the instances; a database without migration 005 keeps the old
#   soft delete whatever the backend release.
# Handling: the nightly run, and any run for an account pending deletion, marks such a row running again, since
#   its deployment still runs; the lifecycle then holds it, or keeps it running for an entitled subscription.
#   A deprovisioned row without a deployment is already torn down and is left alone.
# Coverage: saas-platform/platform-backend/tests/test_instance_lifecycle.py::test_legacy_soft_deleted_instance_that_kept_running_is_held,
#   saas-platform/platform-backend/tests/test_instance_lifecycle.py::test_legacy_soft_deleted_instance_of_a_paying_restored_account_runs
async def legacy_soft_deleted_deployment_exists(instance: dict[str, Any]) -> bool:
    """Return whether the row is a legacy soft-deleted instance whose deployment still exists."""
    if instance.get("status") != "deprovisioned" or instance.get("lifecycle_stopped_at") is not None:
        return False  # A lifecycle teardown sets the hold first.
    return await check_deployment_exists(str(instance["instance_id"]))


def adopt_legacy_soft_deleted_instance(sb: Client, instance: dict[str, Any]) -> dict[str, Any]:
    """Mark a legacy soft-deleted instance running again, as its deployment is, and return the updated row."""
    logger.warning("Instance %s was marked deprovisioned but still runs; marking it running", instance["instance_id"])
    update_instance(sb, instance["instance_id"], {"status": "running"})
    return {**instance, "status": "running"}
