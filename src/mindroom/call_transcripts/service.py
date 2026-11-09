"""How a call between people runs: who is in it and may act on it, Pause, speech, how it ends, its row and message.

All Matrix and network work goes through what the service is given: the room side (the router's view of the room),
the transcriber and the notes writer. The store runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol

from mindroom.background_tasks import create_background_task, run_coroutine_until_complete
from mindroom.call_transcripts.wav import WavError, wav_seconds
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Mapping, Sequence

    from mindroom.call_transcripts.store import CallTranscriptStore, RowEdit, StoredCall, StoredLine

logger = get_logger(__name__)

# The longest stretch of speech accepted. MindRoom Chat's speech finder cuts longer talk into stretches of at most 30
# seconds, so a stretch never holds more than 30 seconds of speech before it is sent. Speech-to-text takes much
# longer audio (OpenAI's transcription API takes files up to 25 MB, about 13 minutes of the audio Chat sends), so
# Chat's cut is the limit that binds.
_MAX_SPEECH_SECONDS = 30.0
# The largest stretch accepted, in bytes: 30 seconds of the 16 kHz 16-bit mono audio Chat sends is 960,044 bytes.
MAX_SPEECH_BYTES = 1_000_000
# The most stretches one person sends in a minute. Chat's speech finder rates 96 ms frames, and a stretch holds at
# least 4 frames of speech and the 14 quiet frames that end it, 1.73 seconds in all, so one person talking without a
# break records at most 35 stretches a minute. Chat sends them one at a time, each once the last is transcribed, so
# stretches that queued behind a slow speech-to-text arrive as fast as it answers once it recovers, and Chat retries
# a refused one only once. 240, about seven times that, lets such a queue drain at four a second while still stopping
# a client that sends without end.
_SPEECH_PER_MINUTE = 240
_SPEECH_WINDOW_SECONDS = 60.0
_PRESENCE_SECONDS = 60.0
_CHECK_SECONDS = 15.0
# How long after a call's end its stretches are still taken, and its message waits. Chat gives up on an upload 30
# seconds after it began, its one retry included, so a stretch it began sending before the end arrives by then or never.
_TRANSCRIPT_DELAY_SECONDS = 30.0
# How long a call's Matrix writes wait, after one could not be made, before they are tried again.
_RETRY_SECONDS = 60.0
_NOTES_ATTEMPTS = 2
_SPEAKING_RETRY_AFTER_SECONDS = 3
# How long a person's upload slot is held at most once its audio has arrived. Chat gives up on an upload 30 seconds
# after it began, its one retry included, and then sends the next stretch, so a slot held longer would refuse that
# stretch and its retry, losing it, for a stretch Chat no longer waits on. The slow stretch is still transcribed and
# written. A slot whose audio is still arriving stays held, so nobody has more than one upload's audio arriving at once.
_UPLOAD_SLOT_SECONDS = 25.0
_TRANSCRIBE_TIMEOUT_SECONDS = 60.0
# Notes not written in this long count as failed notes, so a model that never answers holds up no other call for long.
_NOTES_TIMEOUT_SECONDS = 90.0
_MIN_TICK_SECONDS = 1.0
_FORGET_AFTER_SECONDS = 3600.0
# A transcript message still not posted this long after its call ended is given up, and the call's lines deleted; its
# row is no longer posted or edited, and the call is forgotten.
_GIVE_UP_AFTER_SECONDS = 86_400.0

_NO_SUCH_CALL = "This call does not exist."
_NOT_IN_ROOM = "You are not in this room."
_NOT_IN_CALL = "You are not in this call; join it first."
_TOO_LATE = "It is too late to add to this call's transcript."
_UNREADABLE = "The room cannot be checked right now."

type RefusalCode = Literal[
    "forbidden",
    "not-found",
    "invalid",
    "paused",
    "too-many-requests",
    "unavailable",
    "too-large",
]


class CallRefusedError(Exception):
    """A request the call's rules refuse: ``code`` and the sentence say why, ``retry_after`` when to try again."""

    def __init__(self, *, code: RefusalCode, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.code: RefusalCode = code
        self.message = message
        self.retry_after = retry_after
        """Whole seconds to wait before trying again, for ``too-many-requests``."""


@dataclass(slots=True)
class _UploadSlot:
    """A person's one upload slot, taken before a stretch's audio is read."""

    taken_at: float
    sent: bool = False
    """Whether all of the stretch's audio has arrived."""


@dataclass(frozen=True, slots=True)
class Call:
    """A call, as clients are shown it."""

    id: str
    room_id: str
    row_event_id: str | None
    """The notice the router posts in the room for the call; None until it is up."""
    started_at: float
    ended_at: float | None
    """None while the call is live."""
    members: tuple[str, ...]
    """The people whose apps are in the call now."""
    paused_by: str | None
    """Who paused transcription, None while it is not paused."""


@dataclass(frozen=True, slots=True)
class LiveCall:
    """A room's live call, its lines (all, or those after a given one), and the display names of everyone in either."""

    call: Call | None
    lines: tuple[StoredLine, ...]
    names: Mapping[str, str | None]


@dataclass(frozen=True, slots=True)
class _Turn:
    """What one person said, uninterrupted, in a finished call."""

    sender: str
    text: str


@dataclass(frozen=True, slots=True)
class CallTranscript:
    """A finished call's speech: its turns in order, how long it lasted, and its speakers' display names."""

    call_id: str
    room_id: str
    duration_seconds: int
    turns: tuple[_Turn, ...]
    names: Mapping[str, str | None]


@dataclass(frozen=True, slots=True)
class TranscriptMessage:
    """The single message an ended call leaves in its row's thread, with the notes, or a one-line summary, as its body."""

    room_id: str
    call_id: str
    row_event_id: str
    body: str
    transcript: CallTranscript


@dataclass(frozen=True, slots=True)
class EndedRow:
    """The ended call's row as an edit makes it: the call's length, then also where its transcript message is."""

    room_id: str
    call_id: str
    row_event_id: str
    body: str
    ended_at: float
    duration_seconds: int
    transcript_event_id: str | None

    @property
    def edit(self) -> RowEdit:
        """Which of the row's two edits this is: ``ended``, then ``transcript`` once it links the transcript message."""
        return "ended" if self.transcript_event_id is None else "transcript"


class _CallRoom(Protocol):
    """The router's side of the rooms calls are in."""

    def router_joined(self, room_id: str) -> bool | None:
        """Return whether the router is joined to the room, None while it is not ready."""
        ...

    async def is_member(self, room_id: str, user_id: str) -> bool | None:
        """Return whether ``user_id`` is joined to the room, None when the router cannot tell."""
        ...

    async def may_post(self, room_id: str, user_id: str) -> bool | None:
        """Return whether ``user_id`` may post messages in the room by its power levels, None when it cannot tell."""
        ...

    async def router_may_post(self, room_id: str) -> bool | None:
        """Return whether the router may post messages in the room by its power levels, None when it cannot tell."""
        ...

    async def display_names(self, room_id: str, user_ids: Collection[str]) -> Mapping[str, str | None] | None:
        """Return each person's display name in the room, None for someone without one; None when it cannot tell."""
        ...

    async def post_row(self, room_id: str, call_id: str) -> str | None:
        """Post the notice that stands for the call in the room; return its event ID, None when nothing went out."""
        ...

    async def post_transcript(self, message: TranscriptMessage) -> str | None:
        """Post the call's message in its row's thread; return its event ID, None when nothing went out."""
        ...

    async def edit_row(self, row: EndedRow) -> str | None:
        """Replace the row's text with the ended call's; return the edit's event ID, None when nothing went out."""
        ...

    def title_thread(self, message: TranscriptMessage, event_id: str) -> None:
        """Have the row's thread retitled from the message ``event_id``, in the background."""
        ...


class _Transcriber(Protocol):
    """Speech-to-text for one stretch of a call, recording the usage it causes."""

    async def transcribe(self, audio: bytes, *, call_id: str, user_id: str, seconds: float) -> str | None:
        """Return what was said in the WAV ``audio``, None when it could not be transcribed."""
        ...


class _NotesWriter(Protocol):
    """The operator's notes model."""

    def writes_notes(self) -> bool:
        """Return whether a notes model is set, so notes will be written."""
        ...

    async def write_notes(self, transcript: CallTranscript) -> str | None:
        """Return the call's notes in Markdown, None when no notes model is set; raise when they cannot be written."""
        ...


class CallTranscriptService:
    """Calls between people in rooms while they run, and the message each one leaves after it ends."""

    def __init__(
        self,
        store: CallTranscriptStore,
        room: _CallRoom,
        transcriber: _Transcriber,
        notes: _NotesWriter,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._room = room
        self._transcriber = transcriber
        self._notes = notes
        self._clock = clock
        self._started_at = clock()
        """When this MindRoom began serving calls: every app gets a full minute from then to check in."""
        self._room_locks: dict[str, asyncio.Lock] = {}
        self._uploading: dict[str, _UploadSlot] = {}
        """The slot of each person sending a stretch now, from before its audio is read until it is answered."""
        self._speaking: list[str] = []
        """The call of each stretch let in and not yet written."""
        self._recent_speech: dict[str, deque[float]] = {}
        self._settling: dict[str, asyncio.Task[None]] = {}
        """Each call's one task making its Matrix writes, by call, so no call's writes wait for another's."""
        self._written_notes: dict[str, str] = {}

    async def join(self, room_id: str, user_id: str) -> Call:
        """Put the person's app in the room's live call, starting a call when there is none or it is empty.

        From someone already in the call this is their heartbeat. Only coming in checks that they may post in the room.
        """
        live = await asyncio.to_thread(self._store.live_call, room_id)
        await self._require_member(room_id, user_id, _NOT_IN_ROOM, call=live)
        # The room side is read before taking the room's lock and never under it, so a slow homeserver cannot hold
        # up heartbeats. Whether posting is needed is decided under the lock, with the call being joined.
        may_post = await self._room.may_post(room_id, user_id)
        async with self._room_lock(room_id):
            now = self._clock()
            live = await asyncio.to_thread(self._store.live_call, room_id)
            present = frozenset() if live is None else await self._present(live, now)
            if live is not None and not present:
                # Its last app dropped out, so it ended then, though no check has noticed yet: this join starts another.
                self._settle(await self._end(live), now)
            if user_id not in present:
                _require_poster(may_post)
            call, started = await asyncio.to_thread(self._store.ensure_live_call, room_id, at=now)
            await asyncio.to_thread(self._store.record_presence, call.id, user_id, at=now)
            if started:
                logger.info("call_transcript_call_started", room_id=room_id, call_id=call.id)
            self._settle(call, now)
            return _call_view(call, await self._present(call, now))

    async def leave(self, call_id: str, user_id: str) -> None:
        """Take the person's app out of the call now; when it was the last one in, the call ends.

        A leave from an app that is not in the call changes nothing. Someone who is no longer in the room is taken out,
        then told the call does not exist, as anyone outside the room is. A leave never counts as a check-in: refused
        while the room cannot be read, the person drops out a minute after their app last checked in.
        """
        call = await self._fresh(call_id)
        member = await self._room.is_member(call.room_id, user_id)
        if member is None:
            raise CallRefusedError(code="unavailable", message=_UNREADABLE)
        await self._leave(call, user_id)
        if not member:
            raise CallRefusedError(code="not-found", message=_NO_SUCH_CALL)

    async def pause(self, call_id: str, user_id: str, *, paused: bool) -> Call:
        """Pause or resume transcription for everyone in the call; only someone in the call can do either."""
        call = await self._call_for(call_id, user_id)
        async with self._room_lock(call.room_id):
            call = await self._fresh(call_id)
            if call.ended_at is not None:
                raise CallRefusedError(code="forbidden", message="This call has ended.")
            now = self._clock()
            members = await self._present(call, now)
            if user_id not in members:
                raise CallRefusedError(code="forbidden", message=_NOT_IN_CALL)
            await asyncio.to_thread(self._store.set_pause, call.id, user_id if paused else None, at=now)
            return _call_view(await self._fresh(call_id), members)

    async def speech(self, call_id: str, user_id: str, *, audio: bytes, started_at: float) -> StoredLine | None:
        """Transcribe one stretch the person said from ``started_at`` into a line, or a gap if speech-to-text fails.

        A stretch speech-to-text heard no words in adds nothing, and returns None. The audio is never written to disk.
        A stretch said before its call ended is still taken for the 30 seconds after the end; one said while
        transcription was paused never is.
        """
        call = await self._call_for(call_id, user_id)
        seconds = _speech_seconds(audio)
        async with self._room_lock(call.room_id):
            call = await self._fresh(call_id)
            now = self._clock()
            # A stretch has ended by the time it arrives, and none of the call's was said before the call began.
            said_at = max(call.started_at, min(started_at, now - seconds))
            await self._admit(call, user_id, said_at=said_at, seconds=seconds, now=now)
            self._speaking.append(call.id)
        try:
            text = await self._transcribe(audio, call_id=call.id, user_id=user_id, seconds=seconds)
            if text == "":
                # Speech-to-text heard no words: nothing is missing from the transcript, so there is no gap either.
                return None
            async with self._room_lock(call.room_id):
                if text is not None:
                    line = await asyncio.to_thread(
                        self._store.add_speech,
                        call.id,
                        user_id=user_id,
                        said_at=said_at,
                        seconds=seconds,
                        text=text,
                    )
                else:
                    logger.warning("call_transcript_stretch_not_transcribed", call_id=call.id, seconds=seconds)
                    line = await asyncio.to_thread(
                        self._store.add_gap,
                        call.id,
                        user_id=user_id,
                        said_at=said_at,
                        seconds=seconds,
                        reason="not-transcribed",
                    )
        finally:
            # In flight until its line is written, with no wait between, so the message cannot fall due without it.
            self._speaking.remove(call.id)
        if line is None:
            raise CallRefusedError(code="forbidden", message=_TOO_LATE)
        return line

    async def reserve_upload(self, call_id: str, user_id: str) -> _UploadSlot:
        """Take the person's one upload slot before a stretch's audio is read, until ``release_upload``.

        Refused from someone not in the call, for a call ended more than 30 seconds ago, or while their last upload holds
        the slot: until all its audio has arrived, then for at most 25 seconds after it was taken.
        """
        call = await self._call_for(call_id, user_id)
        now = self._clock()
        await self._require_in_call(call, user_id, now)
        held = self._uploading.get(user_id)
        if held is not None and (not held.sent or now - held.taken_at < _UPLOAD_SLOT_SECONDS):
            raise CallRefusedError(
                code="too-many-requests",
                message="Your previous stretch of speech is still being sent.",
                retry_after=_SPEAKING_RETRY_AFTER_SECONDS,
            )
        slot = _UploadSlot(taken_at=now)
        self._uploading[user_id] = slot
        return slot

    def release_upload(self, user_id: str, slot: _UploadSlot) -> None:
        """Give back the person's upload slot ``slot``, unless their next upload has taken it since."""
        if self._uploading.get(user_id) is slot:
            del self._uploading[user_id]

    async def live(self, room_id: str, user_id: str, *, after: str | None = None) -> LiveCall:
        """Return the room's live call with its lines, those added after the line ``after`` if given, and names.

        Only a member of the room may read it.
        """
        await self._require_member(room_id, user_id, _NOT_IN_ROOM)
        call = await asyncio.to_thread(self._store.live_call, room_id)
        if call is None:
            return LiveCall(call=None, lines=(), names={})
        view = _call_view(call, await self._present(call, self._clock()))
        lines = tuple(await asyncio.to_thread(self._store.lines, call.id, after=after))
        people = {*view.members, *(line.user_id for line in lines)}
        if view.paused_by is not None:
            people.add(view.paused_by)
        names = await self._room.display_names(room_id, people)
        return LiveCall(call=view, lines=lines, names=dict.fromkeys(people) if names is None else names)

    async def tick(self) -> float:
        """Check every call in the store, reading and writing nothing in Matrix, and start the Matrix writes calls owe.

        Ends live calls nobody is in, gives up transcript messages that can no longer be posted, forgets finished
        calls, and starts each call's writes in its one task, so no slow write holds up another call or the next check.
        Returns the seconds to wait before the next tick: at most 15, less when a transcript message falls due sooner.
        """
        due: list[float] = []
        for call in await asyncio.to_thread(self._store.calls):
            try:
                checked = await self._check(call)
            except Exception:
                logger.exception("call_transcript_check_failed", room_id=call.room_id, call_id=call.id)
                continue
            if checked is not None and (falls_due := self._falls_due_at(checked)) is not None:
                due.append(falls_due)
        now = self._clock()
        await self._forget_finished_calls(now)
        self._forget_quiet_speakers(now)
        return min(_CHECK_SECONDS, max(_MIN_TICK_SECONDS, min(due, default=math.inf) - now))

    async def tick_while_off(self) -> None:
        """Hold the store to the one-day limit while call transcripts are off, reading and writing nothing in Matrix.

        A live call not heard from for a day ends when it last was, and a message not posted a day after its call's end
        is given up, deleting the call's lines; finished calls are forgotten. Nothing else changes, so calls carry on
        once call transcripts are back on. A call that cannot be checked is logged, and the others still are.
        """
        for call in await asyncio.to_thread(self._store.calls):
            try:
                await self._check_while_off(call)
            except Exception:
                logger.exception("call_transcript_check_failed", room_id=call.room_id, call_id=call.id)
        await self._forget_finished_calls(self._clock())

    async def _check_while_off(self, call: StoredCall) -> None:
        async with self._room_lock(call.room_id):
            now = self._clock()
            fresh = await asyncio.to_thread(self._store.call, call.id)
            if fresh is None:
                return
            if fresh.ended_at is None and now - await self._last_heard(fresh) >= _GIVE_UP_AFTER_SECONDS:
                fresh = await self._end(fresh)
            if _past_a_day(fresh, now):
                await self._finish(fresh, given_up="not-posted-for-a-day")

    async def _forget_finished_calls(self, now: float) -> None:
        await asyncio.to_thread(
            self._store.forget_finished_calls,
            before=now - _FORGET_AFTER_SECONDS,
            ended_before=now - _GIVE_UP_AFTER_SECONDS,
        )

    def cancel_writes(self) -> None:
        """Cancel the calls' Matrix writes in progress at once: each is made after the next start, and posted once."""
        for task in self._settling.values():
            task.cancel()

    async def stop(self) -> None:
        """Cancel the calls' Matrix writes in progress and wait for them to stop."""
        tasks = list(self._settling.values())
        self.cancel_writes()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _check(self, call: StoredCall) -> StoredCall | None:
        """End the call if nobody is in it, give up its message if it can no longer be posted, start its Matrix writes.

        Returns the call as it is then, None if it is gone.
        """
        async with self._room_lock(call.room_id):
            now = self._clock()
            fresh = await asyncio.to_thread(self._store.call, call.id)
            if fresh is None:
                return None
            if fresh.ended_at is None:
                fresh = await self._end_if_empty(fresh, now)
            reason = self._lost_cause(fresh, now)
            if (reason is not None or _past_a_day(fresh, now)) and (task := self._settling.get(fresh.id)) is not None:
                # A write still under way, such as a stalled upload or row edit, is stopped before the message is given
                # up, and at the day's end whatever it is.
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if reason is not None:
                await self._finish(fresh, given_up=reason)
                fresh = await self._fresh(fresh.id)
            self._settle(fresh, now)
            return fresh

    def _lost_cause(self, call: StoredCall, now: float) -> str | None:
        """Return why the ended call's pending message can no longer be posted, None if it still can be."""
        if call.ended_at is None or call.transcript_done_at is not None:
            return None
        if _past_a_day(call, now):
            return "not-posted-for-a-day"
        if self._transcript_due(call, now) and self._room.router_joined(call.room_id) is False:
            return "router-not-in-room"
        return None

    def _falls_due_at(self, call: StoredCall) -> float | None:
        """Return when the call's pending message is next due for a tick to start it, None if no tick needs to."""
        if (
            call.ended_at is None
            or call.transcript_done_at is not None
            or call.id in self._settling
            or self._room.router_joined(call.room_id) is not True
        ):
            return None
        return max(call.ended_at + _TRANSCRIPT_DELAY_SECONDS, call.retry_at or 0.0)

    async def _end_if_empty(self, call: StoredCall, now: float) -> StoredCall:
        """End a live call nobody is in, under the room's lock: its pause ends, and its message falls due 30 seconds later.

        It ended when an app in it last checked in or left, never before it started: not when that is noticed, which
        may be hours later after MindRoom was down. Returns the call as it is then.
        """
        if await self._present(call, now):
            return call
        return await self._end(call)

    async def _end(self, call: StoredCall) -> StoredCall:
        """End a live call when it was last heard from; return it ended."""
        await asyncio.to_thread(self._store.end_call, call.id, at=await self._last_heard(call))
        logger.info("call_transcript_call_ended", room_id=call.room_id, call_id=call.id)
        return await self._fresh(call.id)

    async def _last_heard(self, call: StoredCall) -> float:
        """Return when an app in the call last checked in or left, never before the call started."""
        presence = await asyncio.to_thread(self._store.presence, call.id)
        return max([call.started_at, *(app.present_at for app in presence.values())])

    async def _present(self, call: StoredCall, now: float) -> frozenset[str]:
        """Return whose apps are in the call now: checked in within the last 60 seconds and not left since.

        From when this MindRoom began serving calls, as after a restart, each app gets a full 60 seconds to check in.
        """
        presence = await asyncio.to_thread(self._store.presence, call.id)
        return frozenset(
            user_id
            for user_id, app in presence.items()
            if not app.left and now - max(app.present_at, self._started_at) <= _PRESENCE_SECONDS
        )

    async def _admit(self, call: StoredCall, user_id: str, *, said_at: float, seconds: float, now: float) -> None:
        """Refuse a stretch the call does not take; otherwise count it against the person's limits.

        Runs under the room's lock.
        """
        await self._require_in_call(call, user_id, now, said_at=said_at)
        if call.ended_at is None and call.paused_since is not None:
            raise CallRefusedError(code="paused", message="Transcription is paused.")
        if call.ended_at is not None and said_at >= call.ended_at:
            raise CallRefusedError(code="forbidden", message="That was said after the call ended.")
        # From when the device says it began to when it arrived: a device clock running behind cannot move a stretch
        # said during a pause to before it.
        if await asyncio.to_thread(
            self._store.overlaps_pause,
            call.id,
            start=said_at,
            end=max(now, said_at + seconds),
        ):
            raise CallRefusedError(code="paused", message="That was said while transcription was paused.")
        self._count_speech(user_id, now)

    async def _require_in_call(
        self,
        call: StoredCall,
        user_id: str,
        now: float,
        *,
        said_at: float | None = None,
    ) -> None:
        """Take speech from someone whose app is in the live call, or was in the ended call, for 30 seconds after its end.

        From someone who left the ended call before it ended, only a stretch ``said_at`` before they left. The
        transcript message is begun no sooner, so a retry of its post sends just what the first attempt did.
        """
        if call.ended_at is None:
            if user_id not in await self._present(call, now):
                raise CallRefusedError(code="forbidden", message=_NOT_IN_CALL)
        elif call.transcript_done_at is not None or now >= call.ended_at + _TRANSCRIPT_DELAY_SECONDS:
            raise CallRefusedError(code="forbidden", message=_TOO_LATE)
        elif (app := (await asyncio.to_thread(self._store.presence, call.id)).get(user_id)) is None:
            raise CallRefusedError(code="forbidden", message="You were not in this call.")
        elif said_at is not None and app.left and said_at >= app.present_at:
            raise CallRefusedError(code="forbidden", message="That was said after you left the call.")

    def _count_speech(self, user_id: str, now: float) -> None:
        """Refuse a person's stretch past their 240 a minute; otherwise count it."""
        recent = self._recent_speech.setdefault(user_id, deque())
        while recent and recent[0] <= now - _SPEECH_WINDOW_SECONDS:
            recent.popleft()
        if len(recent) >= _SPEECH_PER_MINUTE:
            raise CallRefusedError(
                code="too-many-requests",
                message=f"You sent {_SPEECH_PER_MINUTE} stretches of speech in the last minute.",
                retry_after=max(1, math.ceil(recent[0] + _SPEECH_WINDOW_SECONDS - now)),
            )
        recent.append(now)

    async def _transcribe(self, audio: bytes, *, call_id: str, user_id: str, seconds: float) -> str | None:
        """Transcribe a stretch, giving up after 60 seconds so a stuck speech service cannot hold back the call."""
        try:
            async with asyncio.timeout(_TRANSCRIBE_TIMEOUT_SECONDS):
                return await self._transcriber.transcribe(audio, call_id=call_id, user_id=user_id, seconds=seconds)
        except TimeoutError:
            logger.warning("call_transcript_transcription_timed_out", call_id=call_id, seconds=seconds)
            return None

    def _forget_quiet_speakers(self, now: float) -> None:
        """Drop the speech counts of people who sent nothing for a minute."""
        quiet_since = now - _SPEECH_WINDOW_SECONDS
        for user_id in [user_id for user_id, recent in self._recent_speech.items() if recent[-1] <= quiet_since]:
            del self._recent_speech[user_id]

    def _settle(self, call: StoredCall, now: float) -> None:
        """Start the call's Matrix writes in a task of its own, unless none are owed, can be made now, or one is running."""
        if call.id not in self._settling and self._can_settle(call, now):
            task = create_background_task(self._settle_now(call.id), name="call_transcript_settle")
            self._settling[call.id] = task
            # Once done, even cancelled before it first ran, so the call's writes can start again.
            task.add_done_callback(lambda _: self._settling.pop(call.id, None))

    def _can_settle(self, call: StoredCall, now: float) -> bool:
        """Return whether the router can make Matrix writes the call owes now: its row, its due message, a row edit.

        Nothing is owed a day after the call's end, and nothing is tried while the writes wait after a failure.
        """
        if (
            self._room.router_joined(call.room_id) is not True
            or (call.retry_at is not None and call.retry_at > now)
            or _past_a_day(call, now)
        ):
            return False
        return call.row_event_id is None or self._transcript_due(call, now) or _owed_row_edit(call) is not None

    async def _settle_now(self, call_id: str) -> None:
        """Make the call's Matrix writes one at a time, in order.

        Its row, the row's edit for the end, its message once due, then the row's edit for the message. Each is sent
        under a transaction of its own, so making it again never posts twice. A write that cannot be made holds them
        all a minute, when a tick starts them again.
        """
        call: StoredCall | None = None
        try:
            while (call := await asyncio.to_thread(self._store.call, call_id)) is not None:
                if not self._can_settle(call, self._clock()):
                    return
                await self._settle_step(call)
        except Exception:
            logger.exception("call_transcript_write_failed", call_id=call_id)
            if call is not None:
                await self._postpone(call, reason="error")

    async def _settle_step(self, call: StoredCall) -> None:
        """Make the first Matrix write the call owes, or hold its writes a minute if it cannot be made.

        Nothing is sent while the router may not post: a due message is given up, as when it left the room, before any
        notes or upload, and the other writes wait.
        """
        if await self._room.router_may_post(call.room_id) is False:
            if self._transcript_due(call, self._clock()):
                await self._finish(call, given_up="router-may-not-post")
            else:
                await self._postpone(call, reason="router-may-not-post")
        elif call.row_event_id is None:
            event_id = await self._room.post_row(call.room_id, call.id)
            if event_id is None:
                await self._postpone(call, reason="row-not-posted")
            else:
                await asyncio.to_thread(self._store.set_row_event_id, call.id, event_id)
        elif (row := _owed_row_edit(call)) is not None:
            if await self._room.edit_row(row) is None:
                await self._postpone(call, reason="row-not-edited")
            else:
                await asyncio.to_thread(self._store.record_row_edit, call.id, row.edit)
        else:
            await self._post_transcript(call)

    def _transcript_due(self, call: StoredCall, now: float) -> bool:
        """Return whether the ended call's message is due: 30 seconds after the end, with none of its speech in flight."""
        return (
            call.ended_at is not None
            and call.transcript_done_at is None
            and now >= call.ended_at + _TRANSCRIPT_DELAY_SECONDS
            and call.id not in self._speaking
        )

    async def _post_transcript(self, call: StoredCall) -> None:
        """Post the ended call's one message in its row's thread, with notes while they can be had, or hold it a minute.

        A call in which people spoke but none of it could be transcribed posts a message that says so, without turns.
        """
        assert call.ended_at is not None
        assert call.row_event_id is not None
        # Due and read under the room's lock, so every stretch let in before then has its line written first.
        async with self._room_lock(call.room_id):
            if not self._transcript_due(call, self._clock()):
                return
            lines = await asyncio.to_thread(self._store.lines, call.id)
        turns = _turns(lines)
        if not turns and all(line.reason != "not-transcribed" for line in lines):
            await self._finish(call)
            return
        notes = self._written_notes.get(call.id)
        writes_notes = (
            bool(turns) and notes is None and call.notes_failures < _NOTES_ATTEMPTS and self._notes.writes_notes()
        )
        # Only notes name people, by display name; until the room can be read they wait, without using an attempt.
        names = await self._room.display_names(call.room_id, {turn.sender for turn in turns}) if writes_notes else {}
        if names is None:
            await self._postpone(call, reason="names-unreadable")
            return
        transcript = CallTranscript(
            call_id=call.id,
            room_id=call.room_id,
            duration_seconds=round(call.ended_at - call.started_at),
            turns=turns,
            names=names,
        )
        if writes_notes:
            try:
                async with asyncio.timeout(_NOTES_TIMEOUT_SECONDS):
                    notes = await self._notes.write_notes(transcript)
            except Exception:
                logger.exception("call_transcript_notes_failed", room_id=call.room_id, call_id=call.id)
                await asyncio.to_thread(self._store.count_failed_notes, call.id)
                if call.notes_failures + 1 < _NOTES_ATTEMPTS:
                    await self._postpone(call, reason="notes-failed")
                    return
            if notes is not None:
                self._written_notes[call.id] = notes
        message = TranscriptMessage(
            room_id=call.room_id,
            call_id=call.id,
            row_event_id=call.row_event_id,
            body=notes if notes is not None else _summary(transcript),
            transcript=transcript,
        )
        await self._post_message(call, message, with_notes=notes is not None)

    async def _post_message(self, call: StoredCall, message: TranscriptMessage, *, with_notes: bool) -> None:
        """Post the call's message and finish it, or hold it a minute; notes then title the row's thread."""
        if _past_a_day(call, self._clock()):
            return  # A tick gives the message up.
        event_id = await self._room.post_transcript(message)
        if event_id is None:
            await self._postpone(call, reason="transcript-not-posted")
            return
        await self._finish(call, event_id=event_id, with_notes=with_notes, turns=len(message.transcript.turns))
        if with_notes:
            # The notes say what the call was about; until then the thread is titled by its row: "Call ended".
            self._room.title_thread(message, event_id)

    async def _postpone(self, call: StoredCall, *, reason: str) -> None:
        """Hold the call's Matrix writes a minute."""
        await asyncio.to_thread(self._store.postpone, call.id, until=self._clock() + _RETRY_SECONDS)
        logger.warning("call_transcript_postponed", room_id=call.room_id, call_id=call.id, reason=reason)

    async def _finish(
        self,
        call: StoredCall,
        *,
        event_id: str | None = None,
        given_up: str | None = None,
        **details: object,
    ) -> None:
        """Finish the ended call's transcript message, deleting its lines, and log it once with how long people spoke.

        ``event_id`` is the posted message, None when there was nothing to post or the message was given up, which
        ``given_up`` says why. Once begun it is completed, its log included, though its task is cancelled meanwhile.
        """

        async def finish() -> None:
            speech_seconds = await asyncio.to_thread(
                self._store.finish_transcript,
                call.id,
                at=self._clock(),
                event_id=event_id,
            )
            self._written_notes.pop(call.id, None)
            if speech_seconds is None:
                return
            fields = {"room_id": call.room_id, "call_id": call.id, "speech_seconds": speech_seconds, **details}
            if given_up is None:
                logger.info("call_transcript_finished", event_id=event_id, **fields)
            else:
                logger.warning("call_transcript_given_up", reason=given_up, **fields)

        await run_coroutine_until_complete(finish())

    async def _call_for(self, call_id: str, user_id: str) -> StoredCall:
        """Return the call, taking its room from the call itself; to anyone outside that room it does not exist."""
        call = await self._fresh(call_id)
        await self._require_member(call.room_id, user_id, _NO_SUCH_CALL, call=call)
        return call

    async def _fresh(self, call_id: str) -> StoredCall:
        call = await asyncio.to_thread(self._store.call, call_id)
        if call is None:
            raise CallRefusedError(code="not-found", message=_NO_SUCH_CALL)
        return call

    async def _require_member(
        self,
        room_id: str,
        user_id: str,
        not_found: str,
        *,
        call: StoredCall | None = None,
    ) -> None:
        """Refuse someone not joined to the room (``not-found``), or anyone while the room cannot be checked.

        Someone whose app is in the live ``call`` is checked in first, so a room that cannot be read for a while, as
        while the router restarts or the homeserver is down, never counts them out; once it can be read, someone no
        longer joined to it leaves the call.
        """
        in_call = call is not None and await self._check_in(call, user_id)
        member = await self._room.is_member(room_id, user_id)
        if member is None:
            raise CallRefusedError(code="unavailable", message=_UNREADABLE)
        if not member:
            if call is not None and in_call:
                await self._leave(call, user_id)
            raise CallRefusedError(code="not-found", message=not_found)

    async def _check_in(self, call: StoredCall, user_id: str) -> bool:
        """Record a check-in from someone whose app is in the live call; return whether it is."""
        async with self._room_lock(call.room_id):
            fresh = await asyncio.to_thread(self._store.call, call.id)
            now = self._clock()
            if fresh is None or fresh.ended_at is not None or user_id not in await self._present(fresh, now):
                return False
            await asyncio.to_thread(self._store.record_presence, call.id, user_id, at=now)
            return True

    async def _leave(self, call: StoredCall, user_id: str) -> None:
        """Take the person's app out of a live call at once; the last one out ends it.

        An app not in the call, never or no longer, records no leave, so a late one cannot move the call's end. An
        ending edits the row at once, in the call's own task.
        """
        async with self._room_lock(call.room_id):
            call = await self._fresh(call.id)
            if call.ended_at is not None:
                return
            now = self._clock()
            if user_id in await self._present(call, now):
                await asyncio.to_thread(self._store.record_presence, call.id, user_id, at=now, left=True)
            self._settle(await self._end_if_empty(call, now), now)

    def _room_lock(self, room_id: str) -> asyncio.Lock:
        return self._room_locks.setdefault(room_id, asyncio.Lock())


def _require_poster(may_post: bool | None) -> None:
    if may_post is None:
        raise CallRefusedError(code="unavailable", message=_UNREADABLE)
    if not may_post:
        raise CallRefusedError(code="forbidden", message="You may not post messages in this room.")


def _speech_seconds(audio: bytes) -> float:
    """Return a stretch's length from its WAV header, refusing audio that is too big, too long or not 16-bit mono."""
    if len(audio) > MAX_SPEECH_BYTES:
        raise CallRefusedError(code="too-large", message=f"The audio is larger than {MAX_SPEECH_BYTES:,} bytes.")
    try:
        seconds = wav_seconds(audio)
    except WavError as error:
        raise CallRefusedError(code="invalid", message=str(error)) from error
    if seconds > _MAX_SPEECH_SECONDS:
        raise CallRefusedError(code="too-large", message=f"The audio is longer than {_MAX_SPEECH_SECONDS:g} seconds.")
    return seconds


def _past_a_day(call: StoredCall, now: float) -> bool:
    """Return whether the call ended a day or more ago, after which none of its Matrix writes is made."""
    return call.ended_at is not None and now - call.ended_at >= _GIVE_UP_AFTER_SECONDS


def _call_view(call: StoredCall, members: Iterable[str]) -> Call:
    return Call(
        id=call.id,
        room_id=call.room_id,
        row_event_id=call.row_event_id,
        started_at=call.started_at,
        ended_at=call.ended_at,
        members=tuple(sorted(members)),
        paused_by=call.paused_by,
    )


def _turns(lines: Sequence[StoredLine]) -> tuple[_Turn, ...]:
    """Return the call's speech as turns: one speaker's stretches in a row make one turn, and gaps are never turns.

    Lines are taken in the order they were said, ties in the order they were added.
    """
    turns: list[_Turn] = []
    previous: StoredLine | None = None
    for line in sorted(lines, key=lambda line: line.said_at):
        if line.kind == "speech" and line.text is not None:
            if previous is not None and previous.kind == "speech" and previous.user_id == line.user_id:
                turns[-1] = _Turn(sender=line.user_id, text=f"{turns[-1].text} {line.text}")
            else:
                turns.append(_Turn(sender=line.user_id, text=line.text))
        previous = line
    return tuple(turns)


def _summary(transcript: CallTranscript) -> str:
    """Return the message's body without notes: the call's length, and that nothing could be transcribed if so."""
    summary = f"Call · {call_minutes(transcript.duration_seconds)} min"
    return summary if transcript.turns else f"{summary}. Nothing could be transcribed."


def call_minutes(duration_seconds: int) -> int:
    """Return a call's length in whole minutes, at least one, as its row, its message and its notes say it."""
    return max(1, round(duration_seconds / 60))


def _owed_row_edit(call: StoredCall) -> EndedRow | None:
    """Return the edit an ended call's posted row still owes: its end, then also its transcript message once posted."""
    if call.ended_at is None or call.row_event_id is None or call.row_edit == "transcript":
        return None
    duration_seconds = round(call.ended_at - call.started_at)
    row = EndedRow(
        room_id=call.room_id,
        call_id=call.id,
        row_event_id=call.row_event_id,
        body=f"Call ended · {call_minutes(duration_seconds)} min",
        ended_at=call.ended_at,
        duration_seconds=duration_seconds,
        transcript_event_id=call.transcript_event_id,
    )
    return None if row.edit == call.row_edit else row


def iso_time(seconds: float) -> str:
    """Return a time in seconds since the epoch as ISO 8601 in UTC, to the millisecond, as clients read calls' times."""
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
