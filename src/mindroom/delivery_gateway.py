"""Own visible Matrix delivery for already-generated responses."""

from __future__ import annotations

import asyncio
import typing
from copy import deepcopy
from dataclasses import dataclass
from html import escape as html_escape
from typing import TYPE_CHECKING, Any, Literal

from mindroom import constants, interactive
from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome
from mindroom.hooks import (
    AfterResponseContext,
    BeforeResponseContext,
    CancelledResponseContext,
    CancelledResponseInfo,
    HookContextSupport,
    ResponseDraft,
    ResponseResult,
    emit,
    emit_transform,
)
from mindroom.hooks.types import (
    EVENT_MESSAGE_AFTER_RESPONSE,
    EVENT_MESSAGE_BEFORE_RESPONSE,
    EVENT_MESSAGE_CANCELLED,
)
from mindroom.matrix.client_delivery import build_threaded_edit_content, edit_message_result, send_message_result
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_builder import build_message_content
from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001
from mindroom.streaming import (
    StreamDeliveryState,
    StreamingResponse,
    build_restart_interrupted_body,
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
    from mindroom.streaming import _StreamInputChunk
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
        """Emit message:cancelled when a response never reaches final delivery."""
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


@dataclass(frozen=True)
class DeliveryResult:
    """Final send or edit outcome for one generated response."""

    event_id: str | None
    response_text: str
    delivery_kind: Literal["sent", "edited"] | None
    suppressed: bool = False
    option_map: dict[str, str] | None = None
    options_list: list[dict[str, str]] | None = None
    failure_reason: str | None = None


def _is_cancelled_failure_reason(failure_reason: str | None) -> bool:
    """Return whether one fallback failure reason represents cancellation."""
    if failure_reason is None:
        return False
    return "cancel" in failure_reason.lower()


def _late_failure_without_visible(*, cancelled: bool, reason: str) -> FinalDeliveryOutcome:
    if cancelled:
        return FinalDeliveryOutcome.cancelled_without_visible_response(failure_reason=reason)
    return FinalDeliveryOutcome.error_without_visible_response(failure_reason=reason)


def _late_failure_with_preserved_stream(
    *,
    cancelled: bool,
    event_id: str,
    reason: str,
) -> FinalDeliveryOutcome:
    if cancelled:
        return FinalDeliveryOutcome.keep_prior_visible_stream_after_cancel(
            last_physical_stream_event_id=event_id,
            final_visible_body=None,
            failure_reason=reason,
        )
    return FinalDeliveryOutcome.keep_prior_visible_stream_after_error(
        last_physical_stream_event_id=event_id,
        final_visible_body=None,
        failure_reason=reason,
    )


def _late_failure_with_visible_response(
    *,
    cancelled: bool,
    event_id: str,
    reason: str,
) -> FinalDeliveryOutcome:
    if cancelled:
        return FinalDeliveryOutcome.cancelled_with_visible_response(
            final_visible_event_id=event_id,
            failure_reason=reason,
        )
    return FinalDeliveryOutcome.error_with_visible_response(
        final_visible_event_id=event_id,
        final_visible_body="",
        failure_reason=reason,
    )


def _late_delivery_failure_outcome(
    *,
    tracked_event_id: str | None,
    tracked_event_was_visible: bool,
    existing_event_id: str | None,
    existing_event_is_placeholder: bool,
    placeholder_event_id: str | None,
    failure_reason: str | None,
) -> FinalDeliveryOutcome:
    """Map raw late-failure transport facts into one canonical terminal outcome."""
    cancelled = _is_cancelled_failure_reason(failure_reason)

    if tracked_event_id is not None and tracked_event_id == placeholder_event_id:
        return _late_failure_without_visible(
            cancelled=cancelled,
            reason=(
                failure_reason
                or (
                    "delivery_result_missing_after_placeholder_cancel"
                    if cancelled
                    else "delivery_result_missing_after_placeholder"
                )
            ),
        )
    if (
        tracked_event_id is not None
        and existing_event_id is not None
        and existing_event_is_placeholder
        and tracked_event_id == existing_event_id
    ):
        if tracked_event_was_visible:
            return _late_failure_with_preserved_stream(
                cancelled=cancelled,
                event_id=tracked_event_id,
                reason=(
                    failure_reason
                    or (
                        "delivery_result_missing_after_visible_placeholder_stream_cancel"
                        if cancelled
                        else "delivery_result_missing_after_visible_placeholder_stream"
                    )
                ),
            )
        return _late_failure_without_visible(
            cancelled=cancelled,
            reason=(
                failure_reason
                or (
                    "delivery_result_missing_after_visible_placeholder_cancel"
                    if cancelled
                    else "delivery_result_missing_after_visible_placeholder"
                )
            ),
        )
    if tracked_event_id is not None and (existing_event_id is None or tracked_event_id != existing_event_id):
        return _late_failure_with_preserved_stream(
            cancelled=cancelled,
            event_id=tracked_event_id,
            reason=failure_reason
            or (
                "delivery_result_missing_after_visible_stream_cancel"
                if cancelled
                else "delivery_result_missing_after_visible_stream"
            ),
        )
    if existing_event_id is not None and not existing_event_is_placeholder:
        return _late_failure_with_visible_response(
            cancelled=cancelled,
            event_id=existing_event_id,
            reason=failure_reason
            or (
                "delivery_result_missing_after_visible_response_cancel"
                if cancelled
                else "delivery_result_missing_after_visible_response"
            ),
        )
    return _late_failure_without_visible(
        cancelled=cancelled,
        reason=failure_reason or ("delivery_result_missing_cancelled" if cancelled else "delivery_result_missing"),
    )


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
    restart: bool
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
    response_stream: AsyncIterator[_StreamInputChunk]
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

    def _cancelled_note_update(self, *, restart: bool) -> tuple[str, dict[str, str], str]:
        """Return the terminal note body, content metadata, and failure reason."""
        if restart:
            return (
                build_restart_interrupted_body(""),
                {constants.STREAM_STATUS_KEY: constants.STREAM_STATUS_ERROR},
                "sync_restart_cancelled",
            )
        return (
            "**[Response cancelled by user]**",
            {constants.STREAM_STATUS_KEY: constants.STREAM_STATUS_CANCELLED},
            "cancelled_by_user",
        )

    def _current_stream_body(self, outcome: StreamTransportOutcome) -> str:
        """Return the current streamed body snapshot used for hook and outcome decisions."""
        return outcome.rendered_body or ""

    def _visible_stream_event_id(self, outcome: StreamTransportOutcome) -> str | None:
        """Return the streamed event id only when the stream showed real visible body text."""
        if outcome.visible_body_state != "visible_body":
            return None
        return outcome.last_physical_stream_event_id

    def _completed_stream_failure_outcome(
        self,
        *,
        visible_stream_event_id: str | None,
        failure_reason: str | None,
        streamed_text: str | None,
        tool_trace: list[ToolTraceEntry] | None,
        extra_content: dict[str, Any] | None,
        option_map: typing.Mapping[str, str] | None = None,
        options_list: typing.Sequence[typing.Mapping[str, str]] | None = None,
    ) -> FinalDeliveryOutcome:
        """Return the outcome when completed terminal delivery fails after visible streaming."""
        if visible_stream_event_id is not None:
            return FinalDeliveryOutcome.keep_prior_visible_stream_after_completed_terminal_failure(
                last_physical_stream_event_id=visible_stream_event_id,
                final_visible_body=streamed_text or None,
                failure_reason=failure_reason,
                tool_trace=tool_trace or (),
                extra_content=extra_content,
                option_map=option_map,
                options_list=options_list,
            )
        return FinalDeliveryOutcome.error_without_visible_response(
            failure_reason=failure_reason,
            tool_trace=tool_trace or (),
            extra_content=extra_content,
        )

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
        return FinalDeliveryOutcome.error_without_visible_response(
            failure_reason=failure_reason,
            tool_trace=tool_trace or (),
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
            return FinalDeliveryOutcome.suppression_cleanup_failed(
                last_physical_stream_event_id=event_id,
                failure_reason=str(error),
                tool_trace=tool_trace or (),
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
            return FinalDeliveryOutcome.suppression_cleanup_failed(
                last_physical_stream_event_id=event_id,
                failure_reason=str(error) or failure_reason or f"failed to redact suppressed response {event_id}",
                tool_trace=tool_trace or (),
                extra_content=extra_content,
            )
        if not redacted:
            return FinalDeliveryOutcome.suppression_cleanup_failed(
                last_physical_stream_event_id=event_id,
                failure_reason=failure_reason or f"failed to redact suppressed response {event_id}",
                tool_trace=tool_trace or (),
                extra_content=extra_content,
            )
        return FinalDeliveryOutcome.suppressed_redacted(
            last_physical_stream_event_id=event_id,
            failure_reason=failure_reason,
            tool_trace=tool_trace or (),
            extra_content=extra_content,
        )

    async def _cleanup_visible_placeholder(
        self,
        *,
        room_id: str,
        event_id: str,
        response_kind: str,
        response_envelope: MessageEnvelope,
        correlation_id: str,
        redaction_reason: str,
        failure_reason: str,
        tool_trace: list[ToolTraceEntry] | None = None,
        extra_content: dict[str, Any] | None = None,
    ) -> FinalDeliveryOutcome:
        """Redact a visible placeholder and return the canonical cleanup outcome."""
        return await self._redact_visible_response_event(
            room_id=room_id,
            event_id=event_id,
            response_kind=response_kind,
            response_envelope=response_envelope,
            correlation_id=correlation_id,
            redaction_reason=redaction_reason,
            failure_reason=failure_reason,
            tool_trace=tool_trace,
            extra_content=extra_content,
        )

    async def _deliver_visible_response(
        self,
        *,
        target: MessageTarget,
        existing_event_id: str | None,
        existing_event_is_placeholder: bool,
        response_text: str,
        skip_mentions: bool,
        response_kind: str,
        response_envelope: MessageEnvelope,
        correlation_id: str,
        tool_trace: list[ToolTraceEntry] | None,
        extra_content: dict[str, Any] | None,
    ) -> FinalDeliveryOutcome:
        """Deliver one visible response and return the canonical terminal outcome."""
        interactive_response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)
        display_text = interactive_response.formatted_text

        if existing_event_id is not None:
            edited = await self.edit_text(
                EditTextRequest(
                    target=target,
                    event_id=existing_event_id,
                    new_text=display_text,
                    tool_trace=tool_trace,
                    extra_content=extra_content,
                ),
            )
            if edited:
                outcome = FinalDeliveryOutcome.final_visible_delivery(
                    final_visible_event_id=existing_event_id,
                    final_visible_body=display_text,
                    last_physical_stream_event_id=(existing_event_id if existing_event_is_placeholder else None),
                    delivery_kind="edited",
                    tool_trace=tool_trace or (),
                    extra_content=extra_content,
                    option_map=interactive_response.option_map,
                    options_list=interactive_response.options_list,
                )
                await self._emit_after_response_best_effort(
                    correlation_id=correlation_id,
                    envelope=response_envelope,
                    response_text=display_text,
                    response_event_id=existing_event_id,
                    delivery_kind="edited",
                    response_kind=response_kind,
                )
                return outcome

            if existing_event_is_placeholder:
                cleanup_outcome = await self._cleanup_visible_placeholder(
                    room_id=target.room_id,
                    event_id=existing_event_id,
                    response_kind=response_kind,
                    response_envelope=response_envelope,
                    correlation_id=correlation_id,
                    redaction_reason="Failed placeholder response",
                    failure_reason="delivery_failed",
                    tool_trace=tool_trace,
                    extra_content=extra_content,
                )
                if cleanup_outcome.state == "suppression_cleanup_failed":
                    return cleanup_outcome
                return FinalDeliveryOutcome.error_without_visible_response(
                    failure_reason="delivery_failed",
                    tool_trace=tool_trace or (),
                    extra_content=extra_content,
                )
            return FinalDeliveryOutcome.error_with_visible_response(
                final_visible_event_id=existing_event_id,
                final_visible_body="",
                failure_reason="delivery_failed",
                tool_trace=tool_trace or (),
                extra_content=extra_content,
            )

        event_id = await self.send_text(
            SendTextRequest(
                target=target,
                response_text=display_text,
                skip_mentions=skip_mentions,
                tool_trace=tool_trace,
                extra_content=extra_content,
            ),
        )
        if event_id is None:
            return FinalDeliveryOutcome.error_without_visible_response(
                failure_reason="delivery_failed",
                tool_trace=tool_trace or (),
                extra_content=extra_content,
            )

        outcome = FinalDeliveryOutcome.final_visible_delivery(
            final_visible_event_id=event_id,
            final_visible_body=display_text,
            delivery_kind="sent",
            tool_trace=tool_trace or (),
            extra_content=extra_content,
            option_map=interactive_response.option_map,
            options_list=interactive_response.options_list,
        )
        await self._emit_after_response_best_effort(
            correlation_id=correlation_id,
            envelope=response_envelope,
            response_text=display_text,
            response_event_id=event_id,
            delivery_kind="sent",
            response_kind=response_kind,
        )
        return outcome

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

    async def deliver_final(self, request: FinalDeliveryRequest) -> FinalDeliveryOutcome:
        """Apply before/after hooks around one final send or edit."""
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
        if draft.suppress:
            visible_response_event_id = request.existing_event_id if request.existing_event_is_placeholder else None
            self.deps.logger.info(
                "Response suppressed by hook",
                response_kind=request.response_kind,
                source_event_id=request.response_envelope.source_event_id,
                correlation_id=request.correlation_id,
            )
            if visible_response_event_id is not None:
                outcome = await self._redact_visible_response_event(
                    room_id=request.target.room_id,
                    event_id=visible_response_event_id,
                    response_kind=request.response_kind,
                    response_envelope=request.response_envelope,
                    correlation_id=request.correlation_id,
                    redaction_reason="Suppressed placeholder response",
                    failure_reason="suppressed_by_hook",
                    tool_trace=draft.tool_trace,
                    extra_content=draft.extra_content,
                )
            else:
                outcome = FinalDeliveryOutcome.suppressed_without_visible_response(
                    failure_reason="suppressed_by_hook",
                    tool_trace=draft.tool_trace or (),
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

        outcome = await self._deliver_visible_response(
            target=request.target,
            existing_event_id=request.existing_event_id,
            existing_event_is_placeholder=request.existing_event_is_placeholder,
            response_text=draft.response_text,
            skip_mentions=request.skip_mentions,
            response_kind=request.response_kind,
            response_envelope=request.response_envelope,
            correlation_id=request.correlation_id,
            tool_trace=draft.tool_trace,
            extra_content=draft.extra_content,
        )
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
        cancelled_text, extra_content, failure_reason = self._cancelled_note_update(restart=request.restart)
        edited = await self.edit_text(
            EditTextRequest(
                target=request.target,
                event_id=request.event_id,
                new_text=cancelled_text,
                extra_content=extra_content,
            ),
        )
        if edited:
            outcome = FinalDeliveryOutcome.cancelled_with_visible_note(
                final_visible_event_id=request.event_id,
                final_visible_body=cancelled_text,
                last_physical_stream_event_id=request.event_id if request.existing_event_is_placeholder else None,
                delivery_kind="edited",
                failure_reason=failure_reason,
                extra_content=extra_content,
            )
        elif request.existing_event_is_placeholder:
            cleanup_outcome = await self._cleanup_visible_placeholder(
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
                else FinalDeliveryOutcome.cancelled_without_visible_response(
                    failure_reason=failure_reason,
                    extra_content=extra_content,
                )
            )
        else:
            outcome = FinalDeliveryOutcome.cancelled_with_visible_response(
                final_visible_event_id=request.event_id,
                failure_reason=failure_reason,
                extra_content=extra_content,
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

    async def finalize_streamed_response(  # noqa: C901, PLR0911, PLR0912
        self,
        request: FinalizeStreamedResponseRequest,
    ) -> FinalDeliveryOutcome:
        """Apply hooks and any final edit needed after streamed delivery completes."""
        stream_outcome = request.stream_transport_outcome
        streamed_event_id = stream_outcome.last_physical_stream_event_id
        visible_stream_event_id = self._visible_stream_event_id(stream_outcome)
        streamed_text = self._current_stream_body(stream_outcome)

        if stream_outcome.terminal_status == "cancelled":
            if visible_stream_event_id is not None:
                outcome = FinalDeliveryOutcome.keep_prior_visible_stream_after_cancel(
                    last_physical_stream_event_id=visible_stream_event_id,
                    final_visible_body=streamed_text or None,
                    failure_reason=stream_outcome.failure_reason,
                    tool_trace=request.tool_trace or (),
                    extra_content=request.extra_content,
                    option_map=stream_outcome.option_map,
                    options_list=stream_outcome.options_list,
                )
            else:
                outcome = FinalDeliveryOutcome.cancelled_without_visible_response(
                    failure_reason=stream_outcome.failure_reason,
                    tool_trace=request.tool_trace or (),
                    extra_content=request.extra_content,
                )
            return await self.emit_terminal_outcome_hooks(
                outcome=outcome,
                correlation_id=request.correlation_id,
                envelope=request.response_envelope,
                response_kind=request.response_kind,
            )

        if stream_outcome.terminal_status == "error":
            if visible_stream_event_id is not None:
                outcome = FinalDeliveryOutcome.keep_prior_visible_stream_after_error(
                    last_physical_stream_event_id=visible_stream_event_id,
                    final_visible_body=streamed_text or None,
                    failure_reason=stream_outcome.failure_reason,
                    tool_trace=request.tool_trace or (),
                    extra_content=request.extra_content,
                    option_map=stream_outcome.option_map,
                    options_list=stream_outcome.options_list,
                )
            else:
                outcome = FinalDeliveryOutcome.error_without_visible_response(
                    failure_reason=stream_outcome.failure_reason,
                    tool_trace=request.tool_trace or (),
                    extra_content=request.extra_content,
                )
            return await self.emit_terminal_outcome_hooks(
                outcome=outcome,
                correlation_id=request.correlation_id,
                envelope=request.response_envelope,
                response_kind=request.response_kind,
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
            else:
                outcome = self._completed_stream_failure_outcome(
                    visible_stream_event_id=visible_stream_event_id,
                    failure_reason=failure_reason,
                    streamed_text=streamed_text or None,
                    tool_trace=request.tool_trace,
                    extra_content=request.extra_content,
                    option_map=stream_outcome.option_map,
                    options_list=stream_outcome.options_list,
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
            outcome = FinalDeliveryOutcome.error_without_visible_response(
                failure_reason=stream_outcome.failure_reason or "stream_completed_without_visible_body",
                tool_trace=request.tool_trace or (),
                extra_content=request.extra_content,
            )
            return await self.emit_terminal_outcome_hooks(
                outcome=outcome,
                correlation_id=request.correlation_id,
                envelope=request.response_envelope,
                response_kind=request.response_kind,
            )

        draft = await self.deps.response_hooks.apply_before_response(
            correlation_id=request.correlation_id,
            envelope=request.response_envelope,
            response_text=streamed_text,
            response_kind=request.response_kind,
            tool_trace=request.tool_trace,
            extra_content=request.extra_content,
        )
        if draft.suppress:
            if streamed_event_id is not None:
                outcome = await self._redact_visible_response_event(
                    room_id=request.target.room_id,
                    event_id=streamed_event_id,
                    response_kind=request.response_kind,
                    response_envelope=request.response_envelope,
                    correlation_id=request.correlation_id,
                    redaction_reason="Suppressed streamed response",
                    failure_reason="suppressed_by_hook",
                    tool_trace=draft.tool_trace,
                    extra_content=draft.extra_content,
                )
            else:
                outcome = FinalDeliveryOutcome.suppressed_without_visible_response(
                    failure_reason="suppressed_by_hook",
                    tool_trace=draft.tool_trace or (),
                    extra_content=draft.extra_content,
                )
            return await self.emit_terminal_outcome_hooks(
                outcome=outcome,
                correlation_id=request.correlation_id,
                envelope=request.response_envelope,
                response_kind=request.response_kind,
            )

        needs_final_edit = (
            draft.response_text != streamed_text
            or draft.tool_trace != request.tool_trace
            or draft.extra_content != request.extra_content
        )
        if needs_final_edit:
            final_outcome = await self.deliver_final(
                FinalDeliveryRequest(
                    target=request.target,
                    existing_event_id=streamed_event_id,
                    response_text=draft.response_text,
                    response_kind=request.response_kind,
                    response_envelope=request.response_envelope,
                    correlation_id=request.correlation_id,
                    tool_trace=draft.tool_trace,
                    extra_content=draft.extra_content,
                    apply_before_hooks=False,
                    existing_event_is_placeholder=False,
                    emit_terminal_hooks=False,
                ),
            )
            if final_outcome.state == "error_without_visible_response" and visible_stream_event_id is not None:
                recovered_outcome = self._completed_stream_failure_outcome(
                    visible_stream_event_id=visible_stream_event_id,
                    failure_reason=final_outcome.failure_reason or "terminal_update_failed",
                    streamed_text=streamed_text or None,
                    tool_trace=request.tool_trace,
                    extra_content=request.extra_content,
                    option_map=stream_outcome.option_map,
                    options_list=stream_outcome.options_list,
                )
                return await self.emit_terminal_outcome_hooks(
                    outcome=recovered_outcome,
                    correlation_id=request.correlation_id,
                    envelope=request.response_envelope,
                    response_kind=request.response_kind,
                )
            if not final_outcome.emits_after_response:
                return await self.emit_terminal_outcome_hooks(
                    outcome=final_outcome,
                    correlation_id=request.correlation_id,
                    envelope=request.response_envelope,
                    response_kind=request.response_kind,
                )
            return final_outcome

        interactive_response = interactive.parse_and_format_interactive(
            streamed_text,
            extract_mapping=True,
        )
        option_map = stream_outcome.option_map or interactive_response.option_map
        options_list = stream_outcome.options_list or interactive_response.options_list
        assert streamed_event_id is not None
        outcome = FinalDeliveryOutcome.final_visible_delivery(
            final_visible_event_id=streamed_event_id,
            last_physical_stream_event_id=streamed_event_id,
            final_visible_body=interactive_response.formatted_text,
            delivery_kind=request.initial_delivery_kind,
            tool_trace=request.tool_trace or (),
            extra_content=request.extra_content,
            option_map=option_map,
            options_list=options_list,
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
