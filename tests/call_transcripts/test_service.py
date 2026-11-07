"""Tests for the rules of a call between people, with fakes for the room, the transcriber and the notes writer."""

from __future__ import annotations

import asyncio
import io
import threading
import wave
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import pytest

from mindroom.call_transcripts import service as service_module
from mindroom.call_transcripts.service import (
    Call,
    CallEnded,
    CallRefusedError,
    CallTranscript,
    CallTranscriptService,
    CallUpdated,
    EndedRow,
    LineAdded,
    TranscriptMessage,
    _Turn,
)
from mindroom.call_transcripts.store import CallTranscriptStore, StoredCall

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Mapping
    from pathlib import Path

    from mindroom.call_transcripts.service import CallEvent
    from mindroom.call_transcripts.store import StoredLine

ROOM = "!room:example.org"
OTHER_ROOM = "!other:example.org"
ANN = "@ann:example.org"
BOB = "@bob:example.org"
CAROL = "@carol:example.org"
OUTSIDER = "@eve:example.org"
START = 1_000.0


def _wav(seconds: float = 2.0, *, rate: int = 16_000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00" * (int(seconds * rate) * 2))
    return buffer.getvalue()


class FakeClock:
    """Seconds since the epoch that only move when a test moves them."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        """Return the current time."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move time forward."""
        self.now += seconds


@dataclass
class FakeRoom:
    """The router's view of the rooms: who is in them, who may post, the MatrixRTC call, and what it posted."""

    members: set[str] = field(default_factory=lambda: {ANN, BOB, CAROL})
    posters: set[str] = field(default_factory=lambda: {ANN, BOB, CAROL})
    rtc: dict[str, float] | None = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=lambda: {ANN: "Ann", BOB: "Bob"})
    readable: bool = True
    ready: bool = True
    failed_transcript_posts: int = 0
    crashing_rooms: set[str] = field(default_factory=set)
    on_read: Callable[[], None] | None = None
    row_gate: asyncio.Event | None = None
    row_started: asyncio.Event = field(default_factory=asyncio.Event)
    row_attempts: int = 0
    rows: list[tuple[str, str]] = field(default_factory=list)
    transcripts: list[TranscriptMessage] = field(default_factory=list)
    edit_gate: asyncio.Event | None = None
    edit_started: asyncio.Event = field(default_factory=asyncio.Event)
    row_edits: list[EndedRow] = field(default_factory=list)

    def _read(self) -> None:
        if self.on_read is not None:
            self.on_read()

    async def is_member(self, room_id: str, user_id: str) -> bool | None:
        """Answer from ``members``, or not at all while the room is unreadable."""
        del room_id
        return user_id in self.members if self.readable else None

    async def may_post(self, room_id: str, user_id: str) -> bool | None:
        """Answer from ``posters``, or not at all while the room is unreadable."""
        del room_id
        self._read()
        return user_id in self.posters if self.readable else None

    async def call_members(self, room_id: str) -> Mapping[str, float] | None:
        """Return the MatrixRTC memberships, None while the state or the room cannot be read."""
        del room_id
        self._read()
        return None if self.rtc is None or not self.readable else dict(self.rtc)

    async def display_names(self, room_id: str, user_ids: Collection[str]) -> dict[str, str | None] | None:
        """Return the names known for ``user_ids``, or nothing while the room cannot be read or the router is away."""
        del room_id
        if not (self.readable and self.ready):
            return None
        return {user_id: self.names.get(user_id) for user_id in user_ids}

    async def post_row(self, room_id: str, call_id: str) -> str | None:
        """Post a row while the router is ready, after ``row_gate`` opens."""
        self.row_attempts += 1
        self.row_started.set()
        await asyncio.sleep(0)
        if self.row_gate is not None:
            await self.row_gate.wait()
        if not self.ready:
            return None
        self.rows.append((room_id, call_id))
        return f"$row-{len(self.rows)}"

    async def post_transcript(self, message: TranscriptMessage) -> str | None:
        """Post the message while the router is ready, after the failures asked for; crash for ``crashing_rooms``."""
        await asyncio.sleep(0)
        if message.room_id in self.crashing_rooms:
            msg = "A bug in the room side."
            raise RuntimeError(msg)
        if not self.ready:
            return None
        if self.failed_transcript_posts:
            self.failed_transcript_posts -= 1
            return None
        self.transcripts.append(message)
        return f"$transcript-{len(self.transcripts)}"

    async def edit_row(self, row: EndedRow) -> str | None:
        """Edit the row while the router is ready, after ``edit_gate`` opens."""
        self.edit_started.set()
        await asyncio.sleep(0)
        if self.edit_gate is not None:
            await self.edit_gate.wait()
        if not self.ready:
            return None
        self.row_edits.append(row)
        return f"$edit-{len(self.row_edits)}"


@dataclass
class FakeTranscriber:
    """Speech-to-text that answers ``text``, optionally waiting at ``gate`` first."""

    text: str | None = "Hello there"
    gate: asyncio.Event | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    requests: list[tuple[str, str, float]] = field(default_factory=list)

    async def transcribe(self, audio: bytes, *, call_id: str, user_id: str, seconds: float) -> str | None:
        """Record the request and answer ``text``."""
        assert audio.startswith(b"RIFF")
        self.requests.append((call_id, user_id, seconds))
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        return self.text


@dataclass
class FakeNotes:
    """A notes model that writes ``markdown``, or fails while ``failing`` and for calls in ``failing_rooms``."""

    markdown: str | None = "## About\nThe launch."
    failing: bool = False
    failing_rooms: set[str] = field(default_factory=set)
    transcripts: list[CallTranscript] = field(default_factory=list)

    async def write_notes(self, transcript: CallTranscript) -> str | None:
        """Record the transcript and answer ``markdown``, or fail."""
        self.transcripts.append(transcript)
        await asyncio.sleep(0)
        if self.failing or transcript.room_id in self.failing_rooms:
            msg = "The notes model did not answer."
            raise RuntimeError(msg)
        return self.markdown


@dataclass
class World:
    """One service over a real store and fakes for everything it talks to."""

    tmp_path: Path
    clock: FakeClock = field(default_factory=FakeClock)
    room: FakeRoom = field(default_factory=FakeRoom)
    transcriber: FakeTranscriber = field(default_factory=FakeTranscriber)
    notes: FakeNotes = field(default_factory=FakeNotes)
    events: list[CallEvent] = field(default_factory=list)
    service: CallTranscriptService = field(init=False)

    def __post_init__(self) -> None:
        """Start the service."""
        self.service = self.restart()

    @property
    def store(self) -> CallTranscriptStore:
        """Open the store the service writes, to read what it wrote."""
        return CallTranscriptStore(self.tmp_path)

    def restart(self) -> CallTranscriptService:
        """Start MindRoom again on the same storage: everything kept in memory is gone."""
        self.service = CallTranscriptService(
            CallTranscriptStore(self.tmp_path),
            self.room,
            self.transcriber,
            self.notes,
            clock=self.clock,
        )
        return self.service

    async def say(self, call_id: str, user_id: str, *, ago: float = 3.0, seconds: float = 2.0) -> StoredLine:
        """Upload a stretch of ``seconds`` that began ``ago`` seconds before now, which speech-to-text hears words in."""
        line = await self.service.speech(
            call_id,
            user_id,
            audio=_wav(seconds),
            started_at=self.clock.now - ago,
        )
        assert line is not None
        return line

    async def end_by_leaving(self, *user_ids: str) -> str:
        """Start a call with ``user_ids``, let each say something, and have them all leave; return the call's id."""
        call = await self.service.join(ROOM, user_ids[0])
        for user_id in user_ids[1:]:
            await self.service.join(ROOM, user_id)
        self.clock.advance(5)
        for user_id in user_ids:
            await self.say(call.id, user_id)
            self.clock.advance(5)
        for user_id in user_ids:
            await self.service.leave(call.id, user_id)
        return call.id


