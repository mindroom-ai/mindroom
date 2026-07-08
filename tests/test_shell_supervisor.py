"""Tests for the worker-local shell supervisor process and its clients."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import sys
import tempfile
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mindroom import shell_supervisor
from mindroom.api import sandbox_runner as sandbox_runner_module
from mindroom.constants import resolve_runtime_paths
from mindroom.shell_execution import run_command
from mindroom.shell_supervisor import (
    SHELL_SUPERVISOR_SOCKET_ENV,
    _handle_connection,
    _ShellSupervisorManager,
    check_command_via_supervisor,
    kill_command_via_supervisor,
    run_command_via_supervisor,
)
from mindroom.tool_system.metadata import get_tool_by_name

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from agno.tools.toolkit import Toolkit

    from mindroom.constants import RuntimePaths
    from mindroom.shell_execution import ProcessRecord

_MINIMAL_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}


@contextlib.asynccontextmanager
async def _running_server(registry: dict[str, ProcessRecord]) -> AsyncIterator[str]:
    runtime_dir = Path(tempfile.mkdtemp(prefix="mindroom-shell-test-"))
    socket_path = str(runtime_dir / "s.sock")
    server = await asyncio.start_unix_server(partial(_handle_connection, registry), path=socket_path)
    try:
        yield socket_path
    finally:
        server.close()
        await server.wait_closed()
        shutil.rmtree(runtime_dir, ignore_errors=True)


async def _run(socket_path: str, argv: list[str], *, namespace: str = "ns", timeout: float = 30) -> str:  # noqa: ASYNC109
    return await run_command_via_supervisor(
        socket_path,
        namespace=namespace,
        argv=argv,
        env=_MINIMAL_ENV,
        cwd=None,
        tail=100,
        timeout=timeout,
    )


def _extract_handle(message: str) -> str:
    assert "Handle: " in message, message
    return message.split("Handle: ")[1].split("\n", maxsplit=1)[0]


async def _check(socket_path: str, handle: str, *, namespace: str = "ns") -> str:
    return await asyncio.to_thread(check_command_via_supervisor, socket_path, namespace=namespace, handle=handle)


async def _kill(socket_path: str, handle: str, *, namespace: str = "ns", force: bool = False) -> str:
    return await asyncio.to_thread(
        kill_command_via_supervisor,
        socket_path,
        namespace=namespace,
        handle=handle,
        force=force,
    )


async def _wait_for_finished(socket_path: str, handle: str) -> str:
    for _ in range(50):
        status = await _check(socket_path, handle)
        if "FINISHED" in status:
            return status
        await asyncio.sleep(0.1)
    message = f"Handle {handle} never finished: {status}"
    raise AssertionError(message)


async def _assert_pid_dead(pid: int) -> None:
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.05)
    message = f"Process {pid} is still alive"
    raise AssertionError(message)


# ---------------------------------------------------------------------------
# Server protocol
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_returns_output() -> None:
    """Fast commands should return their output through the supervisor."""
    registry: dict[str, ProcessRecord] = {}
    async with _running_server(registry) as socket_path:
        assert await _run(socket_path, ["echo", "hello supervisor"]) == "hello supervisor"


@pytest.mark.asyncio
async def test_run_nonzero_exit_returns_stderr() -> None:
    """Non-zero exits should surface stderr as an error message."""
    registry: dict[str, ProcessRecord] = {}
    async with _running_server(registry) as socket_path:
        result = await _run(socket_path, ["bash", "-c", "echo oops >&2; exit 1"])
        assert result.startswith("Error:")
        assert "oops" in result


@pytest.mark.asyncio
async def test_run_timeout_backgrounds_then_check_and_kill() -> None:
    """The full run→check→kill handle lifecycle should work over the socket."""
    registry: dict[str, ProcessRecord] = {}
    async with _running_server(registry) as socket_path:
        result = await _run(socket_path, ["bash", "-c", "echo bg-line; sleep 300"], timeout=0)
        assert "timed out" in result.lower()
        handle = _extract_handle(result)

        await asyncio.sleep(0.3)
        status = await _check(socket_path, handle)
        assert "RUNNING" in status
        assert "bg-line" in status

        kill_result = await _kill(socket_path, handle, force=True)
        assert "Force-killed" in kill_result

        assert "FINISHED" in await _wait_for_finished(socket_path, handle)


@pytest.mark.asyncio
async def test_handles_are_namespace_scoped() -> None:
    """Handles must not be visible to callers from another namespace."""
    registry: dict[str, ProcessRecord] = {}
    async with _running_server(registry) as socket_path:
        result = await _run(socket_path, ["sleep", "300"], namespace="ns-a", timeout=0)
        handle = _extract_handle(result)
        try:
            assert "Unknown handle" in await _check(socket_path, handle, namespace="ns-b")
            assert "Unknown handle" in await _kill(socket_path, handle, namespace="ns-b", force=True)
        finally:
            await _kill(socket_path, handle, namespace="ns-a", force=True)


@pytest.mark.asyncio
async def test_client_disconnect_cancels_foreground_run() -> None:
    """A client that dies mid-run must not leave the command running unsupervised."""
    registry: dict[str, ProcessRecord] = {}
    pid_dir = Path(tempfile.mkdtemp(prefix="mindroom-shell-pid-"))
    pid_file = pid_dir / "run.pid"
    script = (
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
        "time.sleep(300)"
    )
    try:
        async with _running_server(registry) as socket_path:
            _reader, writer = await asyncio.open_unix_connection(socket_path)
            request = {
                "op": "run",
                "namespace": "ns",
                "argv": [sys.executable, "-c", script, str(pid_file)],
                "env": _MINIMAL_ENV,
                "cwd": None,
                "tail": 100,
                "timeout": 300,
            }
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()

            pid: int | None = None
            for _ in range(50):
                if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip():
                    pid = int(pid_file.read_text(encoding="utf-8").strip())
                    break
                await asyncio.sleep(0.05)
            assert pid is not None

            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

            await _assert_pid_dead(pid)
            assert registry == {}
    finally:
        shutil.rmtree(pid_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_backgrounded_handle_is_discarded_when_client_died_in_same_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that backgrounds in the same loop cycle the client dies must not leak.

    When ``asyncio.wait`` reports the run and the client EOF as done together,
    the registered handle is undeliverable and the command must be killed.
    """
    registry: dict[str, ProcessRecord] = {}
    result = await run_command(
        registry,
        namespace="ns",
        argv=["sleep", "300"],
        env=_MINIMAL_ENV,
        cwd=None,
        tail=100,
        timeout=0,
    )
    assert result.handle is not None
    assert result.handle in registry
    pid = registry[result.handle].pid

    async def completed_run(*_args: object, **_kwargs: object) -> object:
        return result

    monkeypatch.setattr(shell_supervisor, "run_command", completed_run)
    eof_reader = asyncio.StreamReader()
    eof_reader.feed_eof()
    payload = {
        "op": "run",
        "namespace": "ns",
        "argv": ["sleep", "300"],
        "env": _MINIMAL_ENV,
        "cwd": None,
        "tail": 100,
        "timeout": 0,
    }

    message = await shell_supervisor._handle_run(registry, payload, eof_reader)

    assert message is None
    assert registry == {}
    await _assert_pid_dead(pid)


