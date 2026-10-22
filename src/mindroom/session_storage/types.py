"""Values the owned conversation store reads and writes."""

from __future__ import annotations

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
type SessionField = Literal["metadata", "session_state"]


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
    session can keep a run a fresh load no longer returns.
    ``summary`` is the owner scope's latest summary row, derived on load and never written back.
    ``session_state`` is the dict the run's tools share and change in place.
    """

    session_id: str
    kind: SessionKind
    owner_id: str
    user_id: str | None = None
    metadata: dict[str, Any] | None = None
    session_state: dict[str, Any] = field(default_factory=dict)
    runs: list[AnyRunRecord] = field(default_factory=list)
    summary: ScopeSummary | None = None
    created_at: int = field(default_factory=lambda: int(time()))
    updated_at: int | None = None


__all__ = [
    "ConversationSession",
    "Refusal",
    "SaveOutcome",
    "SessionField",
    "SessionFields",
    "SessionKind",
]
