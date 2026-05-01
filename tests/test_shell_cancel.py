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

from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tools.shell import _process_registry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path


def _make_runtime_paths(tmp_path: Path) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )


def _get_run_shell_command(tmp_path: Path) -> Callable[..., Awaitable[str]]:
    runtime_paths = _make_runtime_paths(tmp_path)
    tool = get_tool_by_name("shell", runtime_paths, disable_sandbox_proxy=True, worker_target=None)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None
    return entrypoint


async def _wait_for_pid(pid_file: Path) -> int:
    for _ in range(50):
        if pid_file.exists():
            return int(pid_file.read_text(encoding="utf-8").strip())
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


@pytest.fixture(autouse=True)
def clear_process_registry() -> Iterator[None]:
    """Ensure subprocess registry state does not leak between tests."""
    _process_registry.clear()
    yield
    for record in list(_process_registry.values()):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(record.pid, signal.SIGKILL)
    _process_registry.clear()


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

    with patch("mindroom.tools.shell.os.killpg", wraps=os.killpg) as killpg_mock:
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
    assert _process_registry == {}


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
    assert _process_registry == {}


@pytest.mark.asyncio
async def test_run_shell_command_cancellation_cleans_up_background_handle(tmp_path: Path) -> None:
    """Cancellation after timeout backgrounding should drop the unusable handle."""
    run_shell_command = _get_run_shell_command(tmp_path)
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

    with patch("mindroom.tools.shell.asyncio.sleep", new=controlled_sleep):
        task = asyncio.create_task(run_shell_command(command, timeout=0))
        pid = await _wait_for_pid(pid_file)
        await background_registered.wait()
        assert len(_process_registry) == 1

        task.cancel()
        release_background_sleep.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    await _assert_pid_dead(pid)
    assert _process_registry == {}


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
        patch("mindroom.tools.shell._await_reader_tasks_with_grace", new=blocked_reader_grace),
        patch("mindroom.tools.shell._terminate_process_group", new=terminate_process_group),
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
    assert _process_registry == {}
