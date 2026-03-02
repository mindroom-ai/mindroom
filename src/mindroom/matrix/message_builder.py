"""Matrix message content builder with proper threading support."""

import re
from html import escape
from typing import Any

import markdown

_HTML_TAG_PATTERN = re.compile(r"</?([A-Za-z][A-Za-z0-9-]*)(?:\s+[^<>]*)?\s*/?>")

# Standard Matrix-safe HTML tags.
_GENERAL_FORMATTED_BODY_TAGS = frozenset(
    {
        "a",
        "b",
        "blockquote",
        "br",
        "caption",
        "code",
        "del",
        "details",
        "div",
        "em",
        "font",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "i",
        "img",
        "li",
        "ol",
        "p",
        "pre",
        "s",
        "span",
        "strike",
        "strong",
        "sub",
        "summary",
        "sup",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "u",
        "ul",
    },
)

_ALLOWED_FORMATTED_BODY_TAGS = _GENERAL_FORMATTED_BODY_TAGS


def _escape_unsupported_html_tags(html_text: str) -> str:
    """Escape raw tags that Matrix clients commonly strip entirely.

    Unknown tags from model output (e.g. ``<search>``) can disappear in some
    clients. Escaping unsupported tags keeps them visible as literal text.
    """

    def _replace_tag(match: re.Match[str]) -> str:
        tag_name = match.group(1).lower()
        if tag_name in _ALLOWED_FORMATTED_BODY_TAGS:
            return match.group(0)
        return escape(match.group(0))

    return _HTML_TAG_PATTERN.sub(_replace_tag, html_text)


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
                "use_pygments": True,  # Use Pygments for syntax highlighting.
                "noclasses": True,  # Use inline styles instead of CSS classes
            },
        },
    )
    html_text: str = md.convert(text)
    return _escape_unsupported_html_tags(html_text)


def _build_thread_relation(
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
    extra_content: dict[str, Any] | None = None,
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
        extra_content: Optional extra content fields to merge into the message

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
        content["m.relates_to"] = _build_thread_relation(
            thread_event_id=thread_event_id,
            reply_to_event_id=reply_to_event_id,
            latest_thread_event_id=latest_thread_event_id,
        )
    elif reply_to_event_id:
        # Plain reply without thread (shouldn't happen in this bot)
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to_event_id}}

    if extra_content:
        content.update(extra_content)

    return content
