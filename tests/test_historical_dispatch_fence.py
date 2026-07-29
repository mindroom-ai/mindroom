"""Tests for historical Matrix callback admission."""

from __future__ import annotations

import math

import pytest

from mindroom.historical_dispatch_fence import HistoricalDispatchFence


def test_unarmed_fence_allows_events_without_timestamps() -> None:
    """An inactive fence leaves normal callback admission unchanged."""
    fence = HistoricalDispatchFence(startup_cutoff_ms=2_000)

    assert fence.classify({}).dispatchable


@pytest.mark.parametrize("origin_server_ts", [2_000, 2_001])
def test_armed_fence_allows_events_at_or_after_cutoff(origin_server_ts: int) -> None:
    """The startup cutoff remains inclusive."""
    fence = HistoricalDispatchFence(startup_cutoff_ms=2_000, armed=True)

    assert fence.classify({"origin_server_ts": origin_server_ts}).dispatchable


def test_armed_fence_debug_fences_events_before_cutoff() -> None:
    """Ordinary historical events are rejected at debug severity."""
    fence = HistoricalDispatchFence(startup_cutoff_ms=2_000, armed=True)

    decision = fence.classify({"origin_server_ts": 1_999})

    assert not decision.dispatchable
    assert decision.reason == "predates_startup_cutoff"
    assert decision.log_level == "debug"


def test_armed_fence_info_fences_events_without_timestamps() -> None:
    """Missing timestamps fail closed and remain observable."""
    fence = HistoricalDispatchFence(startup_cutoff_ms=2_000, armed=True)

    decision = fence.classify({})

    assert not decision.dispatchable
    assert decision.reason == "missing_origin_server_ts"
    assert decision.log_level == "info"


@pytest.mark.parametrize("origin_server_ts", [math.nan, math.inf, -math.inf])
def test_armed_fence_info_fences_nonfinite_timestamps(origin_server_ts: float) -> None:
    """Nonfinite timestamps fail closed and remain observable."""
    fence = HistoricalDispatchFence(startup_cutoff_ms=2_000, armed=True)

    decision = fence.classify({"origin_server_ts": origin_server_ts})

    assert not decision.dispatchable
    assert decision.reason == "invalid_origin_server_ts"
    assert decision.log_level == "info"


def test_rearming_preserves_construction_time_cutoff() -> None:
    """Receive-loop restarts reuse the original construction-time cutoff."""
    fence = HistoricalDispatchFence(startup_cutoff_ms=2_000, armed=True)

    fence.set_armed(armed=False)
    fence.set_armed(armed=True)

    assert fence.startup_cutoff_ms == 2_000
    assert not fence.classify({"origin_server_ts": 1_999}).dispatchable
