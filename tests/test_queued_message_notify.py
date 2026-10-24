"""Regression tests for queued-message mid-turn notifications."""

from __future__ import annotations

import asyncio
import threading
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, Protocol, Self, cast
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import nio
import pytest
from agno.media import Image
from agno.tools.function import ToolResult
from structlog.testing import capture_logs

from mindroom import ai_runtime
from mindroom.agent_loop.model_loop import LoopHooks
from mindroom.agent_loop.run import run_agent
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.ai_runtime import (
    queued_message_signal_context,
    queued_notice_after_tool_batch,
    register_queued_notice_storage,
)
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
from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_handoff import PendingDispatchMetadata, PreparedIngress
from mindroom.dispatch_source import (
    ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
    HOOK_DISPATCH_SOURCE_KIND,
    HOOK_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    SCHEDULED_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    VOICE_SOURCE_KIND,
)
from mindroom.entity_resolution import current_internal_sender_ids
from mindroom.final_delivery import FinalDeliveryOutcome
from mindroom.history.request_messages import TurnMessages, build_request, scope_history_messages
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.types import HistoryScope
from mindroom.hooks import MessageEnvelope
from mindroom.interactive import InteractiveMetadata
from mindroom.interactive_models import InteractivePrompt
from mindroom.judgment import llm as judgment_llm
from mindroom.llm.messages import Message as OwnedMessage
from mindroom.llm.runs import AnyRunRecord, RunRecord, TeamRunRecord
from mindroom.llm.runs import RunStatus as OwnedRunStatus
from mindroom.matrix.client import ResolvedVisibleMessage
from mindroom.matrix.conversation_hydration import HYDRATED_PROMPT_WINDOW_MESSAGES
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget, ResponseLifecycleKey
from mindroom.mid_turn import QueuedMessage
from mindroom.post_response_effects import (
    PostResponseEffectsDeps,
    PostResponseEffectsSupport,
    ResponseOutcome,
    apply_post_response_effects,
)
from mindroom.prompts import QUEUED_MESSAGE_NOTICE_TEXT
from mindroom.response_lifecycle import ResponseLifecycleCoordinator, _QueuedMessageState
from mindroom.response_payload_preparation import DispatchPayloadInputs, ResponsePayloadPreparation
from mindroom.response_runner import (
    PostLockRequestPreparationError,
    ResponseRequest,
    ResponseRunner,
    _ResponseGenerationOutcome,
)
from mindroom.response_sources import ResponseSources
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.store import ConversationStore, persist_to
from mindroom.session_storage.types import ConversationSession, SessionKind
from mindroom.turn_controller import _PrecheckedEvent
from mindroom.turn_policy import PreparedDispatch, ResponseAction, _DispatchPlan
from tests.access_schema_support import with_current_room_member_access
from tests.agent_loop.helpers import collect, context, spec, toolkit
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_journal_support,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_membership_stub,
    make_pending_event,
    message_origin,
    prepared_dispatch_result,
    request_envelope,
    runtime_paths_for,
    seed_session,
    serve_conversation_reader,
    test_runtime_paths,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.turn_dispatch_helpers import dispatch_test_turn, prepared_turn_recorder

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterable
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.delivery_gateway import FinalDeliveryRequest
    from mindroom.turn_origin import TurnOrigin


class _ReservationLike(Protocol):
    def consume(self) -> None:
        """Consume the reserved queued-human notice."""

    def cancel(self) -> None:
        """Release the reserved queued-human notice."""


class _NoopResponseLifecycle:
    def __init__(self) -> None:
        self.session_thread_ids: list[str | None] = []

    async def setup_session_watch(self, **kwargs: object) -> object:
        thread_id = kwargs["thread_id"]
        assert isinstance(thread_id, str) or thread_id is None
        self.session_thread_ids.append(thread_id)
        return object()

    async def emit_session_started(self, _watch: object) -> None:
        return

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
    bot = make_test_agent_bot(agent_user, tmp_path, config, runtime_paths_for(config), rooms=["!room:localhost"])
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
    lifecycle = coordinator._lifecycle_coordinator
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


def _queued_notice_metadata(reservation: _ReservationLike) -> tuple[PendingDispatchMetadata, ...]:
    return (
        PendingDispatchMetadata(
            kind="queued_notice_reservation",
            payload=reservation,
            close=reservation.cancel,
        ),
    )


def _targeted_queued_notice_metadata(
    reservation: _ReservationLike,
    target: MessageTarget,
) -> tuple[PendingDispatchMetadata, ...]:
    return (
        PendingDispatchMetadata(
            kind="queued_notice_reservation",
            payload=reservation,
            close=reservation.cancel,
            target_key=target.lifecycle_key,
        ),
    )


def test_response_lifecycle_rejects_mismatched_reservation_target(tmp_path: Path) -> None:
    """Queued notices should use the envelope's canonical lifecycle target."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator._lifecycle_coordinator
    envelope = _envelope()
    mismatched_target = MessageTarget.resolve("!other:localhost", None, "$event")

    with pytest.raises(ValueError, match=r"MessageEnvelope\.target"):
        lifecycle.reserve_waiting_human_message(
            target=mismatched_target,
            response_envelope=envelope,
        )


@pytest.mark.asyncio
async def test_response_lifecycle_rejects_mismatched_locked_response_target(tmp_path: Path) -> None:
    """Response locking should use the envelope's canonical lifecycle target."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator._lifecycle_coordinator
    envelope = _envelope()
    mismatched_target = MessageTarget.resolve("!other:localhost", None, "$event")

    async def locked_operation(_target: MessageTarget) -> str:
        return "$response"

    with pytest.raises(ValueError, match=r"MessageEnvelope\.target"):
        await lifecycle.run_locked_response(
            target=mismatched_target,
            response_envelope=envelope,
            pipeline_timing=None,
            locked_operation=locked_operation,
        )


_QUEUED_NOTICE_MARKER_KEY = "mindroom_queued_message_notice"
_QUEUED_NOTICE_RESPONSE_TURN_ID_KEY = "mindroom_queued_message_notice_response_turn_id"


def _notice_messages(
    messages: list[OwnedMessage],
    *,
    marker: bool | str | None = None,
    response_turn_id: str | None = None,
) -> list[OwnedMessage]:
    matches: list[OwnedMessage] = []
    for message in messages:
        provider_data = message.provider_data
        if not isinstance(provider_data, dict):
            continue
        notice_marker = provider_data.get(_QUEUED_NOTICE_MARKER_KEY)
        if notice_marker not in (True, "persisted"):
            continue
        if marker is not None and notice_marker != marker:
            continue
        if response_turn_id is not None and provider_data.get(_QUEUED_NOTICE_RESPONSE_TURN_ID_KEY) != response_turn_id:
            continue
        matches.append(message)
    return matches


def _notice_count(
    messages: list[OwnedMessage],
    *,
    marker: bool | str | None = None,
    response_turn_id: str | None = None,
) -> int:
    return len(
        _notice_messages(
            messages,
            marker=marker,
            response_turn_id=response_turn_id,
        ),
    )


def _queued_notice_storage(tmp_path: Path) -> ConversationStore:
    return create_state_storage("queued_notice", tmp_path)


async def _after_batch(
    messages: list[OwnedMessage],
    *results: OwnedMessage,
    notice_text: str = QUEUED_MESSAGE_NOTICE_TEXT,
) -> None:
    """Append one tool batch's results and run the queued-notice hook a reply installs after it, as the loop does."""
    messages.extend(results)
    await queued_notice_after_tool_batch(notice_text)(messages, results)


def _owned_notice(response_turn_id: str, *, marker: bool | str = True, from_history: bool = False) -> OwnedMessage:
    return OwnedMessage(
        role="user",
        content=QUEUED_MESSAGE_NOTICE_TEXT,
        provider_data={_QUEUED_NOTICE_MARKER_KEY: marker, _QUEUED_NOTICE_RESPONSE_TURN_ID_KEY: response_turn_id},
        from_history=from_history,
    )


@dataclass(frozen=True, slots=True)
class _RecordingStore(ConversationStore):
    """A conversation store that records the runs each save writes and can hold each session load until released."""

    held: threading.Event | None = None
    loading: threading.Event = field(default_factory=threading.Event)
    saves: list[list[str]] = field(default_factory=list)

    def load_session(self, session_id: str, kind: SessionKind) -> ConversationSession | None:
        self.loading.set()
        if self.held is not None:
            assert self.held.wait(timeout=1)
        return ConversationStore.load_session(self, session_id, kind)

    def save_runs(
        self,
        session: ConversationSession,
        runs: Iterable[AnyRunRecord],
        *,
        state: dict[str, Any] | None = None,
    ) -> None:
        written = list(runs)
        self.saves.append([run.run_id for run in written])
        ConversationStore.save_runs(self, session, written, state=state)


def _recording_store(tmp_path: Path, name: str, *, held: threading.Event | None = None) -> _RecordingStore:
    store = create_state_storage(name, tmp_path)
    return _RecordingStore(store.path, store.names, store.prompt_roles, held=held)


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
            updated_at=1,
        ),
    )


