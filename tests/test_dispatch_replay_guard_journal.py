"""The degraded-history replay guard, which had no tests at all.

When a turn's thread history cannot be read, the ordinary "is there a newer
message from this requester?" check has nothing to read. This guard answers the
same question from the journal's pending set instead, and it is deliberately
one-directional: it suppresses an older turn only on positive proof, so every
way of not knowing lets the older turn run.

The case that matters most here is the one that justifies the branch's only
remaining pair of durable state. "Has this turn finished?" is answered by two
records that live in different substrates -- the journal's pending set and the
handled-turn ledger -- and they settle at different moments. A turn recorded in
the ledger and then interrupted before its journal source settles comes back as
a pending row describing work that is already done. Trusting pending alone
would let that finished turn suppress an older one forever, so the guard
consults both.

Deleting that second consultation used to pass the entire suite. These tests
exist so it cannot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
import structlog

from mindroom.dispatch_handoff import PreparedTextEvent
from mindroom.dispatch_replay_guard import has_newer_unresponded_journal_thread_event
from mindroom.event_journal import EventKind, JournalEvent

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

pytestmark = pytest.mark.asyncio

ROOM = "!room:example.org"
THREAD = "$thread-root"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"
OLDER = "$older"
NEWER = "$newer"


def _older_turn(*, timestamp: int | None = 1_000) -> PreparedTextEvent:
    """Return the turn whose right to run is under question."""
    return PreparedTextEvent(
        sender=ALICE,
        event_id=OLDER,
        body="the older question",
        source={"content": {"msgtype": "m.text", "body": "the older question"}},
        server_timestamp=timestamp,
    )


def _pending(
    *,
    event_id: str = NEWER,
    sender: str = ALICE,
    body: str = "the newer question",
    origin_server_ts: int = 2_000,
) -> JournalEvent:
    """Return one unsettled journal event newer than the turn under question."""
    content = {"msgtype": "m.text", "body": body}
    return JournalEvent(
        event_id=event_id,
        room_id=ROOM,
        thread_id=THREAD,
        kind=EventKind.MESSAGE,
        sender=sender,
        origin_server_ts=origin_server_ts,
        source={"sender": sender, "content": content},
        receipt_order=1,
    )


@dataclass
class _PendingTurns:
    """The journal's pending set, or a journal that cannot be read."""

    events: tuple[JournalEvent, ...] = ()
    error: Exception | None = None
    calls: int = 0

    async def pending_thread_events_after(
        self,
        *,
        room_id: str,
        thread_id: str,
        after_origin_server_ts: int,
        excluding_event_id: str,
        limit: int = 64,
    ) -> tuple[JournalEvent, ...]:
        """Return unsettled events newer than a timestamp, honouring the filters."""
        del limit
        self.calls += 1
        if self.error is not None:
            raise self.error
        return tuple(
            event
            for event in self.events
            if event.room_id == room_id
            and event.thread_id == thread_id
            and event.origin_server_ts > after_origin_server_ts
            and event.event_id != excluding_event_id
        )


async def _guard(
    pending_turns: _PendingTurns,
    *,
    event: PreparedTextEvent | None = None,
    handled_event_ids: Collection[str] = (),
    may_be_superseded_by_newer_requester_turn: bool = True,
    thread_id: str | None = THREAD,
    requester_for: Mapping[str, str] | None = None,
    voice_echo_event_senders: Collection[str] = (),
) -> bool:
    """Run the guard with the smallest collaborators that answer honestly."""
    resolved_event = event if event is not None else _older_turn()

    def requester_user_id_for_event(sender: str, _source: object) -> str:
        return requester_for.get(sender, sender) if requester_for else sender

    def is_visible_router_voice_echo(sender: str, _content: object) -> bool:
        return sender in voice_echo_event_senders

    return await has_newer_unresponded_journal_thread_event(
        room_id=ROOM,
        event=resolved_event,
        requester_user_id=ALICE,
        thread_id=thread_id,
        may_be_superseded_by_newer_requester_turn=may_be_superseded_by_newer_requester_turn,
        pending_turns=pending_turns,  # type: ignore[arg-type]
        requester_user_id_for_event=requester_user_id_for_event,
        is_visible_router_voice_echo=is_visible_router_voice_echo,
        sender_is_trusted_for_ingress_metadata=lambda _sender: True,
        is_handled=lambda event_id: event_id in handled_event_ids,
        logger=structlog.get_logger("test"),
        # ``Any`` only to satisfy the structlog stub's bound-logger type.
    )  # type: ignore[no-any-return]


