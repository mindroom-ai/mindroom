"""The rules of a call between people: who is in it, who may act on it, Pause, speech, its end, its row and message.

The service does no Matrix or network work of its own: the room side (the router's view of the room), the transcriber
and the notes writer are passed in, and the store is called in a worker thread. Each change is announced to the room's
listeners once it is written, as one of the live stream's events.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol

from mindroom.call_transcripts.wav import WavError, wav_seconds
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence

    from mindroom.call_transcripts.store import AppPresence, CallTranscriptStore, RowEdit, StoredCall, StoredLine

logger = get_logger(__name__)

# The largest stretch of speech accepted, in bytes.
MAX_SPEECH_BYTES = 1_000_000
_MAX_SPEECH_SECONDS = 30.0
_SPEECH_PER_MINUTE = 240
_SPEECH_WINDOW_SECONDS = 60.0
_PRESENCE_SECONDS = 60.0
_CHECK_SECONDS = 15.0
_TRANSCRIPT_DELAY_SECONDS = 20.0
# How long a transcript message or row edit that could not be posted waits before it is tried again.
_RETRY_SECONDS = 60.0
_NOTES_ATTEMPTS = 2
_SPEAKING_RETRY_AFTER_SECONDS = 3
_TRANSCRIBE_TIMEOUT_SECONDS = 60.0
# How far the homeserver's clock, which dates MatrixRTC memberships, may lag MindRoom's.
_CLOCK_TOLERANCE_SECONDS = 5.0
_MIN_TICK_SECONDS = 1.0
_FORGET_AFTER_SECONDS = 3600.0

_NO_SUCH_CALL = "This call does not exist."
_NOT_IN_ROOM = "You are not in this room."
_NOT_IN_CALL = "You are not in this call; join it first."
_TOO_LATE = "It is too late to add to this call's transcript."

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


@dataclass(frozen=True, slots=True)
class Call:
    """A call as people see it."""

    id: str
    room_id: str
    row_event_id: str | None
    """The call's row notice, None until it is posted."""
    started_at: float
    ended_at: float | None
    """None while the call is live."""
    members: tuple[str, ...]
    """Who is in the call now: people whose apps are in it, and people in the room's MatrixRTC call."""
    holds: tuple[str, ...]
    """Who holds Pause, the longest held first."""
    transcribed_by: tuple[str, ...]
    """Whose apps are in the call: their speech is transcribed."""


@dataclass(frozen=True, slots=True)
class LiveCall:
    """A room's live call, its lines so far, and the display names of everyone in either."""

    call: Call | None
    lines: tuple[StoredLine, ...]
    names: Mapping[str, str | None]


@dataclass(frozen=True, slots=True)
class CallUpdated:
    """The live stream's ``call`` event: a call started or who is in it, who holds Pause, or its row changed."""

    call: Call


@dataclass(frozen=True, slots=True)
class LineAdded:
    """The live stream's ``line`` event: a stretch transcribed, or a gap."""

    line: StoredLine


@dataclass(frozen=True, slots=True)
class CallEnded:
    """The live stream's ``ended`` event."""

    call_id: str


type CallEvent = CallUpdated | LineAdded | CallEnded


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
    """The one message posted in the call's row thread when it ends: its body is the notes or a summary."""

    room_id: str
    call_id: str
    row_event_id: str
    body: str
    transcript: CallTranscript


@dataclass(frozen=True, slots=True)
class EndedRow:
    """What an ended call's row is edited to say: how long the call lasted and, once posted, its transcript message."""

    room_id: str
    call_id: str
    row_event_id: str
    body: str
    ended_at: float
    duration_seconds: int
    transcript_event_id: str | None