def _stored_runs(
    store: ConversationStore,
    kind: SessionKind = "agent",
    *,
    session_id: str = "session-1",
) -> list[AnyRunRecord]:
    session = ConversationStore.load_session(store, session_id, kind)
    assert session is not None
    return session.runs


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
            dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
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
            dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
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
            dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        ),
    ]

    batch = build_prepared_turn(
        active_follow_up_coalescing_key(room.room_id, "$thread"),
        pending_events,
    )

    assert batch.handled_turn.source_event_ids == ("$a1", "$a2", "$a3")
    assert batch.requester_user_id == "@alice:localhost"
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
    target = MessageTarget.resolve(room.room_id, None, "$a1", room_mode=True)
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
                dispatch_metadata=_targeted_queued_notice_metadata(first_reservation, target),
            ),
            make_pending_event(
                second_event,
                room,
                source_kind=MESSAGE_SOURCE_KIND,
                dispatch_metadata=_targeted_queued_notice_metadata(second_reservation, target),
            ),
        ],
    )

    with patch("mindroom.turn_controller.dispatch_text_message", new=AsyncMock()):
        await bot._turn_controller.handle_prepared_turn(batch)

    first_reservation.consume.assert_called_once_with()
    second_reservation.consume.assert_called_once_with()
    first_reservation.cancel.assert_not_called()
    second_reservation.cancel.assert_not_called()


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
            terminal_status="cancelled",
            event_id=None,
            suppressed=True,
        ),
        ResponseOutcome(
            response_target=MessageTarget.resolve(
                room_id="!room:localhost",
                thread_id="$thread",
                reply_to_event_id="$event",
            ),
            thread_summary_room_id="!room:localhost",
            thread_summary_thread_id="$thread",
            thread_summary_message_count_hint=3,
        ),
        PostResponseEffectsDeps(
            logger=MagicMock(),
            queue_thread_summary=queue_thread_summary,
        ),
    )

    queue_thread_summary.assert_not_called()


@pytest.mark.parametrize(
    ("terminal_status", "run_succeeded"),
    [
        ("error", False),
        ("cancelled", False),
        ("completed", False),
    ],
)
@pytest.mark.asyncio
async def test_post_response_effects_skip_thread_summary_for_unsuccessful_turn(
    terminal_status: Literal["error", "cancelled", "completed"],
    run_succeeded: bool,
) -> None:
    """Visible failed or cancelled turns must not enqueue thread enrichment."""
    should_queue_thread_summary = MagicMock(return_value=True)
    queue_thread_summary = MagicMock()

    await apply_post_response_effects(
        FinalDeliveryOutcome(
            terminal_status=terminal_status,
            event_id="$response",
            is_visible_response=True,
            final_visible_body="Turn did not complete",
        ),
        ResponseOutcome(
            run_succeeded=run_succeeded,
            thread_summary_room_id="!room:localhost",
            thread_summary_thread_id="$thread",
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
            terminal_status="error",
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
            terminal_status="completed",
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
            terminal_status="error",
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
    runtime = BotRuntimeState(
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        agent_reply_memberships=AgentReplyMembershipIndex(),
        enable_streaming=False,
        orchestrator=None,
    )
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
            terminal_status="completed",
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


@pytest.mark.asyncio
async def test_post_response_effects_queues_summary_with_stale_hint_inside_margin(tmp_path: Path) -> None:
    """A stale hint just below threshold should still reach the live summary check."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    client = make_matrix_client_mock()
    client.room_messages.return_value = nio.RoomMessagesResponse(
        room_id="!room:localhost",
        chunk=[
            nio.RoomMessageText.from_dict(
                {
                    "type": "m.room.message",
                    "event_id": "$thread",
                    "sender": "@user0:localhost",
                    "origin_server_ts": 0,
                    "content": {"msgtype": "m.text", "body": "Thread root"},
                },
            ),
        ],
        start="",
        end=None,
    )
    runtime = BotRuntimeState(
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        agent_reply_memberships=AgentReplyMembershipIndex(),
        enable_streaming=False,
        orchestrator=None,
    )
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
        room_id="!room:localhost",
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

    with (
        patch("mindroom.post_response_effects.create_background_task", side_effect=schedule_background_task),
        patch("mindroom.thread_summary._generate_summary", new=AsyncMock(return_value="Summary")) as mock_generate,
        patch(
            "mindroom.thread_summary._send_thread_summary_event",
            new=AsyncMock(return_value="$summary"),
        ) as mock_send,
    ):
        await apply_post_response_effects(
            FinalDeliveryOutcome(
                terminal_status="completed",
                event_id="$response",
                is_visible_response=True,
                final_visible_body="response",
                delivery_kind="sent",
            ),
            ResponseOutcome(
                thread_summary_room_id="!room:localhost",
                thread_summary_thread_id="$thread",
                thread_summary_message_count_hint=4,
            ),
            deps,
        )

        assert scheduled_tasks
        await asyncio.gather(*scheduled_tasks)

    conversation_reader.read_strict.assert_awaited_once_with(
        room_id="!room:localhost",
        thread_id="$thread",
        limit=HYDRATED_PROMPT_WINDOW_MESSAGES,
    )
    # Nothing has echoed the response back, so the projection this pass reads
    # stops one message short of the thread. It summarizes the projected
    # messages plus the answer it was queued for, and counts that answer -- the
    # same number the pre-queue gate above already used.
    generated_history = mock_generate.await_args.args[0]
    assert list(generated_history[:-1]) == thread_history
    assert (generated_history[-1].sender, generated_history[-1].body) == (client.user_id, "response")
    assert mock_generate.await_args.args[1:] == (config, runtime_paths)
    assert mock_generate.await_args.kwargs == {
        "model_name": "default",
        "tag_vocabulary": None,
        "trusted_sender_ids": current_internal_sender_ids(config, runtime_paths),
    }
    mock_send.assert_awaited_once_with(
        client,
        "!room:localhost",
        "$thread",
        "Summary",
        6,
        "default",
        conversation_reader,
        initial_enrichment_complete=None,
        generated_at=ANY,
    )


@pytest.mark.asyncio
async def test_post_response_effects_queues_summary_with_entity_model_for_adhoc_room(tmp_path: Path) -> None:
    """Ad-hoc invited rooms should inherit the responding entity's summary model override."""
    config = _config(tmp_path)
    config.room_thread_summary_models["general"] = "qwen"
    config.models["qwen"] = ModelConfig(provider="openai", id="qwen-test-model")
    runtime_paths = runtime_paths_for(config)
    client = make_matrix_client_mock()
    runtime = BotRuntimeState(
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        agent_reply_memberships=AgentReplyMembershipIndex(),
        enable_streaming=False,
        orchestrator=None,
    )
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
        room_id="!adhoc:localhost",
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

    with (
        patch("mindroom.post_response_effects.create_background_task", side_effect=schedule_background_task),
        patch("mindroom.thread_summary._generate_summary", new=AsyncMock(return_value="Summary")) as mock_generate,
        patch("mindroom.thread_summary._send_thread_summary_event", new=AsyncMock(return_value="$summary")),
    ):
        await apply_post_response_effects(
            FinalDeliveryOutcome(
                terminal_status="completed",
                event_id="$response",
                is_visible_response=True,
                final_visible_body="response",
                delivery_kind="sent",
            ),
            ResponseOutcome(
                thread_summary_room_id="!adhoc:localhost",
                thread_summary_thread_id="$thread",
                thread_summary_message_count_hint=4,
                thread_summary_entity_name="general",
            ),
            deps,
        )

        assert scheduled_tasks
        await asyncio.gather(*scheduled_tasks)

    generated_history = mock_generate.await_args.args[0]
    assert list(generated_history[:-1]) == thread_history
    assert (generated_history[-1].sender, generated_history[-1].body) == (client.user_id, "response")
    assert mock_generate.await_args.args[1:] == (config, runtime_paths)
    assert mock_generate.await_args.kwargs == {
        "model_name": "qwen",
        "tag_vocabulary": None,
        "trusted_sender_ids": current_internal_sender_ids(config, runtime_paths),
    }


@pytest.mark.asyncio
async def test_generate_response_sets_queued_signal_for_human_ingress(tmp_path: Path) -> None:
    """A waiting human-authored turn should notify the active turn before blocking on the lock."""
    bot = _bot(tmp_path)
    response_envelope = _envelope()
    response_target = response_envelope.target
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator._lifecycle_coordinator
    lifecycle_lock = lifecycle._response_lifecycle_lock(response_target)
    queued_signal = lifecycle._get_or_create_queued_signal(response_target)
    await lifecycle_lock.acquire()

    try:
        with patch.object(
            ResponseRunner,
            "_generate_response_locked",
            new=AsyncMock(return_value="$response"),
        ) as mock_locked:
            task = asyncio.create_task(
                bot._response_runner.generate_response(
                    ResponseRequest(
                        sources=ResponseSources(
                            pending_event_ids=(response_envelope.source_event_id,),
                            logical_source_event_ids=(response_envelope.source_event_id,),
                        ),
                        prompt="hello",
                        thread_history=[],
                        user_id="@user:localhost",
                        response_envelope=response_envelope,
                    ),
                ),
            )
            await asyncio.wait_for(queued_signal.wait(), timeout=0.2)
            lifecycle_lock.release()
            assert await task == "$response"
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
    coordinator = unwrap_extracted_collaborator(bot._turn_controller.deps.response_runner)
    lifecycle = coordinator._lifecycle_coordinator
    lifecycle_lock = lifecycle._response_lifecycle_lock(response_target)
    queued_signal = lifecycle._get_or_create_queued_signal(response_target)
    await lifecycle_lock.acquire()

    try:
        with patch.object(
            ResponseRunner,
            "_generate_response_locked",
            new=AsyncMock(return_value="$response"),
        ):
            task = asyncio.create_task(
                bot._response_runner.generate_response(
                    ResponseRequest(
                        sources=ResponseSources(
                            pending_event_ids=(response_envelope.source_event_id,),
                            logical_source_event_ids=(response_envelope.source_event_id,),
                        ),
                        prompt="hello",
                        thread_history=[],
                        user_id="@user:localhost",
                        response_envelope=response_envelope,
                    ),
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
    started = threading.Event()
    loop_advanced = threading.Event()
    release = threading.Event()
    observed_progress = threading.Event()

    def _blocking_read(**_kwargs: object) -> bool:
        started.set()
        assert release.wait(timeout=5)
        return False

    def _release_after_observing_loop() -> None:
        assert started.wait(timeout=5)
        if loop_advanced.wait(timeout=0.5):
            observed_progress.set()
        release.set()

    observer = threading.Thread(target=_release_after_observing_loop, name="compaction-loop-observer")
    observer.start()
    try:
        with patch.object(coordinator, "_read_queued_forced_compaction", side_effect=_blocking_read):
            task = asyncio.create_task(
                coordinator._has_queued_forced_compaction(
                    session_id="session",
                    scope=scope,
                    execution_identity=None,
                ),
            )
            assert await asyncio.to_thread(started.wait, 5)
            loop_advanced.set()
            assert await task is False
    finally:
        release.set()
        observer.join(timeout=5)

    assert observed_progress.is_set(), "compaction-state read blocked the event loop"
    assert not observer.is_alive()


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
    lifecycle = coordinator._lifecycle_coordinator
    lifecycle_lock = lifecycle._response_lifecycle_lock(response_target)
    queued_signal = lifecycle._get_or_create_queued_signal(response_target)
    await lifecycle_lock.acquire()

    try:
        with patch.object(
            ResponseRunner,
            "_generate_response_locked",
            new=AsyncMock(return_value="$response"),
        ):
            task = asyncio.create_task(
                bot._response_runner.generate_response(
                    ResponseRequest(
                        sources=ResponseSources(
                            pending_event_ids=(response_envelope.source_event_id,),
                            logical_source_event_ids=(response_envelope.source_event_id,),
                        ),
                        prompt="hello",
                        thread_history=[],
                        user_id="@user:localhost",
                        response_envelope=response_envelope,
                    ),
                ),
            )
            await asyncio.wait_for(queued_signal.wait(), timeout=0.2)
            lifecycle_lock.release()
            assert await task == "$response"
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

    async def fake_generate_response_locked(
        _self: ResponseRunner,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
        early_placeholder_state: object,
    ) -> str:
        del resolved_target, early_placeholder_state
        return str(request.user_id)

    with (
        patch.object(coordinator._lifecycle_coordinator, "_response_lifecycle_lock", return_value=lock),
        patch.object(ResponseRunner, "_generate_response_locked", new=fake_generate_response_locked),
    ):
        first_task = asyncio.create_task(
            bot._response_runner.generate_response(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=(first_envelope.source_event_id,),
                        logical_source_event_ids=(first_envelope.source_event_id,),
                    ),
                    prompt="hello",
                    thread_history=[],
                    user_id="first",
                    response_envelope=first_envelope,
                ),
            ),
        )
        await lock.first_waiting.wait()

        second_task = asyncio.create_task(
            bot._response_runner.generate_response(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=(second_envelope.source_event_id,),
                        logical_source_event_ids=(second_envelope.source_event_id,),
                    ),
                    prompt="stop",
                    thread_history=[],
                    user_id="second",
                    response_envelope=second_envelope,
                ),
            ),
        )

        lock._allow_first_entry.set()
        second_result = await second_task
        first_result = await first_task

    assert second_result == "second"
    assert first_result == "first"


