"""Origin of an agent call: which conversation the caller started it from.

MindRoom Chat stamps ``origin`` into the call room's ``io.mindroom.agent_call``
state. The call manager validates it against the caller's access and turns the
origin conversation into a bounded brief for the voice agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.custom_tools.attachment_helpers import room_access_allowed
from mindroom.entity_resolution import current_internal_sender_ids
from mindroom.logging_config import get_logger
from mindroom.matrix.conversation_reads import complete_thread_history, projected_thread_history
from mindroom.token_budget import approximate_o200k_tokens

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mindroom.matrix.thread_history_result import ThreadHistoryResult
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)

AGENT_CALL_STATE_EVENT_TYPE = "io.mindroom.agent_call"
CALL_BRIEF_TOKEN_BUDGET = 6_000
_BRIEF_MESSAGE_MAX_CHARS = 2_000
_ROOM_ORIGIN_READ_LIMIT = 200
_THREAD_SUMMARY_CONTENT_KEY = "io.mindroom.thread_summary"


@dataclass(frozen=True)
class CallOrigin:
    """Room and optional thread a call was started from."""

    room_id: str
    thread_id: str | None


@dataclass(frozen=True)
class _CallBriefMessage:
    """One origin message, labelled for the voice agent."""

    label: str
    body: str


@dataclass(frozen=True)
class CallOriginContext:
    """Validated origin plus the snapshot of its conversation taken at join."""

    origin: CallOrigin
    room_name: str
    thread_title: str | None
    messages: tuple[_CallBriefMessage, ...]


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


def _reject(origin: CallOrigin, reason: str, **fields: object) -> None:
    logger.warning("call_origin_rejected", room_id=origin.room_id, thread_id=origin.thread_id, reason=reason, **fields)


async def _read_origin_history(origin: CallOrigin, context: ToolRuntimeContext) -> ThreadHistoryResult | None:
    """Read the origin conversation, or return ``None`` when a thread origin is not a real thread root."""
    if origin.thread_id is not None:
        history = await complete_thread_history(context.conversation_reader, origin.room_id, origin.thread_id)
        return history if history and history[0].event_id == origin.thread_id else None
    page = await context.conversation_reader.read_strict(
        room_id=origin.room_id,
        thread_id=None,
        limit=_ROOM_ORIGIN_READ_LIMIT,
    )
    return projected_thread_history(page, complete=False)


async def resolve_call_origin_context(
    origin: CallOrigin,
    *,
    context: ToolRuntimeContext,
) -> CallOriginContext | None:
    """Snapshot the origin conversation for the caller, or ``None`` when any check fails.

    The origin is stamped by the caller, so it is only trusted after the same
    cross-room check that gates the Matrix tools: the caller must be allowed to
    use this agent and be currently joined to the origin room. The agent must
    also be in that room, and a stamped thread must be rooted at the stamped
    event. Every failure, including a failed read, rejects instead of raising.
    """
    try:
        access_allowed = await room_access_allowed(context, origin.room_id)
    except Exception as error:
        _reject(origin, "caller_cannot_access_origin", exc_info=True, error=str(error))
        return None
    if not access_allowed:
        _reject(origin, "caller_cannot_access_origin")
        return None
    room = context.client.rooms.get(origin.room_id)
    if room is None:
        _reject(origin, "agent_not_in_origin")
        return None
    try:
        history = await _read_origin_history(origin, context)
    except Exception as error:
        _reject(origin, "origin_read_failed", error=str(error))
        return None
    if history is None:
        _reject(origin, "not_a_thread_root")
        return None

    trusted_sender_ids = current_internal_sender_ids(context.config, context.runtime_paths)
    thread_title: str | None = None
    messages: list[_CallBriefMessage] = []
    for message in history:
        if message.sender in trusted_sender_ids and isinstance(message.content.get(_THREAD_SUMMARY_CONTENT_KEY), dict):
            summary = message.content[_THREAD_SUMMARY_CONTENT_KEY].get("summary")
            thread_title = summary if isinstance(summary, str) and summary else None
            continue
        body = message.body.strip()
        if not body:
            continue
        label = "You" if message.sender == context.client.user_id else room.user_name(message.sender) or message.sender
        messages.append(_CallBriefMessage(label=label, body=body))
    return CallOriginContext(
        origin=origin,
        room_name=room.display_name or origin.room_id,
        thread_title=thread_title,
        messages=tuple(messages),
    )


def _capped(body: str) -> str:
    if len(body) <= _BRIEF_MESSAGE_MAX_CHARS:
        return body
    return f"{body[: _BRIEF_MESSAGE_MAX_CHARS - 1]}…"


def _newest_lines_that_could_fit(lines: Sequence[str], token_budget: int) -> int:
    """Upper bound on how many newest lines fit, from per-line token counts alone."""
    total = 0
    for count, line in enumerate(reversed(lines)):
        total += approximate_o200k_tokens(line)
        if total > token_budget:
            return count
    return len(lines)


def build_call_brief(origin_context: CallOriginContext, *, token_budget: int) -> str:
    """Render the newest origin messages that fit ``token_budget``, oldest first.

    The returned text never exceeds ``token_budget`` tokens, measured on the
    final joined string. When older messages are dropped, a marker line says so.
    """
    place = "thread" if origin_context.origin.thread_id is not None else "room conversation"
    title = f' titled "{origin_context.thread_title}"' if origin_context.thread_title else ""
    header = (
        f"## Conversation this call is about\n"
        f'The caller started this call from a {place}{title} in the room "{origin_context.room_name}". '
        "Its recent messages follow, oldest first. Treat them as shared context the caller may refer to."
    )
    if approximate_o200k_tokens(header) > token_budget:
        return ""
    lines = [f"- {message.label}: {_capped(message.body)}" for message in origin_context.messages]

    def render(kept_count: int) -> str:
        omitted = len(lines) - kept_count
        marker = [f"[{omitted} earlier messages omitted]"] if omitted else []
        return "\n".join([header, *marker, *lines[len(lines) - kept_count :]])

    def fits(kept_count: int) -> bool:
        return approximate_o200k_tokens(render(kept_count)) <= token_budget

    if not fits(0):
        return header
    # Binary search the longest newest-suffix whose joined text fits. Every accepted
    # length was measured, so the result is within budget even if BPE merges make
    # the token count slightly non-monotonic.
    low, high = 0, _newest_lines_that_could_fit(lines, token_budget)
    while low < high:
        mid = (low + high + 1) // 2
        if fits(mid):
            low = mid
        else:
            high = mid - 1
    return render(low)
