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
from mindroom.agent_loop.output_limit import CUT_OFF_TOOL_CALL_RESULT, output_limit_policy
from mindroom.agent_loop.run import own_model_messages, run_agent
from mindroom.agent_loop.spec import RunContext, UnstoredSession
from mindroom.agents import create_agent
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths, workspace_home_identity_env
from mindroom.custom_tools.thread_tags import _validate_thread_tags_params
from mindroom.custom_tools.workflow import _validate_workflow_params
from mindroom.entity_resolution import persist_offline_entity_accounts
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
from mindroom.tool_system.tool_access import validate_tool_arguments
from mindroom.tool_system.toolkits import model_arguments
from mindroom.tool_system.worker_routing import agent_workspace_root_path, build_tool_execution_identity
from mindroom.turn_model import ModelScope, resolve_turn_model

from .cases import CallTrace, EvalCase, parse_future_iso_seconds, seed_workspace

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.llm.provider import Capabilities, DeferredTools, ModelProvider
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.tool_system.tool_function import ToolFunction

_REAL_TOOLKITS = frozenset({"shell", "filesystem", "dynamic_tools", None})
_FS_TOOLS = frozenset({"read_file", "write_file", "edit_file", "delete_file", "find_files", "grep", "ls"})
_FS_ERR_PREFIXES = ("Error:", "Error reading ", "Error writing ", "Error removing ", "Error running ", "Error listing ")
_SHELL_OPS = frozenset({"|", "&&", "||", ";", ">", ">>", "<", "<<", "2>&1", "1>&2", "&>", "&>>", "|&"})
_SHELL_INTERPS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "python", "python3", "perl", "ruby", "node"})
_SHELL_FLAGS = frozenset({"-c", "-lc", "-ec", "-e"})
_PORTABLE_PROJ = RequestProjection(lambda _inner, req: portable_tool_search_request(req))
_DECODE_KEYS = ("decoding function arguments", "not a valid json object", "invalid json", "must be a json object")
_INVALID_KEYS = ("invalid value for parameter", "type=literal_error", "validation error", "must be a positive")
_REQ_KEYS = ("missing a required", "type=missing_argument", "missing required parameter", "is required for action=")
_UNEXP_KEYS = ("unexpected keyword argument", "type=unexpected_keyword_argument", "cannot be used with action=")
_FAIL_RULES = (
    ("missing_required_param", _REQ_KEYS),
    ("unexpected_param", _UNEXP_KEYS),
    ("invalid_param_value", (*_INVALID_KEYS, *_DECODE_KEYS)),
)
_EVAL_TOOLS: list[Any] = [
    *("shell", "filesystem", "attachments", "matrix_message"),
    {"chat_ui": {"defer": True, "enable_show_canvas": True}},
    *({n: {"defer": True}} for n in ("workflow", "thread_tags", "scheduler", "memory", "browser")),
]
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
_BASE_ENV = {"MINDROOM_NO_AUTO_INSTALL_TOOLS": "1", "PYTHONDONTWRITEBYTECODE": "1", **dict.fromkeys(_PROXY_KEYS, "")}
_REQ_A = frozenset({"browser_read", "browser_control", "show_canvas", "workflow_read", "workflow_write", "thread_tags"})
_REQ_B = frozenset({"scheduler", "memory", "skill", "run_shell_command", "view_file", "attachment"})
_REQUIRED_SURFACE = _REQ_A | _REQ_B | {"matrix_message", "tool_search", "call_tool"}


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


def _require_file(ws: Path, path: str, label: str) -> None:
    if not (ws / path).is_file():
        msg = f"{label} file not found: {path}"
        raise FileNotFoundError(msg)


def _try_json(raw: str) -> object:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _check_attachment(action: str, kw: Mapping[str, Any], ws: Path) -> None:
    has_id, has_path = _str(kw.get("attachment_id")), _str(kw.get("path"))
    if action == "register":
        _need(has_path, "Missing required parameter 'path' for action='register'.")
    elif action == "get":
        _need(has_id, "Missing required parameter 'attachment_id' for action='get'.")
    elif action == "inline_image":
        _need(has_id or has_path, "Missing required parameter: give 'attachment_id' or 'path'.")
        _need(not (has_id and has_path), "Invalid value for parameter: pass 'attachment_id' or 'path', not both.")
    if action in {"register", "inline_image"} and has_path:
        _require_file(ws, str(kw["path"]), "Attachment")


