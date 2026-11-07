"""The router's side of the rooms calls are in, over a fake of the router's Matrix client at the HTTP boundary."""

from __future__ import annotations

import asyncio
import gc
import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast

import aiohttp
import nio
import pytest

from mindroom.call_transcripts import router_room
from mindroom.call_transcripts.router_room import RouterCallRoom
from mindroom.call_transcripts.service import CallTranscript, EndedRow, TranscriptMessage, _Turn
from mindroom.constants import CALL_TRANSCRIPT_CONTENT_KEY, SKIP_MENTIONS_KEY
from tests.test_journal_ingress import TestAdmittedWorkReachesItsCallback as _AdmittedWork

if TYPE_CHECKING:
    import io
    from collections.abc import Callable

    from mindroom.event_journal import EventJournalStore

ROOM = "!room:example.org"
OTHER_ROOM = "!other:example.org"
ROUTER = "@mindroom_router:example.org"
ANN = "@ann:example.org"
BOB = "@bob:example.org"
CAROL = "@carol:example.org"
NOW_MS = 1_800_000_000_000


def _member(user_id: str, membership: str = "join", displayname: str | None = None) -> dict[str, Any]:
    content: dict[str, Any] = {"membership": membership}
    if displayname is not None:
        content["displayname"] = displayname
    return {
        "type": "m.room.member",
        "state_key": user_id,
        "sender": user_id,
        "content": content,
        "event_id": f"$member-{user_id}",
        "origin_server_ts": 0,
    }


def _power_levels(**content: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "type": "m.room.power_levels",
        "state_key": "",
        "sender": ROUTER,
        "content": content,
        "event_id": "$power",
        "origin_server_ts": 0,
    }


def _create(room_version: str, creator: str = ROUTER) -> dict[str, Any]:
    return {
        "type": "m.room.create",
        "state_key": "",
        "sender": creator,
        "content": {"room_version": room_version},
        "event_id": "$create",
        "origin_server_ts": 0,
    }


@dataclass
class FakeRouterClient:
    """The router's Matrix client as the room side uses it: room state reads, sends and uploads, recorded."""

    state: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    unreadable: BaseException | nio.ErrorResponse | None = None
    send_error: BaseException | nio.RoomSendError | None = None
    upload_error: nio.UploadError | None = None
    user_id: str = ROUTER
    device_id: str = "ROUTER"
    access_token: str = "router-token"  # noqa: S105 - a fake client's token
    olm: None = None
    rooms: dict[str, nio.MatrixRoom] = field(default_factory=dict)
    sent: list[dict[str, Any]] = field(default_factory=list)
    uploads: list[bytes] = field(default_factory=list)
    reads: int = 0
    state_reads: int = 0
    reading: asyncio.Event = field(default_factory=asyncio.Event)  # Set once a room state read has started.
    gate: asyncio.Event | None = None  # When given, each room state read waits for it before it answers.
    uploading: asyncio.Event = field(default_factory=asyncio.Event)  # Set once an upload has started.
    upload_gate: asyncio.Event | None = None  # When given, each upload waits for it before it answers.

    def __post_init__(self) -> None:
        """The router has synced every room it has state for."""
        for room_id in self.state:
            self.rooms.setdefault(room_id, nio.MatrixRoom(room_id, self.user_id))

    async def room_get_state(self, room_id: str) -> nio.RoomGetStateResponse | nio.RoomGetStateError:
        """Return the room's whole state, after the event loop has had a turn, as a request to a homeserver does."""
        self.reads += 1
        self.state_reads += 1
        self.reading.set()
        await asyncio.sleep(0)
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.unreadable, BaseException):
            raise self.unreadable
        if self.unreadable is not None:
            return nio.RoomGetStateError(self.unreadable.message, self.unreadable.status_code, room_id=room_id)
        return nio.RoomGetStateResponse(events=list(self.state.get(room_id, [])), room_id=room_id)

    async def room_get_state_event(
        self,
        room_id: str,
        event_type: str,
        state_key: str = "",
    ) -> nio.RoomGetStateEventResponse | nio.RoomGetStateEventError:
        """Return one state event's content, M_NOT_FOUND when the room has none."""
        self.reads += 1
        if isinstance(self.unreadable, BaseException):
            raise self.unreadable
        if self.unreadable is not None:
            return nio.RoomGetStateEventError(self.unreadable.message, self.unreadable.status_code, room_id=room_id)
        for event in self.state.get(room_id, []):
            if event["type"] == event_type and event["state_key"] == state_key:
                return nio.RoomGetStateEventResponse(dict(event["content"]), event_type, state_key, room_id)
        return nio.RoomGetStateEventError("Event not found.", "M_NOT_FOUND", room_id=room_id)

    async def room_send(
        self,
        room_id: str,
        message_type: str,
        content: dict[str, Any],
        tx_id: str | None = None,
        ignore_unverified_devices: bool = False,
    ) -> nio.RoomSendResponse | nio.RoomSendError:
        """Record the event; the homeserver answers a transaction it already took with that same event."""
        assert ignore_unverified_devices
        if isinstance(self.send_error, BaseException):
            raise self.send_error
        if self.send_error is not None:
            return self.send_error
        for index, sent in enumerate(self.sent):
            if sent["tx_id"] == tx_id:
                return nio.RoomSendResponse(f"$event-{index}", room_id)
        self.sent.append({"room_id": room_id, "type": message_type, "content": content, "tx_id": tx_id})
        return nio.RoomSendResponse(f"$event-{len(self.sent) - 1}", room_id)

    async def upload(
        self,
        *,
        data_provider: Callable[[object, object], io.BytesIO],
        content_type: str,
        filename: str,
        filesize: int,
    ) -> tuple[nio.UploadResponse | nio.UploadError, None]:
        """Keep the uploaded bytes, or refuse them with ``upload_error``."""
        del content_type, filename
        data = data_provider(None, None).read()
        assert len(data) == filesize
        self.uploading.set()
        if self.upload_gate is not None:
            await self.upload_gate.wait()
        if self.upload_error is not None:
            return self.upload_error, None
        self.uploads.append(data)
        return nio.UploadResponse(f"mxc://example.org/upload-{len(self.uploads)}"), None


