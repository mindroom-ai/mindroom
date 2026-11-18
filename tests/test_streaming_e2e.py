"""End-to-end tests for streaming edits, with Matrix clients mocked."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.llm.events import ToolStarted
from mindroom.llm.runs import ToolExecution
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.streaming import StreamingResponse, send_streaming_response
from mindroom.tool_system.runtime_context import WorkerProgressEvent, get_worker_progress_pump
from mindroom.workers.models import WorkerReadyProgress
from tests.bot_helpers import make_test_agent_bot, owned_matrix_login
from tests.conftest import (
    SETTLE_TIMEOUT_SECONDS,
    TEST_ACCESS_TOKEN,
    TEST_PASSWORD,
    bind_runtime_paths,
    drain_coalescing,
    install_runtime_journal_support,
    make_matrix_client_mock,
    orchestrator_runtime_paths,
    runtime_paths_for,
)
from tests.streaming_helpers import FakeReplyRecords

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from pathlib import Path


def _answer_stream(text: str) -> Callable[..., AsyncGenerator[str]]:
    """Return a turn stream that answers ``text`` each time a turn runs."""

    def stream(*_args: object, **_kwargs: object) -> AsyncGenerator[str]:
        async def chunks() -> AsyncGenerator[str]:
            yield text

        return chunks()

    return stream


@pytest.mark.asyncio
async def test_streaming_e2e_worker_warmup_edit_sequence(tmp_path: Path) -> None:  # noqa: PLR0915
    """Worker warmup notices should edit the placeholder stream body and disappear before content arrives."""
    runtime_config = bind_runtime_paths(
        Config(
            agents={"helper": AgentConfig(display_name="HelperAgent", rooms=["!test:localhost"])},
            models={"default": ModelConfig(provider="openai", id="gpt-6-astra")},
            router=RouterConfig(model="default"),
        ),
        orchestrator_runtime_paths(tmp_path),
    )
    client = make_matrix_client_mock()
    deliveries: list[tuple[str, str]] = []
    delivered = asyncio.Event()

    class FastProgressStreamingResponse(StreamingResponse):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            self.update_interval = 0.01
            self.min_update_interval = 0.01
            self.progress_update_interval = 0.01
            self.min_char_update_interval = 0.01
            self.update_char_threshold = 10
            self.min_update_char_threshold = 10

    async def record_send(
        _client: object,
        _room_id: str,
        content: dict[str, object],
    ) -> DeliveredMatrixEvent:
        deliveries.append(("send", str(content["body"])))
        delivered.set()
        return DeliveredMatrixEvent(event_id="$stream_1", content_sent=dict(content))

    async def record_edit(
        _client: object,
        _room_id: str,
        _event_id: str,
        new_content: dict[str, object],
        new_text: str,
    ) -> DeliveredMatrixEvent:
        deliveries.append(("edit", new_text))
        delivered.set()
        return DeliveredMatrixEvent(event_id="$stream_edit", content_sent=dict(new_content))

    async def stream() -> AsyncGenerator[object]:
        async def wait_for_delivery_count(expected_count: int) -> None:
            async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                while len(deliveries) < expected_count:
                    delivered.clear()
                    await delivered.wait()

        yield ToolStarted(
            call=ToolExecution(tool_call_id="call-shell", tool_name="shell", tool_args={"cmd": "echo hello"}).call,
        )
        await asyncio.sleep(0.02)
        pump = get_worker_progress_pump()
        assert pump is not None
        pump.queue.put_nowait(
            WorkerProgressEvent(
                tool_name="shell",
                function_name="run",
                progress=WorkerReadyProgress(
                    phase="cold_start",
                    worker_key="worker-a",
                    backend_name="kubernetes",
                    elapsed_seconds=2.0,
                ),
            ),
        )
        await wait_for_delivery_count(2)
        await asyncio.sleep(0.02)
        pump.queue.put_nowait(
            WorkerProgressEvent(
                tool_name="shell",
                function_name="run",
                progress=WorkerReadyProgress(
                    phase="waiting",
                    worker_key="worker-a",
                    backend_name="kubernetes",
                    elapsed_seconds=7.0,
                ),
            ),
        )
        await wait_for_delivery_count(3)
        pump.queue.put_nowait(
            WorkerProgressEvent(
                tool_name="shell",
                function_name="run",
                progress=WorkerReadyProgress(
                    phase="ready",
                    worker_key="worker-a",
                    backend_name="kubernetes",
                    elapsed_seconds=8.0,
                ),
            ),
        )
        await wait_for_delivery_count(4)
        await asyncio.sleep(0.02)
        yield "x" * 300

    with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)):
        await send_streaming_response(
            client=client,
            target=MessageTarget.resolve("!test:localhost", None, "$original_123"),
            config=runtime_config,
            runtime_paths=runtime_paths_for(runtime_config),
            response_stream=stream(),
            show_tool_calls=False,
            streaming_cls=FastProgressStreamingResponse,
            **FakeReplyRecords(send=record_send, edit=record_edit).hooks(),
        )

    assert len(deliveries) == 6
    assert deliveries[0][0] == "send"
    assert "Thinking..." in deliveries[0][1]
    assert "Preparing isolated worker" not in deliveries[0][1]
    assert deliveries[1][0] == "edit"
    assert "Preparing isolated worker" in deliveries[1][1]
    assert "Preparing isolated worker..." in deliveries[1][1]
    assert "shell.run" not in deliveries[1][1]
    assert "first cold start" not in deliveries[1][1]
    assert deliveries[2][0] == "edit"
    assert "7s elapsed" in deliveries[2][1]
    assert "shell.run" not in deliveries[2][1]
    assert deliveries[3][0] == "edit"
    assert "Preparing isolated worker" not in deliveries[3][1]
    assert "Thinking..." in deliveries[3][1]
    assert deliveries[4][0] == "edit"
    assert "Preparing isolated worker" not in deliveries[4][1]
    assert "x" * 300 in deliveries[4][1]
    assert deliveries[5][0] == "edit"
    assert "Preparing isolated worker" not in deliveries[5][1]


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_user_edits_with_mentions_e2e(tmp_path: Path) -> None:
    """Test that agents DO NOT respond to user edits (even if they add mentions).

    This is by design - the bot ignores all edits to prevent confusion.
    Users should send a new message if they want a new response after editing.
    """
    # Create a single bot for this test
    calc_user = AgentMatrixUser(
        agent_name="calculator",
        user_id="@mindroom_calculator:localhost",
        display_name="CalculatorAgent",
        password=TEST_PASSWORD,
        access_token=TEST_ACCESS_TOKEN,
    )

    # Mock login
    with patch("mindroom.bot.login_agent_owned_session") as mock_login:
        mock_client = make_matrix_client_mock()
        mock_login.return_value = owned_matrix_login(mock_client)

        # Track events
        events_sent: list[dict[str, object]] = []

        async def mock_room_send(
            room_id: str,
            message_type: str,
            content: dict[str, object],
            *,
            ignore_unverified_devices: bool = False,
        ) -> object:
            assert message_type == "m.room.message"
            assert ignore_unverified_devices is True
            event_id = f"$calc_{len(events_sent)}"
            events_sent.append(
                {
                    "event_id": event_id,
                    "content": content,
                },
            )
            return nio.RoomSendResponse(event_id=event_id, room_id=room_id)

        mock_client.room_send.side_effect = mock_room_send

        # Create bot with calculator agent in config

        config = bind_runtime_paths(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                    ),
                    "helper": AgentConfig(
                        display_name="HelperAgent",
                        rooms=["!test:localhost"],
                    ),
                },
                router=RouterConfig(model="default"),
            ),
            orchestrator_runtime_paths(tmp_path),
        )
        config.defaults.enable_streaming = False

        bot = make_test_agent_bot(
            calc_user,
            config,
            runtime_paths_for(config),
            rooms=["!test:localhost"],
        )
        install_runtime_journal_support(bot)
        await bot.start()

        test_room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="", encrypted=False)

        # User sends initial message without mention
        initial_event = MagicMock(spec=nio.RoomMessageText)
        initial_event.body = "What's the sum?"
        initial_event.sender = "@user:localhost"
        initial_event.event_id = "$user_initial"
        initial_event.server_timestamp = 1234567890
        initial_event.source = {
            "event_id": "$user_initial",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567890,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.text",
                "body": "What's the sum?",
            },
        }

        # Process - bot should not respond (not mentioned)
        await bot._on_message(test_room, initial_event)
        await drain_coalescing(bot)
        assert len(events_sent) == 0

        # User edits to add mention
        edit_event = MagicMock(spec=nio.RoomMessageText)
        edit_event.body = "* @mindroom_calculator:localhost what's 2+2?"
        edit_event.sender = "@user:localhost"
        edit_event.event_id = "$user_edit"
        edit_event.server_timestamp = 1234567891
        edit_event.source = {
            "event_id": "$user_edit",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567891,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.text",
                "body": "* @mindroom_calculator:localhost what's 2+2?",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$user_initial",
                },
                "m.new_content": {
                    "body": "@mindroom_calculator:localhost what's 2+2?",
                    "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                },
            },
        }

        # Mock the turn's answer
        with patch("mindroom.response_runner.stream_agent_response") as mock_stream:
            mock_stream.side_effect = _answer_stream("2+2 equals 4")

            # Mock that calculator is mentioned
            with patch("mindroom.conversation_resolver.check_agent_mentioned") as mock_check:
                mock_check.return_value = (["calculator"], True, False)

                # Process edit - bot should NOT respond (edits are ignored)
                await bot._on_message(test_room, edit_event)

        # Wait for processing
        await asyncio.sleep(0.1)

        # Verify bot did NOT respond (edits are ignored by design)
        assert len(events_sent) == 0, "Bot should NOT respond to user edits (even with mentions)"

        await bot.stop()
