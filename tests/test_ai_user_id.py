"""Test that user_id is passed through to agent.arun() for Agno learning."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from contextvars import Context
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.db.base import SessionType
from agno.media import File
from agno.models.message import Message
from agno.models.metrics import Metrics
from agno.models.vertexai.claude import Claude as VertexAIClaude
from agno.run.agent import (
    ModelRequestCompletedEvent,
    RunCancelledEvent,
    RunCompletedEvent,
    RunContentEvent,
    RunErrorEvent,
)
from agno.run.base import RunStatus
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom.agents import create_session_storage
from mindroom.ai import (
    PreparedAgentRun,
    _prepare_agent_and_prompt,
    ai_response,
    append_inline_media_fallback_prompt,
    build_matrix_run_metadata,
    should_retry_without_inline_media,
    stream_agent_response,
)
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import DebugConfig, ModelConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import (
    MATRIX_EVENT_ID_METADATA_KEY,
    MATRIX_SEEN_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY,
    ROUTER_AGENT_NAME,
    RuntimePaths,
    resolve_runtime_paths,
)
from mindroom.conversation_state_writer import ConversationStateWriter, ConversationStateWriterDeps
from mindroom.delivery_gateway import DeliveryResult
from mindroom.history import PreparedHistoryState
from mindroom.history.types import HistoryScope
from mindroom.hooks import (
    BUILTIN_EVENT_NAMES,
    EVENT_SESSION_STARTED,
    EnrichmentItem,
    HookContextSupport,
    HookRegistry,
    SessionHookContext,
    hook,
)
from mindroom.hooks.registry import HookRegistryState
from mindroom.hooks.types import RESERVED_EVENT_NAMESPACES, default_timeout_ms_for_event, validate_event_name
from mindroom.llm_request_logging import install_llm_request_logging
from mindroom.matrix.identity import MatrixID
from mindroom.media_inputs import MediaInputs
from mindroom.memory import MemoryPromptParts
from mindroom.message_target import MessageTarget
from mindroom.post_response_effects import PostResponseEffectsSupport
from mindroom.response_runner import (
    ResponseRequest,
    ResponseRunner,
    ResponseRunnerDeps,
    prepare_memory_and_model_context,
)
from mindroom.streaming import StreamingDeliveryError
from mindroom.tool_system.runtime_context import (
    LiveToolDispatchContext,
    ToolRuntimeSupport,
    get_tool_runtime_context,
    tool_runtime_context,
)
from mindroom.tool_system.worker_routing import (
    build_tool_execution_identity,
    get_tool_execution_identity,
    stream_with_tool_execution_identity,
    tool_execution_identity,
)
from tests.conftest import (
    bind_runtime_paths,
    make_event_cache_mock,
    resolve_response_thread_root_for_test,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path


def _runtime_paths(tmp_path: Path, *, config_path: Path | None = None) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=config_path or tmp_path / "config.yaml",
        storage_path=tmp_path,
    )


def _config() -> Config:
    return Config(
        agents={"general": AgentConfig(display_name="General")},
        models={"default": ModelConfig(provider="openai", id="test-model")},
    )


def _config_with_team() -> Config:
    return Config(
        agents={"general": AgentConfig(display_name="General")},
        teams={
            "ultimate": TeamConfig(
                display_name="Ultimate",
                role="Coordinate the team",
                agents=["general"],
                mode="coordinate",
            ),
        },
        models={"default": ModelConfig(provider="openai", id="test-model")},
    )


def _prepared_prompt_result(
    agent: object,
    *,
    prompt: str = "test prompt",
) -> PreparedAgentRun:
    return PreparedAgentRun(
        agent=agent,
        messages=(Message(role="user", content=prompt),),
        unseen_event_ids=[],
        prepared_history=PreparedHistoryState(),
    )


class _SessionStorage:
    def __init__(self, session: AgentSession | TeamSession | None = None) -> None:
        self._session = deepcopy(session)

    @property
    def session(self) -> AgentSession | TeamSession | None:
        return deepcopy(self._session)

    @session.setter
    def session(self, session: AgentSession | TeamSession | None) -> None:
        self._session = deepcopy(session)

    def open(self) -> _SessionStorageView:
        return _SessionStorageView(self)


class _SessionStorageView:
    def __init__(self, store: _SessionStorage) -> None:
        self._store = store

    def get_session(self, session_id: str, _session_type: object) -> AgentSession | TeamSession | None:
        session = self._store.session
        if session is None or session.session_id != session_id:
            return None
        return session

    def upsert_session(self, session: AgentSession | TeamSession) -> None:
        self._store.session = session

    def close(self) -> None:
        return None


def _plugin(name: str, callbacks: list[object]) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        discovered_hooks=tuple(callbacks),
        entry_config=PluginEntryConfig(path=f"./plugins/{name}"),
        plugin_order=0,
    )


def _make_bot(
    tmp_path: Path,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str = "general",
) -> MagicMock:
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = agent_name
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = SimpleNamespace(for_agent=MagicMock(return_value=None))
    bot._knowledge_access_support.for_agent = MagicMock(return_value=None)
    bot._send_response = AsyncMock(return_value="$response_id")
    bot._handle_interactive_question = AsyncMock()
    return bot


def _team_orchestrator(config: Config, runtime_paths: RuntimePaths) -> SimpleNamespace:
    matrix_admin = object()
    return SimpleNamespace(
        config=config,
        runtime_paths=runtime_paths,
        _hook_matrix_admin=lambda: matrix_admin,
        _hook_room_state_querier=lambda: None,
        _hook_room_state_putter=lambda: None,
    )


def _build_response_runner(
    bot: MagicMock,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    storage_path: Path,
    requester_id: str,  # noqa: ARG001
    hook_registry: HookRegistry | None = None,
    history_storage: object | None = None,
    team_history_storage: object | None = None,
    message_target: MessageTarget | None = None,
    orchestrator: object | None = None,
) -> ResponseRunner:
    """Build a real response runner for one bot-shaped test double."""

    def _open_test_storage(storage: object | None) -> object:
        if isinstance(storage, _SessionStorage):
            return storage.open()
        return storage if storage is not None else MagicMock()

    bot.matrix_id = MagicMock(full_id="@mindroom_general:localhost", domain="localhost")
    bot.enable_streaming = True
    bot.show_tool_calls = False
    bot.orchestrator = orchestrator
    bot._conversation_resolver = MagicMock()
    bot._conversation_resolver.build_message_target = MagicMock(
        return_value=message_target or MessageTarget.resolve("!test:localhost", None, "$user_msg", room_mode=True),
    )
    bot._conversation_resolver.fetch_thread_history = AsyncMock(return_value=())
    bot._conversation_resolver.resolve_response_thread_root = MagicMock(
        side_effect=resolve_response_thread_root_for_test,
    )
    bot._conversation_state_writer = MagicMock()
    bot._conversation_state_writer.create_storage = MagicMock(
        side_effect=lambda *_args, **kwargs: _open_test_storage(
            team_history_storage
            if isinstance(kwargs.get("scope"), HistoryScope) and kwargs["scope"].kind == "team"
            else history_storage,
        ),
    )
    bot._conversation_state_writer.persist_response_event_id_in_session_run = MagicMock()
    bot._conversation_state_writer.history_scope = MagicMock(
        return_value=HistoryScope(
            kind="team" if bot.agent_name in config.teams else "agent",
            scope_id=bot.agent_name,
        ),
    )
    bot._conversation_state_writer.team_history_scope = MagicMock(
        side_effect=lambda team_agents: HistoryScope(
            kind="team",
            scope_id=bot.agent_name
            if bot.agent_name in config.teams
            else f"team_{'+'.join(sorted(mid.agent_name(config, runtime_paths) or mid.username for mid in team_agents))}",
        ),
    )
    bot._conversation_state_writer.session_type_for_scope = MagicMock(
        side_effect=lambda scope: SessionType.TEAM if scope.kind == "team" else SessionType.AGENT,
    )
    bot._edit_message = AsyncMock(return_value=True)
    delivery_gateway = MagicMock()
    delivery_gateway.deliver_final = AsyncMock(
        return_value=MagicMock(
            event_id="$response_id",
            response_text="Hello!",
            delivery_kind="sent",
        ),
    )
    delivery_gateway.deliver_stream = AsyncMock(return_value=("$msg_id", "Hello!"))
    delivery_gateway.edit_text = AsyncMock()
    delivery_gateway.send_text = AsyncMock(return_value="$thinking")
    delivery_gateway.finalize_streamed_response = AsyncMock(
        return_value=MagicMock(
            event_id="$response_id",
            response_text="Hello!",
            delivery_kind="sent",
        ),
    )
    delivery_gateway.deps = SimpleNamespace(
        response_hooks=SimpleNamespace(emit_cancelled_response=AsyncMock()),
    )
    runtime = SimpleNamespace(
        client=bot.client,
        config=config,
        enable_streaming=bot.enable_streaming,
        orchestrator=bot.orchestrator,
        event_cache=make_event_cache_mock(),
    )
    hook_context = HookContextSupport(
        runtime=runtime,
        logger=bot.logger,
        runtime_paths=runtime_paths,
        agent_name=bot.agent_name,
        hook_registry_state=HookRegistryState(hook_registry or HookRegistry.empty()),
        hook_send_message=AsyncMock(),
    )
    tool_runtime = ToolRuntimeSupport(
        runtime=runtime,
        logger=bot.logger,
        runtime_paths=runtime_paths,
        storage_path=storage_path,
        agent_name=bot.agent_name,
        matrix_id=bot.matrix_id,
        resolver=bot._conversation_resolver,
        hook_context=hook_context,
    )

    post_response_effects = PostResponseEffectsSupport(
        runtime=runtime,
        logger=bot.logger,
        runtime_paths=runtime_paths,
        delivery_gateway=delivery_gateway,
        conversation_cache=bot._conversation_resolver.deps.conversation_cache,
    )
    bot._knowledge_access_support = SimpleNamespace(for_agent=MagicMock(return_value=None))

    return ResponseRunner(
        ResponseRunnerDeps(
            runtime=runtime,
            logger=bot.logger,
            stop_manager=bot.stop_manager,
            runtime_paths=runtime_paths,
            storage_path=storage_path,
            agent_name=bot.agent_name,
            matrix_full_id=bot.matrix_id.full_id,
            resolver=bot._conversation_resolver,
            tool_runtime=tool_runtime,
            knowledge_access=bot._knowledge_access_support,
            delivery_gateway=delivery_gateway,
            post_response_effects=post_response_effects,
            state_writer=bot._conversation_state_writer,
        ),
    )


def _response_request(
    *,
    room_id: str = "!test:localhost",
    reply_to_event_id: str = "$user_msg",
    thread_id: str | None = None,
    prompt: str = "Hello",
    model_prompt: str | None = None,
    user_id: str | None = None,
) -> ResponseRequest:
    """Build one response request for direct bot seam tests."""
    return ResponseRequest(
        room_id=room_id,
        reply_to_event_id=reply_to_event_id,
        thread_id=thread_id,
        thread_history=(),
        prompt=prompt,
        model_prompt=model_prompt,
        user_id=user_id,
    )


def test_session_started_event_is_registered() -> None:
    """session:started should be a built-in event with the expected default timeout."""
    assert EVENT_SESSION_STARTED in BUILTIN_EVENT_NAMES
    assert validate_event_name(EVENT_SESSION_STARTED) == EVENT_SESSION_STARTED
    assert "session" in RESERVED_EVENT_NAMESPACES
    assert default_timeout_ms_for_event(EVENT_SESSION_STARTED) == 5000


@pytest.mark.asyncio
async def test_process_and_respond_emits_session_started_after_first_persisted_thread_response(
    tmp_path: Path,
) -> None:
    """The first persisted thread response should emit session:started before delivery finalization."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = "general"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = SimpleNamespace(for_agent=MagicMock(return_value=None))
    bot._knowledge_access_support.for_agent = MagicMock(return_value=None)
    bot._send_response = AsyncMock(return_value="$response_id")

    storage = _SessionStorage()
    sequence: list[tuple[str, str | None, str | None, str | None]] = []
    saw_matrix_admin: list[bool] = []

    @hook(EVENT_SESSION_STARTED, priority=10)
    async def first(ctx: SessionHookContext) -> None:
        saw_matrix_admin.append(ctx.matrix_admin is not None)
        sequence.append(("first", ctx.scope.key, ctx.session_id, ctx.thread_id))

    @hook(EVENT_SESSION_STARTED, priority=20)
    async def second(ctx: SessionHookContext) -> None:
        sequence.append(("second", ctx.scope.key, None, None))

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [first, second])])

    with (
        patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=SimpleNamespace(
                _hook_matrix_admin=MagicMock(return_value=object()),
                _hook_room_state_querier=MagicMock(return_value=None),
                _hook_room_state_putter=MagicMock(return_value=None),
            ),
        )

        async def fake_ai_response(*_args: object, **_kwargs: object) -> str:
            context = get_tool_runtime_context()
            assert context is not None
            storage.session = AgentSession(
                session_id=context.session_id or "",
                agent_id="general",
                created_at=1,
                updated_at=1,
            )
            sequence.append(("ai", context.session_id, None, None))
            return "Hello!"

        mock_ai.side_effect = fake_ai_response
        coordinator.deps.delivery_gateway.deliver_final.side_effect = AsyncMock(
            side_effect=lambda *_args, **_kwargs: sequence.append(("deliver", None, None, None))
            or MagicMock(
                event_id="$response_id",
                response_text="Hello!",
                delivery_kind="sent",
            ),
        )

        await coordinator.process_and_respond(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
        )
        await coordinator.process_and_respond(
            _response_request(prompt="Hello again", user_id="@alice:localhost", thread_id="$thread-root"),
        )

    assert sequence == [
        ("ai", "!test:localhost:$thread-root", None, None),
        ("first", "agent:general", "!test:localhost:$thread-root", "$thread-root"),
        ("second", "agent:general", None, None),
        ("deliver", None, None, None),
        ("ai", "!test:localhost:$thread-root", None, None),
        ("deliver", None, None, None),
    ]
    assert saw_matrix_admin == [True]


