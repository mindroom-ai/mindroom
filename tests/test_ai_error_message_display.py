"""Test that AI errors are properly displayed to users in the Matrix room."""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.db.base import SessionType
from agno.models.message import Message
from agno.run.agent import RunCancelledEvent, RunContentEvent, RunOutput
from agno.run.base import RunStatus

from mindroom.bot import AgentBot
from mindroom.cancellation import SYNC_RESTART_CANCEL_MSG, USER_STOP_CANCEL_MSG, request_task_cancel
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import STREAM_STATUS_ERROR, STREAM_STATUS_KEY
from mindroom.final_delivery import StreamTransportOutcome
from mindroom.history.types import HistoryScope
from mindroom.hooks import HookRegistry
from mindroom.matrix.client import DeliveredMatrixEvent
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.response_runner import ResponseRequest
from mindroom.streaming import CANCELLED_RESPONSE_NOTE, INTERRUPTED_RESPONSE_NOTE, build_restart_interrupted_body
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_cache_support,
    replace_delivery_gateway_deps,
    replace_response_runner_deps,
    resolve_response_thread_root_for_test,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _runtime_bound_config() -> Config:
    """Return a minimal runtime-bound config for bot error-display tests."""
    return bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", rooms=["!room:localhost"])},
        ),
        test_runtime_paths(Path(tempfile.mkdtemp())),
    )


def _mock_bot(tmp_path: Path) -> AgentBot:
    """Create a bot test instance with explicit mocked collaborators."""
    config = _runtime_bound_config()
    bot = AgentBot(
        AgentMatrixUser(
            agent_name="test_agent",
            password=TEST_PASSWORD,
            display_name="Test Agent",
            user_id="@mindroom_test_agent:localhost",
        ),
        tmp_path,
        config,
        runtime_paths_for(config),
        rooms=["!room:localhost"],
    )
    bot.logger = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.client.user_id = "@mindroom_test_agent:localhost"
    bot.hook_registry = HookRegistry.empty()
    bot.enable_streaming = True
    bot.orchestrator = None
    install_runtime_cache_support(bot)
    bot._conversation_resolver.build_message_target = MagicMock(
        return_value=MessageTarget.resolve("!room:localhost", None, None, room_mode=True),
    )
    bot._conversation_resolver.resolve_response_thread_root = MagicMock(
        side_effect=resolve_response_thread_root_for_test,
    )
    bot._conversation_state_writer = MagicMock()
    bot._conversation_state_writer.create_storage = MagicMock(return_value=MagicMock())
    bot._conversation_state_writer.persist_response_event_id_in_session_run = MagicMock()
    bot._conversation_state_writer.history_scope = MagicMock(
        return_value=HistoryScope(kind="agent", scope_id=bot.agent_name),
    )
    bot._conversation_state_writer.team_history_scope = MagicMock(
        return_value=HistoryScope(kind="team", scope_id=bot.agent_name),
    )
    bot._conversation_state_writer.session_type_for_scope = MagicMock(return_value=SessionType.AGENT)
    bot._knowledge_access_support = SimpleNamespace(for_agent=MagicMock(return_value=None))
    return bot


def _build_response_runner(bot: AgentBot) -> None:
    """Rebuild extracted collaborators after tests replace bot-facing dependencies."""
    replace_delivery_gateway_deps(
        bot,
        logger=bot.logger,
        resolver=bot._conversation_resolver,
    )
    replace_response_runner_deps(
        bot,
        logger=bot.logger,
        resolver=bot._conversation_resolver,
        knowledge_access=bot._knowledge_access_support,
        state_writer=bot._conversation_state_writer,
    )


def _response_request(
    *,
    room_id: str = "!test:localhost",
    reply_to_event_id: str = "$user_msg",
    thread_id: str | None = None,
    prompt: str = "Help me with something",
    existing_event_id: str | None = None,
) -> ResponseRequest:
    """Build one response request for direct bot seam tests."""
    return ResponseRequest(
        room_id=room_id,
        reply_to_event_id=reply_to_event_id,
        thread_id=thread_id,
        thread_history=(),
        prompt=prompt,
        existing_event_id=existing_event_id,
    )


def _prepared_run(agent: object, *, prompt: str = "Help me with something") -> SimpleNamespace:
    """Return one minimal prepared-run stub for response-runner tests."""
    return SimpleNamespace(
        agent=agent,
        run_input=[Message(role="user", content=prompt)],
        unseen_event_ids=[],
    )


