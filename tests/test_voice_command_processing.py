"""Test audio normalization and dispatch through the shared text/media flow."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from agno.media import Audio

from mindroom.attachments import _attachment_id_for_event, load_attachment
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    SKIP_MENTIONS_KEY,
    SOURCE_KIND_KEY,
    VOICE_PREFIX,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
    VOICE_TRANSCRIPT_KEY,
)
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.dispatch_handoff import PreparedTextEvent
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND, VOICE_SOURCE_KIND
from mindroom.handled_turns import TurnRecord
from mindroom.history.types import HistoryScope
from mindroom.matrix.cache.thread_history_result import thread_history_result
from mindroom.matrix.identity import MatrixID
from mindroom.message_target import MessageTarget
from mindroom.visible_voice_echo import VisibleVoiceEchoRequest
from mindroom.voice_handler import prepare_voice_message
from tests.conftest import (
    bind_runtime_paths,
    drain_coalescing,
    install_edit_message_mock,
    install_generate_response_mock,
    install_runtime_cache_support,
    install_send_response_mock,
    orchestrator_runtime_paths,
    replace_turn_controller_deps,
    runtime_paths_for,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.delivery_gateway import EditTextRequest


def _attach_runtime_paths(config: Config, tmp_path: Path) -> Config:
    return bind_runtime_paths(config, orchestrator_runtime_paths(tmp_path, config_path=tmp_path / "config.yaml"))


def _agent_bot(*, agent_user: object, storage_path: Path, config: Config, rooms: list[str]) -> AgentBot:
    """Construct an agent bot with the explicit runtime bound to the test config."""
    bot = install_runtime_cache_support(
        AgentBot(
            agent_user=agent_user,
            storage_path=storage_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
            rooms=rooms,
        ),
    )
    wrap_extracted_collaborators(bot)
    return bot


async def _prepare_voice_message_with_runtime(
    client: object,
    storage_path: Path,
    room: nio.MatrixRoom,
    event: nio.RoomMessageAudio | nio.RoomEncryptedAudio,
    config: Config,
    *,
    thread_id: str | None,
) -> object:
    """Normalize voice input with the test config's explicit runtime context."""
    return await prepare_voice_message(
        client,
        storage_path,
        room,
        event,
        config,
        runtime_paths=runtime_paths_for(config),
        thread_id=thread_id,
    )


def _make_voice_event(
    *,
    sender: str,
    event_id: str = "$voice_event",
    body: str = "voice.ogg",
    source: dict | None = None,
    server_timestamp: int = 1_712_350_000_000,
) -> nio.RoomMessageAudio:
    event = MagicMock(spec=nio.RoomMessageAudio)
    event.sender = sender
    event.event_id = event_id
    event.body = body
    event.server_timestamp = server_timestamp
    event.source = source or {"content": {"body": body}}
    return event


def _make_room(*user_ids: str) -> nio.MatrixRoom:
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:example.com"
    room.canonical_alias = None
    room.users = {user_id: MagicMock() for user_id in user_ids}
    room.members_synced = True
    return room


def _install_voice_thread_dispatch_mocks(
    bot: AgentBot,
) -> None:
    """Provide minimal explicit-thread cache reads for normalized voice dispatch."""
    bot._conversation_cache.get_dispatch_thread_snapshot = AsyncMock(
        return_value=thread_history_result([], is_full_history=False),
    )
    bot._conversation_cache.get_dispatch_thread_history = AsyncMock(
        return_value=thread_history_result([], is_full_history=True),
    )


def _make_visible_router_echo_scenario(
    tmp_path: Path,
    *,
    agents: dict | None = None,
    authorization: dict | None = None,
    voice_enabled: bool = True,
    send_response_return: str | None = "$voice_echo",
    send_response_side_effect: list[str] | None = None,
) -> tuple[AgentBot, nio.MatrixRoom, nio.RoomMessageAudio]:
    """Build a router bot + room + voice event for visible echo tests."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:localhost"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:localhost")

    configured_agents = agents or {"home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]}}
    config = _attach_runtime_paths(
        Config(
            agents=configured_agents,
            authorization=authorization or {"default_room_access": True},
            voice={"enabled": voice_enabled, "visible_router_echo": True},
        ),
        tmp_path,
    )

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    _install_voice_thread_dispatch_mocks(bot)
    send_response = AsyncMock()
    if send_response_side_effect is not None:
        send_response.side_effect = send_response_side_effect
    else:
        send_response.return_value = send_response_return
    install_send_response_mock(bot, send_response)
    install_edit_message_mock(bot, AsyncMock(return_value=True))

    room_user_ids = [
        "@mindroom_router:localhost",
        *[f"@mindroom_{name}:localhost" for name in configured_agents],
        "@alice:example.com",
    ]
    room = _make_room(*room_user_ids)
    event = _make_voice_event(sender="@alice:example.com")
    return bot, room, event


@pytest.mark.asyncio
async def test_router_processes_own_voice_transcriptions(tmp_path) -> None:  # noqa: ANN001
    """Router should still handle voice-derived commands it sent on behalf of users."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=_attach_runtime_paths(Config(authorization={"default_room_access": True}), tmp_path),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = MagicMock(return_value=False)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)

    room = _make_room("@mindroom_router:example.com", "@alice:example.com")
    event = MagicMock(spec=nio.RoomMessageText)
    event.sender = "@mindroom_router:example.com"
    event.body = "🎤 !schedule daily"
    event.event_id = "test_event"
    event.server_timestamp = 1234567890
    event.source = {
        "content": {
            "body": "🎤 !schedule daily",
            ORIGINAL_SENDER_KEY: "@alice:example.com",
            SOURCE_KIND_KEY: VOICE_SOURCE_KIND,
        },
    }

    with (
        patch.object(
            bot._command_turn_executor,
            "execute_if_owned",
            new=AsyncMock(return_value=True),
        ) as mock_handle,
        patch("mindroom.turn_controller.interactive.handle_text_response", new_callable=AsyncMock, return_value=None),
        patch("mindroom.text_ingress_dispatch.is_dm_room", return_value=False),
    ):
        bot.client = MagicMock()
        await bot._on_message(room, event)
        await drain_coalescing(bot)

    mock_handle.assert_called_once()
    command = mock_handle.await_args.kwargs["command"]
    assert command.type.value == "schedule"


