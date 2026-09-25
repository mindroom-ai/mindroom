"""Wakeable background loops shared by memory auto-flush and skill learning."""

from __future__ import annotations

import asyncio
import threading

import pytest

from mindroom.background_loop import WakeSignal, run_until_stopped


@pytest.mark.asyncio
async def test_notify_from_a_worker_thread_starts_the_next_cycle_at_once() -> None:
    """Completed responses queue work from threads, and the loop must wake without waiting its interval."""
    signal = WakeSignal()
    stop, wake = asyncio.Event(), asyncio.Event()
    cycles = 0

    async def cycle() -> float:
        nonlocal cycles
        cycles += 1
        if cycles == 1:
            threading.Thread(target=signal.notify).start()
        else:
            stop.set()
            wake.set()
        return 3600

    await asyncio.wait_for(run_until_stopped(stop=stop, wake=wake, signal=signal, cycle=cycle), timeout=5)
    assert cycles == 2


@pytest.mark.asyncio
async def test_stop_requested_during_a_cycle_is_not_lost() -> None:
    """A stop that arrives mid-cycle ends the loop immediately instead of after the next interval."""
    signal = WakeSignal()
    stop, wake = asyncio.Event(), asyncio.Event()

    async def cycle() -> float:
        stop.set()
        wake.set()
        return 3600

    await asyncio.wait_for(run_until_stopped(stop=stop, wake=wake, signal=signal, cycle=cycle), timeout=5)
