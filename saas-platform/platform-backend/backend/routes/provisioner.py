from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from backend.config import PLATFORM_DOMAIN, PROVISIONER_API_KEY, logger
from backend.deps import ensure_supabase
from backend.k8s import check_deployment_exists, run_kubectl, wait_for_deployment_ready
from backend.process import run_helm
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException

router = APIRouter()


async def _background_mark_running_when_ready(instance_id: str, namespace: str = "mindroom-instances") -> None:
    """Background task: wait longer and mark instance running when ready."""
    try:
        ready = await wait_for_deployment_ready(instance_id, namespace=namespace, timeout_seconds=600)
        if ready:
            try:
                sb = ensure_supabase()
                sb.table("instances").update(
                    {"status": "running", "updated_at": datetime.now(UTC).isoformat()},
                ).eq("instance_id", instance_id).execute()
            except Exception:
                logger.warning("Background update: failed to mark instance %s as running", instance_id)
    except Exception:
        logger.exception("Background readiness wait failed for instance %s", instance_id)


@router.post("/api/v1/provision")
async def provision_instance(
    data: dict,
    authorization: Annotated[str | None, Header()] = None,
    background_tasks: BackgroundTasks | None = None,
) -> dict[str, Any]:
    """Provision a new instance (compatible with customer portal)."""
    if authorization != f"Bearer {PROVISIONER_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    sb = ensure_supabase()

    subscription_id = data.get("subscription_id")
    account_id = data.get("account_id")
    tier = data.get("tier", "free")

    # Generate a numeric customer ID from next available subdomain digit
    result = sb.table("instances").select("subdomain").execute()
    existing_ids: list[int] = []
    for instance in result.data or []:
        subdomain = instance.get("subdomain", "")
        if subdomain.isdigit():
            existing_ids.append(int(subdomain))
    next_id = max(existing_ids) + 1 if existing_ids else 1
    customer_id = str(next_id)

    helm_release_name = f"instance-{customer_id}"
    logger.info("Provisioning instance for subscription %s, tier: %s", subscription_id, tier)

    namespace = "mindroom-instances"
    try:
        await run_kubectl(["create", "namespace", namespace])
    except FileNotFoundError:
        error_msg = "Kubectl command not found. Kubernetes provisioning not available in this environment."
        logger.exception(error_msg)
        raise HTTPException(status_code=503, detail=error_msg) from None
    except Exception as e:
        logger.warning("Could not create namespace (may already exist): %s", e)

    logger.info("Deploying instance %s to namespace %s", customer_id, namespace)

    # Pre-create DB record as provisioning with URLs; update status later
    base_domain = PLATFORM_DOMAIN
    frontend_url = f"https://{customer_id}.{base_domain}"
    api_url = f"https://{customer_id}.api.{base_domain}"
    matrix_url = f"https://{customer_id}.matrix.{base_domain}"

    try:
        sb.table("instances").insert(
            {
                "subscription_id": subscription_id,
                "account_id": account_id,
                "instance_id": customer_id,
                "subdomain": customer_id,
                "status": "provisioning",
                "tier": tier,
                "instance_url": frontend_url,
                "frontend_url": frontend_url,
                "backend_url": api_url,
                "api_url": api_url,
                "matrix_url": matrix_url,
                "matrix_server_url": matrix_url,
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            },
        ).execute()
    except Exception:
        logger.exception("Failed to create instance record in database (pre-provision)")

    try:
        # Install charts without waiting; we'll poll readiness ourselves
        code, stdout, stderr = await run_helm(
            [
                "install",
                helm_release_name,
                "/app/k8s/instance/",
                "--namespace",
                namespace,
                "--create-namespace",
                "--set",
                f"customer={customer_id}",
                "--set",
                f"baseDomain={PLATFORM_DOMAIN}",
                "--set",
                "mindroom_image=git.nijho.lt/basnijholt/mindroom-frontend:latest",
            ],
        )
        if code != 0:
            # Mark as error in DB
            try:
                sb.table("instances").update(
                    {"status": "error", "updated_at": datetime.now(UTC).isoformat()},
                ).eq("instance_id", customer_id).execute()
            except Exception:
                logger.warning("Failed to update instance status to error after helm failure")
            raise HTTPException(status_code=500, detail=f"Helm install failed: {stderr}")
        logger.info("Helm install output: %s", stdout)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to deploy instance")
        # Mark as error in DB
        try:
            sb.table("instances").update(
                {"status": "error", "updated_at": datetime.now(UTC).isoformat()},
            ).eq("instance_id", customer_id).execute()
        except Exception:
            logger.warning("Failed to update instance status to error after deploy exception")
        raise HTTPException(status_code=500, detail=f"Failed to deploy instance: {e!s}") from e

    # Optional readiness poll; if ready, mark running. Otherwise remain provisioning.
    ready = await wait_for_deployment_ready(customer_id, namespace=namespace, timeout_seconds=180)
    try:
        sb.table("instances").update(
            {
                "status": "running" if ready else "provisioning",
                "updated_at": datetime.now(UTC).isoformat(),
            },
        ).eq("instance_id", customer_id).execute()
    except Exception:
        logger.warning("Failed to update instance status after readiness poll")

    auth_token = ""  # Placeholder for future per-instance tokens
    if not ready and background_tasks is not None:
        # Fire-and-forget longer background wait to mark running later
        try:
            background_tasks.add_task(_background_mark_running_when_ready, customer_id, namespace)
        except Exception:
            logger.warning("Failed to schedule background readiness task for instance %s", customer_id)

    return {
        "customer_id": customer_id,
        "frontend_url": frontend_url,
        "api_url": api_url,
        "matrix_url": matrix_url,
        "auth_token": auth_token,
        "success": True,
        "message": "Instance provisioned successfully" if ready else "Provisioning started; instance is getting ready",
    }


