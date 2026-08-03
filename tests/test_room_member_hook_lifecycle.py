"""Tests for the room-member hook bootstrap and catch-up phase owner."""

from __future__ import annotations

import nio
import pytest

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
    lifecycle.prepare_startup(transport="classic", resuming_position=False)

    assert lifecycle._baseline_record_pending
    assert lifecycle.full_state_required
    assert not lifecycle.admission_enabled(nio.TimelineEventProvenance.HISTORY)
    assert lifecycle.admission_enabled(nio.TimelineEventProvenance.LIVE)
    assert lifecycle.baseline_capture_pending

    lifecycle.capture_baseline(())

    uncertain_baseline = lifecycle.plan_response(
        _decision(SyncTrustState.UNCERTAIN),
        first_sync_response=False,
    )
    certifiable_baseline = lifecycle.plan_response(
        _decision(SyncTrustState.CERTIFIED),
        first_sync_response=False,
    )

    assert uncertain_baseline.baseline_record_events is None
    assert certifiable_baseline.baseline_record_events == ()

    lifecycle.rewound(has_retry_token=True)
    assert not lifecycle.baseline_capture_pending
    assert (
        lifecycle.plan_response(
            _decision(SyncTrustState.CERTIFIED),
            first_sync_response=False,
        ).baseline_record_events
        == ()
    )

    lifecycle.baseline_recorded()
    plan = lifecycle.plan_response(
        _decision(SyncTrustState.UNCERTAIN),
        first_sync_response=False,
    )

    assert not lifecycle._baseline_record_pending
    assert lifecycle.admission_enabled(nio.TimelineEventProvenance.LIVE)
    assert not plan.drain_captured_timeline

    certified = lifecycle.plan_response(
        _decision(SyncTrustState.CERTIFIED),
        first_sync_response=False,
    )
    assert certified.drain_captured_timeline

    lifecycle.certified()
    assert not lifecycle.full_state_required
    assert not lifecycle.admission_enabled(nio.TimelineEventProvenance.HISTORY)


def test_tokenless_rewind_discards_stale_baseline_and_rearms_capture() -> None:
    """A tokenless reset must replace rather than certify the rejected snapshot."""
    lifecycle = RoomMemberHookLifecycle(enabled=True)
    lifecycle.prepare_startup(transport="classic", resuming_position=True)
    lifecycle.capture_baseline(())

    lifecycle.rewound(has_retry_token=False)

    assert lifecycle._baseline_record_pending
    assert lifecycle.baseline_capture_pending
    with pytest.raises(RuntimeError, match="without a captured full-state baseline"):
        lifecycle.plan_response(
            _decision(SyncTrustState.CERTIFIED),
            first_sync_response=False,
        )


def test_safe_rewind_and_loop_replacement_require_full_state_catchup() -> None:
    """A safe rewind must capture replay joins and reacquire a replacement baseline."""
    lifecycle = RoomMemberHookLifecycle(enabled=True)
    lifecycle.prepare_startup(transport="classic", resuming_position=False)
    lifecycle.capture_baseline(())
    lifecycle.baseline_recorded()
    lifecycle.certified()

    lifecycle.rewound(has_retry_token=True)
    assert lifecycle.full_state_required
    assert not lifecycle._baseline_record_pending
    assert lifecycle.admission_enabled(nio.TimelineEventProvenance.LIVE)

    lifecycle.begin_sync_loop()
    assert lifecycle._baseline_record_pending
    assert lifecycle.baseline_capture_pending

    lifecycle.capture_baseline(())
    lifecycle.baseline_recorded()
    assert not lifecycle._baseline_record_pending


def test_unknown_position_and_stop_reset_phase_boundaries() -> None:
    """Rejected positions restart at a history baseline and stop disables admission."""
    lifecycle = RoomMemberHookLifecycle(enabled=True)
    lifecycle.prepare_startup(transport="classic", resuming_position=True)
    lifecycle.unknown_position()

    assert lifecycle._baseline_record_pending
    assert not lifecycle.admission_enabled(nio.TimelineEventProvenance.HISTORY)
    assert lifecycle.admission_enabled(nio.TimelineEventProvenance.LIVE)

    lifecycle.stop()
    assert not lifecycle.full_state_required
    assert not lifecycle.admission_enabled(nio.TimelineEventProvenance.LIVE)


def test_live_response_drains_admitted_timeline_before_dispatching_state() -> None:
    """Normal responses drain response-owned live work and dispatch state deltas."""
    lifecycle = RoomMemberHookLifecycle(enabled=True)
    lifecycle.prepare_startup(transport="classic", resuming_position=False)
    lifecycle.capture_baseline(())
    lifecycle.baseline_recorded()
    lifecycle.certified()

    plan = lifecycle.plan_response(
        _decision(SyncTrustState.CERTIFIED),
        first_sync_response=False,
    )

    assert plan.dispatch_state
    assert plan.drain_captured_timeline


def test_sliding_startup_admits_only_live_events_without_classic_baseline() -> None:
    """Sliding startup must never inherit Classic historical catch-up authority."""
    lifecycle = RoomMemberHookLifecycle(enabled=True)

    lifecycle.prepare_startup(transport="sliding", resuming_position=True)

    assert not lifecycle._baseline_record_pending
    assert not lifecycle.full_state_required
    assert not lifecycle.admission_enabled(nio.TimelineEventProvenance.HISTORY)
    assert lifecycle.admission_enabled(nio.TimelineEventProvenance.LIVE)
