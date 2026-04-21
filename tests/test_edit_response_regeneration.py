"""Test that agent responses are regenerated when user edits their message."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import nio
import pytest
from agno.db.base import SessionType
from agno.media import Audio
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom import interactive
from mindroom.agents import get_agent_session, remove_run_by_event_id
from mindroom.bot import AgentBot, TeamBot
from mindroom.commands import config_confirmation
from mindroom.config.main import Config
from mindroom.constants import (
    MATRIX_SOURCE_EVENT_IDS_METADATA_KEY,
    ROUTER_AGENT_NAME,
    resolve_runtime_paths,
)
from mindroom.conversation_state_writer import ConversationStateWriter
from mindroom.delivery_gateway import DeliveryResult
from mindroom.handled_turns import HandledTurnRecord, HandledTurnState
from mindroom.history.interrupted_replay import build_interrupted_replay_run, build_interrupted_replay_snapshot
from mindroom.history.types import HistoryScope
from mindroom.matrix.cache.thread_history_result import thread_history_result
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.message_content import _clear_mxc_cache
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.thread_utils import create_session_id
from mindroom.turn_store import LoadedTurnRecord
from tests.conftest import (
    bind_runtime_paths,
    delivered_matrix_side_effect,
    install_generate_response_mock,
    install_runtime_cache_support,
    make_matrix_client_mock,
    patch_response_runner_module,
    replace_edit_regenerator_deps,
    replace_turn_controller_deps,
    replace_turn_policy_deps,
    replace_turn_store_deps,
    runtime_paths_for,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine


def _room_send_response(event_id: str) -> MagicMock:
    """Create one minimal successful Matrix send response."""
    response = MagicMock(spec=nio.RoomSendResponse)
    response.event_id = event_id
    return response


@dataclass
class _FakeTeamStorage:
    session: TeamSession | None
    upserted_session: TeamSession | None = None

    def get_session(self, session_id: str, _session_type: object) -> TeamSession | None:
        if self.session is None or self.session.session_id != session_id:
            return None
        return self.session

    def upsert_session(self, session: TeamSession) -> None:
        self.upserted_session = session

    def close(self) -> None:
        return None


@dataclass
class _FakeAgentStorage:
    session: AgentSession | None
    upserted_session: AgentSession | None = None

    def get_session(self, session_id: str, _session_type: object) -> AgentSession | None:
        if self.session is None or self.session.session_id != session_id:
            return None
        return self.session

    def upsert_session(self, session: AgentSession) -> None:
        self.upserted_session = session

    def close(self) -> None:
        return None


def _test_config(
    tmp_path: Path,
    *,
    agent_names: tuple[str, ...] = ("test_agent",),
    voice_enabled: bool = False,
) -> Config:
    config = Config(
        agents={
            name: {
                "display_name": name.replace("_", " ").title(),
                "rooms": ["!test:example.com"],
            }
            for name in agent_names
        },
        voice={"enabled": voice_enabled},
        authorization={"default_room_access": True, "agent_reply_permissions": {}},
        mindroom_user={"username": "mindroom", "display_name": "MindRoom"},
    )
    return _bind_runtime_paths(config, tmp_path)


def _bind_runtime_paths(config: Config, tmp_path: Path) -> Config:
    """Attach example.com runtime paths to a test config."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://example.com",
            "MINDROOM_NAMESPACE": "",
        },
    )
    return bind_runtime_paths(config, runtime_paths)


@contextmanager
def _open_storage(storage: object) -> object:
    yield storage


def _turn_store(bot: AgentBot | TeamBot) -> object:
    """Return the unwrapped turn store for one bot."""
    return unwrap_extracted_collaborator(bot._turn_store)


def _response_event_id(bot: AgentBot | TeamBot, source_event_id: str) -> str | None:
    """Return the stored response event for one source event when present."""
    turn_record = bot._turn_store.get_turn_record(source_event_id)
    if turn_record is None:
        return None
    return turn_record.response_event_id


def _record_handled_turn(
    turn_store: object,
    source_event_ids: list[str],
    *,
    response_event_id: str | None = None,
    source_event_prompts: dict[str, str] | None = None,
    response_owner: str | None = None,
    history_scope: HistoryScope | None = None,
    conversation_target: MessageTarget | None = None,
) -> None:
    """Record one handled turn through the turn-store API."""
    turn_store.record_turn(
        HandledTurnState.create(
            source_event_ids,
            response_event_id=response_event_id,
            source_event_prompts=source_event_prompts,
            response_owner=response_owner,
            history_scope=history_scope,
            conversation_target=conversation_target,
        ),
    )


def _agent_history_scope(agent_name: str) -> HistoryScope:
    """Return the persisted agent history scope used in edit-regeneration tests."""
    return HistoryScope(kind="agent", scope_id=agent_name)


def _team_history_scope(team_name: str) -> HistoryScope:
    """Return the persisted team history scope used in edit-regeneration tests."""
    return HistoryScope(kind="team", scope_id=team_name)


def _team_test_config(tmp_path: Path) -> Config:
    config = Config(
        agents={
            "worker": {
                "display_name": "Worker",
                "rooms": ["!test:example.com"],
            },
        },
        teams={
            "test_team": {
                "display_name": "Test Team",
                "role": "Coordinate worker",
                "agents": ["worker"],
                "rooms": ["!test:example.com"],
            },
        },
        models={
            "default": {
                "provider": "openai",
                "id": "test-model",
            },
        },
        authorization={"default_room_access": True, "agent_reply_permissions": {}},
        mindroom_user={"username": "mindroom", "display_name": "MindRoom"},
    )
    return _bind_runtime_paths(config, tmp_path)


def _generate_response_with_locked_callback(
    response_event_id: str | None,
) -> Callable[..., Awaitable[str | None]]:
    """Execute locked edit cleanup in mocked response generation paths."""

    async def _generate_response(*_args: object, **kwargs: object) -> str | None:
        locked_callback = kwargs.get("on_lifecycle_lock_acquired")
        if locked_callback is not None:
            locked_callback()
        return response_event_id

    return _generate_response


