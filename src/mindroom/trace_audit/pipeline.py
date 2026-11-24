"""Stateful passive trace-audit pass: Stage 1 scan, Stage 2 audit, cross-thread clustering, and promotion gate."""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

from mindroom.durable_write import write_json_file_durable
from mindroom.logging_config import get_logger
from mindroom.trace_audit.auditor import audit_evidence_card, build_evidence_card
from mindroom.trace_audit.models import (
    AuditedSession,
    AuditFinding,
    AuditJudgeFn,
    AuditPassResult,
    FailureCluster,
    FindingRecord,
    Incident,
    NominateClusterFn,
    TraceAuditSettings,
)
from mindroom.trace_audit.signals import detect_session_incidents
from mindroom.trace_audit.source import load_audited_sessions, load_removed_run_ids

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Set as AbstractSet
    from pathlib import Path

logger = get_logger(__name__)

_STATE_VERSION = 1
_SECONDS_PER_DAY = 86_400.0
_ACTIONABLE_VERDICTS = frozenset({"actionable", "unverified_claim"})
_MAX_CLUSTER_EXCERPTS = 5
_METRIC_KEYS = (
    "passes",
    "scanned_sessions",
    "scanned_runs",
    "incidents_detected",
    "incidents_audited",
    "clusters_promoted",
)


class _AuditState(BaseModel):
    version: int = _STATE_VERSION
    session_cursors: dict[str, int] = Field(default_factory=dict)
    findings: list[FindingRecord] = Field(default_factory=list)
    cooldowns: dict[str, float] = Field(default_factory=dict)
    metrics: dict[str, int] = Field(default_factory=lambda: dict.fromkeys(_METRIC_KEYS, 0))


def _load_state(state_path: Path) -> _AuditState:
    if not state_path.exists():
        return _AuditState()
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        state = _AuditState.model_validate(raw)
    except (OSError, ValueError, ValidationError):
        logger.warning("trace_audit_state_corrupt", path=str(state_path))
        return _AuditState()
    for key in _METRIC_KEYS:
        state.metrics.setdefault(key, 0)
    return state


def _save_state(state_path: Path, state: _AuditState) -> None:
    write_json_file_durable(state_path, state.model_dump(), indent=2, sort_keys=True, trailing_newline=True)


def _prune_state(state: _AuditState, *, now: float, settings: TraceAuditSettings) -> None:
    window_cutoff = now - settings.window_days * _SECONDS_PER_DAY
    deduped = {item.run_id: item for item in state.findings if item.created_at >= window_cutoff}
    state.findings = sorted(deduped.values(), key=lambda f: (f.created_at, f.run_id))[-settings.max_stored_findings :]
    cooldown_cutoff = now - max(settings.window_days, settings.cooldown_days) * _SECONDS_PER_DAY
    state.cooldowns = {k: ts for k, ts in state.cooldowns.items() if ts >= cooldown_cutoff}


def _effective_scope(scope_key: str, room_id: str | None) -> str:
    return f"{scope_key}::{room_id}" if room_id and room_id not in scope_key else scope_key


def _cluster_key(scope: str, finding: AuditFinding, *, tool_name: str, operation: str, kind: str) -> str:
    if tool_name and finding.verdict != "unverified_claim":
        return f"tool|{scope}|{tool_name}|{operation}|{kind}|{finding.remedy}"
    return f"non_tool|{scope}|{finding.category}|{finding.remedy}|{finding.target_skill or ''}"


def _build_clusters(
    findings: list[FindingRecord],
    *,
    min_cluster_sessions: int,
    allowed_scope: str | None = None,
) -> list[FailureCluster]:
    grouped: dict[str, list[FindingRecord]] = defaultdict(list)
    for item in findings:
        if allowed_scope is not None and item.scope_key != allowed_scope:
            continue
        if item.verdict in _ACTIONABLE_VERDICTS and item.teachable:
            grouped[item.cluster_key].append(item)
    clusters: list[FailureCluster] = []
    for key, group in grouped.items():
        sids = tuple(sorted({item.session_id for item in group}))
        if len(sids) < min_cluster_sessions:
            continue
        latest = max(group, key=lambda item: (item.created_at, item.run_id))
        clusters.append(
            FailureCluster(
                cluster_key=key,
                scope_key=latest.scope_key,
                tool_name=latest.tool_name,
                operation=latest.operation,
                kind=latest.kind,
                diagnostic_family=latest.diagnostic_family,
                category=latest.category,
                remedy=latest.remedy,
                target_skill=latest.target_skill,
                distinct_sessions=len(sids),
                occurrence_count=len(group),
                session_ids=sids,
                root_causes=tuple(dict.fromkeys(i.root_cause for i in group if i.root_cause))[:_MAX_CLUSTER_EXCERPTS],
                proposed_rules=tuple(dict.fromkeys(i.proposed_rule for i in group if i.proposed_rule))[
                    :_MAX_CLUSTER_EXCERPTS
                ],
                has_verified_fix=any(i.has_verified_fix for i in group),
                latest_created_at=latest.created_at,
                latest_session_id=latest.session_id,
            ),
        )
    clusters.sort(key=lambda c: (-c.distinct_sessions, -c.occurrence_count, -c.latest_created_at, c.cluster_key))
    return clusters


