"""Matrix message content builder with proper threading support."""

from typing import Any

from .client import markdown_to_html


def build_thread_relation(
    thread_event_id: str,
    reply_to_event_id: str | None = None,
    latest_thread_event_id: str | None = None,
) -> dict[str, Any]:
    """Build the m.relates_to structure for thread messages per MSC3440.

    Args:
        thread_event_id: The thread root event ID
        reply_to_event_id: Optional event ID for genuine replies within thread
        latest_thread_event_id: Latest event in thread (required for fallback if no reply_to)

    Returns:
        The m.relates_to structure for the message content

    """
    if reply_to_event_id:
        # Genuine reply to a specific message in the thread
        return {
            "rel_type": "m.thread",
            "event_id": thread_event_id,
            "is_falling_back": False,
            "m.in_reply_to": {"event_id": reply_to_event_id},
        }
    # Fallback: continuing thread without specific reply
    # Per MSC3440, should point to latest message in thread for backwards compatibility
    assert latest_thread_event_id is not None, "latest_thread_event_id is required for thread fallback"
    return {
        "rel_type": "m.thread",
        "event_id": thread_event_id,
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": latest_thread_event_id},
    }


def build_message_content(
    body: str,
    formatted_body: str | None = None,
    mentioned_user_ids: list[str] | None = None,
    thread_event_id: str | None = None,
    reply_to_event_id: str | None = None,
    latest_thread_event_id: str | None = None,
) -> dict[str, Any]:
    """Build a complete Matrix message content dictionary.

    This handles all the Matrix protocol requirements for messages including:
    - Basic message structure
    - HTML formatting
    - User mentions
    - Thread relations (MSC3440 compliant)
    - Reply relations

    Args:
        body: The plain text message body
        formatted_body: Optional HTML formatted body (if not provided, converts from markdown)
        mentioned_user_ids: Optional list of Matrix user IDs to mention
        thread_event_id: Optional thread root event ID
        reply_to_event_id: Optional event ID to reply to
        latest_thread_event_id: Optional latest event in thread (for MSC3440 fallback)

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
            latest_thread_event_id=latest_thread_event_id,
        )
    elif reply_to_event_id:
        # Plain reply without thread (shouldn't happen in this bot)
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to_event_id}}

    return content
