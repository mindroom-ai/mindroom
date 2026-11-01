"""Regression tests for queued-message mid-turn notifications."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import nio
import pytest
from agno.media import Image
from agno.tools.function import ToolResult
from structlog.testing import capture_logs

from mindroom.agent_loop.model_loop import EMPTY_REQUEST, LoopHooks
from mindroom.agent_loop.run import run_agent
from mindroom.ai_runtime import queued_message_signal_context, queued_notice_after_tool_batch
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.coalescing.batch import (
    CoalescingKey,
    RequesterCoalescingOwner,
    active_follow_up_coalescing_key,
    build_prepared_turn,
)
from mindroom.config.agent import AgentConfig
from mindroom.config.judgment import LLMJudgmentConfig
from mindroom.config.main import Config
from mindroom.config.mid_turn import MidTurnConfig
from mindroom.config.models import ModelConfig
from mindroom.constants import THREAD_SUMMARY_CONTENT_KEY
from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_handoff import PreparedIngress
from mindroom.dispatch_source import (
    ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
    HOOK_DISPATCH_SOURCE_KIND,
    HOOK_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    SCHEDULED_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    VOICE_SOURCE_KIND,
)
from mindroom.final_delivery import (
    Cancelled,
    Completed,
    Failed,
    FailureCause,
    FinalDeliveryOutcome,
    Suppressed,
    Suppression,
    TurnEnding,
)
from mindroom.history.request_messages import TurnMessages, build_request, scope_history_messages
from mindroom.history.run_visibility import is_queued_notice_message
from mindroom.history.types import HistoryScope
from mindroom.hooks import MessageEnvelope
from mindroom.interactive import InteractiveMetadata
from mindroom.interactive_models import InteractivePrompt
from mindroom.llm.messages import Message as OwnedMessage
from mindroom.llm.runs import AnyRunRecord, RunRecord
from mindroom.llm.runs import RunStatus as OwnedRunStatus
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.conversation_hydration import HYDRATED_PROMPT_WINDOW_MESSAGES
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.matrix.users import AgentMatrixUser
from mindroom.media_inputs import MediaInputs
from mindroom.message_target import MessageTarget, ResponseLifecycleKey
from mindroom.mid_turn import QueuedMessage
from mindroom.post_response_effects import (
    PostResponseEffectsDeps,
    PostResponseEffectsSupport,
    ResponseOutcome,
    apply_post_response_effects,
)
from mindroom.prompts import QUEUED_MESSAGE_NOTICE_TEXT
from mindroom.reply_turn.locked_turn import LockedTurns
from mindroom.reply_turn.request import ResponseRequest
from mindroom.response_lifecycle import ResponseLifecycleCoordinator, _QueuedMessageState
from mindroom.response_payload_preparation import DispatchPayloadInputs, ResponsePayloadPreparation
from mindroom.response_runner import ResponseRunner, _PostLockRequestPreparationError
from mindroom.response_sources import Owned, ResponseSources, SourceFate, Unclaimed, UnclaimedReason
from mindroom.session_storage.agent_storage import create_agent_state_storage
from mindroom.session_storage.store import ConversationStore, persist_to
from mindroom.session_storage.types import ConversationSession, SessionKind
from mindroom.turn_policy import DispatchPlan, PreparedDispatch, ResponseAction
from mindroom.turns.dispatch import TurnDispatcher
from tests.access_schema_support import with_current_room_member_access
from tests.agent_loop.helpers import collect, context, spec, toolkit
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_model_providers,
    install_runtime_journal_support,
    loop_runs_while_blocked,
    make_conversation_reader_mock,
    make_ingress_facts,
    make_matrix_client_mock,
    make_membership_stub,
    make_pending_event,
    message_origin,
    request_envelope,
    runtime_paths_for,
    seed_session,
    serve_conversation_reader,
    summarized_lines,
    test_runtime_paths,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.response_runner_helpers import streamed_answer
from tests.runtime_fakes import ready_runtime
from tests.turn_dispatch_helpers import dispatch_test_turn, nothing_else_queued, prepared_turn_recorder

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.delivery_gateway import FinalDeliveryRequest
    from mindroom.matrix.conversation_reads import ConversationReader
    from mindroom.turn_origin import TurnOrigin


class _NoopResponseLifecycle:
    def __init__(self) -> None:
        self.identity = SimpleNamespace(correlation_id="$event", response_kind="ai")

    async def finalize(
        self,
        final_delivery_outcome: FinalDeliveryOutcome,
        **_kwargs: object,
    ) -> FinalDeliveryOutcome:
        return final_delivery_outcome


def _config(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        with_current_room_member_access(
            Config(
                agents={"general": AgentConfig(display_name="General", rooms=["!room:localhost"])},
                teams={},
                models={"default": ModelConfig(provider="openai", id="test-model")},
            ),
        ),
        test_runtime_paths(tmp_path),
    )


def _bot(tmp_path: Path) -> AgentBot:
    config = _config(tmp_path)
    agent_user = AgentMatrixUser(
        agent_name="general",
        password=TEST_PASSWORD,
        display_name="General",
        user_id="@mindroom_general:localhost",
    )
    bot = make_test_agent_bot(agent_user, config, runtime_paths_for(config), rooms=["!room:localhost"])
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    install_runtime_journal_support(bot)
    wrap_extracted_collaborators(bot)
    return bot


def _envelope(
    *,
    source_kind: str = MESSAGE_SOURCE_KIND,
    dispatch_policy_source_kind: str | None = None,
    source_event_id: str = "$event",
    target: MessageTarget | None = None,
    requester_id: str = "@user:localhost",
    sender_id: str = "@user:localhost",
    origin: TurnOrigin | None = None,
) -> MessageEnvelope:
    target = target or MessageTarget.resolve(
        room_id="!room:localhost",
        thread_id=None,
        reply_to_event_id="$event",
    )
    return MessageEnvelope(
        source_event_id=source_event_id,
        target=target,
        body="hello",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="general",
        dispatch_policy_source_kind=dispatch_policy_source_kind,
        origin=origin or message_origin(sender_id=sender_id, requester_id=requester_id, source_kind=source_kind),
    )


def _request(envelope: MessageEnvelope, **overrides: Any) -> ResponseRequest:  # noqa: ANN401
    """Return the request answering ``envelope``'s one source; ``overrides`` replace its other fields."""
    return replace(
        ResponseRequest(
            sources=ResponseSources(
                pending_event_ids=(envelope.source_event_id,),
                logical_source_event_ids=(envelope.source_event_id,),
            ),
            thread_history=[],
            prompt="hello",
            user_id="@user:localhost",
            response_envelope=envelope,
        ),
        **overrides,
    )


def _prepared_text_event(*, event_id: str = "$event") -> PreparedIngress:
    return PreparedIngress(
        sender="@user:localhost",
        event_id=event_id,
        body="hello",
        source={"content": {"body": "hello"}},
        server_timestamp=1234,
    )


def _message_context() -> MessageContext:
    return MessageContext(
        am_i_mentioned=False,
        is_thread=False,
        thread_id=None,
        thread_history=[],
        mentioned_agents=[],
        has_non_agent_mentions=False,
    )


def _payload_preparation(target: MessageTarget) -> ResponsePayloadPreparation:
    """Build a minimal under-lock preparation carrier bound to ``target``."""
    return ResponsePayloadPreparation(
        dispatch=PreparedDispatch(
            requester_user_id="@user:localhost",
            context=_message_context(),
            target=target,
            correlation_id="$event",
            envelope=_envelope(source_event_id="$event", target=target),
        ),
        prompt="hello",
        action_kind="individual",
        payload_inputs=DispatchPayloadInputs((), (), ()),
        target_member_names=None,
        dispatch_started_at=0.0,
        context_ready_monotonic=0.0,
    )


def _reserved_follow_up_case(
    bot: AgentBot,
    room: nio.MatrixRoom,
    *,
    event_id: str,
    body: str = "hello",
) -> SimpleNamespace:
    target = MessageTarget.resolve(room.room_id, "$thread", event_id)
    envelope = _envelope(
        dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        source_event_id=event_id,
        target=target,
    )
    event = PreparedIngress(
        sender="@user:localhost",
        event_id=event_id,
        body=body,
        source={"content": {"body": body}},
        server_timestamp=1234,
    )
    dispatch = PreparedDispatch(
        requester_user_id="@user:localhost",
        context=MessageContext(
            am_i_mentioned=False,
            is_thread=True,
            thread_id="$thread",
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        ),
        target=target,
        correlation_id=event_id,
        envelope=envelope,
    )
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator.lifecycle_coordinator
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    queued_signal.begin_response_turn()
    reservation = lifecycle.reserve_waiting_human_message(target=target, response_envelope=envelope)
    assert reservation is not None
    return SimpleNamespace(
        dispatch=dispatch,
        event=event,
        queued_signal=queued_signal,
        reservation=reservation,
    )


def test_response_lifecycle_rejects_mismatched_reservation_target() -> None:
    """Queued notices should use the envelope's canonical lifecycle target."""
    lifecycle = ResponseLifecycleCoordinator()
    envelope = _envelope()
    mismatched_target = MessageTarget.resolve("!other:localhost", None, "$event")

    with pytest.raises(ValueError, match=r"MessageEnvelope\.target"):
        lifecycle.reserve_waiting_human_message(
            target=mismatched_target,
            response_envelope=envelope,
        )


@pytest.mark.asyncio
async def test_response_lifecycle_rejects_mismatched_locked_response_target() -> None:
    """Response locking should use the envelope's canonical lifecycle target."""
    lifecycle = ResponseLifecycleCoordinator()
    envelope = _envelope()
    mismatched_target = MessageTarget.resolve("!other:localhost", None, "$event")

    async def locked_operation(_target: MessageTarget) -> SourceFate:
        return Owned()

    with pytest.raises(ValueError, match=r"MessageEnvelope\.target"):
        await lifecycle.run_locked_response(
            target=mismatched_target,
            response_envelope=envelope,
            pipeline_timing=None,
            locked_operation=locked_operation,
        )


_QUEUED_NOTICE_MARKER_KEY = "mindroom_queued_message_notice"
_QUEUED_NOTICE_RESPONSE_TURN_ID_KEY = "mindroom_queued_message_notice_response_turn_id"


def _notice_messages(messages: list[OwnedMessage], *, response_turn_id: str | None = None) -> list[OwnedMessage]:
    return [
        message
        for message in messages
        if is_queued_notice_message(message)
        and (
            response_turn_id is None
            or (message.provider_data or {}).get(_QUEUED_NOTICE_RESPONSE_TURN_ID_KEY) == response_turn_id
        )
    ]


def _notice_count(messages: list[OwnedMessage], *, response_turn_id: str | None = None) -> int:
    return len(_notice_messages(messages, response_turn_id=response_turn_id))


def _queued_notice_storage(tmp_path: Path) -> ConversationStore:
    return create_agent_state_storage("queued_notice", tmp_path)


async def _after_batch(
    messages: list[OwnedMessage],
    *results: OwnedMessage,
    notice_text: str = QUEUED_MESSAGE_NOTICE_TEXT,
) -> None:
    """Append one tool batch's results and run the queued-notice hook a reply installs after it, as the loop does."""
    messages.extend(results)
    await queued_notice_after_tool_batch(notice_text)(messages, results)


def _owned_notice(response_turn_id: str, *, from_history: bool = False) -> OwnedMessage:
    return OwnedMessage(
        role="user",
        content=QUEUED_MESSAGE_NOTICE_TEXT,
        provider_data={_QUEUED_NOTICE_MARKER_KEY: True, _QUEUED_NOTICE_RESPONSE_TURN_ID_KEY: response_turn_id},
        from_history=from_history,
    )


def _completed(run_id: str, messages: list[OwnedMessage], *, agent_id: str = "general") -> RunRecord:
    """Return a completed run holding its own copy of ``messages``, as the run's save stores it."""
    return RunRecord(
        run_id=run_id,
        session_id="session-1",
        agent_id=agent_id,
        status=OwnedRunStatus.completed,
        messages=deepcopy(messages),
    )


def _seed(
    store: ConversationStore,
    *runs: AnyRunRecord,
    kind: SessionKind = "agent",
    session_id: str = "session-1",
) -> ConversationSession:
    owner = "general" if kind == "agent" else "queued-notice-team"
    for run in runs:
        run.session_id = session_id
    return seed_session(
        store,
        ConversationSession(
            session_id=session_id,
            kind=kind,
            owner_id=owner,
            runs=list(runs),
            created_at=1,
        ),
    )


