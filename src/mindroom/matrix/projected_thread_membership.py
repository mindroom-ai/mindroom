"""Thread membership for callers outside the turn path, proven through the conversation projection.

Tools normalizing a thread target and outbound mutations classifying their
thread ask the same question a turn asks, so they get the same answer: a
candidate root is proven by reading its thread strictly from the projection,
which hydrates the conversation once and then follows sync.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.matrix.conversation_reads import complete_thread_history
from mindroom.matrix.thread_membership import (
    ThreadMembershipAccess,
    resolve_event_thread_membership,
    thread_messages_thread_membership_access,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.matrix.conversation_reads import ConversationReader
    from mindroom.matrix.event_info import EventInfo
    from mindroom.matrix.relation_lookup import RelationLookup

logger = get_logger(__name__)


def projected_thread_membership_access(
    reader: ConversationReader,
    relations: RelationLookup,
    *,
    fetch_event_info: Callable[[str, str], Awaitable[EventInfo | None]] | None = None,
) -> ThreadMembershipAccess:
    """Build membership access whose root proofs read the candidate's thread strictly.

    Every answer here may be acted on, so none of them is a guess. The thread
    lookup is journal-only and raises on a failed read, a strict read that
    cannot vouch for the thread raises, and a thread whose history may be
    incomplete -- replies this bot could not decrypt, a walk that stopped at a
    ceiling -- cannot be ruled out by the replies it happens to hold. Each of
    those is an unavailable proof, never "no thread".
    """
    return thread_messages_thread_membership_access(
        lookup_thread_id=relations.strict_thread_id,
        fetch_event_info=fetch_event_info or relations.event_info,
        fetch_thread_messages=lambda room_id, thread_id: complete_thread_history(reader, room_id, thread_id),
        absence_needs_full_history=True,
    )


async def _degradable_event_info(
    relations: RelationLookup,
    room_id: str,
    event_id: str,
) -> EventInfo | None:
    """Return one event's relations, or nothing when the lookup could not say.

    ``relations`` owns this read and memoizes it for the turn, so one turn
    resolving the same event from several places pays for one round trip. What
    it will not do is guess: it raises when it cannot tell a deleted event from
    one the homeserver refused to serve, because a caller resolving a reply
    target would otherwise attach the turn to the wrong conversation.

    Normalizing a thread ID is not that caller. It has a local answer for an
    event the homeserver cannot describe, so a failed lookup degrades to that
    rather than taking the tool down -- but it is logged, because the fallback
    can produce a plausible wrong thread rather than an obvious failure.
    """
    try:
        return await relations.event_info(room_id, event_id)
    except Exception:
        logger.warning(
            "Failed to resolve an event's relations for thread normalization; falling back to local state",
            room_id=room_id,
            event_id=event_id,
            exc_info=True,
        )
        return None


async def resolve_thread_root_event_id(
    reader: ConversationReader,
    relations: RelationLookup,
    room_id: str,
    event_id: str,
) -> str | None:
    """Resolve one event ID into a canonical thread root when thread membership can prove one."""
    normalized_event_id = event_id.strip()
    if not normalized_event_id:
        return None

    event_info = await _degradable_event_info(relations, room_id, normalized_event_id)
    if event_info is None:
        # Local state only. The homeserver was just asked for this exact
        # event and could not answer; asking it again resolves nothing.
        return await relations.admitted_thread_id(room_id, normalized_event_id)

    resolution = await resolve_event_thread_membership(
        room_id,
        event_info,
        event_id=normalized_event_id,
        allow_current_root=True,
        access=projected_thread_membership_access(
            reader,
            relations,
            fetch_event_info=lambda lookup_room_id, lookup_event_id: _degradable_event_info(
                relations,
                lookup_room_id,
                lookup_event_id,
            ),
        ),
    )
    return resolution.thread_id
