"""One exported thread's body, read from the journal's visible-message projection.

Export used to own a second Matrix event reducer: its own backward ``/messages``
walk, its own edit and redaction rules, its own sidecar resolution, its own
thread-membership resolution. That is the projection's job, and having it twice
meant an exported thread and the history a model was shown could disagree about
which edit won or what a redaction left behind.

So export reads what prompts read, through the same ``ConversationReader``. The
only thing that differs is how much: a prompt asks for its window and stops,
export pages until the conversation runs out. Nothing here interprets a Matrix
event.

The reader is bound to an *active* bot's principal, because a projection is
only warm for a principal something is syncing. A warm thread therefore costs
zero Matrix history calls; a thread nobody has read yet costs exactly one
hydration, and never again under the same membership.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.matrix.conversation_hydration import ConversationHydrator
from mindroom.matrix.conversation_reads import ConversationReader, projected_visible_messages

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config
    from mindroom.event_journal import ConversationCursor, PrincipalStore
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

# One page is a store round trip, not a homeserver one, so this trades a little
# memory for far fewer of them. It is deliberately smaller than the prompt
# window: export is the caller that reads whole threads, and a page that big
# would make the paging loop untested in practice.
EXPORT_PAGE_MESSAGES = 500


@dataclass(frozen=True, slots=True)
class _ExportClientRuntime:
    """The client-and-config view hydration asks for, for one export login.

    Hydration holds a runtime rather than a client because a bot builds its
    collaborators before it logs in. An export has its client first, so this is
    that indirection collapsed to the two values it actually reads.
    """

    client: nio.AsyncClient
    config: Config


def export_conversation_reader(
    *,
    client: nio.AsyncClient,
    config: Config,
    store: PrincipalStore,
    self_sender: str,
) -> ConversationReader:
    """Return the reader one export login uses for thread bodies.

    ``self_sender`` is the Matrix user ID this export logged in as, and must be
    the same account whose principal ``store`` is bound to: hydration drops that
    sender's in-flight streaming edits, exactly as live admission did, so a
    refetched conversation reduces to what the live projection holds.
    """
    return ConversationReader(
        store=store,
        hydrator=ConversationHydrator(
            store=store,
            runtime=_ExportClientRuntime(client=client, config=config),
            self_sender=self_sender,
        ),
    )


async def fetch_projected_thread_history(
    reader: ConversationReader,
    *,
    room_id: str,
    thread_id: str,
    page_messages: int = EXPORT_PAGE_MESSAGES,
) -> list[ResolvedVisibleMessage]:
    """Return one thread's complete current history, oldest first.

    Paging runs backwards, because that is the direction the projection is
    indexed in, and each page is prepended so the result stays in the thread's
    own order across page boundaries. The cursor is strictly decreasing by
    construction, so the loop cannot revisit a page or fail to terminate.

    Every page is a strict read: the conversation is hydrated once if it has
    never been built under this membership, and any message owing a point
    refetch is repaired before the page is returned. A page that still cannot
    be completed raises, which fails this one thread rather than writing a file
    that looks whole and is not.

    The root is a member of the room conversation rather than of its own
    thread, so the projection merges it into whichever page its timestamp falls
    in — once, because the cursor that page hands back excludes it from the
    next.
    """
    messages: list[ResolvedVisibleMessage] = []
    cursor: ConversationCursor | None = None
    while True:
        page = await reader.read_strict(
            room_id=room_id,
            thread_id=thread_id,
            limit=page_messages,
            before=cursor,
        )
        messages[:0] = projected_visible_messages(page)
        if page.next_cursor is None:
            return messages
        cursor = page.next_cursor


__all__ = [
    "EXPORT_PAGE_MESSAGES",
    "export_conversation_reader",
    "fetch_projected_thread_history",
]
