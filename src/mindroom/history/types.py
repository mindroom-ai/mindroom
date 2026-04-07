"""History compaction types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

_ScopeKind = Literal["agent", "team"]
_HistoryMode = Literal["all", "runs", "messages"]
_CompactionMode = Literal["auto", "manual"]
_CompactionAvailabilityReason = Literal["no_context_window", "non_positive_summary_input_budget"]
_ReplayPlanMode = Literal["configured", "limited", "summary_only", "disabled"]


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
    system_message_role: str = "system"
    skip_history_system_role: bool = True


@dataclass(frozen=True)
class HistoryScopeState:
    """Persisted compaction control/audit state stored in session metadata."""

    last_compacted_at: str | None = None
    last_summary_model: str | None = None
    last_compacted_run_count: int | None = None
    force_compact_before_next_run: bool = False


@dataclass(frozen=True)
class ResolvedHistoryExecutionPlan:
    """Single source of truth for history-budget policy in one run scope."""

    authored_compaction_config: bool
    authored_compaction_enabled: bool
    destructive_compaction_available: bool
    explicit_compaction_model: bool
    compaction_model_name: str
    compaction_context_window: int | None
    replay_window_tokens: int | None
    trigger_threshold_tokens: int | None
    reserve_tokens: int
    static_prompt_tokens: int | None
    replay_budget_tokens: int | None
    summary_input_budget_tokens: int | None
    unavailable_reason: _CompactionAvailabilityReason | None = None


@dataclass(frozen=True)
class ResolvedReplayPlan:
    """Concrete persisted-replay plan for one live model call."""

    mode: _ReplayPlanMode
    estimated_tokens: int
    add_history_to_context: bool
    add_session_summary_to_context: bool
    num_history_runs: int | None = None
    num_history_messages: int | None = None
    history_limit_mode: Literal["runs", "messages"] | None = None
    history_limit: int | None = None


def _to_k(tokens: int) -> str:
    """Abbreviate token counts: ``145826`` → ``~146K``, values <1000 as-is."""
    if tokens >= 1000:
        return f"~{int((tokens + 500) // 1000)}K"
    return str(tokens)


@dataclass(frozen=True)
class CompactionOutcome:
    """Completed pre-run compaction result used for notices and tests."""

    mode: _CompactionMode
    session_id: str
    scope: str
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
    role_instructions_tokens: int | None = None
    tool_definition_tokens: int | None = None
    current_prompt_tokens: int | None = None

    def to_notice_metadata(self) -> dict[str, object]:
        """Return serialized notice metadata for Matrix compaction messages."""
        meta: dict[str, object] = {
            "version": 1,
            "mode": self.mode,
            "session_id": self.session_id,
            "scope": self.scope,
            "summary_model": self.summary_model,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "window_tokens": self.window_tokens,
            "runs_before": self.runs_before,
            "runs_after": self.runs_after,
            "compacted_run_count": self.compacted_run_count,
            "compacted_at": self.compacted_at,
        }
        if self.role_instructions_tokens is not None:
            meta["role_instructions_tokens"] = self.role_instructions_tokens
        if self.tool_definition_tokens is not None:
            meta["tool_definition_tokens"] = self.tool_definition_tokens
        if self.current_prompt_tokens is not None:
            meta["current_prompt_tokens"] = self.current_prompt_tokens
        return meta

    def format_notice(self) -> str:
        """Format a human-readable compaction notice."""
        line1 = (
            f"\U0001f4e6 Compacted {self.compacted_run_count} runs: "
            f"{_to_k(self.before_tokens)} \u2192 {_to_k(self.after_tokens)}"
            f" / {_to_k(self.window_tokens)} history tokens"
        )
        overhead_parts: list[str] = []
        if self.role_instructions_tokens is not None:
            overhead_parts.append(f"{_to_k(self.role_instructions_tokens)} instructions")
        if self.tool_definition_tokens is not None:
            overhead_parts.append(f"{_to_k(self.tool_definition_tokens)} tools")
        if self.current_prompt_tokens is not None:
            overhead_parts.append(f"{_to_k(self.current_prompt_tokens)} prompt")
        if overhead_parts:
            return f"{line1}\n   Overhead: {' + '.join(overhead_parts)}"
        return line1


@dataclass(frozen=True)
class PreparedHistoryState:
    """Prepared persisted-history state for one run."""

    compaction_outcomes: list[CompactionOutcome] = field(default_factory=list)
    replay_plan: ResolvedReplayPlan | None = None
    replays_persisted_history: bool = False
