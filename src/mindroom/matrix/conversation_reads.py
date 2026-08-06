"""The conversation-read API the rest of MindRoom uses.

Two kinds of caller exist, and they want opposite things when a message is
mid-refetch. Prompt assembly must not omit content, so it waits. A UI or hook
must not block on a homeserver, so it skips. Neither can see the redacted
revision, because the store never returns it to anyone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.event_journal import ConversationCursor, ConversationPage, PrincipalStore
    from mindroom.matrix.conversation_hydration import ConversationHydrator

logger = get_logger(__name__)


class _StaleConversationError(RuntimeError):
    """A strict read could not obtain the server-authoritative content."""


@dataclass(frozen=True, slots=True)
class _ConversationReader:
    """Bounded conversation reads, hydrated on first use."""

    store: PrincipalStore
    hydrator: ConversationHydrator

    async def read(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        limit: int,
        before: ConversationCursor | None = None,
    ) -> ConversationPage:
        """Return a bounded page without waiting for anything.

        Never blocks and never serves stale content: a message whose revision
        was redacted is simply absent until a strict read repairs it.
        """
        return await self.store.read_conversation(
            room_id=room_id,
            thread_id=thread_id,
            limit=limit,
            before=before,
        )

    async def read_strict(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        limit: int,
        before: ConversationCursor | None = None,
    ) -> ConversationPage:
        """Return a complete bounded page, hydrating and refetching as needed.

        Raises rather than returning a page with content missing. A caller
        building a prompt cannot tell an omitted message from a conversation
        that never had one, so silently dropping it would change what the model
        is answering.
        """
        await self.hydrator.ensure_hydrated(room_id=room_id, thread_id=thread_id)
        page = await self.store.read_conversation(
            room_id=room_id,
            thread_id=thread_id,
            limit=limit,
            before=before,
        )
        if not page.refresh_pending:
            return page
        await self.hydrator.resolve_refreshes(room_id=room_id, thread_id=thread_id)
        page = await self.store.read_conversation(
            room_id=room_id,
            thread_id=thread_id,
            limit=limit,
            before=before,
        )
        if page.refresh_pending:
            msg = (
                f"Conversation {room_id}/{thread_id} has "
                f"{len(page.refresh_pending)} message(s) awaiting a server refetch"
            )
            raise _StaleConversationError(msg)
        return page
