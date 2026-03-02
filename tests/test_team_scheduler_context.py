"""Tests for scheduler context propagation in team response flows."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import AgentBot, _DispatchPayload
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.users import AgentMatrixUser
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from tests.conftest import TEST_ACCESS_TOKEN, TEST_PASSWORD

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@asynccontextmanager
async def _noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
    yield


def _make_bot(tmp_path: Path) -> AgentBot:
    config = Config(
        agents={
            "general": AgentConfig(display_name="General Agent", rooms=["!team:localhost"]),
            "research": AgentConfig(display_name="Research Agent", rooms=["!team:localhost"]),
        },
        models={"default": ModelConfig(provider="ollama", id="test-model")},
        router=RouterConfig(model="default"),
    )
    agent_user = AgentMatrixUser(
        agent_name="general",
        user_id="@mindroom_general:localhost",
        display_name="General Agent",
        password=TEST_PASSWORD,
        access_token=TEST_ACCESS_TOKEN,
    )
    bot = AgentBot(agent_user=agent_user, storage_path=tmp_path, config=config, rooms=["!team:localhost"])
    bot.client = AsyncMock()
    bot.client.user_id = agent_user.user_id
    bot.client.rooms = {"!team:localhost": MagicMock(room_id="!team:localhost")}
    bot.orchestrator = MagicMock(config=config)
    bot._send_response = AsyncMock(return_value="$team_response")
    bot._handle_interactive_question = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_team_non_streaming_has_scheduler_context(tmp_path: Path) -> None:
    """Team non-streaming flow should expose scheduler context to tool calls."""
    bot = _make_bot(tmp_path)
    team_agents = [
        MatrixID.from_agent("general", bot.config.domain),
        MatrixID.from_agent("research", bot.config.domain),
    ]

    async def fake_run_cancellable_response(**kwargs: object) -> None:
        response_function = kwargs["response_function"]
        await response_function(None)

    async def fake_team_response(*_args: object, **_kwargs: object) -> str:
        assert get_tool_runtime_context() is not None
        return "team non-streaming response"

    bot._run_cancellable_response = AsyncMock(side_effect=fake_run_cancellable_response)

    with (
        patch("mindroom.bot.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.bot.typing_indicator", new=_noop_typing_indicator),
        patch("mindroom.bot.team_response", new=fake_team_response),
    ):
        await bot._generate_team_response_helper(
            room_id="!team:localhost",
            reply_to_event_id="$user_event",
            thread_id="$thread_root",
            payload=_DispatchPayload(prompt="Please coordinate and schedule a reminder"),
            team_agents=team_agents,
            team_mode="coordinate",
            thread_history=[],
            requester_user_id="@user:localhost",
        )


@pytest.mark.asyncio
async def test_team_streaming_has_scheduler_context(tmp_path: Path) -> None:
    """Team streaming flow should expose scheduler context to tool calls."""
    bot = _make_bot(tmp_path)
    team_agents = [
        MatrixID.from_agent("general", bot.config.domain),
        MatrixID.from_agent("research", bot.config.domain),
    ]

    async def fake_run_cancellable_response(**kwargs: object) -> None:
        response_function = kwargs["response_function"]
        await response_function(None)

    async def fake_send_streaming_response(*args: object, **_kwargs: object) -> tuple[str, str]:
        response_stream = args[6]
        chunks = [str(chunk) async for chunk in response_stream]
        return "$stream_event", "".join(chunks)

    async def fake_team_response_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        assert get_tool_runtime_context() is not None
        yield "stream chunk"

    bot._run_cancellable_response = AsyncMock(side_effect=fake_run_cancellable_response)

    with (
        patch("mindroom.bot.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.bot.typing_indicator", new=_noop_typing_indicator),
        patch("mindroom.bot.team_response_stream", new=fake_team_response_stream),
        patch("mindroom.bot.send_streaming_response", new=AsyncMock(side_effect=fake_send_streaming_response)),
    ):
        await bot._generate_team_response_helper(
            room_id="!team:localhost",
            reply_to_event_id="$user_event",
            thread_id="$thread_root",
            payload=_DispatchPayload(prompt="Please collaborate and schedule a reminder"),
            team_agents=team_agents,
            team_mode="collaborate",
            thread_history=[],
            requester_user_id="@user:localhost",
        )
