"""Reproducing tests for PR #4260 round-1 review fixes (items 1-7)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import signal
import sys
import threading
import time
from contextlib import contextmanager, suppress
from io import StringIO
from typing import TYPE_CHECKING, Self
from uuid import uuid4

import pytest

from mindroom.agent_cli.main import main
from mindroom.agent_cli.protocol import ToolCallOperation
from mindroom.agent_cli.turn import ToolKey
from mindroom.api import sandbox_request_cancellation
from mindroom.shell.execution import ProcessRecord, run_command
from mindroom.shell.output_capture import ShellOutputDestination, format_shell_completion
from mindroom.shell.supervisor import _ShellSupervisorManager, run_command_via_supervisor
from mindroom.tool_system.worker_proxy_client import WorkerCallCancellation
from tests.test_workers_run_control import (
    SETTLE_TIMEOUT_SECONDS,
    _make_owner,
    capture_bash_batch,
    response_cli_lifetime,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# ---------------------------------------------------------------------------
# Item 1: "Confirmed" must mean the process group is gone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r1_item1_runner_cancel_unconfirmed_when_no_stop_hook_bound() -> None:
    """Cancelling a tracked request with no stop hook bound is not confirmed stopped."""
    with sandbox_request_cancellation.track_request("req-no-hook"):
        cancelled, stopped = await sandbox_request_cancellation.cancel_request_and_wait("req-no-hook")
    assert (cancelled, stopped) == (True, False)


@pytest.mark.asyncio
async def test_r1_item1_runner_cancel_confirmed_when_stop_hook_completes() -> None:
    """Cancelling a tracked request whose stop hook completes confirms stopped."""
    called = False

    def stop() -> bool:
        nonlocal called
        called = True
        return True

    with sandbox_request_cancellation.track_request("req-hook"):
        sandbox_request_cancellation.bind_request_stop(stop)
        cancelled, stopped = await sandbox_request_cancellation.cancel_request_and_wait("req-hook")
    assert called is True
    assert (cancelled, stopped) == (True, True)


@pytest.mark.asyncio
async def test_r1_item1_runner_cancel_unconfirmed_when_stop_hook_reports_already_exited() -> None:
    """When the stop hook reports the process already exited (False), stopped is False."""
    with sandbox_request_cancellation.track_request("req-exited"):
        sandbox_request_cancellation.bind_request_stop(lambda: False)
        cancelled, stopped = await sandbox_request_cancellation.cancel_request_and_wait("req-exited")
    assert (cancelled, stopped) == (True, False)


@pytest.mark.asyncio
async def test_r1_item1_supervisor_cancel_confirms_only_after_process_group_dies(tmp_path: Path) -> None:
    """Supervisor cancel waits until a SIGTERM-ignoring process group is actually dead before confirming."""
    from mindroom.shell.supervisor import cancel_supervised_run  # noqa: PLC0415

    pid_file = tmp_path / "child.pid"
    manager = _ShellSupervisorManager()
    socket_path = manager.ensure()
    try:
        request_id = f"req-{uuid4().hex}"
        run_task = asyncio.create_task(
            run_command_via_supervisor(
                socket_path,
                namespace="test",
                argv=["/bin/sh", "-c", f"trap '' TERM; echo $$ > {pid_file}; sleep 30"],
                env=dict(os.environ),
                cwd=str(tmp_path),
                tail=10,
                timeout=30.0,
                on_timeout="kill",
                request_id=request_id,
            ),
        )
        deadline = time.monotonic() + 5.0
        while not pid_file.exists() or not pid_file.read_text().strip():
            assert time.monotonic() < deadline, "supervised command did not start"
            await asyncio.sleep(0.02)
        child_pid = int(pid_file.read_text().strip())

        confirmed = await asyncio.to_thread(cancel_supervised_run, socket_path, request_id)
        assert confirmed is True
        # By the time cancel_supervised_run returns True, the process group must already be gone.
        with pytest.raises(ProcessLookupError):
            os.killpg(child_pid, 0)

        if not run_task.done():
            run_task.cancel()
        with suppress(asyncio.CancelledError):
            await run_task

        # A command that already finished normally must not report stopped=True.
        done_id = f"req-{uuid4().hex}"
        res = await run_command_via_supervisor(
            socket_path,
            namespace="test",
            argv=["/bin/sh", "-c", "true"],
            env=dict(os.environ),
            cwd=str(tmp_path),
            tail=10,
            timeout=5.0,
            on_timeout="kill",
            request_id=done_id,
        )
        assert res.execution_error is False
        assert await asyncio.to_thread(cancel_supervised_run, socket_path, done_id) is False
    finally:
        manager.shutdown()


# ---------------------------------------------------------------------------
# Item 2: Cancel before the command started says it never ran
# ---------------------------------------------------------------------------


def test_r1_item2_cancel_never_ran_prints_never_ran_notice(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When cancel returns cancel_outcome='never_ran', stderr says the command never ran."""
    installed: dict[int, object] = {}

    def fake_signal(signum: int, handler: object) -> object:
        prev = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        return prev

    class Client:
        def operation(self, payload: dict[str, object]) -> dict[str, object]:
            return {"status": "running", "call_id": payload["call_id"]}

        def receipt(self, call_id: str, **_kwargs: object) -> dict[str, object]:
            del call_id
            handler = installed[int(signal.SIGTERM)]
            assert callable(handler)
            handler(int(signal.SIGTERM), None)
            pytest.fail("signal should interrupt")

        def cancel(self, call_id: str, *, timeout: float = 8.0) -> dict[str, object]:
            del call_id, timeout
            return {"status": "cancelled", "cancel_outcome": "never_ran"}

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr(signal, "signal", fake_signal)

    assert main(["workers", "run", "f2", "--", "sleep", "60"]) == 128 + int(signal.SIGTERM)
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "mindroom-agent: cancelled by SIGTERM; the command never ran in worker f2\n"


