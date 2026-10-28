"""The approval lifecycle of a response: pause for tool approval, resume the approved run, and recover it."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from uuid import uuid4

from mindroom import reply_lifecycle as rl
from mindroom.agent_cli.cli_approval_waits import CliApprovalWaits
from mindroom.approval.execution import AgentApprovalExecution
from mindroom.approval.receipt_readers import approval_receipt_context
from mindroom.approval.response import (
    ApprovalResponseCoordinator,
    continuation_target,
    identify_approval_tools,
    plan_approval_calls,
    require_ordered_pause_presentation,
)
from mindroom.authorization import is_sender_allowed_for_entity_replies_in_room
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.constants import (
    STREAM_STATUS_APPROVAL_PENDING,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
)
from mindroom.delivery_gateway import (
    EditTextRequest,
    FinalDeliveryRequest,
    ResponseIdentity,
    SendTextRequest,
)
from mindroom.event_journal import (
    INTERRUPTED_FAILURE_REASON,
    ApprovalAdvance,
    ApprovalContinuation,
    ApprovalMemoryTurn,
    MatrixDelivery,
)
from mindroom.event_journal import (
    ApprovalDecision as ContinuationDecision,
)
from mindroom.final_delivery import FinalDeliveryOutcome
from mindroom.hooks import MessageEnvelope
from mindroom.interactive import InteractiveMetadata
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.presence import should_use_streaming
from mindroom.orchestration.runtime import (
    cancel_failure_reason,
    cancel_source_from_failure_reason,
    classify_cancel_source,
    current_task_is_process_shutdown,
)
from mindroom.post_response_effects import ResponseOutcome
from mindroom.reply_presentation import (
    AGENT_PLACEHOLDER,
    TEAM_PLACEHOLDER,
    NoteKind,
    Presentation,
    current_answer,
    decode_presentation,
    note_segment,
    render_body,
)
from mindroom.reply_scope import (
    ReplyWriteRefusedError,
    SpanHandle,
    SpanSlot,
    current_span,
    initial_write,
    interruption_note,
    pause_decision,
    pause_write,
)
from mindroom.response_runner import (
    DeliveryProgress,
    ResponseRequest,
    correlation_id_for_request,
    merge_response_extra_content,
    reply_authorization_entity_names,
    response_typing_indicator,
    response_typing_log_context,
)
from mindroom.response_turn import CompletedApprovalRun, PausedAnswer, PausedAttempt
from mindroom.streaming import PROGRESS_PLACEHOLDER
from mindroom.teams.members import select_model_for_team
from mindroom.teams.resume import continue_paused_team_run
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.events import body_without_tool_markers
from mindroom.tool_system.runtime_context import (
    LiveToolDispatchContext,
    ToolDispatchContext,
    runtime_context_from_dispatch_context,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
    from contextlib import AbstractAsyncContextManager

    import nio

    from mindroom.history.types import HistoryScope
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.llm.runs import RunRequirement
    from mindroom.message_target import MessageTarget
    from mindroom.post_response_effects import PostResponseEffectsDeps
    from mindroom.response_lifecycle import ResponseLifecycle, ResponseLifecycleCoordinator
    from mindroom.response_runner import PreparedResponseRuntime, ResponseRunnerDeps
    from mindroom.streaming import ProgressPublisher
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from mindroom.turn_origin import TurnOrigin

# How often a message waiting behind an approval rechecks that the approval still exists.
_APPROVAL_HOLD_RECHECK_SECONDS = 30.0
# A resume span that ended this way wrote its own ending; only a restart's or a release's hands work back.
_UNANSWERED_RESUME_OUTCOMES = frozenset({rl.SpanOutcome.FAILED, rl.SpanOutcome.CANCELLED, rl.SpanOutcome.SUPPRESSED})


def _approval_interruption_cancel_source(reason: str) -> Literal["sync_restart", "interrupted"] | None:
    """Recover the cancellation provenance persisted for an interrupted approval."""
    cancel_source = cancel_source_from_failure_reason(reason)
    if cancel_source == "user_stop" or cancel_failure_reason(cancel_source) != reason:
        return None
    return cancel_source


def _terminal_status_of_span(span: rl.Span) -> Literal["completed", "cancelled", "error"]:
    """Return the status a frozen approval FINAL reports, from the span that wrote it."""
    if span.outcome is rl.SpanOutcome.COMPLETED:
        return "completed"
    return "cancelled" if span.outcome is rl.SpanOutcome.CANCELLED else "error"


def _paused_answer() -> PausedAnswer:
    """Return the answer the current approval resume continues, as its reply's records keep it."""
    handle = current_span()
    assert handle is not None, "every approval resume runs in its reply's resume span"
    segment = current_answer(handle.base, handle.span_id)
    if segment is None:
        return PausedAnswer()
    # The resume completes these entries in place; the span's base keeps what the pause showed.
    return PausedAnswer(text=segment.text, tool_trace=deepcopy(segment.tool_trace), team_state=segment.team_state)


def _paused_presentation(handle: SpanHandle, paused: PausedAttempt, *, waiting_text: str | None) -> Presentation:
    """Return what a span's reply shows while it waits for approval."""
    return handle.presentation(
        paused.response_text or "",
        tuple(paused.tool_trace),
        team_state=paused.response_presentation_state or None,
        trailing_note=None if waiting_text is None else note_segment(NoteKind.APPROVAL_WAIT, waiting_text),
    )


@dataclass(frozen=True)
class _ShownPause:
    """Where a pause shows before its continuation exists."""

    response_event_id: str | None
    delivery_kind: Literal["sent", "edited"] | None
    final_visible_body: str | None
    # The span, its paused presentation, and the stage of the row that shows
    # it with the continuation (``None`` when its create already showed it).
    handle: SpanHandle
    reply_pause: Presentation
    reply_stage: rl.WriteStage | None = None


class _ResponseHost(Protocol):
    """What the approval lifecycle uses of the response runner that hosts it."""

    deps: ResponseRunnerDeps

    def client(self) -> nio.AsyncClient: ...

    def knowledge_refresh_scheduler(self) -> KnowledgeRefreshScheduler | None: ...

    def thread_summary_message_count_hint(self, request: ResponseRequest) -> int: ...

    def response_identity(self, request: ResponseRequest, *, response_kind: str) -> ResponseIdentity: ...

    def build_lifecycle(self, *, identity: ResponseIdentity, request: ResponseRequest) -> ResponseLifecycle: ...

    def response_finished_notifier(self, source_event_ids: Sequence[str]) -> Callable[[], None] | None: ...

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
    ) -> Callable[[], None] | None: ...

    def build_persist_response_event_id_effect(
        self,
        *,
        session_id: str,
        scope: HistoryScope,
        execution_identity: ToolExecutionIdentity | None,
    ) -> Callable[[str, str], Awaitable[None]]: ...

    async def request_remains_authorized(self, request: ResponseRequest) -> bool: ...

    async def refresh_mid_turn_context_for_approval(self, request: ResponseRequest) -> None: ...

    async def run_in_tool_context[T](
        self,
        *,
        tool_dispatch: ToolDispatchContext,
        operation: Callable[[], Awaitable[T]],
    ) -> T: ...

    async def run_and_settle_locked_response(
        self,
        request: ResponseRequest,
        *,
        target: MessageTarget,
        lifecycle: ResponseLifecycle,
        progress: DeliveryProgress,
        response_function: Callable[[str | None], Coroutine[Any, Any, None]],
        user_id: str | None,
        build_post_response_outcome: Callable[[FinalDeliveryOutcome], ResponseOutcome],
        post_response_deps: PostResponseEffectsDeps | Callable[[], PostResponseEffectsDeps],
    ) -> str | None: ...

    async def run_locked_response_lifecycle(
        self,
        request: ResponseRequest,
        *,
        response_kind: str,
        locked_operation: Callable[[MessageTarget, object], Awaitable[str | None]],
        signal_queued_message: bool = True,
    ) -> str | None: ...

    def track_inbox_response(
        self,
        response: Coroutine[Any, Any, None],
        *,
        name: str,
        room_id: str,
        recovery_proof_ready: Callable[[], bool | Awaitable[bool]],
        source_event_ids: tuple[str, ...] = (),
    ) -> asyncio.Task[None]: ...