@pytest.mark.asyncio
async def test_process_and_respond_applies_session_started_agent_and_room_scopes(tmp_path: Path) -> None:
    """session:started hooks should respect agent and room decorator scopes."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = "general"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = SimpleNamespace(for_agent=MagicMock(return_value=None))
    bot._knowledge_access_support.for_agent = MagicMock(return_value=None)
    bot._send_response = AsyncMock(return_value="$response_id")

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED, agents=["general"], rooms=["!test:localhost"])
    async def matching(ctx: SessionHookContext) -> None:
        sequence.append(f"{ctx.scope.key}:{ctx.agent_name}:{ctx.room_id}:{ctx.thread_id}")

    @hook(EVENT_SESSION_STARTED, agents=["other"], rooms=["!test:localhost"])
    async def wrong_agent(ctx: SessionHookContext) -> None:
        sequence.append(f"wrong-agent:{ctx.agent_name}")

    @hook(EVENT_SESSION_STARTED, agents=["general"], rooms=["!elsewhere:localhost"])
    async def wrong_room(ctx: SessionHookContext) -> None:
        sequence.append(f"wrong-room:{ctx.room_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [matching, wrong_agent, wrong_room])])

    with (
        patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def fake_ai_response(*_args: object, **_kwargs: object) -> str:
            context = get_tool_runtime_context()
            assert context is not None
            storage.session = AgentSession(
                session_id=context.session_id or "",
                agent_id="general",
                created_at=1,
                updated_at=1,
            )
            sequence.append("ai")
            return "Hello!"

        mock_ai.side_effect = fake_ai_response

        await coordinator.process_and_respond(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
        )

    assert sequence == ["ai", "agent:general:general:!test:localhost:$thread-root"]


@pytest.mark.asyncio
async def test_process_and_respond_does_not_emit_session_started_without_persisted_session(tmp_path: Path) -> None:
    """session:started should not fire when the run never creates a persisted session."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = "general"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = SimpleNamespace(for_agent=MagicMock(return_value=None))
    bot._knowledge_access_support.for_agent = MagicMock(return_value=None)
    bot._send_response = AsyncMock(return_value="$response_id")

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(_ctx: SessionHookContext) -> None:
        sequence.append("started")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    with (
        patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def fake_ai_response(*_args: object, **_kwargs: object) -> str:
            sequence.append("ai")
            return "Hello!"

        mock_ai.side_effect = fake_ai_response

        await coordinator.process_and_respond(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
        )

    assert sequence == ["ai"]


@pytest.mark.asyncio
async def test_send_skill_command_response_emits_session_started_after_first_persisted_session(
    tmp_path: Path,
) -> None:
    """Skill-command replies should emit session:started when they create a new session."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.scope.key}:{ctx.session_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    with (
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock()),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )
        coordinator.deps.delivery_gateway.send_text = AsyncMock(
            side_effect=lambda request: sequence.append(f"send:{request.response_text}") or "$skill-reply",
        )

        async def fake_ai_response(*_args: object, **_kwargs: object) -> str:
            context = get_tool_runtime_context()
            assert context is not None
            storage.session = AgentSession(
                session_id=context.session_id or "",
                agent_id="general",
                created_at=1,
                updated_at=1,
            )
            sequence.append(f"ai:{context.session_id}")
            return "Skill response"

        mock_ai.side_effect = fake_ai_response

        event_id = await coordinator.send_skill_command_response(
            room_id="!test:localhost",
            reply_to_event_id="$user_msg",
            thread_id="$thread-root",
            thread_history=(),
            prompt="Use demo skill",
            agent_name="general",
            user_id="@alice:localhost",
        )

    assert event_id == "$skill-reply"
    assert sequence == [
        "ai:!test:localhost:$thread-root",
        "started:agent:general:!test:localhost:$thread-root:$thread-root",
        "send:Skill response",
    ]


@pytest.mark.asyncio
async def test_send_skill_command_response_uses_target_agent_storage_for_session_started(
    tmp_path: Path,
) -> None:
    """Router skill-command replies should probe the target agent session storage."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name=ROUTER_AGENT_NAME)

    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED, agents=["general"], rooms=["!test:localhost"])
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.scope.key}:{ctx.agent_name}:{ctx.session_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    with (
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock()),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )
        coordinator.deps = replace(
            coordinator.deps,
            state_writer=ConversationStateWriter(
                ConversationStateWriterDeps(
                    runtime=coordinator.deps.runtime,
                    logger=coordinator.deps.logger,
                    runtime_paths=runtime_paths,
                    agent_name=bot.agent_name,
                ),
            ),
        )
        coordinator.deps.delivery_gateway.send_text = AsyncMock(
            side_effect=lambda request: sequence.append(f"send:{request.response_text}") or "$skill-reply",
        )

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            context = get_tool_runtime_context()
            assert context is not None
            storage = create_session_storage(
                agent_name="general",
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=kwargs["execution_identity"],
            )
            try:
                storage.upsert_session(
                    AgentSession(
                        session_id=context.session_id or "",
                        agent_id="general",
                        created_at=1,
                        updated_at=1,
                    ),
                )
            finally:
                storage.close()
            sequence.append(f"ai:{context.session_id}")
            return "Skill response"

        mock_ai.side_effect = fake_ai_response

        event_id = await coordinator.send_skill_command_response(
            room_id="!test:localhost",
            reply_to_event_id="$user_msg",
            thread_id="$thread-root",
            thread_history=(),
            prompt="Use demo skill",
            agent_name="general",
            user_id="@alice:localhost",
        )

    assert event_id == "$skill-reply"
    assert sequence == [
        "ai:!test:localhost:$thread-root",
        "started:agent:general:general:!test:localhost:$thread-root:$thread-root",
        "send:Skill response",
    ]


@pytest.mark.asyncio
async def test_send_skill_command_response_passes_current_and_model_prompt_to_ai(
    tmp_path: Path,
) -> None:
    """Skill-command replies should preserve raw and expanded prompt layers separately."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai:
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )
        coordinator.deps.delivery_gateway.send_text = AsyncMock(return_value="$skill-reply")

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            assert kwargs["prompt"] == "Use demo skill"
            assert kwargs["model_prompt"] != "Use demo skill"
            assert "Use demo skill" in kwargs["model_prompt"]
            return "Skill response"

        mock_ai.side_effect = fake_ai_response

        event_id = await coordinator.send_skill_command_response(
            room_id="!test:localhost",
            reply_to_event_id="$user_msg",
            thread_id="$thread-root",
            thread_history=(),
            prompt="Use demo skill",
            agent_name="general",
            user_id="@alice:localhost",
        )

    assert event_id == "$skill-reply"


@pytest.mark.asyncio
async def test_send_skill_command_response_returns_event_id_after_post_effect_failure(
    tmp_path: Path,
) -> None:
    """Skill-command replies should preserve the visible event id after late post-effect failures."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with (
        patch("mindroom.response_runner.ai_response", new=AsyncMock(return_value="Skill response")),
        patch(
            "mindroom.response_lifecycle.apply_post_response_effects",
            new=AsyncMock(side_effect=RuntimeError("late boom")),
        ),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=HookRegistry.empty(),
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )
        coordinator.deps.delivery_gateway.send_text = AsyncMock(return_value="$skill-reply")

        event_id = await coordinator.send_skill_command_response(
            room_id="!test:localhost",
            reply_to_event_id="$user_msg",
            thread_id="$thread-root",
            thread_history=(),
            prompt="Use demo skill",
            agent_name="general",
            user_id="@alice:localhost",
        )

    assert event_id == "$skill-reply"
    coordinator.deps.logger.error.assert_called_once()
    assert coordinator.deps.logger.error.call_args.kwargs["response_kind"] == "skill_command"
    assert coordinator.deps.logger.error.call_args.kwargs["response_event_id"] == "$skill-reply"


@pytest.mark.asyncio
async def test_send_skill_command_response_passes_user_id_to_ai_response(tmp_path: Path) -> None:
    """Skill-command replies should preserve the Matrix sender on the ai_response path."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with (
        patch("mindroom.response_runner.ai_response", new=AsyncMock(return_value="Skill response")) as mock_ai,
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock()),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=HookRegistry.empty(),
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )
        coordinator.deps.delivery_gateway.send_text = AsyncMock(return_value="$skill-reply")

        event_id = await coordinator.send_skill_command_response(
            room_id="!test:localhost",
            reply_to_event_id="$user_msg",
            thread_id="$thread-root",
            thread_history=(),
            prompt="Use demo skill",
            agent_name="general",
            user_id="@alice:localhost",
        )

    assert event_id == "$skill-reply"
    assert mock_ai.await_args is not None
    assert mock_ai.await_args.kwargs["user_id"] == "@alice:localhost"


@pytest.mark.asyncio
async def test_should_watch_session_started_returns_false_when_storage_probe_fails(
    tmp_path: Path,
) -> None:
    """session:started eligibility should degrade to False when the session probe fails."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    @hook(EVENT_SESSION_STARTED)
    async def started(_ctx: SessionHookContext) -> None:
        return None

    class BrokenStorage:
        def get_session(self, _session_id: str, _session_type: object) -> AgentSession | TeamSession | None:
            msg = "probe boom"
            raise RuntimeError(msg)

        def close(self) -> None:
            return None

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])
    coordinator = _build_response_runner(
        bot,
        config=config,
        runtime_paths=runtime_paths,
        storage_path=tmp_path,
        requester_id="@alice:localhost",
        hook_registry=registry,
        message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
    )
    target = MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg")
    tool_context = coordinator.deps.tool_runtime.build_context(
        target,
        user_id="@alice:localhost",
        session_id=target.session_id,
    )

    should_watch = coordinator._should_watch_session_started(
        tool_context=tool_context,
        session_id=target.session_id,
        session_type=SessionType.AGENT,
        create_storage=BrokenStorage,
    )

    assert should_watch is False
    coordinator.deps.logger.exception.assert_called_once()
    assert coordinator.deps.logger.exception.call_args.kwargs["session_id"] == target.session_id
    assert coordinator.deps.logger.exception.call_args.kwargs["failure_reason"] == "probe boom"


