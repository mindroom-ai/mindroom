"""Test that the 🛑 emoji can be reused for other purposes when not stopping generation."""

from __future__ import annotations

from contextlib import asynccontextmanager
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
from tests.reply_span_helpers import reply_span

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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


@asynccontextmanager
async def _generating(bot: AgentBot, message_id: str, target: MessageTarget) -> AsyncIterator[None]:
    """Run the block while ``message_id`` shows a reply still being generated."""
    await _record_pending_turn(bot, message_id, target)
    async with reply_span(
        bot.journal_principal(),
        source_event_id=f"{message_id}-source",
        room_id=target.room_id,
        thread_id=target.resolved_thread_id,
        entity_name=bot.agent_name,
        placeholder_event_id=message_id,
    ):
        # A Stop by a permitted sender would reach this reply.
        assert await bot._user_stop_reconciler.accepts_reply_stop(message_id, target.room_id)
        yield


async def _assert_not_stopped(bot: AgentBot, message_id: str) -> None:
    """Neither the turn nor the reply showing ``message_id`` recorded a Stop."""
    pending = bot._turn_store.get_turn_record(f"{message_id}-source")
    assert pending is not None
    assert not pending.completed
    assert pending.user_stop_receipt_order is None
    reply = await bot.journal_principal().replies.for_event(message_id)
    assert reply is not None
    assert not reply.unapplied_stop


@pytest.mark.asyncio
async def test_stop_emoji_without_a_running_reply_is_an_interactive_reaction(tmp_path: Path) -> None:
    """A 🛑 reaction stops only a running reply; on any other message it is an ordinary reaction."""
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
        await dispatch_reaction_durably(bot, room, reaction_event)

        claim_interactive.assert_awaited_once()


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
        async with _generating(
            bot,
            "$message:example.com",
            MessageTarget.resolve("!test:localhost", None, "$message:example.com"),
        ):
            await dispatch_reaction_durably(bot, room, reaction_event)

            # Managed-agent reactions cannot answer this agent's interactive prompt.
            claim_interactive.assert_not_awaited()
            # Agents can't stop generation.
            await _assert_not_stopped(bot, "$message:example.com")


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

    async with _generating(
        bot,
        "$message:example.com",
        MessageTarget.resolve("!test:example.com", None, "$message:example.com"),
    ):
        await dispatch_reaction_durably(bot, room, reaction_event)

        # The response was NOT stopped — sender is disallowed
        await _assert_not_stopped(bot, "$message:example.com")
    # No confirmation message should have been sent
    send_response.assert_not_called()
