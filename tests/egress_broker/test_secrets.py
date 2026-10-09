"""Tests for egress broker secret storage."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.credential_policy import credential_service_policy
from mindroom.credentials import CredentialsManager
from mindroom.egress_broker.secrets import (
    delete_secret,
    egress_credential_service,
    load_secret,
    save_secret,
    secret_status,
)
from mindroom.egress_broker.tokens import WorkerClaims

if TYPE_CHECKING:
    from pathlib import Path


def _manager(tmp_path: Path) -> CredentialsManager:
    """Create a CredentialsManager with proper directory structure."""
    # Create a credentials subdirectory so storage_root is tmp_path, not tmp_path.parent
    creds_path = tmp_path / "credentials"
    return CredentialsManager(creds_path)


def _claims(
    *,
    worker_scope: str | None,
    requester_id: str | None = "alice",
    agent_name: str = "test_agent",
    tenant_id: str | None = None,
) -> WorkerClaims:
    """Build test worker claims."""
    worker_key = f"v1:{tenant_id or 'local'}:{worker_scope or 'unscoped'}:test"
    return WorkerClaims(
        worker_key=worker_key,
        worker_scope=worker_scope,  # type: ignore[arg-type]
        routing_agent_name=agent_name,
        tenant_id=tenant_id,
        account_id=None,
        channel="matrix",
        agent_name=agent_name,
        requester_id=requester_id,
    )


def test_user_agent_secret_lands_in_primary_runtime_scope(tmp_path: Path) -> None:
    """user_agent scope secrets must go to private_oauth/<requester>/<agent>, not workers/."""
    manager = _manager(tmp_path)
    claims = _claims(worker_scope="user_agent", requester_id="alice", agent_name="test_agent")
    target = claims.to_worker_target()

    save_secret(manager, target, "github", "secret-value")

    # Should be in private_oauth/<requester-hash>/<agent-hash>/egress_github_credentials.json
    # storage_root is tmp_path (from _manager helper)
    private_oauth = tmp_path / "private_oauth"
    assert private_oauth.exists()
    egress_files = list(private_oauth.rglob("egress_github*"))
    assert len(egress_files) == 1
    # Path structure: .../private_oauth/alice-hash/test_agent-hash/egress_github_credentials.json
    assert egress_files[0].parts[-4] == "private_oauth"

    # Should NOT be in workers/
    workers_dir = tmp_path / "workers"
    if workers_dir.exists():
        # No egress_github under workers/
        assert list(workers_dir.rglob("egress_github*")) == []


def test_shared_secret_lands_in_agent_scope(tmp_path: Path) -> None:
    """Shared scope secrets must go to for_primary_runtime_agent_scope(agent)."""
    manager = _manager(tmp_path)
    claims = _claims(worker_scope="shared", agent_name="test_agent")
    target = claims.to_worker_target()

    save_secret(manager, target, "github", "secret-value")

    # Should be in private_oauth/_agents/<agent-hash>/egress_github_credentials.json
    private_oauth = tmp_path / "private_oauth"
    assert private_oauth.exists()
    agents_dir = private_oauth / "_agents"
    assert agents_dir.exists()
    egress_files = list(agents_dir.rglob("egress_github*"))
    assert len(egress_files) == 1

    # Should NOT be in workers/
    workers_dir = tmp_path / "workers"
    if workers_dir.exists():
        assert list(workers_dir.rglob("egress_github*")) == []


def test_unscoped_secret_uses_global_store(tmp_path: Path) -> None:
    """Unscoped (None) scope secrets should go to the global store."""
    manager = _manager(tmp_path)
    claims = _claims(worker_scope=None)
    target = claims.to_worker_target()

    save_secret(manager, target, "github", "secret-value")

    # Should be in credentials/egress_github_credentials.json
    expected_path = tmp_path / "credentials" / "egress_github_credentials.json"
    assert expected_path.exists()

    # Should NOT be in private_oauth/ or workers/
    private_oauth_dir = tmp_path / "private_oauth"
    workers_dir = tmp_path / "workers"
    if private_oauth_dir.exists():
        assert list(private_oauth_dir.rglob("egress_github*")) == []
    if workers_dir.exists():
        assert list(workers_dir.rglob("egress_github*")) == []


def test_no_shared_fallback_for_user_agent(tmp_path: Path) -> None:
    """user_agent target with global secret set should return None, not fallback."""
    manager = _manager(tmp_path)

    # Set a global secret
    unscoped_claims = _claims(worker_scope=None)
    unscoped_target = unscoped_claims.to_worker_target()
    save_secret(manager, unscoped_target, "github", "global-secret")

    # Try to load with user_agent scope
    user_agent_claims = _claims(worker_scope="user_agent", requester_id="alice", agent_name="test_agent")
    user_agent_target = user_agent_claims.to_worker_target()

    # Should return None, not fallback to global
    loaded = load_secret(manager, user_agent_target, "github")
    assert loaded is None


def test_scopes_are_isolated(tmp_path: Path) -> None:
    """Requester A's secret should be invisible to requester B."""
    manager = _manager(tmp_path)

    # Alice sets a secret
    alice_claims = _claims(worker_scope="user_agent", requester_id="alice", agent_name="test_agent")
    alice_target = alice_claims.to_worker_target()
    save_secret(manager, alice_target, "github", "alice-secret")

    # Bob should not see it
    bob_claims = _claims(worker_scope="user_agent", requester_id="bob", agent_name="test_agent")
    bob_target = bob_claims.to_worker_target()
    loaded = load_secret(manager, bob_target, "github")
    assert loaded is None


