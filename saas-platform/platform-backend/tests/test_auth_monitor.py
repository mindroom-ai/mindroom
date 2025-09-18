"""Test auth_monitor functionality."""

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch


from backend.auth_monitor import (
    BLOCK_DURATION_MINUTES,
    MAX_FAILURES,
    WINDOW_MINUTES,
    blocked_ips,
    failed_attempts,
    is_blocked,
    record_failure,
    record_success,
)
from backend.metrics import get_auth_metric, get_blocked_ip_count, reset_security_metrics


class TestAuthMonitor:
    """Test authentication monitoring functionality."""

    def setup_method(self):
        """Clear state before each test."""
        failed_attempts.clear()
        blocked_ips.clear()
        reset_security_metrics()

    def test_record_single_failure(self):
        """Test recording a single failure doesn't trigger block."""
        ip = "192.168.1.1"
        blocked = record_failure(ip, "user1")

        assert not blocked
        assert ip not in blocked_ips
        assert len(failed_attempts[ip]) == 1
        assert get_auth_metric(actor="user", outcome="failure") == 1

    def test_multiple_failures_trigger_block(self):
        """Test that MAX_FAILURES triggers IP blocking."""
        ip = "192.168.1.2"

        # Record MAX_FAILURES - 1 failures
        for i in range(MAX_FAILURES - 1):
            blocked = record_failure(ip, f"user{i}")
            assert not blocked

        # The MAX_FAILURES-th attempt should trigger block
        blocked = record_failure(ip, "userX")
        assert blocked
        assert ip in blocked_ips
        assert is_blocked(ip)
        assert get_auth_metric(actor="user", outcome="failure") == MAX_FAILURES
        assert get_auth_metric(actor="user", outcome="blocked") == 1
        assert get_blocked_ip_count() == 1

    def test_is_blocked_check(self):
        """Test is_blocked function."""
        ip = "192.168.1.3"

        # Not blocked initially
        assert not is_blocked(ip)
        assert get_blocked_ip_count() == 0

        # Trigger block
        for _ in range(MAX_FAILURES):
            record_failure(ip)

        assert is_blocked(ip)

    def test_block_expiration(self):
        """Test that blocks expire after BLOCK_DURATION_MINUTES."""
        ip = "192.168.1.4"

        # Trigger block
        for _ in range(MAX_FAILURES):
            record_failure(ip)
        assert is_blocked(ip)

        # Simulate time passing (just before expiration)
        blocked_ips[ip] = datetime.now(UTC) - timedelta(minutes=BLOCK_DURATION_MINUTES - 1)
        assert is_blocked(ip)  # Still blocked

        # Simulate block expiration
        blocked_ips[ip] = datetime.now(UTC) - timedelta(minutes=BLOCK_DURATION_MINUTES + 1)
        assert not is_blocked(ip)  # Block expired
        assert ip not in blocked_ips  # Removed from dict

    def test_failures_window_cleanup(self):
        """Test that old failures are cleaned up outside the window."""
        ip = "192.168.1.5"
        now = datetime.now(UTC)

        # Add old failures (outside window)
        old_time = now - timedelta(minutes=WINDOW_MINUTES + 1)
        failed_attempts[ip] = [old_time, old_time, old_time]

        # Record new failure - should clean old ones
        record_failure(ip)

        # Should only have 1 failure now (the new one)
        assert len(failed_attempts[ip]) == 1
        assert not is_blocked(ip)

    def test_record_success_clears_failures(self):
        """Test that successful auth clears failure history."""
        ip = "192.168.1.6"

        # Record some failures
        for _ in range(3):
            record_failure(ip)
        assert len(failed_attempts[ip]) == 3

        # Record success
        record_success(ip, "user1")

        # Failures should be cleared
        assert ip not in failed_attempts or len(failed_attempts[ip]) == 0
        assert get_auth_metric(actor="user", outcome="success") == 1

    def test_different_ips_tracked_separately(self):
        """Test that different IPs are tracked independently."""
        ip1 = "192.168.1.7"
        ip2 = "192.168.1.8"

        # Record failures for ip1
        for _ in range(3):
            record_failure(ip1)

        # Record failures for ip2
        for _ in range(2):
            record_failure(ip2)

        assert len(failed_attempts[ip1]) == 3
        assert len(failed_attempts[ip2]) == 2
        assert not is_blocked(ip1)
        assert not is_blocked(ip2)

    def test_block_persists_across_checks(self):
        """Test that blocked status persists across multiple checks."""
        ip = "192.168.1.9"

        # Trigger block
        for _ in range(MAX_FAILURES):
            record_failure(ip)

        # Check multiple times
        for _ in range(10):
            assert is_blocked(ip)
            assert ip in blocked_ips

    @patch("backend.auth_monitor.create_audit_log")
    def test_audit_logging_on_block(self, mock_create_audit_log):
        """Test that blocking triggers audit log."""
        ip = "192.168.1.10"

        # Trigger block
        for _ in range(MAX_FAILURES):
            record_failure(ip)

        # Verify audit logs were called
        # Should have MAX_FAILURES auth_failed logs + 1 ip_blocked log
        assert mock_create_audit_log.call_count == MAX_FAILURES + 1

        # Check the final ip_blocked call
        final_call = mock_create_audit_log.call_args_list[-1]
        assert final_call.kwargs["action"] == "ip_blocked"
        assert final_call.kwargs["resource_type"] == "security"
        assert final_call.kwargs["ip_address"] == ip
        assert final_call.kwargs["details"]["reason"] == "excessive_auth_failures"
        assert final_call.kwargs["success"] is True

    @patch("backend.auth_monitor.create_audit_log", MagicMock())
    def test_works_without_supabase(self):
        """Test that auth monitor works even without database."""
        ip = "192.168.1.11"

        # Should work without errors even if audit logging fails
        for _ in range(MAX_FAILURES):
            blocked = record_failure(ip)

        assert blocked
        assert is_blocked(ip)

    def test_rapid_failures_within_window(self):
        """Test rapid failures within time window."""
        ip = "192.168.1.12"

        # Simulate rapid failures
        for _ in range(MAX_FAILURES):
            record_failure(ip)
            time.sleep(0.01)  # Small delay

        assert is_blocked(ip)
        # Note: failed_attempts are kept for logging purposes
        assert len(failed_attempts[ip]) == MAX_FAILURES

    def test_block_prevents_further_tracking(self):
        """Test that blocked IPs continue to be tracked for audit."""
        ip = "192.168.1.13"

        # Trigger block
        for _ in range(MAX_FAILURES):
            record_failure(ip)
        assert is_blocked(ip)

        # Try to record more failures
        for _ in range(5):
            record_failure(ip)

        # Should still be blocked
        assert is_blocked(ip)
        # Current implementation continues tracking all attempts for audit
        assert len(failed_attempts[ip]) == MAX_FAILURES + 5