@pytest.mark.asyncio
async def test_generate_response_waits_for_lock_before_starting_placeholder_lifecycle(tmp_path: Path) -> None:
    """A queued scheduled turn should not start the placeholder lifecycle until it owns the lock."""
    bot = _bot(tmp_path)
    response_envelope = _envelope(source_kind=SCHEDULED_SOURCE_KIND)
    response_target = response_envelope.target
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle_lock = coordinator._lifecycle_coordinator._response_lifecycle_lock(response_target)
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

    async def fake_run_cancellable_response(*_args: object, **kwargs: object) -> str:
        lifecycle_started.set()
        response_function = kwargs["response_function"]
        await response_function(None)
        return "$response"

    try:
        with (
            patch.object(
                ResponseRunner,
                "_process_and_respond",
                new=AsyncMock(
                    return_value=_ResponseGenerationOutcome(
                        delivery=FinalDeliveryOutcome(
                            terminal_status="completed",
                            event_id="$response",
                            is_visible_response=True,
                            final_visible_body="ok",
                            delivery_kind="sent",
                        ),
                        run_succeeded=True,
                    ),
                ),
            ),
            patch.object(
                ResponseRunner,
                "_run_cancellable_response",
                new=AsyncMock(side_effect=fake_run_cancellable_response),
            ) as mock_run_cancellable_response,
            patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
            patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock()),
        ):
            task = asyncio.create_task(
                bot._response_runner.generate_response(
                    ResponseRequest(
                        sources=ResponseSources(
                            pending_event_ids=(response_envelope.source_event_id,),
                            logical_source_event_ids=(response_envelope.source_event_id,),
                        ),
                        prompt="hello",
                        thread_history=[],
                        user_id="@user:localhost",
                        response_envelope=response_envelope,
                    ),
                ),
            )
            await lock_wait_started.wait()
            mock_run_cancellable_response.assert_not_awaited()

            lifecycle_lock.release()
            await lifecycle_started.wait()
            resolution = await task
            assert resolution == "$response"
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
        request = await coordinator._refresh_model_history_after_lock(
            ResponseRequest(
                sources=ResponseSources(
                    pending_event_ids=("$event",),
                    logical_source_event_ids=("$event",),
                ),
                thread_history=[],
                prompt="hello",
                user_id="@user:localhost",
                response_envelope=request_envelope(
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
        request = await coordinator._refresh_model_history_after_lock(
            ResponseRequest(
                sources=ResponseSources(
                    pending_event_ids=(envelope.source_event_id,),
                    logical_source_event_ids=(envelope.source_event_id,),
                ),
                thread_history=[],
                prompt="hello",
                user_id="@user:localhost",
                response_envelope=envelope,
                requires_model_history_refresh=True,
            ),
        )

    assert request.thread_id is None
    assert request.thread_history == []
    assert request.response_envelope.target.resolved_thread_id is None


@pytest.mark.asyncio
async def test_generate_response_uses_post_lock_reproof_target(tmp_path: Path) -> None:
    """Agent delivery must enter the runner with the finalized stable room target."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    stable_target = MessageTarget.resolve("!room:localhost", None, "$event", room_mode=True)
    observed_run_targets: list[MessageTarget] = []
    observed_delivery_targets: list[MessageTarget | None] = []

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        target = kwargs["target"]
        assert isinstance(target, MessageTarget)
        observed_run_targets.append(target)
        response_function = cast(
            "Callable[[object | None], Awaitable[object]]",
            kwargs["response_function"],
        )
        await response_function(None)
        return "$response"

    async def fake_process_and_respond(
        request: ResponseRequest,
        **_kwargs: object,
    ) -> _ResponseGenerationOutcome:
        observed_delivery_targets.append(request.response_envelope.target)
        return _ResponseGenerationOutcome(
            delivery=FinalDeliveryOutcome(
                terminal_status="completed",
                event_id="$response",
                is_visible_response=True,
                final_visible_body="ok",
                delivery_kind="sent",
            ),
            run_succeeded=True,
        )

    with (
        patch.object(coordinator, "_build_lifecycle", MagicMock(return_value=_NoopResponseLifecycle())),
        patch.object(
            coordinator,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch.object(
            coordinator,
            "_process_and_respond",
            new=AsyncMock(side_effect=fake_process_and_respond),
        ),
        patch("mindroom.response_runner.should_use_streaming", AsyncMock(return_value=False)),
    ):
        result = await coordinator.generate_response(
            ResponseRequest(
                sources=ResponseSources(
                    pending_event_ids=("$event",),
                    logical_source_event_ids=("$event",),
                ),
                thread_history=[],
                prompt="hello",
                user_id="@user:localhost",
                response_envelope=_envelope(source_event_id="$event", target=stable_target),
            ),
        )

    assert result == "$response"
    assert [target.resolved_thread_id for target in observed_run_targets] == [None]
    assert [target.resolved_thread_id if target is not None else None for target in observed_delivery_targets] == [None]
    lock_keys = set(coordinator._lifecycle_coordinator._response_lifecycle_locks)
    assert ResponseLifecycleKey(room_id="!room:localhost", thread_id=None) in lock_keys
    assert ResponseLifecycleKey(room_id="!room:localhost", thread_id="$plain_root") not in lock_keys


@pytest.mark.asyncio
async def test_generate_response_keeps_locked_target_when_payload_preparation_retargets(tmp_path: Path) -> None:
    """Post-lock request preparation may refresh context, but it must not retarget delivery."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    stable_target = MessageTarget.resolve("!room:localhost", None, "$event", room_mode=True)
    retarget = MessageTarget.resolve("!room:localhost", "$other_thread", "$event")
    observed_run_targets: list[MessageTarget] = []
    observed_delivery_targets: list[MessageTarget | None] = []
    observed_lifecycle_targets: list[MessageTarget] = []

    async def fake_prepare(request: ResponseRequest) -> ResponseRequest:
        return replace(
            request,
            response_envelope=_envelope(source_event_id="$event", target=retarget),
            payload_preparation=None,
        )

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        target = kwargs["target"]
        assert isinstance(target, MessageTarget)
        observed_run_targets.append(target)
        response_function = cast(
            "Callable[[object | None], Awaitable[object]]",
            kwargs["response_function"],
        )
        await response_function(None)
        return "$response"

    async def fake_process_and_respond(
        request: ResponseRequest,
        **_kwargs: object,
    ) -> _ResponseGenerationOutcome:
        observed_delivery_targets.append(request.response_envelope.target)
        return _ResponseGenerationOutcome(
            delivery=FinalDeliveryOutcome(
                terminal_status="completed",
                event_id="$response",
                is_visible_response=True,
                final_visible_body="ok",
                delivery_kind="sent",
            ),
            run_succeeded=True,
        )

    def fake_build_lifecycle(**kwargs: object) -> _NoopResponseLifecycle:
        request = kwargs["request"]
        assert isinstance(request, ResponseRequest)
        observed_lifecycle_targets.append(request.response_envelope.target)
        return _NoopResponseLifecycle()

    with (
        patch.object(coordinator, "_build_lifecycle", MagicMock(side_effect=fake_build_lifecycle)),
        patch.object(
            coordinator.deps.request_preparer,
            "prepare",
            new=AsyncMock(side_effect=fake_prepare),
        ),
        patch.object(
            coordinator,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch.object(
            coordinator,
            "_process_and_respond",
            new=AsyncMock(side_effect=fake_process_and_respond),
        ),
        patch("mindroom.response_runner.should_use_streaming", AsyncMock(return_value=False)),
    ):
        result = await coordinator.generate_response(
            ResponseRequest(
                sources=ResponseSources(
                    pending_event_ids=("$event",),
                    logical_source_event_ids=("$event",),
                ),
                thread_history=[],
                prompt="hello",
                user_id="@user:localhost",
                response_envelope=_envelope(source_event_id="$event", target=stable_target),
                payload_preparation=_payload_preparation(stable_target),
            ),
        )

    assert result == "$response"
    assert observed_run_targets == [stable_target]
    assert observed_delivery_targets == [stable_target]
    assert observed_lifecycle_targets == [stable_target]
    lock_keys = set(coordinator._lifecycle_coordinator._response_lifecycle_locks)
    assert ResponseLifecycleKey(room_id="!room:localhost", thread_id=None) in lock_keys
    assert ResponseLifecycleKey(room_id="!room:localhost", thread_id="$other_thread") not in lock_keys


@pytest.mark.asyncio
async def test_generate_team_response_uses_post_lock_reproof_target(tmp_path: Path) -> None:
    """Team delivery/session setup must enter the runner with the finalized stable room target."""
    bot = _bot(tmp_path)
    bot.client = MagicMock()
    cached_room = MagicMock(encrypted=False)
    bot.client.rooms = {"!room:localhost": cached_room}
    bot.client.room_send = AsyncMock()
    bot.client.room_typing = AsyncMock()
    bot.orchestrator = MagicMock()
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    stable_target = MessageTarget.resolve("!room:localhost", None, "$event", room_mode=True)
    lifecycle = _NoopResponseLifecycle()
    observed_run_targets: list[MessageTarget] = []
    observed_delivery_targets: list[MessageTarget] = []

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        target = kwargs["target"]
        assert isinstance(target, MessageTarget)
        observed_run_targets.append(target)
        response_function = cast(
            "Callable[[object | None], Awaitable[object]]",
            kwargs["response_function"],
        )
        await response_function(None)
        return "$response"

    async def fake_deliver_final(request: FinalDeliveryRequest) -> FinalDeliveryOutcome:
        observed_delivery_targets.append(request.target)
        return FinalDeliveryOutcome(
            terminal_status="completed",
            event_id="$response",
            is_visible_response=True,
            final_visible_body=request.response_text,
            delivery_kind="sent",
        )

    with (
        patch.object(coordinator, "_build_lifecycle", MagicMock(return_value=lifecycle)),
        patch.object(
            coordinator,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.delivery_gateway.DeliveryGateway.deliver_final", new=AsyncMock(side_effect=fake_deliver_final)),
        patch("mindroom.response_runner.should_use_streaming", AsyncMock(return_value=False)),
        patch("mindroom.response_runner.team_response", AsyncMock(return_value="team ok")),
    ):
        result = await coordinator.generate_team_response_helper(
            ResponseRequest(
                sources=ResponseSources(
                    pending_event_ids=("$event",),
                    logical_source_event_ids=("$event",),
                ),
                thread_history=[],
                prompt="hello",
                user_id="@user:localhost",
                response_envelope=_envelope(source_event_id="$event", target=stable_target),
            ),
            team_agents=[],
            team_mode="coordinate",
        )

    assert result == "$response"
    assert [target.resolved_thread_id for target in observed_run_targets] == [None]
    assert [target.resolved_thread_id for target in observed_delivery_targets] == [None]
    assert lifecycle.session_thread_ids == [None]
    lock_keys = set(coordinator._lifecycle_coordinator._response_lifecycle_locks)
    assert ResponseLifecycleKey(room_id="!room:localhost", thread_id=None) in lock_keys
    assert ResponseLifecycleKey(room_id="!room:localhost", thread_id="$plain_root") not in lock_keys


@pytest.mark.asyncio
async def test_generate_team_response_keeps_locked_target_when_payload_preparation_retargets(tmp_path: Path) -> None:
    """Team response setup and delivery should keep the target selected before lock acquisition."""
    bot = _bot(tmp_path)
    bot.client = MagicMock()
    cached_room = MagicMock(encrypted=False)
    bot.client.rooms = {"!room:localhost": cached_room}
    bot.client.room_send = AsyncMock()
    bot.client.room_typing = AsyncMock()
    bot.orchestrator = MagicMock()
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

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        target = kwargs["target"]
        assert isinstance(target, MessageTarget)
        observed_run_targets.append(target)
        response_function = cast(
            "Callable[[object | None], Awaitable[object]]",
            kwargs["response_function"],
        )
        await response_function(None)
        return "$response"

    async def fake_deliver_final(request: FinalDeliveryRequest) -> FinalDeliveryOutcome:
        observed_delivery_targets.append(request.target)
        return FinalDeliveryOutcome(
            terminal_status="completed",
            event_id="$response",
            is_visible_response=True,
            final_visible_body=request.response_text,
            delivery_kind="sent",
        )

    with (
        patch.object(coordinator, "_build_lifecycle", MagicMock(return_value=lifecycle)),
        patch.object(
            coordinator.deps.request_preparer,
            "prepare",
            new=AsyncMock(side_effect=fake_prepare),
        ),
        patch.object(
            coordinator,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.delivery_gateway.DeliveryGateway.deliver_final", new=AsyncMock(side_effect=fake_deliver_final)),
        patch("mindroom.response_runner.should_use_streaming", AsyncMock(return_value=False)),
        patch("mindroom.response_runner.team_response", AsyncMock(return_value="team ok")),
    ):
        result = await coordinator.generate_team_response_helper(
            ResponseRequest(
                sources=ResponseSources(
                    pending_event_ids=("$event",),
                    logical_source_event_ids=("$event",),
                ),
                thread_history=[],
                prompt="hello",
                user_id="@user:localhost",
                response_envelope=_envelope(source_event_id="$event", target=stable_target),
                payload_preparation=_payload_preparation(stable_target),
            ),
            team_agents=[],
            team_mode="coordinate",
        )

    assert result == "$response"
    assert observed_run_targets == [stable_target]
    assert observed_delivery_targets == [stable_target]
    assert lifecycle.session_thread_ids == [None]
    lock_keys = set(coordinator._lifecycle_coordinator._response_lifecycle_locks)
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
        pytest.raises(PostLockRequestPreparationError) as excinfo,
    ):
        await coordinator._prepare_request_after_lock(
            ResponseRequest(
                sources=ResponseSources(
                    pending_event_ids=("$event",),
                    logical_source_event_ids=("$event",),
                ),
                thread_history=[],
                prompt="hello",
                user_id="@user:localhost",
                response_envelope=request_envelope(
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
    lifecycle = coordinator._lifecycle_coordinator
    lifecycle_lock = lifecycle._response_lifecycle_lock(response_target)
    queued_signal = lifecycle._get_or_create_queued_signal(response_target)
    await lifecycle_lock.acquire()

    try:
        with patch.object(
            ResponseRunner,
            "_generate_team_response_helper_locked",
            new=AsyncMock(return_value="$team-response"),
        ) as mock_locked:
            task = asyncio.create_task(
                bot._response_runner.generate_team_response_helper(
                    ResponseRequest(
                        sources=ResponseSources(
                            pending_event_ids=(response_envelope.source_event_id,),
                            logical_source_event_ids=(response_envelope.source_event_id,),
                        ),
                        thread_history=[],
                        prompt="hello",
                        user_id="@user:localhost",
                        response_envelope=response_envelope,
                    ),
                    team_agents=[],
                    team_mode="coordinate",
                ),
            )
            await asyncio.wait_for(queued_signal.wait(), timeout=0.2)
            lifecycle_lock.release()
            assert await task == "$team-response"
            mock_locked.assert_awaited_once()
    finally:
        if lifecycle_lock.locked():
            lifecycle_lock.release()

    assert not queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
async def test_generate_response_without_reservation_does_not_drain_human_backlog(tmp_path: Path) -> None:
    """Unreserved direct responses should serialize normally instead of owning active follow-up backlog."""
    bot = _bot(tmp_path)
    response_envelope_b = _envelope(source_event_id="$event-b")
    response_envelope_c = _envelope(source_event_id="$event-c")
    response_target = response_envelope_b.target
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator._lifecycle_coordinator
    lifecycle_lock = lifecycle._response_lifecycle_lock(response_target)
    queued_signal = lifecycle._get_or_create_queued_signal(response_target)
    observed_pending: list[bool] = []
    second_turn_started = asyncio.Event()
    allow_turns_to_finish = asyncio.Event()

    async def fake_locked(_self: ResponseRunner, *_args: object, **_kwargs: object) -> str:
        observed_pending.append(queued_signal.has_pending_human_messages())
        if len(observed_pending) == 1:
            second_turn_started.set()
        await allow_turns_to_finish.wait()
        return f"$response-{len(observed_pending)}"

    await lifecycle_lock.acquire()
    try:
        with patch.object(ResponseRunner, "_generate_response_locked", new=fake_locked):
            task_b = asyncio.create_task(
                bot._response_runner.generate_response(
                    ResponseRequest(
                        sources=ResponseSources(
                            pending_event_ids=(response_envelope_b.source_event_id,),
                            logical_source_event_ids=(response_envelope_b.source_event_id,),
                        ),
                        prompt="hello",
                        thread_history=[],
                        user_id="@user:localhost",
                        response_envelope=response_envelope_b,
                    ),
                ),
            )
            await asyncio.wait_for(queued_signal.wait(), timeout=0.2)
            task_c = asyncio.create_task(
                bot._response_runner.generate_response(
                    ResponseRequest(
                        sources=ResponseSources(
                            pending_event_ids=(response_envelope_c.source_event_id,),
                            logical_source_event_ids=(response_envelope_c.source_event_id,),
                        ),
                        prompt="hello again",
                        thread_history=[],
                        user_id="@user:localhost",
                        response_envelope=response_envelope_c,
                    ),
                ),
            )
            for _ in range(20):
                if queued_signal.pending_human_messages == 2:
                    break
                await asyncio.sleep(0)
            assert queued_signal.pending_human_messages == 2

            lifecycle_lock.release()
            await asyncio.wait_for(second_turn_started.wait(), timeout=0.2)
            assert observed_pending == [True]

            allow_turns_to_finish.set()
            assert await task_b == "$response-1"
            assert await task_c == "$response-2"
    finally:
        if lifecycle_lock.locked():
            lifecycle_lock.release()

    assert observed_pending == [True, False]
    assert not queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
async def test_generate_team_response_without_reservation_does_not_drain_human_backlog(tmp_path: Path) -> None:
    """Unreserved direct team responses should serialize without owning active follow-up backlog."""
    bot = _bot(tmp_path)
    response_envelope_b = _envelope(source_event_id="$team-event-b")
    response_envelope_c = _envelope(source_event_id="$team-event-c")
    response_target = response_envelope_b.target
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator._lifecycle_coordinator
    lifecycle_lock = lifecycle._response_lifecycle_lock(response_target)
    queued_signal = lifecycle._get_or_create_queued_signal(response_target)
    observed_pending: list[bool] = []
    second_turn_started = asyncio.Event()
    allow_turns_to_finish = asyncio.Event()

    async def fake_locked(_self: ResponseRunner, *_args: object, **_kwargs: object) -> str:
        observed_pending.append(queued_signal.has_pending_human_messages())
        if len(observed_pending) == 1:
            second_turn_started.set()
        await allow_turns_to_finish.wait()
        return f"$team-response-{len(observed_pending)}"

    await lifecycle_lock.acquire()
    try:
        with patch.object(ResponseRunner, "_generate_team_response_helper_locked", new=fake_locked):
            task_b = asyncio.create_task(
                bot._response_runner.generate_team_response_helper(
                    ResponseRequest(
                        sources=ResponseSources(
                            pending_event_ids=(response_envelope_b.source_event_id,),
                            logical_source_event_ids=(response_envelope_b.source_event_id,),
                        ),
                        thread_history=[],
                        prompt="hello",
                        user_id="@user:localhost",
                        response_envelope=response_envelope_b,
                    ),
                    team_agents=[],
                    team_mode="coordinate",
                ),
            )
            await asyncio.wait_for(queued_signal.wait(), timeout=0.2)
            task_c = asyncio.create_task(
                bot._response_runner.generate_team_response_helper(
                    ResponseRequest(
                        sources=ResponseSources(
                            pending_event_ids=(response_envelope_c.source_event_id,),
                            logical_source_event_ids=(response_envelope_c.source_event_id,),
                        ),
                        thread_history=[],
                        prompt="hello again",
                        user_id="@user:localhost",
                        response_envelope=response_envelope_c,
                    ),
                    team_agents=[],
                    team_mode="coordinate",
                ),
            )
            for _ in range(20):
                if queued_signal.pending_human_messages == 2:
                    break
                await asyncio.sleep(0)
            assert queued_signal.pending_human_messages == 2

            lifecycle_lock.release()
            await asyncio.wait_for(second_turn_started.wait(), timeout=0.2)
            assert observed_pending == [True]

            allow_turns_to_finish.set()
            assert await task_b == "$team-response-1"
            assert await task_c == "$team-response-2"
    finally:
        if lifecycle_lock.locked():
            lifecycle_lock.release()

    assert observed_pending == [True, False]
    assert not queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
async def test_reserved_human_follow_up_reaches_active_turn_before_dispatch(tmp_path: Path) -> None:
    """Gate-owned follow-ups should still notify the active response before their dispatch starts."""
    bot = _bot(tmp_path)
    active_envelope = _envelope(source_event_id="$active")
    follow_up_envelope = _envelope(source_event_id="$followup")
    response_target = active_envelope.target
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator._lifecycle_coordinator
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
    reservation.consume()

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
    lifecycle = coordinator._lifecycle_coordinator
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
        reservation.consume()

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
    lifecycle = coordinator._lifecycle_coordinator
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
    human_reservation.consume()

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
    target = MessageTarget.resolve(room.room_id, "$thread", "$command")
    envelope = _envelope(
        dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        source_event_id="$command",
        target=target,
    )
    event = PreparedIngress(
        sender="@user:localhost",
        event_id="$command",
        body="!help",
        source={"content": {"body": "!help"}},
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
        correlation_id="$command",
        envelope=envelope,
    )
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator._lifecycle_coordinator
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    queued_signal.begin_response_turn()
    try:
        reservation = lifecycle.reserve_waiting_human_message(target=target, response_envelope=envelope)
        assert reservation is not None
        with (
            patch.object(
                bot._turn_controller,
                "_prepare_dispatch",
                new=AsyncMock(return_value=prepared_dispatch_result(dispatch)),
            ),
            patch("mindroom.turn_policy.TurnPolicy.plan_turn", new=AsyncMock()) as mock_plan_turn,
        ):
            await dispatch_test_turn(
                bot._turn_controller,
                room,
                _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
                queued_notice_reservation=reservation,
            )
    finally:
        queued_signal.finish_response_turn()

    mock_plan_turn.assert_not_awaited()
    assert not queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
async def test_reserved_superseded_follow_up_cleanup_when_dispatch_returns(tmp_path: Path) -> None:
    """Superseded active follow-ups should clear their enqueue-time notice when skipped."""
    bot = _bot(tmp_path)
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    target = MessageTarget.resolve(room.room_id, "$thread", "$older")
    envelope = _envelope(
        dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        source_event_id="$older",
        target=target,
    )
    event = PreparedIngress(
        sender="@user:localhost",
        event_id="$older",
        body="older follow-up",
        source={"content": {"body": "older follow-up"}},
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
        correlation_id="$older",
        envelope=envelope,
    )
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator._lifecycle_coordinator
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    queued_signal.begin_response_turn()
    try:
        reservation = lifecycle.reserve_waiting_human_message(target=target, response_envelope=envelope)
        assert reservation is not None
        with (
            patch.object(
                bot._turn_controller,
                "_prepare_dispatch",
                new=AsyncMock(return_value=prepared_dispatch_result(dispatch)),
            ),
            patch.object(bot._turn_controller, "_has_newer_unresponded_in_thread", return_value=True),
            patch.object(bot._turn_policy, "plan_turn", new=AsyncMock()) as mock_plan_turn,
        ):
            await dispatch_test_turn(
                bot._turn_controller,
                room,
                _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
                queued_notice_reservation=reservation,
            )
    finally:
        queued_signal.finish_response_turn()

    mock_plan_turn.assert_not_awaited()
    assert not queued_signal.has_pending_human_messages()


@pytest.mark.asyncio
async def test_reserved_follow_up_cleanup_when_hook_suppression_returns_before_dispatch(tmp_path: Path) -> None:
    """Hook-suppressed active follow-ups should clear the reservation before planning."""
    bot = _bot(tmp_path)
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    case = _reserved_follow_up_case(bot, room, event_id="$suppressed")
    try:
        with (
            patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=None)),
            patch("mindroom.turn_policy.TurnPolicy.plan_turn", new=AsyncMock()) as mock_plan_turn,
        ):
            await dispatch_test_turn(
                bot._turn_controller,
                room,
                _PrecheckedEvent(event=case.event, requester_user_id="@user:localhost"),
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
                bot._turn_controller,
                "_prepare_dispatch",
                new=AsyncMock(return_value=prepared_dispatch_result(case.dispatch)),
            ),
            patch(
                "mindroom.turn_policy.TurnPolicy.plan_turn",
                new=AsyncMock(return_value=_DispatchPlan(kind="ignore")),
            ),
        ):
            await dispatch_test_turn(
                bot._turn_controller,
                room,
                _PrecheckedEvent(event=case.event, requester_user_id="@user:localhost"),
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
                bot._turn_controller,
                "_prepare_dispatch",
                new=AsyncMock(return_value=prepared_dispatch_result(case.dispatch)),
            ),
            patch(
                "mindroom.turn_policy.TurnPolicy.plan_turn",
                new=AsyncMock(return_value=_DispatchPlan(kind="route", router_message="route this")),
            ),
            patch.object(bot._turn_controller, "_execute_router_relay", new=AsyncMock()) as mock_route,
        ):
            await dispatch_test_turn(
                bot._turn_controller,
                room,
                _PrecheckedEvent(event=case.event, requester_user_id="@user:localhost"),
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
                bot._turn_controller,
                "_prepare_dispatch",
                new=AsyncMock(return_value=prepared_dispatch_result(case.dispatch)),
            ),
            patch(
                "mindroom.turn_policy.TurnPolicy.plan_turn",
                new=AsyncMock(
                    return_value=_DispatchPlan(
                        kind="respond",
                        response_action=ResponseAction(kind="individual"),
                    ),
                ),
            ),
            patch.object(
                bot._turn_controller,
                "_execute_response_action",
                new=AsyncMock(side_effect=RuntimeError("dispatch failed")),
            ),
            pytest.raises(RuntimeError, match="dispatch failed"),
        ):
            await dispatch_test_turn(
                bot._turn_controller,
                room,
                _PrecheckedEvent(event=case.event, requester_user_id="@user:localhost"),
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
                bot._turn_controller,
                "_prepare_dispatch",
                new=AsyncMock(return_value=prepared_dispatch_result(case.dispatch)),
            ),
            patch(
                "mindroom.turn_policy.TurnPolicy.plan_turn",
                new=AsyncMock(
                    return_value=_DispatchPlan(
                        kind="respond",
                        response_action=ResponseAction(kind="individual"),
                    ),
                ),
            ),
            patch.object(
                bot._turn_controller,
                "_execute_response_action",
                new=AsyncMock(side_effect=asyncio.CancelledError),
            ),
        ):
            await dispatch_test_turn(
                bot._turn_controller,
                room,
                _PrecheckedEvent(event=case.event, requester_user_id="@user:localhost"),
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
    lifecycle = coordinator._lifecycle_coordinator
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    queued_signal.begin_response_turn()
    try:
        reservation = lifecycle.reserve_waiting_human_message(target=target, response_envelope=envelope)
        assert reservation is not None
        with (
            patch.object(
                bot._turn_controller,
                "_prepare_dispatch",
                new=AsyncMock(side_effect=RuntimeError("prepare failed")),
            ),
            pytest.raises(RuntimeError, match="prepare failed"),
        ):
            await dispatch_test_turn(
                bot._turn_controller,
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
    lifecycle = coordinator._lifecycle_coordinator
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
                    dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
                    dispatch_metadata=_queued_notice_metadata(reservation),
                ),
            ],
        )
        with (
            patch(
                "mindroom.turn_controller.dispatch_text_message",
                new=AsyncMock(side_effect=RuntimeError("dispatch failed")),
            ),
            pytest.raises(RuntimeError, match="dispatch failed"),
        ):
            await bot._turn_controller.handle_prepared_turn(batch)
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
    lifecycle = coordinator._lifecycle_coordinator
    pre_signal = lifecycle._get_or_create_queued_signal(pre_target)
    post_signal = lifecycle._get_or_create_queued_signal(post_target)
    typed_event = _prepared_text_event(event_id="$typed")
    voice_event = replace(
        _prepared_text_event(event_id="$voice"),
        body="🎤 voice transcript",
        source_kind_override=VOICE_SOURCE_KIND,
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
                    dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
                    dispatch_metadata=_targeted_queued_notice_metadata(pre_reservation, pre_target),
                ),
                make_pending_event(
                    voice_event,
                    room,
                    source_kind=VOICE_SOURCE_KIND,
                    dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
                    dispatch_metadata=_targeted_queued_notice_metadata(post_reservation, post_target),
                ),
            ],
        )

        with patch(
            "mindroom.turn_controller.dispatch_text_message",
            new=AsyncMock(side_effect=prepared_turn_recorder(capture_dispatch)),
        ):
            await bot._turn_controller.handle_prepared_turn(batch)
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
        source_kind_override=VOICE_SOURCE_KIND,
    )
    target = bot._turn_controller.deps.resolver.build_message_target(
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
    lifecycle = coordinator._lifecycle_coordinator
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
                    dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
                    dispatch_metadata=_targeted_queued_notice_metadata(voice_reservation, target),
                ),
            ],
        )

        with patch(
            "mindroom.turn_controller.dispatch_text_message",
            new=AsyncMock(side_effect=prepared_turn_recorder(capture_dispatch)),
        ):
            await bot._turn_controller.handle_prepared_turn(batch)
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
    lifecycle = coordinator._lifecycle_coordinator
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    queued_signal.begin_response_turn()
    try:
        reservation = lifecycle.reserve_waiting_human_message(target=target, response_envelope=envelope)
        assert reservation is not None
        assert queued_signal.pending_human_messages == 1
        reservation_owner = bot._turn_controller.reserve_prompt_ingress_order(room, "@user:localhost")
        try:
            with (
                patch.object(
                    bot._turn_controller,
                    "_enqueue_for_dispatch",
                    new=AsyncMock(side_effect=asyncio.CancelledError),
                ),
                pytest.raises(asyncio.CancelledError),
            ):
                await bot._turn_controller._enqueue_prepared_text_for_dispatch(
                    room=room,
                    prepared_event=event,
                    envelope=envelope,
                    coalescing_thread_id="$thread",
                    requester_user_id="@user:localhost",
                    reservation_owner=reservation_owner,
                    queued_notice_reservation=reservation,
                )
        finally:
            await reservation_owner.release()
    finally:
        queued_signal.finish_response_turn()

    assert queued_signal.pending_human_messages == 0
    assert not queued_signal.has_pending_human_messages()


