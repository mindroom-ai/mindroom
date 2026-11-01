"""Focused unit tests for the turn owners built from their constructor-injected collaborators.

These tests construct ``TurnIngress``, ``TurnDispatcher``, ``InteractiveSelections``, and
``VoiceIngress`` directly from their deps — no ``AgentBot``/orchestrator boot — with real ``TurnPolicy``, ``ConversationResolver``,
``TurnStore`` (journal-backed turn rows), and ``CoalescingGate`` collaborators, and typed
recording fakes only at the execution and delivery seams (``ResponseRunner``,
``DeliveryGateway``). They pin the turn contract: precheck filtering (via ``IngressValidator``),
replay-guard rejection, policy decision routing, ingress-level command dispatch
with terminal TurnStore outcomes (commands are control inputs that never enter
the coalescing gate), durable dedup across restarts, and the interactive
selection path.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field, fields, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from pydantic import ValidationError

from mindroom import constants, interactive
from mindroom.attachments.storage import register_local_attachment
from mindroom.authorization import ReplyMembershipPendingError
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot_runtime_view import BotRuntimeState, RunPhase
from mindroom.coalescing.batch import (
    CoalescingKey,
    PendingEvent,
    PreparedTurn,
    RequesterCoalescingOwner,
    active_follow_up_coalescing_key,
    build_prepared_turn,
    requester_coalescing_key,
)
from mindroom.coalescing.gate import CoalescingGate, IngressAdmissionClosedError, ReadyPendingEvent
from mindroom.command_turn_executor import CommandTurnExecutor, CommandTurnExecutorDeps
from mindroom.commands.parsing import CommandType, command_parser
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.config.participation import ParticipationConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.conversation_resolver import ConversationResolver, ConversationResolverDeps, MessageContext
from mindroom.conversation_state_writer import ConversationStateWriter, ConversationStateWriterDeps
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.desktop.credentials import load_desktop_credentials, save_desktop_credentials
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.dispatch_handoff import PreparedIngress
from mindroom.dispatch_source import (
    EDIT_SOURCE_KIND,
    EXTERNAL_TRIGGER_SOURCE_KIND,
    HOOK_DISPATCH_SOURCE_KIND,
    INTERACTIVE_SELECTION_SOURCE_KIND,
    MEDIA_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    SCHEDULED_SOURCE_KIND,
    SILENT_SCHEDULE_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    ScheduledHistoryBudget,
)
from mindroom.entity_resolution import entity_identity_registry
from mindroom.event_journal import (
    ConversationPage,
    DeliveryStage,
    EventClass,
    EventJournalStore,
    EventKind,
    InboundEvent,
    PrincipalStore,
    SemanticConsumer,
    TurnRecordStore,
    VisibleMessage,
    journal,
    turn_rows,
)
from mindroom.event_journal.identity import journal_principal_id
from mindroom.event_journal.opening import open_event_journal
from mindroom.hooks import (
    EVENT_MESSAGE_RECEIVED,
    HookContextSupport,
    HookRegistry,
    HookRegistryState,
    MessageReceivedContext,
    hook,
)
from mindroom.hooks.ingress import IngressHookRunner
from mindroom.inbound_turn_normalizer import (
    InboundTurnNormalizer,
    InboundTurnNormalizerDeps,
    TextNormalizationRequest,
)
from mindroom.ingress_validation import IngressValidator, IngressValidatorDeps
from mindroom.journal_dispatch import JournalCallbacks, JournalDispatcher
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import build_edit_event_content
from mindroom.matrix.conversation_reads import ConversationReader  # noqa: TC001
from mindroom.matrix.large_messages import prepare_large_message
from mindroom.matrix.thread_history_result import thread_history_result
from mindroom.matrix.thread_membership import ThreadMembershipLookupError
from mindroom.message_target import MessageTarget
from mindroom.reply_scope import ReplyRuntime
from mindroom.reply_turn.request import ResponseRequest
from mindroom.response_admission import ResponseAdmissionRefusedError
from mindroom.response_payload_preparation import DispatchPayloadInputs, ResponsePayloadPreparation
from mindroom.response_sources import Owned, ResponseSources, SourceFate, Unclaimed, UnclaimedReason
from mindroom.router.relay import execute_router_relay
from mindroom.runtime_shutdown import ORDERLY_SHUTDOWN
from mindroom.scheduling.executor import _prepare_scheduled_trigger
from mindroom.scheduling.tasks import ScheduledWorkflow
from mindroom.thread_models import resolve_thread_model_override
from mindroom.tool_system.runtime_context import ToolRuntimeSupport
from mindroom.turn_origin import TurnIntent
from mindroom.turn_policy import PreparedDispatch, ResponseAction, TurnPolicy
from mindroom.turn_record import TurnRecord
from mindroom.turn_store import (
    ClaimContended,
    ClaimRefused,
    TurnClaim,
    TurnStore,
    TurnStoreDeps,
)
from mindroom.turns.dispatch import TurnDispatchDeps, TurnDispatcher
from mindroom.turns.ingress import TurnIngress, TurnIngressDeps
from mindroom.turns.interactive_selection import (
    InteractiveSelectionDispatch,
    InteractiveSelections,
    InteractiveSelectionsDeps,
)
from mindroom.visible_response_reconciliation import VisibleResponseReconciler, VisibleResponseReconcilerDeps
from mindroom.voice.ingress import VoiceIngress, VoiceIngressDeps
from mindroom.voice.readiness import VoiceReadiness
from mindroom.voice.visible_voice_echo import VisibleVoiceEchoDeps, VisibleVoiceEchoLifecycle
from tests.access_schema_support import with_responder_access
from tests.authorization_helpers import (
    make_test_turn_policy_deps,
)
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_ingress_facts,
    make_matrix_client_mock,
    make_pending_event,
    make_relation_lookup,
    make_visible_message,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.journal_helpers import admit_dispatch_event
from tests.runtime_fakes import ready_runtime
from tests.turn_dispatch_helpers import make_test_turn, nothing_else_queued

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterable
    from pathlib import Path

    from mindroom.coalescing.ingress_ticket import IngressTicket
    from mindroom.delivery_gateway import DeliveryGateway, EditTextRequest, SendTextRequest
    from mindroom.hooks import MessageEnvelope
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.event_info import EventInfo
    from mindroom.matrix.thread_history_result import ThreadHistoryResult
    from mindroom.response_lifecycle import QueuedHumanNoticeReservation
    from mindroom.response_runner import ResponseRunner
    from mindroom.turns.facts import IngressFacts

_ROOM_ID = "!focused:localhost"
_SENDER = "@user:localhost"
_SELECTION_FACTS = make_ingress_facts(_SENDER, source_kind=INTERACTIVE_SELECTION_SOURCE_KIND)
_EVENT_ID = "$event:localhost"
_THREAD_ROOT = "$root:localhost"


@dataclass
class _ImmediateLifecycleReservation:
    """A response lifecycle an idle conversation grants at once."""

    async def wait_until_acquired(self) -> None:
        return None

    async def release(self) -> None:
        return None


@dataclass
class _RecordingResponseRunner:
    """Typed ResponseRunner stand-in that records the execution-seam requests.

    Mirrors the two runner seams the turn owners depend on: the request entry
    points (``generate_response`` / ``generate_team_response_helper``) and the
    runner-owned inbox-task ownership (``track_inbox_response``), where the
    dispatch handoff completes once the request signals
    ``on_lifecycle_lock_acquired`` and the response keeps running on a
    runner-owned task.
    """

    # Whether the reply answers its sources; one that does not leaves them owned but unanswered, as a reply an
    # approval holds does.
    answers: bool = True
    pre_lock_error: Exception | None = None
    deferred_sync_restart_error: asyncio.CancelledError | None = None
    # Participation declined the turn before any reply existed, leaving its caller to record and settle it.
    complete_without_response: bool = False
    requests: list[ResponseRequest] = field(default_factory=list)
    response_started: asyncio.Event = field(default_factory=asyncio.Event)
    inbox_tasks: list[asyncio.Task[None]] = field(default_factory=list)
    recovery_proof_checks: list[Callable[[], Awaitable[bool]]] = field(default_factory=list)
    # The room and exact sources each runner-owned task returns to the journal when it finishes.
    inbox_sources: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    claims: list[TurnClaim] = field(default_factory=list)
    # Whose run phase decides, as the runner's inbox does, whether orderly process shutdown closed task ownership.
    runtime: BotRuntimeState = field(kw_only=True)
    # Records a turn answered, as a reply's records do when its answer settles its sources.
    answered: Callable[[tuple[str, ...]], Awaitable[None]] | None = None
    # Whether a pending approval holds every conversation.
    held_for_approval: bool = False

    @property
    def inbox(self) -> _RecordingResponseRunner:
        """Stand in for the runner's inbox ownership too, which ``track_inbox_response`` records."""
        return self

    def has_active_response_for_target(self, target: MessageTarget) -> bool:  # noqa: ARG002
        return False

    def is_held_for_approval(self, target: MessageTarget) -> bool:  # noqa: ARG002
        return self.held_for_approval

    async def reserve_response_lifecycle(self, response_envelope: MessageEnvelope) -> _ImmediateLifecycleReservation:  # noqa: ARG002
        return _ImmediateLifecycleReservation()

    def reserve_waiting_human_message(
        self,
        *,
        target: MessageTarget,  # noqa: ARG002
        response_envelope: MessageEnvelope,  # noqa: ARG002
    ) -> QueuedHumanNoticeReservation:
        msg = "Queued-notice reservations are not part of these focused turn tests"
        raise AssertionError(msg)

    def track_inbox_response(
        self,
        response: Coroutine[Any, Any, None],
        *,
        name: str,
        room_id: str,
        recovery_proof_ready: Callable[[], Awaitable[bool]],
        claim: TurnClaim | None = None,
        source_event_ids: tuple[str, ...] = (),
    ) -> asyncio.Task[None]:
        if self.runtime.phase is RunPhase.SHUTTING_DOWN:
            response.close()
            if claim is not None:
                claim.release()
            raise ResponseAdmissionRefusedError
        self.recovery_proof_checks.append(recovery_proof_ready)
        self.inbox_sources.append((room_id, source_event_ids))
        task = asyncio.get_running_loop().create_task(response, name=name)
        if claim is not None:
            # Like the runner, release the handed-off claim when the task ends, however early.
            self.claims.append(claim)
            task.add_done_callback(lambda _finished: claim.release())
        self.inbox_tasks.append(task)
        return task

    async def settle_inbox_responses(self) -> None:
        """Await every runner-owned response task, surfacing its failures."""
        for task in self.inbox_tasks:
            await task

    async def generate_response(self, request: ResponseRequest) -> SourceFate:
        self.requests.append(request)
        self.response_started.set()
        if self.pre_lock_error is not None:
            raise self.pre_lock_error
        if request.on_lifecycle_lock_acquired is not None:
            request.on_lifecycle_lock_acquired()
        if (fate := await _suppressed_before_reply(request)) is not None:
            return fate
        if self.complete_without_response:
            return Unclaimed(UnclaimedReason.DECLINED)
        if self.answers and self.answered is not None:
            await self.answered(request.sources.logical_source_event_ids)
        if self.deferred_sync_restart_error is not None:
            raise self.deferred_sync_restart_error
        # The reply's records own the sources, whether or not they answered them.
        return Owned()

    async def generate_team_response_helper(
        self,
        request: ResponseRequest,
        *,
        member_names: object,  # noqa: ARG002
        team_mode: str,  # noqa: ARG002
    ) -> SourceFate:
        if self.pre_lock_error is not None:
            raise self.pre_lock_error
        if request.on_lifecycle_lock_acquired is not None:
            request.on_lifecycle_lock_acquired()
        if (fate := await _suppressed_before_reply(request)) is not None:
            return fate
        if self.answers and self.answered is not None:
            await self.answered(request.sources.logical_source_event_ids)
        return Owned()


async def _suppressed_before_reply(request: ResponseRequest) -> SourceFate | None:
    """Apply the locked source gate as the runner does before any reply exists; return the fate that ended it, if any."""
    if request.prepare_source_turn is None:
        return None
    preparation = await request.prepare_source_turn(request.thread_history)
    if not preparation.invalid:
        return None
    return Unclaimed(UnclaimedReason.TERMINAL_SOURCES, preparation)


@dataclass
class _RecordingDeliveryGateway:
    """Typed DeliveryGateway stand-in that records visible sends."""

    sent: list[SendTextRequest] = field(default_factory=list)
    edited: list[EditTextRequest] = field(default_factory=list)
    # Whether a reply owns the event a dispatch failure names, as one owns every acknowledgement.
    replies_own_events: bool = False
    failed_dispatches: list[tuple[str, str]] = field(default_factory=list)
    # Records a turn answered, as a reply's records do when its failure notice settles its sources.
    answered: Callable[[tuple[str, ...]], Awaitable[None]] | None = None
    reactions: list[tuple[str, str, str]] = field(default_factory=list)

    async def send_judgment_reaction(self, *, turn_id: str, room_id: str, event_id: str, key: str, kind: str) -> None:  # noqa: ARG002
        self.reactions.append((event_id, key, kind))

    async def supersede_replay(self, _source_event_ids: tuple[str, ...]) -> bool | None:
        """No reply has the sources in this recording-only delivery fixture."""
        return None

    async def send_text(self, request: SendTextRequest) -> str | None:
        self.sent.append(request)
        return f"$sent-{len(self.sent)}:localhost"

    async def edit_text(self, request: EditTextRequest) -> bool:
        self.edited.append(request)
        return True

    async def fail_reply_dispatch(self, event_id: str, error_text: str) -> bool:
        """Record the failure a reply shows, when the fixture says a reply owns the event."""
        if not self.replies_own_events:
            return False
        self.failed_dispatches.append((event_id, error_text))
        sent = next(request for index, request in enumerate(self.sent, 1) if event_id == f"$sent-{index}:localhost")
        if self.answered is not None and sent.reply_write is not None:
            await self.answered(sent.reply_write.span.sources.logical_source_event_ids)
        return True


