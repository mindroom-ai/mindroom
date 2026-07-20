"""Tests for conversation-state persistence scope selection."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

from agno.agent import Agent
from agno.db.base import SessionType
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom.agent_storage import get_agent_session, get_team_session
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import MATRIX_RESPONSE_EVENT_ID_METADATA_KEY
from mindroom.conversation_state_writer import ConversationStateWriter, ConversationStateWriterDeps
from mindroom.history.runtime import create_scope_session_storage, open_bound_scope_session_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.identity_helpers import entity_ids, persist_entity_accounts

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


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
    """Bookkeeping storage and the real Agno team run must use the same private ad hoc scope."""
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
    agents = [
        Agent(id="private_worker", name="PrivateWorker"),
        Agent(id="shared", name="Shared"),
    ]

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
    try:
        with open_bound_scope_session_context(
            agents=agents,
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=execution_identity,
        ) as bound_scope_context:
            assert bound_scope_context is not None
            assert bound_scope_context.scope == writer_scope
            assert cast("Any", bound_scope_context.storage).db_file == cast("Any", writer_storage).db_file
    finally:
        writer_storage.close()


def _writer(config: Config, runtime_paths: RuntimePaths, agent_name: str = "shared") -> ConversationStateWriter:
    return ConversationStateWriter(
        ConversationStateWriterDeps(
            runtime=SimpleNamespace(config=config),
            logger=MagicMock(),
            runtime_paths=runtime_paths,
            agent_name=agent_name,
        ),
    )


def _agent_config(tmp_path: Path) -> tuple[Config, RuntimePaths]:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"shared": AgentConfig(display_name="Shared")},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        ),
        runtime_paths,
    )
    return config, runtime_paths_for(config)


def _agent_session_with_run(*, content: str | None, with_assistant_message: bool) -> AgentSession:
    messages = [Message(role="user", content="question")]
    if with_assistant_message:
        messages.append(Message(role="assistant", content=content or ""))
    return AgentSession(
        session_id="session-1",
        agent_id="shared",
        runs=[
            RunOutput(
                run_id="run-1",
                agent_id="shared",
                status=RunStatus.completed,
                content=content,
                messages=messages,
            ),
        ],
        created_at=1,
        updated_at=1,
    )


def test_persist_response_event_id_wraps_final_assistant_message(tmp_path: Path) -> None:
    """The visible response event wraps the run's final assistant message once."""
    config, runtime_paths = _agent_config(tmp_path)
    writer = _writer(config, runtime_paths)
    storage = writer.create_storage(None)
    try:
        storage.upsert_session(_agent_session_with_run(content="Final answer", with_assistant_message=True))

        writer.persist_response_event_id_in_session_run(
            storage=storage,
            session_id="session-1",
            session_type=SessionType.AGENT,
            run_id="run-1",
            response_event_id="$visible",
            response_sender_id="@mindroom_shared:localhost",
        )

        persisted = get_agent_session(storage, "session-1")
        assert persisted is not None
        run = cast("RunOutput", (persisted.runs or [])[0])
        assert run.metadata is not None
        assert run.metadata[MATRIX_RESPONSE_EVENT_ID_METADATA_KEY] == "$visible"
        assert run.content == "Final answer"
        assert [(message.role, message.content) for message in run.messages or []] == [
            ("user", "question"),
            (
                "assistant",
                '<msg event_id="$visible" from="@mindroom_shared:localhost"><![CDATA[Final answer]]></msg>',
            ),
        ]
    finally:
        storage.close()


def test_persist_response_event_id_is_idempotent_and_never_nests(tmp_path: Path) -> None:
    """Repeated or changed callbacks rebuild the wrapper from canonical run content."""
    config, runtime_paths = _agent_config(tmp_path)
    writer = _writer(config, runtime_paths)
    storage = writer.create_storage(None)
    try:
        storage.upsert_session(_agent_session_with_run(content="Final answer", with_assistant_message=True))

        for response_event_id in ("$visible", "$visible", "$edited"):
            writer.persist_response_event_id_in_session_run(
                storage=storage,
                session_id="session-1",
                session_type=SessionType.AGENT,
                run_id="run-1",
                response_event_id=response_event_id,
                response_sender_id="@mindroom_shared:localhost",
            )

        persisted = get_agent_session(storage, "session-1")
        assert persisted is not None
        run = cast("RunOutput", (persisted.runs or [])[0])
        assert run.metadata is not None
        assert run.metadata[MATRIX_RESPONSE_EVENT_ID_METADATA_KEY] == "$edited"
        final_message = (run.messages or [])[-1]
        assert final_message.content == (
            '<msg event_id="$edited" from="@mindroom_shared:localhost"><![CDATA[Final answer]]></msg>'
        )
        assert cast("str", final_message.content).count("<msg ") == 1
    finally:
        storage.close()


