"""`workers run` streams its command's bytes: a real CLI process, the primary's CLI routes, and a real sandbox runner."""

# ruff: noqa: ANN001
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import signal
import socket
import sys
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
import pytest
import uvicorn
import uvloop
from fastapi import FastAPI, HTTPException

from mindroom.agent_cli import turn
from mindroom.agent_cli.lifetime import response_cli_lifetime
from mindroom.agent_cli.session import CliCallConflictError, TurnToolRegistry
from mindroom.agent_cli.shell_contract import (
    AGENT_CLI_TOKEN_ENV,
    AGENT_CLI_URL_ENV,
    AGENT_CLI_WINDOW_ENV,
    AGENT_CLI_WINDOW_HEADER,
)
from mindroom.api import sandbox_runner
from mindroom.api.agent_cli import bind_agent_cli_registry, router
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.script_runs.launch_contract import RUN_ID_ENV
from mindroom.shell import execution
from mindroom.tool_system import sandbox_proxy
from mindroom.tool_system.tool_access import ToolKey
from mindroom.tool_system.toolkits import toolkit_function
from mindroom.workers.runtime import reset_primary_worker_manager
from tests.conftest import SETTLE_TIMEOUT_SECONDS
from tests.minimal_agent_fixtures import capture_bash_batch
from tests.terminal.conftest import agent_toolkit, primary_paths, serve_runner
from tests.test_workers_run_control import _make_owner

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.agent_cli.turn import LiveTurnTools

pytestmark = pytest.mark.asyncio

_WINDOW = "outer-1"
_CLI = "import sys; from mindroom.agent_cli.main import main; raise SystemExit(main(sys.argv[1:]))"
_RUNNER_MODES = pytest.mark.parametrize("mode", ["forkserver", "subprocess"])
# What the shell tool answers a model today for `echo OUT; echo ERR >&2; exit 7`.
_CANNOT_START = pytest.mark.parametrize(
    ("program", "exit_code", "said"),
    [
        ("/missing-program-xyz", 127, "/missing-program-xyz: No such file or directory\n"),
        ("missing-program-xyz", 127, "missing-program-xyz: command not found\n"),
        ("/etc/passwd", 126, "/etc/passwd: Permission denied\n"),
    ],
)
_MODEL_RESULT = "Exit code 7\nOutput:\nOUT\nStderr:\nERR"


@dataclass
class _Stack:
    """A primary that serves one reply's CLI routes, whose shell commands run in a real sandbox runner."""

    url: str
    token: str
    owner: LiveTurnTools
    registry: TurnToolRegistry

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", AGENT_CLI_WINDOW_HEADER: _WINDOW}

    async def spawn(self, *argv: str, **popen: Any) -> asyncio.subprocess.Process:  # noqa: ANN401
        """Start `mindroom-agent workers run w ARGV` as a real process."""
        env = {key: value for key, value in os.environ.items() if key != RUN_ID_ENV}
        env.update({AGENT_CLI_URL_ENV: self.url, AGENT_CLI_TOKEN_ENV: self.token, AGENT_CLI_WINDOW_ENV: _WINDOW})
        popen = {"stdin": asyncio.subprocess.DEVNULL, "stdout": asyncio.subprocess.PIPE, **popen}
        popen.setdefault("stderr", asyncio.subprocess.PIPE)
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _CLI,
            "workers",
            "run",
            "w",
            *argv,
            env=env,
            **popen,
        )

    async def run(self, *argv: str, stdin: bytes | None = None) -> tuple[int | None, bytes, bytes]:
        """Run `workers run w ARGV` to its end, and return its exit code, stdout and stderr."""
        process = await self.spawn(*argv, **({} if stdin is None else {"stdin": asyncio.subprocess.PIPE}))
        try:
            async with asyncio.timeout(4 * SETTLE_TIMEOUT_SECONDS):
                stdout, stderr = await process.communicate(stdin)
        finally:
            with suppress(ProcessLookupError):
                process.kill()
        return process.returncode, stdout, stderr

    def submit(self, command: str, **operation: object) -> dict[str, object]:
        """Return a `tools.call` of the shell tool for ``command``, as `workers run` submits it."""
        return {
            "operation": "tools.call",
            "call_id": str(uuid4()),
            "toolkit": "shell",
            "function": "run_shell_command",
            "arguments": {"args": ["bash", "-c", command], "worker": "w", "timeout": 600},
            **operation,
        }


async def _serve(app) -> tuple[uvicorn.Server, asyncio.Task[None], str]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
        while not server.started:
            if serving.done():
                await serving
            await asyncio.sleep(0.01)
    return server, serving, f"http://127.0.0.1:{listener.getsockname()[1]}"