class _StaticQueuedState:
    def __init__(self, *, pending: bool) -> None:
        self.pending = pending

    def has_pending_human_messages(self) -> bool:
        return self.pending

    def pending_message_snapshot(self) -> tuple[QueuedMessage, ...]:
        return (QueuedMessage("$pending", None),) if self.has_pending_human_messages() else ()


def test_queued_message_state_tracks_source_event_ids_idempotently() -> None:
    """Queued-message state should track distinct source events, not anonymous increments."""
    state = _QueuedMessageState()

    assert state.add_waiting_human_message("$event")
    assert not state.add_waiting_human_message("$event")

    assert state.has_pending_human_messages()
    assert state.pending_human_messages == 1

    state.consume_waiting_human_message("$unknown")
    assert state.pending_human_messages == 1

    state.consume_waiting_human_message("$event")
    state.consume_waiting_human_message("$event")

    assert not state.has_pending_human_messages()
    assert state.pending_human_messages == 0


def test_active_follow_up_batch_prompt_uses_queued_receive_order() -> None:
    """Target-scoped active follow-up batches should preserve timeline order and per-message tags."""
    room = nio.MatrixRoom("!room:localhost", "@mindroom_general:localhost")
    pending_events = [
        make_pending_event(
            PreparedIngress(
                sender="@alice:localhost",
                event_id="$a1",
                body="A first",
                source={"content": {"body": "A first"}},
                server_timestamp=1,
            ),
            room,
            requester_user_id="@alice:localhost",
            source_kind=MESSAGE_SOURCE_KIND,
            active_follow_up=True,
        ),
        make_pending_event(
            PreparedIngress(
                sender="@alice:localhost",
                event_id="$a2",
                body="A <context> & more",
                source={"content": {"body": "A <context> & more"}},
                server_timestamp=2,
            ),
            room,
            requester_user_id="@alice:localhost",
            source_kind=MESSAGE_SOURCE_KIND,
            active_follow_up=True,
        ),
        make_pending_event(
            PreparedIngress(
                sender="@alice:localhost",
                event_id="$a3",
                body="A follow-up",
                source={"content": {"body": "A follow-up"}},
                server_timestamp=3,
            ),
            room,
            requester_user_id="@alice:localhost",
            source_kind=MESSAGE_SOURCE_KIND,
            active_follow_up=True,
        ),
    ]

    batch = build_prepared_turn(
        active_follow_up_coalescing_key(room.room_id, "$thread"),
        pending_events,
        queues_other_run=nothing_else_queued,
    )

    assert batch.handled_turn.source_event_ids == ("$a1", "$a2", "$a3")
    assert batch.facts.run_requester_id == "@alice:localhost"
    assert batch.handled_turn.source_event_prompts == {
        "$a1": "A first",
        "$a2": "A <context> & more",
        "$a3": "A follow-up",
    }
    assert batch.event.body == (
        "Messages arrived while the previous response was still running. "
        "They are in chat timeline order. Respond once to the combined context:\n\n"
        "<queued_messages>\n"
        '<msg event_id="$a1" from="@alice:localhost"><![CDATA[A first]]></msg>\n'
        '<msg event_id="$a2" from="@alice:localhost"><![CDATA[A <context> & more]]></msg>\n'
        '<msg event_id="$a3" from="@alice:localhost"><![CDATA[A follow-up]]></msg>\n'
        "</queued_messages>"
    )


@pytest.mark.asyncio
async def test_room_mode_root_batch_consumes_all_same_target_reservations(tmp_path: Path) -> None:
    """Root events coalesced in room mode should consume every reservation for that room."""
    bot = _bot(tmp_path)
    bot.config.agents["general"].thread_mode = "room"
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    first_reservation = MagicMock()
    second_reservation = MagicMock()
    first_event = _prepared_text_event(event_id="$a1")
    second_event = _prepared_text_event(event_id="$b1")
    batch = build_prepared_turn(
        CoalescingKey(room.room_id, None, RequesterCoalescingOwner("@user:localhost")),
        [
            make_pending_event(
                first_event,
                room,
                source_kind=MESSAGE_SOURCE_KIND,
                notice=first_reservation,
            ),
            make_pending_event(
                second_event,
                room,
                source_kind=MESSAGE_SOURCE_KIND,
                notice=second_reservation,
            ),
        ],
        queues_other_run=nothing_else_queued,
    )

    with patch.object(TurnDispatcher, "dispatch_turn", new=AsyncMock()):
        await bot._turn_dispatcher.handle_prepared_turn(batch)

    first_reservation.release.assert_called_once_with()
    second_reservation.release.assert_called_once_with()


class _PrelockBarrierLock:
    def __init__(self) -> None:
        self._locked = False
        self.first_waiting = asyncio.Event()
        self._allow_first_entry = asyncio.Event()
        self._first_entered = asyncio.Event()
        self._released = asyncio.Event()
        self._released.set()

    def locked(self) -> bool:
        return self._locked

    async def acquire(self) -> None:
        if not self.first_waiting.is_set():
            self.first_waiting.set()
            await self._allow_first_entry.wait()
        else:
            await self._first_entered.wait()
        await self._released.wait()
        self._locked = True
        self._released.clear()
        self._first_entered.set()

    def release(self) -> None:
        self._locked = False
        self._released.set()

    async def __aenter__(self) -> Self:
        await self.acquire()
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.release()


@pytest.mark.asyncio
async def test_post_response_effects_skip_thread_summary_for_suppressed_delivery() -> None:
    """Suppressed deliveries must not enqueue a thread summary."""
    queue_thread_summary = MagicMock()

    await apply_post_response_effects(
        FinalDeliveryOutcome(
            ending=Suppressed(Suppression.HOOK),
            event_id=None,
        ),
        ResponseOutcome(
            response_target=MessageTarget.resolve(
                room_id="!room:localhost",
                thread_id="$thread",
                reply_to_event_id="$event",
            ),
            thread_summary_message_count_hint=3,
        ),
        PostResponseEffectsDeps(
            logger=MagicMock(),
            queue_thread_summary=queue_thread_summary,
        ),
    )

    queue_thread_summary.assert_not_called()


@pytest.mark.asyncio
async def test_post_response_effects_report_once_whether_a_question_reply_lost_its_buttons_metadata() -> None:
    """A question reply that cannot get its buttons logs whether its interactive metadata is missing, once."""
    logger = MagicMock()

    await apply_post_response_effects(
        FinalDeliveryOutcome(
            ending=Completed(),
            event_id="$response",
            is_visible_response=True,
            final_visible_body="Pick one.\n\nReact with an emoji or type the number to respond.",
        ),
        ResponseOutcome(),
        PostResponseEffectsDeps(logger=logger, add_interactive_buttons=AsyncMock()),
    )

    logger.warning.assert_called_once_with(
        "Interactive question buttons skipped",
        response_event_id="$response",
        add_interactive_buttons_is_none=False,
        ending="Completed",
        final_visible_body_is_none=False,
        interactive_metadata_is_none=True,
    )


@pytest.mark.parametrize(
    "ending",
    [Failed(FailureCause.ERROR, "boom"), Cancelled("user_stop"), Completed()],
    ids=["error", "cancelled", "completed"],
)
@pytest.mark.asyncio
async def test_post_response_effects_skip_thread_summary_for_unsuccessful_turn(ending: TurnEnding) -> None:
    """Visible failed or cancelled turns must not enqueue thread enrichment."""
    should_queue_thread_summary = MagicMock(return_value=True)
    queue_thread_summary = MagicMock()

    await apply_post_response_effects(
        FinalDeliveryOutcome(
            ending=ending,
            event_id="$response",
            is_visible_response=True,
            final_visible_body="Turn did not complete",
        ),
        ResponseOutcome(
            run_succeeded=False,
            response_target=MessageTarget.resolve(
                room_id="!room:localhost",
                thread_id="$thread",
                reply_to_event_id="$event",
            ),
            thread_summary_message_count_hint=2,
        ),
        PostResponseEffectsDeps(
            logger=MagicMock(),
            should_queue_thread_summary=should_queue_thread_summary,
            queue_thread_summary=queue_thread_summary,
        ),
    )

    should_queue_thread_summary.assert_not_called()
    queue_thread_summary.assert_not_called()


@pytest.mark.asyncio
async def test_post_response_effects_skip_memory_persistence_for_failed_run() -> None:
    """Failed runs should not enqueue memory persistence for incomplete content."""
    queue_memory_persistence = MagicMock()

    await apply_post_response_effects(
        FinalDeliveryOutcome(
            ending=Failed(FailureCause.ERROR, "Provider failed"),
            event_id="$response",
            is_visible_response=True,
            final_visible_body="Provider failed",
        ),
        ResponseOutcome(run_succeeded=False),
        PostResponseEffectsDeps(
            logger=MagicMock(),
            queue_memory_persistence=queue_memory_persistence,
        ),
    )

    queue_memory_persistence.assert_not_called()


@pytest.mark.asyncio
async def test_post_response_effects_add_buttons_for_preserved_stream_success() -> None:
    """Preserved visible streamed replies should still receive reaction buttons."""
    add_interactive_buttons = AsyncMock()
    target = MessageTarget.resolve(
        room_id="!room:localhost",
        thread_id="$thread",
        reply_to_event_id="$event",
    )
    interactive_metadata = InteractiveMetadata._from_parts(
        {"1": "yes"},
        ({"emoji": "1", "label": "Yes", "value": "yes"},),
    )
    assert interactive_metadata is not None

    await apply_post_response_effects(
        FinalDeliveryOutcome(
            ending=Completed(),
            event_id="$stream",
            is_visible_response=True,
            final_visible_body="Choose",
            delivery_kind="sent",
            interactive_metadata=interactive_metadata,
        ),
        ResponseOutcome(
            response_target=target,
        ),
        PostResponseEffectsDeps(
            logger=MagicMock(),
            add_interactive_buttons=add_interactive_buttons,
        ),
    )

    add_interactive_buttons.assert_awaited_once_with(
        "$stream",
        interactive_metadata,
    )


