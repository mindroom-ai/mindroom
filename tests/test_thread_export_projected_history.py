"""Thread export's read of the journal projection.

These run against a real store on both backends and a fake homeserver, because
the properties that matter here are exactly the ones a mock would assert away:
how many Matrix calls a warm thread costs, whether a thread longer than one
page comes back in order with its root once, and whether edits, redactions and
sidecars reduce the way the prompt path reduces them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import nio
import pytest

from mindroom.config.main import Config
from mindroom.event_journal import DepartureSource, EventClass, EventKind
from mindroom.matrix.conversation_hydration import (
    _MAX_FETCHED_EVENTS,
    _MAX_MESSAGES_REQUESTS,
    HYDRATED_PROMPT_WINDOW_MESSAGES,
    ConversationHydrator,
)
from mindroom.matrix.conversation_reads import ConversationReader
from mindroom.matrix.journal_ingress import inbound_event, projected_event
from mindroom.thread_export.projected_history import (
    ProjectedThreadReader,
    ThreadExportIncompleteError,
    export_conversation_reader,
    fetch_projected_thread_history,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from mindroom.event_journal import EventJournalStore, PrincipalStore
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

pytestmark = pytest.mark.asyncio

ROOM = "!room:example.org"
ROOT = "$root:example.org"
ALICE = "@alice:example.org"
ROUTER = "@mindroom_router:example.org"
PRINCIPAL = f"router@{ROUTER}"
SIDECAR_URL = "mxc://example.org/whole-message"
SIDECAR_TEXT = "the whole long message, all of it, well past any preview"
THREAD_SUMMARY_KEY = "io.mindroom.thread_summary"


def raw(
    event_id: str,
    body: str,
    *,
    ts: int,
    sender: str = ALICE,
    thread_id: str | None = None,
    replaces: str | None = None,
    extra_content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one raw Matrix room message, threaded or replacing as asked."""
    content: dict[str, Any] = {"msgtype": "m.text", "body": body}
    if replaces is not None:
        content["body"] = f"* {body}"
        content["m.new_content"] = {"msgtype": "m.text", "body": body}
        content["m.relates_to"] = {"rel_type": "m.replace", "event_id": replaces}
    elif thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    content.update(extra_content or {})
    return {
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": ts,
        "room_id": ROOM,
        "type": "m.room.message",
        "content": content,
    }


def redaction(event_id: str, redacts: str, *, ts: int, sender: str = ALICE) -> dict[str, Any]:
    """Return one raw Matrix redaction of another event."""
    return {
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": ts,
        "room_id": ROOM,
        "type": "m.room.redaction",
        "redacts": redacts,
        "content": {"reason": "oops"},
    }


def sidecar_content() -> dict[str, Any]:
    """Return the content keys that make a body a preview of an attached file."""
    return {
        "url": SIDECAR_URL,
        "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
    }


def parse(source: dict[str, Any]) -> nio.Event:
    """Return the parsed nio event for one raw source."""
    event = nio.Event.parse_event(source)
    assert isinstance(event, nio.Event)
    return event


