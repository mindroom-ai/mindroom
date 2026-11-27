"""End-to-end tests for the full-surface MindRoom tool-calling eval suite."""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from evals.tool_calling.cases import CASES, CASES_BY_ID, CallTrace, EvalCase, _v_calc
from evals.tool_calling.harness import CaseResult, _collect_traces, evaluate_case, has_shell_argv_syntax
from evals.tool_calling.run import resolve_provider, run_cli
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_shared_credentials_manager
from mindroom.llm.messages import Message, MessageMeta, ModelProvenance
from mindroom.llm.provider import (
    Capabilities,
    DeferredTools,
    ModelIdentity,
    ModelRequest,
    StreamEvent,
    TokenCounter,
    TurnCompleted,
)
from mindroom.llm.runs import RunRecord, ToolExecution
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.providers.openai.chat import ChatAdapter
from mindroom.providers.pipeline.fallback import fallback_stream
from mindroom.providers.pipeline.wrapped import WrappedProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

_Step = list[dict[str, Any]] | str | TurnCompleted
_SEQ = itertools.count(1)
_LOG_CMD = "grep -c ERROR logs/service.log && grep ERROR logs/service.log | tail -n 2 > logs/recent_errors.txt"
_PIPE_ARGV = ["grep", "ERROR", "logs/service.log", "|", "tail", "-n", "2", ">", "logs/recent_errors.txt"]


def _tc(name: str, args: dict[str, object] | str) -> dict[str, Any]:
    raw = args if isinstance(args, str) else json.dumps(args)
    return {"id": f"c{next(_SEQ)}", "type": "function", "function": {"name": name, "arguments": raw}}


def _call(name: str, /, **kw: object) -> dict[str, Any]:
    return _tc(name, kw)


def _ct(target: str, /, **kw: object) -> dict[str, Any]:
    return _tc("call_tool", {"name": target, "arguments": kw})


def _sched(**kw: object) -> dict[str, Any]:
    return _ct("scheduler", action="create", message="Check", **kw)


