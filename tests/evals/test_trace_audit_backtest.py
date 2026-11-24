"""Tests for the offline chronological trace-audit backtest CLI and evaluation modules."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from evals.trace_audit.baseline import compute_baseline_a, compute_baseline_b, compute_stage1_breakdown
from evals.trace_audit.judge import (
    BlindedCandidateScore,
    BlindedJudgeResponse,
    JudgeCase,
    build_judge_cases,
    centered_skill_update_excerpts,
    deterministic_arm_order,
    evaluate_blinded_cases,
)
from evals.trace_audit.run import BacktestOptions, run_backtest, run_cli, split_sessions_chronologically
from evals.trace_audit.workspace import prepare_isolated_runtime
from mindroom.agent_loop.helper import HelperResult
from mindroom.budgets.pricing import model_prices
from mindroom.config.main import load_config
from mindroom.config.personal_agents import PersonalAgentRosterEntry
from mindroom.constants import resolve_runtime_paths
from mindroom.contacts.store import ContactBook, ContactParty, ContactRecord, ContactSide
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message, model_provenance
from mindroom.llm.provider import Capabilities, ModelIdentity, ModelRequest, StreamEvent, TurnCompleted, UsageReported
from mindroom.llm.usage import HelperUsage, MessageMetrics, RunMetrics, TokenCounts
from mindroom.session_storage.schema import TableNames, create_log_schema, quote_identifier
from mindroom.skill_learning.audit_reviewer import ReviewOutcome
from mindroom.trace_audit.models import AuditFinding, AuditJudgeFn, EvidenceCard, FailureCluster, FindingRecord
from mindroom.trace_audit.source import load_audited_sessions

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
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
    assert (out_dir.stat().st_mode & 0o777) == 0o700
    assert "**Dataset:** `dataset`" in report
    assert all(s not in report for s in (private_msg, private_arg, "uv run pytest", "python-testing", "harry"))
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

    captured_requests: list[ModelRequest] = []

    class _BadJudgeProvider:
        identity = ModelIdentity(provider="anthropic", id="claude-judge")
        capabilities = Capabilities(structured_output="native")

        async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
            captured_requests.append(request)
            usage_m = MessageMetrics(tokens=TokenCounts(input_tokens=1_000, output_tokens=100, total_tokens=1_100))
            yield UsageReported(usage_m)
            yield TurnCompleted(
                message=Message(role="assistant", content="not-json", metrics=usage_m),
                stop_reason="end",
            )

    cache_path = tmp_path / "err_cache.json"
    with patch(
        "evals.trace_audit.judge.provider_registry.build_model_provider",
        return_value=_BadJudgeProvider(),
    ) as build_mock:
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
    assert len(captured_requests) == 1
    assert captured_requests[0].tool_choice == "none"
    assert captured_requests[0].tools == ()
    assert captured_requests[0].output_schema is None
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


def _seed_isolation_fixtures(tmp_path: Path) -> tuple[Path, Path, str]:
    cfg_path = tmp_path / "config.yaml"
    cfg_text = (
        "models:\n"
        "  audit_m:\n"
        "    provider: openai\n"
        "    id: gpt-audit\n"
        "    context_window: 128000\n"
        "  judge_m:\n"
        "    provider: anthropic\n"
        "    id: claude-judge\n"
        "    context_window: 200000\n"
        "router:\n"
        "  model: audit_m\n"
        "debug:\n"
        "  log_llm_requests: true\n"
        "  llm_request_log_dir: /tmp/should-never-be-written\n"
        "agents:\n"
        "  secret_agent_name:\n"
        "    display_name: Secret\n"
        "    model: audit_m\n"
        "    rooms: []\n"
    )
    cfg_path.write_text(cfg_text, encoding="utf-8")
    db_path = tmp_path / "agent.db"
    s_train, s_straddle, s_hold = f"{ROOM_A}:$t1", f"{ROOM_A}:$straddle", f"{ROOM_A}:$h1"
    cutoff_approx = NOW - 10 * DAY
    _seed_v8_db(
        db_path,
        "secret_agent_name",
        sessions=[
            (s_train, NOW - 25 * DAY, NOW - 25 * DAY),
            (s_straddle, cutoff_approx - 500, cutoff_approx + 500),
            (s_hold, NOW - 2 * DAY, NOW),
        ],
        runs=[
            {"run_id": "t_ok", "session_id": s_train, "seq": 1, "created_at": NOW - 25 * DAY},
            {"run_id": "straddle_run", "session_id": s_straddle, "seq": 1, "created_at": cutoff_approx - 50},
            {"run_id": "h_ok", "session_id": s_hold, "seq": 1, "created_at": NOW},
        ],
        tools=[
            ("t_ok", 0, _fail_tool("pytest a.py", "t0")),
            ("t_ok", 1, _fail_tool("pytest b.py", "t1")),
            ("straddle_run", 0, _fail_tool("pytest c.py", "s0")),
            ("straddle_run", 1, _fail_tool("pytest d.py", "s1")),
        ],
    )
    conn = sqlite3.connect(db_path)
    try:
        names = TableNames("secret_agent_name")
        conn.execute(
            f"UPDATE {quote_identifier(names.log_runs)} SET updated_at = ? WHERE run_id = 'straddle_run'",  # noqa: S608
            (int(cutoff_approx + 5_000),),
        )
        conn.commit()
    finally:
        conn.close()
    return cfg_path, db_path, cfg_text


@pytest.mark.asyncio
async def test_backtest_rejects_overlapping_skills_dir_and_same_model_cli(tmp_path: Path) -> None:
    """Overlapping --skills-dir never deletes scratch files, and same audit/judge model is rejected without flag."""
    cfg_path, db_path, _ = _seed_isolation_fixtures(tmp_path)
    out_overlap = tmp_path / "out_overlap"
    scratch = out_overlap / "scratch_skills" / "skills"
    keep_file = scratch / "keep.txt"
    keep_file.parent.mkdir(parents=True)
    keep_file.write_text("do-not-delete", encoding="utf-8")
    for bad_skills in (out_overlap, out_overlap / "scratch_skills", scratch, scratch / "nested"):
        bad_skills.mkdir(parents=True, exist_ok=True)
        with pytest.raises(ValueError, match="overlap"):
            await run_backtest(
                BacktestOptions(
                    db_path=db_path,
                    config_path=cfg_path,
                    agent_name="secret_agent_name",
                    out_dir=out_overlap,
                    skills_dir=bad_skills,
                    dry_run=True,
                ),
            )
    assert keep_file.read_text(encoding="utf-8") == "do-not-delete"

    with pytest.raises(SystemExit):
        await run_cli(
            [
                "--db",
                str(db_path),
                "--config",
                str(cfg_path),
                "--agent",
                "secret_agent_name",
                "--out",
                str(tmp_path / "same_out"),
                "--audit-model",
                "audit_m",
                "--judge-model",
                "audit_m",
            ],
        )


@pytest.mark.asyncio
async def test_backtest_isolation_symlinks_config_copy_roster_label_and_cutoff_leakage(tmp_path: Path) -> None:
    """Backtest skips symlinks, copies config with disabled request logging, supports --roster-path/--label, and blocks straddle leaks."""
    cfg_path, db_path, cfg_text = _seed_isolation_fixtures(tmp_path)
    outside_secret = tmp_path / "outside_secret.txt"
    outside_secret.write_text("TOP_SECRET", encoding="utf-8")
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "SKILL.md").write_text("---\nname: evil\ndescription: Evil\n---\n", encoding="utf-8")

    skills_dir = tmp_path / "skills_src"
    (skills_dir / "good-skill").mkdir(parents=True)
    (skills_dir / "good-skill" / "SKILL.md").write_text(
        "---\nname: good-skill\ndescription: Good\n---\n1. Check.\n",
        encoding="utf-8",
    )
    (skills_dir / "good-skill" / "secret_link.txt").symlink_to(outside_secret)
    (skills_dir / "evil-link").symlink_to(outside_dir)

    roster_file = tmp_path / "custom_roster.json"
    roster_file.write_text('{"entries": [], "created": []}', encoding="utf-8")

    seen_train_runs: list[str] = []
    seen_debug: list[tuple[bool, str | None]] = []
    seen_config_paths: list[Path] = []

    def audit_factory(_sink: Callable[[HelperUsage], object]) -> AuditJudgeFn:
        async def _audit(card: EvidenceCard) -> AuditFinding:
            seen_train_runs.append(card.run_id)
            return AuditFinding(
                verdict="actionable",
                teachable=True,
                category="tool_misuse",
                remedy="validate_input",
                root_cause="Bad args.",
                proposed_rule="Check args.",
            )

        return _audit

    async def capture_learner(
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        **_kw: object,
    ) -> ReviewOutcome:
        seen_debug.append((config.debug.log_llm_requests, config.debug.llm_request_log_dir))
        seen_config_paths.append(runtime_paths.config_path)
        return ReviewOutcome(
            changed_skills={},
            model_name="audit_m",
            provider="openai",
            model_id="gpt-audit",
            input_tokens=0,
            output_tokens=0,
            model_requests=0,
            tool_calls=0,
        )

    out_iso = tmp_path / "out_iso"
    await run_backtest(
        BacktestOptions(
            db_path=db_path,
            config_path=cfg_path,
            agent_name="secret_agent_name",
            out_dir=out_iso,
            skills_dir=skills_dir,
            roster_path=roster_file,
            label="cohort-alpha",
            audit_model="audit_m",
            judge_model="audit_m",
            allow_same_model=True,
            min_cluster_sessions=1,
            dry_run=False,
        ),
        audit_judge_factory=audit_factory,
        learner_fn=capture_learner,
        blinded_complete_fn=AsyncMock(),
    )

    assert (out_iso.stat().st_mode & 0o777) == 0o700
    assert ((out_iso / "_runtime").stat().st_mode & 0o777) == 0o700
    assert ((out_iso / "scratch_skills").stat().st_mode & 0o777) == 0o700
    assert not (out_iso / "_runtime" / "config.yaml").exists()
    roster_dst = out_iso / "_runtime" / "tracking" / "personal_agents.json"
    assert roster_dst.read_text(encoding="utf-8") == '{"entries": [], "created": []}'
    assert cfg_path.read_text(encoding="utf-8") == cfg_text
    assert seen_debug == [(False, None)]
    assert seen_config_paths == [cfg_path.resolve()]
    assert (out_iso / "scratch_skills" / "skills" / "good-skill" / "SKILL.md").is_file()
    assert not (out_iso / "scratch_skills" / "skills" / "good-skill" / "secret_link.txt").exists()
    assert not (out_iso / "scratch_skills" / "skills" / "evil-link").exists()
    assert "t_ok" in seen_train_runs
    assert "straddle_run" not in seen_train_runs

    report = (out_iso / "report.md").read_text(encoding="utf-8")
    assert "**Dataset:** `cohort-alpha`" in report
    assert "secret_agent_name" not in report


def test_workspace_split_include_config_read_only_and_existing_out_dir_perms(tmp_path: Path) -> None:
    """Split `!include` configs load in memory without mutating source, and existing out_dir keeps its permissions."""
    cfg_dir = tmp_path / "split_cfg"
    cfg_dir.mkdir()
    models_yaml = cfg_dir / "models.yaml"
    agents_yaml = cfg_dir / "agents.yaml"
    cfg_path = cfg_dir / "config.yaml"
    models_yaml.write_text(
        "audit_m:\n  provider: openai\n  id: gpt-audit\njudge_m:\n  provider: anthropic\n  id: claude-judge\n",
        encoding="utf-8",
    )
    agents_yaml.write_text(
        "harry:\n  display_name: Harry\n  model: audit_m\n  rooms: []\n",
        encoding="utf-8",
    )
    cfg_text = (
        "models: !include models.yaml\n"
        "agents: !include agents.yaml\n"
        "debug:\n"
        "  log_llm_requests: true\n"
        "  llm_request_log_dir: /tmp/must-stay-disabled\n"
        "router:\n"
        "  model: audit_m\n"
    )
    cfg_path.write_text(cfg_text, encoding="utf-8")

    # Lock config directory read-only to prove zero writes to source or config_path.parent.
    for file_path in (models_yaml, agents_yaml, cfg_path):
        file_path.chmod(0o444)
    cfg_dir.chmod(0o555)

    existing_out = tmp_path / "existing_out"
    existing_out.mkdir()
    existing_out.chmod(0o755)
    try:
        config, runtime_paths = prepare_isolated_runtime(out_dir=existing_out, config_path=cfg_path)
    finally:
        cfg_dir.chmod(0o755)
        for file_path in (models_yaml, agents_yaml, cfg_path):
            file_path.chmod(0o644)

    assert "harry" in config.agents
    assert set(config.models) == {"audit_m", "judge_m"}
    assert config.debug.log_llm_requests is False
    assert config.debug.llm_request_log_dir is None
    assert runtime_paths.storage_root == (existing_out / "_runtime").resolve()
    assert (existing_out.stat().st_mode & 0o777) == 0o755
    assert ((existing_out / "_runtime").stat().st_mode & 0o777) == 0o700
    assert ((existing_out / "scratch_skills").stat().st_mode & 0o777) == 0o700
    assert cfg_path.read_text(encoding="utf-8") == cfg_text

    # A legacy config needing access migration also loads in memory without mutating the source file.
    legacy_dir = tmp_path / "legacy_cfg"
    legacy_dir.mkdir()
    legacy_cfg = legacy_dir / "config.yaml"
    legacy_text = (
        "models:\n"
        "  audit_m:\n"
        "    provider: openai\n"
        "    id: gpt-audit\n"
        "router:\n"
        "  model: audit_m\n"
        "agents:\n"
        "  harry:\n"
        "    display_name: Harry\n"
        "    model: audit_m\n"
        "    rooms: []\n"
        "authorization:\n"
        "  global_users: []\n"
    )
    legacy_cfg.write_text(legacy_text, encoding="utf-8")
    legacy_cfg.chmod(0o444)
    legacy_dir.chmod(0o555)
    try:
        legacy_loaded, _ = prepare_isolated_runtime(out_dir=tmp_path / "legacy_out", config_path=legacy_cfg)
    finally:
        legacy_dir.chmod(0o755)
        legacy_cfg.chmod(0o644)
    assert "harry" in legacy_loaded.agents
    assert legacy_cfg.read_text(encoding="utf-8") == legacy_text


@pytest.mark.asyncio
async def test_backtest_rejects_same_resolved_model_aliases_and_levels(tmp_path: Path) -> None:
    """Distinct model keys or @level suffixes resolving to the same (provider, id) are rejected unless allowed."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "models:\n"
        "  alias_a:\n"
        "    provider: openai\n"
        "    id: gpt-4o\n"
        "  alias_b:\n"
        "    provider: openai\n"
        "    id: gpt-4o\n"
        "  sonnet:\n"
        "    provider: anthropic\n"
        "    id: claude-sonnet-4-5\n"
        "router:\n"
        "  model: alias_a\n"
        "agents:\n"
        "  harry:\n"
        "    display_name: Harry\n"
        "    model: alias_a\n"
        "    rooms: []\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "harry.db"
    _seed_v8_db(db_path, "harry", sessions=[], runs=[])

    for audit_m, judge_m in (
        ("alias_a", "alias_b"),
        ("alias_a@low", "alias_b@high"),
        ("sonnet@low", "sonnet@high"),
    ):
        with pytest.raises(ValueError, match=r"same model|must differ"):
            await run_backtest(
                BacktestOptions(
                    db_path=db_path,
                    config_path=cfg_path,
                    agent_name="harry",
                    out_dir=tmp_path / f"out_{audit_m}_{judge_m}",
                    audit_model=audit_m,
                    judge_model=judge_m,
                    allow_same_model=False,
                    dry_run=False,
                ),
            )

    # Passing allow_same_model=True permits aliases pointing to the same underlying model.
    allowed_out = tmp_path / "out_allowed"
    art = await run_backtest(
        BacktestOptions(
            db_path=db_path,
            config_path=cfg_path,
            agent_name="harry",
            out_dir=allowed_out,
            audit_model="alias_a",
            judge_model="alias_b",
            allow_same_model=True,
            dry_run=False,
        ),
        audit_judge_factory=lambda _sink: AsyncMock(),
        blinded_complete_fn=AsyncMock(),
    )
    assert art["mode"] == "full"


