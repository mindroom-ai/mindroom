"""Tests that egress broker secrets are never leaked through credential policy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.credentials.manager import CredentialsManager, save_scoped_credentials
from mindroom.egress_broker.secrets import save_secret
from mindroom.egress_broker.tokens import WorkerClaims
from mindroom.tool_system.worker_proxy_client import WorkerProxyClientConfig, _collect_credential_overrides

if TYPE_CHECKING:
    from pathlib import Path


def _manager(tmp_path: Path) -> CredentialsManager:
    """Create a CredentialsManager with proper directory structure."""
    creds_path = tmp_path / "credentials"
    return CredentialsManager(creds_path)


def _user_agent_claims() -> WorkerClaims:
    """Build user_agent worker claims."""
    return WorkerClaims(
        worker_key="v1:local:user_agent:test",
        worker_scope="user_agent",
        routing_agent_name="test_agent",
        tenant_id=None,
        account_id=None,
        channel="matrix",
        agent_name="test_agent",
        requester_id="alice",
    )


def test_collect_credential_overrides_skips_egress_services(tmp_path: Path) -> None:
    """_collect_credential_overrides never leases egress secrets into workers."""
    manager = _manager(tmp_path)
    claims = _user_agent_claims()
    target = claims.to_worker_target()

    # Save an egress secret
    save_secret(manager, target, "github", "secret-value-should-not-leak")

    # Also save a normal credential in the same scope
    save_scoped_credentials("openai", {"api_key": "normal-key"}, credentials_manager=manager, worker_target=target)

    # Create a credential policy that includes egress services
    config = WorkerProxyClientConfig(
        proxy_url=None,
        proxy_token=None,
        proxy_timeout_seconds=120.0,
        credential_lease_ttl_seconds=60,
        credential_policy={"*": ["egress_github", "openai"]},
        lease_tool_credentials=False,
    )

    # Collect overrides for a tool call (calls _collect_credential_overrides internally)
    overrides = _collect_credential_overrides(
        "test_tool",
        "test_function",
        config=config,
        credentials_manager=manager,
        worker_target=target,
        primary_built_service=None,
        worker_grantable_credentials=frozenset(),
    )

    # egress_github secret must not appear in overrides
    assert "secret" not in overrides
    assert "secret-value-should-not-leak" not in str(overrides)

    # Normal credential should be present
    assert "api_key" in overrides
    assert overrides["api_key"] == "normal-key"


def test_collect_credential_overrides_with_wildcard_policy(tmp_path: Path) -> None:
    """Credential policy with wildcard including egress services never leases secrets."""
    manager = _manager(tmp_path)
    claims = _user_agent_claims()
    target = claims.to_worker_target()

    # Save egress secret
    save_secret(manager, target, "github", "github-secret-value")

    # Save normal credentials in the same scope
    save_scoped_credentials("openai", {"api_key": "openai-key"}, credentials_manager=manager, worker_target=target)
    save_scoped_credentials(
        "anthropic",
        {"api_key": "anthropic-key"},
        credentials_manager=manager,
        worker_target=target,
    )

    # Policy includes both egress and normal services via wildcard
    config = WorkerProxyClientConfig(
        proxy_url=None,
        proxy_token=None,
        proxy_timeout_seconds=120.0,
        credential_lease_ttl_seconds=60,
        credential_policy={"*": ["egress_github", "openai", "anthropic"]},
        lease_tool_credentials=False,
    )

    overrides = _collect_credential_overrides(
        "test_tool",
        "test_function",
        config=config,
        credentials_manager=manager,
        worker_target=target,
        primary_built_service=None,
        worker_grantable_credentials=frozenset(["openai", "anthropic"]),
    )

    # Egress secret must not leak
    assert "secret" not in overrides
    assert "github-secret-value" not in str(overrides)

    # Normal credentials should be present
    assert overrides.get("api_key") in ("openai-key", "anthropic-key")
