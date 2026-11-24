"""Tests for the offline chronological trace-audit backtest CLI and evaluation modules."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from evals.trace_audit.baseline import compute_baseline_a, compute_baseline_b, compute_stage1_breakdown
from evals.trace_audit.judge import (
    BlindedCandidateScore,
    BlindedJudgeResponse,
    JudgeCase,
    deterministic_arm_order,
    evaluate_blinded_cases,
)
from evals.trace_audit.run import BacktestOptions, run_backtest, run_cli, split_sessions_chronologically
from mindroom.agent_loop.helper import HelperResult
from mindroom.budgets.pricing import model_prices
from mindroom.config.main import load_config
from mindroom.constants import resolve_runtime_paths
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import model_provenance
from mindroom.llm.usage import HelperUsage, MessageMetrics, RunMetrics, TokenCounts
from mindroom.session_storage.schema import TableNames, create_log_schema, quote_identifier
from mindroom.skill_learning.reviewer import ReviewOutcome
from mindroom.trace_audit.models import AuditFinding, AuditJudgeFn, EvidenceCard, FailureCluster
from mindroom.trace_audit.source import load_audited_sessions

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from mindroom.skill_learning.tools import ReviewProgress

NOW = 1_750_000_000.0
DAY = 86_400.0
ROOM_A, ROOM_B = "!roomA:example.com", "!roomB:example.com"


def _write_config(path: Path) -> Path:
    path.write_text(
        "models:\n"
        "  audit_m:\n    provider: openai\n    id: gpt-audit\n"
        "    pricing: {input: 1.0, output: 4.0, cache_read: 0.1}\n"
        "  judge_m:\n    provider: anthropic\n    id: claude-judge\n"
        "    pricing: {input: 3.0, output: 15.0, cache_read: 0.3, cache_write: 3.75}\n"
        "  learner_m:\n    provider: openai\n    id: gpt-learner\n"
        "    pricing: {input: 2.0, output: 8.0}\n"
        "router:\n  model: audit_m\n"
        "agents:\n  harry:\n    display_name: Harry\n    role: Assistant\n    model: audit_m\n",
        encoding="utf-8",
    )
    return path


def _asst(content: str = "Done.", *, tool_calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "provider_data": model_provenance("gpt-audit", "OpenAI"),
    }
    return {**msg, "tool_calls": tool_calls} if tool_calls is not None else msg


def _fail_tool(cmd: str, tag: str) -> dict[str, Any]:
    return {
        "tool_call_id": f"tc_{tag}",
        "tool_name": "run_shell_command",
        "tool_args": {"command": cmd},
        "tool_call_error": False,
        "result": f"Exit code 1\nStderr:\nfailed {tag}",
    }


def _seed_v8_db(
    db_path: Path,
    storage_name: str,
    *,
    sessions: Sequence[tuple[str, float, float]],
    runs: Sequence[dict[str, Any]],
    tools: Sequence[tuple[str, int, dict[str, Any]]] = (),
    messages: Sequence[tuple[str, int, dict[str, Any]]] = (),
    usage_rows: Sequence[tuple[str, str, dict[str, Any]]] = (),
) -> None:
    names = TableNames(storage_name)
    conn = sqlite3.connect(db_path)
    try:
        create_log_schema(conn, names, HistoryScope(kind="agent", scope_id=storage_name))
        for sid, created, updated in sessions:
            conn.execute(
                f"INSERT INTO {quote_identifier(names.sessions)} "  # noqa: S608
                "(session_id, user_id, session_data, metadata, created_at, updated_at, session_state) "
                "VALUES (?, '@u:example.com', NULL, NULL, ?, ?, NULL)",
                (sid, int(created), int(updated)),
            )
        for r in runs:
            created = int(r["created_at"])
            rec = {
                "run_id": r["run_id"],
                "content": r.get("content", "Done."),
                **({"error": r["error"]} if "error" in r else {}),
            }
            conn.execute(
                f"INSERT INTO {quote_identifier(names.log_runs)} "  # noqa: S608
                "(run_id, session_id, parent_run_id, kind, scope_key, status, seq, folded_by, has_tools, "
                "created_at, updated_at, metadata, record, requirements) "
                "VALUES (?, ?, NULL, 'agent', ?, ?, ?, NULL, 1, ?, ?, NULL, ?, NULL)",
                (
                    r["run_id"],
                    r["session_id"],
                    f"agent:{storage_name}",
                    r.get("status", "COMPLETED"),
                    r.get("seq", 1),
                    created,
                    created + 10,
                    json.dumps(rec),
                ),
            )
        for rid, idx, payload in tools:
            conn.execute(
                f"INSERT INTO {quote_identifier(names.log_tools)} (run_id, ord, data) VALUES (?, ?, ?)",  # noqa: S608
                (rid, idx, json.dumps(payload)),
            )
        for rid, idx, payload in messages:
            conn.execute(
                f"INSERT INTO {quote_identifier(names.log_messages)} (run_id, message_id, ord, folded_by, data) VALUES (?, ?, ?, NULL, ?)",  # noqa: S608
                (rid, f"{rid}_m{idx}", idx, json.dumps(payload)),
            )
        for sid, rid, payload in usage_rows:
            conn.execute(
                f"INSERT INTO {quote_identifier(names.usage)} (session_id, run_id, usage_data) VALUES (?, ?, ?)",  # noqa: S608
                (sid, rid, json.dumps(payload)),
            )
        conn.commit()
    finally:
        conn.close()


def _usage_row(
    provider: str,
    model: str,
    inp: int,
    out: int,
    *,
    cache_read: int = 0,
    kind: str = "skill_learning",
    details: bool = False,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": inp + out,
        "cache_read_tokens": cache_read,
    }
    if details:
        metrics["details"] = {"model": [{"provider": provider, "id": model, **metrics}]}
    return {"kind": kind, "model_provider": provider, "model": model, "created_at": NOW - 5 * DAY, "metrics": metrics}


def _helper_usage(provider: str, model: str, *, inp: int, out: int) -> HelperUsage:
    metrics = RunMetrics()
    metrics.add(
        MessageMetrics(tokens=TokenCounts(input_tokens=inp, output_tokens=out)),
        model_id=model,
        provider=provider,
    )
    return HelperUsage(run_id="u1", model=model, provider=provider, metrics=metrics)


def _score_by_markers(
    prompt: str,
    *,
    direct: str,
    partial: str,
    inp: int,
    out: int,
) -> HelperResult[BlindedJudgeResponse]:
    scores = [
        BlindedCandidateScore(
            label=sec[0],
            score="direct" if direct in sec else ("partial" if partial in sec else "none"),
            rationale="Scored.",
        )
        for sec in prompt.split("=== Candidate ")[1:]
    ]
    return HelperResult(
        content=BlindedJudgeResponse(scores=scores),
        usage=_helper_usage("Anthropic", "claude-judge", inp=inp, out=out),
    )


def test_baselines_a_and_b_and_readonly_safety(tmp_path: Path) -> None:
    """Baselines A & B compute accurate coverage, resets, and USD cost while keeping DB strictly read-only."""
    cfg_path = _write_config(tmp_path / "config.yaml")
    ro_dir = tmp_path / "ro_db"
    ro_dir.mkdir()
    db_path = ro_dir / "harry.db"
    s_fric, s_clean, s_reset, s_other = f"{ROOM_A}:$fric", f"{ROOM_A}:$clean", f"{ROOM_A}:$reset", f"{ROOM_B}:$other"

    _seed_v8_db(
        db_path,
        "harry",
        sessions=[
            (s_fric, NOW - 5 * DAY, NOW - 5 * DAY),
            (s_clean, NOW - 4 * DAY, NOW - 4 * DAY),
            (s_reset, NOW - 3 * DAY, NOW - 3 * DAY),
            (s_other, NOW - 2 * DAY, NOW - 2 * DAY),
        ],
        runs=[
            {"run_id": "f1", "session_id": s_fric, "seq": 1, "created_at": NOW - 5 * DAY},
            {
                "run_id": "f2",
                "session_id": s_fric,
                "seq": 2,
                "status": "ERROR",
                "error": "boom",
                "created_at": NOW - 5 * DAY + 60,
            },
            {"run_id": "c1", "session_id": s_clean, "seq": 1, "created_at": NOW - 4 * DAY},
            {"run_id": "c2", "session_id": s_clean, "seq": 2, "created_at": NOW - 4 * DAY + 60},
            {"run_id": "r1", "session_id": s_reset, "seq": 1, "created_at": NOW - 3 * DAY},
            {"run_id": "r2", "session_id": s_reset, "seq": 2, "created_at": NOW - 3 * DAY + 60},
            {"run_id": "o1", "session_id": s_other, "seq": 1, "created_at": NOW - 2 * DAY},
        ],
        tools=[("f1", 0, _fail_tool("pytest a.py", "1")), ("f1", 1, _fail_tool("pytest b.py", "2"))],
        messages=[
            ("f1", 0, _asst("tried")),
            ("f2", 0, _asst("error_run_ignored")),
            ("c1", 0, _asst("s1")),
            ("c1", 1, _asst("s2")),
            ("c2", 0, _asst("s3")),
            ("c2", 1, _asst("s4")),
            ("r1", 0, _asst("before")),
            ("r2", 0, _asst("reset", tool_calls=[{"function": {"name": "skill_manage"}}])),
            ("r2", 1, _asst("after")),
            ("o1", 0, _asst("o1")),
            ("o1", 1, _asst("o2")),
        ],
        usage_rows=[
            (
                s_clean,
                "sl_1",
                _usage_row("OpenAI Responses", "gpt-audit", 1_000_000, 100_000, cache_read=500_000, details=True),
            ),
            (s_clean, "sl_2", _usage_row("OpenAI", "unpriced", 1_000, 100)),
            (s_fric, "f1", _usage_row("OpenAI", "gpt-audit", 999_999, 100, kind="run")),
            (s_other, "sl_other", _usage_row("OpenAI", "gpt-audit", 1_000_000, 0)),
        ],
    )

    prices = model_prices(
        load_config(
            resolve_runtime_paths(config_path=cfg_path, storage_path=tmp_path / "_rt"),
            tolerate_plugin_load_errors=True,
        ),
    )
    before_bytes, before_stat = db_path.read_bytes(), db_path.stat()
    db_path.chmod(0o444)
    ro_dir.chmod(0o555)
    try:
        sessions = load_audited_sessions(db_path, "harry", allowed_room_id=ROOM_A)
        s1 = compute_stage1_breakdown(sessions, now=NOW + 600)
        assert (s1.friction_sessions, s1.repeat_error_sessions) == (frozenset({s_fric}), frozenset({s_fric}))

        ba = compute_baseline_a(
            db_path,
            "harry",
            prices=prices,
            sessions=sessions,
            friction_sessions=s1.friction_sessions,
            repeat_error_sessions=s1.repeat_error_sessions,
            allowed_room_id=ROOM_A,
        )
        assert (
            ba["review_count"],
            ba["reviewed_sessions"],
            ba["zero_friction_reviews"],
            ba["friction_sessions_missed"],
            ba["repeat_error_sessions_missed"],
        ) == (2, 1, 2, 1, 1)
        assert (ba["cost"]["input_tokens"], ba["cost"]["usd"], ba["cost"]["unpriced_calls"]) == (
            1_001_000,
            pytest.approx(0.95),
            1,
        )

        bb = compute_baseline_b(
            db_path,
            "harry",
            sessions=sessions,
            friction_sessions=s1.friction_sessions,
            repeat_error_sessions=s1.repeat_error_sessions,
            review_interval=2,
        )
        assert (
            bb["review_count"],
            bb["reviewed_sessions"],
            bb["zero_friction_reviews"],
            bb["friction_sessions_missed"],
            bb["repeat_error_sessions_missed"],
            bb["total_model_replies"],
        ) == (2, 1, 2, 1, 1, 7)

        after_stat = db_path.stat()
        assert (after_stat.st_mtime_ns, after_stat.st_size, db_path.read_bytes()) == (
            before_stat.st_mtime_ns,
            before_stat.st_size,
            before_bytes,
        )
    finally:
        ro_dir.chmod(0o755)
        db_path.chmod(0o644)


@pytest.mark.asyncio
async def test_chronological_split_zero_leakage_and_dry_run(tmp_path: Path) -> None:
    """Chronological split separates train, unseen holdout, and continuing sessions; dry-run makes 0 LLM calls."""
    cfg_path = _write_config(tmp_path / "config.yaml")
    db_path = tmp_path / "harry.db"
    skills_dir = tmp_path / "orig_skills"
    (skills_dir / "demo").mkdir(parents=True)
    orig_skill = skills_dir / "demo" / "SKILL.md"
    orig_skill.write_text("---\nname: demo\ndescription: Demo\n---\nKeep untouched.\n", encoding="utf-8")
    private_user_text = "PRIVATE_USER_MARKER_98765"

    s_old, s_train, s_cont, s_hold = f"{ROOM_A}:$old", f"{ROOM_A}:$train", f"{ROOM_A}:$cont", f"{ROOM_B}:$hold"
    _seed_v8_db(
        db_path,
        "harry",
        sessions=[
            (s_old, NOW - 40 * DAY, NOW - 40 * DAY),
            (s_train, NOW - 25 * DAY, NOW - 25 * DAY),
            (s_cont, NOW - 15 * DAY, NOW - 2 * DAY),
            (s_hold, NOW - 5 * DAY, NOW),
        ],
        runs=[
            {"run_id": "old_1", "session_id": s_old, "seq": 1, "created_at": NOW - 40 * DAY},
            {"run_id": "tr_1", "session_id": s_train, "seq": 1, "created_at": NOW - 25 * DAY},
            {"run_id": "co_1", "session_id": s_cont, "seq": 1, "created_at": NOW - 15 * DAY},
            {"run_id": "co_2", "session_id": s_cont, "seq": 2, "created_at": NOW - 2 * DAY},
            {"run_id": "ho_1", "session_id": s_hold, "seq": 1, "created_at": NOW - 5 * DAY},
            {"run_id": "ho_2", "session_id": s_hold, "seq": 2, "created_at": NOW},
        ],
        tools=[("tr_1", 0, _fail_tool("pytest x.py", "t1")), ("tr_1", 1, _fail_tool("pytest y.py", "t2"))],
        messages=[
            ("tr_1", 0, {"role": "user", "content": private_user_text}),
            ("ho_2", 0, {"role": "user", "content": private_user_text}),
        ],
    )

    split = split_sessions_chronologically(load_audited_sessions(db_path, "harry"), window_days=30, train_days=20)
    assert (split.train_session_ids, split.holdout_unseen_session_ids, split.holdout_continuing_session_ids) == (
        frozenset({s_train, s_cont}),
        frozenset({s_hold}),
        frozenset({s_cont}),
    )
    assert split.train_session_ids.isdisjoint(split.holdout_unseen_session_ids)

    out_dir = tmp_path / "dry_out"
    with patch("mindroom.providers.registry.build_model_provider", side_effect=AssertionError("LLM must not run")):
        await run_cli(
            [
                "--db",
                str(db_path),
                "--config",
                str(cfg_path),
                "--agent",
                "harry",
                "--out",
                str(out_dir),
                "--skills-dir",
                str(skills_dir),
                "--window-days",
                "30",
                "--train-days",
                "20",
                "--dry-run",
            ],
        )

    report = (out_dir / "report.md").read_text(encoding="utf-8")
    artifacts_path = out_dir / "artifacts.json"
    assert (artifacts_path.stat().st_mode & 0o777) == 0o600
    art = json.loads(artifacts_path.read_text(encoding="utf-8"))
    assert art["mode"] == "dry_run"
    assert (
        art["split"]["train_sessions"],
        art["split"]["holdout_unseen_sessions"],
        art["split"]["holdout_continuing_sessions"],
    ) == (2, 1, 1)
    assert art["stage1"]["top_error_buckets"][0]["tool_name"] == "run_shell_command"
    assert private_user_text not in report
    assert orig_skill.read_text(encoding="utf-8").endswith("Keep untouched.\n")


@pytest.mark.asyncio
async def test_blinded_judge_ordering_placebo_cache_and_cost(tmp_path: Path) -> None:
    """Blinded judge randomizes candidate order deterministically, scores placebo, caches (0600), and accounts cost."""
    cfg_path = _write_config(tmp_path / "config.yaml")
    runtime_paths = resolve_runtime_paths(config_path=cfg_path, storage_path=tmp_path / "_rt")
    config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    prices = model_prices(config)

    assert len({deterministic_arm_order(seed, "run_1") for seed in range(25)}) > 1
    assert deterministic_arm_order(23, "run_1") == deterministic_arm_order(23, "run_1")

    cases = [
        JudgeCase(
            item_id=f"run_{i}",
            session_id=f"{ROOM_A}:$h{i}",
            cluster_key="tool|scope|run_shell_command|pytest|nonzero_exit|validate_input",
            matched_skill="python-testing",
            failure_text=f"Failure {i}: pytest failed directly.",
            proposed_patch="MARKER_PATCH: Always run pytest via uv run pytest.",
            original_or_noop="MARKER_ORIG: Check python version.",
            placebo_unrelated_skill="MARKER_PLACEBO: Format calendar invites in UTC.",
        )
        for i in range(4)
    ]
    prompts_seen: list[str] = []

    async def fake_complete(prompt: str, _instructions: Sequence[str]) -> HelperResult[BlindedJudgeResponse]:
        prompts_seen.append(prompt)
        assert all(leaked not in prompt for leaked in ("proposed_patch", "original_or_noop", "placebo_unrelated_skill"))
        return _score_by_markers(prompt, direct="MARKER_PATCH", partial="MARKER_ORIG", inp=100_000, out=10_000)

    cache_path = tmp_path / "judge_cache.json"
    s1 = await evaluate_blinded_cases(
        cases,
        config=config,
        runtime_paths=runtime_paths,
        judge_model="judge_m",
        cache_path=cache_path,
        seed=23,
        prices=prices,
        complete_fn=fake_complete,
    )
    assert (len(prompts_seen), s1["total_evaluated"], s1["cache_hits"], s1["cache_misses"]) == (4, 4, 0, 4)
    assert (s1["direct_hit_rate"], s1["baseline_direct_hit_rate"], s1["placebo_hit_rate"]) == (
        pytest.approx(1.0),
        pytest.approx(0.0),
        pytest.approx(0.0),
    )
    assert (s1["arm_counts"]["original_or_noop"]["partial"], s1["cost"]["usd"]) == (4, pytest.approx(1.80))
    assert (cache_path.stat().st_mode & 0o777) == 0o600
    assert len({tuple(item["arm_order"]) for item in s1["items"]}) > 1

    s2 = await evaluate_blinded_cases(
        cases,
        config=config,
        runtime_paths=runtime_paths,
        judge_model="judge_m",
        cache_path=cache_path,
        seed=23,
        prices=prices,
        complete_fn=fake_complete,
    )
    assert (len(prompts_seen), s2["cache_hits"], s2["cache_misses"], s2["cost"]["usd"], s2["direct_hit_rate"]) == (
        4,
        4,
        0,
        pytest.approx(0.0),
        pytest.approx(1.0),
    )


def _seed_full_fixtures(tmp_path: Path, private_msg: str, private_arg: str) -> tuple[Path, Path, Path, str, str]:
    cfg_path, db_path, skills_dir = (
        _write_config(tmp_path / "config.yaml"),
        tmp_path / "harry.db",
        tmp_path / "orig_skills",
    )
    (skills_dir / "python-testing").mkdir(parents=True)
    (skills_dir / "calendar-notes").mkdir(parents=True)
    orig_py = "---\nname: python-testing\ndescription: Python checks\nmetadata:\n  mindroom:\n    learned: true\n---\n1. Inspect pytest output.\n"
    orig_cal = "---\nname: calendar-notes\ndescription: Calendar tips\n---\n1. Use ISO-8601 UTC dates.\n"
    (skills_dir / "python-testing" / "SKILL.md").write_text(orig_py, encoding="utf-8")
    (skills_dir / "calendar-notes" / "SKILL.md").write_text(orig_cal, encoding="utf-8")

    s_a1, s_a2, s_b1, s_cont, s_h1, s_h2 = (
        f"{ROOM_A}:$t1",
        f"{ROOM_A}:$t2",
        f"{ROOM_B}:$b1",
        f"{ROOM_A}:$cont",
        f"{ROOM_A}:$h1",
        f"{ROOM_A}:$h2",
    )
    _seed_v8_db(
        db_path,
        "harry",
        sessions=[
            (s_a1, NOW - 25 * DAY, NOW - 25 * DAY),
            (s_a2, NOW - 20 * DAY, NOW - 20 * DAY),
            (s_b1, NOW - 18 * DAY, NOW - 18 * DAY),
            (s_cont, NOW - 15 * DAY, NOW - 4 * DAY),
            (s_h1, NOW - 6 * DAY, NOW - 6 * DAY),
            (s_h2, NOW - 2 * DAY, NOW),
        ],
        runs=[
            {"run_id": "a1_r1", "session_id": s_a1, "seq": 1, "created_at": NOW - 25 * DAY},
            {"run_id": "a2_r1", "session_id": s_a2, "seq": 1, "created_at": NOW - 20 * DAY},
            {"run_id": "b1_r1", "session_id": s_b1, "seq": 1, "created_at": NOW - 18 * DAY},
            {"run_id": "c_r1", "session_id": s_cont, "seq": 1, "created_at": NOW - 15 * DAY},
            {"run_id": "c_r2", "session_id": s_cont, "seq": 2, "created_at": NOW - 4 * DAY},
            {"run_id": "h1_r1", "session_id": s_h1, "seq": 1, "created_at": NOW - 6 * DAY},
            {"run_id": "h2_r1", "session_id": s_h2, "seq": 1, "created_at": NOW},
        ],
        tools=[
            (rid, i, _fail_tool(f"pytest {private_arg}_{rid}_{i}.py", f"{rid}_{i}"))
            for rid in ("a1_r1", "a2_r1", "b1_r1", "c_r2", "h1_r1", "h2_r1")
            for i in (0, 1)
        ],
        messages=[
            ("a1_r1", 0, {"role": "user", "content": private_msg}),
            ("h1_r1", 0, {"role": "user", "content": private_msg}),
        ],
    )
    return cfg_path, db_path, skills_dir, orig_py, orig_cal


@pytest.mark.asyncio
async def test_full_backtest_end_to_end_room_isolation_diffs_and_privacy(tmp_path: Path) -> None:
    """Full backtest enforces room isolation, scratch skill diffs, blinded judging, cost accounting, and privacy."""
    private_msg, private_arg = "PRIVATE_SNIPPET_ALPHA_777", "PRIVATE_PATH_OMEGA_888"
    cfg_path, db_path, skills_dir, orig_py_text, orig_cal_text = _seed_full_fixtures(tmp_path, private_msg, private_arg)
    orig_py, orig_cal = skills_dir / "python-testing" / "SKILL.md", skills_dir / "calendar-notes" / "SKILL.md"
    before_db_bytes, before_py_mtime = db_path.read_bytes(), orig_py.stat().st_mtime_ns
    learner_clusters: list[FailureCluster] = []

    def make_audit_judge(usage_sink: Callable[[HelperUsage], object]) -> AuditJudgeFn:
        async def _audit(_card: EvidenceCard) -> AuditFinding:
            usage_sink(_helper_usage("OpenAI", "gpt-audit", inp=100_000, out=10_000))
            return AuditFinding(
                verdict="actionable",
                teachable=True,
                category="tool_misuse",
                remedy="validate_input",
                root_cause="Invoked pytest directly.",
                proposed_rule="Always run uv run pytest.",
                has_verified_fix=True,
                target_skill="python-testing",
            )

        return _audit

    async def fake_learner(
        *,
        skills_root: Path,
        cluster: FailureCluster,
        progress: ReviewProgress | None = None,
        **_kw: object,
    ) -> ReviewOutcome:
        learner_clusters.append(cluster)
        target = skills_root / "python-testing" / "SKILL.md"
        target.write_text(
            target.read_text(encoding="utf-8") + "2. Always run tests through `uv run pytest`.\n",
            encoding="utf-8",
        )
        if progress is not None:
            progress.changes["python-testing"] = "updated"
        metrics = RunMetrics()
        metrics.add(
            MessageMetrics(tokens=TokenCounts(input_tokens=50_000, output_tokens=5_000)),
            model_id="gpt-learner",
            provider="OpenAI",
        )
        return ReviewOutcome(
            changed_skills={"python-testing": "updated"},
            model_name="learner_m",
            provider="OpenAI",
            model_id="gpt-learner",
            input_tokens=50_000,
            output_tokens=5_000,
            model_requests=1,
            tool_calls=2,
            final_text="Updated.",
            metrics=metrics,
        )

    async def fake_blinded(prompt: str, _instructions: Sequence[str]) -> HelperResult[BlindedJudgeResponse]:
        return _score_by_markers(prompt, direct="uv run pytest", partial="Inspect pytest output", inp=20_000, out=2_000)

    out_dir = tmp_path / "full_out"
    art = await run_backtest(
        BacktestOptions(
            db_path=db_path,
            config_path=cfg_path,
            agent_name="harry",
            out_dir=out_dir,
            skills_dir=skills_dir,
            window_days=30,
            train_days=20,
            audit_model="audit_m",
            judge_model="judge_m",
            min_cluster_sessions=2,
            max_audits_train=20,
            max_audits_holdout=20,
            seed=23,
            dry_run=False,
        ),
        audit_judge_factory=make_audit_judge,
        learner_fn=fake_learner,
        blinded_complete_fn=fake_blinded,
    )

    assert db_path.read_bytes() == before_db_bytes
    assert (orig_py.read_text(encoding="utf-8"), orig_py.stat().st_mtime_ns, orig_cal.read_text(encoding="utf-8")) == (
        orig_py_text,
        before_py_mtime,
        orig_cal_text,
    )
    assert "uv run pytest" in (out_dir / "scratch_skills" / "skills" / "python-testing" / "SKILL.md").read_text(
        encoding="utf-8",
    )
    assert (len(learner_clusters), learner_clusters[0].distinct_sessions) == (1, 2)
    assert ROOM_A in learner_clusters[0].scope_key
    assert ROOM_B not in learner_clusters[0].scope_key
    assert (len(art["train"]["skill_diffs"]), art["train"]["skill_diffs"][0]["added_lines"]) == (1, 1)
    assert (art["split"]["holdout_unseen_sessions"], art["split"]["holdout_continuing_sessions"]) == (2, 1)
    assert (
        art["holdout"]["unseen"]["recurring_promoted_findings"],
        art["holdout"]["continuing"]["recurring_promoted_findings"],
    ) == (2, 1)
    assert (art["judge"]["total_evaluated"], art["judge"]["direct_hit_rate"], art["judge"]["placebo_hit_rate"]) == (
        2,
        pytest.approx(1.0),
        pytest.approx(0.0),
    )
    assert all(art["costs"][k]["usd"] > 0 for k in ("train_audit", "holdout_audit", "learner", "judge"))
    assert (
        (out_dir / "artifacts.json").stat().st_mode & 0o777,
        (out_dir / "judge_cache.json").stat().st_mode & 0o777,
    ) == (0o600, 0o600)

    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert all(s not in report for s in (private_msg, private_arg, "uv run pytest", "python-testing"))
    assert "Blinded Judge Evaluation" in report
    assert "Cost Breakdown" in report


@pytest.mark.asyncio
async def test_judge_provider_wiring_invalid_response_and_empty_db(tmp_path: Path) -> None:
    """Default blinded judge uses build_model_provider + complete_prompt; invalid replies aren't cached; empty DB works."""
    cfg_path = _write_config(tmp_path / "config.yaml")
    runtime_paths = resolve_runtime_paths(config_path=cfg_path, storage_path=tmp_path / "_rt")
    config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    case = JudgeCase(
        item_id="r_err",
        session_id=f"{ROOM_A}:$e1",
        cluster_key="k",
        matched_skill="s",
        failure_text="Failed.",
        proposed_patch="P",
        original_or_noop="O",
        placebo_unrelated_skill="X",
    )

    async def fake_complete_prompt(
        _provider: object,
        _prompt: str,
        **_kw: object,
    ) -> HelperResult[BlindedJudgeResponse]:
        return HelperResult(content="not-json", usage=_helper_usage("Anthropic", "claude-judge", inp=1_000, out=100))

    cache_path = tmp_path / "err_cache.json"
    with (
        patch("evals.trace_audit.judge.provider_registry.build_model_provider", return_value=object()) as build_mock,
        patch("evals.trace_audit.judge.complete_prompt", side_effect=fake_complete_prompt),
    ):
        res = await evaluate_blinded_cases(
            [case],
            config=config,
            runtime_paths=runtime_paths,
            judge_model="judge_m",
            cache_path=cache_path,
            seed=23,
            prices=model_prices(config),
        )
    assert build_mock.call_count == 1
    assert (res["total_evaluated"], res["judge_errors"], res["cache_misses"]) == (0, 1, 1)
    assert not cache_path.exists()

    empty_db = tmp_path / "empty.db"
    _seed_v8_db(empty_db, "harry", sessions=[], runs=[])
    out_dir = tmp_path / "empty_out"
    art = await run_backtest(
        BacktestOptions(
            db_path=empty_db,
            config_path=cfg_path,
            agent_name="harry",
            out_dir=out_dir,
            audit_model="audit_m",
            judge_model="judge_m",
            dry_run=False,
        ),
    )
    assert (art["split"]["train_sessions"], art["judge"]["total_evaluated"]) == (0, 0)
    assert (out_dir / "report.md").is_file()
