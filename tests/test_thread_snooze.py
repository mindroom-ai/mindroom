"""Tests for the thread snooze wake runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from mindroom.thread_snooze import (
    SNOOZE_RETRY_DELAY,
    Snooze,
    SnoozeClear,
    SnoozeUnavailableError,
    ThreadSnoozeRuntime,
    snooze_from_state_event,
    wake_message_body,
)
from mindroom.thread_tags import THREAD_TAGS_EVENT_TYPE

ROOM = "!room:x"
ROOT = "$root"
PERSON = "@bas:x"
OTHER_PERSON = "@eve:x"
BOT = "@mindroom_router:x"
UNTIL = datetime(2026, 11, 14, 8, tzinfo=UTC)


BRIDGE = "@signalbot:x"


def _is_bot(user_id: str) -> bool:
    return user_id.startswith("@mindroom_") or user_id == BRIDGE


def _is_mindroom_account(user_id: str) -> bool:
    return user_id.startswith("@mindroom_")


def _parse(source: dict[str, object]) -> tuple[str, Snooze | None] | None:
    return snooze_from_state_event(ROOM, source, is_bot=_is_bot, is_mindroom_account=_is_mindroom_account)


def _source(
    *,
    sender: str = PERSON,
    state_key: str = f'["{ROOT}","snoozed"]',
    event_type: str = THREAD_TAGS_EVENT_TYPE,
    content: dict[str, object] | None = None,
) -> dict[str, object]:
    if content is None:
        content = {
            "set_by": PERSON,
            "set_at": "2026-11-13T08:00:00+00:00",
            "note": "check CI",
            "data": {"until": UNTIL.isoformat()},
        }
    return {"type": event_type, "state_key": state_key, "sender": sender, "content": content}


def test_state_event_from_its_own_sender_mentions_that_person() -> None:
    """A person who snoozes a thread is mentioned when it wakes."""
    assert _parse(_source()) == (
        ROOT,
        Snooze(room_id=ROOM, thread_root_id=ROOT, until=UNTIL, note="check CI", mention=PERSON),
    )


def test_state_event_naming_someone_else_mentions_nobody() -> None:
    """A person cannot make the wake message mention someone else."""
    parsed = _parse(_source(sender=OTHER_PERSON))
    assert parsed is not None
    _, snooze = parsed
    assert snooze is not None
    assert snooze.mention is None


def test_state_event_written_by_a_bot_vouches_for_the_requester() -> None:
    """A bot writes the snooze for the person who asked, so that person is mentioned."""
    parsed = _parse(_source(sender=BOT))
    assert parsed is not None
    _, snooze = parsed
    assert snooze is not None
    assert snooze.mention == PERSON


def test_state_event_written_by_a_bridge_bot_vouches_for_nobody() -> None:
    """Only MindRoom's own accounts write on behalf of the person they name; a bridge bot cannot."""
    parsed = _parse(_source(sender=BRIDGE))
    assert parsed is not None
    _, snooze = parsed
    assert snooze is not None
    assert snooze.mention is None


def test_state_event_naming_a_bridge_bot_mentions_nobody() -> None:
    """A bridge bot is never the person to mention, even when it set the snooze itself."""
    content = {"set_by": BRIDGE, "set_at": "2026-11-13T08:00:00+00:00", "data": {"until": UNTIL.isoformat()}}
    parsed = _parse(_source(sender=BRIDGE, content=content))
    assert parsed is not None
    _, snooze = parsed
    assert snooze is not None
    assert snooze.mention is None


def test_state_event_set_by_a_bot_mentions_nobody() -> None:
    """A snooze an agent set for itself mentions nobody."""
    content = {"set_by": BOT, "set_at": "2026-11-13T08:00:00+00:00", "data": {"until": UNTIL.isoformat()}}
    parsed = _parse(_source(sender=BOT, content=content))
    assert parsed is not None
    _, snooze = parsed
    assert snooze is not None
    assert snooze.mention is None
    assert snooze.note is None


@pytest.mark.parametrize(
    "content",
    [
        {},
        {"data": {"until": "x"}},
        {"set_by": PERSON, "set_at": "2026-11-13T08:00:00+00:00", "data": {"until": "x"}},
        {"set_by": PERSON, "set_at": "2026-11-13T08:00:00+00:00", "data": {}},
    ],
)
def test_removed_or_invalid_snooze_event_drops_the_thread(content: dict[str, object]) -> None:
    """A tombstone or an unreadable wake time removes the thread from the index."""
    assert _parse(_source(content=content)) == (ROOT, None)


