"""Test that direct audio responses preserve thread structure."""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from agno.media import Audio

from mindroom import inbound_turn_normalizer, interactive
from mindroom.attachments import _attachment_id_for_event, load_attachment
from mindroom.bot import AgentBot
from mindroom.coalescing import CoalescingGate
from mindroom.coalescing_batch import PendingEvent, build_coalesced_batch, close_pending_event_metadata
from mindroom.config.main import Config
from mindroom.constants import HOOK_MESSAGE_RECEIVED_DEPTH_KEY, HOOK_SOURCE_KEY, ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_handoff import (
    QUEUED_NOTICE_METADATA_KIND,
    DispatchHandoff,
    PendingDispatchMetadata,
    PreparedTextEvent,
)
from mindroom.dispatch_source import (
    ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
    HOOK_DISPATCH_SOURCE_KIND,
    HOOK_SOURCE_KIND,
    IMAGE_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    SCHEDULED_SOURCE_KIND,
    TEXT_COALESCING_CLASS,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    VOICE_COALESCING_CLASS,
    VOICE_SOURCE_KIND,
)
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.turn_ingress_coalescing import (
    BarrierReadyIngressResult,
    DropReadyIngressResult,
    IngressProvisionalKey,
    PromptReadyIngressResult,
    RawVoiceIngressItem,
    TurnIngressCoalescingGate,
    _target_key_for_non_voice_item,
)
from tests.conftest import (
    TEST_ACCESS_TOKEN,
    TEST_PASSWORD,
    admit_prepared_text_like_ingress_for_test,
    bind_runtime_paths,
    dispatch_context_result,
    drain_coalescing,
    install_generate_response_mock,
    install_runtime_cache_support,
    replace_turn_controller_deps,
    runtime_paths_for,
    sync_bot_runtime_state,
    test_runtime_paths,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.coalescing_batch import CoalescedBatch
    from mindroom.handled_turns import HandledTurnState
    from mindroom.hooks import MessageEnvelope


class VoiceNormalizationTestError(Exception):
    """Test-only STT failure."""


class VoiceFlushTestError(Exception):
    """Test-only voice flush failure."""


class VisibleEchoTestError(Exception):
    """Test-only visible echo failure."""


def _agent_bot(*, agent_user: AgentMatrixUser, storage_path: Path, config: Config, rooms: list[str]) -> AgentBot:
    """Construct an agent bot with the explicit runtime bound to the test config."""
    return install_runtime_cache_support(
        AgentBot(
            agent_user=agent_user,
            storage_path=storage_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
            rooms=rooms,
        ),
    )


@pytest.fixture
def mock_home_bot() -> AgentBot:
    """Create a single-agent bot for audio threading tests."""
    tmpdir = Path(tempfile.mkdtemp())
    agent_user = AgentMatrixUser(
        agent_name="home",
        user_id="@mindroom_home:localhost",
        display_name="HomeAssistant",
        password=TEST_PASSWORD,
        access_token=TEST_ACCESS_TOKEN,
    )
    config = Config(
        agents={"home": {"display_name": "HomeAssistant", "rooms": ["!test:server"]}},
        authorization={"default_room_access": True},
    )
    config = bind_runtime_paths(config, test_runtime_paths(tmpdir))
    bot = _agent_bot(agent_user=agent_user, storage_path=tmpdir, config=config, rooms=["!test:server"])
    wrap_extracted_collaborators(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_home:localhost"
    sync_bot_runtime_state(bot)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    bot._generate_response = AsyncMock(return_value="$response")
    install_generate_response_mock(bot, bot._generate_response)
    return bot


def _make_voice_event(
    *,
    event_id: str,
    source: dict,
    server_timestamp: int = 1_712_350_000_000,
) -> nio.RoomMessageAudio:
    voice_event = MagicMock(spec=nio.RoomMessageAudio)
    voice_event.event_id = event_id
    voice_event.sender = "@user:example.com"
    voice_event.body = "voice.ogg"
    voice_event.server_timestamp = server_timestamp
    voice_event.source = source
    return voice_event


def _make_threaded_voice_event(*, event_id: str, thread_id: str = "$thread_root") -> nio.RoomMessageAudio:
    return _make_voice_event(
        event_id=event_id,
        source={
            "event_id": event_id,
            "sender": "@user:example.com",
            "origin_server_ts": 1_712_350_000_000,
            "content": {
                "body": "voice.ogg",
                "msgtype": "m.audio",
                "m.relates_to": {"rel_type": "m.thread", "event_id": thread_id},
            },
        },
    )


def _threaded_prepared_text_event(
    *,
    event_id: str,
    body: str,
    thread_id: str = "$thread_root",
    sender: str = "@user:example.com",
    source_kind: str | None = None,
    content_overrides: dict[str, object] | None = None,
) -> PreparedTextEvent:
    content: dict[str, object] = {
        "body": body,
        "msgtype": "m.text",
        "m.relates_to": {"rel_type": "m.thread", "event_id": thread_id},
    }
    if source_kind is not None:
        content[SOURCE_KIND_KEY] = source_kind
    if content_overrides is not None:
        content.update(content_overrides)
    return PreparedTextEvent(
        sender=sender,
        event_id=event_id,
        body=body,
        source={
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": 1_712_350_000_000,
            "type": "m.room.message",
            "room_id": "!test:server",
            "content": content,
        },
        server_timestamp=1_712_350_000_000,
        source_kind_override=source_kind,
    )


def _threaded_text_event(
    *,
    event_id: str,
    body: str,
    thread_id: str = "$thread_root",
    sender: str = "@user:example.com",
    content_overrides: dict[str, object] | None = None,
) -> nio.RoomMessageText:
    content: dict[str, object] = {
        "body": body,
        "msgtype": "m.text",
        "m.relates_to": {"rel_type": "m.thread", "event_id": thread_id},
    }
    if content_overrides is not None:
        content.update(content_overrides)
    return cast(
        "nio.RoomMessageText",
        nio.RoomMessageText.from_dict(
            {
                "event_id": event_id,
                "sender": sender,
                "origin_server_ts": 1_712_350_000_000,
                "type": "m.room.message",
                "room_id": "!test:server",
                "content": content,
            },
        ),
    )


def _normalized_voice_result(
    *,
    event: nio.RoomMessageAudio,
    text: str,
    thread_id: str = "$thread_root",
) -> inbound_turn_normalizer.VoiceNormalizationResult:
    return inbound_turn_normalizer.VoiceNormalizationResult(
        event=PreparedTextEvent(
            sender=event.sender,
            event_id=event.event_id,
            body=text,
            source={
                "event_id": event.event_id,
                "sender": event.sender,
                "origin_server_ts": event.server_timestamp,
                "type": "m.room.message",
                "room_id": "!test:server",
                "content": {
                    "body": text,
                    "msgtype": "m.text",
                    SOURCE_KIND_KEY: VOICE_SOURCE_KIND,
                    "m.relates_to": {"rel_type": "m.thread", "event_id": thread_id},
                },
            },
            server_timestamp=event.server_timestamp,
            source_kind_override=VOICE_SOURCE_KIND,
        ),
        effective_thread_id=thread_id,
    )


async def _wait_until_claimed_ingress_voice_group_exists(
    gate: TurnIngressCoalescingGate,
    provisional_key: IngressProvisionalKey,
    event_id: str,
) -> None:
    for _ in range(100):
        claimed_groups = gate._ingress_claimed_voice_groups.get(provisional_key)
        if claimed_groups is not None and any(
            group.accepting_late_prompts
            and any(admission.ready_task.get_name() == f"voice_ready:{event_id}" for admission in group.items)
            for group in claimed_groups
        ):
            return
        await asyncio.sleep(0.001)
    pytest.fail("claimed ingress voice group was not live")


def _handled_source_event_ids(handled_turn: HandledTurnState | None) -> list[str]:
    return list(handled_turn.source_event_ids) if handled_turn is not None else []


def _threaded_room() -> MagicMock:
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:server"
    room.canonical_alias = None
    room.users = {
        "@mindroom_home:localhost": MagicMock(),
        "@user:example.com": MagicMock(),
    }
    room.members_synced = True
    return room


def _install_direct_ingress_capture_gates(
    *,
    debounce_seconds: float = 0.0,
    upload_grace_seconds: float = 0.0,
) -> tuple[TurnIngressCoalescingGate, CoalescingGate, list[CoalescedBatch]]:
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    coalescing_gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 60.0,
        is_shutting_down=lambda: False,
    )
    ingress_gate = TurnIngressCoalescingGate(
        debounce_seconds=lambda: debounce_seconds,
        upload_grace_seconds=lambda: upload_grace_seconds,
        is_shutting_down=lambda: False,
        coalescing_gate=coalescing_gate,
    )
    return ingress_gate, coalescing_gate, batches


async def _ready_ingress_result(
    result: object,
    *,
    release: asyncio.Event | None = None,
    error: BaseException | None = None,
) -> object:
    if release is not None:
        await release.wait()
    if error is not None:
        raise error
    return result


def _ready_task(
    result: object,
    *,
    release: asyncio.Event | None = None,
    error: BaseException | None = None,
) -> asyncio.Task[object]:
    return asyncio.create_task(_ready_ingress_result(result, release=release, error=error))


def _ingress_pending_event(
    *,
    room: nio.MatrixRoom,
    key: tuple[str, str | None, str],
    event_id: str,
    body: str,
    source_kind: str = MESSAGE_SOURCE_KIND,
    coalescing_class: str = TEXT_COALESCING_CLASS,
    dispatch_metadata: tuple[PendingDispatchMetadata, ...] = (),
) -> PendingEvent:
    return PendingEvent(
        event=_threaded_prepared_text_event(
            event_id=event_id,
            body=body,
            thread_id=key[1] or "$thread_root",
            sender=key[2],
            source_kind=source_kind,
        ),
        room=room,
        source_kind=source_kind,
        coalescing_class=coalescing_class,
        dispatch_metadata=dispatch_metadata,
    )


def _prompt_ready_result(
    *,
    room: nio.MatrixRoom,
    key: tuple[str, str | None, str],
    event_id: str,
    body: str,
    order: int,
    preliminary_key: tuple[str, str | None, str] | None = None,
    source_kind: str = MESSAGE_SOURCE_KIND,
    coalescing_class: str = TEXT_COALESCING_CLASS,
    dispatch_metadata: tuple[PendingDispatchMetadata, ...] = (),
) -> PromptReadyIngressResult:
    return PromptReadyIngressResult(
        pending_event=_ingress_pending_event(
            room=room,
            key=key,
            event_id=event_id,
            body=body,
            source_kind=source_kind,
            coalescing_class=coalescing_class,
            dispatch_metadata=dispatch_metadata,
        ),
        key=key,
        preliminary_key=preliminary_key or key,
        received_order=order,
        received_wall_time=float(order),
    )


def _barrier_ready_result(
    *,
    room: nio.MatrixRoom,
    key: tuple[str, str | None, str],
    event_id: str,
    order: int,
    source_kind: str = MESSAGE_SOURCE_KIND,
) -> BarrierReadyIngressResult:
    return BarrierReadyIngressResult(
        pending_event=_ingress_pending_event(
            room=room,
            key=key,
            event_id=event_id,
            body="!help",
            source_kind=source_kind,
        ),
        key=key,
        received_order=order,
        received_wall_time=float(order),
    )


def _raw_voice_ingress_item(
    *,
    room: nio.MatrixRoom,
    event_id: str,
    preliminary_key: tuple[str, str | None, str],
    ready_task: asyncio.Task[object],
    order: int,
) -> RawVoiceIngressItem:
    _ = (room, event_id, order)
    return RawVoiceIngressItem(
        preliminary_key_task=_ready_task(preliminary_key),
        ready_task=ready_task,
    )


async def _admit_prompt_result(
    gate: TurnIngressCoalescingGate,
    provisional_key: IngressProvisionalKey,
    result: PromptReadyIngressResult | BarrierReadyIngressResult | DropReadyIngressResult,
    *,
    barrier: bool = False,
    release: asyncio.Event | None = None,
    error: BaseException | None = None,
) -> None:
    source_kind = (
        result.pending_event.source_kind
        if isinstance(result, (PromptReadyIngressResult, BarrierReadyIngressResult))
        else None
    )
    await gate.admit_ready_task(
        provisional_key,
        ready_task=_ready_task(result, release=release, error=error),
        source_kind=source_kind,
        barrier=barrier,
    )


async def _drain_direct_ingress(
    ingress_gate: TurnIngressCoalescingGate,
    coalescing_gate: CoalescingGate,
) -> None:
    await ingress_gate.drain_all()
    await coalescing_gate.drain_all()


async def _wait_for_direct_condition(
    condition: Callable[[], bool],
    *,
    deadline_seconds: float = 0.5,
) -> None:
    for _ in range(max(int(deadline_seconds / 0.001), 1)):
        if condition():
            return
        await asyncio.sleep(0.001)
    pytest.fail("timed out waiting for direct ingress condition")


def _all_ingress_prompt_groups(gate: TurnIngressCoalescingGate) -> tuple[object, ...]:
    return (
        *gate._ingress_open_groups.values(),
        *gate._ingress_grace_groups.values(),
        *(
            claimed_group
            for claimed_groups in gate._ingress_claimed_voice_groups.values()
            for claimed_group in claimed_groups
        ),
    )


async def _wait_for_voice_ready_admission(
    gate: TurnIngressCoalescingGate,
    event_id: str,
    *,
    deadline_seconds: float = 0.5,
) -> None:
    await _wait_for_direct_condition(
        lambda: any(
            admission.ready_task.get_name() == f"voice_ready:{event_id}"
            for group in _all_ingress_prompt_groups(gate)
            for admission in group.items
        ),
        deadline_seconds=deadline_seconds,
    )


def _batch_ids_by_key(batches: list[CoalescedBatch]) -> dict[tuple[str, str | None, str], list[list[str]]]:
    ids_by_key: dict[tuple[str, str | None, str], list[list[str]]] = {}
    for batch in batches:
        ids_by_key.setdefault(batch.coalescing_key, []).append(batch.source_event_ids)
    return ids_by_key


def _install_streaming_test_gate(bot: AgentBot) -> None:
    ingress_gate = TurnIngressCoalescingGate(
        debounce_seconds=lambda: 0.01,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    gate = CoalescingGate(
        dispatch_batch=bot._dispatch_coalesced_batch,
        debounce_seconds=lambda: 0.01,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate, turn_ingress_gate=ingress_gate)
    bot._turn_controller.deps.response_runner.has_active_response_for_target = MagicMock(return_value=True)
    bot._turn_controller.deps.response_runner.reserve_waiting_human_message = MagicMock(return_value=None)


def _staggered_voice_normalizer() -> tuple[dict[str, asyncio.Event], dict[str, asyncio.Event], AsyncMock]:
    started = {"$voice1": asyncio.Event(), "$voice2": asyncio.Event()}
    releases = {"$voice1": asyncio.Event(), "$voice2": asyncio.Event()}

    async def prepare_voice_event(
        request: inbound_turn_normalizer.VoiceNormalizationRequest,
    ) -> inbound_turn_normalizer.VoiceNormalizationResult:
        started[request.event.event_id].set()
        await releases[request.event.event_id].wait()
        return _normalized_voice_result(
            event=request.event,
            text=f"transcript for {request.event.event_id}",
        )

    return started, releases, AsyncMock(side_effect=prepare_voice_event)


def _streaming_dispatch_recorder() -> tuple[asyncio.Event, asyncio.Event, list[tuple[list[str], str]], AsyncMock]:
    streaming_started = asyncio.Event()
    release_streaming = asyncio.Event()
    dispatches: list[tuple[list[str], str]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: PreparedTextEvent | nio.RoomMessageText,
        _requester_user_id: str,
        *,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        source_event_ids = _handled_source_event_ids(handled_turn)
        dispatches.append((source_event_ids, dispatched_event.body))
        if source_event_ids == ["$streaming"]:
            streaming_started.set()
            await release_streaming.wait()

    return streaming_started, release_streaming, dispatches, AsyncMock(side_effect=record_dispatch)


async def _enqueue_ready_streaming_event(
    bot: AgentBot,
    room: nio.MatrixRoom,
    event: PreparedTextEvent,
    key: tuple[str, str | None, str],
) -> None:
    """Seed the downstream gate with an already-ready active response fixture."""
    await bot._turn_controller.deps.coalescing_gate.enqueue(
        key,
        PendingEvent(
            event=event,
            room=room,
            source_kind=MESSAGE_SOURCE_KIND,
            coalescing_class=TEXT_COALESCING_CLASS,
        ),
    )


def _install_active_batch_capture_gates(
    bot: AgentBot,
    *,
    debounce_seconds: float = 0.02,
    voice_debounce_seconds: float = 0.01,
    dispatch_event: asyncio.Event | None = None,
) -> tuple[TurnIngressCoalescingGate, CoalescingGate, list[CoalescedBatch]]:
    ingress_gate = TurnIngressCoalescingGate(
        debounce_seconds=lambda: voice_debounce_seconds,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)
        if dispatch_event is not None:
            dispatch_event.set()

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: debounce_seconds,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate, turn_ingress_gate=ingress_gate)
    bot._turn_controller.deps.response_runner.has_active_response_for_target = MagicMock(return_value=True)
    bot._turn_controller.deps.response_runner.reserve_waiting_human_message = MagicMock(return_value=None)
    return ingress_gate, gate, batches


def _assert_active_voice_batch(
    batches: list[CoalescedBatch],
    expected_ids: list[str],
) -> None:
    assert len(batches) == 1
    assert batches[0].source_event_ids == expected_ids
    assert batches[0].dispatch_policy_source_kind == ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND
    assert batches[0].coalescing_class == VOICE_COALESCING_CLASS


def _assert_voice_barrier_batches(
    batches: list[CoalescedBatch],
    barrier_event_id: str,
    voice_event_id: str,
) -> None:
    assert [batch.source_event_ids for batch in batches] == [[barrier_event_id], [voice_event_id]]
    assert batches[1].source_kind == VOICE_SOURCE_KIND
    assert batches[1].coalescing_class == VOICE_COALESCING_CLASS


def _stub_resolve_dispatch_target(bot: AgentBot, thread_id: str | None, event_id: str) -> None:
    """Stub bounded voice target resolution for direct voice threading tests."""
    unwrap_extracted_collaborator(bot._conversation_resolver).resolve_dispatch_target = AsyncMock(
        return_value=MessageTarget.resolve("!test:server", thread_id, event_id),
    )


@pytest.mark.asyncio
async def test_voice_message_in_main_room_creates_thread(mock_home_bot: AgentBot) -> None:
    """Audio in the main room should reply in a thread rooted at the audio event."""
    bot = mock_home_bot
    _stub_resolve_dispatch_target(bot, None, "$voice123")
    mock_context = MessageContext(
        am_i_mentioned=False,
        is_thread=True,
        thread_id="$voice123",
        thread_history=[],
        mentioned_agents=[],
        has_non_agent_mentions=False,
        requires_model_history_refresh=False,
    )
    bot._conversation_resolver.extract_dispatch_context = AsyncMock(
        return_value=dispatch_context_result(mock_context),
    )

    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:server"
    room.canonical_alias = None
    room.users = {
        "@mindroom_home:localhost": MagicMock(),
        "@user:example.com": MagicMock(),
    }
    room.members_synced = True

    voice_event = _make_voice_event(event_id="$voice123", source={"content": {}})

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", return_value="🎤 what is the weather"),
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        await bot._on_media_message(room, voice_event)
        await drain_coalescing(bot)

    bot._generate_response.assert_called_once()
    call_kwargs = bot._generate_response.call_args.kwargs
    response_target = call_kwargs["response_envelope"].target
    assert response_target.reply_to_event_id == "$voice123"
    assert response_target.resolved_thread_id == "$voice123"
    assert call_kwargs["prompt"].startswith("🎤 what is the weather")


@pytest.mark.asyncio
async def test_voice_message_in_thread_continues_thread(mock_home_bot: AgentBot) -> None:
    """Audio in an existing thread should keep using that thread root."""
    bot = mock_home_bot
    _stub_resolve_dispatch_target(bot, "$thread_root", "$voice456")
    mock_context = MessageContext(
        am_i_mentioned=False,
        is_thread=True,
        thread_id="$thread_root",
        thread_history=[],
        mentioned_agents=[],
        has_non_agent_mentions=False,
        requires_model_history_refresh=False,
    )
    bot._conversation_resolver.extract_dispatch_context = AsyncMock(
        return_value=dispatch_context_result(mock_context),
    )

    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:server"
    room.canonical_alias = None
    room.users = {
        "@mindroom_home:localhost": MagicMock(),
        "@user:example.com": MagicMock(),
    }
    room.members_synced = True

    voice_event = _make_voice_event(
        event_id="$voice456",
        source={
            "content": {
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        },
    )

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", return_value="🎤 show me the forecast"),
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        await bot._on_media_message(room, voice_event)
        await drain_coalescing(bot)

    bot._generate_response.assert_called_once()
    call_kwargs = bot._generate_response.call_args.kwargs
    response_target = call_kwargs["response_envelope"].target
    assert response_target.reply_to_event_id == "$voice456"
    assert response_target.resolved_thread_id == "$thread_root"
    assert call_kwargs["prompt"].startswith("🎤 show me the forecast")
    attachment = load_attachment(bot.storage_path, _attachment_id_for_event("$voice456"))
    assert attachment is not None
    assert attachment.thread_id == "$thread_root"


@pytest.mark.asyncio
async def test_voice_plain_reply_to_thread_message_stays_threaded_transitively(
    mock_home_bot: AgentBot,
) -> None:
    """Plain-reply audio should inherit thread context transitively from the replied-to event."""
    bot = mock_home_bot
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:server"
    room.canonical_alias = None
    room.users = {
        "@mindroom_home:localhost": MagicMock(),
        "@user:example.com": MagicMock(),
    }
    room.members_synced = True

    voice_event = _make_voice_event(
        event_id="$voice789",
        source={"content": {"m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg"}}}},
    )

    _stub_resolve_dispatch_target(bot, "$thread_root", "$voice789")
    mock_context = MessageContext(
        am_i_mentioned=False,
        is_thread=True,
        thread_id="$thread_root",
        thread_history=[],
        mentioned_agents=[],
        has_non_agent_mentions=False,
        requires_model_history_refresh=False,
    )
    bot._conversation_resolver.extract_dispatch_context = AsyncMock(
        return_value=dispatch_context_result(mock_context),
    )

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", return_value="🎤 continue the same thread"),
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        await bot._on_media_message(room, voice_event)
        await drain_coalescing(bot)

    bot._generate_response.assert_called_once()
    call_kwargs = bot._generate_response.call_args.kwargs
    response_target = call_kwargs["response_envelope"].target
    assert response_target.reply_to_event_id == "$voice789"
    assert response_target.resolved_thread_id == "$thread_root"
    assert call_kwargs["prompt"].startswith("🎤 continue the same thread")
    attachment = load_attachment(bot.storage_path, _attachment_id_for_event("$voice789"))
    assert attachment is not None
    assert attachment.thread_id == "$thread_root"


@pytest.mark.asyncio
async def test_voice_message_reserves_preliminary_active_turn_before_stt(mock_home_bot: AgentBot) -> None:
    """Audio follow-ups should reserve preliminary active ownership while transcription is pending."""
    bot = mock_home_bot
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:server"
    room.canonical_alias = None
    room.users = {
        "@mindroom_home:localhost": MagicMock(),
        "@user:example.com": MagicMock(),
    }
    room.members_synced = True

    voice_event = _make_voice_event(
        event_id="$voice-blocked",
        source={
            "content": {
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        },
    )
    target = bot._turn_controller.deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id="$thread_root",
        reply_to_event_id=voice_event.event_id,
        event_source=voice_event.source,
    )
    lifecycle = unwrap_extracted_collaborator(bot._response_runner)._lifecycle_coordinator
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    prepare_started = asyncio.Event()
    allow_prepare = asyncio.Event()

    async def prepare_voice_event(*_args: object, **_kwargs: object) -> None:
        prepare_started.set()
        await allow_prepare.wait()

    queued_signal.begin_response_turn()
    task: asyncio.Task[None] | None = None
    try:
        with (
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$thread_root"),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=AsyncMock(side_effect=prepare_voice_event),
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            task = asyncio.create_task(bot._on_media_message(room, voice_event))
            await asyncio.wait_for(prepare_started.wait(), timeout=0.2)
            assert queued_signal.pending_human_messages == 1
            assert queued_signal.is_set()
            allow_prepare.set()
            await task
            await bot._turn_controller.deps.turn_ingress_gate.drain_all()
    finally:
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        queued_signal.finish_response_turn()

    assert queued_signal.pending_human_messages == 0
    assert not queued_signal.is_set()


@pytest.mark.asyncio
async def test_voice_normalization_error_does_not_mark_audio_source_handled(mock_home_bot: AgentBot) -> None:
    """Failed raw voice normalization should remain replayable."""
    bot = mock_home_bot
    room = _threaded_room()
    voice_event = _make_threaded_voice_event(event_id="$voice-error")

    async def fail_prepare_voice_event(*_args: object, **_kwargs: object) -> None:
        raise VoiceNormalizationTestError

    with (
        patch.object(
            bot._turn_controller.deps.resolver,
            "coalescing_thread_id",
            new=AsyncMock(return_value="$thread_root"),
        ),
        patch.object(
            bot._turn_controller.deps.normalizer,
            "prepare_voice_event",
            new=AsyncMock(side_effect=fail_prepare_voice_event),
        ),
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
    ):
        await bot._on_media_message(room, voice_event)
        await bot._turn_controller.deps.turn_ingress_gate.drain_all()

    assert not bot._turn_store.is_handled("$voice-error")


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_cancels_unresolved_turn_ingress_ready_task(mock_home_bot: AgentBot) -> None:
    """Sync shutdown should not block forever on unresolved receive-time normalization."""
    room = _threaded_room()
    ingress_gate = mock_home_bot._turn_ingress_gate
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")
    release_ready_task = asyncio.Event()
    ready_task = _ready_task(
        _prompt_ready_result(room=room, key=key, event_id="$text", body="pending text", order=1),
        release=release_ready_task,
    )

    await ingress_gate.admit_ready_task(
        provisional_key,
        ready_task=ready_task,
        source_kind=MESSAGE_SOURCE_KIND,
        barrier=False,
    )
    await _wait_for_direct_condition(
        lambda: (
            provisional_key in ingress_gate._ingress_open_groups
            or any(not task.done() for task in ingress_gate._ingress_drain_tasks)
        ),
    )

    try:
        await asyncio.wait_for(mock_home_bot.prepare_for_sync_shutdown(), timeout=2.0)
    finally:
        if not ready_task.done():
            ready_task.cancel()
            with suppress(asyncio.CancelledError):
                await ready_task

    assert ready_task.cancelled()


@pytest.mark.asyncio
async def test_receive_time_coordinator_late_text_joins_newer_claimed_voice_burst() -> None:
    """A typed follow-up after two claimed voice bursts should belong to the newer burst."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(debounce_seconds=0.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    first_key = (room.room_id, "$voice-thread-1", "@user:example.com")
    second_key = (room.room_id, "$voice-thread-2", "@user:example.com")
    release_first = asyncio.Event()
    release_second = asyncio.Event()

    await ingress_gate.admit_raw_voice(
        provisional_key,
        _raw_voice_ingress_item(
            room=room,
            event_id="$voice1",
            preliminary_key=first_key,
            ready_task=_ready_task(
                _prompt_ready_result(
                    room=room,
                    key=first_key,
                    event_id="$voice1",
                    body="first voice",
                    order=1,
                    source_kind=VOICE_SOURCE_KIND,
                    coalescing_class=VOICE_COALESCING_CLASS,
                ),
                release=release_first,
            ),
            order=1,
        ),
    )
    await asyncio.sleep(0)
    await ingress_gate.admit_raw_voice(
        provisional_key,
        _raw_voice_ingress_item(
            room=room,
            event_id="$voice2",
            preliminary_key=second_key,
            ready_task=_ready_task(
                _prompt_ready_result(
                    room=room,
                    key=second_key,
                    event_id="$voice2",
                    body="second voice",
                    order=2,
                    source_kind=VOICE_SOURCE_KIND,
                    coalescing_class=VOICE_COALESCING_CLASS,
                ),
                release=release_second,
            ),
            order=2,
        ),
    )
    await asyncio.sleep(0)
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(
            room=room,
            key=second_key,
            event_id="$typed",
            body="typed after newer voice",
            order=3,
        ),
    )

    release_first.set()
    release_second.set()
    await _drain_direct_ingress(ingress_gate, coalescing_gate)

    source_id_sets = [set(batch.source_event_ids) for batch in batches]
    assert {"$voice1"} in source_id_sets
    assert {"$voice2", "$typed"} in source_id_sets
    assert all("$typed" not in ids or "$voice1" not in ids for ids in source_id_sets)


@pytest.mark.asyncio
async def test_receive_time_admission_order_overrides_ready_result_metadata_and_completion_order() -> None:
    """Coordinator-owned admission order should beat misleading ready-result metadata."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(debounce_seconds=0.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")
    release_voice = asyncio.Event()
    release_text = asyncio.Event()
    release_image = asyncio.Event()

    await ingress_gate.admit_raw_voice(
        provisional_key,
        _raw_voice_ingress_item(
            room=room,
            event_id="$voice",
            preliminary_key=key,
            ready_task=_ready_task(
                _prompt_ready_result(
                    room=room,
                    key=key,
                    event_id="$voice",
                    body="first by admission, last by completion",
                    order=90,
                    source_kind=VOICE_SOURCE_KIND,
                    coalescing_class=VOICE_COALESCING_CLASS,
                ),
                release=release_voice,
            ),
            order=900,
        ),
    )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$text", body="second by admission", order=2),
        release=release_text,
    )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(
            room=room,
            key=key,
            event_id="$image",
            body="third by admission, first by result metadata",
            order=1,
            source_kind=IMAGE_SOURCE_KIND,
        ),
        release=release_image,
    )

    release_image.set()
    release_text.set()
    await asyncio.sleep(0)
    assert batches == []

    release_voice.set()
    await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert [batch.source_event_ids for batch in batches] == [["$voice", "$text", "$image"]]