@pytest.mark.asyncio
async def test_post_response_effects_skip_buttons_for_preserved_stream_error() -> None:
    """Failed preserved stream outcomes must not add buttons to a failed reply."""
    add_interactive_buttons = AsyncMock()
    target = MessageTarget.resolve(
        room_id="!room:localhost",
        thread_id="$thread",
        reply_to_event_id="$event",
    )
    interactive_metadata = InteractiveMetadata._from_parts(
        {"1": "yes"},
        ({"emoji": "1", "label": "Yes", "value": "yes"},),
    )
    assert interactive_metadata is not None

    await apply_post_response_effects(
        FinalDeliveryOutcome(
            ending=Failed(FailureCause.ERROR),
            event_id="$stream",
            is_visible_response=True,
            final_visible_body="Choose",
            interactive_metadata=interactive_metadata,
        ),
        ResponseOutcome(
            response_target=target,
        ),
        PostResponseEffectsDeps(
            logger=MagicMock(),
            add_interactive_buttons=add_interactive_buttons,
        ),
    )

    add_interactive_buttons.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_response_effects_skip_buttons_when_prompt_membership_ended(tmp_path: Path) -> None:
    """A response prompt rejected during transport must not expose dead buttons."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    client = make_matrix_client_mock()
    config.defaults.enable_streaming = False
    runtime = BotRuntimeState(orchestrator=ready_runtime(config, runtime_paths), client=client)
    membership = make_membership_stub()
    membership.interactive_prompt_is_current.return_value = False
    support = PostResponseEffectsSupport(
        runtime=runtime,
        logger=MagicMock(),
        runtime_paths=runtime_paths,
        conversation_reader=make_conversation_reader_mock(),
        membership=membership,
        agent_name="agent",
    )
    interactive_metadata = InteractiveMetadata._from_parts(
        {"1": "yes"},
        ({"emoji": "1", "label": "Yes", "value": "yes"},),
    )
    assert interactive_metadata is not None

    await apply_post_response_effects(
        FinalDeliveryOutcome(
            ending=Completed(),
            event_id="$response",
            is_visible_response=True,
            final_visible_body="Choose",
            delivery_kind="sent",
            interactive_metadata=interactive_metadata,
        ),
        ResponseOutcome(),
        support.build_deps(
            room_id="!room:localhost",
            membership_turn_id="$turn",
        ),
    )

    membership.interactive_prompt_is_current.assert_awaited_once_with(
        room_id="!room:localhost",
        question_event_id="$response",
        expected=InteractivePrompt(
            creator_agent="agent",
            question_text="",
            options={"1": "yes"},
            option_labels={},
            source_event_id="$turn",
        ),
    )
    client.room_send.assert_not_awaited()


async def _run_queued_summary(
    config: Config,
    client: AsyncMock,
    room_id: str,
    *,
    entity_name: str | None = None,
) -> tuple[ConversationReader, list[ResolvedVisibleMessage]]:
    """Deliver an answer in a five-message thread and await the summary pass it queues.

    Returns the reader the pass read the thread through, and the thread history it served.
    """
    config.defaults.enable_streaming = False
    runtime_paths = runtime_paths_for(config)
    runtime = BotRuntimeState(orchestrator=ready_runtime(config, runtime_paths), client=client)
    conversation_reader = make_conversation_reader_mock()
    support = PostResponseEffectsSupport(
        runtime=runtime,
        logger=MagicMock(),
        runtime_paths=runtime_paths,
        conversation_reader=conversation_reader,
        membership=make_membership_stub(),
        agent_name="agent",
    )
    deps = support.build_deps(
        room_id=room_id,
        membership_turn_id="$turn",
    )
    thread_history = [
        ResolvedVisibleMessage.synthetic(
            sender=f"@user{i}:localhost",
            body=f"Message {i}",
            timestamp=i,
            event_id=f"$message{i}",
        )
        for i in range(5)
    ]
    serve_conversation_reader(conversation_reader, thread_history)
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

    with patch("mindroom.post_response_effects.create_background_task", side_effect=schedule_background_task):
        await apply_post_response_effects(
            FinalDeliveryOutcome(
                ending=Completed(),
                event_id="$response",
                is_visible_response=True,
                final_visible_body="response",
                delivery_kind="sent",
            ),
            ResponseOutcome(
                response_target=MessageTarget.resolve(
                    room_id=room_id,
                    thread_id="$thread",
                    reply_to_event_id="$event",
                ),
                thread_summary_message_count_hint=4,
                thread_summary_entity_name=entity_name,
            ),
            deps,
        )

        assert scheduled_tasks
        await asyncio.gather(*scheduled_tasks)
    return conversation_reader, thread_history


@pytest.mark.asyncio
@pytest.mark.usefixtures("thread_summary_baselines")
async def test_post_response_effects_queues_summary_with_stale_hint_inside_margin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale hint just below threshold should still reach the live summary check."""
    model = ScriptedModel([answer('{"summary": "Summary"}')])
    install_model_providers(monkeypatch, {"default": model})
    config = _config(tmp_path)
    config.defaults.thread_summary_first_threshold = 5
    client = make_matrix_client_mock()
    client.room_send.return_value = nio.RoomSendResponse(event_id="$summary", room_id="!room:localhost")

    conversation_reader, thread_history = await _run_queued_summary(config, client, "!room:localhost")

    # One strict read for the gate, one after the pin re-check walked the thread again.
    expected_read = call(room_id="!room:localhost", thread_id="$thread", limit=HYDRATED_PROMPT_WINDOW_MESSAGES)
    assert conversation_reader.read_strict.await_args_list == [expected_read, expected_read]
    conversation_reader.hydrator.rewalk.assert_awaited_once_with(
        room_id="!room:localhost",
        thread_id="$thread",
        edits_of=(),
        unreadable_is_missing=False,
    )
    # Nothing has echoed the response back, so the projection this pass reads
    # stops one message short of the thread. It summarizes the projected
    # messages plus the answer it was queued for, and counts that answer -- the
    # same number the pre-queue gate above already used.
    (request,) = model.requests
    assert summarized_lines(request) == [
        *(f"{message.sender}: {message.body}" for message in thread_history),
        f"{client.user_id}: response",
    ]
    (sent,) = [call.kwargs["content"] for call in client.room_send.await_args_list]
    assert sent["body"] == "Summary"
    assert sent["m.relates_to"]["event_id"] == "$thread"
    assert sent[THREAD_SUMMARY_CONTENT_KEY]["message_count"] == 6
    assert sent[THREAD_SUMMARY_CONTENT_KEY]["model"] == "default"
    assert "initial_enrichment_complete" not in sent[THREAD_SUMMARY_CONTENT_KEY]


