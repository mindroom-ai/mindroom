"""Real shell output redirection across local and supervised execution."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

import pytest
from agno.tools.function import FunctionCall

from mindroom.api import sandbox_runner
from mindroom.constants import resolve_runtime_paths
from mindroom.shell_execution import kill_all_records, run_command
from mindroom.shell_output_capture import ShellOutputDestination
from mindroom.shell_supervisor import SHELL_SUPERVISOR_SOCKET_ENV, _ShellSupervisorManager
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.output_files import ToolOutputFilePolicy, wrap_toolkit_for_output_files
from mindroom.tools import shell as shell_module
from mindroom.tools.shell import shell_tools

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from agno.tools.toolkit import Toolkit

    from mindroom.shell_execution import ShellRunResult


@pytest.fixture(params=[False, True], ids=["local", "supervisor"])
def shell_toolkit(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Toolkit]:
    """Build the registered tool, optionally backed by a real worker supervisor."""
    manager = _ShellSupervisorManager()
    monkeypatch.delenv(SHELL_SUPERVISOR_SOCKET_ENV, raising=False)
    if request.param:
        monkeypatch.setenv(SHELL_SUPERVISOR_SOCKET_ENV, manager.ensure())
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    try:
        yield get_tool_by_name(
            "shell",
            runtime_paths,
            disable_sandbox_proxy=True,
            worker_target=None,
            tool_init_overrides={"base_dir": str(tmp_path)},
            tool_output_workspace_root=tmp_path,
        )
    finally:
        manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("start_fails", [False, True])
async def test_output_ownership_is_independent_of_message_wording(
    shell_toolkit: Toolkit,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start_fails: bool,
) -> None:
    """Changing a run's display text cannot skip an error file or overwrite captured output."""
    display_message = "Unable to start this command." if start_fails else "Error: this is only display text."
    execute = (
        shell_module.run_command_via_supervisor
        if os.environ.get(SHELL_SUPERVISOR_SOCKET_ENV)
        else shell_module.run_command
    )

    def reword[**P](run: Callable[P, Awaitable[ShellRunResult]]) -> Callable[P, Awaitable[ShellRunResult]]:
        async def reworded_run(*args: P.args, **kwargs: P.kwargs) -> ShellRunResult:
            result = await run(*args, **kwargs)
            return replace(result, message=display_message)

        return reworded_run

    monkeypatch.setattr(shell_module, execute.__name__, reword(execute))
    destination = tmp_path / "wording.txt"
    destination.write_text("previous contents")
    await FunctionCall(
        function=shell_toolkit.async_functions["run_shell_command"],
        arguments={
            "args": [str(tmp_path / "missing-command")] if start_fails else "printf complete",
            "mindroom_output_path": "wording.txt",
        },
    ).aexecute()

    expected = display_message if start_fails else "complete"
    assert destination.read_text().split("\n", 1)[-1] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("lines", "failure"), [(125000, False), (250000, False), (125000, True)])
async def test_redirect_saves_complete_shell_output(
    shell_toolkit: Toolkit,
    tmp_path: Path,
    lines: int,
    failure: bool,
) -> None:
    """The save contract survives both the ring-buffer and tail limits, including worker IPC."""
    command = f"awk 'BEGIN {{ for (i=0; i<{lines}; i++) print \"01234567890123456789\" }}'"
    if failure:
        command += " >&2; exit 1"
    result = await FunctionCall(
        function=shell_toolkit.async_functions["run_shell_command"],
        arguments={"args": command, "tail": 2, "mindroom_output_path": "captured.txt"},
    ).aexecute()

    assert result.status == "success"
    saved = (tmp_path / "captured.txt").read_text()
    assert saved.count("01234567890123456789") == lines
    assert "truncated" not in saved
    assert len(str(result.result)) < 1000


@pytest.mark.asyncio
async def test_plain_shell_output_still_honors_tail(shell_toolkit: Toolkit) -> None:
    """An explicit redirect must not change later ordinary calls on the same toolkit."""
    result = await FunctionCall(
        function=shell_toolkit.async_functions["run_shell_command"],
        arguments={"args": "printf 'one\\ntwo\\nthree\\n'", "tail": 2},
    ).aexecute()

    assert result.status == "success"
    assert str(result.result).splitlines()[1:] == ["two", "three"]


