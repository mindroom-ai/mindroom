"""Health check endpoints."""

import time
from datetime import datetime

from fastapi import APIRouter

from ..dokku.client import test_connection as test_dokku
from ..models import HealthCheckResponse
from ..services.supabase import test_connection as test_supabase

router = APIRouter()

# Track service start time
SERVICE_START_TIME = time.time()


@router.get("/health", response_model=HealthCheckResponse)
async def health_check():
    """Check service health status."""
    dokku_connected = test_dokku()
    supabase_connected = test_supabase()

    status = "healthy"
    if not dokku_connected:
        status = "degraded"
    if not supabase_connected:
        status = "degraded" if status == "healthy" else "unhealthy"

    return HealthCheckResponse(
        status=status,
        dokku_connected=dokku_connected,
        supabase_connected=supabase_connected,
        version="1.0.0",
        uptime_seconds=time.time() - SERVICE_START_TIME,
    )


@router.get("/ready")
async def readiness_check():
    """Check if service is ready to handle requests."""
    dokku_connected = test_dokku()
    supabase_connected = test_supabase()

    if not dokku_connected or not supabase_connected:
        return {"ready": False, "message": "Dependencies not available"}

    return {"ready": True}


@router.get("/live")
async def liveness_check():
    """Simple liveness check."""
    return {"alive": True, "timestamp": datetime.utcnow().isoformat()}
