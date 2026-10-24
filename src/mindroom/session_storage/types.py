"""Values the conversation store reads and writes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from time import time
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from mindroom.history.types import ScopeSummary
    from mindroom.llm.runs import AnyRunRecord

type SessionKind = Literal["agent", "team"]
# Why a run save wrote no row: a cut removed the run, or it is folded behind its scope's latest summary row.
type Refusal = Literal["removed", "frozen"]
# What a run save did: wrote the row, or refused it.
type SaveOutcome = Literal["written"] | Refusal


def session_state_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """Return a private copy of a live ``session_state``, as its row would hold it.

    Tools change the live state in place, on the event loop and in the threads sync tools run in, while saves run in
    the database's lane and a managed tool job copies the state it starts from, so a copy that walks the dict in
    Python, as ``copy.deepcopy`` does, can fail with "dictionary changed size during iteration". The C JSON encoder
    reads the whole state without running Python code, so no other thread changes it mid-read.
    """
    return json.loads(json.dumps(state))


@dataclass(frozen=True, slots=True)
class SessionFields:
    """The two session-row fields MindRoom writes by field: scope metadata and the tools' shared ``session_state``."""

    metadata: dict[str, Any] | None
    session_state: dict[str, Any]


@dataclass(kw_only=True)
class ConversationSession:
    """One conversation's session row and the runs a load returns.

    ``owner_id`` is the agent or team the session belongs to (the row's ``agent_id`` or ``team_id``).
    ``runs`` holds, as loaded, the runs after the owner scope's latest summary row, every paused, cancelled, or
    errored run, and their member runs, in log order. The store's saves leave each run they write here as the store
    holds it and take a removed run out; a frozen save leaves the session's copy, or its absence, as it was, so the
    session can keep a run a fresh load no longer returns. A delete handed the session takes the deleted runs out,
    and a change to its summary rows, such as a compaction chunk, a mid-turn fold, or a cut, leaves ``runs`` and
    ``summary`` as a load returns them. ``summary`` is the owner scope's latest summary row, derived on load and on
    those changes and never written back. ``metadata`` is the row's, as loaded and as the store's latest metadata
    change for this session left it. Only the store assigns these fields.
    ``session_state`` is the dict the run's tools share and change in place. ``stored_session_state`` is the state the
    store holds for the session as far as this session knows: as loaded, then as the store's latest write of that
    field for this session left it. A run's terminal save writes ``session_state`` back only when the two differ.
    """

    session_id: str
    kind: SessionKind
    owner_id: str
    metadata: dict[str, Any] | None = None
    session_state: dict[str, Any] = field(default_factory=dict)
    stored_session_state: dict[str, Any] = field(default_factory=dict)
    runs: list[AnyRunRecord] = field(default_factory=list)
    summary: ScopeSummary | None = None
    created_at: int = field(default_factory=lambda: int(time()))
    updated_at: int | None = None


__all__ = [
    "ConversationSession",
    "Refusal",
    "SaveOutcome",
    "SessionFields",
    "SessionKind",
    "session_state_snapshot",
]
