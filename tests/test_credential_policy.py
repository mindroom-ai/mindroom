"""Characterization tests for current credential placement policy."""

from __future__ import annotations

import pytest

from mindroom.constants import UNSUPPORTED_WORKER_GRANTABLE_CREDENTIALS
from mindroom.tool_system.worker_routing import (
    service_uses_local_shared_credentials,
    service_uses_primary_runtime_scoped_credentials,
)


@pytest.mark.parametrize(
    ("service", "worker_scope", "expected"),
    [
        ("google_drive_oauth", "user", True),
        ("google_drive_oauth", "user_agent", True),
        ("google_drive_oauth", "shared", False),
        ("openai", "user", False),
        ("homeassistant", "user_agent", True),
    ],
)
def test_primary_runtime_scoped_service_policy(service: str, worker_scope: str, expected: bool) -> None:
    """Private worker scopes should read local-only services from primary-runtime scoped storage."""
    assert service_uses_primary_runtime_scoped_credentials(service, worker_scope) is expected


@pytest.mark.parametrize(
    ("service", "worker_scope", "expected"),
    [
        ("google_drive", "shared", True),
        ("google_drive_oauth", "shared", True),
        ("gmail", "shared", True),
        ("openai", "shared", False),
        ("google_drive", "user", False),
    ],
)
def test_local_shared_service_policy(service: str, worker_scope: str, expected: bool) -> None:
    """Shared worker scope should read local-only service credentials from the primary runtime."""
    assert service_uses_local_shared_credentials(service, worker_scope) is expected


@pytest.mark.parametrize(
    "service",
    [
        "google_oauth_client",
        "google_calendar_oauth",
        "google_drive_oauth",
        "google_gmail_oauth",
        "google_sheets_oauth",
        "google_vertex_adc",
    ],
)
def test_worker_grantable_policy_rejects_sensitive_google_services(service: str) -> None:
    """Sensitive Google credential services should stay unsupported for worker mirroring."""
    assert service in UNSUPPORTED_WORKER_GRANTABLE_CREDENTIALS
