"""Matrix mention utilities."""

import re
from typing import Any

from ..agent_config import load_config
from .client import markdown_to_html
from .identity import MatrixID


def create_mention_content(
    body: str,
    mentioned_user_ids: list[str],
    thread_event_id: str | None = None,
    reply_to_event_id: str | None = None,
    formatted_body: str | None = None,
) -> dict[str, Any]:
    """Create a properly formatted Matrix message with mentions.

    Args:
        body: The message body text (plain text version)
        mentioned_user_ids: List of Matrix user IDs to mention (e.g., ["@mindroom_calculator:localhost"])
        thread_event_id: Optional thread root event ID
        reply_to_event_id: Optional event ID to reply to
        formatted_body: Optional HTML formatted body (if not provided, will convert from markdown)

    Returns:
        Properly formatted content dict for room_send
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
    if thread_event_id or reply_to_event_id:
        relates_to: dict[str, Any] = {}

        if thread_event_id:
            relates_to["rel_type"] = "m.thread"
            relates_to["event_id"] = thread_event_id

        if reply_to_event_id:
            relates_to["m.in_reply_to"] = {"event_id": reply_to_event_id}

        content["m.relates_to"] = relates_to

    return content


def parse_mentions_in_text(text: str, sender_domain: str = "localhost") -> tuple[str, list[str], str]:
    """Parse text for agent mentions and return processed text with user IDs.

    Args:
        text: Text that may contain @agent_name mentions
        sender_domain: Domain part of the sender's user ID (e.g., "localhost" from "@user:localhost")

    Returns:
        Tuple of (plain_text, list_of_mentioned_user_ids, markdown_text_with_links)
    """
    config = load_config()
    known_agents = set(config.agents.keys())
    mentioned_user_ids = []

    # Keep track of replacements for markdown version
    replacements = []

    # Pattern to match @agent_name (with optional @mindroom_ prefix)
    # Matches: @calculator, @mindroom_calculator, @mindroom_calculator:localhost
    pattern = r"@(mindroom_)?(\w+)(?::[^\s]+)?"

    def collect_mentions(match):
        prefix = match.group(1) or ""  # "mindroom_" or empty
        agent_name = match.group(2)

        # Skip if this is a user (mindroom_user_*)
        if agent_name.startswith("user_"):
            return match.group(0)

        # Check if it's a known agent
        if agent_name in known_agents:
            agent_config = config.agents[agent_name]
            user_id = MatrixID.from_agent(agent_name, sender_domain).full_id
            if user_id not in mentioned_user_ids:
                mentioned_user_ids.append(user_id)
            # Store replacement info for later markdown generation
            replacements.append((match.group(0), user_id, agent_config.display_name))
            return user_id
        elif prefix and agent_name.replace("mindroom_", "") in known_agents:
            # Handle case where someone wrote @mindroom_mindroom_calculator
            actual_agent = agent_name.replace("mindroom_", "")
            agent_config = config.agents[actual_agent]
            user_id = MatrixID.from_agent(actual_agent, sender_domain).full_id
            if user_id not in mentioned_user_ids:
                mentioned_user_ids.append(user_id)
            replacements.append((match.group(0), user_id, agent_config.display_name))
            return user_id
        else:
            # Not a known agent, leave as is
            return match.group(0)

    # Generate plain text version (just user IDs)
    plain_text = re.sub(pattern, collect_mentions, text)

    # Generate markdown version with proper matrix.to links
    markdown_text = text
    for original, user_id, display_name in replacements:
        # Create markdown link that will be converted to HTML
        # This matches Element's format: [@DisplayName](https://matrix.to/#/@user:domain)
        link = f"[@{display_name}](https://matrix.to/#/{user_id})"
        markdown_text = markdown_text.replace(original, link, 1)

    return plain_text, mentioned_user_ids, markdown_text


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
    plain_text, mentioned_user_ids, markdown_text = parse_mentions_in_text(text, sender_domain)

    # Convert markdown (with links) to HTML
    # The markdown converter will properly handle the [@DisplayName](url) format
    formatted_html = markdown_to_html(markdown_text)

    return create_mention_content(
        body=plain_text,
        mentioned_user_ids=mentioned_user_ids,
        thread_event_id=thread_event_id,
        reply_to_event_id=reply_to_event_id,
        formatted_body=formatted_html,
    )
