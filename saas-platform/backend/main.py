"""MindRoom Backend - Simple single-file FastAPI backend."""

import asyncio
import logging
import os
import secrets
import string
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import stripe
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from supabase import create_client

# Load environment variables
load_dotenv(".env")
load_dotenv("../.env")

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(title="MindRoom Backend")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify exact origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
else:
    logger.warning("Supabase not configured")
    supabase = None

# Platform configuration
PLATFORM_DOMAIN = os.getenv("PLATFORM_DOMAIN", "mindroom.chat")
ENVIRONMENT = os.getenv("ENVIRONMENT", "production")

# Initialize Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

# Simple admin auth
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@mindroom.chat")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin-password")

# Provisioner API key
PROVISIONER_API_KEY = os.getenv("PROVISIONER_API_KEY", "")


# === Models ===
class LoginRequest(BaseModel):
    """Login request model."""

    email: str
    password: str


# === Health Check ===
@app.get("/health")
async def health_check() -> dict[str, Any]:
    """Health check endpoint."""
    return {
        "status": "healthy",
        "supabase": bool(supabase),
        "stripe": bool(stripe.api_key),
    }


# === Admin Authentication ===
@app.post("/api/admin/auth/login")
async def admin_login(data: LoginRequest) -> dict[str, Any]:
    """Simple admin login."""
    if data.email == ADMIN_EMAIL and data.password == ADMIN_PASSWORD:
        return {
            "user": {"email": ADMIN_EMAIL, "role": "admin"},
            "token": "admin-token",  # Simple token
        }
    raise HTTPException(status_code=401, detail="Invalid credentials")


@app.post("/api/admin/auth/logout")
async def admin_logout() -> dict[str, bool]:
    """Admin logout."""
    return {"success": True}


