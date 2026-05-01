"""Tests for async shell tool with timeout-to-handle support."""

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
from agno.tools.function import Function, FunctionCall, FunctionExecutionResult

from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tools.shell import _process_registry, shell_tools

if TYPE_CHECKING:
    from pathlib import Path

    from agno.tools.toolkit import Toolkit

    from mindroom.constants import RuntimePaths


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


def _get_toolkit(tmp_path: Path) -> Toolkit:
    runtime_paths = _make_runtime_paths(tmp_path)
    return get_tool_by_name("shell", runtime_paths, disable_sandbox_proxy=True, worker_target=None)


def _fork_child_holding_stdio_script(*, parent_stderr: str = "", parent_exit_code: int = 0) -> str:
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
        f"sys.stderr.write({parent_stderr!r})\n"
        "sys.stderr.flush()\n"
        "pid = os.fork()\n"
        "if pid:\n"
        "    pathlib.Path(sys.argv[1]).write_text(str(pid), encoding='utf-8')\n"
        f"    sys.exit({parent_exit_code})\n"
        "signal.signal(signal.SIGTERM, stop_child)\n"
        "time.sleep(30)\n"
    )


async def _wait_for_pid_file(pid_file: Path) -> int:
    for _ in range(50):
        if pid_file.exists():
            return int(pid_file.read_text(encoding="utf-8").strip())
        await asyncio.sleep(0.05)
    message = f"PID file was not written: {pid_file}"
    raise AssertionError(message)


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

    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.05)

    message = f"Child process {pid} did not stop"
    raise AssertionError(message)


def _get_run_shell_command_function(tool: Toolkit) -> Function:
    return tool.async_functions["run_shell_command"]


async def _aexecute_run_shell_command(tool: Toolkit, args: object) -> FunctionExecutionResult:
    return await FunctionCall(function=_get_run_shell_command_function(tool), arguments={"args": args}).aexecute()


# ---------------------------------------------------------------------------
# run_shell_command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_shell_command_returns_output(tmp_path: Path) -> None:
    """Fast command should return stdout directly."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(["echo", "hello world"])
    assert result == "hello world"


@pytest.mark.asyncio
async def test_run_shell_command_bash_echo_returns_output(tmp_path: Path) -> None:
    """Normal shell output should still be returned."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(["bash", "-lc", "echo hello"])
    assert result == "hello"


def test_run_shell_command_schema_keeps_args_as_string_array(tmp_path: Path) -> None:
    """The tool schema should expose args as array<string>, not anyOf."""
    tool = _get_toolkit(tmp_path)
    args_schema = _get_run_shell_command_function(tool).parameters["properties"]["args"]

    assert args_schema["type"] == "array"
    assert args_schema["items"] == {"type": "string"}
    assert "anyOf" not in args_schema


@pytest.mark.asyncio
async def test_run_shell_command_parses_json_string_args(tmp_path: Path) -> None:
    """JSON-stringified args should be parsed into a shell argv list."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint('["echo", "hello world"]')
    assert result == "hello world"


@pytest.mark.asyncio
async def test_run_shell_command_parses_single_item_json_args(tmp_path: Path) -> None:
    """A single command item in JSON string form should execute normally."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint('["ls"]')
    assert result
    assert not result.startswith("Error:")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        '["echo",',
        '{"cmd": "ls"}',
        '"echo"',
        "",
        '["echo", 42]',
        '["bash", ["-c", "ls"]]',
        '["echo", {"x": 1}]',
    ],
)
async def test_run_shell_command_rejects_invalid_stringified_args(tmp_path: Path, args: str) -> None:
    """Malformed or non-flat stringified args should fail validation."""
    tool = _get_toolkit(tmp_path)
    result = await _aexecute_run_shell_command(tool, args)

    assert result.status == "failure"
    assert result.error is not None
    assert "'args' must be a flat list of strings" in result.error


