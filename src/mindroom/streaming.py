"""Streaming response implementation for real-time message updates."""

from __future__ import annotations

import asyncio
import re
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from agno.run.agent import RunCompletedEvent, RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent

from mindroom import interactive
from mindroom.constants import (
    AI_RUN_METADATA_KEY,
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
    STREAM_VISIBLE_BODY_KEY,
    STREAM_WARMUP_SUFFIX_KEY,
)
from mindroom.final_delivery import StreamTerminalOperation, StreamTransportOutcome
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import edit_message_result, send_message_result
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.message_target import MessageTarget
from mindroom.orchestration.runtime import is_sync_restart_cancel
from mindroom.streaming_delivery import (
    StreamInputChunk,
    _consume_stream_with_progress_supervision,
    _DeliveryRequest,
    _drain_worker_progress_events,
    _drive_stream_delivery,
    _NonTerminalDeliveryError,
    _raise_progress_delivery_error,
    _shutdown_stream_delivery,
    _shutdown_worker_progress_drain,
    _StreamDeliveryShutdownTimeoutError,
)
from mindroom.streaming_warmup import WorkerWarmupState
from mindroom.tool_system.runtime_context import worker_progress_pump_scope

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol
    from mindroom.timing import DispatchPipelineTiming
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.runtime_context import WorkerProgressEvent

logger = get_logger(__name__)

_PROGRESS_PLACEHOLDER = "Thinking..."
PROGRESS_PLACEHOLDER = _PROGRESS_PLACEHOLDER
_CANCELLED_RESPONSE_NOTE = "**[Response cancelled by user]**"
CANCELLED_RESPONSE_NOTE = _CANCELLED_RESPONSE_NOTE
_RESTART_INTERRUPTED_RESPONSE_NOTE = "**[Response interrupted by service restart]**"
_NO_VISIBLE_TEXT_AFTER_THINKING_NOTE = "**[Model emitted no visible text content after thinking. Please retry.]**"
_STREAM_ERROR_RESPONSE_NOTE = "**[Response interrupted by an error"
_VISIBLE_TOOL_MARKER_LINE_PATTERN = re.compile(r"^\s*🔧 `[^`]+` \[\d+\](?: ⏳)?\s*$")
_StreamInputChunk = (
    str | StructuredStreamChunk | RunContentEvent | RunCompletedEvent | ToolCallStartedEvent | ToolCallCompletedEvent
)
_TerminalStreamStatus = Literal["completed", "cancelled", "error"]


@dataclass(slots=True)
class StreamDeliveryState:
    """Capture one streaming delivery's latest visible event and terminal outcome."""

    event_id: str | None = None
    accumulated_text: str = ""
    finalization_outcome: StreamTransportOutcome | None = None
    suppressed_and_cleaned: bool = False
    repair_text: str | None = None
    repair_tool_trace: list[ToolTraceEntry] | None = None
    repair_extra_content: dict[str, Any] | None = None
    repair_option_map: dict[str, str] | None = None
    repair_options_list: list[dict[str, str]] | None = None


class StreamingDeliveryError(Exception):
    """Preserve the finalized stream state when delivery fails mid-response."""

    def __init__(
        self,
        error: Exception,
        *,
        event_id: str | None,
        accumulated_text: str,
        tool_trace: list[ToolTraceEntry],
        transport_outcome: StreamTransportOutcome | None = None,
    ) -> None:
        super().__init__(str(error))
        self.error = error
        self.event_id = event_id
        self.accumulated_text = accumulated_text
        self.tool_trace = tool_trace.copy()
        self.transport_outcome = transport_outcome


class StreamingDeliveryCancelled(asyncio.CancelledError):
    """Preserve finalized stream state when delivery is cancelled mid-response."""

    def __init__(
        self,
        error: asyncio.CancelledError,
        *,
        event_id: str | None,
        accumulated_text: str,
        tool_trace: list[ToolTraceEntry],
        transport_outcome: StreamTransportOutcome | None = None,
    ) -> None:
        super().__init__(*error.args)
        self.error = error
        self.event_id = event_id
        self.accumulated_text = accumulated_text
        self.tool_trace = tool_trace.copy()
        self.transport_outcome = transport_outcome


def _build_streaming_delivery_error(
    streaming: StreamingResponse,
    error: Exception,
    *,
    tool_trace_collector: list[ToolTraceEntry] | None,
) -> StreamingDeliveryError:
    """Build one normalized delivery failure from the current committed stream state."""
    if tool_trace_collector is not None:
        tool_trace_collector[:] = streaming.tool_trace
    return StreamingDeliveryError(
        error,
        event_id=streaming.event_id,
        accumulated_text=streaming.accumulated_text,
        tool_trace=streaming.tool_trace,
    )


