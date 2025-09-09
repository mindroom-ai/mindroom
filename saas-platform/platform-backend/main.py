"""MindRoom Backend - Simple single-file FastAPI backend."""  # noqa: INP001

import asyncio
import logging
import os
import secrets
import string
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import stripe
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
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
    # Create service client that bypasses RLS
    # Using service key instead of anon key automatically bypasses RLS
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    # Create a separate auth client for user verification
    auth_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
else:
    logger.warning("Supabase not configured")
    supabase = None
    auth_client = None

# Platform configuration
PLATFORM_DOMAIN = os.getenv("PLATFORM_DOMAIN", "mindroom.chat")
ENVIRONMENT = os.getenv("ENVIRONMENT", "production")

# Initialize Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

# Provisioner API key
PROVISIONER_API_KEY = os.getenv("PROVISIONER_API_KEY", "")


# === Health Check ===
@app.get("/health")
async def health_check() -> dict[str, Any]:
    """Health check endpoint."""
    return {
        "status": "healthy",
        "supabase": bool(supabase),
        "stripe": bool(stripe.api_key),
    }


# === Authentication Helpers ===
async def verify_user(authorization: str = Header(None)) -> dict:
    """Verify regular user (not admin) - works with new schema where account.id = auth.user.id."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization.replace("Bearer ", "")

    if not auth_client or not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    try:
        # Verify the JWT token with Supabase (use auth client)
        user = auth_client.auth.get_user(token)
        if not user or not user.user:
            raise HTTPException(status_code=401, detail="Invalid token")  # noqa: TRY301

        # With new schema, account.id = auth.user.id
        account_id = user.user.id

        # Verify account exists (use service client to bypass RLS)
        # First, try to get the account
        try:
            result = supabase.from_("accounts").select("*").eq("id", account_id).single().execute()
            if not result.data:
                msg = "No data"
                raise ValueError(msg)  # noqa: TRY301
        except Exception:
            # Account doesn't exist - create it (trigger might have failed)
            logger.info(f"Account not found for user {account_id}, creating...")
            try:
                create_result = (
                    supabase.from_("accounts")
                    .insert(
                        {
                            "id": account_id,
                            "email": user.user.email,
                            "full_name": user.user.user_metadata.get("full_name", "")
                            if user.user.user_metadata
                            else "",
                            "created_at": datetime.now(UTC).isoformat(),
                            "updated_at": datetime.now(UTC).isoformat(),
                        },
                    )
                    .execute()
                )
                result = create_result
            except Exception:
                logger.exception("Failed to create account")
                # Try to fetch again in case it was a race condition
                result = supabase.from_("accounts").select("*").eq("id", account_id).single().execute()
                if not result.data:
                    raise HTTPException(status_code=404, detail="Account creation failed. Please contact support.")  # noqa: B904

        return {  # noqa: TRY300
            "user_id": user.user.id,
            "email": user.user.email,
            "account_id": account_id,  # Same as user_id with new schema
            "account": result.data,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("User verification error")
        raise HTTPException(status_code=401, detail="Authentication failed") from e


async def verify_user_optional(authorization: str = Header(None)) -> dict | None:
    """Optional user verification for public endpoints."""
    if not authorization:
        return None
    try:
        return await verify_user(authorization)
    except HTTPException:
        return None


# === Admin Authentication ===
async def verify_admin(authorization: str = Header(None)) -> dict:
    """Verify admin access via Supabase auth."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization.replace("Bearer ", "")

    if not auth_client or not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    try:
        # Verify the JWT token with Supabase (use auth client)
        user = auth_client.auth.get_user(token)
        if not user or not user.user:
            raise HTTPException(status_code=401, detail="Invalid token")  # noqa: TRY301

        # Check if user is admin (use service client to bypass RLS)
        result = supabase.table("accounts").select("is_admin").eq("id", user.user.id).single().execute()
        if not result.data or not result.data.get("is_admin"):
            raise HTTPException(status_code=403, detail="Admin access required")  # noqa: TRY301

        return {"user_id": user.user.id, "email": user.user.email}  # noqa: TRY300
    except Exception as e:
        logger.exception("Admin verification error")
        raise HTTPException(status_code=401, detail="Authentication failed") from e