@pytest.mark.asyncio
async def test_background_shell_publishes_original_destination(shell_toolkit: Toolkit, tmp_path: Path) -> None:
    """The original output request survives the shell's native timeout and toolkit polling."""
    result = await FunctionCall(
        function=shell_toolkit.async_functions["run_shell_command"],
        arguments={
            "args": "sleep 0.2; awk 'BEGIN { for(i=0;i<10000;i++) print \"complete-line\" }'",
            "timeout": 0,
            "mindroom_output_path": "eventual.txt",
        },
    ).aexecute()
    message = str(result.result)
    assert "Handle: shell:" in message
    assert not (tmp_path / "eventual.txt").exists()
    handle = message.split("Handle: ")[1].splitlines()[0]
    check = shell_toolkit.functions["check_shell_command"].entrypoint
    assert check is not None
    async with asyncio.timeout(10):
        while True:
            status = await asyncio.to_thread(check, handle)
            if "saved_to_file" in status:
                break
            await asyncio.sleep(0.02)
    assert json.loads(status)["mindroom_tool_output"]["path"] == "eventual.txt"
    assert (tmp_path / "eventual.txt").read_text().count("complete-line") == 10000


@pytest.mark.asyncio
@pytest.mark.usefixtures("shell_toolkit")
async def test_capture_limit_rejects_incomplete_file(tmp_path: Path) -> None:
    """Output over the configured cap must not replace an existing file with a truncated success."""
    tool = shell_tools()(
        base_dir=tmp_path,
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "storage",
            process_env={},
        ),
    )
    wrap_toolkit_for_output_files(tool, ToolOutputFilePolicy(workspace_root=tmp_path, max_bytes=60000))
    destination = tmp_path / "existing.txt"
    destination.write_text("keep existing")
    result = await FunctionCall(
        function=tool.async_functions["run_shell_command"],
        arguments={
            "args": "awk 'BEGIN { for(i=0;i<10000;i++) print \"too-much-output\" }'",
            "mindroom_output_path": "existing.txt",
        },
    ).aexecute()

    assert "exceeds" in str(result.result)
    assert "saved_to_file" not in str(result.result)
    assert destination.read_text() == "keep existing"


@pytest.mark.asyncio
async def test_background_publication_rechecks_destination(shell_toolkit: Toolkit, tmp_path: Path) -> None:
    """Replacing a destination parent with an escaping symlink while running cannot write outside the workspace."""
    parent = tmp_path / "reports"
    parent.mkdir()
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir()
    result = await FunctionCall(
        function=shell_toolkit.async_functions["run_shell_command"],
        arguments={
            "args": "while [ ! -f release ]; do sleep 0.01; done; printf complete",
            "timeout": 0,
            "mindroom_output_path": "reports/output.txt",
        },
    ).aexecute()
    handle = str(result.result).split("Handle: ")[1].splitlines()[0]
    parent.rmdir()
    parent.symlink_to(outside, target_is_directory=True)
    (tmp_path / "release").touch()
    check = shell_toolkit.functions["check_shell_command"].entrypoint
    assert check is not None
    async with asyncio.timeout(10):
        while True:
            status = await asyncio.to_thread(check, handle)
            if '"status": "error"' in status:
                break
            await asyncio.sleep(0.02)
    assert not (outside / "output.txt").exists()
    assert "saved_to_file" not in status


@pytest.mark.asyncio
async def test_cancelled_capture_does_not_publish(shell_toolkit: Toolkit, tmp_path: Path) -> None:
    """Cancellation kills an active process and cannot publish its incomplete output."""
    run = shell_toolkit.async_functions["run_shell_command"].entrypoint
    assert run is not None
    task = asyncio.create_task(
        run(
            args="echo $$ > process.pid; printf partial; exec sleep 300",
            mindroom_output_path="cancelled.txt",
        ),
    )
    async with asyncio.timeout(10):
        while not (tmp_path / "process.pid").exists():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    pid = int((tmp_path / "process.pid").read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with asyncio.timeout(10):
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.01)
    assert not (tmp_path / "cancelled.txt").exists()