@asynccontextmanager
async def _stack(tmp_path: Path, mode: str = "forkserver") -> AsyncIterator[_Stack]:
    """Serve a runner in ``mode`` and a primary whose reply has one Bash call open, which admits its CLI calls."""
    reset_primary_worker_manager()
    runner, runner_serving, runner_url = await serve_runner(tmp_path, mode)
    config = Config(agents={"code": AgentConfig(display_name="Code", tools=["shell"])})
    function = toolkit_function(
        agent_toolkit("shell", config, primary_paths(tmp_path, runner_url)),
        "run_shell_command",
    )
    assert function is not None
    entered, release = asyncio.Event(), asyncio.Event()

    async def run_shell_command(
        args: list[str] | str,
        tail: int = 100,
        timeout: int | None = None,  # noqa: ASYNC109
        worker: str | None = None,
    ) -> str:
        """Run a command in the runner; a named worker needs Kubernetes, so the shared runner stands in for it."""
        del worker
        if args == "outer":
            entered.set()
            await release.wait()
            return "outer done"
        return await function.entrypoint(args, tail=tail, timeout=timeout)

    owner, token = await _make_owner(tmp_path, run_shell_command)
    registry = TurnToolRegistry()
    registry.register(owner)
    app = FastAPI()
    app.include_router(router)
    bind_agent_cli_registry(app, registry)
    primary, primary_serving, url = await _serve(app)
    try:
        async with response_cli_lifetime():
            await capture_bash_batch(owner, (_WINDOW, {"command": "outer"}))
            bash = asyncio.create_task(
                owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "outer"}, _WINDOW),
            )
            async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                await entered.wait()
            try:
                yield _Stack(url, token, owner, registry)
            finally:
                release.set()
                await bash
    finally:
        await owner.close()
        primary.should_exit = runner.should_exit = True
        await asyncio.gather(primary_serving, runner_serving)
        reset_primary_worker_manager()


def _alive(pid: int) -> bool:
    """Return whether ``pid`` still runs; a process that ended and waits to be reaped does not."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:  # noqa: PTH123
            return stat.read().rpartition(")")[2].split()[0] != "Z"
    except OSError:
        return False


async def _until(done, *, what: str) -> None:
    deadline = time.monotonic() + SETTLE_TIMEOUT_SECONDS
    while not done():
        assert time.monotonic() < deadline, what
        await asyncio.sleep(0.05)


async def _pid(path: Path) -> int:
    await _until(lambda: path.exists() and path.read_text().strip(), what=f"{path.name} was never written")
    return int(path.read_text())


def _running_calls(owner: LiveTurnTools) -> int:
    """Return how many CLI calls of the reply still run, each of which holds one of its 64 slots."""
    return sum(not task.done() for task in owner._active)


@_RUNNER_MODES
async def test_stdout_and_stderr_arrive_apart_with_the_exit_code(tmp_path: Path, mode: str) -> None:
    """The command's stdout is the CLI's stdout and its stderr the CLI's stderr, with nothing added to either."""
    async with _stack(tmp_path, mode) as stack:
        assert await stack.run("--", "echo OUT; echo ERR >&2; exit 7") == (7, b"OUT\n", b"ERR\n")


@_RUNNER_MODES
async def test_five_megabytes_arrive_whole_whatever_tail_says(tmp_path: Path, mode: str) -> None:
    """Output has no cap, and `--tail` no longer cuts it."""
    expected = "".join(f"{number}\n" for number in range(1, 800_001)).encode()
    assert len(expected) > 5 * 1024 * 1024
    async with _stack(tmp_path, mode) as stack:
        code, stdout, stderr = await stack.run("--tail", "10", "--", "seq", "1", "800000")
    assert (code, stderr) == (0, b"")
    assert (len(stdout), hashlib.sha256(stdout).hexdigest()) == (len(expected), hashlib.sha256(expected).hexdigest())


async def test_binary_output_arrives_byte_for_byte_on_both_streams(tmp_path: Path) -> None:
    """Bytes that are no text, such as random ones, arrive unchanged on stdout and on stderr."""
    random = tmp_path / "random.bin"
    async with _stack(tmp_path) as stack:
        code, stdout, stderr = await stack.run(
            "--",
            f"head -c 1000 /dev/urandom > {random}; cat {random}; cat {random} >&2",
        )
    assert code == 0
    assert len(random.read_bytes()) == 1000
    assert (stdout, stderr) == (random.read_bytes(), random.read_bytes())


async def test_output_arrives_while_the_command_still_runs(tmp_path: Path) -> None:
    """The first line is on the CLI's stdout long before a command that then sleeps ends."""
    async with _stack(tmp_path) as stack:
        process = await stack.spawn("--", "echo first; sleep 4; echo second")
        assert process.stdout is not None
        async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
            assert await process.stdout.readline() == b"first\n"
            first_at = time.monotonic()
            assert await process.stdout.read() == b"second\n"
            assert await process.wait() == 0
        assert time.monotonic() - first_at > 3


async def test_stdin_reaches_the_command(tmp_path: Path) -> None:
    """Piped stdin reaches the command in order and whole, text or not, and its end ends the command's stdin."""
    payload = os.urandom(1024 * 1024)
    async with _stack(tmp_path) as stack:
        assert await stack.run("--", "cat", stdin=b"hi\n") == (0, b"hi\n", b"")
        code, stdout, stderr = await stack.run("--", "cat", stdin=payload)
        assert (code, stderr, hashlib.sha256(stdout).digest()) == (0, b"", hashlib.sha256(payload).digest())
        # A command that never reads its stdin still ends, and so does the CLI that forwards it.
        assert await stack.run("--", "echo done", stdin=payload) == (0, b"done\n", b"")


async def test_a_terminal_as_stdin_gives_the_command_an_empty_stdin(tmp_path: Path) -> None:
    """The CLI forwards no typing: a command that reads its stdin finds it at its end at once."""
    controller, terminal = os.openpty()
    try:
        async with _stack(tmp_path) as stack:
            process = await stack.spawn("--", "cat; echo after", stdin=terminal)
            async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                stdout, stderr = await process.communicate()
            assert (process.returncode, stdout, stderr) == (0, b"after\n", b"")
    finally:
        os.close(controller)
        os.close(terminal)


