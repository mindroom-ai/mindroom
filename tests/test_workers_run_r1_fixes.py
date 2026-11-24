"""Tests for PR #4260 round-1 review fixes (never_ran cancel, structured launch errors, spawn scope, TTY races)."""

from __future__ import annotations

import asyncio
import builtins
import errno
import io
import os
import signal
import sys
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from mindroom.agent_cli.main import main
from mindroom.agent_cli.protocol import ToolCallOperation
from mindroom.agent_cli.turn import ToolKey
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.shell.execution import ShellRunResult, _launch_failure, run_command
from mindroom.shell.output_capture import ShellOutputDestination
from mindroom.tool_system.catalog import builtin_tool_registry
from mindroom.tool_system.sandbox_proxy import WorkerStandIns
from mindroom.tool_system.worker_proxy_client import WorkerCallCancellation
from mindroom.workers.models import WorkerHandle
from mindroom.workers.protocol import WORKER_PROTOCOL_VERSION, ExecuteRequest
from tests.test_workers_run_control import (
    SETTLE_TIMEOUT_SECONDS,
    _make_owner,
    _NoStdin,
    capture_bash_batch,
    response_cli_lifetime,
)
from tests.test_workers_run_small_fixes import _cli_with_stream, _FakeWorkerManager
from tests.workers_run_frames import FakeStream, command_frames

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path

_TEST_TOKEN = "test-token"  # noqa: S105


