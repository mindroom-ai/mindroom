"""Focused tests for Matrix sync-checkpoint and cache-trust ownership."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.matrix.sync_cache_trust import SyncCacheTrust
from mindroom.matrix.sync_certification import (
    SyncCacheWriteResult,
    SyncCheckpoint,
    SyncTrustState,
    handle_unknown_pos,
)
from mindroom.matrix.sync_continuity import SyncContinuityRecord, SyncContinuityStore
from tests.sync_continuity_helpers import load_sync_checkpoint, save_sync_token

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
        continuity_store=SyncContinuityStore(tmp_path, "code"),
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
@pytest.mark.parametrize("safe_token", ["s_safe", None], ids=["warm", "cold"])
async def test_startup_resumes_nio_transport_then_requires_safe_replay(
    tmp_path: Path,
    safe_token: str | None,
) -> None:
    """A newer NIO cursor may drain persisted gaps but cannot certify cache state."""
    trust, _cache, _runtime = _trust(tmp_path)
    if safe_token is not None:
        save_sync_token(tmp_path, "code", safe_token, cache_generation=_GENERATION)

    token = await trust.prepare_startup(
        transport_resume_token="s_nio_live",  # noqa: S106
    )
    replay = await trust.certify_response(
        next_batch="s_after_recovery",
        cache_result=SyncCacheWriteResult(complete=True),
    )

    assert token == "s_nio_live"  # noqa: S105
    assert replay.reason == "sync_cache_replay_required"
    assert replay.reset_client_token is True
    assert trust.retry_token() == safe_token


@pytest.mark.asyncio
async def test_startup_matching_nio_and_safe_tokens_need_no_replay(tmp_path: Path) -> None:
    """An already certified NIO transport position remains ordinary warm startup."""
    trust, _cache, _runtime = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_safe", cache_generation=_GENERATION)

    token = await trust.prepare_startup(
        transport_resume_token="s_safe",  # noqa: S106
    )
    decision = await trust.certify_response(
        next_batch="s_after",
        cache_result=SyncCacheWriteResult(complete=True),
    )

    assert token == "s_safe"  # noqa: S105
    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.reset_client_token is False


@pytest.mark.asyncio
async def test_retry_token_uses_loaded_checkpoint_without_disk_reads(tmp_path: Path) -> None:
    """Pre-certification retries reuse startup state instead of loading per response."""
    trust, _cache, _runtime = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_saved", cache_generation=_GENERATION)
    assert await trust.prepare_startup() == "s_saved"

    with patch.object(
        trust.continuity_store,
        "load",
        side_effect=AssertionError("unexpected continuity reload"),
    ):
        assert trust.retry_token() == "s_saved"
        assert trust.retry_token() == "s_saved"


@pytest.mark.asyncio
async def test_startup_continuity_load_runs_off_event_loop(tmp_path: Path) -> None:
    """Startup checkpoint reads cannot block Matrix runtime progress."""
    trust, _cache, _runtime = _trust(tmp_path)
    load = trust.continuity_store.load
    load_thread: threading.Thread | None = None

    def record_load_thread() -> SyncContinuityRecord:
        nonlocal load_thread
        load_thread = threading.current_thread()
        return load()

    with patch.object(trust.continuity_store, "load", side_effect=record_load_thread):
        await trust.prepare_startup()

    assert load_thread is not None
    assert load_thread is not threading.main_thread()


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        '{"version":"future"}',
    ],
)
async def test_invalid_continuity_record_is_repaired_and_starts_cold(
    tmp_path: Path,
    payload: str,
) -> None:
    """Corrupt or future continuity must fail cold without bricking startup."""
    path = tmp_path / "sync_continuity" / "code.json"
    path.parent.mkdir(parents=True)
    path.write_text(payload, encoding="utf-8")
    trust, cache, _runtime = _trust(tmp_path)

    assert await trust.prepare_startup() is None

    assert trust.state is SyncTrustState.COLD
    assert trust.continuity_store.load() == SyncContinuityRecord(revision=1)
    cache.purge_principal.assert_awaited_once()
    invalid_log = next(
        call for call in trust.logger.warning.call_args_list if call.args == ("matrix_sync_continuity_invalid",)
    )
    assert "Invalid Matrix sync continuity record" in invalid_log.kwargs["error"]


@pytest.mark.asyncio
async def test_complete_cache_delta_certifies_raw_sync_continuity(tmp_path: Path) -> None:
    """Exact callback recovery must not poison independently durable raw cache continuity."""
    trust, _cache, _runtime = _trust(tmp_path)

    decision = await trust.certify_response(
        next_batch="s_complete",
        cache_result=SyncCacheWriteResult(complete=True),
    )

    assert decision.state is SyncTrustState.CERTIFIED
    assert trust.state is SyncTrustState.CERTIFIED
    assert trust.checkpoint == SyncCheckpoint("s_complete")
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        token="s_complete",  # noqa: S106
        cache_generation=_GENERATION,
    )


@pytest.mark.asyncio
async def test_planned_response_does_not_advance_checkpoint_until_applied(tmp_path: Path) -> None:
    """Callers may finish prerequisite durable work before certifying a sync position."""
    trust, _cache, _runtime = _trust(tmp_path)

    decision = trust.plan_response(
        next_batch="s_planned",
        cache_result=SyncCacheWriteResult(complete=True),
    )

    assert decision.checkpoint_to_save == SyncCheckpoint("s_planned")
    assert trust.state is SyncTrustState.COLD
    assert trust.checkpoint is None
    assert load_sync_checkpoint(tmp_path, "code") is None

    await trust.apply_response(decision, cache_result=SyncCacheWriteResult(complete=True))

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


def test_acknowledged_dispatch_failure_does_not_reject_later_certification(
    tmp_path: Path,
) -> None:
    """Non-checkpointed transports may settle failures without poisoning Classic."""
    trust, _cache, _runtime = _trust(tmp_path)
    trust.record_dispatch_persist_failure()

    trust.acknowledge_dispatch_persist_failures()

    assert not trust.consume_dispatch_persist_failure()


@pytest.mark.asyncio
async def test_cache_scope_invalidation_rejects_stale_certification_plan(tmp_path: Path) -> None:
    """A plan made before cache cleanup cannot restore or persist sync continuity."""
    trust, _cache, _runtime = _trust(tmp_path)
    trust.state = SyncTrustState.CERTIFIED
    trust.checkpoint = SyncCheckpoint("s_before_cleanup")
    trust.continuity_store.replace_checkpoint(
        SyncCheckpoint("s_before_cleanup", cache_generation=_GENERATION),
    )
    cache_result = SyncCacheWriteResult(complete=True)
    decision = trust.plan_response(
        next_batch="s_stale_after_cleanup",
        cache_result=cache_result,
    )

    assert await trust.invalidate_for_cache_scope_cleanup()
    applied, _record = await trust.apply_response(decision, cache_result=cache_result)

    assert applied.state is SyncTrustState.UNCERTAIN
    assert applied.reset_client_token is True
    assert applied.reason == "cache_scope_invalidated"
    assert trust.state is SyncTrustState.UNCERTAIN
    assert trust.checkpoint is None
    assert trust.retry_token() is None
    assert load_sync_checkpoint(tmp_path, "code") is None


@pytest.mark.asyncio
async def test_cache_scope_invalidation_preserves_pending_nio_recovery(tmp_path: Path) -> None:
    """Room cleanup clears the checkpoint but not NIO's in-flight recovery handoff."""
    trust, _cache, _runtime = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_before_gap", cache_generation=_GENERATION)
    assert await trust.prepare_startup() == "s_before_gap"
    gap = await trust.certify_response(
        next_batch="s_gap",
        cache_result=SyncCacheWriteResult(
            complete=False,
            unrecovered_room_ids=frozenset({"!gap:localhost"}),
        ),
    )
    assert gap.reset_client_token is False

    assert await trust.invalidate_for_cache_scope_cleanup()

    assert trust.rewind_is_deferred_until_recovery()
    assert trust.retry_token() is None
    recovery = await trust.certify_response(
        next_batch="s_recovered",
        cache_result=SyncCacheWriteResult(
            complete=True,
            recovered_room_ids=frozenset({"!gap:localhost"}),
        ),
    )

    assert recovery.reason == "sync_cache_replay_required"
    assert recovery.reset_client_token is True
    assert load_sync_checkpoint(tmp_path, "code") is None


