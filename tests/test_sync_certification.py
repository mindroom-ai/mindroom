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
)


def test_start_without_token_is_cold() -> None:
    """Missing saved token should keep reads behind the runtime boundary."""
    startup = start_from_loaded_token(None, runtime_started_at=10.0)

    assert startup.state is SyncTrustState.COLD
    assert startup.sync_token is None
    assert startup.thread_cache_read_boundary == 10.0
    assert startup.checkpoint is None


def test_start_with_legacy_token_restores_sync_only() -> None:
    """Plaintext tokens restore nio continuity without trusting old cache rows."""
    startup = start_from_loaded_token("s_legacy", runtime_started_at=10.0)

    assert startup.state is SyncTrustState.COLD
    assert startup.sync_token == "s_legacy"  # noqa: S105
    assert startup.thread_cache_read_boundary == 10.0
    assert startup.checkpoint is None
    assert startup.legacy_token is True


def test_start_with_checkpoint_waits_for_first_sync() -> None:
    """Certified checkpoints become pending until catch-up writes are durable."""
    checkpoint = SyncCheckpoint(token="s_saved", thread_cache_valid_after=3.0)  # noqa: S106

    startup = start_from_loaded_token(checkpoint, runtime_started_at=10.0)

    assert startup.state is SyncTrustState.PENDING
    assert startup.sync_token == "s_saved"  # noqa: S105
    assert startup.thread_cache_read_boundary == 10.0
    assert startup.checkpoint == checkpoint


@pytest.mark.parametrize(
    ("state", "checkpoint", "current_boundary", "expected_boundary"),
    [
        (SyncTrustState.COLD, None, 10.0, 10.0),
        (SyncTrustState.PENDING, SyncCheckpoint("s_saved", 3.0), 10.0, 3.0),
        (SyncTrustState.CERTIFIED, SyncCheckpoint("s_saved", 3.0), 3.0, 3.0),
        (SyncTrustState.UNCERTAIN, None, 12.0, 12.0),
    ],
)
def test_successful_sync_certifies_checkpoint(
    state: SyncTrustState,
    checkpoint: SyncCheckpoint | None,
    current_boundary: float,
    expected_boundary: float,
) -> None:
    """Durable sync writes should save the next batch with the active safe boundary."""
    decision = certify_sync_response(
        state,
        previous_checkpoint=checkpoint,
        next_batch="s_next",
        cache_result=SyncCacheWriteResult(complete=True),
        first_sync=state is SyncTrustState.PENDING,
        now=20.0,
        current_read_boundary=current_boundary,
    )

    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.thread_cache_read_boundary == expected_boundary
    assert decision.checkpoint_to_save == SyncCheckpoint("s_next", expected_boundary)
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
        previous_checkpoint=SyncCheckpoint("s_saved", 3.0),
        next_batch="s_next",
        cache_result=cache_result,
        first_sync=False,
        now=20.0,
        current_read_boundary=3.0,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.thread_cache_read_boundary == 20.0
    assert decision.checkpoint_to_save is None
    assert decision.clear_saved_token is True
    assert decision.reset_client_token is False
    assert decision.reason == reason


def test_pending_first_sync_uncertainty_resets_client_token() -> None:
    """A failed restored-token catch-up should force nio off the ambiguous token."""
    decision = certify_sync_response(
        SyncTrustState.PENDING,
        previous_checkpoint=SyncCheckpoint("s_saved", 3.0),
        next_batch="s_next",
        cache_result=SyncCacheWriteResult(complete=False),
        first_sync=True,
        now=20.0,
        current_read_boundary=10.0,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.clear_saved_token is True
    assert decision.reset_client_token is True
    assert decision.thread_cache_read_boundary == 20.0


def test_missing_next_batch_fails_closed() -> None:
    """A sync response without a next batch cannot become a checkpoint."""
    decision = certify_sync_response(
        SyncTrustState.COLD,
        previous_checkpoint=None,
        next_batch=None,
        cache_result=SyncCacheWriteResult(complete=True),
        first_sync=True,
        now=20.0,
        current_read_boundary=10.0,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.reason == "missing_next_batch"
    assert decision.clear_saved_token is True


def test_unknown_pos_clears_saved_and_client_token() -> None:
    """M_UNKNOWN_POS must fail closed regardless of current state."""
    decision = handle_unknown_pos(now=20.0)

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.thread_cache_read_boundary == 20.0
    assert decision.checkpoint_to_save is None
    assert decision.clear_saved_token is True
    assert decision.reset_client_token is True
    assert decision.reason == "unknown_pos"