@pytest.mark.asyncio
@pytest.mark.usefixtures("thread_summary_baselines")
async def test_post_response_effects_queues_summary_with_entity_model_for_adhoc_room(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ad-hoc invited rooms should inherit the responding entity's summary model override."""
    # Only "qwen" is served, so summarizing with the default model would fail to build it.
    model = ScriptedModel([answer('{"summary": "Summary"}')])
    install_model_providers(monkeypatch, {"qwen": model})
    config = _config(tmp_path)
    config.defaults.thread_summary_first_threshold = 1
    config.room_thread_summary_models["general"] = "qwen"
    config.models["qwen"] = ModelConfig(provider="openai", id="qwen-test-model")
    client = make_matrix_client_mock()

    _, thread_history = await _run_queued_summary(config, client, "!adhoc:localhost", entity_name="general")

    (request,) = model.requests
    assert summarized_lines(request) == [
        *(f"{message.sender}: {message.body}" for message in thread_history),
        f"{client.user_id}: response",
    ]


@pytest.mark.asyncio
async def test_generate_response_sets_queued_signal_for_human_ingress(tmp_path: Path) -> None:
    """A waiting human-authored turn should notify the active turn before blocking on the lock."""
    bot = _bot(tmp_path)
    response_envelope = _envelope()
    response_target = response_envelope.target
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator.lifecycle_coordinator
    lifecycle_lock = lifecycle._response_lifecycle_lock(response_target)
    queued_signal = lifecycle._get_or_create_queued_signal(response_target)
    await lifecycle_lock.acquire()

    try:
        with patch.object(
            ResponseRunner,
            "_run_locked_turn",
            new=AsyncMock(return_value=Owned()),
        ) as mock_locked:
            task = asyncio.create_task(
                bot._response_runner.generate_response(
                    _request(response_envelope),
                ),
            )
            await asyncio.wait_for(queued_signal.wait(), timeout=0.2)
            lifecycle_lock.release()
            assert await task == Owned()
            mock_locked.assert_awaited_once()
    finally:
        if lifecycle_lock.locked():
            lifecycle_lock.release()

    assert not queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_envelope",
    [
        pytest.param(_envelope(source_kind=SCHEDULED_SOURCE_KIND), id="scheduled"),
        pytest.param(_envelope(source_kind=HOOK_SOURCE_KIND), id="hook"),
        pytest.param(_envelope(source_kind=HOOK_DISPATCH_SOURCE_KIND), id="hook-dispatch"),
        pytest.param(
            _envelope(
                sender_id="@mindroom_router:localhost",
                requester_id="@mindroom_router:localhost",
                origin=message_origin(
                    sender_id="@mindroom_router:localhost",
                    requester_id="@mindroom_router:localhost",
                    sender_entity_name="router",
                    requester_entity_name="router",
                    source_kind=MESSAGE_SOURCE_KIND,
                ),
            ),
            id="router-notice",
        ),
    ],
)
async def test_generate_response_skips_signal_for_non_human_prompt_ingress(
    tmp_path: Path,
    response_envelope: MessageEnvelope,
) -> None:
    """Automation and router-authored notices should not interrupt the active turn."""
    bot = _bot(tmp_path)
    response_target = response_envelope.target
    coordinator = unwrap_extracted_collaborator(bot._turn_ingress.deps.response_runner)
    lifecycle = coordinator.lifecycle_coordinator
    lifecycle_lock = lifecycle._response_lifecycle_lock(response_target)
    queued_signal = lifecycle._get_or_create_queued_signal(response_target)
    await lifecycle_lock.acquire()

    try:
        with patch.object(
            ResponseRunner,
            "_run_locked_turn",
            new=AsyncMock(return_value=Owned()),
        ):
            task = asyncio.create_task(
                bot._response_runner.generate_response(
                    _request(response_envelope),
                ),
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(queued_signal.wait(), timeout=0.05)
            lifecycle_lock.release()
            await task
    finally:
        if lifecycle_lock.locked():
            lifecycle_lock.release()

    assert not queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
async def test_forced_compaction_placeholder_check_degrades_on_storage_error(tmp_path: Path) -> None:
    """Storage errors in the placeholder-ordering hint should not abort response generation."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    scope = HistoryScope(kind="agent", scope_id="home")

    with patch.object(
        coordinator.deps.state_writer,
        "create_storage",
        side_effect=RuntimeError("storage unavailable"),
    ):
        result = await coordinator._has_queued_forced_compaction(
            session_id="session",
            scope=scope,
            execution_identity=None,
        )

    assert result is False


@pytest.mark.asyncio
async def test_forced_compaction_placeholder_check_does_not_block_event_loop(tmp_path: Path) -> None:
    """A blocked compaction-state read must run outside the response event loop."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    scope = HistoryScope(kind="agent", scope_id="home")
    loop = asyncio.get_running_loop()
    read_saw_loop_running: list[bool] = []

    def _blocking_read(**_kwargs: object) -> bool:
        read_saw_loop_running.append(loop_runs_while_blocked(loop))
        return False

    with patch.object(coordinator, "_read_queued_forced_compaction", side_effect=_blocking_read):
        result = await coordinator._has_queued_forced_compaction(
            session_id="session",
            scope=scope,
            execution_identity=None,
        )

    assert result is False
    assert read_saw_loop_running == [True], "compaction-state read blocked the event loop"


@pytest.mark.asyncio
async def test_generate_response_sets_queued_signal_for_trusted_router_relay(tmp_path: Path) -> None:
    """A trusted router relay carrying a human requester should notify the active turn."""
    bot = _bot(tmp_path)
    response_envelope = _envelope(
        sender_id="@mindroom_router:localhost",
        requester_id="@user:localhost",
        source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        origin=message_origin(
            sender_id="@mindroom_router:localhost",
            requester_id="@user:localhost",
            sender_entity_name="router",
            requester_entity_name=None,
            source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
            original_sender="@user:localhost",
            trusted_user_relay=True,
        ),
    )
    response_target = response_envelope.target
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator.lifecycle_coordinator
    lifecycle_lock = lifecycle._response_lifecycle_lock(response_target)
    queued_signal = lifecycle._get_or_create_queued_signal(response_target)
    await lifecycle_lock.acquire()

    try:
        with patch.object(
            ResponseRunner,
            "_run_locked_turn",
            new=AsyncMock(return_value=Owned()),
        ):
            task = asyncio.create_task(
                bot._response_runner.generate_response(
                    _request(response_envelope),
                ),
            )
            await asyncio.wait_for(queued_signal.wait(), timeout=0.2)
            lifecycle_lock.release()
            assert await task == Owned()
    finally:
        if lifecycle_lock.locked():
            lifecycle_lock.release()

    assert not queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
async def test_generate_response_detects_active_turn_before_lock_is_held(tmp_path: Path) -> None:
    """A second human turn should queue even before the first acquires the lifecycle lock."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lock = _PrelockBarrierLock()
    first_envelope = _envelope(source_event_id="$first")
    second_envelope = _envelope(source_event_id="$second")

    async def fake_run_locked_turn(
        _self: ResponseRunner,
        request: ResponseRequest,
        **_kwargs: object,
    ) -> SourceFate:
        # Each requester's turn returns a fate of its own, so each task is seen to run its own request.
        return Owned() if request.user_id == "first" else Unclaimed(UnclaimedReason.NOT_DUE)

    with (
        patch.object(coordinator.lifecycle_coordinator, "_response_lifecycle_lock", return_value=lock),
        patch.object(ResponseRunner, "_run_locked_turn", new=fake_run_locked_turn),
    ):
        first_task = asyncio.create_task(
            bot._response_runner.generate_response(
                _request(first_envelope, user_id="first"),
            ),
        )
        await lock.first_waiting.wait()

        second_task = asyncio.create_task(
            bot._response_runner.generate_response(
                _request(second_envelope, prompt="stop", user_id="second"),
            ),
        )

        lock._allow_first_entry.set()
        second_result = await second_task
        first_result = await first_task

    assert second_result == Unclaimed(UnclaimedReason.NOT_DUE)
    assert first_result == Owned()


@pytest.mark.asyncio
async def test_generate_response_waits_for_lock_before_starting_placeholder_lifecycle(tmp_path: Path) -> None:
    """A queued scheduled turn should not start the placeholder lifecycle until it owns the lock."""
    bot = _bot(tmp_path)
    response_envelope = _envelope(source_kind=SCHEDULED_SOURCE_KIND)
    response_target = response_envelope.target
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle_lock = coordinator.lifecycle_coordinator._response_lifecycle_lock(response_target)
    await lifecycle_lock.acquire()
    lifecycle_started = asyncio.Event()
    # Both handoffs this test observes are causal, not timed: the turn is known
    # to be blocked on the lock because its own acquire said so, and the
    # lifecycle is known to have started because it said so. A wall-clock bound
    # on either would only re-add the race, since the work between the release
    # and the first placeholder is real and its duration is the machine's to
    # decide.
    lock_wait_started = asyncio.Event()
    acquire_lifecycle_lock = lifecycle_lock.acquire

    async def announce_lock_wait() -> bool:
        lock_wait_started.set()
        return await acquire_lifecycle_lock()

    lifecycle_lock.acquire = announce_lock_wait  # type: ignore[method-assign]

    async def fake_run_cancellable_response(*_args: object, **kwargs: object) -> FinalDeliveryOutcome:
        lifecycle_started.set()
        response_function = kwargs["response_function"]
        return await response_function(None)

    try:
        with (
            patch.object(
                ResponseRunner,
                "_deliver_turn",
                new=AsyncMock(
                    return_value=FinalDeliveryOutcome(
                        ending=Completed(),
                        event_id="$response",
                        is_visible_response=True,
                        final_visible_body="ok",
                        delivery_kind="sent",
                    ),
                ),
            ),
            patch.object(
                LockedTurns,
                "_run_cancellable_response",
                new=AsyncMock(side_effect=fake_run_cancellable_response),
            ) as mock_run_cancellable_response,
            patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
            patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock()),
        ):
            task = asyncio.create_task(
                bot._response_runner.generate_response(
                    _request(response_envelope),
                ),
            )
            await lock_wait_started.wait()
            mock_run_cancellable_response.assert_not_awaited()

            lifecycle_lock.release()
            await lifecycle_started.wait()
            resolution = await task
            assert resolution == Owned()
    finally:
        if lifecycle_lock.locked():
            lifecycle_lock.release()


@pytest.mark.asyncio
async def test_refresh_model_history_after_lock_refreshes_empty_thread_history(tmp_path: Path) -> None:
    """Threaded turns with an empty cached history should still refresh after lock handoff."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    resolver = unwrap_extracted_collaborator(coordinator.deps.resolver)
    fresh_history = ThreadHistoryResult(
        [
            ResolvedVisibleMessage.synthetic(
                sender="@user:localhost",
                body="updated",
                event_id="$reply",
                content={"body": "updated"},
            ),
        ],
        is_full_history=True,
    )

    with patch.object(
        resolver,
        "fetch_thread_history",
        new=AsyncMock(return_value=fresh_history),
    ) as mock_fetch_thread_history:
        request = await coordinator.locked.refresh_model_history(
            _request(
                request_envelope(
                    room_id="!room:localhost",
                    reply_to_event_id="$event",
                    thread_id="$thread",
                    prompt="hello",
                    user_id="@user:localhost",
                ),
            ),
        )

    mock_fetch_thread_history.assert_awaited_once_with(
        "!room:localhost",
        "$thread",
    )
    assert request.thread_history == fresh_history


@pytest.mark.asyncio
async def test_refresh_model_history_after_lock_does_not_reprove_room_target(
    tmp_path: Path,
) -> None:
    """Post-lock model refresh must not re-prove a finalized room target."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    resolver = unwrap_extracted_collaborator(coordinator.deps.resolver)
    target = MessageTarget.resolve("!room:localhost", None, "$event", room_mode=True)
    envelope = _envelope(source_event_id="$event", target=target)

    with patch.object(
        resolver,
        "fetch_thread_history",
        new=AsyncMock(side_effect=AssertionError("room targets have no model thread history to refresh")),
    ):
        request = await coordinator.locked.refresh_model_history(
            _request(envelope, requires_model_history_refresh=True),
        )

    assert request.thread_id is None
    assert request.thread_history == []
    assert request.response_envelope.target.resolved_thread_id is None


def _recording_run_targets(observed: list[MessageTarget]) -> Callable[..., Awaitable[FinalDeliveryOutcome]]:
    """Return a ``_run_cancellable_response`` stand-in that records each run's target and runs its attempt directly."""

    async def run_cancellable_response(**kwargs: object) -> FinalDeliveryOutcome:
        target = kwargs["target"]
        assert isinstance(target, MessageTarget)
        observed.append(target)
        response_function = cast(
            "Callable[[object | None], Awaitable[FinalDeliveryOutcome]]",
            kwargs["response_function"],
        )
        return await response_function(None)

    return run_cancellable_response


@pytest.mark.asyncio
async def test_generate_response_keeps_room_target_through_run_and_delivery(tmp_path: Path) -> None:
    """Agent delivery must enter the runner with the finalized stable room target."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    stable_target = MessageTarget.resolve("!room:localhost", None, "$event", room_mode=True)
    observed_run_targets: list[MessageTarget] = []
    observed_delivery_targets: list[MessageTarget] = []

    async def fake_deliver_turn(turn: object, **_kwargs: object) -> FinalDeliveryOutcome:
        observed_delivery_targets.append(cast("Any", turn).request.response_envelope.target)
        return FinalDeliveryOutcome(
            ending=Completed(),
            event_id="$response",
            is_visible_response=True,
            final_visible_body="ok",
            delivery_kind="sent",
        )

    with (
        patch.object(LockedTurns, "build_lifecycle", MagicMock(return_value=_NoopResponseLifecycle())),
        patch.object(
            LockedTurns,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=_recording_run_targets(observed_run_targets)),
        ),
        patch.object(coordinator, "_deliver_turn", new=AsyncMock(side_effect=fake_deliver_turn)),
        patch("mindroom.response_runner.should_use_streaming", AsyncMock(return_value=False)),
    ):
        result = await coordinator.generate_response(
            _request(_envelope(source_event_id="$event", target=stable_target)),
        )

    assert result == Owned()
    assert [target.resolved_thread_id for target in observed_run_targets] == [None]
    assert [target.resolved_thread_id for target in observed_delivery_targets] == [None]
    lock_keys = set(coordinator.lifecycle_coordinator._response_lifecycle_locks)
    assert ResponseLifecycleKey(room_id="!room:localhost", thread_id=None) in lock_keys


@pytest.mark.asyncio
async def test_generate_response_keeps_locked_target_when_payload_preparation_retargets(tmp_path: Path) -> None:
    """Post-lock request preparation may refresh context, but it must not retarget delivery."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    stable_target = MessageTarget.resolve("!room:localhost", None, "$event", room_mode=True)
    retarget = MessageTarget.resolve("!room:localhost", "$other_thread", "$event")
    observed_run_targets: list[MessageTarget] = []
    observed_delivery_targets: list[MessageTarget] = []
    observed_lifecycle_targets: list[MessageTarget] = []

    async def fake_prepare(request: ResponseRequest) -> ResponseRequest:
        return replace(
            request,
            response_envelope=_envelope(source_event_id="$event", target=retarget),
            payload_preparation=None,
        )

    async def fake_deliver_turn(turn: object, **_kwargs: object) -> FinalDeliveryOutcome:
        observed_delivery_targets.append(cast("Any", turn).request.response_envelope.target)
        return FinalDeliveryOutcome(
            ending=Completed(),
            event_id="$response",
            is_visible_response=True,
            final_visible_body="ok",
            delivery_kind="sent",
        )

    def fake_build_lifecycle(request: object, **_kwargs: object) -> _NoopResponseLifecycle:
        assert isinstance(request, ResponseRequest)
        observed_lifecycle_targets.append(request.response_envelope.target)
        return _NoopResponseLifecycle()

    with (
        patch.object(LockedTurns, "build_lifecycle", MagicMock(side_effect=fake_build_lifecycle)),
        patch.object(
            coordinator.deps.request_preparer,
            "prepare",
            new=AsyncMock(side_effect=fake_prepare),
        ),
        patch.object(
            LockedTurns,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=_recording_run_targets(observed_run_targets)),
        ),
        patch.object(coordinator, "_deliver_turn", new=AsyncMock(side_effect=fake_deliver_turn)),
        patch("mindroom.response_runner.should_use_streaming", AsyncMock(return_value=False)),
    ):
        result = await coordinator.generate_response(
            _request(
                _envelope(source_event_id="$event", target=stable_target),
                payload_preparation=_payload_preparation(stable_target),
            ),
        )

    assert result == Owned()
    assert observed_run_targets == [stable_target]
    assert observed_delivery_targets == [stable_target]
    assert observed_lifecycle_targets == [stable_target]
    lock_keys = set(coordinator.lifecycle_coordinator._response_lifecycle_locks)
    assert ResponseLifecycleKey(room_id="!room:localhost", thread_id=None) in lock_keys
    assert ResponseLifecycleKey(room_id="!room:localhost", thread_id="$other_thread") not in lock_keys


@pytest.mark.asyncio
async def test_generate_team_response_keeps_room_target_through_run_and_delivery(tmp_path: Path) -> None:
    """Team delivery/session setup must enter the runner with the finalized stable room target."""
    bot = _bot(tmp_path)
    bot.client = MagicMock()
    cached_room = MagicMock(encrypted=False)
    bot.client.rooms = {"!room:localhost": cached_room}
    bot.client.room_send = AsyncMock()
    bot.client.room_typing = AsyncMock()
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    stable_target = MessageTarget.resolve("!room:localhost", None, "$event", room_mode=True)
    lifecycle = _NoopResponseLifecycle()
    observed_run_targets: list[MessageTarget] = []
    observed_delivery_targets: list[MessageTarget] = []

    async def fake_deliver_final(request: FinalDeliveryRequest) -> FinalDeliveryOutcome:
        observed_delivery_targets.append(request.target)
        return FinalDeliveryOutcome(
            ending=Completed(),
            event_id="$response",
            is_visible_response=True,
            final_visible_body=request.response_text,
            delivery_kind="sent",
        )

    with (
        patch.object(LockedTurns, "build_lifecycle", MagicMock(return_value=lifecycle)),
        patch.object(
            LockedTurns,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=_recording_run_targets(observed_run_targets)),
        ),
        patch("mindroom.delivery_gateway.DeliveryGateway.deliver_final", new=AsyncMock(side_effect=fake_deliver_final)),
        patch("mindroom.response_runner.should_use_streaming", AsyncMock(return_value=False)),
        patch(
            "mindroom.response_runner.team_response_stream",
            new=streamed_answer(AsyncMock(return_value="team ok")),
        ),
    ):
        result = await coordinator.generate_team_response_helper(
            _request(_envelope(source_event_id="$event", target=stable_target)),
            member_names=[bot.agent_name],
            team_mode="coordinate",
        )

    assert result == Owned()
    assert [target.resolved_thread_id for target in observed_run_targets] == [None]
    assert [target.resolved_thread_id for target in observed_delivery_targets] == [None]
    lock_keys = set(coordinator.lifecycle_coordinator._response_lifecycle_locks)
    assert ResponseLifecycleKey(room_id="!room:localhost", thread_id=None) in lock_keys


@pytest.mark.asyncio
async def test_generate_team_response_keeps_locked_target_when_payload_preparation_retargets(tmp_path: Path) -> None:
    """Team response setup and delivery should keep the target selected before lock acquisition."""
    bot = _bot(tmp_path)
    bot.client = MagicMock()
    cached_room = MagicMock(encrypted=False)
    bot.client.rooms = {"!room:localhost": cached_room}
    bot.client.room_send = AsyncMock()
    bot.client.room_typing = AsyncMock()
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    stable_target = MessageTarget.resolve("!room:localhost", None, "$event", room_mode=True)
    retarget = MessageTarget.resolve("!room:localhost", "$other_thread", "$event")
    lifecycle = _NoopResponseLifecycle()
    observed_run_targets: list[MessageTarget] = []
    observed_delivery_targets: list[MessageTarget] = []

    async def fake_prepare(request: ResponseRequest) -> ResponseRequest:
        return replace(
            request,
            response_envelope=_envelope(source_event_id="$event", target=retarget),
            payload_preparation=None,
        )

    async def fake_deliver_final(request: FinalDeliveryRequest) -> FinalDeliveryOutcome:
        observed_delivery_targets.append(request.target)
        return FinalDeliveryOutcome(
            ending=Completed(),
            event_id="$response",
            is_visible_response=True,
            final_visible_body=request.response_text,
            delivery_kind="sent",
        )

    with (
        patch.object(LockedTurns, "build_lifecycle", MagicMock(return_value=lifecycle)),
        patch.object(
            coordinator.deps.request_preparer,
            "prepare",
            new=AsyncMock(side_effect=fake_prepare),
        ),
        patch.object(
            LockedTurns,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=_recording_run_targets(observed_run_targets)),
        ),
        patch("mindroom.delivery_gateway.DeliveryGateway.deliver_final", new=AsyncMock(side_effect=fake_deliver_final)),
        patch("mindroom.response_runner.should_use_streaming", AsyncMock(return_value=False)),
        patch(
            "mindroom.response_runner.team_response_stream",
            new=streamed_answer(AsyncMock(return_value="team ok")),
        ),
    ):
        result = await coordinator.generate_team_response_helper(
            _request(
                _envelope(source_event_id="$event", target=stable_target),
                payload_preparation=_payload_preparation(stable_target),
            ),
            member_names=[bot.agent_name],
            team_mode="coordinate",
        )

    assert result == Owned()
    assert observed_run_targets == [stable_target]
    assert observed_delivery_targets == [stable_target]
    lock_keys = set(coordinator.lifecycle_coordinator._response_lifecycle_locks)
    assert ResponseLifecycleKey(room_id="!room:localhost", thread_id=None) in lock_keys
    assert ResponseLifecycleKey(room_id="!room:localhost", thread_id="$other_thread") not in lock_keys


