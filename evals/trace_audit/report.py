"""Aggregate-only Markdown report rendering and diff/finding summaries for trace-audit backtests."""

from __future__ import annotations

import difflib
from collections import Counter
from typing import TYPE_CHECKING, Any

from evals.trace_audit.judge import CANDIDATE_ARMS
from mindroom.tool_system.skills import SKILL_FILENAME

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mindroom.trace_audit.models import FailureCluster, FindingRecord

_ACTIONABLE = frozenset({"actionable", "unverified_claim"})


def summarize_findings(
    findings: Sequence[FindingRecord],
    *,
    trained_act: frozenset[str] = frozenset(),
    trained_pro: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Return aggregate verdict/category/remedy and recurrence counts for a set of findings."""
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


def build_skill_diffs(
    before: Mapping[str, str],
    after: Mapping[str, str],
    cluster: FailureCluster,
) -> list[dict[str, Any]]:
    """Build structured unified diffs for skills created or updated by one learner pass."""
    diffs: list[dict[str, Any]] = []
    for name, after_text in sorted(after.items()):
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
    return diffs


def _pct(numerator: int, denominator: int) -> str:
    return f"{100.0 * numerator / denominator:.1f}%" if denominator else "0.0%"


def _render_full_sections(artifacts: Mapping[str, Any]) -> list[str]:
    tr = artifacts["train"]
    ho_u = artifacts["holdout"]["unseen"]
    ho_c = artifacts["holdout"]["continuing"]
    jg = artifacts["judge"]
    costs = artifacts["costs"]
    diffs = tr["skill_diffs"]
    return [
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
    ]


def render_report(artifacts: Mapping[str, Any]) -> str:
    """Render an aggregate-only Markdown report with ZERO raw user snippets, prompts, or raw agent usernames."""
    cfg = artifacts["config"]
    sp = artifacts["split"]
    s1 = artifacts["stage1"]
    ba = artifacts["baseline_a"]
    bb = artifacts["baseline_b"]
    label = cfg.get("label", "dataset")
    lines = [
        f"# Trace Audit Offline Backtest Report ({artifacts['mode']})",
        f"- **Dataset:** `{label}` | **Window:** {cfg['window_days']}d (Train {cfg['train_days']}d / Holdout {cfg['window_days'] - cfg['train_days']}d) | **Seed:** {cfg['seed']}",
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
        f"- Friction sessions: {s1['friction_sessions_count']} ({_pct(s1['friction_sessions_count'], s1['total_sessions'])}) | Repeat-error: {s1['repeat_error_sessions_count']} ({_pct(s1['repeat_error_sessions_count'], s1['total_sessions'])})",
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
        lines.extend(_render_full_sections(artifacts))
    return "\n".join(lines) + "\n"


def build_base_artifacts(
    *,
    dry_run: bool,
    config_summary: Mapping[str, Any],
    split_dict: Mapping[str, Any],
    stage1_summary: Mapping[str, Any],
    baseline_a: Mapping[str, Any],
    baseline_b: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble the baseline/Stage-1 backtest artifact payload."""
    return {
        "mode": "dry_run" if dry_run else "full",
        "config": dict(config_summary),
        "split": dict(split_dict),
        "stage1": dict(stage1_summary),
        "baseline_a": dict(baseline_a),
        "baseline_b": dict(baseline_b),
    }


def build_full_artifacts(
    *,
    train_findings: Sequence[FindingRecord],
    train_actionable: Sequence[FailureCluster],
    train_promoted: Sequence[FailureCluster],
    diffs: Sequence[dict[str, Any]],
    unseen_findings: Sequence[FindingRecord],
    continuing_findings: Sequence[FindingRecord],
    judge_summary: Mapping[str, Any],
    costs: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble the full-mode train/holdout/judge/cost sections of ``artifacts.json``."""
    act_keys = frozenset(c.cluster_key for c in train_actionable)
    pro_keys = frozenset(c.cluster_key for c in train_promoted)
    return {
        "train": {
            "summary": summarize_findings(train_findings),
            "actionable_clusters": len(train_actionable),
            "promoted_clusters": len(train_promoted),
            "promoted_cluster_keys": [c.cluster_key for c in train_promoted],
            "skill_diffs": list(diffs),
        },
        "holdout": {
            "unseen": summarize_findings(unseen_findings, trained_act=act_keys, trained_pro=pro_keys),
            "continuing": summarize_findings(continuing_findings, trained_act=act_keys, trained_pro=pro_keys),
        },
        "judge": dict(judge_summary),
        "costs": dict(costs),
    }