@dataclass
class FakeHomeserver:
    """A homeserver that answers a thread, and counts every history call.

    The counters are the point. "This export was warm" is a claim about what
    did *not* happen, and nothing except a call count can observe it.
    """

    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    relations: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    sidecars: dict[str, str] = field(default_factory=dict)
    get_event_calls: int = 0
    relation_calls: int = 0
    messages_calls: int = 0
    download_calls: int = 0

    @property
    def history_calls(self) -> int:
        """Return every Matrix call that could have carried a thread body."""
        return self.get_event_calls + self.relation_calls + self.messages_calls + self.download_calls

    def reset_counts(self) -> None:
        """Forget what earlier passes cost."""
        self.get_event_calls = 0
        self.relation_calls = 0
        self.messages_calls = 0
        self.download_calls = 0

    async def room_get_event(self, room_id: str, event_id: str) -> nio.RoomGetEventResponse | nio.RoomGetEventError:
        """Return one stored event."""
        del room_id
        self.get_event_calls += 1
        source = self.events.get(event_id)
        if source is None:
            return nio.RoomGetEventError("M_NOT_FOUND")
        # nio builds this response by assignment rather than construction.
        response = nio.RoomGetEventResponse()
        response.event = parse(source)
        return response

    async def room_get_event_relations(
        self,
        *,
        room_id: str,
        event_id: str,
        direction: nio.MessageDirection = nio.MessageDirection.back,
        recurse: bool = False,
        minimum_recursion_depth: int | None = None,
    ) -> AsyncIterator[nio.Event]:
        """Yield one event's relations in the order the caller asked for.

        Newest first is what the bounded walk rests on, so the fake honours the
        direction rather than accepting and ignoring it.
        """
        del room_id, recurse, minimum_recursion_depth
        self.relation_calls += 1
        sources = sorted(
            self.relations.get(event_id, []),
            key=lambda source: (source["origin_server_ts"], source["event_id"]),
            reverse=direction is nio.MessageDirection.back,
        )
        for source in sources:
            yield parse(source)

    async def room_messages(self, *args: object, **kwargs: object) -> nio.RoomMessagesResponse:
        """Return an exhausted room, and record that someone scanned it."""
        del args, kwargs
        self.messages_calls += 1
        return nio.RoomMessagesResponse(ROOM, [], "start", None)

    async def download(self, mxc: str) -> nio.DownloadResponse | nio.DownloadError:
        """Return one stored long-text sidecar."""
        self.download_calls += 1
        payload = self.sidecars.get(mxc)
        if payload is None:
            return nio.DownloadError("M_NOT_FOUND")
        return nio.DownloadResponse(payload.encode(), "application/json", None)


@pytest.fixture
def router(journal_store: EventJournalStore) -> PrincipalStore:
    """Return the projection view of the account an export logs in as."""
    return journal_store.principal(PRINCIPAL)


def reader_for(store: PrincipalStore, homeserver: FakeHomeserver) -> ProjectedThreadReader:
    """Return the projection view one export login would build for this homeserver."""
    return export_conversation_reader(
        client=homeserver,  # type: ignore[arg-type]
        config=Config(),
        store=store,
        self_sender=ROUTER,
    )


@dataclass(frozen=True, slots=True)
class _BotRuntime:
    """The client-and-config view a running bot hands its hydrator."""

    client: FakeHomeserver
    config: Config


def prompt_reader_for(
    store: PrincipalStore,
    homeserver: FakeHomeserver,
    *,
    window_messages: int,
) -> ConversationReader:
    """Return the conversation reader a running bot builds for the same account.

    Wired the way ``bot.py`` wires it: the prompt path's own hydration bounds
    over *the same journal principal* an export of that account reads. That
    sharing is deliberate -- it is what makes a warm export cost no Matrix
    calls -- so a test about the two callers meeting has to reproduce it rather
    than give each its own store.
    """
    return ConversationReader(
        store=store,
        hydrator=ConversationHydrator(
            store=store,
            runtime=_BotRuntime(client=homeserver, config=Config()),  # type: ignore[arg-type]
            self_sender=ROUTER,
            prompt_window_messages=window_messages,
        ),
    )


async def admit_live(store: PrincipalStore, sources: Iterable[dict[str, Any]]) -> None:
    """Admit raw events the way a running bot's sync loop would."""
    for source in sources:
        event = parse(source)
        kind = EventKind.REDACTION if isinstance(event, nio.RedactionEvent) else EventKind.MESSAGE
        await store.admit(
            inbound_event(ROOM, event, kind, EventClass.ACTIONABLE),
            projected_event(ROOM, event, kind, self_sender=ROUTER),
        )


def serve_thread(homeserver: FakeHomeserver, root: dict[str, Any], relations: list[dict[str, Any]]) -> None:
    """Teach one fake homeserver a whole thread."""
    homeserver.events[root["event_id"]] = root
    for source in relations:
        homeserver.events[source["event_id"]] = source
    homeserver.relations[root["event_id"]] = relations


async def export(projection: ProjectedThreadReader, **kwargs: int) -> list[ResolvedVisibleMessage]:
    """Read one thread the way the export writer does."""
    return await fetch_projected_thread_history(projection, room_id=ROOM, thread_id=ROOT, **kwargs)


