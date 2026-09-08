"""Personal agent scope shared by browser connections and MCP clients."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from fastapi import HTTPException

from mindroom import constants
from mindroom.api.config_lifecycle import ApiSnapshot
from mindroom.api.personal_agent import resolve_personal_agent
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


@pytest.mark.parametrize("scope", ["user", "user_agent"])
def test_mcp_and_portal_share_personal_worker_ownership(personal_snapshot: ApiSnapshot, scope: str) -> None:
    """Changing transport must never change the owner of a person's connections."""
    assert personal_snapshot.runtime_config is not None
    agent = personal_snapshot.runtime_config.agents["personal"]
    assert agent.private is not None
    agent.private = type(agent.private).model_validate({"per": scope})
    gateway = resolve_personal_agent(personal_snapshot, "@alice:example.org")
    browser = resolve_personal_agent(personal_snapshot, "@alice:example.org", channel="matrix")
    other = resolve_personal_agent(personal_snapshot, "@bob:example.org")
    assert gateway.execution_identity.channel == "mcp"
    assert gateway.worker_target.worker_key == browser.worker_target.worker_key
    assert gateway.worker_target.worker_key != other.worker_target.worker_key
    assert gateway.worker_target.worker_scope == scope
    assert gateway.requester_id == "@alice:example.org"
    assert gateway.agent_name == "personal"


@pytest.mark.parametrize("requester", ["", "alice", "@outsider:example.org"])
def test_personal_scope_rejects_invalid_or_ungranted_requester(personal_snapshot: ApiSnapshot, requester: str) -> None:
    """Authenticated transport cannot grant access by choosing an arbitrary owner."""
    with pytest.raises(HTTPException) as error:
        resolve_personal_agent(personal_snapshot, requester)
    assert error.value.status_code == 403


def test_personal_scope_rechecks_current_agent_grant(personal_snapshot: ApiSnapshot) -> None:
    """A token for a different selected agent must not follow operator reconfiguration."""
    with pytest.raises(HTTPException) as error:
        resolve_personal_agent(personal_snapshot, "@alice:example.org", expected_agent_name="old_agent")
    assert error.value.status_code == 403


def test_personal_scope_canonicalizes_alias_before_access_and_credentials(personal_snapshot: ApiSnapshot) -> None:
    """A bridge alias resolves to the same credential owner as the portal."""
    assert personal_snapshot.runtime_config is not None
    personal_snapshot.runtime_config.authorization.aliases = {"@alice:example.org": ["@bridge:example.org"]}
    alias = resolve_personal_agent(personal_snapshot, "@bridge:example.org")
    owner = resolve_personal_agent(personal_snapshot, "@alice:example.org")
    assert alias.requester_id == "@alice:example.org"
    assert alias.worker_target == owner.worker_target


def test_personal_scope_accepts_explicit_glob_and_administrator(personal_snapshot: ApiSnapshot) -> None:
    """The shared resolver preserves existing explicit grant semantics."""
    assert personal_snapshot.runtime_config is not None
    personal_snapshot.runtime_config.agents["personal"].access.users = ["@partner_*:example.org"]
    assert (
        resolve_personal_agent(personal_snapshot, "@partner_one:example.org").requester_id == "@partner_one:example.org"
    )
    assert resolve_personal_agent(personal_snapshot, "@admin:example.org").requester_id == "@admin:example.org"
    with pytest.raises(HTTPException):
        resolve_personal_agent(personal_snapshot, "@alice:example.org")


def test_personal_scope_rejects_nonprivate_agent(personal_snapshot: ApiSnapshot) -> None:
    """Shared execution must not inherit a person's private client grant."""
    assert personal_snapshot.runtime_config is not None
    personal_snapshot.runtime_config.agents["personal"].private = None
    with pytest.raises(HTTPException) as error:
        resolve_personal_agent(personal_snapshot, "@alice:example.org")
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
        resolve_personal_agent(personal_snapshot, "@alice:example.org")
    assert error.value.status_code == status