class TestAIErrorDisplay:
    """Test that AI errors are shown to users properly."""

    @pytest.mark.asyncio
    async def test_non_streaming_error_edits_thinking_message(self, tmp_path: Path) -> None:
        """Test that when AI fails in non-streaming mode, the thinking message is edited with the error."""
        bot = _mock_bot(tmp_path)

        edited_messages = []

        async def mock_gateway_edit_message(
            client: object,  # noqa: ARG001
            room_id: str,  # noqa: ARG001
            event_id: str,
            content: dict[str, object],
            text: str,
        ) -> DeliveredMatrixEvent:
            edited_messages.append((event_id, text))
            return DeliveredMatrixEvent(event_id="$edit", content_sent=content)

        with (
            patch("mindroom.response_runner.ai_response") as mock_ai,
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=mock_gateway_edit_message),
            ),
        ):
            _build_response_runner(bot)
            error_msg = "[test_agent] 🔴 Authentication failed. Please check your API key configuration."
            mock_ai.return_value = error_msg

            await bot._response_runner.process_and_respond(
                _response_request(existing_event_id="$thinking_msg"),
            )

            assert len(edited_messages) == 1
            event_id, text = edited_messages[0]
            assert event_id == "$thinking_msg"
            assert "Authentication failed" in text
            assert "API key" in text

    @pytest.mark.asyncio
    async def test_streaming_error_updates_message(self, tmp_path: Path) -> None:
        """Test that when streaming AI fails, the message is updated with the error."""
        bot = _mock_bot(tmp_path)

        # Mock the _edit_message method to track what gets edited
        edited_messages = []

        async def mock_edit_message(
            room_id: str,  # noqa: ARG001
            event_id: str,
            text: str,
            thread_id: str | None,  # noqa: ARG001
            extra_content: object | None = None,  # noqa: ARG001
        ) -> None:
            edited_messages.append((event_id, text))

        bot._edit_message = mock_edit_message
        bot._handle_interactive_question = AsyncMock()

        # Mock stream_agent_response to yield an error message
        with patch("mindroom.response_runner.stream_agent_response") as mock_stream:

            async def error_stream() -> AsyncIterator[str]:
                yield "[test_agent] 🔴 Rate limited. Please wait before trying again."

            mock_stream.return_value = error_stream()

            _build_response_runner(bot)
            error_text = "[test_agent] 🔴 Rate limited. Please wait before trying again."
            with patch(
                "mindroom.delivery_gateway.DeliveryGateway.deliver_stream",
                new=AsyncMock(
                    return_value=StreamTransportOutcome(
                        last_physical_stream_event_id="$msg_id",
                        terminal_operation="edit",
                        terminal_result="succeeded",
                        terminal_status="completed",
                        rendered_body=error_text,
                        visible_body_state="visible_body",
                    ),
                ),
            ) as mock_deliver_stream:
                # Call the method with an existing_event_id
                await bot._response_runner.process_and_respond_streaming(
                    _response_request(existing_event_id="$thinking_msg"),
                )

                # Verify the delivery gateway was asked to stream the response.
                mock_deliver_stream.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancellation_shows_cancelled_message(self, tmp_path: Path) -> None:
        """Test that when a response is cancelled, it shows a cancellation message."""
        bot = _mock_bot(tmp_path)
        edited_messages = []

        async def mock_gateway_edit_message(
            client: object,  # noqa: ARG001
            room_id: str,  # noqa: ARG001
            event_id: str,
            content: dict[str, object],
            text: str,
        ) -> DeliveredMatrixEvent:
            edited_messages.append((event_id, text))
            return DeliveredMatrixEvent(event_id="$edit", content_sent=content)

        # Mock ai_response to raise a generic interruption.
        with (
            patch("mindroom.response_runner.ai_response") as mock_ai,
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=mock_gateway_edit_message),
            ),
        ):
            _build_response_runner(bot)
            mock_ai.side_effect = asyncio.CancelledError()

            await bot._response_runner.process_and_respond(
                _response_request(existing_event_id="$thinking_msg"),
            )

            # Verify the thinking message was edited with the generic interruption message.
            assert len(edited_messages) == 1
            event_id, text = edited_messages[0]
            assert event_id == "$thinking_msg"
            assert text == INTERRUPTED_RESPONSE_NOTE

    @pytest.mark.asyncio
    async def test_user_stop_edits_thinking_message_with_user_cancel_note(self, tmp_path: Path) -> None:
        """Explicit stop-button cancellations should keep the user-cancelled note."""
        bot = _mock_bot(tmp_path)

        edited_messages = []

        async def mock_gateway_edit_message(
            client: object,  # noqa: ARG001
            room_id: str,  # noqa: ARG001
            event_id: str,
            content: dict[str, object],
            text: str,
        ) -> DeliveredMatrixEvent:
            edited_messages.append((event_id, text))
            return DeliveredMatrixEvent(event_id="$edit", content_sent=content)

        with (
            patch("mindroom.response_runner.ai_response") as mock_ai,
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=mock_gateway_edit_message),
            ),
        ):
            _build_response_runner(bot)
            mock_ai.side_effect = asyncio.CancelledError(USER_STOP_CANCEL_MSG)

            with pytest.raises(asyncio.CancelledError):
                await bot._response_runner.process_and_respond(
                    _response_request(existing_event_id="$thinking_msg"),
                )

        assert len(edited_messages) == 1
        event_id, text = edited_messages[0]
        assert event_id == "$thinking_msg"
        assert text == CANCELLED_RESPONSE_NOTE

    @pytest.mark.asyncio
    async def test_cancelled_run_status_preserves_user_stop_note(self, tmp_path: Path) -> None:
        """RunStatus.cancelled should keep a user-stop label when the task is already cancelling."""
        bot = _mock_bot(tmp_path)

        edited_messages: list[tuple[str, str]] = []

        async def mock_gateway_edit_message(
            client: object,  # noqa: ARG001
            room_id: str,  # noqa: ARG001
            event_id: str,
            content: dict[str, object],
            text: str,
        ) -> DeliveredMatrixEvent:
            edited_messages.append((event_id, text))
            return DeliveredMatrixEvent(event_id="$edit", content_sent=content)

        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.id = "test-model"
        mock_agent.add_history_to_context = False

        async def fake_cached_run(*_args: object, **_kwargs: object) -> RunOutput:
            current_task = asyncio.current_task()
            assert current_task is not None
            request_task_cancel(current_task, cancel_msg=USER_STOP_CANCEL_MSG)
            return RunOutput(
                run_id="run-1",
                agent_id="test_agent",
                session_id="session-1",
                content="Run run-1 was cancelled",
                messages=[Message(role="assistant", content="Run run-1 was cancelled")],
                status=RunStatus.cancelled,
            )

        with (
            patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
            patch(
                "mindroom.ai.open_resolved_scope_session_context",
                new=lambda **_kwargs: nullcontext(
                    SimpleNamespace(storage=MagicMock(), session=None),
                ),
            ),
            patch("mindroom.ai._prepare_agent_and_prompt", new=AsyncMock(return_value=_prepared_run(mock_agent))),
            patch("mindroom.ai.ai_runtime.cached_agent_run", new=AsyncMock(side_effect=fake_cached_run)),
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=mock_gateway_edit_message),
            ),
        ):
            _build_response_runner(bot)

            with pytest.raises(asyncio.CancelledError, match=USER_STOP_CANCEL_MSG):
                await bot._response_runner.process_and_respond(
                    _response_request(existing_event_id="$thinking_msg"),
                )

        assert len(edited_messages) == 1
        event_id, text = edited_messages[0]
        assert event_id == "$thinking_msg"
        assert text == CANCELLED_RESPONSE_NOTE

    @pytest.mark.asyncio
    async def test_run_cancelled_event_preserves_user_stop_note_in_streaming(self, tmp_path: Path) -> None:
        """RunCancelledEvent should keep a user-stop label in the final streamed edit."""
        bot = _mock_bot(tmp_path)
        bot._handle_interactive_question = AsyncMock()

        edited_messages: list[tuple[str, str]] = []

        async def mock_stream_edit_message(
            client: object,  # noqa: ARG001
            room_id: str,  # noqa: ARG001
            event_id: str,
            content: dict[str, object],
            text: str,
        ) -> DeliveredMatrixEvent:
            edited_messages.append((event_id, text))
            return DeliveredMatrixEvent(event_id="$edit", content_sent=content)

        mock_agent = MagicMock()
        mock_agent.model = MagicMock()
        mock_agent.model.id = "test-model"
        mock_agent.name = "Test Agent"
        mock_agent.add_history_to_context = False

        async def fake_arun_stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            yield RunContentEvent(content="Partial answer")
            current_task = asyncio.current_task()
            assert current_task is not None
            request_task_cancel(current_task, cancel_msg=USER_STOP_CANCEL_MSG)
            yield RunCancelledEvent(
                run_id="run-2",
                session_id="session-1",
                reason="Run run-2 was cancelled",
            )

        with (
            patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
            patch(
                "mindroom.ai.open_resolved_scope_session_context",
                new=lambda **_kwargs: nullcontext(
                    SimpleNamespace(storage=MagicMock(), session=None),
                ),
            ),
            patch("mindroom.ai._prepare_agent_and_prompt", new=AsyncMock(return_value=_prepared_run(mock_agent))),
            patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=mock_stream_edit_message)),
        ):
            _build_response_runner(bot)
            mock_agent.arun = MagicMock(return_value=fake_arun_stream())

            with pytest.raises(asyncio.CancelledError, match=USER_STOP_CANCEL_MSG):
                await bot._response_runner.process_and_respond_streaming(
                    _response_request(existing_event_id="$thinking_msg"),
                )

        assert len(edited_messages) == 2
        event_id, text = edited_messages[-1]
        assert event_id == "$thinking_msg"
        assert text == f"Partial answer\n\n{CANCELLED_RESPONSE_NOTE}"

    @pytest.mark.asyncio
    async def test_various_error_messages_are_user_friendly(self, tmp_path: Path) -> None:
        """Test that various error types result in user-friendly messages."""
        bot = _mock_bot(tmp_path)

        edited_messages = []

        async def mock_gateway_edit_message(
            client: object,  # noqa: ARG001
            room_id: str,  # noqa: ARG001
            event_id: str,  # noqa: ARG001
            content: dict[str, object],
            text: str,
        ) -> DeliveredMatrixEvent:
            edited_messages.append(text)
            return DeliveredMatrixEvent(event_id="$edit", content_sent=content)

        error_messages = [
            "[test_agent] 🔴 Authentication failed. Please check your API key configuration.",
            "[test_agent] 🔴 Rate limited. Please wait before trying again.",
            "[test_agent] 🔴 Request timed out. Please try again.",
            "[test_agent] 🔴 Service temporarily unavailable. Please try again later.",
            "[test_agent] 🔴 Error: Invalid model specified. Please check your configuration.",
        ]

        for error_msg in error_messages:
            edited_messages.clear()

            with (
                patch("mindroom.response_runner.ai_response") as mock_ai,
                patch(
                    "mindroom.delivery_gateway.edit_message_result",
                    new=AsyncMock(side_effect=mock_gateway_edit_message),
                ),
            ):
                _build_response_runner(bot)
                mock_ai.return_value = error_msg

                await bot._response_runner.process_and_respond(
                    _response_request(
                        prompt="Help me",
                        existing_event_id=f"$thinking_{error_messages.index(error_msg)}",
                    ),
                )

                assert len(edited_messages) == 1
                displayed_msg = edited_messages[0]

                if "Authentication" in error_msg:
                    assert "Authentication" in displayed_msg
                elif "Rate limited" in error_msg:
                    assert "Rate limited" in displayed_msg
                elif "timed out" in error_msg:
                    assert "timed out" in displayed_msg
                elif "unavailable" in error_msg:
                    assert "unavailable" in displayed_msg
                elif "Invalid model" in error_msg:
                    assert "Invalid model" in displayed_msg

    @pytest.mark.asyncio
    async def test_non_streaming_sync_restart_edits_thinking_message_with_restart_status(
        self,
        tmp_path: Path,
    ) -> None:
        """Sync restarts should not render as user-initiated cancellation."""
        bot = _mock_bot(tmp_path)

        edited_messages: list[tuple[str, dict[str, object], str]] = []

        async def mock_gateway_edit_message(
            client: object,  # noqa: ARG001
            room_id: str,  # noqa: ARG001
            event_id: str,
            content: dict[str, object],
            text: str,
        ) -> DeliveredMatrixEvent:
            edited_messages.append((event_id, content, text))
            return DeliveredMatrixEvent(event_id="$edit", content_sent=content)

        with (
            patch("mindroom.response_runner.ai_response") as mock_ai,
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=mock_gateway_edit_message),
            ),
        ):
            _build_response_runner(bot)
            mock_ai.side_effect = asyncio.CancelledError(SYNC_RESTART_CANCEL_MSG)

            await bot._response_runner.process_and_respond(
                _response_request(existing_event_id="$thinking_msg"),
            )

        assert len(edited_messages) == 1
        event_id, content, text = edited_messages[0]
        assert event_id == "$thinking_msg"
        assert text == build_restart_interrupted_body("")
        assert content[STREAM_STATUS_KEY] == STREAM_STATUS_ERROR

    @pytest.mark.asyncio
    async def test_knowledge_init_failure_falls_back_to_response_without_knowledge(self, tmp_path: Path) -> None:
        """Matrix reply paths should continue when request-scoped knowledge init fails."""
        bot = _mock_bot(tmp_path)
        bot._knowledge_access_support.for_agent = MagicMock(return_value=None)

        with (
            patch(
                "mindroom.response_runner.ensure_request_knowledge_managers",
                new_callable=AsyncMock,
                side_effect=RuntimeError("knowledge init failed"),
            ),
            patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
            patch(
                "mindroom.delivery_gateway.send_message_result",
                new=AsyncMock(
                    return_value=DeliveredMatrixEvent(
                        event_id="$response_id",
                        content_sent={"body": "Response without knowledge", "msgtype": "m.text"},
                    ),
                ),
            ),
        ):
            _build_response_runner(bot)
            mock_ai.return_value = "Response without knowledge"

            delivery = await bot._response_runner.process_and_respond(
                _response_request(),
            )

        assert delivery.event_id == "$response_id"
        assert mock_ai.call_args.kwargs["knowledge"] is None
        bot.logger.exception.assert_called_once()
