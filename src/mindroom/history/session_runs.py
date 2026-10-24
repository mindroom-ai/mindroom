"""Read the finished Matrix turns of an agent's conversations, live and compacted, for background workers."""

from __future__ import annotations

from itertools import takewhile
from typing import TYPE_CHECKING

from mindroom.history.storage import run_event_ids
from mindroom.llm.runs import RunRecord, RunStatus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.config.main import Config
    from mindroom.session_storage.store import ConversationStore

# Which of a room's events an agent saw redacted, or None until that agent's running bot has finished its first sync.
type RedactedEventIds = Callable[[str, str, tuple[str, ...]], Awaitable[frozenset[str] | None]]

# A task another agent delegated runs in a session of its own, named after the delegation.
DELEGATION_SESSION_PREFIX = "delegate:"
# Dynamic-workflow participants keep sessions of their own beside the conversation that started them.
_WORKFLOW_SESSION_MARKER = ":dynamic_workflow:"
_DAY_SECONDS = 86_400
_APPROVAL_GRACE_SECONDS = 3600


def run_key(run: RunRecord) -> tuple[int, str]:
    """Return a run's position in its session."""
    return (run.created_at, run.run_id)


def updated_sessions(storage: ConversationStore, agent_name: str, *, since: int) -> list[tuple[str, int]]:
    """Return ``(session_id, updated_at)`` for the agent's conversations updated after ``since``, oldest first."""
    return [
        (session_id, updated_at)
        for session_id, updated_at in storage.list_updated_sessions("agent", agent_name, since=since)
        if _WORKFLOW_SESSION_MARKER not in session_id
    ]


def session_runs(storage: ConversationStore, session_id: str) -> list[RunRecord]:
    """Return the session's top-level Matrix turns that finished or wait for approval, live and compacted, oldest first.

    A turn waiting for approval finishes in place later, so callers stop at it rather than skip it.
    """
    # A loaded session holds only the runs after the latest summary row, so the whole run log is read instead.
    return sorted(
        (
            run
            for run in storage.list_runs(session_id)
            if isinstance(run, RunRecord)
            and run.parent_run_id is None
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


def finished_runs(runs: list[RunRecord], config: Config, *, now: float) -> tuple[list[RunRecord], float | None]:
    """Return the finished runs before the first one that can still finish later, and when that one stops waiting.

    A turn waiting for approval finishes in place with its original, earlier creation time, so nothing after it is
    read yet; once its approval window has passed it can no longer finish and is skipped.
    """
    approval_window = _approval_window_seconds(config)
    finished: list[RunRecord] = []
    for run in runs:
        if run.status == RunStatus.paused:
            # Its newest message is the call waiting for approval, whose card expires counting from when it was posted.
            asked_at = max([run.created_at, *(message.created_at for message in run.messages)])
            expires_at = asked_at + approval_window
            if now < expires_at:
                return finished, expires_at
            continue
        finished.append(run)
    return finished, None


async def unredacted_runs(
    runs: list[RunRecord],
    agent_name: str,
    redacted_event_ids: RedactedEventIds,
) -> list[RunRecord] | None:
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
