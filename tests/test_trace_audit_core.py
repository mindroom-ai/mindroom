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
    AuditCategory,
    AuditedRun,
    AuditedSession,
    AuditFinding,
    AuditPassResult,
    AuditRemedy,
    EvidenceCard,
    FailureCluster,
    NominateClusterFn,
    RunMessageRecord,
    ToolExecutionRecord,
    TraceAuditSettings,
    audit_evidence_card,
    build_evidence_card,
    classify_tool_execution,
    detect_session_incidents,
    load_audited_sessions,
    make_llm_audit_judge,
    run_audit_pass,
)
from mindroom.trace_audit.signals import extract_human_text, triggering_human_text
from mindroom.trace_audit.source import load_removed_run_ids
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
        updated_at=created_at + 5.0 if updated_at is None else updated_at,
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
    sessions: Sequence[dict[str, Any]] = (),
    runs: Sequence[dict[str, Any]] = (),
    tools: Sequence[tuple[str, int, dict[str, Any]]] = (),
    messages: Sequence[tuple[str, int, dict[str, Any]]] = (),
    removed: Sequence[tuple[str, str]] = (),
) -> None:
    names = TableNames(storage_name)
    with sqlite3.connect(db_path) as conn:
        create_log_schema(conn, names, HistoryScope(kind="agent", scope_id=storage_name))
        conn.executemany(
            f"INSERT OR IGNORE INTO {quote_identifier(names.sessions)} "  # noqa: S608
            "(session_id, user_id, session_data, metadata, created_at, updated_at, session_state) "
            "VALUES (?, ?, NULL, ?, ?, ?, NULL)",
            [
                (
                    s["session_id"],
                    s.get("user_id", "@alice:example.com"),
                    json.dumps(s["metadata"]) if s.get("metadata") is not None else None,
                    int(s.get("created_at", NOW - 3600)),
                    int(s.get("updated_at", NOW - 1200)),
                )
                for s in sessions
            ],
        )
        conn.executemany(
            f"INSERT INTO {quote_identifier(names.log_runs)} "  # noqa: S608
            "(run_id, session_id, parent_run_id, kind, scope_key, status, seq, folded_by, has_tools, "
            "created_at, updated_at, metadata, record, requirements) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, ?, ?, ?, NULL)",
            [
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
                    json.dumps(
                        {
                            "run_id": r["run_id"],
                            "agent_id": storage_name,
                            "content": r.get("content", "Completed task."),
                            "error": r.get("error"),
                            "failing_tools": r.get("failing_tools", {}),
                        },
                    ),
                )
                for r in runs
            ],
        )
        conn.executemany(
            f"INSERT INTO {quote_identifier(names.log_tools)} (run_id, ord, data) VALUES (?, ?, ?)",  # noqa: S608
            [(rid, ord_idx, json.dumps(payload)) for rid, ord_idx, payload in tools],
        )
        conn.executemany(
            f"INSERT INTO {quote_identifier(names.log_messages)} "  # noqa: S608
            "(run_id, message_id, ord, folded_by, data) VALUES (?, ?, ?, NULL, ?)",
            [(rid, f"msg_{rid}_{ord_idx}", ord_idx, json.dumps(payload)) for rid, ord_idx, payload in messages],
        )
        conn.executemany(
            f"INSERT INTO {quote_identifier(names.log_removed)} (run_id, session_id) VALUES (?, ?)",  # noqa: S608
            list(removed),
        )


def _db_run(
    rid: str,
    sid: str,
    seq: int = 1,
    ts: float = NOW - 3600,
    *,
    status: str = "COMPLETED",
    error: str | None = None,
    parent_run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": rid,
        "session_id": sid,
        "seq": seq,
        "created_at": ts,
        "updated_at": ts + 100,
        "status": status,
        "error": error,
        "parent_run_id": parent_run_id,
        "metadata": metadata,
    }


def _fail_row(tag: str, *, command: str = "pytest tests/") -> dict[str, Any]:
    return {
        "tool_call_id": f"tc_{tag}",
        "tool_name": "run_shell_command",
        "tool_args": {"command": command},
        "tool_call_error": False,
        "result": "Exit code 1\nOutput:\nfailed",
    }


def _finding(
    *,
    category: AuditCategory = "tool_misuse",
    remedy: AuditRemedy = "validate_input",
    root_cause: str = "Invoked release script with wrong flag.",
    proposed_rule: str = "Check required flags before running release commands.",
    target_skill: str | None = None,
) -> AuditFinding:
    return AuditFinding(
        verdict="actionable",
        teachable=True,
        category=category,
        remedy=remedy,
        root_cause=root_cause,
        proposed_rule=proposed_rule,
        has_verified_fix=True,
        target_skill=target_skill,
    )


