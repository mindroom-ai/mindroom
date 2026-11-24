"""Stage 1 deterministic friction classifier and session incident detector (zero SQLite/config/LLM imports)."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any

from mindroom.history.run_checkpoint import INTERRUPTED_CALL_RESULT
from mindroom.hooks.enrichment import is_transient_context, strip_enrichment_blocks
from mindroom.prompt_message_tags import parse_msg_tags
from mindroom.trace_audit.models import (
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
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SENT_BY_YOU_RE = re.compile(r"""<msg\b[^>]*\bsent_by_you=(["'])true\1[^>]*><!\[CDATA\[.*?\]\]></msg>""", re.DOTALL)

_MAX_JSON_ERROR_CHARS = 65_536
_WINDOW_SECONDS = 86_400.0
_TERMINAL_STATUSES = frozenset({"COMPLETED", "ERROR", "CANCELLED"})
SHELL_TOOLS = frozenset({"run_shell_command", "bash", "shell"})
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


def _extract_operation(tool_name: str, tool_args: dict[str, Any]) -> str:
    if tool_name.rsplit(".", 1)[-1] in SHELL_TOOLS:
        return ""
    for key in ("action", "operation", "method"):
        val = tool_args.get(key)
        if isinstance(val, str) and _IDENT_RE.match(val.lower()):
            return val.lower()
    return ""


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
    if kind in {"ok", "approval_denied", "interrupted"}:
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
    if (exit_match := _EXIT_CODE_RE.match(first_line)) and (code := int(exit_match.group(1))) != 0:
        return ("nonzero_exit", True, result, code)
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


def _evaluate_run_incident(session: AuditedSession, idx: int) -> Incident | None:
    run = session.runs[idx]
    run_outcomes = [classify_tool_execution(t) for t in run.tools]
    has_run_failure = any(o.is_failure for o in run_outcomes)
    has_run_error = run.status == "ERROR" or bool((run.error or "").strip())
    has_failing_tools = any(count > 0 for count in run.failing_tools.values())
    if not (has_run_failure or has_run_error or has_failing_tools):
        return None

    prev = [
        p
        for p in reversed(session.runs[:idx])
        if p.status in _TERMINAL_STATUSES and run.created_at - p.created_at <= _WINDOW_SECONDS
    ][:2]
    window = (*reversed(prev), run)
    window_outcomes = [*(classify_tool_execution(t) for item in reversed(prev) for t in item.tools), *run_outcomes]
    window_failures = [o for o in window_outcomes if o.is_failure]

    reasons: list[IncidentReason] = []
    if has_run_error:
        reasons.append("run_error")
    if has_run_failure:
        if any(c >= 2 for c in Counter((f.tool_name, f.operation) for f in window_failures).values()):
            reasons.append("repeat_tool_error")
        if len(window_failures) >= 2:
            reasons.append("multi_tool_error")
        if _has_tool_loop(window_outcomes):
            reasons.append("tool_loop")
    if has_failing_tools:
        reasons.append("failing_tools")
    if not reasons:
        return None
    return Incident(
        session_id=session.session_id,
        room_id=session.room_id,
        run_id=run.run_id,
        run_seq=run.seq,
        created_at=run.created_at,
        window_run_ids=tuple(item.run_id for item in window),
        reasons=tuple(reasons),
        failures=tuple(window_failures),
    )


def detect_session_incidents(
    session: AuditedSession,
    *,
    now: float,
    min_idle_seconds: int = 600,
    after_seq: int = 0,
) -> list[Incident]:
    """Detect Stage 1 material friction incidents for one settled session."""
    if not session.runs or now - session.last_updated_at < min_idle_seconds:
        return []
    return [
        incident
        for idx, run in enumerate(session.runs)
        if run.seq > after_seq
        and run.status in _TERMINAL_STATUSES
        and (incident := _evaluate_run_incident(session, idx)) is not None
    ]
