"""Managed OS process lifecycle, Linux idle/systemd launch wrapping, and Bash 126/127 classification."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import functools
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from mindroom.shell._process_target import (
    ProcessScope,
    command_processes_exist,
    kill_command_processes,
    make_scope_unit_name,
    parse_probe_slice_cgroup_dir,
    signal_command,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from signal import Signals

_PROCESS_EXIT_POLL_INTERVAL_SECONDS = 0.05
_SHELL_SYSTEMD_SLICE_ENV = "MINDROOM_SHELL_SYSTEMD_SLICE"
_SYSTEMD_SCOPE_DESCRIPTION = "MindRoom shell"
_SYSTEMD_UNAVAILABLE_STATES = frozenset({"stopping", "offline"})


class _IdleLaunch(NamedTuple):
    argv: list[str]
    env: dict[str, str]
    uses_chrt: bool
    scope: ProcessScope | None = None


class _SystemdSliceLauncher(NamedTuple):
    prefix: tuple[str, ...]
    slice_name: str
    env_bin: str
    bus_env: tuple[tuple[str, str | None], ...]
    slice_cgroup_dir: Path | None = None


@functools.cache
def _idle_launcher() -> str | None:
    """Return a ``chrt`` that can start a program at ``SCHED_IDLE``, or ``None`` when there is none or it fails."""
    chrt = shutil.which("chrt")
    if chrt is None:
        return None
    chrt = str(Path(chrt).absolute())  # commands run in other directories
    # A sandbox that refuses the policy makes chrt exit without running the program; then commands start directly.
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        if subprocess.run([chrt, "-i", "0", chrt, "-m"], capture_output=True, timeout=5, check=False).returncode == 0:
            return chrt
    return None


@functools.cache
def _idle_io_launcher() -> str | None:
    """Return an ``ionice`` that can start a program in the idle I/O class, or ``None`` when unavailable."""
    ionice = shutil.which("ionice")
    if ionice is None:
        return None
    ionice = str(Path(ionice).absolute())  # commands run in other directories
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        if subprocess.run([ionice, "-c", "3", ionice], capture_output=True, timeout=5, check=False).returncode == 0:
            return ionice
    return None


def _systemd_bus_env(runtime_dir: str | None, dbus_addr: str | None) -> dict[str, str]:
    """Return the minimal bus environment passed to user-systemd probe commands."""
    return {
        key: value
        for key, value in (("XDG_RUNTIME_DIR", runtime_dir), ("DBUS_SESSION_BUS_ADDRESS", dbus_addr))
        if value is not None
    }


def _user_manager_generation(runtime_dir: str | None, dbus_addr: str | None) -> int | None:
    """Return the active ``systemd --user`` monotonic startup timestamp, or ``None`` if unavailable."""
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return None
    systemctl = str(Path(systemctl).absolute())  # commands run in other directories
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        result = subprocess.run(
            [systemctl, "--user", "show", "--property=UserspaceTimestampMonotonic", "--property=SystemState"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=_systemd_bus_env(runtime_dir, dbus_addr),
        )
        if result.returncode != 0 or not result.stdout:
            return None
        props = {
            key: value.strip()
            for line in result.stdout.splitlines()
            for key, sep, value in (line.partition("="),)
            if sep
        }
        if props.get("SystemState") in _SYSTEMD_UNAVAILABLE_STATES:
            return None
        raw_generation = props.get("UserspaceTimestampMonotonic", "")
        if raw_generation.isascii() and raw_generation.isdigit() and (generation := int(raw_generation)) > 0:
            return generation
    return None


@functools.lru_cache(maxsize=4)
def _probe_systemd_slice_launcher(
    slice_name: str,
    runtime_dir: str | None,
    dbus_addr: str | None,
    generation: int,
) -> _SystemdSliceLauncher | None:
    """Return a ``systemd-run --user --scope`` launcher when probing *slice_name* succeeds, else ``None``."""
    del generation  # cache key only; re-probes when the user manager restarts
    systemd_run = shutil.which("systemd-run")
    env_bin = shutil.which("env")
    if systemd_run is None or env_bin is None:
        return None
    systemd_run = str(Path(systemd_run).absolute())  # commands run in other directories
    env_bin = str(Path(env_bin).absolute())
    cat_bin = shutil.which("cat")
    probe_payload = (
        [str(Path(cat_bin).absolute()), "/proc/self/cgroup"] if cat_bin is not None else [systemd_run, "--version"]
    )
    probe_unit = make_scope_unit_name(probe=True)
    # --description hides raw argv from the systemd user journal;
    # --expand-environment=no stops systemd 262+ from expanding $$ / $VAR in argv (older versions fail open here);
    # OOMPolicy=kill sets memory.oom.group=1 on each transient .scope so a slice-level MemoryMax OOM cleanly
    # terminates the whole offending command scope instead of picking off single workers one at a time.
    prefix = (
        systemd_run,
        "--user",
        "--scope",
        "--quiet",
        f"--description={_SYSTEMD_SCOPE_DESCRIPTION}",
        "--expand-environment=no",
        f"--slice={slice_name}",
        "--property=OOMPolicy=kill",
    )
    bus_env: tuple[tuple[str, str | None], ...] = (
        ("XDG_RUNTIME_DIR", runtime_dir),
        ("DBUS_SESSION_BUS_ADDRESS", dbus_addr),
    )
    try:
        probe = subprocess.run(
            [
                *prefix,
                f"--unit={probe_unit}",
                "--",
                env_bin,
                "-u",
                "MINDROOM_PROBE_UNSET",
                "--",
                "MINDROOM_PROBE_SET=1",
                *probe_payload,
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            check=False,
            env=_systemd_bus_env(runtime_dir, dbus_addr),
        )
    except subprocess.TimeoutExpired:
        # Let transient probe timeouts escape lru_cache so the next call re-probes instead of caching None.
        raise
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode == 0:
        slice_cgroup_dir = (
            parse_probe_slice_cgroup_dir(probe.stdout or "", probe_unit=probe_unit, slice_name=slice_name)
            if cat_bin is not None
            else None
        )
        return _SystemdSliceLauncher(
            prefix=prefix,
            slice_name=slice_name,
            env_bin=env_bin,
            bus_env=bus_env,
            slice_cgroup_dir=slice_cgroup_dir,
        )
    return None


def _is_valid_systemd_slice_name(slice_name: str) -> bool:
    return (
        len(slice_name) > len(".slice")
        and slice_name.endswith(".slice")
        and not slice_name.startswith("-")
        and "/" not in slice_name
        and not any(ch.isspace() or ch == "\x00" for ch in slice_name)
    )


def _systemd_slice_launcher() -> _SystemdSliceLauncher | None:
    """Return a ``systemd-run --user --scope`` launcher for ``MINDROOM_SHELL_SYSTEMD_SLICE``, or ``None``."""
    slice_name = os.environ.get(_SHELL_SYSTEMD_SLICE_ENV, "").strip()
    if not _is_valid_systemd_slice_name(slice_name):
        return None
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or None
    dbus_addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS") or None
    generation = _user_manager_generation(runtime_dir, dbus_addr)
    if generation is None:
        _probe_systemd_slice_launcher.cache_clear()
        return None
    try:
        return _probe_systemd_slice_launcher(slice_name, runtime_dir, dbus_addr, generation)
    except subprocess.TimeoutExpired:
        return None


def _find_program(program: str, env: dict[str, str], cwd: str | None, mode: int = os.F_OK | os.X_OK) -> str | None:
    """Like Popen, look a bare name up on the command's PATH and a path with a slash up, both from the command's directory."""
    program = str(Path(cwd, program)) if cwd and os.sep in program else program
    search = [str(Path(cwd, entry)) if cwd else entry for entry in os.get_exec_path(env)]
    return shutil.which(program, mode=mode, path=os.pathsep.join(search))


# Bash's exit code and words for the spawn errors that are a shell's own exit: of a program named by a path, of a bare
# name no PATH entry holds, and of a bare name found on PATH. Every other error stays a failed call.
_PATH_LAUNCH_FAILURES = {
    errno.ENOENT: (127, "No such file or directory"),
    errno.ENOTDIR: (126, "Not a directory"),
    errno.EACCES: (126, "Permission denied"),
}
_MISSING_NAME_LAUNCH_FAILURES = dict.fromkeys((errno.ENOENT, errno.ENOTDIR), (127, "command not found"))
_FOUND_NAME_LAUNCH_FAILURES = {
    # A file the program needs to start, such as its interpreter, is missing.
    errno.ENOENT: (127, "cannot execute: required file not found"),
    errno.ENOTDIR: (127, "cannot execute: required file not found"),
    errno.EACCES: (126, "Permission denied"),
}


def classify_launch_failure(
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
    exc: Exception,
) -> tuple[int, str] | None:
    """Return Bash's exit code and words for a spawn error of a program that is missing or cannot be executed, else None.

    A working directory the command cannot enter fails before any program. The error number and the program's name
    as given decide alone: the program file is never opened.
    """
    if (
        not isinstance(exc, OSError)
        or not argv
        # uvloop, which sandbox runners serve on, names no file in a spawn error.
        or exc.filename not in (argv[0], None)
        or (cwd is not None and not (Path(cwd).is_dir() and os.access(cwd, os.X_OK)))
    ):
        return None
    program = argv[0]
    if os.sep in program:
        failures = _PATH_LAUNCH_FAILURES
    elif (found := _find_program(program, env, cwd, os.F_OK)) is None:
        failures = _MISSING_NAME_LAUNCH_FAILURES
    else:
        # Bash names a program it found on PATH by where it found it.
        program, failures = found, _FOUND_NAME_LAUNCH_FAILURES
    failure = failures.get(exc.errno or 0)
    return None if failure is None else (failure[0], f"{program}: {failure[1]}")


def _idle_launch_argv(argv: list[str], env: dict[str, str], cwd: str | None) -> _IdleLaunch:
    """Wrap *argv* on Linux for optional systemd slice, ``SCHED_IDLE``, and idle I/O priority.

    Nice 19 lowers a command's CPU share, but the kernel still counts a CPU that runs it as busy when it places
    MindRoom's waking event loop. It counts a CPU that runs only idle-policy threads as free, and a waking normal thread
    preempts them at once.
    All wrappers exec the program in the same process. A program that cannot be found starts directly instead, so the
    spawn fails with the same error as before; unavailable wrappers are skipped individually.
    """
    if sys.platform != "linux" or not argv or _find_program(argv[0], env, cwd) is None:
        return _IdleLaunch(argv, env, uses_chrt=False)

    spawn_argv = argv
    spawn_env = env
    ionice = _idle_io_launcher()
    chrt = _idle_launcher()
    systemd_launcher = _systemd_slice_launcher()
    if ionice is not None:
        spawn_argv = [ionice, "-c", "3", "--", *spawn_argv]
    if chrt is not None:
        spawn_argv = [chrt, "-i", "0", *spawn_argv]
    scope: ProcessScope | None = None
    if systemd_launcher is not None:
        # systemd-run needs the verified backend user bus, while the payload must keep the caller's exact
        # values and omissions for those bus variables.
        spawn_env = dict(env)
        unset_ops: list[str] = []
        set_ops: list[str] = []
        for key, bus_value in systemd_launcher.bus_env:
            if bus_value is None:
                spawn_env.pop(key, None)
            else:
                spawn_env[key] = bus_value
            if (key in spawn_env) != (key in env) or spawn_env.get(key) != env.get(key):
                if key in env:
                    set_ops.append(f"{key}={env[key]}")
                else:
                    unset_ops.extend(["-u", key])
        if unset_ops or set_ops:
            spawn_argv = [systemd_launcher.env_bin, *unset_ops, "--", *set_ops, *spawn_argv]
        unit_name = make_scope_unit_name()
        scope = ProcessScope(
            unit_name=unit_name,
            slice_name=systemd_launcher.slice_name,
            slice_cgroup_dir=systemd_launcher.slice_cgroup_dir,
        )
        spawn_argv = [*systemd_launcher.prefix, f"--unit={unit_name}", "--", *spawn_argv]
    return _IdleLaunch(spawn_argv, spawn_env, uses_chrt=chrt is not None, scope=scope)


def _schedule_process_tree_idle(pid: int) -> None:
    """Move every thread of ``pid`` and of its descendants to ``SCHED_IDLE``; what they start later inherits it.

    This is the fallback for a command that did not start behind chrt (see ``_idle_launch_argv``).
    A busy event loop gives the command time to fork and start threads before this runs, so the walk sets each thread
    before it reads that thread's children: whatever the thread starts afterwards inherits the policy. It misses a
    thread started during the walk by a thread it has not reached yet, and a process whose parent already exited.
    """
    if sys.platform != "linux":
        return
    pending = [pid]
    while pending:
        with contextlib.suppress(OSError):
            for thread in Path(f"/proc/{pending.pop()}/task").iterdir():
                with contextlib.suppress(OSError):
                    os.sched_setscheduler(int(thread.name), os.SCHED_IDLE, os.sched_param(0))
                    pending.extend(int(child) for child in (thread / "children").read_text().split())


class ManagedProcess:
    """Owns one spawned OS subprocess together with its optional verified systemd scope."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        scope: ProcessScope | None = None,
    ) -> None:
        self.process = process
        self.scope = scope

    @classmethod
    async def spawn(
        cls,
        argv: list[str],
        env: dict[str, str],
        cwd: str | None,
        *,
        stdin: int | None,
        stderr: int = asyncio.subprocess.PIPE,
    ) -> ManagedProcess:
        """Start one command at idle priority as the leader of its own session, with its stdout on a pipe."""
        launch = await asyncio.to_thread(_idle_launch_argv, argv, env, cwd)
        process = await asyncio.create_subprocess_exec(
            *launch.argv,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
            cwd=cwd,
            env=launch.env,
            start_new_session=True,
        )
        # A busy command, such as a build, must not take the CPU from the process that runs MindRoom; processes the
        # command starts later inherit the priority.
        with contextlib.suppress(OSError):
            os.setpriority(os.PRIO_PGRP, process.pid, 19)
        if sys.platform == "linux" and not launch.uses_chrt:
            _schedule_process_tree_idle(process.pid)
        return cls(process, launch.scope)

    @property
    def pid(self) -> int:
        """Return the spawned leader PID."""
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        """Return the process exit code once exited, else ``None``."""
        return self.process.returncode

    @property
    def stdin(self) -> asyncio.StreamWriter | None:
        """Return the subprocess stdin writer when spawned with a stdin pipe."""
        return self.process.stdin

    @property
    def stdout(self) -> asyncio.StreamReader | None:
        """Return the subprocess stdout reader."""
        return self.process.stdout

    @property
    def stderr(self) -> asyncio.StreamReader | None:
        """Return the subprocess stderr reader when spawned with a stderr pipe."""
        return self.process.stderr

    async def wait_exit(
        self,
        timeout_seconds: float | None = None,
        on_poll: Callable[[], None] | None = None,
    ) -> None:
        """Wait for the process to exit without depending on inherited pipe EOF, calling ``on_poll`` while it runs."""
        if self.returncode is not None:
            return
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise TimeoutError

        wait_task = asyncio.create_task(self.process.wait())
        deadline = None if timeout_seconds is None else asyncio.get_running_loop().time() + timeout_seconds
        try:
            while self.returncode is None:
                poll_seconds = _PROCESS_EXIT_POLL_INTERVAL_SECONDS
                if deadline is not None:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError
                    poll_seconds = min(poll_seconds, remaining)
                done_tasks, _pending_tasks = await asyncio.wait(
                    (wait_task,),
                    timeout=poll_seconds,
                )
                if wait_task in done_tasks:
                    await wait_task
                    return
                if on_poll is not None:
                    on_poll()
        finally:
            if not wait_task.done():
                wait_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await wait_task

    def signal(self, sig: Signals) -> bool:
        """Send *sig* once to this process's group and any extra Linux session/scope members."""
        return signal_command(self.pid, sig, scope=self.scope)

    def kill_tree(self) -> None:
        """Force-kill this process's group, Linux session members, and verified systemd scope."""
        kill_command_processes(self.pid, scope=self.scope)

    def _processes_exist(self) -> bool:
        """Return whether any process of this command still exists in its group, Linux session, or verified scope."""
        return command_processes_exist(self.pid, scope=self.scope)

    async def terminate_tree(
        self,
        *,
        grace_period: float = 0.5,
        whole_group: bool = False,
    ) -> bool:
        """Stop a running command: SIGTERM, then SIGKILL every process left after the grace.

        Members that outlive the leader are killed too, so a child that ignores SIGTERM cannot survive the stop.
        A command whose leader already exited ended on its own, and its detached children keep running, unless
        ``whole_group`` stops them too; return whether all command processes are then gone.
        """
        if self.returncode is not None and not whole_group:
            return False

        self.signal(signal.SIGTERM)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace_period
        try:
            while loop.time() < deadline and self._processes_exist():  # noqa: ASYNC110 - a process tree has no exit event
                await asyncio.sleep(_PROCESS_EXIT_POLL_INTERVAL_SECONDS)
        finally:
            # A stop cancelled during the grace still kills what ignored SIGTERM.
            self.kill_tree()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.process.wait(), timeout=grace_period)
        deadline = loop.time() + grace_period
        while whole_group and self._processes_exist() and loop.time() < deadline:  # noqa: ASYNC110 - as above
            await asyncio.sleep(_PROCESS_EXIT_POLL_INTERVAL_SECONDS)
        return not self._processes_exist()
