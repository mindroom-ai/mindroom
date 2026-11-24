"""Limits, persona ceiling, spend totals, and budget rules for workflow runs."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any

from mindroom.delegation.state import SubagentPersona
from mindroom.workflow_runs.models import WorkflowRunState
from mindroom.workflow_runs.store import WorkflowRunStoreError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.workflow_runs.models import WorkflowRun
    from mindroom.workflow_runs.store import UsageSummary, WorkflowRunStore

_OVER_BUDGET = "Stopped at the cost limit (${spent} of ${limit})."
_FILTERED_TURN = "A turn that is limited to some of its agent's tools cannot start or resume a workflow."


@dataclass(frozen=True, slots=True)
class WorkflowLimits:
    """The limits one `workflow` toolkit sets for the runs it starts."""

    max_concurrent_runs: int = 8
    max_runtime_hours: float = 72
    max_tool_calls_per_minute: int = 600
    max_concurrent_agents: int | None = None
    # What a run may be projected to cost before its card says it is large; 0 never says so for its cost.
    large_run_notice_usd: float = 10


def ceiling(context: ToolRuntimeContext, *saved: Mapping[str, object] | None) -> dict[str, object] | None:
    """Return the persona a run is saved with: what the turn in *context* and every persona of *saved* all keep.

    *saved* is the run's own persona when the turn resumes it, and its outer run's when the turn starts it inside
    that run. A run so never has more than the turn that started it, nor more than a narrower one that resumed it.
    A turn that a filter narrows keeps a list no persona holds, so it is refused.
    """
    if context.tool_function_filter is not None:
        raise WorkflowRunStoreError(_FILTERED_TURN)
    kept = context.persona
    for other in (SubagentPersona.from_dict(persona) for persona in saved if persona is not None):
        kept = (
            other
            if kept is None
            else replace(
                kept,
                tools=_both(kept.tools, other.tools, toolkits=True),
                skills=_both(kept.kept_skills, other.kept_skills),
                knowledge=_both(kept.kept_knowledge, other.kept_knowledge),
            )
        )
    return asdict(kept) if kept is not None else None


def _covers(side: tuple[str, ...] | None, name: str, *, toolkits: bool = False) -> bool:
    return side is None or name in side or (toolkits and name.partition(".")[0] in side)


def _both(
    one: tuple[str, ...] | None,
    other: tuple[str, ...] | None,
    *,
    toolkits: bool = False,
) -> tuple[str, ...] | None:
    """Return the names both lists hold, where None holds every name and a toolkit holds each of its functions."""
    if one is None or other is None:
        return other if one is None else one
    return tuple(
        name
        for name in dict.fromkeys((*one, *other))
        if _covers(one, name, toolkits=toolkits) and _covers(other, name, toolkits=toolkits)
    )


def _within(actual: tuple[str, ...] | None, cap: tuple[str, ...] | None, *, toolkits: bool = False) -> bool:
    return cap is None or (actual is not None and all(_covers(cap, name, toolkits=toolkits) for name in actual))


def fits_ceiling(persona: SubagentPersona | None, cap: SubagentPersona) -> bool:
    """Return whether *persona* stays within *cap*, using the same coverage rule as `ceiling()`."""
    return (
        _within(None if persona is None else persona.tools, cap.tools, toolkits=True)
        and _within(None if persona is None else persona.kept_skills, cap.kept_skills)
        and _within(None if persona is None else persona.kept_knowledge, cap.kept_knowledge)
    )


def run_usage(
    store: WorkflowRunStore,
    run_id: str,
    *,
    root_summary: UsageSummary | None = None,
) -> dict[str, Any]:
    """Return what a run used so far and what its current attempt cost, with the runs inside it, each once.

    A nested run is part of the attempt that started it.
    """
    run = store.get_run(run_id)
    summary = root_summary or store.usage_summary(run_id, attempt=run.attempt)
    used: dict[str, Any] = {**summary.totals, "attempt_cost_usd": summary.attempt_cost_usd}
    for child in store.child_runs(run_id):
        inner = run_usage(store, child.run_id)
        for name in ("input_tokens", "output_tokens", "tool_calls", "cost_usd"):
            used[name] += inner[name]
        used["cost_unknown"] = used["cost_unknown"] or inner["cost_unknown"]
        group = store.get_node(run_id, f"{child.parent_node_key}/{child.run_id}")
        if group is not None and group.attempt == run.attempt:
            if group.generation == 1:
                used["attempt_cost_usd"] += inner["cost_usd"]
            elif child.state is not WorkflowRunState.STARTING:
                used["attempt_cost_usd"] += inner["attempt_cost_usd"]
    return used


def budget_exceeded(store: WorkflowRunStore, run: WorkflowRun) -> str | None:
    """Return the stop reason when *run* or any ancestor has reached its `max_cost_usd`, else None."""
    current: WorkflowRun | None = run
    while current is not None:
        if current.max_cost_usd is not None:
            spent = run_usage(store, current.run_id)["cost_usd"]
            if limit_reached(spent, current.max_cost_usd):
                return _OVER_BUDGET.format(spent=usd(spent), limit=usd(current.max_cost_usd))
        current = store.get_run(current.parent_run_id) if current.parent_run_id is not None else None
    return None


def limit_reached(spent: float, limit: float) -> bool:
    """Return whether *spent* has reached *limit*, tolerating binary float drift on summed turn costs."""
    return spent >= limit or math.isclose(spent, limit, rel_tol=1e-9, abs_tol=0.0)


def usd(amount: float) -> str:
    """Return an amount of money as a person reads it: two decimal places, and more for one under a cent."""
    return f"{amount:.2f}" if amount >= 0.01 or amount == 0 else f"{amount:.6f}".rstrip("0")
