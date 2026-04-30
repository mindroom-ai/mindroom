"""Shell tool configuration with async subprocess execution and timeout-to-handle support."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import signal
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, cast

from agno.tools.toolkit import Toolkit
from pydantic import BeforeValidator

from mindroom.constants import RuntimePaths, shell_execution_runtime_env_values
from mindroom.logging_config import get_logger
from mindroom.tool_system.metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolExecutionTarget,
    ToolManagedInitArg,
    ToolStatus,
    register_tool_with_metadata,
)
from mindroom.vendor_telemetry import vendor_telemetry_env_values

_LOCAL_SHELL_PASSTHROUGH_ENV_KEYS = frozenset(
    {
        "CURL_CA_BUNDLE",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_PROXY",
        "PATH",
        "PIP_CACHE_DIR",
        "PYTHONPATH",
        "PYTHONPYCACHEPREFIX",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SHELL",
        "TERM",
        "TMPDIR",
        "UV_CACHE_DIR",
        "USER",
        "VIRTUAL_ENV",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    },
)
_STALE_RECORD_SECONDS = 600  # 10 minutes
_MAX_BACKGROUNDED = 16
_MAX_OUTPUT_LINES = 10_000
_PROCESS_EXIT_POLL_INTERVAL_SECONDS = 0.05
_POST_EXIT_READER_GRACE_SECONDS = 0.5
_SHELL_ARGS_ERROR = '\'args\' must be a flat list of strings. Send args like ["bash", "-lc", "ls"].'

# Module-level process registry shared across all MindRoomShellTools instances.
# This ensures handles survive toolkit re-creation (e.g. in sandbox runner mode
# where _resolve_entrypoint builds a fresh toolkit per request).
_process_registry: dict[str, _ProcessRecord] = {}


def _normalize_shell_args(args: object) -> list[str]:
    """Normalize stringified shell args while keeping the public schema as list[str]."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError as exc:
            raise ValueError(_SHELL_ARGS_ERROR) from exc

    if not isinstance(args, list):
        raise ValueError(_SHELL_ARGS_ERROR)  # noqa: TRY004

    if any(not isinstance(item, str) for item in args):
        raise ValueError(_SHELL_ARGS_ERROR)

    return cast("list[str]", args)


def _shell_path_prepend_entries(shell_path_prepend: str | None) -> tuple[str, ...]:
    """Parse configured shell PATH prefixes."""
    if shell_path_prepend is None:
        return ()
    return tuple(part.strip() for part in re.split(r"[\n,]+", shell_path_prepend) if part.strip())


def _shell_subprocess_path(
    current_path: str | None,
    *,
    prepend_entries: tuple[str, ...] = (),
) -> str | None:
    """Return the PATH value for shell subprocesses."""
    if current_path is None and not prepend_entries:
        return current_path

    path_entries = [entry for entry in prepend_entries if entry]
    if current_path:
        path_entries.extend(entry for entry in current_path.split(os.pathsep) if entry)

    if not path_entries:
        return current_path

    deduped_entries: list[str] = []
    seen_entries: set[str] = set()
    for entry in path_entries:
        if entry in seen_entries:
            continue
        seen_entries.add(entry)
        deduped_entries.append(entry)
    return os.pathsep.join(deduped_entries)


def _shell_subprocess_env(
    runtime_env: dict[str, str],
    *,
    base_process_env: dict[str, str] | None = None,
    shell_path_prepend: str | None = None,
) -> dict[str, str]:
    """Build the env passed to shell subprocesses."""
    env = {key: value for key, value in os.environ.items() if key in _LOCAL_SHELL_PASSTHROUGH_ENV_KEYS}
    if base_process_env is not None:
        env.update({key: value for key, value in base_process_env.items() if key in _LOCAL_SHELL_PASSTHROUGH_ENV_KEYS})
    env.update(runtime_env)

    path_value = _shell_subprocess_path(
        env.get("PATH"),
        prepend_entries=_shell_path_prepend_entries(shell_path_prepend),
    )
    if path_value is None:
        env.pop("PATH", None)
    else:
        env["PATH"] = path_value
    env.update(vendor_telemetry_env_values())
    return env