@pytest.mark.asyncio
async def test_receive_time_coordinator_retargets_text_to_deduped_successful_voice_key() -> None:
    """Two successful voices resolving to the same final key should pull captured text into one sealed batch."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(debounce_seconds=0.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    preliminary_key = (room.room_id, "$preliminary", "@user:example.com")
    final_key = (room.room_id, "$final", "@user:example.com")

    for order, event_id in [(1, "$voice1"), (2, "$voice2")]:
        await ingress_gate.admit_raw_voice(
            provisional_key,
            _raw_voice_ingress_item(
                room=room,
                event_id=event_id,
                preliminary_key=preliminary_key,
                ready_task=_ready_task(
                    _prompt_ready_result(
                        room=room,
                        key=final_key,
                        preliminary_key=preliminary_key,
                        event_id=event_id,
                        body=f"voice {order}",
                        order=order,
                        source_kind=VOICE_SOURCE_KIND,
                        coalescing_class=VOICE_COALESCING_CLASS,
                    ),
                ),
                order=order,
            ),
        )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(
            room=room,
            key=preliminary_key,
            preliminary_key=preliminary_key,
            event_id="$typed",
            body="captured text",
            order=3,
        ),
    )
    await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert [batch.source_event_ids for batch in batches] == [["$voice1", "$voice2", "$typed"]]
    assert batches[0].coalescing_key == final_key
    assert batches[0].pending_events[-1].coalescing_class == VOICE_COALESCING_CLASS


@pytest.mark.asyncio
async def test_receive_time_retarget_closes_stripped_queued_notice_metadata() -> None:
    """Retargeted non-voice items must close metadata removed from the sealed voice batch."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(debounce_seconds=0.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    preliminary_key = (room.room_id, "$preliminary", "@user:example.com")
    final_key = (room.room_id, "$final", "@user:example.com")
    reservation = MagicMock()

    await ingress_gate.admit_raw_voice(
        provisional_key,
        _raw_voice_ingress_item(
            room=room,
            event_id="$voice",
            preliminary_key=preliminary_key,
            ready_task=_ready_task(
                _prompt_ready_result(
                    room=room,
                    key=final_key,
                    preliminary_key=preliminary_key,
                    event_id="$voice",
                    body="voice final",
                    order=1,
                    source_kind=VOICE_SOURCE_KIND,
                    coalescing_class=VOICE_COALESCING_CLASS,
                ),
            ),
            order=1,
        ),
    )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(
            room=room,
            key=preliminary_key,
            preliminary_key=preliminary_key,
            event_id="$typed",
            body="captured text",
            order=2,
            dispatch_metadata=(
                PendingDispatchMetadata(
                    kind=QUEUED_NOTICE_METADATA_KIND,
                    payload=reservation,
                    close=reservation.cancel,
                    requires_solo_batch=True,
                ),
            ),
        ),
    )
    await _drain_direct_ingress(ingress_gate, coalescing_gate)

    reservation.cancel.assert_called_once_with()
    assert [batch.source_event_ids for batch in batches] == [["$voice", "$typed"]]
    retargeted_text = next(
        pending_event for pending_event in batches[0].pending_events if pending_event.event.event_id == "$typed"
    )
    assert retargeted_text.dispatch_metadata == ()


@pytest.mark.asyncio
async def test_late_prompt_during_claimed_voice_dispatch_starts_next_group() -> None:
    """A prompt received after a voice group is sealed must not append into a closed snapshot."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(debounce_seconds=0.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    preliminary_key = (room.room_id, "$preliminary", "@user:example.com")
    final_key = (room.room_id, "$final", "@user:example.com")
    original_enqueue_sealed_batch = coalescing_gate.enqueue_sealed_batch
    first_enqueue_started = asyncio.Event()
    release_first_enqueue = asyncio.Event()

    async def enqueue_sealed_batch(
        key: tuple[str, str | None, str],
        pending_events: list[PendingEvent],
    ) -> None:
        if not first_enqueue_started.is_set():
            first_enqueue_started.set()
            await release_first_enqueue.wait()
        await original_enqueue_sealed_batch(key, pending_events)

    coalescing_gate.enqueue_sealed_batch = AsyncMock(side_effect=enqueue_sealed_batch)

    await ingress_gate.admit_raw_voice(
        provisional_key,
        _raw_voice_ingress_item(
            room=room,
            event_id="$voice",
            preliminary_key=preliminary_key,
            ready_task=_ready_task(
                _prompt_ready_result(
                    room=room,
                    key=final_key,
                    preliminary_key=preliminary_key,
                    event_id="$voice",
                    body="voice final",
                    order=1,
                    source_kind=VOICE_SOURCE_KIND,
                    coalescing_class=VOICE_COALESCING_CLASS,
                ),
            ),
            order=1,
        ),
    )
    await asyncio.wait_for(first_enqueue_started.wait(), timeout=1.0)
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(
            room=room,
            key=preliminary_key,
            preliminary_key=preliminary_key,
            event_id="$typed",
            body="late text",
            order=2,
        ),
    )

    release_first_enqueue.set()
    await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert [batch.source_event_ids for batch in batches] == [["$voice"], ["$typed"]]


@pytest.mark.asyncio
async def test_known_barrier_waits_for_older_prompt_group_to_enqueue() -> None:
    """A command/barrier must not overtake an earlier prompt in the same receive key."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, _batches = _install_direct_ingress_capture_gates(debounce_seconds=0.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread", "@user:example.com")
    enqueue_order: list[list[str]] = []
    original_enqueue_sealed_batch = coalescing_gate.enqueue_sealed_batch
    original_enqueue = coalescing_gate.enqueue

    async def enqueue_sealed_batch(
        sealed_key: tuple[str, str | None, str],
        pending_events: list[PendingEvent],
    ) -> None:
        enqueue_order.append([pending_event.event.event_id for pending_event in pending_events])
        await original_enqueue_sealed_batch(sealed_key, pending_events)

    async def enqueue(
        barrier_key: tuple[str, str | None, str],
        pending_event: PendingEvent,
    ) -> None:
        enqueue_order.append([pending_event.event.event_id])
        await original_enqueue(barrier_key, pending_event)

    coalescing_gate.enqueue_sealed_batch = AsyncMock(side_effect=enqueue_sealed_batch)
    coalescing_gate.enqueue = AsyncMock(side_effect=enqueue)

    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$text", body="text", order=1),
    )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _barrier_ready_result(room=room, key=key, event_id="$command", order=2),
        barrier=True,
    )
    await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert enqueue_order == [["$text"], ["$command"]]


@pytest.mark.asyncio
async def test_known_barrier_without_existing_group_blocks_later_prompt_until_ready() -> None:
    """A delayed command/barrier received first must stay before later prompts."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(debounce_seconds=0.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread", "@user:example.com")
    release_barrier = asyncio.Event()

    barrier_task = asyncio.create_task(
        _admit_prompt_result(
            ingress_gate,
            provisional_key,
            _barrier_ready_result(room=room, key=key, event_id="$command", order=1),
            barrier=True,
            release=release_barrier,
        ),
    )
    try:
        await asyncio.sleep(0)
        await _admit_prompt_result(
            ingress_gate,
            provisional_key,
            _prompt_ready_result(room=room, key=key, event_id="$text", body="text", order=2),
        )
        await asyncio.sleep(0.02)
        await coalescing_gate.drain_all()

        assert batches == []

        release_barrier.set()
        await asyncio.wait_for(barrier_task, timeout=1.0)
        await _drain_direct_ingress(ingress_gate, coalescing_gate)
    finally:
        release_barrier.set()
        if not barrier_task.done():
            barrier_task.cancel()
        with suppress(asyncio.CancelledError):
            await barrier_task

    assert [batch.source_event_ids for batch in batches] == [["$command"], ["$text"]]


@pytest.mark.asyncio
async def test_known_barriers_preserve_receive_order_when_older_barrier_is_delayed() -> None:
    """A later command/barrier must not pass an older command/barrier that is still resolving."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(debounce_seconds=0.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread", "@user:example.com")
    release_first_barrier = asyncio.Event()
    second_barrier_task: asyncio.Task[None] | None = None

    first_barrier_task = asyncio.create_task(
        _admit_prompt_result(
            ingress_gate,
            provisional_key,
            _barrier_ready_result(room=room, key=key, event_id="$command1", order=1),
            barrier=True,
            release=release_first_barrier,
        ),
    )
    try:
        await asyncio.sleep(0)
        second_barrier_task = asyncio.create_task(
            _admit_prompt_result(
                ingress_gate,
                provisional_key,
                _barrier_ready_result(room=room, key=key, event_id="$command2", order=2),
                barrier=True,
            ),
        )
        await asyncio.sleep(0.02)
        await coalescing_gate.drain_all()

        assert batches == []
        assert not second_barrier_task.done()

        release_first_barrier.set()
        await asyncio.wait_for(first_barrier_task, timeout=1.0)
        await asyncio.wait_for(second_barrier_task, timeout=1.0)
        await _drain_direct_ingress(ingress_gate, coalescing_gate)
    finally:
        release_first_barrier.set()
        for task in (first_barrier_task, second_barrier_task):
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await task

    assert [batch.source_event_ids for batch in batches] == [["$command1"], ["$command2"]]


@pytest.mark.asyncio
async def test_known_barrier_flushes_ready_open_group_before_older_unresolved_group() -> None:
    """Barrier ordering is based on receive order, not internal group container order."""
    room = _threaded_room()
    dispatched: list[list[str]] = []
    debounce_seconds = 0.0
    downstream_gate = MagicMock()

    async def enqueue_sealed_batch(_key: tuple[str, str | None, str], pending_events: list[PendingEvent]) -> None:
        dispatched.append([pending_event.event.event_id for pending_event in pending_events])

    async def enqueue(_key: tuple[str, str | None, str], pending_event: PendingEvent) -> None:
        dispatched.append([pending_event.event.event_id])

    downstream_gate.enqueue_sealed_batch = AsyncMock(side_effect=enqueue_sealed_batch)
    downstream_gate.enqueue = AsyncMock(side_effect=enqueue)
    ingress_gate = TurnIngressCoalescingGate(
        debounce_seconds=lambda: debounce_seconds,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
        coalescing_gate=downstream_gate,
    )
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread", "@user:example.com")
    release_slow_text = asyncio.Event()

    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$slow", body="slow text", order=1),
        release=release_slow_text,
    )
    await _wait_for_direct_condition(lambda: bool(ingress_gate._ingress_draining_groups.get(provisional_key)))

    debounce_seconds = 60.0
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$ready", body="ready text", order=2),
    )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _barrier_ready_result(room=room, key=key, event_id="$command", order=3),
        barrier=True,
    )

    assert dispatched == [["$ready"], ["$command"]]

    release_slow_text.set()
    await ingress_gate.drain_all()
    assert dispatched == [["$ready"], ["$command"], ["$slow"]]


