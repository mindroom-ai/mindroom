"""Hydration, point refetch, and the strict/non-strict read split."""

from __future__ import annotations

import asyncio
import itertools
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import nio
import pytest

from mindroom.constants import (
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_INTERRUPTED,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
)
from mindroom.event_journal import EventClass, EventKind, HydrationPolicy, ProjectedEvent
from mindroom.matrix.agent_message_snapshot import AgentMessageSnapshot
from mindroom.matrix.client_delivery import build_edit_event_content
from mindroom.matrix.conversation_hydration import (
    _MESSAGES_PAGE_LIMIT,
    HYDRATED_PROMPT_WINDOW_MESSAGES,
    ConversationHydrator,
    _HydrationError,
    _projected_from_event,
    _reduce_current_revision,
)
from mindroom.matrix.conversation_reads import (
    _LATEST_SENDER_MESSAGE_WINDOW_MESSAGES,
    ConversationReader,
    _StaleConversationError,
    latest_agent_message_snapshot,
    projected_thread_history,
)
from mindroom.matrix.journal_ingress import inbound_event, projected_event

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Iterator

    from mindroom.event_journal import EventJournalStore, PrincipalStore, RefreshRequest

pytestmark = pytest.mark.asyncio

ROOM = "!room:example.org"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"
BOT = "@mindroom_general:example.org"


@pytest.fixture
def alice(journal_store: EventJournalStore) -> PrincipalStore:
    """Return one bound principal view."""
    return journal_store.principal("agent@alice")


def raw(
    event_id: str,
    body: str,
    *,
    sender: str = ALICE,
    ts: int = 1_000,
    thread_id: str | None = None,
    replaces: str | None = None,
    redacted: bool = False,
) -> dict[str, Any]:
    """Return one raw Matrix message event."""
    content: dict[str, Any] = {"msgtype": "m.text", "body": body}
    if replaces is not None:
        content["m.new_content"] = {"msgtype": "m.text", "body": body}
        content["m.relates_to"] = {"rel_type": "m.replace", "event_id": replaces}
    elif thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    source: dict[str, Any] = {
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": ts,
        "type": "m.room.message",
        "content": {} if redacted else content,
    }
    if redacted:
        source["unsigned"] = {
            "redacted_because": {"type": "m.room.redaction", "sender": sender, "content": {}},
        }
    return source


def redaction(event_id: str, redacts: str, *, ts: int = 1_000, sender: str = ALICE) -> dict[str, Any]:
    """Return one raw Matrix redaction of another event."""
    return {
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": ts,
        "type": "m.room.redaction",
        "redacts": redacts,
        "content": {},
    }


def encrypted(event_id: str, *, sender: str = ALICE, ts: int = 1_000) -> dict[str, Any]:
    """Return one raw Matrix event the way it sits on the wire in an encrypted room.

    nio parses this into a ``MegolmEvent``, whose ``source`` type is
    ``m.room.encrypted`` rather than ``m.room.message``, so it projects to
    nothing until something decrypts it. That is what every relation in an
    encrypted room looks like to a hydrator, because nio's ``receive_response``
    has no branch for a relations response and so never decrypts one.
    """
    return {
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": ts,
        "type": "m.room.encrypted",
        "content": {
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": f"ciphertext-of-{event_id}",
            "sender_key": "sender-key",
            "session_id": "session",
            "device_id": "DEVICE",
        },
    }


def parse(source: dict[str, Any]) -> nio.Event:
    """Return the parsed nio event for one raw source."""
    event = nio.Event.parse_event(source)
    assert isinstance(event, nio.Event)
    return event


@dataclass
class FakeClient:
    """A homeserver that answers exactly what a test set up."""

    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    relations: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    reported_depth: int | None = 3
    relation_calls: int = 0
    relation_events: int = 0
    # A relation tree that outlives any one walk, which is what a long thread of
    # streamed answers is: one original and a run of edits per answer, forever.
    endless_relations: bool = False
    endless_relation_edits: int = 0
    history_pages: int = 0
    history_end_token: str | None = None
    # A room whose history outlives any startup walk, which is what a
    # long-lived room actually looks like.
    endless_history: bool = False
    # What each endless page is made of. A streamed answer is one original
    # followed by a run of edits, so a page of a busy MindRoom room carries far
    # fewer logical messages than it carries events.
    endless_originals_per_page: int = 1
    endless_edits_per_page: int = 0
    # Explicit (chunk, end) pages, for shapes a real server produces that the
    # endless generator cannot express.
    pages: list[tuple[list[dict[str, Any]], str | None]] | None = None
    repeat_last: bool = False
    # The bodies this server serves for long-text sidecars, by MXC URL, and a
    # count of how many times each was actually fetched.
    sidecars: dict[str, str] = field(default_factory=dict)
    downloads: list[str] = field(default_factory=list)
    # Whether this device has crypto set up at all. nio only attempts
    # decryption when it does, so neither does anything reading through it.
    olm: object | None = None
    # The cleartext each encrypted event stands for, by event ID. An encrypted
    # event missing from here is one whose room key never reached this device,
    # which is the ordinary way decryption fails against a real homeserver.
    room_keys: dict[str, dict[str, Any]] = field(default_factory=dict)

    def decrypt_event(self, event: nio.MegolmEvent) -> nio.Event:
        """Decrypt one event, or refuse the way nio refuses.

        Only the relation walk ever reaches this. The other reads a hydrator
        makes come back decrypted already, which is modelled by the fixtures
        storing cleartext for them rather than by decrypting here -- that is
        what nio's own ``receive_response`` has done by the time those return.
        """
        cleartext = self.room_keys.get(event.event_id)
        if cleartext is None:
            msg = f"no megolm session for {event.event_id}"
            raise nio.EncryptionError(msg)
        return parse(cleartext)

    async def download(self, mxc: str) -> nio.DownloadResponse | nio.DownloadError:
        """Return one stored attachment."""
        self.downloads.append(mxc)
        payload = self.sidecars.get(mxc)
        if payload is None:
            return nio.DownloadError("M_NOT_FOUND")
        return nio.DownloadResponse(payload.encode(), "application/json", None)

    async def room_get_event(
        self,
        room_id: str,
        event_id: str,
    ) -> nio.RoomGetEventResponse | nio.RoomGetEventError:
        """Return one stored event."""
        del room_id
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
        """Yield stored relations in server order, enforcing depth the way nio does."""
        del room_id, recurse
        self.relation_calls += 1
        sources = self._ordered_relations(event_id, direction)
        first = next(sources, None)
        # Mirrors nio: an empty page has no depth to report and nothing that
        # could have been truncated, so it is never rejected.
        if first is None:
            return
        if minimum_recursion_depth is not None and (
            self.reported_depth is None or self.reported_depth < minimum_recursion_depth
        ):
            raise nio.InsufficientRecursionDepthError(minimum_recursion_depth, self.reported_depth)
        for source in itertools.chain([first], sources):
            self.relation_events += 1
            yield parse(source)

    def _ordered_relations(
        self,
        event_id: str,
        direction: nio.MessageDirection,
    ) -> Iterator[dict[str, Any]]:
        """Return stored relations in the order a homeserver would send them.

        MSC3981: relations come back in the same topological order ``/messages``
        would give for the same direction, which for these fixtures is their
        timestamp order. A walk that stops early depends on that, so the fake
        has to honor it rather than replay insertion order.
        """
        if self.endless_relations:
            return self._endless_relations()
        return iter(
            sorted(
                self.relations.get(event_id, []),
                key=lambda source: (source["origin_server_ts"], source["event_id"]),
                reverse=direction is not nio.MessageDirection.front,
            ),
        )

    def _endless_relations(self) -> Iterator[dict[str, Any]]:
        """Yield an inexhaustible relation tree, newest first.

        Each answer arrives as its edits and then the answer itself, because
        every edit is newer than the message it revises. Backwards is the only
        direction this one can express, which is the only direction hydration
        asks for.
        """
        for index in itertools.count():
            ts = 1_000_000 - index * 100
            original = f"$answer{index}"
            for edit in reversed(range(self.endless_relation_edits)):
                yield raw(
                    f"{original}-edit{edit}",
                    f"answer {index} v{edit}",
                    ts=ts + 1 + edit,
                    replaces=original,
                )
            yield raw(original, f"answer {index}", ts=ts, thread_id="$root")

    async def room_messages(
        self,
        room_id: str,
        start: str | None = None,
        direction: object = None,
        limit: int = 10,
    ) -> nio.RoomMessagesResponse | nio.RoomMessagesError:
        """Return one page of history, then successful exhaustion."""
        del room_id, start, direction, limit
        self.history_pages += 1
        if self.pages is not None:
            index = min(self.history_pages - 1, len(self.pages) - 1) if self.repeat_last else self.history_pages - 1
            sources, end = self.pages[index]
            return nio.RoomMessagesResponse(ROOM, [parse(source) for source in sources], "start", end)
        if self.endless_history:
            return nio.RoomMessagesResponse(
                ROOM,
                self._endless_page(self.history_pages),
                "start",
                f"token-{self.history_pages}",
            )
        if self.history_pages > 1:
            return nio.RoomMessagesResponse(ROOM, [], "start", self.history_end_token)
        return nio.RoomMessagesResponse(
            ROOM,
            [parse(source) for source in self.history],
            "start",
            self.history_end_token,
        )

    def _endless_page(self, page: int) -> list[nio.Event]:
        """Return one page of an inexhaustible room."""
        events: list[nio.Event] = []
        for index in range(self.endless_originals_per_page):
            original = f"$page{page}-{index}"
            events.append(parse(raw(original, f"message {page}-{index}", ts=1_000 + page)))
            events.extend(
                parse(
                    raw(
                        f"{original}-edit{edit}",
                        f"message {page}-{index} v{edit}",
                        ts=1_001 + page + edit,
                        replaces=original,
                    ),
                )
                for edit in range(self.endless_edits_per_page)
            )
        return events


