"""Account management routes."""

from datetime import UTC, datetime
from typing import Annotated, Any

from backend.deps import ensure_supabase, limiter, verify_user
from backend.models import AccountSetupResponse, AccountWithRelationsOut, AdminStatusOut
from fastapi import APIRouter, Depends, HTTPException, Request

router = APIRouter()


@router.get("/my/account", response_model=AccountWithRelationsOut)
@limiter.limit("30/minute")  # Reading account info
async def get_current_account(
    request: Request,
    user: Annotated[dict, Depends(verify_user)],
) -> dict[str, Any]:
    """Get current user's account with subscription and instances."""
    sb = ensure_supabase()

    account_id = user["account_id"]

    account_result = (
        sb.table("accounts").select("*, subscriptions(*, instances(*))").eq("id", account_id).single().execute()
    )

    if not account_result.data:
        raise HTTPException(status_code=404, detail="Account not found")

    return account_result.data


@router.get("/my/account/admin-status", response_model=AdminStatusOut)
@limiter.limit("30/minute")  # Reading admin status
async def check_admin_status(
    request: Request,
    user: Annotated[dict, Depends(verify_user)],
) -> dict[str, bool]:
    """Check if current user is an admin."""
    sb = ensure_supabase()

    account_id = user["account_id"]
    account_result = sb.table("accounts").select("is_admin").eq("id", account_id).single().execute()
    if not account_result.data:
        return AdminStatusOut(is_admin=False).model_dump()
    return AdminStatusOut(is_admin=bool(account_result.data.get("is_admin", False))).model_dump()


@router.post("/my/account/setup", response_model=AccountSetupResponse)
@limiter.limit("5/minute")
async def setup_account(request: Request, user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:  # noqa: ARG001
    """Setup free tier account for new user."""
    sb = ensure_supabase()

    account_id = user["account_id"]

    sub_result = sb.table("subscriptions").select("id").eq("account_id", account_id).execute()
    if sub_result.data:
        return {"message": "Account already setup", "account_id": account_id}

    subscription_data = {
        "account_id": account_id,
        "tier": "free",
        "status": "active",
        "max_agents": 1,
        "max_messages_per_day": 100,
        "created_at": datetime.now(UTC).isoformat(),
    }

    sub_result = sb.table("subscriptions").insert(subscription_data).execute()

    return {
        "message": "Free tier account created",
        "account_id": account_id,
        "subscription": sub_result.data[0] if sub_result.data else None,
    }