@pytest.mark.asyncio
async def test_known_barrier_waits_for_ready_predecessor_group_before_open_group() -> None:
    """A barrier must not overtake a ready predecessor group already enqueueing downstream."""
    room = _threaded_room()
    dispatched: list[list[str]] = []
    debounce_seconds = 0.0
    first_enqueue_started = asyncio.Event()
    release_first_enqueue = asyncio.Event()
    downstream_gate = MagicMock()

    async def enqueue_sealed_batch(_key: tuple[str, str | None, str], pending_events: list[PendingEvent]) -> None:
        event_ids = [pending_event.event.event_id for pending_event in pending_events]
        dispatched.append(event_ids)
        if event_ids == ["$old"]:
            first_enqueue_started.set()
            await release_first_enqueue.wait()

    async def enqueue(_key: tuple[str, str | None, str], pending_event: PendingEvent) -> None:
        dispatched.append([pending_event.event.event_id])

    downstream_gate.enqueue_sealed_batch = AsyncMock(side_effect=enqueue_sealed_batch)
    downstream_gate.enqueue = AsyncMock(side_effect=enqueue)
    ingress_gate = TurnIngressCoalescingGate(
        debounce_seconds=lambda: debounce_seconds,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
        coalescing_gate=downstream_gate,
    )
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread", "@user:example.com")

    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$old", body="old text", order=1),
    )
    await asyncio.wait_for(first_enqueue_started.wait(), timeout=1.0)

    debounce_seconds = 60.0
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$ready", body="ready text", order=2),
    )
    barrier_task = asyncio.create_task(
        _admit_prompt_result(
            ingress_gate,
            provisional_key,
            _barrier_ready_result(room=room, key=key, event_id="$command", order=3),
            barrier=True,
        ),
    )
    await asyncio.sleep(0.02)

    assert dispatched == [["$old"]]
    assert not barrier_task.done()

    release_first_enqueue.set()
    await asyncio.wait_for(barrier_task, timeout=1.0)
    await ingress_gate.drain_all()

    assert dispatched == [["$old"], ["$ready"], ["$command"]]


@pytest.mark.asyncio
async def test_receive_time_coordinator_partitions_same_room_requester_by_preliminary_thread() -> None:
    """A voice in one preliminary thread must not retarget text from another preliminary thread."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(debounce_seconds=0.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    text_key = (room.room_id, "$text-thread", "@user:example.com")
    voice_preliminary_key = (room.room_id, "$voice-preliminary", "@user:example.com")
    voice_final_key = (room.room_id, "$voice-final", "@user:example.com")

    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=text_key, event_id="$typed", body="thread text", order=1),
    )
    await ingress_gate.admit_raw_voice(
        provisional_key,
        _raw_voice_ingress_item(
            room=room,
            event_id="$voice",
            preliminary_key=voice_preliminary_key,
            ready_task=_ready_task(
                _prompt_ready_result(
                    room=room,
                    key=voice_final_key,
                    preliminary_key=voice_preliminary_key,
                    event_id="$voice",
                    body="voice final",
                    order=2,
                    source_kind=VOICE_SOURCE_KIND,
                    coalescing_class=VOICE_COALESCING_CLASS,
                ),
            ),
            order=2,
        ),
    )
    await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert _batch_ids_by_key(batches) == {
        text_key: [["$typed"]],
        voice_final_key: [["$voice"]],
    }


@pytest.mark.asyncio
async def test_receive_time_barrier_dispatches_before_pending_raw_voice_stt_finishes() -> None:
    """A known barrier should not wait for unrelated pending STT in an earlier prompt group."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(debounce_seconds=60.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")
    release_voice = asyncio.Event()

    await ingress_gate.admit_raw_voice(
        provisional_key,
        _raw_voice_ingress_item(
            room=room,
            event_id="$voice",
            preliminary_key=key,
            ready_task=_ready_task(
                _prompt_ready_result(
                    room=room,
                    key=key,
                    event_id="$voice",
                    body="voice transcript",
                    order=1,
                    source_kind=VOICE_SOURCE_KIND,
                    coalescing_class=VOICE_COALESCING_CLASS,
                ),
                release=release_voice,
            ),
            order=1,
        ),
    )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _barrier_ready_result(room=room, key=key, event_id="$command", order=2),
        barrier=True,
    )
    await coalescing_gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$command"]]

    release_voice.set()
    await _drain_direct_ingress(ingress_gate, coalescing_gate)
    assert [batch.source_event_ids for batch in batches] == [["$command"], ["$voice"]]


@pytest.mark.asyncio
async def test_receive_time_repeated_barriers_do_not_wait_for_pending_raw_voice_stt() -> None:
    """A later command should not block on a voice group already split by an earlier command."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(debounce_seconds=60.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")
    release_voice = asyncio.Event()

    await ingress_gate.admit_raw_voice(
        provisional_key,
        _raw_voice_ingress_item(
            room=room,
            event_id="$voice",
            preliminary_key=key,
            ready_task=_ready_task(
                _prompt_ready_result(
                    room=room,
                    key=key,
                    event_id="$voice",
                    body="voice transcript",
                    order=1,
                    source_kind=VOICE_SOURCE_KIND,
                    coalescing_class=VOICE_COALESCING_CLASS,
                ),
                release=release_voice,
            ),
            order=1,
        ),
    )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _barrier_ready_result(room=room, key=key, event_id="$command1", order=2),
        barrier=True,
    )
    await coalescing_gate.drain_all()
    assert [batch.source_event_ids for batch in batches] == [["$command1"]]

    second_barrier_task = asyncio.create_task(
        _admit_prompt_result(
            ingress_gate,
            provisional_key,
            _barrier_ready_result(room=room, key=key, event_id="$command2", order=3),
            barrier=True,
        ),
    )
    try:
        await asyncio.sleep(0.02)
        await coalescing_gate.drain_all()
        assert [batch.source_event_ids for batch in batches] == [["$command1"], ["$command2"]]
        assert second_barrier_task.done()
    finally:
        release_voice.set()
        await second_barrier_task
        await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert [batch.source_event_ids for batch in batches] == [["$command1"], ["$command2"], ["$voice"]]


@pytest.mark.asyncio
async def test_receive_time_barrier_flushes_ready_prefix_from_pending_closed_group() -> None:
    """A command after text+pending voice should flush the ready text without waiting for STT."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(debounce_seconds=0.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")
    release_voice = asyncio.Event()

    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$text", body="text first", order=1),
    )
    await ingress_gate.admit_raw_voice(
        provisional_key,
        _raw_voice_ingress_item(
            room=room,
            event_id="$voice",
            preliminary_key=key,
            ready_task=_ready_task(
                _prompt_ready_result(
                    room=room,
                    key=key,
                    event_id="$voice",
                    body="voice transcript",
                    order=2,
                    source_kind=VOICE_SOURCE_KIND,
                    coalescing_class=VOICE_COALESCING_CLASS,
                ),
                release=release_voice,
            ),
            order=2,
        ),
    )
    await _wait_for_direct_condition(lambda: bool(ingress_gate._ingress_draining_groups.get(provisional_key)))

    barrier_task = asyncio.create_task(
        _admit_prompt_result(
            ingress_gate,
            provisional_key,
            _barrier_ready_result(room=room, key=key, event_id="$command", order=3),
            barrier=True,
        ),
    )
    try:
        await asyncio.sleep(0.02)
        await coalescing_gate.drain_all()
        assert [batch.source_event_ids for batch in batches] == [["$text"], ["$command"]]
        assert barrier_task.done()
    finally:
        release_voice.set()
        await barrier_task
        await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert [batch.source_event_ids for batch in batches] == [["$text"], ["$command"], ["$voice"]]


@pytest.mark.asyncio
async def test_receive_time_dynamic_barrier_dispatches_before_pending_raw_voice_stt_finishes() -> None:
    """A ready dynamic barrier should not wait behind unrelated earlier STT."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(debounce_seconds=0.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")
    release_voice = asyncio.Event()

    await ingress_gate.admit_raw_voice(
        provisional_key,
        _raw_voice_ingress_item(
            room=room,
            event_id="$voice",
            preliminary_key=key,
            ready_task=_ready_task(
                _prompt_ready_result(
                    room=room,
                    key=key,
                    event_id="$voice",
                    body="pending voice transcript",
                    order=1,
                    source_kind=VOICE_SOURCE_KIND,
                    coalescing_class=VOICE_COALESCING_CLASS,
                ),
                release=release_voice,
            ),
            order=1,
        ),
    )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _barrier_ready_result(room=room, key=key, event_id="$command", order=2),
        barrier=False,
    )

    await _wait_for_direct_condition(
        lambda: [batch.source_event_ids for batch in batches] == [["$command"]],
        deadline_seconds=0.2,
    )

    release_voice.set()
    await _drain_direct_ingress(ingress_gate, coalescing_gate)
    assert [batch.source_event_ids for batch in batches] == [["$command"], ["$voice"]]


@pytest.mark.asyncio
async def test_receive_time_dynamic_barrier_dispatches_ready_text_before_barrier() -> None:
    """A ready dynamic barrier must not overtake earlier ready prompt work."""
    room = _threaded_room()
    downstream_gate = MagicMock()
    dispatched: list[list[str]] = []

    async def enqueue_sealed_batch(_key: tuple[str, str | None, str], pending_events: list[PendingEvent]) -> None:
        dispatched.append([pending_event.event.event_id for pending_event in pending_events])

    async def enqueue(_key: tuple[str, str | None, str], pending_event: PendingEvent) -> None:
        dispatched.append([pending_event.event.event_id])

    downstream_gate.enqueue_sealed_batch = AsyncMock(side_effect=enqueue_sealed_batch)
    downstream_gate.enqueue = AsyncMock(side_effect=enqueue)
    ingress_gate = TurnIngressCoalescingGate(
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
        coalescing_gate=downstream_gate,
    )
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")

    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$text", body="ready text", order=1),
    )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _barrier_ready_result(room=room, key=key, event_id="$command", order=2),
        barrier=False,
    )
    await ingress_gate.drain_all()

    assert dispatched == [["$text"], ["$command"]]


@pytest.mark.asyncio
async def test_receive_time_drop_barrier_splits_later_text_from_pending_voice_group() -> None:
    """A drop barrier should split prompt groups without dispatching its own event."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(debounce_seconds=60.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")
    release_voice = asyncio.Event()

    await ingress_gate.admit_raw_voice(
        provisional_key,
        _raw_voice_ingress_item(
            room=room,
            event_id="$voice",
            preliminary_key=key,
            ready_task=_ready_task(
                _prompt_ready_result(
                    room=room,
                    key=key,
                    event_id="$voice",
                    body="voice transcript",
                    order=1,
                    source_kind=VOICE_SOURCE_KIND,
                    coalescing_class=VOICE_COALESCING_CLASS,
                ),
                release=release_voice,
            ),
            order=1,
        ),
    )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$text1", body="before drop", order=2),
    )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        DropReadyIngressResult(received_order=3, received_wall_time=3.0, split_prompt_group=True),
        barrier=True,
    )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$text2", body="after drop", order=4),
    )

    release_voice.set()
    await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert all(not {"$text1", "$text2"}.issubset(batch.source_event_ids) for batch in batches)
    assert [event_id for batch in batches for event_id in batch.source_event_ids].count("$text2") == 1


@pytest.mark.asyncio
async def test_receive_time_dynamic_drop_barrier_splits_after_text_without_waiting_for_voice() -> None:
    """A ready dynamic drop barrier should release later prompt work while earlier STT is pending."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(debounce_seconds=0.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")
    release_voice = asyncio.Event()

    await ingress_gate.admit_raw_voice(
        provisional_key,
        _raw_voice_ingress_item(
            room=room,
            event_id="$voice",
            preliminary_key=key,
            ready_task=_ready_task(
                _prompt_ready_result(
                    room=room,
                    key=key,
                    event_id="$voice",
                    body="pending voice transcript",
                    order=1,
                    source_kind=VOICE_SOURCE_KIND,
                    coalescing_class=VOICE_COALESCING_CLASS,
                ),
                release=release_voice,
            ),
            order=1,
        ),
    )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$text1", body="before drop", order=2),
    )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        DropReadyIngressResult(received_order=3, received_wall_time=3.0, split_prompt_group=True),
        barrier=False,
    )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$text2", body="after drop", order=4),
    )

    await _wait_for_direct_condition(
        lambda: [batch.source_event_ids for batch in batches] == [["$text2"]],
        deadline_seconds=0.2,
    )

    release_voice.set()
    await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert [batch.source_event_ids for batch in batches] == [["$text2"], ["$voice", "$text1"]]
    assert all(not {"$text1", "$text2"}.issubset(batch.source_event_ids) for batch in batches)


@pytest.mark.asyncio
async def test_receive_time_room_scope_raw_voice_and_text_coalesce_on_room_key() -> None:
    """Room-scoped raw voice and text should share the room-level preliminary key."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(debounce_seconds=0.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    room_key = (room.room_id, None, "@user:example.com")

    await ingress_gate.admit_raw_voice(
        provisional_key,
        _raw_voice_ingress_item(
            room=room,
            event_id="$voice",
            preliminary_key=room_key,
            ready_task=_ready_task(
                _prompt_ready_result(
                    room=room,
                    key=room_key,
                    event_id="$voice",
                    body="room voice",
                    order=1,
                    source_kind=VOICE_SOURCE_KIND,
                    coalescing_class=VOICE_COALESCING_CLASS,
                ),
            ),
            order=1,
        ),
    )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=room_key, event_id="$typed", body="room text", order=2),
    )
    await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert [batch.source_event_ids for batch in batches] == [["$voice", "$typed"]]
    assert batches[0].coalescing_key == room_key


@pytest.mark.asyncio
async def test_receive_time_room_scope_text_roots_remain_separate() -> None:
    """The ingress layer must preserve the downstream split for independent room-level text."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(debounce_seconds=0.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    room_key = (room.room_id, None, "@user:example.com")

    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=room_key, event_id="$first", body="first", order=1),
    )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=room_key, event_id="$second", body="second", order=2),
    )
    await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert [batch.source_event_ids for batch in batches] == [["$first"], ["$second"]]


@pytest.mark.asyncio
async def test_receive_time_voice_group_is_not_delayed_by_upload_grace() -> None:
    """Voice-bearing ingress groups should dispatch after debounce without upload-grace hold."""
    room = _threaded_room()
    ingress_gate, _coalescing_gate, batches = _install_direct_ingress_capture_gates(
        debounce_seconds=0.0,
        upload_grace_seconds=60.0,
    )
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")

    await ingress_gate.admit_raw_voice(
        provisional_key,
        _raw_voice_ingress_item(
            room=room,
            event_id="$voice",
            preliminary_key=key,
            ready_task=_ready_task(
                _prompt_ready_result(
                    room=room,
                    key=key,
                    event_id="$voice",
                    body="voice transcript",
                    order=1,
                    source_kind=VOICE_SOURCE_KIND,
                    coalescing_class=VOICE_COALESCING_CLASS,
                ),
            ),
            order=1,
        ),
    )

    await _wait_for_direct_condition(
        lambda: [batch.source_event_ids for batch in batches] == [["$voice"]],
        deadline_seconds=0.2,
    )


@pytest.mark.asyncio
async def test_receive_time_text_first_image_during_upload_grace_dispatches_once() -> None:
    """Late media may join a text-only ingress group held by upload grace."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(
        debounce_seconds=0.01,
        upload_grace_seconds=0.2,
    )
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")

    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$text", body="text first", order=1),
    )
    await asyncio.sleep(0.04)
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(
            room=room,
            key=key,
            event_id="$image",
            body="image upload",
            order=2,
            source_kind=IMAGE_SOURCE_KIND,
        ),
    )
    await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert [batch.source_event_ids for batch in batches] == [["$text", "$image"]]


@pytest.mark.asyncio
async def test_receive_time_text_first_delayed_image_during_upload_grace_dispatches_once() -> None:
    """Media admitted during upload grace should join even when its ready task is still pending."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(
        debounce_seconds=0.01,
        upload_grace_seconds=0.2,
    )
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")
    release_image = asyncio.Event()

    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$text", body="text first", order=1),
    )
    await asyncio.sleep(0.04)
    await ingress_gate.admit_ready_task(
        provisional_key,
        ready_task=_ready_task(
            _prompt_ready_result(
                room=room,
                key=key,
                event_id="$image",
                body="image upload",
                order=2,
                source_kind=IMAGE_SOURCE_KIND,
            ),
            release=release_image,
        ),
        source_kind=IMAGE_SOURCE_KIND,
        barrier=False,
    )
    await asyncio.sleep(0.03)
    assert batches == []

    release_image.set()
    await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert [batch.source_event_ids for batch in batches] == [["$text", "$image"]]


@pytest.mark.asyncio
async def test_receive_time_text_first_multiple_images_during_upload_grace_dispatch_once() -> None:
    """Media added during upload grace should keep the grace window open for later uploads."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(
        debounce_seconds=0.01,
        upload_grace_seconds=0.2,
    )
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")

    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$text", body="text first", order=1),
    )
    await asyncio.sleep(0.04)
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(
            room=room,
            key=key,
            event_id="$image1",
            body="first image upload",
            order=2,
            source_kind=IMAGE_SOURCE_KIND,
        ),
    )
    await asyncio.sleep(0.04)
    assert batches == []

    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(
            room=room,
            key=key,
            event_id="$image2",
            body="second image upload",
            order=3,
            source_kind=IMAGE_SOURCE_KIND,
        ),
    )
    await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert [batch.source_event_ids for batch in batches] == [["$text", "$image1", "$image2"]]


@pytest.mark.asyncio
async def test_receive_time_late_media_rearms_upload_grace() -> None:
    """Each late media upload extends the grace window for another upload in the same turn."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(
        debounce_seconds=0.01,
        upload_grace_seconds=0.05,
    )
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")

    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$text", body="text first", order=1),
    )
    await _wait_for_direct_condition(lambda: provisional_key in ingress_gate._ingress_grace_groups)
    await asyncio.sleep(0.02)
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(
            room=room,
            key=key,
            event_id="$image1",
            body="first image upload",
            order=2,
            source_kind=IMAGE_SOURCE_KIND,
        ),
    )
    await asyncio.sleep(0.04)
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(
            room=room,
            key=key,
            event_id="$image2",
            body="second image upload",
            order=3,
            source_kind=IMAGE_SOURCE_KIND,
        ),
    )
    await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert [batch.source_event_ids for batch in batches] == [["$text", "$image1", "$image2"]]


@pytest.mark.asyncio
async def test_receive_time_slow_text_still_waits_for_late_media_upload_grace() -> None:
    """Upload grace starts from text receive time even if text readiness is still pending."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(
        debounce_seconds=0.01,
        upload_grace_seconds=0.05,
    )
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")
    release_text = asyncio.Event()

    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$text", body="text first", order=1),
        release=release_text,
    )
    await _wait_for_direct_condition(lambda: provisional_key in ingress_gate._ingress_grace_groups)
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(
            room=room,
            key=key,
            event_id="$image",
            body="image upload",
            order=2,
            source_kind=IMAGE_SOURCE_KIND,
        ),
    )

    release_text.set()
    await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert [batch.source_event_ids for batch in batches] == [["$text", "$image"]]


