"""Tests for the multi-agent bot system."""

from __future__ import annotations

import asyncio
import itertools
import os
import signal
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self, cast
from unittest.mock import AsyncMock, MagicMock, call, patch
from zoneinfo import ZoneInfo

import httpx
import nio
import pytest
import uvicorn
from agno.db.base import SessionType
from agno.knowledge.document import Document
from agno.knowledge.knowledge import Knowledge
from agno.media import Image
from agno.models.ollama import Ollama
from agno.run.agent import RunContentEvent
from agno.run.team import TeamRunOutput

from mindroom import interactive
from mindroom.attachments import _attachment_id_for_event, register_local_attachment
from mindroom.authorization import is_authorized_sender as is_authorized_sender_for_test
from mindroom.bot import (
    AgentBot,
    MultiKnowledgeVectorDb,
    TeamBot,
)
from mindroom.coalescing import PreparedTextEvent
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig, RouterConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    RuntimePaths,
    resolve_runtime_paths,
)
from mindroom.conversation_resolver import MessageContext
from mindroom.delivery_gateway import DeliveryResult, FinalDeliveryRequest, SuppressedPlaceholderCleanupError
from mindroom.handled_turns import HandledTurnState
from mindroom.history import CompactionOutcome
from mindroom.history.types import HistoryScope
from mindroom.hooks import (
    EVENT_MESSAGE_AFTER_RESPONSE,
    EVENT_MESSAGE_BEFORE_RESPONSE,
    EVENT_REACTION_RECEIVED,
    AfterResponseContext,
    BeforeResponseContext,
    EnrichmentItem,
    HookRegistry,
    MessageEnvelope,
    ReactionReceivedContext,
    hook,
)
from mindroom.inbound_turn_normalizer import DispatchPayload, DispatchPayloadWithAttachmentsRequest
from mindroom.knowledge.manager import KnowledgeManager
from mindroom.matrix.client import (
    PermanentMatrixStartupError,
    ResolvedVisibleMessage,
    ThreadHistoryResult,
    _ThreadHistoryFastPathUnavailableError,
)
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY, AgentMatrixUser
from mindroom.media_inputs import MediaInputs
from mindroom.message_target import MessageTarget
from mindroom.orchestration.runtime import (
    _matrix_homeserver_startup_timeout_seconds_from_env,
    run_with_retry,
    wait_for_matrix_homeserver,
)
from mindroom.orchestrator import (
    MultiAgentOrchestrator,
    _run_auxiliary_task_forever,
    _SignalAwareUvicornServer,
    main,
)
from mindroom.response_runner import ResponseRequest, ResponseRunner, _merge_response_extra_content
from mindroom.runtime_state import get_runtime_state, reset_runtime_state, set_runtime_ready
from mindroom.streaming import StreamingDeliveryError
from mindroom.teams import TeamIntent, TeamMemberStatus, TeamMode, TeamOutcome, TeamResolution, TeamResolutionMember
from mindroom.thread_summary import thread_summary_message_count_hint
from mindroom.tool_system.events import ToolTraceEntry
from mindroom.turn_controller import TurnController, _PrecheckedEvent
from mindroom.turn_policy import DispatchPlan, PreparedDispatch, ResponseAction, TurnPolicy
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_edit_message_mock,
    install_generate_response_mock,
    install_runtime_cache_support,
    install_send_response_mock,
    make_event_cache_mock,
    make_event_cache_write_coordinator_mock,
    patch_response_runner_module,
    replace_delivery_gateway_deps,
    replace_response_runner_deps,
    replace_turn_controller_deps,
    runtime_paths_for,
    test_runtime_paths,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)