async def test_a_stopped_or_killed_command_keeps_its_output_and_gives_its_exit_code(tmp_path: Path) -> None:
    """A command stopped at its timeout exits 124 and one a signal killed 128 plus the signal, after their output."""
    async with _stack(tmp_path) as stack:
        assert await stack.run("--timeout", "1", "--", "echo partial; sleep 60") == (
            124,
            b"partial\n",
            b"mindroom-agent: stopped after 1s\n",
        )
        assert await stack.run("--", "echo before; kill -KILL $$") == (137, b"before\n", b"")


async def test_a_reader_that_stops_reading_stops_the_command_instead_of_filling_the_primary(tmp_path: Path) -> None:
    """While the CLI's stdout is not read, the primary holds a few frames and the command waits to write the rest."""
    size = 48 * 1024 * 1024
    done = tmp_path / "done"
    async with _stack(tmp_path) as stack:
        process = await stack.spawn("--", f"head -c {size} /dev/zero; touch {done}")
        assert process.stdout is not None
        await _until(lambda: _stream_frames(stack.owner) is not None, what="the call never streamed")
        frames = _stream_frames(stack.owner)
        deepest = 0
        for _ in range(60):
            await asyncio.sleep(0.05)
            deepest = max(deepest, frames.qsize())
        # Nothing was read for three seconds: the queue filled to its bound, and the command has not finished.
        assert deepest == frames.maxsize <= 16
        assert sum(len(frame["b"]) for frame in list(frames._queue)) <= 16 * 44 * 1024
        assert not done.exists()
        received = 0
        async with asyncio.timeout(4 * SETTLE_TIMEOUT_SECONDS):
            while chunk := await process.stdout.read(1024 * 1024):
                received += len(chunk)
                assert not chunk.strip(b"\0")
            assert await process.wait() == 0
        assert received == size
        assert done.exists()


def _stream_frames(owner: LiveTurnTools) -> Any:  # noqa: ANN401
    """Return the queue of frames the reply's one streamed call holds for its reader, once it has one."""
    streams = [call.stream for call in owner._calls.values() if call.stream is not None]
    return streams[0].frames if streams else None