@pytest.mark.parametrize(
    "source",
    [
        _source(state_key=f'["{ROOT}","resolved"]'),
        _source(event_type="m.room.message"),
        _source(state_key="not json"),
        {key: value for key, value in _source().items() if key != "state_key"},
    ],
)
def test_other_tags_and_other_event_types_are_not_snoozes(source: dict[str, object]) -> None:
    """Only snoozed thread tag state events concern the runtime."""
    assert _parse(source) is None


def test_wake_message_body() -> None:
    """The note goes on one line and is cut to 200 characters."""
    assert wake_message_body(None) == "⏰ Snooze ended"
    assert wake_message_body("  check\nCI  ") == "⏰ Snooze ended: check CI"
    assert wake_message_body("x" * 5000) == "⏰ Snooze ended: " + "x" * 200


@dataclass
class _Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass
class FakeDelivery:
    """Records calls and answers from per-method queues, defaulting to a normal wake."""

    until: dict[tuple[str, str], datetime | None] = field(default_factory=dict)
    resolved: bool = False
    clear_results: list[SnoozeClear | Exception] = field(default_factory=list)
    cleared_untils: list[datetime] = field(default_factory=list)
    send_results: list[bool] = field(default_factory=list)
    read_errors: int = 0
    calls: list[str] = field(default_factory=list)
    sent: list[Snooze] = field(default_factory=list)
    sent_event: asyncio.Event = field(default_factory=asyncio.Event)

    async def current_until(self, room_id: str, thread_root_id: str) -> datetime | None:
        """Return the indexed wake time unless told to fail the read."""
        self.calls.append("current_until")
        if self.read_errors:
            self.read_errors -= 1
            msg = "read failed"
            raise SnoozeUnavailableError(msg)
        return self.until.get((room_id, thread_root_id), UNTIL)

    async def is_resolved(self, room_id: str, thread_root_id: str) -> bool:  # noqa: ARG002
        """Return the configured resolved flag."""
        self.calls.append("is_resolved")
        return self.resolved

    async def clear(self, room_id: str, thread_root_id: str, until: datetime) -> SnoozeClear:  # noqa: ARG002
        """Return or raise the next queued clear result."""
        self.calls.append("clear")
        self.cleared_untils.append(until)
        result = self.clear_results.pop(0) if self.clear_results else "cleared"
        if isinstance(result, Exception):
            raise result
        return result

    async def send_ended(self, snooze: Snooze) -> bool:
        """Record a delivered wake message unless the next queued result is False."""
        self.calls.append("send_ended")
        result = self.send_results.pop(0) if self.send_results else True
        if result:
            self.sent.append(snooze)
            self.sent_event.set()
        return result


def _snooze(until: datetime = UNTIL, root: str = ROOT) -> Snooze:
    return Snooze(room_id=ROOM, thread_root_id=root, until=until, note="check CI", mention=PERSON)


def _runtime(now: datetime) -> tuple[ThreadSnoozeRuntime, FakeDelivery, _Clock]:
    delivery = FakeDelivery()
    clock = _Clock(now)
    return ThreadSnoozeRuntime(delivery, clock=clock), delivery, clock


@pytest.mark.asyncio
async def test_due_snooze_is_cleared_then_announced() -> None:
    """A due snooze is checked, cleared and announced once."""
    runtime, delivery, _ = _runtime(UNTIL)
    runtime.track(_snooze())
    assert runtime._next_due() == UNTIL

    await runtime._wake_due()

    assert delivery.calls == ["current_until", "is_resolved", "clear", "send_ended"]
    assert delivery.cleared_untils == [UNTIL]
    assert delivery.sent == [_snooze()]
    assert runtime._next_due() is None


@pytest.mark.asyncio
async def test_snooze_replaced_before_the_clear_is_kept_and_rechecked() -> None:
    """A snooze replaced between the read and the clear is not removed or announced; the next pass re-tracks it."""
    runtime, delivery, clock = _runtime(UNTIL)
    delivery.clear_results = ["changed"]
    runtime.track(_snooze())

    await runtime._wake_due()

    assert delivery.calls == ["current_until", "is_resolved", "clear"]
    assert delivery.sent == []
    assert runtime._next_due() == clock() + SNOOZE_RETRY_DELAY

    later = UNTIL + timedelta(hours=2)
    delivery.until[ROOM, ROOT] = later
    clock.now += SNOOZE_RETRY_DELAY
    await runtime._wake_due()

    assert delivery.calls == ["current_until", "is_resolved", "clear", "current_until"]
    assert delivery.sent == []
    assert runtime._next_due() == later


