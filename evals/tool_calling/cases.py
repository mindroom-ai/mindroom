"""Realistic full-surface tool-calling eval cases, workspace fixtures, and verifiers."""

from __future__ import annotations

import base64
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class CallTrace:
    """One recorded tool call from an evaluation run."""

    turn: int
    raw_name: str
    raw_args: dict[str, Any]
    resolved_name: str
    args: dict[str, Any]
    is_error: bool
    failure_kind: str | None
    result_preview: str


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One self-contained tool-calling evaluation case."""

    case_id: str
    prompt: str
    verify: Callable[[Path, Sequence[CallTrace]], bool]
    files: Mapping[str, str | bytes] = field(default_factory=dict)


DEPLOY_SKILL_MD = (
    "---\nname: deploy-check\ndescription: Pre-deployment checklist.\n---\n\n"
    "1. Staging error rate < 0.5%.\n2. Migrations complete.\n3. Rollback tag pinned.\n"
)
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
)
_LAST_TWO = [
    "2026-11-27T08:05:22Z ERROR upstream 502 from payments",
    "2026-11-27T08:07:33Z ERROR worker OOM killed on node-4",
]
_LOG = (
    "2026-11-27T08:00:01Z INFO startup complete\n2026-11-27T08:01:12Z ERROR db timeout on shard-1\n"
    "2026-11-27T08:02:00Z WARN retry scheduled\n2026-11-27T08:03:45Z ERROR cache miss storm on edge-2\n"
    f"{_LAST_TWO[0]}\n{_LAST_TWO[1]}\n2026-11-27T08:08:00Z INFO drain finished\n"
)
_CALC = {
    "src/__init__.py": "",
    "src/calc.py": "def apply_discount(price: float, pct: float) -> float:\n    return price + (price * pct / 100.0)\n",
    "check_calc.py": (
        "from src.calc import apply_discount\n"
        'assert apply_discount(100.0, 20.0) == 80.0\nassert apply_discount(50.0, 10.0) == 45.0\nprint("OK")\n'
    ),
}
_CFG = {
    "src/__init__.py": "",
    "src/config.py": 'LEGACY_PORT = 8080\nSERVICE_NAME = "edge-api"\n',
    "src/server.py": 'from src.config import LEGACY_PORT\ndef bind_address() -> str:\n    return f"0.0.0.0:{LEGACY_PORT}"\n',
}


def seed_workspace(workspace: Path, case: EvalCase) -> None:
    """Populate ``workspace`` with the shared deploy skill and ``case``'s fixture files."""
    for rel, content in {"skills/deploy-check/SKILL.md": DEPLOY_SKILL_MD, **case.files}.items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content) if isinstance(content, bytes) else target.write_text(content, encoding="utf-8")


def _ok(calls: Sequence[CallTrace], name: str, pred: Callable[[dict[str, Any]], bool] | None = None) -> bool:
    return any(
        c.resolved_name == name and not c.is_error and c.failure_kind is None and (pred is None or pred(c.args))
        for c in calls
    )


def _str(v: object) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _ends(v: object, suffix: str) -> bool:
    return _str(v) and Path(str(v)).as_posix().endswith(suffix)


def _v_canvas(ws: Path, c: Sequence[CallTrace]) -> bool:
    def good(a: dict[str, Any]) -> bool:
        h, p = a.get("html"), a.get("path")
        has_html = isinstance(h, str) and "<" in h and bool(h.strip())
        has_file = _str(p) and (ws / str(p)).is_file() and bool((ws / str(p)).read_text().strip())
        return _str(a.get("title")) and (has_html or has_file)

    return _ok(c, "show_canvas", good)


def _v_sched(_ws: Path, c: Sequence[CallTrace]) -> bool:
    def good(a: dict[str, Any]) -> bool:
        d = a.get("delay_seconds")
        timed = (isinstance(d, int) and not isinstance(d, bool) and d > 0) or _str(a.get("execute_at"))
        return (
            a.get("action") == "create" and _str(a.get("message")) and isinstance(a.get("new_thread"), bool) and timed
        )

    return _ok(c, "scheduler", good)


def _v_mem(_ws: Path, c: Sequence[CallTrace]) -> bool:
    searched = _ok(c, "memory", lambda a: a.get("action") == "search" and _str(a.get("query")))
    saved = _ok(
        c,
        "memory",
        lambda a: (
            a.get("action") == "add"
            and _str(a.get("content"))
            and ("2s" in a["content"].lower() or "backoff" in a["content"].lower())
        ),
    )
    return searched and saved


