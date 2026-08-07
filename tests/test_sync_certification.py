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


def test_an_unrecovered_room_does_not_withhold_the_checkpoint() -> None:
    """One room's open gap must not decide the cursor for every other room.

    nio fences a limited-timeline gap to its own room and carries it across
    restarts, so the checkpoint stays true for everything else. Withholding it
    is what made the next attempt ask for a strictly larger gap.
    """
    decision = certify_sync_response(
        next_batch="s_past_the_gap",
        recovery=SyncRecoveryOutcome(unrecovered_room_ids=frozenset({"!rebuilding:localhost"})),
    )

    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.checkpoint_to_save == SyncCheckpoint("s_past_the_gap")
    assert decision.reset_client_token is False


def test_many_unrecovered_rooms_still_certify() -> None:
    """Open gaps do not accumulate into a reason to stop the cursor."""
    decision = certify_sync_response(
        next_batch="s_next",
        recovery=SyncRecoveryOutcome(
            unrecovered_room_ids=frozenset({"!one:localhost", "!two:localhost"}),
        ),
    )

    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.reason is None


def test_an_unrecovered_room_cannot_excuse_a_refused_admission() -> None:
    """The remaining fail-closed arms are about the response, not about a room.

    An event admission refused inside the callback nio awaits has no durable
    owner, and that is still a reason to refuse the checkpoint however healthy
    every room's recovery looks.
    """
    decision = certify_sync_response(
        next_batch="s_next",
        recovery=SyncRecoveryOutcome(
            unrecovered_room_ids=frozenset({"!rebuilding:localhost"}),
            admission_refused=True,
        ),
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.reason == "admission_refused"


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
