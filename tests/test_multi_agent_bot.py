"""Tests for the multi-agent bot system."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import nio
import pytest
from agno.knowledge.document import Document
from agno.knowledge.knowledge import Knowledge
from agno.media import Image
from agno.models.ollama import Ollama
from agno.run.agent import RunContentEvent
from agno.run.team import TeamRunOutput

from mindroom.bot import AgentBot, MessageContext, MultiAgentOrchestrator, MultiKnowledgeVectorDb
from mindroom.config import AgentConfig, AuthorizationConfig, Config, DefaultsConfig, KnowledgeBaseConfig, ModelConfig
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.users import AgentMatrixUser
from mindroom.teams import TeamFormationDecision, TeamMode
from mindroom.tool_events import ToolTraceEntry

from .conftest import TEST_PASSWORD

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


@dataclass
class MockConfig:
    """Mock configuration for testing."""

    agents: dict[str, Any] = None

    def __post_init__(self) -> None:
        """Initialize agents dictionary if not provided."""
        if self.agents is None:
            self.agents = {
                "calculator": MagicMock(rooms=["lobby", "science", "analysis"]),
                "general": MagicMock(rooms=["lobby", "help"]),
            }


@pytest.fixture
def mock_agent_user() -> AgentMatrixUser:
    """Create a mock agent user."""
    return AgentMatrixUser(
        agent_name="calculator",
        password=TEST_PASSWORD,
        display_name="CalculatorAgent",
        user_id="@mindroom_calculator:localhost",
    )


@pytest.fixture
def mock_agent_users() -> dict[str, AgentMatrixUser]:
    """Create mock agent users."""
    return {
        "calculator": AgentMatrixUser(
            agent_name="calculator",
            password=TEST_PASSWORD,
            display_name="CalculatorAgent",
            user_id="@mindroom_calculator:localhost",
        ),
        "general": AgentMatrixUser(
            agent_name="general",
            password=TEST_PASSWORD,
            display_name="GeneralAgent",
            user_id="@mindroom_general:localhost",
        ),
    }


@dataclass
class _SyncStubVectorDb:
    documents: list[Document]

    def search(
        self,
        *,
        query: str,
        limit: int,
        filters: dict[str, Any] | list[Any] | None = None,
    ) -> list[Document]:
        _ = (query, filters)
        return self.documents[:limit]


@dataclass
class _AsyncStubVectorDb(_SyncStubVectorDb):
    async def async_search(
        self,
        *,
        query: str,
        limit: int,
        filters: dict[str, Any] | list[Any] | None = None,
    ) -> list[Document]:
        _ = (query, filters)
        return self.documents[:limit]


@dataclass
class _FailingStubVectorDb:
    error_message: str = "search failed"

    def search(
        self,
        *,
        query: str,
        limit: int,
        filters: dict[str, Any] | list[Any] | None = None,
    ) -> list[Document]:
        _ = (query, limit, filters)
        raise RuntimeError(self.error_message)

    async def async_search(
        self,
        *,
        query: str,
        limit: int,
        filters: dict[str, Any] | list[Any] | None = None,
    ) -> list[Document]:
        _ = (query, limit, filters)
        raise RuntimeError(self.error_message)


class TestAgentBot:
    """Test cases for AgentBot class."""

    def create_mock_config(self) -> MagicMock:
        """Create a mock config for testing."""
        mock_config = MagicMock()
        mock_config.agents = {
            "calculator": MagicMock(display_name="CalculatorAgent", rooms=["!test:localhost"]),
            "general": MagicMock(display_name="GeneralAgent", rooms=["!test:localhost"]),
        }
        mock_config.teams = {}

        # Create a proper ModelConfig for the default model
        default_model = ModelConfig(provider="test", id="test-model")
        mock_config.models = {"default": default_model}

        mock_config.router = MagicMock(model="default")
        mock_config.get_all_configured_rooms = MagicMock(return_value=["!test:localhost"])

        # Add the ids property for MatrixID lookups
        mock_config.ids = {
            "calculator": MatrixID(username="mindroom_calculator", domain="localhost"),
            "general": MatrixID(username="mindroom_general", domain="localhost"),
            "router": MatrixID(username="mindroom_router", domain="localhost"),
        }
        mock_config.domain = "localhost"
        mock_config.authorization = AuthorizationConfig(default_room_access=True)
        mock_config.get_mindroom_user_id = MagicMock(return_value="@mindroom_user:localhost")

        return mock_config

    @staticmethod
    def _make_handler_event(handler_name: str, *, sender: str, event_id: str) -> MagicMock:
        """Create a minimal event object for a specific handler type."""
        if handler_name == "message":
            event = MagicMock(spec=nio.RoomMessageText)
            event.body = "hello"
            event.source = {"content": {"body": "hello"}}
        elif handler_name == "image":
            event = MagicMock(spec=nio.RoomMessageImage)
            event.body = "image.jpg"
            event.source = {"content": {"body": "image.jpg"}}
        elif handler_name == "voice":
            event = MagicMock(spec=nio.RoomMessageAudio)
            event.body = "voice"
            event.source = {"content": {"body": "voice"}}
        elif handler_name == "reaction":
            event = MagicMock(spec=nio.ReactionEvent)
            event.key = "👍"
            event.reacts_to = "$question"
            event.source = {"content": {}}
        else:  # pragma: no cover - defensive guard for test helper misuse
            msg = f"Unsupported handler: {handler_name}"
            raise ValueError(msg)

        event.sender = sender
        event.event_id = event_id
        return event

    @staticmethod
    async def _invoke_handler(
        bot: AgentBot,
        handler_name: str,
        room: nio.MatrixRoom,
        event: MagicMock,
    ) -> None:
        """Invoke the target handler by name."""
        if handler_name == "message":
            await bot._on_message(room, event)
        elif handler_name == "image":
            await bot._on_image_message(room, event)
        elif handler_name == "voice":
            await bot._on_voice_message(room, event)
        elif handler_name == "reaction":
            await bot._on_reaction(room, event)
        else:  # pragma: no cover - defensive guard for test helper misuse
            msg = f"Unsupported handler: {handler_name}"
            raise ValueError(msg)

    @staticmethod
    def create_config_with_knowledge_bases(
        *,
        assigned_bases: list[str] | None,
        knowledge_bases: dict[str, KnowledgeBaseConfig] | None = None,
    ) -> Config:
        """Create a real config with one calculator agent for knowledge assignment tests."""
        return Config(
            agents={
                "calculator": AgentConfig(
                    display_name="CalculatorAgent",
                    rooms=["!test:localhost"],
                    knowledge_bases=assigned_bases or [],
                ),
            },
            knowledge_bases=knowledge_bases or {},
        )

    def test_knowledge_for_agent_returns_none_when_unassigned(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Unassigned agents should not receive knowledge access."""
        config = self.create_config_with_knowledge_bases(
            assigned_bases=[],
            knowledge_bases={
                "research": KnowledgeBaseConfig(path=str(tmp_path / "kb"), watch=False),
            },
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.orchestrator = MagicMock(knowledge_managers={"research": MagicMock()})

        assert bot._knowledge_for_agent("calculator") is None

    def test_knowledge_for_agent_uses_assigned_base_manager(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Agents should receive knowledge from their assigned knowledge base manager."""
        config = self.create_config_with_knowledge_bases(
            assigned_bases=["research"],
            knowledge_bases={
                "research": KnowledgeBaseConfig(path=str(tmp_path / "kb"), watch=False),
            },
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        expected_knowledge = object()
        manager = MagicMock()
        manager.get_knowledge.return_value = expected_knowledge
        bot.orchestrator = MagicMock(knowledge_managers={"research": manager})

        assert bot._knowledge_for_agent("calculator") is expected_knowledge

    def test_knowledge_for_agent_merges_multiple_assigned_bases(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Agents assigned to multiple bases should search across all assigned bases."""
        config = self.create_config_with_knowledge_bases(
            assigned_bases=["research", "legal"],
            knowledge_bases={
                "research": KnowledgeBaseConfig(path=str(tmp_path / "kb_research"), watch=False),
                "legal": KnowledgeBaseConfig(path=str(tmp_path / "kb_legal"), watch=False),
            },
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config)

        research_vector_db = MagicMock()
        research_vector_db.search.return_value = [
            Document(content="research content 1"),
            Document(content="research content 2"),
            Document(content="research content 3"),
        ]
        research_knowledge = Knowledge(vector_db=research_vector_db)

        legal_vector_db = MagicMock()
        legal_vector_db.search.return_value = [
            Document(content="legal content 1"),
            Document(content="legal content 2"),
            Document(content="legal content 3"),
        ]
        legal_knowledge = Knowledge(vector_db=legal_vector_db)

        research_manager = MagicMock()
        research_manager.get_knowledge.return_value = research_knowledge
        legal_manager = MagicMock()
        legal_manager.get_knowledge.return_value = legal_knowledge

        bot.orchestrator = MagicMock(knowledge_managers={"research": research_manager, "legal": legal_manager})

        combined_knowledge = bot._knowledge_for_agent("calculator")
        assert combined_knowledge is not None

        docs = combined_knowledge.search("knowledge query", max_results=4)
        assert [doc.content for doc in docs] == [
            "research content 1",
            "legal content 1",
            "research content 2",
            "legal content 2",
        ]
        research_vector_db.search.assert_called_once_with(query="knowledge query", limit=4, filters=None)
        legal_vector_db.search.assert_called_once_with(query="knowledge query", limit=4, filters=None)

    def test_multi_knowledge_vector_db_interleaves_sync_results(self) -> None:
        """Round-robin merge should include top results from each knowledge base."""
        vector_db = MultiKnowledgeVectorDb(
            vector_dbs=[
                _SyncStubVectorDb(
                    documents=[
                        Document(content="research 1"),
                        Document(content="research 2"),
                        Document(content="research 3"),
                    ],
                ),
                _SyncStubVectorDb(
                    documents=[
                        Document(content="legal 1"),
                        Document(content="legal 2"),
                        Document(content="legal 3"),
                    ],
                ),
            ],
        )

        docs = vector_db.search(query="knowledge query", limit=4)
        assert [doc.content for doc in docs] == ["research 1", "legal 1", "research 2", "legal 2"]

    def test_multi_knowledge_vector_db_sync_ignores_failing_source(self) -> None:
        """A failing knowledge source should not suppress healthy source results."""
        vector_db = MultiKnowledgeVectorDb(
            vector_dbs=[
                _SyncStubVectorDb(
                    documents=[
                        Document(content="research 1"),
                        Document(content="research 2"),
                    ],
                ),
                _FailingStubVectorDb(error_message="boom"),
            ],
        )

        docs = vector_db.search(query="knowledge query", limit=3)
        assert [doc.content for doc in docs] == ["research 1", "research 2"]

    @pytest.mark.asyncio
    async def test_multi_knowledge_vector_db_interleaves_async_results(self) -> None:
        """Async merge should interleave and support sync-only vector DBs."""
        vector_db = MultiKnowledgeVectorDb(
            vector_dbs=[
                _AsyncStubVectorDb(
                    documents=[
                        Document(content="research 1"),
                        Document(content="research 2"),
                        Document(content="research 3"),
                    ],
                ),
                _SyncStubVectorDb(
                    documents=[
                        Document(content="legal 1"),
                        Document(content="legal 2"),
                        Document(content="legal 3"),
                    ],
                ),
            ],
        )

        docs = await vector_db.async_search(query="knowledge query", limit=5)
        assert [doc.content for doc in docs] == [
            "research 1",
            "legal 1",
            "research 2",
            "legal 2",
            "research 3",
        ]

    @pytest.mark.asyncio
    async def test_multi_knowledge_vector_db_async_ignores_failing_source(self) -> None:
        """Async search should continue returning healthy source results on failures."""
        vector_db = MultiKnowledgeVectorDb(
            vector_dbs=[
                _AsyncStubVectorDb(
                    documents=[
                        Document(content="research 1"),
                        Document(content="research 2"),
                    ],
                ),
                _FailingStubVectorDb(error_message="boom"),
            ],
        )

        docs = await vector_db.async_search(query="knowledge query", limit=3)
        assert [doc.content for doc in docs] == ["research 1", "research 2"]

    @pytest.mark.asyncio
    @patch("mindroom.config.Config.from_yaml")
    async def test_agent_bot_initialization(
        self,
        mock_load_config: MagicMock,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test AgentBot initialization."""
        mock_load_config.return_value = self.create_mock_config()
        config = mock_load_config.return_value

        bot = AgentBot(mock_agent_user, tmp_path, config, rooms=["!test:localhost"])
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
        )
        assert bot_no_stream.enable_streaming is False

    @pytest.mark.asyncio
    @patch("mindroom.bot.MATRIX_HOMESERVER", "http://localhost:8008")
    @patch("mindroom.bot.login_agent_user")
    @patch("mindroom.bot.AgentBot.ensure_user_account")
    @patch("mindroom.config.Config.from_yaml")
    async def test_agent_bot_start(
        self,
        mock_load_config: MagicMock,
        mock_ensure_user: AsyncMock,
        mock_login: AsyncMock,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test starting an agent bot."""
        mock_client = AsyncMock()
        # add_event_callback is a sync method, not async
        mock_client.add_event_callback = MagicMock()
        mock_login.return_value = mock_client

        # Mock ensure_user_account to not change the agent_user
        mock_ensure_user.return_value = None

        mock_load_config.return_value = self.create_mock_config()
        config = mock_load_config.return_value

        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        await bot.start()

        assert bot.running
        assert bot.client == mock_client
        # The bot calls ensure_setup which calls ensure_user_account
        # and then login with whatever user account was ensured
        assert mock_login.called
        assert mock_client.add_event_callback.call_count == 5  # invite, message, reaction, and 2 image callbacks

    @pytest.mark.asyncio
    async def test_agent_bot_stop(self, mock_agent_user: AgentMatrixUser, tmp_path: Path) -> None:
        """Test stopping an agent bot."""
        config = Config.from_yaml()

        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.client = AsyncMock()
        bot.running = True

        await bot.stop()

        assert not bot.running
        bot.client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_agent_bot_on_invite(self, mock_agent_user: AgentMatrixUser, tmp_path: Path) -> None:
        """Test handling room invitations."""
        config = Config.from_yaml()

        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.client = AsyncMock()

        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"

        mock_event = MagicMock()
        mock_event.sender = "@user:localhost"

        await bot._on_invite(mock_room, mock_event)

        bot.client.join.assert_called_once_with("!test:localhost")

    @pytest.mark.asyncio
    async def test_agent_bot_on_message_ignore_own(self, mock_agent_user: AgentMatrixUser, tmp_path: Path) -> None:
        """Test that agent ignores its own messages."""
        config = Config.from_yaml()

        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.client = AsyncMock()

        mock_room = MagicMock()
        mock_event = MagicMock()
        mock_event.sender = "@mindroom_calculator:localhost"  # Bot's own ID

        await bot._on_message(mock_room, mock_event)

        # Should not send any response
        bot.client.room_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_bot_on_message_ignore_other_agents(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test that agent ignores messages from other agents."""
        config = Config.from_yaml()

        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.client = AsyncMock()

        mock_room = MagicMock()
        mock_event = MagicMock()
        mock_event.sender = "@mindroom_general:localhost"  # Another agent

        await bot._on_message(mock_room, mock_event)

        # Should not send any response
        bot.client.room_send.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enable_streaming", [True, False])
    @patch("mindroom.bot.get_latest_thread_event_id_if_needed")
    @patch("mindroom.bot.ai_response")
    @patch("mindroom.bot.stream_agent_response")
    @patch("mindroom.bot.fetch_thread_history")
    @patch("mindroom.bot.should_use_streaming")
    async def test_agent_bot_on_message_mentioned(
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
        mock_fetch_history.return_value = []
        # Mock the presence check to return same value as enable_streaming
        mock_should_use_streaming.return_value = enable_streaming
        # Mock get_latest_thread_event_id_if_needed
        mock_get_latest_thread.return_value = "latest_thread_event"

        config = Config.from_yaml()
        mention_id = f"@mindroom_calculator:{config.domain}"
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
        )
        bot.client = AsyncMock()

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
        mock_event.source = {
            "content": {
                "body": f"{mention_id}: What's 2+2?",
                "m.mentions": {"user_ids": [mention_id]},
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root_id"},
            },
        }

        await bot._on_message(mock_room, mock_event)

        # Should call AI and send response based on streaming mode
        if enable_streaming:
            mock_stream_agent_response.assert_called_once_with(
                agent_name="calculator",
                prompt=f"{mention_id}: What's 2+2?",
                session_id="!test:localhost:$thread_root_id",
                storage_path=tmp_path,
                config=config,
                thread_history=[],
                room_id="!test:localhost",
                knowledge=None,
                user_id="@user:localhost",
                images=None,
                reply_to_event_id="event123",
                show_tool_calls=True,
                run_metadata_collector=ANY,
            )
            mock_ai_response.assert_not_called()
            # With streaming and stop button: initial message + reaction + edits
            # Note: The exact count may vary based on implementation
            assert bot.client.room_send.call_count >= 2
        else:
            mock_ai_response.assert_called_once_with(
                agent_name="calculator",
                prompt=f"{mention_id}: What's 2+2?",
                session_id="!test:localhost:$thread_root_id",
                storage_path=tmp_path,
                config=config,
                thread_history=[],
                room_id="!test:localhost",
                knowledge=None,
                user_id="@user:localhost",
                images=None,
                reply_to_event_id="event123",
                show_tool_calls=True,
                tool_trace_collector=ANY,
                run_metadata_collector=ANY,
            )
            mock_stream_agent_response.assert_not_called()
            # With stop button support: initial + reaction + final
            assert bot.client.room_send.call_count >= 2

    @pytest.mark.asyncio
    async def test_non_streaming_hidden_tool_calls_do_not_send_tool_trace(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Hidden tool calls should not propagate structured tool metadata."""

        @asynccontextmanager
        async def noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            yield

        config = Config(
            agents={
                "calculator": AgentConfig(
                    display_name="CalculatorAgent",
                    rooms=["!test:localhost"],
                    show_tool_calls=False,
                ),
            },
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.client = AsyncMock()
        bot._knowledge_for_agent = MagicMock(return_value=None)
        bot._build_scheduling_tool_context = MagicMock(return_value=None)
        bot._build_openclaw_context = MagicMock(return_value=None)
        bot._send_response = AsyncMock(return_value="$response")

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            collector = kwargs["tool_trace_collector"]
            collector.append(
                ToolTraceEntry(
                    type="tool_call_completed",
                    tool_name="read_file",
                    args_preview="path=README.md",
                ),
            )
            return "Hidden tool call output"

        with (
            patch("mindroom.bot.typing_indicator", noop_typing_indicator),
            patch("mindroom.bot.ai_response", side_effect=fake_ai_response) as mock_ai,
        ):
            event_id = await bot._process_and_respond(
                room_id="!test:localhost",
                prompt="Summarize README",
                reply_to_event_id="$event",
                thread_id=None,
                thread_history=[],
                user_id="@user:localhost",
            )

        assert event_id == "$response"
        assert mock_ai.call_args.kwargs["show_tool_calls"] is False
        tool_trace = bot._send_response.call_args.kwargs["tool_trace"]
        assert tool_trace is None

    @pytest.mark.asyncio
    async def test_skill_command_uses_target_agent_show_tool_calls_setting(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Skill command responses should use the target agent's show_tool_calls setting."""

        @asynccontextmanager
        async def noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            yield

        def discard_background_task(coro: object, *, _name: str) -> None:
            close = getattr(coro, "close", None)
            if callable(close):
                close()

        config = Config(
            agents={
                "calculator": AgentConfig(
                    display_name="CalculatorAgent",
                    rooms=["!test:localhost"],
                    show_tool_calls=False,
                ),
                "general": AgentConfig(
                    display_name="GeneralAgent",
                    rooms=["!test:localhost"],
                    show_tool_calls=True,
                ),
            },
            defaults=DefaultsConfig(show_tool_calls=False),
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.client = AsyncMock()
        bot._knowledge_for_agent = MagicMock(return_value=None)
        bot._build_scheduling_tool_context = MagicMock(return_value=None)
        bot._build_openclaw_context = MagicMock(return_value=None)
        bot._send_response = AsyncMock(return_value="$response")

        with (
            patch("mindroom.bot.typing_indicator", noop_typing_indicator),
            patch("mindroom.bot.ai_response", new_callable=AsyncMock) as mock_ai,
            patch("mindroom.bot.create_background_task", side_effect=discard_background_task),
        ):
            mock_ai.return_value = "Skill response"
            await bot._send_skill_command_response(
                room_id="!test:localhost",
                reply_to_event_id="$event",
                thread_id=None,
                thread_history=[],
                prompt="Use research skill",
                agent_name="general",
                user_id="@user:localhost",
                reply_to_event=None,
            )

        assert mock_ai.call_args.kwargs["show_tool_calls"] is True

    @pytest.mark.asyncio
    async def test_agent_bot_on_message_not_mentioned(self, mock_agent_user: AgentMatrixUser, tmp_path: Path) -> None:
        """Test agent bot not responding when not mentioned."""
        config = Config.from_yaml()

        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.client = AsyncMock()

        mock_room = MagicMock()
        mock_event = MagicMock()
        mock_event.sender = "@user:localhost"
        mock_event.body = "Hello everyone!"
        mock_event.source = {"content": {"body": "Hello everyone!"}}

        await bot._on_message(mock_room, mock_event)

        # Should not send any response
        bot.client.room_send.assert_not_called()

    def test_build_scheduling_tool_context_uses_active_client_when_room_cached(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Scheduler context should use the active bot client when room cache is present."""
        config = Config(
            agents={
                "calculator": AgentConfig(
                    display_name="CalculatorAgent",
                    rooms=["!test:localhost"],
                ),
            },
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        room_id = "!test:localhost"
        local_room = MagicMock(spec=nio.MatrixRoom)
        local_room.room_id = room_id
        bot.client = MagicMock(rooms={room_id: local_room})
        bot.orchestrator = MagicMock()

        context = bot._build_scheduling_tool_context(
            room_id=room_id,
            thread_id="$thread",
            reply_to_event_id="$event",
            user_id="@user:localhost",
        )

        assert context is not None
        assert context.client is bot.client
        assert context.room is local_room
        assert context.thread_id == "$thread"
        assert context.requester_id == "@user:localhost"

    def test_build_scheduling_tool_context_returns_none_when_room_not_cached(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Scheduler context should be skipped when active client has no room cache entry."""
        config = Config(
            agents={
                "calculator": AgentConfig(
                    display_name="CalculatorAgent",
                    rooms=["!test:localhost"],
                ),
            },
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        room_id = "!test:localhost"
        bot.client = MagicMock(rooms={})
        bot.orchestrator = MagicMock()

        context = bot._build_scheduling_tool_context(
            room_id=room_id,
            thread_id="$thread",
            reply_to_event_id="$event",
            user_id="@user:localhost",
        )

        assert context is None

    def test_build_scheduling_tool_context_returns_none_when_client_unavailable(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Scheduler context should be skipped when no Matrix client is available."""
        config = Config(
            agents={
                "calculator": AgentConfig(
                    display_name="CalculatorAgent",
                    rooms=["!test:localhost"],
                ),
            },
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.client = None

        context = bot._build_scheduling_tool_context(
            room_id="!test:localhost",
            thread_id="$thread",
            reply_to_event_id="$event",
            user_id="@user:localhost",
        )

        assert context is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("handler_name", "marks_responded"),
        [
            ("message", True),
            ("image", True),
            ("voice", True),
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
        config = Config(
            agents={"calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!test:localhost"])},
            voice={"enabled": True},
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.client = AsyncMock()
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        room.users = {"@mindroom_calculator:localhost": MagicMock(), "@user:localhost": MagicMock()}

        event = self._make_handler_event(handler_name, sender="@user:localhost", event_id=f"${handler_name}_unauth")

        with patch("mindroom.bot.is_authorized_sender", return_value=False):
            await self._invoke_handler(bot, handler_name, room, event)

        if marks_responded:
            bot.response_tracker.mark_responded.assert_called_once_with(event.event_id)
        else:
            bot.response_tracker.mark_responded.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("handler_name", "marks_responded"),
        [
            ("message", True),
            ("image", True),
            ("voice", True),
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
        config = Config(
            agents={"calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!test:localhost"])},
            voice={"enabled": True},
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.client = AsyncMock()
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        room.users = {"@mindroom_calculator:localhost": MagicMock(), "@user:localhost": MagicMock()}

        event = self._make_handler_event(handler_name, sender="@user:localhost", event_id=f"${handler_name}_denied")

        if handler_name == "image":
            bot._extract_message_context = AsyncMock(
                return_value=MessageContext(
                    am_i_mentioned=False,
                    is_thread=False,
                    thread_id=None,
                    thread_history=[],
                    mentioned_agents=[],
                    has_non_agent_mentions=False,
                ),
            )

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch.object(bot, "_can_reply_to_sender", return_value=False),
            patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
        ):
            await self._invoke_handler(bot, handler_name, room, event)

        if marks_responded:
            bot.response_tracker.mark_responded.assert_called_once_with(event.event_id)
        else:
            bot.response_tracker.mark_responded.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_bot_on_image_message_forwards_image_to_generate_response(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Image messages should call _generate_response with images payload."""
        config = Config.from_yaml()
        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.client = AsyncMock()

        tracker = MagicMock()
        tracker.has_responded.return_value = False
        bot.__dict__["response_tracker"] = tracker

        bot._extract_message_context = AsyncMock(
            return_value=MessageContext(
                am_i_mentioned=False,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
            ),
        )
        bot._generate_response = AsyncMock(return_value="$response")

        room = MagicMock()
        room.room_id = "!test:localhost"

        event = MagicMock(spec=nio.RoomMessageImage)
        event.sender = "@user:localhost"
        event.event_id = "$img_event"
        event.body = "photo.jpg"
        event.source = {"content": {"body": "photo.jpg"}}  # no filename → body is filename

        image = MagicMock()

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.bot.decide_team_formation",
                new_callable=AsyncMock,
                return_value=TeamFormationDecision(
                    should_form_team=False,
                    agents=[],
                    mode=TeamMode.COLLABORATE,
                ),
            ),
            patch("mindroom.bot.should_agent_respond", return_value=True),
            patch("mindroom.bot.image_handler.download_image", new_callable=AsyncMock, return_value=image),
        ):
            await bot._on_image_message(room, event)

        bot._generate_response.assert_awaited_once_with(
            room_id="!test:localhost",
            prompt="[Attached image]",
            reply_to_event_id="$img_event",
            thread_id=None,
            thread_history=[],
            user_id="@user:localhost",
            images=[image],
        )
        tracker.mark_responded.assert_called_once_with("$img_event", "$response")

    @pytest.mark.asyncio
    async def test_agent_bot_on_image_message_marks_responded_when_download_fails(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Image download failure should still mark event as responded."""
        config = Config.from_yaml()
        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.client = AsyncMock()

        tracker = MagicMock()
        tracker.has_responded.return_value = False
        bot.__dict__["response_tracker"] = tracker

        bot._extract_message_context = AsyncMock(
            return_value=MessageContext(
                am_i_mentioned=False,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
            ),
        )
        bot._generate_response = AsyncMock()

        room = MagicMock()
        room.room_id = "!test:localhost"

        event = MagicMock(spec=nio.RoomMessageImage)
        event.sender = "@user:localhost"
        event.event_id = "$img_event_fail"
        event.body = "please analyze"
        event.source = {"content": {"body": "please analyze"}}

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.bot.decide_team_formation",
                new_callable=AsyncMock,
                return_value=TeamFormationDecision(
                    should_form_team=False,
                    agents=[],
                    mode=TeamMode.COLLABORATE,
                ),
            ),
            patch("mindroom.bot.should_agent_respond", return_value=True),
            patch("mindroom.bot.image_handler.download_image", new_callable=AsyncMock, return_value=None),
        ):
            await bot._on_image_message(room, event)

        bot._generate_response.assert_not_called()
        tracker.mark_responded.assert_called_once_with("$img_event_fail")

    @pytest.mark.asyncio
    async def test_router_routes_image_messages_in_multi_agent_rooms(
        self,
        tmp_path: Path,
    ) -> None:
        """Router should call _handle_ai_routing for images in multi-agent rooms."""
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )

        config = Config.from_yaml()
        config.ids = {
            "general": MatrixID.from_username("mindroom_general", "localhost"),
            "calculator": MatrixID.from_username("mindroom_calculator", "localhost"),
            "router": MatrixID.from_username("mindroom_router", "localhost"),
        }

        bot = AgentBot(agent_user, tmp_path, config=config)
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        bot._handle_ai_routing = AsyncMock()
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False

        mock_context = MagicMock()
        mock_context.am_i_mentioned = False
        mock_context.mentioned_agents = []
        mock_context.has_non_agent_mentions = False
        mock_context.is_thread = False
        mock_context.thread_id = None
        mock_context.thread_history = []
        bot._extract_message_context = AsyncMock(return_value=mock_context)

        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_router:localhost")
        room.users = {
            "@mindroom_router:localhost": None,
            "@mindroom_general:localhost": None,
            "@mindroom_calculator:localhost": None,
            "@user:localhost": None,
        }

        event = nio.RoomMessageImage.from_dict(
            {
                "event_id": "$img_route",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "content": {
                    "msgtype": "m.image",
                    "body": "photo.jpg",
                    "url": "mxc://localhost/test_image",
                    "info": {"mimetype": "image/jpeg"},
                },
            },
        )

        with (
            patch("mindroom.bot.extract_agent_name", return_value=None),
            patch("mindroom.bot.get_agents_in_thread", return_value=[]),
            patch("mindroom.bot.has_multiple_non_agent_users_in_thread", return_value=False),
            patch("mindroom.bot.get_available_agents_for_sender") as mock_get_available,
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.bot.image_handler.extract_caption", return_value="[Attached image]"),
        ):
            mock_get_available.return_value = [config.ids["general"], config.ids["calculator"]]
            await bot._on_image_message(room, event)

        bot._handle_ai_routing.assert_called_once_with(
            room,
            event,
            [],
            None,
            message="[Attached image]",
            requester_user_id="@user:localhost",
        )

    @pytest.mark.asyncio
    async def test_router_dispatch_parity_text_and_image_route_under_same_conditions(self, tmp_path: Path) -> None:
        """Router should route both text and image when the decision context is equivalent."""
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )
        config = Config(
            agents={
                "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!test:localhost"]),
                "general": AgentConfig(display_name="GeneralAgent", rooms=["!test:localhost"]),
            },
            authorization={"default_room_access": True},
        )
        bot = AgentBot(agent_user, tmp_path, config=config)
        bot.client = AsyncMock()
        bot._handle_ai_routing = AsyncMock()
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False
        bot._extract_message_context = AsyncMock(
            return_value=MessageContext(
                am_i_mentioned=False,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
            ),
        )

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        room.users = {
            "@mindroom_router:localhost": MagicMock(),
            "@mindroom_calculator:localhost": MagicMock(),
            "@mindroom_general:localhost": MagicMock(),
            "@user:localhost": MagicMock(),
        }

        text_event = self._make_handler_event("message", sender="@user:localhost", event_id="$route_text")
        text_event.body = "help me"
        text_event.source = {"content": {"body": "help me"}}

        image_event = self._make_handler_event("image", sender="@user:localhost", event_id="$route_img")
        image_event.body = "image.jpg"
        image_event.source = {"content": {"body": "image.jpg"}}

        with (
            patch("mindroom.bot.extract_agent_name", return_value=None),
            patch("mindroom.bot.get_agents_in_thread", return_value=[]),
            patch("mindroom.bot.has_multiple_non_agent_users_in_thread", return_value=False),
            patch(
                "mindroom.bot.get_available_agents_for_sender",
                return_value=[config.ids["calculator"], config.ids["general"]],
            ),
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch("mindroom.bot.image_handler.extract_caption", return_value="[Attached image]"),
            patch("mindroom.bot.interactive.handle_text_response", new_callable=AsyncMock, return_value=None),
        ):
            await bot._on_message(room, text_event)
            await bot._on_image_message(room, image_event)

        assert bot._handle_ai_routing.await_count == 2
        first_call = bot._handle_ai_routing.await_args_list[0].kwargs
        second_call = bot._handle_ai_routing.await_args_list[1].kwargs
        assert first_call["requester_user_id"] == "@user:localhost"
        assert first_call["message"] is None
        assert second_call["requester_user_id"] == "@user:localhost"
        assert second_call["message"] == "[Attached image]"

    @pytest.mark.asyncio
    async def test_router_dispatch_parity_text_and_image_skip_under_same_conditions(self, tmp_path: Path) -> None:
        """Router should skip routing both text and image in single-agent-visible rooms."""
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )
        config = Config(
            agents={"calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!test:localhost"])},
            authorization={"default_room_access": True},
        )
        bot = AgentBot(agent_user, tmp_path, config=config)
        bot.client = AsyncMock()
        bot._handle_ai_routing = AsyncMock()
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False
        bot._extract_message_context = AsyncMock(
            return_value=MessageContext(
                am_i_mentioned=False,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
            ),
        )

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        room.users = {
            "@mindroom_router:localhost": MagicMock(),
            "@mindroom_calculator:localhost": MagicMock(),
            "@user:localhost": MagicMock(),
        }

        text_event = self._make_handler_event("message", sender="@user:localhost", event_id="$skip_text")
        image_event = self._make_handler_event("image", sender="@user:localhost", event_id="$skip_img")

        with (
            patch("mindroom.bot.extract_agent_name", return_value=None),
            patch("mindroom.bot.get_agents_in_thread", return_value=[]),
            patch("mindroom.bot.has_multiple_non_agent_users_in_thread", return_value=False),
            patch("mindroom.bot.get_available_agents_for_sender", return_value=[config.ids["calculator"]]),
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch("mindroom.bot.image_handler.extract_caption", return_value="[Attached image]"),
            patch("mindroom.bot.interactive.handle_text_response", new_callable=AsyncMock, return_value=None),
        ):
            await bot._on_message(room, text_event)
            await bot._on_image_message(room, image_event)

        bot._handle_ai_routing.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_agent_receives_images_from_thread_root_after_routing(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """After router routes an image, the selected agent should download it from the thread root."""
        config = Config.from_yaml()
        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.client = AsyncMock()

        tracker = MagicMock()
        tracker.has_responded.return_value = False
        bot.response_tracker = tracker
        bot._generate_response = AsyncMock(return_value="$response")

        # The thread root is the original image event
        fake_image = Image(content=b"png-bytes", mime_type="image/png")
        bot._fetch_thread_images = AsyncMock(return_value=[fake_image])

        # Simulate the routing mention event in a thread rooted at the image
        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_calculator:localhost")

        mock_context = MagicMock()
        mock_context.am_i_mentioned = True
        mock_context.mentioned_agents = [mock_agent_user.matrix_id]
        mock_context.has_non_agent_mentions = False
        mock_context.is_thread = True
        mock_context.thread_id = "$img_root"
        mock_context.thread_history = []
        bot._extract_message_context = AsyncMock(return_value=mock_context)

        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$router_mention",
                "sender": "@mindroom_router:localhost",
                "origin_server_ts": 1234567890,
                "content": {
                    "msgtype": "m.text",
                    "body": "@calculator could you help with this?",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$img_root"},
                },
            },
        )

        with (
            patch("mindroom.bot.extract_agent_name", return_value="router"),
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.bot.interactive.handle_text_response"),
            patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch("mindroom.bot.get_agents_in_thread", return_value=[]),
            patch("mindroom.bot.get_available_agents_for_sender", return_value=[]),
            patch(
                "mindroom.bot.decide_team_formation",
                new_callable=AsyncMock,
                return_value=TeamFormationDecision(
                    should_form_team=False,
                    agents=[],
                    mode=TeamMode.COLLABORATE,
                ),
            ),
            patch("mindroom.bot.should_agent_respond", return_value=True),
        ):
            await bot._on_message(room, event)

        bot._fetch_thread_images.assert_awaited_once_with("!test:localhost", "$img_root")
        bot._generate_response.assert_awaited_once()
        call_kwargs = bot._generate_response.call_args.kwargs
        assert call_kwargs["images"] == [fake_image]

    @pytest.mark.asyncio
    async def test_decide_team_for_sender_passes_sender_filtered_dm_agents(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """DM team fallback should only see agents allowed for the requester."""
        config = Config(
            agents={
                "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!dm:localhost"]),
                "general": AgentConfig(display_name="GeneralAgent", rooms=["!dm:localhost"]),
            },
            authorization={
                "default_room_access": True,
                "agent_reply_permissions": {
                    "calculator": ["@alice:localhost"],
                    "general": ["@bob:localhost"],
                },
            },
        )

        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!dm:localhost"
        room.users = {
            config.ids["calculator"].full_id: MagicMock(),
            config.ids["general"].full_id: MagicMock(),
        }

        with patch("mindroom.bot.decide_team_formation", new_callable=AsyncMock) as mock_decide:
            mock_decide.return_value = TeamFormationDecision(
                should_form_team=False,
                agents=[],
                mode=TeamMode.COLLABORATE,
            )

            await bot._decide_team_for_sender(
                agents_in_thread=[],
                context=context,
                room=room,
                requester_user_id="@alice:localhost",
                message="help me",
                is_dm=True,
            )

        assert mock_decide.await_count == 1
        assert mock_decide.call_args.kwargs["available_agents_in_room"] == [config.ids["calculator"]]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enable_streaming", [True, False])
    @patch("mindroom.config.Config.from_yaml")
    @patch("mindroom.teams.get_model_instance")
    @patch("mindroom.teams.Team.arun")
    @patch("mindroom.bot.ai_response")
    @patch("mindroom.bot.stream_agent_response")
    @patch("mindroom.bot.fetch_thread_history")
    @patch("mindroom.bot.should_use_streaming")
    @patch("mindroom.bot.get_latest_thread_event_id_if_needed")
    async def test_agent_bot_thread_response(  # noqa: PLR0915
        self,
        mock_get_latest_thread: AsyncMock,
        mock_should_use_streaming: AsyncMock,
        mock_fetch_history: AsyncMock,
        mock_stream_agent_response: AsyncMock,
        mock_ai_response: AsyncMock,
        mock_team_arun: AsyncMock,
        mock_get_model_instance: MagicMock,
        mock_load_config: MagicMock,
        enable_streaming: bool,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test agent bot thread response behavior based on agent participation."""
        # Use the helper method to create mock config
        config = self.create_mock_config()
        mock_load_config.return_value = config

        # Mock get_model_instance to return a mock model
        mock_model = Ollama(id="test-model")
        mock_get_model_instance.return_value = mock_model

        # Mock get_latest_thread_event_id_if_needed to return a valid event ID
        mock_get_latest_thread.return_value = "latest_thread_event"

        bot = AgentBot(
            mock_agent_user,
            tmp_path,
            rooms=["!test:localhost"],
            enable_streaming=enable_streaming,
            config=config,
        )
        bot.client = AsyncMock()

        # Mock orchestrator with agent_bots
        mock_orchestrator = MagicMock()
        mock_agent_bot = MagicMock()
        mock_agent_bot.agent = MagicMock()
        mock_orchestrator.agent_bots = {"calculator": mock_agent_bot, "general": mock_agent_bot}
        mock_orchestrator.current_config = config
        mock_orchestrator.config = config  # This is what teams.py uses
        bot.orchestrator = mock_orchestrator

        # Mock successful room_send response
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        bot.client.room_send.return_value = mock_send_response

        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"
        # Mock room users to include the agent - use the actual agent's user_id
        mock_room.users = {mock_agent_user.user_id: MagicMock()}

        # Test 1: Thread with only this agent - should respond without mention
        mock_fetch_history.return_value = [
            {"sender": "@user:localhost", "body": "Previous message", "timestamp": 123, "event_id": "prev1"},
            {
                "sender": mock_agent_user.user_id,
                "body": "My previous response",
                "timestamp": 124,
                "event_id": "prev2",
            },
        ]

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
        mock_event.source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "thread_root",
                },
            },
        }

        await bot._on_message(mock_room, mock_event)

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
            {"sender": "@user:localhost", "body": "Previous message", "timestamp": 123, "event_id": "prev1"},
            {"sender": mock_agent_user.user_id, "body": "My response", "timestamp": 124, "event_id": "prev2"},
            {
                "sender": config.ids["general"].full_id if "general" in config.ids else "@mindroom_general:localhost",
                "body": "Another agent response",
                "timestamp": 125,
                "event_id": "prev3",
            },
        ]
        mock_fetch_history.return_value = test2_history

        # Create a new event with a different ID for Test 2
        mock_event_2 = MagicMock()
        mock_event_2.sender = "@user:localhost"
        mock_event_2.body = "Thread message without mention"
        mock_event_2.event_id = "event456"  # Different event ID
        mock_event_2.source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "thread_root",
                },
            },
        }

        await bot._on_message(mock_room, mock_event_2)

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
        config = Config.from_yaml()

        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.client = AsyncMock()

        # Mark an event as already responded
        bot.response_tracker.mark_responded("event123")

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

        # Should not send any message since it already responded
        bot.client.room_send.assert_not_called()


class TestMultiAgentOrchestrator:
    """Test cases for MultiAgentOrchestrator class."""

    @pytest.mark.asyncio
    async def test_orchestrator_initialization(self, tmp_path: Path) -> None:
        """Test MultiAgentOrchestrator initialization."""
        orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
        assert orchestrator.agent_bots == {}
        assert not orchestrator.running

    @pytest.mark.asyncio
    @pytest.mark.requires_matrix  # Requires real Matrix server for orchestrator initialization
    @pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
    @patch("mindroom.config.Config.from_yaml")
    async def test_orchestrator_initialize(
        self,
        mock_load_config: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test initializing the orchestrator with agents."""
        # Mock config with just 2 agents
        mock_config = MagicMock()
        mock_config.agents = {
            "calculator": MagicMock(display_name="CalculatorAgent", rooms=["lobby"]),
            "general": MagicMock(display_name="GeneralAgent", rooms=["lobby"]),
        }
        mock_config.teams = {}
        mock_load_config.return_value = mock_config

        with patch("mindroom.bot.MultiAgentOrchestrator._ensure_user_account", new=AsyncMock()):
            orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
            await orchestrator.initialize()

            # Should have 3 bots: calculator, general, and router
            assert len(orchestrator.agent_bots) == 3
            assert "calculator" in orchestrator.agent_bots
            assert "general" in orchestrator.agent_bots
            assert "router" in orchestrator.agent_bots

    @pytest.mark.asyncio
    @pytest.mark.requires_matrix  # Requires real Matrix server for orchestrator start
    @pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
    @patch("mindroom.config.Config.from_yaml")
    async def test_orchestrator_start(
        self,
        mock_load_config: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test starting all agent bots."""
        # Mock config with just 2 agents
        mock_config = MagicMock()
        mock_config.agents = {
            "calculator": MagicMock(display_name="CalculatorAgent", rooms=["lobby"]),
            "general": MagicMock(display_name="GeneralAgent", rooms=["lobby"]),
        }
        mock_config.teams = {}
        mock_config.get_all_configured_rooms.return_value = ["lobby"]
        mock_load_config.return_value = mock_config

        with patch("mindroom.bot.MultiAgentOrchestrator._ensure_user_account", new=AsyncMock()):
            orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
            await orchestrator.initialize()  # Need to initialize first

            # Mock start for all bots to avoid actual login/setup
            start_mocks = []
            for bot in orchestrator.agent_bots.values():
                # Create a mock that tracks the call
                mock_start = AsyncMock()
                # Replace start with our mock
                bot.start = mock_start
                start_mocks.append(mock_start)
                bot.running = False

            # Start the orchestrator but don't wait for sync_forever
            start_tasks = [bot.start() for bot in orchestrator.agent_bots.values()]

            await asyncio.gather(*start_tasks)
            orchestrator.running = True  # Manually set since we're not calling orchestrator.start()

            assert orchestrator.running
            # Verify start was called for each bot
            for mock_start in start_mocks:
                mock_start.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.requires_matrix  # Requires real Matrix server for orchestrator stop
    @pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
    @patch("mindroom.config.Config.from_yaml")
    async def test_orchestrator_stop(
        self,
        mock_load_config: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test stopping all agent bots."""
        # Mock config with just 2 agents
        mock_config = MagicMock()
        mock_config.agents = {
            "calculator": MagicMock(display_name="CalculatorAgent", rooms=["lobby"]),
            "general": MagicMock(display_name="GeneralAgent", rooms=["lobby"]),
        }
        mock_config.teams = {}
        mock_config.get_all_configured_rooms.return_value = ["lobby"]
        mock_load_config.return_value = mock_config

        with patch("mindroom.bot.MultiAgentOrchestrator._ensure_user_account", new=AsyncMock()):
            orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
            await orchestrator.initialize()

            # Mock the agent clients and ensure_user_account
            for bot in orchestrator.agent_bots.values():
                bot.client = AsyncMock()
                bot.running = True
                bot.ensure_user_account = AsyncMock()

            await orchestrator.stop()

            assert not orchestrator.running
            for bot in orchestrator.agent_bots.values():
                assert not bot.running
                if bot.client is not None:
                    bot.client.close.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.requires_matrix  # Requires real Matrix server for orchestrator streaming
    @pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
    @patch("mindroom.config.Config.from_yaml")
    async def test_orchestrator_streaming_default_config(
        self,
        mock_load_config: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test that orchestrator respects defaults.enable_streaming."""
        # Mock config with just 2 agents
        mock_config = MagicMock()
        mock_config.agents = {
            "calculator": MagicMock(display_name="CalculatorAgent", rooms=["lobby"]),
            "general": MagicMock(display_name="GeneralAgent", rooms=["lobby"]),
        }
        mock_config.teams = {}
        mock_config.defaults.enable_streaming = False
        mock_config.get_all_configured_rooms.return_value = ["lobby"]
        mock_load_config.return_value = mock_config

        with patch("mindroom.bot.MultiAgentOrchestrator._ensure_user_account", new=AsyncMock()):
            orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
            await orchestrator.initialize()

            # All bots should have streaming disabled except teams (which never stream)
            for bot in orchestrator.agent_bots.values():
                if hasattr(bot, "enable_streaming"):
                    assert bot.enable_streaming is False
