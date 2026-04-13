"""Own visible Matrix delivery for already-generated responses."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from html import escape as html_escape
from typing import TYPE_CHECKING, Any, Literal

import nio

from mindroom import constants, interactive
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
from mindroom.matrix.client import (
    build_threaded_edit_content,
    edit_message,
    get_latest_thread_event_id_if_needed,
    send_message,
)
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_builder import build_message_content
from mindroom.streaming import StreamingResponse, send_streaming_response

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.history.types import CompactionOutcome
    from mindroom.hooks import MessageEnvelope
    from mindroom.message_target import MessageTarget
    from mindroom.streaming import _StreamInputChunk
    from mindroom.timing import DispatchPipelineTiming
    from mindroom.tool_system.events import ToolTraceEntry


class SuppressedPlaceholderCleanupError(RuntimeError):
    """Raised when one provisional suppressed response cannot be removed safely."""


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
            tool_trace=tool_trace,
            extra_content=extra_content,
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
    streaming_cls: type[StreamingResponse] = StreamingResponse
    pipeline_timing: DispatchPipelineTiming | None = None


@dataclass(frozen=True)
class DeliveryGatewayDeps:
    """Explicit dependencies needed for Matrix delivery."""

    runtime: BotRuntimeView
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
    streamed_event_id: str
    streamed_text: str
    delivery_kind: Literal["sent", "edited"]
    response_kind: str
    response_envelope: MessageEnvelope
    correlation_id: str
    tool_trace: list[ToolTraceEntry] | None
    extra_content: dict[str, Any] | None
    cleanup_suppressed_streamed_event: bool = False


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
                reply_to_event_id=None,
                latest_thread_event_id=None,
                tool_trace=request.tool_trace,
                extra_content=request.extra_content,
            )
        else:
            latest_thread_event_id = await get_latest_thread_event_id_if_needed(
                client,
                resolved_target.room_id,
                effective_thread_id,
                resolved_target.reply_to_event_id,
                event_cache=self.deps.runtime.event_cache,
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

        event_id = await send_message(client, resolved_target.room_id, content)
        if event_id:
            self.deps.logger.info("Sent response", event_id=event_id, **resolved_target.log_context)
            return event_id
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
                tool_trace=request.tool_trace,
                extra_content=request.extra_content,
            )
        else:
            content = await build_threaded_edit_content(
                client,
                room_id=target.room_id,
                new_text=request.new_text,
                thread_id=target.resolved_thread_id,
                config=config,
                runtime_paths=self.deps.runtime_paths,
                sender_domain=self.deps.sender_domain,
                tool_trace=request.tool_trace,
                extra_content=request.extra_content,
                event_cache=self.deps.runtime.event_cache,
            )

        response = await edit_message(
            client,
            target.room_id,
            request.event_id,
            content,
            request.new_text,
        )
        if isinstance(response, nio.RoomSendResponse):
            self.deps.logger.info("Edited message", event_id=request.event_id, **target.log_context)
            return True
        self.deps.logger.error(
            "Failed to edit message",
            event_id=request.event_id,
            error=str(response),
            **target.log_context,
        )
        return False

    async def redact_suppressed_response_event(
        self,
        *,
        room_id: str,
        event_id: str,
        response_text: str,
        reason: str,
    ) -> DeliveryResult:
        """Redact one provisional response and report a suppressed no-final-event outcome."""
        redacted = await self.deps.redact_message_event(
            room_id=room_id,
            event_id=event_id,
            reason=reason,
        )
        if not redacted:
            msg = f"failed to redact suppressed response {event_id}"
            raise SuppressedPlaceholderCleanupError(msg)
        return DeliveryResult(
            event_id=None,
            response_text=response_text,
            delivery_kind=None,
            suppressed=True,
        )

    async def cleanup_suppressed_streamed_response(
        self,
        *,
        room_id: str,
        event_id: str,
        response_text: str,
        response_kind: str,
        response_envelope: MessageEnvelope,
        correlation_id: str,
    ) -> DeliveryResult:
        """Remove one provisional streamed response after a suppressing hook runs."""
        self.deps.logger.warning(
            "Streaming response was already delivered before a suppressing hook ran",
            response_kind=response_kind,
            source_event_id=response_envelope.source_event_id,
            correlation_id=correlation_id,
        )
        return await self.redact_suppressed_response_event(
            room_id=room_id,
            event_id=event_id,
            response_text=response_text,
            reason="Suppressed streamed response",
        )

    async def emit_suppressed_response(
        self,
        *,
        correlation_id: str,
        response_envelope: MessageEnvelope,
        response_kind: str,
        visible_response_event_id: str | None = None,
    ) -> None:
        """Treat hook-suppressed turns as non-delivered responses for cleanup hooks."""
        await self.deps.response_hooks.emit_cancelled_response(
            correlation_id=correlation_id,
            envelope=response_envelope,
            visible_response_event_id=visible_response_event_id,
            response_kind=response_kind,
        )

    async def deliver_final(self, request: FinalDeliveryRequest) -> DeliveryResult:
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
            await self.emit_suppressed_response(
                correlation_id=request.correlation_id,
                response_envelope=request.response_envelope,
                response_kind=request.response_kind,
                visible_response_event_id=(
                    request.existing_event_id if request.existing_event_is_placeholder else None
                ),
            )
            self.deps.logger.info(
                "Response suppressed by hook",
                response_kind=request.response_kind,
                source_event_id=request.response_envelope.source_event_id,
                correlation_id=request.correlation_id,
            )
            if request.existing_event_id is not None and request.existing_event_is_placeholder:
                return await self.redact_suppressed_response_event(
                    room_id=request.target.room_id,
                    event_id=request.existing_event_id,
                    response_text=draft.response_text,
                    reason="Suppressed placeholder response",
                )
            return DeliveryResult(
                event_id=None,
                response_text=draft.response_text,
                delivery_kind=None,
                suppressed=True,
            )

        interactive_response = interactive.parse_and_format_interactive(draft.response_text, extract_mapping=True)
        display_text = interactive_response.formatted_text
        resolved_target = request.target
        if request.existing_event_id:
            edited = await self.edit_text(
                EditTextRequest(
                    target=resolved_target,
                    event_id=request.existing_event_id,
                    new_text=display_text,
                    tool_trace=draft.tool_trace,
                    extra_content=draft.extra_content,
                ),
            )
            event_id = request.existing_event_id if edited else None
            delivery_kind: Literal["sent", "edited"] | None = "edited" if edited else None
        else:
            event_id = await self.send_text(
                SendTextRequest(
                    target=resolved_target,
                    response_text=display_text,
                    tool_trace=draft.tool_trace,
                    extra_content=draft.extra_content,
                ),
            )
            delivery_kind = "sent" if event_id else None

        if event_id and delivery_kind is not None:
            await self._emit_after_response_best_effort(
                correlation_id=request.correlation_id,
                envelope=request.response_envelope,
                response_text=display_text,
                response_event_id=event_id,
                delivery_kind=delivery_kind,
                response_kind=request.response_kind,
            )

        return DeliveryResult(
            event_id=event_id,
            response_text=display_text,
            delivery_kind=delivery_kind,
            suppressed=False,
            option_map=interactive_response.option_map,
            options_list=interactive_response.options_list,
            failure_reason="delivery_failed" if delivery_kind is None and not event_id else None,
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
        event_id = await send_message(client, target.room_id, content)
        if event_id:
            self.deps.logger.info(
                "Sent compaction notice",
                event_id=event_id,
                **target.log_context,
                summary_model=request.outcome.summary_model,
            )
            return event_id
        self.deps.logger.error("Failed to send compaction notice", **target.log_context)
        return None

    async def deliver_stream(
        self,
        request: StreamingDeliveryRequest,
    ) -> tuple[str | None, str]:
        """Send one streaming Matrix response."""
        client = self._client()
        config = self.deps.runtime.config
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
            event_cache=self.deps.runtime.event_cache,
        )

    async def finalize_streamed_response(
        self,
        request: FinalizeStreamedResponseRequest,
    ) -> DeliveryResult:
        """Apply hooks and any final edit needed after streamed delivery completes."""
        draft = await self.deps.response_hooks.apply_before_response(
            correlation_id=request.correlation_id,
            envelope=request.response_envelope,
            response_text=request.streamed_text,
            response_kind=request.response_kind,
            tool_trace=request.tool_trace,
            extra_content=request.extra_content,
        )
        if draft.suppress:
            await self.emit_suppressed_response(
                correlation_id=request.correlation_id,
                response_envelope=request.response_envelope,
                response_kind=request.response_kind,
                visible_response_event_id=request.streamed_event_id,
            )
            if request.cleanup_suppressed_streamed_event:
                return await self.cleanup_suppressed_streamed_response(
                    room_id=request.target.room_id,
                    event_id=request.streamed_event_id,
                    response_text=request.streamed_text,
                    response_kind=request.response_kind,
                    response_envelope=request.response_envelope,
                    correlation_id=request.correlation_id,
                )
            self.deps.logger.warning(
                "Streaming response was already delivered before a suppressing hook ran",
                source_event_id=request.response_envelope.source_event_id,
                correlation_id=request.correlation_id,
            )
            return DeliveryResult(
                event_id=request.streamed_event_id,
                response_text=request.streamed_text,
                delivery_kind=request.delivery_kind,
                suppressed=True,
            )

        needs_final_edit = (
            draft.response_text != request.streamed_text
            or draft.tool_trace != request.tool_trace
            or draft.extra_content != request.extra_content
        )
        if needs_final_edit:
            return await self.deliver_final(
                FinalDeliveryRequest(
                    target=request.target,
                    existing_event_id=request.streamed_event_id,
                    response_text=draft.response_text,
                    response_kind=request.response_kind,
                    response_envelope=request.response_envelope,
                    correlation_id=request.correlation_id,
                    tool_trace=draft.tool_trace,
                    extra_content=draft.extra_content,
                    apply_before_hooks=False,
                ),
            )

        interactive_response = interactive.parse_and_format_interactive(
            request.streamed_text,
            extract_mapping=True,
        )
        await self._emit_after_response_best_effort(
            correlation_id=request.correlation_id,
            envelope=request.response_envelope,
            response_text=interactive_response.formatted_text,
            response_event_id=request.streamed_event_id,
            delivery_kind=request.delivery_kind,
            response_kind=request.response_kind,
        )
        return DeliveryResult(
            event_id=request.streamed_event_id,
            response_text=interactive_response.formatted_text,
            delivery_kind=request.delivery_kind,
            option_map=interactive_response.option_map,
            options_list=interactive_response.options_list,
        )