def test_classify_tool_execution_priority_operations_and_diagnostics() -> None:
    """Tool classifier applies priority order, reader immunity, shell op redaction, and diagnostic families."""
    # Priority order: approval_denied -> interrupted -> tool_error -> output checks.
    assert (
        classify_tool_execution(_tool(result="Not approved", is_error=True, confirmed=False)).kind,
        classify_tool_execution(_tool(result="Not approved", is_error=True, confirmed=False)).is_failure,
    ) == ("approval_denied", False)
    interrupted = classify_tool_execution(_tool(result=INTERRUPTED_CALL_RESULT, is_error=True))
    assert (interrupted.kind, interrupted.is_failure) == ("interrupted", False)
    err = classify_tool_execution(_tool("edit_file", result="boom", is_error=True))
    assert (err.kind, err.is_failure) == ("tool_error", True)

    timeout = classify_tool_execution(_tool(args={"command": "curl https://x"}, result="Stopped after 30s."))
    assert (timeout.kind, timeout.is_failure, timeout.operation, timeout.diagnostic_family) == (
        "tool_timeout",
        True,
        "",
        "timeout",
    )

    # Shell tools always emit empty operation (never leaking custom binary/script names).
    for cmd, result, expected_family in (
        ("pytest tests/test_a.py", "Exit code 1\nOutput:\n1 failed", "exit_1"),
        ("git push origin main", "Exit code 1\nStderr:\nPermission denied", "permission"),
        ("./client-orchid-release --prod", "Exit code 2\nStderr:\nNo such file or directory", "not_found"),
        ("python -c 'bad'", "Exit code 1\nStderr:\nSyntaxError: invalid syntax", "syntax"),
    ):
        out = classify_tool_execution(_tool(args={"command": cmd}, result=result))
        assert (out.kind, out.is_failure, out.operation, out.diagnostic_family) == (
            "nonzero_exit",
            True,
            "",
            expected_family,
        )

    # Non-shell tools extract lowercase identifier from action/operation/method.
    api_err = classify_tool_execution(
        _tool("matrix_room", args={"action": "Create"}, result='{"errcode": "M_FORBIDDEN", "error": "Access denied"}'),
    )
    assert (api_err.kind, api_err.is_failure, api_err.operation, api_err.diagnostic_family) == (
        "json_error",
        True,
        "create",
        "permission",
    )
    for payload in (
        '{"status": "error", "error": "rate limit exceeded"}',
        '{"mindroom_tool_output": {"status": "error", "error": "429 Too Many Requests"}}',
        '{"view_status": "error", "message": "not found"}',
        '{"ok": false, "error": "bad input"}',
    ):
        out = classify_tool_execution(_tool("custom_api", args={"method": "bad space!"}, result=payload))
        assert (out.kind, out.is_failure, out.operation) == ("json_error", True, "")

    # Reader tools ignore anchored Error:/Traceback lines; non-reader tools classify them as text_error.
    for reader in ("read_file", "grep", "view_file", "matrix_message", "search_memories"):
        for text in ("Error: sample log line from file", "Traceback (most recent call last):\n  File x.py"):
            out = classify_tool_execution(_tool(reader, result=text))
            assert (out.kind, out.is_failure) == ("ok", False)
    non_reader = classify_tool_execution(_tool("edit_file", result="Error: file not found at /tmp/x"))
    assert (non_reader.kind, non_reader.is_failure, non_reader.diagnostic_family) == ("text_error", True, "not_found")


