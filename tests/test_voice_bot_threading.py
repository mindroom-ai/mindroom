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

from mindroom import inbound_turn_normalizer
from mindroom.attachments import _attachment_id_for_event, load_attachment
from mindroom.bot import AgentBot
from mindroom.coalescing import CoalescingGate
from mindroom.coalescing_batch import CoalescingKey
from mindroom.config.main import Config
from mindroom.constants import (
    ORIGINAL_SENDER_KEY,
    SOURCE_KIND_KEY,
    VISIBLE_ROUTER_VOICE_ECHO_KEY,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
)
from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_handoff import PreparedTextEvent
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND, VOICE_SOURCE_KIND
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.turn_controller import _PrecheckedEvent
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

if TYPE_CHECKING:
    from mindroom.handled_turns import HandledTurnState


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


def _threaded_room() -> MagicMock:
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:server"
    room.canonical_alias = None
    room.users = {
        "@mindroom_home:localhost": MagicMock(),
        "@mindroom_router:localhost": MagicMock(),
        "@user:example.com": MagicMock(),
    }
    room.members_synced = True
    return room


def _make_threaded_voice_event(
    *,
    event_id: str,
    thread_id: str = "$thread_root",
    server_timestamp: int = 1_712_350_000_000,
) -> nio.RoomMessageAudio:
    return _make_voice_event(
        event_id=event_id,
        server_timestamp=server_timestamp,
        source={
            "event_id": event_id,
            "sender": "@user:example.com",
            "origin_server_ts": server_timestamp,
            "type": "m.room.message",
            "room_id": "!test:server",
            "content": {
                "body": "voice.ogg",
                "msgtype": "m.audio",
                "m.relates_to": {"rel_type": "m.thread", "event_id": thread_id},
            },
        },
    )


def _threaded_text_event(
    *,
    event_id: str,
    body: str,
    thread_id: str = "$thread_root",
    server_timestamp: int = 1_712_350_000_000,
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
                "origin_server_ts": server_timestamp,
                "type": "m.room.message",
                "room_id": "!test:server",
                "content": content,
            },
        ),
    )


def _threaded_prepared_text_event(
    *,
    event_id: str,
    body: str,
    thread_id: str = "$thread_root",
    server_timestamp: int = 1_712_350_000_000,
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
            "origin_server_ts": server_timestamp,
            "type": "m.room.message",
            "room_id": "!test:server",
            "content": content,
        },
        server_timestamp=server_timestamp,
        source_kind_override=source_kind,
    )


def _normalized_voice_result(
    *,
    event: nio.RoomMessageAudio,
    text: str,
    thread_id: str | None = "$thread_root",
) -> inbound_turn_normalizer._VoiceNormalizationResult:
    content: dict[str, object] = {
        "body": text,
        "msgtype": "m.text",
        SOURCE_KIND_KEY: VOICE_SOURCE_KIND,
    }
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    return inbound_turn_normalizer._VoiceNormalizationResult(
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
                "content": content,
            },
            server_timestamp=event.server_timestamp,
            source_kind_override=VOICE_SOURCE_KIND,
        ),
        effective_thread_id=thread_id,
    )


def _handled_source_event_ids(handled_turn: HandledTurnState | None) -> list[str]:
    return list(handled_turn.source_event_ids) if handled_turn is not None else []