def _room(client: FakeRouterClient | None, *, monotonic: Callable[[], float] = lambda: 0.0) -> RouterCallRoom:
    return RouterCallRoom(client=lambda: cast("nio.AsyncClient | None", client), monotonic=monotonic)


def _ended_row(*, transcript_event_id: str | None = None) -> EndedRow:
    return EndedRow(
        room_id=ROOM,
        call_id="call1",
        row_event_id="$row",
        body="Call ended · 2 min",
        ended_at=NOW_MS / 1000,
        duration_seconds=125,
        transcript_event_id=transcript_event_id,
    )


def _message(*, body: str = "Call · 2 min", turns: tuple[_Turn, ...] | None = None) -> TranscriptMessage:
    if turns is None:
        turns = (_Turn(sender=ANN, text="We ship on Friday."), _Turn(sender=BOB, text="I will write the notes."))
    return TranscriptMessage(
        room_id=ROOM,
        call_id="call1",
        row_event_id="$row",
        body=body,
        transcript=CallTranscript(
            call_id="call1",
            room_id=ROOM,
            duration_seconds=125,
            turns=turns,
            names={ANN: "Ann", BOB: "Bob"},
        ),
    )


# Reading the room


@pytest.mark.asyncio
async def test_without_a_ready_router_nothing_can_be_told() -> None:
    """Before the router is ready, or after it stopped, every question is answered with None and nothing is posted."""
    room = _room(None)

    assert room.router_joined(ROOM) is None
    assert await room.is_member(ROOM, ANN) is None
    assert await room.may_post(ROOM, ANN) is None
    assert await room.display_names(ROOM, [ANN]) is None
    assert await room.post_row(ROOM, "call1") is None
    assert await room.post_transcript(_message()) is None
    assert await room.edit_row(_ended_row()) is None


@pytest.mark.asyncio
async def test_a_room_the_router_is_not_in_has_no_members() -> None:
    """The router reads only rooms it is in: in any other, nobody is a member or may post."""
    client = FakeRouterClient(state={ROOM: [_member(ANN)]})
    assert _room(client).router_joined(ROOM) is True
    client.rooms.clear()

    room = _room(client)

    assert room.router_joined(ROOM) is False
    assert await room.is_member(ROOM, ANN) is False
    assert await room.may_post(ROOM, ANN) is False
    assert await room.display_names(ROOM, [ANN]) is None
    assert client.reads == 0


