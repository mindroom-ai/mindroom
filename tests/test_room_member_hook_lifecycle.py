"""Tests for the room-member hook bootstrap and catch-up phase owner."""

from __future__ import annotations

import nio

from mindroom.matrix.room_member_hook_lifecycle import (
    RoomMemberHookLifecycle,
)
from mindroom.matrix.sync_certification import SyncCertificationDecision, SyncTrustState


def _decision(
    state: SyncTrustState,
    *,
    reset_client_token: bool = False,
) -> SyncCertificationDecision:
    return SyncCertificationDecision(
        state=state,
        reset_client_token=reset_client_token,
    )


def test_tokenless_baseline_transitions_to_catchup_until_certified() -> None:
    """A consumed tokenless baseline must capture later incremental joins."""
    lifecycle = RoomMemberHookLifecycle(enabled=True)
    lifecycle.prepare_startup(resuming_position=False)

    assert lifecycle.baseline_record_pending
    assert lifecycle.full_state_required
    assert not lifecycle.admission_enabled(nio.TimelineEventProvenance.HISTORY)
    assert lifecycle.admission_enabled(nio.TimelineEventProvenance.LIVE)

    lifecycle.baseline_recorded()
    plan = lifecycle.plan_response(
        _decision(SyncTrustState.UNCERTAIN),
        first_sync_response=False,
    )

    assert not lifecycle.baseline_record_pending
    assert lifecycle.admission_enabled(nio.TimelineEventProvenance.LIVE)
    assert not lifecycle.callback_execution_enabled
    assert not plan.drain_captured_timeline

    certified = lifecycle.plan_response(
        _decision(SyncTrustState.CERTIFIED),
        first_sync_response=False,
    )
    assert certified.drain_captured_timeline

    lifecycle.certified()
    assert lifecycle.callback_execution_enabled
    assert not lifecycle.full_state_required
    assert not lifecycle.admission_enabled(nio.TimelineEventProvenance.HISTORY)


def test_safe_rewind_and_loop_replacement_require_full_state_catchup() -> None:
    """A safe rewind must capture replay joins and reacquire a replacement baseline."""
    lifecycle = RoomMemberHookLifecycle(enabled=True)
    lifecycle.prepare_startup(resuming_position=False)
    lifecycle.baseline_recorded()
    lifecycle.certified()

    lifecycle.rewound(has_retry_token=True)
    assert lifecycle.full_state_required
    assert not lifecycle.baseline_record_pending
    assert lifecycle.admission_enabled(nio.TimelineEventProvenance.LIVE)

    lifecycle.begin_sync_loop()
    assert lifecycle.baseline_record_pending

    lifecycle.baseline_recorded()
    assert not lifecycle.baseline_record_pending


def test_unknown_position_and_stop_reset_phase_boundaries() -> None:
    """Rejected positions restart at a history baseline and stop disables admission."""
    lifecycle = RoomMemberHookLifecycle(enabled=True)
    lifecycle.prepare_startup(resuming_position=True)
    lifecycle.unknown_position()

    assert lifecycle.baseline_record_pending
    assert not lifecycle.admission_enabled(nio.TimelineEventProvenance.HISTORY)
    assert lifecycle.admission_enabled(nio.TimelineEventProvenance.LIVE)

    lifecycle.stop()
    assert not lifecycle.full_state_required
    assert not lifecycle.callback_execution_enabled
    assert not lifecycle.admission_enabled(nio.TimelineEventProvenance.LIVE)


def test_live_response_dispatches_state_without_bootstrap_drain() -> None:
    """Normal live responses keep state-delta dispatch separate from catch-up draining."""
    lifecycle = RoomMemberHookLifecycle(enabled=True)
    lifecycle.prepare_startup(resuming_position=False)
    lifecycle.baseline_recorded()
    lifecycle.certified()

    plan = lifecycle.plan_response(
        _decision(SyncTrustState.CERTIFIED),
        first_sync_response=False,
    )

    assert plan.dispatch_state
    assert not plan.drain_captured_timeline
