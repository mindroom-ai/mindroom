"""Tests for scheduler context propagation in team response flows."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import STREAM_STATUS_ERROR, STREAM_STATUS_KEY
from mindroom.hooks import MessageEnvelope
from mindroom.inbound_turn_normalizer import DispatchPayload
from mindroom.matrix.client import DeliveredMatrixEvent
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.orchestration.runtime import SYNC_RESTART_CANCEL_MSG
from mindroom.response_runner import ResponseRunner
from mindroom.streaming import _INTERRUPTED_RESPONSE_NOTE as INTERRUPTED_RESPONSE_NOTE
from mindroom.streaming import build_restart_interrupted_body
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from tests.conftest import (
    TEST_ACCESS_TOKEN,
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_cache_support,
    patch_response_runner_module,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@asynccontextmanager
async def _noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
    yield


def _response_envelope() -> MessageEnvelope:
    return MessageEnvelope(
        source_event_id="$user_event",
        room_id="!team:localhost",
        target=MessageTarget.resolve("!team:localhost", "$thread_root", "$user_event"),
        requester_id="@user:localhost",
        sender_id="@user:localhost",
        body="Please coordinate and schedule a reminder",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="general",
        source_kind="message",
    )


def _make_bot(tmp_path: Path) -> AgentBot:
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General Agent", rooms=["!team:localhost"]),
                "research": AgentConfig(display_name="Research Agent", rooms=["!team:localhost"]),
            },
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
        ),
        test_runtime_paths(tmp_path),
    )
    agent_user = AgentMatrixUser(
        agent_name="general",
        user_id="@mindroom_general:localhost",
        display_name="General Agent",
        password=TEST_PASSWORD,
        access_token=TEST_ACCESS_TOKEN,
    )
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!team:localhost"],
    )
    bot.client = AsyncMock()
    bot.client.user_id = agent_user.user_id
    bot.client.rooms = {"!team:localhost": MagicMock(room_id="!team:localhost")}
    bot.orchestrator = MagicMock(config=config)
    bot._send_response = AsyncMock(return_value="$team_response")
    bot._handle_interactive_question = AsyncMock()
    return install_runtime_cache_support(bot)


@pytest.mark.asyncio
async def test_team_non_streaming_has_scheduler_context(tmp_path: Path) -> None:
    """Team non-streaming flow should expose scheduler context to tool calls."""
    bot = _make_bot(tmp_path)
    team_agents = [
        MatrixID.from_agent(
            "general",
            bot.config.get_domain(runtime_paths_for(bot.config)),
            runtime_paths_for(bot.config),
        ),
        MatrixID.from_agent(
            "research",
            bot.config.get_domain(runtime_paths_for(bot.config)),
            runtime_paths_for(bot.config),
        ),
    ]
    response_run_id: str | None = None

    async def fake_run_cancellable_response(**kwargs: object) -> None:
        nonlocal response_run_id
        assert isinstance(kwargs["run_id"], str)
        assert kwargs["run_id"]
        response_run_id = kwargs["run_id"]
        response_function = kwargs["response_function"]
        await response_function(None)

    async def fake_team_response(*_args: object, **_kwargs: object) -> str:
        assert get_tool_runtime_context() is not None
        assert _kwargs["session_id"] == "!team:localhost:$thread_root"
        assert _kwargs["user_id"] == "@user:localhost"
        assert _kwargs["run_id"] == response_run_id
        return "team non-streaming response"

    with (
        patch.object(
            ResponseRunner,
            "run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.response_runner.typing_indicator", new=_noop_typing_indicator),
        patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=False),
            team_response=fake_team_response,
        ),
    ):
        await bot._generate_team_response_helper(
            room_id="!team:localhost",
            reply_to_event_id="$user_event",
            thread_id="$thread_root",
            payload=DispatchPayload(prompt="Please coordinate and schedule a reminder"),
            team_agents=team_agents,
            team_mode="coordinate",
            thread_history=[],
            requester_user_id="@user:localhost",
            response_envelope=_response_envelope(),
            correlation_id="corr-team-non-streaming",
        )


@pytest.mark.asyncio
async def test_team_non_streaming_cancellation_edits_placeholder(tmp_path: Path) -> None:
    """Generic team interruptions should replace the thinking placeholder with an interruption note."""
    bot = _make_bot(tmp_path)
    team_agents = [
        MatrixID.from_agent(
            "general",
            bot.config.get_domain(runtime_paths_for(bot.config)),
            runtime_paths_for(bot.config),
        ),
        MatrixID.from_agent(
            "research",
            bot.config.get_domain(runtime_paths_for(bot.config)),
            runtime_paths_for(bot.config),
        ),
    ]

    async def fake_run_cancellable_response(**kwargs: object) -> None:
        response_function = kwargs["response_function"]
        with suppress(asyncio.CancelledError):
            await response_function("$thinking")

    async def fake_team_response(*_args: object, **_kwargs: object) -> str:
        raise asyncio.CancelledError

    with (
        patch.object(
            ResponseRunner,
            "run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.response_runner.typing_indicator", new=_noop_typing_indicator),
        patch(
            "mindroom.delivery_gateway.edit_message_result",
            new=AsyncMock(
                return_value=DeliveredMatrixEvent(
                    event_id="$thinking",
                    content_sent={"body": INTERRUPTED_RESPONSE_NOTE},
                ),
            ),
        ) as mock_edit,
        patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=False),
            team_response=fake_team_response,
        ),
    ):
        await bot._generate_team_response_helper(
            room_id="!team:localhost",
            reply_to_event_id="$user_event",
            thread_id="$thread_root",
            payload=DispatchPayload(prompt="Please coordinate and schedule a reminder"),
            team_agents=team_agents,
            team_mode="coordinate",
            thread_history=[],
            requester_user_id="@user:localhost",
            response_envelope=_response_envelope(),
            correlation_id="corr-team-cancelled",
        )

    assert mock_edit.await_args.args[2] == "$thinking"
    assert mock_edit.await_args.args[4] == INTERRUPTED_RESPONSE_NOTE
    assert mock_edit.await_args.args[3][STREAM_STATUS_KEY] == STREAM_STATUS_ERROR


@pytest.mark.asyncio
async def test_team_non_streaming_sync_restart_edits_placeholder_with_restart_note(tmp_path: Path) -> None:
    """Sync restarts should mark team placeholders as interrupted, not user-cancelled."""
    bot = _make_bot(tmp_path)
    team_agents = [
        MatrixID.from_agent(
            "general",
            bot.config.get_domain(runtime_paths_for(bot.config)),
            runtime_paths_for(bot.config),
        ),
        MatrixID.from_agent(
            "research",
            bot.config.get_domain(runtime_paths_for(bot.config)),
            runtime_paths_for(bot.config),
        ),
    ]

    async def fake_run_cancellable_response(**kwargs: object) -> None:
        response_function = kwargs["response_function"]
        with suppress(asyncio.CancelledError):
            await response_function("$thinking")

    async def fake_team_response(*_args: object, **_kwargs: object) -> str:
        raise asyncio.CancelledError(SYNC_RESTART_CANCEL_MSG)

    with (
        patch.object(
            ResponseRunner,
            "run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.response_runner.typing_indicator", new=_noop_typing_indicator),
        patch(
            "mindroom.delivery_gateway.edit_message_result",
            new=AsyncMock(
                return_value=DeliveredMatrixEvent(
                    event_id="$thinking",
                    content_sent={"body": build_restart_interrupted_body("")},
                ),
            ),
        ) as mock_edit,
        patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=False),
            team_response=fake_team_response,
        ),
    ):
        await bot._generate_team_response_helper(
            room_id="!team:localhost",
            reply_to_event_id="$user_event",
            thread_id="$thread_root",
            payload=DispatchPayload(prompt="Please coordinate and schedule a reminder"),
            team_agents=team_agents,
            team_mode="coordinate",
            thread_history=[],
            requester_user_id="@user:localhost",
            response_envelope=_response_envelope(),
            correlation_id="corr-team-restart",
        )

    assert mock_edit.await_args.args[2] == "$thinking"
    assert mock_edit.await_args.args[4] == build_restart_interrupted_body("")
    assert mock_edit.await_args.args[3][STREAM_STATUS_KEY] == STREAM_STATUS_ERROR


@pytest.mark.asyncio
async def test_team_streaming_has_scheduler_context(tmp_path: Path) -> None:
    """Team streaming flow should expose scheduler context to tool calls."""
    bot = _make_bot(tmp_path)
    team_agents = [
        MatrixID.from_agent(
            "general",
            bot.config.get_domain(runtime_paths_for(bot.config)),
            runtime_paths_for(bot.config),
        ),
        MatrixID.from_agent(
            "research",
            bot.config.get_domain(runtime_paths_for(bot.config)),
            runtime_paths_for(bot.config),
        ),
    ]
    response_run_id: str | None = None

    async def fake_run_cancellable_response(**kwargs: object) -> None:
        nonlocal response_run_id
        assert isinstance(kwargs["run_id"], str)
        assert kwargs["run_id"]
        response_run_id = kwargs["run_id"]
        response_function = kwargs["response_function"]
        await response_function(None)

    async def fake_send_streaming_response(*args: object, **_kwargs: object) -> tuple[str, str]:
        response_stream = args[7]
        chunks = [str(chunk) async for chunk in response_stream]
        return "$stream_event", "".join(chunks)

    async def fake_team_response_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        assert get_tool_runtime_context() is not None
        assert _kwargs["session_id"] == "!team:localhost:$thread_root"
        assert _kwargs["user_id"] == "@user:localhost"
        assert _kwargs["run_id"] == response_run_id
        yield "stream chunk"

    with (
        patch.object(
            ResponseRunner,
            "run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.response_runner.typing_indicator", new=_noop_typing_indicator),
        patch(
            "mindroom.delivery_gateway.send_streaming_response",
            new=AsyncMock(side_effect=fake_send_streaming_response),
        ),
        patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=True),
            team_response_stream=fake_team_response_stream,
        ),
    ):
        await bot._generate_team_response_helper(
            room_id="!team:localhost",
            reply_to_event_id="$user_event",
            thread_id="$thread_root",
            payload=DispatchPayload(prompt="Please collaborate and schedule a reminder"),
            team_agents=team_agents,
            team_mode="collaborate",
            thread_history=[],
            requester_user_id="@user:localhost",
            response_envelope=_response_envelope(),
            correlation_id="corr-team-streaming",
        )


@pytest.mark.asyncio
async def test_team_late_cancellation_during_post_effects_propagates(tmp_path: Path) -> None:
    """Late cancellation should still cancel the team path while post-effects are running."""
    bot = _make_bot(tmp_path)
    team_agents = [
        MatrixID.from_agent(
            "general",
            bot.config.get_domain(runtime_paths_for(bot.config)),
            runtime_paths_for(bot.config),
        ),
        MatrixID.from_agent(
            "research",
            bot.config.get_domain(runtime_paths_for(bot.config)),
            runtime_paths_for(bot.config),
        ),
    ]
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = kwargs["response_function"]
        await response_function(None)
        return "$team_response"

    async def fake_post_effects(*_args: object, **_kwargs: object) -> None:
        started.set()
        await release.wait()

    with (
        patch.object(
            ResponseRunner,
            "run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.response_runner.typing_indicator", new=_noop_typing_indicator),
        patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=False),
            team_response=AsyncMock(return_value="team non-streaming response"),
            apply_post_response_effects=AsyncMock(side_effect=fake_post_effects),
        ),
    ):
        task = asyncio.create_task(
            bot._generate_team_response_helper(
                room_id="!team:localhost",
                reply_to_event_id="$user_event",
                thread_id="$thread_root",
                payload=DispatchPayload(prompt="Please coordinate and schedule a reminder"),
                team_agents=team_agents,
                team_mode="coordinate",
                thread_history=[],
                requester_user_id="@user:localhost",
                response_envelope=_response_envelope(),
                correlation_id="corr-team-late-cancel",
            ),
        )
        await started.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
