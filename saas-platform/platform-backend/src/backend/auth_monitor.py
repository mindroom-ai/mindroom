"""
Authentication failure monitoring for security.
Simple in-memory tracking with automatic blocking.
"""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
import logging

from backend.metrics import record_auth_event, set_blocked_ip_count
from backend.utils.audit import create_audit_log

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
        set_blocked_ip_count(len(blocked_ips))
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

    # Log to database for audit and bump metrics
    create_audit_log(
        action="auth_failed",
        resource_type="authentication",
        account_id=user_id,
        ip_address=ip_address,
        success=False,
    )
    record_auth_event(actor="user", outcome="failure")

    # Check if threshold exceeded and block if needed
    if len(failed_attempts[ip_address]) >= MAX_FAILURES:
        blocked_ips[ip_address] = datetime.now(UTC)
        logger.warning(f"Blocked IP {ip_address} due to too many failed auth attempts")

        create_audit_log(
            action="ip_blocked",
            resource_type="security",
            details={
                "reason": "excessive_auth_failures",
                "attempts": len(failed_attempts[ip_address]),
            },
            ip_address=ip_address,
            success=True,
        )
        record_auth_event(actor="user", outcome="blocked")
        set_blocked_ip_count(len(blocked_ips))
        return True

    return False


def record_success(ip_address: str, user_id: str = None):
    """Record a successful authentication."""
    # Clear failures for this IP on success
    if ip_address in failed_attempts:
        del failed_attempts[ip_address]

    # Log successful auth
    create_audit_log(
        action="auth_success",
        resource_type="authentication",
        account_id=user_id,
        ip_address=ip_address,
        success=True,
    )
    record_auth_event(actor="user", outcome="success")
    set_blocked_ip_count(len(blocked_ips))