def _check_workflow_or_tags(name: str, action: str, kw: Mapping[str, Any]) -> None:
    args = {k: v for k, v in kw.items() if k != "action"}
    if name == "thread_tags":
        _need(not (err := _validate_thread_tags_params(action, args)), err or "")
        return
    _validate_workflow_params(name, action, args)
    if name == "workflow_write" and action == "start":
        sources = sum(_str(args.get(k)) for k in ("source", "path", "name"))
        _need(sources >= 1, "Missing required parameter: give one of source, path, or name.")
        _need(sources == 1, "Invalid value for parameter: give only one of source, path, or name.")


def _check_semantics(name: str, action: str, kw: Mapping[str, Any], ws: Path) -> None:
    if name == "show_canvas":
        _need(bool(kw.get("canvas_event_id")) or _str(kw.get("title")), "Missing required parameter 'title'.")
        html, path, has_path = kw.get("html"), kw.get("path"), _str(kw.get("path"))
        _need(bool(html) != has_path, "Missing required parameter: give exactly one of html or path.")
        _need(not html or _str(html), "Invalid value for parameter 'html'.")
        if has_path:
            _require_file(ws, str(path), "Canvas")
    elif name == "scheduler" and action == "create":
        _need(_str(kw.get("message")), "Missing required parameter 'message' for action='create'.")
        _need(kw.get("new_thread") is not None, "Missing required parameter 'new_thread' for action='create'.")
        timings = sum(kw.get(k) is not None for k in ("execute_at", "delay_seconds", "cron"))
        _need(timings == 1, "Missing required parameter: timing field.")
        delay, ex_at = kw.get("delay_seconds"), kw.get("execute_at")
        _need(delay is None or delay >= 1, "Invalid value for parameter 'delay_seconds': must be >= 1.")
        ok_at = ex_at is None or parse_future_iso_seconds(ex_at, timezone=kw.get("timezone")) is not None
        _need(ok_at, "Invalid value for parameter 'execute_at': must be a future ISO-8601 datetime.")
    elif name == "memory" and action in {"search", "add", "get", "delete", "update"}:
        req = {"search": "query", "add": "content"}.get(action, "memory_id")
        _need(_str(kw.get(req)), f"Missing required parameter '{req}' for action={action!r}.")
        if action == "update":
            _need(_str(kw.get("content")), "Missing required parameter 'content' for action='update'.")
    elif name == "browser_control" and action in {"open", "navigate"}:
        has_target = _str(kw.get("targetUrl")) or bool(kw.get("paths"))
        _need(has_target, f"Missing required parameter 'targetUrl' for action={action!r}.")
    elif name == "attachment":
        _check_attachment(action, kw, ws)
    elif name in {"workflow_read", "workflow_write", "thread_tags"}:
        _check_workflow_or_tags(name, action, kw)
    elif name == "matrix_message" and action == "send":
        has_body = _str(kw.get("message")) or bool(kw.get("attachments"))
        _need(has_body, "Missing required parameter 'message' or 'attachments'.")


def _memory_stub(action: str, kw: Mapping[str, Any], memories: dict[str, str]) -> dict[str, Any]:
    if action in {"search", "list"}:
        items = [{"id": k, "content": v} for k, v in memories.items()]
        return {"status": "ok", "results" if action == "search" else "memories": items}
    if action == "add":
        mid = f"mem_{len(memories) + 1}"
        memories[mid] = str(kw["content"])
        return {"status": "ok", "memory_id": mid}
    if (mid := str(kw.get("memory_id") or "")) not in memories:
        return {"status": "error", "message": "Memory not found"}
    if action == "delete":
        memories.pop(mid)
        return {"status": "ok", "deleted": mid}
    if action == "update":
        memories[mid] = str(kw["content"])
    return {"status": "ok", "memory": {"id": mid, "content": memories[mid]}}


