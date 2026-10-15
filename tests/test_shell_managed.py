"""Standard-mode shell commands that run to completion as one call, so a background job owns their whole run."""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import TYPE_CHECKING

import pytest
from agno.tools.function import FunctionCall

from mindroom.constants import resolve_runtime_paths
from mindroom.shell import managed
from mindroom.shell.execution import kill_all_records
from mindroom.shell.supervisor import SHELL_SUPERVISOR_SOCKET_ENV, _ShellSupervisorManager
from mindroom.tool_jobs.control import JobControl, job_control_context
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tools.shell import _process_registry

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from agno.tools.toolkit import Toolkit


@pytest.fixture(params=[False, True], ids=["local", "supervisor"])
def shell_toolkit(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Toolkit]:
    """Build the registered shell with fast slices, optionally backed by a real worker supervisor."""
    monkeypatch.setattr(managed, "_SLICE_SECONDS", 1)
    monkeypatch.setattr(managed, "_POLL_START_SECONDS", 0.05)
    monkeypatch.setattr(managed, "_POLL_MAX_SECONDS", 0.05)
    supervisor = _ShellSupervisorManager()
    monkeypatch.delenv(SHELL_SUPERVISOR_SOCKET_ENV, raising=False)
    if request.param:
        monkeypatch.setenv(SHELL_SUPERVISOR_SOCKET_ENV, supervisor.ensure())
    toolkit = _native_shell(tmp_path)
    assert managed.install_managed_shell([toolkit])
    try:
        yield toolkit
    finally:
        kill_all_records(_process_registry)
        supervisor.shutdown()


def _native_shell(tmp_path: Path) -> Toolkit:
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    return get_tool_by_name(
        "shell",
        runtime_paths,
        disable_sandbox_proxy=True,
        worker_target=None,
        tool_init_overrides={"base_dir": str(tmp_path)},
        tool_output_workspace_root=tmp_path,
    )


async def _run(toolkit: Toolkit, arguments: dict[str, object], *, as_job: bool = True) -> object:
    call = FunctionCall(function=toolkit.get_async_functions()["run_shell_command"], arguments=arguments)
    with job_control_context(JobControl()) if as_job else contextlib.nullcontext():
        await call.aexecute()
    return call.result


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _read_pids(path: Path) -> list[int]:
    for _ in range(100):
        if path.exists() and len(path.read_text().split()) == 2:
            return [int(pid) for pid in path.read_text().split()]
        await asyncio.sleep(0.02)
    raise AssertionError(path)


async def _until_gone(pids: list[int]) -> None:
    for _ in range(100):
        if not any(_alive(pid) for pid in pids):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(pids)


@pytest.mark.asyncio
async def test_a_short_command_returns_its_output(shell_toolkit: Toolkit) -> None:
    """A command that finishes within the slice returns its output directly."""
    result = await _run(shell_toolkit, {"args": "echo hi"})

    assert "hi" in str(result)
    assert "Handle:" not in str(result)


@pytest.mark.asyncio
async def test_a_command_longer_than_the_slice_returns_its_final_output(shell_toolkit: Toolkit) -> None:
    """A command that outlives the slice still returns its final output, not a handle."""
    result = await _run(shell_toolkit, {"args": "sleep 1.6; echo done"})

    assert "done" in str(result)
    assert "Handle:" not in str(result)
    assert str(result).startswith("[cwd: ")


@pytest.mark.asyncio
async def test_cancelling_while_polling_stops_the_whole_command(shell_toolkit: Toolkit, tmp_path: Path) -> None:
    """Cancelling the call after the slice stops the command and the processes it started."""
    pids = tmp_path / "pids"
    command = f"sleep 30 & echo $$ $! > {pids}; wait"
    call = asyncio.create_task(_run(shell_toolkit, {"args": command}))
    recorded = await _read_pids(pids)
    await asyncio.sleep(1.3)  # Past the slice, so the call is polling.

    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    await _until_gone(recorded)


@pytest.mark.asyncio
async def test_cancelling_force_stops_a_command_that_ignores_terminate(shell_toolkit: Toolkit, tmp_path: Path) -> None:
    """A command that ignores SIGTERM is force-killed when its call is cancelled."""
    pids = tmp_path / "pids"
    command = f"trap '' TERM; sleep 30 & echo $$ $! > {pids}; wait"
    call = asyncio.create_task(_run(shell_toolkit, {"args": command}))
    recorded = await _read_pids(pids)
    await asyncio.sleep(1.3)

    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    await _until_gone(recorded)