def test_queued_human_reservation_is_idempotent(tmp_path: Path) -> None:
    """Reservation consume/cancel operations should clear the notice at most once."""
    bot = _bot(tmp_path)
    target = MessageTarget.resolve("!room:localhost", "$thread", "$event")
    envelope = _envelope(
        dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        source_event_id="$event",
        target=target,
    )
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator._lifecycle_coordinator
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    queued_signal.begin_response_turn()
    try:
        reservation = lifecycle.reserve_waiting_human_message(target=target, response_envelope=envelope)
        assert reservation is not None
        assert queued_signal.pending_human_messages == 1
        reservation.consume()
        reservation.consume()
        reservation.cancel()
        assert queued_signal.pending_human_messages == 0
    finally:
        queued_signal.finish_response_turn()

    assert not queued_signal.has_pending_human_messages()


def test_managed_message_does_not_reserve_queued_human_notice(tmp_path: Path) -> None:
    """Managed chatter should not count as a queued human follow-up."""
    bot = _bot(tmp_path)
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
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator._lifecycle_coordinator
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
async def test_lifecycle_notice_is_cancelled_when_lock_wait_is_cancelled(tmp_path: Path) -> None:
    """A lifecycle-owned notice should not leak if lock acquisition is cancelled."""
    bot = _bot(tmp_path)
    target = MessageTarget.resolve("!room:localhost", "$thread", "$event")
    envelope = _envelope(
        dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        source_event_id="$event",
        target=target,
    )
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = coordinator._lifecycle_coordinator
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    queued_signal.begin_response_turn()
    reservation = lifecycle.reserve_waiting_human_message(target=target, response_envelope=envelope)
    assert reservation is not None
    assert queued_signal.pending_human_messages == 1
    reservation.consume()

    lock = lifecycle._response_lifecycle_lock(target)
    await lock.acquire()

    async def locked_operation(_target: MessageTarget) -> str:
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
            bot._turn_controller,
            "_prepare_dispatch",
            new=AsyncMock(return_value=prepared_dispatch_result(dispatch)),
        ),
        patch.object(bot._turn_controller, "_has_newer_unresponded_in_thread", return_value=True),
        patch.object(
            bot._turn_policy,
            "plan_turn",
            new=AsyncMock(return_value=_DispatchPlan(kind="ignore")),
        ) as mock_plan,
    ):
        await dispatch_test_turn(
            bot._turn_controller,
            room,
            _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
        )

    assert not bot._turn_store.is_handled("$older")
    mock_plan.assert_not_awaited()
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    assert coordinator._lifecycle_coordinator._thread_queued_signals == {}


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
        prior_notice = _owned_notice("prior-response", marker="persisted", from_history=True)
        current_stale_notice = _owned_notice(notice_context.response_turn_id, marker="persisted")
        messages = [prior_notice, current_stale_notice]
        await _after_batch(messages, OwnedMessage(role="tool", content="result"))

    assert messages[0] is prior_notice
    assert _notice_count(messages, marker="persisted", response_turn_id="prior-response") == 1
    assert _notice_count(messages, marker="persisted", response_turn_id=notice_context.response_turn_id) == 0
    assert _notice_count(messages, marker=True, response_turn_id=notice_context.response_turn_id) == 1