@pytest.mark.asyncio
async def test_r1_queued_and_cold_start_cancel_report_never_ran(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queued calls and cancels before _arm (cold start) report cancel_outcome='never_ran'."""
    cancellation = WorkerCallCancellation()
    assert cancellation.cancel() is True

    # Cancel during cold-start warmup (before _arm is called) sets cancel_outcome='never_ran'.
    in_warmup = threading.Event()
    release_warmup = threading.Event()
    handle = WorkerHandle(
        worker_id="worker-f2",
        worker_key="agent:test:named=f2",
        endpoint="http://worker/api/sandbox-runner/execute",
        auth_token=_TEST_TOKEN,
        status="ready",
        backend_name="kubernetes",
        last_used_at=0.0,
        created_at=0.0,
    )

    @contextmanager
    def fake_lease(_rp: object, _pc: object) -> Iterator[_FakeWorkerManager]:
        yield _FakeWorkerManager(handle)

    def blocking_build(*_args: object, **_kwargs: object) -> tuple[ExecuteRequest, WorkerHandle, None]:
        in_warmup.set()
        release_warmup.wait(timeout=5.0)
        return (
            ExecuteRequest(
                protocol=WORKER_PROTOCOL_VERSION,
                tool_name="shell",
                function_name="run_shell_command",
                args=[],
                kwargs={"args": ["sleep", "60"]},
            ),
            handle,
            None,
        )

    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_WORKER_BACKEND": "kubernetes"},
    )
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.lease_worker_manager", fake_lease)
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy._build_execute_request", blocking_build)

    async def local_placeholder(args: str) -> str:
        return args

    proxied_shell = cast(
        "Callable[[str], Awaitable[object]]",
        WorkerStandIns(
            tool_name="shell",
            runtime_paths=runtime_paths,
            credentials_manager=None,
            tools=builtin_tool_registry(),
        )("run_shell_command", local_placeholder),
    )

    outer_entered = asyncio.Event()
    release_outer = asyncio.Event()

    async def run_shell_command(args: str) -> str:
        if args == "outer":
            outer_entered.set()
            await release_outer.wait()
            return "outer done"
        return str(await proxied_shell(args))

    owner, _token = await _make_owner(tmp_path, run_shell_command)
    try:
        async with response_cli_lifetime():
            await capture_bash_batch(owner, ("outer-1", {"command": "outer"}))
            bash_task = asyncio.create_task(
                owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "outer"}, "outer-1"),
            )
            async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                await outer_entered.wait()
                # 1. Queued cancel before dispatch
                queued_id = uuid4()
                await owner.operation(
                    window="outer-1",
                    operation=ToolCallOperation(
                        operation="tools.call",
                        call_id=queued_id,
                        toolkit="shell",
                        function="run_shell_command",
                        arguments={"args": "never"},
                    ),
                )
                queued_receipt = await owner.cancel_call(str(queued_id))
                assert queued_receipt["status"] == "cancelled"
                assert queued_receipt["cancel_outcome"] == "never_ran"

                # 2. Cold-start cancel before _arm / dispatch to runner
                warmup_id = uuid4()
                await owner.operation(
                    window="outer-1",
                    operation=ToolCallOperation(
                        operation="tools.call",
                        call_id=warmup_id,
                        toolkit="shell",
                        function="run_shell_command",
                        arguments={"args": "warmup"},
                    ),
                )
                assert await asyncio.to_thread(in_warmup.wait, 5.0)
                warmup_receipt = await owner.cancel_call(str(warmup_id))
                assert warmup_receipt["status"] == "cancelled"
                assert warmup_receipt["cancel_outcome"] == "never_ran"

                release_outer.set()
                await bash_task
    finally:
        release_warmup.set()
        await owner.close()


def test_r1_successful_command_printing_launch_failure_text_exits_0(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful command printing 'Could not start command (exit code 127): example' exits 0 on stdout."""
    text = "Could not start command (exit code 127): example"

    _cli_with_stream(monkeypatch, command_frames(f"{text}\n".encode(), header="[worker: w1]"))
    assert main(["workers", "run", "w1", "--", "echo", text]) == 0
    output = capsys.readouterr()
    assert output.out == f"{text}\n"
    assert output.err == ""


@pytest.mark.asyncio
async def test_r1_output_capture_eacces_stays_execution_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PermissionError from creating the capture's file stays an execution error, even one that names the program."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(errno.EACCES, "Permission denied", "/bin/sh")

    monkeypatch.setattr("mindroom.shell.output_capture.tempfile.TemporaryFile", refuse)
    result = await run_command(
        {},
        namespace="test",
        argv=["/bin/sh", "-c", "echo ok"],
        env=dict(os.environ),
        cwd=str(tmp_path),
        tail=10,
        timeout=5.0,
        output_destination=ShellOutputDestination(workspace_root=str(tmp_path), path="out.txt", max_bytes=1024),
        launch_failure_exits=True,
    )
    assert result.execution_error is True
    assert result.message.startswith("Error: [Errno 13]")


async def _launch(argv: list[str], *, path: Path | str, cwd: Path) -> ShellRunResult:
    """Run a command whose launch failure exits as a shell reports it, with ``path`` as its whole PATH."""
    return await run_command(
        {},
        namespace="test",
        argv=argv,
        env={"PATH": str(path)},
        cwd=str(cwd),
        tail=10,
        timeout=5.0,
        launch_failure_exits=True,
    )


@pytest.mark.asyncio
async def test_r3_bare_name_missing_from_a_path_with_a_file_entry_is_command_not_found(tmp_path: Path) -> None:
    """A bare name no PATH entry holds is not found, whatever error the last entry gave: here it is a regular file."""
    entry = tmp_path / "not-a-directory"
    entry.write_text("")
    result = await _launch(["missing-program-xyz"], path=f"{tmp_path}{os.pathsep}{entry}", cwd=tmp_path)
    assert result == ShellRunResult(message="Exit code 127\nStderr:\nmissing-program-xyz: command not found")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_line", "mode", "bare", "exit_code", "reason"),
    [
        ("#!/nonexistent/interp -x\n", 0o755, True, 127, "cannot execute: required file not found"),
        ("#!/nonexistent/interp -x\n", 0o755, False, 127, "No such file or directory"),
        ("#!/bin/sh\r\n", 0o755, True, 127, "cannot execute: required file not found"),
        ("#!/bin/sh\n", 0o644, True, 126, "Permission denied"),
        ("#!/bin/sh\n", 0o644, False, 126, "Permission denied"),
    ],
)
async def test_r3_program_that_is_found_but_cannot_run_is_reported_by_its_spawn_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_line: str,
    mode: int,
    bare: bool,
    exit_code: int,
    reason: str,
) -> None:
    """A script whose interpreter is missing, or without the execute bit, is reported from its spawn error alone."""
    # Where the idle policy is refused, a command starts directly, so its spawn error is the one that is reported.
    monkeypatch.setattr("mindroom.shell.execution._idle_launcher", lambda: None)
    script = tmp_path / "script-xyz"
    script.write_text(f"{first_line}echo hi\n")
    script.chmod(mode)
    result = await _launch(["script-xyz" if bare else str(script)], path=tmp_path, cwd=tmp_path)
    assert result == ShellRunResult(message=f"Exit code {exit_code}\nStderr:\n{script}: {reason}")