@pytest.mark.asyncio
async def test_router_ignores_non_voice_self_messages(tmp_path) -> None:  # noqa: ANN001
    """Router should still ignore its own regular text messages."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=_attach_runtime_paths(Config(authorization={"default_room_access": True}), tmp_path),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = MagicMock(return_value=False)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)

    room = _make_room("@mindroom_router:example.com", "@bob:example.com")
    event = MagicMock(spec=nio.RoomMessageText)
    event.sender = "@mindroom_router:example.com"
    event.body = "Regular message from router"
    event.event_id = "test_event"
    event.server_timestamp = 1234567890
    event.source = {"content": {"body": "Regular message from router"}}

    with (
        patch.object(
            bot._command_turn_executor,
            "execute_if_owned",
            new=AsyncMock(return_value=True),
        ) as mock_handle,
        patch("mindroom.turn_controller.interactive.handle_text_response", new_callable=AsyncMock, return_value=None),
        patch("mindroom.text_ingress_dispatch.is_dm_room", return_value=False),
    ):
        bot.client = MagicMock()
        await bot._on_message(room, event)

    mock_handle.assert_not_called()


@pytest.mark.asyncio
async def test_router_processes_own_sidecar_commands_using_original_sender(tmp_path) -> None:  # noqa: ANN001
    """Self-sent sidecar previews should still use ORIGINAL_SENDER for dispatch prechecks."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=_attach_runtime_paths(
            Config(
                agents={"home": AgentConfig(display_name="Home", rooms=["!test:example.com"])},
                authorization={"default_room_access": True},
            ),
            tmp_path,
        ),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = MagicMock(return_value=False)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.download = AsyncMock(
        return_value=MagicMock(
            spec=nio.DownloadResponse,
            body=json.dumps(
                {
                    "msgtype": "m.text",
                    "body": "!schedule tomorrow at 9am @mindroom_home:localhost turn off the lights",
                    "m.mentions": {"user_ids": ["@mindroom_home:localhost"]},
                },
            ).encode("utf-8"),
        ),
    )
    send_response = AsyncMock(return_value="$reply")
    install_send_response_mock(bot, send_response)

    room = _make_room("@mindroom_router:example.com", "@mindroom_home:localhost", "@alice:example.com")
    event = nio.Event.parse_event(
        {
            "event_id": "$sidecar-relay",
            "sender": "@mindroom_router:example.com",
            "origin_server_ts": 1234567890,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.file",
                "body": "!schedule tomorrow [Message continues in attached file]",
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
                "url": "mxc://server/sidecar-relay",
                ORIGINAL_SENDER_KEY: "@alice:example.com",
                SOURCE_KIND_KEY: VOICE_SOURCE_KIND,
            },
        },
    )

    with (
        patch(
            "mindroom.turn_controller.interactive.handle_text_response",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_interactive,
        patch(
            "mindroom.commands.handler.schedule_task",
            new_callable=AsyncMock,
            return_value=("task123", "scheduled"),
        ) as mock_schedule,
    ):
        assert isinstance(event, nio.RoomMessageFile)
        await bot._on_media_message(room, event)
        await bot._coalescing_gate.drain_all()

    mock_interactive.assert_awaited_once()
    assert mock_schedule.await_args.kwargs["scheduled_by"] == "@alice:example.com"


@pytest.mark.asyncio
async def test_router_parses_sidecar_schedule_command_from_canonical_body(tmp_path) -> None:  # noqa: ANN001
    """Router should schedule from the hydrated sidecar body and mentions."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=_attach_runtime_paths(
            Config(
                agents={"home": AgentConfig(display_name="Home", rooms=["!test:example.com"])},
                authorization={"default_room_access": True},
            ),
            tmp_path,
        ),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = MagicMock(return_value=False)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.download = AsyncMock(
        return_value=MagicMock(
            spec=nio.DownloadResponse,
            body=json.dumps(
                {
                    "msgtype": "m.text",
                    "body": "!schedule tomorrow at 9am @mindroom_home:localhost turn off the lights",
                    "m.mentions": {"user_ids": ["@mindroom_home:localhost"]},
                },
            ).encode("utf-8"),
        ),
    )
    send_response = AsyncMock(return_value="$reply")
    install_send_response_mock(bot, send_response)

    room = _make_room("@mindroom_router:example.com", "@mindroom_home:localhost", "@alice:example.com")
    event = nio.Event.parse_event(
        {
            "event_id": "$sidecar-schedule",
            "sender": "@alice:example.com",
            "origin_server_ts": 1234567890,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.file",
                "body": "!schedule tomorrow [Message continues in attached file]",
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
                "url": "mxc://server/sidecar-schedule",
            },
        },
    )

    with (
        patch(
            "mindroom.turn_controller.interactive.handle_text_response",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_interactive,
        patch(
            "mindroom.commands.handler.schedule_task",
            new_callable=AsyncMock,
            return_value=("task123", "scheduled"),
        ) as mock_schedule,
    ):
        assert isinstance(event, nio.RoomMessageFile)
        await bot._on_media_message(room, event)
        await bot._coalescing_gate.drain_all()

    mock_interactive.assert_awaited_once()
    assert (
        mock_schedule.await_args.kwargs["full_text"] == "tomorrow at 9am @mindroom_home:localhost turn off the lights"
    )
    mentioned_agents = mock_schedule.await_args.kwargs["mentioned_agents"]
    assert [agent.full_id for agent in mentioned_agents] == ["@mindroom_home:localhost"]


@pytest.mark.asyncio
async def test_router_treats_sidecar_skill_command_as_unknown_command(tmp_path) -> None:  # noqa: ANN001
    """Router should not special-case removed skill commands after sidecar hydration."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=_attach_runtime_paths(
            Config(
                agents={
                    "home": AgentConfig(display_name="Home", rooms=["!test:example.com"], skills=["demo"]),
                    "research": AgentConfig(display_name="Research", rooms=["!test:example.com"], skills=["demo"]),
                },
                authorization={"default_room_access": True},
            ),
            tmp_path,
        ),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = MagicMock(return_value=False)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.download = AsyncMock(
        return_value=MagicMock(
            spec=nio.DownloadResponse,
            body=json.dumps(
                {
                    "msgtype": "m.text",
                    "body": "!skill demo summarize the release notes",
                    "m.mentions": {"user_ids": ["@mindroom_home:localhost"]},
                },
            ).encode("utf-8"),
        ),
    )
    send_response = AsyncMock(return_value="$fallback")
    install_send_response_mock(bot, send_response)

    room = _make_room(
        "@mindroom_router:example.com",
        "@mindroom_home:localhost",
        "@mindroom_research:localhost",
        "@alice:example.com",
    )
    event = nio.Event.parse_event(
        {
            "event_id": "$sidecar-skill",
            "sender": "@alice:example.com",
            "origin_server_ts": 1234567890,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.file",
                "body": "!skill demo [Message continues in attached file]",
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
                "url": "mxc://server/sidecar-skill",
            },
        },
    )

    with patch(
        "mindroom.turn_controller.interactive.handle_text_response",
        new_callable=AsyncMock,
        return_value=None,
    ) as mock_interactive:
        assert isinstance(event, nio.RoomMessageFile)
        await bot._on_media_message(room, event)
        await bot._coalescing_gate.drain_all()

    mock_interactive.assert_awaited_once()
    send_response.assert_awaited_once()
    assert send_response.await_args.kwargs["response_text"] == ("❌ Unknown command. Try !help for available commands.")


