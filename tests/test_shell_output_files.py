"""Real shell output redirection across local and supervised execution."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import stat
import sys
import tempfile
import threading
import time
from ast import literal_eval
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

import pytest

from mindroom.agent_cli.projection import _INLINE_BUDGET, project_cli_result
from mindroom.api import sandbox_runner
from mindroom.constants import DEFAULT_TOOL_OUTPUT_MAX_BYTES, resolve_runtime_paths
from mindroom.custom_tools.filesystem import FilesystemTools
from mindroom.shell import output_capture
from mindroom.shell.background_scope import shell_background_scope
from mindroom.shell.output import ShellOutputBuffer, SpoolCapture
from mindroom.shell.output_capture import (
    PublishedShellSpill,
    ShellOutputDestination,
    ShellOutputSpill,
    ShellSpillPolicy,
    format_named_shell_spill,
)
from mindroom.shell.registry import ProcessRegistry
from mindroom.shell.runner import ActiveCommand, run_foreground
from mindroom.shell.supervisor import SHELL_SUPERVISOR_SOCKET_ENV, _ShellSupervisorManager
from mindroom.tool_system.invocation import ToolCallContext, client_tool_result, execute_tool_call
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.output_files import ToolOutputFilePolicy
from mindroom.tool_system.progress import structured_tool_result
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.tool_function import ToolOutcome
from mindroom.tool_system.toolkits import configure_toolkit_functions, toolkit_function
from mindroom.tools import shell as shell_module
from mindroom.tools.shell import shell_tools
from tests.api.sandbox_runner_support import execute_call

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from mindroom.shell.runner import ShellRunResult
    from mindroom.tool_system.authoring import Toolkit
    from mindroom.workers.protocol import ExecuteResponse


@dataclass(frozen=True)
class _Executed:
    """How one shell call ended: its result, or the error it raised."""

    status: str
    result: object


async def _run(toolkit: Toolkit, arguments: dict[str, object]) -> _Executed:
    """Run ``run_shell_command`` with ``arguments`` as an agent's run calls it."""
    function = toolkit_function(toolkit, "run_shell_command")
    assert function is not None
    try:
        return _Executed("success", await function.entrypoint(**arguments))
    except Exception as error:
        return _Executed("failure", error)


@pytest.fixture(params=[False, True], ids=["local", "supervisor"])
def shell_toolkit(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Toolkit]:
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
            runs_in_worker=False,
            worker_target=None,
            tool_init_overrides={"base_dir": str(tmp_path)},
            tool_output_workspace_root=tmp_path,
            tools=builtin_tool_registry(),
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
        else shell_module.run_foreground
    )

    def reword[**P](run: Callable[P, Awaitable[ShellRunResult]]) -> Callable[P, Awaitable[ShellRunResult]]:
        async def reworded_run(*args: P.args, **kwargs: P.kwargs) -> ShellRunResult:
            result = await run(*args, **kwargs)
            return replace(result, message=display_message)

        return reworded_run

    monkeypatch.setattr(shell_module, execute.__name__, reword(execute))
    destination = tmp_path / "wording.txt"
    destination.write_text("previous contents")
    await _run(
        shell_toolkit,
        {
            "args": [str(tmp_path / "missing-command")] if start_fails else "printf complete",
            "mindroom_output_path": "wording.txt",
        },
    )

    # The saved file holds the output alone, with no cwd line before it.
    assert destination.read_text() == (display_message if start_fails else "complete")


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
    result = await _run(shell_toolkit, {"args": command, "tail": 2, "mindroom_output_path": "captured.txt"})

    assert result.status == "success"
    saved = (tmp_path / "captured.txt").read_text()
    assert saved.count("01234567890123456789") == lines
    assert "truncated" not in saved
    assert len(str(result.result)) < 1000


@pytest.mark.asyncio
async def test_plain_shell_output_still_honors_tail(shell_toolkit: Toolkit) -> None:
    """An explicit redirect must not change later ordinary calls on the same toolkit."""
    result = await _run(shell_toolkit, {"args": "printf 'one\\ntwo\\nthree\\n'", "tail": 2})

    assert result.status == "success"
    assert str(result.result).splitlines() == [
        "[Showing the last 2 of 3 lines. Pass a larger tail, or narrow the command.]",
        "two",
        "three",
    ]


@pytest.mark.asyncio
async def test_nonzero_exit_leads_with_its_exit_code(shell_toolkit: Toolkit) -> None:
    """A failed command states its exit code before both output streams, locally and on a worker."""
    result = await _run(shell_toolkit, {"args": "echo partial; echo broken >&2; exit 7"})

    assert result.status == "success"
    assert isinstance(result.result, ToolOutcome)
    assert result.result.is_error
    assert result.result.content.splitlines() == ["Exit code 7", "Output:", "partial", "Stderr:", "broken"]


