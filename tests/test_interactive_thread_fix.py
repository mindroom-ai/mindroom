"""Regression test for interactive question thread reference bug.

This test ensures that when an agent sends an interactive message with reaction options,
and a user reacts to it, the subsequent acknowledgment and response stay in the original
thread instead of creating a new thread rooted at the agent's message.

Bug: Interactive questions were being registered with the wrong thread_id (the agent's
message ID instead of the original user message ID), causing reactions to create new
threads instead of continuing the existing conversation.
"""

import asyncio
from collections.abc import AsyncIterator, Coroutine
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import (
    bind_runtime_paths,
    delivered_matrix_side_effect,
    install_runtime_cache_support,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
)


def _room_send_response(event_id: str) -> MagicMock:
    """Return a RoomSendResponse-shaped mock with one event id."""
    response = MagicMock(spec=nio.RoomSendResponse)
    response.event_id = event_id
    response.__class__ = nio.RoomSendResponse
    return response


@pytest.mark.asyncio
async def test_interactive_question_preserves_thread_root_in_streaming(tmp_path: Path) -> None:
    """Streaming responses should register interactivity through the coordinator path."""
    scheduled_tasks: list[asyncio.Task[None]] = []

    def schedule_background_task(
        coro: Coroutine[object, object, None],
        *,
        name: str,
        error_handler: object | None = None,  # noqa: ARG001
        owner: object | None = None,  # noqa: ARG001
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(coro, name=name)
        scheduled_tasks.append(task)
        return task

    with (
        patch("mindroom.response_runner.stream_agent_response") as mock_ai_response,
        patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=True),
        patch(
            "mindroom.delivery_gateway.send_streaming_response",
            new_callable=AsyncMock,
        ) as mock_send_streaming_response,
        patch("mindroom.bot.interactive.parse_and_format_interactive") as mock_parse,
        patch("mindroom.bot.interactive.register_interactive_question") as mock_register,
        patch("mindroom.bot.interactive.add_reaction_buttons", new_callable=AsyncMock),
        patch("mindroom.post_response_effects.maybe_generate_thread_summary", new_callable=AsyncMock),
        patch("mindroom.response_runner.create_background_task", side_effect=schedule_background_task),
        patch("mindroom.post_response_effects.create_background_task", side_effect=schedule_background_task),
    ):

        async def mock_stream() -> AsyncIterator[str]:
            yield "Test interactive response"

        mock_ai_response.return_value = mock_stream()
        mock_send_streaming_response.return_value = ("$agent_message_id", "Test interactive response")

        mock_response = MagicMock()
        mock_response.formatted_text = "Test interactive question"
        mock_response.option_map = {"1": "option1", "2": "option2"}
        mock_response.options_list = [{"emoji": "1", "label": "Option 1"}, {"emoji": "2", "label": "Option 2"}]
        mock_parse.return_value = mock_response

        # Create bot
        config = bind_runtime_paths(
            Config(agents={"general": AgentConfig(display_name="General")}),
            test_runtime_paths(tmp_path),
        )
        config.memory.backend = "file"
        agent_user = AgentMatrixUser(
            agent_name="general",
            user_id="@mindroom_general:localhost",
            display_name="GeneralAgent",
            password="test_password",  # noqa: S106
        )

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
            rooms=["!test:localhost"],
        )

        # Mock client
        client = make_matrix_client_mock(user_id="@mindroom_general:localhost")
        client.user_id = "@mindroom_general:localhost"
        client.room_send.return_value = _room_send_response("$agent_message_id")
        bot.client = client
        install_runtime_cache_support(bot)

        room_id = "!test:localhost"
        user_message_id = "$user_original_message"
        thread_id = user_message_id

        event_id = await bot._generate_response(
            room_id=room_id,
            prompt="Test prompt",
            reply_to_event_id=user_message_id,
            thread_id=thread_id,
            thread_history=[],
            user_id="@user:localhost",
        )
        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        assert event_id == "$agent_message_id"
        mock_register.assert_called_once()
        call_args = mock_register.call_args[0]
        registered_event_id = call_args[0]
        registered_room_id = call_args[1]
        registered_thread_id = call_args[2]
        assert registered_event_id == "$agent_message_id", "Event ID should be the agent's message"
        assert registered_room_id == "!test:localhost", "Room ID should match"
        assert registered_thread_id == user_message_id, (
            f"Thread ID should be the original user message {user_message_id}, "
            f"not the agent's message {registered_event_id}. "
            f"Got: {registered_thread_id}"
        )


