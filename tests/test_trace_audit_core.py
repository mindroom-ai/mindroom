"""Unit tests for the core passive trace auditor package."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from mindroom.constants import HOOK_SOURCE_RUN_METADATA_KEY
from mindroom.history.run_checkpoint import INTERRUPTED_CALL_RESULT
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.provider import Capabilities, ModelIdentity, ModelRequest, StreamEvent, TurnCompleted, UsageReported
from mindroom.llm.usage import HelperUsage, MessageMetrics, TokenCounts
from mindroom.providers.gemini.adapter import GeminiProvider, GeminiSettings
from mindroom.session_storage.schema import TableNames, create_log_schema, quote_identifier
from mindroom.trace_audit import (
    AuditedRun,
    AuditedSession,
    AuditFinding,
    EvidenceCard,
    FailureCluster,
    RunMessageRecord,
    ToolExecutionRecord,
    TraceAuditSettings,
    audit_evidence_card,
    build_evidence_card,
    classify_tool_execution,
    detect_session_incidents,
    latest_run_timestamp,
    load_audited_sessions,
    load_usage_rows,
    make_llm_audit_judge,
    run_audit_pass,
)
from mindroom.usage.storage import USAGE_KINDS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

NOW = 1_750_000_000.0
DAY = 86_400.0


def _tool(
    name: str = "run_shell_command",
    *,
    ord_idx: int = 0,
    args: dict[str, Any] | None = None,
    result: str = "ok",
    is_error: bool = False,
    confirmed: bool | None = None,
) -> ToolExecutionRecord:
    return ToolExecutionRecord(
        ord=ord_idx,
        tool_call_id=f"call_{ord_idx}",
        tool_name=name,
        tool_args=args or {},
        is_error=is_error,
        result=result,
        confirmed=confirmed,
    )


def _run(
    run_id: str,
    *,
    session_id: str = "!roomA:example.com:$t1",
    seq: int = 1,
    status: str = "COMPLETED",
    created_at: float = NOW - 3600,
    updated_at: float | None = None,
    duration: float | None = 5.0,
    error: str | None = None,
    content: str = "Done.",
    tools: tuple[ToolExecutionRecord, ...] = (),
    messages: tuple[RunMessageRecord, ...] = (),
    failing_tools: dict[str, int] | None = None,
) -> AuditedRun:
    return AuditedRun(
        run_id=run_id,
        session_id=session_id,
        seq=seq,
        status=status,
        created_at=created_at,
        updated_at=created_at + (duration or 0.0) if updated_at is None else updated_at,
        duration_seconds=duration,
        error=error,
        content=content,
        tools=tools,
        messages=messages,
        failing_tools=failing_tools or {},
    )


def _session(
    runs: tuple[AuditedRun, ...],
    *,
    session_id: str = "!roomA:example.com:$t1",
    room_id: str | None = "!roomA:example.com",
    last_updated_at: float | None = None,
) -> AuditedSession:
    created = runs[0].created_at if runs else NOW - 3600
    updated = (max(r.updated_at for r in runs) if runs else created) if last_updated_at is None else last_updated_at
    return AuditedSession(
        session_id=session_id,
        room_id=room_id,
        created_at=created,
        last_updated_at=updated,
        runs=runs,
    )


def _seed_v8_db(
    db_path: Path,
    storage_name: str,
    *,
    sessions: Sequence[dict[str, Any]],
    runs: Sequence[dict[str, Any]],
    tools: Sequence[tuple[str, int, dict[str, Any]]] = (),
    messages: Sequence[tuple[str, int, dict[str, Any]]] = (),
    removed: Sequence[tuple[str, str]] = (),
    usage_rows: Sequence[tuple[str, str, dict[str, Any]]] = (),
) -> None:
    names = TableNames(storage_name)
    conn = sqlite3.connect(db_path)
    try:
        create_log_schema(conn, names, HistoryScope(kind="agent", scope_id=storage_name))
        for s in sessions:
            conn.execute(
                f"INSERT INTO {quote_identifier(names.sessions)} "  # noqa: S608
                "(session_id, user_id, session_data, metadata, created_at, updated_at, session_state) "
                "VALUES (?, ?, NULL, ?, ?, ?, NULL)",
                (
                    s["session_id"],
                    s.get("user_id", "@alice:example.com"),
                    json.dumps(s["metadata"]) if s.get("metadata") is not None else None,
                    int(s.get("created_at", NOW - 3600)),
                    int(s.get("updated_at", NOW - 1200)),
                ),
            )
        for r in runs:
            rec = {
                "run_id": r["run_id"],
                "agent_id": storage_name,
                "content": r.get("content", "Completed task."),
                "error": r.get("error"),
                "metrics": {"duration": r.get("duration", 4.0)},
                "failing_tools": r.get("failing_tools", {}),
            }
            conn.execute(
                f"INSERT INTO {quote_identifier(names.log_runs)} "  # noqa: S608
                "(run_id, session_id, parent_run_id, kind, scope_key, status, seq, folded_by, has_tools, "
                "created_at, updated_at, metadata, record, requirements) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, ?, ?, ?, NULL)",
                (
                    r["run_id"],
                    r["session_id"],
                    r.get("parent_run_id"),
                    r.get("kind", "agent"),
                    f"agent:{storage_name}",
                    r.get("status", "COMPLETED"),
                    r.get("seq", 1),
                    int(r.get("created_at", NOW - 3600)),
                    int(r.get("updated_at", NOW - 1200)),
                    json.dumps(r["metadata"]) if r.get("metadata") is not None else None,
                    json.dumps(rec),
                ),
            )
        for run_id, ord_idx, payload in tools:
            conn.execute(
                f"INSERT INTO {quote_identifier(names.log_tools)} (run_id, ord, data) VALUES (?, ?, ?)",  # noqa: S608
                (run_id, ord_idx, json.dumps(payload)),
            )
        for run_id, ord_idx, payload in messages:
            conn.execute(
                f"INSERT INTO {quote_identifier(names.log_messages)} "  # noqa: S608
                "(run_id, message_id, ord, folded_by, data) VALUES (?, ?, ?, NULL, ?)",
                (run_id, f"msg_{run_id}_{ord_idx}", ord_idx, json.dumps(payload)),
            )
        for run_id, session_id in removed:
            conn.execute(
                f"INSERT INTO {quote_identifier(names.log_removed)} (run_id, session_id) VALUES (?, ?)",  # noqa: S608
                (run_id, session_id),
            )
        for session_id, run_id, payload in usage_rows:
            conn.execute(
                f"INSERT INTO {quote_identifier(names.usage)} (session_id, run_id, usage_data) VALUES (?, ?, ?)",  # noqa: S608
                (session_id, run_id, json.dumps(payload)),
            )
        conn.commit()
    finally:
        conn.close()


def test_usage_kind_includes_trace_audit() -> None:
    """Independent usage ledger exposes ``trace_audit`` as a first-class usage kind."""
    assert "trace_audit" in USAGE_KINDS


def test_classify_tool_execution_shell_benign_vs_real_failures() -> None:
    """Benign predicate exit-1 commands are suppressed while real failures classify cleanly."""
    for cmd in (
        "grep -rn missing_symbol src/",
        "rg --files-with-matches nope src/",
        "diff -u a.txt b.txt",
        "test -f /nonexistent",
        "git diff --quiet",
        "git -C /repo grep needle",
        "cd /repo && rg needle src/",
    ):
        out = classify_tool_execution(_tool(args={"command": cmd}, result="Exit code 1"))
        assert (out.kind, out.is_failure) == ("benign_exit_1", False), cmd

    list_grep = classify_tool_execution(_tool(args={"args": ["grep", "needle", "a.py"]}, result="Exit code 1"))
    assert (list_grep.kind, list_grep.is_failure) == ("benign_exit_1", False)

    real_cases = [
        ({"command": "pytest tests/test_a.py"}, "Exit code 1\nOutput:\n1 failed", "pytest", "exit_1"),
        ({"command": "git push origin main"}, "Exit code 1\nStderr:\nPermission denied", "git:push", "permission"),
        (
            {"command": "grep needle missing.txt"},
            "Exit code 1\nStderr:\ngrep: No such file or directory",
            "grep",
            "not_found",
        ),
        ({"command": "grep '[invalid' file.txt"}, "Exit code 2\nStderr:\nSyntax error", "grep", "syntax"),
        ({"command": "pytest && grep needle src/"}, "Exit code 1", "pytest", "exit_1"),
    ]
    for args, result, expected_op, expected_family in real_cases:
        out = classify_tool_execution(_tool(args=args, result=result))
        assert (out.kind, out.is_failure, out.operation, out.diagnostic_family) == (
            "nonzero_exit",
            True,
            expected_op,
            expected_family,
        )


def test_classify_tool_execution_priority_json_reader_and_special_cases() -> None:
    """Timeout, JSON error, reader-tool immunity, approval denial, and interruption follow priority rules."""
    timeout = classify_tool_execution(_tool(args={"command": "curl https://x"}, result="Stopped after 30s."))
    assert (timeout.kind, timeout.is_failure, timeout.diagnostic_family) == ("tool_timeout", True, "timeout")

    for payload in (
        '{"status": "error", "error": "rate limit exceeded"}',
        '{"mindroom_tool_output": {"status": "error", "error": "429 Too Many Requests"}}',
        '{"view_status": "error", "message": "not found"}',
        '{"ok": false, "error": "bad input"}',
        '{"errcode": "M_FORBIDDEN", "error": "Access denied"}',
    ):
        out = classify_tool_execution(_tool("custom_api", result=payload))
        assert (out.kind, out.is_failure) == ("json_error", True)

    for reader in ("read_file", "grep", "view_file", "matrix_message", "search_memories"):
        for text in ("Error: sample log line from file", "Traceback (most recent call last):\n  File x.py"):
            out = classify_tool_execution(_tool(reader, result=text))
            assert (out.kind, out.is_failure) == ("ok", False)

    non_reader = classify_tool_execution(_tool("edit_file", result="Error: file not found at /tmp/123e4567"))
    assert (non_reader.kind, non_reader.is_failure, non_reader.diagnostic_family) == ("text_error", True, "not_found")

    denied = classify_tool_execution(_tool(result="Not approved by requester", is_error=True, confirmed=False))
    assert (denied.kind, denied.is_failure) == ("approval_denied", False)

    interrupted = classify_tool_execution(_tool(result=INTERRUPTED_CALL_RESULT, is_error=True))
    assert (interrupted.kind, interrupted.is_failure) == ("interrupted", False)

    err = classify_tool_execution(_tool("edit_file", result="boom", is_error=True))
    assert (err.kind, err.is_failure) == ("tool_error", True)


def test_detect_session_incidents_material_weak_cancel_correction_and_sampler() -> None:
    """Stage 1 incident detection enforces settled idle window, material/weak rules, cancel exclusions, and clean sampling."""
    fresh = _session((_run("r_fresh", status="ERROR", created_at=NOW - 100, updated_at=NOW - 60),))
    assert detect_session_incidents(fresh, now=NOW, min_idle_seconds=600) == []

    fail_tool = _tool(args={"command": "pytest"}, result="Exit code 1\nOutput:\nfailed")
    s_mat = _session(
        (
            _run("r1", seq=1, created_at=NOW - 4000, tools=(fail_tool,)),
            _run("r2", seq=2, created_at=NOW - 3500, tools=(fail_tool,), failing_tools={"run_shell_command": 2}),
            _run("r3", seq=3, status="ERROR", created_at=NOW - 3000, error="crash"),
        ),
    )
    mat_inc = detect_session_incidents(s_mat, now=NOW, sample_rate=0.0)
    assert [i.run_id for i in mat_inc] == ["r2", "r3"]
    assert {"repeat_tool_error", "multi_tool_error", "failing_tools"} <= set(mat_inc[0].reasons)
    assert "run_error" in mat_inc[1].reasons

    loop_tools = tuple(_tool("search_api", ord_idx=i, args={"q": "x"}, result='{"ok": false}') for i in range(3))
    paged_tools = tuple(
        _tool("search_api", ord_idx=i, args={"q": "x", "offset": i * 10}, result='{"ok": false}') for i in range(3)
    )
    assert "tool_loop" in detect_session_incidents(_session((_run("rl", tools=loop_tools),)), now=NOW)[0].reasons
    assert "tool_loop" not in detect_session_incidents(_session((_run("rp", tools=paged_tools),)), now=NOW)[0].reasons

    nl_msg = RunMessageRecord(
        ord=0,
        role="user",
        content=(
            '<msg from="@u:x"><![CDATA[<mindroom_message_context><item key="k" cache_policy="stable">ctx</item>'
            "</mindroom_message_context>\n\nNee, verkeerd bestand!]]></msg>"
        ),
    )
    s_cancel = _session(
        (
            _run(
                "c_int",
                seq=1,
                status="CANCELLED",
                duration=30.0,
                tools=(_tool(result=INTERRUPTED_CALL_RESULT, is_error=True),),
            ),
            _run("c_short", seq=2, status="CANCELLED", duration=10.0, tools=()),
            _run("c_real", seq=3, status="CANCELLED", duration=25.0, created_at=NOW - 3000, updated_at=NOW - 2975),
            _run("r_nl", seq=4, created_at=NOW - 2900, updated_at=NOW - 2890, messages=(nl_msg,)),
        ),
    )
    cancel_inc = detect_session_incidents(s_cancel, now=NOW, sample_rate=0.0)
    assert [i.run_id for i in cancel_inc] == ["c_real"]
    assert set(cancel_inc[0].reasons) == {"settled_cancel", "user_correction"}
    assert cancel_inc[0].correction_text == "Nee, verkeerd bestand!"

    assert detect_session_incidents(_session((_run("r_one", tools=(fail_tool,)),)), now=NOW, sample_rate=0.0) == []
    for bad_reply, delay in (
        ("> No, quoted", 30),
        ("```\nwrong\n```", 30),
        ("No, " + "x" * 410, 30),
        ("No, late", 700),
    ):
        s_ign = _session(
            (
                _run("ra", seq=1, created_at=NOW - 3000, updated_at=NOW - 2990, tools=(fail_tool,)),
                _run(
                    "rb",
                    seq=2,
                    created_at=NOW - 2990 + delay,
                    messages=(RunMessageRecord(ord=0, role="user", content=bad_reply),),
                ),
            ),
        )
        assert detect_session_incidents(s_ign, now=NOW, sample_rate=0.0) == []

    s_en = _session(
        (
            _run("e1", seq=1, created_at=NOW - 3000, updated_at=NOW - 2990, tools=(fail_tool,)),
            _run(
                "e2",
                seq=2,
                created_at=NOW - 2950,
                messages=(RunMessageRecord(ord=0, role="user", content="@harry: No, run ruff first."),),
            ),
        ),
    )
    assert set(detect_session_incidents(s_en, now=NOW, sample_rate=0.0)[0].reasons) == {
        "single_tool_error",
        "user_correction",
    }

    outlier_tools = (fail_tool, *(_tool("read_file", ord_idx=i) for i in range(1, 15)))
    s_out = _session((_run("r_out", tools=outlier_tools),))
    assert detect_session_incidents(s_out, now=NOW, median_tools=None, sample_rate=0.0) == []
    out_inc = detect_session_incidents(s_out, now=NOW, median_tools=4.0, baseline_run_count=25, sample_rate=0.0)
    assert set(out_inc[0].reasons) == {"single_tool_error", "tool_outlier"}

    s_clean = _session((_run("tiny", seq=1, content="ok"), _run("long", seq=2, content="x" * 220)))
    sampled = detect_session_incidents(s_clean, now=NOW, sample_rate=1.0, sample_salt="salt")
    assert sampled == detect_session_incidents(s_clean, now=NOW, sample_rate=1.0, sample_salt="salt")
    assert [i.run_id for i in sampled] == ["long"]
    assert sampled[0].is_clean_sample


def test_source_read_only_and_exclusions(tmp_path: Path) -> None:
    """Read-only SQLite loader enforces mode=ro, filters excluded/other-room rows, and reads usage."""
    ro_dir = tmp_path / "ro_sessions"
    ro_dir.mkdir()
    db_path = ro_dir / "harry.db"
    _seed_v8_db(
        db_path,
        "harry",
        sessions=[
            {"session_id": "!roomA:example.com:$t1"},
            {"session_id": "!roomB:example.com:$t2"},
            {"session_id": "!roomA:example.com:$t1:dynamic_workflow:wf1"},
            {"session_id": "!roomA:example.com:$auto_thread"},
            {"session_id": "!roomA:example.com:$contact"},
        ],
        runs=[
            {"run_id": "keep_1", "session_id": "!roomA:example.com:$t1", "seq": 1},
            {
                "run_id": "auto_run",
                "session_id": "!roomA:example.com:$t1",
                "seq": 2,
                "metadata": {HOOK_SOURCE_RUN_METADATA_KEY: "automation/dreaming"},
            },
            {"run_id": "removed_run", "session_id": "!roomA:example.com:$t1", "seq": 3},
            {"run_id": "child_run", "session_id": "!roomA:example.com:$t1", "seq": 4, "parent_run_id": "keep_1"},
            {"run_id": "room_b_run", "session_id": "!roomB:example.com:$t2", "seq": 1},
            {"run_id": "wf_run", "session_id": "!roomA:example.com:$t1:dynamic_workflow:wf1", "seq": 1},
            {"run_id": "auto_thread_run", "session_id": "!roomA:example.com:$auto_thread", "seq": 1},
            {"run_id": "contact_run", "session_id": "!roomA:example.com:$contact", "seq": 1},
        ],
        tools=[
            (
                "keep_1",
                0,
                {
                    "tool_call_id": "tc1",
                    "tool_name": "run_shell_command",
                    "tool_args": {"command": "pytest"},
                    "tool_call_error": False,
                    "result": "Exit code 1",
                },
            ),
        ],
        messages=[
            ("keep_1", 0, {"role": "user", "content": "Run tests"}),
            ("keep_1", 1, {"role": "assistant", "content": "Failed."}),
        ],
        removed=[("removed_run", "!roomA:example.com:$t1")],
        usage_rows=[
            (
                "!roomA:example.com:$t1",
                "skill_learning:u1",
                {"kind": "skill_learning", "created_at": NOW - 2000, "metrics": {"input_tokens": 500}},
            ),
            (
                "!roomA:example.com:$t1",
                "keep_1",
                {"created_at": NOW - 2000, "metrics": {"input_tokens": 100}},
            ),
        ],
    )
    db_path.chmod(0o444)
    ro_dir.chmod(0o555)
    try:
        before = db_path.stat()
        sessions = load_audited_sessions(
            db_path,
            "harry",
            excluded_sessions=frozenset({"!roomA:example.com:$auto_thread"}),
            allowed_room_id="!roomA:example.com",
            contact_session_check=lambda sid: sid.endswith(":$contact"),
        )
        assert [s.session_id for s in sessions] == ["!roomA:example.com:$t1"]
        assert [r.run_id for r in sessions[0].runs] == ["keep_1"]
        assert sessions[0].runs[0].tools[0].tool_name == "run_shell_command"
        assert len(sessions[0].runs[0].messages) == 2

        usage = load_usage_rows(db_path, "harry", kind="skill_learning", since_ts=NOW - 5000)
        assert len(usage) == 1
        assert usage[0]["metrics"]["input_tokens"] == 500
        after = db_path.stat()
        assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)
    finally:
        ro_dir.chmod(0o755)
        db_path.chmod(0o644)


@pytest.mark.asyncio
async def test_evidence_card_bounds_redaction_and_llm_judge() -> None:
    """EvidenceCard enforces size/redaction bounds and recovery tags; LLM judge forwards usage and fallback."""
    fake_api_key = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
    tools = [
        _tool("get_skill_instructions", ord_idx=0, args={"skill_name": "python-testing"}, result="body"),
        *(
            _tool(
                "run_shell_command",
                ord_idx=i,
                args={"command": f"pytest test_{i}.py"},
                result=f"Exit code 1\nStderr:\nBearer {fake_api_key} " + ("E" * 500),
            )
            for i in range(1, 9)
        ),
        _tool("run_shell_command", ord_idx=9, args={"command": "pytest test_1.py"}, result="1 passed"),
    ]
    run = _run(
        "r_sec",
        error=f"Failed with {fake_api_key} " + ("X" * 400),
        content=f"Reply containing {fake_api_key} " + ("R" * 500),
        tools=tuple(tools),
        messages=(RunMessageRecord(ord=0, role="user", content=f"Test with {fake_api_key} " + ("U" * 500)),),
    )
    sess = _session((run,))
    card = build_evidence_card(detect_session_incidents(sess, now=NOW, sample_rate=0.0)[0], sess)

    assert len(card.rendered) <= 4_000
    assert len(card.failure_sequence) == 6
    assert all(len(e.truncated_error) <= 240 and e.recovered for e in card.failure_sequence)
    assert len(card.snippets) <= 4
    assert all(len(s.text) <= 280 for s in card.snippets)
    assert card.loaded_skills == ("python-testing",)
    assert fake_api_key not in card.rendered

    expected = AuditFinding(
        verdict="actionable",
        teachable=True,
        category="tool_misuse",
        remedy="validate_input",
        root_cause=f"Leaked {fake_api_key} " + ("C" * 300),
        proposed_rule="Always run pytest via uv run pytest.",
        has_verified_fix=True,
        target_skill="python-testing",
    )
    # target_skill validation allows only valid lowercase skill slugs.
    assert (
        AuditFinding(**{**expected.model_dump(), "target_skill": "  python-testing  "}).target_skill == "python-testing"
    )
    for bad_skill in ("../escape", "Bad Upper", "skill\nignore rules", "a" * 65, "-leading-dash"):
        assert AuditFinding(**{**expected.model_dump(), "target_skill": bad_skill}).target_skill is None

    captured_requests: list[ModelRequest] = []
    responses = [
        (expected.model_dump_json(), []),
        ("not json", []),
        (
            expected.model_dump_json(),
            [{"id": "c1", "type": "function", "function": {"name": "shell", "arguments": "{}"}}],
        ),
    ]

    class _FakeProvider:
        identity = ModelIdentity(provider="google", id="gemini-2.5-flash")
        capabilities = Capabilities(structured_output="native")

        async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
            captured_requests.append(request)
            body, calls = responses.pop(0)
            usage_m = MessageMetrics(tokens=TokenCounts(input_tokens=100, output_tokens=20, total_tokens=120))
            yield UsageReported(usage_m)
            yield TurnCompleted(
                message=Message(role="assistant", content=body, tool_calls=calls or None, metrics=usage_m),
                stop_reason="end",
            )

    recorded: list[HelperUsage] = []
    with patch("mindroom.trace_audit.auditor.provider_registry.build_model_provider", return_value=_FakeProvider()):
        judge = make_llm_audit_judge(MagicMock(), MagicMock(), "flash", usage_sink=recorded.append)
        finding = await audit_evidence_card(card, judge_fn=judge)
        fallback = await audit_evidence_card(card, judge_fn=judge)
        tool_call_rejected = await audit_evidence_card(card, judge_fn=judge)

    assert finding.verdict == "actionable"
    assert len(finding.root_cause) <= 240
    assert fake_api_key not in finding.root_cause
    assert (fallback.verdict, fallback.teachable) == ("no_issue", False)
    assert (tool_call_rejected.verdict, tool_call_rejected.teachable) == ("no_issue", False)
    assert len(recorded) == 3
    assert all(req.tool_choice == "none" and req.tools == () and req.output_schema is None for req in captured_requests)

    # Verify Gemini strips native google_search when given the exact ModelRequest built by make_llm_audit_judge.
    gemini = GeminiProvider(
        identity=ModelIdentity(provider="google", id="gemini-2.5-flash"),
        capabilities=Capabilities(structured_output="native"),
        settings=GeminiSettings(api_key="test-key", search=True),
    )
    gen_cfg = gemini._request_config(captured_requests[0], system_instruction="sys")
    assert getattr(gen_cfg, "tools", None) is None
    assert getattr(gen_cfg, "response_mime_type", None) == "application/json"


@pytest.mark.asyncio
async def test_pipeline_clustering_room_isolation_cooldown_and_content_free_state(tmp_path: Path) -> None:
    """Pipeline clusters across 2 distinct sessions, isolates rooms, gates cooldowns, and keeps state content-free."""
    db_path = tmp_path / "harry.db"
    state_path = tmp_path / "trace_audit.json"
    private_phrase = "UNIQUE_PRIVATE_USER_PHRASE_XYZ"
    private_arg = "PRIVATE_ARG_PATH_999"

    def _fail_row(tag: str) -> dict[str, Any]:
        return {
            "tool_call_id": f"tc_{tag}",
            "tool_name": "run_shell_command",
            "tool_args": {"command": f"pytest {private_arg}_{tag}.py"},
            "tool_call_error": False,
            "result": "Exit code 1\nOutput:\nfailed",
        }

    _seed_v8_db(
        db_path,
        "harry",
        sessions=[{"session_id": "!roomA:example.com:$t1"}, {"session_id": "!roomB:example.com:$t1"}],
        runs=[
            {
                "run_id": "a1_r1",
                "session_id": "!roomA:example.com:$t1",
                "seq": 1,
                "created_at": NOW - 4000,
                "updated_at": NOW - 3900,
            },
            {
                "run_id": "a1_r2",
                "session_id": "!roomA:example.com:$t1",
                "seq": 2,
                "created_at": NOW - 3800,
                "updated_at": NOW - 3700,
            },
            {
                "run_id": "b1_r1",
                "session_id": "!roomB:example.com:$t1",
                "seq": 1,
                "created_at": NOW - 3600,
                "updated_at": NOW - 3500,
            },
        ],
        tools=[
            ("a1_r1", 0, _fail_row("1")),
            ("a1_r1", 1, _fail_row("2")),
            ("a1_r2", 0, _fail_row("3")),
            ("a1_r2", 1, _fail_row("4")),
            ("b1_r1", 0, _fail_row("5")),
            ("b1_r1", 1, _fail_row("6")),
        ],
        messages=[
            ("a1_r1", 0, {"role": "user", "content": private_phrase}),
            ("b1_r1", 0, {"role": "user", "content": private_phrase}),
        ],
    )

    async def fake_judge(_card: EvidenceCard) -> AuditFinding:
        return AuditFinding(
            verdict="actionable",
            teachable=True,
            category="tool_misuse",
            remedy="validate_input",
            root_cause="Ran pytest directly without uv run.",
            proposed_rule="Always invoke pytest through uv run pytest.",
            has_verified_fix=True,
        )

    nominated: list[FailureCluster] = []

    async def fake_nominate(cluster: FailureCluster) -> str:
        nominated.append(cluster)
        return "saved"

    settings = TraceAuditSettings(min_cluster_sessions=2, cooldown_days=14, sample_rate=0.0)

    res1 = await run_audit_pass(
        db_path=db_path,
        storage_name="harry",
        state_path=state_path,
        scope_key="agent:harry",
        judge_fn=fake_judge,
        now=NOW,
        settings=settings,
        nominate_fn=fake_nominate,
    )
    assert res1.incidents_audited == 3
    assert res1.promoted_clusters == ()
    assert nominated == []

    # Add 2nd distinct session in Room A -> promotes!
    conn = sqlite3.connect(db_path)
    names = TableNames("harry")
    conn.execute(
        f"INSERT INTO {quote_identifier(names.sessions)} "  # noqa: S608
        "(session_id, user_id, session_data, metadata, created_at, updated_at, session_state) "
        "VALUES ('!roomA:example.com:$t2', '@alice:example.com', NULL, NULL, ?, ?, NULL)",
        (int(NOW - 2000), int(NOW - 1500)),
    )
    conn.execute(
        f"INSERT INTO {quote_identifier(names.log_runs)} "  # noqa: S608
        "(run_id, session_id, parent_run_id, kind, scope_key, status, seq, folded_by, has_tools, "
        "created_at, updated_at, metadata, record, requirements) "
        "VALUES ('a2_r1', '!roomA:example.com:$t2', NULL, 'agent', 'agent:harry', 'COMPLETED', 1, NULL, 1, ?, ?, NULL, ?, NULL)",
        (int(NOW - 2000), int(NOW - 1500), json.dumps({"run_id": "a2_r1", "content": "Failed."})),
    )
    for idx in (0, 1):
        conn.execute(
            f"INSERT INTO {quote_identifier(names.log_tools)} (run_id, ord, data) VALUES ('a2_r1', ?, ?)",  # noqa: S608
            (idx, json.dumps(_fail_row(f"a2_{idx}"))),
        )
    conn.commit()
    conn.close()

    res2 = await run_audit_pass(
        db_path=db_path,
        storage_name="harry",
        state_path=state_path,
        scope_key="agent:harry",
        judge_fn=fake_judge,
        now=NOW + 100,
        settings=settings,
        nominate_fn=fake_nominate,
    )
    assert len(res2.promoted_clusters) == 1
    assert res2.promoted_clusters[0].distinct_sessions == 2
    assert len(nominated) == 1

    res3 = await run_audit_pass(
        db_path=db_path,
        storage_name="harry",
        state_path=state_path,
        scope_key="agent:harry",
        judge_fn=fake_judge,
        now=NOW + 5 * DAY,
        settings=settings,
        nominate_fn=fake_nominate,
    )
    assert res3.promoted_clusters == ()
    assert len(nominated) == 1

    async def crashing_nominate(_cluster: FailureCluster) -> None:
        msg = "simulated learner crash"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="simulated learner crash"):
        await run_audit_pass(
            db_path=db_path,
            storage_name="harry",
            state_path=state_path,
            scope_key="agent:harry",
            judge_fn=fake_judge,
            now=NOW + 15 * DAY,
            settings=settings,
            nominate_fn=crashing_nominate,
        )

    res_after = await run_audit_pass(
        db_path=db_path,
        storage_name="harry",
        state_path=state_path,
        scope_key="agent:harry",
        judge_fn=fake_judge,
        now=NOW + 15 * DAY + 3600,
        settings=settings,
        nominate_fn=fake_nominate,
    )
    assert res_after.promoted_clusters == ()
    assert len(nominated) == 1

    raw_state = state_path.read_text(encoding="utf-8")
    assert private_phrase not in raw_state
    assert private_arg not in raw_state
    assert set(json.loads(raw_state)) >= {"session_cursors", "findings", "cooldowns", "metrics"}


@pytest.mark.asyncio
async def test_pipeline_reserves_clean_samples_and_defers_overflow_cursor(tmp_path: Path) -> None:
    """Budget cap reserves clean-sample slots and leaves deferred friction runs for the next pass."""
    db_path = tmp_path / "harry.db"
    state_path = tmp_path / "trace_audit.json"
    _seed_v8_db(
        db_path,
        "harry",
        sessions=[{"session_id": "!roomA:example.com:$t1"}, {"session_id": "!roomA:example.com:$t2"}],
        runs=[
            {"run_id": "err_1", "session_id": "!roomA:example.com:$t1", "seq": 1, "status": "ERROR", "error": "boom"},
            {"run_id": "err_2", "session_id": "!roomA:example.com:$t1", "seq": 2, "status": "ERROR", "error": "boom"},
            {"run_id": "clean_1", "session_id": "!roomA:example.com:$t2", "seq": 1, "content": "x" * 250},
        ],
    )

    seen_runs: list[str] = []

    async def judge(card: EvidenceCard) -> AuditFinding:
        seen_runs.append(card.run_id)
        return AuditFinding(
            verdict="no_issue",
            teachable=False,
            category="other",
            remedy="none",
            root_cause="ok",
            proposed_rule="",
        )

    cfg = TraceAuditSettings(max_audits_per_run=2, max_clean_samples_per_run=1, sample_rate=1.0)
    pass1 = await run_audit_pass(
        db_path=db_path,
        storage_name="harry",
        state_path=state_path,
        scope_key="agent:harry",
        judge_fn=judge,
        now=NOW,
        settings=cfg,
    )
    assert (pass1.incidents_audited, pass1.clean_samples_audited) == (2, 1)
    assert seen_runs == ["err_1", "clean_1"]

    pass2 = await run_audit_pass(
        db_path=db_path,
        storage_name="harry",
        state_path=state_path,
        scope_key="agent:harry",
        judge_fn=judge,
        now=NOW + 60,
        settings=cfg,
    )
    assert pass2.incidents_audited == 1
    assert seen_runs == ["err_1", "clean_1", "err_2"]


def test_shell_preamble_extraction_clean_followup_and_delegate_exclusion(tmp_path: Path) -> None:
    """Pin real-data backtest fixes: shell preambles, no duplicate follow-up friction, delegate exclusion, latest_ts."""
    preamble_err = classify_tool_execution(
        ToolExecutionRecord(
            ord=0,
            tool_call_id="tc_pre",
            tool_name="run_shell_command",
            tool_args={"args": "set -e\ncd /tmp\nexport FOO=1\npython3 script.py"},
            is_error=False,
            result="Exit code 1\nStderr:\nTraceback",
        ),
    )
    assert preamble_err.is_failure is True
    assert preamble_err.operation == "python3"

    # A clean follow-up run after an earlier multi-error run must NOT re-emit a duplicate friction incident.
    failing_tool = ToolExecutionRecord(
        ord=0,
        tool_call_id="tc_f1",
        tool_name="run_shell_command",
        tool_args={"args": "pytest"},
        is_error=False,
        result="Exit code 1\nfailed",
    )
    session = AuditedSession(
        session_id="!roomA:example.com:$t1",
        room_id="!roomA:example.com",
        created_at=NOW - 3_000,
        last_updated_at=NOW - 1_000,
        runs=(
            AuditedRun(
                run_id="r_fail",
                session_id="!roomA:example.com:$t1",
                seq=1,
                status="COMPLETED",
                created_at=NOW - 2_000,
                updated_at=NOW - 1_900,
                duration_seconds=5.0,
                error=None,
                content="failed",
                tools=(
                    failing_tool,
                    ToolExecutionRecord(1, "tc_f2", "run_shell_command", {"args": "pytest"}, False, "Exit code 1"),
                ),
                messages=(),
            ),
            AuditedRun(
                run_id="r_clean_followup",
                session_id="!roomA:example.com:$t1",
                seq=2,
                status="COMPLETED",
                created_at=NOW - 1_500,
                updated_at=NOW - 1_000,
                duration_seconds=2.0,
                error=None,
                content="all good now",
                tools=(ToolExecutionRecord(0, "tc_ok", "run_shell_command", {"args": "pytest"}, False, "passed"),),
                messages=(),
            ),
        ),
    )
    incidents = detect_session_incidents(session, now=NOW, sample_rate=0.0)
    assert [inc.run_id for inc in incidents] == ["r_fail"]

    db_path = tmp_path / "delegate.db"
    _seed_v8_db(
        db_path,
        "harry",
        sessions=[
            {"session_id": "!roomA:example.com:$t1", "created_at": NOW - 2_000, "updated_at": NOW - 1_000},
            {"session_id": "delegate:harry:harry:abc123", "created_at": NOW - 2_000, "updated_at": NOW - 1_000},
        ],
        runs=[
            {
                "run_id": "r1",
                "session_id": "!roomA:example.com:$t1",
                "seq": 1,
                "created_at": NOW - 1_500,
                "updated_at": NOW - 1_200,
            },
            {
                "run_id": "r_del",
                "session_id": "delegate:harry:harry:abc123",
                "seq": 1,
                "created_at": NOW - 1_400,
                "updated_at": NOW - 1_100,
            },
        ],
    )
    loaded = load_audited_sessions(db_path, "harry")
    assert [s.session_id for s in loaded] == ["!roomA:example.com:$t1"]
    assert latest_run_timestamp(db_path, "harry") == NOW - 1_100


def test_source_symlink_refusal_and_post_cutoff_updated_at_exclusion(tmp_path: Path) -> None:
    """Source refuses symlinked DB or parent dir, follows symlinked ancestors, and excludes post-cutoff updated_at."""
    real_root = tmp_path / "real_root"
    real_dir = real_root / "sessions"
    real_dir.mkdir(parents=True)
    db_path = real_dir / "agent.db"
    cutoff = NOW - 1_000
    _seed_v8_db(
        db_path,
        "agent",
        sessions=[{"session_id": "!roomA:example.com:$t1", "created_at": cutoff - 500, "updated_at": NOW}],
        runs=[
            {
                "run_id": "r_before",
                "session_id": "!roomA:example.com:$t1",
                "seq": 1,
                "created_at": cutoff - 300,
                "updated_at": cutoff - 100,
            },
            {
                "run_id": "r_straddle",
                "session_id": "!roomA:example.com:$t1",
                "seq": 2,
                "created_at": cutoff - 50,
                "updated_at": cutoff + 200,
            },
        ],
    )

    # 1. Symlinked DB file is refused.
    db_link = real_dir / "link.db"
    db_link.symlink_to(db_path)
    with pytest.raises(ValueError, match="link"):
        load_audited_sessions(db_link, "agent")
    with pytest.raises(ValueError, match="link"):
        latest_run_timestamp(db_link, "agent")
    with pytest.raises(ValueError, match="link"):
        load_usage_rows(db_link, "agent")

    # 2. Dangling symlink DB file is also refused.
    dangling = real_dir / "dangling.db"
    dangling.symlink_to(real_dir / "missing.db")
    with pytest.raises(ValueError, match="link"):
        load_audited_sessions(dangling, "agent")

    # 3. Symlinked immediate parent directory is refused.
    linked_parent = tmp_path / "linked_sessions"
    linked_parent.symlink_to(real_dir)
    with pytest.raises(ValueError, match="link"):
        load_audited_sessions(linked_parent / "agent.db", "agent")

    # 4. Symlinked ancestor ABOVE db_path.parent is allowed (e.g., /tmp or /dev/shm symlinks).
    ancestor_link = tmp_path / "ancestor_link"
    ancestor_link.symlink_to(real_root)
    loaded = load_audited_sessions(ancestor_link / "sessions" / "agent.db", "agent", until_ts=cutoff)
    assert [[r.run_id for r in s.runs] for s in loaded] == [["r_before"]]


@pytest.mark.asyncio
async def test_pipeline_default_window_room_switch_no_starvation_and_latest_session_id(tmp_path: Path) -> None:
    """Default 30d window, active-room cluster filter, max_audits_per_run=1 progress, and latest_session_id."""
    db_path = tmp_path / "agent.db"
    state_path = tmp_path / "trace_audit.json"
    _seed_v8_db(
        db_path,
        "agent",
        sessions=[
            {"session_id": "!roomA:example.com:$old", "created_at": NOW - 45 * 86_400, "updated_at": NOW - 45 * 86_400},
            {"session_id": "!roomA:example.com:$z_first", "created_at": NOW - 5_000, "updated_at": NOW - 4_000},
            {"session_id": "!roomA:example.com:$a_second", "created_at": NOW - 3_000, "updated_at": NOW - 2_000},
            {"session_id": "!roomA:example.com:$clean", "created_at": NOW - 2_500, "updated_at": NOW - 2_000},
            {"session_id": "!roomB:example.com:$b1", "created_at": NOW - 1_500, "updated_at": NOW - 1_200},
        ],
        runs=[
            {
                "run_id": "r_expired",
                "session_id": "!roomA:example.com:$old",
                "seq": 1,
                "status": "ERROR",
                "error": "ancient",
                "created_at": NOW - 45 * 86_400,
                "updated_at": NOW - 45 * 86_400,
            },
            {
                "run_id": "r_z_first",
                "session_id": "!roomA:example.com:$z_first",
                "seq": 1,
                "status": "ERROR",
                "error": "boom",
                "created_at": NOW - 4_500,
                "updated_at": NOW - 4_000,
            },
            {
                "run_id": "r_a_second",
                "session_id": "!roomA:example.com:$a_second",
                "seq": 1,
                "status": "ERROR",
                "error": "boom",
                "created_at": NOW - 2_500,
                "updated_at": NOW - 2_000,
            },
            {
                "run_id": "r_clean",
                "session_id": "!roomA:example.com:$clean",
                "seq": 1,
                "status": "COMPLETED",
                "content": "y" * 260,
                "created_at": NOW - 2_200,
                "updated_at": NOW - 2_000,
            },
            {
                "run_id": "r_b1",
                "session_id": "!roomB:example.com:$b1",
                "seq": 1,
                "status": "COMPLETED",
                "content": "z" * 260,
                "created_at": NOW - 1_400,
                "updated_at": NOW - 1_200,
            },
        ],
    )

    audited_ids: list[str] = []

    async def judge(card: EvidenceCard) -> AuditFinding:
        audited_ids.append(card.run_id)
        is_err = card.run_id in {"r_z_first", "r_a_second"}
        return AuditFinding(
            verdict="actionable" if is_err else "no_issue",
            teachable=is_err,
            category="tool_misuse" if is_err else "other",
            remedy="validate_input" if is_err else "none",
            root_cause="Repeated failure" if is_err else "Clean",
            proposed_rule="Check preconditions." if is_err else "",
        )

    # max_audits_per_run=1 must NOT starve friction behind clean samples, AND default since_ts excludes r_expired!
    cfg = TraceAuditSettings(
        max_audits_per_run=1,
        max_clean_samples_per_run=2,
        sample_rate=1.0,
        min_cluster_sessions=2,
        window_days=30,
    )
    p1 = await run_audit_pass(
        db_path=db_path,
        storage_name="agent",
        state_path=state_path,
        scope_key="agent:agent",
        judge_fn=judge,
        now=NOW,
        allowed_room_id="!roomA:example.com",
        settings=cfg,
    )
    assert p1.incidents_audited == 1
    assert p1.clean_samples_audited == 0
    assert audited_ids == ["r_z_first"]
    assert "r_expired" not in audited_ids

    # Second pass audits the second friction run and promotes the 2-session cluster with latest_session_id=$a_second!
    p2 = await run_audit_pass(
        db_path=db_path,
        storage_name="agent",
        state_path=state_path,
        scope_key="agent:agent",
        judge_fn=judge,
        now=NOW + 60,
        allowed_room_id="!roomA:example.com",
        settings=cfg,
    )
    assert audited_ids == ["r_z_first", "r_a_second"]
    assert len(p2.actionable_clusters) == 1
    cluster = p2.actionable_clusters[0]
    assert cluster.session_ids == ("!roomA:example.com:$a_second", "!roomA:example.com:$z_first")
    assert cluster.latest_session_id == "!roomA:example.com:$a_second"

    # Switching active room to !roomB (which has only a clean run and zero friction):
    # 1) max_audits_per_run=1 audits the clean sample when friction is empty;
    # 2) persisted !roomA clusters are NEVER surfaced or promoted in !roomB (even with cooldown=0).
    nominated: list[str] = []
    p3 = await run_audit_pass(
        db_path=db_path,
        storage_name="agent",
        state_path=state_path,
        scope_key="agent:agent",
        judge_fn=judge,
        now=NOW + 180,
        allowed_room_id="!roomB:example.com",
        settings=TraceAuditSettings(
            max_audits_per_run=1,
            max_clean_samples_per_run=2,
            min_cluster_sessions=2,
            cooldown_days=0,
            sample_rate=1.0,
        ),
        nominate_fn=lambda c: asyncio.sleep(0, result=nominated.append(c.cluster_key)),
    )
    assert (p3.incidents_audited, p3.clean_samples_audited) == (1, 1)
    assert audited_ids[-1] == "r_b1"
    assert p3.actionable_clusters == ()
    assert p3.promoted_clusters == ()
    assert nominated == []


@pytest.mark.asyncio
async def test_shell_operation_allowlist_strips_private_executables(tmp_path: Path) -> None:
    """Allowlisted POSIX/dev binaries emit operations while private/custom script names map to empty string."""
    for cmd, expected_op in (
        ("pytest tests/", "pytest"),
        ("uv run pytest", "uv"),
        ("git -C /repo push origin main", "git:push"),
        ("[ -f missing ]", "["),
        ("[[ -d missing ]]", "[["),
        ("docker compose up", "docker"),
        ("sqlite3 db.sqlite .tables", "sqlite3"),
        ("./client-orchid-release --prod", ""),
        ("/opt/internal/acme-deploy-script --force", ""),
        ("FOO=1 ./private-helper-bin arg", ""),
        ("my_custom_cli do-thing", ""),
    ):
        out = classify_tool_execution(_tool(args={"command": cmd}, result="Exit code 1\nStderr:\nfailed"))
        assert (out.kind, out.is_failure, out.operation) == ("nonzero_exit", True, expected_op), cmd

    db_path = tmp_path / "private_bin.db"
    _seed_v8_db(
        db_path,
        "agent",
        sessions=[
            {"session_id": "!roomA:example.com:$p1", "created_at": NOW - 3000, "updated_at": NOW - 3000},
            {"session_id": "!roomA:example.com:$p2", "created_at": NOW - 2000, "updated_at": NOW - 2000},
        ],
        runs=[
            {"run_id": "r_p1", "session_id": "!roomA:example.com:$p1", "seq": 1, "created_at": NOW - 3000},
            {"run_id": "r_p2", "session_id": "!roomA:example.com:$p2", "seq": 1, "created_at": NOW - 2000},
        ],
        tools=[
            (
                "r_p1",
                0,
                {
                    "tool_call_id": "t1",
                    "tool_name": "run_shell_command",
                    "tool_args": {"command": "./client-orchid-release --prod"},
                    "result": "Exit code 1\nStderr:\nboom",
                },
            ),
            (
                "r_p1",
                1,
                {
                    "tool_call_id": "t2",
                    "tool_name": "run_shell_command",
                    "tool_args": {"command": "./client-orchid-release --prod"},
                    "result": "Exit code 1\nStderr:\nboom",
                },
            ),
            (
                "r_p2",
                0,
                {
                    "tool_call_id": "t3",
                    "tool_name": "run_shell_command",
                    "tool_args": {"command": "./client-orchid-release --staging"},
                    "result": "Exit code 1\nStderr:\nboom",
                },
            ),
            (
                "r_p2",
                1,
                {
                    "tool_call_id": "t4",
                    "tool_name": "run_shell_command",
                    "tool_args": {"command": "./client-orchid-release --staging"},
                    "result": "Exit code 1\nStderr:\nboom",
                },
            ),
        ],
    )
    state_path = tmp_path / "state.json"

    async def judge(_card: EvidenceCard) -> AuditFinding:
        return AuditFinding(
            verdict="actionable",
            teachable=True,
            category="tool_misuse",
            remedy="validate_input",
            root_cause="Invoked release script with wrong flag.",
            proposed_rule="Check required flags before running release commands.",
        )

    res = await run_audit_pass(
        db_path=db_path,
        storage_name="agent",
        state_path=state_path,
        scope_key="agent:agent",
        judge_fn=judge,
        now=NOW,
        settings=TraceAuditSettings(min_cluster_sessions=2, sample_rate=0.0),
    )
    assert len(res.actionable_clusters) == 1
    assert res.actionable_clusters[0].operation == ""
    assert "client-orchid-release" not in res.actionable_clusters[0].cluster_key
    assert "client-orchid-release" not in state_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_audit_evidence_card_drops_unloaded_target_skill_before_clustering(tmp_path: Path) -> None:
    """Invented target_skill values not in card.loaded_skills are coerced to None before clustering."""
    run_loaded = _run(
        "r_loaded",
        error="Run failed",
        tools=(_tool("get_skill_instructions", ord_idx=0, args={"skill_name": "python-testing"}, result="ok"),),
    )
    sess = _session((run_loaded,))
    card = build_evidence_card(detect_session_incidents(sess, now=NOW, sample_rate=0.0)[0], sess)
    assert card.loaded_skills == ("python-testing",)

    async def judge_loaded(_card: EvidenceCard) -> AuditFinding:
        return AuditFinding(
            verdict="actionable",
            teachable=True,
            category="skill_bug",
            remedy="fix_skill",
            root_cause="Skill step outdated.",
            proposed_rule="Update the loaded skill step.",
            target_skill="python-testing",
        )

    async def judge_invented(_card: EvidenceCard) -> AuditFinding:
        return AuditFinding(
            verdict="actionable",
            teachable=True,
            category="skill_bug",
            remedy="fix_skill",
            root_cause="Skill step outdated.",
            proposed_rule="Update the loaded skill step.",
            target_skill="invented-slug",
        )

    assert (await audit_evidence_card(card, judge_fn=judge_loaded)).target_skill == "python-testing"
    assert (await audit_evidence_card(card, judge_fn=judge_invented)).target_skill is None

    # In the pipeline, two sessions whose judge invents different skill slugs still merge into one cluster.
    db_path = tmp_path / "skills.db"
    _seed_v8_db(
        db_path,
        "agent",
        sessions=[
            {"session_id": "!roomA:example.com:$k1", "created_at": NOW - 3000, "updated_at": NOW - 3000},
            {"session_id": "!roomA:example.com:$k2", "created_at": NOW - 2000, "updated_at": NOW - 2000},
        ],
        runs=[
            {
                "run_id": "r_k1",
                "session_id": "!roomA:example.com:$k1",
                "seq": 1,
                "created_at": NOW - 3000,
                "status": "ERROR",
                "error": "boom",
            },
            {
                "run_id": "r_k2",
                "session_id": "!roomA:example.com:$k2",
                "seq": 1,
                "created_at": NOW - 2000,
                "status": "ERROR",
                "error": "boom",
            },
        ],
    )
    slugs = iter(("invented-alpha", "invented-beta"))

    async def judge_split(_card: EvidenceCard) -> AuditFinding:
        return AuditFinding(
            verdict="actionable",
            teachable=True,
            category="verification_gap",
            remedy="verify_outcome",
            root_cause="Skipped final check.",
            proposed_rule="Verify outputs before reporting done.",
            target_skill=next(slugs),
        )

    res = await run_audit_pass(
        db_path=db_path,
        storage_name="agent",
        state_path=tmp_path / "state.json",
        scope_key="agent:agent",
        judge_fn=judge_split,
        now=NOW,
        settings=TraceAuditSettings(min_cluster_sessions=2, sample_rate=0.0),
    )
    assert len(res.actionable_clusters) == 1
    cluster = res.actionable_clusters[0]
    assert cluster.target_skill is None
    assert "invented-alpha" not in cluster.cluster_key
    assert "invented-beta" not in cluster.cluster_key


@pytest.mark.asyncio
async def test_pipeline_per_card_audit_error_defers_only_failed_card(tmp_path: Path) -> None:
    """One failing audit card preserves sibling findings and defers only its own session cursor for retry."""
    db_path = tmp_path / "resilient.db"
    s1, s2, s3 = "!roomA:example.com:$s1", "!roomA:example.com:$s2", "!roomA:example.com:$s3"
    _seed_v8_db(
        db_path,
        "agent",
        sessions=[
            {"session_id": s1, "created_at": NOW - 4000, "updated_at": NOW - 4000},
            {"session_id": s2, "created_at": NOW - 3000, "updated_at": NOW - 3000},
            {"session_id": s3, "created_at": NOW - 2000, "updated_at": NOW - 2000},
        ],
        runs=[
            {
                "run_id": "r_ok_1",
                "session_id": s1,
                "seq": 1,
                "created_at": NOW - 4000,
                "status": "ERROR",
                "error": "fail",
            },
            {"run_id": "r_bad_pre", "session_id": s2, "seq": 1, "created_at": NOW - 3500},
            {
                "run_id": "r_bad",
                "session_id": s2,
                "seq": 2,
                "created_at": NOW - 3000,
                "status": "ERROR",
                "error": "fail",
            },
            {
                "run_id": "r_ok_2",
                "session_id": s3,
                "seq": 1,
                "created_at": NOW - 2000,
                "status": "ERROR",
                "error": "fail",
            },
        ],
    )
    state_path = tmp_path / "state.json"
    calls: list[str] = []
    fail_once = {"r_bad"}

    async def flaky_judge(card: EvidenceCard) -> AuditFinding:
        calls.append(card.run_id)
        if card.run_id in fail_once:
            fail_once.remove(card.run_id)
            msg = "transient provider 502"
            raise RuntimeError(msg)
        return AuditFinding(
            verdict="actionable",
            teachable=True,
            category="verification_gap",
            remedy="verify_outcome",
            root_cause="Missing check.",
            proposed_rule="Check output.",
        )

    p1 = await run_audit_pass(
        db_path=db_path,
        storage_name="agent",
        state_path=state_path,
        scope_key="agent:agent",
        judge_fn=flaky_judge,
        now=NOW,
        settings=TraceAuditSettings(min_cluster_sessions=2, sample_rate=0.0, audit_concurrency=3),
    )
    assert p1.incidents_audited == 2
    assert {f.run_id for f in p1.findings} == {"r_ok_1", "r_ok_2"}
    assert len(p1.actionable_clusters) == 1
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["session_cursors"][s1] == 1
    assert saved["session_cursors"][s2] == 1  # Deferred before r_bad (seq 2).
    assert saved["session_cursors"][s3] == 1

    calls.clear()
    p2 = await run_audit_pass(
        db_path=db_path,
        storage_name="agent",
        state_path=state_path,
        scope_key="agent:agent",
        judge_fn=flaky_judge,
        now=NOW + 60,
        settings=TraceAuditSettings(min_cluster_sessions=2, sample_rate=0.0),
    )
    assert calls == ["r_bad"]
    assert p2.incidents_audited == 1
    saved2 = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved2["session_cursors"][s2] == 2