@dataclass
class _SpyTurnPolicy:
    """Delegating TurnPolicy wrapper that counts policy evaluations."""

    inner: TurnPolicy
    plan_turn_calls: int = 0

    def can_reply_to_sender_in_room(
        self,
        sender_id: str,
        room_id: str,
        *,
        observed_room: nio.MatrixRoom | None = None,
    ) -> bool:
        return self.inner.can_reply_to_sender_in_room(sender_id, room_id, observed_room=observed_room)

    async def responder_candidates_for_room(
        self,
        room: nio.MatrixRoom,
        requester_user_id: str,
    ) -> list[object]:
        return list(await self.inner.responder_candidates_for_room(room, requester_user_id))

    def adaptive_participation(self, **kwargs: Any) -> Any:  # noqa: ANN401
        return self.inner.adaptive_participation(**kwargs)

    def router_mention_skips(self, room: nio.MatrixRoom, source: dict[str, Any]) -> bool | None:
        return self.inner.router_mention_skips(room, source)

    async def router_thread_skips(self, room: nio.MatrixRoom, requester_user_id: str, thread_history: Any) -> bool:  # noqa: ANN401
        return await self.inner.router_thread_skips(room, requester_user_id, thread_history)

    def effective_response_action(self, action: ResponseAction) -> ResponseAction:
        return self.inner.effective_response_action(action)

    async def plan_turn(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        self.plan_turn_calls += 1
        return await self.inner.plan_turn(*args, **kwargs)


async def _claim_semantic_consumer(_consumer: SemanticConsumer) -> bool:
    """Record nothing: these turns run outside a journal callback, as a fresh event's would before its claim."""
    return True


async def _no_personal_room_command(*_args: object) -> bool:
    """No personal-room onboarding is configured in these tests."""
    return False


class _UnusedEditRegenerator:
    """Edit-regeneration stub: the edit path must not run in these tests."""

    async def handle_message_edit(
        self,
        room: nio.MatrixRoom,  # noqa: ARG002
        event: nio.RoomMessageText,  # noqa: ARG002
        event_info: EventInfo,  # noqa: ARG002
        facts: IngressFacts,  # noqa: ARG002
    ) -> None:
        msg = "Edit regeneration must not run in focused turn tests"
        raise AssertionError(msg)


@dataclass
class _Harness:
    """One set of directly constructed turn owners plus their observable seams."""

    runtime: BotRuntimeState
    ingress: TurnIngress
    dispatcher: TurnDispatcher
    selections: InteractiveSelections
    voice: VoiceIngress
    policy: _SpyTurnPolicy
    runner: _RecordingResponseRunner
    gateway: _RecordingDeliveryGateway
    journal_store: EventJournalStore
    journal_principal: PrincipalStore
    turn_store: TurnStore
    gate: CoalescingGate
    gate_batches: list[PreparedTurn]
    ignored_dispatch_sources: list[tuple[str, ...]]
    retried_dispatch_sources: list[tuple[str, ...]]

    async def deliver(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
        """Run one inbound text turn end-to-end, settling runner-owned responses.

        Conversational responses keep running on runner-owned inbox tasks after
        the gate handoff completes, so assertions must wait for those tasks too.
        """
        await self.ingress.handle_text_event(room, event)
        await self.gate.drain_all()
        await self.runner.settle_inbox_responses()


def _conversation_page(messages: Iterable[ResolvedVisibleMessage]) -> ConversationPage:
    """Return the projected page a reader would serve for these messages."""
    return ConversationPage(
        messages=tuple(
            VisibleMessage(
                logical_event_id=message.event_id,
                room_id=_ROOM_ID,
                thread_id=_THREAD_ROOT,
                sender=message.sender,
                created_ts=message.timestamp or 0,
                revision_event_id=message.event_id,
                revision_ts=message.timestamp or 0,
                content=dict(message.content),
            )
            for message in messages
        ),
        refresh_pending=(),
        next_cursor=None,
    )


def _serve_conversation(harness: _Harness, messages: Iterable[ResolvedVisibleMessage]) -> None:
    """Point one harness's reader at these messages after it was constructed."""
    page = _conversation_page(messages)
    reader = harness.ingress.deps.resolver.deps.conversation_reader
    reader.read.return_value = page
    reader.read_strict.return_value = page


def _conversation_reader(thread_history: ThreadHistoryResult | None = None) -> ConversationReader:
    """Return a reader for a harness that has no journal store behind it.

    Not the same thing as stubbing a reader that does: these harnesses build a
    resolver directly, so there is no projection to reach and a fake page is
    the honest analogue of the conversation-cache mock they already carry.
    """
    page = _conversation_page(thread_history or ())
    return cast(
        "ConversationReader",
        SimpleNamespace(
            may_have_unread_history=AsyncMock(return_value=False),
            hydration_was_truncated=AsyncMock(return_value=False),
            read=AsyncMock(return_value=page),
            read_strict=AsyncMock(return_value=page),
        ),
    )


_VOICE_READINESS_FIELDS = frozenset({"normalizer", "turn_store", "logger"})


def _observe_admission_wait(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """Return an event set once a decision waits for the runtime to reopen response admission."""
    wait_for_admission_or_shutdown = harness.runtime.wait_for_admission_or_shutdown
    started = asyncio.Event()

    async def observed_wait() -> bool:
        started.set()
        return await wait_for_admission_or_shutdown()

    monkeypatch.setattr(harness.runtime, "wait_for_admission_or_shutdown", observed_wait)
    return started


def _replace_harness_deps(harness: _Harness, **changes: object) -> None:
    """Swap collaborators in place on every turn owner whose deps name them, voice readiness included.

    In place, because the gate keeps the owners it was built with.
    """
    owners = (harness.ingress, harness.dispatcher, harness.selections, harness.voice)
    known = _VOICE_READINESS_FIELDS.union(*(owner.deps.__dataclass_fields__ for owner in owners))
    unknown = set(changes) - known
    assert not unknown, f"not a turn owner collaborator: {sorted(unknown)}"
    for owner in owners:
        owner_changes = {name: value for name, value in changes.items() if name in owner.deps.__dataclass_fields__}
        if owner_changes:
            owner.deps = replace(owner.deps, **owner_changes)
    readiness_changes = {name: value for name, value in changes.items() if name in _VOICE_READINESS_FIELDS}
    if readiness_changes:
        harness.voice.deps = replace(
            harness.voice.deps,
            readiness=replace(harness.voice.deps.readiness, **readiness_changes),
        )


def _build_harness(
    config: Config,
    storage_path: Path,
    *,
    agent_name: str = "general",
    thread_history: ThreadHistoryResult | None = None,
    debounce_seconds: float = 0.0,
) -> _Harness:
    """Construct the turn owners from typed collaborators without booting a bot."""
    runtime_paths = runtime_paths_for(config)
    registry = entity_identity_registry(config, runtime_paths)
    matrix_id = registry.current_id(agent_name)
    logger = get_logger("test_turns_focused")
    runtime = BotRuntimeState(
        orchestrator=ready_runtime(config, runtime_paths),
        client=make_matrix_client_mock(user_id=matrix_id.full_id),
    )

    conversation_reader = _conversation_reader(thread_history)
    resolver = ConversationResolver(
        ConversationResolverDeps(
            runtime=runtime,
            logger=logger,
            runtime_paths=runtime_paths,
            agent_name=agent_name,
            matrix_id=matrix_id,
            relations=make_relation_lookup(),
            conversation_reader=conversation_reader,
        ),
    )
    normalizer = InboundTurnNormalizer(
        InboundTurnNormalizerDeps(
            runtime=runtime,
            logger=logger,
            storage_path=storage_path,
            runtime_paths=runtime_paths,
        ),
    )
    state_writer = ConversationStateWriter(
        ConversationStateWriterDeps(
            runtime=runtime,
            logger=logger,
            runtime_paths=runtime_paths,
            agent_name=agent_name,
        ),
    )
    hook_context = HookContextSupport(
        runtime=runtime,
        runtime_paths=runtime_paths,
        agent_name=agent_name,
        hook_registry_state=HookRegistryState(HookRegistry.empty()),
        conversation_reader=conversation_reader,
        logger=get_logger("tests.hooks"),
    )
    tool_runtime = ToolRuntimeSupport(
        runtime=runtime,
        runtime_paths=runtime_paths,
        storage_path=storage_path,
        agent_name=agent_name,
        matrix_id=matrix_id,
        resolver=resolver,
        hook_context=hook_context,
        membership=MagicMock(),
    )
    journal_store = open_event_journal(
        config.event_journal,
        runtime_paths=runtime_paths,
        storage_path=storage_path,
    ).store
    journal_principal = journal_store.principal(journal_principal_id(agent_name, matrix_id.full_id))
    turn_store = TurnStore(
        TurnStoreDeps(
            turn_records=journal_store.turn_records(agent_name),
            legacy_responses_file=None,
            state_writer=state_writer,
            tool_runtime=tool_runtime,
            response_recovery_state=journal_principal.response_recovery_state,
            # Resolved late: the retry recorder is defined below, with the turn owners'.
            retry_dispatch_sources=lambda room_id, event_ids: _retry_dispatch_sources(room_id, event_ids),
        ),
    )
    policy = _SpyTurnPolicy(
        TurnPolicy(
            make_test_turn_policy_deps(
                runtime=runtime,
                logger=logger,
                runtime_paths=runtime_paths,
                agent_name=agent_name,
                matrix_id=matrix_id,
                agent_reply_memberships=runtime.agent_reply_memberships,
            ),
        ),
    )

    async def _answered(logical_source_event_ids: tuple[str, ...]) -> None:
        # The reply's records complete the turn when they settle its sources.
        assert await turn_store.deps.turn_records.write(turn_rows.complete_turn_of, logical_source_event_ids)

    runner = _RecordingResponseRunner(runtime=runtime, answered=_answered)
    gateway = _RecordingDeliveryGateway(answered=_answered)
    gate_batches: list[PreparedTurn] = []
    ignored_dispatch_sources: list[tuple[str, ...]] = []
    retried_dispatch_sources: list[tuple[str, ...]] = []

    async def _settle_ignored_dispatch_sources(source_event_ids: tuple[str, ...]) -> None:
        ignored_dispatch_sources.append(source_event_ids)

    async def _dispatch_source_is_terminal(_source_event_id: str) -> bool:
        return False

    def _retry_dispatch_sources(_room_id: str, source_event_ids: tuple[str, ...]) -> None:
        retried_dispatch_sources.append(source_event_ids)

    async def _recover_config_confirmation_setup(_room_id: str, _preview_event_id: str) -> bool:
        return False

    visible_responses = VisibleResponseReconciler(
        VisibleResponseReconcilerDeps(
            runtime=runtime,
            logger=logger,
            response_sender=matrix_id.full_id,
            turn_store=turn_store,
            delivery_gateway=cast("DeliveryGateway", gateway),
            settle_ignored_sources=_settle_ignored_dispatch_sources,
            replies=ReplyRuntime(
                store=EventJournalStore.open_sqlite(storage_path / "replies.db").principal(
                    journal_principal_id(agent_name, matrix_id.full_id),
                ),
                entity_name=agent_name,
                generation="test-runtime",
                retry_sources=lambda _room_id, _event_ids: None,
                approval_ended=lambda _ended: None,
            ),
        ),
    )
    command_executor = CommandTurnExecutor(
        CommandTurnExecutorDeps(
            runtime=runtime,
            logger=logger,
            runtime_paths=runtime_paths,
            agent_name=agent_name,
            normalizer=normalizer,
            conversation_reader=make_conversation_reader_mock(),
            turn_policy=cast("TurnPolicy", policy),
            turn_store=turn_store,
            visible_responses=visible_responses,
            recover_config_confirmation_setup=_recover_config_confirmation_setup,
            controller_identity=lambda _entity_name: (_ for _ in ()).throw(
                AssertionError("status must not resolve the controller identity"),
            ),
        ),
    )

    ingress_validator = IngressValidator(
        IngressValidatorDeps(
            runtime=runtime,
            runtime_paths=runtime_paths,
            agent_name=agent_name,
            matrix_id=matrix_id,
            turn_store=turn_store,
            turn_policy=policy,
            settle_ignored_sources=_settle_ignored_dispatch_sources,
        ),
    )
    selections = InteractiveSelections(
        InteractiveSelectionsDeps(
            logger=logger,
            runtime_paths=runtime_paths,
            agent_name=agent_name,
            resolver=resolver,
            normalizer=normalizer,
            ingress=ingress_validator,
            response_runner=cast("ResponseRunner", runner),
            delivery_gateway=cast("DeliveryGateway", gateway),
            turn_store=turn_store,
            visible_responses=visible_responses,
            retry_dispatch_sources=_retry_dispatch_sources,
            settle_dispatch_sources=_settle_ignored_dispatch_sources,
            dispatch_source_is_terminal=_dispatch_source_is_terminal,
        ),
    )
    voice = VoiceIngress(
        VoiceIngressDeps(
            runtime=runtime,
            logger=logger,
            runtime_paths=runtime_paths,
            matrix_id=matrix_id,
            resolver=resolver,
            response_runner=cast("ResponseRunner", runner),
            delivery_gateway=cast("DeliveryGateway", gateway),
            readiness=VoiceReadiness(
                normalizer=normalizer,
                turn_store=turn_store,
                visible_echo=VisibleVoiceEchoLifecycle(
                    VisibleVoiceEchoDeps(
                        runtime=runtime,
                        logger=logger,
                        agent_name=agent_name,
                        delivery_gateway=cast("DeliveryGateway", gateway),
                        turn_store=turn_store,
                        router_turn_records=journal_store.turn_records(ROUTER_AGENT_NAME),
                        ingress=ingress_validator,
                    ),
                ),
                logger=logger,
            ),
        ),
    )
    dispatcher = TurnDispatcher(
        TurnDispatchDeps(
            runtime=runtime,
            logger=logger,
            runtime_paths=runtime_paths,
            agent_name=agent_name,
            pending_turns=journal_principal,
            resolver=resolver,
            normalizer=normalizer,
            command_executor=command_executor,
            turn_policy=cast("TurnPolicy", policy),
            ingress_hook_runner=IngressHookRunner(hook_context=hook_context),
            response_runner=cast("ResponseRunner", runner),
            delivery_gateway=cast("DeliveryGateway", gateway),
            turn_store=turn_store,
            ingress=ingress_validator,
            visible_responses=visible_responses,
            retry_dispatch_sources=_retry_dispatch_sources,
            selections=selections,
        ),
    )

    async def _dispatch_batch(turn: PreparedTurn) -> None:
        gate_batches.append(turn)
        await dispatcher.handle_prepared_turn(turn)

    gate = CoalescingGate(
        dispatch_turn=_dispatch_batch,
        debounce_seconds=lambda: debounce_seconds,
        is_shutting_down=lambda: False,
        on_dispatch_failure=_retry_dispatch_sources,
    )
    ingress = TurnIngress(
        TurnIngressDeps(
            runtime=runtime,
            logger=logger,
            runtime_paths=runtime_paths,
            agent_name=agent_name,
            matrix_id=matrix_id,
            interactive_questions=cast("PrincipalStore", AsyncMock()),
            resolver=resolver,
            normalizer=normalizer,
            turn_policy=cast("TurnPolicy", policy),
            response_runner=cast("ResponseRunner", runner),
            delivery_gateway=cast("DeliveryGateway", gateway),
            turn_store=turn_store,
            coalescing_gate=gate,
            edit_regenerator=_UnusedEditRegenerator(),
            ingress=ingress_validator,
            visible_responses=visible_responses,
            dispatcher=dispatcher,
            selections=selections,
            voice=voice,
            semantic_consumer=lambda: None,
            claim_semantic_consumer=_claim_semantic_consumer,
            personal_room_command=_no_personal_room_command,
        ),
    )
    return _Harness(
        runtime=runtime,
        ingress=ingress,
        dispatcher=dispatcher,
        selections=selections,
        voice=voice,
        policy=policy,
        runner=runner,
        gateway=gateway,
        journal_store=journal_store,
        journal_principal=journal_principal,
        turn_store=turn_store,
        gate=gate,
        gate_batches=gate_batches,
        ignored_dispatch_sources=ignored_dispatch_sources,
        retried_dispatch_sources=retried_dispatch_sources,
    )


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Two-agent config bound to isolated runtime paths."""
    return bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General"),
                "research": AgentConfig(display_name="Research"),
            },
        ),
        test_runtime_paths(tmp_path / "runtime"),
    )


def _entity_user_id(config: Config, entity_name: str) -> str:
    return entity_identity_registry(config, runtime_paths_for(config)).current_id(entity_name).full_id


def _room_with_members(config: Config, *entity_names: str, room_id: str = _ROOM_ID) -> nio.MatrixRoom:
    room = nio.MatrixRoom(room_id, _entity_user_id(config, entity_names[0]) if entity_names else _SENDER)
    room.add_member(_SENDER, _SENDER, None)
    for entity_name in entity_names:
        user_id = _entity_user_id(config, entity_name)
        room.add_member(user_id, user_id, None)
    return room


def _text_event(
    body: str,
    *,
    event_id: str = _EVENT_ID,
    thread_id: str | None = None,
    origin_server_ts: int = 1_000_000,
    sender: str = _SENDER,
) -> nio.RoomMessageText:
    content: dict[str, Any] = {"body": body, "msgtype": "m.text"}
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    return nio.RoomMessageText.from_dict(
        {
            "content": content,
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": origin_server_ts,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )


def _emote_event(
    body: str,
    *,
    event_id: str = _EVENT_ID,
    origin_server_ts: int = 1_000_000,
) -> nio.RoomMessageEmote:
    """Return one `/me` message: an ordinary user turn written in third person."""
    return cast(
        "nio.RoomMessageEmote",
        nio.RoomMessageEmote.from_dict(
            {
                "content": {"body": body, "msgtype": "m.emote"},
                "event_id": event_id,
                "sender": _SENDER,
                "origin_server_ts": origin_server_ts,
                "room_id": _ROOM_ID,
                "type": "m.room.message",
            },
        ),
    )


def _image_event(*, event_id: str) -> nio.RoomMessageImage:
    return cast(
        "nio.RoomMessageImage",
        nio.RoomMessageImage.from_dict(
            {
                "content": {
                    "body": "photo.jpg",
                    "info": {"mimetype": "image/jpeg"},
                    "msgtype": "m.image",
                    "url": "mxc://localhost/photo",
                },
                "event_id": event_id,
                "sender": _SENDER,
                "origin_server_ts": 1_000_000,
                "room_id": _ROOM_ID,
                "type": "m.room.message",
            },
        ),
    )


def _managed_file_event(
    config: Config,
    *,
    event_id: str,
    thread_id: str,
    sender_name: str = "general",
) -> nio.RoomMessageFile:
    """Return an unmentioned threaded file event authored by a managed agent."""
    return cast(
        "nio.RoomMessageFile",
        nio.RoomMessageFile.from_dict(
            {
                "event_id": event_id,
                "sender": _entity_user_id(config, sender_name),
                "origin_server_ts": 1_000,
                "room_id": _ROOM_ID,
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.file",
                    "body": "response.md",
                    "filename": "response.md",
                    "url": "mxc://localhost/managed-attachment",
                    "info": {"mimetype": "text/markdown"},
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": thread_id,
                        "is_falling_back": True,
                        "m.in_reply_to": {"event_id": thread_id},
                    },
                },
            },
        ),
    )


def _obligation_runner(
    harness: _Harness,
    *,
    tracking_path: Path,
    matrix_user_id: str,
    entity_name: str,
    room: nio.MatrixRoom,
) -> JournalDispatcher:
    async def noop(_room: nio.MatrixRoom, _event: nio.Event) -> None:
        pass

    store = EventJournalStore.open_sqlite(tracking_path / "event_journal.db")
    return JournalDispatcher(
        store=store.principal(journal_principal_id(entity_name, matrix_user_id)),
        callbacks=JournalCallbacks(
            on_message=harness.ingress.handle_text_event,
            on_media=harness.ingress.handle_media_event,
            on_reaction=cast("Any", noop),
            on_approval=cast("Any", noop),
            on_room_lifecycle=cast("Any", noop),
            on_redaction=cast("Any", noop),
            on_approval_continuation=AsyncMock(return_value=None),
            source_has_live_owner=harness.gate.has_pending_source_event,
            turn_has_live_claim=harness.turn_store.has_live_turn_claim,
            replies_ended=lambda _reply_ids: None,
            on_job_follow_up=AsyncMock(),
            on_job_cancel=AsyncMock(),
        ),
        room_for_id=lambda _room_id: room,
        schedule_trigger_sender_is_managed=lambda sender: sender == matrix_user_id,
    )


async def _settle_dispatcher(harness: _Harness, dispatcher: JournalDispatcher) -> None:
    """Drive test-owned response tasks and honor retry cooldowns until journal settlement."""
    async with asyncio.timeout(5):
        while await dispatcher.store.unsettled_event_ids():
            await dispatcher.drain_once()
            await harness.gate.drain_all()
            await harness.runner.settle_inbox_responses()
            await asyncio.sleep(0.01)


def _router_relay_event(
    config: Config,
    *,
    event_id: str,
    original_event_id: str,
    body: str,
    origin_server_ts: int,
) -> nio.RoomMessageText:
    """Build one trusted router relay that explicitly replies to its human source."""
    return nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": body,
                "msgtype": "m.text",
                constants.ORIGINAL_SENDER_KEY: _SENDER,
                constants.SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": _THREAD_ROOT,
                    "m.in_reply_to": {"event_id": original_event_id},
                },
            },
            "event_id": event_id,
            "sender": _entity_user_id(config, ROUTER_AGENT_NAME),
            "origin_server_ts": origin_server_ts,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )


@pytest.mark.asyncio
async def test_replayed_turn_is_rejected_before_policy_and_compacted(config: Config, tmp_path: Path) -> None:
    """A turn superseded by a newer unresponded requester message never reaches policy.

    The dispatch replay guard must consume the exact callback obligation without
    recording a turn, evaluating policy, or sending anything.
    """
    newer_history = thread_history_result(
        [
            make_visible_message(
                sender=_SENDER,
                body="newer follow-up from the same requester",
                event_id="$newer:localhost",
                timestamp=2_000_000,
            ),
        ],
        is_full_history=True,
    )
    harness = _build_harness(config, tmp_path, thread_history=newer_history)
    room = _room_with_members(config, "general")
    event = _text_event("older superseded message", thread_id=_THREAD_ROOT, origin_server_ts=1_000_000)

    await harness.deliver(room, event)

    assert harness.policy.plan_turn_calls == 0
    assert harness.runner.requests == []
    assert harness.gateway.sent == []
    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id) is False
    assert harness.ignored_dispatch_sources == [(event.event_id,)]


@pytest.mark.asyncio
async def test_detached_response_hands_its_exact_sources_to_the_inbox_retry(config: Config, tmp_path: Path) -> None:
    """The detached response owner must hand its exact sources to the inbox, whose finish retry returns them."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _text_event("please answer", thread_id=_THREAD_ROOT)

    await harness.deliver(room, event)

    assert harness.runner.inbox_sources == [(_ROOM_ID, (event.event_id,))]


@pytest.mark.asyncio
async def test_a_replayed_turn_leaves_its_reply_to_the_claim(
    config: Config,
    tmp_path: Path,
) -> None:
    """A pending intent's reply is named by the reply's records when the turn claims it, not found by scanning Matrix."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _text_event("please recover", thread_id=_THREAD_ROOT)
    target = harness.ingress.deps.resolver.build_message_target(
        room_id=_ROOM_ID,
        thread_id=_THREAD_ROOT,
        reply_to_event_id=event.event_id,
        event_source=event.source,
    )
    pending_turn = harness.turn_store.attach_response_context(
        TurnRecord.create([event.event_id], completed=False),
        history_scope=harness.turn_store.response_history_scope(
            ResponseAction(kind="individual"),
            requester_user_id=_SENDER,
        ),
        conversation_target=target,
    )
    await harness.turn_store.record_pending_turn(pending_turn)

    with patch(
        "mindroom.visible_response_reconciliation.find_response_event_ids_via_room_messages",
    ) as find_response_event_ids:
        await harness.deliver(room, event)

    assert len(harness.runner.requests) == 1
    assert harness.runner.requests[0].existing_event_id is None
    find_response_event_ids.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_prefers_untracked_terminal_fallback_over_persisted_placeholder(
    config: Config,
    tmp_path: Path,
) -> None:
    """A hard crash after fallback send must adopt that terminal event, not its failed placeholder."""
    harness = _build_harness(config, tmp_path)
    pending_turn = await harness.turn_store.record_pending_turn(
        _in_room(TurnRecord.create([_EVENT_ID], response_event_id="$placeholder:localhost", completed=False)),
    )
    assert pending_turn is not None

    with patch(
        "mindroom.visible_response_reconciliation.find_response_event_ids_via_room_messages",
        return_value=frozenset({"$fallback:localhost"}),
    ) as find_response_event_ids:
        recovered = await harness.dispatcher.deps.visible_responses.recovered_response_event_id(
            pending_turn.record,
            room_id=_ROOM_ID,
        )

    assert recovered == "$fallback:localhost"
    response_filter = find_response_event_ids.await_args.kwargs["response_source_filter"]
    assert response_filter({"content": {constants.STREAM_STATUS_KEY: constants.STREAM_STATUS_COMPLETED}})
    assert not response_filter(
        {
            "content": {
                constants.STREAM_STATUS_KEY: constants.STREAM_STATUS_COMPLETED,
                constants.VISIBLE_ROUTER_VOICE_ECHO_KEY: True,
            },
        },
    )


@pytest.mark.asyncio
async def test_recovery_lookup_excludes_visible_voice_echo(config: Config, tmp_path: Path) -> None:
    """Router recovery must distinguish its relay from an earlier visible voice echo."""
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    pending_turn = await harness.turn_store.record_pending_turn(
        _in_room(TurnRecord.create([_EVENT_ID], completed=False)),
    )
    assert pending_turn is not None

    with patch(
        "mindroom.visible_response_reconciliation.find_response_event_ids_via_room_messages",
        return_value=frozenset({"$voice-echo:localhost", "$relay:localhost"}),
    ):
        recovered = await harness.dispatcher.deps.visible_responses.recovered_response_event_id(
            pending_turn.record,
            room_id=_ROOM_ID,
            excluded_event_ids=("$voice-echo:localhost",),
        )

    assert recovered == "$relay:localhost"


@pytest.mark.asyncio
async def test_response_intent_is_durable_before_any_matrix_response(config: Config, tmp_path: Path) -> None:
    """A restart can recognize incomplete work even if the first visible send wins the crash race."""
    harness = _build_harness(config, tmp_path)
    harness.runner.answers = False
    room = _room_with_members(config, "general")
    event = _text_event("please persist response intent")

    await harness.deliver(room, event)

    record = await harness.turn_store.get_turn_record(event.event_id)
    assert record is not None
    assert record.response_event_id is None
    assert record.completed is False


def _in_room(record: TurnRecord, room_id: str = _ROOM_ID) -> TurnRecord:
    """Give a seeded turn the conversation every runtime write names."""
    return replace(
        record,
        conversation_target=record.conversation_target
        or MessageTarget.resolve(room_id, None, record.source_event_ids[-1]),
    )


async def _redact(harness: _Harness, event_id: str, room_id: str) -> None:
    """Record a redaction as its admission does: once for the agent, applied to every login of it."""
    await harness.turn_store.deps.turn_records.backend.write(
        lambda transaction: journal.redact_for_agent(
            transaction,
            harness.turn_store.deps.turn_records.agent_name,
            room_id=room_id,
            event_id=event_id,
            receipt_order=0,
        ),
    )


@pytest.mark.asyncio
async def test_redacted_pending_turn_settles_deferred_source_before_response(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redaction racing the pending-intent write must terminally settle its callback owner."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _text_event("redact before response")
    real_record_pending = harness.turn_store.record_pending_turn

    async def redact_then_record(turn_record: TurnRecord) -> turn_rows.PendingTurn | None:
        await _redact(harness, event.event_id, room.room_id)
        return await real_record_pending(turn_record)

    monkeypatch.setattr(harness.turn_store, "record_pending_turn", redact_then_record)

    await harness.deliver(room, event)

    assert harness.runner.requests == []
    assert harness.ignored_dispatch_sources == [(event.event_id,)]


@pytest.mark.asyncio
async def test_a_batch_source_deleted_during_its_pending_write_leaves_the_rest_owing_an_answer(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting one message of a pending batch settles only it; the other answers as its own turn."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    events = [
        _text_event("first", event_id="$first:localhost"),
        _text_event("second", event_id="$second:localhost"),
    ]
    turn = build_prepared_turn(
        CoalescingKey(_ROOM_ID, None, RequesterCoalescingOwner(_SENDER)),
        [
            make_pending_event(event, room, source_kind=MESSAGE_SOURCE_KIND, requester_user_id=_SENDER)
            for event in events
        ],
        queues_other_run=nothing_else_queued,
    )
    real_record_pending = harness.turn_store.record_pending_turn

    async def redact_then_record(turn_record: TurnRecord) -> turn_rows.PendingTurn | None:
        await _redact(harness, events[0].event_id, room.room_id)
        return await real_record_pending(turn_record)

    monkeypatch.setattr(harness.turn_store, "record_pending_turn", redact_then_record)

    await harness.dispatcher.handle_prepared_turn(turn)

    assert harness.runner.requests == []
    assert harness.ignored_dispatch_sources == [(events[0].event_id,)]
    assert harness.retried_dispatch_sources == [(events[1].event_id,)]


@pytest.mark.asyncio
async def test_a_non_model_delivery_batch_with_a_deleted_source_leaves_the_rest_owing_an_answer(
    config: Config,
    tmp_path: Path,
) -> None:
    """A router relay or command over a batch settles only its deleted source; the other answers as its own turn."""
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    first, second = "$first:localhost", "$second:localhost"
    batch = TurnRecord.create([first, second], completed=False, requester_id=_SENDER)
    # The delivering dispatch holds the batch's claim until it ends.
    claim = await harness.turn_store.claim(batch)
    assert isinstance(claim, TurnClaim)
    await _redact(harness, first, _ROOM_ID)

    prepared = await harness.dispatcher.deps.visible_responses.prepare_visible_delivery_turn(
        batch,
        requester_id=_SENDER,
        target=MessageTarget.resolve(_ROOM_ID, None, second),
    )

    assert prepared == (None, None)
    assert harness.gateway.sent == []
    assert harness.ignored_dispatch_sources == [(first,)]
    assert harness.retried_dispatch_sources == [], "the survivor waits for the delivering dispatch's claim"
    claim.release()
    assert harness.retried_dispatch_sources == [(second,)]
    assert not await harness.turn_store.is_handled(_ROOM_ID, second)


@pytest.mark.asyncio
async def test_locked_coalesced_redaction_settles_every_suppressed_source(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The locked redaction check must release every physical source in a suppressed batch."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    mention = _entity_user_id(config, "general")
    relay_event_ids = ("$relay-one:localhost", "$relay-two:localhost")
    relay_events = [
        _router_relay_event(
            config,
            event_id=event_id,
            original_event_id=human_event_id,
            body=f"{mention} {body}",
            origin_server_ts=timestamp,
        )
        for event_id, human_event_id, body, timestamp in (
            (relay_event_ids[0], "$human-one:localhost", "first", 1_000_000),
            (relay_event_ids[1], "$human-two:localhost", "second", 1_000_001),
        )
    ]
    turn = build_prepared_turn(
        CoalescingKey(_ROOM_ID, _THREAD_ROOT, RequesterCoalescingOwner(_SENDER)),
        [
            make_pending_event(
                event,
                room,
                source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                requester_user_id=_SENDER,
                payload_trusted=True,
                discovery_event_id=human_event_id,
            )
            for event, human_event_id in zip(
                relay_events,
                ("$human-one:localhost", "$human-two:localhost"),
                strict=True,
            )
        ],
        queues_other_run=nothing_else_queued,
    )
    real_generate_response = harness.runner.generate_response

    async def redact_before_locked_check(request: ResponseRequest) -> SourceFate:
        await _redact(harness, relay_event_ids[0], room.room_id)
        return await real_generate_response(request)

    monkeypatch.setattr(harness.runner, "generate_response", redact_before_locked_check)

    await harness.dispatcher.handle_prepared_turn(turn)
    await harness.runner.settle_inbox_responses()

    assert len(harness.runner.requests) == 1
    # No reply existed yet, so only the deleted source settles; the other answers as its own turn.
    assert harness.ignored_dispatch_sources == [(relay_event_ids[0],)]
    assert harness.retried_dispatch_sources == [(relay_event_ids[1],)]


@pytest.mark.asyncio
async def test_coalesced_router_relays_index_every_human_source_for_edit_lookup(
    config: Config,
    tmp_path: Path,
) -> None:
    """Every human source of coalesced relays must discover the shared response."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    mention = _entity_user_id(config, "general")
    relay_events = [
        _router_relay_event(
            config,
            event_id="$relay-one:localhost",
            original_event_id="$human-one:localhost",
            body=f"{mention} first",
            origin_server_ts=1_000_000,
        ),
        _router_relay_event(
            config,
            event_id="$relay-two:localhost",
            original_event_id="$human-two:localhost",
            body=f"{mention} second",
            origin_server_ts=1_000_001,
        ),
    ]
    batch = build_prepared_turn(
        CoalescingKey(_ROOM_ID, _THREAD_ROOT, RequesterCoalescingOwner(_SENDER)),
        [
            make_pending_event(
                event,
                room,
                source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                requester_user_id=_SENDER,
                payload_trusted=True,
                discovery_event_id=human_event_id,
            )
            for event, human_event_id in zip(
                relay_events,
                ("$human-one:localhost", "$human-two:localhost"),
                strict=True,
            )
        ],
        queues_other_run=nothing_else_queued,
    )

    await harness.dispatcher.handle_prepared_turn(batch)
    await harness.runner.settle_inbox_responses()

    assert harness.runner.requests[0].sources == ResponseSources(
        pending_event_ids=("$relay-two:localhost", "$relay-one:localhost"),
        logical_source_event_ids=("$relay-one:localhost", "$relay-two:localhost"),
        discovery_event_ids=("$human-one:localhost", "$human-two:localhost"),
    )
    first_lookup = await harness.turn_store.get_turn_record("$human-one:localhost")
    second_lookup = await harness.turn_store.get_turn_record("$human-two:localhost")
    assert first_lookup is not None
    assert first_lookup == second_lookup
    assert first_lookup.source_event_ids == ("$relay-one:localhost", "$relay-two:localhost")
    assert first_lookup.discovery_event_ids == ("$human-one:localhost", "$human-two:localhost")
    assert first_lookup.source_event_metadata is not None
    assert first_lookup.source_event_metadata["$relay-one:localhost"].discovery_event_id == "$human-one:localhost"
    assert first_lookup.source_event_metadata["$relay-two:localhost"].discovery_event_id == "$human-two:localhost"


@pytest.mark.asyncio
async def test_single_router_relay_persists_human_prompt_ownership(config: Config, tmp_path: Path) -> None:
    """A routed singleton must retain the alias-to-relay prompt mapping for later edits."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    mention = _entity_user_id(config, "general")
    relay = _router_relay_event(
        config,
        event_id="$relay:localhost",
        original_event_id="$human:localhost",
        body=f"{mention} single",
        origin_server_ts=1_000_000,
    )
    batch = build_prepared_turn(
        CoalescingKey(_ROOM_ID, _THREAD_ROOT, RequesterCoalescingOwner(_SENDER)),
        [
            make_pending_event(
                relay,
                room,
                source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                requester_user_id=_SENDER,
                payload_trusted=True,
                discovery_event_id="$human:localhost",
            ),
        ],
        queues_other_run=nothing_else_queued,
    )

    await harness.dispatcher.handle_prepared_turn(batch)
    await harness.runner.settle_inbox_responses()

    record = await harness.turn_store.get_turn_record("$human:localhost")
    assert record is not None
    assert record.source_event_metadata is not None
    assert record.source_event_metadata["$relay:localhost"].discovery_event_id == "$human:localhost"
    assert record.prompt_source_event_id("$human:localhost") == "$relay:localhost"


@pytest.mark.asyncio
async def test_router_relay_is_admitted_while_original_ingress_claim_is_active(
    config: Config,
    tmp_path: Path,
) -> None:
    """A human alias claim cannot veto its distinct physical router relay."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    original_event_id = "$human-active:localhost"
    relay = _router_relay_event(
        config,
        event_id="$relay-active:localhost",
        original_event_id=original_event_id,
        body=f"{_entity_user_id(config, 'general')} respond",
        origin_server_ts=1_000_000,
    )
    original_claim = await harness.turn_store.claim(TurnRecord.create([original_event_id], completed=False))
    assert isinstance(original_claim, TurnClaim)

    try:
        await harness.deliver(room, relay)
    finally:
        original_claim.release()

    assert len(harness.runner.requests) == 1
    metadata = harness.runner.requests[0].matrix_run_metadata
    assert metadata is not None
    assert metadata[constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY] == ["$relay-active:localhost"]


@pytest.mark.asyncio
async def test_completed_router_alias_rejects_later_physical_relay(config: Config, tmp_path: Path) -> None:
    """A new relay event cannot replay a human source already completed through another relay."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    original_event_id = "$human-complete:localhost"
    await harness.turn_store.record_turn(
        TurnRecord.create(
            ["$relay-complete:localhost"],
            discovery_event_ids=[original_event_id],
            response_event_id="$response-complete:localhost",
        ),
        room_id=_ROOM_ID,
    )
    duplicate_relay = _router_relay_event(
        config,
        event_id="$relay-duplicate:localhost",
        original_event_id=original_event_id,
        body=f"{_entity_user_id(config, 'general')} duplicate",
        origin_server_ts=1_000_001,
    )

    await harness.deliver(room, duplicate_relay)

    assert harness.runner.requests == []
    assert await harness.turn_store.get_turn_record("$relay-duplicate:localhost") is None


@pytest.mark.asyncio
async def test_duplicate_router_relay_claim_settles_without_restart(config: Config, tmp_path: Path) -> None:
    """A relay blocked by the same human alias must not remain deferred without an owner."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    original_event_id = "$human-duplicate:localhost"
    mention = _entity_user_id(config, "general")
    first = _router_relay_event(
        config,
        event_id="$relay-one:localhost",
        original_event_id=original_event_id,
        body=f"{mention} first",
        origin_server_ts=1_000_000,
    )
    second = _router_relay_event(
        config,
        event_id="$relay-two:localhost",
        original_event_id=original_event_id,
        body=f"{mention} duplicate",
        origin_server_ts=1_000_001,
    )
    obligation_runner = _obligation_runner(
        harness,
        tracking_path=tmp_path / "dispatch-tracking",
        matrix_user_id=_entity_user_id(config, "general"),
        entity_name="general",
        room=room,
    )
    normalization_started = asyncio.Event()
    release_normalization = asyncio.Event()
    resolve_text_event = harness.ingress.deps.normalizer.resolve_text_event

    async def resolve_with_barrier(
        _normalizer: InboundTurnNormalizer,
        request: TextNormalizationRequest,
    ) -> PreparedIngress:
        if request.event.event_id == first.event_id:
            normalization_started.set()
            await release_normalization.wait()
        return await resolve_text_event(request)

    with patch.object(InboundTurnNormalizer, "resolve_text_event", new=resolve_with_barrier):
        await admit_dispatch_event(obligation_runner, room, first, EventKind.MESSAGE, EventClass.ACTIONABLE)
        first_dispatch = asyncio.create_task(obligation_runner.drain_once())
        await normalization_started.wait()
        await admit_dispatch_event(obligation_runner, room, second, EventKind.MESSAGE, EventClass.ACTIONABLE)
        assert not first_dispatch.done()
        release_normalization.set()
        await first_dispatch
        await obligation_runner.drain_once()

    await harness.gate.drain_all()
    await harness.runner.settle_inbox_responses()
    await obligation_runner.drain_once()

    obligation_runner.start()
    try:
        async with asyncio.timeout(5):
            while await obligation_runner.store.unsettled_event_ids():  # noqa: ASYNC110 - settlement has no event signal
                await asyncio.sleep(0.01)
    finally:
        await obligation_runner.stop()

    assert not await obligation_runner.store.is_pending(first.event_id)
    assert not await obligation_runner.store.is_pending(second.event_id)


@pytest.mark.asyncio
async def test_router_alias_claim_loser_defers_and_retries_after_failed_owner(config: Config, tmp_path: Path) -> None:
    """A relay losing its alias to a live owner defers without waiting, and returns to the journal once that owner exits."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    original_event_id = "$human-retry:localhost"
    active_claim = await harness.turn_store.claim(
        TurnRecord.create(["$failed-relay:localhost"], discovery_event_ids=[original_event_id], completed=False),
    )
    assert isinstance(active_claim, TurnClaim)
    event = _router_relay_event(
        config,
        event_id="$retry-relay:localhost",
        original_event_id=original_event_id,
        body=f"{_entity_user_id(config, 'general')} retry",
        origin_server_ts=1_000_000,
    )

    assert await harness.ingress.handle_text_event(room, event) is TurnDispatchOutcome.DEFERRED
    assert harness.turn_store.has_live_turn_claim(event.event_id), "the deferred relay still counts as owned"
    assert all(slot.released for slot in harness.gate.lanes.unsettled_slots()), "the loser gave its lane slot up"
    assert harness.retried_dispatch_sources == []
    active_claim.release()
    assert harness.retried_dispatch_sources == [(event.event_id,)]
    assert not harness.turn_store.has_live_turn_claim(event.event_id)

    # The journal runs the retried relay's callback again.
    assert await harness.ingress.handle_text_event(room, event) is TurnDispatchOutcome.DEFERRED
    await harness.gate.drain_all()
    await harness.runner.settle_inbox_responses()
    assert len(harness.runner.requests) == 1


@pytest.mark.asyncio
async def test_an_alias_contender_does_not_hold_its_room_lane_while_the_turn_it_lost_to_waits(
    config: Config,
    tmp_path: Path,
) -> None:
    """A duplicate relay whose original a waiting turn owns defers without blocking its room.

    The owner stands for a relay's turn held behind an approval. The journal
    still runs a later message in the same room, such as the approval decision,
    and the duplicate stays owned until the owner ends, then returns to the
    journal and is answered.
    """
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    mention = _entity_user_id(config, "general")
    original_event_id = "$human-held:localhost"
    held = await harness.turn_store.claim(
        TurnRecord.create(["$relay-held:localhost"], discovery_event_ids=[original_event_id], completed=False),
    )
    assert isinstance(held, TurnClaim)
    contender = _router_relay_event(
        config,
        event_id="$relay-contender:localhost",
        original_event_id=original_event_id,
        body=f"{mention} answer the relayed question",
        origin_server_ts=1_000_000,
    )
    later = _text_event(f"{mention} a later message", event_id="$later:localhost", origin_server_ts=1_000_001)
    dispatcher = _obligation_runner(
        harness,
        tracking_path=tmp_path / "dispatch-tracking",
        matrix_user_id=_entity_user_id(config, "general"),
        entity_name="general",
        room=room,
    )
    harness.turn_store.deps = replace(harness.turn_store.deps, retry_dispatch_sources=dispatcher.retry_turn_sources)
    await admit_dispatch_event(dispatcher, room, contender, EventKind.MESSAGE, EventClass.ACTIONABLE)
    await admit_dispatch_event(dispatcher, room, later, EventKind.MESSAGE, EventClass.ACTIONABLE)

    async with asyncio.timeout(5):
        await dispatcher.drain_once()
        await harness.gate.drain_all()
        await harness.runner.settle_inbox_responses()

    assert [batch.event.event_id for batch in harness.gate_batches] == [later.event_id]
    assert await dispatcher.store.is_pending(contender.event_id)
    assert harness.turn_store.has_live_turn_claim(contender.event_id)

    held.release()
    await _settle_dispatcher(harness, dispatcher)

    assert [batch.event.event_id for batch in harness.gate_batches] == [later.event_id, contender.event_id]
    assert len(harness.runner.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["live_claim_outside_the_batch", "alias_answered_meanwhile"])
async def test_a_refused_gate_exit_merge_returns_its_sources_to_the_journal_at_once(
    config: Config,
    tmp_path: Path,
    refusal: str,
) -> None:
    """A batch whose turn cannot be claimed hands its sources back at once instead of leaving them deferred."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    original_event_id = "$human-merge:localhost"
    relay = _router_relay_event(
        config,
        event_id="$relay-merge:localhost",
        original_event_id=original_event_id,
        body=f"{_entity_user_id(config, 'general')} respond",
        origin_server_ts=1_000_000,
    )
    outsider = None
    if refusal == "live_claim_outside_the_batch":
        message_claim = await harness.turn_store.claim(TurnRecord.create([relay.event_id], completed=False))
        outsider = await harness.turn_store.claim(
            TurnRecord.create(["$other-relay:localhost"], discovery_event_ids=[original_event_id], completed=False),
        )
        assert isinstance(outsider, TurnClaim)
    else:
        message_claim = await harness.turn_store.claim(
            TurnRecord.create([relay.event_id], discovery_event_ids=[original_event_id], completed=False),
        )
        # Another relay of the same original is answered while this batch waits.
        await harness.turn_store.record_turn(
            TurnRecord.create(["$other-relay:localhost"], discovery_event_ids=[original_event_id]),
            room_id=_ROOM_ID,
        )
    assert isinstance(message_claim, TurnClaim)
    turn = make_test_turn(
        room,
        relay,
        _SENDER,
        handled_turn=TurnRecord.create([relay.event_id], discovery_event_ids=[original_event_id], completed=False),
        claim=message_claim,
    )

    await harness.dispatcher.handle_prepared_turn(turn)

    assert harness.retried_dispatch_sources == [(relay.event_id,)]
    assert harness.runner.requests == []
    assert not harness.turn_store.has_live_turn_claim(relay.event_id)
    if outsider is not None:
        assert harness.turn_store.has_live_turn_claim(original_event_id), "the outside owner keeps its claim"
        outsider.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("answered", [False, True])
async def test_a_relay_of_an_answered_original_is_settled_unless_it_was_answered_itself(
    config: Config,
    tmp_path: Path,
    *,
    answered: bool,
) -> None:
    """A relay whose original another relay answered is refused; only one not already answered settles as ignored."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    original_event_id = "$human-answered:localhost"
    await harness.turn_store.record_turn(
        TurnRecord.create(["$first-relay:localhost"], discovery_event_ids=[original_event_id]),
        room_id=_ROOM_ID,
    )
    if answered:
        await harness.turn_store.record_turn(TurnRecord.create(["$second-relay:localhost"]), room_id=_ROOM_ID)
    ticket = harness.ingress.reserve_prompt_ingress_order(room, _SENDER)

    try:
        outcome = await ticket.claim_turn(
            TurnRecord.create(["$second-relay:localhost"], discovery_event_ids=[original_event_id], completed=False),
        )
    finally:
        ticket.close()

    assert outcome is (TurnDispatchOutcome.DEFERRED if answered else TurnDispatchOutcome.INTENTIONALLY_IGNORED)
    assert not harness.turn_store.has_live_turn_claim("$second-relay:localhost")
    assert harness.retried_dispatch_sources == []


@pytest.mark.asyncio
async def test_a_selection_coalesced_with_another_message_is_refused(config: Config, tmp_path: Path) -> None:
    """An interactive selection only ever dispatches alone, as the barrier the gate makes of it."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    target = MessageTarget.resolve(room_id=room.room_id, thread_id=None, reply_to_event_id="$question:localhost")
    selection = PendingEvent(
        event=PreparedIngress(sender=_SENDER, event_id="$selection:localhost", body="", source={}),
        room=room,
        facts=_SELECTION_FACTS,
        selection=InteractiveSelectionDispatch(
            selection=interactive.InteractiveSelection(
                question_event_id="$question:localhost",
                question_text="Proceed?",
                selection_key="1",
                selected_label="Yes",
                selected_value="yes",
                thread_id=None,
            ),
            response_target=target,
        ),
    )
    message = PendingEvent(
        event=PreparedIngress(sender=_SENDER, event_id="$message:localhost", body="hello", source={}),
        room=room,
        facts=make_ingress_facts(_SENDER),
    )
    turn = build_prepared_turn(
        requester_coalescing_key(room.room_id, None, _SENDER),
        [message, selection],
        queues_other_run=nothing_else_queued,
    )

    with pytest.raises(ValueError, match="single FIFO barrier"):
        await harness.dispatcher.handle_prepared_turn(turn)

    assert harness.runner.inbox_tasks == []


@pytest.mark.asyncio
async def test_command_dispatch_keeps_the_alias_its_ingress_claimed(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command dispatched past the gate keeps every event its ingress claimed, its discovery alias included."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    alias = "$placeholder:localhost"
    event = _text_event("!help", event_id="$command-alias:localhost")
    prepared = PreparedIngress(sender=_SENDER, event_id=event.event_id, body="!help", source=event.source)
    ticket = harness.ingress.reserve_prompt_ingress_order(room, _SENDER)
    assert (
        await ticket.claim_turn(TurnRecord.create([event.event_id], discovery_event_ids=[alias], completed=False))
        is None
    )
    alias_claimed_during_command: list[bool] = []

    async def execute_if_owned(**_kwargs: object) -> bool:
        alias_claimed_during_command.append(harness.turn_store.has_live_turn_claim(alias))
        return True

    monkeypatch.setattr(harness.dispatcher.deps.command_executor, "execute_if_owned", execute_if_owned)
    try:
        await harness.ingress._dispatch_command_control_input(
            room=room,
            dispatch_event=prepared,
            facts=harness.ingress.deps.ingress.classify(prepared, room_id=room.room_id),
            coalescing_thread_id=None,
            ticket=ticket,
        )
    finally:
        ticket.close()

    assert alias_claimed_during_command == [True]
    assert not harness.turn_store.has_live_turn_claim(alias)
    assert not harness.turn_store.has_live_turn_claim(event.event_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("handled_meanwhile", [False, True])
async def test_command_that_cannot_keep_its_claim_ends_as_its_refused_claim(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handled_meanwhile: bool,
) -> None:
    """A command whose claim cannot merge ends its callback as a refused claim does, with no retry beside it.

    A retry would only reach the same refusal, and a retried source the callback also defers has no live owner, so
    the journal would reclaim it as a lost handoff.
    """
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _text_event("!help", event_id="$refused-command:localhost")

    async def refuse(claims: tuple[TurnClaim, ...], _turn_record: TurnRecord) -> None:
        for claim in claims:
            claim.release()
        if handled_meanwhile:
            await harness.turn_store.record_turn(TurnRecord.create([event.event_id]), room_id=room.room_id)

    execute_if_owned = AsyncMock(return_value=True)
    monkeypatch.setattr(harness.turn_store, "merge_claims", refuse)
    monkeypatch.setattr(harness.dispatcher.deps.command_executor, "execute_if_owned", execute_if_owned)

    outcome = await harness.ingress.handle_text_event(room, event)

    assert outcome is (TurnDispatchOutcome.DEFERRED if handled_meanwhile else TurnDispatchOutcome.INTENTIONALLY_IGNORED)
    assert harness.retried_dispatch_sources == []
    execute_if_owned.assert_not_awaited()
    assert not harness.turn_store.has_live_turn_claim(event.event_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["edit", "command_target_not_ready"])
async def test_a_late_reply_denial_at_ingress_settles_its_event_inside_the_fence(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
) -> None:
    """An edit, or a command whose conversation is unresolved, refused by a later authorization settles under the fence."""
    harness = _build_harness(config, tmp_path, agent_name="general" if site == "edit" else ROUTER_AGENT_NAME)
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    gate = harness.runtime.response_admission_gate
    if site == "edit":
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* edited",
                    "msgtype": "m.text",
                    "m.new_content": {"body": "edited", "msgtype": "m.text"},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$original:localhost"},
                },
                "event_id": "$edit:localhost",
                "sender": _SENDER,
                "origin_server_ts": 1_000_001,
                "room_id": _ROOM_ID,
                "type": "m.room.message",
            },
        )
    else:
        event = _text_event("!help", event_id="$unplaced-command:localhost")

        async def unresolved(*_args: object) -> str | None:
            raise ThreadMembershipLookupError

        monkeypatch.setattr(harness.ingress.deps.resolver, "coalescing_thread_id", unresolved)
    # The first gate admits the sender; access is revoked before the decision that would act on it.
    decisions = iter([True])
    monkeypatch.setattr(
        harness.policy,
        "can_reply_to_sender_in_room",
        lambda *_args, **_kwargs: next(decisions, False),
    )
    settled: list[tuple[tuple[str, ...], int]] = []

    async def settle(event_ids: tuple[str, ...]) -> None:
        settled.append((event_ids, gate.in_flight_response_count))

    ingress = harness.ingress.deps.ingress
    _replace_harness_deps(harness, ingress=IngressValidator(replace(ingress.deps, settle_ignored_sources=settle)))

    outcome = await harness.ingress.handle_text_event(room, event)

    assert outcome is TurnDispatchOutcome.INTENTIONALLY_IGNORED
    assert settled == [((event.event_id,), 1)]
    assert harness.gateway.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("denied_at", ["message_received", "plan"])
async def test_a_turn_whose_requester_lost_access_in_the_queue_settles_its_sources_inside_the_fence(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    denied_at: str,
) -> None:
    """A queued turn refused at gate exit, before its hooks or before its plan, settles every source it held."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    gate = harness.runtime.response_admission_gate
    event = _text_event("answer me", event_id="$queued:localhost")
    claim = await harness.turn_store.claim(TurnRecord.create([event.event_id], completed=False))
    assert isinstance(claim, TurnClaim)
    # The message:received hook decides first, then the plan.
    decisions = iter([] if denied_at == "message_received" else [True])
    monkeypatch.setattr(
        harness.policy,
        "can_reply_to_sender_in_room",
        lambda *_args, **_kwargs: next(decisions, False),
    )
    settled: list[tuple[tuple[str, ...], int]] = []

    async def settle(event_ids: tuple[str, ...]) -> None:
        settled.append((event_ids, gate.in_flight_response_count))

    ingress = harness.dispatcher.deps.ingress
    _replace_harness_deps(harness, ingress=IngressValidator(replace(ingress.deps, settle_ignored_sources=settle)))

    await harness.dispatcher.handle_prepared_turn(make_test_turn(room, event, _SENDER, claim=claim))

    assert settled == [((event.event_id,), 1)]
    assert harness.policy.plan_turn_calls == 0
    assert harness.runner.requests == []
    assert not harness.turn_store.has_live_turn_claim(event.event_id)


def _approval_reply(event_id: str = "$approval-reply:localhost") -> nio.RoomMessageText:
    """Return a person's reply to an approval card."""
    return nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "no, use the other file",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$approval-card:localhost"}},
            },
            "event_id": event_id,
            "sender": _SENDER,
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("already_claimed", [False, True])
async def test_an_approval_reply_claims_its_consumer_before_its_effect_and_answers_no_turn(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    already_claimed: bool,
) -> None:
    """A reply an approval card consumes claims that consumer durably first, and its card is its answer.

    A replay whose consumer is already claimed runs the approval path without claiming again, and never becomes a turn,
    even when the approval it answered is gone.
    """
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _approval_reply()
    steps: list[str] = []

    async def claim(consumer: SemanticConsumer) -> bool:
        steps.append(f"claim:{consumer.value}")
        return True

    async def maybe_approval(*, before_consume: Callable[[], Awaitable[None]] | None, **_kwargs: object) -> bool:
        if already_claimed:
            assert before_consume is None
            return False
        assert before_consume is not None
        await before_consume()
        steps.append("deny_approval")
        return True

    _replace_harness_deps(
        harness,
        semantic_consumer=lambda: SemanticConsumer.APPROVAL_REPLY if already_claimed else None,
        claim_semantic_consumer=claim,
    )
    monkeypatch.setattr("mindroom.turns.ingress.maybe_handle_tool_approval_reply", maybe_approval)

    outcome = await harness.ingress.handle_text_event(room, event)
    await harness.gate.drain_all()

    assert outcome is TurnDispatchOutcome.INTENTIONALLY_IGNORED
    assert steps == ([] if already_claimed else [f"claim:{SemanticConsumer.APPROVAL_REPLY.value}", "deny_approval"])
    assert harness.gate_batches == []
    assert harness.runner.requests == []
    # Its card answers it, so a message that waited for the approval is not skipped as older than it.
    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("active_card", [False, True])
async def test_a_reply_that_is_no_approval_answer_is_classified_once_and_dispatched(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active_card: bool,
) -> None:
    """A text event's provenance is classified once for onboarding, approval, and its turn.

    A reply to an approval card this process holds keeps the lane slot it took before the approval lookup.
    """
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _approval_reply()
    classified: list[str] = []
    onboarding_requesters: list[str] = []
    reservations: list[str] = []
    classify_event = IngressValidator.classify

    def classify(validator: IngressValidator, inbound: nio.RoomMessageText, **kwargs: str | None) -> IngressFacts:
        classified.append(inbound.event_id)
        return classify_event(validator, inbound, **kwargs)

    async def onboarding(_room: nio.MatrixRoom, _event: nio.RoomMessageFormatted, requester_user_id: str) -> bool:
        onboarding_requesters.append(requester_user_id)
        return False

    reserve = harness.ingress.reserve_prompt_ingress_order

    def record_reservation(room: nio.MatrixRoom, requester_user_id: str, **kwargs: object) -> IngressTicket:
        reservations.append(requester_user_id)
        return reserve(room, requester_user_id, **kwargs)

    monkeypatch.setattr(IngressValidator, "classify", classify)
    _replace_harness_deps(harness, personal_room_command=onboarding)
    monkeypatch.setattr(harness.ingress, "reserve_prompt_ingress_order", record_reservation)
    monkeypatch.setattr("mindroom.turns.ingress.is_process_active_approval_card", lambda _event_id: active_card)
    monkeypatch.setattr("mindroom.turns.ingress.maybe_handle_tool_approval_reply", AsyncMock(return_value=False))

    await harness.deliver(room, event)

    assert classified == [event.event_id]
    assert onboarding_requesters == [_SENDER]
    assert reservations == [_SENDER]
    assert [batch.handled_turn.source_event_ids for batch in harness.gate_batches] == [(event.event_id,)]


@pytest.mark.asyncio
async def test_a_personal_room_command_is_handled_before_any_approval_lookup(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An onboarding command ends its callback before the approval path or a turn sees it."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _approval_reply()
    approval = AsyncMock(return_value=True)

    async def onboarding(*_args: object) -> bool:
        return True

    _replace_harness_deps(harness, personal_room_command=onboarding)
    monkeypatch.setattr("mindroom.turns.ingress.maybe_handle_tool_approval_reply", approval)

    outcome = await harness.ingress.handle_text_event(room, event)

    assert outcome is TurnDispatchOutcome.INTENTIONALLY_IGNORED
    approval.assert_not_awaited()
    assert not await harness.turn_store.is_handled(_ROOM_ID, event.event_id)


@pytest.mark.asyncio
async def test_contended_claim_does_not_strand_the_winner_in_the_senders_lane(
    config: Config,
    tmp_path: Path,
) -> None:
    """A losing duplicate callback must not hold the lane its winner's flush waits on.

    Duplicate delivery runs two callbacks for one event concurrently. The loser
    waits for the winner's turn to settle; the winner's coalesced batch cannot
    flush until every undelivered slot in that sender's lane settles. A loser
    that keeps its slot across the wait closes the cycle: the batch is admitted
    to the gate and never dispatched, with no error logged anywhere.
    """
    # A media event waits out the debounce in the gate, holding its claim, until the drain flushes it, which builds
    # the cycle: ordinary text skips the wait and would be dispatched before the duplicate arrived.
    harness = _build_harness(config, tmp_path, debounce_seconds=60.0)
    room = _room_with_members(config, "general")
    event = _image_event(event_id="$duplicate-delivery:localhost")

    async with asyncio.timeout(10):
        outcomes = [
            await harness.ingress.handle_media_event(room, event),
            await harness.ingress.handle_media_event(room, event),
        ]
        await harness.gate.drain_all()
        await harness.runner.settle_inbox_responses()

    assert outcomes == [TurnDispatchOutcome.DEFERRED, TurnDispatchOutcome.DEFERRED]
    # Two admitted copies would coalesce into one batch, so the batch must hold one media event.
    assert [len(batch.media_events) for batch in harness.gate_batches] == [1]
    assert len(harness.runner.requests) == 1
    assert harness.gate.lanes.all_settled()
    # The duplicate goes back to the journal once the winner's claim is released.
    assert harness.retried_dispatch_sources == [(event.event_id,)]


@pytest.mark.asyncio
async def test_router_relay_ignored_by_this_agent_compacts_exact_callback(
    config: Config,
    tmp_path: Path,
) -> None:
    """A relay routed elsewhere settles locally without indexing a fake response turn."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", "research", ROUTER_AGENT_NAME)
    relay = _router_relay_event(
        config,
        event_id="$relay-research:localhost",
        original_event_id="$human-research:localhost",
        body=f"{_entity_user_id(config, 'research')} investigate",
        origin_server_ts=1_000_000,
    )

    await harness.deliver(room, relay)

    assert harness.runner.requests == []
    assert await harness.turn_store.get_turn_record("$relay-research:localhost") is None
    assert await harness.turn_store.get_turn_record("$human-research:localhost") is None
    assert harness.ignored_dispatch_sources == [("$relay-research:localhost",)]


@pytest.mark.asyncio
async def test_failed_gate_admission_releases_ingress_claim_once(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejected gate admission closes the claim it moved into the pending event."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _text_event("admission closes")
    claims: list[TurnClaim] = []
    real_claim = harness.turn_store.claim

    async def record_claim(record: TurnRecord) -> TurnClaim | ClaimRefused | ClaimContended:
        result = await real_claim(record)
        if isinstance(result, TurnClaim):
            claims.append(result)
        return result

    monkeypatch.setattr(harness.turn_store, "claim", record_claim)
    monkeypatch.setattr(
        harness.gate,
        "submit_lane_slot",
        MagicMock(side_effect=IngressAdmissionClosedError),
    )

    obligation_runner = _obligation_runner(
        harness,
        tracking_path=tmp_path / "dispatch-tracking",
        matrix_user_id=_entity_user_id(config, "general"),
        entity_name="general",
        room=room,
    )
    await admit_dispatch_event(obligation_runner, room, event, EventKind.MESSAGE, EventClass.ACTIONABLE)

    with pytest.raises(IngressAdmissionClosedError):
        await obligation_runner.callbacks.on_message(room, event)

    assert len(claims) == 1
    assert await obligation_runner.store.is_pending(event.event_id)
    assert not harness.turn_store.has_live_turn_claim(event.event_id)
    competing_claim = await real_claim(TurnRecord.create([event.event_id], completed=False))
    assert isinstance(competing_claim, TurnClaim)
    competing_claim.release()


@pytest.mark.asyncio
async def test_failed_media_admission_remains_a_pending_exact_callback(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed media lane must raise through the durable boundary for recovery."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _image_event(event_id="$media-admission-closed:localhost")
    monkeypatch.setattr(
        harness.gate,
        "submit_lane_slot",
        MagicMock(side_effect=IngressAdmissionClosedError),
    )
    obligation_runner = _obligation_runner(
        harness,
        tracking_path=tmp_path / "dispatch-tracking",
        matrix_user_id=_entity_user_id(config, "general"),
        entity_name="general",
        room=room,
    )
    await admit_dispatch_event(obligation_runner, room, event, EventKind.MEDIA, EventClass.ACTIONABLE)

    with pytest.raises(IngressAdmissionClosedError):
        await obligation_runner.callbacks.on_message(room, event)

    assert await obligation_runner.store.is_pending(event.event_id)


@pytest.mark.asyncio
async def test_router_silent_ignore_compacts_exact_callback(config: Config, tmp_path: Path) -> None:
    """A router ignore without a visible voice echo explicitly settles its source."""
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    room = _room_with_members(config, ROUTER_AGENT_NAME, "general")
    event = _text_event("hello there", event_id="$router-ignore:localhost")

    obligation_runner = _obligation_runner(
        harness,
        tracking_path=tmp_path / "dispatch-tracking",
        matrix_user_id=_entity_user_id(config, ROUTER_AGENT_NAME),
        entity_name=ROUTER_AGENT_NAME,
        room=room,
    )
    _replace_harness_deps(
        harness,
        visible_responses=VisibleResponseReconciler(
            replace(
                harness.dispatcher.deps.visible_responses.deps,
                settle_ignored_sources=obligation_runner.settle_intentionally_ignored_turn_sources,
            ),
        ),
    )

    await admit_dispatch_event(obligation_runner, room, event, EventKind.MESSAGE, EventClass.ACTIONABLE)
    await obligation_runner.drain_once()
    await harness.gate.drain_all()

    assert harness.policy.plan_turn_calls == 1
    assert harness.runner.requests == []
    assert await harness.turn_store.get_turn_record(event.event_id) is None
    assert not await obligation_runner.store.is_pending(event.event_id)


@pytest.mark.asyncio
async def test_unmentioned_managed_attachment_settles_before_unavailable_thread_history(
    config: Config,
    tmp_path: Path,
) -> None:
    """Non-actionable managed chatter must not become a poison retry during hydration."""
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    room = _room_with_members(config, ROUTER_AGENT_NAME, "general")
    event = _managed_file_event(
        config,
        event_id="$managed-attachment:localhost",
        thread_id="$unavailable-root:localhost",
    )
    reader = harness.ingress.deps.resolver.deps.conversation_reader
    history_error = RuntimeError("RoomEventRelationsError: M_NOT_FOUND: Event not found in room")
    reader.read.side_effect = history_error
    reader.read_strict.side_effect = history_error
    received_event_ids: list[str] = []

    @hook(EVENT_MESSAGE_RECEIVED)
    async def received(context: MessageReceivedContext) -> None:
        received_event_ids.append(context.envelope.source_event_id)

    plugin = SimpleNamespace(
        name="managed-ingress-observer",
        discovered_hooks=(received,),
        discovered_automations=(),
        entry_config=PluginEntryConfig(path="./plugins/managed-ingress-observer"),
        plugin_order=0,
    )
    hook_context = harness.dispatcher.deps.ingress_hook_runner.hook_context
    hook_context.hook_registry_state.registry = HookRegistry.from_plugins([cast("Any", plugin)])

    outcome = await harness.ingress.handle_media_event(room, event)
    await harness.gate.drain_all()

    assert outcome is TurnDispatchOutcome.DEFERRED
    assert harness.ignored_dispatch_sources == [(event.event_id,)]
    assert harness.retried_dispatch_sources == []
    assert received_event_ids == [event.event_id]
    reader.read.assert_not_awaited()
    reader.read_strict.assert_not_awaited()
    assert harness.policy.plan_turn_calls == 0
    assert harness.runner.requests == []


@pytest.mark.asyncio
async def test_managed_follow_up_cannot_settle_human_source_queued_before_it(
    config: Config,
    tmp_path: Path,
) -> None:
    """Mixed-requester backlogs dispatch per requester, so managed chatter settles only itself."""
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    room = _room_with_members(config, ROUTER_AGENT_NAME, "general")
    human_event = _text_event(
        "please inspect the attachment",
        event_id="$human-follow-up:localhost",
        thread_id="$unavailable-root:localhost",
        origin_server_ts=999,
    )
    managed_event = _managed_file_event(
        config,
        event_id="$managed-follow-up:localhost",
        thread_id="$unavailable-root:localhost",
    )
    pending_events = (
        make_pending_event(
            human_event,
            room,
            source_kind=MESSAGE_SOURCE_KIND,
            requester_user_id=_SENDER,
            active_follow_up=True,
        ),
        make_pending_event(
            managed_event,
            room,
            source_kind=MEDIA_SOURCE_KIND,
            active_follow_up=True,
            facts=harness.ingress.deps.ingress.classify(
                managed_event,
                source_kind=MEDIA_SOURCE_KIND,
                room_id=room.room_id,
            ),
        ),
    )
    reader = harness.ingress.deps.resolver.deps.conversation_reader
    history_error = RuntimeError("RoomEventRelationsError: M_NOT_FOUND: Event not found in room")
    reader.read.side_effect = history_error
    reader.read_strict.side_effect = history_error
    key = active_follow_up_coalescing_key(room.room_id, "$unavailable-root:localhost")
    retried_batches: list[tuple[str, ...]] = []

    def record_failed_batch(_room_id: str, source_event_ids: tuple[str, ...]) -> None:
        retried_batches.append(source_event_ids)

    gate = CoalescingGate(
        dispatch_turn=harness.dispatcher.handle_prepared_turn,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
        on_dispatch_failure=record_failed_batch,
    )

    for pending_event in pending_events:
        await gate.admit(
            key,
            ready_result=ReadyPendingEvent(pending_event=pending_event),
            source_event_id=pending_event.event.event_id,
        )
    await gate.drain_all()

    assert harness.ignored_dispatch_sources == [(managed_event.event_id,)]
    assert retried_batches == [(human_event.event_id,)]
    assert reader.read.await_count + reader.read_strict.await_count > 0
    assert harness.policy.plan_turn_calls == 0
    assert harness.runner.requests == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_sender_outside_reply_allowlist_is_dropped_at_precheck(tmp_path: Path) -> None:
    """An unauthorized sender is filtered at precheck and the turn gets a terminal record."""
    config = bind_runtime_paths(
        with_responder_access(
            Config(agents={"general": AgentConfig(display_name="General")}),
            "general",
            users=["@owner:localhost"],
        ),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _text_event("hello from a stranger")

    await harness.deliver(room, event)

    assert harness.policy.plan_turn_calls == 0
    assert harness.runner.requests == []
    assert harness.gateway.sent == []
    # The drop is terminal: the turn is recorded so restarts never resurrect it.
    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id) is True


@pytest.mark.asyncio
async def test_policy_ignore_sends_nothing_and_compacts_callback(config: Config, tmp_path: Path) -> None:
    """An ignore plan compacts exact callback truth without recording a turn."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", "research")
    event = _text_event("untagged message with multiple visible responders")

    await harness.deliver(room, event)

    assert harness.policy.plan_turn_calls == 1
    assert harness.runner.requests == []
    assert harness.gateway.sent == []
    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id) is False
    assert harness.ignored_dispatch_sources == [(event.event_id,)]


