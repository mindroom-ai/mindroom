"""Tests for escaping a Classic-sync rebuild that cannot converge.

A room nio cannot recover fails certification, the cursor rewinds to the last
certified checkpoint, and the next attempt measures the same gap against a live
position that has moved on. These tests pin that the loop now terminates, and —
just as importantly — that it only terminates when the checkpoint really is
stuck, so a recovery that is merely slow is never cut short.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import nio
import pytest
from structlog.testing import capture_logs

from mindroom.logging_config import get_logger
from mindroom.matrix.sync_certification import SyncRecoveryOutcome, SyncTrustState
from mindroom.matrix.sync_checkpoint_trust import SyncCheckpointTrust
from mindroom.matrix.sync_continuity import SyncContinuityStore
from mindroom.matrix.sync_recovery_escape import (
    _CLASSIC_SYNC_RECOVERY_STALL_LIMIT,
    SkippedRecoveryGap,
    SyncRecoveryStallTracker,
)
from tests.sync_continuity_helpers import (
    RecordedHistoryRecoveries,
    certify_response,
    load_sync_checkpoint,
    save_sync_token,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.event_journal import HistoryRecoveryRecordView, RoomHistoryRecovery

_STORE_GENERATION = "sync-recovery-escape"
_WEDGED_ROOM = "!wedged:localhost"
_HEALTHY_ROOM = "!healthy:localhost"
_SKIP_LOG_EVENT = "matrix_sync_recovery_gap_skipped_after_stalled_rebuild"
_STUCK = "s_stuck"  # an opaque Matrix sync token, not a credential
_SKIPPED_TO = "s_live_now"
_REPLAYED = "s_live_replayed"
_APPLY_RETRIED = "s_apply_retried"


class _FailOnceHistoryRecoveries(RecordedHistoryRecoveries):
    """Refuse the first durable gap record, then behave normally."""

    fail_next = True

    async def record_room_history_recovery(self, room_id: str) -> RoomHistoryRecovery:
        """Leave the first proposed skip unapplied."""
        if self.fail_next:
            self.fail_next = False
            msg = "injected history-recovery persistence failure"
            raise RuntimeError(msg)
        return await super().record_room_history_recovery(room_id)


def _trust(tmp_path: Path, *, history_recovery: HistoryRecoveryRecordView | None = None) -> SyncCheckpointTrust:
    """Build one principal's real checkpoint trust over a temporary continuity store."""
    recorder = RecordedHistoryRecoveries() if history_recovery is None else history_recovery
    return SyncCheckpointTrust(
        continuity_store=SyncContinuityStore(tmp_path, "code"),
        logger=get_logger(),
        state=SyncTrustState.PENDING,
        store_generation=_STORE_GENERATION,
        history_recovery_provider=lambda: recorder,
    )


def _sync_response(*, next_batch: str, unrecovered_room_ids: frozenset[str]) -> nio.SyncResponse:
    """Build a real nio response carrying an authoritative recovery outcome."""
    return nio.SyncResponse(
        next_batch=next_batch,
        rooms=nio.Rooms(invite={}, join={}, leave={}),
        device_key_count=nio.DeviceOneTimeKeyCount(curve25519=0, signed_curve25519=0),
        device_list=nio.DeviceList(changed=[], left=[]),
        to_device_events=[],
        presence_events=[],
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=unrecovered_room_ids,
    )


def _recovery(
    response: nio.SyncResponse,
    *,
    admission_refused: bool = False,
) -> SyncRecoveryOutcome:
    """Build the recovery outcome from the exact typed upstream response."""
    return SyncRecoveryOutcome.from_sync_response(
        response,
        admission_refused=admission_refused,
    )


async def _certify_unrecovered(
    trust: SyncCheckpointTrust,
    *,
    next_batch: str,
    unrecovered_room_ids: frozenset[str],
) -> tuple[SyncTrustState, bool]:
    """Run one full sync response whose named rooms nio could not recover."""
    response = _sync_response(next_batch=next_batch, unrecovered_room_ids=unrecovered_room_ids)
    decision = await certify_response(
        trust,
        next_batch=response.next_batch,
        recovery=_recovery(response),
    )
    return decision.state, decision.reset_client_token