@pytest.mark.asyncio
async def test_stale_snooze_replaced_before_the_clear_is_kept() -> None:
    """A stale snooze replaced before its silent clear is kept for a re-check, not dropped."""
    runtime, delivery, clock = _runtime(UNTIL + timedelta(hours=25))
    delivery.clear_results = ["changed"]
    runtime.track(_snooze())

    await runtime._wake_due()

    assert delivery.calls == ["current_until", "clear"]
    assert runtime._next_due() == clock() + SNOOZE_RETRY_DELAY


@pytest.mark.asyncio
async def test_snooze_not_yet_due_is_left_alone() -> None:
    """Nothing happens before the wake time."""
    runtime, delivery, _ = _runtime(UNTIL - timedelta(seconds=1))
    runtime.track(_snooze())

    await runtime._wake_due()

    assert delivery.calls == []
    assert runtime._next_due() == UNTIL


@pytest.mark.asyncio
async def test_moved_snooze_is_reindexed_not_woken() -> None:
    """A snooze moved later in room state waits for its new time."""
    runtime, delivery, _ = _runtime(UNTIL)
    later = UNTIL + timedelta(hours=3)
    delivery.until[ROOM, ROOT] = later
    runtime.track(_snooze())

    await runtime._wake_due()

    assert delivery.calls == ["current_until"]
    assert runtime._next_due() == later


@pytest.mark.asyncio
async def test_removed_snooze_is_dropped() -> None:
    """A snooze removed from room state is forgotten without a message."""
    runtime, delivery, _ = _runtime(UNTIL)
    delivery.until[ROOM, ROOT] = None
    runtime.track(_snooze())

    await runtime._wake_due()

    assert delivery.calls == ["current_until"]
    assert runtime._next_due() is None


@pytest.mark.asyncio
async def test_forgotten_snooze_is_not_woken() -> None:
    """Forget removes a snooze from the index."""
    runtime, delivery, _ = _runtime(UNTIL)
    runtime.track(_snooze())
    runtime.forget(ROOM, ROOT)

    await runtime._wake_due()

    assert delivery.calls == []
    assert runtime._next_due() is None


@pytest.mark.asyncio
async def test_resolved_thread_is_cleared_without_a_message() -> None:
    """A resolved thread loses its snooze and gets no wake message."""
    runtime, delivery, _ = _runtime(UNTIL)
    delivery.resolved = True
    runtime.track(_snooze())

    await runtime._wake_due()

    assert delivery.calls == ["current_until", "is_resolved", "clear"]
    assert runtime._next_due() is None


@pytest.mark.asyncio
async def test_snooze_23_hours_late_still_announces() -> None:
    """A backend back within 24 hours still wakes the thread."""
    runtime, delivery, _ = _runtime(UNTIL + timedelta(hours=23))
    runtime.track(_snooze())

    await runtime._wake_due()

    assert delivery.calls == ["current_until", "is_resolved", "clear", "send_ended"]
    assert runtime._next_due() is None


@pytest.mark.asyncio
async def test_snooze_25_hours_late_is_cleared_without_a_message() -> None:
    """A backend back after more than 24 hours removes the snooze silently."""
    runtime, delivery, _ = _runtime(UNTIL + timedelta(hours=25))
    runtime.track(_snooze())

    await runtime._wake_due()

    assert delivery.calls == ["current_until", "clear"]
    assert delivery.sent == []
    assert runtime._next_due() is None


@pytest.mark.asyncio
async def test_snooze_25_hours_late_but_already_gone_is_dropped_without_a_clear() -> None:
    """A stale snooze that room state no longer holds is forgotten without touching room state."""
    runtime, delivery, _ = _runtime(UNTIL + timedelta(hours=25))
    delivery.until[ROOM, ROOT] = None
    runtime.track(_snooze())

    await runtime._wake_due()

    assert delivery.calls == ["current_until"]
    assert runtime._next_due() is None


