"""Characterization tests for current credential placement policy."""

from __future__ import annotations

import pytest

from mindroom.credentials.policy import (
    _UNSUPPORTED_WORKER_GRANTABLE_CREDENTIALS,
    OAUTH_CREDENTIAL_FIELDS,
    CredentialPlacement,
    credential_service_policy,
    dashboard_may_edit_oauth_service,
    filter_oauth_credential_fields,
    is_egress_broker_service,
    is_global_credential_service,
    looks_like_oauth_credentials,
)
from mindroom.egress_broker.oauth_reuse import OAUTH_REUSE_CREDENTIAL_SERVICE


@pytest.mark.parametrize(
    ("service", "worker_scope", "placement", "unflagged_placement"),
    [
        ("github", "shared", CredentialPlacement.AGENT, CredentialPlacement.WORKER),
        ("github", "user", CredentialPlacement.REQUESTER, CredentialPlacement.WORKER),
        ("github", "user_agent", CredentialPlacement.REQUESTER, CredentialPlacement.WORKER),
        # A Google Cloud tool's own settings are primary-owned like any other tool's, not installation-wide.
        ("google_bigquery", "shared", CredentialPlacement.AGENT, CredentialPlacement.WORKER),
        ("google_bigquery", "user_agent", CredentialPlacement.REQUESTER, CredentialPlacement.WORKER),
        # Local-only and OAuth services keep their own placement.
        ("google_gmail", "shared", CredentialPlacement.SHARED, CredentialPlacement.SHARED),
        ("github_oauth", "shared", CredentialPlacement.AGENT, CredentialPlacement.AGENT),
    ],
)
def test_primary_built_tool_settings_use_primary_stores(
    service: str,
    worker_scope: str,
    placement: CredentialPlacement,
    unflagged_placement: CredentialPlacement,
) -> None:
    """Settings of a tool the primary builds stay in primary stores in every worker scope."""
    assert credential_service_policy(service, worker_scope, primary_built_tool=True).placement is placement
    assert credential_service_policy(service, worker_scope).placement is unflagged_placement


@pytest.mark.parametrize(
    ("service", "worker_scope", "expected"),
    [
        ("google_drive_oauth", "user", True),
        ("google_drive_oauth", "user_agent", True),
        ("google_drive_oauth", "shared", False),
        ("acme_oauth", "user", True),
        ("acme_oauth", "user_agent", True),
        ("acme_oauth", "shared", False),
        ("openai", "user", False),
        ("homeassistant", "user_agent", True),
    ],
)
def test_primary_runtime_scoped_service_policy(service: str, worker_scope: str, expected: bool) -> None:
    """Private worker scopes should read local-only services from primary-runtime scoped storage."""
    assert (credential_service_policy(service, worker_scope).placement is CredentialPlacement.REQUESTER) is expected


@pytest.mark.parametrize(
    ("service", "worker_scope", "expected"),
    [
        ("google_drive", "shared", True),
        ("google_drive_oauth", "shared", False),
        ("acme_oauth", "shared", False),
        ("gmail", "shared", True),
        ("openai", "shared", False),
        ("google_drive", "user", False),
    ],
)
def test_local_shared_service_policy(service: str, worker_scope: str, expected: bool) -> None:
    """Shared worker scope should read local-only service credentials from the primary runtime."""
    assert (credential_service_policy(service, worker_scope).placement is CredentialPlacement.SHARED) is expected


@pytest.mark.parametrize(
    ("service", "worker_scope", "expected"),
    [
        ("google_drive_oauth", "shared", True),
        ("acme_oauth", "shared", True),
        ("mcp_demo_oauth", "shared", True),
        ("google_drive_oauth", "user", False),
        ("google_drive_oauth", "user_agent", False),
        ("google_drive_oauth", None, False),
        ("acme_oauth", "user", False),
        ("acme_oauth", None, False),
        ("mcp_demo_oauth", "user_agent", False),
        ("mcp_demo_oauth", None, False),
        ("google_drive", "shared", False),
        ("google_drive_oauth_client", "shared", False),
        ("openai", "shared", False),
    ],
)
def test_agent_scoped_service_policy(service: str, worker_scope: str | None, expected: bool) -> None:
    """Shared worker scope should keep OAuth tokens in per-agent primary-runtime storage."""
    assert (credential_service_policy(service, worker_scope).placement is CredentialPlacement.AGENT) is expected