@pytest.mark.asyncio
async def test_policy_respond_crosses_seam_as_immutable_values(config: Config, tmp_path: Path) -> None:
    """A respond plan invokes the runner once with value-only ResponseRequest inputs.

    Pins the one-way ingress→execution seam: the request carries an immutable
    ``ResponsePayloadPreparation`` value (no callbacks back into turn dispatch),
    and the recorded outcome lands in the durable turn store.
    """
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _text_event("please summarize the build failure")

    await harness.deliver(room, event)

    # Conversational turns flow through the coalescing gate, unlike commands.
    assert len(harness.gate_batches) == 1
    assert harness.policy.plan_turn_calls == 1
    assert len(harness.runner.requests) == 1
    request = harness.runner.requests[0]

    assert request.prompt == "please summarize the build failure"
    assert request.user_id == _SENDER
    assert request.correlation_id == event.event_id
    assert request.current_timestamp_ms == float(event.server_timestamp)
    assert request.current_prompt_is_structured is False
    # Display names ride along as an ingress snapshot of the synced room cache.
    general_user_id = _entity_user_id(config, "general")
    assert request.member_display_names == {_SENDER: _SENDER, general_user_id: general_user_id}
    assert request.response_envelope.requester_id == _SENDER
    assert request.response_envelope.target.room_id == _ROOM_ID
    assert request.sources == ResponseSources(
        pending_event_ids=(event.event_id,),
        logical_source_event_ids=(event.event_id,),
    )
    # A rootable room-level message becomes its own thread root.
    assert request.response_envelope.target.resolved_thread_id == event.event_id

    preparation = request.payload_preparation
    assert isinstance(preparation, ResponsePayloadPreparation)
    assert preparation.prompt == "please summarize the build failure"
    assert preparation.action_kind == "individual"
    assert preparation.target_member_names is None
    assert preparation.payload_inputs == DispatchPayloadInputs(
        message_attachment_ids=(),
        trusted_attachment_ids=(),
        media_events=(),
    )
    # The envelope crosses the seam as the same value the dispatch was planned with.
    assert request.response_envelope is preparation.dispatch.envelope

    # Payload data crosses the seam as values, not closures: the request has no
    # prepare-after-lock callback and nothing callable rides inside the
    # preparation value. The request does carry the inbox handoff signal, which
    # the runner fires when it takes the lifecycle lock.
    request_field_names = {request_field.name for request_field in fields(ResponseRequest)}
    assert "prepare_after_lock" not in request_field_names
    assert request.on_lifecycle_lock_acquired is not None
    assert not any(callable(getattr(preparation, preparation_field.name)) for preparation_field in fields(preparation))

    metadata = request.matrix_run_metadata
    assert metadata is not None
    assert metadata[constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY] == [event.event_id]
    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("held", "mention"),
    [(False, None), (True, None), (True, "general"), (True, "research"), (True, "person")],
)
async def test_a_message_waiting_for_a_pending_approval_gets_an_hourglass(
    config: Config,
    tmp_path: Path,
    held: bool,
    mention: str | None,
) -> None:
    """A human message to a conversation an approval holds is marked as waiting, unless it is for another agent.

    The queue records whether it is for another participant, so it never shares a turn with messages for this agent.
    """
    harness = _build_harness(config, tmp_path)
    harness.runner.held_for_approval = held
    room = _room_with_members(config, "general", "research")
    event = _text_event("and what about tomorrow?")
    if mention is not None:
        mentioned = "@someone:localhost" if mention == "person" else _entity_user_id(config, mention)
        event.source["content"]["m.mentions"] = {"user_ids": [mentioned]}

    await harness.ingress.handle_text_event(room, event)
    await wait_for_background_tasks(timeout=5.0, owner=harness.ingress.deps.runtime)

    waits = held and mention in {None, "general"}
    assert harness.gateway.reactions == ([(event.event_id, "⏳", "approval_wait")] if waits else [])
    await harness.gate.drain_all()
    assert [batch.event.for_another_participant for batch in harness.gate_batches] == [
        {None: None, "general": False}.get(mention, True),
    ]


@pytest.mark.asyncio
async def test_bridge_alias_crosses_execution_seam_as_one_canonical_requester(tmp_path: Path) -> None:
    """Requester-owned execution must not split one human across configured bridge identities."""
    canonical_sender = "@owner:localhost"
    bridge_sender = "@bridge_owner:localhost"
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General",
                    access=ResponderAccessConfig(users=[canonical_sender]),
                ),
            },
            authorization={"aliases": {canonical_sender: [bridge_sender]}},
        ),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    room.add_member(bridge_sender, bridge_sender, None)
    event = _text_event("use my private state", sender=bridge_sender)

    await harness.deliver(room, event)

    assert len(harness.runner.requests) == 1
    request = harness.runner.requests[0]
    assert request.user_id == canonical_sender
    assert request.response_envelope.requester_id == canonical_sender
    assert request.response_envelope.origin.transport_sender_id == bridge_sender


