"""
Authentication failure monitoring for security.
Simple in-memory tracking with automatic blocking.
"""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
import logging

from backend.config import supabase

logger = logging.getLogger(__name__)

# Configuration
MAX_FAILURES = 5
WINDOW_MINUTES = 15
BLOCK_DURATION_MINUTES = 30

# In-memory tracking (resets on restart - that's OK for simplicity)
failed_attempts = defaultdict(list)
blocked_ips = {}


def is_blocked(ip_address: str) -> bool:
    """Check if an IP is currently blocked."""
    if ip_address not in blocked_ips:
        return False

    # Check if block has expired
    block_time = blocked_ips[ip_address]
    if datetime.now(UTC) - block_time > timedelta(minutes=BLOCK_DURATION_MINUTES):
        del blocked_ips[ip_address]
        return False

    return True


def record_failure(ip_address: str, user_id: str = None) -> bool:
    """
    Record an authentication failure.
    Returns True if IP should be blocked.
    """
    now = datetime.now(UTC)

    # Clean old attempts
    cutoff = now - timedelta(minutes=WINDOW_MINUTES)
    failed_attempts[ip_address] = [attempt for attempt in failed_attempts[ip_address] if attempt > cutoff]

    # Add new failure
    failed_attempts[ip_address].append(now)

    # Log to database for audit
    try:
        if supabase:
            supabase.table("audit_logs").insert(
                {
                    "account_id": user_id,
                    "action": "auth_failed",
                    "resource_type": "authentication",
                    "ip_address": ip_address,
                    "success": False,
                    "created_at": now.isoformat(),
                }
            ).execute()
    except Exception as e:
        logger.error(f"Failed to log auth failure: {e}")

    # Check if threshold exceeded
    if len(failed_attempts[ip_address]) >= MAX_FAILURES:
        _block_ip(ip_address)
        return True

    return False


def _block_ip(ip_address: str):
    """Block an IP address."""
    blocked_ips[ip_address] = datetime.now(UTC)

    # Log the block
    logger.warning(f"Blocked IP {ip_address} due to too many failed auth attempts")

    try:
        if supabase:
            supabase.table("audit_logs").insert(
                {
                    "action": "ip_blocked",
                    "resource_type": "security",
                    "details": {
                        "ip_address": ip_address,
                        "reason": "excessive_auth_failures",
                        "attempts": len(failed_attempts[ip_address]),
                    },
                    "ip_address": ip_address,
                    "success": True,
                    "created_at": datetime.now(UTC).isoformat(),
                }
            ).execute()
    except Exception as e:
        logger.error(f"Failed to log IP block: {e}")


def record_success(ip_address: str, user_id: str = None):
    """Record a successful authentication."""
    # Clear failures for this IP on success
    if ip_address in failed_attempts:
        del failed_attempts[ip_address]

    # Log successful auth
    try:
        if supabase:
            supabase.table("audit_logs").insert(
                {
                    "account_id": user_id,
                    "action": "auth_success",
                    "resource_type": "authentication",
                    "ip_address": ip_address,
                    "success": True,
                    "created_at": datetime.now(UTC).isoformat(),
                }
            ).execute()
    except Exception as e:
        logger.error(f"Failed to log auth success: {e}")
