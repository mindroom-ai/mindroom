"""Utilities for provisioner operations."""

from __future__ import annotations

from typing import Any

from backend.config import PROVISIONER_API_KEY, logger
from backend.db_utils import update_instance_status
from backend.k8s import check_deployment_exists, run_kubectl
from fastapi import HTTPException


async def _execute_instance_action(
    instance_id: int,
    action: str,
    kubectl_args: list[str],
    target_status: str | None,
    authorization: str | None,
) -> dict[str, Any]:
    """Generic instance action executor."""
    if authorization != f"Bearer {PROVISIONER_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    logger.info("%s instance %s", action.capitalize(), instance_id)

    if not await check_deployment_exists(instance_id):
        error_msg = f"Deployment mindroom-backend-{instance_id} not found"
        logger.warning(error_msg)
        raise HTTPException(status_code=404, detail=error_msg)

    try:
        code, out, err = await run_kubectl(kubectl_args, namespace="mindroom-instances")
        if code != 0:
            msg = f"kubectl command failed: {err}"
            raise RuntimeError(msg)  # noqa: TRY301

        logger.info("%s instance %s: %s", action.capitalize(), instance_id, out)

        # Update database status if specified
        if target_status and not update_instance_status(instance_id, target_status):
            logger.warning("Failed to update DB status to %s for instance %s", target_status, instance_id)

    except Exception as e:
        logger.exception("Failed to %s instance %s", action, instance_id)
        raise HTTPException(status_code=500, detail=f"Failed to {action} instance: {e}") from e

    return {"success": True, "message": f"Instance {instance_id} {action}d successfully"}


async def start_instance(instance_id: int, authorization: str | None = None) -> dict[str, Any]:
    """Start an instance."""
    return await _execute_instance_action(
        instance_id=instance_id,
        action="start",
        kubectl_args=["scale", f"deployment/mindroom-backend-{instance_id}", "--replicas=1"],
        target_status="running",
        authorization=authorization,
    )


async def stop_instance(instance_id: int, authorization: str | None = None) -> dict[str, Any]:
    """Stop an instance."""
    return await _execute_instance_action(
        instance_id=instance_id,
        action="stop",
        kubectl_args=["scale", f"deployment/mindroom-backend-{instance_id}", "--replicas=0"],
        target_status="stopped",
        authorization=authorization,
    )


async def restart_instance(instance_id: int, authorization: str | None = None) -> dict[str, Any]:
    """Restart an instance."""
    return await _execute_instance_action(
        instance_id=instance_id,
        action="restart",
        kubectl_args=["rollout", "restart", f"deployment/mindroom-backend-{instance_id}"],
        target_status=None,  # Restart doesn't change status
        authorization=authorization,
    )