def _login_bash_command_index(args: list[str]) -> int | None:
    """Return the command index for `bash -lc ...` style invocations."""
    if not args or Path(args[0]).name != "bash":
        return None

    login_shell = False
    command_index: int | None = None
    for index, arg in enumerate(args[1:], start=1):
        if arg == "--":
            break
        if not arg.startswith("-"):
            break
        if arg in {"-l", "--login"}:
            login_shell = True
            continue
        if arg.startswith("--"):
            continue
        if "l" in arg:
            login_shell = True
        if "c" in arg:
            command_index = index + 1
            break

    if not login_shell or command_index is None or command_index >= len(args):
        return None
    return command_index


def _restore_env_exports_for_login_shell(env: dict[str, str]) -> str:
    """Return shell exports that restore env values commonly reset by login startup."""
    path_value = env.get("PATH")
    if path_value is None:
        return ""
    return f"export PATH={shlex.quote(path_value)}; "


def _shell_subprocess_args(args: list[str], env: dict[str, str]) -> list[str]:
    """Return argv adjusted for shell startup files that rewrite env overlays."""
    command_index = _login_bash_command_index(args)
    if command_index is None:
        return args

    env_restore = _restore_env_exports_for_login_shell(env)
    if not env_restore:
        return args

    adjusted_args = list(args)
    # Login bash can reset PATH in /etc/profile after subprocess env is applied.
    # Restore MindRoom's captured execution PATH without re-sourcing workspace hooks.
    adjusted_args[command_index] = env_restore + adjusted_args[command_index]
    return adjusted_args


def _handle_namespace(*, runtime_paths: RuntimePaths, base_dir: Path | None) -> str:
    """Return the namespace that owns one shell handle registry record."""
    storage_root = str(runtime_paths.storage_root.resolve())
    resolved_base_dir = str(base_dir.expanduser().resolve()) if base_dir is not None else ""
    return f"{storage_root}::{resolved_base_dir}"


@dataclass
class _ProcessRecord:
    """Tracks a backgrounded shell process."""

    namespace: str
    handle: str
    pid: int
    args: list[str]
    process: asyncio.subprocess.Process
    stdout_buf: deque[str] = field(default_factory=lambda: deque(maxlen=_MAX_OUTPUT_LINES))
    stderr_buf: deque[str] = field(default_factory=lambda: deque(maxlen=_MAX_OUTPUT_LINES))
    started_at: float = field(default_factory=time.monotonic)
    tail: int = 100
    finished: bool = False
    finished_at: float | None = None
    return_code: int | None = None
    _monitor_task: asyncio.Task[None] | None = field(default=None, repr=False)