@pytest.mark.asyncio
async def test_blinded_judge_centers_updated_long_skill_around_appended_hunk(tmp_path: Path) -> None:
    """Updated long skills (>4,000 chars) pass hunk-centered excerpts so appended rules reach the blinded judge."""
    frontmatter = "---\nname: long-skill\ndescription: Long workflow guide\n---\n"
    filler = "".join(f"Step {i:03d}: " + ("general background guidance " * 3) + "\n" for i in range(80))
    appended_rule = "Step 999: Always verify command flags with uv run pytest --tb=short.\n"
    original = frontmatter + filler
    proposed = original + appended_rule
    assert len(original) > 5_000

    finding = FindingRecord(
        cluster_key="tool|scope|run_shell_command|pytest|nonzero_exit|validate_input",
        scope_key="scope",
        session_id=f"{ROOM_A}:$h1",
        run_id="h_long",
        created_at=NOW,
        verdict="actionable",
        teachable=True,
        category="tool_misuse",
        remedy="validate_input",
        tool_name="run_shell_command",
        operation="pytest",
        kind="nonzero_exit",
        diagnostic_family="exit_1",
        root_cause="Ran bare pytest instead of uv run pytest.",
        proposed_rule="Use uv run pytest --tb=short.",
    )
    diffs = [
        {
            "skill_name": "long-skill",
            "action": "updated",
            "cluster_key": finding.cluster_key,
            "tool_name": "run_shell_command",
            "operation": "pytest",
            "category": "tool_misuse",
            "remedy": "validate_input",
        },
    ]
    cases = build_judge_cases(
        [finding],
        {},
        diffs=diffs,
        initial_skills={"long-skill": original},
        frozen_skills={"long-skill": proposed},
        seed=23,
    )
    assert len(cases) == 1
    case = cases[0]
    assert len(case.proposed_patch) <= 4_000
    assert len(case.original_or_noop) <= 4_000
    assert case.proposed_patch != case.original_or_noop
    assert "uv run pytest --tb=short" in case.proposed_patch
    assert "uv run pytest --tb=short" not in case.original_or_noop
    assert "name: long-skill" in case.proposed_patch
    assert "name: long-skill" in case.original_or_noop
    assert "@@" not in case.proposed_patch

    prompts_seen: list[str] = []

    async def fake_complete(prompt: str, _inst: Sequence[str]) -> HelperResult[BlindedJudgeResponse]:
        prompts_seen.append(prompt)
        return _score_by_markers(
            prompt,
            direct="uv run pytest --tb=short",
            partial="Long workflow guide",
            inp=1_000,
            out=100,
        )

    cfg_path = _write_config(tmp_path / "config.yaml")
    runtime_paths = resolve_runtime_paths(config_path=cfg_path, storage_path=tmp_path)
    config = load_config(runtime_paths)
    summary = await evaluate_blinded_cases(
        cases,
        config=config,
        runtime_paths=runtime_paths,
        judge_model="judge_m",
        cache_path=tmp_path / "judge_cache.json",
        seed=23,
        prices=model_prices(config),
        complete_fn=fake_complete,
    )
    assert len(prompts_seen) == 1
    assert "uv run pytest --tb=short" in prompts_seen[0]
    assert summary["direct_hit_rate"] == pytest.approx(1.0)
    assert summary["baseline_direct_hit_rate"] == pytest.approx(0.0)

    # Single-line >4,000-char append fallback also surfaces the appended text within <=4,000 chars.
    single_orig = "A" * 5_000
    single_prop = single_orig + " UNIQUE_APPENDED_RULE"
    orig_ex, prop_ex = centered_skill_update_excerpts(single_orig, single_prop, limit=4_000)
    assert len(orig_ex) <= 4_000
    assert len(prop_ex) <= 4_000
    assert "UNIQUE_APPENDED_RULE" in prop_ex
    assert "UNIQUE_APPENDED_RULE" not in orig_ex


