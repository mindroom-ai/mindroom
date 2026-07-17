"""Text-turn dispatch pipeline through the TurnController seam: history hydration, execute-dispatch, and failure paths."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import nio
import pytest

from mindroom.bot import AgentBot, TeamBot
from mindroom.coalescing import ReadyPendingEvent
from mindroom.coalescing_batch import CoalescingKey, PendingEvent
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import (
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    SOURCE_KIND_KEY,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
    RuntimePaths,
)
from mindroom.conversation_resolver import MessageContext
from mindroom.delivery_gateway import (
    DeliveryGateway,
    FinalDeliveryRequest,
    ResponseIdentity,
    SendTextRequest,
)
from mindroom.dispatch_handoff import PreparedTextEvent
from mindroom.dispatch_source import (
    EXTERNAL_TRIGGER_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    VOICE_SOURCE_KIND,
)
from mindroom.final_delivery import FinalDeliveryOutcome
from mindroom.handled_turns import TurnRecord
from mindroom.hooks import (
    MessageEnvelope,
)
from mindroom.inbound_turn_normalizer import DispatchPayload
from mindroom.matrix.cache import ThreadHistoryResult
from mindroom.matrix.cache.thread_history_result import thread_history_result
from mindroom.matrix.client import ResolvedVisibleMessage
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.response_payload_preparation import DispatchPayloadInputs, ResponsePayloadPreparer
from mindroom.response_runner import (
    PostLockRequestPreparationError,
    ResponseRequest,
    ResponseRunner,
    _ResponseGenerationOutcome,
)
from mindroom.teams import TeamIntent, TeamMode, TeamResolution
from mindroom.turn_controller import _IngressAdmissionOutcome, _PrecheckedEvent
from mindroom.turn_policy import PreparedDispatch, ResponseAction, _DispatchPlan
from tests.bot_helpers import (
    AgentBotTestBase,
    _agent_response_handled_turn,
    _handled_response_event_id,
    _hook_envelope,
    _install_runtime_cache_support,
    _make_matrix_client_mock,
    _matrix_room,
    _replace_turn_policy_deps,
    _room_audio_event,
    _room_image_event,
    _runtime_bound_config,
    _set_turn_store_tracker,
    _visible_message,
    _visible_response_event_id,
    _wrap_extracted_collaborators,
    make_mock_agent_user,
)
from tests.conftest import (
    TEST_PASSWORD,
    drain_coalescing,
    install_edit_message_mock,
    install_generate_response_mock,
    install_runtime_cache_support,
    install_send_response_mock,
    message_origin,
    patch_response_runner_module,
    prepared_dispatch_result,
    replace_delivery_gateway_deps,
    replace_turn_controller_deps,
    runtime_paths_for,
    wrap_extracted_collaborators,
)
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


@pytest.fixture
def mock_agent_user() -> AgentMatrixUser:
    """Mock agent user for testing."""
    return make_mock_agent_user()


class TestAgentBot(AgentBotTestBase):
    """Bot behavior tests moved verbatim from tests/test_multi_agent_bot.py."""

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_sends_visible_rejection_for_unsupported_team_request(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Rejected team requests should send one actionable reply instead of silently skipping."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        tracker = _set_turn_store_tracker(bot, MagicMock())
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=True,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[bot.matrix_id],
                has_non_agent_mentions=False,
            ),
            target=(
                dispatch_target := MessageTarget.resolve(
                    room_id=room.room_id,
                    thread_id=None,
                    reply_to_event_id=event.event_id,
                    thread_start_root_event_id=event.event_id,
                )
            ),
            correlation_id="$event",
            envelope=_hook_envelope(body="help me", source_event_id="$event", target=dispatch_target),
        )
        action = ResponseAction(
            kind="reject",
            rejection_message="Team request includes private agent 'mind'; private agents are only supported in explicit Matrix ad hoc teams with requester identity",
        )

        bot.client = AsyncMock(spec=nio.AsyncClient)

        with patch.object(DeliveryGateway, "send_text", new=AsyncMock(return_value="$reply")) as send_text:
            await bot._turn_controller._execute_response_action(
                room,
                event,
                dispatch,
                action,
                DispatchPayloadInputs((), (), ()),
                processing_log="processing",
                dispatch_started_at=0.0,
                handled_turn=TurnRecord.create([event.event_id]),
            )

        send_text.assert_awaited_once()
        delivered_request = send_text.await_args.args[0]
        assert delivered_request.response_text.endswith(
            "private agents are only supported in explicit Matrix ad hoc teams with requester identity",
        )
        tracker.record_handled_turn.assert_called_once_with(
            TurnRecord.create(
                ["$event"],
                response_event_id="$reply",
            ),
        )

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_does_not_mark_reject_handled_when_rejection_send_fails(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Reject actions must not mark the source handled when no rejection reply was delivered."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        tracker = _set_turn_store_tracker(bot, MagicMock())
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=True,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[bot.matrix_id],
                has_non_agent_mentions=False,
            ),
            target=(
                dispatch_target := MessageTarget.resolve(
                    room_id=room.room_id,
                    thread_id=None,
                    reply_to_event_id=event.event_id,
                )
            ),
            correlation_id="$event",
            envelope=_hook_envelope(body="help me", source_event_id="$event", target=dispatch_target),
        )
        action = ResponseAction(
            kind="reject",
            rejection_message="Rejected request",
        )
        bot.client = AsyncMock(spec=nio.AsyncClient)

        with patch("mindroom.delivery_gateway.send_message_result", new=AsyncMock(return_value=None)):
            await bot._turn_controller._execute_response_action(
                room,
                event,
                dispatch,
                action,
                DispatchPayloadInputs((), (), ()),
                processing_log="processing",
                dispatch_started_at=0.0,
                handled_turn=TurnRecord.create([event.event_id]),
            )

        tracker.record_handled_turn.assert_called_once_with(
            TurnRecord.create([event.event_id]),
        )

    @pytest.mark.asyncio
    async def test_extract_dispatch_context_uses_bounded_full_thread_history(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Dispatch startup should use the bounded full-history read."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Follow up",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
                },
                "event_id": "$event",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )
        history = ThreadHistoryResult(
            [
                ResolvedVisibleMessage.synthetic(
                    sender="@user:localhost",
                    body="Root",
                    event_id="$thread_root",
                    timestamp=1234567889,
                    content={"body": "Root"},
                ),
            ],
            is_full_history=True,
        )

        mock_advisory_history = AsyncMock()
        mock_dispatch_history = AsyncMock(return_value=history)

        with (
            patch.object(bot._conversation_cache, "get_dispatch_thread_history", new=mock_dispatch_history),
            patch.object(bot._conversation_cache, "get_thread_history", new=mock_advisory_history),
        ):
            context_result = await bot._conversation_resolver.extract_dispatch_context(room, event)
            context = context_result.context

        assert context.is_thread is True
        assert context.thread_id == "$thread_root"
        assert [message.event_id for message in context.thread_history] == ["$thread_root"]
        assert context.requires_model_history_refresh is False
        mock_dispatch_history.assert_awaited_once_with(
            room.room_id,
            "$thread_root",
            caller_label="dispatch_context",
        )
        mock_advisory_history.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_extract_dispatch_context_fetches_direct_thread_history_through_dispatch_fetcher(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Direct-thread dispatch context should read bounded full history through the dispatch fetcher."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        install_runtime_cache_support(bot)
        bot.client = AsyncMock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Follow up",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
                },
                "event_id": "$event",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )
        dispatch_history = ThreadHistoryResult(
            [
                ResolvedVisibleMessage.synthetic(
                    sender="@user:localhost",
                    body="Root",
                    event_id="$thread_root",
                    timestamp=1234567889,
                    content={"body": "Root"},
                ),
                ResolvedVisibleMessage.synthetic(
                    sender="@mindroom_calculator:localhost",
                    body="Reply",
                    event_id="$reply",
                    timestamp=1234567890,
                    content={"body": "Reply"},
                ),
            ],
            is_full_history=True,
        )

        with patch(
            "mindroom.matrix.conversation_cache.fetch_dispatch_thread_history",
            new=AsyncMock(return_value=dispatch_history),
        ) as mock_history:
            context_result = await bot._conversation_resolver.extract_dispatch_context(room, event)
            context = context_result.context

        assert context.is_thread is True
        assert context.thread_id == "$thread_root"
        assert context.thread_history == dispatch_history
        assert context.requires_model_history_refresh is False
        trusted_sender_ids = frozenset(
            matrix_id.full_id for matrix_id in entity_ids(config, runtime_paths_for(config)).values()
        )
        mock_history.assert_awaited_once_with(
            bot.client,
            room.room_id,
            "$thread_root",
            event_cache=bot.event_cache,
            cache_write_guard_started_at=ANY,
            trusted_sender_ids=trusted_sender_ids,
            caller_label="dispatch_context",
            coordinator_queue_wait_ms=ANY,
        )

    @pytest.mark.asyncio
    async def test_dispatch_text_message_prepares_full_history_payload_after_lock_when_required(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Planning should hide partial history while payload preparation refreshes it."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$event"
        event.sender = "@user:localhost"
        event.body = "hello"
        event.server_timestamp = 1234567890
        event.source = {"content": {"body": "hello"}}

        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=False,
                is_thread=True,
                thread_id="$thread_root",
                thread_history=[
                    ResolvedVisibleMessage.synthetic(
                        sender="@user:localhost",
                        body="Snapshot root",
                        event_id="$thread_root",
                        timestamp=1,
                        content={"body": "Snapshot root"},
                    ),
                ],
                mentioned_agents=[],
                has_non_agent_mentions=False,
                requires_model_history_refresh=True,
            ),
            target=(
                dispatch_target := MessageTarget.resolve(
                    room_id=room.room_id,
                    thread_id="$thread_root",
                    reply_to_event_id=event.event_id,
                )
            ),
            correlation_id="corr-hydrate-dispatch",
            envelope=_hook_envelope(body="hello", source_event_id="$event", target=dispatch_target),
        )
        _set_turn_store_tracker(bot, MagicMock())
        snapshot_history = list(dispatch.context.thread_history)
        full_history = [
            *snapshot_history,
            ResolvedVisibleMessage.synthetic(
                sender="@user:localhost",
                body="[Attached file]",
                event_id="$older-attachment",
                timestamp=2,
                content={
                    "body": "[Attached file]",
                    "com.mindroom.attachment_ids": ["att_older"],
                },
            ),
        ]
        call_order: list[str] = []

        async def fake_plan(*_args: object, **_kwargs: object) -> _DispatchPlan:
            call_order.append("action")
            assert list(dispatch.context.thread_history) == snapshot_history
            assert dispatch.context.planning_thread_history == ()
            assert dispatch.context.planning_thread_history_unavailable is True
            return _DispatchPlan(
                kind="respond",
                response_action=ResponseAction(kind="individual"),
            )

        async def fake_build_payload(context: MessageContext) -> DispatchPayload:
            call_order.append("payload")
            assert list(context.thread_history) == full_history
            return DispatchPayload(prompt="hello", attachment_ids=["att_older"])

        async def refresh_thread_history(
            request: ResponseRequest,
            *,
            exclude_event_id: str | None = None,
        ) -> ResponseRequest:
            del exclude_event_id
            return replace(
                request,
                thread_history=ThreadHistoryResult(full_history, is_full_history=True),
                requires_model_history_refresh=False,
            )

        async def run_cancellable_response(**kwargs: object) -> str | None:
            call_order.append("generate")
            response_function = kwargs["response_function"]
            assert callable(response_function)
            await cast("Any", response_function)(None)
            return None

        def prepare_memory_and_model_context(
            prompt: str,
            thread_history: Sequence[ResolvedVisibleMessage],
            *,
            config: Config,
            runtime_paths: RuntimePaths,
            model_prompt: str | None = None,
        ) -> tuple[str, Sequence[ResolvedVisibleMessage], str | None, Sequence[ResolvedVisibleMessage]]:
            del config, runtime_paths
            return prompt, thread_history, model_prompt, thread_history

        with (
            patch.object(
                bot._turn_controller,
                "_prepare_dispatch",
                new=AsyncMock(return_value=prepared_dispatch_result(dispatch)),
            ),
            patch.object(bot._turn_policy, "plan_turn", new=AsyncMock(side_effect=fake_plan)),
            patch.object(
                ResponseRunner,
                "_refresh_model_history_after_lock",
                new=AsyncMock(side_effect=refresh_thread_history),
            ) as mock_refresh_thread_history,
            patch.object(
                bot._inbound_turn_normalizer,
                "build_dispatch_payload_with_attachments",
                new=AsyncMock(side_effect=fake_build_payload),
            ) as mock_build_payload,
            patch.object(
                ResponseRunner,
                "process_and_respond",
                new=AsyncMock(
                    return_value=_ResponseGenerationOutcome(
                        delivery=FinalDeliveryOutcome(
                            terminal_status="completed",
                            event_id="$response",
                            is_visible_response=True,
                            final_visible_body="ok",
                        ),
                        run_succeeded=True,
                    ),
                ),
            ) as mock_process,
            patch.object(
                ResponseRunner,
                "run_cancellable_response",
                new=AsyncMock(side_effect=run_cancellable_response),
            ),
            patch.object(ResponsePayloadPreparer, "_log_dispatch_latency"),
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=False),
                prepare_memory_and_model_context=prepare_memory_and_model_context,
                reprioritize_auto_flush_sessions=MagicMock(),
                apply_post_response_effects=AsyncMock(),
            ),
        ):
            await bot._turn_controller._dispatch_text_message(
                room,
                _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
            )
            await drain_coalescing(bot)

        mock_refresh_thread_history.assert_awaited_once()
        mock_build_payload.assert_awaited_once()
        process_request = mock_process.await_args.args[0]
        assert list(process_request.thread_history) == full_history
        assert process_request.attachment_ids == ("att_older",)
        assert call_order == ["action", "payload", "generate"]

    @pytest.mark.asyncio
    async def test_dispatch_text_message_skip_path_does_not_hydrate_full_history_before_planning(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Planning should use policy-grade history only and skip model refresh on ignore."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$event"
        event.sender = "@user:localhost"
        event.body = "hello"
        event.server_timestamp = 1234567890
        event.source = {"content": {"body": "hello"}}

        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=False,
                is_thread=True,
                thread_id="$thread_root",
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
                requires_model_history_refresh=True,
            ),
            target=(
                dispatch_target := MessageTarget.resolve(
                    room_id=room.room_id,
                    thread_id="$thread_root",
                    reply_to_event_id=event.event_id,
                )
            ),
            correlation_id="corr-no-action",
            envelope=_hook_envelope(body="hello", source_event_id="$event", target=dispatch_target),
        )

        async def fake_plan(
            *_args: object,
            **_kwargs: object,
        ) -> _DispatchPlan:
            assert list(dispatch.context.thread_history) == []
            assert dispatch.context.planning_thread_history == ()
            assert dispatch.context.requires_model_history_refresh is True
            return _DispatchPlan(kind="ignore")

        with (
            patch.object(
                bot._turn_controller,
                "_prepare_dispatch",
                new=AsyncMock(return_value=prepared_dispatch_result(dispatch)),
            ),
            patch.object(
                bot._turn_policy,
                "plan_turn",
                new=AsyncMock(side_effect=fake_plan),
            ),
            patch.object(
                bot._inbound_turn_normalizer,
                "build_dispatch_payload_with_attachments",
                new=AsyncMock(),
            ) as mock_build_payload,
            patch.object(bot._turn_controller, "_execute_response_action", new=AsyncMock()) as mock_execute,
        ):
            await bot._turn_controller._dispatch_text_message(
                room,
                _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
            )

        mock_build_payload.assert_not_awaited()
        mock_execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dispatch_text_message_command_bypasses_full_history_hydration(
        self,
        tmp_path: Path,
    ) -> None:
        """Commands should short-circuit before full thread-history hydration."""
        agent_user = AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$command"
        event.sender = "@user:localhost"
        event.body = "!help"
        event.server_timestamp = 1234567890
        event.source = {"content": {"body": "!help"}}

        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=False,
                is_thread=True,
                thread_id="$thread_root",
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
                requires_model_history_refresh=True,
            ),
            target=(
                dispatch_target := MessageTarget.resolve(
                    room_id=room.room_id,
                    thread_id="$thread_root",
                    reply_to_event_id=event.event_id,
                )
            ),
            correlation_id="corr-command-bypass",
            envelope=_hook_envelope(body="!help", source_event_id="$command", target=dispatch_target),
        )

        with (
            patch.object(
                bot._inbound_turn_normalizer,
                "resolve_text_event",
                new=AsyncMock(return_value=event),
            ),
            patch.object(
                bot._turn_controller,
                "_prepare_dispatch",
                new=AsyncMock(return_value=prepared_dispatch_result(dispatch)),
            ),
            patch.object(bot._turn_controller, "_execute_command", new=AsyncMock()) as mock_execute_command,
        ):
            await bot._turn_controller._dispatch_text_message(
                room,
                _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
            )

        mock_execute_command.assert_awaited_once()
        assert mock_execute_command.await_args.kwargs["target"] == dispatch.target

    @pytest.mark.asyncio
    async def test_dispatch_text_message_command_uses_snapshot_target_context(
        self,
        tmp_path: Path,
    ) -> None:
        """Command dispatch should resolve targets with the bounded snapshot path."""
        agent_user = AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$command"
        event.sender = "@user:localhost"
        event.body = "!help"
        event.server_timestamp = 1234567890
        event.source = {
            "event_id": "$command",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567890,
            "room_id": room.room_id,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.text",
                "body": "!help",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        }
        snapshot_history = thread_history_result([], is_full_history=False)

        with (
            patch.object(
                bot._inbound_turn_normalizer,
                "resolve_text_event",
                new=AsyncMock(return_value=event),
            ),
            patch.object(
                bot._conversation_cache,
                "get_dispatch_thread_history",
                new=AsyncMock(side_effect=AssertionError("command used full dispatch history")),
            ) as mock_full_history,
            patch.object(
                bot._conversation_cache,
                "get_dispatch_thread_snapshot",
                new=AsyncMock(return_value=snapshot_history),
            ) as mock_snapshot,
            patch.object(bot._turn_controller, "_execute_command", new=AsyncMock()) as mock_execute_command,
        ):
            await bot._turn_controller._dispatch_text_message(
                room,
                _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
            )

        mock_full_history.assert_not_awaited()
        mock_snapshot.assert_awaited_once_with(
            room.room_id,
            "$thread_root",
            caller_label="dispatch_command_context",
        )
        mock_execute_command.assert_awaited_once()
        assert mock_execute_command.await_args.kwargs["target"].resolved_thread_id == "$thread_root"

    @pytest.mark.asyncio
    async def test_router_dispatch_marks_visible_echo_from_any_coalesced_source_event(
        self,
        tmp_path: Path,
    ) -> None:
        """Router ignore plans should preserve visible echoes recorded on non-primary source events."""
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = _make_matrix_client_mock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        tracker.visible_echo_event_id_for_sources.side_effect = lambda source_event_ids: (
            "$voice_echo" if tuple(source_event_ids) == ("$voice", "$text") else None
        )
        tracker.get_turn_record.side_effect = lambda source_event_id: (
            TurnRecord.create(
                ["$voice", "$text"],
                completed=False,
                visible_echo_event_id="$voice_echo",
            )
            if source_event_id in {"$voice", "$text"}
            else None
        )
        tracker.has_responded.return_value = False

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$text"
        event.sender = "@user:localhost"
        event.body = "hello"
        event.server_timestamp = 1234567890
        event.source = {"content": {"body": "hello"}}

        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=False,
                is_thread=True,
                thread_id="$thread_root",
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=True,
            ),
            target=(
                dispatch_target := MessageTarget.resolve(
                    room_id=room.room_id,
                    thread_id="$thread_root",
                    reply_to_event_id=event.event_id,
                )
            ),
            correlation_id="corr-visible-echo",
            envelope=_hook_envelope(body="hello", source_event_id="$text", target=dispatch_target),
        )

        with (
            patch.object(bot._inbound_turn_normalizer, "resolve_text_event", new=AsyncMock(return_value=event)),
            patch.object(
                bot._turn_controller,
                "_prepare_dispatch",
                new=AsyncMock(return_value=prepared_dispatch_result(dispatch)),
            ),
            patch.object(bot._turn_controller, "_has_newer_unresponded_in_thread", return_value=False),
            patch.object(bot._turn_controller, "_should_skip_deep_synthetic_full_dispatch", return_value=False),
        ):
            await bot._turn_controller._dispatch_text_message(
                room,
                _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
                handled_turn=TurnRecord.create(
                    ["$voice", "$text"],
                    source_event_prompts={"$voice": "voice prompt", "$text": "text prompt"},
                ),
            )

        assert tracker.record_handled_turn.call_args_list == [
            call(
                TurnRecord.create(
                    ["$voice", "$text"],
                    response_event_id="$voice_echo",
                    source_event_prompts={"$voice": "voice prompt", "$text": "text prompt"},
                    visible_echo_event_id="$voice_echo",
                    requester_id="@user:localhost",
                    correlation_id="corr-visible-echo",
                ),
            ),
        ]

    @pytest.mark.asyncio
    async def test_dispatch_text_message_preserves_prompt_map_when_router_routes_coalesced_turn(
        self,
        tmp_path: Path,
    ) -> None:
        """Router handoff for a coalesced turn should persist the full prompt map."""
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        _wrap_extracted_collaborators(bot)
        bot.client = _make_matrix_client_mock()
        tracker = _set_turn_store_tracker(bot, MagicMock())

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.canonical_alias = None
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$text"
        event.sender = "@user:localhost"
        event.body = "hello"
        event.server_timestamp = 1234567890
        event.source = {"content": {"body": "hello"}}

        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=False,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
            ),
            target=(
                dispatch_target := MessageTarget.resolve(
                    room_id=room.room_id,
                    thread_id=None,
                    reply_to_event_id=event.event_id,
                    thread_start_root_event_id=event.event_id,
                )
            ),
            correlation_id="corr-router-coalesced",
            envelope=_hook_envelope(body="hello", source_event_id="$text", target=dispatch_target),
        )
        coalesced_turn = TurnRecord.create(
            ["$voice", "$text"],
            source_event_prompts={"$voice": "voice prompt", "$text": "hello"},
        )

        async def fake_execute_router_relay(
            _room: nio.MatrixRoom,
            _event: nio.RoomMessageText,
            _thread_history: Sequence[ResolvedVisibleMessage],
            _thread_id: str | None = None,
            message: str | None = None,
            *,
            requester_user_id: str,
            extra_content: dict[str, Any] | None = None,
            media_events: list[object] | None = None,
            handled_turn: TurnRecord | None = None,
        ) -> None:
            assert message == "hello"
            assert requester_user_id == "@user:localhost"
            assert extra_content is None
            assert media_events is None
            assert handled_turn is not None
            assert handled_turn.source_event_prompts == {"$voice": "voice prompt", "$text": "hello"}
            bot._turn_controller._mark_source_events_responded(replace(handled_turn, response_event_id="$route"))

        with (
            patch.object(bot._inbound_turn_normalizer, "resolve_text_event", new=AsyncMock(return_value=event)),
            patch.object(
                bot._turn_controller,
                "_prepare_dispatch",
                new=AsyncMock(return_value=prepared_dispatch_result(dispatch)),
            ),
            patch.object(
                bot._turn_policy,
                "plan_turn",
                new=AsyncMock(
                    return_value=_DispatchPlan(
                        kind="route",
                        router_message="hello",
                        router_event=event,
                    ),
                ),
            ),
            patch.object(
                bot._turn_controller,
                "_execute_router_relay",
                new=AsyncMock(side_effect=fake_execute_router_relay),
            ),
            patch.object(bot._turn_controller, "_has_newer_unresponded_in_thread", return_value=False),
            patch.object(bot._turn_controller, "_should_skip_deep_synthetic_full_dispatch", return_value=False),
        ):
            await bot._turn_controller._dispatch_text_message(
                room,
                _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
                handled_turn=coalesced_turn,
            )

        assert tracker.record_handled_turn.call_args_list == [
            call(
                replace(
                    TurnRecord.create(
                        ["$voice", "$text"],
                        response_event_id="$route",
                        source_event_prompts={"$voice": "voice prompt", "$text": "hello"},
                    ),
                    response_owner="router",
                    requester_id="@user:localhost",
                    correlation_id="corr-router-coalesced",
                    history_scope=None,
                    conversation_target=dispatch.target,
                ),
            ),
        ]

    @pytest.mark.asyncio
    async def test_trusted_internal_router_relays_use_gate_bypass(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Agent-authored relays should enter the gate as FIFO bypass events."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.canonical_alias = None
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$relay",
                "sender": "@mindroom_router:localhost",
                "origin_server_ts": 1234567890,
                "content": {
                    "msgtype": "m.text",
                    "body": "@mindroom_calculator:localhost could you help with this?",
                    ORIGINAL_SENDER_KEY: "@user:localhost",
                    SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                },
            },
        )

        with (
            patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock()) as mock_dispatch,
            patch.object(bot._coalescing_gate, "admit", new=AsyncMock()) as mock_admit,
        ):
            reservation_owner = bot._turn_controller._reserve_prompt_ingress_order(room, "@user:localhost")
            await bot._turn_controller._enqueue_for_dispatch(
                event,
                room,
                source_kind=MESSAGE_SOURCE_KIND,
                requester_user_id="@user:localhost",
                reservation_owner=reservation_owner,
            )
            await asyncio.wait_for(reservation_owner.slot.settled.wait(), timeout=1.0)

        mock_dispatch.assert_not_awaited()
        mock_admit.assert_awaited_once()
        key = mock_admit.await_args.args[0]
        ready_result = mock_admit.await_args.kwargs["ready_result"]
        assert isinstance(ready_result, ReadyPendingEvent)
        pending_event = ready_result.pending_event
        assert key == CoalescingKey(room.room_id, None, "@user:localhost")
        assert isinstance(pending_event, PendingEvent)
        assert pending_event.event is event
        assert pending_event.source_kind == TRUSTED_INTERNAL_RELAY_SOURCE_KIND

    @staticmethod
    def _router_relay_event(
        *,
        sender: str = "@mindroom_router:localhost",
        reply_to: str | None = "$user_msg:localhost",
        is_falling_back: bool = False,
    ) -> nio.RoomMessageText:
        content: dict[str, Any] = {
            "msgtype": "m.text",
            "body": "@mindroom_calculator:localhost could you help with this?",
            ORIGINAL_SENDER_KEY: "@user:localhost",
            SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        }
        if reply_to is not None:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": "$thread_root:localhost",
                "is_falling_back": is_falling_back,
                "m.in_reply_to": {"event_id": reply_to},
            }
        return nio.RoomMessageText.from_dict(
            {
                "event_id": "$relay:localhost",
                "sender": sender,
                "origin_server_ts": 1234567890,
                "content": content,
            },
        )

    @pytest.mark.asyncio
    async def test_router_relay_dispatch_indexes_routed_original_event(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Routed turns must stay discoverable by the human event the router relayed."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.canonical_alias = None
        event = self._router_relay_event()

        with (
            patch.object(
                bot._inbound_turn_normalizer,
                "resolve_text_event",
                new=AsyncMock(return_value=event),
            ),
            patch.object(
                bot._turn_controller,
                "_prepare_dispatch",
                new=AsyncMock(return_value=None),
            ) as mock_prepare,
        ):
            await bot._turn_controller._dispatch_text_message(
                room,
                _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
            )

        handled_turn = mock_prepare.await_args.kwargs["handled_turn"]
        assert handled_turn.source_event_ids == ("$relay:localhost",)
        assert "$user_msg:localhost" in handled_turn.discovery_event_ids

    @pytest.mark.asyncio
    async def test_router_relay_original_event_requires_explicit_router_reply(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Fallback replies and non-router relays must not alias the routed turn."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        validator = bot._ingress_validator

        explicit_relay = self._router_relay_event()
        fallback_relay = self._router_relay_event(reply_to="$latest:localhost", is_falling_back=True)
        replyless_relay = self._router_relay_event(reply_to=None)
        non_router_relay = self._router_relay_event(sender="@mindroom_general:localhost")

        assert validator.router_relay_original_event_id(explicit_relay) == "$user_msg:localhost"
        assert validator.router_relay_original_event_id(fallback_relay) is None
        assert validator.router_relay_original_event_id(replyless_relay) is None
        assert validator.router_relay_original_event_id(non_router_relay) is None

    @pytest.mark.asyncio
    async def test_external_trigger_to_private_agent_uses_trigger_owner_as_requester(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Private external-trigger dispatch should run as the triggering owner."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!room:localhost"],
                        private=AgentPrivateConfig(per="user", root="calculator_data"),
                    ),
                },
                models={"default": ModelConfig(provider="openai", id="test-model")},
                authorization={
                    "global_users": ["@owner:localhost"],
                    "agent_reply_permissions": {"calculator": ["@owner:localhost"]},
                },
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        ids = entity_ids(config, runtime_paths)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        _install_runtime_cache_support(bot)
        bot.client = _make_matrix_client_mock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        tracker.has_responded.return_value = False
        generate_response = AsyncMock(return_value="$response")
        install_generate_response_mock(bot, generate_response)
        room = _matrix_room(
            room_id="!room:localhost",
            own_user_id=mock_agent_user.user_id,
            user_ids=[
                ids[ROUTER_AGENT_NAME].full_id,
                ids["calculator"].full_id,
                "@owner:localhost",
            ],
        )
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$external-trigger",
                "sender": ids[ROUTER_AGENT_NAME].full_id,
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "body": "@CalculatorAgent Campground opened",
                    "m.mentions": {"user_ids": [ids["calculator"].full_id]},
                    SOURCE_KIND_KEY: EXTERNAL_TRIGGER_SOURCE_KIND,
                    ORIGINAL_SENDER_KEY: "@owner:localhost",
                },
            },
        )

        with patch("mindroom.text_ingress_dispatch.is_dm_room", new_callable=AsyncMock, return_value=False):
            await bot._on_message(room, event)
            await drain_coalescing(bot)

        generate_response.assert_awaited_once()
        generate_kwargs = generate_response.await_args.kwargs
        assert generate_kwargs["user_id"] == "@owner:localhost"
        assert generate_kwargs["response_envelope"].requester_id == "@owner:localhost"

    @pytest.mark.asyncio
    async def test_human_forged_external_trigger_metadata_uses_human_sender_as_requester(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Human-authored trigger metadata must not spoof the effective requester."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!room:localhost"],
                        private=AgentPrivateConfig(per="user", root="calculator_data"),
                    ),
                },
                models={"default": ModelConfig(provider="openai", id="test-model")},
                authorization={
                    "global_users": ["@mallory:localhost", "@victim:localhost"],
                    "agent_reply_permissions": {
                        "calculator": ["@mallory:localhost", "@victim:localhost"],
                    },
                },
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        ids = entity_ids(config, runtime_paths)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        _install_runtime_cache_support(bot)
        bot.client = _make_matrix_client_mock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        tracker.has_responded.return_value = False
        generate_response = AsyncMock(return_value="$response")
        install_generate_response_mock(bot, generate_response)
        room = _matrix_room(
            room_id="!room:localhost",
            own_user_id=mock_agent_user.user_id,
            user_ids=[
                ids["calculator"].full_id,
                "@mallory:localhost",
                "@victim:localhost",
            ],
        )
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$forged-external-trigger",
                "sender": "@mallory:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "body": "@CalculatorAgent Campground opened",
                    "m.mentions": {"user_ids": [ids["calculator"].full_id]},
                    SOURCE_KIND_KEY: EXTERNAL_TRIGGER_SOURCE_KIND,
                    ORIGINAL_SENDER_KEY: "@victim:localhost",
                },
            },
        )

        with patch("mindroom.text_ingress_dispatch.is_dm_room", new_callable=AsyncMock, return_value=False):
            await bot._on_message(room, event)
            await drain_coalescing(bot)

        generate_response.assert_awaited_once()
        generate_kwargs = generate_response.await_args.kwargs
        assert generate_kwargs["user_id"] == "@mallory:localhost"
        assert generate_kwargs["response_envelope"].requester_id == "@mallory:localhost"

    @pytest.mark.asyncio
    async def test_handle_message_inner_enqueues_active_thread_follow_up_as_coalescible_gate_event(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Human follow-ups in an active thread must keep policy while remaining coalescible."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        bot.client = _make_matrix_client_mock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$followup",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "body": "stop right now!",
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$thread_root",
                        "is_falling_back": True,
                        "m.in_reply_to": {"event_id": "$thread_root"},
                    },
                },
            },
        )
        prepared_event = PreparedTextEvent(
            sender="@user:localhost",
            event_id="$followup",
            body="stop right now!",
            source=event.source,
            server_timestamp=1234567890,
        )
        target = MessageTarget.resolve(room.room_id, "$thread_root", event.event_id)
        envelope = MessageEnvelope(
            source_event_id=event.event_id,
            target=target,
            body="stop right now!",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=bot.agent_name,
            origin=message_origin(
                sender_id="@user:localhost",
                requester_id="@user:localhost",
                source_kind=MESSAGE_SOURCE_KIND,
            ),
        )

        with (
            patch.object(
                bot._turn_controller,
                "_precheck_dispatch_event",
                return_value=_PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
            ),
            patch(
                "mindroom.inbound_turn_normalizer.InboundTurnNormalizer.resolve_text_event",
                new=AsyncMock(return_value=prepared_event),
            ),
            patch(
                "mindroom.conversation_resolver.ConversationResolver.build_ingress_envelope",
                return_value=envelope,
            ),
            patch.object(bot._turn_controller, "_should_skip_deep_synthetic_full_dispatch", return_value=False),
            patch.object(
                bot._response_runner,
                "active_thread_ids_for_room",
                return_value=frozenset({"$thread_root"}),
            ) as mock_active_thread_ids,
            patch.object(
                bot._response_runner,
                "has_active_response_for_target",
                return_value=True,
            ),
            patch.object(
                bot._response_runner,
                "reserve_waiting_human_message",
                return_value=MagicMock(),
            ) as mock_reserve_waiting_human_message,
            patch.object(
                bot._turn_controller,
                "_dispatch_text_message",
                new=AsyncMock(),
            ) as mock_dispatch,
            patch.object(bot._coalescing_gate, "admit", new=AsyncMock()) as mock_admit,
        ):
            await asyncio.wait_for(bot._on_message(room, event), timeout=0.05)
            await asyncio.wait_for(bot._coalescing_gate.drain_all(), timeout=1.0)

        mock_active_thread_ids.assert_called_with(room.room_id)
        mock_reserve_waiting_human_message.assert_called_once()
        signal_target = mock_reserve_waiting_human_message.call_args.kwargs["target"]
        assert signal_target.resolved_thread_id == target.resolved_thread_id
        assert mock_reserve_waiting_human_message.call_args.kwargs["response_envelope"] is envelope
        mock_dispatch.assert_not_awaited()
        mock_admit.assert_awaited_once()
        key = mock_admit.await_args.args[0]
        ready_result = mock_admit.await_args.kwargs["ready_result"]
        assert isinstance(ready_result, ReadyPendingEvent)
        pending_event = ready_result.pending_event
        assert key == CoalescingKey(room.room_id, "$thread_root", "@user:localhost")
        assert isinstance(pending_event, PendingEvent)
        assert pending_event.requester_user_id == "@user:localhost"
        assert pending_event.event is event
        assert pending_event.source_kind == MESSAGE_SOURCE_KIND
        assert pending_event.dispatch_policy_source_kind is None
        assert len(pending_event.dispatch_metadata) == 1
        metadata = pending_event.dispatch_metadata[0]
        assert metadata.kind == "queued_notice_reservation"
        assert metadata.payload is mock_reserve_waiting_human_message.return_value
        assert metadata.requires_solo_batch is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source_kind", ["hook", "hook_dispatch"])
    async def test_handle_message_inner_enqueues_trusted_hook_source_kind_as_gate_bypass(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        source_kind: str,
    ) -> None:
        """Trusted hook messages should keep their bypass source kind on the real text path."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        bot.client = _make_matrix_client_mock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": f"${source_kind}",
                "sender": "@mindroom_general:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "body": f"@mindroom_calculator:localhost {source_kind} says hello",
                    SOURCE_KIND_KEY: source_kind,
                    ORIGINAL_SENDER_KEY: "@user:localhost",
                },
            },
        )
        prepared_event = PreparedTextEvent(
            sender="@mindroom_general:localhost",
            event_id=f"${source_kind}",
            body=f"@mindroom_calculator:localhost {source_kind} says hello",
            source=event.source,
            server_timestamp=1234567890,
        )

        with (
            patch.object(
                bot._turn_controller,
                "_precheck_dispatch_event",
                return_value=_PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
            ),
            patch(
                "mindroom.inbound_turn_normalizer.InboundTurnNormalizer.resolve_text_event",
                new=AsyncMock(return_value=prepared_event),
            ),
            patch.object(bot._turn_controller, "_should_skip_deep_synthetic_full_dispatch", return_value=False),
            patch.object(
                bot._conversation_resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value=None),
            ),
            patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock()) as mock_dispatch,
            patch.object(bot._coalescing_gate, "admit", new=AsyncMock()) as mock_admit,
        ):
            await asyncio.wait_for(bot._on_message(room, event), timeout=0.05)
            await asyncio.wait_for(bot._coalescing_gate.drain_all(), timeout=1.0)

        mock_dispatch.assert_not_awaited()
        mock_admit.assert_awaited_once()
        ready_result = mock_admit.await_args.kwargs["ready_result"]
        assert isinstance(ready_result, ReadyPendingEvent)
        pending_event = ready_result.pending_event
        assert isinstance(pending_event, PendingEvent)
        assert pending_event.event is event
        assert pending_event.source_kind == source_kind

    @pytest.mark.asyncio
    async def test_voice_preview_reserves_active_thread_follow_up(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Transcribed voice follow-ups should share the active-response notice path."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        bot.client = _make_matrix_client_mock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        voice_event = _room_audio_event(sender="@user:localhost", event_id="$voice-followup", room_id=room.room_id)
        voice_event.source["content"]["m.relates_to"] = {"rel_type": "m.thread", "event_id": "$thread_root"}
        prepared_event = PreparedTextEvent(
            sender="@user:localhost",
            event_id="$voice-followup",
            body="please stop",
            source={"content": {"msgtype": "m.text", "body": "please stop", SOURCE_KIND_KEY: "voice"}},
            server_timestamp=1234567890,
            source_kind_override="voice",
        )

        with (
            patch(
                "mindroom.inbound_turn_normalizer.InboundTurnNormalizer.prepare_voice_event",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        event=prepared_event,
                    ),
                ),
            ),
            patch.object(bot._turn_controller, "_maybe_send_visible_voice_echo", new=AsyncMock()) as mock_echo,
            patch.object(
                bot._response_runner,
                "active_thread_ids_for_room",
                return_value=frozenset({"$thread_root"}),
            ),
            patch.object(
                bot._response_runner,
                "has_active_response_for_target",
                return_value=True,
            ),
            patch.object(
                bot._response_runner,
                "reserve_waiting_human_message",
                return_value=MagicMock(),
            ) as mock_reserve_waiting_human_message,
            patch.object(bot._coalescing_gate, "admit", new=AsyncMock()) as mock_admit,
        ):
            reservation_owner = bot._turn_controller._reserve_prompt_ingress_order(room, "@user:localhost")
            await bot._turn_controller._on_audio_media_message(
                room,
                _PrecheckedEvent(event=voice_event, requester_user_id="@user:localhost"),
                event_info=EventInfo.from_event(voice_event.source),
                dispatch_timing=None,
                reservation_owner=reservation_owner,
            )
            await asyncio.wait_for(reservation_owner.slot.settled.wait(), timeout=1.0)
            mock_admit.assert_awaited_once()
            key = mock_admit.await_args.args[0]
            assert key == CoalescingKey(room.room_id, "$thread_root", "@user:localhost")
            ready_event = mock_admit.await_args.kwargs["ready_result"]

        assert isinstance(ready_event, ReadyPendingEvent)
        mock_echo.assert_awaited_once()
        mock_reserve_waiting_human_message.assert_called_once()
        reserved_target = mock_reserve_waiting_human_message.call_args.kwargs["target"]
        assert reserved_target.resolved_thread_id == "$thread_root"
        reserved_envelope = mock_reserve_waiting_human_message.call_args.kwargs["response_envelope"]
        assert reserved_envelope.source_kind == VOICE_SOURCE_KIND
        pending_event = ready_event.pending_event
        assert isinstance(pending_event, PendingEvent)
        assert pending_event.requester_user_id == "@user:localhost"
        assert pending_event.event is prepared_event
        assert pending_event.source_kind == VOICE_SOURCE_KIND
        assert pending_event.dispatch_policy_source_kind is None
        assert len(pending_event.dispatch_metadata) == 1
        metadata = pending_event.dispatch_metadata[0]
        assert metadata.kind == "queued_notice_reservation"
        assert metadata.payload is mock_reserve_waiting_human_message.return_value
        assert metadata.requires_solo_batch is False

    @pytest.mark.asyncio
    async def test_file_sidecar_text_preview_enqueues_prepared_text(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """File sidecar previews should hand prepared text to the gate, not dispatch inline."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        bot.client = _make_matrix_client_mock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        sidecar_event = cast(
            "nio.RoomMessageFile",
            nio.Event.parse_event(
                {
                    "event_id": "$sidecar",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567890,
                    "room_id": room.room_id,
                    "type": "m.room.message",
                    "content": {
                        "msgtype": "m.file",
                        "body": "long-text.txt",
                        "info": {"mimetype": "application/json"},
                        "io.mindroom.long_text": {
                            "version": 2,
                            "encoding": "matrix_event_content_json",
                        },
                        "url": "mxc://localhost/sidecar",
                    },
                },
            ),
        )
        prepared_event = PreparedTextEvent(
            sender="@user:localhost",
            event_id="$sidecar",
            body="full long text",
            source={"content": {"msgtype": "m.text", "body": "full long text"}},
            server_timestamp=1234567890,
        )
        target = MessageTarget.resolve(room.room_id, "$thread_root", sidecar_event.event_id)
        envelope = MessageEnvelope(
            source_event_id=sidecar_event.event_id,
            target=target,
            body="full long text",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=bot.agent_name,
            origin=message_origin(
                sender_id="@user:localhost",
                requester_id="@user:localhost",
                source_kind=MESSAGE_SOURCE_KIND,
            ),
        )

        with (
            patch(
                "mindroom.inbound_turn_normalizer.InboundTurnNormalizer.prepare_file_sidecar_text_event",
                new=AsyncMock(return_value=prepared_event),
            ),
            patch.object(bot._conversation_resolver, "build_ingress_envelope", return_value=envelope),
            patch.object(bot._turn_controller, "_should_skip_deep_synthetic_full_dispatch", return_value=False),
            patch("mindroom.turn_controller.interactive.handle_text_response", new=AsyncMock(return_value=None)),
            patch.object(
                bot._conversation_resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$thread_root"),
            ),
            patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock()) as mock_dispatch,
            patch.object(bot._coalescing_gate, "admit", new=AsyncMock()) as mock_admit,
        ):
            reservation_owner = bot._turn_controller._reserve_prompt_ingress_order(room, "@user:localhost")
            handled = await bot._turn_controller._dispatch_file_sidecar_text_preview(
                room,
                _PrecheckedEvent(event=sidecar_event, requester_user_id="@user:localhost"),
                reservation_owner=reservation_owner,
                coalescing_thread_id="$thread_root",
            )
            await asyncio.wait_for(reservation_owner.slot.settled.wait(), timeout=1.0)

        assert handled is _IngressAdmissionOutcome.ADMITTED
        mock_dispatch.assert_not_awaited()
        mock_admit.assert_awaited_once()
        key = mock_admit.await_args.args[0]
        ready_result = mock_admit.await_args.kwargs["ready_result"]
        assert isinstance(ready_result, ReadyPendingEvent)
        pending_event = ready_result.pending_event
        assert key == CoalescingKey(room.room_id, "$thread_root", "@user:localhost")
        assert isinstance(pending_event, PendingEvent)
        assert pending_event.requester_user_id == "@user:localhost"
        assert pending_event.event is prepared_event
        assert pending_event.source_kind == MESSAGE_SOURCE_KIND

    @pytest.mark.asyncio
    async def test_file_sidecar_text_preview_reserves_active_thread_follow_up(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Sidecar text follow-ups should share the active-response notice path."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        bot.client = _make_matrix_client_mock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        sidecar_event = cast(
            "nio.RoomMessageFile",
            nio.Event.parse_event(
                {
                    "event_id": "$sidecar-followup",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567890,
                    "room_id": room.room_id,
                    "type": "m.room.message",
                    "content": {
                        "msgtype": "m.file",
                        "body": "long-text.txt",
                        "info": {"mimetype": "application/json"},
                        "io.mindroom.long_text": {
                            "version": 2,
                            "encoding": "matrix_event_content_json",
                        },
                        "url": "mxc://localhost/sidecar-followup",
                    },
                },
            ),
        )
        prepared_event = PreparedTextEvent(
            sender="@user:localhost",
            event_id="$sidecar-followup",
            body="please stop",
            source={"content": {"msgtype": "m.text", "body": "please stop"}},
            server_timestamp=1234567890,
        )
        target = MessageTarget.resolve(room.room_id, "$thread_root", sidecar_event.event_id)
        envelope = MessageEnvelope(
            source_event_id=sidecar_event.event_id,
            target=target,
            body="please stop",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=bot.agent_name,
            origin=message_origin(
                sender_id="@user:localhost",
                requester_id="@user:localhost",
                source_kind=MESSAGE_SOURCE_KIND,
            ),
        )

        with (
            patch(
                "mindroom.inbound_turn_normalizer.InboundTurnNormalizer.prepare_file_sidecar_text_event",
                new=AsyncMock(return_value=prepared_event),
            ),
            patch.object(bot._conversation_resolver, "build_ingress_envelope", return_value=envelope),
            patch.object(bot._turn_controller, "_should_skip_deep_synthetic_full_dispatch", return_value=False),
            patch("mindroom.turn_controller.interactive.handle_text_response", new=AsyncMock(return_value=None)),
            patch.object(
                bot._conversation_resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value="$thread_root"),
            ),
            patch.object(
                bot._response_runner,
                "active_thread_ids_for_room",
                return_value=frozenset({"$thread_root"}),
            ),
            patch.object(
                bot._response_runner,
                "has_active_response_for_target",
                return_value=True,
            ),
            patch.object(
                bot._response_runner,
                "reserve_waiting_human_message",
                return_value=MagicMock(),
            ) as mock_reserve_waiting_human_message,
            patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock()) as mock_dispatch,
            patch.object(bot._coalescing_gate, "admit", new=AsyncMock()) as mock_admit,
        ):
            reservation_owner = bot._turn_controller._reserve_prompt_ingress_order(room, "@user:localhost")
            handled = await bot._turn_controller._dispatch_file_sidecar_text_preview(
                room,
                _PrecheckedEvent(event=sidecar_event, requester_user_id="@user:localhost"),
                reservation_owner=reservation_owner,
                coalescing_thread_id="$thread_root",
            )
            await asyncio.wait_for(reservation_owner.slot.settled.wait(), timeout=1.0)

        assert handled is _IngressAdmissionOutcome.ADMITTED
        mock_dispatch.assert_not_awaited()
        mock_reserve_waiting_human_message.assert_called_once()
        mock_admit.assert_awaited_once()
        key = mock_admit.await_args.args[0]
        ready_result = mock_admit.await_args.kwargs["ready_result"]
        assert isinstance(ready_result, ReadyPendingEvent)
        pending_event = ready_result.pending_event
        assert key == CoalescingKey(room.room_id, "$thread_root", "@user:localhost")
        assert isinstance(pending_event, PendingEvent)
        assert pending_event.requester_user_id == "@user:localhost"
        assert pending_event.event is prepared_event
        assert pending_event.source_kind == MESSAGE_SOURCE_KIND
        assert pending_event.dispatch_policy_source_kind is None
        assert len(pending_event.dispatch_metadata) == 1
        metadata = pending_event.dispatch_metadata[0]
        assert metadata.kind == "queued_notice_reservation"
        assert metadata.payload is mock_reserve_waiting_human_message.return_value
        assert metadata.requires_solo_batch is False

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_team_defers_placeholder_creation_to_coordinator(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Planner-side team dispatch should hand placeholder ownership to the coordinator."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        bot.logger = MagicMock()
        _replace_turn_policy_deps(bot, logger=bot.logger)

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        event.body = "hello"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=True,
                is_thread=True,
                thread_id="$thread_root",
                thread_history=[
                    _visible_message(
                        sender="@user:localhost",
                        body="hello",
                        timestamp=0,
                        event_id="$thread_root",
                    ),
                ],
                mentioned_agents=[bot.matrix_id],
                has_non_agent_mentions=False,
                requires_model_history_refresh=False,
            ),
            target=(
                dispatch_target := MessageTarget.resolve(
                    room_id=room.room_id,
                    thread_id="$thread_root",
                    reply_to_event_id=event.event_id,
                )
            ),
            correlation_id="corr-team-dispatch",
            envelope=_hook_envelope(body="hello", source_event_id="$event", target=dispatch_target),
        )
        action = ResponseAction(
            kind="team",
            form_team=TeamResolution.team(
                intent=TeamIntent.EXPLICIT_MEMBERS,
                requested_members=[bot.matrix_id],
                member_statuses=[],
                eligible_members=[bot.matrix_id],
                mode=TeamMode.COORDINATE,
            ),
        )

        mock_send_response = AsyncMock()
        mock_generate_team_response = AsyncMock(
            return_value="$team-response",
        )
        install_send_response_mock(bot, mock_send_response)
        bot._response_runner.generate_team_response_helper = mock_generate_team_response
        _replace_turn_policy_deps(
            bot,
            delivery_gateway=bot._delivery_gateway,
            response_runner=bot._response_runner,
        )

        with (
            patch.object(ResponsePayloadPreparer, "_log_dispatch_latency"),
            patch(
                "mindroom.turn_controller.select_ad_hoc_team_mode",
                new=AsyncMock(return_value=TeamMode.COORDINATE),
            ),
        ):
            await bot._turn_controller._execute_response_action(
                room,
                event,
                dispatch,
                action,
                DispatchPayloadInputs((), (), ()),
                processing_log="processing",
                dispatch_started_at=0.0,
                handled_turn=TurnRecord.create([event.event_id]),
            )

        team_request = mock_generate_team_response.await_args.args[0]
        assert team_request.existing_event_id is None
        assert team_request.existing_event_is_placeholder is False
        mock_send_response.assert_not_awaited()
        tracker.record_handled_turn.assert_called_once_with(
            TurnRecord.create(
                ["$event"],
                response_event_id="$team-response",
            ),
        )

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_team_explicit_members_uses_ai_team_mode(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Ad-hoc team dispatch should ask the AI selector for the team mode."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        _set_turn_store_tracker(bot, MagicMock())
        bot.logger = MagicMock()
        _replace_turn_policy_deps(bot, logger=bot.logger)

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        event.body = "hello"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=True,
                is_thread=True,
                thread_id="$thread_root",
                thread_history=[],
                mentioned_agents=[bot.matrix_id],
                has_non_agent_mentions=False,
                requires_model_history_refresh=False,
            ),
            target=(
                dispatch_target := MessageTarget.resolve(
                    room_id=room.room_id,
                    thread_id="$thread_root",
                    reply_to_event_id=event.event_id,
                )
            ),
            correlation_id="corr-team-dispatch",
            envelope=_hook_envelope(body="hello", source_event_id="$event", target=dispatch_target),
        )
        action = ResponseAction(
            kind="team",
            form_team=TeamResolution.team(
                intent=TeamIntent.EXPLICIT_MEMBERS,
                requested_members=[bot.matrix_id],
                member_statuses=[],
                eligible_members=[bot.matrix_id],
                mode=TeamMode.COLLABORATE,
            ),
        )

        team_requests: list[ResponseRequest] = []

        async def generate_team_response(request: ResponseRequest, **_kwargs: object) -> str:
            team_requests.append(request)
            if len(team_requests) == 1:
                assert request.on_sync_restart_cancelled is not None
                request.on_sync_restart_cancelled()
            return "$team-response"

        mock_generate_team_response = AsyncMock(side_effect=generate_team_response)
        install_send_response_mock(bot, AsyncMock())
        bot._response_runner.generate_team_response_helper = mock_generate_team_response
        _replace_turn_policy_deps(
            bot,
            delivery_gateway=bot._delivery_gateway,
            response_runner=bot._response_runner,
        )

        mock_select_team_mode = AsyncMock(return_value=TeamMode.COORDINATE)
        with (
            patch.object(ResponsePayloadPreparer, "_log_dispatch_latency"),
            patch("mindroom.turn_controller.select_ad_hoc_team_mode", new=mock_select_team_mode),
        ):
            await bot._turn_controller._execute_response_action(
                room,
                event,
                dispatch,
                action,
                DispatchPayloadInputs((), (), ()),
                processing_log="processing",
                dispatch_started_at=0.0,
                handled_turn=TurnRecord.create([event.event_id]),
            )
            await bot._restart_retry_queue.flush()

        assert action.form_team is not None
        mock_select_team_mode.assert_awaited_once_with(
            event.body,
            action.form_team.eligible_members,
            bot._turn_controller.deps.runtime.config,
            bot._turn_controller.deps.runtime_paths,
        )
        assert [request.sync_restart_retry_source_event_id for request in team_requests] == [None, event.event_id]
        assert mock_generate_team_response.await_count == 2
        assert mock_generate_team_response.await_args.kwargs["team_mode"] == "coordinate"
        assert mock_generate_team_response.await_args.kwargs["team_agents"] == action.form_team.eligible_members

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_team_configured_team_skips_ai_team_mode(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Configured-team dispatch should pass the configured mode through without an AI call."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        _set_turn_store_tracker(bot, MagicMock())
        bot.logger = MagicMock()
        _replace_turn_policy_deps(bot, logger=bot.logger)

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        event.body = "hello"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=True,
                is_thread=True,
                thread_id="$thread_root",
                thread_history=[],
                mentioned_agents=[bot.matrix_id],
                has_non_agent_mentions=False,
                requires_model_history_refresh=False,
            ),
            target=(
                dispatch_target := MessageTarget.resolve(
                    room_id=room.room_id,
                    thread_id="$thread_root",
                    reply_to_event_id=event.event_id,
                )
            ),
            correlation_id="corr-team-dispatch",
            envelope=_hook_envelope(body="hello", source_event_id="$event", target=dispatch_target),
        )
        action = ResponseAction(
            kind="team",
            form_team=TeamResolution.team(
                intent=TeamIntent.CONFIGURED_TEAM,
                requested_members=[bot.matrix_id],
                member_statuses=[],
                eligible_members=[bot.matrix_id],
                mode=TeamMode.COORDINATE,
            ),
        )

        mock_generate_team_response = AsyncMock(return_value="$team-response")
        install_send_response_mock(bot, AsyncMock())
        bot._response_runner.generate_team_response_helper = mock_generate_team_response
        _replace_turn_policy_deps(
            bot,
            delivery_gateway=bot._delivery_gateway,
            response_runner=bot._response_runner,
        )

        mock_select_team_mode = AsyncMock(return_value=TeamMode.COLLABORATE)
        with (
            patch.object(ResponsePayloadPreparer, "_log_dispatch_latency"),
            patch("mindroom.turn_controller.select_ad_hoc_team_mode", new=mock_select_team_mode),
        ):
            await bot._turn_controller._execute_response_action(
                room,
                event,
                dispatch,
                action,
                DispatchPayloadInputs((), (), ()),
                processing_log="processing",
                dispatch_started_at=0.0,
                handled_turn=TurnRecord.create([event.event_id]),
            )

        mock_select_team_mode.assert_not_called()
        assert mock_generate_team_response.await_args.kwargs["team_mode"] == "coordinate"

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_does_not_send_placeholder_before_response_runner(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Planner-side execution should pass placeholder ownership to the coordinator."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        bot.logger = MagicMock()
        _replace_turn_policy_deps(bot, logger=bot.logger)

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=True,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[bot.matrix_id],
                has_non_agent_mentions=False,
                requires_model_history_refresh=False,
            ),
            target=(
                dispatch_target := MessageTarget.resolve(
                    room_id=room.room_id,
                    thread_id=None,
                    reply_to_event_id=event.event_id,
                )
            ),
            correlation_id="corr-individual-dispatch",
            envelope=_hook_envelope(body="hello", source_event_id="$event", target=dispatch_target),
        )

        mock_send_response = AsyncMock()
        mock_generate_response = AsyncMock(return_value="$response")
        install_send_response_mock(bot, mock_send_response)
        install_generate_response_mock(bot, mock_generate_response)
        _replace_turn_policy_deps(
            bot,
            delivery_gateway=bot._delivery_gateway,
            response_runner=bot._response_runner,
        )

        with patch.object(ResponsePayloadPreparer, "_log_dispatch_latency"):
            await bot._turn_controller._execute_response_action(
                room,
                event,
                dispatch,
                ResponseAction(kind="individual"),
                DispatchPayloadInputs((), (), ()),
                processing_log="processing",
                dispatch_started_at=0.0,
                handled_turn=TurnRecord.create([event.event_id]),
            )

        mock_send_response.assert_not_awaited()
        assert mock_generate_response.await_args.kwargs["existing_event_id"] is None
        assert mock_generate_response.await_args.kwargs["existing_event_is_placeholder"] is False
        tracker.record_handled_turn.assert_called_once_with(
            TurnRecord.create(
                ["$event"],
                response_event_id="$response",
            ),
        )

    @pytest.mark.asyncio
    async def test_media_download_failure_sends_terminal_error_without_placeholder(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Media setup failures before response generation should send one terminal error reply."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        tracker = MagicMock()
        tracker.has_responded.return_value = False
        _set_turn_store_tracker(bot, tracker)

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        room.users = {"@mindroom_calculator:localhost": MagicMock(), "@user:localhost": MagicMock()}
        event = _room_image_event(sender="@user:localhost", event_id="$img_event_fail", body="photo.jpg")
        event.source = {"content": {"body": "photo.jpg"}}

        bot._conversation_resolver.extract_message_context = AsyncMock(
            return_value=MessageContext(
                am_i_mentioned=False,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
                requires_model_history_refresh=False,
            ),
        )
        bot._edit_message = AsyncMock(return_value=True)
        install_edit_message_mock(bot, bot._edit_message)
        generate_response = AsyncMock()
        install_generate_response_mock(bot, generate_response)
        bot._delivery_gateway.send_text = AsyncMock(return_value="$error")
        wrap_extracted_collaborators(bot, "_turn_policy")
        bot._turn_policy.plan_turn = AsyncMock(
            return_value=_DispatchPlan(
                kind="respond",
                response_action=ResponseAction(kind="individual"),
            ),
        )

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
            patch("mindroom.text_ingress_dispatch.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch("mindroom.inbound_turn_normalizer.download_image", new_callable=AsyncMock, return_value=None),
            patch.object(ResponsePayloadPreparer, "_log_dispatch_latency"),
        ):
            await bot._on_media_message(room, event)
            await drain_coalescing(bot)

        generate_response.assert_not_called()
        bot._edit_message.assert_not_awaited()
        bot._delivery_gateway.send_text.assert_awaited_once()
        assert bot._delivery_gateway.send_text.await_args.args[0].response_text == (
            "[calculator] ⚠️ Error: Failed to download image"
        )
        expected_handled_turn = _agent_response_handled_turn(
            agent_name=mock_agent_user.agent_name,
            room_id=room.room_id,
            event_id="$img_event_fail",
            response_event_id="$error",
            requester_id="@user:localhost",
            correlation_id="$img_event_fail",
            source_event_prompts={"$img_event_fail": "[Attached image]"},
        )
        expected_handled_turn = replace(
            expected_handled_turn,
            response_event_id="$error",
            conversation_target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id=None,
                reply_to_event_id="$img_event_fail",
            ).with_thread_root("$img_event_fail"),
        )
        tracker.record_handled_turn.assert_called_once_with(
            expected_handled_turn,
        )

    @pytest.mark.asyncio
    async def test_finalize_dispatch_failure_sends_terminal_error_message(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Dispatch setup failures should go through the terminal delivery gateway."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        bot._delivery_gateway.send_text = AsyncMock(return_value="$error")
        _replace_turn_policy_deps(bot, delivery_gateway=bot._delivery_gateway)

        resolution = await bot._turn_controller._finalize_dispatch_failure(
            target=MessageTarget.resolve("!test:localhost", "$thread_root", "$event"),
            error=RuntimeError("boom"),
        )

        assert resolution == "$error"
        bot._delivery_gateway.send_text.assert_awaited_once_with(
            SendTextRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread_root", "$event"),
                response_text="[calculator] ⚠️ Error: boom",
                extra_content={STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED},
            ),
        )

    @pytest.mark.asyncio
    async def test_finalize_dispatch_failure_uses_system_response_kind_for_team_bot(
        self,
        tmp_path: Path,
    ) -> None:
        """Dispatch setup failures are system replies even when they occur on a team bot."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(display_name="GeneralAgent", rooms=["!test:localhost"]),
                },
                teams={
                    "team_bot": TeamConfig(
                        display_name="Team Bot",
                        role="Coordinate work",
                        agents=["general"],
                        rooms=["!test:localhost"],
                    ),
                },
                models={"default": ModelConfig(provider="test", id="test-model")},
                authorization=AuthorizationConfig(default_room_access=True),
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        team_user = AgentMatrixUser(
            agent_name="team_bot",
            user_id="@mindroom_team_bot:localhost",
            display_name="Team Bot",
            password=TEST_PASSWORD,
        )
        bot = TeamBot(
            team_user,
            tmp_path,
            config=config,
            runtime_paths=runtime_paths,
            team_mode="coordinate",
        )
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        bot._delivery_gateway.send_text = AsyncMock(return_value="$team-error")
        _replace_turn_policy_deps(bot, delivery_gateway=bot._delivery_gateway)

        await bot._turn_controller._finalize_dispatch_failure(
            target=MessageTarget.resolve("!test:localhost", "$thread_root", "$event"),
            error=RuntimeError("boom"),
        )

        assert bot._delivery_gateway.send_text.await_args.args == (
            SendTextRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread_root", "$event"),
                response_text="[team_bot] ⚠️ Error: boom",
                extra_content={STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED},
            ),
        )

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_edits_early_placeholder_on_setup_failure(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Dispatch setup failures should replace and track the early placeholder."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        bot.logger = MagicMock()
        _replace_turn_policy_deps(bot, logger=bot.logger)

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=False,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
                requires_model_history_refresh=False,
            ),
            target=(
                dispatch_target := MessageTarget.resolve(
                    room_id=room.room_id,
                    thread_id=None,
                    reply_to_event_id=event.event_id,
                )
            ),
            correlation_id="corr-payload-error-1",
            envelope=_hook_envelope(body="hello", source_event_id="$event", target=dispatch_target),
        )

        failure_message = "setup failed"

        mock_edit = AsyncMock(return_value=True)
        install_edit_message_mock(bot, mock_edit)
        bot._delivery_gateway.send_text = AsyncMock(return_value="$error")
        _replace_turn_policy_deps(
            bot,
            delivery_gateway=bot._delivery_gateway,
        )

        with patch(
            "mindroom.response_runner.prepare_memory_and_model_context",
            side_effect=RuntimeError(failure_message),
        ):
            await bot._turn_controller._execute_response_action(
                room,
                event,
                dispatch,
                ResponseAction(kind="individual"),
                DispatchPayloadInputs((), (), ()),
                processing_log="processing",
                dispatch_started_at=0.0,
                handled_turn=TurnRecord.create([event.event_id]),
            )

        mock_edit.assert_awaited_once()
        bot._delivery_gateway.send_text.assert_awaited_once()
        tracker.record_handled_turn.assert_called_once_with(
            TurnRecord.create(
                ["$event"],
                response_event_id="$error",
            ),
        )

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_does_not_mark_responded_when_failure_cleanup_is_incomplete(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Incomplete placeholder cleanup should leave the source event retryable."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        bot.logger = MagicMock()
        _replace_turn_policy_deps(bot, logger=bot.logger)

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=False,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
                requires_model_history_refresh=False,
            ),
            target=(
                dispatch_target := MessageTarget.resolve(
                    room_id=room.room_id,
                    thread_id=None,
                    reply_to_event_id=event.event_id,
                )
            ),
            correlation_id="corr-payload-error-2",
            envelope=_hook_envelope(body="hello", source_event_id="$event", target=dispatch_target),
        )

        failure_message = "setup failed"

        with (
            patch.object(
                ResponsePayloadPreparer,
                "prepare",
                new=AsyncMock(side_effect=RuntimeError(failure_message)),
            ),
            patch(
                "mindroom.bot.TurnController._finalize_dispatch_failure",
                new=AsyncMock(
                    return_value=None,
                ),
            ),
        ):
            await bot._turn_controller._execute_response_action(
                room,
                event,
                dispatch,
                ResponseAction(kind="individual"),
                DispatchPayloadInputs((), (), ()),
                processing_log="processing",
                dispatch_started_at=0.0,
                handled_turn=TurnRecord.create([event.event_id]),
            )

        tracker.record_handled_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_handles_post_lock_request_preparation_error_without_unboundlocalerror(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Post-lock request preparation failures should degrade to a visible terminal error cleanly."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        bot.logger = MagicMock()
        _replace_turn_policy_deps(bot, logger=bot.logger)

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=False,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
                requires_model_history_refresh=False,
            ),
            target=(
                dispatch_target := MessageTarget.resolve(
                    room_id=room.room_id,
                    thread_id=None,
                    reply_to_event_id=event.event_id,
                )
            ),
            correlation_id="corr-post-lock-failure",
            envelope=_hook_envelope(body="hello", source_event_id="$event", target=dispatch_target),
        )

        async def fail_generate_response(*_args: object, **_kwargs: object) -> FinalDeliveryOutcome:
            message = "post-lock setup failed"
            error = RuntimeError(message)
            raise PostLockRequestPreparationError(message) from error

        replace_turn_controller_deps(
            bot,
            response_runner=SimpleNamespace(
                generate_response=AsyncMock(side_effect=fail_generate_response),
                generate_team_response_helper=AsyncMock(),
            ),
        )

        with patch(
            "mindroom.bot.TurnController._finalize_dispatch_failure",
            new=AsyncMock(
                return_value="$error",
            ),
        ):
            await bot._turn_controller._execute_response_action(
                room,
                event,
                dispatch,
                ResponseAction(kind="individual"),
                DispatchPayloadInputs((), (), ()),
                processing_log="processing",
                dispatch_started_at=0.0,
                handled_turn=TurnRecord.create([event.event_id]),
            )

        tracker.record_handled_turn.assert_called_once_with(
            TurnRecord.create(
                ["$event"],
                response_event_id="$error",
            ),
        )

    @pytest.mark.asyncio
    async def test_post_lock_failure_delivery_uses_stable_dispatch_target(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Post-lock failures should deliver to the same target as successful responses."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        delivery_gateway = SimpleNamespace(send_text=AsyncMock(return_value="$error"))
        _replace_turn_policy_deps(bot, logger=bot.logger)

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        stable_target = MessageTarget.resolve(
            room_id=room.room_id,
            thread_id=None,
            reply_to_event_id=event.event_id,
            thread_start_root_event_id=event.event_id,
        )
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=False,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
                requires_model_history_refresh=False,
            ),
            target=stable_target,
            correlation_id="corr-post-lock-target-failure",
            envelope=_hook_envelope(body="hello", source_event_id="$event", target=stable_target),
        )

        async def fail_generate_response(*_args: object, **_kwargs: object) -> FinalDeliveryOutcome:
            message = "post-lock setup failed"
            error = RuntimeError(message)
            raise PostLockRequestPreparationError(message) from error

        replace_turn_controller_deps(
            bot,
            delivery_gateway=delivery_gateway,
            response_runner=SimpleNamespace(
                generate_response=AsyncMock(side_effect=fail_generate_response),
                generate_team_response_helper=AsyncMock(),
            ),
        )

        await bot._turn_controller._execute_response_action(
            room,
            event,
            dispatch,
            ResponseAction(kind="individual"),
            DispatchPayloadInputs((), (), ()),
            processing_log="processing",
            dispatch_started_at=0.0,
            handled_turn=TurnRecord.create([event.event_id]),
        )

        delivery_gateway.send_text.assert_awaited_once()
        request = delivery_gateway.send_text.await_args.args[0]
        assert request.target == stable_target

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_records_visible_linkage_when_suppressed_cleanup_fails(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Suppressed placeholder cleanup failures should still persist visible linkage."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        bot.logger = MagicMock()
        wrap_extracted_collaborators(bot, "_response_runner")
        replace_turn_controller_deps(
            bot,
            logger=bot.logger,
            response_runner=bot._response_runner,
        )

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=True,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[bot.matrix_id],
                has_non_agent_mentions=False,
                requires_model_history_refresh=False,
            ),
            target=(
                dispatch_target := MessageTarget.resolve(
                    room_id=room.room_id,
                    thread_id=None,
                    reply_to_event_id=event.event_id,
                )
            ),
            correlation_id="corr-suppress-cleanup-failed",
            envelope=_hook_envelope(body="hello", source_event_id="$event", target=dispatch_target),
        )

        with (
            patch.object(
                bot._response_runner,
                "generate_response",
                new=AsyncMock(
                    return_value="$thinking",
                ),
            ),
            patch.object(ResponsePayloadPreparer, "_log_dispatch_latency"),
        ):
            await bot._turn_controller._execute_response_action(
                room,
                event,
                dispatch,
                ResponseAction(kind="individual"),
                DispatchPayloadInputs((), (), ()),
                processing_log="processing",
                dispatch_started_at=0.0,
                handled_turn=TurnRecord.create([event.event_id]),
            )

        tracker.record_handled_turn.assert_called_once_with(
            TurnRecord.create(
                ["$event"],
                response_event_id="$thinking",
            ),
        )

    @pytest.mark.asyncio
    async def test_deliver_final_suppression_preserves_existing_visible_response_linkage(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Suppressing a reused visible response must keep that prior event visible without remarking success."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        gateway = replace_delivery_gateway_deps(
            bot,
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(
                    return_value=SimpleNamespace(
                        response_text="ignored",
                        response_kind="ai",
                        tool_trace=None,
                        extra_content=None,
                        envelope=response_envelope,
                        suppress=True,
                    ),
                ),
                emit_after_response=AsyncMock(),
                emit_cancelled_response=AsyncMock(),
            ),
        )

        outcome = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                existing_event_id="$existing",
                existing_event_is_placeholder=False,
                response_text="Updated answer",
                identity=ResponseIdentity(
                    response_kind="ai",
                    response_envelope=response_envelope,
                    correlation_id="corr-deliver-suppress-existing",
                ),
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert outcome.terminal_status == "cancelled"
        assert outcome.suppressed is True
        assert _visible_response_event_id(outcome) == "$existing"
        assert _handled_response_event_id(outcome) is None
        assert outcome.mark_handled is False
        gateway.deps.response_hooks.emit_cancelled_response.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deliver_final_failed_existing_visible_edit_preserves_prior_response(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Failed edits of an existing visible response must keep the prior event visible but retryable."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        gateway = replace_delivery_gateway_deps(
            bot,
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(
                    return_value=SimpleNamespace(
                        response_text="Updated answer",
                        response_kind="ai",
                        tool_trace=None,
                        extra_content=None,
                        envelope=response_envelope,
                        suppress=False,
                    ),
                ),
                emit_after_response=AsyncMock(),
                emit_cancelled_response=AsyncMock(),
            ),
        )
        with patch("mindroom.delivery_gateway.edit_message_result", new=AsyncMock(return_value=None)):
            outcome = await gateway.deliver_final(
                FinalDeliveryRequest(
                    target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                    existing_event_id="$existing",
                    existing_event_is_placeholder=False,
                    response_text="Updated answer",
                    identity=ResponseIdentity(
                        response_kind="ai",
                        response_envelope=response_envelope,
                        correlation_id="corr-deliver-existing-failure",
                    ),
                    tool_trace=None,
                    extra_content=None,
                ),
            )

        assert outcome.terminal_status == "error"
        assert _visible_response_event_id(outcome) == "$existing"
        assert _handled_response_event_id(outcome) == "$existing"
        assert outcome.mark_handled is True
        gateway.deps.response_hooks.emit_cancelled_response.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deliver_final_before_response_exception_cleans_placeholder(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A before-response crash must clean up a visible placeholder instead of leaving it behind."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        gateway = replace_delivery_gateway_deps(
            bot,
            redact_message_event=AsyncMock(return_value=True),
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(side_effect=RuntimeError("hook boom")),
                emit_after_response=AsyncMock(),
                emit_cancelled_response=AsyncMock(),
            ),
        )

        outcome = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                existing_event_id="$thinking",
                existing_event_is_placeholder=True,
                response_text="Updated answer",
                identity=ResponseIdentity(
                    response_kind="ai",
                    response_envelope=response_envelope,
                    correlation_id="corr-deliver-before-hook-crash",
                ),
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert outcome.terminal_status == "error"
        gateway.deps.redact_message_event.assert_awaited_once_with(
            room_id="!test:localhost",
            event_id="$thinking",
            reason="Failed placeholder response before delivery",
        )
        gateway.deps.response_hooks.emit_cancelled_response.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deliver_final_before_response_cancellation_cleans_placeholder(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A cancelled before-response hook must redact the placeholder and propagate cancellation."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        response_envelope = _hook_envelope(body="hello", source_event_id="$event123")
        gateway = replace_delivery_gateway_deps(
            bot,
            redact_message_event=AsyncMock(return_value=True),
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(side_effect=asyncio.CancelledError("hook cancelled")),
                emit_after_response=AsyncMock(),
                emit_cancelled_response=AsyncMock(),
            ),
        )

        with pytest.raises(asyncio.CancelledError, match="hook cancelled"):
            await gateway.deliver_final(
                FinalDeliveryRequest(
                    target=MessageTarget.resolve("!test:localhost", "$thread123", "$event123"),
                    existing_event_id="$thinking",
                    existing_event_is_placeholder=True,
                    response_text="Updated answer",
                    identity=ResponseIdentity(
                        response_kind="ai",
                        response_envelope=response_envelope,
                        correlation_id="corr-deliver-before-hook-cancel",
                    ),
                    tool_trace=None,
                    extra_content=None,
                ),
            )

        gateway.deps.redact_message_event.assert_awaited_once_with(
            room_id="!test:localhost",
            event_id="$thinking",
            reason="Cancelled placeholder response",
        )
        gateway.deps.response_hooks.emit_cancelled_response.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_does_not_mark_responded_when_generation_returns_no_final_event(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Retryable resolutions with no response identity must keep the source retryable."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        bot.logger = MagicMock()

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = MagicMock()
        event.event_id = "$event"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=True,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[bot.matrix_id],
                has_non_agent_mentions=False,
                requires_model_history_refresh=False,
            ),
            target=(
                dispatch_target := MessageTarget.resolve(
                    room_id=room.room_id,
                    thread_id=None,
                    reply_to_event_id=event.event_id,
                )
            ),
            correlation_id="corr-suppress-cleanup-complete",
            envelope=_hook_envelope(body="hello", source_event_id="$event", target=dispatch_target),
        )

        with (
            patch.object(
                bot._response_runner,
                "generate_response",
                new=AsyncMock(
                    return_value=None,
                ),
            ),
            patch.object(ResponsePayloadPreparer, "_log_dispatch_latency"),
        ):
            await bot._turn_controller._execute_response_action(
                room,
                event,
                dispatch,
                ResponseAction(kind="individual"),
                DispatchPayloadInputs((), (), ()),
                processing_log="processing",
                dispatch_started_at=0.0,
                handled_turn=TurnRecord.create([event.event_id]),
            )

        tracker.record_handled_turn.assert_not_called()