def _v_logs(ws: Path, c: Sequence[CallTrace]) -> bool:
    out = ws / "logs" / "recent_errors.txt"
    if not (_ok(c, "run_shell_command") and out.is_file()):
        return False
    return [x.strip() for x in out.read_text(encoding="utf-8").splitlines() if x.strip()] == _LAST_TWO


def _v_calc(ws: Path, c: Sequence[CallTrace]) -> bool:
    if not _ok(c, "run_shell_command", lambda a: "check_calc.py" in str(a.get("args", ""))):
        return False
    proc = subprocess.run([sys.executable, "check_calc.py"], cwd=ws, capture_output=True, timeout=10, check=False)
    return proc.returncode == 0


def _v_cfg(ws: Path, c: Sequence[CallTrace]) -> bool:
    cfg = ws / "src" / "config.py"
    searched = _ok(c, "grep") or _ok(c, "run_shell_command", lambda a: "LEGACY_PORT" in str(a.get("args", "")))
    if not (searched and cfg.is_file()):
        return False
    return "LEGACY_PORT = 9090" in (text := cfg.read_text(encoding="utf-8")) and "8080" not in text


def _v_attach(_ws: Path, c: Sequence[CallTrace]) -> bool:
    listed = _ok(c, "attachment", lambda a: a.get("action") == "list")
    reg = _ok(c, "attachment", lambda a: a.get("action") == "register" and _ends(a.get("path"), "reports/summary.txt"))
    return listed and reg


def _v_view(_ws: Path, c: Sequence[CallTrace]) -> bool:
    viewed = _ok(c, "view_file", lambda a: _ends(a.get("path"), "assets/sample.png"))
    sent = _ok(c, "matrix_message", lambda a: a.get("action", "send") == "send" and _str(a.get("message")))
    return viewed and sent


CASES: tuple[EvalCase, ...] = (
    EvalCase(
        "canvas_dashboard",
        "Build a tiny interactive HTML deploy-status widget (staging=green, prod=amber) and display it in chat as a canvas.",
        _v_canvas,
    ),
    EvalCase(
        "workflow_runs_check",
        "Check what workflow runs are currently sitting around in this conversation and report their status.",
        lambda _w, c: _ok(c, "list_workflow_runs"),
    ),
    EvalCase(
        "thread_tags_audit",
        "Check what tags are on this thread right now, and add the `triage` tag if it isn't already there.",
        lambda _w, c: _ok(c, "list_thread_tags") and _ok(c, "tag_thread", lambda a: a.get("tag") == "triage"),
    ),
    EvalCase(
        "scheduler_reminder",
        "Schedule a one-time reminder in 2 hours in this thread to check the staging rollout.",
        _v_sched,
    ),
    EvalCase(
        "memory_lookup_and_save",
        "Search saved memory for our retry backoff policy, and save a note that prod uses exponential backoff starting at 2s.",
        _v_mem,
    ),
    EvalCase(
        "browser_inspect",
        "Open https://example.org/status in the browser so we can inspect the status page.",
        lambda _w, c: _ok(
            c,
            "browser_control",
            lambda a: a.get("action") in {"open", "navigate"} and "example.org/status" in str(a.get("targetUrl") or ""),
        ),
    ),
    EvalCase(
        "shell_pipeline_logs",
        "Using the shell, count ERROR lines in logs/service.log, write the last 2 ERROR lines to logs/recent_errors.txt, and report the count.",
        _v_logs,
        {"logs/service.log": _LOG},
    ),
    EvalCase(
        "shell_and_edit_fix",
        "Read src/calc.py and check_calc.py — apply_discount has a bug. Fix src/calc.py and run check_calc.py with the shell to confirm it passes.",
        _v_calc,
        _CALC,
    ),
    EvalCase(
        "grep_and_edit_config",
        "Find where LEGACY_PORT is defined and referenced in the workspace, then update its value in src/config.py from 8080 to 9090.",
        _v_cfg,
        _CFG,
    ),
    EvalCase(
        "skill_load_deploy",
        "Before touching deployment config, load the `deploy-check` skill instructions and summarize the checklist.",
        lambda _w, c: _ok(c, "skill", lambda a: a.get("action") == "instructions" and a.get("name") == "deploy-check"),
    ),
    EvalCase(
        "attachment_list_and_register",
        "Check existing conversation attachments, then register reports/summary.txt as a conversation attachment.",
        _v_attach,
        {"reports/summary.txt": "Q4 uptime: 99.97%\nIncidents: 0\n"},
    ),
    EvalCase(
        "view_image_and_notify",
        "Inspect assets/sample.png with your image viewer tool, then send a quick status message in this thread saying it was inspected.",
        _v_view,
        {"assets/sample.png": _PNG},
    ),
)
CASES_BY_ID: dict[str, EvalCase] = {case.case_id: case for case in CASES}
