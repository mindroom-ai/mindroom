"""History replay and compaction types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput

_ScopeKind = Literal["agent", "team"]
_HistoryMode = Literal["all", "runs", "messages"]
_CompactionMode = Literal["auto", "manual"]


@dataclass(frozen=True)
class HistoryScope:
    """One logical replay scope inside a stored Agno session."""

    kind: _ScopeKind
    scope_id: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.scope_id}"


@dataclass(frozen=True)
class HistoryPolicy:
    """Authored raw-history replay policy for one run."""

    mode: _HistoryMode
    limit: int | None = None


@dataclass(frozen=True)
class CompactionState:
    """Persisted scoped compaction state stored in session metadata."""

    summary: str | None = None
    last_compacted_run_id: str | None = None
    compacted_at: str | None = None
    summary_model: str | None = None
    force_compact_before_next_run: bool = False

    @property
    def has_summary(self) -> bool:
        return self.summary is not None and self.summary.strip() != ""

    @property
    def has_cutoff(self) -> bool:
        return self.last_compacted_run_id is not None and self.last_compacted_run_id != ""


@dataclass(frozen=True)
class ReplayPlan:
    """Resolved persisted replay state for the current run before live-thread glue."""

    scope: HistoryScope
    state: CompactionState
    visible_runs: list[RunOutput | TeamRunOutput]
    summary_prompt_prefix: str
    history_message_groups: list[list[Message]]
    history_messages: list[Message]
    replay_tokens: int
    has_stored_replay_state: bool


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
    last_compacted_run_id: str | None
    compacted_at: str
    notify: bool

    def to_notice_metadata(self) -> dict[str, object]:
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
class PreparedHistory:
    """Prepared persisted replay payload for one run."""

    summary_prompt_prefix: str = ""
    history_messages: list[Message] = field(default_factory=list)
    cache_key_fragment: str | None = None
    compaction_outcomes: list[CompactionOutcome] = field(default_factory=list)
    has_stored_replay_state: bool = False