@pytest.mark.asyncio
async def test_notice_hook_preserves_prior_persisted_replay() -> None:
    """A prior persisted notice should survive later tool-result formatting byte-for-byte."""
    prior_notice = _owned_notice("prior-response", marker="persisted", from_history=True)
    original = prior_notice.to_dict()

    with queued_message_signal_context(_StaticQueuedState(pending=True)) as notice_context:
        messages = [prior_notice]
        for index in range(2):
            await _after_batch(messages, OwnedMessage(role="tool", content=f"result {index}"))

    assert prior_notice.to_dict() == original
    assert _notice_count(messages, marker="persisted", response_turn_id="prior-response") == 1
    assert _notice_count(messages, marker=True, response_turn_id=notice_context.response_turn_id) == 1


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
            ),
        )

    messages = list(model.requests[-1].messages)
    assert _notice_count(messages) == 1
    assert messages[-2].role == "user"
    assert messages[-2].images
    assert messages[-1].content == QUEUED_MESSAGE_NOTICE_TEXT


@pytest.mark.asyncio
async def test_response_finalization_collapses_nested_current_notice_state(tmp_path: Path) -> None:
    """Outer finalization should leave one exact notice in the top-level completed run."""
    store = _recording_store(tmp_path, "queued-notice-team")

    with queued_message_signal_context(_StaticQueuedState(pending=True)) as notice_context:
        messages: list[OwnedMessage] = []
        await _after_batch(messages, OwnedMessage(role="tool", content="result"))
        _seed(
            store,
            TeamRunRecord(
                run_id="run-1",
                session_id="session-1",
                team_id="queued-notice-team",
                status=OwnedRunStatus.completed,
                messages=deepcopy(messages),
                member_responses=[_completed("member-run-1", messages, agent_id="member")],
            ),
            kind="team",
        )
        register_queued_notice_storage(
            storage_factory=lambda: store,
            session_id="session-1",
            session_kind="team",
            entity_name="queued-notice-team",
        )
        assert _notice_count(messages, marker=True) == 1
        await ai_runtime.finalize_queued_notice_response_turn_async(notice_context)

    [stored_team_run] = _stored_runs(store, "team")
    assert isinstance(stored_team_run, TeamRunRecord)
    assert _notice_count(stored_team_run.messages, marker=True) == 0
    assert _notice_count(stored_team_run.messages, marker="persisted") == 1
    [stored_member_run] = stored_team_run.member_responses
    assert _notice_count(stored_member_run.messages) == 0