def test_status_reports_updated_at_and_never_value(tmp_path: Path) -> None:
    """secret_status should report updated_at and configured, but never the secret value."""
    manager = _manager(tmp_path)
    claims = _claims(worker_scope="user_agent", requester_id="alice", agent_name="test_agent")
    target = claims.to_worker_target()

    # Not configured yet
    status = secret_status(manager, target, "github")
    assert not status.configured
    assert status.updated_at is None

    # Save a secret
    save_secret(manager, target, "github", "secret-value")

    # Now configured with updated_at
    status = secret_status(manager, target, "github")
    assert status.configured
    assert status.updated_at is not None
    # Should be ISO-8601 UTC format
    assert "T" in status.updated_at
    assert status.updated_at.endswith("Z")


def test_save_rejects_blank_and_oversized(tmp_path: Path) -> None:
    """save_secret should reject empty/whitespace and >16KiB secrets."""
    manager = _manager(tmp_path)
    claims = _claims(worker_scope="user_agent", requester_id="alice", agent_name="test_agent")
    target = claims.to_worker_target()

    # Empty string
    with pytest.raises(ValueError, match="empty"):
        save_secret(manager, target, "github", "")

    # Whitespace only
    with pytest.raises(ValueError, match="empty"):
        save_secret(manager, target, "github", "   \n\t  ")

    # Control characters
    with pytest.raises(ValueError, match="control character"):
        save_secret(manager, target, "github", "s3cret\r\nX: y")

    with pytest.raises(ValueError, match="control character"):
        save_secret(manager, target, "github", "a\x00b")

    with pytest.raises(ValueError, match="control character"):
        save_secret(manager, target, "github", "test\x7f")

    # Oversized (16 KiB = 16384 bytes)
    oversized = "x" * 16385
    with pytest.raises(ValueError, match="16 KiB"):
        save_secret(manager, target, "github", oversized)

    # Exactly 16 KiB should work
    exactly_16k = "x" * 16384
    save_secret(manager, target, "github", exactly_16k)
    assert load_secret(manager, target, "github") == exactly_16k


def test_worker_grantable_copy_skips_egress() -> None:
    """defaults.worker_grantable_credentials with egress service should not copy into worker store."""
    # This will be tested via credential_policy integration
    # The policy should mark egress services as worker_grantable_supported=False
    policy = credential_service_policy("egress_github", "user_agent", primary_built_tool=False)
    assert not policy.worker_grantable_supported


def test_egress_credential_service() -> None:
    """egress_credential_service should prefix with 'egress_'."""
    assert egress_credential_service("github") == "egress_github"
    assert egress_credential_service("openai") == "egress_openai"


def test_none_target_uses_global_store(tmp_path: Path) -> None:
    """Passing None as target should use the global (unscoped) store."""
    manager = _manager(tmp_path)

    # Save with None target
    save_secret(manager, None, "github", "global-secret")

    # Should be in credentials/egress_github_credentials.json
    expected_path = tmp_path / "credentials" / "egress_github_credentials.json"
    assert expected_path.exists()

    # Load with None target
    loaded = load_secret(manager, None, "github")
    assert loaded == "global-secret"

    # Check status with None target
    status = secret_status(manager, None, "github")
    assert status.configured
    assert status.updated_at is not None

    # Delete with None target
    delete_secret(manager, None, "github")

    # Should no longer be configured
    status = secret_status(manager, None, "github")
    assert not status.configured
    assert status.updated_at is None

    # Should NOT be in private_oauth/ or workers/
    private_oauth_dir = tmp_path / "private_oauth"
    workers_dir = tmp_path / "workers"
    if private_oauth_dir.exists():
        assert list(private_oauth_dir.rglob("egress_github*")) == []
    if workers_dir.exists():
        assert list(workers_dir.rglob("egress_github*")) == []
