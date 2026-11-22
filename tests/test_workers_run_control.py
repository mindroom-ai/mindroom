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

        def receipt(self, call_id, *, timeout: float = 30, wait: float | None = None):
            del call_id, timeout, wait
            handler = installed[sig]
            assert callable(handler)
            handler(int(sig), None)
            pytest.fail("signal handler should interrupt wait")

        def cancel(self, call_id, *, timeout: float = 5.0):
            cancelled.append((call_id, timeout))
            return {"status": "cancelled", "call_id": call_id}

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr(signal, "signal", fake_signal)

    exit_code = main(["workers", "run", "fan3", "--", "sleep", "300"])
    assert exit_code == expected_code
    [call_id] = submitted
    assert len(cancelled) == 1
    assert cancelled[0][0] == call_id
    assert cancelled[0][1] <= 5.0
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

        def receipt(self, call_id, *, timeout: float = 30, wait: float | None = None):
            del call_id, timeout, wait
            handler = installed[signal.SIGTERM]
            assert callable(handler)
            handler(int(signal.SIGTERM), None)
            pytest.fail("signal handler should interrupt wait")

        def cancel(self, call_id, *, timeout: float = 5.0):
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

        def receipt(self, call_id, *, timeout: float = 30, wait: float | None = None):
            del call_id, timeout, wait
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
            {"status": "running", "progress": "starting worker fan3..."},
            {"status": "running", "progress": "starting worker fan3..."},
            {"status": "running", "progress": "starting worker fan3... (5s)"},
            {
                "status": "completed",
                "outcome": "[worker: fan3, started]\n" + format_shell_completion("hello", None, return_code=0),
            },
        ],
    )

    class Client:
        def operation(self, payload):
            return {"status": "queued", "call_id": payload["call_id"]}

        def receipt(self, call_id, *, timeout: float = 30, wait: float | None = None):
            del call_id, timeout
            waits.append(wait)
            return next(receipts)

    def no_sleep(_seconds: float) -> None:
        pytest.fail("healthy long-poll must not sleep")

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr("mindroom.agent_cli.main.time.sleep", no_sleep)

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
