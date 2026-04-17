"""Test that edit regeneration works correctly after bot restart."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.constants import resolve_runtime_paths
from mindroom.handled_turns import HandledTurnState
from mindroom.hooks import MessageEnvelope
from mindroom.inbound_turn_normalizer import DispatchPayload
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.turn_policy import PreparedDispatch, ResponseAction
from tests.conftest import install_runtime_cache_support, replace_turn_controller_deps, wrap_extracted_collaborators

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_bot_skips_duplicate_edit_after_restart_once_it_was_terminally_processed(tmp_path: Path) -> None:
    """A completed edit event should not regenerate twice after restart.

    Scenario:
    1. User sends message
    2. Bot responds
    3. User edits message
    4. Bot regenerates the response
    5. Bot crashes/restarts
    6. Matrix server redelivers the edit event
    7. Bot should skip the duplicate edit event
    """
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    # Create a minimal mock config
    config = Mock()
    config.agents = {"test_agent": Mock()}
    config.teams = {}
    config.get_agent_knowledge_base_ids.return_value = []
    config.get_ids.return_value = {"test_agent": MatrixID.parse("@test_agent:example.com")}
    config.get_mindroom_user_id.return_value = "@mindroom:example.com"
    config.authorization.agent_reply_permissions = {}

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env={},
        ),
        rooms=["!test:example.com"],
    )
    wrap_extracted_collaborators(bot)

    # Mock the client
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"
    install_runtime_cache_support(bot)

    # Mock logger
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Simulate that the bot has already responded to the original message
    original_event_id = "$original:example.com"
    response_event_id = "$response:example.com"
    bot._turn_store.record_turn(
        HandledTurnState.create(
            [original_event_id],
            response_event_id=response_event_id,
        ),
    )

    # Mark the edit event as already terminally handled before restart.
    edit_event_id = "$edit:example.com"
    bot._turn_store.record_turn(HandledTurnState.create([edit_event_id]))

    # Create an edit event that would be redelivered after restart
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* @test_agent what is 3+3?",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "@test_agent what is 3+3?",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": original_event_id,
                    "rel_type": "m.replace",
                },
            },
            "event_id": edit_event_id,
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* @test_agent what is 3+3?",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "@test_agent what is 3+3?",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": original_event_id,
                "rel_type": "m.replace",
            },
        },
        "event_id": edit_event_id,
        "sender": "@user:example.com",
    }

    # Mock the methods needed for regeneration
    with (
        patch.object(bot._edit_regenerator, "handle_message_edit", new_callable=AsyncMock) as mock_handle_edit,
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
    ):
        await bot._on_message(room, edit_event)
        mock_handle_edit.assert_not_called()


@pytest.mark.asyncio
async def test_bot_skips_duplicate_regular_message_after_restart(tmp_path: Path) -> None:
    """Test that the bot correctly skips regular messages that are redelivered after restart.

    This is the original purpose of the is_handled check - prevent duplicate responses.
    """
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    # Create a minimal mock config
    config = Mock()
    config.agents = {"test_agent": Mock()}
    config.teams = {}
    config.get_agent_knowledge_base_ids.return_value = []
    config.get_ids.return_value = {"test_agent": MatrixID.parse("@test_agent:example.com")}
    config.get_mindroom_user_id.return_value = "@mindroom:example.com"
    config.authorization.agent_reply_permissions = {}

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env={},
        ),
        rooms=["!test:example.com"],
    )
    wrap_extracted_collaborators(bot)

    # Mock the client
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"
    install_runtime_cache_support(bot)

    # Mock logger
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Mark a message as already responded to
    message_event_id = "$message:example.com"
    bot._turn_store.record_turn(HandledTurnState.create([message_event_id]))

    # Create a regular message event (not an edit)
    message_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "@test_agent hello",
                "msgtype": "m.text",
            },
            "event_id": message_event_id,
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    message_event.source = {
        "content": {
            "body": "@test_agent hello",
            "msgtype": "m.text",
        },
        "event_id": message_event_id,
        "sender": "@user:example.com",
    }

    # Mock methods
    with (
        patch.object(bot._turn_controller, "_dispatch_text_message", new_callable=AsyncMock) as mock_dispatch,
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
    ):
        # Process the redelivered message
        await bot._on_message(room, message_event)

        # The bot should NOT process this message again
        mock_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_replayed_message_reuses_pending_response_transaction_id(tmp_path: Path) -> None:
    """Replay should keep using the same reserved transaction id until the turn completes."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    config = Mock()
    config.agents = {"test_agent": Mock()}
    config.teams = {}
    config.get_agent_knowledge_base_ids.return_value = []
    config.get_ids.return_value = {"test_agent": MatrixID.parse("@test_agent:example.com")}
    config.get_mindroom_user_id.return_value = "@mindroom:example.com"
    config.authorization.agent_reply_permissions = {}

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env={},
        ),
        rooms=["!test:example.com"],
    )
    wrap_extracted_collaborators(bot)
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"
    install_runtime_cache_support(bot)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")
    message_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "@test_agent what is 3+3?",
                "msgtype": "m.text",
            },
            "event_id": "$message:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    message_event.source = {
        "content": {
            "body": "@test_agent what is 3+3?",
            "msgtype": "m.text",
        },
        "event_id": "$message:example.com",
        "sender": "@user:example.com",
        "room_id": "!test:example.com",
    }

    target = MessageTarget.resolve("!test:example.com", None, message_event.event_id)
    handled_turn = bot._turn_store.attach_response_context(
        HandledTurnState.from_source_event_id(message_event.event_id),
        history_scope=None,
        conversation_target=target,
    )
    pending_response = bot._turn_store.reserve_pending_response(handled_turn)

    dispatch = PreparedDispatch(
        requester_user_id="@user:example.com",
        context=SimpleNamespace(
            thread_history=[],
            thread_id=None,
            requires_full_thread_history=False,
            am_i_mentioned=True,
        ),
        target=target,
        correlation_id=message_event.event_id,
        envelope=MessageEnvelope(
            source_event_id=message_event.event_id,
            room_id=room.room_id,
            target=target,
            requester_id="@user:example.com",
            sender_id="@user:example.com",
            body=message_event.body,
            attachment_ids=(),
            mentioned_agents=(),
            agent_name="test_agent",
            source_kind="message",
        ),
    )

    async def payload_builder(_context: object) -> DispatchPayload:
        return DispatchPayload(prompt=message_event.body)

    bot._response_runner.generate_response = AsyncMock(return_value="$response:example.com")

    await bot._turn_controller._execute_response_action(
        room,
        message_event,
        dispatch,
        ResponseAction(kind="individual"),
        payload_builder,
        processing_log="Processing message",
        dispatch_started_at=0.0,
        handled_turn=handled_turn,
    )

    request = bot._response_runner.generate_response.await_args.args[0]
    assert request.response_transaction_id == pending_response.response_transaction_id
    turn_record = bot._turn_store.get_turn_record(message_event.event_id)
    assert turn_record is not None
    assert turn_record.response_event_id == "$response:example.com"
    assert turn_record.response_transaction_id == pending_response.response_transaction_id