@pytest.mark.asyncio
async def test_run_shell_command_empty_json_list_uses_existing_empty_behavior(tmp_path: Path) -> None:
    """An empty JSON list should fall through to the existing subprocess error path."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint("[]")
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_run_shell_command_returns_error_on_nonzero_exit(tmp_path: Path) -> None:
    """Non-zero exit should return stderr."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(["bash", "-c", "echo oops >&2; exit 1"])
    assert result.startswith("Error:")
    assert "oops" in result


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
async def test_run_shell_command_returns_after_foreground_exit_with_inherited_pipe_child(tmp_path: Path) -> None:
    """Regression: tmux/auth helpers/watchers/daemons can inherit pipes after foreground exit."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    pid_file = tmp_path / "child.pid"
    stop_file = tmp_path / "child.stopped"
    command = [sys.executable, "-c", _fork_child_holding_stdio_script(), str(pid_file), str(stop_file)]
    started = time.perf_counter()
    task = asyncio.create_task(entrypoint(command, timeout=10))
    child_pid: int | None = None

    try:
        child_pid = await _wait_for_pid_file(pid_file)
        result = await asyncio.wait_for(task, timeout=2)
    finally:
        if child_pid is not None:
            await _stop_recorded_child(child_pid, stop_file)
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    elapsed = time.perf_counter() - started
    assert elapsed < 2
    assert result == ""


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
async def test_run_shell_command_nonzero_foreground_exit_with_inherited_pipe_child(tmp_path: Path) -> None:
    """Nonzero foreground exit should return buffered stderr without waiting for descendant EOF."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    pid_file = tmp_path / "child-error.pid"
    stop_file = tmp_path / "child-error.stopped"
    command = [
        sys.executable,
        "-c",
        _fork_child_holding_stdio_script(parent_stderr="foreground failed\n", parent_exit_code=7),
        str(pid_file),
        str(stop_file),
    ]
    started = time.perf_counter()
    task = asyncio.create_task(entrypoint(command, timeout=10))
    child_pid: int | None = None

    try:
        child_pid = await _wait_for_pid_file(pid_file)
        result = await asyncio.wait_for(task, timeout=2)
    finally:
        if child_pid is not None:
            await _stop_recorded_child(child_pid, stop_file)
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    elapsed = time.perf_counter() - started
    assert elapsed < 2
    assert result == "Error: foreground failed"


