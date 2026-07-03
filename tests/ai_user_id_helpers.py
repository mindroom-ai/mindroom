"""Shared helpers for the AI response and response-runner test modules."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, TypeVar, cast
from unittest.mock import AsyncMock, MagicMock

import nio
from agno.db.base import SessionType
from agno.models.message import Message

from mindroom.ai import (
    _PreparedAgentRun,
)
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import (
    RuntimePaths,
    resolve_runtime_paths,
)
from mindroom.delivery_gateway import DeliveryGateway, DeliveryGatewayDeps, ResponseHookService
from mindroom.entity_resolution import entity_identity_registry
from mindroom.final_delivery import StreamTransportOutcome
from mindroom.history import PreparedHistoryState
from mindroom.history.runtime import ScopeSessionContext
from mindroom.history.types import HistoryScope
from mindroom.hooks import (
    HookContextSupport,
    HookRegistry,
)
from mindroom.hooks.registry import HookRegistryState
from mindroom.knowledge.utils import KnowledgeAvailabilityDetail, _KnowledgeResolution
from mindroom.matrix.cache.thread_history_result import thread_history_result
from mindroom.message_target import MessageTarget
from mindroom.post_response_effects import PostResponseEffectsDeps, PostResponseEffectsSupport
from mindroom.response_payload_preparation import ResponsePayloadPreparer
from mindroom.response_runner import (
    ResponseRequest,
    ResponseRunner,
    ResponseRunnerDeps,
)
from mindroom.team_scope import ad_hoc_team_scope_id
from mindroom.tool_system.runtime_context import (
    ToolRuntimeSupport,
)
from tests.bot_helpers import (  # noqa: F401
    _handled_response_event_id,
    _stream_outcome,
    _visible_response_event_id,
)
from tests.conftest import bind_runtime_paths as _bind_runtime_paths
from tests.conftest import (
    make_event_cache_mock,
    request_envelope,
)
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable
    from pathlib import Path

    from agno.knowledge.knowledge import Knowledge
    from agno.session.agent import AgentSession
    from agno.session.team import TeamSession

    from mindroom.matrix.identity import MatrixID
    from mindroom.media_inputs import MediaInputs


T = TypeVar("T")


def bind_runtime_paths(config: Config, runtime_paths: RuntimePaths) -> Config:
    """Bind test runtime paths and persist managed account identities."""
    bound = _bind_runtime_paths(config, runtime_paths)
    persist_entity_accounts(bound, runtime_paths)
    return bound


def _set_gateway_method(gateway: DeliveryGateway, name: str, value: T) -> T:
    object.__setattr__(gateway, name, value)
    return value


def _runtime_paths(tmp_path: Path, *, config_path: Path | None = None) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=config_path or tmp_path / "config.yaml",
        storage_path=tmp_path,
    )


def _entity_alias_for_test(config: Config, runtime_paths: RuntimePaths, matrix_id: MatrixID) -> str:
    registry = entity_identity_registry(config, runtime_paths)
    return registry.current_entity_name_for_user_id(matrix_id.full_id) or matrix_id.username


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


def _config_with_team_matrix_message() -> Config:
    return Config(
        agents={
            "general": AgentConfig(
                display_name="General",
                tools=["matrix_message"],
            ),
        },
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
    prepared_context_tokens: int | None = None,
    runtime_model_name: str = "default",
) -> _PreparedAgentRun:
    return _PreparedAgentRun(
        agent=agent,
        messages=(Message(role="user", content=prompt),),
        unseen_event_ids=[],
        prepared_history=PreparedHistoryState(
            prepared_context_tokens=prepared_context_tokens,
        ),
        runtime_model_name=runtime_model_name,
    )


def _metadata_config(provider: str, model_id: str) -> Config:
    return Config(
        agents={"general": AgentConfig(display_name="General")},
        models={"default": ModelConfig(provider=provider, id=model_id, context_window=200_000)},
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


def _mark_requester_online(client: AsyncMock, user_id: str, room_id: str = "!test:localhost") -> None:
    """Make the requester show as online in the client's synced room cache.

    ``should_use_streaming`` reads presence from ``client.rooms`` first, so this
    drives the streaming decision through its real input instead of a patch.
    """
    room = nio.MatrixRoom(room_id, "@mindroom_general:localhost")
    room.users[user_id] = nio.MatrixUser(user_id, presence="online")
    client.rooms = {room_id: room}


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
    bot.stop_manager.add_stop_button = AsyncMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = agent_name
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = _knowledge_access_support()
    return bot


def _knowledge_access_support(
    knowledge: object | None = None,
    unavailable: dict[str, KnowledgeAvailabilityDetail] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        for_agent=MagicMock(return_value=knowledge),
        resolve_for_agent=MagicMock(
            return_value=_KnowledgeResolution(
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
    enable_streaming: bool = True,
) -> ResponseRunner:
    """Build a real response runner for one bot-shaped test double."""

    def _open_test_storage(storage: object | None) -> object:
        if isinstance(storage, _SessionStorage):
            return storage.open()
        return storage if storage is not None else MagicMock()

    bot.matrix_id = MagicMock(full_id="@mindroom_general:localhost", domain="localhost")
    bot.enable_streaming = enable_streaming
    bot.show_tool_calls = False
    bot.orchestrator = orchestrator
    bot._conversation_resolver = MagicMock()
    bot._conversation_resolver.build_message_target = MagicMock(
        return_value=message_target or MessageTarget.resolve("!test:localhost", None, "$user_msg", room_mode=True),
    )
    bot._conversation_resolver.fetch_thread_history = AsyncMock(
        return_value=thread_history_result([], is_full_history=True),
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

    def _team_history_scope_for_test(
        team_agents: Iterable[MatrixID],
        *,
        requester_user_id: str | None = None,
    ) -> HistoryScope:
        if bot.agent_name in config.teams:
            return HistoryScope(kind="team", scope_id=bot.agent_name)
        member_names = [_entity_alias_for_test(config, runtime_paths, mid) for mid in team_agents]
        scope_id = (
            ad_hoc_team_scope_id(
                member_names,
                config.agents,
                requester_user_id=requester_user_id,
                missing_requester_message="Private ad hoc team history scope requires requester_user_id",
            )
            or "team_"
        )
        return HistoryScope(
            kind="team",
            scope_id=scope_id,
        )

    bot._conversation_state_writer.team_history_scope = MagicMock(side_effect=_team_history_scope_for_test)
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
            request_preparer=ResponsePayloadPreparer(
                normalizer=MagicMock(),
                ingress_hook_runner=MagicMock(),
                agent_name=bot.agent_name,
                logger=bot.logger,
            ),
        ),
    )


def _response_request(
    *,
    room_id: str = "!test:localhost",
    reply_to_event_id: str = "$user_msg",
    thread_id: str | None = None,
    prompt: str = "Hello",
    model_prompt: str | None = None,
    media: MediaInputs | None = None,
    user_id: str | None = None,
    correlation_id: str | None = None,
) -> ResponseRequest:
    """Build one response request for direct bot seam tests."""
    return ResponseRequest(
        thread_history=(),
        prompt=prompt,
        response_envelope=request_envelope(
            room_id=room_id,
            reply_to_event_id=reply_to_event_id,
            thread_id=thread_id,
            prompt=prompt,
            user_id=user_id,
        ),
        model_prompt=model_prompt,
        media=media,
        user_id=user_id,
        correlation_id=correlation_id,
    )


class _InertPostResponseEffects(PostResponseEffectsSupport):
    """Post-response support whose per-response deps carry no side effects.

    The real ``apply_post_response_effects`` still runs; every effect it guards
    on (interactive registration, memory persistence, run-metadata linkage,
    thread summaries) is absent from the built deps, so tests exercise the
    lifecycle without patching the module function.
    """

    def build_deps(
        self,
        *,
        room_id: str,
        interactive_agent_name: str,
        queue_memory_persistence: Callable[[], None] | None = None,
        persist_response_event_id: Callable[[str, str], None] | None = None,
    ) -> PostResponseEffectsDeps:
        del room_id, interactive_agent_name, queue_memory_persistence, persist_response_event_id
        return PostResponseEffectsDeps(logger=self.logger)


def _install_inert_post_response_effects(coordinator: ResponseRunner) -> None:
    """Swap the post-response collaborator for the inert variant at the deps seam."""
    support = coordinator.deps.post_response_effects
    coordinator.deps = replace(
        coordinator.deps,
        post_response_effects=_InertPostResponseEffects(
            runtime=support.runtime,
            logger=support.logger,
            runtime_paths=support.runtime_paths,
            delivery_gateway=support.delivery_gateway,
            conversation_cache=support.conversation_cache,
        ),
    )
