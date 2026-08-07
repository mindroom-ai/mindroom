"""Provenance mapping, durable admission, and pending-event execution."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

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
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.event_journal import EventClass, EventKind, SemanticConsumer, SettlementOutcome, VisibleMessage
from mindroom.journal_dispatch import JournalCallbacks, JournalDispatcher
from mindroom.matrix.client_delivery import build_edit_event_content
from mindroom.matrix.conversation_hydration import _projected_from_event
from mindroom.matrix.journal_ingress import (
    JournalCorruptionError,
    JournalIngress,
    _event_class_for,
    _event_kind,
    inbound_event,
    parse_journal_event,
    projected_event,
)
from mindroom.pending_event_worker import _BATCH_SIZE, PendingEventWorker

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sized

    from mindroom.event_journal import EventJournalStore, JournalEvent, PrincipalStore

pytestmark = pytest.mark.asyncio

ROOM = "!room:example.org"
ALICE = "@alice:example.org"
BOT = "@mindroom_general:example.org"


@pytest.fixture
def alice(journal_store: EventJournalStore) -> PrincipalStore:
    """Return one bound principal view."""
    return journal_store.principal("agent@alice")


def text_event(
    event_id: str,
    body: str = "hello",
    *,
    thread_id: str | None = None,
    ts: int = 1_000,
) -> nio.Event:
    """Return a parsed text message event."""
    content: dict[str, Any] = {"msgtype": "m.text", "body": body}
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    event = nio.Event.parse_event(
        {
            "event_id": event_id,
            "sender": ALICE,
            "origin_server_ts": ts,
            "type": "m.room.message",
            "content": content,
        },
    )
    assert isinstance(event, nio.Event)
    return event


def bot_event(event_id: str, body: str = "the answer", *, ts: int = 1_100) -> nio.Event:
    """Return this bot's own message as it comes back on the sync timeline."""
    event = nio.Event.parse_event(
        {
            "event_id": event_id,
            "sender": BOT,
            "origin_server_ts": ts,
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": body},
        },
    )
    assert isinstance(event, nio.Event)
    return event


def image_event(
    event_id: str,
    body: str = "photo.png",
    *,
    ts: int = 1_000,
    encrypted: bool = False,
) -> nio.Event:
    """Return a parsed image message, optionally with its decryption keys."""
    content: dict[str, Any] = {
        "msgtype": "m.image",
        "body": body,
        "info": {"mimetype": "image/png", "size": 4_096, "w": 64, "h": 64},
    }
    if encrypted:
        content["file"] = {
            "url": f"mxc://example.org/{event_id.lstrip('$')}",
            "key": {
                "k": "cipher-key-material",
                "alg": "A256CTR",
                "ext": True,
                "key_ops": ["encrypt", "decrypt"],
                "kty": "oct",
            },
            "iv": "initialization-vector",
            "hashes": {"sha256": "content-hash"},
            "v": "v2",
        }
    else:
        content["url"] = f"mxc://example.org/{event_id.lstrip('$')}"
    source = {
        "event_id": event_id,
        "sender": ALICE,
        "origin_server_ts": ts,
        "type": "m.room.message",
        "content": content,
    }
    event = nio.RoomMessage.parse_decrypted_event(source) if encrypted else nio.Event.parse_event(source)
    assert isinstance(event, nio.Event)
    return event


def redaction_event(event_id: str, redacts: str, *, ts: int = 2_000) -> nio.Event:
    """Return a parsed redaction event."""
    event = nio.Event.parse_event(
        {
            "event_id": event_id,
            "sender": ALICE,
            "origin_server_ts": ts,
            "type": "m.room.redaction",
            "redacts": redacts,
            "content": {},
        },
    )
    assert isinstance(event, nio.Event)
    return event


def reaction_event(event_id: str, *, target: str = "$target", key: str = "OK") -> nio.Event:
    """Return a parsed annotation, whose callback finishes when it returns."""
    event = nio.Event.parse_event(
        {
            "event_id": event_id,
            "sender": ALICE,
            "origin_server_ts": 1_000,
            "type": "m.reaction",
            "content": {"m.relates_to": {"rel_type": "m.annotation", "event_id": target, "key": key}},
        },
    )
    assert isinstance(event, nio.Event)
    return event


def room() -> nio.MatrixRoom:
    """Return a minimal joined room."""
    return nio.MatrixRoom(ROOM, ALICE)


PLACEHOLDER_ID = "$placeholder"
PLACEHOLDER_BODY = "Thinking..."


def placeholder_event(*, ts: int = 1_000, msgtype: str = "m.notice") -> nio.Event:
    """Return the visible message a streamed answer starts life as.

    ``m.notice``, because that is what the runtime sends: every `pending` and
    `streaming` frame is a notice so Matrix suppresses it before evaluating
    mention rules, and only the terminal frame reverts to ``m.text``
    (`streaming.py`, `_prepare_delivery_from_snapshot`).

    This fixture used to hard-code ``m.text`` while claiming it was built the
    way the runtime builds it. It was not, and the difference was the whole
    bug: a notice is a sibling of `RoomMessageText` in nio, not a subclass, so
    the real placeholder was never admitted and every streamed answer's
    terminal edit had no original to reduce onto.
    """
    event = nio.Event.parse_event(
        {
            "event_id": PLACEHOLDER_ID,
            "sender": BOT,
            "origin_server_ts": ts,
            "type": "m.room.message",
            "content": {
                "msgtype": msgtype,
                "body": PLACEHOLDER_BODY,
                STREAM_STATUS_KEY: STREAM_STATUS_PENDING,
            },
        },
    )
    assert isinstance(event, nio.Event)
    return event


def stream_event(
    event_id: str,
    body: str,
    status: str,
    *,
    replaces: str,
    sender: str = BOT,
    msgtype: str = "m.text",
    ts: int = 1_100,
) -> nio.Event:
    """Return one revision of a streamed answer, in the real edit envelope.

    Uses the production builder rather than a hand-written shape, so a change
    to where the stream status lands inside an edit breaks these tests instead
    of quietly making them test nothing.
    """
    content = build_edit_event_content(
        event_id=replaces,
        new_content={"msgtype": msgtype, "body": body},
        new_text=body,
        extra_content={STREAM_STATUS_KEY: status},
    )
    event = nio.Event.parse_event(
        {
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": ts,
            "type": "m.room.message",
            "content": content,
        },
    )
    assert isinstance(event, nio.Event)
    return event


class TestProvenanceMapping:
    """nio owns provenance; MindRoom owns only what it means."""

    @pytest.mark.parametrize(
        ("provenance", "expected"),
        [
            (nio.TimelineEventProvenance.LIVE, EventClass.ACTIONABLE),
            (nio.TimelineEventProvenance.RECOVERED, EventClass.ACTIONABLE),
            (nio.TimelineEventProvenance.HISTORY, EventClass.CONTEXT_ONLY),
        ],
    )
    async def test_provenance_decides_whether_work_may_start(
        self,
        provenance: nio.TimelineEventProvenance,
        expected: EventClass,
    ) -> None:
        """Provenance decides whether work may start."""
        assert _event_class_for(provenance, text_event("$m", "hi")) is expected

    async def test_every_provenance_is_mapped(self) -> None:
        """A new provenance must not silently default to actionable."""
        for provenance in nio.TimelineEventProvenance:
            assert _event_class_for(provenance, text_event("$m", "hi")) in EventClass