# --- The stall tracker in isolation -----------------------------------------


def test_repeated_failures_from_one_checkpoint_skip_the_gap() -> None:
    """A room that keeps failing from an unchanging checkpoint is given up on."""
    tracker = SyncRecoveryStallTracker()
    rooms = frozenset({_WEDGED_ROOM})

    before_limit = [
        tracker.observe(unrecovered_room_ids=rooms, checkpoint_token=_STUCK)
        for _ in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1)
    ]
    at_limit = tracker.observe(unrecovered_room_ids=rooms, checkpoint_token=_STUCK)

    assert before_limit == [()] * (_CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1)
    assert at_limit == (
        SkippedRecoveryGap(
            room_id=_WEDGED_ROOM,
            skipped_from_token=_STUCK,
            failed_attempts=_CLASSIC_SYNC_RECOVERY_STALL_LIMIT,
        ),
    )


def test_an_advancing_checkpoint_restarts_the_count_forever() -> None:
    """Failures measured from a checkpoint that keeps moving are forward progress."""
    tracker = SyncRecoveryStallTracker()
    rooms = frozenset({_WEDGED_ROOM})

    skipped = [
        tracker.observe(unrecovered_room_ids=rooms, checkpoint_token=f"s_{attempt}")
        for attempt in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT * 4)
    ]

    assert skipped == [()] * (_CLASSIC_SYNC_RECOVERY_STALL_LIMIT * 4)


def test_one_recovered_response_clears_an_accumulated_stall() -> None:
    """A room that recovers must start over rather than inherit old failures."""
    tracker = SyncRecoveryStallTracker()
    rooms = frozenset({_WEDGED_ROOM})

    for _ in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1):
        assert tracker.observe(unrecovered_room_ids=rooms, checkpoint_token=_STUCK) == ()
    assert tracker.observe(unrecovered_room_ids=frozenset(), checkpoint_token=_STUCK) == ()
    after_recovery = tracker.observe(unrecovered_room_ids=rooms, checkpoint_token=_STUCK)

    assert after_recovery == ()


def test_a_wedged_room_never_skips_a_room_that_only_just_failed() -> None:
    """Stalls are counted per room, so one wedged room cannot skip another's history."""
    tracker = SyncRecoveryStallTracker()

    for _ in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1):
        tracker.observe(unrecovered_room_ids=frozenset({_WEDGED_ROOM}), checkpoint_token=_STUCK)
    skipped = tracker.observe(
        unrecovered_room_ids=frozenset({_WEDGED_ROOM, _HEALTHY_ROOM}),
        checkpoint_token=_STUCK,
    )

    assert [gap.room_id for gap in skipped] == [_WEDGED_ROOM]


def test_skip_eligibility_survives_until_every_unrecovered_room_is_eligible() -> None:
    """Offset rooms must converge instead of alternately losing their threshold."""
    tracker = SyncRecoveryStallTracker()
    for _ in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1):
        tracker.observe(unrecovered_room_ids=frozenset({_WEDGED_ROOM}), checkpoint_token=_STUCK)

    both = frozenset({_WEDGED_ROOM, _HEALTHY_ROOM})
    skipped_room_ids = [
        frozenset(gap.room_id for gap in tracker.observe(unrecovered_room_ids=both, checkpoint_token=_STUCK))
        for _ in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT)
    ]

    assert skipped_room_ids == [
        frozenset({_WEDGED_ROOM}),
        frozenset({_WEDGED_ROOM}),
        both,
    ]


# --- The escape through real cache trust ------------------------------------


