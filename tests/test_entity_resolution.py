"""Tests for runtime-derived entity resolution helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, resolve_runtime_paths
from mindroom.entity_resolution import (
    DuplicateManagedEntityIdentityError,
    MissingManagedEntityAccountError,
    configured_bot_user_ids_for_room,
    entity_identity_registry,
)
from mindroom.matrix.state import MatrixState
from tests.conftest import bind_runtime_paths, runtime_paths_for

if TYPE_CHECKING:
    from pathlib import Path


def test_configured_bot_user_ids_for_room_includes_agents_teams_and_router(tmp_path: Path) -> None:
    """Room membership resolution returns agent, team, and router bot user IDs."""
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General", role="General agent", rooms=["!room:server"]),
                "other": AgentConfig(display_name="Other", role="Other agent", rooms=["!other:server"]),
            },
            teams={
                "team": TeamConfig(
                    display_name="Team",
                    role="Team role",
                    agents=["general"],
                    rooms=["!room:server"],
                ),
            },
        ),
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "mindroom_data",
            process_env={},
        ),
    )
    runtime_paths = runtime_paths_for(config)
    state = MatrixState()
    state.add_account(f"agent_{ROUTER_AGENT_NAME}", "mindroom_router", "pw", domain="localhost")
    state.add_account("agent_general", "mindroom_general", "pw", domain="localhost")
    state.add_account("agent_other", "mindroom_other", "pw", domain="localhost")
    state.add_account("agent_team", "mindroom_team", "pw", domain="localhost")
    state.save(runtime_paths=runtime_paths)

    user_ids = configured_bot_user_ids_for_room(config, "!room:server", runtime_paths)

    assert user_ids == {
        f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
        "@mindroom_general:localhost",
        "@mindroom_team:localhost",
    }


def test_configured_bot_user_ids_for_room_uses_persisted_current_user_ids(tmp_path: Path) -> None:
    """Room membership user IDs should resolve through live persisted account user IDs."""
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General", role="General agent", rooms=["!room:server"]),
            },
            teams={
                "team": TeamConfig(
                    display_name="Team",
                    role="Team role",
                    agents=["general"],
                    rooms=["!room:server"],
                ),
            },
        ),
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "mindroom_data",
            process_env={},
        ),
    )
    runtime_paths = runtime_paths_for(config)
    state = MatrixState()
    state.add_account(f"agent_{ROUTER_AGENT_NAME}", "actual_router", "pw", domain="matrix.example")
    state.add_account("agent_general", "actual_general", "pw", domain="matrix.example")
    state.add_account("agent_team", "actual_team", "pw", domain="matrix.example")
    state.save(runtime_paths=runtime_paths)

    user_ids = configured_bot_user_ids_for_room(config, "!room:server", runtime_paths)

    assert user_ids == {
        "@actual_router:matrix.example",
        "@actual_general:matrix.example",
        "@actual_team:matrix.example",
    }


def test_entity_identity_registry_uses_only_persisted_current_ids(tmp_path: Path) -> None:
    """Runtime identity should be exact persisted entity aliases to Matrix IDs."""
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General", role="General agent"),
            },
        ),
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "mindroom_data",
            process_env={},
        ),
    )
    runtime_paths = runtime_paths_for(config)
    state = MatrixState()
    state.add_account(f"agent_{ROUTER_AGENT_NAME}", "mindroom_router_oldns", "pw", domain="localhost")
    state.add_account("agent_general", "mindroom_general_oldns", "pw", domain="localhost")
    state.save(runtime_paths=runtime_paths)

    identity = entity_identity_registry(config, runtime_paths)

    assert identity.current_ids["general"].full_id == "@mindroom_general_oldns:localhost"
    assert identity.current_entity_name_for_user_id("@mindroom_general_oldns:localhost") == "general"
    assert identity.current_entity_name_for_user_id("@mindroom_general:localhost") is None


def test_entity_identity_registry_requires_prepared_entity_accounts(tmp_path: Path) -> None:
    """Runtime identity lookup fails at the account-preparation boundary."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    config = Config.validate_with_runtime(
        Config(agents={"general": AgentConfig(display_name="General", role="General agent")}).authored_model_dump(),
        runtime_paths,
    )

    with pytest.raises(MissingManagedEntityAccountError, match="router"):
        entity_identity_registry(config, runtime_paths)


def test_entity_identity_registry_rejects_duplicate_persisted_entity_ids(tmp_path: Path) -> None:
    """Persisted Matrix IDs must map to one configured entity alias."""
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General", role="General agent"),
                "writer": AgentConfig(display_name="Writer", role="Writer agent"),
            },
        ),
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "mindroom_data",
            process_env={},
        ),
    )
    runtime_paths = runtime_paths_for(config)
    state = MatrixState()
    state.add_account(f"agent_{ROUTER_AGENT_NAME}", "mindroom_router", "pw", domain="localhost")
    state.add_account("agent_general", "shared_bot", "pw", domain="localhost")
    state.add_account("agent_writer", "shared_bot", "pw", domain="localhost")
    state.save(runtime_paths=runtime_paths)

    with pytest.raises(DuplicateManagedEntityIdentityError, match="shared_bot"):
        entity_identity_registry(config, runtime_paths)