@pytest.mark.asyncio
async def test_router_skips_unauthorized_sidecar_commands_before_hydration(tmp_path) -> None:  # noqa: ANN001
    """Unauthorized sidecar previews should be rejected before download or dispatch."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=_attach_runtime_paths(Config(authorization={"default_room_access": True}), tmp_path),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = MagicMock(return_value=False)
    turn_store.record_turn = MagicMock(wraps=turn_store.record_turn)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.user_id = bot.matrix_id.full_id
    bot.client.download = AsyncMock()

    room = _make_room("@mindroom_router:example.com", "@alice:example.com")
    event = nio.Event.parse_event(
        {
            "event_id": "$sidecar-unauthorized",
            "sender": "@alice:example.com",
            "origin_server_ts": 1234567890,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.file",
                "body": "!schedule tomorrow [Message continues in attached file]",
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
                "url": "mxc://server/sidecar-unauthorized",
            },
        },
    )

    with (
        patch(
            "mindroom.turn_controller.interactive.handle_text_response",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_interactive,
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=False),
        patch("mindroom.commands.handler.schedule_task", new_callable=AsyncMock) as mock_schedule,
    ):
        assert isinstance(event, nio.RoomMessageFile)
        await bot._on_media_message(room, event)

    bot.client.download.assert_not_awaited()
    mock_interactive.assert_not_awaited()
    mock_schedule.assert_not_awaited()
    turn_store.record_turn.assert_called_once_with(
        TurnRecord.create([event.event_id]),
    )


@pytest.mark.asyncio
async def test_prepare_voice_message_includes_original_sender_and_attachment_metadata(tmp_path) -> None:  # noqa: ANN001
    """Audio normalization should preserve sender identity and attachment IDs."""
    config = _attach_runtime_paths(
        Config(
            authorization={"default_room_access": True},
            voice={"enabled": True},
        ),
        tmp_path,
    )
    room = _make_room("@mindroom_router:example.com", "@alice:example.com")
    event = _make_voice_event(sender="@alice:example.com")
    client = MagicMock()

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = "🎤 turn on the lights"
        prepared = await _prepare_voice_message_with_runtime(
            client,
            tmp_path,
            room,
            event,
            config,
            thread_id=None,
        )

    assert prepared is not None
    assert prepared.text == "🎤 turn on the lights"
    expected_attachment_id = _attachment_id_for_event("$voice_event")
    assert prepared.source["content"][ORIGINAL_SENDER_KEY] == "@alice:example.com"
    assert prepared.source["content"][SOURCE_KIND_KEY] == VOICE_SOURCE_KIND
    assert prepared.source["content"][ATTACHMENT_IDS_KEY] == [expected_attachment_id]
    assert VOICE_RAW_AUDIO_FALLBACK_KEY not in prepared.source["content"]
    attachment = load_attachment(tmp_path, expected_attachment_id)
    assert attachment is not None
    assert attachment.local_path.exists()


@pytest.mark.asyncio
async def test_prepare_voice_message_sanitizes_user_authored_internal_metadata(tmp_path) -> None:  # noqa: ANN001
    """Voice normalization should trust only system-owned internal metadata."""
    config = _attach_runtime_paths(
        Config(
            authorization={"default_room_access": True},
            voice={"enabled": True},
        ),
        tmp_path,
    )
    room = _make_room("@mindroom_router:example.com", "@alice:example.com")
    event = _make_voice_event(
        sender="@alice:example.com",
        source={
            "content": {
                "body": "voice.ogg",
                ATTACHMENT_IDS_KEY: ["spoofed-attachment"],
                ORIGINAL_SENDER_KEY: "@spoofed:example.com",
                VOICE_RAW_AUDIO_FALLBACK_KEY: True,
                SKIP_MENTIONS_KEY: True,
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        },
    )
    client = MagicMock()

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = "🎤 sanitized transcript"
        prepared = await _prepare_voice_message_with_runtime(
            client,
            tmp_path,
            room,
            event,
            config,
            thread_id=None,
        )

    assert prepared is not None
    content = prepared.source["content"]
    expected_attachment_id = _attachment_id_for_event("$voice_event")
    assert content[ORIGINAL_SENDER_KEY] == "@alice:example.com"
    assert content[SOURCE_KIND_KEY] == VOICE_SOURCE_KIND
    assert content[ATTACHMENT_IDS_KEY] == [expected_attachment_id]
    assert VOICE_RAW_AUDIO_FALLBACK_KEY not in content
    assert SKIP_MENTIONS_KEY not in content
    assert content["m.relates_to"] == {"rel_type": "m.thread", "event_id": "$thread_root"}


@pytest.mark.asyncio
async def test_prepare_voice_message_marks_raw_audio_fallback_and_thread(tmp_path) -> None:  # noqa: ANN001
    """Fallback normalization should keep thread metadata and the raw-audio flag."""
    config = _attach_runtime_paths(Config(authorization={"default_room_access": True}), tmp_path)
    room = _make_room("@mindroom_home:example.com", "@alice:example.com")
    event = _make_voice_event(
        sender="@alice:example.com",
        source={
            "content": {
                "body": "voice.ogg",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        },
    )
    client = MagicMock()

    with patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio:
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        prepared = await _prepare_voice_message_with_runtime(
            client,
            tmp_path,
            room,
            event,
            config,
            thread_id="$thread_root",
        )

    assert prepared is not None
    assert prepared.text == f"{VOICE_PREFIX}[Attached voice message]"
    expected_attachment_id = _attachment_id_for_event("$voice_event")
    assert prepared.source["content"][ORIGINAL_SENDER_KEY] == "@alice:example.com"
    assert prepared.source["content"][SOURCE_KIND_KEY] == VOICE_SOURCE_KIND
    assert prepared.source["content"][VOICE_RAW_AUDIO_FALLBACK_KEY] is True
    assert prepared.source["content"][ATTACHMENT_IDS_KEY] == [expected_attachment_id]
    assert prepared.source["content"]["m.relates_to"] == {"rel_type": "m.thread", "event_id": "$thread_root"}
    attachment = load_attachment(tmp_path, expected_attachment_id)
    assert attachment is not None
    assert attachment.thread_id == "$thread_root"


@pytest.mark.asyncio
async def test_router_ignores_audio_events_from_internal_agents(tmp_path) -> None:  # noqa: ANN001
    """Audio from another agent should be ignored immediately."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    config = _attach_runtime_paths(
        Config(
            agents={"assistant": {"display_name": "Assistant"}},
            authorization={"default_room_access": True},
            voice={"enabled": True},
        ),
        tmp_path,
    )

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = MagicMock(return_value=False)
    turn_store.record_turn = MagicMock(wraps=turn_store.record_turn)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    bot.client = MagicMock()
    send_response = AsyncMock()
    install_send_response_mock(bot, send_response)

    room = _make_room(
        "@mindroom_router:example.com",
        f"@mindroom_assistant:{config.get_domain(runtime_paths_for(config))}",
        "@alice:example.com",
    )
    event = _make_voice_event(
        sender=f"@mindroom_assistant:{config.get_domain(runtime_paths_for(config))}",
        event_id="$agent_audio_event",
        body="generated_audio.ogg",
        source={"content": {"body": "generated_audio.ogg", "msgtype": "m.audio"}},
    )

    with (
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
    ):
        await bot._on_media_message(room, event)

    mock_voice.assert_not_called()
    mock_download_audio.assert_not_called()
    send_response.assert_not_called()
    turn_store.record_turn.assert_not_called()


