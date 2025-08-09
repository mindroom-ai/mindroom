"""Streaming response implementation for real-time message updates."""

import time
from dataclasses import dataclass

import nio

from . import interactive
from .logging_config import get_logger
from .matrix.client import edit_message
from .matrix.mentions import create_mention_content_from_text

logger = get_logger(__name__)

# Global constant for the in-progress marker
IN_PROGRESS_MARKER = " ⋯"


@dataclass
class StreamingResponse:
    """Manages a streaming response with incremental message updates."""

    room_id: str
    reply_to_event_id: str
    thread_id: str | None
    sender_domain: str
    accumulated_text: str = ""
    event_id: str | None = None  # None until first message sent
    last_update: float = 0.0
    update_interval: float = 1.0

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

        content = create_mention_content_from_text(
            display_text,
            sender_domain=self.sender_domain,
            thread_event_id=effective_thread_id,
            reply_to_event_id=self.reply_to_event_id,
        )

        if self.event_id is None:
            # First message - send new
            logger.debug("Sending initial streaming message")
            from mindroom.matrix.client import send_message

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