@pytest.mark.asyncio
async def test_heartbeats_together_share_one_read_of_the_rooms_state_for_five_seconds() -> None:
    """Many people asking at once, about members and posting, cause one full state read per five seconds."""
    client = FakeRouterClient(state={ROOM: [_create("11"), _member(ANN), _member(BOB)]})
    now = [0.0]
    room = _room(client, monotonic=lambda: now[0])

    async def heartbeat(user_id: str) -> tuple[object, ...]:
        return (await room.is_member(ROOM, user_id), await room.may_post(ROOM, user_id))

    answers = await asyncio.gather(*(heartbeat(user_id) for user_id in (ANN, BOB) * 5))

    assert client.state_reads == 1
    assert set(answers) == {(True, True)}
    now[0] += 4.9
    await heartbeat(ANN)
    assert client.state_reads == 1
    now[0] += 0.1
    await asyncio.gather(heartbeat(ANN), heartbeat(BOB))
    assert client.state_reads == 2


@pytest.mark.asyncio
async def test_a_question_that_goes_away_leaves_the_shared_read_running_for_the_others() -> None:
    """Cancelling one question waiting for the room's read cancels neither the read nor the others sharing it."""
    gate = asyncio.Event()
    client = FakeRouterClient(state={ROOM: [_member(ANN), _member(BOB)]}, gate=gate)
    room = _room(client)
    leaving = asyncio.create_task(room.is_member(ROOM, ANN))
    staying = asyncio.create_task(room.is_member(ROOM, BOB))
    await client.reading.wait()

    leaving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leaving
    gate.set()

    assert await staying is True
    assert await room.is_member(ROOM, ANN) is True
    assert client.state_reads == 1


@pytest.mark.asyncio
async def test_a_read_that_fails_after_everyone_waiting_went_away_reports_nothing() -> None:
    """A read that raises once its only question was cancelled leaves no unretrieved error for the event loop."""
    loop = asyncio.get_running_loop()
    reported: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        gate = asyncio.Event()
        client = FakeRouterClient(state={ROOM: [_member(ANN)]}, unreadable=RuntimeError("A bug."), gate=gate)
        room = _room(client)
        leaving = asyncio.create_task(room.is_member(ROOM, ANN))
        await client.reading.wait()
        leaving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leaving
        read = room._reads[ROOM].result
        gate.set()
        await asyncio.wait([read])
        del room, read
        gc.collect()
    finally:
        loop.set_exception_handler(None)

    assert reported == []


@pytest.mark.asyncio
async def test_a_failed_read_of_the_room_is_never_reused() -> None:
    """After a read failed, the next question reads the room again."""
    client = FakeRouterClient(state={ROOM: [_member(ANN)]}, unreadable=aiohttp.ClientConnectionError("down"))
    room = _room(client)

    assert await room.is_member(ROOM, ANN) is None
    client.unreadable = None

    assert await room.is_member(ROOM, ANN) is True
    assert client.state_reads == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [nio.LocalProtocolError("The client is logged out."), ValueError("Not JSON."), TimeoutError()],
)
async def test_a_read_that_raises_cannot_tell_and_never_raises(error: BaseException) -> None:
    """Whatever reading the room raises, every question about it answers None, so the routes answer unavailable."""
    client = FakeRouterClient(state={ROOM: [_create("12", creator=ANN), _member(ANN)]}, unreadable=error)
    room = _room(client)

    assert await room.may_post(ROOM, ANN) is None
    assert await room.is_member(ROOM, ANN) is None
    assert await room.display_names(ROOM, [ANN]) is None


# Who is in the room, and who may post


@pytest.mark.asyncio
async def test_a_read_of_the_room_that_takes_too_long_cannot_tell(monkeypatch: pytest.MonkeyPatch) -> None:
    """A homeserver that does not answer a read of the room in time is a room that cannot be read, never a wait."""
    monkeypatch.setattr(router_room, "_STATE_READ_TIMEOUT_SECONDS", 0.01)
    client = FakeRouterClient(state={ROOM: [_member(ANN)]}, gate=asyncio.Event())
    room = _room(client)

    async with asyncio.timeout(5):
        assert await room.is_member(ROOM, ANN) is None

    client.gate.set()
    assert await room.is_member(ROOM, ANN) is True
    assert client.state_reads == 2


@pytest.mark.asyncio
async def test_only_people_joined_to_the_room_are_members() -> None:
    """A joined person is a member; someone who left, was invited, or never came is not."""
    client = FakeRouterClient(state={ROOM: [_member(ANN), _member(BOB, "leave"), _member(CAROL, "invite")]})
    room = _room(client)

    assert await room.is_member(ROOM, ANN) is True
    assert await room.is_member(ROOM, BOB) is False
    assert await room.is_member(ROOM, CAROL) is False
    assert await room.is_member(ROOM, "@eve:example.org") is False


