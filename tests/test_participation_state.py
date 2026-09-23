"""Participation state settles once, independently of any model framework."""

from __future__ import annotations

import asyncio

import pytest

from mindroom.participation import ParticipationDecision, ParticipationGate


@pytest.mark.asyncio
async def test_simultaneous_primary_calls_share_one_decision() -> None:
    """Concurrent entries must not issue competing participation decisions."""
    entered = asyncio.Event()
    release = asyncio.Event()
    decisions = 0

    async def decide() -> ParticipationDecision:
        nonlocal decisions
        decisions += 1
        entered.set()
        await release.wait()
        return ParticipationDecision(action="respond", reason="Open question.")

    gate = ParticipationGate()
    first = asyncio.create_task(gate.check(decide))
    await entered.wait()
    second = asyncio.create_task(gate.check(decide))
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(first, second) == [True, True]
    assert decisions == 1


@pytest.mark.asyncio
async def test_late_approval_cannot_reopen_failed_turn() -> None:
    """An in-flight decision must not reopen activity after quiet failure."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def decide() -> ParticipationDecision:
        entered.set()
        await release.wait()
        return ParticipationDecision(action="respond", reason="Open question.")

    gate = ParticipationGate()
    decision = asyncio.create_task(gate.check(decide))
    await entered.wait()
    gate.decline("preparation_failed")
    assert gate.decided.is_set()
    release.set()

    assert not await decision
    assert gate.is_silent
    assert not gate.is_declined
    assert gate.decision is not None
    assert gate.decision.reason == "preparation_failed"


@pytest.mark.asyncio
async def test_settled_approval_cannot_be_overwritten() -> None:
    """Late errors must not revoke a turn that already owns visible output."""

    async def decide() -> ParticipationDecision:
        return ParticipationDecision(action="respond", reason="Open question.")

    gate = ParticipationGate()
    assert await gate.check(decide)
    gate.decline("late_failure")
    assert gate.approved

    async def unexpected_decider() -> ParticipationDecision:
        pytest.fail("A settled turn must not ask another decider.")

    assert await gate.check(unexpected_decider)


@pytest.mark.asyncio
async def test_deliberate_decline_is_distinct_from_failure() -> None:
    """A model's free-text reason must not determine whether its decline is intentional."""
    gate = ParticipationGate()

    async def decide() -> ParticipationDecision:
        return ParticipationDecision(action="stay_silent", reason="decision_failed")

    assert not await gate.check(decide)
    assert gate.is_silent
    assert gate.is_declined