@pytest.mark.asyncio
async def test_agent_handles_audio_without_router_when_voice_disabled(tmp_path) -> None:  # noqa: ANN001
    """A single agent should answer audio directly when no router is present."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_home:localhost"
    agent_user.agent_name = "home"
    agent_user.matrix_id = MatrixID.parse("@mindroom_home:localhost")

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=_attach_runtime_paths(
            Config(
                agents={"home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]}},
                authorization={"default_room_access": True},
            ),
            tmp_path,
        ),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = MagicMock(return_value=False)
    turn_store.record_pending_turn = MagicMock(wraps=turn_store.record_pending_turn)
    turn_store.record_turn = MagicMock(wraps=turn_store.record_turn)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_home:localhost"
    generate_response = AsyncMock(return_value="$response")
    install_generate_response_mock(bot, generate_response)
    _install_voice_thread_dispatch_mocks(bot)

    room = _make_room("@mindroom_home:localhost", "@alice:example.com")
    event = _make_voice_event(sender="@alice:example.com")

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
        patch("mindroom.text_ingress_dispatch.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = None
        await bot._on_media_message(room, event)
        await drain_coalescing(bot)

    generate_response.assert_called_once()
    call_kwargs = generate_response.call_args.kwargs
    expected_attachment_id = _attachment_id_for_event("$voice_event")
    assert call_kwargs["response_envelope"].target.reply_to_event_id == "$voice_event"
    assert call_kwargs["prompt"].startswith(f"{VOICE_PREFIX}[Attached voice message]")
    assert call_kwargs["attachment_ids"] == [expected_attachment_id]
    assert list(call_kwargs["media"].audio)
    expected_record = replace(
        TurnRecord.create(
            ["$voice_event"],
            response_event_id="$response",
            source_event_prompts={"$voice_event": f"{VOICE_PREFIX}[Attached voice message]"},
        ),
        response_owner="home",
        requester_id="@alice:example.com",
        correlation_id="$voice_event",
        history_scope=HistoryScope(kind="agent", scope_id="home"),
        conversation_target=MessageTarget(
            room_id=room.room_id,
            source_thread_id=None,
            resolved_thread_id="$voice_event",
            reply_to_event_id="$voice_event",
            session_id=f"{room.room_id}:$voice_event",
        ),
    )
    turn_store.record_pending_turn.assert_called_once()
    pending_input = turn_store.record_pending_turn.call_args.args[0]
    assert replace(pending_input, response_event_id="$response") == expected_record
    turn_store.record_turn.assert_called_once()
    terminal_input = turn_store.record_turn.call_args.args[0]
    assert replace(terminal_input, completed=True, timestamp=0.0) == expected_record
    persisted_record = turn_store.get_turn_record("$voice_event")
    assert persisted_record is not None
    assert persisted_record.completed is True


@pytest.mark.asyncio
async def test_agent_handles_audio_with_router_present_in_single_agent_room(tmp_path) -> None:  # noqa: ANN001
    """Router presence should not block the only visible agent from answering audio."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_home:localhost"
    agent_user.agent_name = "home"
    agent_user.matrix_id = MatrixID.parse("@mindroom_home:localhost")

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=_attach_runtime_paths(
            Config(
                agents={"home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]}},
                authorization={"default_room_access": True},
            ),
            tmp_path,
        ),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = MagicMock(return_value=False)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_home:localhost"
    generate_response = AsyncMock(return_value="$response")
    install_generate_response_mock(bot, generate_response)
    _install_voice_thread_dispatch_mocks(bot)

    room = _make_room("@mindroom_router:localhost", "@mindroom_home:localhost", "@alice:example.com")
    event = _make_voice_event(sender="@alice:example.com")

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
        patch("mindroom.text_ingress_dispatch.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = None
        await bot._on_media_message(room, event)
        await drain_coalescing(bot)

    mock_download_audio.assert_called_once()
    generate_response.assert_called_once()


@pytest.mark.asyncio
async def test_router_and_agent_share_audio_normalization_when_router_is_present(tmp_path) -> None:  # noqa: ANN001
    """Router-present rooms should still normalize one audio event only once."""
    config = _attach_runtime_paths(
        Config(
            agents={"home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]}},
            authorization={"default_room_access": True},
            voice={"enabled": True, "visible_router_echo": False},
        ),
        tmp_path,
    )

    bots: list[AgentBot] = []
    send_response_mocks: list[AsyncMock] = []
    generate_response_mocks: list[AsyncMock] = []
    for agent_name in (ROUTER_AGENT_NAME, "home"):
        agent_user = MagicMock()
        agent_user.user_id = f"@mindroom_{agent_name}:localhost"
        agent_user.agent_name = agent_name
        agent_user.matrix_id = MatrixID.parse(f"@mindroom_{agent_name}:localhost")
        bot = _agent_bot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            rooms=["!test:example.com"],
        )
        turn_store = unwrap_extracted_collaborator(bot._turn_store)
        turn_store.is_handled = MagicMock(return_value=False)
        bot.logger = MagicMock()
        replace_turn_controller_deps(bot, logger=bot.logger)
        bot.client = AsyncMock()
        bot.client.rooms = {}
        bot.client.user_id = agent_user.user_id
        send_response = AsyncMock(return_value="$router_response")
        generate_response = AsyncMock(return_value=f"${agent_name}_response")
        install_send_response_mock(bot, send_response)
        install_generate_response_mock(bot, generate_response)
        _install_voice_thread_dispatch_mocks(bot)
        bots.append(bot)
        send_response_mocks.append(send_response)
        generate_response_mocks.append(generate_response)

    room = _make_room("@mindroom_router:localhost", "@mindroom_home:localhost", "@alice:example.com")
    event = _make_voice_event(sender="@alice:example.com")

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
        patch("mindroom.text_ingress_dispatch.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}turn on the lights"
        for bot in bots:
            await bot._on_media_message(room, event)
        await drain_coalescing(*bots)

    assert mock_download_audio.await_count == 1
    assert mock_voice.await_count == 1
    send_response_mocks[0].assert_not_called()
    assert generate_response_mocks[1].await_count == 1


@pytest.mark.asyncio
async def test_router_posts_visible_voice_echo_when_enabled(tmp_path) -> None:  # noqa: ANN001
    """Router can optionally post the normalized voice text for user visibility."""
    bot, room, event = _make_visible_router_echo_scenario(tmp_path)

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}@home turn on the lights"
        await bot._on_media_message(room, event)
        await drain_coalescing(bot)

    bot._delivery_gateway.send_text.assert_called_once()
    placeholder_request = bot._delivery_gateway.send_text.call_args.args[0]
    assert placeholder_request.target.reply_to_event_id == "$voice_event"
    assert placeholder_request.response_text == "Router agent is transcribing…"
    assert placeholder_request.target.resolved_thread_id == "$voice_event"
    assert placeholder_request.skip_mentions is True

    bot._delivery_gateway.edit_text.assert_awaited_once()
    edit_request = bot._delivery_gateway.edit_text.await_args.args[0]
    assert edit_request.event_id == "$voice_echo"
    assert edit_request.new_text == f"{VOICE_PREFIX}@home turn on the lights"
    assert edit_request.extra_content is not None
    assert edit_request.extra_content[ORIGINAL_SENDER_KEY] == "@alice:example.com"
    assert edit_request.extra_content[SOURCE_KIND_KEY] == TRUSTED_INTERNAL_RELAY_SOURCE_KIND
    assert edit_request.extra_content[ATTACHMENT_IDS_KEY] == [_attachment_id_for_event("$voice_event")]
    assert VOICE_RAW_AUDIO_FALLBACK_KEY not in edit_request.extra_content


@pytest.mark.asyncio
async def test_router_voice_echo_skips_transcription_placeholder_when_voice_is_disabled(tmp_path) -> None:  # noqa: ANN001
    """Disabled STT should post only truthful attached-voice fallback text."""
    bot, room, event = _make_visible_router_echo_scenario(tmp_path, voice_enabled=False)

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        await bot._on_media_message(room, event)
        await drain_coalescing(bot)

    bot._delivery_gateway.send_text.assert_awaited_once()
    request = bot._delivery_gateway.send_text.await_args.args[0]
    assert request.response_text == f"{VOICE_PREFIX}[Attached voice message]"
    assert request.extra_content is not None
    assert request.extra_content[VOICE_RAW_AUDIO_FALLBACK_KEY] is True
    bot._delivery_gateway.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_router_posts_transcription_placeholder_before_voice_is_ready(tmp_path) -> None:  # noqa: ANN001
    """Router should show immediate progress, then replace it with the normalized voice text."""
    bot, room, event = _make_visible_router_echo_scenario(tmp_path)
    allow_placeholder_send = asyncio.Event()
    placeholder_send_finished = asyncio.Event()
    placeholder_send_started = asyncio.Event()
    transcription_started = asyncio.Event()
    release_transcription = asyncio.Event()

    async def transcribe_voice(*_args: object, **_kwargs: object) -> str:
        transcription_started.set()
        await release_transcription.wait()
        return f"{VOICE_PREFIX}@home turn on the lights"

    async def send_visible_echo(_request: object) -> str:
        placeholder_send_started.set()
        await allow_placeholder_send.wait()
        placeholder_send_finished.set()
        return "$voice_echo"

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", side_effect=transcribe_voice),
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        bot._delivery_gateway.send_text.side_effect = send_visible_echo
        await bot._turn_controller.handle_media_event(room, event)
        try:
            await asyncio.wait_for(placeholder_send_started.wait(), timeout=1)
            await asyncio.wait_for(transcription_started.wait(), timeout=1)
            assert not placeholder_send_finished.is_set()
            allow_placeholder_send.set()
            await asyncio.wait_for(placeholder_send_finished.wait(), timeout=1)
            placeholder_request = bot._delivery_gateway.send_text.await_args.args[0]
            assert placeholder_request.response_text == "Router agent is transcribing…"
            bot._delivery_gateway.edit_text.assert_not_awaited()
        finally:
            allow_placeholder_send.set()
            release_transcription.set()
            await drain_coalescing(bot)

    bot._delivery_gateway.send_text.assert_awaited_once()
    bot._delivery_gateway.edit_text.assert_awaited_once()
    edit_request = bot._delivery_gateway.edit_text.await_args.args[0]
    assert edit_request.event_id == "$voice_echo"
    assert edit_request.new_text == f"{VOICE_PREFIX}@home turn on the lights"
    assert edit_request.extra_content is not None
    assert edit_request.extra_content[ORIGINAL_SENDER_KEY] == "@alice:example.com"
    assert edit_request.extra_content[SOURCE_KIND_KEY] == TRUSTED_INTERNAL_RELAY_SOURCE_KIND
    assert edit_request.extra_content[ATTACHMENT_IDS_KEY] == [_attachment_id_for_event("$voice_event")]


@pytest.mark.asyncio
async def test_concurrent_voice_redelivery_shares_visible_echo_lifecycle(tmp_path) -> None:  # noqa: ANN001
    """Concurrent delivery of one audio event should send and finish one visible echo."""
    bot, room, event = _make_visible_router_echo_scenario(tmp_path)
    allow_normalization = asyncio.Event()
    allow_placeholder_send = asyncio.Event()
    normalization_started = asyncio.Event()
    placeholder_send_started = asyncio.Event()
    normalization_count = 0
    normalized_event = PreparedTextEvent(
        sender=event.sender,
        event_id=event.event_id,
        body=f"{VOICE_PREFIX}@home turn on the lights",
        source={
            "content": {
                "body": f"{VOICE_PREFIX}@home turn on the lights",
                ORIGINAL_SENDER_KEY: event.sender,
                SOURCE_KIND_KEY: VOICE_SOURCE_KIND,
                VOICE_TRANSCRIPT_KEY: True,
            },
        },
        server_timestamp=event.server_timestamp,
        source_kind_override=VOICE_SOURCE_KIND,
    )

    async def normalize_voice(*_args: object, **_kwargs: object) -> tuple[PreparedTextEvent, str]:
        nonlocal normalization_count
        normalization_count += 1
        normalization_started.set()
        await allow_normalization.wait()
        return normalized_event, event.event_id

    async def send_placeholder(_request: object) -> str:
        placeholder_send_started.set()
        await allow_placeholder_send.wait()
        return "$voice_echo"

    with (
        patch.object(
            bot._turn_controller,
            "_normalize_voice_event_or_fallback",
            new=AsyncMock(side_effect=normalize_voice),
        ),
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
    ):
        bot._delivery_gateway.send_text.side_effect = send_placeholder
        await bot._turn_controller.handle_media_event(room, event)
        redelivery = asyncio.create_task(bot._turn_controller.handle_media_event(room, event))
        try:
            await asyncio.wait_for(normalization_started.wait(), timeout=1)
            await asyncio.wait_for(placeholder_send_started.wait(), timeout=1)
            await asyncio.sleep(0)
            assert normalization_count == 1
            assert bot._delivery_gateway.send_text.await_count == 1
        finally:
            allow_normalization.set()
            allow_placeholder_send.set()
        assert await asyncio.wait_for(redelivery, timeout=1) is TurnDispatchOutcome.DEFERRED
        await drain_coalescing(bot)

    bot._delivery_gateway.send_text.assert_awaited_once()
    bot._delivery_gateway.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_voice_echo_finishes_after_config_is_disabled_mid_transcription(tmp_path) -> None:  # noqa: ANN001
    """A started placeholder lifecycle should finish despite a live config toggle."""
    bot, room, event = _make_visible_router_echo_scenario(tmp_path)
    placeholder_sent = asyncio.Event()
    release_transcription = asyncio.Event()
    transcription_started = asyncio.Event()

    async def transcribe_voice(*_args: object, **_kwargs: object) -> str:
        transcription_started.set()
        await release_transcription.wait()
        return f"{VOICE_PREFIX}@home turn on the lights"

    async def send_placeholder(_request: object) -> str:
        placeholder_sent.set()
        return "$voice_echo"

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", side_effect=transcribe_voice),
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        bot._delivery_gateway.send_text.side_effect = send_placeholder
        await bot._turn_controller.handle_media_event(room, event)
        await asyncio.wait_for(placeholder_sent.wait(), timeout=1)
        await asyncio.wait_for(transcription_started.wait(), timeout=1)
        bot.config.voice.visible_router_echo = False
        release_transcription.set()
        await drain_coalescing(bot)

    bot._delivery_gateway.send_text.assert_awaited_once()
    bot._delivery_gateway.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_voice_echo_edit_failure_retries_existing_placeholder(tmp_path) -> None:  # noqa: ANN001
    """A failed replacement should retry the same placeholder on redelivery."""
    bot, room, event = _make_visible_router_echo_scenario(tmp_path)
    bot._delivery_gateway.edit_text.side_effect = None
    bot._delivery_gateway.edit_text.return_value = False

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}@home turn on the lights"
        await bot._turn_controller.handle_media_event(room, event)
        await drain_coalescing(bot)
        assert not bot._turn_store.is_handled(event.event_id)

        bot._delivery_gateway.edit_text.return_value = True
        await bot._turn_controller.handle_media_event(room, event)
        await drain_coalescing(bot)

    bot._delivery_gateway.send_text.assert_awaited_once()
    assert bot._delivery_gateway.edit_text.await_count == 2
    assert bot._turn_store.is_handled(event.event_id)
    assert bot._turn_store.visible_echo_for_source(event.event_id) == "$voice_echo"


@pytest.mark.asyncio
async def test_finalized_voice_transcript_is_not_replaced_by_late_fallback(tmp_path) -> None:  # noqa: ANN001
    """A late fallback must not downgrade a successfully delivered transcript."""
    bot, room, event = _make_visible_router_echo_scenario(tmp_path)
    voice_target = bot._turn_controller.deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id=event.event_id,
        reply_to_event_id=event.event_id,
        event_source=event.source,
    )
    bot._turn_store.record_visible_echo(event.event_id, "$voice_echo")
    bot._turn_store.record_finalized_visible_echo(
        event.event_id,
        "$voice_echo",
        is_fallback=False,
    )
    handle = bot._visible_voice_echo.start(
        VisibleVoiceEchoRequest(
            source_event_id=event.event_id,
            target=voice_target,
            requester_user_id=event.sender,
            raw_source=event.source,
        ),
    )

    await bot._visible_voice_echo.finish(
        handle,
        PreparedTextEvent(
            sender=event.sender,
            event_id=event.event_id,
            body=f"{VOICE_PREFIX}[Attached voice message]",
            source={
                "content": {
                    "body": f"{VOICE_PREFIX}[Attached voice message]",
                    ORIGINAL_SENDER_KEY: event.sender,
                    SOURCE_KIND_KEY: VOICE_SOURCE_KIND,
                    VOICE_RAW_AUDIO_FALLBACK_KEY: True,
                },
            },
            server_timestamp=event.server_timestamp,
            source_kind_override=VOICE_SOURCE_KIND,
        ),
    )

    bot._delivery_gateway.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_voice_finish_does_not_replace_finalized_transcript(tmp_path) -> None:  # noqa: ANN001
    """Cancellation cleanup must not downgrade a successfully delivered transcript."""
    bot, room, event = _make_visible_router_echo_scenario(tmp_path)
    voice_target = bot._turn_controller.deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id=event.event_id,
        reply_to_event_id=event.event_id,
        event_source=event.source,
    )
    bot._turn_store.record_visible_echo(event.event_id, "$voice_echo")
    bot._turn_store.record_finalized_visible_echo(
        event.event_id,
        "$voice_echo",
        is_fallback=False,
    )
    handle = bot._visible_voice_echo.start(
        VisibleVoiceEchoRequest(
            source_event_id=event.event_id,
            target=voice_target,
            requester_user_id=event.sender,
            raw_source=event.source,
        ),
    )
    fallback_event = PreparedTextEvent(
        sender=event.sender,
        event_id=event.event_id,
        body=f"{VOICE_PREFIX}[Attached voice message]",
        source={
            "content": {
                "body": f"{VOICE_PREFIX}[Attached voice message]",
                ORIGINAL_SENDER_KEY: event.sender,
                SOURCE_KIND_KEY: VOICE_SOURCE_KIND,
                VOICE_RAW_AUDIO_FALLBACK_KEY: True,
            },
        },
        server_timestamp=event.server_timestamp,
        source_kind_override=VOICE_SOURCE_KIND,
    )

    bot._visible_voice_echo.finish_after_cancellation(handle, fallback_event)

    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view) is True
    bot._delivery_gateway.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_transcript_wins_when_fallback_edit_is_in_flight(tmp_path) -> None:  # noqa: ANN001
    """A transcript arriving during fallback delivery should become final visible text."""
    bot, room, event = _make_visible_router_echo_scenario(tmp_path)
    voice_target = bot._turn_controller.deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id=event.event_id,
        reply_to_event_id=event.event_id,
        event_source=event.source,
    )
    handle = bot._visible_voice_echo.start(
        VisibleVoiceEchoRequest(
            source_event_id=event.event_id,
            target=voice_target,
            requester_user_id=event.sender,
            raw_source=event.source,
        ),
    )
    assert handle is not None
    assert handle.placeholder_task is not None
    assert await handle.placeholder_task == "$voice_echo"

    edit_started = asyncio.Event()
    release_fallback_edit = asyncio.Event()
    edited_texts: list[str] = []

    async def edit_text(request: EditTextRequest) -> bool:
        new_text = request.new_text
        edited_texts.append(new_text)
        if len(edited_texts) == 1:
            edit_started.set()
            await release_fallback_edit.wait()
        return True

    bot._delivery_gateway.edit_text.side_effect = edit_text
    fallback_event = PreparedTextEvent(
        sender=event.sender,
        event_id=event.event_id,
        body=f"{VOICE_PREFIX}[Attached voice message]",
        source={
            "content": {
                "body": f"{VOICE_PREFIX}[Attached voice message]",
                VOICE_RAW_AUDIO_FALLBACK_KEY: True,
            },
        },
    )
    transcript_event = PreparedTextEvent(
        sender=event.sender,
        event_id=event.event_id,
        body=f"{VOICE_PREFIX}summarize this audio",
        source={
            "content": {
                "body": f"{VOICE_PREFIX}summarize this audio",
                VOICE_TRANSCRIPT_KEY: True,
            },
        },
    )

    fallback_task = asyncio.create_task(bot._visible_voice_echo.finish(handle, fallback_event))
    await asyncio.wait_for(edit_started.wait(), timeout=1)
    transcript_task = asyncio.create_task(bot._visible_voice_echo.finish(handle, transcript_event))
    release_fallback_edit.set()
    await asyncio.gather(fallback_task, transcript_task)

    assert edited_texts == [
        f"{VOICE_PREFIX}[Attached voice message]",
        f"{VOICE_PREFIX}summarize this audio",
    ]
    finalized = bot._turn_store.finalized_visible_echo(event.event_id)
    assert finalized is not None
    assert finalized.is_fallback is False


