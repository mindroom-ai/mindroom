"""Offline chronological backtest CLI for passive trace auditing and friction-triggered skill learning."""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import math
import shutil
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evals.trace_audit.baseline import (
    CostAccumulator,
    compute_baseline_a,
    compute_baseline_b,
    compute_stage1_breakdown,
)
from evals.trace_audit.judge import (
    CANDIDATE_ARMS,
    DEFAULT_NOOP_GUIDANCE,
    DEFAULT_PLACEBO_SKILL,
    BlindedJudgeCompleteFn,
    JudgeCase,
    evaluate_blinded_cases,
    write_private_json,
)
from mindroom.budgets.pricing import model_prices
from mindroom.config.main import load_config
from mindroom.constants import resolve_runtime_paths
from mindroom.skill_learning.reviewer import ReviewOutcome, review_audit_cluster
from mindroom.skill_learning.tools import ReviewProgress
from mindroom.tool_system.skills import SKILL_FILENAME, parse_skill_markdown
from mindroom.trace_audit.auditor import make_llm_audit_judge
from mindroom.trace_audit.models import (
    AuditedSession,
    AuditFinding,
    AuditJudgeFn,
    EvidenceCard,
    FailureCluster,
    FindingRecord,
    TraceAuditSettings,
)
from mindroom.trace_audit.pipeline import run_audit_pass
from mindroom.trace_audit.source import load_audited_sessions

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.llm.usage import HelperUsage

_SECONDS_PER_DAY = 86_400.0
_MIN_IDLE_SECONDS = 600
_ACTIONABLE = frozenset({"actionable", "unverified_claim"})

type AuditJudgeFactory = Callable[[Callable[[HelperUsage], Any]], AuditJudgeFn]
type LearnerFn = Callable[..., Awaitable[ReviewOutcome]]


@dataclass(frozen=True, slots=True)
class BacktestOptions:
    """Configuration options for one offline chronological trace-audit backtest."""

    db_path: Path
    config_path: Path
    agent_name: str
    out_dir: Path
    skills_dir: Path | None = None
    window_days: int = 30
    train_days: int = 20
    audit_model: str | None = None
    judge_model: str | None = None
    room_id: str | None = None
    review_interval: int = 10
    min_cluster_sessions: int = 2
    max_audits_train: int = 60
    max_audits_holdout: int = 40
    seed: int = 23
    dry_run: bool = False


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
    run_ts = [r.created_at for s in all_sessions for r in s.runs]
    if not run_ts:
        return ChronologicalSplit(0.0, 0.0, 0.0, frozenset(), frozenset(), frozenset(), 0, 0, 0)
    snapshot_now = max(run_ts)
    window_start = snapshot_now - window_days * _SECONDS_PER_DAY
    cutoff = snapshot_now - (window_days - train_days) * _SECONDS_PER_DAY
    train_sids, unseen_sids, cont_sids = set(), set(), set()
    train_runs = unseen_runs = cont_runs = 0
    for s in all_sessions:
        first_ts = min(r.created_at for r in s.runs)
        tr = sum(window_start <= r.created_at <= cutoff for r in s.runs)
        ho = sum(cutoff < r.created_at <= snapshot_now for r in s.runs)
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