# === User Account Management ===
@app.get("/api/v1/account/current")
async def get_current_account(user=Depends(verify_user)) -> dict[str, Any]:  # noqa: ANN001, B008
    """Get current user's account with subscription and instances."""
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    try:
        account_id = user["account_id"]

        # Get account with subscription and instances
        account_result = (
            supabase.table("accounts")
            .select(
                "*, subscriptions(*, instances(*))",
            )
            .eq("id", account_id)
            .single()
            .execute()
        )

        if not account_result.data:
            raise HTTPException(status_code=404, detail="Account not found")  # noqa: TRY301

        return account_result.data  # noqa: TRY300
    except Exception as e:
        logger.exception("Error fetching account")
        raise HTTPException(status_code=500, detail="Failed to fetch account") from e


@app.get("/api/v1/account/is-admin")
async def check_admin_status(user=Depends(verify_user)) -> dict[str, bool]:  # noqa: ANN001, B008
    """Check if current user is an admin."""
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    try:
        account_id = user["account_id"]

        # Get account admin status
        account_result = supabase.table("accounts").select("is_admin").eq("id", account_id).single().execute()

        if not account_result.data:
            return {"is_admin": False}

        return {"is_admin": account_result.data.get("is_admin", False)}
    except Exception:
        logger.exception("Error checking admin status")
        return {"is_admin": False}


@app.post("/api/v1/account/setup")
async def setup_account(user=Depends(verify_user)) -> dict[str, Any]:  # noqa: ANN001, B008
    """Setup free tier account for new user."""
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    try:
        account_id = user["account_id"]

        # Check if subscription already exists
        sub_result = supabase.table("subscriptions").select("id").eq("account_id", account_id).execute()

        if sub_result.data:
            return {"message": "Account already setup", "account_id": account_id}

        # Create free tier subscription
        subscription_data = {
            "account_id": account_id,
            "tier": "free",
            "status": "active",
            "max_agents": 1,
            "max_messages_per_day": 100,
            "created_at": datetime.now(UTC).isoformat(),
        }

        sub_result = supabase.table("subscriptions").insert(subscription_data).execute()

        return {
            "message": "Free tier account created",
            "account_id": account_id,
            "subscription": sub_result.data[0] if sub_result.data else None,
        }
    except Exception as e:
        logger.exception("Error setting up account")
        raise HTTPException(status_code=500, detail="Failed to setup account") from e


# === Subscription Management ===
@app.get("/api/v1/subscription")
async def get_user_subscription(user=Depends(verify_user)) -> dict[str, Any]:  # noqa: ANN001, B008
    """Get current user's subscription."""
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    try:
        account_id = user["account_id"]

        # Get subscription for account
        result = supabase.table("subscriptions").select("*").eq("account_id", account_id).single().execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="Subscription not found")  # noqa: TRY301

        return result.data  # noqa: TRY300
    except Exception as e:
        logger.exception("Error fetching subscription")
        raise HTTPException(status_code=500, detail="Failed to fetch subscription") from e


