"""Shared FastAPI dependency functions for auth and context."""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import hmac
import ipaddress
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import jwt
from backend import auth_monitor
from backend.metrics import record_admin_verification, record_auth_event
from backend.config import TRUSTED_PROXY_NETWORKS, auth_client, logger, supabase
from backend.utils.audit import AuditActor, record_audit_actor
from fastapi import Header, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

if TYPE_CHECKING:
    from supabase import Client

AUTH_CACHE_MAX_ENTRIES = 100
AUTH_CACHE_MAX_TTL_SECONDS = 300
ACTIVE_ACCOUNT_STATUS = "active"
# Status the soft-delete RPC sets alongside deleted_at; only such accounts keep GDPR self-service access.
# Limiting the allowance to this status is what stops a suspended account awaiting deletion from lifting
# its suspension through cancel-deletion.
PENDING_DELETION_ACCOUNT_STATUS = "deleted"


@dataclass(frozen=True)
class AuthCacheEntry:
    """Cached auth result of an active account, bounded by the JWT expiration."""

    expires_at: datetime
    account_id: str
    user_data: dict[str, Any]


_auth_cache: OrderedDict[str, AuthCacheEntry] = OrderedDict()


def _auth_cache_key(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def _token_cache_deadline(token: str, now: datetime) -> datetime | None:
    try:
        claims = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
    except jwt.InvalidTokenError:
        return None

    exp = claims.get("exp")
    if not isinstance(exp, int | float):
        return None

    token_expires_at = datetime.fromtimestamp(exp, UTC)
    if token_expires_at <= now:
        return None

    max_cache_expires_at = now + timedelta(seconds=AUTH_CACHE_MAX_TTL_SECONDS)
    return min(token_expires_at, max_cache_expires_at)


def _cached_user_data(token: str, now: datetime) -> dict[str, Any] | None:
    cache_key = _auth_cache_key(token)
    entry = _auth_cache.get(cache_key)
    if entry is None:
        return None

    if entry.expires_at <= now:
        _auth_cache.pop(cache_key, None)
        return None

    _auth_cache.move_to_end(cache_key)
    return deepcopy(entry.user_data)


def _store_auth_cache(token: str, user_data: dict[str, Any], now: datetime) -> None:
    expires_at = _token_cache_deadline(token, now)
    if expires_at is None:
        return

    cache_key = _auth_cache_key(token)
    _auth_cache[cache_key] = AuthCacheEntry(
        expires_at=expires_at, account_id=str(user_data["account_id"]), user_data=deepcopy(user_data)
    )
    _auth_cache.move_to_end(cache_key)
    while len(_auth_cache) > AUTH_CACHE_MAX_ENTRIES:
        _auth_cache.popitem(last=False)


def invalidate_account_auth_cache(account_id: str) -> None:
    """Drop cached auth for one account so a status or role change applies to its next request."""
    for cache_key in [key for key, entry in _auth_cache.items() if entry.account_id == account_id]:
        del _auth_cache[cache_key]


def _account_is_active(account: dict[str, Any]) -> bool:
    return account.get("status") == ACTIVE_ACCOUNT_STATUS and account.get("deleted_at") is None


def _account_is_pending_deletion(account: dict[str, Any]) -> bool:
    return account.get("status") == PENDING_DELETION_ACCOUNT_STATUS and account.get("deleted_at") is not None


def account_may_sign_in(account: dict[str, Any]) -> bool:
    """Whether some platform route admits the account, so its owner must still be able to sign in."""
    return _account_is_active(account) or _account_is_pending_deletion(account)


def _require_account_access(account: dict[str, Any], *, allow_pending_deletion: bool) -> None:
    """Reject suspended, unverified, and deleted accounts; optionally admit accounts awaiting deletion."""
    if _account_is_active(account) or (allow_pending_deletion and _account_is_pending_deletion(account)):
        return
    raise HTTPException(status_code=403, detail="Account is not active")


def _ip_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def client_ip_from_request(request: Request) -> str:
    """Return the end-user IP for auth monitoring and rate limiting.

    Only a trusted proxy (`TRUSTED_PROXY_CIDRS`) may name the client in `X-Real-IP`, which ingress-nginx overwrites;
    any other caller is keyed by its own connection address, so it cannot choose its rate-limit or lockout key.
    """
    peer = get_remote_address(request)
    peer_ip = _ip_address(peer)
    if peer_ip is not None and any(peer_ip in network for network in TRUSTED_PROXY_NETWORKS):
        real_ip = _ip_address(request.headers.get("x-real-ip", "").strip())
        if real_ip is not None:
            return str(real_ip)
    return peer


def rate_limit_key(request: Request) -> str:
    """Key rate limits by the client IP reported by the trusted ingress."""
    return client_ip_from_request(request)


# Global rate limiter for the FastAPI app and routes
limiter = Limiter(key_func=rate_limit_key)


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


async def _authenticate_user(  # noqa: C901, PLR0912
    authorization: str | None, request: Request | None, *, allow_pending_deletion: bool
) -> dict:
    """Authenticate a Supabase JWT and load its account row, refusing accounts `_require_account_access` rejects.

    With the current schema, `account.id == auth.user.id`.
    Ensures the `accounts` row exists, creating it if necessary.
    Only active accounts are cached, so a cache hit passes either access check,
    and a restored account is re-read on its next request.
    A refused account is not recorded as a successful authentication.
    """
    # Get client IP for monitoring
    client_ip = client_ip_from_request(request) if request is not None else "unknown"

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

    now = datetime.now(UTC)
    cached_user_data = _cached_user_data(token, now)
    if cached_user_data is not None:
        logger.info("Auth cache hit (instant)")
        return cached_user_data

    # Start timing for database lookup
    start = time.perf_counter()
    ac = _ensure_auth_client()

    try:
        user = ac.auth.get_user(token)
        if not user or not user.user:
            # Record auth failure
            auth_monitor.record_failure(client_ip)
            msg = "Invalid token"
            raise HTTPException(status_code=401, detail=msg)  # noqa: TRY301

        account_id = user.user.id
        sb = ensure_supabase()

        # Ensure account exists
        try:
            result = sb.table("accounts").select("*").eq("id", account_id).single().execute()
            if not result.data:
                msg = "No data"
                raise ValueError(msg)  # noqa: TRY301
            account = result.data
        except Exception:
            logger.info(f"Account not found for user {account_id}, creating...")
            try:
                now_iso = now.isoformat()
                create_result = (
                    sb.table("accounts")
                    .insert(
                        {
                            "id": account_id,
                            "email": user.user.email,
                            "full_name": user.user.user_metadata.get("full_name", "")
                            if user.user.user_metadata
                            else "",
                            "created_at": now_iso,
                            "updated_at": now_iso,
                        }
                    )
                    .execute()
                )
                # Inserts return the list of created rows, including column defaults such as `status`.
                account = create_result.data[0]
            except Exception:
                logger.exception("Failed to create account")
                # Try to fetch again in case it was a race condition
                result = sb.table("accounts").select("*").eq("id", account_id).single().execute()
                if not result.data:
                    msg = "Account creation failed. Please contact support."
                    raise HTTPException(status_code=404, detail=msg) from None
                account = result.data

        _require_account_access(account, allow_pending_deletion=allow_pending_deletion)
        auth_monitor.record_success(client_ip, str(account_id))

        # Prepare response data
        user_data = {
            "user_id": user.user.id,
            "email": user.user.email,
            "account_id": account_id,
            "account": account,
        }

        if _account_is_active(account):
            _store_auth_cache(token, user_data, now)

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


def _record_audit_actor(request: Request | None, account_id: object, email: str | None) -> None:
    if request is not None:
        record_audit_actor(request, AuditActor(account_id=str(account_id), email=email))


async def verify_user(authorization: str = Header(None), request: Request = None) -> dict:
    """Verify a user via Supabase JWT whose account is active."""
    user_data = await _authenticate_user(authorization, request, allow_pending_deletion=False)
    _record_audit_actor(request, user_data["account_id"], user_data["email"])
    return user_data


async def verify_user_allow_deleted(authorization: str = Header(None), request: Request = None) -> dict:
    """Verify a user whose account is active or awaiting deletion.

    Only GDPR self-service routes use this, so an account pending deletion can read itself, export its data,
    and cancel the deletion; suspended and unverified accounts are still rejected.
    """
    user_data = await _authenticate_user(authorization, request, allow_pending_deletion=True)
    _record_audit_actor(request, user_data["account_id"], user_data["email"])
    return user_data


async def verify_admin(authorization: str = Header(None), request: Request = None) -> dict:
    """Verify admin access via Supabase auth for an active admin account."""
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

        result = sb.table("accounts").select("is_admin,status,deleted_at").eq("id", user.user.id).single().execute()
        if not result.data or not result.data.get("is_admin") or not _account_is_active(result.data):
            msg = "Admin access required"
            record_admin_verification("forbidden")
            raise HTTPException(status_code=403, detail=msg)  # noqa: TRY301
        record_admin_verification("success")
        _record_audit_actor(request, user.user.id, user.user.email)
        return {"user_id": user.user.id, "email": user.user.email}  # noqa: TRY300
    except HTTPException:
        raise
    except Exception:
        logger.exception("Admin verification error")
        msg = "Authentication failed"
        record_admin_verification("error")
        raise HTTPException(status_code=401, detail=msg) from None
