"""Regression tests for shell subprocess cancellation."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

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

    task = asyncio.create_task(run_shell_command(command, timeout=120))
    pid = await _wait_for_pid(pid_file)
    await asyncio.sleep(0.1)

    started_cancel = time.perf_counter()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.perf_counter() - started_cancel

    await _assert_pid_dead(pid)
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
