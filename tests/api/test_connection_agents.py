"""Connections eligibility and execution scopes for private and shared agents."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from fastapi import HTTPException

from mindroom import constants
from mindroom.api import connection_agents
from mindroom.api.config_lifecycle import ApiSnapshot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def personal_snapshot(tmp_path: Path) -> ApiSnapshot:
    """Build two explicit personal users without a live chat or owner fallback."""
    paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_CONNECTIONS_AGENT": "personal", "MATRIX_HOMESERVER": "https://example.org"},
    )
    config = Config.model_validate(
        {
            "administrators": ["@admin:example.org"],
            "models": {"default": {"provider": "ollama", "id": "test-model"}},
            "agents": {
                "personal": {
                    "display_name": "Personal assistant",
                    "role": "Personal assistant",
                    "tools": ["calculator"],
                    "private": {"per": "user_agent"},
                    "access": {"users": ["@alice:example.org", "@bob:example.org"]},
                },
            },
        },
    )
    return ApiSnapshot(generation=1, runtime_paths=paths, config_data=config.model_dump(), runtime_config=config)


@pytest.mark.parametrize("scope", [None, "shared", "user", "user_agent"])
def test_shared_tool_target_uses_authored_scope(personal_snapshot: ApiSnapshot, scope: str | None) -> None:
    """Credential managers may use a shared target without access to the personal agent."""
    config = personal_snapshot.runtime_config
    assert config is not None
    config.agents["shared"] = AgentConfig.model_validate(
        {
            "display_name": "Shared tools",
            "role": "Shared tools",
            "tools": ["calculator"],
            "worker_scope": scope,
            "credential_managers": ["@manager:example.org"],
        },
    )
    user = connection_agents.resolve_connection_user(personal_snapshot, "@manager:example.org")
    assert user.agent_names == ("shared",)
    assert user.personal_agent_name is None
    target = connection_agents.resolve_connection_agent(user, "shared")
    assert target.agent_name == "shared"
    assert target.requester_id == "@manager:example.org"
    assert target.execution_identity.channel == "mcp"
    assert target.worker_target.worker_scope == scope
    assert target.worker_target.private_agent_names in (None, frozenset())
    with pytest.raises(HTTPException) as denied:
        connection_agents.resolve_connection_agent(user, "personal")
    assert denied.value.status_code == 404


def test_connection_user_has_no_implicit_agent_authority(personal_snapshot: ApiSnapshot) -> None:
    """Signed users can retain account controls without being granted any tool target."""
    user = connection_agents.resolve_connection_user(personal_snapshot, "@stranger:example.org")
    assert user.agent_names == ()
    assert user.personal_agent_name is None
    with pytest.raises(HTTPException) as denied:
        connection_agents.resolve_connection_agent(user, "personal")
    assert denied.value.status_code == 404


@pytest.mark.parametrize("scope", ["user", "user_agent"])
def test_personal_worker_ownership_is_user_scoped(personal_snapshot: ApiSnapshot, scope: str) -> None:
    """Eligible users receive distinct credential owners under the authored private scope."""
    assert personal_snapshot.runtime_config is not None
    agent = personal_snapshot.runtime_config.agents["personal"]
    assert agent.private is not None
    agent.private = type(agent.private).model_validate({"per": scope})
    alice = connection_agents.resolve_connection_user(personal_snapshot, "@alice:example.org")
    bob = connection_agents.resolve_connection_user(personal_snapshot, "@bob:example.org")
    gateway = connection_agents.resolve_connection_agent(alice, "personal")
    other = connection_agents.resolve_connection_agent(bob, "personal")
    assert gateway.execution_identity.channel == "mcp"
    assert gateway.worker_target.worker_key != other.worker_target.worker_key
    assert gateway.worker_target.worker_scope == scope
    assert gateway.requester_id == "@alice:example.org"
    assert gateway.agent_name == "personal"


@pytest.mark.parametrize("requester", ["", "alice"])
def test_connection_user_rejects_invalid_requester(personal_snapshot: ApiSnapshot, requester: str) -> None:
    """Authenticated transport cannot grant access by choosing an arbitrary owner."""
    with pytest.raises(HTTPException) as error:
        connection_agents.resolve_connection_user(personal_snapshot, requester)
    assert error.value.status_code == 403


def test_personal_scope_canonicalizes_alias_before_access_and_credentials(personal_snapshot: ApiSnapshot) -> None:
    """A bridge alias resolves to the same credential owner as the portal."""
    assert personal_snapshot.runtime_config is not None
    personal_snapshot.runtime_config.authorization.aliases = {"@alice:example.org": ["@bridge:example.org"]}
    alias = connection_agents.resolve_connection_user(personal_snapshot, "@bridge:example.org")
    owner = connection_agents.resolve_connection_user(personal_snapshot, "@alice:example.org")
    assert alias.owner.requester_id == "@alice:example.org"
    assert (
        connection_agents.resolve_connection_agent(alias, "personal").worker_target
        == connection_agents.resolve_connection_agent(owner, "personal").worker_target
    )


def test_personal_scope_accepts_explicit_glob_and_administrator(personal_snapshot: ApiSnapshot) -> None:
    """The shared resolver preserves existing explicit grant semantics."""
    assert personal_snapshot.runtime_config is not None
    personal_snapshot.runtime_config.agents["personal"].access.users = ["@partner_*:example.org"]
    for requester in ("@partner_one:example.org", "@admin:example.org"):
        user = connection_agents.resolve_connection_user(personal_snapshot, requester)
        assert user.owner.requester_id == requester
        assert user.personal_agent_name == "personal"
    assert connection_agents.resolve_connection_user(personal_snapshot, "@alice:example.org").agent_names == ()


def test_personal_scope_rejects_nonprivate_agent(personal_snapshot: ApiSnapshot) -> None:
    """Shared execution must not inherit a person's private client grant."""
    assert personal_snapshot.runtime_config is not None
    personal_snapshot.runtime_config.agents["personal"].private = None
    with pytest.raises(HTTPException) as error:
        connection_agents.resolve_connection_user(personal_snapshot, "@alice:example.org")
    assert error.value.status_code == 403


@pytest.mark.parametrize(("mode", "status"), [("disabled", 404), ("missing_config", 503)])
def test_personal_scope_fails_closed_without_configuration(
    personal_snapshot: ApiSnapshot,
    mode: str,
    status: int,
) -> None:
    """Missing selection or configuration cannot fall back to another agent."""
    if mode == "disabled":
        personal_snapshot = replace(
            personal_snapshot,
            runtime_paths=replace(personal_snapshot.runtime_paths, process_env={}),
        )
    else:
        personal_snapshot = replace(personal_snapshot, runtime_config=None)
    with pytest.raises(HTTPException) as error:
        connection_agents.resolve_connection_user(personal_snapshot, "@alice:example.org")
    assert error.value.status_code == status
