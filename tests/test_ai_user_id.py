"""Test that user_id is passed through to agent.arun() for Agno learning."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager, suppress
from contextvars import Context
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, TypeVar, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.db.base import SessionType
from agno.media import File
from agno.models.message import Message
from agno.models.metrics import Metrics
from agno.models.response import ToolExecution
from agno.models.vertexai.claude import Claude as VertexAIClaude
from agno.run.agent import (
    ModelRequestCompletedEvent,
    RunCancelledEvent,
    RunCompletedEvent,
    RunContentEvent,
    RunErrorEvent,
    RunOutput,
    ToolCallCompletedEvent,
    ToolCallStartedEvent,
)
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom.agent_run_context import append_knowledge_availability_enrichment
from mindroom.ai import (
    _compose_current_turn_prompt,
    _prepare_agent_and_prompt,
    _PreparedAgentRun,
    _stream_completed_without_visible_output,
    _stream_with_request_log_context,
    _StreamingAttemptState,
    ai_response,
    build_matrix_run_metadata,
    should_retry_without_inline_media,
    stream_agent_response,
)
from mindroom.ai_run_metadata import _serialize_metrics
from mindroom.bot import AgentBot
from mindroom.cancellation import USER_STOP_CANCEL_MSG
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
from mindroom.delivery_gateway import DeliveryGateway, DeliveryGatewayDeps, ResponseHookService
from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome
from mindroom.history import PreparedHistoryState, strip_transient_enrichment_from_session
from mindroom.history.runtime import ScopeSessionContext
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope
from mindroom.hooks import (
    BUILTIN_EVENT_NAMES,
    EVENT_MESSAGE_CANCELLED,
    EVENT_SESSION_STARTED,
    CancelledResponseContext,
    EnrichmentItem,
    HookContextSupport,
    HookRegistry,
    MessageEnvelope,
    SessionHookContext,
    hook,
    render_system_enrichment_block,
)
from mindroom.hooks.registry import HookRegistryState
from mindroom.hooks.types import RESERVED_EVENT_NAMESPACES, default_timeout_ms_for_event, validate_event_name
from mindroom.knowledge import KnowledgeAvailability, KnowledgeAvailabilityDetail, KnowledgeResolution
from mindroom.llm_request_logging import install_llm_request_logging
from mindroom.matrix.identity import MatrixID
from mindroom.media_fallback import append_inline_media_fallback_prompt
from mindroom.media_inputs import MediaInputs
from mindroom.memory import MemoryPromptParts
from mindroom.message_target import MessageTarget
from mindroom.post_response_effects import PostResponseEffectsSupport
from mindroom.response_runner import (
    ResponseRequest,
    ResponseRunner,
    ResponseRunnerDeps,
    _strip_visible_tool_markers,
    prepare_memory_and_model_context,
)
from mindroom.streaming import StreamingDeliveryError
from mindroom.tool_system.events import ToolTraceEntry
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
from tests.conftest import bind_runtime_paths, make_event_cache_mock, resolve_response_thread_root_for_test

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Generator
    from pathlib import Path

    from agno.knowledge.knowledge import Knowledge

T = TypeVar("T")


def _stream_outcome(
    event_id: str | None,
    body: str,
    *,
    terminal_status: str = "completed",
    visible_body_state: str = "visible_body",
    failure_reason: str | None = None,
) -> StreamTransportOutcome:
    return StreamTransportOutcome(
        last_physical_stream_event_id=event_id,
        terminal_status=terminal_status,
        rendered_body=body,
        visible_body_state=visible_body_state,
        failure_reason=failure_reason,
    )


def _visible_response_event_id(outcome: FinalDeliveryOutcome | str | None) -> str | None:
    if isinstance(outcome, str) or outcome is None:
        return outcome
    return outcome.final_visible_event_id


def _handled_response_event_id(outcome: FinalDeliveryOutcome | str | None) -> str | None:
    if isinstance(outcome, str) or outcome is None:
        return outcome
    return outcome.event_id if outcome.mark_handled and outcome.is_visible_response and not outcome.suppressed else None


def _set_gateway_method(gateway: DeliveryGateway, name: str, value: T) -> T:
    object.__setattr__(gateway, name, value)
    return value


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


def _config_with_matrix_message() -> Config:
    return Config(
        agents={
            "general": AgentConfig(
                display_name="General",
                tools=["matrix_message"],
            ),
        },
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
) -> _PreparedAgentRun:
    return _PreparedAgentRun(
        agent=agent,
        messages=(Message(role="user", content=prompt),),
        unseen_event_ids=[],
        prepared_history=PreparedHistoryState(),
    )


def test_serialize_metrics_preserves_zero_usage_fields_from_metrics() -> None:
    """Metrics serialization should preserve only the provider payload Agno exposes."""
    payload = _serialize_metrics(Metrics(input_tokens=6, output_tokens=0, cache_read_tokens=46449))

    assert payload == {
        "input_tokens": 6,
        "cache_read_tokens": 46449,
    }


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


@contextmanager
def _open_agent_scope_context(
    storage: _SessionStorage,
    *,
    scope_id: str = "general",
) -> Generator[ScopeSessionContext, None, None]:
    yield ScopeSessionContext(
        scope=HistoryScope(kind="agent", scope_id=scope_id),
        storage=storage.open(),
        session=storage.session,
    )


@contextmanager
def _open_team_scope_context(
    storage: _SessionStorage,
    *,
    scope_id: str = "ultimate",
) -> Generator[ScopeSessionContext, None, None]:
    yield ScopeSessionContext(
        scope=HistoryScope(kind="team", scope_id=scope_id),
        storage=storage.open(),
        session=storage.session,
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
    bot._knowledge_access_support = _knowledge_access_support()
    bot._send_response = AsyncMock(return_value="$response_id")
    bot._handle_interactive_question = AsyncMock()
    return bot


def _knowledge_access_support(
    knowledge: object | None = None,
    unavailable: dict[str, KnowledgeAvailabilityDetail] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        for_agent=MagicMock(return_value=knowledge),
        resolve_for_agent=MagicMock(
            return_value=KnowledgeResolution(
                knowledge=cast("Knowledge | None", knowledge),
                unavailable=unavailable or {},
            ),
        ),
    )


def _team_orchestrator(config: Config, runtime_paths: RuntimePaths) -> SimpleNamespace:
    matrix_admin = object()
    knowledge_refresh_scheduler = SimpleNamespace(
        schedule_refresh=lambda _base_id: None,
        is_refreshing=lambda _base_id: False,
    )
    return SimpleNamespace(
        config=config,
        runtime_paths=runtime_paths,
        knowledge_refresh_scheduler=knowledge_refresh_scheduler,
        hook_matrix_admin=lambda: matrix_admin,
        hook_room_state_querier=lambda: None,
        hook_room_state_putter=lambda: None,
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
    knowledge_access_support: SimpleNamespace | None = None,
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
    bot._conversation_resolver.deps = SimpleNamespace(
        conversation_cache=SimpleNamespace(
            get_latest_thread_event_id_if_needed=AsyncMock(return_value=None),
            notify_outbound_message=MagicMock(),
            notify_outbound_event=MagicMock(),
            notify_outbound_redaction=MagicMock(),
        ),
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
    runtime = SimpleNamespace(
        client=bot.client,
        config=config,
        enable_streaming=bot.enable_streaming,
        orchestrator=bot.orchestrator,
        event_cache=make_event_cache_mock(),
        runtime_started_at=0.0,
    )
    hook_context = HookContextSupport(
        runtime=runtime,
        logger=bot.logger,
        runtime_paths=runtime_paths,
        agent_name=bot.agent_name,
        hook_registry_state=HookRegistryState(hook_registry or HookRegistry.empty()),
        hook_send_message=AsyncMock(),
    )
    response_hook_service = ResponseHookService(hook_context=hook_context)
    response_hook_service.emit_cancelled_response = AsyncMock(wraps=response_hook_service.emit_cancelled_response)
    response_hook_service.emit_after_response = AsyncMock(wraps=response_hook_service.emit_after_response)
    delivery_gateway = DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=runtime,
            runtime_paths=runtime_paths,
            agent_name=bot.agent_name,
            logger=bot.logger,
            redact_message_event=AsyncMock(return_value=True),
            sender_domain="localhost",
            resolver=bot._conversation_resolver,
            response_hooks=response_hook_service,
        ),
    )
    _set_gateway_method(
        delivery_gateway,
        "deliver_stream",
        AsyncMock(
            return_value=StreamTransportOutcome(
                last_physical_stream_event_id="$msg_id",
                terminal_status="completed",
                rendered_body="Hello!",
                visible_body_state="visible_body",
            ),
        ),
    )
    _set_gateway_method(delivery_gateway, "edit_text", AsyncMock(return_value=True))
    _set_gateway_method(delivery_gateway, "send_text", AsyncMock(return_value="$thinking"))
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
    bot._knowledge_access_support = knowledge_access_support or _knowledge_access_support()

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


def test_persist_interrupted_turn_closes_storage_after_write(tmp_path: Path) -> None:
    """Runner-owned interrupted replay should always close the opened storage handle."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    coordinator = _build_response_runner(
        bot,
        config=config,
        runtime_paths=runtime_paths,
        storage_path=tmp_path,
        requester_id="@alice:localhost",
    )
    storage = MagicMock()
    coordinator.deps.state_writer.create_storage = MagicMock(return_value=storage)
    recorder = TurnRecorder(user_message="Hello")
    recorder.mark_interrupted()

    with patch("mindroom.response_runner.persist_interrupted_replay_snapshot") as mock_persist:
        coordinator._persist_interrupted_turn(
            recorder=recorder,
            session_scope=HistoryScope(kind="agent", scope_id="general"),
            session_id="session1",
            execution_identity=None,
            run_id="run-1",
            is_team=False,
        )

    mock_persist.assert_called_once()
    storage.close.assert_called_once_with()


def test_persist_interrupted_turn_closes_storage_when_write_fails(tmp_path: Path) -> None:
    """Runner-owned interrupted replay should close storage even if persistence raises."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    coordinator = _build_response_runner(
        bot,
        config=config,
        runtime_paths=runtime_paths,
        storage_path=tmp_path,
        requester_id="@alice:localhost",
    )
    storage = MagicMock()
    coordinator.deps.state_writer.create_storage = MagicMock(return_value=storage)
    recorder = TurnRecorder(user_message="Hello")
    recorder.mark_interrupted()

    with (
        patch(
            "mindroom.response_runner.persist_interrupted_replay_snapshot",
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        coordinator._persist_interrupted_turn(
            recorder=recorder,
            session_scope=HistoryScope(kind="agent", scope_id="general"),
            session_id="session1",
            execution_identity=None,
            run_id="run-1",
            is_team=False,
        )

    storage.close.assert_called_once_with()


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


@pytest.mark.asyncio
@pytest.mark.parametrize("use_streaming", [False, True])
async def test_generate_response_emits_cancelled_hook_once_for_empty_prompt(
    tmp_path: Path,
    use_streaming: bool,
) -> None:
    """Blank prompts should emit one canonical message:cancelled hook through lifecycle finalization."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=use_streaming)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
        )
        coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.reset_mock()

        response_event_id = await coordinator.generate_response(
            _response_request(prompt="   ", user_id="@alice:localhost"),
        )

    assert response_event_id is None
    coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.assert_awaited_once()
    assert (
        coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.await_args.kwargs[
            "failure_reason"
        ]
        == "empty_prompt"
    )