@pytest.mark.asyncio
async def test_stale_gap_plan_keeps_recovery_handoff_after_cache_invalidation(
    tmp_path: Path,
) -> None:
    """A cleanup racing plan application cannot rewind beneath NIO's open gap."""
    trust, _cache, _runtime = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_before_gap", cache_generation=_GENERATION)
    assert await trust.prepare_startup() == "s_before_gap"
    cache_result = SyncCacheWriteResult(
        complete=False,
        unrecovered_room_ids=frozenset({"!gap:localhost"}),
    )
    decision = trust.plan_response(
        next_batch="s_gap",
        cache_result=cache_result,
    )

    assert await trust.invalidate_for_cache_scope_cleanup()
    applied, _record = await trust.apply_response(
        decision,
        cache_result=cache_result,
    )

    assert applied.reason == "cache_scope_invalidated"
    assert applied.clear_saved_token is True
    assert applied.reset_client_token is False
    assert applied.unresolved_recovery_room_ids == frozenset({"!gap:localhost"})
    assert applied.replay_required_after_recovery is True
    assert trust.rewind_is_deferred_until_recovery()
    assert trust.retry_token() is None


@pytest.mark.asyncio
async def test_cache_scope_invalidation_serializes_after_inflight_certification(
    tmp_path: Path,
) -> None:
    """Invalidation must clear a certification that already entered its durable write."""
    trust, _cache, _runtime = _trust(tmp_path)
    cache_result = SyncCacheWriteResult(complete=True)
    decision = trust.plan_response(
        next_batch="s_stale",
        cache_result=cache_result,
    )
    write_started = threading.Event()
    release_write = threading.Event()
    accept_classic_response = trust.continuity_store.accept_classic_response

    def blocking_accept(
        checkpoint: SyncCheckpoint,
        *,
        joined_room_ids: object,
    ) -> SyncContinuityRecord:
        write_started.set()
        assert release_write.wait(timeout=2)
        return accept_classic_response(
            checkpoint,
            joined_room_ids=joined_room_ids,  # type: ignore[arg-type]
        )

    with patch.object(
        trust.continuity_store,
        "accept_classic_response",
        side_effect=blocking_accept,
    ):
        apply_task = asyncio.create_task(
            trust.apply_response(decision, cache_result=cache_result),
        )
        assert await asyncio.to_thread(write_started.wait, 2)
        invalidate_task = asyncio.create_task(trust.invalidate_for_cache_scope_cleanup())
        await asyncio.sleep(0.05)
        invalidation_finished_before_write = invalidate_task.done()
        release_write.set()
        await asyncio.gather(apply_task, invalidate_task)

    assert not invalidation_finished_before_write
    assert trust.state is SyncTrustState.UNCERTAIN
    assert trust.checkpoint is None
    assert trust.retry_token() is None
    assert load_sync_checkpoint(tmp_path, "code") is None


