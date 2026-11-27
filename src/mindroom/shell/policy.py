"""Shell argument normalization and foreground/request timeout policy helpers."""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING, Any, cast

from mindroom.agent_cli.shell_contract import SHELL_FUNCTION
from mindroom.shell.background_scope import current_shell_background_scope
from mindroom.tool_jobs.control import job_owns_execution

if TYPE_CHECKING:
    from collections.abc import Mapping

_DESKTOP_SHELL_FUNCTION = "run_desktop_shell"
_DEFAULT_RUN_TIMEOUT_SECONDS = 120
_SHELL_ARGS_ERROR = (
    '\'args\' must be a shell command string or a flat list of strings. Send args like "ls -la" or ["git", "status"].'
)
_SHELL_COMMAND_LINE_CHARS = frozenset("$|&;<>*?~`!(){}[]\n\r")
# Time a worker shell request needs beyond its command's foreground wait to deliver the answer.
_SHELL_REQUEST_GRACE_SECONDS = 30.0


def default_run_timeout_seconds() -> float:
    """Return the current foreground wait / auto-background threshold in seconds."""
    return float(_DEFAULT_RUN_TIMEOUT_SECONDS)


def shell_call_timeout(timeout: float | None) -> float | None:
    """Return when a shell call's command is stopped: its own timeout, none for a top-level job, else the default."""
    if timeout is not None:
        return timeout
    if job_owns_execution() and current_shell_background_scope() is None:
        return None
    return _DEFAULT_RUN_TIMEOUT_SECONDS


def _looks_like_shell_command_line(command: str) -> bool:
    """Return whether a single argv item is probably a shell command line."""
    if any(char.isspace() for char in command):
        return True
    return any(char in _SHELL_COMMAND_LINE_CHARS for char in command)


def _parse_shell_args(args: object) -> str | list[str]:
    """Parse natural shell command strings and explicit argv lists.

    Returns a raw shell command line string when the caller supplied one (including a
    single-item list containing shell syntax or whitespace), or an explicit argv list.
    """
    if isinstance(args, str):
        stripped = args.strip()
        if not stripped:
            raise ValueError(_SHELL_ARGS_ERROR)
        if stripped[0] not in "[{":
            return args
        try:
            args = json.loads(args)
        except json.JSONDecodeError as exc:
            if any(char.isspace() for char in stripped):
                return args
            raise ValueError(_SHELL_ARGS_ERROR) from exc

    if not isinstance(args, list):
        raise ValueError(_SHELL_ARGS_ERROR)  # noqa: TRY004

    if any(not isinstance(item, str) for item in args):
        raise ValueError(_SHELL_ARGS_ERROR)

    normalized_args = cast("list[str]", args)
    if len(normalized_args) == 1 and _looks_like_shell_command_line(normalized_args[0]):
        if not normalized_args[0].strip():
            raise ValueError(_SHELL_ARGS_ERROR)
        return normalized_args[0]
    return normalized_args


def normalize_shell_args(args: object) -> list[str]:
    """Normalize natural shell command strings and explicit argv lists into argv."""
    parsed = _parse_shell_args(args)
    return ["bash", "-c", parsed] if isinstance(parsed, str) else parsed


def shell_args_to_command(args: object) -> str:
    """Convert natural shell command strings or argv lists into one shell command line."""
    parsed = _parse_shell_args(args)
    if isinstance(parsed, str):
        return parsed
    if not parsed or not parsed[0]:
        raise ValueError(_SHELL_ARGS_ERROR)
    return shlex.join(parsed)


def shell_job_wait_seconds(registered_toolkit: str | None, function_name: str) -> float | None:
    """Return how long a reply waits for a shell command that runs as a job when its call sets no wait, else None.

    The reply waits as long as a command outside a job may run, then the command continues as a job, so a command
    that runs longer than the model expected never holds the conversation longer than one outside a job would.
    """
    if (registered_toolkit, function_name) in {("shell", SHELL_FUNCTION), ("desktop", _DESKTOP_SHELL_FUNCTION)}:
        return float(_DEFAULT_RUN_TIMEOUT_SECONDS)
    return None


def shell_request_budget_seconds(
    tool_name: str,
    function_name: str,
    kwargs: Mapping[str, Any],
    *,
    auto_background: bool = False,
) -> float:
    """Return how long one worker request outside a job may take for this shell call, or 0 when it has no foreground wait.

    Callers give a job's call no budget, as `shell_call_timeout` gives its command no limit.
    With ``auto_background``, foreground waiting is capped at the default auto-background wait.
    """
    if tool_name != "shell" or function_name != SHELL_FUNCTION:
        return 0.0
    raw_timeout = kwargs.get("timeout")
    try:
        timeout = _DEFAULT_RUN_TIMEOUT_SECONDS if raw_timeout is None else max(0.0, float(raw_timeout))
    except (TypeError, ValueError):
        timeout = _DEFAULT_RUN_TIMEOUT_SECONDS
    if auto_background:
        timeout = min(timeout, float(_DEFAULT_RUN_TIMEOUT_SECONDS))
    return timeout + _SHELL_REQUEST_GRACE_SECONDS
