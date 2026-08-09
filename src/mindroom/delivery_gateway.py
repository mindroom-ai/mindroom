"""Own visible Matrix delivery for already-generated responses."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass, field, replace
from html import escape as html_escape
from typing import TYPE_CHECKING, Any, Literal
from weakref import WeakValueDictionary

from nio.exceptions import SendRetryError

from mindroom import constants, interactive
from mindroom.constants import SKIP_MENTIONS_KEY
from mindroom.event_journal import OutboxDelivery, OutboxView, TerminalTurnWrite
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
    build_edit_event_content,
    edit_message_result,
    send_message_result,
)
from mindroom.matrix.large_messages import prepare_large_message
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_builder import build_message_content
from mindroom.matrix.room_history_reads import find_response_event_ids_via_room_messages
from mindroom.response_delivery import (
    DeliveryStage,
    RecoveryOutcome,
    ResponseDelivery,
    SendDelivery,
    TurnHandoff,
)
from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001
from mindroom.streaming import (
    PROGRESS_PLACEHOLDER,
    FinalTextTransform,
    StreamingResponse,
    TerminalEdit,
    TerminalSend,
    build_cancelled_response_update,
    cancel_failure_reason,
    cancel_source_from_failure_reason,
    classify_cancel_source,
    interactive_response_for_visible_body,
    send_streaming_response,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    import nio
    import structlog

    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.history.types import (
        CompactionLifecycleFailure,
        CompactionLifecycleProgress,
        CompactionLifecycleStart,
        CompactionOutcome,
    )
    from mindroom.hooks import MessageEnvelope
    from mindroom.message_target import MessageTarget
    from mindroom.streaming import StreamInputChunk
    from mindroom.timing import DispatchPipelineTiming
    from mindroom.tool_system.events import ToolTraceEntry

_PLACEHOLDER_DELIVERY_FAILURE_TEXT = "Response delivery failed. Please retry."
_PLACEHOLDER_DELIVERY_FAILURE_REASONS = frozenset(
    {
        "delivery_failed",
        "terminal_update_cancelled",
        "terminal_update_failed",
    },
)


def _is_placeholder_delivery_failure(failure_reason: str) -> bool:
    """Return whether a placeholder-only error came from Matrix delivery itself."""
    return failure_reason in _PLACEHOLDER_DELIVERY_FAILURE_REASONS or failure_reason.startswith(
        "terminal_update_exception:",
    )


class _DeliveryRefusedError(RuntimeError):
    """Matrix declined one outbox delivery, leaving it unacknowledged."""


@dataclass(frozen=True)
class ResponseIdentity:
    """Identify which visible response a delivery or hook call belongs to."""

    response_kind: str
    response_envelope: MessageEnvelope
    correlation_id: str


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
        return await emit_final_response_transform(
            self.hook_context.registry,
            EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM,
            context,
        )

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
    # Which of a turn's two durable delivery points this is. A streamed answer
    # creates its visible message once, as a placeholder, and reaches its final
    # text by editing that message; the placeholder is therefore the delivery
    # whose duplication a reader would see, and it is the initial stage.
    delivery_stage: DeliveryStage = DeliveryStage.FINAL


@dataclass(frozen=True)
class EditTextRequest:  # noqa: D101
    target: MessageTarget
    event_id: str
    new_text: str
    tool_trace: list[ToolTraceEntry] | None = None
    extra_content: dict[str, Any] | None = None
    retry_sync_recovery: bool = False
    # Set when this edit is a turn's final answer. Once a placeholder exists
    # the answer reaches the room as an edit of it, so this is the delivery
    # whose loss leaves a user looking at "Thinking..." for good.
    delivery_turn_id: str | None = None


@dataclass(frozen=True)
class FinalDeliveryRequest:  # noqa: D101
    target: MessageTarget
    existing_event_id: str | None
    response_text: str
    identity: ResponseIdentity
    tool_trace: list[ToolTraceEntry] | None
    extra_content: dict[str, Any] | None
    existing_event_is_placeholder: bool = False
    skip_mentions: bool = False


@dataclass(frozen=True)
class CancelledVisibleNoteRequest:
    """Parameters for one terminal cancellation-note edit."""

    target: MessageTarget
    event_id: str
    existing_event_is_placeholder: bool
    cancel_source: Literal["user_stop", "sync_restart", "interrupted"]
    identity: ResponseIdentity


@dataclass(frozen=True)
class _PlaceholderFailureUpdateRequest:
    """Parameters for finalizing a placeholder after Matrix delivery fails."""

    target: MessageTarget
    event_id: str
    identity: ResponseIdentity
    failure_reason: str
    tool_trace: list[ToolTraceEntry] | None
    extra_content: dict[str, Any] | None


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
    header: str | None = None
    show_tool_calls: bool = False
    extra_content: dict[str, Any] | None = None
    tool_trace_collector: list[ToolTraceEntry] | None = None
    streaming_cls: type[StreamingResponse] = StreamingResponse
    pipeline_timing: DispatchPipelineTiming | None = None
    visible_event_id_callback: Callable[[str], None] | None = None
    preserve_existing_visible_on_empty_terminal: bool = False


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
    outbox: OutboxView
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
    terminal_turn_committed: Callable[[str, str], Awaitable[None]] | None = None


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

    def _client(self) -> nio.AsyncClient:
        """Return the current Matrix client required for delivery."""
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for response delivery"
            raise RuntimeError(msg)
        return client

    @staticmethod
    def _cancelled_error_failure_reason(error: asyncio.CancelledError) -> str:
        """Normalize CancelledError values to the canonical cancellation reason strings."""
        return cancel_failure_reason(classify_cancel_source(error))

    async def _cleanup_completed_placeholder_only_stream(
        self,
        *,
        room_id: str,
        streamed_event_id: str | None,
        identity: ResponseIdentity,
        failure_reason: str,
        tool_trace: list[ToolTraceEntry] | None,
        extra_content: dict[str, Any] | None,
    ) -> FinalDeliveryOutcome:
        """Remove a completed placeholder-only streamed event before returning no-visible-response."""
        if streamed_event_id is not None:
            cleanup_failure = await self._redact_visible_response_event(
                room_id=room_id,
                event_id=streamed_event_id,
                identity=identity,
                redaction_reason="Completed placeholder-only streamed response",
                failure_reason=failure_reason,
            )
            if cleanup_failure is not None:
                return FinalDeliveryOutcome(
                    terminal_status="error",
                    event_id=streamed_event_id,
                    is_visible_response=False,
                    failure_reason=cleanup_failure,
                    tool_trace=tuple(tool_trace or ()),
                    extra_content=extra_content,
                )
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

    async def _redact_visible_response_event(
        self,
        *,
        room_id: str,
        event_id: str,
        identity: ResponseIdentity,
        redaction_reason: str,
        failure_reason: str | None = None,
        propagate_cancelled: bool = False,
    ) -> str | None:
        """Redact one visible event, optionally propagating cancellation, and return any cleanup failure."""
        if not await self._visible_notice_is_current(identity, room_id):
            # The event this would tidy up belonged to a membership that has
            # ended, and the fence has already dropped everything derived from
            # it. There is nothing left here to clean up, and no failure.
            return None
        self.deps.logger.warning(
            "Visible response was already delivered before suppression; attempting cleanup",
            response_kind=identity.response_kind,
            source_event_id=identity.response_envelope.source_event_id,
            correlation_id=identity.correlation_id,
            visible_response_event_id=event_id,
        )
        try:
            redacted = await self.deps.redact_message_event(
                room_id=room_id,
                event_id=event_id,
                reason=redaction_reason,
            )
        except asyncio.CancelledError as error:
            if propagate_cancelled:
                raise
            return self._cancelled_error_failure_reason(error)
        except Exception as error:
            self.deps.logger.exception(
                "Failed to redact visible response during cleanup",
                room_id=room_id,
                event_id=event_id,
                response_kind=identity.response_kind,
                correlation_id=identity.correlation_id,
            )
            return str(error) or failure_reason or f"failed to redact suppressed response {event_id}"
        if not redacted:
            return failure_reason or f"failed to redact suppressed response {event_id}"
        return None

    async def _finish_placeholder_delivery_failure(
        self,
        request: _PlaceholderFailureUpdateRequest,
    ) -> FinalDeliveryOutcome:
        """Best-effort terminal error edit for a visible placeholder."""
        failure_extra_content = dict(request.extra_content or {})
        failure_extra_content[constants.STREAM_STATUS_KEY] = constants.STREAM_STATUS_ERROR
        edited = await self._visible_notice_is_current(
            request.identity,
            request.target.room_id,
        ) and await self.edit_text(
            EditTextRequest(
                target=request.target,
                event_id=request.event_id,
                new_text=_PLACEHOLDER_DELIVERY_FAILURE_TEXT,
                tool_trace=request.tool_trace,
                extra_content=failure_extra_content,
            ),
        )
        if edited:
            return FinalDeliveryOutcome(
                terminal_status="error",
                event_id=request.event_id,
                is_visible_response=True,
                final_visible_body=_PLACEHOLDER_DELIVERY_FAILURE_TEXT,
                delivery_kind="edited",
                failure_reason=request.failure_reason,
                tool_trace=tuple(request.tool_trace or ()),
                extra_content=failure_extra_content,
            )

        self.deps.logger.error(
            "Failed to deliver placeholder failure update",
            room_id=request.target.room_id,
            event_id=request.event_id,
            response_kind=request.identity.response_kind,
            source_event_id=request.identity.response_envelope.source_event_id,
            correlation_id=request.identity.correlation_id,
            failure_reason=request.failure_reason,
        )
        return FinalDeliveryOutcome(
            terminal_status="error",
            event_id=request.event_id,
            is_visible_response=True,
            failure_reason=request.failure_reason,
            tool_trace=tuple(request.tool_trace or ()),
            extra_content=failure_extra_content,
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
        row = await self.deps.outbox.load_delivery(turn_id=turn_id, stage=stage)
        content = dict(row.payload) if row is not None else fallback
        return DeliveredMatrixEvent(event_id=event_id, content_sent=content)

    async def _send_claimed(
        self,
        claimed: OutboxDelivery,
        *,
        retry_sync_recovery: bool,
    ) -> DeliveredMatrixEvent:
        """Send one claimed delivery exactly as it was frozen.

        The payload comes from the row, never from whatever the caller happens
        to be holding. A turn that ran twice sends what its first attempt
        froze: content regenerated after a claim would go out under a
        transaction ID the homeserver has already seen, be dropped as a
        duplicate, and leave the durable result and the room disagreeing
        forever.
        """
        delivered = await send_message_result(
            self._client(),
            claimed.room_id,
            dict(claimed.payload),
            retry_sync_recovery=retry_sync_recovery,
            transaction_id=claimed.transaction_id,
        )
        if delivered is None:
            msg = f"Matrix refused delivery for turn {claimed.turn_id!r} stage {claimed.stage.value!r}"
            raise _DeliveryRefusedError(msg)
        return delivered

    def _response_delivery(self, send: SendDelivery, *, handoff: TurnHandoff | None) -> ResponseDelivery:
        """Return the outbox writer, for a live delivery or for recovery.

        Both go through here so they cannot drift. They did: recovery was built
        separately and silently lacked the terminal-record hook, so a recovered
        answer acknowledged its row while the turn record stayed ignorant of
        the event -- the very state the deleted repair pass used to fix.

        The handoff is the one real difference, and recovery passes ``None``:
        it resends rows that already exist, and the sources those rows answer
        were handed over when the rows were first recorded.
        """
        return ResponseDelivery(
            store=self.deps.outbox,
            send=send,
            sending_device_id=self.deps.sending_device_id(),
            resolve_delivered=self._delivered_under_a_previous_device,
            handoff=handoff,
            terminal_turn_for=self._terminal_turn_write,
            terminal_turn_committed=self.deps.terminal_turn_committed,
            turn_locks=self._delivery_turn_locks,
        )

    def _terminal_turn_write(self, turn_id: str, event_id: str) -> TerminalTurnWrite | None:
        """Turn the terminal record for one delivered answer into a journal row.

        The turn store produces the record and stops at its own boundary; this
        layer already owns the journal's types, so the conversion belongs here
        rather than reaching across.
        """
        if self.deps.terminal_turn_for is None:
            return None
        record = self.deps.terminal_turn_for(turn_id, event_id)
        if record is None or record.anchor_event_id is None:
            return None
        return TerminalTurnWrite(
            agent_name=self.deps.agent_name,
            index_event_ids=record.indexed_event_ids,
            anchor_event_id=record.anchor_event_id,
            record_json=json.dumps(TurnRecordCodec._to_ledger_record(record)),
        )

    async def _delivered_under_a_previous_device(self, claimed: OutboxDelivery) -> str | None:
        """Return the answer an earlier device already put in the room, if it did.

        Reached only when the frozen transaction ID has stopped being proof,
        which is a re-login between the attempt and the retry. The room itself
        is then the only witness, so it is read the same way replayed turns
        read it: find a message from this bot replying to the sources this turn
        answers.

        The turn's sources come from the durable ledger rather than from
        anything this process remembers, because the process that made the
        first attempt is gone. A turn the ledger has never heard of resolves to
        its own anchor event, which is the right question for the uncoalesced
        case and the only one available for the rest.

        This finds answers, not every message. The scan matches a genuine
        reply -- ``is_falling_back`` false, pointing at a source -- which is
        what an agent answering a user sends. A delivery with no source to
        reply to, a scheduled message or a hook-emitted notice, matches
        nothing and reads as "not delivered", so it is sent. That is the same
        blind resend as before this guard existed: unchanged for the cases it
        cannot see, and correct for the ones it can.
        """
        client = self._client()
        response_sender = client.user_id
        if not response_sender:
            return None
        source_event_ids = self.deps.turn_handoff.sources_for_turn(claimed.turn_id)
        delivered = await find_response_event_ids_via_room_messages(
            client,
            claimed.room_id,
            response_sender=response_sender,
            source_event_ids=source_event_ids,
        )
        if len(delivered) > 1:
            # Two visible answers to the same sources is the duplicate this
            # lookup exists to prevent, already committed. Adopting one at
            # random would bind every later edit to a coin flip, so the row
            # stays unacknowledged and a human-visible error is raised rather
            # than a third answer sent.
            msg = (
                f"Turn {claimed.turn_id!r} has {len(delivered)} visible answers in {claimed.room_id}, "
                f"so no single one can be adopted after the sending device changed"
            )
            raise RuntimeError(msg)
        return next(iter(delivered), None)

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

        async def send(claimed: OutboxDelivery) -> str:
            delivered = await self._send_claimed(claimed, retry_sync_recovery=True)
            return delivered.event_id

        return await self._response_delivery(send, handoff=None).recover()

    async def _send_content(
        self,
        request: SendTextRequest,
        room_id: str,
        content: dict[str, Any],
    ) -> DeliveredMatrixEvent | None:
        """Send one built message, through the outbox when it belongs to a turn.

        A send with no turn behind it -- a voice echo, a command confirmation,
        a reconciliation notice -- takes the direct path. It has no identity
        that survives a restart, so there is nothing for recovery to key on and
        a durable row would only be a row nobody can resolve.
        """
        client = self._client()
        if request.delivery_turn_id is None:
            return await send_message_result(
                client,
                room_id,
                content,
                retry_sync_recovery=request.retry_sync_recovery,
            )
        # Prepared before the row is written, so the frozen payload is the one
        # that goes on the wire. Uploading the sidecar after the claim left the
        # row holding the oversized original while Matrix received an MXC
        # reference, and a recovery resend would upload again -- minting a new
        # MXC, and new encrypted-file keys, under a transaction ID the
        # homeserver had already accepted. Preparing twice is harmless: an
        # already-prepared payload is below the size limit, so this is a no-op
        # for it, including on the recovery path.
        content = await self._prepared_for_the_wire(
            room_id,
            content,
            turn_id=request.delivery_turn_id,
            stage=request.delivery_stage,
        )
        requested_delivery: DeliveredMatrixEvent | None = None

        async def send(claimed: OutboxDelivery) -> str:
            nonlocal requested_delivery
            delivered = await self._send_claimed(claimed, retry_sync_recovery=request.retry_sync_recovery)
            if claimed.stage is request.delivery_stage:
                requested_delivery = delivered
            return delivered.event_id

        try:
            event_id = await self._response_delivery(send, handoff=self.deps.turn_handoff).deliver(
                turn_id=request.delivery_turn_id,
                stage=request.delivery_stage,
                room_id=room_id,
                thread_id=request.target.resolved_thread_id,
                payload=content,
            )
        except _DeliveryRefusedError:
            return None
        if event_id is None:
            # The membership this turn answered has ended. The room it was
            # answering is not the room the bot is in now, so there is nothing
            # to send and nothing to recover.
            return None
        if requested_delivery is not None and requested_delivery.event_id == event_id:
            return requested_delivery
        # The delivery was already acknowledged, so nothing was sent and the
        # callback never ran. That is a turn re-running after its answer
        # reached the room; reporting it as a failed send would make a
        # delivered answer look lost and invite a duplicate.
        return await self._acknowledged_delivery(request.delivery_turn_id, request.delivery_stage, event_id, content)

    async def send_text(self, request: SendTextRequest) -> str | None:
        """Send one response message to a room."""
        config = self.deps.runtime.config
        resolved_target = request.target
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
        failure_reason = "send_message_result returned None"
        try:
            delivered = await self._send_content(request, resolved_target.room_id, content)
        except SendRetryError:
            delivered = None
            failure_reason = "matrix timeline recovery still blocked the send"
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
    ) -> DeliveredMatrixEvent | None:
        """Apply one edit, through the outbox when it carries a turn's answer.

        Once a turn has a placeholder, its answer reaches the room as an edit
        of that message rather than a new one, so this is where the answer
        becomes visible and where losing it leaves the user reading
        "Thinking..." with nothing durable to recover.

        Edits that are not a turn's answer -- streaming progress, cancellation
        notices, failure updates -- take the direct path. They are transport,
        and a durable row per streamed revision would put a claim-before-send
        round trip inside the streaming loop.
        """
        client = self._client()
        if request.delivery_turn_id is None:
            return await edit_message_result(
                client,
                room_id,
                request.event_id,
                content,
                request.new_text,
                retry_sync_recovery=request.retry_sync_recovery,
            )
        # What is stored is the finished wire event, not the text it was built
        # from. Recovery sends the row exactly as frozen and has no request to
        # rebuild from, so anything reconstructed at send time -- the replace
        # envelope, the fallback body -- would be missing on the one path that
        # matters, and the answer would come back as a second message with the
        # placeholder still above it.
        envelope = build_edit_event_content(
            event_id=request.event_id,
            new_content=content,
            new_text=request.new_text,
        )
        # Prepared before the row is written, for the same reason the envelope
        # is built here: the row has to hold the finished wire event. A sidecar
        # uploaded after the claim would leave the row holding the oversized
        # original while Matrix received an MXC reference, and a resend would
        # upload again under a transaction ID already accepted. Preparing an
        # already-prepared payload is a no-op, so the recovery path is safe.
        envelope = await self._prepared_for_the_wire(
            room_id,
            envelope,
            turn_id=request.delivery_turn_id,
            stage=DeliveryStage.FINAL,
        )
        delivered: DeliveredMatrixEvent | None = None

        async def send(claimed: OutboxDelivery) -> str:
            # The frozen row, not the request that produced it. `edit_message_result`
            # would rebuild the envelope from the current closure, which is the same
            # bytes on a first attempt and the wrong ones on a second: a row is frozen
            # once attempted, so a regenerated answer would go out under a transaction
            # ID the homeserver has already seen -- dropped as a duplicate if the first
            # attempt landed, visible while the durable row says otherwise if it did
            # not. The stored envelope already is what that helper would build.
            nonlocal delivered
            edited = await send_message_result(
                client,
                claimed.room_id,
                dict(claimed.payload),
                operation="edit_message",
                retry_sync_recovery=request.retry_sync_recovery,
                transaction_id=claimed.transaction_id,
            )
            if edited is None:
                msg = f"Matrix refused the final edit for turn {claimed.turn_id!r}"
                raise _DeliveryRefusedError(msg)
            delivered = edited
            return edited.event_id

        try:
            event_id = await self._response_delivery(send, handoff=self.deps.turn_handoff).deliver(
                turn_id=request.delivery_turn_id,
                stage=DeliveryStage.FINAL,
                room_id=room_id,
                thread_id=request.target.resolved_thread_id,
                payload=envelope,
                edits_event_id=request.event_id,
            )
        except _DeliveryRefusedError:
            return None
        if event_id is None:
            # The membership this turn answered has ended, so its answer is
            # not this bot's to give in the room it is in now.
            return None
        if delivered is not None:
            return delivered
        # Already acknowledged: this turn's answer reached the room on an
        # earlier run, so nothing was sent and the callback never ran.
        return await self._acknowledged_delivery(request.delivery_turn_id, DeliveryStage.FINAL, event_id, envelope)

    async def edit_text(self, request: EditTextRequest) -> bool:
        """Edit one existing response message."""
        config = self.deps.runtime.config
        target = request.target
        # The edit envelope discards any pre-existing relation before adding m.replace.
        content = format_message_with_mentions(
            config,
            self.deps.runtime_paths,
            request.new_text,
            tool_trace=request.tool_trace,
            extra_content=request.extra_content,
        )

        failure_reason = "edit_message_result returned None"
        try:
            delivered = await self._edit_content(request, target.room_id, content)
        except SendRetryError:
            delivered = None
            failure_reason = "matrix timeline recovery still blocked the edit"
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

    async def deliver_final(  # noqa: C901, PLR0911, PLR0912
        self,
        request: FinalDeliveryRequest,
    ) -> FinalDeliveryOutcome:
        """Apply before_response hooks and perform the final send or edit."""
        try:
            draft = await self.deps.response_hooks._apply_before_response(
                identity=request.identity,
                response_text=request.response_text,
                tool_trace=request.tool_trace,
                extra_content=request.extra_content,
            )
        except asyncio.CancelledError as error:
            failure_reason = self._cancelled_error_failure_reason(error)
            cancel_source = classify_cancel_source(error)
            if request.existing_event_id is not None and request.existing_event_is_placeholder:
                cleanup_failure = await self._redact_visible_response_event(
                    room_id=request.target.room_id,
                    event_id=request.existing_event_id,
                    identity=request.identity,
                    redaction_reason="Cancelled placeholder response",
                    failure_reason=failure_reason,
                )
                if cleanup_failure is not None:
                    return FinalDeliveryOutcome(
                        terminal_status="error",
                        event_id=request.existing_event_id,
                        is_visible_response=True,
                        cancel_source=cancel_source,
                        failure_reason=cleanup_failure,
                        tool_trace=tuple(request.tool_trace or ()),
                        extra_content=request.extra_content,
                    )
            raise
        except Exception as error:
            failure_reason = str(error)
            if request.existing_event_id is not None and request.existing_event_is_placeholder:
                cleanup_failure = await self._redact_visible_response_event(
                    room_id=request.target.room_id,
                    event_id=request.existing_event_id,
                    identity=request.identity,
                    redaction_reason="Failed placeholder response before delivery",
                    failure_reason=failure_reason,
                    propagate_cancelled=True,
                )
                if cleanup_failure is not None:
                    return FinalDeliveryOutcome(
                        terminal_status="error",
                        event_id=request.existing_event_id,
                        is_visible_response=True,
                        failure_reason=cleanup_failure,
                        tool_trace=tuple(request.tool_trace or ()),
                        extra_content=request.extra_content,
                    )
            if request.existing_event_id is not None and not request.existing_event_is_placeholder:
                return FinalDeliveryOutcome(
                    terminal_status="error",
                    event_id=request.existing_event_id,
                    is_visible_response=True,
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
        if draft.suppress:
            self.deps.logger.info(
                "Response suppressed by hook",
                response_kind=request.identity.response_kind,
                source_event_id=request.identity.response_envelope.source_event_id,
                correlation_id=request.identity.correlation_id,
            )
            if request.existing_event_id is not None and request.existing_event_is_placeholder:
                cleanup_failure = await self._redact_visible_response_event(
                    room_id=request.target.room_id,
                    event_id=request.existing_event_id,
                    identity=request.identity,
                    redaction_reason="Suppressed placeholder response",
                    failure_reason="suppressed_by_hook",
                )
                if cleanup_failure is not None:
                    return FinalDeliveryOutcome(
                        terminal_status="error",
                        event_id=request.existing_event_id,
                        is_visible_response=True,
                        failure_reason=cleanup_failure,
                        suppressed=True,
                        tool_trace=tuple(draft.tool_trace or ()),
                        extra_content=draft.extra_content,
                    )
                return FinalDeliveryOutcome(
                    terminal_status="cancelled",
                    event_id=None,
                    failure_reason="suppressed_by_hook",
                    suppressed=True,
                    tool_trace=tuple(draft.tool_trace or ()),
                    extra_content=draft.extra_content,
                )
            if request.existing_event_id is not None:
                return FinalDeliveryOutcome(
                    terminal_status="cancelled",
                    event_id=request.existing_event_id,
                    is_visible_response=True,
                    failure_reason="suppressed_by_hook",
                    suppressed=True,
                    tool_trace=tuple(draft.tool_trace or ()),
                    extra_content=draft.extra_content,
                )
            return FinalDeliveryOutcome(
                terminal_status="cancelled",
                event_id=None,
                failure_reason="suppressed_by_hook",
                suppressed=True,
                tool_trace=tuple(draft.tool_trace or ()),
                extra_content=draft.extra_content,
            )

        interactive_response = interactive.parse_and_format_interactive(draft.response_text, extract_mapping=True)
        display_text = interactive_response.formatted_text

        if request.existing_event_id is not None:
            edited = await self.edit_text(
                EditTextRequest(
                    target=request.target,
                    event_id=request.existing_event_id,
                    new_text=display_text,
                    tool_trace=draft.tool_trace,
                    extra_content=draft.extra_content,
                    delivery_turn_id=request.identity.response_envelope.source_event_id,
                    retry_sync_recovery=True,
                ),
            )
            if edited:
                return FinalDeliveryOutcome(
                    terminal_status="completed",
                    event_id=request.existing_event_id,
                    is_visible_response=True,
                    final_visible_body=display_text,
                    delivery_kind="edited",
                    tool_trace=tuple(draft.tool_trace or ()),
                    extra_content=draft.extra_content,
                    interactive_metadata=interactive_response.interactive_metadata,
                )

            if request.existing_event_is_placeholder:
                return await self._finish_placeholder_delivery_failure(
                    _PlaceholderFailureUpdateRequest(
                        target=request.target,
                        event_id=request.existing_event_id,
                        identity=request.identity,
                        failure_reason="delivery_failed",
                        tool_trace=draft.tool_trace,
                        extra_content=draft.extra_content,
                    ),
                )
            return FinalDeliveryOutcome(
                terminal_status="error",
                event_id=request.existing_event_id,
                is_visible_response=True,
                failure_reason="delivery_failed",
                tool_trace=tuple(draft.tool_trace or ()),
                extra_content=draft.extra_content,
            )
        event_id = await self.send_text(
            SendTextRequest(
                target=request.target,
                response_text=display_text,
                skip_mentions=request.skip_mentions,
                tool_trace=draft.tool_trace,
                extra_content=draft.extra_content,
                retry_sync_recovery=True,
                # The Matrix event that caused this turn. The handled-turn
                # ledger already keys on it, and it re-derives to the same
                # value after a restart, which a generated ID would not.
                delivery_turn_id=request.identity.response_envelope.source_event_id,
            ),
        )
        if event_id is None:
            return FinalDeliveryOutcome(
                terminal_status="error",
                event_id=None,
                failure_reason="delivery_failed",
                tool_trace=tuple(draft.tool_trace or ()),
                extra_content=draft.extra_content,
            )
        return FinalDeliveryOutcome(
            terminal_status="completed",
            event_id=event_id,
            is_visible_response=True,
            final_visible_body=display_text,
            delivery_kind="sent",
            tool_trace=tuple(draft.tool_trace or ()),
            extra_content=draft.extra_content,
            interactive_metadata=interactive_response.interactive_metadata,
        )

    async def deliver_cancelled_visible_note(
        self,
        request: CancelledVisibleNoteRequest,
    ) -> FinalDeliveryOutcome:
        """Edit the in-flight visible response into a terminal cancellation note."""
        cancelled_text, stream_status = build_cancelled_response_update("", cancel_source=request.cancel_source)
        extra_content = {constants.STREAM_STATUS_KEY: stream_status}
        failure_reason = cancel_failure_reason(request.cancel_source)
        # A cancellation note is transport, not a turn's answer, so it never
        # reaches the outbox and nothing else would keep it out of a room this
        # bot has left.
        edited = await self._visible_notice_is_current(
            request.identity,
            request.target.room_id,
        ) and await self.edit_text(
            EditTextRequest(
                target=request.target,
                event_id=request.event_id,
                new_text=cancelled_text,
                extra_content=extra_content,
            ),
        )
        if edited:
            return FinalDeliveryOutcome(
                terminal_status="cancelled",
                event_id=request.event_id,
                is_visible_response=True,
                final_visible_body=cancelled_text,
                delivery_kind="edited",
                cancel_source=request.cancel_source,
                failure_reason=failure_reason,
                extra_content=extra_content,
            )
        if not request.existing_event_is_placeholder:
            return FinalDeliveryOutcome(
                terminal_status="cancelled",
                event_id=request.event_id,
                is_visible_response=True,
                final_visible_body=cancelled_text,
                cancel_source=request.cancel_source,
                failure_reason=failure_reason,
                extra_content=extra_content,
            )
        cleanup_failure = await self._redact_visible_response_event(
            room_id=request.target.room_id,
            event_id=request.event_id,
            identity=request.identity,
            redaction_reason="Failed cancelled placeholder response",
            failure_reason=failure_reason,
        )
        if cleanup_failure is not None:
            return FinalDeliveryOutcome(
                terminal_status="error",
                event_id=request.event_id,
                is_visible_response=True,
                cancel_source=request.cancel_source,
                failure_reason=cleanup_failure,
                extra_content=extra_content,
            )
        return FinalDeliveryOutcome(
            terminal_status="cancelled",
            event_id=None,
            cancel_source=request.cancel_source,
            failure_reason=failure_reason,
            extra_content=extra_content,
        )

    async def finalize_user_stopped_response(self, target: MessageTarget, event_id: str) -> bool:
        """Edit a recovered in-flight response into its terminal user-stop state."""
        cancelled_text, stream_status = build_cancelled_response_update("", cancel_source="user_stop")
        return await self.edit_text(
            EditTextRequest(
                target=target,
                event_id=event_id,
                new_text=cancelled_text,
                extra_content={constants.STREAM_STATUS_KEY: stream_status},
            ),
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
        delivered = await send_message_result(self._client(), target.room_id, content)
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
        delivered = await edit_message_result(
            self._client(),
            target.room_id,
            event_id,
            content,
            body,
        )
        if delivered is not None:
            self.deps.logger.info("Edited compaction lifecycle notice", event_id=event_id, **target.log_context)
            return
        self.deps.logger.error("Failed to edit compaction lifecycle notice", event_id=event_id, **target.log_context)

    async def deliver_stream(
        self,
        request: StreamingDeliveryRequest,
    ) -> StreamTransportOutcome:
        """Send one streaming Matrix response."""
        client = self._client()
        config = self.deps.runtime.config
        # The turn this stream answers. Its terminal edit is the delivery that
        # makes the answer visible, so that one becomes durable; every earlier
        # edit stays transport.
        delivery_turn_id = request.identity.response_envelope.source_event_id
        latest_thread_event_id = await self.deps.resolver.deps.conversation_reader.latest_thread_event_id(
            room_id=request.target.room_id,
            thread_id=request.target.resolved_thread_id,
            reply_to_event_id=request.target.reply_to_event_id,
            existing_event_id=request.existing_event_id,
        )
        return await send_streaming_response(
            client,
            request.target,
            config,
            self.deps.runtime_paths,
            request.response_stream,
            streaming_cls=request.streaming_cls,
            header=request.header,
            show_tool_calls=request.show_tool_calls,
            existing_event_id=request.existing_event_id,
            adopt_existing_placeholder=request.adopt_existing_placeholder,
            extra_content=request.extra_content,
            tool_trace_collector=request.tool_trace_collector,
            pipeline_timing=request.pipeline_timing,
            visible_event_id_callback=request.visible_event_id_callback,
            latest_thread_event_id=latest_thread_event_id,
            preserve_existing_visible_on_empty_terminal=(
                request.preserve_existing_visible_on_empty_terminal
                or (request.existing_event_id is not None and not request.adopt_existing_placeholder)
            ),
            terminal_edit=self._durable_terminal_edit(delivery_turn_id, request.target),
            terminal_send=self._durable_terminal_send(delivery_turn_id, request.target),
            final_text_transform=self._final_text_transform(request.identity),
            transport_is_current=self._stream_transport_gate(delivery_turn_id, request.target.room_id),
        )

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

    def _durable_terminal_send(self, turn_id: str, target: MessageTarget) -> TerminalSend:
        """Return a sender that records a stream's terminal *send* before making it.

        A stream normally edits a placeholder, but there is not always one to
        edit: a queued forced compaction suppresses it deliberately, and its
        own send can simply fail. The answer is then the stream's first
        visible event, and without this it would reach the room with no
        durable row behind it -- the one thing the outbox exists to prevent.
        """

        async def terminal_send(
            client: nio.AsyncClient,
            room_id: str,
            content: dict[str, Any],
            display_text: str,
            *,
            retry_sync_recovery: bool = False,
        ) -> DeliveredMatrixEvent | None:
            del client, room_id
            if display_text == PROGRESS_PLACEHOLDER:
                # Same reasoning as the terminal edit: a stream that ends
                # reading "Thinking..." has not answered, and recording that
                # as the turn's final delivery would settle it with a
                # placeholder and leave `deliver_final` nothing to do.
                return await send_message_result(
                    self._client(),
                    target.room_id,
                    content,
                    retry_sync_recovery=retry_sync_recovery,
                )
            return await self._send_content(
                SendTextRequest(
                    target=target,
                    response_text="",
                    retry_sync_recovery=retry_sync_recovery,
                    delivery_turn_id=turn_id,
                    delivery_stage=DeliveryStage.FINAL,
                ),
                target.room_id,
                content,
            )

        return terminal_send

    async def _prepared_for_the_wire(
        self,
        room_id: str,
        content: dict[str, Any],
        *,
        turn_id: str,
        stage: DeliveryStage,
    ) -> dict[str, Any]:
        """Prepare a payload for the wire, unless a frozen one already exists.

        Preparation can upload a sidecar, so it must not run for a turn whose
        answer is already acknowledged. That row is what `flush` returns, its
        payload is frozen and `enqueue` refuses to overwrite it, so preparing
        again would upload an attachment nothing can ever reference -- or fail,
        and take down a rerun whose answer is already durable and visible.
        """
        existing = await self.deps.outbox.load_delivery(turn_id=turn_id, stage=stage)
        if existing is not None and existing.acknowledged_event_id is not None:
            return content
        return await prepare_large_message(self._client(), room_id, content)

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

    def _durable_terminal_edit(self, turn_id: str, target: MessageTarget) -> TerminalEdit:
        """Return a sender that records a stream's terminal edit before making it.

        Nothing extra is sent. The edit the stream was going to make anyway is
        enqueued first and acknowledged after, so an unacknowledged row means
        exactly "the terminal edit never landed" -- which is the condition
        startup recovery should act on, and the only one.
        """

        async def terminal_edit(
            client: nio.AsyncClient,
            room_id: str,
            event_id: str,
            content: dict[str, Any],
            display_text: str,
            *,
            retry_sync_recovery: bool = False,
        ) -> DeliveredMatrixEvent | None:
            del client, room_id
            if display_text == PROGRESS_PLACEHOLDER:
                # A stream that ends still reading "Thinking..." has not
                # answered. Recording that as the turn's final delivery would
                # settle it with a placeholder, and `deliver_final` -- which
                # delivers the answer in exactly this case -- would then find
                # its own delivery already acknowledged and send nothing.
                return await edit_message_result(
                    self._client(),
                    target.room_id,
                    event_id,
                    content,
                    display_text,
                    retry_sync_recovery=retry_sync_recovery,
                )
            return await self._edit_content(
                EditTextRequest(
                    target=target,
                    event_id=event_id,
                    new_text=display_text,
                    retry_sync_recovery=retry_sync_recovery,
                    delivery_turn_id=turn_id,
                ),
                target.room_id,
                content,
            )

        return terminal_edit

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
            return await self._finish_placeholder_delivery_failure(
                _PlaceholderFailureUpdateRequest(
                    target=request.target,
                    event_id=placeholder_event_id,
                    identity=request.identity,
                    failure_reason=failure_reason,
                    tool_trace=request.tool_trace,
                    extra_content=request.extra_content,
                ),
            )

        return await self._cleanup_completed_placeholder_only_stream(
            room_id=request.target.room_id,
            streamed_event_id=placeholder_event_id,
            identity=request.identity,
            failure_reason=failure_reason,
            tool_trace=request.tool_trace,
            extra_content=request.extra_content,
        )

    async def finalize_streamed_response(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self,
        request: FinalizeStreamedResponseRequest,
    ) -> FinalDeliveryOutcome:
        """Apply hooks and any final edit needed after streamed delivery completes."""
        stream_outcome = request.stream_transport_outcome
        try:
            streamed_event_id = stream_outcome.last_physical_stream_event_id
            visible_stream_event_id = stream_outcome.visible_event_id
            streamed_text = stream_outcome.visible_body_text
            final_body_candidate = stream_outcome.canonical_final_body_candidate or streamed_text
            if stream_outcome.terminal_status == "cancelled":
                failure_reason = stream_outcome.failure_reason or "stream_finalize_cancelled"
                cancel_source = cancel_source_from_failure_reason(failure_reason)
                if (
                    request.initial_delivery_kind == "edited"
                    and stream_outcome.visible_body_state == "none"
                    and not request.existing_event_is_placeholder
                ):
                    existing_visible_event_id = request.existing_event_id or streamed_event_id
                    if existing_visible_event_id is not None:
                        return FinalDeliveryOutcome(
                            terminal_status="cancelled",
                            event_id=existing_visible_event_id,
                            is_visible_response=True,
                            cancel_source=cancel_source,
                            failure_reason=failure_reason,
                            tool_trace=tuple(request.tool_trace or ()),
                            extra_content=request.extra_content,
                        )
                if stream_outcome.visible_body_state == "placeholder_only":
                    cleanup_outcome = await self._cleanup_completed_placeholder_only_stream(
                        room_id=request.target.room_id,
                        streamed_event_id=stream_outcome.last_physical_stream_event_id,
                        identity=request.identity,
                        failure_reason=failure_reason,
                        tool_trace=request.tool_trace,
                        extra_content=request.extra_content,
                    )
                    if cleanup_outcome.event_id is not None:
                        return replace(cleanup_outcome, cancel_source=cancel_source)
                    return FinalDeliveryOutcome(
                        terminal_status="cancelled",
                        event_id=None,
                        cancel_source=cancel_source,
                        failure_reason=failure_reason,
                        tool_trace=tuple(request.tool_trace or ()),
                        extra_content=request.extra_content,
                    )

                visible_stream_event_id = stream_outcome.visible_event_id
                if visible_stream_event_id is not None:
                    return FinalDeliveryOutcome(
                        terminal_status="cancelled",
                        event_id=visible_stream_event_id,
                        is_visible_response=True,
                        final_visible_body=streamed_text or None,
                        delivery_kind=request.initial_delivery_kind
                        if stream_outcome.terminal_update_committed
                        else None,
                        cancel_source=cancel_source,
                        failure_reason=failure_reason,
                        tool_trace=tuple(request.tool_trace or ()),
                        extra_content=request.extra_content,
                    )
                if request.existing_event_id is not None and not request.existing_event_is_placeholder:
                    return FinalDeliveryOutcome(
                        terminal_status="cancelled",
                        event_id=request.existing_event_id,
                        is_visible_response=True,
                        cancel_source=cancel_source,
                        failure_reason=failure_reason,
                        tool_trace=tuple(request.tool_trace or ()),
                        extra_content=request.extra_content,
                    )
                return FinalDeliveryOutcome(
                    terminal_status="cancelled",
                    event_id=None,
                    cancel_source=cancel_source,
                    failure_reason=failure_reason,
                    tool_trace=tuple(request.tool_trace or ()),
                    extra_content=request.extra_content,
                )

            if stream_outcome.terminal_status == "error":
                if (
                    request.initial_delivery_kind == "edited"
                    and stream_outcome.visible_body_state == "none"
                    and not request.existing_event_is_placeholder
                ):
                    existing_visible_event_id = request.existing_event_id or streamed_event_id
                    if existing_visible_event_id is not None:
                        return FinalDeliveryOutcome(
                            terminal_status="error",
                            event_id=existing_visible_event_id,
                            is_visible_response=True,
                            failure_reason=stream_outcome.failure_reason or "stream_finalize_error",
                            tool_trace=tuple(request.tool_trace or ()),
                            extra_content=request.extra_content,
                        )
                failure_reason = stream_outcome.failure_reason or "stream_finalize_error"
                if stream_outcome.visible_body_state == "placeholder_only":
                    return await self._finalize_placeholder_only_stream_error(
                        request,
                        stream_outcome=stream_outcome,
                        failure_reason=failure_reason,
                    )

                visible_stream_event_id = stream_outcome.visible_event_id
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
                if request.existing_event_id is not None and not request.existing_event_is_placeholder:
                    return FinalDeliveryOutcome(
                        terminal_status="error",
                        event_id=request.existing_event_id,
                        is_visible_response=True,
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

            if stream_outcome.canonical_final_body_candidate is not None and stream_outcome.visible_body_state in {
                "none",
                "placeholder_only",
            }:
                existing_event_id = request.existing_event_id
                existing_event_is_placeholder = request.existing_event_is_placeholder
                if stream_outcome.visible_body_state == "placeholder_only":
                    existing_event_id = streamed_event_id
                    existing_event_is_placeholder = True
                return await self.deliver_final(
                    FinalDeliveryRequest(
                        target=request.target,
                        existing_event_id=existing_event_id,
                        existing_event_is_placeholder=existing_event_is_placeholder,
                        response_text=stream_outcome.canonical_final_body_candidate,
                        identity=request.identity,
                        tool_trace=request.tool_trace,
                        extra_content=request.extra_content,
                    ),
                )

            if stream_outcome.visible_body_state == "placeholder_only":
                return await self._cleanup_completed_placeholder_only_stream(
                    room_id=request.target.room_id,
                    streamed_event_id=streamed_event_id,
                    identity=request.identity,
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
                cancel_source=(
                    cancel_source_from_failure_reason(stream_outcome.failure_reason)
                    if stream_outcome.terminal_status == "cancelled"
                    else None
                ),
                failure_reason="stream_finalize_failed",
                tool_trace=tuple(request.tool_trace or ()),
                extra_content=request.extra_content,
            )
