"""Focused tests for Matrix sync-checkpoint certification and continuity ownership."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from mindroom.matrix.sync_certification import (
    SyncRecoveryOutcome,
    SyncTrustState,
    handle_unknown_pos,
)
from mindroom.matrix.sync_checkpoint_trust import SyncCheckpointTrust
from mindroom.matrix.sync_continuity import SyncContinuityRecord, SyncContinuityStore
from mindroom.matrix.sync_token_values import SyncCheckpoint
from tests.sync_continuity_helpers import (
    RecordedHistoryRecoveries,
    certify_response,
    load_sync_checkpoint,
    save_sync_token,
)

if TYPE_CHECKING:
    from pathlib import Path


_GENERATION = "store-generation"


def _trust(
    tmp_path: Path,
    *,
    store_generation: str | None = _GENERATION,
) -> SyncCheckpointTrust:
    return SyncCheckpointTrust(
        continuity_store=SyncContinuityStore(tmp_path, "code"),
        logger=MagicMock(),
        # The event journal's identity, which the bot resolves at startup.
        store_generation=store_generation,
        history_recovery_provider=RecordedHistoryRecoveries,
    )


@pytest.mark.asyncio
async def test_matching_checkpoint_restores_without_cold_cleanup(tmp_path: Path) -> None:
    """A matching store generation restores continuity without discarding it."""
    trust = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_saved", store_generation=_GENERATION)

    token = await trust.prepare_startup()

    assert token == "s_saved"  # noqa: S105
    assert trust.state is SyncTrustState.PENDING
    assert trust.checkpoint is None
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint("s_saved", store_generation=_GENERATION)


@pytest.mark.asyncio
async def test_startup_uses_only_the_journal_certified_checkpoint(tmp_path: Path) -> None:
    """MindRoom continuity is the sole durable Classic startup position."""
    trust = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_safe", store_generation=_GENERATION)

    token = await trust.prepare_startup()
    decision = await certify_response(
        trust,
        next_batch="s_after",
        recovery=SyncRecoveryOutcome(),
    )

    assert token == "s_safe"  # noqa: S105
    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.reset_client_token is False


@pytest.mark.asyncio
async def test_retry_token_uses_loaded_checkpoint_without_disk_reads(tmp_path: Path) -> None:
    """Pre-certification retries reuse startup state instead of loading per response."""
    trust = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_saved", store_generation=_GENERATION)
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
    trust = _trust(tmp_path)
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
@pytest.mark.parametrize("store_generation", [None, "replacement-generation"])
async def test_unverifiable_checkpoint_clears_and_starts_cold(
    tmp_path: Path,
    store_generation: str | None,
) -> None:
    """Missing or changed store generations invalidate saved continuity."""
    trust = _trust(tmp_path, store_generation=store_generation)
    save_sync_token(tmp_path, "code", "s_stale", store_generation=_GENERATION)

    token = await trust.prepare_startup()

    assert token is None
    assert trust.state is SyncTrustState.COLD
    assert load_sync_checkpoint(tmp_path, "code") is None


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
    trust = _trust(tmp_path)

    assert await trust.prepare_startup() is None

    assert trust.state is SyncTrustState.COLD
    assert trust.continuity_store.load() == SyncContinuityRecord(revision=1)
    invalid_log = next(
        call for call in trust.logger.warning.call_args_list if call.args == ("matrix_sync_continuity_invalid",)
    )
    assert "Invalid Matrix sync continuity record" in invalid_log.kwargs["error"]


@pytest.mark.asyncio
async def test_complete_recovery_certifies_raw_sync_continuity(tmp_path: Path) -> None:
    """Exact callback recovery must not poison independently durable raw sync continuity."""
    trust = _trust(tmp_path)

    decision = await certify_response(
        trust,
        next_batch="s_complete",
        recovery=SyncRecoveryOutcome(),
    )

    assert decision.state is SyncTrustState.CERTIFIED
    assert trust.state is SyncTrustState.CERTIFIED
    assert trust.checkpoint == SyncCheckpoint("s_complete")
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        token="s_complete",  # noqa: S106
        store_generation=_GENERATION,
    )


@pytest.mark.asyncio
async def test_planned_response_does_not_advance_checkpoint_until_applied(tmp_path: Path) -> None:
    """Callers may finish prerequisite durable work before certifying a sync position."""
    trust = _trust(tmp_path)

    decision = trust.plan_response(
        next_batch="s_planned",
        recovery=SyncRecoveryOutcome(),
    )

    assert decision.checkpoint_to_save == SyncCheckpoint("s_planned")
    assert trust.state is SyncTrustState.COLD
    assert trust.checkpoint is None
    assert load_sync_checkpoint(tmp_path, "code") is None

    await trust.apply_response(decision, recovery=SyncRecoveryOutcome())

    assert trust.state is SyncTrustState.CERTIFIED
    assert trust.checkpoint == SyncCheckpoint("s_planned")


def test_dispatch_persist_failure_is_consumed_once_per_epoch(tmp_path: Path) -> None:
    """Each new admission failure rejects certification exactly once."""
    trust = _trust(tmp_path)

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
    trust = _trust(tmp_path)
    trust.record_dispatch_persist_failure()

    trust.acknowledge_dispatch_persist_failures()

    assert not trust.consume_dispatch_persist_failure()


@pytest.mark.asyncio
async def test_shutdown_persist_without_store_generation_clears_saved_checkpoint(
    tmp_path: Path,
) -> None:
    """Disabled cache generation must leave restart cold without masking shutdown."""
    trust = _trust(tmp_path)
    trust.state = SyncTrustState.CERTIFIED
    trust.checkpoint = SyncCheckpoint("s_runtime")
    trust.continuity_store.replace_checkpoint(
        SyncCheckpoint("s_saved", store_generation=_GENERATION),
    )
    # The store lost its identity mid-run; a checkpoint can no longer be certified.
    trust.store_generation = None

    await trust.persist_current()

    assert load_sync_checkpoint(tmp_path, "code") is None
    trust.logger.warning.assert_any_call(
        "matrix_sync_checkpoint_skipped_without_store_generation",
    )


@pytest.mark.asyncio
async def test_a_refused_response_resets_live_cursor_and_preserves_checkpoint(tmp_path: Path) -> None:
    """A response nobody took ownership of must replay from the last durable checkpoint."""
    trust = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_before_gap", store_generation=_GENERATION)
    assert await trust.prepare_startup() == "s_before_gap"

    decision = await certify_response(
        trust,
        next_batch="s_partial",
        recovery=SyncRecoveryOutcome(
            admission_refused=True,
        ),
    )

    assert decision.reset_client_token is True
    assert decision.reason == "admission_refused"
    assert trust.state is SyncTrustState.UNCERTAIN
    assert trust.checkpoint is None
    assert trust.retry_token() == "s_before_gap"
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        "s_before_gap",
        store_generation=_GENERATION,
    )


@pytest.mark.asyncio
async def test_unrecovered_outcome_rewinds_before_clean_replay(tmp_path: Path) -> None:
    """An unrecovered in-memory gap never advances the durable checkpoint."""
    trust = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_before_gap", store_generation=_GENERATION)
    assert await trust.prepare_startup() == "s_before_gap"

    unrecovered = await certify_response(
        trust,
        next_batch="s_unrecovered",
        recovery=SyncRecoveryOutcome(
            unrecovered_room_ids=frozenset({"!room:localhost"}),
        ),
    )
    replayed = await certify_response(
        trust,
        next_batch="s_unclassified",
        recovery=SyncRecoveryOutcome(),
    )
    assert unrecovered.reset_client_token is True
    assert replayed.reset_client_token is False
    assert trust.retry_token() == "s_unclassified"
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        "s_unclassified",
        store_generation=_GENERATION,
    )


@pytest.mark.asyncio
async def test_repeated_unrecovered_gap_keeps_rewinding(
    tmp_path: Path,
) -> None:
    """Every failed transient recovery retries from the same safe continuity."""
    trust = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_before_gap", store_generation=_GENERATION)
    assert await trust.prepare_startup() == "s_before_gap"
    gap_result = SyncRecoveryOutcome(
        unrecovered_room_ids=frozenset({"!room:localhost"}),
    )

    first = await certify_response(
        trust,
        next_batch="s_after_gap_1",
        recovery=gap_result,
    )
    second = await certify_response(
        trust,
        next_batch="s_after_gap_2",
        recovery=gap_result,
    )
    clean = await certify_response(
        trust,
        next_batch="s_after_clean_delta",
        recovery=SyncRecoveryOutcome(),
    )

    assert first.reset_client_token is True
    assert second.reset_client_token is True
    assert clean.reset_client_token is False
    assert clean.state is SyncTrustState.CERTIFIED
    assert clean.reason is None
    assert trust.retry_token() == "s_after_clean_delta"
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        "s_after_clean_delta",
        store_generation=_GENERATION,
    )


@pytest.mark.asyncio
async def test_recovered_outcome_settles_prior_gap_and_certifies(tmp_path: Path) -> None:
    """A later positive NIO outcome must settle exact recovery debt."""
    trust = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_before_gap", store_generation=_GENERATION)
    assert await trust.prepare_startup() == "s_before_gap"
    room_id = "!room:localhost"
    await certify_response(
        trust,
        next_batch="s_after_gap",
        recovery=SyncRecoveryOutcome(
            unrecovered_room_ids=frozenset({room_id}),
        ),
    )

    recovered = await certify_response(
        trust,
        next_batch="s_after_recovery",
        recovery=SyncRecoveryOutcome(
            recovered_room_ids=frozenset({room_id}),
        ),
    )

    assert recovered.state is SyncTrustState.CERTIFIED
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        "s_after_recovery",
        store_generation=_GENERATION,
    )


@pytest.mark.asyncio
async def test_consecutive_cache_failures_keep_rewinding_until_certified(tmp_path: Path) -> None:
    """Each uncached response must be replayed before a later checkpoint can certify."""
    trust = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_before_failure", store_generation=_GENERATION)
    assert await trust.prepare_startup() == "s_before_failure"

    first_failure = await certify_response(
        trust,
        next_batch="s1",
        recovery=SyncRecoveryOutcome(
            admission_refused=True,
        ),
    )
    second_failure = await certify_response(
        trust,
        next_batch="s2",
        recovery=SyncRecoveryOutcome(
            admission_refused=True,
        ),
    )

    assert first_failure.reset_client_token is True
    assert second_failure.reset_client_token is True
    assert trust.state is SyncTrustState.UNCERTAIN
    assert trust.checkpoint is None
    assert trust.retry_token() == "s_before_failure"
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        "s_before_failure",
        store_generation=_GENERATION,
    )

    success = await certify_response(
        trust,
        next_batch="s3",
        recovery=SyncRecoveryOutcome(),
    )

    assert success.reset_client_token is False
    assert trust.state is SyncTrustState.CERTIFIED
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        token="s3",  # noqa: S106
        store_generation=_GENERATION,
    )


@pytest.mark.asyncio
async def test_cancelled_durable_clear_cannot_resurrect_runtime_checkpoint(
    tmp_path: Path,
) -> None:
    """A completed clear must invalidate runtime before cancellation escapes."""
    trust = _trust(tmp_path)
    trust.state = SyncTrustState.CERTIFIED
    trust.checkpoint = SyncCheckpoint("s_old")
    trust.continuity_store.replace_checkpoint(
        SyncCheckpoint("s_old", store_generation=_GENERATION),
    )
    clear_started = threading.Event()
    release_clear = threading.Event()
    clear_checkpoint = trust.continuity_store.clear_checkpoint

    def blocking_clear() -> SyncContinuityRecord:
        clear_started.set()
        assert release_clear.wait(timeout=2)
        return clear_checkpoint()

    recovery = SyncRecoveryOutcome(admission_refused=True)
    decision = handle_unknown_pos()
    with patch.object(
        trust.continuity_store,
        "clear_checkpoint",
        side_effect=blocking_clear,
    ):
        apply_task = asyncio.create_task(
            trust.apply_response(decision, recovery=recovery),
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
    trust = _trust(tmp_path)
    recovery = SyncRecoveryOutcome()
    decision = trust.plan_response(
        next_batch="s_committed",
        recovery=recovery,
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
            trust.apply_response(decision, recovery=recovery),
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
        store_generation=_GENERATION,
    )


@pytest.mark.asyncio
async def test_clear_transforms_fresh_store_state_when_cached_checkpoint_is_absent(
    tmp_path: Path,
) -> None:
    """A concurrent checkpoint cannot survive a locally required invalidation."""
    trust = _trust(tmp_path)
    SyncContinuityStore(tmp_path, "code").replace_checkpoint(
        SyncCheckpoint("s_concurrent", store_generation=_GENERATION),
    )
    recovery = SyncRecoveryOutcome(admission_refused=True)
    decision = handle_unknown_pos()

    await trust.apply_response(decision, recovery=recovery)

    assert load_sync_checkpoint(tmp_path, "code") is None


@pytest.mark.asyncio
async def test_limited_recovery_keeps_rewinding_until_complete_delta_certifies(tmp_path: Path) -> None:
    """Each unresolved recovery window must replay until a complete delta certifies."""
    trust = _trust(tmp_path)
    trust.state = SyncTrustState.CERTIFIED

    positioned = await certify_response(
        trust,
        next_batch="s_partial",
        recovery=SyncRecoveryOutcome(
            admission_refused=True,
        ),
    )
    initial = await certify_response(
        trust,
        next_batch="s_initial",
        recovery=SyncRecoveryOutcome(
            admission_refused=True,
        ),
    )
    complete = await certify_response(
        trust,
        next_batch="s_complete",
        recovery=SyncRecoveryOutcome(),
    )

    assert positioned.reset_client_token is True
    assert initial.reset_client_token is True
    assert initial.state is SyncTrustState.UNCERTAIN
    assert complete.state is SyncTrustState.CERTIFIED
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        token="s_complete",  # noqa: S106
        store_generation=_GENERATION,
    )


@pytest.mark.asyncio
async def test_sustained_limited_responses_keep_rewinding_until_a_delta_certifies(tmp_path: Path) -> None:
    """Back-to-back unresolved gaps must not donate an incremental cursor."""
    trust = _trust(tmp_path)
    trust.state = SyncTrustState.CERTIFIED

    decisions = [
        await certify_response(
            trust,
            next_batch=f"s_partial_{index}",
            recovery=SyncRecoveryOutcome(
                admission_refused=True,
            ),
        )
        for index in range(4)
    ]

    assert all(decision.reset_client_token for decision in decisions)


@pytest.mark.asyncio
async def test_cold_limited_initial_window_rewinds_until_certified(tmp_path: Path) -> None:
    """A cold response cannot donate a cursor until its cache result certifies."""
    trust = _trust(tmp_path)

    assert await trust.prepare_startup() is None
    decision = await certify_response(
        trust,
        next_batch="s_initial",
        recovery=SyncRecoveryOutcome(
            admission_refused=True,
        ),
    )

    assert decision.reset_client_token is True
    assert trust.state is SyncTrustState.UNCERTAIN


@pytest.mark.asyncio
async def test_unknown_position_limited_window_keeps_rewinding_until_certified(tmp_path: Path) -> None:
    """M_UNKNOWN_POS recovery cannot advance past an uncertified window."""
    trust = _trust(tmp_path)

    unknown = await trust.reject_unknown_pos()
    initial = await certify_response(
        trust,
        next_batch="s_initial",
        recovery=SyncRecoveryOutcome(
            admission_refused=True,
        ),
    )

    assert unknown.reset_client_token is True
    assert initial.reset_client_token is True


@pytest.mark.asyncio
async def test_clear_failure_disables_cache_and_skips_cold_cleanup(tmp_path: Path) -> None:
    """Failed deletion preserves rows and disables cache use for safe replay."""
    trust = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_preserved", store_generation=_GENERATION)

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
    # The runtime is cold either way: the load failed, so nothing was restored,
    # and the checkpoint left on disk is one no store generation vouches for.
    assert trust.state is SyncTrustState.COLD
    assert trust.retry_token() is None


@pytest.mark.asyncio
async def test_response_clear_failure_leaves_no_replayable_checkpoint(tmp_path: Path) -> None:
    """A failed unknown-position clear must not leave a checkpoint this run can replay."""
    trust = _trust(tmp_path)
    save_sync_token(tmp_path, "code", "s_preserved", store_generation=_GENERATION)
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
    assert trust.retry_token() is None


def test_retry_token_prefers_current_certified_checkpoint(tmp_path: Path) -> None:
    """An in-memory certified checkpoint is the first replay choice."""
    trust = _trust(tmp_path)
    trust.checkpoint = SyncCheckpoint("s_current")
    save_sync_token(tmp_path, "code", "s_saved", store_generation=_GENERATION)

    assert trust.retry_token() == "s_current"


@pytest.mark.parametrize(
    ("store_generation", "saved_generation", "expected"),
    [
        (_GENERATION, _GENERATION, "s_saved"),
        ("replacement-generation", _GENERATION, None),
        (None, _GENERATION, None),
    ],
)
@pytest.mark.asyncio
async def test_saved_retry_token_requires_current_generation(
    tmp_path: Path,
    store_generation: str | None,
    saved_generation: str,
    expected: str | None,
) -> None:
    """A durable retry token is usable only with its original generation."""
    trust = _trust(tmp_path, store_generation=store_generation)
    save_sync_token(tmp_path, "code", "s_saved", store_generation=saved_generation)
    await trust.prepare_startup()

    assert trust.retry_token() == expected
