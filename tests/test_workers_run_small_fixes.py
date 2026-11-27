"""Tests for `mindroom-agent workers run` items 7, 10, and 11."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import signal
import sys
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Self
from uuid import uuid4

import httpx
import pytest

from mindroom.agent_cli.client import AgentCliUnavailableError
from mindroom.agent_cli.main import main
from mindroom.agent_cli.shell_contract import AGENT_CLI_WINDOW_HEADER
from mindroom.agent_cli.turn import ToolKey
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.shell.output_capture import format_shell_completion
from mindroom.shell.supervisor import SHELL_SUPERVISOR_SOCKET_ENV
from mindroom.tool_system.catalog import builtin_tool_registry, get_tool_by_name
from mindroom.tool_system.sandbox_proxy import WorkerStandIns
from mindroom.tool_system.tool_function import ToolOutcome
from mindroom.workers.models import WorkerHandle
from mindroom.workers.protocol import WORKER_PROTOCOL_VERSION, ExecuteRequest
from tests.test_workers_run_control import (
    SETTLE_TIMEOUT_SECONDS,
    _make_app,
    _make_owner,
    _NoStdin,
    capture_bash_batch,
    response_cli_lifetime,
)
from tests.workers_run_frames import FakeStream, command_frames

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterator
    from pathlib import Path

_TEST_WORKER_TOKEN = "test-worker-token"  # noqa: S105


def _shell_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    base_dir: Path | None = None,
) -> Callable[..., Coroutine[object, object, object]]:
    monkeypatch.delenv(SHELL_SUPERVISOR_SOCKET_ENV, raising=False)
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_RUNNER_MODE": "true"},
    )
    overrides: dict[str, object] = {}
    if base_dir is not None:
        overrides["base_dir"] = str(base_dir)
    shell = get_tool_by_name(
        "shell",
        runtime_paths,
        tools=builtin_tool_registry(),
        runs_in_worker=False,
        worker_target=None,
        tool_init_overrides=overrides or None,
    )
    return shell.functions["run_shell_command"]


def _cli_with_stream(monkeypatch: pytest.MonkeyPatch, frames: list[dict[str, object]]) -> list[str]:
    """Answer the CLI's submit with a running call whose stream is ``frames``."""
    submitted: list[str] = []

    class Client(_NoStdin):
        def operation(self, payload: dict[str, object]) -> dict[str, object]:
            call_id = str(payload["call_id"])
            submitted.append(call_id)
            return {"status": "running", "call_id": call_id}

        def stream(self, call_id: str) -> Iterator[dict[str, object]]:
            del call_id
            return iter(frames)

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    return submitted


# ---------------------------------------------------------------------------
# Item 7: Missing / non-executable binary -> 127 / 126
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("program", "exit_code", "reason"),
    [
        ("/nonexistent-binary-xyz", 127, "No such file or directory"),
        ("__missing_bare_cmd_xyz__", 127, "command not found"),
        ("{tmp_path}/not-executable.txt", 126, "Permission denied"),
    ],
)
async def test_item7_program_that_cannot_start_exits_as_a_shell_reports_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    program: str,
    exit_code: int,
    reason: str,
) -> None:
    """A missing or non-executable program ends as a command that failed with a shell's exit code and words.

    The shell call's own result keeps them under ``Stderr:``; `workers run`, which streams, writes them to stderr.
    """
    (tmp_path / "not-executable.txt").write_text("hello\n")
    program = program.format(tmp_path=tmp_path)
    shell_result = await _shell_entrypoint(tmp_path, monkeypatch)([program], worker="w1")
    assert shell_result == ToolOutcome(
        content=format_shell_completion("", f"{program}: {reason}", return_code=exit_code),
        is_error=True,
        counts_as_tool_failure=False,
    )

    said = f"{program}: {reason}\n"
    _cli_with_stream(monkeypatch, command_frames(stderr=said.encode(), code=exit_code, header="[worker: w1]"))
    assert main(["workers", "run", "w1", "--", program]) == exit_code
    output = capsys.readouterr()
    assert (output.out, output.err) == ("", said)


