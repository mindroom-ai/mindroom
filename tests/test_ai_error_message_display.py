"""Test that AI errors are properly displayed to users in the Matrix room."""

from __future__ import annotations

import asyncio
import tempfile
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.cancellation import SYNC_RESTART_CANCEL_MSG, USER_STOP_CANCEL_MSG
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import STREAM_STATUS_ERROR, STREAM_STATUS_KEY
from mindroom.history.types import HistoryScope
from mindroom.knowledge.utils import _KnowledgeResolution
from mindroom.llm.provider import TextDelta
from mindroom.matrix.client import DeliveredMatrixEvent
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.response_runner import ResponseRequest
from mindroom.response_sources import ResponseSources
from mindroom.streaming import _INTERRUPTED_RESPONSE_NOTE, build_restart_interrupted_body
from tests.bot_helpers import make_test_agent_bot, unique_room_send_responses
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_model_providers,
    install_runtime_journal_support,
    make_matrix_client_mock,
    patch_response_runner_module,
    replace_delivery_gateway_deps,
    replace_response_runner_deps,
    request_envelope,
    runtime_paths_for,
    test_runtime_paths,
    unwrap_extracted_collaborator,
)
from tests.fakes.scripted_model import ScriptedModel
from tests.response_attempt_helpers import install_direct_response_admission
from tests.response_runner_helpers import _noop_typing, stream_transport, streamed_answer
from tests.scripted_agent_helpers import stop_turn

if TYPE_CHECKING:
    from mindroom.bot import AgentBot
    from mindroom.final_delivery import FinalDeliveryOutcome


def _runtime_bound_config() -> Config:
    """Return a minimal runtime-bound config for bot error-display tests."""
    return bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", rooms=["!room:localhost"])},
        ),
        test_runtime_paths(Path(tempfile.mkdtemp())),
    )


def _mock_bot() -> AgentBot:
    """Create a bot test instance with explicit mocked collaborators."""
    config = _runtime_bound_config()
    bot = make_test_agent_bot(
        AgentMatrixUser(
            agent_name="test_agent",
            password=TEST_PASSWORD,
            display_name="Test Agent",
            user_id="@mindroom_test_agent:localhost",
        ),
        config,
        runtime_paths_for(config),
        rooms=["!room:localhost"],
    )
    install_direct_response_admission(bot)
    bot.logger = MagicMock()
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.user_id = "@mindroom_test_agent:localhost"
    install_runtime_journal_support(bot)
    bot._conversation_resolver.build_message_target = MagicMock(
        return_value=MessageTarget.resolve("!room:localhost", None, None, room_mode=True),
    )
    bot._conversation_state_writer = MagicMock()
    bot._conversation_state_writer.create_storage = MagicMock(return_value=MagicMock())
    bot._conversation_state_writer.apersist_response_event_id_in_session_run = AsyncMock()
    bot._conversation_state_writer.history_scope = MagicMock(
        return_value=HistoryScope(kind="agent", scope_id=bot.agent_name),
    )
    bot._conversation_state_writer.team_history_scope = MagicMock(
        return_value=HistoryScope(kind="team", scope_id=bot.agent_name),
    )
    bot._knowledge_access_support = _knowledge_access_support()
    return bot


def _knowledge_access_support() -> SimpleNamespace:
    return SimpleNamespace(
        for_agent=MagicMock(return_value=None),
        resolve_for_agent=MagicMock(return_value=_KnowledgeResolution(knowledge=None)),
        resolve_for_agent_async=AsyncMock(return_value=_KnowledgeResolution(knowledge=None)),
    )


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
    room_id: str = "!room:localhost",
    reply_to_event_id: str = "$user_msg",
    thread_id: str | None = None,
    prompt: str = "Help me with something",
) -> ResponseRequest:
    """Build one response request for direct bot seam tests."""
    return ResponseRequest(
        sources=ResponseSources(
            pending_event_ids=(reply_to_event_id,),
            logical_source_event_ids=(reply_to_event_id,),
        ),
        thread_history=(),
        prompt=prompt,
        response_envelope=request_envelope(
            room_id=room_id,
            reply_to_event_id=reply_to_event_id,
            thread_id=thread_id,
            prompt=prompt,
        ),
    )


async def _respond(bot: AgentBot, request: ResponseRequest, *, streaming: bool = False) -> FinalDeliveryOutcome:
    """Run the request's locked turn, streamed or delivered whole, and return how its reply ended."""
    runner = unwrap_extracted_collaborator(bot._response_runner)
    outcomes: list[FinalDeliveryOutcome] = []

    async def record_outcome(outcome: FinalDeliveryOutcome, *_args: object) -> None:
        outcomes.append(outcome)

    with patch_response_runner_module(
        should_use_streaming=AsyncMock(return_value=streaming),
        typing_indicator=_noop_typing,
        apply_post_response_effects=AsyncMock(side_effect=record_outcome),
    ):
        await runner.generate_response(request)
    [outcome] = outcomes
    return outcome