def _install_test_coalescing_gate(bot: AgentBot, *, debounce_seconds: float = 0.02) -> None:
    gate = CoalescingGate(
        dispatch_batch=bot._dispatch_coalesced_batch,
        debounce_seconds=lambda: debounce_seconds,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    bot._coalescing_gate = gate
    replace_turn_controller_deps(bot, coalescing_gate=gate)


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
async def test_voice_message_signals_active_turn_before_stt(mock_home_bot: AgentBot) -> None:
    """Audio follow-ups should notify an active response before transcription finishes."""
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
            allow_prepare.set()
            await task
            await drain_coalescing(bot)
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
async def test_voice_message_clears_active_turn_signal_when_post_stt_echo_fails(
    mock_home_bot: AgentBot,
    echo_error: BaseException,
) -> None:
    """Post-STT failures before dispatch handoff should release the pre-STT reservation."""
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
        assert queued_signal.pending_human_messages == 1
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
        ):
            await bot._on_media_message(room, voice_event)
            await drain_coalescing(bot)
    finally:
        queued_signal.finish_response_turn()

    assert queued_signal.pending_human_messages == 0
    assert not queued_signal.is_set()


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
    _install_test_coalescing_gate(bot, debounce_seconds=0.0)
    voice_event = _make_threaded_voice_event(event_id="$voice-visible-echo")
    normalized_voice = _normalized_voice_result(
        event=voice_event,
        text="canonical voice transcript",
        thread_id="$thread_root",
    )
    dispatches: list[tuple[list[str], str]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: PreparedTextEvent | nio.RoomMessageText,
        _requester_user_id: str,
        *,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        dispatches.append((_handled_source_event_ids(handled_turn), dispatched_event.body))

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
        patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)),
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
    ):
        await bot._on_media_message(room, voice_event)
        await drain_coalescing(bot)

    assert dispatches == [(["$voice-visible-echo"], "canonical voice transcript")]


@pytest.mark.asyncio
async def test_voice_message_retargets_queued_notice_when_stt_thread_changes(
    mock_home_bot: AgentBot,
) -> None:
    """A post-STT target change should cancel the pre-STT notice and reserve the final target."""
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
    captured_reservations: list[object] = []

    async def capture_dispatch(*_args: object, **kwargs: object) -> None:
        assert pre_stt_signal.pending_human_messages == 0
        assert post_stt_signal.pending_human_messages == 1
        reservation = kwargs["queued_notice_reservation"]
        assert reservation is not None
        captured_reservations.append(reservation)
        reservation.consume()

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
            patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=capture_dispatch)),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            await bot._on_media_message(room, voice_event)
            await drain_coalescing(bot)
    finally:
        pre_stt_signal.finish_response_turn()
        post_stt_signal.finish_response_turn()

    assert len(captured_reservations) == 1
    assert pre_stt_signal.pending_human_messages == 0
    assert post_stt_signal.pending_human_messages == 0
    assert not pre_stt_signal.is_set()
    assert not post_stt_signal.is_set()


@pytest.mark.asyncio
async def test_room_mode_voice_notice_survives_until_queued_dispatch_owns_it(
    mock_home_bot: AgentBot,
) -> None:
    """Room-mode voice should signal the room-level active turn before STT finishes."""
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
    captured_reservations: list[object] = []
    prepare_started = asyncio.Event()
    allow_prepare = asyncio.Event()

    async def prepare_voice_event(
        *_args: object,
        **_kwargs: object,
    ) -> inbound_turn_normalizer._VoiceNormalizationResult:
        prepare_started.set()
        await allow_prepare.wait()
        return normalized_voice

    async def capture_dispatch(*_args: object, **kwargs: object) -> None:
        assert queued_signal.pending_human_messages == 1
        reservation = kwargs["queued_notice_reservation"]
        assert reservation is not None
        captured_reservations.append(reservation)
        reservation.consume()

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
            patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=capture_dispatch)),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            task = asyncio.create_task(bot._on_media_message(room, voice_event))
            await asyncio.wait_for(prepare_started.wait(), timeout=0.2)
            assert queued_signal.pending_human_messages == 1
            allow_prepare.set()
            await task
            await drain_coalescing(bot)
    finally:
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        queued_signal.finish_response_turn()

    assert len(captured_reservations) == 1
    assert queued_signal.pending_human_messages == 0
    assert not queued_signal.is_set()


