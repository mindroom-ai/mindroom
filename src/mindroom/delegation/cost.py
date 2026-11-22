"""What the subagent turns whose results one agent turn received cost, collected while that turn runs.

A reply publishes it beside its own cost, and a child's settlement adds it to the child's own, so nested children
roll up into the reply that received the outermost one.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.constants import PRIOR_SUBAGENT_COST_RUN_METADATA_KEY, SUBAGENT_COST_RUN_METADATA_KEY

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping


@dataclass
class SubagentCost:
    """Per-run (``usd``) and carried prior-attempt (``prior_usd``) subagent costs; ``total_usd`` adds them for replies."""

    usd: float | None = None
    prior_usd: float | None = None

    @property
    def total_usd(self) -> float | None:
        """Return combined prior and current subagent cost, or None when none of known cost arrived."""
        return sum_subagent_cost_usd((self.prior_usd, self.usd))

    def advance_run(self) -> None:
        """Roll this run's receipts into ``prior_usd`` and reset ``usd`` for the next run id."""
        if self.usd is not None:
            self.prior_usd = (self.prior_usd or 0.0) + self.usd
            self.usd = None


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


def advance_subagent_cost() -> None:
    """Move this run's subagent receipts into ``prior_usd`` before the turn starts a new run id."""
    if (cost := _RECEIVING.get()) is not None:
        cost.advance_run()


def received_subagent_cost_usd() -> float | None:
    """Return what the subagent turns the turn running in this context received cost, None for none of known cost."""
    cost = _RECEIVING.get()
    return None if cost is None else cost.total_usd


def _float_entry(metadata: Mapping[str, Any] | None, key: str) -> float | None:
    stored = (metadata or {}).get(key)
    return float(stored) if isinstance(stored, int | float) and not isinstance(stored, bool) else None


def sum_subagent_cost_usd(costs: Iterable[float | None]) -> float | None:
    """Sum known subagent costs, returning None when every entry is None."""
    known = [usd for usd in costs if usd is not None]
    return sum(known) if known else None


def subagent_cost_metadata(stored: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the separate per-run and prior-attempt subagent cost entries a run save stores."""
    cost, base = _RECEIVING.get(), resumed_subagent_cost(stored)
    usd = base.usd if cost is None or cost.usd is None else cost.usd
    prior = base.prior_usd if cost is None or cost.prior_usd is None else cost.prior_usd
    pairs = ((SUBAGENT_COST_RUN_METADATA_KEY, usd), (PRIOR_SUBAGENT_COST_RUN_METADATA_KEY, prior))
    return {k: v for k, v in pairs if v is not None}


def resumed_subagent_cost(metadata: Mapping[str, Any] | None) -> SubagentCost:
    """Return the holder of a turn that resumes a stored run, restoring its own receipts and any carried prior cost."""
    return SubagentCost(
        _float_entry(metadata, SUBAGENT_COST_RUN_METADATA_KEY),
        prior_usd=_float_entry(metadata, PRIOR_SUBAGENT_COST_RUN_METADATA_KEY),
    )