class _CallRoom(Protocol):
    """The router's side of the rooms calls are in."""

    async def is_member(self, room_id: str, user_id: str) -> bool | None:
        """Return whether ``user_id`` is joined to the room, None when the router cannot tell."""
        ...

    async def may_post(self, room_id: str, user_id: str) -> bool | None:
        """Return whether ``user_id`` may post messages in the room by its power levels, None when it cannot tell."""
        ...

    async def call_members(self, room_id: str) -> Mapping[str, float] | None:
        """Return who is in the room's MatrixRTC call, with when each membership was created (seconds since the epoch).

        MindRoom's own agents and expired memberships are left out. None when the room's state cannot be read.
        """
        ...

    async def display_names(self, room_id: str, user_ids: Collection[str]) -> Mapping[str, str | None] | None:
        """Return each person's display name in the room, None for someone without one; None when it cannot tell."""
        ...

    async def post_row(self, room_id: str, call_id: str) -> str | None:
        """Post the call's row notice and return its event ID; None if it was not posted, such as before the router is ready."""
        ...

    async def post_transcript(self, message: TranscriptMessage) -> str | None:
        """Post the transcript message in the row's thread and return its event ID; None if it was not posted."""
        ...

    async def edit_row(self, row: EndedRow) -> str | None:
        """Edit the call's row to say it ended, and return the edit's event ID; None if it was not sent."""
        ...


class _Transcriber(Protocol):
    """Speech-to-text for one stretch of a call, recording the usage it causes."""

    async def transcribe(self, audio: bytes, *, call_id: str, user_id: str, seconds: float) -> str | None:
        """Return what was said in the WAV ``audio``, None when it could not be transcribed."""
        ...


class _NotesWriter(Protocol):
    """The operator's notes model."""

    async def write_notes(self, transcript: CallTranscript) -> str | None:
        """Return the call's notes in Markdown, None when no notes model is set; raise when they cannot be written."""
        ...


@dataclass(eq=False, slots=True)
class _Stretch:
    """One person's stretch of speech between being let in and being written."""

    call_id: str