@app.get("/api/v1/usage")
async def get_user_usage(
    user=Depends(verify_user),  # noqa: ANN001, B008
    days: int = 30,
) -> dict[str, Any]:
    """Get usage metrics for current user."""
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    try:
        account_id = user["account_id"]

        # First get the subscription
        sub_result = supabase.table("subscriptions").select("id").eq("account_id", account_id).single().execute()

        if not sub_result.data:
            return {"usage": [], "aggregated": {"totalMessages": 0, "totalAgents": 0, "totalStorage": 0}}

        subscription_id = sub_result.data["id"]

        # Get usage metrics for the last N days
        start_date = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()

        usage_result = (
            supabase.table("usage_metrics")
            .select("*")
            .eq("subscription_id", subscription_id)
            .gte("date", start_date)
            .order("date", desc=False)
            .execute()
        )

        usage_data = usage_result.data or []

        # Aggregate the usage data
        total_messages = sum(d["messages_sent"] for d in usage_data)
        total_agents = max((d["agents_used"] for d in usage_data), default=0)
        total_storage = max((d["storage_used_gb"] for d in usage_data), default=0)

        return {  # noqa: TRY300
            "usage": usage_data,
            "aggregated": {
                "totalMessages": total_messages,
                "totalAgents": total_agents,
                "totalStorage": total_storage,
            },
        }
    except Exception as e:
        logger.exception("Error fetching usage")
        raise HTTPException(status_code=500, detail="Failed to fetch usage") from e


# === User Instance Management ===
@app.get("/api/v1/instances")
async def list_user_instances(user=Depends(verify_user)) -> dict[str, Any]:  # noqa: ANN001, B008
    """List instances for current user."""
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    try:
        account_id = user["account_id"]

        # Get instances through subscription
        result = (
            supabase.table("instances")
            .select(
                "*",
            )
            .eq("account_id", account_id)
            .execute()
        )

        return {"instances": result.data or []}  # noqa: TRY300
    except Exception as e:
        logger.exception("Error fetching instances")
        raise HTTPException(status_code=500, detail="Failed to fetch instances") from e