def _raise_nonterminal_delivery_error(error: Exception) -> NoReturn:
    """Raise one wrapped non-terminal delivery error for unified rollback handling."""
    raise _NonTerminalDeliveryError(error) from error


def _format_stream_error_note(error: Exception) -> str:
    """Return a concise user-facing note for stream-time exceptions."""
    normalized_error = " ".join(str(error).split())
    if not normalized_error:
        return f"{_STREAM_ERROR_RESPONSE_NOTE}. Please retry.]**"
    if len(normalized_error) > 220:
        normalized_error = f"{normalized_error[:219]}…"
    return f"{_STREAM_ERROR_RESPONSE_NOTE}: {normalized_error}]**"


def _merge_final_completion_content(accumulated_text: str, final_text: str) -> str:
    """Preserve visible tool markers when a provider emits canonical final content."""
    tool_marker_lines = [
        line for line in accumulated_text.splitlines() if _VISIBLE_TOOL_MARKER_LINE_PATTERN.fullmatch(line)
    ]
    if not tool_marker_lines:
        return final_text
    tool_marker_block = "\n\n".join(tool_marker_lines)
    return f"{tool_marker_block}\n\n{final_text}" if final_text else tool_marker_block


def is_interrupted_partial_reply(text: object) -> bool:
    """Return True when text carries a terminal interrupted partial-reply marker."""
    if not isinstance(text, str):
        return False
    trimmed_text = text.rstrip()
    return trimmed_text.endswith(
        (
            _CANCELLED_RESPONSE_NOTE,
            _RESTART_INTERRUPTED_RESPONSE_NOTE,
            " [cancelled]",
            " [error]",
        ),
    ) or (_STREAM_ERROR_RESPONSE_NOTE in trimmed_text)


def clean_partial_reply_text(text: str) -> str:
    """Strip partial-reply status notes from persisted text."""
    cleaned = text.rstrip()

    for marker in (
        " [cancelled]",
        " [error]",
        _CANCELLED_RESPONSE_NOTE,
        _RESTART_INTERRUPTED_RESPONSE_NOTE,
    ):
        if cleaned.endswith(marker):
            cleaned = cleaned[: -len(marker)].rstrip()

    if _STREAM_ERROR_RESPONSE_NOTE in cleaned:
        cleaned = cleaned.split(_STREAM_ERROR_RESPONSE_NOTE, 1)[0].rstrip()

    if cleaned == _PROGRESS_PLACEHOLDER or not cleaned or not any(char.isalnum() for char in cleaned):
        return ""
    return cleaned


def build_restart_interrupted_body(text: str) -> str:
    """Return restart-note text for a stale in-progress message body."""
    stripped_text = text.rstrip()
    if not stripped_text or stripped_text == _PROGRESS_PLACEHOLDER:
        return _RESTART_INTERRUPTED_RESPONSE_NOTE
    return f"{stripped_text}\n\n{_RESTART_INTERRUPTED_RESPONSE_NOTE}"


@dataclass(frozen=True)
class _CommittedDeliveryState:
    """One frozen non-terminal stream state that definitely reached Matrix."""

    accumulated_text: str
    tool_trace: list[ToolTraceEntry]
    placeholder_progress_sent: bool


@dataclass(frozen=True)
class _PreparedStreamingDelivery:
    """One frozen non-terminal delivery attempt."""

    content: dict[str, Any]
    display_text: str
    committed_state: _CommittedDeliveryState
    had_warmup_suffix: bool