@pytest.mark.asyncio
async def test_voice_and_text_followups_during_streaming_coalesce_in_receive_order(
    mock_home_bot: AgentBot,
) -> None:
    """Voice and typed follow-ups sent during one active reply should produce one ordered follow-up."""
    bot = mock_home_bot
    room = _threaded_room()
    _install_test_coalescing_gate(bot, debounce_seconds=0.02)

    streaming_started = asyncio.Event()
    release_streaming = asyncio.Event()
    prepare_started = {"$voice1": asyncio.Event(), "$voice2": asyncio.Event()}
    release_prepare = {"$voice1": asyncio.Event(), "$voice2": asyncio.Event()}
    dispatches: list[tuple[list[str], str]] = []
    wait_timeout = 5.0

    streaming_event = _threaded_prepared_text_event(event_id="$streaming", body="still streaming")
    first_voice = _make_threaded_voice_event(event_id="$voice1", server_timestamp=1_712_350_000_001)
    second_voice = _make_threaded_voice_event(event_id="$voice2", server_timestamp=1_712_350_000_002)
    typed_event = _threaded_text_event(
        event_id="$typed",
        body="typed follow-up",
        server_timestamp=1_712_350_000_003,
    )

    async def prepare_voice_event(
        request: inbound_turn_normalizer.VoiceNormalizationRequest,
    ) -> inbound_turn_normalizer._VoiceNormalizationResult:
        prepare_started[request.event.event_id].set()
        await release_prepare[request.event.event_id].wait()
        return _normalized_voice_result(
            event=request.event,
            text=f"transcript for {request.event.event_id}",
        )

    async def resolve_text_event(
        request: inbound_turn_normalizer.TextNormalizationRequest,
    ) -> PreparedTextEvent:
        return _threaded_prepared_text_event(
            event_id=request.event.event_id,
            body=request.event.body,
            server_timestamp=request.event.server_timestamp,
        )

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: PreparedTextEvent | nio.RoomMessageText,
        _requester_user_id: str,
        *,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        source_ids = _handled_source_event_ids(handled_turn)
        dispatches.append((source_ids, dispatched_event.body))
        if source_ids == ["$streaming"]:
            streaming_started.set()
            await release_streaming.wait()

    first_task: asyncio.Task[None] | None = None
    second_task: asyncio.Task[None] | None = None
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
            patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)),
            patch("mindroom.turn_controller.interactive.handle_text_response", new=AsyncMock(return_value=None)),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            await bot._turn_controller._enqueue_for_dispatch(
                streaming_event,
                room,
                source_kind="message",
                requester_user_id="@user:example.com",
                coalescing_key=CoalescingKey(room.room_id, "$thread_root", "@user:example.com"),
            )
            await asyncio.wait_for(streaming_started.wait(), timeout=wait_timeout)

            first_task = asyncio.create_task(bot._on_media_message(room, first_voice))
            second_task = asyncio.create_task(bot._on_media_message(room, second_voice))
            await asyncio.wait_for(prepare_started["$voice1"].wait(), timeout=wait_timeout)
            await asyncio.wait_for(prepare_started["$voice2"].wait(), timeout=wait_timeout)
            await bot._on_message(room, typed_event)

            release_prepare["$voice1"].set()
            release_prepare["$voice2"].set()
            await asyncio.gather(first_task, second_task)
            release_streaming.set()
            await drain_coalescing(bot)
    finally:
        release_streaming.set()
        for event in release_prepare.values():
            event.set()
        for task in (first_task, second_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    assert dispatches == [
        (["$streaming"], "still streaming"),
        (
            ["$voice1", "$voice2", "$typed"],
            "The user sent the following messages in quick succession. "
            "Treat them as one turn and respond once:\n\n"
            "transcript for $voice1\ntranscript for $voice2\ntyped follow-up",
        ),
    ]


@pytest.mark.asyncio
async def test_voice_first_text_second_uses_receive_order_when_stt_finishes_late(
    mock_home_bot: AgentBot,
) -> None:
    """A later typed message must not jump ahead of an earlier voice event while STT is pending."""
    bot = mock_home_bot
    room = _threaded_room()
    _install_test_coalescing_gate(bot, debounce_seconds=0.02)

    voice_event = _make_threaded_voice_event(event_id="$voice", server_timestamp=1_712_350_000_001)
    typed_event = _threaded_text_event(
        event_id="$typed",
        body="typed follow-up",
        server_timestamp=1_712_350_000_002,
    )
    prepare_started = asyncio.Event()
    release_prepare = asyncio.Event()
    dispatches: list[tuple[list[str], str]] = []

    async def prepare_voice_event(
        request: inbound_turn_normalizer.VoiceNormalizationRequest,
    ) -> inbound_turn_normalizer._VoiceNormalizationResult:
        prepare_started.set()
        await release_prepare.wait()
        return _normalized_voice_result(event=request.event, text="voice transcript")

    async def resolve_text_event(
        request: inbound_turn_normalizer.TextNormalizationRequest,
    ) -> PreparedTextEvent:
        return _threaded_prepared_text_event(
            event_id=request.event.event_id,
            body=request.event.body,
            server_timestamp=request.event.server_timestamp,
        )

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: PreparedTextEvent | nio.RoomMessageText,
        _requester_user_id: str,
        *,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        dispatches.append((_handled_source_event_ids(handled_turn), dispatched_event.body))

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
            patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)),
            patch("mindroom.turn_controller.interactive.handle_text_response", new=AsyncMock(return_value=None)),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            voice_task = asyncio.create_task(bot._on_media_message(room, voice_event))
            await asyncio.wait_for(prepare_started.wait(), timeout=1.0)
            await bot._on_message(room, typed_event)
            await asyncio.sleep(0.05)
            release_prepare.set()
            await voice_task
            await drain_coalescing(bot)
    finally:
        release_prepare.set()
        if voice_task is not None and not voice_task.done():
            voice_task.cancel()
            with suppress(asyncio.CancelledError):
                await voice_task

    assert dispatches == [
        (
            ["$voice", "$typed"],
            "The user sent the following messages in quick succession. "
            "Treat them as one turn and respond once:\n\n"
            "voice transcript\ntyped follow-up",
        ),
    ]


