"""Own visible Matrix delivery for already-generated responses."""

from __future__ import annotations

import asyncio
import typing
from copy import deepcopy
from dataclasses import dataclass
from html import escape as html_escape
from typing import TYPE_CHECKING, Any, Literal

from mindroom import constants, interactive
from mindroom.cancellation import CancelSource, cancel_failure_reason, is_cancelled_failure_reason
from mindroom.final_delivery import FinalDeliveryOutcome, FinalDeliveryState, StreamTransportOutcome
from mindroom.hooks import (
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
from mindroom.hooks.types import (
    EVENT_MESSAGE_AFTER_RESPONSE,
    EVENT_MESSAGE_BEFORE_RESPONSE,
    EVENT_MESSAGE_CANCELLED,
    EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM,
)
from mindroom.matrix.client_delivery import build_threaded_edit_content, edit_message_result, send_message_result
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_builder import build_message_content
from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001
from mindroom.streaming import (
    StreamDeliveryState,
    StreamingResponse,
    build_cancelled_response_update,
    send_streaming_response,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    import nio
    import structlog

    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.history.types import CompactionOutcome
    from mindroom.hooks import MessageEnvelope
    from mindroom.message_target import MessageTarget
    from mindroom.streaming_delivery import StreamInputChunk
    from mindroom.timing import DispatchPipelineTiming
    from mindroom.tool_system.events import ToolTraceEntry


@dataclass
class ResponseHookService:
    """Own response hook execution around final delivery."""

    hook_context: HookContextSupport

    async def apply_before_response(
        self,
        *,
        correlation_id: str,
        envelope: MessageEnvelope,
        response_text: str,
        response_kind: str,
        tool_trace: list[ToolTraceEntry] | None,
        extra_content: dict[str, Any] | None,
    ) -> ResponseDraft:
        """Run message:before_response hooks on one generated response."""
        draft = ResponseDraft(
            response_text=response_text,
            response_kind=response_kind,
            tool_trace=deepcopy(tool_trace) if tool_trace is not None else None,
            extra_content=deepcopy(extra_content) if extra_content is not None else None,
            envelope=envelope,
        )
        if not self.hook_context.registry.has_hooks(EVENT_MESSAGE_BEFORE_RESPONSE):
            return draft
        context = BeforeResponseContext(
            **self.hook_context.base_kwargs(EVENT_MESSAGE_BEFORE_RESPONSE, correlation_id),
            draft=draft,
        )
        return await emit_transform(self.hook_context.registry, EVENT_MESSAGE_BEFORE_RESPONSE, context)

    async def apply_final_response_transform(
        self,
        *,
        correlation_id: str,
        envelope: MessageEnvelope,
        response_text: str,
        response_kind: str,
    ) -> FinalResponseDraft:
        """Run message:final_response_transform hooks on one completed streamed response."""
        draft = FinalResponseDraft(
            response_text=response_text,
            response_kind=response_kind,
            envelope=envelope,
        )
        if not self.hook_context.registry.has_hooks(EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM):
            return draft

        context = FinalResponseTransformContext(
            **self.hook_context.base_kwargs(EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM, correlation_id),
            draft=draft,
        )
        return await emit_final_response_transform(
            self.hook_context.registry,
            EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM,
            context,
        )

    async def emit_after_response(
        self,
        *,
        correlation_id: str,
        envelope: MessageEnvelope,
        response_text: str,
        response_event_id: str,
        delivery_kind: Literal["sent", "edited"],
        response_kind: str,
        continue_on_cancelled: bool = False,
    ) -> None:
        """Emit message:after_response after the final send or edit succeeds."""
        if not self.hook_context.registry.has_hooks(EVENT_MESSAGE_AFTER_RESPONSE):
            return

        context = AfterResponseContext(
            **self.hook_context.base_kwargs(EVENT_MESSAGE_AFTER_RESPONSE, correlation_id),
            result=ResponseResult(
                response_text=response_text,
                response_event_id=response_event_id,
                delivery_kind=delivery_kind,
                response_kind=response_kind,
                envelope=envelope,
            ),
        )
        await emit(
            self.hook_context.registry,
            EVENT_MESSAGE_AFTER_RESPONSE,
            context,
            continue_on_cancelled=continue_on_cancelled,
        )

    async def emit_cancelled_response(
        self,
        *,
        correlation_id: str,
        envelope: MessageEnvelope,
        visible_response_event_id: str | None = None,
        response_kind: str = "ai",
        failure_reason: str | None = None,
    ) -> None:
        """Emit message:cancelled when final delivery does not complete cleanly."""
        if not self.hook_context.registry.has_hooks(EVENT_MESSAGE_CANCELLED):
            return

        context = CancelledResponseContext(
            **self.hook_context.base_kwargs(EVENT_MESSAGE_CANCELLED, correlation_id),
            info=CancelledResponseInfo(
                envelope=envelope,
                visible_response_event_id=visible_response_event_id,
                response_kind=response_kind,
                failure_reason=failure_reason,
            ),
        )
        await emit(self.hook_context.registry, EVENT_MESSAGE_CANCELLED, context)


def _classify_cancel_source(exc: asyncio.CancelledError) -> CancelSource:
    """Return the visible cancellation provenance for delivery-layer cancellations."""
    if len(exc.args) == 0:
        return "interrupted"
    if exc.args[0] == "user_stop":
        return "user_stop"
    if exc.args[0] == "sync_restart":
        return "sync_restart"
    return "interrupted"


@dataclass(frozen=True)
class SendTextRequest:
    """Parameters for one visible Matrix send."""

    target: MessageTarget
    response_text: str
    skip_mentions: bool = False
    tool_trace: list[ToolTraceEntry] | None = None
    extra_content: dict[str, Any] | None = None


@dataclass(frozen=True)
class EditTextRequest:
    """Parameters for one Matrix edit."""

    target: MessageTarget
    event_id: str
    new_text: str
    tool_trace: list[ToolTraceEntry] | None = None
    extra_content: dict[str, Any] | None = None


@dataclass(frozen=True)
class FinalDeliveryRequest:
    """Parameters for final hook-wrapped response delivery."""

    target: MessageTarget
    existing_event_id: str | None
    response_text: str
    response_kind: str
    response_envelope: MessageEnvelope
    correlation_id: str
    tool_trace: list[ToolTraceEntry] | None
    extra_content: dict[str, Any] | None
    existing_event_is_placeholder: bool = False
    apply_before_hooks: bool = True
    skip_mentions: bool = False
    emit_terminal_hooks: bool = True


@dataclass(frozen=True)
class CancelledVisibleNoteRequest:
    """Parameters for one terminal cancellation-note edit."""

    target: MessageTarget
    event_id: str
    existing_event_is_placeholder: bool
    cancel_source: CancelSource
    response_kind: str
    response_envelope: MessageEnvelope
    correlation_id: str


@dataclass(frozen=True)
class CompactionNoticeRequest:
    """Parameters for a compaction notice send."""

    target: MessageTarget
    main_response_event_id: str
    outcome: CompactionOutcome


@dataclass(frozen=True)
class StreamingDeliveryRequest:
    """Parameters for streamed Matrix delivery."""

    target: MessageTarget
    response_stream: AsyncIterator[StreamInputChunk]
    existing_event_id: str | None = None
    adopt_existing_placeholder: bool = False
    header: str | None = None
    show_tool_calls: bool = False
    extra_content: dict[str, Any] | None = None
    tool_trace_collector: list[ToolTraceEntry] | None = None
    stream_state: StreamDeliveryState | None = None
    streaming_cls: type[StreamingResponse] = StreamingResponse
    pipeline_timing: DispatchPipelineTiming | None = None
    visible_event_id_callback: Callable[[str], None] | None = None


@dataclass(frozen=True)
class DeliveryGatewayDeps:
    """Explicit dependencies needed for Matrix delivery."""

    runtime: SupportsClientConfig
    runtime_paths: RuntimePaths
    agent_name: str
    logger: structlog.stdlib.BoundLogger
    redact_message_event: Callable[..., Awaitable[bool]]
    sender_domain: str
    resolver: ConversationResolver
    response_hooks: ResponseHookService


@dataclass(frozen=True)
class FinalizeStreamedResponseRequest:
    """Parameters for finalizing one streamed Matrix response."""

    target: MessageTarget
    stream_transport_outcome: StreamTransportOutcome
    initial_delivery_kind: Literal["sent", "edited"]
    response_kind: str
    response_envelope: MessageEnvelope
    correlation_id: str
    tool_trace: list[ToolTraceEntry] | None
    extra_content: dict[str, Any] | None
    existing_event_id: str | None = None
    existing_event_is_placeholder: bool = False


@dataclass(frozen=True)
class LateStreamFinalizeFailureRequest:
    """Parameters for resolving a late streamed-finalization interruption."""

    target: MessageTarget
    stream_transport_outcome: StreamTransportOutcome
    response_kind: str
    response_envelope: MessageEnvelope
    correlation_id: str
    tool_trace: list[ToolTraceEntry] | None
    extra_content: dict[str, Any] | None
    failure_reason: str
    cancelled: bool
    existing_event_id: str | None = None
    existing_event_is_placeholder: bool = False


@dataclass(frozen=True)
class DeliveryGateway:
    """Send, edit, redact, and finalize visible Matrix responses."""

    deps: DeliveryGatewayDeps

    def _client(self) -> nio.AsyncClient:
        """Return the current Matrix client required for delivery."""
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for response delivery"
            raise RuntimeError(msg)
        return client

    async def _emit_after_response_best_effort(
        self,
        *,
        correlation_id: str,
        envelope: MessageEnvelope,
        response_text: str,
        response_event_id: str,
        delivery_kind: Literal["sent", "edited"],
        response_kind: str,
    ) -> None:
        """Best-effort after_response emission once delivery is already visible."""
        try:
            await self.deps.response_hooks.emit_after_response(
                correlation_id=correlation_id,
                envelope=envelope,
                response_text=response_text,
                response_event_id=response_event_id,
                delivery_kind=delivery_kind,
                response_kind=response_kind,
                continue_on_cancelled=True,
            )
        except asyncio.CancelledError:
            self.deps.logger.warning(
                "message:after_response cancelled after visible delivery; returning success",
                correlation_id=correlation_id,
                response_event_id=response_event_id,
                response_kind=response_kind,
                delivery_kind=delivery_kind,
            )
        except Exception:
            self.deps.logger.exception(
                "message:after_response failed after visible delivery; returning success",
                correlation_id=correlation_id,
                response_event_id=response_event_id,
                response_kind=response_kind,
                delivery_kind=delivery_kind,
            )

    async def _emit_cancelled_response_best_effort(
        self,
        *,
        correlation_id: str,
        envelope: MessageEnvelope,
        visible_response_event_id: str | None,
        response_kind: str,
        failure_reason: str | None,
    ) -> None:
        """Best-effort cancelled_response emission that never mutates terminal outcomes."""
        try:
            await self.deps.response_hooks.emit_cancelled_response(
                correlation_id=correlation_id,
                envelope=envelope,
                visible_response_event_id=visible_response_event_id,
                response_kind=response_kind,
                failure_reason=failure_reason,
            )
        except asyncio.CancelledError:
            self.deps.logger.warning(
                "message:cancelled cancelled after terminal outcome resolution; returning success",
                correlation_id=correlation_id,
                visible_response_event_id=visible_response_event_id,
                response_kind=response_kind,
            )
        except Exception:
            self.deps.logger.exception(
                "message:cancelled failed after terminal outcome resolution; returning success",
                correlation_id=correlation_id,
                visible_response_event_id=visible_response_event_id,
                response_kind=response_kind,
            )

    async def emit_terminal_outcome_hooks(
        self,
        *,
        outcome: FinalDeliveryOutcome,
        correlation_id: str,
        envelope: MessageEnvelope,
        response_kind: str,
    ) -> FinalDeliveryOutcome:
        """Emit the coordinator-owned terminal hooks for one canonical outcome."""
        if outcome.state == "final_visible_delivery":
            assert outcome.final_visible_event_id is not None
            assert outcome.final_visible_body is not None
            await self._emit_after_response_best_effort(
                correlation_id=correlation_id,
                envelope=envelope,
                response_text=outcome.final_visible_body,
                response_event_id=outcome.final_visible_event_id,
                delivery_kind=outcome.delivery_kind or "sent",
                response_kind=response_kind,
            )
            return outcome

        await self._emit_cancelled_response_best_effort(
            correlation_id=correlation_id,
            envelope=envelope,
            visible_response_event_id=outcome.visible_response_event_id,
            response_kind=response_kind,
            failure_reason=outcome.failure_reason,
        )
        return outcome

    def _cancelled_note_update(self, *, cancel_source: CancelSource) -> tuple[str, dict[str, str], str]:
        """Return the terminal note body, content metadata, and failure reason."""
        cancelled_text, stream_status = build_cancelled_response_update("", cancel_source=cancel_source)
        return (
            cancelled_text,
            {constants.STREAM_STATUS_KEY: stream_status},
            cancel_failure_reason(cancel_source),
        )

    def _current_stream_body(self, outcome: StreamTransportOutcome) -> str:
        """Return the current streamed body snapshot used for hook and outcome decisions."""
        return outcome.rendered_body or ""

    def _visible_stream_event_id(self, outcome: StreamTransportOutcome) -> str | None:
        """Return the streamed event id only when the stream showed real visible body text."""
        if outcome.visible_body_state != "visible_body":
            return None
        return outcome.last_physical_stream_event_id

    def _outcome(
        self,
        *,
        state: FinalDeliveryState,
        terminal_status: Literal["completed", "cancelled", "error"],
        final_visible_event_id: str | None = None,
        last_physical_stream_event_id: str | None = None,
        final_visible_body: str | None = None,
        delivery_kind: Literal["sent", "edited"] | None = None,
        failure_reason: str | None = None,
        tool_trace: list[ToolTraceEntry] | tuple[ToolTraceEntry, ...] | None = None,
        extra_content: dict[str, Any] | None = None,
        option_map: typing.Mapping[str, str] | None = None,
        options_list: typing.Sequence[typing.Mapping[str, str]] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build one canonical delivery outcome without per-state wrapper methods."""
        return FinalDeliveryOutcome(
            state=state,
            terminal_status=terminal_status,
            final_visible_event_id=final_visible_event_id,
            last_physical_stream_event_id=last_physical_stream_event_id,
            final_visible_body=final_visible_body,
            delivery_kind=delivery_kind,
            failure_reason=failure_reason,
            tool_trace=tuple(tool_trace or ()),
            extra_content=extra_content,
            option_map=dict(option_map) if option_map is not None else None,
            options_list=tuple(dict(item) for item in options_list) if options_list is not None else None,
        )

    @staticmethod
    def _cancelled_error_failure_reason(
        error: asyncio.CancelledError,
        *,
        fallback: str | None = None,
    ) -> str:
        """Normalize CancelledError values to the canonical cancellation reason strings."""
        reason = cancel_failure_reason(_classify_cancel_source(error))
        if reason:
            return reason
        normalized_fallback = (fallback or "").strip()
        return normalized_fallback or "interrupted"

    async def _cleanup_completed_placeholder_only_stream(
        self,
        *,
        room_id: str,
        streamed_event_id: str | None,
        response_kind: str,
        response_envelope: MessageEnvelope,
        correlation_id: str,
        failure_reason: str,
        tool_trace: list[ToolTraceEntry] | None,
        extra_content: dict[str, Any] | None,
    ) -> FinalDeliveryOutcome:
        """Remove a completed placeholder-only streamed event before returning no-visible-response."""
        if streamed_event_id is not None:
            cleanup_outcome = await self._redact_visible_response_event(
                room_id=room_id,
                event_id=streamed_event_id,
                response_kind=response_kind,
                response_envelope=response_envelope,
                correlation_id=correlation_id,
                redaction_reason="Completed placeholder-only streamed response",
                failure_reason=failure_reason,
                tool_trace=tool_trace,
                extra_content=extra_content,
            )
            if cleanup_outcome.state == "suppression_cleanup_failed":
                return cleanup_outcome
        return self._outcome(
            state="error_without_visible_response",
            terminal_status="error",
            failure_reason=failure_reason,
            tool_trace=tool_trace,
            extra_content=extra_content,
        )

    async def _redact_visible_response_event(
        self,
        *,
        room_id: str,
        event_id: str,
        response_kind: str,
        response_envelope: MessageEnvelope,
        correlation_id: str,
        redaction_reason: str,
        failure_reason: str | None = None,
        tool_trace: list[ToolTraceEntry] | None = None,
        extra_content: dict[str, Any] | None = None,
    ) -> FinalDeliveryOutcome:
        """Redact one visible response event and return the canonical suppression outcome."""
        self.deps.logger.warning(
            "Visible response was already delivered before suppression; attempting cleanup",
            response_kind=response_kind,
            source_event_id=response_envelope.source_event_id,
            correlation_id=correlation_id,
            visible_response_event_id=event_id,
        )
        try:
            redacted = await self.deps.redact_message_event(
                room_id=room_id,
                event_id=event_id,
                reason=redaction_reason,
            )
        except asyncio.CancelledError as error:
            return self._outcome(
                state="suppression_cleanup_failed",
                terminal_status="error",
                last_physical_stream_event_id=event_id,
                failure_reason=self._cancelled_error_failure_reason(error, fallback=failure_reason),
                tool_trace=tool_trace,
                extra_content=extra_content,
            )
        except Exception as error:
            self.deps.logger.exception(
                "Failed to redact visible response during cleanup",
                room_id=room_id,
                event_id=event_id,
                response_kind=response_kind,
                correlation_id=correlation_id,
            )
            return self._outcome(
                state="suppression_cleanup_failed",
                terminal_status="error",
                last_physical_stream_event_id=event_id,
                failure_reason=str(error) or failure_reason or f"failed to redact suppressed response {event_id}",
                tool_trace=tool_trace,
                extra_content=extra_content,
            )
        if not redacted:
            return self._outcome(
                state="suppression_cleanup_failed",
                terminal_status="error",
                last_physical_stream_event_id=event_id,
                failure_reason=failure_reason or f"failed to redact suppressed response {event_id}",
                tool_trace=tool_trace,
                extra_content=extra_content,
            )
        return self._outcome(
            state="suppressed_redacted",
            terminal_status="completed",
            last_physical_stream_event_id=event_id,
            failure_reason=failure_reason,
            tool_trace=tool_trace,
            extra_content=extra_content,
        )

    async def send_text(self, request: SendTextRequest) -> str | None:
        """Send one response message to a room."""
        client = self._client()
        config = self.deps.runtime.config
        resolved_target = request.target
        effective_thread_id = resolved_target.resolved_thread_id

        if effective_thread_id is None:
            content = format_message_with_mentions(
                config,
                self.deps.runtime_paths,
                request.response_text,
                sender_domain=self.deps.sender_domain,
                thread_event_id=None,
                reply_to_event_id=resolved_target.reply_to_event_id,
                latest_thread_event_id=None,
                tool_trace=request.tool_trace,
                extra_content=request.extra_content,
            )
        else:
            latest_thread_event_id = (
                await self.deps.resolver.deps.conversation_cache.get_latest_thread_event_id_if_needed(
                    resolved_target.room_id,
                    effective_thread_id,
                    resolved_target.reply_to_event_id,
                )
            )
            content = format_message_with_mentions(
                config,
                self.deps.runtime_paths,
                request.response_text,
                sender_domain=self.deps.sender_domain,
                thread_event_id=effective_thread_id,
                reply_to_event_id=resolved_target.reply_to_event_id,
                latest_thread_event_id=latest_thread_event_id,
                tool_trace=request.tool_trace,
                extra_content=request.extra_content,
            )
        if request.skip_mentions:
            content["com.mindroom.skip_mentions"] = True
        delivered = await send_message_result(client, resolved_target.room_id, content)
        if delivered is not None:
            self.deps.resolver.deps.conversation_cache.notify_outbound_message(
                resolved_target.room_id,
                delivered.event_id,
                delivered.content_sent,
            )
            self.deps.logger.info("Sent response", event_id=delivered.event_id, **resolved_target.log_context)
            return delivered.event_id
        self.deps.logger.error("Failed to send response to room", **resolved_target.log_context)
        return None

    async def edit_text(self, request: EditTextRequest) -> bool:
        """Edit one existing response message."""
        client = self._client()
        config = self.deps.runtime.config
        target = request.target
        if (
            config.get_entity_thread_mode(
                self.deps.agent_name,
                self.deps.runtime_paths,
                room_id=target.room_id,
            )
            == "room"
        ):
            content = format_message_with_mentions(
                config,
                self.deps.runtime_paths,
                request.new_text,
                sender_domain=self.deps.sender_domain,
                reply_to_event_id=target.reply_to_event_id,
                tool_trace=request.tool_trace,
                extra_content=request.extra_content,
            )
        else:
            latest_thread_event_id = (
                await self.deps.resolver.deps.conversation_cache.get_latest_thread_event_id_if_needed(
                    target.room_id,
                    target.resolved_thread_id,
                )
            )
            content = build_threaded_edit_content(
                new_text=request.new_text,
                thread_id=target.resolved_thread_id,
                config=config,
                runtime_paths=self.deps.runtime_paths,
                sender_domain=self.deps.sender_domain,
                tool_trace=request.tool_trace,
                extra_content=request.extra_content,
                latest_thread_event_id=latest_thread_event_id,
            )

        delivered = await edit_message_result(
            client,
            target.room_id,
            request.event_id,
            content,
            request.new_text,
        )
        if delivered is not None:
            self.deps.resolver.deps.conversation_cache.notify_outbound_message(
                target.room_id,
                delivered.event_id,
                delivered.content_sent,
            )
            self.deps.logger.info("Edited message", event_id=request.event_id, **target.log_context)
            return True
        self.deps.logger.error(
            "Failed to edit message",
            event_id=request.event_id,
            error="edit_message_result returned None",
            **target.log_context,
        )
        return False

    async def deliver_final(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self,
        request: FinalDeliveryRequest,
    ) -> FinalDeliveryOutcome:
        """Apply before/after hooks around one final send or edit."""

        async def before_response_failure_outcome(
            *,
            cancelled: bool,
            failure_reason: str,
        ) -> FinalDeliveryOutcome:
            outcome: FinalDeliveryOutcome
            if request.existing_event_id is not None:
                if request.existing_event_is_placeholder:
                    cleanup_outcome = await self._redact_visible_response_event(
                        room_id=request.target.room_id,
                        event_id=request.existing_event_id,
                        response_kind=request.response_kind,
                        response_envelope=request.response_envelope,
                        correlation_id=request.correlation_id,
                        redaction_reason=(
                            "Cancelled placeholder response"
                            if cancelled
                            else "Failed placeholder response before delivery"
                        ),
                        failure_reason=failure_reason,
                        tool_trace=request.tool_trace,
                        extra_content=request.extra_content,
                    )
                    if cleanup_outcome.state == "suppression_cleanup_failed":
                        outcome = cleanup_outcome
                    elif cancelled:
                        outcome = self._outcome(
                            state="cancelled_without_visible_response",
                            terminal_status="cancelled",
                            failure_reason=failure_reason,
                            tool_trace=request.tool_trace,
                            extra_content=request.extra_content,
                        )
                    else:
                        outcome = self._outcome(
                            state="error_without_visible_response",
                            terminal_status="error",
                            failure_reason=failure_reason,
                            tool_trace=request.tool_trace,
                            extra_content=request.extra_content,
                        )
                elif cancelled:
                    outcome = self._outcome(
                        state="cancelled_with_visible_response",
                        terminal_status="cancelled",
                        final_visible_event_id=request.existing_event_id,
                        failure_reason=failure_reason,
                        tool_trace=request.tool_trace,
                        extra_content=request.extra_content,
                    )
                else:
                    outcome = self._outcome(
                        state="kept_prior_visible_response_after_error",
                        terminal_status="error",
                        final_visible_event_id=request.existing_event_id,
                        failure_reason=failure_reason,
                        tool_trace=request.tool_trace,
                        extra_content=request.extra_content,
                    )
            elif cancelled:
                outcome = self._outcome(
                    state="cancelled_without_visible_response",
                    terminal_status="cancelled",
                    failure_reason=failure_reason,
                    tool_trace=request.tool_trace,
                    extra_content=request.extra_content,
                )
            else:
                outcome = self._outcome(
                    state="error_without_visible_response",
                    terminal_status="error",
                    failure_reason=failure_reason,
                    tool_trace=request.tool_trace,
                    extra_content=request.extra_content,
                )
            return outcome

        try:
            draft = (
                await self.deps.response_hooks.apply_before_response(
                    correlation_id=request.correlation_id,
                    envelope=request.response_envelope,
                    response_text=request.response_text,
                    response_kind=request.response_kind,
                    tool_trace=request.tool_trace,
                    extra_content=request.extra_content,
                )
                if request.apply_before_hooks
                else ResponseDraft(
                    response_text=request.response_text,
                    response_kind=request.response_kind,
                    tool_trace=request.tool_trace,
                    extra_content=request.extra_content,
                    envelope=request.response_envelope,
                )
            )
        except asyncio.CancelledError as error:
            outcome = await before_response_failure_outcome(
                cancelled=True,
                failure_reason=self._cancelled_error_failure_reason(error),
            )
            if request.emit_terminal_hooks:
                return await self.emit_terminal_outcome_hooks(
                    outcome=outcome,
                    correlation_id=request.correlation_id,
                    envelope=request.response_envelope,
                    response_kind=request.response_kind,
                )
            return outcome
        except Exception as error:
            outcome = await before_response_failure_outcome(
                cancelled=False,
                failure_reason=str(error),
            )
            if request.emit_terminal_hooks:
                return await self.emit_terminal_outcome_hooks(
                    outcome=outcome,
                    correlation_id=request.correlation_id,
                    envelope=request.response_envelope,
                    response_kind=request.response_kind,
                )
            return outcome
        if draft.suppress:
            self.deps.logger.info(
                "Response suppressed by hook",
                response_kind=request.response_kind,
                source_event_id=request.response_envelope.source_event_id,
                correlation_id=request.correlation_id,
            )
            if request.existing_event_id is not None and request.existing_event_is_placeholder:
                outcome = await self._redact_visible_response_event(
                    room_id=request.target.room_id,
                    event_id=request.existing_event_id,
                    response_kind=request.response_kind,
                    response_envelope=request.response_envelope,
                    correlation_id=request.correlation_id,
                    redaction_reason="Suppressed placeholder response",
                    failure_reason="suppressed_by_hook",
                    tool_trace=draft.tool_trace,
                    extra_content=draft.extra_content,
                )
            elif request.existing_event_id is not None:
                outcome = self._outcome(
                    state="kept_prior_visible_response_after_suppression",
                    terminal_status="completed",
                    final_visible_event_id=request.existing_event_id,
                    failure_reason="suppressed_by_hook",
                    tool_trace=draft.tool_trace,
                    extra_content=draft.extra_content,
                )
            else:
                outcome = self._outcome(
                    state="suppressed_without_visible_response",
                    terminal_status="completed",
                    failure_reason="suppressed_by_hook",
                    tool_trace=draft.tool_trace,
                    extra_content=draft.extra_content,
                )
            if request.emit_terminal_hooks:
                return await self.emit_terminal_outcome_hooks(
                    outcome=outcome,
                    correlation_id=request.correlation_id,
                    envelope=request.response_envelope,
                    response_kind=request.response_kind,
                )
            return outcome

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
                ),
            )
            if edited:
                outcome = self._outcome(
                    state="final_visible_delivery",
                    terminal_status="completed",
                    final_visible_event_id=request.existing_event_id,
                    last_physical_stream_event_id=(
                        request.existing_event_id if request.existing_event_is_placeholder else None
                    ),
                    final_visible_body=display_text,
                    delivery_kind="edited",
                    tool_trace=draft.tool_trace,
                    extra_content=draft.extra_content,
                    option_map=interactive_response.option_map,
                    options_list=interactive_response.options_list,
                )
                if request.emit_terminal_hooks:
                    await self._emit_after_response_best_effort(
                        correlation_id=request.correlation_id,
                        envelope=request.response_envelope,
                        response_text=display_text,
                        response_event_id=request.existing_event_id,
                        delivery_kind="edited",
                        response_kind=request.response_kind,
                    )
                return outcome

            if request.existing_event_is_placeholder:
                cleanup_outcome = await self._redact_visible_response_event(
                    room_id=request.target.room_id,
                    event_id=request.existing_event_id,
                    response_kind=request.response_kind,
                    response_envelope=request.response_envelope,
                    correlation_id=request.correlation_id,
                    redaction_reason="Failed placeholder response",
                    failure_reason="delivery_failed",
                    tool_trace=draft.tool_trace,
                    extra_content=draft.extra_content,
                )
                if cleanup_outcome.state == "suppression_cleanup_failed":
                    outcome = cleanup_outcome
                else:
                    outcome = self._outcome(
                        state="error_without_visible_response",
                        terminal_status="error",
                        failure_reason="delivery_failed",
                        tool_trace=draft.tool_trace,
                        extra_content=draft.extra_content,
                    )
            else:
                outcome = self._outcome(
                    state="kept_prior_visible_response_after_error",
                    terminal_status="error",
                    final_visible_event_id=request.existing_event_id,
                    failure_reason="delivery_failed",
                    tool_trace=draft.tool_trace,
                    extra_content=draft.extra_content,
                )
        else:
            event_id = await self.send_text(
                SendTextRequest(
                    target=request.target,
                    response_text=display_text,
                    skip_mentions=request.skip_mentions,
                    tool_trace=draft.tool_trace,
                    extra_content=draft.extra_content,
                ),
            )
            if event_id is None:
                outcome = self._outcome(
                    state="error_without_visible_response",
                    terminal_status="error",
                    failure_reason="delivery_failed",
                    tool_trace=draft.tool_trace,
                    extra_content=draft.extra_content,
                )
            else:
                outcome = self._outcome(
                    state="final_visible_delivery",
                    terminal_status="completed",
                    final_visible_event_id=event_id,
                    final_visible_body=display_text,
                    delivery_kind="sent",
                    tool_trace=draft.tool_trace,
                    extra_content=draft.extra_content,
                    option_map=interactive_response.option_map,
                    options_list=interactive_response.options_list,
                )
                if request.emit_terminal_hooks:
                    await self._emit_after_response_best_effort(
                        correlation_id=request.correlation_id,
                        envelope=request.response_envelope,
                        response_text=display_text,
                        response_event_id=event_id,
                        delivery_kind="sent",
                        response_kind=request.response_kind,
                    )
                return outcome
        if request.emit_terminal_hooks and not outcome.emits_after_response:
            return await self.emit_terminal_outcome_hooks(
                outcome=outcome,
                correlation_id=request.correlation_id,
                envelope=request.response_envelope,
                response_kind=request.response_kind,
            )
        return outcome

    async def deliver_cancelled_visible_note(
        self,
        request: CancelledVisibleNoteRequest,
    ) -> FinalDeliveryOutcome:
        """Edit the in-flight visible response into a terminal cancellation note."""
        cancelled_text, extra_content, failure_reason = self._cancelled_note_update(cancel_source=request.cancel_source)
        if not request.existing_event_is_placeholder:
            return await self.emit_terminal_outcome_hooks(
                outcome=self._outcome(
                    state="cancelled_with_visible_response",
                    terminal_status="cancelled",
                    final_visible_event_id=request.event_id,
                    failure_reason=failure_reason,
                    extra_content=extra_content,
                ),
                correlation_id=request.correlation_id,
                envelope=request.response_envelope,
                response_kind=request.response_kind,
            )
        edited = await self.edit_text(
            EditTextRequest(
                target=request.target,
                event_id=request.event_id,
                new_text=cancelled_text,
                extra_content=extra_content,
            ),
        )
        if edited:
            outcome = self._outcome(
                state="cancelled_with_visible_note",
                terminal_status="cancelled",
                final_visible_event_id=request.event_id,
                final_visible_body=cancelled_text,
                last_physical_stream_event_id=request.event_id,
                delivery_kind="edited",
                failure_reason=failure_reason,
                extra_content=extra_content,
            )
        else:
            cleanup_outcome = await self._redact_visible_response_event(
                room_id=request.target.room_id,
                event_id=request.event_id,
                response_kind=request.response_kind,
                response_envelope=request.response_envelope,
                correlation_id=request.correlation_id,
                redaction_reason="Failed cancelled placeholder response",
                failure_reason=failure_reason,
                extra_content=extra_content,
            )
            outcome = (
                cleanup_outcome
                if cleanup_outcome.state == "suppression_cleanup_failed"
                else self._outcome(
                    state="cancelled_without_visible_response",
                    terminal_status="cancelled",
                    failure_reason=failure_reason,
                    extra_content=extra_content,
                )
            )
        return await self.emit_terminal_outcome_hooks(
            outcome=outcome,
            correlation_id=request.correlation_id,
            envelope=request.response_envelope,
            response_kind=request.response_kind,
        )

    async def send_compaction_notice(self, request: CompactionNoticeRequest) -> str | None:
        """Send one compaction notice without mention parsing side effects."""
        client = self._client()
        summary_line = request.outcome.format_notice()
        formatted_body = f"<em>{html_escape(summary_line).replace(chr(10), '<br/>')}</em>"
        target = request.target
        effective_thread_id = target.resolved_thread_id
        content = build_message_content(
            summary_line,
            formatted_body=formatted_body,
            thread_event_id=effective_thread_id,
            reply_to_event_id=request.main_response_event_id,
            extra_content={
                "msgtype": "m.notice",
                constants.COMPACTION_NOTICE_CONTENT_KEY: request.outcome.to_notice_metadata(),
                "com.mindroom.skip_mentions": True,
            },
        )
        delivered = await send_message_result(client, target.room_id, content)
        if delivered is not None:
            self.deps.resolver.deps.conversation_cache.notify_outbound_message(
                target.room_id,
                delivered.event_id,
                delivered.content_sent,
            )
            self.deps.logger.info(
                "Sent compaction notice",
                event_id=delivered.event_id,
                **target.log_context,
                summary_model=request.outcome.summary_model,
            )
            return delivered.event_id
        self.deps.logger.error("Failed to send compaction notice", **target.log_context)
        return None

    async def deliver_stream(
        self,
        request: StreamingDeliveryRequest,
    ) -> StreamTransportOutcome:
        """Send one streaming Matrix response."""
        client = self._client()
        config = self.deps.runtime.config
        latest_thread_event_id = await self.deps.resolver.deps.conversation_cache.get_latest_thread_event_id_if_needed(
            request.target.room_id,
            request.target.resolved_thread_id,
            request.target.reply_to_event_id,
            request.existing_event_id,
        )
        return await send_streaming_response(
            client,
            request.target.room_id,
            request.target.reply_to_event_id,
            request.target.resolved_thread_id,
            self.deps.sender_domain,
            config,
            self.deps.runtime_paths,
            request.response_stream,
            streaming_cls=request.streaming_cls,
            header=request.header,
            show_tool_calls=request.show_tool_calls,
            existing_event_id=request.existing_event_id,
            adopt_existing_placeholder=request.adopt_existing_placeholder,
            target=request.target,
            room_mode=request.target.is_room_mode,
            extra_content=request.extra_content,
            tool_trace_collector=request.tool_trace_collector,
            stream_state=request.stream_state,
            pipeline_timing=request.pipeline_timing,
            visible_event_id_callback=request.visible_event_id_callback,
            latest_thread_event_id=latest_thread_event_id,
            conversation_cache=self.deps.resolver.deps.conversation_cache,
        )

    async def finalize_streamed_response(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self,
        request: FinalizeStreamedResponseRequest,
    ) -> FinalDeliveryOutcome:
        """Apply hooks and any final edit needed after streamed delivery completes."""
        stream_outcome = request.stream_transport_outcome
        streamed_event_id = stream_outcome.last_physical_stream_event_id
        visible_stream_event_id = self._visible_stream_event_id(stream_outcome)
        streamed_text = self._current_stream_body(stream_outcome)
        final_body_candidate = stream_outcome.canonical_final_body_candidate or streamed_text
        cancelled_stream_outcome = stream_outcome.terminal_status == "cancelled" or is_cancelled_failure_reason(
            stream_outcome.failure_reason,
        )

        if cancelled_stream_outcome:
            if (
                request.initial_delivery_kind == "edited"
                and stream_outcome.visible_body_state == "none"
                and not request.existing_event_is_placeholder
            ):
                existing_visible_event_id = request.existing_event_id or streamed_event_id
                if existing_visible_event_id is not None:
                    return await self.emit_terminal_outcome_hooks(
                        outcome=self._outcome(
                            state="cancelled_with_visible_response",
                            terminal_status="cancelled",
                            final_visible_event_id=existing_visible_event_id,
                            failure_reason=stream_outcome.failure_reason or "stream_finalize_cancelled",
                            tool_trace=request.tool_trace,
                            extra_content=request.extra_content,
                        ),
                        correlation_id=request.correlation_id,
                        envelope=request.response_envelope,
                        response_kind=request.response_kind,
                    )
            return await self.resolve_late_stream_finalize_failure(
                LateStreamFinalizeFailureRequest(
                    target=request.target,
                    stream_transport_outcome=stream_outcome,
                    response_kind=request.response_kind,
                    response_envelope=request.response_envelope,
                    correlation_id=request.correlation_id,
                    tool_trace=request.tool_trace,
                    extra_content=request.extra_content,
                    failure_reason=stream_outcome.failure_reason or "stream_finalize_cancelled",
                    cancelled=True,
                    existing_event_id=request.existing_event_id,
                    existing_event_is_placeholder=request.existing_event_is_placeholder,
                ),
            )

        if stream_outcome.terminal_status == "error":
            if (
                request.initial_delivery_kind == "edited"
                and stream_outcome.visible_body_state == "none"
                and not request.existing_event_is_placeholder
            ):
                existing_visible_event_id = request.existing_event_id or streamed_event_id
                if existing_visible_event_id is not None:
                    return await self.emit_terminal_outcome_hooks(
                        outcome=self._outcome(
                            state="kept_prior_visible_response_after_error",
                            terminal_status="error",
                            final_visible_event_id=existing_visible_event_id,
                            failure_reason=stream_outcome.failure_reason or "stream_finalize_error",
                            tool_trace=request.tool_trace,
                            extra_content=request.extra_content,
                        ),
                        correlation_id=request.correlation_id,
                        envelope=request.response_envelope,
                        response_kind=request.response_kind,
                    )
            return await self.resolve_late_stream_finalize_failure(
                LateStreamFinalizeFailureRequest(
                    target=request.target,
                    stream_transport_outcome=stream_outcome,
                    response_kind=request.response_kind,
                    response_envelope=request.response_envelope,
                    correlation_id=request.correlation_id,
                    tool_trace=request.tool_trace,
                    extra_content=request.extra_content,
                    failure_reason=stream_outcome.failure_reason or "stream_finalize_error",
                    cancelled=False,
                    existing_event_id=request.existing_event_id,
                    existing_event_is_placeholder=request.existing_event_is_placeholder,
                ),
            )

        if stream_outcome.terminal_result != "succeeded":
            failure_reason = stream_outcome.failure_reason or "terminal_update_failed"
            if stream_outcome.visible_body_state == "placeholder_only":
                outcome = await self._cleanup_completed_placeholder_only_stream(
                    room_id=request.target.room_id,
                    streamed_event_id=streamed_event_id,
                    response_kind=request.response_kind,
                    response_envelope=request.response_envelope,
                    correlation_id=request.correlation_id,
                    failure_reason=failure_reason,
                    tool_trace=request.tool_trace,
                    extra_content=request.extra_content,
                )
            elif (
                request.initial_delivery_kind == "edited"
                and streamed_event_id is not None
                and visible_stream_event_id is None
            ):
                outcome = self._outcome(
                    state="kept_prior_visible_response_after_error",
                    terminal_status="error",
                    final_visible_event_id=streamed_event_id,
                    failure_reason=failure_reason,
                    tool_trace=request.tool_trace,
                    extra_content=request.extra_content,
                )
            elif visible_stream_event_id is not None:
                return await self.emit_terminal_outcome_hooks(
                    outcome=self._outcome(
                        state="kept_prior_visible_stream_after_completed_terminal_failure",
                        terminal_status="completed",
                        last_physical_stream_event_id=visible_stream_event_id,
                        final_visible_body=streamed_text or None,
                        failure_reason=failure_reason,
                        tool_trace=request.tool_trace,
                        extra_content=request.extra_content,
                        option_map=stream_outcome.option_map,
                        options_list=stream_outcome.options_list,
                    ),
                    correlation_id=request.correlation_id,
                    envelope=request.response_envelope,
                    response_kind=request.response_kind,
                )
            else:
                outcome = self._outcome(
                    state="error_without_visible_response",
                    terminal_status="error",
                    failure_reason=failure_reason,
                    tool_trace=request.tool_trace,
                    extra_content=request.extra_content,
                )
            return await self.emit_terminal_outcome_hooks(
                outcome=outcome,
                correlation_id=request.correlation_id,
                envelope=request.response_envelope,
                response_kind=request.response_kind,
            )

        if stream_outcome.visible_body_state == "placeholder_only":
            outcome = await self._cleanup_completed_placeholder_only_stream(
                room_id=request.target.room_id,
                streamed_event_id=streamed_event_id,
                response_kind=request.response_kind,
                response_envelope=request.response_envelope,
                correlation_id=request.correlation_id,
                failure_reason=stream_outcome.failure_reason or "stream_completed_without_visible_body",
                tool_trace=request.tool_trace,
                extra_content=request.extra_content,
            )
            return await self.emit_terminal_outcome_hooks(
                outcome=outcome,
                correlation_id=request.correlation_id,
                envelope=request.response_envelope,
                response_kind=request.response_kind,
            )

        if stream_outcome.visible_body_state != "visible_body":
            if (
                request.initial_delivery_kind == "edited"
                and not request.existing_event_is_placeholder
                and stream_outcome.visible_body_state == "none"
            ):
                existing_visible_event_id = request.existing_event_id or streamed_event_id
                outcome = (
                    self._outcome(
                        state="kept_prior_visible_response_after_error",
                        terminal_status="error",
                        final_visible_event_id=existing_visible_event_id,
                        failure_reason=stream_outcome.failure_reason or "stream_completed_without_visible_body",
                        tool_trace=request.tool_trace,
                        extra_content=request.extra_content,
                    )
                    if existing_visible_event_id is not None
                    else self._outcome(
                        state="error_without_visible_response",
                        terminal_status="error",
                        failure_reason=stream_outcome.failure_reason or "stream_completed_without_visible_body",
                        tool_trace=request.tool_trace,
                        extra_content=request.extra_content,
                    )
                )
            else:
                outcome = self._outcome(
                    state="error_without_visible_response",
                    terminal_status="error",
                    failure_reason=stream_outcome.failure_reason or "stream_completed_without_visible_body",
                    tool_trace=request.tool_trace,
                    extra_content=request.extra_content,
                )
            return await self.emit_terminal_outcome_hooks(
                outcome=outcome,
                correlation_id=request.correlation_id,
                envelope=request.response_envelope,
                response_kind=request.response_kind,
            )
        try:
            final_transform_draft = await self.deps.response_hooks.apply_final_response_transform(
                correlation_id=request.correlation_id,
                envelope=request.response_envelope,
                response_text=final_body_candidate,
                response_kind=request.response_kind,
            )
            if final_transform_draft.response_text not in {final_body_candidate, streamed_text}:
                try:
                    final_outcome = await self.deliver_final(
                        FinalDeliveryRequest(
                            target=request.target,
                            existing_event_id=streamed_event_id,
                            response_text=final_transform_draft.response_text,
                            response_kind=request.response_kind,
                            response_envelope=request.response_envelope,
                            correlation_id=request.correlation_id,
                            tool_trace=request.tool_trace,
                            extra_content=request.extra_content,
                            apply_before_hooks=False,
                            existing_event_is_placeholder=False,
                            emit_terminal_hooks=False,
                        ),
                    )
                except asyncio.CancelledError:
                    self.deps.logger.warning(
                        "Final streamed-response transform edit cancelled; preserving streamed success",
                        correlation_id=request.correlation_id,
                    )
                except Exception:
                    self.deps.logger.exception(
                        "Final streamed-response transform edit failed; preserving streamed success",
                        correlation_id=request.correlation_id,
                    )
                else:
                    if final_outcome.has_final_visible_delivery:
                        assert final_outcome.final_visible_event_id is not None
                        assert final_outcome.final_visible_body is not None
                        assert final_outcome.delivery_kind is not None
                        await self._emit_after_response_best_effort(
                            correlation_id=request.correlation_id,
                            envelope=request.response_envelope,
                            response_text=final_outcome.final_visible_body,
                            response_event_id=final_outcome.final_visible_event_id,
                            delivery_kind=final_outcome.delivery_kind,
                            response_kind=request.response_kind,
                        )
                        return final_outcome
        except asyncio.CancelledError:
            self.deps.logger.warning(
                "Final streamed-response transform cancelled; preserving streamed success",
                correlation_id=request.correlation_id,
            )
        except Exception:
            self.deps.logger.exception(
                "Final streamed-response transform failed; preserving streamed success",
                correlation_id=request.correlation_id,
            )

        assert streamed_event_id is not None
        interactive_response = interactive.parse_and_format_interactive(streamed_text, extract_mapping=True)
        outcome = self._outcome(
            state="final_visible_delivery",
            terminal_status="completed",
            final_visible_event_id=streamed_event_id,
            last_physical_stream_event_id=streamed_event_id,
            final_visible_body=interactive_response.formatted_text,
            delivery_kind=request.initial_delivery_kind,
            tool_trace=request.tool_trace,
            extra_content=request.extra_content,
            option_map=stream_outcome.option_map or interactive_response.option_map,
            options_list=stream_outcome.options_list or interactive_response.options_list,
        )
        await self._emit_after_response_best_effort(
            correlation_id=request.correlation_id,
            envelope=request.response_envelope,
            response_text=interactive_response.formatted_text,
            response_event_id=streamed_event_id,
            delivery_kind=request.initial_delivery_kind,
            response_kind=request.response_kind,
        )
        return outcome

    async def resolve_late_stream_finalize_failure(
        self,
        request: LateStreamFinalizeFailureRequest,
    ) -> FinalDeliveryOutcome:
        """Resolve one raw cancellation/error raised after streaming already completed."""
        stream_outcome = request.stream_transport_outcome
        if stream_outcome.visible_body_state == "placeholder_only":
            cleanup_outcome = await self._cleanup_completed_placeholder_only_stream(
                room_id=request.target.room_id,
                streamed_event_id=stream_outcome.last_physical_stream_event_id,
                response_kind=request.response_kind,
                response_envelope=request.response_envelope,
                correlation_id=request.correlation_id,
                failure_reason=request.failure_reason,
                tool_trace=request.tool_trace,
                extra_content=request.extra_content,
            )
            if cleanup_outcome.state != "suppression_cleanup_failed":
                cleanup_outcome = self._outcome(
                    state="cancelled_without_visible_response"
                    if request.cancelled
                    else "error_without_visible_response",
                    terminal_status="cancelled" if request.cancelled else "error",
                    failure_reason=request.failure_reason,
                    tool_trace=request.tool_trace,
                    extra_content=request.extra_content,
                )
            return await self.emit_terminal_outcome_hooks(
                outcome=cleanup_outcome,
                correlation_id=request.correlation_id,
                envelope=request.response_envelope,
                response_kind=request.response_kind,
            )

        visible_stream_event_id = self._visible_stream_event_id(stream_outcome)
        streamed_text = self._current_stream_body(stream_outcome)
        if visible_stream_event_id is not None:
            outcome = self._outcome(
                state="kept_prior_visible_stream_after_cancel"
                if request.cancelled
                else "kept_prior_visible_stream_after_error",
                terminal_status="cancelled" if request.cancelled else "error",
                last_physical_stream_event_id=visible_stream_event_id,
                final_visible_body=streamed_text or None,
                failure_reason=request.failure_reason,
                tool_trace=request.tool_trace,
                extra_content=request.extra_content,
                option_map=stream_outcome.option_map,
                options_list=stream_outcome.options_list,
            )
        elif request.existing_event_id is not None and not request.existing_event_is_placeholder:
            outcome = self._outcome(
                state="cancelled_with_visible_response"
                if request.cancelled
                else "kept_prior_visible_response_after_error",
                terminal_status="cancelled" if request.cancelled else "error",
                final_visible_event_id=request.existing_event_id,
                failure_reason=request.failure_reason,
                tool_trace=request.tool_trace,
                extra_content=request.extra_content,
            )
        else:
            outcome = self._outcome(
                state="cancelled_without_visible_response" if request.cancelled else "error_without_visible_response",
                terminal_status="cancelled" if request.cancelled else "error",
                failure_reason=request.failure_reason,
                tool_trace=request.tool_trace,
                extra_content=request.extra_content,
            )
        return await self.emit_terminal_outcome_hooks(
            outcome=outcome,
            correlation_id=request.correlation_id,
            envelope=request.response_envelope,
            response_kind=request.response_kind,
        )