@pytest.mark.asyncio
async def test_prepare_request_after_lock_wraps_refresh_failures(tmp_path: Path) -> None:
    """Post-lock refresh failures should route through the normalized preparation error boundary."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    resolver = unwrap_extracted_collaborator(coordinator.deps.resolver)

    with (
        patch.object(
            resolver,
            "fetch_thread_history",
            new=AsyncMock(side_effect=RuntimeError("repair required")),
        ),
        pytest.raises(_PostLockRequestPreparationError) as excinfo,
    ):
        await coordinator._prepare_request_after_lock(
            _request(
                request_envelope(
                    room_id="!room:localhost",
                    reply_to_event_id="$event",
                    thread_id="$thread",
                    prompt="hello",
                    user_id="@user:localhost",
                ),
                requires_model_history_refresh=True,
            ),
        )

    assert isinstance(excinfo.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_generate_team_response_helper_sets_queued_signal(tmp_path: Path) -> None:
    """Team responses should raise the same queued-message signal before waiting on the lock."""
    bot = _bot(tmp_path)
    response_envelope = _envelope()
    response_target = response_envelope.target
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator.lifecycle_coordinator
    lifecycle_lock = lifecycle._response_lifecycle_lock(response_target)
    queued_signal = lifecycle._get_or_create_queued_signal(response_target)
    await lifecycle_lock.acquire()

    try:
        with patch.object(
            ResponseRunner,
            "_run_locked_turn",
            new=AsyncMock(return_value=Owned()),
        ) as mock_locked:
            task = asyncio.create_task(
                bot._response_runner.generate_team_response_helper(
                    _request(response_envelope),
                    member_names=[],
                    team_mode="coordinate",
                ),
            )
            await asyncio.wait_for(queued_signal.wait(), timeout=0.2)
            lifecycle_lock.release()
            assert await task == Owned()
            mock_locked.assert_awaited_once()
    finally:
        if lifecycle_lock.locked():
            lifecycle_lock.release()

    assert not queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
@pytest.mark.parametrize("team", [False, True], ids=["agent", "team"])
async def test_response_without_reservation_does_not_drain_human_backlog(tmp_path: Path, team: bool) -> None:
    """Unreserved direct agent or team responses serialize normally instead of owning active follow-up backlog."""
    bot = _bot(tmp_path)
    response_envelope_b = _envelope(source_event_id="$event-b")
    response_envelope_c = _envelope(source_event_id="$event-c")
    response_target = response_envelope_b.target
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator.lifecycle_coordinator
    lifecycle_lock = lifecycle._response_lifecycle_lock(response_target)
    queued_signal = lifecycle._get_or_create_queued_signal(response_target)
    observed_pending: list[bool] = []
    ran_sources: list[str] = []
    second_turn_started = asyncio.Event()
    allow_turns_to_finish = asyncio.Event()

    async def fake_locked(_self: ResponseRunner, request: ResponseRequest, **_kwargs: object) -> SourceFate:
        observed_pending.append(queued_signal.has_pending_human_messages())
        ran_sources.append(request.response_envelope.source_event_id)
        if len(observed_pending) == 1:
            second_turn_started.set()
        await allow_turns_to_finish.wait()
        return Owned()

    async def respond(request: ResponseRequest) -> SourceFate:
        if team:
            return await bot._response_runner.generate_team_response_helper(
                request,
                member_names=[],
                team_mode="coordinate",
            )
        return await bot._response_runner.generate_response(request)

    await lifecycle_lock.acquire()
    try:
        with patch.object(ResponseRunner, "_run_locked_turn", new=fake_locked):
            task_b = asyncio.create_task(respond(_request(response_envelope_b)))
            await asyncio.wait_for(queued_signal.wait(), timeout=0.2)
            task_c = asyncio.create_task(respond(_request(response_envelope_c, prompt="hello again")))
            for _ in range(20):
                if queued_signal.pending_human_messages == 2:
                    break
                await asyncio.sleep(0)
            assert queued_signal.pending_human_messages == 2

            lifecycle_lock.release()
            await asyncio.wait_for(second_turn_started.wait(), timeout=0.2)
            assert observed_pending == [True]

            allow_turns_to_finish.set()
            assert await task_b == Owned()
            assert await task_c == Owned()
    finally:
        if lifecycle_lock.locked():
            lifecycle_lock.release()

    assert observed_pending == [True, False]
    assert ran_sources == [response_envelope_b.source_event_id, response_envelope_c.source_event_id]
    assert not queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
async def test_reserved_human_follow_up_reaches_active_turn_before_dispatch(tmp_path: Path) -> None:
    """Gate-owned follow-ups should still notify the active response before their dispatch starts."""
    bot = _bot(tmp_path)
    active_envelope = _envelope(source_event_id="$active")
    follow_up_envelope = _envelope(source_event_id="$followup")
    response_target = active_envelope.target
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator.lifecycle_coordinator
    queued_signal = lifecycle._get_or_create_queued_signal(response_target)
    active_started = asyncio.Event()
    active_saw_follow_up = asyncio.Event()
    allow_active_to_finish = asyncio.Event()
    follow_up_observed_pending: list[bool] = []

    async def active_operation(_target: MessageTarget) -> str:
        active_started.set()
        await queued_signal.wait()
        if queued_signal.has_pending_human_messages():
            active_saw_follow_up.set()
        await allow_active_to_finish.wait()
        return "$active-response"

    async def follow_up_operation(_target: MessageTarget) -> str:
        follow_up_observed_pending.append(queued_signal.has_pending_human_messages())
        return "$followup-response"

    active_task = asyncio.create_task(
        lifecycle.run_locked_response(
            target=response_target,
            response_envelope=active_envelope,
            pipeline_timing=None,
            locked_operation=active_operation,
        ),
    )
    await asyncio.wait_for(active_started.wait(), timeout=0.2)

    reservation = lifecycle.reserve_waiting_human_message(
        target=response_target,
        response_envelope=follow_up_envelope,
    )
    assert reservation is not None
    await asyncio.wait_for(active_saw_follow_up.wait(), timeout=0.2)
    assert queued_signal.pending_human_messages == 1
    reservation.release()

    follow_up_task = asyncio.create_task(
        lifecycle.run_locked_response(
            target=response_target,
            response_envelope=follow_up_envelope,
            pipeline_timing=None,
            locked_operation=follow_up_operation,
        ),
    )
    await asyncio.sleep(0)
    assert queued_signal.pending_human_messages == 1

    allow_active_to_finish.set()
    assert await active_task == "$active-response"
    assert await follow_up_task == "$followup-response"

    assert follow_up_observed_pending == [False]
    assert not queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
async def test_response_lifecycle_reservations_clear_individual_notices(tmp_path: Path) -> None:
    """Lifecycle reservations only own queued-message notices, not coalesced follow-up batching."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator.lifecycle_coordinator
    target = MessageTarget.resolve("!room:localhost", "$thread", "$active")
    active_envelope = _envelope(source_event_id="$active", target=target)
    active_started = asyncio.Event()
    release_active = asyncio.Event()
    follow_up_calls: list[str] = []

    async def active_operation(_target: MessageTarget) -> str:
        active_started.set()
        await release_active.wait()
        return "$active-response"

    async def follow_up_operation(source_event_id: str, _target: MessageTarget) -> str:
        follow_up_calls.append(source_event_id)
        return f"$response-{source_event_id}"

    active_task = asyncio.create_task(
        lifecycle.run_locked_response(
            target=target,
            response_envelope=active_envelope,
            pipeline_timing=None,
            locked_operation=active_operation,
        ),
    )
    await asyncio.wait_for(active_started.wait(), timeout=0.2)

    queued_items = []
    for source_event_id, sender_id in (
        ("$a1", "@alice:localhost"),
        ("$b1", "@bob:localhost"),
        ("$a2", "@alice:localhost"),
    ):
        envelope = _envelope(
            source_event_id=source_event_id,
            target=target,
            requester_id=sender_id,
            sender_id=sender_id,
        )
        reservation = lifecycle.reserve_waiting_human_message(target=target, response_envelope=envelope)
        assert reservation is not None
        queued_items.append((source_event_id, envelope, reservation))

    for _source_event_id, _envelope_item, reservation in queued_items:
        reservation.release()

    follow_up_tasks = [
        asyncio.create_task(
            lifecycle.run_locked_response(
                target=target,
                response_envelope=envelope,
                pipeline_timing=None,
                locked_operation=lambda locked_target, source_event_id=source_event_id: follow_up_operation(
                    source_event_id,
                    locked_target,
                ),
            ),
        )
        for source_event_id, envelope, reservation in queued_items
    ]
    await asyncio.sleep(0)
    assert follow_up_calls == []

    release_active.set()
    assert await active_task == "$active-response"
    assert await asyncio.gather(*follow_up_tasks) == [
        "$response-$a1",
        "$response-$b1",
        "$response-$a2",
    ]
    assert follow_up_calls == ["$a1", "$b1", "$a2"]


