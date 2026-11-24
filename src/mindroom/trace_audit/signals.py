"""Stage 1 deterministic friction classifier and session incident detector (zero SQLite/config/LLM imports)."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections import Counter
from typing import Any

from mindroom.history.run_checkpoint import INTERRUPTED_CALL_RESULT
from mindroom.hooks.enrichment import is_transient_context, strip_enrichment_blocks
from mindroom.prompt_message_tags import parse_msg_tags
from mindroom.trace_audit.models import (
    AuditedRun,
    AuditedSession,
    Incident,
    IncidentReason,
    RunMessageRecord,
    ToolExecutionRecord,
    ToolOutcome,
    ToolOutcomeKind,
)

_TIMEOUT_RE = re.compile(r"^Stopped after \d+(?:\.\d+)?s\.")
_EXIT_CODE_RE = re.compile(r"^Exit code (\d+)\b")
_ANCHORED_ERROR_RE = re.compile(r"^(?:Error|ERROR|Traceback \(most recent call last\)|Exception|FATAL):")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SENT_BY_YOU_RE = re.compile(r"""<msg\b[^>]*\bsent_by_you=(["'])true\1[^>]*><!\[CDATA\[.*?\]\]></msg>""", re.DOTALL)
_LEADING_MENTION_RE = re.compile(r"^@[\w.-]+(?::[\w.-]+)?[,:\s]+")
_CORRECTION_RE = re.compile(
    r"^(?:stop(?:\s|[.,!?:;]|$)|no,|wrong|incorrect|you didn't|you did not|you forgot|don't |do not |remember to|nee,|verkeerd|niet zo)",
)

_MAX_JSON_ERROR_CHARS = 65_536
_WINDOW_SECONDS = 86_400.0
_CORRECTION_WINDOW_SECONDS = 600.0
_TERMINAL_STATUSES = frozenset({"COMPLETED", "ERROR", "CANCELLED"})
_SHELL_TOOLS = frozenset({"run_shell_command", "bash", "shell"})
_READER_TOOLS = frozenset(
    {
        "read_file",
        "grep",
        "find_files",
        "ls",
        "read_url",
        "scrape_webpage",
        "search_knowledge_base",
        "search_memories",
        "get_skill_instructions",
        "get_skill_reference",
        "get_skill_script",
        "matrix_message",
        "matrix_room",
        "view_file",
        "get_attachment",
    },
)
_BENIGN_EXIT_1_BINS = frozenset(
    {"grep", "rg", "ag", "ack", "diff", "cmp", "test", "[", "[[", "pgrep", "which", "command"},
)
_ALLOWED_SHELL_BINS = (
    _BENIGN_EXIT_1_BINS
    | {"git", "python", "python3", "pytest", "uv", "npm", "npx", "bun", "cargo", "rustc", "go", "node"}
    | {"ssh", "scp", "rsync", "gh", "curl", "wget", "ls", "cat", "head", "tail", "sed", "awk", "find"}
    | {"cp", "mv", "rm", "mkdir", "chmod", "chown", "docker", "nix", "jq", "timeout", "make", "just"}
    | {"sqlite3", "tar", "systemctl", "sh", "bash", "zsh"}
)
_BENIGN_GIT_SUBS = frozenset({"diff", "grep"})
_GIT_VALUE_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
_PREAMBLE_BINS = frozenset({"cd", "set", "export", "unset", "umask"})
_SHELL_OPS = frozenset({";", "&&", "||", "|", "&", "\n"})
_DIAGNOSTIC_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    (family, tuple(raw.split("|")))
    for family, raw in (
        ("timeout", "timed out|timeout|deadline exceeded|stopped after "),
        ("rate_limit", "rate limit|too many requests|429|quota exceeded|throttl"),
        ("permission", "permission denied|access denied|forbidden|unauthorized|401|403|eacces|eperm"),
        (
            "not_found",
            "no such file|not found|does not exist|enoent|404|command not found|cannot find|modulenotfounderror|filenotfounderror",
        ),
        (
            "syntax",
            "syntaxerror|syntax error|invalid syntax|parse error|jsondecodeerror|unexpected token|unrecognized arguments|invalid argument",
        ),
        ("connection", "connection refused|connection reset|could not resolve|econnrefused|502|503|504"),
        ("conflict", "already exists|merge conflict|non-fast-forward|409|database is locked"),
    )
)


def extract_human_text(raw: str) -> str:
    """Strip Matrix ``<msg>`` and MindRoom enrichment wrappers from a stored user prompt."""
    if not raw or is_transient_context(raw):
        return ""
    cleaned = _SENT_BY_YOU_RE.sub("", raw)
    if not cleaned.strip():
        return ""
    bodies = [strip_enrichment_blocks(body).strip() for _, _, body in parse_msg_tags(cleaned, default_sender="")]
    return "\n".join(body for body in bodies if body).strip()


