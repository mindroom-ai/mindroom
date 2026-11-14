"""Index thread snoozes and wake the due ones through an injected delivery."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from pydantic import ValidationError

from mindroom.logging_config import get_logger
from mindroom.thread_tags import (
    SNOOZED_THREAD_TAG,
    THREAD_TAGS_EVENT_TYPE,
    ThreadTagRecord,
    parse_thread_tag_state_key,
    snooze_until,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

logger = get_logger(__name__)

SNOOZE_RETRY_DELAY = timedelta(seconds=30)
SNOOZE_STALE_AFTER = timedelta(hours=24)
SNOOZE_NOTE_MAX_CHARS = 200
THREAD_SNOOZE_CONTENT_KEY = "io.mindroom.thread_snooze"

__all__ = [
    "SNOOZE_NOTE_MAX_CHARS",
    "SNOOZE_RETRY_DELAY",
    "SNOOZE_STALE_AFTER",
    "THREAD_SNOOZE_CONTENT_KEY",
    "Snooze",
    "SnoozeDelivery",
    "SnoozeUnavailableError",
    "ThreadSnoozeRuntime",
    "snooze_from_state_event",
    "wake_message_body",
]

type _ThreadKey = tuple[str, str]


class SnoozeUnavailableError(RuntimeError):
    """A Matrix call failed; try again later."""


@dataclass(frozen=True, slots=True)
class Snooze:
    """One snoozed thread and how to announce its end."""

    room_id: str
    thread_root_id: str
    until: datetime
    note: str | None
    mention: str | None


def snooze_from_state_event(
    room_id: str,
    source: Mapping[str, object],
    *,
    is_bot: Callable[[str], bool],
    is_mindroom_account: Callable[[str], bool],
) -> tuple[str, Snooze | None] | None:
    """Read a raw thread tag state event as a snooze, a removal, or neither.

    ``is_bot`` names every bot, which is never mentioned; ``is_mindroom_account`` names the accounts MindRoom runs,
    the only ones trusted to write a snooze on behalf of the person it names.
    """
    if source.get("type") != THREAD_TAGS_EVENT_TYPE:
        return None
    parsed_state_key = parse_thread_tag_state_key(source.get("state_key"))
    if parsed_state_key is None or parsed_state_key[1] != SNOOZED_THREAD_TAG:
        return None
    thread_root_id = parsed_state_key[0]

    try:
        record = ThreadTagRecord.model_validate(source.get("content"))
    except ValidationError:
        return thread_root_id, None
    until = snooze_until(record)
    if until is None:
        return thread_root_id, None

    # A person may only name themselves; a MindRoom account writes on behalf of the person it names.
    sender = source.get("sender")
    sender_vouches = sender == record.set_by or (isinstance(sender, str) and is_mindroom_account(sender))
    mention = record.set_by if sender_vouches and not is_bot(record.set_by) else None
    return thread_root_id, Snooze(
        room_id=room_id,
        thread_root_id=thread_root_id,
        until=until,
        note=record.note,
        mention=mention,
    )


def wake_message_body(note: str | None) -> str:
    """Return the wake message body with the note on one line, cut to its limit."""
    one_line_note = " ".join((note or "").split())[:SNOOZE_NOTE_MAX_CHARS]
    if not one_line_note:
        return "⏰ Snooze ended"
    return f"⏰ Snooze ended: {one_line_note}"


class SnoozeDelivery(Protocol):
    """Matrix work the runtime needs to wake one thread."""

    async def current_until(self, room_id: str, thread_root_id: str) -> datetime | None:
        """Return the wake time now in room state, or None when the snooze is gone or no bot can see the room."""
        ...

    async def is_resolved(self, room_id: str, thread_root_id: str) -> bool:
        """Return whether the thread carries the resolved tag."""
        ...

    async def clear(self, room_id: str, thread_root_id: str) -> bool:
        """Remove the snooze; False when no bot may write it."""
        ...

    async def send_ended(self, snooze: Snooze) -> bool:
        """Post the wake message; False when it was not delivered."""
        ...


@dataclass(slots=True)
class _Entry:
    snooze: Snooze
    next_try: datetime


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ThreadSnoozeRuntime:
    """In-memory index of snoozed threads that wakes each one at its time."""

    def __init__(self, delivery: SnoozeDelivery, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._delivery = delivery
        self._clock = clock
        self._snoozes: dict[_ThreadKey, _Entry] = {}
        # Wake messages whose snooze is already cleared; kept apart so the sync echo of the removal keeps them.
        self._pending_sends: dict[_ThreadKey, _Entry] = {}
        self._changed = asyncio.Event()

    def track(self, snooze: Snooze) -> None:
        """Index a snooze, replacing any earlier one for the same thread."""
        self._snoozes[snooze.room_id, snooze.thread_root_id] = _Entry(snooze, snooze.until)
        self._changed.set()

    def forget(self, room_id: str, thread_root_id: str) -> None:
        """Drop a thread's snooze from the index."""
        self._snoozes.pop((room_id, thread_root_id), None)
        self._changed.set()

    def _next_due(self) -> datetime | None:
        """Return when the next wake or retry is due."""
        return min(
            (entry.next_try for entry in (*self._snoozes.values(), *self._pending_sends.values())),
            default=None,
        )

    async def _wake_due(self) -> None:
        """Wake every snooze and retry every wake message whose time has come."""
        now = self._clock()
        for key, entry in list(self._pending_sends.items()):
            if entry.next_try <= now:
                await self._retry_send(key, entry)
        for key, entry in list(self._snoozes.items()):
            if entry.next_try <= now and self._snoozes.get(key) is entry:
                try:
                    await self._wake(key, entry)
                except SnoozeUnavailableError as exc:
                    logger.warning("thread_snooze_wake_retry", room_id=key[0], thread_root_id=key[1], error=str(exc))
                    if self._snoozes.get(key) is entry:
                        entry.next_try = self._clock() + SNOOZE_RETRY_DELAY

    async def run(self) -> None:
        """Wake due snoozes, then sleep until the next one or until the index changes."""
        while True:
            self._changed.clear()
            await self._wake_due()
            due = self._next_due()
            timeout = None if due is None else max(0.0, (due - self._clock()).total_seconds())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout)

    async def _wake(self, key: _ThreadKey, entry: _Entry) -> None:
        snooze = entry.snooze
        current_until = await self._delivery.current_until(*key)
        if self._snoozes.get(key) is not entry:
            return
        if current_until is None:
            self._drop(key, entry)
            return
        if current_until != snooze.until:
            self.track(replace(snooze, until=current_until))
            return

        if self._clock() - snooze.until > SNOOZE_STALE_AFTER:
            cleared = await self._delivery.clear(*key)
            if cleared:
                logger.info("thread_snooze_stale_cleared", room_id=key[0], thread_root_id=key[1])
            else:
                logger.warning("thread_snooze_clear_not_permitted", room_id=key[0], thread_root_id=key[1])
            self._drop(key, entry)
            return

        resolved = await self._delivery.is_resolved(*key)
        if self._snoozes.get(key) is not entry:
            return
        if not await self._delivery.clear(*key):
            logger.warning("thread_snooze_clear_not_permitted", room_id=key[0], thread_root_id=key[1])
            self._drop(key, entry)
            return
        self._drop(key, entry)
        if not resolved:
            await self._send(key, snooze)

    async def _send(self, key: _ThreadKey, snooze: Snooze) -> None:
        if await self._delivery.send_ended(snooze):
            self._pending_sends.pop(key, None)
            return
        logger.warning("thread_snooze_send_retry", room_id=key[0], thread_root_id=key[1])
        self._pending_sends[key] = _Entry(snooze, self._clock() + SNOOZE_RETRY_DELAY)

    async def _retry_send(self, key: _ThreadKey, entry: _Entry) -> None:
        if self._clock() - entry.snooze.until > SNOOZE_STALE_AFTER:
            logger.info("thread_snooze_stale_send_dropped", room_id=key[0], thread_root_id=key[1])
            self._pending_sends.pop(key, None)
            return
        await self._send(key, entry.snooze)

    def _drop(self, key: _ThreadKey, entry: _Entry) -> None:
        if self._snoozes.get(key) is entry:
            del self._snoozes[key]
