"""Helpers for interacting with Kubernetes via subprocess."""

from __future__ import annotations

import json
from typing import Any

from backend.config import logger
from backend.process import run_cmd

INSTANCES_NAMESPACE = "mindroom-instances"


def instance_workload_name(instance_id: str | int) -> str:
    """Return the Kubernetes workload name for a MindRoom instance."""
    return f"mindroom-{instance_id}"


def instance_deployment_ref(instance_id: str | int) -> str:
    """Return the Kubernetes deployment reference for a MindRoom instance."""
    return f"deployment/{instance_workload_name(instance_id)}"


def homeserver_workload_name(instance_id: str | int) -> str:
    """Return the Kubernetes workload name for an instance homeserver."""
    return f"tuwunel-{instance_id}"


def homeserver_deployment_ref(instance_id: str | int) -> str:
    """Return the Kubernetes deployment reference for an instance homeserver."""
    return f"deployment/{homeserver_workload_name(instance_id)}"


def tenant_start_deployment_refs(instance_id: str | int) -> tuple[str, str]:
    """Return tenant deployments in startup order."""
    return (homeserver_deployment_ref(instance_id), instance_deployment_ref(instance_id))


def tenant_stop_deployment_refs(instance_id: str | int) -> tuple[str, str]:
    """Return tenant deployments in shutdown order."""
    return (instance_deployment_ref(instance_id), homeserver_deployment_ref(instance_id))


async def get_deployment(instance_id: str | int) -> dict[str, Any] | None:
    """Return the instance's MindRoom deployment as Kubernetes reports it, or None when it does not exist.

    Raises RuntimeError when kubectl fails, so a failed lookup never reads as a missing deployment.
    """
    deployment_ref = instance_deployment_ref(instance_id)
    code, out, err = await run_kubectl(
        ["get", deployment_ref, "-o=json", "--ignore-not-found"], namespace=INSTANCES_NAMESPACE
    )
    if code != 0:
        msg = f"kubectl get {deployment_ref} failed: {err or out}"
        raise RuntimeError(msg)
    return json.loads(out) if out.strip() else None


async def check_deployment_exists(instance_id: str | int) -> bool:
    """Return whether the instance's MindRoom deployment exists; raises RuntimeError when kubectl fails."""
    return await get_deployment(instance_id) is not None


async def wait_for_deployment_ready(
    instance_id: str, namespace: str = INSTANCES_NAMESPACE, timeout_seconds: int = 120
) -> bool:
    """Block until the instance deployment reports ready or timeout.

    Uses `kubectl rollout status` which waits for the deployment to complete its rollout.
    Returns True if ready; False on timeout or error.
    """
    try:
        deployment_ref = instance_deployment_ref(instance_id)
        code, out, err = await run_kubectl(
            ["rollout", "status", deployment_ref, f"--timeout={timeout_seconds}s"], namespace=namespace
        )
        if code == 0:
            logger.info("Deployment %s ready: %s", instance_id, out)
            return True
        logger.warning("Deployment %s not ready within timeout: %s", instance_id, err or out)
        return False  # noqa: TRY300
    except FileNotFoundError:
        logger.exception("kubectl not found when waiting for deployment readiness")
        return False
    except Exception:
        logger.exception("Error waiting for deployment readiness")
        return False


async def run_kubectl(args: list[str], namespace: str | None = None) -> tuple[int, str, str]:
    """Run a kubectl command and return (returncode, stdout, stderr) as strings.

    - args: positional arguments passed to kubectl (e.g., ["get", "pods"]).
    - namespace: if provided, appended as --namespace=<namespace>.
    """
    cmd = ["kubectl", *args]
    if namespace:
        cmd.append(f"--namespace={namespace}")
    return await run_cmd(cmd)