@pytest.mark.asyncio
async def test_item7_missing_cwd_still_fails_as_execution_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing working directory is still an execution error (not 127 command not found)."""
    missing_dir = tmp_path / "missing-cwd"
    run_shell = _shell_entrypoint(tmp_path, monkeypatch, base_dir=missing_dir)
    with pytest.raises(ValueError, match="missing-cwd"):
        await run_shell(["echo", "hi"], worker="w1")


# ---------------------------------------------------------------------------
# Item 10: Confirmed cancel says so
# ---------------------------------------------------------------------------


def _run_signalled_cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    worker: str,
    cancel_response: dict[str, object] | Exception,
    sig: signal.Signals = signal.SIGTERM,
) -> tuple[int, str]:
    installed: dict[int, object] = {}
    submitted: list[str] = []

    def fake_signal(signum: int, handler: object) -> object:
        prev = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        return prev

    class Client(_NoStdin):
        def operation(self, payload: dict[str, object]) -> dict[str, object]:
            call_id = str(payload["call_id"])
            submitted.append(call_id)
            return {"status": "running", "call_id": call_id}

        def stream(self, call_id: str) -> Iterator[dict[str, object]]:
            del call_id
            handler = installed[int(sig)]
            assert callable(handler)
            handler(int(sig), None)
            pytest.fail("signal handler should interrupt wait")

        def cancel(self, call_id: str, *, timeout: float = 8.0) -> dict[str, object]:
            del call_id, timeout
            if isinstance(cancel_response, Exception):
                raise cancel_response
            return cancel_response

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr(signal, "signal", fake_signal)
    code = main(["workers", "run", worker, "--", "sleep", "60"])
    return code, submitted[0]


def test_item10_never_ran_cancel_prints_never_ran_notice_without_call_id(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When cancel returns status=cancelled with cancel_outcome='never_ran', print never-ran notice without call id."""
    code, call_id = _run_signalled_cli(
        monkeypatch,
        worker="f2",
        cancel_response={"status": "cancelled", "cancel_outcome": "never_ran"},
    )
    output = capsys.readouterr()
    assert code == 128 + int(signal.SIGTERM)
    assert output.out == ""
    assert output.err == "mindroom-agent: cancelled by SIGTERM; the command never ran in worker f2\n"
    assert call_id not in output.err