@pytest.mark.asyncio
async def test_membership_that_cannot_be_read_cannot_be_told() -> None:
    """A failed read of someone's membership answers None."""
    client = FakeRouterClient(state={ROOM: [_member(ANN)]}, unreadable=nio.ErrorResponse("Bad gateway", "M_UNKNOWN"))

    assert await _room(client).is_member(ROOM, ANN) is None


@pytest.mark.asyncio
async def test_posting_follows_the_power_levels_for_messages() -> None:
    """Who may post is decided by the level for ``m.room.message``, then ``events_default``."""
    levels = _power_levels(users={ANN: 50, BOB: 10}, users_default=0, events_default=0, events={"m.room.message": 20})
    room = _room(FakeRouterClient(state={ROOM: [_create("11"), levels]}))

    assert await room.may_post(ROOM, ANN) is True
    assert await room.may_post(ROOM, BOB) is False
    assert await room.may_post(ROOM, CAROL) is False

    by_default = _power_levels(users={ANN: 50}, users_default=0, events_default=50)
    room = _room(FakeRouterClient(state={ROOM: [_create("11"), by_default]}))

    assert await room.may_post(ROOM, ANN) is True
    assert await room.may_post(ROOM, BOB) is False


@pytest.mark.asyncio
async def test_everyone_may_post_in_a_room_without_power_levels() -> None:
    """Without power levels everyone is at level 0, which may post messages."""
    room = _room(FakeRouterClient(state={ROOM: [_create("11")]}))

    assert await room.may_post(ROOM, BOB) is True


@pytest.mark.asyncio
async def test_a_room_version_12_creator_may_post_although_power_levels_do_not_list_them() -> None:
    """Creators of a room version 12 room have unlimited power, which power levels never list."""
    levels = _power_levels(users={}, users_default=0, events_default=50)
    room = _room(FakeRouterClient(state={ROOM: [_create("12", creator=ANN), levels]}))

    assert await room.may_post(ROOM, ANN) is True
    assert await room.may_post(ROOM, BOB) is False


@pytest.mark.asyncio
async def test_power_levels_that_cannot_be_read_cannot_tell_who_may_post() -> None:
    """A failed read of the power levels answers None."""
    client = FakeRouterClient(state={ROOM: [_create("11")]}, unreadable=aiohttp.ClientConnectionError("down"))

    assert await _room(client).may_post(ROOM, ANN) is None


@pytest.mark.asyncio
async def test_display_names_come_from_each_persons_membership() -> None:
    """Each person's display name in the room; None for someone without one or whose membership cannot be found."""
    client = FakeRouterClient(state={ROOM: [_member(ANN, displayname="Ann"), _member(BOB), _member(CAROL, "leave")]})

    names = await _room(client).display_names(ROOM, {ANN, BOB, CAROL, "@eve:example.org"})

    assert names == {ANN: "Ann", BOB: None, CAROL: None, "@eve:example.org": None}


# The row and the transcript message


@pytest.mark.asyncio
async def test_the_row_is_a_notice_that_wakes_no_agent_and_mentions_nobody() -> None:
    """The row says the call started, names the call, mentions nobody, and is sent as a transaction of that call."""
    client = FakeRouterClient(state={ROOM: [_member(ANN)]})

    event_id = await _room(client).post_row(ROOM, "call1")

    assert event_id == "$event-0"
    [sent] = client.sent
    assert sent["room_id"] == ROOM
    assert sent["type"] == "m.room.message"
    assert sent["content"] == {
        "msgtype": "m.notice",
        "body": "Call started",
        "io.mindroom.call": {"call_id": "call1"},
        "m.mentions": {},
        SKIP_MENTIONS_KEY: True,
    }
    assert sent["tx_id"]


@pytest.mark.asyncio
async def test_posting_a_calls_row_or_message_again_is_never_a_second_event() -> None:
    """Each call's row and message have their own transaction, so sending one again posts nothing new."""
    client = FakeRouterClient(state={ROOM: [_member(ANN)]})
    room = _room(client)

    row = await room.post_row(ROOM, "call1")
    message = await room.post_transcript(_message())
    assert await room.post_row(ROOM, "call1") == row
    assert await room.post_transcript(_message()) == message
    other_call_row = await room.post_row(ROOM, "call2")

    assert len({row, message, other_call_row}) == 3
    assert len(client.sent) == 3