@pytest.mark.asyncio
async def test_r1_item2_queued_and_pre_arm_cancel_report_never_ran(tmp_path: Path) -> None:
    """Queued calls and pre-arm WorkerCallCancellation report cancel_outcome='never_ran'."""
    # Pre-arm WorkerCallCancellation (cold start cancel before _arm)
    cancellation = WorkerCallCancellation()
    assert await cancellation.cancel_and_confirm(wait_seconds=1.0) == "never_ran"

    # Queued call in LiveTurnTools
    outer_entered = asyncio.Event()
    release_outer = asyncio.Event()

    async def run_shell_command(args: str) -> str:
        if args == "outer":
            outer_entered.set()
            await release_outer.wait()
            return "outer done"
        return args

    owner, _token = await _make_owner(tmp_path, run_shell_command)
    try:
        async with response_cli_lifetime():
            await capture_bash_batch(owner, ("outer-1", {"command": "outer"}))
            bash_task = asyncio.create_task(
                owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "outer"}, "outer-1"),
            )
            async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                await outer_entered.wait()
                call_id = uuid4()
                await owner.operation(
                    window="outer-1",
                    operation=ToolCallOperation(
                        operation="tools.call",
                        call_id=call_id,
                        toolkit="shell",
                        function="run_shell_command",
                        arguments={"args": "never"},
                    ),
                )
                receipt = await owner.cancel_call(str(call_id))
                assert receipt["status"] == "cancelled"
                assert receipt["cancel_outcome"] == "never_ran"
                release_outer.set()
                await bash_task
    finally:
        await owner.close()


# ---------------------------------------------------------------------------
# Item 3: Launch failure travels as data, not parsed text
# ---------------------------------------------------------------------------


def test_r1_item3_successful_command_printing_launch_failure_text_exits_0(
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


# ---------------------------------------------------------------------------
# Item 4: Only spawn errors of the executable are launch failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r1_item4_output_capture_eacces_stays_execution_error(tmp_path: Path) -> None:
    """EACCES from ShellOutputCapture temp-file creation stays an execution error, not launch exit 126."""
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
        )
        assert result.execution_error is True
        assert result.launch_exit_code is None
        assert result.launch_error is None
    finally:
        ro_dir.chmod(0o755)


# ---------------------------------------------------------------------------
# Item 5: Deadline on the event loop (saturated default executor)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r1_item5_cancel_confirm_deadline_runs_on_event_loop_with_saturated_executor() -> None:
    """cancel_and_confirm enforces wait_seconds on the event loop even when the default executor is saturated."""
    loop = asyncio.get_running_loop()
    release_pool = threading.Event()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(pool)
    pool_started = threading.Event()

    def occupy_pool() -> None:
        pool_started.set()
        release_pool.wait(timeout=10.0)

    blocker = loop.run_in_executor(None, occupy_pool)
    try:
        assert pool_started.wait(timeout=2.0)
        release_stop = threading.Event()
        cancellation = WorkerCallCancellation()
        cancellation._arm(lambda: release_stop.wait(timeout=10.0))

        started = time.monotonic()
        outcome = await asyncio.wait_for(cancellation.cancel_and_confirm(wait_seconds=0.1), timeout=1.0)
        elapsed = time.monotonic() - started
        assert outcome is None
        assert elapsed < 0.8
        release_stop.set()
    finally:
        release_pool.set()
        await blocker
        pool.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Items 6 & 7: TTY flag race + escaping exception
# ---------------------------------------------------------------------------