class CallTranscriptService:
    """Live calls between people in rooms, and the one message each leaves behind."""

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
        self._room_locks: dict[str, asyncio.Lock] = {}
        self._listeners: dict[str, list[Callable[[CallEvent], None]]] = {}
        self._announced: dict[str, Call] = {}
        self._speaking: dict[str, _Stretch] = {}
        self._recent_speech: dict[str, deque[float]] = {}
        self._posting: set[str] = set()
        self._rows_posting: set[str] = set()
        self._rows_editing: set[str] = set()
        self._row_edits_retry_at: dict[str, float] = {}
        self._written_notes: dict[str, str] = {}

    async def join(self, room_id: str, user_id: str) -> Call:
        """Join the room's live call, starting it if there is none; from someone already in it, this is the heartbeat.

        Coming into the call needs permission to post in the room; a heartbeat does not check it again.
        """
        await self._require_member(room_id, user_id, _NOT_IN_ROOM)
        # The room side is read before taking the room's lock and never under it, so a slow homeserver cannot hold
        # up heartbeats. Whether posting is needed is decided under the lock, with the call being joined.
        may_post = await self._room.may_post(room_id, user_id)
        state = await self._room.call_members(room_id)
        async with self._room_lock(room_id):
            now = self._clock()
            live = await asyncio.to_thread(self._store.live_call, room_id)
            if live is None or user_id not in (await self._apps(live, now))[1]:
                _require_poster(may_post)
            call, started = await asyncio.to_thread(self._store.ensure_live_call, room_id, at=now)
            await asyncio.to_thread(self._store.record_call_app, call.id, user_id)
            await asyncio.to_thread(self._store.record_app_presence, room_id, user_id, at=now)
            if started:
                logger.info("call_transcript_call_started", room_id=room_id, call_id=call.id)
            view = await self._settle(call, now, state)
            post_row = self._claim_row(call)
        if post_row and (event_id := await self._post_row(call)) is not None:
            return replace(view, row_event_id=event_id)
        return view

    async def leave(self, call_id: str, user_id: str) -> None:
        """Take the person's app out of the call at once, releasing their hold; the last one out ends the call.

        Someone whose app was never in the call records nothing.
        """
        call = await self._call_for(call_id, user_id)
        state = await self._room.call_members(call.room_id)
        async with self._room_lock(call.room_id):
            call = await self._fresh(call_id)
            if call.ended_at is not None or user_id not in await asyncio.to_thread(self._store.call_apps, call.id):
                return
            now = self._clock()
            await asyncio.to_thread(self._store.record_app_presence, call.room_id, user_id, at=now, left=True)
            await self._release(call, now, user_ids=[user_id])
            view = await self._settle(call, now, state)
        if view.ended_at is not None:
            await self._edit_row(call.id)

    async def pause(self, call_id: str, user_id: str, *, holding: bool) -> Call:
        """Take or release the person's hold on Pause; only someone in the call can take one."""
        call = await self._call_for(call_id, user_id)
        state = await self._room.call_members(call.room_id)
        async with self._room_lock(call.room_id):
            call = await self._fresh(call_id)
            if call.ended_at is not None:
                if holding:
                    raise CallRefusedError(code="forbidden", message="This call has ended.")
                return _call_view(call)
            now = self._clock()
            in_call = user_id in (await self._apps(call, now))[1]
            if holding and not in_call:
                raise CallRefusedError(code="forbidden", message=_NOT_IN_CALL)
            if in_call:
                await asyncio.to_thread(self._store.record_app_presence, call.room_id, user_id, at=now)
            if holding:
                await asyncio.to_thread(self._store.take_hold, call.id, user_id, at=now)
            else:
                await self._release(call, now, user_ids=[user_id])
            return await self._settle(call, now, state)

    async def speech(self, call_id: str, user_id: str, *, audio: bytes, started_at: float) -> StoredLine:
        """Transcribe one stretch the person said from ``started_at`` into a line, or a gap if speech-to-text fails.

        The audio is never written to disk. A stretch said before its call ended is accepted until the transcript
        message is posted; one said while anyone held Pause never is.
        """
        call = await self._call_for(call_id, user_id)
        seconds = _speech_seconds(audio)
        stretch = _Stretch(call_id=call.id)
        async with self._room_lock(call.room_id):
            call = await self._fresh(call_id)
            now = self._clock()
            # A stretch has ended by the time it arrives, and none of the call's was said before the call began.
            said_at = max(call.started_at, min(started_at, now - seconds))
            await self._admit(call, user_id, said_at=said_at, seconds=seconds, now=now)
            self._speaking[user_id] = stretch
        try:
            text = await self._transcribe(audio, call_id=call.id, user_id=user_id, seconds=seconds)
            async with self._room_lock(call.room_id):
                self._done_speaking(user_id, stretch)
                if text:
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
            self._done_speaking(user_id, stretch)
        if line is None:
            raise CallRefusedError(code="forbidden", message=_TOO_LATE)
        self._announce(call.room_id, LineAdded(line=line))
        return line

    async def require_member(self, room_id: str, user_id: str) -> None:
        """Refuse someone not joined to the room (``not-found``), or anyone while the room cannot be checked."""
        await self._require_member(room_id, user_id, _NOT_IN_ROOM)

    async def live(self, room_id: str, user_id: str) -> LiveCall:
        """Return the room's live call with its lines and names, for a member of the room."""
        await self._require_member(room_id, user_id, _NOT_IN_ROOM)
        call = await asyncio.to_thread(self._store.live_call, room_id)
        if call is None:
            return LiveCall(call=None, lines=(), names={})
        state = await self._room.call_members(room_id)
        presence, apps = await self._apps(call, self._clock())
        holds = await asyncio.to_thread(self._store.holds, call.id)
        view = _call_view(
            call,
            members=apps | _matrixrtc_members(state or {}, presence),
            holds=holds,
            transcribed_by=apps,
        )
        lines = tuple(await asyncio.to_thread(self._store.lines, call.id))
        people = {*view.members, *view.holds, *(line.user_id for line in lines)}
        names = await self._room.display_names(room_id, people)
        return LiveCall(call=view, lines=lines, names=dict.fromkeys(people) if names is None else names)

    @contextmanager
    def listen(self, room_id: str, listener: Callable[[CallEvent], None]) -> Iterator[None]:
        """Call ``listener`` with each of the room's events while the block runs; it must return without waiting."""
        listeners = self._listeners.setdefault(room_id, [])
        listeners.append(listener)
        try:
            yield
        finally:
            listeners.remove(listener)
            if not listeners:
                del self._listeners[room_id]

    async def tick(self) -> float:
        """Check every live call, post the transcript messages and row edits that are due, and forget finished calls.

        Returns the seconds to wait before the next tick: at most 15, less when a transcript message falls due sooner.
        """
        for call in await asyncio.to_thread(self._store.live_calls):
            try:
                await self._check(call)
            except Exception:
                logger.exception("call_transcript_check_failed", room_id=call.room_id, call_id=call.id)
        for call in await asyncio.to_thread(self._store.due_transcripts, now=self._clock()):
            try:
                await self._post_transcript(call)
            except Exception:
                logger.exception("call_transcript_post_failed", room_id=call.room_id, call_id=call.id)
                await self._postpone(call)
        owing = await asyncio.to_thread(self._store.rows_owing_edits)
        for call in owing:
            if self._row_edits_retry_at.get(call.id, 0.0) > self._clock():
                continue
            try:
                await self._edit_row(call.id)
            except Exception:
                logger.exception("call_transcript_row_edit_failed", room_id=call.room_id, call_id=call.id)
                self._row_edits_retry_at[call.id] = self._clock() + _RETRY_SECONDS
        owing_ids = {call.id for call in owing}
        self._row_edits_retry_at = {key: at for key, at in self._row_edits_retry_at.items() if key in owing_ids}
        now = self._clock()
        await asyncio.to_thread(self._store.forget_finished_calls, before=now - _FORGET_AFTER_SECONDS)
        self._forget_quiet_speakers(now)
        due_at = await asyncio.to_thread(self._store.next_transcript_due_at)
        if due_at is None:
            return _CHECK_SECONDS
        return min(_CHECK_SECONDS, max(_MIN_TICK_SECONDS, due_at - now))

    async def _check(self, call: StoredCall) -> None:
        """Settle who is in a live call, and post its row if it is still missing."""
        state = await self._room.call_members(call.room_id)
        async with self._room_lock(call.room_id):
            fresh = await asyncio.to_thread(self._store.call, call.id)
            if fresh is None or fresh.ended_at is not None:
                return
            view = await self._settle(fresh, self._clock(), state)
            post_row = view.ended_at is None and self._claim_row(fresh)
        if post_row:
            await self._post_row(fresh)

    async def _settle(self, call: StoredCall, now: float, state: Mapping[str, float] | None) -> Call:
        """Release the holds of people who are out, end the call if nobody is in, and announce what changed.

        Runs under the room's lock, for a live call, with the room's MatrixRTC ``state`` read before the lock was
        taken. A state that could not be read (None) never ends a call.
        """
        presence, apps = await self._apps(call, now)
        members = apps | _matrixrtc_members(state or {}, presence)
        holds = await asyncio.to_thread(self._store.holds, call.id)
        if gone := [user_id for user_id in holds if user_id not in apps]:
            await self._release(call, now, user_ids=gone)
            holds = [user_id for user_id in holds if user_id in apps]
        if not members and state is not None:
            return await self._end(call, now)
        view = _call_view(call, members=members, holds=holds, transcribed_by=apps)
        self._announce_call(view)
        return view

    def _announce_call(self, view: Call) -> None:
        """Announce the call as people see it, if that changed."""
        if self._announced.get(view.id) != view:
            self._announced[view.id] = view
            self._announce(view.room_id, CallUpdated(call=view))

    async def _end(self, call: StoredCall, now: float) -> Call:
        """End a live call: its pause ends, and its transcript message is due 20 seconds later."""
        await self._release(call, now)
        await asyncio.to_thread(
            self._store.end_call,
            call.id,
            at=now,
            transcript_due_at=now + _TRANSCRIPT_DELAY_SECONDS,
        )
        self._announced.pop(call.id, None)
        self._announce(call.room_id, CallEnded(call_id=call.id))
        logger.info("call_transcript_call_ended", room_id=call.room_id, call_id=call.id)
        return _call_view(replace(call, ended_at=now))

    async def _release(self, call: StoredCall, now: float, *, user_ids: Collection[str] | None = None) -> None:
        """Release holds (everyone's when ``user_ids`` is None), announcing the pause's gap if it ended."""
        gap = await asyncio.to_thread(self._store.release_holds, call.id, at=now, user_ids=user_ids)
        if gap is not None:
            self._announce(call.room_id, LineAdded(line=gap))

    async def _apps(self, call: StoredCall, now: float) -> tuple[Mapping[str, AppPresence], frozenset[str]]:
        """Return the room's app check-ins and whose apps are in the call now.

        An app is in while it was in this call, checked in within the last 60 seconds, and has not left since. It
        gets a full 60 seconds after this process started to check in again, so a restart ends no call.
        """
        presence = await asyncio.to_thread(self._store.app_presence, call.room_id)
        joined = await asyncio.to_thread(self._store.call_apps, call.id)
        return presence, frozenset(
            user_id
            for user_id in joined
            if (app := presence.get(user_id)) is not None
            and not app.left
            and now - max(app.present_at, self._started_at) <= _PRESENCE_SECONDS
        )

    async def _admit(self, call: StoredCall, user_id: str, *, said_at: float, seconds: float, now: float) -> None:
        """Refuse a stretch the call does not take; otherwise count it against the person's limits.

        Runs under the room's lock.
        """
        if call.ended_at is None:
            await self._admit_to_live_call(call, user_id, now)
        else:
            await self._admit_to_ended_call(call, user_id, said_at, call.ended_at)
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

    async def _admit_to_live_call(self, call: StoredCall, user_id: str, now: float) -> None:
        """Take speech only from someone whose app is in the call, as their check-in, and never while it is paused."""
        if user_id not in (await self._apps(call, now))[1]:
            raise CallRefusedError(code="forbidden", message=_NOT_IN_CALL)
        await asyncio.to_thread(self._store.record_app_presence, call.room_id, user_id, at=now)
        if await asyncio.to_thread(self._store.holds, call.id):
            raise CallRefusedError(code="paused", message="Transcription is paused.")

    async def _admit_to_ended_call(self, call: StoredCall, user_id: str, said_at: float, ended_at: float) -> None:
        """Take late speech said before the end from someone whose app was in the call, until the message is posted."""
        if call.transcript_done_at is not None or call.id in self._posting:
            raise CallRefusedError(code="forbidden", message=_TOO_LATE)
        if said_at >= ended_at:
            raise CallRefusedError(code="forbidden", message="That was said after the call ended.")
        if user_id not in await asyncio.to_thread(self._store.call_apps, call.id):
            raise CallRefusedError(code="forbidden", message="You were not in this call.")

    def _count_speech(self, user_id: str, now: float) -> None:
        """Refuse a second stretch in flight, or more than 240 a minute, from one person; otherwise count this one."""
        if user_id in self._speaking:
            raise CallRefusedError(
                code="too-many-requests",
                message="Your previous stretch of speech is still being transcribed.",
                retry_after=_SPEAKING_RETRY_AFTER_SECONDS,
            )
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

    def _done_speaking(self, user_id: str, stretch: _Stretch) -> None:
        if self._speaking.get(user_id) is stretch:
            del self._speaking[user_id]

    def _forget_quiet_speakers(self, now: float) -> None:
        """Drop the speech counts of people who sent nothing for a minute."""
        quiet_since = now - _SPEECH_WINDOW_SECONDS
        for user_id in [user_id for user_id, recent in self._recent_speech.items() if recent[-1] <= quiet_since]:
            del self._recent_speech[user_id]

    async def _post_transcript(self, call: StoredCall) -> None:
        """Post a due transcript message unless a stretch of the call is still being transcribed."""
        async with self._room_lock(call.room_id):
            fresh = await asyncio.to_thread(self._store.call, call.id)
            if (
                fresh is None
                or fresh.transcript_due_at is None
                or fresh.transcript_due_at > self._clock()
                or fresh.id in self._posting
                or fresh.id in self._rows_posting
                or any(stretch.call_id == fresh.id for stretch in self._speaking.values())
            ):
                return
            self._posting.add(fresh.id)
        try:
            await self._post_transcript_now(fresh)
        finally:
            self._posting.discard(fresh.id)

    async def _post_transcript_now(self, call: StoredCall) -> None:
        """Post the ended call's one message, with notes while they can be had; postpone it a minute on failure."""
        assert call.ended_at is not None
        lines = await asyncio.to_thread(self._store.lines, call.id)
        turns = _turns(lines)
        if not turns:
            await asyncio.to_thread(self._store.finish_transcript, call.id, at=self._clock())
            logger.info("call_transcript_had_no_speech", room_id=call.room_id, call_id=call.id)
            return
        names = await self._room.display_names(call.room_id, {turn.sender for turn in turns})
        notes = self._written_notes.get(call.id)
        writes_notes = notes is None and call.transcript_attempts < _NOTES_ATTEMPTS
        if writes_notes and names is None:
            # Notes name people by display name; until the room can be read they wait, without using an attempt.
            await self._postpone(call, failed=False)
            return
        transcript = CallTranscript(
            call_id=call.id,
            room_id=call.room_id,
            duration_seconds=round(call.ended_at - call.started_at),
            turns=turns,
            names=names or {},
        )
        if writes_notes:
            try:
                notes = await self._notes.write_notes(transcript)
            except Exception:
                logger.exception("call_transcript_notes_failed", room_id=call.room_id, call_id=call.id)
                if call.transcript_attempts + 1 < _NOTES_ATTEMPTS:
                    await self._postpone(call)
                    return
            if notes is not None:
                self._written_notes[call.id] = notes
        row_event_id = call.row_event_id
        if row_event_id is None and self._claim_row(call):
            row_event_id = await self._post_row(call)
        if row_event_id is None:
            await self._postpone(call)
            return
        message = TranscriptMessage(
            room_id=call.room_id,
            call_id=call.id,
            row_event_id=row_event_id,
            body=notes if notes is not None else f"Call · {_minutes(transcript.duration_seconds)} min",
            transcript=transcript,
        )
        event_id = await self._room.post_transcript(message)
        if event_id is None:
            await self._postpone(call)
            return
        await asyncio.to_thread(self._store.finish_transcript, call.id, at=self._clock(), event_id=event_id)
        self._written_notes.pop(call.id, None)
        logger.info(
            "call_transcript_posted",
            room_id=call.room_id,
            call_id=call.id,
            event_id=event_id,
            with_notes=notes is not None,
            turns=len(turns),
            speech_seconds=sum(line.seconds for line in lines if line.reason != "paused"),
        )

    async def _postpone(self, call: StoredCall, *, failed: bool = True) -> None:
        """Make the call's message due again in a minute, counting a failed attempt unless ``failed`` is False."""
        await asyncio.to_thread(
            self._store.postpone_transcript,
            call.id,
            due_at=self._clock() + _RETRY_SECONDS,
            failed=failed,
        )
        logger.warning("call_transcript_postponed", room_id=call.room_id, call_id=call.id, failed=failed)

    def _claim_row(self, call: StoredCall) -> bool:
        """Claim posting the call's row: only if it has none and nobody is posting it already."""
        if call.row_event_id is not None or call.id in self._rows_posting:
            return False
        self._rows_posting.add(call.id)
        return True

    async def _post_row(self, call: StoredCall) -> str | None:
        """Post a claimed row with the room's lock released, then record it under the lock; return its event ID."""
        try:
            event_id = await self._room.post_row(call.room_id, call.id)
            if event_id is None:
                logger.info("call_transcript_row_not_posted", room_id=call.room_id, call_id=call.id)
                return None
            async with self._room_lock(call.room_id):
                await asyncio.to_thread(self._store.set_row_event_id, call.id, event_id)
                if (view := self._announced.get(call.id)) is not None:
                    self._announce_call(replace(view, row_event_id=event_id))
            return event_id
        finally:
            self._rows_posting.discard(call.id)

    async def _edit_row(self, call_id: str) -> None:
        """Edit an ended call's row until it says what it should, one edit at a time, so the last one sent is the latest.

        An edit that could not be sent is tried again by a tick a minute later.
        """
        if call_id in self._rows_editing:
            return
        self._rows_editing.add(call_id)
        try:
            while (call := await asyncio.to_thread(self._store.call, call_id)) is not None:
                owed = _owed_row_edit(call)
                if owed is None:
                    return
                edit, row = owed
                if await self._room.edit_row(row) is None:
                    logger.info("call_transcript_row_not_edited", room_id=call.room_id, call_id=call.id, edit=edit)
                    self._row_edits_retry_at[call.id] = self._clock() + _RETRY_SECONDS
                    return
                await asyncio.to_thread(self._store.record_row_edit, call.id, edit)
                self._row_edits_retry_at.pop(call.id, None)
        finally:
            self._rows_editing.discard(call_id)

    async def _call_for(self, call_id: str, user_id: str) -> StoredCall:
        """Return the call, taking its room from the call itself; to anyone outside that room it does not exist."""
        call = await self._fresh(call_id)
        await self._require_member(call.room_id, user_id, _NO_SUCH_CALL)
        return call

    async def _fresh(self, call_id: str) -> StoredCall:
        call = await asyncio.to_thread(self._store.call, call_id)
        if call is None:
            raise CallRefusedError(code="not-found", message=_NO_SUCH_CALL)
        return call

    async def _require_member(self, room_id: str, user_id: str, not_found: str) -> None:
        member = await self._room.is_member(room_id, user_id)
        if member is None:
            raise CallRefusedError(code="unavailable", message="The room cannot be checked right now.")
        if not member:
            raise CallRefusedError(code="not-found", message=not_found)

    def _room_lock(self, room_id: str) -> asyncio.Lock:
        return self._room_locks.setdefault(room_id, asyncio.Lock())

    def _announce(self, room_id: str, event: CallEvent) -> None:
        for listener in tuple(self._listeners.get(room_id, ())):
            try:
                listener(event)
            except Exception:
                logger.exception("call_transcript_listener_failed", room_id=room_id)