@pytest.fixture
def world(tmp_path: Path) -> World:
    """Give each test a fresh world."""
    return World(tmp_path)


async def _refused(code: str, operation: Callable[[], Awaitable[object]]) -> CallRefusedError:
    with pytest.raises(CallRefusedError) as refused:
        await operation()
    assert refused.value.code == code
    return refused.value


# A call


@pytest.mark.asyncio
async def test_two_people_starting_at_once_get_the_same_call(world: World) -> None:
    """A room has at most one live call, and one row, however the joins interleave."""
    ann_call, bob_call = await asyncio.gather(world.service.join(ROOM, ANN), world.service.join(ROOM, BOB))
    other = await world.service.join(OTHER_ROOM, CAROL)

    assert ann_call.id == bob_call.id
    assert other.id != ann_call.id
    assert sorted(room_id for room_id, _ in world.room.rows) == [OTHER_ROOM, ROOM]
    live = await world.service.join(ROOM, ANN)
    assert live.id == ann_call.id
    assert live.members == (ANN, BOB)
    assert live.room_id == ROOM


# Who is in it


@pytest.mark.asyncio
async def test_an_app_is_in_the_call_while_it_checks_in_at_least_every_60_seconds(world: World) -> None:
    """Joining is also the heartbeat; an app silent for more than 60 seconds is out."""
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)

    world.clock.advance(40)
    await world.service.join(ROOM, BOB)
    world.clock.advance(20)
    await world.service.tick()
    assert (await world.service.live(ROOM, BOB)).call == Call(
        id=call.id,
        room_id=ROOM,
        row_event_id="$row-1",
        started_at=START,
        ended_at=None,
        members=(ANN, BOB),
        holds=(),
        transcribed_by=(ANN, BOB),
    )

    world.clock.advance(1)
    await world.service.tick()
    live = (await world.service.live(ROOM, BOB)).call
    assert live is not None
    assert (live.members, live.transcribed_by) == ((BOB,), (BOB,))


@pytest.mark.asyncio
async def test_any_request_from_an_app_to_the_call_is_a_check_in(world: World) -> None:
    """Speech and Pause keep a person in the call as a heartbeat does."""
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)

    world.clock.advance(50)
    await world.say(call.id, ANN)
    await world.service.pause(call.id, BOB, holding=False)
    world.clock.advance(50)
    await world.service.tick()

    live = (await world.service.live(ROOM, ANN)).call
    assert live is not None
    assert live.members == (ANN, BOB)
    world.clock.advance(11)
    await world.service.tick()
    assert (await world.service.live(ROOM, ANN)).call is None


@pytest.mark.asyncio
async def test_people_in_the_matrixrtc_call_without_this_feature_are_listed_but_not_transcribed(world: World) -> None:
    """Someone on another Matrix app is in the call; only people whose apps check in are transcribed."""
    world.room.rtc = {CAROL: START - 30, ANN: START - 1}
    call = await world.service.join(ROOM, ANN)

    assert call.members == (ANN, CAROL)
    assert call.transcribed_by == (ANN,)
    await _refused("forbidden", lambda: world.say(call.id, CAROL))


@pytest.mark.asyncio
async def test_a_crashed_apps_leftover_membership_does_not_keep_its_call_live(world: World) -> None:
    """An app that stops checking in without leaving is out after 60 seconds, its MatrixRTC membership with it."""
    world.room.rtc = {ANN: START - 1}
    call = await world.service.join(ROOM, ANN)
    world.clock.advance(20)
    await world.service.join(ROOM, ANN)

    world.clock.advance(60)
    await world.service.tick()
    assert (await world.service.live(ROOM, ANN)).call is not None

    with world.service.listen(ROOM, world.events.append):
        world.clock.advance(1)
        await world.service.tick()

    assert (await world.service.live(ROOM, ANN)).call is None
    assert world.events == [CallEnded(call_id=call.id)]


@pytest.mark.asyncio
async def test_a_leftover_membership_does_not_count_in_a_later_call_but_a_new_one_does(world: World) -> None:
    """A MatrixRTC membership older than that person's app's last call in the room never keeps a call live."""
    world.room.rtc = {ANN: START - 1}
    await world.service.join(ROOM, ANN)
    world.clock.advance(61)
    await world.service.tick()
    assert (await world.service.live(ROOM, BOB)).call is None

    world.clock.advance(600)
    later = await world.service.join(ROOM, BOB)
    assert later.members == (BOB,)
    await world.service.leave(later.id, BOB)
    assert (await world.service.live(ROOM, BOB)).call is None

    world.room.rtc = {ANN: world.clock.now + 5}
    world.clock.advance(10)
    third = await world.service.join(ROOM, BOB)
    assert third.members == (ANN, BOB)
    await world.service.leave(third.id, BOB)
    live = (await world.service.live(ROOM, BOB)).call
    assert live is not None
    assert (live.id, live.members, live.transcribed_by) == (third.id, (ANN,), ())


@pytest.mark.asyncio
async def test_an_app_that_leaves_is_out_at_once(world: World) -> None:
    """Leaving takes the person out of the call immediately, even with their MatrixRTC membership still there."""
    world.room.rtc = {ANN: START - 1, BOB: START - 1}
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)
    world.clock.advance(5)

    with world.service.listen(ROOM, world.events.append):
        await world.service.leave(call.id, ANN)

    [update] = world.events
    assert isinstance(update, CallUpdated)
    assert (update.call.members, update.call.transcribed_by) == ((BOB,), (BOB,))
    await _refused("forbidden", lambda: world.say(call.id, ANN))


@pytest.mark.asyncio
async def test_a_restart_does_not_end_a_call_whose_apps_check_in_again(world: World) -> None:
    """Presence is kept on disk, and apps get a full 60 seconds after a restart to check in again."""
    call = await world.service.join(ROOM, ANN)
    world.clock.advance(90)

    world.restart()
    await world.service.tick()
    world.clock.advance(30)
    await world.service.tick()
    again = await world.service.join(ROOM, ANN)

    assert again.id == call.id
    assert again.members == (ANN,)
    world.clock.advance(61)
    await world.service.tick()
    assert (await world.service.live(ROOM, ANN)).call is None


@pytest.mark.parametrize("restarted", [True, False], ids=["after-a-restart", "while-running"])
@pytest.mark.asyncio
async def test_a_room_that_cannot_be_read_for_a_while_ends_no_call(world: World, *, restarted: bool) -> None:
    """Apps cannot check in while the room cannot be read; once it can, they get a full 60 seconds to check in again."""
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)
    if restarted:
        world.restart()
    world.room.readable = False
    for _ in range(6):
        world.clock.advance(15)
        await world.service.tick()
    await _refused("unavailable", lambda: world.service.join(ROOM, ANN))

    world.room.readable = True
    with world.service.listen(ROOM, world.events.append):
        await world.service.tick()
        world.clock.advance(20)
        again = await world.service.join(ROOM, ANN)

    assert not any(isinstance(event, CallEnded) for event in world.events)
    assert (again.id, again.members) == (call.id, (ANN, BOB))
    world.clock.advance(41)
    await world.service.tick()
    live = (await world.service.live(ROOM, ANN)).call
    assert live is not None
    assert (live.id, live.members) == (call.id, (ANN,))


