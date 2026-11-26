"""Tests for `workers run` control plane: cancel, long-poll, progress, stderr, and exit codes.

The CLI reads one stream per command (`tests/test_workers_run_stream.py` runs it end to end); the long-poll route
stays for other readers of a receipt.
"""

# ruff: noqa: ANN001, ANN202, ARG001, PLR0915
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import subprocess
import sys
import termios
import threading
import time
import urllib.request
from contextlib import ExitStack, contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO, TextIOWrapper
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from mindroom.agent_cli import turn
from mindroom.agent_cli.client import AgentCliClient, AgentCliUnavailableError
from mindroom.agent_cli.lifetime import response_cli_lifetime
from mindroom.agent_cli.main import main
from mindroom.agent_cli.protocol import ToolCallOperation
from mindroom.agent_cli.session import CliAuthenticationError, CliTurnOwner, TurnToolRegistry
from mindroom.agent_cli.shell_contract import AGENT_CLI_WINDOW_HEADER, AgentCliShellEnv
from mindroom.api.agent_cli import bind_agent_cli_registry, router
from mindroom.session_storage.agent_storage import create_agent_state_storage
from mindroom.shell.output_capture import format_shell_completion
from mindroom.tool_system import runtime_context
from mindroom.tool_system.output_files import ToolOutputFilePolicy
from mindroom.tool_system.sandbox_proxy import _make_progress_sink
from mindroom.tool_system.tool_access import ToolKey
from mindroom.workers.models import WorkerReadyProgress
from tests.conftest import SETTLE_TIMEOUT_SECONDS, live_turn_tools_for_test
from tests.minimal_agent_fixtures import capture_bash_batch
from tests.test_agent_tool_calls import _catalog, _function, _toolkit
from tests.workers_run_frames import command_frames

if TYPE_CHECKING:
    from collections.abc import Iterator
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


class _NoStdin:
    """What a fake client answers the CLI's forwarding of its stdin, which is empty under pytest."""

    def stdin(self, call_id, seq, data, *, eof):
        del call_id
        assert (seq, data, eof) == (0, b"", True)
        return {"open": False}


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
async def test_a_cancel_after_the_reader_left_still_waits_for_the_call_to_end(tmp_path: Path) -> None:
    """A streamed call's reader that leaves cancels it; the CLI's cancel that follows answers once the call ended."""
    outer_entered = asyncio.Event()
    release_outer = asyncio.Event()
    long_started = asyncio.Event()

    async def run_shell_command(args: str) -> str:
        if args == "outer":
            outer_entered.set()
            await release_outer.wait()
            return "outer done"
        long_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # As a worker's stop does, ending takes a moment.
            await asyncio.sleep(0.2)
            raise
        return args

    owner, token = await _make_owner(tmp_path, run_shell_command)
    headers = {"Authorization": f"Bearer {token}", AGENT_CLI_WINDOW_HEADER: "outer-1"}
    try:
        async with response_cli_lifetime():
            await capture_bash_batch(owner, ("outer-1", {"command": "outer"}))
            bash_task = asyncio.create_task(
                owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "outer"}, "outer-1"),
            )
            async with (
                asyncio.timeout(SETTLE_TIMEOUT_SECONDS),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=_make_app(owner)), base_url="http://test") as http,
            ):
                await outer_entered.wait()
                call_id = str(uuid4())
                operation = {
                    "operation": "tools.call",
                    "call_id": call_id,
                    "toolkit": "shell",
                    "function": "run_shell_command",
                    "arguments": {"args": "long"},
                    "stream": True,
                }
                assert (
                    await http.post("/api/agent-cli/operations", headers=headers, json=operation)
                ).status_code == 200
                await long_started.wait()
                await owner.stream_call(call_id).aclose()
                first = await http.post(f"/api/agent-cli/calls/{call_id}/cancel", headers=headers)
                assert first.json()["status"] == "cancelled"
            release_outer.set()
            await bash_task
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_a_stream_that_was_asked_for_but_never_read_does_not_keep_its_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a relay that started counts as the call's reader; until then the call is cancelled as one nobody reads."""
    monkeypatch.setattr("mindroom.agent_cli.turn._STREAM_ATTACH_SECONDS", 0.3)
    outer_entered = asyncio.Event()
    release_outer = asyncio.Event()

    async def run_shell_command(args: str) -> str:
        if args == "outer":
            outer_entered.set()
            await release_outer.wait()
            return "outer done"
        await asyncio.Event().wait()
        return args

    owner, token = await _make_owner(tmp_path, run_shell_command)
    headers = {"Authorization": f"Bearer {token}", AGENT_CLI_WINDOW_HEADER: "outer-1"}
    try:
        async with response_cli_lifetime():
            await capture_bash_batch(owner, ("outer-1", {"command": "outer"}))
            bash_task = asyncio.create_task(
                owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "outer"}, "outer-1"),
            )
            async with (
                asyncio.timeout(SETTLE_TIMEOUT_SECONDS),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=_make_app(owner)), base_url="http://test") as http,
            ):
                await outer_entered.wait()
                call_id = str(uuid4())
                operation = {
                    "operation": "tools.call",
                    "call_id": call_id,
                    "toolkit": "shell",
                    "function": "run_shell_command",
                    "arguments": {"args": "long"},
                    "stream": True,
                }
                assert (
                    await http.post("/api/agent-cli/operations", headers=headers, json=operation)
                ).status_code == 200
                unread = owner.stream_call(call_id)
                receipt = (await http.get(f"/api/agent-cli/calls/{call_id}?wait=10", headers=headers)).json()
                assert receipt["status"] == "cancelled"
                await unread.aclose()
            release_outer.set()
            await bash_task
    finally:
        await owner.close()