@pytest.mark.asyncio
async def test_run_shell_command_tail_parameter(tmp_path: Path) -> None:
    """Only the last N lines should be returned."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(["bash", "-c", "for i in $(seq 1 20); do echo line$i; done"], tail=3)
    lines = result.strip().split("\n")
    assert lines == ["line18", "line19", "line20"]


@pytest.mark.asyncio
async def test_run_shell_command_returns_handle_on_timeout(tmp_path: Path) -> None:
    """Command exceeding timeout should return a handle."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    check_fn = tool.functions["check_shell_command"].entrypoint
    assert entrypoint is not None
    assert check_fn is not None

    result = await entrypoint(["sleep", "300"], timeout=1)

    assert "timed out" in result.lower()
    assert "Handle: shell:" in result
    assert "check_shell_command" in result

    # Extract handle and clean up
    handle = result.split("Handle: ")[1].split("\n")[0]
    assert "RUNNING" in check_fn(handle)
    kill_fn = tool.functions["kill_shell_command"].entrypoint
    kill_fn(handle, force=True)
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_run_shell_command_error_on_bad_command(tmp_path: Path) -> None:
    """Non-existent command should return an error."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(["__nonexistent_command_xyz__"])
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_run_shell_command_respects_base_dir(tmp_path: Path) -> None:
    """Shell should execute in base_dir when configured."""
    runtime_paths = _make_runtime_paths(tmp_path)
    tool = get_tool_by_name(
        "shell",
        runtime_paths,
        disable_sandbox_proxy=True,
        worker_target=None,
        tool_init_overrides={"base_dir": str(tmp_path)},
    )
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(["pwd"])
    assert result.strip() == str(tmp_path)


# ---------------------------------------------------------------------------
# check_shell_command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_shell_command_running(tmp_path: Path) -> None:
    """Checking a running process should show RUNNING status."""
    tool = _get_toolkit(tmp_path)
    run_fn = tool.async_functions["run_shell_command"].entrypoint
    check_fn = tool.functions["check_shell_command"].entrypoint
    assert run_fn is not None
    assert check_fn is not None

    result = await run_fn(["sleep", "300"], timeout=1)
    handle = result.split("Handle: ")[1].split("\n")[0]

    status = check_fn(handle)
    assert "RUNNING" in status

    # Clean up
    kill_fn = tool.functions["kill_shell_command"].entrypoint
    kill_fn(handle, force=True)
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_check_shell_command_finished(tmp_path: Path) -> None:
    """Checking a finished process should show FINISHED status and output."""
    tool = _get_toolkit(tmp_path)
    run_fn = tool.async_functions["run_shell_command"].entrypoint
    check_fn = tool.functions["check_shell_command"].entrypoint
    assert run_fn is not None
    assert check_fn is not None

    # Use a command that sleeps longer than the timeout to guarantee backgrounding,
    # but emits output first so we can verify it after finish.
    result = await run_fn(["bash", "-c", "echo done-output; sleep 3"], timeout=1)
    assert "Handle:" in result
    handle = result.split("Handle: ")[1].split("\n")[0]

    # Wait for the process to finish
    for _ in range(50):
        status = check_fn(handle)
        if "FINISHED" in status:
            break
        await asyncio.sleep(0.1)
    assert "FINISHED" in status
    assert "done-output" in status


@pytest.mark.asyncio
async def test_check_shell_command_unknown_handle(tmp_path: Path) -> None:
    """Checking an unknown handle should return an error."""
    tool = _get_toolkit(tmp_path)
    check_fn = tool.functions["check_shell_command"].entrypoint
    assert check_fn is not None

    result = check_fn("shell:nonexistent")
    assert "Error:" in result
    assert "Unknown handle" in result


@pytest.mark.asyncio
async def test_check_shell_command_partial_output(tmp_path: Path) -> None:
    """Checking a running process should show partial output."""
    tool = _get_toolkit(tmp_path)
    run_fn = tool.async_functions["run_shell_command"].entrypoint
    check_fn = tool.functions["check_shell_command"].entrypoint
    assert run_fn is not None
    assert check_fn is not None

    result = await run_fn(
        ["bash", "-c", "for i in 1 2 3; do echo partial-line-$i; done; sleep 300"],
        timeout=1,
    )
    assert "Handle:" in result
    handle = result.split("Handle: ")[1].split("\n")[0]

    # Give readers a moment to consume output
    await asyncio.sleep(0.3)

    status = check_fn(handle)
    assert "RUNNING" in status
    assert "partial-line" in status

    # Clean up
    kill_fn = tool.functions["kill_shell_command"].entrypoint
    kill_fn(handle, force=True)
    await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
# kill_shell_command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_shell_command(tmp_path: Path) -> None:
    """Killing a running process should succeed."""
    tool = _get_toolkit(tmp_path)
    run_fn = tool.async_functions["run_shell_command"].entrypoint
    kill_fn = tool.functions["kill_shell_command"].entrypoint
    check_fn = tool.functions["check_shell_command"].entrypoint
    assert run_fn is not None
    assert kill_fn is not None
    assert check_fn is not None

    result = await run_fn(["sleep", "300"], timeout=1)
    handle = result.split("Handle: ")[1].split("\n")[0]

    kill_result = kill_fn(handle)
    assert "Terminated" in kill_result or "SIGTERM" in kill_result

    # Wait for process to actually exit
    for _ in range(30):
        status = check_fn(handle)
        if "FINISHED" in status:
            break
        await asyncio.sleep(0.1)

    assert "FINISHED" in status


@pytest.mark.asyncio
async def test_kill_shell_command_force(tmp_path: Path) -> None:
    """Force-killing should send SIGKILL."""
    tool = _get_toolkit(tmp_path)
    run_fn = tool.async_functions["run_shell_command"].entrypoint
    kill_fn = tool.functions["kill_shell_command"].entrypoint
    assert run_fn is not None
    assert kill_fn is not None

    result = await run_fn(["sleep", "300"], timeout=1)
    handle = result.split("Handle: ")[1].split("\n")[0]

    kill_result = kill_fn(handle, force=True)
    assert "Force-killed" in kill_result or "SIGKILL" in kill_result
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_kill_shell_command_already_finished(tmp_path: Path) -> None:
    """Killing an already-finished process should say so."""
    tool = _get_toolkit(tmp_path)
    run_fn = tool.async_functions["run_shell_command"].entrypoint
    kill_fn = tool.functions["kill_shell_command"].entrypoint
    check_fn = tool.functions["check_shell_command"].entrypoint
    assert run_fn is not None
    assert kill_fn is not None
    assert check_fn is not None

    # Force backgrounding: command finishes in ~0.2s but timeout is very short
    result = await run_fn(["bash", "-c", "echo fast; sleep 0.2"], timeout=0)
    assert "Handle:" in result
    handle = result.split("Handle: ")[1].split("\n")[0]

    # Wait for it to finish
    for _ in range(30):
        status = check_fn(handle)
        if "FINISHED" in status:
            break
        await asyncio.sleep(0.1)
    assert "FINISHED" in status

    kill_result = kill_fn(handle)
    assert "already finished" in kill_result.lower()


@pytest.mark.asyncio
async def test_kill_shell_command_unknown_handle(tmp_path: Path) -> None:
    """Killing an unknown handle should return error."""
    tool = _get_toolkit(tmp_path)
    kill_fn = tool.functions["kill_shell_command"].entrypoint
    assert kill_fn is not None

    result = kill_fn("shell:nonexistent")
    assert "Error:" in result
    assert "Unknown handle" in result


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def test_shell_registers_three_tools(tmp_path: Path) -> None:
    """Shell toolkit should register run, check, and kill functions."""
    tool = _get_toolkit(tmp_path)

    assert "run_shell_command" in tool.async_functions
    assert "check_shell_command" in tool.functions
    assert "kill_shell_command" in tool.functions


def test_shell_disabled_registers_no_tools(tmp_path: Path) -> None:
    """Shell toolkit with enable_run_shell_command=False should have no tools."""
    runtime_paths = _make_runtime_paths(tmp_path)

    cls = shell_tools()
    toolkit = cls(enable_run_shell_command=False, runtime_paths=runtime_paths)
    assert len(toolkit.functions) == 0
    assert len(toolkit.async_functions) == 0


# ---------------------------------------------------------------------------
# Stale record cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_stale_records(tmp_path: Path) -> None:
    """Stale finished records should be cleaned up based on finished_at."""
    tool = _get_toolkit(tmp_path)
    run_fn = tool.async_functions["run_shell_command"].entrypoint
    check_fn = tool.functions["check_shell_command"].entrypoint
    assert run_fn is not None
    assert check_fn is not None

    # Force backgrounding with timeout=0, command finishes quickly
    result = await run_fn(["bash", "-c", "echo quick; sleep 0.2"], timeout=0)
    assert "Handle:" in result
    handle = result.split("Handle: ")[1].split("\n")[0]

    # Wait for process to finish
    for _ in range(30):
        status = check_fn(handle)
        if "FINISHED" in status:
            break
        await asyncio.sleep(0.1)
    assert "FINISHED" in status

    # Record should still exist — aging started_at should NOT cause sweep
    # because sweep now uses finished_at
    record = _process_registry.get(handle)
    assert record is not None
    record.started_at = time.monotonic() - 700  # 700 seconds ago

    # Trigger sweep — record should survive because finished_at is recent
    await run_fn(["echo", "trigger-sweep"])
    assert handle in _process_registry

    # Now age finished_at — record should be swept
    record.finished_at = time.monotonic() - 700
    await run_fn(["echo", "trigger-sweep-2"])
    assert handle not in _process_registry


# ---------------------------------------------------------------------------
# Env passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_env_passthrough_preserved(tmp_path: Path) -> None:
    """Runtime env values from .env should be visible in shell commands."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("MY_TEST_VAR=async-shell-works\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )
    tool = get_tool_by_name("shell", runtime_paths, disable_sandbox_proxy=True, worker_target=None)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(["bash", "-lc", "printf '%s' \"$MY_TEST_VAR\""])
    assert result == "async-shell-works"


