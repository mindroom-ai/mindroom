"""Test that edit events are not processed as new messages."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.constants import ROUTER_AGENT_NAME, resolve_runtime_paths
from mindroom.matrix.users import AgentMatrixUser
from mindroom.turn_controller import _PrecheckedEvent
from tests.conftest import replace_turn_controller_deps, wrap_extracted_collaborators


@pytest.mark.asyncio
async def test_bot_ignores_edit_events(tmp_path: Path) -> None:
    """Test that the bot does not process edit events as new messages.

    This is a regression test for the bug where edit events (with m.relates_to.rel_type == "m.replace")
    were being treated as new messages, causing the router to create threads from them.
    """
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id="@router:example.com",
        display_name="Router",
        password="test_password",  # noqa: S106
    )

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=MagicMock(),
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
    bot.client.user_id = "@router:example.com"

    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@router:example.com")

    # Create an edit event - this is what Matrix sends when a message is edited
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* Edited message",  # Note the "* " prefix
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "Edited message",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",  # This indicates it's an edit
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* Edited message",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "Edited message",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$original:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
    }

    # Mock the routing method that would be called for the router
    with (
        patch.object(
            bot._turn_controller,
            "_precheck_dispatch_event",
            return_value=_PrecheckedEvent(event=edit_event, requester_user_id="@user:example.com"),
        ),
        patch.object(bot._turn_controller, "_dispatch_text_message", new_callable=AsyncMock) as mock_dispatch,
        patch.object(bot._edit_regenerator, "handle_message_edit", new_callable=AsyncMock) as mock_handle_edit,
    ):
        # Process the edit event - this should not re-enter normal dispatch.
        await bot._on_message(room, edit_event)

        mock_dispatch.assert_not_awaited()
        mock_handle_edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_ignores_multiple_edits(tmp_path: Path) -> None:
    """Test that the bot ignores multiple consecutive edits."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id="@router:example.com",
        display_name="Router",
        password="test_password",  # noqa: S106
    )

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=MagicMock(),
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env={},
        ),
        rooms=["!test:example.com"],
    )
    wrap_extracted_collaborators(bot)

    # Mock the client and dependencies
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@router:example.com"
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@router:example.com")

    # Create multiple edit events like what happened in the bug
    edit_events = []
    for i in range(1, 4):
        edit_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": f"* edit {i}",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": f"edit {i}",
                        "msgtype": "m.text",
                    },
                    "m.relates_to": {
                        "event_id": "$original:example.com",
                        "rel_type": "m.replace",
                    },
                },
                "event_id": f"$edit{i}:example.com",
                "sender": "@user:example.com",
                "origin_server_ts": 1000000 + i,
                "type": "m.room.message",
                "room_id": "!test:example.com",
            },
        )
        edit_event.source = {
            "content": {
                "body": f"* edit {i}",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": f"edit {i}",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": f"$edit{i}:example.com",
            "sender": "@user:example.com",
        }
        edit_events.append(edit_event)

    # Mock the routing method
    with (
        patch.object(
            bot._turn_controller,
            "_precheck_dispatch_event",
            side_effect=[
                _PrecheckedEvent(event=edit_event, requester_user_id="@user:example.com") for edit_event in edit_events
            ],
        ),
        patch.object(bot._turn_controller, "_dispatch_text_message", new_callable=AsyncMock) as mock_dispatch,
        patch.object(bot._edit_regenerator, "handle_message_edit", new_callable=AsyncMock) as mock_handle_edit,
    ):
        # Process all edit events
        for edit_event in edit_events:
            await bot._on_message(room, edit_event)

        assert not mock_dispatch.called
        assert mock_handle_edit.await_count == len(edit_events)


@pytest.mark.asyncio
async def test_regular_agent_ignores_edits(tmp_path: Path) -> None:
    """Test that regular agents also ignore edit events."""
    # Create a mock agent user for a regular agent
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=MagicMock(),
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env={},
        ),
        rooms=["!test:example.com"],
    )
    wrap_extracted_collaborators(bot)

    # Mock the client and dependencies
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Create an edit event with a mention of the agent
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* @test_agent help me",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "@test_agent help me",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* @test_agent help me",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "@test_agent help me",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$original:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
    }

    # Mock the generate_response method
    with (
        patch.object(bot, "_generate_response", new_callable=AsyncMock) as mock_generate,
        patch.object(bot._turn_store, "load_turn_record", return_value=None),
    ):
        # Process the edit event
        await bot._on_message(room, edit_event)

        # The agent should NOT have attempted to generate a response
        # This will FAIL with current code and PASS once fixed
        assert not mock_generate.called, "Agent should NOT respond to edit events"