def test_item10_confirmed_cancel_says_the_command_was_stopped(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel the command's exit confirmed says the command was stopped, which needs no call id to follow up."""
    code, call_id = _run_signalled_cli(
        monkeypatch,
        worker="f2",
        cancel_response={"status": "cancelled", "cancel_outcome": "stopped"},
    )
    output = capsys.readouterr()
    assert code == 128 + int(signal.SIGTERM)
    assert output.out == ""
    assert output.err == "mindroom-agent: cancelled by SIGTERM; stopped the command in worker f2\n"
    assert call_id not in output.err


@pytest.mark.parametrize(
    "cancel_response",
    [
        {"status": "cancelled", "cancel_outcome": None},
        {"status": "cancelled"},
        {"status": "running"},
        {"status": "failed", "outcome": "boom"},
        AgentCliUnavailableError("timed out"),
    ],
)
def test_item10_unconfirmed_cancel_keeps_may_still_be_running(
    cancel_response: dict[str, object] | Exception,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When cancel is not confirmed, stderr keeps 'may still be running' with the call id."""
    code, call_id = _run_signalled_cli(monkeypatch, worker="f2", cancel_response=cancel_response)
    output = capsys.readouterr()
    assert code == 128 + int(signal.SIGTERM)
    assert output.out == ""
    assert (
        output.err
        == f"mindroom-agent: cancelled by SIGTERM; the command may still be running in worker f2 (call {call_id})\n"
    )


class _FakeWorkerManager:
    def __init__(self, handle: WorkerHandle) -> None:
        self._handle = handle

    def ensure_worker(self, _spec: object, **_kwargs: object) -> WorkerHandle:
        return self._handle

    def touch_worker(self, _key: str) -> WorkerHandle:
        return self._handle

    def record_failure(self, *_args: object, **_kwargs: object) -> None:
        return

    def cleanup_locator(self, _handle: object) -> None:
        return None


def _install_fake_worker_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner_cancel_response: httpx.Response,
    in_flight: threading.Event,
    release_stream: threading.Event,
) -> Callable[..., Coroutine[object, object, object]]:
    handle = WorkerHandle(
        worker_id="worker-f2",
        worker_key="agent:test:named=f2",
        endpoint="http://worker/api/sandbox-runner/execute",
        auth_token=_TEST_WORKER_TOKEN,
        status="ready",
        backend_name="kubernetes",
        last_used_at=0.0,
        created_at=0.0,
    )

    class _FakeHttpClient:
        def __init__(self, *, timeout: float) -> None:
            del timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_exc: object) -> None:
            return

        @contextmanager
        def stream(self, method: str, url: str, **_kwargs: object) -> Iterator[httpx.Response]:
            in_flight.set()
            release_stream.wait(timeout=5.0)
            body = json.dumps({"ok": False, "error": "Tool call was cancelled.", "failure_kind": "tool"}).encode()
            yield httpx.Response(200, content=body, request=httpx.Request(method, url))

        def post(self, url: str, **_kwargs: object) -> httpx.Response:
            release_stream.set()
            return httpx.Response(
                runner_cancel_response.status_code,
                content=runner_cancel_response.content,
                headers=runner_cancel_response.headers,
                request=httpx.Request("POST", url),
            )

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
                kwargs={"args": ["sleep", "60"]},
            ),
            handle,
            None,
        ),
    )
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _FakeHttpClient)

    async def local_placeholder(args: str) -> str:
        return args

    stand_ins = WorkerStandIns(
        tool_name="shell",
        runtime_paths=runtime_paths,
        credentials_manager=None,
        tools=builtin_tool_registry(),
    )
    return stand_ins("run_shell_command", local_placeholder)


@pytest.mark.asyncio
async def test_item10_server_cancel_in_flight_leaves_cancel_outcome_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel after dispatch to the worker leaves cancel_outcome=None (CLI says may still be running)."""
    outer_entered = asyncio.Event()
    release_outer = asyncio.Event()
    in_flight = threading.Event()
    release_stream = threading.Event()
    proxied_shell = _install_fake_worker_proxy(
        tmp_path,
        monkeypatch,
        runner_cancel_response=httpx.Response(200, json={"cancelled": True}),
        in_flight=in_flight,
        release_stream=release_stream,
    )

    async def run_shell_command(args: str) -> str:
        if args == "outer":
            outer_entered.set()
            await release_outer.wait()
            return "outer done"
        return str(await proxied_shell(args))

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
                            "arguments": {"args": "sleep"},
                        },
                    )
                    assert resp.status_code == 200
                    assert await asyncio.to_thread(in_flight.wait, 5.0)

                    cancel_resp = await http.post(
                        f"/api/agent-cli/calls/{call_id}/cancel",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert cancel_resp.status_code == 200
                    receipt = cancel_resp.json()
                    assert receipt["status"] == "cancelled"
                    assert receipt["cancel_outcome"] is None
                release_outer.set()
                await bash_task
    finally:
        release_stream.set()
        await owner.close()


# ---------------------------------------------------------------------------
# Item 11: Progress rewrites in place on a TTY
# ---------------------------------------------------------------------------


class _FakeTtyStderr(FakeStream):
    def __init__(self, *, is_tty: bool, fail_after: int | None = None) -> None:
        super().__init__(is_tty=is_tty)
        self._fail_after = fail_after
        self._writes = 0

    def write(self, s: str) -> int:
        self._writes += 1
        if self._fail_after is not None and self._writes > self._fail_after:
            raise OSError(errno.EPIPE, "broken pipe")
        return super().write(s)


_PROGRESS = ("starting worker f1...", "starting worker f1... (5s)", "worker f1 ready (7s)")


def _run_progress_cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stderr_stream: _FakeTtyStderr,
    header: str = "[worker: f1, started]",
    body: str = "done",
) -> tuple[int, str, str]:
    stdout_stream = FakeStream()
    _cli_with_stream(
        monkeypatch,
        [
            *({"t": "progress", "text": text} for text in _PROGRESS),
            *command_frames(f"{body}\n".encode(), header=header),
        ],
    )
    monkeypatch.setattr(sys, "stderr", stderr_stream)
    monkeypatch.setattr(sys, "stdout", stdout_stream)
    code = main(["workers", "run", "f1", "--", "echo", "done"])
    return code, stdout_stream.getvalue(), stderr_stream.getvalue()


def test_item11_tty_progress_rewrites_in_place_and_ends_with_newline_before_cli_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r"""On a TTY, progress rewrites in place with \r + clear-to-EOL and ends with \n before other CLI lines."""
    tty = _FakeTtyStderr(is_tty=True)
    code, out, err = _run_progress_cli(monkeypatch, stderr_stream=tty, header="[worker: f1, started]", body="done")

    assert code == 0
    assert out == "done\n"
    assert err == (
        "\r\x1b[Kmindroom-agent: starting worker f1..."
        "\r\x1b[Kmindroom-agent: starting worker f1... (5s)"
        "\r\x1b[Kmindroom-agent: worker f1 ready (7s)\n"
        "mindroom-agent: started worker f1\n"
    )


