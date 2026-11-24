"""Follow an agent's conversations in their log order, live and compacted, for background readers.

Every reader shares which turns it may read and how it finds conversations with news; each keeps its own progress. A
turn takes its place in the log when it completes, not when it was created, so a reader that follows places reads each
turn once it completes, however long it ran or waited for approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict

from mindroom.history.types import HistoryScope
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.session_storage.store import logged_runs
from mindroom.session_storage.summary_rows import read_redaction_basis

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.store import ConversationStore, LoggedRun
    from mindroom.session_storage.summary_rows import RedactionBasis

# Which of a room's events an agent saw redacted, or None until that agent's running bot has finished its first sync.
type RedactedEventIds = Callable[[str, str, tuple[str, ...]], Awaitable[frozenset[str] | None]]

# A task another agent delegated runs in a session of its own, named after the delegation.
DELEGATION_SESSION_PREFIX = "delegate:"
# Dynamic-workflow participants keep sessions of their own beside the conversation that started them.
_WORKFLOW_SESSION_MARKER = ":dynamic_workflow:"


class Cursor(TypedDict):
    """What every reader keeps of one conversation besides its own progress; each reader extends it."""

    # The conversation's update time once every new turn in it was read.
    updated_at: int
    # Consecutive failures, which set the backoff.
    failures: int
    # When the current failures began, so a reader can give up on turns that keep failing.
    failing_since: float
    retry_at: float


@dataclass(frozen=True)
class DueConversation:
    """A quiet conversation updated since its reader last caught up: its Matrix turns and what a redaction would cut.

    Both come from one snapshot of the conversation's log, so a cut that lands after it cannot make them disagree.
    """

    session_id: str
    updated_at: int
    turns: list[LoggedRun]
    redaction: RedactionBasis


@dataclass(frozen=True)
class _UnreadableConversation:
    """A due conversation whose turns could not be read, for its reader to back off."""

    session_id: str
    updated_at: int
    error: Exception


@dataclass
class _DueScan:
    """The conversations one listing found for a reader."""

    due: list[DueConversation] = field(default_factory=list)
    # ``(session_id, updated_at)`` of due conversations without Matrix turns, which the reader records as caught up.
    empty: list[tuple[str, int]] = field(default_factory=list)
    unreadable: list[_UnreadableConversation] = field(default_factory=list)


def updated_sessions(storage: ConversationStore, *, since: int) -> list[tuple[str, int]]:
    """Return ``(session_id, updated_at)`` of ``storage``'s conversations updated after ``since``, oldest first."""
    return [
        (session_id, updated_at)
        for session_id, updated_at in storage.list_updated_sessions(since=since)
        if _WORKFLOW_SESSION_MARKER not in session_id
    ]


def matrix_turns(storage: ConversationStore, session_id: str) -> list[LoggedRun]:
    """Return the session's top-level Matrix turns that completed or wait for approval, live and compacted, in log order.

    Readers read completed turns only; a turn waiting for approval takes a later place once it completes.
    """
    # A loaded session holds only the runs after the latest summary row, so the whole run log is read instead.
    return _matrix_turns(storage.logged_runs(session_id))


def _read_conversation(
    storage: ConversationStore,
    session_id: str,
    *,
    agent_name: str,
    updated_at: int,
) -> DueConversation:
    """Read a conversation's Matrix turns and what a redaction would cut of the agent's history, in one snapshot."""
    with storage.transaction(write=False) as connection:
        logged = logged_runs(connection, storage.names, session_id)
        redaction = read_redaction_basis(
            connection,
            storage.names,
            session_id=session_id,
            scope_key=HistoryScope(kind="agent", scope_id=agent_name).key,
        )
    return DueConversation(session_id, updated_at, _matrix_turns(logged), redaction)


def _matrix_turns(logged: Sequence[LoggedRun]) -> list[LoggedRun]:
    return [
        turn
        for turn in logged
        if isinstance(turn.run, RunRecord)
        and turn.run.status in (RunStatus.completed, RunStatus.paused)
        and (turn.run.metadata or {}).get("room_id")
    ]


