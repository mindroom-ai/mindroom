"""The conversation-read API the rest of MindRoom uses.

Two kinds of caller exist, and they want opposite things when a message is
mid-refetch. Prompt assembly must not omit content, so it waits. A UI or hook
must not block on a homeserver, so it skips. Neither can see the redacted
revision, because the store never returns it to anyone.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum, auto
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.matrix.agent_message_snapshot import AgentMessageSnapshot
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.conversation_hydration import HYDRATED_PROMPT_WINDOW_MESSAGES
from mindroom.matrix.thread_diagnostics import (
    THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_DEGRADED,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
)
from mindroom.matrix.thread_history_result import ThreadHistoryResult, thread_history_result

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.event_journal import ConversationCursor, ConversationPage, ConversationReadView
    from mindroom.matrix.conversation_hydration import ConversationHydrator

logger = get_logger(__name__)

# A caller asking for one sender's newest visible message wants the current
# one. Walking a whole conversation to find a sender who has said nothing for
# a long time would turn a status probe into a history read, so the answer is
# "nothing recent" rather than a page-by-page search.
_LATEST_SENDER_MESSAGE_WINDOW_MESSAGES = 50


class ThreadReadMode(Enum):
    """Why one caller is reading a conversation, in the caller's own terms.

    What this names is the caller's *contract*, not a storage policy: whether a
    read is on the live dispatch path and so must fail open rather than block.
    There are exactly two contracts because there are exactly two answers to
    that question; a caller that may block says so, and every other caller
    takes whatever is already local.
    """

    NONBLOCKING = auto()
    STRICT = auto()


def projected_visible_messages(page: ConversationPage) -> list[ResolvedVisibleMessage]:
    """Render one projected page as the visible messages every consumer reads.

    The single conversion from a projection row to the shape the rest of
    MindRoom renders, so a prompt and an exported thread cannot disagree about
    which revision is current or when it was made.
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
    return messages


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
    messages = projected_visible_messages(page)
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


@dataclass(frozen=True, slots=True)
class DeliveredResponse:
    """An answer one execution just put in a conversation, by logical event ID.

    The single fact about a conversation that its sender holds and the
    projection does not. A read issued straight after a send is echo-ordered,
    so until sync hands the event back the projection still answers with
    whatever preceded it.

    ``event_id`` is the logical event -- the one first sent -- not the newest
    physical revision of it. A streamed answer edits that same event on every
    progressive update, so the ID stays stable for the whole turn and matches
    what the projection keys a message on once the echo lands.
    """

    event_id: str
    body: str


def with_delivered_response(
    messages: Sequence[ResolvedVisibleMessage],
    delivered_response: DeliveredResponse,
    *,
    thread_id: str | None,
    sender: str,
) -> list[ResolvedVisibleMessage]:
    """Return one read's messages including an answer the caller just delivered.

    For the caller that must not read itself one message behind. Waiting for
    the echo is the other way to get this, and it would make the waiter the one
    reader here that blocks on a sync round-trip -- for a fact it is already
    holding. So the caller is believed, exactly as ``latest_thread_event_id``
    believes one holding a newer send.

    The cost is a projection plus a patch rather than a projection alone. It is
    bounded to one event and keyed on its logical ID, so an echo that lands
    first collapses the patch onto itself rather than duplicating it.

    What the sender knows better than the projection is exactly one thing: the
    final text. A streamed answer's placeholder can already be projected while
    the revision it was edited into is still send-side only, so the delivered
    body wins even over an echoed row. Everything else about an echoed event --
    who sent it, when the server stamped it, where it sits in the conversation
    -- is the projection's to state, and is left alone. Only an event the
    projection has never seen is built here, and it is the newest message in
    the conversation by construction.
    """
    patched = list(messages)
    for index, message in enumerate(patched):
        if message.event_id == delivered_response.event_id:
            patched[index] = replace(
                message,
                body=delivered_response.body,
                content={**message.content, "body": delivered_response.body},
            )
            return patched
    patched.append(
        ResolvedVisibleMessage.synthetic(
            sender=sender,
            body=delivered_response.body,
            event_id=delivered_response.event_id,
            # The server's stamp is the one part of an unechoed event the
            # sender cannot know, so it carries the send's own clock.
            timestamp=int(datetime.now(UTC).timestamp() * 1000),
            content={"msgtype": "m.text", "body": delivered_response.body},
            thread_id=thread_id,
        ),
    )
    return patched