def bodies(messages: list[ResolvedVisibleMessage]) -> list[str]:
    """Return each exported message's current body."""
    return [message.body for message in messages]


def event_ids(messages: list[ResolvedVisibleMessage]) -> list[str]:
    """Return each exported message's logical event ID."""
    return [message.event_id for message in messages]


async def test_a_cold_thread_hydrates_once_and_then_costs_no_history_calls(
    router: PrincipalStore,
) -> None:
    """The whole point: a projection kept warm by sync makes export a local read.

    The cold pass is asserted too, and not only as setup. Without it the zero
    on the warm pass would prove nothing: a counter that never moves is
    indistinguishable from a counter nothing increments.
    """
    homeserver = FakeHomeserver()
    serve_thread(
        homeserver,
        raw(ROOT, "root", ts=100),
        [
            raw("$a:example.org", "first", ts=200, thread_id=ROOT),
            raw("$b:example.org", "second", ts=300, thread_id=ROOT),
        ],
    )
    reader = reader_for(router, homeserver)

    cold = await export(reader)

    assert bodies(cold) == ["root", "first", "second"]
    # One fetch of the root and one walk of its relation tree: no `/messages`
    # scan of the room, and no per-message refetch.
    assert (homeserver.get_event_calls, homeserver.relation_calls) == (1, 1)
    assert (homeserver.messages_calls, homeserver.download_calls) == (0, 0)

    homeserver.reset_counts()
    warm = await export(reader)

    assert bodies(warm) == ["root", "first", "second"]
    assert homeserver.history_calls == 0


async def test_a_warm_thread_reads_locally_from_a_second_export_login(
    journal_store: EventJournalStore,
    router: PrincipalStore,
) -> None:
    """A later pass is a fresh reader over the same principal's rows, and still free."""
    homeserver = FakeHomeserver()
    serve_thread(
        homeserver,
        raw(ROOT, "root", ts=100),
        [raw("$a:example.org", "first", ts=200, thread_id=ROOT)],
    )
    await export(reader_for(router, homeserver))
    homeserver.reset_counts()

    messages = await export(reader_for(journal_store.principal(PRINCIPAL), homeserver))

    assert bodies(messages) == ["root", "first"]
    assert homeserver.history_calls == 0


async def test_live_sync_rows_alone_do_not_count_as_a_built_conversation(
    router: PrincipalStore,
) -> None:
    """Rows without a hydration marker still owe one walk, and only one.

    A bot that watched a thread from its first message already holds every row,
    but nothing has established that it holds *all* of them. Export asks once,
    which is what makes every later pass free.
    """
    homeserver = FakeHomeserver()
    root = raw(ROOT, "root", ts=100)
    reply = raw("$a:example.org", "first", ts=200, thread_id=ROOT)
    serve_thread(homeserver, root, [reply])
    await admit_live(router, [root, reply])
    reader = reader_for(router, homeserver)

    await export(reader)

    assert (homeserver.get_event_calls, homeserver.relation_calls) == (1, 1)

    homeserver.reset_counts()
    await export(reader)

    assert homeserver.history_calls == 0


async def test_a_thread_longer_than_one_page_exports_in_order_with_one_root(
    router: PrincipalStore,
) -> None:
    """Paging runs backwards and the result does not; the root belongs to one page."""
    homeserver = FakeHomeserver()
    serve_thread(
        homeserver,
        raw(ROOT, "message-00", ts=1_000),
        [
            raw(f"$reply-{index:02d}:example.org", f"message-{index:02d}", ts=1_000 + index, thread_id=ROOT)
            for index in range(1, 12)
        ],
    )
    reader = reader_for(router, homeserver)

    messages = await export(reader, page_messages=3)

    assert bodies(messages) == [f"message-{index:02d}" for index in range(12)]
    assert event_ids(messages).count(ROOT) == 1
    assert event_ids(messages)[0] == ROOT
    # Paging must not re-hydrate: the walk happens once for the conversation,
    # not once per page.
    assert (homeserver.get_event_calls, homeserver.relation_calls) == (1, 1)


