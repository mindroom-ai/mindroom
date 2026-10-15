"""The runner streams a call's progress and keep-alives before its result, and a dropped stream stops the call."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import sys
import threading
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

import mindroom.api.sandbox_runner as sandbox_runner_module
from mindroom.api import sandbox_request_cancellation
from mindroom.api.sandbox_runner_app import app as sandbox_runner_app
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.tool_jobs.control import job_owns_execution
from mindroom.tool_system.progress import report_tool_progress, tool_progress_reporter

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_TOKEN = "runner-test-token"  # noqa: S105
_HEADERS = {"x-mindroom-sandbox-token": _TOKEN}
_EXECUTE = "/api/sandbox-runner/execute"


class _FakeToolkit:
    requires_connect = False


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    """A runner app over an empty config, with short keep-alives."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINDROOM_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(tmp_path / ".mindroom"))
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", _TOKEN)
    monkeypatch.setattr(sandbox_runner_module, "EXECUTE_KEEPALIVE_SECONDS", 0.05)
    _refresh_runner()
    with TestClient(sandbox_runner_app) as client:
        yield client


def _refresh_runner() -> None:
    runtime_paths = resolve_primary_runtime_paths(process_env=dict(os.environ))
    config = sandbox_runner_module._runtime_config_or_empty(runtime_paths)
    sandbox_runner_module.initialize_sandbox_runner_app(sandbox_runner_app, runtime_paths, config=config)


def _fake_tool(monkeypatch: pytest.MonkeyPatch, entrypoint: Callable[..., object]) -> None:
    monkeypatch.setattr(sandbox_runner_module, "_resolve_entrypoint", lambda **_kwargs: (_FakeToolkit(), entrypoint))


