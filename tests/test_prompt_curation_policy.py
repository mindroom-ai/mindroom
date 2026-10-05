"""Prompt-curation bounds and the guards that decide whether a pass is kept."""

from __future__ import annotations

import pytest

from mindroom.config.prompt_curation import PromptCurationConfig
from mindroom.prompt_curation.policy import (
    PassBounds,
    PassMeasurement,
    next_active,
    plan_pass,
    validate_pass,
)

SETTINGS = PromptCurationConfig(files=["MEMORY.md", "USER.md"])


def _bounds(measured: int, *, active: bool = False) -> PassBounds:
    bounds = plan_pass(measured, SETTINGS, None, active=active)
    assert bounds is not None
    return bounds


def test_no_pass_below_trigger_when_inactive() -> None:
    """Files at or under the trigger start nothing."""
    assert plan_pass(50_000, SETTINGS, None, active=False) is None
    assert next_active(50_000, SETTINGS, None, active=False) is False


@pytest.mark.parametrize(
    ("measured", "active", "upper", "floor"),
    [
        # Far above the target: cut 10-15%.
        (60_000, False, 54_000, 51_000),
        # Just above the trigger: the band still spans the configured cut.
        (51_000, False, 45_900, 43_350),
        # Still active under the trigger: only the remaining gap to the 45k stop is asked for.
        (46_000, True, 45_000, 42_700),
    ],
)
def test_pass_bounds_ask_for_a_gradual_cut(measured: int, active: bool, upper: int, floor: int) -> None:
    """Each pass is asked for a 10-15% cut, or only the gap to the stop target when that is smaller."""
    bounds = _bounds(measured, active=active)

    assert (bounds.upper_tokens, bounds.floor_tokens) == (upper, floor)
    assert bounds.stop_tokens == 45_000
    assert measured - bounds.floor_tokens <= measured * SETTINGS.max_reduction_per_pass + 1


def test_hysteresis_keeps_curating_until_under_the_stop_target() -> None:
    """Curation starts above the trigger and stays active until under the stop target."""
    assert next_active(50_001, SETTINGS, None, active=False) is True
    assert next_active(46_000, SETTINGS, None, active=True) is True
    assert next_active(44_999, SETTINGS, None, active=True) is False
    assert plan_pass(44_999, SETTINGS, None, active=True) is None


def test_context_fraction_lowers_the_trigger_only_with_a_known_window() -> None:
    """A context-window fraction lowers the trigger for small windows and is ignored without a window."""
    settings = SETTINGS.model_copy(update={"trigger_context_fraction": 0.25})

    def trigger(context_window: int | None) -> int:
        bounds = plan_pass(60_000, settings, context_window, active=False)
        assert bounds is not None
        return bounds.trigger_tokens

    assert (trigger(128_000), trigger(1_000_000), trigger(None)) == (32_000, 50_000, 50_000)
    assert plan_pass(40_000, settings, 128_000, active=False) is not None
    assert plan_pass(40_000, settings, None, active=False) is None


def _measurement(memory: int, user: int, total: int, changed: frozenset[str] = frozenset()) -> PassMeasurement:
    return PassMeasurement(
        curated={"MEMORY.md": memory, "USER.md": user},
        total_memory_tokens=total,
        changed_paths=changed,
    )


BEFORE = _measurement(50_000, 10_000, 100_000)
BOUNDS = _bounds(60_000)


def test_a_moved_cut_within_bounds_passes() -> None:
    """A cut inside the band whose detail moved to memory/ is kept."""
    after = _measurement(43_000, 10_000, 99_000, frozenset({"MEMORY.md", "memory/projects.md"}))

    assert validate_pass(BEFORE, after, BOUNDS, SETTINGS) == []


def test_partial_progress_is_kept() -> None:
    """A pass that shrinks the files but not to the target is still kept."""
    after = _measurement(48_000, 10_000, 100_000, frozenset({"MEMORY.md", "memory/projects.md"}))

    assert validate_pass(BEFORE, after, BOUNDS, SETTINGS) == []


def test_a_file_that_shrinks_too_much_is_rejected() -> None:
    """One file shrinking past the per-file cap rejects the pass."""
    after = _measurement(50_000, 7_000, 100_000, frozenset({"USER.md"}))

    assert validate_pass(BEFORE, after, BOUNDS, SETTINGS) == ["USER.md shrank 30% (more than 25%)"]


def test_a_total_below_the_floor_is_rejected() -> None:
    """A total below the pass's floor rejects it."""
    after = _measurement(39_000, 10_000, 100_000, frozenset({"MEMORY.md"}))

    assert "curated files total 49000 tokens, below the floor of 51000" in validate_pass(
        BEFORE,
        after,
        BOUNDS,
        SETTINGS,
    )


def test_a_changed_protected_or_unlisted_file_is_rejected() -> None:
    """Changing a protected file or a file outside the curatable set and memory/ rejects the pass."""
    after = _measurement(45_000, 10_000, 100_000, frozenset({"MEMORY.md", "SOUL.md", "NOTES.md"}))

    assert validate_pass(BEFORE, after, BOUNDS, SETTINGS) == [
        "protected file SOUL.md changed",
        "NOTES.md is neither curatable nor under memory/",
    ]


def test_a_pass_that_does_not_reduce_is_rejected() -> None:
    """A pass that does not shrink the curated files is rejected."""
    after = _measurement(50_000, 10_000, 102_000, frozenset({"memory/projects.md"}))

    assert validate_pass(BEFORE, after, BOUNDS, SETTINGS) == ["curated files did not shrink (60000 tokens)"]


def test_deleting_instead_of_moving_is_rejected() -> None:
    """Net loss of memory content beyond the tolerance rejects the pass."""
    after = _measurement(45_000, 10_000, 95_000, frozenset({"MEMORY.md"}))

    assert validate_pass(BEFORE, after, BOUNDS, SETTINGS) == [
        "memory content dropped 5000 tokens net (more than 3000); move detail to memory/ instead of deleting it",
    ]