@pytest.mark.asyncio
async def test_voice_readiness_failure_replaces_placeholder_with_fallback(tmp_path) -> None:  # noqa: ANN001
    """A readiness failure after placeholder delivery should leave terminal fallback text."""
    bot, room, event = _make_visible_router_echo_scenario(tmp_path)

    with (
        patch.object(
            bot._turn_controller.deps.resolver,
            "build_ingress_envelope",
            side_effect=RuntimeError("readiness failed"),
        ),
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
    ):
        await bot._turn_controller.handle_media_event(room, event)
        await drain_coalescing(bot)

    bot._delivery_gateway.send_text.assert_awaited_once()
    bot._delivery_gateway.edit_text.assert_awaited_once()
    edit_request = bot._delivery_gateway.edit_text.await_args.args[0]
    assert edit_request.event_id == "$voice_echo"
    assert edit_request.new_text == f"{VOICE_PREFIX}[Attached voice message]"
    assert edit_request.extra_content is not None
    assert edit_request.extra_content[VOICE_RAW_AUDIO_FALLBACK_KEY] is True


@pytest.mark.asyncio
async def test_voice_readiness_cancellation_schedules_terminal_placeholder_fallback(tmp_path) -> None:  # noqa: ANN001
    """Cancelling readiness should schedule terminal fallback replacement before escaping."""
    bot, room, event = _make_visible_router_echo_scenario(tmp_path)
    normalization_started = asyncio.Event()
    placeholder_sent = asyncio.Event()

    async def wait_for_cancellation(*_args: object, **_kwargs: object) -> None:
        normalization_started.set()
        await asyncio.Event().wait()

    async def send_placeholder(_request: object) -> str:
        placeholder_sent.set()
        return "$voice_echo"

    bot._delivery_gateway.send_text.side_effect = send_placeholder
    with (
        patch.object(
            bot._turn_controller.deps.normalizer,
            "prepare_voice_event",
            side_effect=wait_for_cancellation,
        ),
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
    ):
        await bot._turn_controller.handle_media_event(room, event)
        await asyncio.wait_for(normalization_started.wait(), timeout=1)
        await asyncio.wait_for(placeholder_sent.wait(), timeout=1)
        drain_result = await bot._coalescing_gate.drain_all(ready_timeout_seconds=0.0)

    assert drain_result.cancelled_unready_count == 1
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view) is True
    bot._delivery_gateway.edit_text.assert_awaited_once()
    edit_request = bot._delivery_gateway.edit_text.await_args.args[0]
    assert edit_request.event_id == "$voice_echo"
    assert edit_request.new_text == f"{VOICE_PREFIX}[Attached voice message]"
    assert edit_request.extra_content is not None
    assert edit_request.extra_content[VOICE_RAW_AUDIO_FALLBACK_KEY] is True


