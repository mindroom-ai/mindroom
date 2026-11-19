"""Test audio normalization and dispatch through the shared text/media flow."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.attachments.storage import _attachment_id_for_event, load_attachment
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTE_EVENT_TYPE,
    ROUTE_VOICE_TRANSCRIPTS_KEY,
    ROUTER_AGENT_NAME,
    SKIP_MENTIONS_KEY,
    SOURCE_KIND_KEY,
    VOICE_PREFIX,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
    VOICE_TRANSCRIPT_KEY,
)
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND, VOICE_SOURCE_KIND
from mindroom.history.types import HistoryScope
from mindroom.llm.media import Audio
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.thread_history_result import thread_history_result
from mindroom.message_target import MessageTarget
from mindroom.routing_judgment import ResponderSelection
from mindroom.turn_policy import DispatchPlan
from mindroom.turn_record import TurnRecord
from mindroom.voice.handler import prepare_voice_message
from tests.access_schema_support import with_current_room_member_access
from tests.authorization_helpers import isolated_membership_index
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    bind_runtime_paths,
    drain_coalescing,
    install_generate_response_mock,
    install_runtime_journal_support,
    install_send_response_mock,
    orchestrator_runtime_paths,
    replace_turn_deps,
    runtime_paths_for,
    serve_media_from_download,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot


def _attach_runtime_paths(config: Config, tmp_path: Path) -> Config:
    return bind_runtime_paths(config, orchestrator_runtime_paths(tmp_path, config_path=tmp_path / "config.yaml"))


def _agent_bot(*, agent_user: object, config: Config, rooms: list[str]) -> AgentBot:
    """Construct an agent bot with the explicit runtime bound to the test config."""
    bot = install_runtime_journal_support(
        make_test_agent_bot(
            agent_user=agent_user,
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
        membership_index=isolated_membership_index(),
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
    room = nio.MatrixRoom("!test:example.com", user_ids[0] if user_ids else "@mindroom_router:localhost")
    for user_id in user_ids:
        room.add_member(user_id, None, None)
    room.members_synced = True
    return room


def _install_voice_thread_dispatch_mocks(
    bot: AgentBot,
) -> None:
    """Provide minimal explicit-thread cache reads for normalized voice dispatch."""
    bot._turn_ingress.deps.resolver.dispatch_thread_snapshot = AsyncMock(
        return_value=thread_history_result([], is_full_history=False),
    )


@pytest.mark.asyncio
async def test_router_voice_text_for_a_user_is_conversation_not_a_command(tmp_path) -> None:  # noqa: ANN001
    """Voice-kind text the router sent for a user runs as that user's conversation, never as a command."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:localhost"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:localhost")

    bot = _agent_bot(
        agent_user=agent_user,
        config=_attach_runtime_paths(with_current_room_member_access(Config(authorization={})), tmp_path),
        rooms=["!test:localhost"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = AsyncMock(return_value=False)
    bot.logger = MagicMock()
    replace_turn_deps(bot, logger=bot.logger)

    room = _make_room("@mindroom_router:localhost", "@alice:localhost")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$router-voice-text",
            "sender": "@mindroom_router:localhost",
            "origin_server_ts": 1234567890,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.text",
                "body": "🎤 !schedule daily",
                ORIGINAL_SENDER_KEY: "@alice:localhost",
                SOURCE_KIND_KEY: VOICE_SOURCE_KIND,
            },
        },
    )
    facts = bot._ingress_validator.classify(event, room_id=room.room_id)
    assert facts.source_kind == VOICE_SOURCE_KIND
    assert facts.lane_requester_id == "@alice:localhost"

    with (
        patch.object(
            bot._command_turn_executor,
            "execute_if_owned",
            new=AsyncMock(return_value=True),
        ) as mock_handle,
        patch.object(bot._turn_policy, "plan_turn", new=AsyncMock(return_value=DispatchPlan(kind="ignore"))),
    ):
        bot.client = MagicMock()
        await bot._on_message(room, event)
        await drain_coalescing(bot)

    mock_handle.assert_not_called()