@pytest.mark.asyncio
async def test_killed_capture_preserves_existing_destination(shell_toolkit: Toolkit, tmp_path: Path) -> None:
    """Stopping a shell cannot replace the destination with incomplete stderr."""
    destination = tmp_path / "killed.txt"
    destination.write_text("keep existing")
    result = await FunctionCall(
        function=shell_toolkit.async_functions["run_shell_command"],
        arguments={
            "args": "printf partial >&2; touch started; exec sleep 300",
            "timeout": 0,
            "mindroom_output_path": "killed.txt",
        },
    ).aexecute()
    handle = str(result.result).split("Handle: ")[1].splitlines()[0]
    async with asyncio.timeout(10):
        while not (tmp_path / "started").exists():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    kill = shell_toolkit.functions["kill_shell_command"].entrypoint
    check = shell_toolkit.functions["check_shell_command"].entrypoint
    assert kill is not None
    assert check is not None
    await asyncio.to_thread(kill, handle)
    async with asyncio.timeout(10):
        while True:
            status = await asyncio.to_thread(check, handle)
            if "RUNNING" not in status:
                break
            await asyncio.sleep(0.02)
    assert "saved_to_file" not in status
    assert destination.read_text() == "keep existing"


@pytest.mark.skipif(not Path("/dev/full").exists(), reason="Requires a real failing storage device")
@pytest.mark.asyncio
async def test_spool_failure_settles_background_handle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real disk-write/flush failure reports an error and closes both spools without stranding the handle."""
    streams: list[BinaryIO] = []

    def full_device(**_kwargs: object) -> BinaryIO:
        stream = open("/dev/full", "w+b")  # noqa: SIM115, PTH123
        streams.append(stream)
        return stream

    monkeypatch.setattr("mindroom.shell_output_capture.tempfile.TemporaryFile", full_device)
    registry = {}
    result = await run_command(
        registry,
        namespace="disk-failure",
        # Outlive spawn so timeout=0 always backgrounds; a command that already exited returns in the foreground.
        argv=["bash", "-c", "printf data; sleep 0.5"],
        env={"PATH": os.environ["PATH"]},
        cwd=str(tmp_path),
        tail=100,
        timeout=0,
        output_destination=ShellOutputDestination(str(tmp_path), "missing.txt", 10000),
    )
    assert result.handle is not None
    record = registry[result.handle]
    assert record._monitor_task is not None
    try:
        await record._monitor_task
        assert record.finished
        assert '"status": "error"' in str(record.output_receipt)
        assert all(stream.closed for stream in streams)
        assert not (tmp_path / "missing.txt").exists()
    finally:
        for stream in streams:
            with contextlib.suppress(OSError):
                stream.close()


@pytest.mark.asyncio
async def test_spawn_cancellation_closes_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancellation before process creation finishes must release both already-opened spools."""
    spawning = asyncio.Event()
    streams: list[BinaryIO] = []
    temporary_file = tempfile.TemporaryFile

    def observed_file(*, dir: str) -> BinaryIO:  # noqa: A002
        stream = temporary_file(dir=dir)
        streams.append(stream)
        return stream

    async def blocked_spawn(*_args: object, **_kwargs: object) -> None:
        spawning.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("mindroom.shell_output_capture.tempfile.TemporaryFile", observed_file)
    monkeypatch.setattr("mindroom.shell_execution.asyncio.create_subprocess_exec", blocked_spawn)
    task = asyncio.create_task(
        run_command(
            {},
            namespace="cancel-spawn",
            argv=["true"],
            env={},
            cwd=str(tmp_path),
            tail=100,
            timeout=30,
            output_destination=ShellOutputDestination(str(tmp_path), "never.txt", 10000),
        ),
    )
    await spawning.wait()
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(streams) == 2
        assert all(stream.closed for stream in streams)
        assert not (tmp_path / "never.txt").exists()
    finally:
        for stream in streams:
            stream.close()