@register_tool_with_metadata(
    name="shell",
    display_name="Shell Commands",
    description="Execute shell commands and scripts",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    default_execution_target=ToolExecutionTarget.WORKER,
    icon="Terminal",
    icon_color="text-green-500",
    config_fields=[
        ConfigField(
            name="base_dir",
            label="Base Dir",
            type="text",
            required=False,
            default=None,
            authored_override=False,
        ),
        ConfigField(
            name="enable_run_shell_command",
            label="Enable Run Shell Command",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="extra_env_passthrough",
            label="Extra Env Passthrough",
            type="text",
            required=False,
            default=None,
            placeholder="MY_SERVICE_URL, MY_SERVICE_*",
            description=(
                "Comma or newline-separated env var names or glob patterns to expose to shell "
                "execution in addition to the committed runtime env."
            ),
        ),
        ConfigField(
            name="shell_path_prepend",
            label="Shell PATH Prepend",
            type="text",
            required=False,
            default=None,
            placeholder="/opt/custom/bin, /run/wrappers/bin",
            description="Comma or newline-separated path entries to prepend to PATH for shell execution.",
        ),
    ],
    agent_override_fields=[
        ConfigField(
            name="extra_env_passthrough",
            label="Env Passthrough",
            type="string[]",
            required=False,
            default=None,
            placeholder="GITEA_TOKEN",
            description="Extra env var names or glob patterns exposed to shell execution for this agent only.",
        ),
        ConfigField(
            name="shell_path_prepend",
            label="PATH Prepend",
            type="string[]",
            required=False,
            default=None,
            placeholder="/run/wrappers/bin",
            description="Path entries prepended to PATH for this agent's shell tool only.",
        ),
    ],
    managed_init_args=(ToolManagedInitArg.RUNTIME_PATHS,),
    dependencies=[],
    docs_url="https://docs.agno.com/tools/toolkits/local/shell",
    function_names=("check_shell_command", "kill_shell_command", "run_shell_command"),
)
def shell_tools() -> type[Toolkit]:  # noqa: C901
    """Return shell tools for command execution."""

    class MindRoomShellTools(Toolkit):
        """MindRoom shell toolkit with async execution and timeout-to-handle support."""

        def __init__(
            self,
            base_dir: Path | str | None = None,
            enable_run_shell_command: bool = True,
            all: bool = False,  # noqa: A002
            extra_env_passthrough: str | None = None,
            shell_path_prepend: str | None = None,
            *,
            runtime_paths: RuntimePaths,
            **kwargs: object,
        ) -> None:
            self.base_dir: Path | None = Path(base_dir) if isinstance(base_dir, str) else base_dir

            tools: list[object] = []
            if all or enable_run_shell_command:
                tools.extend([self.run_shell_command, self.check_shell_command, self.kill_shell_command])

            super().__init__(name="shell_tools", tools=tools, **kwargs)  # ty: ignore[invalid-argument-type]
            run_shell_command_function = self.async_functions.get("run_shell_command")
            if run_shell_command_function is not None:
                effective_strict = (
                    False if run_shell_command_function.strict is None else run_shell_command_function.strict
                )
                run_shell_command_function.process_entrypoint(strict=effective_strict)

            self._runtime_env = dict(
                shell_execution_runtime_env_values(
                    runtime_paths,
                    extra_env_passthrough=extra_env_passthrough,
                    process_env=runtime_paths.process_env,
                ),
            )
            self._base_process_env = dict(runtime_paths.process_env)
            self._processes = _process_registry
            self._handle_namespace = _handle_namespace(runtime_paths=runtime_paths, base_dir=self.base_dir)
            self._shell_path_prepend = shell_path_prepend

        async def run_shell_command(
            self,
            args: Annotated[list[str], BeforeValidator(_normalize_shell_args)],
            tail: int = 100,
            timeout: int = 120,  # noqa: ASYNC109
        ) -> str:
            """Runs a shell command and returns the output or error.

            If the command completes within ``timeout`` seconds the last ``tail``
            lines of stdout are returned (or the stderr on non-zero exit).  When
            the timeout is exceeded the process keeps running in the background
            and a handle string is returned that can be polled with
            ``check_shell_command`` or stopped with ``kill_shell_command``.

            Args:
                args: The command to run as a list of strings.
                tail: The number of lines to return from the output.
                timeout: Maximum seconds to wait before backgrounding the command.

            Returns:
                The command output, an error message, or a background handle.

            """
            self._sweep_stale_records()

            try:
                subprocess_env = _shell_subprocess_env(
                    self._runtime_env,
                    base_process_env=self._base_process_env,
                    shell_path_prepend=self._shell_path_prepend,
                )
                process = await asyncio.create_subprocess_exec(
                    *_shell_subprocess_args(args, subprocess_env),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self.base_dir) if self.base_dir else None,
                    env=subprocess_env,
                    start_new_session=True,
                )
            except Exception as exc:
                return f"Error: {exc}"

            stdout_buf: deque[str] = deque(maxlen=_MAX_OUTPUT_LINES)
            stderr_buf: deque[str] = deque(maxlen=_MAX_OUTPUT_LINES)
            background_handle: str | None = None
            background_monitor_task: asyncio.Task[None] | None = None

            stdout_reader = asyncio.create_task(_read_stream(process.stdout, stdout_buf))
            stderr_reader = asyncio.create_task(_read_stream(process.stderr, stderr_buf))

            try:
                try:
                    await _await_foreground_process_exit(process, timeout_seconds=timeout)
                except TimeoutError:
                    active = sum(1 for r in self._processes.values() if not r.finished)
                    if active >= _MAX_BACKGROUNDED:
                        with contextlib.suppress(ProcessLookupError, PermissionError):
                            os.killpg(process.pid, signal.SIGKILL)
                        await _cancel_pending_tasks(stdout_reader, stderr_reader)
                        return (
                            f"Error: Too many backgrounded processes ({active}/{_MAX_BACKGROUNDED}). "
                            "Kill or wait for existing ones before running more."
                        )
                    handle = f"shell:{uuid.uuid4().hex[:8]}"
                    record = _ProcessRecord(
                        namespace=self._handle_namespace,
                        handle=handle,
                        pid=process.pid,
                        args=args,
                        process=process,
                        stdout_buf=stdout_buf,
                        stderr_buf=stderr_buf,
                        tail=tail,
                    )
                    record._monitor_task = asyncio.create_task(
                        _monitor_process(self._processes, handle, process, stdout_reader, stderr_reader),
                    )
                    self._processes[handle] = record
                    background_handle = handle
                    background_monitor_task = record._monitor_task
                    await asyncio.sleep(0)
                    return (
                        f"Command timed out after {timeout}s. Still running (PID {process.pid}).\n"
                        f"Handle: {handle}\n"
                        f"Use check_shell_command('{handle}') to poll or "
                        f"kill_shell_command('{handle}') to stop."
                    )

                await _await_reader_tasks_with_grace(
                    stdout_reader,
                    stderr_reader,
                    grace_seconds=_POST_EXIT_READER_GRACE_SECONDS,
                )
            except asyncio.CancelledError:
                if background_handle is not None:
                    self._processes.pop(background_handle, None)
                if background_monitor_task is not None:
                    background_monitor_task.cancel()
                if process.returncode is None:
                    await _terminate_process_group(process)
                await _cancel_pending_tasks(stdout_reader, stderr_reader)
                if background_monitor_task is not None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await background_monitor_task
                raise

            if process.returncode != 0:
                return f"Error: {chr(10).join(stderr_buf)}"
            return "\n".join(list(stdout_buf)[-tail:])

        def check_shell_command(self, handle: str) -> str:
            """Poll the status of a backgrounded shell command.

            Safe to call multiple times — the record is kept until automatic
            cleanup (~10 min after finish). Use ``kill_shell_command`` to stop
            a running process.

            Args:
                handle: The handle string returned by ``run_shell_command``.

            Returns:
                Output if the command finished, or a status summary if still running.

            """
            record = self._processes.get(handle)
            if record is None or record.namespace != self._handle_namespace:
                return f"Error: Unknown handle '{handle}'"

            elapsed = time.monotonic() - record.started_at

            if record.finished:
                output = "\n".join(list(record.stdout_buf)[-record.tail :])
                errors = "\n".join(record.stderr_buf)
                result = f"Status: FINISHED (exit code {record.return_code}, ran for {elapsed:.1f}s)\n"
                if record.return_code != 0 and errors:
                    result += f"Stderr:\n{errors}\n"
                result += f"Output:\n{output}"
                return result

            partial = "\n".join(list(record.stdout_buf)[-50:])
            return (
                f"Status: RUNNING (PID {record.pid}, elapsed {elapsed:.1f}s)\n"
                f"Partial output ({len(record.stdout_buf)} lines so far):\n{partial}"
            )

        def kill_shell_command(self, handle: str, force: bool = False) -> str:
            """Kill a backgrounded shell command.

            Args:
                handle: The handle string returned by ``run_shell_command``.
                force: If True send SIGKILL immediately instead of SIGTERM.

            Returns:
                Confirmation message or error.

            """
            record = self._processes.get(handle)
            if record is None or record.namespace != self._handle_namespace:
                return f"Error: Unknown handle '{handle}'"

            if record.finished:
                return f"Process already finished (exit code {record.return_code})"

            sig = signal.SIGKILL if force else signal.SIGTERM
            sig_name = "SIGKILL" if force else "SIGTERM"
            try:
                os.killpg(record.pid, sig)
            except (ProcessLookupError, PermissionError):
                return f"Process {record.pid} already exited"

            action = "Force-killed" if force else "Terminated"
            return (
                f"{action} process {record.pid} ({sig_name} sent). Use check_shell_command('{handle}') to confirm exit."
            )

        def _sweep_stale_records(self) -> None:
            """Remove records that finished more than 10 minutes ago."""
            now = time.monotonic()
            stale = [
                h
                for h, r in self._processes.items()
                if r.finished and r.finished_at is not None and (now - r.finished_at) > _STALE_RECORD_SECONDS
            ]
            for h in stale:
                self._processes.pop(h, None)

    return MindRoomShellTools