@pytest.mark.asyncio
async def test_voice_admits_before_thread_resolution_so_text_reply_waits(
    mock_home_bot: AgentBot,
) -> None:
    """A typed reply must not outrun an earlier raw voice event while voice ingress resolves."""
    bot = mock_home_bot
    room = _threaded_room()
    _install_test_coalescing_gate(bot, debounce_seconds=0.02)

    voice_event = _make_voice_event(
        event_id="$voice",
        server_timestamp=1_712_350_000_001,
        source={
            "event_id": "$voice",
            "sender": "@user:example.com",
            "origin_server_ts": 1_712_350_000_001,
            "type": "m.room.message",
            "room_id": room.room_id,
            "content": {
                "body": "voice.ogg",
                "msgtype": "m.audio",
            },
        },
    )
    typed_event = _threaded_text_event(
        event_id="$typed",
        body="typed follow-up",
        thread_id="$voice",
        server_timestamp=1_712_350_000_002,
    )
    voice_thread_resolution_started = asyncio.Event()
    release_voice_thread_resolution = asyncio.Event()
    dispatches: list[tuple[list[str], str]] = []

    async def coalescing_thread_id(_room: nio.MatrixRoom, event: object) -> str | None:
        event_id = cast("nio.RoomMessage", event).event_id
        if event_id == "$voice":
            voice_thread_resolution_started.set()
            await release_voice_thread_resolution.wait()
            return None
        if event_id == "$typed":
            return "$voice"
        return None

    async def prepare_voice_event(
        request: inbound_turn_normalizer.VoiceNormalizationRequest,
    ) -> inbound_turn_normalizer._VoiceNormalizationResult:
        return _normalized_voice_result(event=request.event, text="voice transcript", thread_id="$voice")

    async def resolve_text_event(
        request: inbound_turn_normalizer.TextNormalizationRequest,
    ) -> PreparedTextEvent:
        return _threaded_prepared_text_event(
            event_id=request.event.event_id,
            body=request.event.body,
            thread_id="$voice",
            server_timestamp=request.event.server_timestamp,
        )

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: PreparedTextEvent | nio.RoomMessageText,
        _requester_user_id: str,
        *,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        dispatches.append((_handled_source_event_ids(handled_turn), dispatched_event.body))

    voice_task: asyncio.Task[None] | None = None
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
            patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)),
            patch("mindroom.turn_controller.interactive.handle_text_response", new=AsyncMock(return_value=None)),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            voice_task = asyncio.create_task(bot._on_media_message(room, voice_event))
            await asyncio.wait_for(voice_thread_resolution_started.wait(), timeout=1.0)
            await bot._on_message(room, typed_event)
            await asyncio.sleep(0.05)

            assert dispatches == []

            release_voice_thread_resolution.set()
            await voice_task
            await drain_coalescing(bot)
    finally:
        release_voice_thread_resolution.set()
        if voice_task is not None and not voice_task.done():
            voice_task.cancel()
            with suppress(asyncio.CancelledError):
                await voice_task

    assert dispatches == [
        (
            ["$voice", "$typed"],
            "The user sent the following messages in quick succession. "
            "Treat them as one turn and respond once:\n\n"
            "voice transcript\ntyped follow-up",
        ),
    ]


