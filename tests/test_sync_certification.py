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
    assert cache_result._has_certification_blocker is False
    assert cache_result.certified is True


def test_own_join_boundary_without_recovery_debt_certifies_checkpoint() -> None:
    """A normal own-join boundary must not suppress an otherwise safe checkpoint."""
    room_id = "!joined:localhost"
    cache_result = SyncCacheWriteResult(
        complete=True,
        limited_room_ids=(room_id,),
        no_recovery_needed_room_ids=frozenset({room_id}),
    )

    decision = certify_sync_response(
        next_batch="s_after_join",
        cache_result=cache_result,
    )

    assert cache_result._unclassified_limited_room_ids == ()
    assert cache_result.certified is True
    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.checkpoint_to_save == SyncCheckpoint("s_after_join")
    assert decision.reason is None
    assert decision.reset_client_token is False


def test_leave_boundary_without_recovery_debt_certifies_checkpoint() -> None:
    """A normal leave boundary must not suppress an otherwise safe checkpoint."""
    room_id = "!left:localhost"

    decision = certify_sync_response(
        next_batch="s_after_leave",
        cache_result=SyncCacheWriteResult(
            complete=True,
            no_recovery_needed_room_ids=frozenset({room_id}),
        ),
    )

    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.checkpoint_to_save == SyncCheckpoint("s_after_leave")


def test_own_join_boundary_settles_prior_gap_before_clean_certification() -> None:
    """An own-join reset must discharge nio's same-response unrecovered outcome."""
    room_id = "!joined:localhost"
    gap = certify_sync_response(
        next_batch="s_gap",
        cache_result=SyncCacheWriteResult(
            complete=True,
            unrecovered_room_ids=frozenset({room_id}),
        ),
    )
    boundary = certify_sync_response(
        next_batch="s_join",
        cache_result=SyncCacheWriteResult(
            complete=True,
            limited_room_ids=(room_id,),
            unrecovered_room_ids=frozenset({room_id}),
            no_recovery_needed_room_ids=frozenset({room_id}),
        ),
        unsettled_recovery_room_ids=gap.unsettled_recovery_room_ids,
    )
    clean = certify_sync_response(
        next_batch="s_clean",
        cache_result=SyncCacheWriteResult(complete=True),
        unsettled_recovery_room_ids=boundary.unsettled_recovery_room_ids,
    )

    assert gap.unsettled_recovery_room_ids == frozenset({room_id})
    assert boundary.state is SyncTrustState.UNCERTAIN
    assert boundary.reason == "sync_recovery_boundary"
    assert boundary.unsettled_recovery_room_ids == frozenset()
    assert clean.state is SyncTrustState.CERTIFIED
    assert clean.checkpoint_to_save == SyncCheckpoint("s_clean")


def test_unrecovered_room_reason_outweighs_independent_join_boundary() -> None:
    """A benign join boundary must not hide another room's missing history."""
    decision = certify_sync_response(
        next_batch="s_next",
        cache_result=SyncCacheWriteResult(
            complete=True,
            limited_room_ids=("!joined:localhost",),
            unrecovered_room_ids=frozenset({"!missing:localhost"}),
            no_recovery_needed_room_ids=frozenset({"!joined:localhost"}),
        ),
    )

    assert decision.reason == "sync_recovery_incomplete"


def test_recovery_outcomes_fail_closed_for_unrecovered_and_unclassified_rooms() -> None:
    """Every limited room must be recovered and no earlier gap may remain open."""
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
    assert cache_result._has_certification_blocker is True
    assert cache_result.certified is False


@pytest.mark.parametrize(
    ("cache_result", "reason"),
    [
        (SyncCacheWriteResult(complete=False), "cache_write_incomplete"),
        (
            SyncCacheWriteResult(complete=True, limited_room_ids=("!room:localhost",)),
            "limited_sync_timeline",
        ),
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


def test_incomplete_response_cannot_silently_settle_prior_recovery_debt() -> None:
    """Only a complete response may prove that nio authoritatively ended a gap."""
    room_id = "!earlier:localhost"

    decision = certify_sync_response(
        next_batch="s_next",
        cache_result=SyncCacheWriteResult(complete=False),
        unsettled_recovery_room_ids=frozenset({room_id}),
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.reason == "cache_write_incomplete"
    assert decision.unsettled_recovery_room_ids == frozenset({room_id})


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
        "cache_no_recovery_needed_room_count": 0,
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


def test_unrecovered_gap_preserves_positioned_continuity() -> None:
    """A prior nio recovery gap must block a later response checkpoint."""
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
