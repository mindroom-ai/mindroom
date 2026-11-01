"""History compaction types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, cast

if TYPE_CHECKING:
    from datetime import datetime

    from mindroom.config.models import CompactionConfig
    from mindroom.llm.provider import NativeCompactionReplay
    from mindroom.session_storage.types import ConversationSession

COMPACTION_SUMMARY_RETRY_FLOOR_TOKENS = 1_000

_ScopeKind = Literal["agent", "team"]
_HistoryMode = Literal["all", "runs", "messages"]
CompactionMode = Literal["auto", "manual"]
_CompactionDecisionMode = Literal["none", "required"]
CompactionReplyOutcome = Literal["none", "success", "failed", "timeout"]
CompactionLifecycleStatus = Literal["failed", "timeout"]
CompactionAvailabilityReason = Literal[
    "no_context_window",
    "non_positive_summary_input_budget",
    "summary_input_budget_without_retry_headroom",
]
_ReplayPlanMode = Literal["configured", "limited", "disabled"]


class _HistoryScopeMetadata(TypedDict):
    """JSON-safe persisted history-scope identity."""

    kind: _ScopeKind
    scope_id: str


@dataclass(frozen=True)
class HistoryScope:
    """One logical persisted-history scope inside a stored session."""

    kind: _ScopeKind
    scope_id: str

    def __post_init__(self) -> None:
        """Validate the persisted-history scope identity."""
        if self.kind not in {"agent", "team"}:
            message = f"Invalid history scope kind: {self.kind!r}"
            raise ValueError(message)
        if not self.scope_id:
            message = "History scope requires a non-empty scope_id"
            raise ValueError(message)

    @property
    def key(self) -> str:
        """Return the stable serialized storage key for this scope."""
        return f"{self.kind}:{self.scope_id}"

    def to_metadata(self) -> _HistoryScopeMetadata:
        """Return JSON-safe history-scope metadata."""
        return {
            "kind": self.kind,
            "scope_id": self.scope_id,
        }

    @classmethod
    def from_metadata(cls, raw_metadata: object) -> HistoryScope | None:
        """Return normalized history-scope metadata."""
        if not isinstance(raw_metadata, Mapping):
            return None
        metadata = cast("Mapping[str, object]", raw_metadata)
        kind = metadata.get("kind")
        scope_id = metadata.get("scope_id")
        if kind not in {"agent", "team"} or not isinstance(scope_id, str) or not scope_id:
            return None
        return cls(kind=cast("_ScopeKind", kind), scope_id=scope_id)


def owner_scope(session: ConversationSession) -> HistoryScope:
    """Return the history scope that owns ``session``: its kind and owner, which a load takes from its database's."""
    return HistoryScope(kind=session.kind, scope_id=session.owner_id)


@dataclass(frozen=True)
class HistoryPolicy:
    """Authored raw-history selection policy for one run."""

    mode: _HistoryMode
    limit: int | None = None

    @property
    def num_history_runs(self) -> int | None:
        """Return the run-replay limit for this policy."""
        return self.limit if self.mode == "runs" else None

    @property
    def num_history_messages(self) -> int | None:
        """Return the message-replay limit for this policy."""
        return self.limit if self.mode == "messages" else None


@dataclass(frozen=True)
class ResolvedHistorySettings:
    """Resolved history selection policy, tool-call limits, and text compaction config for one run.

    ``has_authored_compaction_config`` tells whether the operator configured text compaction for the entity or its
    defaults, rather than only inheriting the built-in config.
    """

    policy: HistoryPolicy
    max_tool_calls_from_history: int | None
    compaction_config: CompactionConfig
    has_authored_compaction_config: bool

    @property
    def automatic_compaction(self) -> bool:
        """Return whether compaction runs on its own before and during a reply; a manual request compacts regardless."""
        return self.has_authored_compaction_config and self.compaction_config.enabled

    @property
    def replays_whole_history(self) -> bool:
        """Return whether no authored run, message, or tool-call limit drops older history from replay."""
        return self.policy.mode == "all" and self.max_tool_calls_from_history is None


