import os
from typing import Any

import markdown
import nio
from dotenv import load_dotenv

# Load configuration from .env file
load_dotenv()

MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "http://localhost:8008")


def markdown_to_html(text: str) -> str:
    """Convert markdown text to HTML for Matrix formatted messages.

    Args:
        text: The markdown text to convert

    Returns:
        HTML formatted text
    """
    # Configure markdown with common extensions
    md = markdown.Markdown(
        extensions=[
            "markdown.extensions.fenced_code",
            "markdown.extensions.codehilite",
            "markdown.extensions.tables",
            "markdown.extensions.nl2br",
        ],
        extension_configs={
            "markdown.extensions.codehilite": {
                "use_pygments": True,  # Don't use pygments for syntax highlighting
                "noclasses": True,  # Use inline styles instead of CSS classes
            }
        },
    )
    html_text: str = md.convert(text)
    return html_text


def prepare_response_content(response_text: str, event: nio.RoomMessageText, agent_name: str = "") -> dict[str, Any]:
    """Prepares the content for the response message."""
    from .logging_config import colorize, get_logger

    logger = get_logger(__name__)

    content: dict[str, Any] = {
        "msgtype": "m.text",
        "body": response_text,
        "format": "org.matrix.custom.html",
        "formatted_body": markdown_to_html(response_text),
    }

    relates_to = event.source.get("content", {}).get("m.relates_to")
    is_thread_reply = relates_to and relates_to.get("rel_type") == "m.thread"

    agent_prefix = colorize(agent_name) if agent_name else ""

    logger.debug(
        f"{agent_prefix} Preparing response content - Original event_id: {event.event_id}, "
        f"Original relates_to: {relates_to}, Is thread reply: {is_thread_reply}"
    )

    if relates_to:
        if is_thread_reply:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": relates_to.get("event_id"),
                "m.in_reply_to": {"event_id": event.event_id},
            }
            logger.debug(f"{agent_prefix} Setting thread reply with thread_id: {relates_to.get('event_id')}")
        else:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": event.event_id}}
            logger.debug(f"{agent_prefix} Setting regular reply (not thread)")
    else:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": event.event_id}}
        logger.debug(f"{agent_prefix} No relates_to in original message, setting regular reply")

    logger.debug(f"{agent_prefix} Final content m.relates_to: {content.get('m.relates_to')}")

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