def test_persist_response_event_id_keeps_metadata_only_runs_bare(tmp_path: Path) -> None:
    """Runs without string content or a final assistant message persist metadata only."""
    config, runtime_paths = _agent_config(tmp_path)
    writer = _writer(config, runtime_paths)
    storage = writer.create_storage(None)
    try:
        storage.upsert_session(_agent_session_with_run(content=None, with_assistant_message=False))

        writer.persist_response_event_id_in_session_run(
            storage=storage,
            session_id="session-1",
            session_type=SessionType.AGENT,
            run_id="run-1",
            response_event_id="$visible",
            response_sender_id="@mindroom_shared:localhost",
        )

        persisted = get_agent_session(storage, "session-1")
        assert persisted is not None
        run = cast("RunOutput", (persisted.runs or [])[0])
        assert run.metadata is not None
        assert run.metadata[MATRIX_RESPONSE_EVENT_ID_METADATA_KEY] == "$visible"
        assert [(message.role, message.content) for message in run.messages or []] == [("user", "question")]
    finally:
        storage.close()


def test_persist_response_event_id_wraps_team_session_run(tmp_path: Path) -> None:
    """Team sessions wrap the final assistant message the same way."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            teams={"crew": TeamConfig(display_name="Crew", role="Test crew", agents=["shared"])},
            agents={"shared": AgentConfig(display_name="Shared")},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        ),
        runtime_paths,
    )
    runtime_paths = runtime_paths_for(config)
    writer = _writer(config, runtime_paths, agent_name="crew")
    storage = writer.create_storage(None)
    try:
        session = TeamSession(
            session_id="session-1",
            team_id="crew",
            runs=[
                TeamRunOutput(
                    run_id="run-1",
                    team_id="crew",
                    status=RunStatus.completed,
                    content="Team answer",
                    messages=[
                        Message(role="user", content="question"),
                        Message(role="assistant", content="Team answer"),
                    ],
                ),
            ],
            created_at=1,
            updated_at=1,
        )
        storage.upsert_session(session)

        writer.persist_response_event_id_in_session_run(
            storage=storage,
            session_id="session-1",
            session_type=SessionType.TEAM,
            run_id="run-1",
            response_event_id="$team-visible",
            response_sender_id="@mindroom_crew:localhost",
        )

        persisted = get_team_session(storage, "session-1")
        assert persisted is not None
        run = cast("TeamRunOutput", (persisted.runs or [])[0])
        assert run.metadata is not None
        assert run.metadata[MATRIX_RESPONSE_EVENT_ID_METADATA_KEY] == "$team-visible"
        assert (run.messages or [])[-1].content == (
            '<msg event_id="$team-visible" from="@mindroom_crew:localhost"><![CDATA[Team answer]]></msg>'
        )
    finally:
        storage.close()


def test_persist_response_event_id_without_sender_keeps_metadata_only(tmp_path: Path) -> None:
    """Undelivered outcomes link the event in metadata without claiming it for the assistant."""
    config, runtime_paths = _agent_config(tmp_path)
    writer = _writer(config, runtime_paths)
    storage = writer.create_storage(None)
    try:
        storage.upsert_session(_agent_session_with_run(content="Final answer", with_assistant_message=True))

        writer.persist_response_event_id_in_session_run(
            storage=storage,
            session_id="session-1",
            session_type=SessionType.AGENT,
            run_id="run-1",
            response_event_id="$failure-note",
            response_sender_id=None,
        )

        persisted = get_agent_session(storage, "session-1")
        assert persisted is not None
        run = cast("RunOutput", (persisted.runs or [])[0])
        assert run.metadata is not None
        assert run.metadata[MATRIX_RESPONSE_EVENT_ID_METADATA_KEY] == "$failure-note"
        assert (run.messages or [])[-1].content == "Final answer"
    finally:
        storage.close()


def test_persist_response_event_id_wraps_contentless_run_from_assistant_message(tmp_path: Path) -> None:
    """Content-less runs fall back to the final assistant message's own text."""
    config, runtime_paths = _agent_config(tmp_path)
    writer = _writer(config, runtime_paths)
    storage = writer.create_storage(None)
    try:
        session = AgentSession(
            session_id="session-1",
            agent_id="shared",
            runs=[
                RunOutput(
                    run_id="run-1",
                    agent_id="shared",
                    status=RunStatus.completed,
                    content=None,
                    messages=[
                        Message(role="user", content="question"),
                        Message(role="assistant", content="Delivered team text"),
                        Message(role="assistant", content=""),
                    ],
                ),
            ],
            created_at=1,
            updated_at=1,
        )
        storage.upsert_session(session)

        for response_event_id in ("$visible", "$edited"):
            writer.persist_response_event_id_in_session_run(
                storage=storage,
                session_id="session-1",
                session_type=SessionType.AGENT,
                run_id="run-1",
                response_event_id=response_event_id,
                response_sender_id="@mindroom_shared:localhost",
            )

        persisted = get_agent_session(storage, "session-1")
        assert persisted is not None
        run = cast("RunOutput", (persisted.runs or [])[0])
        messages = run.messages or []
        # The tool-call-style empty assistant stub is never targeted, and a
        # changed callback re-wraps from the recovered canonical body.
        assert messages[1].content == (
            '<msg event_id="$edited" from="@mindroom_shared:localhost"><![CDATA[Delivered team text]]></msg>'
        )
        assert messages[2].content == ""
    finally:
        storage.close()