@pytest.mark.asyncio
async def test_receive_time_upload_grace_exits_when_late_media_resolves_to_barrier() -> None:
    """A command-like late upload should split the current prompt immediately."""
    room = _threaded_room()
    downstream_gate = MagicMock()
    dispatched: list[list[str]] = []

    async def enqueue_sealed_batch(_key: tuple[str, str | None, str], pending_events: list[PendingEvent]) -> None:
        dispatched.append([pending_event.event.event_id for pending_event in pending_events])

    async def enqueue(_key: tuple[str, str | None, str], pending_event: PendingEvent) -> None:
        dispatched.append([pending_event.event.event_id])

    downstream_gate.enqueue_sealed_batch = AsyncMock(side_effect=enqueue_sealed_batch)
    downstream_gate.enqueue = AsyncMock(side_effect=enqueue)
    ingress_gate = TurnIngressCoalescingGate(
        debounce_seconds=lambda: 0.01,
        upload_grace_seconds=lambda: 60.0,
        is_shutting_down=lambda: False,
        coalescing_gate=downstream_gate,
    )
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")

    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$text", body="text first", order=1),
    )
    await _wait_for_direct_condition(lambda: provisional_key in ingress_gate._ingress_grace_groups)
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _barrier_ready_result(room=room, key=key, event_id="$command", order=2, source_kind=IMAGE_SOURCE_KIND),
    )

    await _wait_for_direct_condition(lambda: dispatched == [["$text"], ["$command"]], deadline_seconds=0.2)


@pytest.mark.asyncio
async def test_receive_time_upload_grace_hard_cap_prevents_indefinite_extension() -> None:
    """Repeated late uploads should not keep a text-first turn open forever."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(
        debounce_seconds=0.01,
        upload_grace_seconds=0.03,
    )
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")

    with (
        patch("mindroom.coalescing._UPLOAD_GRACE_HARD_CAP_MULTIPLIER", 1.5),
        patch("mindroom.coalescing._UPLOAD_GRACE_MAX_HARD_CAP_SECONDS", 0.045),
    ):
        await _admit_prompt_result(
            ingress_gate,
            provisional_key,
            _prompt_ready_result(room=room, key=key, event_id="$text", body="text first", order=1),
        )
        await _wait_for_direct_condition(lambda: provisional_key in ingress_gate._ingress_grace_groups)
        await asyncio.sleep(0.02)
        await _admit_prompt_result(
            ingress_gate,
            provisional_key,
            _prompt_ready_result(
                room=room,
                key=key,
                event_id="$image1",
                body="first image upload",
                order=2,
                source_kind=IMAGE_SOURCE_KIND,
            ),
        )
        await _wait_for_direct_condition(
            lambda: [batch.source_event_ids for batch in batches] == [["$text", "$image1"]],
        )
        await _admit_prompt_result(
            ingress_gate,
            provisional_key,
            _prompt_ready_result(
                room=room,
                key=key,
                event_id="$image2",
                body="second image upload",
                order=3,
                source_kind=IMAGE_SOURCE_KIND,
            ),
        )
        await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert [batch.source_event_ids for batch in batches] == [["$text", "$image1"], ["$image2"]]


@pytest.mark.asyncio
async def test_receive_time_text_during_upload_grace_starts_new_debounce() -> None:
    """Later text should not be pulled into a prior group's upload-grace hold."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(
        debounce_seconds=0.01,
        upload_grace_seconds=0.2,
    )
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")

    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$text1", body="first text", order=1),
    )
    await asyncio.sleep(0.04)
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$text2", body="second text", order=2),
    )
    await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert [batch.source_event_ids for batch in batches] == [["$text1"], ["$text2"]]


@pytest.mark.asyncio
async def test_receive_time_text_only_claimed_group_does_not_accept_late_text() -> None:
    """Text-only groups closed by debounce should not absorb later text while ready work is blocked."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(debounce_seconds=0.01)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")
    release_first_text = asyncio.Event()

    await ingress_gate.admit_ready_task(
        provisional_key,
        ready_task=_ready_task(
            _prompt_ready_result(room=room, key=key, event_id="$text1", body="first text", order=1),
            release=release_first_text,
        ),
        source_kind=MESSAGE_SOURCE_KIND,
        barrier=False,
    )
    await _wait_for_direct_condition(lambda: provisional_key not in ingress_gate._ingress_open_groups)
    assert batches == []

    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=key, event_id="$text2", body="second text", order=2),
    )
    release_first_text.set()
    await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert [batch.source_event_ids for batch in batches] == [["$text1"], ["$text2"]]


@pytest.mark.asyncio
async def test_receive_time_voice_text_image_retarget_to_single_successful_voice_key() -> None:
    """Text and image in a voice partition should follow one successful final voice key."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(debounce_seconds=0.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    preliminary_key = (room.room_id, "$preliminary", "@user:example.com")
    final_key = (room.room_id, "$final", "@user:example.com")

    await ingress_gate.admit_raw_voice(
        provisional_key,
        _raw_voice_ingress_item(
            room=room,
            event_id="$voice",
            preliminary_key=preliminary_key,
            ready_task=_ready_task(
                _prompt_ready_result(
                    room=room,
                    key=final_key,
                    preliminary_key=preliminary_key,
                    event_id="$voice",
                    body="voice transcript",
                    order=1,
                    source_kind=VOICE_SOURCE_KIND,
                    coalescing_class=VOICE_COALESCING_CLASS,
                ),
            ),
            order=1,
        ),
    )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=preliminary_key, event_id="$text", body="typed", order=2),
    )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(
            room=room,
            key=preliminary_key,
            event_id="$image",
            body="image",
            order=3,
            source_kind=IMAGE_SOURCE_KIND,
        ),
    )
    await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert [batch.source_event_ids for batch in batches] == [["$voice", "$text", "$image"]]
    assert batches[0].coalescing_key == final_key


@pytest.mark.asyncio
async def test_receive_time_voice_split_keeps_text_and_image_on_original_key() -> None:
    """Non-voice items should not guess a target when successful voices split final keys."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(debounce_seconds=0.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    preliminary_key = (room.room_id, "$preliminary", "@user:example.com")
    first_final_key = (room.room_id, "$final-one", "@user:example.com")
    second_final_key = (room.room_id, "$final-two", "@user:example.com")

    for order, event_id, final_key in [
        (1, "$voice1", first_final_key),
        (2, "$voice2", second_final_key),
    ]:
        await ingress_gate.admit_raw_voice(
            provisional_key,
            _raw_voice_ingress_item(
                room=room,
                event_id=event_id,
                preliminary_key=preliminary_key,
                ready_task=_ready_task(
                    _prompt_ready_result(
                        room=room,
                        key=final_key,
                        preliminary_key=preliminary_key,
                        event_id=event_id,
                        body=event_id,
                        order=order,
                        source_kind=VOICE_SOURCE_KIND,
                        coalescing_class=VOICE_COALESCING_CLASS,
                    ),
                ),
                order=order,
            ),
        )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(room=room, key=preliminary_key, event_id="$text", body="typed", order=3),
    )
    await _admit_prompt_result(
        ingress_gate,
        provisional_key,
        _prompt_ready_result(
            room=room,
            key=preliminary_key,
            event_id="$image",
            body="image",
            order=4,
            source_kind=IMAGE_SOURCE_KIND,
        ),
    )
    await _drain_direct_ingress(ingress_gate, coalescing_gate)

    assert _batch_ids_by_key(batches) == {
        first_final_key: [["$voice1"]],
        second_final_key: [["$voice2"]],
        preliminary_key: [["$text", "$image"]],
    }


@pytest.mark.asyncio
async def test_receive_time_text_ready_failure_closes_notice_and_later_text_dispatches() -> None:
    """A failed text ready task should close its queued notice and not poison later text."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, batches = _install_direct_ingress_capture_gates(debounce_seconds=0.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")
    reservation = MagicMock()
    failed_pending_event = _ingress_pending_event(
        room=room,
        key=key,
        event_id="$failed",
        body="failed text",
        dispatch_metadata=(
            PendingDispatchMetadata(
                kind=QUEUED_NOTICE_METADATA_KIND,
                payload=reservation,
                close=reservation.cancel,
                requires_solo_batch=True,
            ),
        ),
    )

    with patch("mindroom.turn_ingress_coalescing.logger.warning") as log_warning:
        await ingress_gate.admit_ready_task(
            provisional_key,
            ready_task=_ready_task(
                None,
                error=TurnIngressCoalescingGate.ReadyTaskError(failed_pending_event, VoiceNormalizationTestError()),
            ),
            barrier=False,
        )
        await _admit_prompt_result(
            ingress_gate,
            provisional_key,
            _prompt_ready_result(room=room, key=key, event_id="$later", body="later text", order=2),
        )
        await _drain_direct_ingress(ingress_gate, coalescing_gate)

    reservation.cancel.assert_called_once_with()
    assert any(call.args == ("Turn ingress ready task failed",) for call in log_warning.call_args_list)
    assert [batch.source_event_ids for batch in batches] == [["$later"]]


@pytest.mark.asyncio
async def test_receive_time_drain_all_raises_background_ingress_drain_error() -> None:
    """Background ingress drain failures must surface through drain_all."""
    room = _threaded_room()
    ingress_gate, coalescing_gate, _batches = _install_direct_ingress_capture_gates(debounce_seconds=0.0)
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    key = (room.room_id, "$thread_root", "@user:example.com")

    with patch.object(
        coalescing_gate,
        "enqueue_sealed_batch",
        new=AsyncMock(side_effect=VoiceFlushTestError("sealed dispatch failed")),
    ):
        await _admit_prompt_result(
            ingress_gate,
            provisional_key,
            _prompt_ready_result(room=room, key=key, event_id="$text", body="text", order=1),
        )
        await asyncio.sleep(0)

        with pytest.raises(VoiceFlushTestError, match="sealed dispatch failed"):
            await ingress_gate.drain_all()


def test_target_key_for_non_voice_item_dedupes_successful_voice_keys() -> None:
    """Only exactly one unique successful voice key should retarget a non-voice item."""
    original_key = ("!room:server", "$original", "@user:example.com")
    voice_key = ("!room:server", "$voice", "@user:example.com")
    other_voice_key = ("!room:server", "$other", "@user:example.com")

    assert _target_key_for_non_voice_item(original_key, [voice_key, voice_key]) == voice_key
    assert _target_key_for_non_voice_item(original_key, [voice_key, other_voice_key]) == original_key
    assert _target_key_for_non_voice_item(original_key, []) == original_key


def _record_handled_turn_ids(bot: AgentBot) -> list[str]:
    recorded_handled_ids: list[str] = []
    original_record_turn = bot._turn_controller.deps.turn_store.record_turn

    def record_turn(handled_turn: HandledTurnState) -> None:
        recorded_handled_ids.extend(handled_turn.source_event_ids)
        original_record_turn(handled_turn)

    bot._turn_controller.deps.turn_store.record_turn = MagicMock(side_effect=record_turn)
    return recorded_handled_ids


def _prepare_voice_event_for_outcomes(
    outcome_specs: list[tuple[str, str, str]],
    prepare_started: dict[str, asyncio.Event],
    release_stt: asyncio.Event,
) -> AsyncMock:
    outcome_by_event_id = {event_id: (outcome_kind, thread_id) for outcome_kind, event_id, thread_id in outcome_specs}

    async def prepare_voice_event(
        request: inbound_turn_normalizer.VoiceNormalizationRequest,
    ) -> inbound_turn_normalizer.VoiceNormalizationResult | None:
        outcome_kind, thread_id = outcome_by_event_id[request.event.event_id]
        prepare_started[request.event.event_id].set()
        await release_stt.wait()
        if outcome_kind == "success":
            return _normalized_voice_result(
                event=request.event,
                text=f"successful transcript for {request.event.event_id}",
                thread_id=thread_id,
            )
        if outcome_kind == "none":
            return None
        raise VoiceNormalizationTestError(request.event.event_id)

    return AsyncMock(side_effect=prepare_voice_event)