def test_detect_session_incidents_material_friction_windows_and_human_text() -> None:
    """Stage 1 enforces idle settlement, material-only friction, 3-run/24h windowing, and clean human text extraction."""
    fresh = _session((_run("r_fresh", status="ERROR", created_at=NOW - 100, updated_at=NOW - 60),))
    assert detect_session_incidents(fresh, now=NOW, min_idle_seconds=600) == []

    fail_shell = _tool(args={"command": "pytest"}, result="Exit code 1\nfailed")
    fail_api = _tool("custom_api", args={"action": "query"}, result='{"ok": false}')
    s_mat = _session(
        (
            _run("r1", seq=1, created_at=NOW - 4000, tools=(fail_shell,)),
            _run("r2", seq=2, created_at=NOW - 3500, tools=(fail_shell,), failing_tools={"run_shell_command": 2}),
            _run("r3", seq=3, status="ERROR", created_at=NOW - 3000, error="crash"),
            _run("r4", seq=4, created_at=NOW - 2500, tools=(_tool(result="passed"),)),
        ),
    )
    mat_inc = detect_session_incidents(s_mat, now=NOW)
    # Single error in r1 does not fire; clean follow-up r4 does not re-emit duplicate friction.
    assert [i.run_id for i in mat_inc] == ["r2", "r3"]
    assert {"repeat_tool_error", "multi_tool_error", "failing_tools"} <= set(mat_inc[0].reasons)
    assert mat_inc[1].reasons == ("run_error",)
    assert [i.run_id for i in detect_session_incidents(s_mat, now=NOW, after_seq=2)] == ["r3"]

    # Two distinct tools failing within 24h triggers multi_tool_error without repeat_tool_error; >24h does not combine.
    s_multi = _session(
        (
            _run("m1", seq=1, created_at=NOW - 3000, tools=(fail_shell,)),
            _run("m2", seq=2, created_at=NOW - 2500, tools=(fail_api,)),
        ),
    )
    assert detect_session_incidents(s_multi, now=NOW)[0].reasons == ("multi_tool_error",)
    s_stale = _session(
        (
            _run("old", seq=1, created_at=NOW - 2 * DAY, tools=(fail_shell,)),
            _run("new", seq=2, created_at=NOW - 2000, tools=(fail_shell,)),
        ),
    )
    assert detect_session_incidents(s_stale, now=NOW) == []

    # 3 consecutive identical failures fire tool_loop; changing args (pagination) does not.
    loop_tools = tuple(_tool("search_api", ord_idx=i, args={"q": "x"}, result='{"ok": false}') for i in range(3))
    paged_tools = tuple(
        _tool("search_api", ord_idx=i, args={"q": "x", "offset": i * 10}, result='{"ok": false}') for i in range(3)
    )
    assert "tool_loop" in detect_session_incidents(_session((_run("rl", tools=loop_tools),)), now=NOW)[0].reasons
    assert "tool_loop" not in detect_session_incidents(_session((_run("rp", tools=paged_tools),)), now=NOW)[0].reasons

    # Human text extraction strips Matrix/enrichment wrappers and skips synthetic/history/sent_by_you messages.
    wrapped = (
        '<msg from="@bot:x" sent_by_you="true"><![CDATA[echo]]></msg>'
        '<msg from="@u:x"><![CDATA[<mindroom_message_context><item key="k" cache_policy="stable">ctx</item>'
        "</mindroom_message_context>\n\nRun the test suite]]></msg>"
    )
    assert extract_human_text(wrapped) == "Run the test suite"
    assert (
        triggering_human_text(
            (
                RunMessageRecord(ord=0, role="user", content="hist", from_history=True),
                RunMessageRecord(ord=1, role="user", content="synth", is_synthetic=True),
                RunMessageRecord(ord=2, role="user", content=wrapped),
            ),
        )
        == "Run the test suite"
    )


def test_evidence_card_selects_triggering_human_prompt_over_preceding_context() -> None:
    """EvidenceCard captures the triggering user message rather than preceding unseen thread context."""
    run = _run(
        "r_trigger_prompt",
        status="ERROR",
        error="pytest failed",
        messages=(
            RunMessageRecord(ord=0, role="user", content="Lunch is at noon"),
            RunMessageRecord(ord=1, role="user", content="Run the test suite"),
        ),
    )
    sess = _session((run,))
    card = build_evidence_card(detect_session_incidents(sess, now=NOW)[0], sess)

    assert "user_ask: Run the test suite" in card.rendered
    assert "Lunch is at noon" not in card.rendered
    assert any(s.label == "user_ask" and s.text == "Run the test suite" for s in card.snippets)


