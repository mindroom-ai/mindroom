"""Own visible Matrix delivery for already-generated responses."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from html import escape as html_escape
from typing import TYPE_CHECKING, Any, Literal

from mindroom import constants, interactive
from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome
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
from mindroom.matrix.client_delivery import build_threaded_edit_content, edit_message_result, send_message_result
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_builder import build_message_content
from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001
from mindroom.streaming import (
    SYNC_RESTART_CANCEL_MSG,
    USER_STOP_CANCEL_MSG,
    CancelSource,
    StreamingResponse,
    build_cancelled_response_update,
    cancel_failure_reason,
    send_streaming_response,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    import nio
    import structlog

    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.history import (
        CompactionLifecycleFailure,
        CompactionLifecycleProgress,
        CompactionLifecycleStart,
        CompactionLifecycleSuccess,
    )
    from mindroom.hooks import MessageEnvelope
    from mindroom.message_target import MessageTarget
    from mindroom.streaming_delivery import StreamInputChunk
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


@dataclass
class ResponseHookService:
    """Own response hook execution around final delivery."""

    hook_context: HookContextSupport

    async def apply_before_response(  # noqa: D102
        self,
        *,
        correlation_id: str,
        envelope: MessageEnvelope,
        response_text: str,
        response_kind: str,
        tool_trace: list[ToolTraceEntry] | None,
        extra_content: dict[str, Any] | None,
    ) -> ResponseDraft:
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

    async def apply_final_response_transform(  # noqa: D102
        self,
        *,
        correlation_id: str,
        envelope: MessageEnvelope,
        response_text: str,
        response_kind: str,
    ) -> FinalResponseDraft:
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

    async def emit_after_response(  # noqa: D102
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

    async def emit_cancelled_response(  # noqa: D102
        self,
        *,
        correlation_id: str,
        envelope: MessageEnvelope,
        visible_response_event_id: str | None = None,
        response_kind: str = "ai",
        failure_reason: str | None = None,
    ) -> None:
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
class SendTextRequest:  # noqa: D101
    target: MessageTarget
    response_text: str
    skip_mentions: bool = False
    tool_trace: list[ToolTraceEntry] | None = None
    extra_content: dict[str, Any] | None = None


@dataclass(frozen=True)
class EditTextRequest:  # noqa: D101
    target: MessageTarget
    event_id: str
    new_text: str
    tool_trace: list[ToolTraceEntry] | None = None
    extra_content: dict[str, Any] | None = None


@dataclass(frozen=True)
class FinalDeliveryRequest:  # noqa: D101
    target: MessageTarget
    existing_event_id: str | None
    response_text: str
    response_kind: str
    response_envelope: MessageEnvelope
    correlation_id: str
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
    response_kind: str
    response_envelope: MessageEnvelope
    correlation_id: str


@dataclass(frozen=True)
class _PlaceholderFailureUpdateRequest:
    """Parameters for finalizing a placeholder after Matrix delivery fails."""

    target: MessageTarget
    event_id: str
    response_kind: str
    response_envelope: MessageEnvelope
    correlation_id: str
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
        return await self.delivery_gateway.send_compaction_lifecycle_start(
            target=self.target,
            reply_to_event_id=self.reply_to_event_id,
            event=event,
        )

    async def progress(self, event: CompactionLifecycleProgress) -> None:
        """Edit the lifecycle notice after persisted compaction progress."""
        await self.delivery_gateway.edit_compaction_lifecycle_progress(
            target=self.target,
            event=event,
        )

    async def complete_success(self, event: CompactionLifecycleSuccess) -> None:
        """Edit the lifecycle notice after successful compaction."""
        await self.delivery_gateway.edit_compaction_lifecycle_success(
            target=self.target,
            event=event,
        )

    async def complete_failure(self, event: CompactionLifecycleFailure) -> None:
        """Edit the lifecycle notice after failed compaction."""
        await self.delivery_gateway.edit_compaction_lifecycle_failure(
            target=self.target,
            event=event,
        )


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

    def _current_stream_body(self, outcome: StreamTransportOutcome) -> str:
        """Return the current streamed body snapshot used for hook and outcome decisions."""
        return outcome.rendered_body or ""

    def _visible_stream_event_id(self, outcome: StreamTransportOutcome) -> str | None:
        """Return the streamed event id only when the stream showed real visible body text."""
        if outcome.visible_body_state != "visible_body":
            return None
        return outcome.last_physical_stream_event_id

    @staticmethod
    def _interactive_response_for_visible_body(
        visible_body: str,
        *,
        canonical_body_candidate: str | None,
        stream_interactive_metadata: interactive.InteractiveMetadata | None = None,
    ) -> interactive._InteractiveResponse:
        """Return interactive metadata only when it belongs to the visible body."""
        if stream_interactive_metadata is not None:
            return interactive._InteractiveResponse(visible_body, stream_interactive_metadata)

        visible_response = interactive.parse_and_format_interactive(visible_body, extract_mapping=True)
        if visible_response.interactive_metadata is not None:
            return visible_response

        if canonical_body_candidate is None or canonical_body_candidate == visible_body:
            return visible_response

        canonical_response = interactive.parse_and_format_interactive(
            canonical_body_candidate,
            extract_mapping=True,
        )
        if canonical_response.interactive_metadata is not None and canonical_response.formatted_text == visible_body:
            return canonical_response

        return visible_response

    @staticmethod
    def _cancelled_error_failure_reason(error: asyncio.CancelledError) -> str:
        """Normalize CancelledError values to the canonical cancellation reason strings."""
        cancel_source: CancelSource = "interrupted"
        if error.args and error.args[0] == USER_STOP_CANCEL_MSG:
            cancel_source = "user_stop"
        elif error.args and error.args[0] == SYNC_RESTART_CANCEL_MSG:
            cancel_source = "sync_restart"
        return cancel_failure_reason(cancel_source)

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
            cleanup_failure = await self._redact_visible_response_event(
                room_id=room_id,
                event_id=streamed_event_id,
                response_kind=response_kind,
                response_envelope=response_envelope,
                correlation_id=correlation_id,
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
    ) -> str | None:
        """Redact one visible response event and return a failure reason when cleanup fails."""
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
            return self._cancelled_error_failure_reason(error)
        except Exception as error:
            self.deps.logger.exception(
                "Failed to redact visible response during cleanup",
                room_id=room_id,
                event_id=event_id,
                response_kind=response_kind,
                correlation_id=correlation_id,
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
        edited = await self.edit_text(
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
            response_kind=request.response_kind,
            source_event_id=request.response_envelope.source_event_id,
            correlation_id=request.correlation_id,
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

    async def deliver_final(  # noqa: C901, PLR0911, PLR0912
        self,
        request: FinalDeliveryRequest,
    ) -> FinalDeliveryOutcome:
        """Apply before_response hooks and perform the final send or edit."""
        try:
            draft = await self.deps.response_hooks.apply_before_response(
                correlation_id=request.correlation_id,
                envelope=request.response_envelope,
                response_text=request.response_text,
                response_kind=request.response_kind,
                tool_trace=request.tool_trace,
                extra_content=request.extra_content,
            )
        except asyncio.CancelledError as error:
            failure_reason = self._cancelled_error_failure_reason(error)
            if request.existing_event_id is not None and request.existing_event_is_placeholder:
                cleanup_failure = await self._redact_visible_response_event(
                    room_id=request.target.room_id,
                    event_id=request.existing_event_id,
                    response_kind=request.response_kind,
                    response_envelope=request.response_envelope,
                    correlation_id=request.correlation_id,
                    redaction_reason="Cancelled placeholder response",
                    failure_reason=failure_reason,
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
            raise
        except Exception as error:
            failure_reason = str(error)
            if request.existing_event_id is not None and request.existing_event_is_placeholder:
                cleanup_failure = await self._redact_visible_response_event(
                    room_id=request.target.room_id,
                    event_id=request.existing_event_id,
                    response_kind=request.response_kind,
                    response_envelope=request.response_envelope,
                    correlation_id=request.correlation_id,
                    redaction_reason="Failed placeholder response before delivery",
                    failure_reason=failure_reason,
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
                response_kind=request.response_kind,
                source_event_id=request.response_envelope.source_event_id,
                correlation_id=request.correlation_id,
            )
            if request.existing_event_id is not None and request.existing_event_is_placeholder:
                cleanup_failure = await self._redact_visible_response_event(
                    room_id=request.target.room_id,
                    event_id=request.existing_event_id,
                    response_kind=request.response_kind,
                    response_envelope=request.response_envelope,
                    correlation_id=request.correlation_id,
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
                        response_kind=request.response_kind,
                        response_envelope=request.response_envelope,
                        correlation_id=request.correlation_id,
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
        edited = await self.edit_text(
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
                failure_reason=failure_reason,
                extra_content=extra_content,
            )
        if not request.existing_event_is_placeholder:
            return FinalDeliveryOutcome(
                terminal_status="cancelled",
                event_id=request.event_id,
                is_visible_response=True,
                final_visible_body=cancelled_text,
                failure_reason=failure_reason,
                extra_content=extra_content,
            )
        cleanup_failure = await self._redact_visible_response_event(
            room_id=request.target.room_id,
            event_id=request.event_id,
            response_kind=request.response_kind,
            response_envelope=request.response_envelope,
            correlation_id=request.correlation_id,
            redaction_reason="Failed cancelled placeholder response",
            failure_reason=failure_reason,
        )
        if cleanup_failure is not None:
            return FinalDeliveryOutcome(
                terminal_status="error",
                event_id=request.event_id,
                is_visible_response=True,
                failure_reason=cleanup_failure,
                extra_content=extra_content,
            )
        return FinalDeliveryOutcome(
            terminal_status="cancelled",
            event_id=None,
            failure_reason=failure_reason,
            extra_content=extra_content,
        )

    async def send_compaction_lifecycle_start(
        self,
        *,
        target: MessageTarget,
        reply_to_event_id: str | None,
        event: CompactionLifecycleStart,
    ) -> str | None:
        """Send the foreground compaction lifecycle notice."""
        body = "Compacting history..."
        content = build_message_content(
            body,
            formatted_body=f"<em>{html_escape(body)}</em>",
            thread_event_id=target.resolved_thread_id,
            reply_to_event_id=reply_to_event_id,
            extra_content={
                "msgtype": "m.notice",
                constants.COMPACTION_NOTICE_CONTENT_KEY: {
                    "version": 3,
                    "status": "running",
                    "mode": event.mode,
                    "session_id": event.session_id,
                    "scope": event.scope,
                    "summary_model": event.summary_model,
                    "before_tokens": event.before_tokens,
                    "history_budget_tokens": event.history_budget_tokens,
                    "runs_before": event.runs_before,
                },
                "com.mindroom.skip_mentions": True,
            },
        )
        delivered = await send_message_result(self._client(), target.room_id, content)
        if delivered is not None:
            self.deps.resolver.deps.conversation_cache.notify_outbound_message(
                target.room_id,
                delivered.event_id,
                delivered.content_sent,
            )
            self.deps.logger.info("Sent compaction lifecycle notice", event_id=delivered.event_id, **target.log_context)
            return delivered.event_id
        self.deps.logger.error("Failed to send compaction lifecycle notice", **target.log_context)
        return None

    async def edit_compaction_lifecycle_progress(
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

    async def edit_compaction_lifecycle_success(
        self,
        *,
        target: MessageTarget,
        event: CompactionLifecycleSuccess,
    ) -> None:
        """Edit the foreground compaction lifecycle notice after success."""
        if event.notice_event_id is None:
            return
        outcome = replace(event.outcome, duration_ms=event.duration_ms)
        await self._edit_compaction_lifecycle_notice(
            target=target,
            event_id=event.notice_event_id,
            body=outcome.format_notice(),
            metadata=outcome.to_notice_metadata(),
        )

    async def edit_compaction_lifecycle_failure(
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
        latest_thread_event_id = await self.deps.resolver.deps.conversation_cache.get_latest_thread_event_id_if_needed(
            target.room_id,
            target.resolved_thread_id,
            target.reply_to_event_id,
            event_id,
        )
        content = build_message_content(
            body,
            formatted_body=f"<em>{html_escape(body).replace(chr(10), '<br/>')}</em>",
            thread_event_id=target.resolved_thread_id,
            reply_to_event_id=target.reply_to_event_id,
            latest_thread_event_id=latest_thread_event_id,
            extra_content={
                "msgtype": "m.notice",
                constants.COMPACTION_NOTICE_CONTENT_KEY: metadata,
                "com.mindroom.skip_mentions": True,
            },
        )
        delivered = await edit_message_result(self._client(), target.room_id, event_id, content, body)
        if delivered is not None:
            self.deps.resolver.deps.conversation_cache.notify_outbound_message(
                target.room_id,
                delivered.event_id,
                delivered.content_sent,
            )
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
            pipeline_timing=request.pipeline_timing,
            visible_event_id_callback=request.visible_event_id_callback,
            latest_thread_event_id=latest_thread_event_id,
            conversation_cache=self.deps.resolver.deps.conversation_cache,
            preserve_existing_visible_on_empty_terminal=(
                request.preserve_existing_visible_on_empty_terminal
                or (request.existing_event_id is not None and not request.adopt_existing_placeholder)
            ),
        )

    async def _finalize_visible_replacement_edit(
        self,
        *,
        target: MessageTarget,
        event_id: str | None,
        response_text: str,
        canonical_body_candidate: str | None = None,
        tool_trace: list[ToolTraceEntry] | None,
        extra_content: dict[str, Any] | None,
        failure_reason: str | None = None,
    ) -> FinalDeliveryOutcome | None:
        if event_id is None:
            return None
        interactive_response = self._interactive_response_for_visible_body(
            response_text,
            canonical_body_candidate=canonical_body_candidate,
        )
        edited = await self.edit_text(
            EditTextRequest(
                target=target,
                event_id=event_id,
                new_text=interactive_response.formatted_text,
                tool_trace=tool_trace,
                extra_content=extra_content,
            ),
        )
        if not edited:
            return None
        return FinalDeliveryOutcome(
            terminal_status="completed",
            event_id=event_id,
            is_visible_response=True,
            final_visible_body=interactive_response.formatted_text,
            delivery_kind="edited",
            failure_reason=failure_reason,
            tool_trace=tuple(tool_trace or ()),
            extra_content=extra_content,
            interactive_metadata=interactive_response.interactive_metadata,
        )

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
                    response_kind=request.response_kind,
                    response_envelope=request.response_envelope,
                    correlation_id=request.correlation_id,
                    failure_reason=failure_reason,
                    tool_trace=request.tool_trace,
                    extra_content=request.extra_content,
                ),
            )

        return await self._cleanup_completed_placeholder_only_stream(
            room_id=request.target.room_id,
            streamed_event_id=placeholder_event_id,
            response_kind=request.response_kind,
            response_envelope=request.response_envelope,
            correlation_id=request.correlation_id,
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
            visible_stream_event_id = self._visible_stream_event_id(stream_outcome)
            streamed_text = self._current_stream_body(stream_outcome)
            final_body_candidate = stream_outcome.canonical_final_body_candidate or streamed_text
            if stream_outcome.terminal_status == "cancelled":
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
                            failure_reason=stream_outcome.failure_reason or "stream_finalize_cancelled",
                            tool_trace=tuple(request.tool_trace or ()),
                            extra_content=request.extra_content,
                        )
                failure_reason = stream_outcome.failure_reason or "stream_finalize_cancelled"
                if stream_outcome.visible_body_state == "placeholder_only":
                    cleanup_outcome = await self._cleanup_completed_placeholder_only_stream(
                        room_id=request.target.room_id,
                        streamed_event_id=stream_outcome.last_physical_stream_event_id,
                        response_kind=request.response_kind,
                        response_envelope=request.response_envelope,
                        correlation_id=request.correlation_id,
                        failure_reason=failure_reason,
                        tool_trace=request.tool_trace,
                        extra_content=request.extra_content,
                    )
                    if cleanup_outcome.event_id is not None:
                        return cleanup_outcome
                    return FinalDeliveryOutcome(
                        terminal_status="cancelled",
                        event_id=None,
                        failure_reason=failure_reason,
                        tool_trace=tuple(request.tool_trace or ()),
                        extra_content=request.extra_content,
                    )

                visible_stream_event_id = self._visible_stream_event_id(stream_outcome)
                if visible_stream_event_id is not None:
                    return FinalDeliveryOutcome(
                        terminal_status="cancelled",
                        event_id=visible_stream_event_id,
                        is_visible_response=True,
                        final_visible_body=streamed_text or None,
                        failure_reason=failure_reason,
                        tool_trace=tuple(request.tool_trace or ()),
                        extra_content=request.extra_content,
                    )
                if request.existing_event_id is not None and not request.existing_event_is_placeholder:
                    return FinalDeliveryOutcome(
                        terminal_status="cancelled",
                        event_id=request.existing_event_id,
                        is_visible_response=True,
                        failure_reason=failure_reason,
                        tool_trace=tuple(request.tool_trace or ()),
                        extra_content=request.extra_content,
                    )
                return FinalDeliveryOutcome(
                    terminal_status="cancelled",
                    event_id=None,
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

                visible_stream_event_id = self._visible_stream_event_id(stream_outcome)
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
                        response_kind=request.response_kind,
                        response_envelope=request.response_envelope,
                        correlation_id=request.correlation_id,
                        tool_trace=request.tool_trace,
                        extra_content=request.extra_content,
                    ),
                )

            if stream_outcome.visible_body_state == "placeholder_only":
                return await self._cleanup_completed_placeholder_only_stream(
                    room_id=request.target.room_id,
                    streamed_event_id=streamed_event_id,
                    response_kind=request.response_kind,
                    response_envelope=request.response_envelope,
                    correlation_id=request.correlation_id,
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
            try:
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
                final_transform_draft = await self.deps.response_hooks.apply_final_response_transform(
                    correlation_id=request.correlation_id,
                    envelope=request.response_envelope,
                    response_text=final_body_candidate,
                    response_kind=request.response_kind,
                )
                if (
                    final_transform_draft.response_text != final_body_candidate
                    and final_transform_draft.response_text.strip()
                ):
                    try:
                        final_outcome = await self._finalize_visible_replacement_edit(
                            target=request.target,
                            event_id=streamed_event_id,
                            response_text=final_transform_draft.response_text,
                            canonical_body_candidate=final_body_candidate,
                            tool_trace=request.tool_trace,
                            extra_content=request.extra_content,
                            failure_reason=stream_outcome.failure_reason,
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
                        if final_outcome is not None:
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
            interactive_response = self._interactive_response_for_visible_body(
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
        except asyncio.CancelledError:
            visible_event_id = self._visible_stream_event_id(stream_outcome)
            event_id = visible_event_id
            if event_id is None and request.existing_event_id is not None and not request.existing_event_is_placeholder:
                event_id = request.existing_event_id
            final_visible_body = self._current_stream_body(stream_outcome) if visible_event_id is not None else None
            return FinalDeliveryOutcome(
                terminal_status="cancelled",
                event_id=event_id,
                is_visible_response=event_id is not None,
                final_visible_body=final_visible_body,
                failure_reason="stream_finalize_cancelled",
                tool_trace=tuple(request.tool_trace or ()),
                extra_content=request.extra_content,
            )
        except Exception:
            self.deps.logger.exception(
                "Unexpected error in finalize_streamed_response",
                correlation_id=request.correlation_id,
            )
            visible_event_id = self._visible_stream_event_id(stream_outcome)
            event_id = visible_event_id
            if event_id is None and request.existing_event_id is not None and not request.existing_event_is_placeholder:
                event_id = request.existing_event_id
            final_visible_body = self._current_stream_body(stream_outcome) if visible_event_id is not None else None
            return FinalDeliveryOutcome(
                terminal_status="error",
                event_id=event_id,
                is_visible_response=event_id is not None,
                final_visible_body=final_visible_body,
                failure_reason="stream_finalize_failed",
                tool_trace=tuple(request.tool_trace or ()),
                extra_content=request.extra_content,
            )
