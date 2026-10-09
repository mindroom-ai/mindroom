"""Own visible Matrix delivery for already-generated responses."""

from __future__ import annotations

import asyncio
import json
import time
from collections import ChainMap
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import partial
from html import escape as html_escape
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import NAMESPACE_URL, uuid5
from weakref import WeakValueDictionary

import nio
from nio.exceptions import SendRetryError

from mindroom import constants, interactive
from mindroom import reply_lifecycle as rl
from mindroom.constants import ACTING_REQUESTER_KEY, SKIP_MENTIONS_KEY
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.event_journal import (
    MatrixDelivery,
    MatrixDeliveryView,
    ProjectedEvent,
    TerminalTurnWrite,
    matrix_delivery_payload,
    replacement_target,
    thread_root,
)
from mindroom.event_journal.models import DURABLE_DELIVERY_ID_KEY
from mindroom.event_journal.replies import (
    AppliedTransition,
    PreparedReplyRow,
    ReplyRowEnqueue,
    ReplyRowRequest,
    edit_delivery_id,
)
from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome
from mindroom.handled_turns import TurnRecord, TurnRecordCodec
from mindroom.hooks import (
    EVENT_MESSAGE_AFTER_RESPONSE,
    EVENT_MESSAGE_BEFORE_RESPONSE,
    EVENT_MESSAGE_CANCELLED,
    EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM,
    AfterResponseContext,
    BeforeResponseContext,
    CancelledResponseContext,
    CancelledResponseInfo,
    FinalResponseDraft,
    FinalResponseTransformContext,
    HookContextSupport,
    ResponseDraft,
    ResponseResult,
    emit,
    emit_final_response_transform,
    emit_transform,
)
from mindroom.matrix.client_delivery import (
    DeliveredMatrixEvent,
    MatrixDeliveryFailure,
    MatrixDeliveryFailureKind,
    MatrixSendOutcome,
    build_edit_event_content,
    edit_message_outcome,
    resolve_room_encryption_outcome,
    send_message_outcome,
    send_room_event_result,
)
from mindroom.matrix.large_messages import MatrixEventTooLargeError, prepare_large_message
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_builder import build_message_content, build_reaction_content
from mindroom.matrix.room_history_reads import (
    find_outbox_delivery_event_id_via_room_messages,
    missing_outbox_delivery_copy_indices_via_room_messages,
)
from mindroom.matrix.segmented_messages import segment_matrix_content
from mindroom.matrix_delivery import (
    DeliveryStage,
    MatrixDeliveryWorker,
    PermanentDeliveryError,
    RecoveryOutcome,
    ReplyRowDelivery,
    ReplyRowEnqueuer,
    SendDelivery,
    TurnHandoff,
)
from mindroom.message_target import MessageTarget
from mindroom.reply_lifecycle import Outcome as ReplyOutcome
from mindroom.reply_lifecycle import ProgressConfirmation as ReplyProgressConfirmation
from mindroom.reply_lifecycle import ReplyState
from mindroom.reply_presentation import (
    NoteKind,
    Presentation,
    RenderedReply,
    Segment,
    decode_presentation,
    note_segment,
    render,
    render_body,
    with_trailing_note,
)
from mindroom.reply_scope import (
    ReplyWrite,
    ReplyWriteRefusedError,
    SpanHandle,
    approval_note_write,
    current_span,
    initial_write,
    interrupted_end,
    owed_note_write,
    release_decision,
    suppress_decision,
    terminal_source_decision,
    terminal_write,
)
from mindroom.requester_identity import is_access_checked_requester_id
from mindroom.response_shutdown_diagnostics import ResponseShutdownPhase, response_shutdown_phase
from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001
from mindroom.scheduled_run_records import record_silent_schedule_result_if_needed
from mindroom.stop import send_stop_button
from mindroom.streaming import (
    USER_STOP_CANCEL_MSG,
    FinalTextTransform,
    ProgressPermission,
    ProgressState,
    StreamingResponse,
    build_cancelled_response_update,
    cancel_failure_reason,
    cancel_source_from_failure_reason,
    classify_cancel_source,
    current_task_is_process_shutdown,
    interactive_response_for_visible_body,
    send_streaming_response,
    stream_progress_edits,
    strip_matching_visible_tool_markers,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager

    import structlog

    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.event_journal.replies import Decide, PostCommitEffect
    from mindroom.history.types import (
        CompactionLifecycleFailure,
        CompactionLifecycleProgress,
        CompactionLifecycleStart,
        CompactionOutcome,
    )
    from mindroom.hooks import MessageEnvelope
    from mindroom.response_sources import ResponseSources
    from mindroom.streaming import CancelSource, ProgressPublisher, StreamInputChunk
    from mindroom.timing import DispatchPipelineTiming
    from mindroom.tool_system.events import StructuredStreamChunk, ToolTraceEntry

_BEFORE_RESPONSE_HOOK_FAILURE_TEXT = "Response failed. Please retry."
_PLACEHOLDER_DELIVERY_FAILURE_REASONS = frozenset(
    {
        "delivery_failed",
        "terminal_update_cancelled",
        "terminal_update_failed",
    },
)
_SEGMENT_PAYLOADS_RESULT_KEY = "io.mindroom.matrix_segment_payloads"


@dataclass(frozen=True, slots=True)
class _PreparedWirePayload:
    """One frozen primary event plus any lossless continuation events."""

    content: dict[str, Any]
    continuation_payloads: tuple[dict[str, Any], ...] = ()


def _result_with_segment_payloads(
    result: dict[str, object] | None,
    prepared: _PreparedWirePayload | MatrixDeliveryFailure,
) -> dict[str, object] | None:
    """Store continuation payloads in local outbox state, never on Matrix."""
    if isinstance(prepared, MatrixDeliveryFailure) or not prepared.continuation_payloads:
        return result
    return {**(result or {}), _SEGMENT_PAYLOADS_RESULT_KEY: list(prepared.continuation_payloads)}


def _continuation_payloads(result: Mapping[str, object] | None) -> tuple[dict[str, Any], ...]:
    """Read the frozen continuation list from an outbox result, if present."""
    if result is None:
        return ()
    raw_payloads = result.get(_SEGMENT_PAYLOADS_RESULT_KEY, [])
    assert isinstance(raw_payloads, list), f"corrupt outbox result: {_SEGMENT_PAYLOADS_RESULT_KEY} is not a list"
    return tuple(dict(cast("dict[str, Any]", payload)) for payload in raw_payloads)


def _segment_transaction_id(base_transaction_id: str, index: int) -> str:
    """Derive a stable Matrix transaction ID for one continuation event."""
    return str(uuid5(NAMESPACE_URL, f"mindroom-matrix-segment:{base_transaction_id}:{index}"))


def _refused_reply_outcome(
    refused: ReplyWriteRefusedError | None,
    request: FinalDeliveryRequest,
    tool_trace: Sequence[ToolTraceEntry] | None,
    extra_content: dict[str, Any] | None,
) -> FinalDeliveryOutcome:
    """Report an answer the reply's rules refused, or whose span already ended: a Stop to apply first, or a span that no longer owns it."""
    stopped = refused is not None and refused.transition.outcome in {ReplyOutcome.RECOMPUTE, ReplyOutcome.STOPPED}
    return FinalDeliveryOutcome(
        terminal_status="cancelled" if stopped else "error",
        event_id=request.existing_event_id,
        is_visible_response=request.existing_event_id is not None,
        cancel_source="user_stop" if stopped else None,
        failure_reason=cancel_failure_reason("user_stop") if stopped else "reply_write_refused",
        tool_trace=tuple(tool_trace or ()),
        extra_content=extra_content,
    )


def _reply_body(
    text: str,
    tool_trace: list[ToolTraceEntry] | None,
    shown: Presentation | None,
) -> tuple[str, list[ToolTraceEntry] | None]:
    """Return the body and trace a reply write shows.

    Once a reply holds more than the writing span's answer, such as a resume
    below a replay's stopped attempt, only its whole presentation says that;
    the span's own text would replace what the reply already shows. A reply
    that hides tool calls sends no trace.
    """
    if shown is None or len(shown.segments) < 2:
        return text, tool_trace
    body, trace = render_body(shown)
    return body, (list(trace) or None) if shown.show_tool_calls else None


def _take_published(published: dict[str, Presentation], body: str) -> Presentation | None:
    """Return what a published whole-reply body shows, forgetting the bodies published before it.

    The streamer only ever sends its latest state, so once this body is
    written ahead no earlier one will be.
    """
    whole = published.get(body)
    if whole is None:
        return None
    for earlier in list(published):
        if earlier == body:
            break
        del published[earlier]
    return whole


@asynccontextmanager
async def _whole_reply_progress(
    progress: AbstractAsyncContextManager[ProgressPublisher],
    handle: SpanHandle,
    published: dict[str, Presentation],
) -> AsyncIterator[ProgressPublisher]:
    """Publish the whole reply for each of the span's own publications, remembering what each body shows.

    A body stays remembered until a later one is written ahead, which is the
    only state the streamer sends from then on.
    """
    async with progress as publish:

        async def publish_whole(chunk: StructuredStreamChunk) -> None:
            shown = handle.presentation(
                chunk.content,
                tuple(chunk.tool_trace or ()),
                team_state=chunk.presentation_state,
            )
            body, trace = render_body(shown)
            published[body] = shown
            await publish(replace(chunk, content=body, tool_trace=list(trace)))

        yield publish_whole


def _owed_answer_outcome() -> FinalDeliveryOutcome:
    """Return the outcome of an answer whose row is recorded but not yet in the room.

    Its sources were handed over with the row, recovery sends it, and only a
    permanent refusal changes the reply, as an owed ``FINAL`` reports ``suspended``.
    """
    return FinalDeliveryOutcome(terminal_status="suspended", event_id=None, failure_reason="delivery_failed")


def _with_note(shown: Presentation, note: Segment) -> Presentation:
    """Return what a reply shows with one note: below its content, or instead of it."""
    if note.note in {NoteKind.DELIVERY_FAILED, NoteKind.APPROVAL_FAILED}:
        # These notes replace the reply's body.
        return with_trailing_note(replace(shown, segments=()), note)
    return with_trailing_note(shown, note)


def _shown_before(reply: rl.Reply, handle: SpanHandle | None) -> Presentation:
    """Return what the reply may show now, as its latest write recorded it."""
    stored = reply.possibly_shown if reply.possibly_shown is not None else reply.presentation
    shown = decode_presentation(stored)
    if handle is not None:
        shown = replace(shown, placeholder=handle.base.placeholder, show_tool_calls=handle.base.show_tool_calls)
    return shown


def _terminal_status_for_reply_state(state: ReplyState) -> Literal["completed", "cancelled", "error"]:
    if state is ReplyState.COMPLETED:
        return "completed"
    if state is ReplyState.CANCELLED:
        return "cancelled"
    return "error"


def _reply_state_for_stream_status(status: object) -> ReplyState:
    """Return the reply state a terminal stream update's wire status stands for."""
    if status == constants.STREAM_STATUS_COMPLETED:
        return ReplyState.COMPLETED
    if status == constants.STREAM_STATUS_CANCELLED:
        return ReplyState.CANCELLED
    return ReplyState.FAILED


def _reply_row_wire_content(claimed: MatrixDelivery) -> dict[str, Any]:
    """Return the event one claimed row sends, wrapping a reply edit whose target was bound late.

    A reply row prepared before the reply's create was acknowledged stores the
    replacement content alone; its target is the event that create bound,
    known only when the row is claimed. Wrapping is deterministic, so a resend
    sends the same bytes under the same transaction ID.
    """
    payload = dict(claimed.payload)
    if claimed.reply_id is None or claimed.edits_event_id is None or "m.new_content" in payload:
        return payload
    new_text = None if claimed.reply_row is None else claimed.reply_row.new_text
    envelope = build_edit_event_content(
        event_id=claimed.edits_event_id,
        new_content=payload,
        new_text=new_text if new_text is not None else str(payload.get("body", "")),
    )
    envelope[DURABLE_DELIVERY_ID_KEY] = payload.get(DURABLE_DELIVERY_ID_KEY)
    return envelope


def _is_placeholder_delivery_failure(failure_reason: str) -> bool:
    """Return whether a placeholder-only error came from Matrix delivery itself."""
    return failure_reason in _PLACEHOLDER_DELIVERY_FAILURE_REASONS or failure_reason.startswith(
        "terminal_update_exception:",
    )


class _DeliveryRefusedError(RuntimeError):
    """Matrix declined one outbox delivery, leaving it unacknowledged."""


class _DeliveryObservationError(RuntimeError):
    """Matrix did not provide authoritative metadata for a delivered event."""


@dataclass(frozen=True)
class ResponseIdentity:
    """Identify which visible response a delivery or hook call belongs to."""

    response_kind: str
    response_envelope: MessageEnvelope
    correlation_id: str
    sources: ResponseSources
    participating_agent_names: tuple[str, ...] = ()


@dataclass
class ResponseHookService:
    """Own response hook execution around final delivery."""

    hook_context: HookContextSupport

    async def _apply_before_response(
        self,
        *,
        identity: ResponseIdentity,
        response_text: str,
        tool_trace: list[ToolTraceEntry] | None,
        extra_content: dict[str, Any] | None,
    ) -> ResponseDraft:
        draft = ResponseDraft(
            response_text=response_text,
            response_kind=identity.response_kind,
            tool_trace=deepcopy(tool_trace) if tool_trace is not None else None,
            extra_content=deepcopy(extra_content) if extra_content is not None else None,
            envelope=identity.response_envelope,
        )
        if not self.hook_context.registry.has_hooks(EVENT_MESSAGE_BEFORE_RESPONSE):
            return draft
        context = BeforeResponseContext(
            **self.hook_context.base_kwargs(EVENT_MESSAGE_BEFORE_RESPONSE, identity.correlation_id),
            draft=draft,
        )
        return await emit_transform(self.hook_context.registry, EVENT_MESSAGE_BEFORE_RESPONSE, context)

    async def _apply_final_response_transform(
        self,
        *,
        identity: ResponseIdentity,
        response_text: str,
    ) -> FinalResponseDraft:
        if current_task_is_process_shutdown():
            raise asyncio.CancelledError
        draft = FinalResponseDraft(
            response_text=response_text,
            response_kind=identity.response_kind,
            envelope=identity.response_envelope,
        )
        if not self.hook_context.registry.has_hooks(EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM):
            return draft
        context = FinalResponseTransformContext(
            **self.hook_context.base_kwargs(EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM, identity.correlation_id),
            draft=draft,
        )
        draft = await emit_final_response_transform(
            self.hook_context.registry,
            EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM,
            context,
        )
        if current_task_is_process_shutdown():
            raise asyncio.CancelledError
        return draft

    async def emit_after_response(  # noqa: D102
        self,
        *,
        identity: ResponseIdentity,
        response_text: str,
        response_event_id: str,
        delivery_kind: Literal["sent", "edited"],
        continue_on_cancelled: bool = False,
    ) -> None:
        if not self.hook_context.registry.has_hooks(EVENT_MESSAGE_AFTER_RESPONSE):
            return
        context = AfterResponseContext(
            **self.hook_context.base_kwargs(EVENT_MESSAGE_AFTER_RESPONSE, identity.correlation_id),
            result=ResponseResult(
                response_text=response_text,
                response_event_id=response_event_id,
                delivery_kind=delivery_kind,
                response_kind=identity.response_kind,
                envelope=identity.response_envelope,
            ),
        )
        await emit(
            self.hook_context.registry,
            EVENT_MESSAGE_AFTER_RESPONSE,
            context,
            continue_on_cancelled=continue_on_cancelled,
        )

    async def emit_cancelled_response(  # noqa: D102
        self,
        *,
        identity: ResponseIdentity,
        visible_response_event_id: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        if not self.hook_context.registry.has_hooks(EVENT_MESSAGE_CANCELLED):
            return
        context = CancelledResponseContext(
            **self.hook_context.base_kwargs(EVENT_MESSAGE_CANCELLED, identity.correlation_id),
            info=CancelledResponseInfo(
                envelope=identity.response_envelope,
                visible_response_event_id=visible_response_event_id,
                response_kind=identity.response_kind,
                failure_reason=failure_reason,
            ),
        )
        await emit(self.hook_context.registry, EVENT_MESSAGE_CANCELLED, context)


@dataclass(frozen=True)
class SendTextRequest:  # noqa: D101
    target: MessageTarget
    response_text: str
    skip_mentions: bool = False
    tool_trace: list[ToolTraceEntry] | None = None
    extra_content: dict[str, Any] | None = None
    retry_sync_recovery: bool = False
    # The turn this send belongs to, when it belongs to one. Present, the send
    # goes through the outbox and carries a transaction ID derived from this
    # value, so a resend after a crash collapses onto the event the homeserver
    # already accepted. Absent, the send is not a turn -- a voice echo, a
    # command confirmation -- and takes the direct path, because a synthetic
    # turn ID would put a row in the outbox that recovery cannot reason about.
    delivery_turn_id: str | None = None
    delivery_result: dict[str, object] | None = None
    # Set when this send is a durable write of an agent or team reply.
    reply_write: ReplyWrite | None = None


@dataclass(frozen=True)
class EditTextRequest:  # noqa: D101
    target: MessageTarget
    event_id: str
    new_text: str
    tool_trace: list[ToolTraceEntry] | None = None
    extra_content: dict[str, Any] | None = None
    retry_sync_recovery: bool = False
    delivery_result: dict[str, object] | None = None
    # Set when this edit is a durable write of an agent or team reply.
    reply_write: ReplyWrite | None = None


@dataclass(frozen=True)
class FinalDeliveryRequest:  # noqa: D101
    target: MessageTarget
    existing_event_id: str | None
    response_text: str
    identity: ResponseIdentity
    tool_trace: list[ToolTraceEntry] | None
    extra_content: dict[str, Any] | None
    skip_mentions: bool = False


@dataclass(frozen=True)
class MatrixCompactionLifecycle:
    """Matrix-backed compaction lifecycle notice adapter."""

    delivery_gateway: DeliveryGateway
    target: MessageTarget
    reply_to_event_id: str | None

    async def start(self, event: CompactionLifecycleStart) -> str | None:
        """Send the initial visible lifecycle notice."""
        return await self.delivery_gateway._send_compaction_lifecycle_start(
            target=self.target,
            reply_to_event_id=self.reply_to_event_id,
            event=event,
        )

    async def progress(self, event: CompactionLifecycleProgress) -> None:
        """Edit the lifecycle notice after persisted compaction progress."""
        await self.delivery_gateway._edit_compaction_lifecycle_progress(
            target=self.target,
            event=event,
        )

    async def complete_success(self, outcome: CompactionOutcome) -> None:
        """Edit the lifecycle notice after successful compaction."""
        await self.delivery_gateway._edit_compaction_lifecycle_success(
            target=self.target,
            outcome=outcome,
        )

    async def complete_failure(self, event: CompactionLifecycleFailure) -> None:
        """Edit the lifecycle notice after failed compaction."""
        await self.delivery_gateway._edit_compaction_lifecycle_failure(
            target=self.target,
            event=event,
        )


@dataclass(frozen=True)
class StreamingDeliveryRequest:
    """Parameters for streamed Matrix delivery."""

    target: MessageTarget
    response_stream: AsyncIterator[StreamInputChunk]
    # The visible response this stream is. Required, because every stream is
    # some turn's answer: the gateway reads the causing event from it to key
    # the durable terminal delivery, and the final-answer transform from it to
    # shape the text before that payload is frozen rather than as a second edit
    # after it was delivered.
    identity: ResponseIdentity
    existing_event_id: str | None = None
    adopt_existing_placeholder: bool = False
    show_tool_calls: bool = False
    extra_content: dict[str, Any] | None = None
    tool_trace_collector: list[ToolTraceEntry] | None = None
    streaming_cls: type[StreamingResponse] = StreamingResponse
    pipeline_timing: DispatchPipelineTiming | None = None
    visible_event_id_callback: Callable[[str], None] | None = None
    visible_progress_callback: Callable[[str], None] | None = None
    preserve_existing_visible_on_empty_terminal: bool = False
    allow_new_terminal_message: Callable[[], bool] | None = None


@dataclass(frozen=True)
class DeliveryGatewayDeps:
    """Explicit dependencies needed for Matrix delivery."""

    runtime: SupportsClientConfig
    runtime_paths: RuntimePaths
    agent_name: str
    logger: structlog.stdlib.BoundLogger
    redact_message_event: Callable[..., Awaitable[bool]]
    resolver: ConversationResolver
    response_hooks: ResponseHookService
    outbox: MatrixDeliveryView
    # Contract 2's handoff: the journal owns an actionable source until the
    # turn's answer is durably owed to a room, and this is where that becomes
    # true. Everything after it is the outbox's to recover.
    turn_handoff: TurnHandoff
    # The Matrix device this process sends as, asked for rather than held: the
    # gateway is built before login, and a re-login replaces it. ``None`` means
    # no login has completed, which the outbox reads as "device unknown".
    sending_device_id: Callable[[], str | None] = lambda: None
    # The terminal turn record a FINAL acknowledgement should commit with it,
    # asked for only once the event ID exists. Returning ``None`` means there
    # is nothing to bind -- no record for the turn, or one that already knows
    # its response event. Without this the acknowledgement and the record are
    # two commits, and a crash between them leaves a delivered answer whose
    # record cannot be edited.
    terminal_turn_for: Callable[[str, str], TurnRecord | None] | None = None
    # Told after an acknowledgement bound its row, so the record that commit
    # wrote is re-asserted through the ledger's own write ordering. Skipping
    # that leaves the row open to a mutation that derived before the commit and
    # lands after it, which erases the event the answer is stored under.
    terminal_turn_committed: Callable[[str, str, TurnRecord | None], Awaitable[None]] | None = None
    # Runs what committed reply transitions left for after their commit:
    # cancelling a span a Stop reached, waking an approval source.
    reply_effects: Callable[[tuple[PostCommitEffect, ...]], Awaitable[None]] | None = None
    # Wakes claims that waited for a reply's earlier writes.
    reply_row_resolved: Callable[[str], None] | None = None


_MATRIX_DELIVERY_FAILURE_REASONS: dict[MatrixDeliveryFailureKind, str] = {
    MatrixDeliveryFailureKind.ENCRYPTION_GUARD: "encrypted delivery rejected by local trust policy",
    MatrixDeliveryFailureKind.UNKNOWN_ENCRYPTION_STATE: "room encryption state unknown",
    MatrixDeliveryFailureKind.PAYLOAD_TOO_LARGE: "matrix event exceeds the hard size limit",
    MatrixDeliveryFailureKind.SEND_EXCEPTION: "matrix delivery raised a local exception",
    MatrixDeliveryFailureKind.UNEXPECTED_RESPONSE: "matrix delivery returned an unexpected response",
}


def _matrix_delivery_failure_reason(outcome: MatrixSendOutcome) -> str:
    """Translate one typed Matrix delivery failure into the gateway failure vocabulary."""
    if isinstance(outcome, MatrixDeliveryFailure):
        return f"{_MATRIX_DELIVERY_FAILURE_REASONS[outcome.kind]}: {outcome.detail}"
    return "matrix delivery failed"


@dataclass(frozen=True)
class FinalizeStreamedResponseRequest:
    """Parameters for finalizing one streamed Matrix response."""

    target: MessageTarget
    stream_transport_outcome: StreamTransportOutcome
    initial_delivery_kind: Literal["sent", "edited"]
    identity: ResponseIdentity
    tool_trace: list[ToolTraceEntry] | None
    extra_content: dict[str, Any] | None
    existing_event_id: str | None = None
    existing_event_is_placeholder: bool = False


@dataclass(frozen=True)
class DeliveryGateway:
    """Send, edit, redact, and finalize visible Matrix responses."""

    deps: DeliveryGatewayDeps
    _delivery_turn_locks: WeakValueDictionary[str, asyncio.Lock] = field(
        default_factory=WeakValueDictionary,
        init=False,
        repr=False,
        compare=False,
    )

    def _ready_client(self) -> nio.AsyncClient:
        """Return the current Matrix client required for delivery."""
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for response delivery"
            raise RuntimeError(msg)
        return client

    async def send_judgment_reaction(
        self,
        *,
        identity: ResponseIdentity,
        room_id: str,
        event_id: str,
        key: str,
        kind: Literal["participation_decline", "mid_turn_defer"],
    ) -> None:
        """Best-effort acknowledgement of a deliberate judgment, stable across replays."""
        if not await self._visible_notice_is_current(identity, room_id):
            return
        client = self._ready_client()
        # Exclude the emoji so a config reload cannot duplicate a replayed reaction.
        transaction_id = str(
            uuid5(NAMESPACE_URL, json.dumps([f"mindroom-{kind.replace('_', '-')}", client.user_id, room_id, event_id])),
        )
        result = await send_room_event_result(
            client,
            room_id,
            "m.reaction",
            build_reaction_content(event_id, key),
            transaction_id=transaction_id,
            operation=f"{kind}_reaction",
        )
        if not isinstance(result, nio.RoomSendResponse):
            self.deps.logger.warning("Judgment reaction failed", kind=kind, room_id=room_id, event_id=event_id)

    @staticmethod
    def _cancelled_error_failure_reason(error: asyncio.CancelledError) -> str:
        """Normalize CancelledError values to the canonical cancellation reason strings."""
        return cancel_failure_reason(classify_cancel_source(error))

    def terminal_outcome_without_visible_event(
        self,
        *,
        terminal_status: Literal["cancelled", "error"],
        failure_reason: str,
    ) -> FinalDeliveryOutcome:
        """Return the terminal outcome for one turn with no visible event to finalize."""
        return FinalDeliveryOutcome(
            terminal_status=terminal_status,
            event_id=None,
            failure_reason=failure_reason,
        )

    def cancelled_outcome(
        self,
        event_id: str,
        cancel_source: Literal["user_stop", "sync_restart", "interrupted"],
    ) -> FinalDeliveryOutcome:
        """Report a cancellation of a visible reply, whose span's exit writes the note through its records."""
        _cancelled_text, stream_status = build_cancelled_response_update("", cancel_source=cancel_source)
        return FinalDeliveryOutcome(
            terminal_status="cancelled",
            event_id=event_id,
            cancel_source=cancel_source,
            failure_reason=cancel_failure_reason(cancel_source),
            extra_content={constants.STREAM_STATUS_KEY: stream_status},
        )

    def cancelled_terminal_outcome(
        self,
        outcome: FinalDeliveryOutcome,
        *,
        failure_reason: str,
    ) -> FinalDeliveryOutcome:
        """Return the cancelled derivative of one delivery outcome, preserving visible facts."""
        return FinalDeliveryOutcome(
            terminal_status="cancelled",
            event_id=outcome.final_visible_event_id,
            is_visible_response=outcome.final_visible_event_id is not None,
            final_visible_body=outcome.final_visible_body,
            failure_reason=failure_reason,
            tool_trace=outcome.tool_trace,
            extra_content=outcome.extra_content,
        )

    async def _cleanup_completed_placeholder_only_stream(
        self,
        *,
        failure_reason: str,
        tool_trace: list[ToolTraceEntry] | None,
        extra_content: dict[str, Any] | None,
    ) -> FinalDeliveryOutcome:
        """End a stream that showed only its placeholder; the reply's records remove the placeholder."""
        handle = self._live_span()
        if handle is not None:
            await self.end_reply_span(handle, suppress_decision(handle))
        return FinalDeliveryOutcome(
            terminal_status="error",
            event_id=None,
            failure_reason=failure_reason,
            tool_trace=tuple(tool_trace or ()),
            extra_content=extra_content,
        )

    async def _visible_notice_is_current(self, identity: ResponseIdentity, room_id: str) -> bool:
        """Return whether a terminal notice still belongs in the room it names.

        Terminal notices -- cancellations, failure updates, suppression
        cleanup -- are direct transport. They carry no answer, so they never
        reach the outbox, and the durable refusal that protects a turn's
        answer does not protect them.
        """
        return await self.deps.outbox.turn_membership_is_current(
            turn_id=identity.response_envelope.source_event_id,
            room_id=room_id,
        )

    async def _acknowledged_delivery(
        self,
        turn_id: str,
        stage: DeliveryStage,
        event_id: str,
        fallback: dict[str, Any],
    ) -> DeliveredMatrixEvent:
        """Return what was actually delivered for an already-acknowledged row.

        The payload comes from the row, not from this caller. A rerun turn can
        arrive with regenerated text, and reporting that as what is in the room
        would tell every downstream consumer that the event says something it
        does not -- under the event ID of the message that really was sent.
        """
        row = await self.deps.outbox.load_matrix_delivery(delivery_id=turn_id, stage=stage)
        content = dict(row.payload) if row is not None else fallback
        return DeliveredMatrixEvent(event_id=event_id, content_sent=content)

    async def _send_claimed(
        self,
        claimed: MatrixDelivery,
        *,
        retry_sync_recovery: bool,
        operation: str = "send_message",
    ) -> DeliveredMatrixEvent:
        """Send one claimed delivery exactly as it was frozen, continuations included.

        The payload is the stored envelope, not something rebuilt from the
        request, and the transaction ID is the one the row already holds. A
        retry that rebuilt either would let the homeserver accept the resend
        as a new event instead of collapsing it onto the earlier one, mint a
        duplicate, and leave the durable result and the room disagreeing
        forever. Continuations get deterministic transaction IDs derived from
        the row's, so their retries collapse the same way.
        """
        outcome = await self._send_frozen(
            claimed,
            _reply_row_wire_content(claimed),
            transaction_id=claimed.transaction_id,
            what="delivery",
            operation="edit_message" if claimed.edits_event_id is not None else operation,
            retry_sync_recovery=retry_sync_recovery,
        )
        for index, continuation in enumerate(_continuation_payloads(claimed.result), start=1):
            await self._send_frozen(
                claimed,
                continuation,
                transaction_id=_segment_transaction_id(claimed.transaction_id, index),
                what=f"continuation {index}",
                retry_sync_recovery=retry_sync_recovery,
            )
        return outcome

    async def _send_frozen(
        self,
        claimed: MatrixDelivery,
        content: dict[str, Any],
        *,
        transaction_id: str,
        what: str,
        operation: str = "send_message",
        retry_sync_recovery: bool,
    ) -> DeliveredMatrixEvent:
        """Send one frozen event of a claimed delivery, mapping refusals to exceptions."""
        outcome = await send_message_outcome(
            self._ready_client(),
            claimed.room_id,
            content,
            operation=operation,
            retry_sync_recovery=retry_sync_recovery,
            transaction_id=transaction_id,
            content_is_prepared=True,
        )
        if isinstance(outcome, MatrixDeliveryFailure):
            detail = _matrix_delivery_failure_reason(outcome)
            if outcome.kind is MatrixDeliveryFailureKind.PAYLOAD_TOO_LARGE:
                raise PermanentDeliveryError(detail)
            msg = f"Matrix refused {what} for turn {claimed.delivery_id!r} stage {claimed.stage.value!r}: {detail}"
            raise _DeliveryRefusedError(msg)
        return outcome

    async def _observe_matrix_event(
        self,
        *,
        room_id: str,
        event_id: str,
    ) -> ProjectedEvent | None:
        """Read one event's authoritative ordering metadata from Matrix."""
        client = self._ready_client()
        response = await client.room_get_event(room_id, event_id)
        if not isinstance(response, nio.RoomGetEventResponse):
            msg = f"Matrix could not read delivered event {event_id!r} in {room_id!r}"
            raise _DeliveryObservationError(msg)
        event = response.event
        if isinstance(event, nio.MegolmEvent):
            msg = f"Matrix could not decrypt delivered event {event_id!r} in {room_id!r}"
            raise _DeliveryObservationError(msg)
        if event.event_id != event_id or event.sender != client.user_id:
            msg = f"Matrix returned the wrong delivered event for {event_id!r}"
            raise _DeliveryObservationError(msg)
        source = event.source
        source_room_id = source.get("room_id") if isinstance(source, dict) else None
        if source_room_id is not None and source_room_id != room_id:
            msg = f"Matrix returned delivered event {event_id!r} from the wrong room"
            raise _DeliveryObservationError(msg)
        timestamp = event.server_timestamp
        if not isinstance(timestamp, int) or isinstance(timestamp, bool):
            msg = f"Matrix returned delivered event {event_id!r} without a server timestamp"
            raise _DeliveryObservationError(msg)
        unsigned = source.get("unsigned") if isinstance(source, dict) else None
        if isinstance(unsigned, dict) and "redacted_because" in unsigned:
            return None
        content = source.get("content") if isinstance(source, dict) else None
        if not isinstance(content, dict):
            msg = f"Matrix returned delivered event {event_id!r} without content"
            raise _DeliveryObservationError(msg)
        return ProjectedEvent(
            event_id=event_id,
            room_id=room_id,
            thread_id=thread_root(content),
            sender=event.sender,
            origin_server_ts=timestamp,
            transaction_id=event.transaction_id if isinstance(event.transaction_id, str) else None,
            content=content,
            replaces_event_id=replacement_target(content),
            redacts_event_id=None,
        )

    async def _observe_delivered(self, claimed: MatrixDelivery, event_id: str) -> tuple[ProjectedEvent, ...]:
        """Return the target and result one delivered outbox row made visible."""
        if claimed.edits_event_id is None and not claimed.has_interactive_prompt:
            return ()
        projections: list[ProjectedEvent] = []
        if claimed.edits_event_id is not None:
            target = await self._observe_matrix_event(
                room_id=claimed.room_id,
                event_id=claimed.edits_event_id,
            )
            if target is None:
                return ()
            projections.append(target)
        delivered = await self._observe_matrix_event(
            room_id=claimed.room_id,
            event_id=event_id,
        )
        if delivered is None:
            return ()
        projections.append(delivered)
        return tuple(projections)

    def _response_delivery(self, send: SendDelivery, *, handoff: TurnHandoff | None) -> MatrixDeliveryWorker:
        """Return the outbox writer, for a live delivery or for recovery.

        Both go through here so they cannot drift. They did: recovery was built
        separately and silently lacked the terminal-record hook, so a recovered
        answer acknowledged its row while the turn record stayed ignorant of
        the event -- the very state the deleted repair pass used to fix.

        The handoff is the one real difference, and recovery passes ``None``:
        it resends rows that already exist, and the sources those rows answer
        were handed over when the rows were first recorded.
        """
        return MatrixDeliveryWorker(
            store=self.deps.outbox,
            send=send,
            observe_delivered=self._observe_delivered,
            event_type="m.room.message",
            sending_device_id=self.deps.sending_device_id(),
            resolve_delivered=self._delivered_under_a_previous_device,
            handoff=handoff,
            terminal_turn_for=self._terminal_turn_write,
            terminal_turn_committed=self._publish_terminal_turn,
            process_shutdown_requested=current_task_is_process_shutdown,
            delivery_locks=self._delivery_turn_locks,
            reply_row_resolved=self.deps.reply_row_resolved,
            run_reply_effects=self._run_reply_effects,
        )

    def _recovery_worker(self) -> MatrixDeliveryWorker:
        """Use the same writer and exact locks for recovery and normal delivery."""

        async def send(claimed: MatrixDelivery) -> str:
            delivered = await self._send_claimed(claimed, retry_sync_recovery=True)
            return delivered.event_id

        return self._response_delivery(send, handoff=None)

    async def _publish_terminal_turn(self, turn_id: str, event_id: str, committed: TerminalTurnWrite | None) -> None:
        """Publish the transaction's exact proof through the ledger's conflict owner."""
        if self.deps.terminal_turn_committed is None:
            return
        record = (
            None
            if committed is None
            else TurnRecordCodec._from_ledger_record(
                committed.index_event_ids[0],
                json.loads(committed.record_json),
            )
        )
        await self.deps.terminal_turn_committed(turn_id, event_id, record)

    def _terminal_turn_write(self, delivery: MatrixDelivery, event_id: str) -> TerminalTurnWrite | None:
        """Turn the terminal record for one delivered answer into a journal row.

        The turn store produces the record and stops at its own boundary; this
        layer already owns the journal's types, so the conversion belongs here
        rather than reaching across. A reply's row commits nothing here: the
        reply's records record its turn answered when they settle its sources.
        """
        if delivery.reply_id is not None or self.deps.terminal_turn_for is None:
            return None
        record = self.deps.terminal_turn_for(delivery.delivery_id, event_id)
        if record is None or record.anchor_event_id is None:
            return None
        return TerminalTurnWrite(
            agent_name=self.deps.agent_name,
            index_event_ids=record.indexed_event_ids,
            anchor_event_id=record.anchor_event_id,
            record_json=json.dumps(TurnRecordCodec._to_ledger_record(record)),
        )

    async def _delivered_under_a_previous_device(self, claimed: MatrixDelivery) -> str | None:
        """Return the exact marker-bearing event an earlier device delivered."""
        client = self._ready_client()
        response_sender = client.user_id
        if not response_sender:
            return None
        event_id = await find_outbox_delivery_event_id_via_room_messages(
            client,
            claimed.room_id,
            delivery_sender=response_sender,
            source_event_ids=(),
            # What the row sent, including the edit envelope a late-bound reply edit gets when claimed.
            delivery_content=_reply_row_wire_content(claimed),
            delivery_event_type=claimed.event_type,
        )
        if event_id is not None:
            await self._deliver_missing_continuations(claimed)
        return event_id

    async def _deliver_missing_continuations(self, claimed: MatrixDelivery) -> None:
        """Send the continuation events an earlier device never got into the room.

        Adopting a segmented delivery keys on the primary event alone, so a
        crash between the primary and its continuations would acknowledge the
        row with most of the answer never sent. Reconciliation counts copies
        rather than checking existence: long homogeneous responses can repeat
        a byte-identical continuation payload, and one observed event must not
        satisfy several positions. Found copies are consumed as credits across
        the expected positions, so exactly the missing ones are sent -- a
        blind resend from this device would duplicate them, because
        transaction IDs are scoped to the device that used them. A failed
        lookup or send raises, leaving the row unacknowledged so the next
        recovery pass resumes exactly here.
        """
        continuations = _continuation_payloads(claimed.result)
        if not continuations:
            return
        client = self._ready_client()
        response_sender = client.user_id
        assert response_sender, "continuation reconciliation requires a logged-in client"
        missing_indices = await missing_outbox_delivery_copy_indices_via_room_messages(
            client,
            claimed.room_id,
            delivery_sender=response_sender,
            delivery_contents=continuations,
            delivery_event_type=claimed.event_type,
        )
        for index in missing_indices:
            await self._send_frozen(
                claimed,
                continuations[index],
                transaction_id=_segment_transaction_id(claimed.transaction_id, index + 1),
                what=f"continuation {index + 1}",
                retry_sync_recovery=True,
            )
        if missing_indices:
            self.deps.logger.info(
                "Recovered segmented Matrix response continuations",
                delivery_id=claimed.delivery_id,
                stage=claimed.stage.value,
                continuation_count=len(missing_indices),
            )

    async def recover_deliveries(self) -> RecoveryOutcome:
        """Resend every delivery whose Matrix outcome this process cannot know.

        A delivery the homeserver already accepted is resent under the same
        transaction ID and collapses back onto the same event, so recovery
        cannot duplicate a visible answer -- which is what makes resending
        unconditionally the safe choice over trying to work out what happened.
        That holds for as long as the device holding the transaction ID's
        namespace is the one retrying, so this pass carries the current device
        and the room lookup that covers the case where it is not.

        A send that fails again leaves its row unacknowledged and is counted in
        the returned outcome, which is how the caller knows to come back.
        Nothing escapes here.
        """
        worker = self._recovery_worker()
        outcome = await worker.recover()
        failed = set(outcome.failed_deliveries)
        # Debt a crash or an earlier failure left: notes owed, events to redact.
        for reply in await self.deps.outbox.replies.with_pending_work():
            await self.settle_reply_debt(reply.reply_id)
        return RecoveryOutcome(recovered=outcome.recovered, failed=len(failed), failed_deliveries=frozenset(failed))

    async def _send_content(  # noqa: PLR0911
        self,
        request: SendTextRequest,
        room_id: str,
        content: dict[str, Any],
    ) -> MatrixSendOutcome | None:
        """Send one built message, through the outbox when it belongs to a turn.

        A send with no turn behind it -- a voice echo, a command confirmation,
        a reconciliation notice -- takes the direct path. It has no identity
        that survives a restart, so there is nothing for recovery to key on and
        a durable row would only be a row nobody can resolve.
        """
        client = self._ready_client()
        if request.reply_write is not None:
            return await self._deliver_reply_write(
                request.reply_write,
                target=request.target,
                content=content,
                new_text=None,
                result=request.delivery_result,
                retry_sync_recovery=request.retry_sync_recovery,
            )
        if request.delivery_turn_id is None:
            return await send_message_outcome(
                client,
                room_id,
                content,
                retry_sync_recovery=request.retry_sync_recovery,
            )
        requested_delivery: DeliveredMatrixEvent | None = None

        async def send(claimed: MatrixDelivery) -> str:
            nonlocal requested_delivery
            delivered = await self._send_claimed(claimed, retry_sync_recovery=request.retry_sync_recovery)
            if claimed.stage is DeliveryStage.FINAL:
                requested_delivery = delivered
            return delivered.event_id

        # Prepared before the row is written, so the frozen payload is the one
        # that goes on the wire. Uploading the sidecar after the claim left the
        # row holding the oversized original while Matrix received an MXC
        # reference, and a recovery resend would upload again -- minting a new
        # MXC, and new encrypted-file keys, under a transaction ID the
        # homeserver had already accepted. An attempted row is already frozen,
        # so preparation is skipped and `enqueue` leaves that stored payload
        # untouched for the claimed send below.
        prepared = await self._prepared_for_the_wire(
            room_id,
            content,
            turn_id=request.delivery_turn_id,
            stage=DeliveryStage.FINAL,
            continuation_thread_id=request.target.resolved_thread_id,
            continuation_reply_to_event_id=request.target.reply_to_event_id,
        )
        if isinstance(prepared, MatrixDeliveryFailure):
            if prepared.kind is not MatrixDeliveryFailureKind.PAYLOAD_TOO_LARGE:
                return prepared
            preparation_failure: MatrixDeliveryFailure | None = prepared
        else:
            preparation_failure = None
            content = prepared.content
        delivery_result = _result_with_segment_payloads(request.delivery_result, prepared)

        try:
            event_id = await self._response_delivery(send, handoff=self.deps.turn_handoff).deliver(
                delivery_id=request.delivery_turn_id,
                stage=DeliveryStage.FINAL,
                room_id=room_id,
                thread_id=request.target.resolved_thread_id,
                payload=content,
                result=delivery_result,
                permanent_failure_reason=(
                    _matrix_delivery_failure_reason(preparation_failure) if preparation_failure is not None else None
                ),
            )
        except _DeliveryRefusedError:
            return None
        if event_id is None:
            # The delivery was withdrawn by membership or ended in an explicit
            # permanent failure, so there is nothing left for recovery to send.
            return preparation_failure
        if requested_delivery is not None and requested_delivery.event_id == event_id:
            return requested_delivery
        # The delivery was already acknowledged, so nothing was sent and the
        # callback never ran. That is a turn re-running after its answer
        # reached the room; reporting it as a failed send would make a
        # delivered answer look lost and invite a duplicate.
        return await self._acknowledged_delivery(request.delivery_turn_id, DeliveryStage.FINAL, event_id, content)

    async def _deliver_reply_write(  # noqa: C901
        self,
        write: ReplyWrite,
        *,
        target: MessageTarget,
        content: dict[str, Any],
        new_text: str | None,
        result: dict[str, object] | None,
        retry_sync_recovery: bool,
    ) -> MatrixSendOutcome | None:
        """Send one durable write of a reply as its next row, after the reply's earlier rows.

        The payload is prepared under the reply's sending lock, once earlier
        rows have resolved, so an edit can name the event the reply's create
        bound. The lifecycle rule decides the row inside its enqueue
        transaction; a rule that refuses raises ``ReplyWriteRefusedError``
        and nothing is written.
        """
        requested: DeliveredMatrixEvent | None = None
        predicted: dict[str, object] = {}

        async def send(claimed: MatrixDelivery) -> str:
            nonlocal requested
            delivered = await self._send_claimed(claimed, retry_sync_recovery=retry_sync_recovery)
            if claimed.delivery_id == predicted.get("delivery_id") and claimed.stage == write.stage:
                requested = delivered
            return delivered.event_id

        async def prepare() -> PreparedReplyRow:
            reply = await self.deps.outbox.replies.load(write.reply_id)
            assert reply is not None or write.create is not None, "a reply exists while its rows are written"
            event_id = None if reply is None else reply.event_id
            stage = write.stage
            delivery_id = write.span.delivery_id
            if stage is DeliveryStage.EDIT:
                assert reply is not None
                delivery_id = edit_delivery_id(delivery_id, reply.reply_sequence + 1)
            predicted["delivery_id"] = delivery_id
            edits = stage is not DeliveryStage.INITIAL and event_id is not None
            payload: dict[str, Any] = (
                build_edit_event_content(event_id=event_id, new_content=content, new_text=new_text or "")
                if edits and event_id is not None
                else content
            )
            prepared = await self._prepared_for_the_wire(
                target.room_id,
                payload,
                turn_id=delivery_id,
                stage=stage,
                continuation_thread_id=target.resolved_thread_id,
                continuation_reply_to_event_id=event_id if edits else target.reply_to_event_id,
            )
            failure_reason = None
            if isinstance(prepared, MatrixDeliveryFailure):
                if prepared.kind is not MatrixDeliveryFailureKind.PAYLOAD_TOO_LARGE:
                    raise _DeliveryRefusedError(_matrix_delivery_failure_reason(prepared))
                failure_reason = _matrix_delivery_failure_reason(prepared)
            else:
                payload = prepared.content
            return PreparedReplyRow(
                payload=payload,
                result=_result_with_segment_payloads(result, prepared),
                permanent_failure_reason=failure_reason,
                # Wrapped when the row is claimed, if the reply's create binds an event by then.
                new_text=(
                    new_text or str(content.get("body", ""))
                    if not edits and stage is not DeliveryStage.INITIAL
                    else None
                ),
            )

        recorded: list[ReplyRowEnqueue] = []

        def enqueued(row: ReplyRowEnqueue) -> None:
            # Before any send can fail, so a retry knows the span moved on.
            recorded.append(row)
            if write.handle is not None:
                write.handle.note(row.applied)

        try:
            delivery = await self._response_delivery(send, handoff=self.deps.turn_handoff).deliver_reply_row(
                ReplyRowRequest(
                    reply_id=write.reply_id,
                    span_id=write.span.span_id,
                    decide=write.decide,
                    room_id=target.room_id,
                    thread_id=target.resolved_thread_id,
                    placeholder_only=write.placeholder_only,
                    create=write.create,
                    stage=write.stage,
                    author_generation=None if write.handle is None else write.handle.runtime.generation,
                ),
                prepare=prepare,
                enqueue=write.enqueue,
                on_enqueued=enqueued,
            )
        except _DeliveryRefusedError:
            # Refused before or while sending: what was recorded still ran its transaction.
            delivery = ReplyRowDelivery(enqueue=recorded[0] if recorded else None)
        if delivery.enqueue is not None:
            await self._run_reply_effects(delivery.enqueue.applied.post_commit)
            if not delivery.enqueue.transition.applied and delivery.enqueue.delivery_id is None:
                # A retried write is not refused: it resolves to the row already recorded.
                raise ReplyWriteRefusedError(delivery.enqueue.transition)
        if delivery.event_id is None:
            return None
        if write.handle is not None and write.handle.reply.event_id is None:
            # The reply's create bound its event when Matrix acknowledged it; the span reads it from here on.
            reply = await self.deps.outbox.replies.load(write.reply_id)
            if reply is not None:
                write.handle.reply = reply
        if requested is not None and requested.event_id == delivery.event_id:
            return requested
        return DeliveredMatrixEvent(event_id=delivery.event_id, content_sent=content)

    async def _run_reply_effects(self, effects: tuple[PostCommitEffect, ...]) -> None:
        """Run the work a committed reply transition left for after its commit."""
        if effects and self.deps.reply_effects is not None:
            await self.deps.reply_effects(effects)

    async def _after_reply_commit(self, applied: AppliedTransition) -> None:
        """Run what a committed reply transition left for after its commit, then deliver what its reply owes."""
        await self._run_reply_effects(applied.post_commit)
        if applied.transition.reply is not None:
            await self.settle_reply_debt(applied.transition.reply.reply_id)

    @staticmethod
    def _live_span() -> SpanHandle | None:
        """Return the span this task writes, while it has not ended."""
        handle = current_span()
        return None if handle is None or handle.exited else handle

    async def end_reply_span(self, handle: SpanHandle, decide: Decide) -> None:
        """Apply a span exit that writes nothing itself, then settle what it left owed."""
        await handle.runtime.decide(handle, decide)
        await self.settle_reply_debt(handle.reply_id)

    async def end_interrupted_span(
        self,
        handle: SpanHandle,
        target: MessageTarget,
        *,
        cancel_source: CancelSource | None,
        failure_reason: str | None,
        delivery_started: bool,
    ) -> FinalDeliveryOutcome | None:
        """End a span whose response was stopped, interrupted, or failed, as ``interrupted_end`` decides.

        Returns the outcome of the terminal row it wrote, or ``None`` when the span ended without one.
        """
        reply = await self.deps.outbox.replies.load(handle.reply_id)
        assert reply is not None, "a span's reply exists while the span ends"
        end = interrupted_end(
            reply,
            cancel_source=cancel_source,
            failure_reason=failure_reason,
            delivery_started=delivery_started,
        )
        if end is not None:
            return await self.end_reply_span_with_note(handle, target, state=end.state, note=end.note)
        await self.end_reply_span(handle, release_decision(handle))
        return None

    async def end_reply_span_with_note(
        self,
        handle: SpanHandle,
        target: MessageTarget,
        *,
        state: ReplyState,
        note: Segment,
    ) -> FinalDeliveryOutcome:
        """End a span with its terminal row: what the reply may show, plus one note."""
        while True:
            reply = await self.deps.outbox.replies.load(handle.reply_id)
            assert reply is not None, "a span's reply exists while the span ends"
            handle.reply = reply
            if reply.unapplied_stop and handle.span.kind is not rl.SpanKind.APPROVAL_RESUME:
                # A Stop committed before this span ended; its note is the cancellation.
                state, note = ReplyState.CANCELLED, note_segment(NoteKind.CANCELLED)
            shown = _with_note(_shown_before(reply, handle), note)
            write = terminal_write(handle, shown, state=state)
            # A resumed reply's note keeps its sources pending, yet shows as a failure.
            rendered = render(shown, state=ReplyState.FAILED if state is ReplyState.ACTIVE else state)
            try:
                delivered = await self._deliver_rendered_reply_write(write, target, rendered, event_id=reply.event_id)
            except ReplyWriteRefusedError as refused:
                if refused.transition.outcome is ReplyOutcome.RECOMPUTE:
                    # The reply changed after this note was rendered; render it again.
                    continue
                delivered = None
            break
        # A transition that wrote no row, such as a restore, may still leave a button or a note owed.
        await self.settle_reply_debt(handle.reply_id)
        return FinalDeliveryOutcome(
            terminal_status=_terminal_status_for_reply_state(state),
            event_id=reply.event_id or delivered,
            is_visible_response=(reply.event_id or delivered) is not None,
            final_visible_body=rendered.body if delivered is not None else None,
            delivery_kind=None if delivered is None else ("edited" if reply.event_id else "sent"),
            failure_reason=None if state is ReplyState.COMPLETED else note.note.value if note.note else None,
            tool_trace=rendered.tool_trace,
        )

    async def _deliver_rendered_reply_write(
        self,
        write: ReplyWrite,
        target: MessageTarget,
        rendered: RenderedReply,
        *,
        event_id: str | None,
    ) -> str | None:
        """Send one rendered reply row as an edit of the reply's event, or as its create.

        Raises ``ReplyWriteRefusedError`` when the reply's rule refuses the row.
        """
        return await self._send_reply_write(
            write,
            target,
            text=rendered.body,
            tool_trace=list(rendered.tool_trace) or None,
            extra_content={constants.STREAM_STATUS_KEY: rendered.stream_status},
            event_id=event_id,
        )

    async def _send_reply_write(
        self,
        write: ReplyWrite,
        target: MessageTarget,
        *,
        text: str,
        tool_trace: list[ToolTraceEntry] | None,
        extra_content: dict[str, Any],
        event_id: str | None,
        skip_mentions: bool = False,
        delivery_result: dict[str, object] | None = None,
    ) -> str | None:
        """Send one reply row as an edit of ``event_id``, or as the reply's create; return the event it shows on.

        Raises ``ReplyWriteRefusedError`` when the reply's rule refuses the row.
        """
        if event_id is None:
            return await self.send_text(
                SendTextRequest(
                    target=target,
                    response_text=text,
                    skip_mentions=skip_mentions,
                    tool_trace=tool_trace,
                    extra_content=extra_content,
                    retry_sync_recovery=True,
                    delivery_result=delivery_result,
                    reply_write=write,
                ),
            )
        edited = await self.edit_text(
            EditTextRequest(
                target=target,
                event_id=event_id,
                new_text=text,
                tool_trace=tool_trace,
                extra_content=extra_content,
                retry_sync_recovery=True,
                delivery_result=delivery_result,
                reply_write=write,
            ),
        )
        return event_id if edited else None

    async def supersede_replay(self, source_event_ids: tuple[str, ...]) -> bool | None:
        """Settle a superseded replay's sources with the reply they left.

        Returns ``None`` when no reply has those sources or it already ended
        without them, and ``False`` when the reply still owes Matrix a write,
        which only its replay resolves.
        """
        applied = await self.deps.outbox.replies.supersede_replay(source_event_ids, now_ns=time.time_ns())
        if applied is None or applied.transition.outcome is ReplyOutcome.DUPLICATE:
            return None
        if applied.transition.outcome is ReplyOutcome.DEFERRED:
            return False
        await self._after_reply_commit(applied)
        return True

    async def settle_unclaimed_reply(self, source_event_ids: tuple[str, ...], *, source_deleted: bool) -> bool:
        """End the reply an earlier attempt left for sources that became terminal before a claim.

        Returns whether a reply exists for them, whose records then own their settlement.
        """
        reply = await self.deps.outbox.replies.for_sources(source_event_ids)
        if reply is None:
            return False
        if reply.terminal or reply.current_span_id is not None:
            # A terminal reply keeps its answer; a span an older instance left
            # current is ended when this instance starts.
            return True
        applied = await self.deps.outbox.replies.decide(
            reply_id=reply.reply_id,
            span_id=reply.last_span_id,
            decide=terminal_source_decision(source_deleted=source_deleted),
        )
        await self._after_reply_commit(applied)
        return True

    async def pause_shown_reply(
        self,
        handle: SpanHandle,
        decide: Decide,
        *,
        enqueue: ReplyRowEnqueuer,
        target: MessageTarget,
    ) -> bool:
        """Pause a reply whose create already showed the pause, with the continuation; return whether it paused."""
        enqueued = await enqueue(
            ReplyRowRequest(
                reply_id=handle.reply_id,
                span_id=handle.span_id,
                decide=decide,
                room_id=target.room_id,
                thread_id=target.resolved_thread_id,
                author_generation=handle.runtime.generation,
            ),
            PreparedReplyRow(payload={}),
        )
        if enqueued is None:
            return False
        handle.note(enqueued.applied)
        await self._run_reply_effects(enqueued.applied.post_commit)
        if not enqueued.transition.applied:
            raise ReplyWriteRefusedError(enqueued.transition)
        # The pause wrote nothing to Matrix, so the button the reply no longer shows goes now.
        await self.settle_reply_debt(handle.reply_id)
        return True

    async def write_approval_failure_note(
        self,
        event_id: str,
        *,
        approval_id: str,
        note: NoteKind,
        text: str,
        target: MessageTarget,
    ) -> bool:
        """Show a failed approval's note on the reply it paused; ``text`` is an error note's.

        A Stop or failure note replaces the reply's body; an interruption note
        goes below what the reply showed. Returns whether the note was
        delivered; the continuation's finish then ends the reply.
        """
        while True:
            reply = await self.deps.outbox.replies.for_event(event_id)
            assert reply is not None, "a continuation's paused reply exists while the continuation does"
            if reply.current_span_id is not None and await self._end_span_left_behind(reply, reply.current_span_id):
                continue
            span = await self.deps.outbox.replies.span(reply.last_span_id)
            assert span is not None, "a reply's last span exists"
            final = await self.deps.outbox.load_matrix_delivery(delivery_id=span.delivery_id, stage=DeliveryStage.FINAL)
            if final is not None:
                # An earlier settlement recorded the note; a retry resends it rather than writing another.
                if final.acknowledged_event_id is None and not final.permanently_failed:
                    with suppress(_DeliveryRefusedError):
                        await self._recovery_worker().send_reply_rows(reply.reply_id)
                    final = await self.deps.outbox.load_matrix_delivery(
                        delivery_id=span.delivery_id,
                        stage=DeliveryStage.FINAL,
                    )
                return final is not None and (final.acknowledged_event_id is not None or final.permanently_failed)
            # A Stop recorded meanwhile decides the note, as the finish decides the state.
            shown_note = NoteKind.CANCELLED if reply.unapplied_stop else note
            shown_before = _shown_before(reply, None)
            if shown_note in {NoteKind.CANCELLED, NoteKind.ERROR}:
                shown_before = replace(shown_before, segments=())
            shown = with_trailing_note(
                shown_before,
                note_segment(shown_note, text if shown_note is NoteKind.ERROR else None),
            )
            state = ReplyState.CANCELLED if shown_note is NoteKind.CANCELLED else ReplyState.FAILED
            write = approval_note_write(
                reply,
                span,
                shown,
                approval_id=approval_id,
                state=state,
            )
            rendered = render(shown, state=state)
            try:
                return (
                    await self._deliver_rendered_reply_write(write, target, rendered, event_id=reply.event_id)
                    is not None
                )
            except ReplyWriteRefusedError as refused:
                if refused.transition.outcome is ReplyOutcome.RECOMPUTE:
                    # The reply changed after this note was rendered; render it again.
                    continue
                # The reply ended otherwise; the continuation's finish reads its rows.
                return True

    async def _end_span_left_behind(self, reply: rl.Reply, span_id: str) -> bool:
        """End a resume an older bot instance left running on the reply; return whether it ended one."""
        generation = await self.deps.outbox.replies.active_generation()
        applied = await self.deps.outbox.replies.decide(
            reply_id=reply.reply_id,
            span_id=span_id,
            decide=lambda current, span: rl.span_left_behind(
                current,
                span,
                active_generation=generation,
                now_ns=time.time_ns(),
            ),
        )
        return applied.transition.applied

    async def fail_reply_dispatch(self, event_id: str, error_text: str) -> bool:
        """Show a dispatch failure on the reply bound to one event before a span ran it.

        Returns whether a reply owns the event; its records then settle the
        sources and deliver the error.
        """
        reply = await self.deps.outbox.replies.for_event(event_id)
        if reply is None:
            return False
        now_ns = time.time_ns()
        applied = await self.deps.outbox.replies.decide(
            reply_id=reply.reply_id,
            span_id=reply.last_span_id,
            decide=lambda current, span: rl.dispatch_failed(current, span, error_text=error_text, now_ns=now_ns),
        )
        await self._after_reply_commit(applied)
        return True

    async def add_reply_stop_button(self, handle: SpanHandle, event_id: str) -> None:
        """Show the Stop button on a span's reply, best effort, and record it on the reply."""
        if handle.reply.stop_button_event_id is not None:
            # An earlier span of the reply already shows one.
            return
        button_event_id = await send_stop_button(self._ready_client(), handle.reply.room_id, event_id)
        if button_event_id is None:
            return
        applied = await self.deps.outbox.replies.record_stop_button(
            handle.reply_id,
            button_event_id,
            now_ns=time.time_ns(),
        )
        if applied is None:
            return
        handle.note(applied)
        reply = applied.transition.reply
        if reply is not None and reply.redaction_pending:
            # The reply left active while the button was sent.
            await self.settle_reply_debt(handle.reply_id)

    async def stop_reply(self, event_id: str, receipt_order: int, *, room_id: str, may_wait: bool) -> bool:
        """Record a Stop on the reply it reaches and run what it left, cancelling the span or owing the note.

        A Stop on an event no reply is bound to yet, while a reply create in
        its room is unresolved, waits for that create, whose acknowledgement
        applies it. Returns whether a reply took the Stop or it waits for one.
        """
        recorded = await self.deps.outbox.replies.record_stop(
            event_id,
            room_id=room_id,
            receipt_order=receipt_order,
            may_wait=may_wait,
            now_ns=time.time_ns(),
        )
        if isinstance(recorded, bool):
            return recorded
        await self._after_reply_commit(recorded)
        return True

    async def accepts_reply_stop(self, event_id: str, room_id: str) -> bool:
        """Return whether a Stop reaction on this event reaches a running reply or a pending create."""
        return await self.deps.outbox.replies.accepts_stop(event_id, room_id)

    async def settle_reply_debt(self, reply_id: str) -> None:
        """Redact what a reply owes removed and deliver the note it owes, if any."""
        reply = await self.deps.outbox.replies.load(reply_id)
        if reply is None:
            return
        if reply.redaction_pending:
            done = [
                event_id for event_id in reply.redaction_pending if await self._redact_owed(reply.room_id, event_id)
            ]
            if done:
                await self.deps.outbox.replies.update(
                    reply_id,
                    lambda current: rl.redactions_done(current, tuple(done), now_ns=time.time_ns()),
                )
        if reply.owed_write is not None:
            await self._flush_owed_write(reply_id)

    async def _redact_owed(self, room_id: str, event_id: str) -> bool:
        """Redact one event a reply owes removed; a failed attempt stays owed for the next settlement."""
        try:
            return await self.deps.redact_message_event(room_id=room_id, event_id=event_id, reason="Reply removed")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.deps.logger.warning("Owed reply redaction failed", event_id=event_id, error=str(error))
            return False

    async def _flush_owed_write(self, reply_id: str) -> None:
        """Render and send the note a reply-authored transition owed."""
        while (reply := await self.deps.outbox.replies.load(reply_id)) is not None and reply.owed_write is not None:
            owed = reply.owed_write
            span = await self.deps.outbox.replies.span(owed.span_id)
            assert span is not None, "an owed write names a span of its reply"
            shown = _with_note(_shown_before(reply, None), note_segment(owed.note, owed.text))
            final = await self.deps.outbox.load_matrix_delivery(delivery_id=span.delivery_id, stage=DeliveryStage.FINAL)
            write = owed_note_write(reply, span, shown, span_has_final=final is not None)
            rendered = render(shown, state=reply.state)
            target = MessageTarget.resolve(room_id=reply.room_id, thread_id=reply.thread_id, reply_to_event_id=None)
            try:
                await self._deliver_rendered_reply_write(write, target, rendered, event_id=reply.event_id)
            except ReplyWriteRefusedError as refused:
                if refused.transition.outcome is ReplyOutcome.RECOMPUTE:
                    # The reply changed after this note was rendered; render what it owes now.
                    continue
                return
            current = await self.deps.outbox.replies.load(reply_id)
            # A note the outbox refuses because the room's membership ended is never sent; one that stays owed
            # for another reason, such as a payload that could not be prepared yet, the next recovery pass retries.
            if (
                current is not None
                and current.owed_write == owed
                and not await self.deps.outbox.turn_membership_is_current(
                    turn_id=span.delivery_id,
                    room_id=current.room_id,
                )
            ):
                self.deps.logger.warning("reply_owed_write_refused", reply_id=reply_id, note=owed.note)
                await self.deps.outbox.replies.update(
                    reply_id,
                    lambda latest, owed=owed: rl.owed_write_refused(latest, owed, now_ns=time.time_ns()),
                )
            return

    async def send_text(self, request: SendTextRequest) -> str | None:
        """Send one response message to a room."""
        config = self.deps.runtime.config
        resolved_target = request.target
        if request.reply_write is not None:
            response_text, tool_trace = _reply_body(
                request.response_text,
                request.tool_trace,
                request.reply_write.shown,
            )
            request = replace(request, response_text=response_text, tool_trace=tool_trace)
        effective_thread_id = resolved_target.resolved_thread_id

        if effective_thread_id is None:
            content = format_message_with_mentions(
                config,
                self.deps.runtime_paths,
                request.response_text,
                thread_event_id=None,
                reply_to_event_id=resolved_target.reply_to_event_id,
                latest_thread_event_id=None,
                tool_trace=request.tool_trace,
                extra_content=request.extra_content,
            )
        else:
            latest_thread_event_id = await self.deps.resolver.deps.conversation_reader.latest_thread_event_id(
                room_id=resolved_target.room_id,
                thread_id=effective_thread_id,
                reply_to_event_id=resolved_target.reply_to_event_id,
            )
            content = format_message_with_mentions(
                config,
                self.deps.runtime_paths,
                request.response_text,
                thread_event_id=effective_thread_id,
                reply_to_event_id=resolved_target.reply_to_event_id,
                latest_thread_event_id=latest_thread_event_id,
                tool_trace=request.tool_trace,
                extra_content=request.extra_content,
            )
        if request.skip_mentions:
            content[SKIP_MENTIONS_KEY] = True
        failure_reason = "durable Matrix delivery was refused"
        try:
            outcome = await self._send_content(request, resolved_target.room_id, content)
        except SendRetryError:
            delivered = None
            failure_reason = "matrix timeline recovery still blocked the send"
        else:
            delivered = outcome if isinstance(outcome, DeliveredMatrixEvent) else None
            if isinstance(outcome, MatrixDeliveryFailure):
                failure_reason = _matrix_delivery_failure_reason(outcome)
        if delivered is not None:
            self.deps.logger.info("Sent response", event_id=delivered.event_id, **resolved_target.log_context)
            return delivered.event_id
        self.deps.logger.error(
            "Failed to send response to room",
            error=failure_reason,
            **resolved_target.log_context,
        )
        return None

    async def _edit_content(
        self,
        request: EditTextRequest,
        room_id: str,
        content: dict[str, Any],
    ) -> MatrixSendOutcome | None:
        """Apply one edit, through the outbox when it is a durable write of a reply.

        Other edits -- a voice echo, a reconciliation update -- take the direct
        path: they are transport with no identity a restart can resolve.
        """
        client = self._ready_client()
        if request.reply_write is not None:
            return await self._deliver_reply_write(
                request.reply_write,
                target=request.target,
                content=content,
                new_text=request.new_text,
                result=request.delivery_result,
                retry_sync_recovery=request.retry_sync_recovery,
            )
        return await edit_message_outcome(
            client,
            room_id,
            request.event_id,
            content,
            request.new_text,
            retry_sync_recovery=request.retry_sync_recovery,
        )

    async def edit_text(self, request: EditTextRequest) -> bool:
        """Edit one existing response message."""
        config = self.deps.runtime.config
        target = request.target
        if request.reply_write is not None:
            new_text, tool_trace = _reply_body(request.new_text, request.tool_trace, request.reply_write.shown)
            request = replace(request, new_text=new_text, tool_trace=tool_trace)
        # The edit envelope discards any pre-existing relation before adding m.replace.
        content = format_message_with_mentions(
            config,
            self.deps.runtime_paths,
            request.new_text,
            tool_trace=request.tool_trace,
            extra_content=request.extra_content,
        )

        failure_reason = "durable Matrix edit was refused"
        try:
            outcome = await self._edit_content(request, target.room_id, content)
        except SendRetryError:
            delivered = None
            failure_reason = "matrix timeline recovery still blocked the edit"
        else:
            delivered = outcome if isinstance(outcome, DeliveredMatrixEvent) else None
            if isinstance(outcome, MatrixDeliveryFailure):
                failure_reason = _matrix_delivery_failure_reason(outcome)
        if delivered is not None:
            self.deps.logger.info("Edited message", event_id=request.event_id, **target.log_context)
            return True
        self.deps.logger.error(
            "Failed to edit message",
            event_id=request.event_id,
            error=failure_reason,
            **target.log_context,
        )
        return False

    async def deliver_final(
        self,
        request: FinalDeliveryRequest,
    ) -> FinalDeliveryOutcome:
        """Run final delivery under the fixed shutdown diagnostic boundary."""
        with response_shutdown_phase(ResponseShutdownPhase.FINAL_DELIVERY):
            return await self._deliver_final(request)

    async def _deliver_final(  # noqa: C901, PLR0911, PLR0912
        self,
        request: FinalDeliveryRequest,
    ) -> FinalDeliveryOutcome:
        """Apply before_response hooks and write the reply span's answer as its terminal row."""
        handle = self._live_span()
        if handle is None:
            # Only a running reply span writes an answer; one that ended meanwhile writes nothing.
            return _refused_reply_outcome(None, request, request.tool_trace, request.extra_content)
        try:
            draft = await self.deps.response_hooks._apply_before_response(
                identity=request.identity,
                response_text=request.response_text,
                tool_trace=request.tool_trace,
                extra_content=request.extra_content,
            )
        except asyncio.CancelledError:
            # The span's exit writes the cancellation through the reply's records.
            raise
        except Exception as error:
            return await self._reply_hook_failed(request, handle, failure_reason=str(error) or "hook_failed")
        suppression_reason = "suppressed_by_hook" if draft.suppress else None
        if suppression_reason is None and draft.envelope.source_kind == SILENT_SCHEDULE_SOURCE_KIND:
            no_report_text = draft.response_text
            if draft.tool_trace:
                no_report_text = strip_matching_visible_tool_markers(no_report_text, draft.tool_trace)
            if constants.is_silent_schedule_no_report_response(no_report_text):
                suppression_reason = "silent_no_report"
        await record_silent_schedule_result_if_needed(
            entity_name=self.deps.agent_name,
            agent_names=request.identity.participating_agent_names or (self.deps.agent_name,),
            envelope=request.identity.response_envelope,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            suppression_reason=suppression_reason,
            response_text=draft.response_text,
        )
        if suppression_reason is not None:
            self.deps.logger.info(
                "Response suppressed",
                response_kind=request.identity.response_kind,
                source_event_id=request.identity.response_envelope.source_event_id,
                correlation_id=request.identity.correlation_id,
                suppression_reason=suppression_reason,
            )
            return await self._reply_suppressed(handle, draft, suppression_reason=suppression_reason)

        interactive_response = interactive.parse_and_format_interactive(draft.response_text, extract_mapping=True)
        display_text = interactive_response.formatted_text
        delivery_extra_content = dict(draft.extra_content or {}) | self._acting_requester_content(request.identity)
        if interactive_response.interactive_metadata is not None:
            delivery_extra_content.update(
                interactive.build_prompt_content(
                    interactive_response.interactive_metadata,
                    creator_agent=self.deps.agent_name,
                    source_event_id=request.identity.response_envelope.source_event_id,
                ),
            )
        reply_write = terminal_write(
            handle,
            handle.presentation(display_text, tuple(draft.tool_trace or ())),
            state=ReplyState.COMPLETED,
        )
        # What the reply shows is what its outcome reports and freezes, earlier spans' work included.
        shown_text, _shown_trace = _reply_body(display_text, draft.tool_trace, reply_write.shown)
        delivery_result: dict[str, object] | None = None
        metadata = interactive_response.interactive_metadata
        if handle.span.kind is rl.SpanKind.APPROVAL_RESUME:
            # Recovery after a restart registers an approved run's question from its frozen answer.
            delivery_result = None if metadata is None else {"interactive": metadata.to_metadata()}

        edited_event_id = request.existing_event_id
        if edited_event_id is not None:
            # The answer replaces an earlier visible message, so mark it finished as a streamed final does.
            delivery_extra_content[constants.STREAM_STATUS_KEY] = constants.STREAM_STATUS_COMPLETED
        try:
            event_id = await self._send_reply_write(
                reply_write,
                request.target,
                text=display_text,
                tool_trace=draft.tool_trace,
                extra_content=delivery_extra_content,
                event_id=edited_event_id,
                skip_mentions=request.skip_mentions,
                delivery_result=delivery_result,
            )
        except ReplyWriteRefusedError as refused:
            return _refused_reply_outcome(refused, request, draft.tool_trace, draft.extra_content)
        if event_id is None:
            if handle.exited:
                return _owed_answer_outcome()
            return FinalDeliveryOutcome(
                terminal_status="error",
                # A failed edit leaves the earlier message visible.
                event_id=edited_event_id,
                is_visible_response=edited_event_id is not None,
                failure_reason="delivery_failed",
                tool_trace=tuple(draft.tool_trace or ()),
                extra_content=delivery_extra_content,
            )
        return FinalDeliveryOutcome(
            terminal_status="completed",
            event_id=event_id,
            is_visible_response=True,
            final_visible_body=display_text if edited_event_id is None else shown_text,
            delivery_kind="sent" if edited_event_id is None else "edited",
            tool_trace=tuple(draft.tool_trace or ()),
            extra_content=delivery_extra_content,
            interactive_metadata=interactive_response.interactive_metadata,
        )

    async def _reply_hook_failed(
        self,
        request: FinalDeliveryRequest,
        handle: SpanHandle,
        *,
        failure_reason: str,
    ) -> FinalDeliveryOutcome:
        """A before-response hook raised for a reply span."""
        reply = await self.deps.outbox.replies.load(handle.reply_id)
        silent = request.identity.response_envelope.source_kind == SILENT_SCHEDULE_SOURCE_KIND
        if silent and reply is not None and (reply.event_id is None or reply.placeholder_only):
            # A silent schedule reports its failure durably.
            note = note_segment(NoteKind.ERROR, _BEFORE_RESPONSE_HOOK_FAILURE_TEXT)
            outcome = await self.end_reply_span_with_note(
                handle,
                request.target,
                state=ReplyState.FAILED,
                note=note,
            )
            return replace(outcome, failure_reason=failure_reason)
        await self.end_reply_span(handle, suppress_decision(handle, reason="hook_failed"))
        event_id = None if reply is None or reply.placeholder_only else reply.event_id
        return FinalDeliveryOutcome(
            terminal_status="error",
            event_id=event_id,
            is_visible_response=event_id is not None,
            failure_reason=failure_reason,
            tool_trace=tuple(request.tool_trace or ()),
            extra_content=request.extra_content,
        )

    async def _reply_suppressed(
        self,
        handle: SpanHandle,
        draft: ResponseDraft,
        *,
        suppression_reason: str,
    ) -> FinalDeliveryOutcome:
        """A hook suppressed a reply span's answer."""
        reply = await self.deps.outbox.replies.load(handle.reply_id)
        await self.end_reply_span(handle, suppress_decision(handle))
        event_id = None if reply is None or reply.placeholder_only else reply.event_id
        return FinalDeliveryOutcome(
            terminal_status="cancelled",
            event_id=event_id,
            is_visible_response=event_id is not None,
            failure_reason=suppression_reason,
            suppressed=True,
            tool_trace=tuple(draft.tool_trace or ()),
            extra_content=draft.extra_content,
        )

    async def _send_compaction_lifecycle_start(
        self,
        *,
        target: MessageTarget,
        reply_to_event_id: str | None,
        event: CompactionLifecycleStart,
    ) -> str | None:
        """Send the foreground compaction lifecycle notice."""
        body = "Compacting history..."
        notice_metadata: dict[str, object] = {
            "version": 3,
            "status": "running",
            "mode": event.mode,
            "session_id": event.session_id,
            "scope": event.scope,
            "summary_model": event.summary_model,
            "before_tokens": event.before_tokens,
            "history_budget_tokens": event.history_budget_tokens,
            "runs_before": event.runs_before,
        }
        if event.threshold_tokens is not None:
            notice_metadata["threshold_tokens"] = event.threshold_tokens
        content = build_message_content(
            body,
            formatted_body=f"<em>{html_escape(body)}</em>",
            thread_event_id=target.resolved_thread_id,
            reply_to_event_id=reply_to_event_id,
            extra_content={
                "msgtype": "m.notice",
                constants.COMPACTION_NOTICE_CONTENT_KEY: notice_metadata,
                SKIP_MENTIONS_KEY: True,
            },
        )
        outcome = await send_message_outcome(self._ready_client(), target.room_id, content)
        delivered = outcome if isinstance(outcome, DeliveredMatrixEvent) else None
        if delivered is not None:
            self.deps.logger.info("Sent compaction lifecycle notice", event_id=delivered.event_id, **target.log_context)
            return delivered.event_id
        self.deps.logger.error("Failed to send compaction lifecycle notice", **target.log_context)
        return None

    async def _edit_compaction_lifecycle_progress(
        self,
        *,
        target: MessageTarget,
        event: CompactionLifecycleProgress,
    ) -> None:
        """Edit the foreground compaction lifecycle notice after progress."""
        if event.notice_event_id is None:
            return
        await self._edit_compaction_lifecycle_notice(
            target=target,
            event_id=event.notice_event_id,
            body=event.format_notice(),
            metadata=event.to_notice_metadata(),
        )

    async def _edit_compaction_lifecycle_success(
        self,
        *,
        target: MessageTarget,
        outcome: CompactionOutcome,
    ) -> None:
        """Edit the foreground compaction lifecycle notice after success."""
        if outcome.lifecycle_notice_event_id is None:
            return
        await self._edit_compaction_lifecycle_notice(
            target=target,
            event_id=outcome.lifecycle_notice_event_id,
            body=outcome.format_notice(),
            metadata=outcome.to_notice_metadata(),
        )

    async def _edit_compaction_lifecycle_failure(
        self,
        *,
        target: MessageTarget,
        event: CompactionLifecycleFailure,
    ) -> None:
        """Edit the foreground compaction lifecycle notice after failure."""
        if event.notice_event_id is None:
            return
        body = f"Compaction failed; continuing with trimmed history. {event.failure_reason}"
        await self._edit_compaction_lifecycle_notice(
            target=target,
            event_id=event.notice_event_id,
            body=body,
            metadata={
                "version": 3,
                "status": event.status,
                "mode": event.mode,
                "session_id": event.session_id,
                "scope": event.scope,
                "summary_model": event.summary_model,
                "duration_ms": event.duration_ms,
                "failure_reason": event.failure_reason,
                "history_budget_tokens": event.history_budget_tokens,
            },
        )

    async def _edit_compaction_lifecycle_notice(
        self,
        *,
        target: MessageTarget,
        event_id: str,
        body: str,
        metadata: dict[str, object],
    ) -> None:
        # Same as ``edit_text``: this content is wrapped by ``build_edit_event_content``,
        # which discards ``m.relates_to``, so neither the thread relation nor the
        # latest-thread lookup that completes it survives to the wire. Passing
        # ``thread_event_id`` without a resolved fallback would also trip the thread-relation
        # assertion in ``build_thread_relation``.
        content = build_message_content(
            body,
            formatted_body=f"<em>{html_escape(body).replace(chr(10), '<br/>')}</em>",
            extra_content={
                "msgtype": "m.notice",
                constants.COMPACTION_NOTICE_CONTENT_KEY: metadata,
                SKIP_MENTIONS_KEY: True,
            },
        )
        outcome = await edit_message_outcome(
            self._ready_client(),
            target.room_id,
            event_id,
            content,
            body,
        )
        delivered = outcome if isinstance(outcome, DeliveredMatrixEvent) else None
        if delivered is not None:
            self.deps.logger.info("Edited compaction lifecycle notice", event_id=event_id, **target.log_context)
            return
        self.deps.logger.error("Failed to edit compaction lifecycle notice", event_id=event_id, **target.log_context)

    async def deliver_stream(
        self,
        request: StreamingDeliveryRequest,
    ) -> StreamTransportOutcome:
        """Run streaming transport under the fixed shutdown diagnostic boundary."""
        with response_shutdown_phase(ResponseShutdownPhase.STREAMING_RESPONSE):
            return await self._deliver_stream(request)

    def _acting_requester_content(self, identity: ResponseIdentity) -> dict[str, str]:
        """Name a human or bot-account requester on the reply, so entities it mentions act for that requester."""
        requester_id = identity.response_envelope.requester_id
        if not is_access_checked_requester_id(requester_id, self.deps.runtime.config, self.deps.runtime_paths):
            return {}
        return {ACTING_REQUESTER_KEY: requester_id}

    async def _deliver_stream(
        self,
        request: StreamingDeliveryRequest,
    ) -> StreamTransportOutcome:
        """Send one streaming Matrix response."""
        client = self._ready_client()
        config = self.deps.runtime.config
        # The turn this stream answers, whose membership gates its transport.
        delivery_turn_id = request.identity.response_envelope.source_event_id
        latest_thread_event_id = await self.deps.resolver.deps.conversation_reader.latest_thread_event_id(
            room_id=request.target.room_id,
            thread_id=request.target.resolved_thread_id,
            reply_to_event_id=request.target.reply_to_event_id,
            existing_event_id=request.existing_event_id,
        )
        handle = current_span()
        assert handle is not None, "every reply stream runs in its reply span"
        reply_hooks = self._reply_stream_hooks(handle, request.target)
        return await send_streaming_response(
            client,
            request.target,
            config,
            self.deps.runtime_paths,
            request.response_stream,
            streaming_cls=request.streaming_cls,
            show_tool_calls=request.show_tool_calls,
            existing_event_id=request.existing_event_id,
            adopt_existing_placeholder=request.adopt_existing_placeholder,
            # A live view, because the caller keeps adding run metadata to its dict while the stream runs.
            extra_content=ChainMap(
                self._acting_requester_content(request.identity),
                request.extra_content if request.extra_content is not None else {},
            ),
            tool_trace_collector=request.tool_trace_collector,
            pipeline_timing=request.pipeline_timing,
            visible_event_id_callback=request.visible_event_id_callback,
            visible_progress_callback=request.visible_progress_callback,
            latest_thread_event_id=latest_thread_event_id,
            preserve_existing_visible_on_empty_terminal=(
                request.preserve_existing_visible_on_empty_terminal
                or (request.existing_event_id is not None and not request.adopt_existing_placeholder)
            ),
            final_text_transform=self._final_text_transform(request.identity),
            transport_is_current=self._stream_transport_gate(delivery_turn_id, request.target.room_id),
            interactive_creator_agent=self.deps.agent_name,
            interactive_source_event_id=delivery_turn_id,
            allow_new_terminal_message=request.allow_new_terminal_message,
            # What a stopped attempt at the reply showed; the stream continues below it.
            resumed=handle.resumed,
            **reply_hooks,
        )

    def _reply_stream_hooks(  # noqa: C901
        self,
        handle: SpanHandle,
        target: MessageTarget,
        *,
        published: dict[str, Presentation] | None = None,
    ) -> dict[str, Any]:
        """Return the streamer callbacks that make a span's stream a durable reply.

        The first visible create and every terminal update become the reply's
        rows; each direct progress edit is recorded before it is sent and
        confirmed by the next write. ``published`` maps the whole-reply bodies a
        progress stream sent to the presentations they show.
        """

        def shown(progress: ProgressState) -> Presentation:
            whole = None if published is None else _take_published(published, progress.text)
            if whole is not None:
                return whole
            return handle.presentation(
                progress.text,
                progress.tool_trace,
                team_state=progress.presentation_state,
            )

        async def deliver(
            build: Callable[[], ReplyWrite],
            *,
            content: dict[str, Any],
            new_text: str | None,
            retry_sync_recovery: bool,
        ) -> DeliveredMatrixEvent | None:
            """Deliver one stream row, rendering it again once when the Stop path's own row met its Stop."""
            for rendered_again in (False, True):
                self._ready_client()
                try:
                    outcome = await self._deliver_reply_write(
                        build(),
                        target=target,
                        content=content,
                        # A create's text is its content's body; an edit names its new text.
                        new_text=new_text,
                        result=None,
                        retry_sync_recovery=retry_sync_recovery,
                    )
                except ReplyWriteRefusedError as refused:
                    if rendered_again or refused.transition.outcome is not ReplyOutcome.RECOMPUTE:
                        return None
                    if content.get(constants.STREAM_STATUS_KEY) != constants.STREAM_STATUS_CANCELLED:
                        # A Stop committed after this update was rendered; the
                        # span's Stop path writes the reply from here.
                        raise asyncio.CancelledError(USER_STOP_CANCEL_MSG) from refused
                    # This is the Stop path's own cancelled row, rendered before the
                    # handle saw the Stop's revision; the refusal refreshed the handle.
                    continue
                return outcome if isinstance(outcome, DeliveredMatrixEvent) else None
            return None

        async def initial_send(
            client: nio.AsyncClient,
            room_id: str,
            content: dict[str, Any],
            display_text: str,
            *,
            retry_sync_recovery: bool = False,
            progress: ProgressState,
        ) -> DeliveredMatrixEvent | None:
            # A create sends its content as rendered; only an edit names its new text.
            del client, room_id, display_text
            return await deliver(
                lambda: initial_write(handle, shown(progress), placeholder_only=progress.placeholder_only),
                content=content,
                new_text=None,
                retry_sync_recovery=retry_sync_recovery,
            )

        def answered_nothing(state_content: dict[str, Any], progress: ProgressState) -> bool:
            # A stream that completed showing only its placeholder answered
            # nothing; the span's exit removes the placeholder.
            status = state_content.get(constants.STREAM_STATUS_KEY)
            return status == constants.STREAM_STATUS_COMPLETED and progress.placeholder_only

        def terminal(state_content: dict[str, Any], progress: ProgressState) -> ReplyWrite:
            status = state_content.get(constants.STREAM_STATUS_KEY)
            state = _reply_state_for_stream_status(status)
            if progress.untransformed_text is None:
                return terminal_write(handle, shown(progress), state=state)
            # The final transform reshaped the whole reply: that is what it
            # shows from now on, and the span's own answer stays canonical.
            whole = Presentation(
                segments=(
                    Segment(kind="answer", text=progress.text, span_id=handle.span_id, tool_trace=progress.tool_trace),
                ),
                placeholder=handle.base.placeholder,
                show_tool_calls=handle.base.show_tool_calls,
            )
            canonical = shown(replace(progress, text=progress.untransformed_text))
            return terminal_write(handle, canonical, state=state, frozen_display=whole)

        async def terminal_send(
            client: nio.AsyncClient,
            room_id: str,
            content: dict[str, Any],
            display_text: str,
            *,
            retry_sync_recovery: bool = False,
            progress: ProgressState,
        ) -> DeliveredMatrixEvent | None:
            # A create sends its content as rendered; only an edit names its new text.
            del client, room_id, display_text
            if answered_nothing(content, progress):
                return None
            return await deliver(
                lambda: terminal(content, progress),
                content=content,
                new_text=None,
                retry_sync_recovery=retry_sync_recovery,
            )

        async def terminal_edit(
            client: nio.AsyncClient,
            room_id: str,
            event_id: str,
            content: dict[str, Any],
            display_text: str,
            *,
            retry_sync_recovery: bool = False,
            progress: ProgressState,
        ) -> DeliveredMatrixEvent | None:
            del client, room_id
            if answered_nothing(content, progress):
                return DeliveredMatrixEvent(event_id=event_id, content_sent=content)
            return await deliver(
                lambda: terminal(content, progress),
                content=content,
                new_text=display_text,
                retry_sync_recovery=retry_sync_recovery,
            )

        async def progress_write_ahead(progress: ProgressState) -> ProgressPermission:
            return await handle.runtime.write_ahead(handle, shown(progress))

        def progress_delivered(progress: ProgressState, event_id: str) -> None:
            handle.unconfirmed_progress = ReplyProgressConfirmation(
                event_id=event_id,
                placeholder_only=progress.placeholder_only,
            )

        return {
            "initial_send": initial_send,
            "terminal_send": terminal_send,
            "terminal_edit": terminal_edit,
            "progress_write_ahead": progress_write_ahead,
            "progress_delivered": progress_delivered,
        }

    def stream_progress(
        self,
        *,
        target: MessageTarget,
        event_id: str,
        identity: ResponseIdentity,
        show_tool_calls: bool,
        extra_content: dict[str, Any] | None = None,
        visible_progress_callback: Callable[[str], None] | None = None,
    ) -> AbstractAsyncContextManager[ProgressPublisher]:
        """Stream live progress into an existing reply whose terminal delivery the caller owns.

        Each publication is the span's own answer; once the reply also shows
        earlier spans' work, such as a resume below a replay's stopped attempt,
        the stream sends the whole reply so that work stays.
        """
        handle = current_span()
        assert handle is not None, "progress streams into the reply of the span that runs it"
        published: dict[str, Presentation] = {}
        hooks = self._reply_stream_hooks(handle, target, published=published)
        progress = stream_progress_edits(
            self._ready_client(),
            target,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            event_id=event_id,
            show_tool_calls=show_tool_calls,
            extra_content=extra_content,
            visible_progress_callback=visible_progress_callback,
            transport_is_current=self._stream_transport_gate(
                identity.response_envelope.source_event_id,
                target.room_id,
            ),
            # The reply's records hear of each progress edit before it is sent.
            progress_write_ahead=hooks["progress_write_ahead"],
            progress_delivered=hooks["progress_delivered"],
        )
        if all(segment.span_id == handle.span_id for segment in handle.base.segments):
            return progress
        return _whole_reply_progress(progress, handle, published)

    def _stream_transport_gate(
        self,
        turn_id: str,
        room_id: str,
    ) -> Callable[[], Awaitable[bool]]:
        """Return the check that stops a stream editing into an ended membership.

        Progressive edits never reach the outbox, so the durable refusal that
        protects the terminal delivery does not protect them. Without this, a
        turn that began before a fence keeps writing into a conversation the
        fence deleted, for as long as the model keeps producing text.
        """

        async def transport_is_current() -> bool:
            return await self.deps.outbox.turn_membership_is_current(turn_id=turn_id, room_id=room_id)

        return transport_is_current

    async def _prepared_for_the_wire(
        self,
        room_id: str,
        content: dict[str, Any],
        *,
        turn_id: str,
        stage: DeliveryStage,
        continuation_thread_id: str | None = None,
        continuation_reply_to_event_id: str | None = None,
    ) -> _PreparedWirePayload | MatrixDeliveryFailure:
        """Prepare a payload for the wire, unless a frozen one already exists.

        Preparation can upload a sidecar, so it must not run for a turn whose
        row has already been attempted. That row is frozen and `enqueue`
        refuses to overwrite it, so preparing again would upload an attachment
        nothing can ever reference -- or fail before the durable payload gets
        another chance to reach Matrix.

        A durable row may be replayed after room encryption is enabled, and an
        attempted row cannot be rebuilt. Durable sidecars therefore use the
        encrypted form before the payload is frozen, even while the room is
        currently plaintext. Direct sends keep standard plaintext sidecars and
        rebuild only if encryption changes during their upload.
        """
        existing = await self.deps.outbox.load_matrix_delivery(delivery_id=turn_id, stage=stage)
        if existing is not None and existing.attempted:
            return _PreparedWirePayload(content=content)
        client = self._ready_client()
        encryption_outcome = await resolve_room_encryption_outcome(
            client,
            room_id,
            operation="prepare_durable_delivery",
        )
        if isinstance(encryption_outcome, MatrixDeliveryFailure):
            return encryption_outcome
        wire_content = matrix_delivery_payload(
            self.deps.outbox.principal_id,
            turn_id,
            stage,
            content,
        )
        segmented = (
            segment_matrix_content(
                wire_content,
                room_encrypted=encryption_outcome,
                continuation_thread_id=continuation_thread_id,
                continuation_reply_to_event_id=continuation_reply_to_event_id,
            )
            if self.deps.runtime.config.defaults.large_message_strategy == "split"
            else None
        )
        if segmented is not None:
            # Segments already carry the durable identity copied from wire_content.
            self.deps.logger.info(
                "Prepared lossless Matrix response segmentation",
                delivery_id=turn_id,
                stage=stage.value,
                continuation_count=len(segmented.continuations),
            )
            return _PreparedWirePayload(content=segmented.first, continuation_payloads=segmented.continuations)
        try:
            prepared = await prepare_large_message(
                client,
                room_id,
                wire_content,
                room_encrypted=encryption_outcome,
                prepare_for_encrypted_delivery=True,
            )
            return _PreparedWirePayload(content=prepared)
        except MatrixEventTooLargeError as error:
            return MatrixDeliveryFailure(MatrixDeliveryFailureKind.PAYLOAD_TOO_LARGE, str(error))

    def _final_text_transform(self, identity: ResponseIdentity) -> FinalTextTransform:
        """Return the hook that shapes the answer before its terminal payload is built.

        Applying it here keeps the durable row and the room in agreement: the
        payload is frozen from the transformed text, so there is no later edit
        to lose to a crash.
        """

        async def transform(response_text: str) -> str:
            draft = await self.deps.response_hooks._apply_final_response_transform(
                identity=identity,
                response_text=response_text,
            )
            return draft.response_text

        return transform

    async def _finalize_placeholder_only_stream_error(
        self,
        request: FinalizeStreamedResponseRequest,
        *,
        stream_outcome: StreamTransportOutcome,
        failure_reason: str,
    ) -> FinalDeliveryOutcome:
        """Finalize a failed stream whose only visible event is still the placeholder."""
        placeholder_event_id = stream_outcome.last_physical_stream_event_id
        if placeholder_event_id is None:
            return FinalDeliveryOutcome(
                terminal_status="error",
                event_id=None,
                failure_reason=failure_reason,
                tool_trace=tuple(request.tool_trace or ()),
                extra_content=request.extra_content,
            )

        if _is_placeholder_delivery_failure(failure_reason):
            # The reply's answer row stays owed; only its permanent refusal writes the failure note.
            return FinalDeliveryOutcome(
                terminal_status="error",
                event_id=placeholder_event_id,
                is_visible_response=True,
                failure_reason=failure_reason,
                tool_trace=tuple(request.tool_trace or ()),
                extra_content=request.extra_content,
            )

        return await self._cleanup_completed_placeholder_only_stream(
            failure_reason=failure_reason,
            tool_trace=request.tool_trace,
            extra_content=request.extra_content,
        )

    async def _end_resumed_reply_span(
        self,
        request: FinalizeStreamedResponseRequest,
        handle: SpanHandle,
    ) -> FinalDeliveryOutcome:
        """End a resumed reply's continuation that stopped before streaming anything below it."""
        stream_outcome = request.stream_transport_outcome
        failure_reason = stream_outcome.failure_reason or "interrupted"
        # The note shows below the recovered content, and the sources stay for a retry unless a Stop ended the reply.
        outcome = await self.end_interrupted_span(
            handle,
            request.target,
            cancel_source=(
                cancel_source_from_failure_reason(failure_reason)
                if stream_outcome.terminal_status == "cancelled"
                else None
            ),
            failure_reason=failure_reason,
            delivery_started=False,
        )
        assert outcome is not None, "a resumed reply has its event, so its end writes a note"
        return replace(outcome, terminal_status=stream_outcome.terminal_status, failure_reason=failure_reason)

    async def finalize_streamed_response(
        self,
        request: FinalizeStreamedResponseRequest,
    ) -> FinalDeliveryOutcome:
        """Run streamed finalization under the fixed shutdown diagnostic boundary."""
        with response_shutdown_phase(ResponseShutdownPhase.FINAL_DELIVERY):
            return await self._finalize_streamed_response(request)

    async def _finalize_interrupted_stream(  # noqa: PLR0911
        self,
        request: FinalizeStreamedResponseRequest,
        *,
        streamed_event_id: str | None,
        streamed_text: str,
    ) -> FinalDeliveryOutcome:
        """Report a stream a cancellation or failure ended, with the event that stays visible, if any."""
        stream_outcome = request.stream_transport_outcome
        cancelled = stream_outcome.terminal_status == "cancelled"
        failure_reason = stream_outcome.failure_reason or (
            "stream_finalize_cancelled" if cancelled else "stream_finalize_error"
        )
        cancel_source = cancel_source_from_failure_reason(failure_reason) if cancelled else None
        outcome = partial(
            FinalDeliveryOutcome,
            terminal_status=stream_outcome.terminal_status,
            cancel_source=cancel_source,
            failure_reason=failure_reason,
            tool_trace=tuple(request.tool_trace or ()),
            extra_content=request.extra_content,
        )
        if (
            request.initial_delivery_kind == "edited"
            and stream_outcome.visible_body_state == "none"
            and not request.existing_event_is_placeholder
        ):
            existing_visible_event_id = request.existing_event_id or streamed_event_id
            if existing_visible_event_id is not None:
                return outcome(event_id=existing_visible_event_id, is_visible_response=True)
        if stream_outcome.visible_body_state == "placeholder_only":
            if not cancelled:
                return await self._finalize_placeholder_only_stream_error(
                    request,
                    stream_outcome=stream_outcome,
                    failure_reason=failure_reason,
                )
            cleanup_outcome = await self._cleanup_completed_placeholder_only_stream(
                failure_reason=failure_reason,
                tool_trace=request.tool_trace,
                extra_content=request.extra_content,
            )
            if cleanup_outcome.event_id is not None:
                return replace(cleanup_outcome, cancel_source=cancel_source)
            return outcome(event_id=None)
        if stream_outcome.visible_event_id is not None:
            return outcome(
                event_id=stream_outcome.visible_event_id,
                is_visible_response=True,
                final_visible_body=streamed_text or None,
                # Only a cancellation's committed terminal update counts as this stream's delivery.
                delivery_kind=request.initial_delivery_kind
                if cancelled and stream_outcome.terminal_update_committed
                else None,
            )
        if request.existing_event_id is not None and not request.existing_event_is_placeholder:
            return outcome(event_id=request.existing_event_id, is_visible_response=True)
        return outcome(event_id=None)

    async def _finalize_streamed_response(  # noqa: C901, PLR0911, PLR0912
        self,
        request: FinalizeStreamedResponseRequest,
    ) -> FinalDeliveryOutcome:
        """Apply hooks and any final edit needed after streamed delivery completes."""
        stream_outcome = request.stream_transport_outcome
        if current_task_is_process_shutdown() and stream_outcome.terminal_status == "cancelled":
            failure_reason = stream_outcome.failure_reason or "interrupted"
            return FinalDeliveryOutcome(
                terminal_status="cancelled",
                event_id=stream_outcome.last_physical_stream_event_id or request.existing_event_id,
                cancel_source=cancel_source_from_failure_reason(failure_reason),
                failure_reason=failure_reason,
                tool_trace=tuple(request.tool_trace or ()),
                extra_content=request.extra_content,
            )
        try:
            streamed_event_id = stream_outcome.last_physical_stream_event_id
            visible_stream_event_id = stream_outcome.visible_event_id
            streamed_text = stream_outcome.visible_body_text
            final_body_candidate = stream_outcome.canonical_final_body_candidate or streamed_text
            resumed = current_span()
            if (
                resumed is not None
                and resumed.resumed is not None
                and request.existing_event_id is not None
                and stream_outcome.terminal_status in {"cancelled", "error"}
                and stream_outcome.visible_body_state == "none"
            ):
                # A continuation of a stopped attempt that ended before streaming anything below it.
                handle = self._live_span()
                if handle is not None:
                    return await self._end_resumed_reply_span(request, handle)
                # A span that already ended left the reply as its rules decided.
                return FinalDeliveryOutcome(
                    terminal_status=stream_outcome.terminal_status,
                    event_id=request.existing_event_id,
                    is_visible_response=True,
                    failure_reason=stream_outcome.failure_reason or "interrupted",
                    tool_trace=tuple(request.tool_trace or ()),
                    extra_content=request.extra_content,
                )
            if stream_outcome.terminal_status in {"cancelled", "error"}:
                return await self._finalize_interrupted_stream(
                    request,
                    streamed_event_id=streamed_event_id,
                    streamed_text=streamed_text,
                )

            if stream_outcome.canonical_final_body_candidate is not None and stream_outcome.visible_body_state in {
                "none",
                "placeholder_only",
            }:
                existing_event_id = request.existing_event_id
                if stream_outcome.visible_body_state == "placeholder_only":
                    existing_event_id = streamed_event_id
                return await self.deliver_final(
                    FinalDeliveryRequest(
                        target=request.target,
                        existing_event_id=existing_event_id,
                        response_text=stream_outcome.canonical_final_body_candidate,
                        identity=request.identity,
                        tool_trace=request.tool_trace,
                        extra_content=request.extra_content,
                    ),
                )

            if stream_outcome.visible_body_state == "placeholder_only":
                return await self._cleanup_completed_placeholder_only_stream(
                    failure_reason=stream_outcome.failure_reason or "stream_completed_without_visible_body",
                    tool_trace=request.tool_trace,
                    extra_content=request.extra_content,
                )

            if (
                stream_outcome.visible_body_state == "none"
                and stream_outcome.failure_reason is None
                and request.initial_delivery_kind == "edited"
                and not request.existing_event_is_placeholder
            ):
                existing_visible_event_id = request.existing_event_id or streamed_event_id
                if existing_visible_event_id is not None:
                    return FinalDeliveryOutcome(
                        terminal_status="completed",
                        event_id=existing_visible_event_id,
                        is_visible_response=True,
                        final_visible_body=streamed_text or None,
                        delivery_kind="edited",
                        failure_reason=stream_outcome.failure_reason,
                        tool_trace=tuple(request.tool_trace or ()),
                        extra_content=request.extra_content,
                    )

            if stream_outcome.failure_reason is not None and stream_outcome.visible_body_state != "visible_body":
                failure_reason = stream_outcome.failure_reason or "terminal_update_failed"
                if (
                    request.initial_delivery_kind == "edited"
                    and streamed_event_id is not None
                    and visible_stream_event_id is None
                ):
                    return FinalDeliveryOutcome(
                        terminal_status="error",
                        event_id=streamed_event_id,
                        is_visible_response=True,
                        failure_reason=failure_reason,
                        tool_trace=tuple(request.tool_trace or ()),
                        extra_content=request.extra_content,
                    )
                if visible_stream_event_id is not None:
                    return FinalDeliveryOutcome(
                        terminal_status="error",
                        event_id=visible_stream_event_id,
                        is_visible_response=True,
                        final_visible_body=streamed_text or None,
                        failure_reason=failure_reason,
                        tool_trace=tuple(request.tool_trace or ()),
                        extra_content=request.extra_content,
                    )
                return FinalDeliveryOutcome(
                    terminal_status="error",
                    event_id=None,
                    failure_reason=failure_reason,
                    tool_trace=tuple(request.tool_trace or ()),
                    extra_content=request.extra_content,
                )

            if stream_outcome.visible_body_state != "visible_body":
                if (
                    request.initial_delivery_kind == "edited"
                    and not request.existing_event_is_placeholder
                    and stream_outcome.visible_body_state == "none"
                ):
                    existing_visible_event_id = request.existing_event_id or streamed_event_id
                    if existing_visible_event_id is not None:
                        return FinalDeliveryOutcome(
                            terminal_status="error",
                            event_id=existing_visible_event_id,
                            is_visible_response=True,
                            failure_reason=stream_outcome.failure_reason or "stream_completed_without_visible_body",
                            tool_trace=tuple(request.tool_trace or ()),
                            extra_content=request.extra_content,
                        )
                return FinalDeliveryOutcome(
                    terminal_status="error",
                    event_id=None,
                    failure_reason=stream_outcome.failure_reason or "stream_completed_without_visible_body",
                    tool_trace=tuple(request.tool_trace or ()),
                    extra_content=request.extra_content,
                )
            if stream_outcome.failure_reason is not None:
                failure_reason = stream_outcome.failure_reason or "terminal_update_failed"
                return FinalDeliveryOutcome(
                    terminal_status="error",
                    event_id=visible_stream_event_id,
                    is_visible_response=True,
                    final_visible_body=streamed_text,
                    failure_reason=failure_reason,
                    tool_trace=tuple(request.tool_trace or ()),
                    extra_content=request.extra_content,
                )
            # The transform already ran against the answer text, before the
            # terminal payload was built, so the durable outbox row and the
            # room carry the same body. A second edit here is what made them
            # disagree, and losing it to a crash left the room showing raw text.
            assert streamed_event_id is not None
            interactive_response = interactive_response_for_visible_body(
                streamed_text,
                canonical_body_candidate=final_body_candidate,
                stream_interactive_metadata=stream_outcome.interactive_metadata,
            )
            return FinalDeliveryOutcome(
                terminal_status="completed",
                event_id=streamed_event_id,
                is_visible_response=True,
                final_visible_body=streamed_text or interactive_response.formatted_text,
                delivery_kind=request.initial_delivery_kind,
                failure_reason=stream_outcome.failure_reason,
                tool_trace=tuple(request.tool_trace or ()),
                extra_content=request.extra_content,
                interactive_metadata=interactive_response.interactive_metadata,
            )
        except asyncio.CancelledError as error:
            visible_event_id = stream_outcome.visible_event_id
            event_id = visible_event_id
            if event_id is None and request.existing_event_id is not None and not request.existing_event_is_placeholder:
                event_id = request.existing_event_id
            final_visible_body = stream_outcome.visible_body_text if visible_event_id is not None else None
            return FinalDeliveryOutcome(
                terminal_status="cancelled",
                event_id=event_id,
                is_visible_response=event_id is not None,
                final_visible_body=final_visible_body,
                cancel_source=classify_cancel_source(error),
                failure_reason=self._cancelled_error_failure_reason(error),
                tool_trace=tuple(request.tool_trace or ()),
                extra_content=request.extra_content,
            )
        except Exception:
            self.deps.logger.exception(
                "Unexpected error in finalize_streamed_response",
                correlation_id=request.identity.correlation_id,
            )
            visible_event_id = stream_outcome.visible_event_id
            event_id = visible_event_id
            if event_id is None and request.existing_event_id is not None and not request.existing_event_is_placeholder:
                event_id = request.existing_event_id
            final_visible_body = stream_outcome.visible_body_text if visible_event_id is not None else None
            return FinalDeliveryOutcome(
                terminal_status="error",
                event_id=event_id,
                is_visible_response=event_id is not None,
                final_visible_body=final_visible_body,
                cancel_source=stream_outcome.resolved_cancel_source,
                failure_reason="stream_finalize_failed",
                tool_trace=tuple(request.tool_trace or ()),
                extra_content=request.extra_content,
            )
