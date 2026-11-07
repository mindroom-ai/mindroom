"""The router's side of the rooms calls are in: who is in a room, who may post, what the router posts.

Everything is read and sent through the router's own Matrix client, and nothing while the router is not ready. One
full read of a room's state answers every question about that room for a few seconds, however many heartbeats ask.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import aiohttp
import nio

from mindroom.call_transcripts.service import iso_time
from mindroom.constants import CALL_TRANSCRIPT_CONTENT_KEY, SKIP_MENTIONS_KEY
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import edit_message_result, send_message_result
from mindroom.matrix.message_builder import build_message_content
from mindroom.matrix.room_power import privileged_creators, user_power_level

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Mapping

    from mindroom.call_transcripts.service import EndedRow, TranscriptMessage
    from mindroom.matrix.client_delivery import DeliveredMatrixEvent

logger = get_logger(__name__)

# The row notice names its call, and the transcript message the call it finishes.
_CALL_CONTENT_KEY = "io.mindroom.call"
_CALL_NOTES_CONTENT_KEY = "io.mindroom.call_notes"
_ROW_BODY = "Call started"
# What reading or sending through the client can raise: a network failure, a stopped client, an unreadable answer.
_MATRIX_ERRORS = (nio.exceptions.ProtocolError, OSError, aiohttp.ClientError, ValueError)
# How long one full read of a room's state answers the questions asked about the room.
_STATE_REUSE_SECONDS = 5.0
# A read of a room's state not answered in this long counts as a room that cannot be read.
_STATE_READ_TIMEOUT_SECONDS = 20.0
_MENTION_START = re.compile(r"(?<!\w)@")


@dataclass(frozen=True, slots=True)
class _RoomState:
    """One full read of a room's state, each event by its type and state key."""

    events: Mapping[tuple[str, str], dict[str, Any]]

    def content(self, event_type: str, state_key: str = "") -> Mapping[str, object]:
        """Return one state event's content, empty when the room has none."""
        content = self.events.get((event_type, state_key), {}).get("content")
        return content if isinstance(content, dict) else {}

    def creators(self) -> frozenset[str]:
        """Return the users the room's version gives unlimited power, which power levels never list."""
        return privileged_creators(self.events.get(("m.room.create", ""), {}))


@dataclass(frozen=True, slots=True)
class _StateRead:
    """A read of a room's state that the questions asked while it is recent share."""

    started_at: float
    result: asyncio.Future[_RoomState | None]

    def reusable(self, now: float) -> bool:
        """Return whether it is still in flight, or read the state less than a few seconds ago."""
        if not self.result.done():
            return True
        read = not self.result.cancelled() and self.result.exception() is None and self.result.result() is not None
        return read and now - self.started_at < _STATE_REUSE_SECONDS


