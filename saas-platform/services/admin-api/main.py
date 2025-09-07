"""Admin API Backend - Secure backend for admin dashboard."""  # noqa: INP001

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import httpx
import stripe
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Try to load env vars from dotenv if available
try:
    from dotenv import load_dotenv

    # Load both .env files
    load_dotenv("../../.env")
    load_dotenv("../.env")
except ImportError:
    pass

# Initialize FastAPI app
app = FastAPI(title="MindRoom Admin API")

# CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # Vite dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Supabase client with service key (server-side only!)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    from supabase import Client, create_client

    supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
else:
    print("Warning: Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_KEY")
    supabase = None

# Initialize Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

# Provisioner API config
PROVISIONER_URL = os.getenv("PROVISIONER_URL", "http://instance-provisioner:8002")
PROVISIONER_API_KEY = os.getenv("PROVISIONER_API_KEY", "")


# === Models ===
class Account(BaseModel):
    """Account model."""

    email: str
    full_name: str | None
    company_name: str | None
    subscription_tier: str = "free"
    subscription_status: str = "active"


class Instance(BaseModel):
    """Instance model."""

    account_id: str
    name: str
    subdomain: str
    plan: str = "starter"


# === Supabase Data Provider Endpoints ===
@app.get("/api/{resource}")
async def get_list(  # noqa: ANN201
    resource: str,
    _sort: Annotated[str | None, Query()] = None,
    _order: Annotated[str | None, Query()] = None,
    _start: Annotated[int, Query()] = 0,
    _end: Annotated[int, Query()] = 10,
    q: Annotated[str | None, Query()] = None,
):
    """Generic list endpoint for React Admin data provider."""
    if not supabase:
        raise HTTPException(status_code=503, detail="Database not configured")

    try:
        query = supabase.table(resource).select("*", count="exact")

        # Apply search filter
        if q:
            # Search across common text fields
            search_fields = {
                "accounts": ["email", "full_name", "company_name"],
                "instances": ["name", "subdomain"],
                "audit_logs": ["action", "details"],
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

        # Return with total count header for React Admin
        return {
            "data": result.data,
            "total": result.count,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/{resource}/{id}")
async def get_one(resource: str, id: str):  # noqa: ANN201, A002
    """Get single record endpoint for React Admin."""
    try:
        result = supabase.table(resource).select("*").eq("id", id).single().execute()
        return {"data": result.data}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.post("/api/{resource}")
async def create(resource: str, data: dict[str, Any]):  # noqa: ANN201
    """Create record endpoint for React Admin."""
    try:
        result = supabase.table(resource).insert(data).execute()
        return {"data": result.data[0] if result.data else None}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.put("/api/{resource}/{id}")
async def update(resource: str, id: str, data: dict[str, Any]):  # noqa: ANN201, A002
    """Update record endpoint for React Admin."""
    try:
        # Remove id from data if present
        data.pop("id", None)
        result = supabase.table(resource).update(data).eq("id", id).execute()
        return {"data": result.data[0] if result.data else None}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.delete("/api/{resource}/{id}")
async def delete(resource: str, id: str):  # noqa: ANN201, A002
    """Delete record endpoint for React Admin."""
    try:
        supabase.table(resource).delete().eq("id", id).execute()
        return {"data": {"id": id}}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# === Dashboard Metrics ===
@app.get("/api/metrics/overview")
async def get_metrics():  # noqa: ANN201
    """Get dashboard metrics."""
    try:
        # Get counts
        accounts = supabase.table("accounts").select("*", count="exact").execute()
        instances = supabase.table("instances").select("*", count="exact").execute()

        # Get subscription breakdown
        subs = supabase.table("accounts").select("subscription_tier").execute()
        tier_counts = {}
        for acc in subs.data:
            tier = acc.get("subscription_tier", "free")
            tier_counts[tier] = tier_counts.get(tier, 0) + 1

        # Calculate MRR
        mrr = (
            tier_counts.get("starter", 0) * 49
            + tier_counts.get("professional", 0) * 199
            + tier_counts.get("enterprise", 0) * 999
        )

        return {
            "total_accounts": accounts.count or 0,
            "active_instances": instances.count or 0,
            "mrr": mrr,
            "subscription_breakdown": tier_counts,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/metrics/dashboard")
async def get_dashboard_metrics():  # noqa: ANN201
    """Get comprehensive dashboard metrics."""
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
        mrr = sum(tier_prices.get(sub.get("tier", "free"), 0) for sub in subs_data.data) if subs_data.data else 0

        # Get daily messages for last 7 days
        seven_days_ago = (datetime.now() - timedelta(days=7)).isoformat()  # noqa: DTZ005
        messages = (
            supabase.table("usage_metrics")
            .select("metric_date, messages_sent")
            .gte("metric_date", seven_days_ago)
            .order("metric_date")
            .execute()
        )

        # Aggregate by date
        daily_messages = []
        if messages.data:
            from collections import defaultdict  # noqa: PLC0415

            by_date = defaultdict(int)
            for m in messages.data:
                date = m["metric_date"][:10]  # YYYY-MM-DD
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

        return {
            "totalAccounts": accounts.count or 0,
            "activeSubscriptions": active_subs.count or 0,
            "runningInstances": running_instances.count or 0,
            "mrr": mrr,
            "dailyMessages": daily_messages,
            "instanceStatuses": instance_statuses,
            "recentActivity": recent_activity,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# === Instance Control (Proxy to Provisioner) ===
@app.post("/api/instances/{instance_id}/{action}")
async def instance_action(instance_id: str, action: str):  # noqa: ANN201
    """Proxy instance actions to provisioner API."""
    if action not in ["start", "stop", "restart"]:
        raise HTTPException(status_code=400, detail="Invalid action")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{PROVISIONER_URL}/api/instances/{instance_id}/{action}",
                headers={"X-API-Key": PROVISIONER_API_KEY},
            )
            response.raise_for_status()
            return response.json()
        except httpx.RequestError as e:
            raise HTTPException(status_code=503, detail=f"Provisioner unavailable: {e}") from e
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=e.response.text) from e


# === Auth Endpoints ===
@app.post("/api/auth/login")
async def login(credentials: dict[str, str]):  # noqa: ANN201
    """Admin login - validates against Supabase auth."""
    try:
        # For now, we'll use a simple check
        # In production, this should validate against Supabase Auth
        if credentials.get("email") == os.getenv("ADMIN_EMAIL") and credentials.get("password") == os.getenv(
            "ADMIN_PASSWORD",
        ):
            return {
                "user": {"email": credentials["email"], "role": "admin"},
                "token": "admin-token",  # In production, generate a real JWT
            }
        raise HTTPException(status_code=401, detail="Invalid credentials")
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e)) from e


@app.post("/api/auth/logout")
async def logout():  # noqa: ANN201
    """Admin logout."""
    return {"success": True}


# === Serve React App ===
# In production, the built React app will be in /app/static
if Path("/app/static").exists():
    app.mount("/", StaticFiles(directory="/app/static", html=True), name="static")

    @app.get("/{full_path:path}")
    async def serve_react_app(full_path: str):  # noqa: ANN201
        """Serve React app for all non-API routes."""
        if not full_path.startswith("api/"):
            return FileResponse("/app/static/index.html")
        return None


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104
# ruff: noqa: TRY300 TRY301
