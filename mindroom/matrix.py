import os
import re
from typing import Any

import nio
from dotenv import load_dotenv

# Load configuration from .env file
load_dotenv()

# Load configuration from .env file
MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER")
MATRIX_USER_ID = os.getenv("MATRIX_USER_ID")
MATRIX_PASSWORD = os.getenv("MATRIX_PASSWORD")


def parse_message(message: str, bot_user_id: str, bot_display_name: str | None) -> tuple[str, str] | None:
    """Parses a message to find an agent command or a direct mention.
    Returns a tuple of (agent_name, prompt) or None.
    """
    # Fallback to general agent if bot is mentioned directly
    if bot_user_id in message:
        agent_name = "general"
        prompt = message.replace(bot_user_id, "").strip().lstrip(":").strip()
        if bot_display_name:
            prompt = prompt.replace(bot_display_name, "").strip()
        return agent_name, prompt

    # Use a regex to find mentions like @agent_name: prompt
    match = re.match(r"@(\w+):\s*(.*)", message)
    if match:
        agent_name = match.group(1)
        prompt = match.group(2).strip()
        return agent_name, prompt

    return None


def handle_message_parsing(
    event: nio.RoomMessageText,
    bot_user_id: str,
    bot_display_name: str | None,
) -> tuple[str, str] | None:
    """Parses the message and returns the agent name and prompt."""
    is_thread_reply = event.source.get("content", {}).get("m.relates_to", {}).get("rel_type") == "m.thread"
    parsed = parse_message(event.body, bot_user_id, bot_display_name)

    if not parsed and not is_thread_reply:
        return None

    if parsed:
        agent_name, prompt = parsed
    else:  # It's a thread reply without a direct mention
        agent_name = "general"
        prompt = event.body

    if not prompt:
        return None

    return agent_name, prompt


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