def hydrator(
    store: PrincipalStore,
    client: FakeClient,
    *,
    self_sender: str = BOT,
    require_complete: bool = False,
    policy: HydrationPolicy = HydrationPolicy.PROMPT,
    **bounds: int,
) -> ConversationHydrator:
    """Return a hydrator wired to a fake homeserver.

    The bounds are shrunk far below either policy's real ceilings so a walk can
    reach one inside a test. The policy is passed separately for that reason:
    it names which caller this stands for, and the ordering that the durable
    marker is compared on is the policy's, not the shrunken numbers'.
    """
    return ConversationHydrator(
        store=store,
        runtime=SimpleNamespace(client=client),  # type: ignore[arg-type]
        self_sender=self_sender,
        require_complete=require_complete,
        policy=policy,
        **bounds,
    )


# What an export caller is, as the hydrator sees it: it needs the whole
# conversation and it walks under the widest policy there is. Named once
# because the two travel together. A strict caller left on the prompt policy
# would be satisfied by a prompt's own marker, which is the bug that made every
# warm thread unexportable.
EXPORT_CALLER: dict[str, Any] = {"require_complete": True, "policy": HydrationPolicy.EXPORT}


def edited_thread(answers: int, edits: int) -> FakeClient:
    """Return a thread of streamed answers, each ending at its newest edit."""
    relations: list[dict[str, Any]] = []
    for index in range(answers):
        ts = 1_000 + index * 100
        original = f"$answer{index}"
        relations.append(raw(original, f"answer {index}", ts=ts, thread_id="$root"))
        relations.extend(
            raw(f"{original}-edit{edit}", f"answer {index} v{edit}", ts=ts + 1 + edit, replaces=original)
            for edit in range(edits)
        )
    return FakeClient(events={"$root": raw("$root", "root", ts=500)}, relations={"$root": relations})


@dataclass
class HeldFirstWalk(FakeClient):
    """A homeserver that holds the first thread walk open until the test lets it go.

    Two hydrators walk one principal and nothing sequences them, so which of
    them installs last is a real degree of freedom rather than a scheduling
    accident. Holding the first walk at the server picks that order outright,
    which is the only way to state it without a timing guess.
    """

    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def room_get_event_relations(
        self,
        *,
        room_id: str,
        event_id: str,
        direction: nio.MessageDirection = nio.MessageDirection.back,
        recurse: bool = False,
        minimum_recursion_depth: int | None = None,
    ) -> AsyncIterator[nio.Event]:
        """Yield stored relations, parking the first caller at the gate."""
        if not self.started.is_set():
            self.started.set()
            await self.release.wait()
        async for event in super().room_get_event_relations(
            room_id=room_id,
            event_id=event_id,
            direction=direction,
            recurse=recurse,
            minimum_recursion_depth=minimum_recursion_depth,
        ):
            yield event


async def admit_all(store: PrincipalStore, sources: Iterable[dict[str, Any]]) -> None:
    """Admit raw events as live traffic."""
    for source in sources:
        event = parse(source)
        await store.admit(
            inbound_event(ROOM, event, EventKind.MESSAGE, EventClass.ACTIONABLE),
            projected_event(ROOM, event, EventKind.MESSAGE, self_sender=BOT),
        )


async def bodies(store: PrincipalStore, thread_id: str | None = None) -> list[str]:
    """Return the visible bodies of one conversation."""
    page = await store.read_conversation(room_id=ROOM, thread_id=thread_id, limit=50)
    return [str(m.content["body"]) for m in page.messages]


async def refreshes(store: PrincipalStore, thread_id: str | None = None) -> tuple[RefreshRequest, ...]:
    """Return the refetch debts one conversation read reports, the way production learns them."""
    page = await store.read_conversation(room_id=ROOM, thread_id=thread_id, limit=50)
    return page.refresh_pending


async def revisions(store: PrincipalStore, thread_id: str | None = None) -> list[str]:
    """Return which revision each logical message is currently showing."""
    page = await store.read_conversation(room_id=ROOM, thread_id=thread_id, limit=50)
    return [m.revision_event_id for m in page.messages]


def stream_raw(
    event_id: str,
    body: str,
    status: str,
    *,
    sender: str = BOT,
    ts: int = 1_000,
    thread_id: str | None = None,
    replaces: str | None = None,
) -> dict[str, Any]:
    """Return one frame of a streamed answer, as the server would serve it.

    Edits go through the production envelope builder rather than a
    hand-written shape, so a change to where the stream status lives inside an
    edit breaks these tests instead of quietly making them test nothing.
    """
    if replaces is not None:
        content = build_edit_event_content(
            event_id=replaces,
            new_content={"msgtype": "m.text", "body": body},
            new_text=body,
            extra_content={STREAM_STATUS_KEY: status},
        )
    else:
        content = {"msgtype": "m.text", "body": body, STREAM_STATUS_KEY: status}
        if thread_id is not None:
            content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    return {
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": ts,
        "type": "m.room.message",
        "content": content,
    }


class TestRevisionReduction:
    """Hydration must reach the same answer the live projection would."""

    async def test_the_original_wins_when_there_are_no_edits(self) -> None:
        """The original wins when there are no edits."""
        original = _projected_from_event(ROOM, parse(raw("$m", "first")), self_sender=BOT)
        assert original is not None

        revision = _reduce_current_revision(original, ())

        assert revision.event_id == "$m"
        assert revision.content["body"] == "first"

    async def test_the_newest_edit_wins(self) -> None:
        """The newest edit wins."""
        original = _projected_from_event(ROOM, parse(raw("$m", "first")), self_sender=BOT)
        assert original is not None
        relations = [
            _projected_from_event(ROOM, parse(raw("$e1", "second", ts=2_000, replaces="$m")), self_sender=BOT),
            _projected_from_event(ROOM, parse(raw("$e2", "third", ts=3_000, replaces="$m")), self_sender=BOT),
        ]

        revision = _reduce_current_revision(original, [r for r in relations if r is not None])

        assert revision.content["body"] == "third"

    async def test_an_edit_from_another_sender_is_ignored(self) -> None:
        """An edit from another sender is ignored."""
        original = _projected_from_event(ROOM, parse(raw("$m", "first")), self_sender=BOT)
        assert original is not None
        forged = _projected_from_event(
            ROOM,
            parse(raw("$e", "forged", sender=BOB, ts=9_000, replaces="$m")),
            self_sender=BOT,
        )
        assert forged is not None

        revision = _reduce_current_revision(original, [forged])

        assert revision.content["body"] == "first"

    @pytest.mark.parametrize("status", [STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING])
    async def test_this_bots_progress_edit_converts_to_nothing(self, status: str) -> None:
        """The converter is where the two paths are made to agree."""
        source = parse(stream_raw("$p", "half an ans", status, ts=2_000, replaces="$answer"))

        assert _projected_from_event(ROOM, source, self_sender=BOT) is None

    @pytest.mark.parametrize(
        "status",
        [
            STREAM_STATUS_COMPLETED,
            STREAM_STATUS_CANCELLED,
            STREAM_STATUS_ERROR,
            STREAM_STATUS_INTERRUPTED,
        ],
    )
    async def test_this_bots_terminal_edit_still_converts(self, status: str) -> None:
        """Only an unfinished revision is transport; every ending is content."""
        source = parse(stream_raw("$t", "the whole answer", status, ts=2_000, replaces="$answer"))

        projected = _projected_from_event(ROOM, source, self_sender=BOT)

        assert projected is not None
        assert projected.replaces_event_id == "$answer"

    @pytest.mark.parametrize("status", [STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING])
    async def test_someone_elses_progress_edit_still_converts(self, status: str) -> None:
        """Only this bot's own revisions are transport."""
        source = parse(stream_raw("$p", "half an ans", status, sender=ALICE, ts=2_000, replaces="$answer"))

        assert _projected_from_event(ROOM, source, self_sender=BOT) is not None

    @pytest.mark.parametrize(
        ("replacement_status", "fallback_status", "is_transport"),
        [
            (STREAM_STATUS_STREAMING, STREAM_STATUS_COMPLETED, True),
            (STREAM_STATUS_COMPLETED, STREAM_STATUS_STREAMING, False),
        ],
    )
    async def test_the_replacement_body_decides_not_the_fallback(
        self,
        replacement_status: str,
        fallback_status: str,
        is_transport: bool,
    ) -> None:
        """An edit says two things, and only one of them is the message.

        A Matrix edit carries its real content under ``m.new_content`` and a
        fallback copy at the top level for clients that do not resolve edits.
        The projection installs the replacement, so the replacement is what has
        to be classified; reading the fallback would let the two disagree and
        pick the wrong one.
        """
        source = stream_raw("$e", "half an ans", replacement_status, ts=2_000, replaces="$answer")
        source["content"][STREAM_STATUS_KEY] = fallback_status

        projected = _projected_from_event(ROOM, parse(source), self_sender=BOT)

        assert (projected is None) is is_transport

    async def test_this_bots_placeholder_still_converts(self) -> None:
        """A pending original is the message the answer will land on."""
        source = parse(stream_raw("$answer", "Thinking...", STREAM_STATUS_PENDING, ts=2_000))

        projected = _projected_from_event(ROOM, source, self_sender=BOT)

        assert projected is not None
        assert projected.replaces_event_id is None

    async def test_a_redacted_event_projects_as_its_own_deletion(self) -> None:
        """A stripped body is a deletion, and dropping it applies nothing.

        A thread walk never sees the ``m.room.redaction`` event -- a redaction
        carries no ``m.relates_to``, so it is in no relation tree -- and this
        shape is the only thing that says the message is gone. Reading it as a
        deletion of itself is what removes the body already in the projection
        instead of leaving it readable.
        """
        projected = _projected_from_event(ROOM, parse(raw("$m", "gone", redacted=True)), self_sender=BOT)

        assert projected is not None
        assert projected.redacts_event_id == "$m"

    async def test_a_redaction_projects_as_a_deletion_of_its_target(self) -> None:
        """The other shape a walk meets, carrying the same fact about another event."""
        projected = _projected_from_event(ROOM, parse(redaction("$r", "$m", ts=3_000)), self_sender=BOT)

        assert projected is not None
        assert projected.redacts_event_id == "$m"

    async def test_a_redacted_event_this_projection_never_held_deletes_nothing(self) -> None:
        """Only a message can be deleted from a projection, because only a message is in one.

        A redaction preserves its target's ``type``, so a stripped event whose
        type is not ``m.room.message`` is the remains of something this
        projection never stored -- a reaction, a state event. Reading that as a
        deletion of itself writes a durable tombstone naming an event no visible
        message ever had: it removes nothing, and every later walk of the room
        has to carry it.
        """
        source = {
            "event_id": "$reaction",
            "sender": ALICE,
            "origin_server_ts": 1_000,
            "type": "m.reaction",
            "content": {},
            "unsigned": {"redacted_because": {"type": "m.room.redaction", "sender": ALICE, "content": {}}},
        }

        assert _projected_from_event(ROOM, parse(source), self_sender=BOT) is None