@pytest.mark.asyncio
async def test_the_row_is_edited_to_say_its_call_ended_and_then_where_its_transcript_is() -> None:
    """Each edit replaces the row as a notice that mentions nobody, sent as a transaction of that call and edit."""
    client = FakeRouterClient(state={ROOM: [_member(ANN)]})
    room = _room(client)

    ended = await room.edit_row(_ended_row())
    linked = await room.edit_row(_ended_row(transcript_event_id="$transcript"))
    assert await room.edit_row(_ended_row()) == ended
    assert await room.edit_row(_ended_row(transcript_event_id="$transcript")) == linked

    assert len({ended, linked}) == 2
    [end, link] = client.sent
    replacement = {
        "msgtype": "m.notice",
        "body": "Call ended · 2 min",
        "io.mindroom.call": {"call_id": "call1", "ended_at": "2027-01-15T08:00:00.000Z", "duration_seconds": 125},
        "m.mentions": {},
        SKIP_MENTIONS_KEY: True,
    }
    assert end["content"]["m.new_content"] == replacement
    assert end["content"]["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$row"}
    assert (end["content"]["msgtype"], end["content"]["body"]) == ("m.notice", "* Call ended · 2 min")
    assert end["content"]["m.mentions"] == {}
    assert link["content"]["m.new_content"] == {
        **replacement,
        "io.mindroom.call": {**replacement["io.mindroom.call"], "transcript_event_id": "$transcript"},
    }
    assert link["content"]["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$row"}
    assert end["tx_id"]
    assert link["tx_id"]
    other_call = await room.edit_row(replace(_ended_row(), call_id="call2"))
    assert other_call not in {ended, linked}


@pytest.mark.asyncio
async def test_the_transcript_message_is_a_notice_in_the_rows_thread_with_the_transcript_and_the_call() -> None:
    """The message replies in the row's thread, carries the turns by sender ID, and names its call."""
    client = FakeRouterClient(state={ROOM: [_member(ANN)]})

    event_id = await _room(client).post_transcript(_message())

    assert event_id == "$event-0"
    [sent] = client.sent
    content = sent["content"]
    assert content["msgtype"] == "m.notice"
    assert content["body"] == "Call · 2 min"
    assert content["m.relates_to"]["rel_type"] == "m.thread"
    assert content["m.relates_to"]["event_id"] == "$row"
    assert content[CALL_TRANSCRIPT_CONTENT_KEY] == {
        "duration_seconds": 125,
        "turns": [
            {"sender": ANN, "text": "We ship on Friday."},
            {"sender": BOB, "text": "I will write the notes."},
        ],
    }
    assert content["io.mindroom.call_notes"] == {"call_id": "call1"}
    assert content["m.mentions"] == {}
    assert content[SKIP_MENTIONS_KEY] is True
    assert sent["tx_id"]


@pytest.mark.asyncio
async def test_mentions_in_the_notes_are_neutralised() -> None:
    """A Matrix ID or room mention the notes model wrote pings nobody, in the body or its HTML; an address stays."""
    client = FakeRouterClient(state={ROOM: [_member(ANN)]})
    notes = "We planned the launch.\n\n**Action items**\n- @ann:example.org: send the deck to @room and ann@example.org"

    await _room(client).post_transcript(_message(body=notes))

    content = client.sent[0]["content"]
    assert "@ann:example.org" not in content["body"]
    assert "@room" not in content["body"]
    assert "@ann:example.org" not in content["formatted_body"]
    assert content["body"].replace("\u200b", "") == notes
    assert "<strong>Action items</strong>" in content["formatted_body"]
    assert "and ann@example.org" in content["body"]


@pytest.mark.asyncio
async def test_a_transcript_too_large_for_one_event_goes_to_the_sidecar() -> None:
    """A long call's transcript is uploaded beside a preview that still names the call and lives in the thread."""
    client = FakeRouterClient(state={ROOM: [_member(ANN)]})
    turns = tuple(_Turn(sender=ANN if index % 2 else BOB, text="word " * 400) for index in range(60))

    event_id = await _room(client).post_transcript(_message(turns=turns))

    assert event_id == "$event-0"
    [sent] = client.sent
    content = sent["content"]
    assert CALL_TRANSCRIPT_CONTENT_KEY not in content
    assert content["io.mindroom.call_notes"] == {"call_id": "call1"}
    assert content["m.relates_to"]["event_id"] == "$row"
    assert content["io.mindroom.long_text"]["is_complete_content"] is True
    # Still a notice that mentions nobody, so it notifies nobody either.
    assert content["msgtype"] == "m.notice"
    assert content["m.mentions"] == {}
    assert content[SKIP_MENTIONS_KEY] is True
    [upload] = client.uploads
    sidecar = json.loads(upload)
    assert len(sidecar[CALL_TRANSCRIPT_CONTENT_KEY]["turns"]) == 60
    assert sidecar["io.mindroom.call_notes"] == {"call_id": "call1"}


