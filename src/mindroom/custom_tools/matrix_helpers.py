"""Shared non-rendering helpers for Matrix tool modules."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections import deque
    from threading import Lock

    from mindroom.tool_system.runtime_context import ToolRuntimeContext


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
