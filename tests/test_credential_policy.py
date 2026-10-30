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
    ("service", "worker_scope", "primary_built_tool", "placement", "grantable"),
    [
        # Regular services live in the worker store, or in the primary's stores for a tool the primary builds.
        ("openai", "shared", False, CredentialPlacement.WORKER, True),
        ("openai", "user", False, CredentialPlacement.WORKER, True),
        ("github", "shared", False, CredentialPlacement.WORKER, True),
        ("github", "shared", True, CredentialPlacement.AGENT, True),
        ("github", "user", False, CredentialPlacement.WORKER, True),
        ("github", "user", True, CredentialPlacement.REQUESTER, True),
        ("github", "user_agent", False, CredentialPlacement.WORKER, True),
        ("github", "user_agent", True, CredentialPlacement.REQUESTER, True),
        # A Google Cloud tool's own settings are primary-owned like any other tool's, not installation-wide.
        ("google_bigquery", "shared", False, CredentialPlacement.WORKER, True),
        ("google_bigquery", "shared", True, CredentialPlacement.AGENT, True),
        ("google_bigquery", "user_agent", False, CredentialPlacement.WORKER, True),
        ("google_bigquery", "user_agent", True, CredentialPlacement.REQUESTER, True),
        # Local-only services stay in the primary: shared for a shared scope, the requester's store otherwise.
        ("gmail", "shared", False, CredentialPlacement.SHARED, True),
        ("google_gmail", "shared", False, CredentialPlacement.SHARED, True),
        ("google_gmail", "shared", True, CredentialPlacement.SHARED, True),
        ("google_drive", "shared", False, CredentialPlacement.SHARED, True),
        ("google_drive", "user", False, CredentialPlacement.REQUESTER, True),
        ("homeassistant", "user_agent", False, CredentialPlacement.REQUESTER, True),
        # OAuth tokens, built-in, plugin, and MCP alike, bind to the agent in a shared scope and are never granted.
        ("google_drive_oauth", None, False, CredentialPlacement.GLOBAL, False),
        ("google_drive_oauth", "shared", False, CredentialPlacement.AGENT, False),
        ("google_drive_oauth", "user", False, CredentialPlacement.REQUESTER, False),
        ("google_drive_oauth", "user_agent", False, CredentialPlacement.REQUESTER, False),
        ("github_oauth", "shared", False, CredentialPlacement.AGENT, False),
        ("github_oauth", "shared", True, CredentialPlacement.AGENT, False),
        ("acme_oauth", None, False, CredentialPlacement.GLOBAL, False),
        ("acme_oauth", "shared", False, CredentialPlacement.AGENT, False),
        ("acme_oauth", "user", False, CredentialPlacement.REQUESTER, False),
        ("acme_oauth", "user_agent", False, CredentialPlacement.REQUESTER, False),
        ("mcp_demo_oauth", None, False, CredentialPlacement.GLOBAL, False),
        ("mcp_demo_oauth", "shared", False, CredentialPlacement.AGENT, False),
        ("mcp_demo_oauth", "user_agent", False, CredentialPlacement.REQUESTER, False),
        # OAuth app client configs, built-in and plugin alike, live in one global primary store.
        ("google_drive_oauth_client", "shared", False, CredentialPlacement.GLOBAL, False),
        ("google_drive_oauth_client", "user_agent", False, CredentialPlacement.GLOBAL, False),
        ("acme_oauth_client", "shared", False, CredentialPlacement.GLOBAL, False),
    ],
)
def test_credential_service_placement(
    service: str,
    worker_scope: str | None,
    primary_built_tool: bool,
    placement: CredentialPlacement,
    grantable: bool,
) -> None:
    """Each service's name, worker scope, and primary-built flag decide its store and whether a worker may be granted it."""
    policy = credential_service_policy(service, worker_scope, primary_built_tool=primary_built_tool)

    assert (policy.placement, policy.grantable) == (placement, grantable)


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