class TestTheTwoRecordsAreBothConsulted:
    """Why "is this turn finished?" needs the ledger as well as the journal."""

    async def test_a_pending_event_already_in_the_ledger_does_not_suppress(self) -> None:
        """The interruption window between the two records, made concrete.

        The newer turn ran and was recorded in the handled-turn ledger, then
        the process died before its journal source settled. It comes back as a
        pending row that describes finished work. If the guard trusted the
        journal alone it would read that row as "a newer question is still
        waiting" and refuse to run the older turn -- not once, but on every
        replay, because nothing will ever settle it into an answer.
        """
        pending_turns = _PendingTurns(events=(_pending(),))

        suppressed = await _guard(pending_turns, handled_event_ids={NEWER})

        assert suppressed is False

    async def test_a_pending_event_absent_from_the_ledger_does_suppress(self) -> None:
        """The mirror, so the test above cannot pass by never suppressing.

        Same pending row, same everything, except that the ledger has never
        heard of it. Now it really is an unanswered newer question, and the
        older turn is the one that should stand aside.
        """
        pending_turns = _PendingTurns(events=(_pending(),))

        suppressed = await _guard(pending_turns, handled_event_ids=set())

        assert suppressed is True


class TestOnlyTheRequestersOwnNewerTurnCounts:
    """Whose turn an event is cannot be decided from a column."""

    async def test_a_newer_turn_from_someone_else_does_not_suppress(self) -> None:
        """Another participant talking is not this requester superseding themselves."""
        pending_turns = _PendingTurns(events=(_pending(sender=BOB),))

        assert await _guard(pending_turns) is False

    async def test_a_relayed_turn_is_attributed_to_the_person_behind_it(self) -> None:
        """A relay's own Matrix ID is not the requester; the metadata says who is.

        Without this the guard would compare against the bridge or relay
        account and never find the requester's own newer message.
        """
        pending_turns = _PendingTurns(events=(_pending(sender=BOB),))

        assert await _guard(pending_turns, requester_for={BOB: ALICE}) is True

    async def test_a_router_voice_echo_is_not_a_turn(self) -> None:
        """The echo is a display artifact of the turn, not a second question."""
        pending_turns = _PendingTurns(events=(_pending(),))

        assert await _guard(pending_turns, voice_echo_event_senders={ALICE}) is False

    async def test_a_newer_command_is_not_a_turn_that_owes_an_answer(self) -> None:
        """A command leaves dispatch long before a response would be owed."""
        pending_turns = _PendingTurns(events=(_pending(body="!help"),))

        assert await _guard(pending_turns) is False


class TestEveryWayOfNotKnowingLetsTheOlderTurnRun:
    """The guard acts on proof, so its failure direction is fixed."""

    async def test_an_unreadable_journal_does_not_suppress(self) -> None:
        """A guard that suppressed on error would drop turns whenever it broke."""
        pending_turns = _PendingTurns(error=RuntimeError("journal unavailable"))

        assert await _guard(pending_turns) is False

    async def test_an_empty_pending_set_does_not_suppress(self) -> None:
        """Nothing newer is pending, so there is nothing to stand aside for."""
        assert await _guard(_PendingTurns()) is False

    async def test_a_turn_outside_a_thread_is_never_suppressed(self) -> None:
        """The query is thread-scoped, so a roomwide turn has nothing to ask."""
        pending_turns = _PendingTurns(events=(_pending(),))

        assert await _guard(pending_turns, thread_id=None) is False
        assert pending_turns.calls == 0, "the journal was queried without a thread to query"

    async def test_a_turn_with_no_timestamp_is_never_suppressed(self) -> None:
        """Being newer is meaningless without one, and guessing would be worse."""
        pending_turns = _PendingTurns(events=(_pending(),))

        assert await _guard(pending_turns, event=_older_turn(timestamp=None)) is False
        assert pending_turns.calls == 0

    async def test_a_turn_that_cannot_be_superseded_is_never_suppressed(self) -> None:
        """The caller has already decided this turn stands on its own."""
        pending_turns = _PendingTurns(events=(_pending(),))

        assert await _guard(pending_turns, may_be_superseded_by_newer_requester_turn=False) is False
        assert pending_turns.calls == 0


class TestTheQueryIsAskedForTheRightWindow:
    """The filters the guard delegates to SQL still have to be the right ones."""

    async def test_the_turn_under_question_is_excluded_from_its_own_answer(self) -> None:
        """Its own pending row must not read as a newer turn than itself."""
        own_row: Any = _pending(event_id=OLDER, origin_server_ts=2_000)
        pending_turns = _PendingTurns(events=(own_row,))

        assert await _guard(pending_turns) is False

    async def test_an_older_pending_event_is_not_newer(self) -> None:
        """Ordering is by origin timestamp, and the query is asked for one side."""
        pending_turns = _PendingTurns(events=(_pending(origin_server_ts=500),))

        assert await _guard(pending_turns) is False
