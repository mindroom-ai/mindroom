"""Streaming response implementation for real-time message updates."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass, field
from html import escape
from typing import TYPE_CHECKING, Any, NoReturn

from agno.run.agent import RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent

from mindroom import interactive
from mindroom.constants import (
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import edit_message_result, send_message_result
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.message_target import MessageTarget
from mindroom.orchestration.runtime import is_sync_restart_cancel
from mindroom.tool_system.events import (
    StructuredStreamChunk,
    ToolTraceEntry,
    complete_pending_tool_block,
    extract_tool_completed_info,
    format_tool_started_event,
)
from mindroom.tool_system.runtime_context import worker_progress_pump_scope

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol
    from mindroom.timing import DispatchPipelineTiming
    from mindroom.tool_system.runtime_context import WorkerProgressEvent, WorkerProgressPump
    from mindroom.workers.models import WorkerReadyProgress

logger = get_logger(__name__)

_PROGRESS_PLACEHOLDER = "Thinking..."
PROGRESS_PLACEHOLDER = _PROGRESS_PLACEHOLDER
_CANCELLED_RESPONSE_NOTE = "**[Response cancelled by user]**"
CANCELLED_RESPONSE_NOTE = _CANCELLED_RESPONSE_NOTE
_RESTART_INTERRUPTED_RESPONSE_NOTE = "**[Response interrupted by service restart]**"
_STREAM_ERROR_RESPONSE_NOTE = "**[Response interrupted by an error"
_StreamInputChunk = str | StructuredStreamChunk | RunContentEvent | ToolCallStartedEvent | ToolCallCompletedEvent


class StreamingDeliveryError(Exception):
    """Preserve the finalized stream state when delivery fails mid-response."""

    def __init__(
        self,
        error: Exception,
        *,
        event_id: str | None,
        accumulated_text: str,
        tool_trace: list[ToolTraceEntry],
    ) -> None:
        super().__init__(str(error))
        self.error = error
        self.event_id = event_id
        self.accumulated_text = accumulated_text
        self.tool_trace = tool_trace.copy()


def _format_stream_error_note(error: Exception) -> str:
    """Return a concise user-facing note for stream-time exceptions."""
    normalized_error = " ".join(str(error).split())
    if not normalized_error:
        return f"{_STREAM_ERROR_RESPONSE_NOTE}. Please retry.]**"
    if len(normalized_error) > 220:
        normalized_error = f"{normalized_error[:219]}…"
    return f"{_STREAM_ERROR_RESPONSE_NOTE}: {normalized_error}]**"


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


def _longest_common_prefix_len(first: list[ToolTraceEntry], second: list[ToolTraceEntry]) -> int:
    """Return the number of leading tool-trace entries shared by both lists."""
    max_len = min(len(first), len(second))
    index = 0
    while index < max_len and first[index] == second[index]:
        index += 1
    return index


def _merge_tool_trace(existing: list[ToolTraceEntry], incoming: list[ToolTraceEntry]) -> list[ToolTraceEntry]:
    """Merge a trace snapshot without dropping entries when stream styles are mixed."""
    if not existing:
        return incoming.copy()
    if not incoming:
        return existing.copy()

    shared_prefix = _longest_common_prefix_len(existing, incoming)
    if shared_prefix == len(existing):
        # Incoming is newer or equal.
        return incoming.copy()
    if shared_prefix == len(incoming):
        # Incoming is an older prefix; keep current entries.
        return existing.copy()

    # Diverged snapshots with equal-or-greater length are typically newer
    # "full snapshot" replacements (e.g. pending -> completed in place).
    if len(incoming) >= len(existing):
        return incoming.copy()

    # Shorter divergent snapshot is treated as stale; keep current entries.
    return existing.copy()


def _shorten_warmup_error(error: str | None) -> str:
    """Return a concise one-line startup failure message."""
    normalized_error = " ".join((error or "Worker startup failed").split())
    if len(normalized_error) > 180:
        normalized_error = f"{normalized_error[:179]}…"
    return normalized_error


@dataclass
class _ActiveWarmup:
    """Live side-band worker warmup state rendered below the current stream body."""

    worker_key: str
    backend_name: str
    tool_labels: list[str]
    last_event: WorkerReadyProgress


@dataclass(frozen=True, slots=True)
class _DeliveryRequest:
    """One non-terminal stream delivery request for the single delivery owner."""

    progress_hint: bool = False
    force_refresh: bool = False
    allow_empty_progress: bool = False


def _render_worker_status_line(warmup: _ActiveWarmup, *, show_tool_calls: bool) -> str:
    """Render one worker warmup line without leaking hidden tool metadata."""
    labels = ", ".join(warmup.tool_labels)
    if show_tool_calls and labels:
        waiting_copy = f"Preparing isolated worker for {labels}..."
        failure_copy = f"Worker startup failed for {labels}"
    else:
        waiting_copy = "Preparing isolated worker..."
        failure_copy = "Worker startup failed"

    phase = warmup.last_event.phase
    if phase == "failed":
        error = _shorten_warmup_error(warmup.last_event.error)
        suffix = "" if error.endswith((".", "!", "?")) else "."
        return f"⚠️ {failure_copy}: {error}{suffix}"
    if phase == "cold_start":
        return f"⏳ {waiting_copy}"

    elapsed_seconds = max(1, int(warmup.last_event.elapsed_seconds))
    return f"⏳ {waiting_copy} {elapsed_seconds}s elapsed."


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
    _active_warmups: dict[str, _ActiveWarmup] = field(default_factory=dict, init=False, repr=False)
    _last_send_had_warmup_suffix: bool = field(default=False, init=False, repr=False)
    _needs_warmup_clear_edit: bool = field(default=False, init=False, repr=False)

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
        self._clear_terminal_warmups()
        self._update(new_chunk)
        await self._throttled_send(client)

    async def finalize(
        self,
        client: nio.AsyncClient,
        *,
        cancelled: bool = False,
        restart_interrupted: bool = False,
        error: Exception | None = None,
    ) -> None:
        """Send final message update."""
        self._active_warmups.clear()
        self._needs_warmup_clear_edit = False
        if error is not None:
            stripped_text = self.accumulated_text.rstrip()
            error_note = _format_stream_error_note(error)
            self.accumulated_text = f"{stripped_text}\n\n{error_note}" if stripped_text else error_note
        elif restart_interrupted:
            self.accumulated_text = build_restart_interrupted_body(self.accumulated_text)
        elif cancelled:
            stripped_text = self.accumulated_text.rstrip()
            self.accumulated_text = (
                f"{stripped_text}\n\n{_CANCELLED_RESPONSE_NOTE}" if stripped_text else _CANCELLED_RESPONSE_NOTE
            )

        # When a placeholder message exists but no real text arrived,
        # still edit the message to finalize the stream status.
        has_placeholder = (
            self.event_id is not None and self.placeholder_progress_sent and not self.accumulated_text.strip()
        )
        final_stream_status = STREAM_STATUS_COMPLETED
        if error is not None or restart_interrupted:
            final_stream_status = STREAM_STATUS_ERROR
        elif cancelled:
            final_stream_status = STREAM_STATUS_CANCELLED
        send_succeeded = await self._send_or_edit_message(
            client,
            is_final=True,
            allow_empty_progress=has_placeholder,
            stream_status=final_stream_status,
        )
        if not send_succeeded:
            logger.warning(
                "Failed to persist terminal stream status",
                event_id=self.event_id,
                room_id=self.room_id,
                stream_status=final_stream_status,
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
        warmup_suffix_lines = self._render_warmup_suffix_lines()
        if not self.accumulated_text.strip() and not allow_empty_progress and not warmup_suffix_lines:
            return True

        assert self.target is not None
        effective_thread_id = self.target.resolved_thread_id

        text_to_send = self.accumulated_text if self.accumulated_text.strip() else _PROGRESS_PLACEHOLDER

        # Format the text (handles interactive questions if present)
        response = interactive.parse_and_format_interactive(text_to_send, extract_mapping=False)
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
        if warmup_suffix_lines:
            warmup_suffix = "\n".join(warmup_suffix_lines)
            display_text = f"{display_text}\n\n{warmup_suffix}" if display_text else warmup_suffix
            content["body"] = f"{content['body']}\n\n{warmup_suffix}"
            suffix_html = "".join(f"<p>{escape(line)}</p>" for line in warmup_suffix_lines)
            content["formatted_body"] = f"{content['formatted_body']}{suffix_html}"

        send_succeeded = await self._send_content(
            client,
            content=content,
            display_text=display_text,
            retry_on_failure=is_final,
        )
        if send_succeeded:
            self._last_send_had_warmup_suffix = bool(warmup_suffix_lines)
            self._needs_warmup_clear_edit = False
            if not is_final:
                self.placeholder_progress_sent = not self.accumulated_text.strip()
            else:
                self.placeholder_progress_sent = False
        return send_succeeded

    def _clear_terminal_warmups(self) -> None:
        """Drop failed warmup notices once the stream resumes with normal content."""
        failed_worker_keys = [
            worker_key for worker_key, warmup in self._active_warmups.items() if warmup.last_event.phase == "failed"
        ]
        for worker_key in failed_worker_keys:
            self._active_warmups.pop(worker_key, None)

    def _render_warmup_suffix_lines(self) -> list[str]:
        """Render all active worker warmup notices as side-band suffix lines."""
        if not self._active_warmups:
            return []

        return [
            _render_worker_status_line(warmup, show_tool_calls=self.show_tool_calls)
            for warmup in self._active_warmups.values()
        ]

    def apply_worker_progress_event(self, event: WorkerProgressEvent) -> bool:
        """Update side-band warmup state from one routed worker progress event."""
        progress = event.progress
        worker_key = progress.worker_key
        if progress.phase == "ready":
            removed = self._active_warmups.pop(worker_key, None)
            if removed is None:
                return False
            if not self._active_warmups and self._last_send_had_warmup_suffix:
                self._needs_warmup_clear_edit = True
            return True

        tool_label = f"{event.tool_name}.{event.function_name}"
        self._needs_warmup_clear_edit = False
        warmup = self._active_warmups.get(worker_key)
        if warmup is None:
            self._active_warmups[worker_key] = _ActiveWarmup(
                worker_key=worker_key,
                backend_name=progress.backend_name,
                tool_labels=[tool_label],
                last_event=progress,
            )
            return True

        if tool_label not in warmup.tool_labels:
            warmup.tool_labels.append(tool_label)
        warmup.backend_name = progress.backend_name
        warmup.last_event = progress
        return True

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
        attempts = 2 if retry_on_failure else 1
        for attempt in range(1, attempts + 1):
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
                if attempt == attempts:
                    raise
            if attempt < attempts:
                logger.warning(
                    "Retrying failed terminal streaming update",
                    attempt=attempt,
                    event_id=self.event_id,
                    room_id=self.room_id,
                )
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
    response_stream: AsyncIterator[_StreamInputChunk],
    streaming: StreamingResponse,
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
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
        elif isinstance(chunk, RunContentEvent) and chunk.content:
            text_chunk = str(chunk.content)
        elif isinstance(chunk, ToolCallStartedEvent):
            if not streaming.show_tool_calls:
                if chunk.tool is not None:
                    streaming._ensure_hidden_tool_gap()
                _queue_delivery_request(delivery_queue, progress_hint=True)
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
                        _queue_delivery_request(delivery_queue, progress_hint=True)
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
                    _queue_delivery_request(delivery_queue, progress_hint=True)
                    continue
                _queue_delivery_request(delivery_queue)
                continue
            text_chunk = ""
        else:
            logger.debug("unhandled_streaming_event_type", event_type=type(chunk).__name__)
            continue

        if text_chunk:
            streaming._clear_terminal_warmups()
            streaming._update(text_chunk)
            _queue_delivery_request(delivery_queue)


async def _drain_worker_progress_events(
    streaming: StreamingResponse,
    queue: asyncio.Queue[WorkerProgressEvent],
    pump: WorkerProgressPump,
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
) -> None:
    """Apply worker progress events to side-band state and refresh the current stream body."""
    while True:
        event = await queue.get()
        if pump.shutdown.is_set():
            return
        if streaming.apply_worker_progress_event(event):
            if pump.shutdown.is_set():
                return
            if streaming._needs_warmup_clear_edit:
                _queue_delivery_request(
                    delivery_queue,
                    force_refresh=True,
                    allow_empty_progress=not streaming.accumulated_text.strip(),
                )
                continue
            should_refresh = (
                bool(streaming.accumulated_text.strip())
                or bool(streaming._active_warmups)
                or event.progress.phase == "failed"
            )
            if not should_refresh:
                continue
            if pump.shutdown.is_set():
                return
            _queue_delivery_request(delivery_queue, progress_hint=True)


async def _shutdown_worker_progress_drain(
    pump: WorkerProgressPump,
    progress_task: asyncio.Task[None] | None,
) -> Exception | None:
    """Stop the worker-progress drain before terminal stream finalization."""
    pump.shutdown.set()
    if progress_task is None:
        return None
    if not progress_task.done():
        progress_task.cancel()
    try:
        await asyncio.wait_for(progress_task, timeout=0.5)
    except (asyncio.CancelledError, TimeoutError):
        return None
    except Exception as exc:
        return exc
    return None


def _raise_progress_delivery_error(error: Exception) -> NoReturn:
    """Raise a stored worker-progress delivery error from a helper."""
    raise error


def _queue_delivery_request(
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
    *,
    progress_hint: bool = False,
    force_refresh: bool = False,
    allow_empty_progress: bool = False,
) -> None:
    """Queue one non-terminal delivery request for the single delivery owner."""
    delivery_queue.put_nowait(
        _DeliveryRequest(
            progress_hint=progress_hint,
            force_refresh=force_refresh,
            allow_empty_progress=allow_empty_progress,
        ),
    )


async def _drive_stream_delivery(
    client: nio.AsyncClient,
    streaming: StreamingResponse,
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
) -> None:
    """Own all non-terminal stream sends and edits from one supervised task."""
    stop_after_current = False

    while True:
        request = await delivery_queue.get()
        if request is None:
            return

        merged_request = request
        while True:
            try:
                next_request = delivery_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if next_request is None:
                stop_after_current = True
                break
            merged_request = _DeliveryRequest(
                progress_hint=merged_request.progress_hint or next_request.progress_hint,
                force_refresh=merged_request.force_refresh or next_request.force_refresh,
                allow_empty_progress=merged_request.allow_empty_progress or next_request.allow_empty_progress,
            )

        if merged_request.force_refresh:
            sent = await streaming._send_or_edit_message(
                client,
                allow_empty_progress=merged_request.allow_empty_progress,
            )
            if sent:
                streaming.last_update = time.time()
                streaming.chars_since_last_update = 0
        else:
            await streaming._throttled_send(client, progress_hint=merged_request.progress_hint)

        if stop_after_current:
            return


async def _shutdown_stream_delivery(
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
    delivery_task: asyncio.Task[None] | None,
) -> Exception | None:
    """Stop the single delivery owner before terminal stream finalization."""
    if delivery_task is None:
        return None
    if not delivery_task.done():
        delivery_queue.put_nowait(None)
    try:
        await asyncio.wait_for(delivery_task, timeout=0.5)
    except asyncio.CancelledError:
        return None
    except TimeoutError:
        if not delivery_task.done():
            delivery_task.cancel()
            with suppress(asyncio.CancelledError, TimeoutError, Exception):
                await asyncio.wait_for(delivery_task, timeout=0.5)
        return None
    except Exception as exc:
        return exc
    return None


async def _cancel_stream_consumer(stream_task: asyncio.Task[None]) -> None:
    """Cancel chunk consumption after a progress-delivery failure wins ownership."""
    if stream_task.done():
        with suppress(asyncio.CancelledError, Exception):
            await stream_task
        return
    stream_task.cancel()
    with suppress(asyncio.CancelledError, TimeoutError, Exception):
        await asyncio.wait_for(stream_task, timeout=0.5)


async def _handle_auxiliary_task_completion(
    done_tasks: set[asyncio.Task[None]],
    task: asyncio.Task[None] | None,
    *,
    stream_task: asyncio.Task[None],
    monitored_tasks: set[asyncio.Task[None]],
) -> bool:
    """Surface one auxiliary-task failure through the normal streaming contract."""
    if task is None or task not in done_tasks:
        return False

    if task.cancelled():
        await _cancel_stream_consumer(stream_task)
        raise asyncio.CancelledError

    task_error = task.exception()
    if task_error is not None:
        await _cancel_stream_consumer(stream_task)
        if not isinstance(task_error, Exception):
            raise task_error
        _raise_progress_delivery_error(task_error)

    monitored_tasks.discard(task)
    return True


async def _consume_stream_with_progress_supervision(
    response_stream: AsyncIterator[_StreamInputChunk],
    streaming: StreamingResponse,
    progress_task: asyncio.Task[None] | None,
    delivery_task: asyncio.Task[None] | None,
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
) -> None:
    """Abort chunk consumption as soon as the worker-progress drain fails."""
    stream_task = asyncio.create_task(_consume_streaming_chunks(response_stream, streaming, delivery_queue))
    monitored_tasks: set[asyncio.Task[None]] = {stream_task}
    if progress_task is not None:
        monitored_tasks.add(progress_task)
    if delivery_task is not None:
        monitored_tasks.add(delivery_task)

    try:
        while True:
            done, _pending = await asyncio.wait(monitored_tasks, return_when=asyncio.FIRST_COMPLETED)

            if await _handle_auxiliary_task_completion(
                done,
                progress_task,
                stream_task=stream_task,
                monitored_tasks=monitored_tasks,
            ):
                progress_task = None

            if await _handle_auxiliary_task_completion(
                done,
                delivery_task,
                stream_task=stream_task,
                monitored_tasks=monitored_tasks,
            ):
                delivery_task = None

            if stream_task in done:
                await stream_task
                return
    finally:
        await _cancel_stream_consumer(stream_task)


async def send_streaming_response(  # noqa: C901, PLR0912, PLR0915
    client: nio.AsyncClient,
    room_id: str,
    reply_to_event_id: str | None,
    thread_id: str | None,
    sender_domain: str,
    config: Config,
    runtime_paths: RuntimePaths,
    response_stream: AsyncIterator[_StreamInputChunk],
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
    pipeline_timing: DispatchPipelineTiming | None = None,
    visible_event_id_callback: Callable[[str], None] | None = None,
    latest_thread_event_id: str | None = None,
    conversation_cache: ConversationCacheProtocol | None = None,
) -> tuple[str | None, str]:
    """Stream chunks to a Matrix room, returning (event_id, accumulated_text)."""
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

    if header:
        await streaming.update_content(header, client)

    worker_progress_queue: asyncio.Queue[WorkerProgressEvent] = asyncio.Queue()
    delivery_queue: asyncio.Queue[_DeliveryRequest | None] = asyncio.Queue()
    progress_task: asyncio.Task[None] | None = None
    delivery_task: asyncio.Task[None] | None = None
    loop = asyncio.get_running_loop()

    with worker_progress_pump_scope(loop, worker_progress_queue) as pump:
        delivery_task = asyncio.create_task(_drive_stream_delivery(client, streaming, delivery_queue))
        progress_task = asyncio.create_task(
            _drain_worker_progress_events(streaming, worker_progress_queue, pump, delivery_queue),
        )
        try:
            await _consume_stream_with_progress_supervision(
                response_stream,
                streaming,
                progress_task,
                delivery_task,
                delivery_queue,
            )
            progress_error = await _shutdown_worker_progress_drain(pump, progress_task)
            progress_task = None
            delivery_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)
            delivery_task = None
            if progress_error is not None:
                _raise_progress_delivery_error(progress_error)
            if delivery_error is not None:
                _raise_progress_delivery_error(delivery_error)
        except asyncio.CancelledError as exc:
            cleanup_error = await _shutdown_worker_progress_drain(pump, progress_task)
            progress_task = None
            delivery_cleanup_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)
            delivery_task = None
            if cleanup_error is not None:
                logger.warning(
                    "Worker progress drain raised during cancellation cleanup",
                    error=str(cleanup_error),
                )
            if delivery_cleanup_error is not None:
                logger.warning(
                    "Stream delivery controller raised during cancellation cleanup",
                    error=str(delivery_cleanup_error),
                )
            if is_sync_restart_cancel(exc):
                logger.info("Streaming response interrupted by sync restart", message_id=streaming.event_id)
                await streaming.finalize(client, restart_interrupted=True)
            else:
                logger.warning(
                    "Streaming response cancelled — traceback for diagnosis",
                    message_id=streaming.event_id,
                    exc_info=True,
                )
                await streaming.finalize(client, cancelled=True)
            raise
        except Exception as e:
            logger.exception("Streaming response failed", error=str(e))
            cleanup_error = await _shutdown_worker_progress_drain(pump, progress_task)
            progress_task = None
            delivery_cleanup_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)
            delivery_task = None
            if cleanup_error is not None and cleanup_error is not e:
                logger.warning(
                    "Worker progress drain raised during error cleanup",
                    error=str(cleanup_error),
                )
            if delivery_cleanup_error is not None and delivery_cleanup_error is not e:
                logger.warning(
                    "Stream delivery controller raised during error cleanup",
                    error=str(delivery_cleanup_error),
                )
            await streaming.finalize(client, error=e)
            if tool_trace_collector is not None:
                tool_trace_collector[:] = streaming.tool_trace
            raise StreamingDeliveryError(
                e,
                event_id=streaming.event_id,
                accumulated_text=streaming.accumulated_text,
                tool_trace=streaming.tool_trace,
            ) from e
        else:
            await streaming.finalize(client)
        finally:
            cleanup_error = await _shutdown_worker_progress_drain(pump, progress_task)
            if cleanup_error is not None:
                logger.warning(
                    "Worker progress drain raised during final cleanup",
                    error=str(cleanup_error),
                )
            delivery_cleanup_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)
            if delivery_cleanup_error is not None:
                logger.warning(
                    "Stream delivery controller raised during final cleanup",
                    error=str(delivery_cleanup_error),
                )

    if tool_trace_collector is not None:
        tool_trace_collector[:] = streaming.tool_trace

    return streaming.event_id, streaming.accumulated_text
