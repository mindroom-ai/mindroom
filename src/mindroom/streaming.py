"""Streaming response implementation for real-time message updates."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agno.run.agent import RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent

from . import interactive
from .logging_config import get_logger
from .matrix.client import edit_message, send_message
from .matrix.mentions import format_message_with_mentions
from .tool_events import (
    StructuredStreamChunk,
    ToolTraceEntry,
    complete_pending_tool_block,
    extract_tool_completed_info,
    format_tool_started_event,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import nio

    from .config import Config

from .matrix.client import get_latest_thread_event_id_if_needed

logger = get_logger(__name__)

# Global constant for the in-progress marker
IN_PROGRESS_MARKER = " ⋯"
PROGRESS_PLACEHOLDER = "Thinking..."
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

    # Diverged snapshots: preserve known history and append unseen tail.
    return existing + incoming[shared_prefix:]


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
    show_tool_calls: bool = True  # When False, omit inline tool call text (metadata still tracked)
    tool_trace: list[ToolTraceEntry] = field(default_factory=list)
    stream_started_at: float | None = None
    chars_since_last_update: int = 0
    in_progress_update_count: int = 0

    def _update(self, new_chunk: str) -> None:
        """Append new chunk to accumulated text."""
        self.accumulated_text += new_chunk
        self.chars_since_last_update += len(new_chunk)

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

    async def finalize(self, client: nio.AsyncClient) -> None:
        """Send final message update."""
        # When a placeholder message exists but no real text arrived,
        # still edit the message to strip the in-progress marker.
        has_placeholder = self.event_id is not None and not self.accumulated_text.strip()
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
            tool_trace=self.tool_trace,
        )

        if self.event_id is None:
            # First message - send new
            logger.debug("Sending initial streaming message")
            response_event_id = await send_message(client, self.room_id, content)
            if response_event_id:
                self.event_id = response_event_id
                logger.debug("Initial streaming message sent", event_id=self.event_id)
            else:
                logger.error("Failed to send initial streaming message")
        else:
            # Subsequent updates - edit existing message
            logger.debug("Editing streaming message", event_id=self.event_id)
            response_event_id = await edit_message(client, self.room_id, self.event_id, content, display_text)
            if not response_event_id:
                logger.error("Failed to edit streaming message")

        if not is_final:
            self.in_progress_update_count += 1


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


async def send_streaming_response(  # noqa: C901, PLR0912
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
    )

    # Ensure the first chunk triggers an initial send immediately
    streaming.last_update = float("-inf")

    if existing_event_id:
        streaming.event_id = existing_event_id
        streaming.accumulated_text = ""

    if header:
        await streaming.update_content(header, client)

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
            text_chunk, trace_entry = format_tool_started_event(chunk.tool)
            if trace_entry is not None:
                streaming.tool_trace.append(trace_entry)
            if not streaming.show_tool_calls:
                text_chunk = ""
                await streaming._throttled_send(client, progress_hint=True)
        elif isinstance(chunk, ToolCallCompletedEvent):
            info = extract_tool_completed_info(chunk.tool)
            if info:
                tool_name, result = info
                if streaming.show_tool_calls:
                    streaming.accumulated_text, trace_entry = complete_pending_tool_block(
                        streaming.accumulated_text,
                        tool_name,
                        result,
                    )
                else:
                    _, trace_entry = complete_pending_tool_block("", tool_name, result)
                streaming.tool_trace.append(trace_entry)
                if streaming.show_tool_calls:
                    await streaming._throttled_send(client)
                else:
                    await streaming._throttled_send(client, progress_hint=True)
                continue
            text_chunk = ""
        else:
            logger.debug(f"Unhandled streaming event type: {type(chunk).__name__}")
            continue

        if text_chunk:
            await streaming.update_content(text_chunk, client)

    await streaming.finalize(client)

    return streaming.event_id, streaming.accumulated_text
