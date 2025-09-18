"""MindRoom Backend API.

Modular FastAPI application that includes route modules from `backend/`.
This replaces the previous monolithic implementation.
"""

from __future__ import annotations

import logging
import socket
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from backend.config import ALLOWED_ORIGINS, ENABLE_CLEANUP_SCHEDULER, ENVIRONMENT, PLATFORM_DOMAIN
from backend.metrics import instrument_app
from backend.deps import limiter
from backend.middleware.audit_logging import AuditLoggingMiddleware
from backend.tasks.cleanup import run_all_cleanup_tasks
from backend.routes import (
    accounts,
    admin,
    gdpr,
    health,
    instances,
    pricing,
    provisioner,
    sso,
    stripe_routes,
    subscriptions,
    usage,
    webhooks,
)
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.responses import Response as StarletteResponse

# FastAPI app
app = FastAPI(title="MindRoom Backend")

instrument_app(app)

# IMPORTANT: Middleware order is reversed in FastAPI!
# The last middleware added runs first.
# We want the execution order to be:
# Request -> CORS -> TrustedHost -> SlowAPI -> Security -> AuditLogging -> Routes
# So we add them in reverse order:

# 1. Audit logging middleware (runs after security checks)
app.add_middleware(AuditLoggingMiddleware)

# NOTE: The following middleware will be added later:
# 2. Custom @app.middleware("http") decorators for security headers and request size
# 3. SlowAPIMiddleware for rate limiting
# 4. TrustedHostMiddleware for host validation
# 5. CORSMiddleware (must be last to run first)


# Request size limit middleware (1 MiB default)
MAX_REQUEST_BYTES = 1024 * 1024


@app.middleware("http")
async def enforce_request_size(
    request: Request, call_next: Callable[[Request], Awaitable[StarletteResponse]]
) -> StarletteResponse:
    """Return 413 if Content-Length exceeds MAX_REQUEST_BYTES."""
    try:
        length = int(request.headers.get("content-length", "0") or "0")
    except ValueError:
        length = 0
    if length and length > MAX_REQUEST_BYTES:
        return JSONResponse({"detail": "Request too large"}, status_code=413)
    return await call_next(request)


# Basic security headers
@app.middleware("http")
async def add_security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[StarletteResponse]]
) -> StarletteResponse:
    """Inject basic security headers into every response."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


# Rate limiting (applies to routes decorated with @limiter.limit)
app.state.limiter = limiter

rate_logger = logging.getLogger("mindroom.ratelimit")


async def _logged_rate_limit_exceeded(request: Request, exc: RateLimitExceeded) -> StarletteResponse:  # type: ignore[override]
    client = request.client.host if request.client else "unknown"
    rate_logger.warning("429 Too Many Requests: path=%s client=%s", request.url.path, client)
    return _rate_limit_exceeded_handler(request, exc)


app.add_exception_handler(RateLimitExceeded, _logged_rate_limit_exceeded)


def _service_allowed_hosts(*, environment: str) -> list[str]:
    """Return host aliases Prometheus (and other in-cluster services) use to scrape /metrics."""

    base_hosts = {
        "platform-backend",
        f"platform-backend.mindroom-{environment}",
        f"platform-backend.mindroom-{environment}.svc",
        f"platform-backend.mindroom-{environment}.svc.cluster.local",
    }

    hosts_with_ports = {f"{host}:8000" for host in base_hosts}

    try:
        pod_ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        pod_ip = ""

    if pod_ip:
        base_hosts.add(pod_ip)
        hosts_with_ports.add(f"{pod_ip}:8000")

    return list(base_hosts | hosts_with_ports)


# Add remaining middleware BEFORE routers (but after custom middleware functions)
# Remember: Last added = First to run

# 3. SlowAPI rate limiting middleware
app.add_middleware(SlowAPIMiddleware)

# 4. Trusted host middleware - restrict allowed hosts
allowed_hosts = [f"*.{PLATFORM_DOMAIN}", PLATFORM_DOMAIN, "testserver"]
if ENVIRONMENT != "production":
    allowed_hosts += ["localhost", "127.0.0.1", "testserver"]

allowed_hosts += _service_allowed_hosts(environment=ENVIRONMENT)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

# 5. Compute CORS origins: exclude localhost in production
cors_origins = [o for o in ALLOWED_ORIGINS if not (ENVIRONMENT == "production" and o.startswith("http://localhost"))]

# 6. CORS middleware (added LAST so it runs FIRST - ensures CORS headers on ALL responses)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    expose_headers=["X-Total-Count"],
    max_age=86400,
)

# Include routers
app.include_router(health.router)
app.include_router(accounts.router)
app.include_router(subscriptions.router)
app.include_router(usage.router)
app.include_router(instances.router)
app.include_router(provisioner.router)
app.include_router(admin.router)
app.include_router(pricing.router)
app.include_router(stripe_routes.router)
app.include_router(sso.router)
app.include_router(webhooks.router)
app.include_router(gdpr.router)

# Keep a reference list of primary endpoints for tooling/tests that grep this file
EXPOSED_ENDPOINTS = ["/my/subscription", "/my/usage", "/my/account/admin-status", "/admin/stats"]


def _run_cleanup_job() -> None:
    """Execute periodic cleanup tasks with logging."""

    try:
        result = run_all_cleanup_tasks()
        logger = logging.getLogger("mindroom.cleanup")
        logger.info("Cleanup job completed", extra={"result": result})
    except Exception:  # noqa: BLE001
        logging.getLogger("mindroom.cleanup").exception("Scheduled cleanup job failed")


@app.on_event("startup")
async def start_cleanup_scheduler() -> None:
    """Start APScheduler for periodic cleanup when enabled."""

    if not ENABLE_CLEANUP_SCHEDULER:
        return

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        _run_cleanup_job,
        trigger="cron",
        hour=3,
        minute=0,
        id="daily_cleanup",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    app.state.cleanup_scheduler = scheduler
    logging.getLogger("mindroom.cleanup").info("Cleanup scheduler started (daily at 03:00 UTC)")


@app.on_event("shutdown")
async def stop_cleanup_scheduler() -> None:
    """Shut down the APScheduler instance on application exit."""

    scheduler: AsyncIOScheduler | None = getattr(app.state, "cleanup_scheduler", None)
    if scheduler:
        scheduler.shutdown(wait=False)
        logging.getLogger("mindroom.cleanup").info("Cleanup scheduler stopped")
        app.state.cleanup_scheduler = None


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104
