"""Tests for PR #4260 round-1 review fixes (never_ran cancel, structured launch errors, spawn scope, TTY races)."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
from contextlib import contextmanager
from io import StringIO
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from mindroom.agent_cli.main import main
from mindroom.agent_cli.protocol import ToolCallOperation
from mindroom.agent_cli.turn import ToolKey
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.shell.execution import ProcessRecord, run_command
from mindroom.shell.output_capture import ShellOutputDestination, format_shell_completion
from mindroom.tool_system.catalog import builtin_tool_registry
from mindroom.tool_system.sandbox_proxy import WorkerStandIns
from mindroom.tool_system.worker_proxy_client import WorkerCallCancellation
from mindroom.workers.models import WorkerHandle
from mindroom.workers.protocol import WORKER_PROTOCOL_VERSION, ExecuteRequest
from tests.test_workers_run_control import (
    SETTLE_TIMEOUT_SECONDS,
    _make_owner,
    capture_bash_batch,
    response_cli_lifetime,
)
from tests.test_workers_run_small_fixes import _FakeWorkerManager

if TYPE_CHECKING:
    from collections.abc import Iterator
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

    def blocking_build(*_args: object, **_kwargs: object) -> tuple[ExecuteRequest, WorkerHandle]:
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

    proxied_shell = WorkerStandIns(
        tool_name="shell",
        runtime_paths=runtime_paths,
        credentials_manager=None,
        tools=builtin_tool_registry(),
    )("run_shell_command", local_placeholder)

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

    class Client:
        def operation(self, payload: dict[str, object]) -> dict[str, object]:
            return {
                "status": "completed",
                "call_id": payload["call_id"],
                "outcome": f"[worker: w1]\n{format_shell_completion(text, None, return_code=0)}",
            }

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    assert main(["workers", "run", "w1", "--", "echo", text]) == 0
    output = capsys.readouterr()
    assert output.out == f"{text}\n"
    assert output.err == ""


@pytest.mark.asyncio
async def test_r1_output_capture_eacces_stays_execution_error(tmp_path: Path) -> None:
    """EACCES from ShellOutputCapture temp-file creation stays an execution error, not a command that exited 126."""
    ro_dir = tmp_path / "readonly"
    ro_dir.mkdir()
    ro_dir.chmod(0o555)
    try:
        registry: dict[str, ProcessRecord] = {}
        result = await run_command(
            registry,
            namespace="test",
            argv=["/bin/sh", "-c", "echo ok"],
            env=dict(os.environ),
            cwd=str(tmp_path),
            tail=10,
            timeout=5.0,
            output_destination=ShellOutputDestination(
                workspace_root=str(ro_dir),
                path="out.txt",
                max_bytes=1024,
            ),
            launch_failure_exits=True,
        )
        assert result.execution_error is True
        assert result.message.startswith("Error: [Errno 13]")
    finally:
        ro_dir.chmod(0o755)


def test_r1_signal_during_first_tty_progress_flush_still_ends_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """A signal delivered right after the first TTY progress write still ends the progress line before cancel notice."""
    installed: dict[int, object] = {}

    def fake_signal(signum: int, handler: object) -> object:
        prev = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        return prev

    class _SignallingTty(StringIO):
        def __init__(self) -> None:
            super().__init__()
            self._signalled = False

        def isatty(self) -> bool:
            return True

        def flush(self) -> None:
            super().flush()
            if not self._signalled and "starting worker f1..." in self.getvalue():
                self._signalled = True
                handler = installed[int(signal.SIGTERM)]
                assert callable(handler)
                handler(int(signal.SIGTERM), None)

    class Client:
        def operation(self, payload: dict[str, object]) -> dict[str, object]:
            return {"status": "queued", "call_id": payload["call_id"]}

        def receipt(self, call_id: str, **_kwargs: object) -> dict[str, object]:
            del call_id
            return {"status": "running", "progress": "starting worker f1...", "progress_revision": 1}

        def cancel(self, call_id: str, *, timeout: float = 8.0) -> dict[str, object]:
            del call_id, timeout
            return {"status": "cancelled", "cancel_outcome": "never_ran"}

    tty = _SignallingTty()
    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr("mindroom.agent_cli.main._POLL_FLOOR_SECONDS", 0.0)
    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr(sys, "stderr", tty)

    assert main(["workers", "run", "f1", "--", "sleep", "60"]) == 128 + int(signal.SIGTERM)
    assert tty.getvalue() == (
        "\r\x1b[Kmindroom-agent: starting worker f1...\n"
        "mindroom-agent: cancelled by SIGTERM; the command never ran in worker f1\n"
    )


def test_r1_escaping_exception_ends_open_tty_progress_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected escaping exception ends an open TTY progress line with a newline."""

    class _Tty(StringIO):
        def isatty(self) -> bool:
            return True

    class Client:
        def operation(self, payload: dict[str, object]) -> dict[str, object]:
            return {
                "status": "running",
                "call_id": payload["call_id"],
                "progress": "starting worker f1...",
                "progress_revision": 1,
            }

        def receipt(self, call_id: str, **_kwargs: object) -> dict[str, object]:
            del call_id
            msg = "unexpected boom"
            raise RuntimeError(msg)

    tty = _Tty()
    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr("mindroom.agent_cli.main._POLL_FLOOR_SECONDS", 0.0)
    monkeypatch.setattr(sys, "stderr", tty)

    with pytest.raises(RuntimeError, match="unexpected boom"):
        main(["workers", "run", "f1", "--", "echo", "hi"])
    assert tty.getvalue() == "\r\x1b[Kmindroom-agent: starting worker f1...\n"
