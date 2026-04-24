"""History compaction types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeGuard

_ScopeKind = Literal["agent", "team"]
_HistoryMode = Literal["all", "runs", "messages"]
_CompactionMode = Literal["auto", "manual"]
_CompactionDecisionMode = Literal["none", "opportunistic", "required"]
_CompactionLifecycleStatus = Literal["success", "failed", "timeout"]
_CompactionAvailabilityReason = Literal["no_context_window", "non_positive_summary_input_budget"]
_ReplayPlanMode = Literal["configured", "limited", "disabled"]


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
    hard_replay_budget_tokens: int | None = None


@dataclass(frozen=True)
class ResolvedReplayPlan:
    """Concrete persisted-replay plan for one live model call."""

    mode: _ReplayPlanMode
    estimated_tokens: int
    add_history_to_context: bool
    num_history_runs: int | None = None
    num_history_messages: int | None = None
    history_limit_mode: Literal["runs", "messages"] | None = None
    history_limit: int | None = None


@dataclass(frozen=True)
class CompactionDecision:
    """Resolved compaction lifecycle decision for one history preparation."""

    mode: _CompactionDecisionMode
    reason: str
    current_history_tokens: int | None = None
    trigger_budget_tokens: int | None = None
    hard_budget_tokens: int | None = None
    fitted_replay_tokens: int | None = None


@dataclass(frozen=True)
class PostResponseCompactionCheck:
    """Post-response check for whether the updated session should compact now."""

    agent_name: str
    session_id: str
    scope_kind: _ScopeKind
    scope_id: str
    execution_plan: ResolvedHistoryExecutionPlan
    active_context_window: int | None

    @property
    def scope(self) -> HistoryScope:
        """Return the typed history scope for this request."""
        return HistoryScope(kind=self.scope_kind, scope_id=self.scope_id)

    @property
    def dedupe_key(self) -> tuple[str, str, str]:
        """Return the in-process dedupe key for one compaction check."""
        return (self.agent_name, self.scope.key, self.session_id)


@dataclass(frozen=True)
class CompactionLifecycleStart:
    """Visible lifecycle notice payload emitted before foreground compaction."""

    mode: _CompactionMode
    session_id: str
    scope: str
    summary_model: str
    before_tokens: int
    history_budget_tokens: int | None
    runs_before: int


@dataclass(frozen=True)
class CompactionLifecycleSuccess:
    """Visible lifecycle notice payload emitted after successful foreground compaction."""

    notice_event_id: str | None
    outcome: CompactionOutcome
    duration_ms: int


@dataclass(frozen=True)
class CompactionLifecycleFailure:
    """Visible lifecycle notice payload emitted after failed foreground compaction."""

    notice_event_id: str | None
    mode: _CompactionMode
    session_id: str
    scope: str
    summary_model: str
    status: _CompactionLifecycleStatus
    duration_ms: int
    failure_reason: str
    history_budget_tokens: int | None


class CompactionLifecycle(Protocol):
    """Interface for ordered foreground compaction Matrix notice delivery."""

    async def start(self, event: CompactionLifecycleStart) -> str | None:
        """Send the initial compaction notice and return its Matrix event id."""

    async def complete_success(self, event: CompactionLifecycleSuccess) -> None:
        """Edit the lifecycle notice after successful compaction."""

    async def complete_failure(self, event: CompactionLifecycleFailure) -> None:
        """Edit the lifecycle notice after failed compaction."""


def _to_k(tokens: int) -> str:
    """Abbreviate token counts: ``145826`` → ``~145K``, values <1000 as-is.

    Uses floor rounding so nearby values do not jump across adjacent ``K``
    buckets when this helper is used for compact auxiliary counts.
    """
    if tokens >= 1000:
        return f"~{tokens // 1000}K"
    return str(tokens)


def _format_exact_tokens(tokens: int) -> str:
    """Format token counts exactly with thousands separators."""
    return f"{tokens:,}"


def _should_render_overhead_tokens(tokens: int | None) -> TypeGuard[int]:
    """Return whether one overhead segment should appear in the notice."""
    return tokens is not None and tokens != 0


@dataclass(frozen=True)
class CompactionOutcome:
    """Completed compaction result used for lifecycle notices and tests."""

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
    history_budget_tokens: int | None = None
    role_instructions_tokens: int | None = None
    tool_definition_tokens: int | None = None
    current_prompt_tokens: int | None = None
    lifecycle_notice_event_id: str | None = None
    duration_ms: int | None = None
    status: _CompactionLifecycleStatus = "success"

    def to_notice_metadata(self) -> dict[str, object]:
        """Return serialized notice metadata for Matrix compaction messages."""
        version = 2 if self.history_budget_tokens is not None else 1
        meta: dict[str, object] = {
            "version": version,
            "status": self.status,
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
        if self.history_budget_tokens is not None:
            meta["history_budget_tokens"] = self.history_budget_tokens
        if self.role_instructions_tokens is not None:
            meta["role_instructions_tokens"] = self.role_instructions_tokens
        if self.tool_definition_tokens is not None:
            meta["tool_definition_tokens"] = self.tool_definition_tokens
        if self.current_prompt_tokens is not None:
            meta["current_prompt_tokens"] = self.current_prompt_tokens
        if self.lifecycle_notice_event_id is not None:
            meta["lifecycle_notice_event_id"] = self.lifecycle_notice_event_id
        if self.duration_ms is not None:
            meta["duration_ms"] = self.duration_ms
        return meta

    def format_notice(self) -> str:
        """Format a human-readable compaction notice."""
        line1 = (
            f"\U0001f4e6 Compacted {self.compacted_run_count} runs: "
            f"{_format_exact_tokens(self.before_tokens)} \u2192 {_format_exact_tokens(self.after_tokens)}"
        )
        if self.history_budget_tokens is not None:
            line1 += f" / {_format_exact_tokens(self.history_budget_tokens)} history budget"
        overhead_parts: list[str] = []
        if _should_render_overhead_tokens(self.role_instructions_tokens):
            overhead_parts.append(f"{_to_k(self.role_instructions_tokens)} instructions")
        if _should_render_overhead_tokens(self.tool_definition_tokens):
            overhead_parts.append(f"{_to_k(self.tool_definition_tokens)} tools")
        if _should_render_overhead_tokens(self.current_prompt_tokens):
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
    compaction_decision: CompactionDecision = field(
        default_factory=lambda: CompactionDecision(mode="none", reason="unclassified"),
    )
    post_response_compaction_checks: list[PostResponseCompactionCheck] = field(default_factory=list)