@pytest.mark.asyncio
async def test_an_unconvergent_rebuild_escapes_and_certifies_forward(tmp_path: Path) -> None:
    """Repeated failures from one checkpoint stop rewinding and move the cursor on."""
    save_sync_token(tmp_path, "code", _STUCK, store_generation=_STORE_GENERATION)
    trust = _trust(tmp_path)
    assert await trust.prepare_startup() == _STUCK

    outcomes = [
        await _certify_unrecovered(
            trust,
            next_batch=f"s_live_{attempt}",
            unrecovered_room_ids=frozenset({_WEDGED_ROOM}),
        )
        for attempt in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT)
    ]

    assert outcomes[:-1] == [(SyncTrustState.UNCERTAIN, True)] * (_CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1)
    assert outcomes[-1] == (SyncTrustState.CERTIFIED, True)
    checkpoint = load_sync_checkpoint(tmp_path, "code")
    assert checkpoint is not None
    assert checkpoint.token == f"s_live_{_CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1}"
    assert trust.retry_token() == f"s_live_{_CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1}"


@pytest.mark.asyncio
async def test_classic_escape_records_one_unknown_room_obligation(tmp_path: Path) -> None:
    """Classic records only the room after its bounded same-checkpoint stall proof."""
    recorder = RecordedHistoryRecoveries()
    save_sync_token(tmp_path, "code", _STUCK, store_generation=_STORE_GENERATION)
    trust = _trust(tmp_path, history_recovery=recorder)
    assert await trust.prepare_startup() == _STUCK

    for attempt in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT):
        await _certify_unrecovered(
            trust,
            next_batch=f"s_live_{attempt}",
            unrecovered_room_ids=frozenset({_WEDGED_ROOM}),
        )

    assert recorder.rooms == [_WEDGED_ROOM]


@pytest.mark.asyncio
async def test_a_permanently_wedged_room_keeps_advancing_the_watermark(tmp_path: Path) -> None:
    """A room that can never be rebuilt must not freeze the checkpoint forever.

    One escape only proves the first skip lands. The freeze this guards against
    is the cycle after it: the skip certifies a checkpoint, the client restarts
    from it, the same room fails again, and the principal must keep moving. So
    this drives many rounds and asserts the durable token advanced every time,
    by value, rather than that any single response was certified.
    """
    save_sync_token(tmp_path, "code", _STUCK, store_generation=_STORE_GENERATION)
    trust = _trust(tmp_path)
    assert await trust.prepare_startup() == _STUCK

    rounds = 4
    certified_tokens = []
    for attempt in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT * rounds):
        state, _reset = await _certify_unrecovered(
            trust,
            next_batch=f"s_live_{attempt}",
            unrecovered_room_ids=frozenset({_WEDGED_ROOM}),
        )
        if state is SyncTrustState.CERTIFIED:
            durable = load_sync_checkpoint(tmp_path, "code")
            assert durable is not None
            certified_tokens.append(durable.token)

    # One escape per full stall window, each from the checkpoint the previous
    # escape established, so the watermark never stops moving.
    assert certified_tokens == [
        f"s_live_{window * _CLASSIC_SYNC_RECOVERY_STALL_LIMIT + _CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1}"
        for window in range(rounds)
    ]
    assert trust.retry_token() == certified_tokens[-1]


@pytest.mark.asyncio
async def test_a_checkpoint_that_advances_between_failures_never_escapes(tmp_path: Path) -> None:
    """A rebuild whose checkpoint keeps moving is progress and must never skip history."""
    save_sync_token(tmp_path, "code", "s_start", store_generation=_STORE_GENERATION)
    trust = _trust(tmp_path)
    assert await trust.prepare_startup() == "s_start"

    outcomes = []
    for attempt in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT * 4):
        outcomes.append(
            await _certify_unrecovered(
                trust,
                next_batch=f"s_live_{attempt}",
                unrecovered_room_ids=frozenset({_WEDGED_ROOM}),
            ),
        )
        # A later response closes every gap, so the checkpoint advances and the
        # next failure is measured from a position the room has moved past.
        clean = _sync_response(next_batch=f"s_clean_{attempt}", unrecovered_room_ids=frozenset())
        certified = await certify_response(
            trust,
            next_batch=clean.next_batch,
            recovery=_recovery(clean),
        )
        assert certified.state is SyncTrustState.CERTIFIED

    assert outcomes == [(SyncTrustState.UNCERTAIN, True)] * (_CLASSIC_SYNC_RECOVERY_STALL_LIMIT * 4)