@pytest.mark.parametrize(
    ("scope", "placement"),
    [
        ("shared", CredentialPlacement.AGENT),
        ("user", CredentialPlacement.REQUESTER),
        ("user_agent", CredentialPlacement.REQUESTER),
    ],
)
def test_egress_services_are_primary_only_and_not_grantable(scope: str, placement: CredentialPlacement) -> None:
    """Egress broker services should be primary-only and not worker-grantable in all scopes."""
    policy = credential_service_policy("egress_github", scope, primary_built_tool=False)
    assert not policy.leasable
    assert not policy.grantable
    assert policy.placement is placement


@pytest.mark.parametrize("scope", ["shared", "user", "user_agent", None])
def test_agent_keys_account_opt_ins_stay_primary_only(scope: str | None) -> None:
    """Worker code must never read or grant itself an account's opt-in, so its document is an egress document.

    The generic credentials API rejects egress documents and worker leases skip them.
    """
    policy = credential_service_policy(OAUTH_REUSE_CREDENTIAL_SERVICE, scope)
    assert is_egress_broker_service(OAUTH_REUSE_CREDENTIAL_SERVICE)
    assert not policy.leasable
    assert not policy.grantable


_EGRESS_PLACEMENT_BY_SCOPE = {
    None: CredentialPlacement.GLOBAL,
    "shared": CredentialPlacement.AGENT,
    "user": CredentialPlacement.REQUESTER,
    "user_agent": CredentialPlacement.REQUESTER,
}


@pytest.mark.parametrize("service", ["egress_x_oauth", "egress_x_oauth_client"])
@pytest.mark.parametrize("scope", ["shared", "user", "user_agent", None])
def test_egress_services_with_oauth_suffixes_stay_primary_only(service: str, scope: str | None) -> None:
    """An OAuth-looking suffix never turns an egress service into an OAuth token or client service."""
    policy = credential_service_policy(service, scope)
    assert not policy.leasable
    assert not policy.grantable
    assert not is_global_credential_service(service)
    assert policy.placement is _EGRESS_PLACEMENT_BY_SCOPE[scope]


@pytest.mark.parametrize(
    "service",
    [
        "google_calendar_oauth",
        "google_docs_oauth",
        "google_drive_oauth",
        "google_gmail_oauth",
        "google_sheets_oauth",
        "google_tasks_oauth",
    ],
)
def test_worker_grantable_policy_rejects_google_oauth_token_services(service: str) -> None:
    """Google OAuth token services should stay unsupported for worker mirroring."""
    assert credential_service_policy(service, "shared").grantable is False


def test_worker_grantable_policy_rejects_sensitive_google_services() -> None:
    """Machine-local identity services stay unsupported for worker mirroring."""
    assert frozenset({"desktop", "google_vertex_adc", "slack_app"}) == _UNSUPPORTED_WORKER_GRANTABLE_CREDENTIALS
    assert credential_service_policy("google_vertex_adc", "shared").grantable is False
    assert credential_service_policy("slack_app", None).grantable is False


def test_credential_service_policy_classifies_google_oauth_user_scope() -> None:
    """Google OAuth token services should use private storage and reject worker grants."""
    policy = credential_service_policy("google_drive_oauth", "user")

    assert policy.placement is CredentialPlacement.REQUESTER
    assert policy.grantable is False


def test_credential_service_policy_classifies_plugin_oauth_token_service() -> None:
    """Plugin OAuth token services should use primary-runtime storage and reject worker grants."""
    shared_policy = credential_service_policy("acme_oauth", "shared")
    user_agent_policy = credential_service_policy("acme_oauth", "user_agent")

    assert shared_policy.placement is CredentialPlacement.AGENT
    assert shared_policy.grantable is False
    assert user_agent_policy.placement is CredentialPlacement.REQUESTER
    assert user_agent_policy.grantable is False


def test_credential_service_policy_classifies_oauth_client_config_as_global_primary_runtime() -> None:
    """OAuth app client config should stay in one primary-runtime deployment store."""
    policy = credential_service_policy("google_drive_oauth_client", "user_agent")

    assert policy.placement is CredentialPlacement.GLOBAL
    assert policy.grantable is False


def test_credential_service_policy_classifies_plugin_oauth_client_config_as_global_primary_runtime() -> None:
    """Plugin OAuth app client config should use the same global placement policy."""
    policy = credential_service_policy("acme_oauth_client", "shared")

    assert policy.placement is CredentialPlacement.GLOBAL
    assert policy.grantable is False


def test_credential_service_policy_classifies_regular_shared_service() -> None:
    """Regular services should remain worker-grantable and avoid local-only storage policy."""
    policy = credential_service_policy("openai", "shared")

    assert policy.placement is CredentialPlacement.WORKER
    assert policy.grantable is True


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
