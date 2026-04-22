"""Shared helpers for Matrix tool modules."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

from mindroom.matrix.visible_body import bundled_visible_body_preview

if TYPE_CHECKING:
    from collections import deque
    from threading import Lock

    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def message_preview(body: object, max_length: int = 120) -> str:
    """Return a compact preview of a message body, truncated to max_length."""
    if not isinstance(body, str):
        return ""
    compact = " ".join(body.split())
    if len(compact) <= max_length:
        return compact
    return f"{compact[: max_length - 3].rstrip()}..."


def bundled_replacement_body(event_source: object, *, local_agent_domain: str | None) -> str | None:
    """Return one trusted preview body from bundled replacement metadata."""
    unsigned = None
    if isinstance(event_source, dict):
        event_source_dict = cast("dict[str, object]", event_source)
        unsigned = event_source_dict.get("unsigned")
    for container in (unsigned, event_source):
        if not isinstance(container, dict):
            continue
        container_dict = cast("dict[str, object]", container)
        relations = container_dict.get("m.relations")
        if not isinstance(relations, dict):
            continue
        relations_dict = cast("dict[str, object]", relations)
        replacement = relations_dict.get("m.replace")
        if not isinstance(replacement, dict):
            continue
        replacement_dict = cast("dict[str, object]", replacement)
        for candidate in (
            replacement_dict,
            replacement_dict.get("event"),
            replacement_dict.get("latest_event"),
        ):
            body = bundled_visible_body_preview(candidate, local_agent_domain=local_agent_domain)
            if body is not None:
                return body
    return None


def check_rate_limit(
    *,
    lock: Lock,
    recent_actions: dict[tuple[str, str, str], deque[float]],
    window_seconds: float,
    max_actions: int,
    tool_name: str,
    context: ToolRuntimeContext,
    room_id: str,
    weight: int = 1,
) -> str | None:
    """Enforce a per-(agent, requester, room) sliding-window rate limit.

    Returns an error message string if the limit is exceeded, or None if allowed.
    """
    key = (context.agent_name, context.requester_id, room_id)
    now = time.monotonic()
    cutoff = now - window_seconds
    action_weight = max(1, weight)

    with lock:
        history = recent_actions[key]
        while history and history[0] < cutoff:
            history.popleft()
        if len(history) + action_weight > max_actions:
            return f"Rate limit exceeded for {tool_name} actions ({max_actions} per {int(window_seconds)}s)."
        history.extend(now for _ in range(action_weight))

        stale_keys: list[tuple[str, str, str]] = []
        for k, v in recent_actions.items():
            if k == key:
                continue
            while v and v[0] < cutoff:
                v.popleft()
            if not v:
                stale_keys.append(k)
        for k in stale_keys:
            del recent_actions[k]

    return None
