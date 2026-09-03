"""Shared rendering for model-facing Matrix ``<msg>`` prompt tags."""

from __future__ import annotations

import re
from html import unescape as html_unescape
from xml.sax.saxutils import quoteattr as xml_quoteattr


_MSG_OPEN_TAG_RE = re.compile(r"<msg\b[^>]*>")
_FROM_ATTR_RE = re.compile(r"\bfrom=(?P<quote>[\"'])(?P<value>.*?)(?P=quote)")
_DISPLAY_NAME_ATTR_RE = re.compile(r"\bdisplay_name=(?P<quote>[\"']).*?(?P=quote)")
_CDATA_START = "<![CDATA["
_CDATA_END = "]]>"


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


def _enrich_msg_open_tags(
    text: str,
    member_display_names: dict[str, str],
) -> str:
    """Refresh display-name attributes in markup text that is outside CDATA."""

    def _enrich_tag(match: re.Match[str]) -> str:
        tag = match.group(0)
        sender_match = _FROM_ATTR_RE.search(tag)
        if sender_match is None:
            return tag

        sender = html_unescape(sender_match.group("value"))
        display_name = member_display_names.get(sender)
        if not display_name:
            return tag

        rendered_display_name = f"display_name={xml_quoteattr(display_name)}"
        if _DISPLAY_NAME_ATTR_RE.search(tag):
            return _DISPLAY_NAME_ATTR_RE.sub(rendered_display_name, tag, count=1)
        return f"{tag[:-1]} {rendered_display_name}>"

    return _MSG_OPEN_TAG_RE.sub(_enrich_tag, text)


def enrich_msg_tags_with_display_names(
    text: str,
    member_display_names: dict[str, str] | None,
) -> str:
    """Refresh structured Matrix message tags without modifying CDATA message bodies."""
    if not member_display_names:
        return text

    rendered_parts: list[str] = []
    cursor = 0
    while cursor < len(text):
        cdata_start = text.find(_CDATA_START, cursor)
        if cdata_start == -1:
            rendered_parts.append(_enrich_msg_open_tags(text[cursor:], member_display_names))
            break

        rendered_parts.append(_enrich_msg_open_tags(text[cursor:cdata_start], member_display_names))
        cdata_end = text.find(_CDATA_END, cdata_start + len(_CDATA_START))
        if cdata_end == -1:
            rendered_parts.append(text[cdata_start:])
            break

        cdata_end += len(_CDATA_END)
        rendered_parts.append(text[cdata_start:cdata_end])
        cursor = cdata_end

    return "".join(rendered_parts)
