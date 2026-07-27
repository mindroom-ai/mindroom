"""Focused tests for backend-neutral Matrix event-cache semantics."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.matrix.cache.thread_cache_state import ThreadCacheGap, thread_cache_gap_row

if TYPE_CHECKING:
    from collections.abc import Sequence


@pytest.mark.parametrize(
    "values",
    [
        (),
        (1.0,),
        (1.0, "reason", 3.0),
    ],
)
def test_thread_cache_gap_row_rejects_malformed_storage_width(
    values: Sequence[float | str | None],
) -> None:
    """Storage rows must match the two-column query contract exactly."""
    with pytest.raises(ValueError, match=r"must contain exactly 2 values, got \d+"):
        thread_cache_gap_row(values)


def test_thread_cache_gap_row_treats_unmarked_row_as_absent() -> None:
    """A thread row with no marker carries no gap."""
    assert thread_cache_gap_row((None, None)) is None
    assert thread_cache_gap_row(None) is None


def test_thread_cache_gap_row_reads_a_marked_row() -> None:
    """A marked row carries its instant and reason."""
    assert thread_cache_gap_row((12.5, "limited_sync_timeline")) == ThreadCacheGap(
        gap_marked_at=12.5,
        gap_reason="limited_sync_timeline",
    )