@pytest.mark.asyncio
async def test_trusted_router_relay_preserves_transport_sender_while_canonicalizing_requester(tmp_path: Path) -> None:
    """A trusted relay keeps its bot provenance while the human owns execution."""
    canonical_sender = "@owner:localhost"
    bridge_sender = "@bridge_owner:localhost"
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General",
                    access=ResponderAccessConfig(users=[canonical_sender]),
                ),
            },
            authorization={"aliases": {canonical_sender: [bridge_sender]}},
        ),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    router_sender = _entity_user_id(config, ROUTER_AGENT_NAME)
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "@general use my private state",
                "msgtype": "m.text",
                constants.ORIGINAL_SENDER_KEY: bridge_sender,
                constants.SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
            },
            "event_id": "$router-relay:localhost",
            "sender": router_sender,
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )

    await harness.deliver(room, event)

    assert len(harness.runner.requests) == 1
    request = harness.runner.requests[0]
    assert request.user_id == canonical_sender
    assert request.response_envelope.requester_id == canonical_sender
    assert request.response_envelope.origin.transport_sender_id == router_sender


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize("admitted", [False, True])
async def test_router_handoff_runs_as_the_bot_account_it_routes(tmp_path: Path, *, admitted: bool) -> None:
    """A routed bot-account message runs as that bot account, so access applies to it rather than to the router."""
    bot_account = "@telegram:localhost"
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General",
                    access=ResponderAccessConfig(users=[bot_account if admitted else "@owner:localhost"]),
                ),
            },
            bot_accounts=[bot_account],
        ),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    router_sender = _entity_user_id(config, ROUTER_AGENT_NAME)
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "@general could you help with this?",
                "msgtype": "m.text",
                "m.mentions": {"user_ids": [_entity_user_id(config, "general")]},
                constants.ORIGINAL_SENDER_KEY: bot_account,
                constants.SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
            },
            "event_id": "$router-bot-relay:localhost",
            "sender": router_sender,
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )

    await harness.deliver(room, event)

    assert [request.user_id for request in harness.runner.requests] == ([bot_account] if admitted else [])
    assert [request.response_envelope.requester_id for request in harness.runner.requests] == (
        [bot_account] if admitted else []
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_router_voice_echo_for_a_bot_account_is_display_only(tmp_path: Path) -> None:
    """The router's visible transcript of bot-account audio starts no turn, just like a human's."""
    bot_account = "@telegram:localhost"
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General", access=ResponderAccessConfig(users=[bot_account]))},
            bot_accounts=[bot_account],
        ),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "🎤 @general could you help with this?",
                "msgtype": "m.text",
                "m.mentions": {"user_ids": [_entity_user_id(config, "general")]},
                constants.ORIGINAL_SENDER_KEY: bot_account,
                constants.SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                constants.VISIBLE_ROUTER_VOICE_ECHO_KEY: True,
            },
            "event_id": "$router-bot-voice-echo:localhost",
            "sender": _entity_user_id(config, ROUTER_AGENT_NAME),
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )

    await harness.deliver(room, event)

    assert harness.runner.requests == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize("sender_name", [ROUTER_AGENT_NAME, "research"])
@pytest.mark.parametrize("skip_mentions", [True, False])
async def test_moved_thread_copy_that_names_an_agent_starts_no_turn(
    config: Config,
    tmp_path: Path,
    sender_name: str,
    skip_mentions: bool,
) -> None:
    """A copied message that names an agent wakes it unless the copy carries the mention guard."""
    with_responder_access(config, "general", users=[_entity_user_id(config, sender_name)])
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", "research", ROUTER_AGENT_NAME)
    event = _text_event(
        "@general could you help with this?",
        event_id="$moved-copy:localhost",
        thread_id=_THREAD_ROOT,
        sender=_entity_user_id(config, sender_name),
    )
    content = event.source["content"]
    content["m.mentions"] = {}
    if skip_mentions:
        content[constants.SKIP_MENTIONS_KEY] = True
    if sender_name == ROUTER_AGENT_NAME:
        content[constants.ORIGINAL_SENDER_KEY] = _SENDER

    await harness.deliver(room, event)

    # Without the guard the agent wakes, and the relay sender, never the original human, is the requester.
    expected_requesters = [] if skip_mentions else [_entity_user_id(config, sender_name)]
    assert [request.user_id for request in harness.runner.requests] == expected_requesters


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_a_file_that_ignores_mentions_keeps_the_mention_in_text_batched_with_it(
    config: Config,
    tmp_path: Path,
) -> None:
    """An agent's file and its recipient text share one batch, and the file's flag must not erase the text's mention."""
    with_responder_access(config, "general", users=[_entity_user_id(config, "research")])
    harness = _build_harness(config, tmp_path, debounce_seconds=1.0)
    room = _room_with_members(config, "general", "research")
    file_event = _managed_file_event(
        config,
        event_id="$research-file:localhost",
        thread_id=_THREAD_ROOT,
        sender_name="research",
    )
    file_event.source["content"][constants.SKIP_MENTIONS_KEY] = True
    text_event = _text_event(
        "@general please review the attached report",
        event_id="$research-text:localhost",
        thread_id=_THREAD_ROOT,
        origin_server_ts=1_001,
        sender=_entity_user_id(config, "research"),
    )
    text_event.source["content"]["m.mentions"] = {"user_ids": [_entity_user_id(config, "general")]}

    await harness.ingress.handle_media_event(room, file_event)
    await harness.deliver(room, text_event)

    assert [turn.handled_turn.source_event_ids for turn in harness.gate_batches] == [
        (file_event.event_id, text_event.event_id),
    ]
    assert len(harness.runner.requests) == 1


_OWNER = "@owner:localhost"


def _owner_only_general_config(tmp_path: Path) -> Config:
    """Return general reachable only by the owner, and research reachable by anyone in the room."""
    return bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General", access=ResponderAccessConfig(users=[_OWNER])),
                "research": AgentConfig(display_name="Research"),
            },
        ),
        test_runtime_paths(tmp_path / "runtime"),
    )


def _entity_reply_event(
    config: Config,
    *,
    sender: str,
    acting_requester: str,
    mention: str | None = "general",
    event_id: str = "$agent-reply:localhost",
) -> nio.RoomMessageText:
    """Return one reply that research (or ``sender``) wrote for a human requester."""
    content: dict[str, Any] = {
        "body": "@general please take this over" if mention else "Here is my answer",
        "msgtype": "m.text",
        constants.ACTING_REQUESTER_KEY: acting_requester,
    }
    if mention is not None:
        content["m.mentions"] = {"user_ids": [_entity_user_id(config, mention)]}
    return nio.RoomMessageText.from_dict(
        {
            "content": content,
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )


def _joined_room(config: Config, *entity_names: str, requester: str = _OWNER) -> nio.MatrixRoom:
    """Return a room with complete membership in which the requester an agent reply acts for is joined."""
    room = _room_with_members(config, *entity_names)
    room.add_member(requester, requester, None)
    room.members_synced = True
    return room


def _final_reply_edit(
    config: Config,
    *,
    placeholder_id: str,
    event_id: str,
    status: str = constants.STREAM_STATUS_COMPLETED,
    body: str = "@general please take this over",
) -> nio.RoomMessageText:
    """Return research's edit that delivers its reply for the owner into its placeholder, as delivery builds it."""
    content = build_edit_event_content(
        event_id=placeholder_id,
        new_content={
            "body": body,
            "msgtype": "m.text",
            "m.mentions": {"user_ids": [_entity_user_id(config, "general")]},
        },
        new_text=body,
        extra_content={constants.ACTING_REQUESTER_KEY: _OWNER, constants.STREAM_STATUS_KEY: status},
    )
    return _final_reply_edit_event(config, content, event_id=event_id)


def _final_reply_edit_event(config: Config, content: dict[str, Any], *, event_id: str) -> nio.RoomMessageText:
    """Return research's edit event carrying this content."""
    return nio.RoomMessageText.from_dict(
        {
            "content": content,
            "event_id": event_id,
            "sender": _entity_user_id(config, "research"),
            "origin_server_ts": 1_000_001,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )


def _admit_placeholder(harness: _Harness, placeholder_id: str, thread_id: str | None = _THREAD_ROOT) -> None:
    """Record the placeholder as admitted into this thread, or at room level."""
    resolver = harness.ingress.deps.resolver
    resolver.deps = replace(resolver.deps, relations=make_relation_lookup(threads={placeholder_id: thread_id}))


class _NoEditRegeneration:
    """Edit regenerator that records the edits it receives and regenerates nothing."""

    def __init__(self) -> None:
        self.edit_event_ids: list[str] = []
        self.edit_facts: list[IngressFacts] = []

    async def handle_message_edit(
        self,
        room: nio.MatrixRoom,  # noqa: ARG002
        event: nio.RoomMessageText,
        event_info: EventInfo,  # noqa: ARG002
        facts: IngressFacts,
    ) -> None:
        self.edit_event_ids.append(event.event_id)
        self.edit_facts.append(facts)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_mentioned_agent_acts_for_the_human_an_agent_reply_was_written_for(tmp_path: Path) -> None:
    """The mentioned agent authorizes and runs as the human, while the replying agent stays the author."""
    config = _owner_only_general_config(tmp_path)
    harness = _build_harness(config, tmp_path)
    room = _joined_room(config, "general", "research")
    research = _entity_user_id(config, "research")

    await harness.deliver(room, _entity_reply_event(config, sender=research, acting_requester=_OWNER))

    assert len(harness.runner.requests) == 1
    request = harness.runner.requests[0]
    assert request.user_id == _OWNER
    assert request.response_envelope.requester_id == _OWNER
    assert request.response_envelope.origin.transport_sender_id == research
    assert request.response_envelope.origin.acting_sender_id == research


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_mentioned_agent_ignores_an_agent_reply_for_a_human_its_access_excludes(tmp_path: Path) -> None:
    """A human the mentioned agent would refuse directly cannot reach it through another agent's reply."""
    config = _owner_only_general_config(tmp_path)
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", "research")
    event = _entity_reply_event(config, sender=_entity_user_id(config, "research"), acting_requester=_SENDER)

    await harness.deliver(room, event)

    assert harness.policy.plan_turn_calls == 0
    assert harness.runner.requests == []
    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id) is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize("admitted", [False, True])
async def test_mentioned_agent_acts_for_the_bot_account_an_agent_reply_was_written_for(
    tmp_path: Path,
    *,
    admitted: bool,
) -> None:
    """A configured bot account is held to the mentioned agent's access like a human, not run as the replying agent."""
    bot_account = "@telegram:localhost"
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General",
                    access=ResponderAccessConfig(users=[bot_account if admitted else _OWNER]),
                ),
                "research": AgentConfig(display_name="Research"),
            },
            bot_accounts=[bot_account],
        ),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path)
    room = _joined_room(config, "general", "research", requester=bot_account)
    event = _entity_reply_event(config, sender=_entity_user_id(config, "research"), acting_requester=bot_account)

    await harness.deliver(room, event)

    assert [request.user_id for request in harness.runner.requests] == ([bot_account] if admitted else [])


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_acting_requester_written_by_a_human_is_ignored(tmp_path: Path) -> None:
    """Only a managed entity's own reply may name the requester it acts for."""
    config = _owner_only_general_config(tmp_path)
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", "research")

    await harness.deliver(room, _entity_reply_event(config, sender=_SENDER, acting_requester=_OWNER))

    assert harness.policy.plan_turn_calls == 0
    assert harness.runner.requests == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_unmentioned_agent_reply_for_a_human_stays_agent_chatter(tmp_path: Path) -> None:
    """Naming a human requester does not turn another agent's unaddressed reply into a human message."""
    config = _owner_only_general_config(tmp_path)
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", "research")
    event = _entity_reply_event(
        config,
        sender=_entity_user_id(config, "research"),
        acting_requester=_OWNER,
        mention=None,
    )

    await harness.deliver(room, event)

    assert harness.policy.plan_turn_calls == 0
    assert harness.runner.requests == []
    assert harness.ignored_dispatch_sources == [(event.event_id,)]


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_agent_still_drops_its_own_reply_that_names_a_human_requester(tmp_path: Path) -> None:
    """An agent's echo of its own reply is dropped, whoever the reply was written for."""
    config = _owner_only_general_config(tmp_path)
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", "research")
    event = _entity_reply_event(config, sender=_entity_user_id(config, "general"), acting_requester=_OWNER)

    await harness.deliver(room, event)

    assert harness.policy.plan_turn_calls == 0
    assert harness.runner.requests == []
    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id) is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize("membership", ["joined", "invited", "departed", "unknown"])
async def test_agent_reply_wakes_a_mentioned_agent_only_while_its_requester_is_joined(
    tmp_path: Path,
    membership: str,
) -> None:
    """An agent's mention wakes another agent only while the person it acts for is joined to the room."""
    config = _owner_only_general_config(tmp_path)
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", "research")
    room.add_member(_OWNER, _OWNER, None, invited=membership == "invited")
    if membership == "departed":
        room.remove_member(_OWNER)
    room.members_synced = membership != "unknown"
    harness.ingress._client().joined_members.return_value = nio.JoinedMembersError("unavailable")
    event = _entity_reply_event(config, sender=_entity_user_id(config, "research"), acting_requester=_OWNER)

    await harness.deliver(room, event)

    assert [request.user_id for request in harness.runner.requests] == ([_OWNER] if membership == "joined" else [])
    if membership != "joined":
        assert harness.ignored_dispatch_sources == [(event.event_id,)]


def test_consecutive_agent_reply_limit_defaults_to_fifty_and_must_be_positive() -> None:
    """The limit on consecutive agent messages is fifty unless configured, and never below one."""
    assert DefaultsConfig().max_consecutive_agent_replies == 50
    with pytest.raises(ValidationError, match="max_consecutive_agent_replies"):
        DefaultsConfig(max_consecutive_agent_replies=0)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_agents_stop_waking_each_other_at_the_limit_until_a_person_writes(tmp_path: Path) -> None:
    """Consecutive agent messages since a person last wrote stop the chain; a new person message restarts it."""
    config = _owner_only_general_config(tmp_path)
    config.defaults.max_consecutive_agent_replies = 2
    harness = _build_harness(config, tmp_path)
    room = _joined_room(config, "general", "research")
    general = _entity_user_id(config, "general")
    research = _entity_user_id(config, "research")
    owner_message = make_visible_message(sender=_OWNER, body="@research look into it", event_id=_THREAD_ROOT)
    general_message = make_visible_message(sender=general, body="@research here is more")

    def reply(event_id: str) -> nio.RoomMessageText:
        event = _entity_reply_event(config, sender=research, acting_requester=_OWNER, event_id=event_id)
        event.source["content"]["m.relates_to"] = {"rel_type": "m.thread", "event_id": _THREAD_ROOT}
        return event

    capped = reply("$capped-reply:localhost")
    _serve_conversation(
        harness,
        [owner_message, general_message, make_visible_message(sender=research, event_id=capped.event_id)],
    )
    await harness.deliver(room, capped)

    assert harness.runner.requests == []
    assert harness.ignored_dispatch_sources == [(capped.event_id,)]

    resumed = reply("$resumed-reply:localhost")
    _serve_conversation(
        harness,
        [
            owner_message,
            general_message,
            make_visible_message(sender=research, event_id=capped.event_id),
            make_visible_message(sender=_OWNER, body="keep going"),
            make_visible_message(sender=research, event_id=resumed.event_id),
        ],
    )
    await harness.deliver(room, resumed)

    assert [request.response_envelope.source_event_id for request in harness.runner.requests] == [resumed.event_id]


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize("placeholder_thread", [_THREAD_ROOT, None])
async def test_mention_in_an_agents_finished_reply_wakes_the_mentioned_agent_once(
    tmp_path: Path,
    placeholder_thread: str | None,
) -> None:
    """A reply delivered as the final edit of its placeholder dispatches its mention once, answering that reply."""
    config = _owner_only_general_config(tmp_path)
    harness = _build_harness(config, tmp_path)
    room = _joined_room(config, "general", "research")
    research = _entity_user_id(config, "research")
    placeholder_id = "$placeholder:localhost"
    _admit_placeholder(harness, placeholder_id, placeholder_thread)
    placeholder = _entity_reply_event(config, sender=research, acting_requester=_OWNER, event_id=placeholder_id)
    placeholder.source["content"]["body"] = "Thinking..."
    placeholder.source["content"][constants.STREAM_STATUS_KEY] = constants.STREAM_STATUS_PENDING
    if placeholder_thread is not None:
        placeholder.source["content"]["m.relates_to"] = {"rel_type": "m.thread", "event_id": placeholder_thread}
    final_edit = _final_reply_edit(config, placeholder_id=placeholder_id, event_id="$final:localhost")

    await harness.deliver(room, placeholder)
    assert harness.runner.requests == []
    await harness.deliver(room, final_edit)
    await harness.deliver(room, final_edit)
    await harness.deliver(
        room,
        _final_reply_edit(config, placeholder_id=placeholder_id, event_id="$regenerated:localhost"),
    )

    assert len(harness.runner.requests) == 1
    request = harness.runner.requests[0]
    assert request.user_id == _OWNER
    assert request.response_envelope.origin.acting_sender_id == research
    # The answer replies to the finished reply, in its thread, or in a new thread on it.
    assert request.reply_to_event_id == placeholder_id
    assert request.response_envelope.target.resolved_thread_id == (placeholder_thread or placeholder_id)
    assert request.sources.logical_source_event_ids == (final_edit.event_id,)
    assert request.sources.discovery_event_ids == (placeholder_id,)
    assert "please take this over" in request.prompt


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_placeholder_the_mentioned_agent_cannot_answer_does_not_swallow_the_finished_reply(
    tmp_path: Path,
) -> None:
    """A placeholder carries no requester, so its refusal must not mark the turn its finished reply needs."""
    config = _owner_only_general_config(tmp_path)
    harness = _build_harness(config, tmp_path)
    room = _joined_room(config, "general", "research")
    placeholder_id = "$placeholder:localhost"
    _admit_placeholder(harness, placeholder_id)
    placeholder = _entity_reply_event(
        config,
        sender=_entity_user_id(config, "research"),
        acting_requester=_OWNER,
        mention=None,
        event_id=placeholder_id,
    )
    content = placeholder.source["content"]
    del content[constants.ACTING_REQUESTER_KEY]
    content["body"] = "Thinking..."
    content[constants.STREAM_STATUS_KEY] = constants.STREAM_STATUS_PENDING
    content["m.relates_to"] = {"rel_type": "m.thread", "event_id": _THREAD_ROOT}

    await harness.deliver(room, placeholder)
    await harness.deliver(room, _final_reply_edit(config, placeholder_id=placeholder_id, event_id="$final:localhost"))

    assert len(harness.runner.requests) == 1
    assert harness.runner.requests[0].user_id == _OWNER


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize("status", [constants.STREAM_STATUS_CANCELLED, constants.STREAM_STATUS_ERROR])
async def test_cancelled_or_failed_agent_reply_never_wakes_the_agent_it_mentions(tmp_path: Path, status: str) -> None:
    """Only a completed reply hands off; a stopped or failed one stays an agent's edit, which changes nothing."""
    config = _owner_only_general_config(tmp_path)
    # The edit path checks the editing agent's own account, so general admits research here.
    with_responder_access(config, "general", users=[_OWNER, _entity_user_id(config, "research")])
    harness = _build_harness(config, tmp_path)
    regenerator = _NoEditRegeneration()
    _replace_harness_deps(harness, edit_regenerator=regenerator)
    room = _joined_room(config, "general", "research")
    _admit_placeholder(harness, "$placeholder:localhost")
    edit = _final_reply_edit(
        config,
        placeholder_id="$placeholder:localhost",
        event_id="$stopped:localhost",
        status=status,
    )

    await harness.deliver(room, edit)

    assert harness.runner.requests == []
    assert regenerator.edit_event_ids == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_agent_reply_finished_through_a_sidecar_still_acts_for_its_requester(tmp_path: Path) -> None:
    """An oversized final reply keeps its requester inside the replacement content, where the handoff reads it."""
    config = _owner_only_general_config(tmp_path)
    harness = _build_harness(config, tmp_path)
    room = _joined_room(config, "general", "research")
    placeholder_id = "$placeholder:localhost"
    _admit_placeholder(harness, placeholder_id)
    uploads: list[bytes] = []

    async def upload(**kwargs: Any) -> tuple[nio.UploadResponse, None]:  # noqa: ANN401
        uploads.append(kwargs["data_provider"](None, None).read())
        return nio.UploadResponse("mxc://localhost/final-reply"), None

    upload_client = make_matrix_client_mock()
    upload_client.upload = AsyncMock(side_effect=upload)
    body = "@general please take this over. " + "Details. " * 4_000
    edit = _final_reply_edit(config, placeholder_id=placeholder_id, event_id="$final:localhost", body=body)
    sidecar_content = await prepare_large_message(upload_client, _ROOM_ID, edit.source["content"], room_encrypted=False)
    assert constants.ACTING_REQUESTER_KEY not in sidecar_content
    harness.ingress._client().download = AsyncMock(
        return_value=MagicMock(spec=nio.DownloadResponse, body=uploads[0]),
    )

    await harness.deliver(room, _final_reply_edit_event(config, sidecar_content, event_id="$final:localhost"))

    assert [request.user_id for request in harness.runner.requests] == [_OWNER]
    assert harness.runner.requests[0].prompt.count("Details.") == 4_000


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_command_an_agent_wrote_for_a_human_runs_with_that_humans_authority(tmp_path: Path) -> None:
    """A human cannot change entities their access excludes by having another agent post the command."""
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General", access=ResponderAccessConfig(users=[_OWNER])),
                "research": AgentConfig(display_name="Research", access=ResponderAccessConfig(users=[_SENDER])),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
            router={"access": {"current_room_members": False, "members_of_rooms": [], "users": [_SENDER]}},
        ),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    room = _joined_room(config, ROUTER_AGENT_NAME, "general", "research", requester=_SENDER)
    thread_root = "$thread-root:localhost"
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "!model default",
                "msgtype": "m.text",
                "m.mentions": {"user_ids": [_entity_user_id(config, ROUTER_AGENT_NAME)]},
                "m.relates_to": {"rel_type": "m.thread", "event_id": thread_root},
                constants.ACTING_REQUESTER_KEY: _SENDER,
            },
            "event_id": "$agent-command:localhost",
            "sender": _entity_user_id(config, "research"),
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )

    await harness.deliver(room, event)

    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id) is True
    overrides = resolve_thread_model_override(runtime_paths_for(config), thread_root, config=config)
    assert set(overrides.active) == {"research", ROUTER_AGENT_NAME}


@pytest.mark.asyncio
async def test_desktop_command_an_agent_wrote_for_a_human_is_refused(tmp_path: Path) -> None:
    """Only the human may change their Desktop target; an agent posting the command for them is refused."""
    config = bind_runtime_paths(
        Config(agents={"code": AgentConfig(display_name="Code", tools=["desktop"])}),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    room = _room_with_members(config, ROUTER_AGENT_NAME, "code")
    room.members_synced = True
    credentials = get_runtime_credentials_manager(runtime_paths_for(config))
    save_desktop_credentials(credentials, {"device_id": "DEVICE"}, requester_id=_SENDER, agent_name="code")

    def disconnect_event(sender: str, event_id: str) -> nio.RoomMessageText:
        return nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "!desktop disconnect confirm",
                    "msgtype": "m.text",
                    "m.mentions": {"user_ids": [_entity_user_id(config, ROUTER_AGENT_NAME)]},
                    constants.ACTING_REQUESTER_KEY: _SENDER,
                },
                "event_id": event_id,
                "sender": sender,
                "origin_server_ts": 1_000_000,
                "room_id": _ROOM_ID,
                "type": "m.room.message",
            },
        )

    await harness.deliver(room, disconnect_event(_entity_user_id(config, "code"), "$agent-command:localhost"))

    assert harness.gateway.sent[-1].response_text == "❌ Agents cannot run this command for you. Send it yourself."
    assert load_desktop_credentials(credentials, requester_id=_SENDER, agent_name="code") is not None

    await harness.deliver(room, disconnect_event(_SENDER, "$human-command:localhost"))

    assert harness.gateway.sent[-1].response_text == "✅ Desktop disconnected for you and agent `code`."
    assert load_desktop_credentials(credentials, requester_id=_SENDER, agent_name="code") is None


@pytest.mark.asyncio
async def test_scheduled_task_text_never_runs_as_a_command(tmp_path: Path) -> None:
    """An agent cannot run a command as the human by scheduling its text in a new thread."""
    config = bind_runtime_paths(
        Config(agents={"code": AgentConfig(display_name="Code", tools=["desktop"])}),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    room = _room_with_members(config, ROUTER_AGENT_NAME, "code")
    room.members_synced = True
    credentials = get_runtime_credentials_manager(runtime_paths_for(config))
    save_desktop_credentials(credentials, {"device_id": "DEVICE"}, requester_id=_SENDER, agent_name="code")
    workflow = ScheduledWorkflow(
        schedule_type="once",
        message="!desktop disconnect confirm",
        description="Disconnect Desktop",
        created_by=_SENDER,
        room_id=_ROOM_ID,
        new_thread=True,
    )
    content = await _prepare_scheduled_trigger(
        make_matrix_client_mock(),
        workflow,
        config,
        runtime_paths_for(config),
        make_conversation_reader_mock(),
        HookRegistryState(HookRegistry.empty()),
        "task-1",
        None,
        MessageTarget.for_scheduled_task(workflow),
        "correlation-1",
    )
    assert content is not None
    assert content["body"] == "!desktop disconnect confirm"
    event = nio.RoomMessageText.from_dict(
        {
            "content": content,
            "event_id": "$scheduled-command:localhost",
            "sender": _entity_user_id(config, "code"),
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )

    await harness.deliver(room, event)

    assert load_desktop_credentials(credentials, requester_id=_SENDER, agent_name="code") is not None
    assert all("Desktop disconnected" not in sent.response_text for sent in harness.gateway.sent)


@pytest.mark.asyncio
async def test_agent_reply_for_a_human_does_not_share_that_humans_follow_up_batch(
    config: Config,
    tmp_path: Path,
) -> None:
    """A backlog splits a human's follow-up from an agent reply written for them, so only the reply is chatter."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", "research")
    thread_root = "$thread-root:localhost"
    human_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "@general please also check the logs",
                "msgtype": "m.text",
                "m.mentions": {"user_ids": [_entity_user_id(config, "general")]},
                "m.relates_to": {"rel_type": "m.thread", "event_id": thread_root},
            },
            "event_id": "$human-follow-up:localhost",
            "sender": _SENDER,
            "origin_server_ts": 999,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )
    agent_event = _entity_reply_event(
        config,
        sender=_entity_user_id(config, "research"),
        acting_requester=_SENDER,
        mention=None,
        event_id="$agent-follow-up:localhost",
    )
    agent_event.source["content"]["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_root}
    agent_pending = make_pending_event(
        agent_event,
        room,
        source_kind=MESSAGE_SOURCE_KIND,
        active_follow_up=True,
        payload_trusted=True,
        facts=harness.ingress.deps.ingress.classify(agent_event, room_id=room.room_id),
    )
    assert agent_pending.facts.acts_for_requester
    pending_events = (
        make_pending_event(
            human_event,
            room,
            source_kind=MESSAGE_SOURCE_KIND,
            requester_user_id=_SENDER,
            active_follow_up=True,
        ),
        agent_pending,
    )
    gate = CoalescingGate(
        dispatch_turn=harness.dispatcher.handle_prepared_turn,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )

    for pending_event in pending_events:
        await gate.admit(
            active_follow_up_coalescing_key(room.room_id, thread_root),
            ready_result=ReadyPendingEvent(pending_event=pending_event),
            source_event_id=pending_event.event.event_id,
        )
    await gate.drain_all()
    await harness.runner.settle_inbox_responses()

    assert harness.ignored_dispatch_sources == [(agent_event.event_id,)]
    assert [request.sources.logical_source_event_ids for request in harness.runner.requests] == [
        (human_event.event_id,),
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_policy_planning_waits_for_config_replacement_before_authorizing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn prechecked before reload must plan against the replacement policy."""
    runtime_paths = test_runtime_paths(tmp_path / "runtime")
    old_config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General",
                    access=ResponderAccessConfig(users=[_SENDER]),
                ),
            },
        ),
        runtime_paths,
    )
    new_config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General",
                    access=ResponderAccessConfig(users=[]),
                ),
            },
        ),
        runtime_paths,
    )
    harness = _build_harness(old_config, tmp_path)
    room = _room_with_members(old_config, "general")
    event = _text_event("please answer after the reload")
    admission_gate = harness.runtime.response_admission_gate
    assert admission_gate.close_if_idle()

    plan_started = asyncio.Event()
    gate_wait_started = _observe_admission_wait(harness, monkeypatch)
    real_plan_turn = harness.policy.plan_turn

    async def observed_plan_turn(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        plan_started.set()
        return await real_plan_turn(*args, **kwargs)

    monkeypatch.setattr(harness.policy, "plan_turn", observed_plan_turn)
    delivery_task = asyncio.create_task(harness.deliver(room, event))
    await asyncio.wait_for(gate_wait_started.wait(), timeout=1)

    assert not plan_started.is_set()
    harness.runtime.orchestrator.config = new_config
    admission_gate.reopen()
    await delivery_task

    assert harness.policy.plan_turn_calls == 0
    assert harness.runner.requests == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_ingress_denial_waits_for_config_replacement_before_settling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient pre-publication denial must not terminally discard the turn."""
    runtime_paths = test_runtime_paths(tmp_path / "runtime")
    old_config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General",
                    access=ResponderAccessConfig(users=[]),
                ),
            },
        ),
        runtime_paths,
    )
    new_config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General",
                    access=ResponderAccessConfig(users=[_SENDER]),
                ),
            },
        ),
        runtime_paths,
    )
    harness = _build_harness(old_config, tmp_path)
    room = _room_with_members(old_config, "general")
    event = _text_event("authorize me after the reload")
    admission_gate = harness.runtime.response_admission_gate
    assert admission_gate.close_if_idle()

    gate_wait_started = _observe_admission_wait(harness, monkeypatch)
    delivery_task = asyncio.create_task(harness.deliver(room, event))
    try:
        await asyncio.wait_for(gate_wait_started.wait(), timeout=1)
        assert not delivery_task.done()
        harness.runtime.orchestrator.config = new_config
    finally:
        admission_gate.reopen()
        await delivery_task

    assert len(harness.runner.requests) == 1
    assert harness.ignored_dispatch_sources == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_command_waits_for_config_replacement_and_rechecks_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command prechecked before reload must not execute under a replacement deny policy."""
    runtime_paths = test_runtime_paths(tmp_path / "runtime")
    old_config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General")},
            router={"access": {"current_room_members": False, "members_of_rooms": [], "users": [_SENDER]}},
        ),
        runtime_paths,
    )
    new_config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General")},
            router={"access": {"current_room_members": False, "members_of_rooms": [], "users": []}},
        ),
        runtime_paths,
    )
    harness = _build_harness(old_config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    room = _room_with_members(old_config, ROUTER_AGENT_NAME, "general")
    event = _text_event("!help", event_id="$command-during-reload:localhost")
    admission_gate = harness.runtime.response_admission_gate
    assert admission_gate.close_if_idle()

    gate_wait_started = _observe_admission_wait(harness, monkeypatch)
    delivery_task = asyncio.create_task(harness.deliver(room, event))
    await asyncio.wait_for(gate_wait_started.wait(), timeout=1)

    assert harness.gateway.sent == []
    harness.runtime.orchestrator.config = new_config
    admission_gate.reopen()
    await delivery_task

    assert harness.gateway.sent == []
    assert harness.ignored_dispatch_sources == []
    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id)