@pytest.mark.asyncio
async def test_a_command_whose_handle_disappears_fails(shell_toolkit: Toolkit, monkeypatch: pytest.MonkeyPatch) -> None:
    """A command whose runtime lost its handle fails, warning that it may have partly run."""
    parse = managed.parse_shell_supervisor_status
    monkeypatch.setattr(managed, "parse_shell_supervisor_status", lambda _message: parse("Error: Unknown handle 'x'"))
    call = FunctionCall(
        function=shell_toolkit.get_async_functions()["run_shell_command"],
        arguments={"args": "sleep 1.6"},
    )

    with job_control_context(JobControl()):
        await call.aexecute()

    assert "may have partly run" in str(call.error or call.result)


@pytest.mark.asyncio
async def test_a_timeout_from_an_older_call_is_ignored(shell_toolkit: Toolkit) -> None:
    """A model repeating the native timeout argument from older history still runs the command."""
    call = FunctionCall(
        function=shell_toolkit.get_async_functions()["run_shell_command"],
        arguments={"args": "echo hi", "timeout": 5},
    )
    with job_control_context(JobControl()):
        await call.aexecute()

    assert call.error is None
    assert "hi" in str(call.result)


def test_a_shell_configured_without_check_and_kill_stays_native(tmp_path: Path) -> None:
    """A shell whose settings remove its check and kill functions keeps its native run instead of disappearing."""
    toolkit = _native_shell(tmp_path)
    toolkit.functions.pop("check_shell_command")
    toolkit.async_functions.pop("check_shell_command", None)

    assert not managed.install_managed_shell([toolkit])
    assert "timeout" in toolkit.get_async_functions()["run_shell_command"].parameters["properties"]


@pytest.mark.asyncio
async def test_large_running_output_does_not_end_the_call_early(shell_toolkit: Toolkit) -> None:
    """Output big enough to be saved to a file never makes a still-running command look finished."""
    started = asyncio.get_running_loop().time()

    result = await _run(shell_toolkit, {"args": "printf '%51200s' x; sleep 2.5; echo done"})

    assert asyncio.get_running_loop().time() - started >= 2.5
    assert result is not None


@pytest.mark.asyncio
async def test_a_failed_poll_stops_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When polling fails, the command is stopped instead of running on without its job."""
    monkeypatch.setattr(managed, "_SLICE_SECONDS", 1)
    monkeypatch.setattr(managed, "_POLL_START_SECONDS", 0.05)
    monkeypatch.delenv(SHELL_SUPERVISOR_SOCKET_ENV, raising=False)
    toolkit = _native_shell(tmp_path)
    check = toolkit.functions["check_shell_command"]
    native_check = check.entrypoint
    assert native_check is not None

    def failing_check(handle: str, output: bool = True) -> str:
        if not output:
            msg = "worker request failed"
            raise RuntimeError(msg)
        return native_check(handle)

    check.entrypoint = failing_check
    assert managed.install_managed_shell([toolkit])
    pids = tmp_path / "pids"
    try:
        call = FunctionCall(
            function=toolkit.get_async_functions()["run_shell_command"],
            arguments={"args": f"sleep 30 & echo $$ $! > {pids}; wait"},
        )
        with job_control_context(JobControl()):
            await call.aexecute()

        assert "worker request failed" in str(call.error)
        await _until_gone(await _read_pids(pids))
    finally:
        kill_all_records(_process_registry)


@pytest.mark.asyncio
async def test_a_redirected_long_command_saves_its_whole_output(shell_toolkit: Toolkit, tmp_path: Path) -> None:
    """A redirected command that outlives the slice saves its whole output and returns the receipt."""
    result = await _run(shell_toolkit, {"args": "sleep 1.6; echo saved", "mindroom_output_path": "out.txt"})

    assert "saved" in (tmp_path / "out.txt").read_text()
    assert "out.txt" in str(result)


@pytest.mark.asyncio
async def test_a_call_that_is_not_a_job_is_stopped_at_its_limit(
    shell_toolkit: Toolkit,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call outside any background job is stopped at its limit instead of holding its reply forever."""
    monkeypatch.setattr(managed, "_NOT_A_JOB_LIMIT_SECONDS", 2)
    pids = tmp_path / "pids"

    result = await _run(shell_toolkit, {"args": f"sleep 30 & echo $$ $! > {pids}; wait"}, as_job=False)

    assert "cannot run in the background" in str(result)
    await _until_gone(await _read_pids(pids))


def test_the_model_sees_one_shell_function_without_a_timeout(shell_toolkit: Toolkit) -> None:
    """The model sees only run_shell_command, without timeout or handle instructions."""
    functions = shell_toolkit.get_async_functions()

    assert set(functions) == {"run_shell_command"}
    run = functions["run_shell_command"]
    assert "timeout" not in run.parameters["properties"]
    assert "mindroom_output_path" in run.parameters["properties"]
    assert "check_shell_command" not in (run.description or "")
