"""Read the finished Matrix turns of an agent's conversations, live and compacted, for background workers."""

from __future__ import annotations

from itertools import takewhile
from typing import TYPE_CHECKING, cast

from agno.db.base import SessionType
from agno.run.agent import RunOutput
from agno.run.base import RunStatus

from mindroom.history.storage import run_event_ids

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agno.db.base import BaseDb
    from agno.run.team import TeamRunOutput
    from agno.run.workflow import WorkflowRunOutput
    from agno.session.agent import AgentSession

    from mindroom.config.main import Config

# Which of a room's events an agent saw redacted, or None until that agent's running bot has finished its first sync.
type RedactedEventIds = Callable[[str, str, tuple[str, ...]], Awaitable[frozenset[str] | None]]

# A task another agent delegated runs in a session of its own, named after the delegation.
DELEGATION_SESSION_PREFIX = "delegate:"
# Dynamic-workflow participants keep sessions of their own beside the conversation that started them.
_WORKFLOW_SESSION_MARKER = ":dynamic_workflow:"
_DAY_SECONDS = 86_400
_APPROVAL_GRACE_SECONDS = 3600


def run_key(run: RunOutput) -> tuple[int, str]:
    """Return a run's position in its session."""
    return (run.created_at or 0, run.run_id or "")


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


def _approval_window_seconds(config: Config) -> float:
    """Return how long a turn can wait for approval before every approval it could get has expired and resumed it."""
    approval = config.tool_approval
    days = max([approval.timeout_days, *(rule.timeout_days for rule in approval.rules if rule.timeout_days)])
    # The card's expiry counts from when it was posted, a little after the newest message, and then resumes the turn.
    return days * _DAY_SECONDS + _APPROVAL_GRACE_SECONDS


def finished_runs(runs: list[RunOutput], config: Config, *, now: float) -> tuple[list[RunOutput], float | None]:
    """Return the finished runs before the first one that can still finish later, and when that one stops waiting.

    A turn waiting for approval finishes in place with its original, earlier creation time, so nothing after it is
    read yet; once its approval window has passed it can no longer finish and is skipped.
    """
    approval_window = _approval_window_seconds(config)
    finished: list[RunOutput] = []
    for run in runs:
        if run.status == RunStatus.paused:
            # Its newest message is the call waiting for approval, whose card expires counting from when it was posted.
            asked_at = max([run.created_at or 0, *((message.created_at or 0) for message in run.messages or [])])
            expires_at = asked_at + approval_window
            if now < expires_at:
                return finished, expires_at
            continue
        finished.append(run)
    return finished, None


async def unredacted_runs(
    runs: list[RunOutput],
    agent_name: str,
    redacted_event_ids: RedactedEventIds,
) -> list[RunOutput] | None:
    """Return the runs before the first that derives from a redacted event, or None while the agent cannot tell.

    ``runs`` are one conversation's, oldest first. A redaction removes that turn and every later one, which may build
    on it, from stored history only when the agent next replies there, so readers skip them here.
    """
    by_room: dict[str, set[str]] = {}
    for run in runs:
        by_room.setdefault(str((run.metadata or {}).get("room_id")), set()).update(run_event_ids(run))
    redacted: set[str] = set()
    for room_id, event_ids in by_room.items():
        if not event_ids:
            continue
        found = await redacted_event_ids(agent_name, room_id, tuple(sorted(event_ids)))
        if found is None:
            return None
        redacted |= found
    return list(takewhile(lambda run: not run_event_ids(run) & redacted, runs))
