"""Read the finished Matrix turns of an agent's conversations, live and compacted, for graph ingestion."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from agno.db.base import SessionType
from agno.run.agent import RunOutput
from agno.run.base import RunStatus

from mindroom.knowledge_graph.episodes import run_key

if TYPE_CHECKING:
    from agno.db.base import BaseDb
    from agno.run.team import TeamRunOutput
    from agno.run.workflow import WorkflowRunOutput
    from agno.session.agent import AgentSession

# Dynamic-workflow participants keep sessions of their own beside the conversation that started them.
_WORKFLOW_SESSION_MARKER = ":dynamic_workflow:"


def updated_sessions(storage: BaseDb, agent_name: str, *, since: int) -> list[tuple[str, int]]:
    """Return ``(session_id, updated_at)`` for the agent's conversations updated after ``since``, oldest first."""
    # Agno filters by creation time only, so the update cursor is applied here.
    sessions = cast(
        "list[AgentSession]",
        storage.get_sessions(
            session_type=SessionType.AGENT,
            component_id=agent_name,
            sort_by="updated_at",
            sort_order="asc",
            include_runs=False,
        ),
    )
    listed: list[tuple[str, int]] = []
    for session in sessions:
        updated_at = session.updated_at or session.created_at or 0
        if updated_at > since and _WORKFLOW_SESSION_MARKER not in session.session_id:
            listed.append((session.session_id, updated_at))
    return listed


def session_runs(storage: BaseDb, session_id: str) -> list[RunOutput]:
    """Return the session's top-level Matrix turns that finished or wait for approval, live and compacted, oldest first.

    A turn waiting for approval finishes in place later, so callers stop at it rather than skip it.
    """
    # A loaded session holds only the runs after the latest summary row, so the whole run log is read instead.
    runs = cast("list[RunOutput | TeamRunOutput | WorkflowRunOutput]", storage.get_runs(session_id=session_id))
    return sorted(
        (
            run
            for run in runs
            if isinstance(run, RunOutput)
            and run.parent_run_id is None
            and run.run_id
            and run.status in (RunStatus.completed, RunStatus.paused)
            and (run.metadata or {}).get("room_id")
        ),
        key=run_key,
    )