def test_worker_subprocess_preserves_background_capture(tmp_path: Path) -> None:
    """A request process can exit while its supervisor retains capture and publishes the complete file."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\n"
        "agents:\n  probe:\n    display_name: Probe\n    role: Test shell output\n    tools: [shell]\nrouter:\n  model: default\nmemory:\n  backend: file\n",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE": "subprocess"},
    )
    config = sandbox_runner._runtime_config_or_empty(runtime_paths)
    workspace = tmp_path / "storage/agents/probe/workspace"

    def execute(function_name: str, kwargs: dict[str, object]) -> str:
        response = sandbox_runner._execute_request_subprocess_sync(
            sandbox_runner.SandboxRunnerExecuteRequest(
                tool_name="shell",
                function_name=function_name,
                kwargs=kwargs,
                routing_agent_name="probe",
                tool_init_overrides={"base_dir": str(workspace)},
            ),
            runtime_paths,
            config,
        )
        assert response.ok, response.error
        assert isinstance(response.result, str)
        return response.result

    initial = execute(
        "run_shell_command",
        {
            "args": "sleep 1; awk 'BEGIN { for(i=0;i<125000;i++) print \"0123456789012345678\" }'",
            "tail": 2,
            "timeout": 0,
            "mindroom_output_path": "worker.txt",
        },
    )
    handle = initial.split("Handle: ")[1].splitlines()[0]
    deadline = time.monotonic() + 10
    while True:
        status = execute("check_shell_command", {"handle": handle})
        if "RUNNING" not in status:
            break
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert "saved_to_file" in status
    saved = (workspace / "worker.txt").read_bytes().split(b"\n", 1)[1]
    assert saved == b"0123456789012345678\n" * 125000


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_seconds", [0, 30])
@pytest.mark.parametrize("stderr", [False, True])
async def test_reader_failure_never_publishes_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wait_seconds: int,
    stderr: bool,
) -> None:
    """A pipe-read failure after real bytes arrive must fail closed in either lifecycle."""
    original_read = asyncio.StreamReader.read
    failed_streams: set[asyncio.StreamReader] = set()

    async def failing_read(stream: asyncio.StreamReader, n: int = -1) -> bytes:
        if stream in failed_streams:
            message = "pipe read failed"
            raise OSError(message)
        data = await original_read(stream, n)
        if data:
            failed_streams.add(stream)
        return data

    monkeypatch.setattr(asyncio.StreamReader, "read", failing_read)
    destination = tmp_path / "partial.txt"
    destination.write_text("keep existing")
    registry = {}
    result = await run_command(
        registry,
        namespace="reader-failure",
        argv=["bash", "-c", "printf partial" + (" >&2" if stderr else "")],
        env={"PATH": os.environ["PATH"]},
        cwd=str(tmp_path),
        tail=100,
        timeout=wait_seconds,
        output_destination=ShellOutputDestination(str(tmp_path), "partial.txt", 10000),
    )
    if result.handle is not None:
        record = registry[result.handle]
        assert record._monitor_task is not None
        await record._monitor_task
        assert record.finished
        message = record.output_receipt
    else:
        message = result.message
    assert '"status": "error"' in str(message)
    assert destination.read_text() == "keep existing"


@pytest.mark.asyncio
async def test_spool_creation_failure_returns_shell_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A spool allocation failure must return through the shell protocol without executing the command."""

    def fail_spool(**_kwargs: object) -> BinaryIO:
        message = "Cannot create capture spool"
        raise OSError(message)

    monkeypatch.setattr("mindroom.shell_output_capture.tempfile.TemporaryFile", fail_spool)
    result = await run_command(
        {},
        namespace="spool-create",
        argv=["touch", "ran"],
        env={},
        cwd=str(tmp_path),
        tail=100,
        timeout=30,
        output_destination=ShellOutputDestination(str(tmp_path), "output.txt", 10000),
    )
    assert result.message.startswith("Error:")
    assert "Cannot create capture spool" in result.message
    assert not (tmp_path / "ran").exists()


@pytest.mark.asyncio
async def test_spawn_error_preserves_generic_redirection(shell_toolkit: Toolkit, tmp_path: Path) -> None:
    """A command that cannot start still redirects its ordinary shell error result."""
    result = await FunctionCall(
        function=shell_toolkit.async_functions["run_shell_command"],
        arguments={"args": [str(tmp_path / "missing-command")], "mindroom_output_path": "error.txt"},
    ).aexecute()
    assert "saved_to_file" in str(result.result)
    assert "Error:" in (tmp_path / "error.txt").read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_seconds", [0, 30])
async def test_publication_io_failure_settles_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wait_seconds: int,
) -> None:
    """A filesystem error during destination revalidation must settle with an error receipt."""
    original_resolve = Path.resolve

    def failing_resolve(path: Path, strict: bool = False) -> Path:
        if path == tmp_path:
            message = "destination became inaccessible"
            raise PermissionError(message)
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", failing_resolve)
    registry = {}
    result = await run_command(
        registry,
        namespace="publication-failure",
        argv=["bash", "-c", "printf complete"],
        env={"PATH": os.environ["PATH"]},
        cwd=str(tmp_path),
        tail=100,
        timeout=wait_seconds,
        output_destination=ShellOutputDestination(str(tmp_path), "missing.txt", 10000),
    )
    if result.handle is not None:
        record = registry[result.handle]
        assert record._monitor_task is not None
        await record._monitor_task
        assert record.finished
        message = record.output_receipt
    else:
        message = result.message
    assert '"status": "error"' in str(message)
    assert not (tmp_path / "missing.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_seconds", [0, 30])
