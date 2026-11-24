"""Offline chronological backtest CLI for passive trace auditing and friction-triggered skill learning."""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evals.trace_audit.baseline import (
    ChronologicalSplit,
    CostAccumulator,
    evaluate_window_baselines,
    split_sessions_chronologically,
)
from evals.trace_audit.judge import (
    BlindedJudgeCompleteFn,
    build_judge_cases,
    evaluate_blinded_cases,
    write_private_json,
)
from evals.trace_audit.report import build_base_artifacts, build_full_artifacts, build_skill_diffs, render_report
from evals.trace_audit.workspace import (
    ensure_disjoint_skills_dir,
    prepare_isolated_runtime,
    prepare_scratch_skills,
    read_skills_snapshot,
)
from mindroom.budgets.pricing import model_prices
from mindroom.config.models import split_model_reference
from mindroom.contacts.turns import contact_session_check
from mindroom.providers.identity import model_identity
from mindroom.skill_learning.audit_reviewer import ReviewOutcome, review_audit_cluster
from mindroom.skill_learning.tools import ReviewProgress
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

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.llm.usage import HelperUsage

__all__ = [
    "BacktestOptions",
    "ChronologicalSplit",
    "main",
    "render_report",
    "run_backtest",
    "run_cli",
    "split_sessions_chronologically",
]

_SECONDS_PER_DAY = 86_400.0
_MIN_IDLE_SECONDS = 600

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
    roster_path: Path | None = None
    label: str = "dataset"
    window_days: int = 30
    train_days: int = 20
    audit_model: str | None = None
    judge_model: str | None = None
    allow_same_model: bool = False
    room_id: str | None = None
    review_interval: int = 10
    min_cluster_sessions: int = 2
    max_audits_train: int = 60
    max_audits_holdout: int = 40
    seed: int = 23
    dry_run: bool = False

    def config_summary(self) -> dict[str, Any]:
        """Return JSON-serializable summary metadata for ``artifacts.json``."""
        excluded = {"db_path", "config_path", "agent_name", "out_dir"} | {
            "skills_dir",
            "roster_path",
            "allow_same_model",
            "dry_run",
        }
        return {"agent": self.agent_name, **{k: v for k, v in asdict(self).items() if k not in excluded}}


@dataclass(frozen=True, slots=True)
class _TrainResult:
    findings: list[FindingRecord]
    actionable: tuple[FailureCluster, ...]
    promoted: list[FailureCluster]
    diffs: list[dict[str, Any]]
    initial_skills: dict[str, str]
    frozen_skills: dict[str, str]