@pytest.mark.asyncio
async def test_login_bash_preserves_runtime_path_after_profile_reset(tmp_path: Path) -> None:
    """MindRoom shell calls should keep runtime PATH even when login startup rewrites it."""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_bash = fake_bin / "bash"
    fake_bash.write_text(
        '#!/bin/sh\nif [ "$1" = "-lc" ]; then\n  export PATH=/profile/default\n  exec /bin/sh -c "$2"\nfi\nexec /bin/sh "$@"\n',
        encoding="utf-8",
    )
    fake_bash.chmod(0o755)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={"PATH": "/worker-env/bin:/usr/bin:/bin"},
    )
    tool = get_tool_by_name("shell", runtime_paths, disable_sandbox_proxy=True, worker_target=None)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint([str(fake_bash), "-lc", "printf '%s' \"$PATH\""])

    assert result == "/worker-env/bin:/usr/bin:/bin"


# ---------------------------------------------------------------------------
# Handle persistence across toolkit instances (sandbox runner path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_persists_across_toolkit_instances(tmp_path: Path) -> None:
    """Handles created by one toolkit instance should be visible to a new one.

    This simulates the sandbox runner path where _resolve_entrypoint creates
    a fresh MindRoomShellTools per request.
    """
    tool1 = _get_toolkit(tmp_path)
    run_fn = tool1.async_functions["run_shell_command"].entrypoint
    assert run_fn is not None

    result = await run_fn(["sleep", "300"], timeout=1)
    assert "Handle:" in result
    handle = result.split("Handle: ")[1].split("\n")[0]

    # Create a second toolkit instance (simulates sandbox runner re-creation)
    tool2 = _get_toolkit(tmp_path)
    check_fn = tool2.functions["check_shell_command"].entrypoint
    assert check_fn is not None

    status = check_fn(handle)
    assert "RUNNING" in status

    # Kill via the second instance too
    kill_fn = tool2.functions["kill_shell_command"].entrypoint
    assert kill_fn is not None
    kill_result = kill_fn(handle, force=True)
    assert "Force-killed" in kill_result or "already exited" in kill_result.lower()
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_handle_check_then_kill_across_instances(tmp_path: Path) -> None:
    """Full run→check→kill lifecycle across separate toolkit instances."""
    tool_run = _get_toolkit(tmp_path)
    run_fn = tool_run.async_functions["run_shell_command"].entrypoint
    assert run_fn is not None

    result = await run_fn(["bash", "-c", "for i in 1 2 3; do echo line-$i; done; sleep 300"], timeout=1)
    assert "Handle:" in result
    handle = result.split("Handle: ")[1].split("\n")[0]

    await asyncio.sleep(0.3)

    # Check from a fresh instance
    tool_check = _get_toolkit(tmp_path)
    check_fn = tool_check.functions["check_shell_command"].entrypoint
    assert check_fn is not None
    status = check_fn(handle)
    assert "RUNNING" in status
    assert "line-" in status

    # Kill from yet another fresh instance
    tool_kill = _get_toolkit(tmp_path)
    kill_fn = tool_kill.functions["kill_shell_command"].entrypoint
    assert kill_fn is not None
    kill_fn(handle, force=True)

    for _ in range(30):
        tool_final = _get_toolkit(tmp_path)
        final_check = tool_final.functions["check_shell_command"].entrypoint
        assert final_check is not None
        status = final_check(handle)
        if "FINISHED" in status:
            break
        await asyncio.sleep(0.1)

    assert "FINISHED" in status