@pytest.mark.asyncio
async def test_bot_regenerates_response_on_edit(tmp_path: Path) -> None:
    """Test that the bot regenerates its response when a user edits their message."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    config = _test_config(tmp_path)

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )

    # Mock the client
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")

    replace_edit_regenerator_deps(bot)
    replace_turn_policy_deps(bot)

    # Mock logger
    bot.logger = MagicMock()

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")

    # Create an original message event
    original_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "@test_agent what is 2+2?",
                "msgtype": "m.text",
            },
            "event_id": "$original:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    original_event.source = {
        "content": {
            "body": "@test_agent what is 2+2?",
            "msgtype": "m.text",
        },
        "event_id": "$original:example.com",
        "sender": "@user:example.com",
    }

    # Simulate that the bot has already responded to the original message
    response_event_id = "$response:example.com"
    stored_target = MessageTarget.resolve(
        room_id=room.room_id,
        thread_id=None,
        reply_to_event_id=original_event.event_id,
    )
    _record_handled_turn(
        bot._turn_store,
        [original_event.event_id],
        response_event_id=response_event_id,
        response_owner="test_agent",
        history_scope=_agent_history_scope("test_agent"),
        conversation_target=stored_target,
    )

    # Create an edit event
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* @test_agent what is 3+3?",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "@test_agent what is 3+3?",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* @test_agent what is 3+3?",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "@test_agent what is 3+3?",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$original:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
    }

    # Mock the methods needed for regeneration
    mock_streaming = AsyncMock(return_value=False)
    mock_ai_response = AsyncMock(return_value="The answer is 6")
    with (
        patch_response_runner_module(
            should_use_streaming=mock_streaming,
            ai_response=mock_ai_response,
        ),
        patch.object(bot._conversation_resolver, "extract_message_context", new_callable=AsyncMock) as mock_context,
        patch(
            "mindroom.delivery_gateway.edit_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
        ) as mock_edit,
    ):
        # Setup mocks
        mock_context.return_value = MagicMock(
            am_i_mentioned=True,
            is_thread=True,
            thread_id=stored_target.resolved_thread_id,
            thread_history=[],
            mentioned_agents=[MatrixID.from_agent("test_agent", "example.com", runtime_paths_for(config))],
        )
        # Process the edit event
        await bot._on_message(room, edit_event)

        # Verify that the bot attempted to regenerate the response
        mock_context.assert_called_once()
        mock_ai_response.assert_called_once()

        # Verify that the bot edited the existing response message
        mock_edit.assert_called_once()
        edit_args = mock_edit.call_args.args
        assert edit_args[0] is bot.client
        assert edit_args[1] == room.room_id
        assert edit_args[2] == response_event_id
        assert edit_args[4] == "The answer is 6"
        assert "m.relates_to" not in edit_args[3]

        # Verify that the response tracker still maps to the same response
        assert _response_event_id(bot, original_event.event_id) == response_event_id


@pytest.mark.asyncio
async def test_bot_edit_hooks_see_hydrated_sidecar_edit_body(tmp_path: Path) -> None:
    """Edit regeneration should use the resolved edited body from a v2 sidecar."""
    _clear_mxc_cache()
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )
    config = _test_config(tmp_path)
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")
    bot.client.download = AsyncMock(
        return_value=MagicMock(
            spec=nio.DownloadResponse,
            body=json.dumps(
                {
                    "body": "* @test_agent what is 99+1?",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "@test_agent what is 99+1?",
                        "msgtype": "m.text",
                    },
                    "m.relates_to": {
                        "event_id": "$original:example.com",
                        "rel_type": "m.replace",
                    },
                },
            ).encode("utf-8"),
        ),
    )
    replace_edit_regenerator_deps(bot)
    bot.logger = MagicMock()

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    _record_handled_turn(
        bot._turn_store,
        ["$original:example.com"],
        response_event_id="$response:example.com",
        response_owner="test_agent",
        history_scope=_agent_history_scope("test_agent"),
        conversation_target=MessageTarget.resolve(
            room_id=room.room_id,
            thread_id=None,
            reply_to_event_id="$original:example.com",
        ),
    )

    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* Preview edit",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "Preview edit",
                    "msgtype": "m.file",
                    "info": {"mimetype": "application/json"},
                    "io.mindroom.long_text": {
                        "version": 2,
                        "encoding": "matrix_event_content_json",
                    },
                    "url": "mxc://server/edit-sidecar-regeneration",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = edit_event.__dict__["source"]

    with (
        patch.object(bot._conversation_resolver, "extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(
            bot._ingress_hook_runner,
            "emit_message_received_hooks",
            new_callable=AsyncMock,
        ) as mock_emit_hooks,
    ):
        mock_context.return_value = MagicMock(
            am_i_mentioned=True,
            is_thread=True,
            thread_id="$original:example.com",
            thread_history=[],
            mentioned_agents=[MatrixID.from_agent("test_agent", "example.com", runtime_paths_for(config))],
            has_non_agent_mentions=False,
        )
        mock_emit_hooks.return_value = False

        await bot._on_message(room, edit_event)

    emitted_envelope = mock_emit_hooks.await_args.kwargs["envelope"]
    assert emitted_envelope.body == "@test_agent what is 99+1?"


@pytest.mark.asyncio
async def test_bot_edit_regeneration_does_not_rerun_response_gating_after_hydrated_recovery(tmp_path: Path) -> None:
    """Edit regeneration should not re-run should-respond heuristics after durable recovery."""
    _clear_mxc_cache()
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )
    config = _test_config(tmp_path, agent_names=("test_agent", "other_agent"))
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")
    bot.client.download = AsyncMock(
        return_value=MagicMock(
            spec=nio.DownloadResponse,
            body=json.dumps(
                {
                    "body": "* @test_agent what is 99+1?",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "@test_agent what is 99+1?",
                        "msgtype": "m.text",
                        "m.mentions": {
                            "user_ids": ["@mindroom_test_agent:example.com"],
                        },
                    },
                    "m.relates_to": {
                        "event_id": "$original:example.com",
                        "rel_type": "m.replace",
                    },
                },
            ).encode("utf-8"),
        ),
    )
    replace_edit_regenerator_deps(bot)
    bot.logger = MagicMock()
    bot._conversation_resolver.derive_conversation_context = AsyncMock(return_value=(False, None, []))
    bot._conversation_cache.get_thread_history = AsyncMock(return_value=[])
    bot._conversation_cache.get_thread_snapshot = AsyncMock(
        return_value=thread_history_result([], is_full_history=False),
    )

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    _record_handled_turn(bot._turn_store, ["$original:example.com"], response_event_id="$response:example.com")

    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* Preview edit",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "Preview edit",
                    "msgtype": "m.file",
                    "info": {"mimetype": "application/json"},
                    "io.mindroom.long_text": {
                        "version": 2,
                        "encoding": "matrix_event_content_json",
                    },
                    "url": "mxc://server/edit-sidecar-gating",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = edit_event.__dict__["source"]

    with (
        patch.object(
            bot._ingress_hook_runner,
            "emit_message_received_hooks",
            new_callable=AsyncMock,
        ) as mock_emit_hooks,
    ):
        mock_emit_hooks.return_value = False

        await bot._on_message(room, edit_event)


@pytest.mark.asyncio
async def test_handle_message_edit_reuses_persisted_target_and_thread_scope(
    tmp_path: Path,
) -> None:
    """Edit regeneration should reuse the recorded target instead of rebuilding it from live heuristics."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )
    config = _test_config(tmp_path)
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")
    replace_edit_regenerator_deps(bot)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    wrap_extracted_collaborators(bot, "_delivery_gateway", "_response_runner")
    replace_turn_controller_deps(
        bot,
        delivery_gateway=bot._delivery_gateway,
        response_runner=bot._response_runner,
    )

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    stored_target = MessageTarget.resolve(
        room_id=room.room_id,
        thread_id="$original:example.com",
        reply_to_event_id="$router-echo:example.com",
    )
    _record_handled_turn(
        bot._turn_store,
        ["$original:example.com"],
        response_event_id="$response:example.com",
        response_owner="test_agent",
        history_scope=_agent_history_scope("test_agent"),
        conversation_target=stored_target,
    )

    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* updated question",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "updated question",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": room.room_id,
        },
    )
    edit_event.source = {
        "content": {
            "body": "* updated question",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "updated question",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$original:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
    }

    with (
        patch.object(bot._conversation_resolver, "extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(
            bot._conversation_resolver,
            "fetch_thread_history",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_fetch_history,
        patch.object(
            bot._turn_store,
            "_remove_stale_runs_for_turn_record",
            return_value=True,
        ) as mock_remove_stale_runs,
        patch.object(
            bot,
            "_generate_response",
            new_callable=AsyncMock,
            side_effect=_generate_response_with_locked_callback("$response:example.com"),
        ) as mock_generate_response,
    ):
        mock_context.return_value = MagicMock(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )

        await bot._edit_regenerator.handle_message_edit(
            room,
            edit_event,
            EventInfo.from_event(edit_event.source),
            requester_user_id="@user:example.com",
        )

    mock_fetch_history.assert_awaited_once_with(bot.client, room.room_id, stored_target.resolved_thread_id)
    mock_remove_stale_runs.assert_called_once()
    call_kwargs = mock_generate_response.call_args.kwargs
    assert call_kwargs["reply_to_event_id"] == "$original:example.com"
    assert call_kwargs["thread_id"] == stored_target.resolved_thread_id
    assert call_kwargs["target"] == stored_target


def test_remove_run_by_event_id_removes_team_runs() -> None:
    """Team edit regeneration should be able to delete stale runs from TeamSession storage."""
    session = TeamSession(
        session_id="session-1",
        team_id="test_team",
        runs=[
            TeamRunOutput(
                session_id="session-1",
                metadata={"matrix_event_id": "$original:example.com"},
            ),
            TeamRunOutput(
                session_id="session-1",
                metadata={"matrix_event_id": "$other:example.com"},
            ),
        ],
    )
    storage = _FakeTeamStorage(session)

    removed = remove_run_by_event_id(
        storage,
        "session-1",
        "$original:example.com",
        session_type=SessionType.TEAM,
    )

    assert removed is True
    assert storage.upserted_session is session
    assert len(session.runs or []) == 1
    assert session.runs[0].metadata["matrix_event_id"] == "$other:example.com"


def test_remove_run_by_event_id_matches_coalesced_source_event_ids() -> None:
    """Coalesced runs should be removable through any batch member event ID."""
    session = TeamSession(
        session_id="session-1",
        team_id="test_team",
        runs=[
            TeamRunOutput(
                session_id="session-1",
                metadata={
                    "matrix_event_id": "$primary:example.com",
                    "matrix_source_event_ids": ["$first:example.com", "$primary:example.com"],
                },
            ),
        ],
    )
    storage = _FakeTeamStorage(session)

    removed = remove_run_by_event_id(
        storage,
        "session-1",
        "$first:example.com",
        session_type=SessionType.TEAM,
    )

    assert removed is True
    assert session.runs == []


@pytest.mark.asyncio
async def test_team_bot_regenerates_edits_against_team_history_storage(tmp_path: Path) -> None:
    """Team edit regeneration should delete stale runs from the shared team session."""
    agent_user = AgentMatrixUser(
        agent_name="test_team",
        user_id="@mindroom_test_team:example.com",
        display_name="Test Team",
        password="test_password",  # noqa: S106
    )
    config = _team_test_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    team_member = config.get_ids(runtime_paths)["worker"]
    bot = TeamBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths,
        rooms=["!test:example.com"],
        team_agents=[team_member],
        team_mode="coordinate",
    )
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_team:example.com")
    replace_edit_regenerator_deps(bot)
    bot.logger = MagicMock()
    bot.orchestrator = MagicMock(
        current_config=config,
        config=config,
        runtime_paths=runtime_paths,
    )

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_team:example.com")
    response_event_id = "$response:example.com"
    stored_target = MessageTarget.resolve(
        room_id=room.room_id,
        thread_id=None,
        reply_to_event_id="$original:example.com",
    )
    _record_handled_turn(
        bot._turn_store,
        ["$original:example.com"],
        response_event_id=response_event_id,
        response_owner="test_team",
        history_scope=_team_history_scope("test_team"),
        conversation_target=stored_target,
    )
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* @test_team redo that",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "@test_team redo that",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* @test_team redo that",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "@test_team redo that",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$original:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
    }

    storage = MagicMock()
    scheduled_tasks: list[asyncio.Task[None]] = []

    @asynccontextmanager
    async def noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
        yield

    async def fake_store_conversation_memory(*_args: object, **_kwargs: object) -> None:
        return None

    def schedule_background_task(
        coro: Coroutine[object, object, None],
        *,
        name: str,
        error_handler: object | None = None,  # noqa: ARG001
        owner: object | None = None,  # noqa: ARG001
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(coro, name=name)
        scheduled_tasks.append(task)
        return task

    mock_team_response = AsyncMock(return_value="team response")

    with (
        patch.object(bot._conversation_resolver, "extract_message_context", new_callable=AsyncMock) as mock_context,
        patch("mindroom.bot.store_conversation_memory", side_effect=fake_store_conversation_memory),
        patch("mindroom.bot.create_background_task", side_effect=schedule_background_task),
        patch_response_runner_module(
            team_response=mock_team_response,
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=noop_typing_indicator,
        ),
        patch(
            "mindroom.response_lifecycle.apply_post_response_effects",
            new=AsyncMock(),
        ),
        patch(
            "mindroom.response_runner.DeliveryGateway.deliver_final",
            new=AsyncMock(
                return_value=DeliveryResult(
                    event_id=response_event_id,
                    response_text="team response",
                    delivery_kind="edited",
                ),
            ),
        ),
        patch.object(
            bot._conversation_state_writer,
            "create_storage",
            return_value=storage,
        ),
        patch("mindroom.turn_store.remove_run_by_event_id", return_value=True) as mock_remove_run,
    ):
        mock_context.return_value = MagicMock(
            am_i_mentioned=True,
            is_thread=True,
            thread_id=stored_target.resolved_thread_id,
            thread_history=[],
            mentioned_agents=[MatrixID.from_agent("test_team", "example.com", runtime_paths)],
        )

        await bot._on_message(room, edit_event)

    if scheduled_tasks:
        await asyncio.gather(*scheduled_tasks)

    assert mock_remove_run.call_args_list == [
        call(
            storage,
            stored_target.session_id,
            "$original:example.com",
            session_type=SessionType.TEAM,
        ),
    ]
    mock_team_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_ignores_edit_without_previous_response(tmp_path: Path) -> None:
    """Test that the bot ignores edits if it didn't respond to the original message."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    config = _test_config(tmp_path)

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )

    # Mock the client
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")

    replace_edit_regenerator_deps(bot)
    replace_turn_policy_deps(bot)

    # Mock logger
    bot.logger = MagicMock()

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Create an edit event for a message we never responded to
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* @test_agent help",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "@test_agent help",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$unknown:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* @test_agent help",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "@test_agent help",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$unknown:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
    }

    # Mock the methods
    with (
        patch.object(bot._conversation_resolver, "extract_message_context", new_callable=AsyncMock),
        patch.object(bot, "_generate_response", new_callable=AsyncMock) as mock_generate,
        patch.object(bot, "_edit_message", new_callable=AsyncMock) as mock_edit,
    ):
        # Process the edit event
        await bot._on_message(room, edit_event)

        # Verify that the bot did NOT attempt to regenerate
        mock_generate.assert_not_called()
        mock_edit.assert_not_called()


@pytest.mark.asyncio
async def test_bot_ignores_agent_edits(tmp_path: Path) -> None:
    """Test that the bot ignores edit events from other agents (e.g., streaming edits)."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    config = _test_config(tmp_path, agent_names=("test_agent", "helper_agent"))

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )

    # Mock the client
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")

    replace_edit_regenerator_deps(bot)

    # Mock logger
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    wrap_extracted_collaborators(bot, "_delivery_gateway", "_response_runner")
    replace_turn_controller_deps(
        bot,
        delivery_gateway=bot._delivery_gateway,
        response_runner=bot._response_runner,
    )
    replace_turn_controller_deps(bot, logger=bot.logger)

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Simulate that the bot has responded to some message
    _record_handled_turn(bot._turn_store, ["$original:example.com"], response_event_id="$response:example.com")

    # Test 1: Bot's own edit
    own_edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* Updated response",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "Updated response",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@mindroom_test_agent:example.com",  # Bot's own edit
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    own_edit_event.source = {
        "content": {
            "body": "* Updated response",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "Updated response",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$original:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@mindroom_test_agent:example.com",
    }

    # Test 2: Another agent's edit
    other_agent_edit = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* Hey @test_agent what's up",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "Hey @test_agent what's up",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit2:example.com",
            "sender": "@mindroom_helper_agent:example.com",  # Another agent's edit
            "origin_server_ts": 1000002,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    other_agent_edit.source = {
        "content": {
            "body": "* Hey @test_agent what's up",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "Hey @test_agent what's up",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$original:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit2:example.com",
        "sender": "@mindroom_helper_agent:example.com",
    }

    # Mock the methods
    with (
        patch.object(bot._conversation_resolver, "extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(bot, "_edit_message", new_callable=AsyncMock) as mock_edit,
    ):
        mock_context.return_value = MagicMock(
            am_i_mentioned=False,
            is_thread=False,
            thread_history=[],
            thread_id=None,
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )

        # Process the bot's own edit event
        await bot._on_message(room, own_edit_event)

        # Process another agent's edit event
        await bot._on_message(room, other_agent_edit)

        # Both edits should be ignored before any regeneration work begins.
        mock_context.assert_not_called()
        mock_edit.assert_not_called()


@pytest.mark.asyncio
async def test_handle_message_edit_rebuilds_coalesced_prompt_for_non_primary_edit(
    tmp_path: Path,
) -> None:
    """Editing any member of a coalesced turn should regenerate against the full reconstructed prompt."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    config = _test_config(tmp_path)

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")
    stored_target = MessageTarget.resolve(
        room_id="!test:example.com",
        thread_id=None,
        reply_to_event_id="$primary:example.com",
    )
    replace_edit_regenerator_deps(bot)
    _record_handled_turn(
        bot._turn_store,
        ["$first:example.com", "$primary:example.com"],
        response_event_id="$response:example.com",
        source_event_prompts={
            "$first:example.com": "first",
            "$primary:example.com": "primary",
        },
        response_owner="test_agent",
        history_scope=_agent_history_scope("test_agent"),
        conversation_target=stored_target,
    )
    bot.logger = MagicMock()

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* updated first",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "updated first",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$first:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* updated first",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "updated first",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$first:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
    }

    with (
        patch.object(bot._conversation_resolver, "extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(
            bot._conversation_state_writer,
            "create_storage",
        ) as mock_create_storage,
        patch("mindroom.turn_store.remove_run_by_event_id", return_value=True) as mock_remove_run,
        patch.object(
            bot,
            "_generate_response",
            new_callable=AsyncMock,
            side_effect=_generate_response_with_locked_callback("$response:example.com"),
        ) as mock_generate_response,
    ):
        mock_context.return_value = MagicMock(
            am_i_mentioned=False,
            is_thread=True,
            thread_id=stored_target.resolved_thread_id,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )

        await bot._edit_regenerator.handle_message_edit(
            room,
            edit_event,
            EventInfo.from_event(edit_event.source),
            requester_user_id=edit_event.sender,
        )

        mock_generate_response.assert_awaited_once()
        call_kwargs = mock_generate_response.call_args.kwargs
        assert call_kwargs["prompt"] == (
            "The user sent the following messages in quick succession. "
            "Treat them as one turn and respond once:\n\nupdated first\nprimary"
        )
        assert call_kwargs["reply_to_event_id"] == "$primary:example.com"
        assert call_kwargs["target"] == stored_target
        assert call_kwargs["matrix_run_metadata"] == {
            "matrix_source_event_ids": ["$first:example.com", "$primary:example.com"],
            "matrix_source_event_prompts": {
                "$first:example.com": "updated first",
                "$primary:example.com": "primary",
            },
        }
        assert _response_event_id(bot, "$first:example.com") == "$response:example.com"
        assert _response_event_id(bot, "$primary:example.com") == "$response:example.com"
        assert mock_remove_run.call_count == 2
        mock_remove_run.assert_has_calls(
            [
                call(
                    mock_create_storage.return_value,
                    "!test:example.com",
                    "$first:example.com",
                    session_type=SessionType.AGENT,
                ),
                call(
                    mock_create_storage.return_value,
                    "!test:example.com",
                    "$primary:example.com",
                    session_type=SessionType.AGENT,
                ),
            ],
        )


@pytest.mark.asyncio
async def test_handle_message_edit_reuses_existing_response_without_placeholder_flag(
    tmp_path: Path,
) -> None:
    """Edited-message regeneration must keep message reuse distinct from startup placeholders."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    config = _test_config(tmp_path)

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")
    bot.client.room_send.return_value = _room_send_response("$thinking:example.com")
    replace_edit_regenerator_deps(bot)
    bot.logger = MagicMock()

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    stored_target = MessageTarget.resolve(
        room_id=room.room_id,
        thread_id=None,
        reply_to_event_id="$original:example.com",
    )
    _record_handled_turn(
        bot._turn_store,
        ["$original:example.com"],
        response_event_id="$response:example.com",
        response_owner="test_agent",
        history_scope=_agent_history_scope("test_agent"),
        conversation_target=stored_target,
    )

    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* @test_agent what is 3+3?",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "@test_agent what is 3+3?",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* @test_agent what is 3+3?",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "@test_agent what is 3+3?",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$original:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
    }

    with (
        patch.object(bot._conversation_resolver, "extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(
            bot._conversation_state_writer,
            "create_storage",
        ),
        patch("mindroom.turn_store.remove_run_by_event_id", return_value=False) as mock_remove_run,
        patch.object(
            bot,
            "_generate_response",
            new_callable=AsyncMock,
            side_effect=_generate_response_with_locked_callback("$response:example.com"),
        ) as mock_generate_response,
    ):
        mock_context.return_value = MagicMock(
            am_i_mentioned=True,
            is_thread=True,
            thread_id=stored_target.resolved_thread_id,
            thread_history=[],
            mentioned_agents=[MatrixID.from_agent("test_agent", "example.com", runtime_paths_for(config))],
            has_non_agent_mentions=False,
        )

        await bot._edit_regenerator.handle_message_edit(
            room,
            edit_event,
            EventInfo.from_event(edit_event.source),
            requester_user_id="@user:example.com",
        )

        mock_generate_response.assert_awaited_once()
        call_kwargs = mock_generate_response.call_args.kwargs
        assert call_kwargs["reply_to_event_id"] == "$original:example.com"
        assert call_kwargs["existing_event_id"] == "$response:example.com"
        assert call_kwargs["existing_event_is_placeholder"] is False
        assert call_kwargs["target"] == stored_target
        assert _response_event_id(bot, "$original:example.com") == "$response:example.com"
        mock_remove_run.assert_called_once()


@pytest.mark.asyncio
async def test_handle_message_edit_does_not_remark_response_when_regeneration_is_suppressed(
    tmp_path: Path,
) -> None:
    """Suppressed edit regeneration should leave the existing response mapping unchanged."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    config = _test_config(tmp_path)

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")
    replace_edit_regenerator_deps(bot)
    stored_target = MessageTarget.resolve(
        room_id="!test:example.com",
        thread_id=None,
        reply_to_event_id="$original:example.com",
    )
    _record_handled_turn(
        bot._turn_store,
        ["$original:example.com"],
        response_event_id="$response:example.com",
        response_owner="test_agent",
        history_scope=_agent_history_scope("test_agent"),
        conversation_target=stored_target,
    )
    turn_store = _turn_store(bot)
    turn_store.record_turn = MagicMock(wraps=turn_store.record_turn)
    bot.logger = MagicMock()

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* @test_agent what is 3+3?",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "@test_agent what is 3+3?",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* @test_agent what is 3+3?",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "@test_agent what is 3+3?",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$original:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
    }

    with (
        patch.object(bot._conversation_resolver, "extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(
            bot._conversation_state_writer,
            "create_storage",
        ),
        patch("mindroom.turn_store.remove_run_by_event_id", return_value=False) as mock_remove_run,
        patch.object(
            bot,
            "_generate_response",
            new_callable=AsyncMock,
            side_effect=_generate_response_with_locked_callback(None),
        ) as mock_generate_response,
    ):
        mock_context.return_value = MagicMock(
            am_i_mentioned=True,
            is_thread=True,
            thread_id=stored_target.resolved_thread_id,
            thread_history=[],
            mentioned_agents=[MatrixID.from_agent("test_agent", "example.com", runtime_paths_for(config))],
            has_non_agent_mentions=False,
        )

        await bot._edit_regenerator.handle_message_edit(
            room,
            edit_event,
            EventInfo.from_event(edit_event.source),
            requester_user_id=edit_event.sender,
        )

        mock_generate_response.assert_awaited_once()
        assert turn_store.record_turn.call_count == 0
        assert _response_event_id(bot, "$original:example.com") == "$response:example.com"
        mock_remove_run.assert_called_once()


@pytest.mark.asyncio
async def test_handle_message_edit_rebuilds_coalesced_prompt_from_persisted_run_metadata(
    tmp_path: Path,
) -> None:
    """Coalesced edit regeneration should fall back to persisted run metadata when the ledger lacks prompts."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    config = _test_config(tmp_path)

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")
    replace_edit_regenerator_deps(bot)
    stored_target = MessageTarget.resolve(
        room_id="!test:example.com",
        thread_id=None,
        reply_to_event_id="$primary:example.com",
    )
    _record_handled_turn(
        bot._turn_store,
        ["$first:example.com", "$primary:example.com"],
        response_event_id="$response:example.com",
        response_owner="test_agent",
        history_scope=_agent_history_scope("test_agent"),
        conversation_target=stored_target,
    )
    bot.logger = MagicMock()

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* updated first",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "updated first",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$first:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* updated first",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "updated first",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$first:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
    }

    session_id = create_session_id("!test:example.com", None)
    storage = MagicMock()
    storage.get_session.return_value = AgentSession(
        session_id=session_id,
        runs=[
            RunOutput(
                session_id=session_id,
                metadata={
                    "matrix_event_id": "$primary:example.com",
                    "matrix_source_event_ids": ["$first:example.com", "$primary:example.com"],
                    "matrix_source_event_prompts": {
                        "$first:example.com": "first",
                        "$primary:example.com": "primary",
                    },
                },
            ),
        ],
    )

    with (
        patch.object(bot._conversation_resolver, "extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(bot._conversation_resolver, "fetch_thread_history", new=AsyncMock(return_value=[])),
        patch.object(
            bot._conversation_state_writer,
            "create_storage",
            return_value=storage,
        ),
        patch("mindroom.turn_store.remove_run_by_event_id", return_value=True) as mock_remove_run,
        patch.object(
            bot,
            "_generate_response",
            new_callable=AsyncMock,
            side_effect=_generate_response_with_locked_callback("$response:example.com"),
        ) as mock_generate_response,
    ):
        mock_context.return_value = MagicMock(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )

        await bot._edit_regenerator.handle_message_edit(
            room,
            edit_event,
            EventInfo.from_event(edit_event.source),
            requester_user_id=edit_event.sender,
        )

        mock_generate_response.assert_awaited_once()
        call_kwargs = mock_generate_response.call_args.kwargs
        assert call_kwargs["prompt"] == (
            "The user sent the following messages in quick succession. "
            "Treat them as one turn and respond once:\n\nupdated first\nprimary"
        )
        assert call_kwargs["reply_to_event_id"] == "$primary:example.com"
        assert call_kwargs["target"] == stored_target
        assert call_kwargs["matrix_run_metadata"] == {
            "matrix_source_event_ids": ["$first:example.com", "$primary:example.com"],
            "matrix_source_event_prompts": {
                "$first:example.com": "updated first",
                "$primary:example.com": "primary",
            },
        }
        assert _response_event_id(bot, "$first:example.com") == "$response:example.com"
        assert _response_event_id(bot, "$primary:example.com") == "$response:example.com"
        assert mock_remove_run.call_count == 2
        mock_remove_run.assert_has_calls(
            [
                call(
                    storage,
                    "!test:example.com",
                    "$first:example.com",
                    session_type=SessionType.AGENT,
                ),
                call(
                    storage,
                    "!test:example.com",
                    "$primary:example.com",
                    session_type=SessionType.AGENT,
                ),
            ],
        )


def test_load_turn_prefers_newest_matching_run(tmp_path: Path) -> None:
    """TurnStore should prefer the newest persisted matching run metadata."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )
    config = _test_config(tmp_path)
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )

    session_id = create_session_id("!test:example.com", None)
    storage = MagicMock()
    storage.get_session.return_value = AgentSession(
        session_id=session_id,
        runs=[
            RunOutput(
                run_id="run-old",
                session_id=session_id,
                metadata={
                    "matrix_event_id": "$primary:example.com",
                    "matrix_source_event_ids": ["$first:example.com", "$primary:example.com"],
                    "matrix_source_event_prompts": {
                        "$first:example.com": "first old",
                        "$primary:example.com": "primary old",
                    },
                    "matrix_response_event_id": "$response-old:example.com",
                },
            ),
            RunOutput(
                run_id="run-new",
                session_id=session_id,
                metadata={
                    "matrix_event_id": "$primary:example.com",
                    "matrix_source_event_ids": ["$first:example.com", "$primary:example.com"],
                    "matrix_source_event_prompts": {
                        "$first:example.com": "first new",
                        "$primary:example.com": "primary new",
                    },
                    "matrix_response_event_id": "$response-new:example.com",
                },
            ),
        ],
    )
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")

    with patch.object(
        bot._conversation_state_writer,
        "create_storage",
        return_value=storage,
    ):
        loaded_turn = bot._turn_store.load_turn(
            room=room,
            thread_id=None,
            original_event_id="$first:example.com",
            requester_user_id="@user:example.com",
        )

    assert loaded_turn is not None
    assert loaded_turn.record.response_event_id == "$response-new:example.com"
    assert loaded_turn.record.source_event_prompts == {
        "$first:example.com": "first new",
        "$primary:example.com": "primary new",
    }


def test_load_turn_preserves_persisted_anchor_for_interactive_selection(tmp_path: Path) -> None:
    """Selection-triggered runs should keep the original question anchor when reloaded."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )
    config = _test_config(tmp_path)
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )

    _record_handled_turn(
        bot._turn_store,
        ["$selection:example.com"],
        response_event_id="$response-ledger:example.com",
    )

    session_id = create_session_id("!test:example.com", None)
    storage = MagicMock()
    storage.get_session.return_value = AgentSession(
        session_id=session_id,
        runs=[
            RunOutput(
                run_id="run-selection",
                session_id=session_id,
                metadata={
                    "matrix_event_id": "$question:example.com",
                    "matrix_source_event_ids": ["$selection:example.com"],
                    "matrix_response_event_id": "$response-persisted:example.com",
                },
            ),
        ],
    )
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")

    with patch.object(
        bot._conversation_state_writer,
        "create_storage",
        return_value=storage,
    ):
        loaded_turn = bot._turn_store.load_turn(
            room=room,
            thread_id=None,
            original_event_id="$selection:example.com",
            requester_user_id="@user:example.com",
        )

    assert loaded_turn is not None
    assert loaded_turn.record.anchor_event_id == "$question:example.com"
    assert loaded_turn.record.source_event_ids == ("$selection:example.com",)
    assert loaded_turn.record.response_event_id == "$response-persisted:example.com"
    assert loaded_turn.requires_backfill is True


@pytest.mark.asyncio
async def test_handle_message_edit_uses_persisted_interrupted_response_event_id_after_restart(
    tmp_path: Path,
) -> None:
    """A restarted bot should regenerate against the visible interrupted reply when persistence provides it."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )
    config = _test_config(tmp_path)
    session_id = create_session_id("!test:example.com", None)

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")
    replace_edit_regenerator_deps(bot)
    bot.logger = MagicMock()

    storage = MagicMock()
    storage.get_session.return_value = AgentSession(
        session_id=session_id,
        runs=[
            build_interrupted_replay_run(
                snapshot=build_interrupted_replay_snapshot(
                    user_message="original question",
                    partial_text="Half done",
                    completed_tools=[],
                    interrupted_tools=[],
                    run_metadata={
                        "matrix_event_id": "$original:example.com",
                        "matrix_source_event_ids": ["$original:example.com"],
                        "matrix_source_event_prompts": {
                            "$original:example.com": "original question",
                        },
                    },
                    response_event_id="$partial-response:example.com",
                    interruption_reason="Run interrupted",
                ),
                run_id="run-interrupted",
                scope_id="test_agent",
                session_id=session_id,
                is_team=False,
            ),
        ],
    )

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* updated original question",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "updated original question",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* updated original question",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "updated original question",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$original:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
    }

    with (
        patch.object(bot._conversation_state_writer, "create_storage", return_value=storage),
        patch.object(bot._conversation_resolver, "extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(
            bot,
            "_generate_response",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_generate_response,
    ):
        mock_context.return_value = MagicMock(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
            requires_full_thread_history=False,
        )

        await bot._edit_regenerator.handle_message_edit(
            room,
            edit_event,
            EventInfo.from_event(edit_event.source),
            requester_user_id=edit_event.sender,
        )

    mock_generate_response.assert_awaited_once()
    call_kwargs = mock_generate_response.call_args.kwargs
    assert call_kwargs["existing_event_id"] == "$partial-response:example.com"
    assert call_kwargs["reply_to_event_id"] == "$original:example.com"
    assert _response_event_id(bot, "$original:example.com") == "$partial-response:example.com"


@pytest.mark.asyncio
async def test_edit_regenerator_preserves_interactive_selection_run_metadata(tmp_path: Path) -> None:
    """Editing a numeric selection should keep the original question anchor and selection lookup metadata."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )
    config = _test_config(tmp_path)
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")
    replace_edit_regenerator_deps(bot)
    _record_handled_turn(
        bot._turn_store,
        ["$selection:example.com"],
        response_event_id="$response:example.com",
    )

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* 2",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "2",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$selection:example.com",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* 2",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "2",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": "$selection:example.com",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
        "origin_server_ts": 1000000,
        "type": "m.room.message",
        "room_id": "!test:example.com",
    }

    session_id = create_session_id("!test:example.com", None)
    storage = MagicMock()
    storage.get_session.return_value = AgentSession(
        session_id=session_id,
        runs=[
            RunOutput(
                run_id="run-selection",
                session_id=session_id,
                metadata={
                    "matrix_event_id": "$question:example.com",
                    "matrix_source_event_ids": ["$selection:example.com"],
                    "matrix_response_event_id": "$response:example.com",
                },
            ),
        ],
    )
    generate_response = AsyncMock(return_value="$response-new:example.com")
    replace_edit_regenerator_deps(bot, generate_response=generate_response)

    with (
        patch.object(bot._conversation_resolver, "extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(
            bot._conversation_state_writer,
            "create_storage",
            return_value=storage,
        ),
        patch.object(
            bot._ingress_hook_runner,
            "emit_message_received_hooks",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        mock_context.return_value = MagicMock(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
            requires_full_thread_history=False,
        )

        await bot._edit_regenerator.handle_message_edit(
            room,
            edit_event,
            EventInfo.from_event(edit_event.source),
            requester_user_id=edit_event.sender,
        )

    generate_response.assert_awaited_once()
    call_kwargs = generate_response.call_args.kwargs
    assert call_kwargs["reply_to_event_id"] == "$question:example.com"
    assert call_kwargs["matrix_run_metadata"] == {
        MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$selection:example.com"],
    }


@pytest.mark.asyncio
async def test_edit_regenerator_backfill_preserves_interactive_selection_anchor_when_suppressed(
    tmp_path: Path,
) -> None:
    """Suppressed interactive-selection regeneration should still backfill a converged question anchor."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )
    config = _test_config(tmp_path)
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")
    replace_edit_regenerator_deps(bot)
    _record_handled_turn(
        bot._turn_store,
        ["$selection:example.com"],
        response_event_id="$response:example.com",
    )

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* 2",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "2",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$selection:example.com",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* 2",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "2",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": "$selection:example.com",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
        "origin_server_ts": 1000000,
        "type": "m.room.message",
        "room_id": "!test:example.com",
    }

    session_id = create_session_id("!test:example.com", None)
    storage = MagicMock()
    storage.get_session.return_value = AgentSession(
        session_id=session_id,
        runs=[
            RunOutput(
                run_id="run-selection",
                session_id=session_id,
                metadata={
                    "matrix_event_id": "$question:example.com",
                    "matrix_source_event_ids": ["$selection:example.com"],
                    "matrix_response_event_id": "$response:example.com",
                },
            ),
        ],
    )
    generate_response = AsyncMock(return_value=None)
    replace_edit_regenerator_deps(bot, generate_response=generate_response)

    with (
        patch.object(bot._conversation_resolver, "extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(
            bot._conversation_state_writer,
            "create_storage",
            return_value=storage,
        ),
        patch.object(
            bot._ingress_hook_runner,
            "emit_message_received_hooks",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        mock_context.return_value = MagicMock(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
            requires_full_thread_history=False,
        )

        await bot._edit_regenerator.handle_message_edit(
            room,
            edit_event,
            EventInfo.from_event(edit_event.source),
            requester_user_id=edit_event.sender,
        )

        loaded_turn = bot._turn_store.load_turn(
            room=room,
            thread_id=None,
            original_event_id="$selection:example.com",
            requester_user_id=edit_event.sender,
        )

    generate_response.assert_awaited_once()
    assert loaded_turn is not None
    assert loaded_turn.record.anchor_event_id == "$question:example.com"
    assert loaded_turn.record.response_event_id == "$response:example.com"
    assert loaded_turn.requires_backfill is False


def test_load_turn_prefers_newest_match_across_thread_and_room_sessions(tmp_path: Path) -> None:
    """TurnStore should compare matching persisted runs across thread and room scopes."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )
    config = _test_config(tmp_path)
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )

    threaded_session_id = create_session_id("!test:example.com", "$thread:example.com")
    room_session_id = create_session_id("!test:example.com", None)
    threaded_storage = _FakeAgentStorage(
        AgentSession(
            session_id=threaded_session_id,
            runs=[
                RunOutput(
                    run_id="run-thread-old",
                    session_id=threaded_session_id,
                    created_at=1,
                    metadata={
                        "matrix_event_id": "$primary:example.com",
                        "matrix_source_event_ids": ["$first:example.com", "$primary:example.com"],
                        "matrix_source_event_prompts": {
                            "$first:example.com": "first thread",
                            "$primary:example.com": "primary thread",
                        },
                        "matrix_response_event_id": "$response-thread:example.com",
                    },
                ),
            ],
        ),
    )
    room_storage = _FakeAgentStorage(
        AgentSession(
            session_id=room_session_id,
            runs=[
                RunOutput(
                    run_id="run-room-new",
                    session_id=room_session_id,
                    created_at=2,
                    metadata={
                        "matrix_event_id": "$primary:example.com",
                        "matrix_source_event_ids": ["$first:example.com", "$primary:example.com"],
                        "matrix_source_event_prompts": {
                            "$first:example.com": "first room",
                            "$primary:example.com": "primary room",
                        },
                        "matrix_response_event_id": "$response-room:example.com",
                    },
                ),
            ],
        ),
    )
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")

    with patch.object(
        bot._conversation_state_writer,
        "create_storage",
        side_effect=[threaded_storage, room_storage],
    ):
        loaded_turn = bot._turn_store.load_turn(
            room=room,
            thread_id="$thread:example.com",
            original_event_id="$first:example.com",
            requester_user_id="@user:example.com",
        )

    assert loaded_turn is not None
    assert loaded_turn.record.response_event_id == "$response-room:example.com"
    assert loaded_turn.record.source_event_prompts == {
        "$first:example.com": "first room",
        "$primary:example.com": "primary room",
    }


def test_turn_store_fallback_cleanup_uses_state_writer_helpers_and_rebound_logger(
    tmp_path: Path,
) -> None:
    """Fallback stale-run cleanup should stay owned by TurnStore and the state writer."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )
    config = _test_config(tmp_path)
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )

    captured_logger = MagicMock()
    rebound_logger = MagicMock()
    state_writer = unwrap_extracted_collaborator(bot._conversation_state_writer)
    bot._conversation_state_writer = ConversationStateWriter(
        replace(state_writer.deps, logger=captured_logger),
    )
    bot.logger = rebound_logger
    replace_turn_store_deps(
        bot,
        state_writer=bot._conversation_state_writer,
    )

    storage = MagicMock()
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    loaded_turn = LoadedTurnRecord(
        record=HandledTurnRecord(
            anchor_event_id="$original:example.com",
            source_event_ids=("$original:example.com",),
            response_event_id="$response:example.com",
        ),
        recorded_turn_context_available=False,
        response_owner_missing=False,
        requires_backfill=False,
    )

    with (
        patch.object(
            bot._conversation_state_writer,
            "create_storage",
            return_value=storage,
        ) as mock_create_storage,
        patch("mindroom.turn_store.remove_run_by_event_id", return_value=True),
    ):
        bot._turn_store.remove_stale_runs_for_edit(
            loaded_turn=loaded_turn,
            room=room,
            thread_id=None,
            original_event_id="$original:example.com",
            requester_user_id="@user:example.com",
        )

    mock_create_storage.assert_called_once()
    captured_logger.info.assert_called_once_with(
        "Removed stale run for edited message",
        event_id="$original:example.com",
        session_id="!test:example.com",
    )
    rebound_logger.info.assert_not_called()
    storage.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_handle_message_edit_uses_fallback_cleanup_when_turn_context_was_reconstructed(
    tmp_path: Path,
) -> None:
    """Legacy rows without stored target/scope should still use broad stale-run cleanup."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    config = _test_config(tmp_path)
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")
    replace_edit_regenerator_deps(bot)
    bot.logger = MagicMock()
    _record_handled_turn(
        bot._turn_store,
        ["$original:example.com"],
        response_event_id="$response:example.com",
        response_owner="test_agent",
    )

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* updated original",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "updated original",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* updated original",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "updated original",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$original:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
    }
    cleanup_called = False

    async def generate_response_with_locked_cleanup(*_args: object, **kwargs: object) -> str:
        nonlocal cleanup_called
        locked_cleanup = kwargs["on_lifecycle_lock_acquired"]
        assert locked_cleanup is not None
        locked_cleanup()
        cleanup_called = True
        return "$response:example.com"

    with (
        patch.object(bot._conversation_resolver, "extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(
            bot._turn_store,
            "_remove_stale_runs_for_edited_message",
        ) as mock_fallback_cleanup,
        patch.object(
            bot._turn_store,
            "_remove_stale_runs_for_turn_record",
        ) as mock_recorded_cleanup,
        patch.object(
            bot,
            "_generate_response",
            new_callable=AsyncMock,
            side_effect=generate_response_with_locked_cleanup,
        ) as mock_generate_response,
    ):
        mock_context.return_value = MagicMock(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )

        await bot._edit_regenerator.handle_message_edit(
            room,
            edit_event,
            EventInfo.from_event(edit_event.source),
            requester_user_id=edit_event.sender,
        )

    mock_fallback_cleanup.assert_called_once()
    fallback_request = mock_fallback_cleanup.call_args.args[0]
    assert fallback_request.room == room
    assert fallback_request.thread_id is None
    assert fallback_request.original_event_id == "$original:example.com"
    assert fallback_request.requester_user_id == edit_event.sender
    assert cleanup_called is True
    mock_recorded_cleanup.assert_not_called()
    mock_generate_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_message_edit_recovers_missing_ledger_row_from_persisted_run_metadata(
    tmp_path: Path,
) -> None:
    """Edit regeneration should recover when run metadata exists but the handled-turn ledger row is missing."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    config = _test_config(tmp_path)
    config.agents["test_agent"].thread_mode = "room"

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")
    bot.client.room_send.return_value = _room_send_response("$thinking:example.com")
    replace_edit_regenerator_deps(bot)
    bot.logger = MagicMock()

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* updated first",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "updated first",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$first:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* updated first",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "updated first",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$first:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
    }

    session_id = create_session_id("!test:example.com", None)
    storage = _FakeAgentStorage(session=None)

    async def process_and_respond(*_args: object, **kwargs: object) -> DeliveryResult:
        storage.session = AgentSession(
            session_id=session_id,
            runs=[
                RunOutput(
                    run_id=kwargs["run_id"],
                    session_id=session_id,
                    metadata={
                        "matrix_event_id": "$primary:example.com",
                        "matrix_source_event_ids": ["$first:example.com", "$primary:example.com"],
                        "matrix_source_event_prompts": {
                            "$first:example.com": "first",
                            "$primary:example.com": "primary",
                        },
                    },
                ),
            ],
        )
        return DeliveryResult(
            event_id="$response:example.com",
            response_text="ok",
            delivery_kind="sent",
        )

    with (
        patch.object(bot._conversation_state_writer, "create_storage", return_value=storage),
        patch(
            "mindroom.response_runner.ResponseRunner.process_and_respond",
            new=AsyncMock(side_effect=process_and_respond),
        ),
        patch("mindroom.response_runner.reprioritize_auto_flush_sessions"),
        patch("mindroom.response_runner.mark_auto_flush_dirty_session"),
        patch.object(Config, "get_agent_memory_backend", return_value="none"),
        patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=False),
        ),
    ):
        response_event_id = await bot._generate_response(
            room_id="!test:example.com",
            prompt="primary",
            reply_to_event_id="$primary:example.com",
            thread_id=None,
            thread_history=[],
            user_id="@user:example.com",
            matrix_run_metadata={
                "matrix_source_event_ids": ["$first:example.com", "$primary:example.com"],
                "matrix_source_event_prompts": {
                    "$first:example.com": "first",
                    "$primary:example.com": "primary",
                },
            },
        )

    assert response_event_id == "$response:example.com"
    assert storage.upserted_session is not None
    persisted_metadata = storage.upserted_session.runs[0].metadata
    assert persisted_metadata is not None
    assert persisted_metadata["matrix_response_event_id"] == "$response:example.com"

    with (
        patch.object(bot._conversation_resolver, "extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(
            bot._conversation_state_writer,
            "create_storage",
            return_value=storage,
        ),
        patch("mindroom.turn_store.remove_run_by_event_id", return_value=True),
        patch.object(bot, "_generate_response", new_callable=AsyncMock, return_value=None) as mock_generate_response,
    ):
        mock_context.return_value = MagicMock(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )

        await bot._edit_regenerator.handle_message_edit(
            room,
            edit_event,
            EventInfo.from_event(edit_event.source),
            requester_user_id=edit_event.sender,
        )

        mock_generate_response.assert_awaited_once()
        call_kwargs = mock_generate_response.call_args.kwargs
        assert call_kwargs["existing_event_id"] == "$response:example.com"
        assert call_kwargs["reply_to_event_id"] == "$primary:example.com"
        assert _response_event_id(bot, "$first:example.com") == "$response:example.com"
        assert _response_event_id(bot, "$primary:example.com") == "$response:example.com"
        turn_record = bot._turn_store.get_turn_record("$primary:example.com")
        assert turn_record is not None
        assert turn_record.source_event_prompts == {
            "$first:example.com": "updated first",
            "$primary:example.com": "primary",
        }


@pytest.mark.asyncio
async def test_handle_message_edit_recovers_threaded_turn_using_resolved_context_before_turn_lookup(
    tmp_path: Path,
) -> None:
    """Edit recovery should use resolved thread context before probing persisted turn state."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )
    config = _test_config(tmp_path)
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")
    replace_edit_regenerator_deps(bot)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* updated threaded question",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "updated threaded question",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$first:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* updated threaded question",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "updated threaded question",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$first:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
    }

    threaded_session_id = create_session_id("!test:example.com", "$thread_root:example.com")
    threaded_storage = _FakeAgentStorage(
        AgentSession(
            session_id=threaded_session_id,
            runs=[
                RunOutput(
                    run_id="run-threaded",
                    session_id=threaded_session_id,
                    metadata={
                        "matrix_event_id": "$primary:example.com",
                        "matrix_source_event_ids": ["$first:example.com", "$primary:example.com"],
                        "matrix_source_event_prompts": {
                            "$first:example.com": "first",
                            "$primary:example.com": "primary",
                        },
                        "matrix_response_event_id": "$response:example.com",
                    },
                ),
            ],
        ),
    )
    room_storage = _FakeAgentStorage(session=None)

    with (
        patch.object(
            bot._conversation_resolver,
            "extract_message_context",
            new=AsyncMock(
                return_value=MagicMock(
                    am_i_mentioned=False,
                    is_thread=True,
                    thread_id="$thread_root:example.com",
                    thread_history=[],
                    mentioned_agents=[],
                    has_non_agent_mentions=False,
                ),
            ),
        ),
        patch.object(
            bot._conversation_state_writer,
            "create_storage",
            side_effect=[threaded_storage, room_storage],
        ),
        patch("mindroom.turn_store.remove_run_by_event_id", return_value=True),
        patch.object(bot, "_generate_response", new_callable=AsyncMock, return_value=None) as mock_generate_response,
    ):
        await bot._edit_regenerator.handle_message_edit(
            room,
            edit_event,
            EventInfo.from_event(edit_event.source),
            requester_user_id=edit_event.sender,
        )

    mock_generate_response.assert_awaited_once()
    call_kwargs = mock_generate_response.call_args.kwargs
    assert call_kwargs["thread_id"] == "$thread_root:example.com"
    assert call_kwargs["existing_event_id"] == "$response:example.com"
    assert call_kwargs["reply_to_event_id"] == "$primary:example.com"


