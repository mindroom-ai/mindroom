"""Health check routes."""

from __future__ import annotations

from typing import Any

from backend.config import stripe
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

    overall_status = "ok" if (supabase_ok and bool(stripe.api_key)) else "degraded"

    return {
        "status": overall_status,
        "supabase": supabase_ok,
        "stripe": bool(stripe.api_key),
    }
