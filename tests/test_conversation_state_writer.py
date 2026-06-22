"""Tests for conversation-state persistence scope selection."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.conversation_state_writer import ConversationStateWriter, ConversationStateWriterDeps
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.identity_helpers import entity_ids, persist_entity_accounts

if TYPE_CHECKING:
    from pathlib import Path


def test_private_ad_hoc_team_history_scope_is_requester_partitioned(tmp_path: Path) -> None:
    """Private ad hoc team replay must not share one team scope across requesters."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "shared": AgentConfig(display_name="Shared"),
                "private_worker": AgentConfig(
                    display_name="PrivateWorker",
                    private=AgentPrivateConfig(per="user"),
                ),
            },
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        ),
        runtime_paths,
    )
    persist_entity_accounts(config, runtime_paths_for(config))
    runtime_paths = runtime_paths_for(config)
    ids = entity_ids(config, runtime_paths)
    writer = ConversationStateWriter(
        ConversationStateWriterDeps(
            runtime=SimpleNamespace(config=config),
            logger=MagicMock(),
            runtime_paths=runtime_paths,
            agent_name="shared",
        ),
    )

    alice_scope = writer.team_history_scope(
        [ids["private_worker"], ids["shared"]],
        requester_user_id="@alice:localhost",
    )
    bob_scope = writer.team_history_scope(
        [ids["private_worker"], ids["shared"]],
        requester_user_id="@bob:localhost",
    )

    assert alice_scope.kind == "team"
    assert bob_scope.kind == "team"
    assert alice_scope.scope_id.startswith("team_private_worker+shared_requester_")
    assert bob_scope.scope_id.startswith("team_private_worker+shared_requester_")
    assert alice_scope != bob_scope