def _stream(client: TestClient, **request: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    body = {"tool_name": "fake", "function_name": "call", **request}
    with client.stream("POST", _EXECUTE, headers=_HEADERS, json=body) as response:
        assert response.status_code == 200
        return [json.loads(line) for line in response.iter_lines() if line]


def _result(result: object) -> dict[str, Any]:
    return {"ok": True, "result": result, "error": None, "failure_kind": None}


def test_a_call_streams_its_progress_and_keepalives_before_its_result(
    runner: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The result line is the same JSON a single response carried; progress and keep-alives come first."""

    async def entrypoint() -> str:
        report_tool_progress("step 1")
        await asyncio.sleep(0.3)
        return "done"

    _fake_tool(monkeypatch, entrypoint)
    frames = _stream(runner)

    assert frames[0] == {"progress": "step 1"}
    assert {"keepalive": True} in frames
    assert frames[-1] == _result("done")


def test_a_synchronous_call_runs_off_the_event_loop_so_keepalives_keep_flowing(
    runner: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocking tool function cannot starve the stream of keep-alives."""

    def entrypoint() -> str:
        time.sleep(0.3)
        return "done"

    _fake_tool(monkeypatch, entrypoint)
    frames = _stream(runner)

    assert {"keepalive": True} in frames
    assert frames[-1] == _result("done")


@pytest.mark.parametrize("run_as_job", [False, True])
def test_a_job_request_runs_its_tool_as_part_of_that_job(
    runner: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_as_job: bool,
) -> None:
    """A tool in the runner knows whether its call runs as a background job."""
    _fake_tool(monkeypatch, job_owns_execution)

    assert _stream(runner, run_as_job=run_as_job)[-1] == _result(run_as_job)


def test_an_unexpected_runner_error_after_the_stream_started_is_a_worker_failure_line(
    runner: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the stream started, a runner failure is a result line that blames the worker, not a status code."""

    def broken(**_kwargs: object) -> object:
        msg = "runner bug"
        raise RuntimeError(msg)

    monkeypatch.setattr(sandbox_runner_module, "_resolve_entrypoint", broken)

    [frame] = _stream(runner)
    assert frame["ok"] is False
    assert frame["failure_kind"] == "worker"
    assert "runner bug" in frame["error"]


def test_a_spawned_child_runs_a_job_request_as_part_of_that_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """A per-request child binds the job its request belongs to before running the tool."""
    seen: list[bool] = []

    async def fake_execute(*_args: object, **_kwargs: object) -> sandbox_runner_module.SandboxRunnerExecuteResponse:
        seen.append(job_owns_execution())
        return sandbox_runner_module.SandboxRunnerExecuteResponse(ok=True, result="done")

    monkeypatch.setattr(sandbox_runner_module, "_execute_prepared_request_inprocess", fake_execute)
    monkeypatch.setattr(sandbox_runner_module, "_ensure_registry_loaded_with_config", lambda *_args: None)
    runtime_paths = resolve_primary_runtime_paths(process_env={})
    for run_as_job in (False, True):
        envelope = sandbox_runner_module.sandbox_protocol.serialize_subprocess_envelope(
            request=sandbox_runner_module.PreparedSandboxRunnerExecuteRequest(
                tool_name="fake",
                function_name="call",
                run_as_job=run_as_job,
            ).model_dump(mode="json"),
            runtime_paths=sandbox_runner_module.constants.serialize_runtime_paths(runtime_paths),
            config_yaml="agents: {}\n",
        )
        sandbox_runner_module._run_subprocess_worker_payload(envelope, lambda _text: None)

    assert seen == [False, True]


async def _drive_until_disconnect(body: dict[str, Any], *, disconnect_after: Callable[[], bool]) -> list[bytes]:
    """Run one execute request through the ASGI app and hang up once ``disconnect_after`` says so."""
    sent: list[bytes] = []
    request_sent = False
    hang_up = asyncio.Event()

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
        await hang_up.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            sent.append(message.get("body", b""))
            if disconnect_after():
                hang_up.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": _EXECUTE,
        "raw_path": _EXECUTE.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            *((key.encode(), value.encode()) for key, value in _HEADERS.items()),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 80),
    }
    async with asyncio.timeout(20):
        await sandbox_runner_app(scope, receive, send)
    return sent


@pytest.mark.asyncio
@pytest.mark.usefixtures("runner")
async def test_a_dropped_stream_cancels_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A primary that hangs up stops the call, and nothing more is written to it."""
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def entrypoint() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "unreachable"

    _fake_tool(monkeypatch, entrypoint)
    sent = await _drive_until_disconnect(
        {"tool_name": "fake", "function_name": "call"},
        disconnect_after=started.is_set,
    )

    await asyncio.wait_for(cancelled.wait(), 5)
    assert all(b'"ok"' not in chunk for chunk in sent)


@pytest.mark.asyncio
@pytest.mark.usefixtures("runner")
async def test_a_dropped_stream_kills_a_worker_shell_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """In a per-request child, a dropped stream stops the supervised shell command and every process it started."""
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE", "subprocess")
    _refresh_runner()
    pid_file = tmp_path / "command.pid"
    command = f"echo $$ > {shlex.quote(str(pid_file))}; sleep 300"

    sent = await _drive_until_disconnect(
        {"tool_name": "shell", "function_name": "run_shell_command", "args": [command], "run_as_job": True},
        disconnect_after=lambda: pid_file.exists() and bool(pid_file.read_text()),
    )

    pid = int(pid_file.read_text())
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("the shell command survived the dropped stream")
    assert all(b'"ok"' not in chunk for chunk in sent)


_PROGRESS_CHILD = """
import json, socket, sys, time
conn = socket.socket(fileno=int(sys.argv[sys.argv.index("--progress-fd") + 1]))
for text in ("one", "two"):
    conn.sendall(json.dumps({"progress": text}).encode() + b"\\n")
time.sleep(0.3)
print("out", end="")
"""


def test_a_spawned_child_relays_its_progress_and_runs_without_a_limit() -> None:
    """A spawned request child reports progress through its own socket, and a job's child has no deadline."""
    reports: list[str] = []

    with tool_progress_reporter(reports.append):
        completed = sandbox_runner_module._run_request_subprocess(
            [sys.executable, "-c", _PROGRESS_CHILD],
            input="",
            timeout=None,
            env=None,
            cwd=None,
        )

    assert completed.returncode == 0
    assert completed.stdout == "out"
    assert reports == ["one", "two"]


_PIPE_HOLDING_CHILD = """
import pathlib, subprocess, sys
pids = pathlib.Path(sys.argv[1])
member = subprocess.Popen(["sleep", "300"])
escaped = subprocess.Popen(["sleep", "300"], start_new_session=True)
pids.write_text(f"{member.pid} {escaped.pid}")
member.wait()
"""


def test_stopping_a_spawned_child_kills_its_group_and_never_waits_for_a_pipe_holder(tmp_path: Path) -> None:
    """A stop kills what the child started; a process that left its group cannot keep the call waiting."""
    pid_file = tmp_path / "pids"
    stopper_done = threading.Event()

    def stop_once_started() -> None:
        while not (pid_file.exists() and pid_file.read_text()):
            time.sleep(0.01)
        sandbox_request_cancellation.cancel_request("spawned")
        stopper_done.set()

    escaped = None
    try:
        with sandbox_request_cancellation.track_request("spawned"):
            threading.Thread(target=stop_once_started, daemon=True).start()
            started = time.monotonic()
            completed = sandbox_runner_module._run_request_subprocess(
                [sys.executable, "-c", _PIPE_HOLDING_CHILD, str(pid_file)],
                input="",
                timeout=None,
                env=None,
                cwd=None,
            )
        member, escaped = (int(pid) for pid in pid_file.read_text().split())

        assert stopper_done.is_set()
        assert completed.returncode != 0
        assert time.monotonic() - started < 10
        for _ in range(100):
            try:
                os.kill(member, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("a process in the child's group survived the stop")
    finally:
        if escaped is not None:
            with suppress(ProcessLookupError):
                os.kill(escaped, signal.SIGKILL)


@pytest.mark.usefixtures("runner")
def test_a_worker_shell_call_streams_its_output_as_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shell command in a per-request child reports its output tail through the supervisor to the stream."""
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE", "subprocess")
    _refresh_runner()
    with TestClient(sandbox_runner_app) as client:
        frames = _stream(
            client,
            tool_name="shell",
            function_name="run_shell_command",
            args=["echo started; sleep 2.5; echo finished"],
        )

    assert {"progress": "started"} in frames
    assert frames[-1]["ok"] is True
    assert frames[-1]["result"].endswith("started\nfinished")
