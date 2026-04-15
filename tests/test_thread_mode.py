"""Tests for thread_mode: room configuration and behavior."""

from __future__ import annotations

import asyncio
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from pydantic import ValidationError

from mindroom.bot import AgentBot
from mindroom.commands.parsing import Command, CommandType
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import ROUTER_AGENT_NAME, resolve_runtime_paths
from mindroom.conversation_resolver import MessageContext
from mindroom.matrix.cache.event_cache import ThreadCacheState
from mindroom.matrix.cache.write_coordinator import _EventCacheWriteCoordinator
from mindroom.matrix.client import ResolvedVisibleMessage, ThreadHistoryResult
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.streaming import StreamingResponse, send_streaming_response
from mindroom.thread_utils import create_session_id
from mindroom.tool_system.runtime_context import ToolRuntimeContext

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    delivered_matrix_event,
    install_runtime_cache_support,
    install_send_response_mock,
    make_event_cache_mock,
    runtime_paths_for,
    sync_bot_runtime_state,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)


def _runtime_bound_config(config: Config, runtime_root: Path | None = None) -> Config:
    """Return a runtime-bound config for thread mode tests."""
    if runtime_root is None:
        runtime_root = Path(tempfile.mkdtemp())
    runtime_paths = resolve_runtime_paths(
        config_path=runtime_root / "config.yaml",
        storage_path=runtime_root / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )
    return bind_runtime_paths(config, runtime_paths)


def _entity_thread_mode(config: Config, entity_name: str, *, room_id: str | None = None) -> str:
    """Resolve entity thread mode with the config's bound runtime context."""
    return config.get_entity_thread_mode(entity_name, runtime_paths_for(config), room_id=room_id)


def _agent_bot(
    *,
    config: Config,
    agent_user: AgentMatrixUser,
    storage_path: Path,
    rooms: list[str] | None = None,
) -> AgentBot:
    """Construct an agent bot with the test config's bound runtime context."""
    bot = AgentBot(
        config=config,
        agent_user=agent_user,
        storage_path=storage_path,
        runtime_paths=runtime_paths_for(config),
        rooms=[] if rooms is None else rooms,
    )
    install_runtime_cache_support(bot)
    wrap_extracted_collaborators(bot)
    return bot


def _install_static_logger_deps(bot: AgentBot, logger: MagicMock) -> None:
    """Rebuild extracted collaborators with one fixed logger dependency."""
    bot._conversation_cache.logger = logger
    resolver = replace(
        unwrap_extracted_collaborator(bot._conversation_resolver),
        deps=replace(unwrap_extracted_collaborator(bot._conversation_resolver).deps, logger=logger),
    )
    normalizer = replace(
        unwrap_extracted_collaborator(bot._inbound_turn_normalizer),
        deps=replace(unwrap_extracted_collaborator(bot._inbound_turn_normalizer).deps, logger=logger),
    )
    state_writer = replace(
        unwrap_extracted_collaborator(bot._conversation_state_writer),
        deps=replace(unwrap_extracted_collaborator(bot._conversation_state_writer).deps, logger=logger),
    )
    bot._conversation_resolver = resolver
    bot._inbound_turn_normalizer = normalizer
    bot._conversation_state_writer = state_writer
    wrap_extracted_collaborators(
        bot,
        "_conversation_resolver",
        "_inbound_turn_normalizer",
        "_conversation_state_writer",
    )


def _streaming_response(
    config: Config,
    *,
    room_id: str,
    reply_to_event_id: str | None,
    thread_id: str | None,
    sender_domain: str,
    room_mode: bool = False,
    latest_thread_event_id: str | None = None,
) -> StreamingResponse:
    """Construct a streaming response with the explicit runtime bound to the test config."""
    return StreamingResponse(
        room_id=room_id,
        reply_to_event_id=reply_to_event_id,
        thread_id=thread_id,
        sender_domain=sender_domain,
        config=config,
        runtime_paths=runtime_paths_for(config),
        room_mode=room_mode,
        latest_thread_event_id=latest_thread_event_id,
    )