@pytest.mark.asyncio
async def test_a_transcript_whose_sidecar_cannot_be_uploaded_is_not_posted() -> None:
    """Without its sidecar the transcript is not sent at all, never as a cut-short text that would replace it."""
    client = FakeRouterClient(
        state={ROOM: [_member(ANN)]},
        upload_error=nio.UploadError("Service unavailable", "M_UNKNOWN"),
    )
    room = _room(client)
    turns = tuple(_Turn(sender=ANN if index % 2 else BOB, text="word " * 400) for index in range(60))

    assert await room.post_transcript(_message(turns=turns)) is None
    assert client.sent == []

    client.upload_error = None
    assert await room.post_transcript(_message(turns=turns)) == "$event-0"
    assert client.sent[0]["content"]["io.mindroom.long_text"]["is_complete_content"] is True


@pytest.mark.asyncio
async def test_switching_off_while_the_sidecar_uploads_sends_nothing() -> None:
    """A transcript whose sidecar is still uploading when call transcripts go off is never sent."""
    client = FakeRouterClient(state={ROOM: [_member(ANN)]}, upload_gate=asyncio.Event())
    on = True
    room = RouterCallRoom(client=lambda: cast("nio.AsyncClient", client) if on else None)
    turns = tuple(_Turn(sender=ANN if index % 2 else BOB, text="word " * 400) for index in range(60))
    posting = asyncio.ensure_future(room.post_transcript(_message(turns=turns)))
    await client.uploading.wait()

    on = False
    client.upload_gate.set()

    assert await posting is None
    assert client.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "send_error",
    [nio.RoomSendError("Forbidden", "M_FORBIDDEN", room_id=ROOM), aiohttp.ClientConnectionError("down")],
)
async def test_a_failed_post_answers_none(send_error: BaseException | nio.RoomSendError) -> None:
    """A post the homeserver refused, or that never reached it, is not posted."""
    client = FakeRouterClient(state={ROOM: [_member(ANN)]}, send_error=send_error)
    room = _room(client)

    assert await room.post_row(ROOM, "call1") is None
    assert await room.post_transcript(_message()) is None
    assert await room.edit_row(_ended_row()) is None


@pytest.mark.asyncio
async def test_a_failed_post_to_a_room_the_router_has_not_synced_answers_none() -> None:
    """Posting needs the room's encryption state; failing to read it is a failed post, not an error."""
    client = FakeRouterClient(state={ROOM: [_member(ANN)]}, unreadable=aiohttp.ClientConnectionError("down"))
    client.rooms.clear()

    assert await _room(client).post_row(OTHER_ROOM, "call1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["row", "row edit", "transcript", "transcript in a sidecar"])
async def test_the_row_and_the_transcript_message_start_no_agent_turn(
    journal_store: EventJournalStore,
    message: str,
) -> None:
    """An agent in the room takes the router's row, its edits and the transcript message as conversation, never as a turn."""
    client = FakeRouterClient(state={ROOM: [_member(ANN)]})
    room = _room(client)
    if message == "row":
        await room.post_row(ROOM, "call1")
    elif message == "row edit":
        await room.edit_row(_ended_row(transcript_event_id="$transcript"))
    elif message == "transcript":
        await room.post_transcript(_message(body="We planned the launch. @mindroom_helper:example.org will book it."))
    else:
        await room.post_transcript(_message(turns=tuple(_Turn(sender=ANN, text="word " * 400) for _ in range(60))))
    [sent] = client.sent
    event = nio.Event.parse_event(
        {
            "event_id": "$sent",
            "sender": ROUTER,
            "origin_server_ts": 1_000,
            "type": sent["type"],
            "content": sent["content"],
        },
    )
    assert isinstance(event, nio.Event)

    delivery = await _AdmittedWork()._deliver(
        journal_store.principal("agent@helper"),
        event,
        nio.TimelineEventProvenance.LIVE,
    )

    assert not delivery.owed_work
    assert delivery.handled == ()