# Ending


@pytest.mark.asyncio
async def test_the_call_ends_when_the_last_person_leaves(world: World) -> None:
    """Leaving checks at once whether anyone is still in; the last one out ends the call."""
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)
    world.clock.advance(30)
    await world.service.leave(call.id, ANN)
    assert (await world.service.live(ROOM, BOB)).call is not None

    with world.service.listen(ROOM, world.events.append):
        await world.service.leave(call.id, BOB)

    assert world.events == [CallEnded(call_id=call.id)]
    assert (await world.service.live(ROOM, BOB)).call is None
    ended = world.store.call(call.id)
    assert ended is not None
    assert (ended.ended_at, ended.transcript_due_at) == (START + 30, START + 50)
    await world.service.leave(call.id, BOB)


@pytest.mark.asyncio
async def test_a_live_call_is_checked_every_15_seconds(world: World) -> None:
    """The check runs at most 15 seconds apart, sooner when a transcript message falls due."""
    assert await world.service.tick() == 15
    call_id = await world.end_by_leaving(ANN)

    assert await world.service.tick() == 15
    world.clock.advance(10)
    assert await world.service.tick() == 10
    world.clock.advance(10)
    assert await world.service.tick() == 15
    assert len(world.room.transcripts) == 1
    assert world.room.transcripts[0].call_id == call_id


@pytest.mark.asyncio
async def test_a_failed_state_read_never_ends_a_call(world: World) -> None:
    """While the room's MatrixRTC state cannot be read, nobody being in the call is not believed."""
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)
    world.room.rtc = None
    await world.service.leave(call.id, ANN)
    world.clock.advance(61)
    await world.service.tick()
    await world.service.leave(call.id, BOB)

    assert (await world.service.live(ROOM, ANN)).call is not None
    world.room.rtc = {}
    await world.service.tick()
    assert (await world.service.live(ROOM, ANN)).call is None


# The row


@pytest.mark.asyncio
async def test_the_row_is_posted_when_the_call_starts(world: World) -> None:
    """The router posts the call's row once, and the call carries its event ID."""
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)

    assert world.room.rows == [(ROOM, call.id)]
    assert call.row_event_id == "$row-1"
    stored = world.store.call(call.id)
    assert stored is not None
    assert stored.row_event_id == "$row-1"


@pytest.mark.asyncio
async def test_the_row_is_posted_once_the_router_is_ready(world: World) -> None:
    """A call started while the router is not ready gets its row on a later check, announced to the room."""
    world.room.ready = False
    call = await world.service.join(ROOM, ANN)
    await world.service.tick()
    assert call.row_event_id is None
    assert world.room.rows == []

    world.room.ready = True
    with world.service.listen(ROOM, world.events.append):
        await world.service.tick()

    assert world.room.rows == [(ROOM, call.id)]
    [update] = world.events
    assert isinstance(update, CallUpdated)
    assert update.call.row_event_id == "$row-1"
    await world.service.tick()
    assert world.room.rows == [(ROOM, call.id)]


@pytest.mark.asyncio
async def test_the_row_is_edited_when_the_call_ends_and_again_once_its_transcript_is_posted(world: World) -> None:
    """The end says how long the call lasted at once; the transcript message adds where to find it."""
    call = await world.service.join(ROOM, ANN)
    world.clock.advance(50)
    await world.say(call.id, ANN)
    world.clock.advance(70)
    await world.service.leave(call.id, ANN)

    ended = EndedRow(
        room_id=ROOM,
        call_id=call.id,
        row_event_id="$row-1",
        body="Call ended · 2 min",
        ended_at=START + 120,
        duration_seconds=120,
        transcript_event_id=None,
    )
    assert world.room.row_edits == [ended]
    world.clock.advance(20)
    await world.service.tick()

    assert len(world.room.transcripts) == 1
    assert world.room.row_edits == [ended, replace(ended, transcript_event_id="$transcript-1")]
    world.restart()
    world.clock.advance(120)
    await world.service.tick()
    assert len(world.room.row_edits) == 2


@pytest.mark.asyncio
async def test_the_row_of_a_call_without_speech_is_edited_only_for_its_end(world: World) -> None:
    """With nothing to post, the edit for the end is the row's last."""
    call = await world.service.join(ROOM, ANN)
    world.clock.advance(30)
    await world.service.leave(call.id, ANN)
    world.clock.advance(20)
    await world.service.tick()
    world.clock.advance(60)
    await world.service.tick()

    [ended] = world.room.row_edits
    assert (ended.body, ended.duration_seconds, ended.transcript_event_id) == ("Call ended · 1 min", 30, None)
    assert world.room.transcripts == []


@pytest.mark.asyncio
async def test_a_failed_row_edit_is_retried_by_the_loop_a_minute_later(world: World) -> None:
    """An edit that could not be sent is tried again by a later check, after a restart too, and sent once."""
    call = await world.service.join(ROOM, ANN)
    world.room.ready = False
    world.clock.advance(30)
    await world.service.leave(call.id, ANN)
    await world.service.tick()
    assert world.room.row_edits == []

    world.room.ready = True
    world.clock.advance(15)
    await world.service.tick()
    assert world.room.row_edits == []
    world.clock.advance(45)
    world.room.ready = False
    world.restart()
    await world.service.tick()
    world.room.ready = True
    world.clock.advance(60)
    await world.service.tick()
    world.clock.advance(60)
    await world.service.tick()

    [ended] = world.room.row_edits
    assert ended.call_id == call.id
    assert ended.transcript_event_id is None


@pytest.mark.asyncio
async def test_a_row_posted_after_the_end_is_edited_once_with_its_transcript(world: World) -> None:
    """A row that only goes up with the transcript message gets one edit, saying both the end and the message."""
    world.room.ready = False
    call_id = await world.end_by_leaving(ANN)
    world.clock.advance(20)
    await world.service.tick()

    world.room.ready = True
    world.clock.advance(60)
    await world.service.tick()

    assert world.room.rows == [(ROOM, call_id)]
    [edit] = world.room.row_edits
    assert (edit.row_event_id, edit.transcript_event_id) == ("$row-1", "$transcript-1")


@pytest.mark.asyncio
async def test_an_edit_for_the_end_is_never_sent_after_the_one_with_the_transcript(world: World) -> None:
    """While the row's end is still being edited, the transcript's edit waits for it, so the last edit is the latest."""
    call = await world.service.join(ROOM, ANN)
    world.clock.advance(5)
    await world.say(call.id, ANN)
    world.room.edit_gate = asyncio.Event()
    leaving = asyncio.create_task(world.service.leave(call.id, ANN))
    await world.room.edit_started.wait()
    world.clock.advance(20)
    await world.service.tick()
    assert len(world.room.transcripts) == 1
    assert world.room.row_edits == []

    world.room.edit_gate.set()
    await leaving

    assert [edit.transcript_event_id for edit in world.room.row_edits] == [None, "$transcript-1"]


