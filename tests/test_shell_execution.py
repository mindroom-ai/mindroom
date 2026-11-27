"""Optional shell engine seams used by the Desktop shell, with the agent shell tool defaults unchanged."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mindroom.shell.execution import (
    ProcessRecord,
    _idle_io_launcher,
    _idle_launch_argv,
    _idle_launcher,
    _probe_systemd_slice_launcher,
    _schedule_process_tree_idle,
    _systemd_slice_launcher,
    _SystemdSliceLauncher,
    check_command,
    configured_systemd_slice_name,
    kill_all_records,
    kill_command,
    run_command,
    signal_record,
)
from mindroom.shell.output_capture import ShellOutputCapture, ShellOutputDestination

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_FD0_IDENTITY = "import os, sys; info = os.fstat(0); sys.stdout.write(f'{info.st_dev}:{info.st_ino}')"


class _RecordingCapture(ShellOutputCapture):
    """Keep the spool readable after the engine reports completion."""

    def __init__(self, directory: Path) -> None:
        super().__init__(ShellOutputDestination(workspace_root=str(directory), path="", max_bytes=1024))
        self.return_codes: list[int | None] = []

    def publish(self, return_code: int | None) -> str:
        self.return_codes.append(return_code)
        return "published"

    def close(self) -> None:
        """Defer release until the test has read the spool."""

    def release(self) -> None:
        super().close()


@pytest.fixture
def registry() -> Iterator[dict[str, ProcessRecord]]:
    """Kill anything a failing test leaves registered."""
    records: dict[str, ProcessRecord] = {}
    yield records
    kill_all_records(records)


async def _run(registry: dict[str, ProcessRecord], argv: list[str], cwd: Path, **options: object) -> str:
    result = await run_command(
        registry,
        namespace="test",
        argv=argv,
        env={"PATH": os.environ["PATH"]},
        cwd=str(cwd),
        tail=100,
        timeout=10,
        **options,
    )
    assert result.handle is None
    return result.message


@pytest.mark.asyncio
async def test_a_finished_handle_is_swept_ten_minutes_after_it_finished_however_long_it_ran(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
) -> None:
    """Desktop shell and script handles stay readable for ten minutes after their command ends."""
    finished = await run_command(
        registry,
        namespace="test",
        argv=["echo", "long-output"],
        env={"PATH": os.environ["PATH"]},
        cwd=str(tmp_path),
        tail=100,
        timeout=10,
        register_finished=True,
    )
    assert finished.handle is not None
    record = registry[finished.handle]
    # It started long ago but finished just now.
    record.started_at -= 900

    await _run(registry, ["true"], tmp_path)
    assert "long-output" in check_command(registry, namespace="test", handle=finished.handle)

    assert record.finished_at is not None
    record.finished_at -= 700
    await _run(registry, ["true"], tmp_path)
    assert finished.handle not in registry


async def _wait_until_gone(pid: int) -> bool:
    for _ in range(200):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.01)
    return False


def _identity(path: str) -> str:
    info = Path(path).stat()
    return f"{info.st_dev}:{info.st_ino}"


@pytest.mark.asyncio
async def test_default_stdin_is_inherited_and_devnull_seam_detaches_it(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
) -> None:
    """The agent tool keeps inheriting stdin; the Desktop helper can keep its private channel away from commands."""
    read_end, write_end = os.pipe()
    saved_stdin = os.dup(0)
    try:
        os.dup2(read_end, 0)
        pipe_identity = f"{os.fstat(0).st_dev}:{os.fstat(0).st_ino}"
        inherited = await _run(registry, [sys.executable, "-c", _FD0_IDENTITY], tmp_path)
        detached = await _run(
            registry,
            [sys.executable, "-c", _FD0_IDENTITY],
            tmp_path,
            stdin=asyncio.subprocess.DEVNULL,
        )
    finally:
        os.dup2(saved_stdin, 0)
        for descriptor in (saved_stdin, read_end, write_end):
            os.close(descriptor)
    assert inherited == pipe_identity
    assert detached == _identity(os.devnull)


@pytest.mark.asyncio
async def test_default_streams_stay_separate_and_merge_seam_interleaves(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
) -> None:
    """Merged stderr keeps terminal order in one stream; by default a success's stderr follows its stdout."""
    argv = ["/bin/sh", "-c", "echo first; echo second >&2; echo third"]
    assert await _run(registry, argv, tmp_path) == "first\nthird\nStderr:\nsecond"
    assert await _run(registry, argv, tmp_path, stderr=asyncio.subprocess.STDOUT) == "first\nsecond\nthird"