@pytest.mark.asyncio
async def test_backtest_excludes_contact_room_sessions(tmp_path: Path) -> None:
    """Backtest wires contact_session_check into window loading, train audit, and holdout audit."""
    cfg_dir = tmp_path / "deploy"
    tracking = cfg_dir / "mindroom_data" / "tracking"
    tracking.mkdir(parents=True)
    cfg_path = cfg_dir / "config.yaml"
    cfg_path.write_text(
        "models:\n"
        "  audit_m:\n"
        "    provider: openai\n"
        "    id: gpt-audit\n"
        "  judge_m:\n"
        "    provider: anthropic\n"
        "    id: claude-judge\n"
        "router:\n"
        "  model: audit_m\n"
        "personal_agents:\n"
        "  mind:\n"
        "    agent:\n"
        "      display_name: '{user_display_name} Mind'\n"
        "      model: audit_m\n"
        "      rooms: []\n",
        encoding="utf-8",
    )
    roster_entry = PersonalAgentRosterEntry(
        template="mind",
        user_id="@alice:example.com",
        username="alice",
        agent_name="mind_alice",
        matrix_username=None,
        user_display_name="Alice",
        source_room_id=ROOM_A,
    )
    (tracking / "personal_agents.json").write_text(
        json.dumps({"entries": [roster_entry.model_dump(mode="json")], "created": []}),
        encoding="utf-8",
    )
    contact_room = "!contact_room:example.com"
    book = ContactBook(
        contacts=[
            ContactRecord(
                id="c1",
                status="active",
                requester=ContactParty(kind="agent", id="mind_alice", owner="@alice:example.com"),
                target=ContactParty(kind="person", id="@bob:example.com", owner="@bob:example.com"),
                reason="Collab",
                requester_side=ContactSide(),
                target_side=ContactSide(),
                origin_room_id=ROOM_A,
                origin_thread_id="$origin",
                room_id=contact_room,
            ),
        ],
    )
    (tracking / "contacts.json").write_text(book.model_dump_json(), encoding="utf-8")

    db_path = tmp_path / "mind_alice.db"
    s_own_train = f"{ROOM_A}:$own_t1"
    s_own_hold = f"{ROOM_A}:$own_h1"
    s_contact_train = f"{contact_room}:$ct_t1"
    s_contact_hold = f"{contact_room}:$ct_h1"
    _seed_v8_db(
        db_path,
        "mind_alice",
        sessions=[
            (s_own_train, NOW - 25 * DAY, NOW - 25 * DAY),
            (s_contact_train, NOW - 24 * DAY, NOW - 24 * DAY),
            (s_own_hold, NOW - 2 * DAY, NOW),
            (s_contact_hold, NOW - 1 * DAY, NOW),
        ],
        runs=[
            {"run_id": "own_t1", "session_id": s_own_train, "seq": 1, "created_at": NOW - 25 * DAY},
            {"run_id": "ct_t1", "session_id": s_contact_train, "seq": 1, "created_at": NOW - 24 * DAY},
            {"run_id": "own_h1", "session_id": s_own_hold, "seq": 1, "created_at": NOW - 2 * DAY},
            {"run_id": "ct_h1", "session_id": s_contact_hold, "seq": 1, "created_at": NOW - 1 * DAY},
        ],
        tools=[
            (rid, i, _fail_tool("pytest a.py", f"{rid}_{i}"))
            for rid in ("own_t1", "ct_t1", "own_h1", "ct_h1")
            for i in (0, 1)
        ],
        usage_rows=[
            (s_own_train, "sl_own", _usage_row("OpenAI", "gpt-audit", 1_000, 100)),
            (s_contact_train, "sl_contact", _usage_row("OpenAI", "gpt-audit", 9_000, 900)),
        ],
    )

    audited_sessions: list[str] = []

    def audit_factory(_sink: Callable[[HelperUsage], object]) -> AuditJudgeFn:
        async def _audit(card: EvidenceCard) -> AuditFinding:
            audited_sessions.append(card.session_id)
            return AuditFinding(
                verdict="actionable",
                teachable=True,
                category="tool_misuse",
                remedy="validate_input",
                root_cause="Bad args.",
                proposed_rule="Check args.",
            )

        return _audit

    art = await run_backtest(
        BacktestOptions(
            db_path=db_path,
            config_path=cfg_path,
            agent_name="mind_alice",
            out_dir=tmp_path / "contact_out",
            audit_model="audit_m",
            judge_model="judge_m",
            min_cluster_sessions=1,
            dry_run=False,
        ),
        audit_judge_factory=audit_factory,
        learner_fn=AsyncMock(
            return_value=ReviewOutcome(
                changed_skills={},
                model_name="audit_m",
                provider="openai",
                model_id="gpt-audit",
                input_tokens=0,
                output_tokens=0,
                model_requests=0,
                tool_calls=0,
            ),
        ),
        blinded_complete_fn=AsyncMock(),
    )
    assert art["stage1"]["total_sessions"] == 2
    assert art["baseline_a"]["review_count"] == 1
    assert set(audited_sessions) == {s_own_train, s_own_hold}
    assert all(contact_room not in sid for sid in audited_sessions)
