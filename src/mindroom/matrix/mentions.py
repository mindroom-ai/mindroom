"""Matrix mention utilities."""

import re
from typing import Any

from ..thread_utils import get_known_agent_names


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


def parse_mentions_in_text(text: str, sender_domain: str = "localhost") -> tuple[str, list[str]]:
    """Parse text for agent mentions and return processed text with user IDs.

    Args:
        text: Text that may contain @agent_name mentions
        sender_domain: Domain part of the sender's user ID (e.g., "localhost" from "@user:localhost")

    Returns:
        Tuple of (processed_text, list_of_mentioned_user_ids)
    """
    known_agents = get_known_agent_names()
    mentioned_user_ids = []

    # Pattern to match @agent_name (with optional @mindroom_ prefix)
    # Matches: @calculator, @mindroom_calculator, @mindroom_calculator:localhost
    pattern = r"@(mindroom_)?(\w+)(?::[^\s]+)?"

    def replace_mention(match):
        prefix = match.group(1) or ""  # "mindroom_" or empty
        agent_name = match.group(2)

        # Skip if this is a user (mindroom_user_*)
        if agent_name.startswith("user_"):
            return match.group(0)

        # Check if it's a known agent
        if agent_name in known_agents:
            user_id = f"@mindroom_{agent_name}:{sender_domain}"
            if user_id not in mentioned_user_ids:
                mentioned_user_ids.append(user_id)
            return user_id
        elif prefix and agent_name.replace("mindroom_", "") in known_agents:
            # Handle case where someone wrote @mindroom_mindroom_calculator
            actual_agent = agent_name.replace("mindroom_", "")
            user_id = f"@mindroom_{actual_agent}:{sender_domain}"
            if user_id not in mentioned_user_ids:
                mentioned_user_ids.append(user_id)
            return user_id
        else:
            # Not a known agent, leave as is
            return match.group(0)

    processed_text = re.sub(pattern, replace_mention, text)
    return processed_text, mentioned_user_ids


def create_mention_content_from_text(
    text: str,
    sender_domain: str = "localhost",
    thread_event_id: str | None = None,
    reply_to_event_id: str | None = None,
) -> dict[str, Any]:
    """Parse text for mentions and create properly formatted Matrix message.

    This is the universal function that should be used everywhere.

    Args:
        text: Message text that may contain @agent_name mentions
        sender_domain: Domain part of the sender's user ID
        thread_event_id: Optional thread root event ID
        reply_to_event_id: Optional event ID to reply to

    Returns:
        Properly formatted content dict for room_send
    """
    processed_text, mentioned_user_ids = parse_mentions_in_text(text, sender_domain)

    return create_mention_content(
        body=processed_text,
        mentioned_user_ids=mentioned_user_ids,
        thread_event_id=thread_event_id,
        reply_to_event_id=reply_to_event_id,
    )


def mention_agent(agent_name: str, message: str, **kwargs: Any) -> dict[str, Any]:
    """Create a mention for a specific agent.

    DEPRECATED: Use create_mention_content_from_text() instead for universal mention support.

    Args:
        agent_name: Name of the agent (e.g., "calculator")
        message: Message to send
        **kwargs: Additional arguments for create_mention_content

    Returns:
        Content dict ready for room_send
    """
    # Ensure the agent is mentioned in the message
    if f"@{agent_name}" not in message and f"@mindroom_{agent_name}" not in message:
        message = f"@{agent_name} {message}"

    # Use the universal function
    sender_domain = kwargs.pop("sender_domain", "localhost")
    return create_mention_content_from_text(message, sender_domain, **kwargs)