_log = get_logger(__name__)


async def _read_stream(stream: asyncio.StreamReader | None, buf: deque[str]) -> None:
    """Read lines from an async stream into *buf* until EOF."""
    if stream is None:
        return
    while True:
        try:
            line = await stream.readline()
        except ValueError:
            _log.warning("shell: oversized line exceeded StreamReader buffer limit, skipping")
            continue
        if not line:
            break
        buf.append(line.decode(errors="replace").rstrip("\n"))


async def _cancel_pending_tasks(*tasks: asyncio.Task[None]) -> None:
    """Cancel any pending tasks and wait for them to finish."""
    pending_tasks = [task for task in tasks if not task.done()]
    for task in pending_tasks:
        task.cancel()
    for task in pending_tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _await_foreground_process_exit(
    process: asyncio.subprocess.Process,
    *,
    timeout_seconds: float,
) -> None:
    """Wait for the foreground process to exit without depending on pipe EOF."""
    if process.returncode is not None:
        return
    if timeout_seconds <= 0:
        raise TimeoutError

    wait_task = asyncio.create_task(process.wait())
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    try:
        while process.returncode is None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            done_tasks, _pending_tasks = await asyncio.wait(
                (wait_task,),
                timeout=min(_PROCESS_EXIT_POLL_INTERVAL_SECONDS, remaining),
            )
            if wait_task in done_tasks:
                await wait_task
                return
    finally:
        if not wait_task.done():
            wait_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wait_task


