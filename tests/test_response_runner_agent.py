"""Agent response lifecycle through the ResponseRunner seam: generate_response, its delivery, and its locked turn."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import nio
import pytest

from mindroom import reply_lifecycle as rl
from mindroom import response_lifecycle
from mindroom.approval.continuation_runner import ApprovalContinuationRunner
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.cancellation import SYNC_RESTART_CANCEL_MSG
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.participation import ParticipationConfig
from mindroom.constants import (
    ACTING_REQUESTER_KEY,
    ATTACHMENT_IDS_KEY,
    SILENT_SCHEDULE_NO_REPLY_TOKEN,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    TOOL_TRACE_CONTENT_KEY,
    RuntimePaths,
)
from mindroom.conversation_resolver import MessageContext
from mindroom.delivery_gateway import (
    FinalizeStreamedResponseRequest,
    ResponseIdentity,
)
from mindroom.dispatch_source import (
    MESSAGE_SOURCE_KIND,
    SCHEDULED_SOURCE_KIND,
    SILENT_SCHEDULE_SOURCE_KIND,
)
from mindroom.final_delivery import (
    AwaitingApproval,
    Cancelled,
    Completed,
    Failed,
    FailureCause,
    FinalDeliveryOutcome,
    StreamTransportOutcome,
    Suppressed,
    Suppression,
    TurnProgress,
    outcome_label,
)
from mindroom.history.storage import with_force_compaction
from mindroom.history.types import CompactionLifecycleStart, HistoryScope
from mindroom.hooks import (
    EVENT_MESSAGE_AFTER_RESPONSE,
    EVENT_MESSAGE_BEFORE_RESPONSE,
    AfterResponseContext,
    BeforeResponseContext,
    EnrichmentItem,
    HookRegistry,
    MessageEnvelope,
    hook,
)
from mindroom.inbound_turn_normalizer import DispatchPayload
from mindroom.judgment.client import JudgmentClient
from mindroom.judgment.typesafe import _PINNED_MODEL
from mindroom.knowledge.utils import _KnowledgeResolution
from mindroom.llm.events import ReportedCall, ToolCompleted, ToolStarted
from mindroom.llm.runs import RunRequirement, ToolExecution
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.matrix.conversation_reads import DeliveredResponse
from mindroom.matrix.thread_history_result import ThreadHistoryResult, thread_history_result
from mindroom.message_target import MessageTarget
from mindroom.reply_scope import ReplyRuntime
from mindroom.reply_turn.enrichment import _cached_room_display_name, with_matrix_message_target
from mindroom.reply_turn.locked_turn import LockedTurns
from mindroom.reply_turn.request import ResponseRequest, merge_response_extra_content
from mindroom.response_payload_preparation import (
    DispatchPayloadInputs,
    ResponsePayloadPreparation,
    ResponsePayloadPreparer,
)
from mindroom.response_runner import ResponseRunner
from mindroom.response_sources import Owned, ResponseSources, Unclaimed, UnclaimedReason
from mindroom.response_turn import PausedAttempt, ResponsePausedForApproval
from mindroom.session_storage.types import ConversationSession
from mindroom.streaming import PausePresentationError, StreamingDeliveryError
from mindroom.tool_system.events import FailedReply
from mindroom.turn_policy import PreparedDispatch
from mindroom.turn_store import SourcePreparation
from tests.bot_helpers import (
    AgentBotTestBase,
    _hook_envelope,
    _hook_plugin,
    _make_matrix_client_mock,
    _noop_typing_indicator,
    _response_request,
    _runtime_bound_config,
    _set_knowledge_for_agent,
    _visible_message,
    _visible_response_event_id,
    make_mock_agent_user,
    make_test_agent_bot,
    unique_room_send_responses,
)
from tests.conftest import (
    delivered_matrix_event,
    delivered_matrix_side_effect,
    install_model_providers,
    install_runtime_journal_support,
    message_origin,
    patch_response_runner_module,
    request_envelope,
    runtime_paths_for,
    seed_session,
    unwrap_extracted_collaborator,
)
from tests.fakes.scripted_model import Await, ScriptedModel, ScriptStep, answer
from tests.journal_membership_helpers import admit_room_membership
from tests.response_attempt_helpers import install_direct_response_admission
from tests.response_runner_helpers import (
    attempt_in_place,
    delivered_turn,
    failed_answer,
    recorded_reply_event,
    stream_transport,
    streamed_answer,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine, Sequence
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.users import AgentMatrixUser
    from mindroom.post_response_effects import ResponseOutcome
    from mindroom.response_turn import ResponseTurnContext


async def _respond(bot: AgentBot, request: ResponseRequest, *, streaming: bool = False) -> FinalDeliveryOutcome:
    """Run ``request`` through the runner's locked turn, streamed or delivered whole, and return how its reply ended."""
    runner = unwrap_extracted_collaborator(bot._response_runner)
    ended: list[FinalDeliveryOutcome] = []
    apply_post_response_effects = response_lifecycle.apply_post_response_effects

    async def record_ending(delivery: FinalDeliveryOutcome, *args: object, **kwargs: object) -> None:
        ended.append(delivery)
        await apply_post_response_effects(delivery, *args, **kwargs)  # type: ignore[arg-type]

    with patch_response_runner_module(
        should_use_streaming=AsyncMock(return_value=streaming),
        apply_post_response_effects=record_ending,
    ):
        await runner.generate_response(request)
    [delivery] = ended
    return delivery


def _consuming_send(
    transport: StreamTransportOutcome,
    consumed: list[object] | None = None,
) -> Callable[..., Awaitable[StreamTransportOutcome]]:
    """Return a ``send_streaming_response`` that reads the whole response stream, then reports ``transport``.

    Given ``consumed``, it records there each chunk it read.
    """

    async def send(*args: object, **_kwargs: object) -> StreamTransportOutcome:
        async for chunk in cast("AsyncIterator[object]", args[4]):
            if consumed is not None:
                consumed.append(chunk)
        return transport

    return send


@pytest.fixture
def mock_agent_user() -> AgentMatrixUser:
    """Mock agent user for testing."""
    return make_mock_agent_user()


def _matrix_target_item(mock_response: AsyncMock | MagicMock) -> EnrichmentItem:
    turn_context = mock_response.call_args.args[0]
    return next(item for item in turn_context.transient_enrichment_items if item.key == "matrix_message_target")


def test_with_matrix_message_target_drops_hook_owned_reserved_item_without_runtime_target() -> None:
    """Hooks cannot supply the reserved target context when the runtime has none."""
    regular_item = EnrichmentItem(key="regular", text="Keep me")
    hook_target = EnrichmentItem(key="matrix_message_target", text="Fake target")

    assert with_matrix_message_target((regular_item, hook_target), None) == (regular_item,)


def test_cached_room_display_name_uses_synced_matrix_room() -> None:
    """Matrix target context should use room name without fetching state."""
    room = nio.MatrixRoom("!test:localhost", "@mindroom_test:localhost")
    room.name = "Engineering"
    runtime = SimpleNamespace(client=SimpleNamespace(rooms={room.room_id: room}))

    assert _cached_room_display_name(runtime, room.room_id) == "Engineering"
    assert _cached_room_display_name(runtime, "!unknown:localhost") is None