@pytest.mark.asyncio
async def test_plain_reply_voice_to_thread_child_waits_with_thread_root_text(
    mock_home_bot: AgentBot,
) -> None:
    """A plain-reply voice to a thread child should not be outrun by root-scoped typed text."""
    bot = mock_home_bot
    room = _threaded_room()
    _install_test_coalescing_gate(bot, debounce_seconds=0.02)

    voice_event = _make_voice_event(
        event_id="$voice",
        server_timestamp=1_712_350_000_001,
        source={
            "event_id": "$voice",
            "sender": "@user:example.com",
            "origin_server_ts": 1_712_350_000_001,
            "type": "m.room.message",
            "room_id": room.room_id,
            "content": {
                "body": "voice.ogg",
                "msgtype": "m.audio",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_child"}},
            },
        },
    )
    typed_event = _threaded_text_event(
        event_id="$typed",
        body="typed follow-up",
        thread_id="$thread_root",
        server_timestamp=1_712_350_000_002,
    )
    voice_thread_resolution_started = asyncio.Event()
    release_voice_thread_resolution = asyncio.Event()
    dispatches: list[tuple[list[str], str]] = []

    async def coalescing_thread_id(_room: nio.MatrixRoom, event: object) -> str | None:
        event_id = cast("nio.RoomMessage", event).event_id
        if event_id == "$voice":
            voice_thread_resolution_started.set()
            await release_voice_thread_resolution.wait()
        return "$thread_root"

    async def prepare_voice_event(
        request: inbound_turn_normalizer.VoiceNormalizationRequest,
    ) -> inbound_turn_normalizer._VoiceNormalizationResult:
        return _normalized_voice_result(event=request.event, text="voice transcript", thread_id="$thread_root")

    async def resolve_text_event(
        request: inbound_turn_normalizer.TextNormalizationRequest,
    ) -> PreparedTextEvent:
        return _threaded_prepared_text_event(
            event_id=request.event.event_id,
            body=request.event.body,
            thread_id="$thread_root",
            server_timestamp=request.event.server_timestamp,
        )

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: PreparedTextEvent | nio.RoomMessageText,
        _requester_user_id: str,
        *,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        dispatches.append((_handled_source_event_ids(handled_turn), dispatched_event.body))

    voice_task: asyncio.Task[None] | None = None
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
            patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)),
            patch("mindroom.turn_controller.interactive.handle_text_response", new=AsyncMock(return_value=None)),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            voice_task = asyncio.create_task(bot._on_media_message(room, voice_event))
            await asyncio.wait_for(voice_thread_resolution_started.wait(), timeout=1.0)
            await bot._on_message(room, typed_event)
            await asyncio.sleep(0.05)

            assert dispatches == []

            release_voice_thread_resolution.set()
            await voice_task
            await drain_coalescing(bot)
    finally:
        release_voice_thread_resolution.set()
        if voice_task is not None and not voice_task.done():
            voice_task.cancel()
            with suppress(asyncio.CancelledError):
                await voice_task

    assert dispatches == [
        (
            ["$voice", "$typed"],
            "The user sent the following messages in quick succession. "
            "Treat them as one turn and respond once:\n\n"
            "voice transcript\ntyped follow-up",
        ),
    ]


