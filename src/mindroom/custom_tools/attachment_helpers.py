"""Shared helpers used by multiple custom tool modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.authorization import is_sender_allowed_for_responder
from mindroom.contacts.turns import contact_room
from mindroom.matrix.client_room_admin import get_room_members
from mindroom.matrix.projected_thread_membership import resolve_thread_root_event_id
from mindroom.matrix.state import resolve_room_id
from mindroom.requester_identity import (
    equivalent_requester_ids,
    is_human_requester_id,
    resolve_human_requester_alias,
)

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


async def requester_joined_target_room(context: ToolRuntimeContext, room_id: str) -> bool:
    """Return whether the requester currently belongs to one requested room.

    Responder access answers whether a requester may converse with this agent,
    never whether they belong to one specific room, so any room other than the
    conversation room additionally requires proven current membership. A
    membership fetch that fails counts as not joined.

    Only a human principal can hold that membership. A managed sender is joined
    to every room its agent serves, so its own membership would prove nothing,
    and an agent relaying another agent's message must stay in their shared
    conversation.

    The agent's own contact room, active or removed, belongs to its owner alone,
    who is only invited to it, because there the agent speaks for its owner.
    """
    if room_id == context.room_id:
        return True
    config = context.current_config
    if not is_human_requester_id(context.requester_id, config, context.runtime_paths):
        return False
    if (contact := contact_room(config, context.runtime_paths, room_id, context.agent_name)) is not None:
        record, side = contact
        owner = record.party(side).owner
        return resolve_human_requester_alias(context.requester_id, config, context.runtime_paths) == owner
    member_ids = await get_room_members(context.client, room_id)
    return member_ids is not None and not member_ids.isdisjoint(
        equivalent_requester_ids(context.requester_id, config, context.runtime_paths),
    )


async def room_access_allowed(context: ToolRuntimeContext, room_id: str, *, contact_room_ok: bool = False) -> bool:
    """Return whether the requester may act in the given room.

    The agent's own contact room, active or removed and even as the current room, is open only to callers that set
    ``contact_room_ok``: they only read it, or they run the contact policy on everything they post there and post
    nothing into a removed contact's room, as ``matrix_message`` does.
    """
    if not room_id:
        return False
    config = context.current_config
    if not contact_room_ok and contact_room(config, context.runtime_paths, room_id, context.agent_name) is not None:
        return False
    if room_id == context.room_id:
        return True
    return is_sender_allowed_for_responder(
        context.requester_id,
        context.agent_name,
        room_id,
        config,
        context.runtime_paths,
        context.require_agent_reply_memberships(),
    ) and await requester_joined_target_room(context, room_id)


def unknown_room_error(room_id: str) -> str:
    """Return the error for a requested room that names no known room."""
    return f"Unknown room {room_id!r}; pass a room ID, alias, or configured room name."


def resolve_requested_room_id(
    context: ToolRuntimeContext,
    room_id: str | None,
) -> tuple[str | None, str | None]:
    """Resolve the requested room target or return a validation error.

    A configured room name resolves to its room ID; any other name that is neither a room ID nor an alias is unknown.
    """
    if room_id is None:
        return context.room_id, None
    normalized_room_id = room_id.strip()
    if not normalized_room_id:
        return None, "room_id must be a non-empty string."
    resolved_room_id = resolve_room_id(normalized_room_id, context.runtime_paths)
    if not resolved_room_id.startswith(("!", "#")):
        return None, unknown_room_error(normalized_room_id)
    return resolved_room_id, None


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
class _CanonicalToolThreadTarget:
    """Shared tool-facing thread-target normalization result."""

    requested_thread_id: str | None
    canonical_thread_id: str | None
    error: str | None = None


async def resolve_canonical_tool_thread_target(
    context: ToolRuntimeContext,
    *,
    room_id: str,
    thread_id: str | None,
    allow_context_fallback: bool = True,
    fail_closed_on_normalization_error: bool = False,
) -> _CanonicalToolThreadTarget:
    """Resolve one tool thread target into the canonical thread root or a stable error."""
    requested_thread_id = resolve_context_thread_id(
        context,
        room_id=room_id,
        thread_id=thread_id,
        allow_context_fallback=allow_context_fallback,
    )
    if requested_thread_id is None:
        return _CanonicalToolThreadTarget(
            requested_thread_id=None,
            canonical_thread_id=None,
            error="thread_id is required when no active thread context is available for the target room.",
        )

    try:
        canonical_thread_id = await resolve_thread_root_event_id(
            context.conversation_reader,
            context.relations,
            room_id,
            requested_thread_id,
        )
    except Exception:
        if not fail_closed_on_normalization_error:
            raise
        canonical_thread_id = None
    if canonical_thread_id is None:
        return _CanonicalToolThreadTarget(
            requested_thread_id=requested_thread_id,
            canonical_thread_id=None,
            error="Failed to resolve a canonical thread root for the target event.",
        )

    return _CanonicalToolThreadTarget(
        requested_thread_id=requested_thread_id,
        canonical_thread_id=canonical_thread_id,
    )


async def explicit_thread_root(context: ToolRuntimeContext, *, room_id: str, thread_id: str) -> str:
    """Return the root of the thread an explicit thread ID belongs to, or the ID itself.

    Matrix rejects a reply as a thread root, so a reply event ID resolves to its
    thread's root. Unlike the thread tools, an ID whose thread cannot be proven is
    not refused but kept as given: a room-level message no thread contains yet
    can start one, and the homeserver answers for an ID it cannot use.
    """
    target = await resolve_canonical_tool_thread_target(
        context,
        room_id=room_id,
        thread_id=thread_id,
        fail_closed_on_normalization_error=True,
    )
    return target.canonical_thread_id or thread_id


async def resolve_current_room_thread_root(
    context: ToolRuntimeContext,
    thread_id: str | None,
) -> tuple[str | None, str | None]:
    """Return the root of one thread in the current room, or why none resolves.

    The active thread is already its root; an explicit thread root or reply
    event ID resolves through the thread's relations and fails closed.
    """
    if thread_id is None:
        if context.resolved_thread_id is None:
            return None, "thread_id is required when no active thread context is available."
        return context.resolved_thread_id, None
    target = await resolve_canonical_tool_thread_target(
        context,
        room_id=context.room_id,
        thread_id=thread_id,
        fail_closed_on_normalization_error=True,
    )
    return target.canonical_thread_id, target.error
