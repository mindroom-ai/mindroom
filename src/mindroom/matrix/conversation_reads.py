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
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.conversation_hydration import HYDRATED_PROMPT_WINDOW_MESSAGES
from mindroom.matrix.thread_diagnostics import (
    THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_DEGRADED,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
)
from mindroom.matrix.thread_history_result import ThreadHistoryResult, thread_history_result

if TYPE_CHECKING:
    from mindroom.event_journal import ConversationCursor, ConversationPage, ConversationReadView
    from mindroom.matrix.conversation_hydration import ConversationHydrator

logger = get_logger(__name__)


def projected_thread_history(
    page: ConversationPage,
    *,
    complete: bool,
    source_degraded: bool = False,
) -> ThreadHistoryResult:
    """Render one projected page as the history shape the prompt path consumes.

    ``complete`` is the caller's guarantee, not something the page can report:
    a strict read has hydrated and resolved every refresh, a non-blocking read
    has done neither. It stays separate because a page that omitted a message
    and a conversation that never had one look identical from here.
    """
    messages = [
        ResolvedVisibleMessage.from_message_data(
            {
                "sender": message.sender,
                "body": str(message.content.get("body", "")),
                "timestamp": message.created_ts,
                "event_id": message.logical_event_id,
                "content": dict(message.content),
            },
            thread_id=message.thread_id,
            latest_event_id=message.revision_event_id,
        )
        for message in page.messages
    ]
    for message, projected in zip(messages, page.messages, strict=True):
        if projected.revision_event_id != projected.logical_event_id:
            message.edited_timestamp = projected.revision_ts
    diagnostics = (
        {
            THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_DEGRADED,
            THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
        }
        if source_degraded
        else None
    )
    return thread_history_result(
        messages,
        # A page with more behind it is not the whole conversation, however
        # hard the caller worked for it. Consumers that count what they got and
        # record the total -- thread summaries do -- would otherwise write the
        # size of a suffix down as the size of the thread.
        is_full_history=complete and not page.refresh_pending and page.next_cursor is None,
        diagnostics=diagnostics,
    )


class _StaleConversationError(RuntimeError):
    """A strict read could not obtain the server-authoritative content."""


@dataclass(frozen=True, slots=True)
class ConversationReader:
    """Bounded conversation reads, hydrated on first use."""

    store: ConversationReadView
    hydrator: ConversationHydrator

    async def may_have_unread_history(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        source_event_id: str,
    ) -> bool:
        """Return whether local absence cannot prove this conversation is fresh."""
        if await self.store.conversation_is_hydrated(room_id=room_id, thread_id=thread_id):
            return False
        return await self.store.has_other_admitted_room_event(
            room_id=room_id,
            event_id=source_event_id,
        )

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


async def complete_thread_history(
    reader: ConversationReader,
    room_id: str,
    thread_id: str,
) -> ThreadHistoryResult:
    """Return one thread's complete history for a caller outside the turn path.

    Summaries, schedulers, and Matrix tools all want the same thing the prompt
    path wants -- a conversation with nothing missing from it -- without also
    wanting the resolver's thread-identity machinery.
    """
    page = await reader.read_strict(
        room_id=room_id,
        thread_id=thread_id,
        limit=HYDRATED_PROMPT_WINDOW_MESSAGES,
    )
    return projected_thread_history(page, complete=True)