@pytest.mark.asyncio
async def test_room_mode_voice_burst_dispatches_as_one_turn(mock_home_bot: AgentBot) -> None:
    """Room-scoped voice bursts should batch even though their coalescing key has no thread id."""
    bot = mock_home_bot
    bot.config.agents["home"].thread_mode = "room"
    room = _threaded_room()
    _install_test_coalescing_gate(bot, debounce_seconds=0.02)

    first_voice = _make_threaded_voice_event(event_id="$voice1", server_timestamp=1_712_350_000_001)
    second_voice = _make_threaded_voice_event(event_id="$voice2", server_timestamp=1_712_350_000_002)
    dispatches: list[list[str]] = []

    async def prepare_voice_event(
        request: inbound_turn_normalizer.VoiceNormalizationRequest,
    ) -> inbound_turn_normalizer._VoiceNormalizationResult:
        return _normalized_voice_result(
            event=request.event,
            text=f"room transcript {request.event.event_id}",
            thread_id=None,
        )

    async def record_dispatch(
        _room: nio.MatrixRoom,
        _dispatched_event: PreparedTextEvent | nio.RoomMessageText,
        _requester_user_id: str,
        *,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        dispatches.append(_handled_source_event_ids(handled_turn))

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
        patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)),
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
    ):
        await asyncio.gather(
            bot._on_media_message(room, first_voice),
            bot._on_media_message(room, second_voice),
        )
        await drain_coalescing(bot)

    assert dispatches == [["$voice1", "$voice2"]]


@pytest.mark.asyncio
async def test_trusted_router_visible_voice_echo_is_display_only(mock_home_bot: AgentBot) -> None:
    """Trusted router voice echoes should be marked handled and skipped by target agents."""
    bot = mock_home_bot
    room = _threaded_room()
    echo_event = _threaded_prepared_text_event(
        event_id="$echo",
        body="🎤 voice transcript",
        sender="@mindroom_router:localhost",
        source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        content_overrides={
            ORIGINAL_SENDER_KEY: "@user:example.com",
            VISIBLE_ROUTER_VOICE_ECHO_KEY: True,
        },
    )

    with (
        patch.object(
            bot._turn_controller.deps.resolver,
            "coalescing_thread_id",
            new=AsyncMock(return_value="$thread_root"),
        ),
        patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock()) as mock_dispatch,
        patch("mindroom.turn_controller.interactive.handle_text_response", new=AsyncMock(return_value=None)),
    ):
        await bot._turn_controller._dispatch_prepared_text_like_ingress(
            room=room,
            prepared_event=echo_event,
            dispatch_event=echo_event,
            requester_user_id="@user:example.com",
            dispatch_timing=None,
        )
        await drain_coalescing(bot)

    mock_dispatch.assert_not_awaited()
    assert bot._turn_store.is_handled("$echo")


@pytest.mark.asyncio
async def test_forged_visible_voice_echo_marker_still_dispatches(mock_home_bot: AgentBot) -> None:
    """Human-authored visible-echo marker content should not suppress dispatch."""
    bot = mock_home_bot
    room = _threaded_room()
    forged_event = _threaded_prepared_text_event(
        event_id="$forged-echo",
        body="@home this should still dispatch",
        content_overrides={
            ORIGINAL_SENDER_KEY: "@user:example.com",
            SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
            VISIBLE_ROUTER_VOICE_ECHO_KEY: True,
        },
    )

    with (
        patch.object(
            bot._turn_controller.deps.resolver,
            "coalescing_thread_id",
            new=AsyncMock(return_value="$thread_root"),
        ),
        patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock()) as mock_dispatch,
        patch("mindroom.turn_controller.interactive.handle_text_response", new=AsyncMock(return_value=None)),
    ):
        await bot._turn_controller._dispatch_prepared_text_like_ingress(
            room=room,
            prepared_event=forged_event,
            dispatch_event=forged_event,
            requester_user_id="@user:example.com",
            dispatch_timing=None,
        )
        await drain_coalescing(bot)

    mock_dispatch.assert_awaited_once()
    assert not bot._turn_store.is_handled("$forged-echo")