# The transcript message


@pytest.mark.asyncio
async def test_the_transcript_message_is_posted_about_20_seconds_after_the_end(world: World) -> None:
    """The message goes in the row's thread with the notes, the speech as turns, and the call's length."""
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)
    world.clock.advance(50)
    world.transcriber.text = "Let's ship on Friday."
    await world.say(call.id, ANN, ago=10)
    world.transcriber.text = None
    await world.say(call.id, BOB, ago=8)
    world.transcriber.text = "I'll write the release notes."
    await world.say(call.id, BOB, ago=4)
    await world.service.pause(call.id, ANN, holding=True)
    world.clock.advance(30)
    await world.service.leave(call.id, ANN)
    await world.service.leave(call.id, BOB)

    world.clock.advance(19)
    await world.service.tick()
    assert world.room.transcripts == []
    world.clock.advance(1)
    await world.service.tick()

    assert world.room.transcripts == [
        TranscriptMessage(
            room_id=ROOM,
            call_id=call.id,
            row_event_id="$row-1",
            body="## About\nThe launch.",
            transcript=CallTranscript(
                call_id=call.id,
                room_id=ROOM,
                duration_seconds=80,
                turns=(
                    _Turn(sender=ANN, text="Let's ship on Friday."),
                    _Turn(sender=BOB, text="I'll write the release notes."),
                ),
                names={ANN: "Ann", BOB: "Bob"},
            ),
        ),
    ]
    assert world.notes.transcripts == [world.room.transcripts[0].transcript]
    assert world.store.lines(call.id) == []
    world.clock.advance(120)
    await world.service.tick()
    assert len(world.room.transcripts) == 1


@pytest.mark.asyncio
async def test_consecutive_stretches_of_one_speaker_are_one_turn(world: World) -> None:
    """A speaker's stretches in a row read as one turn; another speaker or a gap starts a new one."""
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)
    world.clock.advance(60)
    for user_id, text, ago in [(ANN, "One.", 50), (ANN, "Two.", 45), (BOB, "Three.", 40), (ANN, "Four.", 35)]:
        world.transcriber.text = text
        await world.say(call.id, user_id, ago=ago)
    await world.service.pause(call.id, BOB, holding=True)
    await world.service.pause(call.id, BOB, holding=False)
    world.clock.advance(5)
    world.transcriber.text = "Five."
    await world.say(call.id, ANN, ago=2, seconds=1)
    await world.service.leave(call.id, ANN)
    await world.service.leave(call.id, BOB)
    world.clock.advance(20)

    await world.service.tick()

    [message] = world.room.transcripts
    assert message.transcript.turns == (
        _Turn(sender=ANN, text="One. Two."),
        _Turn(sender=BOB, text="Three."),
        _Turn(sender=ANN, text="Four."),
        _Turn(sender=ANN, text="Five."),
    )


@pytest.mark.asyncio
async def test_without_a_notes_model_the_message_says_how_long_the_call_was(world: World) -> None:
    """No notes model: the body is a one-line summary of the call's length."""
    world.notes.markdown = None
    call = await world.service.join(ROOM, ANN)
    for _ in range(36):
        world.clock.advance(20)
        await world.service.join(ROOM, ANN)
    await world.say(call.id, ANN)
    await world.service.leave(call.id, ANN)
    world.clock.advance(20)

    await world.service.tick()

    [message] = world.room.transcripts
    assert message.body == "Call · 12 min"
    assert message.transcript.duration_seconds == 720


@pytest.mark.asyncio
async def test_a_call_with_no_speech_posts_nothing(world: World) -> None:
    """Without anything transcribed there is no message, and the call's lines still go."""
    call = await world.service.join(ROOM, ANN)
    world.clock.advance(30)
    world.transcriber.text = None
    await world.say(call.id, ANN)
    await world.service.pause(call.id, ANN, holding=True)
    await world.service.leave(call.id, ANN)
    world.clock.advance(20)

    await world.service.tick()

    assert world.room.transcripts == []
    assert world.notes.transcripts == []
    assert world.store.lines(call.id) == []
    stored = world.store.call(call.id)
    assert stored is not None
    assert stored.transcript_done_at == START + 50


@pytest.mark.asyncio
async def test_failed_notes_are_retried_once_after_60_seconds_then_the_transcript_goes_without_them(
    world: World,
) -> None:
    """Notes that fail twice leave the transcript with the one-line summary instead."""
    world.notes.failing = True
    await world.end_by_leaving(ANN)
    world.clock.advance(20)

    await world.service.tick()
    assert world.room.transcripts == []
    world.clock.advance(59)
    await world.service.tick()
    assert world.room.transcripts == []
    world.clock.advance(1)
    await world.service.tick()

    [message] = world.room.transcripts
    assert message.body == "Call · 1 min"
    assert len(world.notes.transcripts) == 2


@pytest.mark.asyncio
async def test_notes_that_fail_once_are_used_when_the_retry_works(world: World) -> None:
    """The one retry writes the notes again and posts them."""
    world.notes.failing = True
    await world.end_by_leaving(ANN)
    world.clock.advance(20)
    await world.service.tick()

    world.notes.failing = False
    world.clock.advance(60)
    await world.service.tick()

    [message] = world.room.transcripts
    assert message.body == "## About\nThe launch."


@pytest.mark.asyncio
async def test_a_failed_post_is_retried_after_60_seconds_with_the_notes_already_written(world: World) -> None:
    """A post that fails is tried again a minute later, without asking the notes model twice."""
    world.room.failed_transcript_posts = 1
    await world.end_by_leaving(ANN)
    world.clock.advance(20)
    await world.service.tick()
    assert world.room.transcripts == []

    world.clock.advance(60)
    await world.service.tick()

    [message] = world.room.transcripts
    assert message.body == "## About\nThe launch."
    assert len(world.notes.transcripts) == 1


@pytest.mark.asyncio
async def test_a_router_not_ready_at_the_end_still_gets_its_row_and_transcript_posted(world: World) -> None:
    """However long the router is away, the row and then the transcript message are posted once it is back."""
    world.room.ready = False
    call_id = await world.end_by_leaving(ANN, BOB)
    for _ in range(5):
        world.clock.advance(60)
        await world.service.tick()
    assert world.room.rows == []
    assert world.room.transcripts == []

    world.room.ready = True
    world.clock.advance(60)
    await world.service.tick()
    world.clock.advance(60)
    await world.service.tick()

    assert world.room.rows == [(ROOM, call_id)]
    [message] = world.room.transcripts
    assert message.row_event_id == "$row-1"
    assert message.body == "## About\nThe launch."
    assert len(world.notes.transcripts) == 1
    assert [turn.sender for turn in message.transcript.turns] == [ANN, BOB]


@pytest.mark.asyncio
async def test_notes_wait_until_the_room_can_be_read_so_they_name_people_by_name(world: World) -> None:
    """Notes are not written while names cannot be read, which uses up none of their attempts; then they are."""
    world.room.ready = False
    await world.end_by_leaving(ANN, BOB)
    for _ in range(5):
        world.clock.advance(60)
        await world.service.tick()
    assert world.notes.transcripts == []

    world.room.ready = True
    world.clock.advance(60)
    await world.service.tick()

    [transcript] = world.notes.transcripts
    assert transcript.names == {ANN: "Ann", BOB: "Bob"}
    [message] = world.room.transcripts
    assert message.body == "## About\nThe launch."


