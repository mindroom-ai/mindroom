"""MindRoom Backend API.

Modular FastAPI application that includes route modules from `backend/`.
This replaces the previous monolithic implementation.
"""

from __future__ import annotations

from backend.config import ALLOWED_ORIGINS
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
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# FastAPI app
app = FastAPI(title="MindRoom Backend")

# IMPORTANT: Middleware order is reversed in FastAPI!
# The last middleware added runs first.
# We want: Request -> AuditLogging -> CORS -> Routes
# So we add them in reverse order:

# Audit logging middleware (added first, runs second)
app.add_middleware(AuditLoggingMiddleware)

# CORS middleware (added last, runs first - ensures CORS headers on ALL responses)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
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
