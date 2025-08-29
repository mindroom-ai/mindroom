"""Streaming response implementation for real-time message updates."""

from __future__ import annotations

import time
from dataclasses import dataclass
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING, Any, cast

from . import interactive
from .logging_config import get_logger
from .matrix.client import edit_message, send_message
from .matrix.mentions import create_mention_content_from_text

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

    async def update_content(self, new_chunk: str, client: nio.AsyncClient) -> None:
        """Add new content and potentially update the message."""
        self.accumulated_text += new_chunk

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

        content = create_mention_content_from_text(
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

    async def update_content(self, new_chunk: str, client: nio.AsyncClient) -> None:
        """Replace accumulated text with the latest chunk and update display."""
        self.accumulated_text = new_chunk
        current_time = time.time()
        if current_time - self.last_update >= self.update_interval:
            await self._send_or_edit_message(client)
            self.last_update = current_time


async def stream_chunks_to_room(
    client: nio.AsyncClient,
    room_id: str,
    reply_to_event_id: str | None,
    thread_id: str | None,
    sender_domain: str,
    config: Config,
    chunk_iter: AsyncIterator[object],
    header: str | None = None,
    existing_event_id: str | None = None,
    streaming_cls: type[StreamingResponse] | None = None,
) -> tuple[str | None, str]:
    """Stream chunks to a Matrix room, returning (event_id, accumulated_text).

    Args:
        client: Matrix client
        room_id: Destination room
        reply_to_event_id: Event to reply to (can be None when in a thread)
        thread_id: Thread root if already in a thread
        sender_domain: Sender's homeserver domain for mention formatting
        config: App config for mention formatting
        chunk_iter: Async iterator yielding text chunks
        header: Optional text prefix to send before chunks
        existing_event_id: If editing an existing message, pass its ID
        streaming_cls: Optional StreamingResponse class override (useful for tests)

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

    sr_cls = streaming_cls or StreamingResponse
    streaming = sr_cls(
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
        if iscoroutinefunction(streaming.update_content):
            await streaming.update_content(header, client)
        else:
            cast("Any", streaming).update_content(header, client)

    async for chunk in chunk_iter:
        # Normalize non-string chunks (e.g., Agno events) to text
        if isinstance(chunk, str):
            text_chunk = chunk
        else:
            content = getattr(chunk, "content", None)
            text_chunk = str(content) if content is not None else str(chunk)
        if iscoroutinefunction(streaming.update_content):
            await streaming.update_content(text_chunk, client)
        else:
            cast("Any", streaming).update_content(text_chunk, client)

    if iscoroutinefunction(streaming.finalize):
        await streaming.finalize(client)
    else:
        cast("Any", streaming).finalize(client)

    return streaming.event_id, streaming.accumulated_text