@pytest.mark.asyncio
async def test_unknown_operation_returns_error() -> None:
    """Unknown operations should produce an error message, not kill the server."""
    registry: dict[str, ProcessRecord] = {}
    async with _running_server(registry) as socket_path:
        reader, writer = await asyncio.open_unix_connection(socket_path)
        writer.write(json.dumps({"op": "nope"}).encode() + b"\n")
        await writer.drain()
        line = await reader.readline()
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        assert "Unknown shell supervisor operation" in json.loads(line)["message"]

        # The server keeps serving after the bad request.
        assert await _run(socket_path, ["echo", "still-alive"]) == "still-alive"


# ---------------------------------------------------------------------------
# Supervisor process + manager
# ---------------------------------------------------------------------------


@pytest.fixture
def manager() -> Iterator[_ShellSupervisorManager]:
    """Provide an isolated supervisor manager that is shut down after the test."""
    supervisor_manager = _ShellSupervisorManager()
    yield supervisor_manager
    supervisor_manager.shutdown()


@pytest.mark.asyncio
async def test_manager_spawns_supervisor_and_reuses_it(manager: _ShellSupervisorManager) -> None:
    """ensure() should spawn one real supervisor process and reuse it while alive."""
    socket_path = manager.ensure()
    assert await _run(socket_path, ["echo", "via-process"]) == "via-process"
    assert manager.ensure() == socket_path


