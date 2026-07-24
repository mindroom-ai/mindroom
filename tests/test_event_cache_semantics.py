"""Focused tests for backend-neutral Matrix event-cache semantics."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.matrix.cache.thread_cache_state import thread_cache_state_row

if TYPE_CHECKING:
    from collections.abc import Sequence


@pytest.mark.parametrize(
    "values",
    [
        (),
        (1.0,),
        (1.0, 2.0, "reason", 3.0),
        (1.0, 2.0, "reason", 3.0, "room_reason", 4.0, 5.0),
    ],
)
def test_thread_cache_state_row_rejects_malformed_storage_width(
    values: Sequence[float | str | None],
) -> None:
    """Storage rows must match the eight-column query contract exactly."""
    with pytest.raises(ValueError, match=r"must contain exactly 8 values, got \d+"):
        thread_cache_state_row(values)


def test_thread_cache_state_row_treats_full_null_row_as_absent() -> None:
    """A complete outer-join miss remains an absent cache-state row."""
    assert thread_cache_state_row((None, None, None, None, None, 0, None, None)) is None
