"""Test that edit regeneration works correctly after bot restart."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.constants import resolve_runtime_paths
from mindroom.handled_turns import HandledTurnLedger, HandledTurnState
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import replace_turn_controller_deps, wrap_extracted_collaborators

if TYPE_CHECKING:
    from pathlib import Path


def _record_handled_turn(
    ledger: HandledTurnLedger,
    source_event_ids: list[str],
    *,
    response_event_id: str | None = None,
) -> None:
    """Record one handled turn through the typed ledger API."""
    ledger.record_handled_turn(
        HandledTurnState.create(
            source_event_ids,
            response_event_id=response_event_id,
        ),
    )


@pytest.mark.asyncio
async def test_bot_handles_redelivered_edit_after_restart(tmp_path: Path) -> None:
    """Test that the bot correctly handles an edit event that gets redelivered after restart.

    Scenario:
    1. User sends message
    2. Bot responds
    3. User edits message
    4. Bot starts regenerating
    5. Bot crashes/restarts
    6. Matrix server redelivers the edit event
    7. Bot should regenerate (not skip as "already seen")
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

    # Create real HandledTurnLedger with the test path
    bot._handled_turn_ledger = HandledTurnLedger(agent_name="test_agent", base_path=tmp_path)

    # Mock logger
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, handled_turn_ledger=bot._handled_turn_ledger, logger=bot.logger)

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Simulate that the bot has already responded to the original message
    original_event_id = "$original:example.com"
    response_event_id = "$response:example.com"
    _record_handled_turn(bot._handled_turn_ledger, [original_event_id], response_event_id=response_event_id)

    # Also mark the edit event as "seen" (simulating it was delivered before restart)
    # With the correct implementation, edits should still be processed
    edit_event_id = "$edit:example.com"
    _record_handled_turn(bot._handled_turn_ledger, [edit_event_id])

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
        # Process the redelivered edit event
        await bot._on_message(room, edit_event)

        # The bot SHOULD handle the edit (regenerate the response)
        # even though we've "seen" this edit event before
        mock_handle_edit.assert_called_once()


@pytest.mark.asyncio
async def test_bot_skips_duplicate_regular_message_after_restart(tmp_path: Path) -> None:
    """Test that the bot correctly skips regular messages that are redelivered after restart.

    This is the original purpose of the has_responded check - prevent duplicate responses.
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

    # Create real HandledTurnLedger with the test path
    bot._handled_turn_ledger = HandledTurnLedger(agent_name="test_agent", base_path=tmp_path)

    # Mock logger
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, handled_turn_ledger=bot._handled_turn_ledger, logger=bot.logger)

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Mark a message as already responded to
    message_event_id = "$message:example.com"
    _record_handled_turn(bot._handled_turn_ledger, [message_event_id])

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
