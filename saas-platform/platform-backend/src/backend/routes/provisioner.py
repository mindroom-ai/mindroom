"""Instance provisioning and management routes."""

import hmac
from datetime import UTC, datetime
from typing import Annotated, Any

from backend.config import (
    ANTHROPIC_API_KEY,
    GITEA_TOKEN,
    GITEA_USER,
    GOOGLE_API_KEY,
    DEEPSEEK_API_KEY,
    OPENAI_API_KEY,
    OPENROUTER_API_KEY,
    PLATFORM_DOMAIN,
    PROVISIONER_API_KEY,
    SUPABASE_ANON_KEY,
    SUPABASE_SERVICE_KEY,
    SUPABASE_URL,
    logger,
)
from backend.db_utils import update_instance_status
from backend.deps import _extract_bearer_token, ensure_supabase, limiter
from backend.k8s import (
    check_deployment_exists,
    ensure_docker_registry_secret,
    run_kubectl,
    wait_for_deployment_ready,
)
from backend.models import ActionResult, ProvisionResponse, SyncResult, SyncUpdateOut
from backend.process import run_helm
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

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
                logger.warning(
                    "Background update: failed to mark instance %s as running",
                    instance_id,
                )
    except Exception:
        logger.exception("Background readiness wait failed for instance %s", instance_id)