@pytest.mark.asyncio
async def test_the_live_call_reads_without_names_while_they_cannot_be_read(world: World) -> None:
    """A live call whose names cannot be read is still answered, with no name for anyone."""
    await world.service.join(ROOM, ANN)
    world.room.ready = False

    live = await world.service.live(ROOM, ANN)

    assert live.names == {ANN: None}


@pytest.mark.asyncio
async def test_the_transcript_message_is_posted_once(world: World) -> None:
    """Checks running together post it once, and a restart after posting does not post it again."""
    await world.end_by_leaving(ANN)
    world.clock.advance(20)

    await asyncio.gather(world.service.tick(), world.service.tick())
    world.restart()
    world.clock.advance(300)
    await world.service.tick()

    assert len(world.room.transcripts) == 1


@pytest.mark.asyncio
async def test_a_transcript_due_before_a_restart_is_posted_after_it(world: World) -> None:
    """What was said is on disk, so the message survives a restart before it was posted."""
    call_id = await world.end_by_leaving(ANN)
    world.restart()
    world.clock.advance(20)

    await world.service.tick()

    [message] = world.room.transcripts
    assert message.call_id == call_id
    assert message.transcript.turns == (_Turn(sender=ANN, text="Hello there"),)


# Pause


@pytest.mark.asyncio
async def test_while_anyone_holds_pause_speech_is_refused(world: World) -> None:
    """A hold stops everyone's speech, including a stretch said before it began but arriving during it."""
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)
    world.clock.advance(30)

    held = await world.service.pause(call.id, ANN, holding=True)
    assert held.holds == (ANN,)
    await _refused("paused", lambda: world.say(call.id, BOB, ago=20))
    await _refused("paused", lambda: world.say(call.id, ANN, ago=20))

    world.clock.advance(10)
    released = await world.service.pause(call.id, ANN, holding=False)
    assert released.holds == ()
    world.clock.advance(5)
    line = await world.say(call.id, BOB, ago=3)
    assert line.kind == "speech"


@pytest.mark.asyncio
async def test_a_pause_lasts_until_the_last_hold_is_released_and_shows_once_as_a_gap(world: World) -> None:
    """Overlapping holds are one pause; the gap is announced once, from the first hold to the last release."""
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)
    world.clock.advance(30)

    with world.service.listen(ROOM, world.events.append):
        await world.service.pause(call.id, ANN, holding=True)
        world.clock.advance(5)
        both = await world.service.pause(call.id, BOB, holding=True)
        world.clock.advance(5)
        await world.service.pause(call.id, ANN, holding=False)
        await _refused("paused", lambda: world.say(call.id, ANN, ago=0.5, seconds=0.5))
        world.clock.advance(5)
        await world.service.pause(call.id, BOB, holding=False)
        await world.service.pause(call.id, BOB, holding=False)

    assert both.holds == (ANN, BOB)
    gaps = [event.line for event in world.events if isinstance(event, LineAdded)]
    assert [(gap.user_id, gap.said_at, gap.seconds, gap.kind, gap.reason) for gap in gaps] == [
        (ANN, START + 30, 15.0, "gap", "paused"),
    ]
    assert [event.call.holds for event in world.events if isinstance(event, CallUpdated)] == [
        (ANN,),
        (ANN, BOB),
        (BOB,),
        (),
    ]
    assert world.store.lines(call.id) == gaps


@pytest.mark.asyncio
async def test_a_stretch_that_overlapped_a_pause_is_refused_whenever_it_arrives(world: World) -> None:
    """Speech said during any part of a pause never gets in, even uploaded after it; speech after it does."""
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)
    world.clock.advance(28)
    before = await world.say(call.id, BOB, ago=1, seconds=1)
    world.clock.advance(2)
    await world.service.pause(call.id, ANN, holding=True)
    world.clock.advance(10)
    await world.service.pause(call.id, ANN, holding=False)
    world.clock.advance(20)
    await world.service.join(ROOM, BOB)

    await _refused("paused", lambda: world.say(call.id, BOB, ago=31, seconds=2))
    await _refused("paused", lambda: world.say(call.id, BOB, ago=25, seconds=2))
    await _refused("paused", lambda: world.say(call.id, BOB, ago=21, seconds=2))
    await _refused("paused", lambda: world.say(call.id, BOB, ago=33, seconds=2))
    after = await world.say(call.id, BOB, ago=19, seconds=2)

    assert (before.kind, after.kind) == ("speech", "speech")
    await world.service.leave(call.id, ANN)
    await world.service.leave(call.id, BOB)
    await _refused("paused", lambda: world.say(call.id, BOB, ago=25, seconds=2))


@pytest.mark.asyncio
async def test_leaving_releases_a_hold(world: World) -> None:
    """Someone who leaves while holding Pause no longer holds it, and the pause shows as a gap."""
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)
    await world.service.pause(call.id, ANN, holding=True)
    world.clock.advance(10)

    await world.service.leave(call.id, ANN)

    live = await world.service.live(ROOM, BOB)
    assert live.call is not None
    assert live.call.holds == ()
    assert [(line.reason, line.seconds) for line in live.lines] == [("paused", 10.0)]
    world.clock.advance(5)
    assert (await world.say(call.id, BOB, ago=1, seconds=0.5)).kind == "speech"


@pytest.mark.asyncio
async def test_the_hold_of_an_app_that_stopped_checking_in_is_released(world: World) -> None:
    """A crashed app cannot keep the call paused: its hold goes when it is out of the call."""
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)
    await world.service.pause(call.id, ANN, holding=True)
    for _ in range(4):
        world.clock.advance(20)
        await world.service.join(ROOM, BOB)

    await world.service.tick()

    live = await world.service.live(ROOM, BOB)
    assert live.call is not None
    assert (live.call.members, live.call.holds) == ((BOB,), ())
    assert [(line.user_id, line.reason, line.seconds) for line in live.lines] == [(ANN, "paused", 80.0)]


@pytest.mark.asyncio
async def test_holds_survive_a_restart_and_the_pause_is_marked_once(world: World) -> None:
    """After a restart speech is still refused during a hold, and its release writes the one gap from its start."""
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)
    world.clock.advance(10)
    await world.service.pause(call.id, ANN, holding=True)
    world.clock.advance(10)

    world.restart()
    await _refused("paused", lambda: world.say(call.id, BOB, ago=2))
    live = await world.service.live(ROOM, BOB)
    assert live.call is not None
    assert live.call.holds == (ANN,)
    world.clock.advance(10)
    await world.service.pause(call.id, ANN, holding=False)
    await world.service.pause(call.id, ANN, holding=False)

    lines = (await world.service.live(ROOM, BOB)).lines
    assert [(line.reason, line.said_at, line.seconds) for line in lines] == [("paused", START + 10, 20.0)]