@dataclass
class _ScriptedProvider:
    """Deterministic scripted ``ModelProvider`` for end-to-end agent-loop tests."""

    turns: Sequence[_Step]
    model_id: str = "scripted-local"
    provider_name: str = "openai"
    capabilities: Capabilities = field(default_factory=Capabilities)
    requests: list[ModelRequest] = field(default_factory=list)
    _idx: int = 0

    @property
    def identity(self) -> ModelIdentity:
        return ModelIdentity(id=self.model_id, provider=self.provider_name)

    @property
    def native_history_route(self) -> None:
        return None

    @property
    def token_counter(self) -> TokenCounter:
        return TokenCounter(estimate=lambda text: max(1, len(text) // 4), pdf_page_tokens=None)

    @property
    def model_params(self) -> dict[str, Any]:
        return {}

    def request_tokens(self, _request: ModelRequest, *, start: int = 0) -> int:
        del start
        return 100

    def context_key(self, _request: ModelRequest) -> str:
        return "scripted"

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        step = self.turns[self._idx] if self._idx < len(self.turns) else "Done."
        self._idx += 1
        if isinstance(step, TurnCompleted):
            yield step
            return
        metrics = MessageMetrics(tokens=TokenCounts(input_tokens=100, output_tokens=25, total_tokens=125))
        meta = MessageMeta(model=ModelProvenance(self.identity.id, self.identity.provider))
        if isinstance(step, str):
            msg = Message(role="assistant", content=step, metrics=metrics, meta=meta)
            yield TurnCompleted(message=msg, stop_reason="end")
        else:
            msg = Message(role="assistant", content="", tool_calls=step, metrics=metrics, meta=meta)
            yield TurnCompleted(message=msg, stop_reason="tool_use")


async def _run(case_id: str, *turns: _Step, max_turns: int = 6) -> CaseResult:
    return await evaluate_case(CASES_BY_ID[case_id], _ScriptedProvider([*turns, "Done."]), max_turns=max_turns)


def _unwrap(provider: object) -> tuple[object, list[object]]:
    stages: list[object] = []
    while isinstance(provider, WrappedProvider):
        stages.append(provider.stage.func if isinstance(provider.stage, partial) else provider.stage)
        provider = provider.inner
    return provider, stages


_VIEW = _call("view_file", path="assets/sample.png")
_SEND = _call("matrix_message", action="send", message="Inspected.")
_SCRIPTS: dict[str, list[_Step]] = {
    "canvas_dashboard": [
        [_call("tool_search", query="show_canvas")],
        [_ct("show_canvas", title="Deploy", html="<div>staging=green prod=amber</div>", path="")],
    ],
    "workflow_runs_check": [[_call("tool_search", query="workflow runs")], [_ct("workflow_read", action="list")]],
    "thread_tags_audit": [
        [_call("tool_search", query="thread tags")],
        [_ct("thread_tags", action="list"), _ct("thread_tags", action="tag", tag="triage")],
    ],
    "scheduler_reminder": [[_call("tool_search", query="scheduler")], [_sched(new_thread=False, delay_seconds=7200)]],
    "memory_lookup_and_save": [
        [_call("tool_search", query="memory")],
        [_ct("memory", action="search", query="retry"), _ct("memory", action="add", content="Prod 2s backoff.")],
    ],
    "browser_inspect": [
        [_call("tool_search", query="browser")],
        [_ct("browser_control", action="open", targetUrl="https://example.org/status")],
    ],
    "shell_pipeline_logs": [[_call("run_shell_command", args=_LOG_CMD)]],
    "shell_and_edit_fix": [
        [_call("read_file", path="src/calc.py"), _call("read_file", path="check_calc.py")],
        [_call("edit_file", path="src/calc.py", edits=[{"old_text": "price + (", "new_text": "price - ("}])],
        [_call("run_shell_command", args="python3 check_calc.py")],
    ],
    "grep_and_edit_config": [
        [_call("grep", pattern="LEGACY_PORT")],
        [_call("edit_file", path="src/config.py", edits=[{"old_text": "8080", "new_text": "9090"}])],
    ],
    "skill_load_deploy": [[_call("skill", action="instructions", name="deploy-check")]],
    "attachment_list_and_register": [
        [_call("attachment", action="list"), _call("attachment", action="register", path="reports/summary.txt")],
    ],
    "view_image_and_notify": [[_VIEW, _SEND]],
}


@pytest.mark.asyncio
async def test_portable_surface_and_provider_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real AgentBuild strips deferred tools, resolves providers without default key leaks, and disables fallbacks."""
    provider = _ScriptedProvider(
        [*_SCRIPTS["workflow_runs_check"], "Done."],
        capabilities=Capabilities(native_tool_search=True),
    )
    assert (await evaluate_case(CASES_BY_ID["workflow_runs_check"], provider)).passed
    offered = {tool.name for tool in provider.requests[0].tools}
    assert {"run_shell_command", "read_file", "edit_file", "grep", "attachment"} <= offered
    assert {"view_file", "matrix_message", "skill", "tool_search", "call_tool"} <= offered
    deferred = {
        "show_canvas",
        "workflow_read",
        "workflow_write",
        "thread_tags",
        "scheduler",
        "memory",
        "browser_read",
        "browser_control",
    }
    assert offered.isdisjoint(deferred)
    sys_text = "\n".join(m.get_content_string() for m in provider.requests[0].messages if m.role == "system")
    assert "deploy-check" in sys_text
    assert "tool_search" in sys_text

    url = "http://llama.local/v1"
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(tmp_path / "storage"))
    monkeypatch.setenv("MINDROOM_SHARED_CREDENTIALS_PATH", str(tmp_path / "storage" / "credentials"))
    loc_p = resolve_provider("local:inkling-2:mini-vllm", config_path=None, base_url=url, tmp_dir=tmp_path / "local")
    assert loc_p.capabilities.native_tool_search is False
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "defaults:\n  tools: []\nagents:\n  eval_agent:\n    display_name: Eval\n    role: Eval\n"
        "memory:\n  backend: none\nmodels:\n  default:\n    provider: openai\n    id: backup-model\n"
        f"    extra_kwargs:\n      base_url: {url}\n      api_key: sk-no-key-required\n"
        "  primary:\n    provider: openai\n    id: inkling-2:mini-vllm\n    fallbacks:\n      - default\n"
        f"    extra_kwargs:\n      base_url: {url}\n      api_key: sk-no-key-required\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-test-key\n", encoding="utf-8")
    creds = get_runtime_shared_credentials_manager(resolve_runtime_paths(config_path=cfg_file))
    creds.save_credentials("model:default", {"api_key": "sk-wrong-default-key"})
    or_p = resolve_provider("openrouter:google/gemma-4-31b-it", config_path=cfg_file, base_url=url)
    assert or_p.capabilities.native_tool_search is False
    or_inner, _ = _unwrap(or_p)
    assert isinstance(or_inner, ChatAdapter)
    sdk_client = or_inner.clients.factory()
    try:
        assert sdk_client.api_key == "sk-or-test-key"
    finally:
        await sdk_client.close()

    cfg_p = resolve_provider("primary", config_path=cfg_file, base_url=url)
    assert (cfg_p.capabilities.native_tool_search, cfg_p.identity.id) == (False, "inkling-2:mini-vllm")
    assert fallback_stream not in _unwrap(cfg_p)[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[case.case_id for case in CASES])
async def test_all_12_cases_pass_first_try(case: EvalCase) -> None:
    """Every one of the 12 eval cases passes on first try through the real MindRoom loop."""
    assert len(CASES) == 12
    res = await _run(case.case_id, *_SCRIPTS[case.case_id])
    assert res.passed, f"{case.case_id} failed: {res.calls}"
    assert (res.error, res.first_try_passed, res.mis_calls, res.failure_counts) == (None, True, 0, {})
    assert res.input_tokens > 0
    assert res.output_tokens > 0


@pytest.mark.asyncio
async def test_alternative_paths_and_stripped_env_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Workspace-file canvas, shell sed edit, stripped env/proxy/HOME, shell JSON stdout, and tamper-proof _v_calc."""
    w = [_call("write_file", path="widget.html", content="<div>staging=green</div>")]
    c = [_ct("show_canvas", title="Deploy", path="widget.html")]
    assert (await _run("canvas_dashboard", w, c)).first_try_passed
    g = [_call("grep", pattern="LEGACY_PORT")]
    s = [_call("run_shell_command", args="sed -i.bak 's/8080/9090/' src/config.py")]
    assert (await _run("grep_and_edit_config", g, s)).first_try_passed

    monkeypatch.setenv("EVAL_SECRET_SHOULD_NOT_LEAK", "super-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@proxy:8080")
    (src := tmp_path / "src").mkdir()
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "calc.py").write_text("def apply_discount(p, d): return p + p * d / 100.0\n", encoding="utf-8")
    (tmp_path / "check_calc.py").write_text('print("OK")\n', encoding="utf-8")
    tr = CallTrace(1, "run_shell_command", {}, "run_shell_command", {"args": "python3 check_calc.py"}, False, None, "")
    assert not _v_calc(tmp_path, (tr,))

    (src / "calc.py").write_text(
        "import os\ndef apply_discount(p, d):\n    assert 'EVAL_SECRET_SHOULD_NOT_LEAK' not in os.environ\n"
        f"    assert os.environ['HOME'] == {str(tmp_path)!r}\n    return p - (p * d / 100.0)\n",
        encoding="utf-8",
    )
    (tmp_path / "check_calc.py").write_text('raise AssertionError("must not run disk script")\n', encoding="utf-8")
    assert _v_calc(tmp_path, (tr,))

    env_cmd = 'test -z "$EVAL_SECRET_SHOULD_NOT_LEAK" && test -z "$HTTPS_PROXY" && test "$HOME" = "$PWD"'
    json_cmd = """printf '{"error": 4}\\n'"""
    awk_call = _call("run_shell_command", args=["awk", "/ERROR/ && NF", "logs/service.log"])
    s_t1 = [_call("run_shell_command", args=env_cmd), _call("run_shell_command", args=json_cmd), awk_call]
    assert (await _run("shell_pipeline_logs", s_t1, [_call("run_shell_command", args=_LOG_CMD)])).first_try_passed


@pytest.mark.asyncio
async def test_stateful_stubs_and_attachment_validation() -> None:
    """Tags, memories, attachments, and matrix_room stubs validate semantics and persist state per case."""
    tt = partial(_ct, "thread_tags")
    wfr, wfw = partial(_ct, "workflow_read"), partial(_ct, "workflow_write")
    tags_res = await _run(
        "thread_tags_audit",
        [tt(action="tag", tag="triage"), tt(action="list", tag=None)],
        [tt(action="untag", tag="triage"), tt(action="list"), tt(action="tag", tag="triage")],
    )
    assert tags_res.first_try_passed
    assert "triage" in json.loads(tags_res.calls[1].result_preview)["tags"]
    assert "triage" not in json.loads(tags_res.calls[3].result_preview)["tags"]

    wf_err = [wfr(action="get"), wfw(action="save", run_id="wf_01"), wfw(action="start"), tt(action="tag")]
    wf_bad = [wfr(action="list", run_id="wf-1"), tt(action="list", note="extra")]
    wf_res = await _run("workflow_runs_check", [*wf_err, *wf_bad], [wfr(action="list", run_id=None)])
    assert (wf_res.passed, wf_res.failure_counts) == (True, {"missing_required_param": 4, "unexpected_param": 2})

    mem_res = await _run(
        "memory_lookup_and_save",
        [_ct("memory", action="get", memory_id="mem_99")],
        [_ct("memory", action="search", query="retry"), _ct("memory", action="add", content="Prod 2s backoff.")],
        [_ct("memory", action="get", memory_id="mem_2")],
    )
    assert (mem_res.passed, mem_res.failure_counts) == (True, {"tool_error": 1})
    assert json.loads(mem_res.calls[3].result_preview)["memory"]["id"] == "mem_2"

    att_res = await _run(
        "attachment_list_and_register",
        [_call("attachment", action="list"), _call("attachment", action="get", attachment_id="att_missing")],
        [_call("attachment", action="register", path="reports/summary.txt")],
        [_call("attachment", action="get", attachment_id="att_eval_1"), _call("matrix_room", action="room_info")],
    )
    assert (att_res.passed, att_res.calls[1].is_error, att_res.failure_counts) == (True, True, {"tool_error": 1})
    assert json.loads(att_res.calls[3].result_preview)["attachment"]["attachment_id"] == "att_eval_1"
    assert json.loads(att_res.calls[4].result_preview)["name"] == "Eval Room"

    view_att = await _run(
        "view_image_and_notify",
        [_call("attachment", action="get", path="assets/sample.png", view=True)],
        [
            _call("attachment", action="inline_image"),
            _call("attachment", action="inline_image", attachment_id="att_1", path="assets/sample.png"),
        ],
        [_call("attachment", action="inline_image", path="assets/sample.png"), _VIEW, _SEND],
    )
    assert (view_att.passed, view_att.first_try_passed) == (True, False)
    assert view_att.calls[0].failure_kind == "missing_required_param"
    assert view_att.failure_counts == {"invalid_param_value": 1, "missing_required_param": 2}


_TWO_HOURS_LA = (datetime.now(ZoneInfo("America/Los_Angeles")) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
_FAILURE_SCENARIOS: dict[str, tuple[str, list[_Step], bool, dict[str, int]]] = {
    "nested_call_tool_and_unknown": (
        "canvas_dashboard",
        [
            [_tc("call_tool", {"arguments": {"name": "show_canvas", "title": "Deploy", "html": "<p>ok</p>"}})],
            [_tc("call_tool", {"name": "show_canvas", "arguments": ["not-an-object"]}), _ct("nonexistent_fn")],
            [_ct("show_canvas", title="Deploy", html="<p>ok</p>")],
        ],
        True,
        {"nested_call_tool_name": 2, "unknown_tool": 1},
    ),
    "shell_argv_syntax": (
        "shell_pipeline_logs",
        [[_call("run_shell_command", args=_PIPE_ARGV)], [_call("run_shell_command", args=_LOG_CMD)]],
        True,
        {"shell_argv_syntax": 1},
    ),
    "hallucinated_action_and_bad_output_path": (
        "canvas_dashboard",
        [
            [
                _ct("show_canvas", action="show", title="Deploy", html="<p>ok</p>"),
                _ct("workflow_read", action="list", mindroom_output_path=123),
            ],
            [_ct("show_canvas", title="Deploy", html="<p>ok</p>", path=None)],
        ],
        True,
        {"hallucinated_action": 1, "invalid_param_value": 1},
    ),
    "hallucinated_action_view_file": (
        "view_image_and_notify",
        [[_call("view_file", action="view", path="assets/sample.png")], [_VIEW, _SEND]],
        True,
        {"hallucinated_action": 1},
    ),
    "scheduler_direct_enum_missing_and_execute_at": (
        "scheduler_reminder",
        [
            [_call("scheduler", action="list"), _ct("scheduler", action="bogus"), _sched()],
            [
                _sched(new_thread=False, execute_at="in 2 hours"),
                _sched(new_thread=False, execute_at="2000-01-01T00:00:00Z"),
            ],
            [_sched(new_thread=False, execute_at=_TWO_HOURS_LA, timezone="America/Los_Angeles")],
        ],
        True,
        {"unknown_tool": 1, "invalid_param_value": 3, "missing_required_param": 1},
    ),
    "browser_unexpected_param": (
        "browser_inspect",
        [
            [_ct("browser_control", action="open", url="https://example.org/status")],
            [_ct("browser_control", action="open", targetUrl="https://example.org/status")],
        ],
        True,
        {"unexpected_param": 1},
    ),
    "silent_skill_error_fails_case": (
        "skill_load_deploy",
        [[_call("skill", action="instructions", name="deploy-check", path="SKILL.md")]],
        False,
        {"tool_error": 1},
    ),
    "silent_fs_and_view_errors": (
        "view_image_and_notify",
        [[_call("read_file", path="missing.txt"), _call("view_file", path="missing.png")], [_VIEW, _SEND]],
        True,
        {"tool_error": 2},
    ),
    "scheduler_rejects_new_thread": ("scheduler_reminder", [[_sched(new_thread=True, delay_seconds=7200)]], False, {}),
    "scheduler_rejects_short_delay": ("scheduler_reminder", [[_sched(new_thread=False, delay_seconds=1)]], False, {}),
    "view_rejects_new_thread_true": (
        "view_image_and_notify",
        [[_VIEW, _call("matrix_message", action="send", message="Done.", new_thread=True)]],
        False,
        {},
    ),
    "view_rejects_room_thread_id": (
        "view_image_and_notify",
        [[_VIEW, _call("matrix_message", action="send", message="Done.", thread_id="room")]],
        False,
        {},
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_id", "turns", "expected_passed", "expected_counts"),
    list(_FAILURE_SCENARIOS.values()),
    ids=list(_FAILURE_SCENARIOS),
)
async def test_failure_modes_and_verifier_contracts(
    case_id: str,
    turns: list[_Step],
    expected_passed: bool,
    expected_counts: dict[str, int],
) -> None:
    """All tracked failure modes, silent tool errors, and tightened verifier contracts behave accurately."""
    res = await _run(case_id, *turns)
    assert res.passed is expected_passed
    assert res.failure_counts == expected_counts


@pytest.mark.asyncio
async def test_shell_syntax_plural_validation_raw_preview_trace_matching_and_verify_error() -> None:
    """Shell argv detector, plural Pydantic errors, _raw decode preview, call_id matching, turn cap, verify error."""
    assert has_shell_argv_syntax({"args": ["grep", "ERROR", "logs/service.log", "|", "wc", "-l"]})
    assert has_shell_argv_syntax({"args": ["ls", "~/logs"]})
    assert not has_shell_argv_syntax({"args": ["bash", "-c", "grep ERROR logs/service.log | wc -l"]})
    assert not has_shell_argv_syntax({"args": ["bash", "-o", "pipefail", "-c", "grep ERROR logs/service.log | wc -l"]})
    assert not has_shell_argv_syntax({"args": ["awk", "/ERROR/ && NF", "logs/service.log"]})

    bad_raw = '{"name": "show_canvas", "html": "<div>unterminated'
    res = await _run(
        "shell_pipeline_logs",
        [_call("run_shell_command", args={"argv": ["ls"]}), _tc("call_tool", bad_raw)],
        [_call("run_shell_command", args=_LOG_CMD)],
    )
    assert res.passed
    assert "validation errors for" in res.calls[0].result_preview
    assert (res.calls[0].failure_kind, res.calls[1].failure_kind) == ("invalid_param_value", "invalid_param_value")
    assert res.calls[1].raw_args == {"_raw": bad_raw}

    t1 = ToolExecution(tool_call_id="b", tool_name="tool_search", tool_args={"query": "b"}, is_error=False, result="{}")
    t2 = ToolExecution(tool_call_id="", tool_name="tool_search", tool_args={"query": "a"}, is_error=False, result="{}")
    raw_hist = [("a", 1, "tool_search", {"query": "a"}), ("b", 2, "tool_search", {"query": "b"})]
    traces, _ = _collect_traces(RunRecord(run_id="r", session_id="s", tools=[t1, t2]), raw_hist, {}, DeferredTools())
    assert (traces[0].turn, traces[0].raw_args, traces[1].turn) == (2, {"query": "b"}, 1)

    capped = await _run("workflow_runs_check", *([_call("tool_search", query="wf")] for _ in range(5)), max_turns=2)
    assert (capped.passed, capped.turns) == (False, 2)

    def _boom(_ws: Path, _c: Sequence[CallTrace]) -> bool:
        msg = "verifier exploded"
        raise RuntimeError(msg)

    err_res = await evaluate_case(EvalCase("boom", "Say hi.", _boom), _ScriptedProvider(["hi"]))
    assert (err_res.passed, err_res.error) == (False, "RuntimeError: verifier exploded")


@pytest.mark.asyncio
async def test_run_cli_end_to_end_and_output_limit_resilience(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """run_cli prints Markdown, writes JSON, survives ModelOutputLimitError in one model, and rejects bad case IDs."""
    out_file = tmp_path / "results.json"

    def factory(model: str) -> _ScriptedProvider:
        if model == "clean":
            turns: list[_Step] = [[_ct("show_canvas", title="Deploy", html="<div>ok</div>")], "Done."]
        elif model == "oom":
            tok = MessageMetrics(tokens=TokenCounts(input_tokens=100, output_tokens=200, total_tokens=300))
            meta = MessageMeta(model=ModelProvenance("oom", "openai"))
            msg = Message(role="assistant", content="", reasoning_content="x", metrics=tok, meta=meta)
            turns = [TurnCompleted(message=msg, stop_reason="output_limit")]
        else:
            turns = [
                [_tc("call_tool", {"arguments": {"name": "show_canvas", "title": "Deploy", "html": "<div>ok</div>"}})],
                [_ct("show_canvas", title="Deploy", html="<div>ok</div>")],
                "Done.",
            ]
        return _ScriptedProvider(turns, model_id=model)

    report = await run_cli(
        ["--model", "clean,noisy,oom", "--cases", "canvas_dashboard", "--max-turns", "4", "--out", str(out_file)],
        provider_factory=factory,
    )
    stdout = capsys.readouterr().out
    assert "| Model | First-Try | Passed (+rec) | Mis-Calls | Failure Breakdown | Tokens | Elapsed |" in stdout
    assert "| clean | 1/1 | 1/1 | 0 | none |" in stdout
    assert "| noisy | 0/1 | 1/1 (+1) | 1 | nested_call_tool_name=1 |" in stdout
    assert "| oom | 0/1 | 0/1 | 0 | none |" in stdout
    saved = json.loads(out_file.read_text(encoding="utf-8"))
    assert saved == report
    assert [m["model"] for m in saved["models"]] == ["clean", "noisy", "oom"]
    assert "output token limit" in (saved["models"][2]["cases"][0]["error"] or "").lower()

    with pytest.raises(SystemExit):
        await run_cli(["--model", "clean", "--cases", "not_a_real_case"], provider_factory=factory)
