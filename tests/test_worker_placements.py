"""The session whose worker-routed calls run in a dedicated worker of its own."""

from __future__ import annotations

import asyncio
import contextvars

import pytest

from mindroom.workers.placements import DedicatedWorkerPlacement, dedicated_worker_placement, placement_for_session

_PLACEMENT = DedicatedWorkerPlacement("a" * 32, "small")


def test_placement_is_bound_only_inside_its_block_and_for_its_session() -> None:
    """A session is placed while its block runs, and no other session or block sees it."""
    assert placement_for_session("session") is None
    with dedicated_worker_placement("session", _PLACEMENT):
        assert placement_for_session("session") == _PLACEMENT
        assert placement_for_session("other") is None
        assert placement_for_session(None) is None
    assert placement_for_session("session") is None


def test_a_nested_attempt_without_a_placement_hides_the_outer_one() -> None:
    """A grandchild's attempt that asked for no worker of its own places nothing, even inside its parent's attempt."""
    with dedicated_worker_placement("session", _PLACEMENT):
        with dedicated_worker_placement("grandchild", None):
            assert placement_for_session("session") is None
            assert placement_for_session("grandchild") is None
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


@pytest.mark.asyncio
async def test_work_started_inside_the_attempt_keeps_it_and_work_started_elsewhere_does_not() -> None:
    """Calls in a copy of the attempt's context are placed; a request handled in its own context is not."""

    async def seen() -> DedicatedWorkerPlacement | None:
        return placement_for_session("session")

    with dedicated_worker_placement("session", _PLACEMENT):
        inside = await asyncio.create_task(seen())
    elsewhere = await asyncio.create_task(seen(), context=contextvars.Context())

    assert inside == _PLACEMENT
    assert elsewhere is None