def test_source_read_only_exclusions_and_symlink_refusal(tmp_path: Path) -> None:
    """Read-only SQLite loader preserves DB bytes, enforces all exclusions, and refuses symlinked DBs/parents."""
    real_root = tmp_path / "real_root"
    ro_dir = real_root / "sessions"
    ro_dir.mkdir(parents=True)
    db_path = ro_dir / "assistant.db"
    _seed_v8_db(
        db_path,
        "assistant",
        sessions=[
            {"session_id": "!roomA:example.com:$t1"},
            {"session_id": "!roomB:example.com:$t2"},
            {"session_id": "!roomA:example.com:$t1:dynamic_workflow:wf1"},
            {"session_id": "delegate:assistant:assistant:abc123"},
            {"session_id": "!roomA:example.com:$auto_thread"},
            {"session_id": "!roomA:example.com:$contact"},
        ],
        runs=[
            _db_run("keep_1", "!roomA:example.com:$t1", 1, NOW - 3000),
            _db_run("old_1", "!roomA:example.com:$t1", 0, NOW - 40 * DAY),
            _db_run(
                "auto_run",
                "!roomA:example.com:$t1",
                2,
                metadata={HOOK_SOURCE_RUN_METADATA_KEY: "automation/dreaming"},
            ),
            _db_run("removed_run", "!roomA:example.com:$t1", 3),
            _db_run("child_run", "!roomA:example.com:$t1", 4, parent_run_id="keep_1"),
            _db_run("room_b_run", "!roomB:example.com:$t2"),
            _db_run("wf_run", "!roomA:example.com:$t1:dynamic_workflow:wf1"),
            _db_run("del_run", "delegate:assistant:assistant:abc123"),
            _db_run("auto_thread_run", "!roomA:example.com:$auto_thread"),
            _db_run("contact_run", "!roomA:example.com:$contact"),
        ],
        tools=[("keep_1", 0, _fail_row("1"))],
        messages=[
            ("keep_1", 0, {"role": "user", "content": "Run tests"}),
            ("keep_1", 1, {"role": "assistant", "content": "Failed."}),
        ],
        removed=[("removed_run", "!roomA:example.com:$t1")],
    )

    # Symlink refusal: DB symlink, dangling symlink, and symlinked parent dir are refused; ancestor symlink allowed.
    db_link = ro_dir / "link.db"
    db_link.symlink_to(db_path)
    dangling = ro_dir / "dangling.db"
    dangling.symlink_to(ro_dir / "missing.db")
    linked_parent = tmp_path / "linked_sessions"
    linked_parent.symlink_to(ro_dir)
    for bad in (db_link, dangling, linked_parent / "assistant.db"):
        with pytest.raises(ValueError, match="link"):
            load_audited_sessions(bad, "assistant")
        with pytest.raises(ValueError, match="link"):
            load_removed_run_ids(bad, "assistant")

    ancestor_link = tmp_path / "ancestor_link"
    ancestor_link.symlink_to(real_root)

    db_path.chmod(0o444)
    ro_dir.chmod(0o555)
    try:
        before = db_path.stat()
        sessions = load_audited_sessions(
            ancestor_link / "sessions" / "assistant.db",
            "assistant",
            since_ts=NOW - 30 * DAY,
            excluded_sessions=frozenset({"!roomA:example.com:$auto_thread"}),
            allowed_room_id="!roomA:example.com",
            contact_session_check=lambda sid: sid.endswith(":$contact"),
        )
        assert [s.session_id for s in sessions] == ["!roomA:example.com:$t1"]
        assert [r.run_id for r in sessions[0].runs] == ["keep_1"]
        assert sessions[0].runs[0].tools[0].tool_name == "run_shell_command"
        assert len(sessions[0].runs[0].messages) == 2
        assert load_removed_run_ids(db_path, "assistant") == frozenset({"removed_run"})
        after = db_path.stat()
        assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)
    finally:
        ro_dir.chmod(0o755)
        db_path.chmod(0o644)


@pytest.mark.asyncio
async def test_evidence_card_bounds_redaction_and_tool_free_llm_judge() -> None:
    """EvidenceCard enforces size/redaction bounds; Stage 2 judge is tool-free, filters target_skill, and records usage."""
    assert "trace_audit" in USAGE_KINDS
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
    card = build_evidence_card(detect_session_incidents(sess, now=NOW)[0], sess)

    assert len(card.rendered) <= 4_000
    assert len(card.failure_sequence) == 6
    assert all(len(e.truncated_error) <= 240 for e in card.failure_sequence)
    assert [e.recovered for e in card.failure_sequence] == [True, False, False, False, False, False]
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
    assert (
        AuditFinding(**{**expected.model_dump(), "target_skill": "  python-testing  "}).target_skill == "python-testing"
    )
    for bad_skill in ("../escape", "Bad Upper", "skill\nignore rules", "a" * 65, "-leading-dash"):
        assert AuditFinding(**{**expected.model_dump(), "target_skill": bad_skill}).target_skill is None

    # audit_evidence_card drops valid slugs not present in card.loaded_skills.
    invented = expected.model_copy(update={"target_skill": "invented-slug"})
    assert (await audit_evidence_card(card, judge_fn=lambda _c: asyncio.sleep(0, result=invented))).target_skill is None

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

    assert (finding.verdict, finding.target_skill) == ("actionable", "python-testing")
    assert len(finding.root_cause) <= 240
    assert fake_api_key not in finding.root_cause
    assert (fallback.verdict, fallback.teachable) == ("no_issue", False)
    assert (tool_call_rejected.verdict, tool_call_rejected.teachable) == ("no_issue", False)
    assert len(recorded) == 3
    assert all(req.tool_choice == "none" and req.tools == () and req.output_schema is None for req in captured_requests)

    gemini = GeminiProvider(
        identity=ModelIdentity(provider="google", id="gemini-2.5-flash"),
        capabilities=Capabilities(structured_output="native"),
        settings=GeminiSettings(api_key="test-key", search=True),
    )
    gen_cfg = gemini._request_config(captured_requests[0], system_instruction="sys")
    assert getattr(gen_cfg, "tools", None) is None
    assert getattr(gen_cfg, "response_mime_type", None) == "application/json"