@pytest.fixture
def room_mode_config() -> Config:
    """Config with one agent in room mode and one in default thread mode."""
    return _runtime_bound_config(
        Config(
            agents={
                "assistant": AgentConfig(
                    display_name="Assistant",
                    rooms=["!room:localhost"],
                    thread_mode="room",
                ),
                "coder": AgentConfig(
                    display_name="Coder",
                    rooms=["!room:localhost"],
                ),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
        ),
    )


@pytest.fixture
def assistant_user() -> AgentMatrixUser:
    """Create a mock assistant agent user in room mode."""
    return AgentMatrixUser(
        agent_name="assistant",
        password=TEST_PASSWORD,
        display_name="Assistant",
        user_id="@mindroom_assistant:localhost",
    )


@pytest.fixture
def coder_user() -> AgentMatrixUser:
    """Create a mock coder agent user in default thread mode."""
    return AgentMatrixUser(
        agent_name="coder",
        password=TEST_PASSWORD,
        display_name="Coder",
        user_id="@mindroom_coder:localhost",
    )


class TestThreadModeConfig:
    """Test thread_mode config parsing."""

    def test_default_thread_mode_is_thread(self) -> None:
        """Default thread_mode should be 'thread'."""
        agent = AgentConfig(display_name="Test")
        assert agent.thread_mode == "thread"

    def test_thread_mode_room(self) -> None:
        """Setting thread_mode to 'room' should work."""
        agent = AgentConfig(display_name="Test", thread_mode="room")
        assert agent.thread_mode == "room"

    def test_thread_mode_thread_explicit(self) -> None:
        """Explicitly setting thread_mode to 'thread' should work."""
        agent = AgentConfig(display_name="Test", thread_mode="thread")
        assert agent.thread_mode == "thread"

    def test_invalid_thread_mode_rejected(self) -> None:
        """Invalid thread_mode values should be rejected by Pydantic."""
        with pytest.raises(ValidationError):
            AgentConfig(display_name="Test", thread_mode="invalid")

    def test_room_thread_modes_override(self) -> None:
        """Per-room thread mode overrides should parse and persist."""
        agent = AgentConfig(
            display_name="Test",
            thread_mode="thread",
            room_thread_modes={"lobby": "room", "!room:localhost": "thread"},
        )
        assert agent.room_thread_modes == {"lobby": "room", "!room:localhost": "thread"}

    def test_invalid_room_thread_mode_rejected(self) -> None:
        """Invalid room_thread_modes values should be rejected by Pydantic."""
        with pytest.raises(ValidationError):
            AgentConfig(display_name="Test", room_thread_modes={"lobby": "invalid"})


class TestConfigThreadModeResolution:
    """Test thread-mode resolution for non-agent entities."""

    def test_agent_uses_room_override_for_matching_room(self) -> None:
        """Agent should honor room-specific thread mode overrides."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "assistant": AgentConfig(
                        display_name="Assistant",
                        thread_mode="thread",
                        room_thread_modes={"!room:localhost": "room"},
                    ),
                },
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
        )
        assert _entity_thread_mode(config, "assistant", room_id="!room:localhost") == "room"
        assert _entity_thread_mode(config, "assistant", room_id="!other:localhost") == "thread"

    def test_router_inherits_uniform_room_mode(self) -> None:
        """Router should use room mode when all configured agents use room mode."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "assistant": AgentConfig(display_name="Assistant", thread_mode="room"),
                    "coder": AgentConfig(display_name="Coder", thread_mode="room"),
                },
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
        )
        assert _entity_thread_mode(config, ROUTER_AGENT_NAME) == "room"

    def test_team_uses_member_mode_when_uniform(self) -> None:
        """Team should inherit room mode when all member agents are room mode."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "assistant": AgentConfig(display_name="Assistant", thread_mode="room"),
                    "coder": AgentConfig(display_name="Coder", thread_mode="room"),
                },
                teams={
                    "ops": TeamConfig(
                        display_name="Ops Team",
                        role="Operations",
                        agents=["assistant", "coder"],
                    ),
                },
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
        )
        assert _entity_thread_mode(config, "ops") == "room"

    def test_team_defaults_to_thread_when_members_mixed(self) -> None:
        """Team should default to thread mode when member modes differ."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "assistant": AgentConfig(display_name="Assistant", thread_mode="room"),
                    "coder": AgentConfig(display_name="Coder", thread_mode="thread"),
                },
                teams={
                    "ops": TeamConfig(
                        display_name="Ops Team",
                        role="Operations",
                        agents=["assistant", "coder"],
                    ),
                },
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
        )
        assert _entity_thread_mode(config, "ops") == "thread"

    def test_team_uses_room_specific_member_modes(self) -> None:
        """Team should resolve member modes with room-specific overrides."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "assistant": AgentConfig(
                        display_name="Assistant",
                        thread_mode="thread",
                        room_thread_modes={"!room:localhost": "room"},
                    ),
                    "coder": AgentConfig(
                        display_name="Coder",
                        thread_mode="thread",
                        room_thread_modes={"!room:localhost": "room"},
                    ),
                },
                teams={
                    "ops": TeamConfig(
                        display_name="Ops Team",
                        role="Operations",
                        agents=["assistant", "coder"],
                    ),
                },
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
        )
        assert _entity_thread_mode(config, "ops", room_id="!room:localhost") == "room"
        assert _entity_thread_mode(config, "ops", room_id="!other:localhost") == "thread"

    def test_router_uses_room_specific_modes_for_room_agents(self) -> None:
        """Router should resolve mode from agents configured for the active room."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "assistant": AgentConfig(
                        display_name="Assistant",
                        rooms=["!room:localhost"],
                        thread_mode="thread",
                        room_thread_modes={"!room:localhost": "room"},
                    ),
                    "coder": AgentConfig(
                        display_name="Coder",
                        rooms=["!other:localhost"],
                        thread_mode="thread",
                    ),
                },
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
        )
        assert _entity_thread_mode(config, ROUTER_AGENT_NAME, room_id="!room:localhost") == "room"

    def test_router_uses_team_room_agents_for_room_mode_resolution(self) -> None:
        """Router should include agents brought into a room via team room mapping."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "assistant": AgentConfig(
                        display_name="Assistant",
                        thread_mode="thread",
                        room_thread_modes={"!team-room:localhost": "room"},
                    ),
                    "coder": AgentConfig(
                        display_name="Coder",
                        rooms=["!other:localhost"],
                        thread_mode="thread",
                    ),
                },
                teams={
                    "ops": TeamConfig(
                        display_name="Ops Team",
                        role="Operations",
                        agents=["assistant"],
                        rooms=["!team-room:localhost"],
                    ),
                },
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
        )
        assert _entity_thread_mode(config, ROUTER_AGENT_NAME, room_id="!team-room:localhost") == "room"


class TestRouterHandoffThreadMode:
    """Test router handoff replies follow the suggested entity's thread mode."""

    @pytest.fixture
    def router_user(self) -> AgentMatrixUser:
        """Create a mock router user."""
        return AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            password=TEST_PASSWORD,
            display_name="Router",
            user_id="@mindroom_router:localhost",
        )

    @staticmethod
    def _routing_event() -> MagicMock:
        event = MagicMock(spec=nio.RoomMessageText)
        event.sender = "@user:localhost"
        event.body = "Help me"
        event.event_id = "$user_event"
        event.server_timestamp = 1000
        event.source = {
            "event_id": "$user_event",
            "sender": "@user:localhost",
            "type": "m.room.message",
            "content": {"body": "Help me", "msgtype": "m.text"},
        }
        return event

    @pytest.mark.asyncio
    async def test_router_handoff_uses_suggested_room_mode(
        self,
        room_mode_config: Config,
        router_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Router should send handoff in-room when the suggested agent is room-mode."""
        bot = _agent_bot(config=room_mode_config, agent_user=router_user, storage_path=tmp_path)
        bot.client = AsyncMock()
        captured_content: dict[str, object] = {}

        async def mock_send(_client: object, _room_id: str, content: dict) -> object:
            captured_content.clear()
            captured_content.update(content)
            return delivered_matrix_event("$reply", content)

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"

        # Mixed agent modes keep the router itself in thread mode.
        assert _entity_thread_mode(bot.config, ROUTER_AGENT_NAME, room_id=room.room_id) == "thread"

        with (
            patch("mindroom.turn_controller.suggest_agent_for_message", AsyncMock(return_value="assistant")),
            patch("mindroom.delivery_gateway.send_message_result", side_effect=mock_send),
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_latest_thread_event_id_if_needed",
                new_callable=AsyncMock,
            ) as mock_get_latest,
        ):
            await bot._turn_controller._execute_router_relay(
                room,
                self._routing_event(),
                [],
                "$thread_root",
                requester_user_id="@user:localhost",
            )
        mock_get_latest.assert_not_called()
        assert captured_content["m.relates_to"] == {"m.in_reply_to": {"event_id": "$user_event"}}

    @pytest.mark.asyncio
    async def test_router_handoff_uses_suggested_thread_mode(
        self,
        room_mode_config: Config,
        router_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Router should keep thread replies when the suggested agent is thread-mode."""
        bot = _agent_bot(config=room_mode_config, agent_user=router_user, storage_path=tmp_path)
        bot.client = AsyncMock()
        captured_content: dict[str, object] = {}

        async def mock_send(_client: object, _room_id: str, content: dict) -> object:
            captured_content.clear()
            captured_content.update(content)
            return delivered_matrix_event("$reply", content)

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"

        with (
            patch("mindroom.turn_controller.suggest_agent_for_message", AsyncMock(return_value="coder")),
            patch("mindroom.delivery_gateway.send_message_result", side_effect=mock_send),
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_latest_thread_event_id_if_needed",
                new_callable=AsyncMock,
                return_value="$latest",
            ) as mock_get_latest,
        ):
            await bot._turn_controller._execute_router_relay(
                room,
                self._routing_event(),
                [],
                "$thread_root",
                requester_user_id="@user:localhost",
            )
        mock_get_latest.assert_awaited_once()
        assert "m.relates_to" in captured_content
        assert isinstance(captured_content["m.relates_to"], dict)
        assert captured_content["m.relates_to"].get("rel_type") == "m.thread"