@pytest.mark.asyncio
async def test_voice_placeholder_is_owned_by_runtime_shutdown(tmp_path) -> None:  # noqa: ANN001
    """Runtime shutdown should find and cancel a blocked voice-placeholder send."""
    bot, room, event = _make_visible_router_echo_scenario(tmp_path)
    placeholder_send_started = asyncio.Event()
    placeholder_send_stopped = asyncio.Event()
    release_placeholder_send = asyncio.Event()
    release_transcription = asyncio.Event()

    async def transcribe_voice(*_args: object, **_kwargs: object) -> str:
        await release_transcription.wait()
        return f"{VOICE_PREFIX}@home turn on the lights"

    async def send_placeholder(_request: object) -> str:
        placeholder_send_started.set()
        try:
            await release_placeholder_send.wait()
        finally:
            placeholder_send_stopped.set()
        return "$voice_echo"

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", side_effect=transcribe_voice),
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        bot._delivery_gateway.send_text.side_effect = send_placeholder
        await bot._turn_controller.handle_media_event(room, event)
        await asyncio.wait_for(placeholder_send_started.wait(), timeout=1)
        try:
            completed = await wait_for_background_tasks(timeout=0.0, owner=bot._runtime_view)
            assert completed is False
            await asyncio.wait_for(placeholder_send_stopped.wait(), timeout=1)
        finally:
            release_placeholder_send.set()
            release_transcription.set()
            await drain_coalescing(bot)


@pytest.mark.asyncio
async def test_voice_placeholder_finish_is_owned_by_runtime_shutdown(tmp_path) -> None:  # noqa: ANN001
    """Runtime shutdown should find and cancel a blocked placeholder replacement."""
    bot, room, event = _make_visible_router_echo_scenario(tmp_path)
    edit_started = asyncio.Event()
    edit_stopped = asyncio.Event()
    release_edit = asyncio.Event()

    async def edit_placeholder(_request: object) -> bool:
        edit_started.set()
        try:
            await release_edit.wait()
        finally:
            edit_stopped.set()
        return True

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}@home turn on the lights"
        bot._delivery_gateway.edit_text.side_effect = edit_placeholder
        await bot._turn_controller.handle_media_event(room, event)
        await asyncio.wait_for(edit_started.wait(), timeout=1)
        try:
            completed = await wait_for_background_tasks(timeout=0.0, owner=bot._runtime_view)
            assert completed is False
            await asyncio.wait_for(edit_stopped.wait(), timeout=1)
        finally:
            release_edit.set()
            await drain_coalescing(bot)


@pytest.mark.asyncio
async def test_router_visible_voice_echo_is_deduplicated_on_redelivery(tmp_path) -> None:  # noqa: ANN001
    """Visible router echoes should be sent once even if the same audio event is redelivered."""
    bot, room, event = _make_visible_router_echo_scenario(tmp_path)

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}@home turn on the lights"
        await bot._on_media_message(room, event)
        await bot._on_media_message(room, event)
        await drain_coalescing(bot)

    bot._delivery_gateway.send_text.assert_called_once()
    bot._delivery_gateway.edit_text.assert_awaited_once()
    assert mock_download_audio.await_count == 1
    assert mock_voice.await_count == 1
    assert bot._turn_store.is_handled(event.event_id)
    turn_record = bot._turn_store.get_turn_record(event.event_id)
    assert turn_record is not None
    assert turn_record.response_event_id == "$voice_echo"


