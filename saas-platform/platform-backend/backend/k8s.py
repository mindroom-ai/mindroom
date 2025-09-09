"""Helpers for interacting with Kubernetes via subprocess."""

from __future__ import annotations

import asyncio

from backend.config import logger


async def check_deployment_exists(instance_id: str, namespace: str = "mindroom-instances") -> bool:
    """Check if a Kubernetes deployment exists for an instance."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "kubectl",
            "get",
            f"deployment/mindroom-backend-{instance_id}",
            f"--namespace={namespace}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            error_msg = stderr.decode()
            if "not found" in error_msg.lower() or "notfound" in error_msg.lower():
                logger.info("Deployment mindroom-backend-%s not found in namespace %s", instance_id, namespace)
                return False
            return False
        return proc.returncode == 0
    except Exception:
        logger.exception("Error checking deployment existence")
        return False


async def wait_for_deployment_ready(
    instance_id: str,
    namespace: str = "mindroom-instances",
    timeout_seconds: int = 120,
) -> bool:
    """Block until the instance deployment reports ready or timeout.

    Uses `kubectl rollout status` which waits for the deployment to complete its rollout.
    Returns True if ready; False on timeout or error.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "kubectl",
            "rollout",
            "status",
            f"deployment/mindroom-backend-{instance_id}",
            f"--namespace={namespace}",
            f"--timeout={timeout_seconds}s",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            logger.info("Deployment %s ready: %s", instance_id, stdout.decode())
            return True
        logger.warning(
            "Deployment %s not ready within timeout: %s",
            instance_id,
            stderr.decode() or stdout.decode(),
        )
        return False
    except FileNotFoundError:
        logger.exception("kubectl not found when waiting for deployment readiness")
        return False
    except Exception:
        logger.exception("Error waiting for deployment readiness")
        return False