def _read_skills_snapshot(skills_root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    if not skills_root.exists():
        return snapshot
    for child in sorted(skills_root.iterdir()):
        skill_file = child / SKILL_FILENAME
        if not child.is_dir() or not skill_file.is_file():
            continue
        text = skill_file.read_text(encoding="utf-8")
        try:
            raw = parse_skill_markdown(text)[0].get("name")
            name = raw.strip() if isinstance(raw, str) and raw.strip() else child.name
        except Exception:
            name = child.name
        snapshot[name] = text
    return snapshot


def _summarize_findings(
    findings: Sequence[FindingRecord],
    *,
    trained_act: frozenset[str] = frozenset(),
    trained_pro: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    teachable = [f for f in findings if f.verdict in _ACTIONABLE and f.teachable]
    rec_act = [f for f in teachable if f.cluster_key in trained_act]
    rec_pro = [f for f in teachable if f.cluster_key in trained_pro]
    return {
        "audited": len(findings),
        "teachable": len(teachable),
        "verdict_counts": dict(sorted(Counter(f.verdict for f in findings).items())),
        "category_counts": dict(sorted(Counter(f.category for f in teachable).items())),
        "remedy_counts": dict(sorted(Counter(f.remedy for f in teachable).items())),
        "recurring_actionable_findings": len(rec_act),
        "recurring_actionable_sessions": len({f.session_id for f in rec_act}),
        "recurring_promoted_findings": len(rec_pro),
        "recurring_promoted_sessions": len({f.session_id for f in rec_pro}),
    }


def _match_diff(f: FindingRecord, diffs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    ranks = (
        lambda d: d["cluster_key"] == f.cluster_key,
        lambda d: f.target_skill is not None and d["skill_name"] == f.target_skill,
        lambda d: bool(f.tool_name) and (d["tool_name"], d["operation"]) == (f.tool_name, f.operation),
        lambda d: bool(f.tool_name) and d["tool_name"] == f.tool_name,
        lambda d: (d["category"], d["remedy"]) == (f.category, f.remedy),
    )
    return next((d for pred in ranks for d in diffs if pred(d)), diffs[0])


def _build_judge_cases(
    holdout_findings: Sequence[FindingRecord],
    holdout_cards: Mapping[str, EvidenceCard],
    *,
    diffs: Sequence[dict[str, Any]],
    initial_skills: Mapping[str, str],
    frozen_skills: Mapping[str, str],
    seed: int,
) -> list[JudgeCase]:
    if not diffs:
        return []
    touched = {d["skill_name"] for d in diffs}
    cases: list[JudgeCase] = []
    for f in holdout_findings:
        if f.verdict not in _ACTIONABLE or not f.teachable:
            continue
        matched = _match_diff(f, diffs)
        skill = matched["skill_name"]
        if not (proposed := frozen_skills.get(skill)):
            continue
        excluded = touched | {skill} | ({f.target_skill} if f.target_skill else set())
        pool = [text for name, text in sorted(initial_skills.items()) if name not in excluded]
        digest = hashlib.sha256(f"placebo:{seed}:{f.run_id}".encode()).digest()
        placebo = pool[int.from_bytes(digest[:8], "big") % len(pool)] if pool else DEFAULT_PLACEBO_SKILL
        card = holdout_cards.get(f.run_id)
        failure_parts = [
            f"Category: {f.category} | Remedy: {f.remedy}",
            *([f"Root cause: {f.root_cause}"] if f.root_cause else []),
            *([card.rendered] if card is not None else []),
        ]
        cases.append(
            JudgeCase(
                item_id=f.run_id,
                session_id=f.session_id,
                cluster_key=f.cluster_key,
                matched_skill=skill,
                failure_text="\n".join(failure_parts),
                proposed_patch=proposed,
                original_or_noop=initial_skills.get(skill, DEFAULT_NOOP_GUIDANCE),
                placebo_unrelated_skill=placebo,
            ),
        )
    return cases


async def _run_train_phase(
    options: BacktestOptions,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    window_sessions: Sequence[AuditedSession],
    split: ChronologicalSplit,
    train_judge: AuditJudgeFn,
    learner_fn: LearnerFn,
    learner_cost: CostAccumulator,
) -> tuple[
    list[FindingRecord],
    tuple[FailureCluster, ...],
    list[FailureCluster],
    list[dict[str, Any]],
    dict[str, str],
    dict[str, str],
]:
    scratch = options.out_dir / "scratch_skills" / "skills"
    if scratch.exists():
        shutil.rmtree(scratch)
    if options.skills_dir is not None and options.skills_dir.exists():
        shutil.copytree(options.skills_dir, scratch)
    else:
        scratch.mkdir(parents=True, exist_ok=True)
    initial_skills = _read_skills_snapshot(scratch)
    diffs: list[dict[str, Any]] = []

    async def _nominate(cluster: FailureCluster) -> ReviewOutcome:
        before = _read_skills_snapshot(scratch)
        outcome = await learner_fn(
            config=config,
            runtime_paths=runtime_paths,
            agent_name=options.agent_name,
            cluster=cluster,
            skills_root=scratch,
            progress=ReviewProgress(),
            room_id=options.room_id,
            record_usage=False,
        )
        learner_cost.add_review_outcome(outcome)
        for name, after_text in sorted(_read_skills_snapshot(scratch).items()):
            before_text = before.get(name)
            if before_text == after_text:
                continue
            dlines = list(
                difflib.unified_diff(
                    (before_text or "").splitlines(keepends=True),
                    after_text.splitlines(keepends=True),
                    fromfile=f"a/{name}/{SKILL_FILENAME}",
                    tofile=f"b/{name}/{SKILL_FILENAME}",
                ),
            )
            diffs.append(
                {
                    "skill_name": name,
                    "action": "created" if before_text is None else "updated",
                    "cluster_key": cluster.cluster_key,
                    "tool_name": cluster.tool_name,
                    "operation": cluster.operation,
                    "kind": cluster.kind,
                    "category": cluster.category,
                    "remedy": cluster.remedy,
                    "target_skill": cluster.target_skill,
                    "distinct_sessions": cluster.distinct_sessions,
                    "added_lines": sum(ln.startswith("+") and not ln.startswith("+++") for ln in dlines),
                    "removed_lines": sum(ln.startswith("-") and not ln.startswith("---") for ln in dlines),
                    "unified_diff": "".join(dlines),
                },
            )
        return outcome

    train_state = options.out_dir / "train_state.json"
    train_state.unlink(missing_ok=True)
    ticks = {
        min(
            split.cutoff,
            split.window_start + (int((r.created_at - split.window_start) // _SECONDS_PER_DAY) + 1) * _SECONDS_PER_DAY,
        )
        for s in window_sessions
        for r in s.runs
        if split.window_start <= r.created_at <= split.cutoff
    }
    if ticks or split.train_runs:
        ticks.add(split.cutoff)

    findings: list[FindingRecord] = []
    actionable: tuple[FailureCluster, ...] = ()
    promoted: list[FailureCluster] = []
    audited = 0
    for tick_ts in sorted(ticks):
        if (rem := options.max_audits_train - audited) <= 0:
            break
        max_upd = max(
            (r.updated_at for s in window_sessions for r in s.runs if split.window_start <= r.created_at <= tick_ts),
            default=tick_ts,
        )
        tick_now = max(tick_ts, max_upd) + _MIN_IDLE_SECONDS if tick_ts == split.cutoff else tick_ts
        res = await run_audit_pass(
            db_path=options.db_path,
            storage_name=options.agent_name,
            state_path=train_state,
            scope_key=f"agent:{options.agent_name}",
            judge_fn=train_judge,
            now=tick_now,
            allowed_room_id=options.room_id,
            settings=TraceAuditSettings(
                min_idle_seconds=_MIN_IDLE_SECONDS,
                max_audits_per_run=rem,
                max_clean_samples_per_run=min(1, rem // 5),
                min_cluster_sessions=options.min_cluster_sessions,
                window_days=max(options.window_days + 1, 30),
                max_nominations_per_run=max(1, options.max_audits_train),
            ),
            nominate_fn=_nominate,
            since_ts=split.window_start,
            until_ts=tick_ts,
        )
        findings.extend(res.findings)
        actionable = res.actionable_clusters
        promoted.extend(res.promoted_clusters)
        audited += res.incidents_audited
    if train_state.exists():
        train_state.chmod(0o600)
    return findings, actionable, promoted, diffs, initial_skills, _read_skills_snapshot(scratch)


async def _run_holdout_split(
    options: BacktestOptions,
    *,
    state_name: str,
    target_sids: frozenset[str],
    all_sids: frozenset[str],
    split: ChronologicalSplit,
    holdout_now: float,
    budget: int,
    judge_fn: AuditJudgeFn,
) -> tuple[FindingRecord, ...]:
    if not target_sids or budget <= 0:
        return ()
    state_file = options.out_dir / state_name
    state_file.unlink(missing_ok=True)
    res = await run_audit_pass(
        db_path=options.db_path,
        storage_name=options.agent_name,
        state_path=state_file,
        scope_key=f"agent:{options.agent_name}",
        judge_fn=judge_fn,
        now=holdout_now,
        allowed_room_id=options.room_id,
        excluded_sessions=all_sids - target_sids,
        settings=TraceAuditSettings(
            min_idle_seconds=_MIN_IDLE_SECONDS,
            max_audits_per_run=budget,
            max_clean_samples_per_run=min(2, budget),
            min_cluster_sessions=options.min_cluster_sessions,
            window_days=max(options.window_days + 1, 30),
        ),
        nominate_fn=None,
        since_ts=math.nextafter(split.cutoff, math.inf),
        until_ts=split.snapshot_now,
    )
    if state_file.exists():
        state_file.chmod(0o600)
    return res.findings


def render_report(artifacts: Mapping[str, Any]) -> str:
    """Render an aggregate-only Markdown report with ZERO raw user snippets, prompts, or private titles."""
    cfg, sp, s1, ba, bb = (
        artifacts["config"],
        artifacts["split"],
        artifacts["stage1"],
        artifacts["baseline_a"],
        artifacts["baseline_b"],
    )
    pct = lambda n, d: f"{100.0 * n / d:.1f}%" if d else "0.0%"  # noqa: E731
    lines = [
        f"# Trace Audit Offline Backtest Report ({artifacts['mode']})",
        f"- **Agent:** `{cfg['agent']}` | **Window:** {cfg['window_days']}d (Train {cfg['train_days']}d / Holdout {cfg['window_days'] - cfg['train_days']}d) | **Seed:** {cfg['seed']}",
        f"- **Models:** audit=`{cfg['audit_model'] or 'n/a'}`, judge=`{cfg['judge_model'] or 'n/a'}` | **Min cluster sessions:** {cfg['min_cluster_sessions']} | **Interval:** {cfg['review_interval']}",
        "",
        "## Chronological Split",
        "| Partition | Sessions | Runs |",
        "| --- | ---: | ---: |",
        f"| Train | {sp['train_sessions']} | {sp['train_runs']} |",
        f"| Holdout (unseen) | {sp['holdout_unseen_sessions']} | {sp['holdout_unseen_runs']} |",
        f"| Holdout (continuing) | {sp['holdout_continuing_sessions']} | {sp['holdout_continuing_runs']} |",
        "",
        "## Baselines vs Stage 1 Friction",
        "| Baseline | Reviews | Sessions | Zero-Friction | Friction Missed | Repeat-Error Missed | USD |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        f"| Baseline A (observed) | {ba['review_count']} | {ba['reviewed_sessions']} | {ba['zero_friction_rate'] * 100:.1f}% | {ba['friction_sessions_missed']}/{ba['friction_sessions_total']} | {ba['repeat_error_sessions_missed']}/{ba['repeat_error_sessions_total']} | ${ba['cost']['usd']:.4f} |",
        f"| Baseline B (interval={bb['review_interval']}) | {bb['review_count']} | {bb['reviewed_sessions']} | {bb['zero_friction_rate'] * 100:.1f}% | {bb['friction_sessions_missed']}/{bb['friction_sessions_total']} | {bb['repeat_error_sessions_missed']}/{bb['repeat_error_sessions_total']} | n/a |",
        "",
        "## Stage 1 Friction Breakdown (0 LLM Cost)",
        f"- Sessions: {s1['total_sessions']} | Runs: {s1['total_runs']} | Tool calls: {s1['total_tools']} | Incidents: {s1['incident_count']}",
        f"- Friction sessions: {s1['friction_sessions_count']} ({pct(s1['friction_sessions_count'], s1['total_sessions'])}) | Repeat-error: {s1['repeat_error_sessions_count']} ({pct(s1['repeat_error_sessions_count'], s1['total_sessions'])})",
        f"- Reasons: `{s1['reason_counts']}` | Tool outcomes: `{s1['tool_outcome_counts']}`",
        "| Tool | Operation | Kind | Count |",
        "| --- | --- | --- | ---: |",
        *(
            [
                f"| `{b['tool_name']}` | `{b['operation'] or '-'}` | `{b['kind']}` | {b['count']} |"
                for b in s1["top_error_buckets"]
            ]
            or ["| (none) | - | - | 0 |"]
        ),
    ]
    if artifacts["mode"] == "full":
        tr, ho_u, ho_c, jg, costs = (
            artifacts["train"],
            artifacts["holdout"]["unseen"],
            artifacts["holdout"]["continuing"],
            artifacts["judge"],
            artifacts["costs"],
        )
        diffs = tr["skill_diffs"]
        lines.extend(
            [
                "",
                "## Stage 2 Audit & Recurrence",
                f"- Train: audited={tr['summary']['audited']}, teachable={tr['summary']['teachable']}, clusters={tr['actionable_clusters']}, promoted={tr['promoted_clusters']}",
                f"- Holdout unseen: audited={ho_u['audited']}, teachable={ho_u['teachable']}, recurring={ho_u['recurring_actionable_findings']}/{ho_u['recurring_promoted_findings']} (act/pro)",
                f"- Holdout continuing: audited={ho_c['audited']}, teachable={ho_c['teachable']}, recurring={ho_c['recurring_actionable_findings']}/{ho_c['recurring_promoted_findings']} (act/pro)",
                f"- Train verdicts: `{tr['summary']['verdict_counts']}` | categories: `{tr['summary']['category_counts']}` | remedies: `{tr['summary']['remedy_counts']}`",
                "",
                "## Anonymized Skill Diff Summary",
                f"- Patches: {len(diffs)} (created={sum(d['action'] == 'created' for d in diffs)}, updated={sum(d['action'] == 'updated' for d in diffs)}) | Lines: +{sum(d['added_lines'] for d in diffs)}/-{sum(d['removed_lines'] for d in diffs)}",
                *(
                    f"- Patch #{i}: `{d['action']}` (+{d['added_lines']}/-{d['removed_lines']}) tool=`{d['tool_name']}/{d['operation'] or '-'}` `{d['category']}/{d['remedy']}` ({d['distinct_sessions']} sessions)"
                    for i, d in enumerate(diffs, 1)
                ),
                "",
                "## Blinded Judge Evaluation",
                f"- Evaluated: {jg['total_evaluated']} | Cache hits: {jg['cache_hits']} | Errors: {jg['judge_errors']} | Direct: {jg['direct_hit_rate'] * 100:.1f}% | Baseline: {jg['baseline_direct_hit_rate'] * 100:.1f}% | Placebo: {jg['placebo_hit_rate'] * 100:.1f}% | Harmful: {jg['harmful_rate'] * 100:.1f}%",
                "| Arm | direct | partial | none | harmful |",
                "| --- | ---: | ---: | ---: | ---: |",
                *(
                    f"| `{a}` | {jg['arm_counts'][a]['direct']} | {jg['arm_counts'][a]['partial']} | {jg['arm_counts'][a]['none']} | {jg['arm_counts'][a]['harmful']} |"
                    for a in CANDIDATE_ARMS
                ),
                "",
                "## Cost Breakdown",
                "| Stage | Calls | Input | Output | Cache Read | USD |",
                "| --- | ---: | ---: | ---: | ---: | ---: |",
                *(
                    f"| {lbl} | {costs[k]['calls']} | {costs[k]['input_tokens']} | {costs[k]['output_tokens']} | {costs[k]['cache_read_tokens']} | ${costs[k]['usd']:.4f} |"
                    for lbl, k in (
                        ("Baseline A", "baseline_a"),
                        ("Stage 2 Train", "train_audit"),
                        ("Stage 2 Holdout", "holdout_audit"),
                        ("Skill Learner", "learner"),
                        ("Blinded Judge (eval)", "judge"),
                    )
                ),
            ],
        )
    return "\n".join(lines) + "\n"


async def run_backtest(
    options: BacktestOptions,
    *,
    audit_judge_factory: AuditJudgeFactory | None = None,
    learner_fn: LearnerFn | None = None,
    blinded_complete_fn: BlindedJudgeCompleteFn | None = None,
) -> dict[str, Any]:
    """Execute one offline chronological trace-audit backtest and write ``report.md`` & ``artifacts.json``."""
    options.out_dir.mkdir(parents=True, exist_ok=True)
    runtime_paths = resolve_runtime_paths(config_path=options.config_path, storage_path=options.out_dir / "_runtime")
    config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    prices = model_prices(config)

    all_sessions = load_audited_sessions(options.db_path, options.agent_name, allowed_room_id=options.room_id)
    split = split_sessions_chronologically(all_sessions, window_days=options.window_days, train_days=options.train_days)
    window_sessions = (
        load_audited_sessions(
            options.db_path,
            options.agent_name,
            since_ts=split.window_start,
            until_ts=split.snapshot_now,
            allowed_room_id=options.room_id,
        )
        if split.snapshot_now > 0
        else []
    )
    stage1_now = (
        max(split.snapshot_now, *(s.last_updated_at for s in window_sessions)) + _MIN_IDLE_SECONDS
        if window_sessions
        else 0.0
    )
    stage1 = compute_stage1_breakdown(window_sessions, now=stage1_now, min_idle_seconds=_MIN_IDLE_SECONDS)
    base_a = compute_baseline_a(
        options.db_path,
        options.agent_name,
        prices=prices,
        sessions=window_sessions,
        friction_sessions=stage1.friction_sessions,
        repeat_error_sessions=stage1.repeat_error_sessions,
        since_ts=split.window_start if split.snapshot_now > 0 else None,
        until_ts=split.snapshot_now if split.snapshot_now > 0 else None,
        allowed_room_id=options.room_id,
    )
    base_b = compute_baseline_b(
        options.db_path,
        options.agent_name,
        sessions=window_sessions,
        friction_sessions=stage1.friction_sessions,
        repeat_error_sessions=stage1.repeat_error_sessions,
        review_interval=options.review_interval,
    )
    artifacts: dict[str, Any] = {
        "mode": "dry_run" if options.dry_run else "full",
        "config": {
            "agent": options.agent_name,
            "window_days": options.window_days,
            "train_days": options.train_days,
            "audit_model": options.audit_model,
            "judge_model": options.judge_model,
            "room_id": options.room_id,
            "review_interval": options.review_interval,
            "min_cluster_sessions": options.min_cluster_sessions,
            "max_audits_train": options.max_audits_train,
            "max_audits_holdout": options.max_audits_holdout,
            "seed": options.seed,
        },
        "split": split.to_dict(),
        "stage1": stage1.summary,
        "baseline_a": base_a,
        "baseline_b": base_b,
    }

    if not options.dry_run:
        if audit_judge_factory is None and not options.audit_model:
            msg = "Full mode requires --audit-model."
            raise ValueError(msg)
        if blinded_complete_fn is None and not options.judge_model:
            msg = "Full mode requires --judge-model."
            raise ValueError(msg)
        train_cost, holdout_cost, learner_cost = (
            CostAccumulator(prices),
            CostAccumulator(prices),
            CostAccumulator(prices),
        )

        def _make_judge(acc: CostAccumulator) -> AuditJudgeFn:
            if audit_judge_factory is not None:
                return audit_judge_factory(acc.add_helper_usage)
            assert options.audit_model is not None
            return make_llm_audit_judge(config, runtime_paths, options.audit_model, usage_sink=acc.add_helper_usage)

        tr_findings, tr_act, tr_pro, diffs, init_sk, frozen_sk = await _run_train_phase(
            options,
            config=config,
            runtime_paths=runtime_paths,
            window_sessions=window_sessions,
            split=split,
            train_judge=_make_judge(train_cost),
            learner_fn=learner_fn or review_audit_cluster,
            learner_cost=learner_cost,
        )
        holdout_cards: dict[str, EvidenceCard] = {}
        raw_holdout = _make_judge(holdout_cost)

        async def _holdout_judge(card: EvidenceCard) -> AuditFinding:
            holdout_cards[card.run_id] = card
            return await raw_holdout(card)

        all_sids = frozenset(s.session_id for s in all_sessions)
        unseen = await _run_holdout_split(
            options,
            state_name="holdout_unseen_state.json",
            target_sids=split.holdout_unseen_session_ids,
            all_sids=all_sids,
            split=split,
            holdout_now=stage1_now,
            budget=options.max_audits_holdout,
            judge_fn=_holdout_judge,
        )
        continuing = await _run_holdout_split(
            options,
            state_name="holdout_continuing_state.json",
            target_sids=split.holdout_continuing_session_ids,
            all_sids=all_sids,
            split=split,
            holdout_now=stage1_now,
            budget=max(0, options.max_audits_holdout - len(unseen)),
            judge_fn=_holdout_judge,
        )
        judge_summary = await evaluate_blinded_cases(
            _build_judge_cases(
                unseen,
                holdout_cards,
                diffs=diffs,
                initial_skills=init_sk,
                frozen_skills=frozen_sk,
                seed=options.seed,
            ),
            config=config,
            runtime_paths=runtime_paths,
            judge_model=options.judge_model or "stub-judge",
            cache_path=options.out_dir / "judge_cache.json",
            seed=options.seed,
            prices=prices,
            complete_fn=blinded_complete_fn,
        )
        act_keys, pro_keys = frozenset(c.cluster_key for c in tr_act), frozenset(c.cluster_key for c in tr_pro)
        artifacts.update(
            {
                "train": {
                    "summary": _summarize_findings(tr_findings),
                    "actionable_clusters": len(tr_act),
                    "promoted_clusters": len(tr_pro),
                    "promoted_cluster_keys": [c.cluster_key for c in tr_pro],
                    "skill_diffs": diffs,
                },
                "holdout": {
                    "unseen": _summarize_findings(unseen, trained_act=act_keys, trained_pro=pro_keys),
                    "continuing": _summarize_findings(continuing, trained_act=act_keys, trained_pro=pro_keys),
                },
                "judge": judge_summary,
                "costs": {
                    "baseline_a": base_a["cost"],
                    "train_audit": train_cost.to_dict(),
                    "holdout_audit": holdout_cost.to_dict(),
                    "learner": learner_cost.to_dict(),
                    "judge": judge_summary["cost"],
                },
            },
        )

    write_private_json(options.out_dir / "artifacts.json", artifacts)
    (options.out_dir / "report.md").write_text(render_report(artifacts), encoding="utf-8")
    return artifacts


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m evals.trace_audit.run",
        description="Offline chronological trace-audit backtest.",
    )
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--skills-dir", type=Path, default=None)
    p.add_argument("--window-days", type=int, default=30)
    p.add_argument("--train-days", type=int, default=20)
    p.add_argument("--audit-model", default=None)
    p.add_argument("--judge-model", default=None)
    p.add_argument("--room-id", default=None)
    p.add_argument("--review-interval", type=int, default=10)
    p.add_argument("--min-cluster-sessions", type=int, default=2)
    p.add_argument("--max-audits-train", type=int, default=60)
    p.add_argument("--max-audits-holdout", type=int, default=40)
    p.add_argument("--seed", type=int, default=23)
    p.add_argument("--dry-run", action="store_true")
    return p


async def run_cli(argv: Sequence[str]) -> dict[str, Any]:
    """Parse CLI ``argv`` and execute the offline chronological backtest."""
    parser = _build_parser()
    a = parser.parse_args(argv)
    if not a.db.is_file():
        parser.error(f"--db not found: {a.db}")
    if a.window_days < 1 or not 1 <= a.train_days <= a.window_days:
        parser.error("--train-days must satisfy 1 <= train_days <= window_days")
    if a.review_interval < 1 or a.min_cluster_sessions < 1 or a.max_audits_train < 0 or a.max_audits_holdout < 0:
        parser.error("intervals/cluster thresholds must be >= 1 and audit budgets >= 0")
    if not a.dry_run and (not a.audit_model or not a.judge_model):
        parser.error("full mode requires both --audit-model and --judge-model (or pass --dry-run)")
    return await run_backtest(
        BacktestOptions(
            db_path=a.db,
            config_path=a.config,
            agent_name=a.agent,
            out_dir=a.out,
            skills_dir=a.skills_dir,
            window_days=a.window_days,
            train_days=a.train_days,
            audit_model=a.audit_model,
            judge_model=a.judge_model,
            room_id=a.room_id,
            review_interval=a.review_interval,
            min_cluster_sessions=a.min_cluster_sessions,
            max_audits_train=a.max_audits_train,
            max_audits_holdout=a.max_audits_holdout,
            seed=a.seed,
            dry_run=a.dry_run,
        ),
    )


def main() -> None:
    """CLI entrypoint for ``python -m evals.trace_audit.run``."""
    asyncio.run(run_cli(sys.argv[1:]))


if __name__ == "__main__":
    main()
