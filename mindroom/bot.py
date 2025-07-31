import asyncio
import os
from typing import Any

from loguru import logger
from nio import (
    AsyncClient,
    InviteEvent,
    LoginResponse,
    MatrixRoom,
    MessageDirection,
    RoomMessageText,
)

from .ai import ai_response
from .logging_config import setup_logging
from .matrix import (
    MATRIX_HOMESERVER,
    MATRIX_PASSWORD,
    MATRIX_USER_ID,
    handle_message_parsing,
    prepare_response_content,
)

# Configure logger with colors
setup_logging(level="INFO")


async def fetch_thread_history(client: AsyncClient, room_id: str, thread_id: str) -> list[dict[str, Any]]:
    """Fetch all messages in a thread.

    Args:
        client: The Matrix client instance
        room_id: The room ID to fetch messages from
        thread_id: The thread root event ID

    Returns:
        List of messages in chronological order, each containing sender, body, timestamp, and event_id

    """
    messages = []
    from_token = None

    while True:
        response = await client.room_messages(
            room_id,
            start=from_token,
            limit=100,
            message_filter={"types": ["m.room.message"]},
            direction=MessageDirection.back,
        )

        for event in response.chunk:
            if hasattr(event, "source") and event.source.get("type") == "m.room.message":
                relates_to = event.source.get("content", {}).get("m.relates_to", {})
                if relates_to.get("rel_type") == "m.thread" and relates_to.get("event_id") == thread_id:
                    messages.append(
                        {
                            "sender": event.sender,
                            "body": getattr(event, "body", ""),
                            "timestamp": event.server_timestamp,
                            "event_id": event.event_id,
                        },
                    )

        if not response.end:
            break
        from_token = response.end

    return list(reversed(messages))  # Return in chronological order


class Bot:
    def __init__(self) -> None:
        if not all([MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_PASSWORD]):
            msg = "Matrix configuration is missing from .env file."
            raise ValueError(msg)
        assert MATRIX_HOMESERVER is not None
        assert MATRIX_USER_ID is not None
        self.client = AsyncClient(MATRIX_HOMESERVER, MATRIX_USER_ID)
        self.client.add_event_callback(self._on_invite, InviteEvent)
        self.client.add_event_callback(self._on_message, RoomMessageText)

    async def start(self) -> None:
        """Start the bot."""
        logger.info("Starting bot...")
        assert MATRIX_PASSWORD is not None
        response = await self.client.login(MATRIX_PASSWORD)
        if isinstance(response, LoginResponse):
            logger.info(f"Successfully logged in as {self.client.user_id}")
            await self.client.sync_forever(timeout=30000)
        else:
            logger.error(f"Failed to log in: {response}")
            await self.client.close()

    async def _on_invite(self, room: MatrixRoom, event: InviteEvent) -> None:
        """Callback for when the bot is invited to a room."""
        logger.info(f"Received invite to room: {room.display_name} ({room.room_id})")
        await self.client.join(room.room_id)
        logger.info(f"Joined room: {room.room_id}")

    async def _on_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        """Callback for when a message is received in a room."""
        if event.sender == self.client.user_id:
            return

        logger.info(f"Received message from {event.sender}: {event.body}")
        parsed_data = handle_message_parsing(event, self.client.user_id, self.client.user)
        if not parsed_data:
            logger.info(f"Message not parsed as a bot command: {event.body}")
            return

        agent_name, prompt = parsed_data
        logger.info(f"Parsed command - Agent: {agent_name}, Prompt: {prompt}")

        # Create a unique session_id that includes thread information for context isolation
        relates_to = event.source.get("content", {}).get("m.relates_to", {})
        thread_id = relates_to.get("event_id") if relates_to and relates_to.get("rel_type") == "m.thread" else None

        # Use room_id + thread_id for session isolation
        session_id = f"{room.room_id}:{thread_id}" if thread_id else room.room_id

        # Fetch thread history if in a thread
        thread_history = []
        if thread_id:
            thread_history = await fetch_thread_history(self.client, room.room_id, thread_id)

        response_text = await ai_response(agent_name, prompt, session_id, thread_history=thread_history)

        content = prepare_response_content(response_text, event)

        await self.client.room_send(
            room_id=room.room_id,
            message_type="m.room.message",
            content=content,
        )
        logger.info(f"Sent response to room {room.room_id}")


async def main() -> None:
    # Create tmp directory for sqlite dbs if it doesn't exist
    if not os.path.exists("tmp"):
        os.makedirs("tmp")
    bot = Bot()
    await bot.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
