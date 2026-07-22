"""Shared rendering for model-facing Matrix ``<msg>`` prompt tags."""

from __future__ import annotations

import re
from xml.sax.saxutils import quoteattr as xml_quoteattr

_LEGACY_ASSISTANT_MSG_TAG = re.compile(
    r'^<msg event_id="(?P<event_id>[^"]+)" from="(?P<sender>[^"]+)"><!\[CDATA\[(?P<body>.*)\]\]></msg>$',
    re.DOTALL,
)


def _cdata_body(body: str) -> str:
    """Render body text inside CDATA without entity-escaping normal message text."""
    return body.replace("]]>", "]]]]><![CDATA[>")


def render_msg_tag(
    *,
    sender: str,
    body: str,
    event_id: str | None = None,
    ts: str | None = None,
) -> str:
    """Render one Matrix message as a ``<msg ...><![CDATA[...]]></msg>`` tag."""
    attrs: list[str] = []
    if event_id is not None:
        attrs.append(f"event_id={xml_quoteattr(event_id)}")
    attrs.append(f"from={xml_quoteattr(sender)}")
    if ts is not None:
        attrs.append(f"ts={xml_quoteattr(ts)}")
    return f"<msg {' '.join(attrs)}><![CDATA[{_cdata_body(body)}]]></msg>"


def unwrap_legacy_assistant_msg_tag(content: str, *, response_event_id: str) -> str | None:
    """Unwrap a canonical assistant tag previously persisted for one response event."""
    match = _LEGACY_ASSISTANT_MSG_TAG.fullmatch(content)
    if match is None or match.group("event_id") != response_event_id:
        return None
    body = match.group("body").replace("]]]]><![CDATA[>", "]]>")
    if (
        render_msg_tag(
            sender=match.group("sender"),
            body=body,
            event_id=response_event_id,
        )
        != content
    ):
        return None
    return body
