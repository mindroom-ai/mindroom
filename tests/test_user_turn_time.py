"""Tests for model-facing user-turn timestamp rendering."""

from __future__ import annotations

import pytest

from mindroom.user_turn_time import prefix_user_turn_time


def test_prefix_user_turn_time_formats_valid_timestamp() -> None:
    """Valid Matrix timestamps should render in the configured timezone."""
    assert (
        prefix_user_turn_time(
            "hello",
            timezone="America/Los_Angeles",
            timestamp_ms=1_774_019_700_000,
        )
        == "[2026-03-20 08:15 PDT] hello"
    )


@pytest.mark.parametrize(
    "timestamp_ms",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        1e20,
        -1e20,
    ],
)
def test_prefix_user_turn_time_returns_prompt_for_invalid_numeric_timestamp(timestamp_ms: float) -> None:
    """Invalid numeric timestamps should not abort prompt preparation."""
    assert prefix_user_turn_time("hello", timezone="UTC", timestamp_ms=timestamp_ms) == "hello"