@pytest.mark.asyncio
async def test_response_finalization_does_not_copy_notice_to_untouched_target(tmp_path: Path) -> None:
    """Finalization should persist a notice only in storage that contains its live marker."""
    untouched_store = _recording_store(tmp_path, "delegated-agent")
    noticed_store = _recording_store(tmp_path, "outer-agent")

    with queued_message_signal_context(_StaticQueuedState(pending=True)) as notice_context:
        noticed_messages = [OwnedMessage(role="user", content="Outer request")]
        await _after_batch(noticed_messages, OwnedMessage(role="tool", content="Outer tool result"))
        delegated = [
            OwnedMessage(role="user", content="Delegated request"),
            OwnedMessage(role="assistant", content="Delegated answer"),
        ]
        _seed(untouched_store, _completed("untouched-run", delegated), session_id="shared-session")
        _seed(noticed_store, _completed("noticed-run", noticed_messages), session_id="shared-session")
        for name, store in (("delegated-agent", untouched_store), ("outer-agent", noticed_store)):
            register_queued_notice_storage(
                storage_factory=partial(lambda chosen: chosen, store),
                session_id="shared-session",
                session_kind="agent",
                entity_name=name,
            )

        await ai_runtime.finalize_queued_notice_response_turn_async(notice_context)

    assert untouched_store.saves == []
    [untouched_run] = _stored_runs(untouched_store, session_id="shared-session")
    assert [(message.role, message.content) for message in untouched_run.messages] == [
        ("user", "Delegated request"),
        ("assistant", "Delegated answer"),
    ]
    assert noticed_store.saves == [["noticed-run"]]
    # The live list the model saw is left untouched; the finalized stored copy changed.
    assert _notice_count(noticed_messages, marker=True) == 1
    [persisted_run] = _stored_runs(noticed_store, session_id="shared-session")
    assert _notice_count(persisted_run.messages, marker=True) == 0
    assert _notice_count(persisted_run.messages, marker="persisted") == 1


