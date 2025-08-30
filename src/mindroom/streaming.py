"""Streaming response implementation for real-time message updates."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agno.run.response import RunResponseContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent

from . import interactive
from .ai import _format_tool_completed_message, _format_tool_started_message
from .logging_config import get_logger
from .matrix.client import edit_message, send_message
from .matrix.mentions import format_message_with_mentions

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import nio

    from .config import Config

from .matrix.client import get_latest_thread_event_id_if_needed

logger = get_logger(__name__)

# Global constant for the in-progress marker
IN_PROGRESS_MARKER = " ⋯"


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
    update_interval: float = 1.0
    latest_thread_event_id: str | None = None  # For MSC3440 compliance

    def _update(self, new_chunk: str) -> None:
        """Append new chunk to accumulated text."""
        self.accumulated_text += new_chunk

    async def update_content(self, new_chunk: str, client: nio.AsyncClient) -> None:
        """Add new content and potentially update the message."""
        self._update(new_chunk)

        current_time = time.time()
        if current_time - self.last_update >= self.update_interval:
            await self._send_or_edit_message(client)
            self.last_update = current_time

    async def finalize(self, client: nio.AsyncClient) -> None:
        """Send final message update."""
        await self._send_or_edit_message(client, is_final=True)

    async def _send_or_edit_message(self, client: nio.AsyncClient, is_final: bool = False) -> None:
        """Send new message or edit existing one."""
        if not self.accumulated_text.strip():
            return

        # Always ensure we have a thread_id - use the original message as thread root if needed
        effective_thread_id = self.thread_id if self.thread_id else self.reply_to_event_id

        # Add in-progress marker during streaming (not on final update)
        text_to_send = self.accumulated_text
        if not is_final:
            text_to_send += IN_PROGRESS_MARKER

        # Format the text (handles interactive questions if present)
        response = interactive.parse_and_format_interactive(text_to_send, extract_mapping=False)
        display_text = response.formatted_text

        # Only use latest_thread_event_id for the initial message (not edits)
        latest_for_message = self.latest_thread_event_id if self.event_id is None else None

        content = format_message_with_mentions(
            config=self.config,
            text=display_text,
            sender_domain=self.sender_domain,
            thread_event_id=effective_thread_id,
            reply_to_event_id=self.reply_to_event_id,
            latest_thread_event_id=latest_for_message,
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


class ReplacementStreamingResponse(StreamingResponse):
    """StreamingResponse variant that replaces content instead of appending.

    Useful for structured live rendering where the full document is rebuilt
    on each tick and we want the message to reflect the latest full view,
    not incremental concatenation.
    """

    def _update(self, new_chunk: str) -> None:
        """Replace accumulated text with new chunk."""
        self.accumulated_text = new_chunk


async def send_streaming_response(
    client: nio.AsyncClient,
    room_id: str,
    reply_to_event_id: str | None,
    thread_id: str | None,
    sender_domain: str,
    config: Config,
    response_stream: AsyncIterator[object],
    streaming_cls: type[StreamingResponse] = StreamingResponse,
    header: str | None = None,
    existing_event_id: str | None = None,
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

    Returns:
        Tuple of (final event_id or None, full accumulated text)

    """
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
        elif isinstance(chunk, RunResponseContentEvent) and chunk.content:
            text_chunk = str(chunk.content)
        elif isinstance(chunk, ToolCallStartedEvent):
            text_chunk = _format_tool_started_message(chunk)
        elif isinstance(chunk, ToolCallCompletedEvent):
            text_chunk = _format_tool_completed_message(chunk)
        else:
            # Fallback for other event types - try to extract content
            content = getattr(chunk, "content", None)
            text_chunk = str(content) if content is not None else ""
            if not text_chunk:
                logger.debug(f"Unhandled streaming event type: {type(chunk).__name__}")
                continue

        if text_chunk:
            await streaming.update_content(text_chunk, client)

    await streaming.finalize(client)

    return streaming.event_id, streaming.accumulated_text