def _audit_settings(
    options: BacktestOptions,
    *,
    max_audits: int,
    clean_cap: int,
    max_nominations: int = 1,
) -> TraceAuditSettings:
    return TraceAuditSettings(
        min_idle_seconds=_MIN_IDLE_SECONDS,
        max_audits_per_run=max_audits,
        max_clean_samples_per_run=clean_cap,
        min_cluster_sessions=options.min_cluster_sessions,
        window_days=max(options.window_days + 1, 30),
        max_nominations_per_run=max_nominations,
        audit_concurrency=6,
    )


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
    contact_check: Callable[[str], bool],
) -> _TrainResult:
    scratch = options.out_dir / "scratch_skills" / "skills"
    initial_skills = prepare_scratch_skills(options.skills_dir, scratch)
    diffs: list[dict[str, Any]] = []

    async def _nominate(cluster: FailureCluster) -> ReviewOutcome:
        before = read_skills_snapshot(scratch)
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
        diffs.extend(build_skill_diffs(before, read_skills_snapshot(scratch), cluster))
        return outcome

    train_state = options.out_dir / "train_state.json"
    train_state.unlink(missing_ok=True)
    train_runs = [
        r
        for s in window_sessions
        for r in s.runs
        if split.window_start <= r.created_at and max(r.created_at, r.updated_at) <= split.cutoff
    ]
    ticks = {
        min(
            split.cutoff,
            split.window_start + (int((r.created_at - split.window_start) // _SECONDS_PER_DAY) + 1) * _SECONDS_PER_DAY,
        )
        for r in train_runs
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
        max_upd = max((r.updated_at for r in train_runs if r.updated_at <= tick_ts), default=tick_ts)
        tick_now = max(tick_ts, max_upd) + _MIN_IDLE_SECONDS if tick_ts == split.cutoff else tick_ts
        res = await run_audit_pass(
            db_path=options.db_path,
            storage_name=options.agent_name,
            state_path=train_state,
            scope_key=f"agent:{options.agent_name}",
            judge_fn=train_judge,
            now=tick_now,
            allowed_room_id=options.room_id,
            contact_session_check=contact_check,
            settings=_audit_settings(
                options,
                max_audits=rem,
                clean_cap=min(1, rem // 5),
                max_nominations=max(1, options.max_audits_train),
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
    return _TrainResult(findings, actionable, promoted, diffs, initial_skills, read_skills_snapshot(scratch))


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
    contact_check: Callable[[str], bool],
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
        contact_session_check=contact_check,
        settings=_audit_settings(options, max_audits=budget, clean_cap=min(2, budget)),
        nominate_fn=None,
        since_ts=math.nextafter(split.cutoff, math.inf),
        until_ts=split.snapshot_now,
    )
    if state_file.exists():
        state_file.chmod(0o600)
    return res.findings


def _validate_options(options: BacktestOptions, *, has_audit_factory: bool, has_blinded_fn: bool) -> None:
    if options.skills_dir is not None:
        ensure_disjoint_skills_dir(options.skills_dir, options.out_dir / "scratch_skills" / "skills")
    if options.dry_run:
        return
    if not has_audit_factory and not options.audit_model:
        msg = "Full mode requires --audit-model."
        raise ValueError(msg)
    if not has_blinded_fn and not options.judge_model:
        msg = "Full mode requires --judge-model."
        raise ValueError(msg)
    if (
        not options.allow_same_model
        and options.audit_model
        and options.judge_model
        and split_model_reference(options.audit_model)[0] == split_model_reference(options.judge_model)[0]
    ):
        msg = "--audit-model and --judge-model must differ unless --allow-same-model is passed."
        raise ValueError(msg)


def _validate_resolved_models(options: BacktestOptions, config: Config) -> None:
    if options.dry_run or options.allow_same_model or not options.audit_model or not options.judge_model:
        return
    audit_base, _ = split_model_reference(options.audit_model)
    judge_base, _ = split_model_reference(options.judge_model)
    audit_cfg, judge_cfg = config.models.get(audit_base), config.models.get(judge_base)
    if audit_cfg is not None and judge_cfg is not None and model_identity(audit_cfg) == model_identity(judge_cfg):
        ident = model_identity(audit_cfg)
        msg = (
            f"--audit-model ({options.audit_model}) and --judge-model ({options.judge_model}) "
            f"resolve to the same model ({ident.provider}/{ident.id}); pass --allow-same-model to override."
        )
        raise ValueError(msg)


async def run_backtest(
    options: BacktestOptions,
    *,
    audit_judge_factory: AuditJudgeFactory | None = None,
    learner_fn: LearnerFn | None = None,
    blinded_complete_fn: BlindedJudgeCompleteFn | None = None,
) -> dict[str, Any]:
    """Execute one offline chronological trace-audit backtest and write ``report.md`` & ``artifacts.json``."""
    _validate_options(
        options,
        has_audit_factory=audit_judge_factory is not None,
        has_blinded_fn=blinded_complete_fn is not None,
    )
    config, runtime_paths = prepare_isolated_runtime(
        out_dir=options.out_dir,
        config_path=options.config_path,
        roster_path=options.roster_path,
    )
    _validate_resolved_models(options, config)
    prices = model_prices(config)
    contact_check = contact_session_check(config, runtime_paths, options.agent_name)

    base = evaluate_window_baselines(
        db_path=options.db_path,
        agent_name=options.agent_name,
        prices=prices,
        window_days=options.window_days,
        train_days=options.train_days,
        review_interval=options.review_interval,
        min_idle_seconds=_MIN_IDLE_SECONDS,
        allowed_room_id=options.room_id,
        contact_session_check=contact_check,
    )
    window_sessions, split, stage1_now, base_a = base.sessions, base.split, base.stage1_now, base.baseline_a
    artifacts = build_base_artifacts(
        dry_run=options.dry_run,
        config_summary=options.config_summary(),
        split_dict=split.to_dict(),
        stage1_summary=base.stage1.summary,
        baseline_a=base_a,
        baseline_b=base.baseline_b,
    )

    if not options.dry_run:
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

        tr = await _run_train_phase(
            options,
            config=config,
            runtime_paths=runtime_paths,
            window_sessions=window_sessions,
            split=split,
            train_judge=_make_judge(train_cost),
            learner_fn=learner_fn or review_audit_cluster,
            learner_cost=learner_cost,
            contact_check=contact_check,
        )
        holdout_cards: dict[str, EvidenceCard] = {}
        raw_holdout = _make_judge(holdout_cost)

        async def _holdout_judge(card: EvidenceCard) -> AuditFinding:
            holdout_cards[card.run_id] = card
            return await raw_holdout(card)

        all_sids = frozenset(s.session_id for s in window_sessions)
        unseen = await _run_holdout_split(
            options,
            state_name="holdout_unseen_state.json",
            target_sids=split.holdout_unseen_session_ids,
            all_sids=all_sids,
            split=split,
            holdout_now=stage1_now,
            budget=options.max_audits_holdout,
            judge_fn=_holdout_judge,
            contact_check=contact_check,
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
            contact_check=contact_check,
        )
        judge_summary = await evaluate_blinded_cases(
            build_judge_cases(
                unseen,
                holdout_cards,
                diffs=tr.diffs,
                initial_skills=tr.initial_skills,
                frozen_skills=tr.frozen_skills,
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
        artifacts.update(
            build_full_artifacts(
                train_findings=tr.findings,
                train_actionable=tr.actionable,
                train_promoted=tr.promoted,
                diffs=tr.diffs,
                unseen_findings=unseen,
                continuing_findings=continuing,
                judge_summary=judge_summary,
                costs={
                    "baseline_a": base_a["cost"],
                    "train_audit": train_cost.to_dict(),
                    "holdout_audit": holdout_cost.to_dict(),
                    "learner": learner_cost.to_dict(),
                    "judge": judge_summary["cost"],
                },
            ),
        )

    write_private_json(options.out_dir / "artifacts.json", artifacts)
    (options.out_dir / "report.md").write_text(render_report(artifacts), encoding="utf-8")
    return artifacts


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m evals.trace_audit.run",
        description="Offline chronological trace-audit backtest.",
    )
    p.add_argument("--db", dest="db_path", type=Path, required=True)
    p.add_argument("--config", dest="config_path", type=Path, required=True)
    p.add_argument("--agent", dest="agent_name", required=True)
    p.add_argument("--out", dest="out_dir", type=Path, required=True)
    p.add_argument("--skills-dir", type=Path, default=None)
    p.add_argument(
        "--roster-path",
        type=Path,
        default=None,
        help="Optional personal_agents.json path (defaults to auto-detecting <config-dir>/mindroom_data/tracking/personal_agents.json).",
    )
    p.add_argument("--label", default="dataset", help="Neutral dataset label printed in report.md instead of --agent.")
    p.add_argument("--window-days", type=int, default=30)
    p.add_argument("--train-days", type=int, default=20)
    p.add_argument("--audit-model", default=None)
    p.add_argument("--judge-model", default=None)
    p.add_argument("--allow-same-model", action="store_true", help="Allow --audit-model and --judge-model to match.")
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
    if not a.db_path.is_file():
        parser.error(f"--db not found: {a.db_path}")
    if a.roster_path is not None and not a.roster_path.is_file():
        parser.error(f"--roster-path not found: {a.roster_path}")
    if a.window_days < 1 or not 1 <= a.train_days <= a.window_days:
        parser.error("--train-days must satisfy 1 <= train_days <= window_days")
    if a.review_interval < 1 or a.min_cluster_sessions < 1 or a.max_audits_train < 0 or a.max_audits_holdout < 0:
        parser.error("intervals/cluster thresholds must be >= 1 and audit budgets >= 0")
    if not a.dry_run and (not a.audit_model or not a.judge_model):
        parser.error("full mode requires both --audit-model and --judge-model (or pass --dry-run)")
    if (
        not a.dry_run
        and split_model_reference(a.audit_model)[0] == split_model_reference(a.judge_model)[0]
        and not a.allow_same_model
    ):
        parser.error("--audit-model and --judge-model must differ unless --allow-same-model is passed")
    return await run_backtest(BacktestOptions(**vars(a)))


def main() -> None:
    """CLI entrypoint for ``python -m evals.trace_audit.run``."""
    asyncio.run(run_cli(sys.argv[1:]))


if __name__ == "__main__":
    main()