@pytest.mark.asyncio
async def test_session_started_hooks_continue_after_timeout(tmp_path: Path) -> None:
    """A timed-out session hook should not block later session hooks or the response itself."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = "general"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = SimpleNamespace(for_agent=MagicMock(return_value=None))
    bot._knowledge_access_support.for_agent = MagicMock(return_value=None)
    bot._send_response = AsyncMock(return_value="$response_id")

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED, priority=10, timeout_ms=10)
    async def slow(_ctx: SessionHookContext) -> None:
        sequence.append("slow")
        await asyncio.sleep(0.05)

    @hook(EVENT_SESSION_STARTED, priority=20)
    async def fast(ctx: SessionHookContext) -> None:
        sequence.append(f"fast:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [slow, fast])])

    with (
        patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def fake_ai_response(*_args: object, **_kwargs: object) -> str:
            context = get_tool_runtime_context()
            assert context is not None
            storage.session = AgentSession(
                session_id=context.session_id or "",
                agent_id="general",
                created_at=1,
                updated_at=1,
            )
            sequence.append("ai")
            return "Hello!"

        mock_ai.side_effect = fake_ai_response

        await coordinator.process_and_respond(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
        )

    assert sequence == ["ai", "slow", "fast:$thread-root"]


@pytest.mark.asyncio
async def test_session_started_hooks_continue_after_runtime_error(tmp_path: Path) -> None:
    """A failed session hook should fail open and let later hooks and delivery finish."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED, priority=10)
    async def failing(_ctx: SessionHookContext) -> None:
        sequence.append("failed")
        msg = "hook failed"
        raise RuntimeError(msg)

    @hook(EVENT_SESSION_STARTED, priority=20)
    async def fast(ctx: SessionHookContext) -> None:
        sequence.append(f"fast:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [failing, fast])])

    with (
        patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )
        coordinator.deps.delivery_gateway.deliver_final.side_effect = AsyncMock(
            side_effect=lambda *_args, **_kwargs: sequence.append("deliver")
            or MagicMock(
                event_id="$response_id",
                response_text="Hello!",
                delivery_kind="sent",
            ),
        )

        async def fake_ai_response(*_args: object, **_kwargs: object) -> str:
            context = get_tool_runtime_context()
            assert context is not None
            storage.session = AgentSession(
                session_id=context.session_id or "",
                agent_id="general",
                created_at=1,
                updated_at=1,
            )
            sequence.append("ai")
            return "Hello!"

        mock_ai.side_effect = fake_ai_response

        await coordinator.process_and_respond(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
        )

    assert sequence == ["ai", "failed", "fast:$thread-root", "deliver"]


@pytest.mark.asyncio
async def test_process_and_respond_streaming_emits_session_started_after_persisted_delivery_error(
    tmp_path: Path,
) -> None:
    """session:started should still fire when streaming delivery fails after the session is persisted."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = "general"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = SimpleNamespace(for_agent=MagicMock(return_value=None))
    bot._knowledge_access_support.for_agent = MagicMock(return_value=None)
    bot._handle_interactive_question = AsyncMock()

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.session_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    with (
        patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.response_runner.stream_agent_response") as mock_stream,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@bob:localhost",
            hook_registry=registry,
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def consume_delivery_and_fail(request: object) -> tuple[str, str]:
            chunks = [chunk async for chunk in request.response_stream]
            accumulated = "".join(chunks)
            sequence.append(f"deliver:{accumulated}")
            raise StreamingDeliveryError(
                RuntimeError("boom"),
                event_id="$terminal",
                accumulated_text=accumulated,
                tool_trace=[],
            )

        coordinator.deps.delivery_gateway.deliver_stream.side_effect = consume_delivery_and_fail

        def fake_stream_agent_response(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            async def fake_stream() -> AsyncIterator[str]:
                context = get_tool_runtime_context()
                assert context is not None
                storage.session = AgentSession(
                    session_id=context.session_id or "",
                    agent_id="general",
                    created_at=1,
                    updated_at=1,
                )
                sequence.append("stream")
                yield "Hello!"

            return fake_stream()

        mock_stream.side_effect = fake_stream_agent_response

        delivery = await coordinator.process_and_respond_streaming(
            _response_request(prompt="Hello", user_id="@bob:localhost", thread_id="$thread-root"),
        )

    assert delivery.event_id == "$terminal"
    assert delivery.response_text == "Hello!"
    assert sequence == [
        "stream",
        "deliver:Hello!",
        "started:!test:localhost:$thread-root:$thread-root",
    ]


@pytest.mark.asyncio
async def test_process_and_respond_emits_session_started_after_persisted_cancellation(
    tmp_path: Path,
) -> None:
    """session:started should still fire when a cancelled run has already persisted the session."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = "general"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = SimpleNamespace(for_agent=MagicMock(return_value=None))
    bot._knowledge_access_support.for_agent = MagicMock(return_value=None)
    bot._send_response = AsyncMock(return_value="$response_id")

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.session_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    with (
        patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )
        coordinator.deps.delivery_gateway.edit_text = AsyncMock()

        async def fake_ai_response(*_args: object, **_kwargs: object) -> str:
            cancel_message = "cancel"
            context = get_tool_runtime_context()
            assert context is not None
            storage.session = AgentSession(
                session_id=context.session_id or "",
                agent_id="general",
                created_at=1,
                updated_at=1,
            )
            sequence.append("ai")
            raise asyncio.CancelledError(cancel_message)

        mock_ai.side_effect = fake_ai_response

        with pytest.raises(asyncio.CancelledError, match="cancel"):
            await coordinator.process_and_respond(
                replace(
                    _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
                    existing_event_id="$thinking",
                ),
            )

    assert sequence == [
        "ai",
        "started:!test:localhost:$thread-root:$thread-root",
    ]


@pytest.mark.asyncio
async def test_process_and_respond_streaming_emits_session_started_after_persisted_cancellation(
    tmp_path: Path,
) -> None:
    """session:started should still fire when streamed delivery is cancelled after persistence."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = "general"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = SimpleNamespace(for_agent=MagicMock(return_value=None))
    bot._knowledge_access_support.for_agent = MagicMock(return_value=None)
    bot._handle_interactive_question = AsyncMock()

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.session_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    with (
        patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.response_runner.stream_agent_response") as mock_stream,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@bob:localhost",
            hook_registry=registry,
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def consume_delivery_and_cancel(request: object) -> tuple[str, str]:
            accumulated = ""
            async for chunk in request.response_stream:
                accumulated += str(chunk)
                sequence.append(f"deliver:{accumulated}")
            return "$msg_id", accumulated

        coordinator.deps.delivery_gateway.deliver_stream.side_effect = consume_delivery_and_cancel

        def fake_stream_agent_response(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            async def fake_stream() -> AsyncIterator[str]:
                cancel_message = "cancel"
                context = get_tool_runtime_context()
                assert context is not None
                storage.session = AgentSession(
                    session_id=context.session_id or "",
                    agent_id="general",
                    created_at=1,
                    updated_at=1,
                )
                sequence.append("stream")
                yield "Hello!"
                raise asyncio.CancelledError(cancel_message)

            return fake_stream()

        mock_stream.side_effect = fake_stream_agent_response

        with pytest.raises(asyncio.CancelledError, match="cancel"):
            await coordinator.process_and_respond_streaming(
                replace(
                    _response_request(prompt="Hello", user_id="@bob:localhost", thread_id="$thread-root"),
                    existing_event_id="$thinking",
                ),
            )

    assert sequence == [
        "stream",
        "deliver:Hello!",
        "started:!test:localhost:$thread-root:$thread-root",
    ]


@pytest.mark.asyncio
async def test_process_and_respond_uses_resolved_thread_id_for_ai_logging_context(
    tmp_path: Path,
) -> None:
    """Non-streaming AI calls should receive the resolved thread root, not the raw request thread id."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with (
        patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
        )

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            assert kwargs["thread_id"] == "$resolved-thread"
            return "Hello!"

        mock_ai.side_effect = fake_ai_response

        request = replace(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$raw-thread"),
            target=MessageTarget.resolve("!test:localhost", "$resolved-thread", "$user_msg"),
        )
        await coordinator.process_and_respond(request)


@pytest.mark.asyncio
async def test_process_and_respond_streaming_uses_resolved_thread_id_for_ai_logging_context(
    tmp_path: Path,
) -> None:
    """Streaming AI calls should receive the resolved thread root, not the raw request thread id."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with (
        patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.response_runner.stream_agent_response") as mock_stream,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
        )

        def fake_stream_agent_response(*_args: object, **kwargs: object) -> AsyncIterator[str]:
            assert kwargs["thread_id"] == "$resolved-thread"

            async def fake_stream() -> AsyncIterator[str]:
                yield "Hello!"

            return fake_stream()

        mock_stream.side_effect = fake_stream_agent_response

        request = replace(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$raw-thread"),
            target=MessageTarget.resolve("!test:localhost", "$resolved-thread", "$user_msg"),
        )
        await coordinator.process_and_respond_streaming(request)


@pytest.mark.asyncio
async def test_process_and_respond_passes_current_and_model_prompt_to_ai(
    tmp_path: Path,
) -> None:
    """Non-streaming AI calls should preserve raw and expanded prompt layers separately."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with (
        patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
        )

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            assert kwargs["prompt"] == "Hello"
            assert kwargs["model_prompt"] == "Hello with context"
            return "Hello!"

        mock_ai.side_effect = fake_ai_response

        await coordinator.process_and_respond(
            _response_request(
                prompt="Hello",
                model_prompt="Hello with context",
                user_id="@alice:localhost",
                thread_id="$thread-root",
            ),
        )


