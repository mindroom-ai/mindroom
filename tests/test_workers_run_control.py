"""Tests for `workers run` control plane: cancel, long-poll, progress, stderr, and exit codes."""

# ruff: noqa: ANN001, ANN202, ARG001, PLR0915
from __future__ import annotations

import asyncio
import json
import signal
import time
import urllib.request
from io import BytesIO
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from mindroom.agent_cli import turn
from mindroom.agent_cli.client import AgentCliClient, AgentCliUnavailableError
from mindroom.agent_cli.lifetime import response_cli_lifetime
from mindroom.agent_cli.main import main
from mindroom.agent_cli.protocol import ToolCallOperation
from mindroom.agent_cli.session import CliTurnOwner, TurnToolRegistry
from mindroom.agent_cli.shell_contract import AGENT_CLI_WINDOW_HEADER, AgentCliShellEnv
from mindroom.api.agent_cli import bind_agent_cli_registry, router
from mindroom.session_storage.agent_storage import create_agent_state_storage
from mindroom.shell.output_capture import format_shell_completion, format_shell_timeout
from mindroom.tool_system import runtime_context
from mindroom.tool_system.output_files import ToolOutputFilePolicy
from mindroom.tool_system.sandbox_proxy import _make_progress_sink
from mindroom.tool_system.tool_access import ToolKey
from mindroom.workers.models import WorkerReadyProgress
from tests.conftest import SETTLE_TIMEOUT_SECONDS, live_turn_tools_for_test
from tests.minimal_agent_fixtures import capture_bash_batch
from tests.test_agent_tool_calls import _catalog, _toolkit

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_cli.turn import LiveTurnTools


_SHELL_ENV = AgentCliShellEnv("http://127.0.0.1:9", "test-grant")


async def _make_owner(
    tmp_path: Path,
    run_shell_command,
    *,
    turn_id: str = "turn",
) -> tuple[LiveTurnTools, str]:
    catalog = await _catalog(tmp_path / turn_id, [_toolkit("shell", run_shell_command)])
    catalog.store = create_agent_state_storage("helper", tmp_path / turn_id)

    async def authorize(key, arguments) -> None:
        return None

    owner = live_turn_tools_for_test(
        CliTurnOwner(catalog.runtime_context.execution_identity, turn_id, "run"),
        catalog=catalog,
        authorize=authorize,
        output_file_policy=ToolOutputFilePolicy(tmp_path / "workspace"),
    )
    owner.shell_env = _SHELL_ENV
    grant = owner.issue(now_ns=time.time_ns(), expires_at_ns=time.time_ns() + 10**12)
    return owner, grant.raw_token


def _make_app(*owners: LiveTurnTools) -> FastAPI:
    registry = TurnToolRegistry()
    for owner in owners:
        registry.register(owner)
    app = FastAPI()
    app.include_router(router)
    bind_agent_cli_registry(app, registry)
    return app


