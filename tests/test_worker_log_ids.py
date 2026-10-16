"""Logs name workers by digest, because worker keys embed requester identifiers."""

from __future__ import annotations

import ast
from pathlib import Path

from structlog.testing import capture_logs

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.tool_system.worker_routing import (
    private_instance_scope_root_path,
    shared_storage_root,
    worker_dir_name,
    worker_log_id,
)
from mindroom.workers.backends._dedicated_worker_common import plan_scoped_workspace_mounts

_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "mindroom"
_LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical"})


def test_worker_log_id_hides_the_requester_and_matches_the_worker_directory() -> None:
    """The logged ID carries no requester part and still identifies the worker's directory."""
    worker_key = "v1:tenant:user_agent:@alice:example.org:assistant"

    log_id = worker_log_id(worker_key)

    assert "alice" not in log_id
    assert worker_dir_name(worker_key).endswith(f"-{log_id}")
    assert worker_log_id(worker_key) == log_id


def _is_raw_worker_key(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id.endswith("worker_key")) or (
        isinstance(node, ast.Attribute) and node.attr.endswith("worker_key")
    )


def _raw_worker_key_arguments(call: ast.Call) -> list[ast.expr]:
    """Return log arguments that are, or format, a raw worker key."""
    values = [*call.args, *(keyword.value for keyword in call.keywords)]
    raw: list[ast.expr] = [value for value in values if _is_raw_worker_key(value)]
    for value in values:
        if isinstance(value, ast.JoinedStr):
            raw.extend(
                part.value
                for part in value.values
                if isinstance(part, ast.FormattedValue) and _is_raw_worker_key(part.value)
            )
    return raw


def _is_logger(receiver: ast.expr) -> bool:
    if isinstance(receiver, ast.Name):
        return "log" in receiver.id.lower()
    return isinstance(receiver, ast.Attribute) and "log" in receiver.attr.lower()


def _raw_key_log_calls(tree: ast.AST) -> list[ast.Call]:
    """Return log calls that pass, or format, a raw worker key."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _LOG_METHODS
        and _is_logger(node.func.value)
        and _raw_worker_key_arguments(node)
    ]


def test_no_log_call_passes_a_raw_worker_key() -> None:
    """Every log call wraps a worker key in `worker_log_id` instead of logging it as is."""
    offenders = [
        f"{path.relative_to(_SOURCE_ROOT)}:{call.lineno}"
        for path in sorted(_SOURCE_ROOT.rglob("*.py"))
        for call in _raw_key_log_calls(ast.parse(path.read_text(encoding="utf-8")))
    ]

    assert offenders == []


def _is_message_fstring(parent: ast.AST | None) -> bool:
    # Ruff's EM rules move a positional exception message into a variable first, whatever its name.
    if isinstance(parent, ast.Assign | ast.AnnAssign):
        return True
    # Thread names, and HTTP error details that the primary logs when a worker returns them.
    return isinstance(parent, ast.keyword) and parent.arg in {"name", "detail"}


def test_no_error_message_or_thread_name_embeds_a_raw_worker_key() -> None:
    """Error messages and thread names reach logs through tracebacks, so they also use `worker_log_id`."""
    offenders: list[str] = []
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.JoinedStr) and _is_message_fstring(parents.get(node))):
                continue
            for value in node.values:
                if not isinstance(value, ast.FormattedValue):
                    continue
                if _is_raw_worker_key(value.value):
                    offenders.append(f"{path.relative_to(_SOURCE_ROOT)}:{node.lineno}")

    assert offenders == []


_PRIVATE_USER_KEY = "v1:default:user:~@alice:example.org"


def _plan_private_mounts(storage_root: Path) -> tuple[object, ...]:
    config = Config(
        agents={"writer": AgentConfig(display_name="Writer", private=AgentPrivateConfig(per="user", root="workspace"))},
    )
    return plan_scoped_workspace_mounts(
        worker_key=_PRIVATE_USER_KEY,
        local_shared_storage_root=storage_root,
        worker_visible_shared_storage_root=Path("/app/worker"),
        private_agent_names=frozenset(),
        resolved_agent_policies=config.get_agent_policies(),
    )


def test_missing_private_workspace_log_names_no_requester(tmp_path: Path) -> None:
    """A private workspace that does not exist yet is logged below its scope, whose directory names the requester."""
    with capture_logs() as logs:
        mounts = _plan_private_mounts(tmp_path)

    assert mounts == ()
    [event] = [log for log in logs if log["event"] == "Not mounting a workspace that does not exist yet"]
    assert event["worker"] == worker_log_id(_PRIVATE_USER_KEY)
    assert event["workspace"] == "writer/workspace"
    assert "alice" not in repr(logs)


def test_linked_private_workspace_warning_names_no_requester(tmp_path: Path) -> None:
    """A private workspace replaced by a link is refused and logged below its scope too."""
    scope = private_instance_scope_root_path(shared_storage_root(tmp_path), _PRIVATE_USER_KEY)
    (scope / "writer").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (scope / "writer" / "workspace").symlink_to(elsewhere)

    with capture_logs() as logs:
        mounts = _plan_private_mounts(tmp_path)

    assert mounts == ()
    [event] = [log for log in logs if log["event"] == "Not mounting a workspace reached through a link"]
    assert event["worker"] == worker_log_id(_PRIVATE_USER_KEY)
    assert event["workspace"] == "writer/workspace"
    assert "alice" not in repr(logs)


def test_the_log_guard_catches_formatted_and_attribute_loggers() -> None:
    """The guard sees a key formatted into an f-string message and calls on `self.logger`-style receivers."""
    flagged = ast.parse(
        "logger.warning(f'worker {worker_key} failed')\nself.deps.logger.info('started', worker=handle.worker_key)\n",
    )
    allowed = ast.parse("logger.warning(f'worker {worker_log_id(worker_key)} failed')\n")

    assert len(_raw_key_log_calls(flagged)) == 2
    assert _raw_key_log_calls(allowed) == []