# === React Admin Data Provider ===
@app.get("/api/admin/{resource}")
async def admin_get_list(
    resource: str,
    _sort: Annotated[str | None, Query()] = None,
    _order: Annotated[str | None, Query()] = None,
    _start: Annotated[int, Query()] = 0,
    _end: Annotated[int, Query()] = 10,
    q: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Generic list endpoint for React Admin."""
    if not supabase:
        raise HTTPException(status_code=503, detail="Database not configured")

    try:
        query = supabase.table(resource).select("*", count="exact")

        # Apply search filter
        if q:
            search_fields = {
                "accounts": ["email", "full_name", "company_name"],
                "instances": ["name", "subdomain"],
                "audit_logs": ["action", "details"],
                "subscriptions": ["tier", "status"],
            }
            if resource in search_fields:
                or_conditions = [f"{field}.ilike.%{q}%" for field in search_fields[resource]]
                query = query.or_(",".join(or_conditions))

        # Apply sorting
        if _sort:
            order_column = f"{_sort}.{_order.lower() if _order else 'asc'}"
            query = query.order(order_column)

        # Apply pagination
        query = query.range(_start, _end - 1)

        result = query.execute()
    except Exception:
        logger.exception("Error in get_list")
        return {"data": [], "total": 0}
    else:
        return {
            "data": result.data,
            "total": result.count,
        }


@app.get("/api/admin/{resource}/{resource_id}")
async def admin_get_one(resource: str, resource_id: str) -> dict[str, Any]:
    """Get single record for React Admin."""
    if not supabase:
        raise HTTPException(status_code=503, detail="Database not configured")

    try:
        result = supabase.table(resource).select("*").eq("id", resource_id).single().execute()
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    else:
        return {"data": result.data}


@app.post("/api/admin/{resource}")
async def admin_create(resource: str, data: dict) -> dict[str, Any]:
    """Create record for React Admin."""
    if not supabase:
        raise HTTPException(status_code=503, detail="Database not configured")

    try:
        result = supabase.table(resource).insert(data).execute()
        return {"data": result.data[0] if result.data else None}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.put("/api/admin/{resource}/{resource_id}")
async def admin_update(resource: str, resource_id: str, data: dict) -> dict[str, Any]:
    """Update record for React Admin."""
    if not supabase:
        raise HTTPException(status_code=503, detail="Database not configured")

    try:
        data.pop("id", None)
        result = supabase.table(resource).update(data).eq("id", resource_id).execute()
        return {"data": result.data[0] if result.data else None}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.delete("/api/admin/{resource}/{resource_id}")
async def admin_delete(resource: str, resource_id: str) -> dict[str, Any]:
    """Delete record for React Admin."""
    if not supabase:
        raise HTTPException(status_code=503, detail="Database not configured")

    try:
        supabase.table(resource).delete().eq("id", resource_id).execute()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    else:
        return {"data": {"id": resource_id}}


# === Dashboard Metrics ===
@app.get("/api/admin/metrics/dashboard")
async def get_dashboard_metrics() -> dict[str, Any]:
    """Get dashboard metrics for admin panel."""
    if not supabase:
        return {
            "totalAccounts": 0,
            "activeSubscriptions": 0,
            "runningInstances": 0,
            "mrr": 0,
            "dailyMessages": [],
            "instanceStatuses": [],
            "recentActivity": [],
        }

    try:
        # Get counts
        accounts = supabase.table("accounts").select("*", count="exact", head=True).execute()
        active_subs = (
            supabase.table("subscriptions").select("*", count="exact", head=True).eq("status", "active").execute()
        )
        running_instances = (
            supabase.table("instances").select("*", count="exact", head=True).eq("status", "running").execute()
        )

        # Get MRR
        subs_data = supabase.table("subscriptions").select("tier").eq("status", "active").execute()
        tier_prices = {"starter": 49, "professional": 199, "enterprise": 999, "free": 0}
        mrr = sum(tier_prices.get(sub.get("tier", "free"), 0) for sub in (subs_data.data or []))

        # Get daily messages for last 7 days
        seven_days_ago = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        messages = (
            supabase.table("usage_metrics")
            .select("metric_date, messages_sent")
            .gte("metric_date", seven_days_ago)
            .order("metric_date")
            .execute()
        )

        daily_messages = []
        if messages.data:
            by_date = defaultdict(int)
            for m in messages.data:
                date = m["metric_date"][:10]
                by_date[date] += m.get("messages_sent", 0)
            daily_messages = [{"date": date, "messages_sent": count} for date, count in sorted(by_date.items())]

        # Get instance status distribution
        all_instances = supabase.table("instances").select("status").execute()
        status_counts = {}
        if all_instances.data:
            for inst in all_instances.data:
                status = inst.get("status", "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1
        instance_statuses = [{"status": status, "count": count} for status, count in status_counts.items()]

        # Get recent activity
        audit_logs = (
            supabase.table("audit_logs")
            .select("created_at, action, account_id")
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )
        recent_activity = audit_logs.data if audit_logs.data else []
    except Exception:
        logger.exception("Error fetching metrics")
        return {
            "totalAccounts": 0,
            "activeSubscriptions": 0,
            "runningInstances": 0,
            "mrr": 0,
            "dailyMessages": [],
            "instanceStatuses": [],
            "recentActivity": [],
        }
    else:
        return {
            "totalAccounts": accounts.count or 0,
            "activeSubscriptions": active_subs.count or 0,
            "runningInstances": running_instances.count or 0,
            "mrr": mrr,
            "dailyMessages": daily_messages,
            "instanceStatuses": instance_statuses,
            "recentActivity": recent_activity,
        }


# === Helper Functions ===
async def check_deployment_exists(instance_id: str, namespace: str = "mindroom-instances") -> bool:
    """Check if a Kubernetes deployment exists."""
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
        # If the deployment doesn't exist, kubectl will return non-zero
        if proc.returncode != 0:
            error_msg = stderr.decode()
            # Check for "not found" errors (deployment or namespace)
            if "not found" in error_msg.lower() or "notfound" in error_msg.lower():
                logger.info(f"Deployment mindroom-backend-{instance_id} not found in namespace {namespace}")
                return False
            return False  # Other errors
        return proc.returncode == 0  # noqa: TRY300
    except Exception:
        logger.exception("Error checking deployment existence")
        return False


# === Instance Provisioner API (for customer portal compatibility) ===
@app.post("/api/v1/provision")
async def provision_instance(
    data: dict,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Provision a new instance (compatible with customer portal)."""
    # Check API key
    if authorization != f"Bearer {PROVISIONER_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not supabase:
        raise HTTPException(status_code=503, detail="Database not configured")

    subscription_id = data.get("subscription_id")
    account_id = data.get("account_id")  # noqa: F841
    tier = data.get("tier", "free")

    # Generate a numeric customer ID
    # Count existing instances to get the next ID
    result = supabase.table("instances").select("subdomain").execute()
    existing_ids = []
    for instance in result.data or []:
        # Extract numeric ID from subdomain if it exists
        subdomain = instance.get("subdomain", "")
        if subdomain.isdigit():
            existing_ids.append(int(subdomain))

    # Get next available ID
    next_id = max(existing_ids) + 1 if existing_ids else 1
    customer_id = str(next_id)

    logger.info(f"Provisioning instance for subscription {subscription_id}, tier: {tier}")

    # Create namespace if it doesn't exist
    namespace = "mindroom-instances"
    try:
        proc = await asyncio.create_subprocess_exec(
            "kubectl",
            "create",
            "namespace",
            namespace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
    except Exception as e:
        logger.warning(f"Could not create namespace (may already exist): {e}")

    # Deploy instance using Helm
    logger.info(f"Deploying instance {customer_id} to namespace {namespace}")

    try:
        # Run helm install command
        # Note: API keys should be configured by the customer after provisioning
        proc = await asyncio.create_subprocess_exec(
            "helm",
            "install",
            customer_id,
            "/app/k8s/instance/",  # Path to instance chart
            "--namespace",
            namespace,
            "--create-namespace",
            "--set",
            f"customer={customer_id}",
            "--set",
            f"baseDomain={PLATFORM_DOMAIN}",
            "--set",
            "mindroom_image=git.nijho.lt/basnijholt/mindroom-frontend:latest",
            "--wait",
            "--timeout",
            "5m",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error_msg = stderr.decode()
            logger.error(f"Failed to deploy instance: {error_msg}")
            # Try to clean up if deployment failed
            cleanup_proc = await asyncio.create_subprocess_exec(
                "helm",
                "uninstall",
                customer_id,
                "--namespace",
                namespace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await cleanup_proc.communicate()
            raise HTTPException(status_code=500, detail=f"Failed to deploy instance: {error_msg}") from None  # noqa: TRY301

        logger.info(f"Successfully deployed instance {customer_id}")
    except Exception as e:
        logger.exception("Error deploying instance.")
        raise HTTPException(status_code=500, detail=f"Failed to deploy instance: {e!s}") from e

    return {
        "customer_id": customer_id,
        "frontend_url": f"https://{customer_id}.{PLATFORM_DOMAIN}",
        "api_url": f"https://{customer_id}.api.{PLATFORM_DOMAIN}",
        "matrix_url": f"https://{customer_id}.matrix.{PLATFORM_DOMAIN}",
        "auth_token": "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32)),
        "success": True,
        "message": "Instance provisioned successfully",
    }


@app.post("/api/v1/start/{instance_id}")
async def start_instance_provisioner(
    instance_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Start an instance (provisioner API compatible)."""
    if authorization != f"Bearer {PROVISIONER_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    logger.info(f"Starting instance {instance_id}")

    # Check if deployment exists first
    if not await check_deployment_exists(instance_id):
        error_msg = f"Deployment mindroom-backend-{instance_id} not found"
        logger.warning(error_msg)
        raise HTTPException(status_code=404, detail=error_msg)

    try:
        proc = await asyncio.create_subprocess_exec(
            "kubectl",
            "scale",
            f"deployment/mindroom-backend-{instance_id}",
            "--replicas=1",
            "--namespace=mindroom-instances",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise Exception(stderr.decode())  # noqa: TRY002, TRY301
        logger.info(f"Started instance {instance_id}: {stdout.decode()}")
    except Exception as e:
        logger.exception(f"Failed to start instance {instance_id}")
        raise HTTPException(status_code=500, detail=f"Failed to start instance: {e}") from e

    return {
        "success": True,
        "message": f"Instance {instance_id} started successfully",
    }


@app.post("/api/v1/stop/{instance_id}")
async def stop_instance_provisioner(
    instance_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Stop an instance (provisioner API compatible)."""
    if authorization != f"Bearer {PROVISIONER_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    logger.info(f"Stopping instance {instance_id}")

    # Check if deployment exists first
    if not await check_deployment_exists(instance_id):
        error_msg = f"Deployment mindroom-backend-{instance_id} not found"
        logger.warning(error_msg)
        raise HTTPException(status_code=404, detail=error_msg)

    try:
        proc = await asyncio.create_subprocess_exec(
            "kubectl",
            "scale",
            f"deployment/mindroom-backend-{instance_id}",
            "--replicas=0",
            "--namespace=mindroom-instances",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise Exception(stderr.decode())  # noqa: TRY002, TRY301
        logger.info(f"Stopped instance {instance_id}: {stdout.decode()}")
    except Exception as e:
        logger.exception(f"Failed to stop instance {instance_id}")
        raise HTTPException(status_code=500, detail=f"Failed to stop instance: {e}") from e

    return {
        "success": True,
        "message": f"Instance {instance_id} stopped successfully",
    }


@app.post("/api/v1/restart/{instance_id}")
async def restart_instance_provisioner(
    instance_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Restart an instance (provisioner API compatible)."""
    if authorization != f"Bearer {PROVISIONER_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    logger.info(f"Restarting instance {instance_id}")

    # Check if deployment exists first
    if not await check_deployment_exists(instance_id):
        error_msg = f"Deployment mindroom-backend-{instance_id} not found"
        logger.warning(error_msg)
        raise HTTPException(status_code=404, detail=error_msg)

    try:
        proc = await asyncio.create_subprocess_exec(
            "kubectl",
            "rollout",
            "restart",
            f"deployment/mindroom-backend-{instance_id}",
            "--namespace=mindroom-instances",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise Exception(stderr.decode())  # noqa: TRY002, TRY301
        logger.info(f"Restarted instance {instance_id}: {stdout.decode()}")
    except Exception as e:
        logger.exception(f"Failed to restart instance {instance_id}")
        raise HTTPException(status_code=500, detail=f"Failed to restart instance: {e}") from e

    return {
        "success": True,
        "message": f"Instance {instance_id} restarted successfully",
    }


# === Instance Sync API ===
@app.post("/api/v1/sync-instances")
async def sync_instances(  # noqa: C901
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Sync instance states between database and Kubernetes cluster."""
    if authorization != f"Bearer {PROVISIONER_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not supabase:
        raise HTTPException(status_code=503, detail="Database not configured")

    logger.info("Starting instance sync")

    try:
        # Get all instances from database
        result = supabase.table("instances").select("*").execute()
        instances = result.data if result.data else []

        sync_results = {
            "total": len(instances),
            "synced": 0,
            "errors": 0,
            "updates": [],
        }

        for instance in instances:
            instance_id = instance.get("instance_id") or instance.get("subdomain")
            if not instance_id:
                logger.warning(f"Instance {instance.get('id')} has no instance_id or subdomain")
                sync_results["errors"] += 1
                continue

            # Check if deployment exists in Kubernetes
            exists = await check_deployment_exists(instance_id)

            current_status = instance.get("status", "unknown")

            # Determine what the status should be
            if not exists:
                # Deployment doesn't exist in Kubernetes
                if current_status not in ["error", "deprovisioned"]:
                    # Update database to reflect reality
                    logger.info(f"Instance {instance_id} not found in cluster, marking as error")
                    supabase.table("instances").update(
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
                # Deployment exists, check its actual state
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "kubectl",
                        "get",
                        f"deployment/mindroom-backend-{instance_id}",
                        "--namespace=mindroom-instances",
                        "-o=jsonpath={.spec.replicas}",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await proc.communicate()
                    if proc.returncode == 0:
                        replicas = int(stdout.decode().strip() or "0")
                        actual_status = "running" if replicas > 0 else "stopped"

                        if current_status != actual_status:
                            logger.info(
                                f"Instance {instance_id} status mismatch: DB={current_status}, K8s={actual_status}",
                            )
                            supabase.table("instances").update(
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
                    logger.exception(f"Error checking instance {instance_id} state")
                    sync_results["errors"] += 1

        logger.info(f"Instance sync completed: {sync_results}")
        return sync_results  # noqa: TRY300

    except Exception as e:
        logger.exception("Failed to sync instances")
        raise HTTPException(status_code=500, detail=f"Failed to sync instances: {e}") from e


# === Stripe Webhooks ===
def handle_subscription_created(subscription: dict) -> None:
    """Handle subscription created event."""
    logger.info(f"Subscription created: {subscription['id']}")
    if supabase:
        supabase.table("subscriptions").upsert(
            {
                "subscription_id": subscription["id"],
                "customer_id": subscription["customer"],
                "status": subscription["status"],
                "tier": subscription["items"]["data"][0]["price"]["lookup_key"]
                if subscription["items"]["data"]
                else "free",
            },
        ).execute()
        # TODO: Provision instance if needed


def handle_subscription_deleted(subscription: dict) -> None:
    """Handle subscription deleted event."""
    logger.info(f"Subscription deleted: {subscription['id']}")
    if supabase:
        supabase.table("subscriptions").update(
            {"status": "cancelled"},
        ).eq("subscription_id", subscription["id"]).execute()
        # TODO: Deprovision instance if needed


def handle_payment_succeeded(invoice: dict) -> None:
    """Handle payment succeeded event."""
    logger.info(f"Payment succeeded: {invoice['id']}")
    if supabase:
        supabase.table("payments").insert(
            {
                "invoice_id": invoice["id"],
                "subscription_id": invoice["subscription"],
                "customer_id": invoice["customer"],
                "amount": invoice["amount_paid"] / 100,
                "currency": invoice["currency"],
                "status": "succeeded",
            },
        ).execute()


@app.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Handle Stripe webhooks."""
    if not stripe_signature:
        raise HTTPException(status_code=400, detail="Missing signature")

    body = await request.body()

    try:
        event = stripe.Webhook.construct_event(body, stripe_signature, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        logger.exception("Webhook error")
        raise HTTPException(status_code=400, detail="Invalid signature") from e

    # Handle the event
    try:
        if event.type == "customer.subscription.created":
            handle_subscription_created(event.data.object)
        elif event.type == "customer.subscription.deleted":
            handle_subscription_deleted(event.data.object)
        elif event.type == "invoice.payment_succeeded":
            handle_payment_succeeded(event.data.object)
        else:
            logger.info(f"Unhandled event type: {event.type}")
    except Exception as e:
        logger.exception("Error processing webhook")
        # Return 200 anyway to prevent retries for non-critical errors
        return {"received": True, "error": str(e)}
    else:
        return {"received": True}


# === Serve Static Files (Production) ===
admin_static = Path("/app/admin-static")
if admin_static.exists():
    app.mount("/admin", StaticFiles(directory=str(admin_static), html=True), name="admin")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104