@pytest.mark.asyncio
async def test_ready_voice_event_cancels_thread_resolution_when_cancelled(mock_home_bot: AgentBot) -> None:
    """Cancelling voice readiness should cancel in-progress thread resolution."""
    bot = mock_home_bot
    room = _threaded_room()
    voice_event = _make_threaded_voice_event(event_id="$voice-cancelled")
    thread_resolution_started = asyncio.Event()
    never_release = asyncio.Event()

    async def waiting_thread_id(*_args: object) -> str | None:
        thread_resolution_started.set()
        await never_release.wait()
        return "$thread_root"

    with patch.object(
        bot._turn_controller.deps.resolver,
        "coalescing_thread_id",
        new=AsyncMock(side_effect=waiting_thread_id),
    ):
        ready_task = asyncio.create_task(
            bot._turn_controller._ready_voice_event(
                room=room,
                prechecked_event=_PrecheckedEvent(event=voice_event, requester_user_id="@user:example.com"),
                event_info=EventInfo.from_event(voice_event.source),
                dispatch_timing=None,
            ),
        )

        await thread_resolution_started.wait()
        ready_task.cancel()
        with suppress(asyncio.CancelledError):
            await ready_task

    assert ready_task.cancelled()


@pytest.mark.asyncio
async def test_raw_voice_normalization_exception_dispatches_audio_fallback(mock_home_bot: AgentBot) -> None:
    """Unexpected normalization errors should terminate visibly instead of dropping live ingress."""
    bot = mock_home_bot
    room = _threaded_room()
    voice_event = _make_threaded_voice_event(event_id="$audio-fails")
    dispatches: list[tuple[PreparedTextEvent | nio.RoomMessageText, list[str]]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: PreparedTextEvent | nio.RoomMessageText,
        _requester_user_id: str,
        *,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        dispatches.append((dispatched_event, _handled_source_event_ids(handled_turn)))

    with (
        patch.object(
            bot._turn_controller.deps.resolver,
            "coalescing_thread_id",
            new=AsyncMock(return_value="$thread_root"),
        ),
        patch.object(
            bot._turn_controller.deps.normalizer,
            "prepare_voice_event",
            new=AsyncMock(side_effect=RuntimeError("stt failed")),
        ),
        patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
        patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)),
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
    ):
        await bot._on_media_message(room, voice_event)
        await drain_coalescing(bot)

    assert len(dispatches) == 1
    dispatched_event, handled_source_ids = dispatches[0]
    assert isinstance(dispatched_event, PreparedTextEvent)
    assert dispatched_event.body == "🎤 [Attached voice message]"
    assert dispatched_event.source["content"][SOURCE_KIND_KEY] == VOICE_SOURCE_KIND
    assert dispatched_event.source["content"][VOICE_RAW_AUDIO_FALLBACK_KEY] is True
    assert dispatched_event.source["content"]["m.relates_to"] == {
        "rel_type": "m.thread",
        "event_id": "$thread_root",
    }
    assert handled_source_ids == ["$audio-fails"]


@pytest.mark.asyncio
async def test_raw_voice_thread_resolution_exception_does_not_dispatch_guess(mock_home_bot: AgentBot) -> None:
    """Thread-resolution failures should not guess a fallback dispatch target."""
    bot = mock_home_bot
    room = _threaded_room()
    voice_event = _make_threaded_voice_event(event_id="$thread-resolution-fails")
    dispatches: list[tuple[PreparedTextEvent | nio.RoomMessageText, list[str]]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: PreparedTextEvent | nio.RoomMessageText,
        _requester_user_id: str,
        *,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        dispatches.append((dispatched_event, _handled_source_event_ids(handled_turn)))

    with (
        patch.object(
            bot._turn_controller.deps.resolver,
            "coalescing_thread_id",
            new=AsyncMock(side_effect=RuntimeError("thread lookup failed")),
        ),
        patch.object(bot._turn_controller.deps.normalizer, "prepare_voice_event", new=AsyncMock()),
        patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()),
        patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)),
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
    ):
        await bot._on_media_message(room, voice_event)
        await drain_coalescing(bot)

    assert dispatches == []
    assert not bot._turn_store.is_handled("$thread-resolution-fails")