@pytest.mark.asyncio
async def test_router_visible_voice_echo_respects_reply_permissions(tmp_path) -> None:  # noqa: ANN001
    """Router should not post visible echoes when it cannot reply to the sender."""
    bot, room, event = _make_visible_router_echo_scenario(
        tmp_path,
        authorization={
            "default_room_access": True,
            "agent_reply_permissions": {ROUTER_AGENT_NAME: ["@bob:example.com"]},
        },
    )

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
    ):
        await bot._on_media_message(room, event)

    bot._delivery_gateway.send_text.assert_not_called()
    mock_download_audio.assert_not_awaited()
    mock_voice.assert_not_awaited()
    assert bot._turn_store.is_handled(event.event_id)
    turn_record = bot._turn_store.get_turn_record(event.event_id)
    assert turn_record is not None
    assert turn_record.response_event_id is None


@pytest.mark.asyncio
async def test_router_visible_voice_echo_keeps_multi_agent_handoff(tmp_path) -> None:  # noqa: ANN001
    """Visible router echoes should not replace the normal multi-agent handoff."""
    bot, room, event = _make_visible_router_echo_scenario(
        tmp_path,
        agents={
            "home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]},
            "research": {"display_name": "ResearchAgent", "rooms": ["!test:example.com"]},
        },
        send_response_side_effect=["$voice_echo", "$route"],
    )

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
        patch("mindroom.turn_controller.suggest_responder_for_message", new_callable=AsyncMock, return_value="home"),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}summarize this audio"
        await bot._on_media_message(room, event)
        await drain_coalescing(bot)

    assert bot._delivery_gateway.send_text.await_count == 2
    echo_request = bot._delivery_gateway.send_text.call_args_list[0].args[0]
    handoff_request = bot._delivery_gateway.send_text.call_args_list[1].args[0]
    assert echo_request.target.reply_to_event_id == "$voice_event"
    assert echo_request.response_text == "Router agent is transcribing…"
    assert echo_request.skip_mentions is True
    bot._delivery_gateway.edit_text.assert_awaited_once()
    edit_request = bot._delivery_gateway.edit_text.await_args.args[0]
    assert edit_request.event_id == "$voice_echo"
    assert edit_request.new_text == f"{VOICE_PREFIX}summarize this audio"
    assert edit_request.extra_content is not None
    assert edit_request.extra_content[ORIGINAL_SENDER_KEY] == "@alice:example.com"
    assert edit_request.extra_content[SOURCE_KIND_KEY] == TRUSTED_INTERNAL_RELAY_SOURCE_KIND
    assert edit_request.extra_content[ATTACHMENT_IDS_KEY] == [_attachment_id_for_event("$voice_event")]
    assert VOICE_RAW_AUDIO_FALLBACK_KEY not in edit_request.extra_content
    assert handoff_request.target.reply_to_event_id == "$voice_event"
    assert handoff_request.response_text == "@home could you help with this?"
    assert handoff_request.extra_content == {
        ORIGINAL_SENDER_KEY: "@alice:example.com",
        SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        ATTACHMENT_IDS_KEY: [_attachment_id_for_event("$voice_event")],
        VOICE_TRANSCRIPT_KEY: True,
    }
    finalized_echo = bot._turn_store.finalized_visible_echo(event.event_id)
    assert finalized_echo is not None
    assert finalized_echo.event_id == "$voice_echo"
    assert finalized_echo.is_fallback is False


@pytest.mark.asyncio
async def test_router_visible_voice_echo_marks_raw_audio_fallback(tmp_path) -> None:  # noqa: ANN001
    """Visible router voice echoes should preserve the raw-audio fallback marker."""
    bot, room, event = _make_visible_router_echo_scenario(tmp_path)

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        await bot._on_media_message(room, event)
        await drain_coalescing(bot)

    bot._delivery_gateway.send_text.assert_called_once()
    placeholder_request = bot._delivery_gateway.send_text.call_args.args[0]
    assert placeholder_request.response_text == "Router agent is transcribing…"

    bot._delivery_gateway.edit_text.assert_awaited_once()
    edit_request = bot._delivery_gateway.edit_text.await_args.args[0]
    assert edit_request.new_text == f"{VOICE_PREFIX}[Attached voice message]"
    assert edit_request.extra_content is not None
    assert edit_request.extra_content[ORIGINAL_SENDER_KEY] == "@alice:example.com"
    assert edit_request.extra_content[SOURCE_KIND_KEY] == TRUSTED_INTERNAL_RELAY_SOURCE_KIND
    assert edit_request.extra_content[ATTACHMENT_IDS_KEY] == [_attachment_id_for_event("$voice_event")]
    assert edit_request.extra_content[VOICE_RAW_AUDIO_FALLBACK_KEY] is True


@pytest.mark.asyncio
async def test_router_visible_voice_echo_is_not_duplicated_when_handoff_retries(tmp_path) -> None:  # noqa: ANN001
    """A failed handoff retry should reuse the prior visible echo instead of reposting it."""
    bot, room, event = _make_visible_router_echo_scenario(
        tmp_path,
        agents={
            "home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]},
            "research": {"display_name": "ResearchAgent", "rooms": ["!test:example.com"]},
        },
        send_response_side_effect=["$voice_echo", None, "$route"],
    )

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
        patch("mindroom.turn_controller.suggest_responder_for_message", new_callable=AsyncMock, return_value="home"),
        patch(
            "mindroom.visible_response_reconciliation.find_response_event_ids_via_room_messages",
            new_callable=AsyncMock,
            return_value=frozenset(),
        ),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}summarize this audio"
        await bot._on_media_message(room, event)
        await drain_coalescing(bot)

        assert not bot._turn_store.is_handled(event.event_id)
        assert bot._turn_store.visible_echo_for_source(event.event_id) == "$voice_echo"

        await bot._on_media_message(room, event)
        await drain_coalescing(bot)

    response_texts = [call.args[0].response_text for call in bot._delivery_gateway.send_text.call_args_list]
    assert response_texts == [
        "Router agent is transcribing…",
        "@home could you help with this?",
        "@home could you help with this?",
    ]
    bot._delivery_gateway.edit_text.assert_awaited_once()
    assert bot._turn_store.is_handled(event.event_id)
    assert bot._turn_store.visible_echo_for_source(event.event_id) == "$voice_echo"


@pytest.mark.asyncio
async def test_router_visible_voice_echo_is_not_duplicated_when_handoff_retries_after_restart(
    tmp_path: Path,
) -> None:
    """A fresh bot should reuse the persisted visible echo after a failed handoff retry."""
    agents = {
        "home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]},
        "research": {"display_name": "ResearchAgent", "rooms": ["!test:example.com"]},
    }
    bot, room, event = _make_visible_router_echo_scenario(
        tmp_path,
        agents=agents,
        send_response_side_effect=["$voice_echo", None],
    )

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
        patch("mindroom.turn_controller.suggest_responder_for_message", new_callable=AsyncMock, return_value="home"),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}summarize this audio"
        await bot._on_media_message(room, event)
        await drain_coalescing(bot)

    assert not bot._turn_store.is_handled(event.event_id)
    assert bot._turn_store.visible_echo_for_source(event.event_id) == "$voice_echo"

    restarted_bot, restarted_room, restarted_event = _make_visible_router_echo_scenario(
        tmp_path,
        agents=agents,
        send_response_return="$route",
    )

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
        patch("mindroom.turn_controller.suggest_responder_for_message", new_callable=AsyncMock, return_value="home"),
        patch(
            "mindroom.visible_response_reconciliation.find_response_event_ids_via_room_messages",
            new_callable=AsyncMock,
            return_value=frozenset(),
        ),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}summarize this audio"
        await restarted_bot._on_media_message(restarted_room, restarted_event)
        await drain_coalescing(restarted_bot)

    response_texts = [call.args[0].response_text for call in restarted_bot._delivery_gateway.send_text.call_args_list]
    assert response_texts == ["@home could you help with this?"]
    assert restarted_bot._turn_store.is_handled(event.event_id)
    assert restarted_bot._turn_store.visible_echo_for_source(event.event_id) == "$voice_echo"