@pytest.mark.asyncio
async def test_process_and_respond_streaming_passes_current_and_model_prompt_to_ai(
    tmp_path: Path,
) -> None:
    """Streaming AI calls should preserve raw and expanded prompt layers separately."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with (
        patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.response_runner.stream_agent_response") as mock_stream,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
        )

        def fake_stream_agent_response(*_args: object, **kwargs: object) -> AsyncIterator[str]:
            assert kwargs["prompt"] == "Hello"
            assert kwargs["model_prompt"] == "Hello with context"

            async def fake_stream() -> AsyncIterator[str]:
                yield "Hello!"

            return fake_stream()

        mock_stream.side_effect = fake_stream_agent_response

        await coordinator.process_and_respond_streaming(
            _response_request(
                prompt="Hello",
                model_prompt="Hello with context",
                user_id="@alice:localhost",
                thread_id="$thread-root",
            ),
        )


@pytest.mark.asyncio
async def test_generate_response_locked_sets_failure_reason_for_plain_streaming_exception(
    tmp_path: Path,
) -> None:
    """Plain streaming exceptions should propagate their text to message:cancelled."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with (
        patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@bob:localhost",
            hook_registry=HookRegistry.empty(),
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )
        coordinator.generate_streaming_ai_response = AsyncMock(side_effect=RuntimeError("plain boom"))

        event_id = await coordinator.generate_response_locked(
            _response_request(prompt="Hello", user_id="@bob:localhost", thread_id="$thread-root"),
            resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

    assert event_id is None
    coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.assert_awaited_once()
    assert (
        coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.await_args.kwargs[
            "visible_response_event_id"
        ]
        == "$thinking"
    )
    assert (
        coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.await_args.kwargs[
            "failure_reason"
        ]
        == "plain boom"
    )


@pytest.mark.asyncio
async def test_generate_team_response_helper_streaming_emits_session_started_after_persisted_delivery_error(
    tmp_path: Path,
) -> None:
    """session:started should still fire for team streams that fail after persisting the session."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = "ultimate"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = SimpleNamespace(for_agent=MagicMock(return_value=None))
    bot._knowledge_access_support.for_agent = MagicMock(return_value=None)

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.scope.key}:{ctx.session_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_runner.team_response_stream") as mock_team_stream,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            team_history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=_team_orchestrator(config, runtime_paths),
        )

        async def consume_delivery_and_fail(request: object) -> tuple[str, str]:
            chunks = [chunk async for chunk in request.response_stream]
            accumulated = "".join(chunks)
            sequence.append(f"deliver:{accumulated}")
            raise StreamingDeliveryError(
                RuntimeError("boom"),
                event_id="$team-terminal",
                accumulated_text=accumulated,
                tool_trace=[],
            )

        coordinator.deps.delivery_gateway.deliver_stream.side_effect = consume_delivery_and_fail

        def fake_team_response_stream(*_args: object, **kwargs: object) -> AsyncIterator[str]:
            async def fake_stream() -> AsyncIterator[str]:
                session_id = kwargs["session_id"]
                assert isinstance(session_id, str)
                storage.session = TeamSession(
                    session_id=session_id,
                    team_id="ultimate",
                    created_at=1,
                    updated_at=1,
                )
                sequence.append("stream")
                yield "Team hello"

            return fake_stream()

        mock_team_stream.side_effect = fake_team_response_stream
        request = replace(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            existing_event_id="$placeholder",
            existing_event_is_placeholder=True,
        )

        event_id = await coordinator.generate_team_response_helper(
            request,
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    assert event_id == "$team-terminal"
    assert sequence == [
        "stream",
        "deliver:Team hello",
        "started:team:ultimate:!test:localhost:$thread-root:$thread-root",
    ]


@pytest.mark.asyncio
async def test_generate_team_response_helper_emits_session_started_after_persisted_cancellation(
    tmp_path: Path,
) -> None:
    """session:started should still fire when a cancelled team run has already persisted the session."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = "ultimate"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = SimpleNamespace(for_agent=MagicMock(return_value=None))
    bot._knowledge_access_support.for_agent = MagicMock(return_value=None)

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.scope.key}:{ctx.session_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = cast("Callable[[str | None], Awaitable[object]]", kwargs["response_function"])
        with suppress(asyncio.CancelledError):
            await response_function("$thinking")
        return "$thinking"

    with (
        patch.object(
            ResponseRunner,
            "run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.team_response", new_callable=AsyncMock) as mock_team_response,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            team_history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=_team_orchestrator(config, runtime_paths),
        )
        coordinator.deps.delivery_gateway.edit_text = AsyncMock()

        async def fake_team_response(*_args: object, **kwargs: object) -> str:
            cancel_message = "cancel"
            session_id = kwargs["session_id"]
            assert isinstance(session_id, str)
            storage.session = TeamSession(
                session_id=session_id,
                team_id="ultimate",
                created_at=1,
                updated_at=1,
            )
            sequence.append("team")
            raise asyncio.CancelledError(cancel_message)

        mock_team_response.side_effect = fake_team_response

        event_id = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    assert event_id is None
    assert sequence == [
        "team",
        "started:team:ultimate:!test:localhost:$thread-root:$thread-root",
    ]


@pytest.mark.asyncio
async def test_generate_team_response_helper_streaming_emits_session_started_after_persisted_cancellation(
    tmp_path: Path,
) -> None:
    """session:started should still fire when a cancelled team stream has already persisted the session."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = "ultimate"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = SimpleNamespace(for_agent=MagicMock(return_value=None))
    bot._knowledge_access_support.for_agent = MagicMock(return_value=None)

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.scope.key}:{ctx.session_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = cast("Callable[[str | None], Awaitable[object]]", kwargs["response_function"])
        with suppress(asyncio.CancelledError):
            await response_function("$thinking")
        return "$thinking"

    with (
        patch.object(
            ResponseRunner,
            "run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_runner.team_response_stream") as mock_team_stream,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            team_history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=_team_orchestrator(config, runtime_paths),
        )

        async def consume_delivery_and_cancel(request: object) -> tuple[str, str]:
            accumulated = ""
            async for chunk in request.response_stream:
                accumulated += str(chunk)
                sequence.append(f"deliver:{accumulated}")
            return "$team-msg", accumulated

        coordinator.deps.delivery_gateway.deliver_stream.side_effect = consume_delivery_and_cancel

        def fake_team_response_stream(*_args: object, **kwargs: object) -> AsyncIterator[str]:
            async def fake_stream() -> AsyncIterator[str]:
                cancel_message = "cancel"
                session_id = kwargs["session_id"]
                assert isinstance(session_id, str)
                storage.session = TeamSession(
                    session_id=session_id,
                    team_id="ultimate",
                    created_at=1,
                    updated_at=1,
                )
                sequence.append("stream")
                yield "Team hello"
                raise asyncio.CancelledError(cancel_message)

            return fake_stream()

        mock_team_stream.side_effect = fake_team_response_stream

        event_id = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    assert event_id is None
    assert sequence == [
        "stream",
        "deliver:Team hello",
        "started:team:ultimate:!test:localhost:$thread-root:$thread-root",
    ]


@pytest.mark.asyncio
async def test_generate_team_response_helper_uses_persisted_team_scope_for_session_started_hooks(
    tmp_path: Path,
) -> None:
    """Ad hoc team session hooks should scope to the persisted team scope, not the router bot."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name=ROUTER_AGENT_NAME)

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED, agents=["team_general"], rooms=["!test:localhost"])
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.scope.key}:{ctx.agent_name}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.team_response", new_callable=AsyncMock) as mock_team_response,
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            team_history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=_team_orchestrator(config, runtime_paths),
        )

        async def fake_team_response(*_args: object, **kwargs: object) -> str:
            session_id = kwargs["session_id"]
            assert isinstance(session_id, str)
            storage.session = TeamSession(
                session_id=session_id,
                team_id="team_general",
                created_at=1,
                updated_at=1,
            )
            return "Team hello"

        mock_team_response.side_effect = fake_team_response

        await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    assert sequence == ["started:team:team_general:team_general"]


@pytest.mark.asyncio
async def test_generate_team_response_helper_merges_raw_prompt_into_model_prompt(
    tmp_path: Path,
) -> None:
    """Ad hoc team responses should keep the user request when model_prompt only adds metadata."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name=ROUTER_AGENT_NAME)

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.team_response", new_callable=AsyncMock) as mock_team_response,
    ):
        mock_team_response.return_value = "Team hello"
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=_team_orchestrator(config, runtime_paths),
        )

        event_id = await coordinator.generate_team_response_helper(
            _response_request(
                prompt="What is in the image?",
                model_prompt="Available attachment IDs: att_img. Use tool calls to inspect or process them.",
                user_id="@alice:localhost",
                thread_id="$thread-root",
            ),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    assert event_id == "$response_id"
    assert mock_team_response.await_args is not None
    message = mock_team_response.await_args.kwargs["message"]
    assert "What is in the image?" in message
    assert "Available attachment IDs: att_img. Use tool calls to inspect or process them." in message


@pytest.mark.asyncio
async def test_generate_team_response_helper_uses_delivery_result_failure_reason_for_cancelled_stream(
    tmp_path: Path,
) -> None:
    """Team cancelled hooks should fall back to DeliveryResult.failure_reason when no exception was raised."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")

    storage = _SessionStorage()

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_runner.team_response_stream") as mock_team_stream,
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=HookRegistry.empty(),
            team_history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=_team_orchestrator(config, runtime_paths),
        )
        coordinator.deps.delivery_gateway.deliver_stream = AsyncMock(return_value=("$team-msg", "Team hello"))
        coordinator.deps.delivery_gateway.finalize_streamed_response = AsyncMock(
            return_value=DeliveryResult(
                event_id=None,
                response_text="Team hello",
                delivery_kind=None,
                failure_reason="stream failure",
            ),
        )

        def fake_team_response_stream(*_args: object, **kwargs: object) -> AsyncIterator[str]:
            async def fake_stream() -> AsyncIterator[str]:
                session_id = kwargs["session_id"]
                assert isinstance(session_id, str)
                storage.session = TeamSession(
                    session_id=session_id,
                    team_id="ultimate",
                    created_at=1,
                    updated_at=1,
                )
                yield "Team hello"

            return fake_stream()

        mock_team_stream.side_effect = fake_team_response_stream

        event_id = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    assert event_id is None
    coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.assert_awaited_once()
    assert (
        coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.await_args.kwargs[
            "failure_reason"
        ]
        == "stream failure"
    )
    assert (
        coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.await_args.kwargs[
            "visible_response_event_id"
        ]
        == "$team-msg"
    )


