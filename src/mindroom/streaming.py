"""Streaming response implementation for real-time message updates."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agno.run.agent import RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent

from mindroom import interactive
from mindroom.logging_config import get_logger
from mindroom.matrix.client import edit_message, send_message
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.tool_events import (
    StructuredStreamChunk,
    ToolTraceEntry,
    complete_pending_tool_block,
    extract_tool_completed_info,
    format_tool_started_event,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import nio

    from mindroom.config.main import Config

from mindroom.matrix.client import get_latest_thread_event_id_if_needed

logger = get_logger(__name__)

# Global constant for the in-progress marker
IN_PROGRESS_MARKER = " ⋯"
PROGRESS_PLACEHOLDER = "Thinking..."
CANCELLED_RESPONSE_NOTE = "**[Response cancelled by user]**"
StreamInputChunk = str | StructuredStreamChunk | RunContentEvent | ToolCallStartedEvent | ToolCallCompletedEvent
_IN_PROGRESS_MESSAGE_PATTERN = re.compile(rf"{re.escape(IN_PROGRESS_MARKER)}\.*$")


def is_in_progress_message(text: object) -> bool:
    """Return True when a message ends with an in-progress marker."""
    if not isinstance(text, str):
        return False
    return bool(_IN_PROGRESS_MESSAGE_PATTERN.search(text))


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


@dataclass
class StreamingResponse:
    """Manages a streaming response with incremental message updates."""

    room_id: str
    reply_to_event_id: str | None
    thread_id: str | None
    sender_domain: str
    config: Config
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
    in_progress_update_count: int = 0
    placeholder_progress_sent: bool = False

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
        self._update(new_chunk)
        await self._throttled_send(client)

    async def finalize(self, client: nio.AsyncClient, *, cancelled: bool = False) -> None:
        """Send final message update."""
        if cancelled:
            stripped_text = self.accumulated_text.rstrip()
            self.accumulated_text = (
                f"{stripped_text}\n\n{CANCELLED_RESPONSE_NOTE}" if stripped_text else CANCELLED_RESPONSE_NOTE
            )

        # When a placeholder message exists but no real text arrived,
        # still edit the message to strip the in-progress marker.
        has_placeholder = (
            self.event_id is not None and self.placeholder_progress_sent and not self.accumulated_text.strip()
        )
        await self._send_or_edit_message(client, is_final=True, allow_empty_progress=has_placeholder)

    async def _send_or_edit_message(
        self,
        client: nio.AsyncClient,
        is_final: bool = False,
        *,
        allow_empty_progress: bool = False,
    ) -> None:
        """Send new message or edit existing one."""
        if not self.accumulated_text.strip() and not allow_empty_progress:
            return

        effective_thread_id = None if self.room_mode else self.thread_id if self.thread_id else self.reply_to_event_id

        # Add in-progress marker during streaming (not on final update)
        text_to_send = self.accumulated_text if self.accumulated_text.strip() else PROGRESS_PLACEHOLDER
        if not is_final:
            marker_suffix = "." * (self.in_progress_update_count % 3)
            text_to_send += f"{IN_PROGRESS_MARKER}{marker_suffix}"

        # Format the text (handles interactive questions if present)
        response = interactive.parse_and_format_interactive(text_to_send, extract_mapping=False)
        display_text = response.formatted_text

        # Only use latest_thread_event_id for the initial message (not edits)
        latest_for_message = self.latest_thread_event_id if self.event_id is None and not self.room_mode else None

        content = format_message_with_mentions(
            config=self.config,
            text=display_text,
            sender_domain=self.sender_domain,
            thread_event_id=effective_thread_id,
            reply_to_event_id=None if self.room_mode else self.reply_to_event_id,
            latest_thread_event_id=latest_for_message,
            tool_trace=self.tool_trace if self.show_tool_calls else None,
            extra_content=self.extra_content,
        )

        send_succeeded = False
        if self.event_id is None:
            # First message - send new
            logger.debug("Sending initial streaming message")
            response_event_id = await send_message(client, self.room_id, content)
            if response_event_id:
                self.event_id = response_event_id
                logger.debug("Initial streaming message sent", event_id=self.event_id)
                send_succeeded = True
            else:
                logger.error("Failed to send initial streaming message")
        else:
            # Subsequent updates - edit existing message
            logger.debug("Editing streaming message", event_id=self.event_id)
            response_event_id = await edit_message(client, self.room_id, self.event_id, content, display_text)
            if response_event_id:
                send_succeeded = True
            else:
                logger.error("Failed to edit streaming message")

        if send_succeeded and not is_final:
            self.in_progress_update_count += 1
            self.placeholder_progress_sent = not self.accumulated_text.strip()
        elif send_succeeded and is_final:
            self.placeholder_progress_sent = False


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
    response_stream: AsyncIterator[StreamInputChunk],
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
        elif isinstance(chunk, RunContentEvent) and chunk.content:
            text_chunk = str(chunk.content)
        elif isinstance(chunk, ToolCallStartedEvent):
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
            logger.debug(f"Unhandled streaming event type: {type(chunk).__name__}")
            continue

        if text_chunk:
            await streaming.update_content(text_chunk, client)


async def send_streaming_response(
    client: nio.AsyncClient,
    room_id: str,
    reply_to_event_id: str | None,
    thread_id: str | None,
    sender_domain: str,
    config: Config,
    response_stream: AsyncIterator[StreamInputChunk],
    streaming_cls: type[StreamingResponse] = StreamingResponse,
    header: str | None = None,
    existing_event_id: str | None = None,
    room_mode: bool = False,
    show_tool_calls: bool = True,
    extra_content: dict[str, Any] | None = None,
) -> tuple[str | None, str]:
    """Stream chunks to a Matrix room, returning (event_id, accumulated_text).

    Args:
        client: Matrix client
        room_id: Destination room
        reply_to_event_id: Event to reply to (can be None when in a thread)
        thread_id: Thread root if already in a thread
        sender_domain: Sender's homeserver domain for mention formatting
        config: App config for mention formatting
        response_stream: Async iterator yielding text chunks or response events
        streaming_cls: StreamingResponse class to use (default: StreamingResponse, alternative: ReplacementStreamingResponse)
        header: Optional text prefix to send before chunks
        existing_event_id: If editing an existing message, pass its ID
        room_mode: If True, skip thread relations (for bridges/mobile)
        show_tool_calls: Whether to include tool call text inline in the streamed message
        extra_content: Optional custom metadata fields merged into each event

    Returns:
        Tuple of (final event_id or None, full accumulated text)

    """
    if room_mode:
        latest_thread_event_id = None
    else:
        latest_thread_event_id = await get_latest_thread_event_id_if_needed(
            client,
            room_id,
            thread_id,
            reply_to_event_id,
            existing_event_id,
        )

    streaming = streaming_cls(
        room_id=room_id,
        reply_to_event_id=reply_to_event_id,
        thread_id=thread_id,
        sender_domain=sender_domain,
        config=config,
        latest_thread_event_id=latest_thread_event_id,
        room_mode=room_mode,
        show_tool_calls=show_tool_calls,
        extra_content=extra_content,
    )

    # Ensure the first chunk triggers an initial send immediately
    streaming.last_update = float("-inf")

    if existing_event_id:
        streaming.event_id = existing_event_id
        streaming.accumulated_text = ""

    if header:
        await streaming.update_content(header, client)

    try:
        await _consume_streaming_chunks(client, response_stream, streaming)
    except asyncio.CancelledError:
        await streaming.finalize(client, cancelled=True)
        raise

    await streaming.finalize(client)

    return streaming.event_id, streaming.accumulated_text