@pytest.mark.asyncio
async def test_a_wedged_room_does_not_make_a_healthy_room_skip(tmp_path: Path) -> None:
    """The escape names only the room that stalled, never one that just failed once."""
    save_sync_token(tmp_path, "code", _STUCK, store_generation=_STORE_GENERATION)
    trust = _trust(tmp_path)
    assert await trust.prepare_startup() == _STUCK
    for attempt in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1):
        await _certify_unrecovered(
            trust,
            next_batch=f"s_live_{attempt}",
            unrecovered_room_ids=frozenset({_WEDGED_ROOM}),
        )

    both = _sync_response(
        next_batch="s_live_both",
        unrecovered_room_ids=frozenset({_WEDGED_ROOM, _HEALTHY_ROOM}),
    )
    with capture_logs() as logs:
        decision = await certify_response(
            trust,
            next_batch=both.next_batch,
            recovery=_recovery(both),
        )

    # The healthy room's single failure still blocks the checkpoint outright.
    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.reason == "sync_recovery_incomplete"
    assert [entry["event"] for entry in logs].count(_SKIP_LOG_EVENT) == 0
    assert load_sync_checkpoint(tmp_path, "code") is not None
    assert trust.retry_token() == _STUCK


@pytest.mark.asyncio
async def test_offset_wedged_rooms_eventually_advance_together(tmp_path: Path) -> None:
    """A room already eligible to skip must wait for, then advance with, its peer."""
    recorder = RecordedHistoryRecoveries()
    save_sync_token(tmp_path, "code", _STUCK, store_generation=_STORE_GENERATION)
    trust = _trust(tmp_path, history_recovery=recorder)
    assert await trust.prepare_startup() == _STUCK
    for attempt in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1):
        await _certify_unrecovered(
            trust,
            next_batch=f"s_first_{attempt}",
            unrecovered_room_ids=frozenset({_WEDGED_ROOM}),
        )

    both = frozenset({_WEDGED_ROOM, _HEALTHY_ROOM})
    outcomes = [
        await _certify_unrecovered(
            trust,
            next_batch=f"s_both_{attempt}",
            unrecovered_room_ids=both,
        )
        for attempt in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT)
    ]

    assert outcomes[:-1] == [(SyncTrustState.UNCERTAIN, True)] * (_CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1)
    assert outcomes[-1] == (SyncTrustState.CERTIFIED, True)
    assert recorder.rooms == sorted(both)
    checkpoint = load_sync_checkpoint(tmp_path, "code")
    assert checkpoint is not None
    assert checkpoint.token == f"s_both_{_CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1}"


@pytest.mark.asyncio
async def test_skip_eligibility_survives_a_failed_durable_apply(tmp_path: Path) -> None:
    """Record-before-checkpoint failure must retry the same eligible rooms."""
    recorder = _FailOnceHistoryRecoveries()
    save_sync_token(tmp_path, "code", _STUCK, store_generation=_STORE_GENERATION)
    trust = _trust(tmp_path, history_recovery=recorder)
    assert await trust.prepare_startup() == _STUCK
    for attempt in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1):
        await _certify_unrecovered(
            trust,
            next_batch=f"s_first_{attempt}",
            unrecovered_room_ids=frozenset({_WEDGED_ROOM}),
        )
    both = frozenset({_WEDGED_ROOM, _HEALTHY_ROOM})
    for attempt in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1):
        await _certify_unrecovered(
            trust,
            next_batch=f"s_both_{attempt}",
            unrecovered_room_ids=both,
        )

    with pytest.raises(RuntimeError, match="injected history-recovery persistence failure"):
        await _certify_unrecovered(
            trust,
            next_batch="s_apply_failed",
            unrecovered_room_ids=both,
        )

    checkpoint = load_sync_checkpoint(tmp_path, "code")
    assert checkpoint is not None
    assert checkpoint.token == _STUCK
    outcome = await _certify_unrecovered(
        trust,
        next_batch=_APPLY_RETRIED,
        unrecovered_room_ids=both,
    )

    assert outcome == (SyncTrustState.CERTIFIED, True)
    assert recorder.rooms == sorted(both)
    checkpoint = load_sync_checkpoint(tmp_path, "code")
    assert checkpoint is not None
    assert checkpoint.token == _APPLY_RETRIED


