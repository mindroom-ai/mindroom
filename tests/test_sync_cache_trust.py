"""Focused tests for Matrix sync-checkpoint and cache-trust ownership."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.matrix.sync_cache_trust import SyncCacheTrust
from mindroom.matrix.sync_certification import SyncCacheWriteResult, SyncCheckpoint, SyncTrustState
from mindroom.matrix.sync_tokens import load_sync_checkpoint, save_sync_token

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.matrix.cache import ConversationEventCache

_GENERATION = "cache-generation"


@dataclass
class _Runtime:
    event_cache: ConversationEventCache


def _trust(
    tmp_path: Path,
    *,
    cache_generation: str | None = _GENERATION,
) -> tuple[SyncCacheTrust, MagicMock, _Runtime]:
    cache = MagicMock()
    cache.cache_generation = cache_generation
    cache.initialize = AsyncMock()
    cache.purge_principal = AsyncMock()
    runtime = _Runtime(event_cache=cache)
    trust = SyncCacheTrust(
        storage_path=tmp_path,
        agent_name="code",
        runtime=runtime,
        logger=MagicMock(),
    )
    return trust, cache, runtime


@pytest.mark.asyncio
async def test_matching_checkpoint_restores_without_cold_cleanup(tmp_path: Path) -> None:
    """A matching cache generation restores continuity without deleting rows."""
    trust, cache, _runtime = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_saved", cache_generation=_GENERATION)

    token = await trust.prepare_startup()

    assert token == "s_saved"  # noqa: S105
    assert trust.state is SyncTrustState.PENDING
    assert trust.checkpoint is None
    cache.initialize.assert_awaited_once()
    cache.purge_principal.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_generation", [None, "replacement-generation"])
async def test_unverifiable_checkpoint_clears_and_starts_cold(
    tmp_path: Path,
    cache_generation: str | None,
) -> None:
    """Missing or changed cache generations invalidate saved continuity."""
    trust, cache, _runtime = _trust(tmp_path, cache_generation=cache_generation)
    save_sync_token(tmp_path, "code", "s_stale", cache_generation=_GENERATION)

    token = await trust.prepare_startup()

    assert token is None
    assert trust.state is SyncTrustState.COLD
    assert load_sync_checkpoint(tmp_path, "code") is None
    cache.purge_principal.assert_awaited_once()


def test_save_binds_checkpoint_to_current_cache_generation(tmp_path: Path) -> None:
    """Saved checkpoints include the generation that received the sync delta."""
    trust, _cache, _runtime = _trust(tmp_path)

    trust.save(SyncCheckpoint("s_new"))

    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        token="s_new",  # noqa: S106
        cache_generation=_GENERATION,
    )


def test_complete_cache_delta_certifies_raw_sync_continuity(tmp_path: Path) -> None:
    """Exact callback recovery must not poison independently durable raw cache continuity."""
    trust, _cache, _runtime = _trust(tmp_path)

    decision = trust.certify_response(
        next_batch="s_complete",
        cache_result=SyncCacheWriteResult(complete=True),
        first_sync=False,
    )

    assert decision.state is SyncTrustState.CERTIFIED
    assert trust.state is SyncTrustState.CERTIFIED
    assert trust.checkpoint == SyncCheckpoint("s_complete")
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        token="s_complete",  # noqa: S106
        cache_generation=_GENERATION,
    )


def test_planned_response_does_not_advance_checkpoint_until_applied(tmp_path: Path) -> None:
    """Callers may finish prerequisite durable work before certifying a sync position."""
    trust, _cache, _runtime = _trust(tmp_path)

    decision = trust.plan_response(
        next_batch="s_planned",
        cache_result=SyncCacheWriteResult(complete=True),
        first_sync=False,
    )

    assert decision.checkpoint_to_save == SyncCheckpoint("s_planned")
    assert trust.state is SyncTrustState.COLD
    assert trust.checkpoint is None
    assert load_sync_checkpoint(tmp_path, "code") is None

    trust.apply_response(decision, cache_result=SyncCacheWriteResult(complete=True))

    assert trust.state is SyncTrustState.CERTIFIED
    assert trust.checkpoint == SyncCheckpoint("s_planned")


def test_dispatch_persist_failure_is_consumed_once_per_epoch(tmp_path: Path) -> None:
    """Each new admission failure rejects certification exactly once."""
    trust, _cache, _runtime = _trust(tmp_path)

    assert not trust.consume_dispatch_persist_failure()

    trust.record_dispatch_persist_failure()
    trust.record_dispatch_persist_failure()

    assert trust.consume_dispatch_persist_failure()
    assert not trust.consume_dispatch_persist_failure()

    trust.record_dispatch_persist_failure()

    assert trust.consume_dispatch_persist_failure()


def test_dispatch_acceptance_policy_rejects_failed_response_then_applies_next(tmp_path: Path) -> None:
    """SyncCacheTrust must own the failure-epoch gate around planned certification."""
    trust, _cache, _runtime = _trust(tmp_path)
    cache_result = SyncCacheWriteResult(complete=True)
    failed_decision = trust.plan_response(
        next_batch="s_failed",
        cache_result=cache_result,
        first_sync=False,
    )
    trust.record_dispatch_persist_failure()

    rejected_decision, rejected = trust.apply_response_after_dispatch_acceptance(
        failed_decision,
        cache_result=cache_result,
    )

    assert rejected is True
    assert rejected_decision is failed_decision
    assert trust.state is SyncTrustState.COLD
    next_decision = trust.plan_response(
        next_batch="s_next",
        cache_result=cache_result,
        first_sync=False,
    )
    applied_decision, rejected = trust.apply_response_after_dispatch_acceptance(
        next_decision,
        cache_result=cache_result,
    )
    assert rejected is False
    assert applied_decision is next_decision
    assert trust.state is SyncTrustState.CERTIFIED


def test_cache_scope_invalidation_rejects_stale_certification_plan(tmp_path: Path) -> None:
    """A plan made before cache cleanup cannot restore or persist sync continuity."""
    trust, _cache, _runtime = _trust(tmp_path)
    trust.state = SyncTrustState.CERTIFIED
    trust.checkpoint = SyncCheckpoint("s_before_cleanup")
    trust.save(trust.checkpoint)
    cache_result = SyncCacheWriteResult(complete=True)
    decision = trust.plan_response(
        next_batch="s_stale_after_cleanup",
        cache_result=cache_result,
        first_sync=False,
    )

    assert trust.invalidate_for_cache_scope_cleanup()
    applied = trust.apply_response(decision, cache_result=cache_result)

    assert applied.state is SyncTrustState.UNCERTAIN
    assert applied.reset_client_token is True
    assert applied.reason == "cache_scope_invalidated"
    assert trust.state is SyncTrustState.UNCERTAIN
    assert trust.checkpoint is None
    assert trust.retry_token() is None
    assert load_sync_checkpoint(tmp_path, "code") is None


def test_positioned_limited_response_resets_sync_continuity(tmp_path: Path) -> None:
    """A limited response after a position must force one since-less replay."""
    trust, _cache, _runtime = _trust(tmp_path)
    trust.state = SyncTrustState.CERTIFIED
    trust.save(SyncCheckpoint("s_before_gap"))

    decision = trust.certify_response(
        next_batch="s_partial",
        cache_result=SyncCacheWriteResult(
            complete=False,
            limited_room_ids=("!room:localhost",),
        ),
        first_sync=False,
    )

    assert decision.reset_client_token is True
    assert decision.reason == "limited_sync_timeline"
    assert trust.state is SyncTrustState.UNCERTAIN
    assert trust.checkpoint is None
    assert load_sync_checkpoint(tmp_path, "code") is None


def test_consecutive_cache_failures_keep_rewinding_until_certified(tmp_path: Path) -> None:
    """Each uncached response must be replayed before a later checkpoint can certify."""
    trust, _cache, _runtime = _trust(tmp_path)
    trust.state = SyncTrustState.CERTIFIED

    first_failure = trust.certify_response(
        next_batch="s1",
        cache_result=SyncCacheWriteResult(
            complete=False,
            errors=(RuntimeError("first cache write failed"),),
        ),
        first_sync=False,
    )
    second_failure = trust.certify_response(
        next_batch="s2",
        cache_result=SyncCacheWriteResult(
            complete=False,
            errors=(RuntimeError("second cache write failed"),),
        ),
        first_sync=False,
    )

    assert first_failure.reset_client_token is True
    assert second_failure.reset_client_token is True
    assert trust.state is SyncTrustState.UNCERTAIN
    assert trust.checkpoint is None
    assert load_sync_checkpoint(tmp_path, "code") is None

    success = trust.certify_response(
        next_batch="s3",
        cache_result=SyncCacheWriteResult(complete=True),
        first_sync=False,
    )

    assert success.reset_client_token is False
    assert trust.state is SyncTrustState.CERTIFIED
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        token="s3",  # noqa: S106
        cache_generation=_GENERATION,
    )


def test_limited_recovery_keeps_rewinding_until_complete_delta_certifies(tmp_path: Path) -> None:
    """Each unresolved recovery window must replay until a complete delta certifies."""
    trust, _cache, _runtime = _trust(tmp_path)
    trust.state = SyncTrustState.CERTIFIED

    positioned = trust.certify_response(
        next_batch="s_partial",
        cache_result=SyncCacheWriteResult(
            complete=False,
            limited_room_ids=("!room:localhost",),
        ),
        first_sync=False,
    )
    initial = trust.certify_response(
        next_batch="s_initial",
        cache_result=SyncCacheWriteResult(
            complete=False,
            limited_room_ids=("!room:localhost",),
        ),
        first_sync=False,
    )
    complete = trust.certify_response(
        next_batch="s_complete",
        cache_result=SyncCacheWriteResult(complete=True),
        first_sync=False,
    )

    assert positioned.reset_client_token is True
    assert initial.reset_client_token is True
    assert initial.state is SyncTrustState.UNCERTAIN
    assert complete.state is SyncTrustState.CERTIFIED
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        token="s_complete",  # noqa: S106
        cache_generation=_GENERATION,
    )


def test_sustained_limited_responses_keep_rewinding_until_a_delta_certifies(tmp_path: Path) -> None:
    """Back-to-back unresolved gaps must not donate an incremental cursor."""
    trust, _cache, _runtime = _trust(tmp_path)
    trust.state = SyncTrustState.CERTIFIED

    decisions = [
        trust.certify_response(
            next_batch=f"s_partial_{index}",
            cache_result=SyncCacheWriteResult(
                complete=False,
                limited_room_ids=("!room:localhost",),
            ),
            first_sync=False,
        )
        for index in range(4)
    ]

    assert all(decision.reset_client_token for decision in decisions)


@pytest.mark.asyncio
async def test_cold_limited_initial_window_rewinds_until_certified(tmp_path: Path) -> None:
    """A cold response cannot donate a cursor until its cache result certifies."""
    trust, _cache, _runtime = _trust(tmp_path)

    assert await trust.prepare_startup() is None
    decision = trust.certify_response(
        next_batch="s_initial",
        cache_result=SyncCacheWriteResult(
            complete=False,
            limited_room_ids=("!room:localhost",),
        ),
        first_sync=True,
    )

    assert decision.reset_client_token is True
    assert trust.state is SyncTrustState.UNCERTAIN


def test_unknown_position_limited_window_keeps_rewinding_until_certified(tmp_path: Path) -> None:
    """M_UNKNOWN_POS recovery cannot advance past an uncertified window."""
    trust, _cache, _runtime = _trust(tmp_path)

    unknown = trust.reject_unknown_pos()
    initial = trust.certify_response(
        next_batch="s_initial",
        cache_result=SyncCacheWriteResult(
            complete=False,
            limited_room_ids=("!room:localhost",),
        ),
        first_sync=False,
    )

    assert unknown.reset_client_token is True
    assert initial.reset_client_token is True


@pytest.mark.asyncio
async def test_clear_failure_disables_cache_and_skips_cold_cleanup(tmp_path: Path) -> None:
    """Failed deletion preserves rows and disables cache use for safe replay."""
    trust, cache, _runtime = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_preserved", cache_generation=_GENERATION)

    with (
        patch(
            "mindroom.matrix.sync_cache_trust.load_sync_checkpoint",
            side_effect=OSError("checkpoint unreadable"),
        ),
        patch(
            "mindroom.matrix.sync_cache_trust.clear_sync_token",
            side_effect=OSError("checkpoint cannot be removed"),
        ),
    ):
        token = await trust.prepare_startup()

    assert token is None
    assert load_sync_checkpoint(tmp_path, "code") is not None
    cache.disable.assert_called_once_with("sync_checkpoint_clear_failed")
    cache.purge_principal.assert_not_awaited()


@pytest.mark.asyncio
async def test_cold_start_purges_untrusted_principal_rows(tmp_path: Path) -> None:
    """Cold startup removes principal rows before cache use."""
    trust, cache, _runtime = _trust(tmp_path)

    assert await trust.prepare_startup() is None

    cache.purge_principal.assert_awaited_once()
    cache.disable.assert_not_called()


@pytest.mark.asyncio
async def test_failed_cold_start_cleanup_disables_principal_view(tmp_path: Path) -> None:
    """Failed cold cleanup leaves the principal view network-only."""
    trust, cache, _runtime = _trust(tmp_path)
    cache.purge_principal.side_effect = RuntimeError("purge failed")

    assert await trust.prepare_startup() is None

    cache.disable.assert_called_once_with("untrusted_principal_cache_cleanup_failed")
    assert trust.state is SyncTrustState.COLD


def test_retry_token_prefers_current_certified_checkpoint(tmp_path: Path) -> None:
    """An in-memory certified checkpoint is the first replay choice."""
    trust, _cache, _runtime = _trust(tmp_path)
    trust.checkpoint = SyncCheckpoint("s_current")
    save_sync_token(tmp_path, "code", "s_saved", cache_generation=_GENERATION)

    assert trust.retry_token() == "s_current"


@pytest.mark.parametrize(
    ("cache_generation", "saved_generation", "expected"),
    [
        (_GENERATION, _GENERATION, "s_saved"),
        ("replacement-generation", _GENERATION, None),
        (None, _GENERATION, None),
    ],
)
def test_saved_retry_token_requires_current_generation(
    tmp_path: Path,
    cache_generation: str | None,
    saved_generation: str,
    expected: str | None,
) -> None:
    """A durable retry token is usable only with its original generation."""
    trust, _cache, _runtime = _trust(tmp_path, cache_generation=cache_generation)
    save_sync_token(tmp_path, "code", "s_saved", cache_generation=saved_generation)

    assert trust.retry_token() == expected