@pytest.mark.asyncio
async def test_pipeline_clustering_room_isolation_cooldown_pruning_and_content_free_state(tmp_path: Path) -> None:
    """Pipeline clusters per room, persists cooldown before learner call, prunes removed runs, and stays content-free."""
    db_path, state_path = tmp_path / "assistant.db", tmp_path / "trace_audit.json"
    private_phrase = "UNIQUE_PRIVATE_USER_PHRASE_XYZ"
    private_cmd = "./client-orchid-release --prod PRIVATE_ARG_999"
    s_a1, s_a2, s_b1 = "!roomA:example.com:$z_first", "!roomA:example.com:$a_second", "!roomB:example.com:$t1"

    _seed_v8_db(
        db_path,
        "assistant",
        sessions=[{"session_id": s_a1}, {"session_id": s_b1}],
        runs=[
            _db_run("a1_r1", s_a1, 1, NOW - 4000),
            _db_run("a1_r2", s_a1, 2, NOW - 3800),
            _db_run("b1_r1", s_b1, 1, NOW - 3600),
        ],
        tools=[
            (rid, idx, _fail_row(f"{rid}_{idx}", command=private_cmd))
            for rid in ("a1_r1", "a1_r2", "b1_r1")
            for idx in (0, 1)
        ],
        messages=[
            ("a1_r1", 0, {"role": "user", "content": private_phrase}),
            ("b1_r1", 0, {"role": "user", "content": private_phrase}),
        ],
    )

    invented_slugs = iter(("invented-alpha", "invented-beta", "invented-gamma", "invented-delta"))

    async def fake_judge(_card: EvidenceCard) -> AuditFinding:
        return _finding(target_skill=next(invented_slugs, None))

    nominated: list[FailureCluster] = []

    async def fake_nominate(cluster: FailureCluster) -> str:
        nominated.append(cluster)
        return "saved"

    settings = TraceAuditSettings(min_cluster_sessions=2, cooldown_days=14)

    async def run_pass(
        now: float,
        *,
        allowed_room_id: str | None = None,
        pass_settings: TraceAuditSettings | None = None,
        nominate_fn: NominateClusterFn | None = fake_nominate,
    ) -> AuditPassResult:
        return await run_audit_pass(
            db_path=db_path,
            storage_name="assistant",
            state_path=state_path,
            scope_key="agent:assistant",
            judge_fn=fake_judge,
            now=now,
            allowed_room_id=allowed_room_id,
            settings=pass_settings or settings,
            nominate_fn=nominate_fn,
        )

    res1 = await run_pass(NOW)
    assert (res1.incidents_audited, res1.promoted_clusters, nominated) == (3, (), [])

    # Add 2nd distinct session in Room A (alphabetically earlier than $z_first, newer timestamp) -> promotes!
    _seed_v8_db(
        db_path,
        "assistant",
        sessions=[{"session_id": s_a2, "created_at": NOW - 2000, "updated_at": NOW - 1500}],
        runs=[{"run_id": "a2_r1", "session_id": s_a2, "seq": 1, "created_at": NOW - 2000, "updated_at": NOW - 1500}],
        tools=[("a2_r1", idx, _fail_row(f"a2_{idx}", command=private_cmd)) for idx in (0, 1)],
    )
    res2 = await run_pass(NOW + 100)
    assert len(res2.promoted_clusters) == 1
    cluster = res2.promoted_clusters[0]
    assert (cluster.distinct_sessions, cluster.operation, cluster.target_skill, cluster.latest_session_id) == (
        2,
        "",
        None,
        s_a2,
    )
    assert len(nominated) == 1

    # Switching allowed_room_id to Room B never surfaces or promotes persisted Room A clusters even with cooldown=0.
    res_other_room = await run_pass(
        NOW + 120,
        allowed_room_id="!roomB:example.com",
        pass_settings=TraceAuditSettings(min_cluster_sessions=2, cooldown_days=0),
    )
    assert (res_other_room.actionable_clusters, res_other_room.promoted_clusters, len(nominated)) == ((), (), 1)

    # Cooldown suppresses re-promotion inside cooldown_days.
    assert (await run_pass(NOW + 5 * DAY)).promoted_clusters == ()

    # Cooldown is persisted BEFORE invoking learner, so a crash does not cause blind retry loops.
    async def crashing_nominate(_cluster: FailureCluster) -> None:
        msg = "simulated learner crash"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="simulated learner crash"):
        await run_pass(NOW + 15 * DAY, nominate_fn=crashing_nominate)
    assert (await run_pass(NOW + 15 * DAY + 3600)).promoted_clusters == ()
    assert len(nominated) == 1

    # Content-free state check: no user text, private shell command, or invented skill slug persisted.
    raw_state = state_path.read_text(encoding="utf-8")
    for secret in (private_phrase, "client-orchid-release", "PRIVATE_ARG_999", "invented-alpha"):
        assert secret not in raw_state
    assert set(json.loads(raw_state)) >= {"session_cursors", "findings", "cooldowns", "metrics"}

    # Tombstoning a previously audited run in _log_removed prunes its finding from state on the next pass.
    _seed_v8_db(db_path, "assistant", removed=[("a2_r1", s_a2)])
    res_pruned = await run_pass(NOW + 15 * DAY + 7200, nominate_fn=None)
    assert res_pruned.actionable_clusters == ()
    assert "a2_r1" not in {f["run_id"] for f in json.loads(state_path.read_text(encoding="utf-8"))["findings"]}


