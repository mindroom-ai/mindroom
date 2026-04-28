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
    start_from_loaded_token,
    sync_cache_write_diagnostics,
)


def test_start_without_token_is_cold() -> None:
    """Missing saved token should start from cold sync state."""
    startup = start_from_loaded_token(None)

    assert startup.state is SyncTrustState.COLD
    assert startup.sync_token is None


def test_start_with_legacy_token_restores_sync_only() -> None:
    """Plaintext tokens restore nio continuity without checkpoint certification."""
    startup = start_from_loaded_token("s_legacy")

    assert startup.state is SyncTrustState.COLD
    assert startup.sync_token == "s_legacy"  # noqa: S105
    assert startup.legacy_token is True


def test_start_with_checkpoint_waits_for_first_sync() -> None:
    """Certified checkpoints become pending until catch-up writes are durable."""
    checkpoint = SyncCheckpoint(token="s_saved")  # noqa: S106

    startup = start_from_loaded_token(checkpoint)

    assert startup.state is SyncTrustState.PENDING
    assert startup.sync_token == "s_saved"  # noqa: S105


@pytest.mark.parametrize(
    "state",
    [
        SyncTrustState.COLD,
        SyncTrustState.PENDING,
        SyncTrustState.CERTIFIED,
        SyncTrustState.UNCERTAIN,
    ],
)
def test_successful_sync_certifies_checkpoint(
    state: SyncTrustState,
) -> None:
    """Durable sync writes should save the next batch as certified."""
    decision = certify_sync_response(
        state,
        next_batch="s_next",
        cache_result=SyncCacheWriteResult(complete=True),
        first_sync=state is SyncTrustState.PENDING,
    )

    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.checkpoint_to_save == SyncCheckpoint("s_next")
    assert decision.clear_saved_token is False
    assert decision.reset_client_token is False


@pytest.mark.parametrize(
    ("cache_result", "reason"),
    [
        (SyncCacheWriteResult(complete=False), "cache_write_incomplete"),
        (SyncCacheWriteResult(complete=True, limited_room_ids=("!room:localhost",)), "limited_sync_timeline"),
        (SyncCacheWriteResult(complete=True, errors=(RuntimeError("boom"),)), "cache_write_failed"),
        (SyncCacheWriteResult(complete=True, errors=(asyncio.CancelledError(),)), "cache_write_failed"),
    ],
)
def test_uncertain_sync_fails_closed(cache_result: SyncCacheWriteResult, reason: str) -> None:
    """Limited, failed, incomplete, or cancelled cache writes must not save a token."""
    decision = certify_sync_response(
        SyncTrustState.CERTIFIED,
        next_batch="s_next",
        cache_result=cache_result,
        first_sync=False,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.clear_saved_token is True
    assert decision.reset_client_token is False
    assert decision.reason == reason


def test_sync_cache_write_diagnostics_explains_uncertainty() -> None:
    """Sync-certification logs should expose the cache-write details behind uncertainty."""
    diagnostics = sync_cache_write_diagnostics(
        SyncCacheWriteResult(
            complete=False,
            limited_room_ids=("!room:localhost",),
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
        "cache_error_count": 1,
        "cache_runtime_available": False,
        "cache_task_count": 3,
        "cache_backend": "postgres",
        "cache_postgres_unavailable_reason": "connection closed",
        "cache_limited_room_ids": ("!room:localhost",),
        "cache_error_types": ("RuntimeError",),
        "cache_error_messages": ("cache failed",),
    }


def test_pending_first_sync_uncertainty_resets_client_token() -> None:
    """A failed restored-token catch-up should force nio off the ambiguous token."""
    decision = certify_sync_response(
        SyncTrustState.PENDING,
        next_batch="s_next",
        cache_result=SyncCacheWriteResult(complete=False),
        first_sync=True,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.clear_saved_token is True
    assert decision.reset_client_token is True


def test_missing_next_batch_fails_closed() -> None:
    """A sync response without a next batch cannot become a checkpoint."""
    decision = certify_sync_response(
        SyncTrustState.COLD,
        next_batch=None,
        cache_result=SyncCacheWriteResult(complete=True),
        first_sync=True,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.reason == "missing_next_batch"
    assert decision.clear_saved_token is True


def test_unknown_pos_clears_saved_and_client_token() -> None:
    """M_UNKNOWN_POS must fail closed regardless of current state."""
    decision = handle_unknown_pos()

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.clear_saved_token is True
    assert decision.reset_client_token is True
    assert decision.reason == "unknown_pos"
