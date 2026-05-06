"""
Shared audit logging utilities.
KISS principle - simple function for consistent audit logging.
"""

from datetime import UTC, datetime
import logging
import re
from typing import Any

from backend.config import supabase

logger = logging.getLogger(__name__)
REDACTED = "***redacted***"
_SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "client_secret",
        "cookie",
        "credit_card",
        "id_token",
        "password",
        "refresh_token",
        "secret",
        "set_cookie",
        "token",
    }
)
_SECRET_KEY_VARIANTS = tuple(
    (key, key.replace("_", ""), tuple(key.split("_"))) for key in sorted(_SECRET_KEYS, key=len, reverse=True)
)


def _normalize_key(value: object) -> str:
    key = str(value).strip()
    key = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key)
    key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def _is_secret_key(value: object) -> bool:
    normalized = _normalize_key(value)
    parts = tuple(part for part in normalized.split("_") if part)
    compact = normalized.replace("_", "")
    for key, compact_key, key_parts in _SECRET_KEY_VARIANTS:
        if (
            normalized == key
            or normalized.endswith(f"_{key}")
            or compact == compact_key
            or compact.endswith(compact_key)
        ):
            return True
        for start in range(len(parts) - len(key_parts) + 1):
            if parts[start : start + len(key_parts)] == key_parts:
                return True
    return False


def redact_audit_details(value: Any) -> Any:  # noqa: ANN401
    """Recursively redact credential-bearing fields from audit details."""
    if isinstance(value, dict):
        return {
            str(key): REDACTED if _is_secret_key(key) else redact_audit_details(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_audit_details(item) for item in value]
    return value


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
            "details": redact_audit_details(details),
            "ip_address": ip_address,
            "success": success,
            "created_at": datetime.now(UTC).isoformat(),
        }

        supabase.table("audit_logs").insert(log_entry).execute()
    except Exception as e:
        # Audit logging is best-effort, don't fail the main operation
        logger.error(f"Failed to create audit log: {e}")
