"""Matrix mention utilities."""

import re
from typing import Any

from ..models import Config
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


def parse_mentions_in_text(text: str, sender_domain: str, config: Config) -> tuple[str, list[str], str]:
    """Parse text for agent mentions and return processed text with user IDs.

    Args:
        text: Text that may contain @agent_name mentions
        sender_domain: Domain part of the sender's user ID (e.g., "localhost" from "@user:localhost")

    Returns:
        Tuple of (plain_text, list_of_mentioned_user_ids, markdown_text_with_links)
    """
    # Pattern to match @agent_name (with optional @mindroom_ prefix or domain)
    # Matches: @calculator, @mindroom_calculator, @mindroom_calculator:localhost
    pattern = r"@(mindroom_)?(\w+)(?::[^\s]+)?"

    # Find all mentions and process them
    mentions_data = []
    for match in re.finditer(pattern, text):
        mention_info = _process_mention(match, config, sender_domain)
        if mention_info:
            mentions_data.append(mention_info)

    # Build outputs from collected data
    plain_text = text
    markdown_text = text
    mentioned_user_ids: list[str] = []

    # Apply replacements (reverse order to preserve positions)
    for original, user_id, display_name in reversed(mentions_data):
        # Plain text: replace with full Matrix ID
        plain_text = plain_text.replace(original, user_id, 1)

        # Markdown: replace with clickable link
        link = f"[@{display_name}](https://matrix.to/#/{user_id})"
        markdown_text = markdown_text.replace(original, link, 1)

        # Collect unique user IDs
        if user_id not in mentioned_user_ids:
            mentioned_user_ids.insert(0, user_id)  # Insert at start to maintain order

    return plain_text, mentioned_user_ids, markdown_text


def _process_mention(match: re.Match, config: Any, sender_domain: str) -> tuple[str, str, str] | None:
    """Process a single mention match and return replacement data.

    Args:
        match: The regex match object
        config: The loaded config
        sender_domain: Domain for constructing Matrix IDs

    Returns:
        Tuple of (original_text, matrix_user_id, display_name) or None if not a valid agent
    """
    original = match.group(0)
    prefix = match.group(1) or ""  # "mindroom_" or empty
    name = match.group(2)

    # Skip user mentions (mindroom_user_*)
    if name.startswith("user_"):
        return None

    # Try to find the agent
    agent_name = None
    if name in config.agents:
        # Direct match: @calculator
        agent_name = name
    elif prefix and name.replace("mindroom_", "") in config.agents:
        # Handle @mindroom_mindroom_calculator
        agent_name = name.replace("mindroom_", "")

    if agent_name:
        agent_config = config.agents[agent_name]
        user_id = MatrixID.from_agent(agent_name, sender_domain).full_id
        return (original, user_id, agent_config.display_name)

    return None


def create_mention_content_from_text(
    config: Config,
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
    plain_text, mentioned_user_ids, markdown_text = parse_mentions_in_text(text, sender_domain, config)

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