@app.post("/api/v1/instances/provision")
async def provision_user_instance(user=Depends(verify_user)) -> dict[str, Any]:  # noqa: ANN001, B008
    """Provision an instance for the current user."""
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    try:
        account_id = user["account_id"]

        # Get user's subscription
        sub_result = supabase.table("subscriptions").select("*").eq("account_id", account_id).single().execute()

        if not sub_result.data:
            raise HTTPException(status_code=404, detail="No subscription found")  # noqa: TRY301

        subscription = sub_result.data

        # Check if instance already exists
        inst_result = supabase.table("instances").select("id").eq("subscription_id", subscription["id"]).execute()

        if inst_result.data:
            raise HTTPException(status_code=400, detail="Instance already exists")  # noqa: TRY301

        # Call the existing provision endpoint internally
        return await provision_instance(
            data={
                "subscription_id": subscription["id"],
                "account_id": account_id,
                "tier": subscription["tier"],
            },
            authorization=f"Bearer {PROVISIONER_API_KEY}",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error provisioning instance")
        raise HTTPException(status_code=500, detail="Failed to provision instance") from e


@app.post("/api/v1/instances/{instance_id}/start")
async def start_user_instance(instance_id: str, user=Depends(verify_user)) -> dict[str, Any]:  # noqa: ANN001, B008
    """Start user's instance."""
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    # Verify ownership
    result = (
        supabase.table("instances")
        .select("id")
        .eq("instance_id", instance_id)
        .eq("account_id", user["account_id"])
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Instance not found or access denied")

    # Use existing start logic
    return await start_instance_provisioner(instance_id, f"Bearer {PROVISIONER_API_KEY}")


@app.post("/api/v1/instances/{instance_id}/stop")
async def stop_user_instance(instance_id: str, user=Depends(verify_user)) -> dict[str, Any]:  # noqa: ANN001, B008
    """Stop user's instance."""
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    # Verify ownership
    result = (
        supabase.table("instances")
        .select("id")
        .eq("instance_id", instance_id)
        .eq("account_id", user["account_id"])
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Instance not found or access denied")

    # Use existing stop logic
    return await stop_instance_provisioner(instance_id, f"Bearer {PROVISIONER_API_KEY}")


@app.post("/api/v1/instances/{instance_id}/restart")
async def restart_user_instance(instance_id: str, user=Depends(verify_user)) -> dict[str, Any]:  # noqa: ANN001, B008
    """Restart user's instance."""
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    # Verify ownership
    result = (
        supabase.table("instances")
        .select("id")
        .eq("instance_id", instance_id)
        .eq("account_id", user["account_id"])
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Instance not found or access denied")

    # Use existing restart logic
    return await restart_instance_provisioner(instance_id, f"Bearer {PROVISIONER_API_KEY}")


# === Stripe API Endpoints ===


class CheckoutRequest(BaseModel):
    """Request model for Stripe checkout session creation."""

    price_id: str
    tier: str


@app.post("/api/v1/stripe/checkout")
async def create_checkout_session(
    request: CheckoutRequest,
    user: Annotated[dict | None, Depends(verify_user_optional)],
) -> dict[str, Any]:
    """Create Stripe checkout session for subscription."""
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    # Get or create customer
    customer_id = None

    if user:
        # Check if user already has a Stripe customer ID
        result = supabase.table("accounts").select("stripe_customer_id").eq("id", user["account_id"]).single().execute()

        if result.data and result.data.get("stripe_customer_id"):
            customer_id = result.data["stripe_customer_id"]
        else:
            # Create a new Stripe customer
            customer = stripe.Customer.create(
                email=user["email"],
                metadata={
                    "supabase_user_id": user["account_id"],
                },
            )
            customer_id = customer.id

            # Save the customer ID to the database
            supabase.table("accounts").update(
                {"stripe_customer_id": customer_id},
            ).eq("id", user["account_id"]).execute()

    # Create checkout session
    checkout_params = {
        "line_items": [
            {
                "price": request.price_id,
                "quantity": 1,
            },
        ],
        "mode": "subscription",
        "success_url": f"{os.getenv('APP_URL', 'https://app.staging.mindroom.chat')}/dashboard?success=true&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{os.getenv('APP_URL', 'https://app.staging.mindroom.chat')}/pricing?cancelled=true",
        "allow_promotion_codes": True,
        "billing_address_collection": "required",
        "payment_method_collection": "if_required",
        "subscription_data": {
            "trial_period_days": 14,  # 14-day free trial
            "metadata": {
                "tier": request.tier,
                "supabase_user_id": user["account_id"] if user else "",
            },
        },
        "metadata": {
            "tier": request.tier,
            "supabase_user_id": user["account_id"] if user else "",
        },
    }

    # Add customer if we have one
    if customer_id:
        checkout_params["customer"] = customer_id

    session = stripe.checkout.Session.create(**checkout_params)

    return {"url": session.url}


@app.post("/api/v1/stripe/portal")
async def create_portal_session(user=Depends(verify_user)) -> dict[str, Any]:  # noqa: ANN001, B008
    """Create Stripe customer portal session for subscription management."""
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    # Get the user's subscription
    result = (
        supabase.table("subscriptions")
        .select("stripe_customer_id")
        .eq("account_id", user["account_id"])
        .single()
        .execute()
    )

    if not result.data or not result.data.get("stripe_customer_id"):
        raise HTTPException(status_code=404, detail="No Stripe customer found")

    # Create a Stripe billing portal session
    session = stripe.billing_portal.Session.create(
        customer=result.data["stripe_customer_id"],
        return_url=f"{os.getenv('APP_URL', 'https://app.staging.mindroom.chat')}/dashboard/billing",
    )

    return {"url": session.url}


# === Admin API Endpoints ===
@app.get("/api/admin/stats")
async def get_admin_stats(admin=Depends(verify_admin)) -> dict[str, Any]:  # noqa: ARG001, ANN001, B008
    """Get platform statistics for admin dashboard."""
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    try:
        # Get counts
        accounts = supabase.table("accounts").select("*", count="exact").execute()
        subscriptions = supabase.table("subscriptions").select("*", count="exact").eq("status", "active").execute()
        instances = supabase.table("instances").select("*", count="exact").eq("status", "active").execute()

        return {
            "accounts": len(accounts.data) if accounts.data else 0,
            "active_subscriptions": len(subscriptions.data) if subscriptions.data else 0,
            "running_instances": len(instances.data) if instances.data else 0,
        }
    except Exception as e:
        logger.exception("Error fetching admin stats")
        raise HTTPException(status_code=500, detail="Failed to fetch statistics") from e


@app.post("/api/admin/instances/{instance_id}/restart")
async def restart_instance(instance_id: str, admin=Depends(verify_admin)) -> dict[str, Any]:  # noqa: ARG001, ANN001, B008
    """Restart a customer instance."""
    # This would trigger the actual instance restart via Kubernetes
    # For now, just update the status
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    try:
        result = (
            supabase.table("instances")
            .update(
                {
                    "status": "restarting",
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            )
            .eq("instance_id", instance_id)
            .execute()
        )

        if not result.data:
            raise HTTPException(status_code=404, detail="Instance not found")  # noqa: TRY301

        # TODO: Trigger actual Kubernetes restart

        return {"status": "restarting", "instance_id": instance_id}  # noqa: TRY300
    except Exception as e:
        logger.exception("Error restarting instance")
        raise HTTPException(status_code=500, detail="Failed to restart instance") from e


@app.put("/api/admin/accounts/{account_id}/status")
async def update_account_status(
    account_id: str,
    status: str,
    admin=Depends(verify_admin),  # noqa: ANN001, B008
) -> dict[str, Any]:
    """Update account status (active, suspended, etc)."""
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    valid_statuses = ["active", "suspended", "deleted", "pending_verification"]
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {valid_statuses}")

    try:
        result = (
            supabase.table("accounts")
            .update(
                {
                    "status": status,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            )
            .eq("id", account_id)
            .execute()
        )

        if not result.data:
            raise HTTPException(status_code=404, detail="Account not found")  # noqa: TRY301

        # Log the action
        supabase.table("audit_logs").insert(
            {
                "account_id": admin["user_id"],
                "action": "update",
                "resource_type": "account",
                "resource_id": account_id,
                "details": {"status": status},
                "created_at": datetime.now(UTC).isoformat(),
            },
        ).execute()

        return {"status": "success", "account_id": account_id, "new_status": status}  # noqa: TRY300
    except Exception as e:
        logger.exception("Error updating account status")
        raise HTTPException(status_code=500, detail="Failed to update account status") from e


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
async def provision_instance(  # noqa: C901
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
    account_id = data.get("account_id")
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

    # Use a prefixed helm release name to avoid conflicts
    helm_release_name = f"instance-{customer_id}"

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
            helm_release_name,
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
                helm_release_name,
                "--namespace",
                namespace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await cleanup_proc.communicate()
            raise HTTPException(status_code=500, detail=f"Failed to deploy instance: {error_msg}") from None  # noqa: TRY301

        logger.info(f"Successfully deployed instance {customer_id}")

        # Generate auth token
        auth_token = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))

        # Create database record - this is the authoritative source
        if supabase:
            try:
                # Determine resource limits based on tier
                memory_limit = 512 if tier == "free" else 1024 if tier == "starter" else 2048
                cpu_limit = 0.5 if tier == "free" else 1.0 if tier == "starter" else 2.0

                instance_data = {
                    "subscription_id": subscription_id,
                    "instance_id": customer_id,
                    "subdomain": customer_id,
                    "name": f"Instance {customer_id}",
                    "status": "running",
                    "tier": tier,
                    "instance_url": f"https://{customer_id}.{PLATFORM_DOMAIN}",
                    "frontend_url": f"https://{customer_id}.{PLATFORM_DOMAIN}",
                    "backend_url": f"https://{customer_id}.api.{PLATFORM_DOMAIN}",
                    "api_url": f"https://{customer_id}.api.{PLATFORM_DOMAIN}",
                    "matrix_url": f"https://{customer_id}.matrix.{PLATFORM_DOMAIN}",
                    "matrix_server_url": f"https://{customer_id}.matrix.{PLATFORM_DOMAIN}",
                    "auth_token": auth_token,
                    "memory_limit_mb": memory_limit,
                    "cpu_limit": cpu_limit,
                    "agent_count": 0,
                    "created_at": datetime.now(UTC).isoformat(),
                    "updated_at": datetime.now(UTC).isoformat(),
                }

                # Add account_id if provided (with new schema, this is the auth.user.id)
                if account_id:
                    instance_data["account_id"] = account_id

                result = supabase.table("instances").insert(instance_data).execute()
                logger.info(f"Created database record for instance {customer_id}")
            except Exception:
                logger.exception("Failed to create database record")
                # Continue anyway - K8s deployment succeeded

    except Exception as e:
        logger.exception("Error deploying instance.")
        raise HTTPException(status_code=500, detail=f"Failed to deploy instance: {e!s}") from e

    return {
        "customer_id": customer_id,
        "frontend_url": f"https://{customer_id}.{PLATFORM_DOMAIN}",
        "api_url": f"https://{customer_id}.api.{PLATFORM_DOMAIN}",
        "matrix_url": f"https://{customer_id}.matrix.{PLATFORM_DOMAIN}",
        "auth_token": auth_token,
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


@app.delete("/api/v1/uninstall/{instance_id}")
async def uninstall_instance(  # noqa: C901, PLR0912
    instance_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Completely uninstall/deprovision an instance (remove all Kubernetes resources)."""
    if authorization != f"Bearer {PROVISIONER_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    logger.info(f"Uninstalling instance {instance_id}")

    try:
        # Try with new naming convention first (instance-X)
        helm_release_name = f"instance-{instance_id}" if instance_id.isdigit() else instance_id

        # Uninstall the helm release
        proc = await asyncio.create_subprocess_exec(
            "helm",
            "uninstall",
            helm_release_name,
            "--namespace=mindroom-instances",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error_msg = stderr.decode()
            # Check if it's already uninstalled
            if "not found" not in error_msg.lower():
                logger.error(f"Failed to uninstall instance: {error_msg}")
                raise HTTPException(status_code=500, detail=f"Failed to uninstall instance: {error_msg}")  # noqa: TRY301

            # Try with old naming convention if new one failed
            if instance_id.isdigit():
                logger.info(f"Trying old naming convention for instance {instance_id}")
                proc2 = await asyncio.create_subprocess_exec(
                    "helm",
                    "uninstall",
                    instance_id,
                    "--namespace=mindroom-instances",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout2, stderr2 = await proc2.communicate()
                if proc2.returncode != 0:
                    error_msg2 = stderr2.decode()
                    if "not found" not in error_msg2.lower():
                        logger.error(f"Failed to uninstall with old naming: {error_msg2}")
                    logger.info(f"Instance {instance_id} was already uninstalled")
                else:
                    logger.info(f"Successfully uninstalled instance {instance_id} with old naming")
            else:
                logger.info(f"Instance {instance_id} was already uninstalled")
        else:
            logger.info(f"Successfully uninstalled instance {instance_id}: {stdout.decode()}")

        # Update database status if configured
        if supabase:
            try:
                supabase.table("instances").update(
                    {
                        "status": "deprovisioned",
                        "updated_at": datetime.now(UTC).isoformat(),
                    },
                ).or_(f"instance_id.eq.{instance_id},subdomain.eq.{instance_id}").execute()
            except Exception as e:
                logger.warning(f"Failed to update database for instance {instance_id}: {e}")

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to uninstall instance {instance_id}")
        raise HTTPException(status_code=500, detail=f"Failed to uninstall instance: {e}") from e

    return {
        "success": True,
        "message": f"Instance {instance_id} uninstalled successfully",
        "instance_id": instance_id,
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104
