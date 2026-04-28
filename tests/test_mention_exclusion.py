"""Test that agents don't respond when other agents are mentioned by users."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.conversation_resolver import MessageContext
from mindroom.matrix.users import AgentMatrixUser
from mindroom.teams import TeamResolution
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_generate_response_mock,
    install_runtime_cache_support,
    install_send_response_mock,
    orchestrator_runtime_paths,
    runtime_paths_for,
    sync_bot_runtime_state,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)


@pytest.mark.asyncio
async def test_agent_ignores_user_message_mentioning_other_agents(tmp_path) -> None:  # noqa: ANN001
    """Test that an agent doesn't respond when a user mentions other agents."""
    # Create test config
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General", rooms=["!room:localhost"]),
                "research": AgentConfig(display_name="Research", rooms=["!room:localhost"]),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        ),
        orchestrator_runtime_paths(tmp_path, config_path=tmp_path / "config.yaml"),
    )
    domain = config.get_domain(runtime_paths_for(config))

    # Create GeneralAgent bot
    general_bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="general",
            user_id=f"@mindroom_general:{domain}",
            display_name="General",
            password=TEST_PASSWORD,
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room:localhost"],
    )
    wrap_extracted_collaborators(general_bot)

    # Mock the client
    general_bot.client = AsyncMock(spec=nio.AsyncClient)
    general_bot.client.user_id = f"@mindroom_general:{domain}"
    general_bot.client.rooms = {}
    install_runtime_cache_support(general_bot)
    sync_bot_runtime_state(general_bot)

    # Create a test room
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_general:localhost")

    # Create a message where user mentions ResearchAgent
    # The message content has mentions for ResearchAgent
    event = Mock(spec=nio.RoomMessageText)
    event.event_id = "$test_event"
    event.server_timestamp = 1000
    event.sender = "@user:localhost"  # User, not an agent
    event.body = "@research find the latest news"
    event.server_timestamp = 1234567890
    event.source = {
        "content": {
            "body": "@research find the latest news",
            "m.mentions": {
                "user_ids": [f"@mindroom_research:{domain}"],  # ResearchAgent is mentioned
            },
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$thread_root",
            },
        },
    }

    general_bot._send_response = AsyncMock(return_value="$placeholder")
    general_bot._generate_response = AsyncMock()
    install_send_response_mock(general_bot, general_bot._send_response)
    install_generate_response_mock(general_bot, general_bot._generate_response)

    mock_context = MessageContext(
        am_i_mentioned=False,
        is_thread=True,
        thread_id="$thread_root",
        thread_history=[],
        replay_guard_history=[],
        mentioned_agents=[config.get_ids(runtime_paths_for(config))["research"]],
        has_non_agent_mentions=False,
        requires_full_thread_history=False,
    )
    unwrap_extracted_collaborator(general_bot._conversation_resolver).extract_dispatch_context = AsyncMock(
        return_value=mock_context,
    )

    await general_bot._on_message(room, event)
    await general_bot._coalescing_gate.drain_all()

    # GeneralAgent should NOT generate a response because ResearchAgent is mentioned
    general_bot._generate_response.assert_not_called()


@pytest.mark.asyncio
async def test_agent_responds_when_mentioned_along_with_others(tmp_path) -> None:  # noqa: ANN001
    """Test that an agent DOES respond when mentioned, even if other agents are also mentioned."""
    # Create test config
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General", rooms=["!room:localhost"]),
                "research": AgentConfig(display_name="Research", rooms=["!room:localhost"]),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        ),
        orchestrator_runtime_paths(tmp_path, config_path=tmp_path / "config.yaml"),
    )
    domain = config.get_domain(runtime_paths_for(config))

    # Create GeneralAgent bot
    general_bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="general",
            user_id=f"@mindroom_general:{domain}",
            display_name="General",
            password=TEST_PASSWORD,
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room:localhost"],
    )
    wrap_extracted_collaborators(general_bot)

    # Mock the client
    general_bot.client = AsyncMock(spec=nio.AsyncClient)
    general_bot.client.user_id = f"@mindroom_general:{domain}"
    general_bot.client.rooms = {}
    install_runtime_cache_support(general_bot)
    sync_bot_runtime_state(general_bot)

    # Mock response tracker
    # Create a test room
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_general:localhost")

    # Create a message where user mentions BOTH agents
    event = Mock(spec=nio.RoomMessageText)
    event.event_id = "$test_event"
    event.server_timestamp = 1000
    event.sender = "@user:localhost"  # User, not an agent
    event.body = "@general @research help me with this"
    event.server_timestamp = 1234567890
    event.source = {
        "content": {
            "body": "@general @research help me with this",
            "m.mentions": {
                "user_ids": [
                    f"@mindroom_general:{domain}",  # GeneralAgent is mentioned
                    f"@mindroom_research:{domain}",  # ResearchAgent is also mentioned
                ],
            },
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$thread_root",
            },
        },
    }

    general_bot._send_response = AsyncMock(return_value="$placeholder")
    general_bot._generate_response = AsyncMock(return_value="$response")
    install_send_response_mock(general_bot, general_bot._send_response)
    install_generate_response_mock(general_bot, general_bot._generate_response)

    mock_context = MessageContext(
        am_i_mentioned=True,
        is_thread=True,
        thread_id="$thread_root",
        thread_history=[],
        replay_guard_history=[],
        mentioned_agents=[
            config.get_ids(runtime_paths_for(config))["general"],
            config.get_ids(runtime_paths_for(config))["research"],
        ],
        has_non_agent_mentions=False,
        requires_full_thread_history=False,
    )
    unwrap_extracted_collaborator(general_bot._conversation_resolver).extract_dispatch_context = AsyncMock(
        return_value=mock_context,
    )

    with patch("mindroom.turn_policy.decide_team_formation", return_value=TeamResolution.none()):
        await general_bot._on_message(room, event)
        await general_bot._coalescing_gate.drain_all()

    # GeneralAgent SHOULD generate a response because it's mentioned
    general_bot._generate_response.assert_called_once()
