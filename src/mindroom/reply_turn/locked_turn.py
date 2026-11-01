"""The locked turn every reply shares: its settlement, its effects, and what its owners read of it.

The response runner prepares agent and team turns and the approval continuation runner resumes paused ones; both settle
through ``LockedTurns``, which knows neither, so the dependency between them runs one way.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any

from mindroom import reply_lifecycle as rl
from mindroom.ai_runtime import bind_mid_turn_conversation_context
from mindroom.authorization import is_sender_allowed_for_entity_replies_in_room
from mindroom.automations.steps import is_automation_hook_source
from mindroom.background_tasks import create_background_task
from mindroom.config.resolution import memory_backend
from mindroom.contacts.turns import contact_turn
from mindroom.conversation_state_writer import session_started_on_creation
from mindroom.delivery_gateway import ResponseIdentity
from mindroom.entity_resolution import current_internal_sender_ids
from mindroom.failure_text import failure_text
from mindroom.final_delivery import (
    AwaitingApproval,
    Cancelled,
    Completed,
    Failed,
    FailureCause,
    FinalDeliveryOutcome,
    Owed,
    Suppressed,
    Suppression,
    TurnProgress,
)
from mindroom.judgment.conversation import conversation_context_before_sources
from mindroom.memory import store_conversation_memory
from mindroom.orchestration.runtime import classify_cancel_source, current_task_is_process_shutdown
from mindroom.reply_scope import current_span, release_decision, suppress_decision
from mindroom.reply_turn.request import (
    correlation_id_for_request,
    is_silent_schedule_response,
    reply_authorization_entity_names,
)
from mindroom.response_attempt import ResponseAttemptDeps, ResponseAttemptRequest, ResponseAttemptRunner, SpanAttempt
from mindroom.response_lifecycle import ResponseLifecycle, ResponseLifecycleDeps
from mindroom.response_turn import ResponsePausedForApproval, skip_unapproved_attempt
from mindroom.streaming import PausePresentationError, StreamingDeliveryError
from mindroom.thread_summary.schedule import thread_summary_message_count_hint
from mindroom.turn_origin import TurnIntent

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence

    import nio
    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.conversation_state_writer import ConversationStateWriter, ReplySession
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.event_journal.approval_store import ApprovalStore
    from mindroom.history.types import HistoryScope
    from mindroom.judgment.state import JudgmentMessage
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.knowledge.utils import KnowledgeAccessSupport
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.message_target import MessageTarget
    from mindroom.participation import ParticipationGate
    from mindroom.post_response_effects import PostResponseEffectsDeps, PostResponseEffectsSupport, ResponseOutcome
    from mindroom.reply_scope import ReplyRuntime, SpanHandle
    from mindroom.reply_turn.request import ResponseRequest
    from mindroom.response_payload_preparation import ResponsePayloadPreparer
    from mindroom.tool_system.runtime_context import ToolRuntimeSupport
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from mindroom.turn_origin import TurnOrigin


def existing_event(request: ResponseRequest) -> tuple[str | None, bool]:
    """Return the event this response writes over, and whether that event is a placeholder rather than an answer.

    Inside a span its reply's records say. Before the claim, the request names
    the event it was dispatched for: an interactive selection's acknowledgement
    is the placeholder its answer fills, while a regeneration or an approval
    resume replaces an answer.
    """
    handle = current_span()
    if handle is None:
        event_id = request.existing_event_id
        return event_id, event_id is not None and request.interactive_span_id is not None
    reply = handle.reply
    replaces_answer = handle.span.kind in {rl.SpanKind.REGENERATION, rl.SpanKind.APPROVAL_RESUME}
    return reply.event_id, reply.event_id is not None and (not replaces_answer or reply.placeholder_only)


def existing_event_id(request: ResponseRequest) -> str | None:
    """Return the event this response writes over, from its reply's records once claimed."""
    return existing_event(request)[0]


DECLINED = FinalDeliveryOutcome(ending=Suppressed(Suppression.PARTICIPATION_DECLINED))


