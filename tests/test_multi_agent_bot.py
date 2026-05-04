"""Tests for the multi-agent bot system."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import os
import signal
import sys
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self, cast
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch
from zoneinfo import ZoneInfo

import httpx
import nio
import pytest
import uvicorn
from agno.knowledge.document import Document
from agno.knowledge.knowledge import Knowledge
from agno.media import Image
from agno.models.ollama import Ollama
from agno.run.agent import RunContentEvent
from agno.run.team import TeamRunOutput

import mindroom.tool_system.plugin_imports as plugin_module
from mindroom import interactive
from mindroom.approval_inbound import handle_tool_approval_action
from mindroom.approval_manager import (
    ApprovalManager,
    PendingApproval,
    SentApprovalEvent,
    get_approval_store,
    initialize_approval_store,
)
from mindroom.attachments import _attachment_id_for_event, register_local_attachment
from mindroom.authorization import is_authorized_sender as is_authorized_sender_for_test
from mindroom.bot import AgentBot, TeamBot
from mindroom.coalescing import COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP, COALESCING_BYPASS_TRUSTED_INTERNAL_RELAY
from mindroom.coalescing_batch import PendingEvent
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
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
from mindroom.delivery_gateway import (
    DeliveryGateway,
    FinalDeliveryRequest,
    FinalizeStreamedResponseRequest,
    SendTextRequest,
)
from mindroom.dispatch_handoff import PreparedTextEvent
from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome
from mindroom.handled_turns import HandledTurnState
from mindroom.history import CompactionLifecycleStart, CompactionOutcome
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
from mindroom.knowledge import KnowledgeAvailability, KnowledgeResolution
from mindroom.knowledge.manager import KnowledgeManager
from mindroom.knowledge.utils import MultiKnowledgeVectorDb
from mindroom.matrix.cache import ThreadHistoryResult
from mindroom.matrix.cache.thread_history_result import thread_history_result
from mindroom.matrix.client import DeliveredMatrixEvent, PermanentMatrixStartupError, ResolvedVisibleMessage
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY, AgentMatrixUser
from mindroom.media_inputs import MediaInputs
from mindroom.message_target import MessageTarget
from mindroom.orchestration.config_updates import ConfigUpdatePlan
from mindroom.orchestration.plugin_watch import collect_plugin_root_changes
from mindroom.orchestration.runtime import (
    _matrix_homeserver_startup_timeout_seconds_from_env,
    run_with_retry,
    wait_for_matrix_homeserver,
)
from mindroom.orchestrator import (
    MultiAgentOrchestrator,
    _EmbeddedApiServerContext,
    _run_api_server,
    _run_auxiliary_task_forever,
    _SignalAwareUvicornServer,
    _wait_for_runtime_completion,
    main,
)
from mindroom.response_lifecycle import response_outcome_label
from mindroom.response_runner import (
    PostLockRequestPreparationError,
    ResponseRequest,
    ResponseRunner,
    _merge_response_extra_content,
)
from mindroom.runtime_state import get_runtime_state, reset_runtime_state, set_runtime_ready
from mindroom.runtime_support import StartupThreadPrewarmRegistry
from mindroom.streaming import StreamingDeliveryError
from mindroom.teams import TeamIntent, TeamMemberStatus, TeamMode, TeamOutcome, TeamResolution, TeamResolutionMember
from mindroom.thread_summary import thread_summary_message_count_hint
from mindroom.tool_approval import ApprovalActionResult, MatrixApprovalAction, shutdown_approval_store
from mindroom.tool_system.events import ToolTraceEntry
from mindroom.tool_system.metadata import TOOL_METADATA
from mindroom.tool_system.skills import _get_plugin_skill_roots, set_plugin_skill_roots
from mindroom.tool_system.worker_routing import agent_state_root_path
from mindroom.turn_controller import TurnController, _PrecheckedEvent
from mindroom.turn_policy import DispatchPlan, PreparedDispatch, ResponseAction, TurnPolicy
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    delivered_matrix_event,
    delivered_matrix_side_effect,
    drain_coalescing,
    install_edit_message_mock,
    install_generate_response_mock,
    install_runtime_cache_support,
    install_send_response_mock,
    make_event_cache_mock,
    make_event_cache_write_coordinator_mock,
    make_matrix_client_mock,
    patch_response_runner_module,
    replace_delivery_gateway_deps,
    replace_response_runner_deps,
    replace_turn_controller_deps,
    runtime_paths_for,
    test_runtime_paths,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)
from tests.conftest import replace_turn_policy_deps as shared_replace_turn_policy_deps

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Sequence
    from pathlib import Path

    from mindroom.post_response_effects import ResponseOutcome
    from mindroom.turn_store import TurnStore


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


def _outcome(
    terminal_status: str,
    final_visible_event_id: str | None = None,
    visible_response_event_id: str | None = None,
    response_identity_event_id: str | None = None,
    turn_completion_event_id: str | None = None,
    last_physical_stream_event_id: str | None = None,
    final_visible_body: str | None = None,
    delivery_kind: str | None = None,
    failure_reason: str | None = None,
    suppressed: bool = False,
    extra_content: dict[str, object] | None = None,
) -> FinalDeliveryOutcome:
    event_id = (
        response_identity_event_id
        or visible_response_event_id
        or final_visible_event_id
        or turn_completion_event_id
        or last_physical_stream_event_id
    )
    resolved_suppressed = suppressed or (failure_reason == "suppressed_by_hook" and response_identity_event_id is None)
    is_visible_response = any(
        value is not None
        for value in (
            final_visible_event_id,
            visible_response_event_id,
            response_identity_event_id,
            last_physical_stream_event_id,
        )
    )
    return FinalDeliveryOutcome(
        terminal_status=terminal_status,
        event_id=event_id,
        is_visible_response=is_visible_response,
        final_visible_body=final_visible_body,
        delivery_kind=delivery_kind,
        failure_reason=failure_reason,
        suppressed=resolved_suppressed,
        extra_content=extra_content,
    )


def _visible_response_event_id(outcome: FinalDeliveryOutcome | str | None) -> str | None:
    if isinstance(outcome, str) or outcome is None:
        return outcome
    return outcome.final_visible_event_id


def _handled_response_event_id(outcome: FinalDeliveryOutcome | str | None) -> str | None:
    if isinstance(outcome, str) or outcome is None:
        return outcome
    return outcome.event_id if outcome.mark_handled and outcome.is_visible_response and not outcome.suppressed else None


async def _run_orchestrator_start_until_ready(orchestrator: MultiAgentOrchestrator) -> None:
    """Run start() until readiness, then explicitly shut the runtime down."""
    ready = asyncio.Event()

    def mark_ready() -> None:
        ready.set()

    with patch("mindroom.orchestrator.set_runtime_ready", side_effect=mark_ready):
        runtime_task = asyncio.create_task(orchestrator.start())
        try:
            await asyncio.wait_for(ready.wait(), timeout=1.0)
            await orchestrator.stop()
            await asyncio.wait_for(runtime_task, timeout=1.0)
        finally:
            if not runtime_task.done():
                runtime_task.cancel()
                with suppress(asyncio.CancelledError):
                    await runtime_task


def _make_matrix_client_mock() -> AsyncMock:
    """Return one Matrix client mock with safe thread-history defaults."""
    return make_matrix_client_mock()


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
    """Attach the full injected runtime-support bundle to a bot test instance."""
    bot.event_cache = make_event_cache_mock()
    bot.event_cache_write_coordinator = make_event_cache_write_coordinator_mock()
    bot.startup_thread_prewarm_registry = StartupThreadPrewarmRegistry()


def _empty_full_thread_history() -> ThreadHistoryResult:
    """Return a fully hydrated empty thread history for tests that bypass Matrix fetches."""
    return ThreadHistoryResult([], is_full_history=True)


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
    resolve_for_agent = MagicMock(
        return_value=KnowledgeResolution(knowledge=knowledge_for_agent.return_value),
    )
    bot._knowledge_access_support.resolve_for_agent = resolve_for_agent
    return resolve_for_agent


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
    requester_id: str | None = None,
    correlation_id: str | None = None,
    source_event_prompts: dict[str, str] | None = None,
) -> HandledTurnState:
    """Return the handled-turn state persisted for one direct agent response."""
    return HandledTurnState.from_source_event_id(
        event_id,
        response_event_id=response_event_id,
        requester_id=requester_id,
        correlation_id=correlation_id,
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


def _approval_reload_config(tmp_path: Path, *, include_code: bool) -> Config:
    """Return one minimal config for approval reload tests."""
    agents: dict[str, dict[str, object]] = {
        "general": {
            "display_name": "GeneralAgent",
            "role": "General assistant",
            "model": "default",
            "rooms": ["lobby"],
        },
    }
    if include_code:
        agents["code"] = {
            "display_name": "CodeAgent",
            "role": "Writes code",
            "model": "default",
            "rooms": ["lobby"],
        }
    return _runtime_bound_config(
        Config(
            agents=agents,
            models={"default": {"provider": "test", "id": "test-model"}},
        ),
        tmp_path,
    )


def _code_only_config(tmp_path: Path, *, rooms: list[str]) -> Config:
    """Return one minimal config with only the code agent."""
    return _runtime_bound_config(
        Config(
            agents={
                "code": {
                    "display_name": "CodeAgent",
                    "role": "Writes code",
                    "model": "default",
                    "rooms": rooms,
                },
            },
            models={"default": {"provider": "test", "id": "test-model"}},
        ),
        tmp_path,
    )


def _mock_approval_reload_bot(
    config: Config,
    *,
    agent_name: str,
    user_id: str,
    room_send: AsyncMock,
) -> MagicMock:
    """Return one managed-bot double with a live Matrix client for approval reload tests."""
    bot = _mock_managed_bot(config)
    bot.agent_name = agent_name
    bot.running = True
    bot.client = make_matrix_client_mock(user_id=user_id)
    bot.client.room_send = room_send
    bot.client.rooms["!room:localhost"].add_member(user_id, agent_name.capitalize(), None)
    latest_thread_event_id = "$latest-thread-event" if agent_name == "code" else None
    bot.latest_thread_event_id_if_needed = AsyncMock(return_value=latest_thread_event_id)
    bot.cleanup = AsyncMock()
    return bot


async def _wait_for_live_pending(
    store: ApprovalManager,
    sender: AsyncMock,
    *,
    room_id: str = "!test:localhost",
) -> PendingApproval:
    async with asyncio.timeout(5):
        while True:
            if sender.await_args is not None:
                approval_id = sender.await_args.args[2]["approval_id"]
                card_event_id = store._live_card_event_id_for_approval(approval_id)
                if card_event_id is not None:
                    pending = await store.get_pending_approval(room_id, approval_id)
                    if pending is not None:
                        return pending
            await asyncio.sleep(0)


async def _wait_for_pending_approval_id(store: ApprovalManager, approval_ids: list[str]) -> str:
    async with asyncio.timeout(1):
        while True:
            if approval_ids and await store.get_pending_approval("!room:localhost", approval_ids[0]) is not None:
                return approval_ids[0]
            await asyncio.sleep(0)


async def _start_live_approval(
    runtime_paths: RuntimePaths,
    *,
    approver_user_id: str = "@user:localhost",
    editor: AsyncMock | None = None,
    arguments: dict[str, Any] | None = None,
) -> tuple[ApprovalManager, PendingApproval, asyncio.Task[Any], AsyncMock]:
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    approval_editor = editor or AsyncMock(return_value=True)
    store = initialize_approval_store(runtime_paths, sender=sender, editor=approval_editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments=arguments or {"path": "notes.txt"},
            room_id="!test:localhost",
            requester_id="@user:localhost",
            approver_user_id=approver_user_id,
            timeout_seconds=30,
        ),
    )
    try:
        pending = await _wait_for_live_pending(store, sender)
    except Exception:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await shutdown_approval_store()
        raise
    return store, pending, task, approval_editor


def _approval_removal_plan(new_config: Config) -> ConfigUpdatePlan:
    """Return one config-update plan that removes the code bot without restarting others."""
    return ConfigUpdatePlan(
        new_config=new_config,
        changed_mcp_servers=set(),
        all_new_entities=set(),
        entities_to_restart=set(),
        new_entities=set(),
        removed_entities={"code"},
        mindroom_user_changed=False,
        matrix_room_access_changed=False,
        matrix_space_changed=False,
        authorization_changed=False,
    )


def _cleanup_recorder(event_order: list[str]) -> Callable[[], Awaitable[None]]:
    """Return one async cleanup side effect that records teardown ordering."""

    async def _cleanup() -> None:
        event_order.append("cleanup")

    return _cleanup


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
    manager._cached_persisted_indexing_state = SimpleNamespace(
        status="complete",
        availability=KnowledgeAvailability.READY.value,
    )
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


def _room_image_event(
    *,
    sender: str,
    event_id: str,
    body: str = "image.jpg",
    room_id: str = "!test:localhost",
    server_timestamp: int = 1000,
) -> nio.RoomMessageImage:
    """Create a typed Matrix image event for media-dispatch tests."""
    return cast(
        "nio.RoomMessageImage",
        nio.RoomMessageImage.from_dict(
            {
                "event_id": event_id,
                "sender": sender,
                "origin_server_ts": server_timestamp,
                "room_id": room_id,
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.image",
                    "body": body,
                    "url": "mxc://localhost/test-image",
                    "info": {"mimetype": "image/jpeg"},
                },
            },
        ),
    )


def _room_audio_event(
    *,
    sender: str,
    event_id: str,
    body: str = "voice.ogg",
    room_id: str = "!test:localhost",
    server_timestamp: int = 1000,
) -> nio.RoomMessageAudio:
    """Create a typed Matrix audio event for media-dispatch tests."""
    return cast(
        "nio.RoomMessageAudio",
        nio.RoomMessageAudio.from_dict(
            {
                "event_id": event_id,
                "sender": sender,
                "origin_server_ts": server_timestamp,
                "room_id": room_id,
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.audio",
                    "body": body,
                    "url": "mxc://localhost/test-audio",
                    "info": {"mimetype": "audio/ogg"},
                },
            },
        ),
    )


def _room_file_event(
    *,
    sender: str,
    event_id: str,
    body: str = "report.pdf",
    room_id: str = "!test:localhost",
    server_timestamp: int = 1000,
) -> nio.RoomMessageFile:
    """Create a typed Matrix file event for media-dispatch tests."""
    return cast(
        "nio.RoomMessageFile",
        nio.RoomMessageFile.from_dict(
            {
                "event_id": event_id,
                "sender": sender,
                "origin_server_ts": server_timestamp,
                "room_id": room_id,
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.file",
                    "body": body,
                    "url": "mxc://localhost/test-file",
                    "info": {"mimetype": "application/pdf"},
                },
            },
        ),
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


def _make_compaction_outcome(*, mode: str = "auto") -> CompactionOutcome:
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
    def _make_handler_event(handler_name: str, *, sender: str, event_id: str) -> object:
        """Create a minimal event object for a specific handler type."""
        if handler_name == "message":
            event = MagicMock(spec=nio.RoomMessageText)
            event.body = "hello"
            event.server_timestamp = 1234567890
            event.source = {"content": {"body": "hello"}}
        elif handler_name == "image":
            event = _room_image_event(sender=sender, event_id=event_id)
        elif handler_name == "voice":
            event = _room_audio_event(sender=sender, event_id=event_id, body="voice")
        elif handler_name == "file":
            event = _room_file_event(sender=sender, event_id=event_id)
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
            await drain_coalescing(bot)
        elif handler_name in {"image", "voice", "file"}:
            await bot._on_media_message(room, event)
            await drain_coalescing(bot)
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
        lookup = SimpleNamespace(
            key=SimpleNamespace(
                base_id="research",
                storage_root=str(tmp_path),
                knowledge_path=str(tmp_path / "kb"),
                indexing_settings=(),
            ),
            index=SimpleNamespace(
                knowledge=expected_knowledge,
                state=SimpleNamespace(
                    source_signature=hashlib.sha256().hexdigest(),
                    last_published_at="2999-01-01T00:00:00+00:00",
                ),
            ),
            availability=KnowledgeAvailability.READY,
        )

        with patch("mindroom.knowledge.utils.get_published_index", return_value=lookup):
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

        def _lookup(base_id: str, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                key=SimpleNamespace(
                    base_id=base_id,
                    storage_root=str(tmp_path),
                    knowledge_path=str(tmp_path / f"kb_{base_id}"),
                    indexing_settings=(),
                ),
                index=SimpleNamespace(
                    knowledge={"research": research_knowledge, "legal": legal_knowledge}[base_id],
                    state=SimpleNamespace(
                        source_signature=hashlib.sha256().hexdigest(),
                        last_published_at="2999-01-01T00:00:00+00:00",
                    ),
                ),
                availability=KnowledgeAvailability.READY,
            )

        with patch("mindroom.knowledge.utils.get_published_index", side_effect=_lookup):
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
            mock_client.add_event_callback.call_count == 13
        )  # invite, message, redaction, reaction, audio, image/file/video, unknown-event callbacks

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
    async def test_embedded_uvicorn_signal_handler_requests_application_shutdown(self) -> None:
        """Uvicorn process signals should propagate to the top-level shutdown event."""
        shutdown_requested = asyncio.Event()

        async def app(scope: object, receive: object, send: object) -> None:
            del scope
            del receive
            del send

        server = _SignalAwareUvicornServer(
            uvicorn.Config(app, host="127.0.0.1", port=0),
            shutdown_requested,
        )

        with patch("mindroom.orchestrator.logger.info") as mock_info:
            server.handle_exit(signal.SIGTERM, None)

        assert shutdown_requested.is_set()
        assert server.should_exit is True
        assert server._captured_signals == []
        mock_info.assert_any_call(
            "embedded_api_server_signal_received",
            signal_number=int(signal.SIGTERM),
            signal_name="SIGTERM",
        )

    @pytest.mark.asyncio
    async def test_run_api_server_fails_fast_when_serve_returns_unexpectedly(self, tmp_path: Path) -> None:
        """server.serve() returning outside shutdown should be a fatal API lifecycle failure."""

        class ReturningServer:
            should_exit = False
            force_exit = False

            def __init__(self, _config: object, _shutdown_requested: asyncio.Event | None) -> None:
                pass

            async def serve(self) -> None:
                return None

        with (
            patch("mindroom.orchestrator.uvicorn.Config", return_value=object()),
            patch("mindroom.orchestrator._SignalAwareUvicornServer", ReturningServer),
            patch("mindroom.api.main.initialize_api_app"),
            patch("mindroom.api.main.bind_orchestrator_knowledge_refresh_scheduler"),
            patch("mindroom.orchestrator.logger.error") as mock_error,
            pytest.raises(RuntimeError, match="Embedded API server exited unexpectedly"),
        ):
            await _run_api_server(
                "127.0.0.1",
                0,
                "INFO",
                self._runtime_paths(tmp_path),
                shutdown_requested=asyncio.Event(),
            )

        mock_error.assert_called_once()
        assert mock_error.call_args.args == ("fatal_embedded_api_server_exit",)

    @pytest.mark.asyncio
    async def test_run_api_server_allows_expected_shutdown_after_serve_returns(self, tmp_path: Path) -> None:
        """server.serve() returning after an intentional shutdown should not be fatal."""

        class ReturningServer:
            should_exit = True
            force_exit = False

            def __init__(self, _config: object, _shutdown_requested: asyncio.Event | None) -> None:
                pass

            async def serve(self) -> None:
                return None

        shutdown_requested = asyncio.Event()
        shutdown_requested.set()

        with (
            patch("mindroom.orchestrator.uvicorn.Config", return_value=object()),
            patch("mindroom.orchestrator._SignalAwareUvicornServer", ReturningServer),
            patch("mindroom.api.main.initialize_api_app"),
            patch("mindroom.api.main.bind_orchestrator_knowledge_refresh_scheduler"),
            patch("mindroom.orchestrator.logger.error") as mock_error,
        ):
            await _run_api_server(
                "127.0.0.1",
                0,
                "INFO",
                self._runtime_paths(tmp_path),
                shutdown_requested=shutdown_requested,
            )

        mock_error.assert_not_called()

    @pytest.mark.asyncio
    async def test_runtime_completion_raises_done_orchestrator_failure_before_clean_shutdown_return(self) -> None:
        """Simultaneous shutdown/API completion must not hide orchestrator failures."""
        shutdown_requested = asyncio.Event()
        shutdown_requested.set()

        async def _failed_orchestrator() -> None:
            msg = "orchestrator failed during shutdown"
            raise RuntimeError(msg)

        async def _api_done() -> None:
            return None

        orchestrator_task = asyncio.create_task(_failed_orchestrator(), name="orchestrator")
        shutdown_wait_task = asyncio.create_task(shutdown_requested.wait(), name="application_shutdown_wait")
        api_task = asyncio.create_task(_api_done(), name="api_server")
        await asyncio.sleep(0)

        try:
            with pytest.raises(RuntimeError, match="orchestrator failed during shutdown"):
                await _wait_for_runtime_completion(
                    orchestrator_task=orchestrator_task,
                    shutdown_wait_task=shutdown_wait_task,
                    api_task=api_task,
                    shutdown_requested=shutdown_requested,
                    api_server=_EmbeddedApiServerContext(host="127.0.0.1", port=0),
                )
        finally:
            await asyncio.gather(orchestrator_task, shutdown_wait_task, api_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_runtime_completion_raises_orchestrator_failure_during_api_shutdown_grace(self) -> None:
        """API shutdown grace must keep observing orchestrator failures."""
        shutdown_requested = asyncio.Event()
        shutdown_requested.set()

        async def _failed_orchestrator() -> None:
            await asyncio.sleep(0.01)
            msg = "orchestrator failed during API shutdown grace"
            raise RuntimeError(msg)

        async def _blocked_api() -> None:
            await asyncio.Event().wait()

        orchestrator_task = asyncio.create_task(_failed_orchestrator(), name="orchestrator")
        shutdown_wait_task = asyncio.create_task(shutdown_requested.wait(), name="application_shutdown_wait")
        api_task = asyncio.create_task(_blocked_api(), name="api_server")

        try:
            with (
                patch("mindroom.orchestrator._EMBEDDED_API_SHUTDOWN_GRACE_SECONDS", 0.05),
                pytest.raises(RuntimeError, match="orchestrator failed during API shutdown grace"),
            ):
                await _wait_for_runtime_completion(
                    orchestrator_task=orchestrator_task,
                    shutdown_wait_task=shutdown_wait_task,
                    api_task=api_task,
                    shutdown_requested=shutdown_requested,
                    api_server=_EmbeddedApiServerContext(host="127.0.0.1", port=0),
                )
        finally:
            api_task.cancel()
            await asyncio.gather(orchestrator_task, shutdown_wait_task, api_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_orchestrator_main_logs_api_shutdown_timeout_before_cancelling_stuck_api_task(
        self,
        tmp_path: Path,
    ) -> None:
        """Shutdown should wait for API grace timeout before cancelling a stuck API task."""
        reset_runtime_state()
        events: list[str] = []
        orchestrator_cancelled = asyncio.Event()
        api_cancelled = asyncio.Event()
        mock_orchestrator = MagicMock()
        mock_orchestrator.knowledge_refresh_scheduler = None
        mock_orchestrator.stop = AsyncMock()

        async def _start() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                orchestrator_cancelled.set()
                raise

        async def _api_requests_shutdown_and_blocks(
            _host: str,
            _port: int,
            _log_level: str,
            _runtime_paths: RuntimePaths,
            _knowledge_refresh_scheduler: object,
            shutdown_requested: asyncio.Event | None,
        ) -> None:
            assert shutdown_requested is not None
            shutdown_requested.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("api_cancelled")
                api_cancelled.set()
                raise

        async def _blocked_auxiliary_task(*_args: object, **_kwargs: object) -> None:
            await asyncio.Event().wait()

        def _record_warning(*_args: object, **_kwargs: object) -> None:
            events.append("timeout_logged")

        mock_orchestrator.start = AsyncMock(side_effect=_start)

        with (
            patch("mindroom.orchestrator.setup_logging"),
            patch("mindroom.orchestrator.sync_env_to_credentials"),
            patch("mindroom.orchestrator.MultiAgentOrchestrator", return_value=mock_orchestrator),
            patch("mindroom.orchestrator._run_auxiliary_task_forever", new=_blocked_auxiliary_task),
            patch("mindroom.orchestrator._run_api_server", side_effect=_api_requests_shutdown_and_blocks),
            patch("mindroom.orchestrator.logger.warning", side_effect=_record_warning) as mock_warning,
            patch("mindroom.orchestrator._EMBEDDED_API_SHUTDOWN_GRACE_SECONDS", 0.01),
        ):
            await asyncio.wait_for(
                main(
                    log_level="INFO",
                    runtime_paths=self._runtime_paths(tmp_path),
                    api=True,
                    api_host="127.0.0.1",
                ),
                timeout=1,
            )

        assert events[:2] == ["timeout_logged", "api_cancelled"]
        assert orchestrator_cancelled.is_set()
        assert api_cancelled.is_set()
        mock_warning.assert_called_once_with(
            "embedded_api_server_shutdown_timeout",
            host="127.0.0.1",
            port=8765,
            timeout_seconds=0.01,
        )
        mock_orchestrator.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orchestrator_main_waits_for_api_server_graceful_shutdown_after_request(
        self,
        tmp_path: Path,
    ) -> None:
        """Shutdown should let the embedded API server run its own cleanup before teardown."""
        reset_runtime_state()
        api_shutdown_started = asyncio.Event()
        api_allow_finish = asyncio.Event()
        api_completed = asyncio.Event()
        api_cancelled = asyncio.Event()
        start_blocker = asyncio.Event()
        mock_orchestrator = MagicMock()
        mock_orchestrator.knowledge_refresh_scheduler = None
        mock_orchestrator.stop = AsyncMock()

        async def _start() -> None:
            await start_blocker.wait()

        async def _api_requests_shutdown_then_finishes(
            _host: str,
            _port: int,
            _log_level: str,
            _runtime_paths: RuntimePaths,
            _knowledge_refresh_scheduler: object,
            shutdown_requested: asyncio.Event | None,
        ) -> None:
            assert shutdown_requested is not None
            shutdown_requested.set()
            api_shutdown_started.set()
            try:
                await api_allow_finish.wait()
            except asyncio.CancelledError:
                api_cancelled.set()
                raise
            api_completed.set()

        async def _blocked_auxiliary_task(*_args: object, **_kwargs: object) -> None:
            await asyncio.Event().wait()

        mock_orchestrator.start = AsyncMock(side_effect=_start)

        with (
            patch("mindroom.orchestrator.setup_logging"),
            patch("mindroom.orchestrator.sync_env_to_credentials"),
            patch("mindroom.orchestrator.MultiAgentOrchestrator", return_value=mock_orchestrator),
            patch("mindroom.orchestrator._run_auxiliary_task_forever", new=_blocked_auxiliary_task),
            patch("mindroom.orchestrator._run_api_server", side_effect=_api_requests_shutdown_then_finishes),
        ):
            main_task = asyncio.create_task(
                main(log_level="INFO", runtime_paths=self._runtime_paths(tmp_path), api=True),
            )
            try:
                await asyncio.wait_for(api_shutdown_started.wait(), timeout=1)
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert not main_task.done()
                assert not api_cancelled.is_set()
                api_allow_finish.set()
                await asyncio.wait_for(main_task, timeout=1)
            finally:
                api_allow_finish.set()
                if not main_task.done():
                    main_task.cancel()
                await asyncio.gather(main_task, return_exceptions=True)

        assert api_completed.is_set()
        assert not api_cancelled.is_set()
        mock_orchestrator.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orchestrator_main_stops_when_api_server_requests_shutdown(self, tmp_path: Path) -> None:
        """Regression coverage for API server signal shutdown not leaving the process half alive."""
        reset_runtime_state()
        start_released = asyncio.Event()
        mock_orchestrator = MagicMock()
        mock_orchestrator.knowledge_refresh_scheduler = None

        async def _start() -> None:
            await start_released.wait()

        async def _stop() -> None:
            start_released.set()

        async def _api_requests_shutdown(
            _host: str,
            _port: int,
            _log_level: str,
            _runtime_paths: RuntimePaths,
            _knowledge_refresh_scheduler: object,
            shutdown_requested: asyncio.Event | None,
        ) -> None:
            assert shutdown_requested is not None
            shutdown_requested.set()

        async def _blocked_auxiliary_task(*_args: object, **_kwargs: object) -> None:
            await asyncio.Event().wait()

        mock_orchestrator.start = AsyncMock(side_effect=_start)
        mock_orchestrator.stop = AsyncMock(side_effect=_stop)

        with (
            patch("mindroom.orchestrator.setup_logging"),
            patch("mindroom.orchestrator.sync_env_to_credentials"),
            patch("mindroom.orchestrator.MultiAgentOrchestrator", return_value=mock_orchestrator),
            patch("mindroom.orchestrator._run_auxiliary_task_forever", new=_blocked_auxiliary_task),
            patch("mindroom.orchestrator._run_api_server", side_effect=_api_requests_shutdown),
        ):
            await main(log_level="INFO", runtime_paths=self._runtime_paths(tmp_path), api=True)

        mock_orchestrator.stop.assert_awaited_once()
        mock_orchestrator.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orchestrator_main_fails_when_api_server_exits_unexpectedly(self, tmp_path: Path) -> None:
        """An unexpected API-server task failure should stop the top-level run non-silently."""
        reset_runtime_state()
        mock_orchestrator = MagicMock()
        mock_orchestrator.knowledge_refresh_scheduler = None
        mock_orchestrator.stop = AsyncMock()
        start_blocker = asyncio.Event()

        async def _start() -> None:
            await start_blocker.wait()

        mock_orchestrator.start = AsyncMock(side_effect=_start)

        async def _blocked_auxiliary_task(*_args: object, **_kwargs: object) -> None:
            await asyncio.Event().wait()

        async def _api_fails(*_args: object, **_kwargs: object) -> None:
            msg = "Embedded API server exited unexpectedly"
            raise RuntimeError(msg)

        with (
            patch("mindroom.orchestrator.setup_logging"),
            patch("mindroom.orchestrator.sync_env_to_credentials"),
            patch("mindroom.orchestrator.MultiAgentOrchestrator", return_value=mock_orchestrator),
            patch("mindroom.orchestrator._run_auxiliary_task_forever", new=_blocked_auxiliary_task),
            patch("mindroom.orchestrator._run_api_server", side_effect=_api_fails),
            pytest.raises(RuntimeError, match="Embedded API server exited unexpectedly"),
        ):
            await main(log_level="INFO", runtime_paths=self._runtime_paths(tmp_path), api=True)

        mock_orchestrator.stop.assert_awaited_once()

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
            stream_kwargs = mock_stream_agent_response.call_args.kwargs
            assert stream_kwargs["agent_name"] == "calculator"
            assert stream_kwargs["prompt"] == f"{mention_id}: What's 2+2?"
            assert stream_kwargs["model_prompt"].startswith("[")
            assert stream_kwargs["model_prompt"].endswith(f"{mention_id}: What's 2+2?")
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
            assert ai_kwargs["prompt"] == f"{mention_id}: What's 2+2?"
            assert ai_kwargs["model_prompt"].startswith("[")
            assert ai_kwargs["model_prompt"].endswith(f"{mention_id}: What's 2+2?")
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
        _install_runtime_cache_support(bot)
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
        assert mock_ai.call_args.kwargs["prompt"] == "Please send an update"
        model_prompt = mock_ai.call_args.kwargs["model_prompt"]
        assert "[Matrix metadata for tool calls]" in model_prompt
        assert "room_id: !test:localhost" in model_prompt
        assert "thread_id: none" in model_prompt
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
        _install_runtime_cache_support(bot)
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
        assert mock_ai.call_args.kwargs["prompt"] == "Please send an update"
        model_prompt = mock_ai.call_args.kwargs["model_prompt"]
        assert "[Matrix metadata for tool calls]" in model_prompt
        assert "room_id: !test:localhost" in model_prompt
        assert "thread_id: none" in model_prompt
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
        mock_stream_agent_response = MagicMock()

        with patch(
            "mindroom.delivery_gateway.send_streaming_response",
            new_callable=AsyncMock,
        ) as mock_send_streaming_response:
            mock_stream_agent_response.return_value = mock_streaming_response()
            mock_send_streaming_response.return_value = _stream_outcome("$response", "chunk")
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
        assert mock_stream_agent_response.call_args.kwargs["prompt"] == "Please reply in thread"
        model_prompt = mock_stream_agent_response.call_args.kwargs["model_prompt"]
        assert "[Matrix metadata for tool calls]" in model_prompt
        assert "room_id: !test:localhost" in model_prompt
        assert "thread_id: none" in model_prompt
        assert "reply_to_event_id: $event456" in model_prompt

    @pytest.mark.asyncio
    async def test_process_and_respond_uses_safe_thread_root_for_prompt_metadata(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Prompt metadata should prefer the stable thread root over plain reply event IDs."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        tools=["matrix_message"],
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
        target = MessageTarget.resolve(
            room_id="!test:localhost",
            thread_id=None,
            reply_to_event_id="$reply_plain:localhost",
            thread_start_root_event_id="$thread_root:localhost",
        )

        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            ai_response=mock_ai,
        ):
            await bot._response_runner.process_and_respond(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Continue",
                    reply_to_event_id="$reply_plain:localhost",
                    thread_history=[],
                    user_id="@user:localhost",
                    target=target,
                ),
            )

        model_prompt = mock_ai.call_args.kwargs["model_prompt"]
        assert "thread_id: $thread_root:localhost" in model_prompt
        assert "reply_to_event_id: $reply_plain:localhost" in model_prompt

    @pytest.mark.asyncio
    async def test_process_and_respond_keeps_thread_root_metadata_when_reply_anchor_is_root(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Thread-root replies should preserve the canonical thread id in tool-call metadata."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        tools=["matrix_message"],
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
        target = MessageTarget.resolve(
            room_id="!test:localhost",
            thread_id=None,
            reply_to_event_id="$thread_root:localhost",
            thread_start_root_event_id="$thread_root:localhost",
        )

        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            ai_response=mock_ai,
        ):
            await bot._response_runner.process_and_respond(
                _response_request(
                    room_id="!test:localhost",
                    prompt="Continue",
                    reply_to_event_id="$thread_root:localhost",
                    thread_history=[],
                    user_id="@user:localhost",
                    target=target,
                ),
            )

        model_prompt = mock_ai.call_args.kwargs["model_prompt"]
        assert "thread_id: $thread_root:localhost" in model_prompt
        assert "reply_to_event_id: $thread_root:localhost" in model_prompt

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
        mock_stream_agent_response = MagicMock(return_value=mock_streaming_response())
        with patch(
            "mindroom.delivery_gateway.send_streaming_response",
            new_callable=AsyncMock,
        ) as mock_send_streaming_response:
            mock_send_streaming_response.return_value = _stream_outcome("$response", "chunk")
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
        bot._knowledge_access_support.resolve_for_agent.assert_called_once()
        args, kwargs = bot._knowledge_access_support.resolve_for_agent.call_args
        assert args == ("calculator",)
        assert kwargs["execution_identity"] is not None

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
        _install_runtime_cache_support(bot)
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

        async def _consuming_send_streaming(*args: object, **_kwargs: object) -> StreamTransportOutcome:
            stream = args[7]  # response_stream positional arg
            async for _ in stream:
                pass
            return StreamTransportOutcome(
                last_physical_stream_event_id="$response",
                terminal_status="completed",
                rendered_body="chunk",
                visible_body_state="visible_body",
            )

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

        async def _consuming_send_streaming(*args: object, **_kwargs: object) -> StreamTransportOutcome:
            stream = args[7]
            async for _ in stream:
                pass
            return StreamTransportOutcome(
                last_physical_stream_event_id="$response",
                terminal_status="completed",
                rendered_body="hello",
                visible_body_state="visible_body",
            )

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

        async def _consuming_send_streaming(*args: object, **kwargs: object) -> StreamTransportOutcome:
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
        mock_stream_agent_response = MagicMock(return_value=mock_streaming_response())
        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
                side_effect=StreamingDeliveryError(
                    RuntimeError("boom"),
                    event_id="$terminal",
                    accumulated_text="partial\n\n**[Response interrupted by an error: boom]**",
                    tool_trace=[],
                    transport_outcome=_stream_outcome(
                        "$terminal",
                        "partial\n\n**[Response interrupted by an error: boom]**",
                        terminal_status="error",
                        failure_reason="boom",
                    ),
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

        assert _visible_response_event_id(delivery) == "$terminal"
        assert _handled_response_event_id(delivery) == "$terminal"
        assert delivery.delivery_kind is None
        assert "Response interrupted by an error" in delivery.response_text

    @pytest.mark.asyncio
    async def test_process_and_respond_applies_before_and_after_hooks_non_streaming(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Direct non-streaming delivery still applies before_response before lifecycle finalization."""
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
        _install_runtime_cache_support(bot)
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
        assert after_results == []

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
        _install_runtime_cache_support(bot)
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
    async def test_process_and_respond_streaming_ignores_post_visible_before_response_mutation(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Direct streamed delivery keeps before_response off the post-visible path before lifecycle finalization."""
        after_results: list[tuple[str, str, str, str]] = []
        before_calls = 0

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            nonlocal before_calls
            before_calls += 1
            ctx.draft.response_text = f"{ctx.draft.response_text} [hooked]"
            ctx.draft.suppress = True

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
        _install_runtime_cache_support(bot)
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [before_hook, after_hook])])
        mock_stream_agent_response = MagicMock(return_value=mock_streaming_response())
        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
            ) as mock_send_streaming_response,
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ) as mock_edit_message,
        ):
            mock_send_streaming_response.return_value = _stream_outcome("$response", "chunk")
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
        assert before_calls == 0
        mock_edit_message.assert_not_awaited()
        assert after_results == []

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
            mock_stream = MagicMock(return_value=mock_streaming_response())
            with patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
            ) as mock_send_streaming_response:
                mock_send_streaming_response.return_value = _stream_outcome("$response", "chunk")
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
        _install_runtime_cache_support(bot)
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [before_hook, after_hook])])
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        matrix_ids = config.get_ids(runtime_paths_for(config))
        with (
            patch(
                "mindroom.delivery_gateway.send_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
            ) as mock_send_message,
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ) as mock_edit_message,
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response=AsyncMock(return_value="Team reply"),
            ),
        ):
            resolution = await bot._generate_team_response_helper(
                room_id="!test:localhost",
                reply_to_event_id="$team-root",
                thread_id=None,
                team_agents=[matrix_ids["calculator"], matrix_ids["general"]],
                team_mode="collaborate",
                thread_history=[],
                requester_user_id="@user:localhost",
                payload=DispatchPayload(prompt="team prompt"),
                response_envelope=_hook_envelope(body="team prompt", source_event_id="$team-root"),
                correlation_id="corr-team",
            )

        assert _handled_response_event_id(resolution) == "$team"
        assert mock_send_message.await_args.args[2]["body"] == "🤝 Team Response: Thinking..."
        assert mock_edit_message.await_args.args[4] == "Team reply [hooked]"
        assert after_results == [("$team", "Team reply [hooked]", "edited", "team")]

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_preserves_enrichment_in_shared_team_session(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Shared team responses should never scrub enriched history after delivery."""
        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        matrix_ids = config.get_ids(runtime_paths_for(config))
        bot._conversation_state_writer.create_storage = MagicMock(return_value=MagicMock())
        with (
            patch(
                "mindroom.delivery_gateway.send_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
            ),
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response=AsyncMock(return_value="Team reply"),
            ),
        ):
            resolution = await bot._generate_team_response_helper(
                room_id="!test:localhost",
                reply_to_event_id="$team-root",
                thread_id=None,
                team_agents=[matrix_ids["calculator"], matrix_ids["general"]],
                team_mode="collaborate",
                thread_history=[],
                requester_user_id="@user:localhost",
                payload=DispatchPayload(prompt="team prompt"),
                response_envelope=_hook_envelope(body="team prompt", source_event_id="$team-root"),
                correlation_id="corr-team",
            )

        assert _handled_response_event_id(resolution) == "$team"

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_merges_raw_prompt_with_model_prompt(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Team helper must preserve the raw user prompt when model-only context is present."""
        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        matrix_ids = config.get_ids(runtime_paths_for(config))
        mock_team_response = AsyncMock(return_value="Team reply")

        with (
            patch(
                "mindroom.delivery_gateway.send_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
            ),
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response=mock_team_response,
            ),
        ):
            resolution = await bot._generate_team_response_helper(
                room_id="!test:localhost",
                reply_to_event_id="$team-root",
                thread_id=None,
                team_agents=[matrix_ids["calculator"], matrix_ids["general"]],
                team_mode="collaborate",
                thread_history=[],
                requester_user_id="@user:localhost",
                payload=DispatchPayload(
                    prompt="Summarize the latest invoice.",
                    model_prompt="Available attachment IDs: att_invoice. Use tool calls to inspect or process them.",
                ),
                response_envelope=_hook_envelope(
                    body="Summarize the latest invoice.",
                    source_event_id="$team-root",
                ),
                correlation_id="corr-team",
            )

        assert _handled_response_event_id(resolution) == "$team"
        prepared_message = mock_team_response.await_args.kwargs["message"]
        assert "Summarize the latest invoice." in prepared_message
        assert "Available attachment IDs: att_invoice." in prepared_message
        assert prepared_message.index("Summarize the latest invoice.") < prepared_message.index(
            "Available attachment IDs: att_invoice.",
        )

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_does_not_duplicate_already_timestamped_prompt(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Team helper should treat an already timestamped prompt as the same user turn."""
        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        matrix_ids = config.get_ids(runtime_paths_for(config))
        mock_team_response = AsyncMock(return_value="Team reply")
        timestamped_prompt = "[2026-03-20 08:15 PDT] What time is it?"

        with (
            patch(
                "mindroom.delivery_gateway.send_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
            ),
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response=mock_team_response,
            ),
        ):
            resolution = await bot._generate_team_response_helper(
                room_id="!test:localhost",
                reply_to_event_id="$team-root",
                thread_id=None,
                team_agents=[matrix_ids["calculator"], matrix_ids["general"]],
                team_mode="collaborate",
                thread_history=[],
                requester_user_id="@user:localhost",
                payload=DispatchPayload(
                    prompt="What time is it?",
                    model_prompt=timestamped_prompt,
                ),
                response_envelope=_hook_envelope(
                    body="What time is it?",
                    source_event_id="$team-root",
                ),
                correlation_id="corr-team",
            )

        assert _handled_response_event_id(resolution) == "$team"
        prepared_message = mock_team_response.await_args.kwargs["message"]
        assert prepared_message == timestamped_prompt

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_uses_resolved_thread_root_for_placeholder_and_edit(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Team helper should preserve the canonical thread root across placeholder and edit flow."""
        sent_contents: list[dict[str, object]] = []

        async def record_send(_client: object, _room_id: str, content: dict[str, object]) -> DeliveredMatrixEvent:
            sent_contents.append(content)
            return delivered_matrix_event("$team", content)

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
        history = ThreadHistoryResult([], is_full_history=True)

        with (
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_latest_thread_event_id_if_needed",
                new=AsyncMock(return_value="$latest:localhost"),
            ),
            patch.object(bot._conversation_cache, "get_thread_history", AsyncMock(return_value=history)),
            patch("mindroom.delivery_gateway.send_message_result", new=AsyncMock(side_effect=record_send)),
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ) as mock_edit_message,
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response=AsyncMock(return_value="Team reply"),
            ),
        ):
            resolution = await bot._generate_team_response_helper(
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

        assert _handled_response_event_id(resolution) == "$team"
        assert len(sent_contents) == 1
        content = sent_contents[0]
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$canonical_thread:localhost"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$reply_plain:localhost"
        assert mock_edit_message.await_args.args[3]["m.relates_to"]["event_id"] == "$canonical_thread:localhost"

    @pytest.mark.asyncio
    async def test_team_generate_response_nonteam_fallback_delivers_without_after_response(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Non-team fallback should deliver directly without response lifecycle hooks."""
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
            eligible_members=[],
            outcome=TeamOutcome.NONE,
            reason="No team available",
        )

        bot._edit_message = AsyncMock(return_value=True)
        bot._delivery_gateway.deliver_final = AsyncMock()
        bot._delivery_gateway.deps.response_hooks.emit_after_response = AsyncMock()
        bot._delivery_gateway.deps.response_hooks.emit_cancelled_response = AsyncMock()

        with (
            patch.object(bot._turn_policy, "materializable_agent_names", return_value={"general"}),
            patch("mindroom.bot.resolve_configured_team", return_value=resolution),
        ):
            delivery_resolution = await bot._generate_response(
                room_id="!test:localhost",
                prompt="Team, summarize this thread",
                reply_to_event_id="$event",
                thread_id="$thread",
                thread_history=[],
                existing_event_id="$existing",
                existing_event_is_placeholder=True,
                user_id="@alice:localhost",
                response_envelope=_hook_envelope(body="hello", source_event_id="$event"),
                correlation_id="corr-nonteam-fallback",
            )

        bot._delivery_gateway.deliver_final.assert_not_awaited()
        bot._edit_message.assert_awaited_once_with(
            room_id="!test:localhost",
            event_id="$existing",
            new_text="No team available",
            thread_id="$thread",
        )
        bot._delivery_gateway.deps.response_hooks.emit_after_response.assert_not_awaited()
        bot._delivery_gateway.deps.response_hooks.emit_cancelled_response.assert_not_awaited()
        assert delivery_resolution == "$existing"

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
        assert _handled_response_event_id(delivery) is None

    @pytest.mark.asyncio
    async def test_deliver_generated_response_suppressed_existing_event_returns_no_final_event(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Suppressing a non-placeholder edit should keep the prior visible event retryable."""

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
        assert delivery.event_id == "$existing"
        redact_message_event.assert_not_awaited()
        assert _handled_response_event_id(delivery) is None

    @pytest.mark.asyncio
    async def test_deliver_generated_response_raises_when_suppressed_placeholder_redaction_fails(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A failed placeholder redaction should stay inside the typed terminal contract."""

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

        outcome = await gateway.deliver_final(
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

        assert outcome.terminal_status == "error"
        assert _visible_response_event_id(outcome) == "$placeholder"
        assert _handled_response_event_id(outcome) is None
        assert outcome.mark_handled is False
        gateway.deps.response_hooks.emit_cancelled_response.assert_not_awaited()

        redact_message_event.assert_awaited_once_with(
            room_id="!test:localhost",
            event_id="$placeholder",
            reason="Suppressed placeholder response",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("hook_action"), ["rewrite", "suppress"])
    async def test_streamed_before_response_no_longer_mutates_post_visible_success(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        hook_action: str,
    ) -> None:
        """message:before_response must not mutate or suppress once streamed text is already visible."""

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            if hook_action == "rewrite":
                ctx.draft.response_text = "updated text"
            else:
                ctx.draft.suppress = True

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [before_hook])])
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        redact_message_event = AsyncMock(return_value=True)
        gateway = replace_delivery_gateway_deps(bot, redact_message_event=redact_message_event)
        mock_deliver_final = AsyncMock(
            return_value=FinalDeliveryOutcome(
                terminal_status="completed",
                event_id="$streaming",
                is_visible_response=True,
                final_visible_body="updated text",
                delivery_kind="edited",
            ),
        )
        object.__setattr__(gateway, "deliver_final", mock_deliver_final)

        outcome = await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                stream_transport_outcome=_stream_outcome("$streaming", "chunk"),
                initial_delivery_kind="sent",
                response_kind="ai",
                response_envelope=response_envelope,
                correlation_id="corr-finalize-stream-visible-failure",
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert outcome.terminal_status == "completed"
        assert outcome.final_visible_event_id == "$streaming"
        assert outcome.final_visible_body == "chunk"
        mock_deliver_final.assert_not_awaited()
        redact_message_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_finalize_streamed_response_cancelled_placeholder_only_stream_cleans_up_placeholder(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Interrupted terminal finalization must redact a placeholder-only stream instead of leaking Thinking...."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        gateway = replace_delivery_gateway_deps(
            bot,
            redact_message_event=AsyncMock(return_value=True),
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(),
                emit_after_response=AsyncMock(),
                emit_cancelled_response=AsyncMock(),
            ),
        )

        outcome = await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                stream_transport_outcome=_stream_outcome(
                    "$thinking",
                    "Thinking...",
                    terminal_status="cancelled",
                    visible_body_state="placeholder_only",
                    failure_reason="terminal_update_cancelled",
                ),
                initial_delivery_kind="edited",
                response_kind="ai",
                response_envelope=response_envelope,
                correlation_id="corr-finalize-stream-cancelled-placeholder",
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert outcome.terminal_status == "cancelled"
        gateway.deps.redact_message_event.assert_awaited_once_with(
            room_id="!test:localhost",
            event_id="$thinking",
            reason="Completed placeholder-only streamed response",
        )
        gateway.deps.response_hooks.emit_cancelled_response.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_finalize_streamed_response_placeholder_cleanup_failure_is_unhandled(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Failed placeholder-only cleanup should leave the user turn retryable."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        gateway = replace_delivery_gateway_deps(
            bot,
            redact_message_event=AsyncMock(return_value=False),
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(),
                emit_after_response=AsyncMock(),
                emit_cancelled_response=AsyncMock(),
            ),
        )

        outcome = await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                stream_transport_outcome=_stream_outcome(
                    "$thinking",
                    "Thinking...",
                    terminal_status="cancelled",
                    visible_body_state="placeholder_only",
                    failure_reason="terminal_update_cancelled",
                ),
                initial_delivery_kind="edited",
                response_kind="ai",
                response_envelope=response_envelope,
                correlation_id="corr-finalize-stream-placeholder-cleanup-failed",
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert outcome.terminal_status == "error"
        assert outcome.event_id == "$thinking"
        assert outcome.is_visible_response is False
        assert outcome.mark_handled is False

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_does_not_mark_responded_when_cancelled_visible_note_survives(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Visible cancellation artifacts must not mark the source as handled."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        bot.logger = MagicMock()

        room = nio.MatrixRoom(room_id="!room:localhost", own_user_id=bot.matrix_id)
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
            correlation_id="corr-visible-cancel-note",
            envelope=_hook_envelope(body="hello", source_event_id="$event"),
        )

        async def payload_builder(_context: MessageContext) -> DispatchPayload:
            return DispatchPayload(prompt="help me")

        with (
            patch.object(
                bot._response_runner,
                "generate_response",
                new=AsyncMock(return_value="$cancelled"),
            ),
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
        tracker.record_handled_turn.assert_called_once_with(
            HandledTurnState.from_source_event_id(event.event_id).with_response_event_id("$cancelled"),
        )

    @pytest.mark.asyncio
    async def test_streamed_regeneration_against_an_existing_visible_reply_preserves_linkage_when_no_new_body_lands(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A no-op streamed regeneration should keep the prior visible reply linked."""

        async def mock_streaming_response() -> AsyncGenerator[str, None]:
            if False:
                yield "chunk"

        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        bot._handle_interactive_question = AsyncMock()

        with (
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=MagicMock(return_value=mock_streaming_response()),
            ),
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
            ) as mock_send_streaming_response,
        ):
            mock_send_streaming_response.return_value = StreamTransportOutcome(
                last_physical_stream_event_id="$existing",
                terminal_status="completed",
                rendered_body=None,
                visible_body_state="none",
            )
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
                    correlation_id="corr-stream-regenerate-noop",
                    existing_event_id="$existing",
                    existing_event_is_placeholder=False,
                ),
            )

        assert delivery.terminal_status == "completed"
        assert _visible_response_event_id(delivery) == "$existing"
        assert _handled_response_event_id(delivery) == "$existing"
        assert delivery.mark_handled is True

    def test_response_outcome_prefers_terminal_status_over_delivery_kind(self) -> None:
        """Pipeline outcome summaries must not report cancelled or error states as plain send/edit success."""
        assert (
            response_outcome_label(
                _outcome(
                    terminal_status="cancelled",
                    final_visible_event_id="$cancelled",
                    visible_response_event_id="$cancelled",
                    turn_completion_event_id="$cancelled",
                    final_visible_body="Cancelled.",
                    delivery_kind="edited",
                ),
            )
            == "cancelled"
        )
        assert (
            response_outcome_label(
                _outcome(
                    terminal_status="error",
                    final_visible_event_id="$error",
                    visible_response_event_id="$error",
                    final_visible_body="boom",
                ),
            )
            == "error"
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
        _install_runtime_cache_support(bot)
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
            patch(
                "mindroom.delivery_gateway.send_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
            ),
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ),
            patch("mindroom.bot.interactive.register_interactive_question") as mock_register,
            patch("mindroom.bot.interactive.add_reaction_buttons", new_callable=AsyncMock) as mock_add_buttons,
        ):
            resolution = await bot._generate_team_response_helper(
                room_id="!test:localhost",
                reply_to_event_id="$team-root",
                thread_id=None,
                team_agents=[matrix_ids["calculator"], matrix_ids["general"]],
                team_mode="collaborate",
                thread_history=[],
                requester_user_id="@user:localhost",
                payload=DispatchPayload(prompt="team prompt"),
            )

        assert _handled_response_event_id(resolution) == "$team"
        mock_register.assert_called_once()
        assert mock_register.call_args.args[0] == "$team"
        assert mock_register.call_args.args[1] == "!test:localhost"
        assert mock_register.call_args.args[2] is None
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

        with patch("mindroom.bot.interactive.handle_reaction", new=AsyncMock(return_value=False)):
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

    @pytest.mark.asyncio
    async def test_unknown_tool_approval_response_with_approval_id_and_denial_reason_resolves_live_waiter(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Cinny custom approval responses should resolve by approval_id alone."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        _store, pending, task, editor = await _start_live_approval(runtime_paths)

        try:
            event = SimpleNamespace(
                type="io.mindroom.tool_approval_response",
                source={
                    "sender": "@user:localhost",
                    "content": {
                        "approval_id": pending.approval_id,
                        "status": "denied",
                        "denial_reason": "Not this time.",
                    },
                },
            )
            await bot._on_unknown_event(room, event)
            decision = await task

            assert decision.status == "denied"
            assert decision.reason == "Not this time."
            assert editor.await_args.args[1] == "$approval"
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_unknown_tool_approval_response_with_approval_id_and_non_card_reply_resolves_live_waiter(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Custom approval responses should fall back to approval_id when reply metadata is not the card."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        _store, pending, task, editor = await _start_live_approval(runtime_paths)

        try:
            event = SimpleNamespace(
                type="io.mindroom.tool_approval_response",
                source={
                    "sender": "@user:localhost",
                    "content": {
                        "approval_id": pending.approval_id,
                        "status": "denied",
                        "denial_reason": "Wrong arguments.",
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": "$thread",
                            "m.in_reply_to": {"event_id": "$latest-thread-event"},
                        },
                    },
                },
            )
            await bot._on_unknown_event(room, event)
            decision = await task

            assert decision.status == "denied"
            assert decision.reason == "Wrong arguments."
            assert editor.await_args.args[1] == "$approval"
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_unknown_tool_approval_response_with_approval_id_uses_live_id_entrypoint(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Approval-id-only custom events should use the live-id manager API."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        event = nio.UnknownEvent.from_dict(
            {
                "type": "io.mindroom.tool_approval_response",
                "sender": "@user:localhost",
                "event_id": "$response",
                "origin_server_ts": 1,
                "content": {"approval_id": "approval-1", "status": "approved"},
            },
        )
        with patch(
            "mindroom.approval_inbound.handle_matrix_approval_action",
            new=AsyncMock(return_value=ApprovalActionResult(consumed=True, resolved=True, card_event_id="$approval")),
        ) as handle_matrix_approval_action:
            await bot._on_unknown_event(room, event)

        handle_matrix_approval_action.assert_awaited_once_with(
            MatrixApprovalAction(
                room_id="!test:localhost",
                sender_id="@user:localhost",
                card_event_id=None,
                approval_id="approval-1",
                status="approved",
                reason=None,
            ),
        )

    @pytest.mark.asyncio
    async def test_unknown_truncated_approval_id_response_sends_notice_with_card_event_id(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Approval-id-only responses should still send truncated-argument denial notices."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = make_matrix_client_mock()
        orchestrator = MagicMock()
        orchestrator.send_approval_notice = AsyncMock(return_value=True)
        bot.orchestrator = orchestrator
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        _store, pending, task, editor = await _start_live_approval(
            runtime_paths,
            arguments={"content": "x" * 10_000},
        )

        try:
            event = SimpleNamespace(
                type="io.mindroom.tool_approval_response",
                source={
                    "sender": "@user:localhost",
                    "content": {"approval_id": pending.approval_id, "status": "approved"},
                },
            )
            await bot._on_unknown_event(room, event)
            decision = await task

            assert decision.status == "denied"
            assert "displayed arguments are truncated" in (decision.reason or "")
            replacement = editor.await_args.args[2]
            assert replacement["status"] == "denied"
            assert "displayed arguments are truncated" in replacement["resolution_reason"]
            orchestrator.send_approval_notice.assert_awaited_once_with(
                room_id="!test:localhost",
                approval_event_id=pending.card_event_id,
                thread_id=pending.thread_id,
                reason=replacement["resolution_reason"],
            )
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_non_router_bot_truncated_approval_race_sends_notice_via_orchestrator(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A non-router bot that wins the approval callback race should still trigger notice delivery."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        agent_bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        agent_bot.client = make_matrix_client_mock(user_id="@mindroom_general:localhost")
        router_bot = MagicMock()
        router_bot.client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
        orchestrator = MagicMock()
        orchestrator.send_approval_notice = AsyncMock(return_value=True)
        agent_bot.orchestrator = orchestrator
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        _store, pending, task, editor = await _start_live_approval(
            runtime_paths,
            arguments={"content": "x" * 10_000},
        )

        try:
            handled = await handle_tool_approval_action(
                room=room,
                sender_id="@user:localhost",
                config=agent_bot.config,
                runtime_paths=agent_bot.runtime_paths,
                orchestrator=agent_bot.orchestrator,
                logger=agent_bot.logger,
                approval_event_id=pending.card_event_id,
                status="approved",
                reason=None,
            )
            decision = await task

            assert handled is True
            assert decision.status == "denied"
            replacement = editor.await_args.args[2]
            assert "displayed arguments are truncated" in replacement["resolution_reason"]
            orchestrator.send_approval_notice.assert_awaited_once_with(
                room_id="!test:localhost",
                approval_event_id=pending.card_event_id,
                thread_id=pending.thread_id,
                reason=replacement["resolution_reason"],
            )
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_reply_text_from_non_approver_falls_through_to_normal_handler(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Non-approver approval replies should fall through to normal text handling."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot._turn_controller.handle_text_event = AsyncMock()
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        store, pending, task, editor = await _start_live_approval(
            runtime_paths,
            approver_user_id="@approver:localhost",
        )
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$reply"
        event.sender = "@other:localhost"
        event.body = "I should not resolve this."
        event.server_timestamp = 1234
        event.source = {
            "event_id": "$reply",
            "sender": "@other:localhost",
            "origin_server_ts": 1234,
            "content": {
                "m.relates_to": {"m.in_reply_to": {"event_id": pending.card_event_id}},
            },
        }

        try:
            await bot._on_message(room, event)

            bot._turn_controller.handle_text_event.assert_awaited_once_with(room, event)
            editor.assert_not_awaited()
            assert task.done() is False

            await store.handle_card_response(
                room_id="!test:localhost",
                sender_id="@approver:localhost",
                card_event_id=pending.card_event_id,
                status="approved",
                reason=None,
            )
            decision = await task
            assert decision.status == "approved"
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_plain_rich_reply_does_not_probe_approval_cache(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Ordinary rich replies should not touch approval cache lookup."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot._turn_controller.handle_text_event = AsyncMock()
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        event_cache = MagicMock()
        event_cache.get_event = AsyncMock(side_effect=RuntimeError("cache should not run"))
        store = initialize_approval_store(
            runtime_paths,
            event_cache=event_cache,
        )
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$ordinary-rich-reply"
        event.sender = "@user:localhost"
        event.body = "!help"
        event.server_timestamp = 1234
        event.source = {
            "event_id": "$ordinary-rich-reply",
            "sender": "@user:localhost",
            "origin_server_ts": 1234,
            "content": {
                "m.relates_to": {"m.in_reply_to": {"event_id": "$ordinary-message"}},
            },
        }

        try:
            await bot._on_message(room, event)

            bot._turn_controller.handle_text_event.assert_awaited_once_with(room, event)
            event_cache.get_event.assert_not_awaited()
            assert store is get_approval_store()
        finally:
            await shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_plain_thread_reply_with_approval_store_does_not_require_room_alias(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Ordinary replies should not run approval authorization before matching an in-memory card."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot._turn_controller.handle_text_event = AsyncMock()
        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.matrix_id)
        initialize_approval_store(runtime_paths)
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$ordinary-thread-reply"
        event.sender = "@user:localhost"
        event.body = "ordinary reply"
        event.server_timestamp = 1234
        event.source = {
            "event_id": "$ordinary-thread-reply",
            "sender": "@user:localhost",
            "origin_server_ts": 1234,
            "content": {
                "m.relates_to": {"m.in_reply_to": {"event_id": "$ordinary-message"}},
            },
        }

        try:
            await bot._on_message(room, event)

            bot._turn_controller.handle_text_event.assert_awaited_once_with(room, event)
        finally:
            await shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_duplicate_live_approval_reply_is_consumed_without_falling_through(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Duplicate approver replies should be consumed while the first resolution is in flight."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot._turn_controller.handle_text_event = AsyncMock()
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        edit_started = asyncio.Event()
        release_edit = asyncio.Event()

        async def slow_editor(_room_id: str, _event_id: str, _content: dict[str, Any]) -> bool:
            edit_started.set()
            await release_edit.wait()
            return True

        store, pending, task, editor = await _start_live_approval(
            runtime_paths,
            editor=AsyncMock(side_effect=slow_editor),
        )
        first_resolution = asyncio.create_task(
            store.handle_card_response(
                room_id="!test:localhost",
                sender_id="@user:localhost",
                card_event_id=pending.card_event_id,
                status="approved",
                reason=None,
            ),
        )
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$duplicate-approval-reply"
        event.sender = "@user:localhost"
        event.body = "No, deny it."
        event.server_timestamp = 1234
        event.source = {
            "event_id": "$duplicate-approval-reply",
            "sender": "@user:localhost",
            "origin_server_ts": 1234,
            "content": {
                "m.relates_to": {"m.in_reply_to": {"event_id": pending.card_event_id}},
            },
        }

        try:
            await asyncio.wait_for(edit_started.wait(), timeout=1)
            await bot._on_message(room, event)

            bot._turn_controller.handle_text_event.assert_not_awaited()
            release_edit.set()
            first_result = await first_resolution
            decision = await task

            assert first_result.resolved is True
            assert decision.status == "approved"
            assert editor.await_count == 1
        finally:
            release_edit.set()
            if not first_resolution.done():
                first_resolution.cancel()
                with suppress(asyncio.CancelledError):
                    await first_resolution
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_reply_to_resolved_approval_card_falls_through_to_normal_text(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Follow-up text on a terminal approval card should remain a normal message."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot._turn_controller.handle_text_event = AsyncMock()
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        store, pending, task, _editor = await _start_live_approval(runtime_paths)

        try:
            result = await store.handle_card_response(
                room_id="!test:localhost",
                sender_id="@user:localhost",
                card_event_id=pending.card_event_id,
                status="approved",
                reason=None,
            )
            decision = await task
            assert result.resolved is True
            assert decision.status == "approved"

            event = MagicMock(spec=nio.RoomMessageText)
            event.event_id = "$follow-up-reply"
            event.sender = "@user:localhost"
            event.body = "Why did this fail?"
            event.server_timestamp = 1234
            event.source = {
                "event_id": "$follow-up-reply",
                "sender": "@user:localhost",
                "origin_server_ts": 1234,
                "content": {
                    "m.relates_to": {"m.in_reply_to": {"event_id": pending.card_event_id}},
                },
            }

            await bot._on_message(room, event)

            bot._turn_controller.handle_text_event.assert_awaited_once_with(room, event)
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_checkmark_reaction_reaches_approval_manager_with_card_id_and_sender(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Checkmark reactions should dispatch approval actions to the manager."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = make_matrix_client_mock()
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        event = MagicMock(spec=nio.ReactionEvent)
        event.key = "✅"
        event.reacts_to = "$approval"
        event.sender = "@user:localhost"
        event.event_id = "$reaction"
        with patch(
            "mindroom.approval_inbound.handle_matrix_approval_action",
            new=AsyncMock(return_value=ApprovalActionResult(consumed=True, resolved=True)),
        ) as handle_matrix_approval_action:
            await bot._on_reaction(room, event)

        handle_matrix_approval_action.assert_awaited_once_with(
            MatrixApprovalAction(
                room_id="!test:localhost",
                sender_id="@user:localhost",
                card_event_id="$approval",
                approval_id=None,
                status="approved",
                reason=None,
            ),
        )

    @pytest.mark.asyncio
    async def test_reaction_hooks_inherit_thread_for_promoted_plain_reply_target(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """reaction:received hooks should reuse inherited thread membership for promoted plain replies."""
        seen: list[tuple[str, str | None]] = []

        @hook(EVENT_REACTION_RECEIVED)
        async def record_reaction(ctx: ReactionReceivedContext) -> None:
            seen.append((ctx.target_event_id, ctx.thread_id))

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = make_matrix_client_mock()
        _install_runtime_cache_support(bot)
        bot._conversation_cache.get_thread_id_for_event = AsyncMock(
            side_effect=lambda room_id, event_id: (
                "$thread-root" if (room_id, event_id) == ("!test:localhost", "$thread-reply") else None
            ),
        )
        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "bridged plain reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply"}},
                    },
                    "event_id": "$plain-reply",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            ),
        )
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [record_reaction])])
        room = MagicMock()
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        event = self._make_handler_event("reaction", sender="@user:localhost", event_id="$reaction")
        event.reacts_to = "$plain-reply"
        event.source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$plain-reply",
                    "key": "👍",
                },
            },
        }

        with patch("mindroom.bot.interactive.handle_reaction", new=AsyncMock(return_value=False)):
            await bot._on_reaction(room, event)

        assert seen == [("$plain-reply", "$thread-root")]

    @pytest.mark.asyncio
    async def test_reaction_hooks_inherit_thread_transitively_through_plain_reply_chain(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """reaction:received hooks should follow the transitive reply chain to the threaded ancestor."""
        seen: list[tuple[str, str | None]] = []

        @hook(EVENT_REACTION_RECEIVED)
        async def record_reaction(ctx: ReactionReceivedContext) -> None:
            seen.append((ctx.target_event_id, ctx.thread_id))

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = make_matrix_client_mock()
        _install_runtime_cache_support(bot)
        bot._conversation_cache.get_thread_id_for_event = AsyncMock(
            side_effect=lambda room_id, event_id: (
                "$thread-root" if (room_id, event_id) == ("!test:localhost", "$thread-reply") else None
            ),
        )

        def room_get_event_response(event_id: str, content: dict[str, object]) -> nio.RoomGetEventResponse:
            return nio.RoomGetEventResponse.from_dict(
                {
                    "content": content,
                    "event_id": event_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            )

        async def fetch_related_event(_room_id: str, event_id: str) -> nio.RoomGetEventResponse:
            if event_id == "$plain-reply-2":
                return room_get_event_response(
                    "$plain-reply-2",
                    {
                        "body": "second bridged plain reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$plain-reply-1"}},
                    },
                )
            if event_id == "$plain-reply-1":
                return room_get_event_response(
                    "$plain-reply-1",
                    {
                        "body": "first bridged plain reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply"}},
                    },
                )
            msg = f"unexpected event lookup: {event_id}"
            raise AssertionError(msg)

        bot.client.room_get_event = AsyncMock(side_effect=fetch_related_event)
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [record_reaction])])
        room = MagicMock()
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        event = self._make_handler_event("reaction", sender="@user:localhost", event_id="$reaction")
        event.reacts_to = "$plain-reply-2"
        event.source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$plain-reply-2",
                    "key": "👍",
                },
            },
        }

        with patch("mindroom.bot.interactive.handle_reaction", new=AsyncMock(return_value=False)):
            await bot._on_reaction(room, event)

        assert seen == [("$plain-reply-2", "$thread-root")]

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
        _install_runtime_cache_support(bot)
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
                    return_value=FinalDeliveryOutcome(
                        terminal_status="completed",
                        event_id="$response",
                        is_visible_response=True,
                        final_visible_body="ok",
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
                    return_value=FinalDeliveryOutcome(
                        terminal_status="completed",
                        event_id="$response",
                        is_visible_response=True,
                        final_visible_body="ok",
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
                    return_value=FinalDeliveryOutcome(
                        terminal_status="completed",
                        event_id="$thinking",
                        is_visible_response=True,
                        final_visible_body="",
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

        async def cached_history_refresh(
            _room_id: str,
            _thread_id: str,
            *,
            full_history: bool,
            dispatch_safe: bool,
            caller_label: str,
        ) -> list[ResolvedVisibleMessage]:
            assert full_history is True
            assert dispatch_safe is True
            assert caller_label == "dispatch_post_lock_refresh"
            return fresh_history

        with (
            patch.object(
                ResponseRunner,
                "process_and_respond",
                new=AsyncMock(
                    return_value=FinalDeliveryOutcome(
                        terminal_status="completed",
                        event_id="$response",
                        is_visible_response=True,
                        final_visible_body="ok",
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
                "get_thread_messages",
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
                resolution = await bot._generate_response(
                    room_id="!test:localhost",
                    prompt="Continue",
                    reply_to_event_id="$event",
                    thread_id="$thread",
                    thread_history=stale_history,
                    user_id="@alice:localhost",
                )

        assert _handled_response_event_id(resolution) == "$response"
        mock_get_thread_history.assert_awaited_once_with(
            "!test:localhost",
            "$thread",
            full_history=True,
            dispatch_safe=True,
            caller_label="dispatch_post_lock_refresh",
        )
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

        async def record_send(_client: object, _room_id: str, content: dict[str, object]) -> DeliveredMatrixEvent:
            sent_contents.append(content)
            return delivered_matrix_event("$thinking", content)

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
                thread_start_root_event_id="$thread_root:localhost",
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
                    return_value=FinalDeliveryOutcome(
                        terminal_status="completed",
                        event_id="$thinking",
                        is_visible_response=True,
                        final_visible_body="ok",
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
            patch("mindroom.delivery_gateway.send_message_result", new=AsyncMock(side_effect=record_send)),
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
        bot._knowledge_access_support.resolve_for_agent = MagicMock(return_value=KnowledgeResolution(knowledge=None))
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
            patch(
                "mindroom.delivery_gateway.send_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$response")),
            ),
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ),
            patch.object(
                bot._conversation_cache,
                "get_dispatch_thread_history",
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

        assert mock_get_thread_history.await_count >= 1
        assert all(
            await_args.args == ("!test:localhost", "$thread") for await_args in mock_get_thread_history.await_args_list
        )
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
    async def test_generate_response_keeps_first_turn_follow_up_effects_in_new_thread(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """First-turn threaded replies should keep compaction notices and summaries in the resolved thread."""

        async def fake_store_conversation_memory(*_args: object, **_kwargs: object) -> None:
            return None

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            await kwargs["compaction_lifecycle"].start(
                CompactionLifecycleStart(
                    mode="auto",
                    session_id="session-1",
                    scope="agent:test_agent",
                    summary_model="summary-model",
                    before_tokens=30_000,
                    history_budget_tokens=100_000,
                    runs_before=20,
                ),
            )
            return "ok"

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
        config.defaults.thread_summary_first_threshold = 1
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        _install_runtime_cache_support(bot)
        bot._knowledge_access_support.resolve_for_agent = MagicMock(return_value=KnowledgeResolution(knowledge=None))
        root_event_id = "$root_event"
        resolved_target = MessageTarget.resolve(
            room_id="!test:localhost",
            thread_id=None,
            reply_to_event_id=root_event_id,
        ).with_thread_root(root_event_id)
        response_envelope = replace(_hook_envelope(source_event_id=root_event_id), target=resolved_target)

        with (
            patch("mindroom.response_runner.typing_indicator", _noop_typing_indicator),
            patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
            patch("mindroom.response_runner.ai_response", new_callable=AsyncMock, side_effect=fake_ai_response),
            patch(
                "mindroom.delivery_gateway.send_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$response")),
            ),
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ),
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
            patch(
                "mindroom.delivery_gateway.DeliveryGateway.send_compaction_lifecycle_start",
                new=AsyncMock(return_value="$notice"),
            ) as mock_send_compaction_lifecycle_start,
        ):
            await bot._generate_response(
                room_id="!test:localhost",
                prompt="Start a thread here",
                reply_to_event_id=root_event_id,
                thread_id=None,
                thread_history=[],
                user_id="@alice:localhost",
                target=resolved_target,
                response_envelope=response_envelope,
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        mock_thread_summary.assert_awaited_once_with(
            client=bot.client,
            room_id="!test:localhost",
            thread_id=root_event_id,
            config=config,
            runtime_paths=bot.runtime_paths,
            conversation_cache=bot._conversation_cache,
            message_count_hint=1,
        )
        mock_send_compaction_lifecycle_start.assert_awaited_once()
        compaction_notice_kwargs = mock_send_compaction_lifecycle_start.await_args.kwargs
        assert compaction_notice_kwargs["target"].resolved_thread_id == root_event_id
        assert compaction_notice_kwargs["reply_to_event_id"] == "$response"
        assert "thread_summary_!test:localhost_$root_event" in scheduled_names

    @pytest.mark.asyncio
    async def test_generate_response_marks_non_streaming_model_error_unsuccessful_for_post_effects(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A delivered non-streaming Matrix error reply should not be a successful run outcome."""
        captured_outcomes: list[ResponseOutcome] = []

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            assert {"post" + "_response_compaction_checks_collector"}.isdisjoint(kwargs)
            return "friendly-error"

        async def fake_apply_post_response_effects(
            _delivery: FinalDeliveryOutcome,
            outcome: ResponseOutcome,
            _deps: object,
        ) -> None:
            captured_outcomes.append(outcome)

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        _install_runtime_cache_support(bot)
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            should_use_streaming=AsyncMock(return_value=False),
            ai_response=AsyncMock(side_effect=fake_ai_response),
            apply_post_response_effects=AsyncMock(side_effect=fake_apply_post_response_effects),
        ):
            await bot._generate_response(
                room_id="!test:localhost",
                prompt="Please answer",
                reply_to_event_id="$event",
                thread_id=None,
                thread_history=[],
                user_id="@alice:localhost",
            )

        assert len(captured_outcomes) == 1
        assert captured_outcomes[0].run_succeeded is False

    @pytest.mark.asyncio
    async def test_generate_response_marks_streaming_model_error_unsuccessful_for_post_effects(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A delivered streaming Matrix error reply should not be a successful run outcome."""
        captured_outcomes: list[ResponseOutcome] = []

        async def fake_stream_agent_response(*_args: object, **kwargs: object) -> AsyncGenerator[str, None]:
            assert {"post" + "_response_compaction_checks_collector"}.isdisjoint(kwargs)
            yield "friendly-error"

        async def fake_send_streaming_response(*args: object, **_kwargs: object) -> StreamTransportOutcome:
            response_stream = cast("AsyncGenerator[object, None]", args[7])
            body_parts = [str(chunk) async for chunk in response_stream]
            return _stream_outcome("$response", "".join(body_parts))

        async def fake_apply_post_response_effects(
            _delivery: FinalDeliveryOutcome,
            outcome: ResponseOutcome,
            _deps: object,
        ) -> None:
            captured_outcomes.append(outcome)

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        _install_runtime_cache_support(bot)
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new=AsyncMock(side_effect=fake_send_streaming_response),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=True),
                stream_agent_response=MagicMock(side_effect=fake_stream_agent_response),
                apply_post_response_effects=AsyncMock(side_effect=fake_apply_post_response_effects),
            ),
        ):
            await bot._generate_response(
                room_id="!test:localhost",
                prompt="Please answer",
                reply_to_event_id="$event",
                thread_id=None,
                thread_history=[],
                user_id="@alice:localhost",
            )

        assert len(captured_outcomes) == 1
        assert captured_outcomes[0].run_succeeded is False

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
        history = _empty_full_thread_history()

        with (
            patch.object(
                ResponseRunner,
                "process_and_respond",
                new=AsyncMock(
                    return_value=FinalDeliveryOutcome(
                        terminal_status="completed",
                        event_id="$response",
                        is_visible_response=True,
                        final_visible_body="ok",
                    ),
                ),
            ),
            patch.object(
                ResponseRunner,
                "run_cancellable_response",
                new=AsyncMock(side_effect=run_cancellable_response),
            ),
            patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
            patch.object(bot._conversation_cache, "get_dispatch_thread_history", AsyncMock(return_value=history)),
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
            resolution = await task

        assert _handled_response_event_id(resolution) == "$response"

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
        history = _empty_full_thread_history()

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
            patch.object(bot._conversation_cache, "get_dispatch_thread_history", AsyncMock(return_value=history)),
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
        refreshed_history = ThreadHistoryResult(list(thread_history), is_full_history=True)

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
            patch.object(
                bot._conversation_cache,
                "get_dispatch_thread_history",
                AsyncMock(return_value=refreshed_history),
            ),
            patch(
                "mindroom.post_response_effects.maybe_generate_thread_summary",
                new_callable=AsyncMock,
            ) as mock_thread_summary,
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

        mock_thread_summary.assert_not_called()

    @pytest.mark.asyncio
    async def test_team_generate_response_keeps_streamed_visible_reply_when_before_response_suppresses(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """TeamBot must keep a visible streamed reply even if before_response tries to suppress it afterwards."""

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
        history = _empty_full_thread_history()
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
            patch.object(bot._conversation_cache, "get_dispatch_thread_history", AsyncMock(return_value=history)),
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new=AsyncMock(
                    return_value=StreamTransportOutcome(
                        last_physical_stream_event_id="$team-response",
                        terminal_status="completed",
                        rendered_body="Team reply",
                        visible_body_state="visible_body",
                    ),
                ),
            ),
            patch("mindroom.response_runner.create_background_task", side_effect=schedule_background_task),
            patch("mindroom.post_response_effects.create_background_task", side_effect=schedule_background_task),
            patch("mindroom.bot.store_conversation_memory", side_effect=fake_store_conversation_memory),
            patch(
                "mindroom.post_response_effects.maybe_generate_thread_summary",
                new_callable=AsyncMock,
            ) as mock_thread_summary,
        ):
            resolution = await bot._generate_response(
                room_id="!test:localhost",
                prompt="Team, summarize this thread",
                reply_to_event_id="$event",
                thread_id="$thread",
                thread_history=[],
                user_id="@alice:localhost",
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        assert resolution == "$team-response"
        mock_thread_summary.assert_awaited_once()
        assert "thread_summary_!test:localhost_$thread" in scheduled_names

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
        history = _empty_full_thread_history()
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
            patch.object(bot._conversation_cache, "get_thread_history", AsyncMock(return_value=history)),
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new=AsyncMock(
                    return_value=StreamTransportOutcome(
                        last_physical_stream_event_id="$placeholder",
                        terminal_status="completed",
                        rendered_body="stream chunk",
                        visible_body_state="visible_body",
                    ),
                ),
            ) as mock_send_streaming_response,
        ):
            resolution = await bot._generate_team_response_helper(
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

        assert _handled_response_event_id(resolution) == "$placeholder"
        assert _visible_response_event_id(resolution) == "$placeholder"
        mock_team_response.assert_not_awaited()
        send_kwargs = mock_send_streaming_response.await_args.kwargs
        assert send_kwargs["existing_event_id"] == "$placeholder"
        assert send_kwargs["adopt_existing_placeholder"] is True

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_keeps_streamed_visible_reply_when_before_response_suppresses(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming team helpers must keep the visible reply once real streamed text lands."""

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
        history = _empty_full_thread_history()

        with (
            patch.object(
                unwrap_extracted_collaborator(bot._response_runner),
                "run_cancellable_response",
                new=AsyncMock(side_effect=run_cancellable_response),
            ),
            patch.object(bot._conversation_cache, "get_thread_history", AsyncMock(return_value=history)),
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new=AsyncMock(
                    return_value=StreamTransportOutcome(
                        last_physical_stream_event_id="$placeholder",
                        terminal_status="completed",
                        rendered_body="stream chunk",
                        visible_body_state="visible_body",
                    ),
                ),
            ),
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=True),
                typing_indicator=noop_typing_indicator,
                team_response_stream=fake_team_response_stream,
            ),
        ):
            resolution = await bot._generate_team_response_helper(
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

        assert resolution == "$placeholder"
        bot._redact_message_event.assert_not_awaited()

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
        history = _empty_full_thread_history()

        with (
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=False),
                typing_indicator=_noop_typing_indicator,
                team_response=AsyncMock(return_value="Team handled"),
            ),
            patch.object(bot._conversation_cache, "get_thread_history", AsyncMock(return_value=history)),
        ):
            resolution = await bot._generate_team_response_helper(
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

        assert resolution is None
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

        event = _room_image_event(sender="@user:localhost", event_id="$img_event", body="photo.jpg")
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
            await drain_coalescing(bot)

        bot._generate_response.assert_awaited_once()
        generate_kwargs = bot._generate_response.await_args.kwargs
        assert generate_kwargs["room_id"] == "!test:localhost"
        assert "Available attachment IDs" not in generate_kwargs["prompt"]
        assert generate_kwargs["model_prompt"] is not None
        assert "Available attachment IDs" in generate_kwargs["model_prompt"]
        assert attachment_id in generate_kwargs["model_prompt"]
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
        expected_handled_turn = _agent_response_handled_turn(
            agent_name=mock_agent_user.agent_name,
            room_id=room.room_id,
            event_id="$img_event",
            response_event_id="$response",
            requester_id="@user:localhost",
            correlation_id="$img_event",
            source_event_prompts={"$img_event": "[Attached image]"},
        )
        expected_handled_turn = replace(
            expected_handled_turn,
            conversation_target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id=None,
                reply_to_event_id="$img_event",
            ).with_thread_root("$img_event"),
        )
        tracker.record_handled_turn.assert_called_once_with(
            expected_handled_turn,
        )

    @pytest.mark.asyncio
    async def test_media_dispatch_appends_live_event_before_enqueue(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Image/file media dispatch should update the live cache before enqueueing dispatch."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        room = MagicMock()
        room.room_id = "!test:localhost"
        event = self._make_handler_event("image", sender="@user:localhost", event_id="$img_event")
        prechecked_event = SimpleNamespace(event=event, requester_user_id="@user:localhost")
        bot._conversation_cache.append_live_event = AsyncMock()
        bot._conversation_resolver.coalescing_thread_id = AsyncMock(return_value=None)
        bot._turn_controller._precheck_dispatch_event = MagicMock(return_value=prechecked_event)
        bot._turn_controller._dispatch_special_media_as_text = AsyncMock(return_value=False)
        bot._turn_controller._enqueue_for_dispatch = AsyncMock()

        await bot._turn_controller._handle_media_message_inner(room, event)

        bot._conversation_cache.append_live_event.assert_awaited_once()
        append_args = bot._conversation_cache.append_live_event.await_args
        assert append_args.args == ("!test:localhost", event)
        assert append_args.kwargs["event_info"].is_edit is False
        bot._conversation_resolver.coalescing_thread_id.assert_awaited_once_with(room, event)
        bot._turn_controller._enqueue_for_dispatch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_audio_dispatch_appends_live_event_before_special_media_handling(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Audio dispatch should update live cache before special-media short-circuiting."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        room = MagicMock()
        room.room_id = "!test:localhost"
        event = self._make_handler_event("voice", sender="@user:localhost", event_id="$voice_event")
        prechecked_event = SimpleNamespace(event=event, requester_user_id="@user:localhost")
        bot._conversation_cache.append_live_event = AsyncMock()

        async def record_coalescing(_room: object, _event: object) -> None:
            assert bot._conversation_cache.append_live_event.await_count == 0

        bot._conversation_resolver.coalescing_thread_id = AsyncMock(side_effect=record_coalescing)
        bot._turn_controller._precheck_dispatch_event = MagicMock(return_value=prechecked_event)
        bot._turn_controller._dispatch_special_media_as_text = AsyncMock(return_value=True)
        bot._turn_controller._enqueue_for_dispatch = AsyncMock()

        await bot._turn_controller._handle_media_message_inner(room, event)

        bot._conversation_cache.append_live_event.assert_awaited_once()
        bot._conversation_resolver.coalescing_thread_id.assert_awaited_once_with(room, event)
        bot._turn_controller._enqueue_for_dispatch.assert_not_awaited()

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

        event = _room_image_event(sender="@user:localhost", event_id="$img_event_history", body="photo.png")
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
            await drain_coalescing(bot)

        mock_resolve_media.assert_called_once()
        assert mock_resolve_media.call_args.args[1] == [current_attachment_id, history_attachment_id]

        bot._generate_response.assert_awaited_once()
        generate_kwargs = bot._generate_response.await_args.kwargs
        assert generate_kwargs["attachment_ids"] == [current_attachment_id, history_attachment_id]
        assert current_attachment_id not in generate_kwargs["prompt"]
        assert history_attachment_id not in generate_kwargs["prompt"]
        assert generate_kwargs["model_prompt"] is not None
        assert current_attachment_id in generate_kwargs["model_prompt"]
        assert history_attachment_id in generate_kwargs["model_prompt"]
        tracker.record_handled_turn.assert_called_once_with(
            _agent_response_handled_turn(
                agent_name=mock_agent_user.agent_name,
                room_id=room.room_id,
                event_id="$img_event_history",
                response_event_id="$response",
                thread_id="$thread_root",
                requester_id="@user:localhost",
                correlation_id="$img_event_history",
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
        assert payload.prompt == "describe this"
        assert payload.model_prompt is not None
        assert "Available attachment IDs" in payload.model_prompt
        assert "att_image" in payload.model_prompt
        assert list(payload.media.images) == [stored_image, fallback_image]

    @pytest.mark.asyncio
    async def test_build_dispatch_payload_with_attachments_keeps_raw_prompt_clean(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Attachment IDs should be isolated to model_prompt instead of mutating the raw user prompt."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        stored_image = MagicMock(spec=Image)

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
                ),
            )

        assert payload.prompt == "describe this"
        assert payload.model_prompt is not None
        assert "Available attachment IDs" in payload.model_prompt
        assert "att_image" in payload.model_prompt

    @pytest.mark.asyncio
    async def test_message_enrichment_appends_to_existing_model_prompt(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Message enrichment should extend an existing model prompt rather than replacing it."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
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
            target=MessageTarget.resolve("!test:localhost", None, "$event"),
            correlation_id="corr-1",
            envelope=_hook_envelope(body="hello", source_event_id="$event"),
        )
        registry_stub = MagicMock()
        registry_stub.has_hooks.return_value = True
        bot._ingress_hook_runner.hook_context.hook_registry_state.registry = registry_stub

        with patch(
            "mindroom.turn_policy.emit_collect",
            new=AsyncMock(
                return_value=[EnrichmentItem(key="extra", text="hook enrichment", cache_policy="volatile")],
            ),
        ):
            prepared = await bot._ingress_hook_runner.apply_message_enrichment(
                dispatch,
                DispatchPayload(
                    prompt="hello",
                    model_prompt="Available attachment IDs: att_1. Use tool calls to inspect or process them.",
                    attachment_ids=["att_1"],
                ),
                target_entity_name=mock_agent_user.agent_name,
                target_member_names=None,
            )

        assert prepared.payload.prompt == "hello"
        assert prepared.payload.model_prompt is not None
        assert prepared.payload.model_prompt.startswith("Available attachment IDs: att_1")
        assert "hook enrichment" in prepared.payload.model_prompt

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

        event = _room_image_event(sender="@user:localhost", event_id="$img_event_fail", body="please analyze")
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
            await drain_coalescing(bot)

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

        event = _room_file_event(sender="@user:localhost", event_id="$file_event", body="report.pdf")
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
            await drain_coalescing(bot)

        bot._generate_response.assert_awaited_once()
        generate_kwargs = bot._generate_response.await_args.kwargs
        attachment_id = _attachment_id_for_event("$file_event")
        assert generate_kwargs["room_id"] == "!test:localhost"
        assert generate_kwargs["reply_to_event_id"] == "$file_event"
        assert generate_kwargs["thread_id"] is None
        assert generate_kwargs["thread_history"] == []
        assert generate_kwargs["user_id"] == "@user:localhost"
        assert generate_kwargs["attachment_ids"] == [attachment_id]
        assert "Available attachment IDs" not in generate_kwargs["prompt"]
        assert generate_kwargs["model_prompt"] is not None
        assert "Available attachment IDs" in generate_kwargs["model_prompt"]
        assert attachment_id in generate_kwargs["model_prompt"]
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
                requester_id="@user:localhost",
                correlation_id="$file_event",
                source_event_prompts={"$file_event": "[Attached file]"},
            ).with_response_context(
                response_owner=mock_agent_user.agent_name,
                history_scope=HistoryScope(kind="agent", scope_id=mock_agent_user.agent_name),
                conversation_target=MessageTarget.resolve(
                    room_id=room.room_id,
                    thread_id=None,
                    reply_to_event_id="$file_event",
                ).with_thread_root("$file_event"),
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

        event = _room_file_event(sender="@user:localhost", event_id="$file_event_fail", body="report.pdf")
        event.url = "mxc://localhost/report"
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
            await drain_coalescing(bot)

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
            patch("mindroom.turn_policy.thread_requires_explicit_agent_targeting", return_value=False),
            patch("mindroom.turn_policy.get_available_agents_for_sender_authoritative") as mock_get_available,
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
            patch("mindroom.dispatch_handoff.extract_media_caption", return_value="[Attached image]"),
        ):
            mock_get_available.return_value = [
                config.get_ids(runtime_paths_for(config))["general"],
                config.get_ids(runtime_paths_for(config))["calculator"],
            ]
            await bot._on_media_message(room, event)
            await drain_coalescing(bot)

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
    async def test_router_joined_room_startup_sends_welcome_after_join(
        self,
        tmp_path: Path,
    ) -> None:
        """Startup room joins should cache the room locally before sending a welcome."""
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        install_runtime_cache_support(bot)
        bot.rooms = ["!welcome:localhost"]
        bot.client = AsyncMock()
        bot.client.user_id = agent_user.user_id
        bot.client.rooms = {}
        bot.client.join = AsyncMock(return_value=nio.JoinResponse("!welcome:localhost"))
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

        bot._send_response = AsyncMock(side_effect=fake_send_response)
        with (
            patch("mindroom.bot_room_lifecycle._generate_welcome_message", return_value="Welcome"),
            patch("mindroom.bot_room_lifecycle.get_joined_rooms", new=AsyncMock(return_value=[])),
            patch("mindroom.bot.restore_scheduled_tasks", new=AsyncMock(return_value=0)),
            patch("mindroom.bot.config_confirmation.restore_pending_changes", new=AsyncMock(return_value=0)),
        ):
            await bot.join_configured_rooms()

        assert "!welcome:localhost" in bot.client.rooms
        bot.client.join.assert_awaited_once_with("!welcome:localhost")
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
            patch("mindroom.turn_policy.thread_requires_explicit_agent_targeting", return_value=False),
            patch("mindroom.turn_policy.get_available_agents_for_sender_authoritative") as mock_get_available,
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
            await drain_coalescing(bot)

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
            await drain_coalescing(router_bot, general_bot)

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
            patch("mindroom.turn_policy.thread_requires_explicit_agent_targeting", return_value=False),
            patch(
                "mindroom.turn_policy.get_available_agents_for_sender_authoritative",
                return_value=[
                    config.get_ids(runtime_paths_for(config))["calculator"],
                    config.get_ids(runtime_paths_for(config))["general"],
                ],
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
            patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch("mindroom.dispatch_handoff.extract_media_caption", return_value="[Attached image]"),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await bot._on_message(room, text_event)
            await drain_coalescing(bot)
            await bot._on_media_message(room, image_event)
            await drain_coalescing(bot)

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
            patch("mindroom.turn_policy.thread_requires_explicit_agent_targeting", return_value=False),
            patch(
                "mindroom.turn_policy.get_available_agents_for_sender_authoritative",
                return_value=[config.get_ids(runtime_paths_for(config))["calculator"]],
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
            patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch("mindroom.dispatch_handoff.extract_media_caption", return_value="[Attached image]"),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await bot._on_message(room, text_event)
            await drain_coalescing(bot)
            await bot._on_media_message(room, image_event)
            await drain_coalescing(bot)

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
            patch("mindroom.turn_policy.get_available_agents_for_sender_authoritative", return_value=[]),
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
            await drain_coalescing(bot)

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
    async def test_resolve_response_action_keeps_gate_owned_active_thread_follow_up(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Gate-owned active follow-ups should keep active-response treatment after the active turn ends."""
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
            thread_history=[],
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
            source_kind="message",
            dispatch_policy_source_kind=COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP,
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
                return_value=False,
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
        mock_has_active_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_uses_active_follow_up_policy_without_erasing_voice(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Gate-owned voice follow-ups should keep voice source kind and active-response policy."""
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
            thread_history=[],
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
            source_kind="voice",
            dispatch_policy_source_kind=COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP,
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
                return_value=False,
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
        assert envelope.source_kind == "voice"
        mock_should_respond.assert_called_once()
        mock_has_active_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_requires_explicit_mention_in_multi_human_thread_even_after_prior_team_mentions(
        self,
        tmp_path: Path,
    ) -> None:
        """Untargeted follow-ups in a multi-human thread must not reuse stale thread mentions to form a team."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "synth": AgentConfig(
                        display_name="Synthesis",
                        rooms=["!room:localhost"],
                    ),
                    "reasoner": AgentConfig(
                        display_name="Reasoner",
                        rooms=["!room:localhost"],
                    ),
                    "critic": AgentConfig(
                        display_name="Critic",
                        rooms=["!room:localhost"],
                    ),
                },
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        ids = config.get_ids(runtime_paths)
        bot_user = AgentMatrixUser(
            agent_name="synth",
            user_id=ids["synth"].full_id,
            display_name="Synthesis",
            password=TEST_PASSWORD,
        )
        bot = AgentBot(bot_user, tmp_path, config=config, runtime_paths=runtime_paths)
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.users = {
            ids["synth"].full_id: MagicMock(),
            ids["reasoner"].full_id: MagicMock(),
            ids["critic"].full_id: MagicMock(),
            "@bas:localhost": MagicMock(),
            "@maciej:localhost": MagicMock(),
        }
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=True,
            thread_id="$thread",
            thread_history=[
                ResolvedVisibleMessage(
                    sender="@bas:localhost",
                    body="Team, please assess this",
                    timestamp=1,
                    event_id="$m1",
                    content={
                        "body": "Team, please assess this",
                        "m.mentions": {
                            "user_ids": [
                                ids["synth"].full_id,
                                ids["reasoner"].full_id,
                                ids["critic"].full_id,
                            ],
                        },
                    },
                    thread_id="$thread",
                    latest_event_id="$m1",
                ),
                ResolvedVisibleMessage(
                    sender="@maciej:localhost",
                    body="I fixed two issues",
                    timestamp=2,
                    event_id="$m2",
                    content={"body": "I fixed two issues"},
                    thread_id="$thread",
                    latest_event_id="$m2",
                ),
            ],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )

        with (
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new=AsyncMock(side_effect=AssertionError("team formation should be skipped")),
            ) as mock_decide_team_formation,
            patch("mindroom.turn_policy.should_agent_respond", return_value=False) as mock_should_respond,
        ):
            action = await bot._turn_policy.resolve_response_action(
                context,
                room,
                "@bas:localhost",
                "I fixed two issues",
                False,
            )

        assert action.kind == "skip"
        mock_decide_team_formation.assert_not_awaited()
        mock_should_respond.assert_called_once()

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

        bot.client = AsyncMock(spec=nio.AsyncClient)

        async def unused_payload_builder(_context: MessageContext) -> DispatchPayload:
            return DispatchPayload(prompt="help me")

        with patch.object(DeliveryGateway, "send_text", new=AsyncMock(return_value="$reply")) as send_text:
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

        send_text.assert_awaited_once()
        delivered_request = send_text.await_args.args[0]
        assert delivered_request.response_text.endswith(
            "private agents cannot participate in teams yet",
        )
        tracker.record_handled_turn.assert_called_once_with(
            HandledTurnState.from_source_event_id(
                "$event",
                response_event_id="$reply",
            ),
        )

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_does_not_mark_reject_handled_when_rejection_send_fails(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Reject actions must not mark the source handled when no rejection reply was delivered."""
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
            rejection_message="Rejected request",
        )
        bot.client = AsyncMock(spec=nio.AsyncClient)

        async def unused_payload_builder(_context: MessageContext) -> DispatchPayload:
            return DispatchPayload(prompt="help me")

        with patch("mindroom.delivery_gateway.send_message_result", new=AsyncMock(return_value=None)):
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

        tracker.record_handled_turn.assert_called_once_with(
            HandledTurnState.from_source_event_id(event.event_id),
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
            patch.object(bot._conversation_cache, "get_dispatch_thread_snapshot", new=mock_snapshot),
            patch.object(bot._conversation_cache, "get_thread_history", new=mock_history),
        ):
            context = await bot._conversation_resolver.extract_dispatch_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root"
        assert [message.event_id for message in context.thread_history] == ["$thread_root"]
        assert context.requires_full_thread_history is True
        mock_snapshot.assert_awaited_once_with(
            room.room_id,
            "$thread_root",
            caller_label="dispatch_context",
        )
        mock_history.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_extract_dispatch_context_marks_direct_thread_snapshot_for_later_full_hydration(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Direct-thread preview should stay lightweight and request later full hydration."""
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
        snapshot_history = ThreadHistoryResult(
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
            is_full_history=False,
        )

        with patch(
            "mindroom.matrix.conversation_cache.fetch_dispatch_thread_snapshot",
            new=AsyncMock(return_value=snapshot_history),
        ) as mock_snapshot:
            context = await bot._conversation_resolver.extract_dispatch_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root"
        assert context.thread_history == snapshot_history
        assert context.requires_full_thread_history is True
        trusted_sender_ids = frozenset(
            matrix_id.full_id for matrix_id in config.get_ids(runtime_paths_for(config)).values()
        )
        mock_snapshot.assert_awaited_once_with(
            bot.client,
            room.room_id,
            "$thread_root",
            event_cache=bot.event_cache,
            cache_write_guard_started_at=ANY,
            trusted_sender_ids=trusted_sender_ids,
            caller_label="dispatch_context",
            coordinator_queue_wait_ms=ANY,
        )

    @pytest.mark.asyncio
    async def test_dispatch_text_message_prepares_full_history_payload_after_lock_when_required(  # noqa: PLR0915
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Planning and payload preparation should both see full history when snapshots are incomplete."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
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
        full_history = [
            *snapshot_history,
            ResolvedVisibleMessage.synthetic(
                sender="@user:localhost",
                body="[Attached file]",
                event_id="$older-attachment",
                timestamp=2,
                content={
                    "body": "[Attached file]",
                    "com.mindroom.attachment_ids": ["att_older"],
                },
            ),
        ]
        call_order: list[str] = []

        async def fake_hydrate_dispatch_context(
            _room: object,
            _event: object,
            context: MessageContext,
            **_kwargs: object,
        ) -> None:
            call_order.append("hydrate")
            context.thread_history = ThreadHistoryResult(full_history, is_full_history=True)
            context.requires_full_thread_history = False

        async def fake_plan(*_args: object, **_kwargs: object) -> DispatchPlan:
            call_order.append("action")
            assert list(dispatch.context.thread_history) == full_history
            return DispatchPlan(
                kind="respond",
                response_action=ResponseAction(kind="individual"),
            )

        async def fake_build_payload(context: MessageContext) -> DispatchPayload:
            call_order.append("payload")
            assert list(context.thread_history) == full_history
            return DispatchPayload(prompt="hello", attachment_ids=["att_older"])

        async def refresh_thread_history(request: ResponseRequest) -> ResponseRequest:
            return replace(
                request,
                thread_history=ThreadHistoryResult(full_history, is_full_history=True),
            )

        async def run_cancellable_response(**kwargs: object) -> str | None:
            call_order.append("generate")
            response_function = kwargs["response_function"]
            assert callable(response_function)
            await cast("Any", response_function)(None)
            return None

        def prepare_memory_and_model_context(
            prompt: str,
            thread_history: Sequence[ResolvedVisibleMessage],
            *,
            config: Config,
            runtime_paths: RuntimePaths,
            model_prompt: str | None = None,
        ) -> tuple[str, Sequence[ResolvedVisibleMessage], str | None, Sequence[ResolvedVisibleMessage]]:
            del config, runtime_paths
            return prompt, thread_history, model_prompt, thread_history

        with (
            patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
            patch.object(bot._turn_policy, "plan_turn", new=AsyncMock(side_effect=fake_plan)),
            patch.object(
                bot._conversation_resolver,
                "hydrate_dispatch_context",
                new=AsyncMock(side_effect=fake_hydrate_dispatch_context),
            ) as mock_hydrate_dispatch_context,
            patch.object(
                ResponseRunner,
                "_refresh_thread_history_after_lock",
                new=AsyncMock(side_effect=refresh_thread_history),
            ) as mock_refresh_thread_history,
            patch.object(
                bot._inbound_turn_normalizer,
                "build_dispatch_payload_with_attachments",
                new=AsyncMock(side_effect=fake_build_payload),
            ) as mock_build_payload,
            patch.object(
                ResponseRunner,
                "process_and_respond",
                new=AsyncMock(
                    return_value=FinalDeliveryOutcome(
                        terminal_status="completed",
                        event_id="$response",
                        is_visible_response=True,
                        final_visible_body="ok",
                    ),
                ),
            ) as mock_process,
            patch.object(
                ResponseRunner,
                "run_cancellable_response",
                new=AsyncMock(side_effect=run_cancellable_response),
            ),
            patch.object(bot._turn_controller, "_log_dispatch_latency"),
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=False),
                prepare_memory_and_model_context=prepare_memory_and_model_context,
                reprioritize_auto_flush_sessions=MagicMock(),
                apply_post_response_effects=AsyncMock(),
            ),
        ):
            await bot._turn_controller._dispatch_text_message(
                room,
                _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
            )

        mock_hydrate_dispatch_context.assert_awaited_once()
        hydrate_call = mock_hydrate_dispatch_context.await_args
        assert hydrate_call is not None
        assert hydrate_call.args[0] is room
        assert hydrate_call.args[2] is dispatch.context
        mock_refresh_thread_history.assert_awaited_once()
        mock_build_payload.assert_awaited_once()
        process_request = mock_process.await_args.args[0]
        assert list(process_request.thread_history) == full_history
        assert process_request.attachment_ids == ("att_older",)
        assert call_order == ["hydrate", "action", "payload", "generate"]
        assert dispatch.context.thread_history == full_history
        assert dispatch.context.requires_full_thread_history is False

    @pytest.mark.asyncio
    async def test_dispatch_text_message_skip_path_hydrates_full_history_before_planning(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Planning should see full history before deciding to ignore a turn."""
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
        full_history = [
            ResolvedVisibleMessage.synthetic(
                sender="@user:localhost",
                body="Hydrated root",
                event_id="$thread_root",
                timestamp=1,
                content={"body": "Hydrated root"},
            ),
        ]

        async def fake_hydrate_dispatch_context(
            _room: object,
            _event: object,
            context: MessageContext,
            **_kwargs: object,
        ) -> None:
            context.thread_history = ThreadHistoryResult(full_history, is_full_history=True)
            context.requires_full_thread_history = False

        async def fake_plan(
            *_args: object,
            **_kwargs: object,
        ) -> DispatchPlan:
            assert list(dispatch.context.thread_history) == full_history
            return DispatchPlan(kind="ignore")

        with (
            patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
            patch.object(
                bot._conversation_resolver,
                "hydrate_dispatch_context",
                new=AsyncMock(side_effect=fake_hydrate_dispatch_context),
            ) as mock_hydrate_dispatch_context,
            patch.object(
                bot._turn_policy,
                "plan_turn",
                new=AsyncMock(side_effect=fake_plan),
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

        mock_hydrate_dispatch_context.assert_awaited_once()
        hydrate_call = mock_hydrate_dispatch_context.await_args
        assert hydrate_call is not None
        assert hydrate_call.args[0] is room
        assert hydrate_call.args[2] is dispatch.context
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
        tracker.visible_echo_event_id_for_sources.side_effect = lambda source_event_ids: (
            "$voice_echo" if tuple(source_event_ids) == ("$voice", "$text") else None
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
                    requester_id="@user:localhost",
                    correlation_id="corr-visible-echo",
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
                    requester_id="@user:localhost",
                    correlation_id="corr-router-coalesced",
                    history_scope=None,
                    conversation_target=dispatch.target,
                ),
            ),
        ]

    @pytest.mark.asyncio
    async def test_trusted_internal_router_relays_use_gate_bypass(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Agent-authored relays should enter the gate as FIFO bypass events."""
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
            patch.object(bot._coalescing_gate, "enqueue", new=AsyncMock()) as mock_start,
        ):
            await bot._turn_controller._enqueue_for_dispatch(event, room, source_kind="message")

        mock_dispatch.assert_not_awaited()
        mock_start.assert_awaited_once()
        key, pending_event = mock_start.await_args.args
        assert key == (room.room_id, None, "@user:localhost")
        assert isinstance(pending_event, PendingEvent)
        assert pending_event.event is event
        assert pending_event.source_kind == COALESCING_BYPASS_TRUSTED_INTERNAL_RELAY

    @pytest.mark.asyncio
    async def test_handle_message_inner_enqueues_active_thread_follow_up_as_gate_bypass(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Human follow-ups in an actively responding thread must stay owned by the gate."""
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
            patch.object(
                bot._response_runner,
                "reserve_waiting_human_message",
                return_value=MagicMock(),
            ) as mock_reserve_waiting_human_message,
            patch.object(
                bot._turn_controller,
                "_dispatch_text_message",
                new=AsyncMock(),
            ) as mock_dispatch,
            patch.object(bot._coalescing_gate, "enqueue", new=AsyncMock()) as mock_start,
        ):
            await asyncio.wait_for(bot._on_message(room, event), timeout=0.05)

        mock_has_active_response.assert_called_once()
        active_target = mock_has_active_response.call_args.args[0]
        assert active_target.resolved_thread_id == target.resolved_thread_id
        mock_reserve_waiting_human_message.assert_called_once()
        signal_target = mock_reserve_waiting_human_message.call_args.kwargs["target"]
        assert signal_target.resolved_thread_id == target.resolved_thread_id
        assert mock_reserve_waiting_human_message.call_args.kwargs["response_envelope"] is envelope
        mock_dispatch.assert_not_awaited()
        mock_start.assert_awaited_once()
        key, pending_event = mock_start.await_args.args
        assert key == (room.room_id, "$thread_root", "@user:localhost")
        assert isinstance(pending_event, PendingEvent)
        assert pending_event.event is event
        assert pending_event.source_kind == "message"
        assert pending_event.dispatch_policy_source_kind == COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP
        assert len(pending_event.dispatch_metadata) == 1
        metadata = pending_event.dispatch_metadata[0]
        assert metadata.kind == "queued_notice_reservation"
        assert metadata.payload is mock_reserve_waiting_human_message.return_value
        assert metadata.requires_solo_batch is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source_kind", ["hook", "hook_dispatch"])
    async def test_handle_message_inner_enqueues_trusted_hook_source_kind_as_gate_bypass(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        source_kind: str,
    ) -> None:
        """Trusted hook messages should keep their bypass source kind on the real text path."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        bot.client = _make_matrix_client_mock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": f"${source_kind}",
                "sender": "@mindroom_general:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "body": f"@mindroom_calculator:localhost {source_kind} says hello",
                    "com.mindroom.source_kind": source_kind,
                    ORIGINAL_SENDER_KEY: "@user:localhost",
                },
            },
        )
        prepared_event = PreparedTextEvent(
            sender="@mindroom_general:localhost",
            event_id=f"${source_kind}",
            body=f"@mindroom_calculator:localhost {source_kind} says hello",
            source=event.source,
            server_timestamp=1234567890,
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
            patch.object(bot._turn_controller, "_should_skip_deep_synthetic_full_dispatch", return_value=False),
            patch("mindroom.turn_controller.should_handle_interactive_text_response", return_value=False),
            patch.object(
                bot._conversation_resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value=None),
            ),
            patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock()) as mock_dispatch,
            patch.object(bot._coalescing_gate, "enqueue", new=AsyncMock()) as mock_start,
        ):
            await asyncio.wait_for(bot._on_message(room, event), timeout=0.05)

        mock_dispatch.assert_not_awaited()
        mock_start.assert_awaited_once()
        _key, pending_event = mock_start.await_args.args
        assert isinstance(pending_event, PendingEvent)
        assert pending_event.event is event
        assert pending_event.source_kind == source_kind

    @pytest.mark.asyncio
    async def test_voice_preview_reserves_active_thread_follow_up(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Transcribed voice follow-ups should share the active-response notice path."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        bot.client = _make_matrix_client_mock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        voice_event = _room_audio_event(sender="@user:localhost", event_id="$voice-followup", room_id=room.room_id)
        prepared_event = PreparedTextEvent(
            sender="@user:localhost",
            event_id="$voice-followup",
            body="please stop",
            source={"content": {"msgtype": "m.text", "body": "please stop", "com.mindroom.source_kind": "voice"}},
            server_timestamp=1234567890,
            is_synthetic=True,
            source_kind_override="voice",
        )

        with (
            patch(
                "mindroom.inbound_turn_normalizer.InboundTurnNormalizer.prepare_voice_event",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        event=prepared_event,
                        effective_thread_id="$thread_root",
                    ),
                ),
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()) as mock_echo,
            patch.object(
                bot._response_runner,
                "has_active_response_for_target",
                return_value=True,
            ),
            patch.object(
                bot._response_runner,
                "reserve_waiting_human_message",
                return_value=MagicMock(),
            ) as mock_reserve_waiting_human_message,
            patch.object(bot._coalescing_gate, "enqueue", new=AsyncMock()) as mock_start,
        ):
            await bot._turn_controller._on_audio_media_message(
                room,
                _PrecheckedEvent(event=voice_event, requester_user_id="@user:localhost"),
            )

        mock_echo.assert_awaited_once()
        mock_reserve_waiting_human_message.assert_called_once()
        reserved_envelope = mock_reserve_waiting_human_message.call_args.kwargs["response_envelope"]
        assert reserved_envelope.source_kind == "voice"
        mock_start.assert_awaited_once()
        key, pending_event = mock_start.await_args.args
        assert key == (room.room_id, "$thread_root", "@user:localhost")
        assert isinstance(pending_event, PendingEvent)
        assert pending_event.event is prepared_event
        assert pending_event.source_kind == "voice"
        assert pending_event.dispatch_policy_source_kind == COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP
        assert len(pending_event.dispatch_metadata) == 1
        metadata = pending_event.dispatch_metadata[0]
        assert metadata.kind == "queued_notice_reservation"
        assert metadata.payload is mock_reserve_waiting_human_message.return_value
        assert metadata.requires_solo_batch is True

    @pytest.mark.asyncio
    async def test_file_sidecar_text_preview_enqueues_prepared_text(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """File sidecar previews should hand prepared text to the gate, not dispatch inline."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        bot.client = _make_matrix_client_mock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        sidecar_event = cast(
            "nio.RoomMessageFile",
            nio.Event.parse_event(
                {
                    "event_id": "$sidecar",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567890,
                    "room_id": room.room_id,
                    "type": "m.room.message",
                    "content": {
                        "msgtype": "m.file",
                        "body": "long-text.txt",
                        "info": {"mimetype": "application/json"},
                        "io.mindroom.long_text": {
                            "version": 2,
                            "encoding": "matrix_event_content_json",
                        },
                        "url": "mxc://localhost/sidecar",
                    },
                },
            ),
        )
        prepared_event = PreparedTextEvent(
            sender="@user:localhost",
            event_id="$sidecar",
            body="full long text",
            source={"content": {"msgtype": "m.text", "body": "full long text"}},
            server_timestamp=1234567890,
        )
        target = MessageTarget.resolve(room.room_id, "$thread_root", sidecar_event.event_id)
        envelope = MessageEnvelope(
            source_event_id=sidecar_event.event_id,
            room_id=room.room_id,
            target=target,
            requester_id="@user:localhost",
            sender_id="@user:localhost",
            body="full long text",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=bot.agent_name,
            source_kind="message",
        )

        with (
            patch(
                "mindroom.inbound_turn_normalizer.InboundTurnNormalizer.prepare_file_sidecar_text_event",
                new=AsyncMock(return_value=prepared_event),
            ),
            patch.object(bot._conversation_resolver, "build_ingress_envelope", return_value=envelope),
            patch.object(bot._turn_controller, "_should_skip_deep_synthetic_full_dispatch", return_value=False),
            patch("mindroom.turn_controller.should_handle_interactive_text_response", return_value=False),
            patch.object(
                bot._conversation_resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$thread_root"),
            ),
            patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock()) as mock_dispatch,
            patch.object(bot._coalescing_gate, "enqueue", new=AsyncMock()) as mock_start,
        ):
            handled = await bot._turn_controller._dispatch_file_sidecar_text_preview(
                room,
                _PrecheckedEvent(event=sidecar_event, requester_user_id="@user:localhost"),
            )

        assert handled is True
        mock_dispatch.assert_not_awaited()
        mock_start.assert_awaited_once()
        key, pending_event = mock_start.await_args.args
        assert key == (room.room_id, "$thread_root", "@user:localhost")
        assert isinstance(pending_event, PendingEvent)
        assert pending_event.event is prepared_event
        assert pending_event.source_kind == "message"

    @pytest.mark.asyncio
    async def test_file_sidecar_text_preview_reserves_active_thread_follow_up(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Sidecar text follow-ups should share the active-response notice path."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        bot.client = _make_matrix_client_mock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        sidecar_event = cast(
            "nio.RoomMessageFile",
            nio.Event.parse_event(
                {
                    "event_id": "$sidecar-followup",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567890,
                    "room_id": room.room_id,
                    "type": "m.room.message",
                    "content": {
                        "msgtype": "m.file",
                        "body": "long-text.txt",
                        "info": {"mimetype": "application/json"},
                        "io.mindroom.long_text": {
                            "version": 2,
                            "encoding": "matrix_event_content_json",
                        },
                        "url": "mxc://localhost/sidecar-followup",
                    },
                },
            ),
        )
        prepared_event = PreparedTextEvent(
            sender="@user:localhost",
            event_id="$sidecar-followup",
            body="please stop",
            source={"content": {"msgtype": "m.text", "body": "please stop"}},
            server_timestamp=1234567890,
        )
        target = MessageTarget.resolve(room.room_id, "$thread_root", sidecar_event.event_id)
        envelope = MessageEnvelope(
            source_event_id=sidecar_event.event_id,
            room_id=room.room_id,
            target=target,
            requester_id="@user:localhost",
            sender_id="@user:localhost",
            body="please stop",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=bot.agent_name,
            source_kind="message",
        )

        with (
            patch(
                "mindroom.inbound_turn_normalizer.InboundTurnNormalizer.prepare_file_sidecar_text_event",
                new=AsyncMock(return_value=prepared_event),
            ),
            patch.object(bot._conversation_resolver, "build_ingress_envelope", return_value=envelope),
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
            ),
            patch.object(
                bot._response_runner,
                "reserve_waiting_human_message",
                return_value=MagicMock(),
            ) as mock_reserve_waiting_human_message,
            patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock()) as mock_dispatch,
            patch.object(bot._coalescing_gate, "enqueue", new=AsyncMock()) as mock_start,
        ):
            handled = await bot._turn_controller._dispatch_file_sidecar_text_preview(
                room,
                _PrecheckedEvent(event=sidecar_event, requester_user_id="@user:localhost"),
            )

        assert handled is True
        mock_dispatch.assert_not_awaited()
        mock_reserve_waiting_human_message.assert_called_once()
        mock_start.assert_awaited_once()
        key, pending_event = mock_start.await_args.args
        assert key == (room.room_id, "$thread_root", "@user:localhost")
        assert isinstance(pending_event, PendingEvent)
        assert pending_event.event is prepared_event
        assert pending_event.source_kind == "message"
        assert pending_event.dispatch_policy_source_kind == COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP
        assert len(pending_event.dispatch_metadata) == 1
        metadata = pending_event.dispatch_metadata[0]
        assert metadata.kind == "queued_notice_reservation"
        assert metadata.payload is mock_reserve_waiting_human_message.return_value
        assert metadata.requires_solo_batch is True

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
        mock_generate_team_response = AsyncMock(
            return_value="$team-response",
        )
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
        event = _room_image_event(sender="@user:localhost", event_id="$img_event_fail", body="photo.jpg")
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
        bot._delivery_gateway.send_text = AsyncMock(return_value="$error")
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
            await drain_coalescing(bot)

        bot._generate_response.assert_not_called()
        bot._edit_message.assert_not_awaited()
        bot._delivery_gateway.send_text.assert_awaited_once()
        assert bot._delivery_gateway.send_text.await_args.args[0].response_text == (
            "[calculator] ⚠️ Error: Failed to download image"
        )
        expected_handled_turn = _agent_response_handled_turn(
            agent_name=mock_agent_user.agent_name,
            room_id=room.room_id,
            event_id="$img_event_fail",
            response_event_id="$error",
            requester_id="@user:localhost",
            correlation_id="$img_event_fail",
            source_event_prompts={"$img_event_fail": "[Attached image]"},
        )
        expected_handled_turn = replace(
            expected_handled_turn,
            response_event_id="$error",
            conversation_target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id=None,
                reply_to_event_id="$img_event_fail",
            ).with_thread_root("$img_event_fail"),
        )
        tracker.record_handled_turn.assert_called_once_with(
            expected_handled_turn,
        )

    @pytest.mark.asyncio
    async def test_finalize_dispatch_failure_sends_terminal_error_message(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Dispatch setup failures should go through the terminal delivery gateway."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        bot._delivery_gateway.send_text = AsyncMock(return_value="$error")
        _replace_turn_policy_deps(bot, delivery_gateway=bot._delivery_gateway)

        resolution = await bot._turn_controller._finalize_dispatch_failure(
            room_id="!test:localhost",
            reply_to_event_id="$event",
            thread_id="$thread_root",
            error=RuntimeError("boom"),
        )

        assert resolution == "$error"
        bot._delivery_gateway.send_text.assert_awaited_once_with(
            SendTextRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread_root", "$event"),
                response_text="[calculator] ⚠️ Error: boom",
                extra_content={STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED},
            ),
        )

    @pytest.mark.asyncio
    async def test_finalize_dispatch_failure_uses_system_response_kind_for_team_bot(
        self,
        tmp_path: Path,
    ) -> None:
        """Dispatch setup failures are system replies even when they occur on a team bot."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(display_name="GeneralAgent", rooms=["!test:localhost"]),
                },
                teams={
                    "team_bot": TeamConfig(
                        display_name="Team Bot",
                        role="Coordinate work",
                        agents=["general"],
                        rooms=["!test:localhost"],
                    ),
                },
                models={"default": ModelConfig(provider="test", id="test-model")},
                authorization=AuthorizationConfig(default_room_access=True),
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        team_user = AgentMatrixUser(
            agent_name="team_bot",
            user_id="@mindroom_team_bot:localhost",
            display_name="Team Bot",
            password=TEST_PASSWORD,
        )
        team_member = config.get_ids(runtime_paths)["general"]
        bot = TeamBot(
            team_user,
            tmp_path,
            config=config,
            runtime_paths=runtime_paths,
            team_agents=[team_member],
            team_mode="coordinate",
        )
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        bot._delivery_gateway.send_text = AsyncMock(return_value="$team-error")
        _replace_turn_policy_deps(bot, delivery_gateway=bot._delivery_gateway)

        await bot._turn_controller._finalize_dispatch_failure(
            room_id="!test:localhost",
            reply_to_event_id="$event",
            thread_id="$thread_root",
            error=RuntimeError("boom"),
        )

        assert bot._delivery_gateway.send_text.await_args.args == (
            SendTextRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread_root", "$event"),
                response_text="[team_bot] ⚠️ Error: boom",
                extra_content={STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED},
            ),
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

        mock_edit = AsyncMock(return_value=False)
        install_edit_message_mock(bot, mock_edit)
        bot._delivery_gateway.send_text = AsyncMock(return_value="$error")
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
        bot._delivery_gateway.send_text.assert_awaited_once()
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

        with patch(
            "mindroom.bot.TurnController._finalize_dispatch_failure",
            new=AsyncMock(
                return_value=None,
            ),
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
    async def test_execute_dispatch_action_handles_post_lock_request_preparation_error_without_unboundlocalerror(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Post-lock request preparation failures should degrade to a visible terminal error cleanly."""
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
            correlation_id="corr-post-lock-failure",
            envelope=_hook_envelope(body="hello", source_event_id="$event"),
        )

        async def payload_builder(_context: MessageContext) -> DispatchPayload:
            return DispatchPayload(prompt="hello")

        async def fail_generate_response(*_args: object, **_kwargs: object) -> FinalDeliveryOutcome:
            message = "post-lock setup failed"
            error = RuntimeError(message)
            raise PostLockRequestPreparationError(message) from error

        replace_turn_controller_deps(
            bot,
            response_runner=SimpleNamespace(
                generate_response=AsyncMock(side_effect=fail_generate_response),
                generate_team_response_helper=AsyncMock(),
            ),
        )

        with patch(
            "mindroom.bot.TurnController._finalize_dispatch_failure",
            new=AsyncMock(
                return_value="$error",
            ),
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

        tracker.record_handled_turn.assert_called_once_with(
            HandledTurnState.from_source_event_id(
                "$event",
                response_event_id="$error",
            ),
        )

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_records_visible_linkage_when_suppressed_cleanup_fails(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Suppressed placeholder cleanup failures should still persist visible linkage."""
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
                new=AsyncMock(
                    return_value="$thinking",
                ),
            ),
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

        tracker.record_handled_turn.assert_called_once_with(
            HandledTurnState.from_source_event_id(
                "$event",
                response_event_id="$thinking",
            ),
        )

    @pytest.mark.asyncio
    async def test_deliver_final_suppression_preserves_existing_visible_response_linkage(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Suppressing a reused visible response must keep that prior event visible without remarking success."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        gateway = replace_delivery_gateway_deps(
            bot,
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(
                    return_value=SimpleNamespace(
                        response_text="ignored",
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

        outcome = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                existing_event_id="$existing",
                existing_event_is_placeholder=False,
                response_text="Updated answer",
                response_kind="ai",
                response_envelope=response_envelope,
                correlation_id="corr-deliver-suppress-existing",
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert outcome.terminal_status == "cancelled"
        assert outcome.suppressed is True
        assert _visible_response_event_id(outcome) == "$existing"
        assert _handled_response_event_id(outcome) is None
        assert outcome.mark_handled is False
        gateway.deps.response_hooks.emit_cancelled_response.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deliver_final_failed_existing_visible_edit_preserves_prior_response(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Failed edits of an existing visible response must keep the prior event visible but retryable."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        gateway = replace_delivery_gateway_deps(
            bot,
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(
                    return_value=SimpleNamespace(
                        response_text="Updated answer",
                        response_kind="ai",
                        tool_trace=None,
                        extra_content=None,
                        envelope=response_envelope,
                        suppress=False,
                    ),
                ),
                emit_after_response=AsyncMock(),
                emit_cancelled_response=AsyncMock(),
            ),
        )
        with patch("mindroom.delivery_gateway.edit_message_result", new=AsyncMock(return_value=None)):
            outcome = await gateway.deliver_final(
                FinalDeliveryRequest(
                    target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                    existing_event_id="$existing",
                    existing_event_is_placeholder=False,
                    response_text="Updated answer",
                    response_kind="ai",
                    response_envelope=response_envelope,
                    correlation_id="corr-deliver-existing-failure",
                    tool_trace=None,
                    extra_content=None,
                ),
            )

        assert outcome.terminal_status == "error"
        assert _visible_response_event_id(outcome) == "$existing"
        assert _handled_response_event_id(outcome) == "$existing"
        assert outcome.mark_handled is True
        gateway.deps.response_hooks.emit_cancelled_response.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deliver_final_before_response_exception_cleans_placeholder(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A before-response crash must clean up a visible placeholder instead of leaving it behind."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        gateway = replace_delivery_gateway_deps(
            bot,
            redact_message_event=AsyncMock(return_value=True),
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(side_effect=RuntimeError("hook boom")),
                emit_after_response=AsyncMock(),
                emit_cancelled_response=AsyncMock(),
            ),
        )

        outcome = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                existing_event_id="$thinking",
                existing_event_is_placeholder=True,
                response_text="Updated answer",
                response_kind="ai",
                response_envelope=response_envelope,
                correlation_id="corr-deliver-before-hook-crash",
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert outcome.terminal_status == "error"
        gateway.deps.redact_message_event.assert_awaited_once_with(
            room_id="!test:localhost",
            event_id="$thinking",
            reason="Failed placeholder response before delivery",
        )
        gateway.deps.response_hooks.emit_cancelled_response.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deliver_final_before_response_cancellation_cleans_placeholder(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A cancelled before-response hook must redact the placeholder and propagate cancellation."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        gateway = replace_delivery_gateway_deps(
            bot,
            redact_message_event=AsyncMock(return_value=True),
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(side_effect=asyncio.CancelledError("hook cancelled")),
                emit_after_response=AsyncMock(),
                emit_cancelled_response=AsyncMock(),
            ),
        )

        with pytest.raises(asyncio.CancelledError, match="hook cancelled"):
            await gateway.deliver_final(
                FinalDeliveryRequest(
                    target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                    existing_event_id="$thinking",
                    existing_event_is_placeholder=True,
                    response_text="Updated answer",
                    response_kind="ai",
                    response_envelope=response_envelope,
                    correlation_id="corr-deliver-before-hook-cancel",
                    tool_trace=None,
                    extra_content=None,
                ),
            )

        gateway.deps.redact_message_event.assert_awaited_once_with(
            room_id="!test:localhost",
            event_id="$thinking",
            reason="Cancelled placeholder response",
        )
        gateway.deps.response_hooks.emit_cancelled_response.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_does_not_mark_responded_when_generation_returns_no_final_event(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Retryable resolutions with no response identity must keep the source retryable."""
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
            patch.object(
                bot._response_runner,
                "generate_response",
                new=AsyncMock(
                    return_value=None,
                ),
            ),
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
    async def test_execute_dispatch_action_logs_latency_after_locked_payload_preparation(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Latency logging should happen after the locked payload preparation path completes."""
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
                thread_history=ThreadHistoryResult([], is_full_history=True),
                mentioned_agents=[],
                has_non_agent_mentions=False,
                requires_full_thread_history=False,
            ),
            target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id=None,
                reply_to_event_id=event.event_id,
            ),
            correlation_id="corr-latency-order",
            envelope=_hook_envelope(body="hello", source_event_id="$event"),
        )

        mock_generate_response = AsyncMock(return_value="$response")
        install_generate_response_mock(bot, mock_generate_response)
        _replace_turn_policy_deps(
            bot,
            logger=bot.logger,
            response_runner=bot._response_runner,
        )

        payload_built = False

        async def payload_builder(_context: MessageContext) -> DispatchPayload:
            nonlocal payload_built
            payload_built = True
            return DispatchPayload(prompt="help me")

        original_log_dispatch_latency = bot._turn_controller._log_dispatch_latency

        def assert_payload_already_built(**kwargs: object) -> None:
            assert payload_built is True
            original_log_dispatch_latency(**kwargs)

        with patch.object(
            bot._turn_controller,
            "_log_dispatch_latency",
            side_effect=assert_payload_already_built,
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

        assert payload_built is True

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_emits_payload_builder_timing(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Dispatch execution should time the payload builder inside the locked preparation path."""
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
                is_thread=True,
                thread_id="$thread",
                thread_history=ThreadHistoryResult([], is_full_history=True),
                mentioned_agents=[],
                has_non_agent_mentions=False,
                requires_full_thread_history=False,
            ),
            target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id="$thread",
                reply_to_event_id=event.event_id,
            ),
            correlation_id="corr-payload-builder-timing",
            envelope=_hook_envelope(body="hello", source_event_id="$event"),
        )

        mock_generate_response = AsyncMock(return_value="$response")
        install_generate_response_mock(bot, mock_generate_response)
        _replace_turn_policy_deps(
            bot,
            logger=bot.logger,
            response_runner=bot._response_runner,
        )

        async def payload_builder(_context: MessageContext) -> DispatchPayload:
            return DispatchPayload(prompt="help me")

        with patch("mindroom.turn_controller.emit_elapsed_timing") as mock_emit:
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

        builder_calls = [
            call for call in mock_emit.call_args_list if call.args and call.args[0] == "response_payload.builder"
        ]
        assert len(builder_calls) == 1
        assert isinstance(builder_calls[0].args[1], float)
        assert builder_calls[0].kwargs == {
            "room_id": "!room:localhost",
            "thread_id": "$thread",
            "outcome": "success",
        }

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_emits_payload_builder_timing_on_failure(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Payload-builder timing should still emit when locked payload preparation fails."""
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
                is_thread=True,
                thread_id="$thread",
                thread_history=ThreadHistoryResult([], is_full_history=True),
                mentioned_agents=[],
                has_non_agent_mentions=False,
                requires_full_thread_history=False,
            ),
            target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id="$thread",
                reply_to_event_id=event.event_id,
            ),
            correlation_id="corr-payload-builder-failure-timing",
            envelope=_hook_envelope(body="hello", source_event_id="$event"),
        )

        install_generate_response_mock(bot, AsyncMock(return_value="$response"))
        _replace_turn_policy_deps(
            bot,
            logger=bot.logger,
            response_runner=bot._response_runner,
        )

        async def payload_builder(_context: MessageContext) -> DispatchPayload:
            msg = "payload failed"
            raise RuntimeError(msg)

        with (
            patch.object(bot._turn_controller, "_finalize_dispatch_failure", new=AsyncMock(return_value="$error")),
            patch("mindroom.turn_controller.emit_elapsed_timing") as mock_emit,
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

        builder_calls = [
            call for call in mock_emit.call_args_list if call.args and call.args[0] == "response_payload.builder"
        ]
        assert len(builder_calls) == 1
        assert isinstance(builder_calls[0].args[1], float)
        assert builder_calls[0].kwargs == {
            "room_id": "!room:localhost",
            "thread_id": "$thread",
            "outcome": "failed",
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enable_streaming", [True, False])
    @patch("mindroom.config.main.Config.from_yaml")
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
        mock_resolve_agent_knowledge_access.return_value = KnowledgeResolution(knowledge=None)
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
        mock_fetch_snapshot.return_value = thread_history_result(test1_history, is_full_history=False)

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
                sender=config.get_ids(runtime_paths_for(config))["general"].full_id
                if "general" in config.get_ids(runtime_paths_for(config))
                else "@mindroom_general:localhost",
                body="Another agent response",
                timestamp=125,
                event_id="prev3",
            ),
        ]
        mock_fetch_history.return_value = test2_history
        mock_fetch_snapshot.return_value = thread_history_result(test2_history, is_full_history=False)

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
        await drain_coalescing(bot)

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
            patch("mindroom.orchestrator.matrix_state_for_runtime", return_value=MatrixState()),
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
            patch("mindroom.orchestrator.matrix_state_for_runtime", return_value=MatrixState()),
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
            patch("mindroom.orchestrator.matrix_state_for_runtime", return_value=state),
        ):
            await orchestrator._ensure_room_invitations()

        mock_invite.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_room_invitations_ignores_persisted_ad_hoc_invited_rooms(self, tmp_path: Path) -> None:
        """Persisted ad-hoc invites must not leak into normal invitation fan-out."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(
                        display_name="GeneralAgent",
                        rooms=["!managed:localhost"],
                    ),
                },
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        invited_rooms_path = agent_state_root_path(runtime_paths.storage_root, "general") / "invited_rooms.json"
        invited_rooms_path.parent.mkdir(parents=True, exist_ok=True)
        invited_rooms_path.write_text('[\n  "!ad-hoc:localhost"\n]\n', encoding="utf-8")

        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = config

        router_bot = MagicMock()
        router_bot.client = AsyncMock()
        orchestrator.agent_bots = {"router": router_bot}

        with (
            patch("mindroom.constants.runtime_matrix_homeserver", return_value="http://localhost:8008"),
            patch("mindroom.orchestrator.get_joined_rooms", new=AsyncMock(return_value=["!ad-hoc:localhost"])),
            patch("mindroom.orchestrator.get_room_members", new=AsyncMock()) as mock_get_room_members,
            patch("mindroom.orchestrator.invite_to_room", AsyncMock()) as mock_invite,
            patch("mindroom.orchestrator.matrix_state_for_runtime", return_value=MatrixState()),
        ):
            await orchestrator._ensure_room_invitations()

        mock_get_room_members.assert_not_awaited()
        mock_invite.assert_not_awaited()

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
            try:
                await orchestrator.initialize()
            finally:
                await orchestrator._close_runtime_support_services()

        mock_load_config.assert_called_once()
        assert mock_load_config.call_args.args[0].config_path == config_path.resolve()

    @pytest.mark.asyncio
    async def test_initialize_degrades_when_shared_event_cache_init_fails(self, tmp_path: Path) -> None:
        """Initialize should keep starting bots when the shared event cache cannot open."""
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
            patch(
                "mindroom.runtime_support.SqliteEventCache.initialize",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch.object(MultiAgentOrchestrator, "_create_managed_bot") as mock_create_managed_bot,
        ):
            await orchestrator.initialize()

        assert orchestrator.config is config
        assert mock_create_managed_bot.call_count == 2
        assert orchestrator._runtime_support.event_cache.is_initialized is False

    @pytest.mark.asyncio
    async def test_sync_event_cache_service_uses_shared_runtime_support_sync(self, tmp_path: Path) -> None:
        """Shared runtime cache lifecycle should route through the shared sync helper."""
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
        router_bot = _mock_managed_bot(config)
        general_bot = _mock_managed_bot(config)
        orchestrator.agent_bots = {"router": router_bot, "general": general_bot}
        initial_support = orchestrator._runtime_support
        synced_support = SimpleNamespace(
            event_cache=make_event_cache_mock(),
            event_cache_write_coordinator=make_event_cache_write_coordinator_mock(),
            startup_thread_prewarm_registry=StartupThreadPrewarmRegistry(),
        )

        with patch(
            "mindroom.orchestrator.sync_owned_runtime_support",
            new=AsyncMock(return_value=synced_support),
            create=True,
        ) as sync_owned_runtime_support:
            await orchestrator._sync_event_cache_service(config)

        sync_owned_runtime_support.assert_awaited_once()
        assert sync_owned_runtime_support.await_args.args == (initial_support,)
        assert sync_owned_runtime_support.await_args.kwargs == {
            "cache_config": config.cache,
            "runtime_paths": orchestrator.runtime_paths,
            "logger": ANY,
            "background_task_owner": orchestrator._event_cache_write_task_owner,
            "init_failure_reason_prefix": "shared_runtime_init_failed",
            "log_db_path_change": True,
        }
        assert orchestrator._runtime_support is synced_support
        assert router_bot.event_cache is synced_support.event_cache
        assert general_bot.event_cache is synced_support.event_cache
        assert router_bot.event_cache_write_coordinator is synced_support.event_cache_write_coordinator
        assert general_bot.event_cache_write_coordinator is synced_support.event_cache_write_coordinator

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
    async def test_orchestrator_start_sets_up_rooms_before_auxiliary_workers(self, tmp_path: Path) -> None:
        """Room creation/invites should happen before auxiliary runtime workers."""
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = MagicMock()

        bot = MagicMock()
        bot.agent_name = "router"
        bot.try_start = AsyncMock(return_value=True)
        bot.stop = AsyncMock()
        orchestrator.agent_bots = {"router": bot}

        call_order: list[str] = []

        async def _wait_for_homeserver(*_args: object, **_kwargs: object) -> None:
            call_order.append("wait_for_homeserver")

        async def _setup_rooms(_: list[Any]) -> None:
            call_order.append("setup_rooms")

        async def _sync_runtime_support_services(*_args: object, **_kwargs: object) -> None:
            call_order.append("support_services")

        with (
            patch("mindroom.orchestrator.wait_for_matrix_homeserver", side_effect=_wait_for_homeserver),
            patch.object(orchestrator, "_setup_rooms_and_memberships", side_effect=_setup_rooms),
            patch.object(orchestrator, "_sync_runtime_support_services", side_effect=_sync_runtime_support_services),
            patch("mindroom.orchestrator.sync_forever_with_restart", new=AsyncMock()),
        ):
            await _run_orchestrator_start_until_ready(orchestrator)

        assert call_order == ["wait_for_homeserver", "setup_rooms", "support_services"]
        bot.try_start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orchestrator_start_syncs_knowledge_watchers_after_runtime_starts(self, tmp_path: Path) -> None:
        """Normal startup should start watch-owned knowledge refresh after reply paths are live."""
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        config = MagicMock()
        orchestrator.config = config

        bot = MagicMock()
        bot.agent_name = "router"
        bot.try_start = AsyncMock(return_value=True)
        bot.stop = AsyncMock()
        orchestrator.agent_bots = {"router": bot}

        async def _sync_runtime_support_services(*args: object, **kwargs: object) -> None:
            assert orchestrator.running is True
            assert args == (config,)
            assert kwargs == {"start_watcher": True}

        with (
            patch("mindroom.orchestrator.wait_for_matrix_homeserver", new=AsyncMock()),
            patch.object(orchestrator, "_setup_rooms_and_memberships", new=AsyncMock()),
            patch.object(
                orchestrator,
                "_sync_runtime_support_services",
                side_effect=_sync_runtime_support_services,
            ) as sync_runtime_support_services,
            patch("mindroom.orchestrator.sync_forever_with_restart", new=AsyncMock()),
        ):
            await _run_orchestrator_start_until_ready(orchestrator)

        sync_runtime_support_services.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orchestrator_start_discards_tool_approval_cards_on_router_ready(
        self,
        tmp_path: Path,
    ) -> None:
        """Router readiness should trigger Matrix-backed startup discard after room setup."""
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = MagicMock()
        orchestrator.config.tool_approval.timeout_days = 7.0
        orchestrator.config.tool_approval.rules = [SimpleNamespace(timeout_days=10.0)]

        bot = MagicMock()
        bot.agent_name = "router"
        bot.running = False
        bot.client = make_matrix_client_mock(user_id="@mindroom_router:localhost")

        async def _start_bot() -> bool:
            bot.running = True
            return True

        bot.try_start = AsyncMock(side_effect=_start_bot)

        async def _emit_bot_ready(_response: object) -> None:
            await orchestrator.handle_bot_ready(bot)

        bot._on_sync_response = AsyncMock(side_effect=_emit_bot_ready)
        bot.stop = AsyncMock()
        orchestrator.agent_bots = {"router": bot}

        call_order: list[str] = []

        async def _wait_for_homeserver(*_args: object, **_kwargs: object) -> None:
            call_order.append("wait_for_homeserver")

        async def _setup_rooms(_: list[Any]) -> None:
            call_order.append("setup_rooms")

        async def _sync_runtime_support_services(*_: object, **__: object) -> None:
            call_order.append("support_services")

        async def _discard_pending_on_startup(*, lookback_hours: int) -> int:
            assert lookback_hours == 240
            call_order.append("startup_discard")
            return 2

        async def _sync_forever_with_restart(started_bot: object) -> None:
            await cast("Any", started_bot)._on_sync_response(MagicMock(spec=nio.SyncResponse))

        with (
            patch("mindroom.orchestrator.wait_for_matrix_homeserver", side_effect=_wait_for_homeserver),
            patch.object(orchestrator, "_setup_rooms_and_memberships", side_effect=_setup_rooms),
            patch(
                "mindroom.approval_transport.expire_orphaned_approval_cards_on_startup",
                new=AsyncMock(side_effect=_discard_pending_on_startup),
            ) as expire_orphaned_approval_cards_on_startup,
            patch.object(orchestrator, "_sync_runtime_support_services", side_effect=_sync_runtime_support_services),
            patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
            patch("mindroom.orchestrator.sync_forever_with_restart", side_effect=_sync_forever_with_restart),
        ):
            await _run_orchestrator_start_until_ready(orchestrator)

        assert call_order == [
            "wait_for_homeserver",
            "setup_rooms",
            "support_services",
            "startup_discard",
        ]
        expire_orphaned_approval_cards_on_startup.assert_awaited_once_with(lookback_hours=240)
        bot.try_start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_bot_ready_skips_startup_discard_for_non_router_bots(
        self,
        tmp_path: Path,
    ) -> None:
        """Only the router owns startup approval cleanup."""
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))

        bot = MagicMock()
        bot.agent_name = "code"
        bot.running = True
        bot.client = make_matrix_client_mock(user_id="@mindroom_code:localhost")

        with patch(
            "mindroom.approval_transport.expire_orphaned_approval_cards_on_startup",
            new=AsyncMock(),
        ) as expire_orphaned_approval_cards_on_startup:
            await orchestrator.handle_bot_ready(bot)

        expire_orphaned_approval_cards_on_startup.assert_not_awaited()

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
            bot.stop = AsyncMock()
            orchestrator.agent_bots = {"router": bot}

        with (
            patch("mindroom.orchestrator.wait_for_matrix_homeserver", side_effect=_wait_for_homeserver),
            patch.object(orchestrator, "initialize", side_effect=_initialize),
            patch.object(orchestrator, "_setup_rooms_and_memberships", new=AsyncMock()),
            patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
            patch("mindroom.orchestrator.sync_forever_with_restart", new=AsyncMock()),
        ):
            await _run_orchestrator_start_until_ready(orchestrator)

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
    async def test_orchestrator_start_schedules_retry_for_failed_agents(self, tmp_path: Path) -> None:
        """Startup should keep degraded agents around and retry them in the background."""
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = MagicMock()

        router_bot = MagicMock()
        router_bot.agent_name = "router"
        router_bot.try_start = AsyncMock(return_value=True)
        router_bot.stop = AsyncMock()

        failing_bot = MagicMock()
        failing_bot.agent_name = "general"
        failing_bot.try_start = AsyncMock(return_value=False)
        failing_bot.stop = AsyncMock()

        orchestrator.agent_bots = {"router": router_bot, "general": failing_bot}

        with (
            patch("mindroom.orchestrator.wait_for_matrix_homeserver", new=AsyncMock()),
            patch.object(orchestrator, "_setup_rooms_and_memberships", new=AsyncMock()),
            patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
            patch.object(orchestrator, "_schedule_bot_start_retry", new=AsyncMock()) as mock_schedule_retry,
            patch("mindroom.orchestrator.sync_forever_with_restart", new=AsyncMock()),
        ):
            await _run_orchestrator_start_until_ready(orchestrator)

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
        router_bot.stop = AsyncMock()

        failing_bot = MagicMock()
        failing_bot.agent_name = "general"
        failing_bot.try_start = AsyncMock(side_effect=PermanentMatrixStartupError("boom"))
        failing_bot.stop = AsyncMock()

        orchestrator.agent_bots = {"router": router_bot, "general": failing_bot}

        with (
            patch("mindroom.orchestrator.wait_for_matrix_homeserver", new=AsyncMock()),
            patch.object(orchestrator, "_setup_rooms_and_memberships", new=AsyncMock()),
            patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
            patch.object(orchestrator, "_schedule_bot_start_retry", new=AsyncMock()) as mock_schedule_retry,
            patch("mindroom.orchestrator.sync_forever_with_restart", new=AsyncMock()),
        ):
            await _run_orchestrator_start_until_ready(orchestrator)

        assert "general" in orchestrator.agent_bots
        mock_schedule_retry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shutdown_expires_in_flight_approval_send_after_event_id_arrives(  # noqa: PLR0915
        self,
        tmp_path: Path,
    ) -> None:
        """Shutdown should settle approval sends that receive a card id during shutdown."""
        runtime_paths = TestAgentBot._runtime_paths(tmp_path)
        orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
        orchestrator._capture_runtime_loop()

        send_started = asyncio.Event()
        allow_send_to_finish = asyncio.Event()

        async def _room_send(
            room_id: str,
            message_type: str,
            content: dict[str, object],
        ) -> nio.RoomSendResponse:
            del content
            assert room_id == "!room:localhost"
            assert message_type == "io.mindroom.tool_approval"
            send_started.set()
            await allow_send_to_finish.wait()
            return nio.RoomSendResponse(event_id="$approval", room_id=room_id)

        router_client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
        router_client.room_send = AsyncMock(side_effect=_room_send)
        router_client.rooms["!room:localhost"].add_member(router_client.user_id, "Router", None)
        router_bot = MagicMock()
        router_bot.agent_name = "router"
        router_bot.running = True
        router_bot.client = router_client
        router_bot.event_cache = make_event_cache_mock()
        router_bot.stop = AsyncMock()

        code_bot = MagicMock()
        code_bot.agent_name = "code"
        code_bot.running = True
        code_bot.client = make_matrix_client_mock(user_id="@mindroom_code:localhost")
        code_bot.stop = AsyncMock()
        orchestrator.agent_bots = {"router": router_bot, "code": code_bot}

        task: asyncio.Task[object] | None = None
        try:
            store = initialize_approval_store(
                runtime_paths,
                sender=orchestrator._approval_transport.send_approval_event,
                editor=orchestrator._approval_transport.edit_approval_event,
            )
            task = asyncio.create_task(
                store.request_approval(
                    tool_name="run_shell_command",
                    arguments={"command": "echo hi"},
                    agent_name="code",
                    room_id="!room:localhost",
                    thread_id=None,
                    requester_id="@user:localhost",
                    approver_user_id="@user:localhost",
                    timeout_seconds=60,
                ),
            )

            await send_started.wait()
            orchestrator._knowledge_refresh_scheduler = MagicMock()
            orchestrator._knowledge_refresh_scheduler.shutdown = AsyncMock()

            with (
                patch.object(orchestrator, "_cancel_config_reload_task", new=AsyncMock()),
                patch.object(orchestrator, "_stop_memory_auto_flush_worker", new=AsyncMock()),
                patch.object(orchestrator._knowledge_source_watcher, "shutdown", new=AsyncMock()),
                patch.object(orchestrator, "_cancel_bot_start_tasks", new=AsyncMock()),
                patch.object(orchestrator, "_stop_mcp_manager", new=AsyncMock()),
                patch.object(orchestrator, "_close_runtime_support_services", new=AsyncMock()),
            ):
                stop_task = asyncio.create_task(orchestrator.stop())
                await asyncio.sleep(0)
                assert stop_task.done() is False
                allow_send_to_finish.set()
                await stop_task

            decision = await asyncio.wait_for(task, timeout=1)
            assert decision.status == "expired"
            assert decision.reason == "MindRoom shut down before approval completed."
            assert router_bot.running is False
            router_bot.stop.assert_awaited_once_with(reason="shutdown")
        finally:
            allow_send_to_finish.set()
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_orchestrator_stop_shuts_down_approvals_before_mcp_manager(
        self,
        tmp_path: Path,
    ) -> None:
        """Pending approvals should expire even if MCP shutdown fails."""
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        calls: list[str] = []

        async def _shutdown_approvals() -> None:
            calls.append("approvals")

        async def _stop_mcp_manager() -> None:
            calls.append("mcp")
            msg = "mcp shutdown failed"
            raise RuntimeError(msg)

        orchestrator._knowledge_refresh_scheduler = MagicMock()
        orchestrator._knowledge_refresh_scheduler.shutdown = AsyncMock()

        with (
            patch(
                "mindroom.orchestrator.shutdown_approval_runtime",
                new=AsyncMock(side_effect=_shutdown_approvals),
            ) as mock_shutdown_approvals,
            patch.object(orchestrator, "_cancel_config_reload_task", new=AsyncMock()),
            patch.object(orchestrator, "_stop_memory_auto_flush_worker", new=AsyncMock()),
            patch.object(orchestrator._knowledge_source_watcher, "shutdown", new=AsyncMock()),
            patch.object(orchestrator, "_cancel_bot_start_tasks", new=AsyncMock()),
            patch.object(orchestrator, "_stop_mcp_manager", new=AsyncMock(side_effect=_stop_mcp_manager)),
            pytest.raises(RuntimeError, match="mcp shutdown failed"),
        ):
            await orchestrator.stop()

        assert calls == ["approvals", "mcp"]
        mock_shutdown_approvals.assert_awaited_once()

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

        with patch.object(uvicorn.Server, "handle_exit"):
            server.handle_exit(signal.SIGINT, None)

        assert shutdown_requested.is_set()

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
    async def test_update_config_syncs_runtime_services_when_running(self, tmp_path: Path) -> None:
        """Hot reload should sync runtime services without global knowledge refresh work."""
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
            patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()) as mock_sync_runtime,
        ):
            updated = await orchestrator.update_config()

        assert updated is False
        mock_sync_runtime.assert_awaited_once_with(config, start_watcher=True)
        assert not hasattr(orchestrator, "_schedule_knowledge_refresh")

    @pytest.mark.asyncio
    async def test_sync_runtime_support_services_rebinds_approval_store_cache(self, tmp_path: Path) -> None:
        """Approval store transport should track replaced runtime cache objects."""
        config = _runtime_bound_config(
            Config(
                agents={"calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!test:localhost"])},
                models={"default": ModelConfig(provider="test", id="test-model")},
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
        old_cache = MagicMock()
        new_cache = MagicMock()
        support = SimpleNamespace(
            event_cache=new_cache,
            event_cache_write_coordinator=MagicMock(),
            startup_thread_prewarm_registry=StartupThreadPrewarmRegistry(),
        )
        store = initialize_approval_store(runtime_paths, event_cache=old_cache)

        try:
            with (
                patch("mindroom.orchestrator.sync_owned_runtime_support", new=AsyncMock(return_value=support)),
                patch.object(orchestrator._knowledge_source_watcher, "sync", new=AsyncMock()),
                patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
            ):
                await orchestrator._sync_runtime_support_services(config, start_watcher=False)

            assert get_approval_store() is store
            assert store._event_cache is new_cache
        finally:
            await shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_update_config_keeps_router_owned_approvals_pending_when_requesting_bot_is_removed(
        self,
        tmp_path: Path,
    ) -> None:
        """Hot reload should not expire a pending approval just because the requesting bot was removed."""
        runtime_paths = TestAgentBot._runtime_paths(tmp_path)
        orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
        orchestrator._capture_runtime_loop()

        old_config = _approval_reload_config(tmp_path, include_code=True)
        new_config = _approval_reload_config(tmp_path, include_code=False)
        orchestrator.config = old_config
        orchestrator.running = True

        event_order: list[str] = []
        approval_ids: list[str] = []

        async def _router_room_send(
            *,
            room_id: str,
            message_type: str,
            content: dict[str, object],
        ) -> nio.RoomSendResponse:
            assert room_id == "!room:localhost"
            assert message_type == "io.mindroom.tool_approval"
            if "m.new_content" in content:
                event_order.append("edit")
                return nio.RoomSendResponse(event_id="$approval-edit", room_id=room_id)
            event_order.append("send")
            approval_id = content.get("approval_id")
            assert isinstance(approval_id, str)
            approval_ids.append(approval_id)
            return nio.RoomSendResponse(event_id="$approval", room_id=room_id)

        router_bot = _mock_approval_reload_bot(
            old_config,
            agent_name="router",
            user_id="@mindroom_router:localhost",
            room_send=AsyncMock(side_effect=_router_room_send),
        )
        code_bot = _mock_approval_reload_bot(
            old_config,
            agent_name="code",
            user_id="@mindroom_code:localhost",
            room_send=AsyncMock(),
        )
        code_bot.cleanup = AsyncMock(side_effect=_cleanup_recorder(event_order))
        orchestrator.agent_bots = {"router": router_bot, "code": code_bot}

        plan = _approval_removal_plan(new_config)
        task: asyncio.Task[object] | None = None
        try:
            store = initialize_approval_store(
                runtime_paths,
                sender=orchestrator._approval_transport.send_approval_event,
                editor=orchestrator._approval_transport.edit_approval_event,
            )
            task = asyncio.create_task(
                store.request_approval(
                    tool_name="run_shell_command",
                    arguments={"command": "echo hi"},
                    agent_name="code",
                    room_id="!room:localhost",
                    thread_id="$thread",
                    requester_id="@user:localhost",
                    approver_user_id="@user:localhost",
                    timeout_seconds=60,
                ),
            )

            approval_id = await _wait_for_pending_approval_id(store, approval_ids)

            with (
                patch("mindroom.orchestrator.load_config", return_value=new_config),
                patch("mindroom.orchestrator.build_config_update_plan", return_value=plan),
                patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
                patch.object(orchestrator, "_sync_event_cache_service", new=AsyncMock()),
                patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
                patch.object(orchestrator, "_emit_config_reloaded", new=AsyncMock()),
                patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
            ):
                updated = await orchestrator.update_config()

            assert updated is True
            assert task.done() is False
            assert event_order == ["send", "cleanup"]
            pending = await store.get_pending_approval("!room:localhost", approval_id)
            assert pending is not None

            await store.resolve_approval(
                card_event_id=pending.card_event_id,
                room_id=pending.room_id,
                status="approved",
                resolved_by="@user:localhost",
            )
            decision = await task

            assert decision.status == "approved"
            assert event_order == ["send", "cleanup", "edit"]
            assert router_bot.client is not None
            assert router_bot.client.room_send.await_count == 2
        finally:
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await shutdown_approval_store()
            await orchestrator._close_runtime_support_services()

    @pytest.mark.asyncio
    async def test_requesting_bot_room_reconcile_keeps_router_owned_approval_pending(  # noqa: PLR0915
        self,
        tmp_path: Path,
    ) -> None:
        """Leaving the requesting bot's room should not force-expire a router-owned approval."""
        runtime_paths = TestAgentBot._runtime_paths(tmp_path)
        orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
        orchestrator._capture_runtime_loop()

        old_config = _runtime_bound_config(
            Config(
                agents={
                    "code": {
                        "display_name": "CodeAgent",
                        "role": "Writes code",
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
                    "code": {
                        "display_name": "CodeAgent",
                        "role": "Writes code",
                        "model": "default",
                        "rooms": [],
                    },
                },
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
        )

        event_order: list[str] = []
        approval_ids: list[str] = []

        async def _router_room_send(
            *,
            room_id: str,
            message_type: str,
            content: dict[str, object],
        ) -> nio.RoomSendResponse:
            assert room_id == "!room:localhost"
            assert message_type == "io.mindroom.tool_approval"
            if "m.new_content" in content:
                event_order.append("edit")
                return nio.RoomSendResponse(event_id="$approval-edit", room_id=room_id)
            event_order.append("send")
            approval_id = content.get("approval_id")
            assert isinstance(approval_id, str)
            approval_ids.append(approval_id)
            return nio.RoomSendResponse(event_id="$approval", room_id=room_id)

        router_bot = _mock_approval_reload_bot(
            old_config,
            agent_name="router",
            user_id="@mindroom_router:localhost",
            room_send=AsyncMock(side_effect=_router_room_send),
        )

        code_user = AgentMatrixUser(
            agent_name="code",
            user_id="@mindroom_code:localhost",
            display_name="CodeAgent",
            password=TEST_PASSWORD,
        )
        code_bot = AgentBot(
            code_user,
            tmp_path,
            config=old_config,
            runtime_paths=runtime_paths_for(old_config),
            rooms=["!room:localhost"],
        )
        code_bot.orchestrator = orchestrator
        code_bot.client = make_matrix_client_mock(user_id=code_user.user_id)
        code_bot.client.room_send = AsyncMock()
        code_bot.client.rooms["!room:localhost"].add_member(code_user.user_id, code_user.display_name, None)
        code_bot.latest_thread_event_id_if_needed = AsyncMock(
            return_value="$latest-thread-event",
        )
        code_bot.running = True

        orchestrator.agent_bots = {"router": router_bot, "code": code_bot}

        leave_non_dm_rooms = AsyncMock(side_effect=lambda *_args, **_kwargs: event_order.append("leave"))
        task: asyncio.Task[object] | None = None
        try:
            store = initialize_approval_store(
                runtime_paths,
                sender=orchestrator._approval_transport.send_approval_event,
                editor=orchestrator._approval_transport.edit_approval_event,
            )
            task = asyncio.create_task(
                store.request_approval(
                    tool_name="run_shell_command",
                    arguments={"command": "echo hi"},
                    agent_name="code",
                    room_id="!room:localhost",
                    thread_id="$thread",
                    requester_id="@user:localhost",
                    approver_user_id="@user:localhost",
                    timeout_seconds=60,
                ),
            )

            approval_id = await _wait_for_pending_approval_id(store, approval_ids)

            code_bot.config = new_config
            code_bot.rooms = []

            with (
                patch("mindroom.bot_room_lifecycle.get_joined_rooms", new=AsyncMock(return_value=["!room:localhost"])),
                patch("mindroom.bot_room_lifecycle.leave_non_dm_rooms", new=leave_non_dm_rooms),
            ):
                await code_bot.leave_unconfigured_rooms()

            assert task.done() is False
            pending = await store.get_pending_approval("!room:localhost", approval_id)
            assert pending is not None
            assert event_order == ["send", "leave"]
            leave_non_dm_rooms.assert_awaited_once()

            await store.resolve_approval(
                card_event_id=pending.card_event_id,
                room_id=pending.room_id,
                status="approved",
                resolved_by="@user:localhost",
            )
            decision = await task

            assert decision.status == "approved"
            assert event_order == ["send", "leave", "edit"]
        finally:
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await shutdown_approval_store()

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
            patch("mindroom.orchestrator.set_scheduling_hook_registry") as mock_set_scheduling_hook_registry,
            patch("mindroom.orchestrator.clear_worker_validation_snapshot_cache") as mock_clear_snapshot_cache,
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
        mock_set_scheduling_hook_registry.assert_not_called()
        mock_clear_snapshot_cache.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_config_does_not_stop_mcp_entities_before_plugin_reload_succeeds(
        self,
        tmp_path: Path,
    ) -> None:
        """Plugin reload validation must happen before any MCP-driven entity shutdown."""
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))

        current_config = Config(
            agents={"general": {"display_name": "GeneralAgent", "model": "default"}},
            models={"default": {"provider": "test", "id": "test-model"}},
            plugins=["./plugins/current"],
        )
        new_config = Config(
            agents={"general": {"display_name": "GeneralAgent", "model": "default"}},
            models={"default": {"provider": "test", "id": "test-model"}},
            plugins=["./plugins/updated"],
        )

        orchestrator.config = current_config
        bot = _mock_managed_bot(current_config)
        bot.running = True
        orchestrator.agent_bots = {"general": bot}
        plan = ConfigUpdatePlan(
            new_config=new_config,
            changed_mcp_servers={"demo-server"},
            all_new_entities=set(),
            entities_to_restart=set(),
            new_entities=set(),
            removed_entities=set(),
            mindroom_user_changed=False,
            matrix_room_access_changed=False,
            matrix_space_changed=False,
            authorization_changed=False,
        )

        stop_entities_before_mcp_sync = AsyncMock(return_value={"general"})

        with (
            patch("mindroom.orchestrator.load_config", return_value=new_config),
            patch("mindroom.orchestrator.build_config_update_plan", return_value=plan),
            patch.object(
                orchestrator,
                "_stop_entities_before_mcp_sync",
                new=stop_entities_before_mcp_sync,
            ),
            patch(
                "mindroom.orchestrator.prepare_plugin_reload",
                side_effect=RuntimeError("broken plugin"),
            ),
            patch("mindroom.orchestrator.clear_worker_validation_snapshot_cache") as mock_clear_snapshot_cache,
            pytest.raises(RuntimeError, match="broken plugin"),
        ):
            await orchestrator.update_config()

        stop_entities_before_mcp_sync.assert_not_awaited()
        assert bot.running is True
        assert orchestrator.config is current_config
        mock_clear_snapshot_cache.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_config_does_not_leak_plugin_state_before_config_commit(
        self,
        tmp_path: Path,
    ) -> None:
        """Plugin validation during config reload must not mutate live plugin state on later failure."""
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))

        plugin_root = tmp_path / "plugins" / "updated"
        skill_dir = plugin_root / "skills" / "updated-skill"
        skill_dir.mkdir(parents=True)
        (plugin_root / "mindroom.plugin.json").write_text(
            '{"name":"updated","tools_module":"tools.py","hooks_module":"hooks.py","skills":["skills"]}',
            encoding="utf-8",
        )
        (plugin_root / "tools.py").write_text(
            "from agno.tools import Toolkit\n"
            "from mindroom.tool_system.metadata import ToolCategory, register_tool_with_metadata\n"
            "\n"
            "class UpdatedTool(Toolkit):\n"
            "    def __init__(self) -> None:\n"
            "        super().__init__(name='updated', tools=[])\n"
            "\n"
            "@register_tool_with_metadata(\n"
            "    name='updated_plugin_tool',\n"
            "    display_name='Updated Plugin Tool',\n"
            "    description='updated plugin tool',\n"
            "    category=ToolCategory.DEVELOPMENT,\n"
            ")\n"
            "def updated_plugin_tools():\n"
            "    return UpdatedTool\n",
            encoding="utf-8",
        )
        (plugin_root / "hooks.py").write_text(
            "from mindroom.hooks import hook\n"
            "\n"
            "@hook('message:received')\n"
            "async def audit(ctx):\n"
            "    del ctx\n"
            "    return None\n",
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text(
            "---\nname: updated-skill\ndescription: demo\n---\n",
            encoding="utf-8",
        )

        current_config = _runtime_bound_config(
            Config(
                agents={"general": {"display_name": "GeneralAgent", "model": "default"}},
                models={"default": {"provider": "test", "id": "test-model"}},
                plugins=[],
            ),
            tmp_path,
        )
        new_config = _runtime_bound_config(
            Config(
                agents={"general": {"display_name": "GeneralAgent", "model": "default"}},
                models={"default": {"provider": "test", "id": "test-model"}},
                plugins=["./plugins/updated"],
            ),
            tmp_path,
        )

        orchestrator.config = current_config
        old_hook_registry = HookRegistry.empty()
        orchestrator.hook_registry = old_hook_registry
        plan = ConfigUpdatePlan(
            new_config=new_config,
            changed_mcp_servers={"demo-server"},
            all_new_entities=set(),
            entities_to_restart=set(),
            new_entities=set(),
            removed_entities=set(),
            mindroom_user_changed=False,
            matrix_room_access_changed=False,
            matrix_space_changed=False,
            authorization_changed=False,
        )

        original_plugin_skill_roots = _get_plugin_skill_roots()
        set_plugin_skill_roots([])
        try:
            with (
                patch("mindroom.orchestrator.load_config", return_value=new_config),
                patch("mindroom.orchestrator.build_config_update_plan", return_value=plan),
                patch.object(
                    orchestrator,
                    "_stop_entities_before_mcp_sync",
                    new=AsyncMock(side_effect=RuntimeError("stop failed")),
                ),
                pytest.raises(RuntimeError, match="stop failed"),
            ):
                await orchestrator.update_config()

            assert orchestrator.config is current_config
            assert orchestrator.hook_registry is old_hook_registry
            assert "updated_plugin_tool" not in TOOL_METADATA
            assert _get_plugin_skill_roots() == []
        finally:
            set_plugin_skill_roots(original_plugin_skill_roots)

    @pytest.mark.asyncio
    async def test_update_config_preserves_watcher_dirty_state_for_stale_prepared_plugin_snapshot(
        self,
        tmp_path: Path,
    ) -> None:
        """A plugin edit during staged config reload must still be seen by the watcher."""
        orchestrator = MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))

        plugin_root = tmp_path / "plugins" / "updated"
        plugin_root.mkdir(parents=True)
        hooks_path = plugin_root / "hooks.py"
        (plugin_root / "mindroom.plugin.json").write_text(
            '{"name":"updated","hooks_module":"hooks.py","skills":[]}',
            encoding="utf-8",
        )
        hooks_path.write_text("VALUE = 1\n", encoding="utf-8")

        current_config = _runtime_bound_config(
            Config(
                agents={"general": {"display_name": "GeneralAgent", "model": "default"}},
                models={"default": {"provider": "test", "id": "test-model"}},
                plugins=[],
            ),
            tmp_path,
        )
        new_config = _runtime_bound_config(
            Config(
                agents={"general": {"display_name": "GeneralAgent", "model": "default"}},
                models={"default": {"provider": "test", "id": "test-model"}},
                plugins=["./plugins/updated"],
            ),
            tmp_path,
        )

        orchestrator.config = current_config
        orchestrator.hook_registry = HookRegistry.empty()
        plan = ConfigUpdatePlan(
            new_config=new_config,
            changed_mcp_servers=set(),
            all_new_entities=set(),
            entities_to_restart=set(),
            new_entities=set(),
            removed_entities=set(),
            mindroom_user_changed=False,
            matrix_room_access_changed=False,
            matrix_space_changed=False,
            authorization_changed=False,
        )

        original_plugin_skill_roots = _get_plugin_skill_roots()
        original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
        original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
        original_modules = set(sys.modules)
        set_plugin_skill_roots([])
        try:

            async def mutate_plugin_after_prepare(*_args: object, **_kwargs: object) -> set[str]:
                hooks_path.write_text("VALUE = 2\n", encoding="utf-8")
                return set()

            with (
                patch("mindroom.orchestrator.load_config", return_value=new_config),
                patch("mindroom.orchestrator.build_config_update_plan", return_value=plan),
                patch.object(
                    orchestrator,
                    "_stop_entities_before_mcp_sync",
                    new=AsyncMock(side_effect=mutate_plugin_after_prepare),
                ),
                patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
                patch.object(orchestrator, "_sync_event_cache_service", new=AsyncMock()),
                patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
                patch.object(orchestrator, "_emit_config_reloaded", new=AsyncMock()),
            ):
                updated = await orchestrator.update_config()

            assert updated is False
            loaded_hooks_module = plugin_module._MODULE_IMPORT_CACHE[hooks_path.resolve()].module
            assert loaded_hooks_module.VALUE == 1

            changed_paths = collect_plugin_root_changes(
                tuple(orchestrator._plugin_watch_last_snapshot_by_root),
                orchestrator._plugin_watch_last_snapshot_by_root,
            )
            assert changed_paths == {hooks_path.resolve()}
        finally:
            plugin_module._PLUGIN_CACHE.clear()
            plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
            plugin_module._MODULE_IMPORT_CACHE.clear()
            plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
            set_plugin_skill_roots(original_plugin_skill_roots)
            for module_name in set(sys.modules) - original_modules:
                if module_name.startswith("mindroom_plugin_"):
                    sys.modules.pop(module_name, None)

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
            patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
        ):
            try:
                updated = await orchestrator.update_config()
                assert updated is False
                assert router_bot.event_cache is orchestrator._runtime_support.event_cache
                assert general_bot.event_cache is orchestrator._runtime_support.event_cache
                assert (
                    router_bot.event_cache_write_coordinator
                    is orchestrator._runtime_support.event_cache_write_coordinator
                )
                assert (
                    general_bot.event_cache_write_coordinator
                    is orchestrator._runtime_support.event_cache_write_coordinator
                )
            finally:
                await orchestrator._close_runtime_support_services()

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
        old_cache = orchestrator._runtime_support.event_cache
        assert old_cache is not None

        with (
            patch("mindroom.orchestrator.load_config", return_value=new_config),
            patch("mindroom.orchestrator.load_plugins", return_value=[]),
            patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
            patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
        ):
            try:
                updated = await orchestrator.update_config()
                assert updated is False
                assert orchestrator._runtime_support.event_cache is old_cache
                assert old_cache.db_path == old_config.cache.resolve_db_path(orchestrator.runtime_paths)
                assert router_bot.event_cache is old_cache
                assert general_bot.event_cache is old_cache
                assert orchestrator._runtime_support.event_cache_write_coordinator is not None
                assert (
                    router_bot.event_cache_write_coordinator
                    is orchestrator._runtime_support.event_cache_write_coordinator
                )
                assert (
                    general_bot.event_cache_write_coordinator
                    is orchestrator._runtime_support.event_cache_write_coordinator
                )
            finally:
                await orchestrator._close_runtime_support_services()

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
            patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
            patch.object(orchestrator, "_ensure_rooms_exist", new=AsyncMock()),
            patch.object(orchestrator, "_ensure_room_invitations", new=AsyncMock()),
        ):
            try:
                updated = await orchestrator.update_config()
            finally:
                await orchestrator._close_runtime_support_services()

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
            patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
            patch.object(orchestrator, "_ensure_rooms_exist", new=AsyncMock()),
            patch.object(orchestrator, "_ensure_room_invitations", new=AsyncMock()),
        ):
            try:
                updated = await orchestrator.update_config()
            finally:
                await orchestrator._close_runtime_support_services()

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