@pytest.mark.asyncio
async def test_cancel_route_cancels_running_call_without_cancelling_sibling(tmp_path: Path) -> None:
    """Cancelling one CLI call cancels only its task and settles its receipt to ``cancelled``."""
    outer_entered = asyncio.Event()
    release_outer = asyncio.Event()
    long_started = asyncio.Event()
    long_cancelled = asyncio.Event()
    sibling_started = asyncio.Event()
    release_sibling = asyncio.Event()

    async def run_shell_command(args: str) -> str:
        if args == "outer":
            outer_entered.set()
            await release_outer.wait()
            return "outer done"
        if args == "long":
            long_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                long_cancelled.set()
                raise
        if args == "sibling":
            sibling_started.set()
            await release_sibling.wait()
            return "sibling done"
        return args

    owner, token = await _make_owner(tmp_path, run_shell_command)
    app = _make_app(owner)
    headers = {"Authorization": f"Bearer {token}", AGENT_CLI_WINDOW_HEADER: "outer-1"}

    try:
        async with response_cli_lifetime():
            await capture_bash_batch(owner, ("outer-1", {"command": "outer"}))
            bash_task = asyncio.create_task(
                owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "outer"}, "outer-1"),
            )
            async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                await outer_entered.wait()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
                    long_id = str(uuid4())
                    sibling_id = str(uuid4())
                    for call_id, arg in ((long_id, "long"), (sibling_id, "sibling")):
                        resp = await http.post(
                            "/api/agent-cli/operations",
                            headers=headers,
                            json={
                                "operation": "tools.call",
                                "call_id": call_id,
                                "toolkit": "shell",
                                "function": "run_shell_command",
                                "arguments": {"args": arg},
                            },
                        )
                        assert resp.status_code == 200

                    await long_started.wait()
                    await sibling_started.wait()

                    cancel_resp = await http.post(
                        f"/api/agent-cli/calls/{long_id}/cancel",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert cancel_resp.status_code == 200
                    assert cancel_resp.json()["status"] == "cancelled"
                    assert long_cancelled.is_set()

                    # Sibling is unaffected and can finish normally.
                    release_sibling.set()
                    sibling_resp = await http.get(
                        f"/api/agent-cli/calls/{sibling_id}?wait=5",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert sibling_resp.status_code == 200
                    assert sibling_resp.json()["status"] == "completed"
                    assert sibling_resp.json()["outcome"] == "sibling done"

                    # Cancelling a completed call is idempotent and leaves its receipt unchanged.
                    after_complete = await http.post(
                        f"/api/agent-cli/calls/{sibling_id}/cancel",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert after_complete.status_code == 200
                    assert after_complete.json()["status"] == "completed"
                    assert after_complete.json()["outcome"] == "sibling done"

                release_outer.set()
                assert (await bash_task).content == "outer done"
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_cancel_before_dispatch_marks_call_terminal_cancelled_without_running(tmp_path: Path) -> None:
    """A cancel arriving while the call is still queued stops it before dispatch and settles it."""
    outer_entered = asyncio.Event()
    release_outer = asyncio.Event()
    ran: list[str] = []

    async def run_shell_command(args: str) -> str:
        if args == "outer":
            outer_entered.set()
            await release_outer.wait()
            return "outer done"
        ran.append(args)
        return "should not run"

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
                queued = await owner.operation(
                    window="outer-1",
                    operation=ToolCallOperation(
                        operation="tools.call",
                        call_id=call_id,
                        toolkit="shell",
                        function="run_shell_command",
                        arguments={"args": "never"},
                    ),
                )
                assert queued["status"] == "queued"

                # Cancel immediately before yielding so _dispatch has not started yet.
                cancelled = await owner.cancel_call(str(call_id))
                assert cancelled["status"] == "cancelled"

                await asyncio.sleep(0)
                await asyncio.sleep(0)
                receipt = await owner.get_call(str(call_id))
                assert receipt["status"] == "cancelled"
                assert ran == []

                release_outer.set()
                await bash_task
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_cancel_is_scoped_to_owner_bearer(tmp_path: Path) -> None:
    """Another owner's bearer token gets 401 and cannot cancel this owner's call."""
    outer_entered = asyncio.Event()
    release_outer = asyncio.Event()
    child_started = asyncio.Event()
    release_child = asyncio.Event()

    async def run_shell_command(args: str) -> str:
        if args == "outer":
            outer_entered.set()
            await release_outer.wait()
            return "outer done"
        child_started.set()
        await release_child.wait()
        return "child done"

    owner_a, token_a = await _make_owner(tmp_path, run_shell_command, turn_id="turn-a")
    owner_b, token_b = await _make_owner(tmp_path, run_shell_command, turn_id="turn-b")
    app = _make_app(owner_a, owner_b)

    try:
        async with response_cli_lifetime():
            await capture_bash_batch(owner_a, ("outer-a", {"command": "outer"}))
            bash_task = asyncio.create_task(
                owner_a.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "outer"}, "outer-a"),
            )
            async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                await outer_entered.wait()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
                    call_id = str(uuid4())
                    resp = await http.post(
                        "/api/agent-cli/operations",
                        headers={"Authorization": f"Bearer {token_a}", AGENT_CLI_WINDOW_HEADER: "outer-a"},
                        json={
                            "operation": "tools.call",
                            "call_id": call_id,
                            "toolkit": "shell",
                            "function": "run_shell_command",
                            "arguments": {"args": "child"},
                        },
                    )
                    assert resp.status_code == 200
                    await child_started.wait()

                    other = await http.post(
                        f"/api/agent-cli/calls/{call_id}/cancel",
                        headers={"Authorization": f"Bearer {token_b}"},
                    )
                    assert other.status_code == 401

                    release_child.set()
                    done = await http.get(
                        f"/api/agent-cli/calls/{call_id}?wait=5",
                        headers={"Authorization": f"Bearer {token_a}"},
                    )
                    assert done.status_code == 200
                    assert done.json()["status"] == "completed"
                    assert done.json()["outcome"] == "child done"

                release_outer.set()
                await bash_task
    finally:
        await owner_a.close()
        await owner_b.close()


@pytest.mark.asyncio
async def test_long_poll_and_cancel_do_not_consume_active_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Long-poll returns promptly on settle and both long-poll and cancel work when capacity is full."""
    monkeypatch.setattr(turn, "_MAX_ACTIVE_OPERATIONS", 1)
    outer_entered = asyncio.Event()
    release_outer = asyncio.Event()
    child_started = asyncio.Event()

    async def run_shell_command(args: str) -> str:
        if args == "outer":
            outer_entered.set()
            await release_outer.wait()
            return "outer done"
        child_started.set()
        await asyncio.Event().wait()
        return "unreachable"

    owner, token = await _make_owner(tmp_path, run_shell_command)
    app = _make_app(owner)
    headers = {"Authorization": f"Bearer {token}", AGENT_CLI_WINDOW_HEADER: "outer-1"}

    try:
        async with response_cli_lifetime():
            await capture_bash_batch(owner, ("outer-1", {"command": "outer"}))
            bash_task = asyncio.create_task(
                owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "outer"}, "outer-1"),
            )
            async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                await outer_entered.wait()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
                    call_id = str(uuid4())
                    resp = await http.post(
                        "/api/agent-cli/operations",
                        headers=headers,
                        json={
                            "operation": "tools.call",
                            "call_id": call_id,
                            "toolkit": "shell",
                            "function": "run_shell_command",
                            "arguments": {"args": "child"},
                        },
                    )
                    assert resp.status_code == 200
                    await child_started.wait()

                    # Capacity is now full (1/1): another call must be refused.
                    overflow = await http.post(
                        "/api/agent-cli/operations",
                        headers=headers,
                        json={
                            "operation": "tools.call",
                            "call_id": str(uuid4()),
                            "toolkit": "shell",
                            "function": "run_shell_command",
                            "arguments": {"args": "second"},
                        },
                    )
                    assert overflow.status_code == 422

                    # Start long-poll while capacity is full, then cancel while capacity is full.
                    started = time.monotonic()
                    poll_task = asyncio.create_task(
                        http.get(
                            f"/api/agent-cli/calls/{call_id}?wait=25",
                            headers={"Authorization": f"Bearer {token}"},
                        ),
                    )
                    await asyncio.sleep(0.02)
                    assert not poll_task.done()

                    cancel_resp = await http.post(
                        f"/api/agent-cli/calls/{call_id}/cancel",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert cancel_resp.status_code == 200
                    assert cancel_resp.json()["status"] == "cancelled"

                    poll_resp = await poll_task
                    elapsed = time.monotonic() - started
                    assert poll_resp.status_code == 200
                    assert poll_resp.json()["status"] == "cancelled"
                    assert elapsed < 1.0

                release_outer.set()
                await bash_task
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_long_poll_wakes_on_worker_warmup_progress_and_on_timeout(tmp_path: Path) -> None:
    """Warmup progress reaches the CLI call receipt and wakes an active long-poll immediately."""
    outer_entered = asyncio.Event()
    release_outer = asyncio.Event()
    child_started = asyncio.Event()
    emit_progress = asyncio.Event()
    finish_child = asyncio.Event()

    async def run_shell_command(args: str) -> str:
        if args == "outer":
            outer_entered.set()
            await release_outer.wait()
            return "outer done"
        child_started.set()
        sink = _make_progress_sink(
            None,
            reply_timing=None,
            call_progress=runtime_context.get_call_progress_sink(),
            worker_name="fan3",
            tool_name="shell",
            function_name="run_shell_command",
        )
        await emit_progress.wait()
        sink(
            WorkerReadyProgress(
                phase="cold_start",
                worker_key="w",
                backend_name="kubernetes",
                elapsed_seconds=1.6,
            ),
        )
        await finish_child.wait()
        return "[worker: fan3, started]\nok"

    owner, token = await _make_owner(tmp_path, run_shell_command)
    app = _make_app(owner)
    headers = {"Authorization": f"Bearer {token}", AGENT_CLI_WINDOW_HEADER: "outer-1"}

    try:
        async with response_cli_lifetime():
            await capture_bash_batch(owner, ("outer-1", {"command": "outer"}))
            bash_task = asyncio.create_task(
                owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "outer"}, "outer-1"),
            )
            async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                await outer_entered.wait()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
                    call_id = str(uuid4())
                    resp = await http.post(
                        "/api/agent-cli/operations",
                        headers=headers,
                        json={
                            "operation": "tools.call",
                            "call_id": call_id,
                            "toolkit": "shell",
                            "function": "run_shell_command",
                            "arguments": {"args": "child"},
                        },
                    )
                    assert resp.status_code == 200
                    await child_started.wait()

                    # Short wait expires with the running receipt.
                    expired = await http.get(
                        f"/api/agent-cli/calls/{call_id}?wait=0.05",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert expired.status_code == 200
                    assert expired.json()["status"] == "running"
                    assert expired.json()["progress"] is None

                    # Long-poll wakes as soon as progress arrives.
                    started = time.monotonic()
                    poll_task = asyncio.create_task(
                        http.get(
                            f"/api/agent-cli/calls/{call_id}?wait=25",
                            headers={"Authorization": f"Bearer {token}"},
                        ),
                    )
                    await asyncio.sleep(0.02)
                    emit_progress.set()
                    progress_resp = await poll_task
                    assert time.monotonic() - started < 1.0
                    assert progress_resp.status_code == 200
                    body = progress_resp.json()
                    assert body["status"] == "running"
                    assert body["progress"] is not None
                    assert "fan3" in body["progress"]

                    # Next long-poll wakes immediately on completion.
                    started = time.monotonic()
                    settle_task = asyncio.create_task(
                        http.get(
                            f"/api/agent-cli/calls/{call_id}?wait=25",
                            headers={"Authorization": f"Bearer {token}"},
                        ),
                    )
                    await asyncio.sleep(0.02)
                    finish_child.set()
                    done_resp = await settle_task
                    assert time.monotonic() - started < 1.0
                    assert done_resp.status_code == 200
                    assert done_resp.json()["status"] == "completed"

                release_outer.set()
                await bash_task
    finally:
        await owner.close()


@pytest.mark.parametrize(
    ("sig", "expected_code", "sig_name"),
    [
        (signal.SIGTERM, 143, "SIGTERM"),
        (signal.SIGINT, 130, "SIGINT"),
        (signal.SIGHUP, 129, "SIGHUP"),
    ],
)
def test_cli_workers_run_cancels_on_signal_and_exits_128_plus_signo(
    sig: signal.Signals,
    expected_code: int,
    sig_name: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal during the wait triggers cancel and exits 128+signo with one stderr line."""
    installed: dict[int, object] = {}
    cancelled: list[tuple[str, float]] = []
    submitted: list[str] = []

    def fake_signal(signum, handler):
        previous = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        return previous

    class Client:
        def operation(self, payload):
            submitted.append(payload["call_id"])
            return {"status": "running", "call_id": payload["call_id"]}

        def receipt(
            self,
            call_id,
            *,
            timeout: float = 30,
            wait: float | None = None,
            after_progress: int | None = None,
        ):
            del call_id, timeout, wait, after_progress
            handler = installed[sig]
            assert callable(handler)
            handler(int(sig), None)
            pytest.fail("signal handler should interrupt wait")

        def cancel(self, call_id, *, timeout: float = 8.0):
            cancelled.append((call_id, timeout))
            return {"status": "cancelled", "call_id": call_id, "dispatched": False}

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr(signal, "signal", fake_signal)

    exit_code = main(["workers", "run", "fan3", "--", "sleep", "300"])
    assert exit_code == expected_code
    [call_id] = submitted
    assert len(cancelled) == 1
    assert cancelled[0][0] == call_id
    assert cancelled[0][1] <= 8.0
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == f"mindroom-agent: cancelled by {sig_name} (call {call_id})\n"


def test_cli_workers_run_warns_when_signal_cancel_cannot_be_confirmed(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the cancel request fails, stderr warns that the command may still be running with its call id."""
    installed: dict[int, object] = {}
    submitted: list[str] = []

    def fake_signal(signum, handler):
        previous = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        return previous

    class Client:
        def operation(self, payload):
            submitted.append(payload["call_id"])
            return {"status": "running", "call_id": payload["call_id"]}

        def receipt(
            self,
            call_id,
            *,
            timeout: float = 30,
            wait: float | None = None,
            after_progress: int | None = None,
        ):
            del call_id, timeout, wait, after_progress
            handler = installed[signal.SIGTERM]
            assert callable(handler)
            handler(int(signal.SIGTERM), None)
            pytest.fail("signal handler should interrupt wait")

        def cancel(self, call_id, *, timeout: float = 8.0):
            del call_id, timeout
            msg = "Agent CLI transport outcome is unknown"
            raise AgentCliUnavailableError(msg)

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr(signal, "signal", fake_signal)

    assert main(["workers", "run", "fan3", "--", "sleep", "300"]) == 143
    [call_id] = submitted
    output = capsys.readouterr()
    assert output.out == ""
    lines = output.err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("mindroom-agent: cancelled by SIGTERM")
    assert "may still be running" in lines[0]
    assert f"(call {call_id})" in lines[0]


def test_cli_workers_run_cancels_on_broken_stdout_pipe(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BrokenPipe on stdout cancels the remote call instead of leaking it."""
    cancelled: list[str] = []
    submitted: list[str] = []

    class Client:
        def operation(self, payload):
            submitted.append(payload["call_id"])
            return {"status": "running", "call_id": payload["call_id"]}

        def receipt(
            self,
            call_id,
            *,
            timeout: float = 30,
            wait: float | None = None,
            after_progress: int | None = None,
        ):
            del call_id, timeout, wait, after_progress
            raise BrokenPipeError

        def cancel(self, call_id, *, timeout: float = 5.0):
            del timeout
            cancelled.append(call_id)
            return {"status": "cancelled", "call_id": call_id}

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)

    exit_code = main(["workers", "run", "fan3", "--", "sleep", "300"])
    assert exit_code == 128 + signal.SIGPIPE
    assert cancelled == submitted
    output = capsys.readouterr()
    assert output.out == ""
    assert f"(call {submitted[0]})" in output.err


def test_cli_workers_run_uses_long_poll_without_sleeping_and_prints_progress_and_started_to_stderr(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Healthy polling uses `?wait=25` with zero sleep and writes progress + started lines to stderr."""
    waits: list[float | None] = []
    receipts = iter(
        [
            {"status": "running", "progress": "starting worker fan3...", "progress_revision": 1},
            {"status": "running", "progress": "starting worker fan3...", "progress_revision": 1},
            {"status": "running", "progress": "starting worker fan3... (5s)", "progress_revision": 2},
            {
                "status": "completed",
                "outcome": "[worker: fan3, started]\n" + format_shell_completion("hello", None, return_code=0),
            },
        ],
    )

    class Client:
        def operation(self, payload):
            return {"status": "queued", "call_id": payload["call_id"]}

        def receipt(
            self,
            call_id,
            *,
            timeout: float = 30,
            wait: float | None = None,
            after_progress: int | None = None,
        ):
            del call_id, timeout, after_progress
            waits.append(wait)
            return next(receipts)

    now = 0.0

    class Clock:
        def sleep(self, _seconds: float) -> None:
            pytest.fail("healthy long-poll must not sleep")

        def monotonic(self) -> float:
            nonlocal now
            now += 1.0
            return now

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr("mindroom.agent_cli.main.time", Clock())

    assert main(["workers", "run", "fan3", "--", "echo", "hello"]) == 0
    assert waits == [25.0, 25.0, 25.0, 25.0]
    output = capsys.readouterr()
    assert output.out == "hello\n"
    assert output.err == (
        "mindroom-agent: starting worker fan3...\n"
        "mindroom-agent: starting worker fan3... (5s)\n"
        "mindroom-agent: started worker fan3\n"
    )


def test_cli_workers_run_prints_stopped_after_on_stderr_not_stdout(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeout stop message goes to stderr while partial command output stays on stdout."""
    outcome = "[worker: w]\n" + format_shell_timeout("partial", "warn", timeout=5)

    class Client:
        def operation(self, payload):
            return {"status": "completed", "call_id": payload["call_id"], "outcome": outcome}

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)

    assert main(["workers", "run", "w", "--timeout", "5", "--", "sleep", "30"]) == 124
    output = capsys.readouterr()
    assert output.out == "Output:\npartial\nStderr:\nwarn\n"
    assert output.err == "mindroom-agent: stopped after 5s\n"


def test_cli_workers_run_usage_error_writes_only_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Usage errors on `workers run` exit 2 and never pollute stdout with JSON."""
    assert main(["workers", "run", "w", "--tail", "0", "--", "true"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "mindroom-agent:" in output.err
    assert "--tail" in output.err


def test_cli_workers_run_splits_exit_255_unknown_from_exit_125_not_run(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusal before run exits 125; unknown outcome after submission exits 255 with call id."""

    class RejectedClient:
        def operation(self, payload):
            del payload
            msg = "Agent CLI request was rejected: limit reached"
            raise ValueError(msg)

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", RejectedClient)
    assert main(["workers", "run", "w", "--", "true"]) == 125
    out_refused = capsys.readouterr()
    assert out_refused.out == ""
    assert "limit reached" in out_refused.err

    # Transport lost after submission -> 255 with call id on stderr.
    now = 0.0

    class Clock:
        def sleep(self, seconds: float) -> None:
            nonlocal now
            now += seconds

        def monotonic(self) -> float:
            return now

    submissions: list[dict[str, object]] = []

    def open_request(_opener, request: urllib.request.Request, *, timeout: float) -> BytesIO:
        del timeout
        if request.get_method() == "POST":
            assert request.data is not None
            submissions.append(json.loads(request.data))
            return BytesIO(b'{"status":"running"}')
        raise TimeoutError

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", AgentCliClient)
    monkeypatch.setenv("MINDROOM_AGENT_CLI_URL", "http://primary:8765")
    monkeypatch.setenv("MINDROOM_AGENT_CLI_TOKEN", "private-capability")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", open_request)
    monkeypatch.setattr("mindroom.agent_cli.main.time", Clock())

    assert main(["workers", "run", "w", "--", "sleep", "30"]) == 255
    out_unknown = capsys.readouterr()
    assert out_unknown.out == ""
    assert f"(call {submissions[0]['call_id']})" in out_unknown.err
    assert "may still be running" in out_unknown.err


@pytest.mark.asyncio
async def test_long_poll_rejects_negative_and_nonfinite_wait(tmp_path: Path) -> None:
    """Invalid `?wait=` query values are rejected with 422."""

    async def run_shell_command(args: str) -> str:
        return args

    owner, token = await _make_owner(tmp_path, run_shell_command)
    app = _make_app(owner)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            for bad in ("-1", "nan", "inf"):
                resp = await http.get(
                    f"/api/agent-cli/calls/{uuid4()}?wait={bad}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert resp.status_code == 422
    finally:
        await owner.close()


def test_progress_sink_formats_cold_start_waiting_and_ready_for_call_progress() -> None:
    """`cold_start`, `waiting`, and `ready` forward bounded messages; `failed` does not duplicate errors."""
    messages: list[str] = []
    sink = _make_progress_sink(
        None,
        reply_timing=None,
        call_progress=messages.append,
        worker_name="fan3",
        tool_name="shell",
        function_name="run_shell_command",
    )
    for phase, elapsed in (("cold_start", 1.6), ("waiting", 6.2), ("ready", 12.4), ("failed", 15.0)):
        sink(
            WorkerReadyProgress(
                phase=phase,
                worker_key="w",
                backend_name="kubernetes",
                elapsed_seconds=elapsed,
                error="boom" if phase == "failed" else None,
            ),
        )
    assert messages == [
        "starting worker fan3...",
        "starting worker fan3... (6s)",
        "worker fan3 ready (12s)",
    ]


def test_http_client_cancel_and_long_poll_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """`AgentCliClient.cancel` posts to `/calls/{id}/cancel` and `receipt(..., wait=25)` passes `?wait=25`."""
    import threading  # noqa: PLC0415
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: PLC0415

    seen: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(("GET", self.path))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"running"}')

        def do_POST(self) -> None:
            seen.append(("POST", self.path))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"cancelled"}')

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("MINDROOM_AGENT_CLI_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("MINDROOM_AGENT_CLI_TOKEN", "private-capability")
    call_id = str(uuid4())
    try:
        client = AgentCliClient()
        assert client.receipt(call_id, wait=25)["status"] == "running"
        assert client.cancel(call_id)["status"] == "cancelled"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert seen == [
        ("GET", f"/api/agent-cli/calls/{call_id}?wait=25"),
        ("POST", f"/api/agent-cli/calls/{call_id}/cancel"),
    ]


# ---------------------------------------------------------------------------
# Round 1 review fixes (tests-first against 7d2f8bb6a)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_early_cancel_before_submit_prevents_execution_and_is_owner_scoped(tmp_path: Path) -> None:
    """An early cancel for an unknown id returns 401, stops a late submit of that owner, and never affects another owner."""
    outer_a_entered = asyncio.Event()
    release_outer_a = asyncio.Event()
    outer_b_entered = asyncio.Event()
    release_outer_b = asyncio.Event()
    ran: list[str] = []

    async def run_shell_command(args: str) -> str:
        if args == "outer-a":
            outer_a_entered.set()
            await release_outer_a.wait()
            return "outer-a done"
        if args == "outer-b":
            outer_b_entered.set()
            await release_outer_b.wait()
            return "outer-b done"
        ran.append(args)
        return f"ran:{args}"

    owner_a, token_a = await _make_owner(tmp_path, run_shell_command, turn_id="early-a")
    owner_b, token_b = await _make_owner(tmp_path, run_shell_command, turn_id="early-b")
    app = _make_app(owner_a, owner_b)

    try:
        async with response_cli_lifetime():
            await capture_bash_batch(owner_a, ("win-a", {"command": "outer-a"}))
            await capture_bash_batch(owner_b, ("win-b", {"command": "outer-b"}))
            task_a = asyncio.create_task(
                owner_a.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "outer-a"}, "win-a"),
            )
            task_b = asyncio.create_task(
                owner_b.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "outer-b"}, "win-b"),
            )
            async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                await outer_a_entered.wait()
                await outer_b_entered.wait()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
                    early_id = str(uuid4())
                    other_id = str(uuid4())

                    # Owner A cancels early_id BEFORE submit arrives -> 401 (does not leak existence).
                    resp_a = await http.post(
                        f"/api/agent-cli/calls/{early_id}/cancel",
                        headers={"Authorization": f"Bearer {token_a}"},
                    )
                    assert resp_a.status_code == 401

                    # Owner B cancels other_id early -> 401, must NOT affect Owner A submitting other_id.
                    resp_b = await http.post(
                        f"/api/agent-cli/calls/{other_id}/cancel",
                        headers={"Authorization": f"Bearer {token_b}"},
                    )
                    assert resp_b.status_code == 401

                    # Now Owner A's delayed submit for early_id arrives -> immediately terminal cancelled, never runs.
                    sub_early = await http.post(
                        "/api/agent-cli/operations",
                        headers={"Authorization": f"Bearer {token_a}", AGENT_CLI_WINDOW_HEADER: "win-a"},
                        json={
                            "operation": "tools.call",
                            "call_id": early_id,
                            "toolkit": "shell",
                            "function": "run_shell_command",
                            "arguments": {"args": "must-not-run"},
                        },
                    )
                    assert sub_early.status_code == 200
                    assert sub_early.json()["status"] == "cancelled"

                    # Owner A submitting other_id still runs normally despite Owner B's early cancel.
                    sub_other = await http.post(
                        "/api/agent-cli/operations",
                        headers={"Authorization": f"Bearer {token_a}", AGENT_CLI_WINDOW_HEADER: "win-a"},
                        json={
                            "operation": "tools.call",
                            "call_id": other_id,
                            "toolkit": "shell",
                            "function": "run_shell_command",
                            "arguments": {"args": "allowed"},
                        },
                    )
                    assert sub_other.status_code == 200
                    done_other = await http.get(
                        f"/api/agent-cli/calls/{other_id}?wait=5",
                        headers={"Authorization": f"Bearer {token_a}"},
                    )
                    assert done_other.json()["status"] == "completed"
                    assert done_other.json()["outcome"] == "ran:allowed"
                    assert ran == ["allowed"]

                release_outer_a.set()
                release_outer_b.set()
                await asyncio.gather(task_a, task_b)
    finally:
        await owner_a.close()
        await owner_b.close()