@pytest.mark.asyncio
async def test_router_ignores_non_voice_self_messages(tmp_path) -> None:  # noqa: ANN001
    """Router should still ignore its own regular text messages."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    bot = _agent_bot(
        agent_user=agent_user,
        config=_attach_runtime_paths(with_current_room_member_access(Config(authorization={})), tmp_path),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = AsyncMock(return_value=False)
    bot.logger = MagicMock()
    replace_turn_deps(bot, logger=bot.logger)

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
    ):
        bot.client = MagicMock()
        await bot._on_message(room, event)

    mock_handle.assert_not_called()


@pytest.mark.asyncio
async def test_a_sidecar_relay_the_router_sent_stays_conversation(tmp_path) -> None:  # noqa: ANN001
    """A long relay the router sent as a sidecar keeps its relay kind, so its command-shaped text is not a command."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:localhost"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:localhost")

    bot = _agent_bot(
        agent_user=agent_user,
        config=_attach_runtime_paths(
            with_current_room_member_access(
                Config(
                    agents={"home": AgentConfig(display_name="Home", rooms=["!test:localhost"])},
                    authorization={},
                ),
            ),
            tmp_path,
        ),
        rooms=["!test:localhost"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = AsyncMock(return_value=False)
    bot.logger = MagicMock()
    replace_turn_deps(bot, logger=bot.logger)
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

    room = _make_room("@mindroom_router:localhost", "@mindroom_home:localhost", "@alice:localhost")
    event = nio.Event.parse_event(
        {
            "event_id": "$sidecar-relay",
            "sender": "@mindroom_router:localhost",
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
                ORIGINAL_SENDER_KEY: "@alice:localhost",
                SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
            },
        },
    )

    with patch("mindroom.commands.handler.schedule_task", new_callable=AsyncMock) as mock_schedule:
        assert isinstance(event, nio.RoomMessageFile)
        await bot._on_media_message(room, event)
        await bot._coalescing_gate.drain_all()

    mock_schedule.assert_not_awaited()


@pytest.mark.usefixtures("commands_aimed_at_bot_under_test")
@pytest.mark.asyncio
async def test_router_parses_sidecar_schedule_command_from_canonical_body(tmp_path) -> None:  # noqa: ANN001
    """Router should schedule from the hydrated sidecar body and mentions."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    bot = _agent_bot(
        agent_user=agent_user,
        config=_attach_runtime_paths(
            with_current_room_member_access(
                Config(
                    agents={"home": AgentConfig(display_name="Home", rooms=["!test:example.com"])},
                    authorization={},
                ),
            ),
            tmp_path,
        ),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = AsyncMock(return_value=False)
    bot.logger = MagicMock()
    replace_turn_deps(bot, logger=bot.logger)
    bot.client = AsyncMock(spec=nio.AsyncClient)
    serve_media_from_download(bot.client)
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
            "mindroom.commands.handler.schedule_task",
            new_callable=AsyncMock,
            return_value="scheduled",
        ) as mock_schedule,
    ):
        assert isinstance(event, nio.RoomMessageFile)
        await bot._on_media_message(room, event)
        await bot._coalescing_gate.drain_all()

    assert (
        mock_schedule.await_args.kwargs["full_text"] == "tomorrow at 9am @mindroom_home:localhost turn off the lights"
    )
    mentioned_agents = mock_schedule.await_args.kwargs["mentioned_agents"]
    assert [agent.full_id for agent in mentioned_agents] == ["@mindroom_home:localhost"]


@pytest.mark.usefixtures("commands_aimed_at_bot_under_test")
@pytest.mark.asyncio
async def test_router_treats_sidecar_skill_command_as_unknown_command(tmp_path) -> None:  # noqa: ANN001
    """Router should not special-case removed skill commands after sidecar hydration."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    bot = _agent_bot(
        agent_user=agent_user,
        config=_attach_runtime_paths(
            with_current_room_member_access(
                Config(
                    agents={
                        "home": AgentConfig(display_name="Home", rooms=["!test:example.com"], skills=["demo"]),
                        "research": AgentConfig(display_name="Research", rooms=["!test:example.com"], skills=["demo"]),
                    },
                    authorization={},
                ),
            ),
            tmp_path,
        ),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = AsyncMock(return_value=False)
    bot.logger = MagicMock()
    replace_turn_deps(bot, logger=bot.logger)
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

    assert isinstance(event, nio.RoomMessageFile)
    await bot._on_media_message(room, event)
    await bot._coalescing_gate.drain_all()
    send_response.assert_awaited_once()
    assert send_response.await_args.kwargs["response_text"] == (
        "❌ Unknown command. Try `/help` (or `!help`) for available commands."
    )


@pytest.mark.asyncio
async def test_router_skips_unauthorized_sidecar_commands_before_hydration(tmp_path) -> None:  # noqa: ANN001
    """Unauthorized sidecar previews should be rejected before download or dispatch."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    bot = _agent_bot(
        agent_user=agent_user,
        config=_attach_runtime_paths(with_current_room_member_access(Config(authorization={})), tmp_path),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = AsyncMock(return_value=False)
    turn_store.record_turn = AsyncMock(wraps=turn_store.record_turn)
    bot.logger = MagicMock()
    replace_turn_deps(bot, logger=bot.logger)
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
        patch("mindroom.turn_policy.TurnPolicy.can_reply_to_sender_in_room", return_value=False),
        patch("mindroom.turn_policy.TurnPolicy.routes_for_sender_in_room", return_value=False),
        patch("mindroom.commands.handler.schedule_task", new_callable=AsyncMock) as mock_schedule,
    ):
        assert isinstance(event, nio.RoomMessageFile)
        await bot._on_media_message(room, event)

    bot.client.download.assert_not_awaited()
    mock_schedule.assert_not_awaited()
    turn_store.record_turn.assert_called_once_with(TurnRecord.create([event.event_id]), room_id=room.room_id)


@pytest.mark.asyncio
async def test_prepare_voice_message_includes_original_sender_and_attachment_metadata(tmp_path) -> None:  # noqa: ANN001
    """Audio normalization should preserve sender identity and attachment IDs."""
    config = _attach_runtime_paths(
        with_current_room_member_access(
            Config(
                authorization={},
                voice={"enabled": True},
            ),
        ),
        tmp_path,
    )
    room = _make_room("@mindroom_router:example.com", "@alice:example.com")
    event = _make_voice_event(sender="@alice:example.com")
    client = MagicMock()

    with (
        patch("mindroom.voice.handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice.handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
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
        with_current_room_member_access(
            Config(
                authorization={},
                voice={"enabled": True},
            ),
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
        patch("mindroom.voice.handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice.handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
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
    config = _attach_runtime_paths(with_current_room_member_access(Config(authorization={})), tmp_path)
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

    with patch("mindroom.voice.handler._download_audio", new_callable=AsyncMock) as mock_download_audio:
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
        with_current_room_member_access(
            Config(
                agents={"assistant": {"display_name": "Assistant"}},
                authorization={},
                voice={"enabled": True},
            ),
        ),
        tmp_path,
    )

    bot = _agent_bot(
        agent_user=agent_user,
        config=config,
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = AsyncMock(return_value=False)
    turn_store.record_turn = AsyncMock(wraps=turn_store.record_turn)
    bot.logger = MagicMock()
    replace_turn_deps(bot, logger=bot.logger)
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
        patch("mindroom.voice.handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.voice.handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.turn_policy.TurnPolicy.can_reply_to_sender_in_room", return_value=True),
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
        config=_attach_runtime_paths(
            with_current_room_member_access(
                Config(
                    agents={"home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]}},
                    authorization={},
                ),
            ),
            tmp_path,
        ),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = AsyncMock(return_value=False)
    turn_store.record_pending_turn = AsyncMock(wraps=turn_store.record_pending_turn)
    turn_store.record_turn = AsyncMock(wraps=turn_store.record_turn)
    bot.logger = MagicMock()
    replace_turn_deps(bot, logger=bot.logger)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_home:localhost"
    generate_response = AsyncMock(return_value="$response")
    install_generate_response_mock(bot, generate_response)
    _install_voice_thread_dispatch_mocks(bot)

    room = _make_room("@mindroom_home:localhost", "@alice:example.com")
    event = _make_voice_event(sender="@alice:example.com")

    with (
        patch("mindroom.voice.handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice.handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.turn_policy.TurnPolicy.can_reply_to_sender_in_room", return_value=True),
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
            source_event_prompts={"$voice_event": f"{VOICE_PREFIX}[Attached voice message]"},
        ),
        response_owner="home",
        requester_id="@alice:example.com",
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
    assert turn_store.record_pending_turn.call_args.args[0] == expected_record
    # The reply's records record the turn answered; nothing else writes it terminal.
    turn_store.record_turn.assert_not_called()
    persisted_record = await turn_store.get_turn_record("$voice_event")
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
        config=_attach_runtime_paths(
            with_current_room_member_access(
                Config(
                    agents={"home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]}},
                    authorization={},
                    # The live router's visible echo would hold the turn; this covers routing alone.
                    voice={"visible_router_echo": False},
                ),
            ),
            tmp_path,
        ),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = AsyncMock(return_value=False)
    bot.logger = MagicMock()
    replace_turn_deps(bot, logger=bot.logger)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_home:localhost"
    generate_response = AsyncMock(return_value="$response")
    install_generate_response_mock(bot, generate_response)
    _install_voice_thread_dispatch_mocks(bot)

    room = _make_room("@mindroom_router:localhost", "@mindroom_home:localhost", "@alice:example.com")
    event = _make_voice_event(sender="@alice:example.com")

    with (
        patch("mindroom.voice.handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice.handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.turn_policy.TurnPolicy.can_reply_to_sender_in_room", return_value=True),
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
        with_current_room_member_access(
            Config(
                agents={"home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]}},
                authorization={},
                voice={"enabled": True, "visible_router_echo": False},
            ),
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
            config=config,
            rooms=["!test:example.com"],
        )
        turn_store = unwrap_extracted_collaborator(bot._turn_store)
        turn_store.is_handled = AsyncMock(return_value=False)
        bot.logger = MagicMock()
        replace_turn_deps(bot, logger=bot.logger)
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
        patch("mindroom.voice.handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice.handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.turn_policy.TurnPolicy.can_reply_to_sender_in_room", return_value=True),
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
async def test_router_routes_transcribed_audio_when_multiple_agents_are_present(tmp_path) -> None:  # noqa: ANN001
    """Router should route normalized audio like any other synthetic text input."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:localhost"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:localhost")

    config = _attach_runtime_paths(
        with_current_room_member_access(
            Config(
                agents={
                    "home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]},
                    "research": {"display_name": "ResearchAgent", "rooms": ["!test:example.com"]},
                },
                authorization={},
                voice={"enabled": True, "visible_router_echo": False},
            ),
        ),
        tmp_path,
    )

    bot = _agent_bot(
        agent_user=agent_user,
        config=config,
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = AsyncMock(return_value=False)
    turn_store.record_responded_turn = AsyncMock(wraps=turn_store.record_responded_turn)
    bot.logger = MagicMock()
    replace_turn_deps(bot, logger=bot.logger)
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
        patch("mindroom.voice.handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice.handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.turn_policy.TurnPolicy.can_reply_to_sender_in_room", return_value=True),
        patch(
            "mindroom.router.relay.suggest_responder_for_message",
            new_callable=AsyncMock,
            return_value=ResponderSelection("home"),
        ),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}summarize this audio"
        await bot._on_media_message(room, event)
        await drain_coalescing(bot)

    bot._delivery_gateway.send_text.assert_not_called()
    route = dict(bot._delivery_gateway.send_route.await_args.args[1])
    assert route.pop(ROUTE_EVENT_TYPE) == {
        "entity": "@mindroom_home:localhost",
        "event_ids": ["$voice_event"],
        "thread_id": "$voice_event",
    }
    assert route == {
        ORIGINAL_SENDER_KEY: "@alice:example.com",
        SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        ATTACHMENT_IDS_KEY: [_attachment_id_for_event("$voice_event")],
        VOICE_TRANSCRIPT_KEY: True,
        # The responder posts the transcript before its answer, so the route carries it.
        ROUTE_VOICE_TRANSCRIPTS_KEY: {"$voice_event": f"{VOICE_PREFIX}summarize this audio"},
    }
    turn_store.record_responded_turn.assert_called_once()
    record = await turn_store.get_turn_record("$voice_event")
    assert record is not None
    assert record.completed is True
    assert record.response_event_id == "$route"
    assert record.response_owner == ROUTER_AGENT_NAME
    assert record.requester_id == "@alice:example.com"
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
        with_current_room_member_access(
            Config(
                agents={
                    "home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]},
                    "research": {"display_name": "ResearchAgent", "rooms": ["!test:example.com"]},
                },
                authorization={},
                voice={"enabled": True, "visible_router_echo": False},
            ),
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
            config=config,
            rooms=["!test:example.com"],
        )
        turn_store = unwrap_extracted_collaborator(bot._turn_store)
        turn_store.is_handled = AsyncMock(return_value=False)
        bot.logger = MagicMock()
        replace_turn_deps(bot, logger=bot.logger)
        bot.client = AsyncMock()
        bot.client.rooms = {}
        bot.client.user_id = f"@mindroom_{agent_name}:localhost"
        generate_response = AsyncMock(return_value=f"${agent_name}_response")
        install_generate_response_mock(bot, generate_response)
        _install_voice_thread_dispatch_mocks(bot)
        bots.append(bot)
        generate_response_mocks.append(generate_response)

    with (
        patch("mindroom.voice.handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice.handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.turn_policy.TurnPolicy.can_reply_to_sender_in_room", return_value=True),
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
        with_current_room_member_access(
            Config(
                agents={
                    "home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]},
                    "research": {"display_name": "ResearchAgent", "rooms": ["!test:example.com"]},
                },
                authorization={},
                voice={"enabled": True, "visible_router_echo": False},
            ),
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
            config=config,
            rooms=["!test:example.com"],
        )
        turn_store = unwrap_extracted_collaborator(bot._turn_store)
        turn_store.is_handled = AsyncMock(return_value=False)
        bot.logger = MagicMock()
        replace_turn_deps(bot, logger=bot.logger)
        bot.client = AsyncMock()
        bot.client.rooms = {}
        bot.client.user_id = f"@mindroom_{agent_name}:localhost"
        generate_response = AsyncMock(return_value=f"${agent_name}_response")
        install_generate_response_mock(bot, generate_response)
        _install_voice_thread_dispatch_mocks(bot)
        bots.append(bot)
        generate_response_mocks.append(generate_response)

    with (
        patch("mindroom.voice.handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice.handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.turn_policy.TurnPolicy.can_reply_to_sender_in_room", return_value=True),
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
