"""What the subagent turns whose results one agent turn received cost, collected while that turn runs.

A reply publishes it beside its own cost, and a child's settlement adds it to the child's own, so nested children
roll up into the reply that received the outermost one.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.constants import LEGACY_COST_ID, SUBAGENT_COST_RUN_METADATA_KEY, SUBAGENT_RECEIPTS_RUN_METADATA_KEY

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping


def merge_by_key[T](views: Iterable[Mapping[str, T]], size: Callable[[T], float]) -> dict[str, T]:
    """Return every key of ``views`` with its largest value by ``size``: an older view of a turn holds less, never more."""
    merged: dict[str, T] = {}
    for view in views:
        for key, value in view.items():
            if key not in merged or size(value) > size(merged[key]):
                merged[key] = value
    return merged


def stored_by_id(metadata: Mapping[str, Any] | None, key: str, sum_key: str) -> dict[str, Any]:
    """Return what a run's ``metadata`` stores under ``key`` by id; a run that stored only its sum holds one entry."""
    # LEGACY_COMPAT: Run metadata stored with what its turn spent or received before it only as a sum.
    # Legacy format: a run's metadata holds ``mindroom_subagent_cost_usd`` or ``mindroom_prior_usage`` without the map
    # by id beside it (``mindroom_subagent_receipts``, ``mindroom_prior_usage_by_run``), or, once such a release
    # resumed the run after a rollback, a ``mindroom_subagent_cost_usd`` larger than its map's total.
    # Last legacy release: v2026.11.512; the next, still unreleased, release writes each map beside its sum.
    # Handling: the sum, or what it exceeds its map by (``stored_receipts``), reads as one entry under
    # ``LEGACY_COST_ID``, so a resume of that run reports it as those releases did; restart recovery and a job's
    # continuation drop that entry, which may cover what they read by id.
    # Coverage: tests/test_restart_turn_cost.py::test_values_stored_as_sums_resume_as_one_entry_that_restart_recovery_drops,
    # tests/test_restart_turn_cost.py::test_a_continuation_leaves_out_what_its_cut_short_turn_stored_as_a_sum,
    # tests/test_restart_turn_cost.py::test_a_sum_an_earlier_release_advanced_past_its_map_resumes_with_the_difference.
    metadata = metadata or {}
    if isinstance(stored := metadata.get(key), dict):
        return dict(stored)
    return {} if metadata.get(sum_key) is None else {LEGACY_COST_ID: metadata[sum_key]}


@dataclass
class SubagentCost:
    """The priced cost of the subagent turns one turn received, by delegation id, so a receipt it holds twice counts once.

    `unknown` says that it received a turn whose cost nobody knows, so `usd` is a lower bound.
    """

    receipts: dict[str, float] = field(default_factory=dict)
    unknown: bool = False

    @property
    def usd(self) -> float | None:
        """Return what they cost together, None while the turn received none of known cost."""
        return sum(self.receipts.values()) if self.receipts else None

    def add(self, receipts: Mapping[str, float]) -> None:
        """Merge another view of what the turn received into this one."""
        self.receipts.update({key: max(usd, self.receipts.get(key, usd)) for key, usd in receipts.items()})


_RECEIVING: ContextVar[SubagentCost | None] = ContextVar("received_subagent_cost", default=None)


@contextmanager
def receiving_subagent_cost(cost: SubagentCost) -> Iterator[None]:
    """Collect into ``cost`` what the subagent turns handed to the turn running in this context cost."""
    token = _RECEIVING.set(cost)
    try:
        yield
    finally:
        _RECEIVING.reset(token)


def receive_subagent_cost(delegation_id: str, usd: float | None) -> None:
    """Add settled child turn ``delegation_id``'s cost to the turn receiving its result; an unknown cost adds nothing."""
    cost = _RECEIVING.get()
    if cost is not None and usd is not None:
        cost.add({delegation_id: usd})
    elif cost is not None:
        cost.unknown = True


def received_subagent_cost_usd() -> float | None:
    """Return what the subagent turns the turn running in this context received cost, None for none of known cost."""
    cost = _RECEIVING.get()
    return None if cost is None else cost.usd


def stored_receipts(metadata: Mapping[str, Any] | None) -> dict[str, float]:
    """Return the receipts a run's ``metadata`` stores, as ``receipts_metadata`` wrote them; only a number is a cost."""
    stored = stored_by_id(metadata, SUBAGENT_RECEIPTS_RUN_METADATA_KEY, SUBAGENT_COST_RUN_METADATA_KEY)
    # A bool is no cost, though it is an int.
    receipts = {key: float(usd) for key, usd in stored.items() if type(usd) in (int, float)}
    total = (metadata or {}).get(SUBAGENT_COST_RUN_METADATA_KEY)
    if (
        isinstance(total, int | float)
        and not isinstance(total, bool)
        and (unkeyed := total - sum(receipts.values())) > 1e-9
    ):
        receipts[LEGACY_COST_ID] = receipts.get(LEGACY_COST_ID, 0.0) + unkeyed
    return receipts


def carried_receipts(ran: Mapping[str, Mapping[str, Any] | None], jobs: Mapping[str, str | None]) -> dict[str, float]:
    """Return what a turn's cut-short runs received, from each one's stored metadata by run id, for the turn after them."""
    # A sum stored without ids may cover them, and a job is the turn's whose run acknowledged it (``job_acknowledgers``).
    left_out = {LEGACY_COST_ID, *(key for key, by in jobs.items() if by not in ran)}
    received = merge_by_key(map(stored_receipts, ran.values()), float)
    return {key: usd for key, usd in received.items() if key not in left_out}


def receipts_metadata(*views: Mapping[str, float]) -> dict[str, Any]:
    """Return the entries that store ``views`` of a turn's receipts merged, with the sum earlier releases read."""
    receipts = merge_by_key(views, float)
    total = {SUBAGENT_COST_RUN_METADATA_KEY: sum(receipts.values())}
    return {SUBAGENT_RECEIPTS_RUN_METADATA_KEY: receipts, **total} if receipts else {}


def subagent_cost_metadata(*stored: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the metadata entries a run's save stores of what its turn received so far, empty for nothing received.

    They merge what each ``stored`` metadata holds, the record's and its row's, with what the turn in this context
    received, so with nothing received here, as in recovery's saves, they keep the stored cost. A turn that resumes
    the run after a pause goes on from it (``stored_receipts``).
    """
    cost = _RECEIVING.get()
    return receipts_metadata(*map(stored_receipts, stored), cost.receipts if cost is not None else {})