@dataclass(frozen=True)
class ScopeSummary:
    """A scope's latest compaction summary, which replays ahead of the scope's remaining runs."""

    text: str
    updated_at: datetime


def replayed_summary_text(text: str | None) -> str | None:
    """Return a stored summary as replay reads it: stripped, or None when it is missing or blank."""
    return (text or "").strip() or None


@dataclass(frozen=True)
class HistoryScopeState:
    """Persisted compaction control state stored in session metadata.

    Compaction history itself lives in the summary rows beside the runs table,
    owned by ``session_storage/summary_rows.py``.
    """

    force_compact_before_next_run: bool = False

    @property
    def compaction_mode(self) -> CompactionMode:
        """Return how a compaction of this scope is reported: manual when requested, else automatic."""
        return "manual" if self.force_compact_before_next_run else "auto"


@dataclass(frozen=True)
class ResolvedHistoryExecutionPlan:
    """Single source of truth for history-budget policy in one run scope."""

    automatic_compaction: bool
    text_compaction_available: bool
    explicit_compaction_model: bool
    compaction_model_name: str
    replay_window_tokens: int | None
    trigger_threshold_tokens: int | None
    reserve_tokens: int
    static_prompt_tokens: int | None
    replay_budget_tokens: int | None
    summary_input_budget_tokens: int | None
    compaction_timeout_seconds: float
    unavailable_reason: CompactionAvailabilityReason | None = None
    hard_replay_budget_tokens: int | None = None
    compaction_fallback_model_name: str | None = None
    compaction_fallback_summary_input_budget_tokens: int | None = None


@dataclass(frozen=True)
class ResolvedReplayPlan:
    """Concrete persisted-replay plan for one live model call."""

    mode: _ReplayPlanMode
    estimated_tokens: int
    add_history_to_context: bool
    num_history_runs: int | None = None
    num_history_messages: int | None = None


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
class CompactionLifecycleStart:
    """Visible lifecycle notice payload emitted before foreground compaction."""

    mode: CompactionMode
    session_id: str
    scope: str
    summary_model: str
    before_tokens: int
    history_budget_tokens: int | None
    runs_before: int
    threshold_tokens: int | None = None


@dataclass(frozen=True)
class CompactionLifecycleProgress:
    """Visible lifecycle progress payload emitted after persisted compaction chunks."""

    mode: CompactionMode
    session_id: str
    scope: str
    summary_model: str
    before_tokens: int
    after_tokens: int
    history_budget_tokens: int | None
    runs_before: int
    compacted_run_count: int
    runs_remaining: int
    threshold_tokens: int | None = None
    duration_ms: int | None = None
    notice_event_id: str | None = None

    def to_notice_metadata(self) -> dict[str, object]:
        """Return serialized progress metadata for Matrix compaction messages."""
        meta: dict[str, object] = {
            "version": 3,
            "status": "running",
            "mode": self.mode,
            "session_id": self.session_id,
            "scope": self.scope,
            "summary_model": self.summary_model,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "runs_before": self.runs_before,
            "compacted_run_count": self.compacted_run_count,
            "runs_remaining": self.runs_remaining,
        }
        if self.history_budget_tokens is not None:
            meta["history_budget_tokens"] = self.history_budget_tokens
        if self.threshold_tokens is not None:
            meta["threshold_tokens"] = self.threshold_tokens
        if self.duration_ms is not None:
            meta["duration_ms"] = self.duration_ms
        return meta

    def format_notice(self) -> str:
        """Format a human-readable in-progress compaction notice."""
        body = (
            f"Compacting history... saved {self.compacted_run_count}/{self.runs_before} runs: "
            f"{self.before_tokens:,} -> {self.after_tokens:,}"
        )
        if self.history_budget_tokens is not None:
            body += f" / {self.history_budget_tokens:,} history budget"
        if self.runs_remaining:
            body += f"\n   {self.runs_remaining} runs remaining"
        return body


