"""Health check routes."""

from __future__ import annotations

from typing import Any

from backend.config import STRIPE_WEBHOOK_SECRET, stripe
from backend.deps import ensure_supabase
from backend.models import HealthResponse
from fastapi import APIRouter

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health_check() -> dict[str, Any]:
    """Health check endpoint."""
    try:
        ensure_supabase()
        supabase_ok = True
    except Exception:
        supabase_ok = False

    # Stripe is only healthy when inbound webhooks can be verified.
    stripe_ok = bool(stripe.api_key) and bool(STRIPE_WEBHOOK_SECRET)
    overall_status = "ok" if (supabase_ok and stripe_ok) else "degraded"

    return {"status": overall_status, "supabase": supabase_ok, "stripe": stripe_ok}
