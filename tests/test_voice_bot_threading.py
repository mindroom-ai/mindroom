"""Test that direct audio responses preserve thread structure."""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from agno.media import Audio

from mindroom import inbound_turn_normalizer
from mindroom.attachments import _attachment_id_for_event, load_attachment
from mindroom.bot import AgentBot
from mindroom.coalescing import CoalescingGate
from mindroom.config.main import Config
from mindroom.constants import ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_handoff import PendingDispatchMetadata, PreparedTextEvent
from mindroom.dispatch_source import (
    ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    VOICE_SOURCE_KIND,
)
from mindroom.hooks import MessageEnvelope
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.voice_coalescing import (
    TextIngressItem,
    VoiceCoalescingGate,
    VoiceIngressBatch,
    VoiceIngressItem,
    VoiceNormalizationOutcome,
)
from tests.conftest import (
    TEST_ACCESS_TOKEN,
    TEST_PASSWORD,
    bind_runtime_paths,
    dispatch_context_result,
    drain_coalescing,
    install_generate_response_mock,
    install_runtime_cache_support,
    message_origin,
    replace_turn_controller_deps,
    runtime_paths_for,
    sync_bot_runtime_state,
    test_runtime_paths,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)

if TYPE_CHECKING:
    from mindroom.coalescing_batch import CoalescedBatch, PendingEvent
    from mindroom.handled_turns import HandledTurnState


class VoiceNormalizationTestError(Exception):
    """Test-only STT failure."""