@pytest.mark.asyncio
async def test_non_human_lock_owner_does_not_clear_pending_human_notice(tmp_path: Path) -> None:
    """Scheduled or hook turns sharing the target lock must not clear human follow-up notices."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator.lifecycle_coordinator
    target = MessageTarget.resolve("!room:localhost", "$thread", "$active")
    active_envelope = _envelope(source_event_id="$active", target=target)
    human_envelope = _envelope(
        source_event_id="$human",
        target=target,
        dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
    )
    scheduled_envelope = _envelope(
        source_event_id="$scheduled",
        target=target,
        source_kind=SCHEDULED_SOURCE_KIND,
    )
    active_started = asyncio.Event()
    release_active = asyncio.Event()
    observed_scheduled_pending: list[set[str]] = []
    observed_human_pending: list[set[str]] = []

    async def active_operation(_target: MessageTarget) -> str:
        active_started.set()
        await release_active.wait()
        return "$active-response"

    async def scheduled_operation(_target: MessageTarget) -> str:
        observed_scheduled_pending.append(
            {message.event_id for message in lifecycle._get_or_create_queued_signal(target).pending_message_snapshot()},
        )
        return "$scheduled-response"

    async def human_operation(_target: MessageTarget) -> str:
        observed_human_pending.append(
            {message.event_id for message in lifecycle._get_or_create_queued_signal(target).pending_message_snapshot()},
        )
        return "$human-response"

    active_task = asyncio.create_task(
        lifecycle.run_locked_response(
            target=target,
            response_envelope=active_envelope,
            pipeline_timing=None,
            locked_operation=active_operation,
        ),
    )
    await asyncio.wait_for(active_started.wait(), timeout=0.2)

    human_reservation = lifecycle.reserve_waiting_human_message(target=target, response_envelope=human_envelope)
    assert human_reservation is not None
    scheduled_task = asyncio.create_task(
        lifecycle.run_locked_response(
            target=target,
            response_envelope=scheduled_envelope,
            pipeline_timing=None,
            locked_operation=scheduled_operation,
        ),
    )
    await asyncio.sleep(0)

    release_active.set()
    assert await active_task == "$active-response"
    assert await scheduled_task == "$scheduled-response"
    assert observed_scheduled_pending == [{"$human"}]
    assert {
        message.event_id for message in lifecycle._get_or_create_queued_signal(target).pending_message_snapshot()
    } == {"$human"}
    human_reservation.release()

    assert (
        await lifecycle.run_locked_response(
            target=target,
            response_envelope=human_envelope,
            pipeline_timing=None,
            locked_operation=human_operation,
        )
        == "$human-response"
    )
    assert observed_human_pending == [set()]


@pytest.mark.asyncio
async def test_reserved_command_follow_up_cleanup_when_dispatch_returns(tmp_path: Path) -> None:
    """Command-shaped active follow-ups should not leave stale queued-message state."""
    bot = _bot(tmp_path)
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    case = _reserved_follow_up_case(bot, room, event_id="$command", body="!help")
    try:
        with (
            patch.object(
                bot._turn_dispatcher,
                "_prepare_dispatch",
                new=AsyncMock(return_value=case.dispatch),
            ),
            patch("mindroom.turn_policy.TurnPolicy.plan_turn", new=AsyncMock()) as mock_plan_turn,
        ):
            await dispatch_test_turn(
                bot._turn_dispatcher,
                room,
                case.event,
                "@user:localhost",
                queued_notice_reservation=case.reservation,
            )
    finally:
        case.queued_signal.finish_response_turn()

    mock_plan_turn.assert_not_awaited()
    assert not case.queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
async def test_reserved_superseded_follow_up_cleanup_when_dispatch_returns(tmp_path: Path) -> None:
    """Superseded active follow-ups should clear their enqueue-time notice when skipped."""
    bot = _bot(tmp_path)
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    case = _reserved_follow_up_case(bot, room, event_id="$older", body="older follow-up")
    try:
        with (
            patch.object(
                bot._turn_dispatcher,
                "_prepare_dispatch",
                new=AsyncMock(return_value=case.dispatch),
            ),
            patch.object(bot._turn_dispatcher, "_has_newer_unresponded_in_thread", return_value=True),
            patch.object(bot._turn_policy, "plan_turn", new=AsyncMock()) as mock_plan_turn,
        ):
            await dispatch_test_turn(
                bot._turn_dispatcher,
                room,
                case.event,
                "@user:localhost",
                queued_notice_reservation=case.reservation,
            )
    finally:
        case.queued_signal.finish_response_turn()

    mock_plan_turn.assert_not_awaited()
    assert not case.queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
async def test_reserved_follow_up_cleanup_when_hook_suppression_returns_before_dispatch(tmp_path: Path) -> None:
    """Hook-suppressed active follow-ups should clear the reservation before planning."""
    bot = _bot(tmp_path)
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    case = _reserved_follow_up_case(bot, room, event_id="$suppressed")
    try:
        with (
            patch.object(bot._turn_dispatcher, "_prepare_dispatch", new=AsyncMock(return_value=None)),
            patch("mindroom.turn_policy.TurnPolicy.plan_turn", new=AsyncMock()) as mock_plan_turn,
        ):
            await dispatch_test_turn(
                bot._turn_dispatcher,
                room,
                case.event,
                "@user:localhost",
                queued_notice_reservation=case.reservation,
            )
    finally:
        case.queued_signal.finish_response_turn()

    mock_plan_turn.assert_not_awaited()
    assert not case.queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
async def test_reserved_follow_up_cleanup_when_plan_ignores_before_response(tmp_path: Path) -> None:
    """Ignored active follow-ups should clear the reservation before response lifecycle ownership."""
    bot = _bot(tmp_path)
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    case = _reserved_follow_up_case(bot, room, event_id="$ignored")
    try:
        with (
            patch.object(
                bot._turn_dispatcher,
                "_prepare_dispatch",
                new=AsyncMock(return_value=case.dispatch),
            ),
            patch(
                "mindroom.turn_policy.TurnPolicy.plan_turn",
                new=AsyncMock(return_value=DispatchPlan(kind="ignore")),
            ),
        ):
            await dispatch_test_turn(
                bot._turn_dispatcher,
                room,
                case.event,
                "@user:localhost",
                queued_notice_reservation=case.reservation,
            )
    finally:
        case.queued_signal.finish_response_turn()

    assert not case.queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
async def test_reserved_follow_up_cleanup_when_route_returns_before_response(tmp_path: Path) -> None:
    """Routed active follow-ups should clear the reservation after router handoff."""
    bot = _bot(tmp_path)
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    case = _reserved_follow_up_case(bot, room, event_id="$routed")
    try:
        with (
            patch.object(
                bot._turn_dispatcher,
                "_prepare_dispatch",
                new=AsyncMock(return_value=case.dispatch),
            ),
            patch(
                "mindroom.turn_policy.TurnPolicy.plan_turn",
                new=AsyncMock(return_value=DispatchPlan(kind="route", router_message="route this")),
            ),
            patch("mindroom.turns.dispatch.execute_router_relay", new_callable=AsyncMock) as mock_route,
        ):
            await dispatch_test_turn(
                bot._turn_dispatcher,
                room,
                case.event,
                "@user:localhost",
                queued_notice_reservation=case.reservation,
            )
    finally:
        case.queued_signal.finish_response_turn()

    mock_route.assert_awaited_once()
    assert not case.queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
async def test_reserved_follow_up_cleanup_when_dispatch_raises_before_lifecycle(tmp_path: Path) -> None:
    """Exceptions before response lifecycle ownership should cancel the reservation."""
    bot = _bot(tmp_path)
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    case = _reserved_follow_up_case(bot, room, event_id="$raises")
    try:
        with (
            patch.object(
                bot._turn_dispatcher,
                "_prepare_dispatch",
                new=AsyncMock(return_value=case.dispatch),
            ),
            patch(
                "mindroom.turn_policy.TurnPolicy.plan_turn",
                new=AsyncMock(
                    return_value=DispatchPlan(
                        kind="respond",
                        response_action=ResponseAction(kind="individual"),
                    ),
                ),
            ),
            patch.object(
                bot._turn_dispatcher,
                "_execute_response_action",
                new=AsyncMock(side_effect=RuntimeError("dispatch failed")),
            ),
            pytest.raises(RuntimeError, match="dispatch failed"),
        ):
            await dispatch_test_turn(
                bot._turn_dispatcher,
                room,
                case.event,
                "@user:localhost",
                queued_notice_reservation=case.reservation,
            )
    finally:
        case.queued_signal.finish_response_turn()

    assert not case.queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
async def test_reserved_follow_up_cleanup_when_dispatch_cancelled_before_lifecycle(tmp_path: Path) -> None:
    """A response task cancelled before lifecycle ownership still cancels the reservation."""
    bot = _bot(tmp_path)
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    case = _reserved_follow_up_case(bot, room, event_id="$cancelled")
    try:
        with (
            patch.object(
                bot._turn_dispatcher,
                "_prepare_dispatch",
                new=AsyncMock(return_value=case.dispatch),
            ),
            patch(
                "mindroom.turn_policy.TurnPolicy.plan_turn",
                new=AsyncMock(
                    return_value=DispatchPlan(
                        kind="respond",
                        response_action=ResponseAction(kind="individual"),
                    ),
                ),
            ),
            patch.object(
                bot._turn_dispatcher,
                "_execute_response_action",
                new=AsyncMock(side_effect=asyncio.CancelledError),
            ),
        ):
            await dispatch_test_turn(
                bot._turn_dispatcher,
                room,
                case.event,
                "@user:localhost",
                queued_notice_reservation=case.reservation,
            )
    finally:
        case.queued_signal.finish_response_turn()

    assert not case.queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
async def test_reserved_follow_up_cleanup_when_prepare_dispatch_raises(tmp_path: Path) -> None:
    """Reserved follow-ups should clean up even before PreparedDispatch can exist."""
    bot = _bot(tmp_path)
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    target = MessageTarget.resolve(room.room_id, "$thread", "$prepare")
    envelope = _envelope(
        dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        source_event_id="$prepare",
        target=target,
    )
    event = _prepared_text_event(event_id="$prepare")
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator.lifecycle_coordinator
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    queued_signal.begin_response_turn()
    try:
        reservation = lifecycle.reserve_waiting_human_message(target=target, response_envelope=envelope)
        assert reservation is not None
        with (
            patch.object(
                bot._turn_dispatcher,
                "_prepare_dispatch",
                new=AsyncMock(side_effect=RuntimeError("prepare failed")),
            ),
            pytest.raises(RuntimeError, match="prepare failed"),
        ):
            await dispatch_test_turn(
                bot._turn_dispatcher,
                room,
                event,
                "@user:localhost",
                queued_notice_reservation=reservation,
            )
    finally:
        queued_signal.finish_response_turn()

    assert not queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
async def test_reserved_follow_up_cleanup_when_handle_prepared_turn_fails_before_dispatch(
    tmp_path: Path,
) -> None:
    """Reserved follow-ups claimed by the gate should clean up if handoff fails early."""
    bot = _bot(tmp_path)
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    target = MessageTarget.resolve(room.room_id, "$thread", "$handoff")
    envelope = _envelope(
        dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        source_event_id="$handoff",
        target=target,
    )
    event = _prepared_text_event(event_id="$handoff")
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator.lifecycle_coordinator
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    queued_signal.begin_response_turn()
    try:
        reservation = lifecycle.reserve_waiting_human_message(target=target, response_envelope=envelope)
        assert reservation is not None
        batch = build_prepared_turn(
            CoalescingKey(room.room_id, "$thread", RequesterCoalescingOwner("@user:localhost")),
            [
                make_pending_event(
                    event,
                    room,
                    source_kind=MESSAGE_SOURCE_KIND,
                    active_follow_up=True,
                    notice=reservation,
                ),
            ],
            queues_other_run=nothing_else_queued,
        )
        with (
            patch.object(
                TurnDispatcher,
                "dispatch_turn",
                new=AsyncMock(side_effect=RuntimeError("dispatch failed")),
            ),
            pytest.raises(RuntimeError, match="dispatch failed"),
        ):
            await bot._turn_dispatcher.handle_prepared_turn(batch)
    finally:
        queued_signal.finish_response_turn()

    assert not queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
async def test_coalesced_batch_consumes_queued_notice_for_batch_thread(tmp_path: Path) -> None:
    """A mixed batch should consume the notices for its single coalescing target before dispatch."""
    bot = _bot(tmp_path)
    room = nio.MatrixRoom("!room:localhost", bot.matrix_id.full_id)
    pre_target = MessageTarget.resolve(room.room_id, "$pre_stt_thread", "$typed")
    post_target = MessageTarget.resolve(room.room_id, "$post_stt_thread", "$voice")
    pre_envelope = _envelope(
        dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        source_event_id="$typed",
        target=pre_target,
    )
    post_envelope = _envelope(
        source_kind=VOICE_SOURCE_KIND,
        dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        source_event_id="$voice",
        target=post_target,
    )
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator.lifecycle_coordinator
    pre_signal = lifecycle._get_or_create_queued_signal(pre_target)
    post_signal = lifecycle._get_or_create_queued_signal(post_target)
    typed_event = _prepared_text_event(event_id="$typed")
    voice_event = replace(
        _prepared_text_event(event_id="$voice"),
        body="🎤 voice transcript",
        payload_trusted=True,
    )
    captured_dispatches: list[str] = []

    async def capture_dispatch(*_args: object, **kwargs: object) -> None:
        del kwargs
        assert pre_signal.pending_human_messages == 0
        assert post_signal.pending_human_messages == 0
        captured_dispatches.append("dispatched")

    pre_signal.begin_response_turn()
    post_signal.begin_response_turn()
    try:
        pre_reservation = lifecycle.reserve_waiting_human_message(target=pre_target, response_envelope=pre_envelope)
        post_reservation = lifecycle.reserve_waiting_human_message(target=post_target, response_envelope=post_envelope)
        assert pre_reservation is not None
        assert post_reservation is not None
        batch = build_prepared_turn(
            CoalescingKey(room.room_id, "$post_stt_thread", RequesterCoalescingOwner("@user:localhost")),
            [
                make_pending_event(
                    typed_event,
                    room,
                    source_kind=MESSAGE_SOURCE_KIND,
                    active_follow_up=True,
                    notice=pre_reservation,
                ),
                make_pending_event(
                    voice_event,
                    room,
                    source_kind=VOICE_SOURCE_KIND,
                    active_follow_up=True,
                    notice=post_reservation,
                ),
            ],
            queues_other_run=nothing_else_queued,
        )

        with patch.object(
            TurnDispatcher,
            "dispatch_turn",
            new=AsyncMock(side_effect=prepared_turn_recorder(capture_dispatch)),
        ):
            await bot._turn_dispatcher.handle_prepared_turn(batch)
    finally:
        pre_signal.finish_response_turn()
        post_signal.finish_response_turn()

    assert captured_dispatches == ["dispatched"]
    assert pre_signal.pending_human_messages == 0
    assert post_signal.pending_human_messages == 0


@pytest.mark.asyncio
async def test_room_scoped_root_voice_consumes_final_target_queued_notice(tmp_path: Path) -> None:
    """A room-scoped voice root should consume the notice for its final target root before dispatch."""
    bot = _bot(tmp_path)
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    voice_event = replace(
        _prepared_text_event(event_id="$voice-root"),
        body="🎤 voice follow-up",
        payload_trusted=True,
    )
    target = bot._turn_ingress.deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id=None,
        reply_to_event_id=voice_event.event_id,
        event_source=voice_event.source,
    )
    assert target.resolved_thread_id == "$voice-root"
    envelope = _envelope(
        source_kind=VOICE_SOURCE_KIND,
        dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        source_event_id=voice_event.event_id,
        target=target,
    )
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator.lifecycle_coordinator
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    captured_dispatches: list[str] = []

    async def capture_dispatch(*_args: object, **kwargs: object) -> None:
        del kwargs
        assert queued_signal.pending_human_messages == 0
        captured_dispatches.append("dispatched")

    queued_signal.begin_response_turn()
    try:
        voice_reservation = lifecycle.reserve_waiting_human_message(target=target, response_envelope=envelope)
        assert voice_reservation is not None
        batch = build_prepared_turn(
            CoalescingKey(room.room_id, None, RequesterCoalescingOwner("@user:localhost")),
            [
                make_pending_event(
                    voice_event,
                    room,
                    source_kind=VOICE_SOURCE_KIND,
                    active_follow_up=True,
                    notice=voice_reservation,
                ),
            ],
            queues_other_run=nothing_else_queued,
        )

        with patch.object(
            TurnDispatcher,
            "dispatch_turn",
            new=AsyncMock(side_effect=prepared_turn_recorder(capture_dispatch)),
        ):
            await bot._turn_dispatcher.handle_prepared_turn(batch)
    finally:
        queued_signal.finish_response_turn()

    assert captured_dispatches == ["dispatched"]
    assert queued_signal.pending_human_messages == 0
    assert not queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
async def test_active_follow_up_reservation_cancelled_when_enqueue_is_cancelled(tmp_path: Path) -> None:
    """Cancellation during enqueue handoff should not leak the reserved notice."""
    bot = _bot(tmp_path)
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    target = MessageTarget.resolve(room.room_id, "$thread", "$cancelled")
    envelope = _envelope(source_kind=MESSAGE_SOURCE_KIND, source_event_id="$cancelled", target=target)
    event = _prepared_text_event(event_id="$cancelled")
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator.lifecycle_coordinator
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    queued_signal.begin_response_turn()
    reserved_before_enqueue: list[int] = []

    async def cancel_enqueue(*_args: object, **_kwargs: object) -> None:
        reserved_before_enqueue.append(queued_signal.pending_human_messages)
        raise asyncio.CancelledError

    try:
        ticket = bot._turn_ingress.reserve_prompt_ingress_order(room, "@user:localhost")
        try:
            with (
                patch.object(bot._turn_ingress, "_enqueue_for_dispatch", new=cancel_enqueue),
                pytest.raises(asyncio.CancelledError),
            ):
                await bot._turn_ingress._enqueue_prepared_ingress(
                    room,
                    event,
                    facts=make_ingress_facts(event.sender),
                    target=target,
                    envelope=envelope,
                    coalescing_thread_id="$thread",
                    ticket=ticket,
                )
        finally:
            ticket.close()
    finally:
        queued_signal.finish_response_turn()

    assert reserved_before_enqueue == [1]

    assert queued_signal.pending_human_messages == 0
    assert not queued_signal.has_pending_human_messages()


def test_queued_human_reservation_is_idempotent() -> None:
    """Reservation consume/cancel operations should clear the notice at most once."""
    target = MessageTarget.resolve("!room:localhost", "$thread", "$event")
    envelope = _envelope(
        dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        source_event_id="$event",
        target=target,
    )
    lifecycle = ResponseLifecycleCoordinator()
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    queued_signal.begin_response_turn()
    try:
        reservation = lifecycle.reserve_waiting_human_message(target=target, response_envelope=envelope)
        assert reservation is not None
        assert queued_signal.pending_human_messages == 1
        reservation.release()
        reservation.release()
        assert queued_signal.pending_human_messages == 0
        # A later notice for the same message survives a stale release of the first.
        later = lifecycle.reserve_waiting_human_message(target=target, response_envelope=envelope)
        assert later is not None
        reservation.release()
        assert queued_signal.pending_human_messages == 1
        later.release()
    finally:
        queued_signal.finish_response_turn()

    assert not queued_signal.has_pending_human_messages()


def test_managed_message_does_not_reserve_queued_human_notice() -> None:
    """Managed chatter should not count as a queued human follow-up."""
    target = MessageTarget.resolve("!room:localhost", "$thread", "$event")
    envelope = _envelope(
        source_event_id="$event",
        target=target,
        sender_id="@mindroom_general:localhost",
        requester_id="@mindroom_general:localhost",
        origin=message_origin(
            sender_id="@mindroom_general:localhost",
            requester_id="@mindroom_general:localhost",
            sender_entity_name="general",
            requester_entity_name="general",
            source_kind=MESSAGE_SOURCE_KIND,
        ),
    )
    lifecycle = ResponseLifecycleCoordinator()
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    queued_signal.begin_response_turn()
    try:
        reservation = lifecycle.reserve_waiting_human_message(target=target, response_envelope=envelope)
        assert reservation is None
        assert queued_signal.pending_human_messages == 0
    finally:
        queued_signal.finish_response_turn()

    assert not queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
async def test_lifecycle_notice_is_cancelled_when_lock_wait_is_cancelled() -> None:
    """A lifecycle-owned notice should not leak if lock acquisition is cancelled."""
    target = MessageTarget.resolve("!room:localhost", "$thread", "$event")
    envelope = _envelope(
        dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        source_event_id="$event",
        target=target,
    )
    lifecycle = ResponseLifecycleCoordinator()
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    queued_signal.begin_response_turn()
    reservation = lifecycle.reserve_waiting_human_message(target=target, response_envelope=envelope)
    assert reservation is not None
    assert queued_signal.pending_human_messages == 1
    reservation.release()

    lock = lifecycle._response_lifecycle_lock(target)
    await lock.acquire()

    async def locked_operation(_target: MessageTarget) -> SourceFate:
        msg = "lock wait should be cancelled before the operation runs"
        raise AssertionError(msg)

    try:
        task = asyncio.create_task(
            lifecycle.run_locked_response(
                target=target,
                response_envelope=envelope,
                pipeline_timing=None,
                locked_operation=locked_operation,
            ),
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        lock.release()
        queued_signal.finish_response_turn()

    assert queued_signal.pending_human_messages == 0
    assert not queued_signal.has_pending_human_messages()
    assert not queued_signal.has_active_response_turn()


@pytest.mark.asyncio
async def test_coalesced_dispatch_never_creates_queued_signal(tmp_path: Path) -> None:
    """Messages dropped by coalescing should not create false mid-turn notifications."""
    bot = _bot(tmp_path)
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    event = _prepared_text_event(event_id="$older")
    envelope = _envelope()
    dispatch = PreparedDispatch(
        requester_user_id="@user:localhost",
        context=_message_context(),
        target=envelope.target,
        correlation_id="corr",
        envelope=envelope,
    )

    with (
        patch.object(bot._inbound_turn_normalizer, "resolve_text_event", new=AsyncMock(return_value=event)),
        patch.object(
            bot._turn_dispatcher,
            "_prepare_dispatch",
            new=AsyncMock(return_value=dispatch),
        ),
        patch.object(bot._turn_dispatcher, "_has_newer_unresponded_in_thread", return_value=True),
        patch.object(
            bot._turn_policy,
            "plan_turn",
            new=AsyncMock(return_value=DispatchPlan(kind="ignore")),
        ) as mock_plan,
    ):
        await dispatch_test_turn(
            bot._turn_dispatcher,
            room,
            event,
            "@user:localhost",
        )

    assert not await bot._turn_store.is_handled(room.room_id, "$older")
    mock_plan.assert_not_awaited()
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    assert coordinator.lifecycle_coordinator._thread_queued_signals == {}


@pytest.mark.asyncio
async def test_notice_hook_keeps_single_notice_at_end() -> None:
    """The injected notice should stay unique, remain last, and avoid double wrapping.

    A batch whose result stops the run reaches no batch hook; the loop owns that rule.
    """
    plain_messages = [OwnedMessage(role="user", content="hello")]
    await _after_batch(plain_messages, OwnedMessage(role="tool", content="result"))
    assert _notice_count(plain_messages) == 0

    with queued_message_signal_context(_StaticQueuedState(pending=True)) as notice_context:
        queued_messages = [OwnedMessage(role="user", content="hello")]
        await _after_batch(queued_messages, OwnedMessage(role="tool", content="result"))
        await _after_batch(queued_messages, OwnedMessage(role="tool", content="another result"))

    with queued_message_signal_context(_StaticQueuedState(pending=True)) as next_notice_context:
        next_turn_messages = [OwnedMessage(role="user", content="hello")]
        await _after_batch(next_turn_messages, OwnedMessage(role="tool", content="result"))

    assert _notice_count(queued_messages) == 1
    assert queued_messages[-1].content == QUEUED_MESSAGE_NOTICE_TEXT
    assert queued_messages[-1].provider_data == {
        _QUEUED_NOTICE_MARKER_KEY: True,
        _QUEUED_NOTICE_RESPONSE_TURN_ID_KEY: notice_context.response_turn_id,
    }
    assert _notice_count(next_turn_messages) == 1
    assert next_turn_messages[-1].content == QUEUED_MESSAGE_NOTICE_TEXT
    assert next_notice_context.response_turn_id != notice_context.response_turn_id


def test_default_queued_notice_requires_pause_handoff() -> None:
    """The default notice should require an explicit pause handoff and continuation default."""
    assert QUEUED_MESSAGE_NOTICE_TEXT == (
        "[SYSTEM NOTICE — PAUSE FOR A NEWER USER MESSAGE] A newer user message arrived in this thread "
        "while you were working. This is a PAUSE, not a cancellation of the current task. Do not make "
        "any new tool calls. End this turn now with a final text response that explicitly says a newer "
        "message arrived and states: (1) what you completed, (2) what remains unfinished, and (3) the "
        "next step. You cannot see the newer message's contents yet. If work remains, state that you "
        "intend to resume it on the next turn, subject to the newer message's instructions."
    )


@pytest.mark.asyncio
async def test_notice_hook_uses_configured_notice_text() -> None:
    """Queued-message notices should use the configured hidden prompt text."""
    with queued_message_signal_context(_StaticQueuedState(pending=True)) as notice_context:
        messages = [OwnedMessage(role="user", content="hello")]
        await _after_batch(messages, OwnedMessage(role="tool", content="result"), notice_text="Custom queued notice.")

    assert messages[-1].content == "Custom queued notice."
    assert messages[-1].provider_data == {
        _QUEUED_NOTICE_MARKER_KEY: True,
        _QUEUED_NOTICE_RESPONSE_TURN_ID_KEY: notice_context.response_turn_id,
    }


@pytest.mark.asyncio
async def test_notice_hook_scopes_dedupe_to_response_turn_id() -> None:
    """Tool-round dedupe should remove only notices owned by the active response."""
    with queued_message_signal_context(_StaticQueuedState(pending=True)) as notice_context:
        prior_notice = _owned_notice("prior-response", from_history=True)
        current_stale_notice = _owned_notice(notice_context.response_turn_id)
        messages = [prior_notice, current_stale_notice]
        await _after_batch(messages, OwnedMessage(role="tool", content="result"))

    assert messages[0] is prior_notice
    assert all(message is not current_stale_notice for message in messages)
    assert _notice_count(messages, response_turn_id="prior-response") == 1
    assert _notice_count(messages, response_turn_id=notice_context.response_turn_id) == 1


@pytest.mark.asyncio
async def test_notice_hook_preserves_a_prior_replayed_notice() -> None:
    """A prior reply's replayed notice should survive later tool-result formatting byte-for-byte."""
    prior_notice = _owned_notice("prior-response", from_history=True)
    original = prior_notice.to_dict()

    with queued_message_signal_context(_StaticQueuedState(pending=True)) as notice_context:
        messages = [prior_notice]
        for index in range(2):
            await _after_batch(messages, OwnedMessage(role="tool", content=f"result {index}"))

    assert prior_notice.to_dict() == original
    assert _notice_count(messages, response_turn_id="prior-response") == 1
    assert _notice_count(messages, response_turn_id=notice_context.response_turn_id) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("waiting", [True, False])