async def test_an_edited_message_exports_its_current_revision(router: PrincipalStore) -> None:
    """The newest replacement wins, and the original keeps its place in the thread."""
    homeserver = FakeHomeserver()
    serve_thread(
        homeserver,
        raw(ROOT, "root", ts=100),
        [
            raw("$b:example.org", "v1", ts=200, thread_id=ROOT),
            raw("$edit-1:example.org", "v2", ts=400, replaces="$b:example.org"),
            raw("$edit-2:example.org", "v3", ts=500, replaces="$b:example.org"),
        ],
    )

    messages = await export(reader_for(router, homeserver))

    assert event_ids(messages) == [ROOT, "$b:example.org"]
    edited = messages[1]
    assert edited.body == "v3"
    assert edited.latest_event_id == "$edit-2:example.org"
    # The ordering key stays the original's; the edit's own time is separate.
    assert edited.timestamp == 200
    assert edited.edited_timestamp == 500


async def test_a_replacement_from_another_sender_is_not_an_edit(router: PrincipalStore) -> None:
    """Author identity decides what an edit is, and export inherits that rule."""
    homeserver = FakeHomeserver()
    serve_thread(
        homeserver,
        raw(ROOT, "root", ts=100),
        [
            raw("$b:example.org", "second", ts=200, thread_id=ROOT),
            raw("$edit:example.org", "hijacked", ts=400, sender="@mallory:example.org", replaces="$b:example.org"),
        ],
    )

    messages = await export(reader_for(router, homeserver))

    assert event_ids(messages) == [ROOT, "$b:example.org"]
    assert messages[1].body == "second"
    assert messages[1].latest_event_id == "$b:example.org"
    assert messages[1].edited_timestamp is None


async def test_a_redacted_reply_leaves_the_export(router: PrincipalStore) -> None:
    """A message the server stripped is absent rather than exported empty."""
    homeserver = FakeHomeserver()
    root = raw(ROOT, "root", ts=100)
    doomed = raw("$b:example.org", "second", ts=200, thread_id=ROOT)
    kept = raw("$c:example.org", "third", ts=300, thread_id=ROOT)
    serve_thread(homeserver, root, [doomed, kept])
    await admit_live(router, [root, doomed, kept, redaction("$r:example.org", "$b:example.org", ts=400)])

    messages = await export(reader_for(router, homeserver))

    assert event_ids(messages) == [ROOT, "$c:example.org"]
    assert bodies(messages) == ["root", "third"]


async def test_redacting_the_visible_edit_refetches_rather_than_exporting_a_hole(
    router: PrincipalStore,
) -> None:
    """A deleted revision is a debt the strict read settles before the page returns."""
    homeserver = FakeHomeserver()
    root = raw(ROOT, "root", ts=100)
    original = raw("$b:example.org", "v1", ts=200, thread_id=ROOT)
    edit = raw("$edit:example.org", "v2", ts=300, replaces="$b:example.org")
    serve_thread(homeserver, root, [original, edit])
    await admit_live(router, [root, original, edit])
    # The homeserver no longer serves the redacted edit, so a refetch of the
    # original reduces back to the text it was first sent with.
    homeserver.relations[ROOT] = [original]
    homeserver.relations["$b:example.org"] = []
    await admit_live(router, [redaction("$r:example.org", "$edit:example.org", ts=400)])

    messages = await export(reader_for(router, homeserver))

    assert event_ids(messages) == [ROOT, "$b:example.org"]
    assert messages[1].body == "v1"
    assert messages[1].latest_event_id == "$b:example.org"


async def test_a_long_text_sidecar_is_fetched_once_and_then_stays_local(
    router: PrincipalStore,
) -> None:
    """Export writes the message, not its preview, and does not refetch it every pass."""
    homeserver = FakeHomeserver(sidecars={SIDECAR_URL: json.dumps({"msgtype": "m.text", "body": SIDECAR_TEXT})})
    serve_thread(
        homeserver,
        raw(ROOT, "root", ts=100),
        [
            raw(
                "$b:example.org",
                "the whole long mes…",
                ts=200,
                thread_id=ROOT,
                extra_content=sidecar_content(),
            ),
        ],
    )
    reader = reader_for(router, homeserver)

    cold = await export(reader)

    assert bodies(cold) == ["root", SIDECAR_TEXT]
    assert homeserver.download_calls == 1

    homeserver.reset_counts()
    warm = await export(reader)

    assert bodies(warm) == ["root", SIDECAR_TEXT]
    assert homeserver.history_calls == 0


