"""Shared helpers for the multi-agent bot test modules."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio

from mindroom.approval_manager import (
    PendingApproval,
    SentApprovalEvent,
    _ApprovalManager,
    initialize_approval_store,
)
from mindroom.attachments import AttachmentRecord, register_local_attachment
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import (
    VOICE_RAW_AUDIO_FALLBACK_KEY,
    RuntimePaths,
    resolve_runtime_paths,
)
from mindroom.dispatch_handoff import PreparedTextEvent
from mindroom.dispatch_source import (
    MESSAGE_SOURCE_KIND,
    VOICE_SOURCE_KIND,
)
from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome
from mindroom.handled_turns import TurnRecord
from mindroom.history.types import HistoryScope
from mindroom.hooks import (
    EnrichmentItem,
    MessageEnvelope,
)
from mindroom.knowledge.indexing_config import IndexingSettings
from mindroom.knowledge.utils import _KnowledgeResolution
from mindroom.matrix.cache import ThreadHistoryResult
from mindroom.matrix.client import ResolvedVisibleMessage
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.orchestration.config_updates import ConfigUpdatePlan
from mindroom.response_runner import (
    ResponseRequest,
)
from mindroom.runtime_support import StartupThreadPrewarmRegistry
from mindroom.tool_approval import _shutdown_approval_store
from mindroom.turn_policy import PreparedDispatch, TurnPolicy
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    drain_coalescing,
    make_event_cache_mock,
    make_event_cache_write_coordinator_mock,
    make_matrix_client_mock,
    message_origin,
    replace_turn_controller_deps,
    runtime_paths_for,
    test_runtime_paths,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)
from tests.conftest import replace_turn_policy_deps as shared_replace_turn_policy_deps
from tests.identity_helpers import entity_ids, persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence

    from agno.knowledge.document import Document

    from mindroom.bot import AgentBot, TeamBot
    from mindroom.coalescing import ReadyPendingEvent
    from mindroom.config.knowledge import KnowledgeBaseConfig
    from mindroom.conversation_resolver import MessageContext
    from mindroom.inbound_turn_normalizer import DispatchPayload
    from mindroom.media_inputs import MediaInputs
    from mindroom.orchestrator import (
        _MultiAgentOrchestrator,
    )
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


def _assert_ready_voice_text_fallback(ready_event: ReadyPendingEvent | None) -> None:
    assert ready_event is not None
    assert ready_event.pending_event.source_kind == VOICE_SOURCE_KIND
    assert isinstance(ready_event.pending_event.event, PreparedTextEvent)
    assert ready_event.pending_event.event.body == "🎤 [Attached voice message]"
    assert ready_event.pending_event.event.source["content"][VOICE_RAW_AUDIO_FALLBACK_KEY] is True


async def _run_orchestrator_start_until_ready(
    orchestrator: _MultiAgentOrchestrator,
    *,
    wait_for_startup_maintenance: bool = False,
) -> None:
    """Run start() until readiness, then explicitly shut the runtime down."""
    ready = asyncio.Event()

    def mark_ready() -> None:
        ready.set()

    with patch("mindroom.orchestrator.set_runtime_ready", side_effect=mark_ready):
        runtime_task = asyncio.create_task(orchestrator.start())
        try:
            await asyncio.wait_for(ready.wait(), timeout=1.0)
            if wait_for_startup_maintenance:
                maintenance_task = orchestrator._startup_maintenance.task
                assert maintenance_task is not None
                await maintenance_task
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


def _set_turn_store_tracker(bot: AgentBot | TeamBot, tracker: MagicMock) -> MagicMock:
    """Swap the private handled-turn ledger behind one turn store for test assertions."""
    stored_records: dict[str, TurnRecord] = {}

    def update_handled_turn(
        lookup_event_ids: Sequence[str],
        update: Callable[[Mapping[str, TurnRecord]], TurnRecord],
        *,
        wait_for_persist: bool = False,
    ) -> TurnRecord:
        del wait_for_persist  # The fake applies updates synchronously.
        existing_records = {
            source_event_id: turn_record
            for source_event_id in lookup_event_ids
            if isinstance(
                (turn_record := tracker.get_turn_record(source_event_id)),
                TurnRecord,
            )
            or isinstance((turn_record := stored_records.get(source_event_id)), TurnRecord)
        }
        turn_record = update(existing_records)
        for source_event_id in turn_record.indexed_event_ids:
            stored_records[source_event_id] = turn_record
        if turn_record.completed:
            tracker.record_handled_turn(turn_record)
        else:
            tracker.record_pending_turn(turn_record)
        return turn_record

    tracker.update_handled_turn.side_effect = update_handled_turn
    _turn_store(bot)._ledger = tracker
    return tracker


def _set_knowledge_for_agent(bot: AgentBot, knowledge_for_agent: MagicMock) -> MagicMock:
    """Replace the captured knowledge resolver on the real response coordinator."""
    bot._knowledge_access_support.for_agent = knowledge_for_agent
    resolve_for_agent = MagicMock(
        return_value=_KnowledgeResolution(knowledge=knowledge_for_agent.return_value),
    )
    bot._knowledge_access_support.resolve_for_agent = resolve_for_agent
    return resolve_for_agent


def _room_send_response(event_id: str) -> MagicMock:
    """Return a RoomSendResponse-shaped mock for Matrix send/edit tests."""
    response = MagicMock(spec=nio.RoomSendResponse, event_id=event_id)
    response.__class__ = nio.RoomSendResponse
    return response


def _matrix_room(
    room_id: str = "!room:localhost",
    own_user_id: str = "@mindroom_test:localhost",
    *,
    user_ids: Sequence[str] = (),
    invited_user_ids: Sequence[str] = (),
    canonical_alias: str | None = None,
    members_synced: bool = True,
) -> nio.MatrixRoom:
    """Return a real MatrixRoom with the membership fields responder policy reads."""
    room = nio.MatrixRoom(room_id=room_id, own_user_id=own_user_id)
    room.canonical_alias = canonical_alias
    room.members_synced = members_synced
    for user_id in user_ids:
        room.add_member(user_id, None, None)
    for user_id in invited_user_ids:
        room.add_member(user_id, None, None, invited=True)
    return room


def _policy_dispatch(
    bot: AgentBot | TeamBot,
    room: nio.MatrixRoom,
    context: MessageContext,
    requester_user_id: str,
    body: str,
    *,
    source_event_id: str = "$event",
    source_kind: str = MESSAGE_SOURCE_KIND,
) -> PreparedDispatch:
    """Build the complete prepared dispatch required by turn-policy tests."""
    target = MessageTarget.resolve(room.room_id, context.thread_id, source_event_id)
    envelope = MessageEnvelope(
        source_event_id=source_event_id,
        target=target,
        body=body,
        attachment_ids=(),
        mentioned_agents=tuple(context.mentioned_agents),
        agent_name=bot.agent_name,
        origin=message_origin(sender_id=requester_user_id, requester_id=requester_user_id, source_kind=source_kind),
    )
    return PreparedDispatch(
        requester_user_id=requester_user_id,
        context=context,
        target=target,
        correlation_id=source_event_id,
        envelope=envelope,
    )


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
) -> TurnRecord:
    """Return the handled-turn state persisted for one direct agent response."""
    return replace(
        TurnRecord.create(
            [event_id],
            response_event_id=response_event_id,
            requester_id=requester_id,
            correlation_id=correlation_id,
            source_event_prompts=source_event_prompts,
        ),
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
    response_envelope: MessageEnvelope,
    correlation_id: str | None = None,
    matrix_run_metadata: dict[str, Any] | None = None,
    system_enrichment_items: tuple[EnrichmentItem, ...] = (),
) -> ResponseRequest:
    """Build one response request for direct bot seam tests."""
    target = response_envelope.target
    if (
        room_id != target.room_id
        or reply_to_event_id != target.reply_to_event_id
        or thread_id != target.source_thread_id
    ):
        msg = "Test response envelope target does not match the source response coordinates"
        raise ValueError(msg)
    return ResponseRequest(
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
        matrix_run_metadata=matrix_run_metadata,
        system_enrichment_items=system_enrichment_items,
    )


def _runtime_bound_config(config: Config, runtime_root: Path) -> Config:
    """Return a runtime-bound config for bot tests."""
    bound_config = bind_runtime_paths(
        config,
        test_runtime_paths(runtime_root),
    )
    persist_entity_accounts(bound_config, runtime_paths_for(bound_config))
    return bound_config


def _fake_indexing_settings(base_id: str) -> IndexingSettings:
    return IndexingSettings(
        base_id=base_id,
        storage_root="storage",
        knowledge_path=f"knowledge/{base_id}",
        mode="semantic",
        embedder_provider="openai",
        embedder_model="text-embedding-3-small",
        embedder_host="",
        embedder_dimensions="",
        chunk_size="5000",
        chunk_overlap="0",
        repo_identity="",
        git_branch="",
        git_lfs="",
        git_skip_hidden="",
        git_include_patterns="",
        git_exclude_patterns="",
        include_patterns="",
        exclude_patterns="",
        include_extensions="",
        exclude_extensions="()",
    )


def _configured_team_test_config(runtime_root: Path) -> Config:
    """Return a runtime-bound config with one configured team for TeamBot tests."""
    return _runtime_bound_config(
        Config(
            agents={
                "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!test:localhost"]),
                "general": AgentConfig(display_name="GeneralAgent", rooms=["!test:localhost"]),
            },
            teams={
                "support_team": TeamConfig(
                    display_name="Support Team",
                    role="Coordinate test responses",
                    agents=["general"],
                    rooms=["!test:localhost"],
                ),
            },
            models={"default": ModelConfig(provider="test", id="test-model")},
            authorization=AuthorizationConfig(default_room_access=True),
        ),
        runtime_root,
    )


def _configured_team_user(config: Config, runtime_paths: RuntimePaths) -> AgentMatrixUser:
    """Return the Matrix user for the configured TeamBot test team."""
    team_name = "support_team"
    ids = entity_ids(config, runtime_paths)
    team_config = config.teams[team_name]
    return AgentMatrixUser(
        agent_name=team_name,
        user_id=ids[team_name].full_id,
        display_name=team_config.display_name or team_name,
        password=TEST_PASSWORD,
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
    store: _ApprovalManager,
    sender: AsyncMock,
    *,
    room_id: str = "!test:localhost",
) -> PendingApproval:
    async with asyncio.timeout(15):
        while True:
            if sender.await_args is not None:
                approval_id = sender.await_args.args[2]["approval_id"]
                card_event_id = store._live_card_event_id_for_approval(approval_id)
                if card_event_id is not None:
                    pending = await store._pending_approval_for_card(room_id=room_id, card_event_id=card_event_id)
                    if pending is not None:
                        return pending
            await asyncio.sleep(0)


async def _live_pending_approval(
    store: _ApprovalManager,
    *,
    room_id: str,
    approval_id: str,
) -> PendingApproval | None:
    card_event_id = store._live_card_event_id_for_approval(approval_id)
    if card_event_id is None:
        return None
    return await store._pending_approval_for_card(room_id=room_id, card_event_id=card_event_id)


async def _wait_for_pending_approval_id(store: _ApprovalManager, approval_ids: list[str]) -> str:
    async with asyncio.timeout(1):
        while True:
            if (
                approval_ids
                and await _live_pending_approval(store, room_id="!room:localhost", approval_id=approval_ids[0])
                is not None
            ):
                return approval_ids[0]
            await asyncio.sleep(0)


async def _start_live_approval(
    runtime_paths: RuntimePaths,
    *,
    approver_user_id: str = "@user:localhost",
    editor: AsyncMock | None = None,
    arguments: dict[str, Any] | None = None,
) -> tuple[_ApprovalManager, PendingApproval, asyncio.Task[Any], AsyncMock]:
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
        await _shutdown_approval_store()
        raise
    return store, pending, task, approval_editor


def _approval_removal_plan(new_config: Config) -> ConfigUpdatePlan:
    """Return one config-update plan that removes the code bot without restarting others."""
    return ConfigUpdatePlan(
        new_config=new_config,
        changed_mcp_servers=set(),
        configured_entities=set(),
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


def _hook_plugin(name: str, callbacks: list[object]) -> SimpleNamespace:
    """Create a minimal plugin stub for hook registry tests."""
    return SimpleNamespace(
        name=name,
        discovered_hooks=tuple(callbacks),
        entry_config=PluginEntryConfig(path=f"./plugins/{name}"),
        plugin_order=0,
    )


def _hook_envelope(
    *,
    body: str = "hello",
    source_event_id: str = "$event",
    room_id: str = "!test:localhost",
    thread_id: str | None = None,
    target: MessageTarget | None = None,
) -> MessageEnvelope:
    """Create a minimal response envelope for hook-aware bot tests."""
    resolved_target = target or MessageTarget.resolve(room_id, thread_id, source_event_id)
    return MessageEnvelope(
        source_event_id=source_event_id,
        target=resolved_target,
        body=body,
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="calculator",
        origin=message_origin(
            sender_id="@user:localhost",
            requester_id="@user:localhost",
            source_kind=MESSAGE_SOURCE_KIND,
        ),
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


def _attachment_record_stub(attachment_id: str, *, sender: str = "@user:localhost") -> AttachmentRecord:
    """Create a minimal attachment record for mocked media resolution."""
    return AttachmentRecord(
        attachment_id=attachment_id,
        local_path=Path(f"media/{attachment_id}.bin"),
        kind="image",
        sender=sender,
    )


_MediaKind = Literal["audio", "image", "file", "video"]


_MEDIA_MIME_TYPES: dict[_MediaKind, str] = {
    "audio": "audio/wav",
    "image": "image/png",
    "file": "text/plain",
    "video": "video/mp4",
}


def _payload_media_for_kind(payload: DispatchPayload, kind: _MediaKind) -> Sequence[Any]:
    return {
        "audio": payload.media.audio,
        "image": payload.media.images,
        "file": payload.media.files,
        "video": payload.media.videos,
    }[kind]


def _register_payload_media_attachment(
    storage_path: Path,
    *,
    kind: _MediaKind = "image",
    attachment_id: str,
    filename: str,
    content: bytes | None = None,
    local_path: Path | None = None,
    room_id: str = "!test:localhost",
    thread_id: str | None = "$thread",
) -> str:
    """Register one resolvable media attachment and return its local filepath."""
    media_path = local_path or storage_path / filename
    media_path.parent.mkdir(parents=True, exist_ok=True)
    if local_path is None:
        media_path.write_bytes(content or (b"media:" + attachment_id.encode("utf-8")))
    record = register_local_attachment(
        storage_path,
        media_path,
        kind=kind,
        attachment_id=attachment_id,
        filename=filename,
        mime_type=_MEDIA_MIME_TYPES[kind],
        room_id=room_id,
        thread_id=thread_id,
        source_event_id=f"${attachment_id}",
        sender="@user:localhost",
    )
    assert record is not None
    return str(record.local_path)


def _register_payload_image_attachment(
    storage_path: Path,
    *,
    attachment_id: str,
    filename: str,
    room_id: str = "!test:localhost",
    thread_id: str | None = "$thread",
) -> str:
    return _register_payload_media_attachment(
        storage_path,
        kind="image",
        attachment_id=attachment_id,
        filename=filename,
        content=b"\x89PNG\r\n\x1a\n" + attachment_id.encode("utf-8"),
        room_id=room_id,
        thread_id=thread_id,
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


def make_mock_agent_user() -> AgentMatrixUser:
    """Create a mock agent user."""
    return AgentMatrixUser(
        agent_name="calculator",
        password=TEST_PASSWORD,
        display_name="CalculatorAgent",
        user_id="@mindroom_calculator:localhost",
    )


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


class AgentBotTestBase:
    """Shared non-test helpers for the split TestAgentBot modules."""

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
