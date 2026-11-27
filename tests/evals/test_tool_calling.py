"""End-to-end tests for MindRoom's full-surface tool-calling eval suite."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from evals.tool_calling.cases import CASES, CASES_BY_ID, EvalCase
from evals.tool_calling.harness import _collect_traces, evaluate_case, has_shell_argv_syntax
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

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path


def _tc(call_id: str, name: str, arguments: dict[str, Any] | str) -> dict[str, Any]:
    raw_args = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": raw_args}}


def _call_tool(call_id: str, target: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return _tc(call_id, "call_tool", {"name": target, "arguments": arguments or {}})


@dataclass
class _ScriptedProvider:
    """Deterministic scripted ``ModelProvider`` for end-to-end agent-loop tests."""

    turns: Sequence[list[dict[str, Any]] | str]
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
        metrics = MessageMetrics(tokens=TokenCounts(input_tokens=100, output_tokens=25, total_tokens=125))
        prov = model_provenance(self.identity.id, self.identity.provider)
        if isinstance(step, str):
            yield TurnCompleted(
                message=Message(role="assistant", content=step, metrics=metrics, provider_data=prov),
                stop_reason="end",
            )
        else:
            yield TurnCompleted(
                message=Message(role="assistant", content="", tool_calls=step, metrics=metrics, provider_data=prov),
                stop_reason="tool_use",
            )


_SCRIPTS: dict[str, list[list[dict[str, Any]] | str]] = {
    "canvas_dashboard": [
        [_tc("c1", "tool_search", {"query": "show_canvas"})],
        [_call_tool("c2", "show_canvas", {"title": "Deploy", "html": "<div>staging=green prod=amber</div>"})],
        "Done.",
    ],
    "workflow_runs_check": [
        [_tc("c1", "tool_search", {"query": "workflow runs"})],
        [_call_tool("c2", "list_workflow_runs", {})],
        "Done.",
    ],
    "thread_tags_audit": [
        [_tc("c1", "tool_search", {"query": "thread tags"})],
        [_call_tool("c2", "list_thread_tags", {})],
        [_call_tool("c3", "tag_thread", {"tag": "triage"})],
        "Done.",
    ],
    "scheduler_reminder": [
        [_tc("c1", "tool_search", {"query": "scheduler"})],
        [
            _call_tool(
                "c2",
                "scheduler",
                {"action": "create", "message": "Check staging", "new_thread": False, "delay_seconds": 7200},
            ),
        ],
        "Done.",
    ],
    "memory_lookup_and_save": [
        [_tc("c1", "tool_search", {"query": "memory"})],
        [
            _call_tool("c2", "memory", {"action": "search", "query": "retry backoff"}),
            _call_tool("c3", "memory", {"action": "add", "content": "Prod uses exponential backoff starting at 2s."}),
        ],
        "Done.",
    ],
    "browser_inspect": [
        [_tc("c1", "tool_search", {"query": "browser"})],
        [_call_tool("c2", "browser_control", {"action": "open", "targetUrl": "https://example.org/status"})],
        "Done.",
    ],
    "shell_pipeline_logs": [
        [
            _tc(
                "c1",
                "run_shell_command",
                {
                    "args": (
                        "grep -c ERROR logs/service.log && "
                        "grep ERROR logs/service.log | tail -n 2 > logs/recent_errors.txt"
                    ),
                },
            ),
        ],
        "Done.",
    ],
    "shell_and_edit_fix": [
        [_tc("c1", "read_file", {"path": "src/calc.py"}), _tc("c2", "read_file", {"path": "check_calc.py"})],
        [
            _tc(
                "c3",
                "edit_file",
                {"path": "src/calc.py", "edits": [{"old_text": "price + (", "new_text": "price - ("}]},
            ),
        ],
        [_tc("c4", "run_shell_command", {"args": "python3 check_calc.py"})],
        "Done.",
    ],
    "grep_and_edit_config": [
        [_tc("c1", "grep", {"pattern": "LEGACY_PORT"})],
        [_tc("c2", "edit_file", {"path": "src/config.py", "edits": [{"old_text": "8080", "new_text": "9090"}]})],
        "Done.",
    ],
    "skill_load_deploy": [[_tc("c1", "skill", {"action": "instructions", "name": "deploy-check"})], "Done."],
    "attachment_list_and_register": [
        [_tc("c1", "attachment", {"action": "list"})],
        [_tc("c2", "attachment", {"action": "register", "path": "reports/summary.txt"})],
        "Done.",
    ],
    "view_image_and_notify": [
        [_tc("c1", "view_file", {"path": "assets/sample.png"})],
        [_tc("c2", "matrix_message", {"action": "send", "message": "Inspected assets/sample.png."})],
        "Done.",
    ],
}


@pytest.mark.asyncio
async def test_portable_surface_and_provider_resolution(tmp_path: Path) -> None:
    """Real AgentBuild strips deferred tools from requests and resolves local/openrouter/config specs."""
    provider = _ScriptedProvider(_SCRIPTS["workflow_runs_check"], capabilities=Capabilities(native_tool_search=True))
    result = await evaluate_case(CASES_BY_ID["workflow_runs_check"], provider)
    assert result.passed
    offered = {tool.name for tool in provider.requests[0].tools}
    assert {
        "run_shell_command",
        "read_file",
        "edit_file",
        "grep",
        "attachment",
        "view_file",
        "matrix_message",
        "skill",
        "tool_search",
        "call_tool",
    } <= offered
    assert offered.isdisjoint(
        {"show_canvas", "list_workflow_runs", "list_thread_tags", "scheduler", "memory", "browser_control"},
    )
    sys_text = "\n".join(m.get_content_string() for m in provider.requests[0].messages if m.role == "system")
    assert "deploy-check" in sys_text
    assert "tool_search" in sys_text

    assert (
        resolve_provider(
            "local:inkling-2:mini",
            config_path=None,
            base_url="http://llama.local/v1",
        ).capabilities.native_tool_search
        is False
    )
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "defaults:\n  tools: []\nagents:\n  eval_agent:\n    display_name: Eval\n    role: Eval\n"
        "memory:\n  backend: none\nmodels:\n  default:\n    provider: openai\n    id: inkling-2:mini\n"
        "    extra_kwargs:\n      base_url: http://llama.local/v1\n      api_key: sk-no-key-required\n"
        "  inkling-2:mini:\n    provider: openai\n    id: inkling-2:mini\n"
        "    extra_kwargs:\n      base_url: http://llama.local/v1\n      api_key: sk-no-key-required\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-test-key\n", encoding="utf-8")
    assert (
        resolve_provider(
            "openrouter:google/gemma-4-31b-it",
            config_path=cfg_file,
            base_url="http://llama.local/v1",
        ).capabilities.native_tool_search
        is False
    )
    cfg_p = resolve_provider("inkling-2:mini", config_path=cfg_file, base_url="http://llama.local/v1")
    assert cfg_p.capabilities.native_tool_search is False
    assert cfg_p.identity.id == "inkling-2:mini"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[case.case_id for case in CASES])
async def test_all_12_cases_pass_first_try(case: EvalCase) -> None:
    """Every one of the 12 eval cases passes on first try through the real MindRoom loop."""
    assert len(CASES) == 12
    result = await evaluate_case(case, _ScriptedProvider(_SCRIPTS[case.case_id]))
    assert result.error is None
    assert result.passed, f"{case.case_id} failed: {result.calls}"
    assert result.first_try_passed
    assert result.mis_calls == 0
    assert result.failure_counts == {}
    assert result.input_tokens > 0
    assert result.output_tokens > 0


@pytest.mark.asyncio
async def test_alternative_valid_paths_canvas_file_and_shell_config_edit() -> None:
    """Canvas via workspace file and grep_and_edit_config via shell sed both pass."""
    canvas_res = await evaluate_case(
        CASES_BY_ID["canvas_dashboard"],
        _ScriptedProvider(
            [
                [_tc("c1", "write_file", {"path": "widget.html", "content": "<div>staging=green</div>"})],
                [_call_tool("c2", "show_canvas", {"title": "Deploy", "path": "widget.html"})],
                "Done.",
            ],
        ),
    )
    assert canvas_res.first_try_passed

    cfg_res = await evaluate_case(
        CASES_BY_ID["grep_and_edit_config"],
        _ScriptedProvider(
            [
                [_tc("c1", "grep", {"pattern": "LEGACY_PORT"})],
                [_tc("c2", "run_shell_command", {"args": "sed -i 's/8080/9090/' src/config.py"})],
                "Done.",
            ],
        ),
    )
    assert cfg_res.first_try_passed


@pytest.mark.asyncio
async def test_stateful_stubs_persist_within_case() -> None:
    """Tags, memories, attachments, and matrix_room stubs keep state within a case."""
    tags_res = await evaluate_case(
        CASES_BY_ID["thread_tags_audit"],
        _ScriptedProvider(
            [
                [_call_tool("c1", "tag_thread", {"tag": "triage"})],
                [_call_tool("c2", "list_thread_tags", {})],
                [_call_tool("c3", "untag_thread", {"tag": "triage"})],
                [_call_tool("c4", "list_thread_tags", {})],
                [_call_tool("c5", "tag_thread", {"tag": "triage"})],
                "Done.",
            ],
        ),
        max_turns=6,
    )
    assert tags_res.first_try_passed
    assert "triage" in json.loads(tags_res.calls[1].result_preview)["tags"]
    assert "triage" not in json.loads(tags_res.calls[3].result_preview)["tags"]

    mem_res = await evaluate_case(
        CASES_BY_ID["memory_lookup_and_save"],
        _ScriptedProvider(
            [
                [
                    _call_tool("c1", "memory", {"action": "list"}),
                    _call_tool("c2", "memory", {"action": "get", "memory_id": "mem_1"}),
                ],
                [_call_tool("c3", "memory", {"action": "get", "memory_id": "mem_99"})],
                [
                    _call_tool("c4", "memory", {"action": "search", "query": "retry"}),
                    _call_tool("c5", "memory", {"action": "add", "content": "Prod uses 2s exponential backoff."}),
                ],
                [_call_tool("c6", "memory", {"action": "get", "memory_id": "mem_2"})],
                "Done.",
            ],
        ),
        max_turns=6,
    )
    assert mem_res.passed
    assert mem_res.failure_counts == {"tool_error": 1}
    assert json.loads(mem_res.calls[0].result_preview)["memories"][0]["id"] == "mem_1"
    assert json.loads(mem_res.calls[1].result_preview)["memory"]["id"] == "mem_1"
    assert json.loads(mem_res.calls[2].result_preview)["status"] == "error"
    assert json.loads(mem_res.calls[5].result_preview)["memory"]["id"] == "mem_2"

    att_res = await evaluate_case(
        CASES_BY_ID["attachment_list_and_register"],
        _ScriptedProvider(
            [
                [_tc("c1", "attachment", {"action": "list"})],
                [_tc("c2", "attachment", {"action": "register", "path": "reports/summary.txt"})],
                [_tc("c3", "attachment", {"action": "list"}), _tc("c4", "matrix_room", {"action": "room-info"})],
                "Done.",
            ],
        ),
    )
    assert att_res.first_try_passed
    assert json.loads(att_res.calls[0].result_preview)["attachments"] == []
    assert json.loads(att_res.calls[2].result_preview)["attachments"][0]["attachment_id"] == "att_eval_1"
    assert json.loads(att_res.calls[3].result_preview)["name"] == "Eval Room"


@pytest.mark.asyncio
async def test_live_failures_nested_call_tool_shell_argv_and_hallucinated_action() -> None:
    """All 3 inkling-2:mini failure modes are classified accurately and recover."""
    nested_res = await evaluate_case(
        CASES_BY_ID["canvas_dashboard"],
        _ScriptedProvider(
            [
                [
                    _tc(
                        "c1",
                        "call_tool",
                        {"arguments": {"name": "show_canvas", "title": "Deploy", "html": "<p>ok</p>"}},
                    ),
                ],
                [_tc("c2", "call_tool", {"name": "show_canvas", "arguments": ["not-an-object"]})],
                [_call_tool("c3", "nonexistent_fn", {})],
                [_call_tool("c4", "show_canvas", {"title": "Deploy", "html": "<p>ok</p>"})],
                "Done.",
            ],
        ),
        max_turns=6,
    )
    assert nested_res.passed
    assert not nested_res.first_try_passed
    assert nested_res.failure_counts == {"nested_call_tool_name": 2, "unknown_tool": 1}
    assert "Nothing was called" in nested_res.calls[0].result_preview

    assert has_shell_argv_syntax({"args": ["grep", "ERROR", "logs/service.log", "|", "wc", "-l"]})
    assert has_shell_argv_syntax({"args": ["echo", "hi", ">", "out.txt"]})
    assert has_shell_argv_syntax({"args": ["ls", "~/logs"]})
    assert not has_shell_argv_syntax({"args": ["bash", "-c", "grep ERROR logs/service.log | wc -l"]})
    assert not has_shell_argv_syntax({"args": ["python3", "-c", "x = 1; print(x > 0)"]})

    shell_res = await evaluate_case(
        CASES_BY_ID["shell_pipeline_logs"],
        _ScriptedProvider(
            [
                [
                    _tc(
                        "c1",
                        "run_shell_command",
                        {
                            "args": [
                                "grep",
                                "ERROR",
                                "logs/service.log",
                                "|",
                                "tail",
                                "-n",
                                "2",
                                ">",
                                "logs/recent_errors.txt",
                            ],
                        },
                    ),
                ],
                [
                    _tc(
                        "c2",
                        "run_shell_command",
                        {"args": "grep ERROR logs/service.log | tail -n 2 > logs/recent_errors.txt"},
                    ),
                ],
                "Done.",
            ],
        ),
    )
    assert shell_res.passed
    assert shell_res.failure_counts == {"shell_argv_syntax": 1}

    wf_res = await evaluate_case(
        CASES_BY_ID["workflow_runs_check"],
        _ScriptedProvider(
            [
                [_call_tool("c1", "list_workflow_runs", {"action": "list"})],
                [_call_tool("c2", "list_workflow_runs", {})],
                "Done.",
            ],
        ),
    )
    assert wf_res.passed
    assert wf_res.failure_counts == {"hallucinated_action": 1}
    assert "unexpected keyword argument 'action'" in wf_res.calls[0].result_preview

    view_res = await evaluate_case(
        CASES_BY_ID["view_image_and_notify"],
        _ScriptedProvider(
            [
                [_tc("c1", "view_file", {"action": "view", "path": "assets/sample.png"})],
                [_tc("c2", "view_file", {"path": "assets/sample.png"})],
                [_tc("c3", "matrix_message", {"action": "send", "message": "Inspected."})],
                "Done.",
            ],
        ),
    )
    assert view_res.passed
    assert view_res.failure_counts == {"hallucinated_action": 1}


@pytest.mark.asyncio
async def test_other_failure_kinds_plural_validation_raw_preview_and_turn_cap() -> None:
    """Plural Pydantic validation errors, _raw decode preview, call_id matching, and turn cap."""
    bad_raw = '{"name": "show_canvas", "html": "<div>unterminated'
    plural_res = await evaluate_case(
        CASES_BY_ID["shell_pipeline_logs"],
        _ScriptedProvider(
            [
                [_tc("c1", "run_shell_command", {"args": {"argv": ["ls"]}})],
                [_tc("c2", "call_tool", bad_raw)],
                [
                    _tc(
                        "c3",
                        "run_shell_command",
                        {"args": "grep ERROR logs/service.log | tail -n 2 > logs/recent_errors.txt"},
                    ),
                ],
                "Done.",
            ],
        ),
    )
    assert plural_res.passed
    assert "validation errors for" in plural_res.calls[0].result_preview
    assert plural_res.calls[0].failure_kind == "invalid_param_value"
    assert plural_res.calls[1].failure_kind == "invalid_param_value"
    assert plural_res.calls[1].raw_args == {"_raw": bad_raw}

    # Direct deferred call, missing/unexpected/invalid params, and tool_error
    sched_res = await evaluate_case(
        CASES_BY_ID["scheduler_reminder"],
        _ScriptedProvider(
            [
                [_tc("c1", "scheduler", {"action": "list"})],
                [_call_tool("c2", "scheduler", {"action": "bogus"})],
                [_call_tool("c3", "scheduler", {"action": "create", "message": "Check rollout"})],
                [
                    _call_tool(
                        "c4",
                        "scheduler",
                        {"action": "create", "message": "Check rollout", "new_thread": False, "delay_seconds": 7200},
                    ),
                ],
                "Done.",
            ],
        ),
        max_turns=6,
    )
    assert sched_res.passed
    assert sched_res.failure_counts == {"unknown_tool": 1, "invalid_param_value": 1, "missing_required_param": 1}

    browser_res = await evaluate_case(
        CASES_BY_ID["browser_inspect"],
        _ScriptedProvider(
            [
                [_call_tool("c1", "browser_control", {"action": "open", "url": "https://example.org/status"})],
                [_call_tool("c2", "browser_control", {"action": "open", "targetUrl": "https://example.org/status"})],
                "Done.",
            ],
        ),
    )
    assert browser_res.passed
    assert browser_res.failure_counts == {"unexpected_param": 1}

    # Out-of-order tool_call_id matching with index fallback when call_id is empty
    rec = RunRecord(
        run_id="r",
        session_id="s",
        tools=[
            ToolExecution(
                tool_call_id="b",
                tool_name="tool_search",
                tool_args={"query": "b"},
                is_error=False,
                result="{}",
            ),
            ToolExecution(
                tool_call_id="",
                tool_name="tool_search",
                tool_args={"query": "a"},
                is_error=False,
                result="{}",
            ),
        ],
    )
    traces, _ = _collect_traces(
        rec,
        [("a", 1, "tool_search", {"query": "a"}), ("b", 2, "tool_search", {"query": "b"})],
        {},
        DeferredTools(),
    )
    assert traces[0].turn == 2
    assert traces[0].raw_args == {"query": "b"}
    assert traces[1].turn == 1

    looping_p = _ScriptedProvider([[_tc(f"c{i}", "tool_search", {"query": "workflow"})] for i in range(10)])
    capped_res = await evaluate_case(CASES_BY_ID["workflow_runs_check"], looping_p, max_turns=2)
    assert not capped_res.passed
    assert capped_res.turns == 2


@pytest.mark.asyncio
async def test_run_cli_end_to_end_markdown_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """run_cli executes multiple models, prints Markdown summary, writes JSON, and rejects bad case IDs."""
    out_file = tmp_path / "results.json"

    def factory(model: str) -> _ScriptedProvider:
        if model == "clean":
            turns: list[list[dict[str, Any]] | str] = [
                [_call_tool("c1", "show_canvas", {"title": "Deploy", "html": "<div>ok</div>"})],
                "Done.",
            ]
        else:
            turns = [
                [
                    _tc(
                        "c1",
                        "call_tool",
                        {"arguments": {"name": "show_canvas", "title": "Deploy", "html": "<div>ok</div>"}},
                    ),
                ],
                [_call_tool("c2", "show_canvas", {"title": "Deploy", "html": "<div>ok</div>"})],
                "Done.",
            ]
        return _ScriptedProvider(turns, model_id=model)

    report = await run_cli(
        ["--model", "clean,noisy", "--cases", "canvas_dashboard", "--max-turns", "4", "--out", str(out_file)],
        provider_factory=factory,
    )
    stdout = capsys.readouterr().out
    assert "| Model | First-Try | Recovered | Mis-Calls | Failure Breakdown | Tokens | Elapsed |" in stdout
    assert "| clean | 1/1 | 1/1 | 0 | none |" in stdout
    assert "| noisy | 0/1 | 1/1 (+1) | 1 | nested_call_tool_name=1 |" in stdout
    saved = json.loads(out_file.read_text(encoding="utf-8"))
    assert saved == report
    assert [m["model"] for m in saved["models"]] == ["clean", "noisy"]
    assert saved["models"][1]["recovered"] == 1

    with pytest.raises(SystemExit):
        await run_cli(["--model", "clean", "--cases", "not_a_real_case"], provider_factory=factory)