@pytest.mark.asyncio
async def test_handle_message_edit_recovers_missing_single_turn_without_rerunning_response_gating(
    tmp_path: Path,
) -> None:
    """Persisted single-turn recovery should not re-run should-respond heuristics."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    config = _test_config(tmp_path)
    config.agents["test_agent"].thread_mode = "room"

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")
    replace_edit_regenerator_deps(bot)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    wrap_extracted_collaborators(bot, "_delivery_gateway", "_response_runner")
    replace_turn_controller_deps(
        bot,
        delivery_gateway=bot._delivery_gateway,
        response_runner=bot._response_runner,
    )

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* updated question",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "updated question",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* updated question",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "updated question",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$original:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
    }

    session_id = create_session_id("!test:example.com", None)
    storage = MagicMock()
    storage.get_session.return_value = AgentSession(
        session_id=session_id,
        runs=[
            RunOutput(
                session_id=session_id,
                metadata={
                    "matrix_event_id": "$original:example.com",
                    "matrix_source_event_ids": ["$original:example.com"],
                    "matrix_source_event_prompts": {
                        "$original:example.com": "original question",
                    },
                    "matrix_response_event_id": "$response:example.com",
                },
            ),
        ],
    )

    with (
        patch.object(bot._conversation_resolver, "extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(
            bot._conversation_state_writer,
            "create_storage",
            return_value=storage,
        ),
        patch("mindroom.turn_store.remove_run_by_event_id", return_value=True),
        patch.object(
            bot,
            "_generate_response",
            new_callable=AsyncMock,
            side_effect=_generate_response_with_locked_callback("$response:example.com"),
        ) as mock_generate_response,
    ):
        mock_context.return_value = MagicMock(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )

        await bot._edit_regenerator.handle_message_edit(
            room,
            edit_event,
            EventInfo.from_event(edit_event.source),
            requester_user_id=edit_event.sender,
        )

    mock_generate_response.assert_awaited_once()
    call_kwargs = mock_generate_response.call_args.kwargs
    assert call_kwargs["existing_event_id"] == "$response:example.com"
    assert call_kwargs["reply_to_event_id"] == "$original:example.com"
    assert call_kwargs["prompt"] == "updated question"
    assert _response_event_id(bot, "$original:example.com") == "$response:example.com"


@pytest.mark.asyncio
async def test_handle_message_edit_prefers_persisted_response_event_id_after_restart(
    tmp_path: Path,
) -> None:
    """A fresh bot should prefer the newest persisted response linkage over a stale ledger row."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )
    config = _test_config(tmp_path)
    config.agents["test_agent"].thread_mode = "room"
    session_id = create_session_id("!test:example.com", None)

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")
    bot.client.room_send.return_value = _room_send_response("$thinking:example.com")
    replace_edit_regenerator_deps(bot)
    bot.logger = MagicMock()
    stored_target = MessageTarget.resolve(
        room_id="!test:example.com",
        thread_id=None,
        reply_to_event_id="$original:example.com",
        room_mode=True,
    )
    assert stored_target.session_id == "!test:example.com"
    _record_handled_turn(
        bot._turn_store,
        ["$original:example.com"],
        response_event_id="$response-old:example.com",
        response_owner="test_agent",
        history_scope=_agent_history_scope("test_agent"),
        conversation_target=stored_target,
    )

    async def process_and_respond(*_args: object, **kwargs: object) -> DeliveryResult:
        storage = bot._conversation_state_writer.create_storage(None)
        try:
            storage.upsert_session(
                AgentSession(
                    session_id=session_id,
                    agent_id="test_agent",
                    created_at=1,
                    updated_at=1,
                    runs=[
                        RunOutput(
                            run_id=kwargs["run_id"],
                            agent_id="test_agent",
                            agent_name="Test Agent",
                            session_id=session_id,
                            content="ok",
                            metadata={
                                "matrix_event_id": "$original:example.com",
                                "matrix_source_event_ids": ["$original:example.com"],
                                "matrix_source_event_prompts": {
                                    "$original:example.com": "original",
                                },
                            },
                        ),
                    ],
                ),
            )
        finally:
            storage.close()
        return DeliveryResult(
            event_id="$response-new:example.com",
            response_text="ok",
            delivery_kind="sent",
        )

    with (
        patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
        patch(
            "mindroom.response_runner.ResponseRunner.process_and_respond",
            new=AsyncMock(side_effect=process_and_respond),
        ),
        patch("mindroom.response_runner.reprioritize_auto_flush_sessions"),
        patch("mindroom.response_runner.mark_auto_flush_dirty_session"),
        patch.object(Config, "get_agent_memory_backend", return_value="none"),
    ):
        response_event_id = await bot._generate_response(
            room_id="!test:example.com",
            prompt="original",
            reply_to_event_id="$original:example.com",
            thread_id=None,
            thread_history=[],
            user_id="@user:example.com",
            matrix_run_metadata={
                "matrix_source_event_ids": ["$original:example.com"],
                "matrix_source_event_prompts": {
                    "$original:example.com": "original",
                },
            },
        )

    assert response_event_id == "$response-new:example.com"
    storage = bot._conversation_state_writer.create_storage(None)
    try:
        persisted_session = get_agent_session(storage, session_id)
    finally:
        storage.close()
    assert persisted_session is not None
    assert persisted_session.runs is not None
    assert persisted_session.runs[0].metadata["matrix_response_event_id"] == "$response-new:example.com"

    restarted_bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    restarted_bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")
    replace_edit_regenerator_deps(restarted_bot)
    restarted_bot.logger = MagicMock()
    assert _response_event_id(restarted_bot, "$original:example.com") == "$response-old:example.com"

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* updated original",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "updated original",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* updated original",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "updated original",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$original:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
    }

    with (
        patch.object(
            restarted_bot._conversation_resolver,
            "extract_message_context",
            new_callable=AsyncMock,
        ) as mock_context,
        patch("mindroom.turn_store.remove_run_by_event_id", return_value=True),
        patch.object(
            restarted_bot,
            "_generate_response",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_generate_response,
    ):
        mock_context.return_value = MagicMock(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )

        await restarted_bot._edit_regenerator.handle_message_edit(
            room,
            edit_event,
            EventInfo.from_event(edit_event.source),
            requester_user_id=edit_event.sender,
        )

    mock_generate_response.assert_awaited_once()
    call_kwargs = mock_generate_response.call_args.kwargs
    assert call_kwargs["existing_event_id"] == "$response-new:example.com"
    assert call_kwargs["target"].session_id == "!test:example.com"
    assert _response_event_id(restarted_bot, "$original:example.com") == "$response-new:example.com"


@pytest.mark.asyncio
async def test_on_reaction_tracks_response_event_id(tmp_path: Path) -> None:
    """Test that _on_reaction properly tracks the response event ID."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    config = _test_config(tmp_path)

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )

    # Mock the client
    bot.client = make_matrix_client_mock(user_id="@test_agent:example.com")

    replace_edit_regenerator_deps(bot)

    # Mock logger
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    wrap_extracted_collaborators(bot, "_delivery_gateway", "_response_runner")
    replace_turn_controller_deps(
        bot,
        delivery_gateway=bot._delivery_gateway,
        response_runner=bot._response_runner,
    )

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Create a reaction event
    reaction_event = nio.ReactionEvent.from_dict(
        {
            "content": {
                "m.relates_to": {
                    "event_id": "$question:example.com",
                    "key": "1️⃣",
                    "rel_type": "m.annotation",
                },
            },
            "event_id": "$reaction:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.reaction",
            "room_id": "!test:example.com",
        },
    )
    reaction_event.reacts_to = "$question:example.com"
    reaction_event.key = "1️⃣"

    # Mock interactive.handle_reaction to return a result
    with (
        patch("mindroom.bot.interactive.handle_reaction", new_callable=AsyncMock) as mock_handle_reaction,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(bot._delivery_gateway, "send_text", new_callable=AsyncMock) as mock_send_text,
        patch.object(bot._response_runner, "generate_response", new_callable=AsyncMock) as mock_generate_response,
        patch.object(bot._conversation_resolver, "fetch_thread_history", new_callable=AsyncMock) as mock_fetch_history,
    ):
        # Setup mocks
        mock_handle_reaction.return_value = interactive.InteractiveSelection(
            question_event_id="$question:example.com",
            selection_key="1️⃣",
            selected_value="Option 1",
            thread_id="thread_id",
        )
        mock_send_text.return_value = "$ack_event:example.com"
        mock_generate_response.return_value = "$response_event:example.com"
        mock_fetch_history.return_value = []

        # Process the reaction event
        await bot._on_reaction(room, reaction_event)

        # Verify that the bot tracked the response correctly
        assert bot._turn_store.is_handled("$question:example.com")
        assert _response_event_id(bot, "$question:example.com") == "$response_event:example.com"

        # Verify the methods were called with correct parameters
        mock_handle_reaction.assert_called_once()
        mock_send_text.assert_called_once()
        mock_generate_response.assert_called_once()

        request = mock_generate_response.await_args.args[0]
        assert request.existing_event_id == "$ack_event:example.com"
        assert request.existing_event_is_placeholder is True
        assert request.reply_to_event_id == "$question:example.com"
        assert request.thread_id == "thread_id"


@pytest.mark.asyncio
async def test_on_reaction_leaves_question_retryable_when_ack_response_is_suppressed(tmp_path: Path) -> None:
    """A suppressed interactive response must not mark the original question completed."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    config = _test_config(tmp_path)

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = make_matrix_client_mock(user_id="@test_agent:example.com")
    replace_edit_regenerator_deps(bot)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    wrap_extracted_collaborators(bot, "_delivery_gateway", "_response_runner")
    replace_turn_controller_deps(
        bot,
        delivery_gateway=bot._delivery_gateway,
        response_runner=bot._response_runner,
    )

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    reaction_event = nio.ReactionEvent.from_dict(
        {
            "content": {
                "m.relates_to": {
                    "event_id": "$question:example.com",
                    "key": "1️⃣",
                    "rel_type": "m.annotation",
                },
            },
            "event_id": "$reaction:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.reaction",
            "room_id": "!test:example.com",
        },
    )
    reaction_event.reacts_to = "$question:example.com"
    reaction_event.key = "1️⃣"

    with (
        patch("mindroom.bot.interactive.handle_reaction", new_callable=AsyncMock) as mock_handle_reaction,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(bot._delivery_gateway, "send_text", new_callable=AsyncMock) as mock_send_text,
        patch.object(bot._response_runner, "generate_response", new_callable=AsyncMock) as mock_generate_response,
        patch.object(bot._conversation_resolver, "fetch_thread_history", new_callable=AsyncMock) as mock_fetch_history,
    ):
        mock_handle_reaction.return_value = interactive.InteractiveSelection(
            question_event_id="$question:example.com",
            selection_key="1️⃣",
            selected_value="Option 1",
            thread_id="thread_id",
        )
        mock_send_text.return_value = "$ack_event:example.com"
        mock_generate_response.return_value = None
        mock_fetch_history.return_value = []

        await bot._on_reaction(room, reaction_event)

        assert bot._turn_store.is_handled("$question:example.com") is False
        assert _response_event_id(bot, "$question:example.com") is None
        request = mock_generate_response.await_args.args[0]
        assert request.existing_event_id == "$ack_event:example.com"
        assert request.existing_event_is_placeholder is True


@pytest.mark.asyncio
async def test_on_message_routes_interactive_text_selection_through_turn_controller(tmp_path: Path) -> None:
    """Numeric interactive replies should run the controller-owned selection workflow."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    config = _test_config(tmp_path)
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = make_matrix_client_mock(user_id="@test_agent:example.com")
    install_runtime_cache_support(bot)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    wrap_extracted_collaborators(bot, "_delivery_gateway", "_response_runner", "_turn_policy")
    replace_turn_controller_deps(
        bot,
        delivery_gateway=bot._delivery_gateway,
        response_runner=bot._response_runner,
        turn_policy=bot._turn_policy,
    )

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")
    message_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "1",
                "msgtype": "m.text",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread:example.com",
                },
            },
            "event_id": "$selection:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    message_event.source = {
        "content": {
            "body": "1",
            "msgtype": "m.text",
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$thread:example.com",
            },
        },
        "event_id": "$selection:example.com",
        "sender": "@user:example.com",
        "room_id": "!test:example.com",
        "origin_server_ts": 1000000,
        "type": "m.room.message",
    }

    with (
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        patch.object(bot._turn_policy, "can_reply_to_sender", return_value=True),
        patch(
            "mindroom.turn_controller.interactive.handle_text_response",
            new_callable=AsyncMock,
            return_value=interactive.InteractiveSelection(
                question_event_id="$question:example.com",
                selection_key="1",
                selected_value="Option 1",
                thread_id="$thread:example.com",
            ),
        ) as mock_handle_text_response,
        patch.object(bot._delivery_gateway, "send_text", new_callable=AsyncMock, return_value="$ack:example.com"),
        patch.object(
            bot._response_runner,
            "generate_response",
            new_callable=AsyncMock,
            return_value="$response:example.com",
        ) as mock_generate_response,
        patch.object(bot._conversation_resolver, "fetch_thread_history", new_callable=AsyncMock, return_value=[]),
        patch.object(bot._turn_controller, "_dispatch_text_message", new_callable=AsyncMock) as mock_dispatch_text,
    ):
        await bot._on_message(room, message_event)

    mock_handle_text_response.assert_awaited_once()
    mock_dispatch_text.assert_not_awaited()
    request = mock_generate_response.await_args.args[0]
    assert request.reply_to_event_id == "$question:example.com"
    assert request.thread_id == "$thread:example.com"
    assert request.existing_event_id == "$ack:example.com"
    assert request.matrix_run_metadata == {
        MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$selection:example.com"],
    }
    assert bot._turn_store.is_handled("$question:example.com")
    assert _response_event_id(bot, "$question:example.com") == "$response:example.com"
    assert bot._turn_store.is_handled("$selection:example.com")
    assert _response_event_id(bot, "$selection:example.com") == "$response:example.com"


@pytest.mark.asyncio
async def test_on_reaction_respects_agent_reply_permissions(tmp_path: Path) -> None:
    """Disallowed reactions must not consume interactive questions."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    config = _bind_runtime_paths(
        Config(
            agents={
                "test_agent": {
                    "display_name": "Test Agent",
                    "rooms": ["!test:example.com"],
                },
            },
            authorization={
                "default_room_access": True,
                "agent_reply_permissions": {"test_agent": ["@alice:example.com"]},
            },
        ),
        tmp_path,
    )

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")
    replace_edit_regenerator_deps(bot)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    wrap_extracted_collaborators(bot, "_delivery_gateway", "_response_runner")
    replace_turn_controller_deps(
        bot,
        delivery_gateway=bot._delivery_gateway,
        response_runner=bot._response_runner,
    )

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    interactive._active_questions.clear()
    interactive.register_interactive_question(
        event_id="$question:example.com",
        room_id=room.room_id,
        thread_id=None,
        option_map={"1️⃣": "Option 1", "1": "Option 1"},
        agent_name="test_agent",
    )

    disallowed_reaction = nio.ReactionEvent.from_dict(
        {
            "content": {
                "m.relates_to": {
                    "event_id": "$question:example.com",
                    "key": "1️⃣",
                    "rel_type": "m.annotation",
                },
            },
            "event_id": "$reaction_bob:example.com",
            "sender": "@bob:example.com",
            "origin_server_ts": 1000000,
            "type": "m.reaction",
            "room_id": "!test:example.com",
        },
    )
    disallowed_reaction.reacts_to = "$question:example.com"
    disallowed_reaction.key = "1️⃣"

    allowed_reaction = nio.ReactionEvent.from_dict(
        {
            "content": {
                "m.relates_to": {
                    "event_id": "$question:example.com",
                    "key": "1️⃣",
                    "rel_type": "m.annotation",
                },
            },
            "event_id": "$reaction_alice:example.com",
            "sender": "@alice:example.com",
            "origin_server_ts": 1000000,
            "type": "m.reaction",
            "room_id": "!test:example.com",
        },
    )
    allowed_reaction.reacts_to = "$question:example.com"
    allowed_reaction.key = "1️⃣"

    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch("mindroom.bot.config_confirmation.get_pending_change", return_value=None),
        patch.object(bot._delivery_gateway, "send_text", new_callable=AsyncMock) as mock_send_text,
        patch.object(bot._response_runner, "generate_response", new_callable=AsyncMock) as mock_generate_response,
    ):
        mock_send_text.return_value = "$ack_event:example.com"
        mock_generate_response.return_value = "$response_event:example.com"

        await bot._on_reaction(room, disallowed_reaction)
        mock_send_text.assert_not_called()
        mock_generate_response.assert_not_called()

        await bot._on_reaction(room, allowed_reaction)

    interactive._active_questions.clear()

    mock_send_text.assert_called_once()
    mock_generate_response.assert_called_once()


