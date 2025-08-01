"""Matrix mention utilities."""

from typing import Any


def create_mention_content(
    body: str,
    mentioned_user_ids: list[str],
    thread_event_id: str | None = None,
    reply_to_event_id: str | None = None,
) -> dict[str, Any]:
    """Create a properly formatted Matrix message with mentions.

    Args:
        body: The message body text
        mentioned_user_ids: List of Matrix user IDs to mention (e.g., ["@mindroom_calculator:localhost"])
        thread_event_id: Optional thread root event ID
        reply_to_event_id: Optional event ID to reply to

    Returns:
        Properly formatted content dict for room_send
    """
    content: dict[str, Any] = {
        "msgtype": "m.text",
        "body": body,
    }

    # Add mentions if any
    if mentioned_user_ids:
        content["m.mentions"] = {"user_ids": mentioned_user_ids}

    # Add thread/reply relationship if specified
    if thread_event_id or reply_to_event_id:
        relates_to: dict[str, Any] = {}

        if thread_event_id:
            relates_to["rel_type"] = "m.thread"
            relates_to["event_id"] = thread_event_id

        if reply_to_event_id:
            relates_to["m.in_reply_to"] = {"event_id": reply_to_event_id}

        content["m.relates_to"] = relates_to

    return content


def mention_agent(agent_name: str, message: str, **kwargs: Any) -> dict[str, Any]:
    """Create a mention for a specific agent.

    Args:
        agent_name: Name of the agent (e.g., "calculator")
        message: Message to send
        **kwargs: Additional arguments for create_mention_content

    Returns:
        Content dict ready for room_send
    """
    user_id = f"@mindroom_{agent_name}:localhost"

    # Ensure the agent is mentioned in the body
    if user_id not in message:
        message = f"{user_id} {message}"

    return create_mention_content(body=message, mentioned_user_ids=[user_id], **kwargs)