async def test_an_unreadable_sidecar_fails_the_thread_instead_of_exporting_the_preview(
    router: PrincipalStore,
) -> None:
    """A file that cannot be read leaves the debt owed, and the caller is told."""
    homeserver = FakeHomeserver()
    serve_thread(
        homeserver,
        raw(ROOT, "root", ts=100),
        [
            raw(
                "$b:example.org",
                "the whole long mes…",
                ts=200,
                thread_id=ROOT,
                extra_content=sidecar_content(),
            ),
        ],
    )

    with pytest.raises(RuntimeError, match="awaiting a server refetch"):
        await export(reader_for(router, homeserver))


async def test_a_thread_summary_notice_keeps_its_metadata_through_the_export(
    router: PrincipalStore,
) -> None:
    """The summary lives in message content, and the projection round-trips it whole."""
    homeserver = FakeHomeserver()
    serve_thread(
        homeserver,
        raw(ROOT, "root", ts=100),
        [
            raw(
                "$summary:example.org",
                "Deploy pipeline fix",
                ts=200,
                sender=ROUTER,
                thread_id=ROOT,
                extra_content={
                    "msgtype": "m.notice",
                    THREAD_SUMMARY_KEY: {"version": 1, "summary": "Deploy pipeline fix"},
                },
            ),
        ],
    )

    messages = await export(reader_for(router, homeserver))

    assert messages[1].content[THREAD_SUMMARY_KEY] == {"version": 1, "summary": "Deploy pipeline fix"}


async def test_rejoining_the_room_forces_one_fresh_hydration(router: PrincipalStore) -> None:
    """Hydration is per membership, so a rejoin invalidates what the last one built."""
    homeserver = FakeHomeserver()
    serve_thread(
        homeserver,
        raw(ROOT, "root", ts=100),
        [raw("$a:example.org", "first", ts=200, thread_id=ROOT)],
    )
    reader = reader_for(router, homeserver)
    await export(reader)
    homeserver.reset_counts()

    await router.fence_departure(ROOM, source=DepartureSource.LOCAL)
    homeserver.relations[ROOT] = [
        raw("$a:example.org", "first", ts=200, thread_id=ROOT),
        raw("$b:example.org", "second", ts=300, thread_id=ROOT),
    ]
    messages = await export(reader)

    assert (homeserver.get_event_calls, homeserver.relation_calls) == (1, 1)
    assert bodies(messages) == ["root", "first", "second"]


async def test_a_thread_whose_root_the_server_lost_fails_rather_than_exporting_replies(
    router: PrincipalStore,
) -> None:
    """A thread export missing its root is not a shorter thread; it is a wrong one."""
    homeserver = FakeHomeserver()
    homeserver.relations[ROOT] = [raw("$a:example.org", "first", ts=200, thread_id=ROOT)]

    with pytest.raises(RuntimeError, match="Could not fetch thread root"):
        await export(reader_for(router, homeserver))


async def test_one_principals_warm_projection_does_not_serve_another(
    journal_store: EventJournalStore,
    router: PrincipalStore,
) -> None:
    """The projection is per principal, which is why export must bind to an active bot."""
    homeserver = FakeHomeserver()
    serve_thread(
        homeserver,
        raw(ROOT, "root", ts=100),
        [raw("$a:example.org", "first", ts=200, thread_id=ROOT)],
    )
    await export(reader_for(router, homeserver))
    homeserver.reset_counts()

    other = journal_store.principal("general@@mindroom_general:example.org")
    await export(reader_for(other, homeserver))

    assert (homeserver.get_event_calls, homeserver.relation_calls) == (1, 1)