@dataclass(frozen=True, slots=True)
class RouterCallRoom:
    """The rooms calls are in, as the router sees them; while it is not ready nothing can be told or posted."""

    client: Callable[[], nio.AsyncClient | None]
    """The router's client while it runs and has synced, else None."""
    monotonic: Callable[[], float] = time.monotonic
    """A clock that never jumps, which times how long one read of a room's state is reused."""
    _reads: dict[str, _StateRead] = field(default_factory=dict, init=False, repr=False, compare=False)

    def router_joined(self, room_id: str) -> bool | None:
        """Return whether the router is joined to the room, as of its last sync; None while it is not ready."""
        client = self.client()
        return None if client is None else room_id in client.rooms

    async def is_member(self, room_id: str, user_id: str) -> bool | None:
        """Return whether the person is joined to the room; nobody is a member of a room the router is not in."""
        client = self.client()
        if client is None:
            return None
        if room_id not in client.rooms:
            return False
        state = await self._state(client, room_id)
        return None if state is None else state.content("m.room.member", user_id).get("membership") == "join"

    async def may_post(self, room_id: str, user_id: str) -> bool | None:
        """Return whether the room's power levels let the person send ``m.room.message`` events."""
        client = self.client()
        if client is None:
            return None
        if room_id not in client.rooms:
            return False
        state = await self._state(client, room_id)
        if state is None:
            return None
        # Without power levels everyone is at level 0, which may post; room version 12 creators are never listed.
        power_levels = state.content("m.room.power_levels")
        required = _message_power_level(power_levels)
        return user_id in state.creators() or user_power_level(power_levels, user_id) >= required

    async def display_names(self, room_id: str, user_ids: Collection[str]) -> Mapping[str, str | None] | None:
        """Return each person's display name from their membership in the room, None for someone without one.

        None when the room cannot be read, such as while the router is not ready or not in the room.
        """
        client = self.client()
        if client is None or room_id not in client.rooms:
            return None
        state = await self._state(client, room_id)
        if state is None:
            return None
        return {user_id: _display_name(state.content("m.room.member", user_id)) for user_id in user_ids}

    async def post_row(self, room_id: str, call_id: str) -> str | None:
        """Post the call's row notice: it wakes no agent and mentions nobody."""
        content = {
            "msgtype": "m.notice",
            "body": _ROW_BODY,
            _CALL_CONTENT_KEY: {"call_id": call_id},
            "m.mentions": {},
            SKIP_MENTIONS_KEY: True,
        }
        return await self._post(room_id, content, transaction_id=f"mindroom-call-{call_id}-row", operation="call_row")

    async def edit_row(self, row: EndedRow) -> str | None:
        """Edit the call's row to say the call ended and how long it lasted, then also where its transcript message is.

        Still a notice that wakes no agent and mentions nobody. Each of the two edits is its own transaction.
        """
        call: dict[str, object] = {
            "call_id": row.call_id,
            "ended_at": iso_time(row.ended_at),
            "duration_seconds": row.duration_seconds,
        }
        if row.transcript_event_id is not None:
            call["transcript_event_id"] = row.transcript_event_id
        content = {
            "msgtype": "m.notice",
            "body": row.body,
            _CALL_CONTENT_KEY: call,
            "m.mentions": {},
            SKIP_MENTIONS_KEY: True,
        }
        edit = "ended" if row.transcript_event_id is None else "transcript"
        return await self._send(
            row.room_id,
            lambda client: edit_message_result(
                client,
                row.room_id,
                row.row_event_id,
                content,
                row.body,
                transaction_id=f"mindroom-call-{row.call_id}-row-{edit}",
            ),
            operation="call_row_edit",
        )

    async def post_transcript(self, message: TranscriptMessage) -> str | None:
        """Post the call's one message in the row's thread: the notes, the transcript and the call it finishes.

        A transcript too large for one event goes into a sidecar on the way, and the message is not posted while
        the sidecar cannot be uploaded: a cut-short text without the transcript, sent under the call's transaction,
        could never be replaced. Mentions in the notes are neutralised.
        """
        transcript = message.transcript
        content = build_message_content(
            _without_mentions(message.body),
            thread_event_id=message.row_event_id,
            latest_thread_event_id=message.row_event_id,
            extra_content={
                "msgtype": "m.notice",
                "m.mentions": {},
                SKIP_MENTIONS_KEY: True,
                CALL_TRANSCRIPT_CONTENT_KEY: {
                    "duration_seconds": transcript.duration_seconds,
                    "turns": [{"sender": turn.sender, "text": turn.text} for turn in transcript.turns],
                },
                _CALL_NOTES_CONTENT_KEY: {"call_id": message.call_id},
            },
        )
        return await self._post(
            message.room_id,
            content,
            transaction_id=f"mindroom-call-{message.call_id}-transcript",
            operation="call_transcript",
            sidecar_required=True,
        )

    async def _state(self, client: nio.AsyncClient, room_id: str) -> _RoomState | None:
        """Return the room's state from one read shared by everyone asking while it is recent; None if unreadable.

        A read in flight is joined rather than repeated, and a failed one is never reused. Waiting never cancels the
        read, so a caller that goes away leaves it running for the others.
        """
        now = self.monotonic()
        for stale in [room for room, read in self._reads.items() if not read.reusable(now)]:
            del self._reads[stale]
        read = self._reads.get(room_id)
        if read is None:
            result = asyncio.ensure_future(_read_state(client, room_id))
            result.add_done_callback(_retrieve_failure)
            read = self._reads[room_id] = _StateRead(now, result)
        await asyncio.wait([read.result])
        return read.result.result()

    async def _post(
        self,
        room_id: str,
        content: dict[str, Any],
        *,
        transaction_id: str,
        operation: str,
        sidecar_required: bool = False,
    ) -> str | None:
        """Send one message as a transaction derived from its call, so sending it again never posts a second one."""
        return await self._send(
            room_id,
            lambda client: send_message_result(
                client,
                room_id,
                content,
                operation=operation,
                transaction_id=transaction_id,
                sidecar_required=sidecar_required,
            ),
            operation=operation,
        )

    async def _send(
        self,
        room_id: str,
        send: Callable[[nio.AsyncClient], Awaitable[DeliveredMatrixEvent | None]],
        *,
        operation: str,
    ) -> str | None:
        """Send through the router's client while it is ready; return the event's ID, None if it was not sent."""
        client = self.client()
        if client is None:
            return None
        try:
            delivered = await send(client)
        except _MATRIX_ERRORS as error:
            logger.warning("call_transcript_post_failed", room_id=room_id, operation=operation, error=str(error))
            return None
        return None if delivered is None else delivered.event_id


def _retrieve_failure(read: asyncio.Future[_RoomState | None]) -> None:
    """Mark a finished read's failure as retrieved, so one that fails after everyone waiting went away logs no error."""
    if not read.cancelled():
        read.exception()


async def _read_state(client: nio.AsyncClient, room_id: str) -> _RoomState | None:
    """Read the room's whole state; None when it cannot be read, as when the homeserver does not answer in time."""
    try:
        async with asyncio.timeout(_STATE_READ_TIMEOUT_SECONDS):
            response = await client.room_get_state(room_id)
    except _MATRIX_ERRORS as error:
        logger.warning("call_transcript_room_state_unreadable", room_id=room_id, error=str(error))
        return None
    if not isinstance(response, nio.RoomGetStateResponse):
        logger.warning("call_transcript_room_state_unreadable", room_id=room_id, error=str(response))
        return None
    return _RoomState(
        {
            (event_type, state_key): event
            for event in response.events
            if isinstance(event_type := event.get("type"), str) and isinstance(state_key := event.get("state_key"), str)
        },
    )


def _message_power_level(power_levels: Mapping[str, object]) -> int:
    """Return the power level ``m.room.message`` needs: its own entry, else ``events_default``, else 0."""
    events = power_levels.get("events")
    level = cast("Mapping[str, object]", events).get("m.room.message") if isinstance(events, dict) else None
    if type(level) is int:
        return level
    default = power_levels.get("events_default")
    return default if type(default) is int else 0


def _display_name(member: Mapping[str, object]) -> str | None:
    name = member.get("displayname")
    return name if isinstance(name, str) and name.strip() else None


def _without_mentions(text: str) -> str:
    """Return ``text`` with a zero-width space after each ``@`` that starts a word, so no Matrix ID or ``@room`` pings.

    An ``@`` inside a word, as in an email address, cannot start a mention and stays as it is.
    """
    return _MENTION_START.sub("@\u200b", text)
