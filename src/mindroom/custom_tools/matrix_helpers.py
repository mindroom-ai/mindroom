"""Shared helpers for Matrix tool modules."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

import nio

from mindroom.matrix.message_content import resolve_event_source_content
from mindroom.matrix.visible_body import bundled_visible_body_preview, visible_body_from_event_source

if TYPE_CHECKING:
    from collections import deque
    from collections.abc import Collection
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


def _bundled_replacement_candidates(event_source: object) -> list[dict[str, object]]:
    """Return bundled replacement event-source candidates in preference order."""
    candidates: list[dict[str, object]] = []
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
            candidates.extend(
                [cast("dict[str, object]", candidate)] if isinstance(candidate, dict) else [],
            )
    return candidates


async def bundled_replacement_body(
    event_source: object,
    *,
    client: nio.AsyncClient,
    trusted_sender_ids: Collection[str] = (),
) -> str | None:
    """Return one trusted preview body from bundled replacement metadata."""
    for candidate in _bundled_replacement_candidates(event_source):
        resolved_candidate = await resolve_event_source_content(candidate, client)
        body = bundled_visible_body_preview(
            resolved_candidate,
            trusted_sender_ids=trusted_sender_ids,
        )
        if body is not None:
            return body
    return None


async def thread_root_body_preview(
    event: nio.Event,
    *,
    client: nio.AsyncClient,
    trusted_sender_ids: Collection[str] = (),
) -> str:
    """Return the canonical preview body for one thread root event."""
    if isinstance(event, nio.MegolmEvent):
        return "[encrypted]"
    replacement_body = await bundled_replacement_body(
        event.source,
        client=client,
        trusted_sender_ids=trusted_sender_ids,
    )
    if replacement_body is not None:
        return message_preview(replacement_body)
    event_source = event.source if isinstance(event.source, dict) else {}
    resolved_event_source = await resolve_event_source_content(event_source, client)
    content = resolved_event_source.get("content")
    fallback_body = ""
    if isinstance(content, dict):
        body = content.get("body")
        if isinstance(body, str):
            fallback_body = body
    return message_preview(
        visible_body_from_event_source(
            resolved_event_source,
            fallback_body,
            trusted_sender_ids=trusted_sender_ids,
        ),
    )


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
