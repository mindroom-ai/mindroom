"""Shared rendering for model-facing Matrix ``<msg>`` prompt tags."""

from __future__ import annotations

import re
from html import unescape as html_unescape
from xml.sax.saxutils import quoteattr as xml_quoteattr


_MSG_OPEN_TAG_RE = re.compile(r"<msg\\b[^>]*>")
_FROM_ATTR_RE = re.compile(r"\\bfrom=(?P<quote>[\\\"'])(?P<value>.*?)(?P=quote)")
_DISPLAY_NAME_ATTR_RE = re.compile(r"\\bdisplay_name=(?P<quote>[\\\"']).*?(?P=quote)")


def _cdata_body(body: str) -> str:
    """Render body text inside CDATA without entity-escaping normal message text."""
    return body.replace("]]>", "]]]]><![CDATA[>")


def render_msg_tag(
    *,
    sender: str,
    body: str,
    event_id: str | None = None,
    ts: str | None = None,
    display_name: str | None = None,
) -> str:
    """Render one Matrix message as a ``<msg ...><![CDATA[...]]></msg>`` tag."""
    attrs: list[str] = []
    if event_id is not None:
        attrs.append(f"event_id={xml_quoteattr(event_id)}")
    attrs.append(f"from={xml_quoteattr(sender)}")
    if display_name is not None:
        attrs.append(f"display_name={xml_quoteattr(display_name)}")
    if ts is not None:
        attrs.append(f"ts={xml_quoteattr(ts)}")
    return f"<msg {' '.join(attrs)}><![CDATA[{_cdata_body(body)}]]></msg>"


def enrich_msg_tags_with_display_names(
    text: str,
    member_display_names: dict[str, str] | None,
) -> str:
    """Add current display names to already-structured Matrix message tags."""
    if not member_display_names:
        return text

    def _enrich_tag(match: re.Match[str]) -> str:
        tag = match.group(0)
        if _DISPLAY_NAME_ATTR_RE.search(tag):
            return tag
        sender_match = _FROM_ATTR_RE.search(tag)
        if sender_match is None:
            return tag
        sender = html_unescape(sender_match.group("value"))
        display_name = member_display_names.get(sender)
        if not display_name:
            return tag
        return f"{tag[:-1]} display_name={xml_quoteattr(display_name)}>"

    return _MSG_OPEN_TAG_RE.sub(_enrich_tag, text)