@pytest.mark.asyncio
async def test_response_finalization_uses_newest_completed_same_turn_run_and_original_notice_position(
    tmp_path: Path,
) -> None:
    """Two completed attempts should leave one exact notice at the newest notice position."""
    store = _recording_store(tmp_path, "general")

    with queued_message_signal_context(_StaticQueuedState(pending=True)) as notice_context:
        first_messages = [
            OwnedMessage(role="user", content="First request"),
            OwnedMessage(role="assistant", content="First tool call"),
        ]
        await _after_batch(first_messages, OwnedMessage(role="tool", content="First result"))
        first_messages.append(OwnedMessage(role="assistant", content="First final"))
        second_messages = [
            OwnedMessage(role="user", content="Second request"),
            OwnedMessage(role="assistant", content="Second tool call"),
        ]
        await _after_batch(second_messages, OwnedMessage(role="tool", content="Second result"))
        second_messages.append(OwnedMessage(role="assistant", content="Second final"))
        _seed(
            store,
            _completed("first-completed-run", first_messages),
            _completed("second-completed-run", second_messages),
        )
        register_queued_notice_storage(
            storage_factory=lambda: store,
            session_id="session-1",
            session_kind="agent",
            entity_name="general",
        )

        await ai_runtime.finalize_queued_notice_response_turn_async(notice_context)

    # Both attempts change: the first loses its notice, the second gets the persisted one.
    assert [sorted(saved) for saved in store.saves] == [["first-completed-run", "second-completed-run"]]
    stored_first, stored_second = _stored_runs(store)
    assert _notice_count(stored_first.messages) == 0
    assert [(message.role, message.content) for message in stored_second.messages] == [
        ("user", "Second request"),
        ("assistant", "Second tool call"),
        ("tool", "Second result"),
        ("user", QUEUED_MESSAGE_NOTICE_TEXT),
        ("assistant", "Second final"),
    ]
    assert (
        _notice_count(stored_second.messages, marker="persisted", response_turn_id=notice_context.response_turn_id) == 1
    )


@pytest.mark.asyncio
async def test_async_cleanup_keeps_session_storage_io_off_event_loop(tmp_path: Path) -> None:
    """Slow response-boundary storage finalization must stay off the event loop."""
    release = threading.Event()
    store = _recording_store(tmp_path, "queued-notice-agent", held=release)
    factory_threads: list[int] = []
    event_loop_thread_id = threading.get_ident()

    def storage_factory() -> ConversationStore:
        factory_threads.append(threading.get_ident())
        return store

    with queued_message_signal_context(_StaticQueuedState(pending=True)) as notice_context:
        messages: list[OwnedMessage] = []
        await _after_batch(messages, OwnedMessage(role="tool", content="result"))
        _seed(store, _completed("run-1", messages))
        register_queued_notice_storage(
            storage_factory=storage_factory,
            session_id="session-1",
            session_kind="agent",
            entity_name="queued-notice-agent",
        )
        cleanup_task = asyncio.create_task(
            ai_runtime.finalize_queued_notice_response_turn_async(notice_context),
        )
        try:
            assert await asyncio.to_thread(store.loading.wait, 1)
            await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
            assert not cleanup_task.done()
        finally:
            release.set()

        await asyncio.wait_for(cleanup_task, timeout=1)
    assert factory_threads
    assert event_loop_thread_id not in factory_threads
    assert store.saves == [["run-1"]]


@pytest.mark.asyncio
async def test_response_turn_finalization_completes_when_cancelled_during_storage_io(tmp_path: Path) -> None:
    """Cancellation during storage I/O must not leave a durable live imperative behind."""
    release = threading.Event()
    store = _recording_store(tmp_path, "general", held=release)

    with queued_message_signal_context(_StaticQueuedState(pending=True)) as notice_context:
        messages: list[OwnedMessage] = []
        await _after_batch(messages, OwnedMessage(role="tool", content="result"))
        _seed(store, _completed("completed-run", messages))
        register_queued_notice_storage(
            storage_factory=lambda: store,
            session_id="session-1",
            session_kind="agent",
            entity_name="general",
        )
        finalization_task = asyncio.create_task(
            ai_runtime.finalize_queued_notice_response_turn_async(notice_context),
        )
        assert await asyncio.to_thread(store.loading.wait, 1)
        finalization_task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await finalization_task

    # Cancellation is re-raised only after the stored run was finalized.
    [stored] = _stored_runs(store)
    assert _notice_count(stored.messages, marker=True) == 0
    assert _notice_count(stored.messages, marker="persisted", response_turn_id=notice_context.response_turn_id) == 1


@pytest.mark.asyncio
async def test_response_turn_finalization_defers_repeated_cancellation_during_storage_io(tmp_path: Path) -> None:
    """Repeated cancellation must not release the caller before durable finalization finishes."""
    release = threading.Event()
    store = _recording_store(tmp_path, "general", held=release)
    first_cancel_observed = asyncio.Event()
    original_shield = asyncio.shield

    async def observed_shield(task: asyncio.Task[None]) -> None:
        try:
            await original_shield(task)
        except asyncio.CancelledError:
            first_cancel_observed.set()
            raise

    with queued_message_signal_context(_StaticQueuedState(pending=True)) as notice_context:
        messages: list[OwnedMessage] = []
        await _after_batch(messages, OwnedMessage(role="tool", content="result"))
        _seed(store, _completed("completed-run", messages))
        register_queued_notice_storage(
            storage_factory=lambda: store,
            session_id="session-1",
            session_kind="agent",
            entity_name="general",
        )
        with patch.object(ai_runtime.asyncio, "shield", new=observed_shield):
            finalization_task = asyncio.create_task(
                ai_runtime.finalize_queued_notice_response_turn_async(notice_context),
            )
            assert await asyncio.to_thread(store.loading.wait, 1)
            finalization_task.cancel()
            await asyncio.wait_for(first_cancel_observed.wait(), timeout=1)
            finalization_task.cancel()
            completed, _ = await asyncio.wait({finalization_task}, timeout=0.05)
            try:
                assert finalization_task not in completed
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await finalization_task

    assert store.saves == [["completed-run"]]
    [stored] = _stored_runs(store)
    assert _notice_count(stored.messages, marker=True) == 0
    assert _notice_count(stored.messages, marker="persisted", response_turn_id=notice_context.response_turn_id) == 1


@pytest.mark.asyncio
async def test_storage_registration_deduplicates_one_session_across_fresh_factories(tmp_path: Path) -> None:
    """Fresh storage-factory closures for one session should cause one boundary open."""
    store = _recording_store(tmp_path, "general")
    factory_calls = 0

    with queued_message_signal_context(_StaticQueuedState(pending=True)) as notice_context:
        messages: list[OwnedMessage] = []
        await _after_batch(messages, OwnedMessage(role="tool", content="result"))
        _seed(store, _completed("completed-run", messages))

        def fresh_storage_factory() -> Callable[[], ConversationStore]:
            def storage_factory() -> ConversationStore:
                nonlocal factory_calls
                factory_calls += 1
                return store

            return storage_factory

        for _ in range(2):
            register_queued_notice_storage(
                storage_factory=fresh_storage_factory(),
                session_id="session-1",
                session_kind="agent",
                entity_name="general",
            )
        await ai_runtime.finalize_queued_notice_response_turn_async(notice_context)

    assert factory_calls == 1


@pytest.mark.asyncio
async def test_error_responses_never_attach_notices_to_prior_completed_run(tmp_path: Path) -> None:
    """Failed responses must not accumulate their notice facts on an earlier turn."""
    store = _recording_store(tmp_path, "general")
    session = _seed(
        store,
        _completed(
            "prior-completed",
            [
                OwnedMessage(role="user", content="Prior request"),
                OwnedMessage(role="assistant", content="Prior answer"),
            ],
        ),
    )

    for index in range(3):
        with queued_message_signal_context(_StaticQueuedState(pending=True)) as notice_context:
            messages: list[OwnedMessage] = []
            await _after_batch(messages, OwnedMessage(role="tool", content=f"failed result {index}"))
            error_run = _completed(f"error-run-{index}", messages)
            error_run.status = OwnedRunStatus.error
            store.save_run(session, error_run, record_usage=True)
            register_queued_notice_storage(
                storage_factory=lambda: store,
                session_id="session-1",
                session_kind="agent",
                entity_name="general",
            )
            await ai_runtime.finalize_queued_notice_response_turn_async(notice_context)

    stored = _stored_runs(store)
    assert [run.run_id for run in stored] == ["prior-completed", "error-run-0", "error-run-1", "error-run-2"]
    assert sum(_notice_count(run.messages) for run in stored) == 0


