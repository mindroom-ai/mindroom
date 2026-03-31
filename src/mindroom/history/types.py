"""History compaction types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

_ScopeKind = Literal["agent", "team"]
_HistoryMode = Literal["all", "runs", "messages"]
_CompactionMode = Literal["auto", "manual"]


@dataclass(frozen=True)
class HistoryScope:
    """One logical persisted-history scope inside a stored Agno session."""

    kind: _ScopeKind
    scope_id: str

    @property
    def key(self) -> str:
        """Return the stable serialized storage key for this scope."""
        return f"{self.kind}:{self.scope_id}"


@dataclass(frozen=True)
class HistoryPolicy:
    """Authored raw-history selection policy for one run."""

    mode: _HistoryMode
    limit: int | None = None


@dataclass(frozen=True)
class ResolvedHistorySettings:
    """Resolved history selection policy and tool-call limits for one run."""

    policy: HistoryPolicy
    max_tool_calls_from_history: int | None


@dataclass(frozen=True)
class HistoryScopeState:
    """Persisted compaction control/audit state stored in session metadata."""

    last_compacted_at: str | None = None
    last_summary_model: str | None = None
    last_compacted_run_count: int | None = None
    force_compact_before_next_run: bool = False


@dataclass(frozen=True)
class CompactionOutcome:
    """Completed pre-run compaction result used for notices and tests."""

    mode: _CompactionMode
    summary: str
    summary_model: str
    before_tokens: int
    after_tokens: int
    window_tokens: int
    threshold_tokens: int
    reserve_tokens: int
    runs_before: int
    runs_after: int
    compacted_run_count: int
    compacted_at: str
    notify: bool

    def to_notice_metadata(self) -> dict[str, object]:
        """Return serialized notice metadata for Matrix compaction messages."""
        return {
            "version": 1,
            "mode": self.mode,
            "summary_model": self.summary_model,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "window_tokens": self.window_tokens,
            "runs_before": self.runs_before,
            "runs_after": self.runs_after,
            "compacted_run_count": self.compacted_run_count,
            "compacted_at": self.compacted_at,
        }


@dataclass(frozen=True)
class PreparedHistoryState:
    """Prepared persisted-history state for one run."""

    compaction_outcomes: list[CompactionOutcome] = field(default_factory=list)
    has_persisted_history: bool = False
