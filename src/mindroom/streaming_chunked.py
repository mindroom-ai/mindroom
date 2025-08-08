"""Alternative chunked streaming implementation that avoids excessive edits."""

import time
from dataclasses import dataclass, field

import nio

from . import interactive
from .logging_config import get_logger
from .matrix import create_mention_content_from_text

logger = get_logger(__name__)

# Global constant for the in-progress marker
IN_PROGRESS_MARKER = " ⋯"


@dataclass
class ChunkedStreamingResponse:
    """Manages a streaming response with chunked message sending instead of edits.

    This approach sends new messages for significant chunks of content rather than
    constantly editing a single message, reducing database bloat.
    """

    room_id: str
    reply_to_event_id: str
    thread_id: str | None
    sender_domain: str
    accumulated_text: str = ""
    sent_text: str = ""  # Text already sent in messages
    message_ids: list[str] = field(default_factory=list)
    last_update: float = 0.0
    chunk_size: int = 500  # Send new message every N characters
    update_interval: float = 2.0  # Check for updates every 2 seconds

    async def update_content(self, new_chunk: str, client: nio.AsyncClient) -> None:
        """Add new content and potentially send a new message chunk."""
        self.accumulated_text += new_chunk

        current_time = time.time()
        unsent_text = self.accumulated_text[len(self.sent_text) :]

        # Send new message if we have enough unsent content or enough time has passed
        should_send = len(unsent_text) >= self.chunk_size or (
            current_time - self.last_update >= self.update_interval and unsent_text.strip()
        )

        if should_send:
            await self._send_chunk(client, is_final=False)
            self.last_update = current_time

    async def finalize(self, client: nio.AsyncClient) -> None:
        """Send final message chunk if there's any remaining content."""
        unsent_text = self.accumulated_text[len(self.sent_text) :]
        if unsent_text.strip():
            await self._send_chunk(client, is_final=True)
        elif self.message_ids:
            # Edit the last message to remove the in-progress marker
            await self._remove_progress_marker(client)

    async def _send_chunk(self, client: nio.AsyncClient, is_final: bool = False) -> None:
        """Send a new message chunk."""
        unsent_text = self.accumulated_text[len(self.sent_text) :]
        if not unsent_text.strip():
            return

        # Always ensure we have a thread_id
        effective_thread_id = self.thread_id if self.thread_id else self.reply_to_event_id

        # Add progress marker if not final
        text_to_send = unsent_text
        if not is_final:
            text_to_send += IN_PROGRESS_MARKER

        # Format the text
        response = interactive.parse_and_format_interactive(text_to_send, extract_mapping=False)
        display_text = response.formatted_text

        # Use the last message as reply-to for continuity in chunks
        reply_to = self.message_ids[-1] if self.message_ids else self.reply_to_event_id

        content = create_mention_content_from_text(
            display_text,
            sender_domain=self.sender_domain,
            thread_event_id=effective_thread_id,
            reply_to_event_id=reply_to,
        )

        # Add a note that this is a continuation
        if self.message_ids:
            content["body"] = f"[continued] {content['body']}"
            if "formatted_body" in content:
                content["formatted_body"] = f"<em>[continued]</em> {content['formatted_body']}"

        logger.debug(f"Sending chunk {len(self.message_ids) + 1}")
        response = await client.room_send(
            room_id=self.room_id,
            message_type="m.room.message",
            content=content,
        )

        if isinstance(response, nio.RoomSendResponse):
            self.message_ids.append(response.event_id)
            self.sent_text = self.accumulated_text
            logger.debug("Chunk sent", event_id=response.event_id)
        else:
            logger.error("Failed to send chunk", error=str(response))

    async def _remove_progress_marker(self, client: nio.AsyncClient) -> None:
        """Edit the last message to remove the progress marker."""
        if not self.message_ids:
            return

        last_event_id = self.message_ids[-1]
        effective_thread_id = self.thread_id if self.thread_id else self.reply_to_event_id

        # Get the last chunk of text
        if len(self.message_ids) == 1:
            final_text = self.accumulated_text
        else:
            # For continuation messages, keep the [continued] prefix
            prev_sent = self.accumulated_text[
                : len(self.sent_text) - len(self.accumulated_text.split(self.sent_text)[-1])
            ]
            final_text = self.accumulated_text[len(prev_sent) :]

        response = interactive.parse_and_format_interactive(final_text, extract_mapping=False)
        display_text = response.formatted_text

        content = create_mention_content_from_text(
            display_text,
            sender_domain=self.sender_domain,
            thread_event_id=effective_thread_id,
            reply_to_event_id=self.message_ids[-2] if len(self.message_ids) > 1 else self.reply_to_event_id,
        )

        # Add continuation marker if needed
        if len(self.message_ids) > 1:
            content["body"] = f"[continued] {content['body']}"
            if "formatted_body" in content:
                content["formatted_body"] = f"<em>[continued]</em> {content['formatted_body']}"

        # Edit to remove progress marker
        from .matrix import edit_message

        await edit_message(client, self.room_id, last_event_id, content, display_text)