@pytest.mark.asyncio
async def test_only_someone_in_the_call_can_take_a_hold(world: World) -> None:
    """A room member whose app is not in the call cannot pause it; releasing a hold they lack does nothing."""
    call = await world.service.join(ROOM, ANN)

    await _refused("forbidden", lambda: world.service.pause(call.id, CAROL, holding=True))
    unchanged = await world.service.pause(call.id, CAROL, holding=False)

    assert (unchanged.members, unchanged.holds) == ((ANN,), ())
    await world.service.leave(call.id, ANN)
    await _refused("forbidden", lambda: world.service.pause(call.id, ANN, holding=True))
    ended = await world.service.pause(call.id, ANN, holding=False)
    assert (ended.ended_at, ended.holds) == (START, ())


# Speech


@pytest.mark.asyncio
async def test_speech_from_someone_in_the_call_becomes_a_line(world: World) -> None:
    """A stretch is transcribed under its speaker, timed by when it was said and its header's length."""
    call = await world.service.join(ROOM, ANN)
    world.clock.advance(30)

    with world.service.listen(ROOM, world.events.append):
        line = await world.say(call.id, ANN, ago=5, seconds=2.5)

    assert (line.call_id, line.user_id, line.said_at, line.seconds, line.kind, line.text, line.reason) == (
        call.id,
        ANN,
        START + 25,
        2.5,
        "speech",
        "Hello there",
        None,
    )
    assert world.transcriber.requests == [(call.id, ANN, 2.5)]
    assert world.events == [LineAdded(line=line)]
    assert world.store.lines(call.id) == [line]


@pytest.mark.asyncio
async def test_a_stretch_is_never_placed_after_it_arrived_or_before_the_call(world: World) -> None:
    """A device clock ahead or behind cannot move a stretch past its arrival or before the call began."""
    call = await world.service.join(ROOM, ANN)
    world.clock.advance(30)

    ahead = await world.say(call.id, ANN, ago=-10, seconds=2)
    behind = await world.say(call.id, ANN, ago=3_600, seconds=2)

    assert ahead.said_at == START + 28
    assert behind.said_at == START


@pytest.mark.asyncio
async def test_speech_from_someone_not_in_the_call_is_refused(world: World) -> None:
    """Only a person whose app is in the call may add speech to it."""
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)

    await _refused("forbidden", lambda: world.say(call.id, CAROL))
    await world.service.leave(call.id, BOB)
    await _refused("forbidden", lambda: world.say(call.id, BOB))
    world.clock.advance(61)
    await _refused("forbidden", lambda: world.say(call.id, ANN))
    assert world.transcriber.requests == []


@pytest.mark.parametrize(
    ("audio", "code"),
    [
        (_wav(30.5), "too-large"),
        (_wav(16, rate=48_000), "too-large"),
        (b"OggS" + b"\x00" * 100, "invalid"),
        (b"", "invalid"),
    ],
    ids=["longer-than-30s", "larger-than-1MB", "not-wav", "empty"],
)
@pytest.mark.asyncio
async def test_speech_must_be_a_wav_of_at_most_30_seconds_and_1_mb(world: World, audio: bytes, code: str) -> None:
    """Audio that is too long, too big or not a 16-bit mono WAV is refused before anything is transcribed."""
    call = await world.service.join(ROOM, ANN)
    world.clock.advance(40)

    await _refused(code, lambda: world.service.speech(call.id, ANN, audio=audio, started_at=world.clock.now - 31))

    assert world.transcriber.requests == []
    line = await world.say(call.id, ANN, ago=30, seconds=30)
    assert line.seconds == 30.0


@pytest.mark.asyncio
async def test_one_stretch_in_flight_per_person(world: World) -> None:
    """A second stretch while the first is being transcribed is refused with when to try again."""
    world.transcriber.gate = asyncio.Event()
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)
    world.clock.advance(30)
    first = asyncio.create_task(world.say(call.id, ANN))
    await world.transcriber.started.wait()

    async with asyncio.timeout(5):  # Let in, the second stretch would wait at the gate with the first.
        refused = await _refused("too-many-requests", lambda: world.say(call.id, ANN, ago=2.5))
    assert refused.retry_after == 3
    world.transcriber.gate.set()
    bob = await world.say(call.id, BOB)

    assert (await first).user_id == ANN
    assert bob.user_id == BOB
    assert (await world.say(call.id, ANN, ago=2.5)).kind == "speech"


@pytest.mark.asyncio
async def test_at_most_240_stretches_a_minute_per_person(world: World) -> None:
    """The 241st stretch within a minute is refused until the oldest of them is a minute old."""
    call = await world.service.join(ROOM, ANN)
    world.clock.advance(30)
    for _ in range(240):
        await world.say(call.id, ANN, ago=1, seconds=0.25)
        world.clock.advance(0.125)

    refused = await _refused("too-many-requests", lambda: world.say(call.id, ANN, ago=1, seconds=0.25))
    assert refused.retry_after == 30
    await world.service.join(ROOM, BOB)
    assert (await world.say(call.id, BOB, ago=1, seconds=0.25)).kind == "speech"
    world.clock.advance(29)
    refused = await _refused("too-many-requests", lambda: world.say(call.id, ANN, ago=1, seconds=0.25))
    assert refused.retry_after == 1
    world.clock.advance(1)
    assert (await world.say(call.id, ANN, ago=1, seconds=0.25)).kind == "speech"


@pytest.mark.asyncio
async def test_a_stretch_that_fails_to_transcribe_becomes_a_gap(world: World) -> None:
    """Speech-to-text failing leaves a "not transcribed" gap where the stretch was, and the call goes on."""
    world.transcriber.text = None
    call = await world.service.join(ROOM, ANN)
    world.clock.advance(30)

    gap = await world.say(call.id, ANN, ago=4, seconds=3)

    assert (gap.user_id, gap.said_at, gap.seconds, gap.kind, gap.text, gap.reason) == (
        ANN,
        START + 26,
        3.0,
        "gap",
        None,
        "not-transcribed",
    )
    world.transcriber.text = "Still here."
    assert (await world.say(call.id, ANN, ago=1, seconds=0.5)).text == "Still here."


@pytest.mark.asyncio
async def test_a_stretch_in_which_speech_to_text_heard_no_words_adds_nothing(world: World) -> None:
    """No words is not a failure: no line and no gap is written or announced, and the call goes on."""
    world.transcriber.text = ""
    call = await world.service.join(ROOM, ANN)
    world.clock.advance(30)

    with world.service.listen(ROOM, world.events.append):
        nothing = await world.service.speech(call.id, ANN, audio=_wav(1.0), started_at=world.clock.now - 2)

    assert nothing is None
    assert world.store.lines(call.id) == []
    assert world.events == []
    world.transcriber.text = "Still here."
    assert (await world.say(call.id, ANN, ago=1, seconds=0.5)).text == "Still here."


# Late speech


@pytest.mark.asyncio
async def test_speech_said_before_the_end_is_accepted_until_the_transcript_is_posted(world: World) -> None:
    """A late upload of a stretch said in the call joins the transcript; one said after the end never does."""
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)
    world.clock.advance(60)
    await world.service.leave(call.id, ANN)
    await world.service.leave(call.id, BOB)
    world.clock.advance(10)

    world.transcriber.text = "Said just before the end."
    late = await world.say(call.id, ANN, ago=12, seconds=2)
    await _refused("forbidden", lambda: world.say(call.id, ANN, ago=9, seconds=2))
    await _refused("forbidden", lambda: world.say(call.id, CAROL, ago=12, seconds=2))
    world.clock.advance(10)
    await world.service.tick()
    await _refused("forbidden", lambda: world.say(call.id, BOB, ago=25, seconds=2))

    assert late.said_at == START + 58
    [message] = world.room.transcripts
    assert message.transcript.turns == (_Turn(sender=ANN, text="Said just before the end."),)


