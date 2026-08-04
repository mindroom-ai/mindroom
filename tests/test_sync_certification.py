"""Tests for Matrix sync-token cache certification."""

from __future__ import annotations

import asyncio

import pytest

from mindroom.matrix.sync_certification import (
    SyncCacheWriteResult,
    SyncCheckpoint,
    SyncTrustState,
    certify_sync_response,
    handle_unknown_pos,
    sync_cache_write_diagnostics,
)
from mindroom.matrix.sync_token_values import normalize_sync_token


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
    """Durable sync writes should save the next batch as certified."""
    decision = certify_sync_response(
        next_batch="s_next",
        cache_result=SyncCacheWriteResult(complete=True),
    )

    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.checkpoint_to_save == SyncCheckpoint("s_next")
    assert decision.clear_saved_token is False
    assert decision.reset_client_token is False


def test_recovered_limited_room_certifies_after_nio_callback_success() -> None:
    """Pinned nio reports recovered only after every non-live callback succeeds."""
    room_id = "!recovered:localhost"
    cache_result = SyncCacheWriteResult(
        complete=True,
        limited_room_ids=(room_id,),
        recovered_room_ids=frozenset({room_id}),
    )

    assert cache_result._unclassified_limited_room_ids == ()
    assert cache_result.certified is True


def test_positioned_limited_room_without_nio_gap_certifies_checkpoint() -> None:
    """Nio's aggregate outcome absence proves a positioned limited room has no real gap."""
    room_id = "!limited:localhost"
    cache_result = SyncCacheWriteResult(
        complete=True,
        limited_room_ids=(room_id,),
    )

    decision = certify_sync_response(
        next_batch="s_after_limited",
        cache_result=cache_result,
    )

    assert cache_result._unclassified_limited_room_ids == (room_id,)
    assert cache_result.certified is True
    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.checkpoint_to_save == SyncCheckpoint("s_after_limited")
    assert decision.reason is None
    assert decision.reset_client_token is False


def test_leave_without_nio_gap_certifies_checkpoint() -> None:
    """A normal leave boundary must not suppress an otherwise safe checkpoint."""
    decision = certify_sync_response(
        next_batch="s_after_leave",
        cache_result=SyncCacheWriteResult(complete=True),
    )

    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.checkpoint_to_save == SyncCheckpoint("s_after_leave")


def test_unrecovered_boundary_replays_before_a_clean_retry_certifies() -> None:
    """An open transient gap cannot advance the application-owned cursor."""
    room_id = "!joined:localhost"
    boundary = certify_sync_response(
        next_batch="s_join",
        cache_result=SyncCacheWriteResult(
            complete=True,
            limited_room_ids=(room_id,),
            unrecovered_room_ids=frozenset({room_id}),
        ),
    )
    clean = certify_sync_response(
        next_batch="s_replayed_clean",
        cache_result=SyncCacheWriteResult(
            complete=True,
            limited_room_ids=(room_id,),
        ),
    )

    assert boundary.state is SyncTrustState.UNCERTAIN
    assert boundary.reason == "sync_recovery_incomplete"
    assert boundary.reset_client_token is True
    assert clean.state is SyncTrustState.CERTIFIED
    assert clean.checkpoint_to_save == SyncCheckpoint("s_replayed_clean")


def test_unrecovered_room_outweighs_independent_limited_room() -> None:
    """A limited room without a gap must not hide another room's missing history."""
    decision = certify_sync_response(
        next_batch="s_next",
        cache_result=SyncCacheWriteResult(
            complete=True,
            limited_room_ids=("!joined:localhost",),
            unrecovered_room_ids=frozenset({"!missing:localhost"}),
        ),
    )

    assert decision.reason == "sync_recovery_incomplete"


def test_recovery_outcomes_fail_closed_only_for_nio_unrecovered_rooms() -> None:
    """Nio's aggregate gap set blocks certification while outcome absence does not."""
    recovered_room = "!recovered:localhost"
    unrecovered_room = "!unrecovered:localhost"
    unclassified_room = "!unclassified:localhost"
    cache_result = SyncCacheWriteResult(
        complete=True,
        limited_room_ids=(recovered_room, unrecovered_room, unclassified_room),
        recovered_room_ids=frozenset({recovered_room}),
        unrecovered_room_ids=frozenset({unrecovered_room}),
    )

    assert cache_result._unclassified_limited_room_ids == (unclassified_room,)
    assert cache_result.certified is False

    no_gap_result = SyncCacheWriteResult(
        complete=True,
        limited_room_ids=(unclassified_room,),
    )
    assert no_gap_result.certified is True