@pytest.mark.asyncio
async def test_policy_decision_reserves_config_until_response_handoff(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reload cannot commit between reply authorization and runner handoff."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _text_event("keep this decision atomic")
    pending_write_started = asyncio.Event()
    continue_pending_write = asyncio.Event()
    real_record_pending_turn = harness.turn_store.record_pending_turn

    async def hold_pending_write(turn_record: TurnRecord) -> turn_rows.PendingTurn | None:
        pending_write_started.set()
        await continue_pending_write.wait()
        return await real_record_pending_turn(turn_record)

    monkeypatch.setattr(harness.turn_store, "record_pending_turn", hold_pending_write)
    delivery_task = asyncio.create_task(harness.deliver(room, event))
    await asyncio.wait_for(pending_write_started.wait(), timeout=1)

    admission_gate = harness.runtime.response_admission_gate
    reload_closed_gate = admission_gate.close_if_idle()
    try:
        assert not reload_closed_gate
    finally:
        if reload_closed_gate:
            admission_gate.reopen()
        continue_pending_write.set()
        await delivery_task

    assert len(harness.runner.requests) == 1


@pytest.mark.asyncio
async def test_an_emote_is_answered_like_any_other_user_message(config: Config, tmp_path: Path) -> None:
    """`/me asks the bot to X` produces a reply, with its body as the prompt.

    Driven through the journal dispatcher rather than turn ingress directly,
    because the thing that refused an emote was the journal's kind binding, not
    the turn engine: the event was committed as actionable work and then
    discarded by an ``isinstance`` check before any turn existed.

    An emote is text written in the third person, so the prompt is the body
    verbatim. Nothing downstream is told the message was an emote, which is why
    mentions, routing, and commands need no emote-specific handling.
    """
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _emote_event("asks the bot to summarize the build failure", event_id="$emote:localhost")
    obligation_runner = _obligation_runner(
        harness,
        tracking_path=tmp_path / "dispatch-tracking",
        matrix_user_id=_entity_user_id(config, "general"),
        entity_name="general",
        room=room,
    )

    await admit_dispatch_event(obligation_runner, room, event, EventKind.MESSAGE, EventClass.ACTIONABLE)
    await obligation_runner.drain_once()
    await harness.gate.drain_all()
    await harness.runner.settle_inbox_responses()

    assert harness.policy.plan_turn_calls == 1
    assert len(harness.runner.requests) == 1
    assert harness.runner.requests[0].prompt == "asks the bot to summarize the build failure"
    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [[], {}, 1], ids=["list", "dict", "number"])
async def test_a_stream_status_that_is_not_text_does_not_hide_a_message(
    config: Config,
    tmp_path: Path,
    status: object,
) -> None:
    """Any room member can write a status, so one that is not text is not one.

    The message is answered like any other. Raising here instead would leave
    its source pending in the journal, and the room's ordered replay would then
    retry it forever ahead of every later message.
    """
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _text_event("Summarize the build failure", event_id="$malformed-status:localhost")
    event.source["content"][constants.STREAM_STATUS_KEY] = status

    await harness.deliver(room, event)

    assert len(harness.runner.requests) == 1
    assert harness.runner.requests[0].prompt == "Summarize the build failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", sorted(constants.UNFINISHED_REPLY_STATUSES))
async def test_a_message_still_being_written_is_not_dispatched(
    config: Config,
    tmp_path: Path,
    status: str,
) -> None:
    """An unfinished reply waits for the revision that finishes it."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _text_event("Thinking...", event_id="$unfinished:localhost")
    event.source["content"][constants.STREAM_STATUS_KEY] = status

    outcome = await harness.ingress.handle_text_event(room, event)

    assert outcome is TurnDispatchOutcome.INTENTIONALLY_IGNORED
    assert harness.policy.plan_turn_calls == 0
    assert harness.runner.requests == []


@pytest.mark.asyncio
async def test_response_waits_for_pending_context_persistence_before_generation(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session-backed generation must not start before its cleanup context is durable.

    Edit and redaction cleanup and a replay after a crash find the generated run through the turn row, so the
    row and its response context have committed by the time the runner starts generating.
    """
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _text_event("persist my response context first")
    real_write = TurnRecordStore.write
    pending_write_started = asyncio.Event()
    release_pending_write = asyncio.Event()
    committed_at_generation: list[TurnRecord | None] = []
    generate_response = harness.runner.generate_response

    async def write_with_barrier(
        records: TurnRecordStore,
        operation: object,
        *args: object,
        **kwargs: object,
    ) -> object:
        if _is_pending_write_for(operation, args, event.event_id):
            pending_write_started.set()
            await release_pending_write.wait()
        return await real_write(records, operation, *args, **kwargs)

    async def generate_after_reading_the_turn(request: ResponseRequest) -> str | None:
        committed_at_generation.append(await harness.turn_store.get_turn_record(event.event_id))
        return await generate_response(request)

    monkeypatch.setattr(TurnRecordStore, "write", write_with_barrier)
    monkeypatch.setattr(harness.runner, "generate_response", generate_after_reading_the_turn)
    delivery = asyncio.create_task(harness.deliver(room, event))
    try:
        await asyncio.wait_for(pending_write_started.wait(), timeout=5)
        assert harness.runner.requests == []
    finally:
        release_pending_write.set()

    await asyncio.wait_for(delivery, timeout=5)
    await harness.runner.settle_inbox_responses()

    assert len(harness.runner.requests) == 1
    (pending,) = committed_at_generation
    assert pending is not None
    assert pending.completed is False
    assert pending.response_owner == "general"
    assert pending.history_scope is not None
    assert pending.conversation_target == harness.runner.requests[0].response_envelope.target
    persisted = await harness.turn_store.get_turn_record(event.event_id)
    assert persisted is not None
    assert persisted.completed is True


def _is_pending_write_for(operation: object, args: tuple[object, ...], event_id: str) -> bool:
    """Return whether one turn-row write is the pending write of a turn that indexes this event."""
    if operation is not turn_rows.record_pending_turn:
        return False
    record = args[0]
    assert isinstance(record, TurnRecord)
    return event_id in record.indexed_event_ids


def _scheduled_fire_event(
    config: Config,
    *,
    extra_content: dict[str, Any],
    event_id: str = "$scheduled:localhost",
    new_thread: bool = False,
) -> nio.RoomMessageText:
    """Build a self-authored scheduled-fire event as the scheduling executor sends it."""
    content: dict[str, Any] = {
        "body": "Poll the queue" if new_thread else "⏰ [Automated Task]\nPoll the queue",
        "msgtype": "m.text",
        constants.SOURCE_KIND_KEY: SCHEDULED_SOURCE_KIND,
        constants.ORIGINAL_SENDER_KEY: _SENDER,
        **extra_content,
    }
    if new_thread:
        content[constants.PER_FIRE_THREAD_ROOT_KEY] = True
    return nio.RoomMessageText.from_dict(
        {
            "content": content,
            "event_id": event_id,
            "sender": _entity_user_id(config, "general"),
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )


def _silent_schedule_event(
    config: Config,
    *,
    extra_content: dict[str, Any],
    event_id: str = "$silent-schedule:localhost",
) -> nio.RoomMessageText:
    """Build the message-shaped event journal dispatch gives turn ingress."""
    return nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "Poll the queue without posting the trigger",
                "msgtype": "m.text",
                constants.SOURCE_KIND_KEY: SILENT_SCHEDULE_SOURCE_KIND,
                constants.ORIGINAL_SENDER_KEY: _SENDER,
                **extra_content,
            },
            "event_id": event_id,
            "sender": _entity_user_id(config, "general"),
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )


def _silent_schedule_trigger_event(
    config: Config,
    *,
    event_id: str = "$silent-schedule-trigger:localhost",
) -> nio.UnknownEvent:
    """Build the custom event admitted before message-shaped dispatch."""
    event = nio.Event.parse_event(
        {
            "content": {
                "body": "Poll the queue without posting the trigger",
                constants.SOURCE_KIND_KEY: SILENT_SCHEDULE_SOURCE_KIND,
                constants.ORIGINAL_SENDER_KEY: _SENDER,
            },
            "event_id": event_id,
            "sender": _entity_user_id(config, "general"),
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": constants.SILENT_SCHEDULE_EVENT_TYPE,
        },
    )
    assert isinstance(event, nio.UnknownEvent)
    return event


def _single_agent_config(tmp_path: Path, thread_mode: str) -> Config:
    """One-agent config with the given thread mode, bound to isolated runtime paths."""
    return bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General", thread_mode=thread_mode)}),
        test_runtime_paths(tmp_path / "runtime"),
    )


def _membership_single_agent_config(tmp_path: Path) -> Config:
    """One agent whose current room membership is its only human access clause."""
    return bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General",
                    access=ResponderAccessConfig(
                        current_room_members=True,
                        members_of_rooms=[],
                    ),
                ),
            },
        ),
        test_runtime_paths(tmp_path / "runtime"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("model", "expected_model"), [("cheap", "cheap"), (None, None), ("", None), (5, None)])
async def test_scheduled_fire_history_limit_reaches_response_request(
    config: Config,
    tmp_path: Path,
    model: object,
    expected_model: str | None,
) -> None:
    """The history limit annotated on a trusted scheduled fire lands on the ResponseRequest."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _scheduled_fire_event(
        config,
        extra_content={constants.SCHEDULED_HISTORY_LIMIT_KEY: 3, constants.SCHEDULED_MODEL_KEY: model},
    )

    await harness.deliver(room, event)

    assert len(harness.runner.requests) == 1
    request = harness.runner.requests[0]
    assert request.scheduled_history_budget == ScheduledHistoryBudget(
        limit=3,
        source_event_id="$scheduled:localhost",
    )
    assert request.response_envelope.origin.intent is TurnIntent.SCHEDULED_FIRE
    assert request.scheduled_model == expected_model


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_scheduled_fire_rechecks_membership_after_requester_revocation(tmp_path: Path) -> None:
    """A requester removed after scheduling cannot execute through the preserved identity."""
    config = _membership_single_agent_config(tmp_path)
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _scheduled_fire_event(config, extra_content={})
    responder_user_id = _entity_user_id(config, "general")
    client = make_matrix_client_mock(user_id=responder_user_id)
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room.room_id])
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[
            nio.RoomMember(_SENDER, None, None),
            nio.RoomMember(responder_user_id, None, None),
        ],
        room_id=room.room_id,
    )
    memberships = harness.runtime.agent_reply_memberships
    await memberships.refresh(config, runtime_paths_for(config), client)
    assert harness.policy.can_reply_to_sender_in_room(_SENDER, room.room_id)

    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember(responder_user_id, None, None)],
        room_id=room.room_id,
    )
    await memberships.refresh(config, runtime_paths_for(config), client)
    room.remove_member(_SENDER)  # This bot's own sync sees the departure too.

    await harness.deliver(room, event)

    assert not harness.policy.can_reply_to_sender_in_room(_SENDER, room.room_id)
    assert harness.runner.requests == []
    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize("kind", ["text", "media", "edit"])
async def test_membership_uncertainty_does_not_complete_ingress(tmp_path: Path, kind: str) -> None:
    """An invalidated grant must preserve the exact admitted callback for retry."""
    config = _membership_single_agent_config(tmp_path)
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _image_event(event_id="$pending") if kind == "media" else _text_event("owed", event_id="$pending")
    if kind == "edit":
        event.source["content"]["m.relates_to"] = {"rel_type": "m.replace", "event_id": "$original"}
        event.source["content"]["m.new_content"] = {"msgtype": "m.text", "body": "owed edit"}
    memberships = harness.runtime.agent_reply_memberships
    client = make_matrix_client_mock(user_id=_entity_user_id(config, "general"))
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room.room_id])
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember(_SENDER, None, None)],
        room_id=room.room_id,
    )
    await memberships.refresh(config, runtime_paths_for(config), client)
    memberships.invalidate(config, reason="uncertain_sync_response")
    validator = harness.ingress.deps.ingress
    with pytest.raises(ReplyMembershipPendingError):
        await validator.precheck_event(
            room,
            event,
            validator.classify(event, room_id=room.room_id),
            is_edit=kind == "edit",
        )
    dispatcher = _obligation_runner(
        harness,
        tracking_path=tmp_path / "dispatch-tracking",
        matrix_user_id=_entity_user_id(config, "general"),
        entity_name="general",
        room=room,
    )
    await admit_dispatch_event(
        dispatcher,
        room,
        event,
        EventKind.MEDIA if kind == "media" else EventKind.MESSAGE,
        EventClass.ACTIONABLE,
    )
    before = await dispatcher.store.pending()
    await dispatcher.drain_once()
    assert await dispatcher.store.pending() == before
    assert await harness.turn_store.get_turn_record(event.event_id) is None
    assert harness.runner.requests == []
    await memberships.refresh(config, runtime_paths_for(config), client)
    assert await validator.precheck_event(
        room,
        event,
        validator.classify(event, room_id=room.room_id),
        is_edit=kind == "edit",
    )
    client.joined_members.return_value = nio.JoinedMembersResponse(members=[], room_id=room.room_id)
    await memberships.refresh(config, runtime_paths_for(config), client)
    room.remove_member(_SENDER)  # This bot's own sync sees the departure too.
    dispatcher.start()
    try:
        async with asyncio.timeout(5):
            while await dispatcher.store.is_pending(event.event_id):  # noqa: ASYNC110 - journal settlement has no event signal
                await asyncio.sleep(0.01)
    finally:
        await dispatcher.stop()
    assert await dispatcher.store.pending() == ()
    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id)
    assert harness.runner.requests == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_first_message_after_joining_waits_for_the_router_to_apply_the_join(tmp_path: Path) -> None:
    """A newcomer's first message must not be dropped because the router's sync is behind this agent's."""
    config = _membership_single_agent_config(tmp_path)
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")  # this agent's sync already shows the join
    event = _text_event("hello", event_id="$first")
    memberships = harness.runtime.agent_reply_memberships
    client = make_matrix_client_mock(user_id=_entity_user_id(config, "general"))
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room.room_id])
    client.joined_members.return_value = nio.JoinedMembersResponse(members=[], room_id=room.room_id)
    await memberships.refresh(config, runtime_paths_for(config), client)
    validator = harness.ingress.deps.ingress
    router_id = _entity_user_id(config, ROUTER_AGENT_NAME)

    def router_applies_join(user_id: str) -> None:
        join = nio.RoomMemberEvent.from_dict(
            {
                "type": "m.room.member",
                "event_id": f"$join-{user_id}",
                "sender": user_id,
                "state_key": user_id,
                "origin_server_ts": 1,
                "content": {"membership": "join"},
            },
        )
        assert isinstance(join, nio.RoomMemberEvent)
        assert memberships.apply_member_event(config, room.room_id, join, control_user_id=router_id)

    # The check settles in the same callback once the router catches up, instead of failing it into a journal retry.
    precheck = asyncio.create_task(
        validator.precheck_event(room, event, validator.classify(event, room_id=room.room_id)),
    )
    await asyncio.sleep(0)
    assert not precheck.done()
    router_applies_join("@someone_else:localhost")  # a publication that still lacks the sender
    await asyncio.sleep(0)
    assert not precheck.done()
    assert await harness.turn_store.get_turn_record(event.event_id) is None
    router_applies_join(_SENDER)

    assert await asyncio.wait_for(precheck, timeout=5) is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_message_checked_during_a_membership_refresh_passes_when_the_refresh_publishes(tmp_path: Path) -> None:
    """A grant awaiting the router's authoritative refresh settles in the same callback once the refresh lands."""
    config = _membership_single_agent_config(tmp_path)
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _text_event("owed", event_id="$during-refresh")
    memberships = harness.runtime.agent_reply_memberships
    client = make_matrix_client_mock(user_id=_entity_user_id(config, "general"))
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room.room_id])
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember(_SENDER, None, None)],
        room_id=room.room_id,
    )
    await memberships.refresh(config, runtime_paths_for(config), client)
    memberships.invalidate(config, reason="uncertain_sync_response")
    validator = harness.ingress.deps.ingress

    precheck = asyncio.create_task(
        validator.precheck_event(room, event, validator.classify(event, room_id=room.room_id)),
    )
    await asyncio.sleep(0)
    assert not precheck.done()
    await memberships.refresh(config, runtime_paths_for(config), client)

    assert await asyncio.wait_for(precheck, timeout=5) is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_departed_sender_is_denied_once_this_agent_sees_the_leave(tmp_path: Path) -> None:
    """When the router applies a leave first, this agent's stale roster only delays the denial."""
    config = _membership_single_agent_config(tmp_path)
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")  # this agent has not seen the leave yet
    event = _text_event("sent before leaving", event_id="$late")
    memberships = harness.runtime.agent_reply_memberships
    client = make_matrix_client_mock(user_id=_entity_user_id(config, "general"))
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room.room_id])
    client.joined_members.return_value = nio.JoinedMembersResponse(members=[], room_id=room.room_id)
    await memberships.refresh(config, runtime_paths_for(config), client)
    validator = harness.ingress.deps.ingress

    with pytest.raises(ReplyMembershipPendingError):
        await validator.precheck_event(room, event, validator.classify(event, room_id=room.room_id))
    assert await harness.turn_store.get_turn_record(event.event_id) is None
    assert harness.runner.requests == []

    room.remove_member(_SENDER)  # this agent's sync delivers the leave

    assert not await validator.precheck_event(room, event, validator.classify(event, room_id=room.room_id))
    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id)
    assert harness.runner.requests == []


@pytest.mark.asyncio
async def test_scheduled_router_handoff_history_limit_reaches_response_request(
    config: Config,
    tmp_path: Path,
) -> None:
    """A trusted router handoff preserves the scheduled fire's history cap for the target agent."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "@general could you help with this?",
                "msgtype": "m.text",
                constants.SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                constants.ORIGINAL_SENDER_KEY: _SENDER,
                constants.SCHEDULED_HISTORY_LIMIT_KEY: 2,
                "com.mindroom.scheduled_model": "cheap",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$scheduled:localhost"}},
            },
            "event_id": "$scheduled-router-handoff:localhost",
            "sender": _entity_user_id(config, ROUTER_AGENT_NAME),
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )

    await harness.deliver(room, event)

    assert len(harness.runner.requests) == 1
    request = harness.runner.requests[0]
    assert request.scheduled_history_budget == ScheduledHistoryBudget(
        limit=2,
        source_event_id="$scheduled:localhost",
    )
    assert request.response_envelope.origin.intent is TurnIntent.ROUTER_HANDOFF
    assert request.scheduled_model == "cheap"


@pytest.mark.asyncio
async def test_scheduled_router_handoff_for_a_bot_account_keeps_model_and_history_limit(tmp_path: Path) -> None:
    """The router's relay of a task a bot account scheduled keeps the task's model and history cap."""
    bot_account = "@telegram:localhost"
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General")}, bot_accounts=[bot_account]),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "@general ⏰ [Automated Task]\nPoll the queue",
                "msgtype": "m.text",
                constants.SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                constants.ORIGINAL_SENDER_KEY: bot_account,
                constants.SCHEDULED_HISTORY_LIMIT_KEY: 2,
                constants.SCHEDULED_MODEL_KEY: "cheap",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$scheduled:localhost"}},
            },
            "event_id": "$scheduled-router-handoff:localhost",
            "sender": _entity_user_id(config, ROUTER_AGENT_NAME),
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )

    await harness.deliver(room, event)

    assert len(harness.runner.requests) == 1
    request = harness.runner.requests[0]
    assert request.response_envelope.requester_id == bot_account
    assert request.scheduled_history_budget == ScheduledHistoryBudget(
        limit=2,
        source_event_id="$scheduled:localhost",
    )
    assert request.scheduled_model == "cheap"


@pytest.mark.asyncio
async def test_router_relay_keeps_original_alias_unsettled_through_gate_handoff(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The routed source alias stays claimed while dispatch normalizes the relay."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    original_event_id = "$routed-original:localhost"
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "@general could you help with this?",
                "msgtype": "m.text",
                constants.ORIGINAL_SENDER_KEY: _SENDER,
                constants.SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": _THREAD_ROOT,
                    "m.in_reply_to": {"event_id": original_event_id},
                },
            },
            "event_id": "$router-relay:localhost",
            "sender": _entity_user_id(config, ROUTER_AGENT_NAME),
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )
    normalization_started = asyncio.Event()
    release_normalization = asyncio.Event()
    resolve_text_event = InboundTurnNormalizer.resolve_text_event

    async def normalize_with_barrier(
        normalizer: InboundTurnNormalizer,
        request: TextNormalizationRequest,
    ) -> PreparedIngress:
        normalization_started.set()
        await release_normalization.wait()
        return await resolve_text_event(normalizer, request)

    monkeypatch.setattr(InboundTurnNormalizer, "resolve_text_event", normalize_with_barrier)
    delivery = asyncio.create_task(harness.deliver(room, event))
    await normalization_started.wait()

    try:
        assert harness.turn_store.has_live_turn_claim(original_event_id)
    finally:
        release_normalization.set()
        await delivery
    assert not harness.turn_store.has_live_turn_claim(original_event_id)


@pytest.mark.asyncio
async def test_scheduled_new_thread_survives_router_handoff_in_room_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A router relay preserves the scheduled fire's per-fire thread and session."""
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General", thread_mode="room"),
                "research": AgentConfig(display_name="Research"),
            },
        ),
        test_runtime_paths(tmp_path / "runtime"),
    )
    room = _room_with_members(config, ROUTER_AGENT_NAME, "general", "research")
    scheduled_event = _scheduled_fire_event(
        config,
        extra_content={constants.SCHEDULED_MODEL_KEY: "cheap"},
        new_thread=True,
    )
    router_harness = _build_harness(config, tmp_path / "router", agent_name=ROUTER_AGENT_NAME)

    async def _route_to_general(*_args: object, **_kwargs: object) -> str:
        return "general"

    monkeypatch.setattr("mindroom.router.relay.suggest_responder_for_message", _route_to_general)

    await router_harness.deliver(room, scheduled_event)

    assert len(router_harness.gateway.sent) == 1
    handoff = router_harness.gateway.sent[0]
    assert handoff.target.resolved_thread_id == scheduled_event.event_id
    assert handoff.extra_content is not None
    assert handoff.extra_content[constants.SOURCE_KIND_KEY] == TRUSTED_INTERNAL_RELAY_SOURCE_KIND
    assert handoff.extra_content[constants.SCHEDULED_MODEL_KEY] == "cheap"
    assert handoff.extra_content[constants.PER_FIRE_THREAD_ROOT_KEY] is True
    assert handoff.extra_content[constants.PER_FIRE_THREAD_ROOT_EVENT_ID_KEY] == scheduled_event.event_id

    relay_content = {
        "body": handoff.response_text,
        "msgtype": "m.text",
        **handoff.extra_content,
        "m.relates_to": {"rel_type": "m.thread", "event_id": scheduled_event.event_id},
    }
    relay_event = nio.RoomMessageText.from_dict(
        {
            "content": relay_content,
            "event_id": "$scheduled-router-handoff:localhost",
            "sender": _entity_user_id(config, ROUTER_AGENT_NAME),
            "origin_server_ts": 1_000_001,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )
    agent_harness = _build_harness(config, tmp_path / "agent")

    await agent_harness.deliver(room, relay_event)

    assert len(agent_harness.runner.requests) == 1
    assert agent_harness.runner.requests[0].scheduled_model == "cheap"
    target = agent_harness.runner.requests[0].response_envelope.target
    assert target.resolved_thread_id == scheduled_event.event_id
    assert target.session_id == f"{_ROOM_ID}:{scheduled_event.event_id}"


@pytest.mark.asyncio
async def test_scheduled_fire_without_annotation_keeps_full_history(config: Config, tmp_path: Path) -> None:
    """A scheduled fire without the annotation must not cap history for that turn."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _scheduled_fire_event(config, extra_content={})

    await harness.deliver(room, event)

    assert len(harness.runner.requests) == 1
    assert harness.runner.requests[0].scheduled_history_budget is None


@pytest.mark.asyncio
async def test_a_hook_dispatch_carries_its_per_run_model(config: Config, tmp_path: Path) -> None:
    """Built-in automations post hook dispatches, so the per-run model they annotate reaches the request."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _scheduled_fire_event(
        config,
        extra_content={
            constants.SOURCE_KIND_KEY: HOOK_DISPATCH_SOURCE_KIND,
            constants.SCHEDULED_HISTORY_LIMIT_KEY: 3,
            constants.SCHEDULED_MODEL_KEY: "cheap",
        },
    )

    await harness.deliver(room, event)

    assert len(harness.runner.requests) == 1
    request = harness.runner.requests[0]
    assert request.response_envelope.origin.intent is TurnIntent.HOOK_DISPATCH
    assert request.scheduled_model == "cheap"
    assert request.scheduled_history_budget is None


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", [SCHEDULED_SOURCE_KIND, HOOK_DISPATCH_SOURCE_KIND])
async def test_user_message_cannot_spoof_scheduled_history_limit(
    config: Config,
    tmp_path: Path,
    source_kind: str,
) -> None:
    """History-limit and model annotations on untrusted user messages are ignored."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "please summarize the build failure",
                "msgtype": "m.text",
                constants.SOURCE_KIND_KEY: source_kind,
                constants.SCHEDULED_HISTORY_LIMIT_KEY: 0,
                "com.mindroom.scheduled_model": "cheap",
            },
            "event_id": _EVENT_ID,
            "sender": _SENDER,
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )

    await harness.deliver(room, event)

    assert len(harness.runner.requests) == 1
    request = harness.runner.requests[0]
    assert request.scheduled_history_budget is None
    assert request.response_envelope.origin.intent is TurnIntent.USER_MESSAGE
    assert request.scheduled_model is None


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_mode", ["thread", "room"])
async def test_scheduled_fire_response_starts_per_fire_thread_session(tmp_path: Path, thread_mode: str) -> None:
    """A room-level scheduled fire roots a per-fire thread and session in both thread modes."""
    config = _single_agent_config(tmp_path, thread_mode)
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _scheduled_fire_event(config, extra_content={}, new_thread=True)

    await harness.deliver(room, event)

    assert len(harness.runner.requests) == 1
    target = harness.runner.requests[0].response_envelope.target
    assert target.resolved_thread_id == "$scheduled:localhost"
    assert target.session_id == f"{_ROOM_ID}:$scheduled:localhost"


@pytest.mark.asyncio
async def test_room_mode_scheduled_fire_is_planned_with_its_own_thread_history(tmp_path: Path) -> None:
    """A room-mode agent's per-fire thread is where it answers, so its context reads that thread."""
    config = _single_agent_config(tmp_path, "room")
    fire_thread = thread_history_result(
        [make_visible_message(sender=_SENDER, body="Poll the queue", event_id="$scheduled:localhost")],
        is_full_history=True,
    )
    harness = _build_harness(config, tmp_path, thread_history=fire_thread)
    room = _room_with_members(config, "general")

    await harness.deliver(room, _scheduled_fire_event(config, extra_content={}, new_thread=True))

    assert len(harness.runner.requests) == 1
    request = harness.runner.requests[0]
    assert request.response_envelope.target.source_thread_id == "$scheduled:localhost"
    assert [message.event_id for message in request.thread_history] == ["$scheduled:localhost"]