@pytest.mark.asyncio
async def test_r4_path_entry_that_loops_stays_an_execution_error(tmp_path: Path) -> None:
    """An error outside the listed ones, here a PATH entry that is a symlink to itself, is no missing program."""
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    result = await _launch(["missing-program-xyz"], path=loop, cwd=tmp_path)
    assert result.execution_error is True
    assert result.message.startswith(f"Error: [Errno {errno.ELOOP}]")


@pytest.mark.asyncio
@pytest.mark.parametrize("fifo", [False, True])
async def test_r4_program_with_a_trailing_slash_keeps_it_and_is_never_opened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fifo: bool,
) -> None:
    """A file named as a directory is reported under the name it was given, and what it holds is never read."""
    monkeypatch.setattr("mindroom.shell.execution._idle_launcher", lambda: None)
    program = tmp_path / "script-xyz"
    line = b"#!/bin/sh\n"
    if fifo:
        os.mkfifo(program, 0o755)
        # A reader of a FIFO waits for its writer; this end makes any read return at once and shows whether one happened.
        writer = os.open(program, os.O_RDWR | os.O_NONBLOCK)
        os.write(writer, line)
    else:
        program.write_bytes(line)
        program.chmod(0o755)
    try:
        result = await _launch(["./script-xyz/"], path=tmp_path, cwd=tmp_path)
        assert result == ShellRunResult(message="Exit code 126\nStderr:\n./script-xyz/: Not a directory")
        if fifo:
            assert os.read(writer, 64) == line
    finally:
        if fifo:
            os.close(writer)


_FOUND = "{found}"


@pytest.mark.parametrize(
    ("program", "on_path", "error", "expected"),
    [
        ("name-xyz", False, errno.ENOENT, "Exit code 127\nStderr:\nname-xyz: command not found"),
        ("name-xyz", False, errno.ENOTDIR, "Exit code 127\nStderr:\nname-xyz: command not found"),
        ("name-xyz", False, errno.EACCES, None),
        ("name-xyz", False, errno.ELOOP, None),
        ("name-xyz", True, errno.EACCES, f"Exit code 126\nStderr:\n{_FOUND}: Permission denied"),
        ("name-xyz", True, errno.ENOENT, f"Exit code 127\nStderr:\n{_FOUND}: cannot execute: required file not found"),
        ("name-xyz", True, errno.ENOTDIR, f"Exit code 127\nStderr:\n{_FOUND}: cannot execute: required file not found"),
        ("name-xyz", True, errno.ENOEXEC, None),
        ("name-xyz", True, errno.ELOOP, None),
        ("name-xyz", True, None, None),
        ("./name-xyz/", True, errno.ENOENT, "Exit code 127\nStderr:\n./name-xyz/: No such file or directory"),
        ("./name-xyz/", True, errno.ENOTDIR, "Exit code 126\nStderr:\n./name-xyz/: Not a directory"),
        ("./name-xyz/", True, errno.EACCES, "Exit code 126\nStderr:\n./name-xyz/: Permission denied"),
        ("./name-xyz/", True, errno.ELOOP, None),
        ("./name-xyz/", True, errno.ENAMETOOLONG, None),
        ("./name-xyz/", True, errno.ENOEXEC, None),
        ("./name-xyz/", True, errno.EISDIR, None),
        ("./name-xyz/", True, None, None),
    ],
)
def test_r4_launch_failure_is_classified_by_errno_and_the_given_name_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    program: str,
    on_path: bool,
    error: int | None,
    expected: str | None,
) -> None:
    """Only the listed errors are a shell's exit; the program file is never opened, and its name is never rewritten."""
    if on_path:
        (tmp_path / "name-xyz").write_text("#!/nonexistent/interp\n")

    def opened(*_args: object, **_kwargs: object) -> None:
        pytest.fail("the program file was opened")

    with monkeypatch.context() as patched:
        for owner, name in ((io, "open"), (os, "open"), (builtins, "open")):
            patched.setattr(owner, name, opened)
        result = _launch_failure(
            [program],
            {"PATH": str(tmp_path)},
            str(tmp_path),
            OSError(error, "spawn failed", program),
            None,
        )

    message = expected.format(found=tmp_path / "name-xyz") if expected is not None else None
    assert (result.message if result is not None else None) == message