@pytest.mark.asyncio
async def test_default_foreground_exit_keeps_background_children_and_seam_kills_them(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
) -> None:
    """The agent tool may leave `cmd &` running; the seam stops the rest of the process group."""
    argv = ["/bin/sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $! > child.pid"]
    await _run(registry, argv, tmp_path)
    survivor = int((tmp_path / "child.pid").read_text())
    try:
        os.kill(survivor, 0)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(survivor, signal.SIGKILL)

    await _run(registry, argv, tmp_path, kill_group_after_exit=True)
    killed = int((tmp_path / "child.pid").read_text())
    try:
        assert await _wait_until_gone(killed)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(killed, signal.SIGKILL)


@pytest.mark.asyncio
@pytest.mark.parametrize("kill_group_after_exit", [False, True])
async def test_cancelling_a_running_command_kills_its_term_resistant_group_members(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
    kill_group_after_exit: bool,
) -> None:
    """Cancelling a command that still runs stops its whole process group, including members that ignore SIGTERM."""
    script = (
        "import os, pathlib, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path('child.pid').write_text(str(os.getpid())); time.sleep(30)"
    )
    argv = ["/bin/sh", "-c", f"{shlex.quote(sys.executable)} -c {shlex.quote(script)} >/dev/null 2>&1 & wait"]
    task = asyncio.create_task(_run(registry, argv, tmp_path, kill_group_after_exit=kill_group_after_exit))
    pid_file = tmp_path / "child.pid"
    try:
        for _ in range(400):
            if pid_file.exists() and pid_file.read_text():
                break
            await asyncio.sleep(0.01)
        child = int(pid_file.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _wait_until_gone(child)
    finally:
        if pid_file.exists() and pid_file.read_text():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)


@pytest.mark.asyncio
async def test_caller_capture_receives_exit_code_and_full_spool(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
) -> None:
    """A caller-owned capture sees completion without any workspace output file being written."""
    capture = _RecordingCapture(tmp_path)
    try:
        message = await _run(
            registry,
            ["/bin/sh", "-c", "printf 'kept output'; exit 3"],
            tmp_path,
            output_capture=capture,
        )
        assert message == "published"
        assert capture.return_codes == [3]
        assert capture.stdout.read() == "kept output"
        assert list(tmp_path.iterdir()) == []
    finally:
        capture.release()


