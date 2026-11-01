"""The approval lifecycle of a response: pause for tool approval, resume the approved run, and recover it."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, assert_never, cast
from uuid import uuid4

from mindroom import reply_lifecycle as rl
from mindroom.agent_cli.cli_approval_waits import CliApprovalWaits, CliWaiters
from mindroom.approval import manager as approval_manager
from mindroom.approval.continuation_rules import (
    CliExit,
    ContinuationFacts,
    DiscardUnavailable,
    Dispatch,
    Fence,
    Finish,
    OwnerUnavailable,
    Release,
    Resume,
    Resumed,
    Settle,
    Step,
    Wait,
    WakeSources,
    continuation_facts,
    next_step,
)
from mindroom.approval.execution import AgentApprovalExecution
from mindroom.approval.failure import (
    cancelled_failure,
    prepare_approval_failure,
    read_final,
)
from mindroom.approval.receipt_readers import approval_receipt_context
from mindroom.approval.response import (
    ApprovalPausePlan,
    continuation_response_kind,
    continuation_target,
    identify_approval_tools,
    plan_approval_calls,
    publish_generation,
    require_ordered_pause_presentation,
)
from mindroom.authorization import is_sender_allowed_for_entity_replies_in_room
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.constants import (
    STREAM_STATUS_APPROVAL_PENDING,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
)
from mindroom.delegation.recovery import cancel_approval_delegations
from mindroom.delivery_gateway import (
    EditTextRequest,
    FinalDeliveryRequest,
    SendTextRequest,
)
from mindroom.event_journal import (
    ApprovalAdvance,
    ApprovalContinuation,
    ApprovalFailure,
    ApprovalMemoryTurn,
    MatrixDelivery,
)
from mindroom.final_delivery import (
    AwaitingApproval,
    Cancelled,
    Completed,
    Failed,
    FailureCause,
    FinalDeliveryOutcome,
    Owed,
    Suppressed,
    TurnEnding,
    TurnProgress,
    approval_failure,
)
from mindroom.hooks import MessageEnvelope
from mindroom.interactive import InteractiveMetadata
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.presence import should_use_streaming
from mindroom.orchestration.runtime import (
    classify_cancel_source,
    current_task_is_process_shutdown,
)
from mindroom.post_response_effects import ResponseOutcome
from mindroom.reply_lifecycle import ApprovalFailureKind
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
    pause_decision,
    pause_write,
)
from mindroom.reply_turn.request import (
    ResponseRequest,
    correlation_id_for_request,
    merge_response_extra_content,
    reply_authorization_entity_names,
    response_typing_indicator,
    response_typing_log_context,
)
from mindroom.response_turn import CompletedApprovalRun, PausedAnswer, PausedAttempt
from mindroom.streaming import PROGRESS_PLACEHOLDER
from mindroom.teams.resume import continue_paused_team_run
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.events import body_without_tool_markers
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.turn_model import ModelScope, resolve_turn_model

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from contextlib import AbstractAsyncContextManager

    from mindroom.delivery_gateway import ResponseIdentity
    from mindroom.history.types import HistoryScope
    from mindroom.llm.runs import RunRequirement
    from mindroom.message_target import MessageTarget
    from mindroom.post_response_effects import PostResponseEffectsDeps
    from mindroom.reply_turn.locked_turn import LockedTurns, ReplyTurnDeps
    from mindroom.reply_turn.request import PreparedResponseRuntime
    from mindroom.response_lifecycle import ResponseLifecycleCoordinator
    from mindroom.streaming import ProgressPublisher
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

# The note a settled failure shows below its reply; an error's carries its reason.
_FAILURE_NOTES = {
    ApprovalFailureKind.USER_STOP: NoteKind.CANCELLED,
    ApprovalFailureKind.RESTART: NoteKind.RESTART,
    ApprovalFailureKind.INTERRUPTED: NoteKind.INTERRUPTED,
}


def _error(detail: str) -> ApprovalFailure:
    return ApprovalFailure(ApprovalFailureKind.ERROR, detail)


_UNAUTHORIZED = _error("Current authorization no longer permits this tool approval continuation.")


def _ending_of_span(span: rl.Span) -> TurnEnding:
    """Return how the turn a frozen approval FINAL answered ended, from the span that wrote it.

    A FINAL that is not the resume's answer is the note its failure, or a Stop, ended the reply with.
    """
    if span.outcome is rl.SpanOutcome.COMPLETED:
        return Completed()
    return Cancelled("user_stop") if span.outcome is rl.SpanOutcome.CANCELLED else Failed(FailureCause.ERROR)


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
class _ApprovalPausePresentation:
    """Visible response state published for one chained approval generation."""

    response_text: str
    tool_trace: tuple[ToolTraceEntry, ...]
    approval_pending: bool


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


@dataclass
class ApprovalContinuationRunner:
    """Pause a response for tool approval, resume its approved run, and recover it after a failure or restart.

    It settles through the locked turns it shares with the response runner, which hands it paused turns; it knows
    nothing of that runner.
    """

    locked: LockedTurns = field(repr=False)
    lifecycle_coordinator: ResponseLifecycleCoordinator = field(repr=False)
    # Which sources a response waits on in place for a minimal-mode decision.
    cli_waiters: CliWaiters = field(repr=False)
    _approval_execution: AgentApprovalExecution = field(init=False, repr=False)
    _cli_approval_waits: CliApprovalWaits = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Bind response-side approval collaborators to the event journal."""
        self._cli_approval_waits = CliApprovalWaits(
            store=self.deps.approval_store,
            claim=self._claim_in_place_approval,
            advance=self._advance_in_place_approval,
            exit=self._cli_exit,
            waiters=self.cli_waiters,
        )
        self._approval_execution = AgentApprovalExecution(
            config=lambda: self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            client=self.locked.client,
            knowledge_access=self.deps.knowledge_access,
            refresh_scheduler=self.locked.knowledge_refresh_scheduler,
        )

    @property
    def deps(self) -> ReplyTurnDeps:
        """Return the collaborators every owner of a reply turn shares."""
        return self.locked.deps

    async def _failed_approval_handoff(
        self,
        approval_id: str,
        *,
        error: BaseException,
    ) -> FinalDeliveryOutcome | None:
        """Return suspension ownership only after failure intent is durable."""
        detail = (
            "Tool approval suspension was cancelled."
            if isinstance(error, asyncio.CancelledError)
            else str(error) or "Tool approval suspension failed."
        )
        # Fence the continuation the suspension created, whose card publication did not finish.
        born = await self.deps.approval_store.approval_continuation(approval_id)
        continuation = None if born is None else await self._request_failure(born, _error(detail))
        if continuation is None:
            return None
        return FinalDeliveryOutcome(
            ending=AwaitingApproval(),
            event_id=continuation.response_event_id,
            is_visible_response=True,
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
                    ending=AwaitingApproval(),
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
                thread_summary_message_count_hint=self.locked.thread_summary_message_count_hint(request),
                runtime_generation=(self.deps.approval_runtime_generation if continuation_state == "waiting" else None),
            )
            # Later messages of the conversation wait while the approval is pending, as behind a running reply:
            # the pause's commit holds the conversation while this reply still holds its lock.
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

            await self._publish_generation(
                continuation,
                plan,
                target=target,
                failure_reason="Approval card creation failed",
            )
            return FinalDeliveryOutcome(
                ending=AwaitingApproval(),
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
        target: MessageTarget,
        advance: ApprovalAdvance,
        paused: PausedAttempt,
        *,
        visible_text: str,
        stream_status: str,
        tool_trace: tuple[ToolTraceEntry, ...],
        waiting_text: str | None,
        in_place: bool,
    ) -> tuple[ApprovalContinuation | None, bool]:
        """Advance the continuation and pause the span's reply with the pause row, in one transaction.

        Returns the advanced continuation and whether its pause row reached Matrix.
        """
        handle = current_span()
        assert handle is not None, "a chained pause runs in its reply's span"
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
        await self._advance_pause(current, paused, target=target, in_place=True)

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

    async def _publish_generation(
        self,
        continuation: ApprovalContinuation,
        plan: ApprovalPausePlan,
        *,
        target: MessageTarget,
        failure_reason: str,
    ) -> None:
        """Publish a paused generation's cards, release its lease, and wake it when it is executable."""
        await publish_generation(
            continuation,
            plan,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            store=self.deps.approval_store,
            target=target,
            failure_reason=failure_reason,
            retry_sources=self.deps.retry_approval_sources,
        )

    async def _advance_pause(
        self,
        current: ApprovalContinuation,
        paused: PausedAttempt,
        *,
        target: MessageTarget,
        in_place: bool = False,
    ) -> _ApprovalPausePresentation:
        """Replace one claim with the run's next exact pause generation, recorded with the reply's pause row."""
        require_ordered_pause_presentation(paused, show_tool_calls=current.show_tool_calls)
        identified = identify_approval_tools(paused, default_agent_name=current.entity_name)
        plan = await plan_approval_calls(
            identified,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            requester_id=current.requester_id,
            toolkit_owners=paused.toolkit_owners,
            room_id=target.room_id,
            thread_id=target.resolved_thread_id,
        )
        approval_pending = plan.waiting_text is not None
        visible_tool_trace = tuple(paused.tool_trace) if current.show_tool_calls else ()
        visible_text = paused.response_text or plan.waiting_text or PROGRESS_PLACEHOLDER
        stream_status = STREAM_STATUS_APPROVAL_PENDING if approval_pending else STREAM_STATUS_PENDING
        advance = ApprovalAdvance(
            approval_id=current.approval_id,
            claimant_generation=current.generation,
            run_id=paused.run_id,
            session_id=paused.session_id,
            calls=plan.calls,
            runtime_model_name=paused.runtime_model_name,
            continuation_count=max(current.continuation_count, paused.continuation_count),
            delegation_storage_bindings=paused.delegation_storage_bindings,
            cli_call=paused.cli_call,
        )
        publishing, shown_with_pause = await self._advance_reply_pause(
            target,
            advance,
            paused,
            visible_text=visible_text,
            stream_status=stream_status,
            tool_trace=visible_tool_trace,
            waiting_text=plan.waiting_text,
            in_place=in_place,
        )
        if publishing is None:
            msg = "Could not persist the chained approval pause"
            raise RuntimeError(msg)
        failure_reason = "Chained approval publication failed"
        try:
            if not shown_with_pause:
                raise RuntimeError(failure_reason)  # noqa: TRY301
            await self._publish_generation(
                publishing,
                plan,
                target=target,
                failure_reason=failure_reason,
            )
        except (asyncio.CancelledError, Exception):
            await self._request_failure(publishing, _error(failure_reason))
            raise
        return _ApprovalPausePresentation(
            response_text=visible_text,
            tool_trace=visible_tool_trace,
            approval_pending=approval_pending,
        )

    @asynccontextmanager
    async def cli_approval_scope(
        self,
        runtime: PreparedResponseRuntime,
        *,
        request: ResponseRequest,
        progress: TurnProgress,
        history_scope: HistoryScope,
    ) -> AsyncIterator[PreparedResponseRuntime]:
        """Attach approval publication to this response's real source and placeholder."""
        async with self._cli_approval_handler_scope(
            request=request,
            progress=progress,
            history_scope=history_scope,
            target=runtime.tool_context.target,
            execution_identity=runtime.tool_context.execution_identity,
            show_tool_calls=runtime.show_tool_calls,
            settle_terminal=True,
        ) as handler:
            yield replace(runtime, tool_context=replace(runtime.tool_context, cli_approval_handler=handler))

    def _cli_approval_handler_scope(
        self,
        *,
        request: ResponseRequest,
        progress: TurnProgress,
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
            authorize=lambda: self.locked.request_remains_authorized(request),
            settle_terminal=settle_terminal,
        )

    async def _execute_claimed_approval(
        self,
        claimed: ApprovalContinuation,
        *,
        request: ResponseRequest,
        target: MessageTarget,
        identity: ResponseIdentity,
    ) -> tuple[FinalDeliveryOutcome, ApprovalContinuation]:
        """Run and classify one claimed continuation for either lifecycle entry path."""
        if claimed.cli_call is None:
            return await self._deliver_claimed_approval(claimed, request=request, target=target, identity=identity)
        progress = TurnProgress()
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
                identity=identity,
                cli_approval_handler=handler,
            )
            progress.ending = outcome.ending
            return outcome, current

    async def _deliver_claimed_approval(
        self,
        claimed: ApprovalContinuation,
        *,
        request: ResponseRequest,
        target: MessageTarget,
        identity: ResponseIdentity,
        cli_approval_handler: Callable[[PausedAttempt], Awaitable[tuple[RunRequirement, ...]]] | None = None,
    ) -> tuple[FinalDeliveryOutcome, ApprovalContinuation]:
        """Deliver execution output against the latest native continuation generation."""
        tool_trace: list[ToolTraceEntry] = []
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
                self.locked.client(),
                claimed.room_id,
                requester_user_id=claimed.requester_id,
                enable_streaming=self.deps.runtime.config.defaults.enable_streaming,
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
        current = await self.deps.approval_store.approval_continuation(claimed.approval_id) or claimed
        if isinstance(result, CompletedApprovalRun):
            return (
                await self.deps.delivery_gateway.deliver_final(
                    FinalDeliveryRequest(
                        target=target,
                        existing_event_id=claimed.response_event_id,
                        response_text=result.response_text,
                        identity=identity,
                        tool_trace=tool_trace if claimed.show_tool_calls else None,
                        extra_content=merge_response_extra_content(result.metadata_content, claimed.attachment_ids),
                    ),
                ),
                current,
            )
        if current.state in {"waiting", "ready"}:
            return (
                FinalDeliveryOutcome(
                    ending=AwaitingApproval(),
                    event_id=current.response_event_id,
                    is_visible_response=True,
                    final_visible_body=result.response_text,
                    delivery_kind="edited",
                    extra_content={STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING},
                ),
                current,
            )
        presentation = await self._advance_pause(current, result, target=target)
        current = await self.deps.approval_store.approval_continuation(claimed.approval_id) or claimed
        return (
            FinalDeliveryOutcome(
                ending=AwaitingApproval(),
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
    ) -> TurnEnding:
        """Run one claimed pause through the normal stoppable response lifecycle; return how it ended."""
        request = self.approval_response_request(claimed, target=target)
        await self.locked.refresh_mid_turn_context_for_approval(request)
        # The resume writes into the reply it continues from its first step.
        progress = TurnProgress(delivery_started=True)
        lifecycle = self.locked.build_lifecycle(request, response_kind=continuation_response_kind(claimed))
        post_effect_continuation = claimed
        awaiting = FinalDeliveryOutcome(
            ending=AwaitingApproval(),
            event_id=claimed.response_event_id,
            is_visible_response=True,
            extra_content={STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING},
        )

        async def continue_response(_message_id: str | None) -> FinalDeliveryOutcome:
            nonlocal post_effect_continuation
            try:
                outcome, post_effect_continuation = await self._execute_claimed_approval(
                    claimed,
                    request=request,
                    target=target,
                    identity=lifecycle.identity,
                )
            except (asyncio.CancelledError, Exception):
                delivery, _span_outcome = await run_coroutine_until_complete(self._read_final(claimed, recover=True))
                if delivery is None:
                    raise
                if delivery.acknowledged_event_id is None:
                    return awaiting
                return await self._approval_outcome_from_delivery(delivery)
            if (
                not isinstance(outcome.ending, Completed | AwaitingApproval | Owed)
                and (await self._read_final(claimed))[0]
            ):
                # A FINAL the resume froze before it failed is the approval's to finish once Matrix has it.
                return replace(awaiting, final_visible_body=outcome.final_visible_body)
            return outcome

        await self.locked.run_and_settle_locked_response(
            request,
            target=target,
            lifecycle=lifecycle,
            progress=progress,
            response_function=continue_response,
            build_post_response_outcome=lambda final: self._approval_post_response_outcome(
                post_effect_continuation,
                target=target,
                final=final,
            ),
            post_response_deps=lambda: self._approval_post_response_deps(claimed),
        )
        ending = progress.ending
        assert ending is not None, "a settled resume has its ending"
        match ending:
            case Completed():
                step = next_step(await self._facts(post_effect_continuation, recover=True), Resumed("completed"))
                if not await self._run_step(post_effect_continuation, step, target=target):
                    msg = "Approval continuation final delivery was not durably acknowledged"
                    raise RuntimeError(msg)
            case Cancelled() | Failed() | Suppressed():
                # A hook's suppression of a resumed answer fails the approval, as an error does.
                step = next_step(
                    await self._facts(post_effect_continuation, recover=True),
                    Resumed("failed", approval_failure(ending)),
                )
                await self._run_step(
                    post_effect_continuation,
                    step,
                    target=continuation_target(post_effect_continuation),
                )
            case AwaitingApproval() | Owed():
                pass
        return ending

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
            ending=_ending_of_span(span),
            event_id=event_id,
            is_visible_response=True,
            final_visible_body=body,
            delivery_kind="edited" if delivery.edits_event_id is not None else "sent",
            extra_content=visible,
            interactive_metadata=interactive_metadata,
        )

    async def _finalize_frozen_final(
        self,
        continuation: ApprovalContinuation,
        *,
        target: MessageTarget,
    ) -> bool:
        """Finalize an acknowledged FINAL the resume froze, as its response lifecycle would have, then finish.

        Returns whether a FINAL was there to finalize.
        """
        delivery, _span_outcome = await self._read_final(continuation)
        if delivery is None:
            return False
        recovered_outcome = await self._approval_outcome_from_delivery(delivery)
        request = self.approval_response_request(continuation, target=target)
        lifecycle = self.locked.build_lifecycle(request, response_kind=continuation_response_kind(continuation))
        await lifecycle.finalize(
            recovered_outcome,
            build_post_response_outcome=lambda final: self._approval_post_response_outcome(
                continuation,
                target=target,
                final=final,
            ),
            post_response_deps=lambda: self._approval_post_response_deps(continuation),
        )
        if not await self.deps.replies.finish_approval(continuation.approval_id):
            msg = "Recovered approval final delivery lost its journal ownership"
            raise RuntimeError(msg)
        return True

    def approval_response_request(
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
            run_succeeded=final.completed,
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
            # The resumed reply reports the whole turn's cost, but the usage stored before the pause may already be in a
            # scan, possibly of an earlier month, so its cost is not dated and budgets recount instead.
            notify_response_finished=self.locked.response_finished_notifier(
                continuation.sources.logical_source_event_ids,
                continuation.requester_id,
                None,
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
        return self.locked.memory_persistence(
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
        return self.locked.build_persist_response_event_id_effect(
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
        tool_context = self.deps.tool_runtime.build_turn_context(
            target,
            user_id=continuation.requester_id,
            agent_name=continuation.entity_name,
            active_model_name=continuation.runtime_model_name,
            attachment_ids=continuation.attachment_ids,
            correlation_id=correlation_id_for_request(request),
            source_envelope=request.response_envelope,
        )
        if tool_context.execution_identity != execution_identity:
            msg = "Approval continuation execution identity no longer matches its target"
            raise RuntimeError(msg)
        if cli_approval_handler is not None:
            tool_context = replace(tool_context, cli_approval_handler=cli_approval_handler)
        paused_answer = _paused_answer()
        with approval_receipt_context((*continuation.earlier_calls, *continuation.calls), run_id=continuation.run_id):
            if continuation.entity_kind == "team":
                async with response_typing_indicator(
                    self.locked.client(),
                    request,
                    response_run_id=continuation.run_id,
                ):
                    with tool_runtime_context(tool_context):
                        result = await continue_paused_team_run(
                            member_names=continuation.team_member_names,
                            mode=TeamMode(continuation.team_mode or "coordinate"),
                            config=self.deps.runtime.config,
                            runtime_paths=self.deps.runtime_paths,
                            execution_identity=execution_identity,
                            session_id=continuation.session_id,
                            run_id=continuation.run_id,
                            user_id=continuation.requester_id,
                            configured_team_name=continuation.entity_name,
                            model_name=resolve_turn_model(
                                self.deps.runtime.config,
                                self.deps.runtime_paths,
                                ModelScope(
                                    entity=continuation.entity_name,
                                    room_id=continuation.room_id,
                                    thread_id=continuation.thread_id,
                                    explicit=continuation.runtime_model_name,
                                ),
                                None,
                            ).model_name,
                            refresh_scheduler=self.locked.knowledge_refresh_scheduler(),
                            member_model_names=dict(continuation.team_member_model_names) or None,
                            approval_calls=continuation.calls,
                            history_scope=continuation.history_scope,
                            prior_tool_trace=paused_answer.tool_trace,
                            prior_presentation_state=paused_answer.team_state,
                            show_tool_calls=continuation.show_tool_calls,
                            tool_trace_collector=tool_trace_collector,
                            progress=progress,
                        )
            else:
                result = await self._approval_execution.continue_run(
                    continuation,
                    paused_answer=paused_answer,
                    tool_context=tool_context,
                    tool_trace_collector=tool_trace_collector,
                    typing_log_context=response_typing_log_context(
                        request,
                        response_run_id=continuation.run_id,
                    ),
                    progress=progress,
                )
        return result

    async def run_owned_approvals(
        self,
        owned: ApprovalContinuation,
        *,
        request: ResponseRequest,
        target: MessageTarget,
    ) -> None:
        """Run, recover, or settle each generation of the approval that owns a response's source."""
        step = next_step(await self._facts(owned, recover=True), Dispatch())
        while True:
            self.deps.logger.info(
                "response_source_owned_by_approval_continuation",
                source_event_id=request.response_envelope.source_event_id,
                approval_id=owned.approval_id,
                approval_state=owned.state,
            )
            if not isinstance(step, Resume):
                await self._run_step(owned, step, target=target)
                return
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
                await self._run_step(owned, Settle(_UNAUTHORIZED), target=target)
                return
            # Each claimed generation runs in its own resume span.
            async with self.deps.replies.span_scope() as resume_slot:
                claimed = await self._claim_owned_approval(owned, slot=resume_slot)
                if claimed is None:
                    return
                suspended = await self._run_owned_approval_continuation(claimed, target=target)
            if not suspended:
                return
            current = await self.deps.approval_store.approval_continuation_for_source(
                request.response_envelope.source_event_id,
            )
            if current is None or current.approval_id != claimed.approval_id:
                return
            advanced = current.generation > claimed.generation
            step = next_step(await self._facts(current, recover=True), Resumed("suspended", advanced=advanced))
            if not isinstance(step, Resume):
                await self._run_step(current, step, target=target)
                return
            owned = current

    async def _run_owned_approval_continuation(
        self,
        claimed: ApprovalContinuation,
        *,
        target: MessageTarget,
    ) -> bool:
        """Run a claimed generation; return whether it suspended again."""
        try:
            ending = await self._run_claimed_approval_lifecycle(claimed, target=target)
        except asyncio.CancelledError as error:
            # A shutdown that hands this run to a successor runtime records it as
            # restart-interrupted and leaves the span as a crash would, for that
            # runtime to hand back to replay. Read here: the detached task that
            # ends the span is no shutdown task.
            process_shutdown = current_task_is_process_shutdown()
            failure = (
                ApprovalFailure(ApprovalFailureKind.SHUTDOWN)
                if process_shutdown
                else cancelled_failure(classify_cancel_source(error))
            )
            if not process_shutdown:
                await run_coroutine_until_complete(self._end_live_resume_span())
            step = await run_coroutine_until_complete(
                self._end_resume(claimed, Resumed("cancelled", failure), target=target),
            )
            if isinstance(step, Fence):
                raise
            return False
        except Exception as error:
            failure = ApprovalFailure(ApprovalFailureKind.ERROR, str(error) or None)
            if not current_task_is_process_shutdown():
                await self._end_live_resume_span()
            await self._end_resume(claimed, Resumed("raised", failure), target=target)
            return False
        return isinstance(ending, AwaitingApproval | Owed)

    async def _end_resume(
        self,
        claimed: ApprovalContinuation,
        trigger: Resumed,
        *,
        target: MessageTarget,
    ) -> Step:
        """Recover the answer a raised or cancelled resume froze, or fence its observed claim; return the step run."""
        try:
            step = next_step(await self._facts(claimed, recover=True), trigger)
            if isinstance(step, Finish):
                await self._run_step(claimed, step, target=target)
                return step
        except Exception:
            self.deps.logger.exception(
                "approval_final_recovery_failed_before_failure_fence",
                approval_id=claimed.approval_id,
            )
            step = next_step(self._observed_facts(claimed), trigger)
        if not isinstance(step, Settle):
            await self._run_step(claimed, step, target=target)
            return step
        self.deps.logger.exception("approval_continuation_failed", approval_id=claimed.approval_id)
        # The fence names the claim the resume observed before its settlement reads anything more.
        failing = await self._request_failure(claimed, step.failure)
        if failing is not None:
            await self._run_step(failing, step, target=target)
        return step

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

    async def recover_approval_final(self, approval_id: str) -> bool:
        """Finish one paused reply whose entity is gone through its final answer, under its original bot principal."""
        continuation = await self.deps.approval_store.approval_continuation(approval_id)
        if continuation is None:
            return True
        target = continuation_target(
            continuation,
            reply_to_event_id=continuation.source_event_ids[0],
        )
        request = self.approval_response_request(continuation, target=target)

        async def recover(_target: MessageTarget) -> bool:
            # The orchestrator's recovery discards a paused reply with no answer itself.
            step = next_step(await self._facts(continuation, recover=True), OwnerUnavailable())
            return await self._run_step(continuation, step, target=target)

        return await self.lifecycle_coordinator.run_locked_response(
            target=target,
            response_envelope=request.response_envelope,
            pipeline_timing=None,
            locked_operation=recover,
            signal_queued_message=False,
        )

    async def _cli_exit(self, source_event_id: str, trigger: CliExit) -> None:
        """Run what a minimal-mode response's approval scope leaves its paused reply to do as it ends."""
        current = await self.deps.approval_store.approval_continuation_for_source(source_event_id)
        if current is None:
            return
        step = next_step(await self._facts(current, recover=False), trigger)
        await self._run_step(current, step, target=continuation_target(current))

    # -----------------------------------------------------------------------
    # Steps

    async def _facts(self, continuation: ApprovalContinuation, *, recover: bool) -> ContinuationFacts:
        """Read the facts the rules decide by, recovering FINAL delivery first when ``recover`` asks for it."""
        store = self.deps.replies.store
        final, final_span_outcome = await self._read_final(continuation, recover=recover)
        claim = None if continuation.claim_span_id is None else await store.replies.span(continuation.claim_span_id)
        return continuation_facts(
            continuation,
            runtime_generation=self.deps.approval_runtime_generation,
            final=final,
            final_span_outcome=final_span_outcome,
            claim_outcome=None if claim is None else claim.outcome,
        )

    def _observed_facts(self, continuation: ApprovalContinuation) -> ContinuationFacts:
        """Return the facts of a continuation as observed, taking it to have no final answer."""
        return continuation_facts(
            continuation,
            runtime_generation=self.deps.approval_runtime_generation,
            final=None,
            final_span_outcome=None,
        )

    async def _run_step(self, observed: ApprovalContinuation, step: Step, *, target: MessageTarget) -> bool:
        """Run one step of the paused reply ``observed``; return whether its reply now answers its turn."""
        match step:
            case Finish(finalize=True):
                return await self._finalize_frozen_final(observed, target=target)
            case Finish():
                return await self.deps.replies.finish_approval(observed.approval_id)
            case Settle(failure=failure):
                return await self._settle(observed, failure)
            case Release(failure=failure):
                await self._release_to_replay(observed, failure)
            case Fence(failure=failure):
                await self._request_failure(observed, failure)
            case WakeSources():
                self.deps.retry_approval_sources(observed.room_id, observed.source_event_ids)
            case Wait() | Resume() | DiscardUnavailable():
                # Waiting does nothing; a dispatch resumes, and the orchestrator's recovery discards, themselves.
                pass
            case _:
                assert_never(step)
        return False

    async def _read_final(
        self,
        continuation: ApprovalContinuation,
        *,
        recover: bool = False,
    ) -> tuple[MatrixDelivery | None, rl.SpanOutcome | None]:
        """Read the continuation's FINAL and the outcome of its span, retrying an unsent one's delivery first when asked."""
        store = self.deps.replies.store
        final, span_outcome = await read_final(store, continuation)
        if recover and final is not None and final.acknowledged_event_id is None and not final.permanently_failed:
            await self.deps.delivery_gateway.recover_deliveries()
            final, span_outcome = await read_final(store, continuation)
        return final, span_outcome

    async def _answer_stands(self, continuation: ApprovalContinuation) -> bool:
        """Return whether the run's own completed answer is queued or delivered, which no failure may replace."""
        final, span_outcome = await self._read_final(continuation)
        return final is not None and not final.permanently_failed and span_outcome is rl.SpanOutcome.COMPLETED

    async def _request_failure(
        self,
        continuation: ApprovalContinuation,
        failure: ApprovalFailure,
    ) -> ApprovalContinuation | None:
        """Fence exactly the state a failed lifecycle observed and wake settlement."""
        failing = await self.deps.approval_store.request_approval_failure(
            continuation.approval_id,
            failure,
            expected_state=continuation.state,
            expected_generation=continuation.generation,
            expected_runtime_generation=continuation.runtime_generation,
        )
        if failing is not None:
            self.deps.retry_approval_sources(failing.room_id, failing.source_event_ids)
        return failing

    async def _settle(self, observed: ApprovalContinuation, failure: ApprovalFailure) -> bool:
        """Settle a failed paused reply: fence it, expire its cards, cancel its delegations, and end its reply.

        The fence names the state the step was decided from, unless the paused reply
        already fails. The reply ends without a note when it is gone or its
        final answer settled, and shows the failure's note otherwise.
        """
        current = await self.deps.approval_store.approval_continuation(observed.approval_id)
        if current is None:
            return True
        if await self._answer_stands(current):
            return False
        current = await self._prepare_failure(current if current.state == "failing" else observed, failure)
        if current is None:
            return False
        if await self.deps.replies.finish_approval(current.approval_id):
            return True
        written = await self.deps.delivery_gateway.write_approval_failure_note(
            current.response_event_id,
            approval_id=current.approval_id,
            note=_FAILURE_NOTES.get(failure.kind, NoteKind.ERROR),
            text=failure.reason,
            target=continuation_target(current),
        )
        # The reply's records show the note; the finish ends the reply.
        return written and await self.deps.replies.finish_approval(current.approval_id)

    async def _release_to_replay(self, continuation: ApprovalContinuation, failure: ApprovalFailure) -> None:
        """End an interrupted continuation's cards and release it: its sources go back to replay unless a Stop ends the reply."""
        current = await self._prepare_failure(continuation, failure)
        if current is not None:
            await self.deps.replies.release_approval(current.approval_id, current.generation)

    async def _prepare_failure(
        self,
        continuation: ApprovalContinuation,
        failure: ApprovalFailure,
    ) -> ApprovalContinuation | None:
        """Fence a continuation for failure, expire its cards, and cancel its delegations; ``None`` when that failed."""
        manager = approval_manager.get_approval_store()
        current = await prepare_approval_failure(
            continuation,
            failure,
            request_failure=partial(self._request_failure, continuation),
            expire_cards=None if manager is None else manager.expire_continuation_cards,
        )
        if current is None:
            return None
        await cancel_approval_delegations(
            current,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            reason=failure.reason,
        )
        return current