@pytest.mark.asyncio
async def test_handle_isolation_blocks_cross_runtime_access(tmp_path: Path) -> None:
    """A handle from one runtime should not be visible to a different runtime root."""
    runtime_a = tmp_path / "runtime-a"
    runtime_b = tmp_path / "runtime-b"
    runtime_a.mkdir()
    runtime_b.mkdir()

    tool_a = _get_toolkit(runtime_a)
    tool_b = _get_toolkit(runtime_b)
    run_a = tool_a.async_functions["run_shell_command"].entrypoint
    check_b = tool_b.functions["check_shell_command"].entrypoint
    kill_b = tool_b.functions["kill_shell_command"].entrypoint
    kill_a = tool_a.functions["kill_shell_command"].entrypoint
    assert run_a is not None
    assert check_b is not None
    assert kill_b is not None
    assert kill_a is not None

    result = await run_a(["sleep", "300"], timeout=0)
    assert "Handle:" in result
    handle = result.split("Handle: ")[1].split("\n")[0]

    try:
        check_result = check_b(handle)
        assert "Unknown handle" in check_result

        kill_result = kill_b(handle, force=True)
        assert "Unknown handle" in kill_result
    finally:
        kill_a(handle, force=True)
        await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
# _MAX_BACKGROUNDED limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_backgrounded_limit(tmp_path: Path) -> None:
    """Exceeding _MAX_BACKGROUNDED should return an error and kill the excess process."""
    tool = _get_toolkit(tmp_path)
    run_fn = tool.async_functions["run_shell_command"].entrypoint
    assert run_fn is not None

    handles: list[str] = []

    # Patch to a small limit
    with patch("mindroom.tools.shell._MAX_BACKGROUNDED", 2):
        # Fill up to the limit
        for _ in range(2):
            result = await run_fn(["sleep", "300"], timeout=0)
            assert "Handle:" in result
            handles.append(result.split("Handle: ")[1].split("\n")[0])

        # Third one should fail
        result = await run_fn(["sleep", "300"], timeout=0)
        assert "Too many backgrounded processes" in result

    # Clean up
    kill_fn = tool.functions["kill_shell_command"].entrypoint
    for h in handles:
        kill_fn(h, force=True)
    await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
# Long-running command retention (finished_at not started_at)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_running_command_not_swept_on_finish(tmp_path: Path) -> None:
    """A command that ran for >10min should not be swept immediately on finish."""
    tool = _get_toolkit(tmp_path)
    run_fn = tool.async_functions["run_shell_command"].entrypoint
    check_fn = tool.functions["check_shell_command"].entrypoint
    assert run_fn is not None
    assert check_fn is not None

    # Force backgrounding with timeout=0
    result = await run_fn(["bash", "-c", "echo long-output; sleep 0.2"], timeout=0)
    assert "Handle:" in result
    handle = result.split("Handle: ")[1].split("\n")[0]

    # Wait for finish
    for _ in range(30):
        status = check_fn(handle)
        if "FINISHED" in status:
            break
        await asyncio.sleep(0.1)
    assert "FINISHED" in status

    # Simulate a process that started 15 minutes ago (longer than _STALE_RECORD_SECONDS)
    record = _process_registry[handle]
    record.started_at = time.monotonic() - 900

    # Trigger sweep — record should survive because finished_at is recent
    await run_fn(["echo", "trigger"])
    assert handle in _process_registry
    assert "long-output" in check_fn(handle)
