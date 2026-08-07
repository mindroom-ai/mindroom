"""Point lookups answered from the visible projection, and when they are not.

``get_event`` used to be cache-first. It now asks the projection, which already
holds the revision currently on screen, and falls through to the homeserver for
anything the projection does not have. These pin both halves: what a hit is
allowed to say, and that a miss still costs a round trip rather than answering
"no such event".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import nio
import pytest

from mindroom.event_journal import EventClass, EventKind
from mindroom.matrix.conversation_cache import _projected_room_get_event
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.journal_ingress import inbound_event, projected_event

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mindroom.event_journal import EventJournalStore, PrincipalStore

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
    content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one raw Matrix message event."""
    message_content: dict[str, Any] = content if content is not None else {"msgtype": "m.text", "body": body}
    if replaces is not None:
        message_content["m.new_content"] = {"msgtype": "m.text", "body": body}
        message_content["m.relates_to"] = {"rel_type": "m.replace", "event_id": replaces}
    elif thread_id is not None:
        message_content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    return {
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": ts,
        "type": "m.room.message",
        "content": message_content,
    }


def parse(source: dict[str, Any]) -> nio.Event:
    """Return the parsed nio event for one raw source.

    Encrypted media takes nio's decrypted-event path, the same one the live
    sync callbacks reach it by; the plaintext schema rejects it for having no
    top-level ``url``.
    """
    content = source.get("content")
    if isinstance(content, dict) and "file" in content:
        media_event = nio.RoomMessage.parse_decrypted_event(source)
        assert isinstance(media_event, nio.Event)
        return media_event
    event = nio.Event.parse_event(source)
    assert isinstance(event, nio.Event)
    return event


async def admit_all(store: PrincipalStore, sources: Iterable[dict[str, Any]]) -> None:
    """Admit raw events as live traffic."""
    for source in sources:
        event = parse(source)
        await store.admit(
            inbound_event(ROOM, event, EventKind.MESSAGE, EventClass.ACTIONABLE),
            projected_event(ROOM, event, EventKind.MESSAGE, self_sender=BOT),
        )


@dataclass
class FakeClient:
    """A homeserver that answers exactly what a test set up, and counts asks."""

    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)

    async def room_get_event(
        self,
        room_id: str,
        event_id: str,
    ) -> nio.RoomGetEventResponse | nio.RoomGetEventError:
        """Return one stored event."""
        del room_id
        self.asked.append(event_id)
        source = self.events.get(event_id)
        if source is None:
            return nio.RoomGetEventError("M_NOT_FOUND")
        # nio builds this response by assignment rather than construction.
        response = nio.RoomGetEventResponse()
        response.event = parse(source)
        return response


async def lookup(
    store: PrincipalStore,
    client: FakeClient,
    event_id: str,
) -> nio.RoomGetEventResponse | nio.RoomGetEventError:
    """Resolve one event the way the conversation facade does."""
    response, _fetched = await _projected_room_get_event(
        store,
        # The production signature wants nio's client; the fake answers the one
        # method this path calls.
        client,  # type: ignore[arg-type]
        ROOM,
        event_id,
    )
    return response


