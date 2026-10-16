"""Read the finished Matrix turns of an agent's conversations, live and compacted, for graph ingestion."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from agno.db.base import SessionType
from agno.run.agent import RunOutput
from agno.run.base import RunStatus

from mindroom.history.storage import load_archived_runs
from mindroom.knowledge_graph.episodes import run_key
from mindroom.session_storage.agent_storage import get_agent_session

if TYPE_CHECKING:
    from agno.db.base import BaseDb
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
    session = get_agent_session(storage, session_id)
    live = session.runs or [] if session is not None else []
    runs: dict[str, RunOutput] = {}
    # A rollback can return archived runs to the live table, so one run may appear in both; either copy will do.
    for run in [*load_archived_runs(storage, session_id), *live]:
        if (
            isinstance(run, RunOutput)
            and run.parent_run_id is None
            and run.run_id
            and run.status in (RunStatus.completed, RunStatus.paused)
            and (run.metadata or {}).get("room_id")
        ):
            runs[run.run_id] = run
    return sorted(runs.values(), key=run_key)