@dataclass
class ApprovalContinuationRunner:
    """Pause a response for tool approval, resume its approved run, and recover it after a failure or restart."""

    host: _ResponseHost
    lifecycle_coordinator: ResponseLifecycleCoordinator
    _approval_responses: ApprovalResponseCoordinator = field(init=False, repr=False)
    _approval_execution: AgentApprovalExecution = field(init=False, repr=False)
    _cli_approval_waits: CliApprovalWaits = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Bind response-side approval collaborators to the event journal."""
        self._approval_responses = ApprovalResponseCoordinator(
            config=lambda: self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            store=self.deps.approval_store,
            load_span=self.deps.replies.store.replies.span,
            delivery_gateway=self.deps.delivery_gateway,
            retry_sources=self.deps.retry_approval_sources,
            finish_approval=self.deps.replies.finish_approval,
            release_approval=self.deps.replies.release_approval,
        )
        self._cli_approval_waits = CliApprovalWaits(
            store=self.deps.approval_store,
            responses=self._approval_responses,
            runtime_generation=self.deps.approval_runtime_generation,
            retry_sources=self.deps.retry_approval_sources,
            claim=self._claim_in_place_approval,
            advance=self._advance_in_place_approval,
        )
        self._approval_execution = AgentApprovalExecution(
            config=lambda: self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            client=self.host.client,
            tool_runtime=self.deps.tool_runtime,
            knowledge_access=self.deps.knowledge_access,
            refresh_scheduler=self.host.knowledge_refresh_scheduler,
        )

    @property
    def deps(self) -> ResponseRunnerDeps:
        """Return the hosting runner's current collaborators."""
        return self.host.deps

    async def _failed_approval_handoff(
        self,
        approval_id: str,
        *,
        error: BaseException,
    ) -> FinalDeliveryOutcome | None:
        """Return suspension ownership only after failure intent is durable."""
        reason = (
            "Tool approval suspension was cancelled."
            if isinstance(error, asyncio.CancelledError)
            else str(error) or "Tool approval suspension failed."
        )
        continuation = await self._approval_responses.fail_publication(approval_id, reason=reason)
        if continuation is None or continuation.state != "failing":
            return None
        response_event_id = continuation.response_event_id
        return FinalDeliveryOutcome(
            terminal_status="suspended",
            event_id=response_event_id,
            is_visible_response=response_event_id is not None,
            extra_content={STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING},
        )

    async def suspend_for_approval(
        self,
        paused: PausedAttempt,
        *,
        request: ResponseRequest,
        target: MessageTarget,
        execution_identity: ToolExecutionIdentity,
        entity_kind: Literal["agent", "team"],
        history_scope: HistoryScope,
        show_tool_calls: bool,
        team_member_names: tuple[str, ...] = (),
        team_mode: str | None = None,
    ) -> FinalDeliveryOutcome:
        """Persist a paused run, expose its cards, and leave the response lock."""
        requester_id = request.user_id or execution_identity.requester_id
        if requester_id is None:
            msg = "Approval continuation requires the original requester identity"
            raise RuntimeError(msg)
        if paused.cli_call is not None:
            current = await self.deps.approval_store.approval_continuation_for_source(
                request.response_envelope.source_event_id,
            )
            if (
                current is not None
                and current.state in {"waiting", "ready"}
                and current.run_id == paused.run_id
                and current.session_id == paused.session_id
                and current.cli_call == paused.cli_call
            ):
                await self._end_expired_in_place_wait()
                return FinalDeliveryOutcome(
                    terminal_status="suspended",
                    event_id=current.response_event_id,
                    is_visible_response=True,
                    tool_trace=tuple(paused.tool_trace) if show_tool_calls else (),
                )
        require_ordered_pause_presentation(paused, show_tool_calls=show_tool_calls)
        identified_tools = identify_approval_tools(
            paused,
            default_agent_name=self.deps.agent_name,
        )
        approval_id = uuid4().hex
        try:
            plan = await plan_approval_calls(
                identified_tools,
                config=self.deps.runtime.config,
                runtime_paths=self.deps.runtime_paths,
                requester_id=requester_id,
                toolkit_owners=paused.toolkit_owners,
                room_id=target.room_id,
                thread_id=target.resolved_thread_id,
            )
            approval_pending = plan.waiting_text is not None
            visible_tool_trace = tuple(paused.tool_trace) if show_tool_calls else ()
            snapshot_text = paused.response_text
            visible_text = (
                paused.acknowledged_response_text
                if paused.acknowledged_response_text is not None
                else snapshot_text or plan.waiting_text or PROGRESS_PLACEHOLDER
            )
            stream_status = STREAM_STATUS_APPROVAL_PENDING if approval_pending else STREAM_STATUS_PENDING
            shown = await self._show_pause(
                paused,
                waiting_text=plan.waiting_text,
                target=target,
                visible_text=visible_text,
                stream_status=stream_status,
                visible_tool_trace=visible_tool_trace,
            )
            response_event_id = shown.response_event_id
            if response_event_id is None:
                msg = "Could not publish the suspended approval response"
                raise RuntimeError(msg)  # noqa: TRY301

            continuation_state: Literal["waiting", "ready"] = (
                "ready" if all(call.decision is not None for call in plan.calls) else "waiting"
            )
            draft = ApprovalContinuation(
                approval_id=approval_id,
                cli_call=deepcopy(paused.cli_call),
                run_id=paused.run_id,
                session_id=paused.session_id,
                entity_kind=entity_kind,
                entity_name=self.deps.agent_name,
                room_id=target.room_id,
                thread_id=target.resolved_thread_id,
                requester_id=requester_id,
                response_event_id=response_event_id,
                sources=request.sources,
                calls=plan.calls,
                state=continuation_state,
                delegation_storage_bindings=paused.delegation_storage_bindings,
                show_tool_calls=show_tool_calls,
                execution_identity=execution_identity,
                runtime_model_name=paused.runtime_model_name,
                continuation_count=paused.continuation_count,
                team_member_names=team_member_names,
                team_member_model_names=paused.team_member_model_names,
                team_mode=team_mode,
                request_body=request.response_envelope.body,
                attachment_ids=tuple(request.attachment_ids or ()),
                mentioned_agents=request.response_envelope.mentioned_agents,
                hook_source=request.response_envelope.hook_source,
                message_received_depth=request.response_envelope.message_received_depth,
                dispatch_policy_source_kind=request.response_envelope.dispatch_policy_source_kind,
                correlation_id=correlation_id_for_request(request),
                history_scope=history_scope,
                origin=request.response_envelope.origin,
                memory_prompt=request.prompt,
                memory_thread_history=tuple(
                    ApprovalMemoryTurn(
                        sender=message.sender,
                        body=body_without_tool_markers(message.body, message.content),
                    )
                    for message in request.thread_history
                ),
                thread_summary_message_count_hint=self.host.thread_summary_message_count_hint(request),
                runtime_generation=(self.deps.approval_runtime_generation if continuation_state == "waiting" else None),
            )
            # Later messages of the conversation wait while the approval is pending, as behind a running reply.
            # It is held before the pause commits, so the approval's end, which releases it, cannot come first.
            self.lifecycle_coordinator.hold_for_approval(approval_id, target)
            if not await self._pause_reply(
                shown.handle,
                draft,
                shown=shown.reply_pause,
                stage=shown.reply_stage,
                # A response-local CLI wait keeps its span running while it waits.
                in_place=paused.cli_call is not None,
                target=target,
                text=visible_text,
                stream_status=stream_status,
                tool_trace=visible_tool_trace,
            ):
                msg = "Could not publish the suspended approval response"
                raise RuntimeError(msg)  # noqa: TRY301
            continuation = await self.deps.approval_store.approval_continuation(approval_id)
            if continuation is None or continuation.state != continuation_state:
                msg = "Approval continuation lost its journal source ownership"
                raise RuntimeError(msg)  # noqa: TRY301

            await self._approval_responses.publish_generation(
                continuation,
                plan,
                target=target,
                failure_reason="Approval card creation failed",
            )
            return FinalDeliveryOutcome(
                terminal_status="suspended",
                event_id=response_event_id,
                is_visible_response=True,
                final_visible_body=shown.final_visible_body,
                delivery_kind=shown.delivery_kind,
                tool_trace=visible_tool_trace,
                extra_content={STREAM_STATUS_KEY: stream_status},
            )
        except (asyncio.CancelledError, Exception) as error:
            handoff = await self._failed_approval_handoff(
                approval_id,
                error=error,
            )
            if await self.deps.approval_store.approval_continuation(approval_id) is None:
                # No approval is pending, or its end already ran: nothing holds the conversation.
                self.lifecycle_coordinator.release_approval_hold(approval_id)
            if handoff is not None:
                return handoff
            raise

    async def _show_pause(
        self,
        paused: PausedAttempt,
        *,
        waiting_text: str | None,
        target: MessageTarget,
        visible_text: str,
        stream_status: str,
        visible_tool_trace: tuple[ToolTraceEntry, ...],
    ) -> _ShownPause:
        """Show a pause before its continuation exists, or prepare the reply row that shows it."""
        shows_text = waiting_text is not None or bool(paused.response_text)
        initial = SendTextRequest(
            target=target,
            response_text=visible_text,
            extra_content={STREAM_STATUS_KEY: stream_status},
            tool_trace=list(visible_tool_trace) or None,
        )
        handle = current_span()
        assert handle is not None, "a pause runs in its reply's span"
        # The reply's records show the pause; its row is written with the continuation.
        reply_pause = _paused_presentation(handle, paused, waiting_text=waiting_text)
        reply = await handle.runtime.store.replies.load(handle.reply_id)
        assert reply is not None, "a span's reply exists while it pauses"
        if reply.event_id is None:
            # The pause is the reply's first visible message, so its create shows it.
            created = await self.deps.delivery_gateway.send_text(
                replace(initial, reply_write=initial_write(handle, reply_pause, placeholder_only=False)),
            )
            return _ShownPause(created, "sent", visible_text, handle=handle, reply_pause=reply_pause)
        if not shows_text:
            return _ShownPause(reply.event_id, None, None, handle=handle, reply_pause=reply_pause)
        return _ShownPause(
            reply.event_id,
            "edited",
            visible_text,
            handle=handle,
            reply_pause=reply_pause,
            reply_stage=rl.WriteStage.EDIT,
        )

    async def _advance_reply_pause(
        self,
        handle: SpanHandle,
        target: MessageTarget,
        advance: ApprovalAdvance,
        paused: PausedAttempt,
        *,
        visible_text: str,
        stream_status: str,
        tool_trace: tuple[ToolTraceEntry, ...],
        waiting_text: str | None,
        in_place: bool = False,
    ) -> tuple[ApprovalContinuation | None, bool]:
        """Advance the continuation and pause the span's reply with the pause row, in one transaction."""
        reply = await handle.runtime.store.replies.load(handle.reply_id)
        assert reply is not None
        assert reply.event_id is not None, "a resumed reply shows its paused event"
        try:
            shown = await self._edit_pause_row(
                handle,
                advance,
                target=target,
                event_id=reply.event_id,
                shown=_paused_presentation(handle, paused, waiting_text=waiting_text),
                in_place=in_place,
                text=visible_text,
                stream_status=stream_status,
                tool_trace=tool_trace,
            )
        except ReplyWriteRefusedError:
            # A Stop committed while the pause was prepared; neither was written.
            return None, False
        current = await self.deps.approval_store.approval_continuation(advance.approval_id)
        if current is None or current.generation != advance.claimant_generation + 1:
            return None, False
        # The pause row committed with the advance: recovery sends it if this send
        # did not land, and only a permanent refusal fails the approval.
        return current, shown or current.state == "waiting"

    async def _claim_in_place_approval(
        self,
        current: ApprovalContinuation,
    ) -> ApprovalContinuation | None:
        """Claim a ready continuation for the response waiting on it, resuming its reply in place."""
        handle = current_span()
        assert handle is not None, "a response-local wait runs in its reply's span"
        claimed, applied = await self.deps.approval_store.claim_approval_in_place(
            current.approval_id,
            runtime_generation=self.deps.approval_runtime_generation,
            reply_id=handle.reply_id,
            span_id=handle.span_id,
        )
        if applied is not None:
            await handle.runtime.committed(applied, handle)
        return claimed

    async def _advance_in_place_approval(
        self,
        current: ApprovalContinuation,
        paused: PausedAttempt,
        target: MessageTarget,
    ) -> None:
        """Record the next pause of a run that waits in place, with its reply's pause row."""
        handle = current_span()
        assert handle is not None, "a response-local wait runs in its reply's span"
        await self._approval_responses.advance_pause(
            current,
            paused,
            target=target,
            pending_text=PROGRESS_PLACEHOLDER,
            reply_pause=partial(self._advance_reply_pause, handle, target, in_place=True),
        )

    async def _end_expired_in_place_wait(self) -> None:
        """End the span of a response-local wait that expired; the reply waits for the decision."""
        handle = current_span()
        assert handle is not None, "a response-local wait runs in its reply's span"
        if handle.exited:
            return
        reply = await handle.runtime.store.replies.load(handle.reply_id)
        assert reply is not None
        handle.note_reply(reply)
        await self.deps.delivery_gateway.end_reply_span(
            handle,
            pause_decision(
                handle,
                decode_presentation(reply.presentation),
                in_place=False,
                stage=None,
            ),
        )

    async def _pause_reply(
        self,
        handle: SpanHandle,
        continuation: ApprovalContinuation,
        *,
        shown: Presentation,
        stage: rl.WriteStage | None,
        in_place: bool,
        target: MessageTarget,
        text: str,
        stream_status: str,
        tool_trace: tuple[ToolTraceEntry, ...],
    ) -> bool:
        """Pause the span's reply and create the continuation that holds its run, in one transaction.

        Returns whether the pause was recorded: its row commits with the
        continuation, so recovery sends a row this send left owed, and only a
        permanent refusal fails the approval. A rule that refuses the pause (a
        Stop committed while it was prepared) raises ``ReplyWriteRefusedError``.
        """
        if stage is None:
            return await self.deps.delivery_gateway.pause_shown_reply(
                handle,
                pause_decision(handle, shown, in_place=in_place, stage=None),
                enqueue=partial(self.deps.approval_store.pause_for_approval, continuation),
            )
        shown_now = await self._edit_pause_row(
            handle,
            continuation,
            target=target,
            event_id=continuation.response_event_id,
            shown=shown,
            in_place=in_place,
            text=text,
            stream_status=stream_status,
            tool_trace=tool_trace,
        )
        return shown_now or await self.deps.approval_store.approval_continuation(continuation.approval_id) is not None

    async def _edit_pause_row(
        self,
        handle: SpanHandle,
        hold: ApprovalContinuation | ApprovalAdvance,
        *,
        target: MessageTarget,
        event_id: str,
        shown: Presentation,
        in_place: bool,
        text: str,
        stream_status: str,
        tool_trace: tuple[ToolTraceEntry, ...],
    ) -> bool:
        """Edit the paused reply with its pause row, recorded with the continuation ``hold`` creates or advances.

        A rule that refuses the pause (a Stop committed while it was prepared) raises ``ReplyWriteRefusedError``.
        """
        return await self.deps.delivery_gateway.edit_text(
            EditTextRequest(
                target=target,
                event_id=event_id,
                new_text=text,
                extra_content={STREAM_STATUS_KEY: stream_status},
                tool_trace=list(tool_trace) or None,
                reply_write=pause_write(
                    handle,
                    shown,
                    in_place=in_place,
                    enqueue=partial(self.deps.approval_store.pause_for_approval, hold),
                ),
            ),
        )

    @asynccontextmanager
    async def cli_approval_scope(
        self,
        runtime: PreparedResponseRuntime,
        *,
        request: ResponseRequest,
        progress: DeliveryProgress,
        history_scope: HistoryScope,
    ) -> AsyncIterator[PreparedResponseRuntime]:
        """Attach approval publication to this response's real source and placeholder."""
        context = runtime_context_from_dispatch_context(runtime.tool_dispatch)
        if context is None:
            yield runtime
            return
        async with self._cli_approval_handler_scope(
            request=request,
            progress=progress,
            history_scope=history_scope,
            target=runtime.resolved_target,
            execution_identity=runtime.tool_dispatch.execution_identity,
            show_tool_calls=runtime.show_tool_calls,
            settle_terminal=True,
        ) as handler:
            yield replace(
                runtime,
                tool_dispatch=LiveToolDispatchContext.from_runtime_context(
                    replace(context, cli_approval_handler=handler),
                ),
            )

    def _cli_approval_handler_scope(
        self,
        *,
        request: ResponseRequest,
        progress: DeliveryProgress,
        history_scope: HistoryScope,
        target: MessageTarget,
        execution_identity: ToolExecutionIdentity,
        show_tool_calls: bool,
        settle_terminal: bool,
    ) -> AbstractAsyncContextManager[Callable[[PausedAttempt], Awaitable[tuple[RunRequirement, ...]]]]:
        """Bind this request's approval publication and authorization to its CLI wait scope."""
        return self._cli_approval_waits.scope(
            source_event_ids=request.sources.pending_event_ids,
            progress=progress,
            target=target,
            publish=partial(
                self.suspend_for_approval,
                request=request,
                target=target,
                execution_identity=execution_identity,
                entity_kind="agent",
                history_scope=history_scope,
                show_tool_calls=show_tool_calls,
            ),
            authorize=lambda: self.host.request_remains_authorized(request),
            settle_terminal=settle_terminal,
        )

    async def _execute_claimed_approval(
        self,
        claimed: ApprovalContinuation,
        *,
        request: ResponseRequest,
        target: MessageTarget,
    ) -> tuple[FinalDeliveryOutcome, ApprovalContinuation]:
        """Run and classify one claimed continuation for either lifecycle entry path."""
        if claimed.cli_call is None:
            return await self._deliver_claimed_approval(claimed, request=request, target=target)
        progress = DeliveryProgress()
        async with self._cli_approval_handler_scope(
            request=request,
            progress=progress,
            history_scope=claimed.history_scope or self.deps.state_writer.history_scope(),
            target=target,
            execution_identity=self.deps.tool_runtime.build_execution_identity(
                target=target,
                user_id=claimed.requester_id,
            ),
            show_tool_calls=claimed.show_tool_calls,
            settle_terminal=False,
        ) as handler:
            outcome, current = await self._deliver_claimed_approval(
                claimed,
                request=request,
                target=target,
                cli_approval_handler=handler,
            )
            progress.settle(outcome)
            return outcome, current

    async def _deliver_claimed_approval(
        self,
        claimed: ApprovalContinuation,
        *,
        request: ResponseRequest,
        target: MessageTarget,
        cli_approval_handler: Callable[[PausedAttempt], Awaitable[tuple[RunRequirement, ...]]] | None = None,
    ) -> tuple[FinalDeliveryOutcome, ApprovalContinuation]:
        """Deliver execution output against the latest native continuation generation."""
        tool_trace: list[ToolTraceEntry] = []
        identity = self.host.response_identity(
            request,
            response_kind="team" if claimed.entity_kind == "team" else "ai",
        )
        # Streaming requesters watch the resumed work in the reply it continues.
        # Progress closes before any terminal edit, which stays owned below.
        progress_scope: AbstractAsyncContextManager[ProgressPublisher | None] = (
            self.deps.delivery_gateway.stream_progress(
                target=target,
                event_id=claimed.response_event_id,
                identity=identity,
                show_tool_calls=claimed.show_tool_calls,
                extra_content=merge_response_extra_content(None, claimed.attachment_ids),
                visible_progress_callback=self.lifecycle_coordinator.visible_progress_callback(target),
            )
            if await should_use_streaming(
                self.host.client(),
                claimed.room_id,
                requester_user_id=claimed.requester_id,
                enable_streaming=self.deps.runtime.enable_streaming,
            )
            else nullcontext()
        )
        async with progress_scope as progress:
            result = await self._continue_entity_call(
                claimed,
                request=request,
                target=target,
                tool_trace_collector=tool_trace,
                progress=progress,
                cli_approval_handler=cli_approval_handler,
            )
        if isinstance(result, CompletedApprovalRun):
            current = await self.deps.approval_store.approval_continuation(claimed.approval_id) or claimed
            show_tool_calls = claimed.show_tool_calls
            visible_tool_trace = tool_trace if show_tool_calls else []
            return (
                await self.deps.delivery_gateway.deliver_final(
                    FinalDeliveryRequest(
                        target=target,
                        existing_event_id=claimed.response_event_id,
                        response_text=result.response_text,
                        identity=identity,
                        tool_trace=visible_tool_trace if show_tool_calls else None,
                        extra_content=merge_response_extra_content(result.metadata_content, claimed.attachment_ids),
                    ),
                ),
                current,
            )
        current = await self.deps.approval_store.approval_continuation(claimed.approval_id) or claimed
        if current.state in {"waiting", "ready"}:
            return (
                FinalDeliveryOutcome(
                    terminal_status="suspended",
                    event_id=current.response_event_id,
                    is_visible_response=True,
                    final_visible_body=result.response_text,
                    delivery_kind="edited",
                    extra_content={STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING},
                ),
                current,
            )
        handle = current_span()
        assert handle is not None, "an approval resume runs in its reply's resume span"
        presentation = await self._approval_responses.advance_pause(
            current,
            result,
            target=target,
            pending_text=PROGRESS_PLACEHOLDER,
            reply_pause=partial(self._advance_reply_pause, handle, target),
        )
        current = await self.deps.approval_store.approval_continuation(claimed.approval_id) or claimed
        return (
            FinalDeliveryOutcome(
                terminal_status="suspended",
                event_id=claimed.response_event_id,
                is_visible_response=True,
                final_visible_body=presentation.response_text,
                delivery_kind="edited",
                tool_trace=presentation.tool_trace,
                extra_content={
                    STREAM_STATUS_KEY: (
                        STREAM_STATUS_APPROVAL_PENDING if presentation.approval_pending else STREAM_STATUS_PENDING
                    ),
                },
            ),
            current,
        )

    async def _run_claimed_approval_lifecycle(
        self,
        claimed: ApprovalContinuation,
        *,
        target: MessageTarget,
    ) -> FinalDeliveryOutcome:
        """Run one claimed pause through the normal stoppable response lifecycle."""
        request = self._approval_response_request(claimed, target=target)
        await self.host.refresh_mid_turn_context_for_approval(request)
        progress = DeliveryProgress()
        progress.note_delivery_started(claimed.response_event_id)
        lifecycle = self.host.build_lifecycle(
            identity=self.host.response_identity(
                request,
                response_kind="team" if claimed.entity_kind == "team" else "ai",
            ),
            request=request,
        )
        post_effect_continuation = claimed

        async def continue_response(_message_id: str | None) -> None:
            nonlocal post_effect_continuation
            try:
                outcome, post_effect_continuation = await self._execute_claimed_approval(
                    claimed,
                    request=request,
                    target=target,
                )
            except (asyncio.CancelledError, Exception):
                delivery = await run_coroutine_until_complete(
                    self._approval_responses.final_delivery(claimed, recover=True),
                )
                if delivery is None:
                    raise
                if delivery.acknowledged_event_id is None:
                    progress.settle(
                        FinalDeliveryOutcome(
                            terminal_status="suspended",
                            event_id=claimed.response_event_id,
                            is_visible_response=True,
                            extra_content={STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING},
                        ),
                    )
                    return
                progress.settle(await self._approval_outcome_from_delivery(delivery))
                return
            if (
                outcome.terminal_status not in {"completed", "suspended"}
                and await self._approval_responses.final_delivery(
                    claimed,
                )
                is not None
            ):
                outcome = FinalDeliveryOutcome(
                    terminal_status="suspended",
                    event_id=claimed.response_event_id,
                    is_visible_response=True,
                    final_visible_body=outcome.final_visible_body,
                    extra_content={STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING},
                )
            progress.settle(outcome)

        await self.host.run_and_settle_locked_response(
            request,
            target=target,
            lifecycle=lifecycle,
            progress=progress,
            response_function=continue_response,
            user_id=claimed.requester_id,
            build_post_response_outcome=lambda final: self._approval_post_response_outcome(
                post_effect_continuation,
                target=target,
                final=final,
            ),
            post_response_deps=lambda: self._approval_post_response_deps(claimed),
        )
        outcome = progress.delivery_outcome
        if outcome is None:
            msg = "Approval continuation ended without a terminal lifecycle outcome"
            raise RuntimeError(msg)
        if outcome.terminal_status == "completed":
            if not await self.deps.replies.finish_approval(claimed.approval_id):
                msg = "Approval continuation final delivery was not durably acknowledged"
                raise RuntimeError(msg)
        elif outcome.terminal_status != "suspended":
            await self._settle_failed_approval_outcome(post_effect_continuation, outcome)
        return outcome

    async def _settle_failed_approval_outcome(
        self,
        continuation: ApprovalContinuation,
        outcome: FinalDeliveryOutcome,
    ) -> None:
        """Preserve partial text for interrupted continuations, but not explicit stops."""
        reason = outcome.failure_reason or "Tool approval continuation failed safely."
        cancel_source = _approval_interruption_cancel_source(reason)
        if outcome.terminal_status == "cancelled" and cancel_source is not None:
            await self._settle_interrupted_approval_recovery(
                continuation,
                reason=cancel_failure_reason(cancel_source),
                cancel_source=cancel_source,
            )
        else:
            await self._approval_responses.settle_failure(continuation, reason)

    async def _approval_outcome_from_delivery(self, delivery: MatrixDelivery) -> FinalDeliveryOutcome:
        """Restore semantic lifecycle facts from one frozen approval FINAL and the reply it answered."""
        acknowledged_event_id = delivery.acknowledged_event_id
        if acknowledged_event_id is None:
            msg = "Approval final delivery is not acknowledged"
            raise RuntimeError(msg)
        event_id = delivery.edits_event_id or acknowledged_event_id
        payload = dict(delivery.payload)
        nested = payload.get("m.new_content")
        visible = cast("dict[str, Any]", nested) if isinstance(nested, dict) else payload
        semantic = cast("dict[str, object]", delivery.result) if isinstance(delivery.result, dict) else {}
        interactive_metadata = InteractiveMetadata.from_metadata(semantic.get("interactive"))
        reply = None if delivery.reply_id is None else await self.deps.replies.store.replies.load(delivery.reply_id)
        span = None if delivery.span_id is None else await self.deps.replies.store.replies.span(delivery.span_id)
        if reply is None or span is None:
            msg = "Approval final delivery has no reply records"
            raise RuntimeError(msg)
        # What the FINAL showed: the final transform's display when it reshaped the answer.
        body, _trace = render_body(decode_presentation(reply.possibly_shown or reply.presentation))
        return FinalDeliveryOutcome(
            # A frozen FINAL may be the failure or cancellation its span ended with.
            terminal_status=_terminal_status_of_span(span),
            event_id=event_id,
            is_visible_response=True,
            final_visible_body=body,
            delivery_kind="edited" if delivery.edits_event_id is not None else "sent",
            extra_content=visible,
            interactive_metadata=interactive_metadata,
        )

    async def _recover_claimed_approval_lifecycle(
        self,
        claimed: ApprovalContinuation,
        *,
        target: MessageTarget,
    ) -> str | None:
        """Recover frozen FINAL debt without resuming the run a second time."""
        owns_final, event_id = await self._recover_frozen_approval_final(claimed, target=target)
        if owns_final:
            return event_id
        final_delivery = await self._approval_responses.final_delivery(claimed)
        if final_delivery is not None and final_delivery.permanently_failed:
            settled = await self._approval_responses.settle_failure(
                claimed,
                INTERRUPTED_FAILURE_REASON,
            )
            return claimed.response_event_id if settled else None
        return await self._release_interrupted_approval(claimed, reason=INTERRUPTED_FAILURE_REASON)

    async def _release_interrupted_approval(
        self,
        continuation: ApprovalContinuation,
        *,
        reason: str,
    ) -> str | None:
        """Release an approved run a restart cut short, so replay continues its reply or its Stop ends it.

        Before a FINAL the reply is still the unfinished stream of one turn, so
        the replayed turn adopts it like any reply a restart left streaming; a
        Stop the run left unapplied ends it cancelled instead. A
        hand-back that cannot finish yet, such as cards that did not expire, is
        retried by the next recovery pass. A resume that already failed before
        its failure was fenced, or a FINAL already owed, settles the
        continuation as a failure instead.
        """
        claim_span = (
            None
            if continuation.claim_span_id is None
            else await self.deps.replies.store.replies.span(continuation.claim_span_id)
        )
        # A resume that already ended without its answer has nothing for a replay to continue.
        resume_ended = claim_span is not None and claim_span.outcome in _UNANSWERED_RESUME_OUTCOMES
        if resume_ended or await self._approval_responses.final_delivery(continuation) is not None:
            settled = await self._approval_responses.settle_failure(continuation, reason)
            return continuation.response_event_id if settled else None
        await self._approval_responses.release_to_replay(continuation, reason)
        return None

    async def _settle_interrupted_approval_recovery(
        self,
        continuation: ApprovalContinuation,
        *,
        reason: str,
        cancel_source: Literal["sync_restart", "interrupted"],
    ) -> bool:
        """Fence an uncertain stale claim and settle it with the interruption its reply shows."""
        failing = continuation
        if failing.state != "failing":
            requested = await self._approval_responses.request_failure(
                failing,
                reason,
            )
            if requested is None:
                return False
            failing = requested
        # The reply's records know what it showed; Matrix is not read back.
        return await self._approval_responses.settle_failure(
            failing,
            reason,
            interruption=interruption_note(cancel_source),
        )

    async def _recover_frozen_approval_final(
        self,
        claimed: ApprovalContinuation,
        *,
        target: MessageTarget,
    ) -> tuple[bool, str | None]:
        """Complete acknowledged FINAL debt, or retain unacknowledged delivery ownership."""
        delivery = await self._approval_responses.final_delivery(claimed, recover=True)
        if delivery is None:
            return False, None
        if delivery.permanently_failed:
            return False, None
        if delivery.acknowledged_event_id is None:
            return True, None
        recovered_outcome = await self._approval_outcome_from_delivery(delivery)
        request = self._approval_response_request(claimed, target=target)
        lifecycle = self.host.build_lifecycle(
            identity=self.host.response_identity(
                request,
                response_kind="team" if claimed.entity_kind == "team" else "ai",
            ),
            request=request,
        )
        await lifecycle.finalize(
            recovered_outcome,
            build_post_response_outcome=lambda final: self._approval_post_response_outcome(
                claimed,
                target=target,
                final=final,
            ),
            post_response_deps=lambda: self._approval_post_response_deps(claimed),
        )
        if not await self.deps.replies.finish_approval(claimed.approval_id):
            msg = "Recovered approval final delivery lost its journal ownership"
            raise RuntimeError(msg)
        return True, recovered_outcome.event_id

    async def _recover_or_request_claimed_failure(
        self,
        claimed: ApprovalContinuation,
        *,
        target: MessageTarget,
        reason: str,
    ) -> tuple[bool, str | None, ApprovalContinuation | None]:
        """Recover frozen success, or durably fence the observed claim after a recovery error."""
        try:
            owns_final, event_id = await self._recover_frozen_approval_final(claimed, target=target)
        except Exception:
            self.deps.logger.exception(
                "approval_final_recovery_failed_before_failure_fence",
                approval_id=claimed.approval_id,
            )
            owns_final, event_id = False, None
        failing = None if owns_final else await self._approval_responses.request_failure(claimed, reason)
        return owns_final, event_id, failing

    def _approval_response_request(
        self,
        continuation: ApprovalContinuation,
        *,
        target: MessageTarget,
    ) -> ResponseRequest:
        """Rebuild the original response identity for resumed hooks and post-effects."""
        envelope = MessageEnvelope(
            source_event_id=continuation.source_event_ids[0],
            target=target,
            body=continuation.request_body,
            attachment_ids=continuation.attachment_ids,
            mentioned_agents=continuation.mentioned_agents,
            agent_name=continuation.entity_name,
            origin=continuation.origin,
            hook_source=continuation.hook_source,
            message_received_depth=continuation.message_received_depth,
            dispatch_policy_source_kind=continuation.dispatch_policy_source_kind,
        )
        return ResponseRequest(
            thread_history=self._approval_memory_history(continuation),
            prompt=envelope.body,
            response_envelope=envelope,
            sources=continuation.sources,
            existing_event_id=continuation.response_event_id,
            user_id=continuation.requester_id,
            attachment_ids=continuation.attachment_ids,
            correlation_id=continuation.correlation_id,
            participating_agent_names=continuation.team_member_names or (continuation.entity_name,),
        )

    def _approval_post_response_outcome(
        self,
        continuation: ApprovalContinuation,
        *,
        target: MessageTarget,
        final: FinalDeliveryOutcome,
    ) -> ResponseOutcome:
        """Build normal post-response facts for one resumed native run."""
        return ResponseOutcome(
            response_run_id=final.response_run_id or continuation.run_id,
            run_succeeded=final.terminal_status == "completed",
            response_target=target,
            thread_summary_message_count_hint=continuation.thread_summary_message_count_hint,
            thread_summary_entity_name=continuation.entity_name,
        )

    def _approval_post_response_deps(self, continuation: ApprovalContinuation) -> PostResponseEffectsDeps:
        """Build normal post-response dependencies for a continued run."""
        return self.deps.post_response_effects.build_deps(
            room_id=continuation.room_id,
            membership_turn_id=continuation.source_event_ids[0],
            queue_memory_persistence=self._approval_memory_persistence(continuation),
            # A continuation keeps its turn's source events, so a run paused for approval is verified when it ends.
            notify_response_finished=self.host.response_finished_notifier(
                continuation.sources.logical_source_event_ids,
            ),
            persist_response_event_id=self._approval_response_event_persistence(continuation),
        )

    @staticmethod
    def _approval_memory_history(
        continuation: ApprovalContinuation,
    ) -> tuple[ResolvedVisibleMessage, ...]:
        """Rebuild the sender/body history consumed by conversation memory."""
        return tuple(
            ResolvedVisibleMessage.synthetic(
                sender=turn.sender,
                body=turn.body,
                event_id=f"$approval-memory-{continuation.approval_id}-{index}",
                thread_id=continuation.thread_id,
            )
            for index, turn in enumerate(continuation.memory_thread_history)
        )

    def _approval_memory_persistence(self, continuation: ApprovalContinuation) -> Callable[[], None] | None:
        """Return the normal memory handoff for a completed continuation."""
        if continuation.entity_kind == "agent":
            agent_name: str | list[str] = continuation.entity_name
        elif continuation.entity_name in self.deps.runtime.config.teams:
            agent_name = list(continuation.team_member_names)
        else:
            return None
        return self.host.memory_persistence(
            agent_name=agent_name,
            session_id=continuation.session_id,
            execution_identity=continuation.execution_identity,
            prompt=(
                continuation.memory_prompt if continuation.memory_prompt is not None else continuation.request_body
            ),
            thread_history=self._approval_memory_history(continuation),
            user_id=continuation.requester_id,
            hook_source=continuation.hook_source,
            origin=continuation.origin,
        )

    def _approval_response_event_persistence(
        self,
        continuation: ApprovalContinuation,
    ) -> Callable[[str, str], Awaitable[None]] | None:
        """Return the normal run-to-Matrix event linkage for a resumed response."""
        history_scope = continuation.history_scope
        if history_scope is None:
            if continuation.entity_kind == "team":
                return None
            history_scope = self.deps.state_writer.history_scope()
        return self.host.build_persist_response_event_id_effect(
            session_id=continuation.session_id,
            scope=history_scope,
            execution_identity=continuation.execution_identity,
        )

    async def _continue_entity_call(
        self,
        continuation: ApprovalContinuation,
        *,
        request: ResponseRequest,
        target: MessageTarget,
        tool_trace_collector: list[ToolTraceEntry],
        progress: ProgressPublisher | None,
        cli_approval_handler: Callable[[PausedAttempt], Awaitable[tuple[RunRequirement, ...]]] | None,
    ) -> CompletedApprovalRun | PausedAttempt:
        execution_identity = continuation.execution_identity
        tool_dispatch = self.deps.tool_runtime.build_dispatch_context(
            target,
            user_id=continuation.requester_id,
            agent_name=continuation.entity_name,
            active_model_name=continuation.runtime_model_name,
            attachment_ids=continuation.attachment_ids,
            correlation_id=correlation_id_for_request(request),
            source_envelope=request.response_envelope,
        )
        if tool_dispatch.execution_identity != execution_identity:
            msg = "Approval continuation execution identity no longer matches its target"
            raise RuntimeError(msg)
        if cli_approval_handler is not None:
            context = runtime_context_from_dispatch_context(tool_dispatch)
            if context is None:
                msg = "CLI approval recovery requires a live tool runtime"
                raise RuntimeError(msg)
            tool_dispatch = LiveToolDispatchContext.from_runtime_context(
                replace(context, cli_approval_handler=cli_approval_handler),
            )
        decisions = {call.tool_call_id: call.decision is ContinuationDecision.APPROVED for call in continuation.calls}
        denial_reasons = {call.tool_call_id: call.reason for call in continuation.calls}
        paused_answer = _paused_answer()
        with approval_receipt_context((*continuation.earlier_calls, *continuation.calls), run_id=continuation.run_id):
            if continuation.entity_kind == "team":

                async def continue_team() -> CompletedApprovalRun | PausedAttempt:
                    return await continue_paused_team_run(
                        member_names=continuation.team_member_names,
                        mode=TeamMode(continuation.team_mode or "coordinate"),
                        config=self.deps.runtime.config,
                        runtime_paths=self.deps.runtime_paths,
                        execution_identity=execution_identity,
                        session_id=continuation.session_id,
                        run_id=continuation.run_id,
                        user_id=continuation.requester_id,
                        configured_team_name=continuation.entity_name,
                        model_name=select_model_for_team(
                            continuation.entity_name,
                            continuation.room_id,
                            self.deps.runtime.config,
                            self.deps.runtime_paths,
                            thread_id=continuation.thread_id,
                        )
                        if continuation.runtime_model_name is None
                        else continuation.runtime_model_name,
                        decisions=decisions,
                        denial_reasons=denial_reasons,
                        refresh_scheduler=self.host.knowledge_refresh_scheduler(),
                        member_model_names=dict(continuation.team_member_model_names) or None,
                        approval_calls=continuation.calls,
                        history_scope=continuation.history_scope,
                        prior_tool_trace=paused_answer.tool_trace,
                        prior_presentation_state=paused_answer.team_state,
                        show_tool_calls=continuation.show_tool_calls,
                        tool_trace_collector=tool_trace_collector,
                        progress=progress,
                    )

                async with response_typing_indicator(
                    self.host.client(),
                    request,
                    response_run_id=continuation.run_id,
                ):
                    response_text = await self.host.run_in_tool_context(
                        tool_dispatch=tool_dispatch,
                        operation=continue_team,
                    )
            else:
                response_text = await self._approval_execution.continue_run(
                    continuation,
                    paused_answer=paused_answer,
                    tool_dispatch=tool_dispatch,
                    decisions=decisions,
                    denial_reasons=denial_reasons,
                    tool_trace_collector=tool_trace_collector,
                    typing_log_context=response_typing_log_context(
                        request,
                        response_run_id=continuation.run_id,
                    ),
                    progress=progress,
                )
        return response_text

    def hold_for_approval(self, continuation: ApprovalContinuation) -> None:
        """Keep a pending approval's conversation busy, as a restart finds it."""
        self.lifecycle_coordinator.hold_for_approval(continuation.approval_id, continuation_target(continuation))

    def release_approval_hold(self, approval_id: str) -> None:
        """Let the conversation of an approval that ended answer its waiting messages."""
        self.lifecycle_coordinator.release_approval_hold(approval_id)

    def release_approval_holds_in_room(self, room_id: str) -> None:
        """Release the approval holds of a room this bot left."""
        self.lifecycle_coordinator.release_approval_holds_in_room(room_id)

    def is_held_for_approval(self, target: MessageTarget) -> bool:
        """Return whether a pending approval keeps this conversation busy."""
        return bool(self.lifecycle_coordinator.approval_holds(target.room_id, target.resolved_thread_id))

    async def wait_for_thread_response_idle(self, room_id: str, thread_id: str | None) -> None:
        """Wait until one canonical room/thread has no active response turn.

        While it waits it rechecks that each approval holding the conversation
        still exists, so a hold whose end never reached this bot, as after a
        departure it did not see, cannot keep it waiting. A recheck that lands
        while a pause is still committing can take the new approval for an
        ended one and drop its hold, so messages that arrive while that
        approval waits are answered without waiting for it.
        """
        while True:
            try:
                await asyncio.wait_for(
                    self.lifecycle_coordinator.wait_for_thread_idle(room_id, thread_id),
                    timeout=_APPROVAL_HOLD_RECHECK_SECONDS,
                )
            except TimeoutError:
                await self._release_ended_approval_holds(room_id, thread_id)
                continue
            return

    async def _release_ended_approval_holds(self, room_id: str, thread_id: str | None) -> None:
        """Release the holds of a conversation whose approvals no longer exist."""
        for approval_id in self.lifecycle_coordinator.approval_holds(room_id, thread_id):
            try:
                ended = await self.deps.approval_store.approval_continuation(approval_id) is None
            except Exception:
                # The journal is unreachable: keep waiting, and recheck on the next interval.
                self.deps.logger.warning("approval_hold_recheck_failed", approval_id=approval_id, exc_info=True)
                return
            if ended:
                self.lifecycle_coordinator.release_approval_hold(approval_id)

    async def run_owned_approvals(
        self,
        owned: ApprovalContinuation,
        *,
        request: ResponseRequest,
        target: MessageTarget,
    ) -> str | None:
        """Run, recover, or settle each generation of the approval that owns a response's source."""
        while True:
            self.deps.logger.info(
                "response_source_owned_by_approval_continuation",
                source_event_id=request.response_envelope.source_event_id,
                approval_id=owned.approval_id,
                approval_state=owned.state,
            )
            recovered, event_id = await self._recover_nonready_approval(owned, target=target)
            if recovered:
                return event_id
            if not is_sender_allowed_for_entity_replies_in_room(
                owned.requester_id,
                reply_authorization_entity_names(
                    self.deps.runtime.config,
                    owned.entity_name,
                    owned.team_member_names,
                ),
                self.deps.runtime.config,
                owned.room_id,
                self.deps.runtime_paths,
                self.deps.runtime.agent_reply_memberships,
                require_resolved_membership=True,
            ):
                return await self._settle_unauthorized_approval_continuation(owned)
            # Each claimed generation runs in its own resume span.
            async with self.deps.replies.span_scope() as resume_slot:
                claimed = await self._claim_owned_approval(owned, slot=resume_slot)
                if claimed is None:
                    return None
                event_id = await self._run_owned_approval_continuation(claimed, target=target)
            current = await self.deps.approval_store.approval_continuation_for_source(
                request.response_envelope.source_event_id,
            )
            if (
                current is None
                or current.approval_id != claimed.approval_id
                or current.state != "ready"
                or current.generation <= claimed.generation
            ):
                return event_id
            owned = current

    async def _settle_unauthorized_approval_continuation(
        self,
        continuation: ApprovalContinuation,
    ) -> str | None:
        reason = "Current authorization no longer permits this tool approval continuation."
        failing = await self._approval_responses.request_failure(continuation, reason)
        if failing is None:
            return None
        settled = await self._approval_responses.settle_failure(failing, reason)
        return continuation.response_event_id if settled else None

    async def _run_owned_approval_continuation(
        self,
        claimed: ApprovalContinuation,
        *,
        target: MessageTarget,
    ) -> str | None:
        try:
            outcome = await self._run_claimed_approval_lifecycle(claimed, target=target)
        except asyncio.CancelledError as error:
            # A shutdown that hands this run to a successor runtime records it as
            # restart-interrupted and leaves the span as a crash would, for that
            # runtime to hand back to replay. Read here: the detached task that
            # ends the span is no shutdown task.
            process_shutdown = current_task_is_process_shutdown()
            reason = (
                INTERRUPTED_FAILURE_REASON if process_shutdown else cancel_failure_reason(classify_cancel_source(error))
            )
            if not process_shutdown:
                await run_coroutine_until_complete(self._end_live_resume_span())
            owns_final, event_id, _failing = await run_coroutine_until_complete(
                self._recover_or_request_claimed_failure(
                    claimed,
                    target=target,
                    reason=reason,
                ),
            )
            if owns_final:
                return event_id
            raise
        except Exception as error:
            reason = str(error) or "Tool approval continuation failed safely."
            if not current_task_is_process_shutdown():
                await self._end_live_resume_span()
            owns_final, event_id, failing = await self._recover_or_request_claimed_failure(
                claimed,
                target=target,
                reason=reason,
            )
            if owns_final:
                return event_id
            self.deps.logger.exception(
                "approval_continuation_failed",
                approval_id=claimed.approval_id,
            )
            if failing is not None:
                settled = await self._approval_responses.settle_failure(failing, reason)
                event_id = claimed.response_event_id if settled else None
            else:
                event_id = None
        else:
            retained = await self.deps.approval_store.approval_continuation(claimed.approval_id)
            event_id = outcome.event_id if outcome.terminal_status != "suspended" and retained is None else None
        return event_id

    async def _end_live_resume_span(self) -> None:
        """End a resume span its lifecycle left running, before the continuation's failure settles the reply."""
        handle = current_span()
        if handle is None or handle.exited or handle.span.kind is not rl.SpanKind.APPROVAL_RESUME:
            return
        now_ns = time.time_ns()
        # The rules leave a resume span's end to its approval, whether a Stop or a failure ended it.
        await self.deps.delivery_gateway.end_reply_span(
            handle,
            lambda reply, span: rl.fail(reply, span, None, now_ns=now_ns),
        )

    async def _claim_owned_approval(
        self,
        owned: ApprovalContinuation,
        *,
        slot: SpanSlot,
    ) -> ApprovalContinuation | None:
        """Claim a ready continuation together with its reply's resume span."""
        claimed, handle = await self.deps.replies.claim_approval_resume(
            owned,
            placeholder=TEAM_PLACEHOLDER if owned.entity_kind == "team" else AGENT_PLACEHOLDER,
        )
        slot.handle = handle
        return claimed

    async def _resume_approval_source(self, source_event_id: str) -> None:
        """Resume journal-owned approval work before normal ingress can reinterpret its source."""
        continuation = await self.deps.approval_store.approval_continuation_for_source(source_event_id)
        if continuation is None:
            return
        target = continuation_target(continuation, reply_to_event_id=source_event_id)
        request = self._approval_response_request(continuation, target=target)

        async def ownership_disappeared(
            _target: MessageTarget,
            _early_placeholder: object,
        ) -> str | None:
            return None

        await self.host.run_locked_response_lifecycle(
            request,
            response_kind="team" if continuation.entity_kind == "team" else "ai",
            locked_operation=ownership_disappeared,
            signal_queued_message=False,
        )

    async def handoff_approval_source(self, source_event_id: str) -> bool | None:
        """Transfer one durable continuation out of the journal lane and into response ownership."""
        continuation = await self.deps.approval_store.approval_continuation_for_source(source_event_id)
        if continuation is None:
            return None

        if not self._cli_approval_waits.wake((source_event_id,)):
            return False

        resume = self._resume_approval_source(source_event_id)
        try:
            self.host.track_inbox_response(
                resume,
                name=f"approval_resume:{continuation.approval_id}:{continuation.generation}",
                recovery_proof_ready=lambda: True,
                room_id=continuation.room_id,
                source_event_ids=continuation.source_event_ids,
            )
        except BaseException:
            resume.close()
            raise
        return False

    def wake_cli_approval_sources(self, source_event_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Wake response-local CLI owners and return sources still owned by the journal."""
        return self._cli_approval_waits.wake(source_event_ids)

    async def recover_approval_final(self, approval_id: str) -> bool:
        """Finalize one frozen FINAL under its original bot principal."""
        continuation = await self.deps.approval_store.approval_continuation(approval_id)
        if continuation is None:
            return True
        target = continuation_target(
            continuation,
            reply_to_event_id=continuation.source_event_ids[0],
        )
        request = self._approval_response_request(continuation, target=target)

        async def recover(_target: MessageTarget) -> bool:
            delivery = await self._approval_responses.final_delivery(continuation, recover=True)
            if delivery is None:
                return False
            if delivery.permanently_failed:
                reason = delivery.permanent_failure_reason or "Final Matrix delivery was permanently refused."
                return await self._approval_responses.settle_failure(continuation, reason)
            if delivery.acknowledged_event_id is None:
                return False
            if await self._approval_responses.successful_final_delivery(continuation) is None:
                return await self.deps.replies.finish_approval(continuation.approval_id)
            owns_final, event_id = await self._recover_frozen_approval_final(
                continuation,
                target=target,
            )
            return owns_final and event_id is not None

        return await self.lifecycle_coordinator.run_locked_response(
            target=target,
            response_envelope=request.response_envelope,
            pipeline_timing=None,
            locked_operation=recover,
            signal_queued_message=False,
        )

    async def _recover_nonready_approval(
        self,
        owned: ApprovalContinuation,
        *,
        target: MessageTarget,
    ) -> tuple[bool, str | None]:
        """Recover a non-ready owner, leaving ready execution to the caller."""
        if owned.state == "waiting":
            if owned.runtime_generation is not None:
                reason = "Tool approval card publication was interrupted and denied safely."
                failing = await self._approval_responses.request_failure(owned, reason)
                if failing is not None:
                    settled = await self._approval_responses.settle_failure(failing, reason)
                    return True, owned.response_event_id if settled else None
            return True, None
        if owned.state == "claimed":
            return True, await self._recover_claimed_approval_lifecycle(owned, target=target)
        if owned.state == "failing":
            return True, await self._recover_failing_approval(owned, target=target)
        return False, None

    async def _recover_failing_approval(
        self,
        failing: ApprovalContinuation,
        *,
        target: MessageTarget,
    ) -> str | None:
        """Finish a fenced continuation through its frozen FINAL, replay after a restart, or its failure note."""
        if await self._approval_responses.successful_final_delivery(failing, recover=True) is not None:
            owns_final, event_id = await self._recover_frozen_approval_final(failing, target=target)
            return event_id if owns_final else None
        reason = failing.failure_reason or "Tool approval continuation failed safely."
        if reason == INTERRUPTED_FAILURE_REASON:
            return await self._release_interrupted_approval(failing, reason=reason)
        cancel_source = _approval_interruption_cancel_source(reason)
        settled = (
            await self._settle_interrupted_approval_recovery(
                failing,
                reason=reason,
                cancel_source=cancel_source,
            )
            if cancel_source is not None
            else await self._approval_responses.settle_failure(failing, reason)
        )
        return failing.response_event_id if settled else None