@pytest.mark.asyncio
async def test_a_threaded_turn_replayed_after_the_room_switched_to_room_mode_is_answered_and_planned_at_room_level(
    tmp_path: Path,
) -> None:
    """Context follows the target: neither may name the persisted thread while the other answers in the room."""
    config = _single_agent_config(tmp_path, "room")
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")

    prepared = await harness.dispatcher._prepare_dispatch(
        make_test_turn(
            room,
            _text_event("hello", thread_id=_THREAD_ROOT),
            _SENDER,
            # Decided by ingress while the room was still in thread mode.
            coalescing_key=requester_coalescing_key(_ROOM_ID, _THREAD_ROOT, _SENDER),
        ),
    )

    assert prepared is not None
    assert prepared.target.resolved_thread_id is None
    assert (prepared.context.is_thread, prepared.context.thread_id) == (False, None)


@pytest.mark.asyncio
async def test_external_trigger_fire_response_starts_per_fire_thread_session(tmp_path: Path) -> None:
    """A room-level external trigger delivery roots a per-fire thread and session in room mode."""
    config = _single_agent_config(tmp_path, "room")
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "@general poll the webhook queue",
                "msgtype": "m.text",
                constants.SOURCE_KIND_KEY: EXTERNAL_TRIGGER_SOURCE_KIND,
                constants.ORIGINAL_SENDER_KEY: _SENDER,
                constants.PER_FIRE_THREAD_ROOT_KEY: True,
            },
            "event_id": "$trigger:localhost",
            "sender": _entity_user_id(config, "general"),
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )

    await harness.deliver(room, event)

    assert len(harness.runner.requests) == 1
    target = harness.runner.requests[0].response_envelope.target
    assert target.resolved_thread_id == "$trigger:localhost"
    assert target.session_id == f"{_ROOM_ID}:$trigger:localhost"


@pytest.mark.asyncio
async def test_consecutive_scheduled_fires_resolve_distinct_sessions(tmp_path: Path) -> None:
    """Two fires of one recurring room-mode schedule never share a thread root or session."""
    config = _single_agent_config(tmp_path, "room")
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")

    await harness.deliver(
        room,
        _scheduled_fire_event(config, extra_content={}, event_id="$fire-one:localhost", new_thread=True),
    )
    await harness.deliver(
        room,
        _scheduled_fire_event(config, extra_content={}, event_id="$fire-two:localhost", new_thread=True),
    )

    assert len(harness.runner.requests) == 2
    first_target = harness.runner.requests[0].response_envelope.target
    second_target = harness.runner.requests[1].response_envelope.target
    assert first_target.resolved_thread_id == "$fire-one:localhost"
    assert second_target.resolved_thread_id == "$fire-two:localhost"
    assert first_target.session_id == f"{_ROOM_ID}:$fire-one:localhost"
    assert second_target.session_id == f"{_ROOM_ID}:$fire-two:localhost"
    assert first_target.session_id != second_target.session_id


@pytest.mark.asyncio
async def test_concurrent_scheduled_fires_use_distinct_coalescing_threads(tmp_path: Path) -> None:
    """Concurrent per-fire deliveries receive distinct keys before entering the gate."""
    config = _single_agent_config(tmp_path, "room")
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    first_event = _scheduled_fire_event(
        config,
        extra_content={},
        event_id="$fire-one:localhost",
        new_thread=True,
    )
    second_event = _scheduled_fire_event(
        config,
        extra_content={},
        event_id="$fire-two:localhost",
        new_thread=True,
    )

    first_thread_id, second_thread_id = await asyncio.gather(
        harness.ingress.deps.resolver.coalescing_thread_id(room, first_event),
        harness.ingress.deps.resolver.coalescing_thread_id(room, second_event),
    )

    assert first_thread_id == first_event.event_id
    assert second_thread_id == second_event.event_id
    assert first_thread_id != second_thread_id


@pytest.mark.asyncio
async def test_scheduled_fire_into_persisted_thread_keeps_thread_session(config: Config, tmp_path: Path) -> None:
    """A scheduled fire delivered into its persisted thread keeps that thread's session."""
    persisted_thread_history = thread_history_result(
        [
            make_visible_message(
                sender=_SENDER,
                body="original schedule request",
                event_id=_THREAD_ROOT,
                timestamp=500_000,
            ),
        ],
        is_full_history=True,
    )
    harness = _build_harness(config, tmp_path, thread_history=persisted_thread_history)
    room = _room_with_members(config, "general")
    event = _scheduled_fire_event(
        config,
        extra_content={"m.relates_to": {"rel_type": "m.thread", "event_id": _THREAD_ROOT}},
        event_id="$scheduled-in-thread:localhost",
    )

    await harness.deliver(room, event)

    assert len(harness.runner.requests) == 1
    target = harness.runner.requests[0].response_envelope.target
    assert target.resolved_thread_id == _THREAD_ROOT
    assert target.session_id == f"{_ROOM_ID}:{_THREAD_ROOT}"


@pytest.mark.asyncio
async def test_silent_schedule_trigger_in_existing_thread_keeps_thread_session(tmp_path: Path) -> None:
    """A relation-bearing silent trigger must not be demoted to room scope."""
    config = _single_agent_config(tmp_path, "thread")
    thread_history = thread_history_result(
        [
            make_visible_message(
                sender=_SENDER,
                body="original schedule request",
                event_id=_THREAD_ROOT,
                timestamp=500_000,
            ),
        ],
        is_full_history=True,
    )
    harness = _build_harness(config, tmp_path, thread_history=thread_history)
    room = _room_with_members(config, "general")
    event = _silent_schedule_event(
        config,
        extra_content={"m.relates_to": {"rel_type": "m.thread", "event_id": _THREAD_ROOT}},
    )

    await harness.deliver(room, event)

    assert len(harness.runner.requests) == 1
    envelope = harness.runner.requests[0].response_envelope
    assert envelope.source_kind == SILENT_SCHEDULE_SOURCE_KIND
    assert envelope.target.source_thread_id == _THREAD_ROOT
    assert envelope.target.resolved_thread_id == _THREAD_ROOT
    assert envelope.target.session_id == f"{_ROOM_ID}:{_THREAD_ROOT}"


@pytest.mark.asyncio
async def test_room_level_silent_schedule_trigger_never_replies_to_hidden_source(tmp_path: Path) -> None:
    """A relation-free hidden trigger must select the room without rooting a thread or reply."""
    config = _single_agent_config(tmp_path, "thread")
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _silent_schedule_event(config, extra_content={constants.SCHEDULED_MODEL_KEY: "cheap"})

    await harness.deliver(room, event)

    assert len(harness.runner.requests) == 1
    assert harness.runner.requests[0].scheduled_model == "cheap"
    envelope = harness.runner.requests[0].response_envelope
    assert envelope.source_kind == SILENT_SCHEDULE_SOURCE_KIND
    assert envelope.target.source_thread_id is None
    assert envelope.target.resolved_thread_id is None
    assert envelope.target.reply_to_event_id is None
    assert envelope.target.is_room_mode is True
    assert envelope.target.session_id == _ROOM_ID


@pytest.mark.asyncio
async def test_eventless_silent_schedule_completion_is_terminal_before_recovery(tmp_path: Path) -> None:
    """An admitted successful no-response turn cannot replay after cleanup."""
    config = _single_agent_config(tmp_path, "room")
    harness = _build_harness(config, tmp_path)
    harness.runner.complete_without_response = True
    room = _room_with_members(config, "general")
    event = _silent_schedule_trigger_event(config)
    obligation_runner = _obligation_runner(
        harness,
        tracking_path=tmp_path / "dispatch-tracking",
        matrix_user_id=_entity_user_id(config, "general"),
        entity_name="general",
        room=room,
    )
    _replace_harness_deps(
        harness,
        visible_responses=VisibleResponseReconciler(
            replace(
                harness.dispatcher.deps.visible_responses.deps,
                settle_ignored_sources=obligation_runner.settle_intentionally_ignored_turn_sources,
            ),
        ),
    )

    await admit_dispatch_event(obligation_runner, room, event, EventKind.SCHEDULE_TRIGGER, EventClass.ACTIONABLE)
    await obligation_runner.drain_once()
    await harness.gate.drain_all()
    await harness.runner.settle_inbox_responses()

    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id)
    assert not await obligation_runner.store.is_pending(event.event_id)
    await obligation_runner.drain_once()
    assert len(harness.runner.requests) == 1


@pytest.mark.asyncio
async def test_user_message_cannot_spoof_silent_schedule_room_placement(tmp_path: Path) -> None:
    """Untrusted source metadata must not suppress a normal user-message reply."""
    config = _single_agent_config(tmp_path, "thread")
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "pretend this is hidden",
                "msgtype": "m.text",
                constants.SOURCE_KIND_KEY: SILENT_SCHEDULE_SOURCE_KIND,
            },
            "event_id": _EVENT_ID,
            "sender": _SENDER,
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )

    await harness.deliver(room, event)

    assert len(harness.runner.requests) == 1
    envelope = harness.runner.requests[0].response_envelope
    assert envelope.source_kind != SILENT_SCHEDULE_SOURCE_KIND
    assert envelope.target.resolved_thread_id == event.event_id
    assert envelope.target.reply_to_event_id == event.event_id


@pytest.mark.asyncio
async def test_room_mode_plain_user_message_keeps_room_session(tmp_path: Path) -> None:
    """Non-scheduled room-level messages keep the shared room session in room mode."""
    config = _single_agent_config(tmp_path, "room")
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _text_event("hello there")

    await harness.deliver(room, event)

    assert len(harness.runner.requests) == 1
    target = harness.runner.requests[0].response_envelope.target
    assert target.resolved_thread_id is None
    assert target.session_id == _ROOM_ID


@pytest.mark.asyncio
async def test_handled_thread_alone_does_not_prove_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durably handled turn is still not evidence that shutdown recovery is proven."""
    config = _single_agent_config(tmp_path, "thread")
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _text_event("hello there")
    response_started = asyncio.Event()
    release_response = asyncio.Event()
    generate_response = harness.runner.generate_response

    async def generate_with_barrier(request: ResponseRequest) -> SourceFate:
        response_started.set()
        await release_response.wait()
        return await generate_response(request)

    monkeypatch.setattr(harness.runner, "generate_response", generate_with_barrier)
    delivery = asyncio.create_task(harness.deliver(room, event))
    await response_started.wait()

    release_response.set()

    await delivery

    assert harness.runner.requests[0].response_envelope.target.resolved_thread_id == event.event_id
    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id)
    assert await harness.runner.recovery_proof_checks[0]() is False


@pytest.mark.asyncio
async def test_response_recovery_proof_uses_durable_owner_callback(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inbox owner asks the durable journal/outbox boundary after it exits."""
    harness = _build_harness(config, tmp_path)
    durable_recovery_ready = AsyncMock(return_value=True)
    monkeypatch.setattr(harness.turn_store, "response_recovery_ready", durable_recovery_ready)
    event = _text_event("recover me after process shutdown")

    await harness.deliver(_room_with_members(config, "general"), event)

    recovery_check = harness.runner.recovery_proof_checks[0]
    ready = recovery_check()
    assert inspect.isawaitable(ready)
    assert await ready is True
    durable_recovery_ready.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_shutdown_recovery_requires_exact_journal_or_outbox_owner(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal response is safe only while one durable owner still owes it."""
    harness = _build_harness(config, tmp_path)
    store = harness.turn_store
    diagnostics = MagicMock()
    monkeypatch.setattr("mindroom.turn_store.logger", diagnostics)

    journal_source_id = "$journal-owned:localhost"
    journal_turn = _in_room(TurnRecord.create([journal_source_id]))
    await harness.turn_store.record_pending_turn(journal_turn)
    await harness.journal_principal.admit(
        InboundEvent(
            event_id=journal_source_id,
            room_id=_ROOM_ID,
            thread_id=journal_source_id,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender=_SENDER,
            origin_server_ts=1_000,
            source={"event_id": journal_source_id, "content": {"msgtype": "m.text", "body": "journal"}},
        ),
    )
    journal_claim = await harness.turn_store.claim(journal_turn)
    assert isinstance(journal_claim, TurnClaim)
    assert await store.response_recovery_ready(journal_turn) is False
    journal_claim.release()
    assert await store.response_recovery_ready(journal_turn) is True

    await harness.journal_principal.settle(journal_source_id)
    assert await store.response_recovery_ready(journal_turn) is False
    assert await store.response_recovery_ready(replace(journal_turn, anchor_event_id=None)) is False

    mixed_source_ids = ("$mixed-pending:localhost", "$mixed-settled:localhost")
    mixed_turn = _in_room(TurnRecord.create(mixed_source_ids))
    await harness.turn_store.record_pending_turn(mixed_turn)
    for index, source_id in enumerate(mixed_source_ids):
        await harness.journal_principal.admit(
            InboundEvent(
                event_id=source_id,
                room_id=_ROOM_ID,
                thread_id=mixed_source_ids[0],
                kind=EventKind.MESSAGE,
                event_class=EventClass.ACTIONABLE,
                sender=_SENDER,
                origin_server_ts=3_000 + index,
                source={"event_id": source_id, "content": {"msgtype": "m.text", "body": "mixed"}},
            ),
        )
    await harness.journal_principal.settle(mixed_source_ids[1])
    assert await store.response_recovery_ready(mixed_turn) is False

    outbox_source_id = "$outbox-owned:localhost"
    outbox_pending = await harness.turn_store.record_pending_turn(_in_room(TurnRecord.create([outbox_source_id])))
    assert outbox_pending is not None
    outbox_turn = outbox_pending.record
    outbox_turn_id = outbox_turn.anchor_event_id
    assert outbox_turn_id is not None
    assert outbox_turn.turn_key is not None
    await harness.journal_principal.admit(
        InboundEvent(
            event_id=outbox_source_id,
            room_id=_ROOM_ID,
            thread_id=outbox_source_id,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender=_SENDER,
            origin_server_ts=2_000,
            source={"event_id": outbox_source_id, "content": {"msgtype": "m.text", "body": "outbox"}},
        ),
    )
    assert (
        await harness.journal_principal.enqueue_final_answer(
            delivery_id=outbox_turn_id,
            room_id=_ROOM_ID,
            thread_id=outbox_source_id,
            payload={"msgtype": "m.text", "body": "recoverable answer"},
            turn_key=outbox_turn.turn_key,
        )
        is not None
    )
    assert await store.response_recovery_ready(outbox_turn) is True
    await harness.journal_principal.claim_matrix_delivery(
        delivery_id=outbox_turn_id,
        stage=DeliveryStage.FINAL,
    )
    await harness.journal_principal.acknowledge_matrix_delivery(
        delivery_id=outbox_turn_id,
        stage=DeliveryStage.FINAL,
        event_id="$acknowledged:localhost",
        delivered_projections=(),
    )
    assert await store.response_recovery_ready(outbox_turn) is False
    await harness.turn_store.record_responded_turn(
        replace(
            outbox_turn,
            completed=True,
            response_event_id="$different:localhost",
        ),
    )
    assert await store.response_recovery_ready(outbox_turn) is False
    diagnostic_calls = [
        call for call in diagnostics.info.call_args_list if call.args == ("response_recovery_proof_not_ready",)
    ]
    assert [call.kwargs["reason"] for call in diagnostic_calls] == [
        "live_turn_claim",
        "missing_final_delivery",
        "missing_turn_anchor",
        "mixed_source_pending",
        "terminal_turn_mismatch",
    ]
    terminal_mismatch = diagnostic_calls[-1]
    assert terminal_mismatch.kwargs == {
        "agent": "general",
        "reason": "terminal_turn_mismatch",
        "source_event_ids": ("$outbox-owned:localhost",),
        "anchor_event_id": "$outbox-owned:localhost",
        "source_count": 1,
        "pending_source_count": 0,
        "missing_completed_turn_count": 0,
        "incomplete_completed_turn_count": 1,
        "source_identity_mismatch_count": 0,
        "anchor_mismatch_count": 0,
        "response_event_mismatch_count": 1,
    }
    assert all(
        set(call.kwargs)
        <= {
            "agent",
            "reason",
            "source_event_ids",
            "anchor_event_id",
            "source_count",
            "pending_source_count",
            "missing_completed_turn_count",
            "incomplete_completed_turn_count",
            "source_identity_mismatch_count",
            "anchor_mismatch_count",
            "response_event_mismatch_count",
        }
        for call in diagnostic_calls
    )


@pytest.mark.asyncio
async def test_room_mode_schedule_without_new_thread_keeps_room_session(tmp_path: Path) -> None:
    """A relation-free room-scope schedule keeps the shared room session."""
    config = _single_agent_config(tmp_path, "room")
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")

    await harness.deliver(room, _scheduled_fire_event(config, extra_content={}))

    assert len(harness.runner.requests) == 1
    target = harness.runner.requests[0].response_envelope.target
    assert target.resolved_thread_id is None
    assert target.session_id == _ROOM_ID


@pytest.mark.asyncio
async def test_user_message_cannot_spoof_scheduled_thread_promotion(tmp_path: Path) -> None:
    """A scheduled marker on an untrusted user message must not force a per-fire thread."""
    config = _single_agent_config(tmp_path, "room")
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "pretend to be a scheduled fire",
                "msgtype": "m.text",
                constants.SOURCE_KIND_KEY: SCHEDULED_SOURCE_KIND,
                constants.PER_FIRE_THREAD_ROOT_KEY: True,
                constants.PER_FIRE_THREAD_ROOT_EVENT_ID_KEY: "$spoofed-root:localhost",
            },
            "event_id": _EVENT_ID,
            "sender": _SENDER,
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )

    await harness.deliver(room, event)

    assert len(harness.runner.requests) == 1
    target = harness.runner.requests[0].response_envelope.target
    assert target.resolved_thread_id is None
    assert target.session_id == _ROOM_ID


@pytest.mark.asyncio
async def test_deferred_sync_restart_records_handled_outcome_before_rethrow(
    config: Config,
    tmp_path: Path,
) -> None:
    """A turn its reply recorded answered stays handled when the interruption rethrows."""
    harness = _build_harness(config, tmp_path)
    harness.runner.deferred_sync_restart_error = asyncio.CancelledError("sync_restart")
    room = _room_with_members(config, "general")
    event = _text_event("please survive the sync restart")

    with pytest.raises(asyncio.CancelledError, match="sync_restart"):
        await harness.deliver(room, event)

    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id) is True


@pytest.mark.asyncio
async def test_interrupted_router_relay_leaves_its_original_to_the_turn_that_answered_it(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An original answered independently while its relay ran reads its own answer once the relay finishes."""
    harness = _build_harness(config, tmp_path)
    harness.runner.deferred_sync_restart_error = asyncio.CancelledError("sync_restart")
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    human_event_id = "$human:localhost"
    relay = _router_relay_event(
        config,
        event_id="$relay:localhost",
        original_event_id=human_event_id,
        body=f"{_entity_user_id(config, 'general')} respond",
        origin_server_ts=1_000_000,
    )
    response_started = asyncio.Event()
    release_response = asyncio.Event()
    generate_response = harness.runner.generate_response

    async def generate_with_barrier(request: ResponseRequest) -> SourceFate:
        response_started.set()
        await release_response.wait()
        return await generate_response(request)

    monkeypatch.setattr(harness.runner, "generate_response", generate_with_barrier)
    delivery = asyncio.create_task(harness.deliver(room, relay))
    await response_started.wait()
    await harness.turn_store.record_turn(
        TurnRecord.create([human_event_id], response_event_id="$original-response:localhost"),
        room_id=_ROOM_ID,
    )
    release_response.set()
    with pytest.raises(asyncio.CancelledError, match="sync_restart"):
        await delivery

    relay_record = await harness.turn_store.get_turn_record(relay.event_id)
    assert relay_record is not None
    assert relay_record.completed is True
    human_record = await harness.turn_store.get_turn_record(human_event_id)
    assert human_record is not None
    assert human_record.source_event_ids == (human_event_id,)
    assert human_record.response_event_id == "$original-response:localhost"


@pytest.mark.asyncio
async def test_pre_lock_replacement_refusal_poisons_coalescing_drain(config: Config, tmp_path: Path) -> None:
    """A refused source callback must invalidate checkpoint certification."""
    harness = _build_harness(config, tmp_path)
    harness.runner.pre_lock_error = ResponseAdmissionRefusedError()
    room = _room_with_members(config, "general")
    event = _text_event("please survive replacement")

    await harness.ingress.handle_text_event(room, event)
    drain_result = await harness.gate.drain_all()
    await asyncio.gather(*harness.runner.inbox_tasks, return_exceptions=True)

    assert drain_result.completed is False
    assert drain_result.dispatch_failure_count == 1
    record = await harness.turn_store.get_turn_record(event.event_id)
    assert record is not None
    assert record.completed is False


@pytest.mark.asyncio
async def test_process_shutdown_refuses_text_before_pending_turn_persistence(
    config: Config,
    tmp_path: Path,
) -> None:
    """A process-fenced response stays journal-pending without creating a turn owner."""
    harness = _build_harness(config, tmp_path)
    harness.runtime.begin_process_shutdown()
    room = _room_with_members(config, "general")
    event = _text_event("retry me after process restart")

    await harness.ingress.handle_text_event(room, event)
    drain_result = await harness.gate.drain_all()

    assert drain_result.completed is False
    assert drain_result.dispatch_failure_count == 1
    assert drain_result.admission_deferred_count == 0
    assert harness.retried_dispatch_sources == [(event.event_id,)]
    assert await harness.turn_store.get_turn_record(event.event_id) is None
    assert harness.runner.inbox_tasks == []


@pytest.mark.asyncio
async def test_orderly_shutdown_defers_fenced_coalescing_source_without_failed_drain(
    config: Config,
    tmp_path: Path,
) -> None:
    """An exact process-fence refusal is a durable handoff, not a failed drain."""
    harness = _build_harness(config, tmp_path)
    harness.runtime.begin_process_shutdown()
    harness.gate._is_shutting_down = lambda: True
    room = _room_with_members(config, "general")
    event = _text_event("retry me after orderly process restart")

    await harness.ingress.handle_text_event(room, event)
    drain_result = await harness.gate.drain_all(
        shutdown_intent=ORDERLY_SHUTDOWN,
    )

    assert drain_result.completed is True
    assert drain_result.dispatch_failure_count == 0
    assert drain_result.admission_deferred_count == 1
    assert harness.retried_dispatch_sources == [(event.event_id,)]
    assert await harness.turn_store.get_turn_record(event.event_id) is None
    assert harness.runner.inbox_tasks == []
    assert harness.gateway.sent == []


@pytest.mark.asyncio
async def test_orderly_shutdown_keeps_admission_refusal_subclass_as_failed_drain(
    config: Config,
    tmp_path: Path,
) -> None:
    """Only the exact pre-owner process-fence refusal is a durable handoff."""

    class RelatedAdmissionError(ResponseAdmissionRefusedError):
        pass

    harness = _build_harness(config, tmp_path)
    harness.runner.pre_lock_error = RelatedAdmissionError()
    room = _room_with_members(config, "general")
    event = _text_event("do not reinterpret related admission failures")

    await harness.ingress.handle_text_event(room, event)
    drain_result = await harness.gate.drain_all(
        shutdown_intent=ORDERLY_SHUTDOWN,
    )
    await asyncio.gather(*harness.runner.inbox_tasks, return_exceptions=True)

    assert drain_result.completed is False
    assert drain_result.dispatch_failure_count == 1
    assert drain_result.admission_deferred_count == 0


@pytest.mark.asyncio
async def test_process_shutdown_race_refuses_owner_without_leaking_response_coroutine(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recwarn: pytest.WarningsRecorder,
) -> None:
    """Shutdown during the pending write must not leak the unstarted response."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _text_event("fence me while my pending turn commits")
    record_pending_turn = harness.turn_store.record_pending_turn

    async def record_then_fence(turn: TurnRecord) -> turn_rows.PendingTurn | None:
        pending_turn = await record_pending_turn(turn)
        harness.runtime.begin_process_shutdown()
        return pending_turn

    monkeypatch.setattr(harness.turn_store, "record_pending_turn", record_then_fence)

    await harness.ingress.handle_text_event(room, event)
    drain_result = await harness.gate.drain_all()

    assert drain_result.completed is False
    assert drain_result.dispatch_failure_count == 1
    record = await harness.turn_store.get_turn_record(event.event_id)
    assert record is not None
    assert record.completed is False
    assert harness.runner.inbox_tasks == []
    assert not [warning for warning in recwarn if issubclass(warning.category, RuntimeWarning)]


@pytest.mark.asyncio
async def test_never_started_runner_task_releases_transferred_turn_claim(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task cancelled before its first step must release dispatch ownership."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _text_event("cancel my owner before the response starts")
    track_response = harness.runner.track_inbox_response

    def track_then_cancel(
        response: Coroutine[Any, Any, None],
        **kwargs: object,
    ) -> asyncio.Task[None]:
        task = track_response(response, **kwargs)
        task.cancel()
        return task

    monkeypatch.setattr(harness.runner, "track_inbox_response", track_then_cancel)

    await harness.ingress.handle_text_event(room, event)
    await harness.gate.drain_all()
    await asyncio.gather(*harness.runner.inbox_tasks, return_exceptions=True)

    assert harness.runner.claims
    assert harness.turn_store.has_live_turn_claim(event.event_id) is False


@pytest.mark.asyncio
async def test_command_turn_records_terminal_outcome_through_turn_store(config: Config, tmp_path: Path) -> None:
    """A ``!command`` turn executes on the router and records a terminal TurnStore outcome.

    Command-shaped human text is a control input: it dispatches directly at
    ingress and must never enter the coalescing gate.
    """
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    room = _room_with_members(config, ROUTER_AGENT_NAME, "general", "research")
    event = _text_event("!help", event_id="$command:localhost")

    await harness.deliver(room, event)

    # Commands bypass turn-policy planning, AI execution, and the coalescing gate.
    assert harness.policy.plan_turn_calls == 0
    assert harness.runner.requests == []
    assert harness.gate_batches == []
    assert len(harness.gateway.sent) == 1
    assert "command" in harness.gateway.sent[0].response_text.lower()
    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id) is True

    record = await harness.turn_store.get_turn_record(event.event_id)
    assert record is not None
    assert record.response_event_id == "$sent-1:localhost"


@pytest.mark.asyncio
async def test_command_replay_adopts_durable_response(config: Config, tmp_path: Path) -> None:
    """A command replay must reuse the response persisted before terminal settlement."""
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    room = _room_with_members(config, ROUTER_AGENT_NAME, "general", "research")
    event = _text_event("!help", event_id="$command-replay:localhost")
    command = command_parser.parse(event.body)
    assert command is not None
    target = harness.ingress.deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id=None,
        reply_to_event_id=event.event_id,
        event_source=event.source,
    )
    handled_turn = TurnRecord.create([event.event_id])

    with patch.object(
        harness.turn_store,
        "record_placeholder",
        wraps=harness.turn_store.record_placeholder,
    ) as record_visible_response:
        with patch.object(harness.turn_store, "record_responded_turn"):
            await harness.dispatcher.deps.command_executor.execute(
                room,
                event,
                _SENDER,
                command,
                target=target,
                handled_turn=handled_turn,
                acts_for_requester=False,
            )

        pending_turn = await harness.turn_store.get_turn_record(event.event_id)
        assert pending_turn is not None
        assert pending_turn.completed is False
        assert pending_turn.response_event_id == "$sent-1:localhost"

        await harness.dispatcher.deps.command_executor.execute(
            room,
            event,
            _SENDER,
            command,
            target=target,
            handled_turn=handled_turn,
            acts_for_requester=False,
        )

    assert record_visible_response.await_count == 2

    assert len(harness.gateway.sent) == 1
    record = await harness.turn_store.get_turn_record(event.event_id)
    assert record is not None
    assert record.completed is True
    assert record.response_event_id == "$sent-1:localhost"


@pytest.mark.asyncio
async def test_config_command_replay_recovers_confirmation_before_terminal_settlement(
    config: Config,
    tmp_path: Path,
) -> None:
    """A visible config preview is terminal only after its confirmation state is recovered."""
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    recoveries: list[tuple[str, str]] = []

    async def recover_setup(room_id: str, preview_event_id: str) -> bool:
        recoveries.append((room_id, preview_event_id))
        return True

    _replace_harness_deps(
        harness,
        command_executor=CommandTurnExecutor(
            replace(
                harness.dispatcher.deps.command_executor.deps,
                recover_config_confirmation_setup=recover_setup,
            ),
        ),
    )
    command_turn = TurnRecord.create(["$config-command"])

    recovered = await harness.dispatcher.deps.command_executor._recover_visible_response(
        room_id=_ROOM_ID,
        command_type=CommandType.CONFIG,
        command_turn=command_turn,
        response_event_id="$preview",
    )

    assert recovered is True
    assert recoveries == [(_ROOM_ID, "$preview")]
    assert await harness.turn_store.is_handled(_ROOM_ID, "$config-command")


@pytest.mark.asyncio
async def test_command_replay_does_not_repeat_mutation_after_visible_response(config: Config, tmp_path: Path) -> None:
    """A recovered command response must settle without rerunning its handler."""
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    room = _room_with_members(config, ROUTER_AGENT_NAME, "general")
    event = _text_event("!model default", event_id="$model-command-replay:localhost")
    command = command_parser.parse(event.body)
    assert command is not None
    target = harness.ingress.deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id=None,
        reply_to_event_id=event.event_id,
        event_source=event.source,
    )
    handled_turn = TurnRecord.create([event.event_id])

    with (
        patch("mindroom.commands.handler.handle_model_command", return_value="✅ Model changed") as mutate,
        patch.object(harness.turn_store, "record_responded_turn"),
    ):
        await harness.dispatcher.deps.command_executor.execute(
            room,
            event,
            _SENDER,
            command,
            target=target,
            handled_turn=handled_turn,
            acts_for_requester=False,
        )

    await harness.dispatcher.deps.command_executor.execute(
        room,
        event,
        _SENDER,
        command,
        target=target,
        handled_turn=handled_turn,
        acts_for_requester=False,
    )

    mutate.assert_called_once()
    assert len(harness.gateway.sent) == 1


@pytest.mark.asyncio
async def test_command_send_failure_keeps_turn_pending(config: Config, tmp_path: Path) -> None:
    """A command with no visible Matrix event must remain retry-owned."""
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    room = _room_with_members(config, ROUTER_AGENT_NAME, "general")
    event = _text_event("!help", event_id="$command-send-failure:localhost")
    command = command_parser.parse(event.body)
    assert command is not None
    target = harness.ingress.deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id=None,
        reply_to_event_id=event.event_id,
        event_source=event.source,
    )

    async def fail_send(_request: SendTextRequest) -> None:
        return None

    with (
        patch.object(harness.gateway, "send_text", new=fail_send),
        pytest.raises(RuntimeError, match="did not return a Matrix event ID"),
    ):
        await harness.dispatcher.deps.command_executor.execute(
            room,
            event,
            _SENDER,
            command,
            target=target,
            handled_turn=TurnRecord.create([event.event_id]),
            acts_for_requester=False,
        )

    record = await harness.turn_store.get_turn_record(event.event_id)
    assert record is not None
    assert record.completed is False
    assert record.command_result_text is None


@pytest.mark.asyncio
async def test_mutating_command_retries_checkpointed_result_without_reapplying(config: Config, tmp_path: Path) -> None:
    """A failed response send must replay the exact result, not repeat its mutation."""
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    room = _room_with_members(config, ROUTER_AGENT_NAME, "general")
    event = _text_event("!model default", event_id="$model-send-failure:localhost")
    command = command_parser.parse(event.body)
    assert command is not None
    target = harness.ingress.deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id=None,
        reply_to_event_id=event.event_id,
        event_source=event.source,
    )

    async def fail_send(_request: SendTextRequest) -> None:
        return None

    with patch("mindroom.commands.handler.handle_model_command", return_value="✅ Model changed") as mutate:
        with (
            patch.object(harness.gateway, "send_text", new=fail_send),
            pytest.raises(RuntimeError, match="did not return a Matrix event ID"),
        ):
            await harness.dispatcher.deps.command_executor.execute(
                room,
                event,
                _SENDER,
                command,
                target=target,
                handled_turn=TurnRecord.create([event.event_id]),
                acts_for_requester=False,
            )

        pending_turn = await harness.turn_store.get_turn_record(event.event_id)
        assert pending_turn is not None
        assert pending_turn.command_execution_started
        assert pending_turn.command_result_text == "✅ Model changed"

        await harness.dispatcher.deps.command_executor.execute(
            room,
            event,
            _SENDER,
            command,
            target=target,
            handled_turn=pending_turn,
            acts_for_requester=False,
        )

    mutate.assert_called_once()
    assert harness.gateway.sent[0].response_text == "✅ Model changed"
    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id)


@pytest.mark.asyncio
async def test_failed_mutating_command_execution_requires_manual_retry(config: Config, tmp_path: Path) -> None:
    """A failed admitted attempt must report uncertainty instead of rerunning automatically."""
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    room = _room_with_members(config, ROUTER_AGENT_NAME, "general")
    event = _text_event("!model default", event_id="$model-ambiguous:localhost")
    command = command_parser.parse(event.body)
    assert command is not None
    target = harness.ingress.deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id=None,
        reply_to_event_id=event.event_id,
        event_source=event.source,
    )
    with (
        patch(
            "mindroom.commands.handler.handle_model_command",
            side_effect=RuntimeError("transient failure"),
        ) as attempt,
        pytest.raises(RuntimeError, match="transient failure"),
    ):
        await harness.dispatcher.deps.command_executor.execute(
            room,
            event,
            _SENDER,
            command,
            target=target,
            handled_turn=TurnRecord.create([event.event_id]),
            acts_for_requester=False,
        )

    pending_turn = await harness.turn_store.get_turn_record(event.event_id)
    assert pending_turn is not None
    assert pending_turn.command_execution_started
    assert pending_turn.command_result_text is None

    with patch("mindroom.commands.handler.handle_model_command") as replay:
        await harness.dispatcher.deps.command_executor.execute(
            room,
            event,
            _SENDER,
            command,
            target=target,
            handled_turn=pending_turn,
            acts_for_requester=False,
        )

    attempt.assert_called_once()
    replay.assert_not_called()
    assert "outcome is uncertain" in harness.gateway.sent[0].response_text
    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id)


@pytest.mark.asyncio
async def test_rejection_replay_adopts_durable_response(config: Config, tmp_path: Path) -> None:
    """A rejected-turn replay must not send a second visible rejection."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _text_event("unsupported request", event_id="$reject-replay:localhost")
    target = MessageTarget.resolve(room.room_id, None, event.event_id)
    envelope = harness.ingress.deps.resolver.build_envelope(
        event=event,
        facts=make_ingress_facts(event.sender, requester_user_id=_SENDER),
        target=target,
    )
    dispatch = PreparedDispatch(
        requester_user_id=_SENDER,
        context=MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=(),
            mentioned_agents=[],
            has_non_agent_mentions=False,
        ),
        target=target,
        correlation_id=event.event_id,
        envelope=envelope,
    )
    action = ResponseAction(kind="reject", rejection_message="Unsupported request")
    handled_turn = harness.turn_store.attach_response_context(
        TurnRecord.create([event.event_id], completed=False),
        history_scope=None,
        conversation_target=target,
    )
    pending = await harness.turn_store.record_pending_turn(handled_turn)
    assert pending is not None

    with patch.object(harness.turn_store, "record_responded_turn"):
        await harness.dispatcher._execute_response_action(
            room,
            event,
            dispatch,
            action,
            DispatchPayloadInputs((), (), ()),
            dispatch_started_at=0.0,
            handled_turn=pending.record,
        )

    pending_turn = await harness.turn_store.get_turn_record(event.event_id)
    assert pending_turn is not None
    assert pending_turn.completed is False
    assert pending_turn.response_event_id == "$sent-1:localhost"

    await harness.dispatcher._execute_response_action(
        room,
        event,
        dispatch,
        action,
        DispatchPayloadInputs((), (), ()),
        dispatch_started_at=0.0,
        handled_turn=pending_turn,
        reconcile_visible_response=True,
    )

    assert len(harness.gateway.sent) == 1
    record = await harness.turn_store.get_turn_record(event.event_id)
    assert record is not None
    assert record.completed is True
    assert record.response_event_id == "$sent-1:localhost"


