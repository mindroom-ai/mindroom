"""Lifecycle-lock table eviction must never drop live response state, including an approval's hold.

``ResponseLifecycleCoordinator`` bounds its per-target lock table at 100 entries.
Eviction may only reclaim fully idle targets: an acquired lock serializes an
in-flight response, and a live queued-message signal carries queued human
ingress (or an active turn about to acquire the lock) whose silent loss would
drop user input. A pending approval holds its conversation as an active turn.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from mindroom.event_journal import EventJournalStore, approval_signals
from mindroom.event_journal.approval_signals import PausedReply
from mindroom.message_target import MessageTarget
from mindroom.response_lifecycle import ResponseLifecycleCoordinator

if TYPE_CHECKING:
    from pathlib import Path

_LOCK_TABLE_CAP = 100
_PRINCIPAL = "agent@alice"


def _target(index: int) -> MessageTarget:
    return MessageTarget.resolve(f"!room{index}:localhost", None, None, room_mode=True)


def _fill_lock_table(coordinator: ResponseLifecycleCoordinator) -> list[MessageTarget]:
    targets = [_target(index) for index in range(_LOCK_TABLE_CAP)]
    for target in targets:
        coordinator._response_lifecycle_lock(target)
    return targets


@pytest.mark.asyncio
async def test_eviction_never_evicts_a_target_holding_an_active_lock() -> None:
    """An acquired lifecycle lock must survive eviction or serialization breaks."""
    coordinator = ResponseLifecycleCoordinator()
    targets = _fill_lock_table(coordinator)
    protected_lock = coordinator._response_lifecycle_lock(targets[0])
    await protected_lock.acquire()
    try:
        coordinator._response_lifecycle_lock(_target(_LOCK_TABLE_CAP))

        assert len(coordinator._response_lifecycle_locks) == _LOCK_TABLE_CAP
        assert coordinator._response_lifecycle_lock(targets[0]) is protected_lock
        assert targets[1].lifecycle_key not in coordinator._response_lifecycle_locks
    finally:
        protected_lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("liveness", ["pending_human_message", "active_response_turn"])
async def test_eviction_never_evicts_a_target_with_a_live_queued_signal(liveness: str) -> None:
    """A live queued-message signal must survive eviction or queued user input is lost."""
    coordinator = ResponseLifecycleCoordinator()
    targets = _fill_lock_table(coordinator)
    protected_signal = coordinator._get_or_create_queued_signal(targets[0])
    if liveness == "pending_human_message":
        protected_signal.add_waiting_human_message("$queued")
    else:
        protected_signal.begin_response_turn()

    coordinator._response_lifecycle_lock(_target(_LOCK_TABLE_CAP))

    protected_key = targets[0].lifecycle_key
    assert len(coordinator._response_lifecycle_locks) == _LOCK_TABLE_CAP
    assert protected_key in coordinator._response_lifecycle_locks
    assert coordinator._thread_queued_signals[protected_key] is protected_signal
    assert targets[1].lifecycle_key not in coordinator._response_lifecycle_locks


@pytest.mark.asyncio
async def test_eviction_drops_fully_idle_targets_to_keep_the_table_bounded() -> None:
    """A fully idle target (unlocked, no signal state) is the intended eviction victim."""
    coordinator = ResponseLifecycleCoordinator()
    targets = _fill_lock_table(coordinator)
    idle_lock = coordinator._response_lifecycle_lock(targets[0])
    assert len(coordinator._response_lifecycle_locks) == _LOCK_TABLE_CAP

    new_target = _target(_LOCK_TABLE_CAP)
    new_lock = coordinator._response_lifecycle_lock(new_target)

    assert len(coordinator._response_lifecycle_locks) == _LOCK_TABLE_CAP
    assert coordinator._response_lifecycle_lock(new_target) is new_lock
    assert targets[0].lifecycle_key not in coordinator._response_lifecycle_locks
    assert coordinator._response_lifecycle_lock(targets[0]) is not idle_lock


@pytest.mark.asyncio
async def test_an_approval_hold_keeps_its_conversation_busy_without_taking_its_lock(tmp_path: Path) -> None:
    """A held conversation reads as busy and survives eviction, yet the approval's own work can still take its lock."""
    store = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    coordinator = ResponseLifecycleCoordinator()
    targets = _fill_lock_table(coordinator)
    held = targets[0]
    paused = PausedReply(_PRINCIPAL, "approval-1", held.room_id, held.resolved_thread_id)
    try:
        coordinator.follow_approval_holds(store.approval_signals, _PRINCIPAL)
        await store.backend.write(lambda transaction: approval_signals.paused(transaction, paused))
        # An advance reports the paused reply again, which changes nothing: one end frees the conversation.
        await store.backend.write(lambda transaction: approval_signals.paused(transaction, paused))
        assert coordinator.approval_holds(held.room_id, held.resolved_thread_id) == ("approval-1",)
        assert coordinator.has_active_response_for_target(held)
        assert not coordinator.approval_holds(targets[1].room_id, targets[1].resolved_thread_id)

        coordinator._response_lifecycle_lock(_target(_LOCK_TABLE_CAP))
        assert held.lifecycle_key in coordinator._thread_queued_signals
        lock = coordinator._response_lifecycle_lock(held)
        await asyncio.wait_for(lock.acquire(), timeout=1)
        lock.release()

        idle = asyncio.create_task(coordinator.wait_for_thread_idle(held.room_id, held.resolved_thread_id))
        await asyncio.sleep(0)
        assert not idle.done()
        await store.backend.write(lambda transaction: approval_signals.ended(transaction, _PRINCIPAL, "approval-1"))
        await asyncio.wait_for(idle, timeout=1)
        assert not coordinator.has_active_response_for_target(held)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_a_coordinator_holds_only_the_paused_replies_of_the_principal_it_follows(tmp_path: Path) -> None:
    """Holds follow one principal's paused replies from the ones paused when it starts following, until it stops."""
    store = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    coordinator = ResponseLifecycleCoordinator()
    before, other, after = _target(1), _target(2), _target(3)
    try:
        await store.backend.write(
            lambda transaction: approval_signals.paused(
                transaction,
                PausedReply(_PRINCIPAL, "approval-1", before.room_id, before.resolved_thread_id),
            ),
        )
        coordinator.follow_approval_holds(store.approval_signals, _PRINCIPAL)
        # The reply paused before it followed is held as soon as it follows.
        assert coordinator.approval_holds(before.room_id, before.resolved_thread_id) == ("approval-1",)
        await store.backend.write(
            lambda transaction: approval_signals.paused(
                transaction,
                PausedReply("agent@bob", "approval-2", other.room_id, other.resolved_thread_id),
            ),
        )
        assert not coordinator.has_active_response_for_target(other)

        coordinator.stop_following_approval_holds()
        await store.backend.write(
            lambda transaction: approval_signals.paused(
                transaction,
                PausedReply(_PRINCIPAL, "approval-3", after.room_id, after.resolved_thread_id),
            ),
        )
        assert not coordinator.has_active_response_for_target(after)
    finally:
        await store.close()
