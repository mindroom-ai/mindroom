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
    from collections.abc import Iterator, Mapping


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


def subagent_cost_metadata(stored: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the metadata entry a run's save stores of what its turn received so far, empty for nothing received.

    With nothing received in this context, as in recovery's saves, it keeps the cost the ``stored`` metadata holds. A
    turn that resumes the run after a pause goes on from it (``resumed_subagent_cost``).
    """
    usd = received_subagent_cost_usd()
    usd = (stored or {}).get(SUBAGENT_COST_RUN_METADATA_KEY) if usd is None else usd
    return {} if usd is None else {SUBAGENT_COST_RUN_METADATA_KEY: usd}


def resumed_subagent_cost(metadata: dict[str, Any] | None) -> SubagentCost:
    """Return the holder of a turn that resumes a stored run, starting from what the run's turn had received."""
    stored = (metadata or {}).get(SUBAGENT_COST_RUN_METADATA_KEY)
    return SubagentCost(float(stored) if isinstance(stored, int | float) and not isinstance(stored, bool) else None)
