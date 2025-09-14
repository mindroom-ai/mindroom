"""Helpers for interacting with Kubernetes via subprocess."""

from __future__ import annotations

from backend.config import logger
from backend.process import run_cmd


async def check_deployment_exists(instance_id: str, namespace: str = "mindroom-instances") -> bool:
    """Check if a Kubernetes deployment exists for an instance."""
    try:
        code, _out, err = await run_kubectl(["get", f"deployment/mindroom-backend-{instance_id}"], namespace=namespace)
        if code != 0:
            if "not found" in err.lower() or "notfound" in err.lower():
                logger.info("Deployment mindroom-backend-%s not found in namespace %s", instance_id, namespace)
            return False
        return True  # noqa: TRY300
    except Exception:
        logger.exception("Error checking deployment existence")
        return False


async def wait_for_deployment_ready(
    instance_id: str, namespace: str = "mindroom-instances", timeout_seconds: int = 120
) -> bool:
    """Block until the instance deployment reports ready or timeout.

    Uses `kubectl rollout status` which waits for the deployment to complete its rollout.
    Returns True if ready; False on timeout or error.
    """
    try:
        code, out, err = await run_kubectl(
            ["rollout", "status", f"deployment/mindroom-backend-{instance_id}", f"--timeout={timeout_seconds}s"],
            namespace=namespace,
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


async def ensure_docker_registry_secret(
    secret_name: str, server: str, username: str, password: str, namespace: str = "mindroom-instances"
) -> bool:
    """Ensure a docker-registry secret exists; create if missing.

    Returns True if the secret exists (already or created), False on creation failure.
    """
    try:
        code, _out, _err = await run_kubectl(["get", "secret", secret_name], namespace=namespace)
        if code == 0:
            return True
        # Create the secret
        args = [
            "create",
            "secret",
            "docker-registry",
            secret_name,
            f"--docker-server={server}",
            f"--docker-username={username}",
            f"--docker-password={password}",
        ]
        code, out, err = await run_kubectl(args, namespace=namespace)
        if code != 0:
            logger.error("Failed to create imagePullSecret %s: %s", secret_name, err or out)
            return False
        logger.info("Created imagePullSecret %s in namespace %s", secret_name, namespace)
        return True  # noqa: TRY300
    except Exception:
        logger.exception("Error ensuring imagePullSecret %s", secret_name)
        return False