class TestAgentBot(AgentBotTestBase):
    """Agent replies through the bot's response runner: preparation, delivery, hooks, and settlement."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("source_kind", "expects_quiet_instruction"),
        [
            (SILENT_SCHEDULE_SOURCE_KIND, True),
            (SCHEDULED_SOURCE_KIND, False),
        ],
    )
    async def test_payload_preparation_adds_only_silent_schedule_delivery_instruction(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        source_kind: str,
        expects_quiet_instruction: bool,
    ) -> None:
        """Only silent scheduled turns receive quiet-delivery guidance, as a ``persist=False`` item."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        target = MessageTarget.resolve("!test:localhost", None, "$event")
        envelope = request_envelope(
            room_id=target.room_id,
            reply_to_event_id="$event",
            prompt="Check for updates",
            user_id="@user:localhost",
            target=target,
            agent_name=bot.agent_name,
            source_kind=source_kind,
        )
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=True,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
            ),
            target=target,
            correlation_id="$event",
            envelope=envelope,
        )
        preparation = ResponsePayloadPreparation(
            dispatch=dispatch,
            prompt="Check for updates",
            action_kind="individual",
            payload_inputs=DispatchPayloadInputs((), (), ()),
            target_member_names=None,
            dispatch_started_at=0.0,
            context_ready_monotonic=0.0,
        )

        preparer = ResponsePayloadPreparer(
            normalizer=cast(
                "Any",
                SimpleNamespace(
                    build_dispatch_payload_with_attachments=AsyncMock(
                        return_value=DispatchPayload(prompt="Check for updates"),
                    ),
                ),
            ),
            ingress_hook_runner=bot._ingress_hook_runner,
            agent_name=bot.agent_name,
            logger=bot.logger,
        )
        bot._response_runner.locked.deps = replace(bot._response_runner.deps, request_preparer=preparer)
        prepared = await bot._response_runner._prepare_request_after_lock(
            ResponseRequest(
                sources=ResponseSources(
                    pending_event_ids=(envelope.source_event_id,),
                    logical_source_event_ids=(envelope.source_event_id,),
                ),
                thread_history=[],
                prompt="Check for updates",
                user_id="@user:localhost",
                response_envelope=envelope,
                payload_preparation=preparation,
            ),
        )

        delivery_items = [item for item in prepared.system_enrichment_items if item.key == "silent_schedule_delivery"]
        assert bool(delivery_items) is expects_quiet_instruction
        if delivery_items:
            instruction = delivery_items[0]
            assert instruction.persist is False
            assert SILENT_SCHEDULE_NO_REPLY_TOKEN in instruction.text
            assert "findings" in instruction.text.lower()
            assert "failures" in instruction.text.lower()

    @pytest.mark.asyncio
    async def test_agent_reply_includes_matrix_metadata_when_tool_enabled(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Agents with matrix_message should receive transient Matrix targeting context."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        tools=["matrix_message"],
                    ),
                },
            ),
            tmp_path,
        )
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        room = nio.MatrixRoom("!test:localhost", mock_agent_user.matrix_id.full_id)
        room.name = "Engineering"
        bot.client.rooms = {room.room_id: room}
        # The placeholder is the first event the reply sends, and the answer edits it.
        unique_room_send_responses(bot.client, prefix="$response")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        mock_ai = AsyncMock(return_value="Handled")
        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            stream_agent_response=streamed_answer(mock_ai),
        ):
            delivery = await _respond(
                bot,
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please send an update",
                    reply_to_event_id="$event123",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event123",
                        prompt="Please send an update",
                        user_id="@user:localhost",
                    ),
                ),
            )

        assert delivery.event_id == "$response1"
        assert mock_ai.call_args.args[0].prompt == "Please send an update"
        model_prompt = mock_ai.call_args.args[0].model_prompt
        assert "[Matrix metadata for tool calls]" not in model_prompt
        target_item = _matrix_target_item(mock_ai)
        assert target_item.cache_policy == "stable"
        assert "Matrix room 'Engineering' (room ID !test:localhost)" in target_item.text
        assert "outside any thread" in target_item.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("source_kind", "allows_no_report_response"),
        [
            (SILENT_SCHEDULE_SOURCE_KIND, True),
            (SCHEDULED_SOURCE_KIND, False),
        ],
    )
    async def test_agent_turn_context_allows_no_report_only_for_silent_schedules(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        source_kind: str,
        allows_no_report_response: bool,
    ) -> None:
        """The immutable response source alone controls first-empty acceptance."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        # The placeholder is the first event the reply sends, and the answer edits it.
        unique_room_send_responses(bot.client, prefix="$response")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        mock_ai = AsyncMock(return_value="Handled")
        typing_events: list[str] = []

        @asynccontextmanager
        async def record_typing(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            typing_events.append("started")
            yield

        with patch_response_runner_module(
            typing_indicator=record_typing,
            stream_agent_response=streamed_answer(mock_ai),
        ):
            await _respond(
                bot,
                _response_request(
                    room_id="!test:localhost",
                    prompt="Check for updates",
                    reply_to_event_id="$event",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        prompt="Check for updates",
                        user_id="@user:localhost",
                        agent_name=bot.agent_name,
                        source_kind=source_kind,
                    ),
                ),
            )

        assert mock_ai.await_args.args[0].allow_no_report_response is allows_no_report_response
        assert typing_events == ([] if allows_no_report_response else ["started"])
        assert (mock_ai.await_args.args[0].compaction_lifecycle is None) is allows_no_report_response

    @pytest.mark.asyncio
    async def test_agent_reply_includes_matrix_metadata_when_openclaw_compat_enabled(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """openclaw_compat agents should receive transient Matrix targeting context."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        tools=["openclaw_compat"],
                        include_default_tools=False,
                    ),
                },
            ),
            tmp_path,
        )
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        room = nio.MatrixRoom("!test:localhost", mock_agent_user.matrix_id.full_id)
        bot.client.rooms = {room.room_id: room}
        # The placeholder is the first event the reply sends, and the answer edits it.
        unique_room_send_responses(bot.client, prefix="$response")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        mock_ai = AsyncMock(return_value="Handled")
        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            stream_agent_response=streamed_answer(mock_ai),
        ):
            delivery = await _respond(
                bot,
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please send an update",
                    reply_to_event_id="$event123",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event123",
                        prompt="Please send an update",
                        user_id="@user:localhost",
                    ),
                ),
            )

        assert delivery.event_id == "$response1"
        assert mock_ai.call_args.args[0].prompt == "Please send an update"
        model_prompt = mock_ai.call_args.args[0].model_prompt
        assert "[Matrix metadata for tool calls]" not in model_prompt
        assert "!test:localhost" in _matrix_target_item(mock_ai).text

    @pytest.mark.asyncio
    async def test_streamed_agent_reply_includes_matrix_metadata_when_tool_enabled(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming path should inject transient Matrix targeting context."""

        async def mock_streaming_response() -> AsyncGenerator[str]:
            yield "chunk"

        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        tools=["matrix_message"],
                    ),
                },
            ),
            tmp_path,
        )
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        room = nio.MatrixRoom("!test:localhost", mock_agent_user.matrix_id.full_id)
        room.name = "Engineering"
        bot.client.rooms = {room.room_id: room}
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        mock_stream_agent_response = MagicMock()

        with patch(
            "mindroom.delivery_gateway.send_streaming_response",
            new_callable=AsyncMock,
        ) as mock_send_streaming_response:
            mock_stream_agent_response.return_value = mock_streaming_response()
            mock_send_streaming_response.side_effect = _consuming_send(stream_transport("$response", "chunk"))
            with patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=mock_stream_agent_response,
            ):
                delivery = await _respond(
                    bot,
                    _response_request(
                        room_id="!test:localhost",
                        prompt="Please reply in thread",
                        reply_to_event_id="$event456",
                        thread_history=[],
                        user_id="@user:localhost",
                        response_envelope=request_envelope(
                            room_id="!test:localhost",
                            reply_to_event_id="$event456",
                            prompt="Please reply in thread",
                            user_id="@user:localhost",
                        ),
                    ),
                    streaming=True,
                )

        assert delivery.event_id == "$response"
        assert mock_stream_agent_response.call_args.args[0].prompt == "Please reply in thread"
        model_prompt = mock_stream_agent_response.call_args.args[0].model_prompt
        assert "[Matrix metadata for tool calls]" not in model_prompt
        assert (
            "Matrix room 'Engineering' (room ID !test:localhost)"
            in _matrix_target_item(
                mock_stream_agent_response,
            ).text
        )

    @pytest.mark.asyncio
    async def test_agent_reply_uses_safe_thread_root_for_prompt_metadata(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Prompt metadata should prefer the stable thread root over plain reply event IDs."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        tools=["matrix_message"],
                        include_default_tools=False,
                    ),
                },
            ),
            tmp_path,
        )
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        room = nio.MatrixRoom("!test:localhost", mock_agent_user.matrix_id.full_id)
        bot.client.rooms = {room.room_id: room}
        # The placeholder is the first event the reply sends, and the answer edits it.
        unique_room_send_responses(bot.client, prefix="$response")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        mock_ai = AsyncMock(return_value="Handled")
        target = MessageTarget.resolve(
            room_id="!test:localhost",
            thread_id=None,
            reply_to_event_id="$reply_plain:localhost",
            thread_start_root_event_id="$thread_root:localhost",
        )

        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            stream_agent_response=streamed_answer(mock_ai),
        ):
            await _respond(
                bot,
                _response_request(
                    room_id="!test:localhost",
                    prompt="Continue",
                    reply_to_event_id="$reply_plain:localhost",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$reply_plain:localhost",
                        prompt="Continue",
                        user_id="@user:localhost",
                        target=target,
                    ),
                ),
            )

        target_text = _matrix_target_item(mock_ai).text
        assert "$thread_root:localhost" in target_text
        assert "$reply_plain:localhost" not in target_text

    @pytest.mark.asyncio
    async def test_agent_reply_keeps_thread_root_metadata_when_reply_anchor_is_root(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Thread-root replies should preserve the canonical thread id in tool-call metadata."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        tools=["matrix_message"],
                        include_default_tools=False,
                    ),
                },
            ),
            tmp_path,
        )
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        room = nio.MatrixRoom("!test:localhost", mock_agent_user.matrix_id.full_id)
        bot.client.rooms = {room.room_id: room}
        # The placeholder is the first event the reply sends, and the answer edits it.
        unique_room_send_responses(bot.client, prefix="$response")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        mock_ai = AsyncMock(return_value="Handled")
        target = MessageTarget.resolve(
            room_id="!test:localhost",
            thread_id=None,
            reply_to_event_id="$thread_root:localhost",
            thread_start_root_event_id="$thread_root:localhost",
        )

        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            stream_agent_response=streamed_answer(mock_ai),
        ):
            await _respond(
                bot,
                _response_request(
                    room_id="!test:localhost",
                    prompt="Continue",
                    reply_to_event_id="$thread_root:localhost",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$thread_root:localhost",
                        prompt="Continue",
                        user_id="@user:localhost",
                        target=target,
                    ),
                ),
            )

        target_text = _matrix_target_item(mock_ai).text
        assert "$thread_root:localhost" in target_text

    @pytest.mark.asyncio
    async def test_streamed_agent_reply_resolves_knowledge_once(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming should resolve knowledge only inside the request-scoped context."""

        async def mock_streaming_response() -> AsyncGenerator[str]:
            yield "chunk"

        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                    ),
                },
            ),
            tmp_path,
        )
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        bot.client.rooms = {}
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        mock_stream_agent_response = MagicMock(return_value=mock_streaming_response())
        with patch(
            "mindroom.delivery_gateway.send_streaming_response",
            new_callable=AsyncMock,
        ) as mock_send_streaming_response:
            mock_send_streaming_response.side_effect = _consuming_send(stream_transport("$response", "chunk"))
            with patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=mock_stream_agent_response,
            ):
                delivery = await _respond(
                    bot,
                    _response_request(
                        room_id="!test:localhost",
                        prompt="Hello",
                        reply_to_event_id="$event456",
                        thread_history=[],
                        user_id="@user:localhost",
                        response_envelope=request_envelope(
                            room_id="!test:localhost",
                            reply_to_event_id="$event456",
                            prompt="Hello",
                            user_id="@user:localhost",
                        ),
                    ),
                    streaming=True,
                )

        assert delivery.event_id == "$response"
        bot._knowledge_access_support.resolve_for_agent_async.assert_awaited_once()
        args, kwargs = bot._knowledge_access_support.resolve_for_agent_async.call_args
        assert args == ("calculator",)
        assert kwargs["execution_identity"] is not None

    @pytest.mark.asyncio
    async def test_a_cancellation_during_knowledge_resolution_stores_no_interrupted_turn(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A cancellation during knowledge lookup comes before the turn driver starts, so no interrupted turn is stored."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                    ),
                },
            ),
            tmp_path,
        )
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        bot._knowledge_access_support.resolve_for_agent_async = AsyncMock(
            side_effect=asyncio.CancelledError("cancelled"),
        )
        persist_interrupted = MagicMock()

        with patch("mindroom.response_turn.persist_interrupted_replay_snapshot", new=persist_interrupted):
            await _respond(
                bot,
                _response_request(
                    room_id="!test:localhost",
                    prompt="Hello",
                    reply_to_event_id="$event456",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event456",
                        prompt="Hello",
                        user_id="@user:localhost",
                    ),
                ),
                streaming=True,
            )

        # Knowledge resolves before the turn driver starts, so nothing ran that the turn's record would keep; the
        # stopped message reaches the next turn through the thread.
        persist_interrupted.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_reply_includes_attachment_ids_in_response_metadata(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Non-streaming responses should persist attachment IDs in message metadata."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        # The placeholder is the first event the reply sends, and the answer edits it.
        unique_room_send_responses(bot.client, prefix="$response")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            kwargs["sinks"].run_metadata_collector["io.mindroom.ai_run"] = {"version": 1}
            return "Handled"

        mock_ai = AsyncMock(side_effect=fake_ai_response)
        attachment_ids = ["att_image", "att_zip"]
        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            stream_agent_response=streamed_answer(mock_ai),
        ):
            await _respond(
                bot,
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please inspect attachments",
                    reply_to_event_id="$event123",
                    thread_history=[],
                    user_id="@user:localhost",
                    attachment_ids=attachment_ids,
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event123",
                        prompt="Please inspect attachments",
                        user_id="@user:localhost",
                        attachment_ids=tuple(attachment_ids),
                    ),
                ),
            )

        sent_extra_content = bot.client.room_send.await_args.kwargs["content"]
        assert sent_extra_content[ATTACHMENT_IDS_KEY] == attachment_ids
        assert sent_extra_content["io.mindroom.ai_run"]["version"] == 1

    @pytest.mark.asyncio
    async def test_streamed_agent_reply_includes_attachment_ids_in_response_metadata(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming responses should persist attachment IDs in message metadata."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        bot.client.rooms = {}
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        captured_collector: dict[str, Any] = {}

        def fake_stream_agent_response(*_args: object, **kwargs: object) -> AsyncGenerator[str]:
            captured_collector.update({"ref": kwargs["sinks"].run_metadata_collector})

            async def _gen() -> AsyncGenerator[str]:
                yield "chunk"
                # Populate metadata during iteration, matching production ordering
                # where ai.py populates metadata after streaming completes.
                kwargs["sinks"].run_metadata_collector["io.mindroom.ai_run"] = {"version": 1}

            return _gen()

        attachment_ids = ["att_image", "att_zip"]
        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
                side_effect=_consuming_send(stream_transport("$response", "chunk")),
            ) as mock_send_streaming_response,
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=fake_stream_agent_response,
            ),
        ):
            await _respond(
                bot,
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please inspect attachments",
                    reply_to_event_id="$event456",
                    thread_history=[],
                    user_id="@user:localhost",
                    attachment_ids=attachment_ids,
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event456",
                        prompt="Please inspect attachments",
                        user_id="@user:localhost",
                        attachment_ids=tuple(attachment_ids),
                    ),
                ),
                streaming=True,
            )

        sent_extra_content = mock_send_streaming_response.await_args.kwargs["extra_content"]
        assert sent_extra_content[ATTACHMENT_IDS_KEY] == attachment_ids
        # Metadata was populated during generator iteration (not synchronously),
        # proving the mutable reference is preserved through merge_response_extra_content.
        assert sent_extra_content["io.mindroom.ai_run"]["version"] == 1
        # The stream sees the collector's live contents plus the human the reply was written for.
        assert dict(sent_extra_content) == {**captured_collector["ref"], ACTING_REQUESTER_KEY: "@user:localhost"}

    def test_merge_response_extra_content_preserves_mutable_reference(self) -> None:
        """merge_response_extra_content must return the SAME dict object when extra_content is provided."""
        collector: dict[str, Any] = {}
        result = merge_response_extra_content(collector, None)
        assert result is collector

    def test_merge_response_extra_content_returns_none_when_both_absent(self) -> None:
        """merge_response_extra_content returns None when no extra_content and no attachment_ids."""
        assert merge_response_extra_content(None, None) is None
        assert merge_response_extra_content(None, []) is None

    def test_merge_response_extra_content_merges_attachment_ids(self) -> None:
        """merge_response_extra_content merges attachment_ids into extra_content."""
        collector: dict[str, Any] = {}
        result = merge_response_extra_content(collector, ["att_1"])
        assert result is collector
        assert result[ATTACHMENT_IDS_KEY] == ["att_1"]

    def test_merge_response_extra_content_creates_dict_for_attachment_ids_only(self) -> None:
        """merge_response_extra_content creates a dict when only attachment_ids are provided."""
        result = merge_response_extra_content(None, ["att_1"])
        assert result is not None
        assert result[ATTACHMENT_IDS_KEY] == ["att_1"]

    @pytest.mark.asyncio
    async def test_streaming_metadata_propagation_through_mutable_reference(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Metadata populated during generator iteration must appear in extra_content via mutable reference."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        bot.client.rooms = {}
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        async def fake_stream_agent_response(*_args: object, **kwargs: object) -> AsyncGenerator[str]:
            yield "hello"
            # Populate after first yield, mimicking production ai.py ordering
            kwargs["sinks"].run_metadata_collector["io.mindroom.ai_run"] = {
                "version": 1,
                "model": "test-model",
                "tokens": {"input": 10, "output": 5},
            }

        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
                side_effect=_consuming_send(stream_transport("$response", "hello")),
            ) as mock_send_streaming_response,
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=fake_stream_agent_response,
            ),
        ):
            await _respond(
                bot,
                _response_request(
                    room_id="!test:localhost",
                    prompt="Hello",
                    reply_to_event_id="$event789",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event789",
                        prompt="Hello",
                        user_id="@user:localhost",
                    ),
                ),
                streaming=True,
            )

        sent_extra_content = mock_send_streaming_response.await_args.kwargs["extra_content"]
        assert sent_extra_content is not None
        ai_run = sent_extra_content["io.mindroom.ai_run"]
        assert ai_run["version"] == 1
        assert ai_run["model"] == "test-model"
        assert ai_run["tokens"] == {"input": 10, "output": 5}

    @pytest.mark.asyncio
    async def test_streaming_cancelled_response_preserves_metadata(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """CancelledError during streaming must still carry io.mindroom.ai_run in extra_content."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        bot.client.rooms = {}
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        async def fake_stream_agent_response(*_args: object, **kwargs: object) -> AsyncGenerator[str]:
            kwargs["sinks"].run_metadata_collector["io.mindroom.ai_run"] = {"version": 1}
            yield "partial"
            raise asyncio.CancelledError

        captured_extra_content_ref: list[dict[str, Any] | None] = [None]

        async def _consuming_send_streaming(*args: object, **kwargs: object) -> StreamTransportOutcome:
            captured_extra_content_ref[0] = kwargs.get("extra_content")
            stream = args[4]
            try:
                async for _ in stream:
                    pass
            except asyncio.CancelledError:
                pass
            # In production, send_streaming_response writes nothing for a cancelled
            # stream and re-raises; the reply's span exit ends it. We simulate the re-raise.
            raise asyncio.CancelledError

        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
                side_effect=_consuming_send_streaming,
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=fake_stream_agent_response,
            ),
        ):
            # The stream's own cancellation ends the reply cancelled; it does not cancel the turn's caller.
            delivery = await _respond(
                bot,
                _response_request(
                    room_id="!test:localhost",
                    prompt="Cancel me",
                    reply_to_event_id="$event_cancel",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event_cancel",
                        prompt="Cancel me",
                        user_id="@user:localhost",
                    ),
                ),
                streaming=True,
            )

        assert isinstance(delivery.ending, Cancelled)
        # The extra_content dict (mutable reference) was populated during iteration
        extra = captured_extra_content_ref[0]
        assert extra is not None
        assert "io.mindroom.ai_run" in extra
        assert extra["io.mindroom.ai_run"]["version"] == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("error", "shown"),
        [(RuntimeError("boom"), "boom"), (TimeoutError(), "TimeoutError")],
        ids=["with-text", "without-text"],
    )
    async def test_streamed_agent_reply_preserves_terminal_event_id_on_error(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        error: Exception,
        shown: str,
    ) -> None:
        """A stream that fails ends its reply failed, with the stream's own error noted on the event the reply shows."""

        async def mock_streaming_response() -> AsyncGenerator[str]:
            yield "chunk"

        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        # The reply's placeholder is the event its stream writes into.
        unique_room_send_responses(bot.client, prefix="$terminal")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        mock_stream_agent_response = MagicMock(return_value=mock_streaming_response())
        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
                side_effect=StreamingDeliveryError(error),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=mock_stream_agent_response,
            ),
        ):
            delivery = await _respond(
                bot,
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please continue",
                    reply_to_event_id="$event-error",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event-error",
                        prompt="Please continue",
                        user_id="@user:localhost",
                    ),
                ),
                streaming=True,
            )

        assert delivery.ending == Failed(FailureCause.ERROR, shown)
        assert _visible_response_event_id(delivery) == "$terminal1"
        note = bot.client.room_send.await_args.kwargs["content"]["m.new_content"]["body"]
        assert note.endswith(f"**[Response interrupted by an error: {shown}]**")

    @pytest.mark.asyncio
    async def test_a_pause_the_stream_could_not_show_ends_with_the_approval_start_note(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A stream that paused but could not show the pause ends with the approval-start note, not its error text."""

        async def mock_streaming_response() -> AsyncGenerator[str]:
            yield "chunk"

        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        # The reply's placeholder is the event its stream writes into.
        unique_room_send_responses(bot.client, prefix="$terminal")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        mock_stream_agent_response = MagicMock(return_value=mock_streaming_response())
        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
                side_effect=PausePresentationError(
                    RuntimeError("Stream suspension presentation delivery failed: refused"),
                ),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=mock_stream_agent_response,
            ),
        ):
            delivery = await _respond(
                bot,
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please continue",
                    reply_to_event_id="$event-error",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event-error",
                        prompt="Please continue",
                        user_id="@user:localhost",
                    ),
                ),
                streaming=True,
            )

        assert delivery.ending == Failed(FailureCause.APPROVAL_NOT_STARTED)
        # The span exit wrote the note over the placeholder.
        assert delivery.delivery_kind == "edited"
        assert delivery.response_text.endswith("Tool approval could not be started. Please try again.")
        assert "refused" not in delivery.response_text

    @pytest.mark.asyncio
    async def test_agent_reply_applies_before_and_after_hooks_non_streaming(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Non-streaming delivery applies before_response, and the after-response hook sees what it delivered."""
        after_results: list[tuple[str, str, str, str]] = []
        before_calls = 0

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            nonlocal before_calls
            before_calls += 1
            ctx.draft.response_text = f"{ctx.draft.response_text} [hooked]"

        @hook(EVENT_MESSAGE_AFTER_RESPONSE)
        async def after_hook(ctx: AfterResponseContext) -> None:
            after_results.append(
                (
                    ctx.result.response_event_id,
                    ctx.result.response_text,
                    ctx.result.delivery_kind,
                    ctx.result.response_kind,
                ),
            )

        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        # The placeholder is the first event the reply sends, and the answer edits it.
        unique_room_send_responses(bot.client, prefix="$response")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins(
            [_hook_plugin("hooked", [before_hook, after_hook])],
        )
        mock_ai = AsyncMock(return_value="Handled")
        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            stream_agent_response=streamed_answer(mock_ai),
        ):
            delivery = await _respond(
                bot,
                _response_request(
                    room_id="!test:localhost",
                    prompt="Please send an update",
                    reply_to_event_id="$event123",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=_hook_envelope(body="Please send an update", source_event_id="$event123"),
                    correlation_id="corr-hook",
                ),
            )

        assert delivery.event_id == "$response1"
        assert before_calls == 1
        assert bot.client.room_send.await_args.kwargs["content"]["m.new_content"]["body"] == "Handled [hooked]"
        assert after_results == [("$response1", "Handled [hooked]", "edited", "ai")]

    @pytest.mark.asyncio
    async def test_agent_reply_passes_active_response_event_ids(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Non-streaming AI calls should receive the events of the room's replies whose span runs here."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        # The placeholder is the first event the reply sends, and the answer edits it.
        unique_room_send_responses(bot.client, prefix="$response")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        live_event_ids = AsyncMock(return_value=frozenset({"$active"}))

        with patch.object(ReplyRuntime, "live_event_ids", new=live_event_ids):
            mock_ai_response = AsyncMock(return_value="Handled")
            with patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=streamed_answer(mock_ai_response),
            ):
                await _respond(
                    bot,
                    _response_request(
                        room_id="!test:localhost",
                        prompt="Please continue",
                        reply_to_event_id="$event123",
                        thread_history=[],
                        user_id="@user:localhost",
                        response_envelope=request_envelope(
                            room_id="!test:localhost",
                            reply_to_event_id="$event123",
                            prompt="Please continue",
                            user_id="@user:localhost",
                        ),
                    ),
                )

            assert mock_ai_response.call_args.args[0].active_event_ids == frozenset({"$active"})
        live_event_ids.assert_awaited_with("!test:localhost")

    @pytest.mark.asyncio
    async def test_streamed_agent_reply_ignores_post_visible_before_response_mutation(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streamed delivery keeps before_response off the post-visible path; the after-response hook sees the stream."""
        after_results: list[tuple[str, str, str, str]] = []
        before_calls = 0

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            nonlocal before_calls
            before_calls += 1
            ctx.draft.response_text = f"{ctx.draft.response_text} [hooked]"
            ctx.draft.suppress = True

        @hook(EVENT_MESSAGE_AFTER_RESPONSE)
        async def after_hook(ctx: AfterResponseContext) -> None:
            after_results.append(
                (
                    ctx.result.response_event_id,
                    ctx.result.response_text,
                    ctx.result.delivery_kind,
                    ctx.result.response_kind,
                ),
            )

        async def mock_streaming_response() -> AsyncGenerator[str]:
            yield "chunk"

        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        bot.client.rooms = {}
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins(
            [_hook_plugin("hooked", [before_hook, after_hook])],
        )
        mock_stream_agent_response = MagicMock(return_value=mock_streaming_response())
        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
            ) as mock_send_streaming_response,
            patch(
                "mindroom.delivery_gateway.edit_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ) as mock_edit_message,
        ):
            mock_send_streaming_response.side_effect = _consuming_send(stream_transport("$response", "chunk"))
            with patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                stream_agent_response=mock_stream_agent_response,
            ):
                delivery = await _respond(
                    bot,
                    _response_request(
                        room_id="!test:localhost",
                        prompt="Please reply in thread",
                        reply_to_event_id="$event456",
                        thread_history=[],
                        user_id="@user:localhost",
                        response_envelope=_hook_envelope(body="Please reply in thread", source_event_id="$event456"),
                        correlation_id="corr-stream",
                    ),
                    streaming=True,
                )

        assert delivery.event_id == "$response"
        assert before_calls == 0
        mock_edit_message.assert_not_awaited()
        assert after_results == [("$response", "chunk", "sent", "ai")]

    @pytest.mark.asyncio
    async def test_streamed_agent_reply_passes_active_response_event_ids(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming AI calls should receive the events of the room's replies whose span runs here."""

        async def mock_streaming_response() -> AsyncGenerator[str]:
            yield "chunk"

        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        bot.client.rooms = {}
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        live_event_ids = AsyncMock(return_value=frozenset({"$active"}))

        with patch.object(ReplyRuntime, "live_event_ids", new=live_event_ids):
            mock_stream = MagicMock(return_value=mock_streaming_response())
            with patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
            ) as mock_send_streaming_response:
                mock_send_streaming_response.side_effect = _consuming_send(stream_transport("$response", "chunk"))
                with patch_response_runner_module(
                    typing_indicator=_noop_typing_indicator,
                    stream_agent_response=mock_stream,
                ):
                    await _respond(
                        bot,
                        _response_request(
                            room_id="!test:localhost",
                            prompt="Please continue",
                            reply_to_event_id="$event456",
                            thread_history=[],
                            user_id="@user:localhost",
                            response_envelope=request_envelope(
                                room_id="!test:localhost",
                                reply_to_event_id="$event456",
                                prompt="Please continue",
                                user_id="@user:localhost",
                            ),
                        ),
                        streaming=True,
                    )

            assert mock_stream.call_args.args[0].active_event_ids == frozenset({"$active"})
        live_event_ids.assert_awaited_with("!test:localhost")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("hook_action"), ["rewrite", "suppress"])
    async def test_streamed_before_response_no_longer_mutates_post_visible_success(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        hook_action: str,
    ) -> None:
        """message:before_response must not mutate or suppress once streamed text is already visible."""

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            if hook_action == "rewrite":
                ctx.draft.response_text = "updated text"
            else:
                ctx.draft.suppress = True

        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins(
            [_hook_plugin("hooked", [before_hook])],
        )
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        bot.client.room_redact = AsyncMock()
        gateway = unwrap_extracted_collaborator(bot._delivery_gateway)
        mock_deliver_final = AsyncMock(
            return_value=FinalDeliveryOutcome(
                ending=Completed(),
                event_id="$streaming",
                is_visible_response=True,
                final_visible_body="updated text",
                delivery_kind="edited",
            ),
        )
        object.__setattr__(gateway, "deliver_final", mock_deliver_final)

        outcome = await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                stream_transport_outcome=stream_transport("$streaming", "chunk"),
                initial_delivery_kind="sent",
                identity=ResponseIdentity(
                    response_kind="ai",
                    response_envelope=response_envelope,
                    correlation_id="corr-finalize-stream-visible-failure",
                    sources=ResponseSources((response_envelope.source_event_id,), (response_envelope.source_event_id,)),
                ),
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert outcome.ending == Completed()
        assert outcome.final_visible_event_id == "$streaming"
        assert outcome.final_visible_body == "chunk"
        mock_deliver_final.assert_not_awaited()
        bot.client.room_redact.assert_not_awaited()

    def test_response_outcome_prefers_ending_over_delivery_kind(self) -> None:
        """Pipeline outcome summaries must not report cancelled or error states as plain send/edit success."""
        assert (
            outcome_label(
                FinalDeliveryOutcome(
                    ending=Cancelled("user_stop"),
                    event_id="$cancelled",
                    is_visible_response=True,
                    final_visible_body="Cancelled.",
                    delivery_kind="edited",
                ),
            )
            == "cancelled"
        )
        assert (
            outcome_label(
                FinalDeliveryOutcome(
                    ending=Failed(FailureCause.ERROR, "boom"),
                    event_id="$error",
                    is_visible_response=True,
                    final_visible_body="boom",
                ),
            )
            == "error"
        )

    @pytest.mark.asyncio
    async def test_non_streaming_hidden_tool_calls_do_not_send_tool_trace(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Hidden tool calls should not propagate structured tool metadata."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        show_tool_calls=False,
                    ),
                },
            ),
            tmp_path,
        )
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        # The placeholder is the first event the reply sends, and the answer edits it.
        unique_room_send_responses(bot.client, prefix="$response")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        read_file = ReportedCall(tool_call_id="call-1", tool_name="read_file", tool_args={"path": "README.md"})
        mock_ai = MagicMock()

        async def fake_stream_agent_response(*args: object, **kwargs: object) -> AsyncIterator[object]:
            mock_ai(*args, **kwargs)
            yield ToolStarted(read_file)
            yield ToolCompleted(replace(read_file, result="contents"))
            yield "Hidden tool call output"

        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            stream_agent_response=fake_stream_agent_response,
        ):
            delivery = await _respond(
                bot,
                _response_request(
                    room_id="!test:localhost",
                    prompt="Summarize README",
                    reply_to_event_id="$event",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        prompt="Summarize README",
                        user_id="@user:localhost",
                    ),
                ),
            )

        assert delivery.event_id == "$response1"
        assert mock_ai.call_args.args[0].show_tool_calls is False
        content = bot.client.room_send.await_args.kwargs["content"]["m.new_content"]
        assert content["body"] == "Hidden tool call output"
        assert "io.mindroom.tool_trace" not in content

    @pytest.mark.asyncio
    async def test_non_streaming_visible_tool_calls_are_passed_to_ai_response(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A reply delivered without streaming that shows its tool calls sends the trace ``ai_response`` collected."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!test:localhost"],
                        show_tool_calls=True,
                    ),
                },
            ),
            tmp_path,
        )
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        # The placeholder is the first event the reply sends, and the answer edits it.
        unique_room_send_responses(bot.client, prefix="$response")
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        shell = ReportedCall(tool_call_id="call-1", tool_name="run_shell_command", tool_args={"cmd": "git status"})

        async def fake_stream_agent_response(
            ctx: ResponseTurnContext,
            *_args: object,
            **_kwargs: object,
        ) -> AsyncIterator[object]:
            assert ctx.show_tool_calls is True
            yield ToolStarted(shell)
            yield ToolCompleted(replace(shell, result="clean"))
            yield "Final answer"

        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            stream_agent_response=fake_stream_agent_response,
        ):
            delivery = await _respond(
                bot,
                _response_request(
                    room_id="!test:localhost",
                    prompt="Check status",
                    reply_to_event_id="$event",
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        prompt="Check status",
                        user_id="@user:localhost",
                    ),
                ),
            )

        assert delivery.event_id == "$response1"
        content = bot.client.room_send.await_args.kwargs["content"]["m.new_content"]
        assert content["body"].endswith("Final answer")
        assert content[TOOL_TRACE_CONTENT_KEY]["events"] == [
            {
                "type": "tool_call_completed",
                "tool_name": "run_shell_command",
                "args_preview": "cmd=git status",
                "result_preview": "clean",
            },
        ]

    @pytest.mark.asyncio
    async def test_generate_response_passes_structured_user_turn_time(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Top-level response generation should preserve current-turn time as request data."""
        scheduled_tasks: list[asyncio.Task[None]] = []

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            return task

        config = self._config_for_storage(tmp_path)
        config.timezone = "America/Los_Angeles"
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()

        prior_user_time = datetime(2026, 3, 10, 8, 10, tzinfo=ZoneInfo("America/Los_Angeles"))
        prior_agent_time = datetime(2026, 3, 10, 8, 12, tzinfo=ZoneInfo("America/Los_Angeles"))
        current_turn_time = datetime(2026, 3, 20, 8, 15, tzinfo=ZoneInfo("America/Los_Angeles"))
        thread_history = [
            _visible_message(
                sender="@alice:localhost",
                body="Earlier user question",
                timestamp=int(prior_user_time.timestamp() * 1000),
                event_id="$user1",
            ),
            _visible_message(
                sender=mock_agent_user.user_id,
                body="Existing agent reply",
                timestamp=int(prior_agent_time.timestamp() * 1000),
                event_id="$agent1",
            ),
        ]

        answer = AsyncMock(return_value="ok")
        with (
            patch.object(LockedTurns, "_run_cancellable_response", new=attempt_in_place()),
            patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
            patch("mindroom.response_runner.stream_agent_response", new=streamed_answer(answer)),
            patch("mindroom.reply_turn.locked_turn.create_background_task", side_effect=schedule_background_task),
            patch(
                "mindroom.reply_turn.locked_turn.store_conversation_memory",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                bot._conversation_resolver,
                "fetch_thread_history",
                new=AsyncMock(return_value=thread_history_result(thread_history, is_full_history=True)),
            ),
        ):
            await bot._response_runner.generate_response(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$event",),
                        logical_source_event_ids=("$event",),
                    ),
                    prompt="What time is it?",
                    thread_history=thread_history,
                    user_id="@alice:localhost",
                    current_timestamp_ms=int(current_turn_time.timestamp() * 1000),
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        thread_id="$thread",
                        prompt="What time is it?",
                        user_id="@alice:localhost",
                        agent_name=bot.agent_name,
                    ),
                ),
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        turn = answer.call_args.args[0]
        assert turn.prompt == "What time is it?"
        assert turn.model_prompt == "What time is it?"
        assert turn.current_timestamp_ms == int(current_turn_time.timestamp() * 1000)
        assert turn.thread_history[0].body == "[2026-03-10 08:10 PDT] Earlier user question"
        assert turn.thread_history[1].body == "Existing agent reply"

    @pytest.mark.asyncio
    async def test_generate_response_keeps_memory_inputs_unprefixed(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Memory storage should receive the raw conversation, not the model-prefixed version."""
        scheduled_tasks: list[asyncio.Task[None]] = []
        stored_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        async def fake_store_conversation_memory(*args: object, **kwargs: object) -> None:
            stored_calls.append((args, kwargs))

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            return task

        config = self._config_for_storage(tmp_path)
        config.memory.backend = "mem0"
        config.timezone = "America/Los_Angeles"
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()

        bob_time = datetime(2026, 3, 10, 8, 10, tzinfo=ZoneInfo("America/Los_Angeles"))
        alice_time = datetime(2026, 3, 10, 8, 12, tzinfo=ZoneInfo("America/Los_Angeles"))
        agent_time = datetime(2026, 3, 10, 8, 14, tzinfo=ZoneInfo("America/Los_Angeles"))
        current_turn_time = datetime(2026, 3, 20, 8, 15, tzinfo=ZoneInfo("America/Los_Angeles"))
        thread_history = [
            _visible_message(
                sender="@bob:localhost",
                body="Bob question",
                timestamp=int(bob_time.timestamp() * 1000),
                event_id="$bob1",
            ),
            _visible_message(
                sender="@alice:localhost",
                body="Alice earlier",
                timestamp=int(alice_time.timestamp() * 1000),
                event_id="$alice1",
            ),
            _visible_message(
                sender=mock_agent_user.user_id,
                body="Existing agent reply",
                timestamp=int(agent_time.timestamp() * 1000),
                event_id="$agent1",
            ),
        ]

        answer = AsyncMock(return_value="ok")
        with (
            patch.object(LockedTurns, "_run_cancellable_response", new=attempt_in_place()),
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=False),
                stream_agent_response=streamed_answer(answer),
            ),
            patch("mindroom.reply_turn.locked_turn.create_background_task", side_effect=schedule_background_task),
            patch(
                "mindroom.reply_turn.locked_turn.store_conversation_memory",
                side_effect=fake_store_conversation_memory,
            ),
            patch.object(
                bot._conversation_resolver,
                "fetch_thread_history",
                new=AsyncMock(return_value=thread_history_result(thread_history, is_full_history=True)),
            ),
        ):
            await bot._response_runner.generate_response(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$event",),
                        logical_source_event_ids=("$event",),
                    ),
                    prompt="What time is it?",
                    thread_history=thread_history,
                    user_id="@alice:localhost",
                    current_timestamp_ms=int(current_turn_time.timestamp() * 1000),
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        thread_id="$thread",
                        prompt="What time is it?",
                        user_id="@alice:localhost",
                        agent_name=bot.agent_name,
                    ),
                ),
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        turn = answer.call_args.args[0]
        assert turn.prompt == "What time is it?"
        assert turn.model_prompt == "What time is it?"
        assert turn.current_timestamp_ms == int(current_turn_time.timestamp() * 1000)
        assert turn.thread_history[0].body == "[2026-03-10 08:10 PDT] Bob question"
        assert turn.thread_history[1].body == "[2026-03-10 08:12 PDT] Alice earlier"
        assert turn.thread_history[2].body == "Existing agent reply"

        assert len(stored_calls) == 1
        store_args, _ = stored_calls[0]
        assert store_args[0] == "What time is it?"
        assert store_args[5] == thread_history
        assert store_args[6] == "@alice:localhost"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("source_kind", "expects_streaming"),
        [
            (SILENT_SCHEDULE_SOURCE_KIND, False),
            (SCHEDULED_SOURCE_KIND, True),
        ],
    )
    async def test_generate_response_disables_streaming_only_for_silent_schedules(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        source_kind: str,
        expects_streaming: bool,
    ) -> None:
        """Silent scheduled turns override an enabled room without changing ordinary schedules."""

        async def run_cancellable_response(*_args: object, **kwargs: object) -> FinalDeliveryOutcome:
            response_function = cast(
                "Callable[[str | None], Awaitable[FinalDeliveryOutcome]]",
                kwargs["response_function"],
            )
            existing_event_id = cast("str | None", kwargs["existing_event_id"])
            return await response_function(existing_event_id)

        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        deliver = delivered_turn(
            FinalDeliveryOutcome(
                ending=Completed(),
                event_id="$response",
                is_visible_response=True,
                final_visible_body="Handled",
                delivery_kind="sent",
            ),
        )
        run_attempt = AsyncMock(side_effect=run_cancellable_response)

        with (
            patch.object(ResponseRunner, "_deliver_turn", new=deliver),
            patch.object(LockedTurns, "_run_cancellable_response", new=run_attempt),
            patch.object(LockedTurns, "memory_persistence", return_value=None),
            patch(
                "mindroom.delivery_gateway.DeliveryGateway.send_text",
                new=AsyncMock(return_value="$thinking"),
            ) as send_text,
            patch_response_runner_module(should_use_streaming=AsyncMock(return_value=True)),
        ):
            await bot._response_runner.generate_response(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$event",),
                        logical_source_event_ids=("$event",),
                    ),
                    prompt="Check for updates",
                    thread_history=[],
                    user_id="@alice:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        prompt="Check for updates",
                        user_id="@alice:localhost",
                        agent_name=bot.agent_name,
                        source_kind=source_kind,
                    ),
                ),
            )

        deliver.assert_awaited_once()
        assert deliver.await_args.kwargs["use_streaming"] is expects_streaming
        assert send_text.await_count == int(expects_streaming)
        assert run_attempt.await_args.kwargs["show_stop_button"] is expects_streaming

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("response_text", "expected_event_id"),
        [
            ("Finding", "$response"),
            (SILENT_SCHEDULE_NO_REPLY_TOKEN, None),
        ],
    )
    async def test_silent_schedule_agent_default_path_emits_only_final_finding(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        response_text: str,
        expected_event_id: str | None,
    ) -> None:
        """Default agent progress stays hidden, including explicit no-report acknowledgments."""
        typing_events: list[str] = []

        @asynccontextmanager
        async def record_typing(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            typing_events.append("started")
            yield

        config = self._config_for_storage(tmp_path)
        assert config.defaults.enable_streaming is True
        assert config.defaults.show_stop_button is True
        assert config.defaults.show_tool_calls is True
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        mock_ai_response = AsyncMock(return_value=response_text)

        with (
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=True),
                typing_indicator=record_typing,
                stream_agent_response=streamed_answer(mock_ai_response),
            ),
            patch.object(LockedTurns, "memory_persistence", return_value=None),
            patch(
                "mindroom.delivery_gateway.DeliveryGateway.send_text",
                new=AsyncMock(return_value="$response"),
            ) as send_text,
            patch(
                "mindroom.delivery_gateway.DeliveryGateway.edit_text",
                new=AsyncMock(return_value="$response"),
            ) as edit_text,
            patch(
                "mindroom.delivery_gateway.DeliveryGateway.add_reply_stop_button",
                new=AsyncMock(),
            ) as add_stop_button,
        ):
            fate = await bot._response_runner.generate_response(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$event",),
                        logical_source_event_ids=("$event",),
                    ),
                    prompt="Check for updates",
                    thread_history=[],
                    user_id="@alice:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        prompt="Check for updates",
                        user_id="@alice:localhost",
                        agent_name=bot.agent_name,
                        source_kind=SILENT_SCHEDULE_SOURCE_KIND,
                    ),
                    correlation_id="corr-silent-agent-defaults",
                ),
            )

        # The reply's records hold its source; the patched send shows whether it answered, and with what.
        assert fate == Owned()
        reply = await bot._reply_runtime.store.replies.for_sources(("$event",))
        assert reply is not None
        assert reply.terminal
        assert mock_ai_response.await_args.args[0].allow_no_report_response is True
        assert mock_ai_response.await_args.args[0].compaction_lifecycle is None
        assert typing_events == []
        assert send_text.await_count == int(expected_event_id is not None)
        if expected_event_id is not None:
            assert send_text.await_args.args[-1].response_text == "Finding"
        edit_text.assert_not_awaited()
        bot.client.room_redact.assert_not_awaited()
        add_stop_button.assert_not_awaited()
        bot.client.room_send.assert_not_awaited()
        receipt_path = (
            runtime_paths_for(config).storage_root
            / "agents"
            / "calculator"
            / "workspace"
            / ".mindroom"
            / "scheduled_runs"
            / "940ffb8fc9eedd9c606a71df9cf9f467e5d3c1e94d1874613e496ae5b6d8c752.json"
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["status"] == "completed"
        assert receipt["result"] == ("reported" if expected_event_id is not None else "no_report")
        assert receipt["response_text"] == response_text
        assert receipt["started_at"] <= receipt["completed_at"]

    @pytest.mark.asyncio
    async def test_silent_schedule_writes_started_receipt_before_locked_preparation(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A locked-preparation failure must leave machine-readable evidence that the silent run started."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        receipt_path = (
            runtime_paths_for(config).storage_root
            / "agents"
            / "calculator"
            / "workspace"
            / ".mindroom"
            / "scheduled_runs"
            / "940ffb8fc9eedd9c606a71df9cf9f467e5d3c1e94d1874613e496ae5b6d8c752.json"
        )
        receipt_path.parent.mkdir(parents=True)
        receipt_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_event_id": "$event",
                    "started_at": "2026-08-24T12:00:00Z",
                },
            ),
            encoding="utf-8",
        )

        with (
            patch.object(
                bot._response_runner,
                "_prepare_request_after_lock",
                AsyncMock(side_effect=RuntimeError("preparation failed")),
            ),
            pytest.raises(RuntimeError, match="preparation failed"),
        ):
            await bot._response_runner.generate_response(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$event",),
                        logical_source_event_ids=("$event",),
                    ),
                    prompt="Check for updates",
                    thread_history=[],
                    user_id="@alice:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        prompt="Check for updates",
                        user_id="@alice:localhost",
                        agent_name=bot.agent_name,
                        source_kind=SILENT_SCHEDULE_SOURCE_KIND,
                    ),
                ),
            )

        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt == {
            "agent_name": "calculator",
            "completed_at": None,
            "entity_name": "calculator",
            "prompt": "Check for updates",
            "result": None,
            "response_text": None,
            "room_id": "!test:localhost",
            "schema_version": 1,
            "source_event_id": "$event",
            "started_at": receipt["started_at"],
            "status": "started",
            "thread_id": None,
        }
        assert receipt["started_at"].endswith("Z")

    @pytest.mark.asyncio
    async def test_silent_schedule_terminal_source_does_not_write_started_receipt(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A source rejected by the locked terminal gate never claims that execution started."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()

        fate = await bot._response_runner.generate_response(
            ResponseRequest(
                sources=ResponseSources(
                    pending_event_ids=("$terminal-event",),
                    logical_source_event_ids=("$terminal-event",),
                ),
                prompt="Check for updates",
                thread_history=[],
                user_id="@alice:localhost",
                response_envelope=request_envelope(
                    room_id="!test:localhost",
                    reply_to_event_id="$terminal-event",
                    prompt="Check for updates",
                    user_id="@alice:localhost",
                    agent_name=bot.agent_name,
                    source_kind=SILENT_SCHEDULE_SOURCE_KIND,
                ),
                prepare_source_turn=AsyncMock(return_value=SourcePreparation(invalid=("$event",))),
            ),
        )

        # The source ended before any reply existed, so its caller settles it.
        assert fate == Unclaimed(
            UnclaimedReason.TERMINAL_SOURCES,
            SourcePreparation(invalid=("$event",)),
        )
        receipt_directory = (
            runtime_paths_for(config).storage_root
            / "agents"
            / "calculator"
            / "workspace"
            / ".mindroom"
            / "scheduled_runs"
        )
        assert not receipt_directory.exists()

    @pytest.mark.asyncio
    async def test_generate_response_refreshes_thread_history_after_lock(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Queued turns should replace stale pending history with a fresh post-lock snapshot."""

        def passthrough_prepare_context(
            prompt: str,
            thread_history: Sequence[ResolvedVisibleMessage],
            *,
            config: Config,
            runtime_paths: RuntimePaths,
            timezone: str,
            model_prompt: str | None = None,
        ) -> tuple[str, Sequence[ResolvedVisibleMessage], str, list[ResolvedVisibleMessage]]:
            _ = config, runtime_paths, timezone
            return prompt, thread_history, model_prompt or prompt, list(thread_history)

        stale_history = [
            _visible_message(
                sender=mock_agent_user.user_id,
                body="Thinking...",
                event_id="$stale",
                timestamp=1,
                content={"body": "Thinking...", STREAM_STATUS_KEY: STREAM_STATUS_PENDING},
            ),
        ]
        fresh_history = [
            _visible_message(
                sender=mock_agent_user.user_id,
                body="Completed",
                event_id="$stale",
                timestamp=1,
                content={"body": "Completed", STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED},
            ),
        ]

        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        unique_room_send_responses(bot.client, prefix="$response")

        answer = AsyncMock(return_value="ok")
        with (
            patch.object(LockedTurns, "_run_cancellable_response", new=attempt_in_place()),
            patch.object(
                bot._conversation_resolver,
                "fetch_thread_history",
                new=AsyncMock(return_value=ThreadHistoryResult(fresh_history, is_full_history=True)),
            ) as mock_fetch_thread_history,
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=False),
                stream_agent_response=streamed_answer(answer),
                prepare_memory_and_model_context=passthrough_prepare_context,
                apply_post_response_effects=AsyncMock(),
            ),
        ):
            async with bot._conversation_resolver.turn_lookup_scope():
                resolution = await bot._response_runner.generate_response(
                    ResponseRequest(
                        sources=ResponseSources(
                            pending_event_ids=("$event",),
                            logical_source_event_ids=("$event",),
                        ),
                        prompt="Continue",
                        thread_history=stale_history,
                        user_id="@alice:localhost",
                        response_envelope=request_envelope(
                            room_id="!test:localhost",
                            reply_to_event_id="$event",
                            thread_id="$thread",
                            prompt="Continue",
                            user_id="@alice:localhost",
                            agent_name=bot.agent_name,
                        ),
                    ),
                )

        assert resolution == Owned()
        assert await recorded_reply_event(bot, "$event") == "$response1"
        mock_fetch_thread_history.assert_awaited_once_with(
            "!test:localhost",
            "$thread",
        )
        turn = answer.call_args.args[0]
        assert list(turn.thread_history) == fresh_history
        assert turn.thread_history[0].stream_status == STREAM_STATUS_COMPLETED

    @pytest.mark.asyncio
    async def test_generate_response_timestamps_history_in_the_agents_timezone(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """An agent with its own timezone sees the thread's user messages timed in that zone, not the global one."""
        config = self._config_for_storage(tmp_path)
        config.timezone = "UTC"
        config.agents[mock_agent_user.agent_name].timezone = "America/Los_Angeles"
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        # 2026-03-20 15:00 UTC.
        history = [_visible_message(sender="@alice:localhost", body="older text", timestamp=1_774_018_800_000)]

        answer = AsyncMock(return_value="ok")
        with (
            patch.object(LockedTurns, "_run_cancellable_response", new=attempt_in_place()),
            patch.object(
                bot._conversation_resolver,
                "fetch_thread_history",
                new=AsyncMock(return_value=ThreadHistoryResult(history, is_full_history=True)),
            ),
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=False),
                stream_agent_response=streamed_answer(answer),
                apply_post_response_effects=AsyncMock(),
            ),
        ):
            async with bot._conversation_resolver.turn_lookup_scope():
                await bot._response_runner.generate_response(
                    ResponseRequest(
                        sources=ResponseSources(
                            pending_event_ids=("$event",),
                            logical_source_event_ids=("$event",),
                        ),
                        prompt="Continue",
                        thread_history=history,
                        user_id="@alice:localhost",
                        response_envelope=request_envelope(
                            room_id="!test:localhost",
                            reply_to_event_id="$event",
                            thread_id="$thread",
                            prompt="Continue",
                            user_id="@alice:localhost",
                            agent_name=bot.agent_name,
                        ),
                    ),
                )

        [older] = answer.call_args.args[0].thread_history
        assert older.body == "[2026-03-20 08:00 PDT] older text"

    @pytest.mark.asyncio
    async def test_generate_response_uses_resolved_thread_root_for_thinking_placeholder(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Thinking placeholders should use the canonical thread root from the response envelope."""
        scheduled_tasks: list[asyncio.Task[None]] = []
        sent_contents: list[dict[str, object]] = []

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            return task

        async def record_send(
            _client: object,
            _room_id: str,
            content: dict[str, object],
            **_kwargs: object,
        ) -> DeliveredMatrixEvent:
            sent_contents.append(content)
            return delivered_matrix_event("$thinking", content)

        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        envelope = MessageEnvelope(
            source_event_id="$reply_plain:localhost",
            target=MessageTarget.resolve(
                room_id="!test:localhost",
                thread_id=None,
                reply_to_event_id="$reply_plain:localhost",
                thread_start_root_event_id="$thread_root:localhost",
            ),
            body="Continue",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=mock_agent_user.agent_name,
            origin=message_origin(
                sender_id="@alice:localhost",
                requester_id="@alice:localhost",
                source_kind=MESSAGE_SOURCE_KIND,
            ),
        )

        with (
            patch.object(
                ResponseRunner,
                "_deliver_turn",
                new=delivered_turn(
                    FinalDeliveryOutcome(
                        ending=Completed(),
                        event_id="$thinking",
                        is_visible_response=True,
                        final_visible_body="ok",
                        delivery_kind="edited",
                    ),
                ),
            ),
            patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
            patch("mindroom.reply_turn.locked_turn.create_background_task", side_effect=schedule_background_task),
            patch(
                "mindroom.reply_turn.locked_turn.store_conversation_memory",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mindroom.delivery_gateway.send_message_outcome", new=AsyncMock(side_effect=record_send)),
        ):
            await bot._response_runner.generate_response(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=(envelope.source_event_id,),
                        logical_source_event_ids=(envelope.source_event_id,),
                    ),
                    prompt="Continue",
                    thread_history=[],
                    user_id="@alice:localhost",
                    response_envelope=envelope,
                    correlation_id="$request:localhost",
                ),
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        assert len(sent_contents) == 1
        content = sent_contents[0]
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$thread_root:localhost"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$reply_plain:localhost"

    @pytest.mark.asyncio
    async def test_generate_response_queues_thread_summary_for_threaded_reply(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Threaded agent replies should queue summary generation once the threshold is reached."""
        scheduled_tasks: list[asyncio.Task[None]] = []
        scheduled_names: list[str] = []

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            scheduled_names.append(name)
            return task

        config = self._config_for_storage(tmp_path)
        config.defaults.thread_summary_first_threshold = 1
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        bot._knowledge_access_support.resolve_for_agent_async = AsyncMock(
            return_value=_KnowledgeResolution(knowledge=None),
        )
        thread_history = [
            _visible_message(
                sender=f"@user{i}:localhost",
                body=f"Message {i}",
                event_id=f"$message{i}",
                timestamp=i,
            )
            for i in range(4)
        ]

        with (
            patch("mindroom.reply_turn.request.typing_indicator", _noop_typing_indicator),
            patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.response_runner.stream_agent_response",
                new=streamed_answer(AsyncMock(return_value="ok")),
            ),
            patch(
                "mindroom.delivery_gateway.send_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$response")),
            ),
            patch(
                "mindroom.delivery_gateway.edit_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ),
            patch.object(
                bot._conversation_resolver,
                "fetch_thread_history",
                new=AsyncMock(return_value=thread_history_result(thread_history, is_full_history=True)),
            ) as mock_fetch_thread_history,
            patch("mindroom.reply_turn.locked_turn.create_background_task", side_effect=schedule_background_task),
            patch("mindroom.post_response_effects.create_background_task", side_effect=schedule_background_task),
            patch(
                "mindroom.reply_turn.locked_turn.store_conversation_memory",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "mindroom.post_response_effects.maybe_generate_thread_summary",
                new_callable=AsyncMock,
            ) as mock_thread_summary,
        ):
            await bot._response_runner.generate_response(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$event",),
                        logical_source_event_ids=("$event",),
                    ),
                    prompt="Summarize this thread",
                    thread_history=thread_history,
                    user_id="@alice:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        thread_id="$thread",
                        prompt="Summarize this thread",
                        user_id="@alice:localhost",
                        agent_name=bot.agent_name,
                    ),
                ),
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        assert mock_fetch_thread_history.await_count >= 1
        assert all(
            await_args.args == ("!test:localhost", "$thread")
            for await_args in mock_fetch_thread_history.await_args_list
        )
        mock_thread_summary.assert_awaited_once_with(
            client=bot.client,
            room_id="!test:localhost",
            thread_id="$thread",
            config=config,
            runtime_paths=bot.runtime_paths,
            conversation_reader=bot._conversation_reader,
            # The answer that queued this pass, which the projection cannot
            # hold until sync echoes it back.
            delivered_response=DeliveredResponse(event_id="$response", body="ok"),
            entity_name=bot.agent_name,
            membership_index=bot._runtime_view.agent_reply_memberships,
        )
        assert "thread_summary_!test:localhost_$thread" in scheduled_names

    @pytest.mark.asyncio
    async def test_generate_response_keeps_first_turn_follow_up_effects_in_new_thread(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """First-turn threaded replies should keep compaction notices and summaries in the resolved thread."""

        async def fake_ai_response(ctx: ResponseTurnContext, *_args: object, **_kwargs: object) -> str:
            await ctx.compaction_lifecycle.start(
                CompactionLifecycleStart(
                    mode="auto",
                    session_id="session-1",
                    scope="agent:test_agent",
                    summary_model="summary-model",
                    before_tokens=30_000,
                    history_budget_tokens=100_000,
                    runs_before=20,
                ),
            )
            return "ok"

        scheduled_tasks: list[asyncio.Task[None]] = []
        scheduled_names: list[str] = []

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            scheduled_names.append(name)
            return task

        config = self._config_for_storage(tmp_path)
        config.defaults.thread_summary_first_threshold = 1
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        bot._knowledge_access_support.resolve_for_agent_async = AsyncMock(
            return_value=_KnowledgeResolution(knowledge=None),
        )
        root_event_id = "$root_event"
        resolved_target = MessageTarget.resolve(
            room_id="!test:localhost",
            thread_id=None,
            reply_to_event_id=root_event_id,
        ).with_thread_root(root_event_id)
        scope = HistoryScope(kind="agent", scope_id=bot.agent_name)
        storage = bot._conversation_state_writer.create_storage(None, scope=scope)
        session = ConversationSession(session_id=resolved_target.session_id, kind="agent", owner_id=bot.agent_name)
        session.metadata = with_force_compaction(session.metadata)
        seed_session(storage, session)
        response_envelope = replace(_hook_envelope(source_event_id=root_event_id), target=resolved_target)

        with (
            patch("mindroom.reply_turn.request.typing_indicator", _noop_typing_indicator),
            patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.response_runner.stream_agent_response",
                new=streamed_answer(AsyncMock(side_effect=fake_ai_response)),
            ),
            patch(
                "mindroom.delivery_gateway.send_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$response")),
            ),
            patch(
                "mindroom.delivery_gateway.edit_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ),
            patch("mindroom.reply_turn.locked_turn.create_background_task", side_effect=schedule_background_task),
            patch("mindroom.post_response_effects.create_background_task", side_effect=schedule_background_task),
            patch(
                "mindroom.reply_turn.locked_turn.store_conversation_memory",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "mindroom.post_response_effects.maybe_generate_thread_summary",
                new_callable=AsyncMock,
            ) as mock_thread_summary,
            patch(
                "mindroom.delivery_gateway.DeliveryGateway._send_compaction_lifecycle_start",
                new=AsyncMock(return_value="$notice"),
            ) as mock_send_compaction_lifecycle_start,
        ):
            await bot._response_runner.generate_response(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=(response_envelope.source_event_id,),
                        logical_source_event_ids=(response_envelope.source_event_id,),
                    ),
                    prompt="Start a thread here",
                    thread_history=[],
                    user_id="@alice:localhost",
                    response_envelope=response_envelope,
                ),
            )

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        mock_thread_summary.assert_awaited_once_with(
            client=bot.client,
            room_id="!test:localhost",
            thread_id=root_event_id,
            config=config,
            runtime_paths=bot.runtime_paths,
            conversation_reader=bot._conversation_reader,
            delivered_response=DeliveredResponse(event_id="$response", body="ok"),
            entity_name=bot.agent_name,
            membership_index=bot._runtime_view.agent_reply_memberships,
        )
        mock_send_compaction_lifecycle_start.assert_awaited_once()
        compaction_notice_kwargs = mock_send_compaction_lifecycle_start.await_args.kwargs
        assert compaction_notice_kwargs["target"].resolved_thread_id == root_event_id
        assert compaction_notice_kwargs["reply_to_event_id"] == root_event_id
        assert "thread_summary_!test:localhost_$root_event" in scheduled_names

    @pytest.mark.asyncio
    async def test_generate_response_marks_non_streaming_model_error_unsuccessful_for_post_effects(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A delivered non-streaming Matrix error reply should not be a successful run outcome."""
        captured_outcomes: list[ResponseOutcome] = []

        async def fake_apply_post_response_effects(
            _delivery: FinalDeliveryOutcome,
            outcome: ResponseOutcome,
            _deps: object,
        ) -> None:
            captured_outcomes.append(outcome)

        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        install_direct_response_admission(bot)
        install_runtime_journal_support(bot)
        # The answer is delivered through the outbox, which needs the bot in the room.
        await admit_room_membership(bot.journal_principal(), "!test:localhost", "join")
        unique_room_send_responses(bot.client)

        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            should_use_streaming=AsyncMock(return_value=False),
            stream_agent_response=failed_answer("friendly-error"),
            apply_post_response_effects=AsyncMock(side_effect=fake_apply_post_response_effects),
        ):
            await bot._response_runner.generate_response(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$event",),
                        logical_source_event_ids=("$event",),
                    ),
                    prompt="Please answer",
                    thread_history=[],
                    user_id="@alice:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        prompt="Please answer",
                        user_id="@alice:localhost",
                        agent_name=bot.agent_name,
                    ),
                ),
            )

        assert bot.client.room_send.await_args.kwargs["content"]["m.new_content"]["body"] == "friendly-error"
        assert [outcome.run_succeeded for outcome in captured_outcomes] == [False]

    @pytest.mark.asyncio
    async def test_generate_response_marks_streaming_model_error_unsuccessful_for_post_effects(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A delivered streaming Matrix error reply should not be a successful run outcome."""
        captured_outcomes: list[ResponseOutcome] = []
        consumed: list[object] = []

        async def fake_apply_post_response_effects(
            _delivery: FinalDeliveryOutcome,
            outcome: ResponseOutcome,
            _deps: object,
        ) -> None:
            captured_outcomes.append(outcome)

        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))

        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new=AsyncMock(side_effect=_consuming_send(stream_transport("$response", "friendly-error"), consumed)),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=True),
                stream_agent_response=failed_answer("friendly-error"),
                apply_post_response_effects=AsyncMock(side_effect=fake_apply_post_response_effects),
            ),
        ):
            await bot._response_runner.generate_response(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$event",),
                        logical_source_event_ids=("$event",),
                    ),
                    prompt="Please answer",
                    thread_history=[],
                    user_id="@alice:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        prompt="Please answer",
                        user_id="@alice:localhost",
                        agent_name=bot.agent_name,
                    ),
                ),
            )

        assert [chunk.text for chunk in consumed if isinstance(chunk, FailedReply)] == ["friendly-error"]
        assert [outcome.run_succeeded for outcome in captured_outcomes] == [False]

    @pytest.mark.asyncio
    async def test_generate_response_runs_post_effects_after_cancellable_wrapper(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Late cancellation should not skip agent post-response cleanup after delivery."""
        started = asyncio.Event()
        release = asyncio.Event()
        effects: list[FinalDeliveryOutcome] = []

        async def fake_post_effects(delivery: FinalDeliveryOutcome, *_args: object, **_kwargs: object) -> None:
            effects.append(delivery)
            started.set()
            await release.wait()

        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()

        with (
            patch.object(
                ResponseRunner,
                "_deliver_turn",
                new=delivered_turn(
                    FinalDeliveryOutcome(
                        ending=Completed(),
                        event_id="$response",
                        is_visible_response=True,
                        final_visible_body="ok",
                    ),
                ),
            ),
            patch.object(LockedTurns, "_run_cancellable_response", new=attempt_in_place()),
            patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.response_lifecycle.apply_post_response_effects",
                new=AsyncMock(side_effect=fake_post_effects),
            ),
        ):
            task = asyncio.create_task(
                bot._response_runner.generate_response(
                    ResponseRequest(
                        sources=ResponseSources(
                            pending_event_ids=("$event",),
                            logical_source_event_ids=("$event",),
                        ),
                        prompt="Summarize this thread",
                        thread_history=[],
                        user_id="@alice:localhost",
                        response_envelope=request_envelope(
                            room_id="!test:localhost",
                            reply_to_event_id="$event",
                            thread_id="$thread",
                            prompt="Summarize this thread",
                            user_id="@alice:localhost",
                            agent_name=bot.agent_name,
                        ),
                    ),
                ),
            )
            await started.wait()
            task.cancel()
            release.set()
            resolution = await task

        assert resolution == Owned()
        # The cleanup ran to the end for the delivered answer.
        [delivery] = effects
        assert delivery.ending == Completed()
        assert delivery.final_visible_event_id == "$response"

    @pytest.mark.asyncio
    async def test_a_suppressed_reply_settles_through_its_records(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A suppressed reply settles its source through its own records, with no second settlement."""
        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        events: list[str] = []

        request = ResponseRequest(
            sources=ResponseSources(
                pending_event_ids=("$event",),
                logical_source_event_ids=("$event",),
            ),
            prompt="Check for updates",
            thread_history=[],
            user_id="@alice:localhost",
            response_envelope=request_envelope(
                room_id="!test:localhost",
                reply_to_event_id="$event",
                prompt="Check for updates",
                user_id="@alice:localhost",
                agent_name=bot.agent_name,
                source_kind=SILENT_SCHEDULE_SOURCE_KIND,
            ),
        )
        progress = TurnProgress()
        suppressed = FinalDeliveryOutcome(ending=Suppressed(Suppression.HOOK))

        async def generate(_message_id: str | None) -> FinalDeliveryOutcome:
            return suppressed

        runner = unwrap_extracted_collaborator(bot._response_runner)
        identity = runner.locked.build_lifecycle(request, response_kind="ai").identity

        async def finalize(outcome: FinalDeliveryOutcome, **_kwargs: object) -> FinalDeliveryOutcome:
            events.append("cleanup_complete")
            return outcome

        lifecycle = cast("Any", SimpleNamespace(identity=identity, finalize=finalize))
        # The response runs in the reply span the locked generation claims for it.
        async with runner.deps.replies.span_scope():
            claimed = await runner._claim_reply_span(request, history_scope=runner.deps.state_writer.history_scope())
            assert isinstance(claimed, ResponseRequest)
            await runner.locked.run_and_settle_locked_response(
                claimed,
                target=claimed.response_envelope.target,
                lifecycle=lifecycle,
                progress=progress,
                response_function=generate,
                build_post_response_outcome=lambda _outcome: cast("Any", SimpleNamespace()),
                post_response_deps=lambda: cast("Any", SimpleNamespace()),
            )

        # The suppressed reply's records settled its source as its span ended; no second settlement follows.
        assert events == ["cleanup_complete"]
        assert not await runner.deps.replies.store.is_pending("$event")

    @pytest.mark.asyncio
    async def test_paused_approval_releases_conversation_for_the_next_turn(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A persisted approval pause must finish the live lifecycle instead of holding its lock."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        target = request_envelope(
            room_id="!test:localhost",
            reply_to_event_id="$event",
            thread_id="$thread",
            prompt="Run it",
            user_id="@alice:localhost",
            agent_name=bot.agent_name,
        )
        request = ResponseRequest(
            sources=ResponseSources(
                pending_event_ids=(target.source_event_id,),
                logical_source_event_ids=(target.source_event_id,),
            ),
            prompt="Run it",
            thread_history=[],
            user_id="@alice:localhost",
            response_envelope=target,
        )
        pause = ResponsePausedForApproval(
            PausedAttempt(
                session_id=target.target.session_id,
                run_id="run-1",
                requirements=(
                    RunRequirement(ToolExecution(tool_call_id="call-1", tool_name="dangerous", tool_args={})),
                ),
                toolkit_owners={("general", "dangerous"): "test_toolkit"},
            ),
        )
        waiting = FinalDeliveryOutcome(
            ending=AwaitingApproval(),
            event_id="$waiting",
            is_visible_response=True,
            final_visible_body="Waiting for approval",
        )

        with (
            patch.object(LockedTurns, "_run_cancellable_response", new=AsyncMock(side_effect=pause)),
            patch.object(
                ApprovalContinuationRunner,
                "suspend_for_approval",
                new=AsyncMock(return_value=waiting),
            ) as suspend,
            patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
            patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock()) as effects,
        ):
            # The paused approval owns the source.
            assert await bot._response_runner.generate_response(request) == Owned()
            assert not bot._response_runner.has_active_response_for_target(target.target)

        suspend.assert_awaited_once()
        effects.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_pause_whose_approval_cannot_be_created_ends_with_the_approval_start_note(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A pause the approval runner fails to take ends the reply with the approval-start note, never its error."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        target = request_envelope(
            room_id="!test:localhost",
            reply_to_event_id="$event",
            thread_id="$thread",
            prompt="Run it",
            user_id="@alice:localhost",
            agent_name=bot.agent_name,
        )
        request = ResponseRequest(
            sources=ResponseSources(
                pending_event_ids=(target.source_event_id,),
                logical_source_event_ids=(target.source_event_id,),
            ),
            prompt="Run it",
            thread_history=[],
            user_id="@alice:localhost",
            response_envelope=target,
        )
        pause = ResponsePausedForApproval(
            PausedAttempt(
                session_id=target.target.session_id,
                run_id="run-1",
                requirements=(
                    RunRequirement(ToolExecution(tool_call_id="call-1", tool_name="dangerous", tool_args={})),
                ),
                toolkit_owners={("general", "dangerous"): "test_toolkit"},
            ),
        )
        sent: list[str] = []

        async def send(_client: object, _room_id: str, content: dict[str, Any], **_kwargs: object) -> object:
            sent.append(str(content.get("m.new_content", content)["body"]))
            return DeliveredMatrixEvent(f"$sent-{len(sent)}", content)

        with (
            patch.object(LockedTurns, "_run_cancellable_response", new=AsyncMock(side_effect=pause)),
            patch.object(
                ApprovalContinuationRunner,
                "suspend_for_approval",
                new=AsyncMock(side_effect=RuntimeError("card store unavailable")),
            ),
            patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
            patch("mindroom.delivery_gateway.send_message_outcome", new=AsyncMock(side_effect=send)),
        ):
            assert await bot._response_runner.generate_response(request) == Owned()

        assert sent[-1].endswith("Tool approval could not be started. Please try again.")
        assert all("card store unavailable" not in body for body in sent)
        reply = await bot._reply_runtime.store.replies.for_sources((target.source_event_id,))
        assert reply is not None
        assert reply.state is rl.ReplyState.FAILED