def start_seq(turns: Sequence[LoggedRun], older: Callable[[AnyRunRecord], bool]) -> int:
    """Return the place a conversation never read starts after, so its completed turns that are not ``older`` are read.

    A turn that is ``older`` but completes later, such as one approved after the start, takes a later place and is read.
    """
    newer = [turn.seq for turn in turns if turn.run.status == RunStatus.completed and not older(turn.run)]
    if newer:
        return min(newer) - 1
    return turns[-1].seq if turns else 0


def due_conversations(
    storage: ConversationStore,
    cursors: Mapping[str, Cursor],
    *,
    agent_name: str,
    since: int,
    now: float,
    idle_seconds: float,
    limit: int,
    skip: Callable[[str], bool] = lambda _session_id: False,
) -> _DueScan:
    """Return up to ``limit`` conversations updated after ``since`` and quiet for ``idle_seconds`` that have news.

    A conversation has news when it was updated after its cursor caught up and its cursor waits for no retry.
    Conversations ``skip`` names are left out, and ones without Matrix turns, such as conversations through the
    OpenAI-compatible API, take no place among the ``limit``.
    """
    scan = _DueScan()
    # At least a second, so a conversation updated after a pass always carries a later update time than it had then.
    idle_seconds = max(idle_seconds, 1)
    for session_id, updated_at in updated_sessions(storage, since=since):
        if len(scan.due) >= limit:
            break
        cursor = cursors.get(session_id)
        if (
            skip(session_id)
            or now - updated_at < idle_seconds
            or (cursor is not None and (cursor["retry_at"] > now or updated_at <= cursor["updated_at"]))
        ):
            continue
        try:
            conversation = _read_conversation(storage, session_id, agent_name=agent_name, updated_at=updated_at)
        except Exception as exc:
            # One conversation that cannot be read must not stop the reader's others.
            scan.unreadable.append(_UnreadableConversation(session_id, updated_at, exc))
            continue
        if conversation.turns:
            scan.due.append(conversation)
        else:
            scan.empty.append((session_id, updated_at))
    return scan


def back_off(cursor: Cursor, now: float, *, first_seconds: float, max_seconds: float) -> None:
    """Count one more consecutive failure and wait twice as long as after the last one, up to ``max_seconds``."""
    cursor["failing_since"] = cursor["failing_since"] or now
    cursor["failures"] += 1
    cursor["retry_at"] = now + min(max_seconds, first_seconds * 2 ** (cursor["failures"] - 1))


def caught_up(cursor: Cursor, updated_at: int) -> None:
    """Record every new turn of a conversation updated at ``updated_at`` as read, so it waits for news."""
    cursor.update(updated_at=updated_at, failures=0, failing_since=0, retry_at=0)


async def unredacted(
    conversation: DueConversation,
    agent_name: str,
    redacted_event_ids: RedactedEventIds,
) -> list[LoggedRun] | None:
    """Return the completed turns a redaction the agent saw leaves in its history, or None while it cannot tell.

    A redaction removes the first element of the agent's history holding the event, of any status, and everything
    after it, only when the agent next replies there, so readers skip those turns here. The cut is decided from the
    conversation's snapshot, so a reply that removed them while the journal was asked still keeps them out.
    """
    # Old-format history only: a legacy summary row (before v2026.9.315) is not consulted, so neither its removal of the
    # whole history when it may hold the event nor the retirement of legacy summaries when another element holds it
    # (which deletes the runs that folded mid-turn) is mirrored; the agent's next reply removes those turns from history.
    completed = [turn for turn in conversation.turns if turn.run.status == RunStatus.completed]
    candidates = conversation.redaction.candidates()
    if not conversation.turns or not candidates:
        return completed
    # A conversation's turns all name the room its session belongs to.
    room_id = str((conversation.turns[0].run.metadata or {}).get("room_id"))
    redacted = await redacted_event_ids(agent_name, room_id, tuple(sorted(candidates)))
    if redacted is None:
        return None
    cut = conversation.redaction.first_removed_seq(redacted)
    return [turn for turn in completed if cut is None or turn.seq < cut]
