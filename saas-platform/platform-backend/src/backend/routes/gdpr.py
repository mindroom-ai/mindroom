"""
GDPR compliance endpoints for data export and deletion.
KISS principle - simple, straightforward implementation.
"""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.deps import ensure_supabase, verify_user
from backend.models import (
    GdprCancelDeletionResponse,
    GdprConsentResponse,
    GdprDeletionResponse,
    GdprExportResponse,
)
from backend.services import instances_data

router = APIRouter()


class ConsentUpdate(BaseModel):
    """Model for consent update requests."""

    marketing: bool = False
    analytics: bool = False


class DeletionRequest(BaseModel):
    """Model for account deletion requests."""

    confirmation: bool = False


@router.get("/my/gdpr/export-data", response_model=GdprExportResponse)
async def export_user_data(user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
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

    # Get payments (if any)
    payment_result = sb.table("payments").select("*").in_("subscription_id", [s["id"] for s in subscriptions]).execute()
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
            "payment_info": "Payment and webhook records retain account references and payment identifiers.",
            "invoices": "Payment and webhook records are not removed by account cleanup and can prevent deletion.",
            "external_data": (
                "Account cleanup does not delete the authentication user, Stripe customer or subscription data, "
                "Matrix data, or persistent volumes; separate processor and operator policies apply."
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
                "You have a 7-day recovery period to cancel this request explicitly. "
                "Completed application-database deletion cannot be undone; "
                "retained and external data have separate limits."
            ),
        }

    account_id = user["account_id"]
    sb = ensure_supabase()

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

    # Soft-delete now; the optional cleanup scheduler attempts database deletion after 7 days.
    # Payment/webhook references can block cleanup; external data is outside this RPC.
    sb.rpc(
        "soft_delete_account", {"target_account_id": account_id, "reason": "gdpr_request", "requested_by": account_id}
    ).execute()

    return {
        "status": "deletion_scheduled",
        "message": "Your account has been scheduled for deletion",
        "grace_period_days": 7,
        "deletion_date": (
            "Eligible for scheduled application-database cleanup after 7 days, when cleanup is enabled; "
            "completion is not guaranteed"
        ),
        "cancellation": (
            "Within 7 days, sign in and select Cancel Deletion Request in Settings, "
            "or call POST /my/gdpr/cancel-deletion. Signing in alone does not cancel deletion."
        ),
        "data_deleted": (
            "Cleanup targets application-database account, subscription, instance, audit-log, "
            "and subscription-linked usage records"
        ),
        "data_retained": (
            "Payment and webhook records retain account references and can prevent cleanup. "
            "Cleanup does not delete the authentication user, Stripe customer or subscription data, "
            "Matrix data, or persistent volumes; separate processor and operator policies apply."
        ),
    }


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
async def cancel_account_deletion(user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """
    Cancel a pending account deletion request.
    Only works if account is still in soft-delete state.
    """
    account_id = user["account_id"]
    sb = ensure_supabase()

    # Check if account is soft-deleted
    account_result = sb.table("accounts").select("deleted_at").eq("id", account_id).execute()
    if not account_result.data or not account_result.data[0].get("deleted_at"):
        return {"status": "not_pending", "message": "No deletion request found for this account"}

    # The RPC restores the account and records the cancellation in one transaction.
    sb.rpc("restore_account", {"target_account_id": account_id}).execute()

    return {"status": "success", "message": "Account deletion request has been cancelled", "account_status": "active"}
