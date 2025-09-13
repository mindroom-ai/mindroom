"""MindRoom Backend API.

Modular FastAPI application that includes route modules from `backend/`.
This replaces the previous monolithic implementation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from backend.config import ALLOWED_ORIGINS, ENVIRONMENT, PLATFORM_DOMAIN
from backend.deps import limiter
from backend.middleware.audit_logging import AuditLoggingMiddleware
from backend.routes import (
    accounts,
    admin,
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

# IMPORTANT: Middleware order is reversed in FastAPI!
# The last middleware added runs first.
# We want: Request -> AuditLogging -> CORS -> Routes
# So we add them in reverse order:

# Audit logging middleware (added first, runs second)
app.add_middleware(AuditLoggingMiddleware)

# Compute CORS origins: exclude localhost in production
cors_origins = [o for o in ALLOWED_ORIGINS if not (ENVIRONMENT == "production" and o.startswith("http://localhost"))]

# CORS middleware (added last, runs first - ensures CORS headers on ALL responses)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    expose_headers=["X-Total-Count"],
    max_age=86400,
)

# Restrict allowed hosts
allowed_hosts = [f"*.{PLATFORM_DOMAIN}", PLATFORM_DOMAIN, "testserver"]
if ENVIRONMENT != "production":
    allowed_hosts += ["localhost", "127.0.0.1", "testserver"]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)


# Request size limit middleware (1 MiB default)
MAX_REQUEST_BYTES = 1024 * 1024


@app.middleware("http")
async def enforce_request_size(
    request: Request,
    call_next: Callable[[Request], Awaitable[StarletteResponse]],
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
    request: Request,
    call_next: Callable[[Request], Awaitable[StarletteResponse]],
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
app.add_middleware(SlowAPIMiddleware)

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

# Keep a reference list of primary endpoints for tooling/tests that grep this file
EXPOSED_ENDPOINTS = [
    "/my/subscription",
    "/my/usage",
    "/my/account/admin-status",
    "/admin/stats",
]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104
