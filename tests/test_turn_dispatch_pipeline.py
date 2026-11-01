"""Text-turn dispatch pipeline through the turn ingress and dispatch seams: history hydration, execute-dispatch, and failure paths."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.coalescing.batch import CoalescingKey, PendingEvent, RequesterCoalescingOwner, requester_coalescing_key
from mindroom.coalescing.gate import ReadyPendingEvent
from mindroom.coalescing.ingress_lanes import IngressRetryError
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import (
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    SOURCE_KIND_KEY,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_KEY,
    RuntimePaths,
)
from mindroom.conversation_resolver import MessageContext
from mindroom.delivery_gateway import (
    DeliveryGateway,
    SendTextRequest,
)
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.dispatch_handoff import PreparedIngress
from mindroom.dispatch_source import (
    EXTERNAL_TRIGGER_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    VOICE_SOURCE_KIND,
)
from mindroom.event_journal import ConversationPage
from mindroom.final_delivery import Completed, FinalDeliveryOutcome
from mindroom.hooks import MessageEnvelope
from mindroom.inbound_turn_normalizer import DispatchPayload, InboundTurnNormalizer, _VoiceNormalizationResult
from mindroom.matrix.client_delivery import MatrixDeliveryFailure, MatrixDeliveryFailureKind
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.conversation_hydration import HYDRATED_PROMPT_WINDOW_MESSAGES
from mindroom.matrix.conversation_reads import ConversationReader
from mindroom.matrix.message_builder import build_message_content
from mindroom.matrix.thread_history_result import ThreadHistoryResult, thread_history_result
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.reply_turn.locked_turn import LockedTurns
from mindroom.response_payload_preparation import DispatchPayloadInputs, ResponsePayloadPreparer
from mindroom.response_runner import ResponseRunner
from mindroom.response_sources import Owned
from mindroom.teams.formation import TeamIntent, TeamMemberStatus, TeamResolution, TeamResolutionMember
from mindroom.teams.spec import TeamMode
from mindroom.turn_policy import DispatchPlan, PreparedDispatch, ResponseAction
from mindroom.turn_record import SourceVisibleEcho, TurnRecord
from mindroom.turn_store import ClaimContended, TurnClaim
from mindroom.turns.dispatch import TurnDispatcher
from tests.access_schema_support import with_current_room_member_access
from tests.bot_helpers import (
    AgentBotTestBase,
    _hook_envelope,
    _make_matrix_client_mock,
    _matrix_room,
    _replace_turn_policy_deps,
    _room_audio_event,
    _room_image_event,
    _runtime_bound_config,
    _visible_message,
    _wrap_extracted_collaborators,
    assert_no_turn_answered,
    assert_turn_answered,
    make_mock_agent_user,
    make_test_agent_bot,
    unique_room_send_responses,
)
from tests.conftest import (
    TEST_PASSWORD,
    drain_coalescing,
    install_generate_response_mock,
    install_runtime_journal_support,
    install_send_response_mock,
    make_ingress_facts,
    message_origin,
    patch_response_runner_module,
    replace_turn_deps,
    runtime_paths_for,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)
from tests.identity_helpers import entity_ids
from tests.test_turn_store import _in_room
from tests.threading_helpers import seed_hydrated_conversation, seed_thread_history
from tests.turn_dispatch_helpers import dispatch_test_turn

if TYPE_CHECKING:
    from collections.abc import Awaitable, Sequence
    from pathlib import Path

    from mindroom.reply_turn.request import ResponseRequest


@pytest.fixture
def mock_agent_user() -> AgentMatrixUser:
    """Mock agent user for testing."""
    return make_mock_agent_user()


async def _first_of(signal: Awaitable[object], handler: asyncio.Task[None]) -> None:
    """Wait for an ingress signal, or re-raise the handler that will never send it.

    A stub deep in the dispatch path sets an event to say it was reached. If
    something before it raises instead, the handler task finishes and the event
    never fires, so waiting on the event alone hangs until the suite-wide
    timeout kills the test -- naming neither the error nor its cause.
    """
    signal_task = asyncio.ensure_future(signal)
    try:
        await asyncio.wait({signal_task, handler}, return_when=asyncio.FIRST_COMPLETED)
        if signal_task.done():
            return
        # Only the handler finished, so it did not reach the stub. Surface why.
        handler.result()
        msg = "dispatch returned before ingress was reached"
        raise AssertionError(msg)
    finally:
        signal_task.cancel()


class TestAgentBot(AgentBotTestBase):
    """Turns through a test bot's ingress, dispatch, and delivery collaborators."""

    @pytest.mark.asyncio
    async def test_voice_normalization_fallback_obeys_echo_publication_barrier(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A normalization failure must not let a responder fallback overtake the router echo."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        room = _matrix_room(user_ids=("@user:localhost",))
        voice_event = _room_audio_event(
            sender="@user:localhost",
            event_id="$voice-fallback-barrier",
            room_id=room.room_id,
        )
        target = MessageTarget.resolve(
            room_id=room.room_id,
            thread_id="$thread-root",
            reply_to_event_id=voice_event.event_id,
        )
        fallback_event = PreparedIngress(
            sender=voice_event.sender,
            event_id=voice_event.event_id,
            body="🎤 [Attached voice message]",
            source=voice_event.source,
        )
        with (
            patch.object(
                InboundTurnNormalizer,
                "prepare_voice_event",
                new=AsyncMock(side_effect=RuntimeError("normalization failed")),
            ),
            patch.object(
                InboundTurnNormalizer,
                "prepare_raw_voice_fallback_event",
                new=AsyncMock(return_value=_VoiceNormalizationResult(event=fallback_event)),
            ),
            patch.object(
                bot._visible_voice_echo,
                "await_publication",
                new=AsyncMock(return_value=False),
            ) as await_publication,
            pytest.raises(IngressRetryError),
        ):
            await bot._voice_ingress._ready_voice_event(
                room=room,
                event=voice_event,
                facts=make_ingress_facts(voice_event.sender, source_kind=VOICE_SOURCE_KIND),
                voice_target=target,
                coalescing_thread_id="$thread-root",
                dispatch_timing=None,
            )

        await_publication.assert_awaited_once_with(
            room=room,
            source_event_id=voice_event.event_id,
            requester_user_id=voice_event.sender,
        )
        checkpoint = await bot._turn_store.prepared_voice_for_source(voice_event.event_id)
        assert checkpoint is not None
        assert checkpoint.body == "🎤 [Attached voice message]"
        assert checkpoint.coalescing_thread_id == "$thread-root"

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
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
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
        turn_store = unwrap_extracted_collaborator(bot._turn_store)
        handled_turn = await turn_store.record_pending_turn(
            _in_room(TurnRecord.create([event.event_id], completed=False), room.room_id),
        )
        assert handled_turn is not None
        dispatcher = bot._turn_dispatcher
        claim = await turn_store.claim(handled_turn.record)
        assert isinstance(claim, TurnClaim)
        send_started = asyncio.Event()
        release_send = asyncio.Event()

        bot.client = AsyncMock(spec=nio.AsyncClient)

        async def send_rejection(_request: SendTextRequest) -> str:
            send_started.set()
            await release_send.wait()
            return "$reply"

        with patch.object(DeliveryGateway, "send_text", new=AsyncMock(side_effect=send_rejection)) as send_text:
            response_task = bot._response_runner.inbox.track_inbox_response(
                dispatcher._execute_response_action(
                    room,
                    event,
                    dispatch,
                    action,
                    DispatchPayloadInputs((), (), ()),
                    dispatch_started_at=0.0,
                    handled_turn=handled_turn.record,
                ),
                name="inbox_response:$event",
                room_id=room.room_id,
                recovery_proof_ready=lambda: True,
                claim=claim,
            )
            await send_started.wait()
            assert turn_store.has_live_turn_claim(event.event_id), "the response owns its turn while it delivers"
            release_send.set()
            await response_task
            await asyncio.sleep(0)
            assert not turn_store.has_live_turn_claim(event.event_id)

        send_text.assert_awaited_once()
        delivered_request = send_text.await_args.args[0]
        assert delivered_request.response_text.endswith(
            "private agents are only supported in explicit Matrix ad hoc teams with requester identity",
        )
        answered = await turn_store.get_turn_record("$event")
        assert answered is not None
        assert answered.completed
        assert answered.response_event_id == "$reply"

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
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
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
        handled_turn = await unwrap_extracted_collaborator(bot._turn_store).record_pending_turn(
            _in_room(TurnRecord.create([event.event_id], completed=False), room.room_id),
        )
        assert handled_turn is not None
        bot.client = AsyncMock(spec=nio.AsyncClient)
        bot.client.device_id = "DEVICE"
        bot.client.olm = None
        cached_room = MagicMock()
        cached_room.encrypted = False
        bot.client.rooms = {room.room_id: cached_room}

        with (
            patch(
                "mindroom.delivery_gateway.send_message_outcome",
                new=AsyncMock(
                    return_value=MatrixDeliveryFailure(
                        MatrixDeliveryFailureKind.SEND_EXCEPTION,
                        "test delivery failure",
                    ),
                ),
            ),
            pytest.raises(ValueError, match="requires a visible Matrix response event ID"),
        ):
            await bot._turn_dispatcher._execute_response_action(
                room,
                event,
                dispatch,
                action,
                DispatchPayloadInputs((), (), ()),
                dispatch_started_at=0.0,
                handled_turn=handled_turn.record,
            )

        await assert_no_turn_answered(bot)

    @pytest.mark.asyncio
    async def test_extract_dispatch_context_uses_bounded_full_thread_history(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Dispatch startup should use the bounded full-history read."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
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
        await seed_thread_history(
            bot,
            room_id=room.room_id,
            thread_id="$thread_root",
            messages=[
                ResolvedVisibleMessage.synthetic(
                    sender="@user:localhost",
                    body="Root",
                    event_id="$thread_root",
                    timestamp=1234567889,
                    content={"body": "Root"},
                ),
            ],
        )

        observed_limits: list[int] = []
        real_read = ConversationReader.read

        async def spy_read(reader: ConversationReader, **kwargs: object) -> ConversationPage:
            observed_limits.append(kwargs["limit"])
            return await real_read(reader, **kwargs)

        with (
            patch.object(ConversationReader, "read", new=spy_read),
            # Dispatch runs before the turn is accepted, so it must never block
            # on the homeserver to build its planning context.
            patch.object(
                ConversationReader,
                "read_strict",
                new=AsyncMock(side_effect=AssertionError("dispatch blocked on a strict read")),
            ),
        ):
            context = await bot._conversation_resolver.extract_dispatch_context(room, event, thread_id="$thread_root")

        assert context.is_thread is True
        assert context.thread_id == "$thread_root"
        assert [message.event_id for message in context.thread_history] == ["$thread_root"]
        # The conversation was hydrated, so a non-blocking read still proves
        # the history whole and no post-lock refresh is owed.
        assert context.requires_model_history_refresh is False
        # Every read the dispatch path takes is bounded by the hydration window.
        assert observed_limits
        assert set(observed_limits) == {HYDRATED_PROMPT_WINDOW_MESSAGES}

    @pytest.mark.asyncio
    async def test_extract_dispatch_context_fetches_direct_thread_history_through_dispatch_fetcher(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Direct-thread dispatch context should read bounded full history from the projection."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        install_runtime_journal_support(bot)
        bot.client = _make_matrix_client_mock()
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
        seeded = [
            ResolvedVisibleMessage.synthetic(
                sender="@user:localhost",
                body="Root",
                event_id="$thread_root",
                timestamp=1234567889,
                content={"body": "Root"},
                thread_id="$thread_root",
            ),
            ResolvedVisibleMessage.synthetic(
                sender="@mindroom_calculator:localhost",
                body="Reply",
                event_id="$reply",
                timestamp=1234567890,
                content={"body": "Reply"},
                thread_id="$thread_root",
            ),
        ]
        await seed_thread_history(
            bot,
            room_id=room.room_id,
            thread_id="$thread_root",
            messages=seeded,
        )
        dispatch_history = thread_history_result(seeded, is_full_history=True)

        context = await bot._conversation_resolver.extract_dispatch_context(room, event, thread_id="$thread_root")

        assert context.is_thread is True
        assert context.thread_id == "$thread_root"
        assert context.thread_history == dispatch_history
        assert context.requires_model_history_refresh is False

    @pytest.mark.asyncio
    async def test_dispatch_text_message_prepares_full_history_payload_after_lock_when_required(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Planning should hide partial history while payload preparation refreshes it."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = _make_matrix_client_mock()
        room = nio.MatrixRoom("!test:localhost", bot.matrix_id.full_id)
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

        async def fake_plan(*_args: object, **_kwargs: object) -> DispatchPlan:
            call_order.append("action")
            assert list(dispatch.context.thread_history) == snapshot_history
            assert dispatch.context.planning_thread_history == ()
            assert dispatch.context.planning_thread_history_unavailable is True
            return DispatchPlan(
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
            timezone: str,
            model_prompt: str | None = None,
        ) -> tuple[str, Sequence[ResolvedVisibleMessage], str | None, Sequence[ResolvedVisibleMessage]]:
            del config, runtime_paths, timezone
            return prompt, thread_history, model_prompt, thread_history

        with (
            patch.object(
                bot._turn_dispatcher,
                "_prepare_dispatch",
                new=AsyncMock(return_value=dispatch),
            ),
            patch.object(bot._turn_policy, "plan_turn", new=AsyncMock(side_effect=fake_plan)),
            patch.object(
                LockedTurns,
                "refresh_model_history",
                new=AsyncMock(side_effect=refresh_thread_history),
            ) as mock_refresh_thread_history,
            patch.object(
                bot._inbound_turn_normalizer,
                "build_dispatch_payload_with_attachments",
                new=AsyncMock(side_effect=fake_build_payload),
            ) as mock_build_payload,
            patch.object(
                ResponseRunner,
                "_deliver_turn",
                new=AsyncMock(
                    return_value=FinalDeliveryOutcome(
                        ending=Completed(),
                        event_id="$response",
                        is_visible_response=True,
                        final_visible_body="ok",
                    ),
                ),
            ) as mock_deliver,
            patch.object(
                LockedTurns,
                "_run_cancellable_response",
                new=AsyncMock(side_effect=run_cancellable_response),
            ),
            patch.object(ResponsePayloadPreparer, "_log_dispatch_latency"),
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=False),
                prepare_memory_and_model_context=prepare_memory_and_model_context,
                apply_post_response_effects=AsyncMock(),
            ),
        ):
            await dispatch_test_turn(
                bot._turn_dispatcher,
                room,
                event,
                facts=bot._ingress_validator.classify(event),
            )
            await drain_coalescing(bot)

        mock_refresh_thread_history.assert_awaited_once()
        mock_build_payload.assert_awaited_once()
        delivered_turn = mock_deliver.await_args.args[0]
        assert list(delivered_turn.request.thread_history) == full_history
        assert delivered_turn.request.attachment_ids == ("att_older",)
        assert call_order == ["action", "payload", "generate"]

    @pytest.mark.asyncio
    async def test_dispatch_text_message_skip_path_does_not_hydrate_full_history_before_planning(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Planning should use policy-grade history only and skip model refresh on ignore."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
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
        ) -> DispatchPlan:
            assert list(dispatch.context.thread_history) == []
            assert dispatch.context.planning_thread_history == ()
            assert dispatch.context.requires_model_history_refresh is True
            return DispatchPlan(kind="ignore")

        with (
            patch.object(
                bot._turn_dispatcher,
                "_prepare_dispatch",
                new=AsyncMock(return_value=dispatch),
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
            patch.object(bot._turn_dispatcher, "_execute_response_action", new=AsyncMock()) as mock_execute,
        ):
            await dispatch_test_turn(
                bot._turn_dispatcher,
                room,
                event,
                facts=bot._ingress_validator.classify(event),
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
        bot = make_test_agent_bot(agent_user, config=config, runtime_paths=runtime_paths_for(config))
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
                bot._turn_dispatcher,
                "_prepare_dispatch",
                new=AsyncMock(return_value=dispatch),
            ),
            patch.object(
                bot._command_turn_executor,
                "execute_if_owned",
                new=AsyncMock(return_value=True),
            ) as mock_execute_command,
        ):
            await dispatch_test_turn(
                bot._turn_dispatcher,
                room,
                event,
                facts=bot._ingress_validator.classify(event),
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
        bot = make_test_agent_bot(agent_user, config=config, runtime_paths=runtime_paths_for(config))
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
        empty_page = ConversationPage(messages=(), refresh_pending=(), next_cursor=None)
        # A snapshot read is allowed to skip the homeserver only when hydration
        # already proved what the thread holds. Leaving the journal empty would
        # make this pass because nothing was known rather than because the
        # command declined to wait.
        await seed_hydrated_conversation(bot, room_id=room.room_id, thread_id="$thread_root")

        with (
            patch.object(
                bot._inbound_turn_normalizer,
                "resolve_text_event",
                new=AsyncMock(return_value=event),
            ),
            # A command runs before the turn is accepted, so it must take the
            # non-blocking snapshot read and never wait on the homeserver.
            patch.object(
                ConversationReader,
                "read_strict",
                new=AsyncMock(side_effect=AssertionError("command blocked on a strict read")),
            ) as mock_full_history,
            patch.object(
                ConversationReader,
                "read",
                new=AsyncMock(return_value=empty_page),
            ) as mock_snapshot,
            patch.object(
                bot._command_turn_executor,
                "execute_if_owned",
                new=AsyncMock(return_value=True),
            ) as mock_execute_command,
        ):
            await dispatch_test_turn(
                bot._turn_dispatcher,
                room,
                event,
                facts=bot._ingress_validator.classify(event),
            )

        mock_full_history.assert_not_awaited()
        mock_snapshot.assert_awaited_once_with(
            room_id=room.room_id,
            thread_id="$thread_root",
            limit=HYDRATED_PROMPT_WINDOW_MESSAGES,
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
        bot = make_test_agent_bot(agent_user, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = _make_matrix_client_mock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        # The router published and finalized the voice source's echo before the batch reached its ignore plan.
        await bot._turn_store.record_visible_echo(room.room_id, "$voice", "$voice_echo")
        await bot._turn_store.record_finalized_visible_echo("$voice", "$voice_echo", is_fallback=False)
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
                bot._turn_dispatcher,
                "_prepare_dispatch",
                new=AsyncMock(return_value=dispatch),
            ),
            patch.object(bot._turn_dispatcher, "_has_newer_unresponded_in_thread", return_value=False),
        ):
            await dispatch_test_turn(
                bot._turn_dispatcher,
                room,
                event,
                facts=bot._ingress_validator.classify(event),
                handled_turn=TurnRecord.create(
                    ["$voice", "$text"],
                    source_event_prompts={"$voice": "voice prompt", "$text": "text prompt"},
                ),
            )

        await assert_turn_answered(
            bot,
            TurnRecord.create(
                ["$voice", "$text"],
                response_event_id="$voice_echo",
                source_event_prompts={"$voice": "voice prompt", "$text": "text prompt"},
                visible_echoes={"$voice": SourceVisibleEcho("$voice_echo", is_fallback=False)},
                requester_id="@user:localhost",
            ),
        )

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
        bot = make_test_agent_bot(agent_user, config=config, runtime_paths=runtime_paths)
        _wrap_extracted_collaborators(bot)
        bot.client = _make_matrix_client_mock()

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
            _deps: object,
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
            scheduled_prompt: str | None = None,
        ) -> None:
            assert message == "hello"
            assert requester_user_id == "@user:localhost"
            assert extra_content is None
            assert media_events is None
            assert scheduled_prompt is None
            assert handled_turn is not None
            assert handled_turn.source_event_prompts == {"$voice": "voice prompt", "$text": "hello"}
            await bot._turn_store.record_responded_turn(replace(handled_turn, response_event_id="$route"))

        with (
            patch.object(bot._inbound_turn_normalizer, "resolve_text_event", new=AsyncMock(return_value=event)),
            patch.object(
                bot._turn_dispatcher,
                "_prepare_dispatch",
                new=AsyncMock(return_value=dispatch),
            ),
            patch.object(
                bot._turn_policy,
                "plan_turn",
                new=AsyncMock(
                    return_value=DispatchPlan(
                        kind="route",
                        router_message="hello",
                        router_event=event,
                    ),
                ),
            ),
            patch(
                "mindroom.turns.dispatch.execute_router_relay",
                new=AsyncMock(side_effect=fake_execute_router_relay),
            ),
            patch.object(bot._turn_dispatcher, "_has_newer_unresponded_in_thread", return_value=False),
        ):
            await dispatch_test_turn(
                bot._turn_dispatcher,
                room,
                event,
                facts=bot._ingress_validator.classify(event),
                handled_turn=coalesced_turn,
            )

        await assert_turn_answered(
            bot,
            replace(
                TurnRecord.create(
                    ["$voice", "$text"],
                    response_event_id="$route",
                    source_event_prompts={"$voice": "voice prompt", "$text": "hello"},
                ),
                response_owner="router",
                requester_id="@user:localhost",
                history_scope=None,
                conversation_target=dispatch.target,
            ),
        )

    @pytest.mark.asyncio
    async def test_trusted_internal_router_relays_use_gate_bypass(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Agent-authored relays should enter the gate as FIFO bypass events."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
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
            patch.object(TurnDispatcher, "dispatch_turn", new=AsyncMock()) as mock_dispatch,
            patch.object(bot._coalescing_gate, "admit", new=AsyncMock()) as mock_admit,
        ):
            ticket = bot._turn_ingress.reserve_prompt_ingress_order(room, "@user:localhost")
            await bot._turn_ingress._enqueue_for_dispatch(
                PreparedIngress(
                    sender=event.sender,
                    event_id=event.event_id,
                    body=event.body,
                    source=event.source,
                    server_timestamp=event.server_timestamp,
                ),
                room,
                facts=bot._ingress_validator.classify(event),
                ticket=ticket,
                coalescing_key=requester_coalescing_key(room.room_id, None, "@user:localhost"),
            )
            await asyncio.wait_for(ticket.slot.settled.wait(), timeout=1.0)

        mock_dispatch.assert_not_awaited()
        mock_admit.assert_awaited_once()
        key = mock_admit.await_args.args[0]
        ready_result = mock_admit.await_args.kwargs["ready_result"]
        assert isinstance(ready_result, ReadyPendingEvent)
        pending_event = ready_result.pending_event
        assert key == CoalescingKey(room.room_id, None, RequesterCoalescingOwner("@user:localhost"))
        assert isinstance(pending_event, PendingEvent)
        assert isinstance(pending_event.event, PreparedIngress)
        assert pending_event.event.event_id == event.event_id
        assert pending_event.event.body == event.body
        assert pending_event.facts.source_kind == TRUSTED_INTERNAL_RELAY_SOURCE_KIND

    @staticmethod
    def _router_relay_event(
        *,
        sender: str = "@mindroom_router:localhost",
        reply_to: str | None = "$user_msg:localhost",
        is_falling_back: bool = False,
        body: str = "@mindroom_calculator:localhost could you help with this?",
    ) -> nio.RoomMessageText:
        content: dict[str, Any] = {
            "msgtype": "m.text",
            "body": body,
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
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
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
                bot._turn_dispatcher,
                "_prepare_dispatch",
                new=AsyncMock(return_value=None),
            ) as mock_prepare,
        ):
            await dispatch_test_turn(
                bot._turn_dispatcher,
                room,
                event,
                facts=bot._ingress_validator.classify(event),
            )

        handled_turn = mock_prepare.await_args.args[0].handled_turn
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
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        validator = bot._ingress_validator

        explicit_relay = self._router_relay_event()
        fallback_relay = self._router_relay_event(reply_to="$latest:localhost", is_falling_back=True)
        replyless_relay = self._router_relay_event(reply_to=None)
        non_router_relay = self._router_relay_event(sender="@mindroom_general:localhost")

        assert validator.classify(explicit_relay).discovery_event_id == "$user_msg:localhost"
        assert validator.classify(fallback_relay).discovery_event_id is None
        assert validator.classify(replyless_relay).discovery_event_id is None
        assert validator.classify(non_router_relay).discovery_event_id is None

    def test_room_level_router_relay_names_its_original_event(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A room-mode handoff, which clients do not show as a reply, still aliases the human event it relays."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        content = build_message_content(
            "@mindroom_calculator:localhost could you help with this?",
            reply_to_event_id="$user_msg:localhost",
            extra_content={
                ORIGINAL_SENDER_KEY: "@user:localhost",
                SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
            },
        )
        relay = nio.RoomMessageText.from_dict(
            {
                "event_id": "$relay:localhost",
                "sender": "@mindroom_router:localhost",
                "origin_server_ts": 1234567890,
                "content": content,
            },
        )

        assert bot._ingress_validator.classify(relay).discovery_event_id == "$user_msg:localhost"

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
                        access={"users": ["@owner:localhost"]},
                    ),
                },
                models={"default": ModelConfig(provider="openai", id="test-model")},
                administrators=["@owner:localhost"],
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        ids = entity_ids(config, runtime_paths)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths)
        bot.client = _make_matrix_client_mock()
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
                        access={"users": ["@mallory:localhost", "@victim:localhost"]},
                    ),
                },
                models={"default": ModelConfig(provider="openai", id="test-model")},
                administrators=["@mallory:localhost", "@victim:localhost"],
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        ids = entity_ids(config, runtime_paths)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths)
        bot.client = _make_matrix_client_mock()
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

        await bot._on_message(room, event)
        await drain_coalescing(bot)

        generate_response.assert_awaited_once()
        generate_kwargs = generate_response.await_args.kwargs
        assert generate_kwargs["user_id"] == "@mallory:localhost"
        assert generate_kwargs["response_envelope"].requester_id == "@mallory:localhost"

    @pytest.mark.asyncio
    async def test_text_ingress_claims_source_before_first_async_preparation(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """An edit must see source ownership before normal ingress can yield."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.members_synced = True
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$source",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "before edit"},
            },
        )
        ingress_started = asyncio.Event()
        release_ingress = asyncio.Event()

        async def hold_ingress(*_args: object, **_kwargs: object) -> None:
            ingress_started.set()
            await release_ingress.wait()

        with (
            patch.object(
                bot._turn_ingress,
                "_precheck_dispatch_event",
                return_value=True,
            ),
            patch.object(bot._turn_ingress, "_ingest_live_text_event", side_effect=hold_ingress),
        ):
            task = asyncio.create_task(bot._on_message(room, event))
            await _first_of(ingress_started.wait(), task)
            competing_claim = TurnRecord.create([event.event_id], completed=False)
            assert isinstance(await bot._turn_store.claim(competing_claim), ClaimContended)
            release_ingress.set()
            await task

        claim = await bot._turn_store.claim(competing_claim)
        assert isinstance(claim, TurnClaim)
        claim.release()

    @pytest.mark.asyncio
    async def test_router_relay_marks_original_alias_unsettled_before_first_async_preparation(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """An edit of a routed original must see its relay claim before ingress can yield."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.members_synced = True
        event = self._router_relay_event()
        ingress_started = asyncio.Event()
        release_ingress = asyncio.Event()

        async def hold_ingress(*_args: object, **_kwargs: object) -> None:
            ingress_started.set()
            await release_ingress.wait()

        with (
            patch.object(
                bot._turn_ingress,
                "_precheck_dispatch_event",
                return_value=True,
            ),
            patch.object(bot._turn_ingress, "_ingest_live_text_event", side_effect=hold_ingress),
        ):
            task = asyncio.create_task(bot._on_message(room, event))
            # Not a bare ``await ingress_started.wait()``. The event is set by
            # the ingress stub, so anything that raises before ingress leaves
            # this waiting forever on a task that already finished, and the
            # real error is never read. Racing the two surfaces it instead.
            await _first_of(ingress_started.wait(), task)
            assert bot._turn_store.has_live_turn_claim("$user_msg:localhost")
            release_ingress.set()
            await task

        assert not bot._turn_store.has_live_turn_claim("$user_msg:localhost")

    @pytest.mark.asyncio
    async def test_handle_message_inner_enqueues_active_thread_follow_up_as_coalescible_gate_event(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Human follow-ups in an active thread must keep policy while remaining coalescible."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.members_synced = True
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
        prepared_event = PreparedIngress(
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
                bot._turn_ingress,
                "_precheck_dispatch_event",
                return_value=True,
            ),
            patch(
                "mindroom.inbound_turn_normalizer.InboundTurnNormalizer.resolve_text_event",
                new=AsyncMock(return_value=prepared_event),
            ),
            patch(
                "mindroom.conversation_resolver.ConversationResolver.build_envelope",
                return_value=envelope,
            ),
            # The gate reads which conversations have a response running from the runner it was built with.
            patch.object(
                bot._coalescing_gate,
                "_active_thread_ids_for_room",
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
                TurnDispatcher,
                "dispatch_turn",
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
        assert key == CoalescingKey(room.room_id, "$thread_root", RequesterCoalescingOwner("@user:localhost"))
        assert isinstance(pending_event, PendingEvent)
        assert pending_event.facts.run_requester_id == "@user:localhost"
        assert isinstance(pending_event.event, PreparedIngress)
        assert pending_event.event.event_id == event.event_id
        assert pending_event.event.body == event.body
        assert pending_event.facts.source_kind == MESSAGE_SOURCE_KIND
        assert pending_event.event.active_follow_up is False
        assert pending_event.claim is not None
        assert pending_event.claim.source_event_ids == {event.event_id}
        assert pending_event.notice is mock_reserve_waiting_human_message.return_value

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mention_kind", ["other_agent", "joined_human"])
    @pytest.mark.parametrize("relay_sender", ["router", "self"])
    async def test_relay_for_another_target_does_not_signal_active_response(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        *,
        mention_kind: str,
        relay_sender: str,
    ) -> None:
        """A router handoff or this agent's own relay addressed elsewhere must not interrupt its active turn."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        ids = entity_ids(config, runtime_paths)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths)
        replace_turn_deps(bot, runtime=replace(bot._runtime_view, client=_make_matrix_client_mock()))
        room = nio.MatrixRoom("!room:localhost", bot.matrix_id.full_id)
        room.add_member(bot.matrix_id.full_id, "Calculator", None)
        room.add_member("@user:localhost", "User", None)
        if mention_kind == "other_agent":
            body = "@general could you help with this?"
            mentioned_user_id = ids["general"].full_id
        else:
            body = "@person:localhost could you help with this?"
            mentioned_user_id = "@person:localhost"
            room.add_member(mentioned_user_id, "Person", None)
        event = (
            self._router_relay_event(body=body)
            if relay_sender == "router"
            else self._router_relay_event(sender=bot.matrix_id.full_id, body=body)
        )
        event.source["content"]["m.mentions"] = {"user_ids": [mentioned_user_id]}
        prepared_event = PreparedIngress(
            sender=event.sender,
            event_id=event.event_id,
            body=event.body,
            source=event.source,
            server_timestamp=event.server_timestamp,
        )

        with (
            patch.object(bot._response_runner, "has_active_response_for_target", return_value=True),
            patch.object(
                bot._response_runner,
                "reserve_waiting_human_message",
                return_value=MagicMock(),
            ) as reserve_waiting_human_message,
            patch.object(bot._coalescing_gate, "admit", new=AsyncMock()) as admit,
        ):
            ticket = bot._turn_ingress.reserve_prompt_ingress_order(room, "@user:localhost")
            outcome = await bot._turn_ingress._dispatch_prepared_text_like_ingress(
                room=room,
                prepared_event=prepared_event,
                facts=bot._ingress_validator.classify(prepared_event),
                ticket=ticket,
                coalescing_thread_id="$thread_root:localhost",
            )
            await asyncio.wait_for(ticket.slot.settled.wait(), timeout=1.0)

        assert outcome is TurnDispatchOutcome.DEFERRED
        reserve_waiting_human_message.assert_not_called()
        admit.assert_awaited_once()
        ready_result = admit.await_args.kwargs["ready_result"]
        assert isinstance(ready_result, ReadyPendingEvent)
        assert ready_result.pending_event.notice is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("target_kind", ["self", "none", "absent_human"])
    async def test_router_handoff_without_another_room_participant_signals_active_response(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        *,
        target_kind: str,
    ) -> None:
        """Only a handoff addressed to another joined participant suppresses the queued notice."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        ids = entity_ids(config, runtime_paths)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths)
        replace_turn_deps(bot, runtime=replace(bot._runtime_view, client=_make_matrix_client_mock()))
        room = nio.MatrixRoom("!room:localhost", bot.matrix_id.full_id)
        room.add_member(bot.matrix_id.full_id, "Calculator", None)
        room.add_member("@user:localhost", "User", None)
        # A synced cache is what makes "absent" mean absent rather than "not fetched yet".
        room.members_synced = True
        if target_kind == "self":
            body = "@calculator could you help with this?"
            mentioned_user_ids = [ids["calculator"].full_id]
        elif target_kind == "absent_human":
            body = "@person:localhost could you help with this?"
            mentioned_user_ids = ["@person:localhost"]
        else:
            body = "could you help with this?"
            mentioned_user_ids = []
        event = self._router_relay_event(body=body)
        if mentioned_user_ids:
            event.source["content"]["m.mentions"] = {"user_ids": mentioned_user_ids}
        prepared_event = PreparedIngress(
            sender=event.sender,
            event_id=event.event_id,
            body=event.body,
            source=event.source,
            server_timestamp=event.server_timestamp,
        )

        with (
            patch.object(bot._response_runner, "has_active_response_for_target", return_value=True),
            patch.object(
                bot._response_runner,
                "reserve_waiting_human_message",
                return_value=MagicMock(),
            ) as reserve_waiting_human_message,
            patch.object(bot._coalescing_gate, "admit", new=AsyncMock()) as admit,
        ):
            ticket = bot._turn_ingress.reserve_prompt_ingress_order(room, "@user:localhost")
            outcome = await bot._turn_ingress._dispatch_prepared_text_like_ingress(
                room=room,
                prepared_event=prepared_event,
                facts=bot._ingress_validator.classify(prepared_event),
                ticket=ticket,
                coalescing_thread_id="$thread_root:localhost",
            )
            await asyncio.wait_for(ticket.slot.settled.wait(), timeout=1.0)

        assert outcome is TurnDispatchOutcome.DEFERRED
        reserve_waiting_human_message.assert_called_once()
        admit.assert_awaited_once()
        ready_result = admit.await_args.kwargs["ready_result"]
        assert isinstance(ready_result, ReadyPendingEvent)
        assert ready_result.pending_event.notice is not None

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
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.members_synced = True
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
        prepared_event = PreparedIngress(
            sender="@mindroom_general:localhost",
            event_id=f"${source_kind}",
            body=f"@mindroom_calculator:localhost {source_kind} says hello",
            source=event.source,
            server_timestamp=1234567890,
        )

        with (
            patch.object(
                bot._turn_ingress,
                "_precheck_dispatch_event",
                return_value=True,
            ),
            patch(
                "mindroom.inbound_turn_normalizer.InboundTurnNormalizer.resolve_text_event",
                new=AsyncMock(return_value=prepared_event),
            ),
            patch.object(
                bot._conversation_resolver,
                "coalescing_thread_id",
                new=AsyncMock(return_value=None),
            ),
            patch.object(TurnDispatcher, "dispatch_turn", new=AsyncMock()) as mock_dispatch,
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
        assert isinstance(pending_event.event, PreparedIngress)
        assert pending_event.event.event_id == event.event_id
        assert pending_event.event.body == event.body
        assert pending_event.facts.source_kind == source_kind

    @pytest.mark.asyncio
    async def test_voice_preview_reserves_active_thread_follow_up(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Transcribed voice follow-ups should share the active-response notice path."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.users = {"@user:localhost": nio.MatrixUser("@user:localhost")}
        # The runtime's router is running, so voice readiness checks whether it is in the room to echo the voice.
        room.invited_users = {}
        room.members_synced = True
        voice_event = _room_audio_event(sender="@user:localhost", event_id="$voice-followup", room_id=room.room_id)
        voice_event.source["content"]["m.relates_to"] = {"rel_type": "m.thread", "event_id": "$thread_root"}
        prepared_event = PreparedIngress(
            sender="@user:localhost",
            event_id="$voice-followup",
            body="please stop",
            source={"content": {"msgtype": "m.text", "body": "please stop", SOURCE_KIND_KEY: "voice"}},
            server_timestamp=1234567890,
            payload_trusted=True,
        )

        with (
            patch(
                "mindroom.inbound_turn_normalizer.InboundTurnNormalizer.prepare_voice_event",
                new=AsyncMock(return_value=_VoiceNormalizationResult(event=prepared_event)),
            ),
            # The gate reads which conversations have a response running from the runner it was built with.
            patch.object(
                bot._coalescing_gate,
                "_active_thread_ids_for_room",
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
            ticket = bot._turn_ingress.reserve_prompt_ingress_order(room, "@user:localhost")
            assert await ticket.claim_turn(TurnRecord.create([voice_event.event_id], completed=False)) is None
            turn_claim = ticket.claim
            await bot._voice_ingress.admit(
                room,
                voice_event,
                bot._ingress_validator.classify(voice_event, source_kind=VOICE_SOURCE_KIND),
                dispatch_timing=None,
                ticket=ticket,
            )
            await asyncio.wait_for(ticket.slot.settled.wait(), timeout=1.0)
            mock_admit.assert_awaited_once()
            key = mock_admit.await_args.args[0]
            assert key == CoalescingKey(room.room_id, "$thread_root", RequesterCoalescingOwner("@user:localhost"))
            ready_event = mock_admit.await_args.kwargs["ready_result"]

        assert isinstance(ready_event, ReadyPendingEvent)
        mock_reserve_waiting_human_message.assert_called_once()
        reserved_target = mock_reserve_waiting_human_message.call_args.kwargs["target"]
        assert reserved_target.resolved_thread_id == "$thread_root"
        reserved_envelope = mock_reserve_waiting_human_message.call_args.kwargs["response_envelope"]
        assert reserved_envelope.source_kind == VOICE_SOURCE_KIND
        pending_event = ready_event.pending_event
        assert isinstance(pending_event, PendingEvent)
        assert pending_event.facts.run_requester_id == "@user:localhost"
        assert pending_event.event.event_id == prepared_event.event_id
        assert pending_event.event.body == prepared_event.body
        assert pending_event.facts.source_kind == VOICE_SOURCE_KIND
        assert pending_event.event.active_follow_up is False
        assert pending_event.notice is mock_reserve_waiting_human_message.return_value
        assert pending_event.claim is turn_claim, "the lane hands the voice claim to the ready event"
        pending_event.close()
        assert not bot._turn_store.has_live_turn_claim(voice_event.event_id)

    @pytest.mark.asyncio
    async def test_file_sidecar_text_preview_enqueues_prepared_text(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """File sidecar previews should hand prepared text to the gate, not dispatch inline."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
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
        prepared_event = PreparedIngress(
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
            patch.object(bot._conversation_resolver, "build_envelope", return_value=envelope),
            patch.object(TurnDispatcher, "dispatch_turn", new=AsyncMock()) as mock_dispatch,
            patch.object(bot._coalescing_gate, "admit", new=AsyncMock()) as mock_admit,
        ):
            ticket = bot._turn_ingress.reserve_prompt_ingress_order(room, "@user:localhost")
            handled = await bot._turn_ingress._dispatch_file_sidecar_text_preview(
                room,
                sidecar_event,
                bot._ingress_validator.classify(sidecar_event),
                ticket=ticket,
                coalescing_thread_id="$thread_root",
            )
            await asyncio.wait_for(ticket.slot.settled.wait(), timeout=1.0)

        assert handled is TurnDispatchOutcome.DEFERRED
        mock_dispatch.assert_not_awaited()
        mock_admit.assert_awaited_once()
        key = mock_admit.await_args.args[0]
        ready_result = mock_admit.await_args.kwargs["ready_result"]
        assert isinstance(ready_result, ReadyPendingEvent)
        pending_event = ready_result.pending_event
        assert key == CoalescingKey(room.room_id, "$thread_root", RequesterCoalescingOwner("@user:localhost"))
        assert isinstance(pending_event, PendingEvent)
        assert pending_event.facts.run_requester_id == "@user:localhost"
        assert pending_event.event.event_id == prepared_event.event_id
        assert pending_event.event.body == prepared_event.body
        assert pending_event.facts.source_kind == MESSAGE_SOURCE_KIND

    @pytest.mark.asyncio
    async def test_file_sidecar_text_preview_reserves_active_thread_follow_up(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Sidecar text follow-ups should share the active-response notice path."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
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
        prepared_event = PreparedIngress(
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
            patch.object(bot._conversation_resolver, "build_envelope", return_value=envelope),
            # The gate reads which conversations have a response running from the runner it was built with.
            patch.object(
                bot._coalescing_gate,
                "_active_thread_ids_for_room",
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
            patch.object(TurnDispatcher, "dispatch_turn", new=AsyncMock()) as mock_dispatch,
            patch.object(bot._coalescing_gate, "admit", new=AsyncMock()) as mock_admit,
        ):
            ticket = bot._turn_ingress.reserve_prompt_ingress_order(room, "@user:localhost")
            handled = await bot._turn_ingress._dispatch_file_sidecar_text_preview(
                room,
                sidecar_event,
                bot._ingress_validator.classify(sidecar_event),
                ticket=ticket,
                coalescing_thread_id="$thread_root",
            )
            await asyncio.wait_for(ticket.slot.settled.wait(), timeout=1.0)

        assert handled is TurnDispatchOutcome.DEFERRED
        mock_dispatch.assert_not_awaited()
        mock_reserve_waiting_human_message.assert_called_once()
        mock_admit.assert_awaited_once()
        key = mock_admit.await_args.args[0]
        ready_result = mock_admit.await_args.kwargs["ready_result"]
        assert isinstance(ready_result, ReadyPendingEvent)
        pending_event = ready_result.pending_event
        assert key == CoalescingKey(room.room_id, "$thread_root", RequesterCoalescingOwner("@user:localhost"))
        assert isinstance(pending_event, PendingEvent)
        assert pending_event.facts.run_requester_id == "@user:localhost"
        assert pending_event.event.event_id == prepared_event.event_id
        assert pending_event.event.body == prepared_event.body
        assert pending_event.facts.source_kind == MESSAGE_SOURCE_KIND
        assert pending_event.event.active_follow_up is False
        assert pending_event.claim is None
        assert pending_event.notice is mock_reserve_waiting_human_message.return_value

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_team_defers_placeholder_creation_to_coordinator(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Planner-side team dispatch should hand placeholder ownership to the coordinator."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        _replace_turn_policy_deps(bot, logger=bot.logger)

        room = nio.MatrixRoom("!room:localhost", bot.matrix_id.full_id)
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
                member_statuses=[
                    TeamResolutionMember(agent=bot.matrix_id, name=bot.agent_name, status=TeamMemberStatus.ELIGIBLE),
                ],
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
                "mindroom.turns.dispatch.select_ad_hoc_team_mode",
                new=AsyncMock(return_value=TeamMode.COORDINATE),
            ),
        ):
            await bot._turn_dispatcher._execute_response_action(
                room,
                event,
                dispatch,
                action,
                DispatchPayloadInputs((), (), ()),
                dispatch_started_at=0.0,
                handled_turn=TurnRecord.create([event.event_id]),
            )

        team_request = mock_generate_team_response.await_args.args[0]
        assert team_request.existing_event_id is None
        mock_send_response.assert_not_awaited()
        # The reply's records record the turn answered; turn dispatch writes nothing.
        await assert_no_turn_answered(bot)

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_team_explicit_members_uses_ai_team_mode(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Ad-hoc team dispatch should ask the AI selector for the team mode."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        _replace_turn_policy_deps(bot, logger=bot.logger)

        room = nio.MatrixRoom("!room:localhost", bot.matrix_id.full_id)
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
                member_statuses=[
                    TeamResolutionMember(agent=bot.matrix_id, name=bot.agent_name, status=TeamMemberStatus.ELIGIBLE),
                ],
                mode=TeamMode.COLLABORATE,
            ),
        )

        team_requests: list[ResponseRequest] = []

        async def generate_team_response(request: ResponseRequest, **_kwargs: object) -> str:
            team_requests.append(request)
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
            patch("mindroom.turns.dispatch.select_ad_hoc_team_mode", new=mock_select_team_mode),
        ):
            await bot._turn_dispatcher._execute_response_action(
                room,
                event,
                dispatch,
                action,
                DispatchPayloadInputs((), (), ()),
                dispatch_started_at=0.0,
                handled_turn=TurnRecord.create([event.event_id]),
            )

        assert action.form_team is not None
        mock_select_team_mode.assert_awaited_once_with(
            event.body,
            action.form_team.eligible_member_names,
            bot._turn_ingress.deps.runtime.config,
            bot._turn_ingress.deps.runtime_paths,
        )
        assert mock_generate_team_response.await_count == 1
        assert mock_generate_team_response.await_args.kwargs["team_mode"] == "coordinate"
        assert mock_generate_team_response.await_args.kwargs["member_names"] == (bot.agent_name,)
        # Enrichment hooks see the member names team formation resolved.
        [team_request] = team_requests
        assert team_request.payload_preparation is not None
        assert team_request.payload_preparation.target_member_names == (bot.agent_name,)
        assert bot.matrix_id.username != bot.agent_name

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_team_configured_team_skips_ai_team_mode(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Configured-team dispatch should pass the configured mode through without an AI call."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        _replace_turn_policy_deps(bot, logger=bot.logger)

        room = nio.MatrixRoom("!room:localhost", bot.matrix_id.full_id)
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
                member_statuses=[
                    TeamResolutionMember(agent=bot.matrix_id, name=bot.agent_name, status=TeamMemberStatus.ELIGIBLE),
                ],
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
            patch("mindroom.turns.dispatch.select_ad_hoc_team_mode", new=mock_select_team_mode),
        ):
            await bot._turn_dispatcher._execute_response_action(
                room,
                event,
                dispatch,
                action,
                DispatchPayloadInputs((), (), ()),
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
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        _replace_turn_policy_deps(bot, logger=bot.logger)

        room = nio.MatrixRoom("!room:localhost", bot.matrix_id.full_id)
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
            await bot._turn_dispatcher._execute_response_action(
                room,
                event,
                dispatch,
                ResponseAction(kind="individual"),
                DispatchPayloadInputs((), (), ()),
                dispatch_started_at=0.0,
                handled_turn=TurnRecord.create([event.event_id]),
            )

        mock_send_response.assert_not_awaited()
        assert mock_generate_response.await_args.kwargs["existing_event_id"] is None
        # The reply's records record the turn answered; turn dispatch writes nothing.
        await assert_no_turn_answered(bot)

    @pytest.mark.asyncio
    async def test_media_download_failure_edits_the_placeholder_into_a_terminal_error(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A media setup failure before response generation edits the reply's placeholder into the error."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = _make_matrix_client_mock()
        unique_room_send_responses(bot.client)
        bot.logger = MagicMock()

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.members_synced = True
        room.canonical_alias = None
        room.users = {
            "@mindroom_calculator:localhost": nio.MatrixUser("@mindroom_calculator:localhost"),
            "@user:localhost": nio.MatrixUser("@user:localhost"),
        }
        event = _room_image_event(sender="@user:localhost", event_id="$img_event_fail", body="photo.jpg")
        event.source = {"content": {"body": "photo.jpg"}}

        wrap_extracted_collaborators(bot, "_turn_policy")
        bot._turn_policy.plan_turn = AsyncMock(
            return_value=DispatchPlan(
                kind="respond",
                response_action=ResponseAction(kind="individual"),
            ),
        )

        with (
            patch("mindroom.turn_policy.TurnPolicy.can_reply_to_sender_in_room", return_value=True),
            patch("mindroom.inbound_turn_normalizer.download_image", new_callable=AsyncMock, return_value=None),
            patch.object(ResponsePayloadPreparer, "_log_dispatch_latency"),
        ):
            await bot._on_media_message(room, event)
            await drain_coalescing(bot)

        # The reply's records end it with the error and record the turn answered.
        sent = [call.kwargs["content"] for call in bot.client.room_send.await_args_list]
        assert [content.get("m.new_content", content)["body"] for content in sent] == [
            "Thinking...",
            "[calculator] ⚠️ Error: Failed to download image",
        ]
        answered = await unwrap_extracted_collaborator(bot._turn_store).get_turn_record("$img_event_fail")
        assert answered is not None
        assert answered.completed

    @pytest.mark.asyncio
    async def test_finalize_dispatch_failure_sends_terminal_error_message(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Without a reply to show it, a setup failure is sent directly and recorded as the turn's answer before return."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        bot._delivery_gateway.send_text = AsyncMock(return_value="$error")
        _replace_turn_policy_deps(bot, delivery_gateway=bot._delivery_gateway)

        resolution = await bot._interactive_selections._finalize_dispatch_failure(
            target=MessageTarget.resolve("!test:localhost", "$thread_root", "$event"),
            error=RuntimeError("boom"),
            handled_turn=TurnRecord.create(["$event"], completed=False),
            existing_event_id="$placeholder",
        )

        assert resolution == "$error"
        bot._delivery_gateway.send_text.assert_awaited_once_with(
            SendTextRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread_root", "$event"),
                response_text="[calculator] ⚠️ Error: boom",
                extra_content={STREAM_STATUS_KEY: STREAM_STATUS_ERROR},
            ),
        )
        await assert_turn_answered(bot, TurnRecord.create(["$event"], response_event_id="$error"))

    @pytest.mark.asyncio
    async def test_finalize_dispatch_failure_uses_system_response_kind_for_team_bot(
        self,
        tmp_path: Path,
    ) -> None:
        """Dispatch setup failures are system replies even when they occur on a team bot."""
        config = _runtime_bound_config(
            with_current_room_member_access(
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
                    models={"default": ModelConfig(provider="openai", id="test-model")},
                ),
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
        bot = make_test_agent_bot(
            team_user,
            config=config,
            runtime_paths=runtime_paths,
        )
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        bot._delivery_gateway.send_text = AsyncMock(return_value="$team-error")
        _replace_turn_policy_deps(bot, delivery_gateway=bot._delivery_gateway)

        await bot._interactive_selections._finalize_dispatch_failure(
            target=MessageTarget.resolve("!test:localhost", "$thread_root", "$event"),
            error=RuntimeError("boom"),
            handled_turn=TurnRecord.create(["$event"], completed=False),
            existing_event_id="$placeholder",
        )

        assert bot._delivery_gateway.send_text.await_args.args == (
            SendTextRequest(
                target=MessageTarget.resolve("!test:localhost", "$thread_root", "$event"),
                response_text="[team_bot] ⚠️ Error: boom",
                extra_content={STREAM_STATUS_KEY: STREAM_STATUS_ERROR},
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
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        unique_room_send_responses(bot.client)
        bot.logger = MagicMock()
        _replace_turn_policy_deps(bot, logger=bot.logger)

        room = nio.MatrixRoom("!room:localhost", bot.matrix_id.full_id)
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

        with patch(
            "mindroom.response_runner.prepare_memory_and_model_context",
            side_effect=RuntimeError(failure_message),
        ):
            await bot._turn_dispatcher._execute_response_action(
                room,
                event,
                dispatch,
                ResponseAction(kind="individual"),
                DispatchPayloadInputs((), (), ()),
                dispatch_started_at=0.0,
                handled_turn=TurnRecord.create([event.event_id]),
            )

        # The reply's placeholder is edited into the dispatch error.
        sent = [call.kwargs["content"] for call in bot.client.room_send.await_args_list]
        assert [content.get("m.new_content", content)["body"] for content in sent] == [
            "Thinking...",
            "[calculator] ⚠️ Error: setup failed",
        ]
        # The reply's records record the turn answered; turn dispatch writes nothing.
        await assert_no_turn_answered(bot)

    @pytest.mark.asyncio
    async def test_execute_dispatch_action_does_not_mark_responded_when_generation_returns_no_final_event(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Retryable resolutions with no response identity must keep the source retryable."""
        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        bot.logger = MagicMock()

        room = nio.MatrixRoom("!room:localhost", bot.matrix_id.full_id)
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
            patch.object(bot._response_runner, "generate_response", new=AsyncMock(return_value=Owned())),
            patch.object(ResponsePayloadPreparer, "_log_dispatch_latency"),
        ):
            await bot._turn_dispatcher._execute_response_action(
                room,
                event,
                dispatch,
                ResponseAction(kind="individual"),
                DispatchPayloadInputs((), (), ()),
                dispatch_started_at=0.0,
                handled_turn=TurnRecord.create([event.event_id]),
            )

        await assert_no_turn_answered(bot)