@pytest.mark.asyncio
async def test_snooze_25_hours_late_but_moved_is_reindexed() -> None:
    """A stale index entry whose snooze moved in room state waits for the new time instead of clearing it."""
    later = UNTIL + timedelta(hours=26)
    runtime, delivery, _ = _runtime(UNTIL + timedelta(hours=25))
    delivery.until[ROOM, ROOT] = later
    runtime.track(_snooze())

    await runtime._wake_due()

    assert delivery.calls == ["current_until"]
    assert runtime._next_due() == later


@pytest.mark.asyncio
async def test_stale_snooze_that_no_bot_may_clear_is_dropped_without_a_message() -> None:
    """A stale snooze no bot may remove is dropped, and no stale-cleared line claims it was removed."""
    runtime, delivery, _ = _runtime(UNTIL + timedelta(hours=25))
    delivery.clear_results = ["not_permitted"]
    runtime.track(_snooze())

    with patch("mindroom.thread_snooze.logger") as logger:
        await runtime._wake_due()

    assert delivery.calls == ["current_until", "clear"]
    assert delivery.sent == []
    assert runtime._next_due() is None
    assert "thread_snooze_stale_cleared" not in [call.args[0] for call in logger.info.call_args_list]
    logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_no_bot_may_write_drops_without_a_message() -> None:
    """Without a bot that may remove the tag there is no message, now or later."""
    runtime, delivery, _ = _runtime(UNTIL)
    delivery.clear_results = ["not_permitted"]
    runtime.track(_snooze())

    await runtime._wake_due()

    assert delivery.calls == ["current_until", "is_resolved", "clear"]
    assert runtime._next_due() is None


@pytest.mark.asyncio
async def test_failed_read_retries_after_30_seconds() -> None:
    """A failed Matrix call moves that snooze's next try 30 seconds ahead."""
    runtime, delivery, clock = _runtime(UNTIL)
    delivery.read_errors = 1
    runtime.track(_snooze())

    await runtime._wake_due()

    assert delivery.calls == ["current_until"]
    assert runtime._next_due() == clock() + SNOOZE_RETRY_DELAY

    await runtime._wake_due()
    assert delivery.calls == ["current_until"]

    clock.now += SNOOZE_RETRY_DELAY
    await runtime._wake_due()

    assert delivery.calls == ["current_until", "current_until", "is_resolved", "clear", "send_ended"]
    assert runtime._next_due() is None


@pytest.mark.asyncio
async def test_failed_clear_retries_the_whole_wake() -> None:
    """A clear that fails on transport keeps the snooze for a full retry."""
    runtime, delivery, clock = _runtime(UNTIL)
    delivery.clear_results = [SnoozeUnavailableError("clear failed")]
    runtime.track(_snooze())

    await runtime._wake_due()

    assert delivery.calls == ["current_until", "is_resolved", "clear"]
    assert runtime._next_due() == clock() + SNOOZE_RETRY_DELAY


@pytest.mark.asyncio
async def test_failed_send_retries_the_send_only() -> None:
    """A message that fails after the clear is retried without clearing again."""
    runtime, delivery, clock = _runtime(UNTIL)
    delivery.send_results = [False]
    runtime.track(_snooze())

    await runtime._wake_due()

    assert delivery.calls == ["current_until", "is_resolved", "clear", "send_ended"]
    assert runtime._next_due() == clock() + SNOOZE_RETRY_DELAY

    # The sync echo of our own removal forgets the snooze but must not lose the pending message.
    runtime.forget(ROOM, ROOT)
    clock.now += SNOOZE_RETRY_DELAY
    await runtime._wake_due()

    assert delivery.calls == ["current_until", "is_resolved", "clear", "send_ended", "is_resolved", "send_ended"]
    assert delivery.sent == [_snooze()]
    assert runtime._next_due() is None


@pytest.mark.asyncio
async def test_pending_send_is_dropped_when_the_thread_is_resolved_meanwhile() -> None:
    """A wake message still waiting to be sent is given up once the thread is resolved."""
    runtime, delivery, clock = _runtime(UNTIL)
    delivery.send_results = [False]
    runtime.track(_snooze())
    await runtime._wake_due()

    delivery.resolved = True
    clock.now += SNOOZE_RETRY_DELAY
    await runtime._wake_due()

    assert delivery.calls == ["current_until", "is_resolved", "clear", "send_ended", "is_resolved"]
    assert delivery.sent == []
    assert runtime._next_due() is None


