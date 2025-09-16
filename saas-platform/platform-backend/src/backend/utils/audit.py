"""
Shared audit logging utilities.
KISS principle - simple function for consistent audit logging.
"""

from datetime import UTC, datetime
import logging

from backend.config import supabase

logger = logging.getLogger(__name__)


def create_audit_log(
    action: str,
    resource_type: str,
    account_id: str = None,
    resource_id: str = None,
    details: dict = None,
    ip_address: str = None,
    success: bool = True,
) -> None:
    """
    Create an audit log entry in the database.

    Args:
        action: The action being performed (e.g., "auth_failed", "ip_blocked")
        resource_type: Type of resource (e.g., "authentication", "security")
        account_id: ID of the account performing the action
        resource_id: ID of the specific resource being acted upon
        details: Additional details about the action
        ip_address: IP address of the request
        success: Whether the action was successful
    """
    try:
        if not supabase:
            return

        log_entry = {
            "account_id": account_id,
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "details": details,
            "ip_address": ip_address,
            "success": success,
            "created_at": datetime.now(UTC).isoformat(),
        }

        supabase.table("audit_logs").insert(log_entry).execute()
    except Exception as e:
        # Audit logging is best-effort, don't fail the main operation
        logger.error(f"Failed to create audit log: {e}")
