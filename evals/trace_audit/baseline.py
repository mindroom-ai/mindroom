"""Baselines A (observed usage), B (simulated reply-interval), Stage 1 breakdown, and USD pricing."""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.budgets.pricing import PricedModel, cost_usd, run_cost_usd
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.llm.usage import TokenCounts
from mindroom.session_ids import session_in_room
from mindroom.session_storage.schema import TableNames, quote_identifier, table_names
from mindroom.skill_learning.transcript import count_model_replies
from mindroom.trace_audit.signals import classify_tool_execution, detect_session_incidents
from mindroom.trace_audit.source import latest_run_timestamp, load_audited_sessions, load_usage_rows
from mindroom.usage.legacy_usage_storage import current_provider_name
from mindroom.usage.storage import stored_token_counts

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from collections.abc import Set as AbstractSet
    from pathlib import Path

    from mindroom.llm.usage import HelperUsage
    from mindroom.skill_learning.audit_reviewer import ReviewOutcome
    from mindroom.trace_audit.models import AuditedSession

_CHUNK_SIZE = 500
_MIN_BASELINE_RUNS = 20
_TOP_ERROR_BUCKETS = 15


def _safe_counts(raw: object) -> TokenCounts:
    if not isinstance(raw, Mapping):
        return TokenCounts()
    try:
        return stored_token_counts(raw) or TokenCounts()
    except ValueError:
        return TokenCounts()


def _has_tokens(c: TokenCounts) -> bool:
    return any((c.input_tokens, c.output_tokens, c.cache_read_tokens, c.cache_write_tokens))


@dataclass(slots=True)
class CostAccumulator:
    """In-memory token and USD spend accumulator using MindRoom's config price table."""

    prices: Mapping[tuple[str, str], PricedModel]
    calls: int = 0
    tokens: TokenCounts = field(default_factory=TokenCounts)
    usd: float = 0.0
    unpriced_calls: int = 0

    def _record(self, totals: TokenCounts, spent: float | None) -> None:
        self.calls += 1
        self.tokens = self.tokens + totals
        if spent is not None:
            self.usd += spent
        elif _has_tokens(totals):
            self.unpriced_calls += 1

    def add_usage_row(self, row: Mapping[str, Any]) -> None:
        """Accumulate one ``_usage`` ledger row dict."""
        metrics = row.get("metrics") if isinstance(row.get("metrics"), Mapping) else {}
        totals = _safe_counts(metrics)
        details = metrics.get("details") if isinstance(metrics.get("details"), Mapping) else {}
        entries = [
            ((current_provider_name(e["provider"]), e["id"]), _safe_counts(e))
            for group in details.values()
            if isinstance(group, list)
            for e in group
            if isinstance(e, Mapping) and isinstance(e.get("provider"), str) and isinstance(e.get("id"), str)
        ]
        if not entries and isinstance(row.get("model_provider"), str) and isinstance(row.get("model"), str):
            entries = [((current_provider_name(row["model_provider"]), row["model"]), totals)]
        row_cost: float | None = None
        for key, counts in entries:
            if (priced := self.prices.get(key)) is not None and _has_tokens(counts):
                row_cost = (row_cost or 0.0) + cost_usd(counts, priced)
        self._record(totals, row_cost)

    def add_helper_usage(self, usage: HelperUsage) -> None:
        """Accumulate one ``HelperUsage`` emitted by ``complete_prompt``."""
        provider = current_provider_name(usage.provider) if usage.provider is not None else None
        spent = run_cost_usd(usage.metrics, model_provider=provider, model_id=usage.model, prices=self.prices)
        self._record(usage.metrics.tokens, spent)

    def add_review_outcome(self, outcome: ReviewOutcome) -> None:
        """Accumulate one ``ReviewOutcome`` emitted by ``review_audit_cluster``."""
        provider = current_provider_name(outcome.provider)
        if outcome.metrics is not None:
            totals = outcome.metrics.tokens
            spent = run_cost_usd(
                outcome.metrics,
                model_provider=provider,
                model_id=outcome.model_id,
                prices=self.prices,
            )
        else:
            totals = TokenCounts(input_tokens=outcome.input_tokens, output_tokens=outcome.output_tokens)
            priced = self.prices.get((provider, outcome.model_id))
            spent = cost_usd(totals, priced) if priced is not None and _has_tokens(totals) else None
        self._record(totals, spent)

    def to_dict(self) -> dict[str, int | float]:
        """Return an aggregate token and rounded USD spend dictionary."""
        return {
            "calls": self.calls,
            "input_tokens": self.tokens.input_tokens,
            "output_tokens": self.tokens.output_tokens,
            "cache_read_tokens": self.tokens.cache_read_tokens,
            "cache_write_tokens": self.tokens.cache_write_tokens,
            "usd": round(self.usd, 6),
            "unpriced_calls": self.unpriced_calls,
        }


