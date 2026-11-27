"""Full-surface MindRoom agent harness, tool validation stubs, and failure classifier."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.agent_loop.build import prompt_for_turn
from mindroom.agent_loop.model_loop import EndRun, LoopHooks, LoopState, RequestDecision, SendRequest
from mindroom.agent_loop.run import own_model_messages, run_agent
from mindroom.agent_loop.spec import RunContext, UnstoredSession
from mindroom.agents import create_agent
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.entity_resolution import persist_offline_entity_accounts
from mindroom.llm.errors import ProviderError
from mindroom.llm.events import RunFinished
from mindroom.llm.messages import Message
from mindroom.llm.tool_search import (
    call_arguments,
    portable_tool_search_request,
    portable_tool_search_stream,
    resolve_call_tool_calls,
)
from mindroom.providers.pipeline.wrapped import RequestProjection, WrappedProvider
from mindroom.tool_system.invocation import MISSING_TOOL_ERROR_TEXT, ToolCall, ToolNext
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.toolkits import model_arguments
from mindroom.tool_system.worker_routing import agent_workspace_root_path, build_tool_execution_identity
from mindroom.turn_model import ModelScope, resolve_turn_model

from .cases import CallTrace, EvalCase, seed_workspace

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.llm.provider import Capabilities, DeferredTools, ModelProvider
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.tool_system.tool_function import ToolFunction

_REAL_TOOLKITS = frozenset({"shell", "filesystem", "dynamic_tools", None})
_SHELL_OPS = frozenset({"|", "&&", "||", ";", ">", ">>", "<", "<<", "2>&1", "1>&2", "&>", "&>>", "|&"})
_SHELL_INTERPS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "python", "python3", "perl", "ruby", "node"})
_SHELL_FLAGS = frozenset({"-c", "-lc", "-ec", "-e"})
_PORTABLE_PROJ = RequestProjection(lambda _inner, req: portable_tool_search_request(req))
_DECODE_KEYS = ("decoding function arguments", "not a valid json object", "invalid json", "must be a json object")
_FAIL_RULES = (
    ("missing_required_param", ("missing a required argument", "type=missing_argument", "missing required parameter")),
    ("unexpected_param", ("unexpected keyword argument", "type=unexpected_keyword_argument")),
    (
        "invalid_param_value",
        (
            "invalid value for parameter",
            "type=literal_error",
            "validation error for",
            "must be a positive",
            *_DECODE_KEYS,
        ),
    ),
)
_EVAL_TOOLS: list[Any] = [
    "shell",
    "filesystem",
    "attachments",
    "matrix_message",
    {"chat_ui": {"defer": True, "enable_show_canvas": True}},
    *({n: {"defer": True}} for n in ("workflow", "thread_tags", "scheduler", "memory", "browser")),
]


@dataclass(frozen=True, slots=True)
class CaseResult:
    """Evaluation outcome for one case."""

    case_id: str
    passed: bool
    first_try_passed: bool
    turns: int
    mis_calls: int
    failure_counts: dict[str, int]
    calls: tuple[CallTrace, ...]
    input_tokens: int
    output_tokens: int
    elapsed_s: float
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _PortableProvider(WrappedProvider):
    """Force portable deferred tool search across all evaluated providers."""

    @property
    def capabilities(self) -> Capabilities:
        return replace(self.inner.capabilities, native_tool_search=False)


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def _str(v: object) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _matches_schema(v: object, schema: Mapping[str, Any]) -> bool:
    if "anyOf" in schema:
        return any(_matches_schema(v, opt) for opt in schema["anyOf"])
    if "enum" in schema and v not in schema["enum"]:
        return False
    expected = schema.get("type")
    if expected == "array":
        items = schema.get("items")
        return isinstance(v, list) and (not isinstance(items, Mapping) or all(_matches_schema(x, items) for x in v))
    ok = {
        "null": v is None,
        "string": isinstance(v, str),
        "boolean": isinstance(v, bool),
        "object": isinstance(v, dict),
    }
    ok["integer"] = isinstance(v, int) and not isinstance(v, bool)
    ok["number"] = isinstance(v, (int, float)) and not isinstance(v, bool)
    return ok.get(expected, True) if isinstance(expected, str) else True


def _check_semantics(name: str, action: str, kw: Mapping[str, Any], ws: Path) -> None:
    if name == "show_canvas":
        _need(bool(kw.get("canvas_event_id")) or _str(kw.get("title")), "Missing required parameter 'title'.")
        html, path = kw.get("html"), kw.get("path")
        _need(bool(html) != bool(path), "Missing required parameter: give exactly one of html or path.")
        _need(html is None or _str(html), "Invalid value for parameter 'html'.")
        if isinstance(path, str) and not (ws / path).is_file():
            msg = f"Canvas file not found: {path}"
            raise FileNotFoundError(msg)
    elif name == "scheduler" and action == "create":
        _need(_str(kw.get("message")), "Missing required parameter 'message' for action='create'.")
        _need(kw.get("new_thread") is not None, "Missing required parameter 'new_thread' for action='create'.")
        _need(
            sum(kw.get(k) is not None for k in ("execute_at", "delay_seconds", "cron")) == 1,
            "Missing required parameter: timing field.",
        )
        _need(
            kw.get("delay_seconds") is None or kw["delay_seconds"] >= 1,
            "Invalid value for parameter 'delay_seconds': must be >= 1.",
        )
    elif name == "memory" and action in {"search", "add"}:
        req = "query" if action == "search" else "content"
        _need(_str(kw.get(req)), f"Missing required parameter '{req}' for action={action!r}.")
    elif name == "browser_control" and action in {"open", "navigate"}:
        _need(
            _str(kw.get("targetUrl")) or bool(kw.get("paths")),
            f"Missing required parameter 'targetUrl' for action={action!r}.",
        )
    elif name == "attachment" and action == "register":
        _need(_str(kw.get("path")), "Missing required parameter 'path' for action='register'.")
        if not (ws / str(kw["path"])).is_file():
            msg = f"Attachment file not found: {kw['path']}"
            raise FileNotFoundError(msg)
    elif name == "matrix_message" and action == "send":
        _need(
            _str(kw.get("message")) or bool(kw.get("attachments")),
            "Missing required parameter 'message' or 'attachments'.",
        )


def _validate_and_stub(call: ToolCall, workspace: Path) -> str:
    kw = model_arguments(call.arguments, call.function)
    sig, props = inspect.signature(call.function.entrypoint), call.function.parameters.get("properties", {})
    if (
        "mindroom_output_path" in kw
        and "mindroom_output_path" in props
        and "mindroom_output_path" not in sig.parameters
    ):
        kw = {k: v for k, v in kw.items() if k != "mindroom_output_path"}
    sig.bind(**kw)
    for req in call.function.parameters.get("required", ()):
        _need(req in kw, f"Missing required parameter: '{req}'.")
    if isinstance(props, Mapping):
        for k, v in kw.items():
            if isinstance(prop := props.get(k), Mapping):
                _need(_matches_schema(v, prop), f"Invalid value for parameter '{k}': {v!r}.")
    name, action = (
        call.function.name,
        str(kw.get("action") or ("send" if call.function.name == "matrix_message" else "")),
    )
    _check_semantics(name, action, kw, workspace)
    stubs: dict[str, dict[str, Any]] = {
        "show_canvas": {"status": "ok", "tool": "chat_ui", "action": "show_canvas", "event_id": "$canvas_eval_1"},
        "list_workflow_runs": {
            "runs": [{"run_id": "wf_01", "title": "nightly-smoke", "status": "completed"}],
            "saved": [],
        },
        "list_thread_tags": {"status": "ok", "tool": "thread_tags", "action": "list", "tags": {"backend": {}}},
        "tag_thread": {"status": "ok", "tool": "thread_tags", "action": "tag", "tag": kw.get("tag")},
        "scheduler": {"status": "ok", "tool": "scheduler", "action": action, "task_id": "task_eval_1"},
        "memory": {"status": "ok", "results": [{"id": "mem_1", "content": "Staging retries use fixed 1s backoff."}]}
        if action == "search"
        else {"status": "ok", "memory_id": "mem_2"},
        "browser_control": {"status": "ok", "action": action, "targetId": "tab_1", "url": kw.get("targetUrl") or ""},
        "attachment": {
            "status": "ok",
            "tool": "attachment",
            "action": action,
            "attachment_id": "att_eval_1",
            "attachments": [],
        },
        "matrix_message": {"status": "ok", "tool": "matrix_message", "action": action, "event_id": "$msg_eval_1"},
    }
    return json.dumps(stubs.get(name, {"status": "ok", "tool": name}))


def _is_json_error(text: str) -> bool:
    if not text.lstrip().startswith("{"):
        return False
    try:
        parsed = json.loads(text)
    except ValueError:
        return False
    return isinstance(parsed, dict) and parsed.get("status") == "error"


def has_shell_argv_syntax(args: Mapping[str, Any], *, is_error: bool = False) -> bool:
    """Return whether ``run_shell_command`` received a multi-item argv list containing shell syntax."""
    raw = args.get("args")
    if not isinstance(raw, list) or len(raw) <= 1 or not all(isinstance(x, str) for x in raw):
        return False
    if Path(raw[0]).name in _SHELL_INTERPS and len(raw) >= 3 and raw[1] in _SHELL_FLAGS:
        return False
    return any(
        tok in _SHELL_OPS
        or tok == "~"
        or tok.startswith(("~/", "<<", ">", "<"))
        or any(op in tok for op in ("&&", "||", "$(", "<<"))
        or (is_error and any(c in tok for c in "|;><"))
        for tok in raw
    )


def classify_failure(
    raw_name: str,
    resolved_name: str,
    args: Mapping[str, Any],
    *,
    is_error: bool,
    result_text: str,
    function: ToolFunction | None,
    deferred: DeferredTools,
) -> str | None:
    """Classify a failed or mis-routed tool call into one failure kind, or None when clean."""
    failed, lower = is_error or _is_json_error(result_text), result_text.lower()
    if (
        (deferred.portable and raw_name in deferred.names)
        or result_text == MISSING_TOOL_ERROR_TEXT
        or (failed and function is None)
    ):
        return "unknown_tool"
    if raw_name == "call_tool" and resolved_name == "call_tool":
        return (
            "invalid_param_value" if (is_error and any(k in lower for k in _DECODE_KEYS)) else "nested_call_tool_name"
        )
    if resolved_name == "run_shell_command" and has_shell_argv_syntax(args, is_error=failed):
        return "shell_argv_syntax"
    if not failed:
        return None
    props = function.parameters.get("properties", {}) if function is not None else {}
    if "action" in args and isinstance(props, Mapping) and "action" not in props:
        return "hallucinated_action"
    return next((kind for kind, needles in _FAIL_RULES if any(n in lower for n in needles)), "tool_error")


def _build_eval_agent(root: Path, case: EvalCase, provider: ModelProvider) -> tuple[AgentBuild, Path]:
    bin_dir, base_path = str(Path(sys.executable).parent), os.environ.get("PATH", "")
    env = {"MINDROOM_NO_AUTO_INSTALL_TOOLS": "1", "PATH": f"{bin_dir}{os.pathsep}{base_path}" if base_path else bin_dir}
    rp = resolve_runtime_paths(config_path=root / "config.yaml", storage_path=root / "storage", process_env=env)
    agent = {
        "display_name": "EvalAgent",
        "role": "Engineering assistant.",
        "tools": _EVAL_TOOLS,
        "memory_backend": "file",
    }
    authored = {
        "defaults": {"tools": [], "allow_shell_without_worker": True},
        "agents": {"eval_agent": agent},
        "memory": {"backend": "none"},
        "models": {"default": {"provider": "openai", "id": provider.identity.id}},
        "timezone": "UTC",
        "background_tool_jobs": {"enabled": False},
    }
    config = Config.validate_with_runtime(authored, rp)
    persist_offline_entity_accounts(config, rp)
    workspace = agent_workspace_root_path(rp.storage_root, "eval_agent")
    seed_workspace(workspace, case)
    identity = build_tool_execution_identity(
        channel="matrix",
        agent_name="eval_agent",
        runtime_paths=rp,
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=f"eval-{case.case_id}",
    )
    tm = resolve_turn_model(config, rp, ModelScope(entity="eval_agent"), None)
    wrapped = _PortableProvider(provider, portable_tool_search_stream, _PORTABLE_PROJ)
    build = create_agent(
        "eval_agent",
        config,
        rp,
        identity,
        knowledge=None,
        model=tm,
        plugins=config_plugins(config, rp),
        provider=wrapped,
    )
    return build, workspace


def _collect_traces(
    record: AnyRunRecord | None,
    raw_history: Sequence[tuple[int, str, dict[str, Any]]],
    functions: Mapping[str, ToolFunction],
    deferred: DeferredTools,
) -> tuple[tuple[CallTrace, ...], dict[str, int]]:
    traces, counts = [], Counter[str]()
    for idx, ex in enumerate(record.tools or () if record is not None else ()):
        turn, raw_name, raw_args = raw_history[idx] if idx < len(raw_history) else (0, ex.tool_name, ex.tool_args or {})
        args, text = ex.tool_args if isinstance(ex.tool_args, dict) else {}, ex.result or ""
        fn = functions.get(ex.tool_name)
        kind = classify_failure(
            raw_name,
            ex.tool_name,
            args,
            is_error=bool(ex.is_error),
            result_text=text,
            function=fn,
            deferred=deferred,
        )
        if kind is not None:
            counts[kind] += 1
        err = bool(ex.is_error) or _is_json_error(text)
        traces.append(CallTrace(turn, raw_name, raw_args, ex.tool_name, args, err, kind, text[:220]))
    return tuple(traces), dict(sorted(counts.items()))


async def evaluate_case(case: EvalCase, provider: ModelProvider, *, max_turns: int = 5) -> CaseResult:
    """Run one ``EvalCase`` through MindRoom's real ``create_agent`` and ``run_agent`` loop."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"mindroom-eval-{case.case_id}-") as tmp:
        build, workspace = _build_eval_agent(Path(tmp), case, provider)
        deferred, spec = (
            build.request_template.deferred_tools,
            replace(build.spec, system_messages=prompt_for_turn(build)),
        )
        cid = f"eval-{case.case_id}"
        ctx = RunContext(run_id=cid, session=UnstoredSession(cid), user_id="@user:localhost", model_name="default")
        raw_history: list[tuple[int, str, dict[str, Any]]] = []
        batch_raw: dict[str, str] = {}
        current_turn = [0]

        async def before_request(state: LoopState) -> RequestDecision:
            if state.request_index >= max_turns:
                return EndRun()
            current_turn[0] = state.request_index + 1
            return SendRequest()

        def rewrite_calls(calls: list[dict[str, Any]], fns: Mapping[str, ToolFunction]) -> list[dict[str, Any]]:
            batch_raw.clear()
            for tc in calls:
                fn = tc.get("function") or {}
                raw_name = str(fn.get("name") or "")
                raw_history.append((current_turn[0], raw_name, call_arguments(fn) if isinstance(fn, Mapping) else {}))
                batch_raw[str(tc.get("id") or "")] = raw_name
            return resolve_call_tool_calls(calls, fns) if deferred.portable else calls

        async def middleware(call: ToolCall, call_next: ToolNext) -> object:
            raw = batch_raw.get(call.context.call_id, call.function.name)
            _need(not (deferred.portable and raw in deferred.names), f"Tool '{raw}' is deferred; use call_tool.")
            is_real = call.function.toolkit in _REAL_TOOLKITS or call.function.name == "view_file"
            return await call_next(call.arguments) if is_real else _validate_and_stub(call, workspace)

        hooks = LoopHooks(
            before_request=(before_request,),
            rewrite_tool_calls=rewrite_calls,
            tool_middleware=(middleware,),
        )
        record: AnyRunRecord | None = None
        error_text: str | None = None
        try:
            msgs = [Message(role="user", content=case.prompt)]
            async for event in run_agent(
                spec,
                msgs,
                ctx=ctx,
                persist=None,
                request_template=build.request_template,
                hooks=hooks,
            ):
                if isinstance(event, RunFinished):
                    record = event.record
        except ProviderError as exc:
            error_text = f"{type(exc).__name__}: {exc}"

        calls, failure_counts = _collect_traces(record, raw_history, spec.functions, deferred)
        mis_calls = sum(failure_counts.values())
        passed = error_text is None and case.verify(workspace, calls)
        tok = record.metrics.tokens if record is not None and record.metrics is not None else None
        turns = len(own_model_messages(record)) if record is not None else current_turn[0]
        return CaseResult(
            case.case_id,
            passed,
            passed and mis_calls == 0,
            turns,
            mis_calls,
            failure_counts,
            calls,
            tok.input_tokens if tok else 0,
            tok.output_tokens if tok else 0,
            round(time.monotonic() - started, 3),
            error_text,
        )


async def evaluate_model(
    provider: ModelProvider,
    cases: Sequence[EvalCase],
    *,
    max_turns: int = 5,
    concurrency: int = 1,
) -> tuple[CaseResult, ...]:
    """Evaluate ``provider`` across ``cases`` with bounded concurrency."""
    sem = asyncio.Semaphore(max(1, concurrency))

    async def run_one(case: EvalCase) -> CaseResult:
        async with sem:
            return await evaluate_case(case, provider, max_turns=max_turns)

    return tuple(await asyncio.gather(*(run_one(case) for case in cases)))
