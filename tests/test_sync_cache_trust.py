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
    callback_failure_count: int = 0

    def mark_callback_failed(self) -> None:
        self.callback_failure_count += 1


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


def test_callback_failure_blocks_later_certification(tmp_path: Path) -> None:
    """A callback failure prevents later sync responses from restoring trust."""
    trust, _cache, runtime = _trust(tmp_path)
    trust.mark_callback_failed()

    trust.certify_response(
        next_batch="s_after_failure",
        cache_result=SyncCacheWriteResult(complete=True),
        first_sync=False,
    )

    assert runtime.callback_failure_count == 1
    assert trust.state is SyncTrustState.UNCERTAIN
    assert trust.checkpoint is None
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


def test_limited_recovery_window_is_consumed_once_then_complete_delta_certifies(tmp_path: Path) -> None:
    """Recovery must avoid reset loops and certify only after a complete delta."""
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
    assert initial.reset_client_token is False
    assert initial.state is SyncTrustState.UNCERTAIN
    assert complete.state is SyncTrustState.CERTIFIED
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        token="s_complete",  # noqa: S106
        cache_generation=_GENERATION,
    )


@pytest.mark.asyncio
async def test_cold_limited_initial_window_does_not_reset_again(tmp_path: Path) -> None:
    """A since-less startup window may be limited without replaying itself forever."""
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

    assert decision.reset_client_token is False
    assert trust.state is SyncTrustState.UNCERTAIN


def test_callback_failure_preserves_pending_limited_recovery(tmp_path: Path) -> None:
    """A callback failure after rewind must not make the initial window rewind again."""
    trust, _cache, runtime = _trust(tmp_path)
    trust.state = SyncTrustState.CERTIFIED

    reset = trust.certify_response(
        next_batch="s_partial",
        cache_result=SyncCacheWriteResult(
            complete=False,
            limited_room_ids=("!room:localhost",),
        ),
        first_sync=False,
    )
    trust.mark_callback_failed()
    initial = trust.certify_response(
        next_batch="s_initial",
        cache_result=SyncCacheWriteResult(
            complete=False,
            limited_room_ids=("!room:localhost",),
        ),
        first_sync=False,
    )

    assert reset.reset_client_token is True
    assert runtime.callback_failure_count == 1
    assert initial.reset_client_token is False
    assert trust.state is SyncTrustState.UNCERTAIN


def test_unknown_position_marks_next_limited_window_as_initial(tmp_path: Path) -> None:
    """M_UNKNOWN_POS recovery consumes the next since-less limited window."""
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
    assert initial.reset_client_token is False


@pytest.mark.asyncio
async def test_clear_failure_disables_cache_and_skips_cold_cleanup(tmp_path: Path) -> None:
    """Failed deletion preserves rows and disables cache use for safe replay."""
    trust, cache, runtime = _trust(tmp_path)
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
    assert runtime.callback_failure_count == 1
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
