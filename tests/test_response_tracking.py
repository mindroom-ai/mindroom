"""Identity bookkeeping must preserve admission and cleanup semantics."""

import asyncio
from contextlib import nullcontext, suppress
from dataclasses import replace

import pytest
from pydantic import ValidationError

from mindroom import response_activity
from mindroom.response_admission import ResponseAdmissionGate, admitted_response_decision
from mindroom.response_tracking import ResponseActivityTracker, ResponseIdentity


def test_equal_identities_own_separate_slots() -> None:
    """Finishing one equal identity must not remove another operation's slot."""
    tracker = ResponseActivityTracker()
    with tracker.track(responder="helper"):
        with tracker.track(responder="helper"):
            assert tracker.snapshot() == (ResponseIdentity("helper"), ResponseIdentity("helper"))
        assert tracker.snapshot() == (ResponseIdentity("helper"),)
    assert tracker.snapshot() == ()


@pytest.mark.parametrize("failure", [False, True])
def test_recovery_identity_cleanup_while_admission_closed(failure: bool) -> None:
    """Recovery identities must remain visible during replacement and never leak."""
    gate = ResponseAdmissionGate()
    gate.close()
    with (
        pytest.raises(RuntimeError) if failure else nullcontext(),
        gate.track_recovery(responder="helper", requester_id="@alice:example.org"),
    ):
        assert gate.active_operation_count == 1
        assert gate.in_flight_response_count == 0
        assert gate.response_tracker.snapshot()[0].requester_id == "@alice:example.org"
        assert not gate.admit()
        if failure:
            message = "delivery failed"
            raise RuntimeError(message)
    assert gate.response_tracker.snapshot() == ()
    assert gate.active_operation_count == 0


@pytest.mark.asyncio
async def test_admitted_identities_refine_and_nested_slots_cleanup() -> None:
    """Refining one live slot must preserve old snapshots and other live slots."""
    gate = ResponseAdmissionGate()

    async def wait_for_admission() -> bool:
        await gate.wait_until_open()
        return True

    async with admitted_response_decision(gate, wait_for_admission) as handle:
        assert gate.response_tracker.count == gate.in_flight_response_count == 1
        original = gate.response_tracker.snapshot()
        handle.identity = replace(handle.identity, responder="team/helpers", requester_id="@alice:example.org")
        async with admitted_response_decision(gate, wait_for_admission, responder="helper"):
            assert gate.response_tracker.count == gate.in_flight_response_count == 2
            assert original[0].responder is None
            assert gate.response_tracker.snapshot()[0].responder == "team/helpers"
        assert gate.response_tracker.count == gate.in_flight_response_count == 1
    assert gate.response_tracker.snapshot() == ()
    assert gate.in_flight_response_count == 0


@pytest.mark.asyncio
async def test_waiting_admission_has_no_identity_and_cancel_cleans_up() -> None:
    """Only admitted work appears, and cancellation synchronously removes its identity."""
    gate = ResponseAdmissionGate()
    assert gate.response_tracker.count == 0
    gate.close()
    entered = asyncio.Event()

    async def wait_for_admission() -> bool:
        entered.set()
        await gate.wait_until_open()
        return True

    async def operation() -> None:
        async with admitted_response_decision(gate, wait_for_admission, responder="helper"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(operation())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        assert gate.response_tracker.count == gate.active_operation_count == 0
        entered.clear()
        gate.reopen()
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        assert gate.response_tracker.count == gate.active_operation_count == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    assert gate.response_tracker.count == gate.active_operation_count == 0


@pytest.mark.parametrize(
    ("matrix_count", "rows", "valid"),
    [
        (0, [], True),
        (None, [], True),
        (1, [{"channel": "matrix", "responder": None, "requester_id": None, "operations": 1}], True),
        (0, [{"channel": "matrix", "responder": None, "requester_id": None, "operations": 1}], False),
        (None, [{"channel": "matrix", "responder": None, "requester_id": None, "operations": 1}], False),
        (1, [], False),
        (0, [{"channel": "openai", "responder": None, "requester_id": None, "operations": 1}], False),
        (0, [{"channel": "matrix", "responder": None, "requester_id": None, "operations": 0}], False),
        (1, [{"channel": "matrix", "responder": None, "requester_id": None, "operations": True}], False),
        (1, [{"channel": "matrix", "operations": 1}], False),
    ],
)
def test_detailed_activity_reconciles_counts(matrix_count: int | None, rows: list[dict], valid: bool) -> None:
    """Missing and contradictory detail rows must fail conservative wire validation."""
    model = response_activity.DetailedResponseActivity
    payload = {
        "runtime_phase": "ready",
        "admission_paused": False,
        "active_matrix_operations": matrix_count,
        "active_openai_requests": 0,
        "responses": rows,
    }
    if valid:
        assert model.model_validate(payload).responses is not None
    else:
        with pytest.raises(ValidationError):
            model.model_validate(payload)


def test_detailed_activity_requires_responses() -> None:
    """An aggregate-only payload must not masquerade as a detailed snapshot."""
    with pytest.raises(ValidationError):
        response_activity.DetailedResponseActivity.model_validate(
            {
                "runtime_phase": "ready",
                "admission_paused": False,
                "active_matrix_operations": 0,
                "active_openai_requests": 0,
            },
        )