@pytest.mark.asyncio
async def test_cache_scope_invalidation_serializes_after_inflight_shutdown_persist(
    tmp_path: Path,
) -> None:
    """Invalidation must clear a shutdown persist that already entered its durable write."""
    trust, _cache, _runtime = _trust(tmp_path)
    trust.state = SyncTrustState.CERTIFIED
    trust.checkpoint = SyncCheckpoint("s_old")
    trust.continuity_store.replace_checkpoint(
        SyncCheckpoint("s_old", cache_generation=_GENERATION),
    )
    write_started = threading.Event()
    release_write = threading.Event()
    replace_checkpoint = trust.continuity_store.replace_checkpoint

    def blocking_replace(checkpoint: SyncCheckpoint) -> SyncContinuityRecord:
        write_started.set()
        assert release_write.wait(timeout=2)
        return replace_checkpoint(checkpoint)

    with patch.object(
        trust.continuity_store,
        "replace_checkpoint",
        side_effect=blocking_replace,
    ):
        persist_task = asyncio.create_task(trust.persist_current())
        assert await asyncio.to_thread(write_started.wait, 2)
        invalidate_task = asyncio.create_task(trust.invalidate_for_cache_scope_cleanup())
        await asyncio.sleep(0.05)
        invalidation_finished_before_write = invalidate_task.done()
        release_write.set()
        await asyncio.gather(persist_task, invalidate_task)

    assert not invalidation_finished_before_write
    assert trust.state is SyncTrustState.UNCERTAIN
    assert trust.checkpoint is None
    assert trust.retry_token() is None
    assert load_sync_checkpoint(tmp_path, "code") is None