@pytest.mark.asyncio
async def test_a_saved_success_keeps_stdout_in_the_file_and_names_its_stderr_in_the_receipt(
    shell_toolkit: Toolkit,
    tmp_path: Path,
) -> None:
    """The file holds stdout exactly, and the receipt says how much stderr there was and nothing else of it."""
    result = await _run(shell_toolkit, {"args": "echo out; echo warn >&2", "mindroom_output_path": "out.txt"})

    receipt = json.loads(str(result.result))["mindroom_tool_output"]
    assert (tmp_path / "out.txt").read_text() == "out\n"
    assert {key: value for key, value in receipt.items() if key.startswith("stderr")} == {"stderr_bytes": 5}
    quiet = await _run(shell_toolkit, {"args": "echo out", "mindroom_output_path": "quiet.txt"})
    assert "stderr_bytes" not in json.loads(str(quiet.result))["mindroom_tool_output"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("shell_toolkit")
async def test_a_saved_success_counts_all_its_stderr_past_the_capture_cap(tmp_path: Path) -> None:
    """Stderr larger than the redirect cap, read in several chunks, is counted whole and the stdout file is saved."""
    tool = shell_tools()(
        base_dir=tmp_path,
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "storage",
            process_env={},
        ),
    )
    configure_toolkit_functions(tool, output_files=ToolOutputFilePolicy(workspace_root=tmp_path, max_bytes=64))
    result = await _run(
        tool,
        {"args": "echo out; head -c 30005 /dev/zero | tr '\\0' x >&2", "mindroom_output_path": "out.txt"},
    )

    receipt = json.loads(str(result.result))["mindroom_tool_output"]
    assert (receipt["status"], receipt["stderr_bytes"]) == ("saved_to_file", 30005)
    assert (tmp_path / "out.txt").read_text() == "out\n"


@pytest.mark.asyncio
async def test_redirect_preserves_stdout_receipt_on_nonzero_exit(shell_toolkit: Toolkit, tmp_path: Path) -> None:
    """Redirected nonterminal CLI output must use the same complete failure formatting."""
    result = await _run(
        shell_toolkit,
        {
            "args": 'printf \'{"status":"queued","call_id":"call-1"}\'; exit 3',
            "mindroom_output_path": "receipt.txt",
        },
    )

    assert result.status == "success"
    assert isinstance(result.result, ToolOutcome)
    assert (result.result.is_error, result.result.counts_as_tool_failure) == (True, False)
    assert json.loads(result.result.content)["mindroom_tool_output"]["exit_code"] == 3
    assert (tmp_path / "receipt.txt").read_text() == ('Exit code 3\nOutput:\n{"status":"queued","call_id":"call-1"}')


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
    configure_toolkit_functions(tool, output_files=ToolOutputFilePolicy(workspace_root=tmp_path, max_bytes=60000))
    destination = tmp_path / "existing.txt"
    destination.write_text("keep existing")
    result = await _run(
        tool,
        {
            "args": "awk 'BEGIN { for(i=0;i<10000;i++) print \"too-much-output\" }'",
            "mindroom_output_path": "existing.txt",
        },
    )

    assert "exceeds" in str(result.result)
    assert "saved_to_file" not in str(result.result)
    assert destination.read_text() == "keep existing"


@pytest.mark.asyncio
@pytest.mark.usefixtures("shell_toolkit")
async def test_capture_limit_error_keeps_the_exit_code(tmp_path: Path) -> None:
    """A failed command whose output is over the cap saves no file but still reports how it exited."""
    tool = shell_tools()(
        base_dir=tmp_path,
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "storage",
            process_env={},
        ),
    )
    configure_toolkit_functions(tool, output_files=ToolOutputFilePolicy(workspace_root=tmp_path, max_bytes=60000))
    destination = tmp_path / "existing.txt"
    destination.write_text("keep existing")
    result = await _run(
        tool,
        {
            "args": "awk 'BEGIN { for(i=0;i<10000;i++) print \"too-much-output\" }'; exit 3",
            "mindroom_output_path": "existing.txt",
        },
    )

    assert isinstance(result.result, ToolOutcome)
    assert result.result.is_error
    assert json.loads(result.result.content) == {
        "mindroom_tool_output": {
            "status": "error",
            "error": "Redirected shell output exceeds the 60000 byte limit.",
            "exit_code": 3,
        },
    }
    assert destination.read_text() == "keep existing"


@pytest.mark.asyncio
async def test_a_killed_command_reports_its_signal_exit_code(shell_toolkit: Toolkit, tmp_path: Path) -> None:
    """A command killed by a signal, such as the OOM killer's SIGKILL, saves no file but reports its exit code."""
    destination = tmp_path / "killed.txt"
    destination.write_text("keep existing")
    result = await _run(shell_toolkit, {"args": "echo building; kill -KILL $$", "mindroom_output_path": "killed.txt"})

    assert isinstance(result.result, ToolOutcome)
    assert result.result.is_error
    assert json.loads(result.result.content) == {
        "mindroom_tool_output": {
            "status": "error",
            "error": "Shell command or output capture was interrupted; no complete output file was saved.",
            "exit_code": -9,
        },
    }
    assert destination.read_text() == "keep existing"