def _require_provisioner_auth(authorization: str | None) -> None:
    """Validate provisioner API key using constant-time comparison."""
    try:
        token = _extract_bearer_token(authorization)
    except HTTPException:
        raise HTTPException(status_code=401, detail="Unauthorized") from None
    if not PROVISIONER_API_KEY:
        logger.error("PROVISIONER_API_KEY is not configured")
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not hmac.compare_digest(token, PROVISIONER_API_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.post("/system/provision", response_model=ProvisionResponse)
@limiter.limit("5/minute")
async def provision_instance(  # noqa: C901, PLR0912, PLR0915
    request: Request,  # noqa: ARG001
    data: dict,
    authorization: Annotated[str | None, Header()] = None,
    background_tasks: BackgroundTasks = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Provision a new instance (compatible with customer portal)."""
    _require_provisioner_auth(authorization)

    sb = ensure_supabase()

    subscription_id = data.get("subscription_id")
    account_id = data.get("account_id")
    tier = data.get("tier", "free")
    existing_instance_id = data.get("instance_id")  # For re-provisioning

    # If re-provisioning, update existing instance; otherwise insert new
    if existing_instance_id:
        customer_id = str(existing_instance_id)
        try:
            update_res = (
                sb.table("instances")
                .update(
                    {
                        "status": "provisioning",
                        "updated_at": datetime.now(UTC).isoformat(),
                    },
                )
                .eq("instance_id", customer_id)
                .execute()
            )
            if not update_res.data:
                msg = f"Instance {customer_id} not found"
                raise HTTPException(status_code=404, detail=msg)  # noqa: TRY301
            logger.info("Re-provisioning existing instance %s", customer_id)
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Failed to update instance for re-provisioning")
            raise HTTPException(status_code=500, detail=f"Failed to update instance: {e!s}") from e
    else:
        # Insert instance first to get a generated numeric instance_id (as text)
        try:
            now = datetime.now(UTC).isoformat()
            insert_res = (
                sb.table("instances")
                .insert(
                    {
                        "subscription_id": subscription_id,
                        "account_id": account_id,
                        "status": "provisioning",
                        "tier": tier,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                .execute()
            )
            if not insert_res.data:
                msg = "Failed to insert instance"
                raise HTTPException(status_code=500, detail=msg)  # noqa: TRY301
            customer_id = insert_res.data[0]["instance_id"]
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Failed to insert instance")
            raise HTTPException(status_code=500, detail=f"Failed to insert instance: {e!s}") from e

    helm_release_name = f"instance-{customer_id}"
    logger.info(
        "Provisioning instance for subscription %s, new id: %s, tier: %s",
        subscription_id,
        customer_id,
        tier,
    )

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

    # Compute URLs and persist them (subdomain is set via trigger if null)
    frontend_url = f"https://{customer_id}.{PLATFORM_DOMAIN}"
    api_url = f"https://{customer_id}.api.{PLATFORM_DOMAIN}"
    matrix_url = f"https://{customer_id}.matrix.{PLATFORM_DOMAIN}"
    try:
        sb.table("instances").update(
            {
                "instance_url": frontend_url,
                "frontend_url": frontend_url,
                "backend_url": api_url,
                "api_url": api_url,
                "matrix_url": matrix_url,
                "matrix_server_url": matrix_url,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        ).eq("instance_id", customer_id).execute()
    except Exception:
        logger.warning("Failed to update URLs for instance %s", customer_id)

    # Ensure image pull secret exists in namespace for private registry
    if GITEA_USER and GITEA_TOKEN:
        success = await ensure_docker_registry_secret(
            secret_name="gitea-registry",  # noqa: S106
            server="git.nijho.lt",
            username=GITEA_USER,
            password=GITEA_TOKEN,
            namespace=namespace,
        )
        if not success:
            logger.warning("Failed to create image pull secret, deployment may fail")

    try:
        # Use upgrade --install to handle both new and re-provisioning cases
        code, stdout, stderr = await run_helm(
            [
                "upgrade",
                "--install",
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
                f"accountId={account_id}",
                "--set",
                f"supabaseUrl={SUPABASE_URL or ''}",
                "--set",
                f"supabaseAnonKey={SUPABASE_ANON_KEY or ''}",
                "--set",
                f"supabaseServiceKey={SUPABASE_SERVICE_KEY or ''}",
                "--set",
                f"openai_key={OPENAI_API_KEY}",
                "--set",
                f"anthropic_key={ANTHROPIC_API_KEY}",
                "--set",
                f"google_key={GOOGLE_API_KEY}",
                "--set",
                f"openrouter_key={OPENROUTER_API_KEY}",
                "--set",
                f"deepseek_key={DEEPSEEK_API_KEY}",
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
            msg = f"Helm install failed: {stderr}"
            raise HTTPException(status_code=500, detail=msg)  # noqa: TRY301
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

    if not ready and background_tasks is not None:
        # Fire-and-forget longer background wait to mark running later
        try:
            background_tasks.add_task(_background_mark_running_when_ready, customer_id, namespace)
        except Exception:
            logger.warning(
                "Failed to schedule background readiness task for instance %s",
                customer_id,
            )

    return {
        "customer_id": customer_id,
        "frontend_url": frontend_url,
        "api_url": api_url,
        "matrix_url": matrix_url,
        "success": True,
        "message": "Instance provisioned successfully" if ready else "Provisioning started; instance is getting ready",
    }


@router.post("/system/instances/{instance_id}/start", response_model=ActionResult)
@limiter.limit("10/minute")
async def start_instance_provisioner(
    request: Request,  # noqa: ARG001
    instance_id: int,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Start an instance (provisioner API compatible)."""
    _require_provisioner_auth(authorization)

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
            msg = f"kubectl command failed: {err}"
            raise RuntimeError(msg)  # noqa: TRY301
        logger.info("Started instance %s: %s", instance_id, out)
        # Reflect desired state in DB immediately
        if not update_instance_status(instance_id, "running"):
            logger.warning("Failed to update DB status to running for instance %s", instance_id)
    except Exception as e:
        logger.exception("Failed to start instance %s", instance_id)
        raise HTTPException(status_code=500, detail=f"Failed to start instance: {e}") from e

    return {"success": True, "message": f"Instance {instance_id} started successfully"}


@router.post("/system/instances/{instance_id}/stop", response_model=ActionResult)
@limiter.limit("10/minute")
async def stop_instance_provisioner(
    request: Request,  # noqa: ARG001
    instance_id: int,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Stop an instance (provisioner API compatible)."""
    _require_provisioner_auth(authorization)

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
            msg = f"kubectl command failed: {err}"
            raise RuntimeError(msg)  # noqa: TRY301
        logger.info("Stopped instance %s: %s", instance_id, out)
        # Reflect desired state in DB immediately
        if not update_instance_status(instance_id, "stopped"):
            logger.warning("Failed to update DB status to stopped for instance %s", instance_id)
    except Exception as e:
        logger.exception("Failed to stop instance %s", instance_id)
        raise HTTPException(status_code=500, detail=f"Failed to stop instance: {e}") from e

    return {"success": True, "message": f"Instance {instance_id} stopped successfully"}


@router.post("/system/instances/{instance_id}/restart", response_model=ActionResult)
@limiter.limit("10/minute")
async def restart_instance_provisioner(
    request: Request,  # noqa: ARG001
    instance_id: int,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Restart an instance (provisioner API compatible)."""
    _require_provisioner_auth(authorization)

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
            msg = f"kubectl command failed: {err}"
            raise RuntimeError(msg)  # noqa: TRY301
        logger.info("Restarted instance %s: %s", instance_id, out)
    except Exception as e:
        logger.exception("Failed to restart instance %s", instance_id)
        raise HTTPException(status_code=500, detail=f"Failed to restart instance: {e}") from e

    return {
        "success": True,
        "message": f"Instance {instance_id} restarted successfully",
    }


@router.delete("/system/instances/{instance_id}/uninstall", response_model=ActionResult)
@limiter.limit("2/minute")
async def uninstall_instance(
    request: Request,  # noqa: ARG001
    instance_id: int,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Completely uninstall/deprovision an instance."""
    _require_provisioner_auth(authorization)

    logger.info("Uninstalling instance %s", instance_id)

    try:
        helm_release_name = f"instance-{instance_id}"
        code, stdout, stderr = await run_helm(["uninstall", helm_release_name, "--namespace=mindroom-instances"])

        if code != 0:
            error_msg = stderr
            if "not found" in error_msg.lower():
                logger.info("Instance %s was already uninstalled", instance_id)
            else:
                logger.error("Failed to uninstall instance: %s", error_msg)
                msg = f"Failed to uninstall instance: {error_msg}"
                raise HTTPException(status_code=500, detail=msg)  # noqa: TRY301
        else:
            logger.info("Successfully uninstalled instance %s: %s", instance_id, stdout)

        if not update_instance_status(instance_id, "deprovisioned"):
            logger.warning("Failed to update database for instance %s", instance_id)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to uninstall instance %s", instance_id)
        raise HTTPException(status_code=500, detail=f"Failed to uninstall instance: {e}") from e

    return {
        "success": True,
        "message": f"Instance {instance_id} uninstalled successfully",
        "instance_id": instance_id,
    }


@router.post("/system/sync-instances", response_model=SyncResult)
@limiter.limit("5/minute")
async def sync_instances(
    request: Request,  # noqa: ARG001
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Sync instance states between database and Kubernetes cluster."""
    _require_provisioner_auth(authorization)

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
                    logger.info(
                        "Instance %s not found in cluster, marking as error",
                        instance_id,
                    )
                    now = datetime.now(UTC).isoformat()
                    sb.table("instances").update(
                        {
                            "status": "error",
                            "kubernetes_synced_at": now,
                            "updated_at": now,
                        },
                    ).eq("id", instance["id"]).execute()

                    sync_results["updates"].append(
                        SyncUpdateOut(
                            instance_id=instance_id,
                            old_status=current_status,
                            new_status="error",
                            reason="deployment_not_found",
                        ).model_dump(),
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
                            now = datetime.now(UTC).isoformat()
                            sb.table("instances").update(
                                {
                                    "status": actual_status,
                                    "kubernetes_synced_at": now,
                                    "updated_at": now,
                                },
                            ).eq("id", instance["id"]).execute()

                            sync_results["updates"].append(
                                SyncUpdateOut(
                                    instance_id=instance_id,
                                    old_status=current_status,
                                    new_status=actual_status,
                                    reason="status_mismatch",
                                ).model_dump(),
                            )
                            sync_results["synced"] += 1
                except Exception:
                    logger.exception("Error checking instance %s state", instance_id)
                    sync_results["errors"] += 1

        logger.info("Instance sync completed: %s", sync_results)
        return sync_results  # noqa: TRY300
    except Exception as e:
        logger.exception("Failed to sync instances")
        raise HTTPException(status_code=500, detail=f"Failed to sync instances: {e}") from e