class TestCreateSessionIdWithNoneThread:
    """Verify create_session_id returns room-level ID when thread_id=None."""

    def test_room_level_session(self) -> None:
        """When thread_id is None, session_id should be just the room_id."""
        session_id = create_session_id("!room:localhost", None)
        assert session_id == "!room:localhost"

    def test_thread_level_session(self) -> None:
        """When thread_id is set, session_id should include it."""
        session_id = create_session_id("!room:localhost", "$thread123")
        assert session_id == "!room:localhost:$thread123"

    def test_message_target_room_mode_reuses_room_level_session_format(self) -> None:
        """Room-mode MessageTarget sessions should match create_session_id(None)."""
        target = MessageTarget.resolve(
            room_id="!room:localhost",
            thread_id="$thread123",
            reply_to_event_id="$event456",
            room_mode=True,
        )
        assert target.source_thread_id is None
        assert target.resolved_thread_id is None
        assert target.session_id == create_session_id("!room:localhost", None)

    def test_message_target_plain_reply_keeps_room_level_session(self) -> None:
        """Plain reply targets should not derive thread or session identity."""
        target = MessageTarget.resolve(
            room_id="!room:localhost",
            thread_id=None,
            reply_to_event_id="$event456",
            room_mode=False,
        )
        assert target.reply_to_event_id == "$event456"
        assert target.resolved_thread_id is None
        assert target.session_id == create_session_id("!room:localhost", None)

    def test_message_target_from_runtime_context_keeps_room_mode_thread_provenance(self) -> None:
        """Room-mode runtime targets should retain raw provenance only under the source-thread field."""
        config = _runtime_bound_config(
            Config(
                agents={},
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
        )
        runtime_context = ToolRuntimeContext(
            agent_name="assistant",
            room_id="!room:localhost",
            thread_id="$raw-thread",
            resolved_thread_id=None,
            requester_id="@user:localhost",
            client=AsyncMock(),
            config=config,
            runtime_paths=runtime_paths_for(config),
            event_cache=make_event_cache_mock(),
            reply_to_event_id="$event456",
            session_id=create_session_id("!room:localhost", None),
        )

        target = MessageTarget.from_runtime_context(runtime_context)

        assert target.source_thread_id == "$raw-thread"
        assert target.resolved_thread_id is None
        assert target.is_room_mode is True


class TestExtractMessageContextRoomMode:
    """Test _extract_message_context skips thread derivation in room mode."""

    @pytest.mark.asyncio
    async def test_room_mode_skips_derive(
        self,
        room_mode_config: Config,
        assistant_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """In room mode, _extract_message_context should return empty thread context."""
        bot = _agent_bot(config=room_mode_config, agent_user=assistant_user, storage_path=tmp_path)
        bot.client = MagicMock()

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.name = "Test Room"

        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$event123"
        event.server_timestamp = 1000
        event.sender = "@user:localhost"
        event.source = {
            "event_id": "$event123",
            "sender": "@user:localhost",
            "content": {"body": "hello", "msgtype": "m.text"},
            "type": "m.room.message",
        }

        with patch("mindroom.conversation_resolver.check_agent_mentioned", return_value=([], False, False)):
            ctx = await bot._conversation_resolver.extract_message_context(room, event)

        assert ctx.is_thread is False
        assert ctx.thread_id is None
        assert ctx.thread_history == []

    @pytest.mark.asyncio
    async def test_room_override_skips_derive_only_for_matching_room(
        self,
        assistant_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Room-specific mode overrides should only affect matching rooms."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "assistant": AgentConfig(
                        display_name="Assistant",
                        rooms=["!room:localhost", "!other:localhost"],
                        thread_mode="thread",
                        room_thread_modes={"!room:localhost": "room"},
                    ),
                },
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
        )
        bot = _agent_bot(config=config, agent_user=assistant_user, storage_path=tmp_path)
        bot.client = MagicMock()
        unwrap_extracted_collaborator(bot._conversation_resolver)._resolve_thread_context = AsyncMock(
            return_value=(True, "$thread123", [{"event_id": "$thread123"}], False),
        )

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.name = "Room Override"

        other_room = MagicMock(spec=nio.MatrixRoom)
        other_room.room_id = "!other:localhost"
        other_room.name = "No Override"

        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$event123"
        event.server_timestamp = 1000
        event.sender = "@user:localhost"
        event.source = {
            "event_id": "$event123",
            "sender": "@user:localhost",
            "content": {"body": "hello", "msgtype": "m.text"},
            "type": "m.room.message",
        }

        with patch("mindroom.conversation_resolver.check_agent_mentioned", return_value=([], False, False)):
            room_mode_ctx = await bot._conversation_resolver.extract_message_context(room, event)
            thread_mode_ctx = await bot._conversation_resolver.extract_message_context(other_room, event)

        assert room_mode_ctx.is_thread is False
        assert room_mode_ctx.thread_id is None
        assert room_mode_ctx.thread_history == []

        assert thread_mode_ctx.is_thread is True
        assert thread_mode_ctx.thread_id == "$thread123"
        assert thread_mode_ctx.thread_history == [{"event_id": "$thread123"}]

    def test_target_helpers_delegate_to_conversation_resolver(
        self,
        assistant_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Target resolution wrappers should stay thin and route through the resolver."""
        config = _runtime_bound_config(
            Config(
                agents={"assistant": AgentConfig(display_name="Assistant", rooms=["!room:localhost"])},
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )
        bot = _agent_bot(config=config, agent_user=assistant_user, storage_path=tmp_path)
        expected_target = MessageTarget.resolve("!room:localhost", "$thread123", "$event123")
        bot._conversation_resolver.build_message_target = MagicMock(return_value=expected_target)
        bot._conversation_resolver.resolve_response_thread_root = MagicMock(return_value="$thread123")

        target = bot._conversation_resolver.build_message_target(
            room_id="!room:localhost",
            thread_id="$thread123",
            reply_to_event_id="$event123",
        )
        thread_root = bot._conversation_resolver.resolve_response_thread_root(
            "$thread123",
            "$event123",
            room_id="!room:localhost",
        )

        assert target is expected_target
        assert thread_root == "$thread123"
        bot._conversation_resolver.build_message_target.assert_called_once_with(
            room_id="!room:localhost",
            thread_id="$thread123",
            reply_to_event_id="$event123",
        )
        bot._conversation_resolver.resolve_response_thread_root.assert_called_once_with(
            "$thread123",
            "$event123",
            room_id="!room:localhost",
        )

    @pytest.mark.asyncio
    async def test_context_helpers_delegate_to_conversation_resolver(
        self,
        assistant_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Context extraction helpers should go through the extracted resolver."""
        config = _runtime_bound_config(
            Config(
                agents={"assistant": AgentConfig(display_name="Assistant", rooms=["!room:localhost"])},
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )
        bot = _agent_bot(config=config, agent_user=assistant_user, storage_path=tmp_path)
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$event123"
        event.sender = "@user:localhost"
        event.source = {"content": {"body": "hello", "msgtype": "m.text"}}
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=True,
            thread_id="$thread123",
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
            requires_full_thread_history=True,
        )
        bot._conversation_resolver.extract_dispatch_context = AsyncMock(return_value=context)
        bot._conversation_resolver.extract_message_context = AsyncMock(return_value=context)
        bot._conversation_resolver.extract_message_context_impl = AsyncMock(return_value=context)

        assert await bot._conversation_resolver.extract_dispatch_context(room, event) is context
        assert await bot._conversation_resolver.extract_message_context(room, event, full_history=False) is context
        assert (
            await bot._conversation_resolver.extract_message_context_impl(
                room,
                event,
                full_history=True,
                dispatch_safe=False,
            )
            is context
        )

        bot._conversation_resolver.extract_dispatch_context.assert_awaited_once_with(room, event)
        bot._conversation_resolver.extract_message_context.assert_awaited_once_with(
            room,
            event,
            full_history=False,
        )
        bot._conversation_resolver.extract_message_context_impl.assert_awaited_once_with(
            room,
            event,
            full_history=True,
            dispatch_safe=False,
        )

    def test_hot_reloaded_bot_uses_updated_thread_mode_without_restart(
        self,
        assistant_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Swapping bot.config should immediately change resolver thread targeting."""
        initial_config = _runtime_bound_config(
            Config(
                agents={
                    "assistant": AgentConfig(
                        display_name="Assistant",
                        rooms=["!room:localhost"],
                        thread_mode="thread",
                    ),
                },
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )
        updated_config = _runtime_bound_config(
            Config(
                agents={
                    "assistant": AgentConfig(
                        display_name="Assistant",
                        rooms=["!room:localhost"],
                        thread_mode="room",
                    ),
                },
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )
        bot = _agent_bot(config=initial_config, agent_user=assistant_user, storage_path=tmp_path)

        threaded_target = bot._conversation_resolver.build_message_target(
            room_id="!room:localhost",
            thread_id="$thread123",
            reply_to_event_id="$event123",
        )
        bot.config = updated_config
        sync_bot_runtime_state(bot)
        room_mode_target = bot._conversation_resolver.build_message_target(
            room_id="!room:localhost",
            thread_id="$thread123",
            reply_to_event_id="$event123",
            event_source={
                "event_id": "$event123",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "type": "m.room.message",
                "content": {"body": "Hello", "msgtype": "m.text"},
            },
        )

        assert threaded_target.resolved_thread_id == "$thread123"
        assert room_mode_target.source_thread_id is None
        assert room_mode_target.resolved_thread_id is None

    def test_build_message_target_plain_reply_does_not_infer_thread_identity(
        self,
        assistant_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Resolver target building should keep plain replies out of thread/session routing."""
        config = _runtime_bound_config(
            Config(
                agents={"assistant": AgentConfig(display_name="Assistant", rooms=["!room:localhost"])},
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )
        bot = _agent_bot(config=config, agent_user=assistant_user, storage_path=tmp_path)

        target = bot._conversation_resolver.build_message_target(
            room_id="!room:localhost",
            thread_id=None,
            reply_to_event_id="$reply-event:localhost",
            event_source={
                "content": {
                    "body": "plain reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$target:localhost"}},
                },
                "event_id": "$reply-event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!room:localhost",
                "type": "m.room.message",
            },
        )

        assert target.reply_to_event_id == "$reply-event:localhost"
        assert target.resolved_thread_id is None
        assert target.session_id == create_session_id("!room:localhost", None)

    @pytest.mark.parametrize("relation_type", ["m.replace", "m.annotation", "m.reference"])
    def test_build_message_target_plain_reply_relation_does_not_infer_thread_identity(
        self,
        assistant_user: AgentMatrixUser,
        tmp_path: Path,
        relation_type: str,
    ) -> None:
        """Edits, reactions, and references to plain replies must stay room-scoped."""
        config = _runtime_bound_config(
            Config(
                agents={"assistant": AgentConfig(display_name="Assistant", rooms=["!room:localhost"])},
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )
        bot = _agent_bot(config=config, agent_user=assistant_user, storage_path=tmp_path)

        target = bot._conversation_resolver.build_message_target(
            room_id="!room:localhost",
            thread_id=None,
            reply_to_event_id="$relation-event:localhost",
            event_source={
                "content": {
                    "body": "relation on plain reply",
                    "msgtype": "m.text",
                    "m.relates_to": {
                        "rel_type": relation_type,
                        "event_id": "$plain-reply:localhost",
                    },
                },
                "event_id": "$relation-event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!room:localhost",
                "type": "m.room.message",
            },
        )

        assert target.reply_to_event_id == "$relation-event:localhost"
        assert target.resolved_thread_id is None
        assert target.session_id == create_session_id("!room:localhost", None)


class TestSendResponseRoomMode:
    """Test _send_response skips thread relation in room mode."""

    @pytest.mark.asyncio
    async def test_room_mode_no_thread_metadata(
        self,
        room_mode_config: Config,
        assistant_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """In room mode, _send_response should not add thread relation metadata."""
        bot = _agent_bot(config=room_mode_config, agent_user=assistant_user, storage_path=tmp_path)
        bot.client = AsyncMock()

        captured_content: dict = {}

        async def mock_send(_client: object, _room_id: str, content: dict) -> object:
            captured_content.update(content)
            return delivered_matrix_event("$response_event", content)

        with patch("mindroom.delivery_gateway.send_message_result", side_effect=mock_send):
            event_id = await bot._send_response(
                room_id="!room:localhost",
                reply_to_event_id="$event123",
                response_text="Hello!",
                thread_id=None,
            )

        assert event_id == "$response_event"
        # Room mode should NOT have m.relates_to with thread relation
        relates_to = captured_content.get("m.relates_to")
        if relates_to:
            assert relates_to.get("rel_type") != "m.thread"


class TestStreamingResponseRoomMode:
    """Test StreamingResponse skips thread and reply relations when room_mode=True."""

    @pytest.fixture
    def streaming_config(self) -> Config:
        """Minimal config for streaming tests."""
        return _runtime_bound_config(
            Config(
                agents={},
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test")},
                router=RouterConfig(model="default"),
            ),
        )

    def test_room_mode_field_default(self, streaming_config: Config) -> None:
        """StreamingResponse should default room_mode to False."""
        sr = _streaming_response(
            streaming_config,
            room_id="!room:localhost",
            reply_to_event_id="$event123",
            thread_id="$thread123",
            sender_domain="localhost",
        )
        assert sr.room_mode is False

    @pytest.mark.asyncio
    async def test_room_mode_no_relations(self, streaming_config: Config) -> None:
        """In room mode, _send_or_edit_message should emit no m.relates_to."""
        sr = _streaming_response(
            streaming_config,
            room_id="!room:localhost",
            reply_to_event_id="$event123",
            thread_id="$thread123",
            sender_domain="localhost",
            room_mode=True,
            latest_thread_event_id="$latest",
        )
        sr.accumulated_text = "Hello!"

        captured: dict = {}

        async def mock_send(_client: object, _room_id: str, content: dict) -> object:
            captured.update(content)
            return delivered_matrix_event("$sent", content)

        client = AsyncMock()
        with patch("mindroom.streaming.send_message_result", side_effect=mock_send):
            await sr._send_or_edit_message(client, is_final=True)

        assert "m.relates_to" not in captured

    @pytest.mark.asyncio
    async def test_thread_mode_has_relations(self, streaming_config: Config) -> None:
        """In default thread mode, _send_or_edit_message should emit m.relates_to."""
        sr = _streaming_response(
            streaming_config,
            room_id="!room:localhost",
            reply_to_event_id="$event123",
            thread_id="$thread123",
            sender_domain="localhost",
            room_mode=False,
            latest_thread_event_id="$latest",
        )
        sr.accumulated_text = "Hello!"

        captured: dict = {}

        async def mock_send(_client: object, _room_id: str, content: dict) -> object:
            captured.update(content)
            return delivered_matrix_event("$sent", content)

        client = AsyncMock()
        with patch("mindroom.streaming.send_message_result", side_effect=mock_send):
            await sr._send_or_edit_message(client, is_final=True)

        assert "m.relates_to" in captured
        assert captured["m.relates_to"]["rel_type"] == "m.thread"


class TestSendStreamingResponseRoomMode:
    """Test send_streaming_response skips thread lookup and relations in room mode."""

    @pytest.mark.asyncio
    async def test_room_mode_skips_latest_thread_lookup(self) -> None:
        """In room mode, send_streaming_response should not call get_latest_thread_event_id_if_needed."""
        config = _runtime_bound_config(
            Config(
                agents={},
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test")},
                router=RouterConfig(model="default"),
            ),
        )

        async def empty_stream() -> AsyncIterator[str]:
            yield "Hello!"

        client = AsyncMock()

        captured: dict = {}

        async def mock_send(_client: object, _room_id: str, content: dict) -> object:
            captured.update(content)
            return delivered_matrix_event("$sent", content)

        with (
            patch("mindroom.streaming.send_message_result", side_effect=mock_send),
        ):
            await send_streaming_response(
                client,
                "!room:localhost",
                "$event123",
                "$thread123",
                "localhost",
                config,
                runtime_paths_for(config),
                empty_stream(),
                room_mode=True,
            )

        assert "m.relates_to" not in captured


class TestCommandThreadContextRoomMode:
    """Test command handling uses room context in room mode."""

    @pytest.mark.asyncio
    async def test_schedule_command_uses_no_thread_id_in_room_mode(
        self,
        tmp_path: Path,
    ) -> None:
        """Router command scheduling should persist room-level (not thread) context."""
        config = _runtime_bound_config(
            Config(
                agents={"assistant": AgentConfig(display_name="Assistant", thread_mode="room")},
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )
        router_user = AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            password=TEST_PASSWORD,
            display_name="Router",
            user_id="@mindroom_router:localhost",
        )
        bot = _agent_bot(config=config, agent_user=router_user, storage_path=tmp_path)
        bot.client = AsyncMock()
        bot._send_response = AsyncMock(return_value="$reply")
        install_send_response_mock(bot, bot._send_response)
        unwrap_extracted_collaborator(bot._conversation_resolver).derive_conversation_context = AsyncMock(
            return_value=(False, None, []),
        )

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"

        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$event123",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "content": {"msgtype": "m.text", "body": "!schedule in 5 minutes ping"},
            },
        )
        command = Command(
            type=CommandType.SCHEDULE,
            args={"full_text": "in 5 minutes ping"},
            raw_text="!schedule in 5 minutes ping",
        )

        with (
            patch("mindroom.commands.handler.check_agent_mentioned", return_value=([], False, False)),
            patch(
                "mindroom.commands.handler.schedule_task",
                new_callable=AsyncMock,
                return_value=("task123", "scheduled"),
            ) as mock_schedule,
        ):
            await bot._turn_controller._execute_command(
                room=room,
                event=event,
                requester_user_id="@user:localhost",
                command=command,
            )

        assert mock_schedule.await_args.kwargs["thread_id"] is None
        assert bot._send_response.await_args.args[3] is None


class TestExtractedModuleLoggerRebinding:
    """Extracted helper modules should keep their construction-time logger deps."""

    @pytest.mark.asyncio
    async def test_conversation_resolver_uses_rebound_bot_logger(
        self,
        assistant_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Resolver logging should keep the logger captured in its deps."""
        config = _runtime_bound_config(
            Config(
                agents={"assistant": AgentConfig(display_name="Assistant", rooms=["!room:localhost"])},
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )
        bot = _agent_bot(config=config, agent_user=assistant_user, storage_path=tmp_path)
        bot.client = AsyncMock()
        original_logger = MagicMock()
        rebound_logger = MagicMock()
        _install_static_logger_deps(bot, original_logger)
        bot.logger = original_logger
        bot.logger = rebound_logger

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$event123"
        event.sender = "@user:localhost"
        event.source = {
            "event_id": "$event123",
            "sender": "@user:localhost",
            "content": {"body": "hello", "msgtype": "m.text"},
            "type": "m.room.message",
        }

        with patch(
            "mindroom.conversation_resolver.check_agent_mentioned",
            return_value=([assistant_user.matrix_id], True, False),
        ):
            await bot._conversation_resolver.extract_message_context(room, event)

        original_logger.info.assert_any_call("Mentioned", event_id="$event123", room_id="!room:localhost")
        rebound_logger.info.assert_not_called()

    @pytest.mark.asyncio
    async def test_inbound_turn_normalizer_uses_rebound_bot_logger(
        self,
        assistant_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Normalizer logging should keep the logger captured in its deps."""
        config = _runtime_bound_config(
            Config(
                agents={"assistant": AgentConfig(display_name="Assistant", rooms=["!room:localhost"])},
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )
        bot = _agent_bot(config=config, agent_user=assistant_user, storage_path=tmp_path)
        bot.client = AsyncMock()
        original_logger = MagicMock()
        rebound_logger = MagicMock()
        _install_static_logger_deps(bot, original_logger)
        bot.logger = original_logger
        bot.logger = rebound_logger

        event = MagicMock(spec=nio.RoomMessageImage)
        event.event_id = "$img123"
        event.sender = "@user:localhost"
        event.body = "photo.png"
        event.source = {"content": {"body": "photo.png", "msgtype": "m.image"}}

        with patch(
            "mindroom.inbound_turn_normalizer.register_image_attachment",
            new_callable=AsyncMock,
            return_value=None,
        ):
            attachment_id = await bot._inbound_turn_normalizer.register_routed_attachment(
                room_id="!room:localhost",
                thread_id=None,
                event=event,
            )

        assert attachment_id is None
        original_logger.error.assert_called_once_with(
            "Failed to register routed image attachment",
            event_id="$img123",
        )
        rebound_logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_conversation_state_writer_uses_rebound_bot_logger(
        self,
        assistant_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """State-writer cache warnings should keep the logger captured in its deps."""
        config = _runtime_bound_config(
            Config(
                agents={"assistant": AgentConfig(display_name="Assistant", rooms=["!room:localhost"])},
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )
        bot = _agent_bot(config=config, agent_user=assistant_user, storage_path=tmp_path)
        original_logger = MagicMock()
        rebound_logger = MagicMock()
        _install_static_logger_deps(bot, original_logger)
        bot.logger = original_logger
        bot.logger = rebound_logger

        event_cache = AsyncMock()
        event_cache.append_event.side_effect = RuntimeError("cache write failed")
        bot.event_cache = event_cache
        bot.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=bot._runtime_view,
        )

        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$event123",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "content": {
                    "msgtype": "m.text",
                    "body": "hello",
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$threadroot",
                        "is_falling_back": True,
                    },
                },
            },
        )

        await bot._conversation_cache.append_live_event(
            "!room:localhost",
            event,
            event_info=EventInfo.from_event(event.source),
        )

        original_logger.warning.assert_called_once_with(
            "Failed to append thread event to cache",
            room_id="!room:localhost",
            thread_id="$threadroot",
            event_id="$event123",
            context="live",
            error="cache write failed",
        )
        rebound_logger.warning.assert_not_called()

    def test_conversation_resolver_fetch_path_uses_conversation_cache_api(
        self,
        assistant_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Resolver full-history fetches should go through the explicit conversation-cache layer."""
        config = _runtime_bound_config(
            Config(
                agents={"assistant": AgentConfig(display_name="Assistant", rooms=["!room:localhost"])},
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )
        bot = _agent_bot(config=config, agent_user=assistant_user, storage_path=tmp_path)
        bot.client = AsyncMock()
        sync_bot_runtime_state(bot)
        bot._conversation_cache.get_dispatch_thread_history = AsyncMock(return_value=[])

        asyncio.run(
            unwrap_extracted_collaborator(bot._conversation_resolver).fetch_thread_history(
                bot.client,
                "!room:localhost",
                "$threadroot",
            ),
        )

        bot._conversation_cache.get_dispatch_thread_history.assert_awaited_once_with("!room:localhost", "$threadroot")

    @pytest.mark.asyncio
    async def test_conversation_cache_fetch_path_passes_explicit_event_cache(
        self,
        assistant_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Access-layer fetches should opt into cache maintenance explicitly."""
        config = _runtime_bound_config(
            Config(
                agents={"assistant": AgentConfig(display_name="Assistant", rooms=["!room:localhost"])},
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )
        bot = _agent_bot(config=config, agent_user=assistant_user, storage_path=tmp_path)
        bot.event_cache = make_event_cache_mock()
        bot.event_cache.get_thread_events = AsyncMock(
            return_value=[
                {
                    "event_id": "$threadroot",
                    "sender": "@user:localhost",
                    "type": "m.room.message",
                    "origin_server_ts": 1000,
                    "content": {"body": "Root", "msgtype": "m.text"},
                },
                {
                    "event_id": "$reply",
                    "sender": "@agent:localhost",
                    "type": "m.room.message",
                    "origin_server_ts": 2000,
                    "content": {
                        "body": "Reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$threadroot"},
                    },
                },
            ],
        )
        sync_bot_runtime_state(bot)

        client = AsyncMock()
        with patch(
            "mindroom.matrix.conversation_cache.fetch_thread_history",
            new=AsyncMock(return_value=[]),
        ) as fetch_thread_history_mock:
            bot.client = client
            await bot._conversation_cache.get_thread_history(
                "!room:localhost",
                "$threadroot",
            )

        fetch_thread_history_mock.assert_awaited_once()
        call_args = fetch_thread_history_mock.await_args
        assert call_args.args == (
            client,
            "!room:localhost",
            "$threadroot",
        )
        assert call_args.kwargs["event_cache"] is bot.event_cache
        assert call_args.kwargs["runtime_started_at"] == bot._runtime_view.runtime_started_at

    @pytest.mark.asyncio
    async def test_conversation_cache_reuses_fresh_durable_snapshot_before_full_history_hydration(
        self,
        assistant_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Fresh durable thread rows should serve snapshots before full-history hydration."""
        config = _runtime_bound_config(
            Config(
                agents={"assistant": AgentConfig(display_name="Assistant", rooms=["!room:localhost"])},
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )
        bot = _agent_bot(config=config, agent_user=assistant_user, storage_path=tmp_path)
        bot.event_cache = make_event_cache_mock()
        sync_bot_runtime_state(bot)
        bot.event_cache.get_thread_cache_state = AsyncMock(
            return_value=ThreadCacheState(
                validated_at=time.time(),
                invalidated_at=None,
                invalidation_reason=None,
                room_invalidated_at=None,
                room_invalidation_reason=None,
            ),
        )
        bot.event_cache.get_thread_events = AsyncMock(
            return_value=[
                {
                    "event_id": "$threadroot",
                    "sender": "@user:localhost",
                    "type": "m.room.message",
                    "origin_server_ts": 1000,
                    "content": {"body": "Root", "msgtype": "m.text"},
                },
                {
                    "event_id": "$reply",
                    "sender": "@agent:localhost",
                    "type": "m.room.message",
                    "origin_server_ts": 2000,
                    "content": {
                        "body": "Reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$threadroot"},
                    },
                },
            ],
        )

        bot.client = AsyncMock()
        async with bot._conversation_cache.turn_scope():
            snapshot_history = await bot._conversation_cache.get_thread_snapshot(
                "!room:localhost",
                "$threadroot",
            )
            full_history = await bot._conversation_cache.get_thread_history(
                "!room:localhost",
                "$threadroot",
            )

        assert snapshot_history.is_full_history is False
        assert [message.event_id for message in snapshot_history] == ["$threadroot", "$reply"]
        assert full_history.is_full_history is True
        assert [message.event_id for message in full_history] == ["$threadroot", "$reply"]

    @pytest.mark.asyncio
    async def test_coalescing_thread_id_ignores_plain_reply_relations(
        self,
        assistant_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Pre-gate coalescing should not infer a thread from plain reply relations."""
        config = _runtime_bound_config(
            Config(
                agents={"assistant": AgentConfig(display_name="Assistant", rooms=["!room:localhost"])},
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )
        bot = _agent_bot(config=config, agent_user=assistant_user, storage_path=tmp_path)
        bot.client = MagicMock()
        sync_bot_runtime_state(bot)
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$reply:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "body": "follow-up",
                    "m.relates_to": {
                        "m.in_reply_to": {"event_id": "$reply-seed:localhost"},
                    },
                },
            },
        )

        with patch.object(
            bot._conversation_resolver,
            "derive_conversation_target",
            new=AsyncMock(side_effect=AssertionError("coalescing should not derive full dispatch target")),
        ):
            thread_id = await bot._conversation_resolver.coalescing_thread_id(room, event)

        assert thread_id is None

    @pytest.mark.asyncio
    async def test_direct_thread_dispatch_preview_still_requires_full_thread_history(
        self,
        assistant_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Only explicit thread events should defer full history after snapshot preview."""
        config = _runtime_bound_config(
            Config(
                agents={"assistant": AgentConfig(display_name="Assistant", rooms=["!room:localhost"])},
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )
        bot = _agent_bot(config=config, agent_user=assistant_user, storage_path=tmp_path)
        bot.client = AsyncMock()
        sync_bot_runtime_state(bot)
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.name = "Direct Thread Room"
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$reply:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "body": "follow-up",
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$thread-root:localhost",
                        "m.in_reply_to": {"event_id": "$thread-msg:localhost"},
                    },
                },
            },
        )
        preview_snapshot = ThreadHistoryResult(
            [
                ResolvedVisibleMessage.synthetic(
                    sender="@user:localhost",
                    body="Root",
                    event_id="$thread-root:localhost",
                ),
                ResolvedVisibleMessage.synthetic(
                    sender="@user:localhost",
                    body="Earlier reply",
                    event_id="$thread-msg:localhost",
                ),
            ],
            is_full_history=False,
        )

        bot._conversation_cache.get_dispatch_thread_snapshot = AsyncMock(return_value=preview_snapshot)

        with patch.object(
            bot._conversation_cache,
            "get_thread_snapshot",
            AsyncMock(side_effect=AssertionError("dispatch preview should use strict dispatch snapshot reads")),
        ):
            context = await bot._conversation_resolver.extract_dispatch_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread-root:localhost"
        assert [message.event_id for message in context.thread_history] == [
            "$thread-root:localhost",
            "$thread-msg:localhost",
        ]
        assert context.requires_full_thread_history is True
        bot._conversation_cache.get_dispatch_thread_snapshot.assert_awaited_once_with(
            room.room_id,
            "$thread-root:localhost",
        )


class TestConversationCacheArchitecture:
    """Architecture guards for the explicit conversation-cache seam."""

    def test_hot_path_modules_do_not_call_raw_matrix_history_apis(self) -> None:
        """Hot-path conversation modules should use the explicit conversation-cache layer."""
        repo_root = Path(__file__).resolve().parents[1]
        banned_calls = (
            "room_get_event(",
            "room_get_event_relations(",
            "room_messages(",
            "cached_room_get_event(",
        )
        for relative_path in (
            "src/mindroom/conversation_resolver.py",
            "src/mindroom/turn_policy.py",
            "src/mindroom/response_runner.py",
        ):
            file_text = (repo_root / relative_path).read_text()
            for banned_call in banned_calls:
                assert banned_call not in file_text, f"{relative_path} should not call {banned_call}"

    def test_hot_path_modules_do_not_reference_event_cache_directly(self) -> None:
        """Hot-path conversation modules should not bypass the conversation-cache layer for cache state."""
        repo_root = Path(__file__).resolve().parents[1]
        banned_tokens = (
            "_EventCache(",
            "event_cache.",
        )
        for relative_path in (
            "src/mindroom/conversation_resolver.py",
            "src/mindroom/turn_policy.py",
            "src/mindroom/response_runner.py",
        ):
            file_text = (repo_root / relative_path).read_text()
            for banned_token in banned_tokens:
                assert banned_token not in file_text, f"{relative_path} should not reference {banned_token}"