@pytest.mark.asyncio
async def test_config_confirmation_blocked_by_reply_permissions(tmp_path: Path) -> None:
    """Disallowed senders must not trigger config confirmation reactions."""
    agent_user = AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id=f"@mindroom_{ROUTER_AGENT_NAME}:example.com",
        display_name="Router",
        password="test_password",  # noqa: S106
    )

    config = _bind_runtime_paths(
        Config(
            agents={
                "assistant": {
                    "display_name": "Assistant",
                    "rooms": ["!test:example.com"],
                },
            },
            authorization={
                "default_room_access": True,
                "agent_reply_permissions": {ROUTER_AGENT_NAME: ["@alice:example.com"]},
            },
        ),
        tmp_path,
    )

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = make_matrix_client_mock(user_id=f"@mindroom_{ROUTER_AGENT_NAME}:example.com")
    replace_edit_regenerator_deps(bot)
    bot.logger = MagicMock()

    room = nio.MatrixRoom(
        room_id="!test:example.com",
        own_user_id=f"@mindroom_{ROUTER_AGENT_NAME}:example.com",
    )

    # Register a pending config change
    config_confirmation._pending_changes["$config_msg:example.com"] = config_confirmation._PendingConfigChange(
        requester="@bob:example.com",
        room_id=room.room_id,
        thread_id=None,
        config_path="agents.assistant.role",
        old_value="old",
        new_value="new",
    )

    reaction_event = nio.ReactionEvent.from_dict(
        {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$config_msg:example.com",
                    "key": "✅",
                },
            },
            "event_id": "$reaction_bob:example.com",
            "sender": "@bob:example.com",
            "origin_server_ts": 1000000,
            "type": "m.reaction",
            "room_id": "!test:example.com",
        },
    )

    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch("mindroom.bot.config_confirmation.handle_confirmation_reaction", new_callable=AsyncMock) as mock_confirm,
    ):
        await bot._on_reaction(room, reaction_event)

    config_confirmation._pending_changes.clear()

    # Bob is disallowed for the router — the confirmation handler must not run.
    mock_confirm.assert_not_called()