@pytest.mark.asyncio
async def test_router_relay_replay_adopts_durable_response(config: Config, tmp_path: Path) -> None:
    """A router replay must reuse the relay persisted before terminal settlement."""
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    room = _room_with_members(config, ROUTER_AGENT_NAME, "general")
    event = _text_event("route this", event_id="$router-replay:localhost")
    handled_turn = TurnRecord.create([event.event_id], completed=False)

    with patch.object(harness.turn_store, "record_responded_turn"):
        await execute_router_relay(
            harness.dispatcher.deps,
            room,
            event,
            (),
            requester_user_id=_SENDER,
            handled_turn=handled_turn,
        )

    pending_turn = await harness.turn_store.get_turn_record(event.event_id)
    assert pending_turn is not None
    assert pending_turn.completed is False
    assert pending_turn.response_event_id == "$sent-1:localhost"

    await execute_router_relay(
        harness.dispatcher.deps,
        room,
        event,
        (),
        requester_user_id=_SENDER,
        handled_turn=handled_turn,
    )

    assert len(harness.gateway.sent) == 1
    record = await harness.turn_store.get_turn_record(event.event_id)
    assert record is not None
    assert record.completed is True
    assert record.response_event_id == "$sent-1:localhost"


@pytest.mark.asyncio
async def test_command_dispatch_failure_propagates_without_recording_terminal_outcome(
    config: Config,
    tmp_path: Path,
) -> None:
    """A failed owner command must retain only retryable pending intent."""
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    room = _room_with_members(config, ROUTER_AGENT_NAME, "general", "research")
    event = _text_event("!help", event_id="$failed-command:localhost")

    with (
        patch("mindroom.command_turn_executor.handle_command", side_effect=RuntimeError("command failed")),
        pytest.raises(RuntimeError, match="command failed"),
    ):
        await harness.deliver(room, event)

    record = await harness.turn_store.get_turn_record(event.event_id)
    assert record is not None
    assert record.completed is False
    assert record.response_event_id is None


@pytest.mark.asyncio
async def test_non_router_agent_consumes_command_without_responding(config: Config, tmp_path: Path) -> None:
    """Plain agents stay silent on command turns; the router owns command replies."""
    harness = _build_harness(config, tmp_path, agent_name="general")
    room = _room_with_members(config, "general")
    event = _text_event("!help", event_id="$command:localhost")

    await harness.deliver(room, event)

    assert harness.policy.plan_turn_calls == 0
    assert harness.runner.requests == []
    assert harness.gateway.sent == []
    # Commands are control inputs: even a silently consumed one never enters the gate.
    assert harness.gate_batches == []
    # Exact callback truth stays compact when only the router owns the visible reply.
    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id) is False
    assert harness.ignored_dispatch_sources == [(event.event_id,)]


@pytest.mark.asyncio
async def test_normal_desktop_agent_owns_desktop_command(tmp_path: Path) -> None:
    """A requester can pair Desktop directly in a normal agent's private room."""
    config = bind_runtime_paths(
        Config(
            agents={
                "computer": AgentConfig(
                    display_name="Computer",
                    tools=["desktop"],
                ),
            },
        ),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path, agent_name="computer")
    room = _room_with_members(config, "computer")
    room.members_synced = True
    event = _text_event("!desktop status", event_id="$desktop-command:localhost")

    await harness.deliver(room, event)

    assert harness.policy.plan_turn_calls == 0
    assert harness.runner.requests == []
    assert harness.gate_batches == []
    assert len(harness.gateway.sent) == 1
    assert "Desktop setup is required" in harness.gateway.sent[0].response_text
    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id) is True


@pytest.mark.asyncio
async def test_same_turn_after_restart_produces_exactly_one_response(config: Config, tmp_path: Path) -> None:
    """Replaying one handled turn through fresh turn owners never responds twice.

    The second harness shares only the stored turn rows, simulating a process
    restart that re-delivers the same Matrix event. It opens its own database
    handle, so nothing is inherited in memory and the whole guard rests on the
    restarted store reading the rows the first run wrote.
    """
    room = _room_with_members(config, "general")
    event = _text_event("respond exactly once")

    first_run = _build_harness(config, tmp_path)
    await first_run.turn_store.warm()
    await first_run.deliver(room, event)
    assert len(first_run.runner.requests) == 1
    assert await first_run.turn_store.is_handled(_ROOM_ID, event.event_id) is True

    restarted_run = _build_harness(config, tmp_path)
    await restarted_run.turn_store.warm()
    await restarted_run.deliver(room, _text_event("respond exactly once"))

    assert restarted_run.runner.requests == []
    assert restarted_run.gateway.sent == []
    assert restarted_run.policy.plan_turn_calls == 0
    assert await restarted_run.turn_store.is_handled(_ROOM_ID, event.event_id) is True


@pytest.mark.asyncio
async def test_interactive_selection_acks_generates_and_records_once(config: Config, tmp_path: Path) -> None:
    """The interactive selection path acks, runs generation, and records the turn."""
    harness = _build_harness(config, tmp_path)
    room = nio.MatrixRoom(_ROOM_ID, _entity_user_id(config, "general"))
    selection = interactive.InteractiveSelection(
        question_event_id="$question:localhost",
        question_text="Which role should I use?",
        selection_key="🧭",
        selected_label="Department head",
        selected_value="department_head",
        thread_id="$thread-root:localhost",
    )

    await harness.selections._handle_interactive_selection(
        room,
        selection=selection,
        facts=_SELECTION_FACTS,
        source_event_id="$selection:localhost",
        response_target=harness.selections._interactive_selection_target(room.room_id, selection),
    )

    assert len(harness.gateway.sent) == 1
    ack_request = harness.gateway.sent[0]
    assert ack_request.response_text.startswith("You selected: 🧭 Department head\n")
    assert ack_request.target.resolved_thread_id == selection.thread_id
    assert ack_request.target.reply_to_event_id == selection.question_event_id
    assert ack_request.reply_write is not None
    assert ack_request.reply_write.span.delivery_id == "$selection:localhost"

    assert len(harness.runner.requests) == 1
    request = harness.runner.requests[0]
    assert request.prompt == interactive.build_selection_prompt(selection)
    assert request.existing_event_id == "$sent-1:localhost"
    assert request.response_envelope.target.reply_to_event_id == selection.question_event_id
    assert request.response_envelope.target.resolved_thread_id == selection.thread_id
    # The selection queued as a barrier, but its answer replies to a message.
    assert request.response_envelope.source_kind == MESSAGE_SOURCE_KIND
    assert request.sources == ResponseSources(
        pending_event_ids=("$selection:localhost",),
        logical_source_event_ids=("$selection:localhost",),
        discovery_event_ids=(selection.question_event_id,),
    )
    metadata = request.matrix_run_metadata
    assert metadata is not None
    assert metadata[constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY] == ["$selection:localhost"]
    assert metadata[constants.MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY] == [selection.question_event_id]

    assert await harness.turn_store.is_handled(_ROOM_ID, selection.question_event_id) is True
    assert await harness.turn_store.is_handled(_ROOM_ID, "$selection:localhost") is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_interactive_selection_waits_for_reload_and_rechecks_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued selection must not acknowledge or answer after its requester is revoked."""
    runtime_paths = test_runtime_paths(tmp_path / "runtime")
    old_config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General",
                    access=ResponderAccessConfig(users=[_SENDER]),
                ),
            },
        ),
        runtime_paths,
    )
    new_config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General",
                    access=ResponderAccessConfig(users=[]),
                ),
            },
        ),
        runtime_paths,
    )
    harness = _build_harness(old_config, tmp_path)
    room = nio.MatrixRoom(_ROOM_ID, _entity_user_id(old_config, "general"))
    selection = interactive.InteractiveSelection(
        question_event_id="$question:localhost",
        question_text="Which option should I use?",
        selection_key="1",
        selected_label="Option 1",
        selected_value="Option 1",
        thread_id="$thread-root:localhost",
    )
    source_event_id = "$selection-during-reload:localhost"
    admission_gate = harness.runtime.response_admission_gate
    assert admission_gate.close_if_idle()
    gate_wait_started = _observe_admission_wait(harness, monkeypatch)
    selection_task = asyncio.create_task(
        harness.selections._handle_interactive_selection(
            room,
            selection=selection,
            facts=_SELECTION_FACTS,
            source_event_id=source_event_id,
            response_target=harness.selections._interactive_selection_target(room.room_id, selection),
        ),
    )
    await asyncio.wait_for(gate_wait_started.wait(), timeout=1)

    assert harness.gateway.sent == []
    harness.runtime.orchestrator.config = new_config
    admission_gate.reopen()
    await selection_task

    assert harness.gateway.sent == []
    assert harness.runner.requests == []
    assert harness.ignored_dispatch_sources == [(source_event_id,)]


@pytest.mark.asyncio
async def test_edited_question_selection_uses_its_exact_source_as_turn_identity(
    config: Config,
    tmp_path: Path,
) -> None:
    """Two prompt revisions at one Matrix target each receive their selected response."""
    harness = _build_harness(config, tmp_path)
    room = nio.MatrixRoom(_ROOM_ID, _entity_user_id(config, "general"))
    original = interactive.InteractiveSelection(
        question_event_id="$question:localhost",
        question_text="Original?",
        selection_key="1",
        selected_label="Original",
        selected_value="original",
        thread_id="$thread-root:localhost",
    )
    replacement = replace(
        original,
        question_text="Replacement?",
        selected_label="Replacement",
        selected_value="replacement",
    )

    await harness.selections._handle_interactive_selection(
        room,
        selection=original,
        facts=_SELECTION_FACTS,
        source_event_id="$original-selection:localhost",
        response_target=harness.selections._interactive_selection_target(room.room_id, original),
    )
    await harness.selections._handle_interactive_selection(
        room,
        selection=replacement,
        facts=_SELECTION_FACTS,
        source_event_id="$replacement-selection:localhost",
        response_target=harness.selections._interactive_selection_target(room.room_id, replacement),
    )

    assert len(harness.runner.requests) == 2
    assert len(harness.gateway.sent) == 2
    assert await harness.turn_store.is_handled(_ROOM_ID, "$original-selection:localhost") is True
    assert await harness.turn_store.is_handled(_ROOM_ID, "$replacement-selection:localhost") is True


@pytest.mark.asyncio
async def test_numeric_interactive_selection_defers_to_tool_approval_continuation(
    config: Config,
    tmp_path: Path,
) -> None:
    """A numeric answer stays journal-owned while its response waits for approval."""
    harness = _build_harness(config, tmp_path)
    harness.runner.answers = False
    selection = interactive.InteractiveSelection(
        question_event_id="$question:localhost",
        question_text="Which option should I use?",
        selection_key="1",
        selected_label="Option 1",
        selected_value="Option 1",
        thread_id="$thread-root:localhost",
    )
    harness.ingress.deps.interactive_questions.claim_interactive_text = AsyncMock(
        return_value=selection,
    )
    event = _text_event("1", event_id="$selection:localhost", thread_id=selection.thread_id)

    outcome = await harness.ingress.handle_text_event(
        _room_with_members(config, "general"),
        event,
    )
    # The queue and the runner-owned task own the answer, so a retry of its source is not refused as claimed.
    assert harness.turn_store.has_live_turn_claim(event.event_id) is False
    await harness.gate.drain_all()
    await harness.runner.settle_inbox_responses()

    assert outcome is TurnDispatchOutcome.DEFERRED
    assert len(harness.runner.requests) == 1
    assert await harness.turn_store.is_handled(_ROOM_ID, event.event_id) is False
    assert harness.ignored_dispatch_sources == []


@pytest.mark.asyncio
async def test_interactive_selection_replay_adopts_durable_ack(config: Config, tmp_path: Path) -> None:
    """A retry after visible delivery reuses the first acknowledgement's durable row."""
    harness = _build_harness(config, tmp_path)
    harness.runner.answers = False
    room = nio.MatrixRoom(_ROOM_ID, _entity_user_id(config, "general"))
    selection = interactive.InteractiveSelection(
        question_event_id="$question:localhost",
        question_text="Which option should I use?",
        selection_key="1",
        selected_label="Option 1",
        selected_value="Option 1",
        thread_id="$thread-root:localhost",
    )
    selection_event_id = "$selection:localhost"

    # The first attempt's reply owns the selection without answering it, so it stays for a retry.
    assert (
        await harness.selections._handle_interactive_selection(
            room,
            selection=selection,
            facts=_SELECTION_FACTS,
            source_event_id=selection_event_id,
            response_target=harness.selections._interactive_selection_target(room.room_id, selection),
        )
        is True
    )

    pending_turn = await harness.turn_store.get_turn_record(selection.question_event_id)
    assert pending_turn is not None
    assert pending_turn.completed is False

    harness.runner.answers = True
    await harness.selections._handle_interactive_selection(
        room,
        selection=selection,
        facts=_SELECTION_FACTS,
        source_event_id=selection_event_id,
        response_target=harness.selections._interactive_selection_target(room.room_id, selection),
    )

    # The retry sends the acknowledgement under the same delivery id, which the
    # outbox resolves to the row its first attempt recorded.
    assert [request.reply_write.span.delivery_id for request in harness.gateway.sent] == [
        selection_event_id,
        selection_event_id,
    ]
    assert len(harness.runner.requests) == 2
    assert await harness.turn_store.is_handled(_ROOM_ID, selection.question_event_id) is True


