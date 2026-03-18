"""Tests for the multi-agent bot system."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import nio
import pytest
from agno.knowledge.document import Document
from agno.knowledge.knowledge import Knowledge
from agno.media import Image
from agno.models.ollama import Ollama
from agno.run.agent import RunContentEvent
from agno.run.team import TeamRunOutput

from mindroom.attachments import _attachment_id_for_event, register_local_attachment
from mindroom.authorization import is_authorized_sender as is_authorized_sender_for_test
from mindroom.bot import (
    AgentBot,
    MultiKnowledgeVectorDb,
    _DispatchPayload,
    _MessageContext,
    _PreparedDispatch,
    _ResponseAction,
)
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig, RouterConfig
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    RuntimePaths,
    resolve_runtime_paths,
)
from mindroom.knowledge.manager import KnowledgeManager
from mindroom.matrix.client import PermanentMatrixStartupError
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY, AgentMatrixUser
from mindroom.media_inputs import MediaInputs
from mindroom.orchestration.runtime import (
    _matrix_homeserver_startup_timeout_seconds_from_env,
    run_with_retry,
    wait_for_matrix_homeserver,
)
from mindroom.orchestrator import (
    MultiAgentOrchestrator,
    _run_auxiliary_task_forever,
    main,
)
from mindroom.runtime_state import get_runtime_state, reset_runtime_state, set_runtime_ready
from mindroom.teams import TeamIntent, TeamMemberStatus, TeamOutcome, TeamResolution, TeamResolutionMember
from mindroom.tool_system.events import ToolTraceEntry
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable
    from pathlib import Path


def _runtime_bound_config(config: Config, runtime_root: Path) -> Config:
    """Return a runtime-bound config for bot tests."""
    return bind_runtime_paths(
        config,
        test_runtime_paths(runtime_root),
    )


def _mock_shared_knowledge_manager(
    *,
    base_id: str,
    storage_root: Path,
    knowledge_path: Path,
    knowledge: object,
) -> KnowledgeManager:
    manager = MagicMock(spec=KnowledgeManager)
    manager.base_id = base_id
    manager.storage_path = storage_root
    manager.knowledge_path = knowledge_path
    manager.matches.return_value = True
    manager.get_knowledge.return_value = knowledge
    return manager


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

    @staticmethod
    def create_mock_config(runtime_root: Path) -> Config:
        """Create a typed config for tests that do not need a runtime-bound YAML load."""
        return _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!test:localhost"]),
                    "general": AgentConfig(display_name="GeneralAgent", rooms=["!test:localhost"]),
                },
                teams={},
                models={"default": ModelConfig(provider="test", id="test-model")},
                authorization=AuthorizationConfig(default_room_access=True),
            ),
            runtime_root,
        )

    @staticmethod
    def _runtime_paths(storage_path: Path) -> RuntimePaths:
        return resolve_runtime_paths(
            config_path=storage_path / "config.yaml",
            storage_path=storage_path,
            process_env={},
        )

    @classmethod
    def _config_for_storage(cls, storage_path: Path) -> Config:
        return cls.create_mock_config(storage_path)

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
        elif handler_name == "file":
            event = MagicMock(spec=nio.RoomMessageFile)
            event.body = "report.pdf"
            event.url = "mxc://localhost/report"
            event.source = {"content": {"body": "report.pdf", "msgtype": "m.file"}}
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
        elif handler_name in {"image", "voice", "file"}:
            await bot._on_media_message(room, event)
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
        runtime_root: Path,
    ) -> Config:
        """Create a real config with one calculator agent for knowledge assignment tests."""
        return _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        knowledge_bases=assigned_bases or [],
                    ),
                },
                knowledge_bases=knowledge_bases or {},
            ),
            runtime_root,
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
            runtime_root=tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
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
            runtime_root=tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        expected_knowledge = object()
        manager = _mock_shared_knowledge_manager(
            base_id="research",
            storage_root=runtime_paths_for(config).storage_root,
            knowledge_path=(tmp_path / "kb").resolve(),
            knowledge=expected_knowledge,
        )
        bot.orchestrator = MagicMock(knowledge_managers={"research": manager})

        assert bot._knowledge_for_agent("calculator") is expected_knowledge

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
            runtime_root=tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))

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

        research_manager = _mock_shared_knowledge_manager(
            base_id="research",
            storage_root=runtime_paths_for(config).storage_root,
            knowledge_path=(tmp_path / "kb_research").resolve(),
            knowledge=research_knowledge,
        )
        legal_manager = _mock_shared_knowledge_manager(
            base_id="legal",
            storage_root=runtime_paths_for(config).storage_root,
            knowledge_path=(tmp_path / "kb_legal").resolve(),
            knowledge=legal_knowledge,
        )

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

    def test_knowledge_for_agent_prefers_request_bound_manager(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Request-bound managers should win over later cache lookups."""
        config = self.create_config_with_knowledge_bases(
            assigned_bases=["research"],
            knowledge_bases={
                "research": KnowledgeBaseConfig(path=str(tmp_path / "kb"), watch=False),
            },
            runtime_root=tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        cached_manager = MagicMock()
        cached_manager.get_knowledge.return_value = object()
        bot.orchestrator = MagicMock(knowledge_managers={"research": cached_manager})

        expected_knowledge = object()
        bound_manager = MagicMock()
        bound_manager.get_knowledge.return_value = expected_knowledge

        assert (
            bot._knowledge_for_agent(
                "calculator",
                request_knowledge_managers={"research": bound_manager},
            )
            is expected_knowledge
        )

        bound_manager.get_knowledge.assert_called_once_with()
        cached_manager.get_knowledge.assert_not_called()

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
    @patch("mindroom.config.main.Config.from_yaml")
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
    @patch("mindroom.config.main.Config.from_yaml")
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

        mock_load_config.return_value = self.create_mock_config(tmp_path)
        config = mock_load_config.return_value

        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        await bot.start()

        assert bot.running
        assert bot.client == mock_client
        # The bot calls ensure_setup which calls ensure_user_account
        # and then login with whatever user account was ensured
        assert mock_login.called
        assert (
            mock_client.add_event_callback.call_count == 11
        )  # invite, message, reaction, audio, image/file/video callbacks

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
    async def test_orchestrator_main_reraises_permanent_startup_error(self, tmp_path: Path) -> None:
        """Permanent startup errors should stop the process and surface the failure."""
        reset_runtime_state()
        blocking_event = asyncio.Event()
        mock_orchestrator = MagicMock()
        mock_orchestrator.start = AsyncMock(side_effect=PermanentMatrixStartupError("boom"))
        mock_orchestrator.stop = AsyncMock()
        mock_orchestrator.running = False

        async def _blocked_auxiliary_task(*_args: object, **_kwargs: object) -> None:
            await blocking_event.wait()

        with (
            patch("mindroom.orchestrator.setup_logging"),
            patch("mindroom.orchestrator.sync_env_to_credentials"),
            patch("mindroom.orchestrator.MultiAgentOrchestrator", return_value=mock_orchestrator),
            patch("mindroom.orchestrator._run_auxiliary_task_forever", new=_blocked_auxiliary_task),
            pytest.raises(PermanentMatrixStartupError, match="boom"),
        ):
            await main(log_level="INFO", runtime_paths=self._runtime_paths(tmp_path), api=False)

        mock_orchestrator.stop.assert_awaited_once()
        state = get_runtime_state()
        assert state.phase == "idle"
        assert state.detail is None

    @pytest.mark.asyncio
    async def test_orchestrator_main_watches_resolved_config_path(self, tmp_path: Path) -> None:
        """The top-level config watcher should follow the orchestrator's canonical config path."""
        reset_runtime_state()
        watched_paths: list[Path] = []
        config_watcher_ran = asyncio.Event()
        resolved_config_path = (tmp_path / "nested" / "config.yaml").resolve()
        mock_orchestrator = MagicMock()
        mock_orchestrator.config_path = resolved_config_path
        mock_orchestrator._require_config_path.return_value = resolved_config_path
        mock_orchestrator.stop = AsyncMock()

        async def _watch_config_task(path: Path, _orchestrator: object) -> None:
            watched_paths.append(path)
            config_watcher_ran.set()

        async def _run_auxiliary(task_name: str, operation: Callable[[], Awaitable[None]]) -> None:
            del task_name
            await operation()

        async def _start() -> None:
            await asyncio.wait_for(config_watcher_ran.wait(), timeout=1)
            msg = "boom"
            raise PermanentMatrixStartupError(msg)

        mock_orchestrator.start = AsyncMock(side_effect=_start)

        with (
            patch("mindroom.orchestrator.setup_logging"),
            patch("mindroom.orchestrator.sync_env_to_credentials"),
            patch("mindroom.orchestrator.MultiAgentOrchestrator", return_value=mock_orchestrator),
            patch("mindroom.orchestrator._watch_config_task", side_effect=_watch_config_task),
            patch("mindroom.orchestrator._watch_skills_task", new=AsyncMock()),
            patch("mindroom.orchestrator._run_auxiliary_task_forever", side_effect=_run_auxiliary),
            pytest.raises(PermanentMatrixStartupError, match="boom"),
        ):
            await main(log_level="INFO", runtime_paths=self._runtime_paths(tmp_path), api=False)

        assert watched_paths == [resolved_config_path]

    @pytest.mark.asyncio
    async def test_orchestrator_main_commits_runtime_storage_root_before_logging_and_credential_sync(
        self,
        tmp_path: Path,
    ) -> None:
        """Direct orchestrator callers should get the same storage-root contract as the CLI wrapper."""
        reset_runtime_state()
        runtime_storage = tmp_path / "runtime-storage"
        observed_logging_root: Path | None = None
        observed_credentials_root: Path | None = None
        mock_orchestrator = MagicMock()
        mock_orchestrator.start = AsyncMock()
        mock_orchestrator.stop = AsyncMock()

        def _capture_logging(*, level: str, runtime_paths: RuntimePaths) -> None:
            del level
            nonlocal observed_logging_root
            observed_logging_root = runtime_paths.storage_root

        def _capture_credentials_sync(runtime_paths: RuntimePaths) -> None:
            nonlocal observed_credentials_root
            observed_credentials_root = runtime_paths.storage_root

        with (
            patch("mindroom.orchestrator.setup_logging", side_effect=_capture_logging),
            patch("mindroom.orchestrator.sync_env_to_credentials", side_effect=_capture_credentials_sync),
            patch("mindroom.orchestrator.MultiAgentOrchestrator", return_value=mock_orchestrator),
        ):
            await main(log_level="INFO", runtime_paths=self._runtime_paths(runtime_storage), api=False)

        assert observed_logging_root == runtime_storage.resolve()
        assert observed_credentials_root == runtime_storage.resolve()

    @pytest.mark.asyncio
    async def test_agent_bot_stop(self, mock_agent_user: AgentMatrixUser, tmp_path: Path) -> None:
        """Test stopping an agent bot."""
        config = self._config_for_storage(tmp_path)

        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot.running = True

        await bot.stop()

        assert not bot.running
        bot.client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_agent_bot_on_invite(self, mock_agent_user: AgentMatrixUser, tmp_path: Path) -> None:
        """Test handling room invitations."""
        config = self._config_for_storage(tmp_path)

        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
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
        config = self._config_for_storage(tmp_path)

        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
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
        config = self._config_for_storage(tmp_path)

        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
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
        mock_fetch_history.return_value = []
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
            mock_stream_agent_response.assert_called_once()
            stream_kwargs = mock_stream_agent_response.call_args.kwargs
            assert stream_kwargs["agent_name"] == "calculator"
            assert stream_kwargs["prompt"] == f"{mention_id}: What's 2+2?"
            assert stream_kwargs["session_id"] == "!test:localhost:$thread_root_id"
            assert stream_kwargs["runtime_paths"].storage_root == runtime_paths_for(config).storage_root
            assert stream_kwargs["config"] == config
            assert stream_kwargs["thread_history"] == []
            assert stream_kwargs["room_id"] == "!test:localhost"
            assert stream_kwargs["knowledge"] is None
            assert stream_kwargs["user_id"] == "@user:localhost"
            assert stream_kwargs["media"] == MediaInputs()
            assert stream_kwargs["reply_to_event_id"] == "event123"
            assert stream_kwargs["show_tool_calls"] is True
            assert stream_kwargs["run_metadata_collector"] == {}
            mock_ai_response.assert_not_called()
            # With streaming and stop button: initial message + reaction + edits
            # Note: The exact count may vary based on implementation
            assert bot.client.room_send.call_count >= 2
        else:
            mock_ai_response.assert_called_once()
            ai_kwargs = mock_ai_response.call_args.kwargs
            assert ai_kwargs["agent_name"] == "calculator"
            assert ai_kwargs["prompt"] == f"{mention_id}: What's 2+2?"
            assert ai_kwargs["session_id"] == "!test:localhost:$thread_root_id"
            assert ai_kwargs["runtime_paths"].storage_root == runtime_paths_for(config).storage_root
            assert ai_kwargs["config"] == config
            assert ai_kwargs["thread_history"] == []
            assert ai_kwargs["room_id"] == "!test:localhost"
            assert ai_kwargs["knowledge"] is None
            assert ai_kwargs["user_id"] == "@user:localhost"
            assert ai_kwargs["media"] == MediaInputs()
            assert ai_kwargs["reply_to_event_id"] == "event123"
            assert ai_kwargs["show_tool_calls"] is True
            assert ai_kwargs["tool_trace_collector"] == []
            assert ai_kwargs["run_metadata_collector"] == {}
            mock_stream_agent_response.assert_not_called()
            # With stop button support: initial + reaction + final
            assert bot.client.room_send.call_count >= 2

    @pytest.mark.asyncio
    async def test_process_and_respond_includes_matrix_metadata_when_tool_enabled(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Agents with matrix_message should receive room/thread/event ids in the model prompt."""

        @asynccontextmanager
        async def noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            yield

        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        tools=["matrix_message"],
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot._knowledge_for_agent = MagicMock(return_value=None)
        bot._send_response = AsyncMock(return_value="$response")

        with (
            patch("mindroom.bot.typing_indicator", noop_typing_indicator),
            patch("mindroom.bot.ai_response", new_callable=AsyncMock) as mock_ai,
        ):
            mock_ai.return_value = "Handled"
            event_id = await bot._process_and_respond(
                room_id="!test:localhost",
                prompt="Please send an update",
                reply_to_event_id="$event123",
                thread_id=None,
                thread_history=[],
                user_id="@user:localhost",
            )

        assert event_id == "$response"
        model_prompt = mock_ai.call_args.kwargs["prompt"]
        assert "[Matrix metadata for tool calls]" in model_prompt
        assert "room_id: !test:localhost" in model_prompt
        assert "thread_id: $event123" in model_prompt
        assert "reply_to_event_id: $event123" in model_prompt

    @pytest.mark.asyncio
    async def test_process_and_respond_includes_matrix_metadata_when_openclaw_compat_enabled(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """openclaw_compat agents should receive room/thread/event ids in the model prompt."""

        @asynccontextmanager
        async def noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            yield

        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        tools=["openclaw_compat"],
                        include_default_tools=False,
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot._knowledge_for_agent = MagicMock(return_value=None)
        bot._send_response = AsyncMock(return_value="$response")

        with (
            patch("mindroom.bot.typing_indicator", noop_typing_indicator),
            patch("mindroom.bot.ai_response", new_callable=AsyncMock) as mock_ai,
        ):
            mock_ai.return_value = "Handled"
            event_id = await bot._process_and_respond(
                room_id="!test:localhost",
                prompt="Please send an update",
                reply_to_event_id="$event123",
                thread_id=None,
                thread_history=[],
                user_id="@user:localhost",
            )

        assert event_id == "$response"
        model_prompt = mock_ai.call_args.kwargs["prompt"]
        assert "[Matrix metadata for tool calls]" in model_prompt
        assert "room_id: !test:localhost" in model_prompt
        assert "thread_id: $event123" in model_prompt
        assert "reply_to_event_id: $event123" in model_prompt

    @pytest.mark.asyncio
    async def test_process_and_respond_streaming_includes_matrix_metadata_when_tool_enabled(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming path should inject Matrix ids for agents with matrix messaging tools."""

        @asynccontextmanager
        async def noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            yield

        async def mock_streaming_response() -> AsyncGenerator[str, None]:
            yield "chunk"

        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        tools=["matrix_message"],
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot._knowledge_for_agent = MagicMock(return_value=None)
        bot._handle_interactive_question = AsyncMock()

        with (
            patch("mindroom.bot.typing_indicator", noop_typing_indicator),
            patch("mindroom.bot.stream_agent_response", new_callable=AsyncMock) as mock_stream_agent_response,
            patch("mindroom.bot.send_streaming_response", new_callable=AsyncMock) as mock_send_streaming_response,
        ):
            mock_stream_agent_response.return_value = mock_streaming_response()
            mock_send_streaming_response.return_value = ("$response", "chunk")
            event_id = await bot._process_and_respond_streaming(
                room_id="!test:localhost",
                prompt="Please reply in thread",
                reply_to_event_id="$event456",
                thread_id=None,
                thread_history=[],
                user_id="@user:localhost",
            )

        assert event_id == "$response"
        model_prompt = mock_stream_agent_response.call_args.kwargs["prompt"]
        assert "[Matrix metadata for tool calls]" in model_prompt
        assert "room_id: !test:localhost" in model_prompt
        assert "thread_id: $event456" in model_prompt
        assert "reply_to_event_id: $event456" in model_prompt

    @pytest.mark.asyncio
    async def test_process_and_respond_streaming_resolves_knowledge_once(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming should resolve knowledge only inside the request-scoped context."""

        @asynccontextmanager
        async def noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            yield

        async def mock_streaming_response() -> AsyncGenerator[str, None]:
            yield "chunk"

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
        bot.client = AsyncMock()
        bot._knowledge_for_agent = MagicMock(return_value=None)
        bot._handle_interactive_question = AsyncMock()

        with (
            patch("mindroom.bot.typing_indicator", noop_typing_indicator),
            patch("mindroom.bot.stream_agent_response", new_callable=AsyncMock) as mock_stream_agent_response,
            patch("mindroom.bot.send_streaming_response", new_callable=AsyncMock) as mock_send_streaming_response,
        ):
            mock_stream_agent_response.return_value = mock_streaming_response()
            mock_send_streaming_response.return_value = ("$response", "chunk")
            event_id = await bot._process_and_respond_streaming(
                room_id="!test:localhost",
                prompt="Hello",
                reply_to_event_id="$event456",
                thread_id=None,
                thread_history=[],
                user_id="@user:localhost",
            )

        assert event_id == "$response"
        bot._knowledge_for_agent.assert_called_once()
        args, kwargs = bot._knowledge_for_agent.call_args
        assert args == ("calculator",)
        assert kwargs["request_knowledge_managers"] == {}
        assert "execution_identity" not in kwargs

    @pytest.mark.asyncio
    async def test_process_and_respond_includes_attachment_ids_in_response_metadata(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Non-streaming responses should persist attachment IDs in message metadata."""

        @asynccontextmanager
        async def noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            yield

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot._knowledge_for_agent = MagicMock(return_value=None)
        bot._send_response = AsyncMock(return_value="$response")

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            kwargs["run_metadata_collector"]["io.mindroom.ai_run"] = {"version": 1}
            return "Handled"

        attachment_ids = ["att_image", "att_zip"]
        with (
            patch("mindroom.bot.typing_indicator", noop_typing_indicator),
            patch("mindroom.bot.ai_response", new_callable=AsyncMock, side_effect=fake_ai_response),
        ):
            await bot._process_and_respond(
                room_id="!test:localhost",
                prompt="Please inspect attachments",
                reply_to_event_id="$event123",
                thread_id=None,
                thread_history=[],
                user_id="@user:localhost",
                attachment_ids=attachment_ids,
            )

        sent_extra_content = bot._send_response.await_args.kwargs["extra_content"]
        assert sent_extra_content[ATTACHMENT_IDS_KEY] == attachment_ids
        assert sent_extra_content["io.mindroom.ai_run"]["version"] == 1

    @pytest.mark.asyncio
    async def test_process_and_respond_streaming_includes_attachment_ids_in_response_metadata(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming responses should persist attachment IDs in message metadata."""

        @asynccontextmanager
        async def noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            yield

        async def mock_streaming_response() -> AsyncGenerator[str, None]:
            yield "chunk"

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot._knowledge_for_agent = MagicMock(return_value=None)
        bot._handle_interactive_question = AsyncMock()

        def fake_stream_agent_response(*_args: object, **kwargs: object) -> AsyncGenerator[str, None]:
            kwargs["run_metadata_collector"]["io.mindroom.ai_run"] = {"version": 1}
            return mock_streaming_response()

        attachment_ids = ["att_image", "att_zip"]
        with (
            patch("mindroom.bot.typing_indicator", noop_typing_indicator),
            patch("mindroom.bot.stream_agent_response", side_effect=fake_stream_agent_response),
            patch("mindroom.bot.send_streaming_response", new_callable=AsyncMock) as mock_send_streaming_response,
        ):
            mock_send_streaming_response.return_value = ("$response", "chunk")
            await bot._process_and_respond_streaming(
                room_id="!test:localhost",
                prompt="Please inspect attachments",
                reply_to_event_id="$event456",
                thread_id=None,
                thread_history=[],
                user_id="@user:localhost",
                attachment_ids=attachment_ids,
            )

        sent_extra_content = mock_send_streaming_response.await_args.kwargs["extra_content"]
        assert sent_extra_content[ATTACHMENT_IDS_KEY] == attachment_ids
        assert sent_extra_content["io.mindroom.ai_run"]["version"] == 1

    def test_agent_has_matrix_messaging_tool_when_openclaw_compat_enabled(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """openclaw_compat should imply matrix_message availability without explicit config."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        tools=["openclaw_compat"],
                        include_default_tools=False,
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))

        assert bot._agent_has_matrix_messaging_tool("calculator") is True

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

        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        show_tool_calls=False,
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot._knowledge_for_agent = MagicMock(return_value=None)
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

        config = _runtime_bound_config(
            Config(
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
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot._knowledge_for_agent = MagicMock(return_value=None)
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
        config = self._config_for_storage(tmp_path)

        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()

        mock_room = MagicMock()
        mock_event = MagicMock()
        mock_event.sender = "@user:localhost"
        mock_event.body = "Hello everyone!"
        mock_event.source = {"content": {"body": "Hello everyone!"}}

        await bot._on_message(mock_room, mock_event)

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
        bot.orchestrator = MagicMock()

        context = bot._build_tool_runtime_context(
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
        bot.orchestrator = MagicMock()

        context = bot._build_tool_runtime_context(
            room_id=room_id,
            thread_id="$thread",
            reply_to_event_id="$event",
            user_id="@user:localhost",
        )

        assert context is not None
        assert context.room is None

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

        context = bot._build_tool_runtime_context(
            room_id="!test:localhost",
            thread_id="$thread",
            reply_to_event_id="$event",
            user_id="@user:localhost",
        )

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

        context = bot._build_tool_runtime_context(
            room_id="!test:localhost",
            thread_id=None,
            reply_to_event_id="$root_event",
            user_id="@user:localhost",
            attachment_ids=["att_1"],
        )

        assert context is not None
        assert context.thread_id is None
        assert context.resolved_thread_id == "$root_event"
        assert context.attachment_ids == ("att_1",)

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
                return_value=_MessageContext(
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
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()

        tracker = MagicMock()
        tracker.has_responded.return_value = False
        bot.__dict__["response_tracker"] = tracker

        bot._extract_message_context = AsyncMock(
            return_value=_MessageContext(
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
        image.content = b"image-bytes"
        image.mime_type = "image/jpeg"
        attachment_id = _attachment_id_for_event("$img_event")
        attachment_record = MagicMock()
        attachment_record.attachment_id = attachment_id

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.bot.decide_team_formation",
                new_callable=AsyncMock,
                return_value=TeamResolution.none(),
            ),
            patch("mindroom.bot.should_agent_respond", return_value=True),
            patch("mindroom.bot.image_handler.download_image", new_callable=AsyncMock, return_value=image),
            patch(
                "mindroom.bot.register_image_attachment",
                new_callable=AsyncMock,
                return_value=attachment_record,
            ),
            patch(
                "mindroom.bot.resolve_attachment_media",
                return_value=([attachment_id], [], [image], [], []),
            ),
        ):
            await bot._on_media_message(room, event)

        bot._generate_response.assert_awaited_once()
        generate_kwargs = bot._generate_response.await_args.kwargs
        assert generate_kwargs["room_id"] == "!test:localhost"
        assert "Available attachment IDs" in generate_kwargs["prompt"]
        assert attachment_id in generate_kwargs["prompt"]
        assert generate_kwargs["reply_to_event_id"] == "$img_event"
        assert generate_kwargs["thread_id"] is None
        assert generate_kwargs["thread_history"] == []
        assert generate_kwargs["user_id"] == "@user:localhost"
        media = generate_kwargs["media"]
        assert list(media.images) == [image]
        assert list(media.audio) == []
        assert list(media.files) == []
        assert list(media.videos) == []
        assert generate_kwargs["attachment_ids"] == [attachment_id]
        tracker.mark_responded.assert_called_once_with("$img_event", "$response")

    @pytest.mark.asyncio
    async def test_media_message_merges_thread_history_attachment_ids(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Media turns should include attachment IDs already referenced in thread history."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()

        tracker = MagicMock()
        tracker.has_responded.return_value = False
        bot.__dict__["response_tracker"] = tracker

        history_attachment_id = "att_prev_image"
        current_attachment_id = _attachment_id_for_event("$img_event_history")

        bot._extract_message_context = AsyncMock(
            return_value=_MessageContext(
                am_i_mentioned=False,
                is_thread=True,
                thread_id="$thread_root",
                thread_history=[
                    {
                        "event_id": "$routed_prev",
                        "content": {ATTACHMENT_IDS_KEY: [history_attachment_id]},
                    },
                ],
                mentioned_agents=[],
                has_non_agent_mentions=False,
            ),
        )
        bot._generate_response = AsyncMock(return_value="$response")

        room = MagicMock()
        room.room_id = "!test:localhost"

        event = MagicMock(spec=nio.RoomMessageImage)
        event.sender = "@user:localhost"
        event.event_id = "$img_event_history"
        event.body = "photo.png"
        event.source = {
            "content": {
                "body": "photo.png",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        }

        image = MagicMock()
        image.content = b"\x89PNG\r\n\x1a\npayload"
        image.mime_type = "image/png"
        attachment_record = MagicMock()
        attachment_record.attachment_id = current_attachment_id

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.bot.decide_team_formation",
                new_callable=AsyncMock,
                return_value=TeamResolution.none(),
            ),
            patch("mindroom.bot.should_agent_respond", return_value=True),
            patch("mindroom.bot.image_handler.download_image", new_callable=AsyncMock, return_value=image),
            patch(
                "mindroom.bot.register_image_attachment",
                new_callable=AsyncMock,
                return_value=attachment_record,
            ),
            patch("mindroom.bot.resolve_thread_attachment_ids", new_callable=AsyncMock, return_value=[]),
            patch(
                "mindroom.bot.resolve_attachment_media",
                return_value=(
                    [current_attachment_id, history_attachment_id],
                    [],
                    [image],
                    [],
                    [],
                ),
            ) as mock_resolve_media,
        ):
            await bot._on_media_message(room, event)

        mock_resolve_media.assert_called_once()
        assert mock_resolve_media.call_args.args[1] == [current_attachment_id, history_attachment_id]

        bot._generate_response.assert_awaited_once()
        generate_kwargs = bot._generate_response.await_args.kwargs
        assert generate_kwargs["attachment_ids"] == [current_attachment_id, history_attachment_id]
        assert current_attachment_id in generate_kwargs["prompt"]
        assert history_attachment_id in generate_kwargs["prompt"]
        tracker.mark_responded.assert_called_once_with("$img_event_history", "$response")

    @pytest.mark.asyncio
    async def test_agent_bot_on_image_message_marks_responded_when_download_fails(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Image download failure should still mark event as responded."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()

        tracker = MagicMock()
        tracker.has_responded.return_value = False
        bot.__dict__["response_tracker"] = tracker

        bot._extract_message_context = AsyncMock(
            return_value=_MessageContext(
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
                return_value=TeamResolution.none(),
            ),
            patch("mindroom.bot.should_agent_respond", return_value=True),
            patch("mindroom.bot.image_handler.download_image", new_callable=AsyncMock, return_value=None),
        ):
            await bot._on_media_message(room, event)

        bot._generate_response.assert_not_called()
        tracker.mark_responded.assert_called_once_with("$img_event_fail")

    @pytest.mark.asyncio
    async def test_agent_bot_on_file_message_forwards_local_path_to_generate_response(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """File messages should call _generate_response with a local media path in prompt."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()

        tracker = MagicMock()
        tracker.has_responded.return_value = False
        bot.__dict__["response_tracker"] = tracker

        bot._extract_message_context = AsyncMock(
            return_value=_MessageContext(
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

        event = MagicMock(spec=nio.RoomMessageFile)
        event.sender = "@user:localhost"
        event.event_id = "$file_event"
        event.body = "report.pdf"
        event.url = "mxc://localhost/report"
        event.source = {"content": {"body": "report.pdf", "msgtype": "m.file"}}

        local_media_path = tmp_path / "incoming_media" / "file.pdf"
        local_media_path.parent.mkdir(parents=True, exist_ok=True)
        local_media_path.write_bytes(b"pdf")
        attachment_record = register_local_attachment(
            tmp_path,
            local_media_path,
            kind="file",
            attachment_id=_attachment_id_for_event("$file_event"),
            filename="report.pdf",
            mime_type="application/pdf",
            room_id=room.room_id,
            thread_id="$file_event",
            source_event_id="$file_event",
            sender="@user:localhost",
        )
        assert attachment_record is not None

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.bot.decide_team_formation",
                new_callable=AsyncMock,
                return_value=TeamResolution.none(),
            ),
            patch("mindroom.bot.should_agent_respond", return_value=True),
            patch(
                "mindroom.bot.register_file_or_video_attachment",
                new_callable=AsyncMock,
                return_value=attachment_record,
            ),
        ):
            await bot._on_media_message(room, event)

        bot._generate_response.assert_awaited_once()
        generate_kwargs = bot._generate_response.await_args.kwargs
        attachment_id = _attachment_id_for_event("$file_event")
        assert generate_kwargs["room_id"] == "!test:localhost"
        assert generate_kwargs["reply_to_event_id"] == "$file_event"
        assert generate_kwargs["thread_id"] is None
        assert generate_kwargs["thread_history"] == []
        assert generate_kwargs["user_id"] == "@user:localhost"
        assert generate_kwargs["attachment_ids"] == [attachment_id]
        assert "Available attachment IDs" in generate_kwargs["prompt"]
        assert attachment_id in generate_kwargs["prompt"]
        media = generate_kwargs["media"]
        assert len(media.files) == 1
        assert str(media.files[0].filepath) == str(local_media_path)
        assert list(media.videos) == []
        tracker.mark_responded.assert_called_once_with("$file_event", "$response")

    @pytest.mark.asyncio
    async def test_agent_bot_on_file_message_marks_responded_when_store_fails(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """File media persistence failure should still mark event as responded."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()

        tracker = MagicMock()
        tracker.has_responded.return_value = False
        bot.__dict__["response_tracker"] = tracker

        bot._extract_message_context = AsyncMock(
            return_value=_MessageContext(
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

        event = MagicMock(spec=nio.RoomMessageFile)
        event.sender = "@user:localhost"
        event.event_id = "$file_event_fail"
        event.body = "report.pdf"
        event.url = "mxc://localhost/report"
        event.source = {"content": {"body": "report.pdf", "msgtype": "m.file"}}

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.bot.decide_team_formation",
                new_callable=AsyncMock,
                return_value=TeamResolution.none(),
            ),
            patch("mindroom.bot.should_agent_respond", return_value=True),
            patch("mindroom.bot.register_file_or_video_attachment", new_callable=AsyncMock, return_value=None),
        ):
            await bot._on_media_message(room, event)

        bot._generate_response.assert_not_called()
        tracker.mark_responded.assert_called_once_with("$file_event_fail")

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

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
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
            patch("mindroom.bot.extract_media_caption", return_value="[Attached image]"),
        ):
            mock_get_available.return_value = [
                config.get_ids(runtime_paths_for(config))["general"],
                config.get_ids(runtime_paths_for(config))["calculator"],
            ]
            await bot._on_media_message(room, event)

        bot._handle_ai_routing.assert_called_once_with(
            room,
            event,
            [],
            None,
            message="[Attached image]",
            requester_user_id="@user:localhost",
            extra_content={"com.mindroom.original_sender": "@user:localhost"},
        )

    @pytest.mark.asyncio
    async def test_router_routes_file_messages_with_sender_metadata(
        self,
        tmp_path: Path,
    ) -> None:
        """Router should pass sender metadata when routing file messages."""
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
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

        event = nio.RoomMessageFile.from_dict(
            {
                "event_id": "$file_route",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "content": {
                    "msgtype": "m.file",
                    "body": "report.pdf",
                    "url": "mxc://localhost/test_file",
                    "info": {"mimetype": "application/pdf"},
                },
            },
        )
        local_media_path = tmp_path / "incoming_media" / "file_route.pdf"
        local_media_path.parent.mkdir(parents=True, exist_ok=True)
        local_media_path.write_bytes(b"%PDF")
        attachment_record = register_local_attachment(
            tmp_path,
            local_media_path,
            kind="file",
            attachment_id=_attachment_id_for_event("$file_route"),
            filename="report.pdf",
            mime_type="application/pdf",
            room_id=room.room_id,
            thread_id=None,
            source_event_id="$file_route",
            sender="@user:localhost",
        )
        assert attachment_record is not None

        with (
            patch("mindroom.bot.extract_agent_name", return_value=None),
            patch("mindroom.bot.get_agents_in_thread", return_value=[]),
            patch("mindroom.bot.has_multiple_non_agent_users_in_thread", return_value=False),
            patch("mindroom.bot.get_available_agents_for_sender") as mock_get_available,
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch(
                "mindroom.bot.register_file_or_video_attachment",
                new_callable=AsyncMock,
                return_value=attachment_record,
            ) as mock_register_file,
        ):
            mock_get_available.return_value = [
                config.get_ids(runtime_paths_for(config))["general"],
                config.get_ids(runtime_paths_for(config))["calculator"],
            ]
            await bot._on_media_message(room, event)

        bot._handle_ai_routing.assert_called_once()
        mock_register_file.assert_not_awaited()
        call_kwargs = bot._handle_ai_routing.call_args.kwargs
        assert call_kwargs["message"] == "[Attached file]"
        assert call_kwargs["requester_user_id"] == "@user:localhost"
        assert call_kwargs["extra_content"] == {ORIGINAL_SENDER_KEY: "@user:localhost"}

    @pytest.mark.asyncio
    async def test_router_routing_registers_file_with_effective_thread_scope(
        self,
        tmp_path: Path,
    ) -> None:
        """Router should register routed file attachments using the outgoing thread scope."""
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )

        config = _runtime_bound_config(
            Config(
                agents={
                    "router": AgentConfig(display_name="Router"),
                    "general": AgentConfig(display_name="General", thread_mode="room"),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot.response_tracker = MagicMock()
        bot._send_response = AsyncMock(return_value="$route")

        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_router:localhost")
        event = nio.RoomMessageFile.from_dict(
            {
                "event_id": "$file_route",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "content": {
                    "msgtype": "m.file",
                    "body": "report.pdf",
                    "url": "mxc://localhost/test_file",
                    "info": {"mimetype": "application/pdf"},
                },
            },
        )
        media_path = tmp_path / "incoming_media" / "file_route.pdf"
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(b"%PDF")
        attachment_record = register_local_attachment(
            tmp_path,
            media_path,
            kind="file",
            attachment_id=_attachment_id_for_event("$file_route"),
            filename="report.pdf",
            mime_type="application/pdf",
            room_id=room.room_id,
            thread_id=None,
            source_event_id="$file_route",
            sender="@user:localhost",
        )
        assert attachment_record is not None

        with (
            patch(
                "mindroom.bot.filter_agents_by_sender_permissions",
                return_value=[config.get_ids(runtime_paths_for(config))["general"]],
            ),
            patch("mindroom.bot.suggest_agent_for_message", new_callable=AsyncMock, return_value="general"),
            patch(
                "mindroom.bot.register_file_or_video_attachment",
                new_callable=AsyncMock,
                return_value=attachment_record,
            ) as mock_register_file,
        ):
            await bot._handle_ai_routing(
                room=room,
                event=event,
                thread_history=[],
                thread_id=None,
                message="[Attached file]",
                requester_user_id="@user:localhost",
                extra_content={ORIGINAL_SENDER_KEY: "@user:localhost"},
            )

        mock_register_file.assert_awaited_once()
        assert mock_register_file.await_args.kwargs["thread_id"] is None
        sent_extra_content = bot._send_response.await_args.kwargs["extra_content"]
        assert sent_extra_content[ATTACHMENT_IDS_KEY] == [attachment_record.attachment_id]

    @pytest.mark.asyncio
    async def test_router_routing_registers_image_with_effective_thread_scope(
        self,
        tmp_path: Path,
    ) -> None:
        """Router should register routed image attachments using outgoing thread scope."""
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )

        config = _runtime_bound_config(
            Config(
                agents={
                    "router": AgentConfig(display_name="Router"),
                    "general": AgentConfig(display_name="General", thread_mode="room"),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot.response_tracker = MagicMock()
        bot._send_response = AsyncMock(return_value="$route")

        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_router:localhost")
        event = nio.RoomMessageImage.from_dict(
            {
                "event_id": "$image_route",
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

        attachment_record = MagicMock()
        attachment_record.attachment_id = _attachment_id_for_event("$image_route")

        with (
            patch(
                "mindroom.bot.filter_agents_by_sender_permissions",
                return_value=[config.get_ids(runtime_paths_for(config))["general"]],
            ),
            patch("mindroom.bot.suggest_agent_for_message", new_callable=AsyncMock, return_value="general"),
            patch(
                "mindroom.bot.register_image_attachment",
                new_callable=AsyncMock,
                return_value=attachment_record,
            ) as mock_register_image,
        ):
            await bot._handle_ai_routing(
                room=room,
                event=event,
                thread_history=[],
                thread_id=None,
                message="[Attached image]",
                requester_user_id="@user:localhost",
                extra_content={ORIGINAL_SENDER_KEY: "@user:localhost"},
            )

        mock_register_image.assert_awaited_once()
        assert mock_register_image.await_args.kwargs["thread_id"] is None
        sent_extra_content = bot._send_response.await_args.kwargs["extra_content"]
        assert sent_extra_content[ATTACHMENT_IDS_KEY] == [attachment_record.attachment_id]

    @pytest.mark.asyncio
    async def test_multi_agent_file_event_registers_attachment_once(self, tmp_path: Path) -> None:
        """A file event in a multi-agent room should register exactly one attachment."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "router": AgentConfig(display_name="Router", rooms=["!test:localhost"]),
                    "general": AgentConfig(display_name="General", rooms=["!test:localhost"]),
                    "calculator": AgentConfig(display_name="Calculator", rooms=["!test:localhost"]),
                },
                authorization={"default_room_access": True},
            ),
            tmp_path,
        )
        router_bot = AgentBot(
            AgentMatrixUser(
                agent_name="router",
                user_id="@mindroom_router:localhost",
                display_name="Router",
                password=TEST_PASSWORD,
                access_token="mock_test_token",  # noqa: S106
            ),
            tmp_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        general_bot = AgentBot(
            AgentMatrixUser(
                agent_name="general",
                user_id="@mindroom_general:localhost",
                display_name="General",
                password=TEST_PASSWORD,
                access_token="mock_test_token",  # noqa: S106
            ),
            tmp_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )

        router_bot.client = AsyncMock()
        general_bot.client = AsyncMock()
        router_bot.response_tracker = MagicMock()
        router_bot.response_tracker.has_responded.return_value = False
        general_bot.response_tracker = MagicMock()
        general_bot.response_tracker.has_responded.return_value = False
        router_bot._send_response = AsyncMock(return_value="$route")
        general_bot._generate_response = AsyncMock()

        message_context = _MessageContext(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )
        router_bot._extract_message_context = AsyncMock(return_value=message_context)
        general_bot._extract_message_context = AsyncMock(return_value=message_context)

        router_room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_router:localhost")
        general_room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
        room_users = {
            "@mindroom_router:localhost": None,
            "@mindroom_general:localhost": None,
            "@mindroom_calculator:localhost": None,
            "@user:localhost": None,
        }
        router_room.users = room_users
        general_room.users = room_users

        file_event = nio.RoomMessageFile.from_dict(
            {
                "event_id": "$file_once",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "content": {
                    "msgtype": "m.file",
                    "body": "report.pdf",
                    "url": "mxc://localhost/file_once",
                    "info": {"mimetype": "application/pdf"},
                },
            },
        )

        media_path = tmp_path / "incoming_media" / "file_once.pdf"
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(b"%PDF")
        attachment_record = register_local_attachment(
            tmp_path,
            media_path,
            kind="file",
            attachment_id=_attachment_id_for_event("$file_once"),
            filename="report.pdf",
            mime_type="application/pdf",
            room_id=router_room.room_id,
            thread_id=None,
            source_event_id="$file_once",
            sender="@user:localhost",
        )
        assert attachment_record is not None

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.bot.decide_team_formation",
                new_callable=AsyncMock,
                return_value=TeamResolution.none(),
            ),
            patch("mindroom.bot.suggest_agent_for_message", new_callable=AsyncMock, return_value="general"),
            patch(
                "mindroom.bot.register_file_or_video_attachment",
                new_callable=AsyncMock,
                return_value=attachment_record,
            ) as mock_register,
        ):
            await router_bot._on_media_message(router_room, file_event)
            await general_bot._on_media_message(general_room, file_event)

        mock_register.assert_awaited_once()
        assert mock_register.await_args.kwargs["room_id"] == "!test:localhost"
        assert mock_register.await_args.kwargs["thread_id"] == "$file_once"
        general_bot._generate_response.assert_not_called()

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
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!test:localhost"]),
                    "general": AgentConfig(display_name="GeneralAgent", rooms=["!test:localhost"]),
                },
                authorization={"default_room_access": True},
            ),
            tmp_path,
        )
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot._handle_ai_routing = AsyncMock()
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False
        bot._extract_message_context = AsyncMock(
            return_value=_MessageContext(
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
                return_value=[
                    config.get_ids(runtime_paths_for(config))["calculator"],
                    config.get_ids(runtime_paths_for(config))["general"],
                ],
            ),
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch("mindroom.bot.extract_media_caption", return_value="[Attached image]"),
            patch("mindroom.bot.interactive.handle_text_response", new_callable=AsyncMock, return_value=None),
        ):
            await bot._on_message(room, text_event)
            await bot._on_media_message(room, image_event)

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
        config = _runtime_bound_config(
            Config(
                agents={"calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!test:localhost"])},
                authorization={"default_room_access": True},
            ),
            tmp_path,
        )
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot._handle_ai_routing = AsyncMock()
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False
        bot._extract_message_context = AsyncMock(
            return_value=_MessageContext(
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
            patch(
                "mindroom.bot.get_available_agents_for_sender",
                return_value=[config.get_ids(runtime_paths_for(config))["calculator"]],
            ),
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch("mindroom.bot.extract_media_caption", return_value="[Attached image]"),
            patch("mindroom.bot.interactive.handle_text_response", new_callable=AsyncMock, return_value=None),
        ):
            await bot._on_message(room, text_event)
            await bot._on_media_message(room, image_event)

        bot._handle_ai_routing.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_agent_receives_images_from_thread_root_after_routing(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """After router routes an image, the selected agent should resolve it via attachments."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()

        tracker = MagicMock()
        tracker.has_responded.return_value = False
        bot.response_tracker = tracker
        bot._generate_response = AsyncMock(return_value="$response")

        fake_image = Image(content=b"png-bytes", mime_type="image/png")

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
                "mindroom.bot.resolve_thread_attachment_ids",
                new_callable=AsyncMock,
                return_value=["att_img_root"],
            ) as mock_resolve_attachment_ids,
            patch(
                "mindroom.bot.resolve_attachment_media",
                return_value=(["att_img_root"], [], [fake_image], [], []),
            ),
            patch(
                "mindroom.bot.decide_team_formation",
                new_callable=AsyncMock,
                return_value=TeamResolution.none(),
            ),
            patch("mindroom.bot.should_agent_respond", return_value=True),
        ):
            await bot._on_message(room, event)

        mock_resolve_attachment_ids.assert_awaited_once()
        bot._generate_response.assert_awaited_once()
        call_kwargs = bot._generate_response.call_args.kwargs
        assert list(call_kwargs["media"].images) == [fake_image]
        assert call_kwargs["attachment_ids"] == ["att_img_root"]

    @pytest.mark.asyncio
    async def test_decide_team_for_sender_passes_sender_filtered_dm_agents(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """DM team fallback should only see agents allowed for the requester."""
        config = _runtime_bound_config(
            Config(
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
            ),
            tmp_path,
        )

        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.orchestrator = MagicMock()
        bot.orchestrator.agent_bots = {"calculator": MagicMock()}
        context = _MessageContext(
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
            config.get_ids(runtime_paths_for(config))["calculator"].full_id: MagicMock(),
            config.get_ids(runtime_paths_for(config))["general"].full_id: MagicMock(),
        }

        with patch("mindroom.bot.decide_team_formation", new_callable=AsyncMock) as mock_decide:
            mock_decide.return_value = TeamResolution.none()

            await bot._decide_team_for_sender(
                agents_in_thread=[],
                context=context,
                room=room,
                requester_user_id="@alice:localhost",
                message="help me",
                is_dm=True,
            )

        assert mock_decide.await_count == 1
        assert mock_decide.call_args.kwargs["available_agents_in_room"] == [
            config.get_ids(runtime_paths_for(config))["calculator"],
        ]
        assert mock_decide.call_args.kwargs["materializable_agent_names"] == {"calculator"}

    @pytest.mark.asyncio
    async def test_resolve_response_action_rejects_instead_of_falling_through_to_individual_reply(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Explicitly rejected team requests must not fall through to individual replies."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.users = {
            bot.matrix_id.full_id: MagicMock(),
        }
        context = _MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[bot.matrix_id],
            has_non_agent_mentions=False,
        )

        with (
            patch.object(
                bot,
                "_decide_team_for_sender",
                new=AsyncMock(
                    return_value=TeamResolution(
                        intent=TeamIntent.EXPLICIT_MEMBERS,
                        requested_members=[bot.matrix_id],
                        member_statuses=[
                            TeamResolutionMember(
                                agent=bot.matrix_id,
                                name=bot.agent_name,
                                status=TeamMemberStatus.ELIGIBLE,
                                can_respond=True,
                            ),
                        ],
                        eligible_members=[bot.matrix_id],
                        outcome=TeamOutcome.REJECT,
                        reason="Team request includes private agent 'mind'; private agents cannot participate in teams yet",
                    ),
                ),
            ),
            patch("mindroom.bot.should_agent_respond", return_value=True) as mock_should_respond,
        ):
            action = await bot._resolve_response_action(
                context,
                room,
                "@user:localhost",
                "help me",
                False,
            )

        assert action.kind == "reject"
        assert "private agents cannot participate in teams yet" in action.rejection_message
        mock_should_respond.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_rejects_when_explicit_mentions_include_hidden_agent(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Explicit mixed mentions should reject instead of collapsing to one visible agent."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                    "general": AgentConfig(display_name="GeneralAgent", rooms=["!room:localhost"]),
                },
                authorization={
                    "default_room_access": True,
                    "agent_reply_permissions": {
                        "calculator": ["@alice:localhost"],
                        "general": ["@bob:localhost"],
                    },
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.users = {
            config.get_ids(runtime_paths_for(config))["calculator"].full_id: MagicMock(),
            config.get_ids(runtime_paths_for(config))["general"].full_id: MagicMock(),
        }
        context = _MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[
                config.get_ids(runtime_paths_for(config))["calculator"],
                config.get_ids(runtime_paths_for(config))["general"],
            ],
            has_non_agent_mentions=False,
        )

        with patch("mindroom.bot.should_agent_respond", return_value=True) as mock_should_respond:
            action = await bot._resolve_response_action(
                context,
                room,
                "@alice:localhost",
                "calculator and general, help",
                False,
            )

        assert action.kind == "reject"
        assert action.rejection_message == (
            "Team request includes agent 'general' that is not available to you in this room."
        )
        mock_should_respond.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_rejects_when_only_unrequested_visible_bot_can_surface_reject(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Explicit rejects should not go silent when stale room members sort before the live fallback bot."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                    "general": AgentConfig(display_name="GeneralAgent", rooms=["!room:localhost"]),
                    "research": AgentConfig(display_name="ResearchAgent", rooms=["!room:localhost"]),
                },
                authorization={"default_room_access": True},
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.orchestrator = MagicMock()
        bot.orchestrator.agent_bots = {"calculator": MagicMock()}
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.users = {
            config.get_ids(runtime_paths_for(config))["general"].full_id: MagicMock(),
            config.get_ids(runtime_paths_for(config))["research"].full_id: MagicMock(),
            config.get_ids(runtime_paths_for(config))["calculator"].full_id: MagicMock(),
        }
        context = _MessageContext(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[
                config.get_ids(runtime_paths_for(config))["general"],
                config.get_ids(runtime_paths_for(config))["research"],
            ],
            has_non_agent_mentions=False,
        )

        with patch("mindroom.bot.should_agent_respond", return_value=True) as mock_should_respond:
            action = await bot._resolve_response_action(
                context,
                room,
                "@user:localhost",
                "general and research, help",
                False,
            )

        assert action.kind == "reject"
        assert action.rejection_message == (
            "Team request includes agents 'general', 'research' that could not be materialized for this request."
        )
        mock_should_respond.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_skips_when_explicit_mentions_are_all_hidden(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Explicit mixed mentions must not fall through when sender-visible agents are []."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                    "general": AgentConfig(display_name="GeneralAgent", rooms=["!room:localhost"]),
                },
                authorization={
                    "default_room_access": True,
                    "agent_reply_permissions": {
                        "calculator": ["@bob:localhost"],
                        "general": ["@bob:localhost"],
                    },
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.users = {
            config.get_ids(runtime_paths_for(config))["calculator"].full_id: MagicMock(),
            config.get_ids(runtime_paths_for(config))["general"].full_id: MagicMock(),
        }
        context = _MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[
                config.get_ids(runtime_paths_for(config))["calculator"],
                config.get_ids(runtime_paths_for(config))["general"],
            ],
            has_non_agent_mentions=False,
        )

        with patch("mindroom.bot.should_agent_respond", return_value=True) as mock_should_respond:
            action = await bot._resolve_response_action(
                context,
                room,
                "@alice:localhost",
                "calculator and general, help",
                False,
            )

        assert action.kind == "skip"
        mock_should_respond.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_ignores_non_materializable_owner_candidates(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Reject ownership should stay with a live bot instead of a missing requested member."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "alpha": AgentConfig(display_name="AlphaAgent", rooms=["!room:localhost"]),
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.users = {
            config.get_ids(runtime_paths_for(config))["alpha"].full_id: MagicMock(),
            config.get_ids(runtime_paths_for(config))["calculator"].full_id: MagicMock(),
        }
        context = _MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[
                config.get_ids(runtime_paths_for(config))["alpha"],
                config.get_ids(runtime_paths_for(config))["calculator"],
            ],
            has_non_agent_mentions=False,
        )

        with (
            patch.object(
                bot,
                "_decide_team_for_sender",
                new=AsyncMock(
                    return_value=TeamResolution(
                        intent=TeamIntent.EXPLICIT_MEMBERS,
                        requested_members=[
                            config.get_ids(runtime_paths_for(config))["alpha"],
                            config.get_ids(runtime_paths_for(config))["calculator"],
                        ],
                        member_statuses=[
                            TeamResolutionMember(
                                agent=config.get_ids(runtime_paths_for(config))["alpha"],
                                name="alpha",
                                status=TeamMemberStatus.NOT_MATERIALIZABLE,
                                can_respond=False,
                            ),
                            TeamResolutionMember(
                                agent=config.get_ids(runtime_paths_for(config))["calculator"],
                                name="calculator",
                                status=TeamMemberStatus.ELIGIBLE,
                                can_respond=True,
                            ),
                        ],
                        eligible_members=[config.get_ids(runtime_paths_for(config))["calculator"]],
                        outcome=TeamOutcome.REJECT,
                        reason="Team request includes agent 'alpha' that is not available right now.",
                    ),
                ),
            ),
            patch("mindroom.bot.should_agent_respond", return_value=True) as mock_should_respond,
        ):
            action = await bot._resolve_response_action(
                context,
                room,
                "@user:localhost",
                "alpha and calculator, help",
                False,
            )

        assert action.kind == "reject"
        assert action.rejection_message == "Team request includes agent 'alpha' that is not available right now."
        mock_should_respond.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_ignores_unsupported_non_responders_for_reject_ownership(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Reject ownership should ignore unsupported members that cannot emit the response."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "alpha": AgentConfig(display_name="AlphaAgent", rooms=["!room:localhost"]),
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.users = {
            config.get_ids(runtime_paths_for(config))["alpha"].full_id: MagicMock(),
            config.get_ids(runtime_paths_for(config))["calculator"].full_id: MagicMock(),
        }
        context = _MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[
                config.get_ids(runtime_paths_for(config))["alpha"],
                config.get_ids(runtime_paths_for(config))["calculator"],
            ],
            has_non_agent_mentions=False,
        )

        with (
            patch.object(
                bot,
                "_decide_team_for_sender",
                new=AsyncMock(
                    return_value=TeamResolution(
                        intent=TeamIntent.EXPLICIT_MEMBERS,
                        requested_members=[
                            config.get_ids(runtime_paths_for(config))["alpha"],
                            config.get_ids(runtime_paths_for(config))["calculator"],
                        ],
                        member_statuses=[
                            TeamResolutionMember(
                                agent=config.get_ids(runtime_paths_for(config))["alpha"],
                                name="alpha",
                                status=TeamMemberStatus.UNSUPPORTED_FOR_TEAM,
                                can_respond=False,
                            ),
                            TeamResolutionMember(
                                agent=config.get_ids(runtime_paths_for(config))["calculator"],
                                name="calculator",
                                status=TeamMemberStatus.ELIGIBLE,
                                can_respond=True,
                            ),
                        ],
                        eligible_members=[config.get_ids(runtime_paths_for(config))["calculator"]],
                        outcome=TeamOutcome.REJECT,
                        reason="Team request includes private agent 'alpha'; private agents cannot participate in teams yet",
                    ),
                ),
            ),
            patch("mindroom.bot.should_agent_respond", return_value=True) as mock_should_respond,
        ):
            action = await bot._resolve_response_action(
                context,
                room,
                "@user:localhost",
                "alpha and calculator, help",
                False,
            )

        assert action.kind == "reject"
        assert "private agents cannot participate in teams yet" in action.rejection_message
        mock_should_respond.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_uses_actual_team_resolution_for_private_member_reject_ownership(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Real team resolution should keep private requested members from owning the reject reply."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "alpha": AgentConfig(
                        display_name="AlphaAgent",
                        rooms=["!room:localhost"],
                        private=AgentPrivateConfig(per="user", root="alpha_data"),
                    ),
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.orchestrator = MagicMock()
        bot.orchestrator.agent_bots = {"alpha": MagicMock(), "calculator": MagicMock()}
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.users = {
            config.get_ids(runtime_paths_for(config))["alpha"].full_id: MagicMock(),
            config.get_ids(runtime_paths_for(config))["calculator"].full_id: MagicMock(),
        }
        context = _MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[
                config.get_ids(runtime_paths_for(config))["alpha"],
                config.get_ids(runtime_paths_for(config))["calculator"],
            ],
            has_non_agent_mentions=False,
        )

        with patch("mindroom.bot.should_agent_respond", return_value=True) as mock_should_respond:
            action = await bot._resolve_response_action(
                context,
                room,
                "@user:localhost",
                "alpha and calculator, help",
                False,
            )

        assert action.kind == "reject"
        assert action.rejection_message == (
            "Team request includes private agent 'alpha'; private agents cannot participate in teams yet"
        )
        mock_should_respond.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_honors_single_agent_team_fallback(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Team formation may degrade to one responder without falling back through should_agent_respond."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.users = {
            bot.matrix_id.full_id: MagicMock(),
        }
        context = _MessageContext(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )

        with (
            patch.object(
                bot,
                "_decide_team_for_sender",
                new=AsyncMock(
                    return_value=TeamResolution.individual(
                        intent=TeamIntent.IMPLICIT_THREAD_TEAM,
                        requested_members=[bot.matrix_id],
                        member_statuses=[],
                        agent=bot.matrix_id,
                    ),
                ),
            ),
            patch("mindroom.bot.should_agent_respond", return_value=False) as mock_should_respond,
        ):
            action = await bot._resolve_response_action(
                context,
                room,
                "@user:localhost",
                "help me",
                True,
            )

        assert action.kind == "individual"
        mock_should_respond.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_sends_visible_rejection_for_unsupported_team_request(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Rejected team requests should send one actionable reply instead of silently skipping."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.response_tracker = MagicMock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        dispatch = _PreparedDispatch(
            requester_user_id="@user:localhost",
            context=_MessageContext(
                am_i_mentioned=True,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[bot.matrix_id],
                has_non_agent_mentions=False,
            ),
        )
        action = _ResponseAction(
            kind="reject",
            rejection_message="Team request includes private agent 'mind'; private agents cannot participate in teams yet",
        )

        with patch.object(bot, "_send_response", new=AsyncMock(return_value="$reply")) as mock_send_response:
            await bot._execute_dispatch_action(
                room,
                event,
                dispatch,
                action,
                _DispatchPayload(prompt="help me"),
                processing_log="processing",
            )

        mock_send_response.assert_awaited_once()
        assert mock_send_response.await_args.kwargs["response_text"].endswith(
            "private agents cannot participate in teams yet",
        )
        bot.response_tracker.mark_responded.assert_called_once_with("$event", "$reply")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enable_streaming", [True, False])
    @patch("mindroom.config.main.Config.from_yaml")
    @patch("mindroom.teams.get_agent_knowledge")
    @patch("mindroom.teams.create_agent")
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
        mock_create_agent: MagicMock,
        mock_get_agent_knowledge: MagicMock,
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
        mock_get_agent_knowledge.return_value = None
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
            config.get_ids(runtime_paths_for(config))["general"].full_id: MagicMock(),
        }

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
                "sender": config.get_ids(runtime_paths_for(config))["general"].full_id
                if "general" in config.get_ids(runtime_paths_for(config))
                else "@mindroom_general:localhost",
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
        config = self._config_for_storage(tmp_path)

        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
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
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        assert orchestrator.agent_bots == {}
        assert not orchestrator.running

    @pytest.mark.asyncio
    async def test_ensure_room_invitations_invites_authorized_users(self, tmp_path: Path) -> None:
        """Global users and room-permitted users should be invited to managed rooms."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(
                        display_name="GeneralAgent",
                        rooms=["!room1:localhost", "!room2:localhost"],
                    ),
                },
                authorization={
                    "global_users": ["@alice:localhost"],
                    "room_permissions": {"!room1:localhost": ["@bob:localhost"]},
                    "default_room_access": False,
                },
            ),
            tmp_path,
        )
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = config

        router_bot = MagicMock()
        router_bot.client = AsyncMock()
        orchestrator.agent_bots = {"router": router_bot}

        room_members = {
            "!room1:localhost": {"@mindroom_general:localhost", "@mindroom_router:localhost"},
            "!room2:localhost": {"@mindroom_general:localhost", "@mindroom_router:localhost"},
        }

        async def mock_get_room_members(_client: AsyncMock, room_id: str) -> set[str]:
            return room_members[room_id]

        mock_invite = AsyncMock(return_value=True)

        with (
            patch("mindroom.constants.runtime_matrix_homeserver", return_value="http://localhost:8008"),
            patch("mindroom.orchestrator.is_authorized_sender", side_effect=is_authorized_sender_for_test),
            patch("mindroom.orchestrator.get_joined_rooms", new=AsyncMock(return_value=list(room_members))),
            patch("mindroom.orchestrator.get_room_members", side_effect=mock_get_room_members),
            patch("mindroom.orchestrator.invite_to_room", mock_invite),
            patch("mindroom.orchestrator.MatrixState.load", return_value=MatrixState()),
        ):
            await orchestrator._ensure_room_invitations()

        invited_users_by_room = {(call.args[1], call.args[2]) for call in mock_invite.await_args_list}
        assert invited_users_by_room == {
            ("!room1:localhost", "@alice:localhost"),
            ("!room2:localhost", "@alice:localhost"),
            ("!room1:localhost", "@bob:localhost"),
        }

    @pytest.mark.asyncio
    async def test_ensure_room_invitations_skips_non_matrix_authorization_entries(self, tmp_path: Path) -> None:
        """Only concrete Matrix user IDs should be invited from authorization lists."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(
                        display_name="GeneralAgent",
                        rooms=["!room1:localhost"],
                    ),
                },
                authorization={
                    "global_users": ["@alice:localhost", "@admin:*", "alice"],
                    "default_room_access": False,
                },
            ),
            tmp_path,
        )
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = config

        router_bot = MagicMock()
        router_bot.client = AsyncMock()
        orchestrator.agent_bots = {"router": router_bot}

        async def mock_get_room_members(_client: AsyncMock, _room_id: str) -> set[str]:
            return {"@mindroom_general:localhost", "@mindroom_router:localhost"}

        mock_invite = AsyncMock(return_value=True)

        with (
            patch("mindroom.constants.runtime_matrix_homeserver", return_value="http://localhost:8008"),
            patch("mindroom.orchestrator.is_authorized_sender", side_effect=is_authorized_sender_for_test),
            patch("mindroom.orchestrator.get_joined_rooms", new=AsyncMock(return_value=["!room1:localhost"])),
            patch("mindroom.orchestrator.get_room_members", side_effect=mock_get_room_members),
            patch("mindroom.orchestrator.invite_to_room", mock_invite),
            patch("mindroom.orchestrator.MatrixState.load", return_value=MatrixState()),
        ):
            await orchestrator._ensure_room_invitations()

        invited_users = [call.args[2] for call in mock_invite.await_args_list]
        assert invited_users == ["@alice:localhost"]

    @pytest.mark.asyncio
    async def test_ensure_room_invitations_skips_internal_user_when_unconfigured(self, tmp_path: Path) -> None:
        """When mindroom_user is unset, stale internal account credentials must not trigger invites."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(
                        display_name="GeneralAgent",
                        rooms=["!room1:localhost"],
                    ),
                },
                authorization={"default_room_access": False},
            ),
            tmp_path,
        )
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = config

        router_bot = MagicMock()
        router_bot.client = AsyncMock()
        orchestrator.agent_bots = {"router": router_bot}

        state = MatrixState()
        state.add_account(INTERNAL_USER_ACCOUNT_KEY, "legacy_internal_user", "legacy-password")

        async def mock_get_room_members(_client: AsyncMock, _room_id: str) -> set[str]:
            return {"@mindroom_general:localhost", "@mindroom_router:localhost"}

        mock_invite = AsyncMock(return_value=True)

        with (
            patch("mindroom.constants.runtime_matrix_homeserver", return_value="http://localhost:8008"),
            patch("mindroom.orchestrator.get_joined_rooms", new=AsyncMock(return_value=["!room1:localhost"])),
            patch("mindroom.orchestrator.get_room_members", side_effect=mock_get_room_members),
            patch("mindroom.orchestrator.invite_to_room", mock_invite),
            patch("mindroom.orchestrator.MatrixState.load", return_value=state),
        ):
            await orchestrator._ensure_room_invitations()

        mock_invite.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_rooms_and_memberships_skips_internal_user_join_when_unconfigured(self, tmp_path: Path) -> None:
        """When mindroom_user is unset, orchestrator should not attempt internal-user room joins."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(
                        display_name="GeneralAgent",
                        rooms=["lobby"],
                    ),
                },
            ),
            tmp_path,
        )
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = config

        bot = AsyncMock()
        bot.agent_name = "general"
        bot.rooms = []
        bot.ensure_rooms = AsyncMock()

        with (
            patch.object(orchestrator, "_ensure_rooms_exist", new=AsyncMock()),
            patch.object(orchestrator, "_ensure_room_invitations", new=AsyncMock()),
            patch("mindroom.orchestrator.get_rooms_for_entity", return_value=["lobby"]),
            patch("mindroom.orchestrator.resolve_room_aliases", return_value=["!room1:localhost"]),
            patch("mindroom.orchestrator.load_rooms", return_value={"lobby": MagicMock(room_id="!room1:localhost")}),
            patch("mindroom.orchestrator.ensure_user_in_rooms", new=AsyncMock()) as mock_ensure_user_in_rooms,
        ):
            await orchestrator._setup_rooms_and_memberships([bot])

        assert bot.rooms == ["!room1:localhost"]
        mock_ensure_user_in_rooms.assert_not_awaited()
        assert bot.ensure_rooms.await_count == 2

    @pytest.mark.asyncio
    async def test_setup_rooms_and_memberships_retries_invites_after_router_joins(self, tmp_path: Path) -> None:
        """Invite-only existing rooms should get a second invitation/join pass after router joins."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(
                        display_name="GeneralAgent",
                        rooms=["lobby"],
                    ),
                },
                mindroom_user={"username": "mindroom_user", "display_name": "MindRoomUser"},
            ),
            tmp_path,
        )
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = config

        router_bot = AsyncMock()
        router_bot.agent_name = ROUTER_AGENT_NAME
        router_bot.rooms = []
        router_bot.ensure_rooms = AsyncMock()

        general_bot = AsyncMock()
        general_bot.agent_name = "general"
        general_bot.rooms = []
        general_bot.ensure_rooms = AsyncMock()

        with (
            patch.object(orchestrator, "_ensure_rooms_exist", new=AsyncMock()),
            patch.object(orchestrator, "_ensure_room_invitations", new=AsyncMock()) as mock_invitations,
            patch("mindroom.orchestrator.get_rooms_for_entity", return_value=["lobby"]),
            patch("mindroom.orchestrator.resolve_room_aliases", return_value=["!room1:localhost"]),
            patch("mindroom.orchestrator.load_rooms", return_value={"lobby": MagicMock(room_id="!room1:localhost")}),
            patch("mindroom.orchestrator.ensure_user_in_rooms", new=AsyncMock()) as mock_ensure_user_in_rooms,
        ):
            await orchestrator._setup_rooms_and_memberships([router_bot, general_bot])

        assert router_bot.rooms == ["!room1:localhost"]
        assert general_bot.rooms == ["!room1:localhost"]
        assert router_bot.ensure_rooms.await_count == 1
        assert general_bot.ensure_rooms.await_count == 2
        assert mock_invitations.await_count == 2
        assert mock_ensure_user_in_rooms.await_count == 2

    @pytest.mark.asyncio
    async def test_setup_rooms_and_memberships_reruns_room_reconciliation_after_router_joins(
        self,
        tmp_path: Path,
    ) -> None:
        """Startup should rerun room reconciliation after the router joins existing rooms."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(
                        display_name="GeneralAgent",
                        rooms=["lobby"],
                    ),
                },
            ),
            tmp_path,
        )
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = config

        router_joined = False
        reconciliation_join_states: list[bool] = []

        async def record_room_reconciliation() -> None:
            reconciliation_join_states.append(router_joined)

        async def router_join_rooms() -> None:
            nonlocal router_joined
            router_joined = True

        router_bot = AsyncMock()
        router_bot.agent_name = ROUTER_AGENT_NAME
        router_bot.rooms = []
        router_bot.ensure_rooms = AsyncMock(side_effect=router_join_rooms)

        general_bot = AsyncMock()
        general_bot.agent_name = "general"
        general_bot.rooms = []
        general_bot.ensure_rooms = AsyncMock()

        with (
            patch.object(orchestrator, "_ensure_rooms_exist", new=AsyncMock(side_effect=record_room_reconciliation)),
            patch.object(orchestrator, "_ensure_room_invitations", new=AsyncMock()),
            patch("mindroom.orchestrator.get_rooms_for_entity", return_value=["lobby"]),
            patch("mindroom.orchestrator.resolve_room_aliases", return_value=["!room1:localhost"]),
            patch("mindroom.orchestrator.load_rooms", return_value={"lobby": MagicMock(room_id="!room1:localhost")}),
            patch("mindroom.orchestrator.ensure_user_in_rooms", new=AsyncMock()),
        ):
            await orchestrator._setup_rooms_and_memberships([router_bot, general_bot])

        assert reconciliation_join_states == [False, True]
        assert router_bot.ensure_rooms.await_count == 1
        assert general_bot.ensure_rooms.await_count == 2

    @pytest.mark.asyncio
    @pytest.mark.requires_matrix  # Requires real Matrix server for orchestrator initialization
    @pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
    @patch("mindroom.config.main.Config.from_yaml")
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

        with patch("mindroom.orchestrator.MultiAgentOrchestrator._ensure_user_account", new=AsyncMock()):
            orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
            await orchestrator.initialize()

            # Should have 3 bots: calculator, general, and router
            assert len(orchestrator.agent_bots) == 3
            assert "calculator" in orchestrator.agent_bots
            assert "general" in orchestrator.agent_bots
            assert "router" in orchestrator.agent_bots

    @pytest.mark.asyncio
    async def test_orchestrator_initialize_uses_custom_config_path(self, tmp_path: Path) -> None:
        """Initialize should load the exact config file owned by the orchestrator."""
        config_path = tmp_path / "custom-config.yaml"
        mock_config = _runtime_bound_config(Config(router=RouterConfig(model="default")), tmp_path)

        with (
            patch("mindroom.orchestrator.load_config", return_value=mock_config) as mock_load_config,
            patch("mindroom.orchestrator.load_plugins"),
            patch("mindroom.orchestrator.MultiAgentOrchestrator._ensure_user_account", new=AsyncMock()),
            patch.object(MultiAgentOrchestrator, "_create_managed_bot"),
        ):
            orchestrator = MultiAgentOrchestrator(
                runtime_paths=resolve_runtime_paths(
                    config_path=config_path,
                    storage_path=tmp_path,
                    process_env={},
                ),
            )
            await orchestrator.initialize()

        mock_load_config.assert_called_once()
        assert mock_load_config.call_args.args[0].config_path == config_path.resolve()

    @pytest.mark.asyncio
    @pytest.mark.requires_matrix  # Requires real Matrix server for orchestrator start
    @pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
    @patch("mindroom.config.main.Config.from_yaml")
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

        with patch("mindroom.orchestrator.MultiAgentOrchestrator._ensure_user_account", new=AsyncMock()):
            orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
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
    async def test_orchestrator_start_sets_up_rooms_before_knowledge(self, tmp_path: Path) -> None:
        """Room creation/invites should happen before knowledge refresh work."""
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = MagicMock()

        bot = MagicMock()
        bot.agent_name = "router"
        bot.try_start = AsyncMock(return_value=True)
        orchestrator.agent_bots = {"router": bot}

        call_order: list[str] = []

        async def _wait_for_homeserver(*_args: object, **_kwargs: object) -> None:
            call_order.append("wait_for_homeserver")

        async def _setup_rooms(_: list[Any]) -> None:
            call_order.append("setup_rooms")

        async def _schedule_knowledge(*_: object, **__: object) -> None:
            call_order.append("schedule_knowledge")

        with (
            patch("mindroom.orchestrator.wait_for_matrix_homeserver", side_effect=_wait_for_homeserver),
            patch.object(orchestrator, "_setup_rooms_and_memberships", side_effect=_setup_rooms),
            patch.object(orchestrator, "_schedule_knowledge_refresh", side_effect=_schedule_knowledge),
            patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
            patch("mindroom.orchestrator.sync_forever_with_restart", new=AsyncMock()),
        ):
            await orchestrator.start()

        assert call_order == ["wait_for_homeserver", "setup_rooms", "schedule_knowledge"]
        bot.try_start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orchestrator_waits_for_homeserver_before_initialize(self, tmp_path: Path) -> None:
        """Matrix readiness must gate initialize(), which creates the internal Matrix user."""
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        call_order: list[str] = []

        async def _wait_for_homeserver(*_args: object, **_kwargs: object) -> None:
            call_order.append("wait_for_homeserver")

        async def _initialize() -> None:
            call_order.append("initialize")
            orchestrator.config = MagicMock()
            bot = MagicMock()
            bot.agent_name = "router"
            bot.try_start = AsyncMock(return_value=True)
            orchestrator.agent_bots = {"router": bot}

        with (
            patch("mindroom.orchestrator.wait_for_matrix_homeserver", side_effect=_wait_for_homeserver),
            patch.object(orchestrator, "initialize", side_effect=_initialize),
            patch.object(orchestrator, "_setup_rooms_and_memberships", new=AsyncMock()),
            patch.object(orchestrator, "_configure_knowledge", new=AsyncMock()),
            patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
            patch("mindroom.orchestrator.sync_forever_with_restart", new=AsyncMock()),
        ):
            await orchestrator.start()

        assert call_order[:2] == ["wait_for_homeserver", "initialize"]

    @pytest.mark.asyncio
    async def test_wait_for_matrix_homeserver_returns_when_versions(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The homeserver wait should return as soon as `/versions` succeeds."""
        calls = 0

        class _FakeAsyncClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                del args, kwargs

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
                del exc_type, exc, tb

            async def get(self, url: str) -> httpx.Response:
                nonlocal calls
                calls += 1
                request = httpx.Request("GET", url)
                return httpx.Response(200, json={"versions": ["v1.1"]}, request=request)

        monkeypatch.setattr("mindroom.orchestration.runtime.httpx.AsyncClient", _FakeAsyncClient)
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env=dict(os.environ),
        )

        await wait_for_matrix_homeserver(
            runtime_paths=runtime_paths,
            timeout_seconds=0.1,
            retry_interval_seconds=0,
        )

        assert calls == 1

    def test_matrix_homeserver_startup_timeout_defaults_to_infinite(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unset or zero startup timeouts should wait forever."""
        monkeypatch.delenv("MINDROOM_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS", raising=False)
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env=dict(os.environ),
        )
        assert _matrix_homeserver_startup_timeout_seconds_from_env(runtime_paths) is None

        monkeypatch.setenv("MINDROOM_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS", "0")
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env=dict(os.environ),
        )
        assert _matrix_homeserver_startup_timeout_seconds_from_env(runtime_paths) is None

    def test_matrix_homeserver_startup_timeout_reads_positive_seconds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A positive timeout env var should bound the startup wait."""
        monkeypatch.setenv("MINDROOM_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS", "45")
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env=dict(os.environ),
        )
        assert _matrix_homeserver_startup_timeout_seconds_from_env(runtime_paths) == 45

    def test_matrix_homeserver_startup_timeout_rejects_negative_values(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Negative timeout values are invalid."""
        monkeypatch.setenv("MINDROOM_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS", "-1")
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env=dict(os.environ),
        )
        with pytest.raises(ValueError, match="must be 0 or a positive integer"):
            _matrix_homeserver_startup_timeout_seconds_from_env(runtime_paths)

    @pytest.mark.asyncio
    async def test_wait_for_matrix_homeserver_retries_on_connection_errors(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Transient transport failures should be retried until `/versions` succeeds."""
        responses: list[Exception | httpx.Response] = [
            httpx.ConnectError("boom"),
            httpx.ConnectError("boom again"),
            httpx.Response(
                200,
                json={"versions": ["v1.1"]},
                request=httpx.Request("GET", "http://localhost/_matrix/client/versions"),
            ),
        ]

        class _FakeAsyncClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                del args, kwargs

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
                del exc_type, exc, tb

            async def get(self, _url: str) -> httpx.Response:
                response = responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response

        monkeypatch.setattr("mindroom.orchestration.runtime.httpx.AsyncClient", _FakeAsyncClient)
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env=dict(os.environ),
        )

        await wait_for_matrix_homeserver(
            runtime_paths=runtime_paths,
            timeout_seconds=0.1,
            retry_interval_seconds=0,
        )

        assert responses == []

    @pytest.mark.asyncio
    async def test_wait_for_matrix_homeserver_times_out_when_never_ready(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The homeserver wait should fail fast when `/versions` never becomes valid."""

        class _FakeAsyncClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                del args, kwargs

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
                del exc_type, exc, tb

            async def get(self, url: str) -> httpx.Response:
                request = httpx.Request("GET", url)
                return httpx.Response(503, text="starting", request=request)

        monkeypatch.setattr("mindroom.orchestration.runtime.httpx.AsyncClient", _FakeAsyncClient)
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env=dict(os.environ),
        )

        with pytest.raises(TimeoutError, match="Timed out waiting for Matrix homeserver"):
            await wait_for_matrix_homeserver(
                runtime_paths=runtime_paths,
                timeout_seconds=0.01,
                retry_interval_seconds=0.001,
            )

    @pytest.mark.asyncio
    async def test_schedule_knowledge_refresh_retries_until_success(self, tmp_path: Path) -> None:
        """Background knowledge refresh should keep retrying until it succeeds."""
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        config = MagicMock()
        attempts = 0

        async def _configure(*_: object, **__: object) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                msg = "boom"
                raise RuntimeError(msg)

        with (
            patch.object(orchestrator, "_configure_knowledge", side_effect=_configure),
            patch("mindroom.orchestration.runtime.STARTUP_RETRY_INITIAL_DELAY_SECONDS", 0),
            patch("mindroom.orchestration.runtime.STARTUP_RETRY_MAX_DELAY_SECONDS", 0),
        ):
            await orchestrator._schedule_knowledge_refresh(config, start_watcher=True)
            task = orchestrator._knowledge_refresh_task
            assert task is not None
            await task

        assert orchestrator._knowledge_refresh_task is None
        assert attempts == 2

    @pytest.mark.asyncio
    async def test_orchestrator_start_schedules_retry_for_failed_agents(self, tmp_path: Path) -> None:
        """Startup should keep degraded agents around and retry them in the background."""
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = MagicMock()

        router_bot = MagicMock()
        router_bot.agent_name = "router"
        router_bot.try_start = AsyncMock(return_value=True)

        failing_bot = MagicMock()
        failing_bot.agent_name = "general"
        failing_bot.try_start = AsyncMock(return_value=False)

        orchestrator.agent_bots = {"router": router_bot, "general": failing_bot}

        with (
            patch("mindroom.orchestrator.wait_for_matrix_homeserver", new=AsyncMock()),
            patch.object(orchestrator, "_setup_rooms_and_memberships", new=AsyncMock()),
            patch.object(orchestrator, "_schedule_knowledge_refresh", new=AsyncMock()),
            patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
            patch.object(orchestrator, "_schedule_bot_start_retry", new=AsyncMock()) as mock_schedule_retry,
            patch("mindroom.orchestrator.sync_forever_with_restart", new=AsyncMock()),
        ):
            await orchestrator.start()

        assert "general" in orchestrator.agent_bots
        mock_schedule_retry.assert_awaited_once_with("general")

    @pytest.mark.asyncio
    async def test_orchestrator_start_skips_retry_for_permanent_failures(self, tmp_path: Path) -> None:
        """Permanent startup failures should leave bots disabled without retry loops."""
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = MagicMock()

        router_bot = MagicMock()
        router_bot.agent_name = "router"
        router_bot.try_start = AsyncMock(return_value=True)

        failing_bot = MagicMock()
        failing_bot.agent_name = "general"
        failing_bot.try_start = AsyncMock(side_effect=PermanentMatrixStartupError("boom"))

        orchestrator.agent_bots = {"router": router_bot, "general": failing_bot}

        with (
            patch("mindroom.orchestrator.wait_for_matrix_homeserver", new=AsyncMock()),
            patch.object(orchestrator, "_setup_rooms_and_memberships", new=AsyncMock()),
            patch.object(orchestrator, "_schedule_knowledge_refresh", new=AsyncMock()),
            patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
            patch.object(orchestrator, "_schedule_bot_start_retry", new=AsyncMock()) as mock_schedule_retry,
            patch("mindroom.orchestrator.sync_forever_with_restart", new=AsyncMock()),
        ):
            await orchestrator.start()

        assert "general" in orchestrator.agent_bots
        mock_schedule_retry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_auxiliary_task_forever_restarts_after_failure(self) -> None:
        """Auxiliary supervisors should restart tasks that crash."""
        started = asyncio.Event()
        calls = 0

        async def _operation() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                msg = "boom"
                raise RuntimeError(msg)
            started.set()
            await asyncio.Future()

        with (
            patch("mindroom.orchestrator._AUXILIARY_TASK_RESTART_INITIAL_DELAY_SECONDS", 0),
            patch("mindroom.orchestrator._AUXILIARY_TASK_RESTART_MAX_DELAY_SECONDS", 0),
            patch("mindroom.orchestrator.logger.exception"),
        ):
            task = asyncio.create_task(
                _run_auxiliary_task_forever("test task", _operation),
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task

        assert calls == 2

    @pytest.mark.asyncio
    async def test_run_auxiliary_task_forever_logs_traceback_on_failure(self) -> None:
        """Auxiliary task crashes should keep traceback logging intact."""
        started = asyncio.Event()
        calls = 0

        async def _operation() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                msg = "boom"
                raise RuntimeError(msg)
            started.set()
            await asyncio.Future()

        with (
            patch("mindroom.orchestrator._AUXILIARY_TASK_RESTART_INITIAL_DELAY_SECONDS", 0),
            patch("mindroom.orchestrator._AUXILIARY_TASK_RESTART_MAX_DELAY_SECONDS", 0),
            patch("mindroom.orchestrator.logger.exception") as mock_exception,
        ):
            task = asyncio.create_task(
                _run_auxiliary_task_forever("test task", _operation),
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task

        mock_exception.assert_called_once_with(
            "Auxiliary task crashed; restarting",
            task_name="test task",
        )

    @pytest.mark.asyncio
    async def test_run_auxiliary_task_forever_resets_backoff_after_healthy_run(self) -> None:
        """Long healthy runs should reset crash-loop backoff for auxiliary tasks."""
        retry_attempts: list[int] = []
        calls = 0
        third_start = asyncio.Event()

        async def _operation() -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                await asyncio.sleep(0.02)
            if calls == 3:
                third_start.set()
                await asyncio.Future()
            msg = "boom"
            raise RuntimeError(msg)

        with (
            patch("mindroom.orchestrator._AUXILIARY_TASK_RESTART_MAX_DELAY_SECONDS", 0.01),
            patch("mindroom.orchestrator.logger.exception"),
            patch(
                "mindroom.orchestrator.retry_delay_seconds",
                side_effect=lambda attempt, **_: retry_attempts.append(attempt) or 0,
            ),
        ):
            task = asyncio.create_task(
                _run_auxiliary_task_forever("test task", _operation),
            )
            await asyncio.wait_for(third_start.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert calls == 3
        assert retry_attempts == [1, 1]

    @pytest.mark.asyncio
    async def test_run_with_retry_can_skip_runtime_state_updates(self) -> None:
        """Background retries must not flip a ready runtime back to startup state."""
        reset_runtime_state()
        set_runtime_ready()
        attempts = 0

        async def _operation() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                msg = "boom"
                raise RuntimeError(msg)

        with (
            patch("mindroom.orchestration.runtime.STARTUP_RETRY_INITIAL_DELAY_SECONDS", 0),
            patch("mindroom.orchestration.runtime.STARTUP_RETRY_MAX_DELAY_SECONDS", 0),
        ):
            await run_with_retry(
                "background retry",
                _operation,
                update_runtime_state=False,
            )

        state = get_runtime_state()
        assert attempts == 2
        assert state.phase == "ready"
        assert state.detail is None
        reset_runtime_state()

    @pytest.mark.asyncio
    async def test_update_config_schedules_knowledge_refresh_when_running(self, tmp_path: Path) -> None:
        """Hot reload should schedule (not block on) knowledge refresh while running."""
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))

        config = MagicMock()
        config.agents = {}
        config.teams = {}
        config.mindroom_user = None
        config.matrix_room_access = MagicMock()
        config.authorization = MagicMock()
        config.defaults.enable_streaming = True

        orchestrator.config = config
        orchestrator.running = True
        router_bot = MagicMock()
        router_bot.config = config
        router_bot.enable_streaming = True
        router_bot._set_presence_with_model_info = AsyncMock()
        orchestrator.agent_bots = {"router": router_bot}

        with (
            patch("mindroom.orchestrator.load_config", return_value=config),
            patch("mindroom.orchestrator.load_plugins"),
            patch(
                "mindroom.orchestration.config_updates._identify_entities_to_restart",
                return_value=set(),
            ),
            patch.object(orchestrator, "_schedule_knowledge_refresh", new=AsyncMock()) as mock_schedule_knowledge,
            patch.object(orchestrator, "_configure_knowledge", new=AsyncMock()) as mock_configure_knowledge,
            patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
        ):
            updated = await orchestrator.update_config()

        assert updated is False
        mock_schedule_knowledge.assert_awaited_once_with(config, start_watcher=True)
        mock_configure_knowledge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_config_uses_custom_config_path(self, tmp_path: Path) -> None:
        """Hot reload should keep reading the orchestrator's custom config path."""
        config_path = tmp_path / "custom-config.yaml"
        current_config = MagicMock()
        current_config.authorization.global_users = []
        new_config = MagicMock()
        new_config.authorization.global_users = []
        new_config.defaults.enable_streaming = True

        orchestrator = MultiAgentOrchestrator(
            runtime_paths=resolve_runtime_paths(
                config_path=config_path,
                storage_path=tmp_path,
                process_env={},
            ),
        )
        orchestrator.config = current_config
        plan = SimpleNamespace(
            mindroom_user_changed=False,
            new_config=new_config,
            entities_to_restart=set(),
            new_entities=set(),
            only_support_service_changes=True,
        )

        with (
            patch("mindroom.orchestrator.load_config", return_value=new_config) as mock_load_config,
            patch("mindroom.orchestrator.load_plugins"),
            patch("mindroom.orchestrator.build_config_update_plan", return_value=plan),
            patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
        ):
            updated = await orchestrator.update_config()

        assert updated is False
        mock_load_config.assert_called_once()
        assert mock_load_config.call_args.args[0].config_path == config_path.resolve()

    @pytest.mark.asyncio
    async def test_update_config_keeps_failed_new_bot_and_schedules_retry(self, tmp_path: Path) -> None:
        """Hot reload should retain failed bots and retry them instead of dropping them."""
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))

        old_config = _runtime_bound_config(
            Config(
                agents={
                    "general": {
                        "display_name": "GeneralAgent",
                        "role": "General assistant",
                        "model": "default",
                        "rooms": ["lobby"],
                    },
                },
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
        )
        new_config = _runtime_bound_config(
            Config(
                agents={
                    "general": {
                        "display_name": "GeneralAgent",
                        "role": "General assistant",
                        "model": "default",
                        "rooms": ["lobby"],
                    },
                    "coach": {
                        "display_name": "Coach",
                        "role": "Coaching assistant",
                        "model": "default",
                        "rooms": ["lobby"],
                    },
                },
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
        )

        orchestrator.config = old_config
        orchestrator.running = True

        router_bot = MagicMock()
        router_bot.config = old_config
        router_bot.enable_streaming = True
        router_bot._set_presence_with_model_info = AsyncMock()
        general_bot = MagicMock()
        general_bot.config = old_config
        general_bot.enable_streaming = True
        general_bot._set_presence_with_model_info = AsyncMock()
        orchestrator.agent_bots = {"router": router_bot, "general": general_bot}

        new_bot = MagicMock()
        new_bot.agent_name = "coach"
        new_bot.running = False
        new_bot.try_start = AsyncMock(return_value=False)
        new_bot.ensure_rooms = AsyncMock(side_effect=AssertionError("ensure_rooms called on failed bot"))

        with (
            patch("mindroom.orchestrator.load_config", return_value=new_config),
            patch("mindroom.orchestrator.load_plugins"),
            patch(
                "mindroom.orchestration.config_updates._identify_entities_to_restart",
                return_value=set(),
            ),
            patch("mindroom.orchestrator.create_bot_for_entity", return_value=new_bot),
            patch("mindroom.orchestrator.create_temp_user", return_value=MagicMock()),
            patch.object(orchestrator, "_schedule_bot_start_retry", new=AsyncMock()) as mock_schedule_retry,
            patch.object(orchestrator, "_schedule_knowledge_refresh", new=AsyncMock()),
            patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
            patch.object(orchestrator, "_ensure_rooms_exist", new=AsyncMock()),
            patch.object(orchestrator, "_ensure_room_invitations", new=AsyncMock()),
        ):
            updated = await orchestrator.update_config()

        assert updated is True
        assert orchestrator.agent_bots["coach"] is new_bot
        new_bot.ensure_rooms.assert_not_awaited()
        mock_schedule_retry.assert_awaited_once_with("coach")

    @pytest.mark.asyncio
    async def test_update_config_keeps_permanently_failed_new_bot_without_retry(self, tmp_path: Path) -> None:
        """Hot reload should retain permanently failed bots without scheduling retries."""
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))

        old_config = _runtime_bound_config(
            Config(
                agents={
                    "general": {
                        "display_name": "GeneralAgent",
                        "role": "General assistant",
                        "model": "default",
                        "rooms": ["lobby"],
                    },
                },
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
        )
        new_config = _runtime_bound_config(
            Config(
                agents={
                    "general": {
                        "display_name": "GeneralAgent",
                        "role": "General assistant",
                        "model": "default",
                        "rooms": ["lobby"],
                    },
                    "coach": {
                        "display_name": "Coach",
                        "role": "Coaching assistant",
                        "model": "default",
                        "rooms": ["lobby"],
                    },
                },
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
        )

        orchestrator.config = old_config
        orchestrator.running = True

        router_bot = MagicMock()
        router_bot.config = old_config
        router_bot.enable_streaming = True
        router_bot._set_presence_with_model_info = AsyncMock()
        general_bot = MagicMock()
        general_bot.config = old_config
        general_bot.enable_streaming = True
        general_bot._set_presence_with_model_info = AsyncMock()
        orchestrator.agent_bots = {"router": router_bot, "general": general_bot}

        new_bot = MagicMock()
        new_bot.agent_name = "coach"
        new_bot.running = False
        new_bot.try_start = AsyncMock(side_effect=PermanentMatrixStartupError("boom"))
        new_bot.ensure_rooms = AsyncMock(side_effect=AssertionError("ensure_rooms called on failed bot"))

        with (
            patch("mindroom.orchestrator.load_config", return_value=new_config),
            patch("mindroom.orchestrator.load_plugins"),
            patch(
                "mindroom.orchestration.config_updates._identify_entities_to_restart",
                return_value=set(),
            ),
            patch("mindroom.orchestrator.create_bot_for_entity", return_value=new_bot),
            patch("mindroom.orchestrator.create_temp_user", return_value=MagicMock()),
            patch.object(orchestrator, "_schedule_bot_start_retry", new=AsyncMock()) as mock_schedule_retry,
            patch.object(orchestrator, "_schedule_knowledge_refresh", new=AsyncMock()),
            patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
            patch.object(orchestrator, "_ensure_rooms_exist", new=AsyncMock()),
            patch.object(orchestrator, "_ensure_room_invitations", new=AsyncMock()),
        ):
            updated = await orchestrator.update_config()

        assert updated is True
        assert orchestrator.agent_bots["coach"] is new_bot
        new_bot.ensure_rooms.assert_not_awaited()
        mock_schedule_retry.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.requires_matrix  # Requires real Matrix server for orchestrator stop
    @pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
    @patch("mindroom.config.main.Config.from_yaml")
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

        with patch("mindroom.orchestrator.MultiAgentOrchestrator._ensure_user_account", new=AsyncMock()):
            orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
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
    @patch("mindroom.config.main.Config.from_yaml")
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

        with patch("mindroom.orchestrator.MultiAgentOrchestrator._ensure_user_account", new=AsyncMock()):
            orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
            await orchestrator.initialize()

            # All bots should have streaming disabled except teams (which never stream)
            for bot in orchestrator.agent_bots.values():
                if hasattr(bot, "enable_streaming"):
                    assert bot.enable_streaming is False
