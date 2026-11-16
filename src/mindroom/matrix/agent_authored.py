"""Recognise the content marker that says an agent wrote a Matrix event."""

from __future__ import annotations

from collections.abc import Mapping

from mindroom.constants import AGENT_AUTHORED_KEY


def carries_agent_authored_marker(content: Mapping[str, object]) -> bool:
    """Return whether this content, or an edit's replacement content, carries the agent-authored marker.

    Only the key's presence counts: the marker can only take authority away, so no value of it needs trusting.
    """
    if AGENT_AUTHORED_KEY in content:
        return True
    new_content = content.get("m.new_content")
    return isinstance(new_content, Mapping) and AGENT_AUTHORED_KEY in new_content
