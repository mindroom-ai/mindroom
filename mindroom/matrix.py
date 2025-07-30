import os
import re
from typing import Any

from dotenv import load_dotenv
from nio import RoomMessageText

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
    event: RoomMessageText,
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


def prepare_response_content(response_text: str, event: RoomMessageText) -> dict[str, Any]:
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
