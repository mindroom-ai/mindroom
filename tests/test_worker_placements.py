"""The sessions whose worker-routed calls run in a dedicated worker of their own."""

from __future__ import annotations

import pytest

from mindroom.workers.placements import (
    DedicatedWorkerPlacement,
    dedicated_worker_placement,
    normal_worker_calls,
    placement_for_session,
)

_PLACEMENT = DedicatedWorkerPlacement("a" * 32, "small")


def test_placement_is_bound_only_inside_its_block() -> None:
    """A session is placed while its block runs and unplaced after it."""
    assert placement_for_session("session") is None
    with dedicated_worker_placement("session", _PLACEMENT):
        assert placement_for_session("session") == _PLACEMENT
        assert placement_for_session("other") is None
        assert placement_for_session(None) is None
    assert placement_for_session("session") is None


def test_nested_blocks_keep_the_placement_until_the_outer_one_exits() -> None:
    """Overlapping attempts of one session share its placement until the last of them ends."""
    with dedicated_worker_placement("session", _PLACEMENT):
        with dedicated_worker_placement("session", _PLACEMENT):
            pass
        assert placement_for_session("session") == _PLACEMENT
    assert placement_for_session("session") is None


def test_no_placement_places_nothing() -> None:
    """A block without a placement leaves the session on its normal worker."""
    with dedicated_worker_placement("session", None):
        assert placement_for_session("session") is None


def test_normal_worker_calls_see_no_placement() -> None:
    """Calls a background script makes stay on their normal workers while their subagent's session is placed."""
    with dedicated_worker_placement("session", _PLACEMENT):
        with normal_worker_calls():
            assert placement_for_session("session") is None
        assert placement_for_session("session") == _PLACEMENT


def _fail_while_placed() -> None:
    with dedicated_worker_placement("session", _PLACEMENT):
        msg = "boom"
        raise ValueError(msg)


def test_a_failing_block_unbinds_its_placement() -> None:
    """An attempt that raises still releases its session's placement."""
    with pytest.raises(ValueError, match="boom"):
        _fail_while_placed()
    assert placement_for_session("session") is None