def _record_reply_edits(edited: list[tuple[str, dict[str, object]]]) -> AsyncMock:
    """Record each reply edit Matrix receives as the event it replaces and its new content.

    Each new message, as a reply's placeholder is, gets its own ``$thinking_<n>`` event.
    """
    placeholders = count(1)

    async def send(
        _client: object,
        _room_id: str,
        envelope: dict[str, object],
        **_kwargs: object,
    ) -> DeliveredMatrixEvent:
        if "m.new_content" not in envelope:
            return DeliveredMatrixEvent(event_id=f"$thinking_{next(placeholders)}", content_sent=envelope)
        edited.append((envelope["m.relates_to"]["event_id"], envelope["m.new_content"]))
        return DeliveredMatrixEvent(event_id="$edit", content_sent=envelope)

    return AsyncMock(side_effect=send)


class TestAIErrorDisplay:
    """Test that AI errors are shown to users properly."""

    @pytest.mark.asyncio
    async def test_non_streaming_error_edits_thinking_message(self) -> None:
        """Test that when AI fails in non-streaming mode, the thinking message is edited with the error."""
        bot = _mock_bot()

        edited_messages: list[tuple[str, dict[str, object]]] = []

        error_msg = "[test_agent] 🔴 Authentication failed. Please check your API key configuration."
        with (
            patch(
                "mindroom.response_runner.stream_agent_response",
                new=streamed_answer(AsyncMock(return_value=error_msg)),
            ),
            patch("mindroom.delivery_gateway.send_message_outcome", new=_record_reply_edits(edited_messages)),
        ):
            _build_response_runner(bot)
            await _respond(bot, _response_request())

            assert len(edited_messages) == 1
            event_id, content = edited_messages[0]
            assert event_id == "$thinking_1"
            assert "Authentication failed" in content["body"]
            assert "API key" in content["body"]

    @pytest.mark.asyncio
    async def test_streaming_error_updates_message(self) -> None:
        """Test that when streaming AI fails, the message is updated with the error."""
        bot = _mock_bot()

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

        error_text = "[test_agent] 🔴 Rate limited. Please wait before trying again."
        edited_messages: list[tuple[str, dict[str, object]]] = []
        with (
            patch(
                "mindroom.response_runner.stream_agent_response",
                new=streamed_answer(AsyncMock(return_value=error_text)),
            ),
            patch("mindroom.delivery_gateway.send_message_outcome", new=_record_reply_edits(edited_messages)),
            patch(
                "mindroom.delivery_gateway.DeliveryGateway.deliver_stream",
                new=AsyncMock(return_value=stream_transport("$thinking_1", error_text)),
            ) as mock_deliver_stream,
        ):
            _build_response_runner(bot)
            await _respond(bot, _response_request(), streaming=True)

        # Verify the delivery gateway was asked to stream the response into the thinking message.
        mock_deliver_stream.assert_awaited_once()
        assert mock_deliver_stream.await_args.args[0].existing_event_id == "$thinking_1"

    @pytest.mark.asyncio
    async def test_cancellation_shows_cancelled_message(self) -> None:
        """Test that when a response is cancelled, it shows a cancellation message."""
        bot = _mock_bot()
        edited_messages: list[tuple[str, dict[str, object]]] = []

        # Mock ai_response to raise a generic interruption.
        with (
            patch(
                "mindroom.response_runner.stream_agent_response",
                new=streamed_answer(AsyncMock(side_effect=asyncio.CancelledError())),
            ),
            patch("mindroom.delivery_gateway.send_message_outcome", new=_record_reply_edits(edited_messages)),
        ):
            _build_response_runner(bot)
            await _respond(bot, _response_request())

            # Verify the thinking message was edited with the generic interruption message.
            assert len(edited_messages) == 1
            event_id, content = edited_messages[0]
            assert event_id == "$thinking_1"
            assert content["body"] == _INTERRUPTED_RESPONSE_NOTE

    @pytest.mark.asyncio
    async def test_user_stop_reports_a_user_stop_outcome(self) -> None:
        """Explicit stop-button cancellations should keep the user-stop outcome its reply's note shows."""
        bot = _mock_bot()

        unique_room_send_responses(bot.client)
        with patch(
            "mindroom.response_runner.stream_agent_response",
            new=streamed_answer(AsyncMock(side_effect=asyncio.CancelledError(USER_STOP_CANCEL_MSG))),
        ):
            _build_response_runner(bot)
            outcome = await _respond(bot, _response_request())

        assert outcome.terminal_status == "cancelled"
        assert outcome.failure_reason == "cancelled_by_user"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("show_tool_calls", [False, True])
    async def test_a_user_stop_mid_run_keeps_the_user_stop_outcome(
        self,
        monkeypatch: pytest.MonkeyPatch,
        show_tool_calls: bool,
    ) -> None:
        """A stop the user asks for while the model streams ends the blocking reply as stopped by the user."""
        bot = _mock_bot()
        unique_room_send_responses(bot.client)
        bot.config.agents["test_agent"].show_tool_calls = show_tool_calls
        model = ScriptedModel([[TextDelta("Partial answer"), stop_turn("user_stop")]])
        install_model_providers(monkeypatch, {"default": model})
        _build_response_runner(bot)

        outcome = await _respond(bot, _response_request())

        assert outcome.terminal_status == "cancelled"
        assert outcome.failure_reason == "cancelled_by_user"

    @pytest.mark.asyncio
    async def test_a_user_stop_mid_stream_keeps_the_streamed_text_and_the_user_stop_outcome(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stop the user asks for mid-stream keeps the text streamed so far and ends as stopped by the user."""
        bot = _mock_bot()
        unique_room_send_responses(bot.client)
        model = ScriptedModel([[TextDelta("Partial answer"), stop_turn("user_stop")]])
        install_model_providers(monkeypatch, {"default": model})
        _build_response_runner(bot)

        outcome = await _respond(bot, _response_request(), streaming=True)

        # The stopped stream's message keeps what it showed, with the interruption noted below it.
        final_content = bot.client.room_send.await_args.kwargs["content"]["m.new_content"]
        assert final_content["body"] == f"Partial answer\n\n{_INTERRUPTED_RESPONSE_NOTE}"
        assert outcome.terminal_status == "cancelled"
        assert outcome.failure_reason == "cancelled_by_user"

    @pytest.mark.asyncio
    async def test_various_error_messages_are_user_friendly(self) -> None:
        """Test that various error types result in user-friendly messages."""
        bot = _mock_bot()

        edited_messages: list[tuple[str, dict[str, object]]] = []
        # One homeserver for every turn, so each placeholder is its own event.
        send_message_outcome = _record_reply_edits(edited_messages)

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
                patch(
                    "mindroom.response_runner.stream_agent_response",
                    new=streamed_answer(AsyncMock(return_value=error_msg)),
                ),
                patch("mindroom.delivery_gateway.send_message_outcome", new=send_message_outcome),
            ):
                _build_response_runner(bot)
                # Each iteration is a separate turn, so it needs a separate
                # causing event. Sharing one would make the outbox treat the
                # later answers as resends of the first and replay it.
                index = error_messages.index(error_msg)
                await _respond(
                    bot,
                    _response_request(prompt="Help me", reply_to_event_id=f"$user_msg_{index}"),
                )

                assert len(edited_messages) == 1
                displayed_msg = str(edited_messages[0][1]["body"])

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
    ) -> None:
        """Sync restarts should not render as user-initiated cancellation."""
        bot = _mock_bot()

        edited_messages: list[tuple[str, dict[str, object]]] = []

        with (
            patch(
                "mindroom.response_runner.stream_agent_response",
                new=streamed_answer(AsyncMock(side_effect=asyncio.CancelledError(SYNC_RESTART_CANCEL_MSG))),
            ),
            patch("mindroom.delivery_gateway.send_message_outcome", new=_record_reply_edits(edited_messages)),
        ):
            _build_response_runner(bot)
            await _respond(bot, _response_request())

        assert len(edited_messages) == 1
        event_id, content = edited_messages[0]
        assert event_id == "$thinking_1"
        assert content["body"] == build_restart_interrupted_body("")
        assert content[STREAM_STATUS_KEY] == STREAM_STATUS_ERROR

    @pytest.mark.asyncio
    async def test_unavailable_knowledge_falls_back_to_response_without_knowledge(self) -> None:
        """Matrix reply paths should continue when no published knowledge is available."""
        bot = _mock_bot()
        bot._knowledge_access_support = _knowledge_access_support()

        mock_ai = AsyncMock(return_value="Response without knowledge")
        with (
            patch("mindroom.response_runner.stream_agent_response", new=streamed_answer(mock_ai)),
            patch(
                "mindroom.delivery_gateway.send_message_outcome",
                new=AsyncMock(
                    return_value=DeliveredMatrixEvent(
                        event_id="$response_id",
                        content_sent={"body": "Response without knowledge", "msgtype": "m.text"},
                    ),
                ),
            ),
        ):
            _build_response_runner(bot)
            outcome = await _respond(bot, _response_request())

        assert outcome.event_id == "$response_id"
        assert mock_ai.call_args.args[0].knowledge is None
        bot.logger.exception.assert_not_called()