@dataclass
class StreamingResponse:
    """Manages a streaming response with incremental message updates."""

    room_id: str
    reply_to_event_id: str | None
    thread_id: str | None
    sender_domain: str
    config: Config
    runtime_paths: RuntimePaths
    target: MessageTarget | None = None
    accumulated_text: str = ""
    event_id: str | None = None  # None until first message sent
    last_update: float = 0.0
    update_interval: float = 5.0
    min_update_interval: float = 0.5
    interval_ramp_seconds: float = 15.0
    update_char_threshold: int = 240
    min_update_char_threshold: int = 48
    min_char_update_interval: float = 0.35
    progress_update_interval: float = 1.0
    latest_thread_event_id: str | None = None  # For MSC3440 compliance
    room_mode: bool = False  # When True, skip all thread relations (for bridges/mobile)
    show_tool_calls: bool = True  # When False, omit inline tool call text and tool-trace metadata
    tool_trace: list[ToolTraceEntry] = field(default_factory=list)
    extra_content: dict[str, Any] | None = None
    stream_started_at: float | None = None
    chars_since_last_update: int = 0
    placeholder_progress_sent: bool = False
    pipeline_timing: DispatchPipelineTiming | None = None
    conversation_cache: ConversationCacheProtocol | None = None
    visible_event_id_callback: Callable[[str], None] | None = None
    observed_reasoning_content: bool = False
    observed_tool_calls: int = 0

    def __post_init__(self) -> None:
        """Normalize transitional target fields onto one canonical target."""
        if self.target is None:
            self.target = MessageTarget.resolve(
                room_id=self.room_id,
                thread_id=self.thread_id,
                reply_to_event_id=self.reply_to_event_id,
                room_mode=self.room_mode,
            )
        self.room_id = self.target.room_id
        self.thread_id = self.target.resolved_thread_id
        self.reply_to_event_id = self.target.reply_to_event_id
        self.room_mode = self.target.is_room_mode

    def _update(self, new_chunk: str) -> None:
        """Append new chunk to accumulated text."""
        self.accumulated_text += new_chunk
        self.chars_since_last_update += len(new_chunk)

    def _ensure_hidden_tool_gap(self) -> None:
        """Insert a single placeholder gap for hidden tool calls."""
        if not self.accumulated_text.endswith("\n\n"):
            self._update("\n\n")

    def _current_update_interval(self, current_time: float) -> float:
        """Return the current throttling interval.

        Streaming starts with faster edits, then ramps toward the steady-state
        interval to reduce edit noise for long responses.
        """
        if self.stream_started_at is None or self.interval_ramp_seconds <= 0:
            return self.update_interval

        fast_interval = min(self.min_update_interval, self.update_interval)
        elapsed = max(0.0, current_time - self.stream_started_at)
        if elapsed >= self.interval_ramp_seconds:
            return self.update_interval

        progress = elapsed / self.interval_ramp_seconds
        return fast_interval + (self.update_interval - fast_interval) * progress

    def _current_char_threshold(self, current_time: float) -> int:
        """Return the current character threshold for triggering updates."""
        steady_threshold = max(1, self.update_char_threshold)
        if self.stream_started_at is None or self.interval_ramp_seconds <= 0:
            return steady_threshold

        fast_threshold = max(1, min(self.min_update_char_threshold, self.update_char_threshold))
        elapsed = max(0.0, current_time - self.stream_started_at)
        if elapsed >= self.interval_ramp_seconds:
            return steady_threshold

        progress = elapsed / self.interval_ramp_seconds
        threshold = fast_threshold + (self.update_char_threshold - fast_threshold) * progress
        return max(1, round(threshold))

    async def _throttled_send(self, client: nio.AsyncClient, *, progress_hint: bool = False) -> None:
        """Send/edit when either time or character thresholds are met."""
        current_time = time.time()
        if self.stream_started_at is None:
            self.stream_started_at = current_time
        current_interval = self._current_update_interval(current_time)
        if progress_hint:
            current_interval = min(current_interval, self.progress_update_interval)

        elapsed_since_last_update = current_time - self.last_update
        time_triggered = elapsed_since_last_update >= current_interval
        char_triggered = (
            self.chars_since_last_update >= self._current_char_threshold(current_time)
            and elapsed_since_last_update >= self.min_char_update_interval
        )
        should_send = time_triggered or char_triggered
        allow_empty_progress = progress_hint and not self.accumulated_text.strip()
        if should_send and (self.accumulated_text.strip() or allow_empty_progress):
            await self._send_or_edit_message(client, allow_empty_progress=allow_empty_progress)
            self.last_update = current_time
            self.chars_since_last_update = 0

    async def update_content(self, new_chunk: str, client: nio.AsyncClient) -> None:
        """Add new content and potentially update the message."""
        self._warmup_state.clear_terminal_failures()
        self._update(new_chunk)
        await self._throttled_send(client)

    def _prepare_terminal_text_and_status(
        self,
        *,
        cancelled: bool,
        restart_interrupted: bool,
        error: Exception | None,
    ) -> _TerminalStreamStatus:
        """Apply terminal text adjustments and return the terminal stream status."""
        ai_run_payload = self.extra_content.get(AI_RUN_METADATA_KEY) if self.extra_content is not None else None
        observed_nonvisible_activity = (
            self.observed_reasoning_content or self.observed_tool_calls > 0 or self.placeholder_progress_sent
        )
        no_visible_text_error = (
            error is None
            and not restart_interrupted
            and not cancelled
            and not self.accumulated_text.strip()
            and observed_nonvisible_activity
        )
        if no_visible_text_error:
            self.accumulated_text = _NO_VISIBLE_TEXT_AFTER_THINKING_NOTE
            if self.extra_content is not None:
                self.extra_content[STREAM_STATUS_KEY] = STREAM_STATUS_ERROR
                if isinstance(ai_run_payload, dict):
                    ai_run_payload["status"] = STREAM_STATUS_ERROR
            return STREAM_STATUS_ERROR
        if error is not None:
            stripped_text = self.accumulated_text.rstrip()
            error_note = _format_stream_error_note(error)
            self.accumulated_text = f"{stripped_text}\n\n{error_note}" if stripped_text else error_note
            return STREAM_STATUS_ERROR
        if restart_interrupted:
            self.accumulated_text = build_restart_interrupted_body(self.accumulated_text)
            return STREAM_STATUS_ERROR
        if cancelled:
            stripped_text = self.accumulated_text.rstrip()
            self.accumulated_text = (
                f"{stripped_text}\n\n{_CANCELLED_RESPONSE_NOTE}" if stripped_text else _CANCELLED_RESPONSE_NOTE
            )
            return STREAM_STATUS_CANCELLED
        return STREAM_STATUS_COMPLETED

    async def finalize(
        self,
        client: nio.AsyncClient,
        *,
        cancelled: bool = False,
        restart_interrupted: bool = False,
        error: Exception | None = None,
    ) -> StreamTransportOutcome:
        """Send the terminal update and return immutable transport facts."""
        final_stream_status = self._prepare_terminal_text_and_status(
            cancelled=cancelled,
            restart_interrupted=restart_interrupted,
            error=error,
        )
        # When a placeholder message exists but no real text arrived,
        # still edit the message to finalize the stream status.
        has_placeholder = (
            self.event_id is not None and self.placeholder_progress_sent and not self.accumulated_text.strip()
        )
        terminal_operation: StreamTerminalOperation = "edit" if self.event_id is not None else "send"
        text_to_send = self.accumulated_text if self.accumulated_text.strip() else _PROGRESS_PLACEHOLDER
        response = interactive.parse_and_format_interactive(text_to_send, extract_mapping=True)
        rendered_body = response.formatted_text if (self.accumulated_text.strip() or has_placeholder) else None
        visible_body_state: Literal["none", "placeholder_only", "visible_body"]
        if rendered_body is None:
            terminal_operation = "none"
            visible_body_state = "none"
            return StreamTransportOutcome(
                last_physical_stream_event_id=self.event_id,
                terminal_operation=terminal_operation,
                terminal_result="not_attempted",
                terminal_status=final_stream_status,
                rendered_body=None,
                visible_body_state=visible_body_state,
                option_map=None,
                options_list=None,
            )
        visible_body_state = "placeholder_only" if rendered_body == _PROGRESS_PLACEHOLDER else "visible_body"
        options_list = tuple(response.options_list) if response.options_list is not None else None
        try:
            send_succeeded = await self._send_or_edit_message(
                client,
                is_final=True,
                allow_empty_progress=has_placeholder,
                stream_status=final_stream_status,
            )
        except asyncio.CancelledError:
            logger.warning(
                "Terminal streaming update was cancelled before it landed",
                event_id=self.event_id,
                room_id=self.room_id,
                stream_status=final_stream_status,
                exc_info=True,
            )
            return StreamTransportOutcome(
                last_physical_stream_event_id=self.event_id,
                terminal_operation=terminal_operation,
                terminal_result="cancelled",
                terminal_status=final_stream_status,
                rendered_body=rendered_body,
                visible_body_state=visible_body_state,
                failure_reason="terminal_update_cancelled",
                option_map=response.option_map,
                options_list=options_list,
            )
        except Exception as exc:
            logger.warning(
                "Terminal streaming update raised after retries",
                event_id=self.event_id,
                room_id=self.room_id,
                stream_status=final_stream_status,
                reason=str(exc),
                exc_info=True,
            )
            return StreamTransportOutcome(
                last_physical_stream_event_id=self.event_id,
                terminal_operation=terminal_operation,
                terminal_result="failed",
                terminal_status=final_stream_status,
                rendered_body=rendered_body,
                visible_body_state=visible_body_state,
                failure_reason=f"terminal_update_exception:{exc.__class__.__name__}",
                option_map=response.option_map,
                options_list=options_list,
            )
        if not send_succeeded:
            logger.warning(
                "Failed to persist terminal stream status",
                event_id=self.event_id,
                room_id=self.room_id,
                stream_status=final_stream_status,
            )
            return StreamTransportOutcome(
                last_physical_stream_event_id=self.event_id,
                terminal_operation=terminal_operation,
                terminal_result="failed",
                terminal_status=final_stream_status,
                rendered_body=rendered_body,
                visible_body_state=visible_body_state,
                failure_reason="terminal_update_failed",
                option_map=response.option_map,
                options_list=options_list,
            )
        return StreamTransportOutcome(
            last_physical_stream_event_id=self.event_id,
            terminal_operation=terminal_operation,
            terminal_result="succeeded",
            terminal_status=final_stream_status,
            rendered_body=rendered_body,
            visible_body_state=visible_body_state,
            failure_reason=None,
            option_map=response.option_map,
            options_list=options_list,
        )

    async def _send_or_edit_message(
        self,
        client: nio.AsyncClient,
        is_final: bool = False,
        *,
        allow_empty_progress: bool = False,
        stream_status: str | None = None,
    ) -> bool:
        """Send new message or edit existing one."""
        prepared_delivery = self._prepare_delivery(
            is_final=is_final,
            allow_empty_progress=allow_empty_progress,
            stream_status=stream_status,
        )
        if prepared_delivery is None:
            return True

        is_initial_send = self.event_id is None
        send_succeeded = await self._send_content(
            client,
            content=prepared_delivery.content,
            display_text=prepared_delivery.display_text,
            retry_on_failure=is_final,
        )
        if not send_succeeded:
            if not is_final:
                action = "send initial" if is_initial_send else "edit"
                msg = f"Failed to {action} streaming message"
                raise RuntimeError(msg)
            return False

        if not is_final:
            self._warmup_state.note_nonterminal_delivery(
                had_warmup_suffix=prepared_delivery.had_warmup_suffix,
            )
            self._mark_delivery_committed(prepared_delivery.committed_state)
        else:
            self.placeholder_progress_sent = False
        return True

    def _prepare_delivery(
        self,
        *,
        is_final: bool,
        allow_empty_progress: bool,
        stream_status: str | None,
    ) -> _PreparedStreamingDelivery | None:
        """Freeze one exact outbound payload before awaiting Matrix I/O."""
        warmup_suffix_lines = self._warmup_state.render_lines(show_tool_calls=self.show_tool_calls)
        if not self.accumulated_text.strip() and not allow_empty_progress and not warmup_suffix_lines:
            return None

        assert self.target is not None
        effective_thread_id = self.target.resolved_thread_id

        text_to_send = self.accumulated_text if self.accumulated_text.strip() else _PROGRESS_PLACEHOLDER

        # Format the text (handles interactive questions if present)
        response = interactive.parse_and_format_interactive(text_to_send, extract_mapping=is_final)
        display_text = response.formatted_text

        # Only use latest_thread_event_id for the initial message (not edits)
        latest_for_message = self.latest_thread_event_id if self.event_id is None and not self.room_mode else None
        stream_status = self._resolve_stream_status(is_final=is_final, stream_status=stream_status)
        extra_content = dict(self.extra_content or {})
        extra_content[STREAM_STATUS_KEY] = stream_status

        content = format_message_with_mentions(
            config=self.config,
            runtime_paths=self.runtime_paths,
            text=display_text,
            sender_domain=self.sender_domain,
            thread_event_id=effective_thread_id,
            reply_to_event_id=self.target.reply_to_event_id,
            latest_thread_event_id=latest_for_message,
            tool_trace=self.tool_trace if self.show_tool_calls else None,
            extra_content=extra_content,
        )
        canonical_visible_body = content["body"]
        if warmup_suffix_lines:
            content[STREAM_VISIBLE_BODY_KEY] = canonical_visible_body
            warmup_suffix = "\n".join(warmup_suffix_lines)
            content[STREAM_WARMUP_SUFFIX_KEY] = warmup_suffix
            display_text = f"{display_text}\n\n{warmup_suffix}" if display_text else warmup_suffix
            content["body"] = f"{content['body']}\n\n{warmup_suffix}"
            suffix_html = "".join(f"<p>{escape(line)}</p>" for line in warmup_suffix_lines)
            content["formatted_body"] = f"{content['formatted_body']}{suffix_html}"

        return _PreparedStreamingDelivery(
            content=content,
            display_text=display_text,
            retry_on_failure=is_final and self.event_id is not None,
        )

    def _mark_delivery_committed(self, committed_state: _CommittedDeliveryState) -> None:
        """Snapshot the last non-terminal text/tool-trace state that actually reached Matrix."""
        self._last_delivered_text = committed_state.accumulated_text
        self._last_delivered_tool_trace = deepcopy(committed_state.tool_trace)
        self._last_placeholder_progress_sent = committed_state.placeholder_progress_sent
        self.placeholder_progress_sent = committed_state.placeholder_progress_sent

    def restore_last_delivered_state(self) -> None:
        """Discard buffered state that never reached Matrix after a delivery failure."""
        self.accumulated_text = self._last_delivered_text
        self.tool_trace = deepcopy(self._last_delivered_tool_trace)
        self.chars_since_last_update = 0
        self.placeholder_progress_sent = self._last_placeholder_progress_sent

    def apply_worker_progress_event(self, event: WorkerProgressEvent) -> bool:
        """Update side-band warmup state from one routed worker progress event."""
        return self._warmup_state.apply_event(event)

    def _resolve_stream_status(self, *, is_final: bool, stream_status: str | None) -> str:
        """Return the content status for the current send or edit."""
        if stream_status is not None:
            return stream_status
        if is_final:
            return STREAM_STATUS_COMPLETED
        if self.event_id is None:
            return STREAM_STATUS_PENDING
        return STREAM_STATUS_STREAMING

    async def _record_streaming_send(self, event_id: str, content_sent: dict[str, Any]) -> None:
        """Persist one just-sent streaming message into the conversation cache."""
        if self.conversation_cache is None:
            return
        self.conversation_cache.notify_outbound_message(self.room_id, event_id, content_sent)

    async def _record_streaming_edit(
        self,
        edit_event_id: str,
        *,
        content_sent: dict[str, Any],
    ) -> None:
        """Persist one just-sent streaming edit into the conversation cache."""
        if self.conversation_cache is None or self.event_id is None:
            return
        self.conversation_cache.notify_outbound_message(self.room_id, edit_event_id, content_sent)

    def _mark_first_visible_reply_if_needed(self) -> None:
        """Mark first visible reply timing once visible text exists."""
        if self.pipeline_timing is not None and self.accumulated_text.strip():
            self.pipeline_timing.mark_first_visible_reply("stream_update")

    async def _send_initial_content(self, client: nio.AsyncClient, *, content: dict[str, Any]) -> bool:
        """Send the initial streaming event."""
        delivered = await send_message_result(client, self.room_id, content)
        if delivered is None:
            return False
        self.event_id = delivered.event_id
        if self.visible_event_id_callback is not None:
            self.visible_event_id_callback(delivered.event_id)
        await self._record_streaming_send(delivered.event_id, delivered.content_sent)
        self._mark_first_visible_reply_if_needed()
        logger.debug("Initial streaming message sent", event_id=self.event_id)
        return True

    async def _edit_existing_content(
        self,
        client: nio.AsyncClient,
        *,
        content: dict[str, Any],
        display_text: str,
    ) -> bool:
        """Send one streaming edit event for the existing message."""
        assert self.event_id is not None
        delivered = await edit_message_result(client, self.room_id, self.event_id, content, display_text)
        if delivered is None:
            return False
        await self._record_streaming_edit(delivered.event_id, content_sent=delivered.content_sent)
        self._mark_first_visible_reply_if_needed()
        return True

    async def _send_content(
        self,
        client: nio.AsyncClient,
        *,
        content: dict[str, Any],
        display_text: str,
        retry_on_failure: bool = False,
    ) -> bool:
        """Send a new event or edit the existing one."""
        max_retries = 5 if retry_on_failure else 0
        total_attempts = max_retries + 1
        for attempt in range(1, total_attempts + 1):
            try:
                if self.event_id is None:
                    logger.debug("Sending initial streaming message", attempt=attempt)
                    if await self._send_initial_content(client, content=content):
                        return True
                    logger.error("Failed to send initial streaming message", attempt=attempt)
                else:
                    logger.debug("Editing streaming message", event_id=self.event_id, attempt=attempt)
                    if await self._edit_existing_content(client, content=content, display_text=display_text):
                        return True
                    logger.error("Failed to edit streaming message", attempt=attempt)
            except Exception:
                logger.warning(
                    "Streaming update attempt raised an exception",
                    attempt=attempt,
                    event_id=self.event_id,
                    room_id=self.room_id,
                    exc_info=True,
                )
                if attempt == total_attempts:
                    raise
            if attempt < total_attempts:
                backoff_seconds = 2**attempt
                logger.warning(
                    "Retrying failed terminal streaming update",
                    attempt=attempt,
                    event_id=self.event_id,
                    room_id=self.room_id,
                    backoff_seconds=backoff_seconds,
                )
                await asyncio.sleep(backoff_seconds)
        return False


