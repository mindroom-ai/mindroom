"""Pure data models for the passive trace auditor."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

type ToolOutcomeKind = Literal[
    "ok",
    "benign_exit_1",
    "approval_denied",
    "interrupted",
    "tool_error",
    "tool_timeout",
    "nonzero_exit",
    "json_error",
    "text_error",
]
type IncidentReason = Literal[
    "run_error",
    "failing_tools",
    "repeat_tool_error",
    "multi_tool_error",
    "tool_loop",
    "single_tool_error",
    "settled_cancel",
    "user_correction",
    "tool_outlier",
    "clean_sample",
]
type AuditVerdict = Literal["actionable", "transient_env", "benign", "unverified_claim", "no_issue"]
type AuditCategory = Literal["tool_misuse", "verification_gap", "retry_loop", "user_preference", "skill_bug", "other"]
type AuditRemedy = Literal[
    "verify_outcome",
    "validate_input",
    "change_retry_strategy",
    "follow_user_constraint",
    "fix_skill",
    "use_correct_tool",
    "none",
]

MAX_ROOT_CAUSE_CHARS = 240
MAX_PROPOSED_RULE_CHARS = 280
_TARGET_SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _clip_line(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def sanitize_target_skill(value: str | None) -> str | None:
    """Allow only valid skill-name slugs, coercing empty or invalid values to ``None``."""
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned if _TARGET_SKILL_RE.match(cleaned) else None


@dataclass(frozen=True, slots=True)
class ToolExecutionRecord:
    """One recorded tool execution from a run log."""

    ord: int
    tool_call_id: str
    tool_name: str
    tool_args: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False
    result: str = ""
    confirmed: bool | None = None
    requires_confirmation: bool = False


@dataclass(frozen=True, slots=True)
class RunMessageRecord:
    """One stored message belonging to a run."""

    ord: int
    role: str
    content: str
    created_at: float | None = None
    add_to_agent_memory: bool = True
    from_history: bool = False
    is_synthetic: bool = False


@dataclass(frozen=True, slots=True)
class AuditedRun:
    """One terminal top-level agent run loaded for passive auditing."""

    run_id: str
    session_id: str
    seq: int
    status: str
    created_at: float
    updated_at: float
    duration_seconds: float | None = None
    error: str | None = None
    content: str = ""
    tools: tuple[ToolExecutionRecord, ...] = ()
    messages: tuple[RunMessageRecord, ...] = ()
    failing_tools: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    has_interrupted_checkpoint: bool = False


@dataclass(frozen=True, slots=True)
class AuditedSession:
    """One settled conversation session and its ordered terminal top-level runs."""

    session_id: str
    room_id: str | None
    created_at: float
    last_updated_at: float
    runs: tuple[AuditedRun, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """Deterministic Stage 1 classification of one tool execution."""

    tool_ord: int
    tool_name: str
    operation: str
    kind: ToolOutcomeKind
    diagnostic_family: str
    is_failure: bool
    error_excerpt: str = ""
    args_digest: str = ""

    @property
    def signature(self) -> tuple[str, str, ToolOutcomeKind, str]:
        """Return the stable failure signature tuple."""
        return (self.tool_name, self.operation, self.kind, self.diagnostic_family)


@dataclass(frozen=True, slots=True)
class Incident:
    """Stage 1 incident candidate selected for Stage 2 passive auditing."""

    session_id: str
    room_id: str | None
    run_id: str
    run_seq: int
    created_at: float
    window_run_ids: tuple[str, ...]
    reasons: tuple[IncidentReason, ...]
    is_clean_sample: bool = False
    failures: tuple[ToolOutcome, ...] = ()
    correction_text: str | None = None


@dataclass(frozen=True, slots=True)
class FailureSequenceEntry:
    """One bounded, redacted failure line inside an EvidenceCard."""

    ord: int
    tool_name: str
    operation: str
    kind: ToolOutcomeKind
    diagnostic_family: str
    truncated_error: str
    recovered: bool = False


@dataclass(frozen=True, slots=True)
class EvidenceSnippet:
    """One bounded, redacted conversation snippet inside an EvidenceCard."""

    label: str
    text: str


@dataclass(frozen=True, slots=True)
class EvidenceCard:
    """Bounded, redacted summary sent to the tool-free Stage 2 audit model."""

    session_id: str
    run_id: str
    run_status: str
    reasons: tuple[IncidentReason, ...]
    loaded_skills: tuple[str, ...]
    tool_count: int
    failure_sequence: tuple[FailureSequenceEntry, ...]
    snippets: tuple[EvidenceSnippet, ...]
    rendered: str


class AuditFinding(BaseModel):
    """Structured Stage 2 verdict returned by the tool-free audit completion."""

    verdict: AuditVerdict = Field(description="Whether the incident is actionable, transient, benign, or clean.")
    teachable: bool = Field(description="True only if a reusable procedural rule would prevent this mistake.")
    category: AuditCategory = Field(description="High-level failure category.")
    remedy: AuditRemedy = Field(description="Generalized procedural remedy class.")
    root_cause: str = Field(description="Generalized root cause summary (<= 240 chars, no secrets).")
    proposed_rule: str = Field(description="Reusable procedural check (<= 280 chars, no task-specific literals).")
    has_verified_fix: bool = Field(default=False, description="True if a later call in the session verified a fix.")
    target_skill: str | None = Field(default=None, description="Existing loaded skill name if applicable, else null.")

    @field_validator("root_cause")
    @classmethod
    def _clip_root_cause(cls, value: str) -> str:
        return _clip_line(value, MAX_ROOT_CAUSE_CHARS)

    @field_validator("proposed_rule")
    @classmethod
    def _clip_proposed_rule(cls, value: str) -> str:
        return _clip_line(value, MAX_PROPOSED_RULE_CHARS)

    @field_validator("target_skill")
    @classmethod
    def _normalize_target_skill(cls, value: str | None) -> str | None:
        return sanitize_target_skill(value)


class FindingRecord(BaseModel, frozen=True):
    """Content-free persisted metadata for one audited run."""

    cluster_key: str
    scope_key: str
    session_id: str
    run_id: str
    created_at: float
    verdict: AuditVerdict
    teachable: bool
    category: AuditCategory
    remedy: AuditRemedy
    tool_name: str = ""
    operation: str = ""
    kind: str = ""
    diagnostic_family: str = ""
    root_cause: str = ""
    proposed_rule: str = ""
    has_verified_fix: bool = False
    target_skill: str | None = None

    @field_validator("target_skill")
    @classmethod
    def _normalize_target_skill(cls, value: str | None) -> str | None:
        return sanitize_target_skill(value)


@dataclass(frozen=True, slots=True)
class FailureCluster:
    """Cross-thread cluster of recurring teachable findings eligible for skill promotion."""

    cluster_key: str
    scope_key: str
    tool_name: str
    operation: str
    kind: str
    diagnostic_family: str
    category: AuditCategory
    remedy: AuditRemedy
    target_skill: str | None
    distinct_sessions: int
    occurrence_count: int
    session_ids: tuple[str, ...]
    root_causes: tuple[str, ...]
    proposed_rules: tuple[str, ...]
    has_verified_fix: bool
    latest_created_at: float
    latest_session_id: str


@dataclass(frozen=True, slots=True)
class TraceAuditSettings:
    """Tuning parameters for one passive trace-audit pass."""

    min_idle_seconds: int = 600
    sample_rate: float = 0.02
    max_clean_samples_per_run: int = 2
    max_audits_per_run: int = 12
    min_cluster_sessions: int = 2
    cooldown_days: int = 14
    window_days: int = 30
    max_nominations_per_run: int = 1
    max_stored_findings: int = 300
    audit_concurrency: int = 1


@dataclass(frozen=True, slots=True)
class AuditPassResult:
    """Summary returned by ``run_audit_pass``."""

    scanned_sessions: int
    scanned_runs: int
    incidents_detected: int
    incidents_audited: int
    clean_samples_audited: int
    findings: tuple[FindingRecord, ...]
    actionable_clusters: tuple[FailureCluster, ...]
    promoted_clusters: tuple[FailureCluster, ...]
    nomination_results: tuple[Any, ...] = ()


type AuditJudgeFn = Callable[[EvidenceCard], Awaitable[AuditFinding]]
type NominateClusterFn = Callable[[FailureCluster], Awaitable[Any]]
