"""One stop reaction produces one terminal record and one visible cancellation.

These are pins on ``UserStopReconciler``, the owner of that convergence, with a
real ``TurnStore`` behind it. The runner and the gateway are represented by the
two things the reconciler actually needs from them -- serialization under the
target's lifecycle lock, and the one visible edit that commits the cancellation
note -- so that a duplicate visible cancellation is something the test can see
rather than something a broader harness would absorb.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest

from mindroom.handled_turns import TurnRecord, _reset_handled_turn_ledger_runtime
from mindroom.message_target import MessageTarget
from mindroom.turn_store import TurnStore, TurnStoreDeps
from mindroom.user_stop_reconciliation import UserStopReconciler, UserStopReconcilerDeps

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.response_runner import ResponseRunner

pytestmark = pytest.mark.asyncio

_ROOM_ID = "!room:localhost"
_SOURCE_EVENT_ID = "$source"
_RESPONSE_EVENT_ID = "$response"
_STOP_RECEIPT_ORDER = 7


@dataclass
class _SerializingRunner:
    """The part of ``ResponseRunner`` this reconciliation depends on.

    Only two behaviors matter here: the finalize callback runs under a lock
    held per target, and the live response is asked to cancel while waiting for
    it. Everything else the real runner does is unrelated to convergence.
    """

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cancel_requests: int = 0

    async def finalize_user_stop(
        self,
        message_id: str,
        target: MessageTarget,
        stop_receipt_order: int,
        should_cancel: Callable[[], bool],
        finalize: Callable[[], Awaitable[bool]],
    ) -> bool:
        """Cancel the live response, then finalize under the target's lock."""
        del message_id, target, stop_receipt_order
        if should_cancel():
            self.cancel_requests += 1
        async with self.lock:
            return await finalize()


@dataclass
class _CountingGateway:
    """A gateway that records every visible cancellation it is asked for."""

    finalized: list[str] = field(default_factory=list)

    async def finalize_user_stopped_response(self, target: MessageTarget, response_event_id: str) -> bool:
        """Commit the cancellation note for one response."""
        del target
        self.finalized.append(response_event_id)
        return True


def _reconciler(store: TurnStore, runner: _SerializingRunner, gateway: _CountingGateway) -> UserStopReconciler:
    return UserStopReconciler(
        UserStopReconcilerDeps(
            turn_store=store,
            response_runner=cast("ResponseRunner", runner),
            delivery_gateway=cast("DeliveryGateway", gateway),
        ),
    )


def _store(tmp_path: Path) -> TurnStore:
    _reset_handled_turn_ledger_runtime()
    return TurnStore(
        TurnStoreDeps(
            agent_name="agent",
            tracking_base_path=tmp_path,
            state_writer=MagicMock(),
            resolver=MagicMock(),
            tool_runtime=MagicMock(),
        ),
    )


def _record_answered_turn(store: TurnStore) -> None:
    """Record the turn a stop reaction can arrive for: answered, not terminal."""
    store.record_turn_durably(
        TurnRecord.create(
            (_SOURCE_EVENT_ID,),
            response_event_id=_RESPONSE_EVENT_ID,
            completed=False,
            conversation_target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
        ),
    )


async def test_one_stop_makes_the_turn_terminal_and_commits_one_cancellation(tmp_path: Path) -> None:
    """The two sides of a stop have to agree, and there is only one of each."""
    store = _store(tmp_path)
    _record_answered_turn(store)
    runner, gateway = _SerializingRunner(), _CountingGateway()

    finalized = await _reconciler(store, runner, gateway).finalize(
        _RESPONSE_EVENT_ID,
        _STOP_RECEIPT_ORDER,
        _noop,
    )

    assert finalized
    assert gateway.finalized == [_RESPONSE_EVENT_ID]
    stopped = store.get_turn_record(_SOURCE_EVENT_ID)
    assert stopped is not None
    assert stopped.completed
    assert stopped.user_stop_receipt_order == _STOP_RECEIPT_ORDER
    assert stopped.user_stop_settled_receipt_order == _STOP_RECEIPT_ORDER


async def test_the_same_stop_delivered_twice_cancels_once(tmp_path: Path) -> None:
    """A reaction can be redelivered, and the room must not say so twice."""
    store = _store(tmp_path)
    _record_answered_turn(store)
    runner, gateway = _SerializingRunner(), _CountingGateway()
    reconciler = _reconciler(store, runner, gateway)

    await reconciler.finalize(_RESPONSE_EVENT_ID, _STOP_RECEIPT_ORDER, _noop)
    await reconciler.finalize(_RESPONSE_EVENT_ID, _STOP_RECEIPT_ORDER, _noop)

    assert gateway.finalized == [_RESPONSE_EVENT_ID]


async def test_concurrent_stops_converge_on_one_visible_cancellation(tmp_path: Path) -> None:
    """Two reactions racing for the same response are still one outcome."""
    store = _store(tmp_path)
    _record_answered_turn(store)
    runner, gateway = _SerializingRunner(), _CountingGateway()
    reconciler = _reconciler(store, runner, gateway)

    results = await asyncio.gather(
        reconciler.finalize(_RESPONSE_EVENT_ID, _STOP_RECEIPT_ORDER, _noop),
        reconciler.finalize(_RESPONSE_EVENT_ID, _STOP_RECEIPT_ORDER + 1, _noop),
    )

    assert all(results)
    assert gateway.finalized == [_RESPONSE_EVENT_ID]
    stopped = store.get_turn_record(_SOURCE_EVENT_ID)
    assert stopped is not None
    assert stopped.user_stop_settled_receipt_order == max(_STOP_RECEIPT_ORDER, _STOP_RECEIPT_ORDER + 1)


async def _noop() -> None:
    """Stand in for the caller's post-finalization notification."""