class ReplacementStreamingResponse(StreamingResponse):
    """StreamingResponse variant that replaces content instead of appending.

    Useful for structured live rendering where the full document is rebuilt
    on each tick and we want the message to reflect the latest full view,
    not incremental concatenation.
    """

    def _update(self, new_chunk: str) -> None:
        """Replace accumulated text with new chunk."""
        self.accumulated_text = new_chunk
        self.chars_since_last_update += len(new_chunk)


async def _consume_streaming_chunks(  # noqa: C901, PLR0912, PLR0915
    client: nio.AsyncClient,
    response_stream: AsyncIterator[_StreamInputChunk],
    streaming: StreamingResponse,
) -> None:
    """Consume stream chunks and apply incremental message updates."""
    pending_tools: list[tuple[str, int]] = []

    async for chunk in response_stream:
        # Handle different types of chunks from the stream
        if isinstance(chunk, str):
            text_chunk = chunk
        elif isinstance(chunk, StructuredStreamChunk):
            text_chunk = chunk.content
            if chunk.tool_trace is not None:
                streaming.tool_trace = _merge_tool_trace(streaming.tool_trace, chunk.tool_trace)
        elif isinstance(chunk, RunContentEvent):
            if chunk.reasoning_content:
                streaming.observed_reasoning_content = True
            if chunk.content:
                text_chunk = str(chunk.content)
            else:
                await streaming._throttled_send(client, progress_hint=True)
                continue
        elif isinstance(chunk, RunCompletedEvent):
            if chunk.reasoning_content:
                streaming.observed_reasoning_content = True
            if chunk.content is not None:
                streaming.accumulated_text = _merge_final_completion_content(
                    streaming.accumulated_text,
                    str(chunk.content),
                )
            continue
        elif isinstance(chunk, ToolCallStartedEvent):
            if chunk.tool is not None:
                streaming.observed_tool_calls += 1
            if not streaming.show_tool_calls:
                if chunk.tool is not None:
                    streaming._ensure_hidden_tool_gap()
                await streaming._throttled_send(client, progress_hint=True)
                continue

            tool_index = len(streaming.tool_trace) + 1
            text_chunk, trace_entry = format_tool_started_event(chunk.tool, tool_index=tool_index)
            if trace_entry is not None:
                streaming.tool_trace.append(trace_entry)
                pending_tools.append((trace_entry.tool_name, tool_index))
        elif isinstance(chunk, ToolCallCompletedEvent):
            info = extract_tool_completed_info(chunk.tool)
            if info:
                tool_name, result = info
                if streaming.show_tool_calls:
                    match_pos = next(
                        (pos for pos in range(len(pending_tools) - 1, -1, -1) if pending_tools[pos][0] == tool_name),
                        None,
                    )
                    if match_pos is None:
                        logger.warning(
                            "Missing pending tool start in streaming response; skipping completion marker",
                            tool_name=tool_name,
                        )
                        await streaming._throttled_send(client, progress_hint=True)
                        continue
                    _, tool_index = pending_tools.pop(match_pos)
                    streaming.accumulated_text, trace_entry = complete_pending_tool_block(
                        streaming.accumulated_text,
                        tool_name,
                        result,
                        tool_index=tool_index,
                    )
                    if 0 < tool_index <= len(streaming.tool_trace):
                        existing_entry = streaming.tool_trace[tool_index - 1]
                        existing_entry.type = "tool_call_completed"
                        existing_entry.result_preview = trace_entry.result_preview
                        existing_entry.truncated = existing_entry.truncated or trace_entry.truncated
                    else:
                        logger.warning(
                            "Missing tool trace slot in streaming response for completion",
                            tool_name=tool_name,
                            tool_index=tool_index,
                            trace_len=len(streaming.tool_trace),
                        )
                else:
                    await streaming._throttled_send(client, progress_hint=True)
                    continue
                await streaming._throttled_send(client)
                continue
            text_chunk = ""
        else:
            logger.debug("unhandled_streaming_event_type", event_type=type(chunk).__name__)
            continue

        if text_chunk:
            await streaming.update_content(text_chunk, client)