def test_a_frame_for_a_loop_that_stopped_is_dropped() -> None:
    """The worker call's thread does not wait for ever to hand a frame to an event loop that no longer runs."""
    loop = asyncio.new_event_loop()
    serving = threading.Thread(target=loop.run_forever, daemon=True)
    serving.start()
    stream = runtime_context.CallOutputStream(loop, queued_frames=1)
    stream.put({"t": "out"})
    blocked = threading.Thread(target=stream.put, args=({"t": "out"},), daemon=True)
    blocked.start()
    blocked.join(0.2)
    assert blocked.is_alive()
    loop.call_soon_threadsafe(loop.stop)
    serving.join(SETTLE_TIMEOUT_SECONDS)
    blocked.join(5)
    assert not blocked.is_alive()
    loop.close()


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

        def stream(self, call_id):
            del call_id
            handler = installed[sig]
            assert callable(handler)
            handler(int(sig), None)
            pytest.fail("signal handler should interrupt wait")

        def cancel(self, call_id, *, timeout: float = 8.0):
            cancelled.append((call_id, timeout))
            return {"status": "cancelled", "call_id": call_id}

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
    assert (
        output.err
        == f"mindroom-agent: cancelled by {sig_name}; the command may still be running in worker fan3 (call {call_id})\n"
    )


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

        def stream(self, call_id):
            del call_id
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


def test_cli_workers_run_reads_one_stream_without_sleeping_and_prints_progress_and_started_to_stderr(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthy run is one read of the call's stream, with zero sleep, and its progress and started lines on stderr."""
    streams: list[str] = []
    frames = [
        {"t": "progress", "text": "starting worker fan3..."},
        {"t": "keepalive"},
        {"t": "progress", "text": "starting worker fan3... (5s)"},
        *command_frames(b"hello\n", header="[worker: fan3, started]"),
    ]

    class Client(_NoStdin):
        def operation(self, payload):
            return {"status": "queued", "call_id": payload["call_id"]}

        def stream(self, call_id):
            streams.append(call_id)
            return iter(frames)

    class Clock:
        def sleep(self, _seconds: float) -> None:
            pytest.fail("a healthy stream must not sleep")

        def monotonic(self) -> float:
            return 0.0

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr("mindroom.agent_cli.main.time", Clock())

    assert main(["workers", "run", "fan3", "--", "echo", "hello"]) == 0
    assert len(streams) == 1
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
    """Timeout stop message goes to stderr, after the command's own, while its partial output stays on stdout."""
    frames = command_frames(b"partial\n", b"warn\n", code=-9, timed_out=True)

    class Client(_NoStdin):
        def operation(self, payload):
            return {"status": "queued", "call_id": payload["call_id"]}

        def stream(self, _call_id):
            return iter(frames)

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)

    assert main(["workers", "run", "w", "--timeout", "5", "--", "sleep", "30"]) == 124
    output = capsys.readouterr()
    assert output.out == "partial\n"
    assert output.err == "warn\nmindroom-agent: stopped after 5s\n"


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


