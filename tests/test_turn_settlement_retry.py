"""Turn-backed dispatch-obligation settlement retry ownership."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.dispatch_obligations import DispatchObligationStore
from mindroom.turn_settlement_retry import TurnSettlementRetry


@pytest.mark.asyncio
async def test_terminal_turn_settlement_retries_autonomously() -> None:
    """A failed terminal compaction retries without fabricating a callback key."""
    owner = object()
    store = MagicMock(spec=DispatchObligationStore)
    store.entity_name = "code"
    retry = TurnSettlementRetry(
        store=store,
        background_task_owner=owner,
        _retry_initial_delay_seconds=0,
        _retry_max_delay_seconds=0,
    )
    settled = threading.Event()
    attempts = 0

    def settle_pending(_source_event_ids: tuple[str, ...]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            message = "dispatch store unavailable"
            raise OSError(message)
        settled.set()

    store.settle_pending_from_turn_store.side_effect = settle_pending

    retry.bind_event_loop()
    await asyncio.to_thread(retry.retry, ("$message",))
    assert await asyncio.wait_for(asyncio.to_thread(settled.wait), timeout=1)
    await wait_for_background_tasks(timeout=1, owner=owner)

    assert store.settle_pending_from_turn_store.call_count == 2
    store.settle_pending_from_turn_store.assert_called_with(("$message",))


@pytest.mark.asyncio
async def test_terminal_turn_settlement_succeeds_in_persistence_worker_without_retry_task() -> None:
    """The normal post-persist callback finishes before returning from its worker."""
    owner = object()
    store = MagicMock(spec=DispatchObligationStore)
    retry = TurnSettlementRetry(store=store, background_task_owner=owner)

    await asyncio.to_thread(retry.retry, ("$message",))

    store.settle_pending_from_turn_store.assert_called_once_with(("$message",))
    assert retry._task is None
    assert await wait_for_background_tasks(timeout=0, owner=owner) is True
