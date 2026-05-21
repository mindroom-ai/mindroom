"""Test that direct audio responses preserve thread structure."""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from agno.media import Audio

from mindroom import inbound_turn_normalizer
from mindroom.attachments import _attachment_id_for_event, load_attachment
from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.constants import SOURCE_KIND_KEY
from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_handoff import PreparedTextEvent
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from tests.conftest import (
    TEST_ACCESS_TOKEN,
    TEST_PASSWORD,
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
    normalized_voice = inbound_turn_normalizer._VoiceNormalizationResult(
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
    normalized_voice = inbound_turn_normalizer._VoiceNormalizationResult(
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

    async def capture_enqueue(*_args: object, **kwargs: object) -> None:
        assert pre_stt_signal.pending_human_messages == 0
        assert post_stt_signal.pending_human_messages == 0
        assert kwargs["queued_notice_reservation"] is None

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
            patch.object(bot._turn_controller, "_enqueue_for_dispatch", new=AsyncMock(side_effect=capture_enqueue)),
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
    normalized_voice = inbound_turn_normalizer._VoiceNormalizationResult(
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
    ) -> inbound_turn_normalizer._VoiceNormalizationResult:
        prepare_started.set()
        await allow_prepare.wait()
        return normalized_voice

    async def capture_enqueue(*_args: object, **kwargs: object) -> None:
        assert queued_signal.pending_human_messages == 0
        assert kwargs["queued_notice_reservation"] is None

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
            patch.object(bot._turn_controller, "_enqueue_for_dispatch", new=AsyncMock(side_effect=capture_enqueue)),
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
