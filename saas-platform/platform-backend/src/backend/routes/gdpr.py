"""
GDPR compliance endpoints for data export and deletion.
KISS principle - simple, straightforward implementation.
"""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.config import ACCOUNT_DELETION_GRACE_DAYS, logger, stripe
from backend.deps import ensure_supabase, invalidate_account_auth_cache, verify_user, verify_user_allow_deleted
from backend.models import (
    GdprCancelDeletionResponse,
    GdprConsentResponse,
    GdprDeletionResponse,
    GdprExportResponse,
)
from backend.services import instance_lifecycle, instances_data

router = APIRouter()


class ConsentUpdate(BaseModel):
    """Model for consent update requests."""

    marketing: bool = False
    analytics: bool = False


class DeletionRequest(BaseModel):
    """Model for account deletion requests."""

    confirmation: bool = False


@router.get("/my/gdpr/export-data", response_model=GdprExportResponse)
async def export_user_data(user: Annotated[dict, Depends(verify_user_allow_deleted)]) -> dict[str, Any]:
    """
    Export all user data for GDPR compliance.
    Returns all personal data in machine-readable format.
    """
    account_id = user["account_id"]
    sb = ensure_supabase()

    # Get account data
    account_result = sb.table("accounts").select("*").eq("id", account_id).execute()
    account_data = account_result.data[0] if account_result.data else {}

    # Get subscription data
    subscription_result = sb.table("subscriptions").select("*").eq("account_id", account_id).execute()
    subscriptions = subscription_result.data or []

    # Get instances
    instances = instances_data.get_instances_for_account(sb, account_id)

    # Get usage metrics (last 90 days)
    usage_result = (
        sb.table("usage_metrics").select("*").in_("subscription_id", [s["id"] for s in subscriptions]).execute()
    )
    usage_metrics = usage_result.data or []

    # Get audit logs (non-sensitive fields only)
    audit_result = (
        sb.table("audit_logs")
        .select("id,action,resource_type,resource_id,created_at,success")
        .eq("account_id", account_id)
        .execute()
    )
    audit_logs = audit_result.data or []

    # Get payments (if any); payments.subscription_id holds the Stripe id, not subscriptions.id
    payment_result = sb.table("payments").select("*").eq("account_id", account_id).execute()
    payments = payment_result.data or []

    return {
        "export_date": datetime.now(UTC).isoformat(),
        "account_id": account_id,
        "personal_data": {
            "email": account_data.get("email"),
            "full_name": account_data.get("full_name"),
            "company_name": account_data.get("company_name"),
            "created_at": account_data.get("created_at"),
            "status": account_data.get("status"),
            "tier": account_data.get("tier"),
        },
        "subscriptions": subscriptions,
        "instances": instances,
        "usage_metrics": usage_metrics,
        "activity_history": audit_logs,
        "payments": payments,
        "data_processing_purposes": [
            "Service provision and operation",
            "Billing and payment processing",
            "Security and fraud prevention",
        ],
        "data_retention_periods": {
            "personal_data": (
                "Account deletion starts with a 7-day recovery period. After that, scheduled application-database "
                "cleanup attempts deletion when enabled; completion is not guaranteed."
            ),
            "audit_logs": (
                "After successful account deletion, a deletion audit record retains your account UUID. "
                "Separate audit-log cleanup may remove it later."
            ),
            "payment_info": (
                "Payment records keep the invoice, amount, and Stripe customer and subscription identifiers for "
                "accounting after account deletion; only their account_id column is cleared."
            ),
            "invoices": (
                "Stripe webhook event records are kept after account deletion with their Stripe payloads, which can "
                "include your account ID, Stripe customer ID, and invoice contact details; only their account_id "
                "column is cleared."
            ),
            "external_data": (
                "Account cleanup uninstalls hosted instances with their Matrix homeserver data and persistent volumes "
                "and deletes the authentication user, but does not delete Stripe customer or subscription records or "
                "copies held by other Matrix homeservers; separate processor and operator policies apply."
            ),
        },
        "third_party_processors": [
            {
                "name": "Stripe",
                "purpose": "Payment processing",
                "data_shared": "Email only (payment details go directly to Stripe)",
            },
            {"name": "Supabase", "purpose": "Database hosting", "data_shared": "Account and instance data"},
        ],
    }


