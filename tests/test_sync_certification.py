"""Tests for Matrix sync-token certification."""

from __future__ import annotations

import pytest

from mindroom.matrix.sync_certification import (
    SyncRecoveryOutcome,
    SyncTrustState,
    certify_sync_response,
    handle_unknown_pos,
    sync_recovery_diagnostics,
)
from mindroom.matrix.sync_token_values import SyncCheckpoint, normalize_sync_token


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("s_token", "s_token"),
        ("  s_token\n", "s_token"),
        (" \t\n", None),
        (None, None),
        (123, None),
    ],
)
def test_normalize_sync_token_accepts_only_non_empty_strings(value: object, expected: str | None) -> None:
    """Sync-token normalization should have one Matrix-local source of truth."""
    assert normalize_sync_token(value) == expected


def test_successful_sync_certifies_checkpoint() -> None:
    """A response that lost nothing should save its next batch as certified."""
    decision = certify_sync_response(
        next_batch="s_next",
        recovery=SyncRecoveryOutcome(),
    )

    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.checkpoint_to_save == SyncCheckpoint("s_next")
    assert decision.clear_saved_token is False
    assert decision.reset_client_token is False


def test_leave_without_nio_gap_certifies_checkpoint() -> None:
    """A normal leave boundary must not suppress an otherwise safe checkpoint."""
    decision = certify_sync_response(
        next_batch="s_after_leave",
        recovery=SyncRecoveryOutcome(),
    )

    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.checkpoint_to_save == SyncCheckpoint("s_after_leave")


def test_unrecovered_boundary_replays_before_a_clean_retry_certifies() -> None:
    """An open transient gap cannot advance the application-owned cursor."""
    room_id = "!joined:localhost"
    boundary = certify_sync_response(
        next_batch="s_join",
        recovery=SyncRecoveryOutcome(unrecovered_room_ids=frozenset({room_id})),
    )
    clean = certify_sync_response(
        next_batch="s_replayed_clean",
        recovery=SyncRecoveryOutcome(),
    )

    assert boundary.state is SyncTrustState.UNCERTAIN
    assert boundary.reason == "sync_recovery_incomplete"
    assert boundary.reset_client_token is True
    assert clean.state is SyncTrustState.CERTIFIED
    assert clean.checkpoint_to_save == SyncCheckpoint("s_replayed_clean")


def test_only_unrecovered_rooms_fail_closed() -> None:
    """Nio's gap set blocks certification; the rooms it did recover do not."""
    recovered = certify_sync_response(
        next_batch="s_next",
        recovery=SyncRecoveryOutcome(recovered_room_ids=frozenset({"!recovered:localhost"})),
    )
    unrecovered = certify_sync_response(
        next_batch="s_next",
        recovery=SyncRecoveryOutcome(unrecovered_room_ids=frozenset({"!unrecovered:localhost"})),
    )

    assert recovered.state is SyncTrustState.CERTIFIED
    assert unrecovered.state is SyncTrustState.UNCERTAIN
    assert unrecovered.reason == "sync_recovery_incomplete"


