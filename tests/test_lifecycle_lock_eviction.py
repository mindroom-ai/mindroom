"""Lifecycle-lock table eviction must never drop live response state, including an approval's hold.

``ResponseLifecycleCoordinator`` bounds its per-target lock table at 100 entries.
Eviction may only reclaim fully idle targets: an acquired lock serializes an
in-flight response, and a live queued-message signal carries queued human
ingress (or an active turn about to acquire the lock) whose silent loss would
drop user input. A pending approval holds its conversation as an active turn.
"""

from __future__ import annotations

import asyncio

import pytest

from mindroom.message_target import MessageTarget
from mindroom.response_lifecycle import ResponseLifecycleCoordinator

_LOCK_TABLE_CAP = 100


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
async def test_an_approval_hold_keeps_its_conversation_busy_without_taking_its_lock() -> None:
    """A held conversation reads as busy and survives eviction, yet the approval's own work can still take its lock."""
    coordinator = ResponseLifecycleCoordinator()
    targets = _fill_lock_table(coordinator)
    held = targets[0]
    coordinator.hold_for_approval("approval-1", held)
    # Holding again changes nothing: one release frees the conversation.
    coordinator.hold_for_approval("approval-1", held)
    assert coordinator.approval_holds(held.room_id, held.resolved_thread_id)
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
    coordinator.release_approval_hold("approval-1")
    await asyncio.wait_for(idle, timeout=1)
    assert not coordinator.has_active_response_for_target(held)
    # Releasing an approval that holds nothing changes nothing.
    coordinator.release_approval_hold("approval-1")


def test_leaving_a_room_releases_its_approval_holds() -> None:
    """A departure ends the room's approvals without their settlement, so their holds go with it."""
    coordinator = ResponseLifecycleCoordinator()
    left, kept = _target(1), _target(2)
    coordinator.hold_for_approval("approval-1", left)
    coordinator.hold_for_approval("approval-2", kept)
    coordinator.release_approval_holds_in_room(left.room_id)
    assert not coordinator.approval_holds(left.room_id, left.resolved_thread_id)
    assert coordinator.approval_holds(kept.room_id, kept.resolved_thread_id)