@pytest.mark.asyncio
async def test_response_lifecycle_finalizes_notice_after_locked_operation(tmp_path: Path) -> None:
    """The lifecycle boundary should persist the exact notice shown to the model."""
    lifecycle = ResponseLifecycleCoordinator()
    target = MessageTarget.resolve("!room:localhost", "$thread", "$event")
    envelope = _envelope(source_event_id="$event", target=target)
    store = _recording_store(tmp_path, "general")
    notice_text = "Custom queued notice."
    observed_live_counts: list[int] = []

    async def locked_operation(_target: MessageTarget) -> str:
        lifecycle._get_or_create_queued_signal(target).add_waiting_human_message("$newer")
        messages: list[OwnedMessage] = []
        await _after_batch(messages, OwnedMessage(role="tool", content="result"), notice_text=notice_text)
        _seed(store, _completed("completed-run", messages))
        register_queued_notice_storage(
            storage_factory=lambda: store,
            session_id="session-1",
            session_kind="agent",
            entity_name="general",
        )
        observed_live_counts.append(_notice_count(messages, marker=True))
        return "$response"

    result = await lifecycle.run_locked_response(
        target=target,
        response_envelope=envelope,
        pipeline_timing=None,
        locked_operation=locked_operation,
    )

    assert result == "$response"
    assert observed_live_counts == [1]
    [stored_run] = _stored_runs(store)
    assert _notice_count(stored_run.messages, marker=True) == 0
    persisted_notices = _notice_messages(stored_run.messages, marker="persisted")
    assert [message.content for message in persisted_notices] == [notice_text]
    assert len(store.saves) == 1


@pytest.mark.asyncio
async def test_response_lifecycle_finalizes_error_state_when_operation_raises(tmp_path: Path) -> None:
    """The lifecycle finally block should remove live state when the operation raises."""
    lifecycle = ResponseLifecycleCoordinator()
    target = MessageTarget.resolve("!room:localhost", "$thread", "$event")
    envelope = _envelope(source_event_id="$event", target=target)
    store = _recording_store(tmp_path, "general")

    async def locked_operation(_target: MessageTarget) -> str:
        lifecycle._get_or_create_queued_signal(target).add_waiting_human_message("$newer")
        messages: list[OwnedMessage] = []
        await _after_batch(messages, OwnedMessage(role="tool", content="failed result"))
        error_run = _completed("error-run", messages)
        error_run.status = OwnedRunStatus.error
        _seed(store, error_run)
        register_queued_notice_storage(
            storage_factory=lambda: store,
            session_id="session-1",
            session_kind="agent",
            entity_name="general",
        )
        error_message = "provider failed"
        raise RuntimeError(error_message)

    with pytest.raises(RuntimeError, match="provider failed"):
        await lifecycle.run_locked_response(
            target=target,
            response_envelope=envelope,
            pipeline_timing=None,
            locked_operation=locked_operation,
        )

    [stored_run] = _stored_runs(store)
    assert _notice_count(stored_run.messages) == 0


@pytest.mark.asyncio
async def test_response_turn_finalization_is_idempotent_for_one_persisted_notice(tmp_path: Path) -> None:
    """Repeated boundary finalization should not rewrite an already-normalized response."""
    store = _recording_store(tmp_path, "general")

    with queued_message_signal_context(_StaticQueuedState(pending=True)) as notice_context:
        await _after_batch([], OwnedMessage(role="tool", content="result"))
        persisted_notice = _owned_notice(notice_context.response_turn_id, marker="persisted")
        _seed(store, _completed("completed-run", [persisted_notice]))
        original_stored = _stored_runs(store)[0].messages[0].to_dict()
        register_queued_notice_storage(
            storage_factory=lambda: store,
            session_id="session-1",
            session_kind="agent",
            entity_name="general",
        )
        await ai_runtime.finalize_queued_notice_response_turn_async(notice_context)
        await ai_runtime.finalize_queued_notice_response_turn_async(notice_context)

    assert store.saves == []
    assert _stored_runs(store)[0].messages[0].to_dict() == original_stored


def test_pre_replay_crash_recovery_preserves_prior_persisted_and_active_live_notice(tmp_path: Path) -> None:
    """Recovery should persist prior live state without touching prior records or the active response."""
    store = _recording_store(tmp_path, "general")

    with queued_message_signal_context(_StaticQueuedState(pending=False)) as notice_context:
        prior_persisted = _owned_notice("already-persisted", marker="persisted")
        prior_dump = prior_persisted.to_dict()
        active_live = _owned_notice(notice_context.response_turn_id)
        session = _seed(store, _completed("completed-run", [_owned_notice("crash-left"), prior_persisted, active_live]))
        ai_runtime.scrub_queued_notice_session_context(
            scope_context=ScopeSessionContext(
                scope=HistoryScope(kind="agent", scope_id="general"),
                storage=store,
                session=session,
                session_id=session.session_id,
            ),
            entity_name="general",
        )

    stored_messages = _stored_runs(store)[0].messages
    assert prior_persisted.to_dict() == prior_dump
    assert _notice_count(stored_messages, marker="persisted", response_turn_id="already-persisted") == 1
    assert _notice_count(stored_messages, marker="persisted", response_turn_id="crash-left") == 1
    assert _notice_count(stored_messages, marker=True, response_turn_id="crash-left") == 0
    assert _notice_count(stored_messages, marker=True, response_turn_id=notice_context.response_turn_id) == 1


def test_notice_provider_data_survives_mindroom_sqlite_round_trip(tmp_path: Path) -> None:
    """MindRoom's prompt-sanitizing SQLite storage should preserve notice ownership metadata."""
    response_turn_id = "response-1"
    _seed(_queued_notice_storage(tmp_path), _completed("run-1", [_owned_notice(response_turn_id)]))

    session = _queued_notice_storage(tmp_path).load_session("session-1", "agent")

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
        notices = _notice_messages(messages, marker=True)
        assert len(notices) == 1
        provider_data = notices[0].provider_data
        assert isinstance(provider_data, dict)
        response_turn_id = provider_data[_QUEUED_NOTICE_RESPONSE_TURN_ID_KEY]
        assert isinstance(response_turn_id, str)
        response_1_ids.append(response_turn_id)
        messages.append(OwnedMessage(role="assistant", content="First response handoff"))
        _seed(_queued_notice_storage(tmp_path), _completed("run-1", messages))
        register_queued_notice_storage(
            storage_factory=lambda: _queued_notice_storage(tmp_path),
            session_id="session-1",
            session_kind="agent",
            entity_name="general",
        )
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
    session = store.load_session("session-1", "agent")
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
                    system=(),
                    summary=None,
                    history=history,
                    turn=TurnMessages(prompt=OwnedMessage(role="user", content="Second request")),
                ),
                ctx=context(run_id="run-2", session_id="session-1"),
                persist=persist_to(store, session),
            ),
        )
        seen = list(model.requests[0].messages)
        prior = next(message for message in seen if message.content == QUEUED_MESSAGE_NOTICE_TEXT)
        assert prior.from_history is True
        assert prior.provider_data == {
            _QUEUED_NOTICE_MARKER_KEY: "persisted",
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
        assert _notice_count(seen, marker="persisted", response_turn_id=response_1_id) == 1
        assert _notice_count(seen, response_turn_id=response_2_context.response_turn_id) == 0

    persisted = _queued_notice_storage(tmp_path).load_session("session-1", "agent")
    assert persisted is not None
    response_1_run, response_2_run = persisted.runs
    assert response_2_run.content == "ok"
    assert all(message.content != QUEUED_MESSAGE_NOTICE_TEXT for message in response_2_run.messages)
    persisted_notices = _notice_messages(response_1_run.messages, marker="persisted", response_turn_id=response_1_id)
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
    monkeypatch.setattr(judgment_llm, "build_model_provider", lambda *_: judge)
    envelope = _envelope(target=MessageTarget.resolve("!room:localhost", "$thread", "$event"))
    prompt = "Question: Install the dependency?\nSelected option: yes (install)" if selection else "hello"
    if selection:
        envelope = replace(envelope, body="The user selected: yes")
    preparation = _payload_preparation(envelope.target)
    if pending_media:
        preparation = replace(preparation, payload_inputs=replace(preparation.payload_inputs, raw_audio_fallback=True))
    request = ResponseRequest(
        sources=ResponseSources(("$event",), ("$event",)),
        prompt=prompt,
        thread_history=[],
        response_envelope=envelope,
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
        progress = runner._lifecycle_coordinator.visible_progress_callback(envelope.target)
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

    async def locked_operation(_target: MessageTarget, _placeholder: object) -> str:
        await runner._prepare_request_after_lock(replace(request, payload_preparation=None))
        note_progress("I have located the requested file")
        await collect(
            run_agent(
                spec(model, toolkit(queue_followup)),
                [OwnedMessage(role="user", content="Do the task")],
                ctx=context(),
                hooks=LoopHooks(after_tool_batch=(queued_notice_after_tool_batch("WRAP UP NOW"),)),
            ),
        )
        return "$response"

    try:
        assert (
            await runner._run_locked_response_lifecycle(
                request,
                response_kind="agent",
                locked_operation=locked_operation,
            )
            == "$response"
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
        assert runner._lifecycle_coordinator.visible_progress_callback(envelope.target) is None
        if enabled and not pending_media:
            evidence = "\n".join(str(message.content) for message in judge.requests[0].messages)
            assert "I have located the requested file" in evidence
            assert "Later text" not in evidence
            if selection:
                assert "Install the dependency?" in evidence
                assert "yes (install)" in evidence
    finally:
        for reservation in reservations:
            reservation.cancel()
