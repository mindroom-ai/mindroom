"""Tests that egress broker secrets are never leaked through credential policy."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import CredentialsManager, save_scoped_credentials
from mindroom.egress_broker.secrets import save_secret
from mindroom.egress_broker.tokens import WorkerClaims
from mindroom.tool_system.sandbox_proxy import _read_credential_policy
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


def test_read_credential_policy_drops_egress_services(tmp_path: Path) -> None:
    """_read_credential_policy drops egress_* entries."""
    # Create a credential policy JSON with egress services
    policy_dict = {
        "*": ["egress_github", "openai"],
        "my_tool": ["egress_openai", "anthropic"],
        "other_tool": ["normal_service"],
    }
    policy_json = json.dumps(policy_dict)

    # Create runtime paths with the policy
    runtime_paths = resolve_runtime_paths(
        storage_path=tmp_path,
        process_env={"MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON": policy_json},
    )

    # Parse the policy
    parsed = _read_credential_policy(runtime_paths)

    # Egress services should be dropped
    assert "egress_github" not in parsed.get("*", ())
    assert "egress_openai" not in parsed.get("my_tool", ())

    # Normal services should remain
    assert "openai" in parsed.get("*", ())
    assert "anthropic" in parsed.get("my_tool", ())
    assert "normal_service" in parsed.get("other_tool", ())


def test_read_credential_policy_empty_after_filtering_egress(tmp_path: Path) -> None:
    """Selector with only egress services results in empty tuple."""
    policy_dict = {
        "egress_only": ["egress_github", "egress_openai"],
    }
    policy_json = json.dumps(policy_dict)

    runtime_paths = resolve_runtime_paths(
        storage_path=tmp_path,
        process_env={"MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON": policy_json},
    )

    parsed = _read_credential_policy(runtime_paths)

    # Selector should exist but be empty
    assert "egress_only" in parsed
    assert parsed["egress_only"] == ()