def test_r1_item6_signal_during_first_tty_progress_flush_still_ends_line(monkeypatch: pytest.MonkeyPatch) -> None:
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
            return {"status": "cancelled", "cancel_outcome": "stopped"}

    tty = _SignallingTty()
    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr("mindroom.agent_cli.main._POLL_FLOOR_SECONDS", 0.0)
    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr(sys, "stderr", tty)

    assert main(["workers", "run", "f1", "--", "sleep", "60"]) == 128 + int(signal.SIGTERM)
    assert tty.getvalue() == (
        "\r\x1b[Kmindroom-agent: starting worker f1...\n"
        "mindroom-agent: cancelled by SIGTERM; stopped the command in worker f1\n"
    )


def test_r1_item7_escaping_exception_ends_open_tty_progress_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected escaping exception ends an open TTY progress line with a newline."""

    class _Tty(StringIO):
        def isatty(self) -> bool:
            return True

    step = 0

    class Client:
        def operation(self, payload: dict[str, object]) -> dict[str, object]:
            return {
                "status": "running",
                "call_id": payload["call_id"],
                "progress": "starting worker f1...",
                "progress_revision": 1,
            }

        def receipt(self, call_id: str, **_kwargs: object) -> dict[str, object]:
            nonlocal step
            del call_id
            step += 1
            msg = "unexpected boom"
            raise RuntimeError(msg)

    tty = _Tty()
    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr("mindroom.agent_cli.main._POLL_FLOOR_SECONDS", 0.0)
    monkeypatch.setattr(sys, "stderr", tty)

    with pytest.raises(RuntimeError, match="unexpected boom"):
        main(["workers", "run", "f1", "--", "echo", "hi"])
    assert tty.getvalue() == "\r\x1b[Kmindroom-agent: starting worker f1...\n"


@pytest.mark.asyncio
async def test_r1_item3_launch_error_travels_through_sandbox_proxy_into_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A structured launch error from the worker populates receipt launch_exit_code and launch_error via sandbox_proxy."""
    import json  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    from mindroom.constants import resolve_primary_runtime_paths  # noqa: PLC0415
    from mindroom.tool_system.catalog import builtin_tool_registry  # noqa: PLC0415
    from mindroom.tool_system.sandbox_proxy import WorkerStandIns  # noqa: PLC0415
    from mindroom.workers.models import WorkerHandle  # noqa: PLC0415
    from mindroom.workers.protocol import WORKER_PROTOCOL_VERSION, ExecuteRequest  # noqa: PLC0415
    from tests.test_workers_run_small_fixes import _FakeWorkerManager  # noqa: PLC0415

    handle = WorkerHandle(
        worker_id="worker-f2",
        worker_key="agent:test:named=f2",
        endpoint="http://worker/api/sandbox-runner/execute",
        auth_token="test-token",  # noqa: S106
        status="ready",
        backend_name="kubernetes",
        last_used_at=0.0,
        created_at=0.0,
    )

    class _LaunchErrorHttpClient:
        def __init__(self, *, timeout: float) -> None:
            del timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_exc: object) -> None:
            return

        @contextmanager
        def stream(self, method: str, url: str, **_kwargs: object) -> Iterator[httpx.Response]:
            body = json.dumps(
                {
                    "ok": True,
                    "result": {
                        "mindroom_shell_launch_error": {
                            "exit_code": 127,
                            "message": "/nonexistent: No such file or directory",
                        },
                    },
                },
            ).encode()
            yield httpx.Response(200, content=body, request=httpx.Request(method, url))

    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_WORKER_BACKEND": "kubernetes"},
    )

    @contextmanager
    def fake_lease(_rp: object, _pc: object) -> Iterator[_FakeWorkerManager]:
        yield _FakeWorkerManager(handle)

    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.lease_worker_manager", fake_lease)
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy._build_execute_request",
        lambda *_args, **_kwargs: (
            ExecuteRequest(
                protocol=WORKER_PROTOCOL_VERSION,
                tool_name="shell",
                function_name="run_shell_command",
                args=[],
                kwargs={"args": ["/nonexistent"], "worker": "f2"},
            ),
            handle,
        ),
    )

    class _FakeNamed:
        name = "f2"

        def header(self, _handle: object) -> str:
            return "[worker: f2]"

    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy._named_worker_call",
        lambda *_args, **_kwargs: _FakeNamed(),
    )
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _LaunchErrorHttpClient)

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
                call_id = uuid4()
                await owner.operation(
                    window="outer-1",
                    operation=ToolCallOperation(
                        operation="tools.call",
                        call_id=call_id,
                        toolkit="shell",
                        function="run_shell_command",
                        arguments={"args": "/nonexistent"},
                    ),
                )
                receipt = await owner.get_call(str(call_id), wait=5.0)
                assert receipt["status"] == "completed"
                assert receipt["launch_exit_code"] == 127
                assert receipt["launch_error"] == "/nonexistent: No such file or directory"
                assert receipt["outcome"] == "[worker: f2]\n/nonexistent: No such file or directory"
                release_outer.set()
                await bash_task
    finally:
        await owner.close()