class TestProjectionHit:
    """What the projection is allowed to answer with."""

    async def test_an_edited_message_reads_back_as_its_current_revision(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The body on screen, not the one that was first sent."""
        await admit_all(
            alice,
            [
                raw("$original", "first draft", ts=1_000),
                raw("$edit", "corrected", ts=2_000, replaces="$original"),
            ],
        )
        client = FakeClient()

        response = await lookup(alice, client, "$original")

        assert isinstance(response, nio.RoomGetEventResponse)
        assert response.event.source["content"]["body"] == "corrected"
        # The logical message keeps its own identity: a caller asked about
        # `$original` and must not be handed an event with a different ID.
        assert response.event.event_id == "$original"
        # The revision's time, because that is when what is on screen was said.
        assert response.event.server_timestamp == 2_000
        assert client.asked == []

    async def test_an_unedited_message_reads_back_whole(self, alice: PrincipalStore) -> None:
        """A message nobody revised still answers from the projection."""
        await admit_all(alice, [raw("$only", "hello", ts=1_500)])
        client = FakeClient()

        response = await lookup(alice, client, "$only")

        assert isinstance(response, nio.RoomGetEventResponse)
        assert response.event.source["content"]["body"] == "hello"
        assert response.event.server_timestamp == 1_500
        assert client.asked == []

    async def test_an_edited_threaded_reply_keeps_its_thread(self, alice: PrincipalStore) -> None:
        """The relation an edit's ``m.new_content`` does not carry is restored from the row.

        A replacement's new content is specified not to repeat the relation of
        the message it replaces, and the projection stores exactly that content.
        The callers of this lookup are resolving which thread an event belongs
        to, so a reply that reads back unthreaded after being edited is the
        whole failure this guards.
        """
        await admit_all(
            alice,
            [
                raw("$root", "root", ts=1_000),
                raw("$reply", "first draft", ts=2_000, thread_id="$root"),
                raw("$edit", "corrected", ts=3_000, replaces="$reply"),
            ],
        )
        client = FakeClient()

        response = await lookup(alice, client, "$reply")

        assert isinstance(response, nio.RoomGetEventResponse)
        assert response.event.source["content"]["body"] == "corrected"
        assert EventInfo.from_event(response.event.source).thread_id == "$root"

    async def test_an_unedited_reply_keeps_the_relation_it_was_sent_with(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A relation already in the content is left alone, not overwritten.

        A reply carries `m.in_reply_to` inside its own `m.relates_to`, and
        restoring a thread relation over the top would erase it.
        """
        await admit_all(
            alice,
            [
                raw("$root", "root", ts=1_000),
                raw(
                    "$reply",
                    "answering",
                    ts=2_000,
                    content={
                        "msgtype": "m.text",
                        "body": "answering",
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": "$root",
                            "m.in_reply_to": {"event_id": "$root"},
                        },
                    },
                ),
            ],
        )
        client = FakeClient()

        response = await lookup(alice, client, "$reply")

        assert isinstance(response, nio.RoomGetEventResponse)
        info = EventInfo.from_event(response.event.source)
        assert info.thread_id == "$root"
        assert info.reply_to_event_id == "$root"

    async def test_an_encrypted_image_reads_back_openable(self, alice: PrincipalStore) -> None:
        """Media keeps the key material that makes its reference usable.

        `nio.RoomGetEventResponse.from_dict` will not parse an encrypted
        attachment, so this path has its own media branch; without it a
        projected image comes back as nothing at all.
        """
        await admit_all(
            alice,
            [
                raw(
                    "$image",
                    "photo.png",
                    ts=1_000,
                    content={
                        "msgtype": "m.image",
                        "body": "photo.png",
                        "file": {
                            "url": "mxc://example.org/abc",
                            "v": "v2",
                            "key": {
                                "alg": "A256CTR",
                                "ext": True,
                                "k": "aWQtd2l0aC0zMi1ieXRlcy1vZi1rZXktbWF0ZXJpYWw",
                                "key_ops": ["encrypt", "decrypt"],
                                "kty": "oct",
                            },
                            "iv": "aXYtMTZieXRlcw==",
                            "hashes": {"sha256": "Y2hlY2tzdW0="},
                        },
                    },
                ),
            ],
        )
        client = FakeClient()

        response = await lookup(alice, client, "$image")

        assert isinstance(response, nio.RoomGetEventResponse)
        assert isinstance(response.event, nio.RoomEncryptedImage)
        assert response.event.key["k"] == "aWQtd2l0aC0zMi1ieXRlcy1vZi1rZXktbWF0ZXJpYWw"
        assert client.asked == []