@pytest.mark.asyncio
async def test_signal_record_reports_delivery_and_kill_command_names_the_signal(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers learn whether a signal reached the group, and kill_command says which signal it sent."""
    capture = _RecordingCapture(tmp_path)
    try:
        started = await run_command(
            registry,
            namespace="test",
            argv=["/bin/sh", "-c", "sleep 30"],
            env={"PATH": os.environ["PATH"]},
            cwd=str(tmp_path),
            tail=100,
            timeout=0.2,
            output_capture=capture,
        )
        assert started.handle is not None
        record = registry[started.handle]

        def vanished(_pid: int, _signal: int) -> None:
            raise ProcessLookupError

        with monkeypatch.context() as patch:
            patch.setattr(os, "killpg", vanished)
            assert signal_record(record) is False
            assert kill_command(registry, namespace="test", handle=started.handle) == (
                f"Process {record.pid} already exited"
            )
        assert capture.incomplete is False
        assert kill_command(registry, namespace="test", handle=started.handle) == (
            f"Terminated process {record.pid} (SIGTERM sent)."
        )
        assert capture.incomplete is True
        assert await _wait_until_gone(record.pid)
    finally:
        capture.release()


@pytest.mark.asyncio
async def test_checking_a_finished_success_shows_its_stderr_after_its_output(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
) -> None:
    """A finished background success shows its stderr, tailed like its stdout, after the status and output."""
    result = await run_command(
        registry,
        namespace="test",
        argv=["/bin/sh", "-c", "echo out; echo warn >&2; echo again >&2"],
        env={"PATH": os.environ["PATH"]},
        cwd=str(tmp_path),
        tail=1,
        timeout=10,
        register_finished=True,
    )
    assert result.handle is not None

    status, rest = check_command(registry, namespace="test", handle=result.handle).split("\n", 1)
    assert status.startswith("Status: FINISHED (exit code 0, ")
    assert rest == (
        "Output:\nout\nStderr:\n[Showing the last 1 of 2 lines. Pass a larger tail, or narrow the command.]\nagain"
    )


@pytest.mark.asyncio
async def test_checking_a_running_command_counts_its_blank_lines_as_its_notice_does(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
) -> None:
    """The buffered line count of a running command and the notice above its tail count blank lines alike."""
    result = await run_command(
        registry,
        namespace="test",
        argv=["/bin/sh", "-c", "echo text; yes '' | head -n 60; sleep 30"],
        env={"PATH": os.environ["PATH"]},
        cwd=str(tmp_path),
        tail=100,
        timeout=1,
    )
    assert result.handle is not None

    status, rest = check_command(registry, namespace="test", handle=result.handle).split("\n", 1)
    assert status.startswith("Status: RUNNING (PID ")
    assert rest == (
        "Partial output (61 lines buffered):\n"
        "[Showing the last 50 of 61 lines. Pass a larger tail, or narrow the command.]\n" + "\n" * 49
    )


@pytest.mark.asyncio
async def test_register_finished_keeps_a_command_that_finished_in_time_as_a_finished_record(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
) -> None:
    """An opted-in caller gets a finished record to page later, while the default still registers nothing."""
    capture = _RecordingCapture(tmp_path)
    try:
        result = await run_command(
            registry,
            namespace="test",
            argv=["/bin/sh", "-c", "printf kept; exit 3"],
            env={"PATH": os.environ["PATH"]},
            cwd=str(tmp_path),
            tail=100,
            timeout=10,
            output_capture=capture,
            register_finished=True,
        )
        assert result.handle is not None
        record = registry[result.handle]
        assert (record.finished, record.return_code, record.namespace) == (True, 3, "test")
        assert record.finished_at is not None
        assert (capture.return_codes, capture.stdout.read()) == ([3], "kept")
        assert kill_command(registry, namespace="test", handle=result.handle) == (
            "Process already finished (exit code 3)"
        )
        await _run(registry, ["/bin/sh", "-c", "true"], tmp_path)
        assert list(registry) == [result.handle]
    finally:
        capture.release()


_TERM_IGNORING_CHILD = (
    "import os, pathlib, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "pathlib.Path('child.pid').write_text(str(os.getpid())); time.sleep(30)"
)


@pytest.mark.asyncio
async def test_a_command_past_its_timeout_is_stopped_with_its_output_so_far(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
) -> None:
    """A stopped command returns what it printed on both streams, says it was stopped, and leaves no handle."""
    result = await run_command(
        registry,
        namespace="test",
        argv=["/bin/sh", "-c", "echo started; echo warming up >&2; sleep 30"],
        env={"PATH": os.environ["PATH"]},
        cwd=str(tmp_path),
        tail=100,
        timeout=0.5,
        on_timeout="kill",
    )

    assert result.handle is None
    assert result.message == "Stopped after 0.5s.\nOutput:\nstarted\nStderr:\nwarming up"
    assert registry == {}


@pytest.mark.asyncio
async def test_stopping_a_command_kills_a_child_that_ignores_sigterm_after_its_leader_exited(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
) -> None:
    """The leader dies of SIGTERM at the stop; a group member that ignores it is killed anyway."""
    child_command = f"{shlex.quote(sys.executable)} -c {shlex.quote(_TERM_IGNORING_CHILD)} >/dev/null 2>&1 &"
    pid_file = tmp_path / "child.pid"
    try:
        result = await run_command(
            registry,
            namespace="test",
            argv=["/bin/sh", "-c", f"{child_command} sleep 30"],
            env={"PATH": os.environ["PATH"]},
            cwd=str(tmp_path),
            tail=100,
            timeout=1,
            on_timeout="kill",
        )

        assert result.message.startswith("Stopped after 1s.")
        assert await _wait_until_gone(int(pid_file.read_text()))
    finally:
        if pid_file.exists() and pid_file.read_text():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)


@pytest.mark.asyncio
async def test_a_command_without_a_timeout_runs_until_it_ends(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
) -> None:
    """No timeout means no limit, for a command that would be stopped at any short one."""
    result = await run_command(
        registry,
        namespace="test",
        argv=["/bin/sh", "-c", "sleep 0.3; echo done"],
        env={"PATH": os.environ["PATH"]},
        cwd=str(tmp_path),
        tail=100,
        timeout=None,
        on_timeout="kill",
    )

    assert result.message == "done"


@pytest.mark.asyncio
async def test_a_command_and_the_children_it_starts_run_at_the_lowest_cpu_priority(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
) -> None:
    """A busy build an agent starts must not take the CPU from the process that runs MindRoom."""
    report = "import os, sys; print(os.nice(0), sys.platform != 'linux' or os.sched_getscheduler(0) == os.SCHED_IDLE)"
    child = shlex.join([sys.executable, "-c", report])

    message = await _run(registry, ["/bin/sh", "-c", f"sleep 0.2; {child}"], tmp_path)

    assert message == "19 True"


def _exited(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True


@pytest.mark.skipif(sys.platform != "linux", reason="SCHED_IDLE is Linux-only")
@pytest.mark.asyncio
async def test_a_process_the_command_leaves_behind_before_its_policy_could_be_set_runs_at_the_idle_policy(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy event loop can let a shell background a build and exit before MindRoom gets to the command."""
    _idle_launcher.cache_clear()  # another test may have run the first command with a PATH that has no chrt
    if _idle_launcher() is None:
        pytest.skip("needs a chrt that can set SCHED_IDLE; without one only the walk after spawn runs")
    original_spawn = asyncio.create_subprocess_exec

    async def spawn_then_let_the_shell_exit(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
        process = await original_spawn(*args, **kwargs)
        deadline = time.monotonic() + 10
        while not _exited(process.pid):
            assert time.monotonic() < deadline, "the shell never exited"
            await asyncio.sleep(0.01)
        return process

    monkeypatch.setattr("mindroom.shell.execution.asyncio.create_subprocess_exec", spawn_then_let_the_shell_exit)
    orphan = int(await _run(registry, ["/bin/sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $!"], tmp_path))
    try:
        assert os.sched_getscheduler(orphan) == os.SCHED_IDLE
    finally:
        os.kill(orphan, signal.SIGKILL)


@pytest.mark.skipif(sys.platform != "linux", reason="SCHED_IDLE is Linux-only")
@pytest.mark.asyncio
async def test_a_missing_program_reports_the_same_error_as_a_direct_spawn(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
) -> None:
    """Starting commands behind chrt must not turn a missing program into a command that exits 127."""
    with pytest.raises(FileNotFoundError) as direct:
        await asyncio.create_subprocess_exec("./no-such-program", cwd=tmp_path)

    assert await _run(registry, ["./no-such-program"], tmp_path) == f"Error: {direct.value}"


@pytest.mark.skipif(sys.platform != "linux", reason="SCHED_IDLE is Linux-only")
def test_a_relative_path_entry_is_searched_from_the_command_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Popen finds a program on a relative PATH entry from the command's directory, so the lookup must too."""
    for directory in ("workspace/scripts/only-in-workspace", "mindroom/scripts/only-in-mindroom"):
        program = tmp_path / directory
        program.parent.mkdir(parents=True)
        program.write_text("#!/bin/sh\n")
        program.chmod(0o755)
    monkeypatch.chdir(tmp_path / "mindroom")
    monkeypatch.setattr("mindroom.shell.execution._idle_launcher", lambda: "/chrt")
    monkeypatch.setattr("mindroom.shell.execution._idle_io_launcher", lambda: None)
    monkeypatch.delenv("MINDROOM_SHELL_SYSTEMD_SLICE", raising=False)
    env = {"PATH": "scripts"}
    workspace = str(tmp_path / "workspace")

    assert _idle_launch_argv(["only-in-workspace"], env, workspace).argv == ["/chrt", "-i", "0", "only-in-workspace"]
    assert _idle_launch_argv(["only-in-mindroom"], env, workspace).argv == ["only-in-mindroom"]


@pytest.mark.skipif(sys.platform != "linux", reason="ionice is Linux-only")
@pytest.mark.asyncio
async def test_a_command_and_its_children_run_with_idle_io_priority(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
) -> None:
    """Spawned commands and their children inherit idle disk I/O priority when ionice is available."""
    _idle_io_launcher.cache_clear()
    if (ionice := _idle_io_launcher()) is None:
        pytest.skip("needs an ionice binary that can set idle I/O priority")

    message = await _run(registry, ["/bin/sh", "-c", f"sleep 0.05; {shlex.quote(ionice)}"], tmp_path)

    assert message.startswith("idle")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux resource wrappers are Linux-only")
def test_systemd_slice_wraps_before_chrt_and_ionice_and_preserves_caller_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Systemd slice uses the backend bus for systemd-run, restores caller bus vars/omissions, and skips missing commands."""
    program = tmp_path / "run-me"
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    prefix = (
        "/systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--description=MindRoom shell",
        "--expand-environment=no",
        "--slice=s.slice",
        "--",
    )
    xdg, bus = "/run/user/42", "unix:path=/run/user/42/bus"
    launcher = _SystemdSliceLauncher(
        prefix=prefix,
        env_bin="/env",
        bus_env=(("XDG_RUNTIME_DIR", xdg), ("DBUS_SESSION_BUS_ADDRESS", bus)),
    )
    monkeypatch.setattr("mindroom.shell.execution._idle_launcher", lambda: "/chrt")
    monkeypatch.setattr("mindroom.shell.execution._idle_io_launcher", lambda: "/ionice")
    monkeypatch.setattr("mindroom.shell.execution._systemd_slice_launcher", lambda: launcher)

    caller_env = {"PATH": str(tmp_path)}
    argv = ["run-me", "$$", "$VAR"]
    launch = _idle_launch_argv(argv, caller_env, str(tmp_path))

    assert launch.argv == [
        *prefix,
        "/env",
        "-u",
        "XDG_RUNTIME_DIR",
        "-u",
        "DBUS_SESSION_BUS_ADDRESS",
        "--",
        "/chrt",
        "-i",
        "0",
        "/ionice",
        "-c",
        "3",
        "--",
        *argv,
    ]
    assert launch.uses_chrt is True
    assert launch.env == {"PATH": str(tmp_path), "XDG_RUNTIME_DIR": xdg, "DBUS_SESSION_BUS_ADDRESS": bus}
    assert caller_env == {"PATH": str(tmp_path)}

    custom = _idle_launch_argv(argv, {**caller_env, "XDG_RUNTIME_DIR": "/custom"}, str(tmp_path))
    assert custom.env == {"PATH": str(tmp_path), "XDG_RUNTIME_DIR": xdg, "DBUS_SESSION_BUS_ADDRESS": bus}
    assert custom.argv == [
        *prefix,
        "/env",
        "-u",
        "DBUS_SESSION_BUS_ADDRESS",
        "--",
        "XDG_RUNTIME_DIR=/custom",
        "/chrt",
        "-i",
        "0",
        "/ionice",
        "-c",
        "3",
        "--",
        *argv,
    ]

    exact = _idle_launch_argv(
        argv,
        {**caller_env, "XDG_RUNTIME_DIR": xdg, "DBUS_SESSION_BUS_ADDRESS": bus},
        str(tmp_path),
    )
    assert exact.argv == [*prefix, "/chrt", "-i", "0", "/ionice", "-c", "3", "--", *argv]

    missing = ["./no-such-program"]
    missing_launch = _idle_launch_argv(missing, caller_env, str(tmp_path))
    assert missing_launch == (missing, caller_env, False)
    assert missing_launch.argv is missing
    assert missing_launch.env is caller_env

    monkeypatch.setattr("mindroom.shell.execution._idle_launcher", lambda: None)
    no_chrt = _idle_launch_argv(argv, caller_env, str(tmp_path))
    assert no_chrt.argv == [
        *prefix,
        "/env",
        "-u",
        "XDG_RUNTIME_DIR",
        "-u",
        "DBUS_SESSION_BUS_ADDRESS",
        "--",
        "/ionice",
        "-c",
        "3",
        "--",
        *argv,
    ]
    assert no_chrt.uses_chrt is False


def _stub_systemd_tools(
    monkeypatch: pytest.MonkeyPatch,
    show_response: Callable[[], tuple[int, str]],
    probe_rc: Callable[[], int] = lambda: 0,
) -> tuple[list[tuple[list[str], dict[str, str]]], list[tuple[list[str], dict[str, str]]]]:
    real_which = shutil.which
    bins = {"systemctl": "/systemctl", "systemd-run": "/systemd-run", "env": "/env"}

    def fake_which(name: str, *args: object, **kwargs: object) -> str | None:
        return bins[name] if name in bins else real_which(name, *args, **kwargs)  # type: ignore[arg-type]

    show_calls: list[tuple[list[str], dict[str, str]]] = []
    probe_calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[object]:
        run_env = kwargs["env"]
        assert isinstance(run_env, dict)
        if cmd[0] == "/systemctl":
            show_calls.append((cmd, dict(run_env)))
            rc, stdout = show_response()
            return subprocess.CompletedProcess(cmd, rc, stdout=stdout)
        probe_calls.append((cmd, dict(run_env)))
        return subprocess.CompletedProcess(cmd, probe_rc())

    monkeypatch.setattr("mindroom.shell.execution.shutil.which", fake_which)
    monkeypatch.setattr("mindroom.shell.execution.subprocess.run", fake_run)
    return show_calls, probe_calls


@pytest.mark.skipif(sys.platform != "linux", reason="Linux resource wrappers are Linux-only")
def test_systemd_slice_probe_caches_by_bus_env_and_fails_open_on_invalid_or_rejected_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe checks user-manager generation, keys cache on bus env, and fails open on errors."""
    probe_rc = 0
    show_calls, probe_calls = _stub_systemd_tools(
        monkeypatch,
        lambda: (0, "UserspaceTimestampMonotonic=100\nSystemState=running\n"),
        lambda: probe_rc,
    )
    _probe_systemd_slice_launcher.cache_clear()
    try:
        xdg, bus = "/run/user/42", "unix:path=/run/user/42/bus"
        monkeypatch.setenv("MINDROOM_SHELL_SYSTEMD_SLICE", " s.slice ")
        monkeypatch.setenv("XDG_RUNTIME_DIR", xdg)
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", bus)
        expected_show = [
            "/systemctl",
            "--user",
            "show",
            "--property=UserspaceTimestampMonotonic",
            "--property=SystemState",
        ]
        expected_prefix = (
            "/systemd-run",
            "--user",
            "--scope",
            "--quiet",
            "--description=MindRoom shell",
            "--expand-environment=no",
            "--slice=s.slice",
            "--",
        )
        expected = _SystemdSliceLauncher(
            prefix=expected_prefix,
            env_bin="/env",
            bus_env=(("XDG_RUNTIME_DIR", xdg), ("DBUS_SESSION_BUS_ADDRESS", bus)),
        )

        assert _systemd_slice_launcher() == expected
        assert _systemd_slice_launcher() == expected
        assert show_calls == [(expected_show, {"XDG_RUNTIME_DIR": xdg, "DBUS_SESSION_BUS_ADDRESS": bus})] * 2
        assert probe_calls == [
            (
                [
                    *expected_prefix,
                    "/env",
                    "-u",
                    "MINDROOM_PROBE_UNSET",
                    "--",
                    "MINDROOM_PROBE_SET=1",
                    "/systemd-run",
                    "--version",
                ],
                {"XDG_RUNTIME_DIR": xdg, "DBUS_SESSION_BUS_ADDRESS": bus},
            ),
        ]

        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/99")
        assert _systemd_slice_launcher() == _SystemdSliceLauncher(
            prefix=expected_prefix,
            env_bin="/env",
            bus_env=(("XDG_RUNTIME_DIR", "/run/user/99"), ("DBUS_SESSION_BUS_ADDRESS", bus)),
        )
        assert len(show_calls) == 3
        assert len(probe_calls) == 2

        for invalid in ("", "   ", "mindroom.service", ".slice", "-bad.slice", "a/b.slice", "a b.slice", "a\tb.slice"):
            monkeypatch.setenv("MINDROOM_SHELL_SYSTEMD_SLICE", invalid)
            assert _systemd_slice_launcher() is None
        assert len(show_calls) == 3
        assert len(probe_calls) == 2

        probe_rc = 1
        monkeypatch.setenv("MINDROOM_SHELL_SYSTEMD_SLICE", "other.slice")
        assert _systemd_slice_launcher() is None
    finally:
        _probe_systemd_slice_launcher.cache_clear()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux resource wrappers are Linux-only")
def test_systemd_slice_recovers_after_user_manager_outage_and_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outages fail open and clear the cache; manager recovery or restart re-probes once per generation."""
    show_state: tuple[int, str] = (1, "")
    _, probe_calls = _stub_systemd_tools(monkeypatch, lambda: show_state)
    monkeypatch.setenv("MINDROOM_SHELL_SYSTEMD_SLICE", "s.slice")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/42")
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    _probe_systemd_slice_launcher.cache_clear()
    try:
        # Unavailable at startup -> fails open without caching outage; G1 comes up (order-independent Key=Value).
        assert _systemd_slice_launcher() is None
        assert probe_calls == []
        show_state = (0, "SystemState=running\nUserspaceTimestampMonotonic=100\n")
        launcher = _systemd_slice_launcher()
        assert launcher is not None
        assert _systemd_slice_launcher() == launcher
        assert len(probe_calls) == 1

        # Outage clears the cache and fails open; recovery at the same generation still re-probes once.
        show_state = (1, "")
        assert _systemd_slice_launcher() is None
        assert _probe_systemd_slice_launcher.cache_info().currsize == 0
        show_state = (0, "UserspaceTimestampMonotonic=100\nSystemState=running\n")
        assert _systemd_slice_launcher() == launcher
        assert _systemd_slice_launcher() == launcher
        assert len(probe_calls) == 2

        # Direct generation change (restart without observed downtime, G1 -> G2) triggers a fresh probe.
        show_state = (0, "UserspaceTimestampMonotonic=200\nSystemState=running\n")
        assert _systemd_slice_launcher() == launcher
        assert _systemd_slice_launcher() == launcher
        assert len(probe_calls) == 3
    finally:
        _probe_systemd_slice_launcher.cache_clear()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux resource wrappers are Linux-only")
def test_systemd_user_manager_states_and_invalid_generations_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stopping/offline states and missing/zero/non-integer generations fail open; degraded remains active."""
    show_state: tuple[int, str] = (0, "UserspaceTimestampMonotonic=500\nSystemState=degraded\n")
    _, probe_calls = _stub_systemd_tools(monkeypatch, lambda: show_state)
    monkeypatch.setenv("MINDROOM_SHELL_SYSTEMD_SLICE", "s.slice")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/42")
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    _probe_systemd_slice_launcher.cache_clear()
    try:
        assert _systemd_slice_launcher() is not None
        for rejected in (
            "UserspaceTimestampMonotonic=500\nSystemState=stopping\n",
            "UserspaceTimestampMonotonic=500\nSystemState=offline\n",
            "SystemState=running\n",
            "UserspaceTimestampMonotonic=0\nSystemState=running\n",
            "UserspaceTimestampMonotonic=-5\nSystemState=running\n",
            "UserspaceTimestampMonotonic=not-a-number\nSystemState=running\n",
        ):
            show_state = (0, rejected)
            assert _systemd_slice_launcher() is None
        assert len(probe_calls) == 1
        assert _probe_systemd_slice_launcher.cache_info().currsize == 0
    finally:
        _probe_systemd_slice_launcher.cache_clear()


@pytest.mark.skipif(sys.platform != "linux", reason="SCHED_IDLE is Linux-only")
@pytest.mark.asyncio
async def test_idle_fallback_runs_when_chrt_is_absent_even_if_other_wrappers_prepend(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When chrt is unavailable, _schedule_process_tree_idle still runs even though ionice wraps argv."""
    _idle_io_launcher.cache_clear()
    if _idle_io_launcher() is None:
        pytest.skip("needs ionice so argv is still wrapped when chrt is absent")
    monkeypatch.setattr("mindroom.shell.execution._idle_launcher", lambda: None)
    monkeypatch.delenv("MINDROOM_SHELL_SYSTEMD_SLICE", raising=False)
    fallback_pids: list[int] = []
    real_fallback = _schedule_process_tree_idle
    monkeypatch.setattr(
        "mindroom.shell.execution._schedule_process_tree_idle",
        lambda pid: (fallback_pids.append(pid), real_fallback(pid)),
    )

    check = "import os, time; time.sleep(0.1); print(os.sched_getscheduler(0) == os.SCHED_IDLE)"
    assert await _run(registry, [sys.executable, "-c", check], tmp_path) == "True"
    assert len(fallback_pids) == 1


@pytest.mark.skipif(sys.platform != "linux", reason="systemd slices are Linux-only")
@pytest.mark.asyncio
async def test_systemd_slice_moves_command_and_preserves_dollar_args(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live systemd-run --user moves the command into the slice and keeps $$ / $VAR verbatim in argv."""
    if not os.environ.get("XDG_RUNTIME_DIR") and Path(f"/run/user/{os.getuid()}/bus").exists():
        monkeypatch.setenv("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    monkeypatch.setenv("MINDROOM_SHELL_SYSTEMD_SLICE", "app.slice")
    _probe_systemd_slice_launcher.cache_clear()
    if _systemd_slice_launcher() is None:
        pytest.skip("requires a live systemd --user bus supporting --expand-environment=no")

    caller_env = {"PATH": os.environ["PATH"], "MINDROOM_TEST_VAR": "expanded"}
    script = (
        "import os, pathlib, sys; "
        "print(sys.argv[1:], os.environ.get('XDG_RUNTIME_DIR'), os.environ.get('DBUS_SESSION_BUS_ADDRESS'), "
        "pathlib.Path('/proc/self/cgroup').read_text().strip())"
    )
    argv = [sys.executable, "-c", script, "$$", "$MINDROOM_TEST_VAR", "${MINDROOM_TEST_VAR}"]
    result = await run_command(
        registry,
        namespace="test",
        argv=argv,
        env=caller_env,
        cwd=str(tmp_path),
        tail=100,
        timeout=10,
    )

    assert result.handle is None
    assert result.message.startswith("['$$', '$MINDROOM_TEST_VAR', '${MINDROOM_TEST_VAR}'] None None ")
    assert "/app.slice/" in result.message
    assert "XDG_RUNTIME_DIR" not in caller_env

    # Custom caller bus vars (pointing nowhere) must not break systemd-run and must reach the command verbatim.
    custom_xdg = str(tmp_path / "no-bus-here")
    custom_dbus = "unix:path=/nonexistent/bus"
    custom_result = await run_command(
        registry,
        namespace="test",
        argv=argv,
        env={**caller_env, "XDG_RUNTIME_DIR": custom_xdg, "DBUS_SESSION_BUS_ADDRESS": custom_dbus},
        cwd=str(tmp_path),
        tail=100,
        timeout=10,
    )

    assert custom_result.handle is None
    assert custom_result.message.startswith(
        f"['$$', '$MINDROOM_TEST_VAR', '${{MINDROOM_TEST_VAR}}'] {custom_xdg} {custom_dbus} ",
    )
    assert "/app.slice/" in custom_result.message


@pytest.mark.skipif(sys.platform != "linux", reason="systemd slices are Linux-only")
@pytest.mark.asyncio
async def test_systemd_slice_live_outage_fails_open_and_recovers(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mid-run loss of the systemd --user socket fails open cleanly and resumes slicing on recovery."""
    live_xdg = os.environ.get("XDG_RUNTIME_DIR") or (
        f"/run/user/{os.getuid()}" if Path(f"/run/user/{os.getuid()}/bus").exists() else None
    )
    if live_xdg is None:
        pytest.skip("requires a live systemd --user runtime directory")

    with tempfile.TemporaryDirectory(prefix="mr-") as runtime_parent:
        runtime_link = Path(runtime_parent) / "rt"
        runtime_link.symlink_to(live_xdg)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_link))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        monkeypatch.setenv("MINDROOM_SHELL_SYSTEMD_SLICE", "app.slice")
        _probe_systemd_slice_launcher.cache_clear()
        try:
            if _systemd_slice_launcher() is None:
                pytest.skip("requires a live systemd --user bus supporting --expand-environment=no")

            self_cgroup = Path("/proc/self/cgroup").read_text(encoding="utf-8").strip()
            argv = [
                sys.executable,
                "-c",
                "from pathlib import Path; print(Path('/proc/self/cgroup').read_text().strip())",
            ]

            initial = await _run(registry, argv, tmp_path)
            assert "/app.slice/" in initial
            assert initial != self_cgroup

            # Repoint the same XDG_RUNTIME_DIR symlink to a missing dir (simulating user@<uid> socket disappearing).
            runtime_link.unlink()
            runtime_link.symlink_to(tmp_path / "dead-runtime-dir")
            assert await _run(registry, argv, tmp_path) == self_cgroup
            assert _probe_systemd_slice_launcher.cache_info().currsize == 0

            runtime_link.unlink()
            runtime_link.symlink_to(live_xdg)
            recovered = await _run(registry, argv, tmp_path)
            assert "/app.slice/" in recovered
            assert recovered != self_cgroup
        finally:
            _probe_systemd_slice_launcher.cache_clear()


def _process_group_threads(pgid: int) -> list[int]:
    threads = []
    for thread in Path("/proc").glob("[0-9]*/task/[0-9]*"):
        with contextlib.suppress(OSError):
            if os.getpgid(int(thread.parent.parent.name)) == pgid:
                threads.append(int(thread.name))
    return threads


@pytest.mark.skipif(sys.platform != "linux", reason="SCHED_IDLE is Linux-only")
def test_threads_and_processes_a_command_started_before_its_policy_was_set_move_to_the_idle_policy() -> None:
    """A busy event loop lets a command fork and start threads before MindRoom sets its policy."""
    start_thread = "import threading, time; threading.Thread(target=time.sleep, args=(30,)).start(); time.sleep(30)"
    threaded = shlex.join([sys.executable, "-c", start_thread])
    # The leader, a subshell, its sleep (a grandchild), and the two threads of the Python process.
    process = subprocess.Popen(["/bin/sh", "-c", f"(sleep 30; true) | {threaded}"], start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while len(threads := _process_group_threads(process.pid)) < 5:
            assert time.monotonic() < deadline, f"the command started only these threads: {threads}"
            time.sleep(0.01)
        _schedule_process_tree_idle(process.pid)

        assert {os.sched_getscheduler(thread) for thread in threads} == {os.SCHED_IDLE}
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


@pytest.mark.asyncio
async def test_a_running_command_reports_its_latest_output_as_progress(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While it runs, a command reports the tail of both streams when it changed, never the same text twice."""
    monkeypatch.setattr("mindroom.shell.execution._PROGRESS_INTERVAL_SECONDS", 0.1)
    reports: list[str] = []

    result = await run_command(
        registry,
        namespace="test",
        argv=["/bin/sh", "-c", "echo one; sleep 0.5; echo two; echo warn >&2; sleep 0.5"],
        env={"PATH": os.environ["PATH"]},
        cwd=str(tmp_path),
        tail=100,
        timeout=10,
        on_timeout="kill",
        on_progress=reports.append,
    )

    assert result.message == "one\ntwo\nStderr:\nwarn"
    assert reports[0] == "one"
    assert reports[-1] == "one\ntwo\nStderr:\nwarn"
    assert all(earlier != later for earlier, later in itertools.pairwise(reports))


@pytest.mark.skipif(sys.platform != "linux", reason="Linux resource wrappers are Linux-only")
def test_allow_systemd_slice_false_skips_systemd_slice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """allow_systemd_slice=False skips systemd-run while keeping chrt/ionice intact."""
    monkeypatch.setenv("MINDROOM_SHELL_SYSTEMD_SLICE", "test.slice")
    assert configured_systemd_slice_name() == "test.slice"

    slice_calls: list[bool] = []
    monkeypatch.setattr(
        "mindroom.shell.execution._systemd_slice_launcher",
        lambda: (slice_calls.append(True), _SystemdSliceLauncher(("/systemd-run", "--"), "/env", ()))[1],
    )

    env = {"PATH": os.environ["PATH"]}
    unsliced = _idle_launch_argv([sys.executable, "-c", "pass"], env, str(tmp_path), allow_systemd_slice=False)
    assert "/systemd-run" not in unsliced.argv
    assert slice_calls == []
    sliced = _idle_launch_argv([sys.executable, "-c", "pass"], env, str(tmp_path), allow_systemd_slice=True)
    assert sliced.argv[0] == "/systemd-run"
    assert len(slice_calls) == 1
