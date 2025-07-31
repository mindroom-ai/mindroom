import os
from typing import Any

import nio
from dotenv import load_dotenv

# Load configuration from .env file
load_dotenv()

MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "http://localhost:8008")


def prepare_response_content(response_text: str, event: nio.RoomMessageText) -> dict[str, Any]:
    """Prepares the content for the response message."""
    from loguru import logger

    content: dict[str, Any] = {"msgtype": "m.text", "body": response_text}

    relates_to = event.source.get("content", {}).get("m.relates_to")
    is_thread_reply = relates_to and relates_to.get("rel_type") == "m.thread"

    logger.debug("=== Preparing response content ===")
    logger.debug(f"Original event_id: {event.event_id}")
    logger.debug(f"Original relates_to: {relates_to}")
    logger.debug(f"Is thread reply: {is_thread_reply}")

    if relates_to:
        if is_thread_reply:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": relates_to.get("event_id"),
                "m.in_reply_to": {"event_id": event.event_id},
            }
            logger.debug(f"Setting thread reply with thread_id: {relates_to.get('event_id')}")
        else:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": event.event_id}}
            logger.debug("Setting regular reply (not thread)")
    else:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": event.event_id}}
        logger.debug("No relates_to in original message, setting regular reply")

    logger.debug(f"Final content m.relates_to: {content.get('m.relates_to')}")
    logger.debug("=== End preparing response content ===")

    return content


async def fetch_thread_history(client: nio.AsyncClient, room_id: str, thread_id: str) -> list[dict[str, Any]]:
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
            direction=nio.MessageDirection.back,
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
