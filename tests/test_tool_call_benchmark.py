"""Tests for synthetic tool-call benchmark helpers."""

from __future__ import annotations

from scripts.testing.benchmark_tool_call_overhead import summarize_samples


def test_summarize_samples_reports_nearest_rank_percentiles() -> None:
    """Benchmark summaries should stay deterministic for small sample sets."""
    summary = summarize_samples([4.0, 1.0, 3.0, 2.0])

    assert summary == {
        "count": 4,
        "mean_ms": 2.5,
        "p50_ms": 2.0,
        "p95_ms": 4.0,
        "max_ms": 4.0,
    }