def first_human_text(messages: tuple[RunMessageRecord, ...]) -> str:
    """Return the triggering human prompt text from a run's stored messages."""
    for msg in messages:
        if (
            msg.role == "user"
            and not msg.is_synthetic
            and msg.add_to_agent_memory
            and not msg.from_history
            and (text := extract_human_text(msg.content))
        ):
            return text
    return ""


def _strip_env(seq: list[str]) -> list[str]:
    idx = 0
    while idx < len(seq) and ((seq[idx] == "env" and idx + 1 < len(seq)) or _ENV_ASSIGN_RE.match(seq[idx])):
        idx += 1
    return seq[idx:]


def _shell_tokens(raw: object) -> list[str] | None:
    if isinstance(raw, list) and raw and all(isinstance(item, str) for item in raw):
        if len(raw) == 1:
            raw = raw[0]
        elif raw[0].rsplit("/", 1)[-1] in {"sh", "bash", "zsh"} and len(raw) >= 3 and raw[1] in {"-c", "-lc"}:
            raw = raw[2]
        else:
            return list(raw)
    if not isinstance(raw, str):
        return None
    try:
        lexer = shlex.shlex(raw, posix=True, punctuation_chars=";&|\n")
        lexer.whitespace, lexer.whitespace_split = " \t\r", True
        return list(lexer)
    except ValueError:
        return None


def _parse_shell_command(tool_args: dict[str, Any]) -> tuple[list[str], bool]:
    """Return ``(primary_tokens, is_simple_command)`` without executing anything."""
    tokens = _shell_tokens(tool_args.get("command") or tool_args.get("args") or tool_args.get("cmd"))
    if not tokens:
        return [], False
    ops = [i for i, tok in enumerate(tokens) if tok in _SHELL_OPS]
    if not ops:
        return _strip_env(tokens), True
    if len(ops) == 1 and tokens[ops[0]] == "&&":
        head, tail = _strip_env(tokens[: ops[0]]), _strip_env(tokens[ops[0] + 1 :])
        if head and head[0] == "cd" and tail:
            return tail, True
    start = 0
    for op_idx in (*ops, len(tokens)):
        seg = _strip_env(tokens[start:op_idx])
        if seg and seg[0] not in _PREAMBLE_BINS:
            return seg, False
        start = op_idx + 1
    return _strip_env(tokens[: ops[0]]), False


def _git_subcommand(tokens: list[str]) -> str | None:
    idx = 1
    while idx < len(tokens):
        tok = tokens[idx]
        if tok in _GIT_VALUE_FLAGS:
            idx += 2
        elif tok.startswith("-"):
            idx += 1
        else:
            return tok.lower() if _IDENT_RE.match(tok.lower()) else None
    return None


def _extract_operation(tool_name: str, tool_args: dict[str, Any]) -> str:
    if tool_name.rsplit(".", 1)[-1] in _SHELL_TOOLS:
        seg, _ = _parse_shell_command(tool_args)
        binary = seg[0].rsplit("/", 1)[-1] if seg else ""
        if binary == "git":
            return f"git:{sub}" if (sub := _git_subcommand(seg)) else "git"
        return binary if binary in _ALLOWED_SHELL_BINS else ""
    for key in ("action", "operation", "method"):
        val = tool_args.get(key)
        if isinstance(val, str) and _IDENT_RE.match(val.lower()):
            return val.lower()
    return ""


def _stderr_is_empty(result: str) -> bool:
    lines = result.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "Stderr:":
            return all(not rest.strip() for rest in lines[idx + 1 :])
        if stripped.startswith("Stderr:"):
            return not stripped.removeprefix("Stderr:").strip()
    return True


def _json_error_text(result: str) -> str | None:
    stripped = result.lstrip()
    if len(stripped) > _MAX_JSON_ERROR_CHARS or not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    mto = payload.get("mindroom_tool_output")
    mto_err = isinstance(mto, dict) and mto.get("status") == "error"
    errcode = payload.get("errcode")
    if not (
        payload.get("status") == "error"
        or mto_err
        or payload.get("view_status") == "error"
        or payload.get("ok") is False
        or (isinstance(errcode, str) and bool(errcode.strip()))
    ):
        return None
    raw_parts = [
        errcode if isinstance(errcode, str) else "",
        mto.get("error", "") if mto_err and isinstance(mto, dict) else "",
        *(payload.get(k, "") for k in ("error", "message", "detail")),
    ]
    parts = [p.strip() for p in raw_parts if isinstance(p, str) and p.strip()]
    return " | ".join(parts) if parts else stripped[:240]