@pytest.mark.asyncio
async def test_interactive_selection_persistence_failure_prevents_ack_and_generation(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed pending-context write must escape before any visible or persisted response."""
    harness = _build_harness(config, tmp_path)
    room = nio.MatrixRoom(_ROOM_ID, _entity_user_id(config, "general"))
    selection = interactive.InteractiveSelection(
        question_event_id="$question:localhost",
        question_text="Which option should I use?",
        selection_key="1",
        selected_label="Option 1",
        selected_value="Option 1",
        thread_id="$thread-root:localhost",
    )
    real_write = TurnRecordStore.write

    async def fail_pending_write(
        records: TurnRecordStore,
        operation: object,
        *args: object,
        **kwargs: object,
    ) -> object:
        if _is_pending_write_for(operation, args, selection.question_event_id):
            msg = "pending context write failed"
            raise OSError(msg)
        return await real_write(records, operation, *args, **kwargs)

    monkeypatch.setattr(TurnRecordStore, "write", fail_pending_write)

    with pytest.raises(OSError, match="pending context write failed"):
        await harness.selections._handle_interactive_selection(
            room,
            selection=selection,
            facts=_SELECTION_FACTS,
            source_event_id="$selection:localhost",
            response_target=harness.selections._interactive_selection_target(room.room_id, selection),
        )

    assert harness.gateway.sent == []
    assert harness.runner.requests == []


@pytest.mark.asyncio
async def test_interactive_selection_claim_conflict_accepts_racing_terminal_turn(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed owner that wins the pending-record race is a clean terminal outcome."""
    harness = _build_harness(config, tmp_path)
    room = nio.MatrixRoom(_ROOM_ID, _entity_user_id(config, "general"))
    selection = interactive.InteractiveSelection(
        question_event_id="$question:localhost",
        question_text="Which option should I use?",
        selection_key="1",
        selected_label="Option 1",
        selected_value="Option 1",
        thread_id="$thread-root:localhost",
    )
    real_record_pending = harness.turn_store.record_pending_turn

    async def lose_claim_race(_turn_record: TurnRecord) -> None:
        monkeypatch.setattr(harness.turn_store, "record_pending_turn", real_record_pending)
        await harness.turn_store.record_turn(
            TurnRecord.create(
                ["$selection:localhost"],
                response_event_id="$racing-response:localhost",
            ),
            room_id=_ROOM_ID,
        )

    monkeypatch.setattr(harness.turn_store, "record_pending_turn", lose_claim_race)

    await harness.selections._handle_interactive_selection(
        room,
        selection=selection,
        facts=_SELECTION_FACTS,
        source_event_id="$selection:localhost",
        response_target=harness.selections._interactive_selection_target(room.room_id, selection),
    )

    assert harness.gateway.sent == []
    assert harness.runner.requests == []


@pytest.mark.asyncio
async def test_interactive_selection_replacement_refusal_uses_checkpoint_replay(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replaced runtime must bubble refusal so its Matrix event is replayed."""
    harness = _build_harness(config, tmp_path)
    room = nio.MatrixRoom(_ROOM_ID, _entity_user_id(config, "general"))
    selection = interactive.InteractiveSelection(
        question_event_id="$question:localhost",
        question_text="Which option should I use?",
        selection_key="1",
        selected_label="Option 1",
        selected_value="Option 1",
        thread_id="$thread-root:localhost",
    )
    selection_event_id = "$selection:localhost"

    async def refuse(request: ResponseRequest) -> SourceFate:
        harness.runner.requests.append(request)
        raise ResponseAdmissionRefusedError

    monkeypatch.setattr(harness.runner, "generate_response", refuse)

    with pytest.raises(ResponseAdmissionRefusedError):
        await harness.selections._handle_interactive_selection(
            room,
            selection=selection,
            facts=_SELECTION_FACTS,
            source_event_id=selection_event_id,
            response_target=harness.selections._interactive_selection_target(room.room_id, selection),
        )

    # Only the durably tracked acknowledgement was sent. Refusal performs no
    # untimed Matrix settlement inside replacement shutdown.
    assert len(harness.gateway.sent) == 1
    assert harness.gateway.edited == []

    record = await harness.turn_store.get_turn_record(selection_event_id)
    assert record is not None
    assert record.completed is False


@pytest.mark.asyncio
async def test_interactive_selection_redacted_after_ack_is_suppressed_under_lock(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The selection must be pending before ack and recheck aliases at response startup."""
    harness = _build_harness(config, tmp_path)
    room = nio.MatrixRoom(_ROOM_ID, _entity_user_id(config, "general"))
    selection = interactive.InteractiveSelection(
        question_event_id="$question:localhost",
        question_text="Which option should I use?",
        selection_key="1",
        selected_label="Option 1",
        selected_value="Option 1",
        thread_id="$thread-root:localhost",
    )
    selection_event_id = "$selection:localhost"

    async def send_ack_then_redact(request: SendTextRequest) -> str:
        harness.gateway.sent.append(request)
        pending = await harness.turn_store.get_turn_record(selection_event_id)
        assert pending is not None
        assert pending.conversation_target is not None
        assert pending.history_scope is not None
        await _redact(harness, selection_event_id, room.room_id)
        return "$ack:localhost"

    monkeypatch.setattr(harness.gateway, "send_text", send_ack_then_redact)

    await harness.selections._handle_interactive_selection(
        room,
        selection=selection,
        facts=_SELECTION_FACTS,
        source_event_id=selection_event_id,
        response_target=harness.selections._interactive_selection_target(room.room_id, selection),
    )

    assert len(harness.runner.requests) == 1
    record = await harness.turn_store.get_turn_record(selection_event_id)
    assert record is not None
    assert await harness.turn_store.deleted_sources(record) == {selection_event_id}
    assert await harness.turn_store.is_handled(_ROOM_ID, selection_event_id) is True
    assert await harness.turn_store.is_handled(_ROOM_ID, selection.question_event_id) is False


@pytest.mark.asyncio
async def test_interactive_selection_rehydrates_attachment_context_from_thread(
    config: Config,
    tmp_path: Path,
) -> None:
    """A selection callback turn reaches the attachments of the conversation that asked the question.

    The selection is a synthetic turn with no Matrix message of its own, so it
    must rebuild the attachment context from the originating thread; before the
    fix the callback request carried no attachment IDs and ``get_attachment``
    rejected IDs that were available when the question was asked.
    """
    harness = _build_harness(config, tmp_path)
    media_path = tmp_path / "incoming_media" / "report.pdf"
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(b"%PDF-1.4 fake report")
    record = register_local_attachment(
        tmp_path,
        media_path,
        kind="file",
        filename="report.pdf",
        room_id=_ROOM_ID,
        thread_id="$thread-root:localhost",
        sender=_SENDER,
    )
    assert record is not None
    triggering_message = make_visible_message(
        sender=_SENDER,
        body="here is the report",
        content={
            "msgtype": "m.text",
            "body": "here is the report",
            constants.ATTACHMENT_IDS_KEY: [record.attachment_id],
        },
        thread_id="$thread-root:localhost",
    )
    _serve_conversation(harness, [triggering_message])
    room = nio.MatrixRoom(_ROOM_ID, _entity_user_id(config, "general"))
    selection = interactive.InteractiveSelection(
        question_event_id="$question:localhost",
        question_text="Process the attached report?",
        selection_key="1",
        selected_label="Yes",
        selected_value="Yes",
        thread_id="$thread-root:localhost",
    )

    await harness.selections._handle_interactive_selection(
        room,
        selection=selection,
        facts=_SELECTION_FACTS,
        source_event_id="$selection:localhost",
        response_target=harness.selections._interactive_selection_target(room.room_id, selection),
    )

    assert len(harness.runner.requests) == 1
    request = harness.runner.requests[0]
    assert request.attachment_ids == (record.attachment_id,)
    assert request.response_envelope.attachment_ids == (record.attachment_id,)


@pytest.mark.asyncio
async def test_interactive_selection_attachment_setup_failure_finalizes_ack(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attachment-resolution failure visibly terminates the processing acknowledgment through its reply."""
    harness = _build_harness(config, tmp_path)
    harness.gateway.replies_own_events = True

    async def fail_attachment_resolution(
        _normalizer: InboundTurnNormalizer,
        _request: object,
    ) -> object:
        msg = "attachment lookup failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        InboundTurnNormalizer,
        "build_dispatch_payload_with_attachments",
        fail_attachment_resolution,
    )
    room = nio.MatrixRoom(_ROOM_ID, _entity_user_id(config, "general"))
    selection = interactive.InteractiveSelection(
        question_event_id="$question:localhost",
        question_text="Process the attached report?",
        selection_key="1",
        selected_label="Yes",
        selected_value="Yes",
        thread_id="$thread-root:localhost",
    )

    await harness.selections._handle_interactive_selection(
        room,
        selection=selection,
        facts=_SELECTION_FACTS,
        source_event_id="$selection:localhost",
        response_target=harness.selections._interactive_selection_target(room.room_id, selection),
    )

    assert harness.runner.requests == []
    assert len(harness.gateway.sent) == 1
    assert harness.gateway.failed_dispatches == [("$sent-1:localhost", "[general] ⚠️ Error: attachment lookup failed")]
    assert await harness.turn_store.is_handled(_ROOM_ID, selection.question_event_id) is True
    assert await harness.turn_store.is_handled(_ROOM_ID, "$selection:localhost") is True


@pytest.mark.asyncio
async def test_interactive_selection_without_response_stays_retryable(config: Config, tmp_path: Path) -> None:
    """A selection whose generation yields no visible response must not be marked handled."""
    harness = _build_harness(config, tmp_path)
    harness.runner.answers = False
    room = nio.MatrixRoom(_ROOM_ID, _entity_user_id(config, "general"))
    selection = interactive.InteractiveSelection(
        question_event_id="$question:localhost",
        question_text="Which option should I use?",
        selection_key="2",
        selected_label="Option 2",
        selected_value="Option 2",
        thread_id="$thread-root:localhost",
    )

    # The reply's records own the selection, so its handler leaves it unsettled for a retry.
    assert (
        await harness.selections._handle_interactive_selection(
            room,
            selection=selection,
            facts=_SELECTION_FACTS,
            source_event_id="$selection:localhost",
            response_target=harness.selections._interactive_selection_target(room.room_id, selection),
        )
        is True
    )

    assert len(harness.runner.requests) == 1
    assert await harness.turn_store.is_handled(_ROOM_ID, selection.question_event_id) is False
    assert await harness.turn_store.is_handled(_ROOM_ID, "$selection:localhost") is False


@pytest.mark.asyncio
async def test_interactive_selection_interruption_records_handled_selection(
    config: Config,
    tmp_path: Path,
) -> None:
    """A selection its reply recorded answered stays handled when the interruption rethrows."""
    harness = _build_harness(config, tmp_path)
    harness.runner.deferred_sync_restart_error = asyncio.CancelledError("sync_restart")
    room = nio.MatrixRoom(_ROOM_ID, _entity_user_id(config, "general"))
    selection = interactive.InteractiveSelection(
        question_event_id="$question:localhost",
        question_text="Which option should I use?",
        selection_key="2",
        selected_label="Option 2",
        selected_value="Option 2",
        thread_id="$thread-root:localhost",
    )
    selection_event_id = "$selection:localhost"

    with pytest.raises(asyncio.CancelledError, match="sync_restart"):
        await harness.selections._handle_interactive_selection(
            room,
            selection=selection,
            facts=_SELECTION_FACTS,
            source_event_id=selection_event_id,
            response_target=harness.selections._interactive_selection_target(room.room_id, selection),
        )

    record = await harness.turn_store.get_turn_record(selection_event_id)
    assert record is not None
    assert record.completed is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize(("recover_as_member", "cold"), [(True, False), (False, False), (True, True)])
async def test_pending_membership_preserves_receipt_order_and_quiet_retry(  # noqa: PLR0915
    tmp_path: Path,
    recover_as_member: bool,
    cold: bool,
) -> None:
    """The exact admitted lane survives uncertainty, quiet retries, and a new dispatcher."""
    config = _membership_single_agent_config(tmp_path)
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    client = make_matrix_client_mock(user_id=_entity_user_id(config, "general"))
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room.room_id])
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember(_SENDER, None, None)],
        room_id=room.room_id,
    )
    memberships = harness.runtime.agent_reply_memberships
    await memberships.refresh(config, runtime_paths_for(config), client)
    memberships.invalidate(config, reason="uncertain_sync_response")
    tracking_path = tmp_path / "dispatch-tracking"
    dispatcher = _obligation_runner(
        harness,
        tracking_path=tracking_path,
        matrix_user_id=_entity_user_id(config, "general"),
        entity_name="general",
        room=room,
    )
    attempted: list[str] = []
    failed = asyncio.Event()
    finished = asyncio.Event()
    real_callback = dispatcher.callbacks.on_message

    async def observe(room: nio.MatrixRoom, event: nio.RoomMessageFormatted) -> TurnDispatchOutcome:
        attempted.append(event.event_id)
        try:
            outcome = await real_callback(room, event)
        except ReplyMembershipPendingError:
            failed.set()
            raise
        if event.event_id == "$second":
            finished.set()
        return outcome

    dispatcher.callbacks = replace(dispatcher.callbacks, on_message=observe)
    first = _text_event("first owed", event_id="$first")
    second = _text_event("second owed", event_id="$second")
    for event in (first, second):
        await admit_dispatch_event(dispatcher, room, event, EventKind.MESSAGE, EventClass.ACTIONABLE)
    dispatcher.release_turn_replay()
    dispatcher.start()
    try:
        await asyncio.wait_for(failed.wait(), timeout=4)  # above the first gate's 1 s wait for the router
        assert attempted == ["$first"]
        assert await dispatcher.store.is_pending("$first")
        assert await dispatcher.store.is_pending("$second")
        assert await harness.turn_store.get_turn_record("$first") is None
        assert harness.runner.requests == []
        if cold:
            await dispatcher.stop()
            dispatcher = _obligation_runner(
                harness,
                tracking_path=tracking_path,
                matrix_user_id=_entity_user_id(config, "general"),
                entity_name="general",
                room=room,
            )
            dispatcher.callbacks = replace(dispatcher.callbacks, on_message=observe)
        client.joined_members.return_value = nio.JoinedMembersResponse(
            members=[nio.RoomMember(_SENDER, None, None)] if recover_as_member else [],
            room_id=room.room_id,
        )
        await memberships.refresh(config, runtime_paths_for(config), client)
        if not recover_as_member:
            room.remove_member(_SENDER)  # This bot's own sync sees the departure too.
        if cold:
            dispatcher.release_turn_replay()
            dispatcher.start()
        # No admission or wake: the worker's existing failed-lane timer owns recovery.
        await asyncio.wait_for(finished.wait(), timeout=4)
        await harness.gate.drain_all()
        await harness.runner.settle_inbox_responses()
        assert attempted == ["$first", "$first", "$second"]
        await _settle_dispatcher(harness, dispatcher)
    finally:
        await dispatcher.stop()
    # Each top-level message is its own conversation, so the two answers may start in either order once dispatched
    # in receipt order.
    assert sorted(request.prompt for request in harness.runner.requests) == (
        ["first owed", "second owed"] if recover_as_member else []
    )
    assert await harness.turn_store.is_handled(_ROOM_ID, "$first")
    assert await harness.turn_store.is_handled(_ROOM_ID, "$second")
    assert not await dispatcher.store.is_pending("$first")
    assert not await dispatcher.store.is_pending("$second")


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize("race", ["stable", "pending", "denied", "explicit_grant"])
async def test_late_membership_change_preserves_exact_source(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    race: str,
) -> None:
    """Late uncertainty retains the admitted obligation; real denial still settles it."""
    config = _membership_single_agent_config(tmp_path)
    if race == "explicit_grant":
        config.agents["general"].access.users = [_SENDER]
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _text_event(
        f"{_entity_user_id(config, 'general')} answer once",
        event_id="$late-membership:localhost",
    )
    client = make_matrix_client_mock(user_id=_entity_user_id(config, "general"))
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room.room_id])
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember(_SENDER, None, None)],
        room_id=room.room_id,
    )
    memberships = harness.runtime.agent_reply_memberships
    await memberships.refresh(config, runtime_paths_for(config), client)
    dispatcher = _obligation_runner(
        harness,
        tracking_path=tmp_path / "dispatch-tracking",
        matrix_user_id=_entity_user_id(config, "general"),
        entity_name="general",
        room=room,
    )
    visible = harness.dispatcher.deps.visible_responses
    # Replace recording-only settlement seam with actual production journal owner.
    visible.deps = replace(
        visible.deps,
        settle_ignored_sources=dispatcher.settle_intentionally_ignored_turn_sources,
    )
    _replace_harness_deps(
        harness,
        dispatch_source_is_terminal=dispatcher.source_is_terminal,
    )
    reached, release = asyncio.Event(), asyncio.Event()
    first = True
    plan_turn = TurnPolicy.plan_turn

    async def hold_after_strict_authorization(policy: TurnPolicy, *args: object, **kwargs: object) -> object:
        nonlocal first
        if first:
            first = False
            assert harness.policy.can_reply_to_sender_in_room(_SENDER, room.room_id)
            reached.set()
            await release.wait()
        return await plan_turn(policy, *args, **kwargs)

    monkeypatch.setattr(TurnPolicy, "plan_turn", hold_after_strict_authorization)
    await admit_dispatch_event(dispatcher, room, event, EventKind.MESSAGE, EventClass.ACTIONABLE)
    try:
        await dispatcher.drain_once()
        await asyncio.wait_for(reached.wait(), timeout=3)
        if race in {"pending", "explicit_grant"}:
            memberships.invalidate(config, reason="uncertain_sync_response")
            if race == "pending":
                with pytest.raises(ReplyMembershipPendingError):
                    harness.policy.can_reply_to_sender_in_room(_SENDER, room.room_id)
            else:
                assert harness.policy.can_reply_to_sender_in_room(_SENDER, room.room_id)
        elif race == "denied":
            client.joined_members.return_value = nio.JoinedMembersResponse(members=[], room_id=room.room_id)
            await memberships.refresh(config, runtime_paths_for(config), client)
            assert not harness.policy.can_reply_to_sender_in_room(_SENDER, room.room_id)
        release.set()
        await harness.gate.drain_all()
        await harness.runner.settle_inbox_responses()
        before = {
            "pending": await dispatcher.store.is_pending(event.event_id),
            "response_count": len(harness.runner.requests),
            "ledger_exists": await harness.turn_store.get_turn_record(event.event_id) is not None,
            "retry_requests": list(harness.retried_dispatch_sources),
        }
        if race == "pending":
            assert before["pending"] is True
            assert before["response_count"] == 0
            assert before["ledger_exists"] is False
            assert before["retry_requests"] == [(event.event_id,)]
            await memberships.refresh(config, runtime_paths_for(config), client)
        await _settle_dispatcher(harness, dispatcher)
        after_count = len(harness.runner.requests)
        await dispatcher.drain_once()
        await harness.gate.drain_all()
        await harness.runner.settle_inbox_responses()
        assert len(harness.runner.requests) == after_count
        if race == "pending":
            assert (before["pending"], before["response_count"], after_count) == (True, 0, 1), (
                "Accepted source must remain pending through uncertainty, then answer exactly once after refresh",
                before,
                after_count,
            )
        elif race == "denied":
            assert (before["pending"], before["response_count"], after_count) == (False, 0, 0)
        else:
            assert (before["response_count"], after_count) == (1, 1)
    finally:
        release.set()
        await dispatcher.stop()
        await harness.gate.drain_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("mention", [False, True])
async def test_adaptive_text_admission_delays_then_passes_participation(
    config: Config,
    tmp_path: Path,
    mention: bool,
) -> None:
    """Agent participation delays ad hoc room text and follows it through response admission."""
    config.agents["general"].rooms = []
    config.rooms = {}
    config.agents["general"].participation = ParticipationConfig(
        debounce_seconds=30.0 if mention else 0.1,
        decline_reaction="👍",
    )
    history = thread_history_result(
        [
            make_visible_message(sender="@other:localhost", body="earlier", event_id=_THREAD_ROOT),
            make_visible_message(sender=_entity_user_id(config, "general"), body="answer", event_id="$answer"),
        ],
        is_full_history=True,
    )
    harness = _build_harness(config, tmp_path, thread_history=history)
    room = _room_with_members(config, "general", "research")
    room.add_member("@other:localhost", "Other", None)
    await harness.ingress.handle_text_event(room, _text_event("my thought", thread_id=_THREAD_ROOT))
    await asyncio.sleep(0.02)
    assert harness.runner.requests == []
    if mention:
        explicit = _text_event("please answer", event_id="$mention:localhost", thread_id=_THREAD_ROOT)
        explicit.source["content"]["m.mentions"] = {"user_ids": [_entity_user_id(config, "general")]}
        await harness.ingress.handle_text_event(room, explicit)
    await asyncio.wait_for(harness.runner.response_started.wait(), timeout=5)
    await harness.runner.settle_inbox_responses()
    assert len(harness.runner.requests) == 1
    if mention:
        assert harness.runner.requests[0].participation is None
        assert "my thought" in harness.runner.requests[0].prompt
    else:
        assert harness.runner.requests[0].participation is config.agents["general"].participation
        assert harness.runner.requests[0].participation.decline_reaction == "👍"
    await harness.gate.drain_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["multi_human", "single_human", "mentioned"])
async def test_opted_in_active_backlog_preserves_idle_dispatch_and_requesters(
    config: Config,
    tmp_path: Path,
    mode: str,
) -> None:
    """Active backlogs dispatch one ordered turn per requester, selecting participation for multi-human context."""
    config.agents["general"].participation = ParticipationConfig(debounce_seconds=30)
    first_sender = _SENDER if mode == "single_human" else "@other:localhost"
    expected_batch_count = 1 if mode == "single_human" else 2
    history = thread_history_result(
        [
            make_visible_message(sender=first_sender, body="earlier", event_id=_THREAD_ROOT),
            make_visible_message(sender=_entity_user_id(config, "general"), body="working", event_id="$working"),
        ],
        is_full_history=True,
    )
    harness = _build_harness(config, tmp_path, thread_history=history)
    room = _room_with_members(config, "general", "research")
    room.add_member(first_sender, first_sender, None)
    idle = asyncio.Event()
    dispatched = asyncio.Event()
    batches: list[PreparedTurn] = []
    key = active_follow_up_coalescing_key(_ROOM_ID, _THREAD_ROOT)

    async def wait_until_idle(_room_id: str, _thread_id: str | None) -> None:
        await idle.wait()

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)
        await harness.dispatcher.handle_prepared_turn(batch)
        await harness.runner.settle_inbox_responses()
        if len(batches) == expected_batch_count:
            dispatched.set()

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 30.0,
        is_shutting_down=lambda: False,
        wait_for_thread_response_idle=wait_until_idle,
    )
    try:
        for event_id, sender, body in (
            ("$first:localhost", first_sender, "first follow-up"),
            ("$second:localhost", _SENDER, "extra context"),
        ):
            event = _text_event(body, event_id=event_id, thread_id=_THREAD_ROOT, sender=sender)
            if mode == "mentioned" and event_id == "$second:localhost":
                event.source["content"]["m.mentions"] = {"user_ids": [_entity_user_id(config, "general")]}
            pending = make_pending_event(
                event,
                room,
                source_kind=MESSAGE_SOURCE_KIND,
                requester_user_id=sender,
                active_follow_up=True,
            )
            pending.text_debounce_seconds = 30.0 if mode == "multi_human" else 0.0
            await gate.admit(key, ready_result=ReadyPendingEvent(pending_event=pending))
        await asyncio.sleep(0)
        assert batches == []
        assert harness.runner.requests == []
        idle.set()
        async with asyncio.timeout(1):
            await dispatched.wait()
        assert len(batches) == len(harness.runner.requests) == expected_batch_count
        assert all(batch.coalescing_key == key for batch in batches)
        if mode == "single_human":
            expected_turns = [(("$first:localhost", "$second:localhost"), _SENDER, (True, True), False)]
        else:
            expected_turns = [
                (("$first:localhost",), first_sender, (True, False), False),
                (("$second:localhost",), _SENDER, (False, True), mode == "multi_human"),
            ]
        assert [
            (
                batch.handled_turn.source_event_ids,
                request.user_id,
                ("first follow-up" in request.prompt, "extra context" in request.prompt),
                request.participation is not None,
            )
            for batch, request in zip(batches, harness.runner.requests, strict=True)
        ] == expected_turns
        assert [batch.facts.run_requester_id for batch in batches] == [
            requester for _, requester, _, _ in expected_turns
        ]
    finally:
        idle.set()
        await gate.drain_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("by_agent", [False, True], ids=["person", "agent"])
async def test_an_edit_before_the_turn_starts_changes_what_it_answers_unless_an_agent_made_it(
    config: Config,
    tmp_path: Path,
    by_agent: bool,
) -> None:
    """A person's message edited while it still waits is answered as edited, and an agent's as it was queued.

    Neither edit regenerates anything.
    """
    harness = _build_harness(config, tmp_path)
    regenerator = _NoEditRegeneration()
    _replace_harness_deps(harness, edit_regenerator=regenerator)
    room = _room_with_members(config, "general", "research")
    sender = _entity_user_id(config, "research") if by_agent else _SENDER
    mentions = {"user_ids": [_entity_user_id(config, "general")]}
    original = _text_event("@general what is 2+2?", thread_id=_THREAD_ROOT, sender=sender)
    original.source["content"]["m.mentions"] = mentions
    edit = _text_event("* @general what is 3+3?", event_id="$edit:localhost", origin_server_ts=1_000_001, sender=sender)
    edit.source["content"]["m.new_content"] = {
        "body": "@general what is 3+3?",
        "msgtype": "m.text",
        "m.mentions": mentions,
    }
    edit.source["content"]["m.relates_to"] = {"rel_type": "m.replace", "event_id": original.event_id}
    key = CoalescingKey(_ROOM_ID, _THREAD_ROOT, RequesterCoalescingOwner(sender))

    # The message waits out a debounce in its queue, as an agent's participation delay holds it.
    with patch.object(harness.ingress, "_adaptive_text_debounce_seconds", AsyncMock(return_value=60.0)):
        await harness.ingress.handle_text_event(room, original)

        async def queued() -> None:
            while not harness.gate.queued_pending_events(key):  # noqa: ASYNC110
                await asyncio.sleep(0.001)

        await asyncio.wait_for(queued(), timeout=5)
        outcome = await harness.ingress.handle_text_event(room, edit)
        await harness.gate.drain_all()
    await harness.runner.settle_inbox_responses()

    assert outcome is TurnDispatchOutcome.INTENTIONALLY_IGNORED
    assert regenerator.edit_event_ids == []
    (request,) = harness.runner.requests
    answered, replaced = ("2+2", "3+3") if by_agent else ("3+3", "2+2")
    assert f"what is {answered}?" in request.prompt
    assert replaced not in request.prompt


@pytest.mark.asyncio
async def test_each_admitted_event_is_classified_once(config: Config, tmp_path: Path) -> None:
    """Precheck, claim, lanes, debounce, notices, and dispatch all read one classification of the event."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    ingress = MagicMock(spec=IngressValidator, wraps=harness.ingress.deps.ingress)
    _replace_harness_deps(harness, ingress=ingress)

    await harness.deliver(room, _text_event("@general hello", thread_id=_THREAD_ROOT))

    assert len(harness.runner.requests) == 1
    ingress.classify.assert_called_once()


@pytest.mark.asyncio
async def test_edited_second_of_two_queued_messages_keeps_every_message_answered(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch whose last message was edited into a command still answers every message it holds."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    first = _text_event("@general please look at this", event_id="$first:localhost", thread_id=_THREAD_ROOT)
    edited = _text_event("!help", event_id="$second:localhost", thread_id=_THREAD_ROOT, origin_server_ts=1_000_001)
    turn = build_prepared_turn(
        requester_coalescing_key(room.room_id, _THREAD_ROOT, _SENDER),
        [make_pending_event(event, room, source_kind=MESSAGE_SOURCE_KIND) for event in (first, edited)],
        queues_other_run=nothing_else_queued,
    )
    execute_if_owned = AsyncMock(return_value=True)
    monkeypatch.setattr(harness.dispatcher.deps.command_executor, "execute_if_owned", execute_if_owned)

    await harness.dispatcher.handle_prepared_turn(turn)
    await harness.runner.settle_inbox_responses()

    execute_if_owned.assert_not_awaited()
    assert [request.sources.logical_source_event_ids for request in harness.runner.requests] == [
        (first.event_id, edited.event_id),
    ]
    assert harness.ignored_dispatch_sources == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_finished_agent_reply_with_tool_markers_runs_its_command_once_for_its_human(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent's finished reply for a human that ends in a command runs it once, as that human, under its alias."""
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General"), "research": AgentConfig(display_name="Research")},
            router={"access": {"current_room_members": False, "members_of_rooms": [], "users": [_OWNER]}},
        ),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    room = _joined_room(config, ROUTER_AGENT_NAME, "general", "research")
    placeholder_id = "$placeholder:localhost"
    _admit_placeholder(harness, placeholder_id)
    body = "🔧 `search` [1]\n\n!help"
    content = build_edit_event_content(
        event_id=placeholder_id,
        new_content={
            "body": body,
            "msgtype": "m.text",
            "m.mentions": {
                "user_ids": [_entity_user_id(config, ROUTER_AGENT_NAME), _entity_user_id(config, "general")],
            },
            constants.TOOL_TRACE_CONTENT_KEY: {"version": 2, "events": []},
        },
        new_text=body,
        extra_content={
            constants.ACTING_REQUESTER_KEY: _OWNER,
            constants.STREAM_STATUS_KEY: constants.STREAM_STATUS_COMPLETED,
        },
    )
    claimed_aliases: list[bool] = []
    executed: list[tuple[str, bool, str]] = []

    async def execute_if_owned(**kwargs: Any) -> bool:  # noqa: ANN401
        claimed_aliases.append(harness.turn_store.has_live_turn_claim(placeholder_id))
        executed.append((kwargs["command"].type.value, kwargs["acts_for_requester"], kwargs["requester_user_id"]))
        return True

    monkeypatch.setattr(harness.dispatcher.deps.command_executor, "execute_if_owned", execute_if_owned)

    await harness.deliver(room, _final_reply_edit_event(config, content, event_id="$final:localhost"))

    assert executed == [("help", True, _OWNER)]
    assert claimed_aliases == [True]
    assert harness.runner.requests == []


@pytest.mark.asyncio
async def test_router_relay_of_a_managed_agents_schedule_keeps_its_model_and_history_limit(tmp_path: Path) -> None:
    """A task a managed agent scheduled, routed by the router to one of several responders, keeps its run settings."""
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General"), "research": AgentConfig(display_name="Research")},
        ),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", "research", ROUTER_AGENT_NAME)
    # The relay names no original sender: the task's requester is an entity, so the router relays as itself.
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "@general ⏰ [Automated Task]\nSummarize the queue",
                "msgtype": "m.text",
                "m.mentions": {"user_ids": [_entity_user_id(config, "general")]},
                constants.SCHEDULED_HISTORY_LIMIT_KEY: 3,
                constants.SCHEDULED_MODEL_KEY: "cheap",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$scheduled:localhost"}},
            },
            "event_id": "$managed-schedule-relay:localhost",
            "sender": _entity_user_id(config, ROUTER_AGENT_NAME),
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )

    await harness.deliver(room, event)

    assert len(harness.runner.requests) == 1
    request = harness.runner.requests[0]
    assert request.response_envelope.origin.intent is TurnIntent.ROUTER_NOTICE
    assert request.scheduled_history_budget == ScheduledHistoryBudget(limit=3, source_event_id="$scheduled:localhost")
    assert request.scheduled_model == "cheap"


@pytest.mark.asyncio
async def test_a_persons_edit_reaches_the_regenerator_classified_as_an_edit(config: Config, tmp_path: Path) -> None:
    """An edit of a message no queue holds regenerates as an edit, run by its author."""
    harness = _build_harness(config, tmp_path)
    regenerator = _NoEditRegeneration()
    _replace_harness_deps(harness, edit_regenerator=regenerator)
    room = _room_with_members(config, "general")
    edit = _text_event("* @general what is 3+3?", event_id="$edit:localhost", origin_server_ts=1_000_001)
    edit.source["content"]["m.new_content"] = {"body": "@general what is 3+3?", "msgtype": "m.text"}
    edit.source["content"]["m.relates_to"] = {"rel_type": "m.replace", "event_id": "$answered:localhost"}

    await harness.deliver(room, edit)

    assert regenerator.edit_event_ids == [edit.event_id]
    (facts,) = regenerator.edit_facts
    assert facts.source_kind == EDIT_SOURCE_KIND
    assert facts.run_requester_id == _SENDER


@pytest.mark.asyncio
async def test_hook_text_that_looks_like_a_command_is_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hook text runs with a person's authority but a plugin or an agent wrote it, so `!help` there is never a command."""
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General")},
            router={"access": {"current_room_members": False, "members_of_rooms": [], "users": [_SENDER]}},
        ),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    room = _room_with_members(config, ROUTER_AGENT_NAME, "general")
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "!help",
                "msgtype": "m.text",
                constants.SOURCE_KIND_KEY: HOOK_DISPATCH_SOURCE_KIND,
                constants.ORIGINAL_SENDER_KEY: _SENDER,
            },
            "event_id": "$hook-command:localhost",
            "sender": _entity_user_id(config, "general"),
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )
    execute_if_owned = AsyncMock(return_value=True)
    monkeypatch.setattr(harness.dispatcher.deps.command_executor, "execute_if_owned", execute_if_owned)

    await harness.deliver(room, event)

    assert [batch.handled_turn.source_event_ids for batch in harness.gate_batches] == [(event.event_id,)]
    assert harness.gate_batches[0].command is None
    execute_if_owned.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("original_sender", "relay_context"), [(_SENDER, True), ("@telegram:localhost", False)])
async def test_only_a_router_relay_of_a_human_reads_relay_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    original_sender: str,
    relay_context: bool,
) -> None:
    """A human's routed message reads the context the relay names; a bot account's relay reads the conversation."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General")}, bot_accounts=["@telegram:localhost"]),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", ROUTER_AGENT_NAME)
    resolver = harness.ingress.deps.resolver
    trusted_context = AsyncMock(wraps=resolver.extract_trusted_router_relay_context)
    monkeypatch.setattr(resolver, "extract_trusted_router_relay_context", trusted_context)
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "@general could you help with this?",
                "msgtype": "m.text",
                "m.mentions": {"user_ids": [_entity_user_id(config, "general")]},
                constants.SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                constants.ORIGINAL_SENDER_KEY: original_sender,
            },
            "event_id": "$router-relay:localhost",
            "sender": _entity_user_id(config, ROUTER_AGENT_NAME),
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )

    await harness.deliver(room, event)

    assert len(harness.runner.requests) == 1
    assert harness.runner.requests[0].user_id == original_sender
    assert trusted_context.await_count == (1 if relay_context else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("ended_by", ["deletion", "relay_answer"])
async def test_a_batch_source_ended_before_its_reply_hands_the_rest_back_once_the_claim_releases(
    config: Config,
    tmp_path: Path,
    ended_by: str,
) -> None:
    """S of a pending [S, F] deleted, or answered by a relay, before any reply: S settles, F answers on its own later."""
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    first = _text_event("first", event_id="$s:localhost")
    second = _text_event("second", event_id="$f:localhost")
    target = MessageTarget.resolve(room.room_id, None, second.event_id)
    dispatch = PreparedDispatch(
        requester_user_id=_SENDER,
        context=MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=(),
            mentioned_agents=[],
            has_non_agent_mentions=False,
        ),
        target=target,
        correlation_id=second.event_id,
        envelope=harness.ingress.deps.resolver.build_envelope(
            event=second,
            facts=make_ingress_facts(second.sender, requester_user_id=_SENDER),
            target=target,
        ),
    )
    pending = await harness.turn_store.record_pending_turn(
        harness.turn_store.attach_response_context(
            TurnRecord.create([first.event_id, second.event_id], completed=False, requester_id=_SENDER),
            history_scope=harness.turn_store.response_history_scope(
                ResponseAction(kind="individual"),
                requester_user_id=_SENDER,
            ),
            conversation_target=target,
        ),
    )
    assert pending is not None
    # The response task holds the batch's claim until it ends.
    claim = await harness.turn_store.claim(pending.record)
    assert isinstance(claim, TurnClaim)
    if ended_by == "deletion":
        await _redact(harness, first.event_id, _ROOM_ID)
    else:
        await harness.turn_store.record_turn(
            TurnRecord.create(
                ["$relay:localhost"],
                discovery_event_ids=[first.event_id],
                response_event_id="$relay-answer:localhost",
            ),
            room_id=_ROOM_ID,
        )

    await harness.dispatcher._execute_response_action(
        room,
        second,
        dispatch,
        ResponseAction(kind="individual"),
        DispatchPayloadInputs((), (), ()),
        dispatch_started_at=0.0,
        handled_turn=pending.record,
    )

    assert harness.ignored_dispatch_sources == [(first.event_id,)]
    assert harness.retried_dispatch_sources == [], "the survivor waited for the response's claim"
    claim.release()
    assert harness.retried_dispatch_sources == [(second.event_id,)]
    assert not await harness.turn_store.is_handled(_ROOM_ID, second.event_id)


@pytest.mark.asyncio
async def test_router_recovery_relays_once_past_every_echo_of_a_coalesced_voice_turn(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two finalized echoes of one routing turn, then a crash before the relay: the replay relays exactly once."""
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    room = _room_with_members(config, ROUTER_AGENT_NAME, "general")
    event = _text_event("route this", event_id="$voice-2:localhost")
    echoes = {"$voice-1:localhost": "$echo-1:localhost", "$voice-2:localhost": "$echo-2:localhost"}
    for source, echo in echoes.items():
        await harness.turn_store.record_visible_echo(_ROOM_ID, source, echo)
        await harness.turn_store.record_finalized_visible_echo(source, echo, is_fallback=False)
    handled_turn = TurnRecord.create(list(echoes), completed=False)
    send_text = harness.gateway.send_text
    monkeypatch.setattr(harness.gateway, "send_text", AsyncMock(side_effect=RuntimeError("crash before the relay")))
    with pytest.raises(RuntimeError, match="crash before the relay"):
        await execute_router_relay(
            harness.dispatcher.deps,
            room,
            event,
            (),
            requester_user_id=_SENDER,
            handled_turn=handled_turn,
        )
    monkeypatch.setattr(harness.gateway, "send_text", send_text)

    with patch(
        "mindroom.visible_response_reconciliation.find_response_event_ids_via_room_messages",
        return_value=frozenset(echoes.values()),
    ):
        await execute_router_relay(
            harness.dispatcher.deps,
            room,
            event,
            (),
            requester_user_id=_SENDER,
            handled_turn=handled_turn,
        )

    assert len(harness.gateway.sent) == 1
    record = await harness.turn_store.get_turn_record(event.event_id)
    assert record is not None
    assert record.completed is True
    assert record.response_event_id == "$sent-1:localhost"