@pytest.mark.asyncio
async def test_process_and_respond_propagates_before_response_cancellation_to_runner(
    tmp_path: Path,
) -> None:
    """Pre-send before_response cancellation must reach the runner cancellation handler."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with patch("mindroom.response_runner.ai_response", new=AsyncMock(return_value="Hello!")):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
        )
        coordinator._persist_interrupted_recorder = MagicMock()
        coordinator.deps.delivery_gateway.deps.response_hooks.apply_before_response = AsyncMock(
            side_effect=asyncio.CancelledError(USER_STOP_CANCEL_MSG),
        )

        with pytest.raises(asyncio.CancelledError, match=USER_STOP_CANCEL_MSG):
            await coordinator.process_and_respond(
                ResponseRequest(
                    room_id="!test:localhost",
                    reply_to_event_id="$user_msg",
                    thread_id="$thread-root",
                    thread_history=(),
                    prompt="Hello",
                    user_id="@alice:localhost",
                    existing_event_id="$thinking",
                    existing_event_is_placeholder=True,
                ),
                run_id="run-1",
            )

    coordinator._persist_interrupted_recorder.assert_called_once()
    coordinator.deps.delivery_gateway.deps.redact_message_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_and_respond_streaming_preserves_user_stop_outcome(
    tmp_path: Path,
) -> None:
    """Explicit user-stop during streamed delivery should finalize once through the locked path."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )
        expected_outcome = FinalDeliveryOutcome(
            terminal_status="cancelled",
            event_id="$streaming",
            is_visible_response=True,
            final_visible_body="partial answer\n\n**[Response cancelled by user]**",
            failure_reason="cancelled_by_user",
        )
        coordinator.generate_streaming_ai_response = AsyncMock(
            side_effect=StreamingDeliveryError(
                asyncio.CancelledError(USER_STOP_CANCEL_MSG),
                event_id="$streaming",
                accumulated_text="partial answer",
                tool_trace=[],
                transport_outcome=_stream_outcome(
                    "$streaming",
                    "partial answer\n\n**[Response cancelled by user]**",
                    terminal_status="cancelled",
                    failure_reason="cancelled_by_user",
                ),
            ),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "finalize_streamed_response",
            AsyncMock(return_value=expected_outcome),
        )
        coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.reset_mock()

        response_event_id = await coordinator.generate_response_locked(
            replace(
                _response_request(
                    prompt="Hello",
                    user_id="@alice:localhost",
                    thread_id="$thread-root",
                ),
                existing_event_id="$streaming",
            ),
            resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

    assert response_event_id == "$streaming"
    coordinator.deps.delivery_gateway.finalize_streamed_response.assert_awaited_once()
    coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.assert_awaited_once()
    assert (
        coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.await_args.kwargs[
            "failure_reason"
        ]
        == "cancelled_by_user"
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
    bot._knowledge_access_support = _knowledge_access_support()
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
                hook_matrix_admin=MagicMock(return_value=object()),
                hook_room_state_querier=MagicMock(return_value=None),
                hook_room_state_putter=MagicMock(return_value=None),
                knowledge_refresh_scheduler=SimpleNamespace(
                    schedule_refresh=lambda _base_id: None,
                    is_refreshing=lambda _base_id: False,
                ),
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
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "deliver_final",
            AsyncMock(
                side_effect=lambda *_args, **_kwargs: (
                    sequence.append(("deliver", None, None, None))
                    or MagicMock(
                        event_id="$response_id",
                        response_text="Hello!",
                        delivery_kind="sent",
                    )
                ),
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
    bot._knowledge_access_support = _knowledge_access_support()
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
    bot._knowledge_access_support = _knowledge_access_support()
    bot._send_response = AsyncMock(return_value="$response_id")

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(_ctx: SessionHookContext) -> None:
        sequence.append("started")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    with (
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

    lifecycle = coordinator._build_lifecycle(
        response_kind="ai",
        request=replace(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            target=target,
        ),
    )
    watch = lifecycle.setup_session_watch(
        tool_context=tool_context,
        session_id=target.session_id,
        session_type=SessionType.AGENT,
        scope=HistoryScope(kind="agent", scope_id="general"),
        room_id=target.room_id,
        thread_id=target.resolved_thread_id,
        create_storage=BrokenStorage,
    )

    assert watch.should_watch is False
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
    bot._knowledge_access_support = _knowledge_access_support()
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
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "deliver_final",
            AsyncMock(
                side_effect=lambda *_args, **_kwargs: (
                    sequence.append("deliver")
                    or MagicMock(
                        event_id="$response_id",
                        response_text="Hello!",
                        delivery_kind="sent",
                    )
                ),
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
    bot._knowledge_access_support = _knowledge_access_support()
    bot._handle_interactive_question = AsyncMock()

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.session_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    with patch("mindroom.response_runner.stream_agent_response") as mock_stream:
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

        async def consume_delivery_and_fail(request: object) -> StreamTransportOutcome:
            chunks = [chunk async for chunk in request.response_stream]
            accumulated = "".join(chunks)
            sequence.append(f"deliver:{accumulated}")
            raise StreamingDeliveryError(
                RuntimeError("boom"),
                event_id="$terminal",
                accumulated_text=accumulated,
                tool_trace=[],
                transport_outcome=_stream_outcome(
                    "$terminal",
                    accumulated,
                    terminal_status="error",
                    failure_reason="boom",
                ),
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
async def test_process_and_respond_streaming_persists_interrupted_history_when_delivery_fails(
    tmp_path: Path,
) -> None:
    """Stream delivery errors should persist canonical interrupted replay from the partial text."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    storage = _SessionStorage()

    with patch("mindroom.response_runner.stream_agent_response") as mock_stream:
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@bob:localhost",
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def consume_delivery_and_fail(request: object) -> StreamTransportOutcome:
            accumulated = ""
            async for chunk in request.response_stream:
                accumulated += str(chunk)
            raise StreamingDeliveryError(
                RuntimeError("boom"),
                event_id="$terminal",
                accumulated_text="Partial answer\n\n**[Response interrupted by an error: boom]**",
                tool_trace=[],
                transport_outcome=_stream_outcome(
                    "$terminal",
                    "Partial answer\n\n**[Response interrupted by an error: boom]**",
                    terminal_status="error",
                    failure_reason="boom",
                ),
            )

        coordinator.deps.delivery_gateway.deliver_stream.side_effect = consume_delivery_and_fail

        def fake_stream_agent_response(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            async def fake_stream() -> AsyncIterator[str]:
                yield "Partial answer"

            return fake_stream()

        mock_stream.side_effect = fake_stream_agent_response

        delivery = await coordinator.process_and_respond_streaming(
            _response_request(prompt="Hello", user_id="@bob:localhost", thread_id="$thread-root"),
        )

    assert delivery.event_id == "$terminal"
    assert delivery.failure_reason == "boom"
    persisted_session = cast("AgentSession", storage.session)
    assert persisted_session is not None
    assert persisted_session.runs is not None
    persisted_run = cast("RunOutput", persisted_session.runs[0])
    assert persisted_run.messages is not None
    assert [(message.role, message.content) for message in persisted_run.messages] == [
        ("user", "Hello"),
        ("assistant", "Partial answer\n\n[interrupted]"),
    ]


def test_strip_visible_tool_markers_handles_blank_lined_markers() -> None:
    """The tool-marker stripper should leave bodies intact when markers are followed by blank lines."""
    text = "Intro\n\n🔧 `run_shell_command` [1]\n\n---\n\nBody"
    assert _strip_visible_tool_markers(text) == "Intro\n\n\nBody"


@pytest.mark.asyncio
async def test_process_and_respond_streaming_delivery_failure_with_visible_tools_replays_tool_trace_once(
    tmp_path: Path,
) -> None:
    """Visible streamed tool markers should be normalized out before interrupted replay persistence."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    storage = _SessionStorage()

    with (
        patch("mindroom.response_runner.stream_agent_response") as mock_stream,
        patch.object(ResponseRunner, "_show_tool_calls", return_value=True),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@bob:localhost",
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def consume_delivery_and_fail(_request: object) -> StreamTransportOutcome:
            raise StreamingDeliveryError(
                RuntimeError("boom"),
                event_id="$terminal",
                accumulated_text="Partial answer\n\n🔧 `run_shell_command` [1]\n\n**[Response interrupted by an error: boom]**",
                tool_trace=[
                    ToolTraceEntry(
                        type="tool_call_completed",
                        tool_name="run_shell_command",
                        args_preview="cmd=pwd",
                        result_preview="/app",
                    ),
                ],
                transport_outcome=_stream_outcome(
                    "$terminal",
                    "Partial answer\n\n🔧 `run_shell_command` [1]\n\n**[Response interrupted by an error: boom]**",
                    terminal_status="error",
                    failure_reason="boom",
                ),
            )

        coordinator.deps.delivery_gateway.deliver_stream.side_effect = consume_delivery_and_fail

        def fake_stream_agent_response(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            async def fake_stream() -> AsyncIterator[str]:
                yield "Partial answer"

            return fake_stream()

        mock_stream.side_effect = fake_stream_agent_response

        delivery = await coordinator.process_and_respond_streaming(
            _response_request(prompt="Hello", user_id="@bob:localhost", thread_id="$thread-root"),
        )

    assert delivery.event_id == "$terminal"
    persisted_session = cast("AgentSession", storage.session)
    assert persisted_session is not None
    assert persisted_session.runs is not None
    persisted_run = cast("RunOutput", persisted_session.runs[0])
    assistant_text = cast("str", persisted_run.messages[1].content)
    assert "🔧 `run_shell_command` [1]" not in assistant_text
    assert assistant_text.count("[tool:run_shell_command completed]") == 1
    assert assistant_text == (
        "Partial answer\n\n[tool:run_shell_command completed]\n  args: cmd=pwd\n  result: /app\n\n[interrupted]"
    )


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
    bot._knowledge_access_support = _knowledge_access_support()
    bot._send_response = AsyncMock(return_value="$response_id")

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.session_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    with (
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
        _set_gateway_method(coordinator.deps.delivery_gateway, "edit_text", AsyncMock())

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

        delivery = await coordinator.process_and_respond(
            replace(
                _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
                existing_event_id="$thinking",
            ),
        )

    assert delivery.terminal_status == "cancelled"
    assert _visible_response_event_id(delivery) == "$thinking"
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
    bot._knowledge_access_support = _knowledge_access_support()
    bot._handle_interactive_question = AsyncMock()

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.session_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    with patch("mindroom.response_runner.stream_agent_response") as mock_stream:
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

        async def consume_delivery_and_cancel(request: object) -> StreamTransportOutcome:
            accumulated = ""
            async for chunk in request.response_stream:
                accumulated += str(chunk)
                sequence.append(f"deliver:{accumulated}")
            return _stream_outcome("$msg_id", accumulated)

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
async def test_generate_response_locked_persists_minimal_interrupted_history_after_task_cancel(
    tmp_path: Path,
) -> None:
    """Lifecycle-owned agent cancellation should persist one minimal interrupted turn."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    storage = _SessionStorage()
    started = asyncio.Event()

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = cast("Callable[[str | None], Awaitable[object]]", kwargs["response_function"])
        task = asyncio.create_task(response_function("$thinking"))
        await started.wait()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return "$thinking"

    with (
        patch.object(
            ResponseRunner,
            "run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )
        _set_gateway_method(coordinator.deps.delivery_gateway, "edit_text", AsyncMock())

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            run_id_callback = cast("Callable[[str], None]", kwargs["run_id_callback"])
            run_id_callback("run-retry")
            started.set()
            await asyncio.sleep(60)
            return "unreachable"

        mock_ai.side_effect = fake_ai_response

        resolution = await coordinator.generate_response_locked(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

    assert resolution == "$thinking"
    persisted_session = cast("AgentSession", storage.session)
    assert persisted_session is not None
    assert persisted_session.runs is not None
    persisted_run = cast("RunOutput", persisted_session.runs[0])
    assert persisted_run.run_id == "run-retry"
    assert persisted_run.metadata is not None
    assert persisted_run.metadata["matrix_response_event_id"] == "$thinking"
    assert persisted_run.messages is not None
    assert persisted_run.messages[0].role == "user"
    assert "Hello" in cast("str", persisted_run.messages[0].content)
    assert [(message.role, message.content) for message in persisted_run.messages[-1:]] == [
        ("assistant", "[interrupted]"),
    ]


@pytest.mark.asyncio
async def test_generate_response_locked_hard_cancel_does_not_seed_seen_ids_with_active_response_events(
    tmp_path: Path,
) -> None:
    """Lifecycle-owned minimal interruption must not treat active bot replies as consumed user events."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    storage = _SessionStorage()
    started = asyncio.Event()

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = cast("Callable[[str | None], Awaitable[object]]", kwargs["response_function"])
        task = asyncio.create_task(response_function("$thinking"))
        await started.wait()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return "$thinking"

    with (
        patch.object(
            ResponseRunner,
            "run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch.object(ResponseRunner, "_active_response_event_ids", return_value={"$other-response"}),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )
        _set_gateway_method(coordinator.deps.delivery_gateway, "edit_text", AsyncMock())

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            run_id_callback = cast("Callable[[str], None]", kwargs["run_id_callback"])
            run_id_callback("run-retry")
            started.set()
            await asyncio.sleep(60)
            return "unreachable"

        mock_ai.side_effect = fake_ai_response

        resolution = await coordinator.generate_response_locked(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

    assert resolution == "$thinking"
    persisted_session = cast("AgentSession", storage.session)
    assert persisted_session is not None
    assert persisted_session.runs is not None
    persisted_run = cast("RunOutput", persisted_session.runs[0])
    assert persisted_run.metadata is not None
    assert persisted_run.metadata["matrix_seen_event_ids"] == ["$user_msg"]
    assert "$other-response" not in persisted_run.metadata["matrix_seen_event_ids"]


@pytest.mark.asyncio
async def test_generate_response_locked_finalizes_cancelled_task_before_delivery(
    tmp_path: Path,
) -> None:
    """Task cancellation before delivery should still emit the canonical cancelled lifecycle."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    cancelled_seen: list[str | None] = []

    @hook(EVENT_MESSAGE_CANCELLED)
    async def on_cancelled(ctx: CancelledResponseContext) -> None:
        cancelled_seen.append(ctx.info.failure_reason)

    registry = HookRegistry.from_plugins([_plugin("cancelled-hooks", [on_cancelled])])

    async def fake_run_cancellable_response(**kwargs: object) -> str | None:
        on_task_cancelled = cast("Callable[[str], None]", kwargs["on_cancelled"])
        on_task_cancelled("sync_restart_cancelled")
        return None

    with (
        patch.object(
            ResponseRunner,
            "run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
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

        resolution = await coordinator.generate_response_locked(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

    assert resolution is None
    assert cancelled_seen == ["sync_restart_cancelled"]


@pytest.mark.asyncio
async def test_early_cancellation_redacts_thinking_placeholder(
    tmp_path: Path,
) -> None:
    """Cancellation after Thinking... but before delivery starts should redact the placeholder."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    cancelled_seen: list[str | None] = []

    @hook(EVENT_MESSAGE_CANCELLED)
    async def on_cancelled(ctx: CancelledResponseContext) -> None:
        cancelled_seen.append(ctx.info.failure_reason)

    registry = HookRegistry.from_plugins([_plugin("early-cancel-cleanup", [on_cancelled])])

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        on_task_cancelled = cast("Callable[[str], None]", kwargs["on_cancelled"])
        on_task_cancelled("cancelled_by_user")
        return "$thinking"

    async def redact_message_event(*, room_id: str, event_id: str, reason: str) -> bool:
        assert room_id == "!test:localhost"
        assert event_id == "$thinking"
        assert reason == "Completed placeholder-only streamed response"
        assert cancelled_seen == []
        return True

    with (
        patch.object(
            ResponseRunner,
            "run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
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
        redact_mock = AsyncMock(side_effect=redact_message_event)
        object.__setattr__(coordinator.deps.delivery_gateway.deps, "redact_message_event", redact_mock)

        resolution = await coordinator.generate_response_locked(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

    assert resolution is None
    redact_mock.assert_awaited_once()
    assert cancelled_seen == ["cancelled_by_user"]


@pytest.mark.asyncio
async def test_generate_response_locked_returns_none_when_final_delivery_is_unhandled(
    tmp_path: Path,
) -> None:
    """A terminal unhandled delivery outcome should not mark the turn handled."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    storage = _SessionStorage()

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def fake_generate_non_streaming(
            *_args: object,
            **kwargs: object,
        ) -> str:
            turn_recorder = cast("TurnRecorder", kwargs["turn_recorder"])
            turn_recorder.set_run_id("run-delivery-cancel")
            turn_recorder.record_completed(
                run_metadata={},
                assistant_text="Hello!",
                completed_tools=[],
            )
            return "Hello!"

        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "deliver_final",
            AsyncMock(
                return_value=FinalDeliveryOutcome(
                    terminal_status="cancelled",
                    event_id=None,
                    failure_reason="delivery_cancelled",
                ),
            ),
        )

        with patch.object(
            ResponseRunner,
            "generate_non_streaming_ai_response",
            new=AsyncMock(side_effect=fake_generate_non_streaming),
        ):
            resolution = await coordinator.generate_response_locked(
                _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
                resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            )

    assert resolution is None
    assert storage.session is None


@pytest.mark.asyncio
async def test_generate_response_locked_unhandled_delivery_outcome_does_not_persist_tool_replay(
    tmp_path: Path,
) -> None:
    """An unhandled delivery outcome should not synthesize interrupted replay from visible tools."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    history_storage = _SessionStorage()
    ai_scope_storage = _SessionStorage()
    completed_run = RunOutput(
        run_id="run-visible-tools",
        agent_id="general",
        session_id="session1",
        content="Half done",
        messages=[Message(role="assistant", content="Half done")],
        tools=[
            ToolExecution(
                tool_name="run_shell_command",
                tool_args={"cmd": "pwd"},
                result="/app",
            ),
        ],
        status=RunStatus.completed,
    )

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch.object(ResponseRunner, "_show_tool_calls", return_value=True),
        patch(
            "mindroom.ai.open_resolved_scope_session_context",
            new=lambda **_: _open_agent_scope_context(ai_scope_storage),
        ),
        patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        patch("mindroom.ai_runtime.cached_agent_run", new_callable=AsyncMock, return_value=completed_run),
    ):
        mock_prepare.return_value = _prepared_prompt_result(MagicMock(), prompt="Hello")
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            history_storage=history_storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "deliver_final",
            AsyncMock(
                return_value=FinalDeliveryOutcome(
                    terminal_status="cancelled",
                    event_id=None,
                    failure_reason="delivery_cancelled",
                ),
            ),
        )

        resolution = await coordinator.generate_response_locked(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

    assert resolution is None
    assert history_storage.session is None


@pytest.mark.asyncio
async def test_generate_response_locked_preserves_visible_stream_when_finalize_returns_cancelled(
    tmp_path: Path,
) -> None:
    """Delivery-stage cancellation after streaming completes should still persist replay."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    storage = _SessionStorage()
    request = replace(
        _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
        response_envelope=MessageEnvelope(
            source_event_id="$user_msg",
            room_id="!test:localhost",
            target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            requester_id="@alice:localhost",
            sender_id="@alice:localhost",
            body="Hello",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name="general",
            source_kind="message",
        ),
        correlation_id="corr-stream-cancel",
    )

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def fake_generate_streaming(
            *_args: object,
            **kwargs: object,
        ) -> StreamTransportOutcome:
            turn_recorder = cast("TurnRecorder", kwargs["turn_recorder"])
            turn_recorder.set_run_id("run-stream-delivery-cancel")
            turn_recorder.record_completed(
                run_metadata={},
                assistant_text="Hello!",
                completed_tools=[],
            )
            return StreamTransportOutcome(
                last_physical_stream_event_id="$stream-msg",
                terminal_status="completed",
                rendered_body="Hello!",
                visible_body_state="visible_body",
            )

        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "finalize_streamed_response",
            AsyncMock(
                return_value=FinalDeliveryOutcome(
                    terminal_status="cancelled",
                    event_id="$stream-msg",
                    is_visible_response=True,
                    final_visible_body="Hello!",
                    delivery_kind="sent",
                    failure_reason="delivery_cancelled",
                ),
            ),
        )

        with patch.object(
            ResponseRunner,
            "generate_streaming_ai_response",
            new=AsyncMock(side_effect=fake_generate_streaming),
        ):
            resolution = await coordinator.generate_response_locked(
                request,
                resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            )

    assert resolution == "$stream-msg"
    assert storage.session is None


@pytest.mark.asyncio
async def test_generate_response_locked_preserves_visible_stream_on_late_finalize_error(
    tmp_path: Path,
) -> None:
    """Late streamed finalization errors should preserve the visible stream as an error outcome."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    storage = _SessionStorage()
    request = replace(
        _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
        response_envelope=MessageEnvelope(
            source_event_id="$user_msg",
            room_id="!test:localhost",
            target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            requester_id="@alice:localhost",
            sender_id="@alice:localhost",
            body="Hello",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name="general",
            source_kind="message",
        ),
        correlation_id="corr-stream-error",
    )

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def fake_generate_streaming(
            *_args: object,
            **kwargs: object,
        ) -> StreamTransportOutcome:
            turn_recorder = cast("TurnRecorder", kwargs["turn_recorder"])
            turn_recorder.set_run_id("run-stream-delivery-error")
            turn_recorder.record_completed(
                run_metadata={},
                assistant_text="Hello!",
                completed_tools=[],
            )
            return StreamTransportOutcome(
                last_physical_stream_event_id="$stream-msg",
                terminal_status="completed",
                rendered_body="Hello!",
                visible_body_state="visible_body",
            )

        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "finalize_streamed_response",
            AsyncMock(
                return_value=FinalDeliveryOutcome(
                    terminal_status="error",
                    event_id="$stream-msg",
                    is_visible_response=True,
                    final_visible_body="Hello!",
                    delivery_kind="sent",
                    failure_reason="delivery crash",
                ),
            ),
        )

        with patch.object(
            ResponseRunner,
            "generate_streaming_ai_response",
            new=AsyncMock(side_effect=fake_generate_streaming),
        ):
            resolution = await coordinator.generate_response_locked(
                request,
                resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            )

    assert resolution == "$stream-msg"


@pytest.mark.asyncio
async def test_process_and_respond_uses_resolved_thread_id_for_ai_logging_context(
    tmp_path: Path,
) -> None:
    """Non-streaming AI calls should receive the resolved thread root, not the raw request thread id."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with (
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

    with patch("mindroom.response_runner.stream_agent_response") as mock_stream:
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

    with patch("mindroom.response_runner.stream_agent_response") as mock_stream:
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
    """Plain streaming exceptions should propagate their text to the typed error outcome."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with (
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

        resolution = await coordinator.generate_response_locked(
            _response_request(prompt="Hello", user_id="@bob:localhost", thread_id="$thread-root"),
            resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

    assert resolution is None
    coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.assert_awaited_once()
    assert (
        coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.await_args.kwargs[
            "visible_response_event_id"
        ]
        is None
    )
    assert (
        coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.await_args.kwargs[
            "failure_reason"
        ]
        == "plain boom"
    )


@pytest.mark.asyncio
async def test_generate_team_response_helper_preserves_raw_prompt_when_model_prompt_supplies_tail(
    tmp_path: Path,
) -> None:
    """Team responses should keep the raw user text when model_prompt only adds transient tails."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch(
            "mindroom.response_runner.team_response",
            new=AsyncMock(return_value="Team answer"),
        ) as mock_team_response,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=_team_orchestrator(config, runtime_paths),
        )

        await coordinator.generate_team_response_helper(
            replace(
                _response_request(prompt="Describe this image", user_id="@alice:localhost", thread_id="$thread-root"),
                model_prompt="Available attachment IDs: att_1. Use tool calls to inspect or process them.",
            ),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    message = mock_team_response.await_args.kwargs["message"]
    assert "Describe this image" in message
    assert "Available attachment IDs: att_1" in message


@pytest.mark.asyncio
async def test_generate_response_marks_transient_model_prompt_for_cleanup(
    tmp_path: Path,
) -> None:
    """Agent responses should scrub transient per-turn model tails after the run."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new=AsyncMock(return_value="Hello")),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)) as mock_post,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        await coordinator.generate_response(
            replace(
                _response_request(prompt="Describe this image", user_id="@alice:localhost", thread_id="$thread-root"),
                model_prompt="Available attachment IDs: att_1. Use tool calls to inspect or process them.",
            ),
        )

    outcome = mock_post.await_args.args[1]
    assert outcome.strip_transient_enrichment_after_run is True


@pytest.mark.asyncio
async def test_generate_response_strips_transient_model_prompt_from_persisted_session(
    tmp_path: Path,
) -> None:
    """Post-response cleanup should restore persisted user text to the raw turn."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    storage = _SessionStorage()

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new=AsyncMock(return_value="Hello")),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def fake_send_text(*_args: object, **_kwargs: object) -> str:
            storage.session = AgentSession(
                session_id="!test:localhost:$thread-root",
                agent_id="general",
                created_at=1,
                updated_at=1,
                runs=[
                    RunOutput(
                        content="Hello",
                        messages=[
                            Message(role="user", content="Earlier context"),
                            Message(role="assistant", content="Earlier answer"),
                            Message(
                                role="user",
                                content=(
                                    "Describe this image\n\n"
                                    "Available attachment IDs: att_1. Use tool calls to inspect or process them."
                                ),
                            ),
                            Message(role="assistant", content="Hello"),
                        ],
                    ),
                ],
            )
            return "$msg"

        coordinator.deps.delivery_gateway.send_text.side_effect = fake_send_text

        await coordinator.generate_response(
            replace(
                _response_request(prompt="Describe this image", user_id="@alice:localhost", thread_id="$thread-root"),
                model_prompt="Available attachment IDs: att_1. Use tool calls to inspect or process them.",
            ),
        )

    persisted_session = cast("AgentSession", storage.session)
    assert persisted_session.runs is not None
    persisted_run = cast("RunOutput", persisted_session.runs[0])
    assert persisted_run.messages is not None
    assert persisted_run.messages[0].content == "Earlier context"
    assert persisted_run.messages[2].content == "Describe this image"


@pytest.mark.asyncio
async def test_generate_response_marks_matrix_tool_prompt_context_for_cleanup(tmp_path: Path) -> None:
    """Matrix tool metadata appended during runtime preparation should be scrubbed after the run."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_matrix_message(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    model_prompts: list[str] = []

    async def fake_ai_response(*_args: object, **kwargs: object) -> str:
        model_prompts.append(cast("str", kwargs["model_prompt"]))
        return "Hello"

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new=AsyncMock(side_effect=fake_ai_response)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)) as mock_post,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        await coordinator.generate_response(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
        )

    assert model_prompts
    assert "[Matrix metadata for tool calls]" in model_prompts[0]
    outcome = mock_post.await_args.args[1]
    assert outcome.strip_transient_enrichment_after_run is True
    assert outcome.memory_prompt == "Hello"


@pytest.mark.asyncio
async def test_generate_response_cleanup_targets_retry_run_id(tmp_path: Path) -> None:
    """Cleanup should target the persisted retry run instead of the abandoned first run id."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    storage = _SessionStorage()

    async def fake_ai_response(*_args: object, **kwargs: object) -> str:
        run_id_callback = cast("Callable[[str], None]", kwargs["run_id_callback"])
        run_id_callback("retry-run")
        return "Hello"

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new=AsyncMock(side_effect=fake_ai_response)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def fake_send_text(*_args: object, **_kwargs: object) -> str:
            storage.session = AgentSession(
                session_id="!test:localhost:$thread-root",
                agent_id="general",
                created_at=1,
                updated_at=1,
                runs=[
                    RunOutput(
                        run_id="retry-run",
                        content="Hello",
                        messages=[
                            Message(
                                role="user",
                                content=(
                                    "Describe this image\n\n"
                                    "Available attachment IDs: att_1. Use tool calls to inspect or process them."
                                ),
                            ),
                            Message(role="assistant", content="Hello"),
                        ],
                    ),
                ],
            )
            return "$msg"

        coordinator.deps.delivery_gateway.send_text.side_effect = fake_send_text

        await coordinator.generate_response(
            replace(
                _response_request(prompt="Describe this image", user_id="@alice:localhost", thread_id="$thread-root"),
                model_prompt="Available attachment IDs: att_1. Use tool calls to inspect or process them.",
            ),
        )

    persisted_session = cast("AgentSession", storage.session)
    assert persisted_session.runs is not None
    persisted_run = cast("RunOutput", persisted_session.runs[0])
    assert persisted_run.run_id == "retry-run"
    assert persisted_run.messages is not None
    assert persisted_run.messages[0].content == "Describe this image"


@pytest.mark.asyncio
async def test_generate_team_response_marks_transient_model_prompt_for_cleanup(
    tmp_path: Path,
) -> None:
    """Team responses should scrub transient per-turn model tails after the run."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.team_response", new=AsyncMock(return_value="Team answer")),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)) as mock_post,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=_team_orchestrator(config, runtime_paths),
        )

        await coordinator.generate_team_response_helper(
            replace(
                _response_request(prompt="Describe this image", user_id="@alice:localhost", thread_id="$thread-root"),
                model_prompt="Available attachment IDs: att_1. Use tool calls to inspect or process them.",
            ),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    outcome = mock_post.await_args.args[1]
    assert outcome.strip_transient_enrichment_after_run is True
    assert outcome.memory_prompt == "Describe this image"


@pytest.mark.asyncio
async def test_generate_team_response_strips_transient_model_prompt_from_persisted_session(
    tmp_path: Path,
) -> None:
    """Team cleanup should restore the current user turn, not earlier context."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    storage = _SessionStorage()

    async def fake_team_response(*_args: object, **_kwargs: object) -> str:
        storage.session = TeamSession(
            session_id="!test:localhost:$thread-root",
            team_id="ultimate",
            created_at=1,
            updated_at=1,
            runs=[
                TeamRunOutput(
                    content="Team answer",
                    messages=[
                        Message(role="user", content="Earlier context"),
                        Message(role="assistant", content="Earlier answer"),
                        Message(
                            role="user",
                            content=(
                                "Describe this image\n\n"
                                "Available attachment IDs: att_1. Use tool calls to inspect or process them."
                            ),
                        ),
                        Message(role="assistant", content="Team answer"),
                    ],
                ),
            ],
        )
        return "Team answer"

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.team_response", new=AsyncMock(side_effect=fake_team_response)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            team_history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=_team_orchestrator(config, runtime_paths),
        )

        await coordinator.generate_team_response_helper(
            replace(
                _response_request(prompt="Describe this image", user_id="@alice:localhost", thread_id="$thread-root"),
                model_prompt="Available attachment IDs: att_1. Use tool calls to inspect or process them.",
            ),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    persisted_session = cast("TeamSession", storage.session)
    assert persisted_session.runs is not None
    persisted_run = cast("TeamRunOutput", persisted_session.runs[0])
    assert persisted_run.messages is not None
    assert persisted_run.messages[0].content == "Earlier context"
    assert persisted_run.messages[2].content == "Describe this image"


@pytest.mark.asyncio
async def test_generate_team_response_cleanup_targets_retry_run_id(tmp_path: Path) -> None:
    """Team cleanup should target the persisted retry run instead of the abandoned first run id."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    storage = _SessionStorage()

    async def fake_team_response(*_args: object, **kwargs: object) -> str:
        run_id_callback = cast("Callable[[str], None]", kwargs["run_id_callback"])
        run_id_callback("team-retry-run")
        return "Team answer"

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.team_response", new=AsyncMock(side_effect=fake_team_response)),
        patch(
            "mindroom.teams.open_bound_scope_session_context",
            side_effect=lambda **_kwargs: _open_team_scope_context(storage),
        ),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            team_history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=_team_orchestrator(config, runtime_paths),
        )

        async def fake_send_text(*_args: object, **_kwargs: object) -> str:
            storage.session = TeamSession(
                session_id="!test:localhost:$thread-root",
                team_id="ultimate",
                created_at=1,
                updated_at=1,
                runs=[
                    TeamRunOutput(
                        run_id="team-retry-run",
                        content="Team answer",
                        messages=[
                            Message(
                                role="user",
                                content=(
                                    "Describe this image\n\n"
                                    "Available attachment IDs: att_1. Use tool calls to inspect or process them."
                                ),
                            ),
                            Message(role="assistant", content="Team answer"),
                        ],
                    ),
                ],
            )
            return "$msg"

        coordinator.deps.delivery_gateway.send_text.side_effect = fake_send_text

        await coordinator.generate_team_response_helper(
            replace(
                _response_request(prompt="Describe this image", user_id="@alice:localhost", thread_id="$thread-root"),
                model_prompt="Available attachment IDs: att_1. Use tool calls to inspect or process them.",
            ),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    persisted_session = cast("TeamSession", storage.session)
    assert persisted_session.runs is not None
    persisted_run = cast("TeamRunOutput", persisted_session.runs[0])
    assert persisted_run.run_id == "team-retry-run"
    assert persisted_run.messages is not None
    assert persisted_run.messages[0].content == "Describe this image"


def test_strip_transient_enrichment_targets_response_run_id() -> None:
    """Cleanup should not rewrite a newer or older run when the finished run id is known."""
    transient_system_context = render_system_enrichment_block(
        (EnrichmentItem(key="weather", text="72F and sunny", cache_policy="volatile"),),
    )
    storage = _SessionStorage(
        AgentSession(
            session_id="session-1",
            agent_id="general",
            runs=[
                RunOutput(
                    run_id="older-run",
                    messages=[Message(role="user", content="older enriched prompt")],
                ),
                RunOutput(
                    run_id="target-run",
                    messages=[
                        Message(role="system", content=f"durable system context\n\n{transient_system_context}"),
                        Message(role="user", content="current enriched prompt"),
                    ],
                ),
                RunOutput(
                    run_id="newer-run",
                    messages=[Message(role="user", content="newer enriched prompt")],
                ),
            ],
        ),
    )
    storage_view = storage.open()

    changed = strip_transient_enrichment_from_session(
        storage_view,
        session_id="session-1",
        session_type=SessionType.AGENT,
        response_run_id="target-run",
        memory_prompt="current raw prompt",
        transient_system_context=transient_system_context,
    )

    persisted_session = cast("AgentSession", storage.session)
    assert changed is True
    assert persisted_session.runs is not None
    assert cast("RunOutput", persisted_session.runs[0]).messages[0].content == "older enriched prompt"
    target_messages = cast("RunOutput", persisted_session.runs[1]).messages
    assert target_messages is not None
    assert target_messages[0].content == "durable system context"
    assert target_messages[1].content == "current raw prompt"
    assert cast("RunOutput", persisted_session.runs[2]).messages[0].content == "newer enriched prompt"


def test_strip_transient_enrichment_strips_nested_team_member_system_context() -> None:
    """Team cleanup should recurse into persisted member responses."""
    transient_system_context = render_system_enrichment_block(
        (EnrichmentItem(key="weather", text="72F and sunny", cache_policy="volatile"),),
    )
    storage = _SessionStorage(
        TeamSession(
            session_id="session-1",
            team_id="ultimate",
            runs=[
                TeamRunOutput(
                    run_id="target-run",
                    messages=[
                        Message(role="system", content=f"durable team context\n\n{transient_system_context}"),
                        Message(role="user", content="current enriched prompt"),
                    ],
                    member_responses=[
                        RunOutput(
                            run_id="member-run",
                            messages=[
                                Message(
                                    role="system",
                                    content=f"durable member context\n\n{transient_system_context}",
                                ),
                                Message(role="assistant", content="member answer"),
                            ],
                        ),
                    ],
                ),
            ],
        ),
    )
    storage_view = storage.open()

    changed = strip_transient_enrichment_from_session(
        storage_view,
        session_id="session-1",
        session_type=SessionType.TEAM,
        response_run_id="target-run",
        memory_prompt="current raw prompt",
        transient_system_context=transient_system_context,
    )

    persisted_session = cast("TeamSession", storage.session)
    assert changed is True
    assert persisted_session.runs is not None
    target_run = cast("TeamRunOutput", persisted_session.runs[0])
    assert target_run.messages is not None
    assert target_run.messages[0].content == "durable team context"
    assert target_run.messages[1].content == "current raw prompt"
    assert target_run.member_responses is not None
    member_run = cast("RunOutput", target_run.member_responses[0])
    assert member_run.messages is not None
    assert member_run.messages[0].content == "durable member context"


@pytest.mark.asyncio
async def test_generate_response_cleanup_uses_model_time_knowledge_notice(tmp_path: Path) -> None:
    """Cleanup should use the exact knowledge notice appended before the model run."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    knowledge_access_support = _knowledge_access_support()
    knowledge_access_support.resolve_for_agent.side_effect = [
        KnowledgeResolution(
            knowledge=None,
            unavailable={
                "docs": KnowledgeAvailabilityDetail(
                    availability=KnowledgeAvailability.INITIALIZING,
                    search_available=False,
                ),
            },
        ),
        KnowledgeResolution(knowledge=None, unavailable={}),
    ]

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new=AsyncMock(return_value="Hello")),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)) as mock_post,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            knowledge_access_support=knowledge_access_support,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        await coordinator.generate_response(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
        )

    outcome = mock_post.await_args.args[1]
    assert outcome.strip_transient_enrichment_after_run is True
    assert "knowledge_availability" in outcome.transient_system_context
    assert "Knowledge base `docs` is initializing" in outcome.transient_system_context
    assert knowledge_access_support.resolve_for_agent.call_count == 1


@pytest.mark.asyncio
async def test_generate_response_streaming_cleanup_uses_model_time_knowledge_notice(tmp_path: Path) -> None:
    """Streaming cleanup should use the exact knowledge notice appended before the model run."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    knowledge_access_support = _knowledge_access_support()
    knowledge_access_support.resolve_for_agent.side_effect = [
        KnowledgeResolution(
            knowledge=None,
            unavailable={
                "docs": KnowledgeAvailabilityDetail(
                    availability=KnowledgeAvailability.INITIALIZING,
                    search_available=False,
                ),
            },
        ),
        KnowledgeResolution(knowledge=None, unavailable={}),
    ]

    async def fake_stream_agent_response(*_args: object, **_kwargs: object) -> AsyncGenerator[str, None]:
        yield "Hello"

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_runner.stream_agent_response", new=fake_stream_agent_response),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)) as mock_post,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            knowledge_access_support=knowledge_access_support,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        await coordinator.generate_response(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
        )

    outcome = mock_post.await_args.args[1]
    assert outcome.strip_transient_enrichment_after_run is True
    assert "knowledge_availability" in outcome.transient_system_context
    assert "Knowledge base `docs` is initializing" in outcome.transient_system_context
    assert knowledge_access_support.resolve_for_agent.call_count == 1


def test_append_knowledge_availability_notice_rendering() -> None:
    """Knowledge availability notices should render as transient system enrichment."""
    rendered_context = render_system_enrichment_block(
        append_knowledge_availability_enrichment(
            (),
            {
                "docs": KnowledgeAvailabilityDetail(
                    availability=KnowledgeAvailability.INITIALIZING,
                    search_available=False,
                ),
            },
        ),
    )

    assert "knowledge_availability" in rendered_context
    assert "Knowledge base `docs` is initializing" in rendered_context


@pytest.mark.asyncio
async def test_stream_with_request_log_context_closes_wrapped_stream_on_early_close() -> None:
    """Closing the wrapper should immediately close the provider stream."""
    closed = False

    async def source() -> AsyncGenerator[str, None]:
        nonlocal closed
        try:
            yield "first"
            yield "second"
        finally:
            closed = True

    stream = _stream_with_request_log_context(source(), request_context={})

    assert await anext(stream) == "first"
    await stream.aclose()

    assert closed is True


@pytest.mark.asyncio
async def test_generate_response_marks_system_enrichment_for_cleanup(
    tmp_path: Path,
) -> None:
    """Agent responses should scrub transient system enrichment after the run."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new=AsyncMock(return_value="Hello")),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)) as mock_post,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        await coordinator.generate_response(
            replace(
                _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
                system_enrichment_items=(EnrichmentItem(key="weather", text="72F and sunny"),),
            ),
        )

    outcome = mock_post.await_args.args[1]
    assert outcome.strip_transient_enrichment_after_run is True


@pytest.mark.asyncio
async def test_generate_team_response_marks_system_enrichment_for_cleanup(
    tmp_path: Path,
) -> None:
    """Team responses should scrub transient system enrichment after the run."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.team_response", new=AsyncMock(return_value="Team answer")),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)) as mock_post,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=_team_orchestrator(config, runtime_paths),
        )

        await coordinator.generate_team_response_helper(
            replace(
                _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
                system_enrichment_items=(EnrichmentItem(key="weather", text="72F and sunny"),),
            ),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    outcome = mock_post.await_args.args[1]
    assert outcome.strip_transient_enrichment_after_run is True


def test_compose_current_turn_prompt_uses_normalized_tail_comparison() -> None:
    """Whitespace-normalized model prompts should not duplicate the raw turn."""
    prompt = _compose_current_turn_prompt(
        raw_prompt=" report ",
        model_prompt="[2026-03-20 08:15 PDT] report\n\nAvailable attachment IDs: att_report.",
        prompt_parts=MemoryPromptParts(session_preamble="", turn_context=""),
    )

    assert prompt == " report \n\nAvailable attachment IDs: att_report."


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
    bot._knowledge_access_support = _knowledge_access_support()

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

        async def consume_delivery_and_fail(request: object) -> StreamTransportOutcome:
            chunks = [chunk async for chunk in request.response_stream]
            accumulated = "".join(chunks)
            sequence.append(f"deliver:{accumulated}")
            raise StreamingDeliveryError(
                RuntimeError("boom"),
                event_id="$team-terminal",
                accumulated_text=accumulated,
                tool_trace=[],
                transport_outcome=_stream_outcome(
                    "$team-terminal",
                    accumulated,
                    terminal_status="error",
                    failure_reason="boom",
                ),
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

        resolution = await coordinator.generate_team_response_helper(
            request,
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    assert resolution == "$team-terminal"
    assert sequence == [
        "stream",
        "deliver:Team hello",
        "started:team:ultimate:!test:localhost:$thread-root:$thread-root",
    ]


@pytest.mark.asyncio
async def test_generate_team_response_helper_persists_interrupted_history_when_stream_delivery_fails(
    tmp_path: Path,
) -> None:
    """Team stream delivery errors should persist canonical interrupted replay from the partial text."""
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

        async def consume_delivery_and_fail(request: object) -> StreamTransportOutcome:
            accumulated = ""
            async for chunk in request.response_stream:
                accumulated += str(chunk)
            raise StreamingDeliveryError(
                RuntimeError("boom"),
                event_id="$team-terminal",
                accumulated_text="Team hello\n\n**[Response interrupted by an error: boom]**",
                tool_trace=[],
                transport_outcome=_stream_outcome(
                    "$team-terminal",
                    "Team hello\n\n**[Response interrupted by an error: boom]**",
                    terminal_status="error",
                    failure_reason="boom",
                ),
            )

        coordinator.deps.delivery_gateway.deliver_stream.side_effect = consume_delivery_and_fail

        def fake_team_response_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            async def fake_stream() -> AsyncIterator[str]:
                yield "Team hello"

            return fake_stream()

        mock_team_stream.side_effect = fake_team_response_stream

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    assert resolution == "$team-terminal"
    persisted_session = cast("TeamSession", storage.session)
    assert persisted_session is not None
    assert persisted_session.runs is not None
    persisted_run = cast("TeamRunOutput", persisted_session.runs[0])
    assert persisted_run.messages is not None
    assert [(message.role, message.content) for message in persisted_run.messages] == [
        ("user", "Hello"),
        ("assistant", "Team hello\n\n[interrupted]"),
    ]


@pytest.mark.asyncio
async def test_generate_team_response_helper_stream_delivery_failure_with_visible_tools_replays_tool_trace_once(
    tmp_path: Path,
) -> None:
    """Team stream delivery failures should not persist visible tool markers alongside replay traces."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    storage = _SessionStorage()

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_runner.team_response_stream") as mock_team_stream,
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch.object(ResponseRunner, "_show_tool_calls", return_value=True),
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

        async def consume_delivery_and_fail(_request: object) -> StreamTransportOutcome:
            raise StreamingDeliveryError(
                RuntimeError("boom"),
                event_id="$team-terminal",
                accumulated_text=(
                    "🤝 **Team Response** (General):\n\nTeam hello\n\n"
                    "🔧 `run_shell_command` [1]\n\n"
                    "**[Response interrupted by an error: boom]**"
                ),
                tool_trace=[
                    ToolTraceEntry(
                        type="tool_call_completed",
                        tool_name="run_shell_command",
                        args_preview="cmd=pwd",
                        result_preview="/app",
                    ),
                ],
                transport_outcome=_stream_outcome(
                    "$team-terminal",
                    (
                        "🤝 **Team Response** (General):\n\nTeam hello\n\n"
                        "🔧 `run_shell_command` [1]\n\n"
                        "**[Response interrupted by an error: boom]**"
                    ),
                    terminal_status="error",
                    failure_reason="boom",
                ),
            )

        coordinator.deps.delivery_gateway.deliver_stream.side_effect = consume_delivery_and_fail

        def fake_team_response_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            async def fake_stream() -> AsyncIterator[str]:
                yield "🤝 **Team Response** (General):\n\nTeam hello"

            return fake_stream()

        mock_team_stream.side_effect = fake_team_response_stream

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    assert resolution == "$team-terminal"
    persisted_session = cast("TeamSession", storage.session)
    assert persisted_session is not None
    assert persisted_session.runs is not None
    persisted_run = cast("TeamRunOutput", persisted_session.runs[0])
    assistant_text = cast("str", persisted_run.messages[1].content)
    assert "🔧 `run_shell_command` [1]" not in assistant_text
    assert assistant_text.count("[tool:run_shell_command completed]") == 1
    assert assistant_text == (
        "🤝 **Team Response** (General):\n\nTeam hello\n\n"
        "[tool:run_shell_command completed]\n"
        "  args: cmd=pwd\n"
        "  result: /app\n\n"
        "[interrupted]"
    )


@pytest.mark.asyncio
async def test_generate_team_response_helper_persists_minimal_interrupted_history_after_task_cancel(
    tmp_path: Path,
) -> None:
    """Lifecycle-owned team cancellation should persist one minimal interrupted turn."""
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
    bot._knowledge_access_support = _knowledge_access_support()

    storage = _SessionStorage()
    started = asyncio.Event()

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = cast("Callable[[str | None], Awaitable[object]]", kwargs["response_function"])
        task = asyncio.create_task(response_function("$thinking"))
        await started.wait()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return "$thinking"

    with (
        patch.object(
            ResponseRunner,
            "run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch("mindroom.response_runner.team_response", new_callable=AsyncMock) as mock_team_response,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            team_history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=_team_orchestrator(config, runtime_paths),
        )
        _set_gateway_method(coordinator.deps.delivery_gateway, "edit_text", AsyncMock())

        async def fake_team_response(*_args: object, **_kwargs: object) -> str:
            started.set()
            await asyncio.sleep(60)
            return "unreachable"

        mock_team_response.side_effect = fake_team_response

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    assert resolution == "$thinking"
    persisted_session = cast("TeamSession", storage.session)
    assert persisted_session is not None
    assert persisted_session.runs is not None
    persisted_run = cast("TeamRunOutput", persisted_session.runs[0])
    assert persisted_run.messages is not None
    assert persisted_run.messages[0].role == "user"
    assert "Hello" in cast("str", persisted_run.messages[0].content)
    assert [(message.role, message.content) for message in persisted_run.messages[-1:]] == [
        ("assistant", "[interrupted]"),
    ]


@pytest.mark.asyncio
async def test_generate_team_response_helper_persists_interrupted_history_when_final_delivery_is_cancelled(
    tmp_path: Path,
) -> None:
    """Delivery-stage cancellation after a completed team run should still persist replay."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    storage = _SessionStorage()

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch("mindroom.response_runner.team_response", new_callable=AsyncMock) as mock_team_response,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            team_history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=_team_orchestrator(config, runtime_paths),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "deliver_final",
            AsyncMock(side_effect=asyncio.CancelledError("delivery cancel")),
        )

        async def fake_team_response(*_args: object, **kwargs: object) -> str:
            turn_recorder = cast("TurnRecorder", kwargs["turn_recorder"])
            turn_recorder.set_run_id("team-run-delivery-cancel")
            turn_recorder.record_completed(
                run_metadata={},
                assistant_text="🤝 Team Response:\n\nTeam hello",
                completed_tools=[],
            )
            return "🤝 Team Response:\n\nTeam hello"

        mock_team_response.side_effect = fake_team_response

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    assert resolution is None
    persisted_session = cast("TeamSession", storage.session)
    assert persisted_session is not None
    assert persisted_session.runs is not None
    persisted_run = cast("TeamRunOutput", persisted_session.runs[0])
    assert persisted_run.run_id == "team-run-delivery-cancel"
    assert persisted_run.messages is not None
    assert [(message.role, message.content) for message in persisted_run.messages] == [
        ("user", "Hello"),
        ("assistant", "🤝 Team Response:\n\nTeam hello\n\n[interrupted]"),
    ]


@pytest.mark.asyncio
async def test_generate_team_response_helper_preserves_visible_stream_when_finalize_returns_cancelled(
    tmp_path: Path,
) -> None:
    """Delivery-stage cancellation after team streaming completes should still persist replay."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    storage = _SessionStorage()

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch("mindroom.response_runner.team_response_stream") as mock_team_stream,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            team_history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=_team_orchestrator(config, runtime_paths),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "finalize_streamed_response",
            AsyncMock(
                return_value=FinalDeliveryOutcome(
                    terminal_status="cancelled",
                    event_id="$team-msg",
                    is_visible_response=True,
                    final_visible_body="Team hello",
                    delivery_kind="sent",
                    failure_reason="delivery_cancelled",
                ),
            ),
        )

        async def consume_stream(request: object) -> StreamTransportOutcome:
            accumulated = ""
            async for chunk in request.response_stream:
                accumulated += str(chunk)
            return _stream_outcome("$team-msg", accumulated)

        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "deliver_stream",
            AsyncMock(side_effect=consume_stream),
        )

        def fake_team_response_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            async def fake_stream() -> AsyncIterator[str]:
                yield "Team hello"

            return fake_stream()

        mock_team_stream.side_effect = fake_team_response_stream

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    assert resolution == "$team-msg"
    assert storage.session is None


@pytest.mark.asyncio
async def test_generate_team_response_helper_preserves_structured_stream_cancel_delivery_state(
    tmp_path: Path,
) -> None:
    """Structured team stream cancellation must flow through the dedicated cancelled-delivery path."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    storage = _SessionStorage()

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch("mindroom.response_runner.team_response_stream"),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            team_history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=_team_orchestrator(config, runtime_paths),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "deliver_stream",
            AsyncMock(
                side_effect=StreamingDeliveryError(
                    asyncio.CancelledError("team stream cancelled"),
                    event_id="$team-msg",
                    accumulated_text="Team hello",
                    tool_trace=[],
                    transport_outcome=_stream_outcome(
                        "$team-msg",
                        "Team hello",
                        terminal_status="cancelled",
                        failure_reason="cancelled_by_user",
                    ),
                ),
            ),
        )

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    assert resolution == "$team-msg"
    persisted_session = cast("TeamSession", storage.session)
    assert persisted_session is not None
    assert persisted_session.runs is not None
    persisted_run = cast("TeamRunOutput", persisted_session.runs[0])
    assert persisted_run.messages is not None
    assert [(message.role, message.content) for message in persisted_run.messages] == [
        ("user", "Hello"),
        ("assistant", "Team hello\n\n[interrupted]"),
    ]


@pytest.mark.asyncio
async def test_generate_team_response_helper_preserves_visible_stream_on_late_finalize_error(
    tmp_path: Path,
) -> None:
    """Late streamed team finalization errors should preserve the visible stream as an error outcome."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    storage = _SessionStorage()

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch("mindroom.response_runner.team_response_stream") as mock_team_stream,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            team_history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=_team_orchestrator(config, runtime_paths),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "finalize_streamed_response",
            AsyncMock(
                return_value=FinalDeliveryOutcome(
                    terminal_status="error",
                    event_id="$team-msg",
                    is_visible_response=True,
                    final_visible_body="Team hello",
                    delivery_kind="sent",
                    failure_reason="delivery crash",
                ),
            ),
        )

        async def consume_stream(request: object) -> StreamTransportOutcome:
            accumulated = ""
            async for chunk in request.response_stream:
                accumulated += str(chunk)
            return _stream_outcome("$team-msg", accumulated)

        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "deliver_stream",
            AsyncMock(side_effect=consume_stream),
        )

        def fake_team_response_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            async def fake_stream() -> AsyncIterator[str]:
                yield "Team hello"

            return fake_stream()

        mock_team_stream.side_effect = fake_team_response_stream

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    assert resolution == "$team-msg"


@pytest.mark.asyncio
async def test_generate_team_response_helper_routes_placeholder_only_late_failure_through_cleanup_boundary(
    tmp_path: Path,
) -> None:
    """Raw late team failures after sending only Thinking... should use the placeholder cleanup path."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch("mindroom.response_runner.team_response_stream"),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=_team_orchestrator(config, runtime_paths),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "deliver_stream",
            AsyncMock(side_effect=RuntimeError("stream boom")),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "finalize_streamed_response",
            AsyncMock(
                return_value=FinalDeliveryOutcome(
                    terminal_status="error",
                    event_id=None,
                    failure_reason="stream boom",
                ),
            ),
        )

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    assert resolution is None
    coordinator.deps.delivery_gateway.finalize_streamed_response.assert_awaited_once()
    coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.assert_awaited_once()


def test_record_stream_delivery_error_preserves_hidden_tool_state_when_visible_trace_is_empty(
    tmp_path: Path,
) -> None:
    """Delivery failures must keep hidden tool progress already recorded by the stream generator."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    coordinator = _build_response_runner(
        bot,
        config=config,
        runtime_paths=runtime_paths,
        storage_path=tmp_path,
        requester_id="@alice:localhost",
    )
    recorder = TurnRecorder(user_message="Hello")
    recorder.set_run_metadata({"matrix_seen_event_ids": ["$user_msg"]})
    recorder.set_assistant_text("Partial answer")
    recorder.set_completed_tools(
        [
            ToolTraceEntry(
                type="tool_call_completed",
                tool_name="run_shell_command",
                args_preview="cmd=pwd",
                result_preview="/app",
            ),
        ],
    )
    recorder.set_interrupted_tools(
        [
            ToolTraceEntry(
                type="tool_call_started",
                tool_name="save_file",
                args_preview="file_name=main.py",
            ),
        ],
    )

    assert coordinator._record_stream_delivery_error(
        recorder=recorder,
        accumulated_text="Partial answer\n\n**[Response interrupted by an error: boom]**",
        tool_trace=[],
    )

    snapshot = recorder.interrupted_snapshot()
    assert snapshot.partial_text == "Partial answer"
    assert [tool.tool_name for tool in snapshot.completed_tools] == ["run_shell_command"]
    assert [tool.tool_name for tool in snapshot.interrupted_tools] == ["save_file"]


@pytest.mark.asyncio
async def test_generate_team_response_helper_persists_original_user_message_for_cancelled_team_run(
    tmp_path: Path,
) -> None:
    """Cancelled team replay should store the raw user turn, not the shaped model prompt."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    storage = _SessionStorage()
    model_prompts: list[str] = []

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = cast("Callable[[str | None], Awaitable[object]]", kwargs["response_function"])
        on_cancelled = cast("Callable[[str], None] | None", kwargs.get("on_cancelled"))
        with suppress(asyncio.CancelledError):
            await response_function("$thinking")
        if on_cancelled is not None:
            on_cancelled("cancelled_by_user")
        return "$thinking"

    with (
        patch.object(
            ResponseRunner,
            "run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch("mindroom.teams.Team.arun", new_callable=AsyncMock) as mock_team_arun,
        patch(
            "mindroom.teams.open_bound_scope_session_context",
            side_effect=lambda **_kwargs: _open_team_scope_context(storage),
        ),
    ):
        orchestrator = _team_orchestrator(config, runtime_paths)
        orchestrator.agent_bots = {"general": SimpleNamespace(running=True)}
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            team_history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=orchestrator,
        )

        async def fake_team_arun(prompt: str, **kwargs: object) -> TeamRunOutput:
            model_prompts.append(prompt)
            return TeamRunOutput(
                run_id=cast("str | None", kwargs.get("run_id")),
                team_id="ultimate",
                session_id=cast("str | None", kwargs.get("session_id")),
                content="Run cancelled",
                messages=[Message(role="assistant", content="Run cancelled")],
                status=RunStatus.cancelled,
            )

        mock_team_arun.side_effect = fake_team_arun

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    assert resolution == "$thinking"
    assert model_prompts
    assert model_prompts[0] != "Hello"
    assert 'Current message:\n<msg from="@alice:localhost">' in model_prompts[0]
    assert "Hello" in model_prompts[0]
    persisted_session = cast("TeamSession", storage.session)
    assert persisted_session is not None
    assert persisted_session.runs is not None
    persisted_run = cast("TeamRunOutput", persisted_session.runs[0])
    assert persisted_run.messages is not None
    assert [(message.role, message.content) for message in persisted_run.messages] == [
        ("user", "Hello"),
        ("assistant", "[interrupted]"),
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
    bot._knowledge_access_support = _knowledge_access_support()

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.scope.key}:{ctx.session_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = cast("Callable[[str | None], Awaitable[object]]", kwargs["response_function"])
        on_cancelled = cast("Callable[[str], None] | None", kwargs.get("on_cancelled"))
        with suppress(asyncio.CancelledError):
            await response_function("$thinking")
        if on_cancelled is not None:
            on_cancelled("cancelled_by_user")
        return "$thinking"

    with (
        patch.object(
            ResponseRunner,
            "run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
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
        _set_gateway_method(coordinator.deps.delivery_gateway, "edit_text", AsyncMock())

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

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    assert resolution == "$thinking"
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
    bot._knowledge_access_support = _knowledge_access_support()

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.scope.key}:{ctx.session_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = cast("Callable[[str | None], Awaitable[object]]", kwargs["response_function"])
        on_cancelled = cast("Callable[[str], None] | None", kwargs.get("on_cancelled"))
        with suppress(asyncio.CancelledError):
            await response_function("$thinking")
        if on_cancelled is not None:
            on_cancelled("cancelled_by_user")
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

        async def consume_delivery_and_cancel(request: object) -> StreamTransportOutcome:
            accumulated = ""
            async for chunk in request.response_stream:
                accumulated += str(chunk)
                sequence.append(f"deliver:{accumulated}")
            return _stream_outcome("$team-msg", accumulated)

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

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    assert resolution is None
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
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
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

        resolution = await coordinator.generate_team_response_helper(
            _response_request(
                prompt="What is in the image?",
                model_prompt="Available attachment IDs: att_img. Use tool calls to inspect or process them.",
                user_id="@alice:localhost",
                thread_id="$thread-root",
            ),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    assert _handled_response_event_id(resolution) == "$thinking"
    assert mock_team_response.await_args is not None
    message = mock_team_response.await_args.kwargs["message"]
    assert "What is in the image?" in message
    assert "Available attachment IDs: att_img. Use tool calls to inspect or process them." in message


@pytest.mark.asyncio
async def test_generate_team_response_helper_uses_delivery_result_failure_reason_for_cancelled_stream(
    tmp_path: Path,
) -> None:
    """Typed gateway outcomes should preserve their canonical failure_reason."""
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
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "deliver_stream",
            AsyncMock(
                return_value=StreamTransportOutcome(
                    last_physical_stream_event_id="$team-msg",
                    terminal_status="completed",
                    rendered_body="Team hello",
                    visible_body_state="visible_body",
                ),
            ),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "finalize_streamed_response",
            AsyncMock(
                return_value=FinalDeliveryOutcome(
                    terminal_status="error",
                    event_id=None,
                    failure_reason="stream failure",
                ),
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

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            team_agents=[MatrixID.from_agent("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    assert resolution is None


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
        bot._knowledge_access_support = _knowledge_access_support()
        bot._send_response = AsyncMock(return_value="$response_id")
        with patch("mindroom.response_runner.ai_response") as mock_ai:
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
        bot._knowledge_access_support = _knowledge_access_support()
        bot._handle_interactive_question = AsyncMock()
        with patch("mindroom.response_runner.stream_agent_response") as mock_stream:
            coordinator = _build_response_runner(
                bot,
                config=config,
                runtime_paths=runtime_paths,
                storage_path=tmp_path,
                requester_id="@bob:localhost",
            )

            async def consume_delivery(request: object) -> StreamTransportOutcome:
                response_stream = request.response_stream
                chunks = [chunk async for chunk in response_stream]
                return _stream_outcome("$msg_id", "".join(chunks))

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

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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
            compaction_decision=None,
            post_response_compaction_checks=[],
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
        assert mock_agent.additional_context == "existing context\n\nsession preamble\n\nsystem enrichment"

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
            patch("mindroom.ai_runtime.cached_agent_run", new_callable=AsyncMock, return_value=mock_run_output),
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
            patch("mindroom.ai_runtime.cached_agent_run", new_callable=AsyncMock, return_value=mock_run_output),
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
    async def test_ai_response_passes_current_sender_for_matrix_guidance(self, tmp_path: Path) -> None:
        """Matrix turns should preserve the sender who produced the current prompt."""
        mock_agent = MagicMock()
        mock_run_output = MagicMock()
        mock_run_output.content = "Response"
        mock_run_output.tools = None

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai_runtime.cached_agent_run", new_callable=AsyncMock, return_value=mock_run_output),
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            await ai_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                user_id="@alice:example.com",
            )

        assert mock_prepare.await_args.kwargs["current_sender_id"] == "@alice:example.com"

    @pytest.mark.asyncio
    async def test_ai_response_passes_raw_prompt_separately_from_model_prompt(self, tmp_path: Path) -> None:
        """The AI entrypoint should preserve the raw user prompt when model_prompt is provided."""
        mock_agent = MagicMock()
        mock_run_output = MagicMock()
        mock_run_output.content = "Response"
        mock_run_output.tools = None

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai_runtime.cached_agent_run", new_callable=AsyncMock, return_value=mock_run_output),
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

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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
    async def test_stream_agent_response_passes_current_sender_for_matrix_guidance(self, tmp_path: Path) -> None:
        """Streaming Matrix turns should preserve current-sender prompt attribution."""
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
                    user_id="@alice:example.com",
                )
            ]

        assert mock_prepare.await_args.kwargs["current_sender_id"] == "@alice:example.com"

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

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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
            patch("mindroom.ai_runtime.cached_agent_run", new_callable=AsyncMock, return_value=mock_run_output),
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
    async def test_ai_response_persists_interrupted_replay_for_cancelled_runs(self, tmp_path: Path) -> None:
        """Cancelled runs should be rewritten into canonical completed replay history."""
        storage = _SessionStorage()
        mock_agent = MagicMock()
        cancelled_run = RunOutput(
            run_id="run-123",
            agent_id="general",
            session_id="session1",
            content="Half done",
            messages=[Message(role="assistant", content="Half done")],
            tools=[
                ToolExecution(
                    tool_name="run_shell_command",
                    tool_args={"cmd": "pwd"},
                    result="/app",
                ),
            ],
            status=RunStatus.cancelled,
        )

        with (
            patch(
                "mindroom.ai.open_resolved_scope_session_context",
                new=lambda **_: _open_agent_scope_context(storage),
            ),
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai_runtime.cached_agent_run", new_callable=AsyncMock, return_value=cancelled_run),
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            with pytest.raises(asyncio.CancelledError):
                await ai_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    reply_to_event_id="e1",
                    show_tool_calls=False,
                )

        persisted_session = cast("AgentSession", storage.session)
        assert persisted_session.runs is not None
        persisted_run = cast("RunOutput", persisted_session.runs[0])
        assert persisted_run.status is RunStatus.completed
        assert persisted_run.metadata == {
            "room_id": None,
            "thread_id": None,
            "reply_to_event_id": "e1",
            "requester_id": None,
            "correlation_id": "e1",
            "tools_schema": [],
            "model_params": {},
            "matrix_event_id": "e1",
            "matrix_seen_event_ids": ["e1"],
            "mindroom_original_status": "cancelled",
            "mindroom_replay_state": "interrupted",
        }
        assert persisted_run.messages is not None
        assert [(message.role, message.content) for message in persisted_run.messages] == [
            ("user", "test"),
            (
                "assistant",
                "Half done\n\n[tool:run_shell_command completed]\n  args: cmd=pwd\n  result: /app\n\n[interrupted]",
            ),
        ]

    @pytest.mark.asyncio
    async def test_ai_response_cancelled_run_uses_only_latest_assistant_partial_text(
        self,
        tmp_path: Path,
    ) -> None:
        """Cancelled replay should ignore earlier assistant history carried in RunOutput.messages."""
        storage = _SessionStorage()
        mock_agent = MagicMock()
        cancelled_run = RunOutput(
            run_id="run-123",
            agent_id="general",
            session_id="session1",
            content="Half done",
            messages=[
                Message(role="user", content="Earlier question"),
                Message(role="assistant", content="Earlier answer"),
                Message(role="user", content="test"),
                Message(role="assistant", content="Half done"),
            ],
            tools=None,
            status=RunStatus.cancelled,
        )

        with (
            patch(
                "mindroom.ai.open_resolved_scope_session_context",
                new=lambda **_: _open_agent_scope_context(storage),
            ),
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai_runtime.cached_agent_run", new_callable=AsyncMock, return_value=cancelled_run),
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            with pytest.raises(asyncio.CancelledError):
                await ai_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    reply_to_event_id="e1",
                    show_tool_calls=False,
                )

        persisted_session = cast("AgentSession", storage.session)
        assert persisted_session.runs is not None
        persisted_run = cast("RunOutput", persisted_session.runs[0])
        assert persisted_run.messages is not None
        assert [(message.role, message.content) for message in persisted_run.messages] == [
            ("user", "test"),
            ("assistant", "Half done\n\n[interrupted]"),
        ]

    @pytest.mark.asyncio
    async def test_ai_response_persists_incomplete_cancelled_tools_as_interrupted(
        self,
        tmp_path: Path,
    ) -> None:
        """Cancelled non-streaming runs must not serialize unfinished tools as completed."""
        storage = _SessionStorage()
        mock_agent = MagicMock()
        cancelled_run = RunOutput(
            run_id="run-123",
            agent_id="general",
            session_id="session1",
            content="Half done",
            messages=[Message(role="assistant", content="Half done")],
            tools=[
                ToolExecution(
                    tool_name="run_shell_command",
                    tool_args={"cmd": "pwd"},
                    result=None,
                ),
            ],
            status=RunStatus.cancelled,
        )

        with (
            patch(
                "mindroom.ai.open_resolved_scope_session_context",
                new=lambda **_: _open_agent_scope_context(storage),
            ),
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai_runtime.cached_agent_run", new_callable=AsyncMock, return_value=cancelled_run),
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            with pytest.raises(asyncio.CancelledError):
                await ai_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    reply_to_event_id="e1",
                    show_tool_calls=False,
                )

        persisted_session = cast("AgentSession", storage.session)
        assert persisted_session.runs is not None
        persisted_run = cast("RunOutput", persisted_session.runs[0])
        assert persisted_run.messages is not None
        assert [(message.role, message.content) for message in persisted_run.messages] == [
            ("user", "test"),
            (
                "assistant",
                "Half done\n\n[tool:run_shell_command interrupted]\n"
                "  args: cmd=pwd\n"
                "  result: <interrupted before completion>\n\n"
                "[interrupted]",
            ),
        ]

    @pytest.mark.asyncio
    async def test_ai_response_with_turn_recorder_defers_interrupted_persistence_to_runner(
        self,
        tmp_path: Path,
    ) -> None:
        """Lifecycle-owned calls should record interrupted state without persisting directly."""
        storage = _SessionStorage()
        recorder = TurnRecorder(user_message="test")
        mock_agent = MagicMock()
        cancelled_run = RunOutput(
            run_id="run-123",
            agent_id="general",
            session_id="session1",
            content="Half done",
            messages=[Message(role="assistant", content="Half done")],
            tools=[
                ToolExecution(
                    tool_name="run_shell_command",
                    tool_args={"cmd": "pwd"},
                    result="/app",
                ),
            ],
            status=RunStatus.cancelled,
        )

        with (
            patch(
                "mindroom.ai.open_resolved_scope_session_context",
                new=lambda **_: _open_agent_scope_context(storage),
            ),
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai_runtime.cached_agent_run", new_callable=AsyncMock, return_value=cancelled_run),
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            with pytest.raises(asyncio.CancelledError):
                await ai_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    reply_to_event_id="e1",
                    show_tool_calls=False,
                    turn_recorder=recorder,
                )

        assert storage.session is None
        snapshot = recorder.interrupted_snapshot()
        assert snapshot.user_message == "test"
        assert snapshot.partial_text == "Half done"
        assert [tool.tool_name for tool in snapshot.completed_tools] == ["run_shell_command"]
        assert snapshot.seen_event_ids == ("e1",)

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
            patch("mindroom.ai_runtime.cached_agent_run", new_callable=AsyncMock, return_value=mock_run_output),
            patch(
                "mindroom.ai.get_user_friendly_error_message",
                return_value="friendly-error",
            ) as mock_friendly_error,
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
        with patch(
            "mindroom.ai.get_user_friendly_error_message",
            return_value="friendly-error",
        ) as mock_friendly_error:
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
        with patch(
            "mindroom.ai.get_user_friendly_error_message",
            return_value="friendly-error",
        ) as mock_friendly_error:
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

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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
        assert len(logged_contexts) == 2
        assert logged_contexts[0]["agent_id"] == "general"
        assert logged_contexts[0]["session_id"] == "session1"
        assert logged_contexts[0]["room_id"] is None
        assert logged_contexts[0]["thread_id"] is None
        assert logged_contexts[0]["reply_to_event_id"] is None
        assert logged_contexts[0]["requester_id"] is None
        assert logged_contexts[0]["prompt"] == "raw prompt"
        assert logged_contexts[0]["model_prompt"] == "expanded prompt"
        assert logged_contexts[0]["full_prompt"] == prepared_prompt
        assert logged_contexts[1]["full_prompt"] == append_inline_media_fallback_prompt(prepared_prompt)
        assert logged_contexts[1]["correlation_id"] == logged_contexts[0]["correlation_id"]
        assert logged_contexts[0]["metadata"] == {
            "room_id": None,
            "thread_id": None,
            "reply_to_event_id": None,
            "requester_id": None,
            "correlation_id": logged_contexts[0]["correlation_id"],
            "tools_schema": [],
            "model_params": {},
        }
        assert logged_contexts[1]["metadata"] == logged_contexts[0]["metadata"]

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
            patch("mindroom.ai_runtime.cached_agent_run", side_effect=fake_run),
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
    async def test_ai_response_persists_retry_run_id_after_hard_cancellation(self, tmp_path: Path) -> None:
        """Standalone interrupted replay should use the last retry attempt id after hard cancellation."""
        storage = _SessionStorage()
        mock_agent = MagicMock()
        seen_run_ids: list[str | None] = []
        callback_run_ids: list[str] = []

        async def fake_run(*_args: object, **kwargs: object) -> RunOutput:
            seen_run_ids.append(kwargs["run_id"])
            run_id_callback = kwargs["run_id_callback"]
            if run_id_callback is not None and kwargs["run_id"] is not None:
                run_id_callback(kwargs["run_id"])
            if len(seen_run_ids) == 1:
                msg = "Error code: 500 - audio input is not supported"
                raise RuntimeError(msg)
            raise asyncio.CancelledError

        with (
            patch(
                "mindroom.ai.open_resolved_scope_session_context",
                new=lambda **_: _open_agent_scope_context(storage),
            ),
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai_runtime.cached_agent_run", side_effect=fake_run),
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            with pytest.raises(asyncio.CancelledError):
                await ai_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    room_id="!room:localhost",
                    thread_id="$thread:localhost",
                    user_id="@alice:localhost",
                    reply_to_event_id="$event:localhost",
                    run_id="run-123",
                    run_id_callback=callback_run_ids.append,
                    media=MediaInputs(audio=[MagicMock(name="audio_input")]),
                )

        persisted_session = cast("AgentSession", storage.session)
        assert persisted_session.runs is not None
        persisted_run = cast("RunOutput", persisted_session.runs[0])
        assert seen_run_ids[0] == "run-123"
        assert seen_run_ids[1] is not None
        assert seen_run_ids[1] != "run-123"
        assert callback_run_ids == [run_id for run_id in seen_run_ids if run_id is not None]
        assert persisted_run.run_id == seen_run_ids[1]
        assert persisted_run.metadata is not None
        assert persisted_run.metadata["room_id"] == "!room:localhost"
        assert persisted_run.metadata["thread_id"] == "$thread:localhost"
        assert persisted_run.metadata["requester_id"] == "@alice:localhost"
        assert persisted_run.metadata["reply_to_event_id"] == "$event:localhost"
        assert persisted_run.metadata["correlation_id"] == "$event:localhost"
        assert persisted_run.metadata["tools_schema"] == []
        assert persisted_run.metadata["model_params"] == {}

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

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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
        assert len(logged_contexts) == 2
        assert logged_contexts[0]["agent_id"] == "general"
        assert logged_contexts[0]["session_id"] == "session1"
        assert logged_contexts[0]["room_id"] is None
        assert logged_contexts[0]["thread_id"] is None
        assert logged_contexts[0]["reply_to_event_id"] is None
        assert logged_contexts[0]["requester_id"] is None
        assert logged_contexts[0]["prompt"] == "raw prompt"
        assert logged_contexts[0]["model_prompt"] == "expanded prompt"
        assert logged_contexts[0]["full_prompt"] == prepared_prompt
        assert logged_contexts[1]["full_prompt"] == append_inline_media_fallback_prompt(prepared_prompt)
        assert logged_contexts[1]["correlation_id"] == logged_contexts[0]["correlation_id"]
        assert logged_contexts[0]["metadata"] == {
            "room_id": None,
            "thread_id": None,
            "reply_to_event_id": None,
            "requester_id": None,
            "correlation_id": logged_contexts[0]["correlation_id"],
            "tools_schema": [],
            "model_params": {},
        }
        assert logged_contexts[1]["metadata"] == logged_contexts[0]["metadata"]

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

            async def arun(self, prompt: str | list[Message], **_kwargs: object) -> AsyncIterator[object]:
                prompt_messages = prompt if isinstance(prompt, list) else [Message(role="user", content=prompt)]
                async for _chunk in self.model.ainvoke_stream(
                    messages=prompt_messages,
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

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
            patch("mindroom.ai._agent_tools_schema", return_value=[]),
        ):
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
        assert entries[0]["agent_id"] == "general"
        assert entries[0]["session_id"] == "session1"
        assert entries[0]["room_id"] == "!room:example.com"
        assert entries[0]["thread_id"] == "$thread:example.com"
        assert entries[0]["reply_to_event_id"] == "$reply:example.com"
        assert entries[0]["correlation_id"] == "$reply:example.com"
        assert entries[0]["current_turn_prompt"] == "raw prompt"
        assert entries[0]["model_prompt"] == "expanded prompt"
        assert entries[0]["full_prompt"] == prepared_prompt
        assert entries[0]["messages"][0]["role"] == "user"
        logged_content = entries[0]["messages"][0]["content"]
        if isinstance(logged_content, list):
            assert len(logged_content) == 1
            assert logged_content[0]["content"] == prepared_prompt
        else:
            assert logged_content == prepared_prompt

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

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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

    @pytest.mark.asyncio
    async def test_stream_agent_response_persists_retry_run_id_after_hard_cancellation(
        self,
        tmp_path: Path,
    ) -> None:
        """Standalone streaming replay should keep the final retry attempt id after hard cancellation."""
        storage = _SessionStorage()
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def failing_stream() -> AsyncIterator[object]:
            yield RunErrorEvent(content="Error code: 500 - audio input is not supported")

        async def cancelled_stream() -> AsyncIterator[object]:
            raise asyncio.CancelledError
            yield ""  # pragma: no cover

        callback_run_ids: list[str] = []
        mock_agent.arun = MagicMock(side_effect=[failing_stream(), cancelled_stream()])

        with (
            patch(
                "mindroom.ai.open_resolved_scope_session_context",
                new=lambda **_: _open_agent_scope_context(storage),
            ),
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)

            with pytest.raises(asyncio.CancelledError):
                _chunks = [
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

        persisted_session = cast("AgentSession", storage.session)
        assert persisted_session.runs is not None
        persisted_run = cast("RunOutput", persisted_session.runs[0])
        first_call = mock_agent.arun.call_args_list[0]
        second_call = mock_agent.arun.call_args_list[1]
        assert first_call.kwargs["run_id"] == "run-456"
        assert second_call.kwargs["run_id"] is not None
        assert second_call.kwargs["run_id"] != "run-456"
        assert callback_run_ids == [first_call.kwargs["run_id"], second_call.kwargs["run_id"]]
        assert persisted_run.run_id == second_call.kwargs["run_id"]

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
            patch(
                "mindroom.ai.get_user_friendly_error_message",
                return_value="friendly-error",
            ) as mock_friendly_error,
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

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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
            cache_read_tokens=640,
            cache_write_tokens=32,
            reasoning_tokens=24,
            time_to_first_token=0.42,
            duration=1.75,
        )
        mock_agent.arun = AsyncMock(return_value=mock_run_output)

        config = Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=2000)},
        )

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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
        assert payload["usage"]["cache_read_tokens"] == 640
        assert payload["usage"]["cache_write_tokens"] == 32
        assert payload["usage"]["reasoning_tokens"] == 24
        assert payload["context"]["input_tokens"] == 800
        assert payload["context"]["window_tokens"] == 2000
        assert "utilization_pct" not in payload["context"]
        assert payload["tools"]["count"] == 0

    @pytest.mark.asyncio
    async def test_ai_response_context_counts_anthropic_cache_tokens(self, tmp_path: Path) -> None:
        """Claude-family cache tokens should count toward context occupancy."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "Claude"
        mock_agent.model.id = "claude-sonnet-4-6"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        mock_run_output = MagicMock()
        mock_run_output.content = "Response"
        mock_run_output.tools = []
        mock_run_output.run_id = "run-1"
        mock_run_output.session_id = "session1"
        mock_run_output.status = RunStatus.completed
        mock_run_output.model = "claude-sonnet-4-6"
        mock_run_output.model_provider = "Anthropic"
        mock_run_output.metrics = Metrics(
            input_tokens=3000,
            output_tokens=120,
            total_tokens=3120,
            cache_read_tokens=20_000,
            cache_write_tokens=500,
        )
        mock_agent.arun = AsyncMock(return_value=mock_run_output)

        config = Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6", context_window=200_000)},
        )

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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
        assert payload["usage"]["input_tokens"] == 3000
        assert payload["usage"]["cache_read_tokens"] == 20_000
        assert payload["usage"]["cache_write_tokens"] == 500
        assert payload["context"]["input_tokens"] == 23_500
        assert payload["context"]["cache_read_input_tokens"] == 20_000
        assert payload["context"]["cache_write_input_tokens"] == 500
        assert payload["context"]["uncached_input_tokens"] == 3500
        assert "cached_input_tokens" not in payload["context"]
        assert payload["context"]["window_tokens"] == 200_000

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
            "reply_to_event_id": "$primary",
            "tools_schema": [],
            "model_params": {},
            MATRIX_EVENT_ID_METADATA_KEY: "$primary",
            MATRIX_SEEN_EVENT_IDS_METADATA_KEY: ["$primary", "$first", "$unseen"],
            MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$first", "$primary"],
            MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY: {"$first": "first", "$primary": "primary"},
        }

    def test_build_matrix_run_metadata_preserves_existing_trace_fields_without_overrides(self) -> None:
        """Optional trace fields should not erase already-materialized metadata."""
        metadata = build_matrix_run_metadata(
            None,
            [],
            extra_metadata={
                "room_id": "!room:localhost",
                "thread_id": "$thread",
                "reply_to_event_id": "$reply",
                "requester_id": "@alice:localhost",
                "correlation_id": "corr-existing",
                "tools_schema": [{"name": "demo"}],
                "model_params": {"temperature": 0.3},
            },
        )

        assert metadata is not None
        assert metadata["room_id"] == "!room:localhost"
        assert metadata["thread_id"] == "$thread"
        assert metadata["reply_to_event_id"] == "$reply"
        assert metadata["requester_id"] == "@alice:localhost"
        assert metadata["correlation_id"] == "corr-existing"
        assert metadata["tools_schema"] == [{"name": "demo"}]
        assert metadata["model_params"] == {"temperature": 0.3}

    def test_stream_completed_without_visible_output_accepts_final_body_only_completion(self) -> None:
        """Providers that only emit RunCompletedEvent.content still produced visible text."""
        state = _StreamingAttemptState(
            completed_run_event=RunCompletedEvent(run_id="run-1", content="Final answer"),
            canonical_final_body_candidate="Final answer",
        )

        assert _stream_completed_without_visible_output(state) is False

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

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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
    async def test_stream_agent_response_records_final_event_only_text_in_turn_recorder(
        self,
        tmp_path: Path,
    ) -> None:
        """Final-event-only streams should persist the delivered canonical completion content."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            yield RunCompletedEvent(
                content="hello from final event",
                run_id="run-final-only",
                session_id="session1",
            )

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())
        recorder = TurnRecorder(user_message="test")

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            async for _chunk in stream_agent_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                turn_recorder=recorder,
            ):
                pass

        assert recorder.outcome == "completed"
        assert recorder.assistant_text == "hello from final event"

    @pytest.mark.asyncio
    async def test_stream_agent_response_final_event_overwrites_partial_text_in_turn_recorder(
        self,
        tmp_path: Path,
    ) -> None:
        """Canonical final completion content must not overwrite earlier streamed visible text."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            yield RunContentEvent(content="hel")
            yield RunCompletedEvent(
                content="hello",
                run_id="run-corrected",
                session_id="session1",
            )

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())
        recorder = TurnRecorder(user_message="test")

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            async for _chunk in stream_agent_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                turn_recorder=recorder,
            ):
                pass

        assert recorder.outcome == "completed"
        assert recorder.assistant_text == "hel"

    @pytest.mark.asyncio
    async def test_stream_agent_response_empty_final_event_overwrites_partial_text_in_turn_recorder(
        self,
        tmp_path: Path,
    ) -> None:
        """Empty canonical final content must not clear earlier streamed visible text."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            yield RunContentEvent(content="temporary")
            yield RunCompletedEvent(
                content="",
                run_id="run-empty-final",
                session_id="session1",
            )

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())
        recorder = TurnRecorder(user_message="test")

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            async for _chunk in stream_agent_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                turn_recorder=recorder,
            ):
                pass

        assert recorder.outcome == "completed"
        assert recorder.assistant_text == "temporary"

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
            patch("mindroom.ai_runtime.cached_agent_run", new_callable=AsyncMock, return_value=mock_run_output),
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

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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
    async def test_stream_agent_response_persists_hidden_interrupted_tool_state(self, tmp_path: Path) -> None:
        """Streaming cancellation should persist completed and interrupted tools even when hidden in output."""
        storage = _SessionStorage()
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            yield RunContentEvent(content="Half done")
            yield ToolCallStartedEvent(
                tool=ToolExecution(
                    tool_name="run_shell_command",
                    tool_args={"cmd": "pwd"},
                ),
            )
            yield ToolCallCompletedEvent(
                tool=ToolExecution(
                    tool_name="run_shell_command",
                    tool_args={"cmd": "pwd"},
                    result="/app",
                ),
            )
            yield ToolCallStartedEvent(
                tool=ToolExecution(
                    tool_name="save_file",
                    tool_args={"file_name": "main.py"},
                ),
            )
            yield RunCancelledEvent(
                run_id="run-3",
                session_id="session1",
                reason="Run run-3 was cancelled",
            )

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        with (
            patch(
                "mindroom.ai.open_resolved_scope_session_context",
                new=lambda **_: _open_agent_scope_context(storage),
            ),
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            with pytest.raises(asyncio.CancelledError):
                async for _chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    reply_to_event_id="e1",
                    show_tool_calls=False,
                ):
                    pass

        persisted_session = cast("AgentSession", storage.session)
        assert persisted_session.runs is not None
        persisted_run = cast("RunOutput", persisted_session.runs[0])
        assert persisted_run.status is RunStatus.completed
        assert persisted_run.messages is not None
        assert [(message.role, message.content) for message in persisted_run.messages] == [
            ("user", "test"),
            (
                "assistant",
                "Half done\n\n"
                "[tool:run_shell_command completed]\n"
                "  args: cmd=pwd\n"
                "  result: /app\n"
                "[tool:save_file interrupted]\n"
                "  args: file_name=main.py\n"
                "  result: <interrupted before completion>\n\n"
                "[interrupted]",
            ),
        ]

    @pytest.mark.asyncio
    async def test_stream_agent_response_preserves_pending_tool_identity_for_same_named_tools(
        self,
        tmp_path: Path,
    ) -> None:
        """Streaming cancellation must not confuse concurrent same-named tools in one agent scope."""
        storage = _SessionStorage()
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            yield RunContentEvent(content="Half done")
            yield ToolCallStartedEvent(
                tool=ToolExecution(
                    tool_call_id="call-1",
                    tool_name="run_shell_command",
                    tool_args={"cmd": "pwd"},
                ),
            )
            yield ToolCallStartedEvent(
                tool=ToolExecution(
                    tool_call_id="call-2",
                    tool_name="run_shell_command",
                    tool_args={"cmd": "ls"},
                ),
            )
            yield ToolCallCompletedEvent(
                tool=ToolExecution(
                    tool_call_id="call-1",
                    tool_name="run_shell_command",
                    tool_args={"cmd": "pwd"},
                    result="/app",
                ),
            )
            yield RunCancelledEvent(
                run_id="run-3",
                session_id="session1",
                reason="Run run-3 was cancelled",
            )

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        with (
            patch(
                "mindroom.ai.open_resolved_scope_session_context",
                new=lambda **_: _open_agent_scope_context(storage),
            ),
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            with pytest.raises(asyncio.CancelledError):
                async for _chunk in stream_agent_response(
                    agent_name="general",
                    prompt="test",
                    session_id="session1",
                    runtime_paths=_runtime_paths(tmp_path),
                    config=_config(),
                    reply_to_event_id="e1",
                    show_tool_calls=False,
                ):
                    pass

        persisted_session = cast("AgentSession", storage.session)
        assert persisted_session.runs is not None
        persisted_run = cast("RunOutput", persisted_session.runs[0])
        assert persisted_run.messages is not None
        assert [(message.role, message.content) for message in persisted_run.messages] == [
            ("user", "test"),
            (
                "assistant",
                "Half done\n\n"
                "[tool:run_shell_command completed]\n"
                "  args: cmd=pwd\n"
                "  result: /app\n"
                "[tool:run_shell_command interrupted]\n"
                "  args: cmd=ls\n"
                "  result: <interrupted before completion>\n\n"
                "[interrupted]",
            ),
        ]

    @pytest.mark.asyncio
    async def test_stream_agent_response_persists_interrupted_replay_after_external_task_cancel(
        self,
        tmp_path: Path,
    ) -> None:
        """External task cancellation should still persist interrupted replay state."""
        storage = _SessionStorage()
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "OpenAIChat"
        mock_agent.model.id = "test-model"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        first_chunk_seen = asyncio.Event()

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            yield RunContentEvent(content="Half done")
            await asyncio.sleep(60)

        async def consume_stream() -> None:
            async for _chunk in stream_agent_response(
                agent_name="general",
                prompt="test",
                session_id="session1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                reply_to_event_id="e1",
                show_tool_calls=False,
            ):
                first_chunk_seen.set()

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        with (
            patch(
                "mindroom.ai.open_resolved_scope_session_context",
                new=lambda **_: _open_agent_scope_context(storage),
            ),
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_prompt_result(mock_agent)
            task = asyncio.create_task(consume_stream())
            await first_chunk_seen.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        persisted_session = cast("AgentSession", storage.session)
        assert persisted_session.runs is not None
        persisted_run = cast("RunOutput", persisted_session.runs[0])
        assert persisted_run.status is RunStatus.completed
        assert persisted_run.messages is not None
        assert [(message.role, message.content) for message in persisted_run.messages] == [
            ("user", "test"),
            ("assistant", "Half done\n\n[interrupted]"),
        ]

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

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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
    async def test_stream_agent_response_derives_total_tokens_when_request_event_reports_zero(
        self,
        tmp_path: Path,
    ) -> None:
        """Zero-valued request totals should still derive from input and output token counts."""
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
                total_tokens=0,
            )

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        config = Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=100)},
        )

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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
        assert payload["usage"]["input_tokens"] == 12
        assert payload["usage"]["output_tokens"] == 3
        assert payload["usage"]["total_tokens"] == 15

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
                cache_read_tokens=512,
                reasoning_tokens=40,
            )
            yield RunContentEvent(content="step two")
            yield ModelRequestCompletedEvent(
                model="test-model",
                model_provider="openai",
                input_tokens=120,
                output_tokens=20,
                total_tokens=140,
                cache_read_tokens=64,
                reasoning_tokens=8,
            )

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        config = Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=1000)},
        )

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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
        assert payload["usage"]["cache_read_tokens"] == 576
        assert payload["usage"]["reasoning_tokens"] == 48
        assert payload["context"]["input_tokens"] == 120
        assert payload["context"]["cache_read_input_tokens"] == 64
        assert payload["context"]["uncached_input_tokens"] == 56
        assert "cached_input_tokens" not in payload["context"]
        assert payload["context"]["window_tokens"] == 1000

    @pytest.mark.asyncio
    async def test_stream_agent_response_does_not_backfill_latest_context_cache_from_usage(
        self,
        tmp_path: Path,
    ) -> None:
        """Missing latest-request cache counters should stay unknown, not use cumulative totals."""
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
                cache_read_tokens=512,
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

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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
        assert payload["usage"]["cache_read_tokens"] == 512
        assert payload["context"]["input_tokens"] == 120
        assert "cache_read_input_tokens" not in payload["context"]
        assert "cache_write_input_tokens" not in payload["context"]
        assert "uncached_input_tokens" not in payload["context"]
        assert payload["context"]["window_tokens"] == 1000

    @pytest.mark.asyncio
    async def test_stream_agent_response_context_counts_latest_anthropic_cache_tokens(self, tmp_path: Path) -> None:
        """Streaming context metadata should include cache tokens for the latest Claude request."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "Claude"
        mock_agent.model.id = "claude-sonnet-4-6"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            yield RunContentEvent(content="step one")
            yield ModelRequestCompletedEvent(
                model="claude-sonnet-4-6",
                model_provider="Anthropic",
                input_tokens=3000,
                output_tokens=50,
                total_tokens=3050,
                cache_read_tokens=20_000,
                cache_write_tokens=500,
            )
            yield RunContentEvent(content="step two")
            yield ModelRequestCompletedEvent(
                model="claude-sonnet-4-6",
                model_provider="Anthropic",
                input_tokens=120,
                output_tokens=20,
                total_tokens=140,
                cache_read_tokens=9000,
                cache_write_tokens=10,
            )

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        config = Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6", context_window=200_000)},
        )

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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
        assert payload["usage"]["input_tokens"] == 3120
        assert payload["usage"]["cache_read_tokens"] == 29_000
        assert payload["usage"]["cache_write_tokens"] == 510
        assert payload["context"]["input_tokens"] == 9130
        assert payload["context"]["cache_read_input_tokens"] == 9000
        assert payload["context"]["cache_write_input_tokens"] == 10
        assert payload["context"]["uncached_input_tokens"] == 130
        assert "cached_input_tokens" not in payload["context"]
        assert payload["context"]["window_tokens"] == 200_000

    @pytest.mark.asyncio
    async def test_stream_agent_response_context_counts_vertex_claude_cache_tokens(
        self,
        tmp_path: Path,
    ) -> None:
        """Streaming context should use configured provider when event provider is ambiguous."""
        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.__class__.__name__ = "Claude"
        mock_agent.model.id = "claude-sonnet-4-6"
        mock_agent.name = "GeneralAgent"
        mock_agent.add_history_to_context = False

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            yield RunContentEvent(content="step one")
            yield ModelRequestCompletedEvent(
                model="claude-sonnet-4-6",
                model_provider="google",
                input_tokens=120,
                output_tokens=20,
                total_tokens=140,
                cache_read_tokens=9000,
                cache_write_tokens=10,
            )

        mock_agent.arun = MagicMock(return_value=fake_arun_stream())

        config = Config(
            agents={"general": AgentConfig(display_name="General")},
            models={
                "default": ModelConfig(
                    provider="vertexai_claude",
                    id="claude-sonnet-4-6",
                    context_window=200_000,
                ),
            },
        )

        with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
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
        assert payload["model"]["provider"] == "google"
        assert payload["context"]["input_tokens"] == 9130
        assert payload["context"]["cache_read_input_tokens"] == 9000
        assert payload["context"]["cache_write_input_tokens"] == 10
        assert payload["context"]["uncached_input_tokens"] == 130
        assert payload["context"]["window_tokens"] == 200_000