async def test_inherited_pipe_cannot_publish_partial_capture(
    shell_toolkit: Toolkit,
    tmp_path: Path,
    wait_seconds: int,
) -> None:
    """A descendant retaining a pipe beyond the existing grace period must not overwrite the destination."""
    destination = tmp_path / "inherited.txt"
    destination.write_text("keep existing")
    try:
        async with asyncio.timeout(10):
            result = await FunctionCall(
                function=shell_toolkit.async_functions["run_shell_command"],
                arguments={
                    "args": "sleep 30 & echo $! > child.pid; printf early; sleep 0.05",
                    "timeout": wait_seconds,
                    "mindroom_output_path": "inherited.txt",
                },
            ).aexecute()
            status = str(result.result)
            if wait_seconds == 0:
                handle = status.split("Handle: ")[1].splitlines()[0]
                check = shell_toolkit.functions["check_shell_command"].entrypoint
                assert check is not None
                while True:
                    status = await asyncio.to_thread(check, handle)
                    if "Status: RUNNING" not in status:
                        break
                    await asyncio.sleep(0.02)
        assert '"status": "error"' in status
        assert "saved_to_file" not in status
        assert destination.read_text() == "keep existing"
    finally:
        pid_file = tmp_path / "child.pid"
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)


@pytest.mark.asyncio
async def test_background_capture_includes_descendant_output_within_grace(
    shell_toolkit: Toolkit,
    tmp_path: Path,
) -> None:
    """A short-lived descendant can finish writing before background process-group cleanup."""
    result = await FunctionCall(
        function=shell_toolkit.async_functions["run_shell_command"],
        arguments={
            "args": "(sleep 0.2; printf late) & printf early; sleep 0.05",
            "timeout": 0,
            "mindroom_output_path": "descendant.txt",
        },
    ).aexecute()
    handle = str(result.result).split("Handle: ")[1].splitlines()[0]
    check = shell_toolkit.functions["check_shell_command"].entrypoint
    assert check is not None
    async with asyncio.timeout(10):
        while True:
            status = await asyncio.to_thread(check, handle)
            if "Status: RUNNING" not in status:
                break
            await asyncio.sleep(0.02)
    assert "saved_to_file" in status
    assert (tmp_path / "descendant.txt").read_text().split("\n", 1)[-1] == "earlylate"


@pytest.mark.asyncio
async def test_closed_pipe_failure_settles_large_producer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real pipe closure plus reader error must settle even when the producer exceeds pipe capacity."""
    original_read = asyncio.StreamReader.read
    failed_streams: set[asyncio.StreamReader] = set()

    async def failing_read(stream: asyncio.StreamReader, n: int = -1) -> bytes:
        data = await original_read(stream, n)
        if data and stream not in failed_streams:
            failed_streams.add(stream)
            transport = stream._transport
            assert transport is not None
            transport.close()
            message = "pipe read failed"
            stream.set_exception(OSError(message))
        return data

    monkeypatch.setattr(asyncio.StreamReader, "read", failing_read)
    registry = {}
    result = await run_command(
        registry,
        namespace="blocked-producer",
        argv=["bash", "-c", "awk 'BEGIN { for(i=0;i<125000;i++) print \"0123456789012345678\" }'"],
        env={"PATH": os.environ["PATH"]},
        cwd=str(tmp_path),
        tail=100,
        timeout=0,
        output_destination=ShellOutputDestination(str(tmp_path), "missing.txt", 3000000),
    )
    assert result.handle is not None
    record = registry[result.handle]
    assert record._monitor_task is not None
    try:
        await asyncio.wait_for(asyncio.shield(record._monitor_task), timeout=10)
        assert record.finished
        assert record.process.returncode is not None
        assert '"status": "error"' in str(record.output_receipt)
        assert not (tmp_path / "missing.txt").exists()
    finally:
        kill_all_records(registry)
        with contextlib.suppress(asyncio.CancelledError):
            await record._monitor_task
