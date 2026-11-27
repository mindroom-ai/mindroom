"""End-to-end tests for the full-surface MindRoom tool-calling eval suite."""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

import pytest

from evals.tool_calling.cases import CASES, CASES_BY_ID, CallTrace, EvalCase, _v_calc
from evals.tool_calling.harness import CaseResult, _collect_traces, evaluate_case, has_shell_argv_syntax
from evals.tool_calling.run import resolve_provider, run_cli
from mindroom.llm.messages import Message, model_provenance
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
        prov = model_provenance(self.identity.id, self.identity.provider)
        if isinstance(step, str):
            msg = Message(role="assistant", content=step, metrics=metrics, provider_data=prov)
            yield TurnCompleted(message=msg, stop_reason="end")
        else:
            msg = Message(role="assistant", content="", tool_calls=step, metrics=metrics, provider_data=prov)
            yield TurnCompleted(message=msg, stop_reason="tool_use")


async def _run(case_id: str, *turns: _Step, max_turns: int = 6) -> CaseResult:
    return await evaluate_case(CASES_BY_ID[case_id], _ScriptedProvider([*turns, "Done."]), max_turns=max_turns)


_SCRIPTS: dict[str, list[_Step]] = {
    "canvas_dashboard": [
        [_call("tool_search", query="show_canvas")],
        [_ct("show_canvas", title="Deploy", html="<div>staging=green prod=amber</div>")],
    ],
    "workflow_runs_check": [[_call("tool_search", query="workflow runs")], [_ct("list_workflow_runs")]],
    "thread_tags_audit": [
        [_call("tool_search", query="thread tags")],
        [_ct("list_thread_tags"), _ct("tag_thread", tag="triage")],
    ],
    "scheduler_reminder": [
        [_call("tool_search", query="scheduler")],
        [_ct("scheduler", action="create", message="Check staging", new_thread=False, delay_seconds=7200)],
    ],
    "memory_lookup_and_save": [
        [_call("tool_search", query="memory")],
        [
            _ct("memory", action="search", query="retry backoff"),
            _ct("memory", action="add", content="Prod uses exponential backoff starting at 2s."),
        ],
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
    "view_image_and_notify": [
        [_call("view_file", path="assets/sample.png"), _call("matrix_message", action="send", message="Inspected.")],
    ],
}


@pytest.mark.asyncio
async def test_portable_surface_and_provider_resolution(tmp_path: Path) -> None:
    """Real AgentBuild strips deferred tools, resolves provider specs, and disables configured fallbacks."""
    provider = _ScriptedProvider(
        [*_SCRIPTS["workflow_runs_check"], "Done."],
        capabilities=Capabilities(native_tool_search=True),
    )
    assert (await evaluate_case(CASES_BY_ID["workflow_runs_check"], provider)).passed
    offered = {tool.name for tool in provider.requests[0].tools}
    assert {"run_shell_command", "read_file", "edit_file", "grep", "attachment"} <= offered
    assert {"view_file", "matrix_message", "skill", "tool_search", "call_tool"} <= offered
    deferred = {"show_canvas", "list_workflow_runs", "list_thread_tags", "scheduler", "memory", "browser_control"}
    assert offered.isdisjoint(deferred)
    sys_text = "\n".join(m.get_content_string() for m in provider.requests[0].messages if m.role == "system")
    assert "deploy-check" in sys_text
    assert "tool_search" in sys_text

    url = "http://llama.local/v1"
    loc_p = resolve_provider("local:inkling-2:mini-vllm", config_path=None, base_url=url)
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
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-test-key\n", encoding="utf-8")
    or_p = resolve_provider("openrouter:google/gemma-4-31b-it", config_path=cfg_file, base_url=url)
    assert or_p.capabilities.native_tool_search is False
    cfg_p = resolve_provider("primary", config_path=cfg_file, base_url=url)
    assert (cfg_p.capabilities.native_tool_search, cfg_p.identity.id) == (False, "inkling-2:mini-vllm")
    cur: object = cfg_p
    while isinstance(cur, WrappedProvider):
        fn = cur.stage.func if isinstance(cur.stage, partial) else cur.stage
        assert fn is not fallback_stream
        cur = cur.inner


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[case.case_id for case in CASES])
async def test_all_12_cases_pass_first_try(case: EvalCase) -> None:
    """Every one of the 12 eval cases passes on first try through the real MindRoom loop."""
    assert len(CASES) == 12
    result = await _run(case.case_id, *_SCRIPTS[case.case_id])
    assert result.error is None
    assert result.passed, f"{case.case_id} failed: {result.calls}"
    assert result.first_try_passed
    assert result.mis_calls == 0
    assert result.failure_counts == {}
    assert result.input_tokens > 0
    assert result.output_tokens > 0