@router.post("/api/v1/start/{instance_id}")
async def start_instance_provisioner(
    instance_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Start an instance (provisioner API compatible)."""
    if authorization != f"Bearer {PROVISIONER_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    logger.info("Starting instance %s", instance_id)

    if not await check_deployment_exists(instance_id):
        error_msg = f"Deployment mindroom-backend-{instance_id} not found"
        logger.warning(error_msg)
        raise HTTPException(status_code=404, detail=error_msg)

    try:
        code, out, err = await run_kubectl(
            [
                "scale",
                f"deployment/mindroom-backend-{instance_id}",
                "--replicas=1",
            ],
            namespace="mindroom-instances",
        )
        if code != 0:
            raise Exception(err)
        logger.info("Started instance %s: %s", instance_id, out)
    except Exception as e:
        logger.exception("Failed to start instance %s", instance_id)
        raise HTTPException(status_code=500, detail=f"Failed to start instance: {e}") from e

    return {"success": True, "message": f"Instance {instance_id} started successfully"}


@router.post("/api/v1/stop/{instance_id}")
async def stop_instance_provisioner(
    instance_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Stop an instance (provisioner API compatible)."""
    if authorization != f"Bearer {PROVISIONER_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    logger.info("Stopping instance %s", instance_id)

    if not await check_deployment_exists(instance_id):
        error_msg = f"Deployment mindroom-backend-{instance_id} not found"
        logger.warning(error_msg)
        raise HTTPException(status_code=404, detail=error_msg)

    try:
        code, out, err = await run_kubectl(
            [
                "scale",
                f"deployment/mindroom-backend-{instance_id}",
                "--replicas=0",
            ],
            namespace="mindroom-instances",
        )
        if code != 0:
            raise Exception(err)
        logger.info("Stopped instance %s: %s", instance_id, out)
    except Exception as e:
        logger.exception("Failed to stop instance %s", instance_id)
        raise HTTPException(status_code=500, detail=f"Failed to stop instance: {e}") from e

    return {"success": True, "message": f"Instance {instance_id} stopped successfully"}


@router.post("/api/v1/restart/{instance_id}")
async def restart_instance_provisioner(
    instance_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Restart an instance (provisioner API compatible)."""
    if authorization != f"Bearer {PROVISIONER_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    logger.info("Restarting instance %s", instance_id)

    if not await check_deployment_exists(instance_id):
        error_msg = f"Deployment mindroom-backend-{instance_id} not found"
        logger.warning(error_msg)
        raise HTTPException(status_code=404, detail=error_msg)

    try:
        code, out, err = await run_kubectl(
            [
                "rollout",
                "restart",
                f"deployment/mindroom-backend-{instance_id}",
            ],
            namespace="mindroom-instances",
        )
        if code != 0:
            raise Exception(err)
        logger.info("Restarted instance %s: %s", instance_id, out)
    except Exception as e:
        logger.exception("Failed to restart instance %s", instance_id)
        raise HTTPException(status_code=500, detail=f"Failed to restart instance: {e}") from e

    return {"success": True, "message": f"Instance {instance_id} restarted successfully"}


@router.delete("/api/v1/uninstall/{instance_id}")
async def uninstall_instance(
    instance_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Completely uninstall/deprovision an instance."""
    if authorization != f"Bearer {PROVISIONER_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    logger.info("Uninstalling instance %s", instance_id)

    try:
        helm_release_name = f"instance-{instance_id}" if instance_id.isdigit() else instance_id

        code, stdout, stderr = await run_helm(["uninstall", helm_release_name, "--namespace=mindroom-instances"])

        if code != 0:
            error_msg = stderr
            if "not found" not in error_msg.lower():
                logger.error("Failed to uninstall instance: %s", error_msg)
                raise HTTPException(status_code=500, detail=f"Failed to uninstall instance: {error_msg}")

            if instance_id.isdigit():
                logger.info("Trying old naming convention for instance %s", instance_id)
                code2, stdout2, stderr2 = await run_helm(
                    ["uninstall", instance_id, "--namespace=mindroom-instances"],
                )
                if code2 != 0:
                    error_msg2 = stderr2
                    if "not found" not in error_msg2.lower():
                        logger.error("Failed to uninstall with old naming: %s", error_msg2)
                    logger.info("Instance %s was already uninstalled", instance_id)
                else:
                    logger.info("Successfully uninstalled instance %s with old naming", instance_id)
            else:
                logger.info("Instance %s was already uninstalled", instance_id)
        else:
            logger.info("Successfully uninstalled instance %s: %s", instance_id, stdout)

        try:
            sb = ensure_supabase()
            sb.table("instances").update(
                {
                    "status": "deprovisioned",
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            ).or_(f"instance_id.eq.{instance_id},subdomain.eq.{instance_id}").execute()
        except Exception as e:
            logger.warning("Failed to update database for instance %s: %s", instance_id, e)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to uninstall instance %s", instance_id)
        raise HTTPException(status_code=500, detail=f"Failed to uninstall instance: {e}") from e

    return {"success": True, "message": f"Instance {instance_id} uninstalled successfully", "instance_id": instance_id}


@router.post("/api/v1/sync-instances")
async def sync_instances(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Sync instance states between database and Kubernetes cluster."""
    if authorization != f"Bearer {PROVISIONER_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    sb = ensure_supabase()

    logger.info("Starting instance sync")

    try:
        result = sb.table("instances").select("*").execute()
        instances = result.data if result.data else []

        sync_results: dict[str, Any] = {
            "total": len(instances),
            "synced": 0,
            "errors": 0,
            "updates": [],
        }

        for instance in instances:
            instance_id = instance.get("instance_id") or instance.get("subdomain")
            if not instance_id:
                logger.warning("Instance %s has no instance_id or subdomain", instance.get("id"))
                sync_results["errors"] += 1
                continue

            exists = await check_deployment_exists(instance_id)
            current_status = instance.get("status", "unknown")

            if not exists:
                if current_status not in ["error", "deprovisioned"]:
                    logger.info("Instance %s not found in cluster, marking as error", instance_id)
                    sb.table("instances").update(
                        {
                            "status": "error",
                            "updated_at": datetime.now(UTC).isoformat(),
                        },
                    ).eq("id", instance["id"]).execute()

                    sync_results["updates"].append(
                        {
                            "instance_id": instance_id,
                            "old_status": current_status,
                            "new_status": "error",
                            "reason": "deployment_not_found",
                        },
                    )
                    sync_results["synced"] += 1
            else:
                try:
                    code, out, _ = await run_kubectl(
                        [
                            "get",
                            f"deployment/mindroom-backend-{instance_id}",
                            "-o=jsonpath={.spec.replicas}",
                        ],
                        namespace="mindroom-instances",
                    )
                    if code == 0:
                        replicas = int(out.strip() or "0")
                        actual_status = "running" if replicas > 0 else "stopped"

                        if current_status != actual_status:
                            logger.info(
                                "Instance %s status mismatch: DB=%s, K8s=%s",
                                instance_id,
                                current_status,
                                actual_status,
                            )
                            sb.table("instances").update(
                                {
                                    "status": actual_status,
                                    "updated_at": datetime.now(UTC).isoformat(),
                                },
                            ).eq("id", instance["id"]).execute()

                            sync_results["updates"].append(
                                {
                                    "instance_id": instance_id,
                                    "old_status": current_status,
                                    "new_status": actual_status,
                                    "reason": "status_mismatch",
                                },
                            )
                            sync_results["synced"] += 1
                except Exception:
                    logger.exception("Error checking instance %s state", instance_id)
                    sync_results["errors"] += 1

        logger.info("Instance sync completed: %s", sync_results)
        return sync_results
    except Exception as e:
        logger.exception("Failed to sync instances")
        raise HTTPException(status_code=500, detail=f"Failed to sync instances: {e}") from e