@pytest.mark.parametrize(
    ("cache_result", "reason"),
    [
        (SyncCacheWriteResult(complete=False), "cache_write_incomplete"),
        (SyncCacheWriteResult(complete=True, errors=(RuntimeError("boom"),)), "cache_write_failed"),
        (SyncCacheWriteResult(complete=True, errors=(asyncio.CancelledError(),)), "cache_write_failed"),
    ],
)
def test_uncertain_sync_fails_closed(
    cache_result: SyncCacheWriteResult,
    reason: str,
) -> None:
    """Local uncertainty must rewind without discarding the durable retry token."""
    decision = certify_sync_response(
        next_batch="s_next",
        cache_result=cache_result,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.clear_saved_token is False
    assert decision.reset_client_token is True
    assert decision.reason == reason


def test_earlier_unrecovered_room_reports_incomplete_recovery() -> None:
    """An earlier open recovery gap must not be diagnosed as a current limited timeline."""
    decision = certify_sync_response(
        next_batch="s_next",
        cache_result=SyncCacheWriteResult(
            complete=True,
            unrecovered_room_ids=frozenset({"!earlier:localhost"}),
        ),
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.reason == "sync_recovery_incomplete"


def test_sync_cache_write_diagnostics_explains_uncertainty() -> None:
    """Sync-certification logs should expose the cache-write details behind uncertainty."""
    diagnostics = sync_cache_write_diagnostics(
        SyncCacheWriteResult(
            complete=False,
            limited_room_ids=("!room:localhost",),
            unrecovered_room_ids=frozenset({"!other:localhost"}),
            errors=(RuntimeError("cache failed"),),
            runtime_available=False,
            task_count=3,
            runtime_diagnostics={
                "cache_backend": "postgres",
                "cache_postgres_unavailable_reason": "connection closed",
            },
        ),
    )

    assert diagnostics == {
        "cache_write_complete": False,
        "cache_write_certified": False,
        "cache_limited_room_count": 1,
        "cache_recovered_room_count": 0,
        "cache_unrecovered_room_count": 1,
        "cache_unclassified_limited_room_count": 1,
        "cache_error_count": 1,
        "cache_runtime_available": False,
        "cache_task_count": 3,
        "cache_backend": "postgres",
        "cache_postgres_unavailable_reason": "connection closed",
        "cache_limited_room_ids": ("!room:localhost",),
        "cache_unrecovered_room_ids": ("!other:localhost",),
        "cache_unclassified_limited_room_ids": ("!room:localhost",),
        "cache_error_types": ("RuntimeError",),
        "cache_error_messages": ("cache failed",),
    }


def test_uncertainty_resets_client_token_without_clearing_retry() -> None:
    """An uncertified response should rewind nio to the retained durable token."""
    decision = certify_sync_response(
        next_batch="s_next",
        cache_result=SyncCacheWriteResult(complete=False),
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.clear_saved_token is False
    assert decision.reset_client_token is True


def test_limited_cache_failure_preserves_positioned_continuity() -> None:
    """A cache error must not hide the limited window's continuity requirement."""
    decision = certify_sync_response(
        next_batch="s_next",
        cache_result=SyncCacheWriteResult(
            complete=False,
            limited_room_ids=("!room:localhost",),
            errors=(RuntimeError("cache failed"),),
        ),
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.reason == "cache_write_failed"
    assert decision.clear_saved_token is False
    assert decision.reset_client_token is True


def test_unrecovered_gap_resets_to_the_committed_checkpoint() -> None:
    """An open in-memory gap must be rebuilt from the committed checkpoint."""
    decision = certify_sync_response(
        next_batch="s_next",
        cache_result=SyncCacheWriteResult(
            complete=True,
            unrecovered_room_ids=frozenset({"!room:localhost"}),
        ),
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.reason == "sync_recovery_incomplete"
    assert decision.checkpoint_to_save is None
    assert decision.clear_saved_token is False
    assert decision.reset_client_token is True


def test_tokenless_initial_snapshot_certifies_its_baseline() -> None:
    """A limited initial snapshot is a valid baseline, not an incremental gap."""
    decision = certify_sync_response(
        next_batch="s_baseline",
        cache_result=SyncCacheWriteResult(
            complete=True,
            limited_room_ids=("!room:localhost",),
        ),
    )

    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.checkpoint_to_save == SyncCheckpoint("s_baseline")
    assert decision.reset_client_token is False


def test_missing_next_batch_fails_closed() -> None:
    """A sync response without a next batch cannot become a checkpoint."""
    decision = certify_sync_response(
        next_batch=None,
        cache_result=SyncCacheWriteResult(complete=True),
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.reason == "missing_next_batch"
    assert decision.clear_saved_token is False
    assert decision.reset_client_token is True


def test_unknown_pos_clears_saved_and_client_token() -> None:
    """M_UNKNOWN_POS must fail closed regardless of current state."""
    decision = handle_unknown_pos()

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.clear_saved_token is True
    assert decision.reset_client_token is True
    assert decision.reason == "unknown_pos"
