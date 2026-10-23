"""Tests for conversation-state persistence scope selection."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import MATRIX_RESPONSE_EVENT_ID_METADATA_KEY
from mindroom.conversation_state_writer import ConversationStateWriter, ConversationStateWriterDeps
from mindroom.history.session_context import create_scope_session_storage, open_bound_scope_session_context
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, runtime_paths_for, seed_session, test_runtime_paths
from tests.identity_helpers import entity_ids, persist_entity_accounts

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_persist_response_event_id_keeps_assistant_history_plain(tmp_path: Path) -> None:
    """Response linkage belongs in run metadata, not assistant-role content."""
    run = RunRecord(
        run_id="run-1",
        agent_id="test_agent",
        session_id="session-1",
        content="Final answer",
        messages=[
            Message(role="user", content="Question"),
            Message(role="assistant", content="Final answer"),
        ],
    )
    storage = create_state_storage("test_agent", tmp_path)
    seed_session(
        storage,
        ConversationSession(
            kind="agent",
            session_id="session-1",
            owner_id="test_agent",
            runs=[run],
            created_at=1,
            updated_at=1,
        ),
    )
    writer = ConversationStateWriter(
        ConversationStateWriterDeps(
            runtime=MagicMock(),
            logger=MagicMock(),
            runtime_paths=test_runtime_paths(tmp_path),
            agent_name="test_agent",
        ),
    )

    await writer.apersist_response_event_id_in_session_run(
        create_storage=lambda: create_state_storage("test_agent", tmp_path),
        session_id="session-1",
        session_type="agent",
        run_id="run-1",
        response_event_id="$answer",
    )

    persisted = storage.load_session("session-1", "agent")
    assert persisted is not None
    assert persisted.runs is not None
    persisted_run = persisted.runs[0]
    assert persisted_run.metadata == {MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: "$answer"}
    assert persisted_run.messages is not None
    assert persisted_run.messages[-1].content == "Final answer"


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


def test_private_ad_hoc_team_history_scope_matches_bound_team_storage(tmp_path: Path) -> None:
    """Bookkeeping storage and the team run must use the same private ad hoc scope."""
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
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="shared",
        requester_id="@alice:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    writer_scope = writer.team_history_scope(
        [ids["private_worker"], ids["shared"]],
        requester_user_id="@alice:localhost",
    )
    writer_storage = create_scope_session_storage(
        agent_name="shared",
        scope=writer_scope,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    with open_bound_scope_session_context(
        member_names=["private_worker", "shared"],
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
    ) as bound_scope_context:
        assert bound_scope_context is not None
        assert bound_scope_context.scope == writer_scope
        assert bound_scope_context.storage.path == writer_storage.path