def _diagnostic_family(kind: ToolOutcomeKind, text: str, exit_code: int | None = None) -> str:
    if kind in {"ok", "benign_exit_1", "approval_denied", "interrupted"}:
        return kind
    if kind == "tool_timeout":
        return "timeout"
    lowered = text.lower()
    for family, needles in _DIAGNOSTIC_PATTERNS:
        if any(needle in lowered for needle in needles):
            return family
    return f"exit_{exit_code}" if exit_code is not None else "other"


def _classify_output(tool: ToolExecutionRecord, result: str) -> tuple[ToolOutcomeKind, bool, str, int | None]:
    first_line = result.lstrip("\r\n").splitlines()[0] if result.lstrip("\r\n") else ""
    if _TIMEOUT_RE.match(first_line):
        return ("tool_timeout", True, result, None)
    if exit_match := _EXIT_CODE_RE.match(first_line):
        code = int(exit_match.group(1))
        if code != 0:
            seg, is_simple = _parse_shell_command(tool.tool_args)
            binary = seg[0].rsplit("/", 1)[-1] if seg else ""
            benign = (
                code == 1
                and is_simple
                and _stderr_is_empty(result)
                and (binary in _BENIGN_EXIT_1_BINS or (binary == "git" and _git_subcommand(seg) in _BENIGN_GIT_SUBS))
            )
            return ("benign_exit_1", False, "", 1) if benign else ("nonzero_exit", True, result, code)
    if (json_err := _json_error_text(result)) is not None:
        return ("json_error", True, json_err, None)
    if tool.tool_name.rsplit(".", 1)[-1] not in _READER_TOOLS and _ANCHORED_ERROR_RE.match(first_line):
        return ("text_error", True, result, None)
    return ("ok", False, "", None)


def classify_tool_execution(tool: ToolExecutionRecord) -> ToolOutcome:
    """Classify one recorded tool execution once, in priority order."""
    result = tool.result or ""
    kind: ToolOutcomeKind
    if tool.confirmed is False:
        kind, is_failure, text, exit_code = ("approval_denied", False, "", None)
    elif result.strip() == INTERRUPTED_CALL_RESULT:
        kind, is_failure, text, exit_code = ("interrupted", False, "", None)
    elif tool.is_error:
        kind, is_failure, text, exit_code = ("tool_error", True, result, None)
    else:
        kind, is_failure, text, exit_code = _classify_output(tool, result)
    canonical = json.dumps(tool.tool_args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return ToolOutcome(
        tool_ord=tool.ord,
        tool_name=tool.tool_name,
        operation=_extract_operation(tool.tool_name, tool.tool_args),
        kind=kind,
        diagnostic_family=_diagnostic_family(kind, text or result, exit_code=exit_code),
        is_failure=is_failure,
        error_excerpt=" ".join((text or result).strip().split())[:300] if is_failure else "",
        args_digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16],
    )


def _find_user_correction(runs: tuple[AuditedRun, ...], idx: int) -> str | None:
    anchor = max(runs[idx].created_at, runs[idx].updated_at)
    for follow in runs[idx + 1 :]:
        if follow.created_at - anchor > _CORRECTION_WINDOW_SECONDS:
            break
        if follow.created_at < runs[idx].created_at or not (text := first_human_text(follow.messages)):
            continue
        cleaned = text.strip()
        if (
            not cleaned
            or len(cleaned) > 400
            or "```" in cleaned
            or cleaned.startswith((">", '"', "'", "\u201c", "\u2018"))
        ):
            return None
        norm = _LEADING_MENTION_RE.sub("", cleaned).replace("\u2019", "'").strip().lower()
        return text if _CORRECTION_RE.match(norm) else None
    return None


def _has_tool_loop(outcomes: list[ToolOutcome]) -> bool:
    streak, prev_key = 0, None
    for out in outcomes:
        if not out.is_failure:
            streak, prev_key = 0, None
            continue
        key = (out.tool_name, out.args_digest, out.kind, out.diagnostic_family)
        streak = streak + 1 if key == prev_key else 1
        prev_key = key
        if streak >= 3:
            return True
    return False


def _tool_signals(
    run: AuditedRun,
    window_outcomes: list[ToolOutcome],
    window_failures: list[ToolOutcome],
    *,
    median_tools: float | None,
    baseline_run_count: int | None,
) -> tuple[list[IncidentReason], list[IncidentReason]]:
    run_outcomes = window_outcomes[-len(run.tools) :] if run.tools else []
    if not any(outcome.is_failure for outcome in run_outcomes):
        return [], []
    material: list[IncidentReason] = []
    weak: list[IncidentReason] = []
    if any(c >= 2 for c in Counter((f.tool_name, f.operation) for f in window_failures).values()):
        material.append("repeat_tool_error")
    if len(window_failures) >= 2:
        material.append("multi_tool_error")
    if _has_tool_loop(window_outcomes):
        material.append("tool_loop")
    if len(window_failures) == 1:
        weak.append("single_tool_error")
    has_base = median_tools is not None and (baseline_run_count is None or baseline_run_count >= 20)
    threshold = max(15.0, 3.0 * median_tools) if has_base and median_tools is not None else 20.0
    if len(run.tools) >= threshold:
        weak.append("tool_outlier")
    return material, weak