@dataclass(frozen=True)
class CompactionLifecycleFailure:
    """Visible lifecycle notice payload emitted after failed foreground compaction."""

    notice_event_id: str | None
    mode: CompactionMode
    session_id: str
    scope: str
    summary_model: str
    status: CompactionLifecycleStatus
    duration_ms: int
    failure_reason: str
    history_budget_tokens: int | None


class CompactionLifecycle(Protocol):
    """Interface for ordered foreground compaction Matrix notice delivery."""

    async def start(self, event: CompactionLifecycleStart) -> str | None:
        """Send the initial compaction notice and return its Matrix event id."""

    async def complete_success(self, outcome: CompactionOutcome) -> None:
        """Edit the lifecycle notice after successful compaction."""

    async def progress(self, event: CompactionLifecycleProgress) -> None:
        """Edit the lifecycle notice after persisted compaction progress."""

    async def complete_failure(self, event: CompactionLifecycleFailure) -> None:
        """Edit the lifecycle notice after failed compaction."""


@dataclass(frozen=True)
class CompactionOutcome:
    """Completed compaction result used for lifecycle notices and tests."""

    mode: CompactionMode
    session_id: str
    scope: str
    summary: str
    summary_model: str
    before_tokens: int
    after_tokens: int
    window_tokens: int
    threshold_tokens: int
    runs_before: int
    runs_after: int
    compacted_run_count: int
    compacted_at: str
    history_budget_tokens: int | None = None
    lifecycle_notice_event_id: str | None = None
    duration_ms: int | None = None

    def to_notice_metadata(self) -> dict[str, object]:
        """Return serialized notice metadata for Matrix compaction messages."""
        meta: dict[str, object] = {
            "version": 3,
            "status": "success",
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
        if self.threshold_tokens:
            meta["threshold_tokens"] = self.threshold_tokens
        if self.lifecycle_notice_event_id is not None:
            meta["lifecycle_notice_event_id"] = self.lifecycle_notice_event_id
        if self.duration_ms is not None:
            meta["duration_ms"] = self.duration_ms
        return meta

    def format_notice(self) -> str:
        """Format a human-readable compaction notice."""
        line1 = (
            f"\U0001f4e6 Compacted {self.compacted_run_count} runs: {self.before_tokens:,} \u2192 {self.after_tokens:,}"
        )
        if self.history_budget_tokens is not None:
            line1 += f" / {self.history_budget_tokens:,} history budget"
        return line1


@dataclass(frozen=True)
class NativeHistory:
    """What a run's requests carry for provider-managed history: the native compaction replay and portable replay.

    ``native_compaction`` is None when the provider compacts nothing for this run; ``portable_replay`` says the
    request replays its history explicitly instead of continuing provider-stored state.
    """

    native_compaction: NativeCompactionReplay | None
    portable_replay: bool

    def without_native_compaction(self) -> NativeHistory:
        """Return these settings with the provider's compaction turned off, keeping the replay policy."""
        return NativeHistory(native_compaction=None, portable_replay=self.portable_replay)


@dataclass(frozen=True, kw_only=True)
class PreparedHistoryState:
    """Prepared persisted-history state for one run.

    ``native_history`` has no default, so a preparer states it: None when the provider route manages no history of
    its own.
    """

    replay_plan: ResolvedReplayPlan | None = None
    replays_persisted_history: bool = False
    compaction_decision: CompactionDecision = field(
        default_factory=lambda: CompactionDecision(mode="none", reason="unclassified"),
    )
    compaction_reply_outcome: CompactionReplyOutcome = "none"
    prepared_context_tokens: int | None = None
    # The system prompt and tools tokens the run's requests were sized with, when its settings replay the scope's whole
    # history; a run whose final plan replays all of it publishes them, so a later reply can take them off the request
    # size the provider reports.
    non_prompt_tokens: int | None = None
    # Whether history was sized from the latest answer's reported request size, which then sizes the run's first
    # request too.
    reported_history: bool = False
    native_history: NativeHistory | None
