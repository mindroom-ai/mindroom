"""What the subagent turns whose results one agent turn received cost, collected while that turn runs.

A reply publishes it beside its own cost, and a child's settlement adds it to the child's own, so nested children
roll up into the reply that received the outermost one.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.constants import CONTINUED_RUNS_METADATA_KEY, SUBAGENT_COST_RUN_METADATA_KEY

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping


@dataclass
class SubagentCost:
    """The priced cost of the subagent turns one turn received, None while it received none of known cost.

    ``usd`` is what this attempt received, which its run's stored metadata keeps; ``prior_usd`` is what earlier
    restart-interrupted attempts at the same turn received, which only the reply's metadata adds so stored runs stay
    disjoint.
    """

    usd: float | None = None
    prior_usd: float | None = None

    @property
    def total_usd(self) -> float | None:
        """Return what earlier attempts and this one received together, or None when none of known cost arrived."""
        return sum_subagent_cost_usd((self.prior_usd, self.usd))


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
    return None if cost is None else cost.total_usd


def stored_subagent_cost_usd(metadata: Mapping[str, Any] | None) -> float | None:
    """Return the per-attempt subagent cost stored in ``metadata``, or None when absent or non-numeric."""
    stored = (metadata or {}).get(SUBAGENT_COST_RUN_METADATA_KEY)
    return float(stored) if isinstance(stored, int | float) and not isinstance(stored, bool) else None


def sum_subagent_cost_usd(costs: Iterable[float | None]) -> float | None:
    """Sum known subagent costs, returning None when every entry is None."""
    total: float | None = None
    for usd in costs:
        if usd is not None:
            total = (total or 0.0) + usd
    return total


def continued_subagent_cost_usd(
    metadata: Mapping[str, Any] | None,
    read_metadata: Callable[[str], Mapping[str, Any] | None],
) -> float | None:
    """Return the subagent cost stored on the restart-interrupted runs ``metadata`` names as continued."""
    continued = (metadata or {}).get(CONTINUED_RUNS_METADATA_KEY)
    if not isinstance(continued, list | tuple):
        return None
    return sum_subagent_cost_usd(
        stored_subagent_cost_usd(read_metadata(run_id)) for run_id in continued if isinstance(run_id, str)
    )


def subagent_cost_metadata(stored: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the metadata entry a run's save stores of what this attempt received so far, empty for nothing received.

    Only this attempt's own ``usd`` is written (never ``prior_usd`` from earlier restart-interrupted attempts), so
    stored runs stay disjoint and a later recovery can sum them without double counting. With nothing received in this
    context, as in recovery's saves, it keeps the cost the ``stored`` metadata holds. A turn that resumes the run
    after a pause goes on from it (``resumed_subagent_cost``).
    """
    cost = _RECEIVING.get()
    usd = None if cost is None else cost.usd
    usd = (stored or {}).get(SUBAGENT_COST_RUN_METADATA_KEY) if usd is None else usd
    return {} if usd is None else {SUBAGENT_COST_RUN_METADATA_KEY: usd}


def resumed_subagent_cost(metadata: dict[str, Any] | None, *, prior_usd: float | None = None) -> SubagentCost:
    """Return the holder of a turn that resumes a stored run, starting from what the run's turn had received."""
    return SubagentCost(stored_subagent_cost_usd(metadata), prior_usd=prior_usd)
