"""Focused tests for backend-neutral Matrix event-cache semantics."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.matrix.cache import ThreadRevision
from mindroom.matrix.cache.thread_cache_state import thread_cache_state_row, thread_revision_row

if TYPE_CHECKING:
    from collections.abc import Sequence


@pytest.mark.parametrize(
    "values",
    [
        (),
        (1.0,),
        (1.0, 2.0, "reason", 3.0),
        (1.0, 2.0, "reason", 3.0, "room_reason", 4.0),
    ],
)
def test_thread_cache_state_row_rejects_malformed_storage_width(
    values: Sequence[float | str | None],
) -> None:
    """Storage rows must match the five-column query contract exactly."""
    with pytest.raises(ValueError, match=r"must contain exactly 5 values, got \d+"):
        thread_cache_state_row(values)


def test_thread_cache_state_row_treats_full_null_row_as_absent() -> None:
    """A complete outer-join miss remains an absent cache-state row."""
    assert thread_cache_state_row((None, None, None, None, None)) is None


@pytest.mark.parametrize("values", [(), (1,), (1, 2, 3), (1, 2, 3, 4, 5)])
def test_thread_revision_row_rejects_malformed_storage_width(
    values: Sequence[float | int | None],
) -> None:
    """Aggregate rows must match the four-column revision query contract exactly."""
    with pytest.raises(ValueError, match=r"must contain exactly 4 values, got \d+"):
        thread_revision_row(values)


@pytest.mark.parametrize("values", [None, (0, None, None, None), (1, None, 2, 3)])
def test_thread_revision_row_treats_empty_thread_as_absent(
    values: Sequence[float | int | None] | None,
) -> None:
    """Empty or partially aggregated threads never produce a revision."""
    assert thread_revision_row(values) is None


def test_thread_revision_row_normalizes_backend_values() -> None:
    """Backend numeric values normalize into one integer revision."""
    assert thread_revision_row((3, 7, 9, 1000)) == ThreadRevision(
        event_count=3,
        max_write_seq=7,
        max_thread_write_seq=9,
        max_origin_server_ts=1000,
    )
