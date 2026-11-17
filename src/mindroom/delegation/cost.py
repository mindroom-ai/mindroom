"""What the subagent turns whose results one agent turn received cost, collected while that turn runs.

A reply publishes it beside its own cost, and a child's settlement adds it to the child's own, so nested children
roll up into the reply that received the outermost one.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.constants import SUBAGENT_COST_RUN_METADATA_KEY

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass
class SubagentCost:
    """The priced cost of the subagent turns one turn received, None while it received none of known cost."""

    usd: float | None = None


_RECEIVING: ContextVar[SubagentCost | None] = ContextVar("received_subagent_cost", default=None)


@contextmanager
def receiving_subagent_cost(cost: SubagentCost) -> Iterator[None]:
    """Collect into ``cost`` what the subagent turns handed to the turn running in this context cost."""
    token = _RECEIVING.set(cost)
    try:
        yield
    finally:
        _RECEIVING.reset(token)


def receive_subagent_cost(usd: float | None) -> None:
    """Add a settled child turn's cost to the turn receiving its result; a child of unknown cost adds nothing."""
    cost = _RECEIVING.get()
    if cost is not None and usd is not None:
        cost.usd = (cost.usd or 0.0) + usd


def received_subagent_cost_usd() -> float | None:
    """Return what the subagent turns the turn running in this context received cost, None for none of known cost."""
    cost = _RECEIVING.get()
    return None if cost is None else cost.usd


def with_received_subagent_cost(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the metadata a run's record stores: ``metadata``, with what its turn received so far, if anything.

    A turn that resumes the run after a pause goes on from it (``resumed_subagent_cost``).
    """
    usd = received_subagent_cost_usd()
    return metadata if usd is None else {**(metadata or {}), SUBAGENT_COST_RUN_METADATA_KEY: usd}


def resumed_subagent_cost(metadata: dict[str, Any] | None) -> SubagentCost:
    """Return the holder of a turn that resumes a stored run, starting from what the run's turn had received."""
    stored = (metadata or {}).get(SUBAGENT_COST_RUN_METADATA_KEY)
    return SubagentCost(stored if isinstance(stored, float) else None)