@pytest.mark.asyncio
async def test_publication_rechecks_destination(shell_toolkit: Toolkit, tmp_path: Path) -> None:
    """Replacing a destination parent with an escaping symlink while running cannot write outside the workspace."""
    parent = tmp_path / "reports"
    parent.mkdir()
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir()
    running = asyncio.create_task(
        _run(
            shell_toolkit,
            {
                "args": "touch started; while [ ! -f release ]; do sleep 0.01; done; printf complete",
                "mindroom_output_path": "reports/output.txt",
            },
        ),
    )
    async with asyncio.timeout(10):
        while not (tmp_path / "started").exists():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    parent.rmdir()
    parent.symlink_to(outside, target_is_directory=True)
    (tmp_path / "release").touch()
    async with asyncio.timeout(10):
        status = str((await running).result)
    assert '"status": "error"' in status
    assert not (outside / "output.txt").exists()
    assert "saved_to_file" not in status


@pytest.mark.asyncio
async def test_cancelled_capture_does_not_publish(shell_toolkit: Toolkit, tmp_path: Path) -> None:
    """Cancellation kills an active process and cannot publish its incomplete output."""
    function = toolkit_function(shell_toolkit, "run_shell_command")
    assert function is not None
    run = function.entrypoint
    task = asyncio.create_task(
        run(
            args="echo $$ > process.pid; printf partial; exec sleep 300",
            mindroom_output_path="cancelled.txt",
        ),
    )
    pid_file = tmp_path / "process.pid"
    async with asyncio.timeout(10):
        while not (pid_file.exists() and pid_file.read_text()):  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    pid = int(pid_file.read_text())
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
async def test_a_stopped_command_keeps_the_existing_destination(shell_toolkit: Toolkit, tmp_path: Path) -> None:
    """A command stopped at its timeout keeps the destination and reports its output so far in the receipt."""
    destination = tmp_path / "killed.txt"
    destination.write_text("keep existing")
    result = await _run(
        shell_toolkit,
        {
            "args": "echo compiling; printf 'error: missing header' >&2; exec sleep 300",
            "timeout": 1,
            "mindroom_output_path": "killed.txt",
        },
    )
    assert isinstance(result.result, ToolOutcome)
    assert result.result.is_error
    assert json.loads(result.result.content) == {
        "mindroom_tool_output": {
            "status": "error",
            "error": "Stopped after 1s. No output file was saved.\nOutput:\ncompiling\nStderr:\nerror: missing header",
        },
    }
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

    monkeypatch.setattr("mindroom.shell.output.tempfile.TemporaryFile", full_device)
    registry = ProcessRegistry()
    result = await registry.run_or_background(
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
    record = registry._records[result.handle]
    assert record.monitor_task is not None
    try:
        await record.monitor_task
        assert record.finished
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

    monkeypatch.setattr("mindroom.shell.output.tempfile.TemporaryFile", observed_file)
    monkeypatch.setattr("mindroom.shell.process.asyncio.create_subprocess_exec", blocked_spawn)
    task = asyncio.create_task(
        run_foreground(
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


def test_worker_subprocess_saves_supervised_capture(tmp_path: Path) -> None:
    """A request process relays the call while its supervisor captures and publishes the complete file."""
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
            execute_call(
                runtime_paths,
                config,
                tool_name="shell",
                function_name=function_name,
                kwargs=kwargs,
                routing_agent_name="probe",
                base_dir=str(workspace),
            ),
        )
        assert response.ok, response.error
        assert isinstance(response.result, str)
        return response.result

    status = execute(
        "run_shell_command",
        {
            "args": "sleep 1; awk 'BEGIN { for(i=0;i<125000;i++) print \"0123456789012345678\" }'",
            "tail": 2,
            "mindroom_output_path": "worker.txt",
        },
    )
    assert "saved_to_file" in status
    assert (workspace / "worker.txt").read_bytes() == b"0123456789012345678\n" * 125000


def test_worker_subprocess_answers_a_structured_call_with_the_exit_status_and_the_output_as_text(
    tmp_path: Path,
) -> None:
    """The status crosses the supervisor, the request process and the wire; only a plain call's output is saved."""
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

    def execute(*, structured: bool) -> ExecuteResponse:
        return sandbox_runner._execute_request_subprocess_sync(
            execute_call(
                runtime_paths,
                config,
                tool_name="shell",
                function_name="run_shell_command",
                kwargs={"args": "head -c 51200 /dev/zero | tr '\\0' Z; exit 3", "timeout": 30},
                routing_agent_name="probe",
                base_dir=str(tmp_path / "storage/agents/probe/workspace"),
                structured_result=structured,
            ),
        )

    plain, structured = execute(structured=False), execute(structured=True)

    assert (plain.metadata, "mindroom_tool_output" in str(plain.result)) == (None, True)
    assert structured.metadata == {"exit_code": 3}
    assert ("mindroom_tool_output" in str(structured.result), str(structured.result).count("Z")) == (False, 51200)


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
    registry = ProcessRegistry()
    if wait_seconds == 0:
        result = await registry.run_or_background(
            namespace="reader-failure",
            argv=["bash", "-c", "printf partial" + (" >&2" if stderr else "")],
            env={"PATH": os.environ["PATH"]},
            cwd=str(tmp_path),
            tail=100,
            timeout=0,
            output_destination=ShellOutputDestination(str(tmp_path), "partial.txt", 10000),
        )
        if result.handle is not None:
            record = registry._records[result.handle]
            assert record.monitor_task is not None
            await record.monitor_task
            assert record.finished
        else:
            assert '"status": "error"' in result.message
    else:
        result = await run_foreground(
            argv=["bash", "-c", "printf partial" + (" >&2" if stderr else "")],
            env={"PATH": os.environ["PATH"]},
            cwd=str(tmp_path),
            tail=100,
            timeout=wait_seconds,
            output_destination=ShellOutputDestination(str(tmp_path), "partial.txt", 10000),
        )
        assert '"status": "error"' in result.message
    assert destination.read_text() == "keep existing"


@pytest.mark.asyncio
async def test_spool_creation_failure_returns_shell_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A spool allocation failure must return through the shell protocol without executing the command."""

    def fail_spool(**_kwargs: object) -> BinaryIO:
        message = "Cannot create capture spool"
        raise OSError(message)

    monkeypatch.setattr("mindroom.shell.output.tempfile.TemporaryFile", fail_spool)
    result = await run_foreground(
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
    result = await _run(
        shell_toolkit,
        {"args": [str(tmp_path / "missing-command")], "mindroom_output_path": "error.txt"},
    )
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
    registry = ProcessRegistry()
    if wait_seconds == 0:
        result = await registry.run_or_background(
            namespace="publication-failure",
            argv=["bash", "-c", "printf complete"],
            env={"PATH": os.environ["PATH"]},
            cwd=str(tmp_path),
            tail=100,
            timeout=0,
            output_destination=ShellOutputDestination(str(tmp_path), "missing.txt", 10000),
        )
        if result.handle is not None:
            record = registry._records[result.handle]
            assert record.monitor_task is not None
            await record.monitor_task
            assert record.finished
        else:
            assert '"status": "error"' in result.message
    else:
        result = await run_foreground(
            argv=["bash", "-c", "printf complete"],
            env={"PATH": os.environ["PATH"]},
            cwd=str(tmp_path),
            tail=100,
            timeout=wait_seconds,
            output_destination=ShellOutputDestination(str(tmp_path), "missing.txt", 10000),
        )
        assert '"status": "error"' in result.message
    assert not (tmp_path / "missing.txt").exists()


@pytest.mark.asyncio
async def test_inherited_pipe_cannot_publish_partial_capture(
    shell_toolkit: Toolkit,
    tmp_path: Path,
) -> None:
    """A descendant retaining a pipe beyond the existing grace period must not overwrite the destination."""
    destination = tmp_path / "inherited.txt"
    destination.write_text("keep existing")
    try:
        async with asyncio.timeout(10):
            result = await _run(
                shell_toolkit,
                {
                    "args": "sleep 30 & echo $! > child.pid; printf early; sleep 0.05",
                    "mindroom_output_path": "inherited.txt",
                },
            )
            status = str(result.result)
        assert '"status": "error"' in status
        assert "saved_to_file" not in status
        assert destination.read_text() == "keep existing"
    finally:
        pid_file = tmp_path / "child.pid"
        if pid_file.exists() and (pid := pid_file.read_text()):
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid), signal.SIGKILL)


@pytest.mark.asyncio
async def test_capture_includes_descendant_output_within_grace(
    shell_toolkit: Toolkit,
    tmp_path: Path,
) -> None:
    """A short-lived descendant can finish writing within the grace after its command exits."""
    result = await _run(
        shell_toolkit,
        {
            "args": "(sleep 0.2; printf late) & printf early; sleep 0.05",
            "mindroom_output_path": "descendant.txt",
        },
    )
    assert "saved_to_file" in str(result.result)
    assert (tmp_path / "descendant.txt").read_text() == "earlylate"


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
    registry = ProcessRegistry()
    result = await registry.run_or_background(
        namespace="blocked-producer",
        argv=["bash", "-c", "awk 'BEGIN { for(i=0;i<125000;i++) print \"0123456789012345678\" }'"],
        env={"PATH": os.environ["PATH"]},
        cwd=str(tmp_path),
        tail=100,
        timeout=0,
        output_destination=ShellOutputDestination(str(tmp_path), "missing.txt", 3000000),
    )
    assert result.handle is not None
    record = registry._records[result.handle]
    assert record.monitor_task is not None
    try:
        await asyncio.wait_for(asyncio.shield(record.monitor_task), timeout=10)
        assert record.finished
        assert record.process.returncode is not None
        assert not (tmp_path / "missing.txt").exists()
    finally:
        registry.kill_all()
        with contextlib.suppress(asyncio.CancelledError):
            await record.monitor_task


@pytest.mark.asyncio
async def test_failed_shell_auto_save_preserves_is_error_and_saved_output(
    shell_toolkit: Toolkit,
    tmp_path: Path,
) -> None:
    """Large failed shell output auto-saves while keeping is_error=True, counts_as_tool_failure=False, and saved_output."""
    configure_toolkit_functions(
        shell_toolkit,
        output_files=ToolOutputFilePolicy(tmp_path, auto_save_threshold_bytes=64),
    )
    fn = toolkit_function(shell_toolkit, "run_shell_command")
    assert fn is not None
    outcome = await execute_tool_call(
        fn,
        {"args": [sys.executable, "-c", "import sys; print('x' * 200); sys.exit(2)"]},
        ToolCallContext(run_id="r", session_id="s", user_id="@a:t", call_id="auto"),
        middleware=(),
    )
    assert (outcome.is_error, outcome.counts_as_tool_failure) == (True, False)
    assert outcome.saved_output is not None
    receipt = outcome.saved_output["mindroom_tool_output"]
    assert isinstance(receipt, dict)
    assert (receipt["status"], receipt["auto_saved"]) == ("saved_to_file", True)
    assert literal_eval(outcome.content) == client_tool_result(outcome) == outcome.saved_output
    assert (tmp_path / str(receipt["path"])).read_text().startswith("Exit code 2\nOutput:\n" + "x" * 200)


@pytest.mark.parametrize(
    ("chunks", "max_lines", "max_bytes"),
    [
        (["a\nb", "c\n", "", "d"], 2, 32),
        (["a\nb\n", "\n"], 2, 32),
        (["é", "🙂", "界"], 4, 6),
        (["a", "🙂"], 4, 2),
        (["\ud800", "é", "界"], 4, 3),
    ],
)
def test_would_cap_matches_first_buffer_loss(chunks: list[str], max_lines: int, max_bytes: int) -> None:
    """Crossing a hard limit is predicted without mutating the buffer; equality is still complete."""
    buffer = ShellOutputBuffer(max_lines=max_lines, max_bytes=max_bytes)
    for chunk in chunks[:-1]:
        assert not buffer.would_cap(chunk)
        buffer.append(chunk)
        assert not buffer.capped
    before = buffer.text()
    assert buffer.would_cap(chunks[-1])
    assert not buffer.capped
    assert buffer.text() == before
    buffer.append(chunks[-1])
    assert buffer.capped


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stdout_cap", "stderr_cap", "exit_code"),
    [(True, False, 0), (False, True, 0), (False, True, 3), (True, True, 3)],
)
async def test_wrapped_shell_spills_both_streams_once(
    shell_toolkit: Toolkit,
    tmp_path: Path,
    stdout_cap: bool,
    stderr_cap: bool,
    exit_code: int,
) -> None:
    """Both local and supervised tools retain heads and tails, and the wrapper does not auto-save the tailed reply."""
    budget = 512 if stdout_cap else 50 * 1024
    configure_toolkit_functions(
        shell_toolkit,
        output_files=ToolOutputFilePolicy(tmp_path, auto_save_threshold_bytes=budget, tools_use_workdir=stderr_cap),
    )
    out_count = 10_001 if stdout_cap else 2
    err_count = 10_001 if stderr_cap else 2
    script = (
        "import sys; "
        f"sys.stdout.write('out-start\\n' + 'o\\n' * {out_count} + 'out-end'); "
        f"sys.stderr.write('err-start\\n' + 'e\\n' * {err_count} + 'err-end'); sys.exit({exit_code})"
    )
    async with shell_background_scope():
        result = await _run(
            shell_toolkit,
            {
                "args": [sys.executable, "-c", script],
                "tail": 10_000 if stdout_cap else 1,
                "timeout": None if stderr_cap else 10,
            },
        )
    value = result.result
    if exit_code:
        assert isinstance(value, ToolOutcome)
        assert value.is_error
        assert not value.counts_as_tool_failure
        assert value.saved_output is None
        value = value.content
        assert value.startswith(f"Exit code {exit_code}\n")
    assert isinstance(value, str)
    output_dir = tmp_path / "mindroom_tool_outputs"
    (saved,) = output_dir.iterdir()
    assert re.fullmatch(r"shell-spill-[0-9a-f]{32}\.txt", saved.name)
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    shown_path = saved if stderr_cap else saved.relative_to(tmp_path)
    hint = f"Full output saved to {shown_path} (inspect in parts with read_file offset/limit or grep/sed)."
    assert hint in value.split("\n", 2)[bool(exit_code)]
    assert value.count(hint) == 1
    assert "Redirect command output" not in value
    assert len(value.encode("utf-8")) <= budget
    assert (
        saved.read_text()
        == "out-start\n" + "o\n" * out_count + "out-end\nStderr:\n" + "err-start\n" + "e\n" * err_count + "err-end"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 17])
async def test_two_hard_capped_lines_share_one_bounded_inline_preview(
    shell_toolkit: Toolkit,
    tmp_path: Path,
    exit_code: int,
) -> None:
    """Long UTF-8 lines in both streams keep the path and exit status but cannot bypass the inline budget."""
    budget = 513
    policy = ToolOutputFilePolicy(tmp_path, auto_save_threshold_bytes=budget)
    configure_toolkit_functions(shell_toolkit, output_files=policy)
    script = f"import sys; print('é' * 31000); print('雪' * 21000, file=sys.stderr); sys.exit({exit_code})"
    value = (await _run(shell_toolkit, {"args": [sys.executable, "-c", script], "timeout": 10})).result
    if exit_code:
        assert isinstance(value, ToolOutcome)
        assert value.is_error
        assert not value.counts_as_tool_failure
        assert value.saved_output is None
        value = value.content
        assert value.startswith(f"Exit code {exit_code}\n")
    assert isinstance(value, str)
    (saved,) = (tmp_path / "mindroom_tool_outputs").iterdir()
    assert value.count(f"Full output saved to {saved.relative_to(tmp_path)}") == 1
    assert len(value.encode("utf-8")) <= budget
    assert value.endswith("雪" * 40)
    assert saved.read_text() == "é" * 31000 + "\nStderr:\n" + "雪" * 21000 + "\n"


def test_tiny_spill_receipt_keeps_its_label_and_can_still_be_routed() -> None:
    """When only the mandatory receipt fits, retain the label so a worker can translate the path."""
    path = Path(f"mindroom_tool_outputs/shell-spill-{'a' * 32}.txt")
    status = "Exit code 7"
    spill = PublishedShellSpill(path, threshold=1, truncated_at="64 MiB")
    receipt = spill.bound_preview(f"{status}\n{'x' * 500}", status=status)
    assert receipt == f"{status}\n[First 64 MiB saved to {path}]"
    assert (
        format_named_shell_spill(
            receipt,
            worker_header="[worker: build]",
            mount_dir="jobs/build",
            auto_save_threshold_bytes=1,
        )
        == f"[worker: build]\n{status}\n[First 64 MiB saved to jobs/build/{path}]"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("named", [False, True], ids=["workspace", "named-mount"])
async def test_cli_shortens_a_published_shell_spill_without_saving_another_file(
    shell_toolkit: Toolkit,
    tmp_path: Path,
    named: bool,
) -> None:
    """The actual shell spill survives CLI JSON-encoding and remains the only saved artifact."""
    mount = "jobs/build"
    root = tmp_path / mount if named else tmp_path
    root.mkdir(parents=True, exist_ok=True)
    configure_toolkit_functions(shell_toolkit, output_files=ToolOutputFilePolicy(root))
    script = "import sys; print((chr(1) + 'é') * 20000); sys.exit(7)"
    value = (await _run(shell_toolkit, {"args": [sys.executable, "-c", script], "timeout": 10})).result
    assert isinstance(value, ToolOutcome)
    prefix = "Exit code 7\n"
    text = value.content
    if named:
        routed = format_named_shell_spill(
            text,
            worker_header="[worker: build]",
            mount_dir=mount,
            auto_save_threshold_bytes=40_000,
        )
        assert routed is not None
        text = routed
        prefix = "[worker: build]\n" + prefix
    assert len(json.dumps(text, ensure_ascii=False).encode()) > _INLINE_BUDGET

    result = project_cli_result(text, ToolOutputFilePolicy(tmp_path), "run_shell_command")

    assert isinstance(result, str)
    assert result.startswith(prefix)
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= _INLINE_BUDGET
    assert "\x01é" in result
    [directory] = tmp_path.rglob("mindroom_tool_outputs")
    [saved] = directory.iterdir()
    assert f"Full output saved to {saved.relative_to(tmp_path)}" in result
    assert saved.read_text() == "\x01é" * 20000 + "\n"


def test_cli_auto_saves_unspilled_output_with_a_spill_notice_in_its_body(tmp_path: Path) -> None:
    """A copied spill notice inside command output is data, not a receipt for the current call."""
    path = f"mindroom_tool_outputs/shell-spill-{'a' * 32}.txt"
    text = f"[worker: build]\nExit code 7\nOutput:\nTranscript:\n[Full output saved to {path}]\n" + "x" * _INLINE_BUDGET
    result = project_cli_result(text, ToolOutputFilePolicy(tmp_path), "run_shell_command")

    assert isinstance(result, dict)
    saved = result["mindroom_tool_output"]
    assert saved["status"] == "saved_to_file"
    (file,) = (tmp_path / "mindroom_tool_outputs").iterdir()
    assert saved["path"] == str(file.relative_to(tmp_path))
    assert file.read_text() == text


_LARGE_OUTPUT = "print('x' * 52000, flush=True)"
_SLEEP_AFTER_OUTPUT = f"import time; {_LARGE_OUTPUT}; time.sleep(60)"


async def _run_with_spill(root: Path, script: str = _LARGE_OUTPUT, *, timeout_seconds: float = 10) -> ShellRunResult:
    return await run_foreground(
        argv=[sys.executable, "-c", script],
        env={},
        cwd=None,
        tail=1,
        timeout=timeout_seconds,
        spill_policy=ShellSpillPolicy(str(root), 100_000, auto_save_threshold_bytes=512),
    )


@pytest.mark.asyncio
async def test_tail_only_does_not_allocate_a_spill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A child can fill the hard line limit exactly without a spool, even when the response shows only one line."""

    def unexpected_spool(**_kwargs: object) -> BinaryIO:
        pytest.fail("An uncapped buffer allocated a tempfile")

    monkeypatch.setattr("mindroom.shell.output.tempfile.TemporaryFile", unexpected_spool)
    result = await _run_with_spill(tmp_path, "print('a\\n' * 9999 + 'last')")
    assert result.message == "[Showing the last 1 of 10000 lines. Pass a larger tail, or narrow the command.]\nlast"
    assert not result.output_file_handled
    assert not (tmp_path / "mindroom_tool_outputs").exists()


@pytest.mark.asyncio
async def test_structured_shell_skips_spill_even_when_hard_capped(shell_toolkit: Toolkit, tmp_path: Path) -> None:
    """The structured request keeps its original shell text and exit metadata through either execution route."""
    with structured_tool_result() as metadata:
        result = await _run(shell_toolkit, {"args": [sys.executable, "-c", "print('x' * 52000)"], "tail": 1})
    assert isinstance(result.result, str)
    assert "Redirect command output" in result.result
    assert metadata == {"exit_code": 0}
    assert not (tmp_path / "mindroom_tool_outputs").exists()


@pytest.mark.asyncio
async def test_spill_cap_is_combined_and_does_not_split_utf8(tmp_path: Path) -> None:
    """Seed both streams, budget payload plus framing, and stop at a UTF-8 boundary."""
    stdout, stderr = ShellOutputBuffer(max_bytes=4), ShellOutputBuffer(max_bytes=4)
    spill = ShellOutputSpill(ShellSpillPolicy(str(tmp_path), 18, auto_save_threshold_bytes=512), stdout, stderr)
    try:
        for is_stderr, chunk in [(False, "é\n"), (True, "err"), (False, "x🙂"), (True, "!"), (False, "!")]:
            spill.append(chunk, is_stderr=is_stderr)
            (stderr if is_stderr else stdout).append(chunk)
        receipt = await spill.publish()
        (saved,) = (tmp_path / "mindroom_tool_outputs").iterdir()
        assert receipt is not None
        assert receipt.hint.startswith(f"First 18 bytes saved to mindroom_tool_outputs/{saved.name}")
        assert saved.read_text() == "é\nx\nStderr:\nerr"
    finally:
        spill.close()


@pytest.mark.asyncio
async def test_spill_at_file_size_limit_can_be_read_by_workspace_tool(tmp_path: Path) -> None:
    """A spill at the real read limit leaves room for stderr framing and is readable with pagination."""
    cap = DEFAULT_TOOL_OUTPUT_MAX_BYTES
    stdout, stderr = ShellOutputBuffer(), ShellOutputBuffer()
    stdout.append("first page\n")
    spill = ShellOutputSpill(ShellSpillPolicy(str(tmp_path), cap, auto_save_threshold_bytes=512), stdout, stderr)
    chunk = "e" * (1024 * 1024)
    try:
        for _ in range(cap // len(chunk) + 1):
            spill.append(chunk, is_stderr=True)
        receipt = await spill.publish()
    finally:
        spill.close()
    assert receipt is not None
    assert receipt.truncated_at is not None
    assert 0 < (tmp_path / receipt.path).stat().st_size <= cap
    page = FilesystemTools(base_dir=str(tmp_path)).read_file(str(receipt.path), offset=1, limit=1)
    assert page.startswith("1| first page\n")
    assert "Use offset=2 to continue" in page


@pytest.mark.asyncio
async def test_spill_copy_does_not_block_the_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop can reach another task while the file publisher is blocked in I/O."""
    loop = asyncio.get_running_loop()
    copying = asyncio.Event()
    release = threading.Event()
    write = output_capture._write_spill

    def slow_write(spool: SpoolCapture, target: BinaryIO) -> None:
        loop.call_soon_threadsafe(copying.set)
        assert release.wait(timeout=3)
        write(spool, target)

    monkeypatch.setattr(output_capture, "_write_spill", slow_write)
    task = asyncio.create_task(_run_with_spill(tmp_path))
    try:
        await asyncio.wait_for(copying.wait(), timeout=5)
        assert not task.done()
    finally:
        release.set()
        result = await task
    assert result.output_file_handled


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["root_link", "output_dir_link", "disk_write", "spool_open"])
async def test_spill_errors_fall_back_without_writing_outside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Refuse linked roots/destinations and clean failed writes without changing the command outcome."""
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    outside.mkdir()
    if failure == "root_link":
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir()
    if failure == "output_dir_link":
        (root / "mindroom_tool_outputs").symlink_to(outside, target_is_directory=True)

    def disk_error() -> OSError:
        return OSError("Simulated spill I/O failure")

    if failure == "disk_write":

        def fail_write(_capture: SpoolCapture, target: BinaryIO) -> None:
            target.write(b"incomplete")
            raise disk_error()

        monkeypatch.setattr(output_capture, "_write_spill", fail_write)
    if failure == "spool_open":

        def fail_open(**_kwargs: object) -> BinaryIO:
            raise disk_error()

        monkeypatch.setattr("mindroom.shell.output.tempfile.TemporaryFile", fail_open)

    result = await _run_with_spill(root)
    assert result.return_code == 0
    assert not result.output_file_handled
    assert "Redirect command output" in result.message
    assert not list(outside.iterdir())
    assert not list((root / "mindroom_tool_outputs").glob("shell-spill-*.txt"))


@pytest.mark.asyncio
async def test_spill_sweep_limits_only_generated_regular_files(tmp_path: Path) -> None:
    """Expire old spills, keep at most 64 otherwise, and protect new files and unrelated entries."""
    output_dir = tmp_path / "mindroom_tool_outputs"
    output_dir.mkdir()
    now = time.time()

    def make(name: str, age: int) -> Path:
        path = output_dir / name
        path.write_text("previous")
        os.utime(path, (now - age, now - age))
        return path

    expired = make(f"shell-spill-{0:032x}.txt", 25 * 3600)
    oldest = make(f"shell-spill-{1:032x}.txt", 3600)
    for index in range(2, 66):
        make(f"shell-spill-{index:032x}.txt", 600)
    recent = make(f"shell-spill-{66:032x}.txt", 30)
    custom = make(f"shell-spill-{67:032x}.txt.backup", 25 * 3600)
    linked = output_dir / f"shell-spill-{68:032x}.txt"
    linked.symlink_to(custom)

    result = await _run_with_spill(tmp_path)
    assert result.output_file_handled
    assert not expired.exists()
    assert not oldest.exists()
    assert recent.read_text() == custom.read_text() == "previous"
    assert linked.is_symlink()
    assert len([path for path in output_dir.glob("shell-spill-*.txt") if not path.is_symlink()]) == 64


@pytest.mark.asyncio
async def test_inline_timeout_publishes_capped_partial_spill(tmp_path: Path) -> None:
    """An inline stop preserves output already received, unlike an explicit strict redirect."""
    result = await _run_with_spill(tmp_path, _SLEEP_AFTER_OUTPUT, timeout_seconds=1)
    assert result.timed_out
    assert result.output_file_handled
    assert result.message.startswith("Stopped after 1s.")
    (saved,) = (tmp_path / "mindroom_tool_outputs").iterdir()
    assert str(saved.relative_to(tmp_path)) in result.message.split("\n", 2)[1]
    assert len(result.message.encode("utf-8")) <= 512
    assert saved.read_bytes() == b"x" * 52000 + b"\n"


@pytest.fixture
def spill_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[BinaryIO], asyncio.Event]:
    """Observe real spool allocation and release without relying on a subprocess startup delay."""
    temporary_file = tempfile.TemporaryFile
    files: list[BinaryIO] = []
    opened = asyncio.Event()

    def observe(*, dir: str | None) -> BinaryIO:  # noqa: A002
        assert dir == str(tmp_path)
        stream = temporary_file(dir=dir)
        files.append(stream)
        if len(files) == 2:
            opened.set()
        return stream

    monkeypatch.setattr("mindroom.shell.output.tempfile.TemporaryFile", observe)
    return files, opened


@pytest.mark.asyncio
async def test_cancel_releases_active_spill_without_publication(
    tmp_path: Path,
    spill_files: tuple[list[BinaryIO], asyncio.Event],
) -> None:
    """Cancel after both files open and check that the run publishes nothing and closes both."""
    files, opened = spill_files
    task = asyncio.create_task(_run_with_spill(tmp_path, _SLEEP_AFTER_OUTPUT, timeout_seconds=30))
    try:
        await asyncio.wait_for(opened.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(files) == 2
    assert all(file.closed for file in files)
    assert not (tmp_path / "mindroom_tool_outputs").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("cap_before_adoption", [False, True])
async def test_adoption_disables_spill_even_if_not_yet_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spill_files: tuple[list[BinaryIO], asyncio.Event],
    cap_before_adoption: bool,
) -> None:
    """Readers continue after adoption but can neither keep nor initiate anonymous spill files."""
    files, opened = spill_files

    async def adopt(_command: ActiveCommand, **_kwargs: object) -> bool:
        if cap_before_adoption:
            await asyncio.wait_for(opened.wait(), timeout=5)
        return True

    monkeypatch.setattr(ActiveCommand, "wait_or_timeout", adopt)
    prefix = "print('x' * 52000, flush=True)\n" if cap_before_adoption else ""
    script = (
        "import pathlib, time\n"
        + prefix
        + "while not pathlib.Path('release').exists(): time.sleep(0.01)\nprint('y' * 52000, flush=True)"
    )
    registry = ProcessRegistry()
    try:
        result = await registry.run_or_background(
            namespace="spill",
            argv=[sys.executable, "-c", script],
            env={},
            cwd=str(tmp_path),
            tail=1,
            timeout=1,
            spill_policy=ShellSpillPolicy(str(tmp_path), 200_000, auto_save_threshold_bytes=512),
        )
        assert result.handle is not None
        assert not result.output_file_handled
        assert len(files) == 2 * cap_before_adoption
        assert all(file.closed for file in files)
        (tmp_path / "release").touch()
        record = registry.get_process("spill", result.handle)
        assert record is not None
        assert record.monitor_task is not None
        await asyncio.wait_for(asyncio.shield(record.monitor_task), timeout=5)
        assert record.stdout_buf.capped
        assert "y" in record.stdout_buf.text()
        assert len(files) == 2 * cap_before_adoption
        assert not (tmp_path / "mindroom_tool_outputs").exists()
    finally:
        registry.kill_all()
