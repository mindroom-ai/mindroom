"""Logs name workers by digest, because worker keys embed requester identifiers."""

from __future__ import annotations

import ast
from pathlib import Path

from mindroom.tool_system.worker_routing import worker_dir_name, worker_log_id

_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "mindroom"
_LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical"})


def test_worker_log_id_hides_the_requester_and_matches_the_worker_directory() -> None:
    """The logged ID carries no requester part and still identifies the worker's directory."""
    worker_key = "v1:tenant:user_agent:@alice:example.org:assistant"

    log_id = worker_log_id(worker_key)

    assert "alice" not in log_id
    assert worker_dir_name(worker_key).endswith(f"-{log_id}")
    assert worker_log_id(worker_key) == log_id


def _raw_worker_key_arguments(call: ast.Call) -> list[ast.expr]:
    values = [*call.args, *(keyword.value for keyword in call.keywords)]
    return [
        value
        for value in values
        if (isinstance(value, ast.Name) and value.id == "worker_key")
        or (isinstance(value, ast.Attribute) and value.attr == "worker_key")
    ]


def test_no_log_call_passes_a_raw_worker_key() -> None:
    """Every log call wraps a worker key in `worker_log_id` instead of logging it as is."""
    offenders: list[str] = []
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in _LOG_METHODS:
                continue
            receiver = node.func.value
            if not (isinstance(receiver, ast.Name) and "log" in receiver.id.lower()):
                continue
            if _raw_worker_key_arguments(node):
                offenders.append(f"{path.relative_to(_SOURCE_ROOT)}:{node.lineno}")

    assert offenders == []