@pytest.mark.asyncio
async def test_on_media_message_tracks_relay_event_id(tmp_path: Path) -> None:
    """Audio normalization should track the relay event ID."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    config = _test_config(tmp_path, voice_enabled=True)

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )

    # Mock the client
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")

    replace_edit_regenerator_deps(bot)

    # Mock logger
    bot.logger = MagicMock()

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    room.users = {
        "@mindroom_test_agent:example.com": None,
        "@user:example.com": None,
    }

    # Create a voice message event
    voice_event = nio.RoomMessageAudio.from_dict(
        {
            "content": {
                "body": "voice_message.ogg",
                "msgtype": "m.audio",
                "url": "mxc://example.com/voice123",
                "org.matrix.msc1767.audio": {
                    "duration": 5000,
                    "waveform": [0, 100, 200],
                },
                "org.matrix.msc3245.voice": {},
            },
            "event_id": "$voice:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    voice_event.source = {
        "content": {
            "body": "voice_message.ogg",
            "msgtype": "m.audio",
            "url": "mxc://example.com/voice123",
            "org.matrix.msc1767.audio": {
                "duration": 5000,
                "waveform": [0, 100, 200],
            },
            "org.matrix.msc3245.voice": {},
        },
        "event_id": "$voice:example.com",
        "sender": "@user:example.com",
    }

    # Mock voice_handler._handle_voice_message to return a transcription
    mock_generate_response = AsyncMock(return_value="$response:example.com")
    install_generate_response_mock(bot, mock_generate_response)
    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_handle_voice,
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        # Setup mocks
        bot._conversation_cache.get_thread_history = AsyncMock(return_value=[])
        bot._conversation_cache.get_thread_snapshot = AsyncMock(
            return_value=thread_history_result([], is_full_history=False),
        )
        bot._conversation_cache.get_dispatch_thread_history = AsyncMock(return_value=[])
        bot._conversation_cache.get_dispatch_thread_snapshot = AsyncMock(
            return_value=thread_history_result([], is_full_history=False),
        )
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_handle_voice.return_value = "This is the transcribed message from voice"

        # Process the voice event
        await bot._on_media_message(room, voice_event)

        # Verify that the bot tracked the response correctly
        assert bot._turn_store.is_handled("$voice:example.com")
        assert _response_event_id(bot, "$voice:example.com") == "$response:example.com"

        # Verify the methods were called
        mock_handle_voice.assert_called_once()
        assert mock_handle_voice.call_args.args == (
            bot.client,
            room,
            voice_event,
            config,
            runtime_paths_for(config),
        )
        mock_generate_response.assert_called_once()


@pytest.mark.asyncio
async def test_on_media_message_no_transcription_still_marks_relayed(tmp_path: Path) -> None:
    """Audio normalization should still emit a fallback relay when transcription fails."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    config = _test_config(tmp_path, voice_enabled=True)

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )

    # Mock the client
    bot.client = make_matrix_client_mock(user_id="@mindroom_test_agent:example.com")

    replace_edit_regenerator_deps(bot)

    # Mock logger
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    room.users = {
        "@mindroom_test_agent:example.com": None,
        "@user:example.com": None,
    }

    # Create a voice message event
    voice_event = nio.RoomMessageAudio.from_dict(
        {
            "content": {
                "body": "voice_message.ogg",
                "msgtype": "m.audio",
                "url": "mxc://example.com/voice123",
                "org.matrix.msc1767.audio": {
                    "duration": 5000,
                    "waveform": [0, 100, 200],
                },
                "org.matrix.msc3245.voice": {},
            },
            "event_id": "$voice:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    voice_event.source = {
        "content": {
            "body": "voice_message.ogg",
            "msgtype": "m.audio",
            "url": "mxc://example.com/voice123",
            "org.matrix.msc1767.audio": {
                "duration": 5000,
                "waveform": [0, 100, 200],
            },
            "org.matrix.msc3245.voice": {},
        },
        "event_id": "$voice:example.com",
        "sender": "@user:example.com",
    }

    # Mock voice_handler._handle_voice_message to return None (no transcription)
    mock_generate_response = AsyncMock(return_value="$response:example.com")
    install_generate_response_mock(bot, mock_generate_response)
    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_handle_voice,
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        # Setup mocks
        bot._conversation_cache.get_thread_history = AsyncMock(return_value=[])
        bot._conversation_cache.get_thread_snapshot = AsyncMock(
            return_value=thread_history_result([], is_full_history=False),
        )
        bot._conversation_cache.get_dispatch_thread_history = AsyncMock(return_value=[])
        bot._conversation_cache.get_dispatch_thread_snapshot = AsyncMock(
            return_value=thread_history_result([], is_full_history=False),
        )
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_handle_voice.return_value = None  # No transcription

        # Process the voice event
        await bot._on_media_message(room, voice_event)

        # Verify that the bot marked as responded with the fallback relay.
        assert bot._turn_store.is_handled("$voice:example.com")
        assert _response_event_id(bot, "$voice:example.com") == "$response:example.com"

        # Verify voice handler was called and the fallback relay ran.
        mock_handle_voice.assert_called_once()
        assert mock_handle_voice.call_args.args == (
            bot.client,
            room,
            voice_event,
            config,
            runtime_paths_for(config),
        )
        mock_generate_response.assert_called_once()


@pytest.mark.asyncio
async def test_unauthorized_user_cannot_edit_regenerate(tmp_path: Path) -> None:
    """Test that unauthorized users cannot trigger response regeneration through edits."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    # Create a minimal mock config with authorization
    config = _bind_runtime_paths(
        Config(
            agents={"test_agent": {"display_name": "Test Agent", "role": "Test agent", "rooms": ["!test:example.com"]}},
            authorization={
                "global_users": ["@authorized:example.com"],
                "room_permissions": {},
                "default_room_access": False,
            },
        ),
        tmp_path,
    )

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )

    # Mock the client
    bot.client = make_matrix_client_mock(user_id="@test_agent:example.com")

    replace_edit_regenerator_deps(bot)

    # Mock logger
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)

    room = Mock(spec=nio.MatrixRoom)
    room.room_id = "!test:example.com"
    room.canonical_alias = None
    room.is_direct = False

    # Original message from authorized user
    original_event = Mock(spec=nio.RoomMessageText)
    original_event.event_id = "$original:example.com"
    original_event.sender = "@authorized:example.com"
    original_event.body = "Original question"
    original_event.source = {"event_id": "$original:example.com"}

    # Store that we responded to the original
    _record_handled_turn(bot._turn_store, ["$original:example.com"], response_event_id="$response:example.com")

    # Edit from unauthorized user (trying to regenerate)
    edit_event = Mock(spec=nio.RoomMessageText)
    edit_event.event_id = "$edit:example.com"
    edit_event.sender = "@unauthorized:example.com"
    edit_event.body = "Edited question"
    edit_event.source = {
        "event_id": "$edit:example.com",
        "content": {
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": "$original:example.com",
            },
        },
    }

    # Test that authorization check works
    with (
        patch("mindroom.turn_controller.is_authorized_sender", return_value=False) as mock_is_auth,
        patch.object(bot._edit_regenerator, "handle_message_edit") as mock_handle_edit,
    ):
        await bot._on_message(room, edit_event)
        # Verify authorization was checked
        mock_is_auth.assert_called_once_with(
            edit_event.sender,
            config,
            room.room_id,
            runtime_paths_for(config),
            room_alias=None,
        )
        # Should not handle edit for unauthorized user
        mock_handle_edit.assert_not_called()


@pytest.mark.asyncio
async def test_on_media_message_unauthorized_sender_marks_responded(tmp_path: Path) -> None:
    """Test that _on_media_message marks as responded for unauthorized senders."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    config = _test_config(tmp_path, voice_enabled=True)

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )

    # Mock the client
    bot.client = make_matrix_client_mock(user_id="@test_agent:example.com")

    replace_edit_regenerator_deps(bot)

    # Mock logger
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Create a voice message event from unauthorized sender
    voice_event = nio.RoomMessageAudio.from_dict(
        {
            "content": {
                "body": "voice_message.ogg",
                "msgtype": "m.audio",
                "url": "mxc://example.com/voice123",
                "org.matrix.msc1767.audio": {
                    "duration": 5000,
                    "waveform": [0, 100, 200],
                },
                "org.matrix.msc3245.voice": {},
            },
            "event_id": "$voice:example.com",
            "sender": "@unauthorized:example.com",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    voice_event.source = {
        "content": {
            "body": "voice_message.ogg",
            "msgtype": "m.audio",
            "url": "mxc://example.com/voice123",
            "org.matrix.msc1767.audio": {
                "duration": 5000,
                "waveform": [0, 100, 200],
            },
            "org.matrix.msc3245.voice": {},
        },
        "event_id": "$voice:example.com",
        "sender": "@unauthorized:example.com",
    }

    # Mock is_authorized_sender to return False
    with (
        patch("mindroom.turn_controller.is_authorized_sender", return_value=False) as mock_is_authorized,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_handle_voice,
    ):
        # Process the voice event
        await bot._on_media_message(room, voice_event)

        # Verify that the bot marked as responded even for unauthorized sender
        assert bot._turn_store.is_handled("$voice:example.com")
        # Should not have a response event ID since no response was sent
        assert _response_event_id(bot, "$voice:example.com") is None

        # Verify authorization was checked but voice handler was not called
        mock_is_authorized.assert_called_once()
        mock_handle_voice.assert_not_called()
