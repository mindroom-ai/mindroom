"""Run a standard agent's shell command to completion as one call, so its background job owns the whole run.

The command starts with a short native timeout slice. A command that outlives the slice is polled and stopped
through the toolkit's own check and kill functions, so every worker request stays short.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.shell.execution import DEFAULT_RUN_TIMEOUT_SECONDS
from mindroom.shell.supervisor import parse_shell_supervisor_status
from mindroom.tool_jobs.control import job_owns_execution
from mindroom.tool_system.construction import get_toolkit_construction
from mindroom.tool_system.declarations import declare_tool_schema_source

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agno.tools import Toolkit

_SLICE_SECONDS = 15
# A call that cannot continue in the background must not hold its reply forever.
_NOT_A_JOB_LIMIT_SECONDS = DEFAULT_RUN_TIMEOUT_SECONDS
_POLL_START_SECONDS = 1.0
_POLL_MAX_SECONDS = 5.0
_STOP_GRACE_SECONDS = 0.5
_HANDLE_RE = re.compile(r"^Handle: (shell:[0-9a-f]+)$", re.MULTILINE)
_CWD_RE = re.compile(r"^\[cwd: [^\n]*\]\n")
_LOST_MESSAGE = "The runtime running this command restarted, so its output is lost; it may have partly run."
_DESCRIPTION = (
    "Runs a shell command and returns its output or error when it finishes, however long it runs.\n"
    "The last ``tail`` lines of stdout are returned; on non-zero exit, useful stdout is preserved together with "
    "stderr. Meanwhile the call can continue in the background like any other tool, and cancelling it stops the "
    "command and everything it started. A command that never exits, such as a server, keeps its call running until "
    "it is cancelled; to keep one running after the call, start it detached "
    "(`nohup cmd > server.log 2>&1 &`) and read its log.\n\n"
    "With ``mindroom_output_path``, capture the complete supported output within the redirect byte limit instead "
    "of applying ``tail``."
)


def install_managed_shell(tools: Sequence[Toolkit]) -> bool:
    """Make each shell toolkit's command run to completion as its only function; return whether one was found."""
    installed = False
    for toolkit in tools:
        construction = get_toolkit_construction(toolkit)
        functions = toolkit.get_async_functions()
        run = functions.get("run_shell_command")
        # The registry name matches presets and aliases, as background-job exclusion does.
        if construction is None or construction.name != "shell" or run is None or run.entrypoint is None:
            continue
        check = functions["check_shell_command"].entrypoint
        kill = functions["kill_shell_command"].entrypoint
        assert check is not None
        assert kill is not None
        original = run.entrypoint
        managed = _managed_run(original, check, kill)
        declare_tool_schema_source(managed, original)
        run.entrypoint = managed
        run.parameters = _without_timeout(run.parameters)
        run.description = _managed_description(run.description or "")
        for name in ("check_shell_command", "kill_shell_command"):
            toolkit.functions.pop(name, None)
            toolkit.async_functions.pop(name, None)
        installed = True
    return installed


def _managed_run(
    run: Callable[..., object],
    check: Callable[..., object],
    kill: Callable[..., object],
) -> Callable[..., object]:
    async def run_shell_command(args: list[str] | str, tail: int = 100, **kwargs: Any) -> object:  # noqa: ANN401
        loop = asyncio.get_running_loop()
        limit = None if job_owns_execution() else _NOT_A_JOB_LIMIT_SECONDS
        deadline = None if limit is None else loop.time() + limit
        first_slice = _SLICE_SECONDS if limit is None else min(_SLICE_SECONDS, limit)
        first = await _call(run, args=args, tail=tail, timeout=first_slice, **kwargs)
        handle = _HANDLE_RE.search(first) if isinstance(first, str) else None
        if handle is None:
            return first
        assert isinstance(first, str)
        cwd = _CWD_RE.match(first)
        try:
            final = await _until_finished(check, kill, handle.group(1), deadline=deadline)
        except asyncio.CancelledError:
            await run_coroutine_until_complete(_stop(check, kill, handle.group(1)))
            raise
        return f"{cwd.group(0)}{final}" if cwd is not None else final

    return run_shell_command


async def _until_finished(
    check: Callable[..., object],
    kill: Callable[..., object],
    handle: str,
    *,
    deadline: float | None,
) -> str:
    loop = asyncio.get_running_loop()
    delay = _POLL_START_SECONDS
    while True:
        if deadline is not None and loop.time() >= deadline:
            await _stop(check, kill, handle)
            reply = str(await _call(check, handle))
            return f"{reply}\n\nStopped after {_NOT_A_JOB_LIMIT_SECONDS:g}s because this call cannot run in the background."
        wait = delay if deadline is None else min(delay, max(0.0, deadline - loop.time()))
        await asyncio.sleep(wait)
        delay = min(delay * 2, _POLL_MAX_SECONDS)
        reply = str(await _call(check, handle))
        status = parse_shell_supervisor_status(reply)
        if status.state == "running":
            continue
        if status.state == "unknown":
            raise RuntimeError(_LOST_MESSAGE)
        # A finished command, or the saved-file receipt of a redirected one.
        return reply


async def _stop(check: Callable[..., object], kill: Callable[..., object], handle: str) -> None:
    """Terminate the command's process group, then force it if it is still running after a short grace."""
    await _call(kill, handle)
    await asyncio.sleep(_STOP_GRACE_SECONDS)
    if parse_shell_supervisor_status(str(await _call(check, handle))).state == "running":
        await _call(kill, handle, force=True)


async def _call(function: Callable[..., object], *args: object, **kwargs: object) -> object:
    result = function(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def _without_timeout(parameters: dict[str, Any]) -> dict[str, Any]:
    managed = deepcopy(parameters)
    managed.get("properties", {}).pop("timeout", None)
    if "required" in managed:
        managed["required"] = [name for name in managed["required"] if name != "timeout"]
    return managed


def _managed_description(description: str) -> str:
    """Replace the native paragraphs about timeouts and handles, keeping the rest of the shell's guidance."""
    paragraphs = [paragraph for paragraph in description.split("\n\n") if "check_shell_command" not in paragraph]
    return "\n\n".join([_DESCRIPTION, *paragraphs])
