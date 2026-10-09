"""Move a Matrix thread into another room by copying its conversation."""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.constants import (
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    SKIP_MENTIONS_KEY,
    STREAM_STATUS_APPROVAL_PENDING,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
    TOOL_TRACE_CONTENT_KEY,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

# Everything else on a message is run, stream, delivery, relation, or relay
# state that belongs to the original event and would mislead the runtime or
# clients if it reappeared on a copy.
_COPIED_CONTENT_KEYS = (
    "msgtype",
    "body",
    "format",
    "formatted_body",
    "url",
    "file",
    "info",
    "filename",
    TOOL_TRACE_CONTENT_KEY,
)
_IN_PROGRESS_STREAM_STATUSES = frozenset(
    {STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING, STREAM_STATUS_APPROVAL_PENDING}
)
_MEDIA_MSGTYPES = frozenset({"m.image", "m.file", "m.audio", "m.video"})


@dataclass(frozen=True, slots=True)
class PlannedCopy:
    """One message to re-post in the target thread, without its thread relation."""

    poster: str
    content: dict[str, Any]
    source_event_id: str


def plan_thread_copy(
    messages: Sequence[ResolvedVisibleMessage],
    *,
    entity_name_for_sender: Callable[[str], str | None],
    target_posters: frozenset[str],
    display_names: Mapping[str, str],
) -> list[PlannedCopy]:
    """Return the posts that recreate one thread's conversation in another room.

    An entity that can post in the target room re-posts its own messages, so
    each agent still sees its earlier replies as its own turns. Everyone else
    is relayed by the router with visible attribution.
    """
    plan: list[PlannedCopy] = []
    for message in messages:
        if message.content.get("msgtype") == "m.notice" or message.stream_status in _IN_PROGRESS_STREAM_STATUSES:
            continue
        content = {key: message.content[key] for key in _COPIED_CONTENT_KEYS if key in message.content}
        # Copies are history, never requests: no mention in them may wake an agent.
        content[SKIP_MENTIONS_KEY] = True
        content["m.mentions"] = {}
        poster = entity_name_for_sender(message.sender)
        if poster is None or poster not in target_posters:
            poster = ROUTER_AGENT_NAME
            _attribute_relay(content, message.sender, display_names.get(message.sender, message.sender))
        plan.append(PlannedCopy(poster=poster, content=content, source_event_id=message.event_id))
    return plan


def _attribute_relay(content: dict[str, Any], sender: str, name: str) -> None:
    """Name the original author on a router-posted copy."""
    # No source kind: the relay is attributed in prompts but never becomes a human turn.
    content[ORIGINAL_SENDER_KEY] = sender
    body = str(content.get("body", ""))
    if content.get("msgtype") in _MEDIA_MSGTYPES:
        # With a filename present, clients show the body as the caption.
        content["filename"] = content.get("filename") or body
    content["body"] = f"{name}: {body}"
    if "formatted_body" in content:
        content["formatted_body"] = f"<strong>{html.escape(name)}</strong>: {content['formatted_body']}"
