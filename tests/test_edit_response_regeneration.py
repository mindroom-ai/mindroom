"""Test that agent responses are regenerated when user edits their message."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import nio
import pytest
from agno.db.base import SessionType
from agno.media import Audio
from agno.run.team import TeamRunOutput
from agno.session.team import TeamSession

from mindroom import interactive
from mindroom.agents import remove_run_by_event_id
from mindroom.bot import AgentBot, TeamBot, _PersistedTurnMetadata
from mindroom.commands import config_confirmation
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, resolve_runtime_paths
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.message_content import _clear_mxc_cache
from mindroom.matrix.users import AgentMatrixUser
from mindroom.response_tracker import ResponseTracker
from mindroom.thread_utils import create_session_id
from tests.conftest import bind_runtime_paths, runtime_paths_for


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
        authorization={"default_room_access": True, "agent_reply_permissions": {}},
        mindroom_user={"username": "mindroom", "display_name": "MindRoom"},
    )
    return _bind_runtime_paths(config, tmp_path)


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
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_test_agent:example.com"

    # Create real ResponseTracker with the test path
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

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
    bot.response_tracker.mark_responded(original_event.event_id, response_event_id)

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
    with (
        patch.object(bot, "_extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(bot, "_edit_message", new_callable=AsyncMock) as mock_edit,
        patch("mindroom.bot.should_agent_respond") as mock_should_respond,
        patch("mindroom.bot.should_use_streaming", new_callable=AsyncMock) as mock_streaming,
        patch("mindroom.bot.ai_response", new_callable=AsyncMock) as mock_ai_response,
    ):
        # Setup mocks
        mock_context.return_value = MagicMock(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[MatrixID.from_agent("test_agent", "example.com", runtime_paths_for(config))],
        )
        mock_should_respond.return_value = True
        mock_streaming.return_value = False  # Use non-streaming for simpler test
        mock_ai_response.return_value = "The answer is 6"

        # Process the edit event
        await bot._on_message(room, edit_event)

        # Verify that the bot attempted to regenerate the response
        mock_context.assert_called_once()
        mock_should_respond.assert_called_once()
        mock_ai_response.assert_called_once()

        # Verify that the bot edited the existing response message
        mock_edit.assert_called_once_with(
            room.room_id,
            response_event_id,
            "The answer is 6",
            None,  # thread_id
            tool_trace=[],
            extra_content={},
        )

        # Verify that the response tracker still maps to the same response
        assert bot.response_tracker.get_response_event_id(original_event.event_id) == response_event_id


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
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_test_agent:example.com"
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
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)
    bot.logger = MagicMock()

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    bot.response_tracker.mark_responded("$original:example.com", "$response:example.com")

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
        patch.object(bot, "_extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(bot, "_emit_message_received_hooks", new_callable=AsyncMock) as mock_emit_hooks,
        patch("mindroom.bot.should_agent_respond", return_value=False) as mock_should_respond,
    ):
        mock_context.return_value = MagicMock(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[MatrixID.from_agent("test_agent", "example.com", runtime_paths_for(config))],
            has_non_agent_mentions=False,
        )
        mock_emit_hooks.return_value = False

        await bot._on_message(room, edit_event)

    mock_should_respond.assert_called_once()
    emitted_envelope = mock_emit_hooks.await_args.kwargs["envelope"]
    assert emitted_envelope.body == "@test_agent what is 99+1?"


@pytest.mark.asyncio
async def test_bot_edit_regeneration_uses_hydrated_mentions_for_response_gating(tmp_path: Path) -> None:
    """Edit regeneration should route mention detection through canonical hydrated edit content."""
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
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_test_agent:example.com"
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
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)
    bot.logger = MagicMock()
    bot._derive_conversation_context = AsyncMock(return_value=(False, None, []))

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    bot.response_tracker.mark_responded("$original:example.com", "$response:example.com")

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
        patch.object(bot, "_emit_message_received_hooks", new_callable=AsyncMock) as mock_emit_hooks,
        patch("mindroom.bot.should_agent_respond", return_value=False) as mock_should_respond,
    ):
        mock_emit_hooks.return_value = False

        await bot._on_message(room, edit_event)

    mock_should_respond.assert_called_once()
    assert mock_should_respond.call_args.kwargs["am_i_mentioned"] is True
    assert mock_should_respond.call_args.kwargs["mentioned_agents"] == [
        MatrixID.from_agent("test_agent", "example.com", runtime_paths_for(config)),
    ]


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
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_test_team:example.com"
    bot.response_tracker = ResponseTracker(agent_name="test_team", base_path=tmp_path)
    bot.logger = MagicMock()

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_team:example.com")
    response_event_id = "$response:example.com"
    bot.response_tracker.mark_responded("$original:example.com", response_event_id)
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
    with (
        patch.object(bot, "_extract_message_context", new_callable=AsyncMock) as mock_context,
        patch("mindroom.bot.should_agent_respond", return_value=True),
        patch.object(
            bot,
            "_generate_response",
            new_callable=AsyncMock,
            return_value=response_event_id,
        ) as mock_generate,
        patch.object(bot, "_create_history_scope_storage", return_value=storage),
        patch("mindroom.bot.remove_run_by_event_id", return_value=True) as mock_remove_run,
    ):
        mock_context.return_value = MagicMock(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[MatrixID.from_agent("test_team", "example.com", runtime_paths)],
        )

        await bot._on_message(room, edit_event)

    assert mock_remove_run.call_args_list == [
        call(
            storage,
            create_session_id("!test:example.com", "$original:example.com"),
            "$original:example.com",
            session_type=SessionType.TEAM,
        ),
        call(
            storage,
            create_session_id("!test:example.com", None),
            "$original:example.com",
            session_type=SessionType.TEAM,
        ),
    ]
    mock_generate.assert_awaited_once()


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
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_test_agent:example.com"

    # Create real ResponseTracker with the test path
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

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
        patch.object(bot, "_extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(bot, "_edit_message", new_callable=AsyncMock) as mock_edit,
    ):
        # Process the edit event
        await bot._on_message(room, edit_event)

        # Verify that the bot did NOT attempt to regenerate
        mock_context.assert_not_called()
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
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_test_agent:example.com"

    # Create real ResponseTracker with the test path
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

    # Mock logger
    bot.logger = MagicMock()

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Simulate that the bot has responded to some message
    bot.response_tracker.mark_responded("$original:example.com", "$response:example.com")

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
        patch.object(bot, "_extract_message_context", new_callable=AsyncMock) as mock_context,
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
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_test_agent:example.com"
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)
    bot.response_tracker.mark_responded("$first:example.com", "$response:example.com")
    bot.response_tracker.mark_responded("$primary:example.com", "$response:example.com")
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
        patch.object(bot, "_extract_message_context", new_callable=AsyncMock) as mock_context,
        patch("mindroom.bot.should_agent_respond", return_value=False) as mock_should_respond,
        patch.object(bot, "_create_history_scope_storage"),
        patch("mindroom.bot.remove_run_by_event_id", return_value=True) as mock_remove_run,
        patch.object(
            bot,
            "_load_persisted_turn_metadata",
            return_value=_PersistedTurnMetadata(
                anchor_event_id="$primary:example.com",
                source_event_ids=("$first:example.com", "$primary:example.com"),
                batch_prompt=(
                    "The user sent the following messages in quick succession. "
                    "Treat them as one turn and respond once:\n\nfirst\nprimary"
                ),
                source_event_prompts={
                    "$first:example.com": "first",
                    "$primary:example.com": "primary",
                },
            ),
        ),
        patch.object(
            bot,
            "_generate_response",
            new_callable=AsyncMock,
            return_value="$response:example.com",
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

        await bot._handle_message_edit(
            room,
            edit_event,
            EventInfo.from_event(edit_event.source),
            requester_user_id=edit_event.sender,
        )

        mock_should_respond.assert_called_once()
        mock_generate_response.assert_awaited_once()
        call_kwargs = mock_generate_response.call_args.kwargs
        assert call_kwargs["prompt"] == (
            "The user sent the following messages in quick succession. "
            "Treat them as one turn and respond once:\n\nupdated first\nprimary"
        )
        assert call_kwargs["reply_to_event_id"] == "$primary:example.com"
        assert call_kwargs["matrix_run_metadata"] == {
            "matrix_source_event_ids": ["$first:example.com", "$primary:example.com"],
            "matrix_source_event_prompts": {
                "$first:example.com": "updated first",
                "$primary:example.com": "primary",
            },
            "matrix_batch_prompt": (
                "The user sent the following messages in quick succession. "
                "Treat them as one turn and respond once:\n\nupdated first\nprimary"
            ),
        }
        assert bot.response_tracker.get_response_event_id("$first:example.com") == "$response:example.com"
        assert bot.response_tracker.get_response_event_id("$primary:example.com") == "$response:example.com"
        assert mock_remove_run.call_count == 2


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
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_test_agent:example.com"
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)
    bot.logger = MagicMock()

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")
    bot.response_tracker.mark_responded("$original:example.com", "$response:example.com")

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
        patch.object(bot, "_extract_message_context", new_callable=AsyncMock) as mock_context,
        patch("mindroom.bot.should_agent_respond", return_value=True),
        patch.object(bot, "_create_history_scope_storage"),
        patch("mindroom.bot.remove_run_by_event_id", return_value=False) as mock_remove_run,
        patch.object(
            bot,
            "_generate_response",
            new_callable=AsyncMock,
            return_value="$response:example.com",
        ) as mock_generate_response,
    ):
        mock_context.return_value = MagicMock(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[MatrixID.from_agent("test_agent", "example.com", runtime_paths_for(config))],
            has_non_agent_mentions=False,
        )

        await bot._handle_message_edit(
            room,
            edit_event,
            EventInfo.from_event(edit_event.source),
            requester_user_id=edit_event.sender,
        )

        mock_generate_response.assert_awaited_once()
        call_kwargs = mock_generate_response.call_args.kwargs
        assert call_kwargs["reply_to_event_id"] == "$original:example.com"
        assert call_kwargs["existing_event_id"] == "$response:example.com"
        assert call_kwargs["existing_event_is_placeholder"] is False
        assert bot.response_tracker.get_response_event_id("$original:example.com") == "$response:example.com"
        assert mock_remove_run.call_count == 2


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
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_test_agent:example.com"
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)
    bot.response_tracker.mark_responded("$original:example.com", "$response:example.com")
    bot.response_tracker.mark_responded = MagicMock(wraps=bot.response_tracker.mark_responded)
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
        patch.object(bot, "_extract_message_context", new_callable=AsyncMock) as mock_context,
        patch("mindroom.bot.should_agent_respond", return_value=True),
        patch.object(bot, "_create_history_scope_storage"),
        patch("mindroom.bot.remove_run_by_event_id", return_value=False) as mock_remove_run,
        patch.object(bot, "_generate_response", new_callable=AsyncMock, return_value=None) as mock_generate_response,
    ):
        mock_context.return_value = MagicMock(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[MatrixID.from_agent("test_agent", "example.com", runtime_paths_for(config))],
            has_non_agent_mentions=False,
        )

        await bot._handle_message_edit(
            room,
            edit_event,
            EventInfo.from_event(edit_event.source),
            requester_user_id=edit_event.sender,
        )

        mock_generate_response.assert_awaited_once()
        assert bot.response_tracker.mark_responded.call_count == 0
        assert bot.response_tracker.get_response_event_id("$original:example.com") == "$response:example.com"
        assert mock_remove_run.call_count == 2


@pytest.mark.asyncio
async def test_response_tracker_mapping_persistence(tmp_path: Path) -> None:
    """Test that ResponseTracker correctly persists and retrieves user-to-response mappings."""
    # Create a response tracker
    tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

    # Mark some responses
    user_event_1 = "$user1:example.com"
    response_event_1 = "$response1:example.com"
    tracker.mark_responded(user_event_1, response_event_1)

    user_event_2 = "$user2:example.com"
    response_event_2 = "$response2:example.com"
    tracker.mark_responded(user_event_2, response_event_2)

    # Verify mappings are stored
    assert tracker.get_response_event_id(user_event_1) == response_event_1
    assert tracker.get_response_event_id(user_event_2) == response_event_2
    assert tracker.get_response_event_id("$unknown:example.com") is None

    # Create a new tracker instance to test persistence
    tracker2 = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

    # Verify mappings were loaded from disk
    assert tracker2.get_response_event_id(user_event_1) == response_event_1
    assert tracker2.get_response_event_id(user_event_2) == response_event_2


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
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.user_id = "@test_agent:example.com"

    # Create real ResponseTracker with the test path
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

    # Mock logger
    bot.logger = MagicMock()

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
        patch.object(bot, "_send_response", new_callable=AsyncMock) as mock_send_response,
        patch.object(bot, "_generate_response", new_callable=AsyncMock) as mock_generate_response,
        patch("mindroom.bot.fetch_thread_history", new_callable=AsyncMock) as mock_fetch_history,
    ):
        # Setup mocks
        mock_handle_reaction.return_value = ("Option 1", "thread_id")  # selected_value, thread_id
        mock_send_response.return_value = "$ack_event:example.com"  # Acknowledgment event ID
        mock_generate_response.return_value = (
            "$response_event:example.com"  # Response event ID (same as ack since we edit)
        )
        mock_fetch_history.return_value = []

        # Process the reaction event
        await bot._on_reaction(room, reaction_event)

        # Verify that the bot tracked the response correctly
        assert bot.response_tracker.has_responded("$question:example.com")
        assert bot.response_tracker.get_response_event_id("$question:example.com") == "$response_event:example.com"

        # Verify the methods were called with correct parameters
        mock_handle_reaction.assert_called_once()
        mock_send_response.assert_called_once()
        mock_generate_response.assert_called_once()

        # Verify that _generate_response was called with the acknowledgment event ID for editing
        call_kwargs = mock_generate_response.call_args.kwargs
        assert call_kwargs["existing_event_id"] == "$ack_event:example.com"
        assert call_kwargs["existing_event_is_placeholder"] is True


@pytest.mark.asyncio
async def test_on_reaction_leaves_question_retryable_when_ack_response_is_suppressed(tmp_path: Path) -> None:
    """A suppressed interactive response must not mark the original question completed."""
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
                "agent_reply_permissions": {"test_agent": ["@user:example.com"]},
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
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_test_agent:example.com"
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)
    bot.logger = MagicMock()

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
        patch.object(bot, "_send_response", new_callable=AsyncMock) as mock_send_response,
        patch.object(bot, "_generate_response", new_callable=AsyncMock) as mock_generate_response,
        patch("mindroom.bot.fetch_thread_history", new_callable=AsyncMock) as mock_fetch_history,
    ):
        mock_handle_reaction.return_value = ("Option 1", "thread_id")
        mock_send_response.return_value = "$ack_event:example.com"
        mock_generate_response.return_value = None
        mock_fetch_history.return_value = []

        await bot._on_reaction(room, reaction_event)

        assert bot.response_tracker.has_responded("$question:example.com") is False
        assert bot.response_tracker.get_response_event_id("$question:example.com") is None
        call_kwargs = mock_generate_response.call_args.kwargs
        assert call_kwargs["existing_event_id"] == "$ack_event:example.com"
        assert call_kwargs["existing_event_is_placeholder"] is True


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
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_test_agent:example.com"
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)
    bot.logger = MagicMock()

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
        patch.object(bot, "_send_response", new_callable=AsyncMock) as mock_send_response,
        patch.object(bot, "_generate_response", new_callable=AsyncMock) as mock_generate_response,
    ):
        mock_send_response.return_value = "$ack_event:example.com"
        mock_generate_response.return_value = "$response_event:example.com"

        await bot._on_reaction(room, disallowed_reaction)
        mock_send_response.assert_not_called()
        mock_generate_response.assert_not_called()

        await bot._on_reaction(room, allowed_reaction)

    interactive._active_questions.clear()

    mock_send_response.assert_called_once()
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
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = f"@mindroom_{ROUTER_AGENT_NAME}:example.com"
    bot.response_tracker = MagicMock()
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
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_test_agent:example.com"

    # Create real ResponseTracker with the test path
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

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
    with (
        patch("mindroom.bot.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_handle_voice,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
        patch.object(bot, "_generate_response", new_callable=AsyncMock) as mock_generate_response,
    ):
        # Setup mocks
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_handle_voice.return_value = "This is the transcribed message from voice"
        mock_generate_response.return_value = "$response:example.com"

        # Process the voice event
        await bot._on_media_message(room, voice_event)

        # Verify that the bot tracked the response correctly
        assert bot.response_tracker.has_responded("$voice:example.com")
        assert bot.response_tracker.get_response_event_id("$voice:example.com") == "$response:example.com"

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
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_test_agent:example.com"

    # Create real ResponseTracker with the test path
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

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

    # Mock voice_handler._handle_voice_message to return None (no transcription)
    with (
        patch("mindroom.bot.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_handle_voice,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
        patch.object(bot, "_generate_response", new_callable=AsyncMock) as mock_generate_response,
    ):
        # Setup mocks
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_handle_voice.return_value = None  # No transcription
        mock_generate_response.return_value = "$response:example.com"

        # Process the voice event
        await bot._on_media_message(room, voice_event)

        # Verify that the bot marked as responded with the fallback relay.
        assert bot.response_tracker.has_responded("$voice:example.com")
        assert bot.response_tracker.get_response_event_id("$voice:example.com") == "$response:example.com"

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
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.user_id = "@test_agent:example.com"

    # Create real ResponseTracker with the test path
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

    # Mock logger
    bot.logger = MagicMock()

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
    bot.response_tracker.mark_responded("$original:example.com", "$response:example.com")

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
        patch("mindroom.bot.is_authorized_sender", return_value=False) as mock_is_auth,
        patch.object(bot, "_handle_message_edit") as mock_handle_edit,
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
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.user_id = "@test_agent:example.com"

    # Create real ResponseTracker with the test path
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

    # Mock logger
    bot.logger = MagicMock()

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
        patch("mindroom.bot.is_authorized_sender", return_value=False) as mock_is_authorized,
        patch("mindroom.bot.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_handle_voice,
    ):
        # Process the voice event
        await bot._on_media_message(room, voice_event)

        # Verify that the bot marked as responded even for unauthorized sender
        assert bot.response_tracker.has_responded("$voice:example.com")
        # Should not have a response event ID since no response was sent
        assert bot.response_tracker.get_response_event_id("$voice:example.com") is None

        # Verify authorization was checked but voice handler was not called
        mock_is_authorized.assert_called_once()
        mock_handle_voice.assert_not_called()