@pytest.mark.asyncio
async def test_pipeline_window_budget_cursor_deferral_and_per_card_error_isolation(tmp_path: Path) -> None:
    """Pipeline enforces default window_days, defers overflow cursors on budget cap, and isolates per-card errors."""
    db_path, state_path = tmp_path / "resilient.db", tmp_path / "state.json"
    s_old, s1, s2, s3 = (f"!roomA:example.com:${k}" for k in ("old", "s1", "s2", "s3"))
    _seed_v8_db(
        db_path,
        "agent",
        sessions=[
            {"session_id": sid, "created_at": ts, "updated_at": ts}
            for sid, ts in ((s_old, NOW - 45 * DAY), (s1, NOW - 4000), (s2, NOW - 3000), (s3, NOW - 2000))
        ],
        runs=[
            _db_run("r_expired", s_old, 1, NOW - 45 * DAY, status="ERROR", error="ancient"),
            _db_run("r_ok_1", s1, 1, NOW - 4000, status="ERROR", error="fail"),
            _db_run("r_bad_pre", s2, 1, NOW - 3500),
            _db_run("r_bad", s2, 2, NOW - 3000, status="ERROR", error="fail"),
            _db_run("r_ok_2", s3, 1, NOW - 2000, status="ERROR", error="fail"),
        ],
    )

    calls: list[str] = []
    fail_once = {"r_bad"}

    async def flaky_judge(card: EvidenceCard) -> AuditFinding:
        calls.append(card.run_id)
        if card.run_id in fail_once:
            fail_once.remove(card.run_id)
            msg = "transient provider 502"
            raise RuntimeError(msg)
        return _finding(
            category="verification_gap",
            remedy="verify_outcome",
            root_cause="Missing check.",
            proposed_rule="Check output.",
        )

    # Pass 1: cap=2 audits r_ok_1 (succeeds) and r_bad (raises), defers r_ok_2 (overflow) and r_bad (error); excludes r_expired.
    p1 = await run_audit_pass(
        db_path=db_path,
        storage_name="agent",
        state_path=state_path,
        scope_key="agent:agent",
        judge_fn=flaky_judge,
        now=NOW,
        settings=TraceAuditSettings(max_audits_per_run=2, min_cluster_sessions=2, window_days=30, audit_concurrency=2),
    )
    assert (p1.incidents_audited, {f.run_id for f in p1.findings}) == (1, {"r_ok_1"})
    assert "r_expired" not in calls
    saved1 = json.loads(state_path.read_text(encoding="utf-8"))
    assert (saved1["session_cursors"][s1], saved1["session_cursors"][s2], saved1["session_cursors"][s3]) == (1, 1, 0)

    # Pass 2: retries r_bad and audits overflow r_ok_2, completing the cluster and advancing cursors.
    calls.clear()
    p2 = await run_audit_pass(
        db_path=db_path,
        storage_name="agent",
        state_path=state_path,
        scope_key="agent:agent",
        judge_fn=flaky_judge,
        now=NOW + 60,
        settings=TraceAuditSettings(max_audits_per_run=5, min_cluster_sessions=2, window_days=30),
    )
    assert (calls, p2.incidents_audited, len(p2.actionable_clusters)) == (["r_bad", "r_ok_2"], 2, 1)
    saved2 = json.loads(state_path.read_text(encoding="utf-8"))
    assert (saved2["session_cursors"][s2], saved2["session_cursors"][s3]) == (2, 1)