def _require_poster(may_post: bool | None) -> None:
    if may_post is None:
        raise CallRefusedError(code="unavailable", message="The room cannot be checked right now.")
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


def _matrixrtc_members(state: Mapping[str, float], presence: Mapping[str, AppPresence]) -> frozenset[str]:
    """Return who is in the room's MatrixRTC call, without memberships created before that person's app was last in it.

    A crashed app's membership can outlive it for hours on homeservers without delayed leave events; it is older than
    the app's last check-in, so it never keeps this call or a later one in the room live. Memberships are dated by the
    homeserver's clock and check-ins by MindRoom's, so after an app left one dated up to a few seconds before its leave
    still counts: the person rejoined from another app. A crashed app gets no such allowance, because its device joins
    the MatrixRTC call only moments before the app's first check-in.
    """
    return frozenset(
        user_id
        for user_id, created_at in state.items()
        if (app := presence.get(user_id)) is None
        or created_at >= app.present_at - (_CLOCK_TOLERANCE_SECONDS if app.left else 0.0)
    )


def _call_view(
    call: StoredCall,
    *,
    members: Iterable[str] = (),
    holds: Sequence[str] = (),
    transcribed_by: Iterable[str] = (),
) -> Call:
    return Call(
        id=call.id,
        room_id=call.room_id,
        row_event_id=call.row_event_id,
        started_at=call.started_at,
        ended_at=call.ended_at,
        members=tuple(sorted(members)),
        holds=tuple(holds),
        transcribed_by=tuple(sorted(transcribed_by)),
    )