@pytest.mark.asyncio
async def test_supervisor_terminate_kills_children_and_invalidates_handles(
    manager: _ShellSupervisorManager,
) -> None:
    """Stopping the supervisor must kill supervised processes; a respawn has no handles."""
    socket_path = manager.ensure()
    result = await _run(socket_path, ["sleep", "300"], timeout=0)
    handle = _extract_handle(result)
    assert "PID " in result
    pid = int(result.split("PID ")[1].split(")")[0])

    supervisor = manager._supervisor
    assert supervisor is not None
    supervisor.process.terminate()
    supervisor.process.wait(timeout=10)
    await _assert_pid_dead(pid)

    new_socket_path = manager.ensure()
    assert new_socket_path != socket_path
    assert "Unknown handle" in await _check(new_socket_path, handle)


@pytest.mark.asyncio
async def test_orphaned_supervisor_exits_and_kills_children() -> None:
    """A supervisor whose parent dies must clean up its children and socket dir."""
    runtime_dir = Path(tempfile.mkdtemp(prefix="mindroom-shell-orphan-"))
    socket_path = runtime_dir / "supervisor.sock"
    supervisor_pid_file = runtime_dir / "supervisor.pid"
    # Spawn the supervisor from a short-lived intermediate parent so it becomes
    # an orphan as soon as that parent exits.
    launcher = (
        "import pathlib, subprocess, sys, time\n"
        "process = subprocess.Popen([sys.executable, '-m', 'mindroom.shell_supervisor', sys.argv[1]])\n"
        "pathlib.Path(sys.argv[2]).write_text(str(process.pid), encoding='utf-8')\n"
        "deadline = time.monotonic() + 20\n"
        "while not pathlib.Path(sys.argv[1]).exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n"
    )
    launch = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        launcher,
        str(socket_path),
        str(supervisor_pid_file),
    )
    await launch.wait()
    supervisor_pid = int(supervisor_pid_file.read_text(encoding="utf-8").strip())

    try:
        await _assert_pid_dead(supervisor_pid)
        for _ in range(40):
            if not runtime_dir.exists():
                break
            await asyncio.sleep(0.1)
        assert not runtime_dir.exists()
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(supervisor_pid, signal.SIGKILL)
        shutil.rmtree(runtime_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Toolkit client mode
# ---------------------------------------------------------------------------


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


@pytest.mark.asyncio
async def test_toolkit_routes_through_supervisor_across_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manager: _ShellSupervisorManager,
) -> None:
    """With the socket env set, handles must work across fresh toolkit instances.

    This simulates the sandbox subprocess path where every run/check/kill
    request executes in a separate short-lived process.
    """
    monkeypatch.setenv(SHELL_SUPERVISOR_SOCKET_ENV, manager.ensure())

    tool_run = _get_toolkit(tmp_path)
    run_fn = tool_run.async_functions["run_shell_command"].entrypoint
    assert run_fn is not None
    result = await run_fn(["bash", "-c", "echo client-mode; sleep 300"], timeout=0)
    handle = _extract_handle(result)

    await asyncio.sleep(0.3)
    tool_check = _get_toolkit(tmp_path)
    check_fn = tool_check.functions["check_shell_command"].entrypoint
    assert check_fn is not None
    status = await asyncio.to_thread(check_fn, handle)
    assert "RUNNING" in status
    assert "client-mode" in status

    tool_kill = _get_toolkit(tmp_path)
    kill_fn = tool_kill.functions["kill_shell_command"].entrypoint
    assert kill_fn is not None
    kill_result = await asyncio.to_thread(kill_fn, handle, True)
    assert "Force-killed" in kill_result