def test_evidence_card_shell_recovery_requires_matching_command() -> None:
    """Shell failures only mark [recovered] when the same command succeeds, not on unrelated shell commands."""
    unrelated_run = _run(
        "r_unrelated",
        tools=(
            _tool("run_shell_command", ord_idx=0, args={"command": "pytest tests/"}, result="Exit code 1\nfailed"),
            _tool(
                "tools.run_shell_command",
                ord_idx=1,
                args={"command": "pytest tests/"},
                result="Exit code 1\nfailed",
            ),
            _tool("run_shell_command", ord_idx=2, args={"command": "pwd"}, result="/workspace"),
            _tool("tools.run_shell_command", ord_idx=3, args={"command": "pwd"}, result="/workspace"),
        ),
    )
    unrelated_sess = _session((unrelated_run,))
    unrelated_card = build_evidence_card(detect_session_incidents(unrelated_sess, now=NOW)[0], unrelated_sess)
    assert [e.recovered for e in unrelated_card.failure_sequence] == [False, False]
    assert "[recovered]" not in unrelated_card.rendered

    retry_run = _run(
        "r_retry",
        tools=(
            _tool("bash", ord_idx=0, args={"command": "pytest tests/"}, result="Exit code 1\nfailed"),
            _tool("bash", ord_idx=1, args={"command": "ruff check src/"}, result="Exit code 1\nlint error"),
            _tool("bash", ord_idx=2, args={"command": "pytest tests/"}, result="1 passed"),
        ),
    )
    retry_sess = _session((retry_run,))
    retry_card = build_evidence_card(detect_session_incidents(retry_sess, now=NOW)[0], retry_sess)
    assert [e.recovered for e in retry_card.failure_sequence] == [True, False]
    failure_lines = [line for line in retry_card.rendered.splitlines() if line.strip().startswith("#")]
    assert len(failure_lines) == 2
    assert "[recovered]" in failure_lines[0]
    assert "[recovered]" not in failure_lines[1]

    # Non-shell tools still match recovery by (tool_name, operation) even when args change.
    non_shell_run = _run(
        "r_non_shell",
        tools=(
            _tool(
                "matrix_room",
                ord_idx=0,
                args={"action": "create", "name": "bad"},
                result='{"errcode": "M_UNKNOWN"}',
            ),
            _tool(
                "matrix_room",
                ord_idx=1,
                args={"action": "create", "name": "bad2"},
                result='{"errcode": "M_UNKNOWN"}',
            ),
            _tool("matrix_room", ord_idx=2, args={"action": "create", "name": "good"}, result='{"room_id": "!ok:x"}'),
        ),
    )
    non_shell_sess = _session((non_shell_run,))
    non_shell_card = build_evidence_card(detect_session_incidents(non_shell_sess, now=NOW)[0], non_shell_sess)
    assert [e.recovered for e in non_shell_card.failure_sequence] == [True, True]


async def _run_pass_with_optional_cancel(
    *,
    db_path: Path,
    state_path: Path,
    now: float,
    settings: TraceAuditSettings,
    cancel_on_call: int | None,
    completed_runs: list[str],
) -> tuple[list[str], AuditPassResult | None]:
    started: list[str] = []
    cancel_ready, never_set = asyncio.Event(), asyncio.Event()

    async def _judge(card: EvidenceCard) -> AuditFinding:
        started.append(card.run_id)
        if cancel_on_call is not None and len(started) == cancel_on_call:
            cancel_ready.set()
            await never_set.wait()
        completed_runs.append(card.run_id)
        return _finding(
            category="verification_gap",
            remedy="verify_outcome",
            root_cause="Missing check.",
            proposed_rule="Check output.",
        )

    task = asyncio.create_task(
        run_audit_pass(
            db_path=db_path,
            storage_name="assistant",
            state_path=state_path,
            scope_key="agent:assistant",
            judge_fn=_judge,
            now=now,
            settings=settings,
        ),
    )
    if cancel_on_call is None:
        return started, await task
    await cancel_ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return started, None


