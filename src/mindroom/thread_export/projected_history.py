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

Prompts and export want opposite things from that hydration: a prompt wants a
bounded window, export wants the whole thread. There is still one walk and one
reducer -- a second Matrix interpreter is the thing this module exists to
prevent -- but the two callers do not share its *bounds*, and an earlier version
of this made them, which was a regression rather than a simplification. The
prompt window sizes a model's context; imposed on export it turns "export this
thread" into "refuse any thread longer than a prompt", because hydration records
`complete=False` and this module then rightly declines to write a suffix as
though it were the whole thread. Before the cutover an export paginated Matrix
directly and had no such limit.

So export walks with its own, far larger bounds. They are bounds and not
infinities on purpose: a runaway guard that never fires is not a guard, and a
thread past even these is reported as too large to write honestly rather than
silently truncated. The two callers still differ in how they react to hitting a
bound -- a prompt accepts a shorter conversation, export refuses -- which is the
part that was right all along.

Owning the bounds was not enough on its own, because the two callers share a
principal as well as a walk. Hydration runs once per membership and stops at the
first marker, so the prompt path -- which reaches every thread the bot has
answered in, long before anyone exports it -- installed a marker for its own
window and the larger bounds here were never once used. Every warm thread past
the prompt window was unexportable until a rejoin moved the epoch. The hydrator
is therefore told that this caller needs the whole conversation, and a marker
left by a walk that gave up early no longer satisfies it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from mindroom.event_journal import HydrationPolicy
from mindroom.event_journal.views import ConversationReadView, HydrationView
from mindroom.matrix.conversation_hydration import ConversationHydrator
from mindroom.matrix.conversation_reads import ConversationReader, projected_visible_messages

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config
    from mindroom.event_journal import ConversationCursor
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

# One page is a store round trip, not a homeserver one, so this trades a little
# memory for far fewer of them. It is deliberately smaller than the prompt
# window: export is the caller that reads whole threads, and a page that big
# would make the paging loop untested in practice.
EXPORT_PAGE_MESSAGES = 500

# What an export is willing to walk, as distinct from what a prompt is willing
# to read. A prompt stops at the window because a model cannot use more; an
# export stops only to keep one pathological thread from running forever. These
# are deliberately far above the prompt bounds rather than unbounded: a runaway
# guard that never fires is not a guard, and an export that hangs is worse than
# one that says the thread is too large to write honestly.
EXPORT_WINDOW_MESSAGES = 1_000_000
EXPORT_MAX_FETCHED_EVENTS = 2_000_000
EXPORT_MAX_MESSAGES_REQUESTS = 20_000


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


class ExportProjectionView(ConversationReadView, HydrationView, SupportsConversationCompleteness, Protocol):
    """Everything export reads from one principal's projection, and nothing else.

    Export is the one caller that needs all three slices at once -- it reads
    pages, it triggers the hydration behind the first of them, and it asks
    whether that hydration finished -- so this is where the union is written
    down. Naming it keeps the factory from taking the whole ``PrincipalStore``
    for want of a type: an export that reached for ``enqueue_delivery`` or
    ``admit`` would fail the type checker rather than typecheck and be caught,
    if at all, in review.
    """


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
    store: ExportProjectionView,
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
                # An export asks a different question than a prompt, so it
                # cannot inherit the prompt's bounds. The message window exists
                # to size a model's context; applied here it silently converts
                # "export this thread" into "refuse any thread longer than a
                # prompt", because hydration records `complete=False` and this
                # module then correctly declines to write a suffix as if it were
                # the whole thread. Before this cutover an export paginated
                # Matrix directly and had no such limit.
                # The three ceilings and the name they go by. The name is what
                # a walk writes into the marker, so these must be changed
                # together: a policy is the whole set, and recording one of
                # its numbers instead could not tell two policies apart that
                # differ only on one of the others.
                policy=HydrationPolicy.EXPORT,
                prompt_window_messages=EXPORT_WINDOW_MESSAGES,
                max_fetched_events=EXPORT_MAX_FETCHED_EVENTS,
                max_requests=EXPORT_MAX_MESSAGES_REQUESTS,
                # Larger bounds alone were not enough, because hydration runs
                # once per membership and this reader shares the running bot's
                # principal. The prompt path reaches every thread the bot has
                # answered in first, and its marker satisfied the short-circuit,
                # so the bounds above were dead code for exactly the warm
                # threads an export is for. A marker that vouches for a walk
                # which gave up early does not vouch for a whole thread.
                require_complete=True,
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

    Every page is a strict read: the conversation is built from the server if
    no walk has yet read it to its end under this membership, and any message
    owing a point refetch is repaired before the page is returned.

    Completeness is asked once, after that first read, and it is a different
    question from freshness. Hydration is bounded, so a thread longer than a
    walk's allowance leaves a perfectly warm marker over a partial
    conversation, and nothing in a page distinguishes "this is all of it" from
    "this is the end of it". A prompt is right to accept the suffix. An export
    is not: a file that says ``message_count`` and means "the last few hundred"
    is worse than a failure, so the thread fails and the pass records it.

    Reaching here means the deeper walk was already asked for and still did not
    reach the start, so the two cases left are both permanent: a thread past
    even the export bounds, and a room whose history a skipped sync gap lost
    for good. Neither is something a retry fixes, and neither is re-walked.

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
    "EXPORT_MAX_FETCHED_EVENTS",
    "EXPORT_MAX_MESSAGES_REQUESTS",
    "EXPORT_PAGE_MESSAGES",
    "EXPORT_WINDOW_MESSAGES",
    "ExportProjectionView",
    "ProjectedThreadReader",
    "SupportsConversationCompleteness",
    "ThreadExportIncompleteError",
    "export_conversation_reader",
    "fetch_projected_thread_history",
]
