"""Tests for synthetic tool-call benchmark helpers."""

from __future__ import annotations

from scripts.testing.benchmark_tool_call_overhead import summarize_samples


def test_summarize_samples_reports_zeroes_for_empty_samples() -> None:
    """Empty benchmark runs should still emit a complete summary shape."""
    assert summarize_samples([]) == {
        "count": 0,
        "mean_ms": 0.0,
        "p50_ms": 0.0,
        "p95_ms": 0.0,
        "max_ms": 0.0,
    }


def test_summarize_samples_reports_single_sample_for_all_percentiles() -> None:
    """Single-sample summaries should not overrun percentile indexing."""
    assert summarize_samples([1.2345]) == {
        "count": 1,
        "mean_ms": 1.234,
        "p50_ms": 1.234,
        "p95_ms": 1.234,
        "max_ms": 1.234,
    }


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