async def send_streaming_response(  # noqa: C901, PLR0912
    client: nio.AsyncClient,
    room_id: str,
    reply_to_event_id: str | None,
    thread_id: str | None,
    sender_domain: str,
    config: Config,
    runtime_paths: RuntimePaths,
    response_stream: AsyncIterator[StreamInputChunk],
    *,
    streaming_cls: type[StreamingResponse] = StreamingResponse,
    header: str | None = None,
    existing_event_id: str | None = None,
    adopt_existing_placeholder: bool = False,
    room_mode: bool = False,
    target: MessageTarget | None = None,
    show_tool_calls: bool = True,
    extra_content: dict[str, Any] | None = None,
    tool_trace_collector: list[ToolTraceEntry] | None = None,
    stream_state: StreamDeliveryState | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
    visible_event_id_callback: Callable[[str], None] | None = None,
    latest_thread_event_id: str | None = None,
    conversation_cache: ConversationCacheProtocol | None = None,
) -> StreamTransportOutcome:
    """Stream chunks to a Matrix room and return the canonical transport outcome."""
    resolved_target = target or MessageTarget.resolve(
        room_id=room_id,
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        room_mode=room_mode,
    )

    sc = config.defaults.streaming
    streaming = streaming_cls(
        room_id=room_id,
        reply_to_event_id=reply_to_event_id,
        thread_id=thread_id,
        sender_domain=sender_domain,
        config=config,
        runtime_paths=runtime_paths,
        target=resolved_target,
        latest_thread_event_id=latest_thread_event_id,
        room_mode=resolved_target.is_room_mode,
        show_tool_calls=show_tool_calls,
        extra_content=extra_content,
        update_interval=sc.update_interval,
        min_update_interval=sc.min_update_interval,
        interval_ramp_seconds=sc.interval_ramp_seconds,
        pipeline_timing=pipeline_timing,
        conversation_cache=conversation_cache,
        visible_event_id_callback=visible_event_id_callback,
    )

    # Ensure the first chunk triggers an initial send immediately
    streaming.last_update = float("-inf")

    if existing_event_id:
        streaming.event_id = existing_event_id
        if visible_event_id_callback is not None:
            visible_event_id_callback(existing_event_id)
        streaming.accumulated_text = ""
        streaming.placeholder_progress_sent = adopt_existing_placeholder

    stream_error: Exception | None = None
    cancellation_error: asyncio.CancelledError | None = None
    cancellation_traceback = None
    transport_outcome: StreamTransportOutcome | None = None
    try:
        if header:
            await streaming.update_content(header, client)
        await _consume_streaming_chunks(client, response_stream, streaming)
    except asyncio.CancelledError as exc:
        cancellation_error = exc
        cancellation_traceback = exc.__traceback__
        if is_sync_restart_cancel(exc):
            logger.info("Streaming response interrupted by sync restart", message_id=streaming.event_id)
            transport_outcome = await streaming.finalize(client, restart_interrupted=True)
        else:
            logger.warning(
                "Streaming response cancelled — traceback for diagnosis",
                message_id=streaming.event_id,
                exc_info=True,
            )
            transport_outcome = await streaming.finalize(client, cancelled=True)
    except Exception as e:
        stream_error = e
        logger.exception("Streaming response failed", error=str(e))
        transport_outcome = await streaming.finalize(client, error=e)
    else:
        transport_outcome = await streaming.finalize(client)
    finally:
        if tool_trace_collector is not None:
            tool_trace_collector[:] = streaming.tool_trace
        if stream_state is not None:
            stream_state.event_id = streaming.event_id
            stream_state.accumulated_text = streaming.accumulated_text
            stream_state.finalization_outcome = transport_outcome

    if cancellation_error is not None:
        raise StreamingDeliveryCancelled(
            cancellation_error,
            event_id=streaming.event_id,
            accumulated_text=streaming.accumulated_text,
            tool_trace=streaming.tool_trace,
            transport_outcome=transport_outcome,
        ).with_traceback(cancellation_traceback)
    if stream_error is not None:
        raise StreamingDeliveryError(
            stream_error,
            event_id=streaming.event_id,
            accumulated_text=streaming.accumulated_text,
            tool_trace=streaming.tool_trace,
            transport_outcome=transport_outcome,
        ) from stream_error

    assert transport_outcome is not None
    return transport_outcome