@pytest.mark.asyncio
async def test_pending_send_waits_when_the_resolved_check_fails() -> None:
    """A failed resolved check before a retried send tries again later instead of sending blind."""
    runtime, delivery, clock = _runtime(UNTIL)
    delivery.send_results = [False]
    runtime.track(_snooze())
    await runtime._wake_due()

    async def unreadable(room_id: str, thread_root_id: str) -> bool:  # noqa: ARG001
        delivery.calls.append("is_resolved")
        msg = "read failed"
        raise SnoozeUnavailableError(msg)

    delivery.is_resolved = unreadable  # type: ignore[method-assign]
    clock.now += SNOOZE_RETRY_DELAY
    await runtime._wake_due()

    assert delivery.calls == ["current_until", "is_resolved", "clear", "send_ended", "is_resolved"]
    assert delivery.sent == []
    assert runtime._next_due() == clock() + SNOOZE_RETRY_DELAY


@pytest.mark.asyncio
async def test_a_new_snooze_drops_the_pending_wake_message() -> None:
    """Snoozing the thread again cancels the earlier snooze's wake message that is still waiting."""
    runtime, delivery, clock = _runtime(UNTIL)
    delivery.send_results = [False]
    runtime.track(_snooze())
    await runtime._wake_due()

    later = UNTIL + timedelta(hours=2)
    delivery.until[ROOM, ROOT] = later
    runtime.track(_snooze(later))
    clock.now += SNOOZE_RETRY_DELAY
    await runtime._wake_due()

    assert delivery.calls == ["current_until", "is_resolved", "clear", "send_ended"]
    assert delivery.sent == []
    assert runtime._pending_sends == {}
    assert runtime._next_due() == later


@pytest.mark.asyncio
async def test_unreadable_snooze_past_the_stale_cutoff_is_dropped() -> None:
    """A snooze whose room state stays unreadable is given up once it is more than 24 hours late."""
    runtime, delivery, clock = _runtime(UNTIL + timedelta(hours=23))
    delivery.read_errors = 100
    runtime.track(_snooze())

    await runtime._wake_due()
    assert runtime._next_due() == clock() + SNOOZE_RETRY_DELAY

    clock.now = UNTIL + timedelta(hours=24, seconds=1)
    await runtime._wake_due()

    assert delivery.calls == ["current_until", "current_until"]
    assert runtime._next_due() is None


@pytest.mark.asyncio
async def test_pending_send_older_than_24_hours_is_dropped() -> None:
    """A wake message that could not be sent for over 24 hours is given up."""
    runtime, delivery, clock = _runtime(UNTIL)
    delivery.send_results = [False]
    runtime.track(_snooze())
    await runtime._wake_due()

    clock.now = UNTIL + timedelta(hours=25)
    await runtime._wake_due()

    assert delivery.calls == ["current_until", "is_resolved", "clear", "send_ended"]
    assert runtime._next_due() is None


@pytest.mark.asyncio
async def test_snooze_retracked_during_its_wake_is_kept() -> None:
    """A snooze moved while its wake reads room state is neither cleared nor announced by that wake."""
    runtime, delivery, _ = _runtime(UNTIL)
    later = UNTIL + timedelta(hours=1)
    original_current_until = delivery.current_until

    async def current_until_moved_meanwhile(room_id: str, thread_root_id: str) -> datetime | None:
        # The read answers with the old time while the live event of the move arrives.
        runtime.track(_snooze(later))
        return await original_current_until(room_id, thread_root_id)

    delivery.current_until = current_until_moved_meanwhile  # type: ignore[method-assign]
    runtime.track(_snooze())

    await runtime._wake_due()

    assert delivery.calls == ["current_until"]
    assert runtime._next_due() == later


@pytest.mark.asyncio
async def test_tracking_an_earlier_snooze_wakes_the_sleeping_loop() -> None:
    """Run sleeps until the earliest snooze and wakes when an earlier one is tracked."""
    runtime, delivery, clock = _runtime(UNTIL)
    runtime.track(_snooze(UNTIL + timedelta(days=1), root="$later"))
    task = asyncio.create_task(runtime.run())
    try:
        await asyncio.sleep(0)
        assert delivery.calls == []

        runtime.track(_snooze(clock()))
        await asyncio.wait_for(delivery.sent_event.wait(), timeout=1)

        assert [snooze.thread_root_id for snooze in delivery.sent] == [ROOT]
        assert runtime._next_due() == UNTIL + timedelta(days=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