from tests.conftest import (
    replace_turn_policy_deps as shared_replace_turn_policy_deps,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Sequence
    from pathlib import Path

    from mindroom.turn_store import TurnStore


def _make_matrix_client_mock() -> AsyncMock:
    """Return a minimal Matrix client mock for bot tests."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.rooms = {}
    client.user_id = "@mindroom_test:example.com"
    return client


def _wrap_extracted_collaborators(bot: AgentBot) -> AgentBot:
    """Wrap frozen extracted collaborators so tests can patch their methods."""
    wrapped_bot = wrap_extracted_collaborators(bot)
    replace_turn_controller_deps(
        wrapped_bot,
        resolver=wrapped_bot._conversation_resolver,
        normalizer=wrapped_bot._inbound_turn_normalizer,
        turn_policy=wrapped_bot._turn_policy,
        response_runner=wrapped_bot._response_runner,
        delivery_gateway=wrapped_bot._delivery_gateway,
        state_writer=wrapped_bot._conversation_state_writer,
    )
    return wrapped_bot


def _install_runtime_cache_support(bot: AgentBot | TeamBot) -> None:
    """Attach one required cache runtime double to a bot test instance."""
    bot.event_cache = make_event_cache_mock()
    bot.event_cache_write_coordinator = make_event_cache_write_coordinator_mock()


def _replace_turn_policy_deps(bot: AgentBot, **changes: object) -> TurnPolicy:
    """Rebuild the policy with the shared collaborator-replacement helper."""
    return shared_replace_turn_policy_deps(bot, **changes)


def _turn_store(bot: AgentBot | TeamBot) -> TurnStore:
    """Return the real turn store behind one wrapped bot."""
    return unwrap_extracted_collaborator(bot._turn_store)


def _mock_turn_store(bot: AgentBot | TeamBot, *, is_handled: bool = False) -> TurnStore:
    """Patch the existing turn store in place for tests that only need dedupe control."""
    turn_store = _turn_store(bot)
    turn_store.is_handled = MagicMock(return_value=is_handled)
    return turn_store


def _set_turn_store_tracker(bot: AgentBot | TeamBot, tracker: MagicMock) -> MagicMock:
    """Swap the private handled-turn ledger behind one turn store for test assertions."""
    _turn_store(bot)._ledger = tracker
    return tracker


def _replace_response_runner_runtime_deps(
    bot: AgentBot,
    **changes: object,
) -> ResponseRunner:
    """Rebuild the response coordinator with updated runtime-captured deps."""
    return replace_response_runner_deps(bot, **changes)


def _set_knowledge_for_agent(bot: AgentBot, knowledge_for_agent: MagicMock) -> MagicMock:
    """Replace the captured knowledge resolver on the real response coordinator."""
    bot._knowledge_access_support.for_agent = knowledge_for_agent
    return knowledge_for_agent


def _room_send_response(event_id: str) -> MagicMock:
    """Return a RoomSendResponse-shaped mock for Matrix send/edit tests."""
    response = MagicMock(spec=nio.RoomSendResponse, event_id=event_id)
    response.__class__ = nio.RoomSendResponse
    return response


def _agent_response_handled_turn(
    *,
    agent_name: str,
    room_id: str,
    event_id: str,
    response_event_id: str,
    thread_id: str | None = None,
    source_event_prompts: dict[str, str] | None = None,
) -> HandledTurnState:
    """Return the handled-turn state persisted for one direct agent response."""
    return HandledTurnState.from_source_event_id(
        event_id,
        response_event_id=response_event_id,
        source_event_prompts=source_event_prompts,
    ).with_response_context(
        response_owner=agent_name,
        history_scope=HistoryScope(kind="agent", scope_id=agent_name),
        conversation_target=MessageTarget.resolve(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=event_id,
        ),
    )


def _response_request(
    *,
    room_id: str = "!test:localhost",
    reply_to_event_id: str = "$event",
    thread_id: str | None = None,
    thread_history: Sequence[ResolvedVisibleMessage] = (),
    prompt: str = "Hello",
    model_prompt: str | None = None,
    existing_event_id: str | None = None,
    existing_event_is_placeholder: bool = False,
    user_id: str | None = "@user:localhost",
    media: MediaInputs | None = None,
    attachment_ids: Sequence[str] | None = None,
    response_envelope: MessageEnvelope | None = None,
    correlation_id: str | None = None,
    target: MessageTarget | None = None,
    matrix_run_metadata: dict[str, Any] | None = None,
    system_enrichment_items: tuple[EnrichmentItem, ...] = (),
) -> ResponseRequest:
    """Build one response request for direct bot seam tests."""
    return ResponseRequest(
        room_id=room_id,
        reply_to_event_id=reply_to_event_id,
        thread_id=thread_id,
        thread_history=thread_history,
        prompt=prompt,
        model_prompt=model_prompt,
        existing_event_id=existing_event_id,
        existing_event_is_placeholder=existing_event_is_placeholder,
        user_id=user_id,
        media=media,
        attachment_ids=tuple(attachment_ids) if attachment_ids is not None else None,
        response_envelope=response_envelope,
        correlation_id=correlation_id,
        target=target,
        matrix_run_metadata=matrix_run_metadata,
        system_enrichment_items=system_enrichment_items,
    )


def _runtime_bound_config(config: Config, runtime_root: Path) -> Config:
    """Return a runtime-bound config for bot tests."""
    return bind_runtime_paths(
        config,
        test_runtime_paths(runtime_root),
    )


def _mock_managed_bot(config: Config) -> MagicMock:
    """Return a lightweight managed-bot double for orchestrator reload tests."""
    bot = MagicMock()
    bot.config = config
    bot.enable_streaming = config.defaults.enable_streaming
    bot.event_cache = None
    bot.event_cache_write_coordinator = None
    bot._set_presence_with_model_info = AsyncMock()
    return bot


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


def _hook_plugin(name: str, callbacks: list[object]) -> SimpleNamespace:
    """Create a minimal plugin stub for hook registry tests."""
    return SimpleNamespace(
        name=name,
        discovered_hooks=tuple(callbacks),
        entry_config=PluginEntryConfig(path=f"./plugins/{name}"),
        plugin_order=0,
    )


def _hook_envelope(*, body: str = "hello", source_event_id: str = "$event") -> MessageEnvelope:
    """Create a minimal response envelope for hook-aware bot tests."""
    return MessageEnvelope(
        source_event_id=source_event_id,
        room_id="!test:localhost",
        target=MessageTarget.resolve("!test:localhost", None, source_event_id),
        requester_id="@user:localhost",
        sender_id="@user:localhost",
        body=body,
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="calculator",
        source_kind="message",
    )


def _visible_message(
    *,
    sender: str,
    body: str | None = None,
    event_id: str | None = None,
    timestamp: int | None = None,
    content: dict[str, object] | None = None,
) -> ResolvedVisibleMessage:
    """Create a typed visible message for bot thread-history tests."""
    return ResolvedVisibleMessage.synthetic(
        sender=sender,
        body=body,
        event_id=event_id,
        timestamp=timestamp,
        content=content,
    )


def test_agent_bot_init_defers_matrix_id_access_until_after_user_id_is_populated(tmp_path: Path) -> None:
    """Bot init should not parse an empty Matrix user ID while wiring helper deps."""
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

    bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))

    agent_user.user_id = "@mindroom_calculator:localhost"
    bot.client = AsyncMock()
    bot.client.user_id = agent_user.user_id
    assert bot._conversation_resolver._matrix_id().full_id == "@mindroom_calculator:localhost"
    assert bot._inbound_turn_normalizer.deps.sender_domain == "localhost"


@asynccontextmanager
async def _noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
    yield


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


def _make_compaction_outcome(*, mode: str = "auto", notify: bool = True) -> CompactionOutcome:
    return CompactionOutcome(
        mode=mode,
        session_id="!test:localhost:$thread_root_id",
        scope="agent:general",
        summary="## Goal\nPreserve <summary> & keep context.",
        summary_model="compact-model",
        before_tokens=30000,
        after_tokens=12000,
        window_tokens=200000,
        threshold_tokens=100000,
        reserve_tokens=16384,
        runs_before=18,
        runs_after=7,
        compacted_run_count=12,
        compacted_at="2026-03-22T20:15:00Z",
        notify=notify,
    )


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
            event.server_timestamp = 1234567890
            event.source = {"content": {"body": "hello"}}
        elif handler_name == "image":
            event = MagicMock(spec=nio.RoomMessageImage)
            event.body = "image.jpg"
            event.server_timestamp = 1000
            event.source = {"content": {"body": "image.jpg"}}
        elif handler_name == "voice":
            event = MagicMock(spec=nio.RoomMessageAudio)
            event.body = "voice"
            event.server_timestamp = 1000
            event.source = {"content": {"body": "voice"}}
        elif handler_name == "file":
            event = MagicMock(spec=nio.RoomMessageFile)
            event.body = "report.pdf"
            event.url = "mxc://localhost/report"
            event.server_timestamp = 1000
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

        assert bot._knowledge_access_support.for_agent("calculator") is None

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

        assert bot._knowledge_access_support.for_agent("calculator") is expected_knowledge

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

        combined_knowledge = bot._knowledge_access_support.for_agent("calculator")
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
            bot._knowledge_access_support.for_agent(
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
    @patch("mindroom.bot.interactive.init_persistence")
    @patch("mindroom.config.main.Config.from_yaml")
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
        mock_init_persistence.assert_called_once_with(runtime_paths_for(config).storage_root)
        assert (
            mock_client.add_event_callback.call_count == 12
        )  # invite, message, redaction, reaction, audio, image/file/video callbacks

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

        async def _sync_forever(*_args: object, **_kwargs: object) -> None:
            call_order.append("sync")

        mock_client.sync_forever = AsyncMock(side_effect=_sync_forever)
        mock_login.return_value = mock_client
        mock_ensure_user.return_value = None

        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
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

        async def _run_auxiliary(
            task_name: str,
            operation: Callable[[], Awaitable[None]],
            *,
            should_restart: Callable[[], bool] | None = None,
        ) -> None:
            del task_name
            del should_restart
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
        bot.client = _make_matrix_client_mock()
        bot.client.next_batch = "s_test_token"
        bot.running = True

        await bot.stop()

        assert not bot.running
        bot.client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_agent_bot_on_invite(self, mock_agent_user: AgentMatrixUser, tmp_path: Path) -> None:
        """Test handling room invitations."""
        config = self._config_for_storage(tmp_path)

        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
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
        _install_runtime_cache_support(bot)
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
        _install_runtime_cache_support(bot)
        bot.client = AsyncMock()

        mock_room = MagicMock()
        mock_event = MagicMock()
        mock_event.sender = "@mindroom_general:localhost"  # Another agent

        await bot._on_message(mock_room, mock_event)

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
            assert stream_kwargs["prompt"].endswith(f"{mention_id}: What's 2+2?")
            assert stream_kwargs["prompt"].startswith("[")
            assert stream_kwargs["session_id"] == "!test:localhost:$thread_root_id"
            assert stream_kwargs["runtime_paths"].storage_root == runtime_paths_for(config).storage_root
            assert stream_kwargs["config"] == config
            assert stream_kwargs["thread_history"] == []
            assert stream_kwargs["room_id"] == "!test:localhost"
            assert stream_kwargs["knowledge"] is None
            assert stream_kwargs["user_id"] == "@user:localhost"
            assert isinstance(stream_kwargs["run_id"], str)
            assert stream_kwargs["run_id"]
            assert stream_kwargs["media"] == MediaInputs()
            assert stream_kwargs["reply_to_event_id"] == "event123"
            assert stream_kwargs["show_tool_calls"] is True
            assert stream_kwargs["run_metadata_collector"] == {}
            assert stream_kwargs["compaction_outcomes_collector"] == []
            mock_ai_response.assert_not_called()
            # With streaming and stop button: initial message + reaction + edits
            # Note: The exact count may vary based on implementation
            assert bot.client.room_send.call_count >= 2
        else:
            mock_ai_response.assert_called_once()
            ai_kwargs = mock_ai_response.call_args.kwargs
            assert ai_kwargs["agent_name"] == "calculator"
            assert ai_kwargs["prompt"].endswith(f"{mention_id}: What's 2+2?")
            assert ai_kwargs["prompt"].startswith("[")
            assert ai_kwargs["session_id"] == "!test:localhost:$thread_root_id"
            assert ai_kwargs["runtime_paths"].storage_root == runtime_paths_for(config).storage_root
            assert ai_kwargs["config"] == config
            assert ai_kwargs["thread_history"] == []
            assert ai_kwargs["room_id"] == "!test:localhost"
            assert ai_kwargs["knowledge"] is None
            assert ai_kwargs["user_id"] == "@user:localhost"
            assert isinstance(ai_kwargs["run_id"], str)
            assert ai_kwargs["run_id"]
            assert ai_kwargs["media"] == MediaInputs()
            assert ai_kwargs["reply_to_event_id"] == "event123"
            assert ai_kwargs["show_tool_calls"] is True
            assert ai_kwargs["tool_trace_collector"] == []
            assert ai_kwargs["run_metadata_collector"] == {}
            assert ai_kwargs["compaction_outcomes_collector"] == []
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
        bot.client.room_send.return_value = _room_send_response("$response")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        mock_ai = AsyncMock(return_value="Handled")
        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            ai_response=mock_ai,
        ):
            delivery = await bot._response_runner.process_and_respond(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please send an update",
                    reply_to_event_id="$event123",
                    thread_history=[],
                    user_id="@user:localhost",
                ),
            )

        assert delivery.event_id == "$response"
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
        bot.client.room_send.return_value = _room_send_response("$response")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        mock_ai = AsyncMock(return_value="Handled")
        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            ai_response=mock_ai,
        ):
            delivery = await bot._response_runner.process_and_respond(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please send an update",
                    reply_to_event_id="$event123",
                    thread_history=[],
                    user_id="@user:localhost",
                ),
            )

        assert delivery.event_id == "$response"
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
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        bot._handle_interactive_question = AsyncMock()
        mock_stream_agent_response = AsyncMock()

        with patch(
            "mindroom.delivery_gateway.send_streaming_response",
            new_callable=AsyncMock,
        ) as mock_send_streaming_response:
            mock_stream_agent_response.return_value = mock_streaming_response()
            mock_send_streaming_response.return_value = ("$response", "chunk")
            with patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=mock_stream_agent_response,
            ):
                delivery = await bot._response_runner.process_and_respond_streaming(
                    _response_request(
                        room_id="!test:localhost",
                        prompt="Please reply in thread",
                        reply_to_event_id="$event456",
                        thread_history=[],
                        user_id="@user:localhost",
                    ),
                )

        assert delivery.event_id == "$response"
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
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        bot._handle_interactive_question = AsyncMock()
        mock_stream_agent_response = AsyncMock(return_value=mock_streaming_response())
        with patch(
            "mindroom.delivery_gateway.send_streaming_response",
            new_callable=AsyncMock,
        ) as mock_send_streaming_response:
            mock_send_streaming_response.return_value = ("$response", "chunk")
            with patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=mock_stream_agent_response,
            ):
                delivery = await bot._response_runner.process_and_respond_streaming(
                    _response_request(
                        room_id="!test:localhost",
                        prompt="Hello",
                        reply_to_event_id="$event456",
                        thread_history=[],
                        user_id="@user:localhost",
                    ),
                )

        assert delivery.event_id == "$response"
        bot._knowledge_access_support.for_agent.assert_called_once()
        args, kwargs = bot._knowledge_access_support.for_agent.call_args
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
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot.client.room_send.return_value = _room_send_response("$response")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            kwargs["run_metadata_collector"]["io.mindroom.ai_run"] = {"version": 1}
            return "Handled"

        mock_ai = AsyncMock(side_effect=fake_ai_response)
        attachment_ids = ["att_image", "att_zip"]
        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            ai_response=mock_ai,
        ):
            await bot._response_runner.process_and_respond(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please inspect attachments",
                    reply_to_event_id="$event123",
                    thread_history=[],
                    user_id="@user:localhost",
                    attachment_ids=attachment_ids,
                ),
            )

        sent_extra_content = bot.client.room_send.await_args.kwargs["content"]
        assert sent_extra_content[ATTACHMENT_IDS_KEY] == attachment_ids
        assert sent_extra_content["io.mindroom.ai_run"]["version"] == 1

    @pytest.mark.asyncio
    async def test_process_and_respond_streaming_includes_attachment_ids_in_response_metadata(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming responses should persist attachment IDs in message metadata."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        bot._handle_interactive_question = AsyncMock()

        captured_collector: dict[str, Any] = {}

        def fake_stream_agent_response(*_args: object, **kwargs: object) -> AsyncGenerator[str, None]:
            captured_collector.update({"ref": kwargs["run_metadata_collector"]})

            async def _gen() -> AsyncGenerator[str, None]:
                yield "chunk"
                # Populate metadata during iteration, matching production ordering
                # where ai.py populates metadata after streaming completes.
                kwargs["run_metadata_collector"]["io.mindroom.ai_run"] = {"version": 1}

            return _gen()

        async def _consuming_send_streaming(*args: object, **_kwargs: object) -> tuple[str, str]:
            stream = args[7]  # response_stream positional arg
            async for _ in stream:
                pass
            return ("$response", "chunk")

        attachment_ids = ["att_image", "att_zip"]
        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
                side_effect=_consuming_send_streaming,
            ) as mock_send_streaming_response,
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=fake_stream_agent_response,
            ),
        ):
            await bot._response_runner.process_and_respond_streaming(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please inspect attachments",
                    reply_to_event_id="$event456",
                    thread_history=[],
                    user_id="@user:localhost",
                    attachment_ids=attachment_ids,
                ),
            )

        sent_extra_content = mock_send_streaming_response.await_args.kwargs["extra_content"]
        assert sent_extra_content[ATTACHMENT_IDS_KEY] == attachment_ids
        # Metadata was populated during generator iteration (not synchronously),
        # proving the mutable reference is preserved through _merge_response_extra_content.
        assert sent_extra_content["io.mindroom.ai_run"]["version"] == 1
        # The extra_content dict IS the same object as the collector
        assert sent_extra_content is captured_collector["ref"]

    def test_merge_response_extra_content_preserves_mutable_reference(self) -> None:
        """_merge_response_extra_content must return the SAME dict object when extra_content is provided."""
        collector: dict[str, Any] = {}
        result = _merge_response_extra_content(collector, None)
        assert result is collector

    def test_merge_response_extra_content_returns_none_when_both_absent(self) -> None:
        """_merge_response_extra_content returns None when no extra_content and no attachment_ids."""
        assert _merge_response_extra_content(None, None) is None
        assert _merge_response_extra_content(None, []) is None

    def test_merge_response_extra_content_merges_attachment_ids(self) -> None:
        """_merge_response_extra_content merges attachment_ids into extra_content."""
        collector: dict[str, Any] = {}
        result = _merge_response_extra_content(collector, ["att_1"])
        assert result is collector
        assert result[ATTACHMENT_IDS_KEY] == ["att_1"]

    def test_merge_response_extra_content_creates_dict_for_attachment_ids_only(self) -> None:
        """_merge_response_extra_content creates a dict when only attachment_ids are provided."""
        result = _merge_response_extra_content(None, ["att_1"])
        assert result is not None
        assert result[ATTACHMENT_IDS_KEY] == ["att_1"]

    @pytest.mark.asyncio
    async def test_streaming_metadata_propagation_through_mutable_reference(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Metadata populated during generator iteration must appear in extra_content via mutable reference."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        bot._handle_interactive_question = AsyncMock()

        def fake_stream_agent_response(*_args: object, **kwargs: object) -> AsyncGenerator[str, None]:
            async def _gen() -> AsyncGenerator[str, None]:
                yield "hello"
                # Populate after first yield, mimicking production ai.py ordering
                kwargs["run_metadata_collector"]["io.mindroom.ai_run"] = {
                    "version": 1,
                    "model": "test-model",
                    "tokens": {"input": 10, "output": 5},
                }

            return _gen()

        async def _consuming_send_streaming(*args: object, **_kwargs: object) -> tuple[str, str]:
            stream = args[7]
            async for _ in stream:
                pass
            return ("$response", "hello")

        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
                side_effect=_consuming_send_streaming,
            ) as mock_send_streaming_response,
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=fake_stream_agent_response,
            ),
        ):
            await bot._response_runner.process_and_respond_streaming(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Hello",
                    reply_to_event_id="$event789",
                    thread_history=[],
                    user_id="@user:localhost",
                ),
            )

        sent_extra_content = mock_send_streaming_response.await_args.kwargs["extra_content"]
        assert sent_extra_content is not None
        ai_run = sent_extra_content["io.mindroom.ai_run"]
        assert ai_run["version"] == 1
        assert ai_run["model"] == "test-model"
        assert ai_run["tokens"] == {"input": 10, "output": 5}

    @pytest.mark.asyncio
    async def test_streaming_cancelled_response_preserves_metadata(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """CancelledError during streaming must still carry io.mindroom.ai_run in extra_content."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        bot._handle_interactive_question = AsyncMock()

        def fake_stream_agent_response(*_args: object, **kwargs: object) -> AsyncGenerator[str, None]:
            async def _gen() -> AsyncGenerator[str, None]:
                kwargs["run_metadata_collector"]["io.mindroom.ai_run"] = {"version": 1}
                yield "partial"
                raise asyncio.CancelledError

            return _gen()

        captured_extra_content_ref: list[dict[str, Any] | None] = [None]

        async def _consuming_send_streaming(*args: object, **kwargs: object) -> tuple[str, str]:
            captured_extra_content_ref[0] = kwargs.get("extra_content")
            stream = args[7]
            try:
                async for _ in stream:
                    pass
            except asyncio.CancelledError:
                pass
            # In production, send_streaming_response catches CancelledError,
            # sends the final edit, then re-raises. We simulate the re-raise.
            raise asyncio.CancelledError

        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
                side_effect=_consuming_send_streaming,
            ),
            pytest.raises(asyncio.CancelledError),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=fake_stream_agent_response,
            ),
        ):
            await bot._response_runner.process_and_respond_streaming(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Cancel me",
                    reply_to_event_id="$event_cancel",
                    thread_history=[],
                    user_id="@user:localhost",
                ),
            )

        # The extra_content dict (mutable reference) was populated during iteration
        extra = captured_extra_content_ref[0]
        assert extra is not None
        assert "io.mindroom.ai_run" in extra
        assert extra["io.mindroom.ai_run"]["version"] == 1

    @pytest.mark.asyncio
    async def test_process_and_respond_streaming_preserves_terminal_event_id_on_error(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming failures should preserve the terminal event id after finalizing the visible message."""

        async def mock_streaming_response() -> AsyncGenerator[str, None]:
            yield "chunk"

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        bot._handle_interactive_question = AsyncMock()
        mock_stream_agent_response = AsyncMock(return_value=mock_streaming_response())
        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
                side_effect=StreamingDeliveryError(
                    RuntimeError("boom"),
                    event_id="$terminal",
                    accumulated_text="partial\n\n**[Response interrupted by an error: boom]**",
                    tool_trace=[],
                ),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=mock_stream_agent_response,
            ),
        ):
            delivery = await bot._response_runner.process_and_respond_streaming(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please continue",
                    reply_to_event_id="$event-error",
                    thread_history=[],
                    user_id="@user:localhost",
                ),
            )

        assert delivery.event_id == "$terminal"
        assert delivery.delivery_kind == "sent"
        assert "Response interrupted by an error" in delivery.response_text

    @pytest.mark.asyncio
    async def test_process_and_respond_applies_before_and_after_hooks_non_streaming(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Non-streaming responses should pass through before/after hooks."""
        after_results: list[tuple[str, str, str, str]] = []
        before_calls = 0

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            nonlocal before_calls
            before_calls += 1
            ctx.draft.response_text = f"{ctx.draft.response_text} [hooked]"

        @hook(EVENT_MESSAGE_AFTER_RESPONSE)
        async def after_hook(ctx: AfterResponseContext) -> None:
            after_results.append(
                (
                    ctx.result.response_event_id,
                    ctx.result.response_text,
                    ctx.result.delivery_kind,
                    ctx.result.response_kind,
                ),
            )

        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot.client.room_send.return_value = _room_send_response("$response")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [before_hook, after_hook])])
        mock_ai = AsyncMock(return_value="Handled")
        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            ai_response=mock_ai,
        ):
            delivery = await bot._response_runner.process_and_respond(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please send an update",
                    reply_to_event_id="$event123",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=_hook_envelope(body="Please send an update", source_event_id="$event123"),
                    correlation_id="corr-hook",
                ),
            )

        assert delivery.event_id == "$response"
        assert before_calls == 1
        assert bot.client.room_send.await_args.kwargs["content"]["body"] == "Handled [hooked]"
        assert after_results == [("$response", "Handled [hooked]", "sent", "ai")]

    @pytest.mark.asyncio
    async def test_process_and_respond_passes_active_response_event_ids(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Non-streaming AI calls should receive only live tracked event IDs for the room."""

        @asynccontextmanager
        async def noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            yield

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot.client.room_send.return_value = _room_send_response("$response")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        running_task = asyncio.create_task(asyncio.sleep(60))
        done_task = asyncio.create_task(asyncio.sleep(0))
        other_room_task = asyncio.create_task(asyncio.sleep(60))
        await done_task
        bot.stop_manager.set_current("$active", MessageTarget.resolve("!test:localhost", None, "$active"), running_task)
        bot.stop_manager.set_current("$done", MessageTarget.resolve("!test:localhost", None, "$done"), done_task)
        bot.stop_manager.set_current(
            "$other-room",
            MessageTarget.resolve("!other:localhost", None, "$other-room"),
            other_room_task,
        )

        try:
            mock_ai_response = AsyncMock(return_value="Handled")
            with patch_response_runner_module(
                typing_indicator=noop_typing_indicator,
                ai_response=mock_ai_response,
            ):
                await bot._response_runner.process_and_respond(
                    _response_request(
                        room_id="!test:localhost",
                        prompt="Please continue",
                        reply_to_event_id="$event123",
                        thread_history=[],
                        user_id="@user:localhost",
                    ),
                )

            assert mock_ai_response.call_args.kwargs["active_event_ids"] == {"$active"}
        finally:
            running_task.cancel()
            other_room_task.cancel()
            await asyncio.gather(running_task, other_room_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_process_and_respond_streaming_applies_before_and_after_hooks_once(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming responses should fire hooks once after the stream settles."""
        after_results: list[tuple[str, str, str, str]] = []
        before_calls = 0

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            nonlocal before_calls
            before_calls += 1
            ctx.draft.response_text = f"{ctx.draft.response_text} [hooked]"

        @hook(EVENT_MESSAGE_AFTER_RESPONSE)
        async def after_hook(ctx: AfterResponseContext) -> None:
            after_results.append(
                (
                    ctx.result.response_event_id,
                    ctx.result.response_text,
                    ctx.result.delivery_kind,
                    ctx.result.response_kind,
                ),
            )

        async def mock_streaming_response() -> AsyncGenerator[str, None]:
            yield "chunk"

        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [before_hook, after_hook])])
        mock_stream_agent_response = AsyncMock(return_value=mock_streaming_response())
        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
            ) as mock_send_streaming_response,
            patch(
                "mindroom.delivery_gateway.edit_message",
                new=AsyncMock(return_value=_room_send_response("$edit")),
            ) as mock_edit_message,
        ):
            mock_send_streaming_response.return_value = ("$response", "chunk")
            with patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=mock_stream_agent_response,
            ):
                delivery = await bot._response_runner.process_and_respond_streaming(
                    _response_request(
                        room_id="!test:localhost",
                        prompt="Please reply in thread",
                        reply_to_event_id="$event456",
                        thread_history=[],
                        user_id="@user:localhost",
                        response_envelope=_hook_envelope(body="Please reply in thread", source_event_id="$event456"),
                        correlation_id="corr-stream",
                    ),
                )

        assert delivery.event_id == "$response"
        assert before_calls == 1
        assert mock_edit_message.await_args.args[4] == "chunk [hooked]"
        assert after_results == [("$response", "chunk [hooked]", "edited", "ai")]

    @pytest.mark.asyncio
    async def test_process_and_respond_streaming_passes_active_response_event_ids(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming AI calls should receive only live tracked event IDs for the room."""

        @asynccontextmanager
        async def noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            yield

        async def mock_streaming_response() -> AsyncGenerator[str, None]:
            yield "chunk"

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        bot._handle_interactive_question = AsyncMock()

        running_task = asyncio.create_task(asyncio.sleep(60))
        done_task = asyncio.create_task(asyncio.sleep(0))
        other_room_task = asyncio.create_task(asyncio.sleep(60))
        await done_task
        bot.stop_manager.set_current("$active", MessageTarget.resolve("!test:localhost", None, "$active"), running_task)
        bot.stop_manager.set_current("$done", MessageTarget.resolve("!test:localhost", None, "$done"), done_task)
        bot.stop_manager.set_current(
            "$other-room",
            MessageTarget.resolve("!other:localhost", None, "$other-room"),
            other_room_task,
        )

        try:
            mock_stream = AsyncMock(return_value=mock_streaming_response())
            with patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
            ) as mock_send_streaming_response:
                mock_send_streaming_response.return_value = ("$response", "chunk")
                with patch_response_runner_module(
                    typing_indicator=noop_typing_indicator,
                    stream_agent_response=mock_stream,
                ):
                    await bot._response_runner.process_and_respond_streaming(
                        _response_request(
                            room_id="!test:localhost",
                            prompt="Please continue",
                            reply_to_event_id="$event456",
                            thread_history=[],
                            user_id="@user:localhost",
                        ),
                    )

            assert mock_stream.call_args.kwargs["active_event_ids"] == {"$active"}
        finally:
            running_task.cancel()
            other_room_task.cancel()
            await asyncio.gather(running_task, other_room_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_applies_hooks_to_final_team_message(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Team final output should use the same before/after hook flow."""
        after_results: list[tuple[str, str, str, str]] = []

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            ctx.draft.response_text = f"{ctx.draft.response_text} [hooked]"

        @hook(EVENT_MESSAGE_AFTER_RESPONSE)
        async def after_hook(ctx: AfterResponseContext) -> None:
            after_results.append(
                (
                    ctx.result.response_event_id,
                    ctx.result.response_text,
                    ctx.result.delivery_kind,
                    ctx.result.response_kind,
                ),
            )

        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot._send_response = AsyncMock(return_value="$team")
        install_send_response_mock(bot, bot._send_response)
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [before_hook, after_hook])])
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        matrix_ids = config.get_ids(runtime_paths_for(config))
        with (
            patch(
                "mindroom.delivery_gateway.edit_message",
                new=AsyncMock(return_value=_room_send_response("$edit")),
            ) as mock_edit_message,
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response=AsyncMock(return_value="Team reply"),
            ),
        ):
            event_id = await bot._generate_team_response_helper(
                room_id="!test:localhost",
                reply_to_event_id="$team-root",
                thread_id=None,
                team_agents=[matrix_ids["calculator"], matrix_ids["general"]],
                team_mode="collaborate",
                thread_history=[],
                requester_user_id="@user:localhost",
                payload=DispatchPayload(prompt="team prompt"),
                response_envelope=_hook_envelope(body="team prompt", source_event_id="$team-root"),
                strip_transient_enrichment_after_run=True,
                correlation_id="corr-team",
            )

        assert event_id == "$team"
        assert mock_edit_message.await_args.args[4] == "Team reply [hooked]"
        assert after_results == [("$team", "Team reply [hooked]", "edited", "team")]

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_strips_enrichment_from_shared_team_session(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Shared team responses should strip transient enrichment from persisted session history."""
        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot._send_response = AsyncMock(return_value="$team")
        install_send_response_mock(bot, bot._send_response)
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        matrix_ids = config.get_ids(runtime_paths_for(config))
        storage = MagicMock()
        mock_strip_enrichment = MagicMock()
        bot._conversation_state_writer.create_storage = MagicMock(return_value=storage)
        with (
            patch(
                "mindroom.delivery_gateway.edit_message",
                new=AsyncMock(return_value=_room_send_response("$edit")),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response=AsyncMock(return_value="Team reply"),
                strip_enrichment_from_session_storage=mock_strip_enrichment,
            ),
        ):
            event_id = await bot._generate_team_response_helper(
                room_id="!test:localhost",
                reply_to_event_id="$team-root",
                thread_id=None,
                team_agents=[matrix_ids["calculator"], matrix_ids["general"]],
                team_mode="collaborate",
                thread_history=[],
                requester_user_id="@user:localhost",
                payload=DispatchPayload(prompt="team prompt"),
                response_envelope=_hook_envelope(body="team prompt", source_event_id="$team-root"),
                strip_transient_enrichment_after_run=True,
                correlation_id="corr-team",
            )

        assert event_id == "$team"
        mock_strip_enrichment.assert_called_once_with(
            storage,
            MessageTarget.resolve(
                room_id="!test:localhost",
                thread_id=None,
                reply_to_event_id="$team-root",
            ).session_id,
            session_type=SessionType.TEAM,
        )

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_uses_resolved_thread_root_for_placeholder_and_edit(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Team helper should preserve the canonical thread root across placeholder and edit flow."""
        sent_contents: list[dict[str, object]] = []

        async def record_send(_client: object, _room_id: str, content: dict[str, object]) -> str:
            sent_contents.append(content)
            return "$team"

        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths,
        )
        matrix_ids = config.get_ids(runtime_paths)
        envelope = MessageEnvelope(
            source_event_id="$reply_plain:localhost",
            room_id="!test:localhost",
            target=MessageTarget.resolve(
                room_id="!test:localhost",
                thread_id="$raw_thread:localhost",
                reply_to_event_id="$reply_plain:localhost",
            ).with_thread_root("$canonical_thread:localhost"),
            requester_id="@user:localhost",
            sender_id="@user:localhost",
            body="team prompt",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=mock_agent_user.agent_name,
            source_kind="message",
        )

        with (
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_latest_thread_event_id_if_needed",
                new=AsyncMock(return_value="$latest:localhost"),
            ),
            patch("mindroom.delivery_gateway.send_message", new=AsyncMock(side_effect=record_send)),
            patch(
                "mindroom.delivery_gateway.edit_message",
                new=AsyncMock(return_value=_room_send_response("$edit")),
            ) as mock_edit_message,
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response=AsyncMock(return_value="Team reply"),
            ),
        ):
            event_id = await bot._generate_team_response_helper(
                room_id="!test:localhost",
                reply_to_event_id="$reply_plain:localhost",
                thread_id="$raw_thread:localhost",
                team_agents=[matrix_ids["calculator"], matrix_ids["general"]],
                team_mode="collaborate",
                thread_history=[],
                requester_user_id="@user:localhost",
                payload=DispatchPayload(prompt="team prompt"),
                response_envelope=envelope,
                correlation_id="corr-team",
            )

        assert event_id == "$team"
        assert len(sent_contents) == 1
        content = sent_contents[0]
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$canonical_thread:localhost"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$reply_plain:localhost"
        assert mock_edit_message.await_args.args[3]["m.relates_to"]["event_id"] == "$canonical_thread:localhost"

    @pytest.mark.asyncio
    async def test_deliver_generated_response_redacts_suppressed_placeholder(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Suppressing a placeholder-backed response should redact the provisional event."""

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            ctx.draft.suppress = True

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        redact_message_event = AsyncMock(return_value=True)
        gateway = replace_delivery_gateway_deps(
            bot,
            redact_message_event=redact_message_event,
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(
                    return_value=SimpleNamespace(
                        response_text="Handled",
                        response_kind="ai",
                        tool_trace=None,
                        extra_content=None,
                        envelope=response_envelope,
                        suppress=True,
                    ),
                ),
                emit_after_response=AsyncMock(),
                emit_cancelled_response=AsyncMock(),
            ),
        )

        delivery = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                existing_event_id="$placeholder",
                existing_event_is_placeholder=True,
                response_text="Handled",
                response_kind="ai",
                response_envelope=response_envelope,
                correlation_id="corr-deliver-suppress",
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert delivery.suppressed is True
        assert delivery.event_id is None
        redact_message_event.assert_awaited_once_with(
            room_id="!test:localhost",
            event_id="$placeholder",
            reason="Suppressed placeholder response",
        )
        assert (
            unwrap_extracted_collaborator(bot._response_runner).resolve_response_event_id(
                delivery_result=delivery,
                tracked_event_id="$placeholder",
                existing_event_id="$placeholder",
                existing_event_is_placeholder=True,
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_deliver_generated_response_suppressed_existing_event_returns_no_final_event(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Suppressing a non-placeholder edit should not preserve the stale event id."""

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            ctx.draft.suppress = True

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        redact_message_event = AsyncMock()
        gateway = replace_delivery_gateway_deps(
            bot,
            redact_message_event=redact_message_event,
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(
                    return_value=SimpleNamespace(
                        response_text="Handled",
                        response_kind="ai",
                        tool_trace=None,
                        extra_content=None,
                        envelope=response_envelope,
                        suppress=True,
                    ),
                ),
                emit_after_response=AsyncMock(),
                emit_cancelled_response=AsyncMock(),
            ),
        )

        delivery = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                existing_event_id="$existing",
                existing_event_is_placeholder=False,
                response_text="Handled",
                response_kind="ai",
                response_envelope=response_envelope,
                correlation_id="corr-deliver-existing-suppress",
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert delivery.suppressed is True
        assert delivery.event_id is None
        redact_message_event.assert_not_awaited()
        assert (
            unwrap_extracted_collaborator(bot._response_runner).resolve_response_event_id(
                delivery_result=delivery,
                tracked_event_id="$existing",
                existing_event_id="$existing",
                existing_event_is_placeholder=False,
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_deliver_generated_response_raises_when_suppressed_placeholder_redaction_fails(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A failed placeholder redaction should bubble so callers keep the turn retryable."""

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            ctx.draft.suppress = True

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        redact_message_event = AsyncMock(return_value=False)
        gateway = replace_delivery_gateway_deps(
            bot,
            redact_message_event=redact_message_event,
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(
                    return_value=SimpleNamespace(
                        response_text="Handled",
                        response_kind="ai",
                        tool_trace=None,
                        extra_content=None,
                        envelope=response_envelope,
                        suppress=True,
                    ),
                ),
                emit_after_response=AsyncMock(),
                emit_cancelled_response=AsyncMock(),
            ),
        )

        with pytest.raises(SuppressedPlaceholderCleanupError):
            await gateway.deliver_final(
                FinalDeliveryRequest(
                    target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                    existing_event_id="$placeholder",
                    existing_event_is_placeholder=True,
                    response_text="Handled",
                    response_kind="ai",
                    response_envelope=response_envelope,
                    correlation_id="corr-deliver-suppress-fail",
                    tool_trace=None,
                    extra_content=None,
                ),
            )

        redact_message_event.assert_awaited_once_with(
            room_id="!test:localhost",
            event_id="$placeholder",
            reason="Suppressed placeholder response",
        )

    @pytest.mark.asyncio
    async def test_process_and_respond_streaming_redacts_suppressed_provisional_response(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Suppressing after the first streamed send should remove the provisional event."""

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            ctx.draft.suppress = True

        async def mock_streaming_response() -> AsyncGenerator[str, None]:
            yield "chunk"

        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        bot._knowledge_for_agent = MagicMock(return_value=None)
        redact_message_event = AsyncMock(return_value=True)
        replace_delivery_gateway_deps(
            bot,
            redact_message_event=redact_message_event,
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(
                    return_value=SimpleNamespace(
                        response_text="chunk",
                        response_kind="ai",
                        tool_trace=None,
                        extra_content=None,
                        envelope=_hook_envelope(body="Please reply in thread", source_event_id="$event456"),
                        suppress=True,
                    ),
                ),
                emit_after_response=AsyncMock(),
                emit_cancelled_response=AsyncMock(),
            ),
        )

        with (
            patch("mindroom.response_runner.typing_indicator", _noop_typing_indicator),
            patch("mindroom.response_runner.stream_agent_response") as mock_stream_agent_response,
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
            ) as mock_send_streaming_response,
        ):
            mock_stream_agent_response.return_value = mock_streaming_response()
            mock_send_streaming_response.return_value = ("$streaming", "chunk")
            delivery = await bot._response_runner.process_and_respond_streaming(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please reply in thread",
                    reply_to_event_id="$event456",
                    thread_id=None,
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=_hook_envelope(
                        body="Please reply in thread",
                        source_event_id="$event456",
                    ),
                    correlation_id="corr-stream-suppress",
                ),
            )

        assert delivery.suppressed is True
        assert delivery.event_id is None
        redact_message_event.assert_awaited_once_with(
            room_id="!test:localhost",
            event_id="$streaming",
            reason="Suppressed streamed response",
        )
        assert (
            unwrap_extracted_collaborator(bot._response_runner).resolve_response_event_id(
                delivery_result=delivery,
                tracked_event_id="$streaming",
                existing_event_id=None,
            )
            is None
        )

    def test_resolve_response_event_id_does_not_preserve_placeholder_after_failed_edit(self) -> None:
        """A failed edit must not report the placeholder as the final delivered event."""
        delivery = DeliveryResult(
            event_id=None,
            response_text="Handled",
            delivery_kind=None,
            suppressed=False,
        )
        coordinator = MagicMock(spec=ResponseRunner)

        assert (
            ResponseRunner.resolve_response_event_id(
                coordinator,
                delivery_result=delivery,
                tracked_event_id="$placeholder",
                existing_event_id="$placeholder",
                existing_event_is_placeholder=True,
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_registers_interactive_questions_with_bot_agent_name(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Team interactive questions should be owned by the real bot agent name."""
        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot._send_response = AsyncMock(return_value="$team")
        install_send_response_mock(bot, bot._send_response)
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        matrix_ids = config.get_ids(runtime_paths_for(config))
        interactive_response = """```interactive
{"question":"Choose","options":[{"emoji":"✅","label":"Yes","value":"yes"}]}
```"""
        with (
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response=AsyncMock(return_value=interactive_response),
            ),
            patch("mindroom.delivery_gateway.edit_message", new=AsyncMock(return_value=_room_send_response("$edit"))),
            patch("mindroom.bot.interactive.register_interactive_question") as mock_register,
            patch("mindroom.bot.interactive.add_reaction_buttons", new_callable=AsyncMock) as mock_add_buttons,
        ):
            event_id = await bot._generate_team_response_helper(
                room_id="!test:localhost",
                reply_to_event_id="$team-root",
                thread_id=None,
                team_agents=[matrix_ids["calculator"], matrix_ids["general"]],
                team_mode="collaborate",
                thread_history=[],
                requester_user_id="@user:localhost",
                payload=DispatchPayload(prompt="team prompt"),
            )

        assert event_id == "$team"
        mock_register.assert_called_once()
        assert mock_register.call_args.args[0] == "$team"
        assert mock_register.call_args.args[1] == "!test:localhost"
        assert mock_register.call_args.args[2] == "$team-root"
        assert mock_register.call_args.args[4] == bot.agent_name
        assert mock_register.call_args.args[4] != "team"
        mock_add_buttons.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reaction_hooks_run_after_built_in_handlers_decline(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """reaction:received hooks should run only after built-in handlers decline the event."""
        seen: list[tuple[str, str, str | None]] = []

        @hook(EVENT_REACTION_RECEIVED)
        async def record_reaction(ctx: ReactionReceivedContext) -> None:
            seen.append((ctx.reaction_key, ctx.target_event_id, ctx.thread_id))

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        _install_runtime_cache_support(bot)
        bot.client.room_get_event = AsyncMock(
            side_effect=[
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Reply in thread",
                            "msgtype": "m.text",
                            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread-root"},
                        },
                        "event_id": "$question",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {"body": "Thread root", "msgtype": "m.text"},
                        "event_id": "$thread-root",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
            ],
        )
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [record_reaction])])
        room = MagicMock()
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        event = self._make_handler_event("reaction", sender="@user:localhost", event_id="$reaction")
        event.source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$question",
                    "key": "👍",
                },
            },
        }

        with (
            patch("mindroom.bot.interactive.handle_reaction", new=AsyncMock(return_value=False)),
        ):
            await bot._on_reaction(room, event)

        assert seen == [("👍", "$question", "$thread-root")]

    @pytest.mark.asyncio
    async def test_reaction_hooks_do_not_run_when_interactive_handler_claims_event(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """reaction:received hooks should not run when a built-in handler already consumes the reaction."""
        seen: list[str] = []

        @hook(EVENT_REACTION_RECEIVED)
        async def record_reaction(ctx: ReactionReceivedContext) -> None:
            seen.append(ctx.reaction_key)

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [record_reaction])])
        room = MagicMock()
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        event = self._make_handler_event("reaction", sender="@user:localhost", event_id="$reaction")

        with (
            patch(
                "mindroom.bot.interactive.handle_reaction",
                new=AsyncMock(
                    return_value=interactive.InteractiveSelection(
                        question_event_id="$question",
                        selection_key="1",
                        selected_value="Selected",
                        thread_id=None,
                    ),
                ),
            ),
            patch.object(bot._turn_controller, "handle_interactive_selection", new=AsyncMock()),
        ):
            await bot._on_reaction(room, event)

        assert seen == []

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
        bot.client.room_send.return_value = _room_send_response("$response")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

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

        mock_ai = AsyncMock(side_effect=fake_ai_response)
        with patch_response_runner_module(
            typing_indicator=noop_typing_indicator,
            ai_response=mock_ai,
        ):
            delivery = await bot._response_runner.process_and_respond(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Summarize README",
                    reply_to_event_id="$event",
                    thread_history=[],
                    user_id="@user:localhost",
                ),
            )

        assert delivery.event_id == "$response"
        assert mock_ai.call_args.kwargs["show_tool_calls"] is False
        assert "io.mindroom.tool_trace" not in bot.client.room_send.await_args.kwargs["content"]

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
        bot._knowledge_access_support.for_agent = MagicMock(return_value=None)
        bot._send_response = AsyncMock(return_value="$response")
        install_send_response_mock(bot, bot._send_response)
        mock_ai = AsyncMock(return_value="Skill response")
        with patch_response_runner_module(
            typing_indicator=noop_typing_indicator,
            ai_response=mock_ai,
            create_background_task=MagicMock(side_effect=discard_background_task),
        ):
            await bot._response_runner.send_skill_command_response(
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
        assert mock_ai.call_args.kwargs["prompt"].startswith("[")
        assert mock_ai.call_args.kwargs["prompt"].endswith("Use research skill")

    @pytest.mark.asyncio
    async def test_skill_command_room_mode_uses_room_level_session_id(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Room-mode skill dispatch should keep the canonical room-level session key."""

        @asynccontextmanager
        async def noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            yield

        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        thread_mode="room",
                    ),
                    "general": AgentConfig(
                        display_name="GeneralAgent",
                        rooms=["!test:localhost"],
                    ),
                },
                defaults=DefaultsConfig(show_tool_calls=False),
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot._knowledge_access_support.for_agent = MagicMock(return_value=None)
        bot._send_response = AsyncMock(return_value="$response")
        install_send_response_mock(bot, bot._send_response)
        mock_ai = AsyncMock(return_value="Skill response")
        mock_reprioritize = MagicMock()
        with patch_response_runner_module(
            typing_indicator=noop_typing_indicator,
            ai_response=mock_ai,
            reprioritize_auto_flush_sessions=mock_reprioritize,
            mark_auto_flush_dirty_session=MagicMock(),
            create_background_task=MagicMock(),
        ):
            await bot._response_runner.send_skill_command_response(
                room_id="!test:localhost",
                reply_to_event_id="$event",
                thread_id=None,
                thread_history=[],
                prompt="Use room mode skill",
                agent_name="calculator",
                user_id="@user:localhost",
                reply_to_event=None,
            )

        assert mock_ai.call_args.kwargs["session_id"] == "!test:localhost"
        assert mock_reprioritize.call_args.kwargs["active_session_id"] == "!test:localhost"

    @pytest.mark.asyncio
    async def test_generate_response_prefixes_user_turns_with_local_datetime(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Top-level response generation should prefix user turns with local date and time."""

        async def run_cancellable_response(*_args: object, **kwargs: object) -> str:
            response_kwargs = cast("dict[str, Callable[[str | None], Awaitable[None]]]", kwargs)
            response_function = response_kwargs["response_function"]
            await response_function(None)
            return "$response"

        scheduled_tasks: list[asyncio.Task[None]] = []

        async def fake_store_conversation_memory(*_args: object, **_kwargs: object) -> None:
            return None

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            return task

        config = self._config_for_storage(tmp_path)
        config.timezone = "America/Los_Angeles"
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()

        prior_user_time = datetime(2026, 3, 10, 8, 10, tzinfo=ZoneInfo("America/Los_Angeles"))
        prior_agent_time = datetime(2026, 3, 10, 8, 12, tzinfo=ZoneInfo("America/Los_Angeles"))
        thread_history = [
            _visible_message(
                sender="@alice:localhost",
                body="Earlier user question",
                timestamp=int(prior_user_time.timestamp() * 1000),
                event_id="$user1",
            ),
            _visible_message(
                sender=mock_agent_user.user_id,
                body="Existing agent reply",
                timestamp=int(prior_agent_time.timestamp() * 1000),
                event_id="$agent1",
            ),
        ]

        with (
            patch.object(
                ResponseRunner,
                "process_and_respond",
                new=AsyncMock(
                    return_value=DeliveryResult(
                        event_id="$response",
                        response_text="ok",
                        delivery_kind="sent",
                    ),
                ),
            ) as mock_process,
            patch.object(
                ResponseRunner,
                "run_cancellable_response",
                new=AsyncMock(side_effect=run_cancellable_response),
            ),
            patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
            patch("mindroom.response_runner.create_background_task", side_effect=schedule_background_task),
            patch(
                "mindroom.response_runner.store_conversation_memory",
                side_effect=fake_store_conversation_memory,
            ),
            patch("mindroom.response_runner.datetime") as mock_datetime,
            patch.object(
                bot._conversation_resolver,
                "fetch_thread_history",
                new=AsyncMock(return_value=thread_history),
            ),
        ):
            mock_datetime.now.return_value = datetime(2026, 3, 20, 8, 15, tzinfo=ZoneInfo("America/Los_Angeles"))
            mock_datetime.fromtimestamp.side_effect = lambda seconds, tz: datetime.fromtimestamp(seconds, tz)

            await bot._generate_response(
                room_id="!test:localhost",
                prompt="What time is it?",
                reply_to_event_id="$event",
                thread_id="$thread",
                thread_history=thread_history,
                user_id="@alice:localhost",
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        request = mock_process.await_args.args[0]
        assert request.prompt == "What time is it?"
        assert request.model_prompt == "[2026-03-20 08:15 PDT] What time is it?"
        assert request.thread_history[0].body == "[2026-03-10 08:10 PDT] Earlier user question"
        assert request.thread_history[1].body == "Existing agent reply"

    @pytest.mark.asyncio
    async def test_generate_response_keeps_memory_inputs_unprefixed(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Memory storage should receive the raw conversation, not the model-prefixed version."""

        async def run_cancellable_response(*_args: object, **kwargs: object) -> str:
            response_kwargs = cast("dict[str, Callable[[str | None], Awaitable[None]]]", kwargs)
            response_function = response_kwargs["response_function"]
            await response_function(None)
            return "$response"

        scheduled_tasks: list[asyncio.Task[None]] = []
        stored_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        async def fake_store_conversation_memory(*args: object, **kwargs: object) -> None:
            stored_calls.append((args, kwargs))

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            return task

        config = self._config_for_storage(tmp_path)
        config.memory.backend = "mem0"
        config.timezone = "America/Los_Angeles"
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)

        bob_time = datetime(2026, 3, 10, 8, 10, tzinfo=ZoneInfo("America/Los_Angeles"))
        alice_time = datetime(2026, 3, 10, 8, 12, tzinfo=ZoneInfo("America/Los_Angeles"))
        agent_time = datetime(2026, 3, 10, 8, 14, tzinfo=ZoneInfo("America/Los_Angeles"))
        thread_history = [
            _visible_message(
                sender="@bob:localhost",
                body="Bob question",
                timestamp=int(bob_time.timestamp() * 1000),
                event_id="$bob1",
            ),
            _visible_message(
                sender="@alice:localhost",
                body="Alice earlier",
                timestamp=int(alice_time.timestamp() * 1000),
                event_id="$alice1",
            ),
            _visible_message(
                sender=mock_agent_user.user_id,
                body="Existing agent reply",
                timestamp=int(agent_time.timestamp() * 1000),
                event_id="$agent1",
            ),
        ]

        with (
            patch.object(
                ResponseRunner,
                "process_and_respond",
                new=AsyncMock(
                    return_value=DeliveryResult(
                        event_id="$response",
                        response_text="ok",
                        delivery_kind="sent",
                    ),
                ),
            ) as mock_process,
            patch.object(
                ResponseRunner,
                "run_cancellable_response",
                new=AsyncMock(side_effect=run_cancellable_response),
            ),
            patch("mindroom.response_runner.datetime") as mock_datetime,
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=False),
                create_background_task=schedule_background_task,
                store_conversation_memory=fake_store_conversation_memory,
            ),
            patch.object(
                bot._conversation_resolver,
                "fetch_thread_history",
                new=AsyncMock(return_value=thread_history),
            ),
        ):
            mock_datetime.now.return_value = datetime(2026, 3, 20, 8, 15, tzinfo=ZoneInfo("America/Los_Angeles"))
            mock_datetime.fromtimestamp.side_effect = lambda seconds, tz: datetime.fromtimestamp(seconds, tz)

            await bot._generate_response(
                room_id="!test:localhost",
                prompt="What time is it?",
                reply_to_event_id="$event",
                thread_id="$thread",
                thread_history=thread_history,
                user_id="@alice:localhost",
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        request = mock_process.await_args.args[0]
        assert request.prompt == "What time is it?"
        assert request.model_prompt == "[2026-03-20 08:15 PDT] What time is it?"
        assert request.thread_history[0].body == "[2026-03-10 08:10 PDT] Bob question"
        assert request.thread_history[1].body == "[2026-03-10 08:12 PDT] Alice earlier"
        assert request.thread_history[2].body == "Existing agent reply"

        assert len(stored_calls) == 1
        store_args, _ = stored_calls[0]
        assert store_args[0] == "What time is it?"
        assert store_args[6] == thread_history
        assert store_args[7] == "@alice:localhost"

    @pytest.mark.asyncio
    async def test_generate_response_marks_fresh_thinking_message_as_adopted_placeholder(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming generation should flag fresh thinking placeholders for adoption."""

        async def run_cancellable_response(*_args: object, **kwargs: object) -> str:
            response_kwargs = cast("dict[str, Callable[[str | None], Awaitable[None]]]", kwargs)
            response_function = response_kwargs["response_function"]
            await response_function("$thinking")
            return "$thinking"

        scheduled_tasks: list[asyncio.Task[None]] = []

        async def fake_store_conversation_memory(*_args: object, **_kwargs: object) -> None:
            return None

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            return task

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)

        with (
            patch.object(
                ResponseRunner,
                "process_and_respond_streaming",
                new=AsyncMock(
                    return_value=DeliveryResult(
                        event_id="$thinking",
                        response_text="",
                        delivery_kind="edited",
                    ),
                ),
            ) as mock_process,
            patch.object(
                ResponseRunner,
                "run_cancellable_response",
                new=AsyncMock(side_effect=run_cancellable_response),
            ),
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=True),
                create_background_task=schedule_background_task,
                store_conversation_memory=fake_store_conversation_memory,
            ),
        ):
            await bot._generate_response(
                room_id="!test:localhost",
                prompt="Continue",
                reply_to_event_id="$event",
                thread_id=None,
                thread_history=[],
                user_id="@alice:localhost",
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        request = mock_process.await_args.args[0]
        assert request.existing_event_id == "$thinking"
        assert request.existing_event_is_placeholder is True

    @pytest.mark.asyncio
    async def test_generate_response_refreshes_thread_history_after_lock(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Queued turns should replace stale pending history with a fresh post-lock snapshot."""

        async def run_cancellable_response(*_args: object, **kwargs: object) -> str:
            response_function = cast("Callable[[str | None], Awaitable[None]]", kwargs["response_function"])
            await response_function(None)
            return "$response"

        def passthrough_prepare_context(
            prompt: str,
            thread_history: Sequence[ResolvedVisibleMessage],
            *,
            config: Config,
            runtime_paths: RuntimePaths,
            model_prompt: str | None = None,
        ) -> tuple[str, Sequence[ResolvedVisibleMessage], str, list[ResolvedVisibleMessage]]:
            _ = config, runtime_paths
            return prompt, thread_history, model_prompt or prompt, list(thread_history)

        stale_history = [
            _visible_message(
                sender=mock_agent_user.user_id,
                body="Thinking...",
                event_id="$stale",
                timestamp=1,
                content={"body": "Thinking...", STREAM_STATUS_KEY: STREAM_STATUS_PENDING},
            ),
        ]
        fresh_history = [
            _visible_message(
                sender=mock_agent_user.user_id,
                body="Completed",
                event_id="$stale",
                timestamp=1,
                content={"body": "Completed", STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED},
            ),
        ]

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)

        async def cached_history_refresh(_room_id: str, _thread_id: str) -> list[ResolvedVisibleMessage]:
            return fresh_history

        with (
            patch.object(
                ResponseRunner,
                "process_and_respond",
                new=AsyncMock(
                    return_value=DeliveryResult(
                        event_id="$response",
                        response_text="ok",
                        delivery_kind="sent",
                    ),
                ),
            ) as mock_process,
            patch.object(
                ResponseRunner,
                "run_cancellable_response",
                new=AsyncMock(side_effect=run_cancellable_response),
            ),
            patch.object(
                bot._conversation_cache,
                "get_thread_history",
                new=AsyncMock(side_effect=cached_history_refresh),
            ) as mock_get_thread_history,
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=False),
                prepare_memory_and_model_context=passthrough_prepare_context,
                reprioritize_auto_flush_sessions=MagicMock(),
                apply_post_response_effects=AsyncMock(),
            ),
        ):
            async with bot._conversation_resolver.turn_thread_cache_scope():
                event_id = await bot._generate_response(
                    room_id="!test:localhost",
                    prompt="Continue",
                    reply_to_event_id="$event",
                    thread_id="$thread",
                    thread_history=stale_history,
                    user_id="@alice:localhost",
                )

        assert event_id == "$response"
        mock_get_thread_history.assert_awaited_once_with("!test:localhost", "$thread")
        request = mock_process.await_args.args[0]
        assert list(request.thread_history) == fresh_history
        assert request.thread_history[0].stream_status == STREAM_STATUS_COMPLETED

    @pytest.mark.asyncio
    async def test_generate_response_uses_resolved_thread_root_for_thinking_placeholder(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Thinking placeholders should use the canonical thread root from the response envelope."""
        scheduled_tasks: list[asyncio.Task[None]] = []
        sent_contents: list[dict[str, object]] = []

        async def fake_store_conversation_memory(*_args: object, **_kwargs: object) -> None:
            return None

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            return task

        async def record_send(_client: object, _room_id: str, content: dict[str, object]) -> str:
            sent_contents.append(content)
            return "$thinking"

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        envelope = MessageEnvelope(
            source_event_id="$reply_plain:localhost",
            room_id="!test:localhost",
            target=MessageTarget.resolve(
                room_id="!test:localhost",
                thread_id=None,
                reply_to_event_id="$reply_plain:localhost",
                safe_thread_root="$thread_root:localhost",
            ),
            requester_id="@alice:localhost",
            sender_id="@alice:localhost",
            body="Continue",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=mock_agent_user.agent_name,
            source_kind="message",
        )

        with (
            patch.object(
                ResponseRunner,
                "process_and_respond",
                new=AsyncMock(
                    return_value=DeliveryResult(
                        event_id="$thinking",
                        response_text="ok",
                        delivery_kind="edited",
                    ),
                ),
            ),
            patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
            patch("mindroom.response_runner.create_background_task", side_effect=schedule_background_task),
            patch(
                "mindroom.response_runner.store_conversation_memory",
                side_effect=fake_store_conversation_memory,
            ),
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_latest_thread_event_id_if_needed",
                new=AsyncMock(return_value="$latest:localhost"),
            ),
            patch("mindroom.delivery_gateway.send_message", new=AsyncMock(side_effect=record_send)),
        ):
            await bot._generate_response(
                room_id="!test:localhost",
                prompt="Continue",
                reply_to_event_id="$reply_plain:localhost",
                thread_id=None,
                thread_history=[],
                user_id="@alice:localhost",
                response_envelope=envelope,
                correlation_id="$request:localhost",
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        assert len(sent_contents) == 1
        content = sent_contents[0]
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$thread_root:localhost"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$reply_plain:localhost"

    @pytest.mark.asyncio
    async def test_generate_response_queues_thread_summary_for_threaded_reply(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Threaded agent replies should queue summary generation once the threshold is reached."""

        async def fake_store_conversation_memory(*_args: object, **_kwargs: object) -> None:
            return None

        scheduled_tasks: list[asyncio.Task[None]] = []
        scheduled_names: list[str] = []

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            scheduled_names.append(name)
            return task

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        _install_runtime_cache_support(bot)
        bot._knowledge_access_support.for_agent = MagicMock(return_value=None)
        thread_history = [
            _visible_message(
                sender=f"@user{i}:localhost",
                body=f"Message {i}",
                event_id=f"$message{i}",
                timestamp=i,
            )
            for i in range(4)
        ]

        with (
            patch("mindroom.response_runner.typing_indicator", _noop_typing_indicator),
            patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
            patch("mindroom.response_runner.ai_response", new_callable=AsyncMock, return_value="ok"),
            patch("mindroom.delivery_gateway.send_message", new=AsyncMock(return_value="$response")),
            patch("mindroom.delivery_gateway.edit_message", new=AsyncMock(return_value=_room_send_response("$edit"))),
            patch.object(
                bot._conversation_cache,
                "get_thread_history",
                new=AsyncMock(return_value=thread_history),
            ) as mock_get_thread_history,
            patch("mindroom.response_runner.create_background_task", side_effect=schedule_background_task),
            patch("mindroom.post_response_effects.create_background_task", side_effect=schedule_background_task),
            patch("mindroom.bot.store_conversation_memory", side_effect=fake_store_conversation_memory),
            patch(
                "mindroom.response_runner.store_conversation_memory",
                side_effect=fake_store_conversation_memory,
            ),
            patch(
                "mindroom.post_response_effects.maybe_generate_thread_summary",
                new_callable=AsyncMock,
            ) as mock_thread_summary,
        ):
            await bot._generate_response(
                room_id="!test:localhost",
                prompt="Summarize this thread",
                reply_to_event_id="$event",
                thread_id="$thread",
                thread_history=thread_history,
                user_id="@alice:localhost",
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        mock_get_thread_history.assert_awaited_once_with("!test:localhost", "$thread")
        mock_thread_summary.assert_awaited_once_with(
            client=bot.client,
            room_id="!test:localhost",
            thread_id="$thread",
            config=config,
            runtime_paths=bot.runtime_paths,
            conversation_cache=bot._conversation_cache,
            message_count_hint=5,
        )
        assert "thread_summary_!test:localhost_$thread" in scheduled_names

    @pytest.mark.asyncio
    async def test_generate_response_runs_post_effects_after_cancellable_wrapper(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Late cancellation should not skip agent post-response cleanup after delivery."""

        async def run_cancellable_response(*_args: object, **kwargs: object) -> str:
            response_function = cast("Callable[[str | None], Awaitable[None]]", kwargs["response_function"])
            await response_function(None)
            return "$response"

        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_post_effects(*_args: object, **_kwargs: object) -> None:
            started.set()
            await release.wait()

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)

        with (
            patch.object(
                ResponseRunner,
                "process_and_respond",
                new=AsyncMock(
                    return_value=DeliveryResult(
                        event_id="$response",
                        response_text="ok",
                        delivery_kind="sent",
                    ),
                ),
            ),
            patch.object(
                ResponseRunner,
                "run_cancellable_response",
                new=AsyncMock(side_effect=run_cancellable_response),
            ),
            patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.response_lifecycle.apply_post_response_effects",
                new=AsyncMock(side_effect=fake_post_effects),
            ),
        ):
            task = asyncio.create_task(
                bot._generate_response(
                    room_id="!test:localhost",
                    prompt="Summarize this thread",
                    reply_to_event_id="$event",
                    thread_id="$thread",
                    thread_history=[],
                    user_id="@alice:localhost",
                ),
            )
            await started.wait()
            task.cancel()
            release.set()
            event_id = await task

        assert event_id == "$response"

    @pytest.mark.asyncio
    async def test_generate_team_response_queues_memory_before_helper_failure(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Team memory should be queued before the shared helper runs."""

        async def fake_store_conversation_memory(*args: object, **kwargs: object) -> None:
            store_calls.append((args, kwargs))

        scheduled_tasks: list[asyncio.Task[None]] = []
        scheduled_names: list[str] = []
        store_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            scheduled_names.append(name)
            return task

        async def fail_helper(*_args: object, **_kwargs: object) -> str:
            assert any(name.startswith("memory_save_team_") for name in scheduled_names)
            msg = "boom"
            raise RuntimeError(msg)

        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        team_member = config.get_ids(runtime_paths)["general"]
        bot = TeamBot(
            mock_agent_user,
            tmp_path,
            config=config,
            runtime_paths=runtime_paths,
            team_agents=[team_member],
            team_mode="coordinate",
        )
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)

        resolution = TeamResolution(
            intent=TeamIntent.EXPLICIT_MEMBERS,
            requested_members=[team_member],
            member_statuses=[
                TeamResolutionMember(
                    agent=team_member,
                    name="general",
                    status=TeamMemberStatus.ELIGIBLE,
                ),
            ],
            eligible_members=[team_member],
            outcome=TeamOutcome.TEAM,
            mode=TeamMode.COORDINATE,
        )

        with (
            patch.object(bot._turn_policy, "materializable_agent_names", return_value={"general"}),
            patch("mindroom.bot.resolve_configured_team", return_value=resolution),
            patch.object(bot, "_generate_team_response_helper", new=AsyncMock(side_effect=fail_helper)),
            patch("mindroom.bot.create_background_task", side_effect=schedule_background_task),
            patch("mindroom.bot.store_conversation_memory", side_effect=fake_store_conversation_memory),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await bot._generate_response(
                room_id="!test:localhost",
                prompt="Team, summarize this thread",
                reply_to_event_id="$event",
                thread_id="$thread",
                thread_history=[],
                user_id="@alice:localhost",
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        assert len(store_calls) == 1
        assert any(name.startswith("memory_save_team_") for name in scheduled_names)

    @pytest.mark.asyncio
    async def test_team_generate_response_uses_shared_thread_summary_helper_for_summary_gate(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Team replies should reuse the shared thread-summary helper for summary gating."""

        async def fake_store_conversation_memory(*_args: object, **_kwargs: object) -> None:
            return None

        scheduled_tasks: list[asyncio.Task[None]] = []

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            return task

        thread_history = [
            _visible_message(
                sender=f"@user{i}:localhost",
                body=f"Message {i}",
                event_id=f"$message{i}",
                timestamp=i,
            )
            for i in range(4)
        ]

        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        team_member = config.get_ids(runtime_paths)["general"]
        bot = TeamBot(
            mock_agent_user,
            tmp_path,
            config=config,
            runtime_paths=runtime_paths,
            team_agents=[team_member],
            team_mode="coordinate",
        )
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths,
        )

        resolution = TeamResolution(
            intent=TeamIntent.EXPLICIT_MEMBERS,
            requested_members=[team_member],
            member_statuses=[
                TeamResolutionMember(
                    agent=team_member,
                    name="general",
                    status=TeamMemberStatus.ELIGIBLE,
                ),
            ],
            eligible_members=[team_member],
            outcome=TeamOutcome.TEAM,
            mode=TeamMode.COORDINATE,
        )

        with (
            patch.object(bot._turn_policy, "materializable_agent_names", return_value={"general"}),
            patch("mindroom.bot.resolve_configured_team", return_value=resolution),
            patch.object(
                bot,
                "_generate_team_response_helper",
                new=AsyncMock(return_value="$response"),
            ),
            patch(
                "mindroom.post_response_effects.PostResponseEffectsSupport.queue_thread_summary",
            ) as mock_queue_thread_summary,
            patch("mindroom.bot.create_background_task", side_effect=schedule_background_task),
            patch("mindroom.bot.store_conversation_memory", side_effect=fake_store_conversation_memory),
        ):
            await bot._generate_response(
                room_id="!test:localhost",
                prompt="Team, summarize this thread",
                reply_to_event_id="$event",
                thread_id="$thread",
                thread_history=thread_history,
                user_id="@alice:localhost",
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        mock_queue_thread_summary.assert_not_called()

    @pytest.mark.asyncio
    async def test_team_generate_response_redacts_suppressed_streaming_reply(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """TeamBot should redact hook-suppressed streamed replies after the first send."""

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def suppressing_hook(ctx: BeforeResponseContext) -> None:
            ctx.draft.suppress = True

        async def fake_store_conversation_memory(*_args: object, **_kwargs: object) -> None:
            return None

        async def fake_team_response_stream(*_args: object, **_kwargs: object) -> AsyncGenerator[str, None]:
            yield "Team reply"

        scheduled_tasks: list[asyncio.Task[None]] = []
        scheduled_names: list[str] = []

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            scheduled_names.append(name)
            return task

        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        team_member = config.get_ids(runtime_paths)["general"]
        bot = TeamBot(
            mock_agent_user,
            tmp_path,
            config=config,
            runtime_paths=runtime_paths,
            team_agents=[team_member],
            team_mode="coordinate",
        )
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [suppressing_hook])])
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths,
        )
        resolution = TeamResolution(
            intent=TeamIntent.EXPLICIT_MEMBERS,
            requested_members=[team_member],
            member_statuses=[
                TeamResolutionMember(
                    agent=team_member,
                    name="general",
                    status=TeamMemberStatus.ELIGIBLE,
                ),
            ],
            eligible_members=[team_member],
            outcome=TeamOutcome.TEAM,
            mode=TeamMode.COORDINATE,
        )

        with (
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=True),
                typing_indicator=_noop_typing_indicator,
                team_response_stream=lambda *_args, **_kwargs: fake_team_response_stream(),
            ),
            patch.object(bot._turn_policy, "materializable_agent_names", return_value={"general"}),
            patch("mindroom.bot.resolve_configured_team", return_value=resolution),
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new=AsyncMock(return_value=("$team-response", "Team reply")),
            ),
            patch("mindroom.response_runner.create_background_task", side_effect=schedule_background_task),
            patch("mindroom.post_response_effects.create_background_task", side_effect=schedule_background_task),
            patch("mindroom.bot.store_conversation_memory", side_effect=fake_store_conversation_memory),
            patch(
                "mindroom.post_response_effects.maybe_generate_thread_summary",
                new_callable=AsyncMock,
            ) as mock_thread_summary,
        ):
            event_id = await bot._generate_response(
                room_id="!test:localhost",
                prompt="Team, summarize this thread",
                reply_to_event_id="$event",
                thread_id="$thread",
                thread_history=[],
                user_id="@alice:localhost",
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        assert event_id is None
        mock_thread_summary.assert_not_awaited()
        assert "thread_summary_!test:localhost_$thread" not in scheduled_names

    def test_thread_summary_message_count_hint_excludes_existing_summaries(self) -> None:
        """Thread-summary hints should count the post-response non-summary total."""
        thread_history = [
            ResolvedVisibleMessage.synthetic(
                sender=f"@user{i}:localhost",
                body=f"Message {i}",
                timestamp=1700000000 + i,
                event_id=f"$message{i}",
            )
            for i in range(4)
        ]
        thread_history.append(
            ResolvedVisibleMessage.synthetic(
                sender="@mindroom_general:localhost",
                body="🧵 Existing summary",
                timestamp=1700000005,
                event_id="$summary",
                content={
                    "msgtype": "m.notice",
                    "body": "🧵 Existing summary",
                    "io.mindroom.thread_summary": {
                        "version": 1,
                        "summary": "🧵 Existing summary",
                        "message_count": 4,
                        "model": "default",
                    },
                },
                thread_id="$thread",
            ),
        )

        assert thread_summary_message_count_hint(thread_history) == 5

    @pytest.mark.asyncio
    async def test_generate_team_response_streams_into_placeholder_event(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Team streaming should stay enabled when reusing the startup placeholder."""

        @asynccontextmanager
        async def noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            yield

        async def run_cancellable_response(*_args: object, **kwargs: object) -> str:
            response_kwargs = cast("dict[str, Callable[[str | None], Awaitable[None]]]", kwargs)
            response_function = response_kwargs["response_function"]
            await response_function("$placeholder")
            return "$placeholder"

        async def fake_team_response_stream(*_args: object, **_kwargs: object) -> AsyncGenerator[str, None]:
            yield "stream chunk"

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        bot.orchestrator = MagicMock()
        mock_team_response = AsyncMock()
        with (
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=True),
                typing_indicator=noop_typing_indicator,
                team_response_stream=fake_team_response_stream,
                team_response=mock_team_response,
            ),
            patch.object(
                ResponseRunner,
                "run_cancellable_response",
                new=AsyncMock(side_effect=run_cancellable_response),
            ),
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new=AsyncMock(return_value=("$placeholder", "stream chunk")),
            ) as mock_send_streaming_response,
        ):
            event_id = await bot._generate_team_response_helper(
                room_id="!test:localhost",
                reply_to_event_id="$event",
                thread_id="$thread_root",
                payload=DispatchPayload(prompt="Continue"),
                team_agents=[bot.matrix_id],
                team_mode="coordinate",
                thread_history=[],
                requester_user_id="@alice:localhost",
                existing_event_id="$placeholder",
                existing_event_is_placeholder=True,
                response_envelope=_hook_envelope(body="Continue", source_event_id="$event"),
                correlation_id="corr-team-stream",
            )

        assert event_id == "$placeholder"
        mock_team_response.assert_not_awaited()
        send_kwargs = mock_send_streaming_response.await_args.kwargs
        assert send_kwargs["existing_event_id"] == "$placeholder"
        assert send_kwargs["adopt_existing_placeholder"] is True

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_redacts_suppressed_streamed_placeholder(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming team suppression should not preserve the provisional placeholder id."""

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            ctx.draft.suppress = True

        @asynccontextmanager
        async def noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            yield

        async def run_cancellable_response(*_args: object, **kwargs: object) -> str:
            response_kwargs = cast("dict[str, Callable[[str | None], Awaitable[None]]]", kwargs)
            response_function = response_kwargs["response_function"]
            await response_function("$placeholder")
            return "$placeholder"

        async def fake_team_response_stream(*_args: object, **_kwargs: object) -> AsyncGenerator[str, None]:
            yield "stream chunk"

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        _install_runtime_cache_support(bot)
        bot.orchestrator = MagicMock()
        bot._redact_message_event = AsyncMock(return_value=True)
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [before_hook])])
        replace_delivery_gateway_deps(bot, redact_message_event=bot._redact_message_event)

        with (
            patch.object(
                unwrap_extracted_collaborator(bot._response_runner),
                "run_cancellable_response",
                new=AsyncMock(side_effect=run_cancellable_response),
            ),
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new=AsyncMock(return_value=("$placeholder", "stream chunk")),
            ),
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=True),
                typing_indicator=noop_typing_indicator,
                team_response_stream=fake_team_response_stream,
            ),
        ):
            event_id = await bot._generate_team_response_helper(
                room_id="!test:localhost",
                reply_to_event_id="$event",
                thread_id="$thread_root",
                payload=DispatchPayload(prompt="Continue"),
                team_agents=[bot.matrix_id],
                team_mode="coordinate",
                thread_history=[],
                requester_user_id="@alice:localhost",
                existing_event_id="$placeholder",
                existing_event_is_placeholder=True,
                response_envelope=_hook_envelope(body="Continue", source_event_id="$event"),
                correlation_id="corr-team-stream-suppress",
            )

        assert event_id is None
        bot._redact_message_event.assert_awaited_once_with(
            room_id="!test:localhost",
            event_id="$placeholder",
            reason="Suppressed streamed response",
        )

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_returns_none_when_suppressed_placeholder_is_redacted(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Suppressed team placeholder responses should not leak the redacted placeholder id."""

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            ctx.draft.suppress = True

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        _install_runtime_cache_support(bot)
        bot.orchestrator = MagicMock()
        bot._redact_message_event = AsyncMock(return_value=True)
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [before_hook])])
        replace_delivery_gateway_deps(bot, redact_message_event=bot._redact_message_event)

        with (
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=False),
                typing_indicator=_noop_typing_indicator,
                team_response=AsyncMock(return_value="Team handled"),
            ),
        ):
            event_id = await bot._generate_team_response_helper(
                room_id="!test:localhost",
                reply_to_event_id="$event",
                thread_id="$thread_root",
                payload=DispatchPayload(prompt="Continue"),
                team_agents=[bot.matrix_id],
                team_mode="coordinate",
                thread_history=[],
                requester_user_id="@alice:localhost",
                existing_event_id="$placeholder",
                existing_event_is_placeholder=True,
                response_envelope=_hook_envelope(body="Continue", source_event_id="$event"),
                correlation_id="corr-team-suppress",
            )

        assert event_id is None
        bot._redact_message_event.assert_awaited_once_with(
            room_id="!test:localhost",
            event_id="$placeholder",
            reason="Suppressed placeholder response",
        )

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
        """AgentBot construction should not build standalone cache support before startup."""
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

        target = MessageTarget.resolve(room_id="!test:localhost", thread_id=None, reply_to_event_id="$root_event")
        context = bot._tool_runtime_support.build_context(
            target,
            user_id="@user:localhost",
            attachment_ids=["att_1"],
        )

        assert context is not None
        assert context.thread_id is None
        assert context.resolved_thread_id == "$root_event"
        assert context.attachment_ids == ("att_1",)

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
        )
        second = MessageTarget.resolve(
            room_id="!test:localhost",
            thread_id=None,
            reply_to_event_id="$root_b",
        )

        coordinator = unwrap_extracted_collaborator(bot._response_runner)
        assert coordinator._response_lifecycle_lock(first) is coordinator._response_lifecycle_lock(first)
        assert coordinator._response_lifecycle_lock(first) is not coordinator._response_lifecycle_lock(second)

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
            patch("mindroom.turn_controller.is_authorized_sender", return_value=False),
        ):
            await self._invoke_handler(bot, handler_name, room, event)

        if marks_responded:
            tracker.record_handled_turn.assert_called_once_with(
                HandledTurnState.from_source_event_id(event.event_id),
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
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
            patch.object(bot._turn_policy, "can_reply_to_sender", return_value=False),
            patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
        ):
            await self._invoke_handler(bot, handler_name, room, event)

        if marks_responded:
            tracker.record_handled_turn.assert_called_once_with(
                HandledTurnState.from_source_event_id(event.event_id),
            )
        else:
            tracker.record_handled_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_bot_on_image_message_forwards_image_to_generate_response(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Image messages should call _generate_response with images payload."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()

        tracker = MagicMock()
        tracker.has_responded.return_value = False
        _set_turn_store_tracker(bot, tracker)

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
        bot._generate_response = AsyncMock(return_value="$response")
        install_generate_response_mock(bot, bot._generate_response)

        room = MagicMock()
        room.room_id = "!test:localhost"

        event = MagicMock(spec=nio.RoomMessageImage)
        event.sender = "@user:localhost"
        event.event_id = "$img_event"
        event.body = "photo.jpg"
        event.server_timestamp = 1000
        event.source = {"content": {"body": "photo.jpg"}}  # no filename → body is filename

        image = MagicMock()
        image.content = b"image-bytes"
        image.mime_type = "image/jpeg"
        attachment_id = _attachment_id_for_event("$img_event")
        attachment_record = MagicMock()
        attachment_record.attachment_id = attachment_id

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
            patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new_callable=AsyncMock,
                return_value=TeamResolution.none(),
            ),
            patch("mindroom.turn_policy.should_agent_respond", return_value=True),
            patch("mindroom.inbound_turn_normalizer.download_image", new_callable=AsyncMock, return_value=image),
            patch(
                "mindroom.inbound_turn_normalizer.register_image_attachment",
                new_callable=AsyncMock,
                return_value=attachment_record,
            ),
            patch(
                "mindroom.inbound_turn_normalizer.resolve_attachment_media",
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
        tracker.record_handled_turn.assert_called_once_with(
            _agent_response_handled_turn(
                agent_name=mock_agent_user.agent_name,
                room_id=room.room_id,
                event_id="$img_event",
                response_event_id="$response",
                source_event_prompts={"$img_event": "[Attached image]"},
            ),
        )

    @pytest.mark.asyncio
    async def test_media_message_merges_thread_history_attachment_ids(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Media turns should include attachment IDs already referenced in thread history."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()

        tracker = MagicMock()
        tracker.has_responded.return_value = False
        _set_turn_store_tracker(bot, tracker)

        history_attachment_id = "att_prev_image"
        current_attachment_id = _attachment_id_for_event("$img_event_history")

        bot._conversation_resolver.extract_dispatch_context = AsyncMock(
            return_value=MessageContext(
                am_i_mentioned=False,
                is_thread=True,
                thread_id="$thread_root",
                thread_history=[
                    _visible_message(
                        sender="@user:localhost",
                        event_id="$routed_prev",
                        content={ATTACHMENT_IDS_KEY: [history_attachment_id]},
                    ),
                ],
                mentioned_agents=[],
                has_non_agent_mentions=False,
            ),
        )
        bot._generate_response = AsyncMock(return_value="$response")
        install_generate_response_mock(bot, bot._generate_response)
        _replace_turn_policy_deps(bot, resolver=bot._conversation_resolver)
        _set_turn_store_tracker(bot, tracker)

        room = MagicMock()
        room.room_id = "!test:localhost"

        event = MagicMock(spec=nio.RoomMessageImage)
        event.sender = "@user:localhost"
        event.event_id = "$img_event_history"
        event.body = "photo.png"
        event.server_timestamp = 1000
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
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
            patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new_callable=AsyncMock,
                return_value=TeamResolution.none(),
            ),
            patch("mindroom.turn_policy.should_agent_respond", return_value=True),
            patch("mindroom.inbound_turn_normalizer.download_image", new_callable=AsyncMock, return_value=image),
            patch(
                "mindroom.inbound_turn_normalizer.register_image_attachment",
                new_callable=AsyncMock,
                return_value=attachment_record,
            ),
            patch(
                "mindroom.inbound_turn_normalizer.resolve_thread_attachment_ids",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "mindroom.inbound_turn_normalizer.resolve_attachment_media",
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
        tracker.record_handled_turn.assert_called_once_with(
            _agent_response_handled_turn(
                agent_name=mock_agent_user.agent_name,
                room_id=room.room_id,
                event_id="$img_event_history",
                response_event_id="$response",
                thread_id="$thread_root",
                source_event_prompts={"$img_event_history": "[Attached image]"},
            ),
        )

    @pytest.mark.asyncio
    async def test_build_dispatch_payload_merges_fallback_images_with_registered_attachments(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Fallback image bytes should be appended instead of discarded when some registrations succeed."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        stored_image = MagicMock(spec=Image)
        fallback_image = MagicMock(spec=Image)

        with (
            patch(
                "mindroom.inbound_turn_normalizer.resolve_thread_attachment_ids",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "mindroom.inbound_turn_normalizer.resolve_attachment_media",
                return_value=(["att_image"], [], [stored_image], [], []),
            ),
        ):
            payload = await bot._inbound_turn_normalizer.build_dispatch_payload_with_attachments(
                DispatchPayloadWithAttachmentsRequest(
                    room_id="!test:localhost",
                    prompt="describe this",
                    current_attachment_ids=["att_image"],
                    thread_id=None,
                    media_thread_id=None,
                    thread_history=[],
                    fallback_images=[fallback_image],
                ),
            )

        assert payload.attachment_ids == ["att_image"]
        assert list(payload.media.images) == [stored_image, fallback_image]

    @pytest.mark.asyncio
    async def test_agent_bot_on_image_message_leaves_event_retryable_when_terminal_error_cannot_be_sent(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Image download failure should not mark the event responded without a visible terminal error."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()

        tracker = MagicMock()
        tracker.has_responded.return_value = False
        _set_turn_store_tracker(bot, tracker)

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
        bot._generate_response = AsyncMock()
        install_generate_response_mock(bot, bot._generate_response)

        room = MagicMock()
        room.room_id = "!test:localhost"

        event = MagicMock(spec=nio.RoomMessageImage)
        event.sender = "@user:localhost"
        event.event_id = "$img_event_fail"
        event.body = "please analyze"
        event.server_timestamp = 1000
        event.source = {"content": {"body": "please analyze"}}

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
            patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new_callable=AsyncMock,
                return_value=TeamResolution.none(),
            ),
            patch("mindroom.turn_policy.should_agent_respond", return_value=True),
            patch("mindroom.inbound_turn_normalizer.download_image", new_callable=AsyncMock, return_value=None),
        ):
            await bot._on_media_message(room, event)

        bot._generate_response.assert_not_called()
        tracker.record_handled_turn.assert_not_called()

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
        _set_turn_store_tracker(bot, tracker)

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
        bot._generate_response = AsyncMock(return_value="$response")
        install_generate_response_mock(bot, bot._generate_response)

        room = MagicMock()
        room.room_id = "!test:localhost"

        event = MagicMock(spec=nio.RoomMessageFile)
        event.sender = "@user:localhost"
        event.event_id = "$file_event"
        event.body = "report.pdf"
        event.url = "mxc://localhost/report"
        event.server_timestamp = 1000
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
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
            patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new_callable=AsyncMock,
                return_value=TeamResolution.none(),
            ),
            patch("mindroom.turn_policy.should_agent_respond", return_value=True),
            patch(
                "mindroom.inbound_turn_normalizer.register_file_or_video_attachment",
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
        tracker.record_handled_turn.assert_called_once_with(
            _agent_response_handled_turn(
                agent_name=mock_agent_user.agent_name,
                room_id=room.room_id,
                event_id="$file_event",
                response_event_id="$response",
                source_event_prompts={"$file_event": "[Attached file]"},
            ),
        )

    @pytest.mark.asyncio
    async def test_agent_bot_on_file_message_leaves_event_retryable_when_terminal_error_cannot_be_sent(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """File persistence failure should not mark the event responded without a visible terminal error."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()

        tracker = MagicMock()
        tracker.has_responded.return_value = False
        _set_turn_store_tracker(bot, tracker)

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
        bot._generate_response = AsyncMock()
        install_generate_response_mock(bot, bot._generate_response)

        room = MagicMock()
        room.room_id = "!test:localhost"

        event = MagicMock(spec=nio.RoomMessageFile)
        event.sender = "@user:localhost"
        event.event_id = "$file_event_fail"
        event.body = "report.pdf"
        event.url = "mxc://localhost/report"
        event.server_timestamp = 1000
        event.source = {"content": {"body": "report.pdf", "msgtype": "m.file"}}

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
            patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new_callable=AsyncMock,
                return_value=TeamResolution.none(),
            ),
            patch("mindroom.turn_policy.should_agent_respond", return_value=True),
            patch(
                "mindroom.inbound_turn_normalizer.register_file_or_video_attachment",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await bot._on_media_message(room, event)

        bot._generate_response.assert_not_called()
        tracker.record_handled_turn.assert_not_called()

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
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        tracker.has_responded.return_value = False
        bot._turn_controller._execute_router_relay = AsyncMock()

        mock_context = MagicMock()
        mock_context.am_i_mentioned = False
        mock_context.mentioned_agents = []
        mock_context.has_non_agent_mentions = False
        mock_context.is_thread = False
        mock_context.thread_id = None
        mock_context.thread_history = []
        bot._conversation_resolver.extract_message_context = AsyncMock(return_value=mock_context)

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
            patch("mindroom.turn_policy.get_agents_in_thread", return_value=[]),
            patch("mindroom.turn_policy.has_multiple_non_agent_users_in_thread", return_value=False),
            patch("mindroom.turn_policy.get_available_agents_for_sender") as mock_get_available,
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
            patch("mindroom.coalescing.extract_media_caption", return_value="[Attached image]"),
        ):
            mock_get_available.return_value = [
                config.get_ids(runtime_paths_for(config))["general"],
                config.get_ids(runtime_paths_for(config))["calculator"],
            ]
            await bot._on_media_message(room, event)

        bot._turn_controller._execute_router_relay.assert_called_once_with(
            room,
            event,
            [],
            None,
            message="[Attached image]",
            requester_user_id="@user:localhost",
            extra_content={"com.mindroom.original_sender": "@user:localhost"},
        )

    @pytest.mark.asyncio
    async def test_router_welcome_waits_for_joined_room_cache_before_send(
        self,
        tmp_path: Path,
    ) -> None:
        """Welcome sends should wait until the joined room is cached locally."""
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
        bot.client.rooms = {}
        bot.client.room_messages = AsyncMock(
            return_value=nio.RoomMessagesResponse(
                room_id="!welcome:localhost",
                chunk=[],
                start="",
                end=None,
            ),
        )

        async def fake_send_response(*, room_id: str, **_: object) -> str:
            assert room_id in bot.client.rooms
            return "$welcome"

        async def populate_room_cache(_delay: float) -> None:
            bot.client.rooms["!welcome:localhost"] = MagicMock()

        bot._send_response = AsyncMock(side_effect=fake_send_response)
        with (
            patch("mindroom.bot._generate_welcome_message", return_value="Welcome"),
            patch("mindroom.bot.asyncio.sleep", new=AsyncMock(side_effect=populate_room_cache)) as mock_sleep,
        ):
            await bot._send_welcome_message_if_empty("!welcome:localhost")

        mock_sleep.assert_awaited()
        bot._send_response.assert_awaited_once()

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
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        tracker.has_responded.return_value = False
        bot._turn_controller._execute_router_relay = AsyncMock()

        mock_context = MagicMock()
        mock_context.am_i_mentioned = False
        mock_context.mentioned_agents = []
        mock_context.has_non_agent_mentions = False
        mock_context.is_thread = False
        mock_context.thread_id = None
        mock_context.thread_history = []
        bot._conversation_resolver.extract_message_context = AsyncMock(return_value=mock_context)

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
            patch("mindroom.turn_policy.get_agents_in_thread", return_value=[]),
            patch("mindroom.turn_policy.has_multiple_non_agent_users_in_thread", return_value=False),
            patch("mindroom.turn_policy.get_available_agents_for_sender") as mock_get_available,
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
            patch(
                "mindroom.inbound_turn_normalizer.register_file_or_video_attachment",
                new_callable=AsyncMock,
                return_value=attachment_record,
            ) as mock_register_file,
        ):
            mock_get_available.return_value = [
                config.get_ids(runtime_paths_for(config))["general"],
                config.get_ids(runtime_paths_for(config))["calculator"],
            ]
            await bot._on_media_message(room, event)

        bot._turn_controller._execute_router_relay.assert_called_once()
        mock_register_file.assert_not_awaited()
        call_kwargs = bot._turn_controller._execute_router_relay.call_args.kwargs
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
        _set_turn_store_tracker(bot, MagicMock())
        bot._send_response = AsyncMock(return_value="$route")
        install_send_response_mock(bot, bot._send_response)

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
                "mindroom.turn_controller.filter_agents_by_sender_permissions",
                return_value=[config.get_ids(runtime_paths_for(config))["general"]],
            ),
            patch(
                "mindroom.turn_controller.suggest_agent_for_message",
                new_callable=AsyncMock,
                return_value="general",
            ),
            patch(
                "mindroom.inbound_turn_normalizer.register_file_or_video_attachment",
                new_callable=AsyncMock,
                return_value=attachment_record,
            ) as mock_register_file,
        ):
            await bot._turn_controller._execute_router_relay(
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
        _set_turn_store_tracker(bot, MagicMock())
        bot._send_response = AsyncMock(return_value="$route")
        install_send_response_mock(bot, bot._send_response)

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
                "mindroom.turn_controller.filter_agents_by_sender_permissions",
                return_value=[config.get_ids(runtime_paths_for(config))["general"]],
            ),
            patch(
                "mindroom.turn_controller.suggest_agent_for_message",
                new_callable=AsyncMock,
                return_value="general",
            ),
            patch(
                "mindroom.inbound_turn_normalizer.register_image_attachment",
                new_callable=AsyncMock,
                return_value=attachment_record,
            ) as mock_register_image,
        ):
            await bot._turn_controller._execute_router_relay(
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
        router_tracker = _set_turn_store_tracker(router_bot, MagicMock())
        router_tracker.has_responded.return_value = False
        general_tracker = _set_turn_store_tracker(general_bot, MagicMock())
        general_tracker.has_responded.return_value = False
        router_bot._send_response = AsyncMock(return_value="$route")
        install_send_response_mock(router_bot, router_bot._send_response)
        general_bot._generate_response = AsyncMock()
        install_generate_response_mock(general_bot, general_bot._generate_response)

        message_context = MessageContext(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )
        router_bot._conversation_resolver.extract_message_context = AsyncMock(return_value=message_context)
        general_bot._conversation_resolver.extract_message_context = AsyncMock(return_value=message_context)

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
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
            patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new_callable=AsyncMock,
                return_value=TeamResolution.none(),
            ),
            patch(
                "mindroom.turn_controller.suggest_agent_for_message",
                new_callable=AsyncMock,
                return_value="general",
            ),
            patch(
                "mindroom.inbound_turn_normalizer.register_file_or_video_attachment",
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
        _install_runtime_cache_support(bot)
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        tracker.has_responded.return_value = False
        bot._turn_controller._execute_router_relay = AsyncMock()
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
            patch("mindroom.turn_policy.get_agents_in_thread", return_value=[]),
            patch("mindroom.turn_policy.has_multiple_non_agent_users_in_thread", return_value=False),
            patch(
                "mindroom.turn_policy.get_available_agents_for_sender",
                return_value=[
                    config.get_ids(runtime_paths_for(config))["calculator"],
                    config.get_ids(runtime_paths_for(config))["general"],
                ],
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
            patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch("mindroom.coalescing.extract_media_caption", return_value="[Attached image]"),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await bot._on_message(room, text_event)
            await bot._on_media_message(room, image_event)

        assert bot._turn_controller._execute_router_relay.await_count == 2
        first_call = bot._turn_controller._execute_router_relay.await_args_list[0].kwargs
        second_call = bot._turn_controller._execute_router_relay.await_args_list[1].kwargs
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
        _install_runtime_cache_support(bot)
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        tracker.has_responded.return_value = False
        bot._turn_controller._execute_router_relay = AsyncMock()
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
            patch("mindroom.turn_policy.get_agents_in_thread", return_value=[]),
            patch("mindroom.turn_policy.has_multiple_non_agent_users_in_thread", return_value=False),
            patch(
                "mindroom.turn_policy.get_available_agents_for_sender",
                return_value=[config.get_ids(runtime_paths_for(config))["calculator"]],
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
            patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch("mindroom.coalescing.extract_media_caption", return_value="[Attached image]"),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await bot._on_message(room, text_event)
            await bot._on_media_message(room, image_event)

        bot._turn_controller._execute_router_relay.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_agent_receives_images_from_thread_root_after_routing(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """After router routes an image, the selected agent should resolve it via attachments."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        bot.client = AsyncMock()

        tracker = MagicMock()
        tracker.has_responded.return_value = False
        _set_turn_store_tracker(bot, tracker)
        bot._generate_response = AsyncMock(return_value="$response")
        install_generate_response_mock(bot, bot._generate_response)

        fake_image = Image(content=b"png-bytes", mime_type="image/png")

        # Simulate the routing mention event in a thread rooted at the image
        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_calculator:localhost")

        bot._conversation_resolver.extract_dispatch_context = AsyncMock(
            return_value=MessageContext(
                am_i_mentioned=True,
                is_thread=True,
                thread_id="$img_root",
                thread_history=[],
                mentioned_agents=[mock_agent_user.matrix_id],
                has_non_agent_mentions=False,
            ),
        )

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
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch("mindroom.turn_policy.get_agents_in_thread", return_value=[]),
            patch("mindroom.turn_policy.get_available_agents_for_sender", return_value=[]),
            patch(
                "mindroom.inbound_turn_normalizer.resolve_thread_attachment_ids",
                new_callable=AsyncMock,
                return_value=["att_img_root"],
            ) as mock_resolve_attachment_ids,
            patch(
                "mindroom.inbound_turn_normalizer.resolve_attachment_media",
                return_value=(["att_img_root"], [], [fake_image], [], []),
            ),
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new_callable=AsyncMock,
                return_value=TeamResolution.none(),
            ),
            patch("mindroom.turn_policy.should_agent_respond", return_value=True),
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
            config.get_ids(runtime_paths_for(config))["calculator"].full_id: MagicMock(),
            config.get_ids(runtime_paths_for(config))["general"].full_id: MagicMock(),
        }

        with patch("mindroom.turn_policy.decide_team_formation", new_callable=AsyncMock) as mock_decide:
            mock_decide.return_value = TeamResolution.none()
            bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
            bot.orchestrator = MagicMock()
            bot.orchestrator.agent_bots = {"calculator": MagicMock()}

            await bot._turn_policy.decide_team_for_sender(
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
        context = MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[bot.matrix_id],
            has_non_agent_mentions=False,
        )

        with (
            patch(
                "mindroom.bot.TurnPolicy.decide_team_for_sender",
                new=AsyncMock(
                    return_value=TeamResolution(
                        intent=TeamIntent.EXPLICIT_MEMBERS,
                        requested_members=[bot.matrix_id],
                        member_statuses=[
                            TeamResolutionMember(
                                agent=bot.matrix_id,
                                name=bot.agent_name,
                                status=TeamMemberStatus.ELIGIBLE,
                            ),
                        ],
                        eligible_members=[bot.matrix_id],
                        outcome=TeamOutcome.REJECT,
                        reason="Team request includes private agent 'mind'; private agents cannot participate in teams yet",
                    ),
                ),
            ),
            patch("mindroom.turn_policy.should_agent_respond", return_value=True) as mock_should_respond,
        ):
            action = await bot._turn_policy.resolve_response_action(
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
        context = MessageContext(
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

        with patch("mindroom.turn_policy.should_agent_respond", return_value=True) as mock_should_respond:
            action = await bot._turn_policy.resolve_response_action(
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
        context = MessageContext(
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

        with patch("mindroom.turn_policy.should_agent_respond", return_value=True) as mock_should_respond:
            action = await bot._turn_policy.resolve_response_action(
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
    async def test_resolve_response_action_rejects_non_running_requested_member(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Explicit team requests must treat stopped bots as unavailable."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "alpha": AgentConfig(display_name="AlphaAgent", rooms=["!room:localhost"]),
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                },
                authorization={"default_room_access": True},
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.orchestrator = MagicMock()
        bot.orchestrator.agent_bots = {
            "alpha": MagicMock(running=False),
            "calculator": MagicMock(running=True),
        }
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.users = {
            config.get_ids(runtime_paths_for(config))["alpha"].full_id: MagicMock(),
            config.get_ids(runtime_paths_for(config))["calculator"].full_id: MagicMock(),
        }
        context = MessageContext(
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

        with patch("mindroom.turn_policy.should_agent_respond", return_value=True) as mock_should_respond:
            action = await bot._turn_policy.resolve_response_action(
                context,
                room,
                "@user:localhost",
                "alpha and calculator, help",
                False,
            )

        assert action.kind == "reject"
        assert action.rejection_message == (
            "Team request includes agent 'alpha' that could not be materialized for this request."
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
        context = MessageContext(
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

        with patch("mindroom.turn_policy.should_agent_respond", return_value=True) as mock_should_respond:
            action = await bot._turn_policy.resolve_response_action(
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
        context = MessageContext(
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
            patch(
                "mindroom.bot.TurnPolicy.decide_team_for_sender",
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
                            ),
                            TeamResolutionMember(
                                agent=config.get_ids(runtime_paths_for(config))["calculator"],
                                name="calculator",
                                status=TeamMemberStatus.ELIGIBLE,
                            ),
                        ],
                        eligible_members=[config.get_ids(runtime_paths_for(config))["calculator"]],
                        outcome=TeamOutcome.REJECT,
                        reason="Team request includes agent 'alpha' that is not available right now.",
                    ),
                ),
            ),
            patch("mindroom.turn_policy.should_agent_respond", return_value=True) as mock_should_respond,
        ):
            action = await bot._turn_policy.resolve_response_action(
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
        context = MessageContext(
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
            patch(
                "mindroom.bot.TurnPolicy.decide_team_for_sender",
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
                            ),
                            TeamResolutionMember(
                                agent=config.get_ids(runtime_paths_for(config))["calculator"],
                                name="calculator",
                                status=TeamMemberStatus.ELIGIBLE,
                            ),
                        ],
                        eligible_members=[config.get_ids(runtime_paths_for(config))["calculator"]],
                        outcome=TeamOutcome.REJECT,
                        reason="Team request includes private agent 'alpha'; private agents cannot participate in teams yet",
                    ),
                ),
            ),
            patch("mindroom.turn_policy.should_agent_respond", return_value=True) as mock_should_respond,
        ):
            action = await bot._turn_policy.resolve_response_action(
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
        context = MessageContext(
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

        with patch("mindroom.turn_policy.should_agent_respond", return_value=True) as mock_should_respond:
            action = await bot._turn_policy.resolve_response_action(
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
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )

        with (
            patch(
                "mindroom.bot.TurnPolicy.decide_team_for_sender",
                new=AsyncMock(
                    return_value=TeamResolution.individual(
                        intent=TeamIntent.IMPLICIT_THREAD_TEAM,
                        requested_members=[bot.matrix_id],
                        member_statuses=[],
                        agent=bot.matrix_id,
                    ),
                ),
            ),
            patch("mindroom.turn_policy.should_agent_respond", return_value=False) as mock_should_respond,
        ):
            action = await bot._turn_policy.resolve_response_action(
                context,
                room,
                "@user:localhost",
                "help me",
                True,
            )

        assert action.kind == "individual"
        mock_should_respond.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_keeps_human_follow_up_in_active_thread(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Human follow-ups in an actively responding thread should bypass the normal multi-agent skip."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                    "general": AgentConfig(display_name="GeneralAgent", rooms=["!room:localhost"]),
                },
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        ids = config.get_ids(runtime_paths)
        room.users = {
            bot.matrix_id.full_id: MagicMock(),
            ids[ROUTER_AGENT_NAME].full_id: MagicMock(),
            ids["general"].full_id: MagicMock(),
        }
        target = MessageTarget.resolve(room.room_id, "$thread", "$event")
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=True,
            thread_id="$thread",
            thread_history=[
                ResolvedVisibleMessage(
                    sender=ids[ROUTER_AGENT_NAME].full_id,
                    body="routing",
                    timestamp=1,
                    event_id="$router",
                    content={"body": "routing"},
                    thread_id="$thread",
                    latest_event_id="$router",
                ),
                ResolvedVisibleMessage(
                    sender=bot.matrix_id.full_id,
                    body="working",
                    timestamp=2,
                    event_id="$agent",
                    content={"body": "working"},
                    thread_id="$thread",
                    latest_event_id="$agent",
                ),
            ],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )
        envelope = MessageEnvelope(
            source_event_id="$followup",
            room_id=room.room_id,
            target=target,
            requester_id="@user:localhost",
            sender_id="@user:localhost",
            body="stop if you see this",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=bot.agent_name,
            source_kind="live",
        )

        with (
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new=AsyncMock(return_value=TeamResolution.none()),
            ),
            patch("mindroom.turn_policy.should_agent_respond", return_value=False) as mock_should_respond,
            patch.object(
                bot._response_runner,
                "has_active_response_for_target",
                return_value=True,
            ) as mock_has_active_response,
        ):
            action = await bot._turn_policy.resolve_response_action(
                context,
                room,
                "@user:localhost",
                "stop if you see this",
                False,
                target=target,
                source_envelope=envelope,
                has_active_response_for_target=bot._response_runner.has_active_response_for_target,
            )

        assert action.kind == "individual"
        mock_should_respond.assert_called_once()
        mock_has_active_response.assert_called_once_with(target)

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
        tracker = _set_turn_store_tracker(bot, MagicMock())
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=True,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[bot.matrix_id],
                has_non_agent_mentions=False,
            ),
            target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id=None,
                reply_to_event_id=event.event_id,
            ),
            correlation_id="$event",
            envelope=_hook_envelope(body="help me", source_event_id="$event"),
        )
        action = ResponseAction(
            kind="reject",
            rejection_message="Team request includes private agent 'mind'; private agents cannot participate in teams yet",
        )

        mock_send_response = AsyncMock(return_value="$reply")
        install_send_response_mock(bot, mock_send_response)
        _replace_turn_policy_deps(
            bot,
            delivery_gateway=bot._delivery_gateway,
        )

        async def unused_payload_builder(_context: MessageContext) -> DispatchPayload:
            return DispatchPayload(prompt="help me")

        await bot._turn_controller._execute_response_action(
            room,
            event,
            dispatch,
            action,
            unused_payload_builder,
            processing_log="processing",
            dispatch_started_at=0.0,
            handled_turn=HandledTurnState.from_source_event_id(event.event_id),
        )

        mock_send_response.assert_awaited_once()
        assert mock_send_response.await_args.args[2].endswith(
            "private agents cannot participate in teams yet",
        )
        tracker.record_handled_turn.assert_called_once_with(
            HandledTurnState.from_source_event_id(
                "$event",
                response_event_id="$reply",
            ),
        )

    @pytest.mark.asyncio
    async def test_extract_dispatch_context_uses_thread_snapshot_without_full_history(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Dispatch startup should use a lightweight thread snapshot instead of full history."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Follow up",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
                },
                "event_id": "$event",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )
        snapshot = ThreadHistoryResult(
            [
                ResolvedVisibleMessage.synthetic(
                    sender="@user:localhost",
                    body="Root",
                    event_id="$thread_root",
                    timestamp=1234567889,
                    content={"body": "Root"},
                ),
            ],
            is_full_history=False,
        )

        mock_history = AsyncMock()
        mock_snapshot = AsyncMock(return_value=snapshot)

        with (
            patch.object(bot._conversation_cache, "get_thread_snapshot", new=mock_snapshot),
            patch.object(bot._conversation_cache, "get_thread_history", new=mock_history),
        ):
            context = await bot._conversation_resolver.extract_dispatch_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root"
        assert [message.event_id for message in context.thread_history] == ["$thread_root"]
        assert context.requires_full_thread_history is True
        mock_snapshot.assert_awaited_once_with(room.room_id, "$thread_root")
        mock_history.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_extract_dispatch_context_skips_extra_full_history_fetch_after_snapshot_fallback(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Fallback snapshots should not trigger a second full-history fetch during dispatch extraction."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        install_runtime_cache_support(bot)
        bot.client = AsyncMock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Follow up",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
                },
                "event_id": "$event",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )
        full_history = ThreadHistoryResult(
            [
                ResolvedVisibleMessage.synthetic(
                    sender="@user:localhost",
                    body="Root",
                    event_id="$thread_root",
                    timestamp=1234567889,
                    content={"body": "Root"},
                ),
                ResolvedVisibleMessage.synthetic(
                    sender="@mindroom_calculator:localhost",
                    body="Reply",
                    event_id="$reply",
                    timestamp=1234567890,
                    content={"body": "Reply"},
                ),
            ],
            is_full_history=True,
        )

        with (
            patch(
                "mindroom.matrix.client._fetch_thread_context_via_relations",
                new=AsyncMock(side_effect=_ThreadHistoryFastPathUnavailableError("unsupported")),
            ),
            patch(
                "mindroom.matrix.client._fetch_thread_history_via_room_messages",
                new=AsyncMock(return_value=full_history),
            ) as mock_snapshot_fallback,
        ):
            context = await bot._conversation_resolver.extract_dispatch_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root"
        assert context.thread_history == full_history
        assert context.requires_full_thread_history is False
        mock_snapshot_fallback.assert_awaited_once_with(bot.client, room.room_id, "$thread_root")

    @pytest.mark.asyncio
    async def test_dispatch_text_message_keeps_full_history_hydration_out_of_normal_dispatch(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Normal dispatch should keep pre-lock thread context lightweight."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$event"
        event.sender = "@user:localhost"
        event.body = "hello"
        event.server_timestamp = 1234567890
        event.source = {"content": {"body": "hello"}}

        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=False,
                is_thread=True,
                thread_id="$thread_root",
                thread_history=[
                    ResolvedVisibleMessage.synthetic(
                        sender="@user:localhost",
                        body="Snapshot root",
                        event_id="$thread_root",
                        timestamp=1,
                        content={"body": "Snapshot root"},
                    ),
                ],
                mentioned_agents=[],
                has_non_agent_mentions=False,
                requires_full_thread_history=True,
            ),
            target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id="$thread_root",
                reply_to_event_id=event.event_id,
            ),
            correlation_id="corr-hydrate-dispatch",
            envelope=_hook_envelope(body="hello", source_event_id="$event"),
        )
        _set_turn_store_tracker(bot, MagicMock())
        snapshot_history = list(dispatch.context.thread_history)
        call_order: list[str] = []

        async def fake_plan(*_args: object, **_kwargs: object) -> DispatchPlan:
            call_order.append("action")
            assert dispatch.context.thread_history == snapshot_history
            return DispatchPlan(
                kind="respond",
                response_action=ResponseAction(kind="individual"),
            )

        async def fake_build_payload(*_args: object, **_kwargs: object) -> DispatchPayload:
            call_order.append("payload")
            return DispatchPayload(prompt="hello")

        async def fake_generate_response(*_args: object, **kwargs: object) -> str:
            call_order.append("generate")
            assert kwargs["thread_history"] == snapshot_history
            assert kwargs["existing_event_id"] is None
            assert kwargs["existing_event_is_placeholder"] is False
            return "$response"

        generate_response = AsyncMock(side_effect=fake_generate_response)
        install_generate_response_mock(bot, generate_response)
        _replace_turn_policy_deps(
            bot,
            response_runner=bot._response_runner,
        )

        with (
            patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
            patch.object(bot._turn_policy, "plan_turn", new=AsyncMock(side_effect=fake_plan)),
            patch.object(
                bot._inbound_turn_normalizer,
                "build_dispatch_payload_with_attachments",
                new=AsyncMock(side_effect=fake_build_payload),
            ),
            patch.object(bot._turn_controller, "_log_dispatch_latency"),
        ):
            await bot._turn_controller._dispatch_text_message(
                room,
                _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
            )

        assert call_order == ["action", "payload", "generate"]
        assert dispatch.context.thread_history == snapshot_history
        assert dispatch.context.requires_full_thread_history is True

    @pytest.mark.asyncio
    async def test_dispatch_text_message_skip_path_does_not_hydrate_full_history(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Skip paths should avoid both payload work and pre-lock full-history hydration."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$event"
        event.sender = "@user:localhost"
        event.body = "hello"
        event.server_timestamp = 1234567890
        event.source = {"content": {"body": "hello"}}

        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=False,
                is_thread=True,
                thread_id="$thread_root",
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
                requires_full_thread_history=True,
            ),
            target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id="$thread_root",
                reply_to_event_id=event.event_id,
            ),
            correlation_id="corr-no-action",
            envelope=_hook_envelope(body="hello", source_event_id="$event"),
        )

        with (
            patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
            patch.object(
                bot._turn_policy,
                "plan_turn",
                new=AsyncMock(return_value=DispatchPlan(kind="ignore")),
            ),
            patch.object(
                bot._inbound_turn_normalizer,
                "build_dispatch_payload_with_attachments",
                new=AsyncMock(),
            ) as mock_build_payload,
            patch.object(bot._turn_controller, "_execute_response_action", new=AsyncMock()) as mock_execute,
        ):
            await bot._turn_controller._dispatch_text_message(
                room,
                _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
            )

        mock_build_payload.assert_not_awaited()
        mock_execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dispatch_text_message_command_bypasses_full_history_hydration(
        self,
        tmp_path: Path,
    ) -> None:
        """Commands should short-circuit before full thread-history hydration."""
        agent_user = AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$command"
        event.sender = "@user:localhost"
        event.body = "!help"
        event.server_timestamp = 1234567890
        event.source = {"content": {"body": "!help"}}

        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=False,
                is_thread=True,
                thread_id="$thread_root",
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
                requires_full_thread_history=True,
            ),
            target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id="$thread_root",
                reply_to_event_id=event.event_id,
            ),
            correlation_id="corr-command-bypass",
            envelope=_hook_envelope(body="!help", source_event_id="$command"),
        )

        with (
            patch.object(
                bot._inbound_turn_normalizer,
                "resolve_text_event",
                new=AsyncMock(return_value=event),
            ),
            patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
            patch.object(bot._turn_controller, "_execute_command", new=AsyncMock()) as mock_execute_command,
        ):
            await bot._turn_controller._dispatch_text_message(
                room,
                _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
            )

        mock_execute_command.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_router_dispatch_marks_visible_echo_from_any_coalesced_source_event(
        self,
        tmp_path: Path,
    ) -> None:
        """Router ignore plans should preserve visible echoes recorded on non-primary source events."""
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = _make_matrix_client_mock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        tracker.visible_echo_event_id_for_sources.side_effect = (
            lambda source_event_ids: "$voice_echo" if tuple(source_event_ids) == ("$voice", "$text") else None
        )
        tracker.has_responded.return_value = False

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$text"
        event.sender = "@user:localhost"
        event.body = "hello"
        event.server_timestamp = 1234567890
        event.source = {"content": {"body": "hello"}}

        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=False,
                is_thread=True,
                thread_id="$thread_root",
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=True,
            ),
            target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id="$thread_root",
                reply_to_event_id=event.event_id,
            ),
            correlation_id="corr-visible-echo",
            envelope=_hook_envelope(body="hello", source_event_id="$text"),
        )

        with (
            patch.object(bot._inbound_turn_normalizer, "resolve_text_event", new=AsyncMock(return_value=event)),
            patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
            patch.object(bot._turn_controller, "_has_newer_unresponded_in_thread", return_value=False),
            patch.object(bot._turn_controller, "_should_skip_deep_synthetic_full_dispatch", return_value=False),
        ):
            await bot._turn_controller._dispatch_text_message(
                room,
                _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
                handled_turn=HandledTurnState.create(
                    ["$voice", "$text"],
                    source_event_prompts={"$voice": "voice prompt", "$text": "text prompt"},
                ),
            )

        assert tracker.record_handled_turn.call_args_list == [
            call(
                HandledTurnState.create(
                    ["$voice", "$text"],
                    response_event_id="$voice_echo",
                    source_event_prompts={"$voice": "voice prompt", "$text": "text prompt"},
                    visible_echo_event_id="$voice_echo",
                ),
            ),
        ]

    @pytest.mark.asyncio
    async def test_dispatch_text_message_preserves_prompt_map_when_router_routes_coalesced_turn(
        self,
        tmp_path: Path,
    ) -> None:
        """Router handoff for a coalesced turn should persist the full prompt map."""
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        _wrap_extracted_collaborators(bot)
        bot.client = _make_matrix_client_mock()
        tracker = _set_turn_store_tracker(bot, MagicMock())

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.canonical_alias = None
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$text"
        event.sender = "@user:localhost"
        event.body = "hello"
        event.server_timestamp = 1234567890
        event.source = {"content": {"body": "hello"}}

        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=False,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
            ),
            target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id=None,
                reply_to_event_id=event.event_id,
            ),
            correlation_id="corr-router-coalesced",
            envelope=_hook_envelope(body="hello", source_event_id="$text"),
        )
        coalesced_turn = HandledTurnState.create(
            ["$voice", "$text"],
            source_event_prompts={"$voice": "voice prompt", "$text": "hello"},
        )

        async def fake_execute_router_relay(
            _room: nio.MatrixRoom,
            _event: nio.RoomMessageText,
            _thread_history: Sequence[ResolvedVisibleMessage],
            _thread_id: str | None = None,
            message: str | None = None,
            *,
            requester_user_id: str,
            extra_content: dict[str, Any] | None = None,
            media_events: list[object] | None = None,
            handled_turn: HandledTurnState | None = None,
        ) -> None:
            assert message == "hello"
            assert requester_user_id == "@user:localhost"
            assert extra_content is None
            assert media_events is None
            assert handled_turn is not None
            assert handled_turn.source_event_prompts == {"$voice": "voice prompt", "$text": "hello"}
            bot._turn_controller._mark_source_events_responded(handled_turn.with_response_event_id("$route"))

        with (
            patch.object(bot._inbound_turn_normalizer, "resolve_text_event", new=AsyncMock(return_value=event)),
            patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
            patch.object(
                bot._turn_policy,
                "plan_turn",
                new=AsyncMock(
                    return_value=DispatchPlan(
                        kind="route",
                        router_message="hello",
                        router_event=event,
                    ),
                ),
            ),
            patch.object(
                bot._turn_controller,
                "_execute_router_relay",
                new=AsyncMock(side_effect=fake_execute_router_relay),
            ),
            patch.object(bot._turn_controller, "_has_newer_unresponded_in_thread", return_value=False),
            patch.object(bot._turn_controller, "_should_skip_deep_synthetic_full_dispatch", return_value=False),
        ):
            await bot._turn_controller._dispatch_text_message(
                room,
                _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
                handled_turn=coalesced_turn,
            )

        assert tracker.record_handled_turn.call_args_list == [
            call(
                HandledTurnState.create(
                    ["$voice", "$text"],
                    response_event_id="$route",
                    source_event_prompts={"$voice": "voice prompt", "$text": "hello"},
                ).with_response_context(
                    response_owner="router",
                    history_scope=None,
                    conversation_target=dispatch.target,
                ),
            ),
        ]

    @pytest.mark.asyncio
    async def test_trusted_internal_router_relays_bypass_user_turn_coalescing(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Agent-authored relays with preserved original sender should dispatch immediately."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.canonical_alias = None
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$relay",
                "sender": "@mindroom_router:localhost",
                "origin_server_ts": 1234567890,
                "content": {
                    "msgtype": "m.text",
                    "body": "@mindroom_code:localhost could you help with this?",
                    ORIGINAL_SENDER_KEY: "@user:localhost",
                },
            },
        )

        with (
            patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock()) as mock_dispatch,
            patch.object(bot._coalescing_gate, "enqueue", new=AsyncMock()) as mock_enqueue,
        ):
            await bot._turn_controller._enqueue_for_dispatch(event, room, source_kind="message")

        mock_dispatch.assert_awaited_once_with(room, event, "@user:localhost")
        mock_enqueue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_message_inner_bypasses_coalescing_for_active_thread_follow_up(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Human follow-ups in an actively responding thread must bypass IN_FLIGHT coalescing."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        bot.client = _make_matrix_client_mock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$followup",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "body": "stop right now!",
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$thread_root",
                        "is_falling_back": True,
                        "m.in_reply_to": {"event_id": "$thread_root"},
                    },
                },
            },
        )
        prepared_event = PreparedTextEvent(
            sender="@user:localhost",
            event_id="$followup",
            body="stop right now!",
            source=event.source,
            server_timestamp=1234567890,
        )
        target = MessageTarget.resolve(room.room_id, "$thread_root", event.event_id)
        envelope = MessageEnvelope(
            source_event_id=event.event_id,
            room_id=room.room_id,
            target=target,
            requester_id="@user:localhost",
            sender_id="@user:localhost",
            body="stop right now!",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=bot.agent_name,
            source_kind="message",
        )

        with (
            patch.object(
                bot._turn_controller,
                "_precheck_dispatch_event",
                return_value=_PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
            ),
            patch(
                "mindroom.inbound_turn_normalizer.InboundTurnNormalizer.resolve_text_event",
                new=AsyncMock(return_value=prepared_event),
            ),
            patch(
                "mindroom.conversation_resolver.ConversationResolver.build_ingress_envelope",
                return_value=envelope,
            ),
            patch.object(bot._turn_controller, "_should_skip_deep_synthetic_full_dispatch", return_value=False),
            patch("mindroom.turn_controller.should_handle_interactive_text_response", return_value=False),
            patch.object(
                bot._conversation_resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$thread_root"),
            ),
            patch.object(
                bot._response_runner,
                "has_active_response_for_target",
                return_value=True,
            ) as mock_has_active_response,
            patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock()) as mock_dispatch,
            patch.object(bot._coalescing_gate, "enqueue", new=AsyncMock()) as mock_enqueue,
        ):
            await bot._on_message(room, event)

        mock_has_active_response.assert_called_once()
        active_target = mock_has_active_response.call_args.args[0]
        assert active_target.resolved_thread_id == target.resolved_thread_id
        mock_dispatch.assert_awaited_once_with(room, prepared_event, "@user:localhost")
        mock_enqueue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_team_defers_placeholder_creation_to_coordinator(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Planner-side team dispatch should hand placeholder ownership to the coordinator."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        bot.logger = MagicMock()
        _replace_turn_policy_deps(bot, logger=bot.logger)

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=True,
                is_thread=True,
                thread_id="$thread_root",
                thread_history=[
                    _visible_message(
                        sender="@user:localhost",
                        body="hello",
                        timestamp=0,
                        event_id="$thread_root",
                    ),
                ],
                mentioned_agents=[bot.matrix_id],
                has_non_agent_mentions=False,
                requires_full_thread_history=False,
            ),
            target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id="$thread_root",
                reply_to_event_id=event.event_id,
            ),
            correlation_id="corr-team-dispatch",
            envelope=_hook_envelope(body="hello", source_event_id="$event"),
        )
        action = ResponseAction(
            kind="team",
            form_team=TeamResolution.team(
                intent=TeamIntent.EXPLICIT_MEMBERS,
                requested_members=[bot.matrix_id],
                member_statuses=[],
                eligible_members=[bot.matrix_id],
                mode=TeamMode.COORDINATE,
            ),
        )

        mock_send_response = AsyncMock()
        mock_generate_team_response = AsyncMock(return_value="$team-response")
        install_send_response_mock(bot, mock_send_response)
        bot._response_runner.generate_team_response_helper = mock_generate_team_response
        _replace_turn_policy_deps(
            bot,
            delivery_gateway=bot._delivery_gateway,
            response_runner=bot._response_runner,
        )

        with patch.object(TurnController, "_log_dispatch_latency"):

            async def payload_builder(_context: MessageContext) -> DispatchPayload:
                return DispatchPayload(prompt="help me")

            await bot._turn_controller._execute_response_action(
                room,
                event,
                dispatch,
                action,
                payload_builder,
                processing_log="processing",
                dispatch_started_at=0.0,
                handled_turn=HandledTurnState.from_source_event_id(event.event_id),
            )

        team_request = mock_generate_team_response.await_args.args[0]
        assert team_request.existing_event_id is None
        assert team_request.existing_event_is_placeholder is False
        mock_send_response.assert_not_awaited()
        tracker.record_handled_turn.assert_called_once_with(
            HandledTurnState.from_source_event_id(
                "$event",
                response_event_id="$team-response",
            ),
        )

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_does_not_send_placeholder_before_response_runner(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Planner-side execution should pass placeholder ownership to the coordinator."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        bot.logger = MagicMock()
        _replace_turn_policy_deps(bot, logger=bot.logger)

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=True,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[bot.matrix_id],
                has_non_agent_mentions=False,
                requires_full_thread_history=False,
            ),
            target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id=None,
                reply_to_event_id=event.event_id,
            ),
            correlation_id="corr-individual-dispatch",
            envelope=_hook_envelope(body="hello", source_event_id="$event"),
        )

        mock_send_response = AsyncMock()
        mock_generate_response = AsyncMock(return_value="$response")
        install_send_response_mock(bot, mock_send_response)
        install_generate_response_mock(bot, mock_generate_response)
        _replace_turn_policy_deps(
            bot,
            delivery_gateway=bot._delivery_gateway,
            response_runner=bot._response_runner,
        )

        with patch.object(TurnController, "_log_dispatch_latency"):

            async def payload_builder(_context: MessageContext) -> DispatchPayload:
                return DispatchPayload(prompt="help me")

            await bot._turn_controller._execute_response_action(
                room,
                event,
                dispatch,
                ResponseAction(kind="individual"),
                payload_builder,
                processing_log="processing",
                dispatch_started_at=0.0,
                handled_turn=HandledTurnState.from_source_event_id(event.event_id),
            )

        mock_send_response.assert_not_awaited()
        assert mock_generate_response.await_args.kwargs["existing_event_id"] is None
        assert mock_generate_response.await_args.kwargs["existing_event_is_placeholder"] is False
        tracker.record_handled_turn.assert_called_once_with(
            HandledTurnState.from_source_event_id(
                "$event",
                response_event_id="$response",
            ),
        )

    @pytest.mark.asyncio
    async def test_media_download_failure_sends_terminal_error_without_placeholder(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Media setup failures before response generation should send one terminal error reply."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        tracker = MagicMock()
        tracker.has_responded.return_value = False
        _set_turn_store_tracker(bot, tracker)

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        room.users = {"@mindroom_calculator:localhost": MagicMock(), "@user:localhost": MagicMock()}
        event = MagicMock(spec=nio.RoomMessageImage)
        event.sender = "@user:localhost"
        event.event_id = "$img_event_fail"
        event.body = "photo.jpg"
        event.server_timestamp = 1000
        event.source = {"content": {"body": "photo.jpg"}}

        bot._conversation_resolver.extract_message_context = AsyncMock(
            return_value=MessageContext(
                am_i_mentioned=False,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
                requires_full_thread_history=False,
            ),
        )
        bot._edit_message = AsyncMock(return_value=True)
        install_edit_message_mock(bot, bot._edit_message)
        bot._generate_response = AsyncMock()
        install_generate_response_mock(bot, bot._generate_response)
        send_response_mock = AsyncMock(return_value="$error")
        install_send_response_mock(bot, send_response_mock)
        wrap_extracted_collaborators(bot, "_turn_policy")
        bot._turn_policy.plan_turn = AsyncMock(
            return_value=DispatchPlan(
                kind="respond",
                response_action=ResponseAction(kind="individual"),
            ),
        )

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
            patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new_callable=AsyncMock,
                return_value=TeamResolution.none(),
            ),
            patch("mindroom.turn_policy.should_agent_respond", return_value=True),
            patch("mindroom.inbound_turn_normalizer.download_image", new_callable=AsyncMock, return_value=None),
            patch.object(bot._turn_controller, "_log_dispatch_latency"),
        ):
            await bot._on_media_message(room, event)

        bot._generate_response.assert_not_called()
        bot._edit_message.assert_not_awaited()
        send_response_mock.assert_awaited_once()
        send_args = send_response_mock.await_args.args
        assert send_args[0] == room.room_id
        assert send_args[1] == "$img_event_fail"
        assert "Failed to download image" in send_args[2]
        tracker.record_handled_turn.assert_called_once_with(
            _agent_response_handled_turn(
                agent_name=mock_agent_user.agent_name,
                room_id=room.room_id,
                event_id="$img_event_fail",
                response_event_id="$error",
                source_event_prompts={"$img_event_fail": "[Attached image]"},
            ),
        )

    @pytest.mark.asyncio
    async def test_finalize_dispatch_failure_sends_terminal_error_message(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Dispatch setup failures should send a terminal error message in-thread."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        mock_send_response = AsyncMock(return_value="$error")
        install_send_response_mock(bot, mock_send_response)
        _replace_turn_policy_deps(bot, delivery_gateway=bot._delivery_gateway)

        response_event_id = await bot._turn_controller._finalize_dispatch_failure(
            room_id="!test:localhost",
            reply_to_event_id="$event",
            thread_id="$thread_root",
            error=RuntimeError("boom"),
        )

        assert response_event_id == "$error"
        mock_send_response.assert_awaited_once_with(
            "!test:localhost",
            "$event",
            "[calculator] ⚠️ Error: boom",
            "$thread_root",
            reply_to_event=None,
            skip_mentions=False,
            tool_trace=None,
            extra_content={STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED},
            thread_mode_override=None,
            target=MessageTarget.resolve("!test:localhost", "$thread_root", "$event"),
        )

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_marks_terminal_error_event_without_placeholder(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Dispatch setup failures should track the terminal error event even without a placeholder."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        bot.logger = MagicMock()
        _replace_turn_policy_deps(bot, logger=bot.logger)

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=False,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
                requires_full_thread_history=False,
            ),
            target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id=None,
                reply_to_event_id=event.event_id,
            ),
            correlation_id="corr-payload-error-1",
            envelope=_hook_envelope(body="hello", source_event_id="$event"),
        )

        failure_message = "setup failed"

        async def payload_builder(_context: MessageContext) -> DispatchPayload:
            raise RuntimeError(failure_message)

        mock_send_response = AsyncMock(return_value="$error")
        mock_edit = AsyncMock(return_value=False)
        install_send_response_mock(bot, mock_send_response)
        install_edit_message_mock(bot, mock_edit)
        _replace_turn_policy_deps(
            bot,
            delivery_gateway=bot._delivery_gateway,
        )

        with patch.object(TurnController, "_log_dispatch_latency"):
            await bot._turn_controller._execute_response_action(
                room,
                event,
                dispatch,
                ResponseAction(kind="individual"),
                payload_builder,
                processing_log="processing",
                dispatch_started_at=0.0,
                handled_turn=HandledTurnState.from_source_event_id(event.event_id),
            )

        mock_edit.assert_not_awaited()
        mock_send_response.assert_awaited_once()
        tracker.record_handled_turn.assert_called_once_with(
            HandledTurnState.from_source_event_id(
                "$event",
                response_event_id="$error",
            ),
        )

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_does_not_mark_responded_when_failure_cleanup_is_incomplete(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Incomplete placeholder cleanup should leave the source event retryable."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        bot.logger = MagicMock()
        _replace_turn_policy_deps(bot, logger=bot.logger)

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=False,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
                requires_full_thread_history=False,
            ),
            target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id=None,
                reply_to_event_id=event.event_id,
            ),
            correlation_id="corr-payload-error-2",
            envelope=_hook_envelope(body="hello", source_event_id="$event"),
        )

        failure_message = "setup failed"

        async def payload_builder(_context: MessageContext) -> DispatchPayload:
            raise RuntimeError(failure_message)

        with patch("mindroom.bot.TurnController._finalize_dispatch_failure", new=AsyncMock(return_value=None)):
            await bot._turn_controller._execute_response_action(
                room,
                event,
                dispatch,
                ResponseAction(kind="individual"),
                payload_builder,
                processing_log="processing",
                dispatch_started_at=0.0,
                handled_turn=HandledTurnState.from_source_event_id(event.event_id),
            )

        tracker.record_handled_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_does_not_mark_responded_when_suppressed_cleanup_fails(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Suppressed placeholder cleanup failures should leave the source retryable."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        bot.logger = MagicMock()
        wrap_extracted_collaborators(bot, "_response_runner")
        replace_turn_controller_deps(
            bot,
            logger=bot.logger,
            response_runner=bot._response_runner,
        )

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=True,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[bot.matrix_id],
                has_non_agent_mentions=False,
                requires_full_thread_history=False,
            ),
            target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id=None,
                reply_to_event_id=event.event_id,
            ),
            correlation_id="corr-suppress-cleanup-failed",
            envelope=_hook_envelope(body="hello", source_event_id="$event"),
        )

        async def payload_builder(_context: MessageContext) -> DispatchPayload:
            return DispatchPayload(prompt="help me")

        with (
            patch.object(
                bot._response_runner,
                "generate_response",
                new=AsyncMock(side_effect=SuppressedPlaceholderCleanupError("failed cleanup")),
            ),
            patch.object(bot._turn_controller, "_log_dispatch_latency", create=True),
            pytest.raises(SuppressedPlaceholderCleanupError),
        ):
            await bot._turn_controller._execute_response_action(
                room,
                event,
                dispatch,
                ResponseAction(kind="individual"),
                payload_builder,
                processing_log="processing",
                dispatch_started_at=0.0,
                handled_turn=HandledTurnState.from_source_event_id(event.event_id),
            )

        tracker.record_handled_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_does_not_mark_responded_when_generation_returns_no_final_event(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Suppressed delivery with no final event should keep the source retryable."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        bot.logger = MagicMock()

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=True,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[bot.matrix_id],
                has_non_agent_mentions=False,
                requires_full_thread_history=False,
            ),
            target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id=None,
                reply_to_event_id=event.event_id,
            ),
            correlation_id="corr-suppress-cleanup-complete",
            envelope=_hook_envelope(body="hello", source_event_id="$event"),
        )

        async def payload_builder(_context: MessageContext) -> DispatchPayload:
            return DispatchPayload(prompt="help me")

        with (
            patch.object(bot._response_runner, "generate_response", new=AsyncMock(return_value=None)),
            patch.object(bot._turn_controller, "_log_dispatch_latency", create=True),
        ):
            await bot._turn_controller._execute_response_action(
                room,
                event,
                dispatch,
                ResponseAction(kind="individual"),
                payload_builder,
                processing_log="processing",
                dispatch_started_at=0.0,
                handled_turn=HandledTurnState.from_source_event_id(event.event_id),
            )

        tracker.record_handled_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_logs_startup_latency(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Dispatch execution should log setup timing fields before coordinator handoff."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _set_turn_store_tracker(bot, MagicMock())
        bot.logger = MagicMock()
        _replace_turn_policy_deps(bot, logger=bot.logger)

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=False,
                is_thread=False,
                thread_id=None,
                thread_history=ThreadHistoryResult(
                    [],
                    is_full_history=True,
                    diagnostics={
                        "cache_read_ms": 11.0,
                        "incremental_refresh_ms": 22.0,
                        "resolution_ms": 33.0,
                        "sidecar_hydration_ms": 44.0,
                    },
                ),
                mentioned_agents=[],
                has_non_agent_mentions=False,
                requires_full_thread_history=False,
            ),
            target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id=None,
                reply_to_event_id=event.event_id,
            ),
            correlation_id="corr-latency-log",
            envelope=_hook_envelope(body="hello", source_event_id="$event"),
        )

        monotonic_values = itertools.count(start=10.0, step=0.1)
        mock_generate_response = AsyncMock(return_value="$response")
        install_generate_response_mock(bot, mock_generate_response)
        _replace_turn_policy_deps(
            bot,
            logger=bot.logger,
            response_runner=bot._response_runner,
        )

        with patch("mindroom.turn_controller.time.monotonic", side_effect=lambda: next(monotonic_values)):

            async def payload_builder(_context: MessageContext) -> DispatchPayload:
                return DispatchPayload(prompt="help me")

            await bot._turn_controller._execute_response_action(
                room,
                event,
                dispatch,
                ResponseAction(kind="individual"),
                payload_builder,
                processing_log="processing",
                dispatch_started_at=9.5,
                handled_turn=HandledTurnState.from_source_event_id(event.event_id),
            )

        latency_logs = [
            call for call in bot.logger.info.call_args_list if call.args and call.args[0] == "Response startup latency"
        ]
        assert latency_logs
        latency_kwargs = latency_logs[-1].kwargs
        assert "placeholder_event_id" not in latency_kwargs
        assert "placeholder_visible_ms" not in latency_kwargs
        assert latency_kwargs["context_hydration_ms"] == 500.0
        assert latency_kwargs["cache_read_ms"] == 11.0
        assert latency_kwargs["incremental_refresh_ms"] == 22.0
        assert latency_kwargs["resolution_ms"] == 33.0
        assert latency_kwargs["sidecar_hydration_ms"] == 44.0
        assert latency_kwargs["payload_hydration_ms"] >= 0.0
        assert latency_kwargs["startup_total_ms"] == (
            latency_kwargs["context_hydration_ms"] + latency_kwargs["payload_hydration_ms"]
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enable_streaming", [True, False])
    @patch("mindroom.config.main.Config.from_yaml")
    @patch("mindroom.teams.get_agent_knowledge")
    @patch("mindroom.teams.create_agent")
    @patch("mindroom.teams.get_model_instance")
    @patch("mindroom.teams.Team.arun")
    @patch("mindroom.response_runner.ai_response")
    @patch("mindroom.response_runner.stream_agent_response")
    @patch("mindroom.matrix.conversation_cache.MatrixConversationCache.get_thread_snapshot")
    @patch("mindroom.matrix.conversation_cache.MatrixConversationCache.get_thread_history")
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
            config.get_ids(runtime_paths_for(config))["general"].full_id: MagicMock(),
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
        mock_fetch_history.return_value = test1_history
        mock_fetch_snapshot.return_value = test1_history

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
                sender=config.get_ids(runtime_paths_for(config))["general"].full_id
                if "general" in config.get_ids(runtime_paths_for(config))
                else "@mindroom_general:localhost",
                body="Another agent response",
                timestamp=125,
                event_id="prev3",
            ),
        ]
        mock_fetch_history.return_value = test2_history
        mock_fetch_snapshot.return_value = test2_history

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
        _turn_store(bot).record_turn(HandledTurnState.from_source_event_id("event123"))

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
    async def test_initialize_raises_when_shared_event_cache_init_fails(self, tmp_path: Path) -> None:
        """Initialize should fail fast when the shared event cache cannot open."""
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        config = _runtime_bound_config(
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

        with (
            patch("mindroom.orchestrator.load_config", return_value=config),
            patch("mindroom.orchestrator.load_plugins", return_value=[]),
            patch.object(orchestrator, "_prepare_user_account", new=AsyncMock()),
            patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
            patch("mindroom.orchestrator._EventCache.initialize", new=AsyncMock(side_effect=RuntimeError("boom"))),
            patch.object(MultiAgentOrchestrator, "_create_managed_bot") as mock_create_managed_bot,
            pytest.raises(RuntimeError, match="boom"),
        ):
            await orchestrator.initialize()

        assert orchestrator.config is config
        assert mock_create_managed_bot.call_count == 0

    @pytest.mark.asyncio
    async def test_initialize_does_not_activate_hook_runtime_before_user_account_succeeds(
        self,
        tmp_path: Path,
    ) -> None:
        """Startup must not swap the live hook runtime before user-account prep succeeds."""
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        config = MagicMock()
        config.agents = {}
        config.teams = {}
        initial_hook_registry = orchestrator.hook_registry
        new_hook_registry = HookRegistry.empty()

        with (
            patch("mindroom.orchestrator.load_config", return_value=config),
            patch("mindroom.orchestrator.load_plugins", return_value=[]),
            patch("mindroom.orchestrator.HookRegistry.from_plugins", return_value=new_hook_registry),
            patch("mindroom.orchestrator.reset_hook_execution_state") as mock_reset_hook_execution_state,
            patch("mindroom.orchestrator.set_scheduling_hook_registry") as mock_set_scheduling_hook_registry,
            patch.object(
                orchestrator,
                "_prepare_user_account",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch.object(MultiAgentOrchestrator, "_create_managed_bot") as mock_create_managed_bot,
            pytest.raises(RuntimeError, match="boom"),
        ):
            await orchestrator.initialize()

        assert orchestrator.config is None
        assert orchestrator.hook_registry is initial_hook_registry
        mock_reset_hook_execution_state.assert_not_called()
        mock_set_scheduling_hook_registry.assert_not_called()
        mock_create_managed_bot.assert_not_called()

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
    async def test_run_auxiliary_task_forever_exits_cleanly_when_shutdown_requested(self) -> None:
        """Shutdown should suppress restart logging for clean auxiliary exits."""
        shutdown_requested = False
        calls = 0

        async def _operation() -> None:
            nonlocal calls, shutdown_requested
            calls += 1
            shutdown_requested = True

        with patch("mindroom.orchestrator.logger.warning") as mock_warning:
            await _run_auxiliary_task_forever(
                "test task",
                _operation,
                should_restart=lambda: not shutdown_requested,
            )

        assert calls == 1
        mock_warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_auxiliary_task_forever_suppresses_crash_log_when_shutdown_requested(self) -> None:
        """Shutdown should suppress crash logging for auxiliary teardown errors."""
        shutdown_requested = False
        calls = 0

        async def _operation() -> None:
            nonlocal calls, shutdown_requested
            calls += 1
            shutdown_requested = True
            msg = "boom"
            raise RuntimeError(msg)

        with patch("mindroom.orchestrator.logger.exception") as mock_exception:
            await _run_auxiliary_task_forever(
                "test task",
                _operation,
                should_restart=lambda: not shutdown_requested,
            )

        assert calls == 1
        mock_exception.assert_not_called()

    def test_signal_aware_uvicorn_server_marks_shutdown_requested_on_signal(self) -> None:
        """Uvicorn signal handling should surface shutdown intent before serve() returns."""
        shutdown_requested = asyncio.Event()
        config = uvicorn.Config(app=lambda _scope, _receive, _send: None)
        server = _SignalAwareUvicornServer(config, shutdown_requested)

        with patch.object(uvicorn.Server, "handle_exit") as mock_handle_exit:
            server.handle_exit(signal.SIGINT, None)

        assert shutdown_requested.is_set()
        mock_handle_exit.assert_called_once_with(signal.SIGINT, None)

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
        config.cache = MagicMock()
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
            patch.object(orchestrator, "_sync_event_cache_service", new=AsyncMock()),
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
        current_config.cache = MagicMock()
        new_config = MagicMock()
        new_config.authorization.global_users = []
        new_config.cache = MagicMock()
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
            changed_mcp_servers=set(),
            entities_to_restart=set(),
            new_entities=set(),
            removed_entities=set(),
            only_support_service_changes=True,
        )

        with (
            patch("mindroom.orchestrator.load_config", return_value=new_config) as mock_load_config,
            patch("mindroom.orchestrator.load_plugins"),
            patch("mindroom.orchestrator.build_config_update_plan", return_value=plan),
            patch.object(orchestrator, "_sync_event_cache_service", new=AsyncMock()),
            patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
        ):
            updated = await orchestrator.update_config()

        assert updated is False
        mock_load_config.assert_called_once()
        assert mock_load_config.call_args.args[0].config_path == config_path.resolve()

    @pytest.mark.asyncio
    async def test_update_config_does_not_swap_hook_runtime_on_failed_reload(self, tmp_path: Path) -> None:
        """Failed reloads must leave the active hook snapshot and scheduling registry untouched."""
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))

        current_config = MagicMock()
        current_config.authorization.global_users = []
        current_config.cache = MagicMock()
        new_config = MagicMock()
        new_config.authorization.global_users = []
        new_config.cache = MagicMock()
        old_hook_registry = HookRegistry.empty()
        new_hook_registry = HookRegistry.empty()

        orchestrator.config = current_config
        orchestrator.hook_registry = old_hook_registry
        plan = SimpleNamespace(
            mindroom_user_changed=True,
            new_config=new_config,
            changed_mcp_servers=set(),
            entities_to_restart=set(),
            new_entities=set(),
            removed_entities=set(),
            only_support_service_changes=True,
        )

        with (
            patch("mindroom.orchestrator.load_config", return_value=new_config),
            patch("mindroom.orchestrator.load_plugins", return_value=[]),
            patch("mindroom.orchestrator.HookRegistry.from_plugins", return_value=new_hook_registry),
            patch("mindroom.orchestrator.reset_hook_execution_state") as mock_reset_hook_execution_state,
            patch("mindroom.orchestrator.set_scheduling_hook_registry") as mock_set_scheduling_hook_registry,
            patch("mindroom.orchestrator.build_config_update_plan", return_value=plan),
            patch.object(
                orchestrator,
                "_prepare_user_account",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await orchestrator.update_config()

        assert orchestrator.config is current_config
        assert orchestrator.hook_registry is old_hook_registry
        mock_reset_hook_execution_state.assert_not_called()
        mock_set_scheduling_hook_registry.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_config_initializes_shared_event_cache_for_unchanged_bots(self, tmp_path: Path) -> None:
        """Cache service should initialize and bind when a test runtime skipped startup wiring."""
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
                },
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
        )

        orchestrator.config = old_config
        orchestrator.running = True
        router_bot = _mock_managed_bot(old_config)
        general_bot = _mock_managed_bot(old_config)
        orchestrator.agent_bots = {"router": router_bot, "general": general_bot}

        with (
            patch("mindroom.orchestrator.load_config", return_value=new_config),
            patch("mindroom.orchestrator.load_plugins", return_value=[]),
            patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
            patch.object(orchestrator, "_schedule_knowledge_refresh", new=AsyncMock()),
            patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
        ):
            try:
                updated = await orchestrator.update_config()
                assert updated is False
                assert router_bot.event_cache is orchestrator._event_cache
                assert general_bot.event_cache is orchestrator._event_cache
                assert router_bot.event_cache_write_coordinator is orchestrator._event_cache_write_coordinator
                assert general_bot.event_cache_write_coordinator is orchestrator._event_cache_write_coordinator
            finally:
                await orchestrator._close_event_cache_write_coordinator()
                await orchestrator._close_event_cache()

    @pytest.mark.asyncio
    async def test_update_config_keeps_shared_event_cache_when_db_path_changes(self, tmp_path: Path) -> None:
        """Hot reload should keep the active cache service and defer db_path changes to restart."""
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
                cache={"db_path": "event-cache-old.db"},
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
                },
                models={"default": {"provider": "test", "id": "test-model"}},
                cache={"db_path": "event-cache-new.db"},
            ),
            tmp_path,
        )

        orchestrator.config = old_config
        orchestrator.running = True
        router_bot = _mock_managed_bot(old_config)
        general_bot = _mock_managed_bot(old_config)
        orchestrator.agent_bots = {"router": router_bot, "general": general_bot}
        await orchestrator._sync_event_cache_service(old_config)
        old_cache = orchestrator._event_cache
        assert old_cache is not None

        with (
            patch("mindroom.orchestrator.load_config", return_value=new_config),
            patch("mindroom.orchestrator.load_plugins", return_value=[]),
            patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
            patch.object(orchestrator, "_schedule_knowledge_refresh", new=AsyncMock()),
            patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
        ):
            try:
                updated = await orchestrator.update_config()
                assert updated is False
                assert orchestrator._event_cache is old_cache
                assert old_cache.db_path == old_config.cache.resolve_db_path(orchestrator.runtime_paths)
                assert router_bot.event_cache is old_cache
                assert general_bot.event_cache is old_cache
                assert orchestrator._event_cache_write_coordinator is not None
                assert router_bot.event_cache_write_coordinator is orchestrator._event_cache_write_coordinator
                assert general_bot.event_cache_write_coordinator is orchestrator._event_cache_write_coordinator
            finally:
                await orchestrator._close_event_cache_write_coordinator()
                await orchestrator._close_event_cache()

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
