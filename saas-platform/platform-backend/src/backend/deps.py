"""Shared FastAPI dependency functions for auth and context."""

from __future__ import annotations

import hmac
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from backend import auth_monitor
from backend.metrics import record_admin_verification, record_auth_event
from backend.config import auth_client, logger, supabase
from cachetools import TTLCache
from fastapi import Header, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

if TYPE_CHECKING:
    from supabase import Client

# TTL cache for auth verification (5 minutes, max 100 entries)
_auth_cache = TTLCache(maxsize=100, ttl=300)

# Global rate limiter for the FastAPI app and routes
limiter = Limiter(key_func=get_remote_address)


def ensure_supabase() -> Client:
    """Return configured Supabase client or raise 500 if missing."""
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    return supabase


def _ensure_auth_client() -> Client:
    """Return configured Supabase auth client or raise 500 if missing."""
    if not auth_client:
        raise HTTPException(status_code=500, detail="Supabase auth not configured")
    return auth_client


def _extract_bearer_token(authorization: str | None) -> str:
    """Extract and validate bearer token from authorization header.

    This function provides secure token extraction avoiding common pitfalls.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization header")

    # Split and validate format
    parts = authorization.split()
    if len(parts) != 2:
        raise HTTPException(status_code=401, detail="Invalid authorization format")

    # Use constant-time comparison for Bearer prefix
    scheme = parts[0]
    if not hmac.compare_digest(scheme.lower().encode(), b"bearer"):
        raise HTTPException(status_code=401, detail="Invalid authorization scheme")

    return parts[1]


async def verify_user(authorization: str = Header(None), request: Request = None) -> dict:  # noqa: C901, PLR0912
    """Verify regular user via Supabase JWT.

    With the current schema, `account.id == auth.user.id`.
    Ensures the `accounts` row exists, creating it if necessary.
    """
    # Get client IP for monitoring
    client_ip = request.client.host if request else "unknown"

    # Check if IP is blocked
    if auth_monitor.is_blocked(client_ip):
        record_auth_event(actor="user", outcome="blocked_request")
        raise HTTPException(status_code=429, detail="Too many failed attempts. Please try again later.")

    try:
        token = _extract_bearer_token(authorization)
    except HTTPException:
        # Record auth failure
        auth_monitor.record_failure(client_ip)
        raise

    # Check cache first
    if token in _auth_cache:
        logger.info("Auth cache hit (instant)")
        return _auth_cache[token]

    # Start timing for database lookup
    start = time.perf_counter()
    sb = ensure_supabase()
    ac = _ensure_auth_client()

    try:
        user = ac.auth.get_user(token)
        if not user or not user.user:
            # Record auth failure
            auth_monitor.record_failure(client_ip)
            msg = "Invalid token"
            raise HTTPException(status_code=401, detail=msg)  # noqa: TRY301

        account_id = user.user.id

        # Record successful auth
        auth_monitor.record_success(client_ip, str(account_id))

        # Ensure account exists
        try:
            result = sb.table("accounts").select("*").eq("id", account_id).single().execute()
            if not result.data:
                msg = "No data"
                raise ValueError(msg)  # noqa: TRY301
        except Exception:
            logger.info(f"Account not found for user {account_id}, creating...")
            try:
                now = datetime.now(UTC).isoformat()
                create_result = (
                    sb.table("accounts")
                    .insert(
                        {
                            "id": account_id,
                            "email": user.user.email,
                            "full_name": user.user.user_metadata.get("full_name", "")
                            if user.user.user_metadata
                            else "",
                            "created_at": now,
                            "updated_at": now,
                        }
                    )
                    .execute()
                )
                result = create_result
            except Exception:
                logger.exception("Failed to create account")
                # Try to fetch again in case it was a race condition
                result = sb.table("accounts").select("*").eq("id", account_id).single().execute()
                if not result.data:
                    msg = "Account creation failed. Please contact support."
                    raise HTTPException(status_code=404, detail=msg) from None

        # Prepare response data
        user_data = {
            "user_id": user.user.id,
            "email": user.user.email,
            "account_id": account_id,
            "account": result.data,
        }

        # Cache the result (TTL handled by TTLCache)
        _auth_cache[token] = user_data

        # Log the time taken for database auth
        db_time = time.perf_counter() - start
        logger.info("Auth database lookup: %.2fms", db_time * 1000)

    except HTTPException as e:
        # Record failure if it's an auth error (not a 404)
        if e.status_code == 401:
            auth_monitor.record_failure(client_ip)
        raise
    except Exception:
        logger.exception("User verification error")
        msg = "Authentication failed"
        raise HTTPException(status_code=401, detail=msg) from None

    return user_data


async def verify_user_optional(authorization: str = Header(None)) -> dict | None:
    """Optional user verification for public endpoints."""
    if not authorization:
        return None
    try:
        return await verify_user(authorization)
    except HTTPException:
        return None


async def verify_admin(authorization: str = Header(None)) -> dict:
    """Verify admin access via Supabase auth."""
    try:
        token = _extract_bearer_token(authorization)
    except HTTPException as exc:
        if exc.status_code == 401:
            record_admin_verification("unauthorized")
        raise

    sb = ensure_supabase()
    ac = _ensure_auth_client()

    try:
        user = ac.auth.get_user(token)
        if not user or not user.user:
            msg = "Invalid token"
            record_admin_verification("unauthorized")
            raise HTTPException(status_code=401, detail=msg)  # noqa: TRY301

        result = sb.table("accounts").select("is_admin").eq("id", user.user.id).single().execute()
        if not result.data or not result.data.get("is_admin"):
            msg = "Admin access required"
            record_admin_verification("forbidden")
            raise HTTPException(status_code=403, detail=msg)  # noqa: TRY301
        record_admin_verification("success")
        return {"user_id": user.user.id, "email": user.user.email}  # noqa: TRY300
    except HTTPException:
        raise
    except Exception:
        logger.exception("Admin verification error")
        msg = "Authentication failed"
        record_admin_verification("error")
        raise HTTPException(status_code=401, detail=msg) from None