@pytest.mark.asyncio
async def test_shutdown_persist_without_cache_generation_clears_saved_checkpoint(
    tmp_path: Path,
) -> None:
    """Disabled cache generation must leave restart cold without masking shutdown."""
    trust, _cache, runtime = _trust(tmp_path)
    trust.state = SyncTrustState.CERTIFIED
    trust.checkpoint = SyncCheckpoint("s_runtime")
    trust.continuity_store.replace_checkpoint(
        SyncCheckpoint("s_saved", cache_generation=_GENERATION),
    )
    runtime.event_cache.cache_generation = None

    await trust.persist_current()

    assert load_sync_checkpoint(tmp_path, "code") is None
    trust.logger.warning.assert_any_call(
        "matrix_sync_checkpoint_skipped_without_cache_generation",
    )


@pytest.mark.asyncio
async def test_positioned_limited_response_resets_live_cursor_and_preserves_checkpoint(tmp_path: Path) -> None:
    """A limited response must replay from the last durable checkpoint."""
    trust, _cache, _runtime = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_before_gap", cache_generation=_GENERATION)
    assert await trust.prepare_startup() == "s_before_gap"

    decision = await trust.certify_response(
        next_batch="s_partial",
        cache_result=SyncCacheWriteResult(
            complete=False,
            limited_room_ids=("!room:localhost",),
        ),
    )

    assert decision.reset_client_token is True
    assert decision.reason == "cache_write_incomplete"
    assert trust.state is SyncTrustState.UNCERTAIN
    assert trust.checkpoint is None
    assert trust.retry_token() == "s_before_gap"
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        "s_before_gap",
        cache_generation=_GENERATION,
    )


@pytest.mark.asyncio
async def test_positioned_outcomes_drain_gap_then_replay_unresolved_window(tmp_path: Path) -> None:
    """Outcome disappearance rewinds only after nio had a chance to drain the gap."""
    trust, _cache, _runtime = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_before_gap", cache_generation=_GENERATION)
    assert await trust.prepare_startup() == "s_before_gap"

    unrecovered = await trust.certify_response(
        next_batch="s_unrecovered",
        cache_result=SyncCacheWriteResult(
            complete=True,
            unrecovered_room_ids=frozenset({"!room:localhost"}),
        ),
    )
    unresolved = await trust.certify_response(
        next_batch="s_unclassified",
        cache_result=SyncCacheWriteResult(
            complete=True,
            limited_room_ids=("!room:localhost",),
        ),
    )
    replayed = await trust.certify_response(
        next_batch="s_replayed",
        cache_result=SyncCacheWriteResult(
            complete=True,
            limited_room_ids=("!room:localhost",),
        ),
    )

    assert unrecovered.reset_client_token is False
    assert unresolved.reason == "sync_recovery_unresolved"
    assert unresolved.reset_client_token is True
    assert replayed.reset_client_token is False
    assert trust.retry_token() == "s_replayed"
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        "s_replayed",
        cache_generation=_GENERATION,
    )


