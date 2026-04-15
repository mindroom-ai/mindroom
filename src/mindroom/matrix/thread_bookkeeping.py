"""Tool-facing Matrix thread bookkeeping helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_membership import (
    conversation_cache_thread_membership_access_for_client,
    fetch_event_info_for_client,
    fetch_event_info_from_conversation_cache,
    resolve_event_thread_id,
    resolve_related_event_thread_id,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    import nio

    from mindroom.matrix.conversation_cache import ConversationCacheProtocol


async def event_requires_thread_bookkeeping(
    client: nio.AsyncClient,
    room_id: str,
    *,
    event_type: str,
    content: Mapping[str, object],
    conversation_cache: ConversationCacheProtocol | None,
) -> bool:
    """Return whether one outbound event payload targets a thread."""
    if event_type != "m.room.message":
        return False
    event_info = EventInfo.from_event({"type": event_type, "content": dict(content)})
    return isinstance(
        await resolve_event_thread_id(
            room_id,
            event_info,
            access=conversation_cache_thread_membership_access_for_client(
                client,
                conversation_cache=conversation_cache,
            ),
        ),
        str,
    )


async def redaction_requires_thread_bookkeeping(
    client: nio.AsyncClient,
    room_id: str,
    *,
    event_id: str,
    conversation_cache: ConversationCacheProtocol | None,
) -> bool:
    """Return whether one redaction target can affect thread-scoped cache state."""
    if conversation_cache is None:
        target_event_info = await fetch_event_info_for_client(
            client,
            room_id,
            event_id,
            strict=True,
        )
    else:
        target_event_info = await fetch_event_info_from_conversation_cache(
            conversation_cache,
            room_id,
            event_id,
            strict=True,
        )
    if target_event_info is not None and target_event_info.is_reaction:
        return False
    return isinstance(
        await resolve_related_event_thread_id(
            room_id,
            event_id,
            access=conversation_cache_thread_membership_access_for_client(
                client,
                conversation_cache=conversation_cache,
            ),
        ),
        str,
    )
