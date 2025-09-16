"""
GDPR compliance endpoints for data export and deletion.
KISS principle - simple, straightforward implementation.
"""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from backend.deps import ensure_supabase, verify_user

router = APIRouter()


@router.get("/my/gdpr/export-data")
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
    instance_result = sb.table("instances").select("*").eq("account_id", account_id).execute()
    instances = instance_result.data or []

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
            "Customer support",
            "Legal compliance",
            "Security and fraud prevention",
        ],
        "data_retention_periods": {
            "account_data": "7 years from account closure",
            "usage_metrics": "3 years from generation",
            "audit_logs": "7 years from creation",
            "payment_data": "7 years for tax compliance",
        },
        "third_party_processors": [
            {
                "name": "Stripe",
                "purpose": "Payment processing",
                "data_shared": "Email, payment details",
            },
            {
                "name": "Supabase",
                "purpose": "Database and authentication",
                "data_shared": "All user data",
            },
            {
                "name": "Kubernetes/Cloud Provider",
                "purpose": "Infrastructure hosting",
                "data_shared": "Instance configurations",
            },
        ],
    }


@router.post("/my/gdpr/request-deletion")
async def request_account_deletion(
    user: Annotated[dict, Depends(verify_user)],
    confirmation: bool = False,
) -> dict[str, Any]:
    """
    Request account and data deletion under GDPR Article 17.
    Requires explicit confirmation to prevent accidental deletion.
    """
    if not confirmation:
        return {
            "status": "confirmation_required",
            "message": "Please confirm deletion by setting confirmation=true",
            "warning": "This action cannot be undone. All your data will be permanently deleted.",
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

    # Use soft delete function for GDPR compliance
    # This provides a 30-day grace period before hard deletion
    try:
        # Call the soft delete function
        sb.rpc(
            "soft_delete_account",
            {"target_account_id": account_id, "reason": "gdpr_request", "requested_by": account_id},
        ).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to process deletion request: {str(e)}")

    return {
        "status": "deletion_scheduled",
        "message": "Your account has been scheduled for deletion",
        "grace_period_days": 30,
        "deletion_date": "Account will be deleted after 30 days",
        "cancellation": "You can cancel this request by logging in within 30 days",
    }


@router.post("/my/gdpr/consent")
async def update_consent(
    user: Annotated[dict, Depends(verify_user)],
    marketing: bool = False,
    analytics: bool = False,
) -> dict[str, Any]:
    """
    Update user consent preferences for GDPR compliance.
    """
    account_id = user["account_id"]
    sb = ensure_supabase()

    # Store consent preferences
    # In production, this would be a separate consent table
    sb.table("accounts").update(
        {
            "consent_marketing": marketing,
            "consent_analytics": analytics,
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
            "details": {
                "marketing": marketing,
                "analytics": analytics,
            },
            "success": True,
            "created_at": datetime.now(UTC).isoformat(),
        }
    ).execute()

    return {
        "status": "success",
        "consent": {
            "marketing": marketing,
            "analytics": analytics,
            "essential": True,  # Always required for service
        },
        "updated_at": datetime.now(UTC).isoformat(),
    }


@router.post("/my/gdpr/cancel-deletion")
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
        return {
            "status": "not_pending",
            "message": "No deletion request found for this account",
        }

    try:
        # Restore the account
        sb.rpc("restore_account", {"target_account_id": account_id}).execute()

        # Log the cancellation
        sb.table("audit_logs").insert(
            {
                "account_id": account_id,
                "action": "gdpr_deletion_cancelled",
                "resource_type": "account",
                "resource_id": account_id,
                "success": True,
                "created_at": datetime.now(UTC).isoformat(),
            }
        ).execute()

        return {
            "status": "success",
            "message": "Account deletion request has been cancelled",
            "account_status": "active",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to cancel deletion: {str(e)}")
