"""Test that the 🛑 emoji can be reused for other purposes when not stopping generation."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config.main import Config
from mindroom.handled_turns import TurnRecord
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from tests.access_schema_support import with_current_room_member_access
from tests.bot_helpers import dispatch_reaction_durably, make_test_agent_bot
from tests.conftest import (
    bind_runtime_paths,
    install_send_response_mock,
    orchestrator_runtime_paths,
    runtime_paths_for,
    test_runtime_paths,
    unwrap_extracted_collaborator,
)
from tests.identity_helpers import entity_ids, persist_entity_accounts

if TYPE_CHECKING:
    from mindroom.bot import AgentBot


def _stop_test_config(tmp_path: Path, *, include_helper: bool = False) -> Config:
    agents: dict[str, dict[str, object]] = {
        "test_agent": {"display_name": "Test Agent", "rooms": ["!test:example.com"]},
    }
    if include_helper:
        agents["helper"] = {"display_name": "Helper Agent", "rooms": ["!test:example.com"]}
    config = bind_runtime_paths(
        with_current_room_member_access(Config(agents=agents, authorization={})),
        test_runtime_paths(tmp_path),
    )
    persist_entity_accounts(config, runtime_paths_for(config))
    return config


def _stop_test_agent_user(config: Config) -> AgentMatrixUser:
    matrix_id = entity_ids(config, runtime_paths_for(config))["test_agent"]
    return AgentMatrixUser(
        agent_name="test_agent",
        user_id=matrix_id.full_id,
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )


async def _record_pending_turn(bot: AgentBot, message_id: str, target: MessageTarget) -> None:
    """Record the turn whose response is still being generated in ``message_id``."""
    await bot._turn_store.record_pending_turn(
        TurnRecord.create(
            [f"{message_id}-source"],
            response_event_id=message_id,
            completed=False,
            response_owner=bot.agent_name,
            requester_id="@user:example.com",
            conversation_target=target,
        ),
    )


async def _record_pending_response(bot: AgentBot, message_id: str, target: MessageTarget) -> None:
    """Mirror the durable response intent that owns every real stop button."""
    await _record_pending_turn(bot, message_id, target)
    bot._delivery_gateway.finalize_user_stopped_response = AsyncMock(return_value=True)
    bot._journal_dispatcher.receipt_order = AsyncMock(return_value=1)


@pytest.mark.asyncio
async def test_stop_emoji_only_stops_during_generation(tmp_path: Path) -> None:
    """Test that 🛑 reaction only acts as stop button during message generation."""
    config = _stop_test_config(tmp_path)
    agent_user = _stop_test_agent_user(config)

    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )

    # Set up the bot with necessary mocks
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = agent_user.user_id
    bot.logger = MagicMock()
    send_response = AsyncMock(return_value="$stopping:example.com")
    install_send_response_mock(bot, send_response)

    # Create a room and reaction event
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id=agent_user.user_id)

    # Create a 🛑 reaction event
    reaction_event = nio.ReactionEvent.from_dict(
        {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$message:example.com",
                    "key": "🛑",
                },
            },
            "event_id": "$reaction:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.reaction",
            "room_id": "!test:example.com",
        },
    )

    claim_interactive = AsyncMock(return_value=None)
    with patch.object(
        unwrap_extracted_collaborator(bot._journal_dispatcher),
        "claim_interactive_reaction",
        new=claim_interactive,
    ):
        # Case 1: Message is NOT being generated - should handle as interactive
        await dispatch_reaction_durably(bot, room, reaction_event)

        claim_interactive.assert_awaited_once()

        # Reset the mock
        claim_interactive.reset_mock()

        # Case 2: Message IS being generated - should handle as stop button
        target = MessageTarget.resolve("!test:example.com", None, "$message:example.com")
        await _record_pending_response(bot, "$message:example.com", target)

        # A second physical reaction reaches the same STOP target.
        active_reaction_event = nio.ReactionEvent.from_dict(
            {
                "content": reaction_event.source["content"],
                "event_id": "$active-reaction:example.com",
                "sender": reaction_event.sender,
                "origin_server_ts": 1000001,
                "type": "m.reaction",
                "room_id": room.room_id,
            },
        )
        await dispatch_reaction_durably(bot, room, active_reaction_event)

        claim_interactive.assert_not_awaited()
        send_response.assert_not_awaited()

    # The response's turn records the Stop.
    stopped = bot._turn_store.get_turn_record("$message:example.com-source")
    assert stopped is not None
    assert stopped.completed
    assert stopped.user_stop_settled_receipt_order == 1


@pytest.mark.asyncio
async def test_stop_emoji_threaded_target_sends_no_acknowledgement(tmp_path: Path) -> None:
    """Threaded stop reactions should not send a separate acknowledgement message."""
    config = _stop_test_config(tmp_path)
    agent_user = _stop_test_agent_user(config)

    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )

    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = agent_user.user_id
    bot.logger = MagicMock()
    send_response = AsyncMock(return_value="$stopping:example.com")
    install_send_response_mock(bot, send_response)

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id=agent_user.user_id)
    reaction_event = nio.ReactionEvent.from_dict(
        {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$message:example.com",
                    "key": "🛑",
                },
            },
            "event_id": "$reaction:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.reaction",
            "room_id": "!test:example.com",
        },
    )

    target = MessageTarget.resolve("!test:example.com", "$thread:example.com", "$message:example.com")
    await _record_pending_response(bot, "$message:example.com", target)

    await dispatch_reaction_durably(bot, room, reaction_event)

    send_response.assert_not_awaited()
    stopped = bot._turn_store.get_turn_record("$message:example.com-source")
    assert stopped is not None
    assert stopped.user_stop_settled_receipt_order == 1


@pytest.mark.asyncio
async def test_stop_emoji_from_agent_falls_through(tmp_path: Path) -> None:
    """Test that 🛑 reactions from agents fall through to other handlers."""
    config = bind_runtime_paths(
        with_current_room_member_access(
            Config(
                agents={
                    "test_agent": {"display_name": "Test Agent", "rooms": ["!test:localhost"]},
                    "helper": {"display_name": "Helper Agent", "rooms": ["!test:localhost"]},
                },
                authorization={},
            ),
        ),
        test_runtime_paths(tmp_path),
    )
    persist_entity_accounts(config, runtime_paths_for(config))
    ids = entity_ids(config, runtime_paths_for(config))

    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id=ids["test_agent"].full_id,
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )

    # Set up the bot
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = agent_user.user_id
    bot.logger = MagicMock()

    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=agent_user.user_id)

    # Create a 🛑 reaction from ANOTHER AGENT
    reaction_event = nio.ReactionEvent.from_dict(
        {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$message:example.com",
                    "key": "🛑",
                },
            },
            "event_id": "$reaction:example.com",
            "sender": ids["helper"].full_id,
            "origin_server_ts": 1000000,
            "type": "m.reaction",
            "room_id": "!test:localhost",
        },
    )

    claim_interactive = AsyncMock(return_value=None)
    with patch.object(
        unwrap_extracted_collaborator(bot._journal_dispatcher),
        "claim_interactive_reaction",
        new=claim_interactive,
    ):
        # A response is being generated
        await _record_pending_turn(
            bot,
            "$message:example.com",
            MessageTarget.resolve("!test:localhost", None, "$message:example.com"),
        )

        # Process the reaction from an agent
        await dispatch_reaction_durably(bot, room, reaction_event)

        # Managed-agent reactions cannot answer this agent's interactive prompt.
        claim_interactive.assert_not_awaited()

    # The response was NOT stopped (agents can't stop generation)
    pending = bot._turn_store.get_turn_record("$message:example.com-source")
    assert pending is not None
    assert not pending.completed
    assert pending.user_stop_receipt_order is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_stop_reaction_blocked_by_reply_permissions(tmp_path: Path) -> None:
    """Disallowed senders must not trigger stop or send confirmation via 🛑 reaction."""
    config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": {
                    "display_name": "Test Agent",
                    "rooms": ["!test:example.com"],
                    "access": {"users": ["@alice:example.com"]},
                },
            },
        ),
        orchestrator_runtime_paths(tmp_path, config_path=tmp_path / "config.yaml"),
    )
    persist_entity_accounts(config, runtime_paths_for(config))
    agent_user = _stop_test_agent_user(config)

    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = agent_user.user_id
    bot.logger = MagicMock()

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id=agent_user.user_id)

    # A response is being generated
    await _record_pending_turn(
        bot,
        "$message:example.com",
        MessageTarget.resolve("!test:example.com", None, "$message:example.com"),
    )

    # Disallowed sender reacts with stop emoji
    reaction_event = nio.ReactionEvent.from_dict(
        {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$message:example.com",
                    "key": "🛑",
                },
            },
            "event_id": "$reaction_bob:example.com",
            "sender": "@bob:example.com",
            "origin_server_ts": 1000000,
            "type": "m.reaction",
            "room_id": "!test:example.com",
        },
    )

    send_response = AsyncMock()
    install_send_response_mock(bot, send_response)

    await dispatch_reaction_durably(bot, room, reaction_event)

    # The response was NOT stopped — sender is disallowed
    pending = bot._turn_store.get_turn_record("$message:example.com-source")
    assert pending is not None
    assert not pending.completed
    assert pending.user_stop_receipt_order is None
    # No confirmation message should have been sent
    send_response.assert_not_called()