async def test_a_replayed_notice_of_the_same_reply_moves_last_only_while_a_message_waits(*, waiting: bool) -> None:
    """A run replaying its reply's stored notice moves that copy last while a newer message waits, and adds none."""
    with queued_message_signal_context(_StaticQueuedState(pending=waiting)) as notice_context:
        stored = _owned_notice(notice_context.response_turn_id, from_history=True)
        messages = [stored, OwnedMessage(role="user", content="Continue the same task.")]
        await _after_batch(messages, OwnedMessage(role="tool", content="result"))

    assert _notice_count(messages) == 1
    assert (messages[-1] is stored) is waiting
    assert stored.from_history


@pytest.mark.asyncio
async def test_notice_reinjects_at_end_across_multiple_tool_rounds() -> None:
    """Repeated tool rounds should keep exactly one queued notice at the end of the prompt."""
    with queued_message_signal_context(_StaticQueuedState(pending=True)):
        messages = [OwnedMessage(role="user", content="hello")]
        for index in range(5):
            await _after_batch(messages, OwnedMessage(role="tool", content=f"result {index}"))

            assert _notice_count(messages) == 1
            assert messages[-1].content == QUEUED_MESSAGE_NOTICE_TEXT


@pytest.mark.asyncio
async def test_queued_notice_logs_once_per_response_turn() -> None:
    """Repeated successful appends should emit one injection event for the response."""
    with (
        capture_logs() as logs,
        queued_message_signal_context(
            _StaticQueuedState(pending=True),
        ) as notice_context,
    ):
        messages = [OwnedMessage(role="user", content="hello")]
        for index in range(5):
            await _after_batch(messages, OwnedMessage(role="tool", content=f"result {index}"))

    injection_logs = [entry for entry in logs if entry.get("event") == "queued_message_notice_injected"]
    assert injection_logs == [
        {
            "event": "queued_message_notice_injected",
            "log_level": "info",
            "response_turn_id": notice_context.response_turn_id,
        },
    ]

    with capture_logs() as no_injection_logs, queued_message_signal_context(_StaticQueuedState(pending=False)):
        await _after_batch([], OwnedMessage(role="tool", content="result"))

    assert not any(entry.get("event") == "queued_message_notice_injected" for entry in no_injection_logs)