class TestEventKinds:
    """One event carries at most one semantic purpose."""

    async def test_a_text_message_is_a_message(self) -> None:
        """A text message is a message."""
        assert _event_kind(text_event("$m")) is EventKind.MESSAGE

    async def test_a_redaction_is_a_redaction(self) -> None:
        """A redaction is a redaction."""
        assert _event_kind(redaction_event("$r", "$m")) is EventKind.REDACTION

    async def test_an_unrelated_event_has_no_kind(self) -> None:
        """An unrelated event has no kind."""
        event = nio.Event.parse_event(
            {
                "event_id": "$topic",
                "sender": ALICE,
                "origin_server_ts": 1,
                "type": "m.room.topic",
                "state_key": "",
                "content": {"topic": "hi"},
            },
        )
        assert isinstance(event, nio.Event)
        assert _event_kind(event) is None

    @pytest.mark.parametrize(
        ("msgtype", "extra_content"),
        [
            ("m.text", {}),
            ("m.emote", {}),
            ("m.notice", {}),
            ("m.image", {"url": "mxc://example.org/i"}),
            ("m.file", {"url": "mxc://example.org/f"}),
            ("m.video", {"url": "mxc://example.org/v"}),
            ("m.audio", {"url": "mxc://example.org/a"}),
        ],
    )
    async def test_every_room_message_admission_matches_what_hydration_projects(
        self,
        msgtype: str,
        extra_content: dict[str, str],
    ) -> None:
        """Watching a conversation and rebuilding it must agree on what is in it.

        Hydration admits any `m.room.message`, so admission has to as well. It
        did not: the rules enumerated msgtypes, and each one left out was found
        only after shipping -- notices first, then emotes. A user sending
        `/me waves` was journaled live as nothing at all, and then appeared out
        of nowhere the first time the thread was rebuilt.

        Parametrized over msgtypes rather than asserting the base class so the
        two implementations are compared against each other rather than against
        the same assumption twice.
        """
        source = {
            "event_id": f"$msg-{msgtype}",
            "sender": ALICE,
            "origin_server_ts": 1,
            "type": "m.room.message",
            "content": {"msgtype": msgtype, "body": "body", **extra_content},
        }
        event = nio.Event.parse_event(source)
        assert not isinstance(event, nio.BadEvent), f"{msgtype} fixture is malformed"

        projected = _projected_from_event(ROOM, event, self_sender="@someone-else:localhost")

        assert (_event_kind(event) is not None) == (projected is not None)


class TestAdmissionAdapter:
    """The translation from a nio event to a durable row."""

    async def test_a_threaded_message_lands_in_its_thread(self) -> None:
        """A threaded message lands in its thread."""
        inbound = inbound_event(
            ROOM,
            text_event("$m", thread_id="$root"),
            EventKind.MESSAGE,
            EventClass.ACTIONABLE,
        )
        assert inbound.thread_id == "$root"

    async def test_an_unthreaded_message_has_no_thread(self) -> None:
        """An unthreaded message has no thread."""
        inbound = inbound_event(ROOM, text_event("$m"), EventKind.MESSAGE, EventClass.ACTIONABLE)
        assert inbound.thread_id is None

    async def test_a_reaction_does_not_touch_the_projection(self) -> None:
        """A reaction does not touch the projection."""
        event = nio.Event.parse_event(
            {
                "event_id": "$reaction",
                "sender": ALICE,
                "origin_server_ts": 1,
                "type": "m.reaction",
                "content": {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$m", "key": "x"}},
            },
        )
        assert isinstance(event, nio.Event)
        assert projected_event(ROOM, event, EventKind.REACTION, self_sender=BOT) is None

    async def test_a_redaction_projects_onto_its_target(self) -> None:
        """A redaction projects onto its target."""
        projected = projected_event(ROOM, redaction_event("$r", "$m"), EventKind.REDACTION, self_sender=BOT)
        assert projected is not None
        assert projected.redacts_event_id == "$m"

    async def test_a_redaction_without_a_target_never_reaches_the_journal(self) -> None:
        """Nio's schema requires the target, so such an event is never parsed.

        Worth pinning: the projection reads the typed ``redacts`` attribute
        rather than probing the source, and that is only safe while nio refuses
        to produce a redaction with no target.
        """
        event = nio.Event.parse_event(
            {
                "event_id": "$r",
                "sender": ALICE,
                "origin_server_ts": 1,
                "type": "m.room.redaction",
                "content": {},
            },
        )
        assert not isinstance(event, nio.RedactionEvent)
        assert _event_kind(event) is not EventKind.REDACTION


def sidecar_event(event_id: str, preview: str, mxc: str, *, ts: int = 5_000) -> nio.Event:
    """Return a message whose real body lives in a v2 JSON sidecar."""
    event = nio.Event.parse_event(
        {
            "event_id": event_id,
            "sender": BOT,
            "origin_server_ts": ts,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.file",
                "body": preview,
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
                "url": mxc,
            },
        },
    )
    assert isinstance(event, nio.Event)
    return event


