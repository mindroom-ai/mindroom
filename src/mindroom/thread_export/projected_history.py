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

Prompts and export want opposite things from that hydration -- a prompt wants a
bounded window, export wants the whole thread -- and this does not resolve that
by asking for a second, unbounded walk. There is one walk with one bound, and
the two callers differ only in how they react to hitting it: a prompt accepts a
shorter conversation, and export refuses to write a file that claims to be a
whole thread and is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

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


class ThreadExportIncompleteError(RuntimeError):
    """A thread's hydration stopped at a ceiling rather than at its end."""


class SupportsConversationCompleteness(Protocol):
    """Asking whether a conversation's one hydration walk reached its end.

    The narrow slice export needs and no other reader does, declared here
    rather than widened into the shared read protocol: a prompt has no use for
    it, and a protocol every collaborator satisfies is how a boundary stops
    being one.
    """

    async def conversation_is_complete(self, *, room_id: str, thread_id: str | None) -> bool:
        """Return whether the walk that hydrated this conversation ran to its end."""
        ...


@dataclass(frozen=True, slots=True)
class _ExportClientRuntime:
    """The client-and-config view hydration asks for, for one export login.

    Hydration holds a runtime rather than a client because a bot builds its
    collaborators before it logs in. An export has its client first, so this is
    that indirection collapsed to the two values it actually reads.
    """

    client: nio.AsyncClient
    config: Config


@dataclass(frozen=True, slots=True)
class ProjectedThreadReader:
    """One export login's view of the projection.

    Both halves are of the same principal, which is why they are built together
    rather than passed around separately: a completeness answer about one bot's
    conversation says nothing about another's.
    """

    reader: ConversationReader
    completeness: SupportsConversationCompleteness


def export_conversation_reader(
    *,
    client: nio.AsyncClient,
    config: Config,
    store: PrincipalStore,
    self_sender: str,
) -> ProjectedThreadReader:
    """Return the projection view one export login uses for thread bodies.

    ``self_sender`` is the Matrix user ID this export logged in as, and must be
    the same account whose principal ``store`` is bound to: hydration drops that
    sender's in-flight streaming edits, exactly as live admission did, so a
    refetched conversation reduces to what the live projection holds.
    """
    return ProjectedThreadReader(
        reader=ConversationReader(
            store=store,
            hydrator=ConversationHydrator(
                store=store,
                runtime=_ExportClientRuntime(client=client, config=config),
                self_sender=self_sender,
            ),
        ),
        completeness=store,
    )


async def fetch_projected_thread_history(
    projection: ProjectedThreadReader,
    *,
    room_id: str,
    thread_id: str,
    page_messages: int = EXPORT_PAGE_MESSAGES,
) -> list[ResolvedVisibleMessage]:
    """Return one thread's complete current history, oldest first.

    Every page is a strict read: the conversation is hydrated once if it has
    never been built under this membership, and any message owing a point
    refetch is repaired before the page is returned.

    Completeness is asked once, after that first read, and it is a different
    question from freshness. Hydration is bounded, so a thread longer than the
    window leaves a perfectly warm marker over a partial conversation, and
    nothing in a page distinguishes "this is all of it" from "this is the end
    of it". A prompt is right to accept the suffix. An export is not: a file
    that says ``message_count`` and means "the last few hundred" is worse than
    a failure, so the thread fails and the pass records it. There is no deeper
    walk to ask for -- hydration runs once per membership -- which makes this
    terminal for that membership rather than something a retry fixes.

    After that, paging runs backwards, because that is the direction the
    projection is indexed in, and each page is prepended so the result stays in
    the thread's own order across page boundaries. The cursor is strictly
    decreasing by construction, so the loop cannot revisit a page or fail to
    terminate.

    The root is a member of the room conversation rather than of its own
    thread, so the projection merges it into whichever page its timestamp falls
    in — once, because the cursor that page hands back excludes it from the
    next.
    """
    page = await projection.reader.read_strict(room_id=room_id, thread_id=thread_id, limit=page_messages)
    if not await projection.completeness.conversation_is_complete(room_id=room_id, thread_id=thread_id):
        msg = (
            f"Thread {thread_id} in {room_id} was hydrated up to a ceiling rather than to its "
            f"start, so exporting it would write a suffix as if it were the whole thread"
        )
        raise ThreadExportIncompleteError(msg)

    messages = projected_visible_messages(page)
    cursor: ConversationCursor | None = page.next_cursor
    while cursor is not None:
        page = await projection.reader.read_strict(
            room_id=room_id,
            thread_id=thread_id,
            limit=page_messages,
            before=cursor,
        )
        messages[:0] = projected_visible_messages(page)
        cursor = page.next_cursor
    return messages


__all__ = [
    "EXPORT_PAGE_MESSAGES",
    "ProjectedThreadReader",
    "SupportsConversationCompleteness",
    "ThreadExportIncompleteError",
    "export_conversation_reader",
    "fetch_projected_thread_history",
]