@pytest.mark.asyncio
async def test_send_skill_command_response_locked_emits_session_started_after_persisted_cancellation(
    tmp_path: Path,
) -> None:
    """session:started should still fire when a cancelled skill-command run has already persisted the session."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = "general"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = SimpleNamespace(for_agent=MagicMock(return_value=None))
    bot._knowledge_access_support.for_agent = MagicMock(return_value=None)

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.scope.key}:{ctx.session_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    with (
        patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def fake_ai_response(*_args: object, **_kwargs: object) -> str:
            cancel_message = "cancel"
            context = get_tool_runtime_context()
            assert context is not None
            storage.session = AgentSession(
                session_id=context.session_id or "",
                agent_id="general",
                created_at=1,
                updated_at=1,
            )
            sequence.append("ai")
            raise asyncio.CancelledError(cancel_message)

        mock_ai.side_effect = fake_ai_response

        with pytest.raises(asyncio.CancelledError, match="cancel"):
            await coordinator.send_skill_command_response_locked(
                room_id="!test:localhost",
                reply_to_event_id="$user_msg",
                thread_id="$thread-root",
                thread_history=(),
                prompt="Hello",
                agent_name="general",
                user_id="@alice:localhost",
            )

    assert sequence == [
        "ai",
        "started:agent:general:!test:localhost:$thread-root:$thread-root",
    ]


class TestUserIdPassthrough:
    """Test that user_id reaches agent.arun() in both streaming and non-streaming paths."""

    def test_prepare_memory_and_model_context_keeps_raw_prompt_when_model_prompt_only_contains_substring(
        self,
        tmp_path: Path,
    ) -> None:
        """Short prompts must not disappear when they happen to occur inside attachment IDs."""
        config = _config()
        runtime_paths = _runtime_paths(tmp_path)

        memory_prompt, memory_thread_history, model_prompt, model_thread_history = prepare_memory_and_model_context(
            "report",
            [],
            config=config,
            runtime_paths=runtime_paths,
            model_prompt="Available attachment IDs: att_report. Use tool calls to inspect or process them.",
        )

        assert memory_prompt == "report"
        assert memory_thread_history == []
        assert model_thread_history == []
        assert model_prompt.endswith(
            "report\n\nAvailable attachment IDs: att_report. Use tool calls to inspect or process them.",
        )

    def test_prepare_memory_and_model_context_keeps_existing_timestamped_merged_model_prompt(
        self,
        tmp_path: Path,
    ) -> None:
        """Pre-merged timestamped model prompts should not duplicate the raw prompt on reuse."""
        config = _config()
        runtime_paths = _runtime_paths(tmp_path)

        existing_model_prompt = "[2026-03-20 08:15 PDT] report\n\nAvailable attachment IDs: att_report."

        _memory_prompt, _memory_thread_history, model_prompt, _model_thread_history = prepare_memory_and_model_context(
            "report",
            [],
            config=config,
            runtime_paths=runtime_paths,
            model_prompt=existing_model_prompt,
        )

        assert model_prompt == existing_model_prompt

    @pytest.mark.asyncio
    async def test_non_streaming_passes_user_id(self, tmp_path: Path) -> None:
        """Test that _process_and_respond passes user_id through to ai_response."""
        runtime_paths = _runtime_paths(tmp_path)
        config = bind_runtime_paths(_config(), runtime_paths)
        bot = MagicMock(spec=AgentBot)
        bot.logger = MagicMock()
        bot.stop_manager = MagicMock()
        bot.stop_manager.remove_stop_button = AsyncMock()
        bot.client = AsyncMock()
        bot.agent_name = "general"
        bot.storage_path = tmp_path
        bot.config = config
        bot.runtime_paths = runtime_paths
        bot._knowledge_access_support = SimpleNamespace(for_agent=MagicMock(return_value=None))
        bot._knowledge_access_support.for_agent = MagicMock(return_value=None)
        bot._send_response = AsyncMock(return_value="$response_id")
        with (
            patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
            patch("mindroom.response_runner.ai_response") as mock_ai,
        ):
            coordinator = _build_response_runner(
                bot,
                config=config,
                runtime_paths=runtime_paths,
                storage_path=tmp_path,
                requester_id="@alice:localhost",
            )

            async def fake_ai_response(*_args: object, **_kwargs: object) -> str:
                context = get_tool_runtime_context()
                assert context is not None
                assert context.room_id == "!test:localhost"
                assert context.thread_id is None
                assert context.requester_id == "@alice:localhost"
                return "Hello!"

            mock_ai.side_effect = fake_ai_response

            await coordinator.process_and_respond(
                _response_request(prompt="Hello", user_id="@alice:localhost"),
            )

            mock_ai.assert_called_once()
            assert mock_ai.call_args.kwargs["user_id"] == "@alice:localhost"
            assert callable(mock_ai.call_args.kwargs["run_id_callback"])

    @pytest.mark.asyncio
    async def test_streaming_passes_user_id(self, tmp_path: Path) -> None:
        """Test that _process_and_respond_streaming passes user_id through to stream_agent_response."""
        runtime_paths = _runtime_paths(tmp_path)
        config = bind_runtime_paths(_config(), runtime_paths)
        bot = MagicMock(spec=AgentBot)
        bot.logger = MagicMock()
        bot.stop_manager = MagicMock()
        bot.stop_manager.remove_stop_button = AsyncMock()
        bot.client = AsyncMock()
        bot.agent_name = "general"
        bot.matrix_id = MagicMock()
        bot.matrix_id.domain = "localhost"
        bot.config = config
        bot.storage_path = tmp_path
        bot.runtime_paths = runtime_paths
        bot._knowledge_access_support = SimpleNamespace(for_agent=MagicMock(return_value=None))
        bot._knowledge_access_support.for_agent = MagicMock(return_value=None)
        bot._handle_interactive_question = AsyncMock()
        with (
            patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
            patch("mindroom.response_runner.stream_agent_response") as mock_stream,
        ):
            coordinator = _build_response_runner(
                bot,
                config=config,
                runtime_paths=runtime_paths,
                storage_path=tmp_path,
                requester_id="@bob:localhost",
            )

            async def consume_delivery(request: object) -> tuple[str, str]:
                response_stream = request.response_stream
                chunks = [chunk async for chunk in response_stream]
                return "$msg_id", "".join(chunks)

            coordinator.deps.delivery_gateway.deliver_stream.side_effect = consume_delivery

            def fake_stream_agent_response(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
                async def fake_stream() -> AsyncIterator[str]:
                    context = get_tool_runtime_context()
                    assert context is not None
                    assert context.room_id == "!test:localhost"
                    assert context.thread_id is None
                    assert context.requester_id == "@bob:localhost"
                    yield "Hello!"

                return fake_stream()

            mock_stream.side_effect = fake_stream_agent_response

            await coordinator.process_and_respond_streaming(
                _response_request(prompt="Hello", user_id="@bob:localhost"),
            )

            mock_stream.assert_called_once()
            assert mock_stream.call_args.kwargs["user_id"] == "@bob:localhost"
            assert callable(mock_stream.call_args.kwargs["run_id_callback"])

    @pytest.mark.asyncio
    async def test_streaming_tool_context_cleanup_survives_cross_task_close(self, tmp_path: Path) -> None:
        """Wrapped response streams should clean up across task-context boundaries."""
        runtime_paths = _runtime_paths(tmp_path)
        config = bind_runtime_paths(_config(), runtime_paths)
        bot = MagicMock(spec=AgentBot)
        bot.logger = MagicMock()
        bot.stop_manager = MagicMock()
        bot.stop_manager.remove_stop_button = AsyncMock()
        bot.client = AsyncMock()
        bot.agent_name = "general"
        bot.storage_path = tmp_path
        bot.config = config
        bot.runtime_paths = runtime_paths

        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
        )
        target = MessageTarget.resolve("!test:localhost", None, "$user_msg", room_mode=True)
        tool_context = coordinator.deps.tool_runtime.build_context(
            target,
            user_id="@alice:localhost",
            session_id="session-1",
        )
        assert tool_context is not None
        execution_identity = coordinator.deps.tool_runtime.build_execution_identity(
            target=target,
            user_id="@alice:localhost",
            session_id="session-1",
        )
        observed_final_contexts: list[tuple[object | None, object | None]] = []

        async def source() -> AsyncIterator[str]:
            try:
                assert get_tool_runtime_context() is tool_context
                assert get_tool_execution_identity() == execution_identity
                yield "chunk"
                await asyncio.Future()
            finally:
                observed_final_contexts.append(
                    (get_tool_runtime_context(), get_tool_execution_identity()),
                )

        stream = coordinator._stream_in_tool_context(
            tool_dispatch=LiveToolDispatchContext.from_runtime_context(tool_context),
            stream_factory=source,
        )

        first_chunk = await asyncio.create_task(anext(stream), context=Context())
        assert first_chunk == "chunk"
        await asyncio.create_task(stream.aclose(), context=Context())
        assert observed_final_contexts == [(tool_context, execution_identity)]

    @pytest.mark.asyncio
    async def test_execution_identity_stream_factory_masks_outer_context(self, tmp_path: Path) -> None:
        """Factory setup should not inherit an outer execution identity when None is explicit."""
        runtime_paths = _runtime_paths(tmp_path)
        outer_identity = build_tool_execution_identity(
            channel="matrix",
            agent_name="outer",
            runtime_paths=runtime_paths,
            requester_id="@outer:localhost",
            room_id="!test:localhost",
            thread_id=None,
            resolved_thread_id=None,
            session_id="outer-session",
        )
        observed_identity: list[object | None] = []

        def factory() -> AsyncIterator[str]:
            observed_identity.append(get_tool_execution_identity())
            msg = "factory boom"
            raise RuntimeError(msg)

        with tool_execution_identity(outer_identity):
            stream = stream_with_tool_execution_identity(None, stream_factory=factory)
            with pytest.raises(RuntimeError, match="factory boom"):
                await anext(stream)

        assert observed_identity == [None]

    @pytest.mark.asyncio
    async def test_tool_runtime_stream_factory_masks_outer_context(self, tmp_path: Path) -> None:
        """Factory setup should not inherit an outer tool runtime context when None is explicit."""
        runtime_paths = _runtime_paths(tmp_path)
        config = bind_runtime_paths(_config(), runtime_paths)
        bot = MagicMock(spec=AgentBot)
        bot.logger = MagicMock()
        bot.stop_manager = MagicMock()
        bot.stop_manager.remove_stop_button = AsyncMock()
        bot.client = AsyncMock()
        bot.agent_name = "general"
        bot.storage_path = tmp_path
        bot.config = config
        bot.runtime_paths = runtime_paths

        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
        )
        target = MessageTarget.resolve("!test:localhost", None, "$user_msg", room_mode=True)
        outer_context = coordinator.deps.tool_runtime.build_context(
            target,
            user_id="@outer:localhost",
            session_id="outer-session",
        )
        assert outer_context is not None
        observed_context: list[object | None] = []

        def factory() -> AsyncIterator[str]:
            observed_context.append(get_tool_runtime_context())
            msg = "factory boom"
            raise RuntimeError(msg)

        with tool_runtime_context(outer_context):
            stream = coordinator.deps.tool_runtime.stream_in_context(
                tool_context=None,
                stream_factory=factory,
            )
            with pytest.raises(RuntimeError, match="factory boom"):
                await anext(stream)

        assert observed_context == [None]

    @pytest.mark.asyncio
    async def test_ai_response_passes_user_id_to_agent_arun(self, tmp_path: Path) -> None:
        """Test that ai_response passes user_id all the way to agent.arun()."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_run_output = MagicMock()
        mock_run_output.content = "Response"
        mock_run_output.tools = None
        mock_agent.arun = AsyncMock(return_value=mock_run_output)

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                user_id="@user:localhost",
            )

            mock_agent.arun.assert_called_once()
            assert mock_agent.arun.call_args.kwargs["user_id"] == "@user:localhost"

    @pytest.mark.asyncio
    async def test_ai_response_passes_run_id_to_agent_arun(self, tmp_path: Path) -> None:
        """Non-streaming cancellation needs an explicit run_id threaded to Agno."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_run_output = MagicMock()
        mock_run_output.content = "Response"
        mock_run_output.tools = None
        mock_agent.arun = AsyncMock(return_value=mock_run_output)

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                run_id="run-123",
            )

            mock_agent.arun.assert_called_once()
            assert mock_agent.arun.call_args.kwargs["run_id"] == "run-123"

    @pytest.mark.asyncio
    async def test_prepare_agent_and_prompt_threads_config_path_to_create_agent(self, tmp_path: Path) -> None:
        """The shared agent-build helper should preserve an explicit orchestrator config path."""
        config = _config()
        config_path = tmp_path / "custom-config.yaml"
        mock_agent = MagicMock()

        with (
            patch(
                "mindroom.ai.build_memory_prompt_parts",
                new_callable=AsyncMock,
                return_value=MemoryPromptParts(),
            ),
            patch("mindroom.ai.create_agent", return_value=mock_agent) as mock_create_agent,
        ):
            prepared_run = await _prepare_agent_and_prompt(
                agent_name="general",
                prompt="test",
                runtime_paths=_runtime_paths(tmp_path, config_path=config_path),
                config=config,
            )

        agent = prepared_run.agent
        full_prompt = prepared_run.prompt_text
        unseen_event_ids = prepared_run.unseen_event_ids
        prepared_history = prepared_run.prepared_history
        assert agent is mock_agent
        assert full_prompt == "test"
        assert unseen_event_ids == []
        assert prepared_history.compaction_outcomes == []
        assert prepared_history.replays_persisted_history is False
        assert prepared_history.replay_plan is not None
        assert prepared_history.replay_plan.mode == "configured"
        assert "runtime_paths" not in mock_create_agent.call_args.kwargs

    @pytest.mark.asyncio
    async def test_prepare_agent_and_prompt_uses_raw_prompt_for_memory_and_appends_additional_context(
        self,
        tmp_path: Path,
    ) -> None:
        """Raw prompt should drive memory lookup while session context appends to the system prompt."""
        config = _config()
        mock_agent = MagicMock()
        mock_agent.additional_context = "existing context"
        prepared_execution = SimpleNamespace(
            messages=(Message(role="user", content="prepared prompt"),),
            replay_plan=None,
            unseen_event_ids=[],
            replays_persisted_history=False,
            compaction_outcomes=[],
        )

        with (
            patch(
                "mindroom.ai.build_memory_prompt_parts",
                new_callable=AsyncMock,
                return_value=MemoryPromptParts(
                    session_preamble="session preamble",
                    turn_context="turn context",
                ),
            ) as mock_build_prompt_parts,
            patch("mindroom.ai.create_agent", return_value=mock_agent),
            patch("mindroom.ai._render_system_enrichment_context", return_value="system enrichment"),
            patch(
                "mindroom.ai.prepare_agent_execution_context",
                new=AsyncMock(return_value=prepared_execution),
            ) as mock_prepare_execution,
        ):
            prepared_run = await _prepare_agent_and_prompt(
                agent_name="general",
                prompt="raw prompt",
                runtime_paths=_runtime_paths(tmp_path),
                config=config,
                model_prompt="model metadata",
                system_enrichment_items=(EnrichmentItem(key="k", text="v", cache_policy="stable"),),
            )

        agent = prepared_run.agent
        full_prompt = prepared_run.prompt_text
        unseen_event_ids = prepared_run.unseen_event_ids
        prepared_history = prepared_run.prepared_history
        assert agent is mock_agent
        assert full_prompt == "prepared prompt"
        assert unseen_event_ids == []
        assert prepared_history.compaction_outcomes == []
        assert mock_build_prompt_parts.await_args is not None
        assert mock_build_prompt_parts.await_args.args[0] == "raw prompt"
        assert mock_prepare_execution.await_args is not None
        assert mock_prepare_execution.await_args.kwargs["prompt"] == "raw prompt\n\nturn context\n\nmodel metadata"
        assert mock_agent.additional_context == "existing context\n\nsystem enrichment\n\nsession preamble"

    @pytest.mark.asyncio
    async def test_ai_response_passes_config_path_to_prepare_agent(self, tmp_path: Path) -> None:
        """Non-streaming replies should build agents against the orchestrator-owned config file."""
        config_path = tmp_path / "custom-config.yaml"
        mock_agent = MagicMock()
        mock_run_output = MagicMock()
        mock_run_output.content = "Response"
        mock_run_output.tools = None

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.cached_agent_run", new_callable=AsyncMock, return_value=mock_run_output),
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path, config_path=config_path),
                config=_config(),
                include_openai_compat_guidance=True,
            )

        assert mock_prepare.call_args.args[2].config_path == config_path
        assert mock_prepare.await_args.kwargs["include_openai_compat_guidance"] is True

    @pytest.mark.asyncio
    async def test_ai_response_omits_current_sender_for_openai_compat_guidance(self, tmp_path: Path) -> None:
        """OpenAI-compatible requests should not reinterpret request-body user as a Matrix sender."""
        mock_agent = MagicMock()
        mock_run_output = MagicMock()
        mock_run_output.content = "Response"
        mock_run_output.tools = None

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.cached_agent_run", new_callable=AsyncMock, return_value=mock_run_output),
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                user_id="user-123",
                include_openai_compat_guidance=True,
            )

        assert mock_prepare.await_args.kwargs["current_sender_id"] is None
        assert mock_prepare.await_args.kwargs["include_openai_compat_guidance"] is True

    @pytest.mark.asyncio
    async def test_ai_response_passes_raw_prompt_separately_from_model_prompt(self, tmp_path: Path) -> None:
        """The AI entrypoint should preserve the raw user prompt when model_prompt is provided."""
        mock_agent = MagicMock()
        mock_run_output = MagicMock()
        mock_run_output.content = "Response"
        mock_run_output.tools = None

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.cached_agent_run", new_callable=AsyncMock, return_value=mock_run_output),
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            await ai_response(
                agent_name="general",
                prompt="raw prompt",
                model_prompt="model metadata",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
            )

        assert mock_prepare.await_args.args[1] == "raw prompt"
        assert mock_prepare.await_args.kwargs["model_prompt"] == "model metadata"

    @pytest.mark.asyncio
    async def test_stream_agent_response_passes_config_path_to_prepare_agent(self, tmp_path: Path) -> None:
        """Streaming replies should build agents against the orchestrator-owned config file."""
        config_path = tmp_path / "custom-config.yaml"
        mock_agent = MagicMock()

        async def _empty_stream() -> AsyncIterator[str]:
            if False:
                yield ""

        mock_agent.arun = MagicMock(return_value=_empty_stream())

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            _ = [
                chunk
                async for chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path, config_path=config_path),
                    config=_config(),
                    include_openai_compat_guidance=True,
                )
            ]

        assert mock_prepare.call_args.args[2].config_path == config_path
        assert mock_prepare.await_args.kwargs["include_openai_compat_guidance"] is True

    @pytest.mark.asyncio
    async def test_stream_agent_response_omits_current_sender_for_openai_compat_guidance(self, tmp_path: Path) -> None:
        """Streaming OpenAI-compatible requests should keep plain role-labeled prompt formatting."""
        mock_agent = MagicMock()

        async def _empty_stream() -> AsyncIterator[str]:
            if False:
                yield ""

        mock_agent.arun = MagicMock(return_value=_empty_stream())

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            _ = [
                chunk
                async for chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    user_id="user-123",
                    include_openai_compat_guidance=True,
                )
            ]

        assert mock_prepare.await_args.kwargs["current_sender_id"] is None
        assert mock_prepare.await_args.kwargs["include_openai_compat_guidance"] is True

    @pytest.mark.asyncio
    async def test_stream_agent_response_passes_raw_prompt_separately_from_model_prompt(
        self,
        tmp_path: Path,
    ) -> None:
        """Streaming should preserve the raw prompt when model_prompt is present."""
        mock_agent = MagicMock()

        async def _empty_stream() -> AsyncIterator[str]:
            if False:
                yield ""

        mock_agent.arun = MagicMock(return_value=_empty_stream())

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            _ = [
                chunk
                async for chunk in stream_agent_response(
                    agent_name="general",
                    prompt="raw prompt",
                    model_prompt="model metadata",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                )
            ]

        assert mock_prepare.await_args.args[1] == "raw prompt"
        assert mock_prepare.await_args.kwargs["model_prompt"] == "model metadata"

    @pytest.mark.asyncio
    async def test_stream_agent_response_passes_user_id_to_agent_arun(self, tmp_path: Path) -> None:
        """Test that stream_agent_response passes user_id all the way to agent.arun()."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            yield "chunk"

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            # Consume the async generator to trigger the agent.arun call.
            _chunks = [
                chunk
                async for chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    user_id="@user:localhost",
                )
            ]

            mock_agent.arun.assert_called_once()
            assert mock_agent.arun.call_args.kwargs["user_id"] == "@user:localhost"

    @pytest.mark.asyncio
    async def test_stream_agent_response_passes_run_id_to_agent_arun(self, tmp_path: Path) -> None:
        """Streaming cancellation needs an explicit run_id threaded to Agno."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            yield "chunk"

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            _chunks = [
                chunk
                async for chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    run_id="run-456",
                )
            ]

            mock_agent.arun.assert_called_once()
            assert mock_agent.arun.call_args.kwargs["run_id"] == "run-456"

    @pytest.mark.asyncio
    async def test_ai_response_raises_cancelled_error_for_cancelled_runs(self, tmp_path: Path) -> None:
        """Gracefully cancelled Agno runs should surface as task cancellation to the bot."""
        mock_agent = MagicMock()
        mock_run_output = MagicMock()
        mock_run_output.content = "Run run-123 was cancelled"
        mock_run_output.tools = None
        mock_run_output.status = RunStatus.cancelled
        mock_run_output.run_id = "run-123"
        mock_run_output.session_id = "session1"
        mock_run_output.model = "test-model"
        mock_run_output.model_provider = "openai"
        mock_run_output.metrics = None

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.cached_agent_run", new_callable=AsyncMock, return_value=mock_run_output),
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            with pytest.raises(asyncio.CancelledError):
                await ai_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                )

    @pytest.mark.asyncio
    async def test_ai_response_returns_friendly_error_for_error_status(self, tmp_path: Path) -> None:
        """Errored Agno RunOutput values must not be surfaced as successful replies."""
        mock_agent = MagicMock()
        mock_run_output = MagicMock()
        mock_run_output.content = "validation failed in agno"
        mock_run_output.status = RunStatus.error
        mock_run_output.tools = None

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.cached_agent_run", new_callable=AsyncMock, return_value=mock_run_output),
            patch("mindroom.ai.get_user_friendly_error_message", return_value="friendly-error") as mock_friendly_error,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            response = await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
            )

        assert response == "friendly-error"
        mock_friendly_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_ai_response_rejects_configured_team_targets(self, tmp_path: Path) -> None:
        """Generic ai helpers should reject configured team names explicitly."""
        with patch("mindroom.ai.get_user_friendly_error_message", return_value="friendly-error") as mock_friendly_error:
            response = await ai_response(
                agent_name="ultimate",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config_with_team(),
            )

        assert response == "friendly-error"
        error = mock_friendly_error.call_args.args[0]
        assert isinstance(error, ValueError)
        assert "configured team" in str(error)
        assert "team/ultimate" in str(error)

    @pytest.mark.asyncio
    async def test_stream_agent_response_rejects_configured_team_targets(self, tmp_path: Path) -> None:
        """Streaming agent helpers should reject configured team names explicitly."""
        with patch("mindroom.ai.get_user_friendly_error_message", return_value="friendly-error") as mock_friendly_error:
            chunks = [
                chunk
                async for chunk in stream_agent_response(
                    agent_name="ultimate",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config_with_team(),
                )
            ]

        assert chunks == ["friendly-error"]
        error = mock_friendly_error.call_args.args[0]
        assert isinstance(error, ValueError)
        assert "configured team" in str(error)
        assert "team/ultimate" in str(error)

    @pytest.mark.asyncio
    async def test_ai_response_passes_all_files_for_vertex_claude(self, tmp_path: Path) -> None:
        """Vertex Claude path should not silently drop non-PDF file media."""
        mock_agent = MagicMock()
        mock_agent.model = VertexAIClaude(id="claude-sonnet-4@20250514")
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        mock_run_output = MagicMock()
        mock_run_output.content = "Response"
        mock_run_output.tools = None
        mock_agent.arun = AsyncMock(return_value=mock_run_output)

        pdf_file = File(filepath=str(tmp_path / "report.pdf"), filename="report.pdf", mime_type="application/pdf")
        zip_file = File(filepath=str(tmp_path / "archive.zip"), filename="archive.zip")

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                media=MediaInputs(files=[pdf_file, zip_file]),
            )

        mock_agent.arun.assert_called_once()
        run_input = mock_agent.arun.call_args.args[0]
        assert isinstance(run_input, list)
        assert run_input[-1].files == [pdf_file, zip_file]

    @pytest.mark.asyncio
    async def test_stream_agent_response_passes_all_files_for_vertex_claude(self, tmp_path: Path) -> None:
        """Streaming path should not silently drop non-PDF files for Vertex Claude."""
        mock_agent = MagicMock()
        mock_agent.model = VertexAIClaude(id="claude-sonnet-4@20250514")
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[RunContentEvent]:
            yield RunContentEvent(content="chunk")

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        pdf_file = File(filepath=str(tmp_path / "report.pdf"), filename="report.pdf", mime_type="application/pdf")
        zip_file = File(filepath=str(tmp_path / "archive.zip"), filename="archive.zip")

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            _chunks = [
                _chunk
                async for _chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    media=MediaInputs(files=[pdf_file, zip_file]),
                )
            ]

        mock_agent.arun.assert_called_once()
        run_input = mock_agent.arun.call_args.args[0]
        assert isinstance(run_input, list)
        assert run_input[-1].files == [pdf_file, zip_file]

    @pytest.mark.asyncio
    async def test_ai_response_retries_without_media_on_validation_error(self, tmp_path: Path) -> None:
        """When inline media is rejected, non-streaming should retry once without media."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        mock_run_output = MagicMock()
        mock_run_output.content = "Recovered response"
        mock_run_output.tools = None
        mock_agent.arun = AsyncMock(
            side_effect=[
                Exception(
                    "litellm.BadRequestError: invalid_request_error: "
                    "document.source.base64.media_type: Input should be 'application/pdf'",
                ),
                mock_run_output,
            ],
        )

        document_file = File(
            filepath=str(tmp_path / "report.pdf"),
            filename="report.pdf",
            mime_type="application/pdf",
        )

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            response = await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                media=MediaInputs(files=[document_file]),
            )

        assert response == "Recovered response"
        assert mock_agent.arun.await_count == 2
        first_call = mock_agent.arun.await_args_list[0]
        second_call = mock_agent.arun.await_args_list[1]
        first_prompt = first_call.args[0]
        second_prompt = second_call.args[0]
        assert isinstance(first_prompt, list)
        assert isinstance(second_prompt, list)
        assert first_prompt[-1].files == [document_file]
        assert not second_prompt[-1].files
        assert "Inline media unavailable for this model" in str(second_prompt[-1].content)

    @pytest.mark.asyncio
    async def test_ai_response_rebuilds_request_log_context_for_retry(self, tmp_path: Path) -> None:
        """Non-streaming retries should log the actual prompt sent on each attempt."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        mock_run_output = MagicMock()
        mock_run_output.content = "Recovered response"
        mock_run_output.tools = None
        mock_agent.arun = AsyncMock(
            side_effect=[
                Exception(
                    "litellm.BadRequestError: invalid_request_error: "
                    "document.source.base64.media_type: Input should be 'application/pdf'",
                ),
                mock_run_output,
            ],
        )

        prepared_prompt = "prepared prompt"
        logged_contexts: list[dict[str, object]] = []
        document_file = File(
            filepath=str(tmp_path / "report.pdf"),
            filename="report.pdf",
            mime_type="application/pdf",
        )

        def fake_build_llm_request_log_context(**kwargs: object) -> dict[str, object]:
            logged_contexts.append(dict(kwargs))
            return {}

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.build_llm_request_log_context", side_effect=fake_build_llm_request_log_context),
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent, prompt=prepared_prompt)
            response = await ai_response(
                agent_name="general",
                prompt="raw prompt",
                model_prompt="expanded prompt",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                media=MediaInputs(files=[document_file]),
            )

        assert response == "Recovered response"
        mock_prepare.assert_awaited_once()
        assert mock_prepare.await_args.args[1] == "raw prompt"
        assert mock_prepare.await_args.kwargs["model_prompt"] == "expanded prompt"
        assert logged_contexts == [
            {
                "session_id": "session1",
                "room_id": None,
                "thread_id": None,
                "reply_to_event_id": None,
                "prompt": "raw prompt",
                "model_prompt": "expanded prompt",
                "full_prompt": prepared_prompt,
                "metadata": None,
            },
            {
                "session_id": "session1",
                "room_id": None,
                "thread_id": None,
                "reply_to_event_id": None,
                "prompt": "raw prompt",
                "model_prompt": "expanded prompt",
                "full_prompt": append_inline_media_fallback_prompt(prepared_prompt),
                "metadata": None,
            },
        ]

    @pytest.mark.asyncio
    async def test_ai_response_retries_errored_run_output_with_fresh_run_id(self, tmp_path: Path) -> None:
        """Inline-media retries must use a fresh Agno run_id after an errored run output."""
        mock_agent = MagicMock()
        error_output = MagicMock()
        error_output.content = "Error code: 500 - audio input is not supported"
        error_output.status = RunStatus.error
        error_output.tools = None

        success_output = MagicMock()
        success_output.content = "Recovered response"
        success_output.status = RunStatus.completed
        success_output.tools = None

        seen_run_ids: list[str | None] = []
        callback_run_ids: list[str] = []
        responses = [error_output, success_output]

        async def fake_run(*_args: object, **kwargs: object) -> MagicMock:
            seen_run_ids.append(kwargs["run_id"])
            run_id_callback = kwargs["run_id_callback"]
            if run_id_callback is not None and kwargs["run_id"] is not None:
                run_id_callback(kwargs["run_id"])
            return responses.pop(0)

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.cached_agent_run", side_effect=fake_run),
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            response = await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                run_id="run-123",
                run_id_callback=callback_run_ids.append,
                media=MediaInputs(audio=[MagicMock(name="audio_input")]),
            )

        assert response == "Recovered response"
        assert seen_run_ids[0] == "run-123"
        assert seen_run_ids[1] is not None
        assert seen_run_ids[1] != "run-123"
        assert callback_run_ids == [run_id for run_id in seen_run_ids if run_id is not None]

    @pytest.mark.asyncio
    async def test_stream_agent_response_retries_without_media_on_validation_error(self, tmp_path: Path) -> None:
        """When inline media is rejected, streaming should retry once without media."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def failing_stream() -> AsyncIterator[object]:
            yield RunErrorEvent(
                content=(
                    "litellm.BadRequestError: invalid_request_error: "
                    "document.source.base64.media_type: Input should be 'application/pdf'"
                ),
            )

        async def successful_stream() -> AsyncIterator[object]:
            yield RunContentEvent(content="Recovered stream")

        mock_agent.arun = MagicMock(side_effect=[failing_stream(), successful_stream()])

        document_file = File(
            filepath=str(tmp_path / "report.pdf"),
            filename="report.pdf",
            mime_type="application/pdf",
        )

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            chunks = [
                chunk
                async for chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    media=MediaInputs(files=[document_file]),
                )
            ]

        assert mock_agent.arun.call_count == 2
        first_call = mock_agent.arun.call_args_list[0]
        second_call = mock_agent.arun.call_args_list[1]
        first_prompt = first_call.args[0]
        second_prompt = second_call.args[0]
        assert isinstance(first_prompt, list)
        assert isinstance(second_prompt, list)
        assert first_prompt[-1].files == [document_file]
        assert not second_prompt[-1].files
        assert "Inline media unavailable for this model" in str(second_prompt[-1].content)
        assert any(isinstance(chunk, RunContentEvent) and chunk.content == "Recovered stream" for chunk in chunks)

    @pytest.mark.asyncio
    async def test_stream_agent_response_rebuilds_request_log_context_for_retry(self, tmp_path: Path) -> None:
        """Streaming retries should log the actual prompt sent on each attempt."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def failing_stream() -> AsyncIterator[object]:
            yield RunErrorEvent(
                content=(
                    "litellm.BadRequestError: invalid_request_error: "
                    "document.source.base64.media_type: Input should be 'application/pdf'"
                ),
            )

        async def successful_stream() -> AsyncIterator[object]:
            yield RunContentEvent(content="Recovered stream")

        mock_agent.arun = MagicMock(side_effect=[failing_stream(), successful_stream()])

        prepared_prompt = "prepared prompt"
        logged_contexts: list[dict[str, object]] = []
        document_file = File(
            filepath=str(tmp_path / "report.pdf"),
            filename="report.pdf",
            mime_type="application/pdf",
        )

        def fake_build_llm_request_log_context(**kwargs: object) -> dict[str, object]:
            logged_contexts.append(dict(kwargs))
            return {}

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.build_llm_request_log_context", side_effect=fake_build_llm_request_log_context),
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent, prompt=prepared_prompt)
            chunks = [
                chunk
                async for chunk in stream_agent_response(
                    agent_name="general",
                    prompt="raw prompt",
                    model_prompt="expanded prompt",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    media=MediaInputs(files=[document_file]),
                )
            ]

        assert any(isinstance(chunk, RunContentEvent) and chunk.content == "Recovered stream" for chunk in chunks)
        mock_prepare.assert_awaited_once()
        assert mock_prepare.await_args.args[1] == "raw prompt"
        assert mock_prepare.await_args.kwargs["model_prompt"] == "expanded prompt"
        assert logged_contexts == [
            {
                "session_id": "session1",
                "room_id": None,
                "thread_id": None,
                "reply_to_event_id": None,
                "prompt": "raw prompt",
                "model_prompt": "expanded prompt",
                "full_prompt": prepared_prompt,
                "metadata": None,
            },
            {
                "session_id": "session1",
                "room_id": None,
                "thread_id": None,
                "reply_to_event_id": None,
                "prompt": "raw prompt",
                "model_prompt": "expanded prompt",
                "full_prompt": append_inline_media_fallback_prompt(prepared_prompt),
                "metadata": None,
            },
        ]

    @pytest.mark.asyncio
    async def test_stream_agent_response_keeps_request_log_context_for_deferred_model_call(
        self,
        tmp_path: Path,
    ) -> None:
        """Streaming request logs must keep the bound context until the deferred model call runs."""

        class _DeferredLoggingModel:
            def __init__(self) -> None:
                self.id = "test-model"
                self.system_prompt = None
                self.temperature = 0.7
                self.client = None
                self.async_client = None

            async def ainvoke(self, *_args: object, **_kwargs: object) -> dict[str, str]:
                return {"status": "ok"}

            async def ainvoke_stream(
                self,
                *_args: object,
                **_kwargs: object,
            ) -> AsyncIterator[dict[str, str]]:
                yield {"status": "ok"}

        class _DeferredLoggingAgent:
            def __init__(self, model: _DeferredLoggingModel) -> None:
                self.model = model
                self.name = "GeneralAgent"
                self.add_history_to_context = False
                self.db = None
                self.learning = None

            async def arun(self, run_input: object, **_kwargs: object) -> AsyncIterator[object]:
                messages = (
                    [message.model_copy(deep=True) for message in run_input]
                    if isinstance(run_input, list) and all(isinstance(message, Message) for message in run_input)
                    else [Message(role="user", content=cast("str", run_input))]
                )
                async for _chunk in self.model.ainvoke_stream(
                    messages=messages,
                    assistant_message=Message(role="assistant"),
                    tools=[],
                ):
                    pass
                yield RunContentEvent(content="Deferred stream")

        prepared_prompt = "prepared prompt"
        model = _DeferredLoggingModel()
        install_llm_request_logging(
            model,
            agent_name="general",
            debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
            default_log_dir=tmp_path / "unused",
        )
        agent = _DeferredLoggingAgent(model)
        config = _config().model_copy(
            update={
                "debug": DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
            },
        )

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
            mock_prepare.return_value = _prepared_prompt_result(agent, prompt=prepared_prompt)
            chunks = [
                chunk
                async for chunk in stream_agent_response(
                    agent_name="general",
                    prompt="raw prompt",
                    model_prompt="expanded prompt",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=config,
                    room_id="!room:example.com",
                    thread_id="$thread:example.com",
                    reply_to_event_id="$reply:example.com",
                )
            ]

        assert any(isinstance(chunk, RunContentEvent) and chunk.content == "Deferred stream" for chunk in chunks)

        log_files = list(tmp_path.glob("llm-requests-*.jsonl"))
        assert len(log_files) == 1
        entries = [json.loads(line) for line in log_files[0].read_text(encoding="utf-8").splitlines()]
        assert len(entries) == 1
        assert entries[0]["session_id"] == "session1"
        assert entries[0]["room_id"] == "!room:example.com"
        assert entries[0]["thread_id"] == "$thread:example.com"
        assert entries[0]["reply_to_event_id"] == "$reply:example.com"
        assert entries[0]["current_turn_prompt"] == "raw prompt"
        assert entries[0]["model_prompt"] == "expanded prompt"
        assert entries[0]["full_prompt"] == prepared_prompt
        assert entries[0]["messages"][0]["role"] == "user"
        assert entries[0]["messages"][0]["content"] == prepared_prompt

    @pytest.mark.asyncio
    async def test_stream_agent_response_retries_with_fresh_run_id(self, tmp_path: Path) -> None:
        """Streaming inline-media retries must not reuse the cancelled attempt's run_id."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def failing_stream() -> AsyncIterator[object]:
            yield RunErrorEvent(content="Error code: 500 - audio input is not supported")

        async def successful_stream() -> AsyncIterator[object]:
            yield RunContentEvent(content="Recovered stream")

        callback_run_ids: list[str] = []
        mock_agent.arun = MagicMock(side_effect=[failing_stream(), successful_stream()])

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            chunks = [
                chunk
                async for chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    run_id="run-456",
                    run_id_callback=callback_run_ids.append,
                    media=MediaInputs(audio=[MagicMock(name="audio_input")]),
                )
            ]

        assert any(isinstance(chunk, RunContentEvent) and chunk.content == "Recovered stream" for chunk in chunks)
        first_call = mock_agent.arun.call_args_list[0]
        second_call = mock_agent.arun.call_args_list[1]
        assert first_call.kwargs["run_id"] == "run-456"
        assert second_call.kwargs["run_id"] is not None
        assert second_call.kwargs["run_id"] != "run-456"
        assert callback_run_ids == [first_call.kwargs["run_id"], second_call.kwargs["run_id"]]

    @pytest.mark.parametrize(
        ("error_text", "expected"),
        [
            (
                "invalid_request_error: messages.1.content.0.document.source.base64.media_type: Input should be 'application/pdf'",
                True,
            ),
            (
                "invalid_request_error: messages.8.content.1.image.source.base64: The image was specified using the image/jpeg media type, but the image appears to be a image/png image",
                True,
            ),
            ("Error code: 500 - audio input is not supported", True),
            ("invalid_request_error: max_tokens must be <= 4096", False),
            ("Rate limit exceeded", False),
        ],
    )
    def test_should_retry_without_inline_media_error_matching(self, error_text: str, expected: bool) -> None:
        """Retry matcher should target inline-media validation and unsupported-input failures."""
        assert should_retry_without_inline_media(error_text, MediaInputs(images=(object(),))) is expected

    def test_append_inline_media_fallback_prompt_is_idempotent(self) -> None:
        """Fallback marker should only be appended once across retries."""
        initial_prompt = "Inspect this attachment."
        first = append_inline_media_fallback_prompt(initial_prompt)
        second = append_inline_media_fallback_prompt(first)
        assert first == second

    @pytest.mark.asyncio
    async def test_ai_response_does_not_retry_without_media_validation_match(self, tmp_path: Path) -> None:
        """Non-media failures should not trigger inline-media retry even when media is present."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False
        mock_agent.arun = AsyncMock(side_effect=Exception("invalid_request_error: max_tokens must be <= 4096"))

        document_file = File(
            filepath=str(tmp_path / "report.pdf"),
            filename="report.pdf",
            mime_type="application/pdf",
        )

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.get_user_friendly_error_message", return_value="friendly") as mock_friendly_error,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            response = await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                media=MediaInputs(files=[document_file]),
            )

        assert response == "friendly"
        assert mock_agent.arun.await_count == 1
        mock_friendly_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_stream_agent_response_retries_only_once_on_repeated_media_validation_error(
        self,
        tmp_path: Path,
    ) -> None:
        """Streaming should attempt exactly one inline-media fallback retry."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def media_validation_error_stream() -> AsyncIterator[object]:
            yield RunErrorEvent(
                content=(
                    "invalid_request_error: "
                    "messages.3.content.0.document.source.base64.media_type: Input should be 'application/pdf'"
                ),
            )

        mock_agent.arun = MagicMock(
            side_effect=[media_validation_error_stream(), media_validation_error_stream()],
        )

        document_file = File(
            filepath=str(tmp_path / "report.pdf"),
            filename="report.pdf",
            mime_type="application/pdf",
        )

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.get_user_friendly_error_message", return_value="friendly-error") as mock_friendly_error,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            chunks = [
                chunk
                async for chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    media=MediaInputs(files=[document_file]),
                )
            ]

        assert mock_agent.arun.call_count == 2
        first_call = mock_agent.arun.call_args_list[0]
        second_call = mock_agent.arun.call_args_list[1]
        first_prompt = first_call.args[0]
        second_prompt = second_call.args[0]
        assert isinstance(first_prompt, list)
        assert isinstance(second_prompt, list)
        assert first_prompt[-1].files == [document_file]
        assert not second_prompt[-1].files
        assert str(second_prompt[-1].content).count("Inline media unavailable for this model") == 1
        assert chunks == ["friendly-error"]
        mock_friendly_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_user_id_none_when_not_provided(self, tmp_path: Path) -> None:
        """Test that user_id defaults to None when not provided (backward compatibility)."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_run_output = MagicMock()
        mock_run_output.content = "Response"
        mock_run_output.tools = None
        mock_agent.arun = AsyncMock(return_value=mock_run_output)

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            # Call without user_id
            await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
            )

            mock_agent.arun.assert_called_once()
            assert mock_agent.arun.call_args.kwargs["user_id"] is None

    @pytest.mark.asyncio
    async def test_ai_response_collects_tool_trace_when_tool_calls_hidden(self, tmp_path: Path) -> None:
        """Non-streaming path should still surface structured tool metadata."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        tool = MagicMock()
        tool.tool_name = "read_file"
        tool.tool_args = {"path": "README.md"}
        tool.result = "ok"

        mock_run_output = MagicMock()
        mock_run_output.content = "Done."
        mock_run_output.tools = [tool]
        mock_agent.arun = AsyncMock(return_value=mock_run_output)

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            tool_trace: list[object] = []
            response = await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                show_tool_calls=False,
                tool_trace_collector=tool_trace,
            )

        assert response == "Done."
        assert "<tool>" not in response
        assert len(tool_trace) == 1

    @pytest.mark.asyncio
    async def test_ai_response_collects_run_metadata(self, tmp_path: Path) -> None:
        """Non-streaming path should expose model/token/context metadata."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        mock_run_output = MagicMock()
        mock_run_output.content = "Response"
        mock_run_output.tools = []
        mock_run_output.run_id = "run-1"
        mock_run_output.session_id = "session1"
        mock_run_output.status = RunStatus.completed
        mock_run_output.model = "test-model"
        mock_run_output.model_provider = "openai"
        mock_run_output.metrics = Metrics(
            input_tokens=800,
            output_tokens=120,
            total_tokens=920,
            time_to_first_token=0.42,
            duration=1.75,
        )
        mock_agent.arun = AsyncMock(return_value=mock_run_output)

        config = Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=2000)},
        )

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            run_metadata: dict[str, object] = {}
            await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=config,
                run_metadata_collector=run_metadata,
            )

        payload = run_metadata["io.mindroom.ai_run"]
        assert payload["version"] == 1
        assert payload["run_id"] == "run-1"
        assert payload["status"] == "completed"
        assert payload["usage"]["input_tokens"] == 800
        assert payload["context"]["input_tokens"] == 800
        assert payload["context"]["window_tokens"] == 2000
        assert "utilization_pct" not in payload["context"]
        assert payload["tools"]["count"] == 0

    def test_build_matrix_run_metadata_merges_coalesced_source_event_ids(self) -> None:
        """Run metadata should mark every source event in a coalesced batch as seen."""
        metadata = build_matrix_run_metadata(
            "$primary",
            ["$unseen"],
            extra_metadata={
                MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$first", "$primary"],
                MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY: {"$first": "first", "$primary": "primary"},
            },
        )

        assert metadata == {
            MATRIX_EVENT_ID_METADATA_KEY: "$primary",
            MATRIX_SEEN_EVENT_IDS_METADATA_KEY: ["$primary", "$first", "$unseen"],
            MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$first", "$primary"],
            MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY: {"$first": "first", "$primary": "primary"},
        }

    @pytest.mark.asyncio
    async def test_stream_agent_response_collects_run_metadata(self, tmp_path: Path) -> None:
        """Streaming path should expose run metadata from completion events."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            yield RunContentEvent(content="hello")
            yield ModelRequestCompletedEvent(
                model="test-model",
                model_provider="openai",
                input_tokens=500,
                output_tokens=60,
                total_tokens=560,
                time_to_first_token=0.33,
            )
            yield RunCompletedEvent(
                run_id="run-2",
                session_id="session1",
                metrics=Metrics(
                    input_tokens=500,
                    output_tokens=60,
                    total_tokens=560,
                    duration=2.4,
                ),
            )

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        config = Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=1000)},
        )

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            run_metadata: dict[str, object] = {}
            async for _chunk in stream_agent_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=config,
                run_metadata_collector=run_metadata,
            ):
                pass

        payload = run_metadata["io.mindroom.ai_run"]
        assert payload["version"] == 1
        assert payload["run_id"] == "run-2"
        assert payload["usage"]["total_tokens"] == 560
        assert payload["context"]["input_tokens"] == 500
        assert payload["context"]["window_tokens"] == 1000
        assert "utilization_pct" not in payload["context"]

    @pytest.mark.asyncio
    async def test_ai_response_metadata_uses_room_resolved_runtime_model(self, tmp_path: Path) -> None:
        """Non-streaming metadata should report the room-resolved runtime model."""
        runtime_paths = _runtime_paths(tmp_path)
        config = bind_runtime_paths(
            Config(
                agents={"general": AgentConfig(display_name="General", model="default")},
                room_models={"lobby": "large"},
                models={
                    "default": ModelConfig(provider="openai", id="default-model", context_window=2000),
                    "large": ModelConfig(provider="openai", id="large-model", context_window=48000),
                },
            ),
            runtime_paths,
        )
        mock_agent = MagicMock()
        mock_run_output = MagicMock()
        mock_run_output.run_id = "run-room"
        mock_run_output.session_id = "session1"
        mock_run_output.status = RunStatus.completed
        mock_run_output.model = "large-model"
        mock_run_output.model_provider = "openai"
        mock_run_output.metrics = Metrics(input_tokens=800, output_tokens=50, total_tokens=850, duration=1.2)
        mock_run_output.tools = None
        mock_run_output.content = "Response"

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai.cached_agent_run", new_callable=AsyncMock, return_value=mock_run_output),
            patch("mindroom.matrix.rooms.get_room_alias_from_id", return_value="lobby"),
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            run_metadata: dict[str, object] = {}
            await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                room_id="!test:localhost",
                runtime_paths=runtime_paths,
                config=config,
                run_metadata_collector=run_metadata,
            )

        payload = run_metadata["io.mindroom.ai_run"]
        assert payload["model"]["config"] == "large"
        assert payload["model"]["id"] == "large-model"
        assert payload["context"]["window_tokens"] == 48000

    @pytest.mark.asyncio
    async def test_stream_agent_response_metadata_uses_room_resolved_runtime_model(self, tmp_path: Path) -> None:
        """Streaming metadata should report the room-resolved runtime model."""
        runtime_paths = _runtime_paths(tmp_path)
        config = bind_runtime_paths(
            Config(
                agents={"general": AgentConfig(display_name="General", model="default")},
                room_models={"lobby": "large"},
                models={
                    "default": ModelConfig(provider="openai", id="default-model", context_window=1000),
                    "large": ModelConfig(provider="openai", id="large-model", context_window=32000),
                },
            ),
            runtime_paths,
        )
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "large-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            yield RunContentEvent(content="hello")
            yield ModelRequestCompletedEvent(
                model="large-model",
                model_provider="openai",
                input_tokens=500,
                output_tokens=60,
                total_tokens=560,
                time_to_first_token=0.33,
            )
            yield RunCompletedEvent(
                run_id="run-room-stream",
                session_id="session1",
                metrics=Metrics(
                    input_tokens=500,
                    output_tokens=60,
                    total_tokens=560,
                    duration=2.4,
                ),
            )

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.matrix.rooms.get_room_alias_from_id", return_value="lobby"),
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            run_metadata: dict[str, object] = {}
            async for _chunk in stream_agent_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                room_id="!test:localhost",
                runtime_paths=runtime_paths,
                config=config,
                run_metadata_collector=run_metadata,
            ):
                pass

        payload = run_metadata["io.mindroom.ai_run"]
        assert payload["model"]["config"] == "large"
        assert payload["model"]["id"] == "large-model"
        assert payload["context"]["window_tokens"] == 32000

    @pytest.mark.asyncio
    async def test_stream_agent_response_raises_cancelled_error_for_run_cancelled_event(self, tmp_path: Path) -> None:
        """Graceful stream cancellation should preserve metadata and end as CancelledError."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            yield RunContentEvent(content="partial")
            yield ModelRequestCompletedEvent(
                model="test-model",
                model_provider="openai",
                input_tokens=100,
                output_tokens=25,
                total_tokens=125,
            )
            yield RunCancelledEvent(
                run_id="run-3",
                session_id="session1",
                reason="Run run-3 was cancelled",
            )

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        config = Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=1000)},
        )

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            run_metadata: dict[str, object] = {}
            with pytest.raises(asyncio.CancelledError):
                async for _chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=config,
                    run_metadata_collector=run_metadata,
                ):
                    pass

        payload = run_metadata["io.mindroom.ai_run"]
        assert payload["run_id"] == "run-3"
        assert payload["status"] == "cancelled"
        assert payload["usage"]["input_tokens"] == 100
        assert payload["usage"]["output_tokens"] == 25
        assert payload["usage"]["total_tokens"] == 125

    @pytest.mark.asyncio
    async def test_stream_agent_response_uses_request_metrics_fallback(self, tmp_path: Path) -> None:
        """Streaming metadata should fall back to model request metrics when needed."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            yield RunContentEvent(content="ok")
            yield ModelRequestCompletedEvent(
                model="test-model",
                model_provider="openai",
                input_tokens=12,
                output_tokens=3,
                time_to_first_token=0.12,
            )

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        config = Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=100)},
        )

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            run_metadata: dict[str, object] = {}
            async for _chunk in stream_agent_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=config,
                run_metadata_collector=run_metadata,
            ):
                pass

        payload = run_metadata["io.mindroom.ai_run"]
        assert payload["status"] == "completed"
        assert payload["usage"]["input_tokens"] == 12
        assert payload["usage"]["output_tokens"] == 3
        assert payload["usage"]["total_tokens"] == 15
        assert payload["usage"]["time_to_first_token"] == format(0.12, ".12g")
        assert payload["context"]["input_tokens"] == 12
        assert payload["context"]["window_tokens"] == 100
        assert "utilization_pct" not in payload["context"]

    @pytest.mark.asyncio
    async def test_stream_agent_response_uses_latest_request_tokens_for_context(self, tmp_path: Path) -> None:
        """Streaming context metadata should reflect the latest request, not cumulative run usage."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            yield RunContentEvent(content="step one")
            yield ModelRequestCompletedEvent(
                model="test-model",
                model_provider="openai",
                input_tokens=700,
                output_tokens=50,
                total_tokens=750,
            )
            yield RunContentEvent(content="step two")
            yield ModelRequestCompletedEvent(
                model="test-model",
                model_provider="openai",
                input_tokens=120,
                output_tokens=20,
                total_tokens=140,
            )

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        config = Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=1000)},
        )

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            run_metadata: dict[str, object] = {}
            async for _chunk in stream_agent_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=config,
                run_metadata_collector=run_metadata,
            ):
                pass

        payload = run_metadata["io.mindroom.ai_run"]
        assert payload["status"] == "completed"
        assert payload["usage"]["input_tokens"] == 820
        assert payload["usage"]["output_tokens"] == 70
        assert payload["usage"]["total_tokens"] == 890
        assert payload["context"]["input_tokens"] == 120
        assert payload["context"]["window_tokens"] == 1000
