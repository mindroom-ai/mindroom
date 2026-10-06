"""A stop reaction on an event no reply owns changes nothing.

These are pins on ``UserStopReconciler`` with a real ``TurnStore`` behind it.
The gateway is represented by the reply-record side of a Stop, which finds no
reply for these events.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest

from mindroom.handled_turns import _reset_handled_turn_ledger_runtime, with_user_stop
from mindroom.turn_store import TurnStore, TurnStoreDeps
from mindroom.user_stop_reconciliation import UserStopReconciler, UserStopReconcilerDeps

if TYPE_CHECKING:
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.event_journal import EventJournalStore

pytestmark = pytest.mark.asyncio

_ROOM_ID = "!room:localhost"
_STOP_RECEIPT_ORDER = 7


@dataclass
class _NoReplyStop:
    """A Stop no reply record owns: not pending, owned by no reply, writing nothing."""

    pending: bool = False
    owned: bool = False
    turn_id: str | None = None

    def __call__(self, _transaction: object, _record: object) -> None:
        """Write nothing on the reply side."""


@dataclass
class _NoReplyGateway:
    """A gateway whose reply records own none of the events a Stop names."""

    finished: int = 0

    async def reply_stop(self, event_id: str, receipt_order: int, *, room_id: str, may_wait: bool) -> _NoReplyStop:
        """Return a Stop step for a response no durable reply record owns."""
        del event_id, receipt_order, room_id, may_wait
        return _NoReplyStop()

    async def finish_reply_stop(self, _stop: object) -> bool:
        """Count a Stop that reached the reply side."""
        self.finished += 1
        return False


async def _store(journal_store: EventJournalStore) -> TurnStore:
    _reset_handled_turn_ledger_runtime()
    store = TurnStore(
        TurnStoreDeps(
            agent_name="agent",
            turn_records=journal_store.turn_records("agent"),
            redacted_event_ids=journal_store.principal("agent@alice").redacted_event_ids,
            relations=journal_store.principal("agent@alice"),
            legacy_responses_file=None,
            state_writer=MagicMock(),
            resolver=MagicMock(),
            tool_runtime=MagicMock(),
        ),
    )
    await store.warm()
    return store


@pytest.mark.parametrize("stop_already_written", [False, True])
async def test_stop_on_a_voice_echo_without_a_response_target_changes_nothing(
    journal_store: EventJournalStore,
    stop_already_written: bool,
) -> None:
    """A stop naming a visible voice echo has no response to finalize, so it must not write or raise.

    The written case is the durable state an earlier release left behind before
    it raised, which replays after an upgrade.
    """
    store = await _store(journal_store)
    await store.record_visible_echo("$voice", "$echo")
    if stop_already_written:
        echo_turn = store.get_turn_record("$voice")
        assert echo_turn is not None
        await store.record_turn(with_user_stop(echo_turn, "$echo", 5))
    before = store.get_turn_record("$voice")
    gateway = _NoReplyGateway()
    reconciler = UserStopReconciler(
        UserStopReconcilerDeps(turn_store=store, delivery_gateway=cast("DeliveryGateway", gateway)),
    )

    finalized = await reconciler.finalize("$echo", _STOP_RECEIPT_ORDER, room_id=_ROOM_ID)

    assert finalized is False
    assert store.get_turn_record("$voice") == before
    assert gateway.finished == 0