@pytest.mark.asyncio
async def test_terminal_unrecovered_gap_rewinds_after_outcome_disappears(
    tmp_path: Path,
) -> None:
    """NIO ending an abandoned gap must replay from safe continuity once."""
    trust, _cache, _runtime = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_before_gap", cache_generation=_GENERATION)
    assert await trust.prepare_startup() == "s_before_gap"
    gap_result = SyncCacheWriteResult(
        complete=True,
        unrecovered_room_ids=frozenset({"!room:localhost"}),
    )

    first = await trust.certify_response(
        next_batch="s_after_gap_1",
        cache_result=gap_result,
    )
    second = await trust.certify_response(
        next_batch="s_after_gap_2",
        cache_result=gap_result,
    )
    unresolved = await trust.certify_response(
        next_batch="s_after_clean_delta",
        cache_result=SyncCacheWriteResult(complete=True),
    )
    clean = await trust.certify_response(
        next_batch="s_after_clean_replay",
        cache_result=SyncCacheWriteResult(complete=True),
    )

    assert first.reset_client_token is False
    assert second.reset_client_token is False
    assert unresolved.reason == "sync_recovery_unresolved"
    assert unresolved.reset_client_token is True
    assert clean.reset_client_token is False
    assert clean.state is SyncTrustState.CERTIFIED
    assert clean.reason is None
    assert trust.retry_token() == "s_after_clean_replay"
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        "s_after_clean_replay",
        cache_generation=_GENERATION,
    )


@pytest.mark.asyncio
async def test_recovered_outcome_settles_prior_gap_and_certifies(tmp_path: Path) -> None:
    """A later positive NIO outcome must settle exact recovery debt."""
    trust, _cache, _runtime = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_before_gap", cache_generation=_GENERATION)
    assert await trust.prepare_startup() == "s_before_gap"
    room_id = "!room:localhost"
    await trust.certify_response(
        next_batch="s_after_gap",
        cache_result=SyncCacheWriteResult(
            complete=True,
            unrecovered_room_ids=frozenset({room_id}),
        ),
    )

    recovered = await trust.certify_response(
        next_batch="s_after_recovery",
        cache_result=SyncCacheWriteResult(
            complete=True,
            recovered_room_ids=frozenset({room_id}),
        ),
    )

    assert recovered.state is SyncTrustState.CERTIFIED
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        "s_after_recovery",
        cache_generation=_GENERATION,
    )


@pytest.mark.asyncio
async def test_consecutive_cache_failures_keep_rewinding_until_certified(tmp_path: Path) -> None:
    """Each uncached response must be replayed before a later checkpoint can certify."""
    trust, _cache, _runtime = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_before_failure", cache_generation=_GENERATION)
    assert await trust.prepare_startup() == "s_before_failure"

    first_failure = await trust.certify_response(
        next_batch="s1",
        cache_result=SyncCacheWriteResult(
            complete=False,
            errors=(RuntimeError("first cache write failed"),),
        ),
    )
    second_failure = await trust.certify_response(
        next_batch="s2",
        cache_result=SyncCacheWriteResult(
            complete=False,
            errors=(RuntimeError("second cache write failed"),),
        ),
    )

    assert first_failure.reset_client_token is True
    assert second_failure.reset_client_token is True
    assert trust.state is SyncTrustState.UNCERTAIN
    assert trust.checkpoint is None
    assert trust.retry_token() == "s_before_failure"
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        "s_before_failure",
        cache_generation=_GENERATION,
    )

    success = await trust.certify_response(
        next_batch="s3",
        cache_result=SyncCacheWriteResult(complete=True),
    )

    assert success.reset_client_token is False
    assert trust.state is SyncTrustState.CERTIFIED
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        token="s3",  # noqa: S106
        cache_generation=_GENERATION,
    )