@pytest.mark.asyncio
@pytest.mark.skipif(os.geteuid() == 0, reason="root enters every directory")
async def test_r3_cwd_that_cannot_be_entered_stays_an_execution_error_when_it_is_the_program(tmp_path: Path) -> None:
    """A working directory the command cannot enter fails the call before any program starts, as a missing one does."""
    closed = tmp_path / "closed"
    closed.mkdir(mode=0o600)
    try:
        result = await _launch([str(closed)], path=tmp_path, cwd=closed)
    finally:
        closed.chmod(0o700)
    assert result.execution_error is True
    assert result.message.startswith("Error: [Errno 13]")


def test_r1_signal_during_first_tty_progress_flush_still_ends_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """A signal delivered right after the first TTY progress write still ends the progress line before cancel notice."""
    installed: dict[int, object] = {}

    def fake_signal(signum: int, handler: object) -> object:
        prev = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        return prev

    class _SignallingTty(FakeStream):
        def __init__(self) -> None:
            super().__init__(is_tty=True)
            self._signalled = False

        def flush(self) -> None:
            super().flush()
            if not self._signalled and "starting worker f1..." in self.getvalue():
                self._signalled = True
                handler = installed[int(signal.SIGTERM)]
                assert callable(handler)
                handler(int(signal.SIGTERM), None)

    class Client(_NoStdin):
        def operation(self, payload: dict[str, object]) -> dict[str, object]:
            return {"status": "queued", "call_id": payload["call_id"]}

        def stream(self, call_id: str) -> Iterator[dict[str, object]]:
            del call_id
            yield {"t": "progress", "text": "starting worker f1..."}
            pytest.fail("the signal should have ended the relay")

        def cancel(self, call_id: str, *, timeout: float = 8.0) -> dict[str, object]:
            del call_id, timeout
            return {"status": "cancelled", "cancel_outcome": "never_ran"}

    tty = _SignallingTty()
    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr(sys, "stderr", tty)

    assert main(["workers", "run", "f1", "--", "sleep", "60"]) == 128 + int(signal.SIGTERM)
    assert tty.getvalue() == (
        "\r\x1b[Kmindroom-agent: starting worker f1...\n"
        "mindroom-agent: cancelled by SIGTERM; the command never ran in worker f1\n"
    )


def test_r1_escaping_exception_ends_open_tty_progress_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected escaping exception ends an open TTY progress line with a newline."""

    class Client(_NoStdin):
        def operation(self, payload: dict[str, object]) -> dict[str, object]:
            return {"status": "running", "call_id": payload["call_id"]}

        def stream(self, call_id: str) -> Iterator[dict[str, object]]:
            del call_id
            yield {"t": "progress", "text": "starting worker f1..."}
            msg = "unexpected boom"
            raise RuntimeError(msg)

    tty = FakeStream(is_tty=True)
    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr(sys, "stderr", tty)

    with pytest.raises(RuntimeError, match="unexpected boom"):
        main(["workers", "run", "f1", "--", "echo", "hi"])
    assert tty.getvalue() == "\r\x1b[Kmindroom-agent: starting worker f1...\n"