async def test_a_thread_hydration_could_not_finish_fails_rather_than_exporting_a_suffix(
    router: PrincipalStore,
) -> None:
    """The bounded-walk reconciliation, from export's side.

    Hydration has one bound and prompts accept it: a shorter conversation is a
    shorter prompt. Export cannot, because the file it writes carries a
    ``message_count`` and reads as the whole thread. Nothing in a page says
    which of the two it is -- a truncated walk leaves an entirely warm marker --
    so the completeness marker is what decides, and this one thread fails.
    """
    homeserver = FakeHomeserver()
    serve_thread(
        homeserver,
        raw(ROOT, "root", ts=100),
        [raw(f"$reply-{index}:example.org", f"message-{index}", ts=200 + index, thread_id=ROOT) for index in range(5)],
    )
    projection = reader_for(router, homeserver)
    # The bound the prompt path carries, set low enough that this thread trips it.
    projection.reader.hydrator.prompt_window_messages = 2

    with pytest.raises(ThreadExportIncompleteError, match="rather than to its start"):
        await export(projection)


async def test_a_thread_that_fits_the_window_is_complete_and_exports(router: PrincipalStore) -> None:
    """The mirror of the bound: reaching the end of the relations is what completeness means.

    Without this the failure above would also pass if export simply refused
    every thread, which is the mistake a completeness gate is most likely to
    make.
    """
    homeserver = FakeHomeserver()
    serve_thread(
        homeserver,
        raw(ROOT, "root", ts=100),
        [raw(f"$reply-{index}:example.org", f"message-{index}", ts=200 + index, thread_id=ROOT) for index in range(5)],
    )
    projection = reader_for(router, homeserver)
    projection.reader.hydrator.prompt_window_messages = 50

    messages = await export(projection)

    assert bodies(messages) == ["root", "message-0", "message-1", "message-2", "message-3", "message-4"]


async def test_a_thread_the_prompt_path_already_hydrated_still_exports_whole(
    router: PrincipalStore,
) -> None:
    """Export's larger bounds must survive the prompt path getting there first.

    Export reads the running bot's own principal, so by the time anyone exports
    a thread the bot has been answering in, a hydration marker already exists --
    one left by a walk bounded for a *prompt*. A marker that vouches only for a
    suffix must not satisfy a caller that needs the whole thread, or export's
    larger bounds are dead code for every warm thread and the only threads that
    export are the ones nobody has used.

    The second pass is asserted too. A deeper walk that did not record its
    better coverage would leave every later export paying for the same walk
    again.
    """
    homeserver = FakeHomeserver()
    serve_thread(
        homeserver,
        raw(ROOT, "message-00", ts=1_000),
        [
            raw(f"$reply-{index:02d}:example.org", f"message-{index:02d}", ts=1_000 + index, thread_id=ROOT)
            for index in range(1, 6)
        ],
    )
    # The running bot reads the thread first. Its walk stops at the prompt
    # window and leaves a perfectly warm marker over a partial conversation.
    await prompt_reader_for(router, homeserver, window_messages=2).read_strict(
        room_id=ROOM,
        thread_id=ROOT,
        limit=50,
    )
    assert await router.conversation_hydration_was_truncated(room_id=ROOM, thread_id=ROOT)

    messages = await export(reader_for(router, homeserver))

    assert bodies(messages) == [f"message-{index:02d}" for index in range(6)]

    homeserver.reset_counts()
    again = await export(reader_for(router, homeserver))

    assert bodies(again) == [f"message-{index:02d}" for index in range(6)]
    assert homeserver.history_calls == 0


async def test_export_does_not_inherit_the_prompt_window(router: PrincipalStore) -> None:
    """An export must not refuse a thread merely for being longer than a prompt.

    The prompt window sizes a model's context. Imposed on export it silently
    turns "export this thread" into "refuse any thread longer than a prompt",
    because hydration records ``complete=False`` past the window and export then
    rightly declines to write a suffix as though it were the whole thread.
    Before this cutover an export paginated Matrix directly and had no such
    limit, so inheriting it was a regression rather than a simplification.

    The bounds are still finite: a runaway guard that never fires is not a
    guard. What matters is that they are the export's own.
    """
    hydrator = reader_for(router, FakeHomeserver()).reader.hydrator

    assert hydrator.prompt_window_messages > HYDRATED_PROMPT_WINDOW_MESSAGES
    assert hydrator.max_fetched_events > _MAX_FETCHED_EVENTS
    assert hydrator.max_requests > _MAX_MESSAGES_REQUESTS