def _scan_sessions(
    sessions: list[AuditedSession],
    cursors: dict[str, int],
    *,
    now: float,
    cfg: TraceAuditSettings,
) -> tuple[int, int, dict[str, int], list[tuple[Incident, AuditedSession]]]:
    scanned_sessions, scanned_runs = 0, 0
    max_seq: dict[str, int] = {}
    candidates: list[tuple[Incident, AuditedSession]] = []
    for session in sessions:
        if not session.runs or now - session.last_updated_at < cfg.min_idle_seconds:
            continue
        last_seq = cursors.get(session.session_id, 0)
        new_runs = [run for run in session.runs if run.seq > last_seq]
        if not new_runs:
            continue
        scanned_sessions += 1
        scanned_runs += len(new_runs)
        max_seq[session.session_id] = max(run.seq for run in new_runs)
        found = detect_session_incidents(
            session,
            now=now,
            min_idle_seconds=cfg.min_idle_seconds,
            after_seq=last_seq,
        )
        candidates.extend((inc, session) for inc in found)
    return scanned_sessions, scanned_runs, max_seq, candidates


async def _audit_selected(
    candidates: list[tuple[Incident, AuditedSession]],
    *,
    judge_fn: AuditJudgeFn,
    scope_key: str,
    cfg: TraceAuditSettings,
    allowed_room_id: str | None = None,
) -> tuple[list[FindingRecord], dict[str, int]]:
    cap = max(0, cfg.max_audits_per_run)
    selected = candidates[:cap]
    deferred_min_seq: dict[str, int] = {}

    def _defer(inc: Incident) -> None:
        prev = deferred_min_seq.get(inc.session_id)
        if prev is None or inc.run_seq < prev:
            deferred_min_seq[inc.session_id] = inc.run_seq

    for inc, _ in candidates[cap:]:
        _defer(inc)

    sem = asyncio.Semaphore(max(1, cfg.audit_concurrency))

    async def _audit_one(inc: Incident, session: AuditedSession) -> FindingRecord | None:
        try:
            async with sem:
                finding = await audit_evidence_card(build_evidence_card(inc, session), judge_fn=judge_fn)
        except Exception as exc:
            logger.warning(
                "trace_audit_card_failed",
                session_id=inc.session_id,
                run_id=inc.run_id,
                error=str(exc),
            )
            _defer(inc)
            return None
        sig = (
            max(Counter(f.signature for f in inc.failures).items(), key=lambda kv: kv[1])[0]
            if inc.failures
            else ("", "", "", "")
        )
        tool_name, operation, kind, family = sig
        eff_scope = _effective_scope(scope_key, session.room_id or allowed_room_id)
        return FindingRecord(
            cluster_key=_cluster_key(eff_scope, finding, tool_name=tool_name, operation=operation, kind=kind),
            scope_key=eff_scope,
            session_id=inc.session_id,
            run_id=inc.run_id,
            created_at=inc.created_at,
            verdict=finding.verdict,
            teachable=finding.teachable,
            category=finding.category,
            remedy=finding.remedy,
            tool_name=tool_name,
            operation=operation,
            kind=kind,
            diagnostic_family=family,
            root_cause=finding.root_cause,
            proposed_rule=finding.proposed_rule,
            has_verified_fix=finding.has_verified_fix,
            target_skill=finding.target_skill,
        )

    raw_records = await asyncio.gather(*(_audit_one(inc, session) for inc, session in selected))
    records = [record for record in raw_records if record is not None]
    return records, deferred_min_seq