@pytest.mark.asyncio
async def test_alternative_paths_and_stripped_env_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Workspace-file canvas, shell sed config edit, and stripped env / isolated HOME in shell and _v_calc."""
    w = [_call("write_file", path="widget.html", content="<div>staging=green</div>")]
    assert (
        await _run("canvas_dashboard", w, [_ct("show_canvas", title="Deploy", path="widget.html")])
    ).first_try_passed
    g = [_call("grep", pattern="LEGACY_PORT")]
    s = [_call("run_shell_command", args="sed -i 's/8080/9090/' src/config.py")]
    assert (await _run("grep_and_edit_config", g, s)).first_try_passed

    monkeypatch.setenv("EVAL_SECRET_SHOULD_NOT_LEAK", "super-secret")
    (tmp_path / "check_calc.py").write_text(
        "import os\nassert 'EVAL_SECRET_SHOULD_NOT_LEAK' not in os.environ\n"
        f"assert os.environ['HOME'] == {str(tmp_path)!r}\n",
        encoding="utf-8",
    )
    tr = CallTrace(1, "run_shell_command", {}, "run_shell_command", {"args": "python3 check_calc.py"}, False, None, "")
    assert _v_calc(tmp_path, (tr,))

    cmd = 'test -z "$EVAL_SECRET_SHOULD_NOT_LEAK" && test "$HOME" = "$PWD"'
    shell_res = await _run(
        "shell_pipeline_logs",
        [_call("run_shell_command", args=cmd)],
        [_call("run_shell_command", args=_LOG_CMD)],
    )
    assert shell_res.first_try_passed


@pytest.mark.asyncio
async def test_stateful_stubs_and_attachment_validation() -> None:
    """Tags, memories, attachments, and matrix_room stubs validate semantics and persist state per case."""
    tags_res = await _run(
        "thread_tags_audit",
        [_ct("tag_thread", tag="triage"), _ct("list_thread_tags")],
        [_ct("untag_thread", tag="triage"), _ct("list_thread_tags"), _ct("tag_thread", tag="triage")],
    )
    assert tags_res.first_try_passed
    assert "triage" in json.loads(tags_res.calls[1].result_preview)["tags"]
    assert "triage" not in json.loads(tags_res.calls[3].result_preview)["tags"]

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
        [_call("attachment", action="get", attachment_id="att_eval_1"), _call("matrix_room", action="room-info")],
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
        [
            _call("attachment", action="inline_image", path="assets/sample.png"),
            _call("view_file", path="assets/sample.png"),
            _call("matrix_message", action="send", message="Inspected."),
        ],
    )
    assert view_att.passed
    assert not view_att.first_try_passed
    assert view_att.calls[0].failure_kind == "missing_required_param"
    assert view_att.failure_counts == {"invalid_param_value": 1, "missing_required_param": 2}


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
    "hallucinated_action_deferred": (
        "workflow_runs_check",
        [[_ct("list_workflow_runs", action="list")], [_ct("list_workflow_runs")]],
        True,
        {"hallucinated_action": 1},
    ),
    "hallucinated_action_view_file": (
        "view_image_and_notify",
        [
            [_call("view_file", action="view", path="assets/sample.png")],
            [_call("view_file", path="assets/sample.png"), _call("matrix_message", action="send", message="Done.")],
        ],
        True,
        {"hallucinated_action": 1},
    ),
    "scheduler_direct_enum_and_missing": (
        "scheduler_reminder",
        [
            [_call("scheduler", action="list"), _ct("scheduler", action="bogus")],
            [_ct("scheduler", action="create", message="Check")],
            [_ct("scheduler", action="create", message="Check", new_thread=False, delay_seconds=7200)],
        ],
        True,
        {"unknown_tool": 1, "invalid_param_value": 1, "missing_required_param": 1},
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
        [
            [_call("read_file", path="missing.txt"), _call("view_file", path="missing.png")],
            [_call("view_file", path="assets/sample.png"), _call("matrix_message", action="send", message="Done.")],
        ],
        True,
        {"tool_error": 2},
    ),
    "scheduler_rejects_new_thread_true": (
        "scheduler_reminder",
        [[_ct("scheduler", action="create", message="Check", new_thread=True, delay_seconds=7200)]],
        False,
        {},
    ),
    "scheduler_rejects_short_delay": (
        "scheduler_reminder",
        [[_ct("scheduler", action="create", message="Check", new_thread=False, delay_seconds=1)]],
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
            prov = model_provenance("oom", "openai")
            msg = Message(role="assistant", content="", reasoning_content="x", metrics=tok, provider_data=prov)
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
    assert "| Model | First-Try | Recovered | Mis-Calls | Failure Breakdown | Tokens | Elapsed |" in stdout
    assert "| clean | 1/1 | 1/1 | 0 | none |" in stdout
    assert "| noisy | 0/1 | 1/1 (+1) | 1 | nested_call_tool_name=1 |" in stdout
    assert "| oom | 0/1 | 0/1 | 0 | none |" in stdout
    saved = json.loads(out_file.read_text(encoding="utf-8"))
    assert saved == report
    assert [m["model"] for m in saved["models"]] == ["clean", "noisy", "oom"]
    assert "output token limit" in (saved["models"][2]["cases"][0]["error"] or "").lower()

    with pytest.raises(SystemExit):
        await run_cli(["--model", "clean", "--cases", "not_a_real_case"], provider_factory=factory)