async def _run_raw_voice_stt_outcome_case(
    bot: AgentBot,
    outcome_specs: list[tuple[str, str, str]],
) -> tuple[nio.MatrixRoom, list[CoalescedBatch], MagicMock, list[str]]:
    room = _threaded_room()
    ingress_gate, gate, batches = _install_active_batch_capture_gates(
        bot,
        debounce_seconds=0.0,
        voice_debounce_seconds=60.0,
    )
    reservation = MagicMock()
    bot._turn_controller.deps.response_runner.reserve_waiting_human_message = MagicMock(return_value=reservation)
    recorded_handled_ids = _record_handled_turn_ids(bot)
    text_event = _threaded_prepared_text_event(
        event_id="$captured-text",
        body="typed follow-up captured by raw voice",
        thread_id="$pre-stt-thread",
    )
    voice_events = [
        _make_threaded_voice_event(event_id=event_id, thread_id="$pre-stt-thread")
        for _outcome_kind, event_id, _thread_id in outcome_specs
    ]
    prepare_started = {event.event_id: asyncio.Event() for event in voice_events}
    release_stt = asyncio.Event()
    voice_tasks: list[asyncio.Task[None]] = []
    try:
        with (
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$pre-stt-thread"),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=_prepare_voice_event_for_outcomes(outcome_specs, prepare_started, release_stt),
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            voice_tasks.extend(asyncio.create_task(bot._on_media_message(room, event)) for event in voice_events)
            for event in voice_events:
                await asyncio.wait_for(prepare_started[event.event_id].wait(), timeout=1.0)
                await _wait_for_voice_ready_admission(ingress_gate, event.event_id, deadline_seconds=1.0)

            await admit_prepared_text_like_ingress_for_test(
                bot._turn_controller,
                room=room,
                prepared_event=text_event,
                dispatch_event=text_event,
                requester_user_id="@user:example.com",
                dispatch_timing=None,
            )
            await _wait_for_direct_condition(
                lambda: (
                    len(bot._turn_controller.deps.response_runner.reserve_waiting_human_message.call_args_list)
                    == len(voice_events) + 1
                ),
                deadline_seconds=0.5,
            )
            reserved_event_ids = [
                call.kwargs["response_envelope"].source_event_id
                for call in bot._turn_controller.deps.response_runner.reserve_waiting_human_message.call_args_list
            ]
            assert reserved_event_ids == [event.event_id for event in voice_events] + ["$captured-text"]
            assert batches == []

            release_stt.set()
            await asyncio.wait_for(
                asyncio.gather(*voice_tasks, ingress_gate.drain_all(), return_exceptions=True),
                timeout=1.0,
            )
            await gate.drain_all()
    finally:
        release_stt.set()
        for task in voice_tasks:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
    return room, batches, reservation, recorded_handled_ids


def _assert_stt_matrix_dispatch_outcome(
    *,
    room: nio.MatrixRoom,
    batches: list[CoalescedBatch],
    reservation: MagicMock,
    outcome_specs: list[tuple[str, str, str]],
    expected_batch_ids: list[str],
    successful_thread_id: str | None,
) -> None:
    _ = reservation
    if not expected_batch_ids:
        assert batches == []
        return
    assert len(batches) == 1
    assert batches[0].source_event_ids == expected_batch_ids
    assert batches[0].coalescing_key == (room.room_id, successful_thread_id, "@user:example.com")
    expected_coalescing_class = (
        VOICE_COALESCING_CLASS
        if any(outcome_kind == "success" for outcome_kind, _event_id, _thread_id in outcome_specs)
        else TEXT_COALESCING_CLASS
    )
    assert batches[0].coalescing_class == expected_coalescing_class
    for outcome_kind, _event_id, thread_id in outcome_specs:
        if outcome_kind != "success":
            assert batches[0].coalescing_key[1] != thread_id


@pytest.mark.parametrize(
    ("outcome_specs", "expected_batch_ids", "successful_thread_id", "handled_none_ids", "failed_ids"),
    [
        pytest.param(
            [("none", "$voice-none-1", "$none-thread-1"), ("none", "$voice-none-2", "$none-thread-2")],
            ["$captured-text"],
            "$pre-stt-thread",
            ["$voice-none-1", "$voice-none-2"],
            [],
            id="all-none",
        ),
        pytest.param(
            [("success", "$voice-success", "$voice-thread"), ("none", "$voice-none", "$none-thread")],
            ["$voice-success", "$captured-text"],
            "$voice-thread",
            ["$voice-none"],
            [],
            id="success-none",
        ),
        pytest.param(
            [("success", "$voice-success", "$voice-thread"), ("error", "$voice-error", "$error-thread")],
            ["$voice-success", "$captured-text"],
            "$voice-thread",
            [],
            ["$voice-error"],
            id="success-error",
        ),
        pytest.param(
            [("error", "$voice-error-1", "$error-thread-1"), ("error", "$voice-error-2", "$error-thread-2")],
            ["$captured-text"],
            "$pre-stt-thread",
            [],
            ["$voice-error-1", "$voice-error-2"],
            id="all-error",
        ),
    ],
)
@pytest.mark.asyncio
async def test_raw_voice_burst_stt_outcome_matrix_with_captured_text(
    mock_home_bot: AgentBot,
    outcome_specs: list[tuple[str, str, str]],
    expected_batch_ids: list[str],
    successful_thread_id: str | None,
    handled_none_ids: list[str],
    failed_ids: list[str],
) -> None:
    """Raw voice STT failures should not let captured text dispatch unowned."""
    room, batches, reservation, recorded_handled_ids = await _run_raw_voice_stt_outcome_case(
        mock_home_bot,
        outcome_specs,
    )
    _assert_stt_matrix_dispatch_outcome(
        room=room,
        batches=batches,
        reservation=reservation,
        outcome_specs=outcome_specs,
        expected_batch_ids=expected_batch_ids,
        successful_thread_id=successful_thread_id,
    )

    for event_id in handled_none_ids:
        assert recorded_handled_ids.count(event_id) == 1
        assert mock_home_bot._turn_store.is_handled(event_id)
    for event_id in failed_ids:
        assert event_id not in recorded_handled_ids
        assert not mock_home_bot._turn_store.is_handled(event_id)


@pytest.mark.asyncio
async def test_voice_registers_pending_burst_before_thread_resolution_finishes(
    mock_home_bot: AgentBot,
) -> None:
    """A later text event must see pending raw voice even while audio thread lookup is slow."""
    bot = mock_home_bot
    room = _threaded_room()
    voice_event = _make_threaded_voice_event(event_id="$voice-slow-thread")
    resolution_started = asyncio.Event()
    allow_resolution = asyncio.Event()

    async def slow_coalescing_thread_id(*_args: object, **_kwargs: object) -> str:
        resolution_started.set()
        await allow_resolution.wait()
        return "$thread_root"

    normalized_voice = _normalized_voice_result(
        event=voice_event,
        text="transcript after slow thread lookup",
        thread_id="$thread_root",
    )

    with (
        patch.object(
            bot._turn_controller.deps.resolver,
            "coalescing_thread_id",
            new=AsyncMock(side_effect=slow_coalescing_thread_id),
        ),
        patch.object(
            bot._turn_controller.deps.normalizer,
            "prepare_voice_event",
            new=AsyncMock(return_value=normalized_voice),
        ),
        patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
        patch.object(bot._turn_controller.deps.coalescing_gate, "enqueue", new=AsyncMock()),
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
    ):
        task = asyncio.create_task(bot._turn_controller.handle_media_event(room, voice_event))
        await asyncio.wait_for(resolution_started.wait(), timeout=0.2)
        await _wait_for_direct_condition(
            lambda: any(
                admission.ready_task.get_name() == "voice_ready:$voice-slow-thread"
                and admission.preliminary_key_task is not None
                and not admission.preliminary_key_task.done()
                for group in _all_ingress_prompt_groups(bot._turn_controller.deps.turn_ingress_gate)
                for admission in group.items
            ),
            deadline_seconds=0.5,
        )
        allow_resolution.set()
        await asyncio.wait_for(task, timeout=0.5)
        await bot._turn_controller.deps.turn_ingress_gate.drain_all()


@pytest.mark.asyncio
async def test_voice_preliminary_active_final_inactive_cancels_preliminary_reservation(
    mock_home_bot: AgentBot,
) -> None:
    """A final inactive voice target should release any preliminary active-turn notice."""
    bot = mock_home_bot
    room = _threaded_room()
    voice_event = _make_threaded_voice_event(event_id="$voice-final-inactive", thread_id="$pre_stt_thread")
    normalized_voice = _normalized_voice_result(
        event=voice_event,
        text="retargeted out of active thread",
        thread_id="$post_stt_thread",
    )
    pre_stt_target = bot._turn_controller.deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id="$pre_stt_thread",
        reply_to_event_id=voice_event.event_id,
        event_source=voice_event.source,
    )
    lifecycle = unwrap_extracted_collaborator(bot._response_runner)._lifecycle_coordinator
    pre_stt_signal = lifecycle._get_or_create_queued_signal(pre_stt_target)
    enqueued_events: list[tuple[tuple[str, str | None, str], PendingEvent]] = []
    original_reserve = bot._turn_controller.deps.response_runner.reserve_waiting_human_message

    async def capture_gate_enqueue_sealed(
        key: tuple[str, str | None, str],
        pending_events: list[PendingEvent],
    ) -> None:
        assert len(pending_events) == 1
        enqueued_events.append((key, pending_events[0]))

    pre_stt_signal.begin_response_turn()
    try:
        with (
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$pre_stt_thread"),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=AsyncMock(return_value=normalized_voice),
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
            patch.object(
                bot._turn_controller.deps.response_runner,
                "reserve_waiting_human_message",
                wraps=original_reserve,
            ) as reserve_waiting_human_message,
            patch.object(
                bot._turn_controller.deps.coalescing_gate,
                "enqueue_sealed_batch",
                new=AsyncMock(side_effect=capture_gate_enqueue_sealed),
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            await bot._on_media_message(room, voice_event)
            await bot._turn_controller.deps.turn_ingress_gate.drain_all()
    finally:
        pre_stt_signal.finish_response_turn()

    assert [call.kwargs["target"].resolved_thread_id for call in reserve_waiting_human_message.call_args_list] == [
        "$pre_stt_thread",
    ]
    assert pre_stt_signal.pending_human_messages == 0
    assert not pre_stt_signal.is_set()
    assert len(enqueued_events) == 1
    key, pending_event = enqueued_events[0]
    assert key == (room.room_id, "$post_stt_thread", voice_event.sender)
    assert pending_event.dispatch_policy_source_kind is None
    assert pending_event.dispatch_metadata == ()


@pytest.mark.asyncio
async def test_voice_visible_echo_cancellation_cancels_preliminary_active_reservation(
    mock_home_bot: AgentBot,
) -> None:
    """A cancelled visible echo should not leak a preliminary active-turn notice."""
    bot = mock_home_bot
    room = _threaded_room()
    voice_event = _make_threaded_voice_event(event_id="$voice-echo-cancelled", thread_id="$thread_root")
    normalized_voice = _normalized_voice_result(
        event=voice_event,
        text="same active thread after visible echo cancellation",
        thread_id="$thread_root",
    )
    target = bot._turn_controller.deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id="$thread_root",
        reply_to_event_id=voice_event.event_id,
        event_source=voice_event.source,
    )
    lifecycle = unwrap_extracted_collaborator(bot._response_runner)._lifecycle_coordinator
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    original_reserve = bot._turn_controller.deps.response_runner.reserve_waiting_human_message

    queued_signal.begin_response_turn()
    try:
        with (
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$thread_root"),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=AsyncMock(return_value=normalized_voice),
            ),
            patch.object(
                bot._turn_controller,
                "_maybe_send_visible_voice_echo",
                new=AsyncMock(side_effect=asyncio.CancelledError),
            ),
            patch.object(
                bot._turn_controller.deps.response_runner,
                "reserve_waiting_human_message",
                wraps=original_reserve,
            ) as reserve_waiting_human_message,
            patch.object(
                bot._turn_controller.deps.coalescing_gate,
                "enqueue_sealed_batch",
                new=AsyncMock(),
            ) as enqueue_sealed_batch,
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            await bot._on_media_message(room, voice_event)
            await bot._turn_controller.deps.turn_ingress_gate.drain_all()
    finally:
        queued_signal.finish_response_turn()

    assert [call.kwargs["target"].resolved_thread_id for call in reserve_waiting_human_message.call_args_list] == [
        "$thread_root",
    ]
    enqueue_sealed_batch.assert_not_awaited()
    assert queued_signal.pending_human_messages == 0
    assert not queued_signal.is_set()


@pytest.mark.asyncio
async def test_voice_preliminary_inactive_final_active_reserves_final_target(
    mock_home_bot: AgentBot,
) -> None:
    """A voice target that becomes active after STT should carry one final-target reservation."""
    bot = mock_home_bot
    room = _threaded_room()
    voice_event = _make_threaded_voice_event(event_id="$voice-post-active", thread_id="$pre_stt_thread")
    normalized_voice = _normalized_voice_result(
        event=voice_event,
        text="retargeted into active thread",
        thread_id="$post_stt_thread",
    )
    post_stt_target = bot._turn_controller.deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id="$post_stt_thread",
        reply_to_event_id=normalized_voice.event.event_id,
        event_source=normalized_voice.event.source,
    )
    lifecycle = unwrap_extracted_collaborator(bot._response_runner)._lifecycle_coordinator
    post_stt_signal = lifecycle._get_or_create_queued_signal(post_stt_target)
    enqueued_events: list[tuple[tuple[str, str | None, str], PendingEvent]] = []
    original_reserve = bot._turn_controller.deps.response_runner.reserve_waiting_human_message

    async def capture_gate_enqueue_sealed(
        key: tuple[str, str | None, str],
        pending_events: list[PendingEvent],
    ) -> None:
        assert len(pending_events) == 1
        assert post_stt_signal.pending_human_messages == 1
        enqueued_events.append((key, pending_events[0]))

    post_stt_signal.begin_response_turn()
    pending_event: PendingEvent | None = None
    try:
        with (
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$pre_stt_thread"),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=AsyncMock(return_value=normalized_voice),
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
            patch.object(
                bot._turn_controller.deps.response_runner,
                "reserve_waiting_human_message",
                wraps=original_reserve,
            ) as reserve_waiting_human_message,
            patch.object(
                bot._turn_controller.deps.coalescing_gate,
                "enqueue_sealed_batch",
                new=AsyncMock(side_effect=capture_gate_enqueue_sealed),
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            await bot._on_media_message(room, voice_event)
            await bot._turn_controller.deps.turn_ingress_gate.drain_all()

        assert [call.kwargs["target"].resolved_thread_id for call in reserve_waiting_human_message.call_args_list] == [
            "$post_stt_thread",
        ]
        assert len(enqueued_events) == 1
        key, pending_event = enqueued_events[0]
        assert key == (room.room_id, "$post_stt_thread", voice_event.sender)
        assert pending_event.dispatch_policy_source_kind == ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND
        assert len(pending_event.dispatch_metadata) == 1
        assert pending_event.dispatch_metadata[0].payload is not None
        assert post_stt_signal.pending_human_messages == 1
    finally:
        if pending_event is not None:
            close_pending_event_metadata([pending_event])
        post_stt_signal.finish_response_turn()

    assert post_stt_signal.pending_human_messages == 0
    assert not post_stt_signal.is_set()


@pytest.mark.asyncio
async def test_voice_preliminary_active_same_final_target_keeps_reservation_after_active_turn_ends(
    mock_home_bot: AgentBot,
) -> None:
    """A same-target voice follow-up should keep preliminary ownership after STT delay."""
    bot = mock_home_bot
    room = _threaded_room()
    voice_event = _make_threaded_voice_event(event_id="$voice-same-target-ended", thread_id="$thread_root")
    normalized_voice = _normalized_voice_result(
        event=voice_event,
        text="same active thread after slow stt",
        thread_id="$thread_root",
    )
    target = bot._turn_controller.deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id="$thread_root",
        reply_to_event_id=voice_event.event_id,
        event_source=voice_event.source,
    )
    lifecycle = unwrap_extracted_collaborator(bot._response_runner)._lifecycle_coordinator
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    preliminary_reserved = asyncio.Event()
    active_turn_finished = False
    reservations: list[object | None] = []
    enqueued_events: list[tuple[tuple[str, str | None, str], PendingEvent]] = []
    original_reserve = bot._turn_controller.deps.response_runner.reserve_waiting_human_message

    async def prepare_voice_event(
        _request: inbound_turn_normalizer.VoiceNormalizationRequest,
    ) -> inbound_turn_normalizer.VoiceNormalizationResult:
        nonlocal active_turn_finished
        await preliminary_reserved.wait()
        queued_signal.finish_response_turn()
        active_turn_finished = True
        return normalized_voice

    def reserve_waiting_human_message_spy(
        *,
        target: MessageTarget,
        response_envelope: MessageEnvelope,
    ) -> object | None:
        reservation = original_reserve(target=target, response_envelope=response_envelope)
        reservations.append(reservation)
        preliminary_reserved.set()
        return reservation

    async def capture_gate_enqueue_sealed(
        key: tuple[str, str | None, str],
        pending_events: list[PendingEvent],
    ) -> None:
        assert len(pending_events) == 1
        assert not queued_signal.has_active_response_turn()
        enqueued_events.append((key, pending_events[0]))

    queued_signal.begin_response_turn()
    pending_event: PendingEvent | None = None
    try:
        with (
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$thread_root"),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=AsyncMock(side_effect=prepare_voice_event),
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
            patch.object(
                bot._turn_controller.deps.response_runner,
                "reserve_waiting_human_message",
                side_effect=reserve_waiting_human_message_spy,
            ) as reserve_waiting_human_message,
            patch.object(
                bot._turn_controller.deps.coalescing_gate,
                "enqueue_sealed_batch",
                new=AsyncMock(side_effect=capture_gate_enqueue_sealed),
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            await bot._on_media_message(room, voice_event)
            await bot._turn_controller.deps.turn_ingress_gate.drain_all()

        assert [call.kwargs["target"].resolved_thread_id for call in reserve_waiting_human_message.call_args_list] == [
            "$thread_root",
        ]
        assert len(reservations) == 1
        assert len(enqueued_events) == 1
        key, pending_event = enqueued_events[0]
        assert key == (room.room_id, "$thread_root", voice_event.sender)
        assert pending_event.dispatch_policy_source_kind == ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND
        assert len(pending_event.dispatch_metadata) == 1
        assert pending_event.dispatch_metadata[0].payload is reservations[0]
    finally:
        if pending_event is not None:
            close_pending_event_metadata([pending_event])
        if not active_turn_finished:
            queued_signal.finish_response_turn()

    assert queued_signal.pending_human_messages == 0
    assert not queued_signal.is_set()


@pytest.mark.asyncio
async def test_late_text_after_voice_claim_retargets_with_successful_voice_key(
    mock_home_bot: AgentBot,
) -> None:
    """Text arriving after raw voice claim should still follow the retargeted voice group."""
    bot = mock_home_bot
    room = _threaded_room()
    ingress_gate = TurnIngressCoalescingGate(
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate, turn_ingress_gate=ingress_gate)
    bot._turn_controller.deps.response_runner.has_active_response_for_target = MagicMock(return_value=True)
    bot._turn_controller.deps.response_runner.reserve_waiting_human_message = MagicMock(return_value=None)

    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")
    voice_event = _make_threaded_voice_event(event_id="$late-voice", thread_id="$thread_root")
    text_event = _threaded_prepared_text_event(
        event_id="$late-text",
        body="late typed follow-up",
        thread_id="$thread_root",
    )
    prepare_started = asyncio.Event()
    release_stt = asyncio.Event()

    async def prepare_voice_event(
        request: inbound_turn_normalizer.VoiceNormalizationRequest,
    ) -> inbound_turn_normalizer.VoiceNormalizationResult:
        prepare_started.set()
        await release_stt.wait()
        return _normalized_voice_result(
            event=request.event,
            text="retargeted transcript",
            thread_id="$retargeted_thread",
        )

    voice_task: asyncio.Task[None] | None = None
    try:
        with (
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$thread_root"),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=AsyncMock(side_effect=prepare_voice_event),
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            voice_task = asyncio.create_task(bot._on_media_message(room, voice_event))
            await asyncio.wait_for(prepare_started.wait(), timeout=1.0)
            await _wait_until_claimed_ingress_voice_group_exists(
                ingress_gate,
                provisional_key,
                voice_event.event_id,
            )

            await admit_prepared_text_like_ingress_for_test(
                bot._turn_controller,
                room=room,
                prepared_event=text_event,
                dispatch_event=text_event,
                requester_user_id="@user:example.com",
                dispatch_timing=None,
            )

            release_stt.set()
            await asyncio.wait_for(voice_task, timeout=1.0)
            await ingress_gate.drain_all()
            await gate.drain_all()
    finally:
        release_stt.set()
        if voice_task is not None and not voice_task.done():
            voice_task.cancel()
            with suppress(asyncio.CancelledError):
                await voice_task

    assert len(batches) == 1
    voice_pending_event = next(
        pending_event.event
        for pending_event in batches[0].pending_events
        if pending_event.event.event_id == "$late-voice"
    )
    assert voice_pending_event.source["content"]["m.relates_to"]["event_id"] == "$retargeted_thread"
    assert batches[0].source_kind == VOICE_SOURCE_KIND
    assert set(batches[0].source_event_ids) == {"$late-text", "$late-voice"}
    assert "late typed follow-up" in batches[0].prompt
    assert "retargeted transcript" in batches[0].prompt


@pytest.mark.asyncio
async def test_normal_text_after_raw_voice_without_active_response_keeps_receive_order(
    mock_home_bot: AgentBot,
) -> None:
    """Idle voice-first text should join the receive-time voice group before downstream dispatch."""
    bot = mock_home_bot
    room = _threaded_room()
    ingress_gate = TurnIngressCoalescingGate(
        debounce_seconds=lambda: 0.01,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.01,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate, turn_ingress_gate=ingress_gate)
    bot._turn_controller.deps.response_runner.has_active_response_for_target = MagicMock(return_value=False)
    bot._turn_controller.deps.response_runner.reserve_waiting_human_message = MagicMock(return_value=None)

    voice_event = _make_threaded_voice_event(event_id="$voice1")
    text_event = _threaded_prepared_text_event(
        event_id="$typed",
        body="typed follow-up",
    )
    prepare_started = asyncio.Event()
    release_stt = asyncio.Event()

    async def prepare_voice_event(
        request: inbound_turn_normalizer.VoiceNormalizationRequest,
    ) -> inbound_turn_normalizer.VoiceNormalizationResult:
        prepare_started.set()
        await release_stt.wait()
        return _normalized_voice_result(
            event=request.event,
            text="transcript for voice1",
            thread_id="$thread_root",
        )

    voice_task: asyncio.Task[None] | None = None
    try:
        with (
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$thread_root"),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=AsyncMock(side_effect=prepare_voice_event),
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            voice_task = asyncio.create_task(bot._on_media_message(room, voice_event))
            await asyncio.wait_for(prepare_started.wait(), timeout=1.0)
            await _wait_for_direct_condition(
                lambda: any(
                    admission.ready_task.get_name() == "voice_ready:$voice1"
                    for group in ingress_gate._ingress_open_groups.values()
                    for admission in group.items
                ),
                deadline_seconds=1.0,
            )

            await admit_prepared_text_like_ingress_for_test(
                bot._turn_controller,
                room=room,
                prepared_event=text_event,
                dispatch_event=text_event,
                requester_user_id="@user:example.com",
                dispatch_timing=None,
            )
            await asyncio.sleep(0.03)
            assert batches == []

            release_stt.set()
            await asyncio.wait_for(voice_task, timeout=1.0)
            await bot._turn_controller.deps.turn_ingress_gate.drain_all()
            await gate.drain_all()
    finally:
        release_stt.set()
        if voice_task is not None and not voice_task.done():
            voice_task.cancel()
            with suppress(asyncio.CancelledError):
                await voice_task

    assert len(batches) == 1
    assert batches[0].source_event_ids == ["$voice1", "$typed"]
    assert "transcript for voice1" in batches[0].prompt
    assert "typed follow-up" in batches[0].prompt


@pytest.mark.asyncio
async def test_retargeted_voice_text_batch_handoff_uses_resolved_voice_thread(
    mock_home_bot: AgentBot,
) -> None:
    """Synthetic dispatch source should carry the batch key, not the stale typed source thread."""
    bot = mock_home_bot
    room = _threaded_room()
    voice_event = _make_threaded_voice_event(event_id="$voice-retargeted", thread_id="$pre_stt_thread")
    normalized_voice = _normalized_voice_result(
        event=voice_event,
        text="retargeted transcript",
        thread_id="$post_stt_thread",
    )
    text_event = _threaded_prepared_text_event(
        event_id="$typed",
        body="typed follow-up",
        thread_id="$pre_stt_thread",
    )
    voice_key, voice_pending_event, _voice_source_kind = await bot._turn_controller._build_pending_event_for_dispatch(
        normalized_voice.event,
        room,
        source_kind=VOICE_SOURCE_KIND,
        requester_user_id="@user:example.com",
        coalescing_key=(room.room_id, "$post_stt_thread", "@user:example.com"),
        trust_internal_payload_metadata=True,
        enqueue_time=1.0,
    )
    _text_key, text_pending_event, _text_source_kind = await bot._turn_controller._build_pending_event_for_dispatch(
        text_event,
        room,
        source_kind=MESSAGE_SOURCE_KIND,
        requester_user_id="@user:example.com",
        coalescing_key=(room.room_id, "$post_stt_thread", "@user:example.com"),
        enqueue_time=2.0,
    )
    batch = build_coalesced_batch(voice_key, [voice_pending_event, text_pending_event])
    captured_handoffs: list[DispatchHandoff] = []

    async def capture_handoff(handoff: DispatchHandoff, **_kwargs: object) -> None:
        captured_handoffs.append(handoff)

    with patch.object(bot._turn_controller, "_dispatch_handoff", new=AsyncMock(side_effect=capture_handoff)):
        await bot._turn_controller.handle_coalesced_batch(batch)

    assert len(captured_handoffs) == 1
    handoff_event = captured_handoffs[0].event
    assert handoff_event.source["content"]["m.relates_to"]["event_id"] == "$post_stt_thread"


@pytest.mark.asyncio
async def test_retargeted_voice_room_text_batch_handoff_adds_resolved_voice_thread(
    mock_home_bot: AgentBot,
) -> None:
    """A room-level typed primary should not erase the resolved thread from a retargeted voice batch."""
    bot = mock_home_bot
    room = _threaded_room()
    voice_event = _make_threaded_voice_event(event_id="$voice-retargeted", thread_id="$pre_stt_thread")
    normalized_voice = _normalized_voice_result(
        event=voice_event,
        text="retargeted transcript",
        thread_id="$post_stt_thread",
    )
    text_event = PreparedTextEvent(
        sender="@user:example.com",
        event_id="$typed",
        body="typed follow-up",
        source={
            "event_id": "$typed",
            "sender": "@user:example.com",
            "origin_server_ts": 1_712_350_000_000,
            "type": "m.room.message",
            "room_id": room.room_id,
            "content": {
                "body": "typed follow-up",
                "msgtype": "m.text",
            },
        },
        server_timestamp=1_712_350_000_000,
    )
    voice_key, voice_pending_event, _voice_source_kind = await bot._turn_controller._build_pending_event_for_dispatch(
        normalized_voice.event,
        room,
        source_kind=VOICE_SOURCE_KIND,
        requester_user_id="@user:example.com",
        coalescing_key=(room.room_id, "$post_stt_thread", "@user:example.com"),
        trust_internal_payload_metadata=True,
        enqueue_time=1.0,
    )
    _text_key, text_pending_event, _text_source_kind = await bot._turn_controller._build_pending_event_for_dispatch(
        text_event,
        room,
        source_kind=MESSAGE_SOURCE_KIND,
        requester_user_id="@user:example.com",
        coalescing_key=(room.room_id, "$post_stt_thread", "@user:example.com"),
        enqueue_time=2.0,
    )
    batch = build_coalesced_batch(voice_key, [voice_pending_event, text_pending_event])
    captured_handoffs: list[DispatchHandoff] = []

    async def capture_handoff(handoff: DispatchHandoff, **_kwargs: object) -> None:
        captured_handoffs.append(handoff)

    with patch.object(bot._turn_controller, "_dispatch_handoff", new=AsyncMock(side_effect=capture_handoff)):
        await bot._turn_controller.handle_coalesced_batch(batch)

    assert len(captured_handoffs) == 1
    handoff_event = captured_handoffs[0].event
    assert handoff_event.source["content"]["m.relates_to"]["rel_type"] == "m.thread"
    assert handoff_event.source["content"]["m.relates_to"]["event_id"] == "$post_stt_thread"


@pytest.mark.asyncio
async def test_retargeted_voice_plain_reply_text_batch_handoff_adds_resolved_voice_thread(
    mock_home_bot: AgentBot,
) -> None:
    """A voice batch should preserve plain reply metadata while adding the resolved thread."""
    bot = mock_home_bot
    room = _threaded_room()
    voice_event = _make_threaded_voice_event(event_id="$voice-retargeted", thread_id="$pre_stt_thread")
    normalized_voice = _normalized_voice_result(
        event=voice_event,
        text="retargeted transcript",
        thread_id="$post_stt_thread",
    )
    text_event = PreparedTextEvent(
        sender="@user:example.com",
        event_id="$typed",
        body="typed follow-up",
        source={
            "event_id": "$typed",
            "sender": "@user:example.com",
            "origin_server_ts": 1_712_350_000_000,
            "type": "m.room.message",
            "room_id": room.room_id,
            "content": {
                "body": "typed follow-up",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$plain_reply_parent"}},
            },
        },
        server_timestamp=1_712_350_000_000,
    )
    voice_key, voice_pending_event, _voice_source_kind = await bot._turn_controller._build_pending_event_for_dispatch(
        normalized_voice.event,
        room,
        source_kind=VOICE_SOURCE_KIND,
        requester_user_id="@user:example.com",
        coalescing_key=(room.room_id, "$post_stt_thread", "@user:example.com"),
        trust_internal_payload_metadata=True,
        enqueue_time=1.0,
    )
    _text_key, text_pending_event, _text_source_kind = await bot._turn_controller._build_pending_event_for_dispatch(
        text_event,
        room,
        source_kind=MESSAGE_SOURCE_KIND,
        requester_user_id="@user:example.com",
        coalescing_key=(room.room_id, "$post_stt_thread", "@user:example.com"),
        enqueue_time=2.0,
    )
    batch = build_coalesced_batch(voice_key, [voice_pending_event, text_pending_event])
    captured_handoffs: list[DispatchHandoff] = []

    async def capture_handoff(handoff: DispatchHandoff, **_kwargs: object) -> None:
        captured_handoffs.append(handoff)

    with patch.object(bot._turn_controller, "_dispatch_handoff", new=AsyncMock(side_effect=capture_handoff)):
        await bot._turn_controller.handle_coalesced_batch(batch)

    assert len(captured_handoffs) == 1
    relates_to = captured_handoffs[0].event.source["content"]["m.relates_to"]
    assert relates_to["rel_type"] == "m.thread"
    assert relates_to["event_id"] == "$post_stt_thread"
    assert relates_to["m.in_reply_to"]["event_id"] == "$plain_reply_parent"


async def _run_active_raw_voice_text_receive_sequence(
    bot: AgentBot,
    sequence: list[str],
    *,
    expected_ids: list[str],
    slow_text_ids: frozenset[str] = frozenset(),
    release_voice_order: list[str] | None = None,
) -> list[CoalescedBatch]:
    room = _threaded_room()
    _ingress_gate, gate, batches = _install_active_batch_capture_gates(
        bot,
        debounce_seconds=0.02,
        voice_debounce_seconds=0.05,
    )
    voice_events = {
        "$voice1": _make_threaded_voice_event(event_id="$voice1"),
        "$voice2": _make_threaded_voice_event(event_id="$voice2"),
    }
    text_events = {
        "$text": _threaded_prepared_text_event(event_id="$text", body="typed follow-up"),
        "$text1": _threaded_prepared_text_event(event_id="$text1", body="first typed follow-up"),
        "$text2": _threaded_prepared_text_event(event_id="$text2", body="second typed follow-up"),
    }
    raw_text_events = {
        event_id: _threaded_text_event(event_id=event.event_id, body=event.body)
        for event_id, event in text_events.items()
    }
    voice_started = {event_id: asyncio.Event() for event_id in voice_events}
    voice_finished = {event_id: asyncio.Event() for event_id in voice_events}
    voice_releases = {event_id: asyncio.Event() for event_id in voice_events}
    text_resolution_started = {event_id: asyncio.Event() for event_id in slow_text_ids}
    release_text_resolution = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []

    async def coalescing_thread_id(_room: nio.MatrixRoom, event: PreparedTextEvent | nio.RoomMessageAudio) -> str:
        if event.event_id in slow_text_ids:
            text_resolution_started[event.event_id].set()
            await release_text_resolution.wait()
        return "$thread_root"

    async def prepare_voice_event(
        request: inbound_turn_normalizer.VoiceNormalizationRequest,
    ) -> inbound_turn_normalizer.VoiceNormalizationResult:
        voice_started[request.event.event_id].set()
        await voice_releases[request.event.event_id].wait()
        voice_finished[request.event.event_id].set()
        return _normalized_voice_result(
            event=request.event,
            text=f"transcript for {request.event.event_id}",
        )

    async def resolve_text_event(
        request: inbound_turn_normalizer.TextNormalizationRequest,
    ) -> PreparedTextEvent:
        return text_events[request.event.event_id]

    try:
        with (
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(side_effect=coalescing_thread_id),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=AsyncMock(side_effect=prepare_voice_event),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "resolve_text_event",
                new=AsyncMock(side_effect=resolve_text_event),
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            for event_id in sequence:
                await _start_active_receive_order_event(
                    bot=bot,
                    room=room,
                    event_id=event_id,
                    voice_events=voice_events,
                    raw_text_events=raw_text_events,
                    voice_started=voice_started,
                    text_resolution_started=text_resolution_started,
                    slow_text_ids=slow_text_ids,
                    tasks=tasks,
                )

            release_text_resolution.set()
            for event_id in release_voice_order or [event_id for event_id in sequence if event_id in voice_events]:
                voice_releases[event_id].set()
                await asyncio.wait_for(voice_finished[event_id].wait(), timeout=1.0)

            await asyncio.wait_for(asyncio.gather(*tasks), timeout=1.0)
            await bot._turn_controller.deps.turn_ingress_gate.drain_all()
            await gate.drain_all()
    finally:
        release_text_resolution.set()
        for release in voice_releases.values():
            release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    _assert_active_voice_batch(batches, expected_ids)
    return batches


async def _start_active_receive_order_event(
    *,
    bot: AgentBot,
    room: nio.MatrixRoom,
    event_id: str,
    voice_events: dict[str, nio.RoomMessageAudio],
    raw_text_events: dict[str, nio.RoomMessageText],
    voice_started: dict[str, asyncio.Event],
    text_resolution_started: dict[str, asyncio.Event],
    slow_text_ids: frozenset[str],
    tasks: list[asyncio.Task[None]],
) -> None:
    if event_id in voice_events:
        task = asyncio.create_task(bot._on_media_message(room, voice_events[event_id]))
        tasks.append(task)
        await asyncio.wait_for(voice_started[event_id].wait(), timeout=1.0)
        return

    task = asyncio.create_task(bot._on_message(room, raw_text_events[event_id]))
    tasks.append(task)
    if event_id in slow_text_ids:
        await asyncio.wait_for(text_resolution_started[event_id].wait(), timeout=1.0)
        await asyncio.sleep(0.08)
        return
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_active_voice_voice_text_receive_order_coalesces_with_out_of_order_stt(
    mock_home_bot: AgentBot,
) -> None:
    """Out-of-order STT completion must preserve raw receive order."""
    await _run_active_raw_voice_text_receive_sequence(
        mock_home_bot,
        ["$voice1", "$voice2", "$text"],
        expected_ids=["$voice1", "$voice2", "$text"],
        release_voice_order=["$voice2", "$voice1"],
    )


@pytest.mark.asyncio
async def test_voice_burst_coalesces_by_receive_time_not_stt_completion(
    mock_home_bot: AgentBot,
) -> None:
    """Raw voice batch order should come from receive-time admission, not transcript completion."""
    await _run_active_raw_voice_text_receive_sequence(
        mock_home_bot,
        ["$voice1", "$voice2"],
        expected_ids=["$voice1", "$voice2"],
        release_voice_order=["$voice2", "$voice1"],
    )


@pytest.mark.asyncio
async def test_active_voice_text_voice_receive_order_coalesces(
    mock_home_bot: AgentBot,
) -> None:
    """Typed text between raw voice events should stay in one voice batch."""
    await _run_active_raw_voice_text_receive_sequence(
        mock_home_bot,
        ["$voice1", "$text", "$voice2"],
        expected_ids=["$voice1", "$text", "$voice2"],
    )


@pytest.mark.asyncio
async def test_active_text_voice_text_receive_order_survives_slow_text_thread_resolution(
    mock_home_bot: AgentBot,
) -> None:
    """Text received first should stay first even when thread resolution is slow."""
    await _run_active_raw_voice_text_receive_sequence(
        mock_home_bot,
        ["$text1", "$voice1", "$text2"],
        expected_ids=["$text1", "$voice1", "$text2"],
        slow_text_ids=frozenset({"$text1"}),
    )


@pytest.mark.asyncio
async def test_text_received_before_voice_keeps_receive_order_when_text_thread_resolution_is_slow(
    mock_home_bot: AgentBot,
) -> None:
    """A slow first text turn should be admitted before a later raw voice turn."""
    await _run_active_raw_voice_text_receive_sequence(
        mock_home_bot,
        ["$text", "$voice1"],
        expected_ids=["$text", "$voice1"],
        slow_text_ids=frozenset({"$text"}),
    )


@pytest.mark.asyncio
async def test_text_admission_happens_before_prepared_text_ready_resolves_thread(
    mock_home_bot: AgentBot,
) -> None:
    """Text ingress should be admitted before prepared-text thread resolution resumes."""
    bot = mock_home_bot
    room = _threaded_room()
    ingress_gate, gate, batches = _install_active_batch_capture_gates(
        bot,
        debounce_seconds=60.0,
        voice_debounce_seconds=60.0,
    )
    prepared_event = _threaded_prepared_text_event(event_id="$text-admitted", body="typed first")
    resolution_started = asyncio.Event()
    release_resolution = asyncio.Event()
    provisional_key = IngressProvisionalKey(room.room_id, "@user:example.com")

    async def slow_coalescing_thread_id(
        _room: nio.MatrixRoom,
        event: PreparedTextEvent | nio.RoomMessageText,
    ) -> str:
        if event.event_id == "$text-admitted":
            resolution_started.set()
            await release_resolution.wait()
        return "$thread_root"

    task: asyncio.Task[None] | None = None
    try:
        with (
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(side_effect=slow_coalescing_thread_id),
            ),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            task = asyncio.create_task(
                admit_prepared_text_like_ingress_for_test(
                    bot._turn_controller,
                    room=room,
                    prepared_event=prepared_event,
                    dispatch_event=prepared_event,
                    requester_user_id="@user:example.com",
                    dispatch_timing=None,
                ),
            )
            await asyncio.wait_for(resolution_started.wait(), timeout=1.0)
            await _wait_for_direct_condition(
                lambda: (
                    any(
                        admission.ready_task.get_name() == "text_ready:$text-admitted"
                        for group in ingress_gate._ingress_open_groups.values()
                        for admission in group.items
                    )
                    and provisional_key in ingress_gate._ingress_open_groups
                ),
                deadline_seconds=0.5,
            )
            assert batches == []

            release_resolution.set()
            await asyncio.wait_for(task, timeout=1.0)
            await _drain_direct_ingress(ingress_gate, gate)
    finally:
        release_resolution.set()
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    assert [batch.source_event_ids for batch in batches] == [["$text-admitted"]]


@pytest.mark.asyncio
async def test_raw_voice_burst_sent_during_streaming_waits_for_all_transcripts(
    mock_home_bot: AgentBot,
) -> None:
    """Raw voice sent during a streaming reply should flush once after every burst item is transcribed."""
    bot = mock_home_bot
    room = _threaded_room()
    _install_streaming_test_gate(bot)

    first_voice = _make_threaded_voice_event(event_id="$voice1")
    second_voice = _make_threaded_voice_event(event_id="$voice2")
    started, releases, prepare_voice_event = _staggered_voice_normalizer()
    streaming_started, release_streaming, dispatches, record_dispatch = _streaming_dispatch_recorder()

    streaming_event = _threaded_prepared_text_event(event_id="$streaming", body="still streaming")
    first_task: asyncio.Task[None] | None = None
    second_task: asyncio.Task[None] | None = None
    try:
        with (
            patch.object(
                bot._turn_controller,
                "_dispatch_text_message",
                new=record_dispatch,
            ),
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$thread_root"),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=prepare_voice_event,
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            await _enqueue_ready_streaming_event(
                bot,
                room,
                streaming_event,
                (room.room_id, "$thread_root", "@user:example.com"),
            )
            await asyncio.wait_for(streaming_started.wait(), timeout=1.0)

            first_task = asyncio.create_task(bot._on_media_message(room, first_voice))
            second_task = asyncio.create_task(bot._on_media_message(room, second_voice))
            await asyncio.wait_for(started["$voice1"].wait(), timeout=1.0)
            await asyncio.wait_for(started["$voice2"].wait(), timeout=1.0)

            releases["$voice1"].set()
            await asyncio.sleep(0.02)
            release_streaming.set()
            await asyncio.sleep(0.05)

            assert dispatches == [(["$streaming"], "still streaming")]

            releases["$voice2"].set()
            await asyncio.gather(first_task, second_task)
            await drain_coalescing(bot)
    finally:
        release_streaming.set()
        for release in releases.values():
            release.set()
        for task in (first_task, second_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    assert len(dispatches) == 2
    followup_ids, followup_prompt = dispatches[1]
    assert followup_ids == ["$voice1", "$voice2"]
    assert "transcript for $voice1" in followup_prompt
    assert "transcript for $voice2" in followup_prompt


@pytest.mark.asyncio
async def test_text_then_raw_voice_sent_during_streaming_coalesces_after_all_transcripts(
    mock_home_bot: AgentBot,
) -> None:
    """Text sent before raw voice during one streaming turn should wait for every transcript."""
    bot = mock_home_bot
    room = _threaded_room()
    _install_streaming_test_gate(bot)

    typed_followup = _threaded_prepared_text_event(event_id="$typed", body="typed follow-up")
    first_voice = _make_threaded_voice_event(event_id="$voice1")
    second_voice = _make_threaded_voice_event(event_id="$voice2")
    started, releases, prepare_voice_event = _staggered_voice_normalizer()
    streaming_started, release_streaming, dispatches, record_dispatch = _streaming_dispatch_recorder()

    streaming_event = _threaded_prepared_text_event(event_id="$streaming", body="still streaming")
    first_task: asyncio.Task[None] | None = None
    second_task: asyncio.Task[None] | None = None
    try:
        with (
            patch.object(
                bot._turn_controller,
                "_dispatch_text_message",
                new=record_dispatch,
            ),
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$thread_root"),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=prepare_voice_event,
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            await _enqueue_ready_streaming_event(
                bot,
                room,
                streaming_event,
                (room.room_id, "$thread_root", "@user:example.com"),
            )
            await asyncio.wait_for(streaming_started.wait(), timeout=1.0)

            await admit_prepared_text_like_ingress_for_test(
                bot._turn_controller,
                room=room,
                prepared_event=typed_followup,
                dispatch_event=typed_followup,
                requester_user_id="@user:example.com",
                dispatch_timing=None,
            )
            first_task = asyncio.create_task(bot._on_media_message(room, first_voice))
            second_task = asyncio.create_task(bot._on_media_message(room, second_voice))
            await asyncio.wait_for(started["$voice1"].wait(), timeout=1.0)
            await asyncio.wait_for(started["$voice2"].wait(), timeout=1.0)

            releases["$voice1"].set()
            await asyncio.sleep(0.02)
            release_streaming.set()
            await asyncio.sleep(0.05)

            assert dispatches == [(["$streaming"], "still streaming")]

            releases["$voice2"].set()
            await asyncio.gather(first_task, second_task)
            await drain_coalescing(bot)
    finally:
        release_streaming.set()
        for release in releases.values():
            release.set()
        for task in (first_task, second_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    assert len(dispatches) == 2
    followup_ids, followup_prompt = dispatches[1]
    assert set(followup_ids) == {"$typed", "$voice1", "$voice2"}
    assert "typed follow-up" in followup_prompt
    assert "transcript for $voice1" in followup_prompt
    assert "transcript for $voice2" in followup_prompt


@pytest.mark.asyncio
async def test_text_before_retargeted_raw_voice_during_streaming_coalesces_on_voice_thread(
    mock_home_bot: AgentBot,
) -> None:
    """Text already in the main gate should follow a later receive-time voice retarget."""
    bot = mock_home_bot
    room = _threaded_room()
    _install_streaming_test_gate(bot)

    typed_followup = _threaded_prepared_text_event(
        event_id="$typed",
        body="typed follow-up",
        thread_id="$pre_stt_thread",
    )
    voice_event = _make_threaded_voice_event(event_id="$voice1", thread_id="$pre_stt_thread")
    prepare_started = asyncio.Event()
    release_stt = asyncio.Event()
    streaming_started = asyncio.Event()
    release_streaming = asyncio.Event()
    dispatches: list[tuple[list[str], str, str | None]] = []

    async def prepare_voice_event(
        request: inbound_turn_normalizer.VoiceNormalizationRequest,
    ) -> inbound_turn_normalizer.VoiceNormalizationResult:
        prepare_started.set()
        await release_stt.wait()
        return _normalized_voice_result(
            event=request.event,
            text="retargeted transcript",
            thread_id="$post_stt_thread",
        )

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: PreparedTextEvent | nio.RoomMessageText,
        _requester_user_id: str,
        *,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        source_event_ids = _handled_source_event_ids(handled_turn)
        content = dispatched_event.source.get("content") if isinstance(dispatched_event.source, dict) else None
        relates_to = content.get("m.relates_to") if isinstance(content, dict) else None
        thread_id = relates_to.get("event_id") if isinstance(relates_to, dict) else None
        dispatches.append((source_event_ids, dispatched_event.body, thread_id))
        if source_event_ids == ["$streaming"]:
            streaming_started.set()
            await release_streaming.wait()

    streaming_event = _threaded_prepared_text_event(
        event_id="$streaming",
        body="still streaming",
        thread_id="$pre_stt_thread",
    )
    voice_task: asyncio.Task[None] | None = None
    try:
        with (
            patch.object(
                bot._turn_controller,
                "_dispatch_text_message",
                new=AsyncMock(side_effect=record_dispatch),
            ),
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$pre_stt_thread"),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=AsyncMock(side_effect=prepare_voice_event),
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            await _enqueue_ready_streaming_event(
                bot,
                room,
                streaming_event,
                (room.room_id, "$pre_stt_thread", "@user:example.com"),
            )
            await asyncio.wait_for(streaming_started.wait(), timeout=1.0)

            await admit_prepared_text_like_ingress_for_test(
                bot._turn_controller,
                room=room,
                prepared_event=typed_followup,
                dispatch_event=typed_followup,
                requester_user_id="@user:example.com",
                dispatch_timing=None,
            )
            voice_task = asyncio.create_task(bot._on_media_message(room, voice_event))
            await asyncio.wait_for(prepare_started.wait(), timeout=1.0)

            release_stt.set()
            release_streaming.set()
            await asyncio.wait_for(voice_task, timeout=1.0)
            await drain_coalescing(bot)
    finally:
        release_stt.set()
        release_streaming.set()
        if voice_task is not None and not voice_task.done():
            voice_task.cancel()
            with suppress(asyncio.CancelledError):
                await voice_task

    assert len(dispatches) == 2
    followup_ids, followup_prompt, followup_thread_id = dispatches[1]
    assert set(followup_ids) == {"$typed", "$voice1"}
    assert "typed follow-up" in followup_prompt
    assert "retargeted transcript" in followup_prompt
    assert followup_thread_id == "$post_stt_thread"


@pytest.mark.asyncio
async def test_retargeted_voice_waits_for_later_raw_voice_pending_under_original_key(  # noqa: PLR0915
    mock_home_bot: AgentBot,
) -> None:
    """A retargeted first voice group should still wait for later raw voice under the pre-STT key."""
    bot = mock_home_bot
    room = _threaded_room()
    ingress_gate = TurnIngressCoalescingGate(
        debounce_seconds=lambda: 0.01,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    gate = CoalescingGate(
        dispatch_batch=bot._dispatch_coalesced_batch,
        debounce_seconds=lambda: 0.01,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate, turn_ingress_gate=ingress_gate)
    bot._turn_controller.deps.response_runner.has_active_response_for_target = MagicMock(return_value=True)
    bot._turn_controller.deps.response_runner.reserve_waiting_human_message = MagicMock(return_value=None)

    typed_followup = _threaded_prepared_text_event(
        event_id="$typed",
        body="typed follow-up",
        thread_id="$pre_stt_thread",
    )
    first_voice = _make_threaded_voice_event(event_id="$voice1", thread_id="$pre_stt_thread")
    second_voice = _make_threaded_voice_event(event_id="$voice2", thread_id="$pre_stt_thread")
    streaming_event = _threaded_prepared_text_event(
        event_id="$streaming",
        body="still streaming",
        thread_id="$pre_stt_thread",
    )
    raw_key = (room.room_id, "$pre_stt_thread", "@user:example.com")
    started = {"$voice1": asyncio.Event(), "$voice2": asyncio.Event()}
    releases = {"$voice1": asyncio.Event(), "$voice2": asyncio.Event()}
    streaming_started = asyncio.Event()
    release_streaming = asyncio.Event()
    dispatches: list[tuple[list[str], str, str | None]] = []

    async def prepare_voice_event(
        request: inbound_turn_normalizer.VoiceNormalizationRequest,
    ) -> inbound_turn_normalizer.VoiceNormalizationResult:
        started[request.event.event_id].set()
        await releases[request.event.event_id].wait()
        return _normalized_voice_result(
            event=request.event,
            text=f"retargeted transcript for {request.event.event_id}",
            thread_id="$post_stt_thread",
        )

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: PreparedTextEvent | nio.RoomMessageText,
        _requester_user_id: str,
        *,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        source_event_ids = _handled_source_event_ids(handled_turn)
        content = dispatched_event.source.get("content") if isinstance(dispatched_event.source, dict) else None
        relates_to = content.get("m.relates_to") if isinstance(content, dict) else None
        thread_id = relates_to.get("event_id") if isinstance(relates_to, dict) else None
        dispatches.append((source_event_ids, dispatched_event.body, thread_id))
        if source_event_ids == ["$streaming"]:
            streaming_started.set()
            await release_streaming.wait()

    first_task: asyncio.Task[None] | None = None
    second_task: asyncio.Task[None] | None = None
    try:
        with (
            patch.object(
                bot._turn_controller,
                "_dispatch_text_message",
                new=AsyncMock(side_effect=record_dispatch),
            ),
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$pre_stt_thread"),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=AsyncMock(side_effect=prepare_voice_event),
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            await _enqueue_ready_streaming_event(
                bot,
                room,
                streaming_event,
                raw_key,
            )
            await asyncio.wait_for(streaming_started.wait(), timeout=1.0)

            await admit_prepared_text_like_ingress_for_test(
                bot._turn_controller,
                room=room,
                prepared_event=typed_followup,
                dispatch_event=typed_followup,
                requester_user_id="@user:example.com",
                dispatch_timing=None,
            )
            first_task = asyncio.create_task(bot._on_media_message(room, first_voice))
            await asyncio.wait_for(started["$voice1"].wait(), timeout=1.0)
            await _wait_until_claimed_ingress_voice_group_exists(
                ingress_gate,
                IngressProvisionalKey(room.room_id, "@user:example.com"),
                first_voice.event_id,
            )
            second_task = asyncio.create_task(bot._on_media_message(room, second_voice))
            await asyncio.wait_for(started["$voice2"].wait(), timeout=1.0)

            releases["$voice1"].set()
            await asyncio.wait_for(first_task, timeout=1.0)
            release_streaming.set()
            await asyncio.sleep(0.05)

            assert dispatches == [(["$streaming"], "still streaming", "$pre_stt_thread")]

            releases["$voice2"].set()
            await asyncio.wait_for(second_task, timeout=1.0)
            await drain_coalescing(bot)
    finally:
        release_streaming.set()
        for release in releases.values():
            release.set()
        for task in (first_task, second_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    assert len(dispatches) == 2
    followup_ids, followup_prompt, followup_thread_id = dispatches[1]
    assert set(followup_ids) == {"$typed", "$voice1", "$voice2"}
    assert "typed follow-up" in followup_prompt
    assert "retargeted transcript for $voice1" in followup_prompt
    assert "retargeted transcript for $voice2" in followup_prompt
    assert followup_thread_id == "$post_stt_thread"


@pytest.mark.asyncio
async def test_text_then_raw_voice_waits_while_voice_ready_path_is_suspended(
    mock_home_bot: AgentBot,
) -> None:
    """Text queued before voice should not dispatch while voice readiness is paused."""
    bot = mock_home_bot
    room = _threaded_room()
    _install_streaming_test_gate(bot)

    typed_followup = _threaded_prepared_text_event(event_id="$typed", body="typed follow-up")
    voice_event = _make_threaded_voice_event(event_id="$voice1")
    started, releases, prepare_voice_event = _staggered_voice_normalizer()
    streaming_started, release_streaming, dispatches, record_dispatch = _streaming_dispatch_recorder()
    voice_ready_paused = asyncio.Event()
    release_voice_ready = asyncio.Event()

    async def suspend_voice_ready(*_args: object, **_kwargs: object) -> None:
        voice_ready_paused.set()
        await release_voice_ready.wait()

    streaming_event = _threaded_prepared_text_event(event_id="$streaming", body="still streaming")
    voice_task: asyncio.Task[None] | None = None
    try:
        with (
            patch.object(
                bot._turn_controller,
                "_dispatch_text_message",
                new=record_dispatch,
            ),
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$thread_root"),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=prepare_voice_event,
            ),
            patch.object(
                bot._turn_controller,
                "_maybe_send_visible_voice_echo",
                new=AsyncMock(side_effect=suspend_voice_ready),
            ),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            await _enqueue_ready_streaming_event(
                bot,
                room,
                streaming_event,
                (room.room_id, "$thread_root", "@user:example.com"),
            )
            await asyncio.wait_for(streaming_started.wait(), timeout=1.0)

            await admit_prepared_text_like_ingress_for_test(
                bot._turn_controller,
                room=room,
                prepared_event=typed_followup,
                dispatch_event=typed_followup,
                requester_user_id="@user:example.com",
                dispatch_timing=None,
            )
            voice_task = asyncio.create_task(bot._on_media_message(room, voice_event))
            await asyncio.wait_for(started["$voice1"].wait(), timeout=1.0)

            releases["$voice1"].set()
            await asyncio.wait_for(voice_ready_paused.wait(), timeout=1.0)
            release_streaming.set()
            await asyncio.sleep(0.05)

            assert dispatches == [(["$streaming"], "still streaming")]

            release_voice_ready.set()
            await voice_task
            await drain_coalescing(bot)
    finally:
        release_streaming.set()
        release_voice_ready.set()
        for release in releases.values():
            release.set()
        if voice_task is not None and not voice_task.done():
            voice_task.cancel()
            with suppress(asyncio.CancelledError):
                await voice_task

    assert len(dispatches) == 2
    followup_ids, followup_prompt = dispatches[1]
    assert set(followup_ids) == {"$typed", "$voice1"}
    assert "typed follow-up" in followup_prompt
    assert "transcript for $voice1" in followup_prompt


@pytest.mark.asyncio
async def test_raw_voice_and_text_sent_during_streaming_coalesce_into_one_followup(
    mock_home_bot: AgentBot,
) -> None:
    """Voice and typed follow-ups sent during the same streaming reply should produce one reply."""
    bot = mock_home_bot
    room = _threaded_room()
    _install_streaming_test_gate(bot)

    first_voice = _make_threaded_voice_event(event_id="$voice1")
    second_voice = _make_threaded_voice_event(event_id="$voice2")
    typed_followup = _threaded_prepared_text_event(event_id="$typed", body="typed follow-up")
    started, releases, prepare_voice_event = _staggered_voice_normalizer()
    streaming_started, release_streaming, dispatches, record_dispatch = _streaming_dispatch_recorder()

    streaming_event = _threaded_prepared_text_event(event_id="$streaming", body="still streaming")
    first_task: asyncio.Task[None] | None = None
    second_task: asyncio.Task[None] | None = None
    try:
        with (
            patch.object(
                bot._turn_controller,
                "_dispatch_text_message",
                new=record_dispatch,
            ),
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$thread_root"),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=prepare_voice_event,
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            await _enqueue_ready_streaming_event(
                bot,
                room,
                streaming_event,
                (room.room_id, "$thread_root", "@user:example.com"),
            )
            await asyncio.wait_for(streaming_started.wait(), timeout=1.0)

            first_task = asyncio.create_task(bot._on_media_message(room, first_voice))
            second_task = asyncio.create_task(bot._on_media_message(room, second_voice))
            await asyncio.wait_for(started["$voice1"].wait(), timeout=1.0)
            await asyncio.wait_for(started["$voice2"].wait(), timeout=1.0)

            await admit_prepared_text_like_ingress_for_test(
                bot._turn_controller,
                room=room,
                prepared_event=typed_followup,
                dispatch_event=typed_followup,
                requester_user_id="@user:example.com",
                dispatch_timing=None,
            )
            releases["$voice1"].set()
            await asyncio.sleep(0.02)
            release_streaming.set()
            await asyncio.sleep(0.05)

            assert dispatches == [(["$streaming"], "still streaming")]

            releases["$voice2"].set()
            await asyncio.gather(first_task, second_task)
            await drain_coalescing(bot)
    finally:
        release_streaming.set()
        for release in releases.values():
            release.set()
        for task in (first_task, second_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    assert len(dispatches) == 2
    followup_ids, followup_prompt = dispatches[1]
    assert set(followup_ids) == {"$voice1", "$voice2", "$typed"}
    assert "transcript for $voice1" in followup_prompt
    assert "transcript for $voice2" in followup_prompt
    assert "typed follow-up" in followup_prompt


@pytest.mark.asyncio
async def test_active_text_joining_pending_voice_reserves_queued_notice(
    mock_home_bot: AgentBot,
) -> None:
    """Active typed follow-ups should keep their queued notice when captured by raw voice."""
    bot = mock_home_bot
    room = _threaded_room()
    ingress_gate, gate, batches = _install_active_batch_capture_gates(
        bot,
        debounce_seconds=60.0,
        voice_debounce_seconds=60.0,
    )
    typed_followup = _threaded_prepared_text_event(
        event_id="$typed-active",
        body="typed follow-up while voice is pending",
    )
    voice_event = _make_threaded_voice_event(event_id="$pending-voice")
    reservation = MagicMock()
    prepare_started = asyncio.Event()
    release_stt = asyncio.Event()

    async def prepare_voice_event(
        request: inbound_turn_normalizer.VoiceNormalizationRequest,
    ) -> inbound_turn_normalizer.VoiceNormalizationResult:
        prepare_started.set()
        await release_stt.wait()
        return _normalized_voice_result(
            event=request.event,
            text="canonical voice transcript",
            thread_id="$thread_root",
        )

    voice_task: asyncio.Task[None] | None = None
    try:
        with (
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$thread_root"),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=AsyncMock(side_effect=prepare_voice_event),
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
            patch.object(
                bot._turn_controller.deps.response_runner,
                "reserve_waiting_human_message",
                return_value=reservation,
            ) as reserve_waiting_human_message,
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            voice_task = asyncio.create_task(bot._on_media_message(room, voice_event))
            await asyncio.wait_for(prepare_started.wait(), timeout=1.0)
            await _wait_for_voice_ready_admission(ingress_gate, voice_event.event_id, deadline_seconds=1.0)

            await admit_prepared_text_like_ingress_for_test(
                bot._turn_controller,
                room=room,
                prepared_event=typed_followup,
                dispatch_event=typed_followup,
                requester_user_id="@user:example.com",
                dispatch_timing=None,
            )

            await _wait_for_direct_condition(
                lambda: len(reserve_waiting_human_message.call_args_list) == 2,
                deadline_seconds=0.5,
            )
            assert [
                call.kwargs["response_envelope"].source_event_id
                for call in reserve_waiting_human_message.call_args_list
            ] == ["$pending-voice", "$typed-active"]
            assert batches == []

            release_stt.set()
            await asyncio.wait_for(asyncio.gather(voice_task, ingress_gate.drain_all()), timeout=1.0)
            await gate.drain_all()
    finally:
        release_stt.set()
        if voice_task is not None and not voice_task.done():
            voice_task.cancel()
            with suppress(asyncio.CancelledError):
                await voice_task

    assert len(batches) == 1
    assert batches[0].source_event_ids == ["$pending-voice", "$typed-active"]
    assert batches[0].dispatch_metadata[0].payload is reservation


async def _run_pending_raw_voice_barrier(
    bot: AgentBot,
    barrier_event: PreparedTextEvent,
    *,
    send_barrier: Callable[[AgentBot, nio.MatrixRoom, CoalescingGate], Awaitable[None]] | None = None,
) -> list[CoalescedBatch]:
    room = _threaded_room()
    barrier_dispatched = asyncio.Event()
    ingress_gate, gate, batches = _install_active_batch_capture_gates(
        bot,
        debounce_seconds=60.0,
        voice_debounce_seconds=60.0,
        dispatch_event=barrier_dispatched,
    )
    voice_event = _make_threaded_voice_event(event_id="$pending-voice")
    prepare_started = asyncio.Event()
    release_stt = asyncio.Event()

    async def prepare_voice_event(
        request: inbound_turn_normalizer.VoiceNormalizationRequest,
    ) -> inbound_turn_normalizer.VoiceNormalizationResult:
        prepare_started.set()
        await release_stt.wait()
        return _normalized_voice_result(
            event=request.event,
            text="canonical voice transcript",
        )

    async def default_send_barrier() -> None:
        await admit_prepared_text_like_ingress_for_test(
            bot._turn_controller,
            room=room,
            prepared_event=barrier_event,
            dispatch_event=barrier_event,
            requester_user_id="@user:example.com",
            dispatch_timing=None,
        )

    voice_task: asyncio.Task[None] | None = None
    try:
        with (
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$thread_root"),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=AsyncMock(side_effect=prepare_voice_event),
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            voice_task = asyncio.create_task(bot._on_media_message(room, voice_event))
            await asyncio.wait_for(prepare_started.wait(), timeout=1.0)
            await _wait_for_direct_condition(
                lambda: any(
                    admission.ready_task.get_name() == "voice_ready:$pending-voice"
                    for group in (
                        *ingress_gate._ingress_open_groups.values(),
                        *ingress_gate._ingress_grace_groups.values(),
                        *(
                            claimed_group
                            for claimed_groups in ingress_gate._ingress_claimed_voice_groups.values()
                            for claimed_group in claimed_groups
                        ),
                    )
                    for admission in group.items
                ),
                deadline_seconds=1.0,
            )

            if send_barrier is None:
                await default_send_barrier()
            else:
                await send_barrier(bot, room, gate)
            try:
                await asyncio.wait_for(barrier_dispatched.wait(), timeout=0.2)
            except TimeoutError:
                barrier_dispatched_before_stt = False
            else:
                barrier_dispatched_before_stt = [batch.source_event_ids for batch in batches] == [
                    [barrier_event.event_id],
                ]

            release_stt.set()
            await asyncio.wait_for(asyncio.gather(voice_task, ingress_gate.drain_all()), timeout=1.0)
            await gate.drain_all()
    finally:
        release_stt.set()
        if voice_task is not None and not voice_task.done():
            voice_task.cancel()
            with suppress(asyncio.CancelledError):
                await voice_task

    voice_batches = [batch for batch in batches if "$pending-voice" in batch.source_event_ids]
    assert len(voice_batches) == 1
    assert voice_batches[0].source_kind == VOICE_SOURCE_KIND
    assert voice_batches[0].coalescing_class == VOICE_COALESCING_CLASS
    assert any(barrier_event.event_id in batch.source_event_ids for batch in batches)
    if barrier_dispatched_before_stt:
        _assert_voice_barrier_batches(batches, barrier_event.event_id, "$pending-voice")
    assert barrier_dispatched_before_stt
    return batches


@pytest.mark.asyncio
async def test_help_command_not_captured_by_pending_raw_voice(mock_home_bot: AgentBot) -> None:
    """Help commands should bypass pending raw voice and dispatch before STT."""
    barrier_event = _threaded_prepared_text_event(event_id="$help", body="!help")
    raw_event = _threaded_text_event(event_id="$help", body="!help")

    async def send_barrier(bot: AgentBot, room: nio.MatrixRoom, _gate: CoalescingGate) -> None:
        with patch.object(
            bot._turn_controller.deps.normalizer,
            "resolve_text_event",
            new=AsyncMock(return_value=barrier_event),
        ):
            await bot._on_message(room, raw_event)

    await _run_pending_raw_voice_barrier(
        mock_home_bot,
        barrier_event,
        send_barrier=send_barrier,
    )


@pytest.mark.asyncio
async def test_schedule_list_command_not_captured_by_pending_raw_voice(mock_home_bot: AgentBot) -> None:
    """Schedule-list commands should bypass pending raw voice and dispatch before STT."""
    barrier_event = _threaded_prepared_text_event(event_id="$schedule-list", body="!schedule list")
    raw_event = _threaded_text_event(event_id="$schedule-list", body="!schedule list")

    async def send_barrier(bot: AgentBot, room: nio.MatrixRoom, _gate: CoalescingGate) -> None:
        with patch.object(
            bot._turn_controller.deps.normalizer,
            "resolve_text_event",
            new=AsyncMock(return_value=barrier_event),
        ):
            await bot._on_message(room, raw_event)

    await _run_pending_raw_voice_barrier(
        mock_home_bot,
        barrier_event,
        send_barrier=send_barrier,
    )


@pytest.mark.asyncio
async def test_hook_source_not_captured_by_pending_raw_voice(mock_home_bot: AgentBot) -> None:
    """Hook source messages should not join a pending raw voice burst."""
    await _run_pending_raw_voice_barrier(
        mock_home_bot,
        _threaded_prepared_text_event(
            event_id="$hook",
            body="hook message",
            source_kind=HOOK_SOURCE_KIND,
        ),
    )


@pytest.mark.asyncio
async def test_hook_dispatch_source_not_captured_by_pending_raw_voice(mock_home_bot: AgentBot) -> None:
    """Hook dispatch messages should not join a pending raw voice burst."""
    await _run_pending_raw_voice_barrier(
        mock_home_bot,
        _threaded_prepared_text_event(
            event_id="$hook-dispatch",
            body="hook dispatch message",
            source_kind=HOOK_DISPATCH_SOURCE_KIND,
        ),
    )


@pytest.mark.asyncio
async def test_scheduled_source_not_captured_by_pending_raw_voice(mock_home_bot: AgentBot) -> None:
    """Scheduled messages should not join a pending raw voice burst."""
    await _run_pending_raw_voice_barrier(
        mock_home_bot,
        _threaded_prepared_text_event(
            event_id="$scheduled",
            body="scheduled fire",
            source_kind=SCHEDULED_SOURCE_KIND,
        ),
    )


@pytest.mark.asyncio
async def test_trusted_non_voice_relay_not_captured_by_pending_raw_voice(mock_home_bot: AgentBot) -> None:
    """Trusted non-voice relays should bypass a pending raw voice burst."""
    await _run_pending_raw_voice_barrier(
        mock_home_bot,
        _threaded_prepared_text_event(
            event_id="$trusted-relay",
            body="trusted non-voice relay",
            sender="@mindroom_home:localhost",
            source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
            content_overrides={ORIGINAL_SENDER_KEY: "@user:example.com"},
        ),
    )


@pytest.mark.asyncio
async def test_blocking_solo_metadata_not_captured_by_pending_raw_voice(mock_home_bot: AgentBot) -> None:
    """Blocking solo metadata should prevent capture by pending raw voice."""
    barrier_event = _threaded_prepared_text_event(event_id="$blocking-solo", body="blocking solo")

    async def send_barrier(bot: AgentBot, room: nio.MatrixRoom, _gate: CoalescingGate) -> None:
        metadata_owner = MagicMock()
        original_build = bot._turn_controller._build_pending_event_for_dispatch
        build_called = asyncio.Event()

        async def build_with_blocking_metadata(*args: object, **kwargs: object) -> tuple[object, PendingEvent, str]:
            key, pending_event, source_kind = await original_build(*args, **kwargs)
            pending_event.dispatch_metadata = (
                PendingDispatchMetadata(
                    kind="blocking_test_metadata",
                    payload=metadata_owner,
                    close=metadata_owner.close,
                    requires_solo_batch=True,
                ),
            )
            build_called.set()
            return key, pending_event, source_kind

        with patch.object(bot._turn_controller, "_build_pending_event_for_dispatch", new=build_with_blocking_metadata):
            await admit_prepared_text_like_ingress_for_test(
                bot._turn_controller,
                room=room,
                prepared_event=barrier_event,
                dispatch_event=barrier_event,
                requester_user_id="@user:example.com",
                dispatch_timing=None,
            )
            await asyncio.wait_for(build_called.wait(), timeout=1.0)

    await _run_pending_raw_voice_barrier(
        mock_home_bot,
        barrier_event,
        send_barrier=send_barrier,
    )


@pytest.mark.asyncio
async def test_interactive_text_selection_not_captured_by_pending_raw_voice(mock_home_bot: AgentBot) -> None:
    """Interactive selections should split surrounding text while raw voice STT is pending."""
    bot = mock_home_bot
    room = _threaded_room()
    _ingress_gate, gate, batches = _install_active_batch_capture_gates(
        bot,
        debounce_seconds=60.0,
        voice_debounce_seconds=60.0,
    )
    voice_event = _make_threaded_voice_event(event_id="$pending-voice")
    text1_event = _threaded_prepared_text_event(event_id="$text1", body="before selection")
    selection_event = _threaded_prepared_text_event(event_id="$selection", body="1")
    text2_event = _threaded_prepared_text_event(event_id="$text2", body="after selection")
    raw_text1_event = _threaded_text_event(event_id="$text1", body="before selection")
    raw_selection_event = _threaded_text_event(event_id="$selection", body="1")
    raw_text2_event = _threaded_text_event(event_id="$text2", body="after selection")
    prepared_by_id = {
        "$text1": text1_event,
        "$selection": selection_event,
        "$text2": text2_event,
    }
    prepare_started = asyncio.Event()
    release_stt = asyncio.Event()
    selection = interactive.InteractiveSelection(
        question_event_id="$question",
        selection_key="1",
        selected_value="Option 1",
        thread_id="$thread_root",
    )

    async def prepare_voice_event(
        request: inbound_turn_normalizer.VoiceNormalizationRequest,
    ) -> inbound_turn_normalizer.VoiceNormalizationResult:
        prepare_started.set()
        await release_stt.wait()
        return _normalized_voice_result(event=request.event, text="canonical voice transcript")

    async def resolve_text_event(request: inbound_turn_normalizer.TextNormalizationRequest) -> PreparedTextEvent:
        return prepared_by_id[request.event.event_id]

    async def handle_text_response(
        _client: object,
        _room: nio.MatrixRoom,
        event: PreparedTextEvent,
        _agent_name: str,
        *,
        resolved_thread_id: str | None,
    ) -> interactive.InteractiveSelection | None:
        assert resolved_thread_id == "$thread_root"
        return selection if event.event_id == "$selection" else None

    voice_task: asyncio.Task[None] | None = None
    try:
        with (
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$thread_root"),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=AsyncMock(side_effect=prepare_voice_event),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "resolve_text_event",
                new=AsyncMock(side_effect=resolve_text_event),
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new=AsyncMock(side_effect=handle_text_response),
            ),
            patch.object(bot._turn_controller, "handle_interactive_selection", new=AsyncMock()) as handle_selection,
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            voice_task = asyncio.create_task(bot._on_media_message(room, voice_event))
            await asyncio.wait_for(prepare_started.wait(), timeout=1.0)

            await bot._on_message(room, raw_text1_event)
            await bot._on_message(room, raw_selection_event)
            await _wait_for_direct_condition(
                lambda: handle_selection.await_count == 1,
                deadline_seconds=0.5,
            )
            handle_selection.assert_awaited_once()
            await bot._on_message(room, raw_text2_event)
            assert batches == []

            release_stt.set()
            await asyncio.wait_for(
                asyncio.gather(voice_task, bot._turn_controller.deps.turn_ingress_gate.drain_all()),
                timeout=1.0,
            )
            await gate.drain_all()
    finally:
        release_stt.set()
        if voice_task is not None and not voice_task.done():
            voice_task.cancel()
            with suppress(asyncio.CancelledError):
                await voice_task

    dispatched_ids = [event_id for batch in batches for event_id in batch.source_event_ids]
    assert "$selection" not in dispatched_ids
    assert dispatched_ids.count("$text1") == 1
    assert dispatched_ids.count("$text2") == 1
    assert dispatched_ids.count("$pending-voice") == 1
    assert all(not {"$text1", "$text2"}.issubset(batch.source_event_ids) for batch in batches)


@pytest.mark.asyncio
async def test_router_early_skip_text_not_captured_by_pending_raw_voice(mock_home_bot: AgentBot) -> None:
    """Router early skips should split surrounding text while raw voice STT is pending."""
    bot = mock_home_bot
    room = _threaded_room()
    _ingress_gate, gate, batches = _install_active_batch_capture_gates(
        bot,
        debounce_seconds=60.0,
        voice_debounce_seconds=60.0,
    )
    voice_event = _make_threaded_voice_event(event_id="$pending-voice")
    text1_event = _threaded_prepared_text_event(event_id="$text1", body="before skip")
    text2_event = _threaded_prepared_text_event(event_id="$text2", body="after skip")
    raw_text1_event = _threaded_text_event(event_id="$text1", body="before skip")
    raw_text2_event = _threaded_text_event(event_id="$text2", body="after skip")
    hook_content = {
        ORIGINAL_SENDER_KEY: "@user:example.com",
        HOOK_SOURCE_KEY: "test:message:received",
        HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 2,
    }
    skipped_event = _threaded_prepared_text_event(
        event_id="$router-skip",
        body="plain router-skip text",
        sender="@mindroom_home:localhost",
        source_kind=HOOK_SOURCE_KIND,
        content_overrides=hook_content,
    )
    raw_skipped_event = _threaded_text_event(
        event_id="$router-skip",
        body="plain router-skip text",
        sender="@mindroom_home:localhost",
        content_overrides={
            SOURCE_KIND_KEY: HOOK_SOURCE_KIND,
            **hook_content,
        },
    )
    prepared_by_id = {
        "$text1": text1_event,
        "$router-skip": skipped_event,
        "$text2": text2_event,
    }
    prepare_started = asyncio.Event()
    release_stt = asyncio.Event()

    async def prepare_voice_event(
        request: inbound_turn_normalizer.VoiceNormalizationRequest,
    ) -> inbound_turn_normalizer.VoiceNormalizationResult:
        prepare_started.set()
        await release_stt.wait()
        return _normalized_voice_result(event=request.event, text="canonical voice transcript")

    async def resolve_text_event(request: inbound_turn_normalizer.TextNormalizationRequest) -> PreparedTextEvent:
        return prepared_by_id[request.event.event_id]

    voice_task: asyncio.Task[None] | None = None
    try:
        with (
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$thread_root"),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=AsyncMock(side_effect=prepare_voice_event),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "resolve_text_event",
                new=AsyncMock(side_effect=resolve_text_event),
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            voice_task = asyncio.create_task(bot._on_media_message(room, voice_event))
            await asyncio.wait_for(prepare_started.wait(), timeout=1.0)

            await bot._on_message(room, raw_text1_event)
            await bot._on_message(room, raw_skipped_event)
            await bot._on_message(room, raw_text2_event)
            assert batches == []

            release_stt.set()
            await asyncio.wait_for(
                asyncio.gather(voice_task, bot._turn_controller.deps.turn_ingress_gate.drain_all()),
                timeout=1.0,
            )
            await gate.drain_all()
    finally:
        release_stt.set()
        if voice_task is not None and not voice_task.done():
            voice_task.cancel()
            with suppress(asyncio.CancelledError):
                await voice_task

    dispatched_ids = [event_id for batch in batches for event_id in batch.source_event_ids]
    assert "$router-skip" not in dispatched_ids
    assert dispatched_ids.count("$text1") == 1
    assert dispatched_ids.count("$text2") == 1
    assert dispatched_ids.count("$pending-voice") == 1
    assert all(not {"$text1", "$text2"}.issubset(batch.source_event_ids) for batch in batches)


@pytest.mark.asyncio
async def test_voice_message_does_not_reserve_active_turn_signal_when_post_stt_echo_fails(
    mock_home_bot: AgentBot,
) -> None:
    """Visible echo failure should not leak a queued signal or block voice dispatch."""
    bot = mock_home_bot
    _ingress_gate, gate, batches = _install_active_batch_capture_gates(
        bot,
        debounce_seconds=0.0,
        voice_debounce_seconds=0.0,
    )
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:server"
    room.canonical_alias = None
    room.users = {
        "@mindroom_home:localhost": MagicMock(),
        "@user:example.com": MagicMock(),
    }
    room.members_synced = True

    voice_event = _make_voice_event(
        event_id="$voice-echo-fails",
        source={
            "content": {
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        },
    )
    target = bot._turn_controller.deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id="$thread_root",
        reply_to_event_id=voice_event.event_id,
        event_source=voice_event.source,
    )
    lifecycle = unwrap_extracted_collaborator(bot._response_runner)._lifecycle_coordinator
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    normalized_voice = inbound_turn_normalizer.VoiceNormalizationResult(
        event=PreparedTextEvent(
            sender=voice_event.sender,
            event_id=voice_event.event_id,
            body="🎤 continue",
            source={"content": {"body": "🎤 continue", SOURCE_KIND_KEY: "voice"}},
            server_timestamp=voice_event.server_timestamp,
            source_kind_override="voice",
        ),
        effective_thread_id="$thread_root",
    )

    async def fail_visible_echo(*_args: object, **_kwargs: object) -> None:
        assert queued_signal.pending_human_messages == 0
        raise VisibleEchoTestError

    queued_signal.begin_response_turn()
    try:
        with (
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$thread_root"),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=AsyncMock(return_value=normalized_voice),
            ),
            patch.object(
                bot._turn_controller,
                "_maybe_send_visible_voice_echo",
                new=AsyncMock(side_effect=fail_visible_echo),
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            await bot._on_media_message(room, voice_event)
            await bot._turn_controller.deps.turn_ingress_gate.drain_all()
            await gate.drain_all()
    finally:
        queued_signal.finish_response_turn()

    assert queued_signal.pending_human_messages == 0
    assert not queued_signal.is_set()
    assert len(batches) == 1
    assert batches[0].source_event_ids == ["$voice-echo-fails"]
    assert batches[0].coalescing_class == VOICE_COALESCING_CLASS


@pytest.mark.parametrize(
    ("echo_side_effect", "echo_return"),
    [
        pytest.param(RuntimeError("echo failed"), None, id="failed"),
        pytest.param(None, None, id="disabled"),
    ],
)
@pytest.mark.asyncio
async def test_failed_or_disabled_visible_echo_does_not_affect_canonical_voice_dispatch(
    mock_home_bot: AgentBot,
    echo_side_effect: BaseException | None,
    echo_return: str | None,
) -> None:
    """Visible echo failures or disabled echo should not block canonical voice dispatch."""
    bot = mock_home_bot
    room = _threaded_room()
    _ingress_gate, gate, batches = _install_active_batch_capture_gates(
        bot,
        debounce_seconds=0.0,
        voice_debounce_seconds=0.0,
    )
    voice_event = _make_threaded_voice_event(event_id="$voice-visible-echo")
    normalized_voice = _normalized_voice_result(
        event=voice_event,
        text="canonical voice transcript",
        thread_id="$thread_root",
    )

    with (
        patch.object(
            bot._turn_controller.deps.resolver,
            "coalescing_thread_id",
            new=AsyncMock(return_value="$thread_root"),
        ),
        patch.object(
            bot._turn_controller.deps.normalizer,
            "prepare_voice_event",
            new=AsyncMock(return_value=normalized_voice),
        ),
        patch.object(
            bot._turn_controller,
            "_maybe_send_visible_voice_echo",
            new=AsyncMock(side_effect=echo_side_effect, return_value=echo_return),
        ),
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
    ):
        await bot._on_media_message(room, voice_event)
        await bot._turn_controller.deps.turn_ingress_gate.drain_all()
        await gate.drain_all()

    assert len(batches) == 1
    assert batches[0].source_event_ids == ["$voice-visible-echo"]
    assert batches[0].coalescing_class == VOICE_COALESCING_CLASS


@pytest.mark.asyncio
async def test_voice_preliminary_active_final_active_different_reserves_final_target_once(
    mock_home_bot: AgentBot,
) -> None:
    """A voice retarget between active threads should replace the preliminary reservation."""
    bot = mock_home_bot
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:server"
    room.canonical_alias = None
    room.users = {
        "@mindroom_home:localhost": MagicMock(),
        "@user:example.com": MagicMock(),
    }
    room.members_synced = True

    voice_event = _make_voice_event(
        event_id="$voice-retargeted",
        source={
            "content": {
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$pre_stt_thread"},
            },
        },
    )
    normalized_event = PreparedTextEvent(
        sender=voice_event.sender,
        event_id=voice_event.event_id,
        body="🎤 continue somewhere else",
        source={"content": {"body": "🎤 continue somewhere else", SOURCE_KIND_KEY: "voice"}},
        server_timestamp=voice_event.server_timestamp,
        source_kind_override="voice",
    )
    normalized_voice = inbound_turn_normalizer.VoiceNormalizationResult(
        event=normalized_event,
        effective_thread_id="$post_stt_thread",
    )
    pre_stt_target = bot._turn_controller.deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id="$pre_stt_thread",
        reply_to_event_id=voice_event.event_id,
        event_source=voice_event.source,
    )
    post_stt_target = bot._turn_controller.deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id="$post_stt_thread",
        reply_to_event_id=normalized_event.event_id,
        event_source=normalized_event.source,
    )
    lifecycle = unwrap_extracted_collaborator(bot._response_runner)._lifecycle_coordinator
    pre_stt_signal = lifecycle._get_or_create_queued_signal(pre_stt_target)
    post_stt_signal = lifecycle._get_or_create_queued_signal(post_stt_target)

    enqueued_events: list[tuple[tuple[str, str | None, str], PendingEvent]] = []
    original_reserve = bot._turn_controller.deps.response_runner.reserve_waiting_human_message

    async def capture_gate_enqueue_sealed(
        key: tuple[str, str | None, str],
        pending_events: list[PendingEvent],
    ) -> None:
        assert len(pending_events) == 1
        assert pre_stt_signal.pending_human_messages == 0
        assert post_stt_signal.pending_human_messages == 1
        enqueued_events.append((key, pending_events[0]))

    pre_stt_signal.begin_response_turn()
    post_stt_signal.begin_response_turn()
    pending_event: PendingEvent | None = None
    try:
        with (
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$pre_stt_thread"),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=AsyncMock(return_value=normalized_voice),
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
            patch.object(
                bot._turn_controller.deps.response_runner,
                "reserve_waiting_human_message",
                wraps=original_reserve,
            ) as reserve_waiting_human_message,
            patch.object(
                bot._turn_controller.deps.coalescing_gate,
                "enqueue_sealed_batch",
                new=AsyncMock(side_effect=capture_gate_enqueue_sealed),
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            await bot._on_media_message(room, voice_event)
            await bot._turn_controller.deps.turn_ingress_gate.drain_all()

        reserved_threads = [
            call.kwargs["target"].resolved_thread_id for call in reserve_waiting_human_message.call_args_list
        ]
        assert reserved_threads == ["$pre_stt_thread", "$post_stt_thread"]
        assert len(enqueued_events) == 1
        key, pending_event = enqueued_events[0]
        assert key == (room.room_id, "$post_stt_thread", voice_event.sender)
        assert pending_event.event is normalized_event
        assert pending_event.source_kind == "voice"
        assert pending_event.dispatch_policy_source_kind == ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND
        assert len(pending_event.dispatch_metadata) == 1
        assert pre_stt_signal.pending_human_messages == 0
        assert post_stt_signal.pending_human_messages == 1
    finally:
        if pending_event is not None:
            close_pending_event_metadata([pending_event])
        pre_stt_signal.finish_response_turn()
        post_stt_signal.finish_response_turn()

    assert pre_stt_signal.pending_human_messages == 0
    assert post_stt_signal.pending_human_messages == 0
    assert not pre_stt_signal.is_set()
    assert not post_stt_signal.is_set()


@pytest.mark.asyncio
async def test_room_mode_voice_stays_normal_until_queued_dispatch_owns_it(
    mock_home_bot: AgentBot,
) -> None:
    """Room-mode voice should avoid solo notice metadata before dispatch."""
    bot = mock_home_bot
    bot.config.agents["home"].thread_mode = "room"
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:server"
    room.canonical_alias = None
    room.users = {
        "@mindroom_home:localhost": MagicMock(),
        "@user:example.com": MagicMock(),
    }
    room.members_synced = True

    voice_event = _make_voice_event(
        event_id="$voice-room-mode",
        source={
            "content": {
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        },
    )
    normalized_voice = inbound_turn_normalizer.VoiceNormalizationResult(
        event=PreparedTextEvent(
            sender=voice_event.sender,
            event_id=voice_event.event_id,
            body="🎤 room mode follow-up",
            source={"content": {"body": "🎤 room mode follow-up", SOURCE_KIND_KEY: "voice"}},
            server_timestamp=voice_event.server_timestamp,
            source_kind_override="voice",
        ),
        effective_thread_id=None,
    )
    target = bot._turn_controller.deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id=None,
        reply_to_event_id=voice_event.event_id,
        event_source=voice_event.source,
    )
    lifecycle = unwrap_extracted_collaborator(bot._response_runner)._lifecycle_coordinator
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    prepare_started = asyncio.Event()
    allow_prepare = asyncio.Event()

    async def prepare_voice_event(
        *_args: object,
        **_kwargs: object,
    ) -> inbound_turn_normalizer.VoiceNormalizationResult:
        prepare_started.set()
        await allow_prepare.wait()
        return normalized_voice

    enqueued_events: list[tuple[tuple[str, str | None, str], PendingEvent]] = []

    async def capture_gate_enqueue_sealed(
        key: tuple[str, str | None, str],
        pending_events: list[PendingEvent],
    ) -> None:
        assert len(pending_events) == 1
        assert queued_signal.pending_human_messages == 0
        enqueued_events.append((key, pending_events[0]))

    queued_signal.begin_response_turn()
    task: asyncio.Task[None] | None = None
    try:
        with (
            patch.object(
                bot._turn_controller.deps.resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                bot._turn_controller.deps.normalizer,
                "prepare_voice_event",
                new=AsyncMock(side_effect=prepare_voice_event),
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
            patch.object(
                bot._turn_controller.deps.response_runner,
                "reserve_waiting_human_message",
                new=MagicMock(),
            ) as mock_reserve_waiting_human_message,
            patch.object(
                bot._turn_controller.deps.coalescing_gate,
                "enqueue_sealed_batch",
                new=AsyncMock(side_effect=capture_gate_enqueue_sealed),
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            task = asyncio.create_task(bot._on_media_message(room, voice_event))
            await asyncio.wait_for(prepare_started.wait(), timeout=0.2)
            assert queued_signal.pending_human_messages == 0
            allow_prepare.set()
            await task
            await bot._turn_controller.deps.turn_ingress_gate.drain_all()
    finally:
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        queued_signal.finish_response_turn()

    assert queued_signal.pending_human_messages == 0
    assert not queued_signal.is_set()
    mock_reserve_waiting_human_message.assert_not_called()
    assert len(enqueued_events) == 1
    key, pending_event = enqueued_events[0]
    assert key == (room.room_id, None, voice_event.sender)
    assert pending_event.event is normalized_voice.event
    assert pending_event.source_kind == "voice"
    assert pending_event.dispatch_metadata == ()