def test_cli_retries_cancel_once_after_401_and_reports_unconfirmed_remote_stop(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401 on the first cancel retries once after ~0.5s; a dispatched cancel warns that the command may still run."""
    installed: dict[int, object] = {}
    sleeps: list[float] = []
    cancel_calls: list[tuple[str, float]] = []
    submitted: list[str] = []

    def fake_signal(signum, handler):
        previous = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        return previous

    class Client:
        def operation(self, payload):
            submitted.append(payload["call_id"])
            return {"status": "running", "call_id": payload["call_id"]}

        def receipt(
            self,
            call_id,
            *,
            timeout: float = 30,
            wait: float | None = None,
            after_progress: int | None = None,
        ):
            del call_id, timeout, wait, after_progress
            handler = installed[signal.SIGTERM]
            assert callable(handler)
            handler(int(signal.SIGTERM), None)
            pytest.fail("signal handler should interrupt wait")

        def cancel(self, call_id, *, timeout: float = 8.0):
            cancel_calls.append((call_id, timeout))
            if len(cancel_calls) == 1:
                msg = "Agent CLI authority or transport is unavailable"
                raise AgentCliUnavailableError(msg, status_code=401)
            return {
                "status": "cancelled",
                "call_id": call_id,
                "dispatched": True,
                "outcome": "Call interrupted after dispatch; effects may have occurred",
            }

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr("mindroom.agent_cli.main.time.sleep", sleeps.append)

    assert main(["workers", "run", "fan3", "--", "sleep", "300"]) == 143
    [call_id] = submitted
    assert len(cancel_calls) == 2
    assert cancel_calls[0][1] >= 7.0  # Client cancel timeout has headroom over server's 5s settle wait.
    assert sleeps == [pytest.approx(0.5)]
    output = capsys.readouterr()
    assert output.out == ""
    assert "cancel requested" in output.err
    assert f"may still be running in worker fan3 (call {call_id})" in output.err


@pytest.mark.parametrize("case", ["final_write_epipe", "closed_during_wait_no_output"])
def test_real_pipe_subprocess_handles_closed_stdout_pipe(case: str) -> None:
    """EPIPE on write exits 141 without shutdown traceback; a command writing nothing exits with its own code."""
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415
    import threading  # noqa: PLC0415
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: PLC0415

    poll_entered = threading.Event()
    release_poll = threading.Event()
    cancelled: list[str] = []
    submitted: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            if self.path.endswith("/cancel"):
                cancelled.append(self.path)
                release_poll.set()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"cancelled","dispatched":true}')
                return
            payload = json.loads(body)
            submitted.append(payload["call_id"])
            if case == "final_write_epipe":
                poll_entered.set()
                release_poll.wait(timeout=5.0)
                receipt = {"status": "completed", "outcome": "[worker: w]\nhello from worker"}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(receipt).encode())
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"running"}')

        def do_GET(self) -> None:
            poll_entered.set()
            release_poll.wait(timeout=5.0)
            if case == "final_write_epipe":
                receipt = {"status": "completed", "outcome": "[worker: w]\nhello from worker"}
            else:
                receipt = {
                    "status": "completed",
                    "outcome": "[worker: w]\n" + format_shell_completion("", None, return_code=7),
                }
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(receipt).encode())

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {
        **os.environ,
        "MINDROOM_AGENT_CLI_URL": f"http://127.0.0.1:{server.server_port}",
        "MINDROOM_AGENT_CLI_TOKEN": "private-capability",
    }
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; from mindroom.agent_cli.main import main; raise SystemExit(main(['workers', 'run', 'w', '--', 'true']))",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        assert poll_entered.wait(timeout=5.0)
        # Close the reader end of the real pipe while the CLI is waiting on the long-poll.
        proc.stdout.close()
        if case == "final_write_epipe":
            # Let the server return a large output so final write hits EPIPE if not already caught.
            release_poll.set()
        else:
            # Let the server finish immediately with empty output after the reader closed.
            release_poll.set()
        stderr_bytes = proc.stderr.read()
        code = proc.wait(timeout=10.0)
    finally:
        release_poll.set()
        server.shutdown()
        server.server_close()
        thread.join()

    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    assert "Traceback" not in stderr_text
    assert "Exception ignored" not in stderr_text
    if case == "final_write_epipe":
        assert code == 141, f"expected 141, got {code}; stderr={stderr_text!r}"
        assert len(cancelled) >= 1
    else:
        assert code == 7, f"expected command exit code 7, got {code}; stderr={stderr_text!r}"
        assert cancelled == []


@pytest.mark.parametrize(
    ("receipt", "expected_code"),
    [
        ({"status": "cancelled", "dispatched": False, "outcome": "Call cancelled before dispatch"}, 125),
        ({"status": "failed", "dispatched": False, "outcome": "Cannot start worker 'w': limit reached"}, 125),
        (
            {
                "status": "cancelled",
                "dispatched": True,
                "outcome": "Call interrupted after dispatch; effects may have occurred",
            },
            255,
        ),
        (
            {
                "status": "failed",
                "dispatched": True,
                "outcome": "Sandbox proxy stream ended without a result.",
            },
            255,
        ),
    ],
)
def test_cli_exit_125_only_for_known_not_run_and_255_when_command_may_have_run(
    receipt: dict[str, object],
    expected_code: int,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 125 is reserved for failures known to precede launch; post-dispatch failures exit 255 with call id."""
    submitted: list[str] = []

    class Client:
        def operation(self, payload):
            submitted.append(payload["call_id"])
            return receipt

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)

    assert main(["workers", "run", "w", "--", "true"]) == expected_code
    output = capsys.readouterr()
    assert output.out == ""
    if expected_code == 255:
        assert f"(call {submitted[0]})" in output.err