@pytest.mark.asyncio
async def test_the_transcript_waits_for_a_stretch_still_being_transcribed(world: World) -> None:
    """A stretch from the call still at speech-to-text when the message falls due is not left out."""
    call = await world.service.join(ROOM, ANN)
    world.clock.advance(60)
    await world.service.leave(call.id, ANN)
    world.transcriber.gate = asyncio.Event()
    world.clock.advance(15)
    upload = asyncio.create_task(world.say(call.id, ANN, ago=17, seconds=1))
    await world.transcriber.started.wait()
    world.clock.advance(5)

    assert await world.service.tick() == 1
    assert world.room.transcripts == []
    world.transcriber.gate.set()
    await upload
    await world.service.tick()

    [message] = world.room.transcripts
    assert message.transcript.turns == (_Turn(sender=ANN, text="Hello there"),)


# Who may do what


@pytest.mark.asyncio
async def test_starting_or_joining_needs_permission_to_post_but_a_heartbeat_does_not(world: World) -> None:
    """Power levels are checked for someone coming in, not for someone already in the call."""
    world.room.posters = {ANN}
    await _refused("forbidden", lambda: world.service.join(ROOM, BOB))
    assert world.store.live_call(ROOM) is None

    call = await world.service.join(ROOM, ANN)
    world.room.posters = set()
    world.clock.advance(20)
    assert (await world.service.join(ROOM, ANN)).id == call.id
    await _refused("forbidden", lambda: world.service.join(ROOM, BOB))

    world.clock.advance(61)
    await _refused("forbidden", lambda: world.service.join(ROOM, ANN))


@pytest.mark.asyncio
async def test_people_outside_the_room_cannot_read_or_act_on_its_call(world: World) -> None:
    """A call's room comes from the call; to anyone outside it the call does not exist."""
    call = await world.service.join(ROOM, ANN)
    world.room.posters.add(OUTSIDER)

    await _refused("not-found", lambda: world.service.join(ROOM, OUTSIDER))
    await _refused("not-found", lambda: world.service.live(ROOM, OUTSIDER))
    await _refused("not-found", lambda: world.service.leave(call.id, OUTSIDER))
    await _refused("not-found", lambda: world.service.pause(call.id, OUTSIDER, holding=True))
    await _refused("not-found", lambda: world.say(call.id, OUTSIDER))
    for operation in (
        lambda: world.service.leave("unknown", ANN),
        lambda: world.service.pause("unknown", ANN, holding=False),
        lambda: world.say("unknown", ANN),
    ):
        await _refused("not-found", operation)

    world.room.members.discard(ANN)
    await _refused("not-found", lambda: world.say(call.id, ANN))
    assert world.transcriber.requests == []


@pytest.mark.asyncio
async def test_a_room_whose_membership_cannot_be_read_is_unavailable(world: World) -> None:
    """When the router cannot tell who is in the room, requests are refused as unavailable, not forbidden."""
    call = await world.service.join(ROOM, ANN)
    world.room.readable = False

    await _refused("unavailable", lambda: world.service.join(ROOM, ANN))
    await _refused("unavailable", lambda: world.say(call.id, ANN))
    await _refused("unavailable", lambda: world.service.live(ROOM, ANN))


# The live call and its changes


@pytest.mark.asyncio
async def test_the_live_call_has_its_lines_and_everyones_names(world: World) -> None:
    """Reading the live call gives the call, its lines in order, and the names of everyone in it or in its lines."""
    assert (await world.service.live(ROOM, CAROL)).call is None
    world.room.rtc = {CAROL: START + 1}
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)
    world.clock.advance(30)
    second = await world.say(call.id, BOB, ago=3)
    first = await world.say(call.id, ANN, ago=10)
    await world.service.leave(call.id, BOB)

    live = await world.service.live(ROOM, CAROL)

    assert live.call is not None
    assert (live.call.id, live.call.members, live.call.transcribed_by) == (call.id, (ANN, CAROL), (ANN,))
    assert live.lines == (first, second)
    assert live.names == {ANN: "Ann", BOB: "Bob", CAROL: None}


@pytest.mark.asyncio
async def test_changes_are_announced_after_they_are_written(world: World) -> None:
    """Each event reaches the room's listeners once its change can be read back, and only that room's."""
    seen: list[tuple[CallEvent, object]] = []
    other: list[CallEvent] = []

    def listener(event: CallEvent) -> None:
        store = world.store
        if isinstance(event, CallUpdated):
            seen.append((event, store.call(event.call.id)))
        elif isinstance(event, LineAdded):
            seen.append((event, store.lines(event.line.call_id)))
        else:
            seen.append((event, store.call(event.call_id)))

    with world.service.listen(ROOM, listener), world.service.listen(OTHER_ROOM, other.append):
        call = await world.service.join(ROOM, ANN)
        world.clock.advance(10)
        line = await world.say(call.id, ANN)
        await world.service.leave(call.id, ANN)
    await world.service.join(ROOM, ANN)

    assert len(seen) == 4
    (started, started_row), (row_posted, row_posted_row), said, (ended, ended_row) = seen
    assert started == CallUpdated(call=replace(call, row_event_id=None))
    assert isinstance(started_row, StoredCall)
    assert row_posted == CallUpdated(call=call)
    assert isinstance(row_posted_row, StoredCall)
    assert row_posted_row.row_event_id == call.row_event_id == "$row-1"
    assert said == (LineAdded(line=line), [line])
    assert ended == CallEnded(call_id=call.id)
    assert isinstance(ended_row, StoredCall)
    assert ended_row.ended_at == START + 10
    assert other == []


@pytest.mark.asyncio
async def test_the_store_is_used_off_the_event_loop(world: World) -> None:
    """Every store call runs in a worker thread, so a slow disk never blocks the event loop."""
    loop_thread = threading.get_ident()
    threads: set[int] = set()

    class WatchedStore(CallTranscriptStore):
        def __getattribute__(self, name: str) -> object:
            """Note the thread each public method is called on."""
            attribute = super().__getattribute__(name)
            if callable(attribute) and not name.startswith("_"):

                def watched(*args: object, **kwargs: object) -> object:
                    threads.add(threading.get_ident())
                    return attribute(*args, **kwargs)

                return watched
            return attribute

    world.service = CallTranscriptService(
        WatchedStore(world.tmp_path),
        world.room,
        world.transcriber,
        world.notes,
        clock=world.clock,
    )
    call = await world.service.join(ROOM, ANN)
    await world.say(call.id, ANN)
    await world.service.pause(call.id, ANN, holding=True)
    await world.service.live(ROOM, ANN)
    await world.service.leave(call.id, ANN)
    world.clock.advance(20)
    await world.service.tick()

    assert threads
    assert loop_thread not in threads
    assert len(world.room.transcripts) == 1


