"""The rules of a call between people: who is in it, who may act on it, Pause, speech, its end, its row and message.

The service does no Matrix or network work of its own: the room side (the router's view of the room), the transcriber
and the notes writer are passed in, and the store is called in a worker thread.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol

from mindroom.background_tasks import create_background_task
from mindroom.call_transcripts.wav import WavError, wav_seconds
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Mapping, Sequence

    from mindroom.call_transcripts.store import CallTranscriptStore, RowEdit, StoredCall, StoredLine

logger = get_logger(__name__)

# The largest stretch of speech accepted, in bytes.
MAX_SPEECH_BYTES = 1_000_000
_MAX_SPEECH_SECONDS = 30.0
_SPEECH_PER_MINUTE = 240
_SPEECH_WINDOW_SECONDS = 60.0
_PRESENCE_SECONDS = 60.0
_CHECK_SECONDS = 15.0
_TRANSCRIPT_DELAY_SECONDS = 20.0
# How long a call's Matrix writes wait, after one could not be made, before they are tried again.
_RETRY_SECONDS = 60.0
_NOTES_ATTEMPTS = 2
_SPEAKING_RETRY_AFTER_SECONDS = 3
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
    """Whose apps are in the call now."""
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

    def router_joined(self, room_id: str) -> bool | None:
        """Return whether the router is joined to the room, None while it is not ready."""
        ...

    async def is_member(self, room_id: str, user_id: str) -> bool | None:
        """Return whether ``user_id`` is joined to the room, None when the router cannot tell."""
        ...

    async def may_post(self, room_id: str, user_id: str) -> bool | None:
        """Return whether ``user_id`` may post messages in the room by its power levels, None when it cannot tell."""
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

    def writes_notes(self) -> bool:
        """Return whether a notes model is set, so notes will be written."""
        ...

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
        """When this MindRoom began serving calls: every app gets a full minute from then to check in."""
        self._room_locks: dict[str, asyncio.Lock] = {}
        self._speaking: dict[str, _Stretch] = {}
        self._recent_speech: dict[str, deque[float]] = {}
        self._settling: dict[str, asyncio.Task[None]] = {}
        """Each call's one task making its Matrix writes, by call, so no call's writes wait for another's."""
        self._written_notes: dict[str, str] = {}

    async def join(self, room_id: str, user_id: str) -> Call:
        """Join the room's live call, starting it if there is none; from someone already in it, this is the heartbeat.

        Coming into the call needs permission to post in the room; a heartbeat does not check it again.
        """
        live = await asyncio.to_thread(self._store.live_call, room_id)
        await self._require_member(room_id, user_id, _NOT_IN_ROOM, call=live)
        # The room side is read before taking the room's lock and never under it, so a slow homeserver cannot hold
        # up heartbeats. Whether posting is needed is decided under the lock, with the call being joined.
        may_post = await self._room.may_post(room_id, user_id)
        async with self._room_lock(room_id):
            now = self._clock()
            live = await asyncio.to_thread(self._store.live_call, room_id)
            if live is None or user_id not in await self._present(live, now):
                _require_poster(may_post)
            call, started = await asyncio.to_thread(self._store.ensure_live_call, room_id, at=now)
            await asyncio.to_thread(self._store.record_presence, call.id, user_id, at=now)
            if started:
                logger.info("call_transcript_call_started", room_id=room_id, call_id=call.id)
            self._settle(call, now)
            return _call_view(call, await self._present(call, now))

    async def leave(self, call_id: str, user_id: str) -> None:
        """Take the person's app out of the call at once; the last one out ends the call.

        Someone whose app was never in the call records nothing. A leave is never a check-in, so one refused while the
        room cannot be read leaves the person to drop out of the call a minute after their app last checked in.
        """
        call = await self._fresh(call_id)
        await self._require_member(call.room_id, user_id, _NO_SUCH_CALL)
        await self._leave(call, user_id)

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
        A stretch said before its call ended is accepted for 20 seconds after the end; one said while transcription was
        paused never is.
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
            if text == "":
                # Speech-to-text heard no words: nothing is missing from the transcript, so there is no gap either.
                return None
            async with self._room_lock(call.room_id):
                self._done_speaking(user_id, stretch)
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
            self._done_speaking(user_id, stretch)
        if line is None:
            raise CallRefusedError(code="forbidden", message=_TOO_LATE)
        return line

    async def check_speech_sender(self, call_id: str, user_id: str) -> None:
        """Refuse a stretch before its audio is read: from outside the call's room, or with one still in flight."""
        await self._call_for(call_id, user_id)
        self._refuse_second_stretch(user_id)

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
        """Hold the store to its limits while call transcripts are off, reading and writing nothing in Matrix.

        Stops the Matrix writes in progress, ends every live call, gives up every transcript message not yet posted,
        deleting its lines, and forgets finished calls. Rows stay as they are until call transcripts come back on
        within a day of their call's end, when their owed edits are made.
        """
        await self.stop()
        for call in await asyncio.to_thread(self._store.calls):
            async with self._room_lock(call.room_id):
                fresh = await asyncio.to_thread(self._store.call, call.id)
                if fresh is not None and fresh.ended_at is None:
                    fresh = await self._end(fresh)
                if fresh is not None and fresh.transcript_done_at is None:
                    await self._finish(fresh, given_up="call-transcripts-off")
        await self._forget_finished_calls(self._clock())

    async def _forget_finished_calls(self, now: float) -> None:
        await asyncio.to_thread(
            self._store.forget_finished_calls,
            before=now - _FORGET_AFTER_SECONDS,
            ended_before=now - _GIVE_UP_AFTER_SECONDS,
        )

    async def stop(self) -> None:
        """Cancel the calls' Matrix writes in progress: each is made after the next start, and posted once."""
        tasks = list(self._settling.values())
        for task in tasks:
            task.cancel()
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
            if fresh.id not in self._settling and (reason := self._lost_cause(fresh, now)) is not None:
                await self._finish(fresh, given_up=reason)
                fresh = await self._fresh(fresh.id)
            self._settle(fresh, now)
            return fresh

    def _lost_cause(self, call: StoredCall, now: float) -> str | None:
        """Return why the ended call's pending message can no longer be posted, None if it still can be."""
        if call.ended_at is None or call.transcript_done_at is not None:
            return None
        if now - call.ended_at >= _GIVE_UP_AFTER_SECONDS:
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
        """End a live call nobody is in, under the room's lock: its pause ends, and its message falls due 20 seconds later.

        It ended when an app in it last checked in or left, never before it started: not when that is noticed, which
        may be hours later after MindRoom was down. Returns the call as it is then.
        """
        if await self._present(call, now):
            return call
        return await self._end(call)

    async def _end(self, call: StoredCall) -> StoredCall:
        """End a live call when an app in it last checked in or left, never before it started; return it ended."""
        presence = await asyncio.to_thread(self._store.presence, call.id)
        await asyncio.to_thread(
            self._store.end_call,
            call.id,
            at=max([call.started_at, *(app.present_at for app in presence.values())]),
        )
        logger.info("call_transcript_call_ended", room_id=call.room_id, call_id=call.id)
        return await self._fresh(call.id)

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
        if call.ended_at is None:
            await self._admit_to_live_call(call, user_id, now)
        else:
            await self._admit_to_ended_call(call, user_id, said_at=said_at, ended_at=call.ended_at, now=now)
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
        """Take speech only from someone whose app is in the call, and never while it is paused."""
        if user_id not in await self._present(call, now):
            raise CallRefusedError(code="forbidden", message=_NOT_IN_CALL)
        if call.paused_since is not None:
            raise CallRefusedError(code="paused", message="Transcription is paused.")

    async def _admit_to_ended_call(
        self,
        call: StoredCall,
        user_id: str,
        *,
        said_at: float,
        ended_at: float,
        now: float,
    ) -> None:
        """Take late speech said before the end from someone whose app was in the call, for 20 seconds after the end.

        The transcript message is begun no sooner, so a retry of its post sends just what the first attempt did.
        """
        if call.transcript_done_at is not None or now >= ended_at + _TRANSCRIPT_DELAY_SECONDS:
            raise CallRefusedError(code="forbidden", message=_TOO_LATE)
        if said_at >= ended_at:
            raise CallRefusedError(code="forbidden", message="That was said after the call ended.")
        if user_id not in await asyncio.to_thread(self._store.presence, call.id):
            raise CallRefusedError(code="forbidden", message="You were not in this call.")

    def _refuse_second_stretch(self, user_id: str) -> None:
        if user_id in self._speaking:
            raise CallRefusedError(
                code="too-many-requests",
                message="Your previous stretch of speech is still being transcribed.",
                retry_after=_SPEAKING_RETRY_AFTER_SECONDS,
            )

    def _count_speech(self, user_id: str, now: float) -> None:
        """Refuse a second stretch in flight, or more than 240 a minute, from one person; otherwise count this one."""
        self._refuse_second_stretch(user_id)
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

    def _settle(self, call: StoredCall, now: float) -> None:
        """Start the call's Matrix writes in a task of its own, unless none are owed, can be made now, or one is running."""
        if call.id not in self._settling and self._can_settle(call, now):
            self._settling[call.id] = create_background_task(self._settle_now(call.id), name="call_transcript_settle")

    def _can_settle(self, call: StoredCall, now: float) -> bool:
        """Return whether the router can make Matrix writes the call owes now: its row, its due message, a row edit.

        Nothing is owed a day after the call's end, and nothing is tried while the writes wait after a failure.
        """
        if self._room.router_joined(call.room_id) is not True or (call.retry_at is not None and call.retry_at > now):
            return False
        if call.ended_at is not None and now - call.ended_at >= _GIVE_UP_AFTER_SECONDS:
            return False
        return call.row_event_id is None or self._transcript_due(call, now) or _owed_row_edit(call) is not None

    async def _settle_now(self, call_id: str) -> None:
        """Make the call's Matrix writes one at a time, in order: its row, its message once due, then the row's edits.

        Each is sent under a transaction of its own, so making it again never posts twice. A write that cannot be
        made holds them all a minute, when a tick starts them again.
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
        finally:
            self._settling.pop(call_id, None)

    async def _settle_step(self, call: StoredCall) -> None:
        """Make the first Matrix write the call owes, or hold its writes a minute if it cannot be made."""
        if call.row_event_id is None:
            event_id = await self._room.post_row(call.room_id, call.id)
            if event_id is None:
                await self._postpone(call, reason="row-not-posted", failed=False)
            else:
                await asyncio.to_thread(self._store.set_row_event_id, call.id, event_id)
        elif self._transcript_due(call, self._clock()):
            await self._post_transcript(call, call.row_event_id)
        elif (owed := _owed_row_edit(call)) is not None:
            edit, row = owed
            if await self._room.edit_row(row) is None:
                await self._postpone(call, reason="row-not-edited", failed=False)
            else:
                await asyncio.to_thread(self._store.record_row_edit, call.id, edit)

    def _transcript_due(self, call: StoredCall, now: float) -> bool:
        """Return whether the ended call's message is due: 20 seconds after the end, with none of its speech in flight."""
        return (
            call.ended_at is not None
            and call.transcript_done_at is None
            and now >= call.ended_at + _TRANSCRIPT_DELAY_SECONDS
            and all(stretch.call_id != call.id for stretch in self._speaking.values())
        )

    async def _post_transcript(self, call: StoredCall, row_event_id: str) -> None:
        """Post the ended call's one message in its row's thread, with notes while they can be had, or hold it a minute.

        A call in which people spoke but none of it could be transcribed posts a message that says so, without turns.
        """
        assert call.ended_at is not None
        lines = await asyncio.to_thread(self._store.lines, call.id)
        turns = _turns(lines)
        if not turns and all(line.reason != "not-transcribed" for line in lines):
            await self._finish(call)
            return
        notes = self._written_notes.get(call.id)
        writes_notes = (
            bool(turns) and notes is None and call.transcript_attempts < _NOTES_ATTEMPTS and self._notes.writes_notes()
        )
        # Only notes name people, by display name; until the room can be read they wait, without using an attempt.
        names = await self._room.display_names(call.room_id, {turn.sender for turn in turns}) if writes_notes else {}
        if names is None:
            await self._postpone(call, reason="names-unreadable", failed=False)
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
                if call.transcript_attempts + 1 < _NOTES_ATTEMPTS:
                    await self._postpone(call, reason="notes-failed")
                    return
            if notes is not None:
                self._written_notes[call.id] = notes
        message = TranscriptMessage(
            room_id=call.room_id,
            call_id=call.id,
            row_event_id=row_event_id,
            body=notes if notes is not None else _summary(transcript),
            transcript=transcript,
        )
        event_id = await self._room.post_transcript(message)
        if event_id is None:
            await self._postpone(call, reason="transcript-not-posted")
            return
        await self._finish(call, event_id=event_id, with_notes=notes is not None, turns=len(turns))

    async def _postpone(self, call: StoredCall, *, reason: str, failed: bool = True) -> None:
        """Hold the call's Matrix writes a minute, counting a failed message attempt unless ``failed`` is False."""
        await asyncio.to_thread(self._store.postpone, call.id, until=self._clock() + _RETRY_SECONDS, failed=failed)
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
        ``given_up`` says why.
        """
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
            raise CallRefusedError(code="unavailable", message="The room cannot be checked right now.")
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
        """Take the person's app out of a live call at once, unless it was never in it; the last one out ends it.

        An ending edits the row at once, in the call's own task.
        """
        async with self._room_lock(call.room_id):
            call = await self._fresh(call.id)
            if call.ended_at is not None or user_id not in await asyncio.to_thread(self._store.presence, call.id):
                return
            now = self._clock()
            await asyncio.to_thread(self._store.record_presence, call.id, user_id, at=now, left=True)
            self._settle(await self._end_if_empty(call, now), now)

    def _room_lock(self, room_id: str) -> asyncio.Lock:
        return self._room_locks.setdefault(room_id, asyncio.Lock())


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


def _call_view(call: StoredCall, members: Iterable[str] = ()) -> Call:
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
    summary = f"Call · {_minutes(transcript.duration_seconds)} min"
    return summary if transcript.turns else f"{summary}. Nothing could be transcribed."


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