class _StaleConversationError(RuntimeError):
    """A strict read could not obtain the server-authoritative content."""


@dataclass(frozen=True, slots=True)
class ConversationReader:
    """Bounded conversation reads, hydrated on first use."""

    store: ConversationReadView
    hydrator: ConversationHydrator

    async def may_have_unread_history(self, *, room_id: str, thread_id: str | None) -> bool:
        """Return whether local absence cannot prove this conversation is fresh.

        Hydration is the only thing that ever proves it. A conversation nobody
        walked has no evidence behind its emptiness, and a room that predates
        the journal looks exactly like one that has never held a message --
        which is why guessing from what else the journal happens to hold
        answers "fresh" about a room full of history, right up until a second
        event lands in it.
        """
        return not await self.store.conversation_is_hydrated(room_id=room_id, thread_id=thread_id)

    async def hydration_was_truncated(self, *, room_id: str, thread_id: str | None) -> bool:
        """Return whether a walk ran for this conversation and gave up early.

        A bounded walk that stopped at a ceiling installs a hydration marker
        over a partial conversation, and its last page is shaped exactly like
        the last page of a whole one. Nothing about the page distinguishes
        them, so a caller reporting completeness has to ask.

        Asked this way round on purpose. A conversation with no hydration row
        is not complete, but nothing is missing from it either -- there was
        never anything to walk, which is every brand-new room. Only a walk that
        ran and gave up proves the page is a suffix.
        """
        return await self.store.conversation_hydration_was_truncated(room_id=room_id, thread_id=thread_id)

    async def latest_thread_event_id(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str | None = None,
        existing_event_id: str | None = None,
        known_latest_thread_event_id: str | None = None,
    ) -> str | None:
        """Return the event an MSC3440 reply fallback should point at.

        Only when nothing else already answers the question: a caller editing
        an existing event, or one that already knows what it is replying to,
        needs no fallback and gets ``None`` so it keeps what it has.

        ``known_latest_thread_event_id`` is the send response of a message this
        same execution just put in the thread. Reads issued after a send are
        echo-ordered, not read-your-writes, so the projection would answer with
        whatever preceded it; a caller holding the newer fact is believed.

        The thread root is the answer when the projection has nothing newer.
        That is right for an empty thread and it is also the safe reading of a
        thread whose newest message is another principal's, still waiting for
        its echo -- a reply anchored one message back still renders in the
        thread, because the ``m.thread`` relation is what places it. This
        fallback only decides what a client that ignores threads shows it under.
        """
        if thread_id is None or existing_event_id is not None or reply_to_event_id is not None:
            return None
        if known_latest_thread_event_id is not None:
            return known_latest_thread_event_id
        return await self.store.latest_visible_event_id(room_id=room_id, thread_id=thread_id) or thread_id

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
        await self.hydrator.resolve_refreshes(page.refresh_pending)
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


async def latest_agent_message_snapshot(
    reader: ConversationReader,
    *,
    room_id: str,
    thread_id: str | None,
    sender: str,
) -> AgentMessageSnapshot | None:
    """Return the newest visible message ``sender`` has in one conversation scope.

    The read never blocks, because the callers are hooks and status probes
    rather than prompt assembly: an unhydrated conversation answers from what
    the projection already holds instead of waiting on a homeserver. A message
    whose visible revision was redacted is absent rather than stale, which is
    the same rule every other read on this projection follows.

    ``thread_id=None`` means the unthreaded room conversation, not the room as
    a whole, so a threaded reply never answers a room-scope question.
    """
    page = await reader.read(
        room_id=room_id,
        thread_id=thread_id,
        limit=_LATEST_SENDER_MESSAGE_WINDOW_MESSAGES,
    )
    for message in reversed(page.messages):
        if message.sender != sender:
            continue
        return AgentMessageSnapshot(
            content=dict(message.content),
            origin_server_ts=message.revision_ts,
        )
    return None


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
    # A strict read proves nothing is pending. It does not prove the walk that
    # populated the conversation reached the beginning of it.
    truncated = await reader.hydration_was_truncated(room_id=room_id, thread_id=thread_id)
    return projected_thread_history(page, complete=not truncated)