@pytest.mark.parametrize(
    ("receipt", "expected_code"),
    [
        (
            {
                "status": "failed",
                "error_kind": "named_worker_refusal",
                "outcome": "Cannot start worker 'w': limit reached",
            },
            125,
        ),
        ({"status": "cancelled", "outcome": "Call cancelled before dispatch"}, 255),
        (
            {
                "status": "cancelled",
                "outcome": "Call interrupted after dispatch; effects may have occurred",
            },
            255,
        ),
        (
            {
                "status": "failed",
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

        def stream(self, _call_id):
            return iter([receipt])

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
    """A completed call whose result could not be saved and whose stream had no exit ends 255 with call id."""
    from mindroom.agent_cli.projection import project_cli_result  # noqa: PLC0415

    submitted: list[str] = []
    result = format_shell_completion("finished", None, return_code=0)
    policy = ToolOutputFilePolicy(tmp_path, max_bytes=1, auto_save_threshold_bytes=1)
    outcome = project_cli_result(f"[worker: w]\n{result}", policy, "run_shell_command")

    class Client:
        def operation(self, payload):
            submitted.append(payload["call_id"])
            return {"status": "queued"}

        def stream(self, _call_id):
            return iter([{"status": "completed", "outcome": outcome}])

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)

    assert main(["workers", "run", "w", "--", "true"]) == 255
    output = capsys.readouterr()
    assert output.out == ""
    assert f"(call {submitted[0]})" in output.err


@pytest.mark.asyncio
async def test_concurrent_cancels_invoke_task_cancel_only_once(tmp_path: Path) -> None:
    """Only the first cancel request cancels the task and waits for it; a duplicate answers at once."""
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
                # A second cancel arrives while the first one's cleanup runs: it holds no waiter.
                res_2 = await asyncio.wait_for(owner.cancel_call(str(call_id)), timeout=1.0)
                assert res_2["status"] == "running"
                release_cleanup.set()

                res_1 = await cancel_1
                assert res_1["status"] == "cancelled"
                assert not cleanup_interrupted

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

    # The CLI prints the progress its stream carries, before the command's output.
    class ProgressClient(_NoStdin):
        def operation(self, payload):
            del payload
            return {"status": "queued"}

        def stream(self, _call_id):
            ready = {"t": "progress", "text": "worker fan3 ready (3s)"}
            return iter([ready, *command_frames(b"hi\n", header="[worker: fan3]")])

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", ProgressClient)
    assert main(["workers", "run", "fan3", "--", "echo", "hi"]) == 0
    output = capsys.readouterr()
    assert output.out == "hi\n"
    assert output.err == "mindroom-agent: worker fan3 ready (3s)\n"


def test_cli_signal_after_command_finished_reports_finished_and_returns_command_code(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal after the command's exit arrived changes nothing: its output is written, and its code is the CLI's."""
    installed: dict[int, object] = {}
    submitted: list[str] = []

    def fake_signal(signum, handler):
        previous = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        return previous

    class Client(_NoStdin):
        def operation(self, payload):
            submitted.append(payload["call_id"])
            return {"status": "running", "call_id": payload["call_id"]}

        def stream(self, call_id):
            del call_id
            # Everything but the receipt: the signal lands after the command's exit.
            yield from command_frames(b"done\n", code=7)[:-1]
            handler = installed[signal.SIGTERM]
            assert callable(handler)
            handler(int(signal.SIGTERM), None)
            pytest.fail("signal handler should interrupt wait")

        def cancel(self, call_id, *, timeout: float = 8.0):
            del timeout
            return {"status": "completed", "call_id": call_id, "outcome": "[worker: w]\nExit code 7"}

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr(signal, "signal", fake_signal)

    assert main(["workers", "run", "w", "--", "sh", "-c", "exit 7"]) == 7
    output = capsys.readouterr()
    assert output.out == "done\n"
    assert output.err == ""


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

            def stream(self, _call_id, _payload=payload):
                return iter([_payload])

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
    """Cancel says 'may still be running' unless the call was refused, waits for approval, or had ended."""
    for cancel_receipt, expected_code, expected_out, expected_err_sub, forbidden_err_sub in (
        (
            {"status": "waiting"},
            143,
            "",
            "cancelled by SIGTERM; the command was waiting for approval and has not run",
            "may still be running",
        ),
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
        # The command ended while the signal stopped the CLI reading: its exit never arrived, so the signal decides.
        (
            {"status": "completed", "outcome": "[worker: fan3]\nExit code 7"},
            143,
            "",
            "cancelled by SIGTERM; the command had ended, and the rest of its output was not read",
            "may still be running",
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

            def stream(self, call_id, _installed=installed):
                del call_id
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
        assert expected_err_sub in output.err
        assert f"(call {submitted[0]})" in output.err
        assert forbidden_err_sub not in output.err


@pytest.mark.asyncio
async def test_r2_long_poll_waiter_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent `?wait` waiters are capped per owner; a read over the cap answers at once."""
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

                waiter_1 = asyncio.create_task(owner.get_call(call_id, wait=25.0))
                waiter_2 = asyncio.create_task(owner.get_call(call_id, wait=25.0))
                await asyncio.sleep(0.02)
                assert not waiter_1.done()
                assert not waiter_2.done()

                # Third concurrent waiter exceeds cap (2) and returns immediately without waiting.
                started = time.monotonic()
                overflow = await owner.get_call(call_id, wait=25.0)
                assert time.monotonic() - started < 0.5
                assert overflow["status"] == "running"

                release_child.set()
                assert (await waiter_1)["status"] == "completed"
                assert (await waiter_2)["status"] == "completed"

                release_outer.set()
                await bash_task
    finally:
        await owner.close()


# ---------------------------------------------------------------------------
# Round 3: cancel is owned by the turn alone, and the CLI handles signals only while it submits and waits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_during_approval_is_deferred_and_approving_the_card_runs_nothing(tmp_path: Path) -> None:
    """A cancel during the approval handler leaves the approval alone; the approved call then ends unrun."""
    from dataclasses import replace  # noqa: PLC0415
    from functools import partial  # noqa: PLC0415
    from unittest.mock import AsyncMock, patch  # noqa: PLC0415

    from mindroom.event_journal import CallKey  # noqa: PLC0415
    from mindroom.final_delivery import TurnProgress  # noqa: PLC0415
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
    first_publishing = asyncio.Event()
    release_first_publication = asyncio.Event()
    first_published = asyncio.Event()
    published: list[object] = []
    ran: list[str] = []

    async def approve(continuation) -> None:
        for call in continuation.calls:
            await store.approvals.resolve_approval_call(
                CallKey(continuation.approval_id, continuation.generation, call.tool_call_id),
                requested_status="approved",
                reason=None,
            )
        runner.cli_waiters.wake(("$source",))

    async def publish_gen(continuation, plan, **kwargs: object):
        del plan, kwargs
        published.append(continuation)
        if len(published) == 1:
            # The cancel arrives while the first call's card is still being published.
            first_publishing.set()
            await release_first_publication.wait()
        await store.approvals.activate_approval_continuation(
            continuation.approval_id,
            expected_generation=continuation.generation,
        )
        if len(published) == 1:
            first_published.set()
        else:
            await approve(continuation)

    outer_entered = asyncio.Event()
    release_outer = asyncio.Event()

    async def run_shell_command(args: str) -> str:
        outer_entered.set()
        await release_outer.wait()
        return "outer done"

    async def gated(label: str) -> str:
        ran.append(label)
        return f"approved:{label}"

    gated_toolkit = replace(_function(gated, requires_confirmation=True), name="gated")
    catalog = await _catalog(tmp_path / "cli", [_toolkit("shell", run_shell_command), gated_toolkit])
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

    def gated_call(call_id, label: str) -> ToolCallOperation:
        return ToolCallOperation(
            operation="tools.call",
            call_id=call_id,
            toolkit="gated",
            function="gated",
            arguments={"label": label},
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
                        first_id, second_id = uuid4(), uuid4()
                        await owner.operation(window="outer-1", operation=gated_call(first_id, "first"))
                        await first_publishing.wait()

                        # The cancel answers at once and leaves the call to the approval that owns it.
                        started = time.monotonic()
                        deferred = await owner.cancel_call(str(first_id))
                        assert deferred["status"] == "waiting"
                        assert time.monotonic() - started < 1.0
                        release_first_publication.set()
                        await first_published.wait()
                        assert (await owner.cancel_call(str(first_id)))["status"] == "waiting"
                        # Known limit: the card stays, and its publication was not failed by the cancel.
                        current = await store.approvals.approval_continuation_for_source("$source")
                        assert current is not None
                        assert current.state == "waiting"

                        # A sibling that needs approval waits its turn behind the card, as on main.
                        await owner.operation(window="outer-1", operation=gated_call(second_id, "second"))

                        # Approving the leftover card runs nothing: the call ends cancelled.
                        await approve(published[0])
                        cancelled = await owner.get_call(str(first_id), wait=5.0)
                        assert cancelled["status"] == "cancelled", cancelled
                        assert cancelled["outcome"] == "Call cancelled before dispatch"

                        done = await owner.get_call(str(second_id), wait=5.0)
                        assert done["status"] == "completed", done
                        assert done["outcome"] == "approved:second"
                        assert ran == ["second"]

                        release_outer.set()
                        await bash_task
    finally:
        await owner.close()


def test_cli_signal_during_submit_is_held_until_the_submit_returns(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal that lands during the submit request cancels after it, so the cancel cannot overtake the submit."""
    installed: dict[int, object] = {}
    events: list[str] = []

    def fake_signal(signum, handler):
        previous = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        return previous

    class Client:
        def operation(self, payload):
            handler = installed[signal.SIGTERM]
            assert callable(handler)
            handler(int(signal.SIGTERM), None)
            events.append(f"submitted {payload['call_id']}")
            return {"status": "running", "call_id": payload["call_id"]}

        def stream(self, call_id):
            del call_id
            pytest.fail("a held signal cancels before the first read")

        def cancel(self, call_id, *, timeout: float = 8.0):
            del timeout
            events.append(f"cancelled {call_id}")
            return {"status": "cancelled", "call_id": call_id}

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr(signal, "signal", fake_signal)

    assert main(["workers", "run", "fan3", "--", "sleep", "300"]) == 143
    call_id = events[0].removeprefix("submitted ")
    assert events == [f"submitted {call_id}", f"cancelled {call_id}"]
    output = capsys.readouterr()
    assert output.out == ""
    assert (
        output.err
        == f"mindroom-agent: cancelled by SIGTERM; the command may still be running in worker fan3 (call {call_id})\n"
    )


def test_cli_signal_handlers_are_removed_before_its_closing_line_is_printed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command's output is written while a signal still cancels it; the CLI's closing line after that."""
    installed: dict[int, object] = {}
    handlers_at: dict[str, list[dict[int, object]]] = {"out": [], "err": []}
    cancel_calls: list[str] = []

    def fake_signal(signum, handler):
        previous = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        return previous

    def recording(name: str) -> TextIOWrapper:
        class Recording(BytesIO):
            def write(self, data) -> int:
                handlers_at[name].append(dict(installed))
                return super().write(data)

        return TextIOWrapper(Recording(), write_through=True)

    class Client(_NoStdin):
        def operation(self, payload):
            return {"status": "queued", "call_id": payload["call_id"]}

        def stream(self, _call_id):
            return iter(command_frames(b"final line\n", code=-9, timed_out=True))

        def cancel(self, call_id, *, timeout: float = 8.0):
            del timeout
            cancel_calls.append(call_id)
            return {"status": "completed"}

    stdout, stderr = recording("out"), recording("err")
    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr("mindroom.agent_cli.main.sys.stdout", stdout)
    monkeypatch.setattr("mindroom.agent_cli.main.sys.stderr", stderr)

    assert main(["workers", "run", "w", "--timeout", "5", "--", "sh", "-c", "echo final line; sleep 9"]) == 124
    assert stdout.buffer.getvalue() == b"final line\n"
    assert stderr.buffer.getvalue() == b"mindroom-agent: stopped after 5s\n"
    assert cancel_calls == []
    restored = dict.fromkeys((signal.SIGINT, signal.SIGTERM, signal.SIGHUP), signal.SIG_DFL)
    assert handlers_at["out"]
    assert all(handlers != restored for handlers in handlers_at["out"])
    assert handlers_at["err"]
    assert all(handlers == restored for handlers in handlers_at["err"])


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["expired", "revoked"])
@pytest.mark.parametrize("route", ["read", "cancel"])
async def test_a_wait_that_outlives_its_grant_answers_401_not_the_receipt(
    route: str,
    ending: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long-poll or a cancel whose bearer expired or was revoked while it waited does not return the receipt."""
    outer_entered = asyncio.Event()
    release_outer = asyncio.Event()
    child_started = asyncio.Event()
    release_child = asyncio.Event()
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def run_shell_command(args: str) -> str:
        if args == "outer":
            outer_entered.set()
            await release_outer.wait()
            return "outer done"
        child_started.set()
        try:
            await release_child.wait()
        except asyncio.CancelledError:
            cleanup_entered.set()
            await release_cleanup.wait()
            raise
        return "child done"

    def end_grant() -> None:
        if ending == "revoked":
            owner.revoke()
        else:
            now_ns = time.time_ns
            monkeypatch.setattr(time, "time_ns", lambda: now_ns() + 10**15)

    owner, token = await _make_owner(tmp_path, run_shell_command)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with response_cli_lifetime():
            await capture_bash_batch(owner, ("outer-1", {"command": "outer"}))
            bash_task = asyncio.create_task(
                owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "outer"}, "outer-1"),
            )
            async with (
                asyncio.timeout(SETTLE_TIMEOUT_SECONDS),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=_make_app(owner)), base_url="http://test") as http,
            ):
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
                if route == "read":
                    waiting = asyncio.create_task(http.get(f"/api/agent-cli/calls/{call_id}?wait=0.3", headers=headers))
                    await asyncio.sleep(0.05)
                    end_grant()
                else:
                    waiting = asyncio.create_task(http.post(f"/api/agent-cli/calls/{call_id}/cancel", headers=headers))
                    await cleanup_entered.wait()
                    end_grant()
                    release_cleanup.set()
                response = await waiting
                assert response.status_code == 401, response.text
                assert "status" not in response.json()

                release_child.set()
                release_outer.set()
                await bash_task
    finally:
        for release in (release_child, release_cleanup, release_outer):
            release.set()
        await owner.close()


def test_cli_messages_are_dropped_when_stderr_is_closed(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a stderr, `print` would write the CLI's own lines to stdout, among the command's output."""
    frames = [
        {"t": "progress", "text": "starting worker w... (5s)"},
        *command_frames(b"hello\n", b"warning\n", header="[worker: w, started]"),
    ]

    class Client(_NoStdin):
        def operation(self, _payload: object) -> dict[str, object]:
            return {"status": "running"}

        def stream(self, _call_id):
            return iter(frames)

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr("mindroom.agent_cli.main.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(sys, "stderr", None)

    assert main(["workers", "run", "w", "--", "echo", "hello"]) == 0
    assert capsys.readouterr().out == "hello\n"


@pytest.mark.parametrize("stderr", ["closed", "full"])
@pytest.mark.parametrize(
    ("frames", "expected_code", "expected_out"),
    [
        (command_frames(b"hello\n", b"warning\n", header="[worker: w, started]"), 0, b"hello\n"),
        ([{"status": "failed", "error_kind": "named_worker_refusal", "outcome": "limit reached"}], 125, b""),
    ],
)
def test_real_unusable_stderr_changes_neither_stdout_nor_the_exit_code(
    frames: list[dict[str, object]],
    expected_code: int,
    expected_out: bytes,
    stderr: str,
) -> None:
    """A stderr that is closed (`2>&-`) or fails every write (`2>/dev/full`) only loses what was written to it.

    A closed stderr is None in most Pythons; in one that starts with another file open as descriptor 2, writes fail.
    """
    if stderr == "full" and not os.path.exists("/dev/full"):  # noqa: PTH110
        pytest.skip("needs /dev/full")
    reads = 0

    def respond(method: str, _path: str) -> object:
        nonlocal reads
        if method == "POST":
            return {"status": "queued"}
        reads += 1
        return [{"t": "progress", "text": "starting worker w..."}, *frames]

    with ExitStack() as stack:
        popen: dict[str, object] = {"preexec_fn": lambda: os.close(2)}
        if stderr == "full":
            popen = {"stderr": stack.enter_context(open("/dev/full", "w"))}  # noqa: PTH123
        proc = stack.enter_context(_real_workers_run(respond, stdout=subprocess.PIPE, **popen))
        stdout, _ = proc.communicate(timeout=SETTLE_TIMEOUT_SECONDS)
        assert (proc.returncode, stdout) == (expected_code, expected_out)
    assert reads == 1


@contextmanager
def _real_workers_run(respond, **popen: object) -> Iterator[subprocess.Popen[bytes]]:
    """Run `workers run w -- true` as a real process against a local server that answers with ``respond``."""

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path.endswith("/stdin"):
                answer: object = {"open": False}
            else:
                answer = respond(self.command, self.path)
            # A list is a stream: one JSON object per line.
            lines = answer if isinstance(answer, list) else [answer]
            body = b"".join(json.dumps(line).encode() + b"\n" for line in lines)
            # The CLI may be gone by the time a held answer is written.
            with suppress(OSError):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

        do_GET = do_POST = _answer  # noqa: N815

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
    command = "from mindroom.agent_cli.main import main; raise SystemExit(main(['workers', 'run', 'w', '--', 'true']))"
    proc = subprocess.Popen([sys.executable, "-c", command], env=env, **popen)
    try:
        yield cast("subprocess.Popen[bytes]", proc)
    finally:
        proc.kill()
        proc.wait()
        server.shutdown()
        server.server_close()
        thread.join()


_PIPE_BYTES = 4096


def _small_pipe() -> tuple[int, int]:
    """Return a pipe that holds one page, which a test fills to block its writer."""
    if not hasattr(fcntl, "F_SETPIPE_SZ"):
        pytest.skip("needs a pipe whose size can be set")
    read_fd, write_fd = os.pipe()
    fcntl.fcntl(write_fd, fcntl.F_SETPIPE_SZ, _PIPE_BYTES)
    return read_fd, write_fd


def test_real_sigterm_during_a_blocked_write_of_output_cancels_and_stops_the_cli() -> None:
    """SIGTERM reaches a CLI that is blocked writing output to a full stdout pipe: it cancels and exits at once."""
    submitted, cancelled = threading.Event(), threading.Event()

    def respond(method: str, path: str) -> object:
        if path.endswith("/cancel"):
            cancelled.set()
            return {"status": "cancelled"}
        if method == "POST":
            return {"status": "queued"}
        submitted.set()
        return command_frames(b"x" * (8 * _PIPE_BYTES))

    read_fd, write_fd = _small_pipe()
    try:
        with _real_workers_run(respond, stdout=write_fd, stderr=subprocess.DEVNULL) as proc:
            assert submitted.wait(timeout=SETTLE_TIMEOUT_SECONDS)
            # Nothing reads the pipe, so once it is full the CLI is blocked writing the command's output.
            deadline = time.monotonic() + SETTLE_TIMEOUT_SECONDS
            while int.from_bytes(fcntl.ioctl(read_fd, termios.FIONREAD, bytes(4)), sys.byteorder) < _PIPE_BYTES:
                assert time.monotonic() < deadline, "the CLI never filled its stdout pipe"
                time.sleep(0.01)
            proc.send_signal(signal.SIGTERM)
            # The output it could not write does not hold up its exit either.
            assert proc.wait(timeout=SETTLE_TIMEOUT_SECONDS) == 128 + signal.SIGTERM
            assert cancelled.is_set()
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_real_second_sigterm_during_a_blocked_cancel_notice_stops_the_cli() -> None:
    """The cancel notice is written under the handlers the CLI started with, so a full stderr cannot make it deaf."""
    polling, cancelled, done = threading.Event(), threading.Event(), threading.Event()

    def respond(method: str, path: str) -> dict[str, object]:
        if path.endswith("/cancel"):
            cancelled.set()
            return {"status": "cancelled"}
        if method == "GET":
            polling.set()
            done.wait(SETTLE_TIMEOUT_SECONDS)
        return {"status": "running"}

    read_fd, write_fd = _small_pipe()
    os.set_blocking(write_fd, False)
    with suppress(BlockingIOError):
        while True:
            os.write(write_fd, b"x" * _PIPE_BYTES)
    os.set_blocking(write_fd, True)
    try:
        with _real_workers_run(respond, stdout=subprocess.DEVNULL, stderr=write_fd) as proc:
            assert polling.wait(timeout=SETTLE_TIMEOUT_SECONDS)
            proc.send_signal(signal.SIGTERM)
            assert cancelled.wait(timeout=SETTLE_TIMEOUT_SECONDS)
            # The cancel has answered, so the CLI goes on to write its notice to the full pipe.
            time.sleep(0.5)
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=SETTLE_TIMEOUT_SECONDS) == -signal.SIGTERM
    finally:
        done.set()
        os.close(read_fd)
        os.close(write_fd)


def test_real_second_sigterm_during_the_cancel_request_stops_the_cli_at_once() -> None:
    """A second signal does not wait for the cancel request: it ends the CLI as a signal with no handler does."""
    polling, cancelling, done = threading.Event(), threading.Event(), threading.Event()

    def respond(method: str, path: str) -> dict[str, object]:
        if path.endswith("/cancel"):
            cancelling.set()
            done.wait(SETTLE_TIMEOUT_SECONDS)
            return {"status": "cancelled"}
        if method == "GET":
            polling.set()
            done.wait(SETTLE_TIMEOUT_SECONDS)
        return {"status": "running"}

    try:
        with _real_workers_run(respond, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as proc:
            assert polling.wait(timeout=SETTLE_TIMEOUT_SECONDS)
            proc.send_signal(signal.SIGTERM)
            assert cancelling.wait(timeout=SETTLE_TIMEOUT_SECONDS)
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=SETTLE_TIMEOUT_SECONDS) == -signal.SIGTERM
    finally:
        done.set()


def test_cli_submit_refused_with_401_exits_125_at_once(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A submit the server answers 401, 403, or 404 was never admitted, so the CLI exits 125 without reading it."""
    from mindroom.agent_cli.client import AgentCliRefusedError  # noqa: PLC0415

    class Client:
        def operation(self, _payload: object):
            msg = "Agent CLI authority or transport is unavailable"
            raise AgentCliRefusedError(msg)

        def stream(self, call_id):
            del call_id
            pytest.fail("a refused submit has no stream to read")

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr("mindroom.agent_cli.main.time.sleep", lambda _seconds: pytest.fail("must not wait"))

    assert main(["workers", "run", "w", "--", "true"]) == 125
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "mindroom-agent: Agent CLI authority or transport is unavailable\n"


@pytest.mark.parametrize("status", [401, 403, 404])
def test_http_client_reports_a_4xx_without_a_reason_as_refused(status: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4xx the server gives no reason for is a refusal, which stays an unavailable error for a receipt read."""
    from mindroom.agent_cli.client import AgentCliRefusedError  # noqa: PLC0415

    class Handler(BaseHTTPRequestHandler):
        def _refuse(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(status)
            self.end_headers()

        do_GET = do_POST = _refuse  # noqa: N815

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("MINDROOM_AGENT_CLI_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("MINDROOM_AGENT_CLI_TOKEN", "private-capability")
    try:
        client = AgentCliClient()
        with pytest.raises(AgentCliRefusedError):
            client.operation({"operation": "tools.list"})
        with pytest.raises(AgentCliUnavailableError):
            client.receipt(str(uuid4()))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_cli_signal_held_during_a_rejected_submit_exits_with_the_signal(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal held while the submit was rejected still ends the CLI as that signal, and cancels nothing."""
    installed: dict[int, object] = {}
    submitted: list[str] = []

    def fake_signal(signum, handler):
        previous = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        return previous

    class Client:
        def operation(self, payload):
            submitted.append(payload["call_id"])
            handler = installed[signal.SIGTERM]
            assert callable(handler)
            handler(int(signal.SIGTERM), None)
            msg = "Agent CLI request was rejected: worker name is invalid"
            raise ValueError(msg)

        def cancel(self, call_id, *, timeout: float = 8.0):
            del call_id, timeout
            pytest.fail("a rejected submit admitted nothing to cancel")

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr(signal, "signal", fake_signal)

    assert main(["workers", "run", "fan3", "--", "true"]) == 143
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == f"mindroom-agent: cancelled by SIGTERM (call {submitted[0]})\n"


def test_cli_signal_during_handler_installation_is_held_until_the_submit_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal that lands while the handlers are being installed cancels the command after its submit."""
    installed: dict[int, object] = {}
    events: list[str] = []

    def fake_signal(signum, handler):
        previous = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        if signum == signal.SIGINT and callable(handler) and not events:
            events.append("signalled")
            handler(int(signal.SIGINT), None)
        return previous

    class Client:
        def operation(self, payload):
            events.append("submitted")
            return {"status": "running", "call_id": payload["call_id"]}

        def stream(self, call_id):
            del call_id
            pytest.fail("a held signal cancels before the first read")

        def cancel(self, call_id, *, timeout: float = 8.0):
            del call_id, timeout
            events.append("cancelled")
            return {"status": "cancelled"}

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr(signal, "signal", fake_signal)

    assert main(["workers", "run", "fan3", "--", "sleep", "300"]) == 130
    assert events == ["signalled", "submitted", "cancelled"]


@pytest.mark.asyncio
async def test_cancel_of_an_unknown_call_leaves_no_trace_for_a_later_submit(tmp_path: Path) -> None:
    """A cancel for an ID the owner does not hold is refused, and a later submit of that ID runs normally."""
    outer_entered = asyncio.Event()
    release_outer = asyncio.Event()

    async def run_shell_command(args: str) -> str:
        if args == "outer":
            outer_entered.set()
            await release_outer.wait()
        return f"{args} done"

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
                with pytest.raises(CliAuthenticationError):
                    await owner.cancel_call(str(call_id))
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
                done = await owner.get_call(str(call_id), wait=5.0)
                assert done["status"] == "completed"
                assert done["outcome"] == "child done"
                release_outer.set()
                await bash_task
    finally:
        await owner.close()