class VoiceFlushTestError(Exception):
    """Test-only voice flush failure."""


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
) -> PreparedTextEvent:
    return PreparedTextEvent(
        sender="@user:example.com",
        event_id=event_id,
        body=body,
        source={
            "event_id": event_id,
            "sender": "@user:example.com",
            "origin_server_ts": 1_712_350_000_000,
            "type": "m.room.message",
            "room_id": "!test:server",
            "content": {
                "body": body,
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": thread_id},
            },
        },
        server_timestamp=1_712_350_000_000,
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


async def _immediate_normalized_voice_result(
    event: nio.RoomMessageAudio,
) -> inbound_turn_normalizer.VoiceNormalizationResult:
    return _normalized_voice_result(event=event, text=f"transcript for {event.event_id}")


def _voice_ingress_item(room: nio.MatrixRoom, event: nio.RoomMessageAudio) -> VoiceIngressItem:
    return VoiceIngressItem(
        room=room,
        event=event,
        requester_user_id=event.sender,
        coalescing_thread_id="$thread_root",
        normalization_task=asyncio.create_task(_immediate_normalized_voice_result(event)),
        dispatch_timing=None,
    )


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


def _install_streaming_test_gate(bot: AgentBot) -> None:
    voice_gate = VoiceCoalescingGate(
        debounce_seconds=lambda: 0.01,
        is_shutting_down=lambda: False,
    )
    gate = CoalescingGate(
        dispatch_batch=bot._dispatch_coalesced_batch,
        debounce_seconds=lambda: 0.01,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
        has_pending_external_voice_burst=voice_gate.has_pending_voice_burst,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate, voice_coalescing_gate=voice_gate)
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
async def test_voice_message_does_not_reserve_active_turn_before_stt(mock_home_bot: AgentBot) -> None:
    """Audio follow-ups should remain coalescible while transcription is pending."""
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
            assert queued_signal.pending_human_messages == 0
            allow_prepare.set()
            await task
    finally:
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        queued_signal.finish_response_turn()

    assert queued_signal.pending_human_messages == 0
    assert not queued_signal.is_set()


@pytest.mark.asyncio
async def test_voice_gate_drain_all_includes_voice_enqueued_while_draining() -> None:
    """Voice received during a forced drain should be flushed and resolve its waiter."""
    gate = VoiceCoalescingGate(
        debounce_seconds=lambda: 0.05,
        is_shutting_down=lambda: False,
    )
    room = _threaded_room()
    first_voice = _make_threaded_voice_event(event_id="$voice-before-drain")
    second_voice = _make_threaded_voice_event(event_id="$voice-during-drain")
    first_key = (room.room_id, "$thread_root", first_voice.sender)
    second_key = (room.room_id, "$other_thread", second_voice.sender)
    first_flush_started = asyncio.Event()
    release_first_flush = asyncio.Event()
    flushed_batches: list[tuple[str, ...]] = []

    async def flush_batch(batch: VoiceIngressBatch) -> None:
        event_ids = tuple(outcome.item.event.event_id for outcome in batch.voice_outcomes)
        flushed_batches.append(event_ids)
        if event_ids == ("$voice-before-drain",):
            first_flush_started.set()
            await release_first_flush.wait()

    first_task = asyncio.create_task(
        gate.enqueue_voice(
            first_key,
            _voice_ingress_item(room, first_voice),
            flush_batch=flush_batch,
        ),
    )
    drain_task: asyncio.Task[None] | None = None
    second_task: asyncio.Task[None] | None = None
    try:
        await asyncio.sleep(0)
        drain_task = asyncio.create_task(gate.drain_all())
        await asyncio.wait_for(first_flush_started.wait(), timeout=1.0)

        second_task = asyncio.create_task(
            gate.enqueue_voice(
                second_key,
                _voice_ingress_item(room, second_voice),
                flush_batch=flush_batch,
            ),
        )
        await asyncio.sleep(0)
        release_first_flush.set()

        await asyncio.wait_for(asyncio.gather(first_task, drain_task, second_task), timeout=1.0)
    finally:
        release_first_flush.set()
        for task in (first_task, drain_task, second_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        await asyncio.sleep(0.06)

    assert flushed_batches == [
        ("$voice-before-drain",),
        ("$voice-during-drain",),
    ]


@pytest.mark.asyncio
async def test_voice_gate_drain_all_waits_for_claimed_flush_to_finish() -> None:
    """Forced drain must await already-claimed voice drain tasks."""
    gate = VoiceCoalescingGate(
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    room = _threaded_room()
    voice_event = _make_threaded_voice_event(event_id="$voice-claimed")
    key = (room.room_id, "$thread_root", voice_event.sender)
    flush_started = asyncio.Event()
    release_flush = asyncio.Event()
    flushed_batches: list[tuple[str, ...]] = []

    async def flush_batch(batch: VoiceIngressBatch) -> None:
        flush_started.set()
        await release_flush.wait()
        flushed_batches.append(tuple(outcome.item.event.event_id for outcome in batch.voice_outcomes))

    enqueue_task = asyncio.create_task(
        gate.enqueue_voice(
            key,
            _voice_ingress_item(room, voice_event),
            flush_batch=flush_batch,
        ),
    )
    drain_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(flush_started.wait(), timeout=1.0)
        drain_task = asyncio.create_task(gate.drain_all())
        await asyncio.sleep(0.02)

        assert not drain_task.done()

        release_flush.set()
        await asyncio.wait_for(asyncio.gather(enqueue_task, drain_task), timeout=1.0)
    finally:
        release_flush.set()
        for task in (enqueue_task, drain_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    assert flushed_batches == [("$voice-claimed",)]


@pytest.mark.asyncio
async def test_overlapping_claimed_voice_bursts_keep_late_text_on_original_claim(
    mock_home_bot: AgentBot,
) -> None:
    """A second claimed burst on the same key must not replace the first burst's late-text buffer."""
    bot = mock_home_bot
    room = _threaded_room()
    gate = VoiceCoalescingGate(
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, voice_coalescing_gate=gate)

    raw_key = (room.room_id, "$thread_root", "@user:example.com")
    first_voice = _make_threaded_voice_event(event_id="$voice-v1")
    second_voice = _make_threaded_voice_event(event_id="$voice-v2")
    late_text = _threaded_prepared_text_event(
        event_id="$late-text-for-v1",
        body="late typed follow-up for v1",
    )
    _text_key, text_pending_event, _source_kind = await bot._turn_controller._build_pending_event_for_dispatch(
        late_text,
        room,
        source_kind=MESSAGE_SOURCE_KIND,
        dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        requester_user_id="@user:example.com",
        coalescing_key=raw_key,
    )

    flush_started = {
        "$voice-v1": asyncio.Event(),
        "$voice-v2": asyncio.Event(),
    }
    release_flush = {
        "$voice-v1": asyncio.Event(),
        "$voice-v2": asyncio.Event(),
    }
    flushed_event_ids: dict[str, set[str]] = {}

    async def capture_pending_by_key(
        pending_by_key: dict[tuple[str, str | None, str], list[tuple[float, PendingEvent]]],
    ) -> None:
        event_ids = {
            pending_event.event.event_id
            for pending_entries in pending_by_key.values()
            for _received_at, pending_event in pending_entries
        }
        voice_event_id = next(event_id for event_id in event_ids if event_id.startswith("$voice-v"))
        flushed_event_ids[voice_event_id] = event_ids

    async def flush_batch(batch: VoiceIngressBatch) -> None:
        voice_event_id = batch.voice_outcomes[0].item.event.event_id
        flush_started[voice_event_id].set()
        await release_flush[voice_event_id].wait()
        await bot._turn_controller._flush_voice_ingress_batch(batch)

    first_task = asyncio.create_task(
        gate.enqueue_voice(
            raw_key,
            _voice_ingress_item(room, first_voice),
            flush_batch=flush_batch,
        ),
    )
    second_task: asyncio.Task[None] | None = None
    try:
        with (
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
            patch.object(
                bot._turn_controller,
                "_enqueue_voice_ingress_pending_events",
                new=AsyncMock(side_effect=capture_pending_by_key),
            ),
        ):
            await asyncio.wait_for(flush_started["$voice-v1"].wait(), timeout=1.0)

            accepted = gate.enqueue_text_if_voice_pending(
                raw_key,
                TextIngressItem(pending_event=text_pending_event),
            )
            assert accepted

            second_task = asyncio.create_task(
                gate.enqueue_voice(
                    raw_key,
                    _voice_ingress_item(room, second_voice),
                    flush_batch=flush_batch,
                ),
            )
            await asyncio.wait_for(flush_started["$voice-v2"].wait(), timeout=1.0)

            release_flush["$voice-v1"].set()
            await asyncio.wait_for(first_task, timeout=1.0)
            release_flush["$voice-v2"].set()
            await asyncio.wait_for(second_task, timeout=1.0)
    finally:
        for release in release_flush.values():
            release.set()
        for task in (first_task, second_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    assert "$late-text-for-v1" in flushed_event_ids["$voice-v1"]
    assert "$late-text-for-v1" not in flushed_event_ids["$voice-v2"]


@pytest.mark.asyncio
async def test_claimed_voice_late_text_metadata_closes_when_flush_fails(
    mock_home_bot: AgentBot,
) -> None:
    """Late text accepted by a claimed voice burst must close metadata if flush never consumes it."""
    bot = mock_home_bot
    room = _threaded_room()
    gate = VoiceCoalescingGate(
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    raw_key = (room.room_id, "$thread_root", "@user:example.com")
    voice_event = _make_threaded_voice_event(event_id="$voice-flush-fails")
    late_text = _threaded_prepared_text_event(
        event_id="$late-text-flush-fails",
        body="late typed follow-up before flush failure",
    )
    _text_key, text_pending_event, _source_kind = await bot._turn_controller._build_pending_event_for_dispatch(
        late_text,
        room,
        source_kind=MESSAGE_SOURCE_KIND,
        dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        requester_user_id="@user:example.com",
        coalescing_key=raw_key,
    )
    metadata_closed = False

    def close_metadata() -> None:
        nonlocal metadata_closed
        metadata_closed = True

    text_pending_event.dispatch_metadata = (
        PendingDispatchMetadata(
            kind="test_late_text_metadata",
            payload=None,
            close=close_metadata,
            requires_solo_batch=True,
        ),
    )

    flush_started = asyncio.Event()
    release_flush = asyncio.Event()

    async def flush_batch(_batch: VoiceIngressBatch) -> None:
        flush_started.set()
        await release_flush.wait()
        raise VoiceFlushTestError

    task = asyncio.create_task(
        gate.enqueue_voice(
            raw_key,
            _voice_ingress_item(room, voice_event),
            flush_batch=flush_batch,
        ),
    )
    try:
        await asyncio.wait_for(flush_started.wait(), timeout=1.0)
        accepted = gate.enqueue_text_if_voice_pending(
            raw_key,
            TextIngressItem(pending_event=text_pending_event),
        )
        assert accepted

        release_flush.set()
        with pytest.raises(VoiceFlushTestError):
            await asyncio.wait_for(task, timeout=1.0)
    finally:
        release_flush.set()
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    assert metadata_closed


@pytest.mark.asyncio
async def test_voice_normalization_error_marks_audio_source_handled(mock_home_bot: AgentBot) -> None:
    """Failed raw voice normalization should still terminally handle the source event."""
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
        pytest.raises(VoiceNormalizationTestError),
    ):
        await bot._on_media_message(room, voice_event)

    assert bot._turn_store.is_handled("$voice-error")


@pytest.mark.asyncio
async def test_voice_burst_retargets_captured_text_to_single_successful_voice_key(
    mock_home_bot: AgentBot,
) -> None:
    """Text captured before STT should follow the final voice thread when the burst resolves once."""
    bot = mock_home_bot
    room = _threaded_room()
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate)

    voice_event = _make_threaded_voice_event(event_id="$voice-retargeted", thread_id="$pre_stt_thread")
    normalized_voice = _normalized_voice_result(
        event=voice_event,
        text="transcript after retarget",
        thread_id="$post_stt_thread",
    )
    text_event = _threaded_prepared_text_event(
        event_id="$typed",
        body="typed follow-up",
        thread_id="$pre_stt_thread",
    )
    _text_key, text_pending_event, _source_kind = await bot._turn_controller._build_pending_event_for_dispatch(
        text_event,
        room,
        source_kind=MESSAGE_SOURCE_KIND,
        dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        requester_user_id="@user:example.com",
        coalescing_key=(room.room_id, "$pre_stt_thread", "@user:example.com"),
    )
    batch = VoiceIngressBatch(
        key=(room.room_id, "$pre_stt_thread", "@user:example.com"),
        voice_outcomes=(
            VoiceNormalizationOutcome(
                item=VoiceIngressItem(
                    room=room,
                    event=voice_event,
                    requester_user_id="@user:example.com",
                    coalescing_thread_id="$pre_stt_thread",
                    normalization_task=asyncio.create_task(_immediate_normalized_voice_result(voice_event)),
                    dispatch_timing=None,
                ),
                result=normalized_voice,
            ),
        ),
        text_items=(TextIngressItem(pending_event=text_pending_event),),
    )

    with patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()):
        await bot._turn_controller._flush_voice_ingress_batch(batch)
        await gate.drain_all()

    assert len(batches) == 1
    assert set(batches[0].source_event_ids) == {"$typed", "$voice-retargeted"}
    assert "typed follow-up" in batches[0].prompt
    assert "transcript after retarget" in batches[0].prompt


@pytest.mark.asyncio
async def test_late_text_after_voice_claim_retargets_with_successful_voice_key(
    mock_home_bot: AgentBot,
) -> None:
    """Text arriving after raw voice claim should still follow a retargeted voice handoff."""
    bot = mock_home_bot
    room = _threaded_room()
    voice_gate = VoiceCoalescingGate(
        debounce_seconds=lambda: 0.0,
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
        has_pending_external_voice_burst=voice_gate.has_pending_voice_burst,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate, voice_coalescing_gate=voice_gate)
    bot._turn_controller.deps.response_runner.has_active_response_for_target = MagicMock(return_value=True)
    bot._turn_controller.deps.response_runner.reserve_waiting_human_message = MagicMock(return_value=None)

    raw_key = (room.room_id, "$thread_root", "@user:example.com")
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

    async def wait_for_voice_claim() -> None:
        for _ in range(50):
            if raw_key not in voice_gate._entries and voice_gate.has_pending_voice_burst(raw_key):
                return
            await asyncio.sleep(0)
        pytest.fail("voice burst was not claimed before late text")

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
            await wait_for_voice_claim()

            await bot._turn_controller._dispatch_prepared_text_like_ingress(
                room=room,
                prepared_event=text_event,
                dispatch_event=text_event,
                requester_user_id="@user:example.com",
                dispatch_timing=None,
            )

            release_stt.set()
            await asyncio.wait_for(voice_task, timeout=1.0)
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
            await bot._turn_controller._enqueue_for_dispatch(
                streaming_event,
                room,
                source_kind=MESSAGE_SOURCE_KIND,
                requester_user_id="@user:example.com",
                coalescing_key=(room.room_id, "$thread_root", "@user:example.com"),
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
            await bot._turn_controller._enqueue_for_dispatch(
                streaming_event,
                room,
                source_kind=MESSAGE_SOURCE_KIND,
                requester_user_id="@user:example.com",
                coalescing_key=(room.room_id, "$thread_root", "@user:example.com"),
            )
            await asyncio.wait_for(streaming_started.wait(), timeout=1.0)

            await bot._turn_controller._dispatch_prepared_text_like_ingress(
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
async def test_text_then_raw_voice_waits_while_voice_handoff_is_suspended(
    mock_home_bot: AgentBot,
) -> None:
    """Text queued before voice should not dispatch while the voice batch handoff is paused."""
    bot = mock_home_bot
    room = _threaded_room()
    _install_streaming_test_gate(bot)

    typed_followup = _threaded_prepared_text_event(event_id="$typed", body="typed follow-up")
    voice_event = _make_threaded_voice_event(event_id="$voice1")
    started, releases, prepare_voice_event = _staggered_voice_normalizer()
    streaming_started, release_streaming, dispatches, record_dispatch = _streaming_dispatch_recorder()
    voice_handoff_started = asyncio.Event()
    release_voice_handoff = asyncio.Event()

    async def suspend_voice_handoff(*_args: object, **_kwargs: object) -> None:
        voice_handoff_started.set()
        await release_voice_handoff.wait()

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
                new=AsyncMock(side_effect=suspend_voice_handoff),
            ),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            await bot._turn_controller._enqueue_for_dispatch(
                streaming_event,
                room,
                source_kind=MESSAGE_SOURCE_KIND,
                requester_user_id="@user:example.com",
                coalescing_key=(room.room_id, "$thread_root", "@user:example.com"),
            )
            await asyncio.wait_for(streaming_started.wait(), timeout=1.0)

            await bot._turn_controller._dispatch_prepared_text_like_ingress(
                room=room,
                prepared_event=typed_followup,
                dispatch_event=typed_followup,
                requester_user_id="@user:example.com",
                dispatch_timing=None,
            )
            voice_task = asyncio.create_task(bot._on_media_message(room, voice_event))
            await asyncio.wait_for(started["$voice1"].wait(), timeout=1.0)

            releases["$voice1"].set()
            await asyncio.wait_for(voice_handoff_started.wait(), timeout=1.0)
            release_streaming.set()
            await asyncio.sleep(0.05)

            assert dispatches == [(["$streaming"], "still streaming")]

            release_voice_handoff.set()
            await voice_task
            await drain_coalescing(bot)
    finally:
        release_streaming.set()
        release_voice_handoff.set()
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
            await bot._turn_controller._enqueue_for_dispatch(
                streaming_event,
                room,
                source_kind=MESSAGE_SOURCE_KIND,
                requester_user_id="@user:example.com",
                coalescing_key=(room.room_id, "$thread_root", "@user:example.com"),
            )
            await asyncio.wait_for(streaming_started.wait(), timeout=1.0)

            first_task = asyncio.create_task(bot._on_media_message(room, first_voice))
            second_task = asyncio.create_task(bot._on_media_message(room, second_voice))
            await asyncio.wait_for(started["$voice1"].wait(), timeout=1.0)
            await asyncio.wait_for(started["$voice2"].wait(), timeout=1.0)

            await bot._turn_controller._dispatch_prepared_text_like_ingress(
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
async def test_non_voice_trusted_relay_does_not_join_pending_voice_burst(
    mock_home_bot: AgentBot,
) -> None:
    """Trusted relays must preserve bypass behavior even while raw voice STT is pending."""
    bot = mock_home_bot
    room = _threaded_room()
    relay_event = PreparedTextEvent(
        sender="@mindroom_home:localhost",
        event_id="$relay",
        body="trusted relay follow-up",
        source={
            "event_id": "$relay",
            "sender": "@mindroom_home:localhost",
            "origin_server_ts": 1_712_350_000_000,
            "type": "m.room.message",
            "room_id": room.room_id,
            "content": {
                "body": "trusted relay follow-up",
                "msgtype": "m.text",
                ORIGINAL_SENDER_KEY: "@user:example.com",
                SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        },
        server_timestamp=1_712_350_000_000,
    )
    target = MessageTarget.resolve(room.room_id, "$thread_root", "$relay")
    envelope = MessageEnvelope(
        source_event_id="$relay",
        room_id=room.room_id,
        target=target,
        requester_id="@user:example.com",
        sender_id="@mindroom_home:localhost",
        body=relay_event.body,
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="home",
        source_kind=MESSAGE_SOURCE_KIND,
        origin=message_origin(
            sender_id="@mindroom_home:localhost",
            requester_id="@user:example.com",
            source_kind=MESSAGE_SOURCE_KIND,
        ),
    )

    with (
        patch.object(
            bot._turn_controller.deps.voice_coalescing_gate,
            "has_pending_voice_burst",
            return_value=True,
        ),
        patch.object(
            bot._turn_controller.deps.voice_coalescing_gate,
            "enqueue_text_if_voice_pending",
            return_value=True,
        ) as mock_enqueue_text_if_voice_pending,
    ):
        accepted = await bot._turn_controller._try_enqueue_active_follow_up_with_pending_voice(
            room=room,
            event=relay_event,
            envelope=envelope,
            coalescing_thread_id="$thread_root",
            requester_user_id="@user:example.com",
            trust_internal_payload_metadata=None,
        )

    assert not accepted
    mock_enqueue_text_if_voice_pending.assert_not_called()


@pytest.mark.parametrize(
    "echo_error",
    [
        RuntimeError("echo failed"),
        asyncio.CancelledError(),
    ],
)
@pytest.mark.asyncio
async def test_voice_message_does_not_reserve_active_turn_signal_when_post_stt_echo_fails(
    mock_home_bot: AgentBot,
    echo_error: BaseException,
) -> None:
    """Post-STT failures before dispatch handoff should not leave a voice reservation."""
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
        raise echo_error

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
            pytest.raises(type(echo_error)),
        ):
            await bot._on_media_message(room, voice_event)
    finally:
        queued_signal.finish_response_turn()

    assert queued_signal.pending_human_messages == 0
    assert not queued_signal.is_set()


@pytest.mark.asyncio
async def test_voice_message_keeps_normal_dispatch_when_stt_thread_changes(
    mock_home_bot: AgentBot,
) -> None:
    """A post-STT target change should not create solo voice dispatch metadata."""
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

    async def capture_gate_enqueue(key: tuple[str, str | None, str], pending_event: PendingEvent) -> None:
        assert pre_stt_signal.pending_human_messages == 0
        assert post_stt_signal.pending_human_messages == 0
        enqueued_events.append((key, pending_event))

    pre_stt_signal.begin_response_turn()
    post_stt_signal.begin_response_turn()
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
                new=MagicMock(),
            ) as mock_reserve_waiting_human_message,
            patch.object(
                bot._turn_controller.deps.coalescing_gate,
                "enqueue",
                new=AsyncMock(side_effect=capture_gate_enqueue),
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            await bot._on_media_message(room, voice_event)
    finally:
        pre_stt_signal.finish_response_turn()
        post_stt_signal.finish_response_turn()

    assert pre_stt_signal.pending_human_messages == 0
    assert post_stt_signal.pending_human_messages == 0
    assert not pre_stt_signal.is_set()
    assert not post_stt_signal.is_set()
    mock_reserve_waiting_human_message.assert_not_called()
    assert len(enqueued_events) == 1
    key, pending_event = enqueued_events[0]
    assert key == (room.room_id, "$post_stt_thread", voice_event.sender)
    assert pending_event.event is normalized_event
    assert pending_event.source_kind == "voice"
    assert pending_event.dispatch_metadata == ()


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

    async def capture_gate_enqueue(key: tuple[str, str | None, str], pending_event: PendingEvent) -> None:
        assert queued_signal.pending_human_messages == 0
        enqueued_events.append((key, pending_event))

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
                "enqueue",
                new=AsyncMock(side_effect=capture_gate_enqueue),
            ),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            task = asyncio.create_task(bot._on_media_message(room, voice_event))
            await asyncio.wait_for(prepare_started.wait(), timeout=0.2)
            assert queued_signal.pending_human_messages == 0
            allow_prepare.set()
            await task
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
