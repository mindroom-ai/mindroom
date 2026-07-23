"""Shared rendering for model-facing Matrix ``<msg>`` prompt tags."""

from __future__ import annotations

from xml.sax.saxutils import quoteattr as xml_quoteattr


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