@router.post("/my/gdpr/request-deletion", response_model=GdprDeletionResponse)
async def request_account_deletion(
    user: Annotated[dict, Depends(verify_user)], request: DeletionRequest
) -> dict[str, Any]:
    """
    Request account and data deletion under GDPR Article 17.
    Requires explicit confirmation to prevent accidental deletion.
    """
    if not request.confirmation:
        return {
            "status": "confirmation_required",
            "message": "Please confirm deletion by setting confirmation=true",
            "warning": (
                "Confirming stops your hosted instances immediately and lets paid subscriptions end at the end of "
                "their current billing period. Scheduled cleanup becomes eligible after 7 days. "
                "You can cancel the request within those 7 days, which keeps your subscription. "
                "Completed application-database deletion cannot be undone; "
                "retained and external data have separate limits."
            ),
        }

    account_id = user["account_id"]
    sb = ensure_supabase()

    # Schedule the end of billing first, so a Stripe failure leaves the account untouched and can simply be retried.
    try:
        scheduled = await instance_lifecycle.end_account_billing_at_period_end(account_id)
    except stripe.StripeError as exc:
        detail = "Stripe could not schedule the end of your subscription, so your account was not deleted. Try again."
        raise HTTPException(status_code=502, detail=detail) from exc

    try:
        # Log the deletion request
        sb.table("audit_logs").insert(
            {
                "account_id": account_id,
                "action": "gdpr_deletion_requested",
                "resource_type": "account",
                "resource_id": account_id,
                "success": True,
                "created_at": datetime.now(UTC).isoformat(),
            }
        ).execute()
        # Soft-delete now; the optional cleanup scheduler uninstalls the instances and deletes the rows after 7 days.
        # External data is outside this RPC.
        sb.rpc(
            "soft_delete_account",
            {"target_account_id": account_id, "reason": "gdpr_request", "requested_by": account_id},
        ).execute()
    except Exception as exc:
        logger.exception("Could not record the deletion request of account %s", account_id)
        # The soft delete may have committed before its response was lost; then the deletion stands.
        if not _deletion_recorded_after_all(sb, account_id, scheduled):
            raise HTTPException(status_code=500, detail=await _undo_scheduled_billing_end(scheduled)) from exc
    # The account is pending deletion now, so its cached sign-in must not keep full access.
    invalidate_account_auth_cache(account_id)
    # An account pending deletion never runs instances, so this holds them until cleanup.
    hold_errors = await instance_lifecycle.reconcile_account_instances(account_id)
    # Cancelling cannot be undone, so subscriptions without a paid period are cancelled only now the deletion is
    # recorded. The deletion stands whatever fails here, and the nightly cleanup repeats this step.
    try:
        await instance_lifecycle.cancel_unpaid_subscriptions(account_id)
    except Exception:
        logger.exception("Could not cancel the unpaid subscriptions of account %s; cleanup retries", account_id)
    instances = (
        "Stopping your hosted instances failed and is retried automatically."
        if hold_errors
        else "Your hosted instances were stopped."
    )

    return {
        "status": "deletion_scheduled",
        "message": (
            f"Your account is scheduled for deletion. {instances} "
            "Paid subscriptions end at the end of their current billing period unless you cancel the deletion."
        ),
        "grace_period_days": ACCOUNT_DELETION_GRACE_DAYS,
        "deletion_date": (
            "Eligible for scheduled application-database cleanup after 7 days, when cleanup is enabled; "
            "completion is not guaranteed"
        ),
        "cancellation": (
            f"Within {ACCOUNT_DELETION_GRACE_DAYS} days, sign in and select Cancel Deletion Request in Settings, "
            "or call POST /my/gdpr/cancel-deletion. Signing in alone does not cancel deletion. "
            "Cancelling keeps a subscription that has not reached the end of its billing period."
        ),
        "data_deleted": (
            "Cleanup uninstalls hosted instances with their Matrix homeserver data, persistent volumes, and "
            "platform-paid AI keys, then targets application-database account, subscription, instance, "
            "existing account-linked audit-log, and subscription-linked usage records, and finally deletes the "
            "authentication user"
        ),
        "data_retained": (
            "After successful account deletion, a deletion audit record retains your account UUID. "
            "Separate audit-log cleanup may remove it later. "
            "Payment records and Stripe webhook event records are kept for accounting with only their account_id "
            "column cleared; they keep Stripe identifiers and event payloads that can include your account ID and "
            "invoice contact details. "
            "Cleanup deletes the authentication user last, but does not delete Stripe customer or subscription records "
            "or copies held by other Matrix homeservers; separate processor and operator policies apply."
        ),
    }


