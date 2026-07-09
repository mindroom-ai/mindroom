"""Core AgentBot lifecycle and integration tests (see the test_bot_* modules for split concerns)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from agno.models.ollama import Ollama
from agno.run.agent import RunContentEvent
from agno.run.team import TeamRunOutput

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import (
    ROUTER_AGENT_NAME,
)
from mindroom.conversation_resolver import MessageContext
from mindroom.handled_turns import TurnRecord
from mindroom.knowledge.utils import _KnowledgeResolution
from mindroom.matrix.cache import ThreadHistoryResult
from mindroom.matrix.cache.thread_history_result import thread_history_result
from mindroom.matrix.client import PermanentMatrixStartupError
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import AgentMatrixUser
from mindroom.media_inputs import MediaInputs
from mindroom.message_target import MessageTarget
from mindroom.orchestrator import (
    _MultiAgentOrchestrator,
)
from mindroom.startup_errors import PermanentStartupError
from tests.bot_helpers import (
    AgentBotTestBase,
    _install_runtime_cache_support,
    _make_matrix_client_mock,
    _runtime_bound_config,
    _set_turn_store_tracker,
    _turn_store,
    _visible_message,
    _wrap_extracted_collaborators,
    make_mock_agent_user,
)
from tests.conftest import (
    TEST_PASSWORD,
    drain_coalescing,
    runtime_paths_for,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)
from tests.identity_helpers import entity_ids, persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


@pytest.fixture
def mock_agent_user() -> AgentMatrixUser:
    """Mock agent user for testing."""
    return make_mock_agent_user()


def test_agent_bot_init_requires_prepared_matrix_user_id(tmp_path: Path) -> None:
    """Runtime bot construction requires the orchestrator account-preparation barrier."""
    agent_user = AgentMatrixUser(
        agent_name="calculator",
        password=TEST_PASSWORD,
        display_name="CalculatorAgent",
        user_id="",
    )
    config = _runtime_bound_config(
        Config(
            agents={"calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!test:localhost"])},
            models={"default": ModelConfig(provider="test", id="test-model")},
            authorization=AuthorizationConfig(default_room_access=True),
        ),
        tmp_path,
    )

    with pytest.raises(PermanentMatrixStartupError, match="Missing Matrix ID for 'calculator'"):
        AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))


class TestAgentBot(AgentBotTestBase):
    """Bot behavior tests moved verbatim from tests/test_multi_agent_bot.py."""

    def test_agent_property_rejects_private_agent_without_request_identity(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """AgentBot.agent should fail fast for private agents with no request scope."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        role="Math assistant",
                        rooms=[],
                        private=AgentPrivateConfig(per="user", root="mind_data"),
                    ),
                },
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))

        with pytest.raises(
            ValueError,
            match="AgentBot\\.agent is only available for shared agents",
        ):
            _ = bot.agent

    @pytest.mark.asyncio
    @patch("mindroom.config.main.load_config")
    async def test_agent_bot_initialization(
        self,
        mock_load_config: MagicMock,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test AgentBot initialization."""
        mock_load_config.return_value = self.create_mock_config(tmp_path)
        config = mock_load_config.return_value

        bot = AgentBot(mock_agent_user, tmp_path, config, runtime_paths_for(config), rooms=["!test:localhost"])
        assert bot.agent_user == mock_agent_user
        assert bot.agent_name == "calculator"
        assert bot.rooms == ["!test:localhost"]
        assert not bot.running
        assert bot.enable_streaming is True  # Default value

        # Test with streaming disabled
        bot_no_stream = AgentBot(
            mock_agent_user,
            tmp_path,
            rooms=["!test:localhost"],
            enable_streaming=False,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        assert bot_no_stream.enable_streaming is False

    @pytest.mark.asyncio
    @patch("mindroom.constants.runtime_matrix_homeserver", new=lambda *_args, **_kwargs: "http://localhost:8008")
    @patch("mindroom.bot.login_agent_user")
    @patch("mindroom.bot.AgentBot.ensure_user_account")
    @patch("mindroom.bot.interactive.init_persistence")
    @patch("mindroom.config.main.load_config")
    async def test_agent_bot_start(
        self,
        mock_load_config: MagicMock,
        mock_init_persistence: MagicMock,
        mock_ensure_user: AsyncMock,
        mock_login: AsyncMock,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test starting an agent bot."""
        mock_client = AsyncMock()
        # add_event_callback is a sync method, not async
        mock_client.add_event_callback = MagicMock()
        mock_client.add_response_callback = MagicMock()
        mock_login.return_value = mock_client

        # Mock ensure_user_account to not change the agent_user
        mock_ensure_user.return_value = None

        mock_load_config.return_value = self.create_mock_config(tmp_path)
        config = mock_load_config.return_value

        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        await bot.start()

        assert bot.running
        assert bot.client == mock_client
        # The bot calls ensure_setup which calls ensure_user_account
        # and then login with whatever user account was ensured
        assert mock_login.called
        mock_init_persistence.assert_called_once_with(runtime_paths_for(config).storage_root)
        assert (
            mock_client.add_event_callback.call_count == 14
        )  # invite, message, redaction, reaction, audio, image/file/video, unknown-event, megolm callbacks
        registered_event_types = [call.args[1] for call in mock_client.add_event_callback.call_args_list]
        assert nio.MegolmEvent in registered_event_types  # undecryptable events must not vanish silently

    @pytest.mark.asyncio
    @patch("mindroom.config.main.load_config")
    async def test_decrypt_failure_ingress_applies_sender_authorization(
        self,
        mock_load_config: MagicMock,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """The decrypt-failure path must gate senders like every other ingress path."""
        mock_load_config.return_value = self.create_mock_config(tmp_path)
        config = mock_load_config.return_value
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock(spec=nio.MegolmEvent)
        event.sender = "@stranger:localhost"

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=False) as gate,
            patch("mindroom.bot.handle_decrypt_failure", new=AsyncMock()) as handler,
        ):
            await bot._on_decryption_failure(room, event)

        handler.assert_not_awaited()
        gate.assert_called_once_with(event.sender, config, room.room_id, bot.runtime_paths)

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.bot.handle_decrypt_failure", new=AsyncMock()) as handler,
        ):
            await bot._on_decryption_failure(room, event)

        handler.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("mindroom.constants.runtime_matrix_homeserver", new=lambda *_args, **_kwargs: "http://localhost:8008")
    @patch("mindroom.bot.login_agent_user")
    @patch("mindroom.bot.AgentBot.ensure_user_account")
    @patch("mindroom.bot.interactive.init_persistence")
    async def test_agent_bot_start_rebuilds_identity_bound_runtime_after_login_user_id_change(
        self,
        mock_init_persistence: MagicMock,
        mock_ensure_user: AsyncMock,
        mock_login: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Login may canonicalize the Matrix ID before sync callbacks are registered."""
        stale_user_id = "@mindroom_general:localhost"
        actual_user_id = "@actual_general:localhost"
        config = _runtime_bound_config(
            Config(
                agents={"general": AgentConfig(display_name="GeneralAgent", model="default")},
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
        )
        agent_user = AgentMatrixUser(
            agent_name="general",
            user_id=stale_user_id,
            display_name="GeneralAgent",
            password=TEST_PASSWORD,
        )
        mock_client = AsyncMock()
        mock_client.user_id = actual_user_id
        mock_client.add_event_callback = MagicMock()
        mock_client.add_response_callback = MagicMock()
        mock_ensure_user.return_value = None

        async def _login_with_actual_identity(
            _homeserver: str,
            login_user: AgentMatrixUser,
            *_args: object,
            **_kwargs: object,
        ) -> object:
            login_user.user_id = actual_user_id
            login_user.__dict__.pop("matrix_id", None)
            return mock_client

        mock_login.side_effect = _login_with_actual_identity

        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        stale_resolver = bot._conversation_resolver

        await bot.start()

        assert bot.running is True
        assert bot.matrix_id.full_id == actual_user_id
        assert bot._conversation_resolver is not stale_resolver
        assert bot._conversation_resolver.deps.matrix_id.full_id == actual_user_id
        assert bot._tool_runtime_support.matrix_id.full_id == actual_user_id
        assert bot._response_runner.deps.matrix_full_id == actual_user_id
        assert bot._turn_policy.deps.matrix_id.full_id == actual_user_id
        assert bot._turn_controller.deps.matrix_id.full_id == actual_user_id
        mock_init_persistence.assert_called_once_with(runtime_paths_for(config).storage_root)
        assert mock_client.add_event_callback.call_count == 14

    @pytest.mark.asyncio
    @patch("mindroom.constants.runtime_matrix_homeserver", new=lambda *_args, **_kwargs: "http://localhost:8008")
    @patch("mindroom.bot.login_agent_user")
    @patch("mindroom.bot.AgentBot.ensure_user_account")
    async def test_agent_bot_start_revalidates_identity_after_login(
        self,
        mock_ensure_user: AsyncMock,
        mock_login: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Authenticated Matrix IDs must not drift into another configured entity ID."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(display_name="GeneralAgent", model="default"),
                    "writer": AgentConfig(display_name="WriterAgent", model="default"),
                },
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        persist_entity_accounts(
            config,
            runtime_paths,
            usernames={
                ROUTER_AGENT_NAME: "actual_router",
                "general": "actual_general",
                "writer": "actual_writer",
            },
        )
        agent_user = AgentMatrixUser(
            agent_name="general",
            user_id="@actual_general:localhost",
            display_name="GeneralAgent",
            password=TEST_PASSWORD,
        )
        mock_client = AsyncMock()
        mock_client.close = AsyncMock()
        mock_login.return_value = mock_client
        mock_ensure_user.return_value = None

        async def _login_with_duplicate_identity(*_args: object, **_kwargs: object) -> object:
            state = MatrixState.load(runtime_paths=runtime_paths)
            state.add_account("agent_general", "actual_writer", TEST_PASSWORD, domain="localhost")
            state.save(runtime_paths=runtime_paths)
            return mock_client

        mock_login.side_effect = _login_with_duplicate_identity
        orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
        orchestrator.config = config
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.orchestrator = orchestrator
        _install_runtime_cache_support(bot)

        with pytest.raises(PermanentStartupError, match="actual_writer"):
            await bot.start()

        assert bot.running is False
        assert bot.client is None
        mock_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("mindroom.constants.runtime_matrix_homeserver", new=lambda *_args, **_kwargs: "http://localhost:8008")
    @patch("mindroom.bot.login_agent_user")
    @patch("mindroom.bot.AgentBot.ensure_user_account")
    async def test_agent_bot_enters_sync_without_startup_cleanup(
        self,
        mock_ensure_user: AsyncMock,
        mock_login: AsyncMock,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """AgentBot should enter sync directly because orchestrator owns stale cleanup."""
        config = self._config_for_storage(tmp_path)
        call_order: list[str] = []
        mock_client = AsyncMock()
        mock_client.add_event_callback = MagicMock()
        mock_client.add_response_callback = MagicMock()

        async def _sync_forever(*_args: object, **_kwargs: object) -> None:
            call_order.append("sync")

        mock_client.sync_forever = AsyncMock(side_effect=_sync_forever)
        mock_login.return_value = mock_client
        mock_ensure_user.return_value = None

        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        await bot.start()
        await bot.sync_forever()

        assert call_order == ["sync"]

    @pytest.mark.asyncio
    async def test_agent_bot_try_start_reraises_permanent_startup_error(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Permanent startup failures should stop retrying immediately."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))

        with (
            patch.object(
                bot,
                "start",
                new=AsyncMock(side_effect=PermanentMatrixStartupError("boom")),
            ) as mock_start,
            pytest.raises(PermanentMatrixStartupError, match="boom"),
        ):
            await bot.try_start()

        mock_start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_agent_bot_stop(self, mock_agent_user: AgentMatrixUser, tmp_path: Path) -> None:
        """Test stopping an agent bot."""
        config = self._config_for_storage(tmp_path)

        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        bot.client.next_batch = "s_test_token"
        bot.running = True

        await bot.stop()

        assert not bot.running
        bot.client.close.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("accept_invites", "expected_join_calls"), [(True, 1), (False, 0)])
    async def test_agent_bot_on_invite(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        accept_invites: bool,
        expected_join_calls: int,
    ) -> None:
        """Test handling room invitations."""
        config = self._config_for_storage(tmp_path)
        config.agents[mock_agent_user.agent_name].accept_invites = accept_invites

        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        bot.client = AsyncMock()

        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"
        mock_room.canonical_alias = None

        mock_event = MagicMock()
        mock_event.sender = "@user:localhost"

        join_room = AsyncMock(return_value=True)
        with (
            patch("mindroom.bot_room_lifecycle.is_authorized_sender", return_value=True),
            patch("mindroom.bot_room_lifecycle.join_room", join_room),
        ):
            await bot._on_invite(mock_room, mock_event)

        assert join_room.await_count == expected_join_calls

    @pytest.mark.asyncio
    async def test_agent_bot_on_message_ignore_own(self, mock_agent_user: AgentMatrixUser, tmp_path: Path) -> None:
        """Test that agent ignores its own messages."""
        config = self._config_for_storage(tmp_path)

        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        bot.client = AsyncMock()

        mock_room = MagicMock()
        mock_event = MagicMock()
        mock_event.sender = "@mindroom_calculator:localhost"  # Bot's own ID

        await bot._on_message(mock_room, mock_event)
        await drain_coalescing(bot)

        # Should not send any response
        bot.client.room_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_bot_on_message_ignore_other_agents(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test that agent ignores messages from other agents."""
        config = self._config_for_storage(tmp_path)

        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        bot.client = AsyncMock()

        mock_room = MagicMock()
        mock_event = MagicMock()
        mock_event.sender = "@mindroom_general:localhost"  # Another agent

        await bot._on_message(mock_room, mock_event)
        await drain_coalescing(bot)

        # Should not send any response
        bot.client.room_send.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enable_streaming", [True, False])
    @patch("mindroom.matrix.conversation_cache.MatrixConversationCache.get_latest_thread_event_id_if_needed")
    @patch("mindroom.response_runner.ai_response")
    @patch("mindroom.response_runner.stream_agent_response")
    @patch("mindroom.conversation_resolver.ConversationResolver.fetch_thread_history")
    @patch("mindroom.response_runner.should_use_streaming")
    async def test_agent_bot_on_message_mentioned(  # noqa: PLR0915
        self,
        mock_should_use_streaming: AsyncMock,
        mock_fetch_history: AsyncMock,
        mock_stream_agent_response: AsyncMock,
        mock_ai_response: AsyncMock,
        mock_get_latest_thread: AsyncMock,
        enable_streaming: bool,
        mock_agent_user: AgentMatrixUser,  # noqa: ARG002
        tmp_path: Path,
    ) -> None:
        """Test agent bot responding to mentions with both streaming and non-streaming modes."""

        # Mock streaming response - return an async generator
        async def mock_streaming_response() -> AsyncGenerator[str, None]:
            yield "Test"
            yield " response"

        mock_stream_agent_response.return_value = mock_streaming_response()
        mock_ai_response.return_value = "Test response"
        mock_fetch_history.return_value = thread_history_result([], is_full_history=True)
        # Mock the presence check to return same value as enable_streaming
        mock_should_use_streaming.return_value = enable_streaming
        # Mock get_latest_thread_event_id_if_needed
        mock_get_latest_thread.return_value = "latest_thread_event"

        config = self._config_for_storage(tmp_path)
        mention_id = f"@mindroom_calculator:{config.get_domain(runtime_paths_for(config))}"
        agent_user = AgentMatrixUser(
            agent_name="calculator",
            password=TEST_PASSWORD,
            display_name="CalculatorAgent",
            user_id=mention_id,
        )

        bot = AgentBot(
            agent_user,
            tmp_path,
            rooms=["!test:localhost"],
            enable_streaming=enable_streaming,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)

        # Mock presence check to return user online when streaming is enabled
        # We need to create a proper mock response that will be returned by get_presence
        if enable_streaming:
            # Create a mock that looks like PresenceGetResponse
            mock_presence_response = MagicMock()
            mock_presence_response.presence = "online"
            mock_presence_response.last_active_ago = 1000

            # Make get_presence return this response (as a coroutine since it's async)
            async def mock_get_presence(user_id: str) -> MagicMock:  # noqa: ARG001
                return mock_presence_response

            bot.client.get_presence = mock_get_presence
        else:
            mock_presence_response = MagicMock()
            mock_presence_response.presence = "offline"
            mock_presence_response.last_active_ago = 3600000

            async def mock_get_presence(user_id: str) -> MagicMock:  # noqa: ARG001
                return mock_presence_response

            bot.client.get_presence = mock_get_presence

        # Mock successful room_send response
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        bot.client.room_send.return_value = mock_send_response

        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"

        mock_event = MagicMock()
        mock_event.sender = "@user:localhost"
        mock_event.body = f"{mention_id}: What's 2+2?"
        mock_event.event_id = "event123"
        mock_event.server_timestamp = 1_774_019_700_000
        mock_event.source = {
            "content": {
                "body": f"{mention_id}: What's 2+2?",
                "m.mentions": {"user_ids": [mention_id]},
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root_id"},
            },
        }

        snapshot = ThreadHistoryResult([], is_full_history=False)
        history = ThreadHistoryResult([], is_full_history=True)

        with (
            patch.object(bot._conversation_cache, "get_dispatch_thread_snapshot", AsyncMock(return_value=snapshot)),
            patch.object(bot._conversation_cache, "get_dispatch_thread_history", AsyncMock(return_value=history)),
        ):
            await bot._on_message(mock_room, mock_event)
            await drain_coalescing(bot)

        # Should call AI and send response based on streaming mode
        if enable_streaming:
            mock_stream_agent_response.assert_called_once()
            stream_args = mock_stream_agent_response.call_args.args
            stream_kwargs = mock_stream_agent_response.call_args.kwargs
            stream_ctx = stream_args[0]
            assert stream_ctx.entity_label == "calculator"
            assert stream_kwargs["prompt"] == f"{mention_id}: What's 2+2?"
            assert stream_kwargs["model_prompt"] == f"{mention_id}: What's 2+2?"
            assert stream_kwargs["current_timestamp_ms"] == 1_774_019_700_000.0
            assert stream_ctx.session_id == "!test:localhost:$thread_root_id"
            assert stream_kwargs["runtime_paths"].storage_root == runtime_paths_for(config).storage_root
            assert stream_kwargs["config"] == config
            assert stream_kwargs["thread_history"] == []
            assert stream_ctx.room_id == "!test:localhost"
            assert stream_kwargs["knowledge"] is None
            assert stream_ctx.requester_id == "@user:localhost"
            assert isinstance(stream_ctx.run_id, str)
            assert stream_ctx.run_id
            assert stream_kwargs["media"] == MediaInputs()
            assert stream_ctx.reply_to_event_id == "event123"
            assert stream_kwargs["show_tool_calls"] is True
            assert stream_kwargs["run_metadata_collector"] == {}
            mock_ai_response.assert_not_called()
            # With streaming and stop button: initial message + reaction + edits
            # Note: The exact count may vary based on implementation
            assert bot.client.room_send.call_count >= 2
        else:
            mock_ai_response.assert_called_once()
            ai_args = mock_ai_response.call_args.args
            ai_kwargs = mock_ai_response.call_args.kwargs
            ai_ctx = ai_args[0]
            assert ai_ctx.entity_label == "calculator"
            assert ai_kwargs["prompt"] == f"{mention_id}: What's 2+2?"
            assert ai_kwargs["model_prompt"] == f"{mention_id}: What's 2+2?"
            assert ai_kwargs["current_timestamp_ms"] == 1_774_019_700_000.0
            assert ai_ctx.session_id == "!test:localhost:$thread_root_id"
            assert ai_kwargs["runtime_paths"].storage_root == runtime_paths_for(config).storage_root
            assert ai_kwargs["config"] == config
            assert ai_kwargs["thread_history"] == []
            assert ai_ctx.room_id == "!test:localhost"
            assert ai_kwargs["knowledge"] is None
            assert ai_ctx.requester_id == "@user:localhost"
            assert isinstance(ai_ctx.run_id, str)
            assert ai_ctx.run_id
            assert ai_kwargs["media"] == MediaInputs()
            assert ai_ctx.reply_to_event_id == "event123"
            assert ai_kwargs["show_tool_calls"] is True
            assert ai_kwargs["collect_streamed_response"] is True
            assert ai_kwargs["tool_trace_collector"] == []
            assert ai_kwargs["run_metadata_collector"] == {}
            mock_stream_agent_response.assert_not_called()
            # With stop button support: initial + reaction + final
            assert bot.client.room_send.call_count >= 2

    @pytest.mark.asyncio
    async def test_agent_bot_on_message_not_mentioned(self, mock_agent_user: AgentMatrixUser, tmp_path: Path) -> None:
        """Test agent bot not responding when not mentioned."""
        config = self._config_for_storage(tmp_path)

        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()

        mock_room = MagicMock()
        mock_event = MagicMock()
        mock_event.sender = "@user:localhost"
        mock_event.body = "Hello everyone!"
        mock_event.source = {"content": {"body": "Hello everyone!"}}

        await bot._on_message(mock_room, mock_event)
        await drain_coalescing(bot)

        # Should not send any response
        bot.client.room_send.assert_not_called()

    def test_build_tool_runtime_context_populates_room_when_cached(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Runtime context should include the room object when the client cache has it."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        room_id = "!test:localhost"
        local_room = MagicMock(spec=nio.MatrixRoom)
        local_room.room_id = room_id
        bot.client = MagicMock(rooms={room_id: local_room})
        bot.event_cache = MagicMock()
        bot.orchestrator = MagicMock()

        target = MessageTarget.resolve(room_id=room_id, thread_id="$thread", reply_to_event_id="$event")
        context = bot._tool_runtime_support.build_context(target, user_id="@user:localhost")

        assert context is not None
        assert context.client is bot.client
        assert context.room is local_room
        assert context.thread_id == "$thread"
        assert context.requester_id == "@user:localhost"

    def test_build_tool_runtime_context_room_none_when_not_cached(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Runtime context should have room=None when the client has no cache entry."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        room_id = "!test:localhost"
        bot.client = MagicMock(rooms={})
        bot.event_cache = MagicMock()
        bot.orchestrator = MagicMock()

        target = MessageTarget.resolve(room_id=room_id, thread_id="$thread", reply_to_event_id="$event")
        context = bot._tool_runtime_support.build_context(target, user_id="@user:localhost")

        assert context is not None
        assert context.room is None

    def test_build_tool_runtime_context_includes_event_cache(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Runtime context should expose the shared Matrix event cache."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        bot.event_cache = MagicMock()

        target = MessageTarget.resolve(room_id="!test:localhost", thread_id="$thread", reply_to_event_id="$event")
        context = bot._tool_runtime_support.build_context(target, user_id="@user:localhost")

        assert context is not None
        assert context.event_cache is bot.event_cache

    def test_agent_bot_init_does_not_resolve_cache_path_eagerly(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """AgentBot construction should not resolve cache paths before injected startup support is bound."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                    ),
                },
            ),
            tmp_path,
        )
        config.cache = MagicMock()
        config.cache.resolve_db_path.side_effect = AssertionError("cache path resolution should be lazy")

        AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))

        config.cache.resolve_db_path.assert_not_called()

    def test_build_tool_runtime_context_returns_none_when_client_unavailable(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Runtime context should be None when no Matrix client is available."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = None

        target = MessageTarget.resolve(room_id="!test:localhost", thread_id="$thread", reply_to_event_id="$event")
        context = bot._tool_runtime_support.build_context(target, user_id="@user:localhost")

        assert context is None

    def test_build_tool_runtime_context_returns_none_when_event_cache_unavailable(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Runtime context should be None until Matrix runtime support is initialized."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        bot._runtime_view.event_cache = None

        target = MessageTarget.resolve(room_id="!test:localhost", thread_id="$thread", reply_to_event_id="$event")
        context = bot._tool_runtime_support.build_context(target, user_id="@user:localhost")

        assert context is None

    def test_build_tool_runtime_context_sets_attachment_scope_and_thread_root(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Tool runtime context should carry attachment scope and effective thread root."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        bot.event_cache = MagicMock()

        target = MessageTarget.resolve(room_id="!test:localhost", thread_id=None, reply_to_event_id="$root_event")
        context = bot._tool_runtime_support.build_context(
            target,
            user_id="@user:localhost",
            attachment_ids=["att_1"],
        )

        assert context is not None
        assert context.thread_id is None
        assert context.resolved_thread_id is None
        assert context.attachment_ids == ("att_1",)

    def test_build_tool_runtime_context_preserves_room_mode_source_thread_id(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Tool runtime context should preserve source thread provenance when delivery is room-level."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        bot.event_cache = MagicMock()
        target = MessageTarget(
            room_id="!test:localhost",
            source_thread_id="$raw-thread",
            resolved_thread_id=None,
            reply_to_event_id="$root_event",
            session_id="!test:localhost",
        )

        context = bot._tool_runtime_support.build_context(target, user_id="@user:localhost")

        assert context is not None
        assert context.thread_id == "$raw-thread"
        assert context.resolved_thread_id is None
        assert MessageTarget.from_runtime_context(context).source_thread_id == "$raw-thread"

    def test_response_lifecycle_lock_uses_resolved_thread_root(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Different first-turn thread roots should not share one lifecycle lock."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))

        first = MessageTarget.resolve(
            room_id="!test:localhost",
            thread_id=None,
            reply_to_event_id="$root_a",
        ).with_thread_root("$root_a")
        second = MessageTarget.resolve(
            room_id="!test:localhost",
            thread_id=None,
            reply_to_event_id="$root_b",
        ).with_thread_root("$root_b")

        coordinator = unwrap_extracted_collaborator(bot._response_runner)
        lifecycle = coordinator._lifecycle_coordinator
        assert lifecycle._response_lifecycle_lock(first) is lifecycle._response_lifecycle_lock(first)
        assert lifecycle._response_lifecycle_lock(first) is not lifecycle._response_lifecycle_lock(second)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("handler_name", "marks_responded"),
        [
            ("message", True),
            ("image", True),
            ("voice", True),
            ("file", True),
            ("reaction", False),
        ],
    )
    async def test_sender_unauthorized_parity_across_handlers(
        self,
        handler_name: str,
        marks_responded: bool,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Unauthorized senders should follow the expected per-handler tracking behavior."""
        config = _runtime_bound_config(
            Config(
                agents={"calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!test:localhost"])},
                voice={"enabled": True},
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        tracker.has_responded.return_value = False

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        room.users = {"@mindroom_calculator:localhost": MagicMock(), "@user:localhost": MagicMock()}

        event = self._make_handler_event(handler_name, sender="@user:localhost", event_id=f"${handler_name}_unauth")

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=False),
            patch("mindroom.ingress_validation.is_authorized_sender", return_value=False),
        ):
            await self._invoke_handler(bot, handler_name, room, event)

        if marks_responded:
            tracker.record_handled_turn.assert_called_once_with(
                TurnRecord.create([event.event_id]),
            )
        else:
            tracker.record_handled_turn.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("handler_name", "marks_responded"),
        [
            ("message", True),
            ("image", True),
            ("voice", True),
            ("file", True),
            ("reaction", False),
        ],
    )
    async def test_reply_permissions_denied_parity_across_handlers(
        self,
        handler_name: str,
        marks_responded: bool,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Reply-permission denial should follow the expected per-handler tracking behavior."""
        config = _runtime_bound_config(
            Config(
                agents={"calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!test:localhost"])},
                voice={"enabled": True},
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        tracker.has_responded.return_value = False

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        room.users = {"@mindroom_calculator:localhost": MagicMock(), "@user:localhost": MagicMock()}

        event = self._make_handler_event(handler_name, sender="@user:localhost", event_id=f"${handler_name}_denied")

        if handler_name == "image":
            bot._conversation_resolver.extract_message_context = AsyncMock(
                return_value=MessageContext(
                    am_i_mentioned=False,
                    is_thread=False,
                    thread_id=None,
                    thread_history=[],
                    mentioned_agents=[],
                    has_non_agent_mentions=False,
                ),
            )

        wrap_extracted_collaborators(bot, "_turn_policy")
        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
            patch.object(bot._turn_policy, "can_reply_to_sender", return_value=False),
            patch("mindroom.text_ingress_dispatch.is_dm_room", new_callable=AsyncMock, return_value=False),
        ):
            await self._invoke_handler(bot, handler_name, room, event)

        if marks_responded:
            tracker.record_handled_turn.assert_called_once_with(
                TurnRecord.create([event.event_id]),
            )
        else:
            tracker.record_handled_turn.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enable_streaming", [True, False])
    @patch("mindroom.config.main.load_config")
    @patch("mindroom.teams.resolve_agent_knowledge_access")
    @patch("mindroom.teams.create_agent")
    @patch("mindroom.model_loading.get_model_instance")
    @patch("mindroom.teams.Team.arun")
    @patch("mindroom.response_runner.ai_response")
    @patch("mindroom.response_runner.stream_agent_response")
    @patch("mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_snapshot")
    @patch("mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_history")
    @patch("mindroom.response_runner.should_use_streaming")
    @patch("mindroom.matrix.conversation_cache.MatrixConversationCache.get_latest_thread_event_id_if_needed")
    async def test_agent_bot_thread_response(  # noqa: PLR0915
        self,
        mock_get_latest_thread: AsyncMock,
        mock_should_use_streaming: AsyncMock,
        mock_fetch_history: AsyncMock,
        mock_fetch_snapshot: AsyncMock,
        mock_stream_agent_response: AsyncMock,
        mock_ai_response: AsyncMock,
        mock_team_arun: AsyncMock,
        mock_get_model_instance: MagicMock,
        mock_create_agent: MagicMock,
        mock_resolve_agent_knowledge_access: MagicMock,
        mock_load_config: MagicMock,
        enable_streaming: bool,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test agent bot thread response behavior based on agent participation."""
        # Use the helper method to create mock config
        config = self._config_for_storage(tmp_path)
        mock_load_config.return_value = config

        # Mock get_model_instance to return a mock model
        mock_model = Ollama(id="test-model")
        mock_get_model_instance.return_value = mock_model
        mock_resolve_agent_knowledge_access.return_value = _KnowledgeResolution(knowledge=None)
        fake_member = MagicMock()
        fake_member.name = "MockAgent"
        fake_member.instructions = []
        mock_create_agent.return_value = fake_member

        # Mock get_latest_thread_event_id_if_needed to return a valid event ID
        mock_get_latest_thread.return_value = "latest_thread_event"

        bot = AgentBot(
            mock_agent_user,
            tmp_path,
            config,
            runtime_paths_for(config),
            rooms=["!test:localhost"],
            enable_streaming=enable_streaming,
        )
        _install_runtime_cache_support(bot)
        bot.client = AsyncMock()

        # Mock orchestrator with agent_bots
        mock_orchestrator = MagicMock()
        mock_agent_bot = MagicMock()
        mock_agent_bot.agent = MagicMock()
        mock_orchestrator.agent_bots = {"calculator": mock_agent_bot, "general": mock_agent_bot}
        mock_orchestrator.current_config = config
        mock_orchestrator.config = config  # This is what teams.py uses
        mock_orchestrator.runtime_paths = runtime_paths_for(config)
        bot.orchestrator = mock_orchestrator

        # Mock successful room_send response
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        bot.client.room_send.return_value = mock_send_response

        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"
        # Thread team resolution now uses room-visible membership, so include the
        # other participating agent in the room fixture as well.
        mock_room.users = {
            mock_agent_user.user_id: MagicMock(),
            entity_ids(config, runtime_paths_for(config))["general"].full_id: MagicMock(),
        }

        # Test 1: Thread with only this agent - should respond without mention
        test1_history = [
            _visible_message(
                sender="@user:localhost",
                body="Previous message",
                timestamp=123,
                event_id="prev1",
            ),
            _visible_message(
                sender=mock_agent_user.user_id,
                body="My previous response",
                timestamp=124,
                event_id="prev2",
            ),
        ]
        mock_fetch_history.return_value = thread_history_result(test1_history, is_full_history=True)
        mock_fetch_snapshot.return_value = thread_history_result(test1_history, is_full_history=True)

        # Mock streaming response - return an async generator
        async def mock_streaming_response() -> AsyncGenerator[str, None]:
            yield "Thread"
            yield " response"

        mock_stream_agent_response.return_value = mock_streaming_response()
        mock_ai_response.return_value = "Thread response"

        # Mock team arun to return either a string or async iterator based on stream parameter

        async def mock_team_stream() -> AsyncGenerator[Any, None]:
            # Yield member content events (using display names as Agno would)
            event1 = MagicMock(spec=RunContentEvent)
            event1.event = "RunContent"  # Set the event type
            event1.agent_name = "CalculatorAgent"  # Display name, not short name
            event1.content = "Team response chunk 1"
            yield event1

            event2 = MagicMock(spec=RunContentEvent)
            event2.event = "RunContent"  # Set the event type
            event2.agent_name = "GeneralAgent"  # Display name, not short name
            event2.content = "Team response chunk 2"
            yield event2

            # Yield final team response
            team_response = MagicMock(spec=TeamRunOutput)
            team_response.content = "Team consensus"
            team_response.member_responses = []
            team_response.messages = []
            yield team_response

        def mock_team_arun_side_effect(*args: Any, **kwargs: Any) -> Any:  # noqa: ARG001, ANN401
            if kwargs.get("stream"):
                return mock_team_stream()
            return "Team response"

        mock_team_arun.side_effect = mock_team_arun_side_effect
        # Mock the presence check to return same value as enable_streaming
        mock_should_use_streaming.return_value = enable_streaming

        mock_event = MagicMock()
        mock_event.sender = "@user:localhost"
        mock_event.body = "Thread message without mention"
        mock_event.event_id = "event123"
        mock_event.server_timestamp = 126
        mock_event.source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "thread_root",
                },
            },
        }

        await bot._on_message(mock_room, mock_event)
        await drain_coalescing(bot)

        # Should respond as only agent in thread
        if enable_streaming:
            mock_stream_agent_response.assert_called_once()
            mock_ai_response.assert_not_called()
            # With streaming and stop button support
            assert bot.client.room_send.call_count >= 2
        else:
            mock_ai_response.assert_called_once()
            mock_stream_agent_response.assert_not_called()
            # With stop button support: initial + reaction + final
            assert bot.client.room_send.call_count >= 2

        # Reset mocks
        mock_stream_agent_response.reset_mock()
        mock_ai_response.reset_mock()
        mock_team_arun.reset_mock()
        bot.client.room_send.reset_mock()
        mock_fetch_history.reset_mock()

        # Test 2: Thread with multiple agents - should NOT respond without mention
        test2_history = [
            _visible_message(sender="@user:localhost", body="Previous message", timestamp=123, event_id="prev1"),
            _visible_message(sender=mock_agent_user.user_id, body="My response", timestamp=124, event_id="prev2"),
            _visible_message(
                sender=entity_ids(config, runtime_paths_for(config))["general"].full_id
                if "general" in entity_ids(config, runtime_paths_for(config))
                else "@mindroom_general:localhost",
                body="Another agent response",
                timestamp=125,
                event_id="prev3",
            ),
        ]
        mock_fetch_history.return_value = thread_history_result(test2_history, is_full_history=True)
        mock_fetch_snapshot.return_value = thread_history_result(test2_history, is_full_history=True)

        # Create a new event with a different ID for Test 2
        mock_event_2 = MagicMock()
        mock_event_2.sender = "@user:localhost"
        mock_event_2.body = "Thread message without mention"
        mock_event_2.event_id = "event456"  # Different event ID
        mock_event_2.server_timestamp = 127
        mock_event_2.source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "thread_root",
                },
            },
        }

        await bot._on_message(mock_room, mock_event_2)
        await drain_coalescing(bot)

        # Should form team and send a structured streaming team response
        mock_stream_agent_response.assert_not_called()
        mock_ai_response.assert_not_called()
        mock_team_arun.assert_called_once()
        # Structured streaming sends an initial message and one or more edits
        assert bot.client.room_send.call_count >= 1

        # Reset mocks
        mock_stream_agent_response.reset_mock()
        mock_ai_response.reset_mock()
        mock_team_arun.reset_mock()
        bot.client.room_send.reset_mock()

        # Test 3: Thread with multiple agents WITH mention - should respond
        mock_event_with_mention = MagicMock()
        mock_event_with_mention.sender = "@user:localhost"
        mock_event_with_mention.body = "@mindroom_calculator:localhost What's 2+2?"
        mock_event_with_mention.event_id = "event789"  # Unique event ID for Test 3
        mock_event_with_mention.server_timestamp = 128
        mock_event_with_mention.source = {
            "content": {
                "body": "@mindroom_calculator:localhost What's 2+2?",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "thread_root",
                },
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
            },
        }

        # Set up fresh async generator for the second call
        async def mock_streaming_response2() -> AsyncGenerator[str, None]:
            yield "Mentioned"
            yield " response"

        mock_stream_agent_response.return_value = mock_streaming_response2()
        mock_ai_response.return_value = "Mentioned response"

        await bot._on_message(mock_room, mock_event_with_mention)
        await drain_coalescing(bot)

        # Should respond when explicitly mentioned
        if enable_streaming:
            mock_stream_agent_response.assert_called_once()
            mock_ai_response.assert_not_called()
            # With streaming and stop button support
            assert bot.client.room_send.call_count >= 2
        else:
            mock_ai_response.assert_called_once()
            mock_stream_agent_response.assert_not_called()
            # With stop button support: initial + reaction + final
            assert bot.client.room_send.call_count >= 2

    @pytest.mark.asyncio
    async def test_agent_bot_skips_already_responded_messages(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test that agent bot skips messages it has already responded to."""
        config = self._config_for_storage(tmp_path)

        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        bot.client = AsyncMock()

        # Mark an event as already responded
        _turn_store(bot).record_turn(TurnRecord.create(["event123"]))

        # Create mock room and event
        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"

        mock_event = MagicMock()
        mock_event.sender = "@user:localhost"
        mock_event.body = "@mindroom_calculator:localhost: What's 2+2?"
        mock_event.event_id = "event123"  # Same event ID
        mock_event.source = {
            "content": {
                "body": "@mindroom_calculator:localhost: What's 2+2?",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root_id"},
            },
        }

        await bot._on_message(mock_room, mock_event)
        await drain_coalescing(bot)

        # Should not send any message since it already responded
        bot.client.room_send.assert_not_called()
