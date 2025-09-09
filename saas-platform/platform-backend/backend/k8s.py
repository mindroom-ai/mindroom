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
