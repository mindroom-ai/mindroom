"""Passive trace auditor: Stage 1 signal detection, read-only SQLite loading, Stage 2 auditing, and clustering."""

from __future__ import annotations

from mindroom.trace_audit.auditor import (
    audit_evidence_card,
    build_evidence_card,
    complete_tool_free_structured,
    make_llm_audit_judge,
)
from mindroom.trace_audit.models import (
    AuditCategory,
    AuditedRun,
    AuditedSession,
    AuditFinding,
    AuditJudgeFn,
    AuditPassResult,
    AuditRemedy,
    AuditVerdict,
    EvidenceCard,
    EvidenceSnippet,
    FailureCluster,
    FailureSequenceEntry,
    FindingRecord,
    Incident,
    IncidentReason,
    NominateClusterFn,
    RunMessageRecord,
    ToolExecutionRecord,
    ToolOutcome,
    ToolOutcomeKind,
    TraceAuditSettings,
)
from mindroom.trace_audit.pipeline import run_audit_pass
from mindroom.trace_audit.signals import classify_tool_execution, detect_session_incidents
from mindroom.trace_audit.source import load_audited_sessions

__all__ = [
    "AuditCategory",
    "AuditFinding",
    "AuditJudgeFn",
    "AuditPassResult",
    "AuditRemedy",
    "AuditVerdict",
    "AuditedRun",
    "AuditedSession",
    "EvidenceCard",
    "EvidenceSnippet",
    "FailureCluster",
    "FailureSequenceEntry",
    "FindingRecord",
    "Incident",
    "IncidentReason",
    "NominateClusterFn",
    "RunMessageRecord",
    "ToolExecutionRecord",
    "ToolOutcome",
    "ToolOutcomeKind",
    "TraceAuditSettings",
    "audit_evidence_card",
    "build_evidence_card",
    "classify_tool_execution",
    "complete_tool_free_structured",
    "detect_session_incidents",
    "load_audited_sessions",
    "make_llm_audit_judge",
    "run_audit_pass",
]