async def _await_reader_tasks_with_grace(
    stdout_reader: asyncio.Task[None],
    stderr_reader: asyncio.Task[None],
    *,
    grace_seconds: float,
) -> None:
    """Drain reader tasks briefly after foreground exit, then stop waiting for EOF."""
    reader_tasks = (stdout_reader, stderr_reader)
    done_tasks, pending_tasks = await asyncio.wait(reader_tasks, timeout=grace_seconds)

    try:
        for task in done_tasks:
            await task
    finally:
        for task in pending_tasks:
            task.cancel()
        for task in pending_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _terminate_process_group(
    process: asyncio.subprocess.Process,
    *,
    grace_period: float = 0.5,
) -> None:
    """Terminate a subprocess process group, escalating to SIGKILL if needed."""
    if process.returncode is not None:
        return

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)

    try:
        await asyncio.wait_for(process.wait(), timeout=grace_period)
    except TimeoutError:
        pass
    else:
        return

    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=grace_period)


async def _monitor_process(
    registry: dict[str, _ProcessRecord],
    handle: str,
    process: asyncio.subprocess.Process,
    stdout_reader: asyncio.Task[None],
    stderr_reader: asyncio.Task[None],
) -> None:
    """Wait for a backgrounded process to exit and update its record."""
    try:
        await process.wait()
    finally:
        await asyncio.wait([stdout_reader, stderr_reader], timeout=2.0)
        await _cancel_pending_tasks(stdout_reader, stderr_reader)
        record = registry.get(handle)
        if record is not None:
            record.finished = True
            record.finished_at = time.monotonic()
            record.return_code = process.returncode