@dataclass(frozen=True, slots=True)
class Stage1Breakdown:
    """Deterministic Stage 1 friction sets and aggregate-only summary dict."""

    friction_sessions: frozenset[str]
    repeat_error_sessions: frozenset[str]
    summary: dict[str, Any]


def compute_stage1_breakdown(
    sessions: Sequence[AuditedSession],
    *,
    now: float,
    min_idle_seconds: int = 600,
) -> Stage1Breakdown:
    """Compute Stage 1 friction sets, reason counts, tool outcomes, and top error buckets at zero LLM cost."""
    tool_counts = [len(run.tools) for s in sessions for run in s.runs]
    median = float(statistics.median(tool_counts)) if len(tool_counts) >= _MIN_BASELINE_RUNS else None
    friction: set[str] = set()
    repeat: set[str] = set()
    reasons: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    buckets: Counter[tuple[str, str, str]] = Counter()
    incidents = 0
    for session in sessions:
        for run in session.runs:
            for tool in run.tools:
                out = classify_tool_execution(tool)
                outcomes[out.kind] += 1
                if out.is_failure:
                    buckets[(out.tool_name, out.operation, out.kind)] += 1
        for inc in detect_session_incidents(
            session,
            now=now,
            min_idle_seconds=min_idle_seconds,
            median_tools=median,
            baseline_run_count=len(tool_counts),
            sample_rate=0.0,
        ):
            if inc.is_clean_sample:
                continue
            incidents += 1
            friction.add(session.session_id)
            reasons.update(inc.reasons)
            if "repeat_tool_error" in inc.reasons:
                repeat.add(session.session_id)
    summary: dict[str, Any] = {
        "total_sessions": len(sessions),
        "total_runs": len(tool_counts),
        "total_tools": sum(tool_counts),
        "friction_sessions_count": len(friction),
        "repeat_error_sessions_count": len(repeat),
        "incident_count": incidents,
        "reason_counts": dict(sorted(reasons.items())),
        "tool_outcome_counts": dict(sorted(outcomes.items())),
        "top_error_buckets": [
            {"tool_name": t, "operation": op, "kind": k, "count": c}
            for (t, op, k), c in buckets.most_common(_TOP_ERROR_BUCKETS)
        ],
    }
    return Stage1Breakdown(frozenset(friction), frozenset(repeat), summary)


def _coverage_dict(
    reviews: int,
    reviewed: AbstractSet[str],
    zero_friction: int,
    friction: AbstractSet[str],
    repeat: AbstractSet[str],
) -> dict[str, Any]:
    return {
        "review_count": reviews,
        "reviewed_sessions": len(reviewed),
        "zero_friction_reviews": zero_friction,
        "zero_friction_rate": round(zero_friction / reviews, 6) if reviews else 0.0,
        "friction_sessions_total": len(friction),
        "friction_sessions_covered": len(friction & reviewed),
        "friction_sessions_missed": len(friction - reviewed),
        "repeat_error_sessions_total": len(repeat),
        "repeat_error_sessions_covered": len(repeat & reviewed),
        "repeat_error_sessions_missed": len(repeat - reviewed),
    }