@pytest.mark.asyncio
async def test_cancelled_durable_clear_cannot_resurrect_runtime_checkpoint(
    tmp_path: Path,
) -> None:
    """A completed clear must invalidate runtime before cancellation escapes."""
    trust, _cache, _runtime = _trust(tmp_path)
    trust.state = SyncTrustState.CERTIFIED
    trust.checkpoint = SyncCheckpoint("s_old")
    trust.continuity_store.replace_checkpoint(
        SyncCheckpoint("s_old", cache_generation=_GENERATION),
    )
    clear_started = threading.Event()
    release_clear = threading.Event()
    clear_checkpoint = trust.continuity_store.clear_checkpoint

    def blocking_clear() -> SyncContinuityRecord:
        clear_started.set()
        assert release_clear.wait(timeout=2)
        return clear_checkpoint()

    cache_result = SyncCacheWriteResult(complete=False)
    decision = handle_unknown_pos()
    with patch.object(
        trust.continuity_store,
        "clear_checkpoint",
        side_effect=blocking_clear,
    ):
        apply_task = asyncio.create_task(
            trust.apply_response(decision, cache_result=cache_result),
        )
        assert await asyncio.to_thread(clear_started.wait, 2)
        apply_task.cancel()
        await asyncio.sleep(0)
        apply_task.cancel()
        await asyncio.sleep(0)
        escaped_before_clear = apply_task.done()
        release_clear.set()

        with pytest.raises(asyncio.CancelledError):
            await apply_task

    assert not escaped_before_clear
    assert trust.state is SyncTrustState.UNCERTAIN
    assert trust.checkpoint is None
    assert trust.retry_token() is None
    assert load_sync_checkpoint(tmp_path, "code") is None


@pytest.mark.asyncio
async def test_cancelled_certification_publishes_committed_checkpoint_before_escape(
    tmp_path: Path,
) -> None:
    """Cancellation must not split committed durability from runtime publication."""
    trust, _cache, _runtime = _trust(tmp_path)
    cache_result = SyncCacheWriteResult(complete=True)
    decision = trust.plan_response(
        next_batch="s_committed",
        cache_result=cache_result,
    )
    write_started = threading.Event()
    release_write = threading.Event()
    accept_classic_response = trust.continuity_store.accept_classic_response

    def blocking_accept(
        checkpoint: SyncCheckpoint,
        *,
        joined_room_ids: object,
    ) -> SyncContinuityRecord:
        write_started.set()
        assert release_write.wait(timeout=2)
        return accept_classic_response(
            checkpoint,
            joined_room_ids=joined_room_ids,  # type: ignore[arg-type]
        )

    with patch.object(
        trust.continuity_store,
        "accept_classic_response",
        side_effect=blocking_accept,
    ):
        apply_task = asyncio.create_task(
            trust.apply_response(decision, cache_result=cache_result),
        )
        assert await asyncio.to_thread(write_started.wait, 2)
        apply_task.cancel()
        await asyncio.sleep(0)
        apply_task.cancel()
        release_write.set()

        with pytest.raises(asyncio.CancelledError):
            await apply_task

    assert trust.state is SyncTrustState.CERTIFIED
    assert trust.checkpoint == SyncCheckpoint("s_committed")
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        "s_committed",
        cache_generation=_GENERATION,
    )


@pytest.mark.asyncio
async def test_clear_transforms_fresh_store_state_when_cached_checkpoint_is_absent(
    tmp_path: Path,
) -> None:
    """A concurrent checkpoint cannot survive a locally required invalidation."""
    trust, _cache, _runtime = _trust(tmp_path)
    SyncContinuityStore(tmp_path, "code").replace_checkpoint(
        SyncCheckpoint("s_concurrent", cache_generation=_GENERATION),
    )
    cache_result = SyncCacheWriteResult(complete=False)
    decision = handle_unknown_pos()

    await trust.apply_response(decision, cache_result=cache_result)

    assert load_sync_checkpoint(tmp_path, "code") is None


@pytest.mark.asyncio
async def test_limited_recovery_keeps_rewinding_until_complete_delta_certifies(tmp_path: Path) -> None:
    """Each unresolved recovery window must replay until a complete delta certifies."""
    trust, _cache, _runtime = _trust(tmp_path)
    trust.state = SyncTrustState.CERTIFIED

    positioned = await trust.certify_response(
        next_batch="s_partial",
        cache_result=SyncCacheWriteResult(
            complete=False,
            limited_room_ids=("!room:localhost",),
        ),
    )
    initial = await trust.certify_response(
        next_batch="s_initial",
        cache_result=SyncCacheWriteResult(
            complete=False,
            limited_room_ids=("!room:localhost",),
        ),
    )
    complete = await trust.certify_response(
        next_batch="s_complete",
        cache_result=SyncCacheWriteResult(complete=True),
    )

    assert positioned.reset_client_token is True
    assert initial.reset_client_token is True
    assert initial.state is SyncTrustState.UNCERTAIN
    assert complete.state is SyncTrustState.CERTIFIED
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        token="s_complete",  # noqa: S106
        cache_generation=_GENERATION,
    )