def _turns(lines: Sequence[StoredLine]) -> tuple[_Turn, ...]:
    """Return the call's speech as turns: one speaker's stretches in a row make one turn, and gaps are never turns."""
    turns: list[_Turn] = []
    previous: StoredLine | None = None
    for line in lines:
        if line.kind == "speech" and line.text is not None:
            if previous is not None and previous.kind == "speech" and previous.user_id == line.user_id:
                turns[-1] = _Turn(sender=line.user_id, text=f"{turns[-1].text} {line.text}")
            else:
                turns.append(_Turn(sender=line.user_id, text=line.text))
        previous = line
    return tuple(turns)


def _minutes(duration_seconds: int) -> int:
    """Return a call's length in whole minutes, at least one, as its row and message say it."""
    return max(1, round(duration_seconds / 60))


def _owed_row_edit(call: StoredCall) -> tuple[RowEdit, EndedRow] | None:
    """Return the edit an ended call's posted row still owes: its end, then also its transcript message once posted."""
    if call.ended_at is None or call.row_event_id is None or call.row_edit == "transcript":
        return None
    edit: RowEdit = "ended" if call.transcript_event_id is None else "transcript"
    if call.row_edit == edit:
        return None
    duration_seconds = round(call.ended_at - call.started_at)
    return edit, EndedRow(
        room_id=call.room_id,
        call_id=call.id,
        row_event_id=call.row_event_id,
        body=f"Call ended · {_minutes(duration_seconds)} min",
        ended_at=call.ended_at,
        duration_seconds=duration_seconds,
        transcript_event_id=call.transcript_event_id,
    )


def iso_time(seconds: float) -> str:
    """Return a time in seconds since the epoch as ISO 8601 in UTC, to the millisecond, as clients read calls' times."""
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