def _attachment_stub(action: str, kw: Mapping[str, Any], atts: dict[str, str]) -> dict[str, Any]:
    base: dict[str, Any] = {"status": "ok", "tool": "attachment", "action": action}
    if action == "register":
        aid = f"att_eval_{len(atts) + 1}"
        atts[aid] = str(kw["path"])
        return {**base, "attachment_id": aid}
    if action == "get":
        aid = str(kw["attachment_id"])
        if aid not in atts:
            return {**base, "status": "error", "message": f"Attachment not found: {aid}"}
        return {**base, "attachment": {"attachment_id": aid, "path": atts[aid]}}
    return {**base, "attachments": [{"attachment_id": k, "path": v} for k, v in atts.items()]}


def _stateful_stub(name: str, action: str, kw: Mapping[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    if name == "memory":
        return _memory_stub(action, kw, state["memories"])
    if name == "attachment":
        return _attachment_stub(action, kw, state["attachments"])
    if name == "thread_tags":
        tags: dict[str, dict[str, Any]] = state["tags"]
        if action == "tag":
            tags[str(kw["tag"])] = {}
        elif action == "untag":
            tags.pop(str(kw["tag"]), None)
        extra: dict[str, Any] = {"tags": dict(tags)} if action == "list" else {"tag": kw["tag"]}
        return {"status": "ok", "tool": "thread_tags", "action": action, **extra}
    wf_run = {"run_id": "wf_01", "title": "nightly-smoke", "status": "completed"}
    if name == "workflow_read":
        return {"runs": [wf_run], "saved": []} if action == "list" else {"status": "ok", "action": action, **wf_run}
    if name == "workflow_write":
        return {"status": "ok", "action": action, **wf_run}
    stubs: dict[str, dict[str, Any]] = {
        "show_canvas": {"status": "ok", "tool": "chat_ui", "action": "show_canvas", "event_id": "$canvas_eval_1"},
        "scheduler": {"status": "ok", "tool": "scheduler", "action": action, "task_id": "task_eval_1"},
        "browser_read": {"status": "ok", "action": action, "tabs": []},
        "browser_control": {"status": "ok", "action": action, "targetId": "tab_1", "url": kw.get("targetUrl") or ""},
        "matrix_message": {"status": "ok", "tool": "matrix_message", "action": action, "event_id": "$msg_eval_1"},
        "matrix_room": {"status": "ok", "room_id": "!room:localhost", "thread_id": "$thread", "name": "Eval Room"},
    }
    return stubs.get(name, {"status": "ok", "tool": name})


def _validate_and_stub(call: ToolCall, workspace: Path, state: dict[str, Any]) -> str:
    kw = model_arguments(call.arguments, call.function)
    sig, props = inspect.signature(call.function.entrypoint), call.function.parameters.get("properties", {})
    out = "mindroom_output_path"
    strip_out = out in kw and out in props and out not in sig.parameters
    bind_kw = {k: v for k, v in kw.items() if k != out} if strip_out else kw
    sig.bind(**bind_kw)
    for req in call.function.parameters.get("required", ()):
        _need(req in kw, f"Missing required parameter: '{req}'.")
    try:
        validate_tool_arguments(call.function, kw)
    except Exception as exc:
        msg = f"Invalid value for parameter: {exc.__cause__ or exc}"
        raise ValueError(msg) from exc
    name = call.function.name
    action = str(bind_kw.get("action") or ("send" if name == "matrix_message" else ""))
    _check_semantics(name, action, bind_kw, workspace)
    return json.dumps(_stateful_stub(name, action, bind_kw, state))


def _is_error_result(text: str, resolved_name: str) -> bool:
    if resolved_name == "run_shell_command":
        return False
    if resolved_name in _FS_TOOLS:
        return text.startswith(_FS_ERR_PREFIXES)
    if not text.lstrip().startswith("{") or not isinstance(d := _try_json(text), dict):
        return False
    return (
        d.get("status") == "error"
        or (resolved_name == "skill" and bool(d.get("error")))
        or (resolved_name == "view_file" and d.get("view_status") == "error")
    )


def _parse_raw_args(fn: object) -> dict[str, Any]:
    if not isinstance(fn, Mapping):
        return {}
    raw = fn.get("arguments")
    if isinstance(raw, dict):
        return raw
    if not (isinstance(raw, str) and raw.strip()):
        return {}
    return call_arguments(fn) or ({} if _try_json(raw) in ({}, []) else {"_raw": raw[:160]})


def has_shell_argv_syntax(args: Mapping[str, Any], *, is_error: bool = False) -> bool:
    """Return whether ``run_shell_command`` received a multi-item argv list containing shell syntax."""
    raw = args.get("args")
    if not isinstance(raw, list) or len(raw) <= 1 or not all(isinstance(x, str) for x in raw):
        return False
    if Path(raw[0]).name in _SHELL_INTERPS and any(tok in _SHELL_FLAGS for tok in raw[1:-1]):
        return False
    return any(
        tok in _SHELL_OPS
        or tok == "~"
        or tok.startswith(("~/", "<<", ">", "<"))
        or (is_error and (any(op in tok for op in ("&&", "||", "$(", "<<")) or any(c in tok for c in "|;><")))
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
    failed, lower = is_error or _is_error_result(result_text, resolved_name), result_text.lower()
    unknown = (deferred.portable and raw_name in deferred.names) or result_text == MISSING_TOOL_ERROR_TEXT
    if unknown or (failed and function is None):
        return "unknown_tool"
    if raw_name == "call_tool" and resolved_name == "call_tool" and result_text != CUT_OFF_TOOL_CALL_RESULT:
        target, nested = args.get("name"), args.get("arguments")
        ok_nested = (
            nested in (None, "")
            or isinstance(nested, dict)
            or (isinstance(nested, str) and (isinstance(d := _try_json(nested), dict) or d == []))
        )
        valid_target = _str(target) and target != "call_tool" and ok_nested
        bad_json = is_error and any(k in lower for k in _DECODE_KEYS)
        return "invalid_param_value" if bad_json else ("unknown_tool" if valid_target else "nested_call_tool_name")
    if resolved_name == "run_shell_command" and has_shell_argv_syntax(args, is_error=failed):
        return "shell_argv_syntax"
    if not failed:
        return None
    props = function.parameters.get("properties", {}) if function is not None else {}
    if "action" in args and isinstance(props, Mapping) and "action" not in props:
        return "hallucinated_action"
    return next((kind for kind, needles in _FAIL_RULES if any(n in lower for n in needles)), "tool_error")


def _build_eval_agent(root: Path, case: EvalCase, provider: ModelProvider) -> tuple[AgentBuild, Path]:
    storage = (root / "storage").resolve()
    ws = agent_workspace_root_path(storage, "eval_agent")
    path_val = os.pathsep.join(x for x in (str(Path(sys.executable).parent), os.environ.get("PATH", "")) if x)
    env = {**_BASE_ENV, "PATH": path_val, **workspace_home_identity_env(ws)}
    rp = resolve_runtime_paths(config_path=root / "config.yaml", storage_path=storage, process_env=env)
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
    seed_workspace(ws, case)
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
    tm, plugins = resolve_turn_model(config, rp, ModelScope(entity="eval_agent"), None), config_plugins(config, rp)
    wp = _PortableProvider(provider, portable_tool_search_stream, _PORTABLE_PROJ)
    build = create_agent("eval_agent", config, rp, identity, knowledge=None, model=tm, plugins=plugins, provider=wp)
    if missing := sorted(_REQUIRED_SURFACE - build.spec.functions.keys()):
        hint = (
            " (install browser extra: uv run --extra browser ...)"
            if {"browser_read", "browser_control"} & set(missing)
            else ""
        )
        msg = f"Eval agent missing required tool surface: {', '.join(missing)}{hint}"
        raise RuntimeError(msg)
    return build, ws


def _collect_traces(
    record: AnyRunRecord | None,
    raw_history: Sequence[tuple[str, int, str, dict[str, Any]]],
    functions: Mapping[str, ToolFunction],
    deferred: DeferredTools,
) -> tuple[tuple[CallTrace, ...], dict[str, int]]:
    unused = dict(enumerate(raw_history))
    traces, counts = [], Counter[str]()
    for idx, ex in enumerate(record.tools or () if record is not None else ()):
        cid = ex.tool_call_id
        match = next((i for i, item in unused.items() if cid and item[0] == cid), None)
        if match is None and not cid:
            match = idx if idx in unused else next(iter(unused), None)
        name = ex.tool_name
        _, turn, raw_name, raw_args = unused.pop(match) if match is not None else ("", 0, name, ex.tool_args or {})
        args, text, is_err = ex.tool_args if isinstance(ex.tool_args, dict) else {}, ex.result or "", bool(ex.is_error)
        failed, fn = is_err or _is_error_result(text, name), functions.get(name)
        kind = classify_failure(raw_name, name, args, is_error=is_err, result_text=text, function=fn, deferred=deferred)
        if kind is not None:
            counts[kind] += 1
        traces.append(CallTrace(turn, raw_name, raw_args, name, args, failed, kind, text[:220]))
    return tuple(traces), dict(sorted(counts.items()))


def _verify_case(case: EvalCase, ws: Path, calls: Sequence[CallTrace], err: str | None) -> tuple[bool, str | None]:
    if err is not None:
        return False, err
    try:
        return case.verify(ws, calls), None
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def evaluate_case(case: EvalCase, provider: ModelProvider, *, max_turns: int = 5) -> CaseResult:
    """Run one ``EvalCase`` through MindRoom's real ``create_agent`` and ``run_agent`` loop."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"mindroom-eval-{case.case_id}-") as tmp:
        build, workspace = _build_eval_agent(Path(tmp), case, provider)
        tpl, cid = build.request_template, f"eval-{case.case_id}"
        deferred, spec = tpl.deferred_tools, replace(build.spec, system_messages=prompt_for_turn(build))
        ctx = RunContext(run_id=cid, session=UnstoredSession(cid), user_id="@user:localhost", model_name="default")
        raw_history: list[tuple[str, int, str, dict[str, Any]]] = []
        batch_raw: dict[str, str] = {}
        current_turn = [0]
        mem = {"mem_1": "Staging retries use fixed 1s backoff."}
        stub_state: dict[str, Any] = {"tags": {"backend": {}}, "memories": mem, "attachments": {}}

        async def before_req(state: LoopState) -> RequestDecision:
            if state.request_index >= max_turns:
                return EndRun()
            current_turn[0] = state.request_index + 1
            return SendRequest()

        def rewrite_calls(calls: list[dict[str, Any]], fns: Mapping[str, ToolFunction]) -> list[dict[str, Any]]:
            batch_raw.clear()
            for tc in calls:
                call_id, fn = str(tc.get("id") or ""), tc.get("function")
                raw_name = str(fn.get("name") or "") if isinstance(fn, Mapping) else ""
                raw_history.append((call_id, current_turn[0], raw_name, _parse_raw_args(fn)))
                batch_raw[call_id] = raw_name
            return resolve_call_tool_calls(calls, fns) if deferred.portable else calls

        # Enforce the portable wire contract on direct deferred calls and intentionally skip build.tool_policy.
        async def middleware(call: ToolCall, call_next: ToolNext) -> object:
            raw = batch_raw.get(call.context.call_id, call.function.name)
            _need(not (deferred.portable and raw in deferred.names), f"Tool '{raw}' is deferred; use call_tool.")
            is_real = call.function.toolkit in _REAL_TOOLKITS or call.function.name == "view_file"
            return await call_next(call.arguments) if is_real else _validate_and_stub(call, workspace, stub_state)

        hooks = LoopHooks(
            before_request=(before_req,),
            answer_tool_calls=output_limit_policy(),
            rewrite_tool_calls=rewrite_calls,
            tool_middleware=(middleware,),
        )
        record: AnyRunRecord | None = None
        err: str | None = None
        try:
            msgs = [Message(role="user", content=case.prompt)]
            async for event in run_agent(spec, msgs, ctx=ctx, persist=None, request_template=tpl, hooks=hooks):
                if isinstance(event, RunFinished):
                    record = event.record
        except Exception as exc:
            err = record.error if record and record.error else f"{type(exc).__name__}: {exc}"

        calls, fc = _collect_traces(record, raw_history, spec.functions, deferred)
        mis = sum(fc.values())
        passed, err = _verify_case(case, workspace, calls, err)
        tok = record.metrics.tokens if record and record.metrics else None
        turns = len(own_model_messages(record)) if record else current_turn[0]
        in_t, out_t = (tok.input_tokens, tok.output_tokens) if tok else (0, 0)
        dt = round(time.monotonic() - started, 3)
        return CaseResult(case.case_id, passed, passed and not mis, turns, mis, fc, calls, in_t, out_t, dt, err)


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