@pytest.mark.asyncio
async def test_toolkit_reports_unavailable_supervisor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead supervisor socket should produce a clear error, not a crash."""
    monkeypatch.setenv(SHELL_SUPERVISOR_SOCKET_ENV, str(tmp_path / "missing.sock"))
    tool = _get_toolkit(tmp_path)
    run_fn = tool.async_functions["run_shell_command"].entrypoint
    assert run_fn is not None
    result = await run_fn(["echo", "hi"])
    assert result.startswith("Error: Shell supervisor is unavailable")


# ---------------------------------------------------------------------------
# Sandbox runner subprocess dispatch (end-to-end)
# ---------------------------------------------------------------------------


def test_subprocess_mode_shell_background_handle_across_requests(tmp_path: Path) -> None:
    """Background handles must survive per-request subprocess isolation.

    Each run/check/kill request executes in its own sandbox subprocess; the
    handle lives in the runner's shell supervisor, not in any request process.
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE": "subprocess"},
    )
    config = sandbox_runner_module._runtime_config_or_empty(runtime_paths)

    def execute(function_name: str, kwargs: dict[str, object]) -> str:
        response = sandbox_runner_module._execute_request_subprocess_sync(
            sandbox_runner_module.SandboxRunnerExecuteRequest(
                tool_name="shell",
                function_name=function_name,
                kwargs=kwargs,
            ),
            runtime_paths,
            config,
        )
        assert response.ok, response.error
        assert isinstance(response.result, str)
        return response.result

    result = execute("run_shell_command", {"args": ["bash", "-c", "echo bg-e2e; sleep 300"], "timeout": 0})
    handle = _extract_handle(result)

    status = execute("check_shell_command", {"handle": handle})
    assert "RUNNING" in status

    kill_result = execute("kill_shell_command", {"handle": handle, "force": True})
    assert "Force-killed" in kill_result


# ---------------------------------------------------------------------------
# Runner dispatch helpers
# ---------------------------------------------------------------------------


def test_shell_run_timeout_seconds_parses_kwargs() -> None:
    """The dispatch budget helper should read the requested foreground timeout."""

    def prepared(function_name: str, kwargs: dict[str, object]) -> object:
        return sandbox_runner_module.PreparedSandboxRunnerExecuteRequest(
            tool_name="shell",
            function_name=function_name,
            kwargs=kwargs,
        )

    helper = sandbox_runner_module._shell_run_timeout_seconds
    assert helper(prepared("run_shell_command", {"timeout": 300})) == 300.0
    assert helper(prepared("run_shell_command", {})) == 120.0
    assert helper(prepared("run_shell_command", {"timeout": "nope"})) == 120.0
    assert helper(prepared("check_shell_command", {"timeout": 300})) == 0.0


def test_shell_subprocess_dispatch_context_injects_socket_and_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Shell dispatch should advertise the supervisor socket and stretch the budget."""
    socket_path = str(tmp_path / "test.sock")
    monkeypatch.setattr(shell_supervisor, "ensure_shell_supervisor", lambda: socket_path)
    prepared = sandbox_runner_module.PreparedSandboxRunnerExecuteRequest(
        tool_name="shell",
        function_name="run_shell_command",
        kwargs={"timeout": 600},
    )
    subprocess_context = sandbox_runner_module._PreparedSandboxSubprocessContext(
        python_executable=sys.executable,
        subprocess_env={"PATH": "/usr/bin"},
        subprocess_cwd=None,
        template_env={"PATH": "/usr/bin"},
    )

    updated_context, timeout_seconds = sandbox_runner_module._shell_subprocess_dispatch_context(
        prepared,
        subprocess_context,
        120.0,
    )

    assert updated_context.subprocess_env is not None
    assert updated_context.subprocess_env[SHELL_SUPERVISOR_SOCKET_ENV] == socket_path
    assert updated_context.template_env == {"PATH": "/usr/bin"}
    assert timeout_seconds == 630.0