@pytest.mark.asyncio
async def test_someone_never_in_the_call_cannot_add_to_it_by_leaving_it(world: World) -> None:
    """A room member who may not post records nothing by leaving, so they cannot add late speech either."""
    world.room.posters = {ANN}
    call = await world.service.join(ROOM, ANN)
    world.clock.advance(10)
    await world.say(call.id, ANN)

    await world.service.leave(call.id, CAROL)
    assert CAROL not in world.store.app_presence(ROOM)
    await world.service.leave(call.id, ANN)
    world.clock.advance(5)
    await _refused("forbidden", lambda: world.say(call.id, CAROL, ago=8))
    world.clock.advance(15)
    await world.service.tick()

    [message] = world.room.transcripts
    assert [turn.sender for turn in message.transcript.turns] == [ANN]


@pytest.mark.asyncio
async def test_someone_only_in_a_later_call_cannot_add_late_speech_to_an_earlier_one(world: World) -> None:
    """Late speech comes only from people whose apps were in that call, not from the room's next call."""
    call = await world.service.join(ROOM, ANN)
    world.clock.advance(10)
    await world.service.leave(call.id, ANN)
    world.clock.advance(2)
    later = await world.service.join(ROOM, BOB)

    assert later.id != call.id
    await _refused("forbidden", lambda: world.say(call.id, BOB, ago=5))
    assert (await world.say(call.id, ANN, ago=5)).call_id == call.id


@pytest.mark.asyncio
async def test_a_stretch_from_a_clock_behind_that_arrives_after_a_hold_is_refused(world: World) -> None:
    """A device clock a second behind cannot place a stretch said during a short pause before it."""
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)
    world.clock.advance(30.5)
    await world.service.pause(call.id, ANN, holding=True)
    world.clock.advance(0.75)
    await world.service.pause(call.id, ANN, holding=False)
    world.clock.advance(0.25)

    # Said from START + 30 to + 31 by the server's clock; Bob's device, a second behind, says it began at + 29.
    await _refused(
        "paused",
        lambda: world.service.speech(call.id, BOB, audio=_wav(1.0), started_at=START + 29),
    )


@pytest.mark.asyncio
async def test_a_slow_row_post_does_not_hold_up_heartbeats_or_end_the_call(world: World) -> None:
    """While the router takes minutes to post the row, heartbeats and checks go on and nothing is posted twice."""
    world.room.row_gate = asyncio.Event()
    first_join = asyncio.create_task(world.service.join(ROOM, ANN))
    await world.room.row_started.wait()

    async with asyncio.timeout(5):
        for _ in range(5):
            world.clock.advance(20)
            heartbeat = await world.service.join(ROOM, ANN)
            await world.service.join(ROOM, BOB)
            await world.service.tick()

    assert heartbeat.ended_at is None
    assert [call.id for call in world.store.live_calls()] == [heartbeat.id]
    assert world.room.row_attempts == 1
    world.room.row_gate.set()
    started = await first_join
    assert (started.id, started.row_event_id) == (heartbeat.id, "$row-1")
    assert world.room.rows == [(ROOM, heartbeat.id)]


@pytest.mark.asyncio
async def test_notes_failing_in_any_way_for_one_call_hold_back_only_that_call(world: World) -> None:
    """Any error from the notes writer is a notes failure: retried once a minute later, then posted without notes."""
    world.notes.failing_rooms = {ROOM}
    first = await world.service.join(ROOM, ANN)
    second = await world.service.join(OTHER_ROOM, BOB)
    world.clock.advance(10)
    await world.say(first.id, ANN)
    await world.say(second.id, BOB)
    await world.service.leave(first.id, ANN)
    await world.service.leave(second.id, BOB)
    world.clock.advance(20)

    await world.service.tick()

    assert [message.call_id for message in world.room.transcripts] == [second.id]
    world.clock.advance(60)
    await world.service.tick()
    assert [message.call_id for message in world.room.transcripts] == [second.id, first.id]
    assert world.room.transcripts[1].body == "Call · 1 min"
    assert len(world.notes.transcripts) == 3


@pytest.mark.asyncio
async def test_one_failing_call_does_not_stop_the_others(world: World) -> None:
    """A bug while posting one call's message leaves it to the retry rules; other calls' messages still go."""
    world.room.crashing_rooms = {ROOM}
    first = await world.service.join(ROOM, ANN)
    second = await world.service.join(OTHER_ROOM, BOB)
    world.clock.advance(10)
    await world.say(first.id, ANN)
    await world.say(second.id, BOB)
    await world.service.leave(first.id, ANN)
    await world.service.leave(second.id, BOB)
    world.clock.advance(20)

    await world.service.tick()

    assert [message.call_id for message in world.room.transcripts] == [second.id]
    world.room.crashing_rooms = set()
    world.clock.advance(59)
    await world.service.tick()
    assert len(world.room.transcripts) == 1
    world.clock.advance(1)
    await world.service.tick()
    assert [message.call_id for message in world.room.transcripts] == [second.id, first.id]


@pytest.mark.asyncio
async def test_a_stuck_transcription_becomes_a_gap_and_does_not_hold_back_the_message(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Speech-to-text that never answers times out into a "not transcribed" gap, and the message still goes."""
    monkeypatch.setattr(service_module, "_TRANSCRIBE_TIMEOUT_SECONDS", 0.01)
    call = await world.service.join(ROOM, ANN)
    world.clock.advance(10)
    await world.say(call.id, ANN)
    world.transcriber.gate = asyncio.Event()

    gap = await world.say(call.id, ANN, ago=1, seconds=0.5)
    await world.service.leave(call.id, ANN)
    world.clock.advance(20)
    await world.service.tick()

    assert (gap.kind, gap.reason) == ("gap", "not-transcribed")
    [message] = world.room.transcripts
    assert message.transcript.turns == (_Turn(sender=ANN, text="Hello there"),)


@pytest.mark.asyncio
async def test_a_failing_listener_neither_fails_the_request_nor_starves_the_others(world: World) -> None:
    """One broken stream listener is logged and skipped."""

    def broken(event: CallEvent) -> None:
        msg = f"cannot send {event}"
        raise RuntimeError(msg)

    with world.service.listen(ROOM, broken), world.service.listen(ROOM, world.events.append):
        call = await world.service.join(ROOM, ANN)

    assert world.events == [CallUpdated(call=replace(call, row_event_id=None)), CallUpdated(call=call)]


@pytest.mark.asyncio
async def test_a_membership_a_few_seconds_older_by_the_homeservers_clock_still_counts(world: World) -> None:
    """The homeserver's clock may lag MindRoom's: a membership dated up to 5 s before the app's last check-in counts."""
    call = await world.service.join(ROOM, ANN)
    await world.service.join(ROOM, BOB)
    world.clock.advance(30)
    await world.service.leave(call.id, ANN)

    world.room.rtc = {ANN: world.clock.now - 3}
    await world.service.tick()
    live = (await world.service.live(ROOM, BOB)).call
    assert live is not None
    assert live.members == (ANN, BOB)

    world.room.rtc = {ANN: world.clock.now - 10}
    await world.service.tick()
    live = (await world.service.live(ROOM, BOB)).call
    assert live is not None
    assert live.members == (BOB,)


@pytest.mark.asyncio
async def test_join_decides_whether_to_check_posting_with_the_call_it_joins(world: World) -> None:
    """Someone whose app dropped out while the room was being read is checked for permission to post again."""
    await world.service.join(ROOM, ANN)
    world.clock.advance(59)
    world.room.posters = set()
    world.room.on_read = lambda: world.clock.advance(1)

    await _refused("forbidden", lambda: world.service.join(ROOM, ANN))