class TestForeignEdits:
    """An edit is only an edit when it comes from the message's author."""

    async def test_another_senders_replacement_is_not_served_as_the_author_s_message(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Anyone in a room can send an `m.replace`; only the author can revise."""
        await admit_all(
            alice,
            [
                raw("$original", "what alice said", sender=ALICE, ts=1_000),
                raw("$forged", "what bob wants it to say", sender=BOB, ts=2_000, replaces="$original"),
            ],
        )
        client = FakeClient()

        response = await lookup(alice, client, "$original")

        assert isinstance(response, nio.RoomGetEventResponse)
        assert response.event.source["content"]["body"] == "what alice said"
        assert response.event.sender == ALICE

    async def test_the_author_s_own_later_edit_still_wins(self, alice: PrincipalStore) -> None:
        """Refusing a foreign edit must not also refuse the real one."""
        await admit_all(
            alice,
            [
                raw("$original", "what alice said", sender=ALICE, ts=1_000),
                raw("$forged", "what bob wants it to say", sender=BOB, ts=2_000, replaces="$original"),
                raw("$real", "what alice meant", sender=ALICE, ts=3_000, replaces="$original"),
            ],
        )
        client = FakeClient()

        response = await lookup(alice, client, "$original")

        assert isinstance(response, nio.RoomGetEventResponse)
        assert response.event.source["content"]["body"] == "what alice meant"


class TestProjectionMiss:
    """A miss is not an answer, and never becomes one."""

    async def test_an_event_the_projection_never_saw_is_fetched(self, alice: PrincipalStore) -> None:
        """History older than this bot's membership still resolves."""
        client = FakeClient(events={"$ancient": raw("$ancient", "from before", ts=10)})

        response = await lookup(alice, client, "$ancient")

        assert isinstance(response, nio.RoomGetEventResponse)
        assert response.event.source["content"]["body"] == "from before"
        assert client.asked == ["$ancient"]

    async def test_a_revision_is_not_answered_with_the_message_it_revises(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Asked about an edit, the lookup must not hand back an original.

        The projection is keyed by logical message, and an edit's event ID is
        stored only as the revision pointer. Answering from that pointer would
        return an event that reports itself as not an edit at all, which is the
        opposite of what a caller resolving a replacement needs.
        """
        await admit_all(
            alice,
            [
                raw("$original", "first draft", ts=1_000),
                raw("$edit", "corrected", ts=2_000, replaces="$original"),
            ],
        )
        client = FakeClient(events={"$edit": raw("$edit", "corrected", ts=2_000, replaces="$original")})

        response = await lookup(alice, client, "$edit")

        assert isinstance(response, nio.RoomGetEventResponse)
        assert response.event.event_id == "$edit"
        assert EventInfo.from_event(response.event.source).is_edit
        assert client.asked == ["$edit"]

    async def test_a_withheld_body_is_fetched_rather_than_served_empty(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A redacted revision leaves a row with no body; that is not content.

        The projection keeps the row -- the message did not stop existing --
        but clears its body until a refetch says what the server shows now.
        Serving that row would answer with a message that has no text, so the
        lookup treats it as something the projection does not have.
        """
        await admit_all(
            alice,
            [
                raw("$original", "first draft", ts=1_000),
                raw("$edit", "corrected", ts=2_000, replaces="$original"),
            ],
        )
        redaction = parse(
            {
                "event_id": "$redact",
                "sender": ALICE,
                "origin_server_ts": 3_000,
                "type": "m.room.redaction",
                "redacts": "$edit",
                "content": {},
            },
        )
        await alice.admit(
            inbound_event(ROOM, redaction, EventKind.REDACTION, EventClass.ACTIONABLE),
            projected_event(ROOM, redaction, EventKind.REDACTION, self_sender=BOT),
        )
        client = FakeClient(events={"$original": raw("$original", "first draft", ts=1_000)})

        # Asserted at the read itself as well as through the lookup. The lookup
        # degrades to the homeserver when the store read *fails*, too, so on
        # its own it cannot tell a guard that declined from a guard that was
        # removed and left the decode to crash.
        assert await alice.visible_message(room_id=ROOM, logical_event_id="$original") is None

        response = await lookup(alice, client, "$original")

        assert isinstance(response, nio.RoomGetEventResponse)
        assert client.asked == ["$original"]

    async def test_a_homeserver_error_is_returned_rather_than_swallowed(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A caller resolving a reply target has to be able to tell a miss apart."""
        client = FakeClient()

        response = await lookup(alice, client, "$nowhere")

        assert isinstance(response, nio.RoomGetEventError)
        assert client.asked == ["$nowhere"]

    async def test_another_principal_s_message_is_not_visible(
        self,
        alice: PrincipalStore,
        journal_store: EventJournalStore,
    ) -> None:
        """One database holds many bots, and a lookup only sees its own rows."""
        await admit_all(alice, [raw("$mine", "alice's view", ts=1_000)])
        bob = journal_store.principal("agent@bob")
        client = FakeClient(events={"$mine": raw("$mine", "from the server", ts=1_000)})

        response = await lookup(bob, client, "$mine")

        assert isinstance(response, nio.RoomGetEventResponse)
        assert response.event.source["content"]["body"] == "from the server"
        assert client.asked == ["$mine"]