class TestThreadHydration:
    """A thread is built from its root plus its whole relation tree."""

    async def test_a_thread_is_hydrated_once(self, alice: PrincipalStore) -> None:
        """A thread is hydrated once."""
        client = FakeClient(
            events={"$root": raw("$root", "root")},
            relations={
                "$root": [
                    raw("$reply", "reply", ts=2_000, thread_id="$root"),
                    raw("$edit", "reply edited", ts=3_000, replaces="$reply"),
                ],
            },
        )
        hydrate = hydrator(alice, client)

        await hydrate.ensure_hydrated(room_id=ROOM, thread_id="$root")
        await hydrate.ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert client.relation_calls == 1
        # The root carries no thread relation of its own, so reading the thread
        # has to merge it back in; a thread that starts at its first reply is
        # missing the message the whole thread is about.
        assert await bodies(alice, "$root") == ["root", "reply edited"]

    async def test_concurrent_readers_share_one_hydration(self, alice: PrincipalStore) -> None:
        """Concurrent readers share one hydration."""
        client = FakeClient(events={"$root": raw("$root", "root")}, relations={"$root": []})
        hydrate = hydrator(alice, client)

        await asyncio.gather(
            *(hydrate.ensure_hydrated(room_id=ROOM, thread_id="$root") for _ in range(5)),
        )

        assert client.relation_calls == 1

    async def test_a_reader_that_reaches_the_walk_late_does_not_walk_again(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A reader that arrives at the walk after another finished it does not walk again.

        The sibling above races five readers and so only catches this when the
        scheduler happens to cooperate. This one reproduces the losing ordering
        directly. ``ensure_hydrated`` checks the marker before it awaits, but it
        awaits twice more before starting the walk, and ``_shared`` can only join
        a task it can still see -- a finished one has already been dropped. So the
        last arrival reaches the walk itself, and the durable marker is what has
        to stop it.
        """
        client = FakeClient(events={"$root": raw("$root", "root")}, relations={"$root": []})
        hydrate = hydrator(alice, client)

        await hydrate.ensure_hydrated(room_id=ROOM, thread_id="$root")
        assert client.relation_calls == 1

        await hydrate._hydrate(room_id=ROOM, thread_id="$root")

        assert client.relation_calls == 1
        assert await bodies(alice, "$root") == ["root"]

    async def test_a_server_that_ignores_recurse_fails_the_read(
        self,
        alice: PrincipalStore,
    ) -> None:
        """No fallback: such a server silently returns only direct children.

        Omitting the depth is the only portable signal that ``recurse`` was not
        honored, because the number itself means different things on different
        servers.
        """
        client = FakeClient(
            events={"$root": raw("$root", "root")},
            relations={"$root": [raw("$reply", "reply", ts=2_000, thread_id="$root")]},
            reported_depth=None,
        )

        with pytest.raises(_HydrationError, match="recursion depth"):
            await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id="$root")

    async def test_a_shallow_reported_depth_is_accepted(self, alice: PrincipalStore) -> None:
        """Verified against a live Tuwunel: a complete page can report 0.

        Tuwunel reports the depth of the deepest event it returned, so a
        relation tree that is genuinely one level deep reports one level. A
        floor above zero would reject ordinary conversations.
        """
        client = FakeClient(
            events={"$root": raw("$root", "root")},
            relations={"$root": [raw("$reply", "reply", ts=2_000, thread_id="$root")]},
            reported_depth=0,
        )

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert await bodies(alice, "$root") == ["root", "reply"]

    async def test_an_empty_relation_page_is_not_a_failure(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A message with no relations reports no depth, and that is fine."""
        client = FakeClient(
            events={"$root": raw("$root", "root")},
            relations={"$root": []},
            reported_depth=None,
        )

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert await bodies(alice, "$root") == ["root"]

    async def test_a_failed_hydration_is_retried_not_cached(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A failed hydration is retried not cached."""
        client = FakeClient(events={}, relations={})

        with pytest.raises(_HydrationError):
            await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id="$root")

        client.events["$root"] = raw("$root", "root")
        client.relations["$root"] = []
        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert await bodies(alice, "$root") == ["root"]


class TestThreadHydrationBounds:
    """A thread's relation tree is walked as far as a prompt needs, and no further.

    The room walk's ceilings do not carry over mechanically, because a thread
    has no backwards pagination of its own. What carries over is the unit: the
    window counts logical messages, and the event ceiling counts raw relation
    events, which in this product differ by an order of magnitude because a
    streamed answer is one message and a long run of ``m.replace`` edits.
    """

    async def test_the_window_counts_messages_a_prompt_can_read_not_events(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Edits revise a message; they do not fill the window with new ones.

        The room walk learned this the hard way and the thread walk never got
        the same treatment. A thread of streamed answers has far more relations
        than messages, so a bound counted in events would call a handful of
        answers a full prompt window and hydrate almost nothing.
        """
        client = FakeClient(events={"$root": raw("$root", "root")}, endless_relations=True, endless_relation_edits=19)

        await hydrator(
            alice,
            client,
            prompt_window_messages=5,
            max_fetched_events=2_000,
        ).ensure_hydrated(room_id=ROOM, thread_id="$root")

        page = await alice.read_conversation(room_id=ROOM, thread_id="$root", limit=100)
        # The root over and above the window: a thread that starts at its first
        # reply is missing the message the whole thread is about.
        assert len(page.messages) == 6

    async def test_a_message_inside_the_window_keeps_its_whole_edit_tail(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The one thing truncation must never do is split a message from its edits.

        A message installed without its newest edit is shown at a stale
        revision, which is wrong text rather than less history, and no reader
        can tell. The walk may therefore only stop where the relation stream has
        just finished delivering a message -- which, newest first, is the moment
        an original arrives, because every edit of it is newer and already read.
        """
        client = edited_thread(answers=4, edits=3)

        await hydrator(alice, client, prompt_window_messages=2).ensure_hydrated(room_id=ROOM, thread_id="$root")

        # The two newest answers, each at its newest revision, and the root.
        assert await bodies(alice, "$root") == ["root", "answer 2 v2", "answer 3 v2"]
        assert await revisions(alice, "$root") == ["$root", "$answer2-edit2", "$answer3-edit2"]

    async def test_the_event_ceiling_stops_a_thread_the_window_never_would(
        self,
        alice: PrincipalStore,
    ) -> None:
        """One pathological thread must not walk its whole relation tree.

        This is the bound the thread walk was missing entirely: every relation
        was accumulated into one list and written in one projection
        transaction, so a first strict read paid for the whole tree before it
        returned anything.
        """
        client = FakeClient(events={"$root": raw("$root", "root")}, endless_relations=True, endless_relation_edits=19)

        await hydrator(
            alice,
            client,
            prompt_window_messages=50,
            max_fetched_events=200,
        ).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert client.relation_events == 200
        page = await alice.read_conversation(room_id=ROOM, thread_id="$root", limit=100)
        assert len(page.messages) < 50

    async def test_reaching_the_ceiling_still_marks_the_thread_hydrated(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The marker records that the one-time walk ran, not that it filled.

        Withholding it would re-run the whole truncated walk on every read of
        that thread, which is far worse than a prompt with less history than
        its maximum. This is the room walk's recorded trade and it transfers.
        """
        client = FakeClient(events={"$root": raw("$root", "root")}, endless_relations=True, endless_relation_edits=19)

        await hydrator(
            alice,
            client,
            prompt_window_messages=50,
            max_fetched_events=200,
        ).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id="$root")

    async def test_a_bounded_thread_is_hydrated_but_not_complete(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Being hydrated and being whole are different facts, and both are recorded.

        A reader whose correctness is completeness rather than recency -- an
        export, not a prompt -- cannot tell a windowed thread from a short one
        by looking at the projection, so the walk records why it stopped
        instead of leaving it to be inferred.
        """
        client = edited_thread(answers=4, edits=1)

        await hydrator(alice, client, prompt_window_messages=2).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id="$root")
        assert not await alice.conversation_is_complete(room_id=ROOM, thread_id="$root")

    async def test_a_thread_the_walk_read_to_the_end_is_complete(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Running out of relations is completeness, and it is recorded as such."""
        client = edited_thread(answers=4, edits=1)

        await hydrator(alice, client, prompt_window_messages=50).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert await alice.conversation_is_complete(room_id=ROOM, thread_id="$root")

    async def test_a_truncated_walk_is_the_only_thing_that_proves_a_page_is_a_suffix(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A prompt must reject a windowed thread without rejecting a new room.

        These are the two halves a prompt has to tell apart, and the negation
        of `conversation_is_complete` conflates them: a conversation nothing
        ever walked is not complete, but nothing is missing from it either.
        Reporting it truncated would mark every brand-new room's first turn as
        partial history, so the question is asked the other way round.
        """
        client = edited_thread(answers=4, edits=1)

        assert not await alice.conversation_hydration_was_truncated(room_id=ROOM, thread_id="$root")

        await hydrator(alice, client, prompt_window_messages=2).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert await alice.conversation_hydration_was_truncated(room_id=ROOM, thread_id="$root")

    async def test_a_walk_that_reached_the_end_is_not_truncated(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The other direction: a whole thread must not be reported as a suffix."""
        client = edited_thread(answers=4, edits=1)

        await hydrator(alice, client, prompt_window_messages=50).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert not await alice.conversation_hydration_was_truncated(room_id=ROOM, thread_id="$root")


class TestCompletenessRequirement:
    """Who a hydration marker is good enough for.

    One projection serves both kinds of caller, on purpose and under the same
    principal, so the marker a bounded walk leaves is read by a caller that can
    use a suffix and by one that cannot. The bounds a strict caller is built
    with are worth nothing if the short-circuit hands it someone else's walk.
    """

    async def test_a_prompt_keeps_its_warm_marker_and_never_rewalks(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The short-circuit is why startup prewarm could be deleted; it stays cheap.

        A prompt read of a thread whose walk stopped at the window must cost
        nothing at all. Making every warm read re-walk would trade the export
        defect for a far worse one, on the hot path rather than a batch job.
        """
        client = edited_thread(answers=4, edits=1)
        await hydrator(alice, client, prompt_window_messages=2).ensure_hydrated(room_id=ROOM, thread_id="$root")
        assert await alice.conversation_hydration_was_truncated(room_id=ROOM, thread_id="$root")
        walked = client.relation_calls

        await hydrator(alice, client, prompt_window_messages=2).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert client.relation_calls == walked

    async def test_a_strict_caller_walks_past_a_marker_left_by_a_bounded_walk(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The defect itself, at the seam that owns it.

        The prompt path gets to every thread first, so a strict caller that
        accepted its marker would never once use the larger bounds it was
        built with.
        """
        client = edited_thread(answers=4, edits=1)
        await hydrator(alice, client, prompt_window_messages=2).ensure_hydrated(room_id=ROOM, thread_id="$root")

        await hydrator(
            alice,
            client,
            prompt_window_messages=50,
            **EXPORT_CALLER,
        ).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert await alice.conversation_is_complete(room_id=ROOM, thread_id="$root")
        assert await bodies(alice, "$root") == [
            "root",
            "answer 0 v0",
            "answer 1 v0",
            "answer 2 v0",
            "answer 3 v0",
        ]

    async def test_a_strict_caller_accepts_a_walk_that_already_reached_the_end(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The mirror: completeness is satisfiable, not a standing order to re-walk.

        Without this the fix above would also pass if a strict caller simply
        walked on every read, which is the mistake this shape is most likely to
        make.
        """
        client = edited_thread(answers=4, edits=1)
        strict = hydrator(alice, client, prompt_window_messages=50, **EXPORT_CALLER)
        await strict.ensure_hydrated(room_id=ROOM, thread_id="$root")
        walked = client.relation_calls

        await strict.ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert client.relation_calls == walked

    async def test_a_strict_caller_owes_the_deeper_walk_only_once(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A thread past even the strict caller's own bounds must not be re-walked.

        The deeper walk answers the question once. Repeating it would re-read
        the same ceiling for the same marker, and the epoch-retry budget would
        turn one unexportable thread into three full walks of it. The record
        stays honestly truncated, which is what lets the caller refuse.

        Once means once across calls and not merely within one, which is where
        this has to be measured: a strict caller builds a fresh hydrator every
        time it runs, so a requirement discharged in a local variable is
        discharged for nobody. Asked twice here for exactly that reason. At the
        real export allowance the repeat is millions of fetched events, paid on
        every read of a thread that will never satisfy it.
        """
        client = edited_thread(answers=4, edits=1)
        strict = hydrator(alice, client, prompt_window_messages=2, **EXPORT_CALLER)

        await strict.ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert client.relation_calls == 1
        assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id="$root")
        assert not await alice.conversation_is_complete(room_id=ROOM, thread_id="$root")

        await hydrator(alice, client, prompt_window_messages=2, **EXPORT_CALLER).ensure_hydrated(
            room_id=ROOM,
            thread_id="$root",
        )

        assert client.relation_calls == 1
        # Still refused, which is the other half: not re-walking must not
        # become quietly calling a truncated thread whole.
        assert not await alice.conversation_is_complete(room_id=ROOM, thread_id="$root")
        assert await alice.conversation_hydration_was_truncated(room_id=ROOM, thread_id="$root")

    async def test_a_narrower_walk_finishing_last_does_not_unsay_a_wider_one(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Coverage only grows inside a membership, whatever order the walks land in.

        The two callers own separate hydrators over one durable principal --
        that sharing is the whole reason a warm export costs no Matrix calls --
        and nothing sequences them, deliberately, because a lock between them
        would put a stall on every warm prompt read. So the narrower walk can
        finish last, and its install used to overwrite the wider walk's marker
        with its own smaller answer while every message that walk installed was
        still projected: the rows were right and the marker lied about them.
        Whoever read it next either failed a thread it was holding whole or
        paid for the whole larger walk again.
        """
        thread = edited_thread(answers=5, edits=1)
        client = HeldFirstWalk(events=thread.events, relations=thread.relations)
        prompt = hydrator(alice, client, prompt_window_messages=2)
        export = hydrator(alice, client, prompt_window_messages=50, **EXPORT_CALLER)

        bounded = asyncio.create_task(prompt.ensure_hydrated(room_id=ROOM, thread_id="$root"))
        await client.started.wait()
        await export.ensure_hydrated(room_id=ROOM, thread_id="$root")
        assert await alice.conversation_is_complete(room_id=ROOM, thread_id="$root")

        client.release.set()
        await bounded

        assert await bodies(alice, "$root") == [
            "root",
            "answer 0 v0",
            "answer 1 v0",
            "answer 2 v0",
            "answer 3 v0",
            "answer 4 v0",
        ]
        assert await alice.conversation_is_complete(room_id=ROOM, thread_id="$root")
        assert not await alice.conversation_hydration_was_truncated(room_id=ROOM, thread_id="$root")

    async def test_a_narrower_walk_finishing_last_does_not_unspend_a_wider_bound(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The same race, for the other thing the marker remembers.

        When the wider walk also stops short there is no completeness for the
        narrower one to overwrite, and what it would take back instead is the
        record of which bound has been spent here. The next caller at the wider
        bound would then pay for the identical walk again -- the permanent
        re-walk this record exists to prevent, arrived at through the race
        rather than through a second call.
        """
        thread = edited_thread(answers=5, edits=1)
        client = HeldFirstWalk(events=thread.events, relations=thread.relations)
        prompt = hydrator(alice, client, prompt_window_messages=2)
        export = hydrator(alice, client, prompt_window_messages=3, **EXPORT_CALLER)

        bounded = asyncio.create_task(prompt.ensure_hydrated(room_id=ROOM, thread_id="$root"))
        await client.started.wait()
        await export.ensure_hydrated(room_id=ROOM, thread_id="$root")
        client.release.set()
        await bounded
        walked = client.relation_calls

        await hydrator(alice, client, prompt_window_messages=3, **EXPORT_CALLER).ensure_hydrated(
            room_id=ROOM,
            thread_id="$root",
        )

        assert client.relation_calls == walked
        # Neither walk reached the start, so the thread is still honestly
        # short and a strict caller still refuses it.
        assert not await alice.conversation_is_complete(room_id=ROOM, thread_id="$root")


class TestRoomHydration:
    """Room history is walked once, and exhaustion is not a failure."""

    async def test_history_populates_the_conversation(self, alice: PrincipalStore) -> None:
        """History populates the conversation."""
        client = FakeClient(history=[raw("$b", "second", ts=2_000), raw("$a", "first", ts=1_000)])

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

        assert await bodies(alice) == ["first", "second"]

    async def test_an_empty_chunk_without_an_end_token_is_exhaustion(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An empty chunk without an end token is exhaustion.

        This shape used to be read as failure and left rooms unready.
        """
        client = FakeClient(history=[raw("$a", "first")], history_end_token=None)

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

        assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)
        assert await bodies(alice) == ["first"]

    async def test_hydration_stops_once_the_prompt_window_is_full(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Hydration promises the prompt window, not a mirror of the room.

        A long-lived room has more history than any startup walk should read.
        Stopping once the window is full is the contract being met, not a
        shortfall: a caller needing older history paginates Matrix directly.
        """
        client = FakeClient(endless_history=True, endless_originals_per_page=_MESSAGES_PAGE_LIMIT)

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

        assert client.history_pages == HYDRATED_PROMPT_WINDOW_MESSAGES // _MESSAGES_PAGE_LIMIT
        assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)

    async def test_the_window_counts_messages_a_prompt_can_read_not_events(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Edits revise a message; they do not fill the window with new ones.

        MindRoom streams by editing, so the ratio of Matrix events to logical
        messages in its own rooms is an order of magnitude, not a rounding
        error. A walk that stopped after a fixed number of pages would call a
        handful of messages a full prompt window and hydrate almost nothing.
        """
        client = FakeClient(
            endless_history=True,
            endless_originals_per_page=1,
            endless_edits_per_page=_MESSAGES_PAGE_LIMIT - 1,
        )
        window = 5

        await hydrator(alice, client, prompt_window_messages=window).ensure_hydrated(room_id=ROOM, thread_id=None)

        assert client.history_pages == window
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=window * 2)
        assert len(page.messages) == window

    async def test_the_window_is_not_spent_on_deletions_either(self, alice: PrincipalStore) -> None:
        """A redaction removes a message from the window; it does not fill a slot in it.

        Redactions project now, which is what applies a deletion the walk finds
        -- and a projection is not automatically a message. Counting one would
        make every deleted message in a room's recent history cost a live one
        out of the prompt, silently, and a room whose tip is a burst of cleanup
        would hydrate almost nothing.
        """
        window = 3
        client = FakeClient(
            pages=[
                ([redaction("$r1", "$gone1", ts=9_000), redaction("$r2", "$gone2", ts=8_900), raw("$c", "c")], "t1"),
                ([raw("$b", "b")], "t2"),
                ([raw("$a", "a")], "t3"),
                ([], None),
            ],
        )

        await hydrator(alice, client, prompt_window_messages=window).ensure_hydrated(room_id=ROOM, thread_id=None)

        assert client.history_pages == window
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=window * 2)
        assert len(page.messages) == window

    async def test_a_room_of_nothing_but_edits_still_stops(self, alice: PrincipalStore) -> None:
        """The window is what hydration aims for, not what it will spend."""
        client = FakeClient(
            endless_history=True,
            endless_originals_per_page=1,
            endless_edits_per_page=_MESSAGES_PAGE_LIMIT - 1,
        )

        await hydrator(
            alice,
            client,
            prompt_window_messages=1_000,
            max_fetched_events=_MESSAGES_PAGE_LIMIT * 3,
        ).ensure_hydrated(room_id=ROOM, thread_id=None)

        assert client.history_pages == 3
        assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)

    async def test_an_empty_page_with_a_continuation_is_not_exhaustion(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A filtered page can be empty and still have history behind it.

        Only the missing continuation token means the server has run out.
        Treating an empty chunk as exhaustion stops the walk one page early and
        then records the short result as a hydrated conversation.
        """
        client = FakeClient(pages=[([], "more"), ([raw("$older", "older")], None)])

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

        assert client.history_pages == 2
        assert await bodies(alice) == ["older"]

    async def test_a_repeated_token_fails_without_marking_the_room_hydrated(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Neither supported server signals exhaustion by repeating the token.

        Tuwunel derives `end` from the last event returned and omits it for an
        empty page; Synapse omits it explicitly. So a repeated token is a
        stall. Completing on it would install a hydration marker, and hydration
        runs once per membership, so one transient stall would become permanent
        truncation. Failing leaves the conversation unhydrated, which a later
        read can still repair.
        """
        client = FakeClient(pages=[([], "same"), ([], "same")], repeat_last=True)

        with pytest.raises(_HydrationError):
            await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

        assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)

    async def test_the_request_ceiling_is_not_the_event_ceiling(self, alice: PrincipalStore) -> None:
        """One event per page must not be mistaken for a full event budget.

        Deriving the request bound from the event bound made a sparse room stop
        after two pages while reporting that it had read the whole event
        allowance.
        """
        client = FakeClient(endless_history=True, endless_originals_per_page=1)

        await hydrator(alice, client, max_fetched_events=200, max_requests=5).ensure_hydrated(
            room_id=ROOM,
            thread_id=None,
        )

        assert client.history_pages == 5

    async def test_hydration_does_not_create_pending_work(self, alice: PrincipalStore) -> None:
        """Hydration does not create pending work."""
        client = FakeClient(history=[raw("$a", "first")])

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

        assert await alice.pending() == ()


class TestStreamingProgressIsTransport:
    """A cold read must reach the conversation the live path would have kept.

    Hydration fetches the whole relation tree, so a rule applied only to live
    admission would be undone by the first cold read of the room: every
    progress edit the live path declined would come back from the server and
    reduce. The two paths therefore share one predicate, and these pin the
    hydration half of it.

    Unlike live admission, hydration does not filter by ``msgtype`` anywhere,
    so a progress edit really does arrive here whether it was sent as
    ``m.text`` or ``m.notice``. This is the path where the rule pays for
    itself.
    """

    @staticmethod
    def _streamed_answer(status: str | None, *, thread_id: str | None = "$root") -> list[dict[str, Any]]:
        """Return a placeholder, five progress edits, and an optional ending."""
        frames = [stream_raw("$answer", "Thinking...", STREAM_STATUS_PENDING, ts=2_000, thread_id=thread_id)]
        frames.extend(
            stream_raw(
                f"$progress{index}",
                f"partial {index}",
                STREAM_STATUS_STREAMING,
                ts=2_000 + index,
                replaces="$answer",
            )
            for index in range(1, 6)
        )
        if status is not None:
            frames.append(
                stream_raw("$terminal", "the whole answer", status, ts=2_100, replaces="$answer"),
            )
        return frames

    def _thread_client(self, status: str | None) -> FakeClient:
        return FakeClient(
            events={"$root": raw("$root", "the question")},
            relations={"$root": self._streamed_answer(status)},
        )

    @pytest.mark.parametrize("status", [STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING])
    async def test_a_refetched_progress_edit_is_not_reinstalled(
        self,
        alice: PrincipalStore,
        status: str,
    ) -> None:
        """The one revision this bot never wrote down must not come back."""
        client = FakeClient(
            events={"$root": raw("$root", "the question")},
            relations={
                "$root": [
                    stream_raw("$answer", "Thinking...", STREAM_STATUS_PENDING, ts=2_000, thread_id="$root"),
                    stream_raw("$progress", "half an ans", status, ts=2_001, replaces="$answer"),
                ],
            },
        )

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert await bodies(alice, "$root") == ["the question", "Thinking..."]
        assert await revisions(alice, "$root") == ["$root", "$answer"]

    @pytest.mark.parametrize(
        "status",
        [
            STREAM_STATUS_COMPLETED,
            STREAM_STATUS_CANCELLED,
            STREAM_STATUS_ERROR,
            STREAM_STATUS_INTERRUPTED,
        ],
    )
    async def test_a_refetched_terminal_revision_is_installed(
        self,
        alice: PrincipalStore,
        status: str,
    ) -> None:
        """Every terminal ending is content, and a cold read must show it."""
        await hydrator(alice, self._thread_client(status)).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert await bodies(alice, "$root") == ["the question", "the whole answer"]
        assert await revisions(alice, "$root") == ["$root", "$terminal"]
        page = await alice.read_conversation(room_id=ROOM, thread_id="$root", limit=50)
        assert page.messages[1].content[STREAM_STATUS_KEY] == status

    async def test_a_cold_read_after_a_crash_mid_stream_shows_the_placeholder(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Nothing intermediate was durable, so nothing intermediate comes back.

        This is the ordering the whole rule has to survive: the process died
        between the last progress edit and the terminal one, so the server
        holds five progress edits and no ending. Reducing them would show the
        user a half-written answer as if it were finished.
        """
        await hydrator(alice, self._thread_client(None)).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert await bodies(alice, "$root") == ["the question", "Thinking..."]
        assert await revisions(alice, "$root") == ["$root", "$answer"]

    async def test_a_room_walk_skips_progress_edits_too(self, alice: PrincipalStore) -> None:
        """The room walk and the thread walk must agree.

        They are different fetches — ``/messages`` against a room, relations
        against a root — and only the shared converter makes them reach the
        same conversation.
        """
        client = FakeClient(history=list(reversed(self._streamed_answer(None, thread_id=None))))

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

        assert await bodies(alice) == ["Thinking..."]
        assert await revisions(alice) == ["$answer"]

    @pytest.mark.parametrize("status", [STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING])
    async def test_a_refetched_user_edit_claiming_a_transport_status_reduces(
        self,
        alice: PrincipalStore,
        status: str,
    ) -> None:
        """A stream status in someone else's edit is a claim, not a permission."""
        client = FakeClient(
            events={"$root": raw("$root", "the question")},
            relations={
                "$root": [
                    raw("$ask", "frist follow-up", ts=2_000, thread_id="$root"),
                    stream_raw("$fix", "first follow-up", status, sender=ALICE, ts=2_001, replaces="$ask"),
                ],
            },
        )

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert await bodies(alice, "$root") == ["the question", "first follow-up"]
        assert await revisions(alice, "$root") == ["$root", "$fix"]

    @pytest.mark.parametrize("status", [STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING])
    async def test_a_point_refetch_ignores_this_bots_progress_edits(
        self,
        alice: PrincipalStore,
        status: str,
    ) -> None:
        """The one exceptional repair reduces by the same rule as everything else.

        Redacting the visible revision sends the hydrator back to the server
        for whatever is left. If progress edits counted there, the repair would
        install a body the projection had spent the whole stream refusing.
        """
        answer = stream_raw("$answer", "Thinking...", STREAM_STATUS_PENDING, ts=2_000)
        terminal = stream_raw("$terminal", "the whole answer", STREAM_STATUS_COMPLETED, ts=2_100, replaces="$answer")
        progress = stream_raw("$late", "half an ans", status, ts=2_200, replaces="$answer")
        for source in (answer, terminal):
            event = parse(source)
            await alice.admit(
                inbound_event(ROOM, event, EventKind.MESSAGE, EventClass.ACTIONABLE),
                projected_event(ROOM, event, EventKind.MESSAGE, self_sender=BOT),
            )
        redaction = parse(
            {
                "event_id": "$redact",
                "sender": BOT,
                "origin_server_ts": 2_300,
                "type": "m.room.redaction",
                "redacts": "$terminal",
                "content": {},
            },
        )
        await alice.admit(
            inbound_event(ROOM, redaction, EventKind.REDACTION, EventClass.ACTIONABLE),
            projected_event(ROOM, redaction, EventKind.REDACTION, self_sender=BOT),
        )
        client = FakeClient(events={"$answer": answer}, relations={"$answer": [progress]})

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        await hydrator(alice, client).resolve_refreshes(page.refresh_pending)

        assert await bodies(alice) == ["Thinking..."]
        assert await revisions(alice) == ["$answer"]


def _projected(source: dict[str, Any]) -> ProjectedEvent:
    """Return the projection view of one raw event source."""
    event = parse(source)
    projected = projected_event(ROOM, event, EventKind.MESSAGE, self_sender=BOT)
    assert projected is not None
    return projected


class TestSidecarResolution:
    """A message whose text lives in an attachment is served whole, or not at all."""

    @staticmethod
    def _sidecar_source(event_id: str, preview: str, mxc: str, *, ts: int = 1_000) -> dict[str, Any]:
        """Return one event whose body is a preview of an attached payload."""
        return {
            "event_id": event_id,
            "sender": ALICE,
            "origin_server_ts": ts,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.file",
                "body": preview,
                "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
                "url": mxc,
            },
        }

    @staticmethod
    def _payload(body: str) -> str:
        """Return what the attachment holds: the whole original content."""
        return json.dumps({"msgtype": "m.text", "body": body})

    @staticmethod
    async def _reader(store: PrincipalStore, client: FakeClient) -> ConversationReader:
        """Return a reader over an already-hydrated conversation.

        Hydration is not what these tests are about, and leaving it to run
        would let an empty history walk decide the outcome instead of the
        attachment fetch.
        """
        await store.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(),
            complete=True,
            expected_membership_epoch=await store.membership_epoch(ROOM),
        )
        return ConversationReader(store=store, hydrator=hydrator(store, client))

    async def test_a_strict_read_returns_the_attached_body_not_the_preview(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The exact text the sender wrote reaches the reader.

        Asserting the whole body, and not merely that it differs from the
        preview, is the point: an implementation that resolved to an empty
        string, or to the attachment's JSON envelope rather than its body,
        would satisfy "not the preview" while still handing a model something
        its author never wrote.
        """
        whole = "The answer begins here, and then continues for a very long time indeed."
        source = self._sidecar_source("$long", "The answer beg [Message continues in attached file]", "mxc://s/long")
        await admit_all(alice, [source])
        client = FakeClient(events={"$long": source}, sidecars={"mxc://s/long": self._payload(whole)})
        reader = await self._reader(alice, client)

        page = await reader.read_strict(room_id=ROOM, thread_id=None, limit=10)

        assert [message.content["body"] for message in page.messages] == [whole]
        assert page.refresh_pending == ()

    async def test_a_non_strict_read_reports_the_message_as_missing_rather_than_truncated(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A caller unwilling to wait is told the page is short, not given a stub.

        The preview is a plausible-looking body, so a reader handed it has no
        way to know it is incomplete. Absence is the only honest answer that
        costs nothing.
        """
        source = self._sidecar_source("$long", "preview [Message continues in attached file]", "mxc://s/long")
        await admit_all(alice, [source])
        client = FakeClient(events={"$long": source}, sidecars={"mxc://s/long": self._payload("whole")})
        reader = await self._reader(alice, client)

        page = await reader.read(room_id=ROOM, thread_id=None, limit=10)

        assert page.messages == ()
        assert [request.logical_event_id for request in page.refresh_pending] == ["$long"]
        assert client.downloads == []

    async def test_one_attachment_is_fetched_once_however_often_it_is_read(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Resolving is durable, so a second read costs no download.

        Most answers in this product are attachments, so a resolution that
        repeated per read would put a media fetch behind every prompt.
        """
        source = self._sidecar_source("$long", "preview [Message continues in attached file]", "mxc://s/long")
        await admit_all(alice, [source])
        client = FakeClient(events={"$long": source}, sidecars={"mxc://s/long": self._payload("whole")})
        reader = await self._reader(alice, client)

        await reader.read_strict(room_id=ROOM, thread_id=None, limit=10)
        await reader.read_strict(room_id=ROOM, thread_id=None, limit=10)

        assert client.downloads == ["mxc://s/long"]

    async def test_a_streamed_answer_downloads_only_the_revision_that_won(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Streaming rewrites one message many times before it settles.

        Each intermediate edit replaces the visible revision, so only the last
        one is ever owed a resolution. Downloading per admitted revision would
        fetch an attachment for every edit of every answer.
        """
        original = self._sidecar_source("$m", "p0 [Message continues in attached file]", "mxc://s/v0")
        edits = []
        for index, mxc in enumerate(("mxc://s/v1", "mxc://s/v2", "mxc://s/v3"), start=1):
            edit = self._sidecar_source(f"$e{index}", f"p{index} [continues]", mxc, ts=2_000 + index)
            edit["content"]["m.relates_to"] = {"rel_type": "m.replace", "event_id": "$m"}
            edits.append(edit)
        await admit_all(alice, [original, *edits])
        client = FakeClient(
            events={"$m": original},
            relations={"$m": edits},
            sidecars={f"mxc://s/v{index}": self._payload(f"answer v{index}") for index in range(4)},
        )
        reader = await self._reader(alice, client)

        page = await reader.read_strict(room_id=ROOM, thread_id=None, limit=10)

        assert [message.content["body"] for message in page.messages] == ["answer v3"]
        assert client.downloads == ["mxc://s/v3"]

    async def test_an_unreachable_attachment_keeps_the_read_incomplete(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A failed fetch must not settle the debt with the preview.

        This is the direction that matters. Installing the preview here would
        clear the refresh token, and the truncated body would then look exactly
        like content that had been resolved -- permanently, because nothing
        would ever ask again. Failing loudly leaves it repairable.
        """
        source = self._sidecar_source("$long", "The answer beg [continues]", "mxc://s/gone")
        await admit_all(alice, [source])
        client = FakeClient(events={"$long": source})
        reader = await self._reader(alice, client)

        with pytest.raises(_StaleConversationError):
            await reader.read_strict(room_id=ROOM, thread_id=None, limit=10)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=10)
        assert page.messages == ()
        assert [request.logical_event_id for request in page.refresh_pending] == ["$long"]


class TestPointRefetch:
    """Redacting the visible edit is repaired by asking the server."""

    @staticmethod
    async def _redact_current_edit(store: PrincipalStore) -> None:
        await admit_all(
            store,
            [raw("$m", "first"), raw("$e", "second", ts=2_000, replaces="$m")],
        )
        redaction = nio.Event.parse_event(
            {
                "event_id": "$r",
                "sender": ALICE,
                "origin_server_ts": 3_000,
                "type": "m.room.redaction",
                "redacts": "$e",
                "content": {},
            },
        )
        assert isinstance(redaction, nio.Event)
        await store.admit(
            inbound_event(ROOM, redaction, EventKind.REDACTION, EventClass.ACTIONABLE),
            projected_event(ROOM, redaction, EventKind.REDACTION, self_sender=BOT),
        )

    async def test_the_prior_edit_is_restored_when_the_server_still_has_it(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The prior edit is restored when the server still has it."""
        await admit_all(
            alice,
            [
                raw("$m", "first"),
                raw("$e1", "second", ts=2_000, replaces="$m"),
                raw("$e2", "third", ts=3_000, replaces="$m"),
            ],
        )
        redaction = parse(
            {
                "event_id": "$r",
                "sender": ALICE,
                "origin_server_ts": 4_000,
                "type": "m.room.redaction",
                "redacts": "$e2",
                "content": {},
            },
        )
        await alice.admit(
            inbound_event(ROOM, redaction, EventKind.REDACTION, EventClass.ACTIONABLE),
            projected_event(ROOM, redaction, EventKind.REDACTION, self_sender=BOT),
        )
        client = FakeClient(
            events={"$m": raw("$m", "first")},
            relations={
                "$m": [
                    raw("$e1", "second", ts=2_000, replaces="$m"),
                    raw("$e2", "third", ts=3_000, replaces="$m", redacted=True),
                ],
            },
        )

        assert await hydrator(alice, client).refresh(
            (await refreshes(alice))[0],
        )
        assert await bodies(alice) == ["second"]

    async def test_the_original_is_restored_once_superseded_edits_are_purged(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A server may have already reclaimed the superseded edits.

        Both answers are correct, because both are what every other Matrix
        client in the room sees.
        """
        await self._redact_current_edit(alice)
        client = FakeClient(events={"$m": raw("$m", "first")}, relations={"$m": []})

        assert await hydrator(alice, client).refresh(
            (await refreshes(alice))[0],
        )
        assert await bodies(alice) == ["first"]

    async def test_a_message_the_server_lost_is_removed(self, alice: PrincipalStore) -> None:
        """A message the server lost is removed."""
        await self._redact_current_edit(alice)
        client = FakeClient(events={"$m": raw("$m", "first", redacted=True)}, relations={})

        assert await hydrator(alice, client).refresh(
            (await refreshes(alice))[0],
        )
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=10)
        assert page.messages == ()
        assert page.refresh_pending == ()

    async def test_an_unreachable_server_keeps_the_message_hidden(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An unreachable server keeps the message hidden."""
        await self._redact_current_edit(alice)
        client = FakeClient(events={}, relations={})

        assert not await hydrator(alice, client).refresh(
            (await refreshes(alice))[0],
        )
        assert await bodies(alice) == []
        assert len(await refreshes(alice)) == 1


class TestEncryptedRelations:
    """What a walk of an encrypted room may claim about what it read.

    nio decrypts the responses ``receive_response`` recognizes -- a
    ``/messages`` chunk, a context response, a single fetched event -- and its
    chain has no branch for a relations response. So relations come back
    encrypted no matter how many keys this device holds, and the fixtures here
    model exactly that asymmetry: cleartext for the reads nio has already
    decrypted by the time they return, ciphertext for the relation walk.
    """

    @staticmethod
    def _thread_of_encrypted_replies(*, readable: bool) -> FakeClient:
        """Return a thread whose root is readable and whose replies are encrypted."""
        replies = [encrypted("$reply1", ts=2_000), encrypted("$reply2", ts=3_000)]
        return FakeClient(
            events={"$root": raw("$root", "root", ts=1_000)},
            relations={"$root": replies},
            olm=object(),
            room_keys=(
                {
                    "$reply1": raw("$reply1", "first reply", ts=2_000, thread_id="$root"),
                    "$reply2": raw("$reply2", "second reply", ts=3_000, thread_id="$root"),
                }
                if readable
                else {}
            ),
        )

    async def test_a_thread_whose_replies_could_not_be_read_is_not_complete(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A walk that dropped every reply unread must not call the thread whole.

        This is the difference between a degraded answer and a silently wrong
        one. Hydration runs once per membership, so a thread installed as
        complete here is never walked again for the life of that membership,
        and a strict export accepts root-only as the conversation -- forever,
        without anything ever having failed.
        """
        client = self._thread_of_encrypted_replies(readable=False)

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert await bodies(alice, "$root") == ["root"]
        assert not await alice.conversation_is_complete(room_id=ROOM, thread_id="$root")

    async def test_relations_this_device_holds_keys_for_are_decrypted(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The replies are read, not merely reported missing.

        Marking the walk incomplete keeps a wrong answer from being cached, but
        on its own it would leave every encrypted thread permanently root-only.
        Decrypting is what makes the conversation actually arrive.
        """
        client = self._thread_of_encrypted_replies(readable=True)

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert await bodies(alice, "$root") == ["root", "first reply", "second reply"]
        assert await alice.conversation_is_complete(room_id=ROOM, thread_id="$root")

    async def test_a_refresh_does_not_reinstall_a_body_whose_edits_it_could_not_read(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An unread relation tree is not an empty one.

        An empty relation list is a real answer -- it is how a server that
        already reclaimed the superseded edits says the original is current --
        so reducing over relations that were dropped unread silently reinstalls
        the pre-edit body under the same shape, and clears the refresh token
        that would have brought anyone back to fix it.
        """
        await TestPointRefetch._redact_current_edit(alice)
        client = FakeClient(
            events={"$m": raw("$m", "first")},
            relations={"$m": [encrypted("$e2", ts=4_000)]},
            olm=object(),
        )

        assert not await hydrator(alice, client).refresh(
            (await refreshes(alice))[0],
        )
        assert await bodies(alice) == []
        assert len(await refreshes(alice)) == 1

    async def test_a_message_that_could_not_be_decrypted_is_not_treated_as_deleted(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Unreadable is not gone, and the two arrive here in the same shape.

        A message the server no longer has projects to nothing, and so does one
        this device has no key for. Reading the second as the first drops a row
        for a message every other client in the room can still see.
        """
        await TestPointRefetch._redact_current_edit(alice)
        client = FakeClient(events={"$m": encrypted("$m")}, relations={"$m": []}, olm=object())

        assert not await hydrator(alice, client).refresh(
            (await refreshes(alice))[0],
        )
        assert len(await refreshes(alice)) == 1

    async def test_a_thread_whose_root_could_not_be_read_is_not_complete(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Readable replies do not make up for a root nobody could read.

        The root is the one event a thread walk refuses to spend its window on,
        because a thread that starts at its first reply is missing the message
        the whole thread is about. Losing it to a missing key loses exactly as
        much as losing it to a bound.
        """
        client = FakeClient(
            events={"$root": encrypted("$root", ts=1_000)},
            relations={"$root": [raw("$reply", "a reply", ts=2_000, thread_id="$root")]},
            olm=object(),
        )

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert await bodies(alice, "$root") == ["a reply"]
        assert not await alice.conversation_is_complete(room_id=ROOM, thread_id="$root")

    async def test_a_room_walk_that_could_not_read_a_page_is_not_complete(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Nio decrypts `/messages`, so reaching this means decryption was tried and failed.

        Running out of history is only completeness if the walk could read what
        it ran through.
        """
        client = FakeClient(
            history=[raw("$m1", "readable", ts=1_000), encrypted("$m2", ts=2_000)],
            olm=object(),
        )

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

        assert await bodies(alice) == ["readable"]
        assert not await alice.conversation_is_complete(room_id=ROOM, thread_id=None)


class TestReplyFallback:
    """What a thread-blind client is told this message is replying to."""

    @staticmethod
    def _reader(store: PrincipalStore) -> ConversationReader:
        return ConversationReader(store=store, hydrator=hydrator(store, FakeClient()))

    async def test_the_newest_projected_reply_answers(self, alice: PrincipalStore) -> None:
        """The newest projected reply answers."""
        await admit_all(alice, [raw("$root", "root"), raw("$reply", "reply", ts=2_000, thread_id="$root")])

        assert await self._reader(alice).latest_thread_event_id(room_id=ROOM, thread_id="$root") == "$reply"

    async def test_an_empty_thread_answers_with_its_root(self, alice: PrincipalStore) -> None:
        """An empty thread answers with its root."""
        await admit_all(alice, [raw("$root", "root")])

        assert await self._reader(alice).latest_thread_event_id(room_id=ROOM, thread_id="$root") == "$root"

    async def test_a_caller_that_just_sent_is_believed_over_the_projection(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Reads after a send are echo-ordered, so the sender holds the newer fact."""
        await admit_all(alice, [raw("$root", "root"), raw("$reply", "reply", ts=2_000, thread_id="$root")])

        answer = await self._reader(alice).latest_thread_event_id(
            room_id=ROOM,
            thread_id="$root",
            known_latest_thread_event_id="$just-sent",
        )

        assert answer == "$just-sent"

    async def test_a_known_event_does_not_invent_a_fallback_outside_a_thread(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A room-level send has no thread relation to hang a fallback on."""
        answer = await self._reader(alice).latest_thread_event_id(
            room_id=ROOM,
            thread_id=None,
            known_latest_thread_event_id="$just-sent",
        )

        assert answer is None

    async def test_a_caller_replying_deliberately_keeps_its_own_target(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An explicit reply target outranks both the projection and a known send."""
        await admit_all(alice, [raw("$root", "root"), raw("$reply", "reply", ts=2_000, thread_id="$root")])
        reader = self._reader(alice)

        assert (
            await reader.latest_thread_event_id(
                room_id=ROOM,
                thread_id="$root",
                reply_to_event_id="$chosen",
                known_latest_thread_event_id="$just-sent",
            )
            is None
        )
        assert (
            await reader.latest_thread_event_id(
                room_id=ROOM,
                thread_id="$root",
                existing_event_id="$being-edited",
                known_latest_thread_event_id="$just-sent",
            )
            is None
        )


class TestReadModes:
    """The two callers, and what each is allowed to see."""

    @staticmethod
    async def _hidden_message(store: PrincipalStore) -> None:
        await admit_all(store, [raw("$m", "first"), raw("$e", "deleted", ts=2_000, replaces="$m")])
        redaction = parse(
            {
                "event_id": "$r",
                "sender": ALICE,
                "origin_server_ts": 3_000,
                "type": "m.room.redaction",
                "redacts": "$e",
                "content": {},
            },
        )
        await store.admit(
            inbound_event(ROOM, redaction, EventKind.REDACTION, EventClass.ACTIONABLE),
            projected_event(ROOM, redaction, EventKind.REDACTION, self_sender=BOT),
        )

    async def test_a_non_strict_read_omits_rather_than_waits(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A non strict read omits rather than waits."""
        await self._hidden_message(alice)
        client = FakeClient(events={}, relations={})
        reader = ConversationReader(store=alice, hydrator=hydrator(alice, client))

        page = await reader.read(room_id=ROOM, thread_id=None, limit=10)

        assert page.messages == ()
        assert client.relation_calls == 0

    async def test_a_strict_read_repairs_before_returning(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A strict read repairs before returning."""
        await self._hidden_message(alice)
        client = FakeClient(events={"$m": raw("$m", "first")}, relations={"$m": []})
        await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(),
            complete=True,
            expected_membership_epoch=await alice.membership_epoch(ROOM),
        )
        reader = ConversationReader(store=alice, hydrator=hydrator(alice, client))

        page = await reader.read_strict(room_id=ROOM, thread_id=None, limit=10)

        assert [m.content["body"] for m in page.messages] == ["first"]

    async def test_a_strict_read_fails_rather_than_omitting(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Prompt assembly cannot tell an omission from an absence."""
        await self._hidden_message(alice)
        client = FakeClient(events={}, relations={})
        await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(),
            complete=True,
            expected_membership_epoch=await alice.membership_epoch(ROOM),
        )
        reader = ConversationReader(store=alice, hydrator=hydrator(alice, client))

        with pytest.raises(_StaleConversationError):
            await reader.read_strict(room_id=ROOM, thread_id=None, limit=10)


class TestUnreadHistory:
    """Only hydration proves a conversation has nothing behind it."""

    def _reader(self, store: PrincipalStore) -> ConversationReader:
        return ConversationReader(store=store, hydrator=hydrator(store, FakeClient()))

    async def test_an_unhydrated_conversation_cannot_prove_it_is_fresh(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A room's first admitted event says nothing about what preceded it.

        This is what a cutover looks like from inside the journal: a room with
        years of Matrix history behind it, one row in the table, and nothing
        local that distinguishes it from a room created a second ago. Counting
        rows answers "fresh" here and only stops being wrong once a second
        event lands, which is one turn after the answer was needed.
        """
        await admit_all(alice, [raw("$first", "first event this journal ever saw")])

        assert await self._reader(alice).may_have_unread_history(room_id=ROOM, thread_id=None)

    async def test_a_hydrated_conversation_is_proven_fresh(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A walk that ran is the evidence, so a hydrated conversation is not degraded.

        The mirror of the case above: without it, "may have unread history"
        could be hard-coded true and every dispatch read would degrade forever.
        """
        await admit_all(alice, [raw("$first", "first event this journal ever saw")])
        await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(),
            complete=True,
            expected_membership_epoch=await alice.membership_epoch(ROOM),
        )

        assert not await self._reader(alice).may_have_unread_history(room_id=ROOM, thread_id=None)

    async def test_hydration_is_scoped_to_one_conversation(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Hydrating the room says nothing about a thread inside it."""
        await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(),
            complete=True,
            expected_membership_epoch=await alice.membership_epoch(ROOM),
        )

        assert await self._reader(alice).may_have_unread_history(room_id=ROOM, thread_id="$root")


class TestWindowTruncation:
    """A bounded page is not the whole conversation."""

    async def test_a_page_with_more_behind_it_is_not_full_history(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A conversation longer than the read window reports itself truncated.

        Consumers do not all just render what they get. Thread summaries count
        the messages returned and record that count as the size of the thread,
        so a suffix reported as complete is written down as the whole history
        and the next pass compares against it.
        """
        await admit_all(alice, [raw(f"$m{index}", f"message {index}", ts=1_000 + index) for index in range(5)])
        client = FakeClient()
        reader = ConversationReader(store=alice, hydrator=hydrator(alice, client))
        await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(),
            complete=True,
            expected_membership_epoch=await alice.membership_epoch(ROOM),
        )

        page = await reader.read_strict(room_id=ROOM, thread_id=None, limit=3)
        history = projected_thread_history(page, complete=True)

        assert len(history) == 3
        assert page.next_cursor is not None
        assert not history.is_full_history

    async def test_a_page_that_reaches_the_start_is_full_history(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Nothing behind the page means the page is the conversation.

        The mirror of the case above: without it, "not full history" could be
        hard-coded and still pass.
        """
        await admit_all(alice, [raw(f"$m{index}", f"message {index}", ts=1_000 + index) for index in range(3)])
        client = FakeClient()
        reader = ConversationReader(store=alice, hydrator=hydrator(alice, client))
        await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(),
            complete=True,
            expected_membership_epoch=await alice.membership_epoch(ROOM),
        )

        page = await reader.read_strict(room_id=ROOM, thread_id=None, limit=10)
        history = projected_thread_history(page, complete=True)

        assert len(history) == 3
        assert page.next_cursor is None
        assert history.is_full_history


class TestRefreshStarvation:
    """A read repairs what it asked about, not the newest debts in the room."""

    async def test_an_older_debt_is_repaired_behind_many_unrepairable_newer_ones(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Re-selecting the conversation's newest debts starves the page's own.

        The selection this replaces returned newest-first and stopped at a
        fixed number. A page holding one older resolvable message behind
        enough newer unrepairable ones would spend every read retrying the
        newer ones and never once attempt the message it was asked for, so
        that message stayed hidden for good.
        """
        wanted = TestSidecarResolution._sidecar_source("$wanted", "old [continues]", "mxc://s/wanted", ts=1_000)
        unrepairable = [
            TestSidecarResolution._sidecar_source(
                f"$new{index}",
                "new [continues]",
                f"mxc://s/gone{index}",
                ts=2_000 + index,
            )
            for index in range(70)
        ]
        await admit_all(alice, [wanted, *unrepairable])
        client = FakeClient(
            events={"$wanted": wanted, **{f"$new{index}": source for index, source in enumerate(unrepairable)}},
            # Only the older message's attachment exists; every newer one fails.
            sidecars={"mxc://s/wanted": TestSidecarResolution._payload("the older answer")},
        )
        reader = ConversationReader(store=alice, hydrator=hydrator(alice, client))
        await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(),
            complete=True,
            expected_membership_epoch=await alice.membership_epoch(ROOM),
        )

        with pytest.raises(_StaleConversationError):
            await reader.read_strict(room_id=ROOM, thread_id=None, limit=100)

        assert "mxc://s/wanted" in client.downloads, "the requested message was never attempted"
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=100)
        assert [message.content["body"] for message in page.messages] == ["the older answer"]


class TestLatestSenderMessage:
    """What a hook is told one sender's newest visible message is."""

    @staticmethod
    def _reader(store: PrincipalStore, client: FakeClient | None = None) -> ConversationReader:
        return ConversationReader(store=store, hydrator=hydrator(store, client or FakeClient()))

    async def test_the_newest_message_from_that_sender_answers(self, alice: PrincipalStore) -> None:
        """Someone else speaking last must not hide the sender that was asked about."""
        await admit_all(
            alice,
            [
                raw("$a1", "older", ts=1_000),
                raw("$a2", "newer", ts=2_000),
                raw("$b1", "not mine", sender=BOB, ts=3_000),
            ],
        )

        snapshot = await latest_agent_message_snapshot(
            self._reader(alice),
            room_id=ROOM,
            thread_id=None,
            sender=ALICE,
        )

        assert snapshot == AgentMessageSnapshot(
            content={"msgtype": "m.text", "body": "newer"},
            origin_server_ts=2_000,
        )

    async def test_an_edited_message_answers_with_the_edit(self, alice: PrincipalStore) -> None:
        """A streamed answer is read through its edits, so the revision is what counts."""
        await admit_all(
            alice,
            [raw("$m", "half", ts=1_000), raw("$e", "complete", ts=5_000, replaces="$m")],
        )

        snapshot = await latest_agent_message_snapshot(
            self._reader(alice),
            room_id=ROOM,
            thread_id=None,
            sender=ALICE,
        )

        assert snapshot is not None
        assert snapshot.content["body"] == "complete"
        assert snapshot.origin_server_ts == 5_000

    async def test_a_threaded_message_does_not_answer_a_room_scope_read(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Room scope is the unthreaded conversation, not everything in the room."""
        await admit_all(alice, [raw("$root", "root"), raw("$reply", "in thread", ts=2_000, thread_id="$root")])
        reader = self._reader(alice)

        room_scope = await latest_agent_message_snapshot(
            reader,
            room_id=ROOM,
            thread_id=None,
            sender=ALICE,
        )
        thread_scope = await latest_agent_message_snapshot(
            reader,
            room_id=ROOM,
            thread_id="$root",
            sender=ALICE,
        )

        assert room_scope is not None
        assert room_scope.content["body"] == "root"
        assert thread_scope is not None
        assert thread_scope.content["body"] == "in thread"

    async def test_a_silent_sender_answers_with_nothing(self, alice: PrincipalStore) -> None:
        """A sender with nothing visible has no snapshot rather than someone else's."""
        await admit_all(alice, [raw("$b1", "theirs", sender=BOB)])

        assert (
            await latest_agent_message_snapshot(
                self._reader(alice),
                room_id=ROOM,
                thread_id=None,
                sender=ALICE,
            )
            is None
        )

    async def test_a_message_awaiting_refetch_is_never_served(self, alice: PrincipalStore) -> None:
        """The redacted revision is content the sender deleted; absence is the honest answer."""
        await TestReadModes._hidden_message(alice)

        assert (
            await latest_agent_message_snapshot(
                self._reader(alice),
                room_id=ROOM,
                thread_id=None,
                sender=ALICE,
            )
            is None
        )

    async def test_the_read_never_reaches_the_homeserver(self, alice: PrincipalStore) -> None:
        """A hook must not block on Matrix, so an unhydrated conversation answers locally."""
        await admit_all(alice, [raw("$m", "only local", ts=1_000)])
        client = FakeClient(events={"$m": raw("$m", "only local")}, history=[raw("$old", "older", ts=500)])

        snapshot = await latest_agent_message_snapshot(
            self._reader(alice, client),
            room_id=ROOM,
            thread_id=None,
            sender=ALICE,
        )

        assert snapshot is not None
        assert snapshot.content["body"] == "only local"
        assert client.history_pages == 0
        assert client.relation_calls == 0

    async def test_a_sender_older_than_the_window_is_not_searched_for(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The read is a bounded status probe, not a walk back through the conversation."""
        await admit_all(
            alice,
            [
                raw("$mine", "mine", ts=1_000),
                *(
                    raw(f"$b{index}", f"theirs {index}", sender=BOB, ts=2_000 + index)
                    for index in range(_LATEST_SENDER_MESSAGE_WINDOW_MESSAGES)
                ),
            ],
        )

        assert (
            await latest_agent_message_snapshot(
                self._reader(alice),
                room_id=ROOM,
                thread_id=None,
                sender=ALICE,
            )
            is None
        )

    async def test_a_message_from_before_this_run_still_answers(self, alice: PrincipalStore) -> None:
        """No runtime-start bound is applied here, and that is the decision, not an omission.

        The cache accessor this replaced refused a room-scope answer older than
        the current process, comparing the row's *cache* time against
        ``runtime_started_at``. The projection records what the server said,
        not when this process learned it, so that rule cannot be carried over:
        reinstating it against ``origin_server_ts`` would be a different rule
        wearing the same name, and it would blank this read for every
        conversation that predates a restart.

        A greet-once hook therefore still sees the greeting it posted before
        the restart, which is what "have I already spoken here" should mean.
        A hook that genuinely wants the old boundary can still apply it --
        ``HookContext.runtime_started_at`` is the timestamp and the snapshot
        carries ``origin_server_ts`` -- so the choice belongs to the plugin
        rather than to this read.
        """
        await admit_all(alice, [raw("$greeted", "hello, I am here", ts=1_000)])

        snapshot = await latest_agent_message_snapshot(
            self._reader(alice),
            room_id=ROOM,
            thread_id=None,
            sender=ALICE,
        )

        assert snapshot == AgentMessageSnapshot(
            content={"msgtype": "m.text", "body": "hello, I am here"},
            origin_server_ts=1_000,
        )