@pytest.mark.asyncio
async def test_the_escape_logs_the_room_and_the_range_it_skipped(tmp_path: Path) -> None:
    """A silent skip is worse than the livelock, so the loss must reach operators."""
    save_sync_token(tmp_path, "code", _STUCK, store_generation=_STORE_GENERATION)
    trust = _trust(tmp_path)
    assert await trust.prepare_startup() == _STUCK
    for attempt in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1):
        await _certify_unrecovered(
            trust,
            next_batch=f"s_live_{attempt}",
            unrecovered_room_ids=frozenset({_WEDGED_ROOM}),
        )

    with capture_logs() as logs:
        await _certify_unrecovered(
            trust,
            next_batch=_SKIPPED_TO,
            unrecovered_room_ids=frozenset({_WEDGED_ROOM}),
        )

    skips = [entry for entry in logs if entry["event"] == _SKIP_LOG_EVENT]
    assert len(skips) == 1
    assert skips[0]["log_level"] == "error"
    assert skips[0]["room_id"] == _WEDGED_ROOM
    assert skips[0]["skipped_from_token"] == _STUCK
    assert skips[0]["skipped_to_token"] == _SKIPPED_TO
    assert skips[0]["failed_attempts"] == _CLASSIC_SYNC_RECOVERY_STALL_LIMIT


@pytest.mark.asyncio
async def test_a_single_transient_failure_still_recovers_without_skipping(tmp_path: Path) -> None:
    """One unrecovered response replays and certifies normally, losing nothing."""
    save_sync_token(tmp_path, "code", _STUCK, store_generation=_STORE_GENERATION)
    trust = _trust(tmp_path)
    assert await trust.prepare_startup() == _STUCK

    with capture_logs() as logs:
        failed = await _certify_unrecovered(
            trust,
            next_batch="s_live_failed",
            unrecovered_room_ids=frozenset({_WEDGED_ROOM}),
        )
        replayed = _sync_response(next_batch=_REPLAYED, unrecovered_room_ids=frozenset())
        certified = await certify_response(
            trust,
            next_batch=replayed.next_batch,
            recovery=_recovery(replayed),
        )

    assert failed == (SyncTrustState.UNCERTAIN, True)
    assert certified.state is SyncTrustState.CERTIFIED
    assert certified.reset_client_token is False
    assert [entry["event"] for entry in logs].count(_SKIP_LOG_EVENT) == 0
    checkpoint = load_sync_checkpoint(tmp_path, "code")
    assert checkpoint is not None
    assert checkpoint.token == _REPLAYED


@pytest.mark.asyncio
async def test_a_refused_admission_never_counts_toward_a_skip(
    tmp_path: Path,
) -> None:
    """A response that never reached its recovery verdict cannot prove a stall.

    Its own certification already fails on the refused admission, so counting it
    would be invisible until a later genuine failure escaped several attempts
    early.
    """
    save_sync_token(tmp_path, "code", _STUCK, store_generation=_STORE_GENERATION)
    trust = _trust(tmp_path)
    assert await trust.prepare_startup() == _STUCK

    for attempt in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1):
        inconclusive = _sync_response(
            next_batch=f"s_live_{attempt}",
            unrecovered_room_ids=frozenset({_WEDGED_ROOM}),
        )
        decision = await certify_response(
            trust,
            next_batch=inconclusive.next_batch,
            recovery=_recovery(inconclusive, admission_refused=True),
        )
        assert decision.reason == "admission_refused"

    with capture_logs() as logs:
        conclusive = await _certify_unrecovered(
            trust,
            next_batch="s_live_conclusive",
            unrecovered_room_ids=frozenset({_WEDGED_ROOM}),
        )

    assert conclusive == (SyncTrustState.UNCERTAIN, True)
    assert [entry["event"] for entry in logs].count(_SKIP_LOG_EVENT) == 0
    assert trust.retry_token() == _STUCK