class TestAdaptiveResponse(AgentBotTestBase):
    """Exercise quiet participation through the real agent and Matrix lifecycle."""

    @staticmethod
    def _participation_request(
        bot: AgentBot,
        *,
        reaction: str | None,
        backend: str,
    ) -> ResponseRequest:
        """Return one reply request whose participation the model or a judgment backend decides."""
        return ResponseRequest(
            prompt="Any thoughts?",
            sources=ResponseSources(
                pending_event_ids=("$event", "$earlier"),
                logical_source_event_ids=("$earlier", "$event"),
            ),
            thread_history=[],
            user_id="@alice:localhost",
            response_envelope=request_envelope(
                room_id="!test:localhost",
                reply_to_event_id="$event",
                thread_id="$thread",
                prompt="Any thoughts?",
                user_id="@alice:localhost",
                agent_name=bot.agent_name,
            ),
            participation=ParticipationConfig.model_validate(
                {
                    "decline_reaction": reaction,
                    "judgment": (
                        {"provider": "typesafe"} if backend == "typesafe" else {"provider": "llm", "model": "judge"}
                    )
                    if backend != "model"
                    else None,
                },
            ),
        )

    @staticmethod
    def _participation_bot(
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        *,
        process_env: dict[str, str] | None = None,
    ) -> AgentBot:
        """Build the agent's bot for an online requester, with unique event IDs from its mocked homeserver."""
        config = AgentBotTestBase._config_for_storage(tmp_path)
        config.memory.backend = "none"
        # The llm judgment backend decides on its own model.
        config.models["judge"] = config.models["default"]
        paths = replace(runtime_paths_for(config), process_env=process_env or {})
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=paths)
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        unique_room_send_responses(bot.client, prefix="$response")
        # The requester is online, so a running reply shows its Stop button.
        presence = MagicMock(spec=nio.PresenceGetResponse)
        presence.presence = "online"
        presence.last_active_ago = 0
        bot.client.get_presence = AsyncMock(return_value=presence)
        _set_knowledge_for_agent(bot, MagicMock(return_value=None))
        return bot

    @pytest.mark.asyncio
    @pytest.mark.parametrize("streaming", [False, True])
    @pytest.mark.parametrize("action", ["respond", "stay_silent", "sync_restart", "decision_failure"])
    @pytest.mark.parametrize("backend", ["model", "typesafe", "llm"])
    @pytest.mark.parametrize("reaction", [None, "👍"])
    async def test_participation_precedes_every_visible_effect(  # noqa: C901, PLR0912, PLR0915 - full lifecycle assertions
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        streaming: bool,
        action: str,
        backend: str,
        reaction: str | None,
    ) -> None:
        """A silent decision must not send placeholders, typing, retry notices, or a Stop button."""
        bot = self._participation_bot(mock_agent_user, tmp_path, process_env={"TYPESAFE_API_KEY": "test-key"})

        async def fail() -> None:
            if action == "sync_restart":
                raise asyncio.CancelledError(SYNC_RESTART_CANCEL_MSG)
            msg = "Decision provider unavailable"
            raise RuntimeError(msg)

        model_decides = backend == "model" or action == "decision_failure"
        answer_turn: list[ScriptStep] = list(answer("Useful answer"))
        if streaming and action == "respond":
            # Hold the answer until the approved reply's placeholder reached the room.
            placeholder_sent = asyncio.Event()
            unique_room_send = bot.client.room_send.side_effect

            async def room_send(**kwargs: Any) -> MagicMock:  # noqa: ANN401
                if kwargs["content"].get("body") == "Thinking...":
                    placeholder_sent.set()
                return unique_room_send(**kwargs)

            bot.client.room_send.side_effect = room_send

            async def after_placeholder() -> None:
                await asyncio.wait_for(placeholder_sent.wait(), timeout=5)

            answer_turn.insert(0, Await(after_placeholder))
        turns: list[list[ScriptStep]] = []
        if model_decides:
            turns.append(
                [Await(fail)]
                if action in {"sync_restart", "decision_failure"}
                else answer(json.dumps({"action": action, "reason": "Conversation context."})),
            )
        if action == "respond":
            turns.append(answer_turn)
        expected_model_requests = len(turns)
        model = ScriptedModel(turns)
        install_model_providers(monkeypatch, {"default": model})
        typesafe_calls: list[bytes] = []
        judge = ScriptedModel(
            [
                [Await(fail)]
                if action in {"sync_restart", "decision_failure"}
                else answer(json.dumps({"decision": action == "respond"})),
            ],
        )
        if backend == "typesafe":

            async def post(_self: JudgmentClient, body: bytes) -> bytes:
                typesafe_calls.append(body)
                if action in {"sync_restart", "decision_failure"}:
                    await fail()
                return json.dumps(
                    {
                        "model": _PINNED_MODEL,
                        "answers": {"participation": {"type": "noul", "noul": 0.95 if action == "respond" else 0.05}},
                        "usage": {"input_tokens": 100, "output_tokens": 1},
                    },
                ).encode()

            monkeypatch.setattr(JudgmentClient, "_post", post)
        elif backend == "llm":
            install_model_providers(monkeypatch, {"judge": judge})
        monkeypatch.setattr("mindroom.response_runner.should_use_streaming", AsyncMock(return_value=streaming))
        request = self._participation_request(bot, reaction=reaction, backend=backend)
        try:
            result = await bot._response_runner.generate_response(request)
        except asyncio.CancelledError:
            if action != "sync_restart":
                raise
            result = None
        assert await wait_for_background_tasks(timeout=5, owner=bot._runtime_view)
        sends = bot.client.room_send.await_args_list
        bodies = [call.kwargs["content"].get("body", "") for call in sends]
        stop_buttons = [
            (f"$response{index}", call.kwargs["content"]["m.relates_to"]["event_id"])
            for index, call in enumerate(sends, start=1)
            if call.kwargs["message_type"] == "m.reaction" and call.kwargs["content"]["m.relates_to"]["key"] == "🛑"
        ]
        reactions = [
            call.kwargs
            for call in sends
            if call.kwargs["message_type"] == "m.reaction" and call.kwargs["content"]["m.relates_to"]["key"] != "🛑"
        ]
        # The approved reply's first streamed event carries its Stop button until the reply ends. A blocking
        # answer is the reply's first event and ends the reply at once, leaving nothing to stop.
        streamed_answer = action == "respond" and streaming
        assert [target for _button, target in stop_buttons] == (["$response1"] if streamed_answer else [])
        assert [call.args[1] for call in bot.client.room_redact.await_args_list] == [
            button for button, _target in stop_buttons
        ]
        if action == "stay_silent" and reaction is not None:
            assert len(reactions) == 1
            assert reactions[0]["room_id"] == "!test:localhost"
            assert reactions[0]["content"] == {
                "m.relates_to": {"rel_type": "m.annotation", "event_id": "$event", "key": reaction},
            }
            assert reactions[0]["tx_id"]
        else:
            assert reactions == []
        bodies = [body for body in bodies if body]
        assert all('"action"' not in body for body in bodies)
        # Only an approved streamed reply shows its placeholder, and only before its answer.
        if streaming and action == "respond":
            assert bodies[0] == "Thinking..."
            bodies = bodies[1:]
        assert all("Thinking" not in body for body in bodies)
        reply = await bot._reply_runtime.store.replies.for_sources(("$event",))
        if action != "respond":
            assert bodies == []
            assert bot.client.room_typing.await_count == 0
            if action != "sync_restart":
                # The declined reply's records settled its sources.
                assert reply is not None
                assert reply.terminal
        else:
            # The approved reply's first event answers the turn.
            assert reply is not None
            assert reply.event_id == "$response1"
            assert any("Useful answer" in body for body in bodies)
        # Every turn opened its reply span, whose records hold its sources; none leaves them to its caller.
        if action != "sync_restart":
            assert result == Owned()
        assert len(model.requests) == expected_model_requests
        assert len(typesafe_calls) == (1 if backend == "typesafe" else 0)
        assert len(judge.requests) == (1 if backend == "llm" else 0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["respond", "stay_silent"])
    async def test_only_an_approved_reply_runs_its_post_response_effects(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        action: str,
    ) -> None:
        """A declined reply never queues memory or a thread summary; an approved one does both."""
        bot = self._participation_bot(mock_agent_user, tmp_path)
        turns = [answer(json.dumps({"action": action, "reason": "Conversation context."}))]
        if action == "respond":
            turns.append(answer("Useful answer"))
        install_model_providers(monkeypatch, {"default": ScriptedModel(turns)})
        thread_summary = AsyncMock()
        monkeypatch.setattr("mindroom.post_response_effects.maybe_generate_thread_summary", thread_summary)
        memory_queued: list[str] = []
        monkeypatch.setattr(
            LockedTurns,
            "memory_persistence",
            lambda *_args, **_kwargs: lambda: memory_queued.append("stored"),
        )

        await bot._response_runner.generate_response(self._participation_request(bot, reaction=None, backend="model"))

        assert await wait_for_background_tasks(timeout=5, owner=bot._runtime_view)
        assert thread_summary.await_count == (1 if action == "respond" else 0)
        assert memory_queued == (["stored"] if action == "respond" else [])

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("failure_stage", "streaming"),
        [(stage, streaming) for stage in ("history", "payload", "runtime", "knowledge") for streaming in (False, True)],
    )
    async def test_adaptive_setup_failure_settles_quietly(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure_stage: str,
        streaming: bool,
    ) -> None:
        """Early owned preparation failures settle the source without a visible error."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        runner = bot._response_runner
        failure = AsyncMock(side_effect=RuntimeError("Preparation unavailable"))
        if failure_stage == "history":
            monkeypatch.setattr(runner.deps.resolver, "fetch_thread_history", failure)
        elif failure_stage == "payload":
            monkeypatch.setattr(ResponsePayloadPreparer, "prepare", failure)
        elif failure_stage == "runtime":
            monkeypatch.setattr(ResponseRunner, "prepare_response_runtime", failure)
        else:
            monkeypatch.setattr(runner.deps.knowledge_access, "resolve_for_agent_async", failure)
        monkeypatch.setattr("mindroom.response_runner.should_use_streaming", AsyncMock(return_value=streaming))
        envelope = request_envelope(
            room_id="!test:localhost",
            reply_to_event_id="$event",
            thread_id="$thread",
            agent_name=bot.agent_name,
        )
        preparation = ResponsePayloadPreparation(
            dispatch=PreparedDispatch(
                requester_user_id="@alice:localhost",
                context=MessageContext(
                    am_i_mentioned=False,
                    is_thread=True,
                    thread_id="$thread",
                    thread_history=[],
                    mentioned_agents=[],
                    has_non_agent_mentions=False,
                ),
                target=envelope.target,
                correlation_id="$event",
                envelope=envelope,
            ),
            prompt="Any thoughts?",
            action_kind="individual",
            payload_inputs=DispatchPayloadInputs((), (), ()),
            target_member_names=None,
            dispatch_started_at=0.0,
            context_ready_monotonic=0.0,
        )
        result = await runner.generate_response(
            ResponseRequest(
                prompt="Any thoughts?",
                sources=ResponseSources(pending_event_ids=("$event",), logical_source_event_ids=("$event",)),
                thread_history=[],
                response_envelope=envelope,
                participation=ParticipationConfig(),
                requires_model_history_refresh=True,
                payload_preparation=preparation if failure_stage == "payload" else None,
            ),
        )
        # The declined reply's records settled its sources; its caller has nothing to settle.
        assert result == Owned()
        reply = await bot._reply_runtime.store.replies.for_sources(("$event",))
        assert reply is not None
        assert reply.terminal
        assert bot.client.room_send.await_count == 0
        assert bot.client.room_typing.await_count == 0
