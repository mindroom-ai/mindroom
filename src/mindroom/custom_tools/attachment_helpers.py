"""Shared helpers used by multiple custom tool modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.authorization import is_authorized_sender

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

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
    if not isinstance(room_id, str) or not room_id:
        return False
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


def resolve_requested_room_id(
    context: ToolRuntimeContext,
    room_id: object,
) -> tuple[str | None, str | None]:
    """Resolve the requested room target or return a validation error."""
    if room_id is None:
        return context.room_id, None
    if not isinstance(room_id, str):
        return None, "room_id must be a non-empty string."
    normalized_room_id = room_id.strip()
    if not normalized_room_id:
        return None, "room_id must be a non-empty string."
    return normalized_room_id, None


def resolve_context_thread_id(
    context: ToolRuntimeContext,
    *,
    room_id: str,
    thread_id: str | None,
    allow_context_fallback: bool = True,
    room_timeline_sentinel: str | None = None,
) -> str | None:
    """Return a target thread ID only when it is valid for the chosen room."""
    if room_timeline_sentinel is not None and thread_id == room_timeline_sentinel:
        return None
    if thread_id is not None:
        return thread_id
    if allow_context_fallback and room_id == context.room_id:
        return context.resolved_thread_id
    return None


@dataclass(frozen=True)
class CanonicalToolThreadTarget:
    """Shared tool-facing thread-target normalization result."""

    requested_thread_id: str | None
    canonical_thread_id: str | None
    error: str | None = None


async def resolve_canonical_tool_thread_target(
    context: ToolRuntimeContext,
    *,
    room_id: str,
    thread_id: str | None,
    normalize_thread_id: Callable[[str, str], Awaitable[str | None]],
    allow_context_fallback: bool = True,
    fail_closed_on_normalization_error: bool = False,
    room_timeline_sentinel: str | None = None,
) -> CanonicalToolThreadTarget:
    """Resolve one tool thread target into the canonical thread root or a stable error."""
    requested_thread_id = resolve_context_thread_id(
        context,
        room_id=room_id,
        thread_id=thread_id,
        allow_context_fallback=allow_context_fallback,
        room_timeline_sentinel=room_timeline_sentinel,
    )
    if requested_thread_id is None:
        return CanonicalToolThreadTarget(
            requested_thread_id=None,
            canonical_thread_id=None,
            error="thread_id is required when no active thread context is available for the target room.",
        )

    try:
        canonical_thread_id = await normalize_thread_id(room_id, requested_thread_id)
    except Exception:
        if not fail_closed_on_normalization_error:
            raise
        canonical_thread_id = None
    if canonical_thread_id is None:
        return CanonicalToolThreadTarget(
            requested_thread_id=requested_thread_id,
            canonical_thread_id=None,
            error="Failed to resolve a canonical thread root for the target event.",
        )

    return CanonicalToolThreadTarget(
        requested_thread_id=requested_thread_id,
        canonical_thread_id=canonical_thread_id,
    )