async def _frames(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    async for line in response.aiter_lines():
        if line:
            yield json.loads(line)


async def test_frames_carry_at_most_32_kib_and_the_exit_follows_the_last_output(tmp_path: Path) -> None:
    """The stream is the command's output in frames, its exit after the last of them, then the call's receipt."""
    async with _stack(tmp_path) as stack, httpx.AsyncClient(base_url=stack.url, timeout=60) as http:
        operation = stack.submit("head -c 200000 /dev/zero; echo late >&2; exit 3", stream=True)
        assert (await http.post("/api/agent-cli/operations", headers=stack.headers, json=operation)).status_code == 200
        route = f"/api/agent-cli/calls/{operation['call_id']}/stream"
        async with http.stream("GET", route, headers=stack.headers) as response:
            assert response.status_code == 200
            assert response.headers["x-accel-buffering"] == "no"
            frames = [frame async for frame in _frames(response) if frame.get("t") != "keepalive"]
    kinds = [frame.get("t") for frame in frames]
    assert kinds[0] == "started"
    assert kinds[-2:] == ["exit", None]
    assert frames[-2] == {"t": "exit", "code": 3, "timed_out": False}
    output = {
        kind: b"".join(base64.b64decode(frame["b"]) for frame in frames if frame.get("t") == kind)
        for kind in ("out", "err")
    }
    assert output == {"out": bytes(200000), "err": b"late\n"}
    assert max(len(base64.b64decode(frame["b"])) for frame in frames if "b" in frame) <= 32 * 1024
    assert (frames[-1]["status"], frames[-1]["call_id"]) == ("failed", operation["call_id"])
    # The receipt, which the reply's tool trace shows, names the exit and holds none of the output.
    assert "Exit code 3" in frames[-1]["outcome"]
    assert len(frames[-1]["outcome"]) < 200


@_RUNNER_MODES
async def test_a_reader_that_hangs_up_stops_the_command_and_frees_its_slot(tmp_path: Path, mode: str) -> None:
    """A stream whose reader leaves cancels its call: the command's whole process group dies and the call ends."""
    leader, child = tmp_path / "leader", tmp_path / "child"
    async with _stack(tmp_path, mode) as stack, httpx.AsyncClient(base_url=stack.url, timeout=60) as http:
        operation = stack.submit(f"echo $$ > {leader}; sleep 300 & echo $! > {child}; echo up; wait", stream=True)
        assert (await http.post("/api/agent-cli/operations", headers=stack.headers, json=operation)).status_code == 200
        route = f"/api/agent-cli/calls/{operation['call_id']}"
        async with http.stream("GET", f"{route}/stream", headers=stack.headers) as response:
            async for frame in _frames(response):
                if frame.get("t") == "out":
                    break
            pids = [await _pid(leader), await _pid(child)]
            assert all(map(_alive, pids))
            assert _running_calls(stack.owner) == 1
        await _until(lambda: not any(map(_alive, pids)), what="the command outlived its reader")
        await _until(lambda: _running_calls(stack.owner) == 0, what="the call kept its slot")
        assert (await http.get(route, headers=stack.headers)).json()["status"] == "cancelled"


@pytest.mark.parametrize("then", ["wait", "yes"])
async def test_sigterm_to_the_cli_stops_the_command_and_says_so(tmp_path: Path, then: str) -> None:
    """The CLI cancels a command whose output it relays, quiet or not, and says it stopped once that is confirmed."""
    leader, child = tmp_path / "leader", tmp_path / "child"
    async with _stack(tmp_path) as stack:
        process = await stack.spawn("--", f"echo $$ > {leader}; sleep 300 & echo $! > {child}; echo up; {then}")
        assert process.stdout is not None
        assert process.stderr is not None
        async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
            assert await process.stdout.readline() == b"up\n"
            pids = [await _pid(leader), await _pid(child)]
            process.send_signal(signal.SIGTERM)
            assert await process.wait() == 128 + signal.SIGTERM
            assert (
                await process.stderr.read()
                == b"mindroom-agent: cancelled by SIGTERM; stopped the command in worker w\n"
            )
        # The notice is true when it is printed: nothing of the command is left.
        assert not any(map(_alive, pids))
        await _until(lambda: _running_calls(stack.owner) == 0, what="the call kept its slot")


@_CANNOT_START
async def test_a_program_that_cannot_start_exits_as_a_shell_reports_it_on_stderr(
    tmp_path: Path,
    program: str,
    exit_code: int,
    said: str,
) -> None:
    """A program the worker cannot start ends `workers run` as a shell would: 127 or 126, the reason on stderr."""
    async with _stack(tmp_path) as stack:
        assert await stack.run("--", program) == (exit_code, b"", said.encode())


async def test_a_closed_stdout_stops_the_command_and_exits_141(tmp_path: Path) -> None:
    """Like a command in a pipeline whose reader left, the CLI ends as SIGPIPE ends one, and its command stops."""
    leader = tmp_path / "leader"
    async with _stack(tmp_path) as stack:
        process = await stack.spawn("--", f"echo $$ > {leader}; yes")
        assert process.stdout is not None
        async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
            assert await process.stdout.readline() == b"y\n"
            pid = await _pid(leader)
            process._transport.get_pipe_transport(1).close()  # type: ignore[attr-defined]
            assert await process.wait() == 128 + signal.SIGPIPE
        await _until(lambda: not _alive(pid), what="the command outlived its reader")


async def test_only_the_calls_own_reply_reads_its_stream_or_writes_its_stdin(tmp_path: Path) -> None:
    """Another reply's grant reaches neither route, and a call has one reader."""
    async with _stack(tmp_path) as stack, httpx.AsyncClient(base_url=stack.url, timeout=60) as http:

        async def other_shell(args: str) -> str:
            return args

        other, other_token = await _make_owner(tmp_path / "other", other_shell, turn_id="other")
        stack.registry.register(other)
        operation = stack.submit("cat", stream=True)
        assert (await http.post("/api/agent-cli/operations", headers=stack.headers, json=operation)).status_code == 200
        route = f"/api/agent-cli/calls/{operation['call_id']}"
        chunk = {"seq": 0, "b": base64.b64encode(b"hi\n").decode(), "eof": False}
        try:
            for token in (other_token, "no-grant"):
                refused = {"Authorization": f"Bearer {token}"}
                assert (await http.get(f"{route}/stream", headers=refused)).status_code == 401
                assert (await http.post(f"{route}/stdin", headers=refused, json=chunk)).status_code == 401
            async with http.stream("GET", f"{route}/stream", headers=stack.headers) as response:
                frames = _frames(response)
                assert (await anext(frames))["t"] == "started"
                assert (await http.get(f"{route}/stream", headers=stack.headers)).status_code == 409
                # A chunk ahead of its turn is refused, and one sent twice is written once.
                ahead = await http.post(f"{route}/stdin", headers=stack.headers, json={**chunk, "seq": 1})
                assert ahead.status_code == 409
                for sent, still_open in ((chunk, True), (chunk, True), ({"seq": 1, "b": "", "eof": True}, False)):
                    written = await http.post(f"{route}/stdin", headers=stack.headers, json=sent)
                    assert (written.status_code, written.json()) == (200, {"open": still_open})
                rest = [frame async for frame in frames if frame.get("t") != "keepalive"]
            assert [frame.get("t") for frame in rest] == ["out", "exit", None]
            assert base64.b64decode(rest[0]["b"]) == b"hi\n"
        finally:
            await other.close()


async def test_a_streamed_call_nobody_reads_is_cancelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A call that asked to stream and whose reader never comes does not run on unread."""
    monkeypatch.setattr(turn, "_STREAM_ATTACH_SECONDS", 0.5)
    leader = tmp_path / "leader"
    async with _stack(tmp_path) as stack, httpx.AsyncClient(base_url=stack.url, timeout=60) as http:
        operation = stack.submit(f"echo $$ > {leader}; sleep 300", stream=True)
        assert (await http.post("/api/agent-cli/operations", headers=stack.headers, json=operation)).status_code == 200
        pid = await _pid(leader)
        await _until(lambda: not _alive(pid), what="the unread command kept running")
        # The call ends once the runner said its command stopped.
        route = f"/api/agent-cli/calls/{operation['call_id']}"
        async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
            receipt = {"status": "running"}
            while receipt["status"] == "running":
                receipt = (await http.get(route, params={"wait": 5}, headers=stack.headers)).json()
        assert (receipt["status"], receipt["cancel_outcome"]) == ("cancelled", "stopped")


@_RUNNER_MODES
async def test_a_call_that_does_not_stream_answers_as_it_did(tmp_path: Path, mode: str) -> None:
    """What a model reads from the shell tool, called directly or through `tools call`, is the text it was."""
    async with _stack(tmp_path, mode) as stack, httpx.AsyncClient(base_url=stack.url, timeout=60) as http:
        operation = stack.submit("echo OUT; echo ERR >&2; exit 7")
        assert (await http.post("/api/agent-cli/operations", headers=stack.headers, json=operation)).status_code == 200
        route = f"/api/agent-cli/calls/{operation['call_id']}"
        receipt: dict[str, Any] = {}
        async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
            while receipt.get("status") in (None, "queued", "running"):
                receipt = (await http.get(f"{route}?wait=5", headers=stack.headers)).json()
        assert (receipt["status"], receipt["outcome"]) == ("failed", _MODEL_RESULT)
        assert (await http.get(f"{route}/stream", headers=stack.headers)).status_code == 401
        long = stack.submit("seq 1 150")
        long["arguments"] = {**long["arguments"], "tail": 10}  # type: ignore[dict-item]
        assert (await http.post("/api/agent-cli/operations", headers=stack.headers, json=long)).status_code == 200
        async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
            while receipt.get("call_id") != long["call_id"] or receipt["status"] in ("queued", "running"):
                receipt = (
                    await http.get(f"/api/agent-cli/calls/{long['call_id']}?wait=5", headers=stack.headers)
                ).json()
        assert receipt[
            "outcome"
        ] == "[Showing the last 10 of 150 lines. Pass a larger tail, or narrow the command.]\n" + "".join(
            f"{number}\n" for number in range(141, 151)
        ).rstrip("\n")


@_CANNOT_START
async def test_a_program_that_cannot_start_exits_as_a_shell_reports_it_on_the_runners_event_loop(
    tmp_path: Path,
    program: str,
    exit_code: int,
    said: str,
) -> None:
    """Sandbox runners serve on uvloop, whose spawn errors do not name the program that could not start."""

    async def run() -> tuple[int | None, bytes, bytes]:
        async with _stack(tmp_path) as stack:
            return await stack.run("--", program)

    def on_uvloop() -> tuple[int | None, bytes, bytes]:
        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            return runner.run(run())

    # The test's own loop is not uvloop, so the stack runs on one of its own, in a thread.
    assert await asyncio.to_thread(on_uvloop) == (exit_code, b"", said.encode())


def _background_ticks(child: Path) -> tuple[str, ...]:
    """Return a command that ends at once and leaves a process behind that writes to its stdout for a minute."""
    return ("bash", "-c", f"(echo $BASHPID > {child}; for i in $(seq 600); do echo tick; sleep .1; done) &")


async def test_the_time_limit_covers_what_the_command_left_running(tmp_path: Path) -> None:
    """A command whose leader ended still runs while a process it started writes: its time limit stops that too."""
    child = tmp_path / "child"
    async with _stack(tmp_path) as stack:
        started = time.monotonic()
        code, stdout, stderr = await stack.run("--timeout", "1", "--", *_background_ticks(child))
        assert code == 124
        assert time.monotonic() - started < 10
        assert stdout.startswith(b"tick\n")
        assert stderr == b"mindroom-agent: stopped after 1s\n"
        assert not _alive(await _pid(child))


async def test_a_cancel_stops_what_the_command_left_running_before_it_says_so(tmp_path: Path) -> None:
    """The stop a cancel reports covers the whole process group, also once its leader has ended."""
    child = tmp_path / "child"
    async with _stack(tmp_path) as stack:
        process = await stack.spawn("--", *_background_ticks(child))
        assert process.stdout is not None
        assert process.stderr is not None
        async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
            assert await process.stdout.readline() == b"tick\n"
            pid = await _pid(child)
            process.send_signal(signal.SIGTERM)
            assert await process.wait() == 128 + signal.SIGTERM
            assert (
                await process.stderr.read()
                == b"mindroom-agent: cancelled by SIGTERM; stopped the command in worker w\n"
            )
        assert not _alive(pid)


async def test_a_reader_paused_past_the_calls_time_budget_still_gets_its_output_and_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Time the primary waits for a slow reader is not time it waited for the runner, so the call is not given up."""
    budgeted = sandbox_proxy.worker_proxy_client_config
    monkeypatch.setattr(
        sandbox_proxy,
        "worker_proxy_client_config",
        lambda *args, **kwargs: replace(budgeted(*args, **kwargs), proxy_timeout_seconds=1.0),
    )
    async with _stack(tmp_path) as stack:
        process = await stack.spawn("--timeout", "2", "--", "yes")
        assert process.stdout is not None
        assert process.stderr is not None
        await _until(lambda: _stream_frames(stack.owner) is not None, what="the call never streamed")
        # Nothing reads the CLI's stdout until the call's budget and the command's time limit have both passed.
        await asyncio.sleep(3)
        async with asyncio.timeout(4 * SETTLE_TIMEOUT_SECONDS):
            stdout, stderr = await process.communicate()
        assert process.returncode == 124
        assert stdout
        assert not stdout.replace(b"y\n", b"")
        assert stderr == b"mindroom-agent: stopped after 2s\n"


async def test_a_command_that_never_reads_stdin_holds_one_pending_chunk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chunks a command does not read are not piled up: one waits, a short time, and repeats of it wait for nothing."""
    monkeypatch.setattr(execution, "_STDIN_WRITE_SECONDS", 1.0, raising=False)
    chunk = base64.b64encode(bytes(32 * 1024)).decode()
    async with _stack(tmp_path) as stack, httpx.AsyncClient(base_url=stack.url, timeout=60) as http:
        operation = stack.submit("sleep 300", stream=True)
        assert (await http.post("/api/agent-cli/operations", headers=stack.headers, json=operation)).status_code == 200
        route = f"/api/agent-cli/calls/{operation['call_id']}"

        async def send(seq: int) -> httpx.Response:
            return await http.post(f"{route}/stdin", headers=stack.headers, json={"seq": seq, "b": chunk, "eof": False})

        async with http.stream("GET", f"{route}/stream", headers=stack.headers) as response:
            # The frames stay referenced: collecting their generator would close the stream, as a reader that left.
            frames = _frames(response)
            assert (await anext(frames))["t"] == "started"
            async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                seq = 0
                while (answer := await send(seq)).status_code == 200:
                    assert answer.json() == {"open": True}
                    seq += 1
                assert answer.status_code == 504
                # The pipe and one buffer filled, and then one chunk waits: nothing more is taken.
                assert seq <= 8
                started = time.monotonic()
                repeats = await asyncio.gather(*(send(seq) for _ in range(4)), send(seq + 1))
                assert {repeat.status_code for repeat in repeats} == {504}
                assert time.monotonic() - started < 4


@pytest.mark.parametrize("ending", ["expired", "revoked"])
async def test_a_stream_whose_grant_ended_sends_nothing_more(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    """A grant that expires or is revoked while its stream is open ends the stream, which stops the command."""
    leader = tmp_path / "leader"
    async with _stack(tmp_path) as stack, httpx.AsyncClient(base_url=stack.url, timeout=60) as http:
        operation = stack.submit(f"echo $$ > {leader}; echo one; sleep 1; echo two; sleep 300", stream=True)
        assert (await http.post("/api/agent-cli/operations", headers=stack.headers, json=operation)).status_code == 200
        route = f"/api/agent-cli/calls/{operation['call_id']}"
        async with http.stream("GET", f"{route}/stream", headers=stack.headers) as response:
            frames = _frames(response)
            async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                while (await anext(frames)).get("t") != "out":
                    pass
                pid = await _pid(leader)
                if ending == "revoked":
                    stack.owner.revoke()
                else:
                    now_ns = time.time_ns
                    monkeypatch.setattr(time, "time_ns", lambda: now_ns() + 10**15)
                rest = [frame async for frame in frames]
            assert [frame for frame in rest if frame.get("t") not in ("keepalive", "progress")] == []
        await _until(lambda: not _alive(pid), what="the command outlived its stream")


async def test_a_cancel_wakes_the_call_that_waits_for_its_reader(tmp_path: Path) -> None:
    """A cancel from elsewhere reaches a command whose reader has stopped reading, and learns that it stopped."""
    leader = tmp_path / "leader"
    async with _stack(tmp_path) as stack, httpx.AsyncClient(base_url=stack.url, timeout=60) as http:
        operation = stack.submit(f"echo $$ > {leader}; yes", stream=True)
        assert (await http.post("/api/agent-cli/operations", headers=stack.headers, json=operation)).status_code == 200
        route = f"/api/agent-cli/calls/{operation['call_id']}"
        async with http.stream("GET", f"{route}/stream", headers=stack.headers) as response:
            # The frames stay referenced: collecting their generator would close the stream, as a reader that left.
            frames = _frames(response)
            assert (await anext(frames))["t"] == "started"
            pid = await _pid(leader)
            # Wait until the unread output fills every buffer on the way and the queue stays full.
            full = 0
            async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                while full < 20:
                    await asyncio.sleep(0.05)
                    full = full + 1 if _stream_frames(stack.owner).full() else 0
            async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                cancelled = (await http.post(f"{route}/cancel", headers=stack.headers)).json()
            assert (cancelled["status"], cancelled["cancel_outcome"]) == ("cancelled", "stopped")
            assert not _alive(pid)


async def test_malformed_stdin_requests_are_refused_like_the_other_routes(tmp_path: Path) -> None:
    """A call ID that is none answers as an unknown one does, and a chunk's number is a number."""
    async with _stack(tmp_path) as stack, httpx.AsyncClient(base_url=stack.url, timeout=60) as http:
        chunk = {"seq": 0, "b": "", "eof": True}
        unknown = await http.post("/api/agent-cli/calls/not-a-call/stdin", headers=stack.headers, json=chunk)
        assert unknown.status_code == 401
        operation = stack.submit("cat", stream=True)
        assert (await http.post("/api/agent-cli/operations", headers=stack.headers, json=operation)).status_code == 200
        route = f"/api/agent-cli/calls/{operation['call_id']}/stdin"
        assert (await http.post(route, headers=stack.headers, json={**chunk, "seq": False})).status_code == 422
        # Nobody reads this call's output or ends its stdin: left alone it would hold the reply open for a minute.
        cancelled = await http.post(route.replace("/stdin", "/cancel"), headers=stack.headers)
        assert cancelled.json()["status"] == "cancelled"


async def test_output_written_before_the_time_limit_reaches_a_reader_that_was_paused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop at the time limit drops nothing the command had written, however long its reader takes to read it."""
    resume = asyncio.Event()
    stream_command = sandbox_runner.stream_command

    async def paused(*args: Any, on_output: Any, **kwargs: Any) -> None:  # noqa: ANN401
        async def held(kind: str, chunk: bytes) -> None:
            await resume.wait()
            await on_output(kind, chunk)

        await stream_command(*args, on_output=held, **kwargs)

    monkeypatch.setattr(sandbox_runner, "stream_command", paused)
    async with _stack(tmp_path) as stack:
        command = "head -c 100000 /dev/zero; head -c 50000 /dev/zero >&2; sleep 300"
        process = await stack.spawn("--timeout", "1", "--", command)
        # The reader takes nothing until well after the time limit stopped the command.
        await asyncio.sleep(3)
        resume.set()
        async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
            stdout, stderr = await process.communicate()
        assert process.returncode == 124
        assert (len(stdout), stdout.strip(b"\0")) == (100000, b"")
        assert stderr == bytes(50000) + b"mindroom-agent: stopped after 1s\n"


async def test_a_stdin_chunk_whose_connection_broke_is_sent_again_and_written_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chunk that may not have reached the worker is not the end of stdin: it is sent again, and written once."""
    post = httpx.Client.post
    posts = 0

    def breaking(self: httpx.Client, url: Any, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        nonlocal posts
        if not str(url).endswith("/stdin"):
            return post(self, url, **kwargs)
        posts += 1
        if posts == 2:
            msg = "the connection was reset before the chunk left"
            raise httpx.ConnectError(msg)
        response = post(self, url, **kwargs)
        if posts == 5:
            msg = "the connection was reset before the answer arrived"
            raise httpx.ReadError(msg)
        return response

    monkeypatch.setattr(httpx.Client, "post", breaking)
    sent = os.urandom(300_000)
    async with _stack(tmp_path) as stack:
        code, stdout, stderr = await stack.run("--", "cat; echo end >&2", stdin=sent)
    assert posts > 6
    assert (code, hashlib.sha256(stdout).hexdigest(), stderr) == (0, hashlib.sha256(sent).hexdigest(), b"end\n")


async def test_a_cancel_after_the_command_ended_on_its_own_does_not_say_it_stopped_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the runner's word that the process group is gone says "stopped": a command's own exit leaves it unsaid."""
    child = tmp_path / "child"
    stream_command = sandbox_runner.stream_command

    async def slow_to_answer(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        await stream_command(*args, **kwargs)
        await asyncio.sleep(300)

    monkeypatch.setattr(sandbox_runner, "stream_command", slow_to_answer)
    async with _stack(tmp_path) as stack, httpx.AsyncClient(base_url=stack.url, timeout=60) as http:
        # The command ends and leaves a server behind whose output goes elsewhere.
        operation = stack.submit(f"sleep 300 > /dev/null 2>&1 & echo $! > {child}; echo done", stream=True)
        assert (await http.post("/api/agent-cli/operations", headers=stack.headers, json=operation)).status_code == 200
        route = f"/api/agent-cli/calls/{operation['call_id']}"
        pid = await _pid(child)
        try:
            async with http.stream("GET", f"{route}/stream", headers=stack.headers) as response:
                frames = _frames(response)
                async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                    while (frame := await anext(frames)).get("t") != "exit":
                        pass
                    assert frame == {"t": "exit", "code": 0, "timed_out": False}
                    cancelled = (await http.post(f"{route}/cancel", headers=stack.headers)).json()
                assert (cancelled["status"], cancelled["cancel_outcome"]) == ("cancelled", None)
                assert _alive(pid)
        finally:
            os.kill(pid, signal.SIGKILL)


async def test_a_stream_that_was_asked_for_is_the_calls_one_reader_and_must_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asking for the stream claims it, before any of it is sent; a reader that then never reads is none."""
    monkeypatch.setattr(turn, "_STREAM_ATTACH_SECONDS", 1.0)
    leader = tmp_path / "leader"
    async with _stack(tmp_path) as stack, httpx.AsyncClient(base_url=stack.url, timeout=60) as http:
        operation = stack.submit(f"echo $$ > {leader}; sleep 300", stream=True)
        assert (await http.post("/api/agent-cli/operations", headers=stack.headers, json=operation)).status_code == 200
        never_read = stack.owner.stream_call(operation["call_id"])
        with pytest.raises(CliCallConflictError):
            stack.owner.stream_call(operation["call_id"])
        route = f"/api/agent-cli/calls/{operation['call_id']}/stream"
        assert (await http.get(route, headers=stack.headers)).status_code == 409
        pid = await _pid(leader)
        await _until(lambda: not _alive(pid), what="the command of a stream that never started kept running")
        await never_read.aclose()


async def test_the_time_limit_does_not_wait_for_a_process_that_left_the_group_and_keeps_writing(tmp_path: Path) -> None:
    """After the stop, only what the pipes still held is read: a writer the stop cannot reach is not waited for."""
    ticker = tmp_path / "ticker"
    ticks = f"echo \\$\\$ > {ticker}; for i in \\$(seq 100); do echo tick; sleep .1; done"
    async with _stack(tmp_path) as stack:
        started = time.monotonic()
        try:
            code, stdout, stderr = await stack.run(
                "--timeout",
                "1",
                "--",
                "bash",
                "-c",
                f'setsid bash -c "{ticks}" & wait',
            )
            elapsed = time.monotonic() - started
        finally:
            with suppress(ProcessLookupError):
                os.kill(await _pid(ticker), signal.SIGKILL)
        # The ticks go on for ten seconds; the stop at one second is reported well before they end.
        assert (code, stderr) == (124, b"mindroom-agent: stopped after 1s\n")
        assert elapsed < 6
        assert stdout.startswith(b"tick\n")
        assert not stdout.replace(b"tick\n", b"")


async def test_a_cancel_while_the_exit_waits_to_be_delivered_does_not_stop_what_the_command_left(
    tmp_path: Path,
) -> None:
    """A command that ended is not stopped by a cancel that comes while nobody takes its exit."""
    child = tmp_path / "child"
    exiting = asyncio.Event()
    stopped: list[bool] = []

    async def taken(*_: object) -> None:
        return

    async def on_exit(*_: object) -> None:
        # The frames ahead of the exit are unread, so it waits for room.
        exiting.set()
        await asyncio.Event().wait()

    command = asyncio.create_task(
        execution.stream_command(
            ["bash", "-c", f"sleep 300 > /dev/null 2>&1 & echo $! > {child}"],
            dict(os.environ),
            None,
            timeout=60,
            stdin=execution.CommandStdin(),
            on_start=taken,
            on_output=taken,
            on_exit=on_exit,
            on_stopped=lambda: stopped.append(True),
        ),
    )
    pid = await _pid(child)
    try:
        async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
            await exiting.wait()
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command
        assert (stopped, _alive(pid)) == ([], True)
    finally:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize("status", [429, 403])
async def test_a_refused_stdin_chunk_is_sent_again_or_ends_the_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """A refusal that passes gets the chunk again; one that stays is reported and cancels the command."""
    write_stdin = turn.LiveTurnTools.write_stdin
    writes = 0

    async def refusing(self: LiveTurnTools, call_id: str, seq: int, data: str, *, eof: bool) -> bool:
        nonlocal writes
        writes += 1
        if writes == 3:
            raise HTTPException(status_code=status, detail="refused")
        return await write_stdin(self, call_id, seq, data, eof=eof)

    monkeypatch.setattr(turn.LiveTurnTools, "write_stdin", refusing)
    sent = os.urandom(300_000)
    leader = tmp_path / "leader"
    async with _stack(tmp_path) as stack:
        async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
            code, stdout, stderr = await stack.run("--", f"echo $$ > {leader}; cat", stdin=sent)
        if status == 429:
            assert (code, hashlib.sha256(stdout).hexdigest(), stderr) == (0, hashlib.sha256(sent).hexdigest(), b"")
        else:
            # The command would wait for a chunk that never comes, so it is cancelled, and the CLI says why.
            assert code == 255
            assert stderr.startswith(b"mindroom-agent: the command's stdin was refused")
            assert sent.startswith(stdout)
            assert not _alive(await _pid(leader))


async def test_a_stdout_that_fails_its_writes_is_reported_and_stops_the_command(tmp_path: Path) -> None:
    """Only a stdout that is closed ends like SIGPIPE: one that is full is an error the CLI names."""
    leader = tmp_path / "leader"
    async with _stack(tmp_path) as stack:
        with open("/dev/full", "wb") as full:  # noqa: PTH123, ASYNC230
            process = await stack.spawn("--", f"echo $$ > {leader}; echo data; sleep 300", stdout=full)
            async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                _, stderr = await process.communicate()
        assert process.returncode == 1
        assert stderr.startswith(b"mindroom-agent: cannot write the command's output: No space left on device")
        await _until(lambda: not _alive(int(leader.read_text())), what="the command outlived its writer")


@pytest.mark.parametrize("status", [408, 429])
async def test_a_stdin_chunk_the_runner_asks_to_send_again_is_sent_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """An answer of the runner that passes is no refusal: the chunk is sent again, and written once."""
    write = execution.CommandStdin.write
    writes = 0

    async def busy_once(self: execution.CommandStdin, seq: int, data: bytes, *, eof: bool) -> bool:
        nonlocal writes
        writes += 1
        if writes == 3:
            raise HTTPException(status_code=status, detail="busy")
        return await write(self, seq, data, eof=eof)

    monkeypatch.setattr(execution.CommandStdin, "write", busy_once)
    sent = os.urandom(300_000)
    async with _stack(tmp_path) as stack:
        async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
            code, stdout, stderr = await stack.run("--", "cat", stdin=sent)
    assert writes > 3
    assert (code, hashlib.sha256(stdout).hexdigest(), stderr) == (0, hashlib.sha256(sent).hexdigest(), b"")


@pytest.mark.parametrize("cancel_answers", [True, False])
async def test_a_refused_stdin_chunk_stops_the_command_before_the_cli_writes_to_a_stderr_nobody_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_answers: bool,
) -> None:
    """The cancel goes out and the stream is closed before the CLI says why, whether or not the cancel is answered."""

    async def refusing(*_: object, **__: object) -> bool:
        raise HTTPException(status_code=403, detail="refused")

    async def failing(*_: object) -> dict[str, object]:
        raise HTTPException(status_code=500)

    monkeypatch.setattr(turn.LiveTurnTools, "write_stdin", refusing)
    if not cancel_answers:
        monkeypatch.setattr(turn.LiveTurnTools, "cancel_call", failing)
    leader = tmp_path / "leader"
    # A stderr that is full to its last byte: whatever the CLI writes to it waits until it is read.
    reading, writing = os.pipe()
    os.set_blocking(writing, False)
    for size in (4096, 1):
        with suppress(BlockingIOError):
            while True:
                os.write(writing, bytes(size))
    os.set_blocking(writing, True)
    async with _stack(tmp_path) as stack:
        try:
            process = await stack.spawn("--", f"echo $$ > {leader}; cat", stdin=asyncio.subprocess.PIPE, stderr=writing)
        finally:
            os.close(writing)
        try:
            assert process.stdin is not None
            process.stdin.write(b"data\n")
            pid = await _pid(leader)
            await _until(lambda: not _alive(pid), what="the command ran on while the CLI could not write")
            assert process.returncode is None
            async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                said = await asyncio.to_thread(lambda: open(reading, "rb").read())  # noqa: PTH123, SIM115
                assert await process.wait() == 255
            assert said.lstrip(b"\0").startswith(b"mindroom-agent: the command's stdin was refused (HTTP 403)")
        finally:
            with suppress(ProcessLookupError):
                process.kill()