@dataclass(frozen=True)
class ReplyTurnDeps:
    """The collaborators every owner of a reply turn shares: the response runner, its locked turns, and its approvals."""

    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str
    matrix_full_id: str
    resolver: ConversationResolver
    tool_runtime: ToolRuntimeSupport
    knowledge_access: KnowledgeAccessSupport
    delivery_gateway: DeliveryGateway
    post_response_effects: PostResponseEffectsSupport
    state_writer: ConversationStateWriter
    request_preparer: ResponsePayloadPreparer
    approval_store: ApprovalStore
    retry_approval_sources: Callable[[str, tuple[str, ...]], None]
    # Settles journal sources no reply took, delivering the replies their settlement ends.
    settle_ignored_sources: Callable[[tuple[str, ...]], Awaitable[None]]
    approval_runtime_generation: str
    redacted_history_events: Callable[[MessageTarget, tuple[str, ...]], Awaitable[Mapping[str, str | None]]]
    # This bot instance's owner of durable reply records.
    replies: ReplyRuntime


@dataclass
class LockedTurns:
    """Settle one locked reply turn and build what its settlement runs, for every owner of a reply turn."""

    deps: ReplyTurnDeps

    def client(self) -> nio.AsyncClient:
        """Return the current Matrix client required for response coordination."""
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for response coordination"
            raise RuntimeError(msg)
        return client

    def with_contact(self, request: ResponseRequest) -> ResponseRequest:
        """Return the request carrying the contact of this agent's turns in its room, read once as the turn starts."""
        contact = contact_turn(self.deps.runtime.config, self.deps.runtime_paths, request.room_id, self.deps.agent_name)
        return replace(request, contact=contact)

    def is_quiet(self, request: ResponseRequest) -> bool:
        """Return whether a turn shows no progress in its room: a silent schedule's turn, or a contact turn."""
        return is_silent_schedule_response(request) or request.contact is not None

    def knowledge_refresh_scheduler(self) -> KnowledgeRefreshScheduler:
        """Return the runtime's knowledge refresh scheduler."""
        return self.deps.runtime.orchestrator.knowledge_refresh_scheduler

    def post_response_deps(
        self,
        request: ResponseRequest,
        *,
        queue_memory_persistence: Callable[[], None] | None = None,
        queue_skill_review: Callable[[str], Awaitable[None]] | None = None,
        persist_response_event_id: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> PostResponseEffectsDeps:
        """Build post-response effect deps bound to one request's room."""
        return self.deps.post_response_effects.build_deps(
            room_id=request.room_id,
            membership_turn_id=request.response_envelope.source_event_id,
            queue_memory_persistence=queue_memory_persistence,
            queue_skill_review=queue_skill_review,
            notify_response_finished=self.response_finished_notifier(request.sources.logical_source_event_ids),
            persist_response_event_id=persist_response_event_id,
        )

    def response_finished_notifier(self, source_event_ids: Sequence[str]) -> Callable[[], None]:
        """Return the callback that lets automations verify their runs and budgets count the new spend."""
        orchestrator = self.deps.runtime.orchestrator

        def notify() -> None:
            orchestrator.budgets.response_finished()
            orchestrator.automations.response_finished(source_event_ids)

        return notify

    def _log_delivery_failure(self, *, response_kind: str, error: Exception) -> None:
        """Log one response delivery failure with its raw error text; the attempt runner logged its traceback."""
        self.deps.logger.error(
            "Error in response delivery",
            response_kind=response_kind,
            failure_reason=str(error),
            error_type=error.__class__.__name__,
        )

    def thread_summary_message_count_hint(self, request: ResponseRequest) -> int:
        """Return the thread's size after this reply, counted from the request's visible history; it can over-count."""
        return thread_summary_message_count_hint(
            request.thread_history,
            trusted_sender_ids=current_internal_sender_ids(self.deps.runtime.config, self.deps.runtime_paths),
        )

    def memory_persistence(
        self,
        *,
        agent_name: str | list[str],
        session_id: str,
        execution_identity: ToolExecutionIdentity,
        prompt: str,
        thread_history: Sequence[ResolvedVisibleMessage],
        user_id: str | None,
        hook_source: str | None,
        origin: TurnOrigin,
    ) -> Callable[[], None] | None:
        """Build the completed-turn Mem0 or team memory handoff, or None for a turn whose prompt is not memory."""
        # An automation's maintenance turn, such as an unreviewed memory proposal, is not memory, and a job follow-up's
        # prompt is MindRoom's instruction to collect the results, not conversation to extract.
        if is_automation_hook_source(hook_source) or origin.intent is TurnIntent.JOB_FOLLOW_UP:
            return None
        # A file-memory agent's conversation is saved by auto-flush from stored sessions; a team's goes to its shared
        # memory.
        if isinstance(agent_name, str) and memory_backend(self.deps.runtime.config, agent_name) != "mem0":
            return None

        def queue() -> None:
            create_background_task(
                store_conversation_memory(
                    prompt,
                    agent_name,
                    session_id,
                    self.deps.runtime.config,
                    self.deps.runtime_paths,
                    thread_history,
                    user_id,
                    execution_identity=execution_identity,
                ),
                name=f"memory_save_{agent_name if isinstance(agent_name, str) else 'team'}_{session_id}",
                owner=self.deps.runtime,
            )

        return queue

    async def request_remains_authorized(
        self,
        request: ResponseRequest,
        *,
        reply_entity_names: tuple[str, ...] = (),
    ) -> bool:
        """Recheck one requester after serialized lifecycle admission."""
        requester_id = request.response_envelope.requester_id
        entity_names = reply_authorization_entity_names(
            self.deps.runtime.config,
            self.deps.agent_name,
            reply_entity_names,
        )
        if is_sender_allowed_for_entity_replies_in_room(
            requester_id,
            entity_names,
            self.deps.runtime.config,
            request.room_id,
            self.deps.runtime_paths,
            self.deps.runtime.agent_reply_memberships,
            require_resolved_membership=True,
        ):
            return True
        self.deps.logger.info(
            "response_suppressed_after_authorization_revocation",
            source_event_id=request.response_envelope.source_event_id,
            requester_id=requester_id,
            room_id=request.room_id,
            entity_names=entity_names,
        )
        return False

    def build_persist_response_event_id_effect(
        self,
        *,
        session_id: str,
        scope: HistoryScope,
        execution_identity: ToolExecutionIdentity | None,
    ) -> Callable[[str, str], Awaitable[None]]:
        """Build the response-event persistence callback for one session-backed response."""

        async def persist_response_event_id(run_id: str, response_event_id: str) -> None:
            await self.deps.state_writer.apersist_response_event_id_in_session_run(
                execution_identity=execution_identity,
                scope=scope,
                session_id=session_id,
                run_id=run_id,
                response_event_id=response_event_id,
            )

        return persist_response_event_id

    async def refresh_model_history(
        self,
        request: ResponseRequest,
        *,
        exclude_event_id: str | None = None,
    ) -> ResponseRequest:
        """Refresh model-facing thread history once this turn owns the lifecycle lock."""
        if request.thread_id is None:
            return request

        try:
            refreshed_history = await self.deps.resolver.fetch_thread_history(
                request.room_id,
                request.thread_id,
            )
        except Exception as exc:
            if request.requires_model_history_refresh:
                raise
            self.deps.logger.warning(
                "Failed to refresh thread history after lock; continuing with existing history",
                room_id=request.room_id,
                thread_id=request.thread_id,
                error=str(exc),
            )
            return replace(request, requires_model_history_refresh=True)
        if exclude_event_id is not None:
            filtered_history = [message for message in refreshed_history if message.event_id != exclude_event_id]
            if len(filtered_history) != len(refreshed_history):
                refreshed_history = replace(refreshed_history, messages=filtered_history)
        return replace(
            request,
            thread_history=refreshed_history,
            requires_model_history_refresh=False,
        )

    async def refresh_mid_turn_context_for_approval(self, request: ResponseRequest) -> None:
        """Approval resumptions bypass ordinary payload preparation."""
        agent = self.deps.runtime.config.agents.get(request.response_envelope.agent_name)
        if agent is not None and agent.mid_turn is not None:
            # Refresh only the judge's public context; native continuation input stays unchanged.
            refreshed = await self.refresh_model_history(request)
            self.bind_mid_turn_context(refreshed)

    def bind_mid_turn_context(self, request: ResponseRequest) -> None:
        """Only successfully refreshed public history may authorize continuing tools."""
        bind_mid_turn_conversation_context(lambda: self.judgment_context(request))

    def judgment_context(self, request: ResponseRequest) -> tuple[JudgmentMessage, ...] | None:
        """Return the public thread text judges read, or None until the history is refreshed."""
        if request.requires_model_history_refresh:
            return None
        return conversation_context_before_sources(
            request.thread_history,
            source_event_ids=request.sources.logical_source_event_ids,
            thread_id=request.thread_id,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
        )

    async def _finalize_locked_outcome(
        self,
        lifecycle: ResponseLifecycle,
        final_delivery_outcome: FinalDeliveryOutcome,
        *,
        post_response_outcome: ResponseOutcome,
        post_response_deps: Callable[[], PostResponseEffectsDeps],
    ) -> None:
        """Finalize the lifecycle, reporting a cancellation that lands during finalization as the turn's ending."""
        try:
            await lifecycle.finalize(
                final_delivery_outcome,
                build_post_response_outcome=lambda _final_outcome: post_response_outcome,
                post_response_deps=post_response_deps,
            )
        except asyncio.CancelledError as exc:
            if current_task_is_process_shutdown():
                raise
            await lifecycle.finalize(
                replace(
                    final_delivery_outcome,
                    ending=Cancelled(classify_cancel_source(exc)),
                    event_id=final_delivery_outcome.final_visible_event_id,
                    is_visible_response=final_delivery_outcome.final_visible_event_id is not None,
                    delivery_kind=None,
                    interactive_metadata=None,
                ),
                build_post_response_outcome=lambda _final_outcome: post_response_outcome,
                post_response_deps=post_response_deps,
            )
            raise

    async def _attempt_outcome(
        self,
        request: ResponseRequest,
        *,
        target: MessageTarget,
        response_function: Callable[[str | None], Coroutine[Any, Any, FinalDeliveryOutcome]],
        approval_suspension_handler: Callable[[ResponsePausedForApproval], Awaitable[FinalDeliveryOutcome]] | None,
        participation: ParticipationGate | None,
        response_kind: str,
    ) -> FinalDeliveryOutcome:
        """Run the turn's attempt and return how it ended: what it delivered, its pause, its cancellation, or its failure.

        A process shutdown raises, and so does a cancellation of this task, after which the caller still settles the
        turn as cancelled.
        """
        try:
            # The attempt runs against the event the turn already adopted.
            attempt = await self._run_cancellable_response(
                target=target,
                response_function=response_function,
                existing_event_id=existing_event_id(request),
                user_id=request.user_id,
                show_stop_button=not self.is_quiet(request),
            )
        except ResponsePausedForApproval as error:
            if approval_suspension_handler is None:
                raise
            try:
                return await approval_suspension_handler(error)
            except Exception as suspension_error:
                self.deps.logger.exception("approval_suspension_failed", error=str(suspension_error))
                # The span's exit shows the failure unless the pause already committed, which hands the reply to its
                # approval's failure settlement.
                return FinalDeliveryOutcome(
                    ending=Failed(FailureCause.APPROVAL_NOT_STARTED),
                    event_id=existing_event_id(request),
                )
        except Exception as error:
            if current_task_is_process_shutdown():
                raise
            if skip_unapproved_attempt(participation, reason="run_failed_before_decision") is not None:
                # A failure before the participation decision stays quiet: it declines the turn.
                self.deps.logger.exception("Response skipped before participation", error=str(error))
                return DECLINED
            self._log_delivery_failure(response_kind=response_kind, error=error)
            if isinstance(error, PausePresentationError):
                # The run paused, but its reply could not show the pause: no approval exists for it.
                return FinalDeliveryOutcome(ending=Failed(FailureCause.APPROVAL_NOT_STARTED))
            # A stream's failure wraps the error the reader should see.
            shown = error.error if isinstance(error, StreamingDeliveryError) else error
            return FinalDeliveryOutcome(ending=Failed(FailureCause.ERROR, failure_text(shown)))
        return FinalDeliveryOutcome(ending=attempt) if isinstance(attempt, Cancelled) else attempt

    async def run_and_settle_locked_response(
        self,
        request: ResponseRequest,
        *,
        target: MessageTarget,
        lifecycle: ResponseLifecycle,
        progress: TurnProgress,
        response_function: Callable[[str | None], Coroutine[Any, Any, FinalDeliveryOutcome]],
        build_post_response_outcome: Callable[[FinalDeliveryOutcome], ResponseOutcome],
        post_response_deps: Callable[[], PostResponseEffectsDeps],
        approval_suspension_handler: Callable[[ResponsePausedForApproval], Awaitable[FinalDeliveryOutcome]]
        | None = None,
        participation: ParticipationGate | None = None,
        reply_session: ReplySession | None = None,
    ) -> None:
        """Run generation and settle its terminal lifecycle exactly once: the one place a locked turn's ending is decided.

        ``reply_session`` names the stored session the reply writes; when one of its writes creates that session's
        row, ``session:started`` fires once the attempt settles, before the lifecycle's finalization.
        """
        cancelled: asyncio.CancelledError | None = None
        async with session_started_on_creation(
            reply_session,
            correlation_id=lifecycle.identity.correlation_id,
        ):
            try:
                outcome = await self._attempt_outcome(
                    request,
                    target=target,
                    response_function=response_function,
                    approval_suspension_handler=approval_suspension_handler,
                    participation=participation,
                    response_kind=lifecycle.identity.response_kind,
                )
            except asyncio.CancelledError as error:
                if current_task_is_process_shutdown():
                    raise
                # The turn still settles as cancelled; the cancellation then propagates.
                cancelled = error
                outcome = FinalDeliveryOutcome(ending=Cancelled(classify_cancel_source(error)))
        handle = current_span()
        if handle is not None and not handle.exited:
            outcome = await self._end_span_after_outcome(
                handle,
                outcome,
                target=target,
                delivery_started=progress.delivery_started,
                contact_turn=request.contact is not None,
            )
        progress.ending = outcome.ending
        await self._finalize_locked_outcome(
            lifecycle,
            outcome,
            post_response_outcome=build_post_response_outcome(outcome),
            post_response_deps=post_response_deps,
        )
        if cancelled is not None:
            raise cancelled

    async def _end_span_after_outcome(
        self,
        handle: SpanHandle,
        outcome: FinalDeliveryOutcome,
        *,
        target: MessageTarget,
        delivery_started: bool,
        contact_turn: bool,
    ) -> FinalDeliveryOutcome:
        """End a span whose settled outcome wrote no terminal row; return the ending its records committed."""
        gateway = self.deps.delivery_gateway
        match outcome.ending:
            case AwaitingApproval() | Owed():
                # The approval continuation, or the outbox, owns the sources from here.
                await gateway.end_reply_span(handle, release_decision(handle))
                return outcome
            case Completed() | Suppressed():
                # Nothing was answered: a placeholder goes, substantive content stays.
                await gateway.end_reply_span(handle, suppress_decision(handle))
                return outcome
            case Cancelled() | Failed() as ending:
                noted = await gateway.end_interrupted_span(
                    handle,
                    target,
                    ending=ending,
                    delivery_started=delivery_started,
                    contact_turn=contact_turn,
                )
                return outcome if noted is None else noted

    def build_lifecycle(self, request: ResponseRequest, *, response_kind: str) -> ResponseLifecycle:
        """Build one request's response lifecycle, with the identity its hooks and delivery carry."""
        return ResponseLifecycle(
            ResponseLifecycleDeps(
                response_hooks=self.deps.delivery_gateway.deps.response_hooks,
                logger=self.deps.logger,
                process_shutdown_requested=current_task_is_process_shutdown,
            ),
            identity=ResponseIdentity(
                response_kind=response_kind,
                response_envelope=request.response_envelope,
                sources=request.sources,
                correlation_id=correlation_id_for_request(request),
                participating_agent_names=request.participating_agent_names or (self.deps.agent_name,),
                contact_turn=request.contact is not None,
            ),
            pipeline_timing=request.pipeline_timing,
        )

    async def _run_cancellable_response(
        self,
        *,
        target: MessageTarget,
        response_function: Callable[[str | None], Coroutine[Any, Any, FinalDeliveryOutcome]],
        existing_event_id: str | None,
        user_id: str | None,
        show_stop_button: bool,
    ) -> FinalDeliveryOutcome | Cancelled:
        """Run one response-generation attempt with cancellation support; return what it delivered, or its cancellation."""
        return await ResponseAttemptRunner(
            ResponseAttemptDeps(
                client=self.client(),
                logger=self.deps.logger,
                show_stop_button=lambda: show_stop_button and self.deps.runtime.config.defaults.show_stop_button,
            ),
        ).run(
            ResponseAttemptRequest[FinalDeliveryOutcome](
                target=target,
                response_function=response_function,
                existing_event_id=existing_event_id,
                user_id=user_id,
                span=self._span_attempt(),
            ),
        )

    def _span_attempt(self) -> SpanAttempt:
        """Return the current reply span's ownership of an attempt: its Stop and its Stop button."""
        handle = current_span()
        assert handle is not None, "every response attempt runs in a reply span"

        async def reply_event() -> str | None:
            await handle.reply_shown.wait()
            return handle.reply.event_id

        return SpanAttempt(
            register=partial(handle.runtime.spans.register, handle.span_id),
            add_stop_button=lambda message_id: self.deps.delivery_gateway.add_reply_stop_button(handle, message_id),
            reply_event=reply_event,
        )