def compute_baseline_a(
    db_path: Path,
    storage_name: str,
    *,
    prices: Mapping[tuple[str, str], PricedModel],
    sessions: Sequence[AuditedSession],
    friction_sessions: AbstractSet[str],
    repeat_error_sessions: AbstractSet[str],
    since_ts: float | None = None,
    until_ts: float | None = None,
    allowed_room_id: str | None = None,
    contact_session_check: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Compute observed Baseline A metrics from ``kind='skill_learning'`` usage rows."""
    rows = load_usage_rows(db_path, storage_name, kind="skill_learning", since_ts=since_ts, until_ts=until_ts)
    if contact_session_check is not None:
        rows = [r for r in rows if not contact_session_check(str(r["session_id"]))]
    if allowed_room_id is not None:
        known = {s.session_id for s in sessions}
        rows = [r for r in rows if r["session_id"] in known or session_in_room(str(r["session_id"]), allowed_room_id)]
    acc = CostAccumulator(prices)
    reviewed: set[str] = set()
    zero_friction = 0
    for row in rows:
        acc.add_usage_row(row)
        sid = str(row["session_id"])
        reviewed.add(sid)
        zero_friction += sid not in friction_sessions
    return {
        **_coverage_dict(len(rows), reviewed, zero_friction, friction_sessions, repeat_error_sessions),
        "cost": acc.to_dict(),
    }


def _load_unfolded_messages_ro(db_path: Path, storage_name: str, run_ids: Sequence[str]) -> dict[str, list[Message]]:
    if not run_ids or not db_path.exists():
        return {}
    names = TableNames(storage_name)
    by_run: dict[str, list[Message]] = defaultdict(list)
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        conn.execute("PRAGMA query_only = ON")
        if names.log_messages not in table_names(conn):
            return {}
        quoted = quote_identifier(names.log_messages)
        for start in range(0, len(run_ids), _CHUNK_SIZE):
            chunk = list(run_ids[start : start + _CHUNK_SIZE])
            holders = ", ".join("?" for _ in chunk)
            for rid, raw in conn.execute(
                f"SELECT run_id, data FROM {quoted} WHERE folded_by IS NULL AND run_id IN ({holders}) ORDER BY run_id, ord",  # noqa: S608
                chunk,
            ):
                try:
                    if isinstance(payload := json.loads(raw), dict):
                        by_run[str(rid)].append(Message.from_dict(payload))
                except (KeyError, TypeError, ValueError):
                    continue
    finally:
        conn.close()
    return by_run


def compute_baseline_b(
    db_path: Path,
    storage_name: str,
    *,
    sessions: Sequence[AuditedSession],
    friction_sessions: AbstractSet[str],
    repeat_error_sessions: AbstractSet[str],
    review_interval: int = 10,
) -> dict[str, Any]:
    """Replay ``count_model_replies`` chronologically across eligible completed runs."""
    runs = sorted((r for s in sessions for r in s.runs), key=lambda r: (r.created_at, r.seq, r.session_id, r.run_id))
    messages_by_run = _load_unfolded_messages_ro(db_path, storage_name, [r.run_id for r in runs])
    counters: dict[str, int] = defaultdict(int)
    reviewed: set[str] = set()
    eligible_runs = total_replies = reviews = zero_friction = 0
    for run in runs:
        try:
            status = RunStatus(run.status)
        except ValueError:
            continue
        eligible_runs += status == RunStatus.completed
        rec = RunRecord(
            run_id=run.run_id,
            session_id=run.session_id,
            parent_run_id=None,
            status=status,
            messages=messages_by_run.get(run.run_id, []),
        )
        replies, restarted = count_model_replies(rec)
        total_replies += replies
        total = replies if restarted else counters[run.session_id] + replies
        if total >= review_interval:
            counters[run.session_id] = 0
            reviews += 1
            reviewed.add(run.session_id)
            zero_friction += run.session_id not in friction_sessions
        else:
            counters[run.session_id] = total
    return {
        "review_interval": review_interval,
        "eligible_runs": eligible_runs,
        "total_model_replies": total_replies,
        **_coverage_dict(reviews, reviewed, zero_friction, friction_sessions, repeat_error_sessions),
    }


_SECONDS_PER_DAY = 86_400.0


@dataclass(frozen=True, slots=True)
class ChronologicalSplit:
    """Chronological session partition by ``first_run.created_at`` with zero leakage."""

    snapshot_now: float
    window_start: float
    cutoff: float
    train_session_ids: frozenset[str]
    holdout_unseen_session_ids: frozenset[str]
    holdout_continuing_session_ids: frozenset[str]
    train_runs: int
    holdout_unseen_runs: int
    holdout_continuing_runs: int

    def to_dict(self) -> dict[str, Any]:
        """Return aggregate-only split counts and boundary timestamps."""
        return {
            "snapshot_now": self.snapshot_now,
            "window_start": self.window_start,
            "cutoff": self.cutoff,
            "train_sessions": len(self.train_session_ids),
            "train_runs": self.train_runs,
            "holdout_unseen_sessions": len(self.holdout_unseen_session_ids),
            "holdout_unseen_runs": self.holdout_unseen_runs,
            "holdout_continuing_sessions": len(self.holdout_continuing_session_ids),
            "holdout_continuing_runs": self.holdout_continuing_runs,
        }


def split_sessions_chronologically(
    all_sessions: Sequence[AuditedSession],
    *,
    window_days: int,
    train_days: int,
) -> ChronologicalSplit:
    """Partition sessions by ``first_run.created_at`` into Train, Unseen Holdout, and Continuing Holdout."""
    run_ts = [max(r.created_at, r.updated_at) for s in all_sessions for r in s.runs]
    if not run_ts:
        return ChronologicalSplit(0.0, 0.0, 0.0, frozenset(), frozenset(), frozenset(), 0, 0, 0)
    snapshot_now = max(run_ts)
    window_start = snapshot_now - window_days * _SECONDS_PER_DAY
    cutoff = snapshot_now - (window_days - train_days) * _SECONDS_PER_DAY
    train_sids, unseen_sids, cont_sids = set(), set(), set()
    train_runs = unseen_runs = cont_runs = 0
    for s in all_sessions:
        first_ts = min(s.created_at, *(r.created_at for r in s.runs)) if s.runs else s.created_at
        tr = sum(window_start <= r.created_at and max(r.created_at, r.updated_at) <= cutoff for r in s.runs)
        ho = sum(
            cutoff < r.created_at <= snapshot_now and max(r.created_at, r.updated_at) <= snapshot_now for r in s.runs
        )
        if tr:
            train_sids.add(s.session_id)
            train_runs += tr
        if ho:
            if cutoff < first_ts <= snapshot_now:
                unseen_sids.add(s.session_id)
                unseen_runs += ho
            else:
                cont_sids.add(s.session_id)
                cont_runs += ho
    return ChronologicalSplit(
        snapshot_now,
        window_start,
        cutoff,
        frozenset(train_sids),
        frozenset(unseen_sids),
        frozenset(cont_sids),
        train_runs,
        unseen_runs,
        cont_runs,
    )


@dataclass(frozen=True, slots=True)
class WindowBaselines:
    """Loaded window sessions, chronological split, Stage 1 breakdown, and Baselines A & B."""

    sessions: list[AuditedSession]
    split: ChronologicalSplit
    stage1_now: float
    stage1: Stage1Breakdown
    baseline_a: dict[str, Any]
    baseline_b: dict[str, Any]


def evaluate_window_baselines(
    *,
    db_path: Path,
    agent_name: str,
    prices: Mapping[tuple[str, str], PricedModel],
    window_days: int,
    train_days: int,
    review_interval: int,
    min_idle_seconds: int = 600,
    allowed_room_id: str | None = None,
    contact_session_check: Callable[[str], bool] | None = None,
) -> WindowBaselines:
    """Load the evaluation window strictly read-only and compute chronological split, Stage 1, and Baselines A & B."""
    snapshot_now = latest_run_timestamp(db_path, agent_name)
    window_start = snapshot_now - window_days * _SECONDS_PER_DAY if snapshot_now > 0 else 0.0
    sessions = (
        load_audited_sessions(
            db_path,
            agent_name,
            since_ts=window_start,
            until_ts=snapshot_now,
            allowed_room_id=allowed_room_id,
            contact_session_check=contact_session_check,
        )
        if snapshot_now > 0
        else []
    )
    split = split_sessions_chronologically(sessions, window_days=window_days, train_days=train_days)
    stage1_now = max(split.snapshot_now, *(s.last_updated_at for s in sessions)) + min_idle_seconds if sessions else 0.0
    stage1 = compute_stage1_breakdown(sessions, now=stage1_now, min_idle_seconds=min_idle_seconds)
    base_a = compute_baseline_a(
        db_path,
        agent_name,
        prices=prices,
        sessions=sessions,
        friction_sessions=stage1.friction_sessions,
        repeat_error_sessions=stage1.repeat_error_sessions,
        since_ts=split.window_start if split.snapshot_now > 0 else None,
        until_ts=split.snapshot_now if split.snapshot_now > 0 else None,
        allowed_room_id=allowed_room_id,
        contact_session_check=contact_session_check,
    )
    base_b = compute_baseline_b(
        db_path,
        agent_name,
        sessions=sessions,
        friction_sessions=stage1.friction_sessions,
        repeat_error_sessions=stage1.repeat_error_sessions,
        review_interval=review_interval,
    )
    return WindowBaselines(sessions, split, stage1_now, stage1, base_a, base_b)