@pytest.mark.asyncio
async def test_pipeline_cancellation_preserves_completed_findings_and_resumes(tmp_path: Path) -> None:
    """Cancelling Stage 2 mid-batch persists finished findings and advances cursors for completed cards."""
    db_path, state_path = tmp_path / "cancel.db", tmp_path / "state.json"
    s1, s2 = "!roomA:example.com:$s1", "!roomA:example.com:$s2"
    _seed_v8_db(
        db_path,
        "assistant",
        sessions=[
            {"session_id": s1, "created_at": NOW - 4000, "updated_at": NOW - 3000},
            {"session_id": s2, "created_at": NOW - 2500, "updated_at": NOW - 2000},
        ],
        runs=[
            _db_run("r1", s1, 1, NOW - 4000, status="ERROR", error="fail 1"),
            _db_run("r2", s1, 2, NOW - 3500, status="ERROR", error="fail 2"),
            _db_run("r3", s2, 1, NOW - 2500, status="ERROR", error="fail 3"),
        ],
    )
    settings = TraceAuditSettings(max_audits_per_run=5, min_cluster_sessions=2, audit_concurrency=1)
    first_completed: list[str] = []
    pass1_calls, _ = await _run_pass_with_optional_cancel(
        db_path=db_path,
        state_path=state_path,
        now=NOW,
        settings=settings,
        cancel_on_call=2,
        completed_runs=first_completed,
    )
    assert pass1_calls == ["r1", "r2"]
    saved_mid = json.loads(state_path.read_text(encoding="utf-8"))
    assert [f["run_id"] for f in saved_mid["findings"]] == ["r1"]
    assert saved_mid["session_cursors"] == {s1: 1, s2: 0}

    pass2_calls, res2 = await _run_pass_with_optional_cancel(
        db_path=db_path,
        state_path=state_path,
        now=NOW + 60,
        settings=settings,
        cancel_on_call=None,
        completed_runs=first_completed,
    )
    assert res2 is not None
    assert pass2_calls == ["r2", "r3"]
    assert (res2.incidents_audited, {f.run_id for f in res2.findings}) == (2, {"r2", "r3"})
    assert len(res2.actionable_clusters) == 1
    assert (res2.actionable_clusters[0].distinct_sessions, res2.actionable_clusters[0].occurrence_count) == (2, 3)
    saved_after = json.loads(state_path.read_text(encoding="utf-8"))
    assert {f["run_id"] for f in saved_after["findings"]} == {"r1", "r2", "r3"}
    assert saved_after["session_cursors"] == {s1: 2, s2: 1}

    # Cursor advancement prevents re-auditing completed runs even after max_stored_findings evicts them.
    evict_db, evict_state = tmp_path / "cancel_evict.db", tmp_path / "cancel_evict_state.json"
    evict_sids = [f"!roomA:example.com:$ev{i}" for i in range(1, 6)]
    _seed_v8_db(
        evict_db,
        "assistant",
        sessions=[
            {"session_id": sid, "created_at": NOW - 5000 + i * 100, "updated_at": NOW - 4500 + i * 100}
            for i, sid in enumerate(evict_sids, start=1)
        ],
        runs=[
            _db_run(f"e{i}", sid, 1, NOW - 5000 + i * 100, status="ERROR", error=f"evict fail {i}")
            for i, sid in enumerate(evict_sids, start=1)
        ],
    )
    evict_settings = TraceAuditSettings(
        max_audits_per_run=5,
        min_cluster_sessions=2,
        audit_concurrency=1,
        max_stored_findings=2,
    )
    completed_runs: list[str] = []
    started_per_tick: list[list[str]] = []
    for tick, cancel_on in enumerate((3, 3, None)):
        started, res = await _run_pass_with_optional_cancel(
            db_path=evict_db,
            state_path=evict_state,
            now=NOW + tick * 60,
            settings=evict_settings,
            cancel_on_call=cancel_on,
            completed_runs=completed_runs,
        )
        started_per_tick.append(started)
        if cancel_on is None:
            assert res is not None
            assert res.incidents_audited == 1

    assert started_per_tick == [["e1", "e2", "e3"], ["e3", "e4", "e5"], ["e5"]]
    assert completed_runs == ["e1", "e2", "e3", "e4", "e5"]
    evict_saved = json.loads(evict_state.read_text(encoding="utf-8"))
    assert len(evict_saved["findings"]) == 2
    assert evict_saved["session_cursors"] == dict.fromkeys(evict_sids, 1)
