"""Characterization tests for current credential placement policy."""

from __future__ import annotations

import pytest

from mindroom.credential_policy import (
    OAUTH_CREDENTIAL_FIELDS,
    UNSUPPORTED_WORKER_GRANTABLE_CREDENTIALS,
    credential_service_policy,
    dashboard_may_edit_oauth_service,
    filter_oauth_credential_fields,
    looks_like_oauth_credentials,
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
    assert credential_service_policy(service, worker_scope).uses_primary_runtime_scoped_credentials is expected


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
    assert credential_service_policy(service, worker_scope).uses_local_shared_credentials is expected


@pytest.mark.parametrize(
    "service",
    [
        "google_oauth_client",
        "google_calendar_oauth_client",
        "google_calendar_oauth",
        "google_drive_oauth_client",
        "google_drive_oauth",
        "google_gmail_oauth_client",
        "google_gmail_oauth",
        "google_sheets_oauth_client",
        "google_sheets_oauth",
        "google_vertex_adc",
    ],
)
def test_worker_grantable_policy_rejects_sensitive_google_services(service: str) -> None:
    """Sensitive Google credential services should stay unsupported for worker mirroring."""
    assert service in UNSUPPORTED_WORKER_GRANTABLE_CREDENTIALS


def test_credential_service_policy_classifies_google_oauth_user_scope() -> None:
    """Google OAuth token services should use private storage and reject worker grants."""
    policy = credential_service_policy("google_drive_oauth", "user")

    assert policy.service == "google_drive_oauth"
    assert policy.worker_scope == "user"
    assert policy.is_local_only_shared_service is True
    assert policy.uses_primary_runtime_scoped_credentials is True
    assert policy.uses_local_shared_credentials is False
    assert policy.worker_grantable_supported is False


def test_credential_service_policy_classifies_regular_shared_service() -> None:
    """Regular services should remain worker-grantable and avoid local-only storage policy."""
    policy = credential_service_policy("openai", "shared")

    assert policy.service == "openai"
    assert policy.worker_scope == "shared"
    assert policy.is_local_only_shared_service is False
    assert policy.uses_primary_runtime_scoped_credentials is False
    assert policy.uses_local_shared_credentials is False
    assert policy.worker_grantable_supported is True


def test_looks_like_oauth_credentials_detects_oauth_documents() -> None:
    """OAuth-looking credential documents should be identified without provider registry access."""
    assert looks_like_oauth_credentials({"_source": "oauth"}) is True
    assert looks_like_oauth_credentials({"_oauth_provider": "google_drive"}) is True
    assert looks_like_oauth_credentials({"_id_token": "id-token"}) is True
    assert looks_like_oauth_credentials({"_oauth_claims": {"email": "user@example.org"}}) is True
    assert looks_like_oauth_credentials({"token": "ordinary-api-token"}) is False


def test_filter_oauth_credential_fields_removes_token_material() -> None:
    """OAuth field filtering should keep editable settings and remove token material."""
    filtered = filter_oauth_credential_fields(
        {
            "token": "access-token",
            "refresh_token": "refresh-token",
            "client_secret": "client-secret",
            "_oauth_provider": "google_drive",
            "max_read_size": 10485760,
        },
    )

    assert filtered == {"max_read_size": 10485760}


def test_oauth_credential_fields_include_current_token_material() -> None:
    """The policy module should own the current OAuth token field denylist."""
    assert {
        "access_token",
        "client_id",
        "client_secret",
        "refresh_token",
        "token",
        "token_uri",
    } <= OAUTH_CREDENTIAL_FIELDS


@pytest.mark.parametrize(
    ("token_service", "tool_config_service", "expected"),
    [
        (True, False, False),
        (False, True, True),
        (True, True, False),
        (False, False, False),
    ],
)
def test_dashboard_may_edit_oauth_service(
    token_service: bool,
    tool_config_service: bool,
    expected: bool,
) -> None:
    """Dashboard OAuth edits should be limited to tool settings services."""
    assert (
        dashboard_may_edit_oauth_service(
            token_service=token_service,
            tool_config_service=tool_config_service,
        )
        is expected
    )
