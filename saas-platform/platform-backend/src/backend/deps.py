"""Shared FastAPI dependency functions for auth and context."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from backend.config import auth_client, logger, supabase
from fastapi import Header, HTTPException

if TYPE_CHECKING:
    from supabase import Client

# Simple in-memory cache for auth verification (token -> (user_data, expiry))
_auth_cache: dict[str, tuple[dict, datetime]] = {}


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


async def verify_user(authorization: str = Header(None)) -> dict:  # noqa: C901
    """Verify regular user via Supabase JWT.

    With the current schema, `account.id == auth.user.id`.
    Ensures the `accounts` row exists, creating it if necessary.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization.replace("Bearer ", "")

    # Check cache first (5 minute TTL)
    now = datetime.now(UTC)
    if token in _auth_cache:
        cached_data, expiry = _auth_cache[token]
        if expiry > now:
            return cached_data
        del _auth_cache[token]

    # Simple cache size limit
    if len(_auth_cache) > 100:
        _auth_cache.clear()

    sb = ensure_supabase()
    ac = _ensure_auth_client()

    try:
        user = ac.auth.get_user(token)
        if not user or not user.user:
            msg = "Invalid token"
            raise HTTPException(status_code=401, detail=msg)  # noqa: TRY301

        account_id = user.user.id

        # Ensure account exists
        try:
            result = sb.table("accounts").select("*").eq("id", account_id).single().execute()
            if not result.data:
                msg = "No data"
                raise ValueError(msg)  # noqa: TRY301
        except Exception:
            logger.info(f"Account not found for user {account_id}, creating...")
            try:
                create_result = (
                    sb.table("accounts")
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

        # Cache the result with 5 minute TTL
        expiry = now + timedelta(minutes=5)
        _auth_cache[token] = (user_data, expiry)

    except HTTPException:
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
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization.replace("Bearer ", "")

    sb = ensure_supabase()
    ac = _ensure_auth_client()

    try:
        user = ac.auth.get_user(token)
        if not user or not user.user:
            msg = "Invalid token"
            raise HTTPException(status_code=401, detail=msg)  # noqa: TRY301

        result = sb.table("accounts").select("is_admin").eq("id", user.user.id).single().execute()
        if not result.data or not result.data.get("is_admin"):
            msg = "Admin access required"
            raise HTTPException(status_code=403, detail=msg)  # noqa: TRY301
        return {"user_id": user.user.id, "email": user.user.email}  # noqa: TRY300
    except Exception:
        logger.exception("Admin verification error")
        msg = "Authentication failed"
        raise HTTPException(status_code=401, detail=msg) from None