def test_cli_output_save_failure_exits_255_with_call_id(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A completed command whose output auto-save failed already ran, so the CLI exits 255 with call id."""
    from mindroom.agent_cli.projection import project_cli_result  # noqa: PLC0415

    submitted: list[str] = []
    result = format_shell_completion("finished", None, return_code=0)
    policy = ToolOutputFilePolicy(tmp_path, max_bytes=1, auto_save_threshold_bytes=1)
    outcome = project_cli_result(f"[worker: w]\n{result}", policy, "run_shell_command")

    class Client:
        def operation(self, payload):
            submitted.append(payload["call_id"])
            return {"status": "completed", "outcome": outcome}

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)

    assert main(["workers", "run", "w", "--", "true"]) == 255
    output = capsys.readouterr()
    assert output.out == ""
    assert f"(call {submitted[0]})" in output.err


@pytest.mark.asyncio
async def test_concurrent_cancels_invoke_task_cancel_only_once(tmp_path: Path) -> None:
    """Two concurrent cancel requests call `task.cancel()` once so cleanup is not interrupted."""
    outer_entered = asyncio.Event()
    release_outer = asyncio.Event()
    child_started = asyncio.Event()
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_interrupted = False

    async def run_shell_command(args: str) -> str:
        nonlocal cleanup_interrupted
        if args == "outer":
            outer_entered.set()
            await release_outer.wait()
            return "outer done"
        child_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_entered.set()
            try:
                await release_cleanup.wait()
            except asyncio.CancelledError:
                cleanup_interrupted = True
                raise
            raise
        return "unreachable"

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
                        arguments={"args": "child"},
                    ),
                )
                await child_started.wait()

                cancel_1 = asyncio.create_task(owner.cancel_call(str(call_id)))
                await cleanup_entered.wait()
                # Second cancel arrives while first cancel's cleanup is running.
                cancel_2 = asyncio.create_task(owner.cancel_call(str(call_id)))
                await asyncio.sleep(0.02)
                release_cleanup.set()

                res_1, res_2 = await asyncio.gather(cancel_1, cancel_2)
                assert res_1["status"] == "cancelled"
                assert res_2["status"] == "cancelled"
                assert not cleanup_interrupted

                release_outer.set()
                await bash_task
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_cancel_during_approval_withdraws_pending_approval_and_unblocks_sibling(tmp_path: Path) -> None:
    """Cancelling a CLI call paused for approval withdraws its continuation so a sibling approval succeeds."""
    from functools import partial  # noqa: PLC0415
    from unittest.mock import AsyncMock, patch  # noqa: PLC0415

    from agno.tools.toolkit import Toolkit  # noqa: PLC0415

    from mindroom.final_delivery import TurnProgress  # noqa: PLC0415
    from mindroom.tool_system.agno_toolkits import adapt_toolkit  # noqa: PLC0415
    from tests.conftest import unwrap_extracted_collaborator  # noqa: PLC0415
    from tests.test_response_runner_focused import (  # noqa: PLC0415
        _admit_approval_source,
        _bot,
        _in_reply_span,
        _plain_request,
        _target,
    )

    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    first_paused = asyncio.Event()
    second_paused = asyncio.Event()
    published_ids: list[str] = []

    from mindroom.event_journal import CallKey  # noqa: PLC0415

    async def publish_gen(continuation, plan, **kwargs: object):
        del plan, kwargs
        published_ids.append(continuation.approval_id)
        await store.approvals.activate_approval_continuation(
            continuation.approval_id,
            expected_generation=continuation.generation,
        )
        if len(published_ids) == 1:
            first_paused.set()
        else:
            for call in continuation.calls:
                await store.approvals.resolve_approval_call(
                    CallKey(continuation.approval_id, continuation.generation, call.tool_call_id),
                    requested_status="approved",
                    reason=None,
                )
            runner.cli_waiters.wake(("$source",))
            second_paused.set()

    outer_entered = asyncio.Event()
    release_outer = asyncio.Event()

    async def run_shell_command(args: str) -> str:
        outer_entered.set()
        await release_outer.wait()
        return "outer done"

    async def gated(label: str) -> str:
        return f"approved:{label}"

    gated_toolkit = Toolkit(name="gated", tools=[gated], requires_confirmation_tools=["gated"])
    catalog = await _catalog(tmp_path / "cli", [_toolkit("shell", run_shell_command), adapt_toolkit(gated_toolkit)])
    catalog.store = create_agent_state_storage("helper", tmp_path / "cli")

    async def authorize(key, arguments) -> None:
        return None

    owner = live_turn_tools_for_test(
        CliTurnOwner(catalog.runtime_context.execution_identity, "turn-approval", "run"),
        catalog=catalog,
        authorize=authorize,
        output_file_policy=ToolOutputFilePolicy(tmp_path / "workspace"),
    )
    owner.shell_env = _SHELL_ENV
    waits = runner.approvals._cli_approval_waits
    identity = runner.deps.tool_runtime.build_execution_identity(
        target=request.response_envelope.target,
        user_id=request.user_id,
    )

    try:
        with (
            patch("mindroom.approval.response.resolve_tool_approval_approver", return_value="@user:localhost"),
            patch("mindroom.approval.response.evaluate_tool_approval", new=AsyncMock(return_value=(False, 60.0))),
            patch.object(runner.approvals, "_publish_generation", new=publish_gen),
        ):
            async with (
                _in_reply_span(runner, request),
                waits.scope(
                    source_event_ids=("$source",),
                    progress=TurnProgress(),
                    target=request.response_envelope.target,
                    publish=partial(
                        runner.approvals.suspend_for_approval,
                        request=request,
                        target=request.response_envelope.target,
                        execution_identity=identity,
                        history_scope=runner.deps.state_writer.history_scope(),
                        entity_kind="agent",
                        show_tool_calls=True,
                    ),
                    authorize=AsyncMock(return_value=True),
                    settle_terminal=True,
                ) as pause,
            ):
                from dataclasses import replace  # noqa: PLC0415

                catalog.runtime_context = replace(
                    catalog.runtime_context,
                    agent_name="general",
                    cli_approval_handler=pause,
                )
                async with response_cli_lifetime():
                    await capture_bash_batch(owner, ("outer-1", {"command": "outer"}))
                    bash_task = asyncio.create_task(
                        owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "outer"}, "outer-1"),
                    )
                    async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                        await outer_entered.wait()
                        first_id = uuid4()
                        await owner.operation(
                            window="outer-1",
                            operation=ToolCallOperation(
                                operation="tools.call",
                                call_id=first_id,
                                toolkit="gated",
                                function="gated",
                                arguments={"label": "first"},
                            ),
                        )
                        await first_paused.wait()

                        cancelled = await owner.cancel_call(str(first_id))
                        assert cancelled["status"] == "cancelled"

                        # Sibling call needing approval in the same response must now succeed!
                        second_id = uuid4()
                        await owner.operation(
                            window="outer-1",
                            operation=ToolCallOperation(
                                operation="tools.call",
                                call_id=second_id,
                                toolkit="gated",
                                function="gated",
                                arguments={"label": "second"},
                            ),
                        )
                        await second_paused.wait()
                        done = await owner.get_call(str(second_id), wait=5.0)
                        assert done["status"] == "completed", done
                        assert done["outcome"] == "approved:second"

                        release_outer.set()
                        await bash_task
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_progress_revision_delivers_changes_between_gets_and_on_terminal_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server wakes immediately when `after_progress` is behind; CLI prints unseen progress on terminal receipts too."""
    outer_entered = asyncio.Event()
    release_outer = asyncio.Event()
    child_started = asyncio.Event()
    finish_child = asyncio.Event()

    async def run_shell_command(args: str) -> str:
        if args == "outer":
            outer_entered.set()
            await release_outer.wait()
            return "outer done"
        sink = _make_progress_sink(
            None,
            reply_timing=None,
            call_progress=runtime_context.get_call_progress_sink(),
            worker_name="fan3",
            tool_name="shell",
            function_name="run_shell_command",
        )
        sink(WorkerReadyProgress(phase="cold_start", worker_key="w", backend_name="kubernetes", elapsed_seconds=1.6))
        child_started.set()
        await finish_child.wait()
        return "[worker: fan3]\nok"

    owner, token = await _make_owner(tmp_path, run_shell_command)
    app = _make_app(owner)
    try:
        async with response_cli_lifetime():
            await capture_bash_batch(owner, ("outer-1", {"command": "outer"}))
            bash_task = asyncio.create_task(
                owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "outer"}, "outer-1"),
            )
            async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                await outer_entered.wait()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
                    call_id = str(uuid4())
                    await http.post(
                        "/api/agent-cli/operations",
                        headers={"Authorization": f"Bearer {token}", AGENT_CLI_WINDOW_HEADER: "outer-1"},
                        json={
                            "operation": "tools.call",
                            "call_id": call_id,
                            "toolkit": "shell",
                            "function": "run_shell_command",
                            "arguments": {"args": "child"},
                        },
                    )
                    await child_started.wait()
                    await asyncio.sleep(0.02)

                    # Progress happened BEFORE this GET arrived: passing after_progress=0 must return immediately!
                    started = time.monotonic()
                    resp = await http.get(
                        f"/api/agent-cli/calls/{call_id}?wait=3&after_progress=0",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert time.monotonic() - started < 1.0
                    body = resp.json()
                    assert body["progress_revision"] >= 1
                    assert "fan3" in body["progress"]

                    finish_child.set()
                    done = await http.get(
                        f"/api/agent-cli/calls/{call_id}?wait=5&after_progress={body['progress_revision']}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert done.json()["status"] == "completed"
                    assert done.json()["progress"] == body["progress"]
                release_outer.set()
                await bash_task
    finally:
        await owner.close()

    # CLI also prints unseen progress carried on a terminal receipt.
    class TerminalProgressClient:
        def operation(self, payload):
            del payload
            return {
                "status": "completed",
                "progress": "worker fan3 ready (3s)",
                "progress_revision": 2,
                "outcome": "[worker: fan3]\n" + format_shell_completion("hi", None, return_code=0),
            }

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", TerminalProgressClient)
    assert main(["workers", "run", "fan3", "--", "echo", "hi"]) == 0
    output = capsys.readouterr()
    assert output.out == "hi\n"
    assert "mindroom-agent: worker fan3 ready (3s)\n" in output.err


def test_cli_backs_off_against_server_that_ignores_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older server that returns immediately with unchanged status does not cause a busy-loop."""
    now = 0.0
    reads = 0

    class Clock:
        def sleep(self, seconds: float) -> None:
            nonlocal now
            now += seconds

        def monotonic(self) -> float:
            return now

    class EarlyServerClient:
        def operation(self, payload):
            del payload
            return {"status": "running"}

        def receipt(
            self,
            call_id,
            *,
            timeout: float = 30,
            wait: float | None = None,
            after_progress: int | None = None,
        ):
            nonlocal reads
            del call_id, timeout, wait, after_progress
            reads += 1
            if now >= 2.0 or reads > 50:
                return {"status": "completed", "outcome": "[worker: w]\nok"}
            return {"status": "running"}

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", EarlyServerClient)
    monkeypatch.setattr("mindroom.agent_cli.main.time", Clock())

    assert main(["workers", "run", "w", "--", "true"]) == 0
    assert reads <= 10


def test_cli_signal_after_command_finished_reports_finished_and_returns_command_code(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When cancel returns an already-completed receipt, report it finished and exit with its real code."""
    installed: dict[int, object] = {}
    submitted: list[str] = []

    def fake_signal(signum, handler):
        previous = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        return previous

    class Client:
        def operation(self, payload):
            submitted.append(payload["call_id"])
            return {"status": "running", "call_id": payload["call_id"]}

        def receipt(
            self,
            call_id,
            *,
            timeout: float = 30,
            wait: float | None = None,
            after_progress: int | None = None,
        ):
            del call_id, timeout, wait, after_progress
            handler = installed[signal.SIGTERM]
            assert callable(handler)
            handler(int(signal.SIGTERM), None)
            pytest.fail("signal handler should interrupt wait")

        def cancel(self, call_id, *, timeout: float = 8.0):
            del timeout
            return {
                "status": "completed",
                "call_id": call_id,
                "outcome": "[worker: w]\n" + format_shell_completion("done", None, return_code=7),
            }

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr(signal, "signal", fake_signal)

    assert main(["workers", "run", "w", "--", "sh", "-c", "exit 7"]) == 7
    output = capsys.readouterr()
    assert "already finished" in output.err
    assert f"(call {submitted[0]})" in output.err
    assert "cancelled by" not in output.err


def test_cli_flag_before_workers_usage_error_writes_only_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A leading flag before `workers` still suppresses JSON on stdout for usage errors."""
    assert main(["--unknown", "workers", "run", "w", "--", "true"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "mindroom-agent:" in output.err


# ---------------------------------------------------------------------------
# Round 2 review fixes (tests-first against 025d962f7)
# ---------------------------------------------------------------------------


def test_r2_receipt_has_no_dispatched_field_and_only_typed_refusal_exits_125(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receipts expose `error_kind` (not `dispatched`); only typed `named_worker_refusal` exits 125 after submit."""
    from mindroom.agent_cli.protocol import ToolCallReceipt  # noqa: PLC0415

    receipt = ToolCallReceipt(call_id=uuid4(), toolkit="shell", function="run_shell_command", status="queued")
    assert "dispatched" not in receipt.model_dump(mode="json")

    for payload, expected_code in (
        ({"status": "failed", "error_kind": "named_worker_refusal", "outcome": "Cannot start worker 'w': limit"}, 125),
        ({"status": "failed", "outcome": "Unexpected tool error"}, 255),
        ({"status": "cancelled", "outcome": "Call cancelled before dispatch"}, 255),
        ({"status": "cancelled", "outcome": "Call interrupted after dispatch; effects may have occurred"}, 255),
    ):
        submitted: list[str] = []

        class Client:
            def operation(self, op, _payload=payload, _submitted=submitted):
                _submitted.append(op["call_id"])
                return _payload

        monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
        assert main(["workers", "run", "w", "--", "true"]) == expected_code
        output = capsys.readouterr()
        assert output.out == ""
        if expected_code == 255:
            assert f"(call {submitted[0]})" in output.err


def test_r2_cancel_messages_for_submitted_refused_and_completed(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel says 'may still be running' unless settled as typed refusal (never ran) or completed (no extra line)."""
    for cancel_receipt, expected_code, expected_out, expected_err_sub, forbidden_err_sub in (
        (
            {"status": "cancelled", "outcome": "Call interrupted after dispatch; effects may have occurred"},
            143,
            "",
            "cancelled by SIGTERM; the command may still be running in worker fan3",
            "cancel requested",
        ),
        (
            {"status": "failed", "error_kind": "named_worker_refusal", "outcome": "limit reached"},
            143,
            "",
            "never ran",
            "may still be running",
        ),
        (
            {
                "status": "completed",
                "outcome": "[worker: fan3]\n" + format_shell_completion("finished ok", None, return_code=7),
            },
            7,
            "finished ok\n",
            "",
            "SIGTERM",
        ),
    ):
        installed: dict[int, object] = {}
        submitted: list[str] = []

        def fake_signal(signum, handler, _installed=installed):
            prev = _installed.get(signum, signal.SIG_DFL)
            _installed[signum] = handler
            return prev

        class Client:
            def operation(self, op, _submitted=submitted):
                _submitted.append(op["call_id"])
                return {"status": "running", "call_id": op["call_id"]}

            def receipt(
                self,
                call_id,
                *,
                timeout: float = 30,
                wait: float | None = None,
                after_progress: int | None = None,
                _installed=installed,
            ):
                del call_id, timeout, wait, after_progress
                handler = _installed[signal.SIGTERM]
                assert callable(handler)
                handler(int(signal.SIGTERM), None)
                pytest.fail("signal handler should interrupt wait")

            def cancel(self, call_id, *, timeout: float = 8.0, _cancel_receipt=cancel_receipt):
                del call_id, timeout
                return _cancel_receipt

        monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
        monkeypatch.setattr(signal, "signal", fake_signal)
        assert main(["workers", "run", "fan3", "--", "true"]) == expected_code
        output = capsys.readouterr()
        assert output.out == expected_out
        if expected_err_sub:
            assert expected_err_sub in output.err
            assert f"(call {submitted[0]})" in output.err
        else:
            assert output.err == ""
        assert forbidden_err_sub not in output.err


def test_r2_signal_during_final_flush_prints_output_once_and_returns_command_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SIGTERM arriving during the final output flush never re-fetches, prints once, and returns the command code."""
    installed: dict[int, object] = {}
    cancel_calls: list[str] = []
    written: list[str] = []

    def fake_signal(signum, handler):
        prev = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        return prev

    class SignalOnFlushStdout:
        def __init__(self) -> None:
            self._signalled = False

        def write(self, data: str) -> int:
            written.append(data)
            return len(data)

        def flush(self) -> None:
            if not self._signalled:
                self._signalled = True
                handler = installed[signal.SIGTERM]
                assert callable(handler)
                handler(int(signal.SIGTERM), None)

    class Client:
        def operation(self, payload):
            return {
                "status": "completed",
                "call_id": payload["call_id"],
                "outcome": "[worker: w]\n" + format_shell_completion("final line", None, return_code=9),
            }

        def cancel(self, call_id, *, timeout: float = 8.0):
            del timeout
            cancel_calls.append(call_id)
            return {"status": "completed"}

    stderr_chunks: list[str] = []

    class CaptureStderr:
        def write(self, data: str) -> int:
            stderr_chunks.append(data)
            return len(data)

        def flush(self) -> None:
            pass

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr("mindroom.agent_cli.main.sys.stdout", SignalOnFlushStdout())
    monkeypatch.setattr("mindroom.agent_cli.main.sys.stderr", CaptureStderr())

    assert main(["workers", "run", "w", "--", "sh", "-c", "echo final line; exit 9"]) == 9
    assert "".join(written) == "final line\n"
    assert "".join(stderr_chunks) == ""
    assert cancel_calls == []


@pytest.mark.asyncio
async def test_r2_cancel_mid_approval_publication_settles_and_unblocks_sibling(tmp_path: Path) -> None:
    """Cancelling a CLI call while its approval is mid-publication never corrupts the continuation and unblocks siblings."""
    from dataclasses import replace  # noqa: PLC0415
    from functools import partial  # noqa: PLC0415
    from unittest.mock import AsyncMock, patch  # noqa: PLC0415

    from agno.tools.toolkit import Toolkit  # noqa: PLC0415

    from mindroom.event_journal import CallKey  # noqa: PLC0415
    from mindroom.final_delivery import TurnProgress  # noqa: PLC0415
    from mindroom.tool_system.agno_toolkits import adapt_toolkit  # noqa: PLC0415
    from tests.conftest import unwrap_extracted_collaborator  # noqa: PLC0415
    from tests.test_response_runner_focused import (  # noqa: PLC0415
        _admit_approval_source,
        _bot,
        _in_reply_span,
        _plain_request,
        _target,
    )

    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    first_pub_entered = asyncio.Event()
    release_first_pub = asyncio.Event()
    second_paused = asyncio.Event()
    published_ids: list[str] = []

    async def publish_gen(continuation, plan, **kwargs: object):
        del plan, kwargs
        published_ids.append(continuation.approval_id)
        if len(published_ids) == 1:
            first_pub_entered.set()
            # Hold mid-publication while cancel arrives; must not be interrupted by task.cancel()!
            await release_first_pub.wait()
            await store.approvals.activate_approval_continuation(
                continuation.approval_id,
                expected_generation=continuation.generation,
            )
        else:
            await store.approvals.activate_approval_continuation(
                continuation.approval_id,
                expected_generation=continuation.generation,
            )
            for call in continuation.calls:
                await store.approvals.resolve_approval_call(
                    CallKey(continuation.approval_id, continuation.generation, call.tool_call_id),
                    requested_status="approved",
                    reason=None,
                )
            runner.cli_waiters.wake(("$source",))
            second_paused.set()

    outer_entered = asyncio.Event()
    release_outer = asyncio.Event()

    async def run_shell_command(args: str) -> str:
        outer_entered.set()
        await release_outer.wait()
        return "outer done"

    async def gated(label: str) -> str:
        return f"approved:{label}"

    gated_toolkit = Toolkit(name="gated", tools=[gated], requires_confirmation_tools=["gated"])
    catalog = await _catalog(tmp_path / "cli", [_toolkit("shell", run_shell_command), adapt_toolkit(gated_toolkit)])
    catalog.store = create_agent_state_storage("helper", tmp_path / "cli")

    async def authorize(key, arguments) -> None:
        return None

    owner = live_turn_tools_for_test(
        CliTurnOwner(catalog.runtime_context.execution_identity, "turn-mid-pub", "run"),
        catalog=catalog,
        authorize=authorize,
        output_file_policy=ToolOutputFilePolicy(tmp_path / "workspace"),
    )
    owner.shell_env = _SHELL_ENV
    waits = runner.approvals._cli_approval_waits
    identity = runner.deps.tool_runtime.build_execution_identity(
        target=request.response_envelope.target,
        user_id=request.user_id,
    )

    try:
        with (
            patch("mindroom.approval.response.resolve_tool_approval_approver", return_value="@user:localhost"),
            patch("mindroom.approval.response.evaluate_tool_approval", new=AsyncMock(return_value=(False, 60.0))),
            patch.object(runner.approvals, "_publish_generation", new=publish_gen),
        ):
            async with (
                _in_reply_span(runner, request),
                waits.scope(
                    source_event_ids=("$source",),
                    progress=TurnProgress(),
                    target=request.response_envelope.target,
                    publish=partial(
                        runner.approvals.suspend_for_approval,
                        request=request,
                        target=request.response_envelope.target,
                        execution_identity=identity,
                        history_scope=runner.deps.state_writer.history_scope(),
                        entity_kind="agent",
                        show_tool_calls=True,
                    ),
                    authorize=AsyncMock(return_value=True),
                    settle_terminal=True,
                ) as pause,
            ):
                catalog.runtime_context = replace(
                    catalog.runtime_context,
                    agent_name="general",
                    cli_approval_handler=pause,
                )
                async with response_cli_lifetime():
                    await capture_bash_batch(owner, ("outer-1", {"command": "outer"}))
                    bash_task = asyncio.create_task(
                        owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "outer"}, "outer-1"),
                    )
                    async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                        await outer_entered.wait()
                        first_id = uuid4()
                        await owner.operation(
                            window="outer-1",
                            operation=ToolCallOperation(
                                operation="tools.call",
                                call_id=first_id,
                                toolkit="gated",
                                function="gated",
                                arguments={"label": "first"},
                            ),
                        )
                        await first_pub_entered.wait()

                        # Request cancel WHILE publication is still in flight.
                        cancel_task = asyncio.create_task(owner.cancel_call(str(first_id)))
                        await asyncio.sleep(0.02)
                        release_first_pub.set()
                        cancelled = await cancel_task
                        assert cancelled["status"] == "cancelled"

                        # Continuation must be cleanly claimed (not failing), so a sibling approval succeeds.
                        second_id = uuid4()
                        await owner.operation(
                            window="outer-1",
                            operation=ToolCallOperation(
                                operation="tools.call",
                                call_id=second_id,
                                toolkit="gated",
                                function="gated",
                                arguments={"label": "second"},
                            ),
                        )
                        await second_paused.wait()
                        done = await owner.get_call(str(second_id), wait=5.0)
                        assert done["status"] == "completed", done
                        assert done["outcome"] == "approved:second"

                        release_outer.set()
                        await bash_task
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_r2_window_cancel_during_approval_keeps_main_behavior_without_withdrawing(tmp_path: Path) -> None:
    """A window cancel during an approval wait must not trigger route-cancel withdrawal."""
    from dataclasses import replace  # noqa: PLC0415
    from functools import partial  # noqa: PLC0415
    from unittest.mock import AsyncMock, patch  # noqa: PLC0415

    from agno.tools.toolkit import Toolkit  # noqa: PLC0415

    from mindroom.final_delivery import TurnProgress  # noqa: PLC0415
    from mindroom.tool_system.agno_toolkits import adapt_toolkit  # noqa: PLC0415
    from tests.conftest import unwrap_extracted_collaborator  # noqa: PLC0415
    from tests.test_response_runner_focused import (  # noqa: PLC0415
        _admit_approval_source,
        _bot,
        _in_reply_span,
        _plain_request,
        _target,
    )

    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    paused_event = asyncio.Event()

    async def publish_gen(continuation, plan, **kwargs: object):
        del plan, kwargs
        await store.approvals.activate_approval_continuation(
            continuation.approval_id,
            expected_generation=continuation.generation,
        )
        paused_event.set()

    outer_entered = asyncio.Event()
    release_outer = asyncio.Event()

    async def run_shell_command(args: str) -> str:
        outer_entered.set()
        await release_outer.wait()
        return "outer done"

    async def gated(label: str) -> str:
        return f"approved:{label}"

    gated_toolkit = Toolkit(name="gated", tools=[gated], requires_confirmation_tools=["gated"])
    catalog = await _catalog(tmp_path / "cli", [_toolkit("shell", run_shell_command), adapt_toolkit(gated_toolkit)])
    catalog.store = create_agent_state_storage("helper", tmp_path / "cli")

    async def authorize(key, arguments) -> None:
        return None

    owner = live_turn_tools_for_test(
        CliTurnOwner(catalog.runtime_context.execution_identity, "turn-win-cancel", "run"),
        catalog=catalog,
        authorize=authorize,
        output_file_policy=ToolOutputFilePolicy(tmp_path / "workspace"),
    )
    owner.shell_env = _SHELL_ENV
    waits = runner.approvals._cli_approval_waits
    identity = runner.deps.tool_runtime.build_execution_identity(
        target=request.response_envelope.target,
        user_id=request.user_id,
    )

    try:
        with (
            patch("mindroom.approval.response.resolve_tool_approval_approver", return_value="@user:localhost"),
            patch("mindroom.approval.response.evaluate_tool_approval", new=AsyncMock(return_value=(False, 60.0))),
            patch.object(runner.approvals, "_publish_generation", new=publish_gen),
        ):
            async with (
                _in_reply_span(runner, request),
                waits.scope(
                    source_event_ids=("$source",),
                    progress=TurnProgress(),
                    target=request.response_envelope.target,
                    publish=partial(
                        runner.approvals.suspend_for_approval,
                        request=request,
                        target=request.response_envelope.target,
                        execution_identity=identity,
                        history_scope=runner.deps.state_writer.history_scope(),
                        entity_kind="agent",
                        show_tool_calls=True,
                    ),
                    authorize=AsyncMock(return_value=True),
                    settle_terminal=True,
                ) as pause,
            ):
                catalog.runtime_context = replace(
                    catalog.runtime_context,
                    agent_name="general",
                    cli_approval_handler=pause,
                )
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
                                toolkit="gated",
                                function="gated",
                                arguments={"label": "win"},
                            ),
                        )
                        await paused_event.wait()

                        # Window cancel (not route cancel): continuation stays `waiting` until scope exits.
                        owner._cancel_window("outer-1")
                        await asyncio.sleep(0.05)
                        current = await store.approvals.approval_continuation_for_source("$source")
                        assert current is not None
                        assert current.state == "waiting"

                        release_outer.set()
                        await bash_task
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_r2_long_poll_waiter_cap_and_disconnect_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent `?wait` waiters are capped per owner and released when the client disconnects."""
    monkeypatch.setattr(turn, "_MAX_LONG_POLL_WAITERS", 2)
    outer_entered = asyncio.Event()
    release_outer = asyncio.Event()
    child_started = asyncio.Event()
    release_child = asyncio.Event()

    async def run_shell_command(args: str) -> str:
        if args == "outer":
            outer_entered.set()
            await release_outer.wait()
            return "outer done"
        child_started.set()
        await release_child.wait()
        return "child done"

    owner, _token = await _make_owner(tmp_path, run_shell_command)
    try:
        async with response_cli_lifetime():
            await capture_bash_batch(owner, ("outer-1", {"command": "outer"}))
            bash_task = asyncio.create_task(
                owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "outer"}, "outer-1"),
            )
            async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                await outer_entered.wait()
                call_id = str(uuid4())
                await owner.operation(
                    window="outer-1",
                    operation=ToolCallOperation(
                        operation="tools.call",
                        call_id=call_id,
                        toolkit="shell",
                        function="run_shell_command",
                        arguments={"args": "child"},
                    ),
                )
                await child_started.wait()

                disconnected = False

                async def is_disconnected() -> bool:
                    return disconnected

                waiter_1 = asyncio.create_task(owner.get_call(call_id, wait=25.0, is_disconnected=is_disconnected))
                waiter_2 = asyncio.create_task(owner.get_call(call_id, wait=25.0))
                await asyncio.sleep(0.02)
                assert not waiter_1.done()
                assert not waiter_2.done()

                # Third concurrent waiter exceeds cap (2) and returns immediately without waiting.
                started = time.monotonic()
                overflow = await owner.get_call(call_id, wait=25.0)
                assert time.monotonic() - started < 0.5
                assert overflow["status"] == "running"

                # Disconnecting client 1 releases its wait promptly.
                disconnected = True
                started = time.monotonic()
                res_1 = await waiter_1
                assert time.monotonic() - started < 1.0
                assert res_1["status"] == "running"

                release_child.set()
                res_2 = await waiter_2
                assert res_2["status"] == "completed"

                release_outer.set()
                await bash_task
    finally:
        await owner.close()
