"""Shared Agno model-history run visibility policy."""

from typing import TypeGuard

from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput

NON_VISIBLE_RUN_STATUSES = frozenset({RunStatus.paused, RunStatus.cancelled, RunStatus.error})


def is_model_history_visible_run(run: object) -> TypeGuard[RunOutput | TeamRunOutput]:
    """Return whether one run is represented in Agno model history."""
    return (
        isinstance(run, (RunOutput, TeamRunOutput))
        and run.parent_run_id is None
        and run.status not in NON_VISIBLE_RUN_STATUSES
    )