async def _promote_clusters(
    clusters: list[FailureCluster],
    *,
    state: _AuditState,
    state_path: Path,
    now: float,
    cfg: TraceAuditSettings,
    nominate_fn: NominateClusterFn | None,
) -> tuple[list[FailureCluster], list[Any]]:
    cooldown_sec = cfg.cooldown_days * _SECONDS_PER_DAY
    promoted: list[FailureCluster] = []
    results: list[Any] = []
    for cluster in clusters:
        if len(promoted) >= cfg.max_nominations_per_run:
            break
        last = state.cooldowns.get(cluster.cluster_key)
        if last is not None and (now - last) < cooldown_sec:
            continue
        state.cooldowns[cluster.cluster_key] = now
        state.metrics["clusters_promoted"] += 1
        # Persist cooldown before invoking the learner so a crash never causes blind retry loops.
        _save_state(state_path, state)
        promoted.append(cluster)
        if nominate_fn is not None:
            results.append(await nominate_fn(cluster))
    if not promoted:
        _save_state(state_path, state)
    return promoted, results


async def run_audit_pass(
    *,
    db_path: Path,
    storage_name: str,
    state_path: Path,
    scope_key: str,
    judge_fn: AuditJudgeFn | None,
    now: float | None = None,
    allowed_room_id: str | None = None,
    excluded_sessions: AbstractSet[str] = frozenset(),
    contact_session_check: Callable[[str], bool] | None = None,
    settings: TraceAuditSettings | None = None,
    nominate_fn: NominateClusterFn | None = None,
) -> AuditPassResult:
    """Execute one passive trace-audit pass: Stage 1 scan, Stage 2 audit, clustering, and promotion."""
    cfg = settings or TraceAuditSettings()
    current_ts = time.time() if now is None else now
    state = _load_state(state_path)
    _prune_state(state, now=current_ts, settings=cfg)

    since_ts = current_ts - cfg.window_days * _SECONDS_PER_DAY
    cursor_snapshot = dict(state.session_cursors)

    def _load_and_scan() -> tuple[int, int, dict[str, int], list[tuple[Incident, AuditedSession]], frozenset[str]]:
        removed_ids = load_removed_run_ids(db_path, storage_name)
        sessions = load_audited_sessions(
            db_path,
            storage_name,
            since_ts=since_ts,
            excluded_sessions=excluded_sessions,
            allowed_room_id=allowed_room_id,
            contact_session_check=contact_session_check,
        )
        scanned_s, scanned_r, max_s, cands = _scan_sessions(
            sessions,
            cursor_snapshot,
            now=current_ts,
            cfg=cfg,
        )
        return scanned_s, scanned_r, max_s, cands, removed_ids

    scanned_sessions, scanned_runs, max_seq, candidates, removed_ids = await asyncio.to_thread(_load_and_scan)
    if removed_ids:
        state.findings = [f for f in state.findings if f.run_id not in removed_ids]
    already_audited = {f.run_id for f in state.findings}
    candidates = [pair for pair in candidates if pair[0].run_id not in already_audited]
    new_findings, deferred_min_seq = (
        await _audit_selected(
            candidates,
            judge_fn=judge_fn,
            scope_key=scope_key,
            cfg=cfg,
            allowed_room_id=allowed_room_id,
        )
        if judge_fn is not None
        else ([], {})
    )
    if judge_fn is not None:
        state.findings.extend(new_findings)
        for sid, seq in max_seq.items():
            state.session_cursors[sid] = (
                max(state.session_cursors.get(sid, 0), deferred_min_seq[sid] - 1) if sid in deferred_min_seq else seq
            )
    _prune_state(state, now=current_ts, settings=cfg)
    for key, delta in (
        ("passes", 1),
        ("scanned_sessions", scanned_sessions),
        ("scanned_runs", scanned_runs),
        ("incidents_detected", len(candidates)),
        ("incidents_audited", len(new_findings)),
    ):
        state.metrics[key] += delta

    allowed_scope = _effective_scope(scope_key, allowed_room_id) if allowed_room_id is not None else None
    actionable = _build_clusters(
        state.findings,
        min_cluster_sessions=cfg.min_cluster_sessions,
        allowed_scope=allowed_scope,
    )
    promoted, nomination_results = await _promote_clusters(
        actionable,
        state=state,
        state_path=state_path,
        now=current_ts,
        cfg=cfg,
        nominate_fn=nominate_fn,
    )
    return AuditPassResult(
        scanned_sessions=scanned_sessions,
        scanned_runs=scanned_runs,
        incidents_detected=len(candidates),
        incidents_audited=len(new_findings),
        findings=tuple(new_findings),
        actionable_clusters=tuple(actionable),
        promoted_clusters=tuple(promoted),
        nomination_results=tuple(nomination_results),
    )
