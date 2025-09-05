"""Provisioning endpoints for MindRoom instances."""

import logging
import os
import secrets
import string
import time

from fastapi import APIRouter, BackgroundTasks, HTTPException

from ..config import settings
from ..dokku.client import dokku_client
from ..models import (
    DeprovisionRequest,
    DeprovisionResponse,
    InstanceInfo,
    ProvisionRequest,
    ProvisionResponse,
    UpdateRequest,
    UpdateResponse,
)
from ..services.config_generator import generate_mindroom_config, save_config_to_file
from ..services.supabase import get_instance_info, log_event, update_instance_status

router = APIRouter()
logger = logging.getLogger(__name__)


def generate_app_name(subscription_id: str) -> str:
    """Generate unique app name for Dokku.

    App names must be lowercase alphanumeric with hyphens.
    """
    suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
    # Take first 8 chars of subscription ID and clean it
    sub_prefix = subscription_id[:8].lower()
    sub_prefix = "".join(c if c.isalnum() else "" for c in sub_prefix)
    return f"mr-{sub_prefix}-{suffix}"


def generate_password() -> str:
    """Generate secure password for admin user."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(20))


def calculate_resource_limits(tier: str) -> dict:
    """Calculate resource limits based on tier."""
    limits = {
        "free": {"memory": "256m", "cpu": "0.25"},
        "starter": {"memory": "512m", "cpu": "0.5"},
        "professional": {"memory": "2g", "cpu": "1.0"},
        "enterprise": {"memory": "8g", "cpu": "4.0"},
    }
    return limits.get(tier, limits["starter"])


@router.post("/provision", response_model=ProvisionResponse)
async def provision_instance(
    request: ProvisionRequest,
    background_tasks: BackgroundTasks,
):
    """Provision a new MindRoom instance on Dokku."""
    start_time = time.time()
    app_name = generate_app_name(request.subscription_id)

    # Use custom domain or generate subdomain
    if request.custom_domain:
        subdomain = request.custom_domain
    else:
        subdomain = f"{app_name}.{settings.base_domain}"

    admin_password = generate_password()

    # Log provisioning start
    log_event(
        request.subscription_id,
        "provisioning_started",
        f"Starting provisioning for {app_name}",
        {"tier": request.tier, "app_name": app_name},
    )

    # Update status to provisioning
    update_instance_status(
        request.subscription_id,
        "provisioning",
        app_name=app_name,
    )

    try:
        # 1. Create main Dokku app
        logger.info(f"Creating Dokku app: {app_name}")
        if not dokku_client.create_app(app_name):
            raise HTTPException(500, "Failed to create Dokku app")

        # 2. Create backend app
        backend_app = f"{app_name}-backend"
        logger.info(f"Creating backend app: {backend_app}")
        if not dokku_client.create_app(backend_app):
            raise HTTPException(500, "Failed to create backend app")

        # 3. Create frontend app
        frontend_app = f"{app_name}-frontend"
        logger.info(f"Creating frontend app: {frontend_app}")
        if not dokku_client.create_app(frontend_app):
            raise HTTPException(500, "Failed to create frontend app")

        # 4. Create and link PostgreSQL
        db_name = f"{app_name}-db"
        logger.info(f"Creating PostgreSQL database: {db_name}")
        if not dokku_client.create_postgres(db_name):
            raise HTTPException(500, "Failed to create PostgreSQL")
        if not dokku_client.link_postgres(db_name, backend_app):
            raise HTTPException(500, "Failed to link PostgreSQL")

        # 5. Create and link Redis for caching
        redis_name = f"{app_name}-redis"
        logger.info(f"Creating Redis cache: {redis_name}")
        if not dokku_client.create_redis(redis_name):
            raise HTTPException(500, "Failed to create Redis")
        if not dokku_client.link_redis(redis_name, backend_app):
            raise HTTPException(500, "Failed to link Redis")

        # 6. Generate MindRoom configuration
        logger.info(f"Generating MindRoom config for tier: {request.tier}")
        mindroom_config = generate_mindroom_config(request.tier, request.config_overrides)

        # Save config to storage
        storage_path = f"{settings.instance_data_base}/{app_name}"
        os.makedirs(storage_path, exist_ok=True)
        config_path = f"{storage_path}/config.yaml"
        save_config_to_file(mindroom_config, config_path)

        # 7. Set environment variables for backend
        backend_env_vars = {
            "INSTANCE_ID": request.subscription_id,
            "ACCOUNT_ID": request.account_id,
            "TIER": request.tier,
            "ADMIN_PASSWORD": admin_password,
            "BASE_URL": f"https://{subdomain}",
            "FRONTEND_URL": f"https://{subdomain}",
            "BACKEND_URL": f"https://api.{subdomain}",
            "CONFIG_PATH": "/app/config/config.yaml",
            "STORAGE_PATH": "/app/mindroom_data",
            "LOG_LEVEL": "INFO",
            "MAX_AGENTS": str(request.limits.agents),
            "MAX_MESSAGES_PER_DAY": str(request.limits.messages_per_day),
            "MATRIX_HOMESERVER": "https://matrix.org",  # Default
        }

        logger.info("Setting backend environment variables")
        if not dokku_client.set_config(backend_app, backend_env_vars):
            raise HTTPException(500, "Failed to set backend environment variables")

        # 8. Set environment variables for frontend
        frontend_env_vars = {
            "VITE_API_URL": f"https://api.{subdomain}",
            "VITE_INSTANCE_ID": request.subscription_id,
            "VITE_TIER": request.tier,
        }

        logger.info("Setting frontend environment variables")
        if not dokku_client.set_config(frontend_app, frontend_env_vars):
            raise HTTPException(500, "Failed to set frontend environment variables")

        # 9. Set resource limits
        resource_limits = calculate_resource_limits(request.tier)
        memory = f"{request.limits.memory_mb}m"
        cpu = str(request.limits.cpu_limit)

        # Override with tier defaults if not custom
        if request.tier != "enterprise":
            memory = resource_limits["memory"]
            cpu = resource_limits["cpu"]

        logger.info(f"Setting resource limits: memory={memory}, cpu={cpu}")
        dokku_client.set_resource_limits(backend_app, memory, cpu)
        dokku_client.set_resource_limits(frontend_app, memory, cpu)

        # 10. Create persistent storage
        backend_storage = f"{storage_path}/backend"
        frontend_storage = f"{storage_path}/frontend"
        os.makedirs(backend_storage, exist_ok=True)
        os.makedirs(frontend_storage, exist_ok=True)

        logger.info("Creating persistent storage mounts")
        dokku_client.create_storage(backend_app, backend_storage, "/app/mindroom_data")
        dokku_client.create_storage(backend_app, config_path, "/app/config/config.yaml")

        # 11. Deploy the Docker images
        logger.info(f"Deploying backend image: {settings.mindroom_backend_image}")
        if not dokku_client.deploy_image(backend_app, settings.mindroom_backend_image):
            raise HTTPException(500, "Failed to deploy backend")

        logger.info(f"Deploying frontend image: {settings.mindroom_frontend_image}")
        if not dokku_client.deploy_image(frontend_app, settings.mindroom_frontend_image):
            raise HTTPException(500, "Failed to deploy frontend")

        # 12. Set up domains
        domains = {
            backend_app: [f"api.{subdomain}"],
            frontend_app: [subdomain, f"www.{subdomain}"],
        }

        for app, app_domains in domains.items():
            logger.info(f"Setting domains for {app}: {app_domains}")
            if not dokku_client.set_domains(app, app_domains):
                logger.warning(f"Failed to set domains for {app}")

        # 13. Enable SSL with Let's Encrypt (optional, non-critical)
        logger.info("Enabling SSL certificates")
        for app in [backend_app, frontend_app]:
            if not dokku_client.enable_letsencrypt(app):
                logger.warning(f"Failed to enable SSL for {app} - continuing anyway")

        # 14. Optional: Set up Matrix server
        matrix_url = None
        if request.enable_matrix and request.matrix_type:
            matrix_app = f"{app_name}-matrix"
            logger.info(f"Setting up Matrix server: {matrix_app}")

            if dokku_client.create_app(matrix_app):
                matrix_env = {
                    "MATRIX_SERVER_NAME": f"matrix.{subdomain}",
                    "MATRIX_ALLOW_REGISTRATION": "false",
                    "MATRIX_ENABLE_FEDERATION": "true",
                }
                dokku_client.set_config(matrix_app, matrix_env)

                # Deploy Matrix image based on type
                if request.matrix_type == "tuwunel":
                    matrix_image = settings.tuwunel_image
                else:
                    matrix_image = settings.synapse_image

                if dokku_client.deploy_image(matrix_app, matrix_image):
                    matrix_url = f"https://matrix.{subdomain}"
                    dokku_client.set_domains(matrix_app, [f"matrix.{subdomain}"])
                    dokku_client.enable_letsencrypt(matrix_app)

        # Calculate provisioning time
        provisioning_time = time.time() - start_time

        # Update status to running
        background_tasks.add_task(
            update_instance_status,
            subscription_id=request.subscription_id,
            status="running",
            app_name=app_name,
            urls={
                "frontend": f"https://{subdomain}",
                "backend": f"https://api.{subdomain}",
                "matrix": matrix_url,
            },
            metadata={
                "admin_password": admin_password,
                "tier": request.tier,
                "provisioning_time": provisioning_time,
            },
        )

        # Log success
        log_event(
            request.subscription_id,
            "provisioning_completed",
            f"Successfully provisioned {app_name}",
            {"app_name": app_name, "time_seconds": provisioning_time},
        )

        return ProvisionResponse(
            success=True,
            app_name=app_name,
            subdomain=subdomain,
            frontend_url=f"https://{subdomain}",
            backend_url=f"https://api.{subdomain}",
            matrix_url=matrix_url,
            admin_password=admin_password,
            provisioning_time_seconds=provisioning_time,
            message="Instance provisioned successfully",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Provisioning failed: {e}", exc_info=True)

        # Cleanup on failure
        logger.info("Cleaning up failed provisioning")
        for app_suffix in ["", "-backend", "-frontend", "-matrix"]:
            dokku_client.destroy_app(f"{app_name}{app_suffix}", force=True)

        dokku_client.destroy_postgres(f"{app_name}-db", force=True)
        dokku_client.destroy_redis(f"{app_name}-redis", force=True)

        # Update status to failed
        background_tasks.add_task(
            update_instance_status,
            subscription_id=request.subscription_id,
            status="failed",
            error=str(e),
        )

        # Log failure
        log_event(
            request.subscription_id,
            "provisioning_failed",
            f"Failed to provision {app_name}",
            {"error": str(e)},
        )

        raise HTTPException(500, f"Provisioning failed: {e!s}")


@router.delete("/deprovision", response_model=DeprovisionResponse)
async def deprovision_instance(
    request: DeprovisionRequest,
    background_tasks: BackgroundTasks,
):
    """Remove a MindRoom instance from Dokku."""
    logger.info(f"Starting deprovisioning for {request.app_name}")

    # Update status
    update_instance_status(
        request.subscription_id,
        "deprovisioning",
        app_name=request.app_name,
    )

    try:
        # Backup data if requested
        backup_url = None
        if request.backup_data:
            # TODO: Implement backup logic
            logger.info("Data backup requested but not yet implemented")

        # Destroy all related apps
        apps_to_destroy = [
            request.app_name,
            f"{request.app_name}-backend",
            f"{request.app_name}-frontend",
            f"{request.app_name}-matrix",
        ]

        for app in apps_to_destroy:
            logger.info(f"Destroying app: {app}")
            dokku_client.destroy_app(app, force=True)

        # Destroy database and Redis
        logger.info("Destroying database and cache")
        dokku_client.destroy_postgres(f"{request.app_name}-db", force=True)
        dokku_client.destroy_redis(f"{request.app_name}-redis", force=True)

        # Update status to deprovisioned
        background_tasks.add_task(
            update_instance_status,
            subscription_id=request.subscription_id,
            status="deprovisioned",
            app_name=request.app_name,
        )

        # Log event
        log_event(
            request.subscription_id,
            "deprovisioned",
            f"Instance {request.app_name} deprovisioned",
            {"backup_created": request.backup_data},
        )

        return DeprovisionResponse(
            success=True,
            message=f"Instance {request.app_name} successfully deprovisioned",
            backup_url=backup_url,
        )

    except Exception as e:
        logger.error(f"Deprovisioning failed: {e}", exc_info=True)

        # Update status to failed
        update_instance_status(
            request.subscription_id,
            "failed",
            error=f"Deprovisioning failed: {e!s}",
        )

        raise HTTPException(500, f"Deprovisioning failed: {e!s}")


@router.put("/update", response_model=UpdateResponse)
async def update_instance(request: UpdateRequest):
    """Update an existing instance (limits, config, etc.)."""
    logger.info(f"Updating instance {request.app_name}")

    try:
        restart_required = False

        # Update resource limits if provided
        if request.limits:
            memory = f"{request.limits.memory_mb}m"
            cpu = str(request.limits.cpu_limit)

            backend_app = f"{request.app_name}-backend"
            frontend_app = f"{request.app_name}-frontend"

            logger.info(f"Updating resource limits: memory={memory}, cpu={cpu}")
            dokku_client.set_resource_limits(backend_app, memory, cpu)
            dokku_client.set_resource_limits(frontend_app, memory, cpu)

            restart_required = True

        # Update config if provided
        if request.config_updates:
            backend_app = f"{request.app_name}-backend"

            logger.info("Updating configuration")
            dokku_client.set_config(backend_app, request.config_updates)
            restart_required = True

        # Update tier if provided
        if request.tier:
            # Regenerate config for new tier
            logger.info(f"Updating tier to {request.tier}")
            mindroom_config = generate_mindroom_config(request.tier)

            storage_path = f"{settings.instance_data_base}/{request.app_name}"
            config_path = f"{storage_path}/config.yaml"
            save_config_to_file(mindroom_config, config_path)

            # Update tier in environment
            backend_app = f"{request.app_name}-backend"
            dokku_client.set_config(backend_app, {"TIER": request.tier})

            restart_required = True

        # Restart apps if needed
        if restart_required:
            logger.info("Restarting apps to apply changes")
            dokku_client.restart_app(f"{request.app_name}-backend")
            dokku_client.restart_app(f"{request.app_name}-frontend")

        # Log event
        log_event(
            request.subscription_id,
            "instance_updated",
            f"Instance {request.app_name} updated",
            {"restart_required": restart_required},
        )

        return UpdateResponse(
            success=True,
            message=f"Instance {request.app_name} updated successfully",
            restart_required=restart_required,
        )

    except Exception as e:
        logger.error(f"Update failed: {e}", exc_info=True)
        raise HTTPException(500, f"Update failed: {e!s}")


@router.get("/status/{subscription_id}", response_model=InstanceInfo)
async def get_instance_status(subscription_id: str):
    """Check the status of an instance."""
    # Get info from Supabase
    instance_data = get_instance_info(subscription_id)

    if not instance_data:
        raise HTTPException(404, f"Instance not found for subscription {subscription_id}")

    # Check actual status on Dokku
    app_name = instance_data.get("app_name")
    if app_name and dokku_client.app_exists(app_name):
        # Get more details from Dokku if needed
        app_info = dokku_client.get_app_info(app_name)
        instance_data["dokku_info"] = app_info

    return InstanceInfo(**instance_data)
