import os
from typing import Any

import nio
from dotenv import load_dotenv

# Load configuration from .env file
load_dotenv()

# Load configuration from .env file
MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER")
MATRIX_USER_ID = os.getenv("MATRIX_USER_ID")
MATRIX_PASSWORD = os.getenv("MATRIX_PASSWORD")


def prepare_response_content(response_text: str, event: nio.RoomMessageText) -> dict[str, Any]:
    """Prepares the content for the response message."""
    content: dict[str, Any] = {"msgtype": "m.text", "body": response_text}

    relates_to = event.source.get("content", {}).get("m.relates_to")
    is_thread_reply = relates_to and relates_to.get("rel_type") == "m.thread"

    if relates_to:
        if is_thread_reply:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": relates_to.get("event_id"),
                "m.in_reply_to": {"event_id": event.event_id},
            }
        else:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": event.event_id}}
    else:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": event.event_id}}

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