def test_item11_tty_progress_is_cut_to_the_terminal_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """A progress line wider than the terminal would wrap, and the next rewrite would leave its first rows behind."""
    tty = _FakeTtyStderr(is_tty=True)
    monkeypatch.setattr(tty, "fileno", lambda: 2)
    monkeypatch.setattr("mindroom.agent_cli.main.os.get_terminal_size", lambda _fd: os.terminal_size((30, 24)))
    code, out, err = _run_progress_cli(monkeypatch, stderr_stream=tty)

    assert code == 0
    assert out == "done\n"
    assert err == (
        "\r\x1b[Kmindroom-agent: starting work"
        "\r\x1b[Kmindroom-agent: starting work"
        "\r\x1b[Kmindroom-agent: worker f1 rea\n"
        "mindroom-agent: started worker f1\n"
    )


def test_item11_tty_progress_ends_with_newline_before_command_output_without_other_cli_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r"""When there are no further CLI lines after progress, the progress line still ends with \n before stdout."""
    events: list[tuple[str, bytes]] = []
    _cli_with_stream(
        monkeypatch,
        [{"t": "progress", "text": "starting worker f1..."}, *command_frames(b"hello\n", header="[worker: f1]")],
    )
    monkeypatch.setattr(sys, "stderr", FakeStream(is_tty=True, name="err", log=events))
    monkeypatch.setattr(sys, "stdout", FakeStream(name="out", log=events))

    assert main(["workers", "run", "f1", "--", "echo", "hello"]) == 0
    assert events == [
        ("err", b"\r\x1b[Kmindroom-agent: starting worker f1..."),
        ("err", b"\n"),
        ("out", b"hello\n"),
    ]


def test_item11_non_tty_progress_keeps_one_line_per_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-TTY stderr keeps one newline-terminated line per progress update with no escape codes."""
    pipe = _FakeTtyStderr(is_tty=False)
    code, out, err = _run_progress_cli(monkeypatch, stderr_stream=pipe, header="[worker: f1, started]", body="done")

    assert code == 0
    assert out == "done\n"
    assert "\r" not in err
    assert "\x1b[K" not in err
    assert err == (
        "mindroom-agent: starting worker f1...\n"
        "mindroom-agent: starting worker f1... (5s)\n"
        "mindroom-agent: worker f1 ready (7s)\n"
        "mindroom-agent: started worker f1\n"
    )


def test_item11_tty_progress_ignores_stderr_write_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """TTY progress stays best-effort when stderr fails mid-run."""
    flaky = _FakeTtyStderr(is_tty=True, fail_after=1)
    code, out, _err = _run_progress_cli(monkeypatch, stderr_stream=flaky, header="[worker: f1]", body="ok")

    assert code == 0
    assert out == "ok\n"


def test_item11_tty_progress_ends_with_newline_before_cancel_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    r"""A signal while a TTY progress line is open ends that line with \n before the cancel notice."""
    installed: dict[int, object] = {}

    def fake_signal(signum: int, handler: object) -> object:
        prev = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        return prev

    class Client(_NoStdin):
        def operation(self, payload: dict[str, object]) -> dict[str, object]:
            return {"status": "running", "call_id": payload["call_id"]}

        def stream(self, call_id: str) -> Iterator[dict[str, object]]:
            del call_id
            yield {"t": "progress", "text": "starting worker f1..."}
            handler = installed[int(signal.SIGTERM)]
            assert callable(handler)
            handler(int(signal.SIGTERM), None)
            pytest.fail("signal handler should interrupt wait")

        def cancel(self, call_id: str, *, timeout: float = 8.0) -> dict[str, object]:
            del call_id, timeout
            return {"status": "cancelled", "cancel_outcome": "never_ran"}

    tty = _FakeTtyStderr(is_tty=True)
    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr(sys, "stderr", tty)

    assert main(["workers", "run", "f1", "--", "sleep", "60"]) == 128 + int(signal.SIGTERM)
    assert tty.getvalue() == (
        "\r\x1b[Kmindroom-agent: starting worker f1...\n"
        "mindroom-agent: cancelled by SIGTERM; the command never ran in worker f1\n"
    )