@pytest.mark.asyncio
async def test_router_routes_transcribed_audio_when_multiple_agents_are_present(tmp_path) -> None:  # noqa: ANN001
    """Router should route normalized audio like any other synthetic text input."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:localhost"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:localhost")

    config = _attach_runtime_paths(
        Config(
            agents={
                "home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]},
                "research": {"display_name": "ResearchAgent", "rooms": ["!test:example.com"]},
            },
            authorization={"default_room_access": True},
            voice={"enabled": True, "visible_router_echo": False},
        ),
        tmp_path,
    )

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = MagicMock(return_value=False)
    turn_store.record_turn = MagicMock(wraps=turn_store.record_turn)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    bot.client = AsyncMock()
    send_response = AsyncMock(return_value="$response")
    install_send_response_mock(bot, send_response)
    _install_voice_thread_dispatch_mocks(bot)

    room = _make_room(
        "@mindroom_router:localhost",
        "@mindroom_home:localhost",
        "@mindroom_research:localhost",
        "@alice:example.com",
    )
    event = _make_voice_event(sender="@alice:example.com")

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
        patch("mindroom.turn_controller.suggest_responder_for_message", new_callable=AsyncMock, return_value="home"),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}summarize this audio"
        await bot._on_media_message(room, event)
        await drain_coalescing(bot)

    bot._delivery_gateway.send_text.assert_called_once()
    request = bot._delivery_gateway.send_text.call_args.args[0]
    assert request.target.reply_to_event_id == "$voice_event"
    assert request.target.resolved_thread_id == "$voice_event"
    assert request.target.resolved_thread_id == "$voice_event"
    assert request.response_text == "@home could you help with this?"
    assert request.extra_content == {
        ORIGINAL_SENDER_KEY: "@alice:example.com",
        SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        ATTACHMENT_IDS_KEY: [_attachment_id_for_event("$voice_event")],
        VOICE_TRANSCRIPT_KEY: True,
    }
    turn_store.record_turn.assert_called_once()
    record = turn_store.get_turn_record("$voice_event")
    assert record is not None
    assert record.completed is True
    assert record.response_event_id == "$response"
    assert record.response_owner == ROUTER_AGENT_NAME
    assert record.requester_id == "@alice:example.com"
    assert record.correlation_id == "$voice_event"
    assert record.history_scope is None
    assert record.conversation_target == MessageTarget.resolve(
        room_id=room.room_id,
        thread_id="$voice_event",
        reply_to_event_id="$voice_event",
    )


@pytest.mark.asyncio
async def test_transcribed_mentions_target_the_mentioned_agent_when_router_absent(tmp_path) -> None:  # noqa: ANN001
    """A transcript mention should make the mentioned agent respond directly."""
    config = _attach_runtime_paths(
        Config(
            agents={
                "home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]},
                "research": {"display_name": "ResearchAgent", "rooms": ["!test:example.com"]},
            },
            authorization={"default_room_access": True},
            voice={"enabled": True},
        ),
        tmp_path,
    )

    room = _make_room("@mindroom_home:localhost", "@mindroom_research:localhost", "@alice:example.com")
    event = _make_voice_event(sender="@alice:example.com")

    bots: list[AgentBot] = []
    generate_response_mocks: list[AsyncMock] = []
    for agent_name in ("home", "research"):
        agent_user = MagicMock()
        agent_user.user_id = f"@mindroom_{agent_name}:localhost"
        agent_user.agent_name = agent_name
        agent_user.matrix_id = MatrixID.parse(f"@mindroom_{agent_name}:localhost")
        bot = _agent_bot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            rooms=["!test:example.com"],
        )
        turn_store = unwrap_extracted_collaborator(bot._turn_store)
        turn_store.is_handled = MagicMock(return_value=False)
        bot.logger = MagicMock()
        replace_turn_controller_deps(bot, logger=bot.logger)
        bot.client = AsyncMock()
        bot.client.rooms = {}
        bot.client.user_id = f"@mindroom_{agent_name}:localhost"
        generate_response = AsyncMock(return_value=f"${agent_name}_response")
        install_generate_response_mock(bot, generate_response)
        _install_voice_thread_dispatch_mocks(bot)
        bots.append(bot)
        generate_response_mocks.append(generate_response)

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
        patch("mindroom.text_ingress_dispatch.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}@research summarize this audio"
        for bot in bots:
            await bot._on_media_message(room, event)
        await drain_coalescing(*bots)

    assert mock_download_audio.await_count == 1
    assert mock_voice.await_count == 1
    assert generate_response_mocks[0].await_count == 0
    assert generate_response_mocks[1].await_count == 1
    call_kwargs = generate_response_mocks[1].call_args.kwargs
    assert call_kwargs["response_envelope"].target.reply_to_event_id == "$voice_event"
    assert call_kwargs["prompt"].startswith(f"{VOICE_PREFIX}@research summarize this audio")
    assert call_kwargs["attachment_ids"] == [_attachment_id_for_event("$voice_event")]


@pytest.mark.asyncio
async def test_caption_mentions_still_target_agent_when_stt_drops_the_mention(tmp_path) -> None:  # noqa: ANN001
    """Inherited audio-caption mentions should still target the agent when STT omits them."""
    config = _attach_runtime_paths(
        Config(
            agents={
                "home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]},
                "research": {"display_name": "ResearchAgent", "rooms": ["!test:example.com"]},
            },
            authorization={"default_room_access": True},
            voice={"enabled": True},
        ),
        tmp_path,
    )

    room = _make_room("@mindroom_home:localhost", "@mindroom_research:localhost", "@alice:example.com")
    event = _make_voice_event(
        sender="@alice:example.com",
        body="For @research voice note",
        source={
            "content": {
                "body": "For @research voice note",
                "filename": "voice.ogg",
                "m.mentions": {"user_ids": ["@mindroom_research:localhost"]},
            },
        },
    )

    bots: list[AgentBot] = []
    generate_response_mocks: list[AsyncMock] = []
    for agent_name in ("home", "research"):
        agent_user = MagicMock()
        agent_user.user_id = f"@mindroom_{agent_name}:localhost"
        agent_user.agent_name = agent_name
        agent_user.matrix_id = MatrixID.parse(f"@mindroom_{agent_name}:localhost")
        bot = _agent_bot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            rooms=["!test:example.com"],
        )
        turn_store = unwrap_extracted_collaborator(bot._turn_store)
        turn_store.is_handled = MagicMock(return_value=False)
        bot.logger = MagicMock()
        replace_turn_controller_deps(bot, logger=bot.logger)
        bot.client = AsyncMock()
        bot.client.rooms = {}
        bot.client.user_id = f"@mindroom_{agent_name}:localhost"
        generate_response = AsyncMock(return_value=f"${agent_name}_response")
        install_generate_response_mock(bot, generate_response)
        _install_voice_thread_dispatch_mocks(bot)
        bots.append(bot)
        generate_response_mocks.append(generate_response)

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
        patch("mindroom.text_ingress_dispatch.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}summarize this audio"
        for bot in bots:
            await bot._on_media_message(room, event)
        await drain_coalescing(*bots)

    assert mock_download_audio.await_count == 1
    assert mock_voice.await_count == 1
    assert generate_response_mocks[0].await_count == 0
    assert generate_response_mocks[1].await_count == 1
    call_kwargs = generate_response_mocks[1].call_args.kwargs
    assert call_kwargs["response_envelope"].target.reply_to_event_id == "$voice_event"
    assert call_kwargs["prompt"].startswith(f"{VOICE_PREFIX}summarize this audio")
    assert call_kwargs["attachment_ids"] == [_attachment_id_for_event("$voice_event")]
