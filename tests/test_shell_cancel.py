"""Regression tests for shell subprocess cancellation."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.shell.output import BackgroundLogPolicy
from mindroom.shell.process import ManagedProcess
from mindroom.shell.registry import ProcessRegistry
from tests.test_shell_async import _get_toolkit

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path


def _get_run_shell_command(tmp_path: Path) -> Callable[..., Awaitable[str]]:
    return _get_toolkit(tmp_path).functions["run_shell_command"]


async def _wait_for_pid(pid_file: Path) -> int:
    """Wait until the PID file holds a PID, rather than until it merely exists.

    The subprocesses record their PID with `write_text`, which creates the file
    before it puts anything in it. Treating the file's appearance as readiness
    therefore reads whatever is there at that instant, and between those two
    steps that is an empty string -- `int("")`, not a PID. The window is a
    couple of syscalls wide and a loaded machine is enough to land in it, so
    wait for the content this helper actually promises to return.
    """
    for _ in range(50):
        recorded = pid_file.read_text(encoding="utf-8").strip() if pid_file.exists() else ""
        if recorded:
            return int(recorded)
        await asyncio.sleep(0.05)
    message = f"PID file was not written: {pid_file}"
    raise AssertionError(message)


async def _assert_pid_dead(pid: int) -> None:
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.05)
    message = f"Subprocess {pid} is still alive"
    raise AssertionError(message)


def _fork_child_holding_stdio_script() -> str:
    return (
        "import os\n"
        "import pathlib\n"
        "import signal\n"
        "import sys\n"
        "import time\n"
        "stop_file = pathlib.Path(sys.argv[2])\n"
        "def stop_child(signum, frame):\n"
        "    stop_file.write_text('stopped', encoding='utf-8')\n"
        "    os._exit(0)\n"
        "pid = os.fork()\n"
        "if pid:\n"
        "    pathlib.Path(sys.argv[1]).write_text(str(pid), encoding='utf-8')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, stop_child)\n"
        "time.sleep(30)\n"
    )


async def _stop_recorded_child(pid: int, stop_file: Path) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)

    for _ in range(20):
        if stop_file.exists():
            return
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.05)

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)
    await _assert_pid_dead(pid)


@pytest.fixture
def registry() -> Iterator[ProcessRegistry]:
    """Kill anything a failing background test leaves registered."""
    records = ProcessRegistry()
    yield records
    records.kill_all()


@pytest.mark.asyncio
async def test_a_stop_cancelled_during_its_grace_still_kills_what_ignored_sigterm(tmp_path: Path) -> None:
    """A stop that is itself cancelled while waiting out SIGTERM still kills a process that ignored it."""
    pid_file = tmp_path / "member.pid"
    process = await asyncio.create_subprocess_exec(
        "bash",
        "-c",
        '(trap "" TERM; echo "$BASHPID" > "$1"; exec tail -f /dev/null) & wait',
        "bash",
        str(pid_file),
        start_new_session=True,
    )
    member = await _wait_for_pid(pid_file)
    try:
        stopping = asyncio.create_task(ManagedProcess(process).terminate_tree(grace_period=30))
        # The leader dies of SIGTERM while its member ignores it and keeps the group alive.
        await asyncio.wait_for(process.wait(), timeout=10)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        await _assert_pid_dead(member)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(member, signal.SIGKILL)


@pytest.mark.asyncio
async def test_run_shell_command_cancellation_kills_subprocess(tmp_path: Path) -> None:
    """Cancelling run_shell_command should kill the local subprocess and re-raise."""
    run_shell_command = _get_run_shell_command(tmp_path)
    pid_file = tmp_path / "sleep.pid"
    command = [
        sys.executable,
        "-c",
        (
            "import os, pathlib, sys, time; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
            "time.sleep(300)"
        ),
        str(pid_file),
    ]

    with patch("mindroom.shell._process_target.os.killpg", wraps=os.killpg) as killpg_mock:
        task = asyncio.create_task(run_shell_command(command, timeout=120))
        pid = await _wait_for_pid(pid_file)
        await asyncio.sleep(0.1)

        started_cancel = time.perf_counter()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.perf_counter() - started_cancel

    await _assert_pid_dead(pid)
    killpg_mock.assert_called()
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_run_shell_command_cancellation_cleans_up_reader_tasks(tmp_path: Path) -> None:
    """Cancellation should stop a noisy subprocess without leaking a background handle."""
    run_shell_command = _get_run_shell_command(tmp_path)
    pid_file = tmp_path / "stream.pid"
    command = [
        sys.executable,
        "-c",
        (
            "import os, pathlib, sys, time; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
            "i = 0\n"
            "while True:\n"
            "    print(f'out-{i}', flush=True)\n"
            "    print(f'err-{i}', file=sys.stderr, flush=True)\n"
            "    i += 1\n"
            "    time.sleep(0.05)\n"
        ),
        str(pid_file),
    ]

    task = asyncio.create_task(run_shell_command(command, timeout=120))
    pid = await _wait_for_pid(pid_file)
    await asyncio.sleep(0.2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _assert_pid_dead(pid)


@pytest.mark.asyncio
async def test_cancellation_after_backgrounding_drops_the_undeliverable_handle(
    tmp_path: Path,
    registry: ProcessRegistry,
) -> None:
    """A background run cancelled while its handle is being registered kills the command and drops the handle."""
    pid_file = tmp_path / "background.pid"
    command = [
        sys.executable,
        "-c",
        (
            "import os, pathlib, sys, time; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
            "time.sleep(300)"
        ),
        str(pid_file),
    ]

    real_sleep = asyncio.sleep
    background_registered = asyncio.Event()
    release_background_sleep = asyncio.Event()

    async def controlled_sleep(delay: float) -> None:
        if delay == 0 and not background_registered.is_set():
            background_registered.set()
            await release_background_sleep.wait()
            return
        await real_sleep(delay)

    with patch("mindroom.shell.registry.asyncio.sleep", new=controlled_sleep):
        task = asyncio.create_task(
            registry.run_or_background(
                namespace="test",
                argv=command,
                env={"PATH": os.environ["PATH"]},
                cwd=str(tmp_path),
                tail=100,
                timeout=0,
            ),
        )
        pid = await _wait_for_pid(pid_file)
        await background_registered.wait()
        assert registry.record_count == 1

        task.cancel()
        release_background_sleep.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    await _assert_pid_dead(pid)


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
async def test_run_shell_command_cancellation_after_foreground_exit_does_not_terminate_group(tmp_path: Path) -> None:
    """Cancelling after foreground exit should cancel readers without process-group termination."""
    run_shell_command = _get_run_shell_command(tmp_path)
    pid_file = tmp_path / "inherited-child.pid"
    stop_file = tmp_path / "inherited-child.stopped"
    command = [sys.executable, "-c", _fork_child_holding_stdio_script(), str(pid_file), str(stop_file)]
    grace_entered = asyncio.Event()
    release_grace = asyncio.Event()

    async def blocked_reader_grace(
        stdout_reader: object,
        stderr_reader: object,
        *,
        grace_seconds: float,
    ) -> None:
        del stdout_reader, stderr_reader, grace_seconds
        grace_entered.set()
        await release_grace.wait()

    child_pid: int | None = None
    terminate_process_group = AsyncMock()

    with (
        patch("mindroom.shell.runner._await_reader_tasks_with_grace", new=blocked_reader_grace),
        patch.object(ManagedProcess, "terminate_tree", new=terminate_process_group),
    ):
        task = asyncio.create_task(run_shell_command(command, timeout=120))
        try:
            child_pid = await _wait_for_pid(pid_file)
            await asyncio.wait_for(grace_entered.wait(), timeout=2)

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release_grace.set()
            if child_pid is not None:
                await _stop_recorded_child(child_pid, stop_file)
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    terminate_process_group.assert_not_awaited()


def _sleeping_pid_writer_argv(pid_file: Path) -> list[str]:
    return [
        sys.executable,
        "-c",
        (
            "import os, pathlib, sys, time; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
            "print('started', flush=True); "
            "time.sleep(300)"
        ),
        str(pid_file),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("race_kind", ["cancel", "close", "close_namespace"])
async def test_handoff_window_race_leaves_zero_leaked_processes_records_or_open_logs(
    tmp_path: Path,
    registry: ProcessRegistry,
    race_kind: str,
) -> None:
    """Cancellation, registry.close(), or close_namespace() during handoff leaves no leaked process, record, or log."""
    pid_file = tmp_path / f"handoff-{race_kind}.pid"
    policy = BackgroundLogPolicy(log_dir=str(tmp_path / "logs"))
    sibling = (
        await registry.spawn_background(
            namespace="ns-b",
            argv=["sleep", "300"],
            env={"PATH": os.environ["PATH"]},
            cwd=str(tmp_path),
            background_log=policy,
        )
        if race_kind == "close_namespace"
        else None
    )

    real_sleep = asyncio.sleep
    handoff_entered, release_handoff = asyncio.Event(), asyncio.Event()

    async def controlled_sleep(delay: float) -> None:
        if delay == 0 and not handoff_entered.is_set():
            handoff_entered.set()
            await release_handoff.wait()
            return
        await real_sleep(delay)

    with patch("mindroom.shell.registry.asyncio.sleep", new=controlled_sleep):
        task = asyncio.create_task(
            registry.run_or_background(
                namespace="ns-a",
                argv=_sleeping_pid_writer_argv(pid_file),
                env={"PATH": os.environ["PATH"]},
                cwd=str(tmp_path),
                tail=100,
                timeout=0,
                background_log=policy,
            ),
        )
        pid = await _wait_for_pid(pid_file)
        await handoff_entered.wait()
        log_file = next(r for r in registry.records_view.values() if r.namespace == "ns-a").log_file
        assert log_file is not None
        assert log_file.closed is False

        if race_kind == "cancel":
            task.cancel()
            release_handoff.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert registry.record_count == 0
        elif race_kind == "close":
            close_task = asyncio.create_task(registry.close())
            await real_sleep(0.05)
            release_handoff.set()
            result = await task
            await close_task
            assert (result.background_rejected, result.handle, registry.closed, registry.record_count) == (
                True,
                None,
                True,
                0,
            )
            assert "Shell turn already ended" in result.message
        else:
            close_task = asyncio.create_task(registry.close_namespace("ns-a"))
            await real_sleep(0.05)
            release_handoff.set()
            result = await task
            assert await close_task == 1
            assert (result.background_rejected, result.handle) == (True, None)
            assert sibling is not None
            assert sibling.handle is not None
            assert sibling.handle in registry.records_view
            os.kill(registry.records_view[sibling.handle].pid, 0)

    assert log_file.closed is True
    await _assert_pid_dead(pid)
