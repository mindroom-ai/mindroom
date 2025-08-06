"""Streaming response implementation for real-time message updates."""

import time
from dataclasses import dataclass

import nio

from .logging_config import get_logger
from .matrix import create_mention_content_from_text

logger = get_logger(__name__)


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
    update_interval: float = 0.1  # 100ms updates
    interactive_processed: bool = False  # Track if we've already processed an interactive block

    async def update_content(self, new_chunk: str, client: nio.AsyncClient) -> None:
        """Add new content and potentially update the message."""
        self.accumulated_text += new_chunk

        # Don't send updates if we've already processed an interactive block
        # The interactive handler has taken over message editing
        if self.interactive_processed:
            return

        current_time = time.time()
        if current_time - self.last_update >= self.update_interval:
            await self._send_or_edit_message(client)
            self.last_update = current_time

    def has_complete_interactive_block(self) -> bool:
        """Check if we have a complete interactive block that can be processed."""
        if self.interactive_processed:
            return False

        # Check for both formats: ```interactive...``` and ```\ninteractive...```
        import re

        pattern = r"```(?:interactive\s*)?\n(?:interactive\s*\n)?(.*?)\n```"
        match = re.search(pattern, self.accumulated_text, re.DOTALL)
        return match is not None

    async def finalize(self, client: nio.AsyncClient) -> None:
        """Send final message update with completion marker."""
        # Don't finalize if we've already processed an interactive block
        # The interactive handler has already edited the message properly
        if self.interactive_processed:
            return

        if not self.accumulated_text.endswith(" ✓"):
            self.accumulated_text += " ✓"
        await self._send_or_edit_message(client)

    async def _send_or_edit_message(self, client: nio.AsyncClient) -> None:
        """Send new message or edit existing one."""
        if not self.accumulated_text.strip():
            return

        # Always ensure we have a thread_id - use the original message as thread root if needed
        effective_thread_id = self.thread_id if self.thread_id else self.reply_to_event_id

        content = create_mention_content_from_text(
            self.accumulated_text,
            sender_domain=self.sender_domain,
            thread_event_id=effective_thread_id,
            reply_to_event_id=self.reply_to_event_id,
        )

        if self.event_id is None:
            # First message - send new
            logger.debug("Sending initial streaming message")
            response = await client.room_send(
                room_id=self.room_id,
                message_type="m.room.message",
                content=content,
            )
            if isinstance(response, nio.RoomSendResponse):
                self.event_id = response.event_id
                logger.debug("Initial streaming message sent", event_id=self.event_id)
            else:
                logger.error("Failed to send initial streaming message", error=str(response))
        else:
            # Subsequent updates - edit existing message
            logger.debug("Editing streaming message", event_id=self.event_id)
            edit_content = {
                "msgtype": "m.text",
                "body": f"* {self.accumulated_text}",
                "format": "org.matrix.custom.html",
                "formatted_body": content.get("formatted_body", self.accumulated_text),
                "m.new_content": content,
                "m.relates_to": {"rel_type": "m.replace", "event_id": self.event_id},
            }

            response = await client.room_send(
                room_id=self.room_id,
                message_type="m.room.message",
                content=edit_content,
            )
            if not isinstance(response, nio.RoomSendResponse):
                logger.error("Failed to edit streaming message", error=str(response))