def test_a_refused_admission_fails_closed() -> None:
    """An event this process could not take ownership of must not be checkpointed past."""
    decision = certify_sync_response(
        next_batch="s_next",
        recovery=SyncRecoveryOutcome(admission_refused=True),
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.clear_saved_token is False
    assert decision.reset_client_token is True
    assert decision.reason == "admission_refused"


def test_a_refused_admission_leaves_recovery_inconclusive() -> None:
    """A response that refused an event never reached its recovery verdict.

    This is what stops a refused response counting as evidence that a room's
    rebuild has stopped converging, which would let the stall detector give up
    on a gap that was never actually retried.
    """
    refused = SyncRecoveryOutcome(
        unrecovered_room_ids=frozenset({"!room:localhost"}),
        admission_refused=True,
    )
    accepted = SyncRecoveryOutcome(unrecovered_room_ids=frozenset({"!room:localhost"}))

    assert refused.recovery_conclusive is False
    assert accepted.recovery_conclusive is True


def test_a_skipped_gap_certifies_and_restarts_the_client() -> None:
    """Giving up on a room trades its history for a cursor that can move again."""
    room_id = "!stalled:localhost"
    decision = certify_sync_response(
        next_batch="s_past_the_gap",
        recovery=SyncRecoveryOutcome(unrecovered_room_ids=frozenset({room_id})),
        skipped_recovery_room_ids=frozenset({room_id}),
    )

    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.checkpoint_to_save == SyncCheckpoint("s_past_the_gap")
    # Nio may still hold recovery state for the room this checkpoint moved past,
    # so the client restarts from the new position rather than acknowledging.
    assert decision.reset_client_token is True


def test_a_skipped_gap_does_not_excuse_another_unrecovered_room() -> None:
    """Giving up on one room must not certify past a second room's missing history."""
    decision = certify_sync_response(
        next_batch="s_next",
        recovery=SyncRecoveryOutcome(
            unrecovered_room_ids=frozenset({"!stalled:localhost", "!other:localhost"}),
        ),
        skipped_recovery_room_ids=frozenset({"!stalled:localhost"}),
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.reason == "sync_recovery_incomplete"


def test_earlier_unrecovered_room_reports_incomplete_recovery() -> None:
    """An earlier open recovery gap must not be diagnosed as anything else."""
    decision = certify_sync_response(
        next_batch="s_next",
        recovery=SyncRecoveryOutcome(unrecovered_room_ids=frozenset({"!earlier:localhost"})),
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.reason == "sync_recovery_incomplete"


def test_sync_recovery_diagnostics_explains_uncertainty() -> None:
    """Certification logs should expose the recovery details behind uncertainty."""
    diagnostics = sync_recovery_diagnostics(
        SyncRecoveryOutcome(
            recovered_room_ids=frozenset({"!recovered:localhost"}),
            unrecovered_room_ids=frozenset({"!other:localhost"}),
            admission_refused=True,
        ),
    )

    assert diagnostics == {
        "sync_admission_refused": True,
        "sync_recovery_certified": False,
        "sync_recovered_room_count": 1,
        "sync_unrecovered_room_count": 1,
        "sync_recovered_room_ids": ("!recovered:localhost",),
        "sync_unrecovered_room_ids": ("!other:localhost",),
    }


def test_uncertainty_resets_client_token_without_clearing_retry() -> None:
    """An uncertified response should rewind nio to the retained durable token."""
    decision = certify_sync_response(
        next_batch="s_next",
        recovery=SyncRecoveryOutcome(admission_refused=True),
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.clear_saved_token is False
    assert decision.reset_client_token is True


def test_unrecovered_gap_resets_to_the_committed_checkpoint() -> None:
    """An open in-memory gap must be rebuilt from the committed checkpoint."""
    decision = certify_sync_response(
        next_batch="s_next",
        recovery=SyncRecoveryOutcome(unrecovered_room_ids=frozenset({"!room:localhost"})),
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.reason == "sync_recovery_incomplete"
    assert decision.checkpoint_to_save is None
    assert decision.clear_saved_token is False
    assert decision.reset_client_token is True


def test_missing_next_batch_fails_closed() -> None:
    """A sync response without a next batch cannot become a checkpoint."""
    decision = certify_sync_response(
        next_batch=None,
        recovery=SyncRecoveryOutcome(),
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.reason == "missing_next_batch"
    assert decision.clear_saved_token is False
    assert decision.reset_client_token is True


def test_a_missing_next_batch_outranks_a_refused_admission() -> None:
    """The position is reported before the reason it could not be trusted."""
    decision = certify_sync_response(
        next_batch=None,
        recovery=SyncRecoveryOutcome(admission_refused=True),
    )

    assert decision.reason == "missing_next_batch"


def test_unknown_pos_clears_saved_and_client_token() -> None:
    """M_UNKNOWN_POS must fail closed regardless of current state."""
    decision = handle_unknown_pos()

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.clear_saved_token is True
    assert decision.reset_client_token is True
    assert decision.reason == "unknown_pos"