class TestSidecarContent:
    """A message too large for one Matrix event never reaches a prompt truncated."""

    async def test_an_unresolved_sidecar_is_owed_rather_than_served(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Most agent answers exceed the event size limit and live in a sidecar.

        The event itself says only "[Message continues in attached file]".
        Storing that would feed a model a placeholder in place of its own
        previous answer, for the majority of its own history, and no reader
        could tell by looking that the body it got was a stub.

        So the message is reported as owing a resolution instead, which is the
        same shape a redaction leaves behind, and the readers that already know
        how to wait for one repair it.
        """
        preview = "The answer beg [Message continues in attached file]"
        event = sidecar_event("$long", preview, "mxc://server/long-answer")
        await alice.admit(
            inbound_event(ROOM, event, EventKind.MESSAGE, EventClass.ACTIONABLE),
            projected_event(ROOM, event, EventKind.MESSAGE, self_sender=BOT),
        )

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=10)

        assert page.messages == (), "the projection served an unresolved sidecar as a message"
        assert [request.logical_event_id for request in page.refresh_pending] == ["$long"]

    async def test_an_ordinary_message_alongside_it_is_still_served(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Owing one resolution must not hide the rest of the conversation.

        Pins that the sidecar rule is about the one message whose text is
        missing. A rule that withheld the whole page would be indistinguishable
        from the correct one in a test that only ever admits a sidecar.
        """
        plain = text_event("$plain", "a short answer", ts=4_000)
        sidecar = sidecar_event("$long", "truncated [Message continues in attached file]", "mxc://server/long")
        for event in (plain, sidecar):
            await alice.admit(
                inbound_event(ROOM, event, EventKind.MESSAGE, EventClass.ACTIONABLE),
                projected_event(ROOM, event, EventKind.MESSAGE, self_sender=BOT),
            )

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=10)

        assert [message.content["body"] for message in page.messages] == ["a short answer"]
        assert [request.logical_event_id for request in page.refresh_pending] == ["$long"]

    async def test_resolved_content_is_stored_whole(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Content carrying no sidecar reference is the resolved form.

        This is what makes the rule self-clearing: the payload inside the
        attachment has no sidecar metadata of its own, so storing it settles
        the debt without anything having to remember to clear a flag.
        """
        whole = "The answer begins here and runs on for many thousands of characters."
        event = text_event("$long", whole, ts=5_000)
        await alice.admit(
            inbound_event(ROOM, event, EventKind.MESSAGE, EventClass.ACTIONABLE),
            projected_event(ROOM, event, EventKind.MESSAGE, self_sender=BOT),
        )

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=10)

        assert [message.content["body"] for message in page.messages] == [whole]
        assert page.refresh_pending == ()


class TestEchoOrdering:
    """The sync echo is the route this bot's own answers take into a conversation.

    These pin the guarantee the outbound path relies on instead of writing its
    own answers into the projection: an answer and any later user message reach
    this bot on one server-ordered timeline, so a turn resolved after the user's
    message already sees the answer that preceded it.
    """

    async def test_an_answer_reaches_the_conversation_through_its_echo(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A self-authored echo is projected like any other timeline event."""
        echo = bot_event("$answer", "the answer")
        await alice.admit(
            inbound_event(ROOM, echo, EventKind.MESSAGE, EventClass.ACTIONABLE),
            projected_event(ROOM, echo, EventKind.MESSAGE, self_sender=BOT),
        )

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=10)

        assert [message.logical_event_id for message in page.messages] == ["$answer"]
        assert page.messages[0].sender == BOT

    async def test_a_later_user_turn_sees_the_answer_that_preceded_it(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Timeline order puts the echo before the message that follows it."""
        for event in (
            text_event("$ask", "question", ts=1_000),
            bot_event("$answer", "the answer", ts=1_100),
            text_event("$follow_up", "and then?", ts=1_200),
        ):
            await alice.admit(
                inbound_event(ROOM, event, EventKind.MESSAGE, EventClass.ACTIONABLE),
                projected_event(ROOM, event, EventKind.MESSAGE, self_sender=BOT),
            )

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=10)

        assert [message.logical_event_id for message in page.messages] == [
            "$ask",
            "$answer",
            "$follow_up",
        ]

    async def test_one_sync_carrying_both_still_orders_the_answer_first(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Batching an echo and the next message together changes nothing.

        The ordering comes from the server timestamps the server assigned, not
        from how many sync responses the events were split across.
        """
        batch = (
            bot_event("$answer", "the answer", ts=2_100),
            text_event("$follow_up", "and then?", ts=2_200),
        )
        await asyncio.gather(
            *(
                alice.admit(
                    inbound_event(ROOM, event, EventKind.MESSAGE, EventClass.ACTIONABLE),
                    projected_event(ROOM, event, EventKind.MESSAGE, self_sender=BOT),
                )
                for event in batch
            ),
        )

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=10)

        assert [message.logical_event_id for message in page.messages] == ["$answer", "$follow_up"]

    @pytest.mark.parametrize(
        "provenance",
        [nio.TimelineEventProvenance.LIVE, nio.TimelineEventProvenance.RECOVERED],
    )
    async def test_ingress_admits_this_bot_s_own_echo(
        self,
        alice: PrincipalStore,
        provenance: nio.TimelineEventProvenance,
    ) -> None:
        """Admission is decided by provenance, never by who sent the event.

        The tests below reach the store directly, which would keep passing even
        if ingress learned to discard self-authored events on the way in. This
        one goes through `_admit` so that a sender filter added there fails
        here, because the echo route depends on there not being one.
        """
        ingress = JournalIngress(store=alice, self_sender=BOT)

        await ingress._admit(room(), bot_event("$answer", "the answer"), provenance)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=10)
        assert [message.logical_event_id for message in page.messages] == ["$answer"]
        assert page.messages[0].sender == BOT
        # Admitted as actionable like any other live event; the echo is dropped
        # later, by ingress validation, not by refusing to record it.
        assert [event.event_id for event in await alice.pending()] == ["$answer"]

    async def test_a_recovered_answer_orders_with_the_live_message_after_it(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A gap-recovered echo still lands before the live message that follows."""
        recovered = bot_event("$answer", "the answer", ts=3_100)
        await alice.admit(
            inbound_event(ROOM, recovered, EventKind.MESSAGE, EventClass.ACTIONABLE),
            projected_event(ROOM, recovered, EventKind.MESSAGE, self_sender=BOT),
        )
        live = text_event("$follow_up", "and then?", ts=3_200)
        await alice.admit(
            inbound_event(ROOM, live, EventKind.MESSAGE, EventClass.ACTIONABLE),
            projected_event(ROOM, live, EventKind.MESSAGE, self_sender=BOT),
        )

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=10)

        assert [message.logical_event_id for message in page.messages] == ["$answer", "$follow_up"]


class TestStreamingProgressIsTransport:
    """A streamed answer is one message, however many edits it took to write.

    A progress edit is how the answer travels, not something the conversation
    gained: the room still holds one reply, whose body is whatever the stream
    settled on. Reducing every progress echo would rewrite that row once per
    edit and arrive where it was going anyway.

    MindRoom sends in-progress updates as ``m.notice`` so Matrix suppresses
    the push notification each edit would otherwise fire, and only the terminal
    frame reverts to ``m.text``. A notice is not a kind journal admission owns,
    so recognising this bot's own frames is what puts the placeholder in the
    conversation for that terminal edit to land on. Most tests here use
    ``m.text`` frames so the transport rule is exercised on its own; the last
    two run the exact sequence production sends.
    """

    @staticmethod
    async def _admit_live(ingress: JournalIngress, *events: nio.Event) -> None:
        for event in events:
            await ingress._admit(room(), event, nio.TimelineEventProvenance.LIVE)

    @staticmethod
    async def _one_visible(store: PrincipalStore) -> VisibleMessage:
        page = await store.read_conversation(room_id=ROOM, thread_id=None, limit=10)
        assert len(page.messages) == 1, f"expected one logical message, got {len(page.messages)}"
        return page.messages[0]

    @pytest.mark.parametrize("status", [STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING])
    async def test_this_bots_progress_edit_leaves_the_placeholder_on_screen(
        self,
        alice: PrincipalStore,
        status: str,
    ) -> None:
        """A self-authored in-flight revision must not move the visible row."""
        ingress = JournalIngress(store=alice, self_sender=BOT)

        await self._admit_live(
            ingress,
            placeholder_event(),
            stream_event("$progress", "half an ans", status, replaces=PLACEHOLDER_ID, ts=1_100),
        )

        visible = await self._one_visible(alice)
        assert visible.content["body"] == PLACEHOLDER_BODY
        assert visible.revision_event_id == PLACEHOLDER_ID

    async def test_a_thread_summary_reads_the_same_watched_as_hydrated(
        self,
        alice: PrincipalStore,
    ) -> None:
        """One conversation must not depend on how the bot came to see it.

        Hydration accepts any `m.room.message`, notices included
        (`conversation_hydration._projected_from_event`). Live admission used
        to match `RoomMessageText` alone, so a thread summary -- sent as
        `m.notice` -- was in a hydrated conversation and absent from a watched
        one. The prompt then differed by nothing but whether the bot had
        restarted, which is the divergence the projection exists to remove.
        """
        ingress = JournalIngress(store=alice, self_sender=BOT)
        summary = nio.Event.parse_event(
            {
                "event_id": "$summary",
                "sender": BOT,
                "origin_server_ts": 1_000,
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.notice",
                    "body": "So far: they asked about X.",
                    "io.mindroom.thread_summary": {"message_count": 12},
                },
            },
        )
        assert isinstance(summary, nio.Event)

        await self._admit_live(ingress, summary)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=10)
        assert [message.logical_event_id for message in page.messages] == ["$summary"]
        # Present in the conversation, and never a turn to answer.
        assert await alice.pending() == ()

    async def test_someone_elses_notice_is_not_treated_as_our_stream(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A stream status on someone else's notice buys them nothing.

        Notices are admitted now -- the conversation contains them and
        hydration always kept them -- but only ever as context. That is what
        makes the status key safe to ignore: it is an ordinary content field
        any member can set, so if it could promote a notice to work, decorating
        one would be a way to make the bot answer something it should not.
        """
        ingress = JournalIngress(store=alice, self_sender=BOT)
        foreign = nio.Event.parse_event(
            {
                "event_id": "$theirs",
                "sender": ALICE,
                "origin_server_ts": 1_000,
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.notice",
                    "body": "not mine",
                    STREAM_STATUS_KEY: STREAM_STATUS_PENDING,
                },
            },
        )
        assert isinstance(foreign, nio.Event)

        await self._admit_live(ingress, foreign)

        assert ingress.admission_kind(foreign) is EventKind.MESSAGE
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=10)
        assert [message.logical_event_id for message in page.messages] == ["$theirs"]
        # Admitted, projected, and never work.
        assert await alice.pending() == ()

    async def test_the_production_stream_sequence_leaves_one_visible_answer(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The exact shapes production sends, in the order it sends them.

        `m.notice` placeholder, `m.notice` progress, `m.text` terminal. The
        whole point of the notice/text split is invisible to every other test
        here, and it is what broke: with the placeholder unadmitted the
        terminal edit had no original, parked in `unresolved_edits`, and the
        conversation this projection exists to serve never saw the answer.
        """
        ingress = JournalIngress(store=alice, self_sender=BOT)

        await self._admit_live(
            ingress,
            placeholder_event(),
            stream_event(
                "$progress",
                "half an ans",
                STREAM_STATUS_STREAMING,
                replaces=PLACEHOLDER_ID,
                msgtype="m.notice",
                ts=1_100,
            ),
            stream_event(
                "$terminal",
                "the whole answer",
                STREAM_STATUS_COMPLETED,
                replaces=PLACEHOLDER_ID,
                msgtype="m.text",
                ts=1_200,
            ),
        )

        visible = await self._one_visible(alice)
        assert visible.logical_event_id == PLACEHOLDER_ID
        assert visible.revision_event_id == "$terminal"
        assert visible.content["body"] == "the whole answer"
        assert await alice.pending_refreshes(room_id=ROOM, thread_id=None) == ()

    async def test_a_skipped_progress_edit_is_still_an_admitted_event(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Skipping is a projection policy, not a refusal to accept the event.

        Admission is what deduplicates a redelivered echo and what a restart
        replays from. Dropping the event instead of only its projection would
        make nio's redelivery of it look like something new.
        """
        ingress = JournalIngress(store=alice, self_sender=BOT)

        await self._admit_live(
            ingress,
            placeholder_event(),
            stream_event(
                "$progress",
                "half an ans",
                STREAM_STATUS_STREAMING,
                replaces=PLACEHOLDER_ID,
                msgtype="m.notice",
                ts=1_100,
            ),
        )

        # Admitted, and neither is work: a bot answering its own streaming
        # frames is the loop the echo drop exists to prevent, refused here one
        # layer earlier by admitting them as context.
        assert await alice.pending() == ()
        assert await alice.load_event(PLACEHOLDER_ID) is not None
        assert await alice.load_event("$progress") is not None

    async def test_the_placeholder_is_the_message_the_answer_lands_on(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The placeholder advertises ``pending`` too, and must still project.

        It is an original, not a replacement. Skipping it would leave the
        terminal edit with no logical message to revise, and the answer would
        never become visible at all.
        """
        ingress = JournalIngress(store=alice, self_sender=BOT)

        await self._admit_live(ingress, placeholder_event())

        visible = await self._one_visible(alice)
        assert visible.logical_event_id == PLACEHOLDER_ID
        assert visible.content["body"] == PLACEHOLDER_BODY
        assert visible.content[STREAM_STATUS_KEY] == STREAM_STATUS_PENDING

    @pytest.mark.parametrize(
        "status",
        [
            STREAM_STATUS_COMPLETED,
            STREAM_STATUS_CANCELLED,
            STREAM_STATUS_ERROR,
            STREAM_STATUS_INTERRUPTED,
        ],
    )
    async def test_a_terminal_edit_installs_its_body_and_its_status(
        self,
        alice: PrincipalStore,
        status: str,
    ) -> None:
        """Every way a stream ends is content, and the four are not the same.

        ``completed`` is the answer; the other three are the answer being cut
        short. Prompt preparation tells all four apart when it decides whether
        to resume a partial reply, so a rule that kept only ``completed`` would
        strand an interrupted answer on its placeholder.
        """
        ingress = JournalIngress(store=alice, self_sender=BOT)

        await self._admit_live(
            ingress,
            placeholder_event(),
            stream_event("$progress", "half an ans", STREAM_STATUS_STREAMING, replaces=PLACEHOLDER_ID, ts=1_100),
            stream_event("$terminal", "the whole answer", status, replaces=PLACEHOLDER_ID, ts=1_200),
        )

        visible = await self._one_visible(alice)
        assert visible.content["body"] == "the whole answer"
        assert visible.content[STREAM_STATUS_KEY] == status
        assert visible.revision_event_id == "$terminal"

    @pytest.mark.parametrize("status", [STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING])
    async def test_an_edit_claiming_a_transport_status_from_someone_else_reduces(
        self,
        alice: PrincipalStore,
        status: str,
    ) -> None:
        """A stream status is a claim, not a permission.

        Anyone can put this key in their own edit. Only this bot's own
        revisions are transport, so a user's edit reduces whatever it says —
        otherwise a correction could be suppressed by spelling it right.
        """
        ingress = JournalIngress(store=alice, self_sender=BOT)

        await self._admit_live(
            ingress,
            text_event("$ask", "frist question", ts=1_000),
            stream_event("$fix", "first question", status, sender=ALICE, replaces="$ask", ts=1_100),
        )

        visible = await self._one_visible(alice)
        assert visible.content["body"] == "first question"
        assert visible.revision_event_id == "$fix"

    async def test_a_crash_mid_stream_leaves_the_placeholder_until_cleanup_speaks(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The row a crash leaves behind is the placeholder, and that is correct.

        No intermediate body was durable, so there is nothing to half-restore.
        Startup stale-stream cleanup rewrites the visible message with a
        terminal status, and that echo reduces like any other terminal edit —
        which is what makes skipping progress safe rather than lossy.
        """
        ingress = JournalIngress(store=alice, self_sender=BOT)
        progress = [
            stream_event(
                f"$progress{index}",
                f"partial {index}",
                STREAM_STATUS_STREAMING,
                replaces=PLACEHOLDER_ID,
                ts=1_100 + index,
            )
            for index in range(1, 6)
        ]

        await self._admit_live(ingress, placeholder_event(), *progress)

        crashed = await self._one_visible(alice)
        assert crashed.content["body"] == PLACEHOLDER_BODY
        assert crashed.revision_event_id == PLACEHOLDER_ID

        await self._admit_live(
            ingress,
            stream_event(
                "$cleanup",
                "partial 5 [interrupted]",
                STREAM_STATUS_ERROR,
                replaces=PLACEHOLDER_ID,
                ts=1_200,
            ),
        )

        repaired = await self._one_visible(alice)
        assert repaired.content["body"] == "partial 5 [interrupted]"
        assert repaired.content[STREAM_STATUS_KEY] == STREAM_STATUS_ERROR
        assert repaired.revision_event_id == "$cleanup"

    async def test_a_notice_typed_progress_edit_never_reaches_the_projection_policy(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A notice-typed progress echo reaches this rule and is refused by it.

        This test used to assert the opposite, and the difference was a real
        bug. MindRoom sends in-progress updates as ``m.notice`` so they raise
        no push notification, and `RoomMessageNotice` is a sibling of
        `RoomMessageText` in nio rather than a subclass -- so admission
        silently owned neither the progress edits nor the placeholder they
        replace. The terminal frame reverts to ``m.text``, arrived with no
        original to reduce onto, and parked in `unresolved_edits`, which meant
        the live projection was missing every streamed answer this bot gave.

        Admission now owns this bot's own stream frames, and the projection
        policy is what drops the intermediate ones -- which is where that
        decision belonged all along, rather than resting on a
        notification-semantics choice made on the delivery side.
        """
        ingress = JournalIngress(store=alice, self_sender=BOT)

        await self._admit_live(
            ingress,
            placeholder_event(),
            stream_event(
                "$notice",
                "half an ans",
                STREAM_STATUS_STREAMING,
                replaces=PLACEHOLDER_ID,
                msgtype="m.notice",
                ts=1_100,
            ),
        )

        assert await alice.pending() == ()
        assert (await self._one_visible(alice)).revision_event_id == PLACEHOLDER_ID


class TestReplayFidelity:
    """A recovered event must be the same event that was admitted."""

    async def test_a_message_replays_as_itself(self, alice: PrincipalStore) -> None:
        """A message replays as itself."""
        original = text_event("$m", "hello")
        await alice.admit(
            inbound_event(ROOM, original, EventKind.MESSAGE, EventClass.ACTIONABLE),
            projected_event(ROOM, original, EventKind.MESSAGE, self_sender=BOT),
        )

        stored = (await alice.pending())[0]
        replayed = parse_journal_event(stored)

        assert isinstance(replayed, nio.RoomMessageText)
        assert replayed.event_id == "$m"
        assert replayed.body == "hello"

    async def test_decryption_results_survive_replay(self, alice: PrincipalStore) -> None:
        """Nio attaches these after parsing, so they are not in the source.

        Losing them would replay a decrypted event as an untrusted one, which
        changes what the authorization layer is allowed to do with it.
        """
        original = text_event("$m", "secret")
        original.decrypted = True
        original.verified = True
        original.sender_key = "key"
        original.session_id = "session"

        await alice.admit(
            inbound_event(ROOM, original, EventKind.MESSAGE, EventClass.ACTIONABLE),
            projected_event(ROOM, original, EventKind.MESSAGE, self_sender=BOT),
        )
        replayed = parse_journal_event((await alice.pending())[0])

        assert replayed.decrypted
        assert replayed.verified
        assert replayed.sender_key == "key"
        assert replayed.session_id == "session"

    async def test_an_image_replays_with_the_reference_the_model_needs(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A media turn is only replayable if its content reference survives.

        The prompt for a media turn is not the event body; it is the file the
        body points at. A replay that produced the caption without the MXC
        reference would run the turn again against different input and call
        that recovery.
        """
        original = image_event("$img", "diagram.png")
        await alice.admit(
            inbound_event(ROOM, original, EventKind.MEDIA, EventClass.ACTIONABLE),
            projected_event(ROOM, original, EventKind.MEDIA, self_sender=BOT),
        )

        replayed = parse_journal_event((await alice.pending())[0])

        assert isinstance(replayed, nio.RoomMessageImage)
        assert replayed.url == "mxc://example.org/img"
        assert replayed.body == "diagram.png"

    async def test_an_encrypted_image_replays_with_its_decryption_keys(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Without the key material the reference is a file nobody can open."""
        original = image_event("$sealed", "sealed.png", encrypted=True)
        await alice.admit(
            inbound_event(ROOM, original, EventKind.MEDIA, EventClass.ACTIONABLE),
            projected_event(ROOM, original, EventKind.MEDIA, self_sender=BOT),
        )

        replayed = parse_journal_event((await alice.pending())[0])

        assert isinstance(replayed, nio.RoomEncryptedImage)
        assert replayed.url == "mxc://example.org/sealed"
        assert replayed.key["k"] == "cipher-key-material"
        assert replayed.iv == "initialization-vector"
        assert replayed.hashes["sha256"] == "content-hash"

    async def test_a_coalesced_batch_of_text_and_media_replays_whole_and_in_order(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The unit that replays is the batch, not the last event of it.

        Three images and a caption are one turn to the model. Recovering the
        caption alone, or recovering the images in the wrong order, both change
        the input the turn runs on.
        """
        sources = (
            image_event("$one", "first.png", ts=1_000),
            image_event("$two", "second.png", ts=1_001),
            image_event("$three", "third.png", ts=1_002),
            text_event("$caption", "what do these three have in common?", ts=1_003),
        )
        for source in sources:
            kind = EventKind.MESSAGE if isinstance(source, nio.RoomMessageText) else EventKind.MEDIA
            await alice.admit(
                inbound_event(ROOM, source, kind, EventClass.ACTIONABLE),
                projected_event(ROOM, source, kind, self_sender=BOT),
            )

        replayed = [parse_journal_event(stored) for stored in await alice.pending()]

        assert [event.event_id for event in replayed] == ["$one", "$two", "$three", "$caption"]
        assert [
            event.url  # type: ignore[attr-defined]
            for event in replayed
            if isinstance(event, nio.RoomMessageImage)
        ] == [
            "mxc://example.org/one",
            "mxc://example.org/two",
            "mxc://example.org/three",
        ]

    async def test_a_corrupt_payload_is_refused_not_guessed(self, alice: PrincipalStore) -> None:
        """A corrupt payload is refused not guessed."""
        original = text_event("$m")
        await alice.admit(
            inbound_event(ROOM, original, EventKind.MESSAGE, EventClass.ACTIONABLE),
            None,
        )
        stored = (await alice.pending())[0]
        corrupted = replace(stored, source={**stored.source, "event_id": "$different"})

        with pytest.raises(JournalCorruptionError):
            parse_journal_event(corrupted)


class TestDurableAdmission:
    """nio hears "accepted" only after the transaction commits."""

    async def test_an_admitted_event_becomes_pending_work(self, alice: PrincipalStore) -> None:
        """An admitted event becomes pending work."""
        ingress = JournalIngress(store=alice, self_sender=BOT)

        await ingress._admit(room(), text_event("$m"), nio.TimelineEventProvenance.LIVE)

        assert [event.event_id for event in await alice.pending()] == ["$m"]

    async def test_cold_history_populates_context_without_work(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Cold history populates context without work."""
        ingress = JournalIngress(store=alice, self_sender=BOT)

        await ingress._admit(room(), text_event("$m", "old"), nio.TimelineEventProvenance.HISTORY)

        assert await alice.pending() == ()
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=10)
        assert [m.content["body"] for m in page.messages] == ["old"]

    async def test_a_failed_admission_refuses_the_callback(self) -> None:
        """Refusing is what keeps the event for redelivery instead of losing it."""

        class Failing:
            principal_id = "agent@alice"

            async def admit(self, *_args: object, **_kwargs: object) -> None:
                msg = "disk is full"
                raise RuntimeError(msg)

        ingress = JournalIngress(store=Failing(), self_sender=BOT)  # type: ignore[arg-type]

        with pytest.raises(nio.CallbackNotAcceptedError):
            await ingress._admit(room(), text_event("$m"), nio.TimelineEventProvenance.LIVE)

    async def test_redelivery_after_a_crash_creates_one_turn(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Nio redelivers what it was never told was accepted."""
        ingress = JournalIngress(store=alice, self_sender=BOT)
        event = text_event("$m")

        await ingress._admit(room(), event, nio.TimelineEventProvenance.LIVE)
        await ingress._admit(room(), event, nio.TimelineEventProvenance.RECOVERED)

        assert [journal.event_id for journal in await alice.pending()] == ["$m"]

    async def test_an_unowned_event_is_neither_admitted_nor_rejected(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An unowned event is neither admitted nor rejected."""
        ingress = JournalIngress(store=alice, self_sender=BOT)
        topic = nio.Event.parse_event(
            {
                "event_id": "$topic",
                "sender": ALICE,
                "origin_server_ts": 1,
                "type": "m.room.topic",
                "state_key": "",
                "content": {"topic": "hi"},
            },
        )
        assert isinstance(topic, nio.Event)

        await ingress._admit(room(), topic, nio.TimelineEventProvenance.LIVE)

        assert await alice.pending() == ()


class TestPendingEventWorker:
    """Execution order, failure isolation, and crash behavior."""

    @staticmethod
    async def _admit(store: PrincipalStore, event: nio.Event, room_id: str = ROOM) -> None:
        await store.admit(
            inbound_event(room_id, event, EventKind.MESSAGE, EventClass.ACTIONABLE),
            projected_event(room_id, event, EventKind.MESSAGE, self_sender=BOT),
        )

    @staticmethod
    async def _admit_reaction(store: PrincipalStore, event: nio.Event) -> None:
        await store.admit(inbound_event(ROOM, event, EventKind.REACTION, EventClass.ACTIONABLE))

    async def test_a_rooms_events_run_in_receipt_order(self, alice: PrincipalStore) -> None:
        """A rooms events run in receipt order."""
        handled: list[str] = []

        async def handle(event: JournalEvent) -> SettlementOutcome:
            handled.append(event.event_id)
            return SettlementOutcome.SUCCEEDED

        await self._admit(alice, text_event("$second", ts=9_000))
        await self._admit(alice, text_event("$first", ts=1_000))

        await PendingEventWorker(store=alice, handle=handle).drain_once()

        assert handled == ["$second", "$first"]

    async def test_a_settled_event_never_runs_again(self, alice: PrincipalStore) -> None:
        """A settled event never runs again."""
        runs = 0

        async def handle(event: JournalEvent) -> SettlementOutcome:
            nonlocal runs
            runs += 1
            del event
            return SettlementOutcome.SUCCEEDED

        await self._admit(alice, text_event("$m"))
        worker = PendingEventWorker(store=alice, handle=handle)

        await worker.drain_once()
        await worker.drain_once()

        assert runs == 1

    async def test_a_failed_event_stays_pending(self, alice: PrincipalStore) -> None:
        """A failed event stays pending."""

        async def handle(event: JournalEvent) -> SettlementOutcome:
            del event
            msg = "model unavailable"
            raise RuntimeError(msg)

        await self._admit(alice, text_event("$m"))

        await PendingEventWorker(store=alice, handle=handle).drain_once()

        assert [event.event_id for event in await alice.pending()] == ["$m"]

    async def test_a_failure_stops_that_rooms_later_events(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Otherwise the room is answered out of order, and the retry lands last."""
        handled: list[str] = []

        async def handle(event: JournalEvent) -> SettlementOutcome:
            handled.append(event.event_id)
            if event.event_id == "$first":
                msg = "model unavailable"
                raise RuntimeError(msg)
            return SettlementOutcome.SUCCEEDED

        await self._admit(alice, text_event("$first", ts=1_000))
        await self._admit(alice, text_event("$second", ts=2_000))

        await PendingEventWorker(store=alice, handle=handle).drain_once()

        assert handled == ["$first"]
        assert {event.event_id for event in await alice.pending()} == {"$first", "$second"}

    async def test_a_recovery_drain_runs_a_room_through_its_lane_not_beside_it(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A room has one lane, and a drain has to take it rather than add one.

        Recovery is not a phase that finishes before the pump starts:
        `JournalDispatcher.drain_once` is scheduled every time a bot reports
        ready, so it runs against a live pump. A drain that dispatched beside
        the room's lane would break both halves of what a lane guarantees --
        one event would be inside two handlers at once, and event three would
        overtake event two.

        The handler claims a semantic consumer the way a reaction's does, so
        the second handler is not merely wasteful: its claim raises against
        the row the first one already settled, and `_run_lane` logs that and
        stops the room mid-pass. The claim is right to raise. Nothing settles
        an event while its own handler is running, so a settled row there
        means the event was already being run somewhere else.
        """
        handled: list[str] = []
        concurrent = 0
        peak_concurrent = 0
        inside_first = asyncio.Event()
        release_first = asyncio.Event()

        async def handle(event: JournalEvent) -> SettlementOutcome:
            nonlocal concurrent, peak_concurrent
            handled.append(event.event_id)
            concurrent += 1
            peak_concurrent = max(peak_concurrent, concurrent)
            try:
                if event.event_id == "$first":
                    # Held open so a drain has a window to start while the
                    # pump's lane is demonstrably inside this event.
                    inside_first.set()
                    await release_first.wait()
                await alice.claim_semantic_consumer(event.event_id, SemanticConsumer.REACTION_HOOKS)
            finally:
                concurrent -= 1
            return SettlementOutcome.SUCCEEDED

        for event_id in ("$first", "$second", "$third"):
            await self._admit_reaction(alice, reaction_event(event_id))

        worker = PendingEventWorker(store=alice, handle=handle)
        worker.start()
        await asyncio.wait_for(inside_first.wait(), timeout=5)

        draining = asyncio.create_task(worker.drain_once())
        # Enough turns of the loop for a drain to scan the store and dispatch.
        for _ in range(50):
            await asyncio.sleep(0)
        release_first.set()

        await asyncio.wait_for(draining, timeout=5)
        await worker.stop()

        assert handled == ["$first", "$second", "$third"]
        assert peak_concurrent == 1
        assert await alice.unsettled_event_ids() == frozenset()

    async def test_one_stalled_room_does_not_block_another(
        self,
        alice: PrincipalStore,
    ) -> None:
        """One stalled room does not block another."""
        other_room = "!other:example.org"
        released = asyncio.Event()
        fast_finished = asyncio.Event()
        handled: list[str] = []

        async def handle(event: JournalEvent) -> SettlementOutcome:
            if event.room_id == ROOM:
                await released.wait()
            handled.append(event.event_id)
            if event.room_id == other_room:
                fast_finished.set()
            return SettlementOutcome.SUCCEEDED

        await self._admit(alice, text_event("$slow"))
        await self._admit(alice, text_event("$fast"), room_id=other_room)

        worker = PendingEventWorker(store=alice, handle=handle)
        draining = asyncio.create_task(worker.drain_once())

        # The other room finishes while this one is still blocked, which is
        # only possible if the lanes are genuinely independent.
        await asyncio.wait_for(fast_finished.wait(), timeout=5)
        assert handled == ["$fast"]

        released.set()
        await draining
        assert handled == ["$fast", "$slow"]

    async def test_cancellation_leaves_the_event_pending(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A crash mid-turn must make the event eligible again, not stranded."""
        started = asyncio.Event()

        async def handle(event: JournalEvent) -> SettlementOutcome:
            del event
            started.set()
            await asyncio.sleep(3600)
            return SettlementOutcome.SUCCEEDED

        await self._admit(alice, text_event("$m"))
        worker = PendingEventWorker(store=alice, handle=handle)
        worker.start()
        await asyncio.wait_for(started.wait(), timeout=5)

        await worker.stop()

        assert [event.event_id for event in await alice.pending()] == ["$m"]

    async def test_a_restart_resumes_what_the_previous_process_left(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A restart resumes what the previous process left."""
        handled: list[str] = []

        async def never(event: JournalEvent) -> SettlementOutcome:
            del event
            await asyncio.sleep(3600)
            return SettlementOutcome.SUCCEEDED

        async def handle(event: JournalEvent) -> SettlementOutcome:
            handled.append(event.event_id)
            return SettlementOutcome.SUCCEEDED

        await self._admit(alice, text_event("$m"))
        crashed = PendingEventWorker(store=alice, handle=never)
        crashed.start()
        await asyncio.sleep(0.05)
        await crashed.stop()

        restarted = PendingEventWorker(store=alice, handle=handle)
        await restarted.drain_once()

        assert handled == ["$m"]

    async def test_a_backlog_larger_than_one_batch_is_fully_drained(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A bound that drops the remainder abandons durable work silently.

        Driven through the pump rather than a drain, because only the pump has
        to arrange its own next look: nothing admits a further event afterwards
        to wake it, so a scan that stops at one page strands the rest forever.
        """
        count = _BATCH_SIZE + 1
        handled: list[str] = []

        async def handle(event: JournalEvent) -> SettlementOutcome:
            handled.append(event.event_id)
            return SettlementOutcome.SUCCEEDED

        for index in range(count):
            await self._admit(alice, text_event(f"$m{index:04d}", ts=1_000 + index))

        worker = PendingEventWorker(store=alice, handle=handle)
        worker.start()
        await _eventually_async(lambda: alice.pending())
        await worker.stop()

        assert len(handled) == count

    async def test_work_admitted_while_a_lane_runs_is_still_dispatched(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The lost wakeup that leaves a live room permanently unanswered.

        The pump is woken while the room's lane is busy, so it cannot start a
        second one. Unless the finishing lane arranges another look, the event
        admitted during that window stays pending forever even though the
        process is healthy and still syncing.
        """
        released = asyncio.Event()
        handled: list[str] = []

        async def handle(event: JournalEvent) -> SettlementOutcome:
            if event.event_id == "$slow":
                await released.wait()
            handled.append(event.event_id)
            return SettlementOutcome.SUCCEEDED

        worker = PendingEventWorker(store=alice, handle=handle)
        await self._admit(alice, text_event("$slow", ts=1_000))
        worker.start()
        await _eventually(lambda: worker._lanes != {})

        await self._admit(alice, text_event("$late", ts=2_000))
        worker.wake()
        await asyncio.sleep(0)
        released.set()

        await _eventually(lambda: handled == ["$slow", "$late"])
        await worker.stop()

    async def test_a_deferred_turn_does_not_hide_the_events_behind_it(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A turn still running stays pending, so a scan must look past it.

        A full page of in-flight turns is exactly what a busy bot looks like.
        If the scan stops at the first one it cannot act on, every event queued
        behind them is invisible until those turns happen to finish.
        """
        handled: list[str] = []

        async def handle(event: JournalEvent) -> SettlementOutcome | None:
            handled.append(event.event_id)
            return None if event.event_id.startswith("$busy") else SettlementOutcome.SUCCEEDED

        for index in range(_BATCH_SIZE):
            await self._admit(alice, text_event(f"$busy{index:04d}", ts=1_000 + index), room_id=f"!r{index}:x")
        worker = PendingEventWorker(store=alice, handle=handle)
        worker.start()
        await _eventually(lambda: len(handled) == _BATCH_SIZE)
        handled.clear()

        await self._admit(alice, text_event("$behind", ts=9_000), room_id="!behind:x")
        worker.wake()

        await _eventually(lambda: handled == ["$behind"])
        await worker.stop()

    async def test_a_failed_lane_is_retried_without_another_admission(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Nothing else wakes the pump, so the failure has to schedule its own retry."""
        attempts: list[str] = []

        async def handle(event: JournalEvent) -> SettlementOutcome:
            attempts.append(event.event_id)
            if len(attempts) == 1:
                msg = "model unavailable"
                raise RuntimeError(msg)
            return SettlementOutcome.SUCCEEDED

        await self._admit(alice, text_event("$m"))
        worker = PendingEventWorker(store=alice, handle=handle)
        worker._retry_delay_seconds = 0.01
        worker.start()

        await _eventually(lambda: len(attempts) >= 2, seconds=10)
        await worker.stop()

        assert attempts == ["$m", "$m"]

    async def test_a_deferral_survives_a_worker_that_cannot_probe_its_owner(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A worker with no probe has to believe the handoff, as it always did.

        Pins the default so the opposite one cannot be introduced by accident:
        assuming every owner is gone would re-dispatch every in-flight turn on
        every scan, which answers live conversations twice.
        """
        attempts: list[str] = []

        async def handle(event: JournalEvent) -> None:
            attempts.append(event.event_id)

        await self._admit(alice, text_event("$m"))
        worker = PendingEventWorker(store=alice, handle=handle)

        await worker.drain_once()
        await worker.drain_once()

        assert attempts == ["$m"]
        assert [event.event_id for event in await alice.pending()] == ["$m"]

    async def test_a_deferral_whose_owner_is_alive_is_never_taken_back(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The turn is still running, so re-dispatching would answer twice."""
        attempts: list[str] = []

        async def handle(event: JournalEvent) -> None:
            attempts.append(event.event_id)

        await self._admit(alice, text_event("$m"))
        worker = PendingEventWorker(
            store=alice,
            handle=handle,
            deferral_is_live=lambda _event: True,
        )

        await worker.drain_once()
        await worker.drain_once()
        await worker.drain_once()

        assert attempts == ["$m"]

    async def test_a_deferral_whose_owner_died_is_dispatched_again(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The hole this closes: durable work owed to an owner that is gone.

        Deferral is a promise to call ``release``. An owner that dies without
        keeping it leaves the event pending forever while the process looks
        healthy, because no admission and no retry ever reconsiders it.
        """
        attempts: list[str] = []
        owner_alive = True

        async def handle(event: JournalEvent) -> None:
            attempts.append(event.event_id)

        await self._admit(alice, text_event("$m"))
        worker = PendingEventWorker(
            store=alice,
            handle=handle,
            deferral_is_live=lambda _event: owner_alive,
        )

        await worker.drain_once()
        await worker.drain_once()
        assert attempts == ["$m"], "a live owner must keep its event"

        owner_alive = False
        await worker.drain_once()

        assert attempts == ["$m", "$m"]

    async def test_a_lost_owner_is_noticed_without_any_further_admission(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Nothing wakes the pump when an owner dies, so it has to look again.

        Driven through the pump rather than a drain: a drain is an explicit
        "look now", and the failure being closed is precisely that a quiet room
        never gets one.
        """
        attempts: list[str] = []
        owner_alive = True

        async def handle(event: JournalEvent) -> None:
            attempts.append(event.event_id)

        await self._admit(alice, text_event("$m"))
        worker = PendingEventWorker(
            store=alice,
            handle=handle,
            deferral_is_live=lambda _event: owner_alive,
            deferral_scan_seconds=0.01,
        )
        worker.start()
        await _eventually(lambda: attempts == ["$m"])

        # Several scan periods pass with the owner alive and nothing is retaken.
        await asyncio.sleep(0.1)
        assert attempts == ["$m"]

        owner_alive = False

        await _eventually(lambda: attempts == ["$m", "$m"], seconds=10)
        await worker.stop()


class TestOutOfBandDispatch:
    """An event its caller runs itself is still an event with one handler."""

    @staticmethod
    def _dispatcher(
        store: PrincipalStore,
        on_room_lifecycle: Callable[[nio.MatrixRoom, nio.RoomMemberEvent], Awaitable[None]],
    ) -> JournalDispatcher:
        """Build a dispatcher whose only interesting callback is the lifecycle one."""

        async def noop(_room: nio.MatrixRoom, _event: nio.Event) -> None:
            return None

        return JournalDispatcher(
            store=store,
            self_sender=BOT,
            callbacks=JournalCallbacks(
                on_message=cast("Any", noop),
                on_media=cast("Any", noop),
                on_reaction=cast("Any", noop),
                on_approval=cast("Any", noop),
                on_room_lifecycle=on_room_lifecycle,
                on_redaction=cast("Any", noop),
                on_decryption_failure=cast("Any", noop),
                source_has_live_owner=lambda _event_id: False,
                turn_has_live_claim=lambda _event_id: False,
            ),
            room_for_id=lambda _room_id: room(),
        )

    async def test_admit_and_run_is_the_events_only_handler(self, alice: PrincipalStore) -> None:
        """Running an event inline does not exempt it from having one handler.

        ``admit_and_run`` wakes the pump and then awaits twice -- a load and a
        pending check -- before it reaches the callback. The pump has no
        in-flight filter, because an event stays pending for the whole time its
        handler runs, so a scan inside that window collects the very event the
        caller is already running and dispatches it into the room's lane.

        The count matters as much as the concurrency. Asserting only that
        nothing raised would pass with the bug present: a room-lifecycle
        callback claims no semantic consumer, so the second handler runs to
        completion and settles a row the first one is about to settle again.
        """
        handled: list[str] = []
        concurrent = 0
        peak_concurrent = 0
        inside_handler = asyncio.Event()
        second_handler = asyncio.Event()
        release_handler = asyncio.Event()

        async def on_room_lifecycle(_room: nio.MatrixRoom, event: nio.RoomMemberEvent) -> None:
            nonlocal concurrent, peak_concurrent
            handled.append(event.event_id)
            concurrent += 1
            peak_concurrent = max(peak_concurrent, concurrent)
            if concurrent > 1:
                second_handler.set()
            try:
                # Held open so the pump has a window to collect an event that
                # is pending precisely because its handler has not finished.
                inside_handler.set()
                await release_handler.wait()
            finally:
                concurrent -= 1

        dispatcher = self._dispatcher(alice, on_room_lifecycle)
        dispatcher.start()
        running = asyncio.create_task(
            dispatcher.admit_and_run(room(), member_event("$join"), EventKind.ROOM_LIFECYCLE, EventClass.ACTIONABLE),
        )
        await asyncio.wait_for(inside_handler.wait(), timeout=5)
        with contextlib.suppress(TimeoutError):
            # A second handler has to wake the pump, read a page of pending
            # events and start a lane, so this waits on wall time rather than
            # loop turns. Reaching the timeout is the passing case.
            await asyncio.wait_for(second_handler.wait(), timeout=0.5)
        release_handler.set()

        await asyncio.wait_for(running, timeout=5)
        await dispatcher.stop()

        assert handled == ["$join"]
        assert peak_concurrent == 1
        assert await alice.unsettled_event_ids() == frozenset()


class TestDeferralOwnership:
    """Which deferrals still have an owner, and which are work nobody holds."""

    @staticmethod
    def _dispatcher(
        store: PrincipalStore,
        *,
        gate_owns: bool = False,
        turn_claimed: bool = False,
    ) -> JournalDispatcher:
        """Build a dispatcher whose two owner probes answer as configured."""

        async def noop(_room: nio.MatrixRoom, _event: nio.Event) -> None:
            return None

        return JournalDispatcher(
            store=store,
            self_sender=BOT,
            callbacks=JournalCallbacks(
                on_message=cast("Any", noop),
                on_media=cast("Any", noop),
                on_reaction=cast("Any", noop),
                on_approval=cast("Any", noop),
                on_room_lifecycle=cast("Any", noop),
                on_redaction=cast("Any", noop),
                on_decryption_failure=cast("Any", noop),
                source_has_live_owner=lambda _event_id: gate_owns,
                turn_has_live_claim=lambda _event_id: turn_claimed,
            ),
            room_for_id=lambda _room_id: room(),
        )

    @staticmethod
    async def _admitted(store: PrincipalStore, event: nio.Event, kind: EventKind) -> JournalEvent:
        """Admit one event and return the journal row the worker would see."""
        await store.admit(
            inbound_event(ROOM, event, kind, EventClass.ACTIONABLE),
            projected_event(ROOM, event, kind, self_sender=BOT),
        )
        return next(item for item in await store.pending() if item.event_id == event.event_id)

    async def test_a_completing_kind_can_never_have_a_lost_owner(self, alice: PrincipalStore) -> None:
        """A reaction is finished when its handler returns, so it never defers.

        Reporting one of these as lost would take back an event that is not
        deferred at all, which is how a settled reaction runs twice.
        """
        dispatcher = self._dispatcher(alice)
        dispatcher.release_turn_replay()
        reaction = await self._admitted(alice, reaction_event("$r"), EventKind.REACTION)

        assert dispatcher._deferral_is_live(reaction) is True

    async def test_replay_parked_on_the_fleet_is_not_a_lost_owner(self, alice: PrincipalStore) -> None:
        """Turn replay waits for responders to exist, and is released by draining.

        Nothing calls back to hand these over, so treating the absence of a
        claim as death would re-dispatch every replayed turn on every scan,
        before the agents that answer them are even running.
        """
        dispatcher = self._dispatcher(alice)
        message = await self._admitted(alice, text_event("$m"), EventKind.MESSAGE)

        assert dispatcher._deferral_is_live(message) is True

    async def test_a_source_the_coalescing_gate_still_holds_is_owned(self, alice: PrincipalStore) -> None:
        """A batch still debouncing has no turn claim yet, and is not abandoned."""
        dispatcher = self._dispatcher(alice, gate_owns=True)
        dispatcher.release_turn_replay()
        message = await self._admitted(alice, text_event("$m"), EventKind.MESSAGE)

        assert dispatcher._deferral_is_live(message) is True

    async def test_a_source_an_unsettled_turn_still_claims_is_owned(self, alice: PrincipalStore) -> None:
        """The running turn will answer this message, so nobody else may."""
        dispatcher = self._dispatcher(alice, turn_claimed=True)
        dispatcher.release_turn_replay()
        message = await self._admitted(alice, text_event("$m"), EventKind.MESSAGE)

        assert dispatcher._deferral_is_live(message) is True

    async def test_a_source_with_neither_a_gate_nor_a_claim_is_lost(self, alice: PrincipalStore) -> None:
        """Both owners are gone and the event is still pending: nobody will release it."""
        dispatcher = self._dispatcher(alice)
        dispatcher.release_turn_replay()
        message = await self._admitted(alice, text_event("$m"), EventKind.MESSAGE)

        assert dispatcher._deferral_is_live(message) is False


class TestRecoveryDoesNotReenterALiveTurn:
    """A drain runs beside live turns, so it must leave the ones it finds alone."""

    @staticmethod
    def _dispatcher(
        store: PrincipalStore,
        on_turn: Callable[[nio.MatrixRoom, nio.Event], Awaitable[TurnDispatchOutcome]],
        live_claims: set[str],
        *,
        gate_owns: bool = False,
    ) -> JournalDispatcher:
        """Build a dispatcher whose turn claims are whatever the test says."""

        async def noop(_room: nio.MatrixRoom, _event: nio.Event) -> None:
            return None

        return JournalDispatcher(
            store=store,
            self_sender=BOT,
            callbacks=JournalCallbacks(
                on_message=cast("Any", on_turn),
                on_media=cast("Any", on_turn),
                on_reaction=cast("Any", noop),
                on_approval=cast("Any", noop),
                on_room_lifecycle=cast("Any", noop),
                on_redaction=cast("Any", noop),
                on_decryption_failure=cast("Any", noop),
                source_has_live_owner=lambda _event_id: gate_owns,
                turn_has_live_claim=lambda event_id: event_id in live_claims,
            ),
            room_for_id=lambda _room_id: room(),
        )

    @staticmethod
    async def _admit(store: PrincipalStore, event: nio.Event, kind: EventKind = EventKind.MESSAGE) -> None:
        await store.admit(
            inbound_event(ROOM, event, kind, EventClass.ACTIONABLE),
            projected_event(ROOM, event, kind, self_sender=BOT),
        )

    @pytest.mark.parametrize(
        ("kind", "event"),
        [(EventKind.MESSAGE, text_event("$m")), (EventKind.MEDIA, image_event("$m"))],
        ids=("message", "media"),
    )
    async def test_a_source_the_gate_still_holds_is_not_handed_to_a_second_turn(
        self,
        alice: PrincipalStore,
        kind: EventKind,
        event: nio.Event,
    ) -> None:
        """Both turn-backed kinds ask this, so neither may answer it for itself.

        A lane cancelled inside its handler -- a shutdown, a hot reload -- can
        leave a source with the coalescing gate and nothing in the worker's
        memory saying so. The next scan is then free to collect it, and the
        media path refused that while the message path walked straight in.
        """
        entered: list[str] = []

        async def on_turn(_room: nio.MatrixRoom, event: nio.Event) -> TurnDispatchOutcome:
            entered.append(event.event_id)
            return TurnDispatchOutcome.DEFERRED

        dispatcher = self._dispatcher(alice, on_turn, set(), gate_owns=True)
        dispatcher.release_turn_replay()
        await self._admit(alice, event, kind)

        await dispatcher.drain_once()

        assert entered == []
        assert [item.event_id for item in await alice.pending()] == ["$m"]

    async def test_a_drain_leaves_a_running_turns_room_answering(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Re-entering a live turn wedges the room until that turn finishes.

        The duplicate does not answer twice -- the turn store refuses the
        second claim -- but refusing is not returning. ``_claim_live_turn``
        waits for the competing owner to settle, and it does that inside the
        room's lane, so every message received after it goes unanswered for as
        long as the original turn runs. A turn parked on a tool approval makes
        that indefinite.
        """
        live_claims: set[str] = set()
        entered: list[str] = []
        turn_settled = asyncio.Event()

        async def on_turn(_room: nio.MatrixRoom, event: nio.Event) -> TurnDispatchOutcome:
            entered.append(event.event_id)
            if event.event_id in live_claims:
                # What the real contended claim does: wait for the owner.
                await turn_settled.wait()
            live_claims.add(event.event_id)
            return TurnDispatchOutcome.DEFERRED

        dispatcher = self._dispatcher(alice, on_turn, live_claims)
        await self._admit(alice, text_event("$live", ts=1_000))
        await dispatcher.drain_once()
        assert entered == ["$live"], "the first drain starts the turn"

        await self._admit(alice, text_event("$next", ts=2_000))
        try:
            await asyncio.wait_for(dispatcher.drain_once(), timeout=5)
        finally:
            turn_settled.set()

        assert entered == ["$live", "$next"]

    async def test_a_deferred_source_is_still_taken_back_when_its_owner_dies(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Skipping an owned source may not become losing an abandoned one.

        The drain no longer forgets what is in flight, so the liveness probe is
        the only thing left that can hand an orphaned source back.
        """
        live_claims: set[str] = set()
        entered: list[str] = []

        async def on_turn(_room: nio.MatrixRoom, event: nio.Event) -> TurnDispatchOutcome:
            entered.append(event.event_id)
            live_claims.add(event.event_id)
            return TurnDispatchOutcome.DEFERRED

        dispatcher = self._dispatcher(alice, on_turn, live_claims)
        await self._admit(alice, text_event("$m"))

        await dispatcher.drain_once()
        await dispatcher.drain_once()
        assert entered == ["$m"], "a live owner keeps its source"

        live_claims.clear()
        await dispatcher.drain_once()

        assert entered == ["$m", "$m"]


async def _eventually_async(query: Callable[[], Awaitable[Sized]], *, seconds: float = 10.0) -> None:
    """Wait until a durable query comes back empty."""
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if not len(await query()):
            return
        await asyncio.sleep(0.01)
    msg = "The durable queue never drained"
    raise AssertionError(msg)


async def _eventually(predicate: Callable[[], bool], *, seconds: float = 5.0) -> None:
    """Wait for a background pump to reach a state, without fixed sleeps."""
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    msg = "The worker never reached the expected state"
    raise AssertionError(msg)


def member_event(event_id: str, *, user_id: str = ALICE) -> nio.RoomMemberEvent:
    """Return a parsed room-member join event."""
    event = nio.Event.parse_event(
        {
            "event_id": event_id,
            "sender": user_id,
            "state_key": user_id,
            "origin_server_ts": 7_000,
            "type": "m.room.member",
            "content": {"membership": "join"},
            "unsigned": {"prev_content": {"membership": "leave"}},
        },
    )
    assert isinstance(event, nio.RoomMemberEvent)
    return event


class TestTimelineMemberProvenance:
    """A consumer that runs after the timeline still gets nio's verdict."""

    @pytest.mark.parametrize(
        ("provenance", "expected"),
        [
            (nio.TimelineEventProvenance.LIVE, EventClass.ACTIONABLE),
            (nio.TimelineEventProvenance.RECOVERED, EventClass.ACTIONABLE),
            (nio.TimelineEventProvenance.HISTORY, EventClass.CONTEXT_ONLY),
        ],
    )
    async def test_a_declined_member_event_still_states_its_class(
        self,
        alice: PrincipalStore,
        provenance: nio.TimelineEventProvenance,
        expected: EventClass,
    ) -> None:
        """Declining to admit is exactly when a later consumer needs the verdict."""
        ingress = JournalIngress(store=alice, self_sender=BOT)
        event = member_event("$join")

        await ingress._admit(room(), event, provenance)

        assert ingress.admission_kind(event) is None
        assert ingress.timeline_member_event_class(event) is expected

    async def test_an_event_nio_never_offered_has_no_class(self, alice: PrincipalStore) -> None:
        """Nio skips admission for an event it accepted earlier, and silence is the answer."""
        ingress = JournalIngress(store=alice, self_sender=BOT)

        assert ingress.timeline_member_event_class(member_event("$join")) is None

    async def test_only_member_events_are_recorded(self, alice: PrincipalStore) -> None:
        """Nothing else has a consumer that runs later, so nothing else is kept."""
        ingress = JournalIngress(store=alice, self_sender=BOT)

        await ingress._admit(room(), text_event("$m"), nio.TimelineEventProvenance.LIVE)

        assert ingress.timeline_member_provenance.get("$m") is None

    async def test_clearing_forgets_the_response_that_produced_it(self, alice: PrincipalStore) -> None:
        """The verdict is about one delivery, so it cannot answer for the next."""
        ingress = JournalIngress(store=alice, self_sender=BOT)
        event = member_event("$join")
        await ingress._admit(room(), event, nio.TimelineEventProvenance.RECOVERED)

        ingress.timeline_member_provenance.clear()

        assert ingress.timeline_member_event_class(event) is None