@pytest.mark.asyncio
async def test_notice_follows_a_tool_batch_media_follow_up_message() -> None:
    """The loop appends a tool's media follow-up before the batch hooks run, so the queued notice stays last."""

    def generate() -> ToolResult:
        """Generate an image."""
        return ToolResult(content="generated image", images=[Image(url="https://example.com/image.png")])

    model = ScriptedModel([calls(tool_call("generate", "call_1")), answer("Done")])
    with queued_message_signal_context(_StaticQueuedState(pending=True)):
        await collect(
            run_agent(
                spec(model, toolkit(generate)),
                [OwnedMessage(role="user", content="hello")],
                ctx=context(),
                hooks=LoopHooks(after_tool_batch=(queued_notice_after_tool_batch(QUEUED_MESSAGE_NOTICE_TEXT),)),
                persist=None,
                request_template=EMPTY_REQUEST,
            ),
        )

    messages = list(model.requests[-1].messages)
    assert _notice_count(messages) == 1
    assert messages[-2].role == "user"
    assert messages[-2].images
    assert messages[-1].content == QUEUED_MESSAGE_NOTICE_TEXT


def test_notice_provider_data_survives_mindroom_sqlite_round_trip(tmp_path: Path) -> None:
    """MindRoom's prompt-sanitizing SQLite storage should preserve notice ownership metadata."""
    response_turn_id = "response-1"
    _seed(_queued_notice_storage(tmp_path), _completed("run-1", [_owned_notice(response_turn_id)]))

    session = _queued_notice_storage(tmp_path).load_session("session-1")

    assert session is not None
    stored_notice = session.runs[0].messages[0]
    assert stored_notice.content == QUEUED_MESSAGE_NOTICE_TEXT
    assert stored_notice.provider_data == {
        _QUEUED_NOTICE_MARKER_KEY: True,
        _QUEUED_NOTICE_RESPONSE_TURN_ID_KEY: response_turn_id,
    }
    assert stored_notice.from_history is False


async def _persist_notice_bearing_response(tmp_path: Path) -> str:
    lifecycle = ResponseLifecycleCoordinator()
    target = MessageTarget.resolve("!room:localhost", "$thread", "$event-1")
    envelope = _envelope(source_event_id="$event-1", target=target)
    response_1_ids: list[str] = []

    async def response_1_operation(_target: MessageTarget) -> str:
        lifecycle._get_or_create_queued_signal(target).add_waiting_human_message("$newer")
        messages = [OwnedMessage(role="user", content="First request")]
        await _after_batch(messages, OwnedMessage(role="tool", content="First tool result"))
        notices = _notice_messages(messages)
        assert len(notices) == 1
        provider_data = notices[0].provider_data
        assert isinstance(provider_data, dict)
        response_turn_id = provider_data[_QUEUED_NOTICE_RESPONSE_TURN_ID_KEY]
        assert isinstance(response_turn_id, str)
        response_1_ids.append(response_turn_id)
        messages.append(OwnedMessage(role="assistant", content="First response handoff"))
        store = _queued_notice_storage(tmp_path)
        _seed(store, _completed("run-1", messages))
        return "First response handoff"

    response_1 = await lifecycle.run_locked_response(
        target=target,
        response_envelope=envelope,
        pipeline_timing=None,
        locked_operation=response_1_operation,
    )

    assert response_1 == "First response handoff"
    assert len(response_1_ids) == 1
    return response_1_ids[0]


@pytest.mark.asyncio
async def test_prior_notice_survives_actual_next_provider_request_and_tool_round(
    tmp_path: Path,
) -> None:
    """The exact notice should survive real SQLite replay and next-response formatting."""
    response_1_id = await _persist_notice_bearing_response(tmp_path)

    store = _queued_notice_storage(tmp_path)
    session = store.load_session("session-1")
    assert session is not None
    model = ScriptedModel([answer("ok")])
    history = scope_history_messages(
        session.runs,
        scope=HistoryScope(kind="agent", scope_id="general"),
        num_history_runs=10,
        num_history_messages=None,
        max_tool_calls_from_history=None,
    )
    with queued_message_signal_context(_StaticQueuedState(pending=False)) as response_2_context:
        await collect(
            run_agent(
                spec(model),
                build_request(
                    summary=None,
                    history=history,
                    turn=TurnMessages(prompt=OwnedMessage(role="user", content="Second request")),
                    media=MediaInputs(),
                ),
                ctx=context(run_id="run-2", session_id="session-1"),
                persist=persist_to(store, session),
                request_template=EMPTY_REQUEST,
            ),
        )
        seen = list(model.requests[0].messages)
        prior = next(message for message in seen if message.content == QUEUED_MESSAGE_NOTICE_TEXT)
        assert prior.from_history is True
        assert prior.provider_data == {
            _QUEUED_NOTICE_MARKER_KEY: True,
            _QUEUED_NOTICE_RESPONSE_TURN_ID_KEY: response_1_id,
        }
        assert [
            (message.role, message.content)
            for message in seen
            if message.content in {QUEUED_MESSAGE_NOTICE_TEXT, "First response handoff", "Second request"}
        ] == [
            ("user", QUEUED_MESSAGE_NOTICE_TEXT),
            ("assistant", "First response handoff"),
            ("user", "Second request"),
        ]
        original_prior = prior.to_dict()

        await _after_batch(seen, OwnedMessage(role="tool", content="Second tool result"))

        assert prior.to_dict() == original_prior
        assert _notice_count(seen, response_turn_id=response_1_id) == 1
        assert _notice_count(seen, response_turn_id=response_2_context.response_turn_id) == 0

    persisted = _queued_notice_storage(tmp_path).load_session("session-1")
    assert persisted is not None
    response_1_run, response_2_run = persisted.runs
    assert response_2_run.content == "ok"
    assert all(message.content != QUEUED_MESSAGE_NOTICE_TEXT for message in response_2_run.messages)
    persisted_notices = _notice_messages(response_1_run.messages, response_turn_id=response_1_id)
    assert [message.content for message in persisted_notices] == [QUEUED_MESSAGE_NOTICE_TEXT]


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_media", [False, True])
@pytest.mark.parametrize("selection", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("reaction", [None, "👀"])
async def test_response_runner_binds_agent_mid_turn_judge(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pending_media: bool,
    selection: bool,
    enabled: bool,
    reaction: str | None,
) -> None:
    """Agent opt-in reaches the real tool loop while unprepared media keeps wrap-up."""
    bot = _bot(tmp_path)
    bot.client.room_send.return_value = nio.RoomSendResponse.from_dict({"event_id": "$reaction"}, "!room:localhost")
    runner = unwrap_extracted_collaborator(bot._response_runner)
    runner.deps.runtime.config.agents[bot.agent_name].mid_turn = (
        MidTurnConfig(
            judgment=LLMJudgmentConfig(provider="llm", model="default"),
            defer_reaction=reaction,
        )
        if enabled
        else None
    )
    judge = ScriptedModel([answer('{"decision": false}')])
    install_model_providers(monkeypatch, {"default": judge})
    envelope = _envelope(target=MessageTarget.resolve("!room:localhost", "$thread", "$event"))
    prompt = "Question: Install the dependency?\nSelected option: yes (install)" if selection else "hello"
    if selection:
        envelope = replace(envelope, body="The user selected: yes")
    preparation = _payload_preparation(envelope.target)
    if pending_media:
        preparation = replace(preparation, payload_inputs=replace(preparation.payload_inputs, raw_audio_fallback=True))
    request = _request(
        envelope,
        prompt=prompt,
        user_id=None,
        payload_preparation=preparation if pending_media else None,
    )
    monkeypatch.setattr(
        runner.deps.resolver,
        "fetch_thread_history",
        AsyncMock(
            return_value=ThreadHistoryResult(
                [
                    ResolvedVisibleMessage.synthetic(sender="@user:localhost", body="Do the task", event_id="$thread"),
                    ResolvedVisibleMessage.synthetic(sender="@user:localhost", body=prompt, event_id="$event"),
                ],
                is_full_history=True,
            ),
        ),
    )
    reservations = []

    def note_progress(text: str) -> None:
        progress = runner.lifecycle_coordinator.visible_progress_callback(envelope.target)
        assert (progress is not None) is enabled
        if progress is not None:
            progress(text, ())

    def queue_followup() -> str:
        """Queue a follow-up while the turn works."""
        reservation = runner.reserve_waiting_human_message(
            target=envelope.target,
            response_envelope=replace(envelope, source_event_id="$queued", body="Thanks"),
        )
        assert reservation is not None
        reservations.append(reservation)
        note_progress("Later text that the user had not seen when sending")
        return "Completed"

    model = ScriptedModel([calls(tool_call("queue_followup", "call")), answer("Done")])

    async def locked_operation(_target: MessageTarget) -> SourceFate:
        await runner._prepare_request_after_lock(replace(request, payload_preparation=None))
        note_progress("I have located the requested file")
        await collect(
            run_agent(
                spec(model, toolkit(queue_followup)),
                [OwnedMessage(role="user", content="Do the task")],
                ctx=context(),
                hooks=LoopHooks(after_tool_batch=(queued_notice_after_tool_batch("WRAP UP NOW"),)),
                persist=None,
                request_template=EMPTY_REQUEST,
            ),
        )
        return Owned()

    try:
        assert (
            await runner._run_locked_response_lifecycle(
                request,
                response_kind="agent",
                locked_operation=locked_operation,
            )
            == Owned()
        )
        assert len(model.requests) == 2
        reactions = [
            call.kwargs["content"]
            for call in bot.client.room_send.await_args_list
            if call.kwargs["message_type"] == "m.reaction"
        ]
        assert reactions == (
            [{"m.relates_to": {"rel_type": "m.annotation", "event_id": "$queued", "key": "👀"}}]
            if enabled and not pending_media and reaction
            else []
        )
        assert any(message.content == "WRAP UP NOW" for message in model.requests[-1].messages) is (
            pending_media or not enabled
        )
        assert len(judge.requests) == (1 if enabled and not pending_media else 0)
        assert runner.lifecycle_coordinator.visible_progress_callback(envelope.target) is None
        if enabled and not pending_media:
            evidence = "\n".join(str(message.content) for message in judge.requests[0].messages)
            assert "I have located the requested file" in evidence
            assert "Later text" not in evidence
            if selection:
                assert "Install the dependency?" in evidence
                assert "yes (install)" in evidence
    finally:
        for reservation in reservations:
            reservation.release()