def _deletion_recorded_after_all(
    sb: Any, account_id: str, scheduled: list[instance_lifecycle.ScheduledBillingEnd]
) -> bool:
    """Return whether a deletion whose recording raised was stored anyway; answer 500 when that is unknown."""
    try:
        return instance_lifecycle.account_pending_deletion(sb, account_id)
    except Exception as lookup_error:
        logger.exception("Could not check whether the deletion of account %s was recorded", account_id)
        billing = "is unchanged" if not scheduled else "may be set to end at the end of its billing period"
        detail = (
            "We could not confirm whether your deletion request was recorded, and your subscription "
            f"{billing}. Reload your settings: cancel the deletion if it is pending, or request it again."
        )
        raise HTTPException(status_code=500, detail=detail) from lookup_error


async def _undo_scheduled_billing_end(scheduled: list[instance_lifecycle.ScheduledBillingEnd]) -> str:
    """Resume the billing a failed deletion request had set to end, and describe what the customer is left with."""
    try:
        await instance_lifecycle.resume_subscriptions(scheduled)
    except stripe.StripeError:
        logger.exception("Could not resume the Stripe subscriptions a failed deletion request set to end")
        return (
            "Your account was not deleted, but your subscription is still set to end at the end of its billing "
            "period; resume it from the billing page or try the deletion again."
        )
    return "Your account was not deleted and your billing is unchanged. Try again."


@router.post("/my/gdpr/consent", response_model=GdprConsentResponse)
async def update_consent(user: Annotated[dict, Depends(verify_user)], consent: ConsentUpdate) -> dict[str, Any]:
    """
    Update user consent preferences for GDPR compliance.
    """
    account_id = user["account_id"]
    sb = ensure_supabase()

    # Store consent preferences
    # In production, this would be a separate consent table
    sb.table("accounts").update(
        {
            "consent_marketing": consent.marketing,
            "consent_analytics": consent.analytics,
            "consent_updated_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
    ).eq("id", account_id).execute()

    # Log consent update
    sb.table("audit_logs").insert(
        {
            "account_id": account_id,
            "action": "consent_updated",
            "resource_type": "account",
            "resource_id": account_id,
            "details": {"marketing": consent.marketing, "analytics": consent.analytics},
            "success": True,
            "created_at": datetime.now(UTC).isoformat(),
        }
    ).execute()

    return {
        "status": "success",
        "consent": {
            "marketing": consent.marketing,
            "analytics": consent.analytics,
            "essential": True,  # Always required for service
        },
        "updated_at": datetime.now(UTC).isoformat(),
    }


@router.post("/my/gdpr/cancel-deletion", response_model=GdprCancelDeletionResponse)
async def cancel_account_deletion(user: Annotated[dict, Depends(verify_user_allow_deleted)]) -> dict[str, Any]:
    """
    Cancel a pending account deletion request.
    Only works if account is still in soft-delete state.
    """
    account_id = user["account_id"]
    sb = ensure_supabase()

    if not instance_lifecycle.account_pending_deletion(sb, account_id):
        return {"status": "not_pending", "message": "No deletion request found for this account"}

    # The RPC restores the account and records the cancellation in one transaction. It refuses after the grace
    # period, when cleanup may already have uninstalled the instances, and for a suspended account.
    restored = sb.rpc("restore_account", {"target_account_id": account_id}).execute().data
    if not restored:
        raise HTTPException(status_code=409, detail="This account deletion can no longer be cancelled")
    # The account is active again, so a cached pending-deletion sign-in must not limit its next request.
    invalidate_account_auth_cache(account_id)
    try:
        await instance_lifecycle.resume_account_billing(account_id)
    except stripe.StripeError:
        logger.exception("Could not resume Stripe billing after account %s cancelled its deletion", account_id)
        billing = (
            " Stripe could not resume your subscription, so it still ends at the end of its billing period; "
            "resume it from the billing page."
        )
    else:
        billing = ""
    # Instances held for the deletion restart only while their subscription is entitled.
    restart_errors = await instance_lifecycle.reconcile_account_instances(account_id)
    instances = (
        " Restarting your hosted instances failed and is retried automatically."
        if restart_errors
        else " Hosted instances run again while your subscription is active; choose a plan again if it has ended."
    )

    return {
        "status": "success",
        "message": f"Account deletion request has been cancelled.{billing}{instances}",
        "account_status": "active",
    }