@pytest.mark.asyncio
async def test_interactive_question_preserves_thread_root_in_non_streaming(tmp_path: Path) -> None:
    """Non-streaming responses should register interactivity through the coordinator path."""
    scheduled_tasks: list[asyncio.Task[None]] = []

    def schedule_background_task(
        coro: Coroutine[object, object, None],
        *,
        name: str,
        error_handler: object | None = None,  # noqa: ARG001
        owner: object | None = None,  # noqa: ARG001
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(coro, name=name)
        scheduled_tasks.append(task)
        return task

    with (
        patch("mindroom.response_runner.ai_response") as mock_ai_response,
        patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
        patch(
            "mindroom.delivery_gateway.edit_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
        ),
        patch("mindroom.bot.interactive.parse_and_format_interactive") as mock_parse,
        patch("mindroom.bot.interactive.register_interactive_question") as mock_register,
        patch("mindroom.bot.interactive.add_reaction_buttons", new_callable=AsyncMock),
        patch("mindroom.post_response_effects.maybe_generate_thread_summary", new_callable=AsyncMock),
        patch("mindroom.response_runner.create_background_task", side_effect=schedule_background_task),
        patch("mindroom.post_response_effects.create_background_task", side_effect=schedule_background_task),
    ):
        mock_ai_response.return_value = "Test interactive response"

        mock_response_with_interactive = MagicMock()
        mock_response_with_interactive.formatted_text = "Test interactive question"
        mock_response_with_interactive.option_map = {"A": "optionA", "B": "optionB"}
        mock_response_with_interactive.options_list = [
            {"emoji": "A", "label": "Option A"},
            {"emoji": "B", "label": "Option B"},
        ]

        mock_parse.return_value = mock_response_with_interactive

        config = bind_runtime_paths(
            Config(agents={"general": AgentConfig(display_name="General")}),
            test_runtime_paths(tmp_path),
        )
        config.memory.backend = "file"
        agent_user = AgentMatrixUser(
            agent_name="general",
            user_id="@mindroom_general:localhost",
            display_name="GeneralAgent",
            password="test_password",  # noqa: S106
        )

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
            rooms=["!test:localhost"],
        )

        client = make_matrix_client_mock(user_id="@mindroom_general:localhost")
        client.user_id = "@mindroom_general:localhost"
        client.room_send.return_value = _room_send_response("$agent_response_id")
        bot.client = client
        install_runtime_cache_support(bot)

        room_id = "!test:localhost"
        user_message_id = "$user_thread_start"
        thread_id = user_message_id
        event_id = await bot._generate_response(
            room_id=room_id,
            prompt="Test prompt",
            reply_to_event_id=user_message_id,
            thread_id=thread_id,
            thread_history=[],
            user_id="@user:localhost",
        )
        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        assert event_id == "$agent_response_id"
        mock_register.assert_called_once()
        call_args = mock_register.call_args[0]
        registered_event_id = call_args[0]
        registered_room_id = call_args[1]
        registered_thread_id = call_args[2]
        assert registered_event_id == "$agent_response_id", "Event ID should be the agent's message"
        assert registered_room_id == "!test:localhost", "Room ID should match"
        assert registered_thread_id == user_message_id, (
            f"Thread ID should be the original user message {user_message_id}, "
            f"not the agent's message {registered_event_id}. "
            f"Got: {registered_thread_id}"
        )


@pytest.mark.asyncio
async def test_interactive_question_without_thread_streaming(tmp_path: Path) -> None:
    """Streaming interactive replies without a thread should use the response event as the root."""
    with (
        patch("mindroom.response_runner.stream_agent_response") as mock_ai_response,
        patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=True),
        patch(
            "mindroom.delivery_gateway.send_streaming_response",
            new_callable=AsyncMock,
        ) as mock_send_streaming_response,
        patch("mindroom.bot.interactive.parse_and_format_interactive") as mock_parse,
        patch("mindroom.bot.interactive.register_interactive_question") as mock_register,
        patch("mindroom.bot.interactive.add_reaction_buttons", new_callable=AsyncMock),
    ):

        async def mock_stream() -> AsyncIterator[str]:
            yield "Test interactive response"

        mock_ai_response.return_value = mock_stream()
        mock_send_streaming_response.return_value = ("$standalone_message", "Test interactive response")

        mock_response = MagicMock()
        mock_response.formatted_text = "Test interactive question"
        mock_response.option_map = {"✓": "yes", "✗": "no"}
        mock_response.options_list = [{"emoji": "✓", "label": "Yes"}, {"emoji": "✗", "label": "No"}]
        mock_parse.return_value = mock_response

        config = bind_runtime_paths(
            Config(agents={"general": AgentConfig(display_name="General")}),
            test_runtime_paths(tmp_path),
        )
        config.memory.backend = "file"
        agent_user = AgentMatrixUser(
            agent_name="general",
            user_id="@mindroom_general:localhost",
            display_name="GeneralAgent",
            password="test_password",  # noqa: S106
        )

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
            rooms=["!test:localhost"],
        )

        client = AsyncMock()
        client.user_id = "@mindroom_general:localhost"
        client.room_send.return_value = _room_send_response("$standalone_message")
        bot.client = client
        install_runtime_cache_support(bot)

        room_id = "!test:localhost"
        event_id = await bot._generate_response(
            room_id=room_id,
            prompt="Test prompt",
            reply_to_event_id="$some_message",
            thread_id=None,
            thread_history=[],
            user_id="@user:localhost",
        )

        assert event_id == "$standalone_message"
        mock_register.assert_called_once()
        call_args = mock_register.call_args[0]
        registered_event_id = call_args[0]
        registered_thread_id = call_args[2]
        assert registered_event_id == "$standalone_message"
        assert registered_thread_id is not None, (
            "When not in a thread, thread_id should not be None. "
            "It should be the agent's message ID for proper thread creation."
        )
