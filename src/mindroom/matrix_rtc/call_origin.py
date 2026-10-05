"""Origin of an agent call: which conversation the caller started it from.

MindRoom Chat stamps ``origin`` into the call room's ``io.mindroom.agent_call``
state. The call manager validates it against the caller's access and turns the
origin conversation into a bounded brief for the voice agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.token_budget import approximate_o200k_tokens

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

AGENT_CALL_STATE_EVENT_TYPE = "io.mindroom.agent_call"
CALL_BRIEF_TOKEN_BUDGET = 6_000
_BRIEF_MESSAGE_MAX_CHARS = 2_000


@dataclass(frozen=True)
class CallOrigin:
    """Room and optional thread a call was started from."""

    room_id: str
    thread_id: str | None


@dataclass(frozen=True)
class CallBriefMessage:
    """One origin message, labelled for the voice agent."""

    label: str
    body: str


@dataclass(frozen=True)
class CallOriginContext:
    """Validated origin plus the snapshot of its conversation taken at join."""

    origin: CallOrigin
    room_name: str
    thread_title: str | None
    messages: tuple[CallBriefMessage, ...]


def parse_call_origin(
    state_events: Sequence[Mapping[str, Any]],
    *,
    requester_id: str,
    agent_user_id: str,
) -> CallOrigin | None:
    """Return the origin only when the sole caller stamped it for this agent."""
    for event in state_events:
        if event.get("type") == AGENT_CALL_STATE_EVENT_TYPE and event.get("state_key") == "":
            return _origin_from_event(event, requester_id=requester_id, agent_user_id=agent_user_id)
    return None


def _origin_from_event(
    event: Mapping[str, Any],
    *,
    requester_id: str,
    agent_user_id: str,
) -> CallOrigin | None:
    content = event.get("content")
    if not isinstance(content, dict) or content.get("version") != 1:
        return None
    if (
        event.get("sender") != requester_id
        or content.get("creator_user_id") != requester_id
        or content.get("agent_user_id") != agent_user_id
    ):
        return None
    origin = content.get("origin")
    if not isinstance(origin, dict):
        return None
    room_id = origin.get("room_id")
    thread_id = origin.get("thread_id")
    if not isinstance(room_id, str) or not room_id:
        return None
    if thread_id is not None and (not isinstance(thread_id, str) or not thread_id):
        return None
    return CallOrigin(room_id=room_id, thread_id=thread_id)


def _capped(body: str) -> str:
    if len(body) <= _BRIEF_MESSAGE_MAX_CHARS:
        return body
    return f"{body[: _BRIEF_MESSAGE_MAX_CHARS - 1]}…"


def build_call_brief(origin_context: CallOriginContext, *, token_budget: int) -> str:
    """Render the newest origin messages that fit ``token_budget``, oldest first."""
    place = "thread" if origin_context.origin.thread_id is not None else "room conversation"
    title = f' titled "{origin_context.thread_title}"' if origin_context.thread_title else ""
    header = (
        f"## Conversation this call is about\n"
        f'The caller started this call from a {place}{title} in the room "{origin_context.room_name}". '
        "Its recent messages follow, oldest first. Treat them as shared context the caller may refer to."
    )
    used = approximate_o200k_tokens(header)
    if used > token_budget:
        return ""
    kept: list[str] = []
    for message in reversed(origin_context.messages):
        line = f"- {message.label}: {_capped(message.body)}"
        cost = approximate_o200k_tokens(line)
        if used + cost > token_budget:
            break
        kept.append(line)
        used += cost
    kept.reverse()
    omitted = len(origin_context.messages) - len(kept)
    if omitted:
        marker = f"[{omitted} earlier messages omitted]"
        while kept and used + approximate_o200k_tokens(marker) > token_budget:
            used -= approximate_o200k_tokens(kept.pop(0))
            omitted += 1
            marker = f"[{omitted} earlier messages omitted]"
        kept.insert(0, marker)
    return "\n".join([header, *kept])
