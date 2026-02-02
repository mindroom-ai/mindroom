"""Matrix message content builder with proper threading support."""

from typing import Any

from .client import markdown_to_html


def build_thread_relation(
    thread_event_id: str,
    reply_to_event_id: str | None = None,
) -> dict[str, Any]:
    """Build the m.relates_to structure for thread messages.

    Args:
        thread_event_id: The thread root event ID
        reply_to_event_id: Optional event ID for genuine replies within thread
    Returns:
        The m.relates_to structure for the message content

    """
    relation: dict[str, Any] = {
        "rel_type": "m.thread",
        "event_id": thread_event_id,
        "is_falling_back": False,
    }
    if reply_to_event_id:
        relation["m.in_reply_to"] = {"event_id": reply_to_event_id}
    return relation


def build_message_content(
    body: str,
    formatted_body: str | None = None,
    mentioned_user_ids: list[str] | None = None,
    thread_event_id: str | None = None,
    reply_to_event_id: str | None = None,
) -> dict[str, Any]:
    """Build a complete Matrix message content dictionary.

    This handles all the Matrix protocol requirements for messages including:
    - Basic message structure
    - HTML formatting
    - User mentions
    - Thread relations
    - Reply relations

    Args:
        body: The plain text message body
        formatted_body: Optional HTML formatted body (if not provided, converts from markdown)
        mentioned_user_ids: Optional list of Matrix user IDs to mention
        thread_event_id: Optional thread root event ID
        reply_to_event_id: Optional event ID to reply to
    Returns:
        Complete content dictionary ready for room_send

    """
    content: dict[str, Any] = {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": formatted_body if formatted_body else markdown_to_html(body),
    }

    # Add mentions if any
    if mentioned_user_ids:
        content["m.mentions"] = {"user_ids": mentioned_user_ids}

    # Add thread/reply relationship if specified
    if thread_event_id:
        content["m.relates_to"] = build_thread_relation(
            thread_event_id=thread_event_id,
            reply_to_event_id=reply_to_event_id,
        )
    elif reply_to_event_id:
        # Plain reply without thread (shouldn't happen in this bot)
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to_event_id}}

    return content