def _is_settled_cancel(run: AuditedRun) -> bool:
    if run.status != "CANCELLED":
        return False
    duration = run.duration_seconds if run.duration_seconds is not None else max(0.0, run.updated_at - run.created_at)
    interrupted = (
        run.has_interrupted_checkpoint
        or any(tool.result.strip() == INTERRUPTED_CALL_RESULT for tool in run.tools)
        or any(msg.role == "tool" and msg.content.strip() == INTERRUPTED_CALL_RESULT for msg in run.messages)
    )
    return (len(run.tools) >= 1 or duration > 15.0) and not interrupted


def _clean_sample_selected(run: AuditedRun, *, sample_rate: float, sample_salt: str) -> bool:
    if sample_rate <= 0.0 or run.status != "COMPLETED" or (len(run.tools) < 1 and len(run.content.strip()) < 200):
        return False
    digest = hashlib.sha256(f"{sample_salt}:{run.run_id}".encode()).digest()
    return (int.from_bytes(digest[:8], "big") / float(1 << 64)) < sample_rate


def _evaluate_run_incident(
    session: AuditedSession,
    idx: int,
    *,
    median_tools: float | None,
    baseline_run_count: int | None,
    sample_rate: float,
    sample_salt: str,
) -> Incident | None:
    run = session.runs[idx]
    run_outcomes = [classify_tool_execution(t) for t in run.tools]
    has_run_error = run.status == "ERROR" or bool((run.error or "").strip())
    has_failing_tools = any(count > 0 for count in run.failing_tools.values())
    has_cancel = _is_settled_cancel(run)
    correction = _find_user_correction(session.runs, idx)
    if not (any(o.is_failure for o in run_outcomes) or has_run_error or has_failing_tools or has_cancel or correction):
        if not _clean_sample_selected(run, sample_rate=sample_rate, sample_salt=sample_salt):
            return None
        return Incident(
            session_id=session.session_id,
            room_id=session.room_id,
            run_id=run.run_id,
            run_seq=run.seq,
            created_at=run.created_at,
            window_run_ids=(run.run_id,),
            reasons=("clean_sample",),
            is_clean_sample=True,
        )

    prev = [
        p
        for p in reversed(session.runs[:idx])
        if p.status in _TERMINAL_STATUSES and run.created_at - p.created_at <= _WINDOW_SECONDS
    ][:2]
    window = (*reversed(prev), run)
    window_outcomes = [*(classify_tool_execution(t) for item in reversed(prev) for t in item.tools), *run_outcomes]
    window_failures = [o for o in window_outcomes if o.is_failure]
    material, weak = _tool_signals(
        run,
        window_outcomes,
        window_failures,
        median_tools=median_tools,
        baseline_run_count=baseline_run_count,
    )
    if has_run_error:
        material.insert(0, "run_error")
    if has_failing_tools:
        material.append("failing_tools")
    if has_cancel:
        weak.append("settled_cancel")
    if correction is not None:
        weak.append("user_correction")
    if not (material or len(weak) >= 2):
        return None
    return Incident(
        session_id=session.session_id,
        room_id=session.room_id,
        run_id=run.run_id,
        run_seq=run.seq,
        created_at=run.created_at,
        window_run_ids=tuple(item.run_id for item in window),
        reasons=(*material, *weak),
        failures=tuple(window_failures),
        correction_text=correction,
    )


def detect_session_incidents(
    session: AuditedSession,
    *,
    now: float,
    min_idle_seconds: int = 600,
    median_tools: float | None = None,
    baseline_run_count: int | None = None,
    after_seq: int = 0,
    sample_rate: float = 0.02,
    sample_salt: str = "",
) -> list[Incident]:
    """Detect Stage 1 friction incidents and deterministic clean-run samples for one settled session."""
    if not session.runs or now - session.last_updated_at < min_idle_seconds:
        return []
    return [
        incident
        for idx, run in enumerate(session.runs)
        if run.seq > after_seq
        and run.status in _TERMINAL_STATUSES
        and (
            incident := _evaluate_run_incident(
                session,
                idx,
                median_tools=median_tools,
                baseline_run_count=baseline_run_count,
                sample_rate=sample_rate,
                sample_salt=sample_salt,
            )
        )
        is not None
    ]