@pytest.mark.asyncio
async def test_sustained_limited_responses_keep_rewinding_until_a_delta_certifies(tmp_path: Path) -> None:
    """Back-to-back unresolved gaps must not donate an incremental cursor."""
    trust, _cache, _runtime = _trust(tmp_path)
    trust.state = SyncTrustState.CERTIFIED

    decisions = [
        await trust.certify_response(
            next_batch=f"s_partial_{index}",
            cache_result=SyncCacheWriteResult(
                complete=False,
                limited_room_ids=("!room:localhost",),
            ),
        )
        for index in range(4)
    ]

    assert all(decision.reset_client_token for decision in decisions)


@pytest.mark.asyncio
async def test_cold_limited_initial_window_rewinds_until_certified(tmp_path: Path) -> None:
    """A cold response cannot donate a cursor until its cache result certifies."""
    trust, _cache, _runtime = _trust(tmp_path)

    assert await trust.prepare_startup() is None
    decision = await trust.certify_response(
        next_batch="s_initial",
        cache_result=SyncCacheWriteResult(
            complete=False,
            limited_room_ids=("!room:localhost",),
        ),
    )

    assert decision.reset_client_token is True
    assert trust.state is SyncTrustState.UNCERTAIN


@pytest.mark.asyncio
async def test_unknown_position_limited_window_keeps_rewinding_until_certified(tmp_path: Path) -> None:
    """M_UNKNOWN_POS recovery cannot advance past an uncertified window."""
    trust, _cache, _runtime = _trust(tmp_path)

    unknown = await trust.reject_unknown_pos()
    initial = await trust.certify_response(
        next_batch="s_initial",
        cache_result=SyncCacheWriteResult(
            complete=False,
            limited_room_ids=("!room:localhost",),
        ),
    )

    assert unknown.reset_client_token is True
    assert initial.reset_client_token is True


@pytest.mark.asyncio
async def test_clear_failure_disables_cache_and_skips_cold_cleanup(tmp_path: Path) -> None:
    """Failed deletion preserves rows and disables cache use for safe replay."""
    trust, cache, _runtime = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_preserved", cache_generation=_GENERATION)

    with (
        patch.object(
            trust.continuity_store,
            "load",
            side_effect=OSError("checkpoint unreadable"),
        ),
        patch.object(
            trust.continuity_store,
            "clear_checkpoint",
            side_effect=OSError("checkpoint cannot be removed"),
        ),
    ):
        token = await trust.prepare_startup()

    assert token is None
    assert load_sync_checkpoint(tmp_path, "code") is not None
    cache.disable.assert_called_once_with("sync_checkpoint_clear_failed")
    cache.purge_principal.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_clear_failure_disables_cache(tmp_path: Path) -> None:
    """A failed unknown-position clear must make stale cache rows unusable."""
    trust, cache, _runtime = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_preserved", cache_generation=_GENERATION)
    assert await trust.prepare_startup() == "s_preserved"
    with patch.object(
        trust.continuity_store,
        "clear_checkpoint",
        side_effect=OSError("checkpoint cannot be removed"),
    ):
        applied = await trust.reject_unknown_pos()

    assert applied.state is SyncTrustState.UNCERTAIN
    assert trust.checkpoint is None
    assert load_sync_checkpoint(tmp_path, "code") is not None
    cache.disable.assert_called_once_with("sync_checkpoint_clear_failed")


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
@pytest.mark.asyncio
async def test_saved_retry_token_requires_current_generation(
    tmp_path: Path,
    cache_generation: str | None,
    saved_generation: str,
    expected: str | None,
) -> None:
    """A durable retry token is usable only with its original generation."""
    trust, _cache, _runtime = _trust(tmp_path, cache_generation=cache_generation)
    save_sync_token(tmp_path, "code", "s_saved", cache_generation=saved_generation)
    await trust.prepare_startup()

    assert trust.retry_token() == expected
