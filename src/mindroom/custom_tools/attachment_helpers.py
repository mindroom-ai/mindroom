"""Shared helpers used by multiple custom tool modules."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.authorization import is_authorized_sender

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def normalize_str_list(values: list[str] | None, *, field_name: str) -> tuple[list[str], str | None]:
    """Validate and strip a list of string values, returning normalized list and optional error."""
    if values is None:
        return [], None

    normalized: list[str] = []
    for raw_value in values:
        if not isinstance(raw_value, str):
            return [], f"{field_name} entries must be strings."
        value = raw_value.strip()
        if value:
            normalized.append(value)
    return normalized, None


def room_access_allowed(context: ToolRuntimeContext, room_id: str) -> bool:
    """Return whether the requester may act in the given room."""
    if room_id == context.room_id:
        return True
    room_alias = room_id if room_id.startswith("#") else None
    return is_authorized_sender(
        context.requester_id,
        context.config,
        room_id,
        context.runtime_paths,
        room_alias=room_alias,
    )


def resolve_context_thread_id(
    context: ToolRuntimeContext,
    *,
    room_id: str,
    thread_id: str | None,
    allow_context_fallback: bool = True,
    room_timeline_sentinel: str | None = None,
    room_timeline_fallback_event_id: str | None = None,
) -> str | None:
    """Return a target thread ID only when it is valid for the chosen room."""
    if room_timeline_sentinel is not None and thread_id == room_timeline_sentinel:
        return None
    if thread_id is not None:
        return thread_id
    if allow_context_fallback and room_id == context.room_id:
        return context.resolved_thread_id or room_timeline_fallback_event_id
    return None
